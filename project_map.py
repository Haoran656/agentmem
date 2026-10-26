# -*- coding: utf-8 -*-
"""project_map.py — 跨端项目别名管理（逻辑项目 -> 各端 project_name）

用法:
  python project_map.py set <逻辑名> <agent>=<alias> [<agent>=<alias> ...]
  python project_map.py list
  python project_map.py rm <逻辑名>
"""
import sys
import agentmem_db


def setmap(args):
    con = agentmem_db.init()
    n = 0
    for pair in args.pairs:
        agent, _, alias = pair.partition("=")
        if not alias:
            print(f"格式错: {pair}（应为 agent=alias）"); continue
        con.execute("INSERT OR REPLACE INTO projects(logical,agent,alias) VALUES(?,?,?)",
                    (args.logical, agent.strip(), alias.strip()))
        n += 1
    con.commit()
    print(f"'{args.logical}' 映射 {n} 条")
    return 0


def lst(args):
    con = agentmem_db.connect()
    cur_logical = None
    for r in con.execute("SELECT * FROM projects ORDER BY logical,agent"):
        if r["logical"] != cur_logical:
            cur_logical = r["logical"]
            print(f"\n◆ {cur_logical}")
        print(f"   {r['agent']:<10} = {r['alias']}")
    return 0


def rm(args):
    con = agentmem_db.connect()
    con.execute("DELETE FROM projects WHERE logical=?", (args.logical,))
    con.commit(); print("已删除"); return 0


def main():
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("set"); p.add_argument("logical"); p.add_argument("pairs", nargs="+"); p.set_defaults(fn=setmap)
    p = sub.add_parser("list"); p.set_defaults(fn=lst)
    p = sub.add_parser("rm"); p.add_argument("logical"); p.set_defaults(fn=rm)
    args = ap.parse_args()
    sys.exit(args.fn(args) or 0)


if __name__ == "__main__":
    main()
