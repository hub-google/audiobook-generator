"use strict";
(function(){
 let currentTaskId=null,blob=null;
 const $id=id=>document.getElementById(id);
 window.openManualCover=function(id){
  const task=queueTasks().find(t=>t.task_id===id);
  if(!task)throw Error("找不到小說");
  if(!["queued","paused","waiting_review","preparing_assets","needs_attention"].includes(task.status))
   throw Error("第二階段開始後封面已鎖定，請先重新排程");
  currentTaskId=id;blob=null;
  $id("manual-cover-file").value="";
  $id("manual-cover-download").disabled=true;$id("manual-cover-submit").disabled=true;
  text($id("manual-cover-book"),"《"+(task.book_title||"待解析")+"》");
  text($id("manual-cover-status"),"請選擇圖片，會在本機中央裁切成 1280×720。");
  $id("manual-cover-dialog").showModal();
 };
 async function prepare(file){
  if(!file)return;
  if(!["image/jpeg","image/png","image/webp"].includes(file.type))throw Error("只接受 JPG、PNG 或 WEBP");
  if(file.size>20_000_000)throw Error("圖片超過 20MB");
  const bitmap=await createImageBitmap(file,{imageOrientation:"from-image"});
  if(bitmap.width*bitmap.height>80_000_000)throw Error("圖片超過 8,000 萬像素");
  const cv=$id("manual-cover-canvas"),ctx=cv.getContext("2d");
  const scale=Math.max(1280/bitmap.width,720/bitmap.height);
  const width=bitmap.width*scale,height=bitmap.height*scale;
  ctx.fillStyle="#fff";ctx.fillRect(0,0,1280,720);
  ctx.drawImage(bitmap,(1280-width)/2,(720-height)/2,width,height);
  bitmap.close();
  blob=await new Promise((resolve,reject)=>cv.toBlob(b=>b?resolve(b):reject(Error("無法產生 JPG")),"image/jpeg",0.98));
  $id("manual-cover-download").disabled=false;$id("manual-cover-submit").disabled=false;
  text($id("manual-cover-status"),"已完成 1280×720 中央裁切；JPEG "+(blob.size/1024).toFixed(0)+" KB。請先下載檔案，並將它附加至 GitHub Issue 再提交。");
 }
 function download(){
  if(!blob||!currentTaskId)throw Error("請先選擇圖片");
  const url=URL.createObjectURL(blob),a=document.createElement("a");
  a.href=url;a.download="audiobook-"+currentTaskId+"-manual-cover.jpg";
  document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),10000);
 }
 function submit(){
  if(!blob||!currentTaskId)throw Error("請先產生手動封面");
  if(!confirm("已下載 JPEG？下一頁請將此圖片拖曳到 GitHub Issue 本文，再按 Submit new issue，未附圖將不會更新封面。"))return;
  audioIssueAction("manual_cover",{task_id:currentTaskId},"上傳手動封面");
 }
 document.addEventListener("DOMContentLoaded",()=>{
  $id("manual-cover-file").addEventListener("change",e=>prepare(e.target.files?.[0]).catch(err=>notice(err.message,"error")));
  $id("manual-cover-close").addEventListener("click",()=>$id("manual-cover-dialog").close());
  $id("manual-cover-download").addEventListener("click",download);
  $id("manual-cover-submit").addEventListener("click",submit);
 });
})();
