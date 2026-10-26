# -*- coding: utf-8 -*-
"""
mcp_agentmem.py — 跨 Agent 共享记忆 L3：只读检索 MCP server（stdio, JSON-RPC 2.0）

红线：
  1. DB 以 mode=ro 打开且 PRAGMA query_only=ON，物理禁写
  2. 启动时校验 audit_ok 标记（每日 automation 在脱敏审计通过后写入）；缺失/超过 3 天 -> 拒绝启动
  3. 返回文本硬截断（snippet 200、事件正文 400），防整库 dump
  4. 查询日志不落原文（长度与调用名入 stderr 可选，默认全关）

单文件零第三方依赖。用 envs\\default python 运行。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

AM_DIR = Path(__file__).resolve().parent
AUDIT_MARK = AM_DIR / "audit_ok.json"
AUDIT_MAX_AGE_DAYS = 3
LAZY_TS_FILE = AM_DIR / ".lazy_sync_ts"
LAZY_LOCK_FILE = AM_DIR / ".lazy_sync.lock"
LAZY_MIN_INTERVAL = 45.0   # 秒；距上次同步不足则跳过（多实例经时间戳文件共享节流）
LAZY_TIMEOUT = 120.0        # 子进程同步硬上限


_AUDIT_CLOSED = None  # 一旦本进程内 lazy 审计未过，粘性拒答（红线：未审计内容不出库）


def lazy_sync():
    """查询前懒同步：子进程跑 pipeline+audit，server 本体连接始终只读。
    返回状态: ok / skip(节流或他人持锁) / fail(降级用旧快照)。
    审计未过 -> 粘性 _AUDIT_CLOSED，后续所有查询拒答。AGENTMEM_NO_LAZY=1 可关。"""
    global _AUDIT_CLOSED
    if _AUDIT_CLOSED:
        return "audit_closed"
    if os.environ.get("AGENTMEM_NO_LAZY"):
        return "skip"
    now = time.time()
    try:
        if LAZY_TS_FILE.exists() and now - LAZY_TS_FILE.stat().st_mtime < LAZY_MIN_INTERVAL:
            return "skip"
    except OSError:
        return "skip"
    # 跨实例互斥：O_EXCL 锁，超过 5 分钟视为残留自动清理
    try:
        fd = os.open(str(LAZY_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            if now - LAZY_LOCK_FILE.stat().st_mtime > 300:
                LAZY_LOCK_FILE.unlink(missing_ok=True)
                fd = os.open(str(LAZY_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            else:
                return "skip"  # 别的实例正在同步，用现有快照
        except OSError:
            return "skip"
    except OSError:
        return "skip"
    try:
        os.write(fd, str(now).encode())
        os.close(fd)
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        LAZY_TS_FILE.touch()  # 先落节流戳：失败也节流，防每次查询都卡 subprocess
        r1 = subprocess.run([sys.executable, str(AM_DIR / "pipeline.py")],
                            capture_output=True, timeout=LAZY_TIMEOUT, env=env, cwd=str(AM_DIR))
        if r1.returncode != 0:
            return "fail"
        r2 = subprocess.run([sys.executable, str(AM_DIR / "audit_secrets.py")],
                            capture_output=True, timeout=LAZY_TIMEOUT, env=env, cwd=str(AM_DIR))
        if r2.returncode != 0:
            _AUDIT_CLOSED = "懒同步审计未通过：新内容有密钥残留嫌疑，本进程已停止供数（修复 sanitize 规则并重跑审计后重启客户端恢复）"
            return "audit_closed"
        return "ok"
    except Exception:
        return "fail"  # 超时/异常静默降级，用旧快照
    finally:
        try:
            LAZY_LOCK_FILE.unlink(missing_ok=True)
        except OSError:
            pass

import querylib  # noqa: E402


def check_audit_gate():
    if not AUDIT_MARK.exists():
        return ("脱敏审计标记不存在（先跑通 pipeline.py + audit_secrets.py，"
                "automation 会在审计通过后写入 audit_ok.json）")
    try:
        m = json.loads(AUDIT_MARK.read_text(encoding="utf-8"))
    except Exception:
        return "audit_ok.json 无法解析"
    age = (time.time() - m.get("ts", 0)) / 86400
    if m.get("residues") != 0:
        return "最近一次审计未通过（residues != 0）"
    if age > AUDIT_MAX_AGE_DAYS:
        return f"审计标记已过期 {age:.1f} 天（>{AUDIT_MAX_AGE_DAYS}），请先重新跑审计"
    return None


TOOLS = [
    {
        "name": "search_events",
        "description": ("全文检索本机六个 AI agent（WorkBuddy/Codex/Claude Code/Kilo/OpenCode/DeepSeek Harness）"
                        "的历史会话事件：用户消息、助手回复、工具调用。中文英文皆支持（>=3字符走子串索引，命中处用【】高亮）。"
                        "结果顶部先返回'沉淀洞察'（decision/fact/lesson，结论层，优先采信），再列原始会话（证据层）。"
                        "project 参数支持逻辑项目名（自动展开各端别名）。"
                        "默认排除 tool_result 噪声，需要时传 include_tool_result=true。"
                        "用于回答“以前做过什么、怎么做的、在哪讨论过 X、当时的决策是什么”类问题。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词（中英文皆可，>=3字符走子串索引更快更准）"},
                "agent": {"type": "string", "description": "端名（内置: workbuddy/codex/claude/kilo/opencode/dsh；自加 parser 的端名同样可用）"},
                "project": {"type": "string", "description": "项目/工作区名，支持 project_map 登记的逻辑项目名（自动展开各端别名）"},
                "kind": {"type": "string", "enum": ["user", "assistant", "tool_call", "tool_result"]},
                "since": {"type": "string", "description": "YYYY-MM-DD"},
                "until": {"type": "string", "description": "YYYY-MM-DD"},
                "include_tool_result": {"type": "boolean", "default": False,
                                        "description": "是否包含工具返回内容（默认 false 降噪）"},
                "detail": {"type": "string", "enum": ["auto", "index", "context", "full"], "default": "auto",
                           "description": "渐进披露：index=只要会话卡片(最省token)；context=每会话≤5条snippet；full=每会话≤15条；auto=多会话用index单会话用context"},
                "limit": {"type": "integer", "default": 20, "maximum": 100},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_session",
        "description": "展开某个会话的完整事件流（分页）。需先经 search_events/find_sessions 得到 agent+session_id。role 可过滤 user/assistant/tool。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "端名（见 search_events 说明）"},
                "session_id": {"type": "string"},
                "role": {"type": "string", "enum": ["user", "assistant", "tool"]},
                "offset": {"type": "integer", "default": 0},
                "page": {"type": "integer", "default": 50, "maximum": 200},
            },
            "required": ["agent", "session_id"],
        },
    },
    {
        "name": "timeline",
        "description": "以某会话为锚点，返回其时间前后各 window 个会话（跨端混排）——回答“那段时间还在做什么”。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "端名（见 search_events 说明）"},
                "session_id": {"type": "string"},
                "window": {"type": "integer", "default": 8, "maximum": 30},
            },
            "required": ["agent", "session_id"],
        },
    },
    {
        "name": "find_sessions",
        "description": "按维度（端/项目/日期/模型/标题关键词）列出历史会话卡片，不要求关键词。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "端名（见 search_events 说明）"},
                "project": {"type": "string"},
                "since": {"type": "string", "description": "YYYY-MM-DD"},
                "until": {"type": "string", "description": "YYYY-MM-DD"},
                "model": {"type": "string", "description": "模型名子串，如 glm、claude-sonnet"},
                "title_kw": {"type": "string"},
                "limit": {"type": "integer", "default": 30},
            },
        },
    },
    {
        "name": "activity_stats",
        "description": "近期活动汇总：各端会话/事件量、按天分布、工具使用 TOP。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "default": 30, "maximum": 400},
                "agent": {"type": "string", "description": "端名（见 search_events 说明）"},
            },
        },
    },
]

DISPATCH = {
    "search_events": lambda a: querylib.search_events(
        a.get("query", ""), a.get("agent"), a.get("project"), a.get("kind"),
        a.get("since"), a.get("until"), a.get("limit", 20),
        bool(a.get("include_tool_result", False)), a.get("detail")),
    "timeline": lambda a: querylib.timeline(
        a.get("agent", ""), a.get("session_id", ""),
        max(1, min(int(a.get("window") or 8), 30))),
    "get_session": lambda a: querylib.get_session(
        a.get("agent", ""), a.get("session_id", ""), a.get("role"),
        a.get("offset", 0), a.get("page", 50)),
    "find_sessions": lambda a: querylib.find_sessions(
        a.get("agent"), a.get("project"), a.get("since"), a.get("until"),
        a.get("model"), a.get("title_kw"), a.get("limit", 30)),
    "activity_stats": lambda a: querylib.activity_stats(a.get("days", 30), a.get("agent")),
}


def reply(rid, result):
    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}, ensure_ascii=False), flush=True)


def reply_err(rid, code, msg):
    print(json.dumps({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": code, "message": msg}}, ensure_ascii=False), flush=True)


def main():
    err = check_audit_gate()
    if err:
        # MCP stdio：错误写 stderr，直接退出（挂载方会看到 server 启动失败）
        print(f"[agentmem L3] 拒绝启动：{err}", file=sys.stderr)
        sys.exit(2)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        rid = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            reply(rid, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agentmem", "version": "1.0.0"},
            })
        elif method in ("notifications/initialized", "initialized"):
            continue
        elif method == "tools/list":
            reply(rid, {"tools": TOOLS})
        elif method == "ping":
            reply(rid, {})
        elif method == "tools/call":
            name = params.get("name")
            fn = DISPATCH.get(name)
            if not fn:
                reply_err(rid, -32601, f"unknown tool: {name}")
                continue
            status = lazy_sync()
            if status == "audit_closed":
                reply(rid, {"content": [{"type": "text", "text": _AUDIT_CLOSED}], "isError": True})
                continue
            try:
                text = fn(params.get("arguments") or {})
                reply(rid, {"content": [{"type": "text", "text": text}], "isError": False})
            except Exception as e:
                from sanitize import sanitize as _san
                msg, _n = _san(f"{type(e).__name__}: {e}")
                reply(rid, {"content": [{"type": "text",
                           "text": f"agentmem 查询失败: {msg}"}], "isError": True})
        elif method in ("resources/list", "prompts/list"):
            key = method.split("/")[0]
            reply(rid, {key: []})
        else:
            if rid is not None:
                reply_err(rid, -32601, f"method not found: {method}")


if __name__ == "__main__":
    # stdio MCP 关键两件事：
    # 1) stdout 行缓冲+flush，否则管道下块缓冲客户端 30s 超时 did not become ready
    # 2) stdin/stdout 强制 UTF-8——中文 Windows 下子进程默认 cp936，
    #    Node 客户端发 UTF-8 字节会被 GBK 解码成乱码导致 json 解析失败静默丢包
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    main()
