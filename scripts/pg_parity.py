"""같은 데이터를 PostgreSQL에 올리고, 정답 SQL과 의미 계층 SQL을 sqlglot으로 PostgreSQL 방언으로 옮겨 실행해
DuckDB 결과와 대조한다. 운영 DB(PostgreSQL)에서도 같은 답이 나오는가.

사용: python scripts/pg_parity.py [--load]
접속: host=127.0.0.1 port=5432 dbname=fda user=fda (WSL PostgreSQL 16)
"""
import argparse
import io
import json
import sys
from pathlib import Path

import duckdb
import psycopg
import sqlglot

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from fda.evaluate import match  # noqa: E402
from fda.questions import build_questions  # noqa: E402
from fda.semantic import compile_query  # noqa: E402

DSN = "host=127.0.0.1 port=5432 dbname=fda user=fda password=fda_local"
PGTYPE = {"VARCHAR": "TEXT", "BIGINT": "BIGINT", "INTEGER": "INTEGER", "DOUBLE": "DOUBLE PRECISION",
          "BOOLEAN": "BOOLEAN", "TIMESTAMP": "TIMESTAMP", "TIMESTAMP_NS": "TIMESTAMP", "DATE": "DATE"}
INDEXES = ["CREATE INDEX ON production_log (wo_id)", "CREATE INDEX ON production_log (ts)",
           "CREATE INDEX ON inspections (wo_id)", "CREATE INDEX ON inspections (ts)",
           "CREATE INDEX ON work_orders (line_id, status)", "CREATE INDEX ON downtime (eq_id, start_ts)",
           "CREATE INDEX ON inventory_txn (material_id, ts)"]


def load(duck, pg):
    tables = [t for (t,) in duck.execute("SELECT table_name FROM information_schema.tables ORDER BY 1").fetchall()]
    with pg.cursor() as cur:
        for t in tables:
            cols = duck.execute(f"SELECT column_name, data_type FROM information_schema.columns "
                                f"WHERE table_name='{t}' ORDER BY ordinal_position").fetchall()
            cur.execute(f"DROP TABLE IF EXISTS {t}")
            cur.execute(f"CREATE TABLE {t} (" + ", ".join(f"{c} {PGTYPE.get(ty, 'TEXT')}" for c, ty in cols) + ")")
            df = duck.execute(f"SELECT * FROM {t}").df()
            buf = io.StringIO()
            df.to_csv(buf, index=False, header=False, na_rep="\\N")
            buf.seek(0)
            with cur.copy(f"COPY {t} FROM STDIN WITH (FORMAT csv, NULL '\\N')") as cp:
                cp.write(buf.read())
        for s in INDEXES:
            cur.execute(s)
    pg.commit()
    return tables


def to_pg(sql):
    return sqlglot.transpile(sql, read="duckdb", write="postgres")[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--load", action="store_true")
    args = ap.parse_args()
    duck = duckdb.connect(str(ROOT / "data" / "factory.duckdb"), read_only=True)
    pg = psycopg.connect(DSN)
    if args.load:
        print("loaded:", load(duck, pg))
    res = {"gold": {"ok": 0, "fail": []}, "semantic": {"ok": 0, "fail": []}}
    for q in build_questions():
        cases = [("gold", q["gold_sql"])]
        if q["intent"]["metrics"]:
            cases.append(("semantic", compile_query(q["intent"])))
        for name, sql in cases:
            want = duck.execute(sql).fetchall()
            psql = to_pg(sql)
            try:
                with pg.cursor() as cur:
                    cur.execute(psql)
                    got = cur.fetchall()
                pg.rollback()
                ok = match(want, got, q["kind"])
                err = None if ok else f"mismatch duck={want[:2]} pg={got[:2]}"
            except Exception as e:
                pg.rollback()
                ok, err = False, str(e).splitlines()[0][:200]
            if ok:
                res[name]["ok"] += 1
            else:
                res[name]["fail"].append({"qid": q["qid"], "error": err, "pg_sql": psql[:400]})
    for name, r in res.items():
        print(f"{name}: {r['ok']} ok, {len(r['fail'])} fail")
        for f in r["fail"][:6]:
            print("   ", f["qid"], "|", f["error"])
    (ROOT / "results" / "pg_parity.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
