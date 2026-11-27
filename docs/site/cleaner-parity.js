"use strict";
/* Browser port of src/cleaner.py and raw_text_normalizer.py.
   Changes to the Python source MUST update the cross-runtime golden tests.
   This module contains only pure text processing; no network or secrets. */
(function(root){
 const normalize=(value)=>{
  const lines=String(value??"").replace(/\r\n?/g,"\n").normalize("NFKC")
   .replace(/[\u200b\u200c\u200d\u2060\ufeff]/g,"")
   .replace(/[^\S\r\n]+/gu,"");
  return lines.split("\n").map(s=>s.replace(/\s+$/u,"")).join("\n").trim();
 };
 const comparison=value=>String(value??"").normalize("NFKC").toLowerCase().replace(/[^0-9a-z\u3400-\u9fff]/gu,"");
 function matchingSubstring(a,b,alo=0,ahi=a.length,blo=0,bhi=b.length){
  let bestI=alo,bestJ=blo,bestSize=0,prev=new Map();
  for(let i=alo;i<ahi;i++){
   const curr=new Map();
   for(let j=blo;j<bhi;j++){
    if(a[i]!==b[j])continue;
    const size=(prev.get(j-1)||0)+1;curr.set(j,size);
    if(size>bestSize){bestI=i-size+1;bestJ=j-size+1;bestSize=size;}
   }prev=curr;
  }return {i:bestI,j:bestJ,size:bestSize};
 }
 function sequenceRatio(a,b){
  const total=a.length+b.length;if(!total)return 1;
  function matched(alo,ahi,blo,bhi){
   const m=matchingSubstring(a,b,alo,ahi,blo,bhi);
   if(!m.size)return 0;
   return m.size+matched(alo,m.i,blo,m.j)+
    matched(m.i+m.size,ahi,m.j+m.size,bhi);
  }
  return 2*matched(0,a.length,0,b.length)/total;
 }
 const chapterHeading=/^第.{1,18}[章回節节集]/u;
 const pureOrdinal=/^第\s*[0-9零〇一二三四五六七八九十百千萬万兩两]+\s*[章回節节集]\s*(?:[（(]?\s*(?:大結局|大结局|正文完|全書完|全书完|完)\s*[）)]?)?\s*$/u;
 const openingAdvertisement=/(?:請記住本站域名|请记住本站域名|本站域名|快捷鍵\s*[:：].*返回書頁|手機閱讀|手机阅读|返回書頁|返回书页)/u;
 function isRepeatedHeading(line,title){
  const lineKey=comparison(line),titleKey=comparison(title);
  if(!lineKey||!titleKey)return false;
  if(lineKey===titleKey)return true;
  if(pureOrdinal.test(line.normalize("NFKC").trim()))return true;
  if(!chapterHeading.test(line.normalize("NFKC").trim()))return false;
  const ratio=sequenceRatio(lineKey,titleKey),
   common=matchingSubstring(lineKey,titleKey).size;
  return ratio>=.72&&common>=Math.min(6,titleKey.length);
 }
 function removeLiteralPatterns(text,patterns){
  if(!patterns.length)return text;
  for(;;){
   const spans=[];
   for(const pattern of patterns){
    let pos=0;
    while(true){
     const at=text.indexOf(pattern,pos);if(at<0)break;
     spans.push([at,at+pattern.length]);pos=at+1;
    }
   }
   if(!spans.length)return text;
   spans.sort((a,b)=>a[0]-b[0]||a[1]-b[1]);
   const merged=[];
   for(const [start,end]of spans){
    if(merged.length&&start<=merged[merged.length-1][1])
     merged[merged.length-1][1]=Math.max(end,merged[merged.length-1][1]);
    else merged.push([start,end]);
   }
   let rebuilt="",last=0;
   for(const [start,end]of merged){rebuilt+=text.slice(last,start);last=end;}
   text=rebuilt+text.slice(last);
  }
 }
 function normalizePatterns(patterns){
  const found=new Set(),result=[];
  for(const raw of patterns||[]){
   const pattern=normalize(String(raw));
   if(!pattern||found.has(pattern))continue;
   if(pattern.length>10000)throw Error("清理字詞超過 10000 字元");
   found.add(pattern);result.push(pattern);
  }return result;
 }
 function cleanTextContent(body,title,bookTitle,patterns=[],sourceLabels=["黃金屋","黄金屋"]){
  body=normalize(body);title=normalize(title);bookTitle=normalize(bookTitle);
  body=removeLiteralPatterns(body,normalizePatterns(patterns))
   .replace(/\u00a0|\u3000/gu," ");
  const titleKey=comparison(title),bookKey=comparison(bookTitle);
  const labels=new Set(sourceLabels.map(comparison));
  let nonempty=0;const lines=[];
  for(const raw of body.split("\n")){
   const line=raw.trim();if(line)nonempty++;
   let remove=false;
   if(line&&nonempty<=12){
    const key=comparison(line);
    remove=openingAdvertisement.test(line)||labels.has(key)||
     !!(bookKey&&key===bookKey)||!!(titleKey&&isRepeatedHeading(line,title));
   }if(!remove)lines.push(line);
  }
  return lines.join("\n").replace(/\n[ \t]*\n+/gu,"\n").trim();
 }
 const clausePattern=/.*?(?:……|\.\.\.|[，,；;：:。！？!?…]+)[”’」』）》）)]*|.+$/gsu;
 const commaEnd=/[，,][”’」』）》）)]*$/u;
 const turnRe=/卻|但|然而|不過|只是|所以|因此/gu;
 const visible=/[\u3400-\u9fffa-zA-Z0-9]/u;
 function splitTurn(clause){
  if(clause.length<20)return [clause];
  turnRe.lastIndex=0;
  for(const match of clause.matchAll(turnRe)){
   const i=match.index;
   if(i>=10&&clause.length-i>=5)return[
    clause.slice(0,i).replace(/[，,]+$/u,"").trimEnd()+"，",
    clause.slice(i).trimStart()
   ];
  }return [clause];
 }
 function chunkText(value){
  const paragraphs=[];
  for(const rawLine of String(value??"").split(/\r?\n/u)){
   const paragraph=rawLine.trim();if(!paragraph)continue;
   const clauses=[];
   clausePattern.lastIndex=0;
   for(const found of paragraph.matchAll(clausePattern))
    clauses.push(...splitTurn(found[0].trim()));
   const chunks=[];let pending="";
   for(const clause of clauses){
    if(!clause)continue;
    pending+=clause;
    const visibleLength=pending.replace(/[^0-9A-Za-z\u3400-\u9fff]/gu,"").length;
    if(commaEnd.test(pending)&&visibleLength<=6)continue;
    if(visible.test(pending))chunks.push(pending.trim());
    pending="";
   }
   if(pending){
    if(chunks.length)chunks[chunks.length-1]+=pending;
    else if(visible.test(pending))chunks.push(pending.trim());
   }
   if(chunks.length)paragraphs.push(chunks.join("\n"));
  }
  return paragraphs.join("\n\n");
 }
 const api={normalize,normalizePatterns,removeLiteralPatterns,cleanTextContent,chunkText,
  comparison,isRepeatedHeading,sequenceRatio};
 root.AudioCleaner=api;
 if(typeof module!=="undefined"&&module.exports)module.exports=api;
})(typeof window!=="undefined"?window:globalThis);
