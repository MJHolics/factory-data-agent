"""의미 계층 — 지표 정의를 한곳에 두고, LLM은 "무엇을·어떤 조건으로"만 고른다.

LLM이 SQL 조인을 직접 짜지 않으므로 함정 대부분이 구조적으로 사라진다:
- UTC→KST 날짜 변환, 취소 작업지시 제외, BOX→EA 환산, 반품 부호, 계획 정지 제외, 기간 클리핑은 컴파일러가 넣는다.
- 지표를 여러 개 물으면 지표마다 따로 집계한 뒤 그룹 키로 붙인다(1:N 조인 부풀림이 생길 수 없다).

LLM 출력(JSON):
{"metrics": ["output", ...], "line": "L3"|null, "period": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}|null,
 "shift": "A"|"B"|"C"|null, "shift_date": "YYYY-MM-DD"|null, "material": "M12"|null, "product": "P01"|null,
 "qty": 500|null, "group_by": "line"|"family"|null, "include_planned": false}
정의된 지표로 답할 수 없는 질문이면 {"metrics": []} → 자유 SQL 경로로 넘긴다.
"""
from __future__ import annotations

K = "INTERVAL 9 HOUR"
NOW_UTC = "TIMESTAMP '2026-09-30 09:00:00'"

METRICS = {
    "output": "생산량(양품 수량 합). 라인·기간·조(shift+shift_date)·group_by(line|family)",
    "scrap_rate": "불량률(용어집 기본 = 공정 불량률, 스크랩/(양품+스크랩)). 라인·기간·group_by(line|family)",
    "insp_defect_rate": "검사 불량률(질문에 '검사'가 들어갈 때만) = 불량 수/샘플 수. 라인·기간·group_by(line)",
    "insp_samples": "검사한 샘플 수 합. 라인·기간",
    "fail_share": "검사 중 FAIL 판정 비율. 라인·기간",
    "availability": "라인 가동률. 라인·기간(필수)",
    "stock_ea": "자재 현재 재고(EA). material(필수)",
    "receipts_ea": "자재 입고량(EA). material·기간",
    "top_downtime_equipment": "정지 시간이 가장 긴 설비. 기간, include_planned(예방정비 포함 여부)",
    "bom_requirement_ea": "제품 qty개에 드는 자재별 소요량(EA). product, qty",
    "bom_boxes": "제품 qty개에 드는 특정 자재의 박스 수(올림). product, material, qty",
}


def _kst_between(col, period):
    return f"({col} + {K})::DATE BETWEEN DATE '{period['start']}' AND DATE '{period['end']}'"


def _bounds(period):
    s = f"(TIMESTAMP '{period['start']} 00:00:00' - {K})"
    e = f"(TIMESTAMP '{period['end']} 00:00:00' + INTERVAL 1 DAY - {K})"
    return s, e


def _wo_filters(q, alias="w"):
    f = [f"{alias}.status <> 'CANCELLED'"]
    if q.get("line"):
        f.append(f"{alias}.line_id = '{q['line']}'")
    if q.get("shift"):
        f.append(f"{alias}.shift = '{q['shift']}'")
    if q.get("shift_date"):
        f.append(f"({alias}.start_ts + {K})::DATE = DATE '{q['shift_date']}'")
    return f


def _group(q):
    g = q.get("group_by")
    if g == "line":
        return "w.line_id", ""
    if g == "family":
        return "pr.family", " JOIN products pr ON w.product_id = pr.product_id"
    return None, ""


def _prod_metric(q, expr, name):
    key, join = _group(q)
    where = _wo_filters(q)
    if q.get("period") and not q.get("shift_date"):
        where.append(_kst_between("p.ts", q["period"]))
    sel = f"{key} AS k, " if key else ""
    gb = " GROUP BY 1" if key else ""
    return (f"SELECT {sel}{expr} AS {name} FROM production_log p JOIN work_orders w ON p.wo_id = w.wo_id{join} "
            f"WHERE {' AND '.join(where)}{gb}")


def _insp_metric(q, expr, name):
    key, join = _group(q)
    where = _wo_filters(q)
    if q.get("period"):
        where.append(_kst_between("i.ts", q["period"]))
    sel = f"{key} AS k, " if key else ""
    gb = " GROUP BY 1" if key else ""
    return (f"SELECT {sel}{expr} AS {name} FROM inspections i JOIN work_orders w ON i.wo_id = w.wo_id{join} "
            f"WHERE {' AND '.join(where)}{gb}")


def metric_sql(m, q):
    if m == "output":
        return _prod_metric(q, "SUM(p.good_qty)", m)
    if m == "scrap_rate":
        return _prod_metric(q, "SUM(p.scrap_qty)::DOUBLE / SUM(p.good_qty + p.scrap_qty)", m)
    if m == "insp_defect_rate":
        return _insp_metric(q, "SUM(i.defect_count)::DOUBLE / SUM(i.sample_size)", m)
    if m == "insp_samples":
        return _insp_metric(q, "SUM(i.sample_size)", m)
    if m == "fail_share":
        return _insp_metric(q, "AVG(CASE WHEN i.result = 'FAIL' THEN 1.0 ELSE 0 END)", m)
    if m == "availability":
        s, e = _bounds(q["period"])
        return (f"SELECT 1 - SUM(GREATEST(0, epoch(LEAST(COALESCE(d.end_ts, {NOW_UTC}), {e}) - GREATEST(d.start_ts, {s})))) "
                f"/ (COUNT(DISTINCT q.eq_id) * epoch({e} - {s})) AS availability "
                f"FROM equipment q LEFT JOIN downtime d ON d.eq_id = q.eq_id AND NOT d.planned "
                f"AND d.start_ts < {e} AND COALESCE(d.end_ts, {NOW_UTC}) > {s} WHERE q.line_id = '{q['line']}'")
    if m == "stock_ea":
        return ("SELECT SUM(CASE t.txn_type WHEN 'RECEIPT' THEN 1 WHEN 'ADJUST' THEN 1 ELSE -1 END "
                "* t.qty * CASE WHEN t.unit = 'BOX' THEN m.pack_size ELSE 1 END) AS stock_ea "
                f"FROM inventory_txn t JOIN materials m ON t.material_id = m.material_id WHERE t.material_id = '{q['material']}'")
    if m == "receipts_ea":
        where = [f"t.material_id = '{q['material']}'", "t.txn_type = 'RECEIPT'"]
        if q.get("period"):
            where.append(_kst_between("t.ts", q["period"]))
        return ("SELECT SUM(t.qty * CASE WHEN t.unit = 'BOX' THEN m.pack_size ELSE 1 END) AS receipts_ea "
                f"FROM inventory_txn t JOIN materials m ON t.material_id = m.material_id WHERE {' AND '.join(where)}")
    if m == "top_downtime_equipment":
        s, e = _bounds(q["period"])
        planned = "" if q.get("include_planned") else "NOT d.planned AND "
        return (f"SELECT d.eq_id FROM downtime d WHERE {planned}d.start_ts < {e} AND COALESCE(d.end_ts, {NOW_UTC}) > {s} "
                f"GROUP BY 1 ORDER BY SUM(epoch(LEAST(COALESCE(d.end_ts, {NOW_UTC}), {e}) - GREATEST(d.start_ts, {s}))) DESC LIMIT 1")
    if m == "bom_requirement_ea":
        return (f"SELECT b.material_id, b.qty_per_unit * {int(q['qty'])} AS need_ea FROM bom b "
                f"WHERE b.product_id = '{q['product']}' ORDER BY 1")
    if m == "bom_boxes":
        return (f"SELECT CEIL(b.qty_per_unit * {int(q['qty'])}::DOUBLE / m.pack_size) AS boxes FROM bom b "
                f"JOIN materials m ON b.material_id = m.material_id "
                f"WHERE b.product_id = '{q['product']}' AND b.material_id = '{q['material']}'")
    raise KeyError(m)


PERIOD_NAMES = {
    "today": ("2026-09-30", "2026-09-30"), "yesterday": ("2026-09-29", "2026-09-29"),
    "this_week": ("2026-09-28", "2026-09-30"), "last_week": ("2026-09-21", "2026-09-27"),
    "this_month": ("2026-09-01", "2026-09-30"), "last_month": ("2026-08-01", "2026-08-31"),
}


def normalize(q: dict) -> dict:
    """v2(10-14, dev 결과로 결정): LLM이 채운 의도를 정의에 맞게 고친다.
    - period_name → 날짜(코드가 계산)  - 조 없는 shift_date → 그날 하루 기간
    - 형식이 틀린 line·shift·material·product는 버린다(전체로)."""
    import re
    q = dict(q)
    if q.get("period_name") in PERIOD_NAMES and not (q.get("period") or {}).get("start"):   # 명시한 날짜가 우선(v4)
        a, b = PERIOD_NAMES[q["period_name"]]
        q["period"] = {"start": a, "end": b}
    if q.get("shift_date") and not q.get("shift"):
        q["period"] = q.get("period") or {"start": q["shift_date"], "end": q["shift_date"]}
        q["shift_date"] = None
    for key, pat in (("line", r"L[1-4]"), ("shift", r"[ABC]"), ("material", r"M\d\d"), ("product", r"P\d\d")):
        if q.get(key) is not None and not re.fullmatch(pat, str(q[key])):
            q[key] = None
    return q


def compile_query(q: dict) -> str:
    """여러 지표면 지표마다 따로 집계해 그룹 키로 붙인다(부풀림 없음)."""
    q = normalize(q)
    ms = q.get("metrics") or []
    if not ms:
        raise ValueError("no metric")
    for m in ms:
        if m not in METRICS:
            raise KeyError(m)
    if len(ms) == 1:
        return metric_sql(ms[0], q)
    parts = [f"m{i} AS ({metric_sql(m, q)})" for i, m in enumerate(ms)]
    if q.get("group_by"):
        cols = ", ".join(f"m{i}.{m}" for i, m in enumerate(ms))
        joins = " ".join(f"FULL JOIN m{i} USING (k)" for i in range(1, len(ms)))
        return f"WITH {', '.join(parts)} SELECT k, {cols} FROM m0 {joins} ORDER BY k"
    cols = ", ".join(f"(SELECT {m} FROM m{i})" for i, m in enumerate(ms))
    return f"WITH {', '.join(parts)} SELECT {cols}"


def describe() -> str:
    return "\n".join(f"- {k}: {v}" for k, v in METRICS.items())


def missing_conditions(question: str, q: dict) -> list[str]:
    """v4(10-14, dev 결과로 결정): 질문에 있는 조건이 의도에서 빠졌는지 본다(빠지면 답하지 않고 넘긴다).
    재선택 단계에서 모델이 라인·기간을 통째로 지운 사례(dev 2건)가 계기."""
    import re
    miss = []
    if re.search(r"[1-4]\s*라인", question) and not q.get("line"):
        miss.append("line")
    if re.search(r"어제|오늘|이번\s*주|지난주|이번\s*달|지난달|\d+월", question) and not (
            q.get("period") or q.get("period_name") or q.get("shift_date")):
        miss.append("period")
    if re.search(r"M\d\d", question) and not q.get("material"):
        miss.append("material")
    return miss
