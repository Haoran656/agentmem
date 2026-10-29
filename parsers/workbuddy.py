# -*- coding: utf-8 -*-
"""parsers/workbuddy.py — WorkBuddy 会话转录 -> SessionBundle

源: ~/.workbuddy/projects/<编码cwd>/<sid>.jsonl  事件类型:
  session-meta / message(user|assistant) / reasoning / function_call /
  function_call_result / ai-title / file-history-snapshot(丢弃)
游标: 文件 mtime（float）
"""
import json
import time
from pathlib import Path

from core import Ev, UsageRow, SessionBundle, write_session, skip_fresh
import agentmem_config as _cfg

AGENT = "workbuddy"
WB_PROJECTS = _cfg.root("workbuddy")

NOISE_PREFIXES = ("<system-reminder", "<user_info>", "<environment_context>",
                  "<user_instructions>", "<app-context>", "<permissions instructions>",
                  "<turn_aborted>", "The approval policy changed",
                  "Current runtime context", "<memory>", "<working_memory_content>")


def _clean(t: str) -> str:
    t = (t or "").strip()
    head = t[:120].lstrip()
    for p in NOISE_PREFIXES:
        if head.startswith(p):
            return ""
    return t


def _decode_dir(dname: str) -> str:
    # 路径编码规则: "c-Users-<user>-WorkBuddy-Proj" -> "C:\\Users\\<user>\\WorkBuddy\\Proj"（近似还原）
    parts = dname.split("-", 1)
    if len(parts) == 2 and len(parts[0]) == 1:
        return (parts[0].upper() + ":\\" + parts[1].replace("-", "\\"))
    return dname


def parse_file(path: Path, include_reasoning: bool):
    sid = path.stem
    b = SessionBundle(agent="workbuddy", session_id=sid, source_key=str(path),
                      source_path=str(path), cursor_mtime=path.stat().st_mtime)
    models = []
    title_seen = ""
    stepmodels = {}  # (messageId,stepSeq) -> model
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        typ = o.get("type")
        ts = o.get("timestamp") or 0
        if typ == "file-history-snapshot" or typ == "session-meta":
            continue
        if not b.project:
            cwd = o.get("cwd")
            if cwd:
                b.project = cwd
        pd = o.get("providerData") or {}
        if typ == "ai-title":
            title_seen = o.get("aiTitle") or title_seen
            continue
        if typ == "message":
            role = o.get("role")
            if role not in ("user", "assistant"):
                continue
            content = o.get("content") or []
            if isinstance(content, str):
                parts = [content]
            else:
                parts = [_clean((c.get("text") or c.get("input_text") or c.get("output_text") or "")
                                if isinstance(c, dict) else str(c))
                         for c in content]
            text = "\n\n".join(p for p in parts if p)
            if not text:
                continue
            b.events.append(Ev(ts=ts, kind=role, text=text))
        elif typ == "reasoning":
            if include_reasoning:
                txt = _clean("\n".join(c.get("text", "") for c in o.get("content") or [] if c.get("type") in ("text", "reasoning")))
                if txt:
                    b.events.append(Ev(ts=ts, kind="reasoning", text=txt))
        elif typ == "function_call":
            b.events.append(Ev(ts=ts, kind="tool_call",
                               tool_name=o.get("name") or pd.get("name") or "",
                               text=str(o.get("arguments") or "")))
            m = pd.get("model") or ""
            if m and m not in models:
                models.append(m)
            u = pd.get("usage") or {}
            if u:
                indet = u.get("inputTokensDetails") or []
                cache = sum(d.get("cached_tokens", 0) for d in indet if isinstance(d, dict))
                b.usage.append(UsageRow(ts=ts, step=pd.get("stepSeq") or 0, model=m,
                                        in_tokens=u.get("inputTokens", 0) or 0,
                                        out_tokens=u.get("outputTokens", 0) or 0,
                                        cache_tokens=cache,
                                        total_tokens=u.get("totalTokens") or 0))  # WB: cache 是 input 子集，total=in+out
        elif typ == "function_call_result":
            out = o.get("output")
            if isinstance(out, (dict, list)):
                out = json.dumps(out, ensure_ascii=False)
            b.events.append(Ev(ts=ts, kind="tool_result",
                               tool_name=o.get("name") or "",
                               status=str(o.get("status") or ""),
                               text=str(out or "")))
        elif typ == "credit-usage":
            # 每次请求的 credit 增量，会话求和；不产生语义事件
            b.credit += float(pd.get("credit") or 0)
        elif typ == "model-usage":
            m = pd.get("model") or ""
            if m and m not in models:
                models.append(m)
            u = pd.get("usage") or {}
            raw = pd.get("rawUsage") or {}
            if u or raw:
                b.usage.append(UsageRow(
                    ts=ts, step=pd.get("stepSeq") or 0, model=m,
                    in_tokens=u.get("inputTokens") or raw.get("prompt_tokens") or 0,
                    out_tokens=u.get("outputTokens") or raw.get("completion_tokens") or 0,
                    cache_tokens=u.get("cachedInputTokens")
                                 or (raw.get("prompt_tokens_details") or {}).get("cached_tokens") or 0))
        else:
            b._unknown = getattr(b, "_unknown", 0) + 1
    b.title = title_seen
    if not b.project:
        b.project = _decode_dir(path.parent.name)
    if b.events:
        b.started_at = min(e.ts for e in b.events if e.ts)
        b.ended_at = max(e.ts for e in b.events if e.ts)
    b._models = models
    return b


def collect(con, state, ctx, stats):
    if not WB_PROJECTS.is_dir():
        print(f"[workbuddy] 源目录不存在: {WB_PROJECTS}")
        return
    for path in sorted(WB_PROJECTS.glob("*/*.jsonl")):
        if skip_fresh(path, ctx):
            stats["skipped"] += 1
            continue
        if not ctx.get("backfill"):
            mdate = time.strftime("%Y-%m-%d", time.localtime(path.stat().st_mtime))
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
