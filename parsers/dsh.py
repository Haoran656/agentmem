# -*- coding: utf-8 -*-
"""parsers/dsh.py — DeepSeek Harness session.v4.jsonl.zstd -> SessionBundle

源: ~/.dsh/sessions/<mangled-cwd>/session-*/session.v4.jsonl.zstd
行: type + seq + time(ms) + data。
  session(cwd) / session/title / user/message(content[].text)
  assistant/message(message.content=text|tool-call, usage, model?) / tool/call / tool/result
"""
import io
import json
import time as _time
from pathlib import Path

from core import Ev, UsageRow, SessionBundle, write_session
import agentmem_config as _cfg

try:
    import zstandard as _zstd
    HAVE_ZSTD = True
except ImportError:
    HAVE_ZSTD = False

AGENT = "dsh"
DSH_ROOT = _cfg.root("dsh")

NOISE_PREFIXES = ("<system-reminder>", "<environment_context>", "<user_instructions>",
                  "<app-context>", "<permissions instructions>", "Current runtime context")


def _clean(t):
    t = (t or "").strip()
    for p in NOISE_PREFIXES:
        if t[:120].lstrip().startswith(p):
            return ""
    return t


def parse_file(path: Path, include_reasoning: bool):
    sid = path.parent.name
    b = SessionBundle(agent="dsh", session_id=sid, source_key=str(path),
                      source_path=str(path), cursor_mtime=path.stat().st_mtime)
    try:
        with path.open("rb") as fh:
            raw = io.TextIOWrapper(_zstd.ZstdDecompressor().stream_reader(fh),
                                   encoding="utf-8", errors="replace").read()
    except Exception as e:
        b._error = str(e)
        return b
    models = []
    toolnames = {}
    for ln in raw.splitlines():
        try:
            o = json.loads(ln)
        except json.JSONDecodeError:
            continue
        t = o.get("type")
        d = o.get("data") or {}
        ts = o.get("time") or 0
        if isinstance(ts, dict):
            ts = ts.get("created") or 0
        if t == "session":
            b.project = o.get("cwd") or d.get("cwd") or ""
            if o.get("id"):
                b.session_id = str(o["id"])
        elif t == "session/title":
            b.title = d.get("title") or b.title
        elif t == "user/message":
            txt = _clean("\n".join(c.get("text", "") for c in d.get("content") or []
                                   if isinstance(c, dict) and c.get("type") == "text"))
            if txt:
                b.events.append(Ev(ts=ts, kind="user", text=txt))
        elif t == "assistant/message":
            msg = d.get("message") or {}
            m = d.get("model") or msg.get("model") or ""
            if m and m not in models:
                models.append(m)
            u = d.get("usage") or msg.get("usage") or {}
            if u:
                b.usage.append(UsageRow(ts=ts, step=d.get("step") or 0, model=m,
                                        in_tokens=u.get("inputTokens", 0) or 0,
                                        out_tokens=u.get("outputTokens", 0) or 0,
                                        cache_tokens=(u.get("cacheReadTokens", 0) or 0) + (u.get("cacheWriteTokens", 0) or 0),
                                        total_tokens=u.get("totalTokens") or 0))
            texts = []
            for c in msg.get("content") or []:
                ct = c.get("type")
                if ct == "text":
                    texts.append(c.get("text") or "")
                elif ct == "tool-call":
                    args = c.get("arguments")
                    args = json.dumps(args, ensure_ascii=False) if isinstance(args, (dict, list)) else str(args or "")
                    cid = c.get("callId") or c.get("id") or ""
                    nm = c.get("name") or "tool"
                    if cid:
                        toolnames[cid] = nm
                    b.events.append(Ev(ts=ts, kind="tool_call", tool_name=nm, text=args))
                elif ct == "reasoning" and include_reasoning:
                    b.events.append(Ev(ts=ts, kind="reasoning", text=c.get("text") or ""))
            txt = _clean("\n\n".join(texts))
            if txt:
                b.events.append(Ev(ts=ts, kind="assistant", text=txt))
            err = d.get("error") or msg.get("error")
            if err:
                b.events.append(Ev(ts=ts, kind="system", status="error",
                                   text=json.dumps(err, ensure_ascii=False)[:2000]))
        elif t == "tool/result":
            msg = d.get("message") or {}
            cid = msg.get("toolCallId") or (msg.get("source") or {}).get("callId") or ""
            out = "\n".join(c.get("text", "") for c in msg.get("content") or []
                            if isinstance(c, dict) and c.get("type") == "text")
            status = "error" if (msg.get("is_error") or msg.get("isError")) else "ok"
            b.events.append(Ev(ts=ts, kind="tool_result", tool_name=toolnames.get(cid, ""),
                               status=status, text=out))
        # 其余（turn/step/request/job/delivery 等基础设施事件）静默丢弃，不计 unknown
    b._models = models
    if b.events:
        tstamps = [e.ts for e in b.events if e.ts]
        b.started_at, b.ended_at = min(tstamps), max(tstamps)
    return b


def collect(con, state, ctx, stats):
    if not HAVE_ZSTD:
        print("[dsh] 缺 zstandard，跳过（用 envs\\default python 运行）")
        return
    if not DSH_ROOT.is_dir():
        print(f"[dsh] 源目录不存在: {DSH_ROOT}")
        return
    for path in sorted(DSH_ROOT.glob("*/session-*/session.v4.jsonl.zstd")):
        if not ctx.get("backfill"):
            mdate = _time.strftime("%Y-%m-%d", _time.localtime(path.stat().st_mtime))
            if mdate != ctx.get("date_tag"):
                continue
        if state.get(str(path)) == path.stat().st_mtime:
            stats["skipped"] += 1
            continue
        b = parse_file(path, ctx.get("include_reasoning", False))
        if getattr(b, "_error", None):
            print(f"[dsh] 解压失败 {path.parent.name}: {b._error}")
            continue
        write_session(con, b, state, stats)
