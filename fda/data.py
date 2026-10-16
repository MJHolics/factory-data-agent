"""가상 공장(MES·ERP 일부) 데이터 생성 — 결정적(seed 고정).

질의 에이전트가 "실행은 되는데 틀린 답"을 내는 지점은 실제 MES 데이터에서 반복되는 모양을 따랐다.
일부러 넣은 함정(모두 컬럼 설명·용어집에는 적혀 있다 — 숨긴 규칙이 아니다):

1. 시각은 전부 UTC로 저장한다. 공장은 KST(+9). "오늘·어제·이번 주"는 KST 기준.
2. 야간조 C는 22:00~06:00(KST)라 자정을 넘는다. 조의 "근무일"은 조가 시작한 날이다.
3. 작업지시(work_orders) 1건에 생산 실적(production_log)과 검사(inspections)가 각각 여러 건 → 둘을 한 번에 조인해 합하면 부풀려진다.
4. 취소된 작업지시(status='CANCELLED')에도 실적 행이 남아 있다(취소 전 시운전분). 집계에서 빼야 한다.
5. 자재 입출고(inventory_txn)는 단위가 EA와 BOX가 섞여 있다. BOX는 materials.pack_size개.
6. 설비 정지(downtime)는 계획 정지(planned=true, 예방정비)와 비계획 정지가 섞여 있다. 가동률은 비계획만 뺀다.
7. 진행 중인 정지·작업지시는 end_ts가 NULL이다.
8. 불량률은 두 가지가 있다: 공정 불량률(scrap / (good+scrap), 실적 기준)과 검사 불량률(defect_count / sample_size, 샘플 기준).
   용어집에 "불량률"은 공정 불량률로 정의한다.

데이터 안의 사건(분석 질문의 정답이 되는 원인):
- 3라인(L3) 프레스 E07이 기준일 기준 지난주 비계획 정지가 늘었고, 같은 주에 자재 M12의 새 공급처 로트가 들어와 L3 스크래치 불량이 늘었다.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
KST = dt.timedelta(hours=9)

# 기준일(데이터의 "오늘", KST). 질문의 상대 날짜는 이 날을 기준으로 정답을 만든다.
TODAY_KST = dt.date(2026, 9, 30)
NOW_KST = dt.datetime(2026, 9, 30, 18, 0)
DAYS = 91
SHIFTS = {"A": 6, "B": 14, "C": 22}          # KST 시작 시각, 각 8시간


def _rng(seed=7):
    return np.random.default_rng(seed)


def build(seed: int = 7) -> dict[str, pd.DataFrame]:
    rng = _rng(seed)
    start_kst = dt.datetime.combine(TODAY_KST - dt.timedelta(days=DAYS - 1), dt.time(0, 0))

    lines = pd.DataFrame({
        "line_id": ["L1", "L2", "L3", "L4"],
        "line_name": ["1라인 조립", "2라인 조립", "3라인 프레스", "4라인 도장"],
        "process": ["ASSEMBLY", "ASSEMBLY", "PRESS", "PAINT"],
    })
    equipment = pd.DataFrame({
        "eq_id": [f"E{i:02d}" for i in range(1, 13)],
        "line_id": ["L1"] * 3 + ["L2"] * 3 + ["L3"] * 3 + ["L4"] * 3,
        "eq_name": ["로봇암", "체결기", "비전검사기"] * 2 + ["프레스", "피더", "금형교환기"] + ["도장부스", "건조로", "컨베이어"],
    })
    equipment.loc[equipment.eq_id == "E07", "eq_name"] = "프레스"  # L3 첫 설비

    products = pd.DataFrame({
        "product_id": [f"P{i:02d}" for i in range(1, 13)],
        "product_name": [f"브래킷-{c}" for c in "ABCD"] + [f"커버-{c}" for c in "ABCD"] + [f"하우징-{c}" for c in "ABCD"],
        "family": ["BRACKET"] * 4 + ["COVER"] * 4 + ["HOUSING"] * 4,
    })
    prod_line = {p: ("L3" if i < 4 else "L1" if i < 6 else "L2" if i < 8 else "L4" if i < 10 else "L1")
                 for i, p in enumerate(products.product_id)}

    materials = pd.DataFrame({
        "material_id": [f"M{i:02d}" for i in range(1, 21)],
        "material_name": [f"강판-{i}" for i in range(1, 7)] + [f"볼트-{i}" for i in range(1, 7)]
                         + [f"도료-{i}" for i in range(1, 5)] + [f"포장재-{i}" for i in range(1, 5)],
        "base_unit": ["EA"] * 20,
        "pack_size": [50] * 6 + [500] * 6 + [20] * 4 + [100] * 4,   # BOX 1개 = pack_size EA
    })
    materials.loc[materials.material_id == "M12", "material_name"] = "볼트-6(신규 공급처 로트 포함)"

    bom_rows = []
    for i, p in enumerate(products.product_id):
        mats = rng.choice(materials.material_id, size=3, replace=False).tolist()
        if prod_line[p] == "L3" and "M12" not in mats:
            mats[0] = "M12"
        for m in mats:
            bom_rows.append({"product_id": p, "material_id": m, "qty_per_unit": int(rng.integers(1, 5))})
    bom = pd.DataFrame(bom_rows)

    # ---- 작업지시·실적·검사
    wo_rows, log_rows, insp_rows = [], [], []
    wo_n = log_n = insp_n = 0
    event_week_start = TODAY_KST - dt.timedelta(days=TODAY_KST.weekday() + 7)   # 지난주 월요일(KST)
    event_days = {event_week_start + dt.timedelta(days=d) for d in range(7)}
    for d in range(DAYS):
        day = (start_kst + dt.timedelta(days=d)).date()
        for p in products.product_id:
            if rng.random() > 0.55:
                continue
            line = prod_line[p]
            shift = rng.choice(list(SHIFTS))
            s_kst = dt.datetime.combine(day, dt.time(SHIFTS[shift], 0)) + dt.timedelta(minutes=int(rng.integers(0, 60)))
            planned = int(rng.integers(200, 900))
            wo_n += 1
            wo_id = f"WO{wo_n:05d}"
            r = rng.random()
            status = "CANCELLED" if r < 0.06 else "DONE"
            e_kst = s_kst + dt.timedelta(hours=8)
            wo_rows.append({"wo_id": wo_id, "product_id": p, "line_id": line, "shift": shift,
                            "planned_qty": planned, "status": status,
                            "start_ts": s_kst - KST, "end_ts": e_kst - KST})
            n_logs = 1 if status == "CANCELLED" else int(rng.integers(3, 7))
            remaining = planned if status != "CANCELLED" else int(planned * 0.08)
            scrap_rate = 0.025 if line != "L3" else 0.03
            if line == "L3" and day in event_days:
                scrap_rate = 0.075                 # 사건 주간: L3 스크래치 증가
            for k in range(n_logs):
                q = max(1, remaining // (n_logs - k))
                remaining -= q
                scrap = int(rng.binomial(q, scrap_rate))
                log_n += 1
                ts = s_kst + dt.timedelta(minutes=int((k + 1) * 480 / (n_logs + 1)))
                log_rows.append({"log_id": log_n, "wo_id": wo_id, "ts": ts - KST,
                                 "good_qty": q - scrap, "scrap_qty": scrap})
            if status != "CANCELLED":
                for k in range(int(rng.integers(1, 4))):
                    insp_n += 1
                    sample = int(rng.choice([20, 32, 50]))
                    p_def = 0.02 if not (line == "L3" and day in event_days) else 0.08
                    defects = int(rng.binomial(sample, p_def))
                    dtype = None if defects == 0 else (
                        "SCRATCH" if (line == "L3" and day in event_days and rng.random() < 0.8)
                        else rng.choice(["SCRATCH", "DENT", "BURR", "COLOR"]))
                    result = "PASS" if defects <= 1 else ("REWORK" if defects <= 3 else "FAIL")
                    ts = s_kst + dt.timedelta(minutes=int(60 + 150 * k + rng.integers(0, 30)))
                    insp_rows.append({"insp_id": insp_n, "wo_id": wo_id, "ts": ts - KST, "sample_size": sample,
                                      "defect_count": defects, "defect_type": dtype, "result": result})
    work_orders = pd.DataFrame(wo_rows)
    production_log = pd.DataFrame(log_rows)
    inspections = pd.DataFrame(insp_rows)

    # ---- 설비 정지
    dt_rows, dt_n = [], 0
    for d in range(DAYS):
        day = (start_kst + dt.timedelta(days=d)).date()
        for eq in equipment.eq_id:
            lam = 0.25
            if eq == "E07" and day in event_days:
                lam = 2.2
            for _ in range(int(rng.poisson(lam))):
                dt_n += 1
                s = dt.datetime.combine(day, dt.time(0, 0)) + dt.timedelta(minutes=int(rng.integers(0, 1440)))
                mins = int(rng.integers(10, 120))
                dt_rows.append({"dt_id": dt_n, "eq_id": eq, "start_ts": s - KST,
                                "end_ts": s - KST + dt.timedelta(minutes=mins), "planned": False,
                                "reason_code": rng.choice(["JAM", "SENSOR", "TOOL", "MATERIAL"])})
            if day.weekday() == 6 and eq.endswith(("1", "4", "7", "0")):      # 일요일 예방정비(계획)
                dt_n += 1
                s = dt.datetime.combine(day, dt.time(8, 0))
                dt_rows.append({"dt_id": dt_n, "eq_id": eq, "start_ts": s - KST,
                                "end_ts": s - KST + dt.timedelta(minutes=240), "planned": True,
                                "reason_code": "PM"})
    # 진행 중인 정지 1건(end_ts NULL)
    dt_n += 1
    s = dt.datetime.combine(TODAY_KST, dt.time(15, 10))
    dt_rows.append({"dt_id": dt_n, "eq_id": "E07", "start_ts": s - KST, "end_ts": None,
                    "planned": False, "reason_code": "TOOL"})
    downtime = pd.DataFrame(dt_rows)

    # ---- 자재 입출고(EA·BOX 혼재)
    inv_rows, t_n = [], 0
    pack = dict(zip(materials.material_id, materials.pack_size))
    for d in range(DAYS):
        day = (start_kst + dt.timedelta(days=d)).date()
        for m in materials.material_id:
            if rng.random() < 0.25:                 # 입고는 BOX 단위가 대부분
                t_n += 1
                unit = "BOX" if rng.random() < 0.85 else "EA"
                qty = int(rng.integers(5, 40)) if unit == "BOX" else int(rng.integers(50, 600))
                inv_rows.append({"txn_id": t_n, "material_id": m, "ts": dt.datetime.combine(day, dt.time(9, 0)) - KST,
                                 "txn_type": "RECEIPT", "qty": qty, "unit": unit})
            if rng.random() < 0.6:                  # 출고는 EA
                t_n += 1
                inv_rows.append({"txn_id": t_n, "material_id": m,
                                 "ts": dt.datetime.combine(day, dt.time(13, 0)) - KST,
                                 "txn_type": "ISSUE", "qty": int(rng.integers(20, 300)), "unit": "EA"})
            if rng.random() < 0.03:                 # 반품(공급처로, 재고 감소)·실사 조정(±)
                t_n += 1
                inv_rows.append({"txn_id": t_n, "material_id": m, "ts": dt.datetime.combine(day, dt.time(17, 0)) - KST,
                                 "txn_type": rng.choice(["RETURN", "ADJUST"]),
                                 "qty": int(rng.integers(-30, 30)), "unit": "EA"})
    inventory_txn = pd.DataFrame(inv_rows)
    # 반품은 항상 양수로 기록(부호는 txn_type이 정한다), 조정만 부호를 가진다
    m_ret = inventory_txn.txn_type == "RETURN"
    inventory_txn.loc[m_ret, "qty"] = inventory_txn.loc[m_ret, "qty"].abs() + 1
    # 시작 재고(기간 첫날 ADJUST로 기록)
    for m in materials.material_id:
        t_n += 1
        inv_rows_ = {"txn_id": t_n, "material_id": m, "ts": start_kst - KST, "txn_type": "ADJUST",
                     "qty": int(rng.integers(2000, 8000)), "unit": "EA"}
        inventory_txn = pd.concat([inventory_txn, pd.DataFrame([inv_rows_])], ignore_index=True)

    # 현재 시각 이후의 기록은 없다(미래 실적 제거). 그 뒤에 시작하는 작업지시는 PLANNED로.
    now_utc = NOW_KST - KST
    production_log = production_log[production_log.ts <= now_utc].reset_index(drop=True)
    inspections = inspections[inspections.ts <= now_utc].reset_index(drop=True)
    inventory_txn = inventory_txn[inventory_txn.ts <= now_utc].reset_index(drop=True)
    downtime = downtime[downtime.start_ts <= now_utc].reset_index(drop=True)
    late = downtime.end_ts.notna() & (downtime.end_ts > now_utc)
    downtime.loc[late, "end_ts"] = None                    # 지금 진행 중
    fut = work_orders.start_ts > now_utc
    work_orders.loc[fut & (work_orders.status != "CANCELLED"), "status"] = "PLANNED"
    work_orders.loc[fut, "end_ts"] = None
    running = (work_orders.start_ts <= now_utc) & work_orders.end_ts.notna() & (work_orders.end_ts > now_utc)
    work_orders.loc[running & (work_orders.status == "DONE"), "status"] = "RUNNING"
    work_orders.loc[running, "end_ts"] = None

    return {"lines": lines, "equipment": equipment, "products": products, "materials": materials,
            "bom": bom, "work_orders": work_orders, "production_log": production_log,
            "inspections": inspections, "downtime": downtime, "inventory_txn": inventory_txn}


SCHEMA_DOC = {
    "lines": "생산 라인. line_id L1~L4.",
    "equipment": "설비. 라인에 속한다.",
    "products": "제품.",
    "materials": "자재. pack_size = BOX 1개에 든 EA 수.",
    "bom": "제품 1개당 자재 소요량(EA).",
    "work_orders": "작업지시. start_ts·end_ts는 UTC. status: PLANNED/RUNNING/DONE/CANCELLED. "
                   "CANCELLED는 집계에서 뺀다(취소 전 시운전 실적이 남아 있음). shift는 작업지시가 시작한 조. "
                   "진행 중이면 end_ts NULL.",
    "production_log": "생산 실적. 작업지시 1건에 여러 행. ts는 UTC. good_qty 양품, scrap_qty 공정 불량.",
    "inspections": "품질 검사(샘플링). 작업지시 1건에 여러 행. ts는 UTC. defect_count는 sample_size 중 불량 수. "
                   "result: PASS/REWORK/FAIL.",
    "downtime": "설비 정지. start_ts·end_ts는 UTC, 진행 중이면 end_ts NULL. planned=true는 예방정비(계획 정지).",
    "inventory_txn": "자재 입출고. ts는 UTC. unit이 BOX면 qty×materials.pack_size가 EA. "
                     "txn_type: RECEIPT(+), ISSUE(-), RETURN(-, qty는 양수로 기록), ADJUST(± 부호 그대로).",
}

GLOSSARY = [
    "공장 시간대는 KST(UTC+9). 오늘·어제·이번 주·지난주·날짜는 모두 KST 기준. 주는 월요일 시작.",
    "조(shift): A 06~14시, B 14~22시, C 22~익일 06시(KST). 조의 근무일은 조가 시작한 날.",
    "생산량 = 양품 수량(good_qty) 합계. 취소된 작업지시 제외.",
    "불량률(공정 불량률) = scrap_qty 합 / (good_qty 합 + scrap_qty 합). 취소된 작업지시 제외.",
    "검사 불량률 = defect_count 합 / sample_size 합.",
    "가동률(라인) = 1 - 라인 설비들의 비계획 정지 시간 합 / (설비 수 × 기간 전체 시간). 계획 정지(planned=true)는 빼지 않는다. "
    "기간 경계를 넘는 정지는 기간 안의 부분만, 진행 중인 정지(end_ts NULL)는 현재 시각까지로 센다.",
    "현재 시각은 2026-09-30 18:00 KST다.",
    "재고(EA) = RECEIPT·ADJUST는 더하고 ISSUE·RETURN은 뺀 합. BOX는 pack_size를 곱해 EA로.",
    "비율의 평균이 아니라 합계의 비율로 계산한다(작업지시별 불량률을 평균하지 않는다).",
]


def stress(tabs: dict) -> dict:
    """같은 값, 기록 시각만 날짜 경계 쪽으로 옮긴 두 번째 데이터.
    기본 데이터는 입고 09:00 KST(=00:00 UTC)·출고 13:00이라 시간대 변환을 빠뜨려도 날짜가 우연히 같다.
    여기서는 입고 08:00·출고 06:30·조정/반품 05:00 KST로 옮겨 UTC 날짜가 하루 앞으로 가게 한다.
    정답은 정답 SQL을 이 데이터에서 다시 돌려 얻는다 — 두 데이터에서 모두 맞아야 '맞은 SQL'로 본다."""
    t = {k: v.copy() for k, v in tabs.items()}
    inv = t["inventory_txn"]
    shift_h = {"RECEIPT": -1.0, "ISSUE": -6.5, "ADJUST": -12.0, "RETURN": -12.0}
    first = inv.groupby("material_id").ts.transform("min") == inv.ts      # 시작 재고 행은 그대로
    for tt, h in shift_h.items():
        m = (inv.txn_type == tt) & ~first
        inv.loc[m, "ts"] = inv.loc[m, "ts"] + pd.Timedelta(hours=h)
    t["inventory_txn"] = inv
    # 모든 기록을 5시간 앞당긴다(관계는 그대로, 날짜 경계에 걸리는 기록이 바뀐다)
    for name, cols in (("production_log", ["ts"]), ("inspections", ["ts"]), ("work_orders", ["start_ts", "end_ts"]),
                       ("downtime", ["start_ts", "end_ts"])):
        for c in cols:
            t[name][c] = t[name][c] - pd.Timedelta(hours=5)
    return t


def write(seed: int = 7, stressed: bool = False) -> Path:
    import duckdb
    DATA.mkdir(exist_ok=True)
    tabs = build(seed)
    if stressed:
        tabs = stress(tabs)
    db = DATA / ("factory_stress.duckdb" if stressed else "factory.duckdb")
    if db.exists():
        db.unlink()
    con = duckdb.connect(str(db))
    for name, df in tabs.items():
        con.register("df_", df)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM df_")
        con.unregister("df_")
        if not stressed:
            (DATA / "parquet").mkdir(exist_ok=True)
            con.execute(f"COPY {name} TO '{(DATA / 'parquet' / f'{name}.parquet').as_posix()}' (FORMAT PARQUET)")
    con.close()
    return db


if __name__ == "__main__":
    write(stressed=True)
    p = write()
    import duckdb
    con = duckdb.connect(str(p), read_only=True)
    for t in SCHEMA_DOC:
        print(t, con.execute(f"select count(*) from {t}").fetchone()[0])
