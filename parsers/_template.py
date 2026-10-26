# -*- coding: utf-8 -*-
"""parsers/_template.py — 新 agent parser 契约模板（复制本文件改名即接入）

四步接入一个新 agent：
  1. 探查格式：找到它的会话存储（常见：~/.<app>/ 下 jsonl / sqlite db / zstd 流）
  2. 复制本文件为 parsers/<myagent>.py：改 AGENT/ROOT、实现 collect()，
     把原始记录映射进 Ev/UsageRow，组装 SessionBundle 交给 write_session
  3. 无需注册：pipeline/MCP 自动发现；可选在 agentmem_config.DEFAULT_ROOTS 加默认路径、
     dashboard AGENT_COLOR 加颜色（不加则中性灰兜底）
  4. 自测：python agentmem.py sync -- 或 python pipeline.py --agent <myagent>
     → audit_secrets.py → query.py search 某关键词

契约要点（与 core.py 对齐）：
  - 每个源(会话)一个 SessionBundle；source_key 全局唯一（文件绝对路径 或 "db:<sid>"）
  - 游标由 write_session 统一管理：文件类填 cursor_mtime、db 类填 cursor_ts，
    与 sync_state 相等则自动跳过（幂等），parser 不用自己比游标
  - 脱敏(sanitize)在 write_session 内统一执行，parser 只管映射
  - 下划线开头的模块被 discover() 跳过（本模板因此不会执行）
"""
import json
from pathlib import Path

from core import Ev, UsageRow, SessionBundle, write_session
import agentmem_config as _cfg

# ── 必填 ────────────────────────────────────────────────
AGENT = "myagent"        # 端名（全局唯一，小写；MCP 参数/过滤都用它）
ROOT = _cfg.root(AGENT) or (Path.home() / ".myagent" / "sessions")
# root 解析顺序：env AGENTMEM_ROOT_MYAGENT > config agents.myagent.root > 此处兜底默认


def collect(con, state, ctx, stats):
    """pipeline 调用的唯一入口。

    con   : agentmem.db 连接（可写）
    state : 游标 dict {source_key: mtime或ts}（write_session 会自动维护，读也行）
    ctx   : {"backfill": bool, "date_tag": str, "include_reasoning": bool}
    stats : 计数器（sessions/events/skipped/empty/redacted_hits/unknown_events）
    """
    if not ROOT.is_dir():
        print(f"[{AGENT}] 源目录不存在: {ROOT}")
        return
    for path in sorted(ROOT.glob("*.json")):          # TODO: 换成真实遍历
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        events, usage, last_ts = [], [], 0
        for r in records:                              # TODO: 换成真实映射
            ts = int(path.stat().st_mtime * 1000)
            last_ts = max(last_ts, ts)
            kind = {"user": "user", "assistant": "assistant"}.get(r.get("role"), "system")
            events.append(Ev(ts=ts, kind=kind, text=r.get("text", "")))
            # 工具调用示例：
            # events.append(Ev(ts=ts, kind="tool_call", tool_name="bash",
            #                  status="ok", text=json.dumps(cmd_args, ensure_ascii=False)))
        b = SessionBundle(
            agent=AGENT,
            session_id=path.stem,
            source_key=str(path),                      # 游标键：绝对路径
            source_path=str(path),
            cursor_mtime=path.stat().st_mtime,         # 文件类源游标
            project=str(path.parent),
            title=path.stem,
            events=events,                             # ← 别忘了传！
            usage=usage)                               # db 类源可加 UsageRow 列表
        write_session(con, b, state, stats)            # 幂等重放+脱敏+计数，全部内部完成
