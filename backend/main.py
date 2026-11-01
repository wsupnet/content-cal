from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.requests import cookie_parser
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlmodel import SQLModel, Field, Column, JSON, create_engine, Session, select
from sqlalchemy import text, func
from sqlalchemy.exc import IntegrityError
from typing import Optional, List
from datetime import date as _date, datetime, timedelta, timezone as _tz
import asyncio, hashlib, json, os, pathlib, re, secrets, time
from collections import deque
try:
    from zoneinfo import ZoneInfo, available_timezones  # py3.9+
    _HAS_ZONEINFO = True
except Exception:
    _HAS_ZONEINFO = False
    ZoneInfo = None  # type: ignore
    def available_timezones():
        return set()

def _utcnow() -> datetime:
    # Naive UTC, same as the deprecated datetime.utcnow(). Stored timestamps
    # are naive ISO strings compared as text, so they must not gain "+00:00".
    return datetime.now(_tz.utc).replace(tzinfo=None)

import bcrypt as _bcrypt
from itsdangerous import URLSafeTimedSerializer, BadSignature

# ── Validation constants / helpers ───────────────────────────────
VALID_STATUSES = {"idea", "filming", "editing", "ready", "scheduled", "published"}
VALID_FORMATS = {"", "short", "long"}
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_ICON_LEN = 300_000  # ~300KB cap on stored channel icon (base64 or url)
MAX_NAME_LEN = 200
MAX_TEXT_LEN = 5000
MAX_NOTES_LEN = 20_000

def _validate_color(c: Optional[str]) -> str:
    c = (c or "").strip()
    if not c:
        return ""
    if not HEX_COLOR_RE.match(c):
        raise HTTPException(status_code=400, detail="Channel color must be a #RRGGBB hex string.")
    return c

def _validate_icon(s: Optional[str]) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    if len(s) > MAX_ICON_LEN:
        raise HTTPException(status_code=400, detail=f"Channel icon exceeds {MAX_ICON_LEN} bytes.")
    # Uploaded images only. A remote https:// icon would make every member's
    # browser request it on each page load, letting whoever runs that server
    # log their IPs and activity (a tracking pixel).
    if not s.startswith("data:image/"):
        raise HTTPException(status_code=400, detail="Channel icon must be an uploaded image (data:image/*).")
    # Forbid characters that could break out of an HTML attribute even if the
    # frontend forgets to escape (defense in depth).
    if any(ch in s for ch in ('"', "'", "<", ">", "\n", "\r")):
        raise HTTPException(status_code=400, detail="Channel icon contains illegal characters.")
    return s

def _validate_status(s: str) -> str:
    s = (s or "").strip()
    if s not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status '{s}'.")
    return s

def _validate_format(s: Optional[str]) -> str:
    s = (s or "").strip()
    if s not in VALID_FORMATS:
        raise HTTPException(status_code=400, detail=f"Invalid format '{s}'.")
    return s

# New usernames are limited to a plain charset so they can never carry markup
# or script into the UI. Existing accounts keep whatever name they have.
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,80}$")

def _validate_username(s: str) -> str:
    s = (s or "").strip()
    if not USERNAME_RE.match(s):
        raise HTTPException(
            status_code=400,
            detail="Username must be 2–80 characters: letters, numbers, '_', '.' or '-'.",
        )
    return s

def _validate_name(s: str, *, field: str, max_len: int = MAX_NAME_LEN) -> str:
    s = (s or "").strip()
    if not s:
        raise HTTPException(status_code=400, detail=f"{field} is required.")
    if len(s) > max_len:
        raise HTTPException(status_code=400, detail=f"{field} exceeds {max_len} characters.")
    return s

def _validate_text(s: Optional[str], *, field: str, max_len: int = MAX_TEXT_LEN) -> str:
    s = s or ""
    if len(s) > max_len:
        raise HTTPException(status_code=400, detail=f"{field} exceeds {max_len} characters.")
    return s

# ── Paths / config ───────────────────────────────────────────────
# Every data path derives from DATA_DIR — no other path settings.
DATA_DIR = pathlib.Path(os.getenv("DATA_DIR", "/app/data")).resolve()
DB_FILE = DATA_DIR / "calendar.db"
JSON_FILE = DATA_DIR / "calendar.json"
SECRET_FILE = DATA_DIR / ".session_secret"

# REQUIRE_EXISTING_DB=1 makes a missing database fatal instead of silently
# starting a fresh empty one (e.g. when the data volume isn't mounted where
# expected). Leave it unset for a brand-new install's first run.
REQUIRE_EXISTING_DB = os.getenv("REQUIRE_EXISTING_DB", "").strip().lower() in ("1", "true", "yes")

# Sign-up needs an invite link unless ALLOW_OPEN_SIGNUP=1, so a server
# reachable from the internet doesn't hand out accounts to anyone.
ALLOW_OPEN_SIGNUP = os.getenv("ALLOW_OPEN_SIGNUP", "").strip().lower() in ("1", "true", "yes")

# Mark the session cookie Secure (HTTPS-only). Turn on when the app is served
# over HTTPS (reverse proxy, Tailscale Serve); leave off for plain-HTTP LAN use
# or browsers will drop the cookie and nobody can log in.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes")
if REQUIRE_EXISTING_DB and not DB_FILE.is_file():
    raise RuntimeError(
        f"[fatal] REQUIRE_EXISTING_DB is set but no database exists at {DB_FILE}. "
        "Check that the data volume is mounted at DATA_DIR. Refusing to create an empty one."
    )
# Files the app creates (calendar.db, its journal, .session_secret) are
# readable only by the account the server runs as. They hold password hashes.
try:
    os.umask(0o077)
except Exception:
    pass
DATA_DIR.mkdir(parents=True, exist_ok=True)
print(
    f"[init] DATA_DIR={DATA_DIR} DB_FILE={DB_FILE} "
    + (f"(exists, {DB_FILE.stat().st_size} bytes)" if DB_FILE.is_file() else "(missing — a new empty database will be created)")
)

# Session secret: read from env, or persist a generated one in the data dir so
# session cookies survive container restarts (only regenerates if the file is
# deleted). Operators can override via SESSION_SECRET env var.
def _load_or_create_secret() -> str:
    env = os.getenv("SESSION_SECRET")
    if env:
        return env
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text().strip()
    s = secrets.token_urlsafe(48)
    SECRET_FILE.write_text(s)
    try:
        os.chmod(SECRET_FILE, 0o600)
    except Exception:
        pass
    return s

# Best-effort in-memory rate limiter. Throttles the public invite preview (so
# a script can't enumerate codes) and the auth endpoints (so passwords can't
# be brute-forced). Limits live in this process and reset on restart, which is
# one reason ContentCal must run as a single process (see the docs).
#
# Each bucket remembers its own window so housekeeping never drops history a
# longer-window limiter still needs. A check with record=False only peeks and
# never creates a bucket.
_RATE_BUCKETS: dict = {}          # key -> [window_seconds, deque of hit times]
_RATE_SWEEP_AT = 10_000           # sweep expired buckets once the dict passes this
_RATE_MAX_KEYS = 100_000          # hard cap; oldest buckets are evicted beyond it

def _check_rate(key: str, *, max_hits: int, window_seconds: int, record: bool = True) -> bool:
    now = time.monotonic()
    entry = _RATE_BUCKETS.get(key)
    if entry is not None:
        bucket = entry[1]
        cutoff = now - window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= max_hits:
            return False
    if not record:
        return True
    if entry is None:
        entry = [window_seconds, deque()]
        _RATE_BUCKETS[key] = entry
    entry[1].append(now)
    if len(_RATE_BUCKETS) > _RATE_SWEEP_AT:
        _sweep_rate_buckets(now)
    return True

def _sweep_rate_buckets(now: float) -> None:
    global _RATE_SWEEP_AT
    for k in [k for k, (win, b) in _RATE_BUCKETS.items() if not b or b[-1] < now - win]:
        del _RATE_BUCKETS[k]
    # Still huge (a flood of distinct keys)? Evict the oldest-created buckets.
    overflow = len(_RATE_BUCKETS) - _RATE_MAX_KEYS
    if overflow > 0:
        for k in list(_RATE_BUCKETS)[:overflow]:
            del _RATE_BUCKETS[k]
    # Sweep again only after the dict doubles, so sweeps stay amortized O(1).
    _RATE_SWEEP_AT = max(10_000, 2 * len(_RATE_BUCKETS))

def _client_ip(request: Request) -> str:
    # Never read X-Forwarded-For here: any client can send it, which would let
    # a script dodge the rate limits by inventing a new IP per request. Uvicorn
    # already rewrites request.client from that header, but only for proxies
    # listed in the FORWARDED_ALLOW_IPS env var (default 127.0.0.1).
    return request.client.host if request.client else "?"

# Auth throttles. Brute force is stopped per account: failed logins are
# counted per (exact username, client IP), plus a higher per-username cap for
# clients that have never signed in to that account. A browser that has signed
# in before carries a signed device cookie and gets its own bucket, so a
# stranger hammering an account can't lock its owner out.
#
# The per-IP caps are deliberately generous: behind NAT, Docker Desktop, a
# reverse proxy or Tailscale every user can share one IP, and these caps then
# apply to all of them together.
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_PER_IP = 100                # all login attempts per IP per window
LOGIN_MAX_FAILS_PER_ACCOUNT_IP = 10   # failures per (username, IP) per window
LOGIN_MAX_FAILS_PER_ACCOUNT = 50      # failures per username, all IPs (unknown devices only)
LOGIN_MAX_FAILS_PER_DEVICE = 10       # failures per known device per window
SIGNUP_MAX_PER_IP = 30                # account creations per IP per hour
SETUP_MAX_PER_IP = 10                 # setup attempts per IP per window
INVITE_PREVIEW_MAX_PER_IP = 60        # invite previews per IP per minute

def _too_many(detail: str = "Too many attempts. Try again in a few minutes.") -> HTTPException:
    return HTTPException(status_code=429, detail=detail)

SESSION_SECRET = _load_or_create_secret()
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

# Cookies are scoped by host, not port, so two ContentCal installs (or any
# other app) on the same host would share one cookie name and log each other
# out. The name carries a short tag derived from this install's secret. With
# COOKIE_SECURE the __Host- prefix also pins the cookie to this exact origin.
# Other apps on the same host still receive it; only a dedicated hostname per
# app isolates it fully (see the README's security notes).
_COOKIE_TAG = hashlib.sha256(b"contentcal-cookie-name:" + SESSION_SECRET.encode()).hexdigest()[:8]
_COOKIE_PREFIX = "__Host-" if COOKIE_SECURE else ""
SESSION_COOKIE = f"{_COOKIE_PREFIX}contentcal_session_{_COOKIE_TAG}"
DEVICE_COOKIE = f"{_COOKIE_PREFIX}contentcal_device_{_COOKIE_TAG}"
LEGACY_SESSION_COOKIE = "contentcal_session"  # name used before per-install names
DEVICE_MAX_AGE = 60 * 60 * 24 * 365

# Timed so the server, not just the browser, enforces SESSION_MAX_AGE. The salt
# changed from v1, so cookies issued before this version are rejected and
# everyone signs in once after upgrading.
session_serializer = URLSafeTimedSerializer(SESSION_SECRET, salt="session-v2")
# Marks a browser that has signed in to an account before (see the login throttles).
device_serializer = URLSafeTimedSerializer(SESSION_SECRET, salt="device-v1")

# bcrypt has a hard 72-byte input limit. Truncate (UTF-8 byte-safe) so longer
# passphrases don't raise — common-enough that passlib also handles this.
def hash_password(pw: str) -> str:
    pw_bytes = pw.encode("utf-8")[:72]
    return _bcrypt.hashpw(pw_bytes, _bcrypt.gensalt()).decode("utf-8")

# Checked against when the username doesn't exist, so a login for a missing
# account takes as long as a wrong password (no username probing by timing).
_DUMMY_HASH = _bcrypt.hashpw(secrets.token_bytes(16), _bcrypt.gensalt()).decode("utf-8")

def verify_password(pw: str, stored_hash: str) -> bool:
    try:
        return _bcrypt.checkpw(pw.encode("utf-8")[:72], stored_hash.encode("utf-8"))
    except Exception:
        return False

DEFAULT_CHANNELS = [
    {"name": "Japan Life", "pillars": ["Food", "Travel", "Daily Life"]},
    {"name": "Tech & Self-Host", "pillars": ["NAS", "Docker", "Tutorials"]},
    {"name": "Real Estate", "pillars": ["Investing", "Management", "Tax Tips"]},
]

# ── DB Models ────────────────────────────────────────────────────
# Existing tables now carry a workspace_id FK. workspace_id=0 means "unadopted"
# (legacy data from before accounts existed). On first-run setup that data is
# claimed by the first user's workspace.
class Channel(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(default=0, index=True)
    position: int = Field(index=True)
    name: str
    pillars: List[str] = Field(default_factory=list, sa_column=Column(JSON))
    color: str = ""
    icon: str = ""  # may hold base64 image data
    archived: bool = Field(default=False)

class Goal(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(default=0, index=True)
    name: str = ""
    target: int = 0
    deadline: str = ""

class Entry(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(default=0, index=True)
    title: str
    channel: int = Field(index=True)  # positional index of channel (within workspace)
    date: str = Field(index=True)
    status: str
    pillar: str = ""
    notes: str = ""
    format: str = ""
    link: str = ""

class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    default_workspace_id: int = 0
    created_at: str = Field(default_factory=lambda: _utcnow().isoformat())
    # Embedded in every session cookie. Bumping it (on logout) invalidates
    # all of the user's existing cookies, including stolen copies.
    session_version: int = 0

class Workspace(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    created_by_user_id: int = 0
    created_at: str = Field(default_factory=lambda: _utcnow().isoformat())
    # Monotonic counter bumped on every channel-list mutation. Clients send the
    # value they last saw; mismatches trigger a 409 so concurrent edits don't
    # silently clobber each other (since Entry.channel is a positional index).
    channels_version: int = 0
    # IANA timezone name (e.g. "Asia/Tokyo"). Used by auto-publish so a
    # workspace in JST doesn't flip "today" rows hours early because the
    # container clock is UTC. Empty string = UTC.
    timezone: str = ""

class WorkspaceMember(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(index=True)
    user_id: int = Field(index=True)
    role: str = Field(default="member")  # 'owner' | 'member'
    joined_at: str = Field(default_factory=lambda: _utcnow().isoformat())

class Comment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    entry_id: int = Field(index=True)
    workspace_id: int = Field(index=True)
    user_id: int = Field(index=True)
    body: str
    created_at: str = Field(default_factory=lambda: _utcnow().isoformat())
    edited_at: Optional[str] = None

class ActivityEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    entry_id: int = Field(index=True)
    workspace_id: int = Field(index=True)
    user_id: int = Field(index=True)  # actor who triggered the change
    event_type: str  # 'status_changed' | 'date_changed'
    from_value: str = ""  # raw value (status key, or ISO date, or "" for blank)
    to_value: str = ""
    created_at: str = Field(default_factory=lambda: _utcnow().isoformat())

class WorkspaceInvite(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(index=True)
    code: str = Field(index=True, unique=True)
    created_by_user_id: int
    created_at: str = Field(default_factory=lambda: _utcnow().isoformat())
    expires_at: Optional[str] = None  # ISO datetime; None = no expiry
    used_by_user_id: Optional[int] = None
    used_at: Optional[str] = None
    revoked_at: Optional[str] = None

# ── API Input Schemas ─────────────────────────────────────────────
class ChannelIn(SQLModel):
    name: str
    pillars: List[str] = []
    color: Optional[str] = ""
    icon: Optional[str] = ""
    archived: Optional[bool] = False

class GoalIn(SQLModel):
    name: Optional[str] = ""
    target: Optional[int] = 0
    deadline: Optional[str] = ""

class EntryIn(SQLModel):
    id: Optional[int] = None
    title: str
    channel: int
    date: Optional[str] = ""  # optional for idea/filming/editing/ready
    status: str
    pillar: Optional[str] = ""
    notes: Optional[str] = ""
    format: Optional[str] = ""
    link: Optional[str] = ""

class SignupIn(SQLModel):
    username: str
    password: str
    invite_code: Optional[str] = None  # if present, also joins that workspace

class LoginIn(SQLModel):
    username: str
    password: str

class SetupIn(SQLModel):
    username: str
    password: str
    setup_code: str = ""
    workspace_name: Optional[str] = None  # defaults to "{username}'s Workspace"

class WorkspaceIn(SQLModel):
    name: str

class WorkspaceSettingsIn(SQLModel):
    timezone: Optional[str] = None  # IANA name; "" = UTC

class MemberRoleIn(SQLModel):
    role: str  # 'owner' | 'member'

class PasswordChangeIn(SQLModel):
    current_password: str
    new_password: str

class SwitchWorkspaceIn(SQLModel):
    workspace_id: int

class InviteIn(SQLModel):
    expires_in_days: Optional[int] = None  # None = no expiry

class CommentIn(SQLModel):
    body: str

# Hard cap on comment body length — prevents accidental novel-sized payloads.
COMMENT_MAX_LEN = 5000

# ── Engine ────────────────────────────────────────────────────────
engine = create_engine(
    f"sqlite:///{DB_FILE}",
    connect_args={"check_same_thread": False},
    echo=False,
)

# ── Serializers ───────────────────────────────────────────────────
def channel_dict(c: Channel) -> dict:
    return {
        "name": c.name,
        "pillars": c.pillars or [],
        "color": c.color or "",
        # Remote icons saved before uploads-only are dropped here, so the next
        # channel save clears them instead of failing validation.
        "icon": c.icon if (c.icon or "").startswith("data:image/") else "",
        "archived": bool(c.archived),
    }

def goal_dict(g: Optional[Goal]) -> dict:
    if not g:
        return {"name": "", "target": 0, "deadline": ""}
    return {"name": g.name or "", "target": g.target or 0, "deadline": g.deadline or ""}

def entry_dict(e: Entry) -> dict:
    # comment_count is filled in by /api/data; for single-entry POST/PUT
    # responses it defaults to 0, which is correct (new entries can't have
    # comments yet, and edits don't affect existing counts).
    return {
        "id": e.id,
        "title": e.title,
        "channel": e.channel,
        "date": e.date,
        "status": e.status,
        "pillar": e.pillar or "",
        "notes": e.notes or "",
        "format": e.format or "",
        "link": e.link or "",
        "comment_count": 0,
    }

def user_dict(u: User) -> dict:
    return {"id": u.id, "username": u.username, "default_workspace_id": u.default_workspace_id}

def workspace_dict(w: Workspace) -> dict:
    return {
        "id": w.id, "name": w.name, "created_at": w.created_at,
        "timezone": (w.timezone or ""),
    }

def _workspace_today_iso(ws: Workspace) -> str:
    """Return today's date as ISO string in the workspace's configured tz,
    falling back to UTC if zoneinfo isn't available or the name is unknown."""
    tz_name = (ws.timezone or "").strip()
    if tz_name and _HAS_ZONEINFO:
        try:
            return datetime.now(ZoneInfo(tz_name)).date().isoformat()
        except Exception:
            pass
    return _date.today().isoformat()

def comment_dict(c: Comment, username: str = "") -> dict:
    return {
        "kind": "comment",
        "id": c.id,
        "entry_id": c.entry_id,
        "user_id": c.user_id,
        "username": username,
        "body": c.body,
        "created_at": c.created_at,
        "edited_at": c.edited_at,
    }

def activity_dict(e: ActivityEvent, username: str = "") -> dict:
    return {
        "kind": "activity",
        "id": e.id,
        "entry_id": e.entry_id,
        "user_id": e.user_id,
        "username": username,
        "event_type": e.event_type,
        "from_value": e.from_value or "",
        "to_value": e.to_value or "",
        "created_at": e.created_at,
    }

def invite_dict(inv: WorkspaceInvite, ws_name: str = "") -> dict:
    return {
        "id": inv.id,
        "code": inv.code,
        "workspace_id": inv.workspace_id,
        "workspace_name": ws_name,
        "created_at": inv.created_at,
        "expires_at": inv.expires_at,
        "used": inv.used_by_user_id is not None,
        "revoked": inv.revoked_at is not None,
    }

# ── Schema migration helpers ─────────────────────────────────────
# SQLite + SQLModel don't auto-migrate when new columns are added to existing
# tables, so we do a small idempotent ALTER TABLE pass on startup for each
# column we've introduced after v1. Safe to run every boot.
def _column_exists(session: Session, table: str, column: str) -> bool:
    rows = session.exec(text(f"PRAGMA table_info({table})")).all()
    # rows = [(cid, name, type, notnull, dflt_value, pk), ...]
    return any(r[1] == column for r in rows)

def _ensure_column(session: Session, table: str, column: str, ddl_type: str, default: str):
    if not _column_exists(session, table, column):
        session.exec(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type} DEFAULT {default}"))
        session.commit()
        print(f"[migration] Added column {table}.{column}")

# ── Init / Migration ──────────────────────────────────────────────
def init_db():
    SQLModel.metadata.create_all(engine)
    # Databases created before the umask above may be world-readable; tighten
    # them too. Best effort: some filesystems/OSes ignore chmod.
    try:
        os.chmod(DB_FILE, 0o600)
    except Exception:
        pass
    with Session(engine) as session:
        # Idempotent backfill of workspace_id columns on legacy DBs.
        _ensure_column(session, "channel", "workspace_id", "INTEGER", "0")
        _ensure_column(session, "channel", "archived", "INTEGER", "0")
        _ensure_column(session, "entry", "workspace_id", "INTEGER", "0")
        _ensure_column(session, "goal", "workspace_id", "INTEGER", "0")
        # New columns added during the hardening pass.
        _ensure_column(session, "workspace", "channels_version", "INTEGER", "0")
        _ensure_column(session, "workspace", "timezone", "TEXT", "''")
        _ensure_column(session, "workspacemember", "role", "TEXT", "'member'")
        _ensure_column(session, "user", "session_version", "INTEGER", "0")
        # Promote each workspace's creator to owner if no owner is set yet.
        try:
            session.exec(text(
                "UPDATE workspacemember SET role='owner' "
                "WHERE role='member' "
                "AND (workspace_id, user_id) IN "
                "(SELECT id, created_by_user_id FROM workspace WHERE created_by_user_id != 0) "
                "AND NOT EXISTS (SELECT 1 FROM workspacemember m2 "
                "  WHERE m2.workspace_id = workspacemember.workspace_id AND m2.role='owner')"
            ))
            session.commit()
        except Exception as ex:
            print(f"[migration] role backfill skipped: {ex}")
            session.rollback()

        # Seed legacy JSON only if we have a completely empty DB (no channels
        # AND no users — otherwise the data already lives somewhere coherent).
        has_channels = session.exec(select(Channel).limit(1)).first() is not None
        has_users = session.exec(select(User).limit(1)).first() is not None
        if has_channels or has_users:
            return

        if JSON_FILE.exists():
            try:
                with open(JSON_FILE) as f:
                    data = json.load(f)
                channels_in = data.get("channels", [])
                entries_in = data.get("entries", [])
                for i, ch in enumerate(channels_in):
                    session.add(Channel(
                        position=i,
                        name=ch.get("name", f"Channel {i+1}"),
                        pillars=ch.get("pillars", []) or [],
                        color=ch.get("color", "") or "",
                        icon=ch.get("icon", "") or "",
                    ))
                for e in entries_in:
                    session.add(Entry(
                        id=e.get("id"),
                        title=e.get("title", ""),
                        channel=int(e.get("channel", 0)),
                        date=e.get("date", ""),
                        status=e.get("status", "idea"),
                        pillar=e.get("pillar", "") or "",
                        notes=e.get("notes", "") or "",
                        format=e.get("format", "") or "",
                        link=e.get("link", "") or "",
                    ))
                session.commit()
                backup = JSON_FILE.with_name(JSON_FILE.name + ".migrated")
                JSON_FILE.rename(backup)
                print(f"[migration] Imported {len(channels_in)} channels and {len(entries_in)} entries from {JSON_FILE.name} (renamed to {backup.name})")
                return
            except Exception as ex:
                print(f"[migration] Failed to import {JSON_FILE}: {ex}")
                session.rollback()

        # Empty install: seed defaults (still workspace_id=0; first-run setup
        # claims them for the new admin's workspace).
        for i, ch in enumerate(DEFAULT_CHANNELS):
            session.add(Channel(position=i, name=ch["name"], pillars=ch["pillars"]))
        session.commit()
        print("[init] Seeded default channels (empty calendar, awaiting first-run setup)")

# ── Lifespan ──────────────────────────────────────────────────────
def _warn_on_orphan_zero_workspace() -> None:
    """If setup has run (users exist) but any row still has workspace_id=0,
    that's data nobody owns. Surface it loudly so an operator can investigate
    instead of letting it accumulate silently."""
    with Session(engine) as session:
        if not session.exec(select(User).limit(1)).first():
            return
        for table, label in ((Channel, "channel"), (Entry, "entry"), (Goal, "goal")):
            n = session.exec(
                select(func.count(table.id)).where(table.workspace_id == 0)
            ).one()
            if n:
                print(
                    f"[warning] {n} orphaned {label}(s) with workspace_id=0 detected. "
                    "These rows are invisible to every workspace and accumulate forever. "
                    "Migrate them into a real workspace or delete them."
                )

# One-time code required by first-run setup, so a stranger who reaches a fresh
# install before its owner can't claim the admin account. Regenerated on every
# start and printed to the server log (docker logs / Dockhand) while no
# account exists.
# SETUP_CODE can be set explicitly; otherwise it's random per start.
SETUP_CODE = (os.getenv("SETUP_CODE", "").strip().upper() or secrets.token_hex(4).upper())

def _print_setup_code_if_needed() -> None:
    with Session(engine) as session:
        if session.exec(select(User).limit(1)).first():
            return
    print(
        f"[setup] No accounts yet. First-run setup code: {SETUP_CODE}\n"
        "[setup] Enter it in the setup screen to create the admin account.",
        flush=True,  # Docker block-buffers stdout; the code must reach the log now.
    )

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _warn_on_orphan_zero_workspace()
    _print_setup_code_if_needed()
    yield

# The interactive API docs are off unless ENABLE_API_DOCS=1: they publish a map
# of every endpoint, and /docs loads Swagger UI from a third-party CDN.
_API_DOCS = os.getenv("ENABLE_API_DOCS", "").strip().lower() in ("1", "true", "yes")
app = FastAPI(
    title="ContentCal API",
    lifespan=lifespan,
    docs_url="/docs" if _API_DOCS else None,
    redoc_url="/redoc" if _API_DOCS else None,
    openapi_url="/openapi.json" if _API_DOCS else None,
)

# CORS: frontend is served same-origin via StaticFiles, so by default we
# disable cross-origin entirely. Set ALLOWED_ORIGINS (comma-separated) to
# enable specific origins; we never use "*" because cookies require
# credentialed CORS and `*` is silently refused by browsers when credentials
# are present.
_cors_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Request guard (H1). FastAPI reads and parses the whole JSON body before any
# auth dependency runs, so without this an anonymous client can make the
# server parse megabytes per request. Measured before this guard: 20
# concurrent 5 MB bodies pushed memory to 4.6 GB. The guard, before any body
# is read:
#   - rejects /api/* requests that need a login but carry no validly signed
#     session cookie (401), so anonymous bodies are never read;
#   - caps the body per route: 16 KB for public endpoints, 256 KB for normal
#     authenticated ones, MAX_REQUEST_BYTES (2.5 MB) only for the two bulk
#     endpoints (channel save with six 300 KB icons; CSV import chunks);
#   - lets at most two large bodies be processed at once.
PUBLIC_BODY_BYTES = 16 * 1024
DEFAULT_BODY_BYTES = 256 * 1024
MAX_REQUEST_BYTES = int(os.getenv("MAX_REQUEST_BYTES", str(int(2.5 * 1024 * 1024))))
_LARGE_BODY_ROUTES = {("PUT", "/api/channels"), ("POST", "/api/entries/bulk")}
_large_body_slots = asyncio.Semaphore(2)

def _is_public_api(method: str, path: str) -> bool:
    if path.startswith("/api/auth/") or path == "/api/timezones":
        return True
    # GET /api/invites/{code} (the invite preview on the join page).
    return method == "GET" and re.fullmatch(r"/api/invites/[^/]+", path) is not None

def _has_signed_session(scope) -> bool:
    """Cheap pre-check: a session cookie with a valid signature and age. The
    full check (account exists, session version) stays in current_user."""
    for name, value in scope.get("headers", []):
        if name == b"cookie":
            raw = cookie_parser(value.decode("latin-1")).get(SESSION_COOKIE)
            if raw:
                try:
                    session_serializer.loads(raw, max_age=SESSION_MAX_AGE)
                    return True
                except BadSignature:
                    return False
    return False

class RequestGuardMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        method, path = scope["method"], scope["path"]
        if path.startswith("/api/"):
            if _is_public_api(method, path):
                cap = PUBLIC_BODY_BYTES
            else:
                if not _has_signed_session(scope):
                    return await _send_json(send, 401, {"detail": "Not authenticated"})
                cap = MAX_REQUEST_BYTES if (method, path) in _LARGE_BODY_ROUTES else DEFAULT_BODY_BYTES
        else:
            cap = PUBLIC_BODY_BYTES  # static files never take a body
        declared = None
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = cap + 1
        if declared is not None and declared > cap:
            return await _send_json(send, 413, {"detail": "Request body too large."})
        if declared is not None and declared <= DEFAULT_BODY_BYTES:
            return await self.app(scope, receive, send)
        # Large or undeclared length: take a slot, then (for chunked bodies)
        # read at most `cap` bytes and replay them to the app.
        async with _large_body_slots:
            if declared is not None:
                return await self.app(scope, receive, send)
            chunks, size = [], 0
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    break
                size += len(message.get("body", b""))
                if size > cap:
                    return await _send_json(send, 413, {"detail": "Request body too large."})
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            replayed = False

            async def replay_receive():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
                return await receive()

            await self.app(scope, replay_receive, send)

async def _send_json(send, status_code: int, payload: dict) -> None:
    body = json.dumps(payload).encode()
    await send({
        "type": "http.response.start", "status": status_code,
        "headers": [(b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})

app.add_middleware(RequestGuardMiddleware)

# Validation errors never echo the request back (FastAPI's default 422 copies
# each offending input into the response, which turned a 5 MB body into a
# 5 MB reply) and are capped at 20 entries.
def _validation_errors(errors) -> list:
    return [{"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in list(errors)[:20]]

@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": _validation_errors(exc.errors())})

async def _json_list_body(request: Request, max_items: int, label: str) -> list:
    """Reads a JSON array body and checks its length *before* validating any
    item, so a huge array can't make pydantic build one error per element."""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body must be valid JSON.")
    if not isinstance(data, list):
        raise HTTPException(status_code=400, detail=f"Body must be a JSON array of {label}.")
    if len(data) > max_items:
        raise HTTPException(status_code=400, detail=f"Maximum {max_items} {label} supported per request.")
    return data

async def bulk_entries_body(request: Request) -> list:
    return await _json_list_body(request, 1000, "rows")

async def channels_body(request: Request) -> list:
    return await _json_list_body(request, 6, "channels")

# Browser hardening headers on every response. The CSP stops the page from
# fetching, posting to or loading images/scripts/fonts from any other origin
# (connect-src/img-src 'self'), which blocks the usual quiet ways injected
# script would send data out. It can't stop top-level navigation
# (location=, window.open), so it limits XSS rather than preventing exfiltration
# outright. Inline script and styles stay allowed because the single-file
# frontend relies on them. Strict-Transport-Security is only sent with
# COOKIE_SECURE=1, i.e. when the operator says the app is served over HTTPS.
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])
SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
]
if COOKIE_SECURE:
    SECURITY_HEADERS.append((b"strict-transport-security", b"max-age=31536000"))
# /docs and /redoc (only served with ENABLE_API_DOCS=1) load Swagger from a CDN.
_CSP_EXEMPT_PATHS = ("/docs", "/redoc")

class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        csp = not (_API_DOCS and scope.get("path", "").startswith(_CSP_EXEMPT_PATHS))

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = [h for h in message.get("headers", []) if h[0].lower() not in {n for n, _ in SECURITY_HEADERS}]
                headers += SECURITY_HEADERS
                if csp:
                    headers.append((b"content-security-policy", CONTENT_SECURITY_POLICY.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)

app.add_middleware(SecurityHeadersMiddleware)

# ── Session helpers ──────────────────────────────────────────────
def _set_session_cookie(response: Response, user: "User", active_workspace_id: int):
    token = session_serializer.dumps({
        "uid": user.id, "wsid": active_workspace_id, "sv": user.session_version or 0,
    })
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        path="/",
    )
    # Drop the pre-upgrade cookie name; it's no longer valid anyway.
    response.delete_cookie(LEGACY_SESSION_COOKIE, path="/")

def _clear_session_cookie(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/", secure=COOKIE_SECURE, httponly=True, samesite="lax")

def _set_device_cookie(response: Response, username: str):
    token = device_serializer.dumps({"u": username, "d": secrets.token_urlsafe(16)})
    response.set_cookie(
        DEVICE_COOKIE, token, max_age=DEVICE_MAX_AGE,
        httponly=True, samesite="lax", secure=COOKIE_SECURE, path="/",
    )

def _read_device(request: Request) -> Optional[dict]:
    raw = request.cookies.get(DEVICE_COOKIE)
    if not raw:
        return None
    try:
        dev = device_serializer.loads(raw, max_age=DEVICE_MAX_AGE)
    except BadSignature:
        return None
    return dev if isinstance(dev, dict) and "u" in dev and "d" in dev else None

def _read_session(request: Request) -> Optional[dict]:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        # BadSignature also covers SignatureExpired (older than max_age).
        sess = session_serializer.loads(raw, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    return sess if isinstance(sess, dict) and "uid" in sess else None

def _session_user(session: Session, sess: dict) -> Optional[User]:
    """The cookie's user, or None if the account is gone or the cookie was
    revoked (its session version no longer matches the user's)."""
    u = session.get(User, sess["uid"])
    if not u or sess.get("sv", -1) != (u.session_version or 0):
        return None
    return u

# ── Auth dependencies ────────────────────────────────────────────
def current_user(request: Request) -> User:
    sess = _read_session(request)
    if not sess:
        raise HTTPException(status_code=401, detail="Not authenticated")
    with Session(engine) as session:
        u = _session_user(session, sess)
        if not u:
            raise HTTPException(status_code=401, detail="Session expired. Please log in again.")
        return u

def current_user_optional(request: Request) -> Optional[User]:
    sess = _read_session(request)
    if not sess:
        return None
    with Session(engine) as session:
        return _session_user(session, sess)

def current_workspace(request: Request, user: User = Depends(current_user)) -> Workspace:
    """Resolves the active workspace from the session cookie and verifies the
    user is a member. Strict: if the cookie's workspace is no longer
    accessible we return 409 (not silently swap to a different workspace),
    so the client refetches /api/me and re-renders instead of writing into
    the wrong workspace."""
    sess = _read_session(request) or {}
    wsid = sess.get("wsid") or user.default_workspace_id
    with Session(engine) as session:
        member = session.exec(
            select(WorkspaceMember).where(
                (WorkspaceMember.workspace_id == wsid) & (WorkspaceMember.user_id == user.id)
            )
        ).first()
        if not member:
            raise HTTPException(
                status_code=409,
                detail="Your active workspace is no longer accessible. Please refresh.",
            )
        ws = session.get(Workspace, wsid)
        if not ws:
            raise HTTPException(status_code=409, detail="Workspace not found.")
        return ws

def owner_workspace(ws: Workspace = Depends(current_workspace), user: User = Depends(current_user)) -> Workspace:
    """current_workspace, restricted to owners. Used for structural changes
    (channels, goal) that members shouldn't be able to make."""
    with Session(engine) as session:
        _require_owner(session, ws.id, user.id)
    return ws

def _user_workspaces(session: Session, user_id: int) -> List[Workspace]:
    member_rows = session.exec(select(WorkspaceMember).where(WorkspaceMember.user_id == user_id)).all()
    ids = [m.workspace_id for m in member_rows]
    if not ids:
        return []
    return session.exec(select(Workspace).where(Workspace.id.in_(ids))).all()

def _is_member(session: Session, workspace_id: int, user_id: int) -> bool:
    return session.exec(
        select(WorkspaceMember).where(
            (WorkspaceMember.workspace_id == workspace_id) & (WorkspaceMember.user_id == user_id)
        )
    ).first() is not None

def _count_owners(session: Session, workspace_id: int) -> int:
    return session.exec(
        select(func.count(WorkspaceMember.id)).where(
            (WorkspaceMember.workspace_id == workspace_id) & (WorkspaceMember.role == "owner")
        )
    ).one()

def _is_owner(session: Session, workspace_id: int, user_id: int) -> bool:
    m = session.exec(
        select(WorkspaceMember).where(
            (WorkspaceMember.workspace_id == workspace_id) & (WorkspaceMember.user_id == user_id)
        )
    ).first()
    return bool(m and m.role == "owner")

def _require_owner(session: Session, workspace_id: int, user_id: int) -> None:
    if not _is_owner(session, workspace_id, user_id):
        raise HTTPException(status_code=403, detail="Only the workspace owner can do that.")

def _delete_workspace_cascade(session: Session, workspace_id: int) -> None:
    """Hard-delete a workspace and every row scoped to it. Called when the
    last member leaves so we don't leave orphaned data behind."""
    for c in session.exec(select(Comment).where(Comment.workspace_id == workspace_id)).all():
        session.delete(c)
    for a in session.exec(select(ActivityEvent).where(ActivityEvent.workspace_id == workspace_id)).all():
        session.delete(a)
    for e in session.exec(select(Entry).where(Entry.workspace_id == workspace_id)).all():
        session.delete(e)
    for ch in session.exec(select(Channel).where(Channel.workspace_id == workspace_id)).all():
        session.delete(ch)
    for g in session.exec(select(Goal).where(Goal.workspace_id == workspace_id)).all():
        session.delete(g)
    for inv in session.exec(select(WorkspaceInvite).where(WorkspaceInvite.workspace_id == workspace_id)).all():
        # Pre-revoke any open invites so dangling links can't be used to
        # "re-enter" the now-deleted workspace.
        inv.revoked_at = _utcnow().isoformat()
        session.add(inv)
    for m in session.exec(select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id)).all():
        session.delete(m)
    ws = session.get(Workspace, workspace_id)
    if ws:
        session.delete(ws)

# ── Auto-publish helper (now workspace-scoped) ───────────────────
def auto_publish_past_scheduled(session: Session, workspace_id: int) -> int:
    ws = session.get(Workspace, workspace_id)
    today_iso = _workspace_today_iso(ws) if ws else _date.today().isoformat()
    scheduled = session.exec(
        select(Entry).where((Entry.status == "scheduled") & (Entry.workspace_id == workspace_id))
    ).all()
    updated = 0
    for e in scheduled:
        d = (e.date or "").strip()
        if d and d <= today_iso:
            old_status = e.status
            e.status = "published"
            session.add(e)
            # Emit a system-actor activity event so the feed reflects auto
            # transitions instead of silently flipping under live users.
            session.add(ActivityEvent(
                entry_id=e.id, workspace_id=workspace_id, user_id=0,
                event_type="status_changed", from_value=old_status, to_value="published",
            ))
            updated += 1
    if updated:
        session.commit()
    return updated

# ────────────────────────────────────────────────────────────────────
# AUTH ROUTES
# ────────────────────────────────────────────────────────────────────
@app.get("/api/auth/setup-required")
def setup_required():
    """Frontend calls this before showing any auth screen. If no users exist
    yet, the frontend shows the first-run setup wizard instead of /login."""
    with Session(engine) as session:
        has_users = session.exec(select(User).limit(1)).first() is not None
        return {"setup_required": not has_users, "open_signup": ALLOW_OPEN_SIGNUP}

@app.post("/api/auth/setup")
def do_setup(payload: SetupIn, response: Response, request: Request):
    """One-time first-run setup. Creates the admin user, their workspace, and
    adopts any pre-existing workspace_id=0 data (legacy channels/entries/goal)
    into the new workspace. Refuses to run if any users already exist."""
    if not _check_rate(f"setup:{_client_ip(request)}", max_hits=SETUP_MAX_PER_IP, window_seconds=LOGIN_WINDOW_SECONDS):
        raise _too_many()
    if not secrets.compare_digest((payload.setup_code or "").strip().upper(), SETUP_CODE):
        raise HTTPException(
            status_code=403,
            detail="Invalid setup code. Find it in the server logs (e.g. docker logs contentcal).",
        )
    username = _validate_username(payload.username)
    if not payload.password:
        raise HTTPException(status_code=400, detail="Username and password are required.")
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    with Session(engine) as session:
        # Serialize the "first user wins" check against concurrent setup posts.
        try:
            session.exec(text("BEGIN IMMEDIATE"))
        except Exception:
            pass
        if session.exec(select(User).limit(1)).first():
            session.rollback()
            raise HTTPException(status_code=400, detail="Setup has already been completed.")
        user = User(username=username, password_hash=hash_password(payload.password))
        session.add(user)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise HTTPException(status_code=400, detail="Username is already taken.")
        session.refresh(user)
        ws_name = _validate_name(payload.workspace_name or f"{username}'s Workspace", field="Workspace name")
        ws = Workspace(name=ws_name, created_by_user_id=user.id)
        session.add(ws)
        session.commit()
        session.refresh(ws)
        session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="owner"))
        user.default_workspace_id = ws.id
        session.add(user)
        # Adopt legacy data (workspace_id=0 → new ws.id)
        for table in (Channel, Entry, Goal):
            for row in session.exec(select(table).where(table.workspace_id == 0)).all():
                row.workspace_id = ws.id
                session.add(row)
        session.commit()
        _set_session_cookie(response, user, ws.id)
        _set_device_cookie(response, user.username)
        return {"user": user_dict(user), "workspace": workspace_dict(ws)}

@app.post("/api/auth/signup")
def signup(payload: SignupIn, response: Response, request: Request):
    """Creates a user with their own personal workspace. If an invite code is
    supplied, the user is also added to the invited workspace and that becomes
    their default. Unless ALLOW_OPEN_SIGNUP is set, a valid invite is required:
    the account is only created if the invite can be claimed."""
    if not _check_rate(f"signup:{_client_ip(request)}", max_hits=SIGNUP_MAX_PER_IP, window_seconds=3600):
        raise _too_many("Too many sign-ups from this address. Try again later.")
    if not ALLOW_OPEN_SIGNUP and not payload.invite_code:
        raise HTTPException(
            status_code=403,
            detail="Sign-up is invite-only. Ask a workspace owner for an invite link.",
        )
    username = _validate_username(payload.username)
    if not payload.password:
        raise HTTPException(status_code=400, detail="Username and password are required.")
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    with Session(engine) as session:
        # In invite-only mode, check the invite before anything that depends on
        # the username, so a bogus invite can't be used to probe which
        # usernames exist (every bad invite gets the same 410). The atomic
        # claim below still decides races.
        if not ALLOW_OPEN_SIGNUP and not _invite_is_usable(session, payload.invite_code):
            raise HTTPException(status_code=410, detail="That invite link is no longer valid.")
        # Usernames are unique ignoring case, so nobody can register a
        # look-alike ("Alice" next to "alice"). Login itself stays exact-match.
        if session.exec(select(User).where(func.lower(User.username) == username.lower())).first():
            raise HTTPException(status_code=400, detail="Username is already taken.")
        # Everything below is one transaction (flush, not commit) so an invite
        # that can't be claimed in invite-only mode leaves no account behind.
        user = User(username=username, password_hash=hash_password(payload.password))
        session.add(user)
        try:
            session.flush()
        except IntegrityError:
            # Raced with another signup with the same username.
            session.rollback()
            raise HTTPException(status_code=400, detail="Username is already taken.")
        # Personal workspace — user is the owner.
        personal = Workspace(name=f"{username}'s Workspace", created_by_user_id=user.id)
        session.add(personal)
        session.flush()
        session.add(WorkspaceMember(workspace_id=personal.id, user_id=user.id, role="owner"))
        for i, ch in enumerate(DEFAULT_CHANNELS):
            session.add(Channel(workspace_id=personal.id, position=i, name=ch["name"], pillars=ch["pillars"]))
        user.default_workspace_id = personal.id
        active_ws_id = personal.id
        # Honor invite code if present — atomic claim via conditional UPDATE so
        # two simultaneous accepts can't both succeed.
        if payload.invite_code:
            now_iso = _utcnow().isoformat()
            result = session.exec(text(
                "UPDATE workspaceinvite SET used_by_user_id=:uid, used_at=:ts "
                "WHERE code=:code AND used_by_user_id IS NULL AND revoked_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > :ts)"
            ), params={"uid": user.id, "ts": now_iso, "code": payload.invite_code})
            if getattr(result, "rowcount", 0) == 1:
                invite = session.exec(select(WorkspaceInvite).where(WorkspaceInvite.code == payload.invite_code)).first()
                if invite:
                    session.add(WorkspaceMember(workspace_id=invite.workspace_id, user_id=user.id, role="member"))
                    user.default_workspace_id = invite.workspace_id
                    active_ws_id = invite.workspace_id
            elif not ALLOW_OPEN_SIGNUP:
                session.rollback()
                raise HTTPException(status_code=410, detail="That invite link is no longer valid.")
        session.add(user)
        session.commit()
        _set_session_cookie(response, user, active_ws_id)
        _set_device_cookie(response, user.username)
        return {"user": user_dict(user)}

def _invite_is_usable(session: Session, code: Optional[str]) -> bool:
    if not code:
        return False
    inv = session.exec(select(WorkspaceInvite).where(WorkspaceInvite.code == code)).first()
    return bool(
        inv and inv.used_by_user_id is None and inv.revoked_at is None
        and (inv.expires_at is None or inv.expires_at > _utcnow().isoformat())
    )

def _login_failure_limits(request: Request, username: str) -> list:
    """(key, max failures) pairs that apply to this login attempt. Keys use the
    exact username, the same way the account is looked up, so failures against
    "ALICE" never count against "alice"."""
    device = _read_device(request)
    if device and device["u"] == username:
        # A browser that signed in to this account before: its own bucket, so
        # strangers' failures can't lock it out.
        return [(f"login-dev:{device['d']}", LOGIN_MAX_FAILS_PER_DEVICE)]
    return [
        (f"login-acct-ip:{username}|{_client_ip(request)}", LOGIN_MAX_FAILS_PER_ACCOUNT_IP),
        (f"login-acct:{username}", LOGIN_MAX_FAILS_PER_ACCOUNT),
    ]

@app.post("/api/auth/login")
def login(payload: LoginIn, response: Response, request: Request):
    username = (payload.username or "").strip()
    if not _check_rate(f"login-ip:{_client_ip(request)}", max_hits=LOGIN_MAX_PER_IP, window_seconds=LOGIN_WINDOW_SECONDS):
        raise _too_many()
    limits = _login_failure_limits(request, username)
    for key, cap in limits:
        if not _check_rate(key, max_hits=cap, window_seconds=LOGIN_WINDOW_SECONDS, record=False):
            raise _too_many()
    with Session(engine) as session:
        user = session.exec(select(User).where(User.username == username)).first()
        if not verify_password(payload.password, user.password_hash if user else _DUMMY_HASH) or not user:
            # Only failures count toward the per-account caps.
            for key, cap in limits:
                _check_rate(key, max_hits=cap, window_seconds=LOGIN_WINDOW_SECONDS)
            raise HTTPException(status_code=401, detail="Invalid username or password.")
        # Pick active workspace = default, falling back to first membership
        active_ws = user.default_workspace_id
        if not active_ws or not _is_member(session, active_ws, user.id):
            first = session.exec(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).first()
            if first:
                active_ws = first.workspace_id
        if not active_ws:
            raise HTTPException(status_code=500, detail="No workspace available for this account. Contact an admin.")
        _set_session_cookie(response, user, active_ws)
        _set_device_cookie(response, user.username)
        return {"user": user_dict(user), "active_workspace_id": active_ws}

@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    """Revokes every session cookie for this account (all devices), not just
    the one in this browser, so a copied cookie stops working too."""
    sess = _read_session(request)
    if sess:
        with Session(engine) as session:
            u = _session_user(session, sess)
            if u:
                u.session_version = (u.session_version or 0) + 1
                session.add(u)
                session.commit()
    _clear_session_cookie(response)
    return {"ok": True}

@app.get("/api/me")
def me(request: Request, user: User = Depends(current_user)):
    with Session(engine) as session:
        wss = _user_workspaces(session, user.id)
        roles = {
            m.workspace_id: m.role
            for m in session.exec(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()
        }
        sess = _read_session(request) or {}
        return {
            "user": user_dict(user),
            "workspaces": [{**workspace_dict(w), "role": roles.get(w.id, "member")} for w in wss],
            "active_workspace_id": sess.get("wsid") or user.default_workspace_id,
        }

@app.post("/api/me/password")
def change_password(payload: PasswordChangeIn, request: Request, response: Response, user: User = Depends(current_user)):
    """Changes the password (the current one is required) and signs the
    account out everywhere else: session_version is bumped and only this
    browser gets a fresh cookie."""
    fail_key = f"pwchange:{user.id}"
    if not _check_rate(fail_key, max_hits=LOGIN_MAX_FAILS_PER_DEVICE, window_seconds=LOGIN_WINDOW_SECONDS, record=False):
        raise _too_many()
    if len(payload.new_password or "") < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters.")
    with Session(engine) as session:
        u = session.get(User, user.id)
        if not u or not verify_password(payload.current_password or "", u.password_hash):
            _check_rate(fail_key, max_hits=LOGIN_MAX_FAILS_PER_DEVICE, window_seconds=LOGIN_WINDOW_SECONDS)
            raise HTTPException(status_code=403, detail="Current password is incorrect.")
        u.password_hash = hash_password(payload.new_password)
        u.session_version = (u.session_version or 0) + 1
        session.add(u)
        session.commit()
        session.refresh(u)
        sess = _read_session(request) or {}
        _set_session_cookie(response, u, sess.get("wsid") or u.default_workspace_id)
        return {"ok": True}

@app.put("/api/me/active-workspace")
def switch_workspace(payload: SwitchWorkspaceIn, response: Response, request: Request, user: User = Depends(current_user)):
    with Session(engine) as session:
        if not _is_member(session, payload.workspace_id, user.id):
            raise HTTPException(status_code=403, detail="You're not a member of that workspace.")
        # Persist the switch as the new default so the next login lands the
        # user in the same workspace they actively chose, instead of bouncing
        # them back to the historical default.
        u = session.get(User, user.id)
        if u:
            u.default_workspace_id = payload.workspace_id
            session.add(u)
            session.commit()
        _set_session_cookie(response, user, payload.workspace_id)
        return {"ok": True, "active_workspace_id": payload.workspace_id}

# ────────────────────────────────────────────────────────────────────
# WORKSPACE ROUTES
# ────────────────────────────────────────────────────────────────────
@app.post("/api/workspaces")
def create_workspace(payload: WorkspaceIn, response: Response, user: User = Depends(current_user)):
    name = _validate_name(payload.name, field="Workspace name")
    with Session(engine) as session:
        ws = Workspace(name=name, created_by_user_id=user.id)
        session.add(ws)
        session.commit()
        session.refresh(ws)
        session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="owner"))
        # Seed defaults so the new workspace isn't empty
        for i, ch in enumerate(DEFAULT_CHANNELS):
            session.add(Channel(workspace_id=ws.id, position=i, name=ch["name"], pillars=ch["pillars"]))
        session.commit()
        # Switch the user into the new workspace immediately
        _set_session_cookie(response, user, ws.id)
        return {**workspace_dict(ws), "role": "owner"}

@app.put("/api/workspaces/{workspace_id}")
def rename_workspace(workspace_id: int, payload: WorkspaceIn, user: User = Depends(current_user)):
    name = _validate_name(payload.name, field="Workspace name")
    with Session(engine) as session:
        _require_owner(session, workspace_id, user.id)
        ws = session.get(Workspace, workspace_id)
        if not ws:
            raise HTTPException(status_code=404, detail="Workspace not found.")
        ws.name = name
        session.add(ws)
        session.commit()
        session.refresh(ws)
        return workspace_dict(ws)

@app.patch("/api/workspaces/{workspace_id}/settings")
def update_workspace_settings(workspace_id: int, payload: WorkspaceSettingsIn, user: User = Depends(current_user)):
    """Owner-only. Currently only changes the IANA timezone, which controls
    when scheduled entries auto-publish (so users in JST don't get rows
    flipped 9 hours early just because the container clock is UTC)."""
    with Session(engine) as session:
        _require_owner(session, workspace_id, user.id)
        ws = session.get(Workspace, workspace_id)
        if not ws:
            raise HTTPException(status_code=404, detail="Workspace not found.")
        if payload.timezone is not None:
            tz = (payload.timezone or "").strip()
            if tz:
                if not _HAS_ZONEINFO:
                    raise HTTPException(status_code=400, detail="Server has no IANA timezone database installed.")
                try:
                    ZoneInfo(tz)
                except Exception:
                    raise HTTPException(status_code=400, detail=f"Unknown timezone '{tz}'.")
            ws.timezone = tz
            session.add(ws)
            session.commit()
            session.refresh(ws)
        return workspace_dict(ws)

@app.get("/api/timezones")
def list_timezones():
    """Cheap lookup for the workspace-settings dropdown. Returns the sorted
    IANA names available on this host (or empty if zoneinfo isn't bundled)."""
    if not _HAS_ZONEINFO:
        return {"timezones": []}
    return {"timezones": sorted(available_timezones())}

@app.get("/api/workspaces/{workspace_id}/members")
def list_members(workspace_id: int, user: User = Depends(current_user)):
    with Session(engine) as session:
        if not _is_member(session, workspace_id, user.id):
            raise HTTPException(status_code=403, detail="Not a member of this workspace.")
        rows = session.exec(select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id)).all()
        out = []
        for r in rows:
            u = session.get(User, r.user_id)
            if u:
                out.append({
                    "user_id": u.id,
                    "username": u.username,
                    "joined_at": r.joined_at,
                    "role": r.role,
                    "is_you": u.id == user.id,
                })
        return out

@app.delete("/api/workspaces/{workspace_id}/members/{member_user_id}")
def remove_member(workspace_id: int, member_user_id: int, response: Response, request: Request, user: User = Depends(current_user)):
    """Self-leave is always allowed. Removing another member requires owner
    role. Multiple owners may coexist; removing an owner is allowed only if
    at least one other owner remains (so the workspace is never ownerless
    while other members are still there). If the leaver was the *sole*
    owner, ownership auto-passes to the longest-tenured remaining member.
    If the last member leaves, the workspace and all its data are deleted."""
    with Session(engine) as session:
        if not _is_member(session, workspace_id, user.id):
            raise HTTPException(status_code=403, detail="Not a member of this workspace.")
        is_self = (member_user_id == user.id)
        if not is_self:
            _require_owner(session, workspace_id, user.id)
        target = session.exec(
            select(WorkspaceMember).where(
                (WorkspaceMember.workspace_id == workspace_id) & (WorkspaceMember.user_id == member_user_id)
            )
        ).first()
        if not target:
            raise HTTPException(status_code=404, detail="Member not found.")
        was_owner = (target.role == "owner")
        # Guard against removing an owner when other co-owners are absent
        # AND other members remain — that would orphan the workspace. The
        # sole-owner-leaving-an-otherwise-empty-workspace case (delete
        # cascade below) is still allowed.
        if was_owner:
            owner_count = _count_owners(session, workspace_id)
            member_count = session.exec(
                select(func.count(WorkspaceMember.id)).where(WorkspaceMember.workspace_id == workspace_id)
            ).one()
            if owner_count <= 1 and member_count > 1:
                if not is_self:
                    raise HTTPException(status_code=400, detail="Promote another owner before removing the sole owner.")
                # Self-leave by sole owner with co-members → auto-promote
                # below (heir takes over). No-op here.
        session.delete(target)
        session.commit()
        # If the leaver was the sole owner and there are still members, promote
        # the longest-tenured one. (Skipped when other owners already exist.)
        if was_owner:
            remaining_owners = _count_owners(session, workspace_id)
            remaining_members = session.exec(
                select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id).order_by(WorkspaceMember.joined_at)
            ).all()
            if remaining_members and remaining_owners == 0:
                heir = remaining_members[0]
                heir.role = "owner"
                session.add(heir)
                session.commit()
            elif not remaining_members:
                _delete_workspace_cascade(session, workspace_id)
                session.commit()
        # If the leaver was the active user, switch their session to another workspace.
        if is_self:
            remaining = session.exec(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).first()
            if remaining:
                user.default_workspace_id = remaining.workspace_id
                session.add(user)
                session.commit()
                _set_session_cookie(response, user, remaining.workspace_id)
            else:
                _clear_session_cookie(response)
        return {"ok": True}

@app.put("/api/workspaces/{workspace_id}/members/{member_user_id}/role")
def set_member_role(workspace_id: int, member_user_id: int, payload: MemberRoleIn, user: User = Depends(current_user)):
    """Promote a member to owner or demote an owner back to member. Only
    existing owners can change roles. Co-owners are allowed; the only
    invariant enforced is that at least one owner always exists, so the
    last owner can't be demoted (they must promote a successor first).
    Self-demotion is allowed under the same constraint."""
    new_role = (payload.role or "").strip().lower()
    if new_role not in ("owner", "member"):
        raise HTTPException(status_code=400, detail="Role must be 'owner' or 'member'.")
    with Session(engine) as session:
        _require_owner(session, workspace_id, user.id)
        target = session.exec(
            select(WorkspaceMember).where(
                (WorkspaceMember.workspace_id == workspace_id) & (WorkspaceMember.user_id == member_user_id)
            )
        ).first()
        if not target:
            raise HTTPException(status_code=404, detail="Member not found.")
        if target.role == new_role:
            return {"ok": True, "role": target.role}
        if target.role == "owner" and new_role == "member":
            # Demotion — refuse if this would leave the workspace ownerless.
            if _count_owners(session, workspace_id) <= 1:
                raise HTTPException(
                    status_code=400,
                    detail="Promote another owner before demoting the last owner.",
                )
        target.role = new_role
        session.add(target)
        session.commit()
        session.refresh(target)
        return {"ok": True, "role": target.role}

# ────────────────────────────────────────────────────────────────────
# INVITE ROUTES
# ────────────────────────────────────────────────────────────────────
@app.post("/api/workspaces/{workspace_id}/invites")
def create_invite(workspace_id: int, payload: InviteIn, user: User = Depends(current_user)):
    with Session(engine) as session:
        _require_owner(session, workspace_id, user.id)
        expires_at = None
        if payload.expires_in_days and payload.expires_in_days > 0:
            expires_at = (_utcnow() + timedelta(days=payload.expires_in_days)).isoformat()
        invite = WorkspaceInvite(
            workspace_id=workspace_id,
            code=secrets.token_urlsafe(12),
            created_by_user_id=user.id,
            expires_at=expires_at,
        )
        session.add(invite)
        session.commit()
        session.refresh(invite)
        ws = session.get(Workspace, workspace_id)
        return invite_dict(invite, ws.name if ws else "")

@app.get("/api/workspaces/{workspace_id}/invites")
def list_invites(workspace_id: int, user: User = Depends(current_user)):
    with Session(engine) as session:
        if not _is_member(session, workspace_id, user.id):
            raise HTTPException(status_code=403, detail="Not a member of this workspace.")
        # Invites are owner-only: members get an empty list (not a 403, so the
        # settings panel still loads for them) and can't pass links around.
        if not _is_owner(session, workspace_id, user.id):
            return []
        rows = session.exec(select(WorkspaceInvite).where(WorkspaceInvite.workspace_id == workspace_id)).all()
        ws = session.get(Workspace, workspace_id)
        ws_name = ws.name if ws else ""
        # Hide already-used or revoked entries from the active list
        return [invite_dict(i, ws_name) for i in rows if not i.used_by_user_id and not i.revoked_at]

@app.delete("/api/invites/{code}")
def revoke_invite(code: str, user: User = Depends(current_user)):
    with Session(engine) as session:
        inv = session.exec(select(WorkspaceInvite).where(WorkspaceInvite.code == code)).first()
        if not inv:
            raise HTTPException(status_code=404, detail="Invite not found.")
        _require_owner(session, inv.workspace_id, user.id)
        inv.revoked_at = _utcnow().isoformat()
        session.add(inv)
        session.commit()
        return {"ok": True}

@app.get("/api/invites/{code}")
def preview_invite(code: str, request: Request):
    """Public — lets the signup/join page show which workspace the invite
    belongs to before the user commits. Does NOT consume the invite.
    Rate-limited per IP to make code enumeration infeasible."""
    if not _check_rate(f"invite:{_client_ip(request)}", max_hits=INVITE_PREVIEW_MAX_PER_IP, window_seconds=60):
        raise HTTPException(status_code=429, detail="Too many invite lookups. Try again in a minute.")
    with Session(engine) as session:
        inv = session.exec(select(WorkspaceInvite).where(WorkspaceInvite.code == code)).first()
        if not inv:
            raise HTTPException(status_code=404, detail="Invite not found.")
        if inv.revoked_at:
            raise HTTPException(status_code=410, detail="This invite has been revoked.")
        if inv.used_by_user_id:
            raise HTTPException(status_code=410, detail="This invite has already been used.")
        if inv.expires_at and inv.expires_at < _utcnow().isoformat():
            raise HTTPException(status_code=410, detail="This invite has expired.")
        ws = session.get(Workspace, inv.workspace_id)
        return {"workspace_name": ws.name if ws else "(unknown)", "workspace_id": inv.workspace_id}

@app.post("/api/invites/{code}/accept")
def accept_invite(code: str, response: Response, user: User = Depends(current_user)):
    """Already-logged-in user accepts an invite, joining the workspace and
    switching to it. The claim is done via a conditional UPDATE so two
    concurrent accepts can't both succeed."""
    with Session(engine) as session:
        inv = session.exec(select(WorkspaceInvite).where(WorkspaceInvite.code == code)).first()
        if not inv:
            raise HTTPException(status_code=410, detail="Invite not valid.")
        if _is_member(session, inv.workspace_id, user.id):
            raise HTTPException(status_code=400, detail="You're already a member of this workspace.")
        now_iso = _utcnow().isoformat()
        result = session.exec(text(
            "UPDATE workspaceinvite SET used_by_user_id=:uid, used_at=:ts "
            "WHERE code=:code AND used_by_user_id IS NULL AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > :ts)"
        ), params={"uid": user.id, "ts": now_iso, "code": code})
        if getattr(result, "rowcount", 0) != 1:
            session.rollback()
            raise HTTPException(status_code=410, detail="Invite not valid.")
        session.add(WorkspaceMember(workspace_id=inv.workspace_id, user_id=user.id, role="member"))
        session.commit()
        _set_session_cookie(response, user, inv.workspace_id)
        ws = session.get(Workspace, inv.workspace_id)
        return {"ok": True, "workspace": workspace_dict(ws) if ws else None}

# ────────────────────────────────────────────────────────────────────
# DATA ROUTES (all scoped to the current workspace)
# ────────────────────────────────────────────────────────────────────
DATE_REQUIRED_STATUSES = {"scheduled", "published"}

def validate_date_for_status(date: Optional[str], status: str) -> None:
    if status in DATE_REQUIRED_STATUSES and not (date or "").strip():
        raise HTTPException(
            status_code=400,
            detail=f"A date is required when status is '{status}'.",
        )

def _validate_entry_payload(session: Session, ws_id: int, entry: "EntryIn") -> dict:
    """Centralised normalization for POST/PUT /api/entries. Whitelists
    status/format, bounds text fields, validates the date is ISO if present,
    and confirms `channel` is a real index in this workspace."""
    title = _validate_name(entry.title, field="Title", max_len=300)
    status_v = _validate_status(entry.status)
    format_v = _validate_format(entry.format)
    pillar = _validate_text(entry.pillar or "", field="Pillar", max_len=MAX_NAME_LEN)
    notes = _validate_text(entry.notes or "", field="Notes", max_len=MAX_NOTES_LEN)
    link = _validate_text(entry.link or "", field="Link", max_len=2000)
    date_v = (entry.date or "").strip()
    if date_v:
        try:
            _date.fromisoformat(date_v)
        except ValueError:
            raise HTTPException(status_code=400, detail="Date must be ISO YYYY-MM-DD.")
    validate_date_for_status(date_v, status_v)
    if not isinstance(entry.channel, int) or entry.channel < 0:
        raise HTTPException(status_code=400, detail="Channel index is out of range for this workspace.")
    ch_row = session.exec(
        select(Channel).where(
            (Channel.workspace_id == ws_id) & (Channel.position == entry.channel)
        )
    ).first()
    if not ch_row:
        raise HTTPException(status_code=400, detail="Channel index is out of range for this workspace.")
    if ch_row.archived:
        raise HTTPException(status_code=400, detail="Cannot assign an entry to an archived channel.")
    return {
        "title": title, "channel": entry.channel, "date": date_v, "status": status_v,
        "pillar": pillar, "notes": notes, "format": format_v, "link": link,
    }

@app.get("/api/data")
def get_data(ws: Workspace = Depends(current_workspace), user: User = Depends(current_user)):
    with Session(engine) as session:
        auto_publish_past_scheduled(session, ws.id)
        channels = session.exec(
            select(Channel).where(Channel.workspace_id == ws.id).order_by(Channel.position)
        ).all()
        entries = session.exec(
            select(Entry).where(Entry.workspace_id == ws.id).order_by(Entry.id)
        ).all()
        goal = session.exec(
            select(Goal).where(Goal.workspace_id == ws.id).order_by(Goal.id)
        ).first()
        # Comment counts per entry — one grouped query so cards can show the
        # 💬 N badge without per-entry round trips.
        count_rows = session.exec(
            select(Comment.entry_id, func.count(Comment.id))
            .where(Comment.workspace_id == ws.id)
            .group_by(Comment.entry_id)
        ).all()
        counts = {row[0]: row[1] for row in count_rows}
        entry_payloads = []
        for e in entries:
            d = entry_dict(e)
            d["comment_count"] = counts.get(e.id, 0)
            entry_payloads.append(d)
        return {
            "channels": [channel_dict(c) for c in channels],
            "entries": entry_payloads,
            "goal": goal_dict(goal),
            "channels_version": ws.channels_version,
            "workspace_id": ws.id,
            # Lets open tabs notice role changes made by another owner.
            "role": "owner" if _is_owner(session, ws.id, user.id) else "member",
        }

@app.put("/api/goal")
def update_goal(goal_in: GoalIn, ws: Workspace = Depends(owner_workspace)):
    name = _validate_text(goal_in.name or "", field="Goal name", max_len=MAX_NAME_LEN)
    deadline = (goal_in.deadline or "").strip()
    if deadline:
        try:
            _date.fromisoformat(deadline)
        except ValueError:
            raise HTTPException(status_code=400, detail="Deadline must be ISO YYYY-MM-DD.")
    target = int(goal_in.target or 0)
    if target < 0:
        raise HTTPException(status_code=400, detail="Target must be non-negative.")
    with Session(engine) as session:
        existing = session.exec(select(Goal).where(Goal.workspace_id == ws.id)).first()
        if existing:
            existing.name = name
            existing.target = target
            existing.deadline = deadline
            session.add(existing)
        else:
            existing = Goal(workspace_id=ws.id, name=name, target=target, deadline=deadline)
            session.add(existing)
        session.commit()
        session.refresh(existing)
        return goal_dict(existing)

@app.post("/api/entries")
def add_entry(entry: EntryIn, ws: Workspace = Depends(current_workspace)):
    with Session(engine) as session:
        v = _validate_entry_payload(session, ws.id, entry)
        new_e = Entry(workspace_id=ws.id, **v)
        session.add(new_e)
        session.commit()
        session.refresh(new_e)
        return entry_dict(new_e)

@app.post("/api/entries/bulk")
def add_entries_bulk(rows: list = Depends(bulk_entries_body), ws: Workspace = Depends(current_workspace)):
    """Atomic-per-row bulk insert used by the CSV import. Returns a
    per-row result list so the client can surface exactly which rows failed
    and why, instead of swallowing failures one HTTP call at a time. Rows are
    validated one at a time (the array length was checked first), and each
    insert runs in its own SAVEPOINT so a failing row never undoes earlier
    ones."""
    created = []
    errors = []
    with Session(engine) as session:
        for i, row in enumerate(rows):
            try:
                entry = EntryIn.model_validate(row)
                v = _validate_entry_payload(session, ws.id, entry)
            except ValidationError as exc:
                first = exc.errors()[0] if exc.errors() else {}
                field = ".".join(str(x) for x in first.get("loc", ())) or "row"
                errors.append({"index": i, "detail": f"{field}: {first.get('msg', 'invalid')}"})
                continue
            except HTTPException as exc:
                errors.append({"index": i, "detail": str(exc.detail)})
                continue
            new_e = Entry(workspace_id=ws.id, **v)
            try:
                with session.begin_nested():
                    session.add(new_e)
            except Exception as exc:
                # Log the details server-side; don't echo DB internals to clients.
                print(f"[bulk-import] row {i} failed: {exc!r}")
                errors.append({"index": i, "detail": "Could not save this row."})
                continue
            created.append(entry_dict(new_e))
        session.commit()
    return {"created": created, "errors": errors}

@app.put("/api/entries/{entry_id}")
def update_entry(entry_id: int, entry: EntryIn, ws: Workspace = Depends(current_workspace), user: User = Depends(current_user)):
    with Session(engine) as session:
        v = _validate_entry_payload(session, ws.id, entry)
        existing = session.get(Entry, entry_id)
        if not existing or existing.workspace_id != ws.id:
            raise HTTPException(status_code=404, detail="Entry not found")
        # Capture the pre-edit values so we can emit activity events for
        # status/date transitions only. Auto-publish (run elsewhere) also
        # triggers this code path, but here the actor is the live user.
        old_status = existing.status
        old_date = existing.date or ""
        new_date = v["date"]
        existing.title = v["title"]
        existing.channel = v["channel"]
        existing.date = new_date
        existing.status = v["status"]
        existing.pillar = v["pillar"]
        existing.notes = v["notes"]
        existing.format = v["format"]
        existing.link = v["link"]
        session.add(existing)
        if old_status != v["status"]:
            session.add(ActivityEvent(
                entry_id=entry_id, workspace_id=ws.id, user_id=user.id,
                event_type="status_changed", from_value=old_status, to_value=v["status"],
            ))
        if old_date != new_date:
            session.add(ActivityEvent(
                entry_id=entry_id, workspace_id=ws.id, user_id=user.id,
                event_type="date_changed", from_value=old_date, to_value=new_date,
            ))
        session.commit()
        session.refresh(existing)
        # Preserve the current comment_count on the response so the frontend
        # doesn't blow away the badge after editing.
        d = entry_dict(existing)
        d["comment_count"] = session.exec(
            select(func.count(Comment.id)).where(Comment.entry_id == entry_id)
        ).one()
        return d

@app.delete("/api/entries/{entry_id}")
def delete_entry(entry_id: int, ws: Workspace = Depends(current_workspace)):
    with Session(engine) as session:
        existing = session.get(Entry, entry_id)
        if not existing or existing.workspace_id != ws.id:
            raise HTTPException(status_code=404, detail="Entry not found")
        # Cascade: drop all comments and activity events for this entry too.
        for c in session.exec(select(Comment).where(Comment.entry_id == entry_id)).all():
            session.delete(c)
        for a in session.exec(select(ActivityEvent).where(ActivityEvent.entry_id == entry_id)).all():
            session.delete(a)
        session.delete(existing)
        session.commit()
        return {"ok": True}

# ────────────────────────────────────────────────────────────────────
# DISCUSSION (comments + activity feed)
# ────────────────────────────────────────────────────────────────────
def _username_map(session: Session, user_ids: list[int]) -> dict:
    """Batch-resolve user_id → username for feed rendering."""
    ids = list({uid for uid in user_ids if uid})
    if not ids:
        return {}
    rows = session.exec(select(User).where(User.id.in_(ids))).all()
    return {u.id: u.username for u in rows}

@app.get("/api/entries/{entry_id}/feed")
def get_entry_feed(entry_id: int, ws: Workspace = Depends(current_workspace)):
    """Merged chronological feed of comments + activity events, newest first."""
    with Session(engine) as session:
        e = session.get(Entry, entry_id)
        if not e or e.workspace_id != ws.id:
            raise HTTPException(status_code=404, detail="Entry not found")
        comments = session.exec(select(Comment).where(Comment.entry_id == entry_id)).all()
        events = session.exec(select(ActivityEvent).where(ActivityEvent.entry_id == entry_id)).all()
        names = _username_map(session, [c.user_id for c in comments] + [a.user_id for a in events])
        items = []
        for c in comments:
            items.append(comment_dict(c, names.get(c.user_id, "")))
        for a in events:
            items.append(activity_dict(a, names.get(a.user_id, "")))
        # Sort newest first by created_at (ISO 8601 sorts lexicographically)
        items.sort(key=lambda x: x["created_at"], reverse=True)
        return items

@app.post("/api/entries/{entry_id}/comments")
def create_comment(entry_id: int, payload: CommentIn, ws: Workspace = Depends(current_workspace), user: User = Depends(current_user)):
    body = (payload.body or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="Comment cannot be empty.")
    if len(body) > COMMENT_MAX_LEN:
        raise HTTPException(status_code=400, detail=f"Comment exceeds {COMMENT_MAX_LEN} characters.")
    with Session(engine) as session:
        e = session.get(Entry, entry_id)
        if not e or e.workspace_id != ws.id:
            raise HTTPException(status_code=404, detail="Entry not found")
        c = Comment(entry_id=entry_id, workspace_id=ws.id, user_id=user.id, body=body)
        session.add(c)
        session.commit()
        session.refresh(c)
        return comment_dict(c, user.username)

@app.put("/api/comments/{comment_id}")
def edit_comment(comment_id: int, payload: CommentIn, ws: Workspace = Depends(current_workspace), user: User = Depends(current_user)):
    body = (payload.body or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="Comment cannot be empty.")
    if len(body) > COMMENT_MAX_LEN:
        raise HTTPException(status_code=400, detail=f"Comment exceeds {COMMENT_MAX_LEN} characters.")
    with Session(engine) as session:
        c = session.get(Comment, comment_id)
        if not c or c.workspace_id != ws.id:
            raise HTTPException(status_code=404, detail="Comment not found.")
        if c.user_id != user.id:
            raise HTTPException(status_code=403, detail="You can only edit your own comments.")
        c.body = body
        c.edited_at = _utcnow().isoformat()
        session.add(c)
        session.commit()
        session.refresh(c)
        return comment_dict(c, user.username)

@app.delete("/api/comments/{comment_id}")
def delete_comment(comment_id: int, ws: Workspace = Depends(current_workspace), user: User = Depends(current_user)):
    with Session(engine) as session:
        c = session.get(Comment, comment_id)
        if not c or c.workspace_id != ws.id:
            raise HTTPException(status_code=404, detail="Comment not found.")
        # Authors can delete their own comments; workspace owners can moderate.
        if c.user_id != user.id and not _is_owner(session, ws.id, user.id):
            raise HTTPException(status_code=403, detail="You can only delete your own comments.")
        session.delete(c)
        session.commit()
        return {"ok": True}

@app.put("/api/channels")
def update_channels(request: Request, raw_channels: list = Depends(channels_body), ws: Workspace = Depends(owner_workspace)):
    if not raw_channels:
        raise HTTPException(status_code=400, detail="At least one channel is required.")
    try:
        channels_in = [ChannelIn.model_validate(c) for c in raw_channels]
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_validation_errors(exc.errors()))
    # Validate each channel before touching the DB.
    validated = []
    for ch_in in channels_in:
        validated.append({
            "name": _validate_name(ch_in.name, field="Channel name", max_len=80),
            "pillars": [_validate_name(p, field="Pillar", max_len=80) for p in (ch_in.pillars or [])[:20]],
            "color": _validate_color(ch_in.color),
            "icon": _validate_icon(ch_in.icon),
            "archived": bool(ch_in.archived),
        })
    if all(v["archived"] for v in validated):
        raise HTTPException(status_code=400, detail="At least one channel must remain unarchived.")
    # Optimistic-concurrency check: clients must echo the channels_version
    # they last saw (via header or query). Without it, a stale client can
    # silently clobber another user's reorder and corrupt Entry.channel
    # positional pointers. Missing header is allowed for back-compat with
    # the very first save but should be supplied by the new frontend.
    expected_raw = request.headers.get("X-Channels-Version")
    with Session(engine) as session:
        if expected_raw is not None:
            try:
                expected = int(expected_raw)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid X-Channels-Version header.")
            # Reload the workspace row inside this session to get the freshest version.
            ws_live = session.get(Workspace, ws.id)
            if not ws_live or ws_live.channels_version != expected:
                raise HTTPException(
                    status_code=409,
                    detail="Channels were modified by someone else. Reload to see the latest before saving.",
                )
        existing = session.exec(
            select(Channel).where(Channel.workspace_id == ws.id).order_by(Channel.position)
        ).all()
        for i, ch_v in enumerate(validated):
            if i < len(existing):
                row = existing[i]
                row.name = ch_v["name"]
                row.pillars = ch_v["pillars"]
                row.color = ch_v["color"]
                row.icon = ch_v["icon"]
                row.archived = ch_v["archived"]
                session.add(row)
            else:
                session.add(Channel(workspace_id=ws.id, position=i, **ch_v))
        for j in range(len(validated), len(existing)):
            # Shrinking the channel list also drops entries that pointed to the
            # removed positions (and their comments/activity).
            removed_idx = existing[j].position
            for e in session.exec(select(Entry).where((Entry.workspace_id == ws.id) & (Entry.channel == removed_idx))).all():
                for c in session.exec(select(Comment).where(Comment.entry_id == e.id)).all():
                    session.delete(c)
                for a in session.exec(select(ActivityEvent).where(ActivityEvent.entry_id == e.id)).all():
                    session.delete(a)
                session.delete(e)
            session.delete(existing[j])
        # Bump the version so other tabs/users hit 409 on stale saves.
        ws_live = session.get(Workspace, ws.id)
        if ws_live:
            ws_live.channels_version = (ws_live.channels_version or 0) + 1
            session.add(ws_live)
        session.commit()
        result = session.exec(
            select(Channel).where(Channel.workspace_id == ws.id).order_by(Channel.position)
        ).all()
        ws_live = session.get(Workspace, ws.id)
        return {
            "channels": [channel_dict(c) for c in result],
            "channels_version": ws_live.channels_version if ws_live else 0,
        }

@app.delete("/api/channels/{channel_index}")
def delete_channel(channel_index: int, ws: Workspace = Depends(owner_workspace)):
    with Session(engine) as session:
        channels = session.exec(
            select(Channel).where(Channel.workspace_id == ws.id).order_by(Channel.position)
        ).all()
        if channel_index < 0 or channel_index >= len(channels):
            raise HTTPException(status_code=404, detail="Channel not found")
        if len(channels) <= 1:
            raise HTTPException(status_code=400, detail="Cannot remove the last channel")

        session.delete(channels[channel_index])
        for c in channels[channel_index + 1:]:
            c.position -= 1
            session.add(c)
        entries = session.exec(select(Entry).where(Entry.workspace_id == ws.id)).all()
        for e in entries:
            if e.channel == channel_index:
                # Cascade comments + activity for entries that lose their channel.
                for cm in session.exec(select(Comment).where(Comment.entry_id == e.id)).all():
                    session.delete(cm)
                for ae in session.exec(select(ActivityEvent).where(ActivityEvent.entry_id == e.id)).all():
                    session.delete(ae)
                session.delete(e)
            elif e.channel > channel_index:
                e.channel -= 1
                session.add(e)
        # Bump channels_version so concurrent PUT /api/channels saves 409.
        ws_live = session.get(Workspace, ws.id)
        if ws_live:
            ws_live.channels_version = (ws_live.channels_version or 0) + 1
            session.add(ws_live)
        session.commit()

        new_channels = session.exec(
            select(Channel).where(Channel.workspace_id == ws.id).order_by(Channel.position)
        ).all()
        new_entries = session.exec(
            select(Entry).where(Entry.workspace_id == ws.id).order_by(Entry.id)
        ).all()
        ws_live = session.get(Workspace, ws.id)
        return {
            "channels": [channel_dict(c) for c in new_channels],
            "entries": [entry_dict(e) for e in new_entries],
            "channels_version": ws_live.channels_version if ws_live else 0,
        }

# Serve frontend (catch-all). Must be mounted LAST.
app.mount("/", StaticFiles(directory="/frontend", html=True), name="frontend")
