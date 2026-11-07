import os
import uuid
import json
import math
import re
import subprocess
import shutil
import platform
import threading
import time
import hmac
import hashlib
import base64
from datetime import datetime, timezone
import boto3
from pathlib import Path
from fastapi import FastAPI, BackgroundTasks, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel
import yt_dlp
from groq import Groq
import requests
from dotenv import load_dotenv
from typing import Optional
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from supabase import create_client, Client

load_dotenv()

# ffmpeg must be installed (Dockerfile handles this)
if shutil.which("ffmpeg") is None:
    print("[Setup] ⚠️ ffmpeg not found in PATH — install it (see Dockerfile)")

# Cookies setup (optional, from YT_COOKIES env var)
yt_cookies_content = os.getenv("YT_COOKIES")
COOKIES_PATH = Path(__file__).parent / "cookies.txt"
if yt_cookies_content:
    COOKIES_PATH.write_text(yt_cookies_content)
    print(f"[Cookie] cookies.txt write ho gaya ✅ | Path: {COOKIES_PATH} | Exists: {COOKIES_PATH.exists()}")
else:
    print("[Cookie] ⚠️ YT_COOKIES env variable nahi mila — bot detection ho sakta hai")

app = FastAPI(title="ClipAI Backend")

BASE_DIR   = Path(__file__).parent
TEMP_DIR   = BASE_DIR / "temp"
OUTPUT_DIR = BASE_DIR / "outputs"
TEMP_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

GROQ_API_KEY         = os.getenv("GROQ_API_KEY")
OPENROUTER_KEY       = os.getenv("OPENROUTER_KEY")
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
SUPABASE_URL         = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
B2_KEY_ID            = os.getenv("B2_KEY_ID")
B2_APP_KEY           = os.getenv("B2_APP_KEY")
B2_BUCKET_NAME       = os.getenv("B2_BUCKET_NAME", "clipai-clips")
B2_ENDPOINT          = os.getenv("B2_ENDPOINT", "https://s3.us-west-004.backblazeb2.com")

# Zoom applied to the main video after 16:9 -> 9:16 conversion (1.0 = no zoom)
try: ZOOM_FACTOR = max(1.0, float(os.getenv("ZOOM_FACTOR", "1.15")))
except ValueError: ZOOM_FACTOR = 1.15

# 9:16 background: dark (clean, default) | crop (video fills full screen) | blur (dimmed blur)
BG_STYLE  = os.getenv("BG_STYLE", "dark").strip().lower()
BG_FILTER = "boxblur=25:5,eq=brightness=-0.3" if BG_STYLE == "blur" else "drawbox=x=0:y=0:w=iw:h=ih:color=0x0d0d12:t=fill"
FIT_MODE  = "increase" if BG_STYLE == "crop" else "decrease"

# Public URL of THIS backend (no trailing slash) and of the frontend
BACKEND_URL  = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")
REDIRECT_URI = f"{BACKEND_URL}/auth/youtube/callback"

_origins = [FRONTEND_URL, "http://localhost:3000"] + [
    o.strip().rstrip("/") for o in os.getenv("EXTRA_CORS_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_origin_regex=r"https://.*\.vercel\.app",
                   allow_methods=["*"], allow_headers=["*"])

MAX_CLIPS      = 6
WATERMARK_TEXT = "ClipAI"

if not GROQ_API_KEY:    raise RuntimeError("GROQ_API_KEY missing!")
if not OPENROUTER_KEY:  raise RuntimeError("OPENROUTER_KEY missing!")
if not SUPABASE_URL:    raise RuntimeError("SUPABASE_URL missing!")
if not SUPABASE_SERVICE_KEY: raise RuntimeError("SUPABASE_SERVICE_KEY missing!")

groq_client = Groq(api_key=GROQ_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# Supabase HTTP/2 "Server disconnected" fix: purani connection tootne par ek naya try
import httpx
_orig_send = httpx.Client.send
def _send_retry(self, request, *args, **kwargs):
    for i in range(3):
        try:
            return _orig_send(self, request, *args, **kwargs)
        except (httpx.RemoteProtocolError, httpx.ReadError):
            if "supabase" not in str(request.url.host) or i == 2: raise
            time.sleep(0.3)
httpx.Client.send = _send_retry
b2 = boto3.client("s3", endpoint_url=B2_ENDPOINT, aws_access_key_id=B2_KEY_ID, aws_secret_access_key=B2_APP_KEY)

YT_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
jobs: dict = {}

LANG_MAP   = {"hindi": "hi", "hinglish": "hi", "bengali": "bn", "english": "en"}
LENGTH_MAP = {"30-60": (30,60,48), "60-90": (60,90,75), "90-150": (90,150,120)}

PIPED_SERVERS = [
    "https://pipedapi.kavin.rocks",
    "https://piped-api.garudalinux.org",
    "https://api.piped.projectsegfau.lt",
    "https://pipedapi.coldforge.xyz",
]

INVIDIOUS_SERVERS = [
    "https://invidious.kavin.rocks",
    "https://yewtu.be",
    "https://inv.nadeko.net",
    "https://invidious.nerdvpn.de",
]

def get_font_path() -> str:
    if platform.system() == "Windows":
        for f in ["C\\:/Windows/Fonts/arial.ttf", "C\\:/Windows/Fonts/calibri.ttf"]:
            if Path(f.replace("C\\:/", "C:/")).exists(): return f
        return "C\\:/Windows/Fonts/arial.ttf"
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for f in font_paths:
        if Path(f).exists():
            print(f"[Font] Found: {f}")
            return f
    try:
        result = subprocess.run(["fc-list", ":spacing=proportional", "--format=%{file}\n"],
                                capture_output=True, text=True)
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line and Path(line).exists():
                print(f"[Font] fc-list found: {line}")
                return line
    except Exception:
        pass
    print("[Font] ⚠️ Koi font nahi mila — captions nahi dikhenge")
    return ""

FONT_PATH = get_font_path()

class ClipRequest(BaseModel):
    youtube_url: str
    language:    str
    clip_length: str

class UploadRequest(BaseModel):
    b2_key:      str
    clip_db_id:  str
    title:       str
    description: str
    tags:        Optional[list[str]] = []
    keep_clip:   bool = True
    platforms:   Optional[list[str]] = None

class GenerateTitleRequest(BaseModel):
    clip_index: int
    job_id:     str

# ── Auth ──────────────────────────────────────────────────────
def get_current_user(authorization: str = None):
    if not authorization or not authorization.startswith("Bearer "): return None
    try:
        return supabase.auth.get_user(authorization.replace("Bearer ", "")).user
    except: return None

def require_user(authorization: str = None):
    user = get_current_user(authorization)
    if not user: raise HTTPException(status_code=401, detail="Login karo pehle!")
    return user

# ── B2 ────────────────────────────────────────────────────────
def upload_to_b2(local_path: str, b2_key: str) -> str:
    b2.upload_file(Filename=local_path, Bucket=B2_BUCKET_NAME, Key=b2_key)
    return b2_key

def delete_from_b2(b2_key: str):
    try: b2.delete_object(Bucket=B2_BUCKET_NAME, Key=b2_key)
    except Exception as e: print(f"[B2] Delete failed: {e}")

def get_signed_url(b2_key: str, expires: int = 7200) -> str:
    return b2.generate_presigned_url("get_object", Params={"Bucket": B2_BUCKET_NAME, "Key": b2_key}, ExpiresIn=expires)

def delete_all_user_clips(user_id: str):
    try:
        result = supabase.table("clips").select("*").eq("user_id", user_id).execute()
        for clip in result.data:
            if clip.get("b2_key"):
                delete_from_b2(clip["b2_key"])
        supabase.table("clips").delete().eq("user_id", user_id).execute()
        print(f"[ClipAI] Session cleanup done for user: {user_id} — {len(result.data)} clips deleted")
    except Exception as e:
        print(f"[ClipAI] Session cleanup failed for {user_id}: {e}")

# ── YouTube tokens ─────────────────────────────────────────────
def sign_state(user_id: str) -> str:
    sig = hmac.new(SUPABASE_SERVICE_KEY.encode(), user_id.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{user_id}.{sig}".encode()).decode()

def verify_state(state: str) -> Optional[str]:
    try:
        user_id, sig = base64.urlsafe_b64decode(state.encode()).decode().rsplit(".", 1)
        good = hmac.new(SUPABASE_SERVICE_KEY.encode(), user_id.encode(), hashlib.sha256).hexdigest()[:32]
        return user_id if hmac.compare_digest(sig, good) else None
    except Exception:
        return None

def save_yt_tokens(user_id: str, token: str, refresh_token: str, scopes: list):
    supabase.table("yt_tokens").upsert({
        "user_id": user_id, "token": token, "refresh_token": refresh_token,
        "scopes": scopes, "updated_at": datetime.now(timezone.utc).isoformat()
    }).execute()

def load_yt_tokens(user_id: str) -> dict | None:
    result = supabase.table("yt_tokens").select("*").eq("user_id", user_id).execute()
    if result.data: return result.data[0]
    return None

def delete_yt_tokens(user_id: str):
    supabase.table("yt_tokens").delete().eq("user_id", user_id).execute()

def get_yt_credentials(user_id: str) -> Credentials | None:
    t = load_yt_tokens(user_id)
    if not t or not t.get("refresh_token"): return None
    return Credentials(
        token=t.get("token"), refresh_token=t.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID, client_secret=GOOGLE_CLIENT_SECRET,
        scopes=t.get("scopes") or YT_SCOPES
    )

def _make_flow():
    return Flow.from_client_config({
        "web": {
            "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI]
        }
    }, scopes=YT_SCOPES, redirect_uri=REDIRECT_URI, autogenerate_code_verifier=False)

# ── Routes ─────────────────────────────────────────────────────
@app.get("/")
@app.head("/")
def root(): return {"status": "ClipAI backend running ✅"}

@app.post("/create-clips")
def create_clips(req: ClipRequest, background_tasks: BackgroundTasks, authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    existing = supabase.table("clips").select("id").eq("user_id", user.id).execute()
    if len(existing.data) >= MAX_CLIPS:
        raise HTTPException(status_code=429, detail=f"Max {MAX_CLIPS} clips! Pehle kuch delete ya upload karo.")
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "queued", "step": "Starting...", "progress": 0, "clips": [], "error": None, "user_id": user.id}
    background_tasks.add_task(process_video, job_id, req, user.id)
    return {"job_id": job_id}

@app.get("/status/{job_id}")
def get_status(job_id: str, authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    if job_id not in jobs or jobs[job_id].get("user_id") != user.id: raise HTTPException(404, "Job not found")
    return jobs[job_id]

@app.get("/my-clips")
def get_my_clips(authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    result = supabase.table("clips").select("*").eq("user_id", user.id).order("created_at", desc=True).execute()
    clips = []
    for c in result.data:
        if c.get("b2_key"):
            try: c["url"] = get_signed_url(c["b2_key"])
            except: pass
        clips.append(c)
    return {"clips": clips, "count": len(clips), "max": MAX_CLIPS}

@app.delete("/my-clips/all")
def delete_all_my_clips(authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    delete_all_user_clips(user.id)
    return {"deleted": True}

@app.post("/my-clips/all-delete")
async def delete_all_my_clips_beacon(request: Request):
    try:
        body = await request.json()
        token = body.get("token")
        if not token: return {"ok": False}
        user = get_current_user(f"Bearer {token}")
        if not user: return {"ok": False}
        threading.Thread(target=delete_all_user_clips, args=(user.id,), daemon=True).start()
        return {"ok": True}
    except Exception as e:
        print(f"[Beacon] Error: {e}")
        return {"ok": False}

@app.delete("/clip/{clip_id}")
def delete_clip(clip_id: str, authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    clip = supabase.table("clips").select("*").eq("id", clip_id).eq("user_id", user.id).execute()
    if not clip.data: raise HTTPException(404, "Clip nahi mili!")
    if clip.data[0].get("b2_key"): delete_from_b2(clip.data[0]["b2_key"])
    supabase.table("clips").delete().eq("id", clip_id).execute()
    return {"deleted": True}

# ── YouTube Auth ───────────────────────────────────────────────
@app.get("/auth/youtube/url")
def get_youtube_auth_url(authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    flow = _make_flow()
    flow.code_challenge_method = None
    auth_url, _ = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent", state=sign_state(user.id))
    return {"url": auth_url}

@app.get("/auth/youtube/callback")
def auth_youtube_callback(request: Request, code: str = None, error: str = None, state: str = None):
    if error or not code:
        return RedirectResponse(f"{FRONTEND_URL}?yt_error={error or 'no_code'}")
    user_id = verify_state(state) if state else None
    if not user_id:
        return RedirectResponse(f"{FRONTEND_URL}?yt_error=bad_state")
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    flow = _make_flow()
    flow.code_challenge_method = None
    try:
        flow.fetch_token(code=code)
    except Exception as e:
        import traceback
        print(f"[YouTube OAuth Error] {e}")
        print(traceback.format_exc())
        return RedirectResponse(f"{FRONTEND_URL}?yt_error=token_failed")
    creds = flow.credentials
    save_yt_tokens(user_id=user_id, token=creds.token, refresh_token=creds.refresh_token,
                   scopes=list(creds.scopes) if creds.scopes else YT_SCOPES)
    print(f"[ClipAI] YouTube token saved in DB for user: {user_id}")
    return RedirectResponse(f"{FRONTEND_URL}?yt_connected=1")

@app.get("/auth/youtube/status")
def auth_youtube_status(authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    creds = get_yt_credentials(user.id)
    return {"connected": bool(creds and creds.refresh_token)}

@app.delete("/auth/youtube/disconnect")
def auth_youtube_disconnect(authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    delete_yt_tokens(user.id)
    return {"disconnected": True}

@app.get("/auth/youtube/channel")
def get_yt_channel(authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    creds = get_yt_credentials(user.id)
    if not creds: return {"name": None, "logo": None}
    try:
        youtube = build("youtube", "v3", credentials=creds)
        ch = youtube.channels().list(part="snippet", mine=True).execute()
        item = ch["items"][0]["snippet"]
        return {"name": item["title"], "logo": item["thumbnails"]["default"]["url"]}
    except Exception as e:
        print(f"[Channel Error] {e}")
        return {"name": None, "logo": None}

# ── Title Generation ───────────────────────────────────────────
@app.post("/generate-title")
def generate_title(req: GenerateTitleRequest, authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    job = jobs.get(req.job_id)
    if not job or job.get("user_id") != user.id: raise HTTPException(404, "Job not found")
    moments = job.get("moments", [])
    if req.clip_index >= len(moments): raise HTTPException(404, "Clip not found")
    moment = moments[req.clip_index]
    prompt = f"""Viral YouTube Shorts title expert for Indian creators.
Clip info: Caption: {moment.get('caption','')} | Why viral: {moment.get('reason','')}
Generate ONE viral title (max 60 chars, Hindi/Hinglish/English + 1-2 emojis) and ONE description (max 120 chars + hashtags).
Return ONLY raw JSON: {{"title":"...","description":"..."}}"""
    response = requests.post("https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json", "HTTP-Referer": "https://clipai.app"},
        json={"model": "deepseek/deepseek-chat", "messages": [{"role": "user", "content": prompt}], "temperature": 0.8}, timeout=30)
    if response.status_code != 200: raise HTTPException(500, f"DeepSeek error: {response.text}")
    raw = response.json()["choices"][0]["message"]["content"].strip().replace("```json","").replace("```","").strip()
    try:
        data = json.loads(raw)
        return {"title": data.get("title",""), "description": data.get("description","")}
    except: raise HTTPException(500, "AI ne invalid response diya")

# ---- Facebook (Page Reels) ----
FB_APP_ID     = os.getenv("FACEBOOK_APP_ID")
FB_APP_SECRET = os.getenv("FACEBOOK_APP_SECRET")
FB_VER        = "v25.0"
FB_REDIRECT   = f"{BACKEND_URL}/auth/facebook/callback"
FB_SCOPES     = "pages_show_list,pages_read_engagement,pages_manage_posts"

def load_fb(user_id: str):
    r = supabase.table("fb_tokens").select("*").eq("user_id", user_id).execute()
    return r.data[0] if r.data else None

@app.get("/auth/facebook/url")
def fb_auth_url(authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    if not FB_APP_ID: raise HTTPException(500, "FACEBOOK_APP_ID .env me nahi hai")
    from urllib.parse import urlencode
    q = urlencode({"client_id": FB_APP_ID, "redirect_uri": FB_REDIRECT, "state": sign_state(user.id),
                   "scope": FB_SCOPES, "response_type": "code"})
    return {"url": f"https://www.facebook.com/{FB_VER}/dialog/oauth?{q}"}

@app.get("/auth/facebook/callback")
def fb_callback(code: str = None, error: str = None, state: str = None):
    user_id = verify_state(state) if state else None
    if error or not code or not user_id:
        return RedirectResponse(f"{FRONTEND_URL}?fb_error={error or ('bad_state' if not user_id else 'no_code')}")
    try:
        g = f"https://graph.facebook.com/{FB_VER}"
        a = requests.get(f"{g}/oauth/access_token", timeout=30, params={"client_id": FB_APP_ID,
            "client_secret": FB_APP_SECRET, "redirect_uri": FB_REDIRECT, "code": code}).json()
        if "access_token" not in a: raise Exception(a)
        b = requests.get(f"{g}/oauth/access_token", timeout=30, params={"grant_type": "fb_exchange_token",
            "client_id": FB_APP_ID, "client_secret": FB_APP_SECRET, "fb_exchange_token": a["access_token"]}).json()
        if "access_token" not in b: raise Exception(b)
        pages = requests.get(f"{g}/me/accounts", timeout=30, params={"access_token": b["access_token"],
            "fields": "id,name,access_token,tasks,picture{url}"}).json().get("data", [])
        if not pages: return RedirectResponse(f"{FRONTEND_URL}?fb_error=no_page_found")
        pg = next((p for p in pages if "CREATE_CONTENT" in (p.get("tasks") or [])), pages[0])
        supabase.table("fb_tokens").upsert({"user_id": user_id, "page_id": pg["id"], "page_name": pg.get("name"),
            "page_picture": ((pg.get("picture") or {}).get("data") or {}).get("url"),
            "page_token": pg["access_token"], "updated_at": datetime.now(timezone.utc).isoformat()}).execute()
    except Exception as e:
        print(f"[Facebook OAuth Error] {e}")
        return RedirectResponse(f"{FRONTEND_URL}?fb_error=token_failed")
    return RedirectResponse(f"{FRONTEND_URL}?fb_connected=1")

@app.get("/auth/facebook/status")
def fb_status(authorization: Optional[str] = Header(None)):
    fb = load_fb(require_user(authorization).id)
    return {"connected": bool(fb), "name": fb and fb.get("page_name"), "logo": fb and fb.get("page_picture")}

@app.delete("/auth/facebook/disconnect")
def fb_disconnect(authorization: Optional[str] = Header(None)):
    supabase.table("fb_tokens").delete().eq("user_id", require_user(authorization).id).execute()
    return {"disconnected": True}

@app.post("/upload/facebook")
def upload_to_facebook_route(req: UploadRequest, authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    fb = load_fb(user.id)
    if not fb: raise HTTPException(401, "Facebook connect karo pehle!")
    temp_path = str(TEMP_DIR / f"fbup_{uuid.uuid4()}.mp4")
    try: b2.download_file(Bucket=B2_BUCKET_NAME, Key=req.b2_key, Filename=temp_path)
    except Exception as e: raise HTTPException(404, f"Clip nahi mili B2 pe: {e}")
    try:
        g, tok = f"https://graph.facebook.com/{FB_VER}/{fb['page_id']}/video_reels", fb["page_token"]
        s = requests.post(g, data={"upload_phase": "start", "access_token": tok}, timeout=60).json()
        if "video_id" not in s: raise Exception(s)
        with open(temp_path, "rb") as f:
            u = requests.post(s["upload_url"], data=f, timeout=900, headers={"Authorization": f"OAuth {tok}",
                "offset": "0", "file_size": str(os.path.getsize(temp_path))}).json()
        if not u.get("success"): raise Exception(u)
        d = requests.post(g, timeout=60, data={"upload_phase": "finish", "video_id": s["video_id"],
            "video_state": "PUBLISHED", "title": req.title[:100],
            "description": (req.title.strip() + "\n\n" + req.description.strip())[:2000], "access_token": tok}).json()
        if not d.get("success"): raise Exception(d)
        fb_wait_ready(s["video_id"], tok)
        _gone = finish_clip(user.id, req, "facebook")
        if not req.keep_clip:
            delete_from_b2(req.b2_key)
            try: supabase.table("clips").delete().eq("id", req.clip_db_id).execute()
            except Exception as e: print(f"[Upload] DB delete failed: {e}")
        return {"success": True, "deleted": _gone, "video_id": s["video_id"], "url": f"https://www.facebook.com/reel/{s['video_id']}"}
    except Exception as e:
        raise HTTPException(500, f"Facebook upload fail: {e} (Reel 3-60 sec ki honi chahiye)")
    finally:
        try: Path(temp_path).unlink()
        except Exception: pass

# ---- Facebook (Page Reels) ----
FB_APP_ID     = os.getenv("FACEBOOK_APP_ID")
FB_APP_SECRET = os.getenv("FACEBOOK_APP_SECRET")
FB_VER        = "v25.0"
FB_REDIRECT   = f"{BACKEND_URL}/auth/facebook/callback"
FB_SCOPES     = "pages_show_list,pages_read_engagement,pages_manage_posts"

def load_fb(user_id: str):
    r = supabase.table("fb_tokens").select("*").eq("user_id", user_id).execute()
    return r.data[0] if r.data else None

@app.get("/auth/facebook/url")
def fb_auth_url(authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    if not FB_APP_ID: raise HTTPException(500, "FACEBOOK_APP_ID .env me nahi hai")
    from urllib.parse import urlencode
    q = urlencode({"client_id": FB_APP_ID, "redirect_uri": FB_REDIRECT, "state": sign_state(user.id),
                   "scope": FB_SCOPES, "response_type": "code"})
    return {"url": f"https://www.facebook.com/{FB_VER}/dialog/oauth?{q}"}

@app.get("/auth/facebook/callback")
def fb_callback(code: str = None, error: str = None, state: str = None):
    user_id = verify_state(state) if state else None
    if error or not code or not user_id:
        return RedirectResponse(f"{FRONTEND_URL}?fb_error={error or ('bad_state' if not user_id else 'no_code')}")
    try:
        g = f"https://graph.facebook.com/{FB_VER}"
        a = requests.get(f"{g}/oauth/access_token", timeout=30, params={"client_id": FB_APP_ID,
            "client_secret": FB_APP_SECRET, "redirect_uri": FB_REDIRECT, "code": code}).json()
        if "access_token" not in a: raise Exception(a)
        b = requests.get(f"{g}/oauth/access_token", timeout=30, params={"grant_type": "fb_exchange_token",
            "client_id": FB_APP_ID, "client_secret": FB_APP_SECRET, "fb_exchange_token": a["access_token"]}).json()
        if "access_token" not in b: raise Exception(b)
        pages = requests.get(f"{g}/me/accounts", timeout=30, params={"access_token": b["access_token"],
            "fields": "id,name,access_token,tasks,picture{url}"}).json().get("data", [])
        if not pages: return RedirectResponse(f"{FRONTEND_URL}?fb_error=no_page_found")
        pg = next((p for p in pages if "CREATE_CONTENT" in (p.get("tasks") or [])), pages[0])
        supabase.table("fb_tokens").upsert({"user_id": user_id, "page_id": pg["id"], "page_name": pg.get("name"),
            "page_picture": ((pg.get("picture") or {}).get("data") or {}).get("url"),
            "page_token": pg["access_token"], "updated_at": datetime.now(timezone.utc).isoformat()}).execute()
    except Exception as e:
        print(f"[Facebook OAuth Error] {e}")
        return RedirectResponse(f"{FRONTEND_URL}?fb_error=token_failed")
    return RedirectResponse(f"{FRONTEND_URL}?fb_connected=1")

@app.get("/auth/facebook/status")
def fb_status(authorization: Optional[str] = Header(None)):
    fb = load_fb(require_user(authorization).id)
    return {"connected": bool(fb), "name": fb and fb.get("page_name"), "logo": fb and fb.get("page_picture")}

@app.delete("/auth/facebook/disconnect")
def fb_disconnect(authorization: Optional[str] = Header(None)):
    supabase.table("fb_tokens").delete().eq("user_id", require_user(authorization).id).execute()
    return {"disconnected": True}

@app.post("/upload/facebook")
def upload_to_facebook_route(req: UploadRequest, authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    fb = load_fb(user.id)
    if not fb: raise HTTPException(401, "Facebook connect karo pehle!")
    temp_path = str(TEMP_DIR / f"fbup_{uuid.uuid4()}.mp4")
    try: b2.download_file(Bucket=B2_BUCKET_NAME, Key=req.b2_key, Filename=temp_path)
    except Exception as e: raise HTTPException(404, f"Clip nahi mili B2 pe: {e}")
    try:
        g, tok = f"https://graph.facebook.com/{FB_VER}/{fb['page_id']}/video_reels", fb["page_token"]
        s = requests.post(g, data={"upload_phase": "start", "access_token": tok}, timeout=60).json()
        if "video_id" not in s: raise Exception(s)
        with open(temp_path, "rb") as f:
            u = requests.post(s["upload_url"], data=f, timeout=900, headers={"Authorization": f"OAuth {tok}",
                "offset": "0", "file_size": str(os.path.getsize(temp_path))}).json()
        if not u.get("success"): raise Exception(u)
        d = requests.post(g, timeout=60, data={"upload_phase": "finish", "video_id": s["video_id"],
            "video_state": "PUBLISHED", "title": req.title[:100],
            "description": (req.title.strip() + "\n\n" + req.description.strip())[:2000], "access_token": tok}).json()
        if not d.get("success"): raise Exception(d)
        return {"success": True, "video_id": s["video_id"], "url": f"https://www.facebook.com/reel/{s['video_id']}"}
    except Exception as e:
        raise HTTPException(500, f"Facebook upload fail: {e} (Reel 3-60 sec ki honi chahiye)")
    finally:
        try: Path(temp_path).unlink()
        except Exception: pass

def fb_wait_ready(video_id, tok, tries=20):
    """Facebook Reel process hone tak ruko; error ho to batao."""
    for _ in range(tries):
        try:
            st = requests.get(f"https://graph.facebook.com/{FB_VER}/{video_id}", timeout=30,
                              params={"fields": "status", "access_token": tok}).json().get("status") or {}
        except Exception:
            return
        if st.get("video_status") == "ready": return
        if st.get("video_status") == "error" or any(isinstance(v, dict) and v.get("status") == "error" for v in st.values()):
            raise Exception(f"Facebook ne video reject ki: {st}")
        time.sleep(3)

@app.post("/upload/all")
def upload_to_all_route(req: UploadRequest, authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    targets = []
    if load_fb(user.id): targets.append(("facebook", upload_to_facebook_route))
    if get_yt_credentials(user.id): targets.append(("youtube", upload_to_youtube_route))
    if req.platforms is not None: targets = [t for t in targets if t[0] in req.platforms]
    if not targets: raise HTTPException(401, "Koi platform connected nahi hai!")
    req.keep_clip = True
    results, ok_all = {}, True
    for name, fn in targets:
        try: results[name] = fn(req, authorization)
        except HTTPException as e: ok_all = False; results[name] = {"success": False, "error": str(e.detail)}
        except Exception as e: ok_all = False; results[name] = {"success": False, "error": str(e)}
    if False:
        delete_from_b2(req.b2_key)
        try: supabase.table("clips").delete().eq("id", req.clip_db_id).execute()
        except Exception as e: print(f"[Upload] DB delete failed: {e}")
    return {"success": ok_all, "deleted": any(isinstance(r, dict) and r.get("deleted") for r in results.values()), "results": results}

def finish_clip(user_id, req, platform):
    """Platform ko 'uploaded' mark karo; clip tabhi delete jab sab connected platform par ho jaye."""
    try:
        r = supabase.table("clips").select("uploaded_to").eq("id", req.clip_db_id).execute()
        done = list((r.data[0].get("uploaded_to") if r.data else None) or [])
        if platform not in done: done.append(platform)
        connected = [p for p, ok in (("youtube", bool(get_yt_credentials(user_id))), ("facebook", bool(load_fb(user_id)))) if ok]
        if all(p in done for p in connected):
            delete_from_b2(req.b2_key)
            supabase.table("clips").delete().eq("id", req.clip_db_id).execute()
            return True
        supabase.table("clips").update({"uploaded_to": done}).eq("id", req.clip_db_id).execute()
    except Exception as e:
        print(f"[finish_clip] {e}")
    return False

class GenerateTitleFromCaptionRequest(BaseModel):
    caption: str
    reason:  str = ""

@app.post("/generate-title-from-caption")
def generate_title_from_caption(req: GenerateTitleFromCaptionRequest, authorization: Optional[str] = Header(None)):
    require_user(authorization)
    prompt = f"""Viral YouTube Shorts title expert for Indian creators.
Clip info: Caption: {req.caption} | Why viral: {req.reason}
Generate ONE viral title (max 60 chars, Hindi/Hinglish/English + 1-2 emojis) and ONE description (max 120 chars + hashtags).
Return ONLY raw JSON: {{"title":"...","description":"..."}}"""
    response = requests.post("https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
        json={"model": "deepseek/deepseek-chat", "messages": [{"role": "user", "content": prompt}], "temperature": 0.8}, timeout=30)
    raw = response.json()["choices"][0]["message"]["content"].strip().replace("```json","").replace("```","").strip()
    try:
        data = json.loads(raw)
        return {"title": data.get("title",""), "description": data.get("description","")}
    except: raise HTTPException(500, "AI ne invalid response diya")

# ── YouTube Upload ─────────────────────────────────────────────
@app.post("/upload/youtube")
def upload_to_youtube_route(req: UploadRequest, authorization: Optional[str] = Header(None)):
    user  = require_user(authorization)
    creds = get_yt_credentials(user.id)
    if not creds: raise HTTPException(401, "YouTube connect karo pehle!")
    temp_path = str(TEMP_DIR / f"upload_{uuid.uuid4()}.mp4")
    try:
        b2.download_file(Bucket=B2_BUCKET_NAME, Key=req.b2_key, Filename=temp_path)
    except Exception as e:
        raise HTTPException(404, f"Clip nahi mili B2 pe: {e}")
    media = None
    try:
        youtube = build("youtube", "v3", credentials=creds)
        body = {
            "snippet": {"title": req.title[:100], "description": req.description[:5000],
                        "tags": req.tags or [], "categoryId": "22"},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}
        }
        media = MediaFileUpload(temp_path, mimetype="video/mp4", resumable=True, chunksize=5*1024*1024)
        ins = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        resp = None
        while resp is None: _, resp = ins.next_chunk()
        try: media._fd.close()
        except: pass
        video_id = resp.get("id")
        _gone = finish_clip(user.id, req, "youtube")
        try: pass
        except Exception as e: print(f"[Upload] DB delete failed: {e}")
        print(f"[ClipAI] Uploaded to youtube | deleted={_gone}: {req.b2_key}")
        return {"success": True, "deleted": _gone, "video_id": video_id, "url": f"https://youtube.com/shorts/{video_id}"}
    except Exception as e:
        if media:
            try: media._fd.close()
            except: pass
        raise HTTPException(500, str(e))
    finally:
        for attempt in range(5):
            try:
                if Path(temp_path).exists(): Path(temp_path).unlink()
                break
            except PermissionError:
                time.sleep(0.5)

# ── Video Processing ───────────────────────────────────────────
def update(job_id, step, progress=0, status="processing"):
    jobs[job_id].update({"step": step, "status": status, "progress": progress})
    print(f"[{job_id[:8]}] ({progress}%) {step}")

def process_video(job_id: str, req: ClipRequest, user_id: str):
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(exist_ok=True)
    try:
        update(job_id, "Downloading video...", 5)
        video_path, audio_path = download_full_video(req.youtube_url, job_dir)

        update(job_id, "Transcribing with Groq Whisper...", 25)
        lang_code  = LANG_MAP.get(req.language, "hi")
        transcript = transcribe_groq(audio_path, lang_code)

        update(job_id, "AI detecting viral moments...", 45)
        min_sec, max_sec, target_sec = LENGTH_MAP.get(req.clip_length, (30, 60, 48))
        moments = detect_viral_moments(transcript, min_sec, max_sec, target_sec)
        if not moments: raise Exception("No viral moments detected. Longer video try karo.")
        moments = [enforce_duration(m, min_sec, max_sec, target_sec) for m in moments]

        clip_data = []
        for i, moment in enumerate(moments[:6]):
            pct = 55 + int((i / len(moments[:6])) * 38)
            update(job_id, f"Cutting clip {i+1}/{len(moments[:6])}...", pct)
            local_clip = str(job_dir / f"clip_{i+1}.mp4")
            cut_clip_from_local(video_path, audio_path, moment["start"], moment["end"],
                                moment.get("caption",""), local_clip, transcript)

            update(job_id, f"Uploading clip {i+1} to storage...", pct+2)
            b2_key = f"{user_id}/{job_id}/clip_{i+1}.mp4"
            upload_to_b2(local_clip, b2_key)
            signed_url = get_signed_url(b2_key)

            db_clip = supabase.table("clips").insert({
                "user_id": user_id, "job_id": job_id, "b2_key": b2_key,
                "caption": moment.get("caption",""), "reason": moment.get("reason",""),
            }).execute()
            clip_db_id = db_clip.data[0]["id"] if db_clip.data else None

            clip_data.append({"url": signed_url, "b2_key": b2_key, "db_id": clip_db_id,
                               "caption": moment.get("caption",""), "reason": moment.get("reason","")})
            Path(local_clip).unlink(missing_ok=True)

        jobs[job_id]["clips"]   = clip_data
        jobs[job_id]["moments"] = moments[:6]
        update(job_id, "All clips ready! 🎉", 100, "done")

    except Exception as e:
        jobs[job_id].update({"error": str(e), "status": "error", "step": f"Error: {e}"})
        print(f"[{job_id[:8]}] ERROR: {e}")
    finally:
        if job_dir.exists(): shutil.rmtree(job_dir)

# ── Download helpers ───────────────────────────────────────────
def _extract_video_id(youtube_url: str) -> str:
    patterns = [
        r"(?:v=)([a-zA-Z0-9_-]{11})",
        r"(?:shorts/)([a-zA-Z0-9_-]{11})",
        r"(?:youtu\.be/)([a-zA-Z0-9_-]{11})",
        r"(?:embed/)([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, youtube_url)
        if match: return match.group(1)
    raise Exception(f"YouTube video ID nahi mila URL se: {youtube_url}")

def _try_ytdlp_ios(youtube_url, out_dir):
    cookies_file = str(COOKIES_PATH) if COOKIES_PATH.exists() else None
    ydl_opts = {
        # 720p cap: much faster + fits free-tier disk/RAM
        "format": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
        "outtmpl": str(out_dir / "video.%(ext)s"),
        "quiet": True, "no_warnings": True, "merge_output_format": "mp4",
        "retries": 3, "noplaylist": True,
        # YouTube needs a JS runtime to solve its player challenge
        "js_runtimes": {"node": {}} if shutil.which("node") else {"deno": {}},
        "remote_components": ["ejs:github"],
    }
    if cookies_file:
        ydl_opts["cookiefile"] = cookies_file
        print(f"[Cookie] using {cookies_file}")
    else:
        print("[Cookie] no cookies.txt — YouTube may block datacenter IPs")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([youtube_url])
    print("[Download] ✅ yt-dlp success!")

def _try_invidious(youtube_url, out_dir):
    video_id = _extract_video_id(youtube_url)
    print(f"[Invidious] Video ID: {video_id}")
    stream_url = None; last_error = None
    for server in INVIDIOUS_SERVERS:
        try:
            print(f"[Invidious] Trying: {server}")
            res = requests.get(f"{server}/api/v1/videos/{video_id}?fields=adaptiveFormats,formatStreams", timeout=15)
            if res.status_code != 200:
                last_error = f"{server} returned {res.status_code}"; continue
            data = res.json()
            # Combined streams pehle try karo
            fmt_streams = data.get("formatStreams", [])
            if fmt_streams:
                fmt_streams.sort(key=lambda x: int(x.get("resolution","0p").replace("p","") or 0), reverse=True)
                stream_url = fmt_streams[0]["url"]
                print(f"[Invidious] ✅ Combined stream from {server}"); break
        except Exception as e:
            last_error = str(e); print(f"[Invidious] {server} failed: {e}"); continue
    if not stream_url:
        raise Exception(f"Invidious: Koi server kaam nahi kiya. Last: {last_error}")
    out_path = str(out_dir / "video.mp4")
    ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15"
    r = subprocess.run(["ffmpeg","-y","-user_agent",ua,"-i",stream_url,"-c","copy",out_path],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise Exception(f"Invidious ffmpeg failed: {r.stderr[-300:]}")
    print("[Download] ✅ Invidious success!")

def _try_piped(youtube_url, out_dir):
    video_id = _extract_video_id(youtube_url)
    print(f"[Piped] Video ID: {video_id}")
    stream_url = None; audio_url = None; last_error = None
    for server in PIPED_SERVERS:
        try:
            print(f"[Piped] Trying: {server}")
            res = requests.get(f"{server}/streams/{video_id}", timeout=15)
            if res.status_code != 200:
                last_error = f"{server} returned {res.status_code}"; continue
            data = res.json()
            combined   = [s for s in data.get("videoStreams",[]) if not s.get("videoOnly",True)]
            video_only = [s for s in data.get("videoStreams",[]) if s.get("videoOnly",False)]
            audio_list = data.get("audioStreams",[])
            if combined:
                combined.sort(key=lambda x: int(x.get("quality","0p").replace("p","").split(" ")[0] or 0), reverse=True)
                stream_url = combined[0]["url"]; print(f"[Piped] ✅ Combined from {server}"); break
            elif video_only and audio_list:
                video_only.sort(key=lambda x: int(x.get("quality","0p").replace("p","").split(" ")[0] or 0), reverse=True)
                audio_list.sort(key=lambda x: x.get("bitrate",0), reverse=True)
                stream_url = video_only[0]["url"]; audio_url = audio_list[0]["url"]
                print(f"[Piped] ✅ Video+Audio from {server}"); break
        except Exception as e:
            last_error = str(e); print(f"[Piped] {server} failed: {e}"); continue
    if not stream_url:
        raise Exception(f"Piped: Koi bhi server kaam nahi kiya. Last error: {last_error}")
    out_path = str(out_dir / "video.mp4")
    ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15"
    if audio_url:
        r = subprocess.run(["ffmpeg","-y","-user_agent",ua,"-i",stream_url,"-user_agent",ua,"-i",audio_url,"-c:v","copy","-c:a","aac",out_path], capture_output=True, text=True, timeout=600)
    else:
        r = subprocess.run(["ffmpeg","-y","-user_agent",ua,"-i",stream_url,"-c","copy",out_path], capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise Exception(f"Piped ffmpeg failed: {r.stderr[-300:]}")
    print("[Download] ✅ Piped API success!")

def download_full_video(youtube_url, out_dir):
    errors = []
    # Method 1: yt-dlp with cookies
    try:
        print("[Download] Method 1: yt-dlp iOS client...")
        _try_ytdlp_ios(youtube_url, out_dir)
    except Exception as e:
        errors.append(f"yt-dlp iOS: {str(e)[:200]}")
        print(f"[Download] Method 1 failed: {e}")
        # Method 2: Invidious
        try:
            print("[Download] Method 2: Invidious API...")
            _try_invidious(youtube_url, out_dir)
        except Exception as e2:
            errors.append(f"Invidious: {str(e2)[:200]}")
            print(f"[Download] Method 2 failed: {e2}")
            # Method 3: Piped
            try:
                print("[Download] Method 3: Piped API...")
                _try_piped(youtube_url, out_dir)
            except Exception as e3:
                errors.append(f"Piped: {str(e3)[:200]}")
                print(f"[Download] Method 3 failed: {e3}")
                raise Exception("Video download nahi hua. Teeno methods fail:\n" +
                                "\n".join(f"• {err}" for err in errors) +
                                "\n\nYouTube URL sahi hai? Private/restricted video toh nahi?")

    video_path = next(
        (str(f) for f in out_dir.iterdir() if f.suffix in (".mp4",".mkv",".webm") and "audio" not in f.name), None)
    if not video_path:
        raise Exception("Video download failed — file nahi mili output folder mein")
    audio_path = str(out_dir / "audio.mp3")
    r = subprocess.run(["ffmpeg","-y","-i",video_path,"-vn","-acodec","mp3","-ar","16000","-ab","64k","-ac","1",audio_path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise Exception(f"Audio extract failed: {r.stderr[-300:]}")
    return video_path, audio_path

# ── Transcription ──────────────────────────────────────────────
def enforce_duration(moment, min_sec, max_sec, target_sec):
    s, e = float(moment["start"]), float(moment["end"])
    if e-s < min_sec: e = s+target_sec
    elif e-s > max_sec: e = s+max_sec
    if s < 0: s = 0.0
    if e <= s: e = s+target_sec
    moment["start"] = int(s); moment["end"] = int(e)
    return moment

def _transcribe_single(audio_path, lang_code):
    with open(audio_path, "rb") as f:
        resp = groq_client.audio.transcriptions.create(
            file=(Path(audio_path).name, f), model="whisper-large-v3", language=lang_code,
            response_format="verbose_json", timestamp_granularities=["segment"])
    return [{"start": (seg["start"] if isinstance(seg,dict) else seg.start),
             "end":   (seg["end"]   if isinstance(seg,dict) else seg.end),
             "text":  (seg["text"]  if isinstance(seg,dict) else seg.text).strip()} for seg in resp.segments]

def transcribe_groq(audio_path, lang_code):
    MAX_BYTES  = 24*1024*1024
    audio_size = Path(audio_path).stat().st_size
    if audio_size <= MAX_BYTES: return _transcribe_single(audio_path, lang_code)
    probe = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",audio_path],
                           capture_output=True, text=True)
    total_dur  = float(probe.stdout.strip())
    num_chunks = math.ceil(audio_size / MAX_BYTES)
    chunk_dur  = total_dur / num_chunks
    chunk_dir  = Path(audio_path).parent / "chunks"; chunk_dir.mkdir(exist_ok=True)
    all_segs   = []
    try:
        for i in range(num_chunks):
            cp = str(chunk_dir / f"chunk_{i}.mp3")
            subprocess.run(["ffmpeg","-y","-ss",str(i*chunk_dur),"-i",audio_path,"-t",str(chunk_dur),"-acodec","mp3","-ar","16000","-ab","64k","-ac","1",cp],
                           capture_output=True)
            for seg in _transcribe_single(cp, lang_code):
                seg["start"] += i*chunk_dur; seg["end"] += i*chunk_dur
                all_segs.append(seg)
    finally:
        if chunk_dir.exists(): shutil.rmtree(chunk_dir)
    return all_segs

def detect_viral_moments(segments, min_sec, max_sec, target_sec):
    transcript_text = "".join(f"[{int(s['start'])}s-{int(s['end'])}s] {s['text'].strip()}\n" for s in segments)
    prompt = f"""Viral content expert for Indian short-form video.
Find 3-6 viral moments. Each clip MUST be exactly {target_sec}s. Min {min_sec}s. Caption max 8 words Roman script only.
Return ONLY JSON array: [{{"start":10,"end":{10+target_sec},"reason":"...","caption":"..."}}]
Transcript:\n{transcript_text[:6000]}"""
    resp = requests.post("https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json", "HTTP-Referer": "https://clipai.app"},
        json={"model": "deepseek/deepseek-chat", "messages": [{"role": "user", "content": prompt}], "temperature": 0.7}, timeout=60)
    if resp.status_code != 200: raise Exception(f"DeepSeek error: {resp.text}")
    raw = resp.json()["choices"][0]["message"]["content"].strip().replace("```json","").replace("```","").strip()
    try: return json.loads(raw)
    except Exception as e: raise Exception(f"Invalid JSON: {e}\nRaw: {raw[:300]}")

# ── FFmpeg helpers ─────────────────────────────────────────────
def sanitize_caption(text):
    r = "".join(ch for ch in text if ord(ch) < 128)
    return r.replace("'","").replace('"',"").replace(":"," ").replace("[","").replace("]","").replace("\\","").replace("%","").replace(",","").replace(";","").replace("\n"," ").strip()

def build_drawtext(caption, font_path):
    words = caption.split()
    text  = " ".join(words[:len(words)//2])+"\n"+" ".join(words[len(words)//2:]) if len(words) > 4 else caption
    if font_path:
        return f"drawtext=fontfile='{font_path}':text='{text}':fontsize=40:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=12:x=(w-text_w)/2:y=(h*0.77)-text_h/2"
    return f"drawtext=text='{text}':fontsize=40:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=12:x=(w-text_w)/2:y=(h*0.77)-text_h/2"

def build_watermark(font_path):
    if font_path:
        return f"drawtext=fontfile='{font_path}':text='{WATERMARK_TEXT}':fontsize=30:fontcolor=white@0.45:x=30:y=(h*0.65)-text_h/2"
    return f"drawtext=text='{WATERMARK_TEXT}':fontsize=30:fontcolor=white@0.45:x=30:y=(h*0.65)-text_h/2"

def sanitize_unicode(text):
    return re.sub(r"[{}\\\r\n]+", " ", text).strip()

def get_clip_segments(segments, clip_start, clip_end, unicode_ok=False):
    result = []
    for seg in segments:
        s, e = float(seg["start"]), float(seg["end"])
        if e <= clip_start or s >= clip_end: continue
        rel_s = round(max(0.0, s-clip_start), 3)
        rel_e = round(min(float(clip_end-clip_start), e-clip_start), 3)
        if rel_e <= rel_s: continue
        clean = sanitize_unicode(seg["text"]) if unicode_ok else sanitize_caption(seg["text"])
        if clean: result.append({"start": rel_s, "end": rel_e, "text": clean})
    return result

def build_synced_drawtexts(segments, font_path):
    filters = []
    for seg in segments:
        t = seg["text"].strip()
        if not t: continue
        words = t.split()
        if len(words) > 4: t = " ".join(words[:len(words)//2])+"\n"+" ".join(words[len(words)//2:])
        if font_path:
            filters.append(f"drawtext=fontfile='{font_path}':text='{t}':fontsize=38:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=10:x=(w-text_w)/2:y=(h*0.77)-text_h/2:enable='between(t\\,{seg['start']}\\,{seg['end']})'")
        else:
            filters.append(f"drawtext=text='{t}':fontsize=38:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=10:x=(w-text_w)/2:y=(h*0.77)-text_h/2:enable='between(t\\,{seg['start']}\\,{seg['end']})'")
    return ",".join(filters)

# ---- Stylish captions (ASS): white text + shadow, yellow keyword, pop animation ----
FONTS_DIR = BASE_DIR / "fonts"
CAP_FONT  = "Poppins ExtraBold"
CAP_Y     = 1010   # caption ki vertical position (4:3 video ke neeche)
_STOP = {"main","mai","mein","hai","hain","hu","hun","ho","hoga","tha","thi","the","ka","ki","ke","ko","se","ne",
         "par","pe","aur","to","ye","yeh","wo","woh","jaa","raha","rahi","rahe","kar","karo","bhi","nahi","na",
         "is","us","ek","and","the","a","an","of","in","on","it","you","i","we","are","was",
         "है","हैं","हूँ","हूं","मैं","में","का","की","के","को","से","ने","पर","और","तो","ये","यह","वो","वह",
         "भी","नहीं","कि","था","थी","थे","हो","रहा","रही","रहे","कर","एक","जा","लिए"}

def _ass_time(t):
    h, rem = divmod(max(0.0, t), 3600); m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"

def _fe(p):  # ffmpeg filter path escape (Windows safe)
    return str(p).replace("\\", "/").replace(":", "\\:")

def build_ass(clip_segments):
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 720\nPlayResY: 1280\nWrapStyle: 0\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Cap,{CAP_FONT},62,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,1,4,3,5,40,40,0,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    anim = r"{\an5\pos(360,%d)\fad(60,0)\fscx80\fscy80\t(0,130,\fscx108\fscy108)\t(130,230,\fscx100\fscy100)}" % CAP_Y
    ev = []
    for seg in clip_segments:
        words = re.sub(r"[{}\\]", "", seg["text"]).split()
        if not words: continue
        chunks = [words[i:i+3] for i in range(0, len(words), 3)]
        wts = [sum(len(w) + 1 for w in c) for c in chunks]
        total, acc = float(sum(wts)), 0
        dur = seg["end"] - seg["start"]
        for c, wt in zip(chunks, wts):
            st = seg["start"] + dur * acc / total; acc += wt
            en = seg["start"] + dur * acc / total
            cand = [w for w in c if w.lower().strip(".,!?।") not in _STOP] or c
            ki = c.index(max(cand, key=len))
            txt = " ".join((r"{\c&H00E6FF&}" + w + r"{\c&HFFFFFF&}") if i == ki else w for i, w in enumerate(c))
            ev.append(f"Dialogue: 0,{_ass_time(st)},{_ass_time(en)},Cap,,0,0,0,,{anim}{txt}")
    if not ev: return None
    p = TEMP_DIR / f"cap_{uuid.uuid4().hex[:8]}.ass"
    p.write_text(head + "\n".join(ev) + "\n", encoding="utf-8")
    return p

def cut_clip_from_local(video_path, audio_path, start, end, caption_text, out_path, segments=None):
    """Single-pass accurate cut + 9:16 + watermark + synced captions (audio comes from the video itself)."""
    duration = end - start

    # 9:16 with blurred background instead of ugly black bars
    fc_base = ("[0:v]split[a][b];"
               f"[a]scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,{BG_FILTER}[bg];"
               "[b]scale=720:540:force_original_aspect_ratio=increase,crop=720:540,"
               f"scale=trunc(iw*{ZOOM_FACTOR}/2)*2:trunc(ih*{ZOOM_FACTOR}/2)*2,"
               "crop=720:540[fg];"
               "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1")
    wm = build_watermark(FONT_PATH) if FONT_PATH else ""

    def graph(extra):
        parts = [p for p in (wm, extra) if p]
        return fc_base + ("," + ",".join(parts) if parts else "") + "[v]"

    attempts = []
    try:
        ass_f = build_ass(get_clip_segments(segments, start, end, True)) if segments else None
    except Exception as e:
        ass_f = None; print(f"[ASS] {e}")
    if ass_f:
        attempts.append(("ass", graph(f"ass='{_fe(ass_f)}':fontsdir='{_fe(FONTS_DIR)}'")))
    if segments and FONT_PATH:
        synced = build_synced_drawtexts(get_clip_segments(segments, start, end), FONT_PATH)
        if synced: attempts.append(("synced", graph(synced)))
    safe_cap = sanitize_caption(caption_text)
    if safe_cap and FONT_PATH: attempts.append(("static", graph(build_drawtext(safe_cap, FONT_PATH))))
    attempts.append(("no-caption", graph("")))

    last_err = ""
    for label, fc in attempts:
        cmd = ["ffmpeg", "-y", "-ss", str(start), "-t", str(duration), "-i", video_path,
               "-filter_complex", fc, "-map", "[v]", "-map", "0:a:0?",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out_path]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            print(f"[ffmpeg] OK ({label})")
            return out_path
        last_err = r.stderr[-600:]
        print(f"[ffmpeg] {label} failed:\n{last_err}")
    raise Exception(f"ffmpeg: all attempts failed: {last_err[-200:]}")
