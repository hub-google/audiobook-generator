"use strict";
(function(){
 let currentTaskId=null,draftBook=null,blob=null;
 const $id=id=>document.getElementById(id);
 function openDialog(title){
  blob=null;
  $id("manual-cover-file").value="";
  $id("manual-cover-download").disabled=true;
  $id("manual-cover-copy").disabled=true;$id("manual-cover-submit").disabled=true;
  text($id("manual-cover-book"),"《"+title+"》");
  text($id("manual-cover-status"),"選擇 JPG／PNG／WEBP；會在本機中央裁切成 1280×720。");
  const canvas=$id("manual-cover-canvas");
  canvas.getContext("2d").clearRect(0,0,canvas.width,canvas.height);
  $id("manual-cover-dialog").showModal();
 }
 window.openManualCover=function(id){
  const task=queueTasks().find(t=>t.task_id===id);
  if(!task)throw Error("找不到小說");
  if(!["queued","paused","waiting_review","preparing_assets","needs_attention"].includes(task.status))
   throw Error("第二階段已開始，封面不可直接變更");
  currentTaskId=id;draftBook=null;
  openDialog(task.book_title||"待解析");
  void restoreExisting(task.catalog_url);
 };
 window.openDraftManualCover=function(){
  const catalog_url=$id("catalog-url").value.trim(),book_title=$id("book-title").value.trim();
  getSource(catalog_url);
  if(!book_title)throw Error("請先解析目錄或輸入小說名稱");
  currentTaskId=null;draftBook={catalog_url,book_title};
  openDialog(book_title);
  void restoreExisting(catalog_url);
 };
 async function restoreExisting(url){
  const targetUrl=url;
  try{
   const response=await fetch("https://raw.githubusercontent.com/hub-google/audiobook-generator/automation-state/audiobook-book-profiles.json?_="+Date.now(),{cache:"no-store"});
   if(!response.ok)return;
   const json=await response.json();
   const book=Object.values(json.books||{}).find(b=>b.catalog_url===targetUrl);
   const entry=book?.manual_cover||{};
   if(entry.provider!=="github"||entry.repo!=="hub-google/audiobook-generator"||
      !["automation-state","main"].includes(entry.branch)||!/^manual-covers\/[A-Za-z0-9_/-]+\/master_cover\.jpg$/.test(entry.remote_path))return;
   const ref="https://raw.githubusercontent.com/"+entry.repo+"/"+entry.branch+"/"+entry.remote_path;
   const image=await fetch(ref,{cache:"no-store"});
   if(!image.ok)return;
   const existing=await image.blob();
   if((draftBook?.catalog_url||queueTasks().find(t=>t.task_id===currentTaskId)?.catalog_url)!==targetUrl)return;
   if(blob)return; // never overwrite a user-selected new image
   await prepare(new File([existing],"previous-cover.jpg",{type:"image/jpeg"}));
   text($id("manual-cover-status"),"已從雲端載入此書原有手動封面；可直接保留或更換圖片。");
  }catch{ /* Missing old cover does not prevent manual upload. */ }
 }
 async function prepare(file){
  if(!file)return;
  if(!["image/jpeg","image/png","image/webp"].includes(file.type))throw Error("只接受 JPG、PNG 或 WEBP");
  const bitmap=await createImageBitmap(file,{imageOrientation:"from-image"});
  if(bitmap.width*bitmap.height>80_000_000)throw Error("圖片超過 8,000 萬像素");
  const cv=$id("manual-cover-canvas"),ctx=cv.getContext("2d");
  const scale=Math.max(1280/bitmap.width,720/bitmap.height);
  const width=bitmap.width*scale,height=bitmap.height*scale;
  ctx.fillStyle="#fff";ctx.fillRect(0,0,1280,720);
  ctx.drawImage(bitmap,(1280-width)/2,(720-height)/2,width,height);
  bitmap.close();
  blob=await new Promise((resolve,reject)=>cv.toBlob(b=>b?resolve(b):reject(Error("無法產生 JPG")),"image/jpeg",0.98));
  $id("manual-cover-download").disabled=false;$id("manual-cover-copy").disabled=false;$id("manual-cover-submit").disabled=false;
  text($id("manual-cover-status"),"已完成 1280×720 中央裁切；JPEG "+(blob.size/1024).toFixed(0)+" KB。可直接按下「送出並套用封面」，無須下載或開啟其他網站。");
 }
 function download(){
  if(!blob)throw Error("請先選擇圖片");
  const url=URL.createObjectURL(blob),a=document.createElement("a");
  a.href=url;a.download="audiobook-"+(currentTaskId||"new-book")+"-manual-cover.jpg";
  document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),10000);
 }
 async function copyImage(){
  if(!blob)throw Error("請先選擇圖片");
  if(!navigator.clipboard?.write||typeof ClipboardItem==="undefined")
    throw Error("目前瀏覽器不支援圖片剪貼簿，請使用下載 JPEG 方式");
  const canvas=$id("manual-cover-canvas");
  const png=await new Promise((resolve,reject)=>canvas.toBlob(
    b=>b?resolve(b):reject(Error("無法複製封面圖片")),"image/png"));
  await navigator.clipboard.write([new ClipboardItem({"image/png":png})]);
  notice("封面已複製到剪貼簿");
 }
 async function submit(){
  if(!blob||(!currentTaskId&&!draftBook))throw Error("請先產生手動封面");
  const button=$id("manual-cover-submit");button.disabled=true;
  try{
   text($id("manual-cover-status"),"正在安全上傳封面…");
   const cover_key=await window.audioUploadCover(blob);
   const op=currentTaskId?"manual_cover":"manual_cover_prequeue";
   const payload=currentTaskId?{task_id:currentTaskId,cover_key}:{...draftBook,cover_key};
   const request=await window.audioIssueAction(op,payload,"儲存手動封面");
   if(!request)throw Error("封面已上傳但尚未送入後端指令");
   $id("manual-cover-dialog").close();
   notice("封面已排入套用工作，請在本頁「雲端操作狀態」查看結果");
  }catch(error){
   text($id("manual-cover-status"),"無法套用封面："+error.message);
   throw error;
  }finally{button.disabled=false;}
 }
 document.addEventListener("DOMContentLoaded",()=>{
  $id("manual-cover-file").addEventListener("change",e=>prepare(e.target.files?.[0]).catch(err=>notice(err.message,"error")));
  $id("manual-cover-close").addEventListener("click",()=>$id("manual-cover-dialog").close());
  const zone=$id("manual-cover-canvas");
  zone.addEventListener("dragover",event=>{event.preventDefault();event.dataTransfer.dropEffect="copy";});
  zone.addEventListener("drop",event=>{
    event.preventDefault();
    prepare(event.dataTransfer?.files?.[0]).catch(err=>notice(err.message,"error"));
  });
  $id("manual-cover-download").addEventListener("click",download);
  $id("manual-cover-copy").addEventListener("click",()=>copyImage().catch(err=>notice(err.message,"error")));
  $id("manual-cover-submit").addEventListener("click",()=>submit().catch(error=>notice(error.message,"error")));
 });
})();
