"use strict";
(function(){
 let report=null, taskId=null, coverUrl=null, bookTitle="",coverEvidence=null, mode="combined",sortKey="",sortDescending=false,lastSelected=null;
 const selectedCandidates=new Set();
 const state={candidateChoices:new Map(),chapters:{},prompt:""};
 function editedCandidates(){
  const list=(report?.candidates||[]).filter(c=>typeof c?.text==="string").map(c=>({
   ...c, original_text:c.text,
   text:state.candidateChoices.has(c.text)?state.candidateChoices.get(c.text):c.text
  })).filter(c=>c.text!==null);
  const known=new Set(list.map(c=>c.text));for(const rule of patterns()){if(!known.has(rule))list.push({text:rule,original_text:"manual:"+rule,kind:"已儲存規則",count:0,score:0,reason:"人工新增",samples:[],manual:true});}return list;
 }
 window.audioReviewCandidateEdits=function(){
  return [...state.candidateChoices.entries()].map(([original_text,edited_text])=>({
   original_text,edited_text
  }));
 };
 function rawEntries(buffer){
  const bytes=new Uint8Array(buffer),dv=new DataView(buffer);
  let eocd=-1;
  for(let i=bytes.length-22;i>=Math.max(0,bytes.length-66000);i--){if(dv.getUint32(i,true)===0x06054b50){eocd=i;break;}}
  if(eocd<0)throw Error("ZIP 格式無效");
  let off=dv.getUint32(eocd+16,true),count=dv.getUint16(eocd+10,true),out=[],totalSize=0;
  if(count>5000)throw Error("ZIP 項目數過多");
  for(let i=0;i<count;i++){
   if(dv.getUint32(off,true)!==0x02014b50)throw Error("ZIP 目錄損毀");
   const method=dv.getUint16(off+10,true),size=dv.getUint32(off+20,true),
   uncompressed=dv.getUint32(off+24,true),len=dv.getUint16(off+28,true),
   extra=dv.getUint16(off+30,true),comment=dv.getUint16(off+32,true),local=dv.getUint32(off+42,true);
   const name=new TextDecoder().decode(bytes.slice(off+46,off+46+len));off+=46+len+extra+comment;
   if(size>50_000_000||uncompressed>50_000_000||size===0xffffffff||
      uncompressed===0xffffffff)throw Error("ZIP 單一檔案過大");
   if(!/(?:ad-candidates-.*\.json|master_cover_prompt\.json|master_cover\.jpg|cover-preflight-manifest\.json)$/.test(name))continue;
   totalSize+=uncompressed;
   if(totalSize>80_000_000)throw Error("ZIP 審核資料解壓後總量過大");
   if(dv.getUint32(local,true)!==0x04034b50)throw Error("ZIP 檔案項目錯誤");
   const start=local+30+dv.getUint16(local+26,true)+dv.getUint16(local+28,true);
   if(start+size>bytes.length)throw Error("ZIP 檔案資料不完整");
   out.push({name,method,bytes:bytes.slice(start,start+size)});
  }
  return out;
 }
 async function decodeEntry(entry){
  if(entry.method===0)return entry.bytes;
  if(entry.method!==8||!self.DecompressionStream)throw Error("目前瀏覽器無法解開 ZIP 壓縮");
  const stream=new Blob([entry.bytes]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
 }
 function patterns(){return [...new Set($("review-patterns").value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean))];}
 function setPatterns(arr){$("review-patterns").value=[...new Set(arr)].join("\n");renderCandidates();renderText();}
 function candidatesVisible(){
  const q=$("review-search").value.trim().toLowerCase(),kind=$("review-kind").value,status=$("review-state").value;
  const rows=editedCandidates().filter(c=>{
   const removed=patterns().includes(c.text);
   return (!q||String(c.text+" "+c.reason).toLowerCase().includes(q))&&
     (!kind||kind===c.kind)&&(!status||(status==="remove"?removed:!removed));
  });
  if(sortKey){const value=c=>sortKey==="decision"?Number(patterns().includes(c.text)):sortKey==="chapters"?(c.affected_chapters||[]).map(n=>String(n).padStart(8,"0")).join(","):c[sortKey]??"";rows.sort((a,b)=>{const x=value(a),y=value(b);return (sortDescending?-1:1)*(typeof x==="number"&&typeof y==="number"?x-y:String(x).localeCompare(String(y),"zh-Hant",{numeric:true}));});}return rows;
 }
 function selectedRows(){return editedCandidates().filter(c=>selectedCandidates.has(c.original_text));}
 function showContext(){const c=selectedRows()[0];text($("gui-review-context"),c?"判定原因："+(c.reason||"—")+"\n\n影響章節："+(c.affected_chapters||[]).join(", ")+"\n\n"+(c.samples||[]).join("\n\n---\n\n"):"請選取候選詞句。");}
 function modifySelected(action,rows=selectedRows()){
  if(!rows.length){notice("請先選取候選詞句","error");return;}
  const names=new Set(patterns());
  for(const c of rows){if(action==="remove")names.add(c.text);else if(action==="keep")names.delete(c.text);else if(action==="invert"){if(names.has(c.text))names.delete(c.text);else names.add(c.text);}else if(action==="delete"){if(!c.manual)state.candidateChoices.set(c.original_text,null);names.delete(c.text);selectedCandidates.delete(c.original_text);}}
  setPatterns([...names]);
 }
 function editCandidate(c){if(!c)return;const next=prompt("要比對並刪除的文字：",c.text);if(next===null)return;const trimmed=next.trim();if(!trimmed||trimmed.length>10000){notice("候選文字不可空白或超過 10000 字","error");return;}const names=new Set(patterns()),wasRemoved=names.delete(c.text);if(wasRemoved)names.add(trimmed);if(!c.manual){if(trimmed===c.original_text)state.candidateChoices.delete(c.original_text);else state.candidateChoices.set(c.original_text,trimmed);}setPatterns([...names]);}
 function renderCandidates(){
  const target=$("review-candidates");target.replaceChildren();
  const visible=candidatesVisible(),all=editedCandidates(),removed=all.filter(c=>patterns().includes(c.text)).length;
  text($("gui-review-count"),"顯示："+visible.length+"/"+all.length+" 項｜去除："+removed+" 項｜保留："+(all.length-removed)+" 項");
  if(!visible.length){target.textContent=report?"目前沒有符合條件的候選詞":"等待載入本書 TXT 審核資料";showContext();return;}
  const table=document.createElement("table"),head=table.createTHead().insertRow(),body=table.createTBody();
  for(const [key,label] of [["decision","處理狀態"],["kind","類型"],["text","疑似廣告詞句／重複行"],["count","次數"],["chapters","影響章節"],["score","可疑度"],["reason","判定理由"]]){
   const th=document.createElement("th");th.scope="col";th.textContent=label+(sortKey===key?(sortDescending?" ▼":" ▲"):"");th.tabIndex=0;const sort=()=>{if(sortKey===key)sortDescending=!sortDescending;else{sortKey=key;sortDescending=false;}renderCandidates();};th.addEventListener("click",sort);th.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();sort();}});head.append(th);
  }
  visible.forEach((c,index)=>{const tr=body.insertRow();tr.classList.toggle("selected",selectedCandidates.has(c.original_text));tr.dataset.candidateId=c.original_text;tr.tabIndex=0;const status=tr.insertCell(),checkbox=document.createElement("input");checkbox.type="checkbox";checkbox.checked=patterns().includes(c.text);checkbox.setAttribute("aria-label","去除 "+c.text);checkbox.addEventListener("change",()=>{selectedCandidates.clear();selectedCandidates.add(c.original_text);modifySelected(checkbox.checked?"remove":"keep",[c]);});status.append(checkbox,document.createTextNode(checkbox.checked?" 去除":" 保留"));
   for(const value of [c.kind,c.text,c.count,(c.affected_chapters||[]).join(", "),c.score,c.reason])tr.insertCell().textContent=value??"—";
   tr.addEventListener("click",e=>{if(e.target.closest("input"))return;if(e.shiftKey&&lastSelected!==null){const lo=Math.min(lastSelected,index),hi=Math.max(lastSelected,index);if(!e.ctrlKey&&!e.metaKey)selectedCandidates.clear();visible.slice(lo,hi+1).forEach(x=>selectedCandidates.add(x.original_text));}else if(e.ctrlKey||e.metaKey){if(selectedCandidates.has(c.original_text))selectedCandidates.delete(c.original_text);else selectedCandidates.add(c.original_text);}else{selectedCandidates.clear();selectedCandidates.add(c.original_text);}lastSelected=index;for(const row of body.rows)row.classList.toggle("selected",selectedCandidates.has(row.dataset.candidateId));showContext();target.focus();});
   tr.addEventListener("dblclick",e=>{if(e.target.closest("input"))return;if(e.target.cellIndex===2)editCandidate(c);else modifySelected("invert",[c]);});
  });target.append(table);showContext();
 }
 function chapterKeys(){return Object.keys(state.chapters).sort((a,b)=>Number(a)-Number(b));}
 function renderText(){
  const index=$("review-chapter").value,raw=state.chapters[index]||"";
  text($("review-raw"),raw);
  const lineBreak=raw.indexOf("\n");
  const title=lineBreak>=0?raw.slice(0,lineBreak):raw,body=lineBreak>=0?raw.slice(lineBreak+1):"";
  let clean=AudioCleaner.chunkText(AudioCleaner.cleanTextContent(body,title,bookTitle,patterns()));
  text($("review-clean"),clean);
  const original=raw.replace(/\s/g,"").length,after=clean.replace(/\s/g,"").length;
  const ratio=original?((original-after)/original*100):0;
  const warning=ratio>=50?"；⚠ 清除比例偏高":!after&&original?"；⚠ Clean 為空":"";
  text($("review-text-stats"),index?
   "第 "+index+" 章｜Raw "+original.toLocaleString()+" 字｜Python 同規則清理 "+after.toLocaleString()+" 字｜約清除 "+ratio.toFixed(1)+"%"+warning:
   "尚未載入章節文字");
 }
 function navigate(pos){
  const keys=chapterKeys();if(!keys.length)return;
  $("review-chapter").value=keys[pos<0?keys.length-1:pos];renderText();
 }
 async function loadReport(file){
  if(!file)return;
  if(file.size>80_000_000)throw Error("報告超過 80MB，請使用較小的審核分片");
  let payload;
  if(file.name.toLowerCase().endsWith(".json"))payload=JSON.parse(await file.text());
  else{
   const entries=rawEntries(await file.arrayBuffer()).filter(x=>x.name.includes("ad-candidates-"));
   if(entries.length!==1)throw Error("請選取只含此書單一 ad-candidates 報告的 ZIP");
   payload=JSON.parse(new TextDecoder().decode(await decodeEntry(entries[0])));
  }
  if(payload.artifact_role!=="ad_review_candidates"||payload.task_id!==taskId)
   throw Error("TXT Artifact 任務 ID 不一致，拒絕套用其他小說資料");
  report=payload;state.candidateChoices.clear();state.chapters=payload.chapter_texts||{};
  const task=[...queueTasks(),...(queue?.completed||[])].find(t=>t.task_id===taskId);
  // The desktop defaults to only the previously saved removal rules; candidate scores alone do not approve deletion.
  if(!sortKey){sortKey="score";sortDescending=true;}
  $("gui-confirm-text").disabled=task?.status!=="waiting_review"||task.stages?.cover?.status!=="completed";
  const kinds=[...new Set((payload.candidates||[]).map(c=>c.kind).filter(Boolean))];
  const selector=$("review-kind");selector.replaceChildren(new Option("所有類型",""));
  for(const name of kinds)selector.append(new Option(name,name));
  const chapter=$("review-chapter");chapter.replaceChildren();
  for(const key of chapterKeys())chapter.append(new Option("第 "+key+" 章",key));
  if(chapterKeys().length)chapter.value=chapterKeys()[0];
  renderCandidates();renderText();
  text($("review-load-status"),"已核對任務 "+taskId+"｜"+(payload.candidates?.length||0)+" 項候選｜"+chapterKeys().length+" 章｜原文指紋 "+String(payload.raw_fingerprint||"").slice(0,14));
 }
 function uniqueZipEntry(entries,filename){
  const matches=entries.filter(x=>x.name.split("/").pop()===filename);
  if(matches.length!==1)throw Error("ZIP 缺少或包含重複的 "+filename);
  return matches[0];
 }
 async function sha256Hex(bytes){
  if(!crypto?.subtle)throw Error("瀏覽器不支援安全的 SHA-256 驗證");
  const hash=new Uint8Array(await crypto.subtle.digest("SHA-256",bytes));
  return [...hash].map(b=>b.toString(16).padStart(2,"0")).join("");
 }
 async function loadCover(file){
  if(!file)return;
  if(file.size>80_000_000)throw Error("封面資料過大");
  const selectedTask=taskId,entries=rawEntries(await file.arrayBuffer());
  const prompt=uniqueZipEntry(entries,"master_cover_prompt.json");
  const image=uniqueZipEntry(entries,"master_cover.jpg");
  const manifest=uniqueZipEntry(entries,"cover-preflight-manifest.json");
  const promptBytes=await decodeEntry(prompt),imageBytes=await decodeEntry(image);
  const m=JSON.parse(new TextDecoder().decode(await decodeEntry(manifest)));
  if(m.artifact_role!=="cover_preflight"||m.schema_version!==1||m.task_id!==selectedTask)
   throw Error("封面 Manifest 與目前小說任務不一致，拒絕套用");
  if(!/^[0-9a-f]{64}$/.test(m.cover_sha256||"")||
     !/^[0-9a-f]{64}$/.test(m.prompt_sha256||"")||
     await sha256Hex(imageBytes)!==m.cover_sha256||
     await sha256Hex(promptBytes)!==m.prompt_sha256)
   throw Error("封面或 Prompt 的 SHA-256 驗證失敗，拒絕套用不同版本資料");
  if(taskId!==selectedTask||!$("review-dialog").open)return;
  const info=JSON.parse(new TextDecoder().decode(promptBytes));
  state.prompt=String(info.prompt||info.brief?.prompt||"");
  text($("review-cover-prompt"),state.prompt||"尚無 Prompt");
  text($("review-cover-analysis"),JSON.stringify(info.brief||info,null,2));
  if(coverUrl)URL.revokeObjectURL(coverUrl);
  coverUrl=URL.createObjectURL(new Blob([imageBytes],{type:"image/jpeg"}));
  $("review-cover-preview").src=coverUrl;$("review-cover-preview").hidden=false;
  coverEvidence={task_id:selectedTask,cover_sha256:m.cover_sha256,
    prompt_sha256:m.prompt_sha256};
  text($("review-cover-status"),"已核對封面任務 "+selectedTask+" 與兩份 SHA-256 指紋");
  const task=[...queueTasks(),...(queue?.completed||[])].find(t=>t.task_id===selectedTask);$("gui-replace-cover").disabled=task?.workflow_phase!=="preflight";
 }
 async function loadReviewFiles(files){
  const pending=Array.from(files||[]);
  if(!pending.length)return;
  if(pending.length>6)throw Error("一次最多可匯入 6 個檔案");
  let txt=0,cover=0;
  for(const file of pending){
   if(!/\.(?:zip|json)$/i.test(file.name))throw Error("只接受 TXT JSON 或封面／TXT ZIP");
   if(file.size>80_000_000)throw Error("審核檔案超過 80 MB");
   if(file.name.toLowerCase().endsWith(".json")){
    await loadReport(file);txt++;continue;
   }
   const entries=rawEntries(await file.arrayBuffer());
   const hasTxt=entries.some(e=>e.name.split("/").pop().startsWith("ad-candidates-"));
   const hasCover=entries.some(e=>e.name.split("/").pop()==="cover-preflight-manifest.json");
   if(!hasTxt&&!hasCover)throw Error("ZIP 沒有 TXT 或封面審核內容");
   if(hasTxt){await loadReport(file);txt++;}
   if(hasCover){await loadCover(file);cover++;}
  }
  notice("已匯入 TXT "+txt+" 份、封面 "+cover+" 份；請檢查 Raw/Clean 與封面內容。");
 }
 window.audioReviewCoverEvidence=function(){
  if(!coverEvidence||coverEvidence.task_id!==taskId)
   throw Error("本書實際封面審核資料尚未載入，請重新載入封面資料，核對圖片與 Prompt 後再開始後製");
  return {...coverEvidence};
 };
 window.audioReviewEvidence=function(){
  if(!report||report.task_id!==taskId||!chapterKeys().length||
     !/^[0-9a-f]{64}$/.test(String(report.raw_fingerprint||"")))
    throw Error("本書實際 TXT 審核資料尚未載入，請重新載入或確認 TXT 工作完成後再審核");
  return {task_id:taskId,raw_fingerprint:report.raw_fingerprint};
 };
 let reviewLoadGeneration=0;
 async function loadCloudReview(){
  const selected=taskId, generation=++reviewLoadGeneration;
  const valid=()=>taskId===selected&&generation===reviewLoadGeneration&&$("review-dialog").open;
  const task=[...queueTasks(),...(queue?.completed||[])].find(t=>t.task_id===selected);
  if(!task)throw Error("找不到待審核小說");
  const kinds=mode==="cover"?["cover"]:mode==="text"?["scrape"]:["scrape","cover"];
  if(kinds.some(k=>!(k==="cover"?task.cover_run_id:task.scrape_run_id)))throw Error("本次審核 Run 尚未產生");
  const button=$("review-auto-load");button.disabled=true;
  const indicator=$("review-auto-status");
  try{
   text(indicator,"正在由雲端讀取本書 TXT／封面審核資料…");
   const requestIds=await Promise.all(kinds.map(k=>window.audioRequestArtifact(selected,k==="cover"?task.cover_run_id:task.scrape_run_id,k)));
   if(requestIds.some(x=>!x))throw Error("雲端檔案請求未成功排入");
   text(indicator,"正在取得本次實際審核資料…");
   const loadOne=async(requestId,kind)=>{
    const assets=await window.audioWaitForAsset(requestId,valid);
    if(!valid())return;
    const grouped=window.audioAssetGroups(assets);
    if(grouped.length!==1)throw Error("審核檔案群組不完整或無法辨認");
    const blob=await window.audioCombineAssetBlobs(assets);
    if(!valid())return;
    const file=new File([blob],grouped[0].name,{type:"application/zip"});
    await loadReviewFiles([file]);
    text(indicator,kind+" 已通過檔案格式驗證。");
   };
   const results=await Promise.allSettled(kinds.map((k,i)=>loadOne(requestIds[i],k==="scrape"?"TXT":"封面")));
   if(!valid())return;
   const errors=results.filter(x=>x.status==="rejected").map(x=>x.reason?.message||"未知錯誤");
   if(errors.length)throw Error(errors.join("；"));
   text(indicator,"已自動載入本書審核資料；可開始檢查候選詞句與 Raw／Clean。");
  }catch(error){
   if(valid())text(indicator,"自動載入失敗："+error.message+"。可按「重新載入」，若 TXT 工作失敗請先返回佇列重新排程。");
  }finally{if(valid())button.disabled=false;}
 }
 const original=window.openReview;
 window.openReview=function(id,kind="combined"){
  const t=[...queueTasks(),...(queue?.completed||[])].find(x=>x.task_id===id);
  if(!t||kind==="combined"&&t.status!=="waiting_review")throw Error("小說尚未進入審核");
  // Show the real GUI panel even if its source files are not ready yet.
  mode=kind;selectedCandidates.clear();lastSelected=null;sortKey="";sortDescending=false;reviewLoadGeneration++;
  taskId=id;bookTitle=t.book_title||"";report=null;coverEvidence=null;
  state.candidateChoices.clear();
  state.chapters={};state.prompt="";
  original(id);
  // Desktop GUI also reads the latest cloud Profile, not only the task snapshot.
  const initialPatterns=$("review-patterns").value;
  if(typeof window.audioReadBookCleaner==="function"){
   void window.audioReadBookCleaner(t).then(saved=>{
    if(taskId!==id||!$("review-dialog").open||$("review-patterns").value!==initialPatterns)return;
    setPatterns(saved);
   }).catch(()=>{/* Keep the task snapshot if the Profile temporarily cannot be read. */});
  }
  $("review-dialog").dataset.mode=kind;
  text($("review-title"),(kind==="cover"?"封面圖與小說介紹／HF Prompt":kind==="text"?"文字清理／關鍵字審核":"文字清理、Raw/Clean 抽查與封面審核")+"｜《"+bookTitle+"》");
  $("gui-text-tabs").classList.toggle("hidden",kind!=="text");$("gui-confirm-text").classList.toggle("hidden",kind!=="text");$("gui-confirm-text").disabled=true;
  $("review-save-only").classList.toggle("hidden",kind!=="combined");$("gui-combined-start").classList.toggle("hidden",kind!=="combined");$("gui-replace-cover").disabled=!["queued","paused","waiting_review","preparing_assets","needs_attention"].includes(t.status);
  $("review-auto-load").disabled=false;setTextTab("candidates");setCoverTab("analysis");
  for(const field of ["review-report-file","review-cover-file","review-drop-files","review-search"])$(field).value="";
  $("review-kind").value="";$("review-state").value="";
  $("review-chapter").replaceChildren(new Option("尚未載入",""));
  text($("review-load-status"),"正在等待本書 TXT 審核報告，完成後自動顯示候選詞句與原文。");
  if(coverUrl){URL.revokeObjectURL(coverUrl);coverUrl=null;}
  $("review-cover-preview").hidden=true;
  text($("review-cover-analysis"),"尚未載入");text($("review-cover-prompt"),"尚未載入");
  text($("review-cover-status"),"尚未核對封面 Manifest");
  $("review-scrape-link").href="https://github.com/"+REPO+"/actions/runs/"+t.scrape_run_id;
  $("review-cover-link").href="https://github.com/"+REPO+"/actions/runs/"+t.cover_run_id;
  renderCandidates();renderText();
  text($("review-auto-status"),"正在自動讀取本書 GitHub Actions 審核結果…");
  const ready=kind==="cover"?!!t.cover_run_id:kind==="text"?!!t.scrape_run_id:!!(t.cover_run_id&&t.scrape_run_id);
  if(ready)void loadCloudReview().catch(e=>text($("review-auto-status"),"載入失敗："+e.message));
  else{text($("review-auto-status"),kind==="cover"?"封面尚無執行紀錄；請返回佇列執行封面工作。":"本書 TXT 尚無執行紀錄；請先從佇列啟動 TXT 抓取。");$("review-auto-load").disabled=true;}
 };
 function setTextTab(tab){$("gui-candidate-pane").classList.toggle("hidden",mode==="text"&&tab!=="candidates");$("gui-raw-pane").classList.toggle("hidden",mode==="text"&&tab!=="raw");$("gui-text-candidates-tab").setAttribute("aria-pressed",String(tab==="candidates"));$("gui-text-raw-tab").setAttribute("aria-pressed",String(tab==="raw"));}
 function setCoverTab(tab){$("gui-cover-analysis-panel").classList.toggle("hidden",tab!=="analysis");$("gui-cover-prompt-panel").classList.toggle("hidden",tab!=="prompt");$("gui-cover-analysis-tab").setAttribute("aria-pressed",String(tab==="analysis"));$("gui-cover-prompt-tab").setAttribute("aria-pressed",String(tab==="prompt"));}
 document.addEventListener("DOMContentLoaded",()=>{
  $("gui-text-candidates-tab").addEventListener("click",()=>setTextTab("candidates"));$("gui-text-raw-tab").addEventListener("click",()=>setTextTab("raw"));
  $("gui-cover-analysis-tab").addEventListener("click",()=>setCoverTab("analysis"));$("gui-cover-prompt-tab").addEventListener("click",()=>setCoverTab("prompt"));
  $("gui-replace-cover").addEventListener("click",()=>{try{window.openManualCover(taskId);}catch(e){notice(e.message,"error");}});
  for(const [id,action] of [["gui-remove-selected","remove"],["gui-keep-selected","keep"],["gui-invert-selected","invert"],["gui-delete-selected","delete"]])$(id).addEventListener("click",()=>modifySelected(action));
  $("gui-edit-selected").addEventListener("click",()=>{const rows=selectedRows();if(rows.length!==1){notice("請先選取一筆候選","error");return;}editCandidate(rows[0]);});
  $("review-candidates").addEventListener("keydown",e=>{if(e.target.closest("input,button,th"))return;if(e.key===" "||e.key==="Delete"){e.preventDefault();modifySelected(e.key==="Delete"?"keep":"invert");}});
  $("gui-confirm-text").addEventListener("click",async()=>{try{const evidence=window.audioReviewEvidence();const button=$("gui-confirm-text");button.disabled=true;const id=await window.audioIssueAction("review",{task_id:taskId,confirm_only:true,remove_patterns:patterns(),candidate_overrides:window.audioReviewCandidateEdits(),evidence},"確認文字清理設定");if(id){$("review-dialog").close();}else button.disabled=false;}catch(e){notice(e.message,"error");$("gui-confirm-text").disabled=false;}});
  $("review-auto-load").addEventListener("click",()=>loadCloudReview().catch(e=>notice(e.message,"error")));
  $("review-report-file").addEventListener("change",e=>loadReviewFiles(e.target.files).catch(err=>notice(err.message,"error")));
  $("review-cover-file").addEventListener("change",e=>loadReviewFiles(e.target.files).catch(err=>notice(err.message,"error")));
  $("review-drop-files").addEventListener("change",e=>loadReviewFiles(e.target.files).catch(err=>notice(err.message,"error")));
  const drop=$("review-drop-zone");
  drop.addEventListener("dragenter",e=>{e.preventDefault();drop.classList.add("is-dragover")});
  drop.addEventListener("dragover",e=>{e.preventDefault();drop.classList.add("is-dragover")});
  drop.addEventListener("dragleave",e=>{
   if(!drop.contains(e.relatedTarget))drop.classList.remove("is-dragover");
  });
  drop.addEventListener("drop",e=>{
   e.preventDefault();drop.classList.remove("is-dragover");
   void loadReviewFiles(e.dataTransfer?.files).catch(err=>notice(err.message,"error"));
  });
  $("review-search").addEventListener("input",renderCandidates);
  $("review-kind").addEventListener("change",renderCandidates);
  $("review-state").addEventListener("change",renderCandidates);
  $("review-patterns").addEventListener("input",()=>{renderCandidates();renderText()});
  $("review-chapter").addEventListener("change",renderText);
  for(const [id,pos] of [["review-first",0],["review-middle",0.5],["review-last",-1]])
   $(id).addEventListener("click",()=>navigate(pos===0.5?Math.floor((chapterKeys().length-1)/2):pos));
  for(const [id,mode] of [["review-select-visible","remove"],["review-keep-visible","keep"],["review-invert-visible","invert"]])
   $(id).addEventListener("click",()=>{
    const names=new Set(patterns());
    for(const c of candidatesVisible()){
     if(mode==="remove")names.add(c.text);
     else if(mode==="keep")names.delete(c.text);
     else if(names.has(c.text))names.delete(c.text);else names.add(c.text);
    }setPatterns([...names]);
   });
  $("review-add-manual").addEventListener("click",()=>{
   const value=$("review-manual-pattern").value.trim();
   if(!value||value.length>10000){notice("請輸入有效的刪除文字（最多 10000 字）","error");return;}
   setPatterns([...patterns(),value]);$("review-manual-pattern").value="";
  });
  $("review-manual-pattern").addEventListener("keydown",e=>{
   if(e.key==="Enter"){e.preventDefault();$("review-add-manual").click();}
  });
  $("review-copy-prompt").addEventListener("click",()=>navigator.clipboard.writeText(state.prompt).then(()=>notice("已複製 HF Prompt")).catch(e=>notice(e.message,"error")));
  $("review-save-only").addEventListener("click",()=>{
   try{
    if(!taskId)throw Error("尚未選取小說");
    const result=audioIssueAction("review",{task_id:taskId,remove_patterns:patterns(),
      review_only:true,candidate_overrides:window.audioReviewCandidateEdits(),
      evidence:window.audioReviewEvidence()},"儲存文字清理規則");
    Promise.resolve(result).then(url=>{
     if(url)$("review-dialog").close();
    }).catch(error=>notice(error.message,"error"));
   }catch(error){notice(error.message,"error");}
  });
 });
})();
