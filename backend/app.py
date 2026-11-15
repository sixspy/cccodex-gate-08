import os

from flask import Flask, jsonify, request

from auth import authenticate, issue_token, read_user
from db import (
    BadPolicy,
    ExpandContention,
    KeyNotQueued,
    PolicyLocked,
    PolicyNotPinned,
    connect,
    expand_key,
    expand_list,
    field_detail,
    init_db,
    land_scan,
    moves_for_day,
    overview,
    pin_policy,
    rebuild_day,
    settings,
)

app = Flask(__name__)
app.json.ensure_ascii = False


def _stamp(value):
    if value is None:
        return None
    return value.isoformat(sep=" ", timespec="minutes")


def _user():
    return read_user(request.headers.get("Authorization"))


def _guard():
    user = _user()
    if not user:
        return None, (jsonify({"detail": "未登录"}), 401)
    return user, None


def _writer():
    user, error = _guard()
    if error:
        return None, error
    if user["role"] != "writer":
        return None, (jsonify({"detail": "核对员只能查看"}), 403)
    return user, None


@app.get("/api/health")
def health():
    with connect() as conn:
        conn.execute("SELECT 1")
    return jsonify({"status": "ok", "service": "gate-event-lake"})


@app.post("/api/login")
def login():
    body = request.get_json(silent=True) or {}
    user = authenticate(str(body.get("username", "")).strip(), str(body.get("password", "")))
    if not user:
        return jsonify({"detail": "用户名或密码错误"}), 401
    return jsonify(
        {
            "access_token": issue_token(user),
            "username": user["username"],
            "role": user["role"],
            "title": user["title"],
        }
    )


@app.get("/api/dates")
def list_dates():
    if not _guard()[0]:
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT d.biz_date,
                   (SELECT COUNT(*) FROM ods_scan o WHERE o.biz_date = d.biz_date) AS ods_count,
                   (SELECT COUNT(*) FROM dwd_move m WHERE m.biz_date = d.biz_date) AS dwd_count,
                   (SELECT COUNT(*) FROM expand_queue q WHERE q.biz_date = d.biz_date
                      AND q.outcome = 'pending') AS new_key_count,
                   EXISTS(SELECT 1 FROM build_runs r WHERE r.biz_date = d.biz_date) AS built
            FROM (SELECT DISTINCT biz_date FROM ods_scan) d
            ORDER BY d.biz_date
            """
        ).fetchall()
    for row in rows:
        row["biz_date"] = row["biz_date"].isoformat()
    return jsonify(rows)


@app.get("/api/dates/<biz_date>")
def date_detail(biz_date):
    if not _guard()[0]:
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        ods = conn.execute(
            """
            SELECT id, terminal_code, raw_payload
            FROM ods_scan WHERE biz_date = %s ORDER BY id
            """,
            (biz_date,),
        ).fetchall()
        moves = moves_for_day(conn, biz_date)
        run = conn.execute(
            """
            SELECT id, accepted_count, rejected_count, built_by, built_at,
                   policy_used, policy_pinned, expand_deviation
            FROM build_runs WHERE biz_date = %s ORDER BY id DESC LIMIT 1
            """,
            (biz_date,),
        ).fetchone()
        rejects = []
        if run:
            rejects = conn.execute(
                """
                SELECT source_id, reason FROM build_rejects
                WHERE run_id = %s ORDER BY source_id
                """,
                (run["id"],),
            ).fetchall()
        queue = conn.execute(
            """
            SELECT ods_id, key_name, value_text, outcome, detail_column, settle_note
            FROM expand_queue WHERE biz_date = %s ORDER BY id
            """,
            (biz_date,),
        ).fetchall()
        settled = conn.execute(
            """
            SELECT q.ods_id, q.key_name, q.outcome, q.detail_column, q.settle_note
            FROM expand_queue q JOIN build_runs r ON r.id = q.run_id
            WHERE q.biz_date = %s AND q.run_id = (SELECT MAX(id) FROM build_runs WHERE biz_date = %s)
            ORDER BY q.id
            """,
            (biz_date, biz_date),
        ).fetchall()
        fields = field_detail(conn, biz_date)
        policy = settings(conn)
    if not ods:
        return jsonify({"detail": "这一天没有贴源扫描"}), 404
    for move in moves:
        move["event_at"] = _stamp(move["event_at"])
    if run:
        run["built_at"] = _stamp(run["built_at"])
    return jsonify(
        {
            "biz_date": biz_date,
            "ods": ods,
            "moves": moves,
            "run": run,
            "rejects": rejects,
            "new_keys": queue,
            "settled_keys": settled,
            "field_detail": fields["fields"],
            "empty_key_count": fields["empty_key_count"],
            "settings": policy,
        }
    )


@app.post("/api/dates/<biz_date>/build")
def build_date(biz_date):
    user, error = _writer()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    policy = body.get("policy") or None
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM ods_scan WHERE biz_date = %s LIMIT 1",
            (biz_date,),
        ).fetchone()
        if not exists:
            return jsonify({"detail": "这一天没有贴源扫描"}), 404
        try:
            summary = rebuild_day(conn, biz_date, user["username"], policy)
            conn.commit()
        except PolicyNotPinned as exc:
            conn.rollback()
            return jsonify({"detail": str(exc)}), 409
        except PolicyLocked as exc:
            conn.rollback()
            return jsonify({"detail": str(exc)}), 409
        except BadPolicy as exc:
            conn.rollback()
            return jsonify({"detail": str(exc)}), 400
    response = date_detail(biz_date)
    payload = response.get_json()
    payload["build_summary"] = summary
    return jsonify(payload)


@app.post("/api/dates/<biz_date>/land")
def land_scan_route(biz_date):
    """贴源落一条扫描原文，字典外的新键当场进队列。"""
    user, error = _writer()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    payload = body.get("payload")
    if not isinstance(payload, dict):
        return jsonify({"detail": "payload 要是一段原文对象"}), 400
    terminal = str(body.get("terminal") or payload.get("gate") or "T1").strip() or "T1"
    with connect() as conn:
        ods_id = land_scan(conn, biz_date, terminal, payload)
        conn.commit()
    return jsonify({"id": ods_id, "biz_date": biz_date}), 201


@app.get("/api/expand/settings")
def get_settings():
    if not _guard()[0]:
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        return jsonify(settings(conn))


@app.put("/api/expand/settings")
def put_settings():
    user, error = _writer()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    policy = str(body.get("policy") or "").strip()
    try:
        with connect() as conn:
            data = pin_policy(conn, policy, user["username"])
            conn.commit()
        return jsonify(data)
    except BadPolicy as exc:
        return jsonify({"detail": str(exc)}), 400
    except PolicyLocked as exc:
        return jsonify({"detail": str(exc)}), 409


@app.get("/api/expand")
def get_expand():
    if not _guard()[0]:
        return jsonify({"detail": "未登录"}), 401
    biz_date = request.args.get("biz_date") or None
    with connect() as conn:
        data = overview(conn, biz_date)
        conn.commit()
    return jsonify(data)


@app.get("/api/expand/list")
def get_expand_list():
    if not _guard()[0]:
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        return jsonify(expand_list(conn))


@app.post("/api/expand/keys/<key_name>/expand")
def post_expand_key(key_name):
    user, error = _writer()
    if error:
        return error
    try:
        with connect() as conn:
            data = expand_key(conn, key_name, user["username"])
            conn.commit()
        return jsonify(data)
    except PolicyNotPinned as exc:
        return jsonify({"detail": str(exc)}), 409
    except BadPolicy as exc:
        return jsonify({"detail": str(exc)}), 409
    except KeyNotQueued as exc:
        return jsonify({"detail": str(exc)}), 404
    except ExpandContention as exc:
        return jsonify({"detail": str(exc), "key_name": exc.key_name}), 409


@app.get("/api/dates/<biz_date>/fields")
def get_fields(biz_date):
    if not _guard()[0]:
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        return jsonify(field_detail(conn, biz_date))


init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
