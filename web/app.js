import * as duckdb from "https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.30.0/+esm";
import { check } from "./verifier.js";

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
let conn, DATA, SHOW = [], idx = 0, timer = null, paused = false;
const STEP_MS = 9000;

// ---------- 결과 대조(fda/evaluate.py와 같은 규칙)
function num(x) { return typeof x === "number" ? x : typeof x === "bigint" ? Number(x) : null; }
function same(a, b) {
  const na = num(a), nb = num(b);
  if (na !== null && nb !== null) {
    const close = (x, y, rel, abs) => Math.abs(x - y) <= Math.max(rel * Math.max(Math.abs(x), Math.abs(y)), abs);
    if (close(na, nb, 1e-4, 1e-6)) return true;
    if (Math.abs(na) <= 1 && close(na * 100, nb, 1e-4, 1e-4)) return true;
    return false;
  }
  if (a == null || b == null) return a == null && b == null;
  return String(a).trim() === String(b).trim();
}
function rowContains(pred, gold) {
  const used = new Set();
  for (const g of gold) {
    const j = pred.findIndex((p, k) => !used.has(k) && same(g, p));
    if (j < 0) return false;
    used.add(j);
  }
  return true;
}
function match(gold, pred, kind) {
  if (!pred) return false;
  if (!gold.length) return !pred.length;
  if (!pred.length) return false;
  if (kind === "value") return pred.length === 1 && pred[0].some((v) => same(gold[0][0], v));
  if (kind === "label") return pred[0].some((v) => same(gold[0][0], v));
  if (kind === "row") return pred.length === 1 && rowContains(pred[0], gold[0]);
  if (pred.length !== gold.length) return false;
  const left = pred.map((_, i) => i);
  for (const g of gold) {
    const j = left.find((k) => rowContains(pred[k], g));
    if (j === undefined) return false;
    left.splice(left.indexOf(j), 1);
  }
  return true;
}

// ---------- DuckDB-WASM
async function initDB() {
  const bundle = await duckdb.selectBundle(duckdb.getJsDelivrBundles());
  const workerUrl = URL.createObjectURL(new Blob([`importScripts("${bundle.mainWorker}");`], { type: "text/javascript" }));
  const db = new duckdb.AsyncDuckDB(new duckdb.VoidLogger(), new Worker(workerUrl));
  await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
  URL.revokeObjectURL(workerUrl);
  conn = await db.connect();
  for (const t of DATA.meta.tables) {
    const url = new URL(`data/${t}.parquet`, location.href).href;
    await db.registerFileURL(`${t}.parquet`, url, duckdb.DuckDBDataProtocol.HTTP, false);
    await conn.query(`CREATE TABLE ${t} AS SELECT * FROM '${t}.parquet'`);
  }
}
function plain(v) {
  if (typeof v === "bigint") return Number(v);
  if (v && typeof v === "object" && "toString" in v && !(v instanceof Date)) {
    const n = Number(v.toString()); return Number.isFinite(n) ? n : v.toString();
  }
  return v;
}
let runQueue = Promise.resolve();
function run(sql) {              // 쿼리는 줄 세워 하나씩(앞선 결과가 뒤 화면에 섞이지 않게)
  const p = runQueue.then(async () => {
    const t0 = performance.now();
    try {
      const r = await conn.query(sql);
      const rows = r.toArray().map((row) => Object.values(row.toJSON()).map(plain));
      return { rows, ms: performance.now() - t0 };
    } catch (e) {
      return { error: String(e.message || e).split("\n")[0].slice(0, 200), ms: performance.now() - t0 };
    }
  });
  runQueue = p.catch(() => {});
  return p;
}
function fmt(rows) {
  if (!rows) return "";
  if (!rows.length) return "(결과 없음)";
  const f = (v) => typeof v === "number" ? (Math.abs(v) < 1 && v !== 0 ? v.toFixed(4) : (Number.isInteger(v) ? v.toLocaleString() : v.toFixed(2))) : String(v);
  return rows.slice(0, 6).map((r) => r.map(f).join(" · ")).join("\n") + (rows.length > 6 ? `\n… ${rows.length}행` : "");
}
function setRes(el, r, gold, kind, label) {
  el.className = "res";
  if (!r) { el.textContent = ""; return; }
  if (r.error) { el.classList.add("warn"); el.textContent = `실행 오류: ${r.error}`; return; }
  const ok = match(gold, r.rows, kind);
  el.classList.add(ok ? "ok" : "bad");
  el.textContent = `${ok ? "✓" : "✗"} ${fmt(r.rows)}${label ? `  (${label})` : ""}`;
}

// ---------- 시연
const TRAP_KO = { timezone: "시간대(UTC↔KST)", cancelled: "취소 작업지시", fanout: "1:N 조인 부풀림", unit_box: "BOX↔EA 단위",
  return_sign: "반품 부호", planned_downtime: "계획 정지", null_end: "진행 중 정지", period_clip: "기간 경계",
  shift_boundary: "야간조 근무일", ratio_of_sums: "비율의 평균", metric_definition: "지표 정의", control: "함정 없음" };

function pickShowcase() {
  // 바로 쓴 SQL(S1)은 실행됐는데 틀렸고, 의미 계층(S2)은 맞은 test 문항 — 함정 종류가 겹치지 않게
  const seen = new Set();
  const cand = DATA.cases.filter((c) => c.runs.s1 && c.runs.s2 && c.runs.s1.sql && !c.runs.s1.error && !c.runs.s1.correct && c.runs.s2.correct);
  const out = [];
  for (const c of cand) {
    const key = c.traps.find((t) => !seen.has(t));
    if (key) { c.traps.forEach((t) => seen.add(t)); out.push(c); }
  }
  return out.length ? out : cand.slice(0, 6);
}
async function show(i) {
  idx = (i + SHOW.length) % SHOW.length;
  const c = SHOW[idx];
  $("dq").textContent = c.question;
  $("dtraps").innerHTML = c.traps.map((t) => `<span class="chip">${TRAP_KO[t] || t}</span>`).join("");
  const s1 = c.runs.s1.sql, s2 = c.runs.s2;
  $("d1sql").textContent = s1;
  $("d2intent").textContent = JSON.stringify(Object.fromEntries(Object.entries(s2.intent || {}).filter(([, v]) => v !== null && v !== undefined)));
  $("d2sql").textContent = s2.sql || "";
  $("d3sql").textContent = c.gold_sql;
  ["d1res", "d2res", "d3res"].forEach((id) => { $(id).className = "res"; $(id).textContent = "계산 중…"; });
  $("d1flags").innerHTML = "";
  $("dpos").textContent = `${idx + 1} / ${SHOW.length}`;
  const [r1, r2, r3] = [await run(s1), await run(s2.sql), await run(c.gold_sql)];
  if (SHOW[idx] !== c) return;
  setRes($("d1res"), r1, c.gold, c.kind, `${r1.ms.toFixed(0)}ms`);
  setRes($("d2res"), r2, c.gold, c.kind, `${r2.ms.toFixed(0)}ms`);
  setRes($("d3res"), r3, c.gold, c.kind);
  $("d1flags").innerHTML = check(s1, c.question).map((f) => `<li>${f.msg}</li>`).join("");
  restartProgress();
}
function restartProgress() {
  const bar = $("dprog");
  bar.style.transition = "none"; bar.style.width = "0";
  if (paused) return;
  requestAnimationFrame(() => { bar.style.transition = `width ${STEP_MS}ms linear`; bar.style.width = "100%"; });
  clearTimeout(timer);
  timer = setTimeout(() => show(idx + 1), STEP_MS);
}

// ---------- 직접 해 보기
function fillPick() {
  const sel = $("pick");
  sel.innerHTML = DATA.cases.map((c, i) => `<option value="${i}">${c.question}</option>`).join("");
  sel.onchange = () => pickCase(+sel.value);
  pickCase(0);
}
function pickCase(i) {
  const c = DATA.cases[i];
  const r = c.runs;
  const line = (k, label) => r[k] ? `${label} ${r[k].correct ? "✓" : r[k].abstain ? "보류" : r[k].error ? "오류" : "✗"}` : null;
  $("pickinfo").textContent = [`함정: ${c.traps.map((t) => TRAP_KO[t] || t).join(", ")}`,
    line("s0", "DDL만"), line("s1", "설명 추가"), line("s1v", "검증기"), line("s2", "의미 계층")].filter(Boolean).join(" · ");
  $("sql").value = (r.s1 && r.s1.sql) || c.gold_sql;
  $("myres").textContent = ""; $("myres").className = "res"; $("myflags").innerHTML = "";
}
async function runMine() {
  const c = DATA.cases[+$("pick").value];
  const sql = $("sql").value;
  const r = await run(sql);
  setRes($("myres"), r, c.gold, c.kind, `${r.ms.toFixed(0)}ms · 정답 ${fmt(c.gold)}`);
  const fl = check(sql, c.question);
  $("myflags").innerHTML = fl.length ? fl.map((f) => `<li>${f.msg}</li>`).join("") : `<li style="color:var(--ok)">검증기 규칙에 걸린 것 없음</li>`;
}

// ---------- 숫자 표
function fillNums() {
  const s = DATA.meta.summary;
  if (!s) { $("nums").innerHTML = `<p class="note">아직 측정 전</p>`; return; }
  const rows = [["s0", "DDL만"], ["s1", "+ 테이블 설명·용어집"], ["s1v", "+ 검증기(재생성·보류)"], ["s2", "의미 계층"]]
    .filter(([k]) => s[k]).map(([k, n]) => { const x = s[k];
      return `<tr><td>${n}</td><td>${x.correct}</td><td>${x.wrong + (x.lucky || 0)}</td><td>${x.error}</td><td>${x.abstain}</td><td>${(x.acc * 100).toFixed(1)}%</td></tr>`; }).join("");
  $("nums").innerHTML = `<table><thead><tr><th>방식</th><th>정답</th><th>그럴듯한 오답</th><th>실행 오류</th><th>답 보류</th><th>정답률</th></tr></thead><tbody>${rows}</tbody></table>
  <p class="note">정답은 기록 시각을 날짜 경계 쪽으로 옮긴 두 번째 데이터에서도 맞아야 센다. 그럴듯한 오답 = 실행되고 값도 나왔는데 틀린 답.</p>`;
}

// ---------- 자체 대조
async function selftest() {
  let ok = 0, n = 0, vjs = 0, vn = 0; const bad = [];
  for (const c of DATA.cases) {
    const r = await run(c.gold_sql); n++;
    if (!r.error && match(c.gold, r.rows, c.kind)) ok++; else bad.push(c.qid);
    for (const k of ["s0", "s1", "s1v"]) {
      const rr = c.runs[k]; if (!rr || !rr.sql || !rr.py_rules) continue;
      vn++;
      const js = check(rr.sql, c.question).map((f) => f.rule).sort().join(",");
      if (js === rr.py_rules.slice().sort().join(",")) vjs++; else bad.push(`${c.qid}/${k}: js[${js}] py[${rr.py_rules}]`);
    }
  }
  const msg = `selftest 정답 SQL ${ok}/${n} · 검증기 규칙 일치 ${vjs}/${vn}`;
  $("status").textContent = msg;
  window.__selftest = { ok, n, vjs, vn, bad };
  console.log(msg, bad.slice(0, 20));
}

async function main() {
  DATA = await (await fetch("data/cases.json", { cache: "reload" })).json();
  await initDB();
  $("status").textContent = `브라우저 안 DuckDB에 공장 데이터 ${DATA.meta.tables.length}개 테이블을 올렸습니다.`;
  $("glossary").innerHTML = DATA.meta.glossary.map((g) => `<li>${g}</li>`).join("");
  SHOW = pickShowcase();
  fillPick(); fillNums();
  $("run").onclick = runMine;
  $("fillgold").onclick = () => { $("sql").value = DATA.cases[+$("pick").value].gold_sql; };
  $("fillsem").onclick = () => { const s = DATA.cases[+$("pick").value].semantic_sql; if (s) $("sql").value = s; };
  $("next").onclick = () => show(idx + 1);
  $("prev").onclick = () => show(idx - 1);
  $("pause").onclick = () => { paused = !paused; $("pause").textContent = paused ? "재생" : "멈춤"; if (paused) clearTimeout(timer); restartProgress(); };
  if (params.has("selftest")) { await selftest(); return; }
  if (SHOW.length) show(0);
  window.__fda = { run, show, check, match };
}
main().catch((e) => { $("status").textContent = `불러오기 실패: ${e.message || e}`; console.error(e); });
