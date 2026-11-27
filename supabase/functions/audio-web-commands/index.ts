import { createClient } from "npm:@supabase/supabase-js@2.95.0";
import { createRemoteJWKSet, jwtVerify } from "npm:jose@5.10.0";

const PROJECT = Deno.env.get("SUPABASE_URL") || "";
const SECRET = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") || "";
const ORIGIN = "https://hub-google.github.io";
const REPO = "hub-google/audiobook-generator";
const admin = createClient(PROJECT, SECRET, { auth: { persistSession: false, autoRefreshToken: false } });
const jwks = createRemoteJWKSet(new URL("https://token.actions.githubusercontent.com/.well-known/jwks"));
const ops = new Set(["enqueue","batch_add","edit","move","status","delete","complete","restore","requeue","cancel","review","dispatch","preview","ping","manual_cover","export_artifacts","batch_cover_info"]);
const buckets = "audio-web-manual-covers";

function reply(obj,code=200,origin=ORIGIN) {
 return new Response(JSON.stringify(obj),{status:code,headers:{
  "Content-Type":"application/json; charset=utf-8",
  "Access-Control-Allow-Origin":origin,"Access-Control-Allow-Methods":"GET, POST, OPTIONS",
  "Access-Control-Allow-Headers":"authorization, apikey, content-type",
  "Vary":"Origin","Cache-Control":"no-store"
 }});
}
function access(req) {
 const origin=req.headers.get("origin");
 if(origin && origin!==ORIGIN) throw Object.assign(new Error("來源網域未授權"),{status:403});
}
function tokenOf(req) { return (req.headers.get("authorization")||"").replace(/^Bearer\s+/i,""); }
async function userOf(req) {
 const token=tokenOf(req);
 if(!token || token.length>6000) throw Object.assign(new Error("請先在 AUDIO 網站登入"),{status:401});
 const {data,error}=await admin.auth.getUser(token);
 if(error||!data?.user?.id||!data.user.email_confirmed_at||data.user.is_anonymous)
  throw Object.assign(new Error("登入已失效，請重新驗證"),{status:401});
 const email=String(data.user.email||"").trim().toLowerCase();
 const {data:authorized,error:dbError}=await admin.from("audio_web_authorized_emails").select("email").eq("email",email).maybeSingle();
 if(dbError)throw new Error("授權資料庫無法查詢");
 if(!authorized)throw Object.assign(new Error("目前帳號沒有 AUDIO 管理權限"),{status:403});
 return data.user;
}
async function runnerOf(req) {
 const token=tokenOf(req);
 if(!token)throw Object.assign(new Error("缺少 GitHub Actions 身份驗證"),{status:401});
 const {payload}=await jwtVerify(token,jwks,{issuer:"https://token.actions.githubusercontent.com",audience:"audio-web-commands"});
 const expected=REPO+"/.github/workflows/audio-web-command-runner.yml@refs/heads/main";
 if(payload.repository!==REPO || payload.ref!=="refs/heads/main" || payload.workflow_ref!==expected ||
  !["schedule","workflow_dispatch","push"].includes(String(payload.event_name||"")))
  throw Object.assign(new Error("不是已授權的 AUDIO GitHub Actions 工作機"),{status:403});
 return payload;
}
function checkPayload(raw) {
 if(!raw || typeof raw!=="object" || Array.isArray(raw))throw Object.assign(new Error("操作內容格式不正確"),{status:400});
 const op=raw.op, data=raw.data;
 if(!ops.has(op) || typeof data!=="object" || !data || Array.isArray(data))
  throw Object.assign(new Error("不支援這個操作"),{status:400});
 if(JSON.stringify(data).length>350000)throw Object.assign(new Error("章節資料超出上限"),{status:413});
 return {op,data};
}
async function submit(req) {
 const user=await userOf(req);
 const body=await req.json();
 const {op,data}=checkPayload(body);
 if(op==="manual_cover") {
  const key=String(data.cover_key||"");
  if(!/^[0-9a-f-]{36}\/[0-9a-f-]{36}\.jpg$/.test(key) || !key.startsWith(user.id+"/"))
   throw Object.assign(new Error("手動封面不存在或不屬於此帳號"),{status:400});
  const {data:items,error}=await admin.storage.from(buckets).list(user.id,{limit:100,search:key.split("/")[1]});
  if(error || !items?.some(x=>x.name===key.split("/")[1])) throw Object.assign(new Error("找不到已上傳的封面檔案"),{status:400});
 }
 const {count,error:pendingError}=await admin.from("audio_web_commands").select("id",{count:"exact",head:true})
   .eq("requested_by",user.id).in("status",["pending","processing"]);
 if(pendingError)throw new Error("無法查詢等待中的操作");
 if((count||0)>=30)throw Object.assign(new Error("目前已有 30 個操作待處理，請先查看結果"),{status:429});
 const {data:row,error}=await admin.from("audio_web_commands")
  .insert({requested_by:user.id,op,payload:data}).select("id,request_id,status,created_at").single();
 if(error||!row)throw new Error("操作未成功送入雲端");
 return reply({ok:true,request:row},202);
}
async function statuses(req) {
 const user=await userOf(req);
 const u=new URL(req.url),id=u.searchParams.get("id");
 let query=admin.from("audio_web_commands").select("id,request_id,op,status,created_at,finished_at,result,error")
  .eq("requested_by",user.id).order("id",{ascending:false}).limit(25);
 if(id && /^[0-9a-f-]{36}$/.test(id))query=query.eq("request_id",id);
 const {data,error}=await query;
 if(error)throw new Error("無法查詢任務進度");
 return reply({requests:data||[]});
}
async function coverUpload(req) {
 const user=await userOf(req);
 if(req.headers.get("content-type")?.split(";")[0]!=="image/jpeg")throw Object.assign(new Error("只接受 JPEG 封面"),{status:415});
 const bytes=new Uint8Array(await req.arrayBuffer());
 if(bytes.length<30||bytes.length>10_000_000 || bytes[0]!==0xff||bytes[1]!==0xd8)
  throw Object.assign(new Error("封面檔案格式或容量不正確"),{status:413});
 const key=user.id+"/"+crypto.randomUUID()+".jpg";
 const {error}=await admin.storage.from(buckets).upload(key,bytes,{contentType:"image/jpeg",upsert:false});
 if(error)throw new Error("無法上傳封面："+error.message);
 return reply({ok:true,cover_key:key});
}
async function claimCommands(req) {
 await runnerOf(req);
 const expiry=new Date(Date.now()-35*60*1000).toISOString();
 const {data:expired,error:expiredErr}=await admin.from("audio_web_commands").select("id,attempt")
  .eq("status","processing").lt("claimed_at",expiry).limit(20);
 if(expiredErr)throw new Error("無法讀取逾期請求");
 for(const row of expired||[]){
  if(row.attempt>=3)await admin.from("audio_web_commands").update({status:"failed",finished_at:new Date().toISOString(),error:"工作機重試三次後仍未完成"}).eq("id",row.id).eq("status","processing");
  else await admin.from("audio_web_commands").update({status:"pending"}).eq("id",row.id).eq("status","processing");
 }
 const {data:pending,error}=await admin.from("audio_web_commands").select("id,op,payload,attempt")
  .eq("status","pending").order("id",{ascending:true}).limit(8);
 if(error)throw new Error("無法領取操作");
 const claimed=[];
 for(const row of pending||[]){
  const {data:updated,error:e}=await admin.from("audio_web_commands")
   .update({status:"processing",claimed_at:new Date().toISOString(),attempt:row.attempt+1})
   .eq("id",row.id).eq("status","pending").eq("attempt",row.attempt)
   .select("id,op,payload,attempt").maybeSingle();
  if(!e&&updated)claimed.push(updated);
 }
 return reply({commands:claimed});
}
async function finish(req) {
 await runnerOf(req);
 const body=await req.json();
 const id=Number(body.id),ok=body.ok===true;
 if(!Number.isSafeInteger(id)||id<1||typeof body.result!=="string")
  throw Object.assign(new Error("操作完成報告格式錯誤"),{status:400});
 const {data,error}=await admin.from("audio_web_commands")
  .update({status:ok?"completed":"failed",result:ok?body.result.slice(0,1500):null,error:ok?null:String(body.error||"Error").slice(0,200),finished_at:new Date().toISOString()})
  .eq("id",id).eq("status","processing").select("id").maybeSingle();
 if(error||!data)throw Object.assign(new Error("請求不存在或已完成"),{status:409});
 return reply({ok:true});
}
async function downloadCover(req) {
 await runnerOf(req);
 const key=new URL(req.url).searchParams.get("key")||"";
 if(!/^[0-9a-f-]{36}\/[0-9a-f-]{36}\.jpg$/.test(key))throw Object.assign(new Error("封面名稱錯誤"),{status:400});
 const {data,error}=await admin.storage.from(buckets).download(key);
 if(error||!data)throw Object.assign(new Error("找不到封面"),{status:404});
 return new Response(data,{headers:{"Content-Type":"image/jpeg","Cache-Control":"no-store"}});
}
Deno.serve(async req=>{
 try {
  access(req);
  if(req.method==="OPTIONS")return new Response(null,{status:204,headers:{
   "Access-Control-Allow-Origin":ORIGIN,"Access-Control-Allow-Methods":"GET, POST, OPTIONS",
   "Access-Control-Allow-Headers":"authorization, apikey, content-type","Vary":"Origin"}});
  const path=new URL(req.url).pathname.split("/").pop();
  if(req.method==="POST"&&path==="submit")return await submit(req);
  if(req.method==="GET"&&path==="status")return await statuses(req);
  if(req.method==="POST"&&path==="upload")return await coverUpload(req);
  if(req.method==="POST"&&path==="claim")return await claimCommands(req);
  if(req.method==="POST"&&path==="finish")return await finish(req);
  if(req.method==="GET"&&path==="cover")return await downloadCover(req);
  return reply({error:"找不到操作"},404);
 }catch(err){
  const status=Number(err?.status)||500;
  if(status>=500)console.error("AUDIO backend failure",err?.name||"Error");
  return reply({error:status>=500?"雲端服務暫時失敗，請稍後再試":String(err?.message||"操作失敗")},status);
 }
});
