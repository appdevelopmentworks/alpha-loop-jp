/* Buildless, same-origin dashboard. All imported strings are rendered as text. */
(() => {
  "use strict";
  const gradeLabel = {synthetic:"合成データ", reconstructed:"再構成・参考集計", observed:"観測データ", vendor_pit:"時点保証データ"};
  const stageLabel = {EARLY:"初動", PREMOVE:"未上昇", WATCH:"材料監視"};
  const marketLabel = {Prime:"プライム", Standard:"スタンダード", Growth:"グロース"};
  const outcomeLabel = {pending:"評価待ち", horizon_incomplete:"翌営業日待ち", price_unknown:"株価不明", adjustment_unknown:"調整不明"};
  const executionLabel = {unknown:"不明", unfilled:"未約定", limit_up:"ストップ高", halted:"売買停止", proxy_only:"価格proxyのみ", cost_unset:"費用未設定"};
  const cohortKey = day => JSON.stringify([day.data_grade, day.strategy_id, day.strategy_version, day.threshold,
    ["observed","vendor_pit"].includes(day.data_grade) ? day.prediction_eligible : false]);
  const stats = days => {
    const rows = days.flatMap(d => d.candidates), known = rows.filter(r => typeof r.hit === "boolean");
    const hits = known.filter(r => r.hit).length;
    return {candidates:rows.length, evaluated:known.length, hits, misses:known.length-hits,
      unknown:rows.length-known.length, hit_rate:known.length ? hits/known.length : null};
  };
  const filterDays = (days, cohort, start, end) => days.filter(d => cohortKey(d) === cohort && d.session >= start && d.session <= end);
  const shiftDate = (value, delta) => { const date = new Date(value + "T00:00:00Z"); date.setUTCDate(date.getUTCDate()+delta); return date.toISOString().slice(0,10); };
  const csvCell = value => {
    let text = value == null ? "" : String(value);
    if (typeof value === "string" && /^[=+\-@\t\r\n]/.test(text)) text = "'"+text;
    return '"'+text.replaceAll('"','""')+'"';
  };
  if (typeof module !== "undefined") module.exports = {stats, cohortKey, filterDays, shiftDate, csvCell};
  if (typeof document === "undefined") return;
  const $ = id => document.getElementById(id);
  const number = n => n.toLocaleString("ja-JP");
  const percent = n => n == null ? "—" : (n*100).toFixed(1)+"%";
  const text = (id, value) => { $(id).textContent = value; };
  const node = (tag, value, cls) => { const element = document.createElement(tag); if (value != null) element.textContent=value; if(cls) element.className=cls; return element; };
  const timestamp = value => value ? new Date(value).toLocaleString("ja-JP", {timeZone:"Asia/Tokyo",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"})+" JST" : "保存データなし";
  let data, cohort, period=30, selected;

  function download(day, results) {
    const columns = ["候補日","翌営業日","データ区分","条件版","コード","銘柄名","市場","候補区分"];
    if (results) columns.push("判定状態","的中","翌日高値上昇率","約定情報");
    const lines = [columns.map(csvCell).join(",")];
    for (const r of day.candidates) {
      const values = [day.session,day.next_session,day.data_grade,day.strategy_version,r.symbol,r.name,r.market,r.stage];
      if (results) values.push(r.outcome_status,r.hit,r.high_return,r.execution_status);
      lines.push(values.map(csvCell).join(","));
    }
    const url = URL.createObjectURL(new Blob(["\uFEFF"+lines.join("\r\n")+"\r\n"],{type:"text/csv;charset=utf-8"}));
    const a=node("a"); a.href=url; a.download=`${day.session}_${results ? "翌日結果" : "候補一覧"}.csv`; a.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  }

  function drawChart(days) {
    $("chart").replaceChildren();
    if (!days.length) { $("chart").append(node("p","この期間には保存データがありません。","empty")); return; }
    const ns="http://www.w3.org/2000/svg", width=Math.max(600,days.length*44), height=215, bottom=180, top=18;
    const svg=document.createElementNS(ns,"svg"); svg.setAttribute("viewBox",`0 0 ${width} ${height}`);
    svg.setAttribute("role","img"); svg.setAttribute("aria-label","日別の的中、不的中、未評価件数。数値は下の日次一覧でも確認できます。");
    if (days.length>16) svg.style.minWidth=width+"px";
    const make=(tag,attributes,label)=>{const e=document.createElementNS(ns,tag);for(const [k,v] of Object.entries(attributes))e.setAttribute(k,v);if(label!=null)e.textContent=label;svg.append(e);return e;};
    const maximum=Math.max(1,...days.map(d=>d.stats.candidates));
    for(let i=0;i<=4;i++){const y=bottom-(bottom-top)*i/4;make("line",{x1:35,x2:width-12,y1:y,y2:y,stroke:"#e7edf1"});make("text",{x:26,y:y+3,"text-anchor":"end"},String(Math.round(maximum*i/4)));}
    const spacing=(width-55)/days.length, barWidth=Math.min(28,spacing*.6);
    days.forEach((day,i)=>{
      const x=40+i*spacing+(spacing-barWidth)/2;let y=bottom;
      for(const [key,color] of [["hits","#099788"],["misses","#8ba3b9"],["unknown","#e0e7ee"]]){
        const h=day.stats[key]/maximum*(bottom-top);y-=h;const rect=make("rect",{x,y,width:barWidth,height:h,fill:color,rx:2});
        const title=document.createElementNS(ns,"title");title.textContent=`${day.session}：的中 ${day.stats.hits} / 不的中 ${day.stats.misses} / 未評価・不明 ${day.stats.unknown}`;rect.append(title);
      }
      if(days.length<=16||i%Math.ceil(days.length/12)===0)make("text",{x:x+barWidth/2,y:202,"text-anchor":"middle"},day.session.slice(5).replace("-","/"));
    });
    $("chart").append(svg);
  }

  function renderDetails(day) {
    $("candidates").replaceChildren();
    if(!day){text("details-heading","銘柄別の結果");text("detail-note","表示する候補日がありません。");return;}
    text("details-heading",day.session+" の候補");
    text("detail-note",`${gradeLabel[day.data_grade]} · 価格基準 ${day.strategy_version} · ${day.candidates.length}銘柄。日次一覧の候補日を押すと切り替わります。`);
    const query=$("search").value.trim().toLowerCase();
    const rows=day.candidates.filter(r=>(r.symbol+" "+r.name).toLowerCase().includes(query));
    for(const r of rows){
      const tr=node("tr"), company=node("td");company.append(node("strong",r.symbol),node("small",r.name));tr.append(company);
      tr.append(node("td",marketLabel[r.market]||r.market),node("td",stageLabel[r.stage]||r.stage));
      tr.append(node("td",percent(r.high_return),r.high_return!=null ? r.high_return>=0 ? "positive":"negative" : ""));
      const verdict=node("td");verdict.append(node("span",r.hit===true?"的中":r.hit===false?"不的中":outcomeLabel[r.outcome_status]||"不明",r.hit===true?"badge hit":r.hit===false?"badge":"badge pending"));
      tr.append(verdict,node("td",executionLabel[r.execution_status]||r.execution_status));$("candidates").append(tr);
    }
    if(!rows.length){const tr=node("tr"),td=node("td",day.candidates.length?"検索に一致する銘柄はありません。":"候補ゼロ。抽出処理は完了しています。");td.colSpan=6;tr.append(td);$("candidates").append(tr);}
  }

  function render() {
    const all=data.days.filter(d=>cohortKey(d)===cohort).sort((a,b)=>a.session.localeCompare(b.session));
    const latest=all.at(-1), days=filterDays(all,cohort,$("start").value,$("end").value), sum=stats(days);
    text("updated",timestamp(data.updated_at));
    const grade=latest?.data_grade, threshold=latest?.threshold ?? .1;
    const note=grade==="synthetic" ? "合成データのデモです。表示件数・的中率は実市場の予測成績ではありません。" : grade==="reconstructed" ? "再構成データによる事後の参考集計です。事前予測の的中率としては扱いません。" : "保存済みの価格基準候補を表示しています。事前予測への算入可否は実行時点・データ品質によります。";
    text("notice",latest ? note : "保存データがありません。日次処理後に更新されます。");$("notice").className="notice"+(grade==="synthetic"?" demo":"");
    if(latest){
      text("latest-heading",latest.session+" の候補");text("latest-count",number(latest.stats.candidates));
      text("latest-description",`${latest.next_session?latest.next_session+"（翌営業日）":"翌営業日未保存"} の上昇候補 · 価格基準 ${latest.strategy_version} · ${latest.batch_status}${latest.missing_target_count?" / 欠損 "+latest.missing_target_count+"銘柄":""}`);
      text("latest-status",latest.result_saved ? `翌日結果：保存済み（不明 ${latest.stats.unknown}件）` : "翌日結果：評価待ち");
      $("latest-csv").disabled=false;$("latest-csv").onclick=()=>download(latest,false);
    }else{text("latest-count","—");text("latest-description","—");text("latest-status","—");$("latest-csv").disabled=true;}
    text("metric-definition",`翌日高値で${(threshold*100).toFixed(0)}%以上上昇`);text("rate",percent(sum.hit_rate));
    text("candidate-total",number(sum.candidates));text("available-days",`保存済み ${days.length}営業日分`);
    text("hit-total",`${sum.hits} / ${sum.evaluated}`);text("miss-total",number(sum.misses));text("unknown-total",number(sum.unknown));
    drawChart(days);
    const hitEnd=sum.candidates?sum.hits/sum.candidates*360:0, missEnd=sum.candidates?(sum.hits+sum.misses)/sum.candidates*360:0;
    $("donut").style.background=`conic-gradient(#099788 0deg ${hitEnd}deg,#8ba3b9 ${hitEnd}deg ${missEnd}deg,#e0e7ee ${missEnd}deg 360deg)`;
    $("donut-label").replaceChildren(node("span",percent(sum.hit_rate)),node("small","評価済みの的中率"));
    text("period-note",`的中 ${sum.hits} / 不的中 ${sum.misses} / 未評価・不明 ${sum.unknown}。実際に保存されている日だけを集計しています。`);
    text("range-label",`${$("start").value} — ${$("end").value}`);$("empty").hidden=days.length!==0;$("history").replaceChildren();
    for(const day of [...days].reverse()){
      const tr=node("tr"), dateCell=node("td"), pick=node("button",day.session,"session-button");
      pick.onclick=()=>{selected=day;renderDetails(day);};dateCell.append(pick,node("small","→ "+(day.next_session||"翌営業日未保存")));tr.append(dateCell);
      tr.append(node("td",number(day.stats.candidates)),node("td",`${day.stats.hits} / ${day.stats.evaluated}`),node("td",percent(day.stats.hit_rate)));
      const status=node("td");status.append(node("span",day.result_saved?"保存済み":"評価待ち",day.result_saved?"badge hit":"badge pending"));
      if(day.stats.unknown)status.append(node("small",`未評価・不明 ${day.stats.unknown}件`));tr.append(status);
      const files=node("td"),candidate=node("button","候補CSV","table-action"),outcome=node("button","結果CSV","table-action");candidate.onclick=()=>download(day,false);outcome.disabled=!day.result_saved;outcome.onclick=()=>download(day,true);files.append(candidate,outcome);tr.append(files);$("history").append(tr);
    }
    if(!selected||!days.includes(selected))selected=days.at(-1);renderDetails(selected);
  }

  function setPeriod(value) {
    period=value;
    const latest=data.days.filter(d=>cohortKey(d)===cohort).map(d=>d.session).sort().at(-1);
    if(value!=="custom"&&latest){$("end").value=latest;$("start").value=shiftDate(latest,1-Number(value));}
    for(const button of document.querySelectorAll("[data-period]")){const active=button.dataset.period===String(value);button.classList.toggle("active",active);button.setAttribute("aria-pressed",String(active));}
    render();
  }
  async function load() {
    try{
      const response=await fetch("./data/dashboard.json",{cache:"no-store"});if(!response.ok)throw Error("load");data=await response.json();
      if(data.schema_version!==1||!Array.isArray(data.days))throw Error("schema");
      const groups=new Map(data.days.map(d=>[cohortKey(d),d]));
      for(const [key,day] of groups){const eligibility=["observed","vendor_pit"].includes(day.data_grade)?(day.prediction_eligible?"・事前予測":"・事後集計"):"";const option=node("option",`${gradeLabel[day.data_grade]}${eligibility} / ${day.strategy_version} / 高値${day.threshold*100}%`);option.value=key;$("cohort").append(option);}
      cohort=groups.keys().next().value;$("cohort").value=cohort||"";
      $("cohort").onchange=()=>{cohort=$("cohort").value;selected=null;setPeriod(period);};
      for(const button of document.querySelectorAll("[data-period]"))button.onclick=()=>setPeriod(button.dataset.period);
      for(const id of ["start","end"])$(id).onchange=()=>setPeriod("custom");
      $("search").oninput=()=>renderDetails(selected);setPeriod(30);
    }catch(error){text("notice","保存データを読み込めませんでした。HTTPで開いているか、公開用データの配置を確認してください。前回の集計値として表示はしません。");$("notice").className="notice error";}
  }
  load();
})();
