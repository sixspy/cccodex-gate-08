import json
import os
import time
from collections import defaultdict
from datetime import date

import psycopg
from psycopg.rows import dict_row
from psycopg.sql import SQL, Identifier, Placeholder

from build import (
    KNOWN_COLUMNS,
    KNOWN_KEYS,
    OUTCOME_NAMES,
    POLICY_NAMES,
    clean_scan,
    detail_column_for,
    empty_key_count,
    extra_keys,
    value_text,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ods_scan (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    terminal_code TEXT NOT NULL,
    raw_payload JSONB NOT NULL,
    landed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dwd_move (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    source_id BIGINT NOT NULL REFERENCES ods_scan(id),
    terminal_code TEXT NOT NULL,
    container_no TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    event_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS build_runs (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    accepted_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    built_by TEXT NOT NULL,
    built_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS build_rejects (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES build_runs(id),
    source_id BIGINT NOT NULL REFERENCES ods_scan(id),
    reason TEXT NOT NULL
);

-- 扩列设置：全闸口一份。办法一经选定即钉住，本日不得更换。
CREATE TABLE IF NOT EXISTS expand_policy (
    id SMALLINT PRIMARY KEY,
    policy TEXT NOT NULL,
    pinned_by TEXT NOT NULL,
    pinned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    pinned_on DATE NOT NULL,
    CONSTRAINT expand_policy_single CHECK (id = 1),
    CONSTRAINT expand_policy_name CHECK (policy IN ('drop', 'expand'))
);

-- 扩列列表的新键登记：新键名。次数与办法名由 expand_list 视图当场算出。
CREATE TABLE IF NOT EXISTS expand_keys (
    key_name TEXT PRIMARY KEY,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 新键队列：贴源原文里的字典外键逐条排队。键名为空的原文不进队列。
CREATE TABLE IF NOT EXISTS expand_queue (
    id BIGSERIAL PRIMARY KEY,
    ods_id BIGINT NOT NULL REFERENCES ods_scan(id),
    biz_date DATE NOT NULL,
    key_name TEXT NOT NULL,
    value_text TEXT,
    outcome TEXT NOT NULL DEFAULT 'pending',
    detail_column TEXT,
    policy TEXT,
    run_id BIGINT REFERENCES build_runs(id),
    settled_by TEXT,
    queued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    settled_at TIMESTAMPTZ,
    settle_note TEXT,
    CONSTRAINT expand_queue_outcome CHECK (
        outcome IN ('pending', 'dropped', 'expanded', 'deviation', 'contention', 'rejected')
    ),
    CONSTRAINT expand_queue_key UNIQUE (ods_id, key_name)
);

-- 扩列争用：两名仓管同时给同一新键扩列，争到锁的一路落库，另一路记在这里。
CREATE TABLE IF NOT EXISTS expand_conflicts (
    id BIGSERIAL PRIMARY KEY,
    key_name TEXT NOT NULL,
    charged_by TEXT NOT NULL,
    charged_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    kept_value TEXT,
    lost_value TEXT,
    detail_column TEXT,
    note TEXT NOT NULL
);

-- 扩列办法偏了：收成时带上的办法跟扩列设置不合，新键不落库。
CREATE TABLE IF NOT EXISTS expand_deviations (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT REFERENCES build_runs(id),
    biz_date DATE NOT NULL,
    policy_used TEXT,
    policy_pinned TEXT,
    recorded_by TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    note TEXT NOT NULL
);

-- 明细层扩出来的列：字段详情靠它把原文键对到明细列。
CREATE TABLE IF NOT EXISTS dwd_extra_columns (
    column_name TEXT PRIMARY KEY,
    key_name TEXT NOT NULL UNIQUE,
    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS expand_queue_ods_idx ON expand_queue (ods_id);
CREATE INDEX IF NOT EXISTS expand_queue_key_idx ON expand_queue (key_name);

CREATE OR REPLACE VIEW expand_list AS
SELECT e.key_name,
       COALESCE(SUM(CASE WHEN q.outcome = 'dropped' THEN 1 ELSE 0 END), 0)::int AS drop_count,
       COALESCE(SUM(CASE WHEN q.outcome = 'expanded' THEN 1 ELSE 0 END), 0)::int AS expand_count,
       p.policy AS policy,
       CASE p.policy
            WHEN 'drop' THEN '丢掉字典外的键再入库'
            WHEN 'expand' THEN '把新键扩进明细列'
       END AS policy_name
FROM expand_keys e
LEFT JOIN expand_queue q ON q.key_name = e.key_name
LEFT JOIN expand_policy p ON p.id = 1
GROUP BY e.key_name, p.policy
ORDER BY e.key_name;
"""

POLICY_LOCK_KEY = 820208
TODAY = date.today


class PolicyNotPinned(Exception):
    pass


class PolicyLocked(Exception):
    pass


class BadPolicy(Exception):
    pass


class KeyNotQueued(Exception):
    pass


class ExpandContention(Exception):
    def __init__(self, key_name):
        super().__init__(f"扩列争用：{key_name} 另一路正在扩，本路不落库")
        self.key_name = key_name


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def init_db():
    last_error = None
    for _ in range(30):
        try:
            with connect() as conn:
                conn.execute(SCHEMA)
                conn.execute(
                    "ALTER TABLE dwd_move ADD COLUMN IF NOT EXISTS terminal_code TEXT"
                )
                conn.execute(
                    "ALTER TABLE build_runs ADD COLUMN IF NOT EXISTS policy_used TEXT"
                )
                conn.execute(
                    "ALTER TABLE build_runs ADD COLUMN IF NOT EXISTS policy_pinned TEXT"
                )
                conn.execute(
                    "ALTER TABLE build_runs ADD COLUMN IF NOT EXISTS expand_deviation BOOLEAN NOT NULL DEFAULT FALSE"
                )
                count = conn.execute("SELECT COUNT(*) AS n FROM ods_scan").fetchone()["n"]
                if count == 0:
                    _seed(conn)
                sync_queue(conn)
                conn.commit()
            return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(1)
    raise last_error


def _land(conn, biz_date, terminal, payload):
    row = conn.execute(
        """
        INSERT INTO ods_scan (biz_date, terminal_code, raw_payload)
        VALUES (%s, %s, %s::jsonb)
        RETURNING id
        """,
        (biz_date, terminal, json.dumps(payload, ensure_ascii=False)),
    ).fetchone()
    return row["id"]


def _seed(conn):
    built = [
        {"scan": "IN", "box": "CSNU1029384", "at": "2026-11-11T07:40:00", "gate": "T1"},
        {"scan": "OUT", "box": "CSNU1029384", "at": "2026-11-11T16:05:00", "gate": "T1"},
    ]
    waiting = [
        {"scan": "IN", "box": "TCLU5566778", "at": "2026-11-12T09:15:00", "gate": "T2"},
        {"scan": "XX", "box": "TCLU5566778", "at": "2026-11-12T11:00:00", "gate": "T2"},
        {"scan": "RS", "box": "BAD", "at": "2026-11-12T12:00:00", "gate": "T2"},
        # 带字典外新键的扫描，扩列办法还没钉住，先排队。
        {"scan": "IN", "box": "MSCU7788990", "at": "2026-11-12T14:20:00", "gate": "T2", "seal": "S-118"},
        {"scan": "OUT", "box": "MSCU7788990", "at": "2026-11-12T18:40:00", "gate": "T2", "seal": "S-118"},
        # 键名为空的原文键不进新键队列。
        {"scan": "OUT", "box": "TGHU3322110", "at": "2026-11-12T19:10:00", "gate": "T2", "": "无主语"},
    ]
    for payload in built:
        _land(conn, "2026-11-11", payload["gate"], payload)
    for payload in waiting:
        _land(conn, "2026-11-12", payload["gate"], payload)
    sync_queue(conn)
    rebuild_day(conn, "2026-11-11", "admin")


def land_scan(conn, biz_date, terminal, payload):
    ods_id = _land(conn, biz_date, terminal, payload)
    _enqueue_for(conn, ods_id, biz_date, payload)
    return ods_id


def _enqueue_for(conn, ods_id, biz_date, payload):
    for key_name, value in extra_keys(payload):
        conn.execute(
            """
            INSERT INTO expand_keys (key_name) VALUES (%s)
            ON CONFLICT (key_name) DO UPDATE SET last_seen_at = NOW()
            """,
            (key_name,),
        )
        conn.execute(
            """
            INSERT INTO expand_queue (ods_id, biz_date, key_name, value_text)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (ods_id, key_name) DO NOTHING
            """,
            (ods_id, biz_date, key_name, value_text(value)),
        )


def sync_queue(conn):
    """贴源里已有、队列里还没有的字典外键补排进队列；键名为空的不进。"""
    rows = conn.execute(
        "SELECT id, biz_date, raw_payload FROM ods_scan ORDER BY id"
    ).fetchall()
    seen = {
        (row["ods_id"], row["key_name"])
        for row in conn.execute("SELECT ods_id, key_name FROM expand_queue").fetchall()
    }
    for row in rows:
        payload = row["raw_payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        for key_name, value in extra_keys(payload):
            if (row["id"], key_name) in seen:
                continue
            _enqueue_for(conn, row["id"], row["biz_date"], {key_name: value})


def policy_of(conn):
    return conn.execute(
        "SELECT policy, pinned_by, pinned_at, pinned_on FROM expand_policy WHERE id = 1"
    ).fetchone()


def policy_label(policy):
    return POLICY_NAMES.get(policy)


def settings(conn):
    row = policy_of(conn)
    if row is None:
        return {
            "status": "unset",
            "policy": None,
            "policy_name": None,
            "pinned_by": None,
            "pinned_at": None,
            "pinned_on": None,
            "can_change": True,
            "locked_today": False,
            "choices": [
                {"value": key, "label": label, "selected": False}
                for key, label in POLICY_NAMES.items()
            ],
        }
    return {
        "status": "pinned",
        "policy": row["policy"],
        "policy_name": policy_label(row["policy"]),
        "pinned_by": row["pinned_by"],
        "pinned_at": row["pinned_at"].isoformat(sep=" ", timespec="minutes"),
        "pinned_on": row["pinned_on"].isoformat(),
        "can_change": False,
        "locked_today": True,
        "choices": [
            {"value": key, "label": label, "selected": key == row["policy"]}
            for key, label in POLICY_NAMES.items()
        ],
    }


def pin_policy(conn, policy: str, username: str):
    if policy not in POLICY_NAMES:
        raise BadPolicy("扩列办法只能选丢掉字典外的键再入库，或把新键扩进明细列")
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (POLICY_LOCK_KEY,))
    row = policy_of(conn)
    if row is None:
        conn.execute(
            """
            INSERT INTO expand_policy (id, policy, pinned_by, pinned_at, pinned_on)
            VALUES (1, %s, %s, NOW(), %s)
            """,
            (policy, username, TODAY()),
        )
        return settings(conn)
    if row["policy"] == policy:
        return settings(conn)
    # 办法一旦钉住就整湖沿用：不许今天这一个办法、明天换一个，也不许本日更换。
    raise PolicyLocked("扩列办法已钉住，本日不得更换，全湖沿用同一办法")


def _take_key_lock(conn, key_name: str) -> bool:
    return conn.execute(
        "SELECT pg_try_advisory_xact_lock(hashtext(%s)::bigint) AS ok",
        (f"expand:{key_name}",),
    ).fetchone()["ok"]


def _ensure_column(conn, key_name: str) -> str:
    registered = conn.execute(
        "SELECT column_name FROM dwd_extra_columns WHERE key_name = %s", (key_name,)
    ).fetchone()
    if registered:
        return registered["column_name"]
    taken = {
        row["column_name"]
        for row in conn.execute("SELECT column_name FROM dwd_extra_columns").fetchall()
    }
    column_name = detail_column_for(key_name, taken)
    conn.execute(
        SQL("ALTER TABLE dwd_move ADD COLUMN IF NOT EXISTS {} JSONB").format(
            Identifier(column_name)
        )
    )
    conn.execute(
        """
        INSERT INTO dwd_extra_columns (column_name, key_name)
        VALUES (%s, %s)
        ON CONFLICT (key_name) DO NOTHING
        """,
        (column_name, key_name),
    )
    return column_name


def _record_conflict(conn, key_name, username, kept_value, lost_value, column_name, note):
    conn.execute(
        """
        INSERT INTO expand_conflicts
            (key_name, charged_by, kept_value, lost_value, detail_column, note)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (key_name, username, kept_value, lost_value, column_name, note),
    )


def _record_conflict_standalone(key_name, username, kept_value, lost_value, column_name, note):
    """争用的这一路要回滚，争用记录单独落一笔，不然跟着回滚就没了。"""
    with connect() as conn:
        _record_conflict(
            conn, key_name, username, kept_value, lost_value, column_name, note
        )
        conn.commit()


def _settle_drop(conn, item, username, run_id):
    conn.execute(
        """
        UPDATE expand_queue
        SET outcome = 'dropped', detail_column = NULL, policy = 'drop', run_id = %s,
            settled_by = %s, settled_at = NOW(), settle_note = %s
        WHERE id = %s
        """,
        (run_id, username, "按扩列办法丢掉字典外的键", item["id"]),
    )
    return "dropped", None


def _settle_expand(conn, item, username, run_id):
    key_name = item["key_name"]
    if not _take_key_lock(conn, key_name):
        _record_conflict(
            conn,
            key_name,
            username,
            None,
            item["value_text"],
            detail_column_for(key_name),
            "扩列争用：同源加锁没取得，另一路先扩，本路不落库",
        )
        conn.execute(
            """
            UPDATE expand_queue
            SET outcome = 'contention', detail_column = %s, policy = 'expand', run_id = %s,
                settled_by = %s, settled_at = NOW(), settle_note = %s
            WHERE id = %s
            """,
            (
                detail_column_for(key_name),
                run_id,
                username,
                "扩列争用，新键不落库",
                item["id"],
            ),
        )
        return "contention", detail_column_for(key_name)
    column_name = _ensure_column(conn, key_name)
    conn.execute(
        """
        UPDATE expand_queue
        SET outcome = 'expanded', detail_column = %s, policy = 'expand', run_id = %s,
            settled_by = %s, settled_at = NOW(), settle_note = NULL
        WHERE id = %s
        """,
        (column_name, run_id, username, item["id"]),
    )
    return "expanded", column_name


def _record_deviation(conn, item, username, run_id, used, pinned):
    conn.execute(
        """
        UPDATE expand_queue
        SET outcome = 'deviation', detail_column = NULL, policy = %s, run_id = %s,
            settled_by = %s, settled_at = NOW(), settle_note = %s
        WHERE id = %s
        """,
        (used, run_id, username, "扩列办法偏了，新键不落库", item["id"]),
    )


def rebuild_day(conn, biz_date: str, username: str, requested_policy: str | None = None):
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)", (f"build:{biz_date}",)
    )
    sync_queue(conn)
    policy_row = policy_of(conn)
    pinned = policy_row["policy"] if policy_row else None

    if requested_policy is not None and requested_policy not in POLICY_NAMES:
        raise BadPolicy("扩列办法只能选丢掉字典外的键再入库，或把新键扩进明细列")

    rows = conn.execute(
        "SELECT id, terminal_code, raw_payload FROM ods_scan WHERE biz_date = %s ORDER BY id",
        (biz_date,),
    ).fetchall()
    parsed = []
    for row in rows:
        payload = row["raw_payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        cleaned, reason = clean_scan(payload)
        parsed.append((row, payload, cleaned, reason))

    # 扩列还没钉住，这一天只要有带新键的扫描，就不准收成。
    queue = conn.execute(
        """
        SELECT q.id, q.ods_id, q.key_name, q.value_text, q.outcome
        FROM expand_queue q
        JOIN ods_scan o ON o.id = q.ods_id
        WHERE o.biz_date = %s
        ORDER BY q.id
        """,
        (biz_date,),
    ).fetchall()
    queued = defaultdict(list)
    for item in queue:
        queued[item["ods_id"]].append(item)

    if pinned is None and queue:
        raise PolicyNotPinned("扩列办法尚未钉住，不准收成带新键的扫描")

    deviation = bool(requested_policy and pinned and requested_policy != pinned)
    used_policy = requested_policy or pinned

    run = conn.execute(
        """
        INSERT INTO build_runs
            (biz_date, accepted_count, rejected_count, built_by,
             policy_used, policy_pinned, expand_deviation)
        VALUES (%s, 0, 0, %s, %s, %s, %s)
        RETURNING id
        """,
        (biz_date, username, used_policy, pinned, deviation),
    ).fetchone()
    if deviation:
        conn.execute(
            """
            INSERT INTO expand_deviations
                (run_id, biz_date, policy_used, policy_pinned, recorded_by, note)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                run["id"],
                biz_date,
                requested_policy,
                pinned,
                username,
                "收成时带上的扩列办法跟扩列设置不合，记扩列办法偏了，新键不落库",
            ),
        )

    conn.execute("DELETE FROM dwd_move WHERE biz_date = %s", (biz_date,))
    accepted = 0
    rejected = 0
    tally = {"dropped": 0, "expanded": 0, "contention": 0, "deviation": 0}

    for row, payload, cleaned, reason in parsed:
        if reason:
            conn.execute(
                "INSERT INTO build_rejects (run_id, source_id, reason) VALUES (%s, %s, %s)",
                (run["id"], row["id"], reason),
            )
            rejected += 1
            for item in queued[row["id"]]:
                if item["outcome"] in ("dropped", "expanded"):
                    continue
                conn.execute(
                    """
                    UPDATE expand_queue
                    SET outcome = 'rejected', run_id = %s, settled_by = %s,
                        settled_at = NOW(), settle_note = %s
                    WHERE id = %s
                    """,
                    (run["id"], username, "扫描被剔除，新键不处置", item["id"]),
                )
            continue

        extra = {}
        for item in queued[row["id"]]:
            if deviation:
                # 办法不合：本次收成一条新键都不落库，队列里没处置过的记偏了。
                if item["outcome"] in ("pending", "deviation", "contention", "rejected"):
                    _record_deviation(conn, item, username, run["id"], requested_policy, pinned)
                    tally["deviation"] += 1
                continue
            if item["outcome"] in ("dropped", "expanded"):
                # 早先已处置的照旧：扩进过的明细列照带上，不再记一次次数。
                if item["outcome"] == "expanded":
                    extra[item["key_name"]] = item["value_text"]
                continue
            if pinned == "drop":
                _settle_drop(conn, item, username, run["id"])
                tally["dropped"] += 1
            elif pinned == "expand":
                outcome, _ = _settle_expand(conn, item, username, run["id"])
                tally[outcome] += 1
                if outcome == "expanded":
                    extra[item["key_name"]] = item["value_text"]

        _insert_move(conn, biz_date, row, cleaned, extra)
        accepted += 1

    conn.execute(
        """
        UPDATE build_runs SET accepted_count = %s, rejected_count = %s WHERE id = %s
        """,
        (accepted, rejected, run["id"]),
    )
    state = conn.execute(
        """
        SELECT q.outcome, COUNT(*) AS n
        FROM expand_queue q
        WHERE q.biz_date = %s
        GROUP BY q.outcome
        """,
        (biz_date,),
    ).fetchall()
    summary = {
        "run_id": run["id"],
        "policy": pinned,
        "policy_used": used_policy,
        "policy_deviation": deviation,
        # 本次收成处置掉的新键条数
        "settled_this_run": tally,
        # 这一日新键队列的整体去向
        "queue_state": {row["outcome"]: row["n"] for row in state},
        "empty_key_count": sum(
            empty_key_count(payload) for _, payload, _, _ in parsed
        ),
    }
    return summary


def _insert_move(conn, biz_date, row, cleaned, extra):
    base = [
        "biz_date",
        "source_id",
        "terminal_code",
        "container_no",
        "event_kind",
        "event_at",
    ]
    columns = [Identifier(name) for name in base]
    values = [Placeholder() for _ in base]
    params = [
        biz_date,
        row["id"],
        row["terminal_code"],
        cleaned["container_no"],
        cleaned["event_kind"],
        cleaned["event_at"],
    ]
    for key_name, value in extra.items():
        column_name = _column_for(conn, key_name)
        columns.append(Identifier(column_name))
        values.append(SQL("{}::jsonb").format(Placeholder()))
        params.append(json.dumps(value, ensure_ascii=False))
    statement = SQL("INSERT INTO dwd_move ({}) VALUES ({})").format(
        SQL(", ").join(columns), SQL(", ").join(values)
    )
    conn.execute(statement, params)


def _column_for(conn, key_name: str) -> str:
    """新键登记过的列名；还没有就当场扩一列。"""
    return _ensure_column(conn, key_name)


def expand_key(conn, key_name: str, username: str):
    """仓管手动给同一新键扩列。同源加锁，没争到锁的一路记扩列争用。"""
    row = policy_of(conn)
    if row is None:
        raise PolicyNotPinned("扩列办法尚未钉住，不准扩列")
    if row["policy"] != "expand":
        raise BadPolicy("扩列设置是丢掉字典外的键，不能再扩进明细列")

    pending = conn.execute(
        """
        SELECT id, ods_id, key_name, value_text, outcome FROM expand_queue
        WHERE key_name = %s AND outcome IN ('pending', 'deviation', 'contention')
        ORDER BY id
        """,
        (key_name,),
    ).fetchall()
    if not pending:
        registered = conn.execute(
            "SELECT column_name FROM dwd_extra_columns WHERE key_name = %s", (key_name,)
        ).fetchone()
        if registered:
            # 另一路已经扩成这一列：本路不再落库，只记扩列争用。
            _record_conflict_standalone(
                key_name,
                username,
                None,
                None,
                registered["column_name"],
                "两名仓管同时给同一新键扩列，另一路先扩成，本路记扩列争用",
            )
            raise ExpandContention(key_name)
        raise KeyNotQueued(f"新键队列里没有 {key_name}")

    if not _take_key_lock(conn, key_name):
        _record_conflict_standalone(
            key_name,
            username,
            None,
            value_text(pending[0]["value_text"]),
            detail_column_for(key_name),
            "两名仓管同时给同一新键扩列，同源加锁后只收下与扩列设置相符的写法，另一路记扩列争用",
        )
        raise ExpandContention(key_name)

    column_name = _ensure_column(conn, key_name)
    for item in pending:
        conn.execute(
            """
            UPDATE expand_queue
            SET outcome = 'expanded', detail_column = %s, policy = 'expand',
                settled_by = %s, settled_at = NOW(), settle_note = NULL
            WHERE id = %s
            """,
            (column_name, username, item["id"]),
        )
        conn.execute(
            SQL("UPDATE dwd_move SET {} = %s::jsonb WHERE source_id = %s").format(
                Identifier(column_name)
            ),
            (json.dumps(item["value_text"], ensure_ascii=False), item["ods_id"]),
        )
    return {"key_name": key_name, "column": column_name, "expanded": len(pending)}


def expand_list(conn):
    return conn.execute("SELECT * FROM expand_list").fetchall()


def moves_for_day(conn, biz_date: str):
    """明细层的行，连带扩出来的列也一并读出来。"""
    extra = conn.execute(
        "SELECT column_name, key_name FROM dwd_extra_columns ORDER BY key_name"
    ).fetchall()
    columns = [
        "source_id",
        "terminal_code",
        "container_no",
        "event_kind",
        "event_at",
    ]
    select = [Identifier(name) for name in columns] + [
        Identifier(row["column_name"]) for row in extra
    ]
    statement = SQL(
        "SELECT {} FROM dwd_move WHERE biz_date = %s ORDER BY event_at, id"
    ).format(SQL(", ").join(select))
    rows = conn.execute(statement, (biz_date,)).fetchall()
    for row in rows:
        row["extras"] = {
            item["key_name"]: row.pop(item["column_name"])
            for item in extra
            if item["column_name"] in row
        }
    return rows


def overview(conn, biz_date: str | None = None):
    sync_queue(conn)
    queue_sql = """
        SELECT q.id, q.ods_id, q.biz_date, q.key_name, q.value_text, q.outcome,
               q.detail_column, q.policy, q.settled_by, q.settled_at, q.settle_note
        FROM expand_queue q
    """
    params = ()
    if biz_date:
        queue_sql += " WHERE q.biz_date = %s"
        params = (biz_date,)
    queue_sql += " ORDER BY q.biz_date, q.id"
    queue = conn.execute(queue_sql, params).fetchall()
    for item in queue:
        item["biz_date"] = item["biz_date"].isoformat()
        item["outcome_name"] = OUTCOME_NAMES.get(item["outcome"], item["outcome"])
        if item["settled_at"]:
            item["settled_at"] = item["settled_at"].isoformat(sep=" ", timespec="minutes")

    conflicts = conn.execute(
        """
        SELECT key_name, charged_by, charged_at, kept_value, lost_value, detail_column, note
        FROM expand_conflicts ORDER BY id DESC LIMIT 50
        """
    ).fetchall()
    for item in conflicts:
        item["charged_at"] = item["charged_at"].isoformat(sep=" ", timespec="minutes")

    deviations = conn.execute(
        """
        SELECT run_id, biz_date, policy_used, policy_pinned, recorded_by, recorded_at, note
        FROM expand_deviations ORDER BY id DESC LIMIT 50
        """
    ).fetchall()
    for item in deviations:
        item["biz_date"] = item["biz_date"].isoformat()
        item["recorded_at"] = item["recorded_at"].isoformat(sep=" ", timespec="minutes")

    empty = 0
    for row in conn.execute("SELECT raw_payload FROM ods_scan").fetchall():
        payload = row["raw_payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        empty += empty_key_count(payload)

    return {
        "settings": settings(conn),
        "keys": expand_list(conn),
        "queue": queue,
        "conflicts": conflicts,
        "deviations": deviations,
        "empty_key_count": empty,
    }


def field_detail(conn, biz_date: str):
    """字段详情：贴源原文里的键逐一对到明细层的列。"""
    sync_queue(conn)
    rows = conn.execute(
        "SELECT id, raw_payload FROM ods_scan WHERE biz_date = %s ORDER BY id",
        (biz_date,),
    ).fetchall()
    queue = conn.execute(
        "SELECT key_name, outcome, detail_column FROM expand_queue WHERE biz_date = %s",
        (biz_date,),
    ).fetchall()
    by_key = defaultdict(list)
    for item in queue:
        by_key[item["key_name"]].append(item)

    fields = {}
    for row in rows:
        payload = row["raw_payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        for key in payload:
            name = str(key)
            field = fields.get(name)
            if field is None:
                field = {
                    "key_name": name,
                    "empty_name": not name.strip(),
                    "in_dictionary": name in KNOWN_KEYS,
                    "detail_column": None,
                    "status": None,
                    "status_name": None,
                    "expanded_count": 0,
                    "dropped_count": 0,
                    "pending_count": 0,
                    "deviation_count": 0,
                    "seen_count": 0,
                    "source_ids": [],
                }
                fields[name] = field
            field["seen_count"] += 1
            field["source_ids"].append(row["id"])

    for name, field in fields.items():
        if name in KNOWN_KEYS:
            field["detail_column"] = KNOWN_COLUMNS[name]
            field["status"] = "known"
            field["status_name"] = "字典内的键"
            continue
        if field["empty_name"]:
            field["status"] = "empty"
            field["status_name"] = "键名为空，不进新键队列"
            continue
        outcomes = by_key.get(name, [])
        field["expanded_count"] = sum(1 for item in outcomes if item["outcome"] == "expanded")
        field["dropped_count"] = sum(1 for item in outcomes if item["outcome"] == "dropped")
        field["pending_count"] = sum(
            1 for item in outcomes if item["outcome"] in ("pending", "rejected")
        )
        field["deviation_count"] = sum(
            1 for item in outcomes if item["outcome"] in ("deviation", "contention")
        )
        expanded = next(
            (item for item in outcomes if item["outcome"] == "expanded"), None
        )
        if field["expanded_count"]:
            field["detail_column"] = expanded["detail_column"]
            field["status"] = "expanded"
            field["status_name"] = "已扩进明细列"
        elif field["deviation_count"]:
            field["status"] = "deviation"
            field["status_name"] = "偏了 / 争用，新键未落库"
        elif field["dropped_count"]:
            field["status"] = "dropped"
            field["status_name"] = "已丢掉"
        elif outcomes:
            field["status"] = "pending"
            field["status_name"] = "待定"
        else:
            field["status"] = "pending"
            field["status_name"] = "待定"
    return {
        "biz_date": biz_date,
        "fields": list(fields.values()),
        "empty_key_count": sum(
            1 for field in fields.values() if field["empty_name"]
        ),
    }
