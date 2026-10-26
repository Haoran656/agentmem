# -*- coding: utf-8 -*-
"""agentmem.py — 统一入口（面向用户；散件模块保持不变，本文件做薄分发）

  python agentmem.py detect [--json]        扫描本机 agent 存储 + MCP 客户端配置
  python agentmem.py init [--agents a,b]    生成 config + 首灌(或增量) + 审计 + dashboard
  python agentmem.py sync                   增量同步（= pipeline.py）
  python agentmem.py search <q> [opts]      检索（= query.py）
  python agentmem.py status                 一眼体检
  python agentmem.py serve                  以 MCP server 运行（一般由客户端拉起）
  python agentmem.py connect --client X     [RB] 写入客户端 MCP 配置（diff+确认+备份）
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

AM_DIR = Path(__file__).resolve().parent
PY = sys.executable


def _run(mod_args):
    return subprocess.run([PY, *mod_args], cwd=str(AM_DIR))


# ── detect ──────────────────────────────────────────────
AGENT_SOURCES = {  # agent: (默认根, 计数方式, 附加参数)
    "workbuddy": (".workbuddy/projects", "jsonl_glob", None),
    "codex":     (".codex/sessions", "jsonl_rglob", None),
    "claude":    (".claude/projects", "jsonl_glob", None),
    "kilo":      (".local/share/kilo/kilo.db", "sqlite_sessions", "session"),
    "opencode":  (".local/share/opencode/opencode.db", "sqlite_sessions", "session_v2"),
    "dsh":       (".dsh/sessions", "zstd_glob", None),
}
CLIENT_CONFIGS = {
    "workbuddy":     [".workbuddy/mcp.json"],
    "claude-code":   [".claude.json"],
    "claude-desktop":["AppData/Local/Claude/claude_desktop_config.json",
                       "AppData/Roaming/Claude/claude_desktop_config.json",
                       "AppData/Roaming/Claude-3p/claude_desktop_config.json"],
    "codex":         [".codex/config.toml"],
    "kilo":          [".config/kilo/kilo.json", ".config/kilo/kilo.jsonc"],
    "opencode":      [".config/opencode/opencode.json", ".config/opencode/opencode.jsonc"],
    "dsh":           [".dsh/profiles/desktop/cordis.patch.yml", ".dsh/profiles/web/cordis.patch.yml"],
}


def _count(kind, root, arg=None):
    try:
        if kind == "jsonl_glob":
            return sum(1 for p in root.glob("*/*.jsonl"))
        if kind == "jsonl_rglob":
            return sum(1 for p in root.rglob("*.jsonl"))
        if kind == "zstd_glob":
            return sum(1 for p in root.glob("*/session-*/session.v4.jsonl.zstd"))
        if kind == "sqlite_sessions":
            import sqlite3
            con = sqlite3.connect(f"file:{root}?mode=ro", uri=True, timeout=1.0)
            try:
                return con.execute(f"SELECT count(*) FROM {arg}").fetchone()[0]
            finally:
                con.close()
    except Exception:
        return None
    return None


def cmd_detect(args):
    import agentmem_config as cfg
    as_json = getattr(args, "json", False)
    report = {"agents": {}, "clients": {}}
    for ag, (rel, kind, arg) in AGENT_SOURCES.items():
        root = cfg.root(ag) or (Path.home() / rel)
        exists = root.exists()
        n = _count(kind, root, arg) if exists else None
        report["agents"][ag] = {"root": str(root), "present": bool(exists), "sessions": n}
    for cl, rels in CLIENT_CONFIGS.items():
        found = next((str(Path.home() / r) for r in rels if (Path.home() / r).exists()), None)
        report["clients"][cl] = {"config": found, "present": bool(found)}
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0
    print("== agent 存储（数据源：可被查）==")
    for ag, r in report["agents"].items():
        mark = "✓" if r["present"] else "·"
        n = f"{r['sessions']} 会话" if r["sessions"] is not None else ""
        print(f"  [{mark}] {ag:<10} {n:<10} {r['root']}")
    print("\n== MCP 客户端（连接器：可以去查）==")
    for cl, r in report["clients"].items():
        mark = "✓" if r["present"] else "·"
        print(f"  [{mark}] {cl:<15} {r['config'] or '未发现配置'}")
    print("\n下一步： python agentmem.py init --agents <选中的端,逗号分隔>")
    print("注意：claude-desktop / 豆包类云端 app 无本地转录，不支持作为数据源（仅可作为客户端 connect）。")
    return 0


# ── init ────────────────────────────────────────────────
def cmd_init(args):
    import agentmem_config as cfg
    if args.agents:
        chosen = [a.strip() for a in args.agents.split(",") if a.strip()]
        agents_cfg = {}
        for ag in AGENT_SOURCES:
            agents_cfg[ag] = {"enabled": ag in chosen}
        conf = {"version": 1, "timezone": cfg.tz(), "agents": agents_cfg,
                "optional": cfg.load().get("optional", {})}
        p = cfg.CONFIG_PATH
        if p.exists() and not args.yes:
            print(f"已存在 {p.name}，将覆盖（--yes 免提示）")
        p.write_text(json.dumps(conf, ensure_ascii=False, indent=1), encoding="utf-8")
        cfg.reload_()
        print(f"config 写入：启用 {chosen}")
    print("\n[1/3] 同步入库（首灌用 --backfill）…")
    rc = _run(["pipeline.py"] + (["--backfill"] if args.backfill else []))
    print("\n[2/3] 脱敏审计…")
    rc2 = _run(["audit_secrets.py"])
    if rc or rc2:
        print("\ninit 未完成（见上方错误），已跳过工作台。修复后重跑。")
        return 1
    print("\n[3/3] 工作台…")
    _run(["dashboard.py"])
    print("\nOK。把 MCP server 接入你的 agent（见 connect 命令/README），开始跨端检索。")
    return 0


# ── status ──────────────────────────────────────────────
def cmd_status(_args):
    import sqlite3
    import agentmem_config as cfg
    db = cfg.AM_DIR / "agentmem.db"
    if not db.exists():
        print("库不存在——先跑 init")
        return 1
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    sess, ev = con.execute("SELECT count(*) FROM sessions").fetchone()[0], \
               con.execute("SELECT count(*) FROM events").fetchone()[0]
    by = con.execute("SELECT agent,count(DISTINCT session_id) FROM sessions GROUP BY 1").fetchall()
    latest = con.execute("SELECT max(ts) FROM events").fetchone()[0]
    lag = (time.time()*1000 - latest)/3600000 if latest else None
    con.close()
    mark = lambda ok: "✓" if ok else "✗"
    gate = cfg.AM_DIR / "audit_ok.json"
    gate_age = (time.time() - gate.stat().st_mtime)/3600 if gate.exists() else None
    print(f"库: {sess} 会话 / {ev} 事件   端分布: {' '.join(f'{a}={n}' for a,n in by)}")
    print(f"数据滞后: {lag:.1f}h" if lag is not None else "数据滞后: -")
    print(f"审计门禁: {mark(gate_age is not None and gate_age<72)} audit_ok（{gate_age:.1f}h 前）"
          if gate_age is not None else "审计门禁: ✗ 未通过/未运行")
    print(f"config: {mark(cfg.CONFIG_PATH.exists())} {cfg.CONFIG_PATH.name}"
          f"（缺省=全端默认行为） | 时区: {cfg.tz()}")
    return 0


# ── connect (RB 实现) ───────────────────────────────────
def cmd_connect(args):
    import connect
    return connect.cmd_connect(args)


def main():
    ap = argparse.ArgumentParser(prog="agentmem.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("detect"); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_detect)
    p = sub.add_parser("init"); p.add_argument("--agents"); p.add_argument("--backfill", action="store_true")
    p.add_argument("--yes", action="store_true"); p.set_defaults(fn=cmd_init)
    p = sub.add_parser("sync"); p.set_defaults(fn=lambda a: _run(["pipeline.py"]).returncode)
    p = sub.add_parser("status"); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("serve"); p.set_defaults(fn=lambda a: subprocess.run([PY, str(AM_DIR/"mcp_agentmem.py")]).returncode)
    p = sub.add_parser("connect"); p.add_argument("--client"); p.add_argument("--all", action="store_true")
    p.add_argument("--yes", action="store_true"); p.set_defaults(fn=cmd_connect)
    p = sub.add_parser("search"); p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(fn=lambda a: _run(["query.py", "search", *a.rest]).returncode)
    args = ap.parse_args()
    sys.exit(args.fn(args) or 0)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
