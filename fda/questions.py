"""질문 세트와 정답 SQL.

정답 SQL은 의미 계층(semantic.py)과 독립적으로 손으로 쓴다 — 의미 계층의 결과도 이 정답과 대조된다.
각 질문에는 걸려 있는 함정(traps)을 붙인다. 함정이 없는 대조군(control)도 섞어, 검증기가
멀쩡한 답을 얼마나 잘못 막는지(오경보)도 잴 수 있게 한다.

날짜 기준: 오늘 2026-09-30(수, KST), 현재 18:00 KST. 어제 09-29, 이번 주 09-28~, 지난주 09-21~09-27.
시각 컬럼은 UTC → KST 날짜는 (ts + INTERVAL 9 HOUR)::DATE.
"""
from __future__ import annotations

import hashlib

K = "INTERVAL 9 HOUR"
NOW_UTC = "TIMESTAMP '2026-09-30 09:00:00'"            # 18:00 KST
LINES = ["L1", "L2", "L3", "L4"]
LINE_KO = {"L1": "1라인", "L2": "2라인", "L3": "3라인", "L4": "4라인"}

# 기간 이름 → (KST 시작일 포함, KST 종료일 포함, 한국어)
PERIODS = {
    "yesterday": ("2026-09-29", "2026-09-29", "어제"),
    "last_week": ("2026-09-21", "2026-09-27", "지난주"),
    "sep_first_half": ("2026-09-01", "2026-09-15", "9월 1일부터 15일까지"),
    "aug": ("2026-08-01", "2026-08-31", "8월"),
}


def _kst_date(col):
    return f"({col} + {K})::DATE"


def _between(col, p):
    a, b, _ = PERIODS[p]
    return f"{_kst_date(col)} BETWEEN DATE '{a}' AND DATE '{b}'"


def _period_bounds_utc(p):
    a, b, _ = PERIODS[p]
    return (f"(TIMESTAMP '{a} 00:00:00' - {K})", f"(TIMESTAMP '{b} 00:00:00' + INTERVAL 1 DAY - {K})")


def _per(p):
    a, b, _ = PERIODS[p]
    return {"start": a, "end": b}


def build_questions():
    qs = []

    def add(template, text, gold, traps, kind="value", intent=None):
        qid = template + "-" + hashlib.md5(text.encode()).hexdigest()[:6]
        qs.append({"qid": qid, "template": template, "question": text, "gold_sql": gold.strip(),
                   "traps": traps, "kind": kind, "intent": intent or {"metrics": []}})

    # T1 라인 생산량(기간) — 시간대·취소
    for ln in LINES:
        for p in ("yesterday", "last_week", "aug"):
            add("T1_output", f"{PERIODS[p][2]} {LINE_KO[ln]} 생산량은 몇 개야?",
                f"""SELECT SUM(p.good_qty) FROM production_log p JOIN work_orders w ON p.wo_id=w.wo_id
                    WHERE w.line_id='{ln}' AND w.status<>'CANCELLED' AND {_between('p.ts', p)}""",
                ["timezone", "cancelled"], intent={"metrics": ["output"], "line": ln, "period": _per(p)})

    # T2 라인 불량률(기간) — 합계의 비율·취소·시간대
    for ln in LINES:
        for p in ("last_week", "sep_first_half"):
            add("T2_scrap_rate", f"{PERIODS[p][2]} {LINE_KO[ln]} 불량률은?",
                f"""SELECT SUM(p.scrap_qty)::DOUBLE / SUM(p.good_qty + p.scrap_qty) FROM production_log p
                    JOIN work_orders w ON p.wo_id=w.wo_id
                    WHERE w.line_id='{ln}' AND w.status<>'CANCELLED' AND {_between('p.ts', p)}""",
                ["cancelled", "ratio_of_sums", "timezone", "metric_definition"],
                intent={"metrics": ["scrap_rate"], "line": ln, "period": _per(p)})

    # T3 조별 생산량 — 야간조 근무일
    for d, dko in (("2026-09-29", "어제"), ("2026-09-24", "9월 24일"), ("2026-09-12", "9월 12일")):
        for sh in ("A", "C"):
            add("T3_shift_output", f"{dko} {sh}조 생산량 합계는?",
                f"""SELECT SUM(p.good_qty) FROM production_log p JOIN work_orders w ON p.wo_id=w.wo_id
                    WHERE w.shift='{sh}' AND w.status<>'CANCELLED' AND {_kst_date('w.start_ts')} = DATE '{d}'""",
                ["shift_boundary", "timezone", "cancelled"],
                intent={"metrics": ["output"], "shift": sh, "shift_date": d})

    # T4 생산량과 검사 불량률을 한 번에 — 조인 부풀림
    for p in ("last_week", "aug"):
        add("T4_output_and_insp", f"{PERIODS[p][2]} 라인별 생산량과 검사 불량률을 같이 보여줘",
            f"""WITH o AS (SELECT w.line_id, SUM(p.good_qty) AS output FROM production_log p JOIN work_orders w ON p.wo_id=w.wo_id
                          WHERE w.status<>'CANCELLED' AND {_between('p.ts', p)} GROUP BY 1),
                    i AS (SELECT w.line_id, SUM(i.defect_count)::DOUBLE / SUM(i.sample_size) AS insp_rate
                          FROM inspections i JOIN work_orders w ON i.wo_id=w.wo_id
                          WHERE w.status<>'CANCELLED' AND {_between('i.ts', p)} GROUP BY 1)
                SELECT o.line_id, o.output, i.insp_rate FROM o JOIN i USING(line_id) ORDER BY 1""",
            ["fanout", "cancelled", "timezone"], kind="table",
            intent={"metrics": ["output", "insp_defect_rate"], "period": _per(p), "group_by": "line"})
    for ln in LINES:
        add("T4_output_and_insp", f"지난주 {LINE_KO[ln]}의 생산량과 검사한 샘플 수는 각각 얼마야?",
            f"""SELECT (SELECT SUM(p.good_qty) FROM production_log p JOIN work_orders w ON p.wo_id=w.wo_id
                        WHERE w.line_id='{ln}' AND w.status<>'CANCELLED' AND {_between('p.ts', 'last_week')}),
                       (SELECT SUM(i.sample_size) FROM inspections i JOIN work_orders w ON i.wo_id=w.wo_id
                        WHERE w.line_id='{ln}' AND w.status<>'CANCELLED' AND {_between('i.ts', 'last_week')})""",
            ["fanout", "cancelled", "timezone"], kind="row",
            intent={"metrics": ["output", "insp_samples"], "line": ln, "period": _per("last_week")})

    # T5 라인 가동률 — 계획 정지 제외·기간 클리핑·진행 중 정지
    for ln in LINES:
        for p in ("last_week", "sep_first_half"):
            s, e = _period_bounds_utc(p)
            add("T5_availability", f"{PERIODS[p][2]} {LINE_KO[ln]} 가동률은?",
                f"""SELECT 1 - SUM(GREATEST(0, epoch(LEAST(COALESCE(d.end_ts, {NOW_UTC}), {e}) - GREATEST(d.start_ts, {s}))))
                        / (COUNT(DISTINCT q.eq_id) * epoch({e} - {s}))
                    FROM equipment q LEFT JOIN downtime d ON d.eq_id=q.eq_id AND NOT d.planned
                         AND d.start_ts < {e} AND COALESCE(d.end_ts, {NOW_UTC}) > {s}
                    WHERE q.line_id='{ln}'""",
                ["planned_downtime", "period_clip", "null_end", "timezone"],
                intent={"metrics": ["availability"], "line": ln, "period": _per(p)})

    # T6 현재 재고(EA) — BOX 환산·반품 부호
    for m in ("M01", "M07", "M12", "M15", "M18"):
        add("T6_stock", f"자재 {m} 현재 재고는 몇 EA야?",
            f"""SELECT SUM(CASE t.txn_type WHEN 'RECEIPT' THEN 1 WHEN 'ADJUST' THEN 1 ELSE -1 END
                           * t.qty * CASE WHEN t.unit='BOX' THEN m.pack_size ELSE 1 END)
                FROM inventory_txn t JOIN materials m ON t.material_id=m.material_id WHERE t.material_id='{m}'""",
            ["unit_box", "return_sign"], intent={"metrics": ["stock_ea"], "material": m})

    # T7 기간 입고량(EA) — BOX 환산·시간대
    for m in ("M02", "M07", "M09", "M16"):
        for p in ("last_week", "aug"):
            add("T7_receipts", f"{PERIODS[p][2]} 자재 {m} 입고량은 몇 EA야?",
                f"""SELECT SUM(t.qty * CASE WHEN t.unit='BOX' THEN m.pack_size ELSE 1 END)
                    FROM inventory_txn t JOIN materials m ON t.material_id=m.material_id
                    WHERE t.material_id='{m}' AND t.txn_type='RECEIPT' AND {_between('t.ts', p)}""",
                ["unit_box", "timezone"], intent={"metrics": ["receipts_ea"], "material": m, "period": _per(p)})

    # T8 제품군별 불량률 — 비율의 평균 함정
    for p in ("last_week", "sep_first_half", "aug"):
        add("T8_family_scrap", f"{PERIODS[p][2]} 제품군별 불량률을 알려줘",
            f"""SELECT pr.family, SUM(p.scrap_qty)::DOUBLE / SUM(p.good_qty + p.scrap_qty)
                FROM production_log p JOIN work_orders w ON p.wo_id=w.wo_id JOIN products pr ON w.product_id=pr.product_id
                WHERE w.status<>'CANCELLED' AND {_between('p.ts', p)} GROUP BY 1 ORDER BY 1""",
            ["ratio_of_sums", "cancelled", "timezone"], kind="table",
            intent={"metrics": ["scrap_rate"], "period": _per(p), "group_by": "family"})

    # T9 비계획 정지가 가장 긴 설비 — 계획 정지 제외(일요일 PM 240분이 1등을 바꾼다)
    for p in ("last_week", "sep_first_half", "aug"):
        s, e = _period_bounds_utc(p)
        add("T9_top_downtime", f"{PERIODS[p][2]} 비계획 정지 시간이 가장 길었던 설비는?",
            f"""SELECT d.eq_id FROM downtime d WHERE NOT d.planned AND d.start_ts < {e} AND COALESCE(d.end_ts, {NOW_UTC}) > {s}
                GROUP BY 1 ORDER BY SUM(epoch(LEAST(COALESCE(d.end_ts, {NOW_UTC}), {e}) - GREATEST(d.start_ts, {s}))) DESC LIMIT 1""",
            ["planned_downtime", "timezone", "period_clip"], kind="label",
            intent={"metrics": ["top_downtime_equipment"], "period": _per(p), "include_planned": False})
        add("T9_top_downtime", f"{PERIODS[p][2]} 정지 시간이 가장 길었던 설비는? 예방정비 포함해서.",
            f"""SELECT d.eq_id FROM downtime d WHERE d.start_ts < {e} AND COALESCE(d.end_ts, {NOW_UTC}) > {s}
                GROUP BY 1 ORDER BY SUM(epoch(LEAST(COALESCE(d.end_ts, {NOW_UTC}), {e}) - GREATEST(d.start_ts, {s}))) DESC LIMIT 1""",
            ["timezone", "period_clip"], kind="label",
            intent={"metrics": ["top_downtime_equipment"], "period": _per(p), "include_planned": True})

    # T10 BOM 소요량 — BOX 환산·올림
    for prod, qty, mat in (("P01", 500, "M12"), ("P02", 1200, "M12"), ("P05", 800, None), ("P09", 300, None)):
        if mat is None:
            add("T10_bom", f"{prod} {qty}개 만들려면 자재가 각각 몇 EA 필요해?",
                f"""SELECT b.material_id, b.qty_per_unit * {qty} FROM bom b WHERE b.product_id='{prod}' ORDER BY 1""",
                ["control"], kind="table", intent={"metrics": ["bom_requirement_ea"], "product": prod, "qty": qty})
        else:
            add("T10_bom", f"{prod} {qty}개 만들려면 {mat}를 몇 박스 주문해야 해? (박스 단위로 올림)",
                f"""SELECT CEIL(b.qty_per_unit * {qty}::DOUBLE / m.pack_size) FROM bom b JOIN materials m ON b.material_id=m.material_id
                    WHERE b.product_id='{prod}' AND b.material_id='{mat}'""",
                ["unit_box"], intent={"metrics": ["bom_boxes"], "product": prod, "material": mat, "qty": qty})

    # T11 대조군 — 함정 없음(시각 무관·단일 테이블)
    for ln in LINES:
        add("T11_control", f"{LINE_KO[ln]}에 설비가 몇 대 있어?",
            f"SELECT COUNT(*) FROM equipment WHERE line_id='{ln}'", ["control"])
    add("T11_control", "제품군이 몇 개야?", "SELECT COUNT(DISTINCT family) FROM products", ["control"])
    add("T11_control", "박스 하나에 가장 많이 들어가는 자재의 팩 크기는?", "SELECT MAX(pack_size) FROM materials", ["control"])
    for st in ("CANCELLED", "RUNNING"):
        add("T11_control", f"상태가 {st}인 작업지시는 몇 건이야?",
            f"SELECT COUNT(*) FROM work_orders WHERE status='{st}'", ["control"])
    for ln in LINES:
        add("T11_control", f"{LINE_KO[ln]}에서 만드는 제품은 몇 종류야? (작업지시 기준)",
            f"SELECT COUNT(DISTINCT product_id) FROM work_orders WHERE line_id='{ln}'", ["control"])

    # T12 검사 판정 비율 — 시간대·취소 무관(검사는 취소 WO에 없음)
    for ln in LINES:
        add("T12_fail_share", f"지난주 {LINE_KO[ln]} 검사 중 FAIL 판정 비율은?",
            f"""SELECT AVG(CASE WHEN i.result='FAIL' THEN 1.0 ELSE 0 END) FROM inspections i JOIN work_orders w ON i.wo_id=w.wo_id
                WHERE w.line_id='{ln}' AND {_between('i.ts', 'last_week')}""",
            ["timezone"], intent={"metrics": ["fail_share"], "line": ln, "period": _per("last_week")})

    return qs


# 하네스를 디버깅하다 결과를 본 test 문제 — 보고용 test에서 빼고 dev로 옮긴다(10-14).
SEEN_IN_DEBUG = {"T1_output-74bc6d", "T1_output-d1716b"}


def split(qs, dev_frac=0.35, seed=0):
    """템플릿 안에서 층화해 dev/test를 나눈다. 판단·튜닝은 dev에서만."""
    import random
    rnd = random.Random(seed)
    by_t = {}
    for q in qs:
        by_t.setdefault(q["template"], []).append(q)
    for t, items in by_t.items():
        rnd.shuffle(items)
        k = max(1, round(len(items) * dev_frac))
        for i, q in enumerate(items):
            q["split"] = "dev" if (i < k or q["qid"] in SEEN_IN_DEBUG) else "test"
    return qs


if __name__ == "__main__":
    import collections
    import duckdb
    from pathlib import Path
    qs = split(build_questions())
    con = duckdb.connect(str(Path(__file__).resolve().parent.parent / "data" / "factory.duckdb"), read_only=True)
    bad = 0
    for q in qs:
        try:
            r = con.execute(q["gold_sql"]).fetchall()
            if not r or r[0][0] is None:
                print("EMPTY", q["qid"], q["question"]); bad += 1
        except Exception as e:
            print("ERR", q["qid"], e); bad += 1
    print(len(qs), "questions,", bad, "bad;", collections.Counter(q["split"] for q in qs),
          collections.Counter(q["template"] for q in qs))
