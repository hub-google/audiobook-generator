"use strict";
(function(){
 let report=null, taskId=null, coverUrl=null, bookTitle="";
 const state={candidateChoices:new Map(),chapters:{},prompt:""};
 function editedCandidates(){
  return (report?.candidates||[]).filter(c=>typeof c?.text==="string").map(c=>({
   ...c, original_text:c.text,
   text:state.candidateChoices.has(c.text)?state.candidateChoices.get(c.text):c.text
  })).filter(c=>c.text!==null);
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
  let off=dv.getUint32(eocd+16,true),count=dv.getUint16(eocd+10,true),out=[];
  for(let i=0;i<count;i++){
   if(dv.getUint32(off,true)!==0x02014b50)throw Error("ZIP 目錄損毀");
   const method=dv.getUint16(off+10,true),size=dv.getUint32(off+20,true),len=dv.getUint16(off+28,true),
   extra=dv.getUint16(off+30,true),comment=dv.getUint16(off+32,true),local=dv.getUint32(off+42,true);
   const name=new TextDecoder().decode(bytes.slice(off+46,off+46+len));off+=46+len+extra+comment;
   if(size>50_000_000)throw Error("ZIP 單一檔案過大");
   if(!/ad-candidates-.*\.json$|master_cover_prompt\.json$|master_cover\.jpg$/.test(name))continue;
   if(dv.getUint32(local,true)!==0x04034b50)throw Error("ZIP 檔案項目錯誤");
   const start=local+30+dv.getUint16(local+26,true)+dv.getUint16(local+28,true);
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
  return editedCandidates().filter(c=>{
   const removed=patterns().includes(c.text);
   return (!q||String(c.text+" "+c.reason).toLowerCase().includes(q))&&
     (!kind||kind===c.kind)&&(!status||(status==="remove"?removed:!removed));
  });
 }
 function renderCandidates(){
  const target=$("review-candidates");target.replaceChildren();
  for(const c of candidatesVisible()){
   const label=document.createElement("div");label.className="review-candidate";
   const checkbox=document.createElement("input");checkbox.type="checkbox";checkbox.checked=patterns().includes(c.text);
   checkbox.setAttribute("aria-label","去除 "+c.text);
   checkbox.addEventListener("change",()=>{
    const names=new Set(patterns());if(checkbox.checked)names.add(c.text);else names.delete(c.text);
    setPatterns([...names]);
   });
   const details=document.createElement("span");const title=document.createElement("strong");title.textContent=c.text;
   const meta=document.createElement("small");meta.textContent=
     [c.kind,"出現 "+(c.count||0)+" 次","評分 "+(c.score||0),c.reason||""].filter(Boolean).join(" · ");
   const buttons=document.createElement("div");buttons.className="review-candidate-actions";
   const edit=document.createElement("button");edit.type="button";edit.className="secondary";
   edit.textContent="編輯詞句";edit.setAttribute("aria-label","編輯詞句 "+c.text);
   edit.addEventListener("click",()=>{
    const next=prompt("編輯候選詞句",c.text);if(next===null)return;
    const trimmed=next.trim();
    if(!trimmed||trimmed.length>10000){notice("候選文字不可空白或超過 10000 字","error");return;}
    const names=new Set(patterns()),wasRemoved=names.delete(c.text);
    if(wasRemoved)names.add(trimmed);
    if(trimmed===c.original_text)state.candidateChoices.delete(c.original_text);
    else state.candidateChoices.set(c.original_text,trimmed);
    setPatterns([...names]);
   });
   const remove=document.createElement("button");remove.type="button";remove.className="secondary";
   remove.textContent="從名單移除";remove.setAttribute("aria-label","從名單移除 "+c.text);
   remove.addEventListener("click",()=>{
    state.candidateChoices.set(c.original_text,null);
    setPatterns(patterns().filter(name=>name!==c.text));
   });
   buttons.append(edit,remove);details.append(title,meta);
   label.append(checkbox,details,buttons);target.append(label);
  }
  if(!target.childElementCount)target.textContent=report?"目前沒有符合條件的候選詞":"請先匯入 ad-candidates JSON／ZIP";
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
  const kinds=[...new Set((payload.candidates||[]).map(c=>c.kind).filter(Boolean))];
  const selector=$("review-kind");selector.replaceChildren(new Option("所有類型",""));
  for(const name of kinds)selector.append(new Option(name,name));
  const chapter=$("review-chapter");chapter.replaceChildren();
  for(const key of chapterKeys())chapter.append(new Option("第 "+key+" 章",key));
  if(chapterKeys().length)chapter.value=chapterKeys()[0];
  renderCandidates();renderText();
  text($("review-load-status"),"已核對任務 "+taskId+"｜"+(payload.candidates?.length||0)+" 項候選｜"+chapterKeys().length+" 章｜原文指紋 "+String(payload.raw_fingerprint||"").slice(0,14));
 }
 async function loadCover(file){
  if(!file)return;if(file.size>80_000_000)throw Error("封面資料過大");
  const entries=rawEntries(await file.arrayBuffer());
  const prompt=entries.find(x=>x.name.endsWith("master_cover_prompt.json"));
  const image=entries.find(x=>x.name.endsWith("master_cover.jpg"));
  if(!prompt||!image)throw Error("ZIP 不包含封面及 Prompt，請選取 cover-review Artifact");
  const info=JSON.parse(new TextDecoder().decode(await decodeEntry(prompt)));
  state.prompt=String(info.prompt||info.brief?.prompt||"");
  text($("review-cover-prompt"),state.prompt||"尚無 Prompt");
  text($("review-cover-analysis"),JSON.stringify(info.brief||info,null,2));
  if(coverUrl)URL.revokeObjectURL(coverUrl);
  coverUrl=URL.createObjectURL(new Blob([await decodeEntry(image)],{type:"image/jpeg"}));
  $("review-cover-preview").src=coverUrl;$("review-cover-preview").hidden=false;
 }
 window.audioReviewEvidence=function(){
  if(!report||report.task_id!==taskId||!chapterKeys().length||
     !/^[0-9a-f]{64}$/.test(String(report.raw_fingerprint||"")))
    throw Error("請匯入這本小說的 ad-candidates JSON / ZIP，檢查 Raw/Clean 才能核准");
  return {task_id:taskId,raw_fingerprint:report.raw_fingerprint};
 };
 const original=window.openReview;
 window.openReview=function(id){
  const t=queueTasks().find(x=>x.task_id===id);
  if(!t||t.status!=="waiting_review")throw Error("小說尚未進入審核");
  taskId=id;bookTitle=t.book_title||"";report=null;state.candidateChoices.clear();
  state.chapters={};state.prompt="";
  original(id);
  for(const field of ["review-report-file","review-cover-file","review-search"])$(field).value="";
  $("review-kind").value="";$("review-state").value="";
  $("review-chapter").replaceChildren(new Option("尚未載入",""));
  text($("review-load-status"),"請選擇此小說的實際 GitHub Artifact");
  if(coverUrl){URL.revokeObjectURL(coverUrl);coverUrl=null;}
  $("review-cover-preview").hidden=true;
  text($("review-cover-analysis"),"尚未載入");text($("review-cover-prompt"),"尚未載入");
  $("review-scrape-link").href="https://github.com/"+REPO+"/actions/runs/"+t.scrape_run_id;
  $("review-cover-link").href="https://github.com/"+REPO+"/actions/runs/"+t.cover_run_id;
  renderCandidates();renderText();
 };
 document.addEventListener("DOMContentLoaded",()=>{
  $("review-report-file").addEventListener("change",e=>loadReport(e.target.files?.[0]).catch(err=>notice(err.message,"error")));
  $("review-cover-file").addEventListener("change",e=>loadCover(e.target.files?.[0]).catch(err=>notice(err.message,"error")));
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
   if(!taskId)throw Error("尚未選取小說");
   audioIssueAction("review",{task_id:taskId,remove_patterns:patterns(),review_only:true,
     candidate_overrides:window.audioReviewCandidateEdits(),
     evidence:window.audioReviewEvidence()},"儲存文字清理規則");
   $("review-dialog").close();
  });
 });
})();
