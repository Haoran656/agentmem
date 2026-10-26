# -*- coding: utf-8 -*-
"""parsers/opencode.py — OpenCode (opencode.db) -> SessionBundle

表: session_v2(id,title,directory,model JSON) / session_message(id,session_id,type,seq,time_created,data)
    assistant data: {time,agent,model{id},content[text|tool|...],cost,tokens{input,output,reasoning,cache{read,write}}}
游标: 会话内最大 message.time_created（sync_state.last_ts）
"""
import json
import sqlite3
from pathlib import Path

from core import Ev, UsageRow, SessionBundle, write_session
import agentmem_config as _cfg

AGENT = "opencode"
OC_DB = _cfg.root("opencode")

NOISE_PREFIXES = ("<system-reminder>", "<environment_context>", "<user_instructions>",
                  "<app-context>")


def _clean(t):
    t = (t or "").strip()
    for p in NOISE_PREFIXES:
        if t[:120].lstrip().startswith(p):
            return ""
    return t


def _loads(s):
    try:
        return json.loads(s) if s else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _model_id(v):
    if isinstance(v, dict):
        return v.get("id") or ""
    if isinstance(v, str) and v.startswith("{"):
        return _loads(v).get("id") or ""
    return v or ""


def collect(con, state, ctx, stats):
    if not OC_DB.exists():
        print(f"[opencode] 库不存在: {OC_DB}")
        return
    try:
        src = sqlite3.connect(f"file:{OC_DB}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error as e:
        print(f"[opencode] 打开失败: {e}")
        return
    src.row_factory = sqlite3.Row
    try:
        try:
            sessions = src.execute(
                "SELECT id,title,directory,model,time_created FROM session_v2 ORDER BY time_created"
            ).fetchall()
        except sqlite3.Error:
            print("[opencode] 无 session_v2 表，跳过")
            return
        for s in sessions:
            sid = s["id"]
            key = f"opencode:{sid}"
            msgs = src.execute(
                "SELECT id,type,seq,time_created,data FROM session_message WHERE session_id=? ORDER BY seq",
                (sid,)).fetchall()
            if not msgs:
                continue
            last_ts = max(r["time_created"] or 0 for r in msgs)
            if state.get(key) == last_ts:
                stats["skipped"] += 1
                continue

            b = SessionBundle(agent="opencode", session_id=sid, source_key=key,
                              source_path=str(OC_DB), cursor_ts=last_ts,
                              project=s["directory"] or "", title=s["title"] or "")
            models = []
            m0 = _model_id(s["model"])
            if m0:
                models.append(m0)
            for r in msgs:
                d = _loads(r["data"])
                ts = r["time_created"] or 0
                typ = r["type"]
                if typ == "user":
                    txt = _clean(d.get("text") or "")
                    if not txt:
                        parts = d.get("content") or []
                        if isinstance(parts, str):
                            txt = _clean(parts)
                        else:
                            txt = _clean("\n".join(c.get("text", "") for c in parts
                                                   if isinstance(c, dict) and c.get("type") == "text"))
                    if txt:
                        b.events.append(Ev(ts=ts, kind="user", text=txt))
                elif typ == "assistant":
                    mm = _model_id(d.get("model"))
                    if mm and mm not in models:
                        models.append(mm)
                    tk = d.get("tokens") or {}
                    cache = tk.get("cache") or {}
                    cost = float(d.get("cost") or 0)
                    if tk or cost:
                        b.usage.append(UsageRow(ts=ts, model=mm,
                                                in_tokens=tk.get("input", 0) or 0,
                                                out_tokens=(tk.get("output", 0) or 0) + (tk.get("reasoning", 0) or 0),
                                                cache_tokens=(cache.get("read", 0) or 0) + (cache.get("write", 0) or 0),
                                                total_tokens=(tk.get("input", 0) or 0) + (tk.get("output", 0) or 0) + (tk.get("reasoning", 0) or 0) + (cache.get("read", 0) or 0) + (cache.get("write", 0) or 0),
                                                cost_usd=cost))
                    for c in d.get("content") or []:
                        if not isinstance(c, dict):
                            continue
                        ct = c.get("type")
                        if ct == "text":
                            txt = _clean(c.get("text") or "")
                            if txt:
                                b.events.append(Ev(ts=ts, kind="assistant", text=txt))
                        elif ct in ("tool", "tool-call", "toolCall"):
                            st = c.get("state") or {}
                            nm = c.get("tool") or c.get("name") or ""
                            inp = st.get("input") or c.get("input")
                            b.events.append(Ev(ts=ts, kind="tool_call", tool_name=nm,
                                               status=str(st.get("status") or ""),
                                               text=json.dumps(inp, ensure_ascii=False) if isinstance(inp, (dict, list)) else str(inp or "")))
                            out = st.get("output") or c.get("output")
                            if isinstance(out, (dict, list)):
                                out = json.dumps(out, ensure_ascii=False)
                            if out:
                                b.events.append(Ev(ts=ts, kind="tool_result", tool_name=nm,
                                                   status=str(st.get("status") or ""), text=str(out)[:20000]))
                        elif ct == "reasoning" and ctx.get("include_reasoning"):
                            txt = _clean(c.get("text") or c.get("thinking") or "")
                            if txt:
                                b.events.append(Ev(ts=ts, kind="reasoning", text=txt))
            b._models = models
            b.cost_usd = sum(u.cost_usd for u in b.usage)
            if b.events:
                tstamps = [e.ts for e in b.events if e.ts]
                b.started_at, b.ended_at = min(tstamps), max(tstamps)
                write_session(con, b, state, stats)
            else:
                state[key] = last_ts
                stats["empty"] += 1
    finally:
        src.close()
