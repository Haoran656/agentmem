# -*- coding: utf-8 -*-
"""audit_secrets.py — 全库脱敏验收：命中任何密钥模式即非零退出（每日管道末端跑）

默认增量：只审计 id > last_event_id 的新事件（写时已脱敏，这是复核）；sessions/insights
体量小，每次全量。每周（或 --full）做一次全库扫描，防规则升级后的存量残留。

通过后写 audit_ok.json（L3 MCP server 的启动门禁凭据）；失败则删除旧标记。
"""
import argparse
import json
import sys
import time
from pathlib import Path

import agentmem_db
from sanitize import audit

FULL_AUDIT_DAYS = 7


def _read_mark(mark: Path):
    try:
        return json.loads(mark.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="强制全库重扫（脱敏规则升级后使用）")
    args = ap.parse_args()

    con = agentmem_db.connect()
    mark = Path(__file__).resolve().parent / "audit_ok.json"
    prev = _read_mark(mark)

    max_id = con.execute("SELECT COALESCE(max(id),0) m FROM events").fetchone()["m"]
    start_id = int(prev.get("last_event_id") or 0)
    full_ts = float(prev.get("full_ts") or 0)
    full = args.full or not prev or start_id > max_id or (time.time() - full_ts) > FULL_AUDIT_DAYS * 86400
    if full:
        start_id = 0

    bad = 0
    n = 0
    for r in con.execute("SELECT id, text FROM events WHERE id > ? AND text IS NOT NULL", (start_id,)):
        n += 1
        if audit(r["text"]):
            bad += 1
            print(f"HIT events.text id={r['id']} len={len(r['text'])}")

    # 元数据/结论层：行数少，每次全量
    for table, idcol, cols in [
            ("sessions", "session_id", ("project", "title", "source_path")),
            ("insights", "id", ("text",))]:
        for col in cols:
            for r in con.execute(f"SELECT {idcol} AS rid, {col} AS v FROM {table} WHERE {col} IS NOT NULL"):
                if audit(r["v"]):
                    bad += 1
                    print(f"HIT {table}.{col} id={r['rid']} len={len(r['v'])}")

    n_ev = con.execute("SELECT count(*) c FROM events").fetchone()["c"]
    n_red = con.execute("SELECT count(*) c FROM events WHERE redacted=1").fetchone()["c"]
    mode = "full" if full else f"incremental(id>{start_id})"
    print(f"audited {mode}: scanned_events={n}, total_events={n_ev}, redacted_rows={n_red}, residues={bad}")
    if bad:
        mark.unlink(missing_ok=True)
        return 1
    mark.write_text(json.dumps({
        "ts": time.time(), "residues": 0, "events": n_ev, "redacted_rows": n_red,
        "last_event_id": max_id,
        "full_ts": time.time() if full else full_ts,
        "mode": mode,
    }, ensure_ascii=False), encoding="utf-8")
    print(f"audit_ok written -> {mark.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
