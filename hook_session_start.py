# -*- coding: utf-8 -*-
"""hook_session_start.py — Claude Code SessionStart hook：把全机 agent 近期活动注入新会话上下文

设计：只读、限时、失败静默（hook 挂了绝不能挡开会话）。
输出 < 800 字符：近3天各端事件量 + 最近 6 个会话卡片。
"""
import json
import sys, time

sys.stdout.reconfigure(encoding="utf-8")
try:
    import querylib
    import agentmem_config as _cfg
    con = querylib.connect_ro()
    frm = int((time.time() - 3 * 86400) * 1000)
    rows = con.execute(
        f"SELECT date(s.ended_at/1000,'unixepoch','{_cfg.tz()}') d, s.agent, s.project_name,"
        " substr(coalesce(s.title,''),1,28) title, s.session_id"
        " FROM sessions s WHERE s.ended_at>=? ORDER BY s.ended_at DESC LIMIT 6", (frm,)).fetchall()
    ev = con.execute(
        "SELECT agent, count(DISTINCT session_id) c FROM events WHERE ts>=? GROUP BY agent", (frm,)).fetchall()
    con.close()
    if not rows:
        sys.exit(0)
    act = " ".join(f"{r['agent']}:{r['c']}" for r in ev)
    lines = ["[agentmem 跨端记忆·近3天] 全机活动: " + act,
             "最近会话(可用 mcp__agentmem__search_events/get_session/timeline 深挖):"]
    for r in rows:
        lines.append(f"- {r['d']} [{r['agent']}] {r['project_name']} | {r['title'] or '(无标题)'}")
    lines.append("用户问'之前/上次/别的agent里做过什么'时先查 agentmem。")
    ctx = "\n".join(lines)[:1000]
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}))
except Exception:
    sys.exit(0)
