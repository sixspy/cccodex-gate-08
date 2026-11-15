import { useEffect, useMemo, useState } from "react";

function loadProfile() {
  try {
    return JSON.parse(localStorage.getItem("lake.profile") || "null");
  } catch {
    return null;
  }
}

async function request(path, options = {}) {
  const token = localStorage.getItem("lake.token") || "";
  const headers = { ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || "请求没有完成");
  }
  return data;
}

const OUTCOME_TONE = {
  pending: "wait",
  dropped: "drop",
  expanded: "grow",
  contention: "rival",
  deviation: "off",
};

const POLICY_CHOICES = [
  { value: "drop", label: "丢掉字典外的键再入库", hint: "新键不扩进明细，只在扩列列表里记账。" },
  { value: "expand", label: "把新键扩进明细列", hint: "新键在明细层各占一列，原文值照抄进去。" },
];

function fmt(value) {
  return value === null || value === undefined || value === "" ? "—" : value;
}

export default function App() {
  const [profile, setProfile] = useState(loadProfile);
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("123456");
  const [loginError, setLoginError] = useState("");
  const [dates, setDates] = useState([]);
  const [detail, setDetail] = useState(null);
  const [expand, setExpand] = useState(null);
  const [formError, setFormError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const writer = profile?.role === "writer";
  const settings = expand?.settings || detail?.settings;

  async function refresh(prefer) {
    const rows = await request("/api/dates");
    setDates(rows);
    const target = prefer || detail?.biz_date || rows[0]?.biz_date;
    if (target) {
      setDetail(await request(`/api/dates/${target}`));
      setExpand(await request(`/api/expand?biz_date=${target}`));
    }
  }

  async function reloadDay(bizDate) {
    setDetail(await request(`/api/dates/${bizDate}`));
    setExpand(await request(`/api/expand?biz_date=${bizDate}`));
    setDates(await request("/api/dates"));
  }

  useEffect(() => {
    if (!profile) return;
    refresh().catch(() => {
      localStorage.removeItem("lake.token");
      localStorage.removeItem("lake.profile");
      setProfile(null);
    });
  }, [profile]);

  async function submitLogin(event) {
    event.preventDefault();
    setLoginError("");
    try {
      const data = await request("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      localStorage.setItem("lake.token", data.access_token);
      const next = { username: data.username, role: data.role, title: data.title };
      localStorage.setItem("lake.profile", JSON.stringify(next));
      setProfile(next);
    } catch (error) {
      setLoginError(error.message);
    }
  }

  async function buildDay() {
    if (!detail) return;
    setBusy(true);
    setFormError("");
    setNotice("");
    try {
      const data = await request(`/api/dates/${detail.biz_date}/build`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const summary = data.build_summary;
      const settled = summary?.settled_this_run || {};
      setNotice(
        summary?.policy_deviation
          ? `收成结束：扩列办法偏了，新键没落库（本次记偏 ${settled.deviation || 0} 条）。`
          : `收成结束：本次扩进 ${settled.expanded || 0} 条、丢掉 ${settled.dropped || 0} 条、争用 ${settled.contention || 0} 条。`
      );
      await reloadDay(detail.biz_date);
    } catch (error) {
      setFormError(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function choosePolicy(policy) {
    setBusy(true);
    setFormError("");
    setNotice("");
    try {
      const data = await request("/api/expand/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ policy }),
      });
      setExpand((prev) => ({ ...(prev || {}), settings: data }));
      if (detail) {
        setDetail((prev) => ({ ...prev, settings: data }));
        await reloadDay(detail.biz_date);
      }
      setNotice("扩列办法已钉住。本日不再更换。");
    } catch (error) {
      setFormError(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function expandOne(keyName) {
    if (!detail) return;
    setBusy(true);
    setFormError("");
    setNotice("");
    try {
      const data = await request(`/api/expand/keys/${encodeURIComponent(keyName)}/expand`, {
        method: "POST",
      });
      setNotice(`新键 ${data.key_name} 已扩进明细列 ${data.column}，共 ${data.expanded} 条。`);
      await reloadDay(detail.biz_date);
    } catch (error) {
      setFormError(error.message);
      await reloadDay(detail.biz_date).catch(() => {});
    } finally {
      setBusy(false);
    }
  }

  function logout() {
    localStorage.removeItem("lake.token");
    localStorage.removeItem("lake.profile");
    setProfile(null);
    setDetail(null);
    setExpand(null);
  }

  const queue = expand?.queue || [];
  const keys = expand?.keys || [];
  const conflicts = expand?.conflicts || [];
  const deviations = expand?.deviations || [];
  const fields = detail?.field_detail || [];

  const queueBySource = useMemo(() => {
    const grouped = {};
    for (const item of detail?.new_keys || []) {
      (grouped[item.ods_id] = grouped[item.ods_id] || []).push(item);
    }
    return grouped;
  }, [detail]);

  if (!profile) {
    return (
      <main className="sheet gate">
        <p className="kicker">贴源层 → 明细层</p>
        <h1>闸口事件湖仓台</h1>
        <p>扫描原文先落贴源。只有字典认得出的事件才收进明细。</p>
        <form onSubmit={submitLogin}>
          <label>用户<input value={username} onChange={(event) => setUsername(event.target.value)} /></label>
          <label>口令<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
          <button>打开这一层</button>
          {loginError ? <p className="warn">{loginError}</p> : null}
        </form>
      </main>
    );
  }

  return (
    <>
      <header className="bar">
        <div>
          <p className="kicker">闸口扫描 · 分层</p>
          <h1>闸口事件湖仓台</h1>
        </div>
        <div className="who">
          <span>{profile.title} {profile.username}</span>
          <button className="line" onClick={logout}>离开</button>
        </div>
      </header>
      <main className="layout">
        <aside>
          {dates.map((row) => (
            <button
              key={row.biz_date}
              className={detail?.biz_date === row.biz_date ? "day on" : "day"}
              onClick={() => {
                setNotice("");
                setFormError("");
                setDetail(null);
                request(`/api/dates/${row.biz_date}`)
                  .then((data) => {
                    setDetail(data);
                    return request(`/api/expand?biz_date=${row.biz_date}`);
                  })
                  .then(setExpand);
              }}
            >
              <strong>{row.biz_date}</strong>
              <em>{row.built ? "明细已收" : "尚未构建"}</em>
              <span>贴源 {row.ods_count} 条 · 明细 {row.dwd_count} 条</span>
              {row.new_key_count ? <span className="flag">新键待定 {row.new_key_count} 条</span> : null}
            </button>
          ))}
        </aside>
        {detail ? (
          <section className="sheet">
            <div className="split-head">
              <h2>{detail.biz_date}</h2>
              <button disabled={!writer || busy} onClick={buildDay}>
                按扩列设置收成明细层
              </button>
            </div>
            <p className="note">IN 进闸，OUT 出闸，RS 倒箱。箱号要是四位字母加七位数字。</p>
            {detail.empty_key_count ? (
              <p className="note">
                有 {detail.empty_key_count} 个键名为空的原文键，按规矩不进新键队列，也不参与收成。
              </p>
            ) : null}
            {!writer ? <p className="note">核对员不能重新构建，也不能改扩列设置。</p> : null}
            {formError ? <p className="warn">{formError}</p> : null}
            {notice ? <p className="ok">{notice}</p> : null}

            <ExpandSettings
              settings={settings}
              writer={writer}
              busy={busy}
              onChoose={choosePolicy}
            />

            <KeyList keys={keys} conflicts={conflicts} deviations={deviations} />

            <div className="layers">
              <article>
                <h3>贴源层</h3>
                <ul>
                  {detail.ods.map((row) => (
                    <li key={row.id}>
                      <span>{row.terminal_code}</span>
                      <code>{JSON.stringify(row.raw_payload)}</code>
                      {(queueBySource[row.id] || []).length ? (
                        <div className="tags">
                          {queueBySource[row.id].map((item) => (
                            <span key={item.key_name} className={`tag ${OUTCOME_TONE[item.outcome] || "wait"}`}>
                              {item.key_name} · {outcomeLabel(item.outcome)}
                            </span>
                          ))}
                        </div>
                      ) : null}
                    </li>
                  ))}
                </ul>                <h3>新键队列</h3>
                <ul>
                  {queue.map((item) => (
                    <li key={item.id}>
                      <div className="rowline">
                        <span>{item.key_name}</span>
                        <span className="rowline">
                          <em className={`tag ${OUTCOME_TONE[item.outcome] || "wait"}`}>
                            {item.outcome_name}
                          </em>
                          {writer && settings?.policy === "expand"
                          && ["pending", "contention", "deviation"].includes(item.outcome) ? (
                            <button
                              className="mini"
                              disabled={busy}
                              onClick={() => expandOne(item.key_name)}
                            >
                              扩进这一键
                            </button>
                          ) : null}
                        </span>
                      </div>
                      <span className="soft">
                        来源 {item.ods_id} · 值 {fmt(item.value_text)}
                        {item.detail_column ? ` · 列 ${item.detail_column}` : ""}
                        {item.settle_note ? ` · ${item.settle_note}` : ""}
                      </span>
                    </li>
                  ))}
                  {!queue.length ? <li>这一天没有字典外的新键</li> : null}
                </ul>
              </article>

              <article>
                <h3>明细层</h3>
                {detail.moves.length ? (
                  <table>
                    <thead>
                      <tr><th>来源</th><th>箱号</th><th>事件</th><th>时间</th></tr>
                    </thead>
                    <tbody>
                      {detail.moves.map((row) => (
                        <tr key={`${row.source_id}-${row.event_kind}`}>
                          <td>{row.source_id}</td>
                          <td>{row.container_no}</td>
                          <td>{row.event_kind}</td>
                          <td>{row.event_at}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : <p>这一天的明细还是空的。</p>}

                <h3>字段详情 · 原文键 对 明细列</h3>
                <table>
                  <thead>
                    <tr><th>原文键</th><th>明细列</th><th>去向</th><th>出现</th></tr>
                  </thead>
                  <tbody>
                    {fields.map((field) => (
                      <tr key={`${field.key_name}-${field.empty_name}`}>
                        <td>{field.empty_name ? "（空键名）" : field.key_name}</td>
                        <td><code>{fmt(field.detail_column)}</code></td>
                        <td>
                          <span className={`tag ${fieldTone(field.status)}`}>{field.status_name}</span>
                          {field.in_dictionary ? null : (
                            <span className="soft">
                              {" "}丢掉 {field.dropped_count} · 扩进 {field.expanded_count}
                            </span>
                          )}
                        </td>
                        <td>{field.seen_count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>

                <h3>剔除</h3>
                <ul>
                  {detail.rejects.map((row) => (
                    <li key={row.source_id}><span>来源 {row.source_id}</span><em>{row.reason}</em></li>
                  ))}
                  {!detail.rejects.length ? <li>没有被剔除的扫描</li> : null}
                </ul>
              </article>
            </div>
          </section>
        ) : (
          <p className="note">选一个业务日。</p>
        )}
      </main>
    </>
  );
}

function outcomeLabel(outcome) {
  return {
    pending: "待定",
    dropped: "已丢掉",
    expanded: "已扩进",
    contention: "扩列争用",
    deviation: "办法偏了",
    rejected: "被剔除",
  }[outcome] || outcome;
}

function fieldTone(status) {
  return {
    expanded: "grow",
    dropped: "drop",
    deviation: "off",
    pending: "wait",
    known: "plain",
    empty: "muted",
  }[status] || "wait";
}

function ExpandSettings({ settings, writer, busy, onChoose }) {
  if (!settings) return null;
  return (
    <section className="policy">
      <div className="split-head">
        <h3>扩列办法</h3>
        <span className={`tag ${settings.status === "pinned" ? "grow" : "wait"}`}>
          {settings.status === "pinned" ? "已钉住" : "尚未钉住"}
        </span>
      </div>
      <p className="note">
        {settings.status === "pinned"
          ? `${settings.policy_name} · ${settings.pinned_by} 于 ${settings.pinned_at} 钉住；本日不得更换。`
          : "尚未选定。没有钉住时，带新键的扫描不准收成。"}
      </p>
      <div className="choices">
        {settings.choices.map((choice) => (
          <button
            key={choice.value}
            className={choice.selected ? "choice on" : "choice"}
            disabled={!writer || busy || (settings.status === "pinned" && !choice.selected)}
            onClick={() => onChoose(choice.value)}
          >
            <strong>{choice.label}</strong>
            <em>{POLICY_CHOICES.find((item) => item.value === choice.value)?.hint}</em>
          </button>
        ))}
      </div>
    </section>
  );
}

function KeyList({ keys, conflicts, deviations }) {
  return (
    <section className="keys">
      <h3>扩列列表</h3>
      <table>
        <thead>
          <tr><th>新键名</th><th>丢掉次数</th><th>扩进次数</th><th>办法名</th></tr>
        </thead>
        <tbody>
          {keys.map((row) => (
            <tr key={row.key_name}>
              <td><code>{row.key_name}</code></td>
              <td>{row.drop_count}</td>
              <td>{row.expand_count}</td>
              <td>{row.policy_name || "尚未钉住"}</td>
            </tr>
          ))}
          {!keys.length ? (
            <tr><td colSpan={4}>还没有字典外的新键。</td></tr>
          ) : null}
        </tbody>
      </table>
      {conflicts.length ? (
        <>
          <h3>扩列争用</h3>
          <ul>
            {conflicts.map((row, index) => (
              <li key={index}>
                <div className="rowline">
                  <span>{row.key_name}</span>
                  <em className="tag rival">争用</em>
                </div>
                <span className="soft">
                  {row.charged_by} · {row.charged_at} · 收下 {fmt(row.kept_value)}，漏掉 {fmt(row.lost_value)}
                  {row.detail_column ? ` · 列 ${row.detail_column}` : ""}
                </span>
              </li>
            ))}
          </ul>
        </>
      ) : null}
      {deviations.length ? (
        <>
          <h3>办法偏了</h3>
          <ul>
            {deviations.map((row) => (
              <li key={row.run_id}>
                <div className="rowline">
                  <span>{row.biz_date} · 第 {row.run_id} 次收成</span>
                  <em className="tag off">偏了</em>
                </div>
                <span className="soft">
                  带上 {row.policy_used}，扩列设置是 {row.policy_pinned}；新键不落库。{row.recorded_by} · {row.recorded_at}
                </span>
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </section>
  );
}
