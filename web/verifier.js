// 검증기 브라우저판 — fda/verifier.py(sqlglot 파스 트리)의 규칙을 정규식으로 근사했다.
// 파이썬판과의 일치는 ?selftest=1에서 기록된 SQL 전체로 대조한다(규칙 집합이 같으면 일치).
const CHILD = ["production_log", "inspections"];

function stripStrings(sql) {
  return sql.replace(/'[^']*'/g, "''");
}

function selectBlocks(low) {
  // 괄호 깊이별로 SELECT 블록을 나눈다(서브쿼리·CTE 안의 SELECT는 따로 본다).
  const out = [];
  const stack = [{ text: "" }];
  for (const ch of low) {
    if (ch === "(") { stack.push({ text: "" }); continue; }
    if (ch === ")") { const b = stack.pop(); out.push(b.text); if (!stack.length) stack.push({ text: "" }); stack[stack.length - 1].text += " _sub_ "; continue; }
    stack[stack.length - 1].text += ch;
  }
  out.push(...stack.map((b) => b.text));
  return out.filter((t) => /\bselect\b/.test(t));
}

export function check(sql, question = "") {
  const flags = [];
  const low = sql.toLowerCase();
  const bare = stripStrings(low);
  const has = (t) => new RegExp(`\\b${t}\\b`).test(bare);

  for (const blk of selectBlocks(bare)) {
    const fromPart = blk.split(/\bfrom\b/).slice(1).join(" from ");
    if (CHILD.every((t) => new RegExp(`\\b${t}\\b`).test(fromPart)) && /\b(sum|count|avg)\s*\(/.test(blk + " " + bare)) {
      flags.push({ rule: "fanout", msg: "실적(production_log)과 검사(inspections)를 한 SELECT에서 조인해 집계합니다. 작업지시 1건에 둘 다 여러 행이라 합계가 부풀려집니다." });
      break;
    }
  }
  if (!/status/.test(low) && (has("production_log") || (has("work_orders") && /sum\s*\(/.test(low))))
    flags.push({ rule: "cancelled", msg: "취소된 작업지시(status='CANCELLED')를 빼는 조건이 없습니다." });

  const usesTs = /\b(ts|start_ts|end_ts)\b/.test(bare);
  const dateLit = /'\d{4}-\d{2}-\d{2}/.test(sql) || /current_date|now\(\)/.test(low);
  if (usesTs && dateLit && !/interval\s*'?\s*-?\s*9\s*'?\s*hours?|[+-]\s*interval|at\s+time\s+zone|'hours?'\s*,\s*-?\s*9|kst|'\+09/.test(low))
    flags.push({ rule: "timezone", msg: "시각 컬럼은 UTC입니다. 날짜 조건은 KST(+9시간)로 바꿔서 비교하세요." });

  if (has("inventory_txn") && /sum\s*\(/.test(low) && !(/pack_size/.test(low) && /box/.test(low)))
    flags.push({ rule: "unit_box", msg: "unit='BOX'인 행은 qty×pack_size가 EA입니다. 환산 없이 합했습니다." });
  if (has("inventory_txn") && /sum\s*\(/.test(low) && !/txn_type/.test(low))
    flags.push({ rule: "txn_sign", msg: "ISSUE·RETURN은 빼고 RECEIPT·ADJUST는 더해야 합니다." });
  if (has("downtime") && !/planned/.test(low) && !/예방정비\s*포함|계획\s*정지\s*포함/.test(question))
    flags.push({ rule: "planned", msg: "계획 정지(planned=true, 예방정비)를 빼는 조건이 없습니다." });
  if (has("downtime") && /end_ts/.test(low) && !/coalesce|is null|ifnull/.test(low))
    flags.push({ rule: "null_end", msg: "진행 중인 정지는 end_ts가 NULL입니다. 현재 시각으로 채우세요." });
  if (/\bavg\s*\([^()]*(\([^()]*\)[^()]*)*\//.test(bare))
    flags.push({ rule: "avg_of_ratio", msg: "비율을 평균했습니다. 합계끼리 나눠야 합니다." });
  if (/[ABC]\s*조/.test(question) && /shift/.test(low) && /\b(p|production_log)\.ts\b/.test(low))
    flags.push({ rule: "shift_date", msg: "조의 근무일은 작업지시 시작일(start_ts, KST)입니다." });
  return flags;
}
