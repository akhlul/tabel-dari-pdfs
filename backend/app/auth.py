"""Login and access control.

Who may do what:
  public (not logged in)  read tables flagged valid (and their documents, PDFs, exports, /api/v1)
  editor                  everything with the library: import, extract, edit, flag, delete, categories
  admin                   also users, backup / restore and permanent deletes

Two ways to log in:
  - username + password; the first admin comes from ADMIN_USER / ADMIN_PASSWORD (.env)
  - Google, when GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are set; only for e-mail addresses an admin has added

Every /api request passes `AuthMiddleware`: it finds the user of the session cookie, refuses what the role may
not do (deny by default: only the routes in PUBLIC_* are open), checks that writes come from this site (CSRF),
and rate-limits visitors who are not logged in.

Users and sessions are kept in data/auth.db, not in library.db: a backup never carries password hashes and a
restore never removes accounts.

CLI (Docker: docker compose exec app python -m app.auth ...):
    python -m app.auth create-admin NAME     create an admin (asks for the password)
    python -m app.auth set-password NAME     new password (also logs the user out everywhere)
    python -m app.auth list
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Literal
from urllib.parse import parse_qsl, urlencode, urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from .config import (ADMIN_PASSWORD, ADMIN_USER, AUTH_DB_PATH, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, PUBLIC_URL,
                     RATE_PUBLIC, RATE_PUBLIC_FILES, SESSION_DAYS)

COOKIE = "tdp_session"
OAUTH_COOKIE = "tdp_oauth"
RANK = {"editor": 1, "admin": 2}
MIN_PASSWORD = 8
LOGIN_FAILS_PER_MIN = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username TEXT UNIQUE COLLATE NOCASE,     -- password login (NULL: Google only)
  email TEXT UNIQUE COLLATE NOCASE,        -- Google login (NULL: password only)
  name TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL CHECK (role IN ('editor', 'admin')),
  password_hash TEXT,
  google_sub TEXT UNIQUE,                  -- the Google account, linked at its first login
  active INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  last_login_at REAL,
  CHECK (username IS NOT NULL OR email IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,             -- sha256 of the cookie value (the token itself is never stored)
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  method TEXT NOT NULL,                    -- password | google
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  last_seen_at REAL NOT NULL,
  ip TEXT,
  user_agent TEXT
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
"""


# ---------------------------------------------------------------- storage

def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(AUTH_DB_PATH, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


@contextmanager
def adb() -> Iterator[sqlite3.Connection]:
    con = _connect()
    try:
        yield con
    finally:
        con.close()


def init() -> None:
    with adb() as con:
        con.executescript(SCHEMA)
        con.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
        if ADMIN_USER and ADMIN_PASSWORD:
            if not con.execute("SELECT 1 FROM users WHERE username=?", (ADMIN_USER,)).fetchone():
                _insert_user(con, username=ADMIN_USER, email=None, name=ADMIN_USER, role="admin",
                             password=ADMIN_PASSWORD)
                print(f"[auth] admin '{ADMIN_USER}' created from ADMIN_USER / ADMIN_PASSWORD", flush=True)
        if not con.execute("SELECT 1 FROM users WHERE role='admin' AND active=1").fetchone():
            print("[auth] WARNING: there is no admin. Set ADMIN_USER and ADMIN_PASSWORD in .env and restart, "
                  "or run: python -m app.auth create-admin NAME", flush=True)


def _insert_user(con, *, username: str | None, email: str | None, name: str, role: str,
                 password: str | None) -> str:
    uid = uuid.uuid4().hex[:16]
    con.execute("INSERT INTO users (id, username, email, name, role, password_hash, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (uid, username or None, (email or "").lower() or None, name, role,
                 hash_password(password) if password else None, time.time()))
    return uid


def public_user(u: dict | sqlite3.Row) -> dict:
    u = dict(u)
    return {"id": u["id"], "username": u["username"], "email": u["email"], "name": u["name"] or u["username"]
            or u["email"], "role": u["role"], "active": bool(u["active"]), "has_password": bool(u["password_hash"]),
            "google_linked": bool(u["google_sub"]), "created_at": u["created_at"], "last_login_at": u["last_login_at"]}


# ---------------------------------------------------------------- passwords (scrypt, standard library)

_N, _R, _P = 2 ** 15, 8, 1
_MAXMEM = 64 * 1024 * 1024


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM, dklen=32)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(h)}"


def verify_password(pw: str, stored: str | None) -> bool:
    try:
        algo, n, r, p, salt, h = (stored or _dummy_hash()).split("$")
        got = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=_MAXMEM, dklen=32)
        return algo == "scrypt" and stored is not None and hmac.compare_digest(got, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


_dummy: list[str] = []


def _dummy_hash() -> str:
    """Unknown user names take as long as wrong passwords (no user-name probing by timing)."""
    if not _dummy:
        _dummy.append(hash_password(secrets.token_hex(8)))
    return _dummy[0]


# ---------------------------------------------------------------- sessions

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: str, method: str, ip: str | None, user_agent: str | None) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with adb() as con:
        con.execute("INSERT INTO sessions (token_hash, user_id, method, created_at, expires_at, last_seen_at, ip, "
                    "user_agent) VALUES (?,?,?,?,?,?,?,?)",
                    (_token_hash(token), user_id, method, now, now + SESSION_DAYS * 86400, now, ip,
                     (user_agent or "")[:300]))
        con.execute("UPDATE users SET last_login_at=? WHERE id=?", (now, user_id))
    return token


def user_for_token(token: str) -> dict | None:
    th = _token_hash(token)
    now = time.time()
    with adb() as con:
        r = con.execute("SELECT u.*, s.last_seen_at AS s_seen, s.method AS s_method FROM sessions s "
                        "JOIN users u ON u.id = s.user_id WHERE s.token_hash=? AND s.expires_at > ? AND u.active=1",
                        (th, now)).fetchone()
        if not r:
            return None
        if now - r["s_seen"] > 3600:  # sliding expiry: SESSION_DAYS after the last visit
            con.execute("UPDATE sessions SET last_seen_at=?, expires_at=? WHERE token_hash=?",
                        (now, now + SESSION_DAYS * 86400, th))
    return public_user(r) | {"login_method": r["s_method"]}


def end_sessions(user_id: str) -> None:
    with adb() as con:
        con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def _set_session_cookie(resp: Response, request: Request, token: str) -> None:
    resp.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https", path="/")


# ---------------------------------------------------------------- rate limits (in memory, per process)

class Limiter:
    """Fixed one-minute windows per key."""

    def __init__(self, window: float = 60.0):
        self.window = window
        self._hits: dict[tuple, tuple[float, int]] = {}
        self._lock = threading.Lock()
        self._swept = time.monotonic()

    def _get(self, key, now) -> tuple[float, int]:
        start, n = self._hits.get(key, (now, 0))
        return (now, 0) if now - start >= self.window else (start, n)

    def hit(self, key, limit: int) -> bool:
        """Count one request; False when the key is over `limit` in this window."""
        now = time.monotonic()
        with self._lock:
            if now - self._swept > 300:
                self._hits = {k: v for k, v in self._hits.items() if now - v[0] < self.window}
                self._swept = now
            start, n = self._get(key, now)
            self._hits[key] = (start, n + 1)
            return n + 1 <= limit

    def count(self, key) -> int:
        with self._lock:
            return self._get(key, time.monotonic())[1]

    def reset(self, key) -> None:
        with self._lock:
            self._hits.pop(key, None)


limiter = Limiter()
login_fails = Limiter()


# ---------------------------------------------------------------- access policy

def _rx(*ps: str) -> list[re.Pattern]:
    return [re.compile(p) for p in ps]


# deny by default: everything under /api/ needs an editor, except these
PUBLIC_GET = _rx(
    r"/api/health", r"/api/engines", r"/api/auth/(me|google/start|google/callback)",
    r"/api/documents", r"/api/documents/[^/]+", r"/api/documents/[^/]+/(pdf|thumb|tables|export)",
    r"/api/tables", r"/api/tables/[^/]+", r"/api/tables/[^/]+/(export|snapshot\.png)", r"/api/tables-export",
    r"/api/v1(/.*)?",
)
PUBLIC_POST = _rx(r"/api/auth/(login|logout)")
ADMIN_ONLY = _rx(r"/api/backup(/.*)?", r"/api/users(/.*)?")
FILES = _rx(r"/api/documents/[^/]+/(pdf|export)", r"/api/tables/[^/]+/export", r"/api/tables-export",
            r"/api/v1/documents/[^/]+/tables")
SAFE = ("GET", "HEAD", "OPTIONS")


def required_role(method: str, path: str) -> str | None:
    if not path.startswith("/api/"):
        return None  # the web UI itself
    if method in ("GET", "HEAD") and any(p.fullmatch(path) for p in PUBLIC_GET):
        return None
    if method == "POST" and any(p.fullmatch(path) for p in PUBLIC_POST):
        return None
    if any(p.fullmatch(path) for p in ADMIN_ONLY):
        return "admin"
    return "editor"


def _same_site(headers: dict[str, str]) -> bool:
    """Writes must come from this site's own pages (CSRF). Requests without browser headers (curl, scripts) pass:
    they cannot carry a victim's cookie."""
    site = headers.get("sec-fetch-site")
    if site:
        return site in ("same-origin", "none")
    origin = headers.get("origin")
    if not origin or origin == "null":
        return origin is None
    netloc = urlparse(origin).netloc.lower()
    allowed = {headers.get("host", "").lower(), headers.get("x-forwarded-host", "").lower()}
    if PUBLIC_URL:
        allowed.add(urlparse(PUBLIC_URL).netloc.lower())
    return netloc in allowed


def _cookie(header: str, name: str) -> str | None:
    for part in header.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v or None
    return None


CORS = [(b"access-control-allow-origin", b"*"), (b"access-control-allow-methods", b"GET, HEAD, OPTIONS"),
        (b"access-control-allow-headers", b"If-None-Match"), (b"access-control-expose-headers", b"ETag"),
        (b"access-control-max-age", b"86400")]


class AuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path, method = scope["path"], scope["method"]
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        ip = (scope.get("client") or ("?",))[0]
        v1 = path == "/api/v1" or path.startswith("/api/v1/")

        if v1 and method == "OPTIONS":  # CORS preflight of the public JSON API
            return await Response(status_code=204, headers={k.decode(): v.decode() for k, v in CORS})(
                scope, receive, send)

        token = _cookie(headers.get("cookie", ""), COOKIE)
        user = user_for_token(token) if token else None
        scope.setdefault("state", {})["user"] = user
        _user.set(user)  # for the audit columns (current_user_id), also inside sync endpoints

        def refuse(status: int, detail: str, extra: dict | None = None):
            return JSONResponse({"detail": detail}, status_code=status, headers=extra)(scope, receive, send)

        if path.startswith("/api/") and method not in SAFE and not _same_site(headers):
            return await refuse(403, "Request refused: it did not come from this site")
        need = required_role(method, path)
        if need:
            if not user:
                return await refuse(401, "Log in to do this")
            if RANK[user["role"]] < RANK[need]:
                return await refuse(403, "Only an admin can do this")
        elif not user and path.startswith("/api/") and not path.startswith("/api/auth/"):
            files = any(p.fullmatch(path) for p in FILES)
            if not limiter.hit((ip, "files" if files else "api"), RATE_PUBLIC_FILES if files else RATE_PUBLIC):
                return await refuse(429, "Too many requests. Wait a minute, or log in.", {"Retry-After": "60"})

        if not v1:
            return await self.app(scope, receive, send)

        async def send_cors(msg):
            if msg["type"] == "http.response.start":
                msg = {**msg, "headers": [*msg.get("headers", []), *CORS[:1], *CORS[3:4]]}
            await send(msg)

        await self.app(scope, receive, send_cors)


# ---------------------------------------------------------------- helpers for the API modules

_user: ContextVar[dict | None] = ContextVar("user", default=None)


def current_user(request: Request) -> dict | None:
    return getattr(request.state, "user", None)


def current_user_id() -> str | None:
    """The user of the request being handled (None outside requests, e.g. in a background thread)."""
    u = _user.get()
    return u["id"] if u else None


def user_names() -> dict[str, str]:
    """id -> display name of every user (to show who changed something)."""
    with adb() as con:
        return {r["id"]: r["name"] or r["username"] or r["email"] for r in con.execute(
            "SELECT id, name, username, email FROM users")}


def with_names(item: dict, names: dict[str, str] | None = None) -> dict:
    """Add created_by_name / updated_by_name / validated_by_name next to the audit ids of a row."""
    names = user_names() if names is None else names
    for k in ("created_by", "updated_by", "validated_by"):
        if k in item:
            item[f"{k}_name"] = names.get(item[k]) if item[k] else None
    return item


def is_public(request: Request) -> bool:
    """Not logged in: may see only tables flagged valid."""
    return current_user(request) is None


def need(request: Request, role: Literal["editor", "admin"]) -> dict:
    u = current_user(request)
    if not u:
        raise HTTPException(401, "Log in to do this")
    if RANK[u["role"]] < RANK[role]:
        raise HTTPException(403, "Only an admin can do this")
    return u


def google_enabled() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


# ---------------------------------------------------------------- /api/auth

router = APIRouter(prefix="/api/auth")


@router.get("/me")
def me(request: Request):
    return {"user": current_user(request), "google": google_enabled()}


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=200)


@router.post("/login")
def login(body: Login, request: Request):
    ip = request.client.host if request.client else "?"
    if login_fails.count(ip) >= LOGIN_FAILS_PER_MIN:
        raise HTTPException(429, "Too many failed logins. Wait a minute, then try again.")
    with adb() as con:
        u = con.execute("SELECT * FROM users WHERE username=? OR email=?",
                        (body.username.strip(), body.username.strip().lower())).fetchone()
    ok = verify_password(body.password, u["password_hash"] if u else None)
    if not ok or not u or not u["active"]:
        login_fails.hit(ip, LOGIN_FAILS_PER_MIN)
        raise HTTPException(401, "Wrong user name or password")
    login_fails.reset(ip)
    token = create_session(u["id"], "password", ip, request.headers.get("user-agent"))
    resp = JSONResponse({"user": public_user(u)})
    _set_session_cookie(resp, request, token)
    return resp


@router.post("/logout")
def logout(request: Request):
    token = request.cookies.get(COOKIE)
    if token:
        with adb() as con:
            con.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


class PasswordChange(BaseModel):
    current: str = Field("", max_length=200)
    new: str = Field(min_length=MIN_PASSWORD, max_length=200)


@router.post("/password")
def change_password(body: PasswordChange, request: Request):
    """Change your own password (Google-only users may set one, then they can also log in with it)."""
    u = need(request, "editor")
    with adb() as con:
        r = con.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
        if r["password_hash"] and not verify_password(body.current, r["password_hash"]):
            raise HTTPException(400, "The current password is wrong")
        if not r["username"]:
            raise HTTPException(400, "Ask an admin to give you a user name first")
        con.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(body.new), u["id"]))
        # log out the other browsers; this one stays logged in
        con.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?",
                    (u["id"], _token_hash(request.cookies.get(COOKIE, ""))))
    return {"ok": True}


# Google: OpenID Connect authorization-code flow. The id_token comes straight from Google's token endpoint over
# TLS (authenticated with the client secret), so its signature does not need checking (OIDC Core 3.1.3.7).

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"


def _redirect_uri(request: Request) -> str:
    return (PUBLIC_URL or str(request.base_url).rstrip("/")) + "/api/auth/google/callback"


def _safe_next(nxt: str) -> str:
    return nxt if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt else "/"


def _login_error(code: str, **extra: str) -> RedirectResponse:
    resp = RedirectResponse("/login?" + urlencode({"error": code, **extra}), status_code=303)
    resp.delete_cookie(OAUTH_COOKIE, path="/api/auth/google")
    return resp


@router.get("/google/start")
def google_start(request: Request, next: str = "/"):
    if not google_enabled():
        raise HTTPException(404, "Google login is not set up")
    state = secrets.token_urlsafe(24)
    url = GOOGLE_AUTH + "?" + urlencode({
        "client_id": GOOGLE_CLIENT_ID, "redirect_uri": _redirect_uri(request), "response_type": "code",
        "scope": "openid email profile", "state": state, "prompt": "select_account"})
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie(OAUTH_COOKIE, urlencode({"state": state, "next": _safe_next(next)}), max_age=600,
                    httponly=True, samesite="lax", secure=request.url.scheme == "https", path="/api/auth/google")
    return resp


def _claims(id_token: str) -> dict:
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


@router.get("/google/callback")
async def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if not google_enabled():
        raise HTTPException(404, "Google login is not set up")
    saved = dict(parse_qsl(request.cookies.get(OAUTH_COOKIE, "")))
    if error:
        return _login_error("google_cancelled")
    if not code or not saved.get("state") or not hmac.compare_digest(saved["state"], state):
        return _login_error("google_state")
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(GOOGLE_TOKEN, data={
                "code": code, "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": _redirect_uri(request), "grant_type": "authorization_code"})
    except httpx.HTTPError:
        return _login_error("google_unreachable")
    if r.status_code != 200:
        return _login_error("google_token")
    cl = _claims(r.json().get("id_token", ""))
    if (cl.get("iss") not in ("https://accounts.google.com", "accounts.google.com") or cl.get("aud") != GOOGLE_CLIENT_ID
            or float(cl.get("exp", 0)) < time.time() or not cl.get("sub")):
        return _login_error("google_token")
    email = str(cl.get("email", "")).lower()
    if not email or cl.get("email_verified") not in (True, "true"):
        return _login_error("google_email")
    with adb() as con:
        u = con.execute("SELECT * FROM users WHERE google_sub=?", (cl["sub"],)).fetchone() or \
            con.execute("SELECT * FROM users WHERE email=? AND google_sub IS NULL", (email,)).fetchone()
        if not u or not u["active"]:
            return _login_error("not_registered", email=email)
        con.execute("UPDATE users SET google_sub=?, name=CASE WHEN name='' THEN ? ELSE name END WHERE id=?",
                    (cl["sub"], str(cl.get("name", ""))[:100], u["id"]))
    ip = request.client.host if request.client else None
    token = create_session(u["id"], "google", ip, request.headers.get("user-agent"))
    resp = RedirectResponse(_safe_next(saved.get("next", "/")), status_code=303)
    resp.delete_cookie(OAUTH_COOKIE, path="/api/auth/google")
    _set_session_cookie(resp, request, token)
    return resp


# ---------------------------------------------------------------- /api/users (admin)

users_router = APIRouter(prefix="/api/users")
USERNAME = r"^[A-Za-z0-9._-]{3,40}$"
EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class UserCreate(BaseModel):
    username: str | None = Field(None, pattern=USERNAME)
    email: str | None = Field(None, pattern=EMAIL, max_length=254)
    name: str = Field("", max_length=100)
    role: Literal["editor", "admin"] = "editor"
    password: str | None = Field(None, min_length=MIN_PASSWORD, max_length=200)


class UserPatch(BaseModel):
    username: str | None = Field(None, pattern=USERNAME)
    email: str | None = Field(None, max_length=254)  # "" = remove (no more Google login)
    name: str | None = Field(None, max_length=100)
    role: Literal["editor", "admin"] | None = None
    active: bool | None = None
    password: str | None = Field(None, min_length=MIN_PASSWORD, max_length=200)
    remove_password: bool = False
    unlink_google: bool = False


def _get_user(con, uid: str) -> sqlite3.Row:
    u = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        raise HTTPException(404, "User not found")
    return u


def _other_admins(con, uid: str) -> int:
    return con.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND id<>?", (uid,)).fetchone()[0]


def _unique(fn):
    try:
        return fn()
    except sqlite3.IntegrityError as e:
        msg = str(e)
        what = "user name" if "username" in msg else "e-mail address" if "email" in msg else "account"
        raise HTTPException(409, f"That {what} is already used by another user") from e


@users_router.get("")
def list_users():
    with adb() as con:
        return [public_user(u) for u in con.execute("SELECT * FROM users ORDER BY role DESC, created_at")]


@users_router.post("")
def create_user(body: UserCreate):
    if not body.username and not body.email:
        raise HTTPException(400, "Give a user name (password login) or an e-mail address (Google login)")
    if body.username and not body.password:
        raise HTTPException(400, "A user name needs a password")
    with adb() as con:
        uid = _unique(lambda: _insert_user(con, username=body.username, email=body.email, name=body.name.strip(),
                                           role=body.role, password=body.password))
        return public_user(_get_user(con, uid))


@users_router.patch("/{uid}")
def patch_user(uid: str, body: UserPatch, request: Request):
    me_ = need(request, "admin")
    with adb() as con:
        u = _get_user(con, uid)
        losing_admin = u["role"] == "admin" and u["active"] and (
            (body.role is not None and body.role != "admin") or body.active is False)
        if losing_admin and not _other_admins(con, uid):
            raise HTTPException(409, "This is the last admin. Make another user admin first.")
        if uid == me_["id"] and body.active is False:
            raise HTTPException(409, "You cannot deactivate yourself")
        sets: dict[str, object] = {}
        if body.username is not None:
            sets["username"] = body.username
        if body.email is not None:
            email = body.email.strip().lower()
            if email and not re.match(EMAIL, email):
                raise HTTPException(422, "Not an e-mail address")
            sets["email"] = email or None
            if email != (u["email"] or ""):
                sets["google_sub"] = None  # a new address: link the Google account again at its next login
        if body.name is not None:
            sets["name"] = body.name.strip()
        if body.role is not None:
            sets["role"] = body.role
        if body.active is not None:
            sets["active"] = int(body.active)
        if body.password:
            sets["password_hash"] = hash_password(body.password)
        elif body.remove_password:
            sets["password_hash"] = None
        if body.unlink_google:
            sets["google_sub"] = None
        after = {**dict(u), **sets}
        if not after["username"] and not after["email"]:
            raise HTTPException(400, "A user needs a user name or an e-mail address")
        if after["username"] and not after["password_hash"] and not after["email"]:
            raise HTTPException(400, "This user would have no way to log in")
        if sets:
            _unique(lambda: con.execute(f"UPDATE users SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                                        (*sets.values(), uid)))
        # anything that changes what the user may do logs them out everywhere
        if {"role", "active", "password_hash", "google_sub", "email"} & sets.keys() and uid != me_["id"]:
            con.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        return public_user(_get_user(con, uid))


@users_router.delete("/{uid}")
def delete_user(uid: str, request: Request):
    me_ = need(request, "admin")
    if uid == me_["id"]:
        raise HTTPException(409, "You cannot delete yourself")
    with adb() as con:
        u = _get_user(con, uid)
        if u["role"] == "admin" and u["active"] and not _other_admins(con, uid):
            raise HTTPException(409, "This is the last admin")
        con.execute("DELETE FROM users WHERE id=?", (uid,))
    return {"ok": True}


@users_router.post("/{uid}/logout")
def logout_user(uid: str):
    """End all sessions of a user (e.g. a lost laptop)."""
    with adb() as con:
        _get_user(con, uid)
    end_sessions(uid)
    return {"ok": True}


# ---------------------------------------------------------------- CLI

def _main() -> None:
    import argparse
    import getpass

    p = argparse.ArgumentParser(prog="python -m app.auth", description="Tabel dari PDF users")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-admin", help="create an admin with a password")
    c.add_argument("username")
    s = sub.add_parser("set-password", help="set a new password (logs the user out everywhere)")
    s.add_argument("username")
    sub.add_parser("list", help="list users")
    a = p.parse_args()
    init()

    def ask() -> str:
        pw = getpass.getpass("New password: ")
        if len(pw) < MIN_PASSWORD:
            raise SystemExit(f"At least {MIN_PASSWORD} characters")
        if getpass.getpass("Again: ") != pw:
            raise SystemExit("The passwords differ")
        return pw

    with adb() as con:
        if a.cmd == "list":
            for u in con.execute("SELECT * FROM users ORDER BY created_at"):
                print(f"{u['role']:7} {'active ' if u['active'] else 'blocked'} {u['username'] or '-':20} "
                      f"{u['email'] or '-':32} {u['name']}")
        elif a.cmd == "create-admin":
            if not re.match(USERNAME, a.username):
                raise SystemExit("User name: 3-40 letters, digits, . _ -")
            if con.execute("SELECT 1 FROM users WHERE username=?", (a.username,)).fetchone():
                raise SystemExit("That user exists already (use set-password)")
            _insert_user(con, username=a.username, email=None, name=a.username, role="admin", password=ask())
            print(f"admin '{a.username}' created")
        else:
            u = con.execute("SELECT id FROM users WHERE username=?", (a.username,)).fetchone()
            if not u:
                raise SystemExit("No such user")
            con.execute("UPDATE users SET password_hash=?, active=1 WHERE id=?", (hash_password(ask()), u["id"]))
            con.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))
            print("password changed")


if __name__ == "__main__":
    _main()
