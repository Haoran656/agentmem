# -*- coding: utf-8 -*-
"""parsers/codex.py — Codex rollout jsonl -> SessionBundle

源: ~/.codex/sessions/<Y>/<M>/<D>/rollout-*<sid>.jsonl
事件: session_meta(cwd) / response_item(message|custom_tool_call|custom_tool_call_output|reasoning)
      / token_usage_record(usage) / event_msg(thread_settings_applied 拿模型名)
时间戳: ISO8601 Z 字符串 -> epoch ms
"""
import json
import time as _time
from datetime import datetime
from pathlib import Path

from core import Ev, UsageRow, SessionBundle, write_session
import agentmem_config as _cfg

AGENT = "codex"
CODEX_ROOT = _cfg.root("codex")

NOISE_PREFIXES = ("<app-context>", "<environment_context>", "<user_instructions>",
                  "<system-reminder>", "<permissions instructions>")


def iso_ms(s):
    if not s:
        return 0
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def _clean(t):
    t = (t or "").strip()
    for p in NOISE_PREFIXES:
        if t[:120].lstrip().startswith(p):
            return ""
    return t


def parse_file(path: Path, include_reasoning: bool):
    sid = path.stem.split("-")[-1]
    b = SessionBundle(agent="codex", session_id=sid, source_key=str(path),
                      source_path=str(path), cursor_mtime=path.stat().st_mtime)
    models = []
    toolnames = {}  # call_id -> name
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = o.get("type")
        ts = iso_ms(o.get("timestamp"))
        p = o.get("payload") or {}
        if t == "session_meta":
            b.project = p.get("cwd") or ""
            if p.get("id"):
                b.session_id = p["id"]
            b.started_at = max(b.started_at, iso_ms(p.get("timestamp")))
        elif t == "event_msg" and p.get("type") == "thread_settings_applied":
            m = (p.get("thread_settings") or {}).get("model")
            if m and m not in models:
                models.append(m)
        elif t == "response_item":
            pt = p.get("type")
            if pt == "message":
                role = p.get("role")
                if role not in ("user", "assistant"):
                    continue
                parts = [_clean(c.get("text") or c.get("input_text") or c.get("output_text") or "")
                         for c in p.get("content") or [] if isinstance(c, dict)]
                text = "\n\n".join(x for x in parts if x)
                if text:
                    b.events.append(Ev(ts=ts, kind=role, text=text))
            elif pt == "reasoning":
                if include_reasoning:
                    txt = _clean("\n".join(s for s in p.get("summary") or [] if isinstance(s, str)))
                    if txt:
                        b.events.append(Ev(ts=ts, kind="reasoning", text=txt))
            elif pt in ("custom_tool_call", "function_call"):
                inp = p.get("input") or p.get("arguments") or ""
                if isinstance(inp, (dict, list)):
                    inp = json.dumps(inp, ensure_ascii=False)
                nm = p.get("name") or ""
                cid = p.get("call_id") or ""
                if cid:
                    toolnames[cid] = nm
                b.events.append(Ev(ts=ts, kind="tool_call", tool_name=nm,
                                   status=str(p.get("status") or ""), text=str(inp)))
            elif pt in ("custom_tool_call_output", "function_call_output"):
                out = p.get("output")
                if isinstance(out, (dict, list)):
                    out = json.dumps(out, ensure_ascii=False)
                b.events.append(Ev(ts=ts, kind="tool_result",
                                   tool_name=toolnames.get(p.get("call_id") or "", ""),
                                   text=str(out or "")))
        elif t == "token_usage_record":
            u = p.get("usage") or {}
            if u:
                b.usage.append(UsageRow(ts=ts, model="",
                                        in_tokens=u.get("input_tokens", 0) or 0,
                                        out_tokens=u.get("output_tokens", 0) or 0,
                                        cache_tokens=0,  # cached_input 是 input 的子集，不加总
                                        total_tokens=u.get("total_tokens") or ((u.get("input_tokens", 0) or 0) + (u.get("output_tokens", 0) or 0))))
        elif t in ("event_msg", "world_state", "turn_context"):
            pass  # 基础设施事件，静默丢弃
        else:
            b._unknown = getattr(b, "_unknown", 0) + 1
    b._models = models
    if b.events:
        tstamps = [e.ts for e in b.events if e.ts]
        b.started_at = min([b.started_at] + tstamps) if b.started_at else min(tstamps)
        b.ended_at = max(tstamps)
    return b


def collect(con, state, ctx, stats):
    if not CODEX_ROOT.is_dir():
        print(f"[codex] 源目录不存在: {CODEX_ROOT}")
        return
    for path in sorted(CODEX_ROOT.rglob("*.jsonl")):
        if not ctx.get("backfill"):
            mdate = _time.strftime("%Y-%m-%d", _time.localtime(path.stat().st_mtime))
            if mdate != ctx.get("date_tag"):
                continue
        if state.get(str(path)) == path.stat().st_mtime:
            stats["skipped"] += 1
            continue
        try:
            b = parse_file(path, ctx.get("include_reasoning", False))
        except OSError:
            continue
        stats["unknown_events"] += getattr(b, "_unknown", 0)
        write_session(con, b, state, stats)
