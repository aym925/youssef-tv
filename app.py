import html
import hmac
import json
import math
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from vercel import blob

# Load the .env file located beside app.py. Environment variables already
# supplied by Vercel are preserved because override=False is the default.
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / ".env")

# This app needs only the two admin values shown in the user's .env file.
# Vercel Blob credentials are discovered automatically by the official SDK.
APP_NAME = "M3U & APK Manager"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin12345@")
SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip()

if not ADMIN_PASSWORD:
    raise RuntimeError("ADMIN_PASSWORD is empty. Set it in the .env file.")
if len(SESSION_SECRET) < 32:
    raise RuntimeError("SESSION_SECRET is missing or too short. Set a random value of at least 32 characters in .env.")

# Built-in upload limits: no extra .env entries are required.
MAX_M3U_UPLOAD_BYTES = 4 * 1024 * 1024
APK_MAX_UPLOAD_BYTES = 512 * 1024 * 1024
# Keep multipart requests safely below Vercel Functions' 4.5 MB body limit.
APK_CHUNK_BYTES = 3 * 1024 * 1024
MAX_APK_PARTS = math.ceil(APK_MAX_UPLOAD_BYTES / APK_CHUNK_BYTES)
SESSION_MAX_AGE = 7 * 24 * 60 * 60
IS_VERCEL = os.getenv("VERCEL", "").lower() in {"1", "true"}

APK_MANIFEST_PATH = "apk/current.json"
M3U_ID_RE = re.compile(r"^[a-f0-9]{32}$")
VERSION_RE = re.compile(r"^[0-9][A-Za-z0-9.+_-]{0,39}$")
UPLOAD_ID_RE = re.compile(r"^[a-f0-9]{32}$")

app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="media_admin_session",
    max_age=SESSION_MAX_AGE,
    same_site="strict",
    https_only=IS_VERCEL,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


class LoginPayload(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class ApkPublishPayload(BaseModel):
    upload_id: str = Field(min_length=32, max_length=32)
    total_parts: int = Field(ge=1)
    filename: str = Field(min_length=5, max_length=180)
    version: str = Field(min_length=1, max_length=40)
    notes: str = Field(default="", max_length=1000)
    size: int = Field(gt=0)


class ApkMetadataPayload(BaseModel):
    version: str = Field(min_length=1, max_length=40)
    notes: str = Field(default="", max_length=1000)


LOGIN_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#090d18"><title>Admin Login</title>
<style>
:root{color-scheme:dark;--bg:#090d18;--panel:#111a2b;--line:#26344b;--muted:#91a0b8;--text:#eef4ff;--blue:#4c8dff;--blue2:#2865df;--red:#ff7474}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(ellipse at 50% -20%,#1c3156 0,transparent 55%),var(--bg);font-family:Inter,system-ui,-apple-system,Segoe UI,Arial,sans-serif;color:var(--text);display:grid;place-items:center;padding:22px}
.card{width:min(100%,420px);background:rgba(17,26,43,.94);border:1px solid var(--line);border-radius:22px;padding:30px;box-shadow:0 24px 80px #0008}
.logo{width:54px;height:54px;border-radius:16px;background:linear-gradient(145deg,#67a3ff,#2865df);display:grid;place-items:center;font-size:22px;font-weight:900;box-shadow:0 8px 25px #2865df55}
h1{font-size:24px;margin:20px 0 7px;letter-spacing:-.5px}.sub{color:var(--muted);font-size:14px;line-height:1.6;margin:0 0 25px}
label{display:block;font-size:13px;color:#c4d0e2;margin-bottom:9px}input{width:100%;background:#0b1220;border:1px solid #30405a;border-radius:12px;padding:15px;color:var(--text);font-size:15px;outline:none}input:focus{border-color:var(--blue);box-shadow:0 0 0 3px #4c8dff22}
button{width:100%;border:0;border-radius:12px;padding:15px;background:linear-gradient(135deg,var(--blue),var(--blue2));color:white;font-weight:750;font-size:15px;margin-top:17px;cursor:pointer}button:disabled{opacity:.6;cursor:wait}.error{color:var(--red);font-size:13px;min-height:20px;margin-top:13px}.foot{color:#71819a;font-size:12px;text-align:center;margin-top:23px}
</style></head>
<body><main class="card"><div class="logo">M3U</div><h1>Welcome back</h1><p class="sub">Sign in to manage playlist links and your app releases.</p>
<form id="loginForm"><label for="password">Admin password</label><input id="password" type="password" placeholder="Enter password" autocomplete="current-password" required autofocus><button id="loginBtn" type="submit">Sign in</button><div class="error" id="error"></div></form><div class="foot">Private admin dashboard</div></main>
<script>
const form=document.getElementById('loginForm'),btn=document.getElementById('loginBtn'),err=document.getElementById('error');
form.addEventListener('submit',async e=>{e.preventDefault();err.textContent='';btn.disabled=true;btn.textContent='Checking...';try{const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('password').value})});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Login failed');location.reload()}catch(x){err.textContent=x.message;btn.disabled=false;btn.textContent='Sign in'}});
</script></body></html>'''

DASHBOARD_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#090d18"><title>__APP_NAME__</title>
<style>
:root{color-scheme:dark;--bg:#090d18;--panel:#111a2b;--line:#26344b;--muted:#91a0b8;--text:#eef4ff;--blue:#4c8dff;--blue2:#2865df;--green:#40d7a0;--red:#ff7474}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(ellipse at 50% -20%,#1c3156 0,transparent 55%),var(--bg);font-family:Inter,system-ui,-apple-system,Segoe UI,Arial,sans-serif;color:var(--text);padding:22px}
.wrap{width:min(100%,900px);margin:0 auto}.top{display:flex;align-items:center;justify-content:space-between;gap:15px;margin:8px 0 24px}.brand{display:flex;align-items:center;gap:13px}.logo{width:46px;height:46px;border-radius:14px;background:linear-gradient(145deg,#67a3ff,#2865df);display:grid;place-items:center;font-weight:900;box-shadow:0 8px 25px #2865df44}.brand strong{font-size:16px}.brand small{display:block;color:var(--muted);margin-top:3px;font-size:12px}.logout{width:auto;margin:0;padding:10px 14px;background:#18243a;border:1px solid var(--line);font-size:13px}
.panel{background:rgba(17,26,43,.94);border:1px solid var(--line);border-radius:20px;padding:25px;box-shadow:0 24px 80px #0005;margin-bottom:18px}.heading{font-size:22px;letter-spacing:-.4px;margin:0 0 7px}.lead{color:var(--muted);font-size:13px;line-height:1.65;margin:0 0 20px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.field{margin:12px 0}.field label{display:block;font-size:13px;color:#c4d0e2;margin:0 0 8px}.field input,.field textarea{width:100%;background:#0b1220;border:1px solid #30405a;border-radius:11px;padding:13px;color:var(--text);font-size:14px;outline:none}.field textarea{min-height:86px;resize:vertical}.field input:focus,.field textarea:focus{border-color:var(--blue)}input[type=file]{display:none}
.drop{border:1.5px dashed #405675;border-radius:15px;background:#0c1525;padding:25px 16px;text-align:center;cursor:pointer;transition:.2s}.drop.over{border-color:var(--blue);background:#142443}.fileicon{font-size:29px;margin-bottom:8px}.drop b{display:block;margin-bottom:7px}.drop span{color:var(--muted);font-size:12px}.filename{margin-top:12px;color:#bcd2f4;font-size:12px;overflow-wrap:anywhere}
button{border:0;border-radius:11px;padding:12px 16px;background:linear-gradient(135deg,var(--blue),var(--blue2));color:white;font-weight:750;font-size:13px;cursor:pointer}button:disabled{opacity:.55;cursor:wait}.primary{width:100%;margin-top:12px}.secondary{background:#18243a;border:1px solid var(--line)}.danger{background:#3a1b27;border:1px solid #713245;color:#ffb5b5}.smallbtn{padding:8px 10px;font-size:12px}.hint{color:#7788a3;font-size:11px;line-height:1.6;margin:12px 2px 0}.status{display:none;margin-top:13px;padding:12px 14px;border-radius:11px;font-size:12px;line-height:1.5}.status.error{display:block;color:#ffaaaa;background:#3a1b27;border:1px solid #713245}.status.ok{display:block;color:#9bf0ce;background:#103328;border:1px solid #205b48}.status.neutral{display:block;color:#bcd2f4;background:#142746;border:1px solid #2d4770}
.list{display:grid;gap:10px;margin-top:18px}.item{display:flex;align-items:center;justify-content:space-between;gap:12px;background:#0b1423;border:1px solid #273851;border-radius:12px;padding:12px}.item-main{min-width:0;flex:1}.item-title{font-weight:700;font-size:13px;overflow-wrap:anywhere}.item-meta{color:var(--muted);font-size:11px;margin-top:5px;overflow-wrap:anywhere}.item-actions{display:flex;align-items:center;gap:7px;flex-wrap:wrap}.link{color:#9fc3ff;text-decoration:none;font-size:11px;overflow-wrap:anywhere}.link:hover{text-decoration:underline}.empty{color:var(--muted);font-size:12px;text-align:center;padding:18px;border:1px dashed #33445d;border-radius:12px}.resultbox{background:#0a1220;border:1px solid #2d405d;border-radius:12px;padding:13px;margin-top:14px}.fixedurl{font-size:13px;color:#bcd2f4;overflow-wrap:anywhere;line-height:1.5}.meta{color:var(--muted);font-size:11px;margin-top:8px}.progress{height:8px;background:#0a1220;border:1px solid #2b3b55;border-radius:20px;overflow:hidden;margin-top:12px;display:none}.bar{height:100%;width:0;background:linear-gradient(90deg,#4c8dff,#40d7a0);transition:width .15s}.sectionline{border:0;border-top:1px solid var(--line);margin:20px 0}.bottom{color:#71819a;text-align:center;font-size:11px;margin:20px 0}
@media(max-width:650px){body{padding:13px}.panel{padding:19px}.grid{grid-template-columns:1fr}.top{margin-top:3px}.item{align-items:flex-start;flex-direction:column}.item-actions{width:100%}.item-actions button{flex:1}}
</style></head>
<body><main class="wrap">
<header class="top"><div class="brand"><div class="logo">M3U</div><div><strong>__APP_NAME__</strong><small>Playlist hosting · APK release manager</small></div></div><button class="logout" id="logout">Log out</button></header>

<section class="panel">
<h1 class="heading">M3U playlist links</h1><p class="lead">Upload M3U or M3U8 files, copy their permanent links, or remove files you no longer need.</p>
<div class="drop" id="m3uDrop"><div class="fileicon">📄</div><b>Choose an M3U playlist</b><span>Tap to browse · .m3u / .m3u8 · maximum 4 MB by default</span><div class="filename" id="m3uFilename">No file selected</div></div>
<input id="m3uFile" type="file" accept=".m3u,.m3u8,audio/x-mpegurl,application/vnd.apple.mpegurl">
<button class="primary" id="m3uUpload" disabled>Upload playlist and create link</button><div class="status" id="m3uStatus"></div>
<hr class="sectionline"><div style="display:flex;align-items:center;justify-content:space-between;gap:10px"><b style="font-size:14px">Saved playlist links</b><button class="secondary smallbtn" id="refreshM3u">Refresh</button></div>
<div class="list" id="m3uList"><div class="empty">Loading playlists…</div></div>
<p class="hint">Anyone with a playlist link can open it. Only upload playlists and content you are allowed to share.</p>
</section>

<section class="panel">
<h1 class="heading">APK app update</h1><p class="lead">Upload an APK in small parts. The download URL stays the same when you publish a replacement file.</p>
<div class="grid"><div class="field"><label for="apkVersion">App version</label><input id="apkVersion" type="text" maxlength="40" placeholder="Example: 1.0.1"></div><div class="field"><label for="apkFile">APK file</label><input id="apkFile" type="file" accept=".apk,application/vnd.android.package-archive"><div class="drop" id="apkDrop" style="padding:17px 12px"><div class="fileicon">📦</div><b>Choose APK file</b><span>.apk file</span><div class="filename" id="apkFilename">No file selected</div></div></div></div>
<div class="field"><label for="apkNotes">What's new? (optional)</label><textarea id="apkNotes" maxlength="1000" placeholder="Write the changes in this version…"></textarea></div>
<button class="primary" id="apkUpload" disabled>Upload and publish new version</button>
<div class="progress" id="apkProgress"><div class="bar" id="apkProgressBar"></div></div><div class="status" id="apkStatus"></div>
<hr class="sectionline"><b style="font-size:14px">Current published APK</b><div id="apkCurrent" class="resultbox"><div class="empty">Loading APK information…</div></div>
<div class="grid" style="margin-top:15px"><div class="field"><label for="editVersion">Edit version number</label><input id="editVersion" type="text" maxlength="40" placeholder="Example: 1.0.2"></div><div class="field"><label for="editNotes">Edit update notes</label><input id="editNotes" type="text" maxlength="1000" placeholder="Optional update notes"></div></div>
<button class="secondary" id="saveApkInfo">Save version details</button>
<p class="hint">Stable download URL: <b>/app.apk</b> · Latest version information: <b>/api/app/latest</b>. Keep these URLs in your Android app; publishing a new APK does not change them.</p>
</section>
<div class="bottom">Private admin area · __APP_NAME__</div></main>
<script>
const APK_CHUNK_BYTES=__APK_CHUNK_BYTES__;
const APK_MAX_BYTES=__APK_MAX_BYTES__;
const $=id=>document.getElementById(id);
let selectedM3u=null,selectedApk=null,currentApk=null;
function showStatus(el,message,type='ok'){el.className='status '+type;el.textContent=message}
function clearStatus(el){el.className='status';el.textContent=''}
function niceSize(bytes){if(!Number.isFinite(bytes))return '';const units=['B','KB','MB','GB'];let n=bytes,i=0;while(n>=1024&&i<units.length-1){n/=1024;i++}return `${n.toFixed(i===0?0:1)} ${units[i]}`}
function safeName(name){return String(name||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function api(url,options={}){const r=await fetch(url,options);let d={};try{d=await r.json()}catch(e){}if(!r.ok)throw new Error(d.detail||d.error||`Request failed (${r.status})`);return d}
async function copyText(value,button){try{await navigator.clipboard.writeText(value)}catch(e){const t=document.createElement('textarea');t.value=value;document.body.appendChild(t);t.select();document.execCommand('copy');t.remove()}const old=button.textContent;button.textContent='Copied!';setTimeout(()=>button.textContent=old,1300)}

// M3U controls
const m3uInput=$('m3uFile'),m3uDrop=$('m3uDrop'),m3uUpload=$('m3uUpload');
function setM3uFile(f){if(!f)return;selectedM3u=f;$('m3uFilename').textContent=`${f.name} · ${niceSize(f.size)}`;m3uUpload.disabled=false;clearStatus($('m3uStatus'))}
m3uDrop.addEventListener('click',()=>m3uInput.click());m3uInput.addEventListener('change',()=>setM3uFile(m3uInput.files[0]));
m3uDrop.addEventListener('dragover',e=>{e.preventDefault();m3uDrop.classList.add('over')});m3uDrop.addEventListener('dragleave',()=>m3uDrop.classList.remove('over'));m3uDrop.addEventListener('drop',e=>{e.preventDefault();m3uDrop.classList.remove('over');setM3uFile(e.dataTransfer.files[0])});
m3uUpload.addEventListener('click',async()=>{if(!selectedM3u)return;m3uUpload.disabled=true;m3uUpload.textContent='Uploading…';clearStatus($('m3uStatus'));try{const fd=new FormData();fd.append('file',selectedM3u);const d=await api('/api/upload',{method:'POST',body:fd});showStatus($('m3uStatus'),'Playlist uploaded successfully. Link: '+d.url,'ok');selectedM3u=null;m3uInput.value='';$('m3uFilename').textContent='No file selected';await loadM3u()}catch(e){showStatus($('m3uStatus'),e.message,'error')}finally{m3uUpload.disabled=!selectedM3u;m3uUpload.textContent='Upload playlist and create link'}});
$('refreshM3u').addEventListener('click',loadM3u);
async function loadM3u(){const list=$('m3uList');list.innerHTML='<div class="empty">Loading playlists…</div>';try{const d=await api('/api/playlists');list.innerHTML='';if(!d.items.length){list.innerHTML='<div class="empty">No playlists uploaded yet.</div>';return}for(const item of d.items){const row=document.createElement('div');row.className='item';const main=document.createElement('div');main.className='item-main';const title=document.createElement('div');title.className='item-title';title.textContent=item.filename;const meta=document.createElement('div');meta.className='item-meta';meta.textContent=`${niceSize(item.size)} · ${item.id}`;const anchor=document.createElement('a');anchor.className='link';anchor.href=item.url;anchor.target='_blank';anchor.rel='noopener';anchor.textContent=item.url;main.append(title,meta,anchor);const actions=document.createElement('div');actions.className='item-actions';const copy=document.createElement('button');copy.className='secondary smallbtn';copy.textContent='Copy link';copy.addEventListener('click',()=>copyText(item.url,copy));const del=document.createElement('button');del.className='danger smallbtn';del.textContent='Remove';del.addEventListener('click',async()=>{if(!confirm('Remove this playlist? Its link will stop working.'))return;del.disabled=true;try{await api('/api/m3u/'+item.id,{method:'DELETE'});await loadM3u()}catch(e){alert(e.message);del.disabled=false}});actions.append(copy,del);row.append(main,actions);list.append(row)}}catch(e){list.innerHTML='';const err=document.createElement('div');err.className='empty';err.textContent=e.message;list.append(err)}}

// APK controls
const apkInput=$('apkFile'),apkDrop=$('apkDrop'),apkUpload=$('apkUpload');
function setApkFile(f){if(!f)return;selectedApk=f;$('apkFilename').textContent=`${f.name} · ${niceSize(f.size)}`;apkUpload.disabled=false;clearStatus($('apkStatus'))}
apkDrop.addEventListener('click',()=>apkInput.click());apkInput.addEventListener('change',()=>setApkFile(apkInput.files[0]));
apkDrop.addEventListener('dragover',e=>{e.preventDefault();apkDrop.classList.add('over')});apkDrop.addEventListener('dragleave',()=>apkDrop.classList.remove('over'));apkDrop.addEventListener('drop',e=>{e.preventDefault();apkDrop.classList.remove('over');setApkFile(e.dataTransfer.files[0])});
apkUpload.addEventListener('click',async()=>{if(!selectedApk)return;const version=$('apkVersion').value.trim();if(!version){showStatus($('apkStatus'),'Enter the app version first.','error');return}if(!selectedApk.name.toLowerCase().endsWith('.apk')){showStatus($('apkStatus'),'Please choose an .apk file.','error');return}if(selectedApk.size>APK_MAX_BYTES){showStatus($('apkStatus'),`File is larger than the configured limit (${niceSize(APK_MAX_BYTES)}).`,'error');return}const uploadId=crypto.randomUUID().replace(/-/g,'');const totalParts=Math.ceil(selectedApk.size/APK_CHUNK_BYTES);apkUpload.disabled=true;apkUpload.textContent='Uploading…';$('apkProgress').style.display='block';$('apkProgressBar').style.width='0%';clearStatus($('apkStatus'));try{for(let i=0;i<totalParts;i++){const start=i*APK_CHUNK_BYTES,end=Math.min(start+APK_CHUNK_BYTES,selectedApk.size);const piece=selectedApk.slice(start,end);const fd=new FormData();fd.append('upload_id',uploadId);fd.append('part_index',String(i));fd.append('total_parts',String(totalParts));fd.append('file',piece,'part.bin');await api('/api/apk/chunk',{method:'POST',body:fd});const pct=Math.round(((i+1)/totalParts)*100);$('apkProgressBar').style.width=pct+'%';showStatus($('apkStatus'),`Uploading part ${i+1} of ${totalParts} (${pct}%)…`,'neutral')}
await api('/api/apk/publish',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({upload_id:uploadId,total_parts:totalParts,filename:selectedApk.name,version,notes:$('apkNotes').value,size:selectedApk.size})});showStatus($('apkStatus'),'New APK version published. The fixed download URL has not changed.','ok');selectedApk=null;apkInput.value='';$('apkFilename').textContent='No file selected';$('apkNotes').value='';await loadApk()}catch(e){showStatus($('apkStatus'),e.message,'error')}finally{apkUpload.disabled=!selectedApk;apkUpload.textContent='Upload and publish new version';setTimeout(()=>$('apkProgress').style.display='none',1800)}});
async function loadApk(){const box=$('apkCurrent');try{const d=await api('/api/apk/status');currentApk=d.available?d:null;if(!d.available){box.innerHTML='<div class="empty">No APK published yet.</div>';return}box.innerHTML=`<div class="item-title">${safeName(d.filename)}</div><div class="item-meta">Version ${safeName(d.version)} · ${niceSize(d.size)} · ${safeName(d.updated_at||'')}</div><div class="resultbox"><div class="fixedurl" id="apkFixedUrl">${safeName(d.download_url)}</div><div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px"><button class="secondary smallbtn" id="copyApkUrl">Copy fixed link</button><a class="link" href="${safeName(d.download_url)}" target="_blank" rel="noopener" style="align-self:center">Open download</a><button class="danger smallbtn" id="deleteApk">Remove APK</button></div><div class="meta">Latest version API: ${safeName(d.latest_url)}</div></div><div class="meta" style="white-space:pre-wrap">${safeName(d.notes||'No release notes.')}</div>`;$('editVersion').value=d.version;$('editNotes').value=d.notes||'';$('copyApkUrl').addEventListener('click',e=>copyText(d.download_url,e.currentTarget));$('deleteApk').addEventListener('click',async e=>{if(!confirm('Remove the current APK? The fixed download link will return 404.'))return;e.currentTarget.disabled=true;try{await api('/api/apk',{method:'DELETE'});showStatus($('apkStatus'),'Current APK removed.','ok');await loadApk()}catch(x){alert(x.message);e.currentTarget.disabled=false}})}catch(e){box.innerHTML='<div class="empty">'+safeName(e.message)+'</div>'}}
$('saveApkInfo').addEventListener('click',async()=>{const version=$('editVersion').value.trim();if(!version){showStatus($('apkStatus'),'Enter a version number.','error');return}const btn=$('saveApkInfo');btn.disabled=true;try{await api('/api/apk/metadata',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({version,notes:$('editNotes').value})});showStatus($('apkStatus'),'Version information updated.','ok');await loadApk()}catch(e){showStatus($('apkStatus'),e.message,'error')}finally{btn.disabled=false}});
$('logout').addEventListener('click',async()=>{await fetch('/api/logout',{method:'POST'});location.reload()});
loadM3u();loadApk();
</script></body></html>'''


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if request.session.get("authenticated") is True:
        page = DASHBOARD_HTML.replace("__APP_NAME__", html.escape(APP_NAME))
        page = page.replace("__APK_CHUNK_BYTES__", str(APK_CHUNK_BYTES))
        page = page.replace("__APK_MAX_BYTES__", str(APK_MAX_UPLOAD_BYTES))
    else:
        page = LOGIN_HTML
    return HTMLResponse(page)


@app.post("/api/login")
async def login(payload: LoginPayload, request: Request):
    if not hmac.compare_digest(payload.password, ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="Incorrect password")
    request.session.clear()
    request.session["authenticated"] = True
    return {"success": True}


@app.post("/api/logout")
async def logout(request: Request):
    request.session.clear()
    return {"success": True}


def require_login(request: Request) -> None:
    if request.session.get("authenticated") is not True:
        raise HTTPException(status_code=401, detail="Please sign in first")


def get_site_origin(request: Request) -> str:
    forwarded_host = request.headers.get("x-forwarded-host")
    host = forwarded_host or request.headers.get("host")
    forwarded_proto = request.headers.get("x-forwarded-proto", "https" if IS_VERCEL else "http")
    if not host:
        return str(request.base_url).rstrip("/")
    clean_host = host.split(",")[0].strip()
    clean_proto = forwarded_proto.split(",")[0].strip()
    return f"{clean_proto}://{clean_host}"


def safe_filename(value: str, fallback: str) -> str:
    name = Path(value or fallback).name
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")
    return name[:150] or fallback


async def read_json_blob(pathname: str):
    try:
        result = await blob.get_blob(pathname, access="public")
    except Exception as exc:
        if exc.__class__.__name__ in {"BlobNotFoundError", "FileNotFoundError"}:
            return None
        raise
    try:
        value = json.loads(result.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=502, detail="Stored metadata is invalid")
    return value if isinstance(value, dict) else None


async def get_current_apk_manifest():
    try:
        return await read_json_blob(APK_MANIFEST_PATH)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="Could not read APK information from Vercel Blob")


async def delete_apk_parts(upload_id: str, total_parts: int) -> None:
    if not UPLOAD_ID_RE.fullmatch(upload_id or ""):
        return
    paths = [f"apk/releases/{upload_id}/part-{i:05d}.bin" for i in range(total_parts)]
    # The SDK accepts up to 1,000 pathnames in one delete request.
    for offset in range(0, len(paths), 1000):
        await blob.delete_blob(paths[offset:offset + 1000])


@app.post("/api/upload")
async def upload_playlist(request: Request, file: UploadFile = File(...)):
    require_login(request)
    original_name = safe_filename(file.filename or "playlist.m3u", "playlist.m3u")
    if not original_name.lower().endswith((".m3u", ".m3u8")):
        raise HTTPException(status_code=400, detail="Please upload an .m3u or .m3u8 file")
    data = await file.read(MAX_M3U_UPLOAD_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="The selected file is empty")
    if len(data) > MAX_M3U_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File is too large. Maximum size is {MAX_M3U_UPLOAD_BYTES // (1024 * 1024)} MB")
    try:
        decoded = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="The playlist must be a UTF-8 text file")
    first_line = next((line.strip() for line in decoded.splitlines() if line.strip()), "")
    if first_line.upper() != "#EXTM3U":
        raise HTTPException(status_code=400, detail="Invalid M3U file: the first non-empty line must be #EXTM3U")

    file_id = uuid.uuid4().hex
    pathname = f"playlists/{file_id}.m3u"
    metadata_path = f"playlists/{file_id}.json"
    try:
        result = await blob.put_blob(
            pathname,
            data,
            access="public",
            content_type="audio/x-mpegurl; charset=utf-8",
            add_random_suffix=False,
            allow_overwrite=False,
            cache_control_max_age=60,
        )
        metadata = {
            "id": file_id,
            "filename": original_name,
            "size": len(data),
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }
        await blob.put_blob(
            metadata_path,
            json.dumps(metadata, ensure_ascii=False).encode("utf-8"),
            access="public",
            content_type="application/json; charset=utf-8",
            add_random_suffix=False,
            allow_overwrite=False,
            cache_control_max_age=60,
        )
    except Exception as exc:
        message = str(exc).lower()
        if "credential" in message or "token" in message or "store" in message:
            raise HTTPException(status_code=503, detail="Vercel Blob is not configured. Connect a PUBLIC Blob store to this project.")
        raise HTTPException(status_code=502, detail="Could not save the playlist to Vercel Blob")

    site_url = f"{get_site_origin(request)}/p/{file_id}.m3u"
    return {"success": True, "id": file_id, "filename": original_name, "size": len(data), "url": site_url, "storage_url": result.url}


@app.get("/api/playlists")
async def list_playlists(request: Request):
    require_login(request)
    items = []
    try:
        async for entry in blob.query_blobs(prefix="playlists/"):
            match = re.fullmatch(r"playlists/([a-f0-9]{32})\.m3u", entry.pathname)
            if not match:
                continue
            file_id = match.group(1)
            metadata = await read_json_blob(f"playlists/{file_id}.json")
            items.append({
                "id": file_id,
                "filename": (metadata or {}).get("filename") or f"{file_id}.m3u",
                "size": entry.size,
                "uploaded_at": (metadata or {}).get("uploaded_at"),
                "url": f"{get_site_origin(request)}/p/{file_id}.m3u",
            })
    except Exception:
        raise HTTPException(status_code=503, detail="Could not list playlists from Vercel Blob")
    items.sort(key=lambda item: item.get("uploaded_at") or "", reverse=True)
    return {"items": items}


@app.delete("/api/m3u/{file_id}")
async def delete_playlist(file_id: str, request: Request):
    require_login(request)
    if not M3U_ID_RE.fullmatch(file_id):
        raise HTTPException(status_code=404, detail="Playlist not found")
    try:
        await blob.delete_blob([f"playlists/{file_id}.m3u", f"playlists/{file_id}.json"])
    except Exception:
        raise HTTPException(status_code=502, detail="Could not remove the playlist")
    return {"success": True}


@app.get("/p/{file_id}.m3u")
async def serve_playlist(file_id: str):
    if not M3U_ID_RE.fullmatch(file_id):
        raise HTTPException(status_code=404, detail="Playlist not found")
    try:
        result = await blob.get_blob(f"playlists/{file_id}.m3u", access="public")
    except Exception:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return Response(
        content=result.body,
        media_type="audio/x-mpegurl",
        headers={
            "Content-Disposition": 'inline; filename="playlist.m3u"',
            "Cache-Control": "public, max-age=60",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.post("/api/apk/chunk")
async def upload_apk_chunk(
    request: Request,
    upload_id: str = Form(...),
    part_index: int = Form(...),
    total_parts: int = Form(...),
    file: UploadFile = File(...),
):
    require_login(request)
    if not UPLOAD_ID_RE.fullmatch(upload_id):
        raise HTTPException(status_code=400, detail="Invalid upload session")
    if total_parts < 1 or total_parts > MAX_APK_PARTS or part_index < 0 or part_index >= total_parts:
        raise HTTPException(status_code=400, detail="Invalid upload part information")
    data = await file.read(APK_CHUNK_BYTES + 1)
    if not data or len(data) > APK_CHUNK_BYTES:
        raise HTTPException(status_code=413, detail="Upload part is empty or larger than the configured chunk size")
    pathname = f"apk/releases/{upload_id}/part-{part_index:05d}.bin"
    try:
        await blob.put_blob(
            pathname,
            data,
            access="public",
            content_type="application/octet-stream",
            add_random_suffix=False,
            allow_overwrite=True,
            cache_control_max_age=60,
        )
    except Exception:
        raise HTTPException(status_code=502, detail="Could not save this APK upload part to Vercel Blob")
    return {"success": True, "part_index": part_index, "size": len(data)}


@app.post("/api/apk/publish")
async def publish_apk(payload: ApkPublishPayload, request: Request):
    require_login(request)
    upload_id = payload.upload_id
    filename = safe_filename(payload.filename, "application.apk")
    version = payload.version.strip()
    if not UPLOAD_ID_RE.fullmatch(upload_id):
        raise HTTPException(status_code=400, detail="Invalid upload session")
    if not filename.lower().endswith(".apk"):
        raise HTTPException(status_code=400, detail="Please upload a valid .apk filename")
    if not VERSION_RE.fullmatch(version):
        raise HTTPException(status_code=400, detail="Version must start with a number and contain only letters, numbers, dots, plus, underscores, or hyphens")
    if payload.total_parts < 1 or payload.total_parts > MAX_APK_PARTS:
        raise HTTPException(status_code=400, detail="Invalid APK part count")
    if payload.size > APK_MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="APK exceeds the configured maximum size")
    expected_parts = math.ceil(payload.size / APK_CHUNK_BYTES)
    if payload.total_parts != expected_parts:
        raise HTTPException(status_code=400, detail="APK part count does not match the file size")

    total_size = 0
    for part_index in range(payload.total_parts):
        pathname = f"apk/releases/{upload_id}/part-{part_index:05d}.bin"
        try:
            part_metadata = await blob.head_blob(pathname)
        except Exception:
            raise HTTPException(status_code=400, detail=f"APK upload is incomplete. Missing part {part_index + 1}.")
        total_size += part_metadata.size
    if total_size != payload.size:
        raise HTTPException(status_code=400, detail="Uploaded APK size does not match the selected file")

    previous = await get_current_apk_manifest()
    manifest = {
        "upload_id": upload_id,
        "total_parts": payload.total_parts,
        "filename": filename,
        "version": version,
        "notes": payload.notes.strip(),
        "size": total_size,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await blob.put_blob(
            APK_MANIFEST_PATH,
            json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
            access="public",
            content_type="application/json; charset=utf-8",
            add_random_suffix=False,
            allow_overwrite=True,
            cache_control_max_age=1,
        )
    except Exception:
        raise HTTPException(status_code=502, detail="APK parts were uploaded, but publishing the fixed link failed. Try publishing again.")

    if previous and previous.get("upload_id") != upload_id:
        try:
            await delete_apk_parts(previous.get("upload_id", ""), int(previous.get("total_parts", 0)))
        except Exception:
            pass

    return {
        "success": True,
        "version": version,
        "filename": filename,
        "size": total_size,
        "download_url": f"{get_site_origin(request)}/app.apk",
        "latest_url": f"{get_site_origin(request)}/api/app/latest",
    }


@app.get("/api/apk/status")
async def apk_status(request: Request):
    require_login(request)
    manifest = await get_current_apk_manifest()
    if not manifest:
        return {"available": False}
    origin = get_site_origin(request)
    return {
        "available": True,
        "filename": manifest.get("filename", "application.apk"),
        "version": manifest.get("version", ""),
        "notes": manifest.get("notes", ""),
        "size": manifest.get("size", 0),
        "updated_at": manifest.get("updated_at", ""),
        "download_url": f"{origin}/app.apk",
        "latest_url": f"{origin}/api/app/latest",
    }


@app.put("/api/apk/metadata")
async def update_apk_metadata(payload: ApkMetadataPayload, request: Request):
    require_login(request)
    version = payload.version.strip()
    if not VERSION_RE.fullmatch(version):
        raise HTTPException(status_code=400, detail="Invalid version number")
    manifest = await get_current_apk_manifest()
    if not manifest:
        raise HTTPException(status_code=404, detail="There is no APK to update")
    manifest["version"] = version
    manifest["notes"] = payload.notes.strip()
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        await blob.put_blob(
            APK_MANIFEST_PATH,
            json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
            access="public",
            content_type="application/json; charset=utf-8",
            add_random_suffix=False,
            allow_overwrite=True,
            cache_control_max_age=1,
        )
    except Exception:
        raise HTTPException(status_code=502, detail="Could not update APK version information")
    return {"success": True, "version": version}


@app.delete("/api/apk")
async def delete_current_apk(request: Request):
    require_login(request)
    manifest = await get_current_apk_manifest()
    if not manifest:
        return {"success": True, "already_removed": True}
    try:
        await blob.delete_blob(APK_MANIFEST_PATH)
        await delete_apk_parts(manifest.get("upload_id", ""), int(manifest.get("total_parts", 0)))
    except Exception:
        raise HTTPException(status_code=502, detail="Could not completely remove the APK")
    return {"success": True}


@app.get("/api/app/latest")
async def latest_apk_info(request: Request):
    manifest = await get_current_apk_manifest()
    if not manifest:
        raise HTTPException(status_code=404, detail="No APK is currently published")
    origin = get_site_origin(request)
    return {
        "version": manifest.get("version", ""),
        "filename": manifest.get("filename", "application.apk"),
        "size": manifest.get("size", 0),
        "notes": manifest.get("notes", ""),
        "updated_at": manifest.get("updated_at", ""),
        "download_url": f"{origin}/app.apk",
    }


@app.get("/app.apk")
async def download_current_apk():
    manifest = await get_current_apk_manifest()
    if not manifest:
        raise HTTPException(status_code=404, detail="No APK is currently published")
    upload_id = manifest.get("upload_id", "")
    total_parts = int(manifest.get("total_parts", 0))
    if not UPLOAD_ID_RE.fullmatch(upload_id) or total_parts < 1 or total_parts > MAX_APK_PARTS:
        raise HTTPException(status_code=502, detail="Published APK information is invalid")

    async def apk_stream():
        for part_index in range(total_parts):
            pathname = f"apk/releases/{upload_id}/part-{part_index:05d}.bin"
            try:
                part = await blob.get_blob(pathname, access="public")
            except Exception:
                raise RuntimeError("A published APK part could not be loaded")
            yield part.body

    filename = safe_filename(manifest.get("filename", "application.apk"), "application.apk")
    disposition_name = quote(filename)
    return StreamingResponse(
        apk_stream(),
        media_type="application/vnd.android.package-archive",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{disposition_name}",
            "Content-Length": str(int(manifest.get("size", 0))),
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/health")
async def health():
    return {"ok": True, "service": APP_NAME}
