const $ = id => document.getElementById(id);
const kind = {
  goal:["目标","#a6cfff",0], representation:["候选解释","#c9a3ff",1],
  operator:["开发动作","#a98fff",1], bound_operation:["派工依据","#a98fff",1],
  criterion:["验收条件","#ffd092",1], job:["作业","#77d9ff",2],
  receipt:["执行回执","#73e8c3",3], check_result:["实测结果","#73e8c3",3],
  git_commit:["源码版本","#81e8dc",4], delivery:["已交付","#b9ec86",4],
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
const viewport=$("viewport");
const demoNodes=[
  {id:"goal",type:"goal",text:"制作一个排练计时网页，允许添加和删除段落，并自动计算总时长。"},
  {id:"design",type:"operator",instruction:"设计可操作的排练时间轴"},
  {id:"criterion",type:"criterion",condition:{id:"累计时间正确",kind:"browser"}},
  {id:"develop",type:"job",kind:"develop"},
  {id:"verify",type:"job",kind:"verify"},
  {id:"export",type:"job",kind:"verify_export"},
  {id:"receipt",type:"receipt",ok:true},
  {id:"result",type:"check_result",condition:"新增段落后更新总时长",passed:true},
  {id:"release",type:"delivery",goal_version:1},
];
const demoEdges=[
  ["goal","design","intended"],["goal","criterion","acceptance"],
  ["design","develop","realized_by"],["criterion","verify","supports"],
  ["verify","receipt","reported"],["receipt","result","observed"],
  ["result","export","supports"],["export","release","accepted_by"],
];
const demo = {revision:8,nodes:demoNodes,edges:demoEdges.map(([source,target,type])=>({source,target,type}))};
function element(tag,text) {
  const item=document.createElement(tag); if(text!==undefined)item.textContent=text;
  return item;
}
function title(n) {
  return n.type==="goal" ? "用户委托" : caption(n);
}
function message(text="") { $("feedback").textContent=text; $("feedback").hidden=!text; }
function transform(){ $("plane").style.transform=`translate(${x}px,${y}px) scale(${scale})`; }
function fit(){
  const board=$("links");
  const span=Math.max(1,board.width);
  scale=Math.max(.45,Math.min(1,viewport.clientWidth/span,(viewport.clientHeight-45)/Math.min(board.height,700)));
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
  for (const e of graph.edges.filter(e=>e.source===id||e.target===id).slice(0,24)){
    const related=nodeById.get(e.source===id?e.target:e.source);
    if(!related)continue;
    const button=element("button",`${relations[e.type]||e.type} · ${title(related).slice(0,65)}`);
    button.addEventListener("click",()=>inspect(related.id,true));list.append(button);
  }
  for(const button of $("nodes").children){
    const on=button.dataset.id===id;
    button.setAttribute("aria-pressed",String(on));
    if(on&&focus){
      const p=coords.get(id);
      x=viewport.clientWidth/2-(p.x+84)*scale; y=viewport.clientHeight/2-(p.y+29)*scale;
      transform(); button.focus({preventScroll:true});
    }
  }
}
function draw(){
  if(!graph)return;
  const filter=$("filter").value, all=graph.nodes;
  const visible=filter?all.filter(n=>n.type===filter):all;
  const rank=new Map();
  coords=new Map(); $("nodes").replaceChildren();
  for(const n of visible) {
    const [,accent,r]=typeOf(n), col=filter?Math.floor((rank.get(r)||0)/6)%3:r;
    const i=rank.get(r)||0;rank.set(r,i+1);
    const row=filter?Math.floor(i/18)*6+i%6:i;
    const p={x:26+col*204,y:30+row*86};
    coords.set(n.id,p);
    const button=element("button");
    button.className="node";button.dataset.id=n.id;button.style.left=p.x+"px";button.style.top=p.y+"px";
    button.style.setProperty("--accent",accent);
    if(n.id===active)button.classList.add("working");
    const dot=element("span");dot.className="dot";
    button.append(dot,element("span",title(n)));button.addEventListener("click",()=>inspect(n.id));
    $("nodes").append(button);
  }
  const c=$("links");
  c.width=filter?646:1048;
  c.height=Math.max(380,...[...coords.values()].map(p=>p.y+92));
  const ctx=c.getContext("2d");
  for(const edge of graph.edges) {
    const a=coords.get(edge.source), b=coords.get(edge.target);
    if(!a||!b)continue;
    const forward=a.x<b.x, ax=a.x+(forward?168:0), bx=b.x+(forward?0:168);
    const ay=a.y+29,by=b.y+29,end=(ax+bx)/2;
    ctx.beginPath();ctx.moveTo(ax,ay);ctx.bezierCurveTo(end,ay,end,by,bx,by);
    ctx.strokeStyle=edge.type==="accepted_by"?"#a7e894":"#648dc88c";ctx.lineWidth=edge.type==="accepted_by"?2:1.2;ctx.stroke();
    ctx.fillStyle=ctx.strokeStyle;ctx.beginPath();ctx.arc(bx,by,2.6,0,2*Math.PI);ctx.fill();
  }
  if(!fitted){fit();fitted=true;}
  if(visible.some(n=>n.id===selected))inspect(selected);
  else if(visible.length)inspect(visible[0].id);
}
function fillOptions(select,values,selection) {
  select.replaceChildren(...values.map(([value,label])=>new Option(label,value)));
  select.value=selection;
}
function show(snapshot, revisions){
  graph=snapshot;
  const chosen=$("history").value;
  fillOptions($("history"),[["","最新"],...revisions.map(r=>[String(r),`R${r}`])],chosen);
  const types=[...new Set(graph.nodes.map(n=>n.type))];
  const filter=$("filter").value;
  fillOptions($("filter"),[["","全部"],...types.map(t=>[t,typeOf({type:t})[0]])],types.includes(filter)?filter:"");
  const legend=$("legend");
  legend.replaceChildren(...[["目标","#a6cfff"],["选择与动作","#a98fff"],["执行","#77d9ff"],["证据与版本","#73e8c3"],["交付","#b9ec86"]].map(([label,color])=>{
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
    const result=await request(base+"/graph"+(rev?"?revision="+encodeURIComponent(rev):""));
    const snapshot=result.graph||result;
    const revisions=result.revisions||Array.from({length:Math.min(120,result.revision)},(_,i)=>result.revision-i);
    active=rev ? undefined : result.active;
    const key=JSON.stringify([snapshot.case_id,snapshot.revision,revisions[0],active]);
    if(key!==lastSignature){show(snapshot,revisions);lastSignature=key;}
    $("connection").lastChild.textContent=rev?"历史修订":"实时委托";
    message();
  }catch(error){message(error.message);}
  finally{loading=false;}
}
$("filter").addEventListener("change",()=>{fitted=false;draw();});
$("history").addEventListener("change",()=>{fitted=false; if(base)refresh();else {
  const rev=Number($("history").value)||8;
  const nodes=demo.nodes.slice(0,rev+1), ids=new Set(nodes.map(n=>n.id));
  show({revision:rev,nodes,edges:demo.edges.filter(e=>ids.has(e.source)&&ids.has(e.target))},[8,6,3,1]);
}});
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
  show(demo,[8,6,3,1]);
}
setInterval(()=>{if(!document.hidden&&!$("history").value)refresh();},2200);
