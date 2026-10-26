# -*- coding: utf-8 -*-
"""audit_secrets.py — 全库脱敏验收断言：命中任何密钥模式即非零退出（每日管道末端跑）
通过后写 audit_ok.json（L3 MCP server 的启动门禁凭据）；失败则删除旧标记。
"""
import json
import sys
import time
from pathlib import Path
import agentmem_db
from sanitize import audit

con = agentmem_db.connect()
bad = 0
tables = [("events", "id", "text"),
          ("sessions", "session_id", "project"),
          ("sessions", "session_id", "title"),
          ("sessions", "session_id", "source_path"),
          ("insights", "id", "text")]
for table, idcol, col in tables:
    for r in con.execute(f"SELECT {idcol} AS rid, {col} AS v FROM {table} WHERE {col} IS NOT NULL"):
        hits = audit(r["v"])
        if hits:
            bad += 1
            # 只报定位信息与规则名，绝不打印命中内容本身（可能含完整密钥）
            print(f"HIT {table}.{col} id={r['rid']} rules={hits} len={len(r['v'])}")
n_ev = con.execute("SELECT count(*) c FROM events").fetchone()["c"]
n_red = con.execute("SELECT count(*) c FROM events WHERE redacted=1").fetchone()["c"]
print(f"audited events={n_ev}, redacted rows={n_red}, secret residues={bad}")
mark = Path(__file__).resolve().parent / "audit_ok.json"
if bad:
    mark.unlink(missing_ok=True)
else:
    mark.write_text(json.dumps({"ts": time.time(), "residues": 0,
                                "events": n_ev, "redacted_rows": n_red},
                               ensure_ascii=False), encoding="utf-8")
    print(f"audit_ok written -> {mark.name}")
sys.exit(1 if bad else 0)
