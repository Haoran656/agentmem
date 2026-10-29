# -*- coding: utf-8 -*-
"""eval_ab.py — 旧检索器(HEAD) vs 工作区检索器 的同题 A/B 基线

为什么存在：改检索前必须先冻结量尺（WorkBuddy 审计建议）。本脚本用 git HEAD 的
querylib.py 建一个只读副本，在**同一批题目**上跑旧/新两条路径，输出 hit@1/3/20。

三档题目（自动从本机语料生成，含隐私，只在本地跑）：
  phrase  从会话正文抽的连续短语（旧路径甜区）
  multi   从同一会话两条素材各取一个中文词，空格连接（多词 AND）
  short   2 字中文短词（LIKE 兜底路径）

用法:
  python eval_ab.py [--n 40] [--seed 20261029]
输出: eval_ab_results.json（仅聚合数字）
"""
import argparse
import importlib.util
import json
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
AM = Path(__file__).resolve().parent
CACHE = AM / ".eval_head"
DB = AM / "agentmem.db"

CJK = re.compile(r"[\u4e00-\u9fff]{4,}")
LAT = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{5,}")
BAD = re.compile(r"[【】\[\]\"'<>{}|\\*#/]")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def prepare(base="v0.1.0"):
    """把指定基线（默认 v0.1.0 tag）的 querylib + 同库快照放到 .eval_head。
    必须钉死 ref：v0.2.0 提交后再用 HEAD 会拿成新代码，A/B 就成了自己比自己。"""
    CACHE.mkdir(exist_ok=True)
    tag = re.sub(r"[^A-Za-z0-9_.-]", "_", base)
    src = CACHE / f"querylib_{tag}.py"
    if not src.exists():
        out = subprocess.run(["git", "show", f"{base}:querylib.py"], cwd=str(AM),
                             capture_output=True, text=True, encoding="utf-8")
        if out.returncode != 0:
            raise SystemExit(f"git show {base}:querylib.py 失败：" + out.stderr[:200])
        src.write_text(out.stdout, encoding="utf-8")
        shutil.copy2(AM / "agentmem_config.py", CACHE / "agentmem_config.py")
    if not (CACHE / "agentmem.db").exists():
        shutil.copy2(DB, CACHE / "agentmem.db")
    return load("q_old", src), load("q_new", AM / "querylib.py")


def gen_questions(con, n_sessions, seed):
    rng = random.Random(seed)
    sids = [(r["agent"], r["session_id"]) for r in con.execute(
        "SELECT agent, session_id FROM sessions WHERE n_events>=8 ORDER BY RANDOM() LIMIT ?", (n_sessions,))]
    qs = []
    for agent, sid in sids:
        rows = con.execute(
            "SELECT text FROM events WHERE agent=? AND session_id=? AND kind IN ('user','assistant') "
            "AND redacted=0 AND length(text) BETWEEN 80 AND 1500 ORDER BY RANDOM() LIMIT 6",
            (agent, sid)).fetchall()
        texts = [r["text"] for r in rows]
        if not texts:
            continue
        phrase = ""
        for t in texts:
            for m in CJK.finditer(t):
                s = m.group(); ln = rng.randint(4, min(7, len(s)))
                i = rng.randint(0, len(s) - ln); w = s[i:i + ln]
                if not BAD.search(w) and len(set(w)) >= 3:
                    phrase = w; break
            if phrase:
                break
        if phrase:
            qs.append({"tier": "phrase", "q": phrase, "agent": agent, "sid": sid})
        words = []
        for t in texts:
            for m in CJK.finditer(t):
                s = m.group()
                if len(s) >= 4:
                    w = s[rng.randint(0, len(s) - 3):][:3]
                    if not BAD.search(w) and w not in words:
                        words.append(w)
        if len(words) >= 2:
            qs.append({"tier": "multi", "q": f"{words[0]} {words[1]}", "agent": agent, "sid": sid})
        shorts = [m.group()[:2] for t in texts for m in CJK.finditer(t)]
        if shorts:
            qs.append({"tier": "short", "q": rng.choice(shorts), "agent": agent, "sid": sid})
    return qs


def rank(cards, agent, sid):
    for i, c in enumerate(cards):
        if c["agent"] == agent and c["session_id"] == sid:
            return i + 1
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--seed", type=int, default=20261029)
    ap.add_argument("--base", default="v0.1.0", help="旧代码基线 git ref（默认 v0.1.0）")
    args = ap.parse_args()

    q_old, q_new = prepare(args.base)
    con_ro = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con_ro.row_factory = sqlite3.Row
    con_old, con_new = q_old.connect_ro(), q_new.connect_ro()
    qs = gen_questions(con_ro, args.n, args.seed)
    if not qs:
        print("语料不足"); return 1

    res = {t: {"old": [], "new": []} for t in ("phrase", "multi", "short")}
    t_old = t_new = 0.0
    for it in qs:
        t0 = time.perf_counter()
        rows = q_old.build_search(con_old, it["q"], limit=20)
        cards = q_old.group_by_session(rows, 20)
        t_old += time.perf_counter() - t0
        res[it["tier"]]["old"].append(rank(cards, it["agent"], it["sid"]))

        t0 = time.perf_counter()
        cards, _, _ = q_new.search_sessions(con_new, it["q"], limit=20, include_live=True)
        t_new += time.perf_counter() - t0
        res[it["tier"]]["new"].append(rank(cards, it["agent"], it["sid"]))

    def stat(rs):
        n = len(rs) or 1
        return {"n": len(rs),
                "hit@1": round(100 * sum(r == 1 for r in rs) / n, 1),
                "hit@3": round(100 * sum(0 < r <= 3 for r in rs) / n, 1),
                "recall@20": round(100 * sum(0 < r <= 20 for r in rs) / n, 1)}

    out = {"corpus": {"sessions": con_ro.execute("SELECT count(*) c FROM sessions").fetchone()["c"],
                      "events": con_ro.execute("SELECT count(*) c FROM events").fetchone()["c"]},
           "seed": args.seed, "base": args.base, "questions": len(qs),
           "ms_per_query": {"old": round(t_old * 1000 / len(qs), 1), "new": round(t_new * 1000 / len(qs), 1)},
           "tiers": {}}
    print(f"语料 {out['corpus']['sessions']} 会话 / {out['corpus']['events']} 事件 | 题目 {len(qs)} | "
          f"单题 {out['ms_per_query']['old']}ms → {out['ms_per_query']['new']}ms\n")
    print(f"{'tier':<8}{'n':>4}  {'hit@1 旧→新':<20}{'hit@3 旧→新':<20}{'recall@20 旧→新'}")
    for tier in ("phrase", "multi", "short"):
        so, sn = stat(res[tier]["old"]), stat(res[tier]["new"])
        out["tiers"][tier] = {"old": so, "new": sn}
        print(f"{tier:<8}{so['n']:>4}  {so['hit@1']:>6}% → {sn['hit@1']:>6}%    "
              f"{so['hit@3']:>6}% → {sn['hit@3']:>6}%    {so['recall@20']:>6}% → {sn['recall@20']:>6}%")
    (AM / "eval_ab_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n聚合结果 -> eval_ab_results.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
