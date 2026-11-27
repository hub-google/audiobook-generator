"use strict";
/* Desktop GUI parity: in-page operations timeline from public task state.
 * Never copy raw private logs to public GitHub Pages or expose cloud credentials.
 */
(function(){
 const id=name=>document.getElementById(name);
 const lines=[],tasks=new Map(),runs=new Map(),jobSteps=new Map();
 let paused=false,lastJobCheck=0,loading=false,lastSeenRevision=-1;
 let liveTaskId=null,liveCursor=0,liveBusy=false,lastLiveError="";
 const fmt=()=>new Date().toLocaleTimeString("zh-TW",{timeZone:"Asia/Taipei",hour12:false});
 function append(message){
  const entry="["+fmt()+"] "+String(message).replace(/\s+/g," ").slice(0,420);
  if(lines.at(-1)===entry)return;
  lines.push(entry);
  if(lines.length>550)lines.splice(0,lines.length-550);
  draw();
 }
 function draw(){
  const box=id("cloud-log-console");if(!box)return;
  const nearBottom=box.scrollTop+box.clientHeight>=box.scrollHeight-30;
  box.textContent=lines.slice(-500).join("\n")||"尚未取得雲端紀錄";
  if(nearBottom)box.scrollTop=box.scrollHeight;
  id("cloud-log-status").textContent=(paused?"已暫停｜":"自動更新中｜")+
   "共 "+lines.length+" 筆｜每 12 秒檢查頁面資料／每 2 分鐘檢查工作步驟";
 }
 function taskState(t){
  const stage=t.stages||{},hf=t.hf_progress||{},yt=t.youtube_progress||{};
  return [
   t.status||"pending",t.workflow_phase||"queued",
   stage.scrape?.status||"pending",stage.cover?.status||"pending",
   stage.processing?.status||"pending",
   (hf.completed||0)+"/"+(hf.total||0),
   (yt.completed||0)+"/"+(yt.total||0),
   t.scrape_run_id||"",t.cover_run_id||"",t.processing_run_id||t.run_id||""
  ].join("|");
 }
 function observeQueue(){
  if(paused||!queue||!Array.isArray(queue.queue))return;
  const revision=Number(queue.revision||0);
  if(revision===lastSeenRevision&&tasks.size)return;
  lastSeenRevision=revision;
  const items=[...(queue.queue||[]),...(queue.completed||[])];
  const live=new Set();
  for(const t of items){
   const id=String(t.task_id||"");if(!id)continue;live.add(id);
   const val=taskState(t),prior=tasks.get(id);
   if(prior!==val){
    tasks.set(id,val);
    const hf=t.hf_progress||{},yt=t.youtube_progress||{};
    append("《"+(t.book_title||"待解析")+"》 "+(prior?"進度更新":"載入")+
      "｜"+statusName(t.status)+"｜TXT "+(t.stages?.scrape?.status||"pending")+
      "｜封面 "+(t.stages?.cover?.status||"pending")+
      "｜影片 "+(t.stages?.processing?.status||"pending")+
      "｜HF "+(hf.completed||0)+"/"+(hf.total||0)+
      "｜YouTube "+(yt.completed||0)+"/"+(yt.total||0));
   }
  }
  for(const key of [...tasks.keys()])if(!live.has(key)){
   tasks.delete(key);append("任務 "+key+" 已離開佇列");
  }
 }
 function observeRuns(){
  if(paused||!Array.isArray(latestRuns))return;
  for(const run of latestRuns.slice(0,20)){
   const key=String(run.id),val=String(run.status)+"|"+String(run.conclusion||"");
   if(runs.get(key)===val)continue;
   runs.set(key,val);
   append("GitHub Run #"+key+"｜"+(run.display_title||run.name||"雲端工作").slice(0,90)+
    "｜"+(run.conclusion||run.status));
  }
 }
 async function observeJobs(){
  if(paused||loading||document.hidden||Date.now()-lastJobCheck<120000)return;
  const active=[...(queue?.queue||[])].filter(t=>
   ["preparing_assets","running","processing","dispatching","waiting_review","canceling"].includes(t.status));
  const t=active[0];if(!t)return;
  const run=t.processing_run_id||t.run_id||t.cover_run_id||t.scrape_run_id;
  if(!run)return;
  lastJobCheck=Date.now();loading=true;
  try{
   const value=await request("/actions/runs/"+encodeURIComponent(run)+"/jobs?per_page=100");
   for(const job of value.jobs||[]){
    const key=run+"-"+job.id;
    const previous=jobSteps.get(key)||"";
    const info=(job.steps||[]).map(x=>x.name+":"+(x.conclusion||x.status)).join("|");
    if(info===previous)continue;
    jobSteps.set(key,info);
    const changes=(job.steps||[]).filter(x=>
     !previous.includes(x.name+":"+(x.conclusion||x.status))).slice(-8);
    for(const step of changes)append("Run #"+run+"｜"+(job.name||"Worker")+
      "｜"+(step.name||"步驟")+"："+(step.conclusion||step.status||"pending"));
   }
  }catch(e){
   append("雲端工作步驟讀取暫時失敗："+(e.status||e.name||"NetworkError"));
  }finally{loading=false;}
 }
 async function pollLiveEvents(){
  if(paused||liveBusy||document.hidden||!window.audioAuth?.loggedIn||!queue)return;
  if(!id("runs")?.classList.contains("active"))return;
  const task=[...(queue.queue||[]),...(queue.completed||[])].find(t=>
   ["preparing_assets","processing","running","waiting_review","needs_attention"].includes(t.status))||
   (queue.queue||[])[0];
  if(!task)return;
  const current=String(task.task_id||"");
  if(current!==liveTaskId){liveTaskId=current;liveCursor=0;append("即時 Worker 日誌："+(task.book_title||current));}
  liveBusy=true;
  try{
   const bearer=await window.audioAuth.token();
   const url="https://unapiwajmgqqtdkixaab.supabase.co/functions/v1/audio-web-commands/live_events"+
    "?task_id="+encodeURIComponent(current)+"&after="+encodeURIComponent(liveCursor);
   const response=await fetch(url,{headers:{"Authorization":"Bearer "+bearer},cache:"no-store"});
   if(!response.ok)throw Error("HTTP "+response.status);
   const result=await response.json();
   if(liveTaskId!==current)return;
   for(const event of result.events||[]){
    if(Number(event.id)<=liveCursor)continue;
    liveCursor=Number(event.id);
    const moment=new Date(event.created_at).toLocaleTimeString("zh-TW",{
     timeZone:"Asia/Taipei",hour12:false});
    append("Worker "+String(event.job_name||"")+
     "｜"+moment+"｜"+String(event.level||"info")+"｜"+String(event.message||""));
   }
   lastLiveError="";
  }catch(error){
   if(lastLiveError!==error.message){
    lastLiveError=error.message;
    append("即時 Worker 日誌暫時無法同步："+error.message);
   }
  }finally{liveBusy=false;}
 }
 function tick(){if(paused)return;observeQueue();observeRuns();void observeJobs();void pollLiveEvents();}
 async function synchronize(){
  if(paused)paused=false;
  append("手動更新雲端佇列與 Actions 紀錄");
  try{await refresh()}catch(e){append("手動更新失敗："+e.message)}
  tick();draw();
 }
 document.addEventListener("DOMContentLoaded",()=>{
  id("cloud-log-refresh").addEventListener("click",()=>void synchronize());
  id("cloud-log-pause").addEventListener("click",()=>{
   paused=!paused;
   id("cloud-log-pause").textContent=paused?"恢復自動更新":"暫停自動更新";
   append(paused?"使用者暫停站內日誌":"使用者恢復站內日誌");
   draw();
  });
  id("cloud-log-clear").addEventListener("click",()=>{
   lines.length=0;draw();
  });
  id("cloud-log-copy").addEventListener("click",async()=>{
   try{await navigator.clipboard.writeText(lines.join("\n"));notice("已複製 "+lines.length+" 筆站內執行日誌");}
   catch(e){notice("無法複製日誌："+e.message,"error");}
  });
  append("AUDIO 雲端日誌就緒；原始 Run Logs 只會透過受保護的站內操作取得");
  setTimeout(tick,1200);
  setInterval(tick,12000);
  setInterval(()=>{void pollLiveEvents();},5000);
  document.addEventListener("visibilitychange",()=>{if(!document.hidden)tick();});
 });
})();
