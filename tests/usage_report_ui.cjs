// Exercise the shipped script with a small DOM adapter, without network or browser dependencies.
const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const resources = path.join(__dirname, "../src/tkn_codex_chat_note/resources");
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.options = []; this.style = {}; this.attrs = {}; this.events = {}; this.value = ""; this.content = ""; }
  set textContent(v) { this.children = []; this.content = String(v ?? ""); }
  get textContent() { return this.content + this.children.map(c => c.textContent).join(" "); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.content = ""; this.children = children; }
  add(option) { if (!this.options.length) this.value = option.value; this.options.push(option); }
  setAttribute(k,v) { this.attrs[k] = v; }
  addEventListener(k,fn) { this.events[k] = fn; }
}
function start(records, diagnostics = [], notes = []) {
  const html = fs.readFileSync(path.join(resources,"usage_report.html"),"utf8"), elements = new Map();
  for (const match of html.matchAll(/<(\w+)\b[^>]*\bid="([^"]+)"[^>]*>/g)) {
    assert(!elements.has(match[2]), "duplicate HTML id"); elements.set(match[2],new Element(match[1]));
  }
  for (const match of html.matchAll(/<select\b[^>]*id="([^"]+)"[^>]*>(.*?)<\/select>/gs)) {
    for (const option of match[2].matchAll(/<option value="([^"]*)">([^<]*)<\/option>/g)) {
      elements.get(match[1]).add({value:option[1],text:option[2]});
    }
  }
  elements.get("usage-data").textContent = JSON.stringify({records,diagnostics,notes,priceScenarios:{},sources:[],utcOffsetMinutes:540});
  const get = id => { assert(elements.has(id),"Missing template element: " + id); return elements.get(id); };
  const context = vm.createContext({document:{getElementById:get,createElement:t=>new Element(t),createElementNS:(_,t)=>new Element(t)},
    Option:function(text,value){this.text=text;this.value=value;}});
  vm.runInContext(fs.readFileSync(path.join(resources,"usage_report.js"),"utf8"),context);
  return {get, set:(id,value)=>{get(id).value=value;get(id).events.input();},
    rows:id=>get(id).children[0].children[1].children.map(r=>r.children.map(c=>c.textContent))};
}
const row = (extra={}) => ({sourceId:"source",threadId:"task-one",runId:"run",provider:"codex",command:"pull",generationProfile:"high",
  displayTitle:"Readable 日本語",taskTitle:"Old title",noteFile:"summary.md",displayModel:"model-a",date:"2026-09-19",stage:"chunk",
  status:"received",noteStatus:"generated",usageComplete:true,inputTokens:100,outputTokens:20,cachedInputTokens:80,reasoningTokens:15,...extra});
const diagnostic = (extra={}) => ({sourceId:"source",runId:"run",provider:"codex",command:"pull",generationProfile:"high",date:"2026-09-19",
  severity:"ERROR",category:"run-failure",message:"HTTP 429",models:["model-a"],displayModel:"model-a",...extra});

test("timeline modes, legend toggles and weekly buckets preserve usage totals",()=>{
  const ui=start([row(),row({displayModel:"model-b",inputTokens:50,outputTokens:10,date:"2026-09-20",stage:"chunk-repair"}),
    row({displayModel:"model-b",inputTokens:null,knownInputTokens:5,outputTokens:null,knownOutputTokens:2,usageComplete:false,date:"2026-09-20"})]);
  assert.equal(ui.get("input").textContent,"155");
  assert.equal(ui.get("output").textContent,"32");
  ui.set("timelineMode","model");
  assert.deepEqual(ui.rows("timelineValues"),[["2026-09-19","120","0","0"],["2026-09-20","0","67","1"]]);
  ui.get("timelineLegend").children[0].onclick();
  assert.deepEqual(ui.rows("timelineValues"),[["2026-09-19","0","0"],["2026-09-20","67","1"]]);
  assert.equal(ui.get("input").textContent,"155", "Legend does not change global metrics");
  ui.set("timelineMode","model-io");
  assert.deepEqual(ui.rows("timelineValues")[1],["2026-09-20","0","0","55","12","1"]);
  ui.set("period","week");
  assert.deepEqual(ui.rows("timelineValues"),[["2026-09-14","100","20","55","12","1"]]);
  assert(ui.rows("models").some(c=>c[0]==="model-b" && c[5]==="60 / 89.6%"));
});
test("titles, filename search, ranking drilldown and empty results",()=>{
  const evil="</script><script>alert(1)</script>";
  const ui=start([row(),row({threadId:"task-two",displayTitle:evil,noteFile:"second.md",inputTokens:300})]);
  assert(ui.rows("tasks").some(c=>c[0]===evil && c[1]==="second.md"));
  ui.set("threadId","SUMMARY.MD");
  assert.equal(ui.rows("tasks").length,1); assert.equal(ui.rows("tasks")[0][0],"Readable 日本語");
  ui.set("threadId","Old title"); assert.equal(ui.rows("tasks").length,2);
  ui.set("threadId",""); ui.get("ranking").children[0].children[0].onclick();
  assert.equal(ui.rows("tasks").length,1); assert.equal(ui.rows("tasks")[0][0],evil);
  ui.set("threadId","does-not-exist"); assert.equal(ui.get("empty").hidden,false); assert.equal(ui.rows("tasks").length,0);
});
test("diagnostics without calls, severity, model association, reason search and paging",()=>{
  const rows=Array.from({length:51},(_,i)=>diagnostic({message:"error " + i}));
  rows.push(diagnostic({severity:"WARNING",category:"validation",message:"fixed citation",noteStatus:"generated"}));
  rows.push(diagnostic({severity:"INFO",category:"deferred",message:"runtime-deadline",models:["unknown"],displayModel:"unknown"}));
  const ui=start([],rows);
  assert.equal(ui.get("empty").hidden,false);
  assert.equal(ui.rows("diagnostics").length,50);
  ui.get("diagnosticNext").onclick(); assert.equal(ui.rows("diagnostics").length,2);
  ui.set("diagnosticSearch","citation"); assert.equal(ui.rows("diagnostics").length,1);
  assert(ui.rows("diagnostics")[0][4].includes("generated"));
  ui.set("diagnosticSearch",""); ui.set("severity","INFO"); assert.equal(ui.rows("diagnostics").length,1);
  ui.set("displayModel","model-a"); assert.equal(ui.rows("diagnostics").length,0);
  ui.set("severity","WARNING"); assert.equal(ui.rows("diagnostics").length,1);
  ui.set("diagnosticCategory","run-failure"); assert.equal(ui.rows("diagnostics").length,0);
});
