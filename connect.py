# -*- coding: utf-8 -*-
"""connect.py / disconnect.py — 把 agentmem 的 MCP 条目接入/移除各 agent 客户端配置

安全策略（拍板）：探测配置文件 → 打印将写入的条目(diff) → 确认(--yes 跳过) →
备份原文件到 .agentmem-backup/<ts> → 幂等合并（同名已存在且等价=跳过；不等价=提示冲突不强改）。
只增删自己那条(agentmem)，绝不动用户其它条目。
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

AM_DIR = Path(__file__).resolve().parent
SERVER = str(AM_DIR / "mcp_agentmem.py")
PY = sys.executable
MARK = "agentmem"

# JSON 类客户端：显示名 -> (候选路径[探已存在], 缺省创建路径, 键路径, 条目形态)
JSON_CLIENTS = {
    "workbuddy":      ([".workbuddy/mcp.json"], ".workbuddy/mcp.json", ["mcpServers"], "obj"),
    "claude-code":    ([".claude.json"], ".claude.json", ["mcpServers"], "obj"),
    "claude-desktop": (["AppData/Roaming/Claude/claude_desktop_config.json",
                        "AppData/Roaming/Claude-3p/claude_desktop_config.json"],
                       "AppData/Roaming/Claude/claude_desktop_config.json", ["mcpServers"], "obj"),
    "kilo":           ([".config/kilo/kilo.json", ".config/kilo/kilo.jsonc"],
                       ".config/kilo/kilo.json", ["mcp"], "local_timeout"),
    "opencode":       ([".config/opencode/opencode.json", ".config/opencode/opencode.jsonc"],
                       ".config/opencode/opencode.json", ["mcp", "servers"], "local"),
}


CLIENT_AGENT = {
    "workbuddy": "workbuddy", "claude-code": "claude", "claude-desktop": "claude",
    "kilo": "kilo", "opencode": "opencode", "codex": "codex", "dsh": "dsh",
}


def entry_for(kind, agent=""):
    """agent 非空时写入 --self-agent，server 启动即知道“我是谁拉起来的”：
    自我排除只作用于本端，零 env 依赖（WorkBuddy 审计建议）。"""
    extra = ["--self-agent", agent] if agent else []
    if kind in ("local", "local_timeout"):
        e = {"type": "local", "command": [PY, SERVER] + extra}
        if kind == "local_timeout":
            e["enabled"] = True; e["timeout"] = 15000
        return e
    return {"command": PY, "args": [SERVER] + extra}


def _nav_get(store, keys):
    cur = store
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _nav_set(store, keys):
    cur = store
    for k in keys:
        cur = cur.setdefault(k, {})
    return cur


def _backup(path: Path):
    if path.exists():
        BACKUP_DIR = AM_DIR / ".agentmem-backup"
        BACKUP_DIR.mkdir(exist_ok=True)
        dst = BACKUP_DIR / f"{path.name}.{int(time.time())}.bak"
        shutil.copy2(path, dst)
        return dst
    return None


def merge_json(cfg_path: Path, keys, entry, dry=True, legacy=None):
    """往 JSON 配置的 keys 容器里合并 MARK 条目。返回 (status, old, new)。
    legacy: 旧版条目（无 --self-agent）；命中则原地升级，不算冲突。"""
    store = {}
    if cfg_path.exists():
        try:
            store = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            return ("parse_error", None, entry)
    container = _nav_set(store, keys) if isinstance(_nav_get(store, keys[:-1]) or {}, dict) else _nav_set(store, keys)
    old = container.get(MARK) if isinstance(container, dict) else None
    if old is not None and old == entry:
        return ("already", old, entry)
    if old is not None and legacy is not None and old == legacy:
        if not dry:
            _backup(cfg_path)
            container[MARK] = entry
            cfg_path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
        return ("written", old, entry)
    if old is not None and old != entry:
        return ("conflict", old, entry)
    if not dry:
        _backup(cfg_path)
        container[MARK] = entry
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    return ("written", old, entry)


def unmerge_json(cfg_path: Path, keys, dry=True):
    if not cfg_path.exists():
        return ("absent", None)
    store = json.loads(cfg_path.read_text(encoding="utf-8"))
    container = _nav_get(store, keys)
    if not isinstance(container, dict) or MARK not in container:
        return ("none", None)
    old = container.pop(MARK)
    if not dry:
        _backup(cfg_path)
        cfg_path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    return ("removed", old)


# ── codex: TOML 文本块 ──────────────────────────────────
def _toml_block(agent=""):
    extra = f", '--self-agent', '{agent}'" if agent else ""
    return (f'\n[mcp_servers.{MARK}]\ntype = "stdio"\n'
            f'command = \'{PY}\'\nargs = [\'{SERVER}\'{extra}]\nenabled = true\n')


def merge_codex(cfg_path: Path, dry=True, agent="codex"):
    text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
    if f"[mcp_servers.{MARK}]" in text:
        legacy = f"args = ['{SERVER}']"
        if agent and legacy in text:
            if not dry:
                _backup(cfg_path)
                cfg_path.write_text(text.replace(legacy, f"args = ['{SERVER}', '--self-agent', '{agent}']"),
                                    encoding="utf-8")
            return ("written", _toml_block(agent))
        return ("already", None)
    if not dry:
        _backup(cfg_path); cfg_path.parent.mkdir(parents=True, exist_ok=True)
        with cfg_path.open("a", encoding="utf-8") as f:
            f.write(_toml_block(agent))
    return ("written", _toml_block(agent))


def unmerge_codex(cfg_path: Path, dry=True):
    if not cfg_path.exists():
        return ("absent", None)
    lines = cfg_path.read_text(encoding="utf-8").splitlines(keepends=True)
    out, skip = [], False
    for ln in lines:
        if ln.strip().startswith(f"[mcp_servers.{MARK}]"):
            skip = True; continue
        if skip and ln.strip().startswith("["):
            skip = False
        if skip:
            continue
        out.append(ln)
    if not dry:
        _backup(cfg_path); cfg_path.write_text("".join(out), encoding="utf-8")
    return ("removed", None)


# ── dsh: cordis.patch.yml 文本块 ────────────────────────
def _dsh_block(agent="dsh"):
    extra = f", '--self-agent', '{agent}'" if agent else ""
    return (f"\n- insert:\n    - id: mcp-{MARK}\n      name: '@deepseek-ai/dsh-mcp-client'\n"
            f"      config:\n        serverName: {MARK}\n        transport: stdio\n"
            f"        command: '{PY}'\n        args: ['{SERVER}'{extra}]\n")


def merge_dsh(cfg_path: Path, dry=True, agent="dsh"):
    text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
    if f"id: mcp-{MARK}" in text:
        legacy = f"args: ['{SERVER}']"
        if agent and legacy in text:
            if not dry:
                _backup(cfg_path)
                cfg_path.write_text(text.replace(legacy, f"args: ['{SERVER}', '--self-agent', '{agent}']"),
                                    encoding="utf-8")
            return ("written", _dsh_block(agent))
        return ("already", None)
    if not dry:
        _backup(cfg_path); cfg_path.parent.mkdir(parents=True, exist_ok=True)
        with cfg_path.open("a", encoding="utf-8") as f:
            f.write(_dsh_block(agent))
    return ("written", _dsh_block(agent))


def unmerge_dsh(cfg_path: Path, dry=True):
    if not cfg_path.exists():
        return ("absent", None)
    lines = cfg_path.read_text(encoding="utf-8").splitlines(keepends=True)
    out, skip = [], False
    for ln in lines:
        if f"id: mcp-{MARK}" in ln:
            skip = True; out.pop() if out and out[-1].strip() == "- insert:" else None; continue
        if skip and (ln.startswith("- ") or ln.startswith("- insert")):
            skip = False; out.append(ln); continue
        if not skip:
            out.append(ln)
    if not dry:
        _backup(cfg_path); cfg_path.write_text("".join(out), encoding="utf-8")
    return ("removed", None)


def resolve_target(name):
    if name == "codex":
        return Path.home() / ".codex/config.toml"
    if name == "dsh":
        for c in ["desktop", "web", "default"]:
            p = Path.home() / f".dsh/profiles/{c}/cordis.patch.yml"
            if p.exists():
                return p
        return Path.home() / ".dsh/profiles/desktop/cordis.patch.yml"
    cand, default, _, _ = JSON_CLIENTS[name]
    for c in cand:
        p = Path.home() / c
        if p.exists():
            return p
    return Path.home() / default


def show(name, target, status, old, new):
    icon = {"written": "→ 将新增", "already": "✓ 已存在(等价,跳过)",
            "conflict": "⚠ 已存在但不同(不覆盖,请先手动检查)",
            "removed": "→ 将移除", "none": "· 无该条目", "absent": "· 配置不存在",
            "parse_error": "✗ 配置解析失败,跳过"}[status]
    print(f"[{name}] {target}\n   {icon}")
    if status in ("written", "conflict") and new is not None:
        print("   条目:", json.dumps(new, ensure_ascii=False))


def cmd_connect(args):
    targets = list(JSON_CLIENTS) + ["codex", "dsh"] if args.all else \
              ([args.client] if args.client else [])
    if not targets:
        print("指定 --client <name> 或 --all"); return 1
    for name in targets:
        if name not in JSON_CLIENTS and name not in ("codex", "dsh"):
            print(f"未知客户端: {name}"); continue
        target = resolve_target(name)
        if name == "codex":
            st, extra = merge_codex(target, dry=not args.yes, agent="codex")
            show(name, target, st, None, extra)
        elif name == "dsh":
            st, extra = merge_dsh(target, dry=not args.yes, agent="dsh")
            show(name, target, st, None, extra)
        else:
            _, _, keys, kind = JSON_CLIENTS[name]
            agent = CLIENT_AGENT.get(name, name)
            st, old, new = merge_json(target, keys, entry_for(kind, agent),
                                      dry=not args.yes, legacy=entry_for(kind, ""))
            show(name, target, st, old, new)
    if not args.yes:
        print("\n(dry-run；加 --yes 实际写入，写入前自动备份原配置)")
    else:
        print("\n已写入。重启相应客户端生效。")
    return 0


def cmd_disconnect(args):
    name = args.client
    target = resolve_target(name)
    if name == "codex":
        st, _ = unmerge_codex(target, dry=not args.yes)
    elif name == "dsh":
        st, _ = unmerge_dsh(target, dry=not args.yes)
    else:
        _, _, keys, _k = JSON_CLIENTS[name]
        st, _ = unmerge_json(target, keys, dry=not args.yes)
    print(f"[{name}] {target} → {st}" + ("" if args.yes else " (dry-run, 加 --yes 执行)"))
    return 0


def main():
    ap = argparse.ArgumentParser(prog="connect/disconnect")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("connect"); p.add_argument("--client"); p.add_argument("--all", action="store_true")
    p.add_argument("--yes", action="store_true"); p.set_defaults(fn=cmd_connect)
    p = sub.add_parser("disconnect"); p.add_argument("--client", required=True)
    p.add_argument("--yes", action="store_true"); p.set_defaults(fn=cmd_disconnect)
    args = ap.parse_args()
    sys.exit(args.fn(args) or 0)


if __name__ == "__main__":
    main()
