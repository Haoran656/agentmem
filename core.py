# -*- coding: utf-8 -*-
"""core.py — L2 公共数据结构与写入逻辑（parser 与 pipeline 共享，避免循环 import）"""
import json
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field

from sanitize import sanitize

MAX_TEXT = 8000  # 事件正文截断


@dataclass
class Ev:
    ts: int = 0
    kind: str = "user"          # user|assistant|reasoning|tool_call|tool_result|system
    tool_name: str = ""
    text: str = ""
    status: str = ""


@dataclass
class UsageRow:
    ts: int = 0
    step: int = 0
    model: str = ""
    in_tokens: int = 0
    out_tokens: int = 0
    cache_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    cost_cny: float = 0.0


@dataclass
class SessionBundle:
    agent: str
    session_id: str
    source_key: str              # sync_state 游标键（文件路径或 db:sid）
    source_path: str = ""
    project: str = ""
    title: str = ""
    started_at: int = 0
    ended_at: int = 0
    cursor_ts: int = 0           # db 类源游标（会话内最大事件时间）
    cursor_mtime: float = 0.0    # 文件类源游标
    events: list = field(default_factory=list)
    usage: list = field(default_factory=list)
    cost_usd: float = 0.0
    cost_cny: float = 0.0
    credit: float = 0.0


FRESH_GUARD_S = 60  # 正在被写的转录：mtime 太新先不读（半行 JSON + 当前对话污染）


def trunc(t):
    t = (t or "").strip()
    return t[:MAX_TEXT] + "\n…[截断]" if len(t) > MAX_TEXT else t


def skip_fresh(path, ctx):
    """mtime 在 60 秒内的源文件本轮跳过。backfill 不受限。"""
    if ctx.get("backfill"):
        return False
    try:
        return (time.time() - path.stat().st_mtime) < FRESH_GUARD_S
    except OSError:
        return True


def write_session(con: sqlite3.Connection, b: SessionBundle, state: dict, stats: dict) -> bool:
    """会话级幂等写入：默认只追加新事件；只有旧事件整体消失（改稿/回滚/脱敏规则变化）
    才整会话重放。usage 仍整体重写（行数少），sessions 元数据按全量解析结果 upsert。"""
    prev = state.get(b.source_key)
    cur = b.cursor_mtime or b.cursor_ts
    if prev is not None and cur and prev == cur:
        stats["skipped"] += 1
        return False

    if not b.events:
        if cur:
            state[b.source_key] = cur
        stats["empty"] += 1
        return False

    now = int(time.time() * 1000)
    con.execute("DELETE FROM usage WHERE agent=? AND session_id=?", (b.agent, b.session_id))

    models, tin, tout, tcache = {}, 0, 0, 0
    rows_ev = []
    for e in b.events:
        text, hits = sanitize(e.text)
        tname, h1 = sanitize(e.tool_name or "")
        stat, h2 = sanitize(e.status or "")
        rows_ev.append((b.agent, b.session_id, e.ts, e.kind, tname or None,
                        trunc(text), stat or None, 1 if (hits or h1 or h2) else 0))
        hits = hits + h1 + h2
        if hits:
            stats["redacted_hits"] += hits
    # 增量写入（WorkBuddy 审计建议）：append-only 转录不再"整会话 DELETE + 重插"，
    # 否则每次 sync 都会重建整段 FTS，正是 183MB 库与数秒审计的放大器。
    old = Counter()
    for r in con.execute(
            "SELECT ts, kind, tool_name, text, status FROM events "
            "WHERE agent=? AND session_id=?", (b.agent, b.session_id)):
        old[(r["ts"] or 0, r["kind"] or "", r["tool_name"] or "",
             r["text"] or "", r["status"] or "")] += 1
    new = Counter((row[2], row[3] or "", row[4] or "", row[5] or "", row[6] or "")
                  for row in rows_ev)
    stale = old - new
    if stale:
        # 有旧事件在新解析结果里消失：唯一安全做法是整段重放（并让新行重新过审计水位）
        con.execute("DELETE FROM events WHERE agent=? AND session_id=?", (b.agent, b.session_id))
        to_insert = rows_ev
    else:
        need = new - old
        to_insert = []
        for row in rows_ev:
            fp = (row[2], row[3] or "", row[4] or "", row[5] or "", row[6] or "")
            if need[fp] > 0:
                to_insert.append(row)
                need[fp] -= 1
    if to_insert:
        con.executemany(
            "INSERT INTO events(agent,session_id,ts,kind,tool_name,text,status,redacted) "
            "VALUES(?,?,?,?,?,?,?,?)", to_insert)

    for u in b.usage:
        tot = u.total_tokens or (u.in_tokens + u.out_tokens + u.cache_tokens)
        con.execute(
            "INSERT INTO usage(agent,session_id,ts,step,model,in_tokens,out_tokens,cache_tokens,total_tokens,cost_usd,cost_cny) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (b.agent, b.session_id, u.ts, u.step, u.model, u.in_tokens, u.out_tokens,
             u.cache_tokens, tot, u.cost_usd, u.cost_cny))
        tin += u.in_tokens; tout += u.out_tokens; tcache += u.cache_tokens
        if u.model:
            models[u.model.lower()] = True
    for m in getattr(b, "_models", []):
        models.setdefault(m.lower(), True)

    proj_name = (b.project or "").replace("\\", "/").rstrip("/").split("/")[-1]
    # 元数据同正文一样过脱敏（title/project/source_path 可能内嵌 token）
    s_title, m1 = sanitize(b.title or "")
    s_project, m2 = sanitize(b.project or "")
    s_projname, m3 = sanitize(proj_name)
    s_srcline, m4 = sanitize(b.source_path or "")
    if m1 + m2 + m3 + m4:
        stats["redacted_hits"] += m1 + m2 + m3 + m4
    con.execute(
        "INSERT INTO sessions(agent,session_id,project,project_name,title,started_at,ended_at,models,"
        "n_events,in_tokens,out_tokens,cache_tokens,cost_usd,cost_cny,credit,source_path,synced_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(agent,session_id) DO UPDATE SET "
        "project=excluded.project,project_name=excluded.project_name,title=excluded.title,"
        "started_at=excluded.started_at,ended_at=excluded.ended_at,models=excluded.models,"
        "n_events=excluded.n_events,in_tokens=excluded.in_tokens,out_tokens=excluded.out_tokens,"
        "cache_tokens=excluded.cache_tokens,cost_usd=excluded.cost_usd,cost_cny=excluded.cost_cny,"
        "credit=excluded.credit,source_path=excluded.source_path,synced_at=excluded.synced_at",
        (b.agent, b.session_id, s_project, s_projname, s_title, b.started_at, b.ended_at,
         json.dumps(sorted(models)), len(rows_ev), tin, tout, tcache,
         b.cost_usd, b.cost_cny, b.credit, s_srcline, now))

    state[b.source_key] = cur or now
    stats["sessions"] += 1
    stats["events"] += len(to_insert)
    return True
