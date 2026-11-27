"use strict";
/* All reference links and GitHub run details are presented in an AUDIO dialog.
   Downloads triggered with blob/object URLs remain local and are unaffected. */
(function(){
 function dialog(){
  let d=document.getElementById("audio-external-details");
  if(d)return d;
  d=document.createElement("dialog");d.id="audio-external-details";d.className="audio-reference-dialog";
  d.innerHTML='<h2 id="audio-reference-title">參考資訊</h2>'+
   '<p class="muted">資訊直接顯示於 AUDIO，不會開啟其他網站。</p>'+
   '<div id="audio-reference-content" class="audio-reference-content" aria-live="polite"></div>'+
   '<div class="inline-actions"><button type="button" class="secondary" id="audio-reference-copy">複製原始網址</button>'+
   '<button type="button" id="audio-reference-close">關閉</button></div>';
  document.body.append(d);
  document.getElementById("audio-reference-close").addEventListener("click",()=>d.close());
  return d;
 }
 async function show(raw){
  const d=dialog(),url=new URL(raw,location.href);
  if(!/^https?:$/.test(url.protocol))throw Error("不支援的連結");
  const title=document.getElementById("audio-reference-title");
  const box=document.getElementById("audio-reference-content");
  title.textContent=url.hostname.endsWith("github.com")?"GitHub 工作資訊":"來源網站資訊";
  box.replaceChildren();
  const note=document.createElement("p");note.textContent="正在讀取站內資訊…";box.append(note);
  document.getElementById("audio-reference-copy").onclick=async()=>{
   try{await navigator.clipboard.writeText(url.href);notice("已複製參考網址");}
   catch{notice("無法複製網址，請使用瀏覽器的複製功能","error");}
  };
  if(!d.open)d.showModal();
  const match=url.hostname==="github.com"&&url.pathname.match(/^\/hub-google\/audiobook-generator\/actions\/runs\/(\d+)/);
  if(match){
   try{
    const response=await fetch("https://api.github.com/repos/hub-google/audiobook-generator/actions/runs/"+match[1],{
     headers:{"Accept":"application/vnd.github+json"},cache:"no-store"});
    if(!response.ok)throw Error("目前無法取得工作資訊");
    const data=await response.json();
    const information=document.createElement("dl");information.className="audio-reference-list";
    const values=[
     ["工作名稱",data.name||"GitHub Actions"],["Run ID",match[1]],
     ["執行狀態",data.status||"未知"],
     ["執行結果",data.conclusion||"尚未結束"],["開始時間",data.run_started_at||data.created_at||"未知"]
    ];
    for(const [label,value]of values){
     const dt=document.createElement("dt"),dd=document.createElement("dd");
     dt.textContent=label;dd.textContent=value;information.append(dt,dd);
    }
    box.replaceChildren(information);
   }catch(err){note.textContent="暫時無法取得執行狀態："+err.message;}
   return;
  }
  note.textContent="這是原始參考網址，為避免誤跳出 AUDIO，本站改為直接顯示並提供複製："+url.href;
 }
 window.audioShowExternalInfo=url=>void show(url).catch(e=>notice(e.message,"error"));
 document.addEventListener("click",event=>{
  const a=event.target.closest?.("a[href]");
  if(!a||event.defaultPrevented)return;
  let url;try{url=new URL(a.href,location.href)}catch{return;}
  if((url.protocol==="http:"||url.protocol==="https:")&&url.origin!==location.origin){
   event.preventDefault();event.stopImmediatePropagation();
   window.audioShowExternalInfo(url.href);
  }
 },true);
 document.addEventListener("DOMContentLoaded",()=>{dialog();});
})();
