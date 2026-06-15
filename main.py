"""
Dynamic Media Card Tool — Schedule X Website/App Card updates (exact theme + footer from Post Xploder)

NO DATABASE VERSION:
- All state is in-memory (users, encrypted tokens, schedules).
- Schedules and tokens are lost when the process restarts (ideal for local dev and simple deploys).
- The in-process APScheduler still runs and will execute due updates while the process is alive.
- Sign-in is fast; just re-auth if you restart the server.
- Pure X OAuth flow only — clicking "Sign in with X" takes you directly to X.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from dotenv import load_dotenv
load_dotenv()

# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------

X_CLIENT_ID = os.getenv("X_CLIENT_ID", "")
X_CLIENT_SECRET = os.getenv("X_CLIENT_SECRET", "")
X_REDIRECT_URI = os.getenv("X_REDIRECT_URI", "http://127.0.0.1:8000/callback")
SECRET_KEY = os.getenv("SECRET_KEY", "")

if not SECRET_KEY or len(SECRET_KEY) < 16:
    SECRET_KEY = "dev-insecure-change-me-" + secrets.token_hex(16)

def _get_fernet() -> Fernet:
    key_material = hashlib.sha256(SECRET_KEY.encode()).digest()
    fernet_key = base64.urlsafe_b64encode(key_material)
    return Fernet(fernet_key)

FERNET = _get_fernet()

SESSION_SALT = "cardxploder-session-v1"
signer = URLSafeSerializer(SECRET_KEY, salt=SESSION_SALT)

X_AUTHORIZE_URL = "https://twitter.com/i/oauth2/authorize"
X_TOKEN_URL = "https://api.x.com/2/oauth2/token"
X_USERS_ME_URL = "https://api.x.com/2/users/me"
X_TWEETS_URL = "https://api.x.com/2/tweets"

ADS_BASE = "https://ads-api.x.com/12"

# --------------------------------------------------------------------------------------
# In-memory stores (ephemeral, no DB)
# --------------------------------------------------------------------------------------

# Keyed by X user id (string). Lost on restart — you simply sign in again.
USERS: Dict[str, Dict[str, Any]] = {}
TOKENS: Dict[str, Dict[str, Any]] = {}   # encrypted tokens + expires_at etc.

# Schedules live only while this process is running.
SCHEDULES: Dict[int, Dict[str, Any]] = {}
_SCHEDULE_COUNTER = 0

def next_schedule_id() -> int:
    global _SCHEDULE_COUNTER
    _SCHEDULE_COUNTER += 1
    return _SCHEDULE_COUNTER

def encrypt(text: str) -> str:
    if not text:
        return ""
    return FERNET.encrypt(text.encode()).decode()

def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return FERNET.decrypt(token.encode()).decode()
    except Exception:
        return ""

# --------------------------------------------------------------------------------------
# Auth / Session (signed cookie carries the x_user_id)
# --------------------------------------------------------------------------------------

def create_session_cookie(x_user_id: str) -> str:
    return signer.dumps({"xuid": x_user_id})

def get_user_from_cookie(request: Request) -> Optional[Dict[str, Any]]:
    raw = request.cookies.get("session")
    if not raw:
        return None
    try:
        data = signer.loads(raw)
        xuid = data.get("xuid")
        if not xuid:
            return None
        profile = USERS.get(xuid)
        if not profile:
            return None
        tok = TOKENS.get(xuid, {})
        user = {
            "id": xuid,
            "x_user_id": xuid,
            "username": profile.get("username"),
            "name": profile.get("name"),
            "profile_image_url": profile.get("profile_image_url"),
            "access_token": decrypt(tok.get("access_token", "")),
            "refresh_token": decrypt(tok.get("refresh_token", "")),
            "expires_at": tok.get("expires_at", 0),
            "scope": tok.get("scope"),
        }
        return user
    except BadSignature:
        return None

def clear_session_cookie(response: Response):
    response.delete_cookie("session", path="/")

async def require_user(request: Request) -> Dict[str, Any]:
    user = get_user_from_cookie(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user

# --------------------------------------------------------------------------------------
# X / Ads HTTP helpers
# --------------------------------------------------------------------------------------

async def x_get(url: str, access_token: str, params: Optional[Dict] = None) -> Dict[str, Any]:
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=headers, params=params or {})
        if r.status_code == 401:
            raise HTTPException(status_code=401, detail="X token expired or invalid")
        r.raise_for_status()
        return r.json()

async def ads_get(path: str, access_token: str, params: Optional[Dict] = None) -> Dict[str, Any]:
    url = f"{ADS_BASE}{path}"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=headers, params=params or {})
        if r.status_code in (401, 403):
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise HTTPException(status_code=r.status_code, detail=f"Ads API error: {detail}")
        r.raise_for_status()
        return r.json()

async def ads_put(path: str, access_token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{ADS_BASE}{path}"
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.put(url, headers=headers, json=payload)
        if r.status_code in (401, 403):
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise HTTPException(status_code=r.status_code, detail=f"Ads API error: {detail}")
        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise HTTPException(status_code=r.status_code, detail=f"Ads update failed: {detail}")
        return r.json() if r.text else {"ok": True}

async def refresh_x_token_if_needed(user: Dict[str, Any]) -> str:
    """Return a valid access_token, refreshing + updating the in-memory store if necessary."""
    now = time.time()
    if user.get("expires_at", 0) > now + 60:
        return user["access_token"]

    refresh = user.get("refresh_token")
    if not refresh:
        raise HTTPException(status_code=401, detail="No refresh token available. Please sign in again.")

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": X_CLIENT_ID,
    }
    auth = (X_CLIENT_ID, X_CLIENT_SECRET) if X_CLIENT_SECRET else None

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            X_TOKEN_URL,
            data=data,
            auth=auth,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if r.status_code != 200:
            raise HTTPException(status_code=401, detail="Failed to refresh X token. Sign in again.")
        tok = r.json()

    new_access = tok["access_token"]
    new_refresh = tok.get("refresh_token", refresh)
    expires_in = int(tok.get("expires_in", 7200))
    expires_at = time.time() + expires_in

    xuid = user["x_user_id"]
    TOKENS[xuid] = {
        "access_token": encrypt(new_access),
        "refresh_token": encrypt(new_refresh) if new_refresh else None,
        "expires_at": expires_at,
        "scope": TOKENS.get(xuid, {}).get("scope"),
    }

    user["access_token"] = new_access
    user["refresh_token"] = new_refresh
    user["expires_at"] = expires_at
    return new_access

# --------------------------------------------------------------------------------------
# OAuth2 PKCE
# --------------------------------------------------------------------------------------

def generate_pkce() -> Tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge

async def fetch_x_user(access_token: str) -> Dict[str, Any]:
    params = {"user.fields": "id,username,name,profile_image_url"}
    data = await x_get(X_USERS_ME_URL, access_token, params)
    u = data.get("data", {})
    return {
        "x_user_id": u.get("id"),
        "username": u.get("username"),
        "name": u.get("name"),
        "profile_image_url": u.get("profile_image_url"),
    }

# --------------------------------------------------------------------------------------
# Card / Tweet helpers + media type detection
# --------------------------------------------------------------------------------------

TWEET_URL_RE = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/[^/]+/status/(\d+)", re.I)

def extract_tweet_id(url: str) -> Optional[str]:
    m = TWEET_URL_RE.search(url or "")
    return m.group(1) if m else None

async def validate_tweet_has_card(access_token: str, tweet_id: str) -> Dict[str, Any]:
    params = {
        "tweet.fields": "card_uri,attachments,author_id,created_at,text,entities",
        "expansions": "author_id",
        "user.fields": "username",
    }
    data = await x_get(f"{X_TWEETS_URL}/{tweet_id}", access_token, params)
    tweet = data.get("data", {})
    card_uri = tweet.get("card_uri")
    if not card_uri or not str(card_uri).startswith("card://"):
        raise HTTPException(
            status_code=422,
            detail="This post does not contain a website card or app card (no card_uri). "
                   "Please update the post to include one and try again."
        )
    card_id = str(card_uri).replace("card://", "")
    return {"tweet": tweet, "card_id": card_id, "raw": data}

def parse_card_response(card_json: Dict[str, Any]) -> Dict[str, Any]:
    d = card_json.get("data") or card_json
    card = (
        d.get("card")
        or d.get("website_card")
        or d.get("app_download_card")
        or d.get("image_app_download_card")
        or d.get("video_app_download_card")
        or d
    )
    if isinstance(card, list):
        card = card[0] if card else {}

    title = card.get("name") or card.get("title") or ""

    url = (
        card.get("website_url")
        or card.get("url")
        or card.get("android_url")
        or card.get("iphone_url")
        or card.get("ipad_url")
        or ""
    )

    media_id = (
        card.get("media_id")
        or card.get("image_media_id")
        or card.get("video_media_id")
        or card.get("media_key")
        or card.get("preview_media_id")
        or ""
    )

    card_type = "website"
    if "app" in str(card.get("card_type", "")).lower() or "app_download" in str(d):
        card_type = "app"

    # Best-effort inference from card shape (used as fallback)
    inferred = None
    if "video_media_id" in card or "video" in str(card.get("card_type", "")).lower() or "video_app" in str(d).lower():
        inferred = "video"
    elif "image_media_id" in card or "image" in str(card.get("card_type", "")).lower():
        inferred = "image"

    return {
        "id": str(card.get("id") or card.get("card_id") or ""),
        "title": title,
        "url": url,
        "media_id": media_id,
        "card_type": card_type,
        "inferred_media_type": inferred,
        "raw": card_json,
    }

async def fetch_card_details(access_token: str, ads_account_id: str, card_id: str) -> Dict[str, Any]:
    try:
        data = await ads_get(f"/accounts/{ads_account_id}/cards/{card_id}", access_token)
        parsed = parse_card_response(data)
        if parsed.get("id"):
            return parsed
    except HTTPException as e:
        if e.status_code not in (404, 400):
            raise

    try:
        listed = await ads_get(f"/accounts/{ads_account_id}/cards", access_token, {"count": 200})
        items = (listed.get("data") or [])
        for it in items:
            if str(it.get("id")) == str(card_id):
                return parse_card_response({"data": it})
            for k in ("website_card", "app_download_card", "card"):
                if k in it and str(it[k].get("id", "")) == str(card_id):
                    return parse_card_response({k: it[k]})
    except Exception:
        pass

    for suffix in ("/cards/website", "/cards/app_download"):
        try:
            listed = await ads_get(f"/accounts/{ads_account_id}{suffix}", access_token, {"count": 200})
            for it in (listed.get("data") or []):
                if str(it.get("id")) == str(card_id):
                    return parse_card_response({"data": it})
        except Exception:
            continue

    raise HTTPException(status_code=404, detail="Could not retrieve card details from Ads API. Check account access and card id.")

async def fetch_media_info(access_token: str, ads_account_id: str, media_id: str) -> Dict[str, Any]:
    """
    Return authoritative info for a media id from the Ads account's media library.
    Includes dimensions + media_type ('image' or 'video').
    """
    if not media_id:
        return {"width": None, "height": None, "preview": None, "media_type": None}

    # Try media_library first (most common)
    try:
        data = await ads_get(
            f"/accounts/{ads_account_id}/media_library",
            access_token,
            {"media_ids": media_id, "count": 1},
        )
        items = data.get("data") or []
        if items:
            m = items[0]
            w = m.get("width") or m.get("original_width")
            h = m.get("height") or m.get("original_height")
            preview = m.get("media_url_https") or m.get("preview_url") or m.get("thumbnail_url")
            mtype = _normalize_media_type(m)
            if not mtype:
                mtype = _guess_type_from_url(preview or m.get("media_url") or "")
            return {"width": w, "height": h, "preview": preview, "media_type": mtype}
    except Exception:
        pass

    # Fallback to /media
    try:
        data = await ads_get(
            f"/accounts/{ads_account_id}/media",
            access_token,
            {"ids": media_id},
        )
        items = data.get("data") or []
        if items:
            m = items[0]
            w = m.get("width") or m.get("w")
            h = m.get("height") or m.get("h")
            preview = m.get("media_url") or m.get("url")
            mtype = _normalize_media_type(m)
            if not mtype:
                mtype = _guess_type_from_url(preview or "")
            return {"width": w, "height": h, "preview": preview, "media_type": mtype}
    except Exception:
        pass

    return {"width": None, "height": None, "preview": None, "media_type": None}

def _normalize_media_type(m: Dict[str, Any]) -> Optional[str]:
    t = (m.get("type") or m.get("media_type") or m.get("creative_type") or "").upper()
    if t in ("IMAGE", "PHOTO", "PICTURE", "STATIC"):
        return "image"
    if t in ("VIDEO", "GIF", "ANIMATED"):
        return "video"
    return None

def _guess_type_from_url(url: str) -> Optional[str]:
    u = (url or "").lower()
    if any(u.endswith(ext) for ext in (".mp4", ".mov", ".webm", ".m4v", ".avi")):
        return "video"
    if any(u.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic")):
        return "image"
    return None

def dimensions_match(w1: Optional[int], h1: Optional[int], w2: Optional[int], h2: Optional[int]) -> bool:
    if w1 is None or h1 is None or w2 is None or h2 is None:
        return False
    return int(w1) == int(w2) and int(h1) == int(h2)

# --------------------------------------------------------------------------------------
# Scheduler (in-memory)
# --------------------------------------------------------------------------------------

scheduler: Optional[BackgroundScheduler] = None

def start_scheduler(app: FastAPI):
    global scheduler
    if scheduler and scheduler.running:
        return
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        poll_and_execute_due_schedules,
        trigger=IntervalTrigger(seconds=20),
        id="cardxploder-poller",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    scheduler.add_job(poll_and_execute_due_schedules, trigger="date", run_date=datetime.now(timezone.utc) + timedelta(seconds=5))

def poll_and_execute_due_schedules():
    now = time.time()
    due = [
        s for s in SCHEDULES.values()
        if s.get("status") == "pending" and s.get("scheduled_at", 0) <= now
    ]
    due.sort(key=lambda x: x.get("scheduled_at", 0))
    for s in due[:10]:
        execute_card_update(s)

def execute_card_update(schedule: Dict[str, Any]):
    sid = schedule["id"]
    sched = SCHEDULES.get(sid)
    if not sched or sched.get("status") != "pending":
        return
    sched["status"] = "running"

    try:
        xuid = schedule["user_id"]
        tok = TOKENS.get(xuid, {})
        if not tok:
            raise RuntimeError("User tokens not found for schedule")

        user = {
            "id": xuid,
            "x_user_id": xuid,
            "access_token": decrypt(tok.get("access_token", "")),
            "refresh_token": decrypt(tok.get("refresh_token", "")),
            "expires_at": tok.get("expires_at", 0),
        }

        import asyncio
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            access = loop.run_until_complete(refresh_x_token_if_needed(user))
        finally:
            loop.close()

        payload: Dict[str, Any] = {"name": schedule["new_title"]}
        url_val = schedule["new_url"]
        media_val = schedule["new_media_id"]

        lower_url = (url_val or "").lower()
        if "play.google" in lower_url or "apps.apple" in lower_url or "itunes.apple" in lower_url:
            payload["media_id"] = media_val
            payload["website_url"] = url_val
        else:
            payload["image_media_id"] = media_val
            payload["website_url"] = url_val

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(
                ads_put(f"/accounts/{schedule['ads_account_id']}/cards/{schedule['card_id']}", access, payload)
            )
        finally:
            loop.close()

        sched["status"] = "completed"
        sched["result"] = json.dumps(result)[:4000]
        sched["executed_at"] = time.time()

    except Exception as exc:
        sched = SCHEDULES.get(sid)
        if sched:
            sched["status"] = "failed"
            sched["result"] = str(exc)[:4000]
            sched["executed_at"] = time.time()

# --------------------------------------------------------------------------------------
# FastAPI app + templates
# --------------------------------------------------------------------------------------

app = FastAPI(title="Dynamic Media Card Tool", docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="cardx_session",
    max_age=60 * 60 * 24 * 30,
)

templates = Jinja2Templates(directory="templates")

@app.on_event("startup")
async def on_startup():
    start_scheduler(app)

@app.on_event("shutdown")
async def on_shutdown():
    global scheduler
    if scheduler:
        scheduler.shutdown(wait=False)

# --------------------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------------------

class ScheduleIn(BaseModel):
    ads_account_id: str
    card_id: str
    card_type: Optional[str] = None
    original_title: Optional[str] = ""
    original_media_id: Optional[str] = ""
    original_url: Optional[str] = ""
    original_media_width: Optional[int] = None
    original_media_height: Optional[int] = None
    original_media_type: Optional[str] = None
    new_title: str
    new_media_id: str
    new_url: str
    new_media_type: Optional[str] = None
    scheduled_at: float

# --------------------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = get_user_from_cookie(request)
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "user": user, "x_redirect_uri": X_REDIRECT_URI},
    )

@app.get("/login")
async def login(request: Request):
    if not X_CLIENT_ID:
        return HTMLResponse("X_CLIENT_ID is not configured on the server.", status_code=500)

    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(24)

    request.session["pkce_verifier"] = verifier
    request.session["oauth_state"] = state

    params = {
        "response_type": "code",
        "client_id": X_CLIENT_ID,
        "redirect_uri": X_REDIRECT_URI,
        "scope": "tweet.read users.read offline.access",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return RedirectResponse(f"{X_AUTHORIZE_URL}?{urlencode(params)}")

@app.get("/callback")
async def callback(request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    if error:
        return RedirectResponse("/?error=" + error)

    saved_state = request.session.get("oauth_state")
    verifier = request.session.get("pkce_verifier")
    if not code or not state or state != saved_state or not verifier:
        return RedirectResponse("/?error=invalid_oauth_state")

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": X_REDIRECT_URI,
        "client_id": X_CLIENT_ID,
        "code_verifier": verifier,
    }
    auth = (X_CLIENT_ID, X_CLIENT_SECRET) if X_CLIENT_SECRET else None

    async with httpx.AsyncClient(timeout=30) as client:
        tok_resp = await client.post(
            X_TOKEN_URL,
            data=data,
            auth=auth,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if tok_resp.status_code != 200:
            return RedirectResponse("/?error=token_exchange_failed")
        tok = tok_resp.json()

    access_token = tok["access_token"]
    refresh_token = tok.get("refresh_token")
    expires_in = int(tok.get("expires_in", 7200))
    expires_at = time.time() + expires_in
    scope = tok.get("scope", "")

    try:
        profile = await fetch_x_user(access_token)
    except Exception:
        return RedirectResponse("/?error=profile_fetch_failed")

    xuid = profile["x_user_id"]
    USERS[xuid] = {
        "x_user_id": xuid,
        "username": profile["username"],
        "name": profile.get("name"),
        "profile_image_url": profile.get("profile_image_url"),
    }
    TOKENS[xuid] = {
        "access_token": encrypt(access_token),
        "refresh_token": encrypt(refresh_token) if refresh_token else None,
        "expires_at": expires_at,
        "scope": scope,
    }

    resp = RedirectResponse("/")
    resp.set_cookie("session", create_session_cookie(xuid), httponly=True, samesite="lax", max_age=60*60*24*90, path="/")
    request.session.pop("pkce_verifier", None)
    request.session.pop("oauth_state", None)
    return resp

@app.post("/logout")
async def logout(request: Request):
    resp = RedirectResponse("/")
    clear_session_cookie(resp)
    request.session.clear()
    return resp

# --------------------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------------------

@app.get("/api/me")
async def api_me(user: Dict[str, Any] = Depends(get_user_from_cookie)):
    if not user:
        return JSONResponse({"user": None})
    return {
        "user": {
            "id": user["id"],
            "username": user["username"],
            "name": user.get("name"),
            "profile_image_url": user.get("profile_image_url"),
        }
    }

@app.get("/api/ads-accounts")
async def api_ads_accounts(user: Dict[str, Any] = Depends(require_user)):
    access = await refresh_x_token_if_needed(user)
    try:
        data = await ads_get("/accounts", access, {"count": 50})
        accounts = []
        for a in (data.get("data") or []):
            accounts.append({
                "id": a.get("id"),
                "name": a.get("name") or a.get("business_name") or "",
                "timezone": a.get("timezone"),
            })
        return {"accounts": accounts}
    except HTTPException as e:
        return JSONResponse({"accounts": [], "error": str(e.detail)}, status_code=200)

@app.post("/api/validate-post")
async def api_validate_post(request: Request, user: Dict[str, Any] = Depends(require_user)):
    body = await request.json()
    post_url = (body.get("post_url") or "").strip()
    ads_account_id = (body.get("ads_account_id") or "").strip()

    if not ads_account_id:
        raise HTTPException(422, detail="Ads Account ID is required before validating the post.")

    tweet_id = extract_tweet_id(post_url)
    if not tweet_id:
        raise HTTPException(422, detail="Could not parse a valid X post URL. Example: https://x.com/username/status/1234567890123456789")

    access = await refresh_x_token_if_needed(user)

    v = await validate_tweet_has_card(access, tweet_id)
    card_id = v["card_id"]

    card = await fetch_card_details(access, ads_account_id, card_id)

    # Fetch media info (dimensions + type)
    minfo = await fetch_media_info(access, ads_account_id, card.get("media_id", ""))
    card["original_media_width"] = minfo.get("width")
    card["original_media_height"] = minfo.get("height")
    card["media_preview"] = minfo.get("preview")
    # Prefer the real media library type; fall back to inference from card
    card["original_media_type"] = minfo.get("media_type") or card.get("inferred_media_type")

    if not card.get("id"):
        card["id"] = card_id

    return {"ok": True, "tweet_id": tweet_id, "card": card}

@app.post("/api/check-media")
async def api_check_media(request: Request, user: Dict[str, Any] = Depends(require_user)):
    body = await request.json()
    ads_account_id = (body.get("ads_account_id") or "").strip()
    original_media_id = (body.get("original_media_id") or "").strip()
    new_media_id = (body.get("new_media_id") or "").strip()

    if not ads_account_id or not new_media_id:
        return {"match": False, "error": "Missing account or media id"}

    access = await refresh_x_token_if_needed(user)

    orig = await fetch_media_info(access, ads_account_id, original_media_id)
    newi = await fetch_media_info(access, ads_account_id, new_media_id)

    dim_ok = dimensions_match(orig.get("width"), orig.get("height"), newi.get("width"), newi.get("height"))
    type_ok = bool(orig.get("media_type")) and (orig.get("media_type") == newi.get("media_type"))
    overall = dim_ok and type_ok

    err = None
    if not overall:
        if not type_ok:
            err = f"Media type must match the original (original is {orig.get('media_type') or 'unknown'}, new is {newi.get('media_type') or 'unknown'})."
        else:
            err = "Media dimensions do not match the original card's media."

    return {
        "match": overall,
        "type_match": type_ok,
        "original": {"width": orig.get("width"), "height": orig.get("height"), "media_type": orig.get("media_type")},
        "new": {"width": newi.get("width"), "height": newi.get("height"), "media_type": newi.get("media_type"), "preview": newi.get("preview")},
        "error": err,
    }

@app.post("/api/validate-url")
async def api_validate_url(request: Request):
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url:
        return {"valid": False, "error": "URL is required"}
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {"valid": False, "error": "Please enter a valid http(s) URL."}
    try:
        async with httpx.AsyncClient(timeout=6, follow_redirects=True) as client:
            r = await client.head(url)
            if r.status_code >= 400:
                return {"valid": False, "error": f"URL returned status {r.status_code}."}
    except Exception:
        pass
    return {"valid": True}

@app.post("/api/schedules")
async def api_create_schedule(payload: ScheduleIn, user: Dict[str, Any] = Depends(require_user)):
    if payload.scheduled_at <= time.time() + 30:
        raise HTTPException(422, detail="Scheduled time must be in the future.")

    access = await refresh_x_token_if_needed(user)

    # Server-side validation of dimensions + type + url
    orig = await fetch_media_info(access, payload.ads_account_id, payload.original_media_id)
    newi = await fetch_media_info(access, payload.ads_account_id, payload.new_media_id)

    if not dimensions_match(orig.get("width"), orig.get("height"), newi.get("width"), newi.get("height")):
        raise HTTPException(422, detail="New media_id must have the exact same dimensions as the original card's media.")

    orig_type = orig.get("media_type") or payload.original_media_type
    new_type = newi.get("media_type") or payload.new_media_type
    if not orig_type or orig_type != new_type:
        raise HTTPException(422, detail=f"New media must be the same type as the original ({orig_type or 'unknown'}).")

    parsed = urlparse(payload.new_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(422, detail="New URL must be a valid http(s) URL.")

    now = time.time()
    sid = next_schedule_id()
    rec = {
        "id": sid,
        "user_id": user["id"],
        "ads_account_id": payload.ads_account_id,
        "card_id": payload.card_id,
        "card_type": payload.card_type or "website",
        "original_title": payload.original_title or "",
        "original_media_id": payload.original_media_id or "",
        "original_url": payload.original_url or "",
        "original_media_width": payload.original_media_width,
        "original_media_height": payload.original_media_height,
        "original_media_type": orig_type,
        "new_title": payload.new_title,
        "new_media_id": payload.new_media_id,
        "new_url": payload.new_url,
        "new_media_type": new_type,
        "scheduled_at": payload.scheduled_at,
        "status": "pending",
        "result": None,
        "created_at": now,
        "executed_at": None,
    }
    SCHEDULES[sid] = rec
    return {"ok": True, "schedule": rec}

@app.get("/api/schedules")
async def api_list_schedules(user: Dict[str, Any] = Depends(require_user)):
    mine = [s for s in SCHEDULES.values() if s.get("user_id") == user["id"]]
    mine.sort(key=lambda x: x.get("scheduled_at", 0), reverse=True)
    return {"schedules": mine[:100]}

@app.delete("/api/schedules/{sid}")
async def api_cancel_schedule(sid: int, user: Dict[str, Any] = Depends(require_user)):
    rec = SCHEDULES.get(sid)
    if not rec or rec.get("user_id") != user["id"]:
        raise HTTPException(404, "Schedule not found")
    if rec.get("status") in ("completed", "failed"):
        pass
    rec["status"] = "cancelled"
    return {"ok": True}

@app.post("/api/schedules/{sid}/execute")
async def api_force_execute(sid: int, user: Dict[str, Any] = Depends(require_user)):
    rec = SCHEDULES.get(sid)
    if not rec or rec.get("user_id") != user["id"]:
        raise HTTPException(404, "Schedule not found")
    execute_card_update(rec)
    return {"ok": True}

@app.get("/health")
async def health():
    return {"ok": True, "time": time.time(), "schedules_in_memory": len(SCHEDULES)}
