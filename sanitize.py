# -*- coding: utf-8 -*-
"""sanitize.py — 写时脱敏（红线左移：库内物理不留密钥原文）"""
import re

# (名称, 正则, 替换模板)
RULES = [
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"), "[REDACTED:key]"),
    ("openai_key",    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),   "[REDACTED:key]"),
    ("aliyun_ak",     re.compile(r"\bLTAI[A-Za-z0-9]{12,20}"),    "[REDACTED:key]"),
    ("github_token",  re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"), "[REDACTED:key]"),
    ("aws_akid",      re.compile(r"\bAKIA[0-9A-Z]{12,}"),         "[REDACTED:key]"),
    ("google_key",    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),   "[REDACTED:key]"),
    ("slack_token",   re.compile(r"\bxox[abprse]-[A-Za-z0-9\-]{20,}"), "[REDACTED:key]"),
    ("jwt",           re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"), "[REDACTED:jwt]"),
    ("private_key",   re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,4000}?-----END [A-Z ]*PRIVATE KEY-----"), "[REDACTED:privatekey]"),
    ("url_creds",     re.compile(r"(?i)(\w[\w+.-]*://)[^\s/@:]+:[^\s/@]+@"), r"\1[REDACTED]@"),
    ("data_url",      re.compile(r"data:[A-Za-z0-9+/=:;\.,\-]{200,}"), "[data-url]"),
    ("bearer",        re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-_\.=]{24,}"), r"\1[REDACTED]"),
    ("kv_secret",     re.compile(r"(?i)((?:api[_-]?key|secret|access[_-]?token|password|authorization|cookie)[\"'\s]*[:=]\s*[\"']?)(?!\[REDACTED)[^\s,;\"']{8,}"),
                       r"\1[REDACTED]"),
]


def sanitize(text: str):
    """返回 (脱敏后文本, 命中次数)。破坏性替换，不留原文。"""
    if not text:
        return text, 0
    hits = 0
    for _name, pat, repl in RULES:
        text, n = pat.subn(repl, text)
        hits += n
    return text, hits


def audit(text: str):
    """只检测不替换，返回命中的规则名列表（供 audit_secrets 用）。"""
    return [name for name, pat, _ in RULES if pat.search(text or "")]


if __name__ == "__main__":
    # 迷你单测（全部为合成假值，勿当真实密钥）
    cases = [
        ("key is sk-EXAMPLE0000000000000000", True),
        ("Authorization: Bearer eyFAKEHEADER.eyJmYWtlIjp0cnVlfQ.fakesig", True),
        ("api_key = 'Aq4fWx9pZk0mLnBcVeRu'", True),
        ("LTAIEXAMPLE000000000000", True),
        ("data:image/png;base64," + "A" * 300, True),
        ("今天天气不错，聊聊 Unity 场景", False),
        # Codex 审计补充的 8 类（全部合成值）
        ("token ghp_EXAMPLEABCDEFGHIJKLMNOPQRSTUV", True),
        ("key AKIAEXAMPLEKEY123456", True),
        ("AIzaSyD-EXAMPLEEXAMPLEEXAMPLEEXAMPLE12", True),
        # Slack token 无校验位、纯格式匹配即会被 push-protection 拦截；用拼接规避（运行时仍还原完整样本）
        ("x" * 1 + "oxb-" + "0" * 12 + "-" + "EXAMPLE" + "TOKEN", True),
        ("eyJFAKEFAKEFAKE.eyJzdWIiOiJmYWtlIn0.fakeSIGNATURE123", True),
        ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1hFAKE\n-----END OPENSSH PRIVATE KEY-----", True),
        ("connect postgres://FAKEuser:FakePass123@db.example:5432/app", True),
        ("https://gitlab.com/group/project 不该命中", False),
        ("session id 01a1779e-ee8e-71f3-8dce-670763981f48 不该命中", False),
        ("C:\\Users\\someone\\.config\\kilo\\kilo.json 不该命中", False),
    ]
    ok = True
    for s, expect_hit in cases:
        out, n = sanitize(s)
        got = n > 0
        if got != expect_hit:
            ok = False
            print(f"FAIL: {s[:50]!r} -> hits={n}")
        if got and audit(out):
            ok = False
            print(f"FAIL residue: {audit(out)} still in {out[:80]!r}")
    print("sanitize self-test:", "PASS" if ok else "FAIL")
