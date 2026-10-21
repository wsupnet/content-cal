from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlmodel import SQLModel, Field, Column, JSON, create_engine, Session, select
from sqlalchemy import text, func
from sqlalchemy.exc import IntegrityError
from typing import Optional, List
from datetime import date as _date, datetime, timedelta
import json, os, pathlib, re, secrets, time
from collections import deque
try:
    from zoneinfo import ZoneInfo, available_timezones  # py3.9+
    _HAS_ZONEINFO = True
except Exception:
    _HAS_ZONEINFO = False
    ZoneInfo = None  # type: ignore
    def available_timezones():
        return set()

import bcrypt as _bcrypt
from itsdangerous import URLSafeSerializer, BadSignature

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
    if not (s.startswith("data:image/") or s.startswith("https://")):
        raise HTTPException(status_code=400, detail="Channel icon must be a data:image/* or https:// URL.")
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
DATA_DIR = pathlib.Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = pathlib.Path(os.getenv("DB_FILE", str(DATA_DIR / "calendar.db")))
JSON_FILE = pathlib.Path(os.getenv("DATA_FILE", str(DATA_DIR / "calendar.json")))
SECRET_FILE = DATA_DIR / ".session_secret"

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

# Best-effort in-memory rate limiter. Used to throttle the public invite
# preview endpoint so a script can't enumerate codes. Keyed by client IP;
# resets on process restart, so behind a horizontal scaler put a real WAF
# in front for stronger guarantees.
_RATE_BUCKETS: dict = {}
def _check_rate(key: str, *, max_hits: int, window_seconds: int) -> bool:
    now = time.monotonic()
    bucket = _RATE_BUCKETS.get(key)
    if bucket is None:
        bucket = deque()
        _RATE_BUCKETS[key] = bucket
    cutoff = now - window_seconds
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= max_hits:
        return False
    bucket.append(now)
    # Coarse housekeeping so the dict doesn't grow unbounded.
    if len(_RATE_BUCKETS) > 10000:
        for k in list(_RATE_BUCKETS.keys())[:5000]:
            if not _RATE_BUCKETS[k] or _RATE_BUCKETS[k][-1] < cutoff:
                _RATE_BUCKETS.pop(k, None)
    return True

def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"

SESSION_SECRET = _load_or_create_secret()
SESSION_COOKIE = "contentcal_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

session_serializer = URLSafeSerializer(SESSION_SECRET, salt="session-v1")

# bcrypt has a hard 72-byte input limit. Truncate (UTF-8 byte-safe) so longer
# passphrases don't raise — common-enough that passlib also handles this.
def hash_password(pw: str) -> str:
    pw_bytes = pw.encode("utf-8")[:72]
    return _bcrypt.hashpw(pw_bytes, _bcrypt.gensalt()).decode("utf-8")

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
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

class Workspace(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    created_by_user_id: int = 0
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
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
    joined_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

class Comment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    entry_id: int = Field(index=True)
    workspace_id: int = Field(index=True)
    user_id: int = Field(index=True)
    body: str
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    edited_at: Optional[str] = None

class ActivityEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    entry_id: int = Field(index=True)
    workspace_id: int = Field(index=True)
    user_id: int = Field(index=True)  # actor who triggered the change
    event_type: str  # 'status_changed' | 'date_changed'
    from_value: str = ""  # raw value (status key, or ISO date, or "" for blank)
    to_value: str = ""
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

class WorkspaceInvite(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(index=True)
    code: str = Field(index=True, unique=True)
    created_by_user_id: int
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
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
    workspace_name: Optional[str] = None  # defaults to "{username}'s Workspace"

class WorkspaceIn(SQLModel):
    name: str

class WorkspaceSettingsIn(SQLModel):
    timezone: Optional[str] = None  # IANA name; "" = UTC

class MemberRoleIn(SQLModel):
    role: str  # 'owner' | 'member'

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
        "icon": c.icon or "",
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

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _warn_on_orphan_zero_workspace()
    if not os.getenv("SESSION_SECRET"):
        print(
            "[warning] SESSION_SECRET env var not set — using the on-disk secret "
            f"at {SECRET_FILE}. Multi-replica deployments MUST set SESSION_SECRET "
            "or sessions will randomly 401 as requests round-robin between replicas."
        )
    yield

app = FastAPI(title="ContentCal API", lifespan=lifespan)

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

# ── Session helpers ──────────────────────────────────────────────
def _set_session_cookie(response: Response, user_id: int, active_workspace_id: int):
    token = session_serializer.dumps({"uid": user_id, "wsid": active_workspace_id})
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,  # operator can put a TLS-terminating reverse proxy in front
        path="/",
    )

def _clear_session_cookie(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")

def _read_session(request: Request) -> Optional[dict]:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        return session_serializer.loads(raw)
    except BadSignature:
        return None

# ── Auth dependencies ────────────────────────────────────────────
def current_user(request: Request) -> User:
    sess = _read_session(request)
    if not sess:
        raise HTTPException(status_code=401, detail="Not authenticated")
    with Session(engine) as session:
        u = session.get(User, sess["uid"])
        if not u:
            raise HTTPException(status_code=401, detail="Account no longer exists")
        return u

def current_user_optional(request: Request) -> Optional[User]:
    sess = _read_session(request)
    if not sess:
        return None
    with Session(engine) as session:
        return session.get(User, sess["uid"])

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
        inv.revoked_at = datetime.utcnow().isoformat()
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
        return {"setup_required": not has_users}

@app.post("/api/auth/setup")
def do_setup(payload: SetupIn, response: Response):
    """One-time first-run setup. Creates the admin user, their workspace, and
    adopts any pre-existing workspace_id=0 data (legacy channels/entries/goal)
    into the new workspace. Refuses to run if any users already exist."""
    username = _validate_name(payload.username, field="Username", max_len=80)
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
        _set_session_cookie(response, user.id, ws.id)
        return {"user": user_dict(user), "workspace": workspace_dict(ws)}

@app.post("/api/auth/signup")
def signup(payload: SignupIn, response: Response):
    """Open signup. Creates a user with their own personal workspace. If an
    invite code is supplied, the user is also added to the invited workspace
    and that becomes their default."""
    username = _validate_name(payload.username, field="Username", max_len=80)
    if not payload.password:
        raise HTTPException(status_code=400, detail="Username and password are required.")
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    with Session(engine) as session:
        if session.exec(select(User).where(User.username == username)).first():
            raise HTTPException(status_code=400, detail="Username is already taken.")
        user = User(username=username, password_hash=hash_password(payload.password))
        session.add(user)
        try:
            session.commit()
        except IntegrityError:
            # Raced with another signup with the same username.
            session.rollback()
            raise HTTPException(status_code=400, detail="Username is already taken.")
        session.refresh(user)
        # Personal workspace — user is the owner.
        personal = Workspace(name=f"{username}'s Workspace", created_by_user_id=user.id)
        session.add(personal)
        session.commit()
        session.refresh(personal)
        session.add(WorkspaceMember(workspace_id=personal.id, user_id=user.id, role="owner"))
        for i, ch in enumerate(DEFAULT_CHANNELS):
            session.add(Channel(workspace_id=personal.id, position=i, name=ch["name"], pillars=ch["pillars"]))
        user.default_workspace_id = personal.id
        active_ws_id = personal.id
        # Honor invite code if present — atomic claim via conditional UPDATE so
        # two simultaneous accepts can't both succeed.
        if payload.invite_code:
            now_iso = datetime.utcnow().isoformat()
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
        session.add(user)
        session.commit()
        _set_session_cookie(response, user.id, active_ws_id)
        return {"user": user_dict(user)}

@app.post("/api/auth/login")
def login(payload: LoginIn, response: Response):
    with Session(engine) as session:
        user = session.exec(select(User).where(User.username == payload.username.strip())).first()
        if not user or not verify_password(payload.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid username or password.")
        # Pick active workspace = default, falling back to first membership
        active_ws = user.default_workspace_id
        if not active_ws or not _is_member(session, active_ws, user.id):
            first = session.exec(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).first()
            if first:
                active_ws = first.workspace_id
        if not active_ws:
            raise HTTPException(status_code=500, detail="No workspace available for this account. Contact an admin.")
        _set_session_cookie(response, user.id, active_ws)
        return {"user": user_dict(user), "active_workspace_id": active_ws}

@app.post("/api/auth/logout")
def logout(response: Response):
    _clear_session_cookie(response)
    return {"ok": True}

@app.get("/api/me")
def me(request: Request, user: User = Depends(current_user)):
    with Session(engine) as session:
        wss = _user_workspaces(session, user.id)
        sess = _read_session(request) or {}
        return {
            "user": user_dict(user),
            "workspaces": [workspace_dict(w) for w in wss],
            "active_workspace_id": sess.get("wsid") or user.default_workspace_id,
        }

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
        _set_session_cookie(response, user.id, payload.workspace_id)
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
        _set_session_cookie(response, user.id, ws.id)
        return workspace_dict(ws)

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
                _set_session_cookie(response, user.id, remaining.workspace_id)
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
            expires_at = (datetime.utcnow() + timedelta(days=payload.expires_in_days)).isoformat()
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
        inv.revoked_at = datetime.utcnow().isoformat()
        session.add(inv)
        session.commit()
        return {"ok": True}

@app.get("/api/invites/{code}")
def preview_invite(code: str, request: Request):
    """Public — lets the signup/join page show which workspace the invite
    belongs to before the user commits. Does NOT consume the invite.
    Rate-limited per IP to make code enumeration infeasible."""
    if not _check_rate(f"invite:{_client_ip(request)}", max_hits=20, window_seconds=60):
        raise HTTPException(status_code=429, detail="Too many invite lookups. Try again in a minute.")
    with Session(engine) as session:
        inv = session.exec(select(WorkspaceInvite).where(WorkspaceInvite.code == code)).first()
        if not inv:
            raise HTTPException(status_code=404, detail="Invite not found.")
        if inv.revoked_at:
            raise HTTPException(status_code=410, detail="This invite has been revoked.")
        if inv.used_by_user_id:
            raise HTTPException(status_code=410, detail="This invite has already been used.")
        if inv.expires_at and inv.expires_at < datetime.utcnow().isoformat():
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
        now_iso = datetime.utcnow().isoformat()
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
        _set_session_cookie(response, user.id, inv.workspace_id)
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
def get_data(ws: Workspace = Depends(current_workspace)):
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
        }

@app.put("/api/goal")
def update_goal(goal_in: GoalIn, ws: Workspace = Depends(current_workspace)):
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
def add_entries_bulk(entries: List[EntryIn], ws: Workspace = Depends(current_workspace)):
    """Atomic-per-row bulk insert used by the CSV import. Returns a
    per-row result list so the client can surface exactly which rows failed
    and why, instead of swallowing failures one HTTP call at a time."""
    if not entries:
        return {"created": [], "errors": []}
    if len(entries) > 1000:
        raise HTTPException(status_code=400, detail="Bulk import is limited to 1000 rows per request.")
    created = []
    errors = []
    with Session(engine) as session:
        for i, entry in enumerate(entries):
            try:
                v = _validate_entry_payload(session, ws.id, entry)
            except HTTPException as exc:
                errors.append({"index": i, "detail": str(exc.detail)})
                continue
            new_e = Entry(workspace_id=ws.id, **v)
            session.add(new_e)
            try:
                session.flush()
            except Exception as exc:
                session.rollback()
                errors.append({"index": i, "detail": f"Database error: {exc!s}"})
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
        c.edited_at = datetime.utcnow().isoformat()
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
def update_channels(channels_in: List[ChannelIn], request: Request, ws: Workspace = Depends(current_workspace)):
    if not channels_in:
        raise HTTPException(status_code=400, detail="At least one channel is required.")
    if len(channels_in) > 6:
        raise HTTPException(status_code=400, detail="Maximum 6 channels supported.")
    # Validate each channel before touching the DB.
    validated = []
    for ch_in in channels_in:
        validated.append({
            "name": _validate_name(ch_in.name, field="Channel name", max_len=80),
            "pillars": [_validate_name(p, field="Pillar", max_len=80) for p in (ch_in.pillars or [])][:20],
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
def delete_channel(channel_index: int, ws: Workspace = Depends(current_workspace)):
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
