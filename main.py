"""
Dynamic Media Card Tool — Schedule X Website/App Card updates via X Ads API.

- Uses X OAuth 1.0a (3-legged) for sign-in (required for Ads API).
- Schedules are persisted in SQLite so background cron jobs survive server restarts.
- Tokens are also persisted (minimally) so scheduled updates can execute after restart
  (users may still need to re-auth if tokens expire/revoked).
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
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

# OAuth 1.0a credentials (required for X Ads API)
X_CONSUMER_KEY = os.getenv("X_CONSUMER_KEY", "")          # API Key (Consumer Key)
X_CONSUMER_SECRET = os.getenv("X_CONSUMER_SECRET", "")    # API Key Secret (Consumer Secret)

# Optional OAuth2 (kept for backward compat / future, but Ads path uses OAuth1)
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

# OAuth 1.0a endpoints (required for X Ads API user context)
X_REQUEST_TOKEN_URL = "https://api.twitter.com/oauth/request_token"
X_ACCESS_TOKEN_URL = "https://api.twitter.com/oauth/access_token"
X_AUTHORIZE_URL = "https://api.twitter.com/oauth/authorize"

# OAuth2 endpoints kept only if needed for non-Ads parts (we will prefer OAuth1 for everything now)
X_OAUTH2_AUTHORIZE_URL = "https://twitter.com/i/oauth2/authorize"
X_OAUTH2_TOKEN_URL = "https://api.x.com/2/oauth2/token"

X_USERS_ME_URL = "https://api.x.com/2/users/me"
X_TWEETS_URL = "https://api.x.com/2/tweets"

ADS_BASE = "https://ads-api.x.com/12"

# Media upload host (NOT ads-api). Uses the X API v2 chunked upload endpoints
# (dedicated initialize/append/finalize routes; the old `command=` query flow and
# the v1.1 upload.twitter.com endpoint were sunset in 2025). Same OAuth1.0a user context.
X_UPLOAD_BASE = "https://api.x.com/2/media/upload"
UPLOAD_CHUNK_SIZE = 4 * 1024 * 1024  # ~4MB per APPEND segment (X recommends <=5MB)

# Upload size caps (roughly aligned with X media constraints) to prevent a huge upload
# from exhausting memory/disk. Images are small; videos/GIFs get a larger cap.
MAX_IMAGE_UPLOAD_BYTES = 5 * 1024 * 1024          # 5 MB for images
MAX_VIDEO_UPLOAD_BYTES = 512 * 1024 * 1024        # 512 MB for videos/GIFs

# (v1.1 verify_credentials no longer used; profile fetched via /2/users/me with OAuth1 signing)

# --------------------------------------------------------------------------------------
# Persistence (SQLite for schedules + tokens so cron jobs survive restarts)
# In-memory dicts are still used as a cache for fast access.
# --------------------------------------------------------------------------------------

DB_PATH = "dynamic_media_card.db"

def get_db():
    # check_same_thread=False is safe here because a fresh connection is created
    # per call (never shared across threads). WAL + busy_timeout reduce
    # "database is locked" errors when the scheduler thread and request threads
    # touch the DB concurrently.
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            ads_account_id TEXT,
            card_id TEXT,
            card_type TEXT,
            original_title TEXT,
            original_media_id TEXT,
            original_url TEXT,
            original_post_url TEXT,
            original_media_width INTEGER,
            original_media_height INTEGER,
            original_media_type TEXT,
            new_title TEXT,
            new_media_id TEXT,
            new_url TEXT,
            new_media_type TEXT,
            scheduled_at REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            result TEXT,
            executed_at REAL,
            created_at REAL
        )
    """)
    # Backfill column for existing DBs (SQLite is forgiving)
    try:
        c.execute("ALTER TABLE schedules ADD COLUMN original_post_url TEXT")
    except Exception:
        pass  # column already exists or other harmless error
    try:
        c.execute("ALTER TABLE schedules ADD COLUMN original_preview TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE schedules ADD COLUMN new_preview TEXT")
    except Exception:
        pass
    c.execute("""
        CREATE TABLE IF NOT EXISTS tokens (
            x_user_id TEXT PRIMARY KEY,
            oauth_token TEXT,
            oauth_token_secret TEXT,
            access_token_enc TEXT,
            refresh_token_enc TEXT,
            expires_at REAL,
            scope TEXT
        )
    """)
    # NOTE: Public profile fields (username/name/profile_image_url) are intentionally
    # NOT persisted (PII-free at rest). They are held only in the in-memory USERS cache
    # and re-fetched from X on demand (see rehydrate_user) after a restart.
    # Indexes for the hot queries (schedule listing / recovery scans, token lookups).
    c.execute("CREATE INDEX IF NOT EXISTS idx_schedules_user_id ON schedules(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_schedules_status ON schedules(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tokens_x_user_id ON tokens(x_user_id)")
    conn.commit()
    conn.close()

def load_schedules_from_db():
    global SCHEDULES, _SCHEDULE_COUNTER
    conn = get_db()
    rows = conn.execute("SELECT * FROM schedules ORDER BY id").fetchall()
    SCHEDULES.clear()
    max_id = 0
    for r in rows:
        rec = dict(r)
        sid = int(rec["id"])
        SCHEDULES[sid] = rec
        if sid > max_id:
            max_id = sid
    _SCHEDULE_COUNTER = max_id
    conn.close()

def persist_schedule(rec: Dict[str, Any]):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO schedules
        (id, user_id, ads_account_id, card_id, card_type,
         original_title, original_media_id, original_url, original_post_url,
         original_media_width, original_media_height, original_media_type,
         new_title, new_media_id, new_url, new_media_type,
         original_preview, new_preview,
         scheduled_at, status, result, executed_at, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        rec.get("id"),
        rec.get("user_id"),
        rec.get("ads_account_id"),
        rec.get("card_id"),
        rec.get("card_type"),
        rec.get("original_title"),
        rec.get("original_media_id"),
        rec.get("original_url"),
        rec.get("original_post_url"),
        rec.get("original_media_width"),
        rec.get("original_media_height"),
        rec.get("original_media_type"),
        rec.get("new_title"),
        rec.get("new_media_id"),
        rec.get("new_url"),
        rec.get("new_media_type"),
        rec.get("original_preview"),
        rec.get("new_preview"),
        rec.get("scheduled_at"),
        rec.get("status"),
        rec.get("result"),
        rec.get("executed_at"),
        rec.get("created_at"),
    ))
    conn.commit()
    conn.close()

def persist_token(xuid: str, tok: Dict[str, Any]):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO tokens
        (x_user_id, oauth_token, oauth_token_secret, access_token_enc, refresh_token_enc, expires_at, scope)
        VALUES (?,?,?,?,?,?,?)
    """, (
        xuid,
        tok.get("oauth_token"),
        tok.get("oauth_token_secret"),
        tok.get("access_token") or tok.get("access_token_enc"),
        tok.get("refresh_token") or tok.get("refresh_token_enc"),
        tok.get("expires_at"),
        tok.get("scope"),
    ))
    conn.commit()
    conn.close()

def load_tokens_from_db():
    conn = get_db()
    rows = conn.execute("SELECT * FROM tokens").fetchall()
    for r in rows:
        xuid = r["x_user_id"]
        TOKENS[xuid] = {
            "oauth_token": r["oauth_token"],
            "oauth_token_secret": r["oauth_token_secret"],
            "access_token": r["access_token_enc"],
            "refresh_token": r["refresh_token_enc"],
            "expires_at": r["expires_at"],
            "scope": r["scope"],
        }
    conn.close()

# In-memory containers (populated from DB below, also used as hot cache)
USERS: Dict[str, Dict[str, Any]] = {}
TOKENS: Dict[str, Dict[str, Any]] = {}
SCHEDULES: Dict[int, Dict[str, Any]] = {}
_SCHEDULE_COUNTER = 0

# A schedule that has been "running" longer than this is considered a stale/orphaned
# run (process interrupted before it could record a terminal status). The list endpoint
# self-heals such rows to "failed" so the frontend running-poller can stop.
STALE_RUNNING_SECONDS = 600

def next_schedule_id() -> int:
    global _SCHEDULE_COUNTER
    _SCHEDULE_COUNTER += 1
    return _SCHEDULE_COUNTER

# Initialize DB tables + load any persisted schedules/tokens so that
# background jobs (cron) can run after server restart.
init_db()
load_tokens_from_db()
load_schedules_from_db()

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

def _safe_decrypt(value: str) -> str:
    """Decrypt a stored secret, tolerating legacy PLAINTEXT rows.

    OAuth1 secrets (oauth_token / oauth_token_secret) are now encrypted at rest, but
    rows written before this change hold raw plaintext that isn't valid Fernet
    ciphertext. Unlike decrypt() (which returns "" on failure), this returns the value
    unchanged so those legacy rows keep working; they get re-encrypted on next login.
    """
    if not value:
        return ""
    try:
        return FERNET.decrypt(value.encode()).decode()
    except Exception:
        return value

# --------------------------------------------------------------------------------------
# OAuth 1.0a helpers (for X Ads API and also usable for X API v2 user context)
# We use a minimal HMAC-SHA1 implementation so we don't need extra heavy OAuth libs.
# --------------------------------------------------------------------------------------

def _oauth1_percent_encode(s: str) -> str:
    return quote(str(s), safe="~")

def _oauth1_sign(method: str, url: str, params: dict, consumer_secret: str, token_secret: str = "") -> str:
    """Create OAuth1 signature base string and sign it."""
    sorted_params = sorted((k, v) for k, v in params.items())
    param_string = "&".join(
        f"{_oauth1_percent_encode(k)}={_oauth1_percent_encode(v)}" for k, v in sorted_params
    )
    base_string = "&".join([
        method.upper(),
        _oauth1_percent_encode(url),
        _oauth1_percent_encode(param_string),
    ])
    signing_key = f"{_oauth1_percent_encode(consumer_secret)}&{_oauth1_percent_encode(token_secret or '')}"
    digest = hmac.new(signing_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")

def _build_oauth1_header(params: dict) -> str:
    """Build the Authorization: OAuth ... header."""
    # Only oauth_* params go in the header
    oauth_params = {k: v for k, v in params.items() if k.startswith("oauth_")}
    sorted_items = sorted(oauth_params.items())
    parts = [f'{k}="{_oauth1_percent_encode(v)}"' for k, v in sorted_items]
    return "OAuth " + ", ".join(parts)

def make_oauth1_header(
    method: str,
    url: str,
    consumer_key: str,
    consumer_secret: str,
    oauth_token: str,
    oauth_token_secret: str,
    extra_params: dict | None = None,
) -> str:
    """Return a ready-to-use Authorization header for OAuth1 signed request."""
    params: dict = {
        "oauth_consumer_key": consumer_key,
        "oauth_token": oauth_token,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_nonce": secrets.token_urlsafe(16),
        "oauth_version": "1.0",
    }
    if extra_params:
        params.update(extra_params)

    signature = _oauth1_sign(method, url, params, consumer_secret, oauth_token_secret)
    params["oauth_signature"] = signature
    return _build_oauth1_header(params)

# Convenience wrappers for our token storage shape
def get_oauth1_headers(method: str, url: str, user: dict, extra_params: dict | None = None) -> dict:
    """Build headers dict with Authorization for a user dict that has oauth_token + secret."""
    token = user.get("oauth_token") or user.get("access_token")  # tolerate old name during transition
    secret = user.get("oauth_token_secret") or ""
    if not token:
        raise HTTPException(401, "No OAuth1 token for user")
    auth = make_oauth1_header(
        method,
        url,
        X_CONSUMER_KEY,
        X_CONSUMER_SECRET,
        token,
        secret,
        extra_params,
    )
    return {"Authorization": auth}

# --------------------------------------------------------------------------------------
# Auth / Session (signed cookie carries the x_user_id)
# --------------------------------------------------------------------------------------

def create_session_cookie(x_user_id: str) -> str:
    return signer.dumps({"xuid": x_user_id})

def _xuid_from_cookie(request: Request) -> Optional[str]:
    raw = request.cookies.get("session")
    if not raw:
        return None
    try:
        data = signer.loads(raw)
    except BadSignature:
        return None
    return data.get("xuid") or None

def _build_user_dict(xuid: str) -> Dict[str, Any]:
    """Build the runtime user dict. TOKENS holds encrypted values (both OAuth1 and
    OAuth2); we decrypt here so the returned dict carries PLAINTEXT secrets suitable
    for signing helpers / API calls. _safe_decrypt tolerates legacy plaintext rows."""
    profile = USERS.get(xuid, {})
    tok = TOKENS.get(xuid, {})
    return {
        "id": xuid,
        "x_user_id": xuid,
        "username": profile.get("username"),
        "name": profile.get("name"),
        "profile_image_url": profile.get("profile_image_url"),
        # OAuth2 style (legacy)
        "access_token": decrypt(tok.get("access_token", "")),
        "refresh_token": decrypt(tok.get("refresh_token", "")),
        # OAuth1 (preferred for Ads) — decrypt at point of use
        "oauth_token": _safe_decrypt(tok.get("oauth_token")) or decrypt(tok.get("access_token", "")),
        "oauth_token_secret": _safe_decrypt(tok.get("oauth_token_secret")),
        "expires_at": tok.get("expires_at", 0),
        "scope": tok.get("scope"),
    }

def get_user_from_cookie(request: Request) -> Optional[Dict[str, Any]]:
    """Synchronous cookie->user lookup from the in-memory caches (no network).
    Returns None on cache-miss; callers that must survive a restart use the async
    rehydrate_user() instead."""
    xuid = _xuid_from_cookie(request)
    if not xuid:
        return None
    if xuid not in USERS:
        return None
    return _build_user_dict(xuid)

def clear_session_cookie(response: Response):
    response.delete_cookie("session", path="/")

async def rehydrate_user(request: Request) -> Optional[Dict[str, Any]]:
    """Resolve the session cookie to a user, re-fetching the public profile from X
    on cache-miss. Profile PII is no longer persisted, so after a restart USERS is
    empty; if a persisted token exists we re-fetch the profile and cache it IN MEMORY
    ONLY (never persisted). On re-fetch failure we degrade gracefully to a minimal
    user dict (id + tokens) so a valid session isn't hard-broken, and we don't cache
    the miss so a later request can retry."""
    xuid = _xuid_from_cookie(request)
    if not xuid or xuid not in TOKENS:
        return None
    if xuid not in USERS:
        base = _build_user_dict(xuid)
        try:
            profile = await fetch_x_profile(base)
            USERS[xuid] = {
                "x_user_id": xuid,
                "username": profile.get("username"),
                "name": profile.get("name"),
                "profile_image_url": profile.get("profile_image_url"),
            }
        except Exception as e:
            print(f"[rehydrate_user] profile re-fetch failed for {xuid}: {e!r}")
            return base
    return _build_user_dict(xuid)

async def require_user(request: Request) -> Dict[str, Any]:
    user = await rehydrate_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user

# --------------------------------------------------------------------------------------
# X / Ads HTTP helpers
# --------------------------------------------------------------------------------------

async def x_get(url: str, access_token: str, params: Optional[Dict] = None, oauth_token_secret: Optional[str] = None) -> Dict[str, Any]:
    """Supports both:
    - OAuth2: pass access_token (Bearer)
    - OAuth1: pass access_token as the oauth_token + oauth_token_secret
    """
    query_params = params or {}
    if oauth_token_secret:
        # OAuth1 signed — must include query params in the signature base string
        headers = {
            "Authorization": make_oauth1_header(
                "GET", url, X_CONSUMER_KEY, X_CONSUMER_SECRET, access_token, oauth_token_secret,
                extra_params=query_params
            )
        }
    else:
        headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=headers, params=query_params)
        if r.status_code == 401:
            raise HTTPException(status_code=401, detail="X token expired or invalid")
        r.raise_for_status()
        return r.json()

async def ads_get(path: str, access_token: str, params: Optional[Dict] = None, oauth_token_secret: Optional[str] = None) -> Dict[str, Any]:
    """Ads API calls — must use OAuth1 user context."""
    url = f"{ADS_BASE}{path}"
    query_params = params or {}
    if oauth_token_secret:
        headers = {
            "Authorization": make_oauth1_header(
                "GET", url, X_CONSUMER_KEY, X_CONSUMER_SECRET, access_token, oauth_token_secret,
                extra_params=query_params
            )
        }
    else:
        headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=headers, params=query_params)
        if r.status_code in (401, 403):
            try:
                detail = r.json()
            except Exception:
                detail = r.text or r.content.decode(errors="ignore") or "(no body)"
            raise HTTPException(status_code=r.status_code, detail=f"Ads API error (HTTP {r.status_code} on {path}): {detail}")
        r.raise_for_status()
        return r.json()

async def ads_put(path: str, access_token: str, payload: Dict[str, Any], oauth_token_secret: Optional[str] = None) -> Dict[str, Any]:
    url = f"{ADS_BASE}{path}"
    if oauth_token_secret:
        headers = {
            "Authorization": make_oauth1_header("PUT", url, X_CONSUMER_KEY, X_CONSUMER_SECRET, access_token, oauth_token_secret),
            "Content-Type": "application/json",
        }
    else:
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    # Debug: log the exact outgoing request for card updates
    print(f"[ADS-PUT] Sending PUT {url}")
    try:
        print(f"[ADS-PUT] Outgoing body (exact, compact): {json.dumps(payload, ensure_ascii=False)}")
        print("[ADS-PUT] (pretty version printed above in [CARD-UPDATE] block when available)")
    except Exception:
        print(f"[ADS-PUT] Outgoing body (repr): {repr(payload)}")

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.put(url, headers=headers, json=payload)
        if r.status_code in (401, 403):
            try:
                detail = r.json()
            except Exception:
                detail = r.text or r.content.decode(errors="ignore") or "(no body)"
            raise HTTPException(status_code=r.status_code, detail=f"Ads API error (HTTP {r.status_code} on {path}): {detail}")
        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise HTTPException(status_code=r.status_code, detail=f"Ads update failed: {detail}")
        return r.json() if r.text else {"ok": True}

async def ads_post(path: str, access_token: str, params: Optional[Dict] = None, oauth_token_secret: Optional[str] = None) -> Dict[str, Any]:
    """Ads API POST — OAuth1 user context.

    Like the Ads media_library "add" call, parameters are sent as the query string
    (not a JSON/form body), so they ARE part of the OAuth signature base string,
    exactly like the ads_get query params.
    """
    url = f"{ADS_BASE}{path}"
    query_params = params or {}
    if oauth_token_secret:
        headers = {
            "Authorization": make_oauth1_header(
                "POST", url, X_CONSUMER_KEY, X_CONSUMER_SECRET, access_token, oauth_token_secret,
                extra_params=query_params
            )
        }
    else:
        headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=headers, params=query_params)
        if r.status_code in (401, 403):
            try:
                detail = r.json()
            except Exception:
                detail = r.text or r.content.decode(errors="ignore") or "(no body)"
            raise HTTPException(status_code=r.status_code, detail=f"Ads API error (HTTP {r.status_code} on {path}): {detail}")
        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise HTTPException(status_code=r.status_code, detail=f"Ads POST failed (HTTP {r.status_code} on {path}): {detail}")
        return r.json() if r.text else {"ok": True}

# --------------------------------------------------------------------------------------
# Media upload helpers (X API v2 chunked upload — different host than the Ads API)
#
# SIGNING SUBTLETY (OAuth1.0a):
#   - INIT sends a JSON body and APPEND sends multipart/form-data. For OAuth1, neither
#     a JSON body nor multipart form fields (including the binary `media` part and the
#     `segment_index` field) are included in the signature base string. Only the
#     oauth_* params (plus any genuine query-string params, e.g. STATUS's media_id)
#     are signed. So we call make_oauth1_header WITHOUT extra_params for
#     INIT/APPEND/FINALIZE, and WITH extra_params only for the STATUS GET.
# --------------------------------------------------------------------------------------

def _upload_auth_header(method: str, url: str, access_token: str, oauth_token_secret: str, extra_params: Optional[Dict] = None) -> str:
    return make_oauth1_header(
        method, url, X_CONSUMER_KEY, X_CONSUMER_SECRET, access_token, oauth_token_secret,
        extra_params=extra_params,
    )

def _raise_upload_error(r: httpx.Response, step: str):
    try:
        detail = r.json()
    except Exception:
        detail = r.text or "(no body)"
    raise HTTPException(status_code=r.status_code, detail=f"Media upload {step} failed (HTTP {r.status_code}): {detail}")

def _categorize_upload(mime: str, filename: str) -> Tuple[str, str, str]:
    """Return (media_category, app_media_type, mime) for the Ads/X upload.

    app_media_type follows the app convention where GIF counts as "video"
    (consistent with _normalize_media_type).
    """
    m = (mime or "").lower()
    fn = (filename or "").lower()
    if "gif" in m or fn.endswith(".gif"):
        return "TWEET_GIF", "video", (m or "image/gif")
    if m.startswith("image/") or fn.endswith((".jpg", ".jpeg", ".png", ".webp")):
        return "TWEET_IMAGE", "image", (m or "image/jpeg")
    if m.startswith("video/") or fn.endswith((".mp4", ".mov", ".m4v", ".webm")):
        # AMPLIFY_VIDEO is the correct category for videos used in Ads creatives.
        return "AMPLIFY_VIDEO", "video", (m or "video/mp4")
    raise HTTPException(422, detail="Unsupported file type. Please upload an image, GIF, or video.")

async def _media_upload_chunked(
    client: httpx.AsyncClient,
    file: UploadFile,
    access_token: str,
    oauth_token_secret: str,
    mime: str,
    media_category: str,
) -> Tuple[str, Optional[str], Dict[str, Any]]:
    """Run INIT -> APPEND(chunks) -> FINALIZE. Returns (media_id, media_key, finalize_data)."""
    raw = file.file  # underlying SpooledTemporaryFile (sync); avoids loading whole file in memory
    raw.seek(0, os.SEEK_END)
    total_bytes = raw.tell()
    raw.seek(0)
    if total_bytes <= 0:
        raise HTTPException(422, detail="Uploaded file is empty.")

    # Reject oversize uploads before streaming any bytes to X.
    is_image = media_category == "TWEET_IMAGE"
    max_bytes = MAX_IMAGE_UPLOAD_BYTES if is_image else MAX_VIDEO_UPLOAD_BYTES
    if total_bytes > max_bytes:
        limit_mb = max_bytes // (1024 * 1024)
        kind = "image" if is_image else "video/GIF"
        raise HTTPException(
            status_code=413,
            detail=f"File too large: {kind} uploads are limited to {limit_mb} MB.",
        )

    # --- INIT (JSON body; body not part of OAuth signature) ---
    init_url = f"{X_UPLOAD_BASE}/initialize"
    headers = {
        "Authorization": _upload_auth_header("POST", init_url, access_token, oauth_token_secret),
        "Content-Type": "application/json",
    }
    init_body = {"media_type": mime, "total_bytes": total_bytes, "media_category": media_category}
    r = await client.post(init_url, headers=headers, json=init_body)
    if r.status_code >= 400:
        _raise_upload_error(r, "init")
    init_data = (r.json() or {}).get("data") or r.json() or {}
    media_id = str(init_data.get("id") or init_data.get("media_id") or init_data.get("media_id_string") or "")
    media_key = init_data.get("media_key")
    if not media_id:
        raise HTTPException(502, detail=f"Media upload INIT did not return a media id: {init_data}")

    # --- APPEND (multipart; neither `media` nor `segment_index` are signed) ---
    append_url = f"{X_UPLOAD_BASE}/{media_id}/append"
    segment_index = 0
    while True:
        chunk = raw.read(UPLOAD_CHUNK_SIZE)
        if not chunk:
            break
        headers = {"Authorization": _upload_auth_header("POST", append_url, access_token, oauth_token_secret)}
        files = {"media": ("blob", chunk, "application/octet-stream")}
        data = {"segment_index": str(segment_index)}
        r = await client.post(append_url, headers=headers, data=data, files=files)
        if r.status_code >= 400:
            _raise_upload_error(r, f"append (segment {segment_index})")
        segment_index += 1

    # --- FINALIZE (no body) ---
    finalize_url = f"{X_UPLOAD_BASE}/{media_id}/finalize"
    headers = {"Authorization": _upload_auth_header("POST", finalize_url, access_token, oauth_token_secret)}
    r = await client.post(finalize_url, headers=headers)
    if r.status_code >= 400:
        _raise_upload_error(r, "finalize")
    fin = r.json() if r.text else {}
    fin_data = (fin.get("data") or fin) if isinstance(fin, dict) else {}
    media_key = media_key or fin_data.get("media_key")
    return media_id, media_key, fin_data

async def _poll_upload_status(
    client: httpx.AsyncClient,
    media_id: str,
    access_token: str,
    oauth_token_secret: str,
    max_wait: float = 90.0,
) -> Tuple[str, Dict[str, Any]]:
    """Poll STATUS until succeeded/failed or max_wait elapses.

    Returns (state, data) where state is one of "succeeded" | "failed" | "processing".
    STATUS is a GET with a real query param (media_id), so it IS part of the signature.
    """
    deadline = time.time() + max_wait
    last: Dict[str, Any] = {}
    while True:
        params = {"media_id": media_id}
        headers = {"Authorization": _upload_auth_header("GET", X_UPLOAD_BASE, access_token, oauth_token_secret, extra_params=params)}
        r = await client.get(X_UPLOAD_BASE, headers=headers, params=params)
        if r.status_code >= 400:
            _raise_upload_error(r, "status")
        d = r.json() if r.text else {}
        data = (d.get("data") or d) if isinstance(d, dict) else {}
        last = data
        pinfo = data.get("processing_info") or {}
        state = (pinfo.get("state") or "").lower()
        if not pinfo or state == "succeeded":
            return "succeeded", data
        if state == "failed":
            return "failed", data
        if time.time() >= deadline:
            return "processing", data
        wait_secs = int(pinfo.get("check_after_secs") or 3)
        await asyncio.sleep(max(1.0, min(float(wait_secs), deadline - time.time())))

def _map_library_status(raw_status: Optional[str]) -> str:
    """Map an Ads media_library media_status to processing|succeeded|failed."""
    s = (raw_status or "").upper()
    if "FAIL" in s or "ERROR" in s:
        return "failed"
    if any(tok in s for tok in ("PROGRESS", "PENDING", "PROCESS", "TRANSCOD")) and "COMPLET" not in s:
        return "processing"
    # ACTIVE / READY / SUCCEEDED / COMPLETED / TRANSCODE_COMPLETED / empty -> ready
    return "succeeded"

def _normalize_library_item(m: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize one Ads media_library entry to the frontend contract shape."""
    if not isinstance(m, dict):
        m = {}
    app_type = _normalize_media_type(m) or "image"
    thumbnail = None
    if app_type == "image":
        thumbnail = m.get("media_url") or m.get("media_url_https") or m.get("poster_media_url")
    else:
        thumbnail = m.get("poster_media_url") or m.get("video_poster_url") or m.get("media_url")
    return {
        "media_key": m.get("media_key") or m.get("id") or "",
        "media_type": app_type,
        "name": m.get("name") or m.get("title") or m.get("file_name") or "",
        "file_name": m.get("file_name") or m.get("name") or "",
        "created_at": m.get("created_at"),
        "media_status": _map_library_status(m.get("media_status") or m.get("status")),
        "thumbnail": thumbnail,
        "aspect_ratio": _extract_api_aspect(m),
    }

def _encode_cursor(cursors: Dict[str, str]) -> Optional[str]:
    if not cursors:
        return None
    return base64.urlsafe_b64encode(json.dumps(cursors).encode()).decode()

def _decode_cursor(cursor: Optional[str]) -> Dict[str, str]:
    """Decode our composite cursor (used so VIDEO+GIF can paginate together).

    Falls back to treating the value as a single raw API cursor if it isn't ours.
    """
    if not cursor:
        return {}
    try:
        obj = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        if isinstance(obj, dict):
            return {str(k): str(v) for k, v in obj.items() if v}
    except Exception:
        pass
    return {"_raw": cursor}

async def refresh_x_token_if_needed(user: Dict[str, Any]) -> str:
    """Return a usable token.

    - For OAuth1 users: just return the oauth_token (no refresh concept).
    - For legacy OAuth2 users: do the refresh dance.
    """
    # Pure OAuth1 path
    if user.get("oauth_token_secret"):
        # For callers that expect a "access_token" string we return the oauth_token
        return user.get("oauth_token") or user.get("access_token") or ""

    now = time.time()
    if user.get("expires_at", 0) > now + 60:
        return user.get("access_token", "")

    refresh = user.get("refresh_token")
    if not refresh:
        # No refresh available — return whatever we have (may be stale, caller will fail with 401/403)
        return user.get("access_token", "")

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": X_CLIENT_ID,
    }
    auth = (X_CLIENT_ID, X_CLIENT_SECRET) if X_CLIENT_SECRET else None

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            X_OAUTH2_TOKEN_URL,
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
    """OAuth2 path (Bearer)."""
    params = {"user.fields": "id,username,name,profile_image_url"}
    data = await x_get(X_USERS_ME_URL, access_token, params)
    u = data.get("data", {})
    return {
        "x_user_id": u.get("id"),
        "username": u.get("username"),
        "name": u.get("name"),
        "profile_image_url": u.get("profile_image_url"),
    }

async def fetch_user_oauth1(oauth_token: str, oauth_token_secret: str) -> Dict[str, Any]:
    """OAuth1 path — fetch profile using the same v2 /users/me endpoint (with OAuth1 signing)."""
    params = {"user.fields": "id,username,name,profile_image_url"}
    data = await x_get(X_USERS_ME_URL, oauth_token, params, oauth_token_secret=oauth_token_secret)
    u = data.get("data", {})
    return {
        "x_user_id": str(u.get("id") or u.get("x_user_id") or ""),
        "username": u.get("username"),
        "name": u.get("name"),
        "profile_image_url": u.get("profile_image_url"),
    }

async def fetch_x_profile(user: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch a user's public profile from X, dispatching on token type.

    Accepts a runtime user dict (with decrypted tokens). Used to rehydrate the
    in-memory USERS cache on demand since profile PII is no longer persisted.
    """
    secret = user.get("oauth_token_secret") or ""
    if secret:
        return await fetch_user_oauth1(user.get("oauth_token") or "", secret)
    access = await refresh_x_token_if_needed(user)
    return await fetch_x_user(access)

# --------------------------------------------------------------------------------------
# Card / Tweet helpers + media type detection
# --------------------------------------------------------------------------------------

TWEET_URL_RE = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/[^/]+/status/(\d+)", re.I)

def extract_tweet_id(url: str) -> Optional[str]:
    m = TWEET_URL_RE.search(url or "")
    return m.group(1) if m else None

async def validate_tweet_has_card(access_token: str, tweet_id: str, oauth_token_secret: Optional[str] = None) -> Dict[str, Any]:
    """
    STEP 1 — Validate the post:

    - Does the post exist and is it readable with the current token?
    - Does it contain a website card or app card (via the presence of card_uri)?

    This is ONLY the X API v2 check. It does not touch the Ads API.

    Endpoint:
      GET https://api.x.com/2/tweets/{tweet_id}
    """
    params = {
        "tweet.fields": "card_uri,attachments,author_id,created_at,text,entities",
        "expansions": "author_id",
        "user.fields": "username",
    }
    try:
        data = await x_get(f"{X_TWEETS_URL}/{tweet_id}", access_token, params, oauth_token_secret=oauth_token_secret)
    except HTTPException as e:
        if e.status_code == 404:
            raise HTTPException(422, detail="Post not found. It may have been deleted, is private, or the signed-in X account does not have access to it.")
        raise

    tweet = data.get("data", {})
    card_uri = tweet.get("card_uri")

    if not card_uri or not str(card_uri).startswith("card://"):
        raise HTTPException(
            status_code=422,
            detail="This post exists but does not contain a website card or app card"
        )

    card_id = str(card_uri).replace("card://", "")
    return {"tweet": tweet, "card_id": card_id, "raw": data}

def parse_card_response(card_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Parse the response from the direct Ads card endpoint:

      GET /accounts/{account_id}/cards/{card_id}

    Modern responses (v12) use a "components" array:

    {
      "data": {
        "id": "...",
        "card_uri": "card://...",
        "card_type": "IMAGE_WEBSITE",
        "components": [
          { "type": "MEDIA", "media_key": "3_xxxx", "media_metadata": { "3_xxxx": { "type": "IMAGE", "url": "...", "width": 737, "height": 386 } } } },
          { "type": "DETAILS", "title": "...", "destination": { "url": "...", "type": "WEBSITE" } }
        ],
        ...
      }
    }

    We also keep fallbacks for older flat response shapes.
    """
    if not isinstance(card_json, dict):
        card_json = {}

    top = card_json.get("data") or card_json
    card = top if isinstance(top, dict) else {}

    # card id can come from several places
    card_id = str(
        card.get("id")
        or card.get("card_id")
        or (card_json.get("request", {}).get("params", {}) or {}).get("card_id")
        or ""
    )

    # Try modern components structure first
    title = ""
    url = ""
    media_id = ""
    media_type = None
    width = None
    height = None
    preview_url = None
    card_aspect = None

    components = card.get("components") or []
    for comp in components if isinstance(components, list) else []:
        ctype = str(comp.get("type") or "").upper()

        if ctype == "DETAILS":
            title = comp.get("title") or title
            dest = comp.get("destination") or {}
            if isinstance(dest, dict):
                url = dest.get("url") or url

        if ctype == "MEDIA":
            mk = comp.get("media_key") or ""
            if mk:
                media_id = mk
            meta = comp.get("media_metadata") or {}
            # media_metadata is usually keyed by the media_key
            mdata = {}
            if mk and isinstance(meta, dict) and mk in meta:
                mdata = meta[mk] or {}
            elif isinstance(meta, dict):
                # fallback: take the first entry
                for _k, _v in meta.items():
                    if isinstance(_v, dict):
                        mdata = _v
                        if not media_id:
                            media_id = _k
                        break

            if mdata:
                mtype = str(mdata.get("type") or "").upper()
                if "VIDEO" in mtype:
                    media_type = "video"
                elif "IMAGE" in mtype:
                    media_type = "image"
                width = mdata.get("width") or width
                height = mdata.get("height") or height
                preview_url = mdata.get("url") or preview_url
                # Prefer aspect ratio returned by the API in the media metadata
                if not card_aspect:
                    card_aspect = _extract_api_aspect(mdata)
                # For videos, prefer a poster/thumbnail image URL (not the video asset) so <img> preview works
                if media_type == "video":
                    poster = (
                        mdata.get("poster")
                        or mdata.get("poster_url")
                        or mdata.get("preview_image")
                        or mdata.get("thumbnail_url")
                        or mdata.get("preview_url")
                    )
                    if poster:
                        preview_url = poster

    # Fallbacks for older/flat Ads card shapes
    if not title:
        title = card.get("name") or card.get("title") or card.get("headline") or card.get("card_name") or ""

    # Look for explicit video poster at top level (common for video cards)
    if (media_type == "video" or not preview_url):
        for k in ("video_poster_url", "poster_url", "poster_image_url", "thumbnail_url", "poster"):
            val = card.get(k)
            if val:
                preview_url = val
                break

    # Prefer aspect ratio provided by the Ads API at the card or top level
    if not card_aspect:
        card_aspect = _extract_api_aspect(card)

    # Search inside components for video_poster_url (sometimes attached to MEDIA comp or its metadata)
    if (media_type == "video" or not preview_url) and isinstance(components, list):
        for comp in components:
            if not isinstance(comp, dict):
                continue
            for pk in ("video_poster_url", "poster_url", "poster_image_url", "poster", "thumbnail_url"):
                if comp.get(pk):
                    preview_url = comp.get(pk)
                    break
            if preview_url:
                break
            meta = comp.get("media_metadata") or {}
            if isinstance(meta, dict):
                for _mk, _mv in meta.items():
                    if isinstance(_mv, dict):
                        for pk in ("video_poster_url", "poster_url", "poster", "thumbnail_url", "preview_image"):
                            if _mv.get(pk):
                                preview_url = _mv.get(pk)
                                break
                    if preview_url:
                        break
            if preview_url:
                break

    # Deep scan of the entire raw card response for any *poster* image url (last resort for video)
    if (media_type == "video" or not preview_url) and isinstance(card_json, dict):
        def _find_first_poster(d, depth=0):
            if depth > 5 or not isinstance(d, (dict, list)):
                return None
            if isinstance(d, list):
                for item in d:
                    f = _find_first_poster(item, depth + 1)
                    if f:
                        return f
                return None
            # dict
            for kk, vv in d.items():
                klow = str(kk).lower()
                if isinstance(vv, str) and any(p in klow for p in ("poster", "thumb")):
                    if any(vv.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")) or "pbs.twimg" in vv or "video" in klow:
                        return vv
                if isinstance(vv, (dict, list)):
                    f = _find_first_poster(vv, depth + 1)
                    if f:
                        return f
            return None
        found = _find_first_poster(card_json)
        if found:
            preview_url = found

    if not url:
        url = (
            card.get("website_url")
            or card.get("url")
            or card.get("destination_url")
            or card.get("final_url")
            or card.get("android_url")
            or card.get("iphone_url")
            or card.get("ipad_url")
            or ""
        )

    if not media_id:
        media_id = (
            card.get("image_media_id")
            or card.get("video_media_id")
            or card.get("media_id")
            or card.get("media_key")
            or card.get("preview_media_id")
            or card.get("thumbnail_media_id")
            or card.get("media")
            or ""
        )

    card_type_raw = card.get("card_type") or ""
    card_type = "website"
    if "app" in str(card_type_raw).lower() or "app_download" in str(card_json).lower():
        card_type = "app"

    inferred = media_type
    if not inferred:
        if "video" in str(card_type_raw).lower() or "video_media_id" in card:
            inferred = "video"
        elif "image" in str(card_type_raw).lower() or "image_media_id" in card:
            inferred = "image"

    result = {
        "id": card_id,
        "title": title or "",
        "url": url or "",
        "media_id": media_id or "",
        "card_type": card_type,
        "inferred_media_type": inferred,
        "original_media_type": media_type or inferred,
        "original_media_width": width,
        "original_media_height": height,
        "original_aspect_ratio": card_aspect,   # from API (media_metadata or card), do not derive
        "media_preview": preview_url,
        "raw": card_json,
    }

    return result


def build_card_update_payload(
    current_raw: Dict[str, Any],
    new_title: str,
    new_url: str,
    new_media_id: str,
    new_media_type: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build the exact body to PUT when updating a card.

    Strategy:
    - Start from the FULL raw response returned by GET /accounts/{account}/cards/{card_id}
      (or its "data" sub-object). This preserves the exact structure the API uses
      (components array, card_type, other metadata, etc.).
    - Do NOT start from a minimal dict.
    - Then replace only the user-controlled values:
        * title / name
        * destination URL
        * media reference (in components and common flat fields)
    - We clean a few read-only fields (id, card_uri, timestamps) that the API
      rejects on update, but keep everything else exactly as it was.
    """
    if not isinstance(current_raw, dict):
        current_raw = {}

    # The card object we can mutate is usually under "data" for the single-card GET.
    card = current_raw.get("data") if isinstance(current_raw.get("data"), dict) else current_raw
    if not isinstance(card, dict):
        card = {}

    # Deep copy so we don't mutate the original fetched object / caches.
    body: Dict[str, Any] = copy.deepcopy(card)

    # --- Replace title / name (visible title + Ads manager name) ---
    if new_title is not None:
        # Top-level fields used by some card shapes
        for k in ("name", "title", "headline"):
            if k in body or k == "name":
                body[k] = new_title

        # Modern components: DETAILS component carries the title
        comps = body.get("components")
        if isinstance(comps, list):
            for comp in comps:
                if isinstance(comp, dict) and str(comp.get("type") or "").upper() == "DETAILS":
                    comp["title"] = new_title

    # --- Replace destination URL ---
    if new_url is not None:
        # Flat legacy fields
        for k in ("website_url", "url", "destination_url", "final_url"):
            if k in body:
                body[k] = new_url

        # Components: DETAILS.destination.url
        comps = body.get("components")
        if isinstance(comps, list):
            for comp in comps:
                if isinstance(comp, dict) and str(comp.get("type") or "").upper() == "DETAILS":
                    dest = comp.get("destination") or {}
                    if isinstance(dest, dict):
                        dest["url"] = new_url
                        comp["destination"] = dest
                    else:
                        comp["destination"] = {"url": new_url, "type": dest.get("type") if isinstance(dest, dict) else "WEBSITE"}

    # --- Replace media reference ---
    if new_media_id is not None:
        mt = (new_media_type or "").lower()

        # Prefer setting the correct flat key based on stored type or what already exists in the card
        target_flat = None
        if "video" in mt:
            target_flat = "video_media_id"
        elif "image" in mt:
            target_flat = "image_media_id"

        # Update any existing media keys in the body to keep structure
        for flat in ("image_media_id", "video_media_id", "media_id", "media_key"):
            if flat in body:
                body[flat] = new_media_id
                if target_flat is None:
                    target_flat = flat

        if target_flat:
            body[target_flat] = new_media_id
        elif not any(k in body for k in ("image_media_id", "video_media_id", "media_id", "media_key")):
            # default
            body["image_media_id"] = new_media_id

        # Modern components: MEDIA component + its media_metadata
        comps = body.get("components")
        if isinstance(comps, list):
            for comp in comps:
                if isinstance(comp, dict) and str(comp.get("type") or "").upper() == "MEDIA":
                    old_key = comp.get("media_key")
                    comp["media_key"] = new_media_id

                    meta = comp.get("media_metadata") or {}
                    if isinstance(meta, dict):
                        # Move the metadata entry to the new key if it existed under old key
                        if old_key and old_key in meta:
                            val = meta.pop(old_key)
                            meta[new_media_id] = val
                        # If no previous metadata, we leave it; the media_id change is the main thing
                        comp["media_metadata"] = meta

    # Remove fields that are read-only / not accepted on PUT.
    # We omit as little as possible — only the ones that reliably cause 400/403 on update.
    for ro_key in ("id", "card_uri", "card_id", "created_at", "updated_at", "deleted", "preview_url"):
        body.pop(ro_key, None)

    # Ensure we still have a name at minimum (some cards require it)
    if not body.get("name") and new_title:
        body["name"] = new_title

    return body


async def fetch_card_details(access_token: str, ads_account_id: str, card_id: str, oauth_token_secret: Optional[str] = None) -> Dict[str, Any]:
    try:
        data = await ads_get(f"/accounts/{ads_account_id}/cards/{card_id}", access_token, oauth_token_secret=oauth_token_secret)
        parsed = parse_card_response(data)
        if parsed.get("id"):
            return parsed
    except HTTPException as e:
        if e.status_code not in (404, 400):
            raise

    try:
        listed = await ads_get(f"/accounts/{ads_account_id}/cards", access_token, {"count": 200}, oauth_token_secret=oauth_token_secret)
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
            listed = await ads_get(f"/accounts/{ads_account_id}{suffix}", access_token, {"count": 200}, oauth_token_secret=oauth_token_secret)
            for it in (listed.get("data") or []):
                if str(it.get("id")) == str(card_id):
                    return parse_card_response({"data": it})
        except Exception:
            continue

    raise HTTPException(status_code=404, detail="Could not retrieve card details from Ads API. Check account access and card id.")

async def fetch_media_info(access_token: str, ads_account_id: str, media_id: str, oauth_token_secret: Optional[str] = None) -> Dict[str, Any]:
    """
    Return info for a media key using the direct media_library path endpoint:
      GET /accounts/{account_id}/media_library/{media_key}
    The response provides media type, aspect_ratio, and poster_media_url (for preview).
    Dimensions are not guaranteed by this endpoint.
    """
    if not media_id:
        return {"width": None, "height": None, "preview": None, "media_type": None, "aspect_ratio": None, "aspect": None}

    # Primary: direct lookup by path (media key in URL)
    # https://ads-api.x.com/12/accounts/{account_id}/media_library/{media_key}
    try:
        data = await ads_get(
            f"/accounts/{ads_account_id}/media_library/{media_id}",
            access_token,
            None,  # no query params; key is in the path
            oauth_token_secret=oauth_token_secret,
        )
        # Single-resource responses are typically { "data": { ... } }
        m = data.get("data") if isinstance(data, dict) else None
        if isinstance(m, list):
            m = m[0] if m else {}
        if m and isinstance(m, dict):
            w = m.get("width") or m.get("original_width")
            h = m.get("height") or m.get("original_height")
            # Prefer poster_media_url as requested for preview
            preview = (
                m.get("poster_media_url")
                or m.get("video_poster_url")
                or m.get("poster_url")
                or m.get("poster_image_url")
                or m.get("thumbnail_url")
                or m.get("preview_url")
                or m.get("media_url_https")
                or m.get("media_url")
                or m.get("preview_image")
            )
            mtype = _normalize_media_type(m)
            if not mtype:
                mtype = _guess_type_from_url(preview or m.get("media_url") or "")
            # For video, ensure we have a usable poster
            if (mtype == "video" or "video" in str(m.get("type", "")).lower()):
                p2 = (
                    m.get("poster_media_url")
                    or m.get("video_poster_url")
                    or m.get("poster_url")
                    or m.get("poster_image_url")
                    or m.get("thumbnail_url")
                    or m.get("preview_image")
                    or preview
                )
                if p2:
                    preview = p2
            api_ar = _extract_api_aspect(m)
            return {
                "width": w, "height": h,
                "preview": preview, "media_type": mtype,
                "aspect_ratio": api_ar,
                "aspect": _aspect_float(w, h) if (api_ar is None and w and h) else None,
            }
    except Exception as e:
        print(f"[media-info] primary lookup failed for media_id={media_id}: {e!r}")

    # Fallback 1: list-style media_library with query param (older style)
    try:
        data = await ads_get(
            f"/accounts/{ads_account_id}/media_library",
            access_token,
            {"media_ids": media_id, "count": 1},
            oauth_token_secret=oauth_token_secret,
        )
        items = data.get("data") or []
        if items:
            m = items[0]
            w = m.get("width") or m.get("original_width")
            h = m.get("height") or m.get("original_height")
            preview = (
                m.get("poster_media_url")
                or m.get("video_poster_url")
                or m.get("poster_url")
                or m.get("poster_image_url")
                or m.get("thumbnail_url")
                or m.get("preview_url")
                or m.get("media_url_https")
            )
            mtype = _normalize_media_type(m)
            if not mtype:
                mtype = _guess_type_from_url(preview or m.get("media_url") or "")
            if (mtype == "video" or "video" in str(m.get("type", "")).lower()):
                p2 = (
                    m.get("poster_media_url")
                    or m.get("video_poster_url")
                    or m.get("poster_url")
                    or m.get("poster_image_url")
                    or m.get("thumbnail_url")
                    or m.get("preview_image")
                    or preview
                )
                if p2:
                    preview = p2
            api_ar = _extract_api_aspect(m)
            return {
                "width": w, "height": h,
                "preview": preview, "media_type": mtype,
                "aspect_ratio": api_ar,
                "aspect": _aspect_float(w, h) if (api_ar is None and w and h) else None,
            }
    except Exception as e:
        print(f"[media-info] fallback media_library query failed for media_id={media_id}: {e!r}")

    # Fallback 2: /media list
    try:
        data = await ads_get(
            f"/accounts/{ads_account_id}/media",
            access_token,
            {"ids": media_id},
            oauth_token_secret=oauth_token_secret,
        )
        items = data.get("data") or []
        if items:
            m = items[0]
            w = m.get("width") or m.get("w")
            h = m.get("height") or m.get("h")
            preview = (
                m.get("poster_media_url")
                or m.get("video_poster_url")
                or m.get("poster_url")
                or m.get("poster_image_url")
                or m.get("thumbnail_url")
                or m.get("preview_url")
                or m.get("media_url")
                or m.get("url")
            )
            mtype = _normalize_media_type(m)
            if not mtype:
                mtype = _guess_type_from_url(preview or "")
            if (mtype == "video" or "video" in str(m.get("type", "")).lower()):
                p2 = (
                    m.get("poster_media_url")
                    or m.get("video_poster_url")
                    or m.get("poster_url")
                    or m.get("poster_image_url")
                    or m.get("thumbnail_url")
                    or m.get("preview_image")
                    or preview
                )
                if p2:
                    preview = p2
            api_ar = _extract_api_aspect(m)
            return {
                "width": w, "height": h,
                "preview": preview, "media_type": mtype,
                "aspect_ratio": api_ar,
                "aspect": _aspect_float(w, h) if (api_ar is None and w and h) else None,
            }
    except Exception as e:
        print(f"[media-info] fallback /media list failed for media_id={media_id}: {e!r}")

    return {"width": None, "height": None, "preview": None, "media_type": None, "aspect_ratio": None, "aspect": None}

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

def _aspect_ratio_str(w: Optional[int], h: Optional[int]) -> Optional[str]:
    if not w or not h:
        return None
    try:
        w = int(w); h = int(h)
        g = math.gcd(w, h)
        return f"{w // g}:{h // g}"
    except Exception:
        return None

def _aspect_float(w: Optional[int], h: Optional[int]) -> Optional[float]:
    if not w or not h:
        return None
    try:
        return round(int(w) / int(h), 4)
    except Exception:
        return None

def aspect_ratios_match(w1, h1, w2, h2, tol: float = 0.02) -> bool:
    r1 = _aspect_float(w1, h1)
    r2 = _aspect_float(w2, h2)
    if r1 is None or r2 is None:
        return False
    return abs(r1 - r2) < tol

def api_aspects_match(o: Dict[str, Any], n: Dict[str, Any], tol: float = 0.02) -> bool:
    """Compare using the aspect_ratio values returned by the API (preferred).

    - If both sides have an aspect_ratio (string), normalize and compare the ratios.
    - Normalization supports "16:9", "1.777...", "16/9" etc.
    - Exact pixel dimensions are NEVER used to decide an aspect match.
    - No dimension-derived fallback is used to produce a positive match.
    - If aspect_ratio is missing on either side, aspect match is False.
    """
    o = o or {}
    n = n or {}
    oa = o.get("aspect_ratio")
    na = n.get("aspect_ratio")

    if not oa or not na:
        # Strictly use aspect_ratio for the comparison decision.
        # If either side lacks the API-provided aspect_ratio, we do not claim a match on aspect.
        return False

    def norm(a):
        if a is None:
            return None
        s = str(a).strip().lower()
        if ":" in s:
            try:
                p = s.split(":")
                den = float(p[1])
                return float(p[0]) / den if den != 0 else None
            except Exception:
                return None
        if "/" in s:
            try:
                p = s.split("/")
                den = float(p[1])
                return float(p[0]) / den if den != 0 else None
            except Exception:
                return None
        try:
            return float(s)
        except Exception:
            return None

    ro = norm(oa)
    rn = norm(na)
    if ro is not None and rn is not None:
        return abs(ro - rn) < tol

    # Last resort string equality for the raw aspect values
    return str(oa).strip().lower() == str(na).strip().lower()

def _extract_api_aspect(m: Dict[str, Any]) -> Optional[str]:
    """Return the aspect ratio string exactly as (or normalized from) what the Ads API provided.
    Do NOT derive from width/height here.
    """
    if not isinstance(m, dict):
        return None
    candidates = [
        m.get("aspect_ratio"),
        m.get("aspect"),
        m.get("original_aspect_ratio"),
        m.get("ratio"),
        m.get("ar"),
    ]
    for val in candidates:
        if val is None:
            continue
        if isinstance(val, str) and val.strip():
            s = val.strip()
            if ":" in s:
                return s
            # Some APIs return a float string like "1.7778"; keep as-is for display if provided
            return s
        if isinstance(val, (list, tuple)) and len(val) >= 2:
            try:
                return f"{int(val[0])}:{int(val[1])}"
            except Exception:
                pass
        if isinstance(val, dict):
            # Common shapes: {"numerator": 16, "denominator": 9} or [16,9] already handled
            num = val.get("numerator") or val.get("num") or val.get("w") or val.get(0)
            den = val.get("denominator") or val.get("den") or val.get("h") or val.get(1)
            if num is not None and den is not None:
                try:
                    return f"{int(num)}:{int(den)}"
                except Exception:
                    pass
    return None

# --------------------------------------------------------------------------------------
# Scheduler — date-triggered, not polling
# --------------------------------------------------------------------------------------
# Each pending schedule gets exactly one APScheduler job that fires at scheduled_at.
# No background polling loop; the scheduler thread is idle until a job is due.
# On server restart, start_scheduler() re-adds jobs from the DB so nothing is missed.
# --------------------------------------------------------------------------------------

scheduler: Optional[BackgroundScheduler] = None

# Serializes the read-guard-and-set-to-"running" transition in execute_card_update so a
# DateTrigger job and a manual "Run now" cannot both pass the guard and double-execute.
_execute_lock = threading.Lock()

def _sched_job_id(sid: int) -> str:
    return f"sched-{sid}"

def _run_schedule_job(sid: int):
    """Called by APScheduler in its worker thread when a scheduled time is reached."""
    rec = SCHEDULES.get(sid)
    if not rec:
        return
    try:
        asyncio.run(execute_card_update(rec))
    except Exception as e:
        print(f"[scheduler] execute_card_update failed for schedule {sid}: {e}")

def add_schedule_job(rec: Dict[str, Any]):
    """Register a DateTrigger job for a pending schedule. Safe to call multiple times (replace_existing)."""
    if not scheduler or not scheduler.running:
        return
    sid = int(rec["id"])
    scheduled_at = rec.get("scheduled_at") or 0
    run_date = datetime.fromtimestamp(scheduled_at, tz=timezone.utc)
    # If already past-due (e.g. created with a past time), fire almost immediately.
    now_utc = datetime.now(timezone.utc)
    if run_date <= now_utc:
        run_date = now_utc + timedelta(seconds=1)
    scheduler.add_job(
        _run_schedule_job,
        trigger=DateTrigger(run_date=run_date),
        id=_sched_job_id(sid),
        replace_existing=True,
        args=[sid],
    )
    print(f"[scheduler] job scheduled for sid={sid} at {run_date.isoformat()}")

def remove_schedule_job(sid: int):
    """Cancel the APScheduler job for a schedule (used on cancel or Run Now)."""
    if not scheduler or not scheduler.running:
        return
    job_id = _sched_job_id(sid)
    try:
        scheduler.remove_job(job_id)
        print(f"[scheduler] removed job for sid={sid}")
    except Exception:
        pass  # job may have already fired or never existed

def start_scheduler(app: FastAPI):
    # IMPORTANT — SINGLE-WORKER ONLY.
    # SCHEDULES (in-memory), the module-level `scheduler`, and _execute_lock are all
    # per-process state. This app MUST run with a single worker process
    # (e.g. `uvicorn main:app` WITHOUT `--workers N` and WITHOUT gunicorn multi-worker).
    # Running multiple workers would give each process its own scheduler + in-memory
    # dicts and its own lock, so every schedule would be registered and executed once
    # per worker (duplicate card updates) and cross-process guards would not hold.
    global scheduler
    if scheduler and scheduler.running:
        return
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.start()

    now_ts = time.time()

    # --- Recover schedules left "running" from a previous crash/restart ---
    # A "running" row means a prior process began the update but the process died before
    # it could record success/failure. Since a one-shot card update may have already
    # partially applied on X's side, re-running is risky. We therefore mark these
    # "failed" with a clear message rather than silently re-executing.
    conn = get_db()
    stuck_rows = conn.execute("SELECT * FROM schedules WHERE status = 'running'").fetchall()
    for row in stuck_rows:
        rec = dict(row)
        sid = int(rec["id"])
        rec["status"] = "failed"
        rec["result"] = "Interrupted by server restart. Please review the card and re-schedule if needed."
        rec["executed_at"] = now_ts
        SCHEDULES[sid] = rec
        persist_schedule(rec)
        print(f"[scheduler] recovered stuck-running sid={sid} -> failed")

    # On startup: recover any pending schedules from DB.
    # Past-due ones run immediately (in a background thread); future ones get a DateTrigger job.
    rows = conn.execute("SELECT * FROM schedules WHERE status = 'pending'").fetchall()
    conn.close()
    for row in rows:
        rec = dict(row)
        sid = int(rec["id"])
        SCHEDULES.setdefault(sid, rec)  # hydrate memory if not already there
        if (rec.get("scheduled_at") or 0) <= now_ts:
            # Already past-due — run shortly after startup.
            run_date = datetime.now(timezone.utc) + timedelta(seconds=2)
            scheduler.add_job(
                _run_schedule_job,
                trigger=DateTrigger(run_date=run_date),
                id=_sched_job_id(sid),
                replace_existing=True,
                args=[sid],
            )
            print(f"[scheduler] past-due sid={sid} queued for immediate execution")
        else:
            add_schedule_job(rec)

async def execute_card_update(schedule: Dict[str, Any], force: bool = False):
    sid = schedule["id"]
    sched = SCHEDULES.get(sid)
    if not sched:
        return

    # --- Atomic guard: read-status-and-set-to-"running" under a lock ---
    # A DateTrigger job (scheduler thread) and a manual "Run now" (request thread) can
    # fire concurrently. Evaluating the guards and flipping to "running" inside the lock
    # guarantees only one caller wins the transition; the loser returns immediately.
    # Only the transition is locked — the actual network update runs outside the lock.
    with _execute_lock:
        current_status = sched.get("status")
        # Never stack a second concurrent execution — if already running, let it finish.
        if current_status == "running":
            return
        # Cancelled schedules are never re-run (even via Run Now).
        if current_status == "cancelled":
            return
        # Background scheduler only picks up pending; manual Run Now (force=True) can re-run completed/failed too.
        if not force and current_status != "pending":
            return
        sched["status"] = "running"
        sched["started_at"] = time.time()

    persist_schedule(sched)

    try:
        xuid = schedule["user_id"]
        tok = TOKENS.get(xuid, {})
        if not tok:
            raise RuntimeError("User tokens not found for schedule")

        # Decrypts OAuth1/OAuth2 secrets at point of use (tolerates legacy plaintext).
        user = _build_user_dict(xuid)

        # Use the caller's running event loop directly (no manual new_event_loop).
        # This works whether called via "await" from the API or via asyncio.run() from the scheduler thread.
        access = await refresh_x_token_if_needed(user)

        secret = user.get("oauth_token_secret", "")

        # ------------------------------------------------------------------
        # Fetch the CURRENT full card (exact structure from the Ads API)
        # then build the update body by replacing only the user-supplied
        # new values. We use PUT with that (nearly) identical structure.
        # ------------------------------------------------------------------
        raw_current_card: Dict[str, Any] = {}
        try:
            raw_current_card = await ads_get(
                f"/accounts/{schedule['ads_account_id']}/cards/{schedule['card_id']}",
                access,
                oauth_token_secret=secret,
            )
            print(f"[CARD-UPDATE] Fetched current card structure. top-level keys: {list(raw_current_card.keys()) if isinstance(raw_current_card, dict) else type(raw_current_card)}")
        except Exception as fetch_err:
            # If we cannot fetch, we will fall back to a minimal body (old behavior)
            raw_current_card = {}
            print(f"[CARD-UPDATE] WARNING: Could not fetch current card for full structure: {fetch_err}")

        payload = build_card_update_payload(
            raw_current_card,
            schedule.get("new_title") or "",
            schedule.get("new_url") or "",
            schedule.get("new_media_id") or "",
            schedule.get("new_media_type"),
        )

        # As a safety net, if the builder produced an empty payload, fall back
        # to the previous minimal construction.
        used_full_structure = bool(raw_current_card) and bool(payload)
        if not payload:
            payload = {"name": schedule.get("new_title") or ""}
            url_val = schedule.get("new_url") or ""
            media_val = schedule.get("new_media_id") or ""
            lower_url = (url_val or "").lower()
            if "play.google" in lower_url or "apps.apple" in lower_url or "itunes.apple" in lower_url:
                payload["media_id"] = media_val
                payload["website_url"] = url_val
            else:
                payload["image_media_id"] = media_val
                payload["website_url"] = url_val
            used_full_structure = False

        # === DEBUG LOGGING: Exact payload sent for card update ===
        update_url = f"{ADS_BASE}/accounts/{schedule.get('ads_account_id')}/cards/{schedule.get('card_id')}"
        print("\n" + "=" * 70)
        print("[CARD-UPDATE] === CARD UPDATE (PUT) ===")
        print(f"[CARD-UPDATE] Account : {schedule.get('ads_account_id')}")
        print(f"[CARD-UPDATE] Card ID : {schedule.get('card_id')}")
        print(f"[CARD-UPDATE] Using full fetched structure? {used_full_structure}")
        print(f"[CARD-UPDATE] PUT URL : {update_url}")
        print("[CARD-UPDATE] Exact JSON body being sent:")
        try:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        except Exception:
            print(repr(payload))
        print("=" * 70 + "\n")

        put_resp = await ads_put(
            f"/accounts/{schedule['ads_account_id']}/cards/{schedule['card_id']}",
            access,
            payload,
            oauth_token_secret=secret,
        )

        # Build a clean, human result instead of dumping raw JSON for successful runs
        changes = []
        if schedule.get("new_title") != schedule.get("original_title"):
            changes.append(f'title: "{schedule.get("original_title")}" → "{schedule.get("new_title")}"')
        if schedule.get("new_media_id") != schedule.get("original_media_id"):
            changes.append(f'media key: {schedule.get("original_media_id")} → {schedule.get("new_media_id")}')
        if schedule.get("new_url") != schedule.get("original_url"):
            changes.append(f'url → {schedule.get("new_url")}')
        nice = "Card updated. " + (", ".join(changes) if changes else "No field changes detected.")
        post_link = schedule.get("original_post_url") or schedule.get("new_url")
        if post_link:
            nice += f" Post: {post_link}"
        sched["status"] = "completed"
        sched["result"] = nice[:4000]
        sched["executed_at"] = time.time()
        persist_schedule(sched)

    except Exception as exc:
        sched = SCHEDULES.get(sid)
        if sched:
            sched["status"] = "failed"
            sched["result"] = str(exc)[:4000]
            sched["executed_at"] = time.time()
            persist_schedule(sched)
    finally:
        # Guarantee a terminal state: if execution exited without recording a
        # completed/failed status (e.g. a BaseException or a killed reload), the row
        # would otherwise be stuck "running" forever. Only write if not already terminal.
        final = SCHEDULES.get(sid)
        if final is not None and final.get("status") == "running":
            final["status"] = "failed"
            final["result"] = "Execution ended without completing"
            final["executed_at"] = time.time()
            persist_schedule(final)

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
    original_post_url: Optional[str] = None
    original_media_width: Optional[int] = None
    original_media_height: Optional[int] = None
    original_media_type: Optional[str] = None
    original_aspect_ratio: Optional[str] = None
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
    # Use the async rehydrate path so a valid session still renders logged-in after a
    # restart (profile PII is re-fetched from X on cache-miss, in memory only).
    user = await rehydrate_user(request)
    preferred_ads_account_id = ""
    initial_schedules = []
    if user:
        try:
            conn = get_db()
            row = conn.execute(
                "SELECT ads_account_id FROM schedules WHERE user_id = ? AND ads_account_id IS NOT NULL AND ads_account_id != '' ORDER BY id DESC LIMIT 1",
                (user["id"],)
            ).fetchone()
            if row:
                preferred_ads_account_id = row[0] or ""
            # Load previous schedules for immediate display after login (light version with basic "changes").
            # No automatic client fetch on page load. The list is only refreshed on explicit user Refresh
            # or after the user performs a mutating action (save/cancel/run).
            rows = conn.execute(
                "SELECT * FROM schedules WHERE user_id = ? LIMIT 200",
                (user["id"],)
            ).fetchall()
            initial_schedules = [dict(r) for r in rows]
            # Attach basic changes so the "Will change / Changed" lines appear right away in the seeded list.
            def _light_changes(r):
                out = []
                if (r.get("new_title") or "") != (r.get("original_title") or ""):
                    out.append('title: "' + (r.get("original_title") or "") + '" → "' + (r.get("new_title") or "") + '"')
                if (r.get("new_media_id") or "") != (r.get("original_media_id") or ""):
                    out.append('media key: ' + (r.get("original_media_id") or "") + ' → ' + (r.get("new_media_id") or ""))
                if (r.get("new_url") or "") != (r.get("original_url") or ""):
                    out.append('url → ' + (r.get("new_url") or ""))
                if r.get("new_media_type") and r.get("new_media_type") != r.get("original_media_type"):
                    out.append('type: ' + (r.get("original_media_type") or "?") + ' → ' + r.get("new_media_type"))
                return out
            for rec in initial_schedules:
                rec["changes"] = _light_changes(rec)
            # Basic client-friendly order: pending soonest first, then recently executed.
            def _exec_key(r):
                return (r.get("executed_at") or 0, r.get("created_at") or 0, r.get("scheduled_at") or 0)
            pending = [r for r in initial_schedules if (r.get("status") or "pending") == "pending"]
            pending.sort(key=lambda r: r.get("scheduled_at") or 0)
            others = [r for r in initial_schedules if (r.get("status") or "pending") != "pending"]
            others.sort(key=_exec_key, reverse=True)
            initial_schedules = pending + others
            conn.close()
        except Exception:
            initial_schedules = []
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user": user,
            "x_redirect_uri": X_REDIRECT_URI,
            "preferred_ads_account_id": preferred_ads_account_id,
            "initial_schedules": initial_schedules,
        },
    )

@app.get("/login")
async def login(request: Request):
    """OAuth 1.0a 3-legged login (preferred because X Ads API requires OAuth1 user context).

    Users click "Sign in with X", authorize the App, and we receive oauth_token + oauth_token_secret
    that can be used to call both X API v2 (with signing) and the Ads API.
    This keeps the same high-level flow: any user can OAuth the app to act on *their* Ads account.
    """
    if not (X_CONSUMER_KEY and X_CONSUMER_SECRET):
        return HTMLResponse(
            "X_CONSUMER_KEY and X_CONSUMER_SECRET are required for OAuth1 (Ads API support).\n"
            "Set them in .env from your X App's 'Consumer Keys' (API Key + API Key Secret).",
            status_code=500,
        )

    bad_redirect_markers = ("dummy", "your-", "example", "change", "localhost:8000/callback")
    if any(m in X_REDIRECT_URI for m in bad_redirect_markers) or not X_REDIRECT_URI.startswith(("http://127.0.0.1", "https://")):
        return HTMLResponse(
            f"X_REDIRECT_URI is not correctly configured.\n\nCurrent value: {X_REDIRECT_URI}\n\n"
            "Register this exact URL as a Callback URL in your X App (under OAuth 1.0a settings) "
            "and put the same value in .env as X_REDIRECT_URI.",
            status_code=500,
        )

    # Step 1: Get a request token
    oauth_params = {
        "oauth_consumer_key": X_CONSUMER_KEY,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_nonce": secrets.token_urlsafe(16),
        "oauth_version": "1.0",
        "oauth_callback": X_REDIRECT_URI,
    }
    sig = _oauth1_sign("POST", X_REQUEST_TOKEN_URL, oauth_params, X_CONSUMER_SECRET, "")
    oauth_params["oauth_signature"] = sig
    auth_header = _build_oauth1_header(oauth_params)

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(X_REQUEST_TOKEN_URL, headers={"Authorization": auth_header})
        if r.status_code != 200:
            print("[OAuth1] request_token error:", r.status_code, r.text)
            return HTMLResponse("Failed to start OAuth1 flow with X. Check Consumer Key/Secret and Callback URL registration.", status_code=500)

        qs = parse_qs(r.text)
        request_token = qs.get("oauth_token", [None])[0]
        request_token_secret = qs.get("oauth_token_secret", [None])[0]

    if not request_token or not request_token_secret:
        return HTMLResponse("X did not return a valid request token.", status_code=500)

    request.session["oauth1_request_token"] = request_token
    request.session["oauth1_request_token_secret"] = request_token_secret

    authorize_url = f"{X_AUTHORIZE_URL}?oauth_token={request_token}"
    print(f"\n[OAuth1] Sending user to authorize: {authorize_url}")
    print(f"[OAuth1] Expecting callback at: {X_REDIRECT_URI}\n")
    return RedirectResponse(authorize_url)

@app.get("/callback")
async def callback(
    request: Request,
    oauth_token: Optional[str] = None,
    oauth_verifier: Optional[str] = None,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
):
    """Handle both OAuth1 callback (primary) and legacy OAuth2 callback."""
    if error:
        q = error + (f": {error_description}" if error_description else "")
        print(f"[OAuth] X returned error on callback: {q}")
        return RedirectResponse(f"/?error={q}")

    # OAuth 1.0a path
    if X_CONSUMER_KEY and X_CONSUMER_SECRET and oauth_token and oauth_verifier:
        request_token = request.session.get("oauth1_request_token")
        request_token_secret = request.session.get("oauth1_request_token_secret")

        if not request_token or oauth_token != request_token:
            request.session.pop("oauth1_request_token", None)
            request.session.pop("oauth1_request_token_secret", None)
            return RedirectResponse("/?error=invalid_oauth1_state")

        # Exchange request token + verifier for access token + secret (OAuth1)
        oauth_params = {
            "oauth_consumer_key": X_CONSUMER_KEY,
            "oauth_token": request_token,
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": str(int(time.time())),
            "oauth_nonce": secrets.token_urlsafe(16),
            "oauth_version": "1.0",
            "oauth_verifier": oauth_verifier,
        }
        sig = _oauth1_sign("POST", X_ACCESS_TOKEN_URL, oauth_params, X_CONSUMER_SECRET, request_token_secret)
        oauth_params["oauth_signature"] = sig
        auth_header = _build_oauth1_header(oauth_params)

        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(X_ACCESS_TOKEN_URL, headers={"Authorization": auth_header})
            if r.status_code != 200:
                print("[OAuth1] access_token exchange failed:", r.status_code, r.text)
                return RedirectResponse("/?error=oauth1_access_token_failed")

            qs = parse_qs(r.text)
            access_token = qs.get("oauth_token", [None])[0]
            access_token_secret = qs.get("oauth_token_secret", [None])[0]

        if not access_token or not access_token_secret:
            return RedirectResponse("/?error=bad_oauth1_token")

        print("[OAuth1] Access token exchange succeeded. Now fetching profile via /2/users/me...")

        # Fetch profile using the new OAuth1 token (via v2 /users/me with OAuth1 signing)
        try:
            profile = await fetch_user_oauth1(access_token, access_token_secret)
        except Exception as e:
            # Try to extract raw response body for better diagnostics (e.g. 215 Bad Authentication data)
            body = ""
            try:
                if hasattr(e, "response") and e.response is not None:
                    body = e.response.text[:300]
                elif hasattr(e, "detail"):
                    body = str(e.detail)[:300]
            except Exception:
                pass
            print("[OAuth1] profile fetch failed:", repr(e), "body:", body)
            err = "profile_fetch_failed"
            try:
                detail = body or str(e)
                err = "profile_fetch_failed:" + detail[:150]
            except Exception:
                pass
            return RedirectResponse(f"/?error={err}")

        xuid = profile["x_user_id"]
        # Profile PII lives in memory only (never persisted).
        USERS[xuid] = {
            "x_user_id": xuid,
            "username": profile["username"],
            "name": profile.get("name"),
            "profile_image_url": profile.get("profile_image_url"),
        }
        # OAuth1 secrets are encrypted at rest (both in-memory and DB), matching the
        # OAuth2 flow; they are decrypted at point of use in _build_user_dict.
        TOKENS[xuid] = {
            "oauth_token": encrypt(access_token),
            "oauth_token_secret": encrypt(access_token_secret),
            # No real expiry for typical OAuth1 user tokens; treat as long-lived
            "expires_at": time.time() + 365 * 24 * 3600,
            "scope": "oauth1",
        }
        persist_token(xuid, TOKENS[xuid])

        # Clean session
        request.session.pop("oauth1_request_token", None)
        request.session.pop("oauth1_request_token_secret", None)

        resp = RedirectResponse("/")
        resp.set_cookie("session", create_session_cookie(xuid), httponly=True, samesite="lax", max_age=60*60*24*90, path="/")
        return resp

    # Legacy OAuth2 path (still supported for non-Ads flows)
    print(f"\n[OAuth2] /callback hit — code={'yes' if code else 'no'}, state={'yes' if state else 'no'}")
    saved_state = request.session.get("oauth_state")
    verifier = request.session.get("pkce_verifier")
    if not code or not state or state != saved_state or not verifier:
        request.session.pop("pkce_verifier", None)
        request.session.pop("oauth_state", None)
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
            X_OAUTH2_TOKEN_URL,
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
        profile = await fetch_x_user(access_token)  # still works for OAuth2 token
    except Exception as e:
        print("[OAuth2] profile fetch failed:", e)
        return RedirectResponse("/?error=profile_fetch_failed")

    xuid = profile["x_user_id"]
    # Profile PII lives in memory only (never persisted).
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
    persist_token(xuid, TOKENS[xuid])

    resp = RedirectResponse("/")
    resp.set_cookie("session", create_session_cookie(xuid), httponly=True, samesite="lax", max_age=60*60*24*90, path="/")
    request.session.pop("pkce_verifier", None)
    request.session.pop("oauth_state", None)
    return resp

@app.get("/logout")
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
    srv_id = f"s{int(time.time()*1000)%10000000}"
    print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    print(f"[ADS-ACCOUNTS] *********** ENDPOINT HIT srv_id={srv_id} ***********")
    print(f"[ADS-ACCOUNTS] user in request: username={user.get('username')}, x_user_id={user.get('x_user_id') or user.get('id')}")
    access = await refresh_x_token_if_needed(user)
    granted_scopes = user.get("scope") or ""
    secret = user.get("oauth_token_secret", "")
    print(f"[ADS-ACCOUNTS] Fetching accounts for user {user.get('username') or user.get('x_user_id')} using OAuth1 token (has_secret={bool(secret)}) srv_id={srv_id}")
    try:
        data = await ads_get("/accounts", access, {"count": 50}, oauth_token_secret=secret)
        print(f"[ADS-ACCOUNTS] raw response keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
        print(f"[ADS-ACCOUNTS] RAW RESPONSE (truncated): {str(data)[:2000]}")
        req_part = (data.get("request") or {}) if isinstance(data, dict) else {}
        print(f"[ADS-ACCOUNTS] request part: {req_part}")
        print(f"[ADS-ACCOUNTS] data['data'] (first 3): {(data.get('data') or [])[:3] if isinstance(data, dict) else 'N/A'}")
        # Ads (and X) APIs sometimes return HTTP 200 + "errors" array for auth/permission issues
        if isinstance(data, dict) and data.get("errors"):
            errs = data.get("errors") or []
            first = errs[0] if errs else {}
            msg = first.get("message") or str(first) or "Unknown Ads API error"
            code = first.get("code")
            print(f"[ADS-ACCOUNTS] errors in response: {errs}")
            return JSONResponse({
                "accounts": [],
                "error": f"{msg} (code {code})",
                "granted_scopes": granted_scopes,
                "srv_id": srv_id,
                "raw": data
            }, status_code=200)
        accounts = []
        for a in (data.get("data") or []):
            # Support both "id" (standard for /accounts list) and "account_id"
            aid = a.get("id") or a.get("account_id") if isinstance(a, dict) else None
            if aid:
                accounts.append({
                    "id": aid,
                    "name": (a.get("name") or a.get("business_name") or "") if isinstance(a, dict) else "",
                    "timezone": a.get("timezone") if isinstance(a, dict) else None,
                })
        # Also harvest from the echoed "request"."params"."account_id" (user confirmed ads account lives here in responses)
        try:
            p = (req_part.get("params") or {}) if isinstance(req_part, dict) else {}
            aid = p.get("account_id")
            if aid:
                if not any((ac.get("id") == aid) for ac in accounts):
                    print(f"[ADS-ACCOUNTS] harvested account from request.params.account_id: {aid}")
                    accounts.append({"id": aid, "name": "", "timezone": None})
        except Exception as _e:
            print(f"[ADS-ACCOUNTS] harvest error: {_e}")
        print(f"[ADS-ACCOUNTS] found {len(accounts)} account(s) from live list")

        # 1) Supplement with this user's previously scheduled ads accounts (so we can prepopulate even if /accounts list is empty for this token)
        try:
            xuid = user.get("x_user_id") or user.get("id") or ""
            if xuid:
                conn = get_db()
                rows = conn.execute(
                    "SELECT DISTINCT ads_account_id FROM schedules WHERE user_id = ? AND ads_account_id IS NOT NULL AND ads_account_id != '' ORDER BY id DESC",
                    (xuid,)
                ).fetchall()
                for r in rows:
                    aid = r["ads_account_id"] if "ads_account_id" in r.keys() else (r[0] if r else None)
                    if aid and not any((ac.get("id") == aid) for ac in accounts):
                        print(f"[ADS-ACCOUNTS] added previously used account from schedules: {aid}")
                        accounts.append({"id": aid, "name": "(previously used)", "timezone": None})
        except Exception as _e:
            print(f"[ADS-ACCOUNTS] schedule history lookup error: {_e}")

        # 2) Deep harvest any account_id or plausible ads account strings from the entire raw response
        #    (the Ads API often echoes the account used under request.params.account_id in per-account calls)
        def _deep_find_account_ids(o):
            found = set()
            def walk(x):
                if isinstance(x, dict):
                    for kk, vv in x.items():
                        if kk in ("account_id", "ads_account_id") and isinstance(vv, str) and vv.strip():
                            found.add(vv.strip())
                        walk(vv)
                elif isinstance(x, list):
                    for item in x:
                        walk(item)
                elif isinstance(x, str):
                    # match things like 18ce55ox1wa (starts with digit, alphanum, reasonable length)
                    for m in re.finditer(r'\b([0-9][0-9a-zA-Z]{7,20})\b', x):
                        cand = m.group(1)
                        if 8 <= len(cand) <= 20:
                            found.add(cand)
            walk(o)
            return list(found)

        for aid in _deep_find_account_ids(data):
            if not any((ac.get("id") == aid) for ac in accounts):
                print(f"[ADS-ACCOUNTS] deep-harvested account id from raw response: {aid}")
                accounts.append({"id": aid, "name": "", "timezone": None})

        print(f"[ADS-ACCOUNTS] total accounts after supplements/harvest: {len(accounts)} srv_id={srv_id}")
        print(f"[ADS-ACCOUNTS] >>> RETURNING accounts list: {accounts}")
        print(f"[ADS-ACCOUNTS] >>> full payload summary: accounts_len={len(accounts)}, has_error={'error' in locals() or False} srv_id={srv_id}")
        return {"accounts": accounts, "granted_scopes": granted_scopes, "srv_id": srv_id, "raw_request": req_part, "raw": data}
    except HTTPException as e:
        print(f"[ADS-ACCOUNTS] HTTP error: {e.status_code} {e.detail} srv_id={srv_id}")
        return JSONResponse({"accounts": [], "error": str(e.detail), "granted_scopes": granted_scopes, "srv_id": srv_id}, status_code=200)
    except Exception as e:
        print(f"[ADS-ACCOUNTS] unexpected error: {repr(e)} srv_id={srv_id}")
        return JSONResponse({"accounts": [], "error": f"Unexpected error loading accounts: {e}", "granted_scopes": granted_scopes, "srv_id": srv_id}, status_code=200)

@app.get("/api/check-ads-account/{ads_account_id}")
async def api_check_ads_account(ads_account_id: str, user: Dict[str, Any] = Depends(require_user)):
    """Check the currently-authenticated user's access to a specific Ads account.
    Uses: GET https://ads-api.x.com/12/accounts/{ads_account_id}/authenticated_user_access
    This returns the permissions the signed-in X user has on that account (e.g. whether they can manage it).
    Returns a simple ok/failure + the raw response. Useful for debugging 403s.
    """
    access = await refresh_x_token_if_needed(user)
    granted_scopes = user.get("scope") or ""
    secret = user.get("oauth_token_secret", "")
    try:
        data = await ads_get(f"/accounts/{ads_account_id}/authenticated_user_access", access, oauth_token_secret=secret)
        return {
            "ok": True,
            "ads_account_id": ads_account_id,
            "granted_scopes": granted_scopes,
            "data": data
        }
    except HTTPException as e:
        # Return as 200 with details so the frontend can show nice messages without treating it as a hard error
        return JSONResponse({
            "ok": False,
            "ads_account_id": ads_account_id,
            "status": e.status_code,
            "error": e.detail,
            "granted_scopes": granted_scopes
        }, status_code=200)

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
    granted_scopes = user.get("scope") or ""
    oauth_secret = user.get("oauth_token_secret", "")

    # =====================================================================
    # STEP 1: Validate the post (X API v2)
    # =====================================================================
    # Purpose:
    #   - Confirm the post exists and is readable by the authenticated user
    #   - Confirm the post has a website card or app card attached (card_uri)
    #
    # This step does NOT call the Ads API at all.
    print("[validate] STEP 1 — checking if post exists and contains a card (X API v2)...")
    v = await validate_tweet_has_card(access, tweet_id, oauth_token_secret=oauth_secret if oauth_secret else None)
    card_id = v["card_id"]
    tweet = v.get("tweet") or {}
    print(f"[validate] STEP 1 SUCCESS — post exists and has a card. card_id derived from card_uri = {card_id}")

    # ---------------------------------------------------------------------
    # Pre-check: Can we even reach this Ads account? (helps give clear errors)
    # This is done after Step 1 succeeds, before we attempt to read the card.
    # ---------------------------------------------------------------------
    try:
        await ads_get(f"/accounts/{ads_account_id}", access, oauth_token_secret=oauth_secret if oauth_secret else None)
    except HTTPException as e:
        if e.status_code in (401, 403):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Step 1 passed (post exists and has a card). "
                    f"However, cannot access Ads Account {ads_account_id} (HTTP {e.status_code}). "
                    "Make sure you are signed in with an X account that has access to this Ads account, "
                    "the App has the Ads API product enabled, and the Account ID is correct."
                )
            )
        raise

    # =====================================================================
    # STEP 2: Get card details (Ads API)
    # =====================================================================
    # Now that we know the post is valid and has a card, fetch the actual
    # card record using the card_id we extracted in Step 1.
    #
    # Endpoint:
    #   GET https://ads-api.x.com/12/accounts/{account_id}/cards/{card_id}
    print(f"[validate] STEP 2 — fetching card details from Ads for card_id={card_id} ...")
    raw_ads_card = await ads_get(
        f"/accounts/{ads_account_id}/cards/{card_id}",
        access,
        oauth_token_secret=oauth_secret if oauth_secret else None,
    )
    print(f"[validate] STEP 2 response received (top-level keys): {list(raw_ads_card.keys()) if isinstance(raw_ads_card, dict) else type(raw_ads_card)}")

    card = parse_card_response(raw_ads_card)

    # Always keep the card_id we got from the tweet
    if not card.get("id"):
        card["id"] = card_id

    # Fallback title from tweet text if the Ads card had no title
    if not card.get("title"):
        tweet_text = tweet.get("text") or ""
        if tweet_text:
            card["title"] = tweet_text.strip()[:70]

    mid = card.get("media_id") or ""

    # Prefer the rich metadata that came back directly in the /cards/{id} response
    # (width, height, preview, type are already populated by parse_card_response for the new shape)
    has_dims = bool(card.get("original_media_width") and card.get("original_media_height"))
    has_preview = bool(card.get("media_preview"))
    has_mtype = bool(card.get("original_media_type"))

    # Only call the media library as a last resort if something important is missing
    if mid and (not has_dims or not has_preview or not has_mtype):
        try:
            minfo = await fetch_media_info(access, ads_account_id, mid, oauth_token_secret=oauth_secret if oauth_secret else None)
            if not has_dims:
                card["original_media_width"] = minfo.get("width")
                card["original_media_height"] = minfo.get("height")
            if not has_preview:
                card["media_preview"] = minfo.get("preview")
            if not has_mtype:
                card["original_media_type"] = minfo.get("media_type") or card.get("inferred_media_type")
            if not card.get("original_aspect_ratio"):
                card["original_aspect_ratio"] = minfo.get("aspect_ratio")
        except Exception:
            pass

    if not card.get("original_media_type"):
        card["original_media_type"] = card.get("inferred_media_type")

    # For video cards, ensure media_preview is an image (poster), not a video asset URL
    if (card.get("original_media_type") == "video" or card.get("inferred_media_type") == "video"):
        mp = card.get("media_preview") or ""
        if any(mp.lower().endswith(ext) for ext in (".mp4", ".mov", ".webm", ".m3u8", ".ts")):
            # try minfo if we have a better one (the fetch above may have run)
            try:
                minfo = locals().get("minfo") or {}
                if minfo.get("preview"):
                    p = minfo["preview"]
                    if not any(p.lower().endswith(ext) for ext in (".mp4", ".mov", ".webm")):
                        card["media_preview"] = p
            except Exception:
                pass

    # Include the raw responses from both steps (very useful for debugging)
    card["raw_tweet"] = v.get("raw")
    card["raw_ads_card"] = raw_ads_card

    print("[validate] STEP 2 parsed card for UI:", {k: card.get(k) for k in ("id", "title", "url", "media_id", "original_media_type", "original_media_width", "original_media_height", "media_preview")})

    return {"ok": True, "tweet_id": tweet_id, "card": card, "card_id": card_id}

@app.post("/api/check-media")
async def api_check_media(request: Request, user: Dict[str, Any] = Depends(require_user)):
    body = await request.json()
    ads_account_id = (body.get("ads_account_id") or "").strip()
    original_media_id = (body.get("original_media_id") or "").strip()
    new_media_id = (body.get("new_media_id") or "").strip()

    if not ads_account_id or not new_media_id:
        raise HTTPException(422, detail="Missing account or media key")

    access = await refresh_x_token_if_needed(user)
    secret = user.get("oauth_token_secret", "")

    # Info-only: return details for the requested *new* media key (no matching).
    newi = await fetch_media_info(access, ads_account_id, new_media_id, oauth_token_secret=secret)

    return {
        "ok": True,
        "media_id": new_media_id,
        "media_type": newi.get("media_type"),
        "width": newi.get("width"),
        "height": newi.get("height"),
        "aspect_ratio": newi.get("aspect_ratio"),
        "preview": newi.get("preview"),
    }


@app.post("/api/media-info")
async def api_media_info(request: Request, user: Dict[str, Any] = Depends(require_user)):
    """Fetch details for a single media key (no validation against any original).
    Used by the UI "Check" button to retrieve and display Media Type,
    Aspect ratio and Preview for whatever the user entered as New Media Key.
    (The underlying Ads endpoint does not return pixel dimensions.)
    """
    body = await request.json()
    ads_account_id = (body.get("ads_account_id") or "").strip()
    media_id = (body.get("media_id") or "").strip()

    if not ads_account_id or not media_id:
        raise HTTPException(422, detail="Missing account or media key")

    access = await refresh_x_token_if_needed(user)
    secret = user.get("oauth_token_secret", "")

    info = await fetch_media_info(access, ads_account_id, media_id, oauth_token_secret=secret)

    return {
        "ok": True,
        "media_id": media_id,
        "media_type": info.get("media_type"),
        "width": info.get("width"),
        "height": info.get("height"),
        "aspect_ratio": info.get("aspect_ratio"),
        "preview": info.get("preview"),
    }


@app.get("/api/media-library")
async def api_media_library(
    ads_account_id: str,
    media_type: str,
    cursor: Optional[str] = None,
    count: int = 24,
    q: Optional[str] = None,
    user: Dict[str, Any] = Depends(require_user),
):
    """Browse the signed-in user's Ads media library, filtered by app media type.

    media_type is the app-level value "image" or "video".
      image -> IMAGE
      video -> VIDEO + GIF (the app treats GIF as video; see _normalize_media_type)
    """
    app_type = (media_type or "").strip().lower()
    if app_type not in ("image", "video"):
        raise HTTPException(422, detail='media_type must be "image" or "video".')
    if not ads_account_id:
        raise HTTPException(422, detail="ads_account_id is required.")

    api_types = ["IMAGE"] if app_type == "image" else ["VIDEO", "GIF"]
    try:
        count = max(1, min(int(count), 200))
    except Exception:
        count = 24

    access = await refresh_x_token_if_needed(user)
    secret = user.get("oauth_token_secret", "")

    cursors = _decode_cursor(cursor)
    items: List[Dict[str, Any]] = []
    next_cursors: Dict[str, str] = {}

    for t in api_types:
        # On a follow-up page, only query the types that still have a cursor.
        c = cursors.get(t) or (cursors.get("_raw") if len(api_types) == 1 else None)
        if cursor and not c:
            continue
        params: Dict[str, Any] = {"media_type": t, "count": count}
        if c:
            params["cursor"] = c
        data = await ads_get(f"/accounts/{ads_account_id}/media_library", access, params, oauth_token_secret=secret)
        if isinstance(data, dict) and data.get("errors"):
            errs = data.get("errors") or []
            first = errs[0] if errs else {}
            raise HTTPException(502, detail=first.get("message") or str(first) or "Ads media library error")
        for m in (data.get("data") or []):
            items.append(_normalize_library_item(m))
        nc = data.get("next_cursor") if isinstance(data, dict) else None
        if nc:
            next_cursors[t] = nc

    if q:
        ql = q.strip().lower()
        if ql:
            items = [it for it in items if ql in (it.get("name") or "").lower() or ql in (it.get("file_name") or "").lower()]

    # Most-recent first when created_at is available (merged VIDEO+GIF pages).
    try:
        items.sort(key=lambda it: it.get("created_at") or "", reverse=True)
    except Exception:
        pass

    return {"ok": True, "items": items, "next_cursor": _encode_cursor(next_cursors)}


@app.post("/api/media-upload")
async def api_media_upload(
    file: UploadFile = File(...),
    ads_account_id: str = Form(...),
    name: Optional[str] = Form(None),
    user: Dict[str, Any] = Depends(require_user),
):
    """Upload a local file to the X media upload host (chunked, OAuth1-signed), then
    register it in the Ads media library. Returns the same shape as /api/media-info.
    """
    if not ads_account_id:
        raise HTTPException(422, detail="ads_account_id is required.")

    file_name = (name or file.filename or "upload").strip()
    media_category, app_media_type, mime = _categorize_upload(file.content_type or "", file.filename or file_name)

    access = await refresh_x_token_if_needed(user)
    secret = user.get("oauth_token_secret", "")

    # Uploads need more than the default 30s (large videos + processing polls).
    timeout = httpx.Timeout(connect=30.0, read=180.0, write=180.0, pool=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        media_id, media_key, fin_data = await _media_upload_chunked(
            client, file, access, secret, mime, media_category
        )

        # Videos/GIFs return processing_info and need to transcode before they are usable.
        state = "succeeded"
        pinfo = fin_data.get("processing_info") if isinstance(fin_data, dict) else None
        if pinfo and (pinfo.get("state") or "").lower() != "succeeded":
            state, status_data = await _poll_upload_status(client, media_id, access, secret)
            media_key = media_key or status_data.get("media_key")

    if state == "failed":
        raise HTTPException(422, detail="Media processing failed on X. Please try a different file.")
    if not media_key:
        raise HTTPException(502, detail="Upload finished but X did not return a media_key.")

    # Register the uploaded media in the Ads media library (params go in the query string).
    add_params = {"media_key": media_key, "media_category": media_category, "file_name": file_name}
    try:
        await ads_post(f"/accounts/{ads_account_id}/media_library", access, add_params, oauth_token_secret=secret)
    except HTTPException as e:
        # If it's already in the library, treat as non-fatal; otherwise surface.
        detail_text = str(e.detail).lower()
        if "already" not in detail_text and "exist" not in detail_text and "duplicate" not in detail_text:
            raise

    if state == "processing":
        # Client should poll /api/media-status until succeeded.
        return {"ok": True, "status": "processing", "media_key": media_key,
                "media_type": app_media_type, "aspect_ratio": None, "preview": None, "name": file_name}

    info = await fetch_media_info(access, ads_account_id, media_key, oauth_token_secret=secret)
    return {
        "ok": True,
        "media_key": media_key,
        "media_type": info.get("media_type") or app_media_type,
        "aspect_ratio": info.get("aspect_ratio"),
        "preview": info.get("preview"),
        "name": file_name,
        "status": "succeeded",
    }


@app.get("/api/media-status")
async def api_media_status(
    ads_account_id: str,
    media_key: str,
    user: Dict[str, Any] = Depends(require_user),
):
    """Poll the processing status of a media key (so the frontend can wait for videos)."""
    if not ads_account_id or not media_key:
        raise HTTPException(422, detail="ads_account_id and media_key are required.")

    access = await refresh_x_token_if_needed(user)
    secret = user.get("oauth_token_secret", "")

    try:
        data = await ads_get(
            f"/accounts/{ads_account_id}/media_library/{media_key}",
            access, None, oauth_token_secret=secret,
        )
    except HTTPException as e:
        if e.status_code == 404:
            return {"ok": True, "status": "processing", "media_key": media_key}
        raise

    m = data.get("data") if isinstance(data, dict) else None
    if isinstance(m, list):
        m = m[0] if m else {}
    m = m or {}

    status = _map_library_status(m.get("media_status") or m.get("status"))
    result: Dict[str, Any] = {"ok": True, "status": status, "media_key": media_key}
    if status == "succeeded":
        info = await fetch_media_info(access, ads_account_id, media_key, oauth_token_secret=secret)
        result.update({
            "media_type": info.get("media_type") or _normalize_media_type(m),
            "aspect_ratio": info.get("aspect_ratio"),
            "preview": info.get("preview"),
            "name": m.get("name") or m.get("file_name") or "",
        })
    return result


def _host_resolves_to_public_ip(host: str) -> bool:
    """SSRF guard: True only if `host` resolves and every resolved IP is a public address.

    Blocks localhost and any private/loopback/link-local/reserved/multicast/unspecified
    address (IPv4 and IPv6), which covers 127.0.0.1, 0.0.0.0, 169.254.169.254 (cloud
    metadata), RFC1918 ranges, and IPv6 ::1, fc00::/7, fe80::/10.
    """
    if not host or host.lower() == "localhost":
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    if not infos:
        return False
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


@app.post("/api/validate-url")
async def api_validate_url(request: Request, user: Dict[str, Any] = Depends(require_user)):
    # Preserves the {"valid": bool, "error": str} contract the frontend depends on.
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url:
        return {"valid": False, "error": "URL is required"}
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return {"valid": False, "error": "Please enter a valid http(s) URL."}
    # SSRF guard: block requests to internal/metadata hosts before any outbound call.
    if not _host_resolves_to_public_ip(parsed.hostname):
        return {"valid": False, "error": "That URL host is not allowed."}
    try:
        # follow_redirects=False so a redirect can't bounce us to an unvalidated host.
        # 3xx is treated as reachable (valid) without following.
        async with httpx.AsyncClient(timeout=6, follow_redirects=False) as client:
            r = await client.head(url)
            if r.status_code >= 400:
                return {"valid": False, "error": f"URL returned status {r.status_code}."}
    except Exception:
        pass
    return {"valid": True}

@app.post("/api/schedules")
async def api_create_schedule(payload: ScheduleIn, user: Dict[str, Any] = Depends(require_user)):
    if payload.scheduled_at <= time.time():
        raise HTTPException(422, detail="Scheduled time must be in the future.")

    access = await refresh_x_token_if_needed(user)
    secret = user.get("oauth_token_secret", "")

    orig_mid = (payload.original_media_id or "").strip()
    new_mid = (payload.new_media_id or "").strip()

    # Treat a blank "New Media Key" as "keep the original" (no media change intended).
    # This prevents accidentally storing an empty media id.
    if not new_mid:
        new_mid = orig_mid

    # Always look up the media we will actually store as "new".
    # This populates the correct new_media_type even if the client didn't click Check.
    newi = await fetch_media_info(access, payload.ads_account_id, new_mid or orig_mid, oauth_token_secret=secret)
    new_type = newi.get("media_type") or payload.new_media_type
    new_preview = newi.get("preview")

    orig_type = payload.original_media_type

    # Fetch preview for original media (for "before" visual in schedule list when media changes)
    orig_preview = None
    if orig_mid:
        try:
            origi = await fetch_media_info(access, payload.ads_account_id, orig_mid, oauth_token_secret=secret)
            orig_preview = origi.get("preview")
        except Exception:
            pass

    # If the user explicitly supplied a *different* media key, enforce basic compatibility.
    # We intentionally do not require aspect ratio match or a prior "Check" click,
    # but the media *type* (image vs video) must be compatible with the original card.
    # A mismatch here previously led to schedules that stored the original key and later reported "no changes".
    user_supplied_different = bool((payload.new_media_id or "").strip() and (payload.new_media_id or "").strip() != orig_mid)
    if user_supplied_different:
        nt = (new_type or "").strip().lower()
        ot = (orig_type or "").strip().lower()
        if nt and ot and nt != ot:
            raise HTTPException(
                422,
                detail=f"Media type mismatch: the original media for this card is {orig_type or 'unknown'}, "
                       f"but the New Media Key you provided is {new_type or 'unknown'}. "
                       "Updates must use media of the same type (image or video)."
            )
        if not nt:
            raise HTTPException(
                422,
                detail="Could not retrieve details for the New Media Key from the Ads account's media library. "
                       "Double-check the key and the selected Ads Account, then click Check to confirm it loads."
            )

    parsed = urlparse(payload.new_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(422, detail="New URL must be a valid http(s) URL.")

    now = time.time()
    rec = {
        "user_id": user["id"],
        "ads_account_id": payload.ads_account_id,
        "card_id": payload.card_id,
        "card_type": payload.card_type or "website",
        "original_title": payload.original_title or "",
        "original_media_id": payload.original_media_id or "",
        "original_url": payload.original_url or "",
        "original_post_url": payload.original_post_url or "",
        "original_media_width": payload.original_media_width,
        "original_media_height": payload.original_media_height,
        "original_media_type": orig_type,
        "new_title": payload.new_title,
        "new_media_id": new_mid,
        "new_url": payload.new_url,
        "new_media_type": new_type,
        "new_preview": new_preview,
        "scheduled_at": payload.scheduled_at,
        "status": "pending",
        "result": None,
        "created_at": now,
        "executed_at": None,
        "original_preview": orig_preview,
    }

    # Attach the planned changes immediately (used by UI to show "Will change" right after save)
    def _compute_changes_local(r: dict) -> list[str]:
        out = []
        if r.get("new_title") != r.get("original_title"):
            out.append(f'title: "{r.get("original_title") or ""}" → "{r.get("new_title") or ""}"')
        if r.get("new_media_id") != r.get("original_media_id"):
            out.append(f'media key: {r.get("original_media_id") or ""} → {r.get("new_media_id") or ""}')
        if r.get("new_url") != r.get("original_url"):
            out.append(f'url → {r.get("new_url") or ""}')
        if r.get("new_media_type") and r.get("new_media_type") != r.get("original_media_type"):
            out.append(f'type: {r.get("original_media_type") or "?"} → {r.get("new_media_type")}')
        return out
    rec["changes"] = _compute_changes_local(rec)

    # Let SQLite assign the id (source of truth for persistence)
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO schedules
        (user_id, ads_account_id, card_id, card_type,
         original_title, original_media_id, original_url, original_post_url,
         original_media_width, original_media_height, original_media_type,
         new_title, new_media_id, new_url, new_media_type,
         original_preview, new_preview,
         scheduled_at, status, result, executed_at, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        rec["user_id"], rec["ads_account_id"], rec["card_id"], rec["card_type"],
        rec["original_title"], rec["original_media_id"], rec["original_url"], rec.get("original_post_url"),
        rec["original_media_width"], rec["original_media_height"], rec["original_media_type"],
        rec["new_title"], rec["new_media_id"], rec["new_url"], rec["new_media_type"],
        rec.get("original_preview"), rec.get("new_preview"),
        rec["scheduled_at"], rec["status"], rec["result"], rec["executed_at"], rec["created_at"]
    ))
    sid = c.lastrowid
    conn.commit()
    conn.close()

    rec["id"] = sid
    SCHEDULES[sid] = rec
    # Register a DateTrigger job so execution fires at exactly scheduled_at.
    add_schedule_job(rec)
    return {"ok": True, "schedule": rec}

@app.get("/api/schedules")
async def api_list_schedules(user: Dict[str, Any] = Depends(require_user)):
    # Primary source: in-memory SCHEDULES dict (always reflects current execution state, including
    # running/completed transitions that happen mid-flight before DB is synced).
    in_memory_ids: set = set()
    mine: list = []
    for sid, rec in list(SCHEDULES.items()):
        if rec.get("user_id") == user["id"]:
            mine.append(dict(rec))  # shallow copy so enrichment doesn't mutate the live dict
            in_memory_ids.add(int(sid))

    # Fallback: add any DB rows not already covered (e.g. schedules loaded after a server restart
    # that haven't been touched in this process lifetime and therefore aren't in SCHEDULES yet).
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM schedules WHERE user_id = ? LIMIT 200",
        (user["id"],)
    ).fetchall()
    conn.close()
    for row in rows:
        d = dict(row)
        rid = int(d.get("id") or 0)
        if rid and rid not in in_memory_ids:
            mine.append(d)
            # Hydrate into memory so next calls are consistent
            SCHEDULES[rid] = d

    # Self-heal stale "running" rows: a run that started too long ago was almost certainly
    # orphaned (process interrupted before writing a terminal status). Left as-is it makes the
    # frontend poll forever. Age is measured from started_at (set when the run flips to running),
    # falling back to scheduled_at/created_at for rows loaded from DB. Genuinely in-progress runs
    # (started seconds ago) stay "running".
    now_ts = time.time()
    for rec in mine:
        if rec.get("status") != "running":
            continue
        started = rec.get("started_at") or rec.get("scheduled_at") or rec.get("created_at") or 0
        if started and (now_ts - started) > STALE_RUNNING_SECONDS:
            rec["status"] = "failed"
            rec["result"] = "Interrupted (stale running state auto-recovered)"
            rec["executed_at"] = rec.get("executed_at") or now_ts
            live = SCHEDULES.get(int(rec.get("id") or 0))
            target = live if live is not None else rec
            target["status"] = "failed"
            target["result"] = rec["result"]
            target["executed_at"] = rec["executed_at"]
            persist_schedule(target)

    # Enrich previews for media changes (for schedules created before previews were stored,
    # or if somehow missing). This makes "old vs new media preview" work for historical items too.
    access = await refresh_x_token_if_needed(user)
    secret = user.get("oauth_token_secret", "")

    # Collect the preview lookups we need, then run them concurrently. Previews are
    # best-effort for the UI list, so failures must stay non-fatal (return_exceptions).
    async def _enrich(rec: dict, field: str, media_id: str, ads: str):
        info = await fetch_media_info(access, ads, media_id, oauth_token_secret=secret)
        if info.get("preview"):
            rec[field] = info.get("preview")

    tasks = []
    for rec in mine:
        omid = (rec.get("original_media_id") or "").strip()
        nmid = (rec.get("new_media_id") or "").strip()
        ads = rec.get("ads_account_id") or ""
        if omid and nmid and omid != nmid:
            if not rec.get("original_preview"):
                tasks.append(_enrich(rec, "original_preview", omid, ads))
            if not rec.get("new_preview"):
                tasks.append(_enrich(rec, "new_preview", nmid, ads))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    def _compute_changes(rec: dict) -> list[str]:
        """Return human-readable list of what differs between original and new values."""
        out = []
        if rec.get("new_title") != rec.get("original_title"):
            out.append(f'title: "{rec.get("original_title") or ""}" → "{rec.get("new_title") or ""}"')
        if rec.get("new_media_id") != rec.get("original_media_id"):
            out.append(f'media key: {rec.get("original_media_id") or ""} → {rec.get("new_media_id") or ""}')
        if rec.get("new_url") != rec.get("original_url"):
            out.append(f'url → {rec.get("new_url") or ""}')
        if rec.get("new_media_type") and rec.get("new_media_type") != rec.get("original_media_type"):
            out.append(f'type: {rec.get("original_media_type") or "?"} → {rec.get("new_media_type")}')
        return out

    # Defensive: never surface raw JSON body as the "result" for a successful run.
    # If a previous record has a JSON-looking result for completed, rewrite to a friendly summary.
    for rec in mine:
        if (rec.get("status") == "completed") and rec.get("result"):
            rs = str(rec.get("result") or "").strip()
            if rs.startswith("{") or rs.startswith("[") or (len(rs) > 50 and rs[0] in '{["'):
                post = rec.get("original_post_url") or rec.get("new_url") or ""
                rec["result"] = ("Card updated." + (f" Post: {post}" if post else ""))[:4000]

        # Attach explicit changes summary for the UI (shows "what will be changed" for pending, "what was changed" for executed)
        rec["changes"] = _compute_changes(rec)

    # Sort order: pending (by scheduled_at ASC) → running/completed/failed (by executed_at DESC) → cancelled (last)
    def _exec_key(r):
        return (r.get("executed_at") or 0, r.get("created_at") or 0, r.get("scheduled_at") or 0)

    pending  = [r for r in mine if r.get("status") == "pending"]
    active   = [r for r in mine if r.get("status") in ("running", "completed", "failed")]
    cancelled = [r for r in mine if r.get("status") == "cancelled"]
    unknown  = [r for r in mine if r.get("status") not in ("pending", "running", "completed", "failed", "cancelled")]

    pending.sort(key=lambda r: r.get("scheduled_at") or 0)
    active.sort(key=_exec_key, reverse=True)
    cancelled.sort(key=_exec_key, reverse=True)

    ordered = pending + active + unknown + cancelled
    return {"schedules": ordered}

@app.delete("/api/schedules/{sid}")
async def api_cancel_schedule(sid: int, user: Dict[str, Any] = Depends(require_user)):
    rec = SCHEDULES.get(sid)
    if not rec or rec.get("user_id") != user["id"]:
        raise HTTPException(404, "Schedule not found")
    remove_schedule_job(sid)  # cancel the waiting APScheduler job
    rec["status"] = "cancelled"
    rec["executed_at"] = rec.get("executed_at") or time.time()
    persist_schedule(rec)
    return {"ok": True}

@app.post("/api/schedules/{sid}/execute")
async def api_force_execute(sid: int, user: Dict[str, Any] = Depends(require_user)):
    rec = SCHEDULES.get(sid)
    if not rec or rec.get("user_id") != user["id"]:
        raise HTTPException(404, "Schedule not found")
    # Cancel the scheduled job — we're running it now instead.
    remove_schedule_job(sid)
    await execute_card_update(rec, force=True)
    # Re-read so the client can see the final status + any error message
    fresh = SCHEDULES.get(sid) or {}
    return {
        "ok": True,
        "status": fresh.get("status"),
        "result": fresh.get("result"),
    }

@app.get("/health")
async def health():
    return {"ok": True, "time": time.time(), "schedules_in_memory": len(SCHEDULES)}
