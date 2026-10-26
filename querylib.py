# -*- coding: utf-8 -*-
"""querylib.py — L3 检索核心（MCP server + query.py CLI 共享，只读）

中文/英文检索统一走 trigram FTS5（>=3 字符子串，含 snippet 高亮）；
<3 字符短词回退 LIKE。默认降噪：不命中 tool_result（除显式要）。
"""
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import agentmem_config as _cfg

DB_PATH = Path(__file__).resolve().parent / "agentmem.db"


def connect_ro() -> sqlite3.Connection:
    # 红线：检索面物理禁写。写面（insight_add）不走这里，走 agentmem_db.init()。
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA busy_timeout=2000")
    return con


def _ms_of(dstr):
    return int(datetime.strptime(dstr, "%Y-%m-%d").timestamp() * 1000)


def _fmt(ms):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000)) if ms else "?"


def _fts_escape(term: str) -> str:
    # trigram 短语查询用双引号包裹；内部双引号成对转义
    return '"' + term.replace('"', '""') + '"'


def build_search(con, query, agent=None, project=None, kind=None,
                 since=None, until=None, limit=20, include_tool_result=False):
    """返回命中行列表（共享给 MCP 与 CLI）。limit 已钳位。"""
    limit = max(1, min(int(limit or 20), 100))
    query = (query or "").strip()
    if not query:
        return []
    use_trigram = len(query) >= 3            # trigram 只索引 >=3 字符子串

    # 非全文条件（agent/project/kind/降噪/时间）统一构造
    filt, fparams = [], []
    if agent:
        filt.append("e.agent=?"); fparams.append(agent)
    if project:
        al = project_aliases(con, project)
        filt.append(f"s.project_name IN ({','.join('?'*len(al))})"); fparams.extend(al)
    if kind:
        filt.append("e.kind=?"); fparams.append(kind)
    if not kind and not include_tool_result:
        filt.append("e.kind != 'tool_result'")   # 降噪默认
    if since:
        filt.append("e.ts>=?"); fparams.append(_ms_of(since))
    if until:
        filt.append("e.ts<?"); fparams.append(_ms_of(until) + 86400000)
    filt_sql = (" AND " + " AND ".join(filt)) if filt else ""

    if use_trigram:
        cols = ("e.agent,e.session_id,e.ts,e.kind,e.tool_name,e.status,e.redacted,"
                "snippet(events_fts,0,'\u3010','\u3011','…',18) AS txt, s.project_name, s.title")
        sql = f"""SELECT {cols}
            FROM events_fts
            JOIN events e ON e.id = events_fts.rowid
            JOIN sessions s ON s.agent=e.agent AND s.session_id=e.session_id
            WHERE events_fts MATCH ?{filt_sql} ORDER BY e.ts DESC LIMIT ?"""
        return con.execute(sql, [_fts_escape(query)] + fparams + [limit * 5]).fetchall()
    cols = ("e.agent,e.session_id,e.ts,e.kind,e.tool_name,e.status,e.redacted,"
            "substr(e.text,1,160) AS txt, s.project_name, s.title")
    sql = f"""SELECT {cols}
        FROM events e JOIN sessions s ON s.agent=e.agent AND s.session_id=e.session_id
        WHERE e.text LIKE ?{filt_sql} ORDER BY e.ts DESC LIMIT ?"""
    return con.execute(sql, [f"%{query}%"] + fparams + [limit * 5]).fetchall()


def group_by_session(rows, max_sessions):
    """按会话聚合（渐进披露第一层）：保留每会话命中数、时间范围、首条示例。"""
    groups = {}
    order = []
    for r in rows:
        k = (r["agent"], r["session_id"])
        if k not in groups:
            groups[k] = {"rows": [], "head": r}
            order.append(k)
        groups[k]["rows"].append(r)
    out = []
    for k in order[:max_sessions]:
        g = groups[k]
        rs = g["rows"]
        out.append({"agent": k[0], "session_id": k[1], "n": len(rs),
                    "first_ts": min(r["ts"] for r in rs if r["ts"]) if any(r["ts"] for r in rs) else 0,
                    "last_ts": max(r["ts"] for r in rs),
                    "project": g["head"]["project_name"], "title": g["head"]["title"],
                    "sample": max(rs, key=lambda r: len(r["txt"] or ""))["txt"]})
    return out


def project_aliases(con, name):
    """逻辑项目名 -> 各端 alias 集合（含自身，未登记时按原名查）。"""
    if not name:
        return None
    rows = con.execute("SELECT alias FROM projects WHERE logical=?", (name,)).fetchall()
    aliases = {r["alias"] for r in rows}
    aliases.add(name)
    return aliases


def search_events(query, agent=None, project=None, kind=None,
                  since=None, until=None, limit=20, include_tool_result=False,
                  detail=None, include_insights=True):
    """全文检索，渐进披露。detail: index(仅会话卡片) / context(每会话≤5 snippet) / full(全部行)。
    默认：命中会话 >1 走 index（省 token），单会话自动 context。
    include_insights=True 时先单独返回命中的沉淀洞察（结论层），再给原始会话。"""
    con = connect_ro()
    try:
        rows = build_search(con, query, agent, project, kind, since, until, limit, include_tool_result)
        insights = []
        if include_insights:
            use_tri = len(query) >= 3
            w, p = [], []
            if use_tri:
                w.append("text LIKE ?"); p.append(f"%{query}%")   # insights 量小，LIKE 足够
            else:
                w.append("text LIKE ?"); p.append(f"%{query}%")
            if project:
                al = project_aliases(con, project)
                w.append(f"project IN ({','.join('?'*len(al))})"); p.extend(al)
            insights = con.execute(
                f"SELECT * FROM insights WHERE {' AND '.join(w)} ORDER BY ts DESC LIMIT 10", p).fetchall()
    finally:
        con.close()
    if not rows and not insights:
        return f"未命中：{query!r}"
    head = []
    if insights:
        head.append(f"◆ 沉淀洞察 {len(insights)} 条（结论层，优先采信）：")
        for r in insights:
            src = f"[{r['agent']}/{(r['session_id'] or '')[:8]}]" if r["session_id"] else ""
            head.append(f"- [{r['type']}] {r['project'] or '-'} | {r['text'][:180]} {src}")
        head.append("")
    if not rows:
        return "\n".join(head)
    sessions = group_by_session(rows, 100)
    if detail not in ("index", "context", "full"):
        detail = "context" if len(sessions) == 1 else "index"
    note = "（默认已排除工具返回噪声，需含 tool_result 请设 include_tool_result）" \
        if not kind and not include_tool_result else ""
    total_n = sum(s["n"] for s in sessions)
    out = head + [f"检索 {query!r}：{total_n} 条命中 / {len(sessions)} 个会话 | detail={detail}"
                  f"（index 档只看卡片，展开用 detail=context/full 或 get_session）{note}"]
    for s in sessions[:max(1, min(limit, 30))]:
        out.append(f"\n### [{s['agent']}] {s['project']} | {s['title'] or '(无标题)'} | "
                   f"{_fmt(s['first_ts'])}~{_fmt(s['last_ts'])} | 命中{s['n']}条")
        out.append(f"session_id: `{s['session_id']}`")
        if detail == "context":
            for r in [x for x in rows if x["session_id"] == s["session_id"] and x["agent"] == s["agent"]][:5]:
                tag = r["kind"] + (f":{r['tool_name']}" if r["tool_name"] else "")
                red = " [已脱敏]" if r["redacted"] else ""
                txt = (r["txt"] or "").replace("\n", " ")[:180]
                out.append(f"- {_fmt(r['ts'])} `{tag}`{red} {txt}")
        elif detail == "full":
            for r in [x for x in rows if x["session_id"] == s["session_id"] and x["agent"] == s["agent"]][:15]:
                tag = r["kind"] + (f":{r['tool_name']}" if r["tool_name"] else "")
                red = " [已脱敏]" if r["redacted"] else ""
                txt = (r["txt"] or "").replace("\n", " ")[:220]
                out.append(f"- {_fmt(r['ts'])} `{tag}`{red} {txt}")
    return "\n".join(out)


def timeline(agent, session_id, window=8):
    """某会话的时间邻域：前后各 window 个会话（跨端），用于'当时还在做什么'。"""
    con = connect_ro()
    try:
        cur = con.execute("SELECT started_at FROM sessions WHERE agent=? AND session_id=?",
                          (agent, session_id)).fetchone()
        if not cur:
            return f"会话不存在: {agent}/{session_id}"
        ts = cur["started_at"] or 0
        before = con.execute(
            "SELECT agent,session_id,project_name,title,started_at,n_events FROM sessions "
            "WHERE started_at<? ORDER BY started_at DESC LIMIT ?", (ts, window)).fetchall()
        after = con.execute(
            "SELECT agent,session_id,project_name,title,started_at,n_events FROM sessions "
            "WHERE started_at>=? ORDER BY started_at ASC LIMIT ?", (ts + 1, window)).fetchall()
        out = [f"时间线（锚点 [{agent}] {session_id}，{_fmt(ts)}）："]
        for r in reversed(after):
            out.append(f"  {_fmt(r['started_at'])} [{r['agent']}] {r['project_name']} | {(r['title'] or '')[:40]}")
        anchor = con.execute("SELECT project_name,title,n_events FROM sessions WHERE agent=? AND session_id=?",
                             (agent, session_id)).fetchone()
        out.append(f">> {_fmt(ts)} [{agent}] {anchor['project_name']} | {anchor['title'] or ''} <<")
        for r in before:
            out.append(f"  {_fmt(r['started_at'])} [{r['agent']}] {r['project_name']} | {(r['title'] or '')[:40]}")
        return "\n".join(out)
    finally:
        con.close()


def get_session(agent, session_id, role=None, offset=0, page=50):
    """单会话元信息 + 分页事件流。page≤200。"""
    page = max(1, min(int(page or 50), 200))
    offset = max(0, int(offset or 0))
    con = connect_ro()
    try:
        s = con.execute("SELECT * FROM sessions WHERE agent=? AND session_id=?", (agent, session_id)).fetchone()
        if not s:
            return f"会话不存在: {agent}/{session_id}"
        where, params = ["e.agent=?", "e.session_id=?"], [agent, session_id]
        if role == "user":
            where.append("e.kind='user'")
        elif role == "assistant":
            where.append("e.kind='assistant'")
        elif role == "tool":
            where.append("e.kind IN ('tool_call','tool_result')")
        rows = con.execute(f"SELECT e.* FROM events e WHERE {' AND '.join(where)} ORDER BY e.ts LIMIT ? OFFSET ?",
                           params + [page, offset]).fetchall()
        out = [f"## 会话 {agent}/{session_id}",
               f"- 项目: {s['project']}",
               f"- 标题: {s['title'] or '(无标题)'}",
               f"- 模型: {s['models']}"]
        if role:
            out.append(f"- 角色过滤: {role}")
        out.append(f"- 时间: {_fmt(s['started_at'])} ~ {_fmt(s['ended_at'])}，共 {s['n_events']} 事件，本页 {len(rows)} 条（offset={offset}）")
        out.append("")
        for r in rows:
            tag = r["kind"] + (f":{r['tool_name']}" if r["tool_name"] else "") + (f"({r['status']})" if r["status"] else "")
            red = " [已脱敏]" if r["redacted"] else ""
            out.append(f"### {_fmt(r['ts'])} `{tag}`{red}")
            out.append((r["text"] or "")[:400])
            out.append("")
        return "\n".join(out)
    finally:
        con.close()


def find_sessions(agent=None, project=None, since=None, until=None,
                  model=None, title_kw=None, limit=30):
    """按维度过滤列会话卡片。"""
    limit = max(1, min(int(limit or 30), 100))
    where, params = ["1=1"], []
    if agent:
        where.append("agent=?"); params.append(agent)
    if project:
        con0 = connect_ro()
        try:
            al = project_aliases(con0, project)
        finally:
            con0.close()
        where.append(f"project_name IN ({','.join('?'*len(al))})"); params.extend(al)
    if since:
        where.append("ended_at>=?"); params.append(_ms_of(since))
    if until:
        where.append("started_at<?"); params.append(_ms_of(until) + 86400000)
    if model:
        where.append("models LIKE ?"); params.append(f"%{model.lower()}%")
    if title_kw:
        where.append("title LIKE ?"); params.append(f"%{title_kw}%")
    params.append(limit)
    con = connect_ro()
    try:
        rows = con.execute(f"""SELECT * FROM sessions WHERE {' AND '.join(where)}
            ORDER BY ended_at DESC LIMIT ?""", params).fetchall()
    finally:
        con.close()
    if not rows:
        return "无匹配会话。"
    out = [f"匹配 {len(rows)} 个会话（最近活动排序）："]
    for r in rows:
        out.append(f"- [{r['agent']}] `{r['session_id']}` | {r['project_name']} | {r['title'] or '(无标题)'} | "
                   f"{_fmt(r['ended_at'])} | {r['n_events']}事件 | tokens {r['in_tokens']+r['out_tokens']:,} | {r['models']}")
    return "\n".join(out)


def activity_stats(days=30, agent=None):
    days = max(1, min(int(days or 30), 400))
    frm = int((time.time() - days * 86400) * 1000)
    con = connect_ro()
    try:
        where, params = "ts>=?", [frm]
        if agent:
            where += " AND agent=?"; params.append(agent)
        byagent = con.execute(f"SELECT agent,count(DISTINCT session_id) sess,count(*) ev FROM events WHERE {where} GROUP BY agent ORDER BY ev DESC", params).fetchall()
        byday = con.execute(f"SELECT date(ts/1000,'unixepoch','{_cfg.tz()}') d,count(*) ev FROM events WHERE {where} GROUP BY d ORDER BY d DESC LIMIT 10", params).fetchall()
        tools = con.execute(f"SELECT agent,tool_name,count(*) c FROM events WHERE {where} AND kind='tool_call' AND tool_name IS NOT NULL GROUP BY 1,2 ORDER BY c DESC LIMIT 8", params).fetchall()
    finally:
        con.close()
    out = [f"近 {days} 天活动（{('仅 '+agent) if agent else '全部端'}）：", "按端："]
    out += [f"- {r['agent']}: {r['sess']} 会话 / {r['ev']:,} 事件" for r in byagent]
    out.append("按天（近10）：")
    out += [f"- {r['d']}: {r['ev']:,}" for r in byday]
    out.append("工具 TOP8：")
    out += [f"- {r['agent']}:{r['tool_name']} ×{r['c']:,}" for r in tools]
    return "\n".join(out)
