"""질문 전체를 방식별로 돌려 결과를 저장한다(질문 단위로 즉시 저장 — 중간에 죽어도 이어 간다).

사용(WSL에서 vLLM이 8000 포트로 떠 있어야 한다):
    python scripts/run_eval.py --model Qwen/Qwen2.5-7B-Instruct-AWQ --tag q7b --systems s0 s1 s1v s2
"""
import argparse
import json
import sys
from pathlib import Path

import duckdb
from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from fda.agent import Runner  # noqa: E402
from fda.evaluate import match  # noqa: E402
from fda.questions import build_questions, split  # noqa: E402

OUT = ROOT / "results" / "runs"


def ser(rows):
    if rows is None:
        return None
    return [[(v if isinstance(v, (int, float, str)) or v is None else str(v)) for v in r] for r in rows[:50]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--systems", nargs="+", default=["s0", "s1", "s1v", "s2"])
    ap.add_argument("--base-url", default="http://localhost:8000/v1")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    args = ap.parse_args()

    con = duckdb.connect(str(ROOT / "data" / "factory.duckdb"), read_only=True)
    runner = Runner(con, OpenAI(base_url=args.base_url, api_key="local"), args.model)
    qs = [q for q in split(build_questions()) if args.split == "all" or q["split"] == args.split][: args.limit]
    OUT.mkdir(parents=True, exist_ok=True)
    for sysname in args.systems:
        path = OUT / f"{args.tag}_{sysname}.jsonl"
        done = {json.loads(l)["qid"] for l in path.open(encoding="utf-8")} if path.exists() else set()
        ok = n = 0
        with path.open("a", encoding="utf-8") as f:
            for q in qs:
                if q["qid"] in done:
                    continue
                r = getattr(runner, sysname)(q)
                gold = con.execute(q["gold_sql"]).fetchall()
                executed = r.get("error") is None and not r.get("abstain")
                correct = executed and match(gold, r["rows"], q["kind"])
                rec = {"qid": q["qid"], "template": q["template"], "split": q["split"], "traps": q["traps"],
                       "question": q["question"], "kind": q["kind"], "system": sysname, "model": args.model,
                       "sql": r.get("sql"), "rows": ser(r.get("rows")), "gold": ser(gold), "error": r.get("error"),
                       "abstain": bool(r.get("abstain")), "flags": r.get("flags"), "route": r.get("route"),
                       "intent": r.get("intent"), "latency": round(r["latency"], 3), "correct": bool(correct),
                       "steps": r.get("steps")}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                n += 1
                ok += correct
                print(f"[{args.tag}/{sysname}] {q['qid']:<28} {'O' if correct else ('?' if rec['abstain'] else ('E' if rec['error'] else 'X'))}",
                      flush=True)
        print(f"== {args.tag}/{sysname}: {ok}/{n} correct (this run)", flush=True)


if __name__ == "__main__":
    main()
