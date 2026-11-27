"use strict";
// Public search results only. All writes go through the existing authorized Issue bridge.
(function(){
 const byId=id=>document.getElementById(id);
 const selected=new Map(), staged=new Map();
 let current=[],searchGeneration=0,focusedUrl="";
 const say=message=>{byId("novel-search-status").textContent=message;};
 const nameOf=sid=>SOURCES.find(s=>s.id===sid)?.name||sid;
 const validNovel=item=>{
  if(!item||typeof item!=="object"||typeof item.catalog_url!=="string"||
     typeof item.title!=="string"||!item.title.trim())return null;
  try{
   const source=getSource(item.catalog_url);
   if(source.id!==item.source_id)return null;
   return {catalog_url:item.catalog_url,book_title:item.title.trim().slice(0,160),
     author:String(item.author||"").slice(0,120),source_id:source.id};
  }catch{return null;}
 };
 function status(){
  byId("novel-search-result-count").textContent="本次找到 "+current.length+" 本，已勾選 "+selected.size+" 本";
  byId("novel-search-stage").disabled=selected.size===0;
 }
 function renderResults(){
  const body=byId("novel-search-body");body.replaceChildren();
  byId("novel-search-results").classList.remove("hidden");
  if(!current.length){
   const tr=document.createElement("tr"),td=document.createElement("td");
   td.className="empty";td.colSpan=5;td.textContent="沒有符合條件且可解析的小說，請換關鍵字或網站。";
   tr.append(td);body.append(tr);
  }
  for(const novel of current){
   const tr=document.createElement("tr");
   const pick=document.createElement("td"),check=document.createElement("input");
   check.type="checkbox";check.checked=selected.has(novel.catalog_url);
   check.setAttribute("aria-label","選擇 "+novel.book_title);
   check.addEventListener("change",()=>{
    if(check.checked)selected.set(novel.catalog_url,novel);
    else selected.delete(novel.catalog_url);
    status();
   });
   pick.append(check);tr.append(pick);
   for(const value of [novel.book_title,novel.author||"網站未提供",nameOf(novel.source_id)]){
    const td=document.createElement("td");td.textContent=value;tr.append(td);
   }
   const td=document.createElement("td"),link=document.createElement("a");
   link.href=novel.catalog_url;link.textContent="查看來源";link.target="_blank";link.rel="noopener noreferrer";
   td.append(link);tr.append(td);body.append(tr);
  }
  status();
 }
 function renderStage(){
  const list=byId("novel-search-staged-list");list.replaceChildren();
  byId("novel-search-staging").classList.toggle("hidden",staged.size===0);
  byId("novel-search-staged-count").textContent="已加入 "+staged.size+" 本（尚未送出製作）";
  byId("novel-search-add-queue").disabled=staged.size===0;
  byId("novel-search-edit-one").disabled=staged.size===0;
  if(!staged.has(focusedUrl))focusedUrl=staged.keys().next().value||"";
  for(const novel of staged.values()){
   const item=document.createElement("label");item.className="staged-item";
   const radio=document.createElement("input");radio.type="radio";radio.name="novel-staged-focus";
   radio.value=novel.catalog_url;radio.checked=focusedUrl===novel.catalog_url;
   radio.addEventListener("change",()=>{focusedUrl=novel.catalog_url;renderStage();});
   const span=document.createElement("span");span.textContent=novel.book_title+" · "+nameOf(novel.source_id);
   const remove=document.createElement("button");remove.type="button";remove.textContent="移除";
   remove.setAttribute("aria-label","移除 "+novel.book_title);
   remove.addEventListener("click",()=>{staged.delete(novel.catalog_url);renderStage();});
   item.append(radio,span,remove);list.append(item);
  }
 }
 function stageSelected(){
  for(const [url,novel]of selected)staged.set(url,novel);
  if(!staged.size)throw Error("請先勾選搜尋結果");
  selected.clear();renderResults();renderStage();
  say("已將 "+staged.size+" 本放入待新增清單，可繼續搜尋其他書，或進入下一步。");
  byId("novel-search-staging").scrollIntoView({behavior:"smooth",block:"nearest"});
 }
 function editOne(){
  const novel=staged.get(focusedUrl);
  if(!novel)throw Error("請先選取一本待新增小說");
  byId("source").value=novel.source_id;
  byId("catalog-url").value=novel.catalog_url;
  byId("catalog-url").dispatchEvent(new Event("input",{bubbles:true}));
  byId("book-title").value=novel.book_title;
  byId("start-chapter").value="1";byId("end-chapter").value="10";
  for(const id of ["excluded","title-overrides","number-overrides","chapter-order"])byId(id).value="";
  byId("book-form").scrollIntoView({behavior:"smooth",block:"start"});
  say("已帶入「"+novel.book_title+"」，請按「解析網站目錄」取得全書章節，再設定並加入佇列。");
 }
 async function addQueue(){
  if(!staged.size)throw Error("請先將搜尋結果加入待新增清單");
  const books=Array.from(staged.values(),n=>({
   catalog_url:n.catalog_url,book_title:n.book_title,start_chapter:1,end_chapter:null,
   excluded_chapters:[],renumber_selected:true
  }));
  await window.addTasks(books);
  say("已開啟 GitHub 新增確認頁，請按 Submit new issue；確認完成後才會送入製作佇列。");
 }
 const SEARCH_API="https://unapiwajmgqqtdkixaab.supabase.co/functions/v1/audio-novel-search";
 let searchController=null;
 async function search(){
  const keyword=byId("novel-search-keyword").value.trim().replace(/\s+/g," ");
  if(!keyword||keyword.length>80)throw Error("請輸入 1～80 字的書名或作者");
  const source=byId("novel-search-source").value;
  if(source&&!SOURCES.some(s=>s.id===source))throw Error("不支援的來源網站");
  const generation=++searchGeneration;
  if(searchController)searchController.abort();
  searchController=new AbortController();
  const controller=searchController,timeout=setTimeout(()=>controller.abort(),45000);
  current=[];selected.clear();
  byId("novel-search-results").classList.add("hidden");
  byId("novel-search-go").disabled=true;
  say("正在搜尋 "+(source?nameOf(source):"全部 9 個來源")+"，結果會直接顯示在下方…");
  try{
   const url=new URL(SEARCH_API);
   url.searchParams.set("q",keyword);
   if(source)url.searchParams.set("source",source);
   const response=await fetch(url.toString(),{method:"GET",signal:controller.signal,cache:"no-store",
     headers:{"Accept":"application/json"}});
   const data=await response.json();
   if(!response.ok)throw Error(data.error||"搜尋服務 HTTP "+response.status);
   if(generation!==searchGeneration)return;
   if(!Array.isArray(data.results)||!Array.isArray(data.sources))throw Error("搜尋結果格式不正確");
   const dedup=new Map();
   for(const entry of data.results){const novel=validNovel(entry);
    if(novel&&!dedup.has(novel.catalog_url))dedup.set(novel.catalog_url,novel);}
   current=[...dedup.values()];selected.clear();renderResults();
   const summary=data.sources.map(s=>nameOf(s.source_id)+"："+
     (s.status==="unavailable"?"暫時無法搜尋":(s.results?.length||0)+" 本")).join("　");
   byId("novel-search-source-status").textContent=summary;
   say("搜尋完成：找到 "+current.length+" 本；可直接勾選多本並加入下方待新增清單。");
  }catch(error){
   if(generation!==searchGeneration)return;
   const message=error.name==="AbortError"?"搜尋連線逾時，請再試一次":error.message;
   say("搜尋失敗："+message+"。未新增任何製作任務。");
  }finally{
   clearTimeout(timeout);
   if(generation===searchGeneration){byId("novel-search-go").disabled=false;searchController=null;}
  }
 }
 document.addEventListener("DOMContentLoaded",()=>{
  for(const source of SOURCES){
   const opt=document.createElement("option");opt.value=source.id;opt.textContent=source.name;
   byId("novel-search-source").append(opt);
  }
  const bind=(id,fn)=>byId(id).addEventListener("click",()=>{
   try{const task=fn();if(task&&typeof task.catch==="function")task.catch(e=>notice(e.message,"error"));}
   catch(e){notice(e.message,"error");}
  });
  bind("novel-search-go",search);
  byId("novel-search-keyword").addEventListener("keydown",e=>{
   if(e.key==="Enter"){e.preventDefault();byId("novel-search-go").click();}
  });
  bind("novel-search-select-all",()=>{
   current.forEach(n=>selected.set(n.catalog_url,n));renderResults();
  });
  bind("novel-search-select-none",()=>{
   current.forEach(n=>selected.delete(n.catalog_url));renderResults();
  });
  bind("novel-search-stage",stageSelected);
  bind("novel-search-edit-one",editOne);
  bind("novel-search-add-queue",addQueue);
 });
})();
