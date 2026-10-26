# -*- coding: utf-8 -*-
"""query.py — 检索 CLI（trigram FTS + 短词 LIKE 兜底 + 维度过滤 + 渐进披露）

用法:
  python query.py search "关键词" [--agent workbuddy] [--project <工作区或逻辑项目名>]
                   [--kind tool_call] [--since 2026-10-01] [--until 2026-10-26]
                   [-n 12] [--detail index|context|full] [--tr] [--ctx]
  python query.py session <agent> <session_id> [--role all|user|assistant|tool] [-n 60]
  python query.py stats [天数=30]
  python query.py recent [-n 15]

检索路由：>=3 字符走 trigram FTS（中文子串+【】高亮）；短词回退 LIKE。
核心与 MCP server 共享（querylib.build_search）。
"""
import argparse
import re
import sqlite3
import sys
import time

import agentmem_db
import agentmem_config as _cfg
import querylib

CJK = re.compile(r"[\u4e00-\u9fff]")
ASCII_TOKEN = re.compile(r"[A-Za-z0-9_.\-]{2,}")


def fmt_ms(ms):
    if not ms:
        return "?"
    return time.strftime("%m-%d %H:%M", time.localtime(ms / 1000))


def search(con, args):
    q = args.query
    t0 = time.perf_counter()
    rows = querylib.build_search(con, q, agent=args.agent, project=args.project,
                                 kind=args.kind, since=args.since, until=args.until,
                                 limit=args.n, include_tool_result=args.tr)
    dt = (time.perf_counter() - t0) * 1000
    if not rows:
        print(f"未命中：{q!r}"); return
    sessions = querylib.group_by_session(rows, 100)
    detail = args.detail if args.detail != "auto" else ("context" if len(sessions) == 1 else "index")

    note = "（含tool_result）" if (args.tr or args.kind == "tool_result") else "（已默认排除tool_result噪声）"
    total_n = sum(s["n"] for s in sessions)
    print(f"查询 {q!r}: {total_n} 条命中 / {len(sessions)} 个会话  [{dt:.0f}ms] {note} detail={detail}\n")
    for s in sessions[:max(1, min(args.n, 30))]:
        print(f"● [{s['agent']}] {s['project']} | {(s['title'] or '(无标题)')[:40]} | "
              f"{fmt_ms(s['first_ts'])}~{fmt_ms(s['last_ts'])} | 命中{s['n']}条")
        print(f"  session_id={s['session_id']}")
        if detail != "index":
            rs = [r for r in rows if r["session_id"] == s["session_id"] and r["agent"] == s["agent"]]
            for r in rs[: (15 if detail == "full" else args.ctx_n if args.ctx else 5)]:
                tag = r["kind"] + (f":{r['tool_name']}" if r["tool_name"] else "")
                txt = (r["txt"] or "").replace("\n", " ")[:200]
                print(f"   {fmt_ms(r['ts'])} [{tag}] {txt}")
        print()


def session_view(con, args):
    where = ["e.agent=?", "e.session_id=?"]
    params = [args.agent, args.session_id]
    if args.role == "user":
        where.append("e.kind='user'")
    elif args.role == "assistant":
        where.append("e.kind='assistant'")
    elif args.role == "tool":
        where.append("e.kind IN ('tool_call','tool_result')")
    s = con.execute("SELECT * FROM sessions WHERE agent=? AND session_id=?", (args.agent, args.session_id)).fetchone()
    if s:
        print(f"会话 {args.agent}/{args.session_id}")
        print(f"  项目: {s['project']}  标题: {s['title']}  模型: {s['models']}")
        print(f"  起止: {fmt_ms(s['started_at'])} ~ {fmt_ms(s['ended_at'])}  事件: {s['n_events']}  "
              f"tokens(in/out): {s['in_tokens']:,}/{s['out_tokens']:,}  credit: {s['credit'] or '-'}")
    rows = con.execute(f"SELECT e.* FROM events e WHERE {' AND '.join(where)} ORDER BY e.ts LIMIT ?",
                       params + [args.n]).fetchall()
    for r in rows:
        tag = r["kind"] + (f":{r['tool_name']}" if r["tool_name"] else "") + (f"({r['status']})" if r["status"] else "")
        txt = (r["text"] or "").replace("\n", " ")[:200]
        print(f" {fmt_ms(r['ts'])} [{tag}] {txt}")


def stats(con, args):
    days = args.days or 30
    frm = int((time.time() - days * 86400) * 1000)
    print(f"== 近 {days} 天活动（按端）==")
    for r in con.execute("""SELECT agent, count(DISTINCT session_id) sess, count(*) ev
        FROM events WHERE ts>=? GROUP BY agent ORDER BY ev DESC""", (frm,)):
        print(f"  {r['agent']:<10} 会话{r['sess']:>4}  事件{r['ev']:>7,}")
    print("\n== 按天 ==")
    for r in con.execute(f"""SELECT date(ts/1000,'unixepoch','{_cfg.tz()}') d, count(*) ev,
        count(DISTINCT agent||'/'||session_id) sess FROM events WHERE ts>=? GROUP BY d ORDER BY d DESC LIMIT 14""", (frm,)):
        bar = "▇" * min(40, max(1, r["ev"] // 60))
        print(f"  {r['d']}  {r['sess']:>3}会话 {r['ev']:>6,}事件 {bar}")
    print("\n== 工具 TOP10 ==")
    for r in con.execute("""SELECT agent,tool_name,count(*) c FROM events
        WHERE ts>=? AND kind='tool_call' AND tool_name IS NOT NULL GROUP BY 1,2 ORDER BY c DESC LIMIT 10""", (frm,)):
        print(f"  {r['agent']:<10} {r['tool_name']:<18} {r['c']:>6,}")
    print("\n== 花费汇总（转录口径）==")
    for r in con.execute("SELECT agent, sum(in_tokens+out_tokens+cache_tokens) tok, sum(cost_usd) usd, sum(cost_cny) cny, sum(credit) credit FROM sessions GROUP BY agent"):
        extra = []
        if r["usd"]: extra.append(f"${r['usd']:.2f}")
        if r["cny"]: extra.append(f"¥{r['cny']:.2f}")
        if r["credit"]: extra.append(f"credit {r['credit']:.1f}")
        print(f"  {r['agent']:<10} tokens {r['tok'] or 0:>14,}  " + " ".join(extra))


def recent(con, args):
    rows = con.execute("""SELECT agent, session_id, project_name, title, ended_at, n_events, models
        FROM sessions ORDER BY ended_at DESC LIMIT ?""", (args.n,)).fetchall()
    for r in rows:
        try:
            mdl = ",".join(__import__("json").loads(r["models"] or "[]"))[:28]
        except Exception:
            mdl = ""
        print(f" {fmt_ms(r['ended_at'])} [{r['agent']:<9}] {r['project_name'] or '?':<18} "
              f"{(r['title'] or '(无标题)')[:32]:<34} {r['n_events']:>5}事件  {mdl}")


def main():
    ap = argparse.ArgumentParser(prog="query.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--agent"); p.add_argument("--project"); p.add_argument("--kind")
    p.add_argument("--since"); p.add_argument("--until")
    p.add_argument("-n", type=int, default=12)
    p.add_argument("--tr", action="store_true", dest="tr", help="包含 tool_result（默认排除噪声）")
    p.add_argument("--detail", default="auto", choices=["auto", "index", "context", "full"])
    p.add_argument("--ctx", action="store_true", help="每会话多显示几条")
    p.add_argument("--ctx-n", type=int, default=5, dest="ctx_n")
    p.set_defaults(fn=search)
    p = sub.add_parser("session")
    p.add_argument("agent"); p.add_argument("session_id")
    p.add_argument("--role", default="all", choices=["all", "user", "assistant", "tool"])
    p.add_argument("-n", type=int, default=60)
    p.set_defaults(fn=session_view)
    p = sub.add_parser("stats")
    p.add_argument("days", nargs="?", type=int, default=30)
    p.set_defaults(fn=stats)
    p = sub.add_parser("recent")
    p.add_argument("-n", type=int, default=15)
    p.set_defaults(fn=recent)
    args = ap.parse_args()
    con = agentmem_db.connect()
    args.fn(con, args)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
