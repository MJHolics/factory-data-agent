"""생성된 SQL을 실행 전·후에 검사한다 — "실행은 되는데 틀린" 모양을 찾는다.

정적 규칙(sqlglot 파스 트리)은 데이터 사전·용어집에 적힌 규칙을 SQL 구조로 옮긴 것이다.
질문 문장은 규칙이 예외를 판단할 때만 쓴다(예: "예방정비 포함"이면 계획 정지 규칙을 끈다).
규칙마다 이름을 붙여, 평가에서 어느 규칙이 오답을 잡고 어느 규칙이 정답을 잘못 막는지 따로 센다.
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

CHILD = {"production_log", "inspections"}          # work_orders 1:N
TS_COLS = {"ts", "start_ts", "end_ts"}


def _tables_in_select(sel):
    """이 SELECT가 직접 FROM/JOIN 하는 테이블(서브쿼리 안은 제외)."""
    out = []
    frm = sel.args.get("from_") or sel.args.get("from")
    srcs = ([frm.this] if frm is not None else []) + [j.this for j in sel.args.get("joins") or []]
    for s in srcs:
        if isinstance(s, exp.Table):
            out.append(s.name.lower())
    return out


def _has_agg(sel):
    return any(isinstance(n, (exp.Sum, exp.Count, exp.Avg)) for n in sel.find_all(exp.AggFunc)
               if n.find_ancestor(exp.Select) is sel)


def check(sql: str, question: str = "") -> list[dict]:
    flags = []
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception as e:                        # 파스 실패는 실행 단계가 잡는다
        return [{"rule": "parse", "msg": f"SQL 파싱 실패: {e}"}]
    low = sql.lower()
    tables = {t.name.lower() for t in tree.find_all(exp.Table)}

    # R1 1:N 부풀림 — 한 SELECT에서 실적·검사를 함께 조인하고 집계
    for sel in tree.find_all(exp.Select):
        ts = _tables_in_select(sel)
        if len(CHILD & set(ts)) >= 2 and _has_agg(sel):
            flags.append({"rule": "fanout", "msg": "production_log와 inspections를 한 SELECT에서 조인해 집계합니다. "
                          "작업지시 1건에 둘 다 여러 행이라 합계가 부풀려집니다. 각각 따로 집계한 뒤 붙이세요."})
            break

    # R2 취소 작업지시 — 실적/검사/작업지시를 집계하는데 status 조건이 없음
    if "status" not in low and ("production_log" in tables or ("work_orders" in tables and re.search(r"sum\s*\(", low))):
        flags.append({"rule": "cancelled", "msg": "취소된 작업지시(status='CANCELLED')를 빼는 조건이 없습니다."})

    # R3 시간대 — 시각 컬럼을 날짜·시각과 비교하는데 KST(+9시간) 변환이 없음
    uses_ts = any(c.name.lower() in TS_COLS for c in tree.find_all(exp.Column))
    has_date_lit = bool(re.search(r"'\d{4}-\d{2}-\d{2}", sql)) or "current_date" in low or "now()" in low
    if uses_ts and has_date_lit and not re.search(r"interval\s*'?\s*-?\s*9\s*'?\s*hours?|[+-]\s*interval|at\s+time\s+zone|'hours?'\s*,\s*-?\s*9|kst|'\+09", low):
        flags.append({"rule": "timezone", "msg": "시각 컬럼은 UTC입니다. 날짜 조건은 KST(+9시간)로 바꿔서 비교하세요."})

    # R4 단위 — 입출고 수량을 합하는데 BOX 환산이 없음
    if "inventory_txn" in tables and re.search(r"sum\s*\(", low) and not ("pack_size" in low and "box" in low):
        flags.append({"rule": "unit_box", "msg": "unit='BOX'인 행은 qty×pack_size가 EA입니다. 환산 없이 합했습니다."})

    # R5 반품 부호 — 재고를 구하는데 txn_type별 부호 처리가 없음
    if "inventory_txn" in tables and re.search(r"sum\s*\(", low) and "txn_type" not in low:
        flags.append({"rule": "txn_sign", "msg": "ISSUE·RETURN은 빼고 RECEIPT·ADJUST는 더해야 합니다. txn_type 처리가 없습니다."})

    # R6 계획 정지 — 정지 시간을 다루는데 planned 조건이 없음(질문이 예방정비 포함을 요구하면 예외)
    if "downtime" in tables and "planned" not in low and not re.search(r"예방정비\s*포함|계획\s*정지\s*포함", question):
        flags.append({"rule": "planned", "msg": "계획 정지(planned=true, 예방정비)를 빼는 조건이 없습니다."})

    # R7 진행 중 정지 — end_ts를 쓰는데 NULL 처리가 없음
    if "downtime" in tables and "end_ts" in low and "coalesce" not in low and "is null" not in low and "ifnull" not in low:
        flags.append({"rule": "null_end", "msg": "진행 중인 정지는 end_ts가 NULL입니다. 현재 시각으로 채우세요."})

    # R8 비율의 평균 — AVG 안에 나눗셈
    for a in tree.find_all(exp.Avg):
        if a.find(exp.Div):
            flags.append({"rule": "avg_of_ratio", "msg": "비율을 평균했습니다. 합계끼리 나눠야 합니다."})
            break

    # R9 야간조 — 조를 묻는데 날짜를 실적 시각으로 거름(조의 근무일은 작업지시 시작일)
    if re.search(r"[ABC]\s*조", question) and "shift" in low and re.search(r"\b(p|production_log)\.ts\b", low):
        flags.append({"rule": "shift_date", "msg": "조의 근무일은 작업지시 시작일(start_ts, KST)입니다. 실적 시각 날짜로 거르면 야간조가 갈립니다."})
    return flags


def dynamic_check(rows) -> list[dict]:
    if rows is None:
        return []
    if not rows or all(v is None for v in rows[0]):
        return [{"rule": "empty", "msg": "결과가 비었습니다(NULL). 조건·날짜를 다시 확인하세요."}]
    return []
