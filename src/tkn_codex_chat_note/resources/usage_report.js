"use strict";
const data=JSON.parse(document.getElementById("usage-data").textContent), $=id=>document.getElementById(id);
const nf=new Intl.NumberFormat("ja-JP"), number=v=>v==null?"不明":nf.format(v), compact=v=>new Intl.NumberFormat("ja-JP",{notation:"compact",maximumFractionDigits:1}).format(v);
const text=(id,value)=>{$(id).textContent=value;}, filters=["sourceId","provider","displayModel","command","generationProfile"];
let page=0,current=[];
for(const key of filters){const select=$(key);select.add(new Option("すべて",""));for(const v of [...new Set(data.records.map(r=>r[key]||"unknown"))].sort())select.add(new Option(v,v));}
for(const name of Object.keys(data.priceScenarios))$("scenario").add(new Option(name,name));
if(!$("scenario").options.length)$("scenario").add(new Option("単価未設定",""));
const offset=data.utcOffsetMinutes,zone="UTC"+(offset<0?"-":"+")+String(Math.floor(Math.abs(offset)/60)).padStart(2,"0")+":"+String(Math.abs(offset)%60).padStart(2,"0");
text("freshness","生成日時: "+data.builtAt+" / 集計タイムゾーン: "+zone+" / 入力 "+data.sources.length+" ファイル");
text("snapshot","Snapshot: "+data.snapshotId);
for(const source of data.sources){const p=document.createElement("p");p.textContent=source.path+" — SHA-256 "+source.sha256;$("sources").append(p);}
if(data.warnings.length){const div=document.createElement("div");div.className="notice";div.textContent="使用量を復元できない過去記録: "+data.warnings.length+" 件。詳細はJSONの warnings を参照してください。";$("warnings").append(div);}
function matches(r,includeModel=true){return filters.every(key=>key==="displayModel"&&!includeModel||!$(key).value||$(key).value===(r[key]||"unknown"))&&(!$("from").value||r.date&&r.date>=$("from").value)&&(!$("to").value||r.date&&r.date<=$("to").value)&&String(r.threadId||"").includes($("threadId").value);}
function total(rows,key){return rows.reduce((s,r)=>s+(r[key]??r["known"+key[0].toUpperCase()+key.slice(1)]??0),0);}
function grouped(rows,key){const groups=new Map();for(const r of rows){const label=typeof key==="function"?key(r):(r[key]||"unknown");if(!groups.has(label))groups.set(label,[]);groups.get(label).push(r);}return [...groups].map(([label,rs])=>({label,input:total(rs,"inputTokens"),output:total(rs,"outputTokens"),count:rs.length,unknown:rs.filter(r=>!r.usageComplete).length}));}
function table(id,headers,rows){const table=document.createElement("table"),head=document.createElement("thead"),tr=document.createElement("tr");for(const h of headers){const th=document.createElement("th");th.scope="col";th.textContent=h;tr.append(th);}head.append(tr);table.append(head);const body=document.createElement("tbody");for(const cells of rows){const row=document.createElement("tr");cells.forEach((v,i)=>{const cell=document.createElement("td");cell.textContent=v;if(i===0)cell.className="label";row.append(cell);});body.append(row);}table.append(body);$(id).replaceChildren(table);}
function svgNode(name,attrs={},label){const e=document.createElementNS("http://www.w3.org/2000/svg",name);for(const [key,v]of Object.entries(attrs))e.setAttribute(key,String(v));if(label!=null)e.textContent=label;return e;}
function chart(id,groups){const w=Math.max(640,groups.length*38+60),h=260,max=Math.max(1,...groups.map(g=>g.input+g.output)),svg=svgNode("svg",{viewBox:"0 0 "+w+" "+h,role:"img","aria-label":"入力と出力の積み上げ棒グラフ"});svg.style.minWidth=w+"px";const step=(w-80)/Math.max(groups.length,1);
for(let j=0;j<4;j++){const y=25+j*55;svg.append(svgNode("line",{x1:65,x2:w-10,y1:y,y2:y,stroke:"#d9e3e7"}));svg.append(svgNode("text",{x:58,y:y+4,"text-anchor":"end",fill:"#536774","font-size":11},compact(max*(1-j/3))));}
groups.forEach((g,i)=>{const x=65+i*step+step*.16,bw=step*.68;let y=190;for(const [value,fill]of [[g.input,"#117c80"],[g.output,"#e18b39"]]){const height=value/max*165;y-=height;const rect=svgNode("rect",{x,y,width:bw,height,fill});rect.append(svgNode("title",{},g.label+": 入力 "+number(g.input)+" / 出力 "+number(g.output)+" / 不明 "+g.unknown+" 試行"));svg.append(rect);}if(groups.length<=8||i%Math.ceil(groups.length/8)===0)svg.append(svgNode("text",{x:x+bw/2,y:218,"text-anchor":"middle",fill:"#536774","font-size":10},g.label.length===10?g.label.slice(5):g.label));if(g.unknown)svg.append(svgNode("text",{x:x+bw/2,y:242,"text-anchor":"middle",fill:"#825900","font-size":10},"欠測"));});$(id).replaceChildren(svg);}
function period(r){if(!r.date)return"日付不明";if($("period").value==="month")return r.date.slice(0,7);if($("period").value==="week"){const d=new Date(r.date+"T00:00:00Z");d.setUTCDate(d.getUTCDate()-(d.getUTCDay()+6)%7);return d.toISOString().slice(0,10);}return r.date;}
function renderDetails(){const rows=current.slice(page*50,(page+1)*50);table("records",["実行日時 / タスク","モデル","工程 / 結果","入力","出力","キャッシュ入力","推論","取得元"],rows.map(r=>[(r.startedAt||"不明")+" / "+(r.threadId||"不明"),r.displayModel,(r.stage||"不明")+" / "+(r.status||"不明"),number(r.inputTokens),number(r.outputTokens),number(r.cachedInputTokens),number(r.reasoningTokens),r.usageSource||"legacy"]));text("page",(current.length?page+1:0)+" / "+Math.ceil(current.length/50)+" ページ");$("prev").disabled=page===0;$("next").disabled=(page+1)*50>=current.length;}
function render(){current=data.records.filter(r=>matches(r));const count=current.length,unknown=current.filter(r=>!r.usageComplete).length;$("empty").hidden=count>0;text("input",count?number(total(current,"inputTokens")):"—");text("output",count?number(total(current,"outputTokens")):"—");for(const [id,key]of [["inputCoverage","inputTokens"],["outputCoverage","outputTokens"]])text(id,"不明を含む試行 "+current.filter(r=>r[key]==null).length+" / "+count);text("coverage",(count-unknown)+" / "+count);text("status","未完了 "+current.filter(r=>!r.finishedAt&&r.status==="started").length+" · 受信成功以外 "+current.filter(r=>r.status!=="received").length);
chart("timeline",grouped(current,period).sort((a,b)=>a.label.localeCompare(b.label)));
const groups=grouped(current,"displayModel").sort((a,b)=>b.input+b.output-a.input-a.output);
table("models",["モデル","入力","出力","不明試行"],groups.map(g=>[g.label,number(g.input),number(g.output),number(g.unknown)]));
table("commands",["コマンド","入力","出力","試行 / 不明"],grouped(current,"command").map(g=>[g.label,number(g.input),number(g.output),g.count+" / "+g.unknown]));
const byTask=grouped(current,r=>r.sourceId+" / "+r.threadId).sort((a,b)=>b.input+b.output-a.input-a.output);
table("tasks",["対象タスク","入力","出力","試行 / 不明"],byTask.slice(0,50).map(g=>[g.label,number(g.input),number(g.output),g.count+" / "+g.unknown]));
const selectedKeys=new Set(current.map(r=>r.sourceId+"/"+r.runId+"/"+r.threadId));
const notes=data.notes.filter(n=>matches(n,false)&&(!$("displayModel").value||selectedKeys.has(n.sourceId+"/"+n.runId+"/"+n.threadId)));
text("perNote",notes.length&&count?number(Math.round((total(current,"inputTokens")+total(current,"outputTokens"))/notes.length))+" tokens":"—");
text("noteCount","生成成功 "+notes.length+" 件 / "+(unknown?"欠測を含む参考値":"取得済み試行から集計"));
const name=$("scenario").value,price=data.priceScenarios[name];
if(price){const known=current.map(r=>r.referenceCosts[name]).filter(v=>v!=null),cost=known.reduce((a,b)=>a+b,0);
text("cost",known.length?new Intl.NumberFormat("ja-JP",{style:"currency",currency:price.currency,maximumFractionDigits:4}).format(cost):"算出できません");
text("costCoverage","算出可能 "+known.length+" / "+count+" 試行。算出できた分のみの合計です。");
text("priceDetail","単価基準日 "+price.pricing_date+" / 100万tokens: 入力 "+price.input_per_million+", 出力 "+price.output_per_million+" "+price.currency+" / キャッシュ: "+(price.cache_policy==="no-cache"?"全入力を通常単価で試算":"実測内訳を使用・読込 "+price.cached_input_per_million+", 書込 "+(price.cache_write_per_million??"通常入力単価")));
}else{text("cost","単価未設定");text("costCoverage","usage_report.price_scenarios に比較用単価を設定し、build-report を再実行してください。");text("priceDetail","");}renderDetails();}
for(const id of [...filters,"from","to","threadId","period","scenario"])$(id).addEventListener("input",()=>{page=0;render();});
$("prev").onclick=()=>{page--;renderDetails();};$("next").onclick=()=>{page++;renderDetails();};render();

