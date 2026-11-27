"use strict";
const REPO="hub-google/audiobook-generator",API="https://api.github.com/repos/"+REPO,QUEUE_BRANCH="automation-state",QUEUE_PATH="audiobook-queue.json";
const SOURCES=[
{id:"hjwzw",name:"黃金屋",hosts:["tw.hjwzw.com","www.hjwzw.com","hjwzw.com"]},
{id:"shuba69",name:"69 書吧",hosts:["69shuba.com","www.69shuba.com"]},
{id:"czbooks",name:"小說狂人",hosts:["czbooks.net","www.czbooks.net"]},
{id:"sixk",name:"6K 小說",hosts:["6ksu.app","www.6ksu.app"]},
{id:"xszj",name:"小說之家",hosts:["xszj.tw","www.xszj.tw"]},
{id:"metanovel",name:"MetaNovel",hosts:["metanovel.org","www.metanovel.org","m.metanovel.org"]},
{id:"twkan",name:"天天看",hosts:["twkan.cc","www.twkan.cc"]},
{id:"moon17",name:"月亮小說網",hosts:["17moon.com","www.17moon.com"]},
{id:"book8",name:"無限小說",hosts:["8book.com","www.8book.com","v.8book.com","m.8book.com","i.8book.com"]}
];
let token="",queue=null,queueSha=null,latestRuns=[],parsedCatalog=null,editorTaskId=null,reviewTaskId=null,chapterPickerExcluded=null,chapterPickerOrder=null,rankingNovels=[],rankingSelected=new Set(),selectedPending=new Set(),selectedCompleted=new Set();
const $=id=>document.getElementById(id), text=(node,value)=>{node.textContent=value==null?"":String(value)};
function notice(message,kind="ok"){const n=$("message");text(n,message);n.className="notice"+(kind==="error"?" error":"");}
const dateLabel=v=>v?new Date(v).toLocaleString("zh-TW",{timeZone:"Asia/Taipei",hour12:false}):"—";
const escapeHTML=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function toB64(value){const bytes=new TextEncoder().encode(value);let result="";for(let i=0;i<bytes.length;i+=8192)result+=String.fromCharCode(...bytes.slice(i,i+8192));return btoa(result);}
function fromB64(value){const raw=atob(value.replace(/\s/g,""));return new TextDecoder().decode(Uint8Array.from(raw,c=>c.charCodeAt(0)));}
function getSource(url){let parsed;try{parsed=new URL(url)}catch{throw new Error("請輸入有效的小說網址");}if(!["http:","https:"].includes(parsed.protocol)||parsed.username||parsed.password)throw new Error("僅接受 HTTP(S) 網址");const source=SOURCES.find(s=>s.hosts.includes(parsed.hostname.toLowerCase()));if(!source)throw new Error("目前不支援此網址的網站");return source;}
async function request(path,options={}){
 const headers={"Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28",...(options.headers||{})};
 // Repository reads are public; no browser credentials.
 const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),35000);
 let response;
 try{response=await fetch(path.startsWith("https://")?path:API+path,{...options,headers,signal:controller.signal,cache:"no-store"});}
 finally{clearTimeout(timeout);}
 if(!response.ok){const error=new Error("GitHub API HTTP "+response.status+"（請檢查 GitHub 權限與網路）");error.status=response.status;throw error;}
 if(response.status===204)return null;
 const type=response.headers.get("content-type")||"";
 return type.includes("json")?response.json():response;
}
async function readQueue(){
 const url="/contents/"+QUEUE_PATH+"?ref="+QUEUE_BRANCH;
 try{const file=await request(url);return{data:JSON.parse(fromB64(file.content)),sha:file.sha};}
 catch(e){if(e.status===404)return{data:{schema_version:3,revision:0,updated_at:new Date().toISOString(),queue:[],completed:[]},sha:null};throw e;}
}
function queueTasks(){return Array.isArray(queue?.queue)?queue.queue:[];}
function active(task){return ["dispatching","preparing_assets","waiting_review","processing","running","waiting_retry","needs_attention","canceling"].includes(task.status)||Boolean(task.run_id&&!task.run_conclusion&&!["paused","interrupted","stopped","completed"].includes(task.status));}
function live(task){return ["dispatching","preparing_assets","processing","running","canceling"].includes(task.status)||Boolean(task.run_id&&!task.run_conclusion&&!["paused","interrupted","stopped","completed","waiting_review","waiting_retry","needs_attention"].includes(task.status));}
function statusName(s){return({queued:"排隊中",paused:"已暫停",preparing_assets:"TXT / 封面製作中",waiting_review:"等待審核",processing:"影片製作中",running:"執行中",dispatching:"調度中",waiting_retry:"等待重試",needs_attention:"需要處理",completed:"已完成",interrupted:"已中斷",stopped:"已停止",canceling:"取消中"})[s]||s||"未知";}
function statusClass(s){return s==="completed"?"done":["waiting_review","needs_attention"].includes(s)?"review":["stopped","interrupted"].includes(s)?"error":live({status:s})?"running":"";}
function stageBadge(task,name){const s=task.stages?.[name]?.status||"pending";return({completed:"✓",running:"進行中",dispatching:"啟動中",failed:"失敗",pending:"待執行",skipped:"略過"})[s]||s;}
function canEdit(task){return !active(task)&&["queued","paused","interrupted","stopped"].includes(task.status);}
function makeBtn(label,action,id,disabled=false,authRequired=true){const b=document.createElement("button");b.type="button";b.textContent=label;b.className="secondary";b.disabled=disabled;b.addEventListener("click",()=>Promise.resolve().then(()=>action(id)).catch(e=>notice(e.message,"error")));return b;}
function buildTaskRow(task,index){
 const tr=document.createElement("tr"),col=(value)=>{const td=document.createElement("td");td.textContent=value;tr.append(td);return td;};
 const first=col("");const check=document.createElement("input");check.type="checkbox";check.checked=selectedPending.has(task.task_id);
 check.setAttribute("aria-label","選取 "+(task.book_title||"小說"));
 check.addEventListener("change",()=>{if(check.checked)selectedPending.add(task.task_id);else selectedPending.delete(task.task_id);updateBatchStatus();});
 first.append(check,document.createTextNode(" "+(index+1)+". "+(task.book_title||"待解析")));
 const url=document.createElement("small");url.textContent=task.catalog_url||"";first.append(url);
 col("第 "+(task.start_chapter||1)+"–"+(task.end_chapter||"全部")+" 章");
 const status=col("");const badge=document.createElement("span");badge.className="status "+statusClass(task.status);badge.textContent=statusName(task.status);status.append(badge);
 const stages=col("TXT "+stageBadge(task,"scrape")+" · 封面 "+stageBadge(task,"cover")+" · 影片 "+stageBadge(task,"processing"));
 const rid=task.run_id||task.scrape_run_id||task.cover_run_id;if(rid){const small=document.createElement("small"),a=document.createElement("a");a.href="https://github.com/"+REPO+"/actions/runs/"+encodeURIComponent(rid);a.target="_blank";a.rel="noopener noreferrer";a.textContent="Run #"+rid;small.append(a);stages.append(small);}
 const controls=col(""),div=document.createElement("div");div.className="actions";controls.append(div);
 div.append(makeBtn("進度",showProgress,task.task_id,false,false));
 div.append(makeBtn("成品下載",showProgress,task.task_id,false,false));
 if(["queued","paused","waiting_review","preparing_assets","needs_attention"].includes(task.status))div.append(makeBtn("手動封面",openManualCover,task.task_id));
 if(live(task))div.append(makeBtn("取消本次 Run",cancelRunningTask,task.task_id));
 if(task.status==="waiting_review"){div.append(makeBtn("查看審核資料",async()=>{window.open("https://github.com/"+REPO+"/actions/runs/"+(task.cover_run_id||task.run_id),"_blank","noopener");},task.task_id,false,false));div.append(makeBtn("確認審核並繼續",openReview,task.task_id,false));}
 if(["queued","paused","interrupted","stopped"].includes(task.status)){
  div.append(makeBtn("編輯",openEditor,task.task_id,!canEdit(task)));
  div.append(makeBtn("↑",id=>moveTask(id,-1),task.task_id,index===0||!canEdit(task)||!canEdit(queueTasks()[index-1])));
  div.append(makeBtn("↓",id=>moveTask(id,1),task.task_id,index===queueTasks().length-1||!canEdit(task)||!canEdit(queueTasks()[index+1])));
  if(task.status==="queued")div.append(makeBtn("暫停",id=>setTaskState(id,"paused"),task.task_id));
  else div.append(makeBtn("重新排程",requeueTask,task.task_id));
  if(["paused","interrupted","stopped"].includes(task.status))div.append(makeBtn("標為已完成",markCompleted,task.task_id));
  div.append(makeBtn("刪除",deleteTask,task.task_id));
 }
 return tr;
}
function updateBatchStatus(){
 text($("queue-selected-count"),"已選 "+selectedPending.size+" 本");
 const all=queueTasks();$("queue-all").checked=!!all.length&&all.every(t=>selectedPending.has(t.task_id));
 const button=$("batch-restore");button.disabled=selectedCompleted.size===0;
}
function selectedPendingTasks(){return queueTasks().filter(t=>selectedPending.has(t.task_id));}
function renderQueue(){
 const tasks=queueTasks(),completed=Array.isArray(queue?.completed)?queue.completed:[];
 text($("count-queued"),tasks.filter(t=>t.status==="queued"||t.status==="paused").length);
 text($("count-active"),tasks.filter(t=>active(t)).length);text($("count-completed"),completed.length);
 text($("updated-at"),dateLabel(queue?.updated_at));
 const tbody=$("queue-body");tbody.replaceChildren();if(!tasks.length){const tr=document.createElement("tr"),td=document.createElement("td");td.colSpan=5;td.className="empty";td.textContent="目前沒有待製作小說";tr.append(td);tbody.append(tr);}
 selectedPending=new Set([...selectedPending].filter(id=>tasks.some(t=>t.task_id===id)));
 tasks.forEach((t,i)=>tbody.append(buildTaskRow(t,i)));
 const list=$("completed-list");list.replaceChildren();
 if(!completed.length){const p=document.createElement("p");p.className="muted";p.textContent="尚無已完成作品";list.append(p);}
 selectedCompleted=new Set([...selectedCompleted].filter(id=>completed.some(t=>t.task_id===id)));
 completed.forEach(t=>{const row=document.createElement("div");row.className="completed-item";
 const checkbox=document.createElement("input");checkbox.type="checkbox";checkbox.checked=selectedCompleted.has(t.task_id);
 checkbox.setAttribute("aria-label","選取已完成 "+(t.book_title||"小說"));
 checkbox.addEventListener("change",()=>{if(checkbox.checked)selectedCompleted.add(t.task_id);else selectedCompleted.delete(t.task_id);updateBatchStatus();});
 const name=document.createElement("strong");name.textContent=t.book_title||"未知小說";const status=document.createElement("span");status.className="muted";status.textContent=dateLabel(t.completed_at||t.updated_at);const restore=makeBtn("移回佇列",restoreCompleted,t.task_id);const download=makeBtn("成品下載",showProgress,t.task_id,false,false);row.append(checkbox,name,status,download,restore);list.append(row);});
 updateBatchStatus();
}
async function refresh(){
 try{const res=await readQueue();queue=res.data;queueSha=res.sha;renderQueue();
  try{const data=await request("/actions/runs?per_page=35");latestRuns=data.workflow_runs||[];renderRuns();}catch(error){notice("Actions 紀錄載入失敗："+error.message,"error");}
  renderRanking();
  $("connection-pill").textContent="GitHub 登入操作";$("connection-pill").className="pill ok";
 }catch(e){notice("同步失敗："+e.message,"error");}
}
async function connect(){
 const supplied=$("token").value.trim();if(!supplied)throw Error("請輸入 GitHub Token");
 const previous=token;token=supplied;
 try{const r=await request("https://api.github.com/user");$("token").value="";notice("GitHub 已連線："+r.login+"。Token 不會存入網頁儲存空間。");await refresh();}
 catch(e){token=previous;throw e;}
}
function updatePositions(data){data.queue.forEach((task,i)=>{task.position=i+1;});data.revision=(Number(data.revision)||0)+1;data.updated_at=new Date().toISOString();return data;}
async function mutate(transform,message){
 if(!token)throw Error("請先以具備 Contents 權限的 Token 連線");
 for(let attempt=0;attempt<3;attempt++){
  const current=await readQueue();const copy=structuredClone(current.data);
  copy.queue=copy.queue||[];copy.completed=copy.completed||[];
  const result=transform(copy);updatePositions(result);
  const payload={message,branch:QUEUE_BRANCH,content:toB64(JSON.stringify(result,null,2)+"\n")};if(current.sha)payload.sha=current.sha;
  try{await request("/contents/"+QUEUE_PATH,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});queue=result;renderQueue();return;}
  catch(e){if(![409,422].includes(e.status)||attempt===2)throw e;}
 }
}
async function triggerDispatcher(){
 if(!token)throw Error("請先連線 GitHub");
 await request("/actions/workflows/queue-dispatcher.yml/dispatches",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ref:"main",inputs:{force_retry:"false"}})});
 notice("已向 GitHub 提交調度檢查，實際啟動狀態請至製作紀錄查看。");
}
async function moveTask(id,delta){await mutate(q=>{const i=q.queue.findIndex(t=>t.task_id===id),j=i+delta;if(i<0||j<0||j>=q.queue.length||!canEdit(q.queue[i])||!canEdit(q.queue[j]))throw Error("無法移動正在執行的工作");[q.queue[i],q.queue[j]]=[q.queue[j],q.queue[i]];return q;},"Web UI: reorder audiobook queue");notice("已更新工作順序");}
async function setTaskState(id,state){await mutate(q=>{const t=q.queue.find(x=>x.task_id===id);if(!t||!canEdit(t))throw Error("該工作目前不允許修改");t.status=state;t.reason=null;t.retry_at=null;t.updated_at=new Date().toISOString();return q;},"Web UI: update audiobook task status");notice("已更新任務狀態");if(state==="queued")await triggerDispatcher();}
async function renderRunJobDetails(task){
 const container=$("progress-jobs");container.replaceChildren();
 const artifactsBox=$("progress-artifacts");artifactsBox.replaceChildren();
 const ids=[...new Set([task.scrape_run_id,task.cover_run_id,task.processing_run_id,task.run_id].filter(Boolean))];
 if(!ids.length){text(container,"目前尚無已啟動的 GitHub Actions");return;}
 for(const runId of ids){
  const section=document.createElement("section");section.className="progress-job-section";
  const heading=document.createElement("a");heading.href="https://github.com/"+REPO+"/actions/runs/"+encodeURIComponent(runId);
  heading.target="_blank";heading.rel="noopener noreferrer";heading.textContent="Run #"+runId+" — 開啟 GitHub 詳細紀錄";section.append(heading);
  const display=document.createElement("div");display.className="progress-job-list";display.textContent="讀取 Job 與 Step…";section.append(display);container.append(section);
  try{
   const data=await request("/actions/runs/"+encodeURIComponent(runId)+"/jobs?per_page=100");
   const jobs=Array.isArray(data.jobs)?data.jobs:[];
   if(!jobs.length){display.textContent="目前尚無 Job 記錄";continue;}
   const lines=[];
   for(const job of jobs){
    lines.push("◆ "+(job.name||"工作")+"　"+(job.conclusion||job.status||"未知"));
    for(const step of job.steps||[])lines.push("   "+(step.conclusion==="success"?"✓":step.conclusion==="failure"?"✕":"·")+" "+step.name+"　"+(step.conclusion||step.status||""));
   }
   display.textContent=lines.join("\n");
   try{
    const res=await request("/actions/runs/"+encodeURIComponent(runId)+"/artifacts?per_page=100");
    for(const item of (res.artifacts||[]).filter(x=>!x.expired)){
     const link=document.createElement("a");
     link.href="https://github.com/"+REPO+"/actions/runs/"+encodeURIComponent(runId)+"/artifacts/"+encodeURIComponent(item.id);
     link.target="_blank";link.rel="noopener noreferrer";
     link.textContent=(item.name||"Artifact")+" · "+(item.size_in_bytes/1024/1024).toFixed(1)+" MB";
     artifactsBox.append(link);
    }
   }catch(error){const p=document.createElement("p");p.textContent="請從 GitHub Run 頁下載 Artifact";artifactsBox.append(p);}
  }catch(e){display.textContent="Job 詳細資料目前無法公開讀取："+e.message;}
 }
}

async function showProgress(id){
 const t=[...queueTasks(),...(queue?.completed||[])].find(x=>x.task_id===id);
 if(!t)throw Error("找不到任務");
 const detail=[
  "書名："+(t.book_title||"待解析"),
  "狀態："+statusName(t.status),
  "章節："+(t.start_chapter||1)+"–"+(t.end_chapter||"全部"),
  "最後更新："+dateLabel(t.updated_at),
  "TXT："+stageBadge(t,"scrape")+"｜封面："+stageBadge(t,"cover")+"｜影片："+stageBadge(t,"processing")+"｜上傳："+stageBadge(t,"publication"),
  "HF進度："+(t.hf_progress?.completed||0)+"/"+(t.hf_progress?.total||0),
  "YouTube進度："+(t.youtube_progress?.completed||0)+"/"+(t.youtube_progress?.total||0),
  "執行 Run："+(t.run_id||"尚未建立"),
  "工作 ID："+t.task_id,
  "異常或原因："+(t.reason||"無"),
 ].join("\n");
 text($("progress-detail"),detail);$("progress-dialog").showModal();await renderRunJobDetails(t);
}
async function cancelRunningTask(id){
 const task=queueTasks().find(t=>t.task_id===id);
 if(!task||!live(task))throw Error("目前沒有正在執行的工作");
 const ids=[...new Set([task.run_id,task.cover_run_id,task.scrape_run_id,task.processing_run_id].filter(Boolean))];
 if(!ids.length)throw Error("任務正在啟動，尚無可取消的 Run ID");
 if(!confirm("確定取消 "+(task.book_title||"此任務")+" 的本次 GitHub Actions 執行？"))return;
 let requested=0;
 for(const id of ids){
  const state=await request("/actions/runs/"+encodeURIComponent(id));
  if(state.status==="completed")continue;
  await request("/actions/runs/"+encodeURIComponent(id)+"/cancel",{method:"POST"});
  requested++;
 }
 if(!requested){notice("這些 Run 已結束，請先同步狀態");await refresh();return;}
 await mutate(q=>{const t=q.queue.find(x=>x.task_id===task.task_id);
  if(!t)throw Error("取消的任務已不在佇列");
  t.status="canceling";t.reason="user_cancelled";t.updated_at=new Date().toISOString();return q;
 },"Web UI: cancel active audiobook run");
 notice("已提交取消請求；稍後請同步確認 Run 結果");
}
async function markCompleted(id){
 if(!confirm("確定將這本小說標記為已完成？只會修改佇列狀態，不會自動上傳影片。"))return;
 await mutate(q=>{const index=q.queue.findIndex(t=>t.task_id===id);
  if(index<0||!["paused","interrupted","stopped"].includes(q.queue[index].status))throw Error("只能手動完成已暫停或中斷的任務");
  const t=q.queue.splice(index,1)[0],now=new Date().toISOString();
  t.status="completed";t.run_conclusion=t.run_conclusion||"manual";t.run_completed_at=now;t.completed_at=now;
  t.reason=null;t.updated_at=now;delete t.position;
  q.completed.push(t);return q;
 },"Web UI: manually complete audiobook");notice("已標記完成");
}
async function restoreCompleted(id){
 if(!confirm("將這本小說移回未完成佇列（暫停狀態）？"))return;
 await mutate(q=>{const index=q.completed.findIndex(t=>t.task_id===id);
  if(index<0)throw Error("找不到已完成任務");
  const t=q.completed.splice(index,1)[0];Object.assign(t,{
    status:"paused",run_id:null,run_conclusion:null,run_completed_at:null,
    reason:null,retry_at:null,updated_at:new Date().toISOString()
  });q.queue.push(t);return q;
 },"Web UI: restore completed audiobook");notice("已移回佇列（暫停）");
}
async function deleteTask(id){if(!confirm("確定刪除此尚未執行的任務？"))return;await mutate(q=>{const i=q.queue.findIndex(x=>x.task_id===id);if(i<0||!canEdit(q.queue[i]))throw Error("不能刪除正在執行的作業");q.queue.splice(i,1);return q;},"Web UI: remove queued audiobook");notice("任務已從佇列移除");}
function readOverrides(value,start,end,isNumber=false){
 const out={};for(const line of value.split(/\r?\n/)){if(!line.trim())continue;const pos=line.indexOf("|");if(pos<0)throw Error("進階章節設定必須使用「索引 | 內容」格式："+line);
 const index=Number(line.slice(0,pos).trim()),name=line.slice(pos+1).trim();
 if(!Number.isSafeInteger(index)||index<start||index>end)throw Error("章節索引不在範圍內："+line);
 if(!name||name.length>300)throw Error("自訂章節內容不可空白或超過300字");
 if(isNumber&&!/^[1-9]\d*(?:\.\d+)?$/.test(name))throw Error("自訂章號必須為正數或正小數："+name);
 out[String(index)]=name;
 }return out;
}
function readOrder(value,start,end,excluded){
 if(!value.trim())return [];
 const nums=value.split(/[,，;；\s]+/).filter(Boolean).map(Number),deny=new Set(excluded);
 if(nums.some(n=>!Number.isSafeInteger(n)||n<start||n>end||deny.has(n)))throw Error("播放順序包含不合法或已排除的章節");
 const expected=end-start+1-deny.size;
 if(nums.length!==expected||new Set(nums).size!==expected)throw Error("播放順序必須完整列出 "+expected+" 個未排除章節，且不得重複");
 return nums;
}
function formatOverrides(overrides){return Object.entries(overrides||{}).map(([k,v])=>k+" | "+v).join("\n");}
function formatPublish(v){return v?String(v).slice(0,16):"";}
function publishValue(v){return v?v+":00+08:00":null;}
function openEditor(id){
 const task=queueTasks().find(x=>x.task_id===id);if(!task||!canEdit(task))throw Error("執行中的任務不可直接修改");
 editorTaskId=id;text($("edit-book-url"),task.catalog_url);$("edit-name").value=task.book_title||"";$("edit-start").value=task.start_chapter||1;$("edit-end").value=task.end_chapter||10;
 $("edit-excluded").value=(task.excluded_chapters||[]).join(",");$("edit-renumber").checked=!!task.renumber_selected;$("edit-publish").value=formatPublish(task.publish_at);
 $("edit-titles").value=formatOverrides(task.chapter_title_overrides);$("edit-numbers").value=formatOverrides(task.chapter_normalized_number_overrides);
 $("edit-order").value=(task.chapter_order||[]).join(",");$("edit-dialog").showModal();
}
async function saveEditor(event){
 event.preventDefault();const id=editorTaskId;if(!id)throw Error("未選取編輯工作");
 const start=Number($("edit-start").value),end=Number($("edit-end").value),name=$("edit-name").value.trim();
 if(!Number.isSafeInteger(start)||!Number.isSafeInteger(end)||start<1||end<start||end>999999)throw Error("章節範圍無效");
 if(!name)throw Error("書名不可空白");const excluded=excludedNumbers($("edit-excluded").value,start,end);
 const titles=readOverrides($("edit-titles").value,start,end),numbers=readOverrides($("edit-numbers").value,start,end,true);
 const order=readOrder($("edit-order").value,start,end,excluded);
 await mutate(q=>{const t=q.queue.find(x=>x.task_id===id);if(!t||!canEdit(t))throw Error("任務已開始執行，請重新整理");
 Object.assign(t,{book_title:name,start_chapter:start,end_chapter:end,excluded_chapters:excluded,renumber_selected:$("edit-renumber").checked,publish_at:publishValue($("edit-publish").value),chapter_title_overrides:titles,chapter_normalized_number_overrides:numbers,chapter_order:order,updated_at:new Date().toISOString()});
 return q;},"Web UI: edit audiobook chapter plan");$("edit-dialog").close();editorTaskId=null;notice("章節、標題與播放順序已儲存");
}
function openReview(id){const task=queueTasks().find(t=>t.task_id===id);if(!task||task.status!=="waiting_review")throw Error("任務不在待審核狀態");
 reviewTaskId=id;$("review-patterns").value=(task.ad_review?.remove_patterns||task.profile_snapshot?.cleaner_remove_patterns||[]).join("\n");
 $("review-dialog").showModal();
}

async function approveTask(id,patterns){
 await mutate(q=>{const t=q.queue.find(x=>x.task_id===id);if(!t||t.status!=="waiting_review"||!t.scrape_run_id||!t.cover_run_id||!["scrape","cover"].every(s=>t.stages?.[s]?.status==="completed"))throw Error("TXT / 封面尚未完成或審查狀態已改變");
 t.ad_review={status:"approved",approved_at:new Date().toISOString(),remove_patterns:patterns};
 t.status="queued";t.workflow_phase="processing";t.run_id=null;t.processing_run_id=null;t.reason=null;t.updated_at=new Date().toISOString();return q;
 },"Web UI: approve reviewed audiobook preflight");await triggerDispatcher();
}
async function saveReview(event){
 event.preventDefault();if(!reviewTaskId)throw Error("未選取審核工作");
 const patterns=[...new Set($("review-patterns").value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean))];
 if(patterns.some(x=>x.length>10000))throw Error("單一刪除字詞最多10000字");
 if(!confirm("已確認 TXT、封面及著作權授權？確認後開始影片製作與後續發布。"))return;
 await approveTask(reviewTaskId,patterns);$("review-dialog").close();reviewTaskId=null;notice("已審核並提交後續製作");
}
function excludedNumbers(value,start,end){
 const all=new Set();for(const segment of value.split(/[,，;；\s]+/).filter(Boolean)){const m=segment.match(/^(\d+)(?:-(\d+))?$/);if(!m)throw Error("排除章節格式錯誤："+segment);const a=Number(m[1]),b=Number(m[2]||m[1]);if(b<a||b-a>10000)throw Error("章節範圍不合法："+segment);for(let i=a;i<=b;i++)if(i>=start&&i<=end)all.add(i);}
 return [...all].sort((a,b)=>a-b);
}
function buildTask({url,title,start,end,excluded=[],renumber=false,publishAt=null,identity="",titles={},numbers={},order=[]}){
 const now=new Date().toISOString();getSource(url);if(!title.trim())throw Error("請輸入書名");
 if(!Number.isSafeInteger(start)||!Number.isSafeInteger(end)||start<1||end<start||end>999999)throw Error("章節起訖無效");
 const stage=()=>({status:"pending",run_id:null,run_attempt:0,reason:null,completed_at:null});
 const id="book-"+now.slice(0,10).replaceAll("-","")+"-"+crypto.randomUUID().replaceAll("-","").slice(0,8);
 return{task_id:id,position:0,book_title:title.trim(),catalog_url:url.trim(),catalog_identity:identity,start_chapter:start,end_chapter:end,excluded_chapters:excluded,renumber_selected:renumber,publish_at:publishAt,duplicate_chapter_count:null,chapter_title_overrides:titles,chapter_normalized_number_overrides:numbers,chapter_order:order,status:"queued",workflow_phase:"queued",stages:{scrape:stage(),cover:stage(),processing:stage(),publication:stage()},scrape_run_id:null,cover_run_id:null,processing_run_id:null,scrape_snapshot_id:"",ad_review:{status:"pending"},run_id:null,run_attempt:0,execution_generation:1,retry_requested_at:null,run_history:[],retry_at:null,reason:null,hf_progress:{completed:0,total:0},youtube_progress:{completed:0,total:0},created_at:now,updated_at:now};
}
async function addTasks(tasks){
 if(!tasks.length)throw Error("沒有可加入的任務");
 await mutate(q=>{q.queue.push(...tasks);return q;},"Web UI: enqueue "+tasks.length+" audiobook(s)");
 notice("已加入 "+tasks.length+" 本小說的 GitHub 雲端佇列。");await triggerDispatcher();switchTab("overview");
}
async function submitBook(event){
 event.preventDefault();const url=$("catalog-url").value.trim(),source=getSource(url);
 if($("source").value!==source.id)throw Error("選取的網站與網址不一致");
 const start=Number($("start-chapter").value),end=Number($("end-chapter").value);
 if(parsedCatalog&&parsedCatalog.catalog_url===url&&end>parsedCatalog.total_chapters)throw Error("結束章超過目錄總章數");
 const publish=$("publish-scheduled").checked&&$("publish-at").value?$("publish-at").value+":00+08:00":null;
 if($("publish-scheduled").checked&&!publish)throw Error("請選擇排定公開時間，或切回立即公開");
 const excluded=excludedNumbers($("excluded").value,start,end);
 await addTasks([buildTask({url,title:$("book-title").value,start,end,excluded,renumber:$("renumber").checked,publishAt:publish,identity:parsedCatalog?.catalog_url===url?parsedCatalog.catalog_identity:"",titles:readOverrides($("title-overrides").value,start,end),numbers:readOverrides($("number-overrides").value,start,end,true),order:readOrder($("chapter-order").value,start,end,excluded)})]);
 $("book-form").reset();renderPublish();parsedCatalog=null;$("catalog-preview").classList.add("hidden");$("chapter-selection-tools").classList.add("hidden");
}
async function batchAdd(){
 const lines=$("batch-urls").value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);if(!lines.length)throw Error("請貼上批次小說資料");
 const tasks=lines.map((line,i)=>{const parts=line.split("|").map(x=>x.trim());if(parts.length<2)throw Error("第 "+(i+1)+" 行格式錯誤");return buildTask({title:parts[0],url:parts[1],start:Number(parts[2]||1),end:Number(parts[3]||10)});});await addTasks(tasks);$("batch-urls").value="";
}
function filteredRanking(){
 const term=$("rank-search").value.trim().toLocaleLowerCase(),category=$("rank-category").value;
 return rankingNovels.filter(n=>(!category||n.category===category)&&(!term||[n.title,n.author,n.latest_chapter].some(v=>String(v||"").toLocaleLowerCase().includes(term))));
}
function renderRanking(){
 const data=filteredRanking(),body=$("rank-body");body.replaceChildren();
 text($("rank-count"),"顯示 "+data.length+" 本／總計 "+rankingNovels.length+" 本；已選 "+rankingSelected.size+" 本");
 $("rank-copy").disabled=rankingSelected.size===0;
 if(!data.length){const tr=document.createElement("tr"),td=document.createElement("td");td.colSpan=6;td.className="empty";td.textContent=rankingNovels.length?"沒有符合條件的小說":"尚無可用排行資料";tr.append(td);body.append(tr);return;}
 for(const novel of data.slice(0,500)){
  const tr=document.createElement("tr"),url=novel.catalog_url;
  const pick=document.createElement("td"),check=document.createElement("input");check.type="checkbox";check.checked=rankingSelected.has(url);
  check.setAttribute("aria-label","選取 "+novel.title);
  check.addEventListener("change",()=>{if(check.checked)rankingSelected.add(url);else rankingSelected.delete(url);renderRanking();});
  pick.append(check);tr.append(pick);
  for(const value of [novel.rank,novel.title,novel.author,novel.category,novel.latest_chapter]){
   const td=document.createElement("td");td.textContent=value||"—";tr.append(td);
  }body.append(tr);
 }
}
async function loadRanking(){
 try{
  const res=await fetch("./full-novels.json",{cache:"no-store"});
  if(!res.ok)throw Error("HTTP "+res.status);
  const data=await res.json();
  if(!Array.isArray(data.novels))throw Error("排行資料格式不合法");
  rankingNovels=data.novels.filter(n=>n.catalog_url&&n.title&&/^https:\/\/69shuba\.com\/book\/\d+\/$/.test(n.catalog_url));
  const select=$("rank-category");select.replaceChildren();
  const opt=document.createElement("option");opt.value="";opt.textContent="全部分類";select.append(opt);
  for(const category of [...new Set(rankingNovels.map(n=>n.category).filter(Boolean))].sort()){
   const item=document.createElement("option");item.value=category;item.textContent=category;select.append(item);
  }
  renderRanking();
 }catch(e){rankingNovels=[];renderRanking();text($("rank-count"),"目前無法讀取完本排行："+e.message);}
}
function chosenRanking(){return rankingNovels.filter(n=>rankingSelected.has(n.catalog_url));}
async function copyRankingUrls(){
 const lines=chosenRanking().map(n=>n.catalog_url);
 if(!lines.length)throw Error("請先選取小說");
 await navigator.clipboard.writeText(lines.join("\n"));
 notice("已複製 "+lines.length+" 個小說網址");
}

function renderRuns(){const body=$("runs-body");body.replaceChildren();if(!latestRuns.length){const tr=document.createElement("tr");tr.innerHTML='<td colspan="4" class="empty">目前沒有 Actions 紀錄</td>';body.append(tr);return;}
 latestRuns.forEach(run=>{const tr=document.createElement("tr");let td=document.createElement("td");td.textContent=run.display_title||run.name;tr.append(td);td=document.createElement("td");td.textContent=dateLabel(run.created_at);tr.append(td);td=document.createElement("td");const badge=document.createElement("span");badge.className="status "+(run.conclusion==="success"?"done":run.conclusion==="failure"?"error":"running");badge.textContent=run.conclusion||run.status;td.append(badge);tr.append(td);td=document.createElement("td");const link=document.createElement("a");link.href=run.html_url;link.target="_blank";link.rel="noopener noreferrer";link.textContent="查看 Run #"+run.id;td.append(link);tr.append(td);body.append(tr);});
}
async function unzipJson(buffer,filename){
 const bytes=new Uint8Array(buffer),view=new DataView(buffer);
 let eocd=-1;for(let i=bytes.length-22;i>=Math.max(0,bytes.length-66000);i--){if(view.getUint32(i,true)===0x06054b50){eocd=i;break;}}if(eocd<0)throw Error("ZIP 格式不完整");
 let offset=view.getUint32(eocd+16,true),count=view.getUint16(eocd+10,true);
 for(let i=0;i<count;i++){if(view.getUint32(offset,true)!==0x02014b50)throw Error("ZIP 檔案目錄無效");
 const method=view.getUint16(offset+10,true),size=view.getUint32(offset+20,true),len=view.getUint16(offset+28,true),extra=view.getUint16(offset+30,true),comment=view.getUint16(offset+32,true),local=view.getUint32(offset+42,true);
 const name=new TextDecoder().decode(bytes.slice(offset+46,offset+46+len));offset+=46+len+extra+comment;
 if(name!==filename)continue;if(view.getUint32(local,true)!==0x04034b50)throw Error("ZIP 本文無效");
 const start=local+30+view.getUint16(local+26,true)+view.getUint16(local+28,true);
 const compressed=bytes.slice(start,start+size);
 let raw;if(method===0)raw=compressed;else if(method===8){if(typeof DecompressionStream==="undefined")throw Error("瀏覽器不支援解壓縮 ZIP");
 raw=new Uint8Array(await new Response(new Blob([compressed]).stream().pipeThrough(new DecompressionStream("deflate-raw"))).arrayBuffer());
 }else throw Error("不支援的 ZIP 壓縮格式");
 return JSON.parse(new TextDecoder().decode(raw));
 }throw Error("找不到章節摘要 "+filename);
}
async function fetchPreviewArtifact(artifact){
 const response=await request("/actions/artifacts/"+artifact.id+"/zip");
 return unzipJson(await response.arrayBuffer(),"catalog-preview.json");
}
function currentChapterOverrides(){
 if(!parsedCatalog)return {};
 const end=parsedCatalog.total_chapters;
 return readOverrides($("number-overrides").value||"",1,end,true);
}
function analyzePickerDuplicates(){
 const parts=parsedCatalog?.chapter_parts||[];
 const overrides=currentChapterOverrides(),seenNumbers=new Map(),seenNames=new Map(),seenPairs=new Map(),matches=new Map();
 const useNumber=$("duplicate-number").checked,useName=$("duplicate-name").checked,usePair=$("duplicate-pair").checked;
 for(let i=0;i<parts.length;i++){
  const index=i+1,part=parts[i],num=overrides[String(index)]||String(part.n||""),name=String(part.c||"");
  const reason=[],parents=new Set(),pair=num+"\u0000"+name;
  if(useNumber&&num&&seenNumbers.has(num)){reason.push("章號相同");parents.add(seenNumbers.get(num));}
  if(useName&&name&&seenNames.has(name)){reason.push("章名相同");parents.add(seenNames.get(name));}
  if(usePair&&num&&name&&seenPairs.has(pair)){reason.push("章號＋章名相同");parents.add(seenPairs.get(pair));}
  if(parents.size)matches.set(index,{reason,parents:[...parents].sort((a,b)=>a-b)});
  if(useNumber&&num&&!seenNumbers.has(num))seenNumbers.set(num,index);
  if(useName&&name&&!seenNames.has(name))seenNames.set(name,index);
  if(usePair&&num&&name&&!seenPairs.has(pair))seenPairs.set(pair,index);
 }
 return matches;
}
function normalizedOrderValue(index){
 const p=parsedCatalog?.chapter_parts?.[index-1]||{},override=currentChapterOverrides()[String(index)];
 const value=String(override||p.n||"").trim();
 const numeric=/^\d+(?:\.\d+)?$/.test(value)?Number(value):null;
 return numeric!==null&&Number.isFinite(numeric)?numeric:null;
}
function sortPickerOrder(mode){
 if(!chapterPickerOrder)throw Error("尚未開啟章節視窗");
 if(mode==="original")chapterPickerOrder.sort((a,b)=>a-b);
 else{
  const keys=new Map(chapterPickerOrder.map(i=>[i,normalizedOrderValue(i)]));
  chapterPickerOrder.sort((a,b)=>{
   const x=keys.get(a),y=keys.get(b);
   if(x===null&&y===null)return 0;
   if(x===null)return 1;if(y===null)return -1;
   return x-y;
  });
 }
 renderChapterPicker();
}
function movePickerChapter(index,delta){
 if(!chapterPickerOrder)return;
 const at=chapterPickerOrder.indexOf(index),to=at+delta;
 if(at<0||to<0||to>=chapterPickerOrder.length)return;
 [chapterPickerOrder[at],chapterPickerOrder[to]]=[chapterPickerOrder[to],chapterPickerOrder[at]];
 renderChapterPicker();
}
function excludePickerDuplicates(){
 if(!chapterPickerExcluded)throw Error("尚未開啟章節視窗");
 const dup=analyzePickerDuplicates(),start=Number($("start-chapter").value),end=Number($("end-chapter").value);
 let count=0;
 for(const index of dup.keys())if(index>=start&&index<=end&&!chapterPickerExcluded.has(index)){chapterPickerExcluded.add(index);count++;}
 renderChapterPicker();notice("已排除 "+count+" 個重複章節；尚未寫入雲端");
}

function pickerMatchingIndices(){
 if(!parsedCatalog||!Array.isArray(parsedCatalog.chapter_titles))return [];
 const start=Number($("start-chapter").value),end=Number($("end-chapter").value),q=$("chapter-search").value.trim().toLowerCase();
 if(!Number.isSafeInteger(start)||!Number.isSafeInteger(end)||start<1||end<start||end>parsedCatalog.total_chapters)throw Error("請先設定有效的章節範圍");
 const duplicateOnly=$("duplicate-only").checked,duplicate=duplicateOnly?analyzePickerDuplicates():null;
 return (chapterPickerOrder||[]).filter(i=>{
  if(i<start||i>end)return false;
  if(duplicateOnly&&!duplicate.has(i))return false;
  const title=String(parsedCatalog.chapter_titles[i-1]||"");
  return !q||title.toLowerCase().includes(q)||String(i).includes(q);
 });
}
function pickerStatus(matched){
 const start=Number($("start-chapter").value),end=Number($("end-chapter").value);
 const selected=(chapterPickerOrder||[]).filter(i=>!chapterPickerExcluded.has(i)).length;
 const duplicates=analyzePickerDuplicates().size;
 text($("chapter-picker-status"),"本次選定 "+selected+" / "+(end-start+1)+" 章；重複章節 "+duplicates+" 筆；符合搜尋 "+matched+" 章，畫面最多顯示前 250 章");
}
function renderChapterPicker(){
 const matched=pickerMatchingIndices(),container=$("chapter-list"),duplicate=analyzePickerDuplicates();
 container.replaceChildren();
 for(const index of matched.slice(0,250)){
  const label=document.createElement("label");label.className="chapter-item";
  const check=document.createElement("input");check.type="checkbox";check.checked=!chapterPickerExcluded.has(index);
  const span=document.createElement("span");span.textContent="UUID "+index+"｜編號 "+(chapterPickerOrder.indexOf(index)+1)+"｜"+
    (parsedCatalog.chapter_parts?.[index-1]?.d||"未標示章號")+"｜"+String(parsedCatalog.chapter_titles[index-1]||"");
  const match=duplicate.get(index);
  if(match){const small=document.createElement("small");small.className="duplicate-reason";
    small.textContent="重複："+match.reason.join("、")+"；前一筆 UUID "+match.parents.join("、");span.append(small);}
  const move=document.createElement("span");move.className="chapter-reorder";
  for(const [delta,word] of [[-1,"↑"],[1,"↓"]]){
   const b=document.createElement("button");b.type="button";b.className="secondary";b.textContent=word;
   b.setAttribute("aria-label","第"+index+"章"+(delta<0?"上移":"下移"));
   b.addEventListener("click",()=>movePickerChapter(index,delta));move.append(b);
  }
  check.addEventListener("change",()=>{
   if(check.checked)chapterPickerExcluded.delete(index);else chapterPickerExcluded.add(index);
   pickerStatus(matched.length);
  });
  label.append(check,span,move);container.append(label);
 }
 pickerStatus(matched.length);
}
function openChapterPicker(){
 if(!parsedCatalog||!Array.isArray(parsedCatalog.chapter_titles)||parsedCatalog.chapter_titles.length!==parsedCatalog.total_chapters)
  throw Error("完整目錄尚未載入，請重新解析目錄");
 const start=Number($("start-chapter").value),end=Number($("end-chapter").value);
 chapterPickerExcluded=new Set(excludedNumbers($("excluded").value,start,end));
 const orderText=$("chapter-order").value.trim();
 chapterPickerOrder=orderText?readOrder(orderText,start,end,[...chapterPickerExcluded]):Array.from({length:end-start+1},(_,i)=>i+start);
 $("chapter-search").value="";renderChapterPicker();$("chapter-dialog").showModal();
}
function pickerBulk(mode){
 if(!chapterPickerExcluded)throw Error("尚未開啟選章視窗");
 for(const i of pickerMatchingIndices()){
  if(mode==="none")chapterPickerExcluded.add(i);
  else if(mode==="all")chapterPickerExcluded.delete(i);
  else if(chapterPickerExcluded.has(i))chapterPickerExcluded.delete(i);
  else chapterPickerExcluded.add(i);
 }
 renderChapterPicker();
}
function saveChapterPicker(){
 if(!chapterPickerExcluded)throw Error("尚未開啟選章視窗");
 $("excluded").value=[...chapterPickerExcluded].sort((a,b)=>a-b).join(",");
 const result=chapterPickerOrder.filter(i=>!chapterPickerExcluded.has(i));
 const start=Number($("start-chapter").value),end=Number($("end-chapter").value);
 const expected=Array.from({length:end-start+1},(_,i)=>i+start).filter(i=>!chapterPickerExcluded.has(i));
 $("chapter-order").value=result.every((n,i)=>n===expected[i])?"":result.join(",");
 $("renumber").checked=$("renumber").checked||Boolean($("chapter-order").value);
 $("chapter-dialog").close();chapterPickerExcluded=null;chapterPickerOrder=null;
 notice("章節選擇已套用到排除清單；加入佇列前仍可調整");
}
async function parseCatalog(){
 if(!token)throw Error("解析目錄需要 GitHub Token");
 const url=$("catalog-url").value.trim(),source=getSource(url);
 $("source").value=source.id;parsedCatalog=null;$("chapter-selection-tools").classList.add("hidden");
 const requestId=crypto.randomUUID().slice(0,8);
 const btn=$("parse-btn");btn.disabled=true;
 try{
 text($("parse-status"),"已提交雲端目錄解析，正在取得結果…");
 await request("/actions/workflows/web-catalog-preview.yml/dispatches",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ref:"main",inputs:{catalog_url:url,request_id:requestId}})});
 let run=null;for(let i=0;i<45;i++){
  await new Promise(resolve=>setTimeout(resolve,4000));
  const data=await request("/actions/workflows/web-catalog-preview.yml/runs?event=workflow_dispatch&per_page=30");
  run=(data.workflow_runs||[]).find(x=>(x.display_title||"").includes(requestId));
  if(run?.status==="completed")break;
 }
 if(!run||run.status!=="completed")throw Error("目錄解析仍在執行，請到 Actions 查看紀錄");
 if(run.conclusion!=="success")throw Error("目錄解析失敗，請查看 Run #"+run.id);
 const content=await request("/contents/web-previews/"+encodeURIComponent(requestId)+".json?ref="+QUEUE_BRANCH);
 const data=JSON.parse(fromB64(content.content));
 if(data.requested_url!==url)throw Error("解析結果與輸入網址不一致");
 $("catalog-url").value=data.catalog_url;
 parsedCatalog=data;text($("book-title"),data.book_title);$("book-title").value=data.book_title;
 $("end-chapter").value=Math.min(10,data.total_chapters);
 const box=$("catalog-preview");box.replaceChildren();
 const title=document.createElement("strong");title.textContent=data.book_title+" · "+data.total_chapters+" 章";box.append(title);
 const listEl=document.createElement("ul");(data.chapter_titles||[]).slice(0,40).forEach((name,i)=>{const item=document.createElement("li");item.textContent=(i+1)+". "+name;listEl.append(item);});box.append(listEl);box.classList.remove("hidden");$("chapter-selection-tools").classList.remove("hidden");
 text($("parse-status"),"已解析完成（"+data.source_id+"，"+data.total_chapters+" 章）");
 }finally{btn.disabled=false;}
}
function taipeiPreset(days,hours){
 const parts=Object.fromEntries(new Intl.DateTimeFormat("en-US",{timeZone:"Asia/Taipei",year:"numeric",month:"2-digit",day:"2-digit"}).formatToParts(new Date()).filter(x=>x.type!=="literal").map(x=>[x.type,Number(x.value)]));
 const iso=new Date(Date.UTC(parts.year,parts.month-1,parts.day+Number(days))).toISOString().slice(0,10);
 return iso+"T"+hours;
}
function renderPublish(){
 const scheduled=$("publish-scheduled").checked;$("publish-schedule").classList.toggle("hidden",!scheduled);
 const when=$("publish-at").value;
 if(!scheduled){text($("publish-preview"),"立即公開：上傳成功後公開");return;}
 if(!when){text($("publish-preview"),"尚未選擇公開時間");return;}
 const date=new Date(when+":00+08:00"),past=Number.isNaN(date.getTime())||date.getTime()<=Date.now();
 text($("publish-preview"),past?"排定時間已過：上傳成功後依策略 A 立即公開":"預定台灣時間 "+when.replace("T"," ")+" 公開（所有 Parts 同步）");
}
function setPublishPreset(day,hours){
 $("publish-scheduled").checked=true;$("publish-at").value=taipeiPreset(day,hours);renderPublish();
}

function switchTab(which){
 document.querySelectorAll(".tab").forEach(e=>e.classList.toggle("active",e.id===which));
 document.querySelectorAll(".nav").forEach(e=>e.classList.toggle("active",e.dataset.tab===which));
 const headings={overview:["製作總覽","同步 GitHub 雲端佇列與製作進度"],create:["新增小說","支援九個來源，透過雲端解析章節與排程"],runs:["製作紀錄","GitHub Actions 即時執行狀態"]};
 text($("page-title"),headings[which][0]);text($("page-subtitle"),headings[which][1]);
}
function bind(id,event,fn){const el=$(id);if(el)el.addEventListener(event,e=>Promise.resolve().then(()=>fn(e)).catch(err=>notice(err.message,"error")));}
document.addEventListener("DOMContentLoaded",()=>{
 const select=$("source");SOURCES.forEach(s=>{const opt=document.createElement("option");opt.value=s.id;opt.textContent=s.name;select.append(opt);});
 document.querySelectorAll(".nav").forEach(b=>b.addEventListener("click",()=>switchTab(b.dataset.tab)));
 // No PAT input; GitHub itself confirms every write operation.
 bind("refresh-btn","click",refresh);bind("dispatch-btn","click",triggerDispatcher);bind("book-form","submit",submitBook);
 bind("batch-btn","click",batchAdd);bind("parse-btn","click",parseCatalog);
 bind("edit-form","submit",saveEditor);bind("review-form","submit",saveReview);
 bind("open-chapter-picker","click",openChapterPicker);bind("chapter-search","input",renderChapterPicker);
 bind("chapter-all","click",()=>pickerBulk("all"));bind("chapter-none","click",()=>pickerBulk("none"));bind("chapter-invert","click",()=>pickerBulk("invert"));
 bind("chapter-picker-save","click",saveChapterPicker);
 bind("chapter-picker-cancel","click",()=>{$("chapter-dialog").close();chapterPickerExcluded=null;chapterPickerOrder=null;});
 bind("chapter-exclude-duplicates","click",excludePickerDuplicates);
 bind("chapter-sort-number","click",()=>sortPickerOrder("number"));bind("chapter-sort-original","click",()=>sortPickerOrder("original"));
 for(const id of ["duplicate-number","duplicate-name","duplicate-pair","duplicate-only"])bind(id,"change",renderChapterPicker);
 bind("progress-close","click",()=>$("progress-dialog").close());bind("edit-cancel","click",()=>{$("edit-dialog").close();editorTaskId=null;});bind("review-cancel","click",()=>{$("review-dialog").close();reviewTaskId=null;});
 bind("publish-immediate","change",renderPublish);bind("publish-scheduled","change",renderPublish);
 bind("publish-at","change",renderPublish);
 document.querySelectorAll("[data-publish-day]").forEach(b=>b.addEventListener("click",()=>setPublishPreset(Number(b.dataset.publishDay),b.dataset.publishHour)));
 renderPublish();
 bind("queue-all","change",()=>{const on=$("queue-all").checked;selectedPending=new Set(on?queueTasks().map(t=>t.task_id):[]);renderQueue();});
 bind("rank-search","input",renderRanking);bind("rank-category","change",renderRanking);
 bind("rank-select-all","click",()=>{for(const n of filteredRanking())rankingSelected.add(n.catalog_url);renderRanking();});
 bind("rank-select-none","click",()=>{rankingSelected.clear();renderRanking();});
 bind("rank-copy","click",copyRankingUrls);
 loadRanking();
 bind("catalog-url","input",()=>{parsedCatalog=null;$("catalog-preview").classList.add("hidden");$("chapter-selection-tools").classList.add("hidden");});
 refresh();
});
