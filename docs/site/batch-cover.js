"use strict";
(function(){
 let ids=[],activeId=null,results=new Map();
 const el=id=>document.getElementById(id);
 const task=id=>(queue?.queue||[]).concat(queue?.completed||[]).find(t=>t.task_id===id);
 const format=(id,data)=>"《"+(task(id)?.book_title||"待解析")+"》\n\n【小說架構／視覺分析】\n"+
  JSON.stringify(data.analysis||{},null,2)+"\n\n【HF 生圖 Prompt】\n"+(data.prompt||"");
 function render(){
  const list=el("batch-cover-book-list");list.replaceChildren();
  for(const id of ids){
   const entry=results.get(id);
   const button=document.createElement("button");button.type="button";
   button.className="secondary"+(activeId===id?" selected":"");
   button.textContent=(task(id)?.book_title||id)+" · "+
    (entry?.status==="completed"?"完成":entry?.status==="failed"?"失敗":"待產生");
   button.addEventListener("click",()=>{activeId=id;render();});list.append(button);
  }
  const info=results.get(activeId),data=info?.data||{};
  text(el("batch-cover-selected-title"),task(activeId)?.book_title||"尚未選擇小說");
  text(el("batch-cover-analysis"),data.analysis?JSON.stringify({
   research:data.research||{},facts:data.story_facts||[],analysis:data.analysis,
   visual_brief:data.visual_brief||{}
  },null,2):info?.error||"尚未載入");
  text(el("batch-cover-prompt"),data.prompt||info?.error||"尚未載入");
  text(el("batch-cover-summary"),"選取 "+ids.length+" 本｜成功 "+
   [...results.values()].filter(x=>x.status==="completed").length+" 本");
 }
 window.openBatchCover=function(completed=false){
  ids=completed?[...selectedCompleted]:[...selectedPending];
  const available=new Set((queue?.queue||[]).concat(queue?.completed||[]).map(t=>t.task_id));
  ids=ids.filter(id=>available.has(id));
  if(!ids.length)throw Error("請先勾選至少一本小說");
  activeId=ids[0];results=new Map();el("batch-cover-result-file").value="";
  render();el("batch-cover-dialog").showModal();
 };
 async function unzip(buffer){
  const bytes=new Uint8Array(buffer),dv=new DataView(buffer);let last=-1;
  for(let i=bytes.length-22;i>=Math.max(0,bytes.length-66000);i--)
   if(dv.getUint32(i,true)===0x06054b50){last=i;break;}
  if(last<0)throw Error("ZIP 檔案目錄無效");
  let offset=dv.getUint32(last+16,true),count=dv.getUint16(last+10,true);
  for(let i=0;i<count;i++){
   if(dv.getUint32(offset,true)!==0x02014b50)throw Error("ZIP 目錄損壞");
   const method=dv.getUint16(offset+10,true),size=dv.getUint32(offset+20,true),
    nameLen=dv.getUint16(offset+28,true),extra=dv.getUint16(offset+30,true),
    comment=dv.getUint16(offset+32,true),local=dv.getUint32(offset+42,true);
   const name=new TextDecoder().decode(bytes.slice(offset+46,offset+46+nameLen));
   offset+=46+nameLen+extra+comment;
   if(!name.endsWith("batch-cover-info.json"))continue;
   if(size>10_000_000)throw Error("結果檔案過大");
   if(dv.getUint32(local,true)!==0x04034b50)throw Error("ZIP 內容損壞");
   const start=local+30+dv.getUint16(local+26,true)+dv.getUint16(local+28,true);
   const encoded=bytes.slice(start,start+size);
   let decoded;
   if(method===0)decoded=encoded;
   else if(method===8)decoded=new Uint8Array(await new Response(new Blob([encoded])
    .stream().pipeThrough(new DecompressionStream("deflate-raw"))).arrayBuffer());
   else throw Error("不支援的 ZIP 壓縮方式");
   return JSON.parse(new TextDecoder().decode(decoded));
  }
  throw Error("找不到 batch-cover-info.json");
 }
 async function importResults(file){
  if(!file)return;
  if(file.size>20_000_000)throw Error("結果檔案超過20MB");
  const data=file.name.toLowerCase().endsWith(".json")?JSON.parse(await file.text()):
   await unzip(await file.arrayBuffer());
  if(data?.role!=="audio_batch_cover_information"||data.schema_version!==1||!Array.isArray(data.results))
   throw Error("不是 AUDIO 批次封面資訊成果");
  for(const row of data.results){
   if(!ids.includes(row.task_id))throw Error("資料包含未勾選的小說，不可混用");
   if(!["completed","failed"].includes(row.status))throw Error("無效的小說處理狀態");
  }
  results=new Map(data.results.map(row=>[row.task_id,row]));render();
 }
 async function copy(value){
  await navigator.clipboard.writeText(value);notice("已複製封面資訊");
 }
 function currentText(){
  const data=results.get(activeId)?.data;if(!data?.prompt)throw Error("目前小說沒有完成的封面資訊");
  return format(activeId,data);
 }
 function allText(){
  const blocks=[];
  for(const id of ids){
   const data=results.get(id)?.data;
   if(data?.prompt)blocks.push(format(id,data));
  }
  if(!blocks.length)throw Error("沒有可複製的封面資訊");
  return blocks.join("\n\n"+"=".repeat(60)+"\n\n");
 }
 document.addEventListener("DOMContentLoaded",()=>{
  for(const [id,done] of [["batch-cover-btn",false],["batch-cover-completed",true]])
   el(id).addEventListener("click",()=>{
    try{window.openBatchCover(done);}catch(e){notice(e.message,"error");}
   });
  el("batch-cover-generate").addEventListener("click",()=>audioIssueAction(
    "batch_cover_info",{task_ids:ids},"批次產生 "+ids.length+" 本小說封面資訊"));
  el("batch-cover-result-file").addEventListener("change",e=>
   importResults(e.target.files?.[0]).catch(err=>notice(err.message,"error")));
  el("batch-cover-manual").addEventListener("click",()=>{
   if(!activeId)throw Error("未選取小說");
   el("batch-cover-dialog").close();window.openManualCover(activeId);
  });
  el("batch-cover-copy-current").addEventListener("click",()=>
   Promise.resolve().then(()=>copy(currentText())).catch(e=>notice(e.message,"error")));
  el("batch-cover-copy-all").addEventListener("click",()=>
   Promise.resolve().then(()=>copy(allText())).catch(e=>notice(e.message,"error")));
  el("batch-cover-close").addEventListener("click",()=>el("batch-cover-dialog").close());
 });
})();
