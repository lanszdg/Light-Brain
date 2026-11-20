const $ = id => document.getElementById(id);
const kind = {
  goal:["目标","#b2f5d5",0,"◈"], representation:["候选解释","#d0baff",1,"◇"],
  operator:["开发动作","#b6a2eb",1,"⌁"], bound_operation:["派工依据","#bca7e9",1,"↳"],
  criterion:["验收条件","#e4c289",1,"≡"], job:["作业","#8dd4ed",2,"→"],
  receipt:["执行回执","#8fdcc2",3,"∿"], check_result:["实测结果","#95e8bd",3,"✓"],
  git_commit:["源码版本","#9dc6c3",4,"⌘"], delivery:["已交付","#efce86",4,"◆"],
};
const jobs = {plan:"理解需求",template:"渲染模板",design:"交互试演",develop:"开发应用",
  verify:"独立验收",build:"构建源码包",verify_export:"导出复验",release:"登记发布",
  review:"代码评审",inquiry_probe:"验证解释",reuse_implementation:"复用源码",method_verify:"重放验收"};
const relations = {intended:"目标要求",requires:"需要",selects:"选中",interprets:"解释",
  authorizes:"授权派工",realized_by:"实际执行",input_version:"输入版本",reported:"回传",
  concerns_version:"对应版本",observed:"实测",acceptance:"验收条件",supports:"支持",accepted_by:"完成交付"};
const typeOf = n => kind[n.type] || [n.type || "节点","#a6bfd8",3];
const caption = n => ({
  job:jobs[n.kind], receipt:n.ok ? "执行通过" : n.ok === false ? "执行失败" : "回执",
  git_commit:n.sha?.slice(0,9), check_result:n.condition, delivery:"作品可交付",
  operator:n.instruction, criterion:n.condition?.id, bound_operation:n.operator
})[n.type] || typeOf(n)[0];
const params = new URLSearchParams(location.search);
const project = params.get("project");
let token, base, graph, selected, coords = new Map();
let x=0,y=0,scale=1,drag, fitted=false, loading=false, lastSignature, active;
let mode="topology", isolated=false, revisionsNow=[];
const viewport=$("viewport");
const demoNodes=[
  {id:"goal",type:"goal",text:"为排练团队制作一块计时板。添加段落后，时间轴要立即反映累计时长。",version:2},
  {id:"route-live",type:"representation",label:"逐段累计",text:"每一段显示自己的结束时间，删除前一段后，后续时间重新计算。"},
  {id:"route-fixed",type:"representation",label:"只显示总时长",text:"只展示总量，每段的开始和结束时间留空。"},
  {id:"design",type:"operator",label:"排练时间轴",instruction:"将段落编排为可操作的时间轴"},
  {id:"criteria-browser",type:"criterion",label:"删除后重算",condition:{id:"删除后重算",kind:"browser"}},
  {id:"criteria-http",type:"criterion",label:"HTTP 正常响应",condition:{id:"HTTP 正常响应",kind:"http"}},
  {id:"make",type:"job",kind:"develop"},
  {id:"verify",type:"job",kind:"verify"},
  {id:"v1",type:"git_commit",sha:"74a90f1"},
  {id:"receipt-1",type:"receipt",ok:false,label:"第一次复验失败"},
  {id:"fail",type:"check_result",condition:"删段后累计未更新",passed:false},
  {id:"repair",type:"job",kind:"develop",label:"修复累计逻辑"},
  {id:"v2",type:"git_commit",sha:"c921ed8"},
  {id:"verify2",type:"job",kind:"verify",label:"再次验收"},
  {id:"http",type:"check_result",condition:"HTTP 200",passed:true},
  {id:"result",type:"check_result",condition:"累计值已更新",passed:true},
  {id:"pack",type:"job",kind:"build"},
  {id:"export",type:"job",kind:"verify_export"},
  {id:"receipt-2",type:"receipt",ok:true,label:"归档验证通过"},
  {id:"release",type:"delivery",goal_version:2},
];
const demoPosition={
  goal:[500,264], "route-live":[404,141],"route-fixed":[244,140],design:[170,240],"criteria-browser":[143,327],"criteria-http":[262,419],
  make:[403,381],verify:[196,487],v1:[365,493],"receipt-1":[533,510],fail:[532,426],repair:[663,397],
  v2:[695,486],verify2:[817,374],http:[824,245],result:[903,305],pack:[903,455],export:[924,166],
  "receipt-2":[752,125],release:[673,214],
};
const demoEdges=[
  ["goal","route-live","interprets"],["goal","route-fixed","interprets"],["route-live","design","selects"],
  ["goal","criteria-browser","acceptance"],["goal","criteria-http","acceptance"],
  ["design","make","realized_by"],["make","v1","reported"],["v1","verify","input_version"],["criteria-browser","verify","supports"],
  ["verify","receipt-1","reported"],["receipt-1","fail","observed"],["fail","repair","supports"],
  ["repair","v2","reported"],["v2","verify2","input_version"],["criteria-browser","verify2","supports"],
  ["criteria-http","verify2","supports"],["verify2","result","observed"],["verify2","http","observed"],
  ["result","pack","supports"],["http","export","supports"],["pack","export","input_version"],
  ["export","receipt-2","reported"],["receipt-2","release","accepted_by"],
];
const demo={revision:12,nodes:demoNodes,edges:demoEdges.map(([source,target,type])=>({source,target,type}))};
function element(tag,text) {
  const item=document.createElement(tag); if(text!==undefined)item.textContent=text;
  return item;
}
function title(n) {
  return n.label || (n.type==="goal" ? "用户委托" : caption(n));
}
function message(text="") { $("feedback").textContent=text; $("feedback").hidden=!text; }
function transform(){
  $("plane").style.transform=`translate(${x}px,${y}px) scale(${scale})`;
  $("zoom-value").textContent=Math.round(scale*100)+"%";
  drawMinimap();
}
function fit(){
  const board=$("links");
  const span=Math.max(1,board.width);
  scale=Math.max(.30,Math.min(1.12,viewport.clientWidth/span,(viewport.clientHeight-40)/Math.min(board.height,800)));
  x=Math.max(0,(viewport.clientWidth-span*scale)/2);y=Math.max(8,(viewport.clientHeight-board.height*scale)/2);
  transform();
}
function zoom(f){
  const next=Math.min(2.5,Math.max(.35,scale*f)), cx=viewport.clientWidth/2, cy=viewport.clientHeight/2;
  x=cx-(cx-x)*next/scale;y=cy-(cy-y)*next/scale;scale=next;transform();
}
function inspect(id, focus=false){
  const n=graph?.nodes.find(n=>n.id===id);
  if(!n)return;
  selected=id;
  $("detail-type").textContent=typeOf(n)[0];
  $("detail-title").textContent=title(n);
  $("detail-copy").textContent=n.text || n.instruction || "";
  const meta=$("detail-meta");
  meta.replaceChildren();
  for (const [label,value] of [["图谱修订",graph.revision],["目标版本",n.goal_version??n.version],
    ["Git 提交",n.sha??n.candidate_sha],["验收结果",typeof n.passed==="boolean"?n.passed?"通过":"未通过":undefined],
    ["前置证据",n.support?.length],["方法版本",n.method_revision?.revision]]) {
    if(value!==undefined&&value!==null)meta.append(element("dt",label),element("dd",String(value)));
  }
  const list=$("neighbors");
  list.replaceChildren();
  const nodeById=new Map(graph.nodes.map(n=>[n.id,n]));
  for(const [side,label] of [["target","前置与依据"],["source","去向与结果"]]){
    const edges=graph.edges.filter(e=>e[side]===id);
    if(!edges.length)continue;
    const block=element("section"), opposite=side==="target"?"source":"target";
    block.className="neighbor-group";block.append(element("h3",label+" · "+edges.length));
    for(const edge of edges.slice(0,20)){
      const related=nodeById.get(edge[opposite]);
      if(!related)continue;
      const button=element("button",title(related).slice(0,72));
      button.append(element("span",relations[edge.type]||edge.type));
      button.addEventListener("click",()=>{
        if(!coords.has(related.id)){ $("filter").value="";draw(); }
        inspect(related.id,true);
      });
      block.append(button);
    }
    list.append(block);
  }
  const highlight=trace(id);
  for(const button of $("nodes").children){
    const on=button.dataset.id===id;
    button.setAttribute("aria-pressed",String(on));
    button.classList.toggle("muted",isolated&&!highlight.has(button.dataset.id));
    if(on&&focus){
      const pos=coords.get(id);
      x=viewport.clientWidth/2-pos.x*scale; y=viewport.clientHeight/2-pos.y*scale;
      transform(); button.focus({preventScroll:true});
    }
  }
  drawLinks(highlight);
}
function trace(id) {
  const selected=new Set([id]);
  for(const [from,to] of [["source","target"],["target","source"]]){
    let step=[id];
    for(let depth=0;depth<2;depth++){
      const next=new Set(graph.edges.filter(e=>step.includes(e[from])).map(e=>e[to]));
      for(const n of next)selected.add(n);
      step=[...next];
    }
  }
  return selected;
}
const domains=["原始委托","问题和动作","尝试和运行","实测证据","交付与版本"];
function pointFor(node,rank,counts){
  const [, ,group]=typeOf(node);
  const i=rank[group]++;
  if($("filter").value)return [132+(i%5)*203,114+Math.floor(i/5)*79];
  if(mode==="layers")return [106+group*207,110+i*62];
  if(!graph.case_id&&demoPosition[node.id])return demoPosition[node.id];
  if(group===0)return [520,275];
  const bands={1:[Math.PI*1.03,Math.PI*1.77],2:[Math.PI*.24,Math.PI*.98],3:[-.77,.69],4:[Math.PI*1.63,Math.PI*1.92]};
  const [from,to]=bands[group], lanes=Math.min(group===4?8:12,counts[group]);
  const slots=Math.ceil(counts[group]/lanes), slot=Math.floor(i/lanes);
  const local=i%lanes, steps=Math.min(lanes,counts[group]-slot*lanes);
  const a=from+(to-from)*(local+.5)/steps;
  const r=1+slot*.31;
  return [520+Math.cos(a)*385*r,275+Math.sin(a)*210*r];
}
function drawMinimap(){
  const c=$("minimap"), ctx=c.getContext("2d"), plane=$("links");
  if(!plane.width||!plane.height)return;
  const sx=c.width/plane.width, sy=c.height/plane.height;
  ctx.clearRect(0,0,c.width,c.height);
  for(const p of coords.values()){
    ctx.fillStyle=p.accent;ctx.fillRect(p.x*sx-2,p.y*sy-1,4,2);
  }
  ctx.strokeStyle="#bcdfd0aa";
  ctx.strokeRect(-x*sx/scale,-y*sy/scale,viewport.clientWidth*sx/scale,viewport.clientHeight*sy/scale);
}
function drawLinks(highlight=new Set()){
  const c=$("links"), ctx=c.getContext("2d");
  ctx.clearRect(0,0,c.width,c.height);
  const core=coords.get("goal")||{x:c.width/2,y:c.height/2};
  ctx.save();
  ctx.translate(core.x,core.y);
  if(mode==="topology"&&!$("filter").value){
    for(const [w,h] of [[122,78],[230,138],[345,207],[438,250]]){
      ctx.strokeStyle=w===122?"#99efc340":"#79b5ac26";
      ctx.setLineDash(w===438?[3,8]:[2,7]);ctx.beginPath();
      ctx.ellipse(0,0,w,h,0,0,Math.PI*2);ctx.stroke();
    }
    ctx.setLineDash([]);
    for(const angle of [0,Math.PI/4,Math.PI/2,Math.PI*.75]){
      ctx.strokeStyle="#688c861e";ctx.beginPath();
      ctx.moveTo(Math.cos(angle)*-465,Math.sin(angle)*-255);
      ctx.lineTo(Math.cos(angle)*465,Math.sin(angle)*255);ctx.stroke();
    }
  }
  ctx.restore();
  if(mode==="layers"){
    ctx.fillStyle="#a7c3b9";ctx.font="12px system-ui";
    domains.forEach((label,i)=>ctx.fillText(label,54+i*207,49));
  }
  const marked=edge=>highlight.has(edge.source)&&highlight.has(edge.target);
  for(const edge of [...graph.edges.filter(e=>!marked(e)),...graph.edges.filter(marked)]){
    const a=coords.get(edge.source),b=coords.get(edge.target);
    if(!a||!b)continue;
    const yes=marked(edge);
    const rise=b.x-a.x, run=b.y-a.y;
    const start=Math.abs(rise)>75?a.x+Math.sign(rise)*a.w/2:a.x;
    const end=Math.abs(rise)>75?b.x-Math.sign(rise)*b.w/2:b.x;
    const sy=start===a.x?a.y+Math.sign(run)*a.h/2:a.y;
    const ey=end===b.x?b.y-Math.sign(run)*b.h/2:b.y;
    const middle=start+(end-start)*.51;
    ctx.beginPath();ctx.moveTo(start,sy);ctx.bezierCurveTo(middle,sy,middle,ey,end,ey);
    ctx.strokeStyle=edge.type==="accepted_by"?"#e8c67a":yes?b.accent:isolated?"#71958d24":"#7eb4aa52";
    ctx.lineWidth=yes?1.75:1;ctx.stroke();
    if(yes){
      ctx.fillStyle=ctx.strokeStyle;ctx.beginPath();ctx.arc(end,ey,2.5,0,2*Math.PI);ctx.fill();
    }
  }
}
function draw(){
  if(!graph)return;
  const filter=$("filter").value, all=graph.nodes;
  const visible=filter?all.filter(n=>n.type===filter):all;
  const counts=[0,0,0,0,0],rank=[0,0,0,0,0];
  for(const n of visible)counts[typeOf(n)[2]]++;
  coords=new Map();$("nodes").replaceChildren();
  const raw=visible.map(n=>[n,pointFor(n,rank,counts)]);
  if(graph.case_id&&mode==="topology"&&!filter)spread(raw);
  const left=Math.min(0,...raw.map(([,p])=>p[0]-82)), top=Math.min(0,...raw.map(([,p])=>p[1]-70));
  for(const [n,point] of raw){
    const [,accent,,icon]=typeOf(n);
    const w=n.type==="goal"?138:124, h=n.type==="goal"?50:35;
    const pos={x:point[0]-left,y:point[1]-top,w,h,accent:n.ok===false||n.passed===false?"#ed9189":accent};
    coords.set(n.id,pos);
    const button=element("button");
    button.className="node";button.dataset.id=n.id;button.dataset.type=n.type;
    button.style.left=pos.x-w/2+"px";button.style.top=pos.y-h/2+"px";
    button.style.setProperty("--accent",pos.accent);
    button.classList.toggle("working",n.id===active);button.classList.toggle("failed",n.ok===false||n.passed===false);
    const dot=element("span",n.ok===false||n.passed===false?"!":icon||"·");
    dot.className="symbol";dot.setAttribute("aria-hidden","true");
    button.append(dot,element("span",title(n)));button.addEventListener("click",()=>inspect(n.id));
    $("nodes").append(button);
  }
  const c=$("links");
  c.width=Math.max(1035,...[...coords.values()].map(p=>p.x+80));
  c.height=Math.max(585,...[...coords.values()].map(p=>p.y+58));
  if(!fitted){fit();fitted=true;}
  else drawMinimap();
  if(visible.some(n=>n.id===selected))inspect(selected);
  else if(visible.length)inspect(visible[0].id);
  else drawLinks();
}
function spread(items){
  // Fit neighboring cards around their relation cluster, with the actual goal as the anchor.
  for(let pass=0;pass<16;pass++){
    let collisions=0;
    for(let i=0;i<items.length;i++){
      for(let j=i+1;j<items.length;j++){
        const [a,pa]=items[i],[b,pb]=items[j];
        const dx=pb[0]-pa[0],dy=pb[1]-pa[1];
        const overlapX=134-Math.abs(dx),overlapY=(a.type==="goal"||b.type==="goal"?55:44)-Math.abs(dy);
        if(overlapX<=0||overlapY<=0)continue;
        const axis=overlapX/134<overlapY/44?0:1;
        const push=((axis===0?overlapX:overlapY)+1)*(Math.sign(axis===0?dx:dy)||1)/2;
        if(a.type!=="goal")pa[axis]-=push*(b.type==="goal"?2:1);
        if(b.type!=="goal")pb[axis]+=push*(a.type==="goal"?2:1);
        collisions++;
      }
    }
    if(!collisions)break;
  }
}
function fillOptions(select,values,selection) {
  select.replaceChildren(...values.map(([value,label])=>new Option(label,value)));
  select.value=selection;
}
function show(snapshot, revisions){
  graph=snapshot;
  revisionsNow=[...revisions].sort((a,b)=>a-b);
  const scrub=$("scrub");
  scrub.max=String(Math.max(0,revisionsNow.length-1));
  const chosen=$("history").value;
  scrub.value=String(chosen?Math.max(0,revisionsNow.indexOf(Number(chosen))):Number(scrub.max));
  $("timeline-bounds").replaceChildren(element("span","R"+(revisionsNow[0]||1)),element("span","R"+(revisionsNow.at(-1)||1)));
  $("graph-count").textContent=graph.nodes.length+" 节点 · "+graph.edges.length+" 关系";
  fillOptions($("history"),[["","最新"],...revisions.map(r=>[String(r),`R${r}`])],chosen);
  const types=[...new Set(graph.nodes.map(n=>n.type))];
  const filter=$("filter").value;
  fillOptions($("filter"),[["","全部"],...types.map(t=>[t,typeOf({type:t})[0]])],types.includes(filter)?filter:"");
  const legend=$("legend");
  legend.replaceChildren(...[["目标","#b2f5d5"],["选择与动作","#c9b5eb"],["执行","#8dd4ed"],["证据与版本","#95e8bd"],["需处理","#ed9189"],["交付","#efce86"]].map(([label,color])=>{
    const item=element("span"),dot=element("i");dot.className="dot";dot.style.setProperty("--accent",color);item.append(dot,label);return item;
  }));
  $("goal-copy").textContent=graph.nodes.find(n=>n.type==="goal")?.text?.slice(0,250)||"委托正在初始化。";
  draw();
}
async function request(path){
  const r=await fetch(path,{cache:"no-store",headers:token?{Authorization:"Bearer "+token}:{}});
  if(!r.ok)throw Error(r.status===401?"访问令牌无效。":r.status===404?"委托或图谱尚未建立。":"暂时无法读取图谱。");
  return r.json();
}
async function refresh(){
  if(!base||loading)return;
  loading=true;
  try{
    const rev=$("history").value;
    const [result,head]=await Promise.all([
      request(base+"/graph"+(rev?"?revision="+encodeURIComponent(rev):"")),
      project?null:request(base),
    ]);
    const snapshot=result.graph||result;
    const latest=head?.revision??result.revision;
    const revisions=result.revisions||Array.from({length:Math.min(120,latest)},(_,i)=>latest-i);
    active=rev ? undefined : result.active??head?.active;
    const workbench=result.graph ? snapshot.nodes.map(node=>[node.id,node.ok,node.passed,node.label]) : [];
    const key=JSON.stringify([snapshot.case_id,snapshot.revision,revisions[0],active,workbench]);
    if(key!==lastSignature){show(snapshot,revisions);lastSignature=key;}
    $("connection").lastChild.textContent=rev?"历史修订":"实时委托";
    message();
  }catch(error){message(error.message);}
  finally{loading=false;}
}
$("filter").addEventListener("change",()=>{fitted=false;draw();});
$("history").addEventListener("change",()=>{fitted=false; if(base)refresh();else {
  const rev=Number($("history").value)||12;
  const nodes=demo.nodes.slice(0,rev===12?20:rev+3), ids=new Set(nodes.map(n=>n.id));
  show({revision:rev,nodes,edges:demo.edges.filter(e=>ids.has(e.source)&&ids.has(e.target))},[12,9,6,3,1]);
}});
for(const value of ["topology","layers"]){
  $(value).addEventListener("click",()=>{
    mode=value;fitted=false;
    for(const key of ["topology","layers"])$(key).setAttribute("aria-pressed",String(key===mode));
    draw();
  });
}
$("isolate").addEventListener("click",()=>{
  isolated=!isolated;$("isolate").setAttribute("aria-pressed",String(isolated));
  if(selected)inspect(selected);
});
$("scrub").addEventListener("change",e=>{
  const value=Number(e.target.value);
  $("history").value=value===revisionsNow.length-1?"":String(revisionsNow[value]);
  $("history").dispatchEvent(new Event("change"));
});
$("latest").addEventListener("click",()=>{
  $("history").value="";$("history").dispatchEvent(new Event("change"));
});
$("minimap").addEventListener("click",e=>{
  const c=$("minimap"), plane=$("links");
  x=viewport.clientWidth/2-e.offsetX*plane.width/c.width*scale;
  y=viewport.clientHeight/2-e.offsetY*plane.height/c.height*scale;
  transform();
});
new ResizeObserver(()=>{if(graph)fit();}).observe(viewport);
$("reset").addEventListener("click",fit);
$("zoom-in").addEventListener("click",()=>zoom(1.2));
$("zoom-out").addEventListener("click",()=>zoom(1/1.2));
viewport.addEventListener("pointerdown",e=>{
  if(e.target.closest("button")||e.button!==0)return;
  drag={x:e.clientX-x,y:e.clientY-y};viewport.setPointerCapture(e.pointerId);viewport.classList.add("dragging");
});
viewport.addEventListener("pointermove",e=>{if(drag){x=e.clientX-drag.x;y=e.clientY-drag.y;transform();}});
for(const type of ["pointerup","pointercancel","lostpointercapture"])viewport.addEventListener(type,()=>{drag=null;viewport.classList.remove("dragging");});
$("connect-form").addEventListener("submit",async e=>{
  e.preventDefault();const values=new FormData(e.currentTarget);token=String(values.get("token"));
  try{
    const cases=await request("/brains/"+encodeURIComponent(values.get("brain"))+"/cases");
    fillOptions($("cases"),cases.map(c=>[c.id,(c.plan?.title||c.idea||c.id).slice(0,65)]),cases[0]?.id||"");
    $("cases").hidden=!cases.length;
    if(cases.length)$("cases").dispatchEvent(new Event("change"));
    else message("当前实例还没有委托。");
  }catch(error){message(error.message);}
});
$("cases").addEventListener("change",()=>{base="/cases/"+encodeURIComponent($("cases").value);fitted=false;$("history").value="";refresh();});
if(project){
  document.body.classList.add("embedded");
  base="/api/projects/"+encodeURIComponent(project)+"/light-brain";
  refresh();
}else{
  show(demo,[12,9,6,3,1]);
}
setInterval(()=>{if(!document.hidden&&!$("history").value)refresh();},2200);
