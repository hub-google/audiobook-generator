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
let progressGeneration=0,progressTaskId=null,progressTimer=null,progressJobRuns="",progressBusy=false;
let editCleanerInitial="",queue=null,queueSha=null,latestRuns=[],parsedCatalog=null,editorTaskId=null,reviewTaskId=null,chapterPickerExcluded=null,chapterPickerOrder=null,rankingNovels=[],rankingSelected=new Set(),selectedPending=new Set(),selectedCompleted=new Set(),chapterSelectedRows=new Set(),chapterPage=0,chapterPageSize=200,chapterEditingIndex=null,editorParsedCatalog=null,chapterPickerMode="create",pickerOriginalCatalog=null,pickerOriginalDuplicateState=null;
const $=id=>document.getElementById(id), text=(node,value)=>{node.textContent=value==null?"":String(value)};
function notice(message,kind="ok"){const n=$("message");text(n,message);n.className="notice"+(kind==="error"?" error":"");}
const dateLabel=v=>v?new Date(v).toLocaleString("zh-TW",{timeZone:"Asia/Taipei",hour12:false}):"—";
const escapeHTML=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function toB64(value){const bytes=new TextEncoder().encode(value);let result="";for(let i=0;i<bytes.length;i+=8192)result+=String.fromCharCode(...bytes.slice(i,i+8192));return btoa(result);}
function fromB64(value){const raw=atob(value.replace(/\s/g,""));return new TextDecoder().decode(Uint8Array.from(raw,c=>c.charCodeAt(0)));}
function getSource(url){let parsed;try{parsed=new URL(url)}catch{throw new Error("請輸入有效的小說網址");}if(!["http:","https:"].includes(parsed.protocol)||parsed.username||parsed.password)throw new Error("僅接受 HTTP(S) 網址");const source=SOURCES.find(s=>s.hosts.includes(parsed.hostname.toLowerCase()));if(!source)throw new Error("目前不支援此網址的網站");return source;}
async function request(path,options={}){
 if(options.method&&options.method.toUpperCase()!=="GET")throw Error("GitHub Pages 僅允許公開讀取；寫入操作由 GitHub Issue 驗證執行");
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
function canEditAfterCancel(task){
 return ["running","dispatching","canceling"].includes(task.status)&&
   task.workflow_phase==="processing"&&Number.isSafeInteger(Number(task.run_id))&&Number(task.run_id)>0;
}
function canEditTask(task){return canEdit(task)||canEditAfterCancel(task);}
function makeBtn(label,action,id,disabled=false){const b=document.createElement("button");b.type="button";b.textContent=label;b.className="secondary";b.disabled=disabled;b.addEventListener("click",()=>Promise.resolve().then(()=>action(id)).catch(e=>notice(e.message,"error")));return b;}
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
 div.append(makeBtn("整包下載",window.exportArtifacts,task.task_id,false,false));
 if(["queued","paused","waiting_review","preparing_assets","needs_attention"].includes(task.status))div.append(makeBtn("手動封面",window.openManualCover,task.task_id));
 if(live(task))div.append(makeBtn("取消本次 Run",window.cancelRunningTask,task.task_id));
 if(task.status==="waiting_review"){div.append(makeBtn("查看審核資料",async()=>{window.open("https://github.com/"+REPO+"/actions/runs/"+(task.cover_run_id||task.run_id),"_blank","noopener");},task.task_id,false,false));div.append(makeBtn("確認審核並繼續",window.openReview,task.task_id,false));}
 if(["queued","paused","interrupted","stopped"].includes(task.status)){
  div.append(makeBtn("編輯",openEditor,task.task_id,!canEdit(task)));
  div.append(makeBtn("↑",id=>window.moveTask(id,-1),task.task_id,index===0||!canEdit(task)||!canEdit(queueTasks()[index-1])));
  div.append(makeBtn("↓",id=>window.moveTask(id,1),task.task_id,index===queueTasks().length-1||!canEdit(task)||!canEdit(queueTasks()[index+1])));
  if(task.status==="queued")div.append(makeBtn("暫停",id=>window.setTaskState(id,"paused"),task.task_id));
  else div.append(makeBtn("重新排程",window.requeueTask,task.task_id));
  if(["paused","interrupted","stopped"].includes(task.status))div.append(makeBtn("標為已完成",window.markCompleted,task.task_id));
  div.append(makeBtn("刪除",window.deleteTask,task.task_id));
 }
 if(canEditAfterCancel(task))div.append(makeBtn("編輯並重新排程",openEditor,task.task_id));
 if(["waiting_retry","needs_attention"].includes(task.status))
  div.append(makeBtn("重新排程",window.requeueTask,task.task_id));
 tr.addEventListener("dblclick",event=>{
  if(!canEditTask(task)||event.target.closest("button,input,a,select,textarea"))return;
  openEditor(task.task_id);
 });
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
 const name=document.createElement("strong");name.textContent=t.book_title||"未知小說";const status=document.createElement("span");status.className="muted";status.textContent=dateLabel(t.completed_at||t.updated_at);const restore=makeBtn("移回佇列",window.restoreCompleted,t.task_id);const download=makeBtn("成品下載",showProgress,t.task_id,false,false);
 const bundle=makeBtn("整包下載",window.exportArtifacts,t.task_id,false,false);
 row.append(checkbox,name,status,download,bundle,restore);list.append(row);});
 updateBatchStatus();
}
async function refresh(){
 try{const res=await readQueue();queue=res.data;queueSha=res.sha;renderQueue();
  try{const data=await request("/actions/runs?per_page=35");latestRuns=data.workflow_runs||[];renderRuns();}catch(error){notice("Actions 紀錄載入失敗："+error.message,"error");}
  renderRanking();
  $("connection-pill").textContent="GitHub 登入操作";$("connection-pill").className="pill ok";
 }catch(e){notice("同步失敗："+e.message,"error");}
}
let overviewPollInFlight=false;
async function syncOverviewQueue(){
 if(!queue||document.hidden||document.querySelector("dialog[open]")||overviewPollInFlight)
  return false;
 overviewPollInFlight=true;
 try{
  const url="https://raw.githubusercontent.com/"+REPO+"/"+QUEUE_BRANCH+"/"+QUEUE_PATH+
   "?_="+Date.now();
  const response=await fetch(url,{cache:"no-store"});
  if(!response.ok)throw Error("HTTP "+response.status);
  const latest=await response.json();
  if(!Array.isArray(latest.queue)||!Array.isArray(latest.completed)||
     !Number.isSafeInteger(latest.revision))throw Error("Invalid AUDIO queue payload");
  if(document.hidden||document.querySelector("dialog[open]"))return false;
  if(latest.revision>Number(queue.revision||0)){
   queue=latest;queueSha=null;renderQueue();
   return true;
  }
  return false;
 }catch(error){
  // The user's manual refresh button remains available. A brief CDN outage
  // must not overwrite the live page with a misleading empty queue.
  return false;
 }finally{overviewPollInFlight=false;}
}
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

function renderProgressSummary(t){
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
  "異常或原因："+(t.reason||"無")
 ].join("\n");
 text($("progress-detail"),detail);
}
function progressRunIds(t){
 return [t.scrape_run_id,t.cover_run_id,t.processing_run_id,t.run_id].filter(Boolean).join(",");
}
function scheduleProgressPoll(generation){
 if(progressTimer!==null){clearTimeout(progressTimer);progressTimer=null;}
 if(generation!==progressGeneration||!$("progress-dialog").open)return;
 progressTimer=setTimeout(()=>{
  progressTimer=null;
  if(document.hidden){scheduleProgressPoll(generation);return;}
  void pollProgressNow();
 },15000);
}
async function pollProgressNow(forceJobs=false){
 const id=progressTaskId,generation=progressGeneration;
 if(!id||!$("progress-dialog").open||progressBusy)return;
 progressBusy=true;
 if(progressTimer!==null){clearTimeout(progressTimer);progressTimer=null;}
 text($("progress-auto-status"),"正在同步雲端佇列…");
 try{
  // Public read-only raw JSON avoids burning GitHub unauthenticated REST quota
  // every 15 seconds. Secrets and mutation operations stay in GitHub Actions.
  const url="https://raw.githubusercontent.com/"+REPO+"/"+QUEUE_BRANCH+"/"+QUEUE_PATH+
   "?_="+Date.now();
  const response=await fetch(url,{cache:"no-store"});
  if(!response.ok)throw Error("GitHub 佇列讀取 HTTP "+response.status);
  const latest=await response.json();
  if(!Array.isArray(latest.queue)||!Array.isArray(latest.completed))
   throw Error("雲端佇列內容不完整");
  if(generation!==progressGeneration||!$("progress-dialog").open)return;
  queue=latest;renderQueue();
  const task=[...latest.queue,...latest.completed].find(t=>t.task_id===id);
  if(!task){
   text($("progress-auto-status"),"這項任務已不在佇列，請確認是否已刪除。");
   return;
  }
  renderProgressSummary(task);
  const runIds=progressRunIds(task);
  if(forceJobs||runIds!==progressJobRuns){
   progressJobRuns=runIds;
   await renderRunJobDetails(task);
  }
  if(generation!==progressGeneration||!$("progress-dialog").open)return;
  text($("progress-auto-status"),"最近同步："+new Date().toLocaleTimeString("zh-TW",{
   timeZone:"Asia/Taipei",hour12:false})+"；每 15 秒自動更新佇列狀態");
 }catch(error){
  if(generation===progressGeneration&&$("progress-dialog").open)
   text($("progress-auto-status"),"同步失敗："+error.message+"；會自動重試，亦可按鈕重新整理");
 }finally{
  progressBusy=false;
  scheduleProgressPoll(generation);
 }
}
async function showProgress(id){
 const t=[...queueTasks(),...(queue?.completed||[])].find(x=>x.task_id===id);
 if(!t)throw Error("找不到任務");
 progressGeneration++;progressTaskId=id;progressJobRuns=progressRunIds(t);
 if(progressTimer!==null){clearTimeout(progressTimer);progressTimer=null;}
 renderProgressSummary(t);
 text($("progress-auto-status"),"工作步驟載入中；佇列將每 15 秒自動同步");
 $("progress-dialog").showModal();
 const generation=progressGeneration;
 try{await renderRunJobDetails(t);}
 finally{scheduleProgressPoll(generation);}
}
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
function naturalChapterOrder(task){
 const order=task.chapter_order||[];
 if(!order.length)return true;
 const start=Number(task.start_chapter||1),end=Number(task.end_chapter||0);
 const skipped=new Set((task.excluded_chapters||[]).map(Number));
 if(order.length!==end-start+1-skipped.size)return false;
 let expected=start;
 for(const value of order){
  while(skipped.has(expected)&&expected<=end)expected++;
  if(value!==expected)return false;
  expected++;
 }
 return true;
}
function openEditor(id){
 const task=queueTasks().find(x=>x.task_id===id);
 if(!task||!canEditTask(task))throw Error("此任務目前無法編輯（執行中的任務需有可取消的影片 Run）");
 editorTaskId=id;editorParsedCatalog=null;
 text($("edit-catalog-status"),"尚未載入完整目錄；可先編輯文字欄位，或載入完整目錄視覺化選章。");
 text($("edit-book-url"),task.catalog_url);$("edit-name").value=task.book_title||"";$("edit-start").value=task.start_chapter||1;$("edit-end").value=task.end_chapter||10;
 $("edit-excluded").value=(task.excluded_chapters||[]).join(",");$("edit-renumber").checked=!!task.renumber_selected;$("edit-publish").value=formatPublish(task.publish_at);
 $("edit-titles").value=formatOverrides(task.chapter_title_overrides);$("edit-numbers").value=formatOverrides(task.chapter_normalized_number_overrides);
 const detection=task.duplicate_detection||task.profile_snapshot?.duplicate_detection||{};
 $("edit-duplicate-number").checked=detection.use_normalized_number!==false;
 $("edit-duplicate-name").checked=detection.use_chapter_name!==false;
 $("edit-duplicate-pair").checked=!!detection.use_number_and_name;
 $("edit-order").value=naturalChapterOrder(task)?"":(task.chapter_order||[]).join(",");
 editCleanerInitial=(task.ad_review?.remove_patterns||task.profile_snapshot?.cleaner_remove_patterns||[]).join("\n");
 $("edit-cleaner").value=editCleanerInitial;
 $("edit-dialog").showModal();
 void loadCurrentBookCleaner(task).catch(e=>{
  text($("edit-cleaner-status"),"無法讀取最新書籍設定；保留佇列內的原設定。"+e.message);
 });
}
async function loadCurrentBookCleaner(task){
 const selected=task.task_id,previous=$("edit-cleaner").value;
 text($("edit-cleaner-status"),"正在比對目前雲端儲存的清理設定…");
 const file=await request("/contents/audiobook-book-profiles.json?ref="+QUEUE_BRANCH);
 const data=JSON.parse(fromB64(file.content));
 const profile=Object.values(data.books||{}).find(p=>p.catalog_url===task.catalog_url);
 if(editorTaskId!==selected||!$("edit-dialog").open||$("edit-cleaner").value!==previous)return;
 editCleanerInitial=(profile?.cleaner_remove_patterns||[]).join("\n");
 $("edit-cleaner").value=editCleanerInitial;
 text($("edit-cleaner-status"),profile?
  "已載入此書最新雲端清理設定；修改後隨任務儲存。":
  "此書尚未有清理設定；如有需要可新增。");
}
function openReview(id){const task=queueTasks().find(t=>t.task_id===id);if(!task||task.status!=="waiting_review")throw Error("任務不在待審核狀態");
 reviewTaskId=id;$("review-patterns").value=(task.ad_review?.remove_patterns||task.profile_snapshot?.cleaner_remove_patterns||[]).join("\n");
 $("review-dialog").showModal();
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
function pickerField(name){
 if(chapterPickerMode!=="edit")return $(name);
 const names={"start-chapter":"edit-start","end-chapter":"edit-end",
  "excluded":"edit-excluded","chapter-order":"edit-order",
  "title-overrides":"edit-titles","number-overrides":"edit-numbers",
  "renumber":"edit-renumber"};
 return $(names[name]||name);
}
function closeChapterPicker(){
 $("chapter-dialog").close();
 chapterPickerExcluded=null;chapterPickerOrder=null;chapterSelectedRows.clear();
 if(chapterPickerMode==="edit"){
  parsedCatalog=pickerOriginalCatalog;
  if(pickerOriginalDuplicateState){
   for(const [id,checked] of Object.entries(pickerOriginalDuplicateState))$(id).checked=checked;
  }
 }
 chapterPickerMode="create";
 pickerOriginalCatalog=null;pickerOriginalDuplicateState=null;
}
function setEditorCatalog(data,requestedUrl){
 const task=queueTasks().find(t=>t.task_id===editorTaskId);
 if(!task||!$("edit-dialog").open||task.catalog_url!==requestedUrl||data.requested_url!==requestedUrl)
  throw Error("目錄結果與目前編輯的小說不相符");
 if(!Number.isSafeInteger(data.total_chapters)||data.total_chapters<1||
    data.chapter_titles?.length!==data.total_chapters||data.chapter_parts?.length!==data.total_chapters)
  throw Error("目錄章節不完整，無法視覺化編輯");
 if(Number($("edit-end").value)>data.total_chapters)
  throw Error("已選章節超過最新目錄章數，請先確認章數變化");
 editorParsedCatalog=structuredClone(data);
 text($("edit-catalog-status"),"已載入完整目錄，共 "+data.total_chapters+" 章，可選章、編輯章名與重排。");
 return editorParsedCatalog;
}
function openEditorChapterPicker(){
 if(!editorParsedCatalog)throw Error("請先載入這本小說的完整目錄");
 if(chapterPickerMode!=="create"||$("chapter-dialog").open)throw Error("目前有其他選章視窗尚未關閉");
 chapterPickerMode="edit";
 pickerOriginalCatalog=parsedCatalog;
 pickerOriginalDuplicateState=Object.fromEntries(
  ["duplicate-number","duplicate-name","duplicate-pair"].map((id)=>[id,$(id).checked]));
 for(const key of ["number","name","pair"])
  $("duplicate-"+key).checked=$("edit-duplicate-"+key).checked;
 parsedCatalog=editorParsedCatalog;
 try{openChapterPicker("edit");}
 catch(e){closeChapterPicker();throw e;}
}
function currentChapterOverrides(){
 if(!parsedCatalog)return {};
 const end=parsedCatalog.total_chapters;
 return readOverrides(pickerField("number-overrides").value||"",1,end,true);
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
 const dup=analyzePickerDuplicates(),start=Number(pickerField("start-chapter").value),end=Number(pickerField("end-chapter").value);
 let count=0;
 for(const index of dup.keys())if(index>=start&&index<=end&&!chapterPickerExcluded.has(index)){chapterPickerExcluded.add(index);count++;}
 renderChapterPicker();notice("已排除 "+count+" 個重複章節；尚未寫入雲端");
}

function pickerDuplicateGroups(duplicates){
 const groups=new Set(duplicates.keys());
 for(const info of duplicates.values())for(const parent of info.parents)groups.add(parent);
 return groups;
}
function chapterEffectiveNumber(index){
 const parts=parsedCatalog?.chapter_parts?.[index-1]||{};
 return currentChapterOverrides()[String(index)]||String(parts.n||"");
}
function chapterDirectMatches(index){
 const parts=parsedCatalog?.chapter_parts||[],selected=parts[index-1]||{};
 const num=chapterEffectiveNumber(index),name=String(selected.c||"");
 const useNum=$("duplicate-number").checked,useName=$("duplicate-name").checked,usePair=$("duplicate-pair").checked;
 return parts.flatMap((p,other)=>{
  const otherIndex=other+1;if(otherIndex===index)return [];
  const number=chapterEffectiveNumber(otherIndex),comparison=String(p.c||"");
  const reasons=[];
  if(useNum&&num&&num===number)reasons.push("網站章節數正規化");
  if(useName&&name&&name===comparison)reasons.push("章節名稱（去除空白）");
  if(usePair&&num&&name&&num===number&&name===comparison)reasons.push("章號與章名同時符合");
  return reasons.length?[{index:otherIndex,reasons}]:[];
 });
}
function showChapterDuplicates(index){
 const parts=parsedCatalog.chapter_parts[index-1],matches=chapterDirectMatches(index);
 text($("chapter-duplicates-title"),"重複章節明細｜UUID "+index);
 text($("chapter-duplicates-status"),"當前章節："+parsedCatalog.chapter_titles[index-1]+"；直接符合 "+matches.length+" 筆（不串聯）");
 const box=$("chapter-duplicates-list");box.replaceChildren();
 for(const match of matches){
  const p=parsedCatalog.chapter_parts[match.index-1],row=document.createElement("p");
  row.textContent="UUID "+match.index+"｜正規化 "+chapterEffectiveNumber(match.index)+"｜原章號 "+(p.d||"—")+"｜"+p.name+"｜"+match.reasons.join("、");
  box.append(row);
 }
 if(!matches.length)text(box,"沒有直接符合的章節");
 $("chapter-duplicates-dialog").showModal();
}
function openChapterEdit(index){
 chapterEditingIndex=index;
 const parts=parsedCatalog.chapter_parts[index-1];
 text($("chapter-edit-uuid"),"網站 UUID "+index+"｜原始顯示章號："+(parts.d||"無")+"｜網頁目錄來源不可改");
 $("chapter-edit-name").value=parts.name||parsedCatalog.chapter_titles[index-1]||"";
 $("chapter-edit-number").value=currentChapterOverrides()[String(index)]||"";
 $("chapter-edit-dialog").showModal();
}
function saveChapterEdit(){
 const index=chapterEditingIndex;if(!index)throw Error("未選取章節");
 const name=$("chapter-edit-name").value.trim(),n=$("chapter-edit-number").value.trim();
 if(!name)throw Error("章節名稱不可空白");
 if(n&&!/^[1-9]\d*(?:\.\d+)?$/.test(n))throw Error("正規化章號必須為大於 0 的整數或小數");
 const parts=parsedCatalog.chapter_parts[index-1],full=[parts.d,name].filter(Boolean).join(" ");
 const titles=readOverrides(pickerField("title-overrides").value||"",1,parsedCatalog.total_chapters);
 const numbers=currentChapterOverrides();titles[String(index)]=full;
 if(n)numbers[String(index)]=n;else delete numbers[String(index)];
 pickerField("title-overrides").value=formatOverrides(titles);
 pickerField("number-overrides").value=formatOverrides(numbers);
 parsedCatalog.chapter_titles[index-1]=full;
 parts.name=name;parts.c=name.replace(/\s+/g,""); // same rule as normalize_chapter_name_for_comparison
 $("chapter-edit-dialog").close();chapterEditingIndex=null;renderChapterPicker();
}
function moveSelectedChapters(delta){
 const selected=chapterSelectedRows;if(!selected.size)throw Error("請先用多選欄勾選要移動的章節");
 if(delta<0){for(let i=1;i<chapterPickerOrder.length;i++)
  if(selected.has(chapterPickerOrder[i])&&!selected.has(chapterPickerOrder[i-1]))
   [chapterPickerOrder[i-1],chapterPickerOrder[i]]=[chapterPickerOrder[i],chapterPickerOrder[i-1]];
 }else{for(let i=chapterPickerOrder.length-2;i>=0;i--)
  if(selected.has(chapterPickerOrder[i])&&!selected.has(chapterPickerOrder[i+1]))
   [chapterPickerOrder[i+1],chapterPickerOrder[i]]=[chapterPickerOrder[i],chapterPickerOrder[i+1]];
 }
 pickerField("renumber").checked=true;renderChapterPicker();
}
function pickerMatchingIndices(){
 if(!parsedCatalog||!Array.isArray(parsedCatalog.chapter_titles))return [];
 const start=Number(pickerField("start-chapter").value),end=Number(pickerField("end-chapter").value),q=$("chapter-search").value.trim().toLowerCase();
 if(!Number.isSafeInteger(start)||!Number.isSafeInteger(end)||start<1||end<start||end>parsedCatalog.total_chapters)throw Error("請先設定有效的章節範圍");
 const duplicateOnly=$("duplicate-only").checked,duplicate=duplicateOnly?analyzePickerDuplicates():null;
 return (chapterPickerOrder||[]).filter(i=>{
  if(!$("chapter-show-all").checked&&(i<start||i>end))return false;
  if(duplicateOnly&&!pickerDuplicateGroups(duplicate).has(i))return false;
  const title=String(parsedCatalog.chapter_titles[i-1]||"");
  const p=parsedCatalog.chapter_parts?.[i-1]||{};
  return !q||[title,i,p.d,p.n,p.name,chapterEffectiveNumber(i)].some(x=>String(x??"").toLowerCase().includes(q));
 });
}
function pickerStatus(matched){
 const start=Number(pickerField("start-chapter").value),end=Number(pickerField("end-chapter").value);
 const selected=(chapterPickerOrder||[]).filter(i=>i>=start&&i<=end&&!chapterPickerExcluded.has(i)).length;
 const duplicates=analyzePickerDuplicates().size;
 text($("chapter-picker-status"),"本次選定 "+selected+" / "+(end-start+1)+" 章；重複章節 "+duplicates+" 筆；符合搜尋 "+matched+" 章；多選移動 "+chapterSelectedRows.size+" 章");
}
function renderChapterPicker(){
 const matched=pickerMatchingIndices(),container=$("chapter-list"),duplicate=analyzePickerDuplicates();
 container.replaceChildren();
 const totalPages=Math.max(1,Math.ceil(matched.length/chapterPageSize));chapterPage=Math.min(chapterPage,totalPages-1);
 text($("chapter-page-label"),"第 "+(chapterPage+1)+" / "+totalPages+" 頁 · 共 "+matched.length+" 筆");
 $("chapter-prev").disabled=chapterPage<=0;$("chapter-next").disabled=chapterPage>=totalPages-1;
 for(const index of matched.slice(chapterPage*chapterPageSize,(chapterPage+1)*chapterPageSize)){
  const label=document.createElement("label");label.className="chapter-item";
  label.draggable=true;
  label.dataset.uuid=String(index);
  label.addEventListener("dragstart",event=>{
   if(!chapterSelectedRows.has(index)){chapterSelectedRows.clear();chapterSelectedRows.add(index);}
   event.dataTransfer.setData("text/plain",String(index));event.dataTransfer.effectAllowed="move";
  });
  label.addEventListener("dragover",event=>{event.preventDefault();event.dataTransfer.dropEffect="move";});
  label.addEventListener("drop",event=>{
   event.preventDefault();
   const moving=chapterPickerOrder.filter(uuid=>chapterSelectedRows.has(uuid));
   if(!moving.length||moving.includes(index))return;
   const remaining=chapterPickerOrder.filter(uuid=>!chapterSelectedRows.has(uuid));
   const targetAt=remaining.indexOf(index);
   if(targetAt<0)return;
   chapterPickerOrder=remaining.slice(0,targetAt).concat(moving,remaining.slice(targetAt));
   pickerField("renumber").checked=true;
   renderChapterPicker();
  });
  const multi=document.createElement("input");multi.type="checkbox";multi.checked=chapterSelectedRows.has(index);
 multi.setAttribute("aria-label","多選 UUID "+index);
 multi.addEventListener("change",()=>{if(multi.checked)chapterSelectedRows.add(index);else chapterSelectedRows.delete(index);pickerStatus(matched.length);});
 const check=document.createElement("input");check.type="checkbox";check.checked=!chapterPickerExcluded.has(index);
 check.disabled=index<Number(pickerField("start-chapter").value)||index>Number(pickerField("end-chapter").value);
 check.setAttribute("aria-label","製作 UUID "+index);
  const span=document.createElement("span");
 const parts=parsedCatalog.chapter_parts?.[index-1]||{},sourceNum=parts.d||"—";
 const orderNumber=chapterPickerOrder.filter(x=>x>=Number(pickerField("start-chapter").value)&&x<=Number(pickerField("end-chapter").value)&&!chapterPickerExcluded.has(x)).indexOf(index)+1;
 span.textContent="UUID "+index+"｜編號 "+(orderNumber||"—")+"｜正規化 "+(chapterEffectiveNumber(index)||"—")+"｜原章號 "+sourceNum+"｜"+(parts.name||parsedCatalog.chapter_titles[index-1]||"");
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
  const edit=document.createElement("button");edit.type="button";edit.className="secondary";edit.textContent="編輯";edit.addEventListener("click",()=>openChapterEdit(index));move.append(edit);
  const details=document.createElement("button");details.type="button";details.className="secondary";details.textContent="重複明細";details.disabled=!chapterDirectMatches(index).length;details.addEventListener("click",()=>showChapterDuplicates(index));move.append(details);
  label.append(multi,check,span,move);container.append(label);
 }
 pickerStatus(matched.length);
}
function openChapterPicker(mode="create"){
 if(mode!==chapterPickerMode)throw Error("選章器的編輯模式不一致");
 if(!parsedCatalog||!Array.isArray(parsedCatalog.chapter_titles)||parsedCatalog.chapter_titles.length!==parsedCatalog.total_chapters)
  throw Error("完整目錄尚未載入，請重新解析目錄");
 const start=Number(pickerField("start-chapter").value),end=Number(pickerField("end-chapter").value);
 chapterPickerExcluded=new Set(excludedNumbers(pickerField("excluded").value,start,end));
 const orderText=pickerField("chapter-order").value.trim();
 const wholeBook=Array.from({length:parsedCatalog.total_chapters},(_,i)=>i+1);
 const selectedOrder=orderText?readOrder(orderText,start,end,[...chapterPickerExcluded]):[];
 chapterPickerOrder=selectedOrder.concat(wholeBook.filter(i=>!selectedOrder.includes(i)));
 chapterSelectedRows.clear();chapterPage=0;
 $("chapter-show-all").checked=true;$("chapter-search").value="";renderChapterPicker();$("chapter-dialog").showModal();
}
function pickerBulk(mode){
 if(!chapterPickerExcluded)throw Error("尚未開啟選章視窗");
 const start=Number(pickerField("start-chapter").value),end=Number(pickerField("end-chapter").value);
 for(const i of pickerMatchingIndices()){
  if(i<start||i>end)continue;
  if(mode==="none")chapterPickerExcluded.add(i);
  else if(mode==="all")chapterPickerExcluded.delete(i);
  else if(chapterPickerExcluded.has(i))chapterPickerExcluded.delete(i);
  else chapterPickerExcluded.add(i);
 }
 renderChapterPicker();
}
function saveChapterPicker(){
 if(!chapterPickerExcluded)throw Error("尚未開啟選章視窗");
 pickerField("excluded").value=[...chapterPickerExcluded].sort((a,b)=>a-b).join(",");
 const start=Number(pickerField("start-chapter").value),end=Number(pickerField("end-chapter").value);
 const result=chapterPickerOrder.filter(i=>i>=start&&i<=end&&!chapterPickerExcluded.has(i));
 const expected=Array.from({length:end-start+1},(_,i)=>i+start).filter(i=>!chapterPickerExcluded.has(i));
 pickerField("chapter-order").value=result.every((n,i)=>n===expected[i])?"":result.join(",");
 pickerField("renumber").checked=true;
 if(chapterPickerMode==="edit"){
  for(const key of ["number","name","pair"])
   $("edit-duplicate-"+key).checked=$("duplicate-"+key).checked;
 }
 const message=chapterPickerMode==="edit"?
  "章節選擇已套用至目前小說編輯表單；按「儲存任務」才會提交 GitHub。":
  "章節選擇已套用到排除清單；加入佇列前仍可調整";
 closeChapterPicker();
 notice(message);
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
 bind("refresh-btn","click",refresh);bind("dispatch-btn","click",window.triggerDispatcher);bind("book-form","submit",window.submitBook);
 bind("batch-btn","click",window.batchAdd);bind("parse-btn","click",window.parseCatalog);
 bind("edit-form","submit",window.saveEditor);bind("review-form","submit",window.saveReview);
 bind("open-chapter-picker","click",()=>openChapterPicker("create"));
 bind("edit-load-catalog","click",window.loadEditCatalog);
 bind("chapter-search","input",()=>{chapterPage=0;renderChapterPicker();});
 bind("chapter-show-all","change",()=>{chapterPage=0;renderChapterPicker();});
 bind("chapter-prev","click",()=>{chapterPage--;renderChapterPicker();});
 bind("chapter-next","click",()=>{chapterPage++;renderChapterPicker();});
 bind("chapter-list","keydown",event=>{
  if((event.ctrlKey||event.metaKey)&&event.key==="ArrowUp"){
   event.preventDefault();moveSelectedChapters(-1);
  }else if((event.ctrlKey||event.metaKey)&&event.key==="ArrowDown"){
   event.preventDefault();moveSelectedChapters(1);
  }else if(event.key===" "&&event.target.closest(".chapter-item")){
   event.preventDefault();
   const row=event.target.closest(".chapter-item"),uuid=Number(row.dataset.uuid);
   if(chapterSelectedRows.has(uuid))chapterSelectedRows.delete(uuid);
   else chapterSelectedRows.add(uuid);
   renderChapterPicker();
  }
 });
 bind("chapter-move-up","click",()=>moveSelectedChapters(-1));
 bind("chapter-move-down","click",()=>moveSelectedChapters(1));
 bind("chapter-edit-cancel","click",()=>{$("chapter-edit-dialog").close();chapterEditingIndex=null;});
 bind("chapter-edit-save","click",saveChapterEdit);
 bind("chapter-duplicates-close","click",()=>$("chapter-duplicates-dialog").close());
 for(const id of ["duplicate-number","duplicate-name","duplicate-pair","duplicate-only"])bind(id,"change",()=>{
  if(!["duplicate-number","duplicate-name","duplicate-pair"].some(x=>$(x).checked)){
   $(id).checked=true;throw Error("至少需要一個重複判斷條件");
  }chapterPage=0;renderChapterPicker();
 });
 bind("chapter-all","click",()=>pickerBulk("all"));bind("chapter-none","click",()=>pickerBulk("none"));bind("chapter-invert","click",()=>pickerBulk("invert"));
 bind("chapter-picker-save","click",saveChapterPicker);
 bind("chapter-picker-cancel","click",closeChapterPicker);
 bind("chapter-exclude-duplicates","click",excludePickerDuplicates);
 bind("chapter-sort-number","click",()=>sortPickerOrder("number"));bind("chapter-sort-original","click",()=>sortPickerOrder("original"));
 for(const id of ["duplicate-number","duplicate-name","duplicate-pair","duplicate-only"])bind(id,"change",renderChapterPicker);
 bind("progress-close","click",()=>$("progress-dialog").close());
 bind("progress-refresh-now","click",()=>pollProgressNow(true));
 $("progress-dialog").addEventListener("close",()=>{
  progressGeneration++;progressTaskId=null;
  if(progressTimer!==null){clearTimeout(progressTimer);progressTimer=null;}
 });bind("edit-cancel","click",()=>{$("edit-dialog").close();editorTaskId=null;});bind("review-cancel","click",()=>{$("review-dialog").close();reviewTaskId=null;});
 bind("publish-immediate","change",renderPublish);bind("publish-scheduled","change",renderPublish);
 bind("publish-at","change",renderPublish);
 document.querySelectorAll("[data-publish-day]").forEach(b=>b.addEventListener("click",()=>setPublishPreset(Number(b.dataset.publishDay),b.dataset.publishHour)));
 renderPublish();
 // Desktop GUI periodically synchronizes the queue without a manual click.
 // Use the public state JSON instead of unauthenticated REST API calls every 45s.
 setInterval(()=>{void syncOverviewQueue();},45000);
 document.addEventListener("visibilitychange",()=>{
  if(!document.hidden)void syncOverviewQueue();
 });
 bind("queue-all","change",()=>{const on=$("queue-all").checked;selectedPending=new Set(on?queueTasks().map(t=>t.task_id):[]);renderQueue();});
 bind("rank-search","input",renderRanking);bind("rank-category","change",renderRanking);
 bind("rank-select-all","click",()=>{for(const n of filteredRanking())rankingSelected.add(n.catalog_url);renderRanking();});
 bind("rank-select-none","click",()=>{rankingSelected.clear();renderRanking();});
 bind("rank-copy","click",copyRankingUrls);
 loadRanking();
 bind("catalog-url","input",()=>{parsedCatalog=null;$("catalog-preview").classList.add("hidden");$("chapter-selection-tools").classList.add("hidden");});
 refresh();
});
