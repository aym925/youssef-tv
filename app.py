import hmac
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from vercel import blob

# Keep the same .env behaviour as the original working M3U application.
load_dotenv()

APP_NAME = "M3U & APK Manager"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin1234")
SESSION_SECRET = os.getenv("SESSION_SECRET", "local-development-secret-change-this")
MAX_UPLOAD_BYTES = 4 * 1024 * 1024  # Original M3U limit; keep the working upload path unchanged.
IS_VERCEL = os.getenv("VERCEL", "").lower() in {"1", "true"}

# APK files are uploaded to this app in small pieces. Each request stays below
# Vercel's 4.5 MB Function request-body limit. The completed APK is then
# uploaded server-to-Blob, and downloads go directly to the Blob CDN.
APK_CHUNK_BYTES = 3 * 1024 * 1024
MAX_APK_BYTES = 150 * 1024 * 1024
APK_TEMP_PREFIX = "apk-temp"
APK_RELEASE_PREFIX = "apk/releases"
APK_CURRENT_MANIFEST = "apk-manager/current.json"

app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="m3u_admin_session",
    max_age=60 * 60 * 24 * 7,
    same_site="strict",
    https_only=IS_VERCEL,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "HEAD", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


class LoginPayload(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class ApkStartPayload(BaseModel):
    filename: str = Field(min_length=1, max_length=180)
    size: int = Field(gt=0, le=MAX_APK_BYTES)
    version: str = Field(min_length=1, max_length=40)
    notes: str = Field(default="", max_length=1500)


class ApkFinishPayload(BaseModel):
    upload_id: str = Field(min_length=32, max_length=32)


class ApkUpdatePayload(BaseModel):
    version: str = Field(min_length=1, max_length=40)
    notes: str = Field(default="", max_length=1500)


LOGIN_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#090d18"><title>M3U & APK Manager</title>
<style>
:root{color-scheme:dark;--bg:#090d18;--panel:#111a2b;--line:#26344b;--muted:#91a0b8;--text:#eef4ff;--blue:#4c8dff;--blue2:#2865df;--green:#40d7a0;--red:#ff7474}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(ellipse at 50% -20%,#1c3156 0,transparent 55%),var(--bg);font-family:Inter,system-ui,-apple-system,Segoe UI,Arial,sans-serif;color:var(--text);display:grid;place-items:center;padding:22px}
.card{width:min(100%,420px);background:rgba(17,26,43,.94);border:1px solid var(--line);border-radius:22px;padding:30px;box-shadow:0 24px 80px #0008}
.logo{width:54px;height:54px;border-radius:16px;background:linear-gradient(145deg,#67a3ff,#2865df);display:grid;place-items:center;font-size:21px;font-weight:900;box-shadow:0 8px 25px #2865df55}
h1{font-size:24px;margin:20px 0 7px;letter-spacing:-.5px}.sub{color:var(--muted);font-size:14px;line-height:1.6;margin:0 0 25px}
label{display:block;font-size:13px;color:#c4d0e2;margin-bottom:9px}input{width:100%;background:#0b1220;border:1px solid #30405a;border-radius:12px;padding:15px;color:var(--text);font-size:15px;outline:none}input:focus{border-color:var(--blue);box-shadow:0 0 0 3px #4c8dff22}
button{width:100%;border:0;border-radius:12px;padding:15px;background:linear-gradient(135deg,var(--blue),var(--blue2));color:white;font-weight:750;font-size:15px;margin-top:17px;cursor:pointer}button:disabled{opacity:.6;cursor:wait}.error{color:var(--red);font-size:13px;min-height:20px;margin-top:13px}.foot{color:#71819a;font-size:12px;text-align:center;margin-top:23px}
</style></head>
<body><main class="card"><div class="logo">M3U<br>APK</div><h1>Welcome back</h1><p class="sub">Sign in to manage your playlists and publish app updates.</p>
<form id="loginForm"><label for="password">Admin password</label><input id="password" type="password" placeholder="Enter password" autocomplete="current-password" required autofocus><button id="loginBtn" type="submit">Sign in</button><div class="error" id="error"></div></form><div class="foot">Private admin dashboard</div></main>
<script>
const form=document.getElementById('loginForm'),btn=document.getElementById('loginBtn'),err=document.getElementById('error');
form.addEventListener('submit',async e=>{e.preventDefault();err.textContent='';btn.disabled=true;btn.textContent='Checking...';try{const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('password').value})});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Login failed');location.reload()}catch(x){err.textContent=x.message;btn.disabled=false;btn.textContent='Sign in'}});
</script></body></html>'''

DASHBOARD_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#090d18"><title>M3U & APK Manager</title>
<style>
:root{color-scheme:dark;--bg:#090d18;--panel:#111a2b;--line:#26344b;--muted:#91a0b8;--text:#eef4ff;--blue:#4c8dff;--blue2:#2865df;--green:#40d7a0;--red:#ff7474}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(ellipse at 50% -20%,#1c3156 0,transparent 55%),var(--bg);font-family:Inter,system-ui,-apple-system,Segoe UI,Arial,sans-serif;color:var(--text);padding:22px}
.wrap{width:min(100%,820px);margin:0 auto}.top{display:flex;align-items:center;justify-content:space-between;gap:15px;margin:12px 0 27px}.brand{display:flex;align-items:center;gap:13px}.logo{width:46px;height:46px;border-radius:14px;background:linear-gradient(145deg,#67a3ff,#2865df);display:grid;place-items:center;font-weight:900;font-size:12px;line-height:1.05;text-align:center;box-shadow:0 8px 25px #2865df44}.brand strong{font-size:16px}.brand small{display:block;color:var(--muted);margin-top:3px;font-size:12px}.logout{width:auto;margin:0;padding:10px 14px;background:#18243a;border:1px solid var(--line);font-size:13px}
.panel{background:rgba(17,26,43,.94);border:1px solid var(--line);border-radius:22px;padding:25px;margin-bottom:18px;box-shadow:0 24px 80px #0005}.heading{font-size:22px;letter-spacing:-.5px;margin:0 0 7px}.lead{color:var(--muted);font-size:14px;line-height:1.65;margin:0 0 20px}.drop{border:1.5px dashed #405675;border-radius:17px;background:#0c1525;padding:28px 18px;text-align:center;cursor:pointer;transition:.2s}.drop.over{border-color:var(--blue);background:#142443}.fileicon{font-size:30px;margin-bottom:8px}.drop b{display:block;margin-bottom:7px}.drop span{color:var(--muted);font-size:13px}.filename{margin-top:13px;color:#bcd2f4;font-size:13px;overflow-wrap:anywhere}input[type=file]{display:none}
button{border:0;border-radius:12px;padding:13px 17px;background:linear-gradient(135deg,var(--blue),var(--blue2));color:white;font-weight:750;font-size:14px;cursor:pointer}button:disabled{opacity:.55;cursor:wait}.upload{width:100%;margin-top:15px}.hint{color:#7788a3;font-size:12px;line-height:1.6;margin:13px 2px 0}.status{display:none;margin-top:15px;padding:13px 15px;border-radius:12px;font-size:13px;line-height:1.5}.status.error{display:block;color:#ffaaaa;background:#3a1b27;border:1px solid #713245}.status.ok{display:block;color:#9bf0ce;background:#103328;border:1px solid #205b48}.result{display:none;margin-top:20px;border-top:1px solid var(--line);padding-top:20px}.result h3{font-size:15px;margin:0 0 12px}.linkbox{background:#0a1220;border:1px solid #2d405d;border-radius:12px;padding:12px;display:flex;align-items:center;gap:10px}.linktext{font-size:13px;color:#bcd2f4;overflow-wrap:anywhere;flex:1;min-width:0;line-height:1.5}.copy{width:auto;flex-shrink:0;margin:0;padding:10px 13px}.meta{color:var(--muted);font-size:12px;margin-top:10px}.bottom{color:#71819a;text-align:center;font-size:12px;margin:20px 0}.pill{display:inline-block;color:#95baff;background:#142746;padding:5px 9px;border-radius:30px;font-size:11px;margin-left:7px}
.fields{display:grid;grid-template-columns:1fr 1fr;gap:13px;margin-top:15px}.field label{display:block;font-size:13px;color:#c4d0e2;margin:0 0 8px}.field input,.field textarea{width:100%;background:#0b1220;border:1px solid #30405a;border-radius:12px;padding:13px;color:var(--text);font:inherit;font-size:14px;outline:none}.field textarea{min-height:78px;resize:vertical}.field input:focus,.field textarea:focus{border-color:var(--blue)}.full{grid-column:1/-1}.apk-current{background:#0b1424;border:1px solid #2d405d;border-radius:14px;padding:15px;margin:16px 0}.apk-current strong{font-size:15px}.apk-current .small{font-size:12px;color:var(--muted);margin-top:6px;line-height:1.5}.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}.actions button{width:auto;margin:0}.danger{background:#512634;border:1px solid #763448}.secondary{background:#18243a;border:1px solid var(--line)}.progress-wrap{display:none;margin-top:15px}.progress-track{height:9px;background:#0a1220;border:1px solid #2d405d;border-radius:20px;overflow:hidden}.progress-bar{height:100%;width:0;background:linear-gradient(90deg,#4c8dff,#40d7a0);transition:width .15s}.progress-label{font-size:12px;color:var(--muted);margin-top:7px}
@media(max-width:560px){body{padding:14px}.panel{padding:19px}.heading{font-size:20px}.top{margin-top:4px}.fields{grid-template-columns:1fr}.full{grid-column:auto}.actions button{flex:1}}
</style></head>
<body><main class="wrap">
<header class="top"><div class="brand"><div class="logo">M3U<br>APK</div><div><strong>M3U & APK Manager</strong><small>Playlist hosting and app releases</small></div></div><button class="logout" id="logout">Log out</button></header>

<section class="panel">
<h1 class="heading">M3U Playlist Manager</h1><p class="lead">Upload an M3U or M3U8 file. The original playlist upload endpoint and 4 MB limit are kept unchanged.</p>
<div class="drop" id="m3uDrop"><div class="fileicon">📄</div><b>Drop your M3U file here</b><span>or tap to browse · .m3u / .m3u8 · up to 4 MB</span><div class="filename" id="m3uFilename">No file selected</div></div>
<input id="m3uFile" type="file" accept=".m3u,.m3u8,audio/x-mpegurl,application/vnd.apple.mpegurl">
<button class="upload" id="m3uUpload" disabled>Upload and create link</button><div class="status" id="m3uStatus"></div>
<div class="result" id="m3uResult"><h3>✅ Your playlist link is ready</h3><div class="linkbox"><div class="linktext" id="m3uUrl"></div><button class="copy" id="m3uCopy">Copy</button></div><div class="meta" id="m3uMeta"></div></div>
<p class="hint">The generated playlist link is public to anyone who has it. Only upload content you are allowed to share.</p>
</section>

<section class="panel">
<h1 class="heading">Android APK Manager <span class="pill">Stable download link</span></h1>
<p class="lead">Upload an APK and set its version. The download address stays the same when you publish a newer APK.</p>
<div class="apk-current" id="apkCurrent"><strong id="apkCurrentTitle">Loading current release…</strong><div class="small" id="apkCurrentMeta"></div><div class="actions"><button class="secondary" id="copyApkLink" type="button">Copy stable APK link</button><button class="secondary" id="openApkLink" type="button">Open download link</button></div></div>
<div class="field"><label for="apkFile">Select APK file</label><div class="drop" id="apkDrop"><div class="fileicon">📦</div><b>Choose your APK file</b><span>or tap to browse · .apk · maximum 150 MB</span><div class="filename" id="apkFilename">No APK selected</div></div><input id="apkFile" type="file" accept=".apk,application/vnd.android.package-archive"></div>
<div class="fields">
<div class="field"><label for="apkVersion">App version</label><input id="apkVersion" type="text" placeholder="Example: 1.0.1" maxlength="40"></div>
<div class="field"><label for="apkNotes">Release notes</label><textarea id="apkNotes" placeholder="What changed in this version?" maxlength="1500"></textarea></div>
</div>
<button class="upload" id="apkUpload" disabled>Upload and publish APK</button>
<div class="progress-wrap" id="apkProgress"><div class="progress-track"><div class="progress-bar" id="apkProgressBar"></div></div><div class="progress-label" id="apkProgressLabel">Preparing upload…</div></div>
<div class="status" id="apkStatus"></div>
<div class="actions"><button class="secondary" id="saveApkInfo" type="button">Save version details</button><button class="danger" id="deleteApk" type="button">Delete current APK</button></div>
<p class="hint">Public download URL: <b>/app.apk</b>. Anyone opening it starts downloading the current APK. Version information is available at <b>/api/app/latest</b>.</p>
</section>
<div class="bottom">Private admin area · M3U & APK Manager</div></main>
<script>
const $=id=>document.getElementById(id);
function showStatus(el,msg,kind){el.className='status '+kind;el.textContent=msg}
function clearStatus(el){el.className='status';el.textContent=''}
async function readJson(response){let data={};try{data=await response.json()}catch{}if(!response.ok)throw new Error(data.detail||data.error||`Request failed (${response.status})`);return data}

// Original M3U upload flow: same endpoint, same form field, same behaviour.
let m3uChosen=null;
const m3uInput=$('m3uFile'),m3uDrop=$('m3uDrop'),m3uUpload=$('m3uUpload'),m3uStatus=$('m3uStatus'),m3uResult=$('m3uResult');
function setM3uFile(file){if(!file)return;m3uChosen=file;$('m3uFilename').textContent=file.name+' · '+(file.size/1024).toFixed(1)+' KB';m3uUpload.disabled=false;clearStatus(m3uStatus);m3uResult.style.display='none'}
m3uDrop.addEventListener('click',()=>m3uInput.click());m3uInput.addEventListener('change',()=>setM3uFile(m3uInput.files[0]));
m3uDrop.addEventListener('dragover',e=>{e.preventDefault();m3uDrop.classList.add('over')});m3uDrop.addEventListener('dragleave',()=>m3uDrop.classList.remove('over'));m3uDrop.addEventListener('drop',e=>{e.preventDefault();m3uDrop.classList.remove('over');setM3uFile(e.dataTransfer.files[0])});
m3uUpload.addEventListener('click',async()=>{if(!m3uChosen)return;m3uUpload.disabled=true;m3uUpload.textContent='Uploading…';clearStatus(m3uStatus);m3uResult.style.display='none';try{const fd=new FormData();fd.append('file',m3uChosen);const r=await fetch('/api/upload',{method:'POST',body:fd});const d=await readJson(r);$('m3uUrl').textContent=d.url;$('m3uMeta').textContent=d.filename+' · '+(d.size/1024).toFixed(1)+' KB';m3uResult.style.display='block';showStatus(m3uStatus,'Upload completed successfully.','ok')}catch(e){showStatus(m3uStatus,e.message,'error')}finally{m3uUpload.disabled=!m3uChosen;m3uUpload.textContent='Upload and create link'}});
$('m3uCopy').addEventListener('click',()=>copyText($('m3uUrl').textContent,$('m3uCopy')));

async function copyText(value,button){try{await navigator.clipboard.writeText(value);const old=button.textContent;button.textContent='Copied!';setTimeout(()=>button.textContent=old,1300)}catch{const t=document.createElement('textarea');t.value=value;document.body.appendChild(t);t.select();document.execCommand('copy');t.remove();}}

// APK uploads use 3 MB requests, not one large request to the Vercel Function.
let apkChosen=null,currentApk=null;
const apkInput=$('apkFile'),apkDrop=$('apkDrop'),apkUpload=$('apkUpload'),apkStatus=$('apkStatus');
function setApkFile(file){if(!file)return;apkChosen=file;$('apkFilename').textContent=file.name+' · '+(file.size/1024/1024).toFixed(2)+' MB';apkUpload.disabled=false;clearStatus(apkStatus)}
apkDrop.addEventListener('click',()=>apkInput.click());apkInput.addEventListener('change',()=>setApkFile(apkInput.files[0]));
apkDrop.addEventListener('dragover',e=>{e.preventDefault();apkDrop.classList.add('over')});apkDrop.addEventListener('dragleave',()=>apkDrop.classList.remove('over'));apkDrop.addEventListener('drop',e=>{e.preventDefault();apkDrop.classList.remove('over');setApkFile(e.dataTransfer.files[0])});

async function refreshApk(){try{const r=await fetch('/api/app/latest',{cache:'no-store'});if(r.status===404){currentApk=null;$('apkCurrentTitle').textContent='No APK published yet';$('apkCurrentMeta').textContent='Choose an APK below to publish your first release.';$('deleteApk').disabled=true;$('saveApkInfo').disabled=true;return}const d=await readJson(r);currentApk=d;$('apkCurrentTitle').textContent='Current version: '+d.version;$('apkCurrentMeta').textContent=d.filename+' · '+(d.size/1024/1024).toFixed(2)+' MB'+(d.notes?' · '+d.notes:'')+' · Updated '+d.updated_at;$('apkVersion').value=d.version;$('apkNotes').value=d.notes||'';$('deleteApk').disabled=false;$('saveApkInfo').disabled=false}catch(e){$('apkCurrentTitle').textContent='Could not load APK details';$('apkCurrentMeta').textContent=e.message}}

apkUpload.addEventListener('click',async()=>{
 if(!apkChosen)return;
 const version=$('apkVersion').value.trim();if(!version){showStatus(apkStatus,'Enter an app version first.','error');return}
 if(!apkChosen.name.toLowerCase().endsWith('.apk')){showStatus(apkStatus,'Please choose an .apk file.','error');return}
 apkUpload.disabled=true;apkUpload.textContent='Uploading…';clearStatus(apkStatus);$('apkProgress').style.display='block';$('apkProgressBar').style.width='0%';
 let uploadId=null;
 try{
  const start=await readJson(await fetch('/api/apk/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename:apkChosen.name,size:apkChosen.size,version,notes:$('apkNotes').value})}));
  uploadId=start.upload_id;const chunkSize=start.chunk_size;const count=start.chunk_count;
  for(let i=0;i<count;i++){
   const startAt=i*chunkSize,endAt=Math.min(apkChosen.size,startAt+chunkSize),piece=apkChosen.slice(startAt,endAt);
   const fd=new FormData();fd.append('file',piece,`part-${i}.bin`);
   const r=await fetch(`/api/apk/${uploadId}/chunk/${i}`,{method:'POST',body:fd});await readJson(r);
   const percent=Math.round(((i+1)/count)*100);$('apkProgressBar').style.width=percent+'%';$('apkProgressLabel').textContent=`Uploading part ${i+1} of ${count} · ${percent}%`;
  }
  $('apkProgressLabel').textContent='Publishing the APK…';
  const done=await readJson(await fetch('/api/apk/finish',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({upload_id:uploadId})}));
  showStatus(apkStatus,`APK published successfully. Version ${done.version}. Stable link: ${done.url}`,'ok');apkChosen=null;apkInput.value='';$('apkFilename').textContent='No APK selected';await refreshApk();
 }catch(e){showStatus(apkStatus,e.message,'error')}
 finally{apkUpload.disabled=!apkChosen;apkUpload.textContent='Upload and publish APK';$('apkProgress').style.display='none'}
});

$('saveApkInfo').addEventListener('click',async()=>{const version=$('apkVersion').value.trim();if(!version){showStatus(apkStatus,'Enter an app version.','error');return}try{const d=await readJson(await fetch('/api/apk/update-info',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version,notes:$('apkNotes').value})}));showStatus(apkStatus,'Version information updated. Current version: '+d.version,'ok');await refreshApk()}catch(e){showStatus(apkStatus,e.message,'error')}});
$('deleteApk').addEventListener('click',async()=>{if(!currentApk)return;if(!confirm('Delete the current APK release? The stable download link will stop working.'))return;try{await readJson(await fetch('/api/apk/current',{method:'DELETE'}));showStatus(apkStatus,'Current APK deleted.','ok');await refreshApk()}catch(e){showStatus(apkStatus,e.message,'error')}});
$('copyApkLink').addEventListener('click',()=>copyText(location.origin+'/app.apk',$('copyApkLink')));
$('openApkLink').addEventListener('click',()=>window.open('/app.apk','_blank','noopener'));
$('logout').addEventListener('click',async()=>{await fetch('/api/logout',{method:'POST'});location.reload()});
refreshApk();
</script></body></html>'''


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    page = DASHBOARD_HTML if request.session.get("authenticated") is True else LOGIN_HTML
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
    return f"{forwarded_proto.split(',')[0].strip()}://{host.split(',')[0].strip()}"


def is_blob_not_found(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    message = str(exc).lower()
    return status == 404 or "not found" in message or "does not exist" in message


def blob_setup_error(exc: Exception) -> HTTPException:
    message = str(exc).lower()
    if any(word in message for word in ("credential", "token", "store")):
        return HTTPException(
            status_code=503,
            detail="Vercel Blob is not configured. Connect a PUBLIC Blob store to this Vercel project so BLOB_READ_WRITE_TOKEN is provided automatically.",
        )
    return HTTPException(status_code=502, detail="Could not access Vercel Blob. Check the Blob store connection and try again.")


async def read_json_blob(pathname: str, *, missing_ok: bool = False):
    try:
        result = await blob.get_blob(pathname, access="public")
    except Exception as exc:
        if missing_ok and is_blob_not_found(exc):
            return None
        raise blob_setup_error(exc) from exc
    try:
        return json.loads(result.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="Stored manager data is invalid.") from exc


async def write_json_blob(pathname: str, value: dict, *, overwrite: bool = True) -> None:
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        await blob.put_blob(
            pathname,
            data,
            access="public",
            content_type="application/json; charset=utf-8",
            add_random_suffix=False,
            allow_overwrite=overwrite,
            cache_control_max_age=60,
        )
    except Exception as exc:
        raise blob_setup_error(exc) from exc


async def get_apk_current():
    manifest = await read_json_blob(APK_CURRENT_MANIFEST, missing_ok=True)
    if not manifest or manifest.get("deleted") is True:
        return None
    return manifest


async def get_pending_apk(upload_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", upload_id):
        raise HTTPException(status_code=400, detail="Invalid upload ID")
    state = await read_json_blob(f"{APK_TEMP_PREFIX}/{upload_id}/upload.json", missing_ok=True)
    if not state:
        raise HTTPException(status_code=404, detail="APK upload session not found. Start the upload again.")
    return state


async def cleanup_pending_apk(upload_id: str, chunk_count: int) -> None:
    delete_blob = getattr(blob, "delete_blob", None)
    if delete_blob is None:
        return
    paths = [f"{APK_TEMP_PREFIX}/{upload_id}/upload.json"]
    paths.extend(f"{APK_TEMP_PREFIX}/{upload_id}/{i:06d}.part" for i in range(chunk_count))
    try:
        await delete_blob(paths)
    except Exception:
        # The published release remains usable even if temporary-object cleanup fails.
        pass


def make_download_url(url: str, download_url: str | None = None) -> str:
    if download_url:
        return download_url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["download"] = "1"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


# ---------------------------------------------------------------------------
# Original M3U upload / serving routes. The storage method is unchanged.
# ---------------------------------------------------------------------------
@app.post("/api/upload")
async def upload_playlist(request: Request, file: UploadFile = File(...)):
    require_login(request)

    original_name = Path(file.filename or "playlist.m3u").name
    if not original_name.lower().endswith((".m3u", ".m3u8")):
        raise HTTPException(status_code=400, detail="Please upload an .m3u or .m3u8 file")

    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="The selected file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File is too large. Maximum size is 4 MB")

    try:
        decoded = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="The playlist must be a UTF-8 text file")

    first_line = next((line.strip() for line in decoded.splitlines() if line.strip()), "")
    if first_line.upper() != "#EXTM3U":
        raise HTTPException(status_code=400, detail="Invalid M3U file: the first non-empty line must be #EXTM3U")

    file_id = uuid.uuid4().hex
    pathname = f"playlists/{file_id}.m3u"

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
    except Exception as exc:
        raise blob_setup_error(exc) from exc

    site_url = f"{get_site_origin(request)}/p/{file_id}.m3u"
    return {
        "success": True,
        "filename": original_name,
        "size": len(data),
        "url": site_url,
        "storage_url": result.url,
    }


@app.get("/p/{file_id}.m3u")
async def serve_playlist(file_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", file_id):
        raise HTTPException(status_code=404, detail="Playlist not found")
    pathname = f"playlists/{file_id}.m3u"
    try:
        result = await blob.get_blob(pathname, access="public")
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


# ---------------------------------------------------------------------------
# APK publishing. A browser sends only 3 MB pieces to this Function. The
# Function stores each piece in Blob, joins them server-side, publishes one
# versioned APK object, then /app.apk redirects to its direct download URL.
# ---------------------------------------------------------------------------
@app.post("/api/apk/start")
async def start_apk_upload(payload: ApkStartPayload, request: Request):
    require_login(request)
    filename = Path(payload.filename).name
    if not filename.lower().endswith(".apk"):
        raise HTTPException(status_code=400, detail="Please choose an .apk file")
    if payload.size > MAX_APK_BYTES:
        raise HTTPException(status_code=413, detail="APK is too large. Maximum size is 150 MB")

    upload_id = uuid.uuid4().hex
    chunk_count = (payload.size + APK_CHUNK_BYTES - 1) // APK_CHUNK_BYTES
    state = {
        "upload_id": upload_id,
        "filename": filename,
        "size": payload.size,
        "version": payload.version.strip(),
        "notes": payload.notes.strip(),
        "chunk_size": APK_CHUNK_BYTES,
        "chunk_count": chunk_count,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await write_json_blob(f"{APK_TEMP_PREFIX}/{upload_id}/upload.json", state, overwrite=False)
    return {
        "success": True,
        "upload_id": upload_id,
        "chunk_size": APK_CHUNK_BYTES,
        "chunk_count": chunk_count,
    }


@app.post("/api/apk/{upload_id}/chunk/{index}")
async def upload_apk_chunk(upload_id: str, index: int, request: Request, file: UploadFile = File(...)):
    require_login(request)
    state = await get_pending_apk(upload_id)
    if index < 0 or index >= int(state["chunk_count"]):
        raise HTTPException(status_code=400, detail="Invalid APK chunk number")

    expected_size = min(
        int(state["chunk_size"]),
        int(state["size"]) - index * int(state["chunk_size"]),
    )
    data = await file.read(APK_CHUNK_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Empty APK upload chunk")
    if len(data) != expected_size:
        raise HTTPException(status_code=400, detail=f"Incorrect chunk size. Expected {expected_size} bytes")

    pathname = f"{APK_TEMP_PREFIX}/{upload_id}/{index:06d}.part"
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
    except Exception as exc:
        raise blob_setup_error(exc) from exc

    return {"success": True, "index": index, "size": len(data)}


@app.post("/api/apk/finish")
async def finish_apk_upload(payload: ApkFinishPayload, request: Request):
    require_login(request)
    state = await get_pending_apk(payload.upload_id)

    expected_size = int(state["size"])
    chunk_count = int(state["chunk_count"])
    chunk_size = int(state["chunk_size"])
    if expected_size <= 0 or expected_size > MAX_APK_BYTES:
        raise HTTPException(status_code=400, detail="Invalid APK upload size")

    # All incoming requests are small, so Vercel never receives the full APK
    # as a Function request body. The final Blob upload is server-to-Blob.
    assembled = bytearray()
    for index in range(chunk_count):
        chunk_path = f"{APK_TEMP_PREFIX}/{payload.upload_id}/{index:06d}.part"
        try:
            part = await blob.get_blob(chunk_path, access="public")
        except Exception as exc:
            if is_blob_not_found(exc):
                raise HTTPException(status_code=400, detail=f"Upload is incomplete: missing part {index + 1} of {chunk_count}") from exc
            raise blob_setup_error(exc) from exc

        part_data = part.body
        expected_part_size = min(chunk_size, expected_size - index * chunk_size)
        if len(part_data) != expected_part_size:
            raise HTTPException(status_code=400, detail=f"Upload part {index + 1} is incomplete. Please upload again.")
        assembled.extend(part_data)

    if len(assembled) != expected_size:
        raise HTTPException(status_code=400, detail="The uploaded APK size does not match the original file size")
    if not assembled.startswith(b"PK"):
        raise HTTPException(status_code=400, detail="The selected file does not look like a valid APK archive")

    release_id = uuid.uuid4().hex
    release_path = f"{APK_RELEASE_PREFIX}/{release_id}.apk"
    try:
        uploaded = await blob.put_blob(
            release_path,
            bytes(assembled),
            access="public",
            content_type="application/vnd.android.package-archive",
            add_random_suffix=False,
            allow_overwrite=False,
            cache_control_max_age=60,
        )
    except Exception as exc:
        raise blob_setup_error(exc) from exc

    download_url = make_download_url(
        uploaded.url,
        getattr(uploaded, "download_url", None),
    )
    manifest = {
        "filename": state["filename"],
        "version": state["version"],
        "notes": state.get("notes", ""),
        "size": expected_size,
        "pathname": release_path,
        "url": uploaded.url,
        "download_url": download_url,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await write_json_blob(APK_CURRENT_MANIFEST, manifest, overwrite=True)
    except Exception:
        delete_blob = getattr(blob, "delete_blob", None)
        if delete_blob is not None:
            try:
                await delete_blob(release_path)
            except Exception:
                pass
        raise

    await cleanup_pending_apk(payload.upload_id, chunk_count)
    return {
        "success": True,
        "filename": manifest["filename"],
        "version": manifest["version"],
        "size": expected_size,
        "url": f"{get_site_origin(request)}/app.apk",
        "download_url": f"{get_site_origin(request)}/app.apk",
        "updated_at": manifest["updated_at"],
    }


@app.get("/app.apk")
async def download_current_apk():
    manifest = await get_apk_current()
    if not manifest:
        raise HTTPException(status_code=404, detail="No APK has been published yet")
    download_url = manifest.get("download_url") or manifest.get("url")
    if not download_url:
        raise HTTPException(status_code=404, detail="APK download is not available")
    return RedirectResponse(
        url=download_url,
        status_code=307,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/api/app/latest")
async def latest_apk_info():
    manifest = await get_apk_current()
    if not manifest:
        raise HTTPException(status_code=404, detail="No APK has been published yet")
    return JSONResponse(
        {
            "success": True,
            "filename": manifest.get("filename", "app.apk"),
            "version": manifest.get("version", "1.0.0"),
            "notes": manifest.get("notes", ""),
            "size": int(manifest.get("size", 0)),
            "updated_at": manifest.get("updated_at", ""),
            "download_url": "/app.apk",
        },
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.post("/api/apk/update-info")
async def update_apk_info(payload: ApkUpdatePayload, request: Request):
    require_login(request)
    manifest = await get_apk_current()
    if not manifest:
        raise HTTPException(status_code=404, detail="No APK has been published yet")
    manifest["version"] = payload.version.strip()
    manifest["notes"] = payload.notes.strip()
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    await write_json_blob(APK_CURRENT_MANIFEST, manifest, overwrite=True)
    return {"success": True, "version": manifest["version"], "notes": manifest["notes"]}


@app.delete("/api/apk/current")
async def delete_current_apk(request: Request):
    require_login(request)
    manifest = await get_apk_current()
    if not manifest:
        raise HTTPException(status_code=404, detail="No APK has been published yet")

    delete_blob = getattr(blob, "delete_blob", None)
    if delete_blob is not None and manifest.get("pathname"):
        try:
            await delete_blob(manifest["pathname"])
        except Exception:
            # Retire the stable URL regardless; the stored release can be removed from Blob later.
            pass

    await write_json_blob(
        APK_CURRENT_MANIFEST,
        {"deleted": True, "deleted_at": datetime.now(timezone.utc).isoformat()},
        overwrite=True,
    )
    return {"success": True}


@app.get("/api/health")
async def health():
    return {"ok": True, "service": APP_NAME}
