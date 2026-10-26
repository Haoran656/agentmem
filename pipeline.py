# -*- coding: utf-8 -*-
"""
pipeline.py — 跨 Agent 共享记忆 L2：统一事件索引构建管道

用法（用 ~\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe 运行）:
  python pipeline.py                 # 增量：各 parser 只处理今天有改动的源
  python pipeline.py --backfill      # 全量回填
  python pipeline.py --agent workbuddy
  python pipeline.py --include-reasoning   # reasoning 事件入库（默认丢弃）

parser 接口: collect(con, state, ctx, stats)；产出 SessionBundle 交给 core.write_session。
FTS 索引由 events 表触发器自动维护。
"""
import argparse
import importlib
import pkgutil
import sys
import time
from pathlib import Path

import agentmem_db
import agentmem_config as _cfg
from core import Ev, UsageRow, SessionBundle, write_session  # noqa: F401  (re-export for parsers)


def discover():
    """扫描 parsers/ 目录，返回 [(agent名, 已导入模块)]。新端=丢一个带 AGENT+collect 的文件，不碰这里。"""
    import parsers
    found = []
    for _finder, modname, _ispkg in pkgutil.iter_modules(parsers.__path__):
        if modname.startswith("_"):
            continue
        try:
            m = importlib.import_module(f"parsers.{modname}")
        except Exception as e:
            print(f"[{modname}] 装载失败: {type(e).__name__}: {e}")
            continue
        if hasattr(m, "collect") and getattr(m, "AGENT", modname):
            found.append((getattr(m, "AGENT", modname), m))
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d"))
    ap.add_argument("--agent", default="all")   # 端名任意（含自加 parser），不再限 choices
    ap.add_argument("--include-reasoning", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    db_path = Path(args.db) if args.db else agentmem_db.DB_PATH
    con = agentmem_db.init(db_path)

    # 游标：文件类源存 last_mtime(float)，db 类源存 last_ts(int)
    # backfill = 强制全量重放（脱敏规则升级后清存量残留用），故不预载游标
    state = {}
    if not args.backfill:
        for r in con.execute("SELECT source_key,last_mtime,last_ts FROM sync_state"):
            state[r["source_key"]] = r["last_mtime"] if r["last_mtime"] else r["last_ts"]

    ctx = dict(backfill=args.backfill, date_tag=args.date,
               include_reasoning=args.include_reasoning)

    stats = dict(sessions=0, events=0, skipped=0, empty=0,
                 redacted_hits=0, unknown_events=0)

    for nm, m in discover():
        if not _cfg.enabled(nm):
            print(f"[{nm}] 已在 config 中禁用，跳过")
            continue
        if args.agent not in ("all", nm):
            continue
        before = dict(stats)
        print(f"[{nm}] 同步中…")
        try:
            m.collect(con, state, ctx, stats)
        except Exception as e:
            # 消息过 sanitize 再打印：保留排障信息，剥离可能内嵌的原始凭据
            from sanitize import sanitize as _san
            msg, n = _san(f"{type(e).__name__}: {e}")
            print(f"[{nm}] 失败: {msg}" + (f"  ({n} 处已脱敏)" if n else ""))
            con.rollback()
            continue
        con.commit()
        print(f"[{nm}] 新/更新会话 +{stats['sessions']-before['sessions']}，"
              f"新事件 +{stats['events']-before['events']}")

    # 写回 sync_state
    con.executemany(
        "INSERT OR REPLACE INTO sync_state(source_key,last_mtime,last_ts) VALUES(?,?,?)",
        [(k, v if isinstance(v, float) else None, None if isinstance(v, float) else v)
         for k, v in state.items()])
    con.commit()

    print(f"\n== 汇总 == 本次入库会话 {stats['sessions']}，事件 {stats['events']}，"
          f"游标命中跳过 {stats['skipped']}，空会话 {stats['empty']}，"
          f"脱敏替换 {stats['redacted_hits']} 处，未知事件 {stats['unknown_events']}")
    tot = con.execute("SELECT count(*) c FROM sessions").fetchone()["c"]
    ev = con.execute("SELECT count(*) c FROM events").fetchone()["c"]
    by = con.execute("SELECT agent, count(*) s FROM sessions GROUP BY agent").fetchall()
    print(f"库内总计: sessions={tot} events={ev} 按端: " +
          " ".join(f"{r['agent']}={r['s']}" for r in by))
    print(f"db={db_path}")
    con.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
