# -*- coding: utf-8 -*-
"""insight_add.py — 语义事件层写入口（唯一写面；MCP server 保持只读）

由精炼 automation / agent 调用，把会话中的"结论"沉淀成可跨端检索的洞察。
写入前强制过 sanitize（与 L2 同规），--session 会校验会话存在（防瞎挂指针）。

用法:
  python insight_add.py --type decision --project MyProject \
      --text "桥接超时定为 15s，不用默认 5s" \
      --agent workbuddy --session <session_id> --ref path/to/file.py
  python insight_add.py --list [--project X] [--type lesson]
  python insight_add.py --forget <id>
"""
import argparse
import json
import sys
import time

import agentmem_db
from sanitize import sanitize as sanitize_text


def add(args):
    text = (args.text or "").strip()
    if not text:
        print("text 为空"); return 1
    if args.type not in ("decision", "fact", "artifact", "lesson"):
        print("type 必须是 decision/fact/artifact/lesson"); return 1
    con = agentmem_db.init()
    if args.session:
        ok = con.execute("SELECT 1 FROM sessions WHERE agent=? AND session_id=?",
                         (args.agent or "", args.session)).fetchone()
        if not ok:
            print(f"会话不存在: {args.agent}/{args.session}（指针校验失败，拒绝写入）"); return 1
    clean, changed = sanitize_text(text)
    refs = json.dumps(args.ref or [], ensure_ascii=False) if args.ref else None
    # 去重：同 project + type + text 视为同一条结论；重复写入只刷新时间/引用（P2）
    dup = con.execute(
        "SELECT id FROM insights WHERE project IS ? AND type=? AND text=?",
        (args.project, args.type, clean)).fetchone()
    if dup:
        con.execute("UPDATE insights SET ts=?, created_at=?, refs=COALESCE(?, refs), redacted=? WHERE id=?",
                    (args.ts or int(time.time() * 1000), int(time.time() * 1000),
                     refs, 1 if changed else 0, dup["id"]))
        con.commit()
        print(f"insight #{dup['id']} 已存在（同项目/同类型/同文本），已刷新时间与引用")
        return 0
    con.execute(
        "INSERT INTO insights(agent,session_id,project,type,text,refs,ts,created_at,redacted) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (args.agent, args.session, args.project, args.type, clean, refs,
         args.ts or int(time.time() * 1000), int(time.time() * 1000), 1 if changed else 0))
    con.commit()
    print(f"insight #{con.execute('SELECT last_insert_rowid()').fetchone()[0]} 写入"
          + ("（含脱敏替换）" if changed else ""))
    return 0


def lst(args):
    con = agentmem_db.connect()
    where, params = ["1=1"], []
    if args.project:
        where.append("project=?"); params.append(args.project)
    if args.type:
        where.append("type=?"); params.append(args.type)
    rows = con.execute(f"SELECT * FROM insights WHERE {' AND '.join(where)} ORDER BY ts DESC LIMIT 50",
                       params).fetchall()
    if not rows:
        print("（暂无洞察）"); return 0
    for r in rows:
        print(f"#{r['id']} [{r['type']}] {r['project'] or '-'} {time.strftime('%m-%d', time.localtime(r['ts']/1000))} "
              f"[{r['agent']}/{(r['session_id'] or '')[:8]}] {r['text'][:100]}")
    return 0


def forget(args):
    con = agentmem_db.connect()
    con.execute("DELETE FROM insights WHERE id=?", (args.forget,))
    con.commit()
    print("已删除" if con.total_changes else "无此 id")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add")
    p.add_argument("--type", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--agent", default="cli"); p.add_argument("--session"); p.add_argument("--project")
    p.add_argument("--ref", action="append")
    p.add_argument("--ts", type=int)
    p.set_defaults(fn=add)
    p = sub.add_parser("list")
    p.add_argument("--project"); p.add_argument("--type")
    p.set_defaults(fn=lst)
    p = sub.add_parser("forget")
    p.add_argument("forget", type=int)
    p.set_defaults(fn=forget)
    args = ap.parse_args()
    sys.exit(args.fn(args) or 0)


if __name__ == "__main__":
    main()
