# -*- coding: utf-8 -*-
"""parsers/kilo.py — Kilo (kilo.db) -> SessionBundle

表: session(id,title,directory,model JSON,cost,*) / message(id,session_id,time_created,data{role,model,time})
    part(id,message_id,session_id,data{type:text|reasoning|tool|file|step-finish,...})
游标: 会话内最大 message.time_created（存 sync_state.last_ts）
模型名: message.data.model 或 session.model JSON 的 id
step-finish: tokens{input,output,reasoning,cache{read,write}} + cost(USD)
"""
import json
import sqlite3
from pathlib import Path

from core import Ev, UsageRow, SessionBundle, write_session
import agentmem_config as _cfg

AGENT = "kilo"
KILO_DB = _cfg.root("kilo")

NOISE_PREFIXES = ("<system-reminder>", "<environment_context>", "<user_instructions>",
                  "<app-context>", "<permissions instructions>")


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
    if not KILO_DB.exists():
        print(f"[kilo] 库不存在: {KILO_DB}")
        return
    try:
        src = sqlite3.connect(f"file:{KILO_DB}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error as e:
        print(f"[kilo] 打开失败: {e}")
        return
    src.row_factory = sqlite3.Row
    try:
        sessions = src.execute(
            "SELECT id,title,directory,model,time_created FROM session ORDER BY time_created"
        ).fetchall()
        for s in sessions:
            sid = s["id"]
            key = f"kilo:{sid}"
            msgs = src.execute(
                "SELECT id,time_created,data FROM message WHERE session_id=? ORDER BY time_created",
                (sid,)).fetchall()
            if not msgs:
                continue
            last_ts = max(r["time_created"] or 0 for r in msgs)
            if state.get(key) == last_ts:
                stats["skipped"] += 1
                continue

            parts_by_msg = {}
            for p in src.execute("SELECT message_id,data FROM part WHERE session_id=?", (sid,)).fetchall():
                parts_by_msg.setdefault(p["message_id"], []).append(_loads(p["data"]))

            b = SessionBundle(agent="kilo", session_id=sid, source_key=key,
                              source_path=str(KILO_DB), cursor_ts=last_ts,
                              project=s["directory"] or "", title=s["title"] or "")
            models = []
            m0 = _model_id(s["model"])
            if m0:
                models.append(m0)
            for r in msgs:
                md = _loads(r["data"])
                role = md.get("role")
                ts = r["time_created"] or 0
                mm = _model_id(md.get("model"))
                if mm and mm not in models:
                    models.append(mm)
                if role not in ("user", "assistant"):
                    continue
                for d in parts_by_msg.get(r["id"], []):
                    pt = d.get("type")
                    if pt == "text":
                        txt = _clean(d.get("text") or "")
                        if txt:
                            b.events.append(Ev(ts=ts, kind=role, text=txt))
                    elif pt == "reasoning":
                        if ctx.get("include_reasoning"):
                            txt = _clean(d.get("text") or "")
                            if txt:
                                b.events.append(Ev(ts=ts, kind="reasoning", text=txt))
                    elif pt == "tool":
                        st = d.get("state") or {}
                        inp = st.get("input")
                        b.events.append(Ev(ts=ts, kind="tool_call", tool_name=d.get("tool") or "",
                                           status=str(st.get("status") or ""),
                                           text=json.dumps(inp, ensure_ascii=False) if isinstance(inp, (dict, list)) else str(inp or "")))
                        out = st.get("output")
                        if isinstance(out, str) and out.startswith("data:"):
                            out = "[data-url]"
                        elif isinstance(out, (dict, list)):
                            out = json.dumps(out, ensure_ascii=False)
                        if out:
                            b.events.append(Ev(ts=ts, kind="tool_result", tool_name=d.get("tool") or "",
                                               status=str(st.get("status") or ""), text=str(out)))
                    elif pt == "step-finish":
                        tk = d.get("tokens") or {}
                        cache = tk.get("cache") or {}
                        b.usage.append(UsageRow(ts=ts, model=mm or "",
                                                in_tokens=tk.get("input", 0) or 0,
                                                out_tokens=(tk.get("output", 0) or 0) + (tk.get("reasoning", 0) or 0),
                                                cache_tokens=(cache.get("read", 0) or 0) + (cache.get("write", 0) or 0),
                                                total_tokens=tk.get("total") or 0,
                                                cost_usd=float(d.get("cost") or 0)))
                    elif pt == "file":
                        b.events.append(Ev(ts=ts, kind=role, status="file",
                                           text=f"[文件: {d.get('filename') or ''}]"
                                           if not str(d.get("url", "")).startswith("data:")
                                           else "[图片附件]"))
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
