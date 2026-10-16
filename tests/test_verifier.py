"""검증기: 정답 SQL에는 아무 규칙도 걸리지 않아야 하고(오경보 0), 함정을 밟은 SQL은 걸려야 한다."""
import pytest

from fda.questions import build_questions
from fda.verifier import check

QS = build_questions()


@pytest.mark.parametrize("q", QS, ids=[q["qid"] for q in QS])
def test_gold_sql_has_no_flags(q):
    assert check(q["gold_sql"], q["question"]) == []


@pytest.mark.parametrize("sql,rule", [
    ("SELECT SUM(p.good_qty), SUM(i.sample_size) FROM production_log p JOIN work_orders w ON p.wo_id=w.wo_id "
     "JOIN inspections i ON i.wo_id=w.wo_id WHERE w.status<>'CANCELLED' AND (p.ts + INTERVAL 9 HOUR)::DATE = DATE '2026-09-29'", "fanout"),
    ("SELECT SUM(good_qty) FROM production_log WHERE (ts + INTERVAL 9 HOUR)::DATE = DATE '2026-09-29'", "cancelled"),
    ("SELECT SUM(p.good_qty) FROM production_log p JOIN work_orders w USING(wo_id) WHERE w.status<>'CANCELLED' AND p.ts::DATE = DATE '2026-09-29'", "timezone"),
    ("SELECT SUM(qty) FROM inventory_txn WHERE material_id='M01' AND txn_type='RECEIPT'", "unit_box"),
    ("SELECT SUM(qty * CASE WHEN unit='BOX' THEN pack_size ELSE 1 END) FROM inventory_txn JOIN materials USING(material_id) WHERE material_id='M01'", "txn_sign"),
    ("SELECT eq_id FROM downtime GROUP BY 1 ORDER BY SUM(epoch(COALESCE(end_ts, now()) - start_ts)) DESC LIMIT 1", "planned"),
    ("SELECT AVG(scrap_qty::DOUBLE / (good_qty + scrap_qty)) FROM production_log p JOIN work_orders w USING(wo_id) WHERE w.status<>'CANCELLED'", "avg_of_ratio"),
])
def test_trap_sql_is_flagged(sql, rule):
    assert rule in {f["rule"] for f in check(sql, "")}
