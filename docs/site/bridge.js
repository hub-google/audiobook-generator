"use strict";
// GitHub Pages: read-only frontend; GitHub authenticates all writes on github.com.
// No PAT, OAuth access token, HF/Gemini secret or YouTube credential is in this file.
(function(){
 const ISSUES="https://github.com/hub-google/audiobook-generator/issues/new";
 const QUEUE_CHANGING=new Set(["enqueue","batch_add","edit","move","status","delete",
   "complete","restore","requeue","cancel","review"]);
 let pendingWatcher=0;
 function watchQueueUpdate(){
  const initialRevision=Number(queue?.revision||0),watcher=++pendingWatcher;
  const expires=Date.now()+5*60*1000;
  let checks=0,timer=null,checking=false;
  async function check(){
   if(watcher!==pendingWatcher||Date.now()>expires||checks>=10)return;
   if(document.hidden){timer=setTimeout(check,10000);return;}
   if(checking)return;
   checking=true;checks++;
   try{
    const latest=await readQueue();
    if(Number(latest.data?.revision||0)>initialRevision){
     queue=latest.data;queueSha=latest.sha;renderQueue();
     notice("GitHub 雲端佇列已更新，請確認操作結果。");
     return;
    }
   }catch{ /* Network/API errors do not count as operation success. */ }
   finally{checking=false;}
   if(watcher===pendingWatcher&&checks<10)timer=setTimeout(check,10000);
  }
  timer=setTimeout(check,9000);
  window.addEventListener("focus",()=>{if(watcher===pendingWatcher){
   clearTimeout(timer);void check();
  }},{once:true});
 }
 function issueUrl(payload,label){
  const body="<!-- AUDIO-COMMAND-V1:"+payload+":AUDIO-COMMAND-V1 -->\n\n"+
    "## AUDIO 操作確認\n\n"+label+"\n\n請登入 GitHub 並送出 Issue。"+
    "Actions 會再次檢查發起者的 Repo 寫入權限；未授權的請求不會執行。";
  return ISSUES+"?"+new URLSearchParams({title:"[AUDIO] "+label,body}).toString();
 }
 function successNotice(op){
  notice("已開啟 GitHub 確認頁，請按「Submit new issue」；回到 AUDIO 後會自動查詢佇列更新。");
  if(QUEUE_CHANGING.has(op))watchQueueUpdate();
 }
 function base64Url(bytes){
  let binary="";
  for(let offset=0;offset<bytes.length;offset+=8192)
   binary+=String.fromCharCode(...bytes.slice(offset,offset+8192));
  return btoa(binary).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,"");
 }
 function send(op,data,label){
  const body=JSON.stringify({version:1,op,data});
  const url=issueUrl(toB64(body).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,""),label);
  if(url.length<=25000){
   window.open(url,"_blank","noopener");successNotice(op);
   return url;
  }
  // GitHub already accepts gzip commands. Compress large chapter plans before
  // URL encoding; keep the new tab opened in the original click gesture.
  if(!("CompressionStream" in window))
   throw Error("這個瀏覽器不支援大型章節傳送，請使用最新版 Chrome 或 Edge");
  const tab=window.open("about:blank","_blank");
  if(!tab)throw Error("新分頁遭到瀏覽器封鎖，請允許 AUDIO 開啟 GitHub 分頁");
  tab.opener=null;
  notice("章節設定較長，正在壓縮以符合 GitHub 網址長度限制…");
  return (async()=>{
   const input=new TextEncoder().encode(body);
   if(input.length>300000)throw Error("章節設定資料超過 GitHub 操作內容限制");
   const gz=await new Response(new Blob([input]).stream()
      .pipeThrough(new CompressionStream("gzip"))).arrayBuffer();
   const compressedUrl=issueUrl(base64Url(new Uint8Array(gz)),label);
   if(compressedUrl.length>25000)
    throw Error("壓縮後仍超過 GitHub 網址限制；請減少自訂章節內容");
   tab.location.replace(compressedUrl);
   successNotice(op);
   return compressedUrl;
  })().catch(error=>{
   try{tab.close();}catch{}
   notice("無法送出 GitHub 操作："+error.message,"error");
   return null;
  });
 }
 window.audioIssueAction=send;
 window.exportArtifacts=async function(taskId){
  if(!taskId)throw Error("請先選擇小說");
  send("export_artifacts",{task_ids:[taskId]},"整包下載小說成品");
 };

 function selectedActive(){
  const books=selectedPendingTasks();
  if(!books.length)throw Error("請先勾選小說");
  return books;
 }
 function selectedPermitted(statuses){
  const books=selectedActive();
  if(books.some(t=>!statuses.includes(t.status)))throw Error("選取的小說含有不能執行此操作的狀態");
  return books.map(t=>t.task_id);
 }
 function multi(op,data,label){send(op,data,label);}

 window.triggerDispatcher=async function(){send("dispatch",{},"執行調度檢查");};
 window.moveTask=async function(id,delta){send("move",{task_ids:[id],delta},"調整任務順序");};
 window.setTaskState=async function(id,status){
  if(!["queued","paused"].includes(status))throw Error("不允許的工作狀態");
  send("status",{task_ids:[id],status},status==="paused"?"暫停小說":"恢復小說");
 };
 window.requeueTask=async function(id){
  if(confirm("將此任務依桌面 GUI 邏輯重設執行世代並重新排程？"))
   send("requeue",{task_id:id},"重新排程小說");
 };
 window.cancelRunningTask=async function(id){
  const t=queueTasks().find(t=>t.task_id===id);
  if(!t||!live(t))throw Error("目前沒有正在執行的 Run");
  if(confirm("要取消這本小說的本次 Run 嗎？"))
   send("cancel",{task_id:id},"取消製作 Run");
 };
 window.markCompleted=async function(id){
  if(confirm("標記已完成只修改佇列，不代表影片已上傳，是否繼續？"))
   send("complete",{task_ids:[id]},"標記小說已完成");
 };
 window.restoreCompleted=async function(id){
  if(confirm("移回待製作佇列並維持暫停狀態？"))
   send("restore",{task_ids:[id]},"移回待製作佇列");
 };
 window.deleteTask=async function(id){
  if(confirm("確定刪除這本未執行的小說嗎？"))
   send("delete",{task_ids:[id]},"刪除小說任務");
 };
 window.approveTask=async function(id,patterns){
  const t=queueTasks().find(t=>t.task_id===id);
  if(!t||t.status!=="waiting_review"||!t.scrape_run_id||!t.cover_run_id||
     !["scrape","cover"].every(s=>t.stages?.[s]?.status==="completed"))
   throw Error("TXT／封面尚未通過前置檢查，無法核准後製");
  const evidence=window.audioReviewEvidence?.();
  if(!evidence)throw Error("請先匯入這本小說真正的 TXT 審核報告");
  return await send("review",{task_id:id,remove_patterns:patterns,
   candidate_overrides:window.audioReviewCandidateEdits?.()||[],evidence},
   "核准文字與封面，開始後製");
 };
 window.saveReview=async function(e){
  e.preventDefault();if(!reviewTaskId)throw Error("請先選擇小說");
  const patterns=[...new Set($("review-patterns").value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean))];
  if(patterns.some(s=>s.length>10000))throw Error("清理字詞單項過長");
  if(!confirm("是否確認已檢查 TXT、封面、文字清理以及相關授權？"))return;
  if(!await window.approveTask(reviewTaskId,patterns))return;
  $("review-dialog").close();reviewTaskId=null;
 };
 window.saveEditor=async function(e){
  e.preventDefault();const id=editorTaskId,t=queueTasks().find(t=>t.task_id===id);
  if(!t||!canEditTask(t))throw Error("此任務正在執行或已經變更，無法編輯");
  const start=Number($("edit-start").value),end=Number($("edit-end").value);
  if(!Number.isSafeInteger(start)||!Number.isSafeInteger(end)||start<1||end<start)throw Error("章節範圍無效");
  const excluded=excludedNumbers($("edit-excluded").value,start,end);
  const changes={
   book_title:$("edit-name").value.trim(),start_chapter:start,end_chapter:end,
   excluded_chapters:excluded,renumber_selected:$("edit-renumber").checked,
   publish_at:publishValue($("edit-publish").value),
   chapter_title_overrides:readOverrides($("edit-titles").value,start,end),
   chapter_normalized_number_overrides:readOverrides($("edit-numbers").value,start,end,true),
   chapter_order:readOrder($("edit-order").value,start,end,excluded),
   duplicate_detection:{
     use_normalized_number:$("edit-duplicate-number").checked,
     use_chapter_name:$("edit-duplicate-name").checked,
     use_number_and_name:$("edit-duplicate-pair").checked
   }
  };
  if(!Object.values(changes.duplicate_detection).some(Boolean))throw Error("至少需選一種重複判斷條件");
  if(!changes.book_title)throw Error("書名不可空白");
  if(canEditAfterCancel(t)&&!confirm("這本小說正在製作。儲存會取消舊 Run，再依新章節設定重新排程，是否繼續？"))return;
  const opened=await send("edit",{task_id:id,changes},
   canEditAfterCancel(t)?"修改章節並重新排程":"編輯小說章節");
  if(!opened)return;
  $("edit-dialog").close();editorTaskId=null;
 };
 window.addTasks=async function(tasks){
  if(!tasks.length)throw Error("沒有可以新增的小說");
  const books=tasks.map(t=>({
   catalog_url:t.catalog_url,book_title:t.book_title,
   start_chapter:t.start_chapter,end_chapter:t.end_chapter,
   excluded_chapters:t.excluded_chapters||[],renumber_selected:t.renumber_selected,
   chapter_title_overrides:t.chapter_title_overrides||{},
   chapter_normalized_number_overrides:t.chapter_normalized_number_overrides||{},
   chapter_order:t.chapter_order||[],publish_at:t.publish_at||null,
   duplicate_detection:t.duplicate_detection||null
  }));
  return await send(books.length===1?"enqueue":"batch_add",{books},
   books.length===1?"新增小說":"批次新增 "+books.length+" 本小說");
 };
 window.submitBook=async function(e){
  e.preventDefault();const url=$("catalog-url").value.trim(),src=getSource(url);
  if($("source").value!==src.id)throw Error("來源網站與網址不一致");
  const start=Number($("start-chapter").value),end=Number($("end-chapter").value);
  if(parsedCatalog&&parsedCatalog.catalog_url===url&&end>parsedCatalog.total_chapters)
   throw Error("結束章節超過實際目錄");
  const excluded=excludedNumbers($("excluded").value,start,end);
  const task=buildTask({
   url,title:$("book-title").value,start,end,excluded,
   renumber:$("renumber").checked,
   publishAt:$("publish-scheduled").checked?publishValue($("publish-at").value):null,
   titles:readOverrides($("title-overrides").value,start,end),
   numbers:readOverrides($("number-overrides").value,start,end,true),
   order:readOrder($("chapter-order").value,start,end,excluded)
  });
  task.duplicate_detection={use_normalized_number:$("duplicate-number").checked,
   use_chapter_name:$("duplicate-name").checked,use_number_and_name:$("duplicate-pair").checked};
  if(!Object.values(task.duplicate_detection).some(Boolean))throw Error("至少需選一種重複判斷條件");
  if($("publish-scheduled").checked&&!task.publish_at)throw Error("排定公開時間尚未設定");
  await window.addTasks([task]);
 };
 window.batchAdd=async function(){
  const lines=$("batch-urls").value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean);
  if(!lines.length)throw Error("請貼上小說網址");
  const books=lines.map(line=>{
   const p=line.split("|").map(s=>s.trim()),url=p.length>1?p[1]:p[0];
   getSource(url);
   const start=p[2]?Number(p[2]):1,end=p[3]?Number(p[3]):null;
   if(!Number.isSafeInteger(start)||start<1||
      (end!==null&&(!Number.isSafeInteger(end)||end<start)))throw Error("章節範圍無效："+line);
   return {catalog_url:url,book_title:p.length>1?p[0]:"",start_chapter:start,end_chapter:end,
     excluded_chapters:[],renumber_selected:true};
  });
  return await send("batch_add",{books},"批次解析並加入 "+books.length+" 本小說");
 };
 window.addRankingToQueue=async function(){
  const chosen=chosenRanking();if(!chosen.length)throw Error("請先選取小說");
  return await send("batch_add",{books:chosen.map(n=>({
   catalog_url:n.catalog_url,book_title:n.title,start_chapter:1,end_chapter:null,
   excluded_chapters:[],renumber_selected:true
  }))},"從完本排行新增 "+chosen.length+" 本小說");
 };
 function applyCatalog(data,url){
  if(data.requested_url!==url||$("catalog-url").value.trim()!==url)throw Error("目錄結果與目前請求網址不同，請重新解析");
  if(!Number.isSafeInteger(data.total_chapters)||data.total_chapters<1||
     data.chapter_titles?.length!==data.total_chapters||data.chapter_parts?.length!==data.total_chapters)
   throw Error("目錄章節不完整，請重新解析");
  parsedCatalog=data;$("catalog-url").value=data.catalog_url;$("book-title").value=data.book_title;
  $("source").value=getSource(data.catalog_url).id;
  // Match desktop GUI: select the complete book and reset previous book state.
  $("start-chapter").value=1;$("end-chapter").value=data.total_chapters;
  for(const id of ["excluded","title-overrides","number-overrides","chapter-order"])$(id).value="";
  const defaults=data.profile_defaults||{};
  $("title-overrides").value=formatOverrides(defaults.chapter_title_overrides||{});
  $("number-overrides").value=formatOverrides(defaults.chapter_normalized_number_overrides||{});
  $("renumber").checked=true;
  const detection=defaults.duplicate_detection||{};
  $("duplicate-number").checked=detection.use_normalized_number!==false;
  $("duplicate-name").checked=detection.use_chapter_name!==false;
  $("duplicate-pair").checked=!!detection.use_number_and_name;
  const box=$("catalog-preview");box.replaceChildren();
  const title=document.createElement("strong");title.textContent=data.book_title+" · "+data.total_chapters+" 章";box.append(title);
  const ul=document.createElement("ul");
  (data.chapter_titles||[]).slice(0,40).forEach((s,i)=>{const li=document.createElement("li");li.textContent=(i+1)+". "+s;ul.append(li)});
  box.append(ul);box.classList.remove("hidden");
  $("chapter-selection-tools").classList.remove("hidden");
  text($("parse-status"),"完整目錄解析成功，共 "+data.total_chapters+" 章");
 }
 window.loadEditCatalog=async function(){
  const task=queueTasks().find(t=>t.task_id===editorTaskId);
  if(!task||!$("edit-dialog").open)throw Error("請先選擇要編輯的小說");
  if(editorParsedCatalog){openEditorChapterPicker();return;}
  const url=task.catalog_url,id=crypto.randomUUID().slice(0,8);
  if(!await send("preview",{catalog_url:url,request_id:id},
      "載入編輯目錄 "+id))return;
  const button=$("edit-load-catalog");
  button.disabled=true;
  text($("edit-catalog-status"),"請在 GitHub 送出 Issue，正在等待此書目錄…");
  try{
   for(let i=0;i<72;i++){
    if(editorTaskId!==task.task_id||!$("edit-dialog").open)return;
    await new Promise(resolve=>setTimeout(resolve,5000));
    if(editorTaskId!==task.task_id||!$("edit-dialog").open)return;
    const link="https://raw.githubusercontent.com/"+REPO+"/"+QUEUE_BRANCH+
       "/web-previews/"+id+".json?_="+Date.now();
    let response;
    try{response=await fetch(link,{cache:"no-store"});}
    catch{continue;}
    if(response.status===404)continue;
    if(!response.ok)throw Error("讀取完整目錄失敗（HTTP "+response.status+"）");
    setEditorCatalog(await response.json(),url);
    openEditorChapterPicker();
    return;
   }
   throw Error("尚未取得目錄結果，請確認 GitHub Issue 和 Actions 均成功");
  }finally{button.disabled=false;}
 };
 window.parseCatalog=async function(){
  const url=$("catalog-url").value.trim();getSource(url);
  const id=crypto.randomUUID().slice(0,8);
  if(!await send("preview",{catalog_url:url,request_id:id},
   "解析完整章節目錄 "+id))return;
  text($("parse-status"),"請在 GitHub 頁面送出 Issue；本頁會嘗試自動讀取解析結果。");
  const btn=$("parse-btn");btn.disabled=true;
  try{
   for(let i=0;i<72;i++){
    await new Promise(resolve=>setTimeout(resolve,5000));
    const link="https://raw.githubusercontent.com/"+REPO+"/"+QUEUE_BRANCH+
       "/web-previews/"+id+".json?_= "+Date.now();
    const response=await fetch(link.replace("?_= ","?_="),{cache:"no-store"});
    if(!response.ok)continue;
    applyCatalog(await response.json(),url);return;
   }
   throw Error("尚未取得解析結果，請確認 GitHub Issue 已送出與 Actions 已成功");
  }finally{btn.disabled=false;}
 };
 document.addEventListener("DOMContentLoaded",()=>{
  const cmds=[
   ["batch-up",()=>multi("move",{task_ids:selectedPermitted(["queued","paused","interrupted","stopped"]),delta:-1},"批次上移")],
   ["batch-down",()=>multi("move",{task_ids:selectedPermitted(["queued","paused","interrupted","stopped"]),delta:1},"批次下移")],
   ["batch-pause",()=>multi("status",{task_ids:selectedPermitted(["queued","paused"]),status:"paused"},"批次暫停")],
   ["batch-resume",()=>multi("status",{task_ids:selectedPermitted(["queued","paused"]),status:"queued"},"批次恢復")],
   ["batch-complete",()=>{if(confirm("確定將選取小說標為完成（不代表已上傳）？"))
    multi("complete",{task_ids:selectedPermitted(["queued","paused","interrupted","stopped"])},"批次標記完成");}],
   ["batch-delete",()=>{if(confirm("確定刪除所有選取的待製作小說？"))
    multi("delete",{task_ids:selectedPermitted(["queued","paused","interrupted","stopped"])},"批次刪除小說");}],
   ["batch-export",()=>{
      const ids=selectedActive().map(t=>t.task_id);
      multi("export_artifacts",{task_ids:ids},"批次匯出 "+ids.length+" 本小說成品");
   }],
   ["batch-export-completed",()=>{
      if(!selectedCompleted.size)throw Error("請先選擇已完成小說");
      multi("export_artifacts",{task_ids:[...selectedCompleted]},"批次匯出已完成作品");
   }],
   ["batch-restore",()=>{if(!selectedCompleted.size)throw Error("請選擇已完成小說");
    multi("restore",{task_ids:[...selectedCompleted]},"批次移回未完成");}],
  ];
  for(const [id,fn]of cmds)$(id).addEventListener("click",()=>Promise.resolve().then(fn).catch(e=>notice(e.message,"error")));

  const add=$("rank-add");if(add)add.addEventListener("click",()=>window.addRankingToQueue().catch(e=>notice(e.message,"error")));
 });
})();
