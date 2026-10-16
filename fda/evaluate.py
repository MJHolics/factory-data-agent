"""답 대조 — "실행됐다"와 "맞았다"를 따로 센다.

규칙(측정 전에 고정):
- 숫자는 상대오차 1e-4(또는 절대 1e-6) 안이면 같다. 비율은 0.0746과 7.46(퍼센트 표기)을 같은 답으로 본다 —
  표기 차이지 틀린 답이 아니다.
- value: 결과가 한 행이고 그 행 안에 정답 값이 있으면(라벨 열이 붙어 있어도 된다).  row: 첫 행의 숫자 집합(순서 무관).  label: 정답 값이 예측 첫 행 안에 있으면.
- table: 행 수가 같고, 정답의 각 행이 예측의 서로 다른 행 하나에 모두 들어 있으면(예측에 열이 더 있어도 된다).
"""
from __future__ import annotations

import math


def _num(x):
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    try:
        import decimal
        if isinstance(x, decimal.Decimal):
            return float(x)
    except Exception:
        pass
    return None


def same(a, b):
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        if math.isclose(na, nb, rel_tol=1e-4, abs_tol=1e-6):
            return True
        if 0 <= abs(na) <= 1 and math.isclose(na * 100, nb, rel_tol=1e-4, abs_tol=1e-4):
            return True              # 퍼센트 표기
        return False
    if a is None or b is None:
        return a is None and b is None
    return str(a).strip() == str(b).strip()


def _row_contains(pred_row, gold_row):
    used = set()
    for g in gold_row:
        hit = next((j for j, p in enumerate(pred_row) if j not in used and same(g, p)), None)
        if hit is None:
            return False
        used.add(hit)
    return True


def match(gold, pred, kind="value"):
    """gold, pred: list of tuples(fetchall 결과)."""
    if pred is None:
        return False
    if not gold:
        return not pred
    if not pred:
        return False
    if kind == "value":
        return len(pred) == 1 and any(same(gold[0][0], v) for v in pred[0])
    if kind == "label":
        return any(same(gold[0][0], v) for v in pred[0])
    if kind == "row":
        return len(pred) == 1 and _row_contains(pred[0], gold[0])
    if kind == "table":
        if len(pred) != len(gold):
            return False
        left = list(range(len(pred)))
        for g in gold:
            hit = next((j for j in left if _row_contains(pred[j], g)), None)
            if hit is None:
                return False
            left.remove(hit)
        return True
    raise ValueError(kind)
