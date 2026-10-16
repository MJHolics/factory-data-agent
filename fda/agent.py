"""네 가지 방식으로 질문에 답한다(같은 모델·같은 질문·temperature 0).

S0 schema   : DDL + 현재 시각만 주고 SQL 생성
S1 docs     : S0 + 테이블 설명·용어집(UTC 저장, 취소 제외, BOX 환산 등 규칙을 글로 줌)
S1V verify  : S1 → 검증기(정적 규칙 + 실행 결과) → 걸리면 메시지를 붙여 1회 재생성 → 그래도 걸리면 답을 보류
S2 semantic : LLM은 지표·조건 JSON만 고르고 SQL은 의미 계층이 만든다. 정의된 지표로 못 답하면 S1V로 넘긴다.
"""
from __future__ import annotations

import json
import re
import time

from . import data as D
from .semantic import compile_query, describe
from .verifier import check, dynamic_check

NOW_LINE_V1 = "현재 시각: 2026-09-30(수) 18:00 KST."
# v2(10-14, dev 결과로 결정): 상대 날짜를 코드가 미리 풀어 준다 — 모든 방식에 똑같이 준다.
CALENDAR = ("날짜 표(KST): 오늘=2026-09-30, 어제=2026-09-29, 이번 주=2026-09-28~2026-09-30, "
            "지난주=2026-09-21~2026-09-27, 이번 달=2026-09-01~2026-09-30, 지난달=2026-08-01~2026-08-31.")
NOW_LINE = NOW_LINE_V1 + " " + CALENDAR


def ddl(con) -> str:
    out = []
    for (t,) in con.execute("SELECT table_name FROM information_schema.tables ORDER BY 1").fetchall():
        cols = con.execute(f"SELECT column_name, data_type FROM information_schema.columns "
                           f"WHERE table_name='{t}' ORDER BY ordinal_position").fetchall()
        out.append(f"CREATE TABLE {t} (" + ", ".join(f"{c} {ty}" for c, ty in cols) + ");")
    return "\n".join(out)


def docs() -> str:
    return ("[테이블 설명]\n" + "\n".join(f"- {k}: {v}" for k, v in D.SCHEMA_DOC.items())
            + "\n[용어집]\n" + "\n".join(f"- {g}" for g in D.GLOSSARY))


SQL_SYS = ("너는 DuckDB SQL을 쓰는 분석가다. 질문에 답하는 SQL 한 개만 ```sql 코드블록으로 출력한다. "
           "설명은 쓰지 않는다. 결과는 질문이 묻는 값만 담는다.")


def extract_sql(text: str) -> str:
    m = re.search(r"```(?:sql)?\s*(.+?)```", text, re.S | re.I)
    s = (m.group(1) if m else text).strip()
    return s.rstrip(";").strip()


def extract_json(text: str):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


class Runner:
    def __init__(self, con, client, model, max_tokens=700):
        self.con, self.client, self.model, self.max_tokens = con, client, model, max_tokens
        self.schema = ddl(con)
        self.calls = 0

    def chat(self, system, user, history=None):
        msgs = [{"role": "system", "content": system}] + (history or []) + [{"role": "user", "content": user}]
        t = time.perf_counter()
        r = self.client.chat.completions.create(model=self.model, messages=msgs, temperature=0,
                                                max_tokens=self.max_tokens)
        self.calls += 1
        return r.choices[0].message.content, time.perf_counter() - t

    def execute(self, sql):
        try:
            return self.con.execute(sql).fetchall(), None
        except Exception as e:
            return None, str(e).splitlines()[0][:300]

    # ---- S0 / S1
    def gen_sql(self, question, with_docs):
        ctx = f"{self.schema}\n{NOW_LINE}" + (f"\n{docs()}" if with_docs else "")
        text, lat = self.chat(SQL_SYS, f"{ctx}\n\n질문: {question}")
        return extract_sql(text), text, lat

    def s0(self, q):
        sql, raw, lat = self.gen_sql(q["question"], False)
        rows, err = self.execute(sql)
        return {"sql": sql, "rows": rows, "error": err, "latency": lat, "steps": [{"sql": sql, "raw": raw}]}

    def s1(self, q):
        sql, raw, lat = self.gen_sql(q["question"], True)
        rows, err = self.execute(sql)
        return {"sql": sql, "rows": rows, "error": err, "latency": lat, "steps": [{"sql": sql, "raw": raw}]}

    # ---- S1V
    def s1v(self, q):
        ctx = f"{self.schema}\n{NOW_LINE}\n{docs()}"
        user = f"{ctx}\n\n질문: {q['question']}"
        text, lat = self.chat(SQL_SYS, user)
        sql = extract_sql(text)
        steps = [{"sql": sql, "raw": text}]
        rows, err = self.execute(sql)
        flags = check(sql, q["question"]) + ([{"rule": "exec_error", "msg": err}] if err else dynamic_check(rows))
        steps[-1]["flags"] = flags
        if flags:
            fb = "이 SQL에 문제가 있습니다:\n" + "\n".join(f"- {f['msg']}" for f in flags) + "\n고친 SQL 한 개만 출력하세요."
            text2, lat2 = self.chat(SQL_SYS, fb, history=[{"role": "user", "content": user},
                                                         {"role": "assistant", "content": text}])
            lat += lat2
            sql = extract_sql(text2)
            rows, err = self.execute(sql)
            flags = check(sql, q["question"]) + ([{"rule": "exec_error", "msg": err}] if err else dynamic_check(rows))
            steps.append({"sql": sql, "raw": text2, "flags": flags})
        abstain = bool(flags) and not err
        return {"sql": sql, "rows": None if abstain else rows, "error": err, "latency": lat,
                "abstain": abstain, "flags": flags, "steps": steps}

    # ---- S2
    def s2_v2(self, q):
        return self._s2(q, v3=False)

    def s2_v3(self, q):
        return self._s2(q, v3=True, v4=False)

    def s2(self, q):
        return self._s2(q, v3=True, v4=True)

    def _s2(self, q, v3=True, v4=True):
        """v3(10-14, dev 결과로 결정): ① 정의에 없는 지표 이름이면 목록을 보여 주고 1회 다시 고르게 한다
        (S1V의 재생성과 대칭) ② group_by는 질문에 '~별'이 있을 때만 쓰라고 명시한다."""
        from .semantic import METRICS
        sys = ("너는 공장 데이터 질문을 지표 질의(JSON)로 바꾼다. JSON 한 개만 출력한다. "
               "정의된 지표로 답할 수 없는 질문이면 {\"metrics\": []}를 출력한다.")
        spec = (f"{NOW_LINE} 날짜는 KST, 주는 월요일 시작. 기간은 시작일·종료일(포함)로 쓴다.\n"
                f"[지표]\n{describe()}\n"
                "[JSON 형식] {\"metrics\": [지표...], \"line\": \"L1~L4\"|null, "
                "\"period_name\": \"today|yesterday|this_week|last_week|this_month|last_month\"|null, "
                "\"period\": {\"start\": \"YYYY-MM-DD\", \"end\": \"YYYY-MM-DD\"}|null, \"shift\": \"A|B|C\"|null, "
                "\"shift_date\": \"YYYY-MM-DD\"|null, \"material\": \"M01~M20\"|null, \"product\": \"P01~P12\"|null, "
                "\"qty\": 정수|null, \"group_by\": \"line\"|\"family\"|null, \"include_planned\": true|false}\n"
                "라인 이름: 1라인=L1, 2라인=L2, 3라인=L3, 4라인=L4. 조별 질문은 shift와 shift_date(조가 시작한 날)를 쓴다.")
        if v3:
            spec += ("\nmetrics에는 위 [지표]의 이름만 쓴다. group_by는 질문에 '라인별'·'제품군별'처럼 나눠 달라는 말이 있을 때만 쓰고, "
                     "그 밖에는 null이다.")
        user = f"{spec}\n\n질문: {q['question']}"
        text, lat = self.chat(sys, user)
        intent = extract_json(text)
        steps = [{"raw": text, "intent": intent}]
        if v3 and intent and intent.get("metrics") and any(m not in METRICS for m in intent["metrics"]):
            bad = [m for m in intent["metrics"] if m not in METRICS]
            fb = (f"정의에 없는 지표 이름입니다: {bad}. 다음 중에서만 고르세요: {list(METRICS)}. "
                  "정의된 지표로 답할 수 없으면 {\"metrics\": []}. JSON 한 개만 출력하세요.")
            if v4:
                fb += " 라인·기간·자재 등 나머지 조건은 그대로 두고 전체 JSON을 다시 쓰세요."
            text2, lat2 = self.chat(sys, fb, history=[{"role": "user", "content": user},
                                                     {"role": "assistant", "content": text}])
            lat += lat2
            intent = extract_json(text2)
            steps.append({"raw": text2, "intent": intent, "repair": "unknown_metric"})
        if v4 and intent and intent.get("metrics"):
            from .semantic import missing_conditions
            miss = missing_conditions(q["question"], intent)
            if miss:
                steps[-1]["missing"] = miss
                intent = None                                     # 조건이 빠진 의도로는 답하지 않는다
        if intent and intent.get("metrics"):
            try:
                sql = compile_query(intent)
                rows, err = self.execute(sql)
                steps[-1]["sql"] = sql
                return {"sql": sql, "rows": rows, "error": err, "latency": lat, "route": "semantic",
                        "intent": intent, "steps": steps}
            except Exception as e:
                steps[-1]["compile_error"] = str(e)[:200]
        r = self.s1v(q)                                   # 정의 밖 질문 → 자유 SQL + 검증기
        r["latency"] += lat
        r["route"] = "fallback"
        r["intent"] = intent
        r["steps"] = steps + r["steps"]
        return r
