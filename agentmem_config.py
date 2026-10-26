# -*- coding: utf-8 -*-
"""agentmem_config.py — 发布版配置（兼容铁律：config 不存在 = 完全等同内置默认行为）

agentmem.config.json（与代码同目录，个人版不入 VCS）：
{
  "version": 1,
  "timezone": "+8 hours",
  "agents": { "workbuddy": {"enabled": true, "root": null}, ... },
  "optional": { "token_report_db": null }
}
解析优先级：环境变量 AGENTMEM_* > config > 内置默认。
"""
import json
import os
import re
from pathlib import Path

AM_DIR = Path(__file__).resolve().parent
CONFIG_PATH = AM_DIR / "agentmem.config.json"

# 内置默认 = 六端历史行为（Path.home() 标准子路径）
DEFAULT_ROOTS = {
    "workbuddy": ".workbuddy/projects",
    "codex":     ".codex/sessions",
    "claude":    ".claude/projects",
    "kilo":      ".local/share/kilo/kilo.db",
    "opencode":  ".local/share/opencode/opencode.db",
    "dsh":       ".dsh/sessions",
}
DEFAULT_TZ = "+8 hours"

_cache = None


def load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    cfg = {}
    try:
        if CONFIG_PATH.exists():
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass  # 坏配置按无配置处理（行为=默认，不炸）
    _cache = cfg
    return cfg


def reload_():
    global _cache
    _cache = None
    return load()


_TZ_OK = re.compile(r"^([+-]\d{1,2} (hours?|minutes?)|UTC|localtime)$")


def tz() -> str:
    """SQLite 日期修饰符；格式校验防坏配置注入 SQL。非法回落默认。"""
    v = os.environ.get("AGENTMEM_TZ") or load().get("timezone") or DEFAULT_TZ
    return v if _TZ_OK.match(v) else DEFAULT_TZ


def root(agent: str) -> Path:
    """该端存储根路径：env AGENTMEM_ROOT_<AGENT> > config root > 默认。"""
    env = os.environ.get(f"AGENTMEM_ROOT_{agent.upper().replace('-', '_')}")
    if env:
        return Path(env)
    a = (load().get("agents") or {}).get(agent) or {}
    if a.get("root"):
        return Path(a["root"])
    d = DEFAULT_ROOTS.get(agent)
    if not d:
        return None  # 未知端由 parser 自带 root 逻辑（_template 约定）
    return Path.home() / d


def enabled(agent: str) -> bool:
    a = (load().get("agents") or {}).get(agent)
    if a is None:
        return True  # 未配置该端 = 默认启用（老行为：全端尝试，源不在自然跳过）
    return bool(a.get("enabled", True))


def agents_enabled() -> list:
    keys = set(DEFAULT_ROOTS) | set((load().get("agents") or {}))
    return sorted(k for k in keys if enabled(k))


def optional(key: str, default=None):
    return (load().get("optional") or {}).get(key, default)
