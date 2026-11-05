"use strict";
const data = JSON.parse(document.getElementById("usage-data").textContent);
const $ = id => document.getElementById(id);
const nf = new Intl.NumberFormat("ja-JP");
const number = v => v == null ? "不明" : nf.format(v);
const compact = v => new Intl.NumberFormat("ja-JP", {notation:"compact", maximumFractionDigits:1}).format(v);
const text = (id, value) => { $(id).textContent = value; };
const filters = ["sourceId", "provider", "displayModel", "command", "generationProfile"];
const diagnostics = data.diagnostics || [];
const allModels = [...new Set([...data.records.map(r => r.displayModel), ...diagnostics.flatMap(d => d.models)])].sort();
const palette = ["#117c80", "#8858b7", "#bf572c", "#386db3", "#8a7314", "#b23b6f", "#407742", "#687183"];
const colors = new Map(allModels.map((m, i) => [m, palette[i] || `hsl(${(i * 137.5) % 360} 55% 38%)`]));
const hiddenSeries = new Set();
const categories = {
  "run-warning":"実行時の警告", "run-failure":"取込・実行の失敗", "note-failure":"ノート生成の失敗",
  "note-warning":"ノートの警告", validation:"出力内容の検証", "transport-retry":"通信・応答の再試行",
  "budget-stop":"予算上限で停止", deferred:"処理の見送り", "note-metadata":"ノート情報の読込",
  "usage-missing":"過去使用量の欠測", attempt:"モデル呼び出しの失敗・未完了"
};
let page = 0, diagnosticPage = 0, current = [], currentDiagnostics = [];
for (const key of filters) {
  const values = key === "displayModel" ? allModels : [...new Set([...data.records, ...diagnostics].map(r => r[key] || "unknown"))].sort();
  $(key).add(new Option("すべて", ""));
  for (const value of values) $(key).add(new Option(value, value));
}
$("diagnosticCategory").add(new Option("すべて", ""));
for (const key of [...new Set(diagnostics.map(d => d.category))].sort()) {
  $("diagnosticCategory").add(new Option(categories[key] || key, key));
}
for (const name of Object.keys(data.priceScenarios)) $("scenario").add(new Option(name, name));
if (!$("scenario").options.length) $("scenario").add(new Option("単価未設定", ""));
const offset = data.utcOffsetMinutes;
const zone = "UTC" + (offset < 0 ? "-" : "+") + String(Math.floor(Math.abs(offset) / 60)).padStart(2,"0") + ":" + String(Math.abs(offset) % 60).padStart(2,"0");
text("freshness", "生成日時: " + data.builtAt + " / 集計タイムゾーン: " + zone + " / 入力 " + data.sources.length + " ファイル");
text("snapshot", "Snapshot: " + data.snapshotId);
for (const source of data.sources) {
  const p = document.createElement("p");
  p.textContent = source.path + " — SHA-256 " + source.sha256 + (source.scope ? " (" + source.scope + ")" : " (ファイル全体)");
  $("sources").append(p);
}
function matches(r, includeModel = true) {
  const query = $("threadId").value.trim().toLocaleLowerCase();
  const name = [r.threadId, r.taskTitle, r.noteTitle, r.noteFile, r.displayTitle].filter(Boolean).join(" ").toLocaleLowerCase();
  return filters.every(key => key === "displayModel" && !includeModel || !$(key).value ||
    (key === "displayModel" && r.models ? r.models.includes($(key).value) : $(key).value === (r[key] || "unknown"))) &&
    (!$("from").value || r.date && r.date >= $("from").value) &&
    (!$("to").value || r.date && r.date <= $("to").value) && name.includes(query);
}
function total(rows, key) {
  return rows.reduce((s, r) => s + (r[key] ?? r["known" + key[0].toUpperCase() + key.slice(1)] ?? 0), 0);
}
const tokens = rows => total(rows, "inputTokens") + total(rows, "outputTokens");
const repair = r => /-(repair|regenerate)$/.test(r.stage || "");
const failed = r => ["failed", "transport-failed", "invalid-response", "rejected"].includes(r.status);
const taskKey = r => r.sourceId + "/" + r.threadId;
const noteKey = r => taskKey(r) + "/" + r.runId;
function grouped(rows, key) {
  const groups = new Map();
  for (const r of rows) {
    const label = typeof key === "function" ? key(r) : (r[key] || "unknown");
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(r);
  }
  return [...groups].map(([label, rows]) => ({label, rows, input:total(rows,"inputTokens"), output:total(rows,"outputTokens"),
    count:rows.length, unknown:rows.filter(r => !r.usageComplete).length}));
}
function table(id, headers, rows, wrap = [0]) {
  const table = document.createElement("table"), head = document.createElement("thead"), tr = document.createElement("tr");
  for (const h of headers) {
    const th = document.createElement("th"); th.scope = "col"; th.textContent = h; tr.append(th);
  }
  head.append(tr); table.append(head);
  const body = document.createElement("tbody");
  for (const cells of rows) {
    const row = document.createElement("tr");
    cells.forEach((v, i) => {
      const cell = document.createElement("td"); cell.textContent = v;
      if (wrap.includes(i)) cell.className = "label";
      row.append(cell);
    });
    body.append(row);
  }
  table.append(body); $(id).replaceChildren(table);
  if (!rows.length) {
    const p = document.createElement("p"); p.className = "muted"; p.textContent = "該当する記録はありません。"; $(id).append(p);
  }
}
function svgNode(name, attrs = {}, label) {
  const e = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, v] of Object.entries(attrs)) e.setAttribute(key, String(v));
  if (label != null) e.textContent = label;
  return e;
}
function period(r) {
  if (!r.date) return "日付不明";
  if ($("period").value === "month") return r.date.slice(0,7);
  if ($("period").value === "week") {
    const d = new Date(r.date + "T00:00:00Z"); d.setUTCDate(d.getUTCDate() - (d.getUTCDay() + 6) % 7);
    return d.toISOString().slice(0,10);
  }
  return r.date;
}
function renderTimeline() {
  const mode = $("timelineMode").value;
  const models = allModels.filter(m => current.some(r => r.displayModel === m));
  const io = [{key:"inputTokens", label:"入力", color:"#117c80"}, {key:"outputTokens", label:"出力", color:"#c77727"}];
  const series = mode === "io" ? io.map(v => ({...v, id:v.key, value:rs => total(rs,v.key)})) :
    models.flatMap(m => mode === "model" ? [{id:m, label:m, color:colors.get(m), value:rs => tokens(rs.filter(r => r.displayModel === m))}] :
      io.map((v,i) => ({id:m + "/" + v.key, label:m + " / " + v.label, color:colors.get(m), opacity:i ? .5 : 1,
        value:rs => total(rs.filter(r => r.displayModel === m),v.key)})));
  $("timelineLegend").replaceChildren();
  for (const s of series) {
    const button = document.createElement("button");
    button.type = "button"; button.textContent = "■ " + s.label; button.style.color = s.color;
    button.setAttribute("aria-pressed", String(!hiddenSeries.has(s.id)));
    button.onclick = () => { hiddenSeries.has(s.id) ? hiddenSeries.delete(s.id) : hiddenSeries.add(s.id); renderTimeline(); };
    $("timelineLegend").append(button);
  }
  const active = series.filter(s => !hiddenSeries.has(s.id));
  const groups = grouped(current, period).sort((a,b) => a.label.localeCompare(b.label));
  table("timelineValues", ["期間", ...active.map(s => s.label), "不明試行"],
    groups.map(g => [g.label, ...active.map(s => number(s.value(g.rows))), number(g.unknown)]));
  if (!groups.length || !active.length) { text("timeline", "表示する系列がありません。"); return; }
  const w = Math.max(640, groups.length * 38 + 80), max = Math.max(1, ...groups.map(g => active.reduce((v,s) => v + s.value(g.rows),0)));
  const svg = svgNode("svg", {viewBox:`0 0 ${w} 260`, role:"img", "aria-label":"期間別トークン使用量。系列と数値は凡例および数値表を参照。"});
  svg.style.minWidth = w + "px";
  const step = (w - 80) / groups.length;
  for (let j = 0; j < 4; j++) {
    const y = 25 + j * 55;
    svg.append(svgNode("line", {x1:65,x2:w - 10,y1:y,y2:y,stroke:"#d9e3e7"}));
    svg.append(svgNode("text", {x:58,y:y+4,"text-anchor":"end",fill:"#536774","font-size":11},compact(max * (1 - j / 3))));
  }
  groups.forEach((g,i) => {
    const x = 65 + i * step + step * .16, bw = step * .68;
    let y = 190;
    for (const s of active) {
      const value = s.value(g.rows), height = value / max * 165;
      y -= height;
      const rect = svgNode("rect", {x,y,width:bw,height,fill:s.color,"fill-opacity":s.opacity || 1});
      rect.append(svgNode("title", {}, g.label + " / " + s.label + ": " + number(value) + " tokens / 欠測を含む試行 " + g.unknown));
      svg.append(rect);
    }
    if (groups.length <= 8 || i % Math.ceil(groups.length / 8) === 0) svg.append(svgNode("text",
      {x:x+bw/2,y:218,"text-anchor":"middle",fill:"#536774","font-size":10},g.label.length === 10 ? g.label.slice(5) : g.label));
    if (g.unknown) svg.append(svgNode("text", {x:x+bw/2,y:242,"text-anchor":"middle",fill:"#825900","font-size":10},"欠測"));
  });
  $("timeline").replaceChildren(svg);
}
function renderRanking(groups) {
  $("ranking").replaceChildren();
  const max = Math.max(1,...groups.map(g => g.input + g.output));
  for (const [i,g] of groups.slice(0,10).entries()) {
    const div = document.createElement("div"), button = document.createElement("button"), track = document.createElement("div"), bar = document.createElement("span");
    div.className = "rank-row"; button.className = "rank-name";
    button.textContent = (i+1) + ". " + (g.rows[0].displayTitle || g.label) + " — " + number(g.input + g.output) + " tokens";
    button.onclick = () => { $("threadId").value = g.rows[0].threadId; $("sourceId").value = g.rows[0].sourceId; page = diagnosticPage = 0; render(); };
    track.className = "rank-track"; bar.style.width = (g.input + g.output) / max * 100 + "%";
    track.append(bar); div.append(button,track); $("ranking").append(div);
  }
  if (!groups.length) text("ranking", "該当する記録はありません。");
}
function renderDetails() {
  table("records", ["実行日時 / 対象タスク", "モデル", "工程 / 試行結果 / ノート結果", "入力", "出力", "キャッシュ入力", "推論", "取得元"],
    current.slice(page * 50,(page + 1) * 50).map(r => [(r.startedAt || "不明") + "\n" + (r.displayTitle || r.threadId || "不明"),
      r.displayModel, [r.stage,r.status,r.noteStatus].map(v => v || "不明").join(" / "), number(r.inputTokens), number(r.outputTokens),
      number(r.cachedInputTokens), number(r.reasoningTokens), r.usageSource || "legacy"]), [0,1,2]);
  text("page", (current.length ? page + 1 : 0) + " / " + Math.ceil(current.length / 50) + " ページ");
  $("prev").disabled = page === 0; $("next").disabled = (page + 1) * 50 >= current.length;
}
function renderDiagnosticPage() {
  table("diagnostics", ["日時 / 重要度", "分類 / 工程", "内容", "対象タスク / ファイル", "モデル / ノート結果", "実行ID / 試行ID / 根拠ファイル"],
    currentDiagnostics.slice(diagnosticPage * 50,(diagnosticPage + 1) * 50).map(d => [
      (d.startedAt || "日付不明") + "\n" + d.severity,
      (categories[d.category] || d.category) + "\n" + (d.stage || "—") + (d.attempt ? " / 検証 " + d.attempt + " 回目" : ""),
      d.message, (d.displayTitle || "実行全体") + "\n" + (d.noteFile || "") + "\n" + (d.threadId || "") + "\n" + d.sourceId,
      d.displayModel + "\n" + (d.noteStatus || "—"), d.runId + "\n" + (d.usageId || "") + "\n" + (d.evidencePath || "") + "\n" + (d.sourceRef || "")
    ]), [0,1,2,3,4,5]);
  text("diagnosticPage", (currentDiagnostics.length ? diagnosticPage + 1 : 0) + " / " + Math.ceil(currentDiagnostics.length / 50) + " ページ");
  $("diagnosticPrev").disabled = diagnosticPage === 0;
  $("diagnosticNext").disabled = (diagnosticPage + 1) * 50 >= currentDiagnostics.length;
}
function renderDiagnostics() {
  const scoped = diagnostics.filter(d => matches(d));
  text("diagnosticSummary", ["ERROR","WARNING","INFO"].map(s => s + " " + scoped.filter(d => d.severity === s).length + " 件").join(" / "));
  const severity = $("severity").value, query = $("diagnosticSearch").value.trim().toLocaleLowerCase();
  currentDiagnostics = scoped.filter(d => (severity === "all" || (severity === "issues" ? d.severity !== "INFO" : d.severity === severity)) &&
    (!$("diagnosticCategory").value || d.category === $("diagnosticCategory").value) &&
    [d.message,d.stage,d.runId,d.usageId].join(" ").toLocaleLowerCase().includes(query)).reverse();
  const groups = grouped(currentDiagnostics, r => r.severity + " / " + (categories[r.category] || r.category)).sort((a,b) => b.count - a.count);
  table("diagnosticGroups", ["診断の種類", "記録件数", "対象タスク数"], groups.map(g => [g.label, number(g.count),
    number(new Set(g.rows.filter(r => r.threadId).map(taskKey)).size)]));
  renderDiagnosticPage();
}
function renderCost() {
  const name = $("scenario").value, price = data.priceScenarios[name];
  if (price) {
    const known = current.map(r => r.referenceCosts[name]).filter(v => v != null), cost = known.reduce((a,b) => a+b,0);
    text("cost", known.length ? new Intl.NumberFormat("ja-JP", {style:"currency",currency:price.currency,maximumFractionDigits:4}).format(cost) : "算出できません");
    text("costCoverage", "算出可能 " + known.length + " / " + current.length + " 試行。算出できた分のみの合計です。");
    text("priceDetail", "単価基準日 " + price.pricing_date + " / 100万tokens: 入力 " + price.input_per_million + ", 出力 " + price.output_per_million + " " + price.currency +
      " / キャッシュ: " + (price.cache_policy === "no-cache" ? "全入力を通常単価で試算" : "実測内訳を使用・読込 " + price.cached_input_per_million + ", 書込 " + (price.cache_write_per_million ?? "通常入力単価")));
  } else {
    text("cost", "単価未設定"); text("costCoverage", "usage_report.price_scenarios に比較用単価を設定し、build-report を再実行してください。"); text("priceDetail", "");
  }
}
function render() {
  current = data.records.filter(r => matches(r));
  const count = current.length, unknown = current.filter(r => !r.usageComplete).length;
  $("empty").hidden = count > 0;
  text("input", count ? number(total(current,"inputTokens")) : "—"); text("output", count ? number(total(current,"outputTokens")) : "—");
  for (const [id,key] of [["inputCoverage","inputTokens"],["outputCoverage","outputTokens"]]) text(id,"不明を含む試行 " + current.filter(r => r[key] == null).length + " / " + count);
  text("coverage", (count - unknown) + " / " + count);
  text("status", "未完了 " + current.filter(r => !r.finishedAt && r.status === "started").length + " · 受信成功以外 " + current.filter(r => r.status !== "received").length);
  renderTimeline();
  const groups = grouped(current,"displayModel").sort((a,b) => b.input+b.output-a.input-a.output);
  table("models", ["モデル", "対象タスク", "入力", "出力", "試行 / 不明", "修復の入出力 / 比率", "失敗試行"], groups.map(g => [
    g.label, number(new Set(g.rows.map(taskKey)).size), number(g.input), number(g.output), g.count + " / " + g.unknown,
    number(tokens(g.rows.filter(repair))) + " / " + (g.input + g.output ? (tokens(g.rows.filter(repair)) / (g.input + g.output) * 100).toFixed(1) + "%" : "—"),
    number(g.rows.filter(failed).length)]));
  table("stages", ["工程", "入力", "出力", "試行 / 不明", "取得済み入出力の構成比"],
    grouped(current,"stage").sort((a,b) => b.input+b.output-a.input-a.output).map(g => [g.label,number(g.input),number(g.output),g.count + " / " + g.unknown,
      tokens(current) ? ((g.input + g.output) / tokens(current) * 100).toFixed(1) + "%" : "—"]));
  table("commands", ["コマンド","入力","出力","試行 / 不明"], grouped(current,"command").map(g => [g.label,number(g.input),number(g.output),g.count + " / " + g.unknown]));
  const selectedKeys = new Set(current.map(noteKey));
  const notes = data.notes.filter(n => matches(n,false) && (!$("displayModel").value || selectedKeys.has(noteKey(n))));
  text("perNote", notes.length && count ? number(Math.round(tokens(current) / notes.length)) + " tokens" : "—");
  const missingNotes = notes.filter(n => !selectedKeys.has(noteKey(n))).length;
  text("noteCount", "生成成功 " + notes.length + " 件（試行未記録 " + missingNotes + " 件） / " + (unknown || missingNotes ? "欠測を含む参考値" : "取得済み試行から集計"));
  const byTask = grouped(current,taskKey).sort((a,b) => b.input+b.output-a.input-a.output);
  const generations = new Map(grouped(notes,taskKey).map(g => [g.label,g.count]));
  table("tasks", ["対象タスク（title）", "ファイル名", "ソース / タスクID", "モデル", "入力", "出力", "修復の入出力", "試行 / 不明", "生成成功回数"],
    byTask.slice(0,50).map(g => [g.rows[0].displayTitle || "不明",g.rows[0].noteFile || "未取得",g.label,
      [...new Set(g.rows.map(r => r.displayModel))].join("\n"),number(g.input),number(g.output),number(tokens(g.rows.filter(repair))),
      g.count + " / " + g.unknown,number(generations.get(g.label) || 0)]), [0,1,2,3]);
  renderRanking(byTask); renderCost(); renderDetails(); renderDiagnostics();
}
for (const id of [...filters,"from","to","threadId","scenario"]) $(id).addEventListener("input", () => { page = diagnosticPage = 0; render(); });
for (const id of ["period","timelineMode"]) $(id).addEventListener("input", () => { hiddenSeries.clear(); renderTimeline(); });
for (const id of ["severity","diagnosticCategory","diagnosticSearch"]) $(id).addEventListener("input", () => { diagnosticPage = 0; renderDiagnostics(); });
$("prev").onclick = () => { page--; renderDetails(); }; $("next").onclick = () => { page++; renderDetails(); };
$("diagnosticPrev").onclick = () => { diagnosticPage--; renderDiagnosticPage(); }; $("diagnosticNext").onclick = () => { diagnosticPage++; renderDiagnosticPage(); };
render();
