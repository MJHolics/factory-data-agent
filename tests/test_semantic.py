"""의미 계층 컴파일러가 정답 의도(intent)를 받으면 정답 SQL과 같은 결과를 내는가."""
from pathlib import Path

import duckdb
import pytest

from fda.evaluate import match
from fda.questions import build_questions
from fda.semantic import compile_query

DB = Path(__file__).resolve().parent.parent / "data" / "factory.duckdb"
QS = [q for q in build_questions() if q["intent"]["metrics"]]


@pytest.fixture(scope="module")
def con():
    return duckdb.connect(str(DB), read_only=True)


@pytest.mark.parametrize("q", QS, ids=[q["qid"] for q in QS])
def test_oracle_intent_matches_gold(con, q):
    gold = con.execute(q["gold_sql"]).fetchall()
    pred = con.execute(compile_query(q["intent"])).fetchall()
    assert match(gold, pred, q["kind"]), (gold, pred)
