"""브라우저 체험판 데이터 만들기 — web/data/ 에 parquet(테이블)과 cases.json(질문·SQL·정답)을 쓴다.

브라우저는 DuckDB-WASM으로 이 SQL들을 그 자리에서 다시 실행한다(재생인 것: LLM이 쓴 SQL·의도 / 계산인 것: 실행 결과).
?selftest=1 이면 정답 SQL 82개를 브라우저에서 돌려 여기 적어 둔 파이썬 결과와 대조한다.
사용: python scripts/build_web.py --tag q7b_final
"""
import argparse
import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from fda import data as D  # noqa: E402
from fda.questions import build_questions, split  # noqa: E402
from fda.semantic import compile_query  # noqa: E402
from fda.verifier import check  # noqa: E402

WEB = ROOT / "web" / "data"


def ser(rows):
    return [[(v if isinstance(v, (int, float, str)) or v is None else str(v)) for v in r] for r in rows[:50]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="q7b_final")
    args = ap.parse_args()
    WEB.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(ROOT / "data" / "factory.duckdb"), read_only=True)
    tables = [t for (t,) in con.execute("SELECT table_name FROM information_schema.tables ORDER BY 1").fetchall()]
    for t in tables:
        cols = con.execute(f"SELECT column_name, data_type FROM information_schema.columns WHERE table_name='{t}' "
                           f"ORDER BY ordinal_position").fetchall()
        sel = ", ".join(f"CAST({c} AS TIMESTAMP) AS {c}" if ty.startswith("TIMESTAMP") else c for c, ty in cols)
        con.execute(f"COPY (SELECT {sel} FROM {t}) TO '{(WEB / f'{t}.parquet').as_posix()}' (FORMAT PARQUET)")

    runs = {}
    for s in ("s0", "s1", "s1v", "s2"):
        for tag in (args.tag, args.tag.replace("final", "v4") if s == "s2" else args.tag.replace("final", "v2")):
            for split_tag in (tag,):
                p = ROOT / "results" / "runs" / f"{split_tag}_{s}.jsonl"
                if p.exists():
                    for l in p.open(encoding="utf-8"):
                        r = json.loads(l)
                        runs.setdefault(r["qid"], {})[s] = {
                            "sql": r.get("sql"), "first_sql": (r.get("steps") or [{}])[0].get("sql"),
                            "flags": [f["msg"] for f in (r.get("steps") or [{}])[0].get("flags") or []]
                            if s == "s1v" else None,
                            "abstain": r.get("abstain"), "error": r.get("error"), "intent": r.get("intent"),
                            "route": r.get("route"), "correct": r.get("correct"),
                            "py_rules": [f["rule"] for f in check(r["sql"], r["question"])] if (r.get("sql") and s != "s2") else None}
    cases = []
    for q in split(build_questions()):
        gold = con.execute(q["gold_sql"]).fetchall()
        cases.append({"qid": q["qid"], "split": q["split"], "question": q["question"], "traps": q["traps"],
                      "kind": q["kind"], "gold_sql": q["gold_sql"], "gold": ser(gold),
                      "semantic_sql": compile_query(q["intent"]) if q["intent"]["metrics"] else None,
                      "runs": runs.get(q["qid"], {})})
    summ = ROOT / "results" / f"summary_{args.tag}_test.json"
    meta = {"tables": tables, "schema_doc": D.SCHEMA_DOC, "glossary": D.GLOSSARY,
            "summary": json.loads(summ.read_text(encoding="utf-8")) if summ.exists() else None}
    (WEB / "cases.json").write_text(json.dumps({"meta": meta, "cases": cases}, ensure_ascii=False), encoding="utf-8")
    print(len(cases), "cases,", sum(len(c["runs"]) for c in cases), "runs;",
          sum(f.stat().st_size for f in WEB.iterdir()) // 1024, "KB")


if __name__ == "__main__":
    main()
