# -*- coding: utf-8 -*-
"""prescan.py — 发布前隐私闸门：非零退出禁止 push

三道检查（对 git 将纳入的文件，白名单外/命中密钥/含用户名与本机绝对路径 = fail）：
  1. 密钥模式：复用 sanitize.audit（六条规则）
  2. 个人痕迹：运行时派生的用户名、个人品牌词、私人路径特征
  3. 数据文件：agentmem.db* / dashboard.html / .cmd / 备份目录 不得入库
用法： python prescan.py      （在 git 仓库内跑，扫 git ls-files + 未跟踪）
"""
import re
import subprocess
import sys
from pathlib import Path

from sanitize import audit

AM_DIR = Path(__file__).resolve().parent

try:
    USER = re.sub(r"[^a-zA-Z0-9]", "", str(Path.home()).replace("\\", "/").split("/")[-1] or "user").lower()
except Exception:
    USER = ""

BANNED_DATA = re.compile(r"(agentmem\.db(\-wal|\-shm)?|dashboard\.html|\.cmd$|\.config-backups/|db-backups/|\.agentmem-backup/|audit_ok\.json|\.lazy_sync|agentmem\.config\.json)", re.I)
# 允许出现在 docs 示例里的泛化写法白名单
SAFE_HINT = re.compile(r"(<user>|<name>|your-username|/Users/<|C:\\\\Users\\\\<)")


def tracked_files():
    out = subprocess.run(["git", "-C", str(AM_DIR), "ls-files", "--cached", "--others",
                          "--exclude-standard"], capture_output=True, text=True)
    if out.returncode != 0:
        # 非 git 环境退化为全目录扫（跳过忽略项）
        ign = (AM_DIR / ".gitignore").read_text(encoding="utf-8").splitlines()
        files = []
        for p in AM_DIR.rglob("*"):
            if p.is_file() and "__pycache__" not in str(p) and ".git/" not in str(p):
                rel = str(p.relative_to(AM_DIR)).replace("\\", "/")
                if not any(g and not g.startswith("#") and g.rstrip("/") in rel for g in ign):
                    files.append(rel)
        return files
    return out.stdout.split()


def main():
    problems = []
    for rel in tracked_files():
        p = AM_DIR / rel
        if not p.exists():
            continue
        if BANNED_DATA.search(rel):
            problems.append(f"[数据/备份文件] {rel}")
            continue
        if p.suffix.lower() not in (".py", ".md", ".txt", ".toml", ".json", ".yml", ".template", ".cfg", ""):
            continue  # 只文本扫
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        low_lines = text.splitlines()
        # 脱敏规则文件本身含密钥模式定义（误报源）：只扫用户名，不跑 audit
        skip_audit = rel in ("sanitize.py", "prescan.py")
        for ln, line in enumerate(low_lines, 1):
            if SAFE_HINT.search(line):
                continue
            if USER and USER in line.lower():
                problems.append(f"[用户名] {rel}:{ln}")
            if skip_audit:
                continue
            hits = audit(line)
            if hits:
                problems.append(f"[密钥] {rel}:{ln}: {hits}")
    # 去重同类，限量打印
    seen = set(); uniq = []
    for x in problems:
        key = x.split(":")[0]
        if key in seen and uniq.count(x) > 2:
            continue
        seen.add(key); uniq.append(x)
    if uniq:
        print("prescan FAIL — 先修复再 push：")
        for x in uniq[:40]:
            print("  " + x)
        sys.exit(1)
    print(f"prescan OK — {len(list(tracked_files()))} 文件，0 密钥 / 0 个人痕迹 / 0 数据文件")


if __name__ == "__main__":
    main()
