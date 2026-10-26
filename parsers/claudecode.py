# -*- coding: utf-8 -*-
"""parsers/claudecode.py — Claude Code 转录 jsonl -> SessionBundle

源: ~/.claude/projects/<编码cwd>/<sid>.jsonl
行: type=user/assistant，message.content 块 text/thinking/tool_use/tool_result
    ai-title/custom-title 拿标题；cwd/sessionId/timestamp 在行级。
丢弃: queue-operation/attachment/file-history-*/atis-latch/last-prompt/cost-state/agent-name/system
"""
import json
import time as _time
from datetime import datetime
from pathlib import Path

from core import Ev, UsageRow, SessionBundle, write_session
import agentmem_config as _cfg

AGENT = "claude"
CLAUDE_ROOT = _cfg.root("claude")

NOISE_PREFIXES = ("<system-reminder>", "<user_info>", "<environment_context>",
                  "<command-name>", "<command-message>", "<local-command", "<task-notification>")


def iso_ms(s):
    if not s:
        return 0
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def _clean(t):
    t = (t or "").strip()
    for p in NOISE_PREFIXES:
        if t[:120].lstrip().startswith(p):
            return ""
    return t


def parse_file(path: Path, include_reasoning: bool):
    sid = path.stem
    b = SessionBundle(agent="claude", session_id=sid, source_key=str(path),
                      source_path=str(path), cursor_mtime=path.stat().st_mtime)
    models = []
    toolnames = {}  # tool_use_id -> name，供 tool_result 回填
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = o.get("type")
        ts = iso_ms(o.get("timestamp"))
        if t in ("queue-operation", "attachment", "file-history-snapshot",
                 "file-history-delta", "atis-latch", "last-prompt", "cost-state",
                 "agent-name", "system", "api-request-shape", "api-request-blob",
                 "api-request"):
            continue
        if not b.project:
            b.project = o.get("cwd") or ""
        if t == "sessionId" or (o.get("sessionId") and not b.session_id):
            pass
        if t in ("ai-title", "custom-title"):
            b.title = o.get("aiTitle") or o.get("title") or b.title
            continue
        m = o.get("message") or {}
        if t == "user":
            content = m.get("content")
            if isinstance(content, str):
                txt = _clean(content)
                if txt:
                    b.events.append(Ev(ts=ts, kind="user", text=txt))
                continue
            for c in content or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text":
                    txt = _clean(c.get("text") or "")
                    if txt:
                        b.events.append(Ev(ts=ts, kind="user", text=txt))
                elif c.get("type") == "tool_result":
                    out = c.get("content")
                    if isinstance(out, list):
                        parts = []
                        for x in out:
                            if isinstance(x, dict):
                                if x.get("type") == "text":
                                    parts.append(x.get("text") or "")
                                elif x.get("type") == "image":
                                    parts.append("[image]")
                        out = "\n".join(parts)
                    tid = c.get("tool_use_id") or ""
                    b.events.append(Ev(ts=ts, kind="tool_result",
                                       tool_name=toolnames.get(tid, ""),
                                       status="error" if c.get("is_error") else "ok",
                                       text=str(out or "")))
        elif t == "assistant":
            mdl = m.get("model")
            if mdl and mdl not in models:
                models.append(mdl)
            u = m.get("usage") or {}
            if u:
                cr = u.get("cache_read_input_tokens", 0) or 0
                cw = u.get("cache_creation_input_tokens", 0) or 0
                b.usage.append(UsageRow(ts=ts, model=mdl or "",
                                        in_tokens=u.get("input_tokens", 0) or 0,
                                        out_tokens=u.get("output_tokens", 0) or 0,
                                        cache_tokens=cr + cw,
                                        total_tokens=(u.get("input_tokens", 0) or 0) + (u.get("output_tokens", 0) or 0) + cr + cw))
            for c in m.get("content") or []:
                if not isinstance(c, dict):
                    continue
                ct = c.get("type")
                if ct == "text":
                    txt = _clean(c.get("text") or "")
                    if txt:
                        b.events.append(Ev(ts=ts, kind="assistant", text=txt))
                elif ct == "thinking":
                    if include_reasoning:
                        txt = _clean(c.get("thinking") or "")
                        if txt:
                            b.events.append(Ev(ts=ts, kind="reasoning", text=txt))
                elif ct == "tool_use":
                    inp = c.get("input")
                    tid = c.get("id") or ""
                    nm = c.get("name") or ""
                    if tid:
                        toolnames[tid] = nm
                    b.events.append(Ev(ts=ts, kind="tool_call", tool_name=nm,
                                       text=json.dumps(inp, ensure_ascii=False) if isinstance(inp, (dict, list)) else str(inp or "")))
        else:
            b._unknown = getattr(b, "_unknown", 0) + 1
    b._models = models
    if b.events:
        tstamps = [e.ts for e in b.events if e.ts]
        b.started_at, b.ended_at = min(tstamps), max(tstamps)
    return b


def collect(con, state, ctx, stats):
    if not CLAUDE_ROOT.is_dir():
        print(f"[claude] 源目录不存在: {CLAUDE_ROOT}")
        return
    for path in sorted(CLAUDE_ROOT.glob("*/*.jsonl")):
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
