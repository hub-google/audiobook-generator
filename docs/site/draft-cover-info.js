"use strict";
/* Original desktop GUI: show cached cover research immediately, only call Gemini on request. */
(function(){
 const $=id=>document.getElementById(id);
 let book=null,info=null,generation=0;
 function titleAndUrl(){
  const catalog_url=$("catalog-url").value.trim(),book_title=$("book-title").value.trim();
  getSource(catalog_url);
  if(!book_title)throw Error("請先解析目錄或輸入小說名稱");
  return {catalog_url,book_title};
 }
 async function profileKey(url){
  const canonical=new URL(url);canonical.hash="";
  canonical.protocol=canonical.protocol.toLowerCase();
  canonical.hostname=canonical.hostname.toLowerCase();
  const raw=new TextEncoder().encode(canonical.href);
  const hash=new Uint8Array(await crypto.subtle.digest("SHA-256",raw));
  return [...hash].map(b=>b.toString(16).padStart(2,"0")).join("").slice(0,16);
 }
 function showInfo(data){
  info=data;
  $("draft-cover-info-analysis").textContent=JSON.stringify({
    research:data.research||{},story_facts:data.story_facts||[],
    analysis:data.analysis||{},visual_brief:data.visual_brief||{}
  },null,2);
  $("draft-cover-info-prompt").textContent=String(data.prompt||"尚未產生 Prompt");
 }
 async function open(){
  const current=titleAndUrl(),index=++generation;
  book=current;info=null;
  showInfo({});
  $("draft-cover-info-status").textContent="正在尋找同一本小說的既有封面資訊（不呼叫 Gemini）…";
  $("draft-cover-info-dialog").showModal();
  try{
   const key=await profileKey(current.catalog_url);
   const url="https://raw.githubusercontent.com/hub-google/audiobook-generator/automation-state/cover-information/"+key+".json?_="+Date.now();
   const res=await fetch(url,{cache:"no-store"});
   if(index!==generation||!$("draft-cover-info-dialog").open)return;
   if(res.status===404){$("draft-cover-info-status").textContent="尚無已產生的封面資訊；按「開始產生」才會呼叫 Gemini。";return;}
   if(!res.ok)throw Error("HTTP "+res.status);
   const data=await res.json();
   if(!data||!data.prompt)throw Error("既有檔案缺少 Prompt");
   showInfo(data);
   $("draft-cover-info-status").textContent="已載入同書籍既有封面資訊；查看與複製不耗用 Gemini 額度。";
  }catch(e){
   if(index===generation)$("draft-cover-info-status").textContent="目前無法讀取舊封面資訊："+e.message+"。仍可重新產生。";
  }
 }
 async function generate(){
  if(!book)throw Error("尚未選取書籍");
  const title=book.book_title,index=generation;
  if(info?.prompt&&!confirm("再次產生會使用 Gemini 額度，確定要重新產生封面資訊嗎？"))return;
  const button=$("draft-cover-info-generate");button.disabled=true;
  try{
   $("draft-cover-info-status").textContent="已提交 Gemini 封面資訊請求；由受保護的 GitHub Actions 執行，結果會留在 AUDIO。";
   const request=await window.audioIssueAction("cover_info_prequeue",{...book,force:!!info?.prompt},
    "產生《"+title+"》封面資訊");
   if(!request)throw Error("後端沒有接收產生封面資訊請求");
   const assets=await window.audioWaitForAsset(request,()=>index===generation&&$("draft-cover-info-dialog").open);
   if(index!==generation)return;
   const content=await window.audioAssetBlob(assets[0].id);
   const data=JSON.parse(await content.text());
   if(!data.prompt)throw Error("Gemini 結果缺少 Prompt");
   showInfo(data);
   $("draft-cover-info-status").textContent="封面資訊完成並已儲存；再次開啟可直接讀取，不會重複呼叫 Gemini。";
  }catch(e){
   if(index===generation)$("draft-cover-info-status").textContent="封面資訊尚未完成："+e.message+"。請查看站內雲端操作狀態。";
  }finally{if(index===generation)button.disabled=false;}
 }
 document.addEventListener("DOMContentLoaded",()=>{
  $("draft-manual-cover").addEventListener("click",()=>{
   try{window.openDraftManualCover();}catch(e){notice(e.message,"error")}
  });
  $("draft-cover-information").addEventListener("click",()=>open().catch(e=>notice(e.message,"error")));
  $("draft-cover-info-generate").addEventListener("click",()=>generate().catch(e=>notice(e.message,"error")));
  $("draft-cover-info-copy").addEventListener("click",async()=>{
   if(!info?.prompt){notice("目前沒有可複製的封面資料","error");return;}
   try{await navigator.clipboard.writeText($("draft-cover-info-analysis").textContent+"\n\n"+info.prompt);
    notice("已複製封面分析與 HF Prompt");}catch(e){notice(e.message,"error")}
  });
  $("draft-cover-info-close").addEventListener("click",()=>{$("draft-cover-info-dialog").close();generation++;});
 });
})();
