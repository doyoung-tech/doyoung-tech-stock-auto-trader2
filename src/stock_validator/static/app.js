"use strict";

// Report strings are always assigned through textContent, never parsed as HTML.
const DEFAULTS = [
  ["A", "추세 돌파 전략", "추세 · 거래량 · 가격 돌파", "1~5거래일"],
  ["B", "눌림목 매수 전략", "상승 추세 · 조정 후 반등", "1~5거래일"],
  ["C", "과매도 반등 전략", "RSI · 급락 · 지지선", "1~5거래일"],
  ["D", "실적 개선 전략", "시점 검증 실적 · 주가 강세", "1~5거래일"],
  ["E", "폭락 우량주 회복 전략", "70~95% 하락 · 재무 회복", "1~5년"],
  ["F", "업종 상대강도 전략", "시장 대비 업종 · 종목 강도", "1~5거래일"],
];
const LABELS = {PASS: "합격", FAIL: "불합격", DATA_INSUFFICIENT: "데이터 부족"};
const CLASSES = {PASS: "pass", FAIL: "fail", DATA_INSUFFICIENT: "insufficient"};
const $ = (id) => document.getElementById(id);
const finite = (n) => typeof n === "number" && Number.isFinite(n);
const pct = (n) => finite(n) ? `${(n * 100).toLocaleString("ko-KR", {minimumFractionDigits: 2, maximumFractionDigits: 2})}%` : "—";
const num = (n, digits = 0) => finite(n) ? n.toLocaleString("ko-KR", {maximumFractionDigits: digits}) : "—";
const won = (n) => finite(n) ? `${num(n)}원` : "—";
const str = (s, fallback = "미확보") => s === null || s === undefined || s === "" ? fallback : typeof s === "object" ? JSON.stringify(s).slice(0, 1000) : String(s).slice(0, 1000);
const empty = (n) => n.replaceChildren();
let report = {};
let strategies = [];
let selected = "A";

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function badge(status) { return element("span", LABELS[status] || LABELS.DATA_INSUFFICIENT, `badge ${CLASSES[status] || CLASSES.DATA_INSUFFICIENT}`); }
function list(container, items, fallback) {
  empty(container);
  if (!Array.isArray(items) || items.length === 0) {
    container.append(element("p", fallback, "muted"));
    return;
  }
  const ul = element("ul");
  items.forEach((item) => ul.append(element("li", str(item))));
  container.append(ul);
}
function statusFor(strategy) {
  if (report.engine_verified !== true || report.data?.synthetic === true) return "DATA_INSUFFICIENT";
  if (strategy.status === "PASS" && (report.data_evidence_verified !== true || strategy.metrics?.trusted !== true)) return "DATA_INSUFFICIENT";
  return Object.hasOwn(LABELS, strategy.status) ? strategy.status : "DATA_INSUFFICIENT";
}
function metricsFor(strategy) {
  return report.engine_verified === true ? (strategy.metrics || {}) : {};
}
function renderCards() {
  const sort = $("strategy-sort").value;
  const values = [...strategies];
  if (sort !== "default") {
    const key = sort === "return" ? "net_return" : "max_drawdown";
    values.sort((a, b) => {
      const av = metricsFor(a)[key], bv = metricsFor(b)[key];
      if (!finite(av)) return finite(bv) ? 1 : 0;
      if (!finite(bv)) return -1;
      return sort === "drawdown" ? Math.abs(av) - Math.abs(bv) : bv - av;
    });
  }
  empty($("strategy-grid"));
  values.forEach((s) => {
    const m = metricsFor(s);
    const card = element("button", null, `strategy-card${s.id === selected ? " selected" : ""}`);
    card.type = "button";
    card.setAttribute("aria-pressed", String(s.id === selected));
    card.setAttribute("aria-label", `${s.id}. ${s.name}, ${LABELS[statusFor(s)]}, 상세 보기`);
    const top = element("div", null, "card-top");
    top.append(element("span", s.id, "strategy-letter"), badge(statusFor(s)));
    card.append(top, element("h3", s.name), element("p", s.description, "card-description"));
    const values = element("div", null, "card-values");
    [["순수익률", pct(m.net_return)], ["최대 낙폭", pct(m.max_drawdown)], ["거래 수", finite(m.trade_count) ? `${num(m.trade_count)}회` : "—"]].forEach(([label, value]) => {
      const item = element("div"); item.append(element("span", label), element("strong", value)); values.append(item);
    });
    const periodRole = m.period_role === "development_only" ? " · 개발기간·독립 검증 아님" : m.period_role === "holdout" ? " · 최종 독립검증 구간" : "";
    const estimateLabel = report.engine_verified === true && report.data_evidence_verified !== true ? " · 미검증 연구 추정" : "";
    card.append(values, element("div", `${s.horizon} · ${str(s.version, "버전 미확인")}${periodRole}${estimateLabel}`, "card-foot"));
    card.addEventListener("click", () => { selected = s.id; renderCards(); renderDetail(s); });
    $("strategy-grid").append(card);
  });
}
function renderMetrics(m) {
  empty($("metrics-grid"));
  const metrics = [
    ["비용 차감 순수익률", pct(m.net_return)], ["연평균 수익률 (CAGR)", pct(m.cagr)],
    ["최대 낙폭 (MDD)", pct(m.max_drawdown)], ["승률", pct(m.win_rate)],
    ["평균 이익", won(m.avg_win)], ["평균 손실", won(m.avg_loss)],
    ["Profit Factor", num(m.profit_factor, 2)], ["평균 손익비", num(m.payoff_ratio, 2)],
    ["거래당 기대수익", won(m.expectancy)], ["전체 거래 수", finite(m.trade_count) ? `${num(m.trade_count)}회` : "—"],
  ];
  metrics.forEach(([label, value]) => { const node = element("div"); node.append(element("dt", label), element("dd", value)); $("metrics-grid").append(node); });
}
function svgNode(tag, attrs, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attrs || {}).forEach(([key, value]) => node.setAttribute(key, String(value)));
  if (text !== undefined) node.textContent = text;
  return node;
}
function drawLine(id, data, height, key, isDrawdown) {
  const svg = $(id); empty(svg);
  if (data.length === 0) return;
  const ys = data.map((v) => v[key]);
  let min = Math.min(...ys), max = Math.max(...ys);
  if (isDrawdown) max = 0;
  if (min === max) { min -= Math.abs(min) * 0.01 || 1; max += Math.abs(max) * 0.01 || 1; }
  const left = 76, right = 782, top = 15, bottom = height - 15;
  const px = (i) => left + (i / Math.max(data.length - 1, 1)) * (right - left);
  const py = (value) => bottom - ((value - min) / (max - min)) * (bottom - top);
  [0, 0.5, 1].forEach((fraction) => {
    const value = min + fraction * (max - min), y = py(value);
    svg.append(svgNode("line", {x1: left, x2: right, y1: y, y2: y, class: "gridline"}), svgNode("text", {x: left - 12, y: y + 4, "text-anchor": "end", class: "axis-label"}, isDrawdown ? pct(value) : num(value)));
  });
  const points = data.map((v, i) => `${px(i)},${py(v[key])}`).join(" ");
  svg.append(svgNode("polyline", {points, class: isDrawdown ? "drawdown-line" : "equity-line", fill: "none", "vector-effect": "non-scaling-stroke"}));
}
function renderCharts(s) {
  const curve = report.engine_verified === true && Array.isArray(s.equity_curve) ? s.equity_curve.filter((v) => v && finite(v.equity)) : [];
  const drawdowns = Array.isArray(s.drawdown_curve) ? s.drawdown_curve.filter((v) => v && finite(v.drawdown)) : [];
  $("chart-empty").hidden = curve.length > 0;
  $("chart-content").hidden = curve.length === 0;
  $("curve-details").hidden = curve.length === 0;
  empty($("curve-table"));
  if (!curve.length) return;
  // Keep long histories usable on mobile. The table retains all original observations.
  const downsample = (values) => values.length <= 1200 ? values : values.filter((_, i) => i % Math.ceil(values.length / 1200) === 0 || i === values.length - 1);
  drawLine("equity-chart", downsample(curve), 210, "equity", false);
  drawLine("drawdown-chart", downsample(drawdowns), 100, "drawdown", true);
  $("chart-start").textContent = str(curve[0].date);
  $("chart-end").textContent = str(curve[curve.length - 1].date);
  const dd = new Map(drawdowns.map((p) => [p.date, p.drawdown]));
  const fragment = document.createDocumentFragment();
  curve.forEach((p) => { const row = element("tr"); row.append(element("td", str(p.date)), element("td", num(p.equity)), element("td", pct(dd.get(p.date)))); fragment.append(row); });
  $("curve-table").append(fragment);
}
function renderRegimes(s) {
  const node = $("regimes"); empty(node);
  const entries = Object.entries(report.engine_verified === true ? s.regimes || {} : {});
  if (!entries.length) { node.append(element("p", "시장 국면을 분류할 실제 지수 데이터 또는 표본이 부족합니다.", "muted")); return; }
  entries.forEach(([key, value]) => {
    const labels = {bull: "상승장", bear: "하락장", sideways: "횡보장", up: "상승장", down: "하락장"};
    const row = element("div", null, "data-row");
    row.append(element("span", labels[key] || (key === "unknown" ? "국면 미확인" : str(key))), element("span", value && typeof value === "object" ? `손익 ${won(value.realized_pnl)} · 기대 ${won(value.expectancy)} · ${num(value.trade_count)}회` : str(value)));
    node.append(row);
  });
}
function renderStages(s) {
  const node = $("stages"); empty(node);
  const stages = report.engine_verified === true ? s.stages || report.stages || {} : {};
  [["development", "1차 · 전략 개발"], ["walk_forward", "2차 · 워크포워드"], ["holdout", "3차 · 최종 독립 검증"]].forEach(([key, label]) => {
    const item = stages[key];
    const row = element("div", null, "data-row"); row.append(element("span", label));
    if (!item) row.append(element("span", "미실시", "muted"));
    else if (typeof item === "object") {
      const summary = item.executed === true ? `실시 · ${num(item.metrics?.trade_count)}회 · ${pct(item.metrics?.net_return)}` : item.executed === false ? "미실시" : "결과 미확인";
      row.append(element("span", str(item.status_label || item.status || item.reason, summary), "muted"));
    }
    else row.append(element("span", str(item), "muted"));
    node.append(row);
  });
}
function renderBenchmark(s) {
  const node = $("benchmark"); empty(node);
  const benchmarks = report.engine_verified === true ? s.benchmark || report.benchmark || {} : {};
  const entries = Object.entries(benchmarks);
  if (!entries.length) { node.append(element("p", "동일 기간·통화·비용 기준으로 비교할 벤치마크를 확보하지 못했습니다.", "muted")); return; }
  node.append(element("p", "벤치마크는 비용·미확보 배당을 제외한 원화 가격수익률입니다. 순수익률이나 엄밀한 초과성과가 아닙니다.", "muted"));
  entries.forEach(([name, value]) => {
    const row = element("div", null, "data-row"); row.append(element("span", str(name)), element("span", value && typeof value === "object" ? `가격수익 ${pct(value.net_return)} · 단순 차이 ${pct(value.excess_return)}` : "비교 불가")); node.append(row);
    if (value?.reason) node.append(element("p", str(value.reason), "muted"));
    if (value?.basis) node.append(element("p", `비교 기준: ${str(value.basis)}`, "muted"));
  });
}
function renderDetail(s) {
  $("detail-title").textContent = `${s.id}. ${s.name}`;
  $("detail-status").replaceWith(Object.assign(badge(statusFor(s)), {id: "detail-status"}));
  const devOnly = s.metrics?.period_role === "development_only";
  const role = devOnly ? " · 개발기간·독립 검증 아님" : s.metrics?.period_role === "holdout" ? " · 최종 독립검증 구간 (연구용 추정)" : "";
  const period = s.metrics?.period_start && s.metrics?.period_end ? ` · 성과 구간 ${str(s.metrics.period_start)} ~ ${str(s.metrics.period_end)}` : "";
  $("detail-meta").textContent = `보유기간 ${s.horizon} · 전략 버전 ${str(s.version, "미확인")} · ${report.data?.synthetic ? "가상 데이터 예제" : "실제 확보 데이터 기준"}${role}${period}`;
  $("chart-title").textContent = devOnly ? "개발기간 자산곡선·낙폭 (독립 검증 아님)" : "연구 시나리오 자산곡선과 낙폭";
  $("estimate-warning").hidden = report.engine_verified !== true || (report.data_evidence_verified === true && s.metrics?.trusted === true);
  $("estimate-warning").textContent = `미검증 연구 시나리오: 분할·배당·생존자 편향 미해결, 실제 투자 성과로 사용 불가${devOnly ? " · 개발기간·독립 검증 아님" : ""}`;
  const reasons = Array.isArray(s.failed_criteria) ? [...s.failed_criteria] : [];
  if (report.engine_verified !== true) reasons.unshift("엔진 정확성 검증이 확인되지 않아 성과를 신뢰 가능한 결과로 표시하지 않습니다.");
  if (report.data?.synthetic === true) reasons.unshift("가상 데이터 결과는 실제 투자 전략의 합격 근거가 아닙니다.");
  if (s.status === "PASS" && (report.data_evidence_verified !== true || s.metrics?.trusted !== true)) reasons.unshift("과거 시점 데이터·기업행동 회계·신뢰 가능한 성과의 검증 증거가 확인되지 않아 합격 표시를 차단했습니다.");
  if (s.reason) reasons.push(str(s.reason));
  list($("detail-reasons"), reasons, statusFor(s) === "PASS" ? "사전에 정한 검증 기준을 충족했습니다. 실전 적용 전 별도 검토가 필요합니다." : "검증에 필요한 실제 데이터와 독립 검증 근거가 충분하지 않습니다.");
  renderMetrics(metricsFor(s)); renderCharts(s); renderRegimes(s); renderStages(s); renderBenchmark(s);
  renderSensitivity($("cost-sensitivity"), report.engine_verified === true ? s.cost_sensitivity : null, "거래비용을 늘린 독립 검증이 아직 완료되지 않았습니다.");
  renderSensitivity($("parameter-stability"), report.engine_verified === true ? s.parameter_stability : null, "인접 파라미터의 독립 검증이 아직 완료되지 않았습니다.");
  const ci = report.engine_verified === true ? s.confidence_interval || {} : {};
  const node = $("uncertainty"); empty(node);
  if (finite(ci.lower) && finite(ci.upper)) {
    const level = finite(ci.level) ? ci.level : finite(ci.alpha) ? 1 - ci.alpha : null;
    node.append(element("p", `거래당 기대수익 ${finite(level) ? pct(level) : "수준 미기재"} 신뢰구간: ${won(ci.lower)} ~ ${won(ci.upper)}`));
    node.append(element("p", str(ci.method, "산출 방법 미기재"), "muted"));
  } else node.append(element("p", "기대수익의 신뢰구간을 계산할 표본이나 독립 검증이 부족합니다.", "muted"));
  const warnings = Array.isArray(s.warnings) ? s.warnings : [];
  warnings.forEach((warning) => node.append(element("p", str(warning), "risk-warning")));
  node.append(element("p", "여러 전략·파라미터를 비교하면 우연한 고성과를 고를 수 있습니다. 거래 표본과 독립 검증을 함께 확인하세요.", "muted"));
}
function renderSensitivity(node, value, fallback) {
  empty(node);
  if (!value || typeof value !== "object") { node.append(element("p", fallback, "muted")); return; }
  if (value.reason || value.status_label || value.status) node.append(element("p", str(value.reason || value.status_label || value.status)));
  const metrics = value.metrics || value;
  if (finite(value.multiplier)) node.append(element("p", `비용 ${num(value.multiplier, 1)}배 · 사후 재조정 없음`));
  if (finite(metrics.net_return)) node.append(element("p", `검증 순수익률 ${pct(metrics.net_return)}`));
  if (finite(metrics.expectancy)) node.append(element("p", `거래당 기대수익 ${won(metrics.expectancy)}`));
  if (finite(value.profitable_fraction)) node.append(element("p", `인접 조합의 양수 수익 비율 ${pct(value.profitable_fraction)}`));
  if (value.executed === false || value.available === false) node.append(element("p", fallback, "muted"));
  if (!node.childNodes.length) node.append(element("p", str(value), "muted"));
}
function renderData() {
  const data = report.data || {};
  const node = $("data-provenance"); empty(node);
  const items = [["데이터 종류", data.synthetic === true ? "가상 데이터 (실제 시장 아님)" : report.available && data.source ? "실제 확보 데이터 · 범위 확인 필요" : "아직 확보 보고서 없음"], ["출처", str(data.source)], ["확보 기간", data.start && data.end ? `${str(data.start)} ~ ${str(data.end)}` : "미확보"], ["데이터 식별값", str(data.sha256, "미기재")], ["코드 버전", str(report.code_version, "미기재")], ["보고서 형식 버전", str(report.schema_version, "미기재")], ["사전 합격 기준 식별값", str(report.policy_hash, "미기재")]];
  items.forEach(([label, value]) => { const row = element("div"); row.append(element("dt", label), element("dd", value)); node.append(row); });
  list($("data-limitations"), data.limitations, "과거 시점의 종목 구성·재무 공시·상장폐지 자료가 없으면 생존자 편향과 미래 정보 사용 여부를 완전히 평가할 수 없습니다.");
  if (report.costs) {
    const costs = report.costs;
    $("data-limitations").append(element("h3", "거래비용 가정 (현재 법정세율 아님)"));
    [["commission_bps", "편도 수수료"], ["sell_tax_bps", "매도 세금"], ["slippage_bps", "편도 슬리피지"], ["fx_bps", "환전 비용"]].forEach(([key, label]) => {
      const row = element("div", null, "data-row"); row.append(element("span", label), element("span", finite(costs[key]) ? pct(costs[key] / 10000) : "미기재")); $("data-limitations").append(row);
    });
  }
  if (Array.isArray(report.execution_assumptions)) {
    $("data-limitations").append(element("h3", "체결·평가 가정"));
    const ul = element("ul"); report.execution_assumptions.forEach((item) => ul.append(element("li", str(item)))); $("data-limitations").append(ul);
  }
}
function render(value) {
  report = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const existing = Array.isArray(report.strategies) ? report.strategies : [];
  strategies = DEFAULTS.map(([id, name, description, horizon]) => ({id, name, description, horizon, ...(existing.find((s) => s && s.id === id) || {}), id}));
  $("synthetic-banner").hidden = report.data?.synthetic !== true;
  $("evidence-banner").hidden = report.engine_verified !== true || report.data_evidence_verified === true || report.data?.synthetic === true;
  const date = new Date(report.generated_at);
  $("generated-at").textContent = Number.isNaN(date.getTime()) ? "아직 없음" : new Intl.DateTimeFormat("ko-KR", {dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Seoul"}).format(date);
  $("engine-state").textContent = report.engine_verified === true ? "엔진 정확성 검증 통과" : "엔진 검증 확인 대기";
  const counts = {PASS: 0, FAIL: 0, DATA_INSUFFICIENT: 0};
  strategies.forEach((s) => counts[statusFor(s)]++);
  $("count-pass").replaceChildren(document.createTextNode(String(counts.PASS)), element("small", "개"));
  $("count-fail").replaceChildren(document.createTextNode(String(counts.FAIL)), element("small", "개"));
  $("count-insufficient").replaceChildren(document.createTextNode(String(counts.DATA_INSUFFICIENT)), element("small", "개"));
  const decision = counts.PASS > 0 ? `검토 가능한 합격 전략 ${counts.PASS}개` : "합격 전략 없음: 투자하지 않음";
  $("decision-title").textContent = decision;
  $("decision-description").textContent = counts.PASS > 0 ? "합격은 연구 기준 통과를 의미합니다. 실제 투자·활성화는 별도 검토가 필요합니다." : report.available ? str(report.decision, "독립 검증 근거가 부족하거나 사전 합격 기준을 충족하지 못했습니다.") : "실제 데이터와 엔진 검증을 갖춘 보고서를 기다리고 있습니다.";
  renderCards(); renderDetail(strategies.find((s) => s.id === selected) || strategies[0]); renderData();
}
$("strategy-sort").addEventListener("change", renderCards);
render({});
fetch("/api/report", {cache: "no-store", credentials: "same-origin"})
  .then(async (response) => {
    const value = await response.json();
    render(value);
    if (!response.ok) throw new Error("보고서를 읽지 못했습니다. 보고서 생성 작업과 데이터 파일을 확인하세요.");
  })
  .catch(() => { $("error-banner").hidden = false; $("error-banner").textContent = "보고서를 불러오지 못했습니다. 검증 근거가 확인될 때까지 투자하지 않습니다."; });
