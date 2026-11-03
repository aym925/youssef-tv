import hmac
import os
import re
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from vercel import blob


APP_NAME = "YOUSSEF TV"

# These defaults work without creating a .env file.
# You can still override ADMIN_PASSWORD and SESSION_SECRET in Vercel settings.
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin12345678@")
SESSION_SECRET = os.getenv("SESSION_SECRET", "XpT29QXV1up75nPpudholA9OIkW5pdqJnqiJUtXeNQSzHBs3-Y1BUwGvVTOTlPJ0")

# Vercel Blob reads this token from the Vercel project environment.
# Keep the token in Vercel's Environment Variables; do not hard-code it here.
BLOB_READ_WRITE_TOKEN = os.getenv("BLOB_READ_WRITE_TOKEN", "vercel_blob_rw_xh093eJfM9KYCHKv_BSB9CWEom8ee2dfELK85DxK5ARPBM2").strip()
MAX_UPLOAD_BYTES = 4 * 1024 * 1024  # Keep below Vercel Functions' 4.5 MB request limit.
IS_VERCEL = os.getenv("VERCEL", "").lower() in {"1", "true"}

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
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
)


class LoginPayload(BaseModel):
    password: str = Field(min_length=1, max_length=200)


LOGIN_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#090d18"><title>M3U Link Manager</title>
<style>
:root{color-scheme:dark;--bg:#090d18;--panel:#111a2b;--line:#26344b;--muted:#91a0b8;--text:#eef4ff;--blue:#4c8dff;--blue2:#2865df;--green:#40d7a0;--red:#ff7474}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(ellipse at 50% -20%,#1c3156 0,transparent 55%),var(--bg);font-family:Inter,system-ui,-apple-system,Segoe UI,Arial,sans-serif;color:var(--text);display:grid;place-items:center;padding:22px}
.card{width:min(100%,420px);background:rgba(17,26,43,.94);border:1px solid var(--line);border-radius:22px;padding:30px;box-shadow:0 24px 80px #0008}
.logo{width:54px;height:54px;border-radius:16px;background:linear-gradient(145deg,#67a3ff,#2865df);display:grid;place-items:center;font-size:25px;font-weight:900;box-shadow:0 8px 25px #2865df55}
h1{font-size:24px;margin:20px 0 7px;letter-spacing:-.5px}.sub{color:var(--muted);font-size:14px;line-height:1.6;margin:0 0 25px}
label{display:block;font-size:13px;color:#c4d0e2;margin-bottom:9px}input{width:100%;background:#0b1220;border:1px solid #30405a;border-radius:12px;padding:15px;color:var(--text);font-size:15px;outline:none}input:focus{border-color:var(--blue);box-shadow:0 0 0 3px #4c8dff22}
button{width:100%;border:0;border-radius:12px;padding:15px;background:linear-gradient(135deg,var(--blue),var(--blue2));color:white;font-weight:750;font-size:15px;margin-top:17px;cursor:pointer}button:disabled{opacity:.6;cursor:wait}.error{color:var(--red);font-size:13px;min-height:20px;margin-top:13px}.foot{color:#71819a;font-size:12px;text-align:center;margin-top:23px}
</style></head>
<body><main class="card"><div class="logo">M3U</div><h1>Welcome back</h1><p class="sub">Sign in to upload your playlist and create a direct link for your app.</p>
<form id="loginForm"><label for="password">Admin password</label><input id="password" type="password" placeholder="Enter password" autocomplete="current-password" required autofocus><button id="loginBtn" type="submit">Sign in</button><div class="error" id="error"></div></form><div class="foot">Private admin dashboard</div></main>
<script>
const form=document.getElementById('loginForm'),btn=document.getElementById('loginBtn'),err=document.getElementById('error');
form.addEventListener('submit',async e=>{e.preventDefault();err.textContent='';btn.disabled=true;btn.textContent='Checking...';try{const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('password').value})});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Login failed');location.reload()}catch(x){err.textContent=x.message;btn.disabled=false;btn.textContent='Sign in'}});
</script></body></html>'''

DASHBOARD_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#090d18"><title>M3U Link Manager</title>
<style>
:root{color-scheme:dark;--bg:#090d18;--panel:#111a2b;--line:#26344b;--muted:#91a0b8;--text:#eef4ff;--blue:#4c8dff;--blue2:#2865df;--green:#40d7a0;--red:#ff7474}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(ellipse at 50% -20%,#1c3156 0,transparent 55%),var(--bg);font-family:Inter,system-ui,-apple-system,Segoe UI,Arial,sans-serif;color:var(--text);padding:22px}
.wrap{width:min(100%,760px);margin:0 auto}.top{display:flex;align-items:center;justify-content:space-between;gap:15px;margin:12px 0 27px}.brand{display:flex;align-items:center;gap:13px}.logo{width:46px;height:46px;border-radius:14px;background:linear-gradient(145deg,#67a3ff,#2865df);display:grid;place-items:center;font-weight:900;box-shadow:0 8px 25px #2865df44}.brand strong{font-size:16px}.brand small{display:block;color:var(--muted);margin-top:3px;font-size:12px}.logout{width:auto;margin:0;padding:10px 14px;background:#18243a;border:1px solid var(--line);font-size:13px}
.panel{background:rgba(17,26,43,.94);border:1px solid var(--line);border-radius:22px;padding:28px;box-shadow:0 24px 80px #0005}.heading{font-size:24px;letter-spacing:-.5px;margin:0 0 7px}.lead{color:var(--muted);font-size:14px;line-height:1.65;margin:0 0 24px}.drop{border:1.5px dashed #405675;border-radius:17px;background:#0c1525;padding:35px 20px;text-align:center;cursor:pointer;transition:.2s}.drop.over{border-color:var(--blue);background:#142443}.fileicon{font-size:34px;margin-bottom:9px}.drop b{display:block;margin-bottom:7px}.drop span{color:var(--muted);font-size:13px}.filename{margin-top:14px;color:#bcd2f4;font-size:13px;overflow-wrap:anywhere}input[type=file]{display:none}
button{border:0;border-radius:12px;padding:14px 18px;background:linear-gradient(135deg,var(--blue),var(--blue2));color:white;font-weight:750;font-size:14px;cursor:pointer}button:disabled{opacity:.55;cursor:wait}.upload{width:100%;margin-top:17px}.hint{color:#7788a3;font-size:12px;line-height:1.6;margin:13px 2px 0}.status{display:none;margin-top:17px;padding:13px 15px;border-radius:12px;font-size:13px;line-height:1.5}.status.error{display:block;color:#ffaaaa;background:#3a1b27;border:1px solid #713245}.status.ok{display:block;color:#9bf0ce;background:#103328;border:1px solid #205b48}.result{display:none;margin-top:22px;border-top:1px solid var(--line);padding-top:22px}.result h3{font-size:15px;margin:0 0 12px}.linkbox{background:#0a1220;border:1px solid #2d405d;border-radius:12px;padding:13px;display:flex;align-items:center;gap:10px}.linktext{font-size:13px;color:#bcd2f4;overflow-wrap:anywhere;flex:1;min-width:0;line-height:1.5}.copy{width:auto;flex-shrink:0;margin:0;padding:10px 13px}.meta{color:var(--muted);font-size:12px;margin-top:10px}.bottom{color:#71819a;text-align:center;font-size:12px;margin:20px 0}.pill{display:inline-block;color:#95baff;background:#142746;padding:5px 9px;border-radius:30px;font-size:11px;margin-left:7px}
@media(max-width:520px){body{padding:14px}.panel{padding:21px}.heading{font-size:21px}.top{margin-top:4px}}
</style></head>
<body><main class="wrap"><header class="top"><div class="brand"><div class="logo">M3U</div><div><strong>M3U Link Manager</strong><small>Playlist hosting dashboard</small></div></div><button class="logout" id="logout">Log out</button></header>
<section class="panel"><h1 class="heading">Upload your playlist</h1><p class="lead">Choose an M3U file. We will store it and create a link on this website that you can add to your app.</p>
<div class="drop" id="drop"><div class="fileicon">📄</div><b>Drop your M3U file here</b><span>or tap to browse · .m3u / .m3u8 · up to 4 MB</span><div class="filename" id="filename">No file selected</div></div><input id="file" type="file" accept=".m3u,.m3u8,audio/x-mpegurl,application/vnd.apple.mpegurl">
<button class="upload" id="upload" disabled>Upload and create link</button><div class="status" id="status"></div>
<div class="result" id="result"><h3>✅ Your playlist link is ready</h3><div class="linkbox"><div class="linktext" id="url"></div><button class="copy" id="copy">Copy</button></div><div class="meta" id="meta"></div></div>
<p class="hint">The generated link is public to anyone who has it. Do not upload private playlists or links you are not allowed to share.</p></section><div class="bottom">Secure admin area · M3U Link Manager</div></main>
<script>
const input=document.getElementById('file'),drop=document.getElementById('drop'),upload=document.getElementById('upload'),filename=document.getElementById('filename'),statusBox=document.getElementById('status'),result=document.getElementById('result');let chosen=null;
function setFile(f){if(!f)return;chosen=f;filename.textContent=f.name+' · '+(f.size/1024).toFixed(1)+' KB';upload.disabled=false;statusBox.className='status';statusBox.textContent='';result.style.display='none'}
drop.addEventListener('click',()=>input.click());input.addEventListener('change',()=>setFile(input.files[0]));drop.addEventListener('dragover',e=>{e.preventDefault();drop.classList.add('over')});drop.addEventListener('dragleave',()=>drop.classList.remove('over'));drop.addEventListener('drop',e=>{e.preventDefault();drop.classList.remove('over');setFile(e.dataTransfer.files[0])});
upload.addEventListener('click',async()=>{if(!chosen)return;upload.disabled=true;upload.textContent='Uploading...';statusBox.className='status';statusBox.textContent='';result.style.display='none';try{const fd=new FormData();fd.append('file',chosen);const r=await fetch('/api/upload',{method:'POST',body:fd});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Upload failed');document.getElementById('url').textContent=d.url;document.getElementById('meta').textContent=d.filename+' · '+(d.size/1024).toFixed(1)+' KB';result.style.display='block';statusBox.className='status ok';statusBox.textContent='Upload completed successfully.'}catch(e){statusBox.className='status error';statusBox.textContent=e.message}finally{upload.disabled=!chosen;upload.textContent='Upload and create link'}});
document.getElementById('copy').addEventListener('click',async()=>{const u=document.getElementById('url').textContent;try{await navigator.clipboard.writeText(u);document.getElementById('copy').textContent='Copied!';setTimeout(()=>document.getElementById('copy').textContent='Copy',1500)}catch(e){const t=document.createElement('textarea');t.value=u;document.body.appendChild(t);t.select();document.execCommand('copy');t.remove();document.getElementById('copy').textContent='Copied!'}});
document.getElementById('logout').addEventListener('click',async()=>{await fetch('/api/logout',{method:'POST'});location.reload()});
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
    # Use the incoming host only to construct the visible link; never use it for storage paths.
    return f"{forwarded_proto.split(',')[0].strip()}://{host.split(',')[0].strip()}"


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
        message = str(exc).lower()
        if "credential" in message or "token" in message or "store" in message:
            raise HTTPException(
                status_code=503,
                detail="Vercel Blob is not configured. Create a PUBLIC Blob store and connect it to this Vercel project.",
            )
        raise HTTPException(status_code=502, detail="Could not save the playlist to Vercel Blob. Check the Blob store settings.")

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


@app.get("/api/health")
async def health():
    return {"ok": True, "service": APP_NAME}
