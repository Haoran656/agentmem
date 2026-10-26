# -*- coding: utf-8 -*-
"""recall_eval.py — 检索召回自评（hit@k / MRR / 延迟）

原理：从本机语料自动生成问答集——从会话正文里抽"有区分度的短语"作 query，
该会话即标准答案；再用与 MCP 面完全相同的检索路径（build_search→group_by_session，
detail=index 档，limit=20）测目标会话在返回卡片里的排名。

两级题目：
  unique    短语全库只命中 1 个会话（考"找得到"）
  ambiguous 短语命中 2-3 个会话（考"排得前"）

隐私：题目短语属于用户语料，只允许本地 --dump 查看；默认输出
eval_results.json 仅含聚合数字，可公开。

用法：
  python recall_eval.py --run [--n 10]      # 每端 10 题，全端跑，打印+落聚合
  python recall_eval.py --run --dump        # 另存题目明细 eval_questions.local.json（勿入库）
"""
import argparse
import json
import random
import re
import statistics
import time
from pathlib import Path

import querylib

CJK = re.compile(r"[\u4e00-\u9fff]{5,}")
LAT = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{5,}")
BAD_CHARS = re.compile(r"[【】\[\]\"'<>{}|\\*`#/]")
STOP_LAT = {
    "https", "false", "true", "null", "class", "function", "return", "string",
    "number", "import", "export", "default", "const", "python", "controller",
    "components", "response", "requests", "encoding", "datetime", "settings",
    "platform", "desktop", "sessions", "message", "messages", "content",
    "agents", "system", "assistant", "reminder", "context", "environment",
    "workspace", "history", "directory", "template", "registry",
}


def candidates(text, rng):
    out = []
    for m in CJK.finditer(text):
        s = m.group()
        n = rng.randint(4, min(8, len(s)))
        i = rng.randint(0, len(s) - n)
        w = s[i:i + n]
        if not BAD_CHARS.search(w) and len(set(w)) >= 3:
            out.append(w)
    for m in LAT.finditer(text):
        w = m.group()
        if w.lower() not in STOP_LAT and not BAD_CHARS.search(w) and not w.isdigit():
            out.append(w)
    return out


def session_df(con, phrase):
    rows = querylib.build_search(con, phrase, limit=60)
    return {(r["agent"], r["session_id"]) for r in rows}


def make_questions(con, per_agent, seed):
    rng = random.Random(seed)
    agents = [r["agent"] for r in con.execute(
        "SELECT agent FROM sessions GROUP BY agent HAVING count(*)>=2 ORDER BY agent")]
    questions = []
    for ag in agents:
        sids = [r["session_id"] for r in con.execute(
            "SELECT session_id FROM sessions WHERE agent=? AND n_events>=6 "
            "ORDER BY RANDOM() LIMIT 60", (ag,))]
        rng.shuffle(sids)
        got = {"unique": 0, "ambiguous": 0}
        want = {"unique": max(1, per_agent * 3 // 5), "ambiguous": per_agent - want_dummy(per_agent)}
        for sid in sids:
            if got["unique"] >= want["unique"] and got["ambiguous"] >= want["ambiguous"]:
                break
            evs = [r["text"] for r in con.execute(
                "SELECT text FROM events WHERE agent=? AND session_id=? "
                "AND kind IN ('user','assistant') AND redacted=0 "
                "AND length(text) BETWEEN 14 AND 2000 ORDER BY RANDOM() LIMIT 10", (ag, sid))]
            tried = 0
            for t in evs:
                for ph in candidates(t, rng):
                    tried += 1
                    if tried > 40:
                        break
                    df = session_df(con, ph)
                    if (ag, sid) not in df:
                        continue
                    if len(df) == 1 and got["unique"] < want["unique"]:
                        questions.append({"query": ph, "agent": ag, "session_id": sid, "tier": "unique"})
                        got["unique"] += 1
                    elif 2 <= len(df) <= 3 and got["ambiguous"] < want["ambiguous"]:
                        questions.append({"query": ph, "agent": ag, "session_id": sid, "tier": "ambiguous"})
                        got["ambiguous"] += 1
                    if got["unique"] >= want["unique"] and got["ambiguous"] >= want["ambiguous"]:
                        break
                if tried > 40:
                    break
    return questions


def want_dummy(per_agent):
    return max(1, per_agent * 2 // 5)


def run_eval(questions):
    con = querylib.connect_ro()
    ranks, tiers, latencies = [], [], []
    try:
        for q in questions:
            t0 = time.perf_counter()
            rows = querylib.build_search(con, q["query"], limit=20)
            cards = querylib.group_by_session(rows, 20)
            latencies.append((time.perf_counter() - t0) * 1000)
            rank = 0
            for i, c in enumerate(cards):
                if c["agent"] == q["agent"] and c["session_id"] == q["session_id"]:
                    rank = i + 1
                    break
            ranks.append(rank)
            tiers.append(q["tier"])
    finally:
        con.close()
    n = len(questions)

    def score(sel):
        sub = [r for r, t in zip(ranks, tiers) if (not sel or t == sel)]
        m = len(sub) or 1
        return {
            "questions": len(sub),
            "hit@1": round(100 * sum(r == 1 for r in sub) / m, 1),
            "hit@3": round(100 * sum(0 < r <= 3 for r in sub) / m, 1),
            "hit@5": round(100 * sum(0 < r <= 5 for r in sub) / m, 1),
            "hit@20": round(100 * sum(0 < r <= 20 for r in sub) / m, 1),
            "recall@20": round(100 * sum(r > 0 for r in sub) / m, 1),
            "mrr": round(statistics.mean([1 / r if r else 0 for r in sub]), 3),
        }
    return {
        "overall": score(None),
        "unique": score("unique"),
        "ambiguous": score("ambiguous"),
        "latency_ms": {"median": round(statistics.median(latencies), 1),
                        "p95": round(sorted(latencies)[max(0, int(len(latencies)*0.95)-1)], 1)},
        "_ranks": ranks, "_tiers": tiers,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    con = querylib.connect_ro()
    corpus = {
        "date": time.strftime("%Y-%m-%d"),
        "sessions": con.execute("SELECT count(*) c FROM sessions").fetchone()["c"],
        "events": con.execute("SELECT count(*) c FROM events").fetchone()["c"],
        "by_agent": {r["agent"]: r["c"] for r in con.execute(
            "SELECT agent, count(*) c FROM sessions GROUP BY 1")},
    }
    questions = make_questions(con, args.n, args.seed)
    con.close()
    if not questions:
        print("未生成题目（语料太小？加 --n 或先 sync）")
        return 1
    res = run_eval(questions)
    ranks, tiers = res.pop("_ranks"), res.pop("_tiers")
    print(f"语料: {corpus['sessions']} 会话 / {corpus['events']} 事件  |  题目: {len(questions)}\n")
    for name in ("overall", "unique", "ambiguous"):
        s = res[name]
        print(f"{name:>9}  n={s['questions']:>3}  hit@1={s['hit@1']:>5}%  "
              f"hit@3={s['hit@3']:>5}%  hit@5={s['hit@5']:>5}%  hit@20={s['hit@20']:>5}%  "
              f"recall@20={s['recall@20']}%  MRR={s['mrr']}")
    print(f"延迟: median {res['latency_ms']['median']}ms / p95 {res['latency_ms']['p95']}ms")
    miss = [(q["query"], q["agent"], q["tier"], r) for q, r in zip(questions, ranks) if r == 0]
    print(f"\n完全未召回 {len(miss)} 题" + (f"（前 8，短语仅本地打印）: {[m[1]+'/'+m[2] for m in miss[:8]]}" if miss else ""))
    out = {"corpus": corpus, "questions_per_agent_target": args.n, "seed": args.seed, **res}
    Path("eval_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("聚合结果 -> eval_results.json（仅数字，可公开）")
    if args.dump:
        Path("eval_questions.local.json").write_text(json.dumps(questions, ensure_ascii=False, indent=1), encoding="utf-8")
        print("题目明细 -> eval_questions.local.json（含语料短语，已 gitignore，勿提交）")
    return 0


if __name__ == "__main__":
    main()
