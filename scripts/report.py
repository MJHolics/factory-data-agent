"""방식별 결과 집계.

- 정답 / 그럴듯한 오답(실행되고 값도 나왔는데 틀림) / 실행 오류 / 답 보류(검증기가 막음)
- "실행 성공률"과 "정답률"의 차이 = 그럴듯한 오답률. 실행 성공만 보면 이 차이가 안 보인다.
- 함정별 정답률, 검증기 성적(S1V 첫 SQL 기준: 틀린 SQL을 몇 개 잡았나 / 맞은 SQL을 몇 개 잘못 막았나)
사용: python scripts/report.py q7b_v1 [--split dev]
"""
import argparse
import collections
import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "results" / "runs"
sys.path.insert(0, str(ROOT))
from fda.evaluate import match  # noqa: E402

_CON = None


_CON_B = None
_GOLD_B = {}


def robust_correct(r):
    """기본 데이터에서 맞았고, 기록 시각을 경계 쪽으로 옮긴 두 번째 데이터에서도 정답 SQL과 같은 답인가."""
    global _CON_B
    if not r["correct"] or not r.get("sql"):
        return False
    if _CON_B is None:
        _CON_B = duckdb.connect(str(ROOT / "data" / "factory_stress.duckdb"), read_only=True)
    from fda.questions import build_questions
    if not _GOLD_B:
        for q in build_questions():
            _GOLD_B[q["qid"]] = (_CON_B.execute(q["gold_sql"]).fetchall(), q["kind"])
    gold, kind = _GOLD_B[r["qid"]]
    try:
        rows = _CON_B.execute(r["sql"]).fetchall()
    except Exception:
        return False
    return match(gold, rows, kind)


def first_sql_correct(r):
    """재생성 전 첫 SQL을 다시 실행해 정답과 대조한다(검증기 성적용)."""
    global _CON
    if _CON is None:
        _CON = duckdb.connect(str(ROOT / "data" / "factory.duckdb"), read_only=True)
    try:
        rows = _CON.execute(r["steps"][0]["sql"]).fetchall()
    except Exception:
        return False
    if not match([tuple(g) for g in r["gold"]], rows, r["kind"]):
        return False
    return robust_correct({**r, "sql": r["steps"][0]["sql"], "correct": True})


def load(tag, system, split):
    p = RUNS / f"{tag}_{system}.jsonl"
    if not p.exists():
        return []
    rs = [json.loads(l) for l in p.open(encoding="utf-8")]
    return [r for r in rs if split == "all" or r["split"] == split]


def outcome(r):
    """정답은 두 데이터 모두에서 맞아야 한다. 기본 데이터에서만 맞은 것은 '우연히 맞음'으로 따로 센다."""
    if r["abstain"]:
        return "abstain"
    if r["error"]:
        return "error"
    if r["correct"]:
        return "correct" if robust_correct(r) else "lucky"
    return "wrong"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("--split", default="dev")
    ap.add_argument("--systems", nargs="+", default=["s0", "s1", "s1v", "s2"])
    args = ap.parse_args()
    summary = {}
    for s in args.systems:
        rs = load(args.tag, s, args.split)
        if not rs:
            continue
        c = collections.Counter(outcome(r) for r in rs)
        n = len(rs)
        executed = c["correct"] + c["lucky"] + c["wrong"]
        lat = sorted(r["latency"] for r in rs)
        row = {"n": n, **{k: c[k] for k in ("correct", "lucky", "wrong", "error", "abstain")},
               "acc": round(c["correct"] / n, 3), "exec_rate": round(executed / n, 3),
               "plausible_wrong_share_of_executed": round((c["wrong"] + c["lucky"]) / executed, 3) if executed else None,
               "naive_acc_single_db": round((c["correct"] + c["lucky"]) / n, 3),
               "latency_p50": lat[n // 2]}
        by_trap = collections.defaultdict(lambda: [0, 0])
        for r in rs:
            for t in r["traps"]:
                by_trap[t][1] += 1
                by_trap[t][0] += outcome(r) == "correct"
        row["by_trap"] = {t: f"{a}/{b}" for t, (a, b) in sorted(by_trap.items())}
        if s == "s2":
            row["routes"] = dict(collections.Counter(r["route"] for r in rs))
        if s == "s1v":                                         # 검증기 성적: 첫 SQL 기준
            tp = fp = fn = tn = 0
            rules_tp, rules_fp = collections.Counter(), collections.Counter()
            for r in rs:
                first = r["steps"][0]
                fl = [f for f in first.get("flags") or [] if f["rule"] != "exec_error"]
                first_ok = first_sql_correct(r)
                if fl and not first_ok:
                    tp += 1; rules_tp.update(f["rule"] for f in fl)
                elif fl and first_ok:
                    fp += 1; rules_fp.update(f["rule"] for f in fl)
                elif not fl and not first_ok:
                    fn += 1
                else:
                    tn += 1
            row["verifier_first_sql"] = {"caught_wrong": tp, "missed_wrong": fn, "false_alarm": fp, "passed_ok": tn,
                                         "rules_on_wrong": dict(rules_tp), "rules_on_ok": dict(rules_fp)}
        summary[s] = row
        print(f"{args.tag}/{s:<4} n={n} 정답 {c['correct']} · 우연히 맞음 {c['lucky']} · 그럴듯한 오답 {c['wrong']} · 오류 {c['error']} · 보류 {c['abstain']} "
              f"| 정답률 {row['acc']:.3f} 실행률 {row['exec_rate']:.3f} | p50 {row['latency_p50']:.2f}s")
        print("      함정별:", row["by_trap"])
        if "verifier_first_sql" in row:
            print("      검증기(첫 SQL):", row["verifier_first_sql"])
        if "routes" in row:
            print("      경로:", row["routes"])
    out = ROOT / "results" / f"summary_{args.tag}_{args.split}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
