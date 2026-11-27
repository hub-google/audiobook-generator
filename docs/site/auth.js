"use strict";
/* AUDIO uses user-scoped Supabase Auth access tokens, never a repository PAT. */
(function(){
 const URL_BASE="https://unapiwajmgqqtdkixaab.supabase.co";
 const PUBLIC_KEY="sb_publishable_L09XC4Tekm6cZaPTTczm3g_Aap-SOSy";
 const STORE="audio-user-session-v1";
 let session=null,loginPromise=null,loginResolve=null,loginReject=null;
 try{session=JSON.parse(sessionStorage.getItem(STORE)||"null");}catch{}
 const authHeaders=()=>({"apikey":PUBLIC_KEY,"Content-Type":"application/json","Accept":"application/json"});
 function save(data){
  session=data&&data.access_token?data:null;
  try{
   if(session)sessionStorage.setItem(STORE,JSON.stringify(session));
   else sessionStorage.removeItem(STORE);
  }catch{}
  const status=document.getElementById("audio-auth-status");
  if(status)status.textContent=session?"已驗證的站內登入":"尚未登入";
 }
 async function authPost(path,body){
  const response=await fetch(URL_BASE+"/auth/v1/"+path,{method:"POST",headers:authHeaders(),body:JSON.stringify(body),cache:"no-store"});
  const data=await response.json().catch(()=>({}));
  if(!response.ok)throw Error(data.msg||data.error_description||data.message||data.error||"身分驗證失敗");
  return data;
 }
 async function token(){
  if(!session?.access_token)throw Error("請先驗證 AUDIO 管理員身份");
  if(session.expires_at&&session.expires_at*1000-Date.now()<150000){
   if(!session.refresh_token){save(null);throw Error("登入已失效，請重新驗證");}
   const fresh=await authPost("token?grant_type=refresh_token",{refresh_token:session.refresh_token});
   if(!fresh.expires_at)fresh.expires_at=Math.floor(Date.now()/1000)+(fresh.expires_in||3600);
   save(fresh);
  }
  return session.access_token;
 }
 function attachDialog(){
  if(document.getElementById("audio-auth-dialog"))return;
  const dialog=document.createElement("dialog");dialog.id="audio-auth-dialog";
  dialog.className="audio-auth-dialog";
  dialog.innerHTML='<form method="dialog" class="audio-auth-form">'+
  '<h2>驗證 AUDIO 管理員</h2>'+
  '<p class="muted">操作不會開啟 GitHub。第一次執行雲端修改時，請以授權 Email 收取驗證信；登入後可直接在此頁操作。</p>'+
  '<label>授權 Email<input id="audio-auth-email" type="email" autocomplete="email" required placeholder="輸入管理員電子郵件"></label>'+
  '<div class="inline-actions"><button id="audio-auth-send" type="button">寄送登入驗證信</button></div>'+
  '<label>收到的驗證碼（若郵件附有驗證碼）<input id="audio-auth-otp" inputmode="numeric" autocomplete="one-time-code" maxlength="10" placeholder="輸入 6 位驗證碼"></label>'+
  '<div class="inline-actions"><button id="audio-auth-confirm" type="button">驗證並繼續</button><button id="audio-auth-close" type="button" class="secondary">取消</button></div>'+
  '<p id="audio-auth-feedback" class="muted" aria-live="polite"></p>'+
  '</form>';
  document.body.append(dialog);
  const msg=x=>{document.getElementById("audio-auth-feedback").textContent=x;};
  const email=()=>document.getElementById("audio-auth-email").value.trim().toLowerCase();
  const cancel=()=>{
   dialog.close();if(loginReject){loginReject(Error("尚未完成管理員驗證"));loginReject=null;loginResolve=null;loginPromise=null;}
  };
  document.getElementById("audio-auth-close").addEventListener("click",cancel);
  dialog.addEventListener("cancel",e=>{e.preventDefault();cancel();});
  document.getElementById("audio-auth-send").addEventListener("click",async()=>{
   if(!email()||!document.getElementById("audio-auth-email").validity.valid){msg("請先輸入有效 Email");return;}
   const button=document.getElementById("audio-auth-send");button.disabled=true;
   try{
    await authPost("otp",{email:email(),create_user:true,
     email_redirect_to:"https://hub-google.github.io/audiobook-generator/"});
    msg("驗證信已寄出。若郵件提供驗證碼，請在此輸入；若是登入連結，點擊後會回到 AUDIO 網站。");
   }catch(err){msg("無法寄送驗證信："+err.message);}
   finally{button.disabled=false;}
  });
  document.getElementById("audio-auth-confirm").addEventListener("click",async()=>{
   const code=document.getElementById("audio-auth-otp").value.trim();
   if(!email()||!code){msg("請輸入 Email 與收到的驗證碼");return;}
   const button=document.getElementById("audio-auth-confirm");button.disabled=true;
   try{
    const data=await authPost("verify",{email:email(),token:code,type:"email"});
    if(!data.access_token)throw Error("驗證結果缺少登入資訊");
    if(!data.expires_at)data.expires_at=Math.floor(Date.now()/1000)+(data.expires_in||3600);
    save(data);dialog.close();if(loginResolve)loginResolve();
    loginPromise=null;loginResolve=null;loginReject=null;
   }catch(err){msg("登入未完成："+err.message);}
   finally{button.disabled=false;}
  });
 }
 async function ensure(){
  if(session){try{await token();return;}catch{}}
  if(!loginPromise){
   attachDialog();
   loginPromise=new Promise((resolve,reject)=>{loginResolve=resolve;loginReject=reject;});
   const dialog=document.getElementById("audio-auth-dialog");
   if(!dialog.open)dialog.showModal();
   const feedback=document.getElementById("audio-auth-feedback");feedback.textContent="";
  }
  return loginPromise;
 }
 function fromCallback(){
  const hash=new URLSearchParams(location.hash.replace(/^#/,""));
  if(!hash.has("access_token"))return;
  const data={access_token:hash.get("access_token"),refresh_token:hash.get("refresh_token"),
   expires_at:Math.floor(Date.now()/1000)+(Number(hash.get("expires_in"))||3600),
   token_type:hash.get("token_type")||"bearer"};
  if(data.access_token)save(data);
  history.replaceState(history.state,"",location.pathname+location.search);
 }
 fromCallback();
 window.audioAuth={ensure,token,logout:()=>save(null),get loggedIn(){return !!session;}};
 document.addEventListener("DOMContentLoaded",()=>{attachDialog();save(session);});
})();
