"""Login gate for a public deployment: one admin, per-party guest invites, paired TVs.

Roles live on the server-side session, never in what the browser claims:

    admin    password login at /login        -> every room's /host /remote /display, /admin
             (first start: no password yet -> /setup, guarded by a one-time setup code from the log)
    display  /pair shows a QR (and a 6-digit code); the admin's phone scans it
             -> /r/<room>/display of the room it was paired to, for KTV_PARTY_HOURS
    guest    rotating invite QR -> /j/<room>/<token> -> /r/<room>/remote (until that room's party ends)

A room's party starts by itself when its TV asks for the invite QR, stays
open while a TV is connected, and ends (guests signed out) once no TV has
been there for KTV_PARTY_IDLE_MINUTES.  The admin can still end it by hand.

Guests and TVs belong to one room (ktv/rooms.py); each room runs its own
party and invite QR.  Sessions from before rooms count as room `main`.

Sessions are stored in SQLite (only a SHA-256 of the cookie token), so a
restart keeps everyone signed in; revocation takes effect immediately,
including on open WebSockets.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import html
import re
import logging
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from starlette.requests import HTTPConnection, Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import config
from .rooms import DEFAULT as DEFAULT_ROOM
from .rooms import Rooms, room_path, split_room_path

log = logging.getLogger(__name__)

# WebSocket roles each session role may take; the first one is the fallback.
WS_ROLES = {
    "admin": ("remote", "host", "display"),
    "display": ("display",),
    "guest": ("remote",),
}
ROLE_NAMES = {"admin": "管理員", "display": "電視", "guest": "客人"}
MIN_PASSWORD = 10
SETUP_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0/O, 1/I: read off a log, typed on a phone
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **SCRYPT)
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), dklen=32,
                                n=int(n), r=int(r), p=int(p), maxmem=64 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual.hex(), digest)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def device_label(user_agent: str) -> str:
    ua = user_agent.lower()
    for needle, name in (("iphone", "iPhone"), ("ipad", "iPad"), ("android", "Android"),
                         ("windows", "Windows"), ("mac os", "Mac"), ("linux", "Linux")):
        if needle in ua:
            return name
    return "裝置"


@dataclass
class Session:
    id: str  # SHA-256 of the cookie token
    role: str
    label: str
    party: str
    created_at: float
    last_seen: float
    expires_at: float
    saved_seen: float = 0.0
    room: str = ""  # guests and TVs: the room they belong to ("" = main)

    @property
    def home_room(self) -> str:
        return self.room or DEFAULT_ROOM


class AuthStore:
    """Sessions, the admin password hash and the current party, in SQLite."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    id          TEXT PRIMARY KEY,
                    role        TEXT NOT NULL,
                    label       TEXT NOT NULL DEFAULT '',
                    party       TEXT NOT NULL DEFAULT '',
                    created_at  REAL NOT NULL,
                    last_seen   REAL NOT NULL,
                    expires_at  REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(auth_sessions)")}
            if "room" not in columns:  # sessions from before rooms -> main
                self._conn.execute("ALTER TABLE auth_sessions ADD COLUMN room TEXT NOT NULL DEFAULT ''")
            self._conn.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (time.time(),))
            rows = self._conn.execute("SELECT * FROM auth_sessions").fetchall()
        # Every request checks this dict; SQLite is the durable copy.
        self.sessions = {row["id"]: Session(**dict(row), saved_seen=row["last_seen"]) for row in rows}

    # -- meta ------------------------------------------------------------
    def meta(self, key: str) -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM auth_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else ""

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO auth_meta (key, value) VALUES (?, ?) "
                               "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def set_admin_password(self, password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"admin password must be at least {MIN_PASSWORD} characters")
        self.set_meta("admin_password", hash_password(password))
        # A new password signs every admin device out (applies on next start
        # for a running server, which keeps its own copy of the sessions).
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM auth_sessions WHERE role = 'admin'")
        for sid in [s.id for s in self.sessions.values() if s.role == "admin"]:
            self.sessions.pop(sid, None)

    def check_admin_password(self, password: str) -> bool:
        stored = self.meta("admin_password")
        return bool(stored) and verify_password(password, stored)

    def secret(self) -> bytes:
        value = self.meta("invite_secret")
        if not value:
            value = secrets.token_hex(32)
            self.set_meta("invite_secret", value)
        return bytes.fromhex(value)

    # -- sessions --------------------------------------------------------
    def create(self, role: str, label: str, expires_at: float, party: str = "", room: str = "") -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        session = Session(token_hash(token), role, label[:40], party, now, now, expires_at, now, room)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO auth_sessions (id, role, label, party, created_at, last_seen, expires_at, room) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session.id, role, session.label, party, now, now, expires_at, room))
        self.sessions[session.id] = session
        return token

    def lookup(self, token: str) -> Session | None:
        if not 20 <= len(token) <= 64:
            return None
        session = self.sessions.get(token_hash(token))
        if session is None:
            return None
        now = time.time()
        if session.expires_at <= now:
            self.revoke(session.id)
            return None
        session.last_seen = now
        if now - session.saved_seen > 60:
            session.saved_seen = now
            with self._lock, self._conn:
                self._conn.execute("UPDATE auth_sessions SET last_seen = ? WHERE id = ?", (now, session.id))
        return session

    def alive(self, sid: str) -> bool:
        session = self.sessions.get(sid)
        return session is not None and session.expires_at > time.time()

    def revoke(self, sid: str) -> None:
        self.sessions.pop(sid, None)
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM auth_sessions WHERE id = ?", (sid,))

    def revoke_role(self, role: str, room: str | None = None) -> None:
        """Sign out every `role` session, or only those of one room."""
        for sid in [s.id for s in self.sessions.values()
                    if s.role == role and (room is None or s.home_room == room)]:
            self.revoke(sid)

    def set_room(self, sid: str, room: str) -> bool:
        session = self.sessions.get(sid)
        if session is None or session.role == "admin":
            return False
        session.room = room
        with self._lock, self._conn:
            self._conn.execute("UPDATE auth_sessions SET room = ? WHERE id = ?", (room, sid))
        return True

    def listing(self) -> list[Session]:
        now = time.time()
        return sorted((s for s in self.sessions.values() if s.expires_at > now),
                      key=lambda s: (list(WS_ROLES).index(s.role), -s.last_seen))


class Auth:
    """Policy on top of AuthStore: parties, invites, pairing, rate limits."""

    def __init__(self, *, db_path: Path, required: bool = False, bootstrap_password: str = "",
                 public_url: str = "", secure: bool = True, admin_days: int = 30,
                 party_hours: int = 12, invite_minutes: int = 5, party_idle_minutes: int = 30) -> None:
        self.enabled = required or bool(bootstrap_password)
        parsed = urlsplit(public_url)
        if self.enabled and (parsed.scheme not in ("http", "https") or not parsed.netloc
                             or parsed.path not in ("", "/") or parsed.query
                             or parsed.fragment or parsed.username):
            raise ValueError("KTV_PUBLIC_URL must be an absolute origin, e.g. https://ktv.example.com")
        if self.enabled and secure and parsed.scheme != "https":
            raise ValueError("Use HTTPS, or explicitly set KTV_COOKIE_SECURE=0 for local testing")
        if not (1 <= admin_days <= 365 and 1 <= party_hours <= 72 and 1 <= invite_minutes <= 60
                and 1 <= party_idle_minutes <= 24 * 60):
            raise ValueError("KTV_ADMIN_DAYS / KTV_PARTY_HOURS / KTV_INVITE_MINUTES / "
                             "KTV_PARTY_IDLE_MINUTES out of range")
        self.origin = public_url.rstrip("/")
        self.secure = secure
        self.admin_ttl = admin_days * 86400
        self.party_ttl = party_hours * 3600
        self.invite_step = invite_minutes * 60
        self.party_idle = party_idle_minutes * 60
        self.tv_gone: dict[str, float] = {}  # room -> when its last TV disconnected
        self.store: AuthStore | None = None
        self.setup_code = ""  # set while no admin password exists: /setup needs it
        self.attempts: OrderedDict[str, list[float]] = OrderedDict()
        self.failures: list[float] = []  # all IPs: caps distributed guessing
        self.pending: dict[str, dict] = {}  # pairing code -> {pid, expires, label}
        if not self.enabled:
            return
        self.store = AuthStore(db_path)
        if not self.store.meta("admin_password"):
            if bootstrap_password:
                # First start of an older deployment: KTV_ACCESS_KEY becomes the
                # admin password; change it on /admin later.
                self.store.set_admin_password(bootstrap_password)
            else:
                # Nothing is reachable until someone who can read the log sets
                # a password on /setup.
                self.setup_code = "".join(secrets.choice(SETUP_ALPHABET) for _ in range(8))
                log.warning("尚未設定管理員密碼。開啟 %s%s/setup ，輸入設定碼 %s-%s",
                            self.origin, config.BASE_PATH, self.setup_code[:4], self.setup_code[4:])

    def check_setup_code(self, value: str) -> bool:
        typed = "".join(value.split()).replace("-", "").upper()
        return bool(self.setup_code) and hmac.compare_digest(typed, self.setup_code)

    # -- rate limits -----------------------------------------------------
    def rate_ok(self, bucket: str, limit: int = 10, window: float = 300) -> bool:
        now = time.monotonic()
        recent = [stamp for stamp in self.attempts.pop(bucket, []) if now - stamp < window]
        allowed = len(recent) < limit
        if allowed:
            recent.append(now)
        self.attempts[bucket] = recent
        # Bound memory even if clients continuously rotate IP addresses.
        while len(self.attempts) > 4096:
            self.attempts.popitem(last=False)
        return allowed

    def global_failures_ok(self) -> bool:
        now = time.monotonic()
        self.failures = [stamp for stamp in self.failures if now - stamp < 3600]
        return len(self.failures) < 100

    # -- party / invites (one party per room) -----------------------------
    @staticmethod
    def _party_key(room: str) -> str:
        # main keeps the pre-rooms key, so a running party survives the upgrade
        return "party" if room == DEFAULT_ROOM else f"party:{room}"

    def party(self, room: str = DEFAULT_ROOM) -> tuple[str, float] | None:
        assert self.store is not None
        key = self._party_key(room)
        party_id, expires = self.store.meta(key), self.store.meta(key + "_expires")
        if not party_id or not expires or float(expires) <= time.time():
            return None
        return party_id, float(expires)

    def start_party(self, room: str = DEFAULT_ROOM) -> None:
        assert self.store is not None
        key = self._party_key(room)
        if self.party(room) is None:
            self.store.set_meta(key, secrets.token_hex(8))
        self.store.set_meta(key + "_expires", str(time.time() + self.party_ttl))

    def end_party(self, room: str = DEFAULT_ROOM) -> None:
        assert self.store is not None
        self.store.set_meta(self._party_key(room), "")
        self.store.revoke_role("guest", room)
        self.tv_gone.pop(room, None)

    def tend_parties(self, displays: dict[str, int], now: float | None = None) -> list[str]:
        """Called every minute with each room's connected TV count: keep a
        watched party open, end one nobody has watched for `party_idle`.
        Returns the rooms whose party just ended."""
        now = time.time() if now is None else now
        ended = []
        for room, count in displays.items():
            party = self.party(room)
            if party is None:
                self.tv_gone.pop(room, None)
            elif count:
                self.tv_gone.pop(room, None)
                if party[1] - now < self.party_ttl - 600:  # top up, not a write a minute
                    self.start_party(room)
            elif now - self.tv_gone.setdefault(room, now) >= self.party_idle:
                self.end_party(room)
                ended.append(room)
        return ended

    def _invite_sig(self, room: str, party_id: str, window: int) -> str:
        assert self.store is not None
        message = f"{room}:{party_id}:{window}".encode()
        return hmac.new(self.store.secret(), message, hashlib.sha256).hexdigest()[:32]

    def invite_url(self, room: str = DEFAULT_ROOM) -> str | None:
        party = self.party(room)
        if party is None:
            return None
        window = int(time.time() // self.invite_step)
        return f"{self.origin}{config.BASE_PATH}/j/{room}/{window}.{self._invite_sig(room, party[0], window)}"

    def check_invite(self, token: str, room: str = DEFAULT_ROOM) -> tuple[str, float] | None:
        party = self.party(room)
        window_raw, _, sig = token.partition(".")
        if party is None or not window_raw.isdigit() or len(sig) != 32:
            return None
        window, now_window = int(window_raw), int(time.time() // self.invite_step)
        # Accept the previous window too: a code scanned right at rotation works.
        if window not in (now_window, now_window - 1):
            return None
        return party if hmac.compare_digest(sig, self._invite_sig(room, party[0], window)) else None

    # -- TV pairing ------------------------------------------------------
    def new_pairing(self, hint: str = "電視") -> tuple[str, str]:
        now = time.time()
        self.pending = {c: p for c, p in self.pending.items() if p["expires"] > now}
        while len(self.pending) >= 32:
            self.pending.pop(next(iter(self.pending)))
        code = f"{secrets.randbelow(10 ** 6):06d}"
        while code in self.pending:
            code = f"{secrets.randbelow(10 ** 6):06d}"
        pid = secrets.token_hex(16)
        self.pending[code] = {"pid": pid, "expires": now + 600, "label": "", "hint": hint, "room": DEFAULT_ROOM}
        return code, pid

    def pairing_for(self, pid: str) -> tuple[str, dict] | None:
        for code, pending in self.pending.items():
            if hmac.compare_digest(pending["pid"], pid) and pending["expires"] > time.time():
                return code, pending
        return None

    def approve_pairing(self, code: str, label: str, room: str = DEFAULT_ROOM) -> bool:
        pending = self.pending.get(code.strip())
        if pending is None or pending["expires"] <= time.time():
            return False
        pending["label"] = label.strip()[:40] or pending.get("hint") or "電視"
        pending["room"] = room
        return True


PAGE_CSS = """
body{background:#101321;color:#fff;font:18px system-ui;margin:0;display:grid;min-height:100vh;place-items:center}
main{width:min(560px,90vw);padding:24px 0}input,button,select{box-sizing:border-box;width:100%;padding:14px;margin:8px 0;border-radius:8px;font:inherit}
button{background:#5077ff;color:white;border:0;cursor:pointer}button.ghost{background:#2a3050}button.danger{background:#a33a4a}
p,small{color:#bbb}a{color:#aabfff}table{width:100%;border-collapse:collapse;font-size:15px}
td,th{padding:6px 4px;border-bottom:1px solid #2a3050;text-align:left}td form{margin:0}td button{padding:6px 10px;margin:0;width:auto}
.code{font:700 min(18vw,140px)/1.1 ui-monospace,monospace;letter-spacing:.08em;text-align:center;margin:24px 0}
.qr{background:#fff;border-radius:12px;padding:8px;width:min(260px,70vw);margin:16px auto}.qr svg{display:block;width:100%;height:auto}
.row{display:flex;gap:8px}.row>*{flex:1}details{margin:8px 0}summary{cursor:pointer;color:#aabfff}h2 small code{font-size:14px}section{margin:28px 0}code{word-break:break-all;color:#cfe}
"""


def page(title: str, body: str, status: int = 200, refresh: int | str | None = None,
         script: str = "") -> HTMLResponse:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    csp = ("default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
           "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
    if script:  # only our own inline script, and it may only call back home
        nonce = secrets.token_urlsafe(16)
        csp += f"; script-src 'nonce-{nonce}'; connect-src 'self'"
        body += f'<script nonce="{nonce}">{script}</script>'
    return HTMLResponse(f'''<!doctype html><html lang="zh-Hant"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{meta}
<title>{html.escape(title)}</title><style>{PAGE_CSS}</style></head><body><main>{body}</main></body></html>''',
        status_code=status, headers={
            "Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin",
            "Content-Security-Policy": csp,
        })


def qr_svg(data: str) -> str:
    """An inline <svg> QR (inline markup needs no img-src)."""
    import qrcode
    from qrcode.image.svg import SvgPathImage

    svg = qrcode.make(data, image_factory=SvgPathImage, border=2).to_string(encoding="unicode")
    return re.sub(r"^<\?xml[^>]*>\s*", "", svg)


def ago(stamp: float) -> str:
    minutes = int((time.time() - stamp) // 60)
    if minutes < 2:
        return "剛剛"
    if minutes < 120:
        return f"{minutes} 分鐘前"
    if minutes < 2880:
        return f"{minutes // 60} 小時前"
    return f"{minutes // 1440} 天前"


class AuthMiddleware:
    COOKIE = "ktv_session"
    PAIR_COOKIE = "ktv_pair"
    PAGES = ("/", "/host", "/remote", "/display", "/admin", "/songs")
    ROOM_PAGES = ("/host", "/remote", "/display")
    # Which SPA / admin pages each role may open, and where it goes instead.
    HOME = {"admin": "/host", "display": "/display", "guest": "/remote"}
    PAGE_ROLES = {"/host": ("admin",), "/admin": ("admin",), "/songs": ("admin",),
                  "/display": ("admin", "display")}
    # admin pages that belong to no room (everything else opens in one)
    ROOMLESS = ("/", "/songs")

    def __init__(self, app, *, auth: Auth, rooms: Rooms | None = None):
        self.app = app
        self.auth = auth
        self.rooms = rooms if rooms is not None else Rooms(None)

    def home(self, session: Session) -> str:
        """App path a session lands on: the admin's room list, else its room page."""
        if session.role == "admin":
            return "/"
        return room_path(session.home_room, self.HOME[session.role])

    @property
    def store(self) -> AuthStore:
        assert self.auth.store is not None
        return self.auth.store

    @staticmethod
    def cookie_values(conn: HTTPConnection, name: str) -> list[str]:
        """Every value sent for `name`, most specific path first.

        A browser can hold the same cookie name at two paths (e.g. `/` from
        before KTV_BASE_PATH and `/ktv` now) and sends both; Starlette's
        `conn.cookies` keeps only the LAST one, which is the stale root copy.
        """
        values = []
        for part in conn.headers.get("cookie", "").split(";"):
            key, sep, value = part.strip().partition("=")
            if sep and key == name and value:
                values.append(value)
        return values

    def session(self, conn: HTTPConnection) -> Session | None:
        for token in self.cookie_values(conn, self.COOKIE):
            found = self.store.lookup(token)
            if found is not None:
                return found
        return None

    def cookie_paths(self, path: str = "") -> list[str]:
        """Paths to clear a cookie at: the current one plus the pre-sub-path one."""
        paths = [(config.BASE_PATH + path) or "/"]
        if config.BASE_PATH:
            paths.append(path or "/")
        return paths

    def clear_cookie(self, response: Response, name: str, path: str = "") -> None:
        for cookie_path in self.cookie_paths(path):
            response.delete_cookie(name, path=cookie_path, secure=self.auth.secure,
                                   httponly=True, samesite="strict")

    def origin_ok(self, conn: HTTPConnection) -> bool:
        # Explicit configured origin avoids trusting arbitrary Host / proxy headers.
        return conn.headers.get("origin") == self.auth.origin

    def set_session_cookie(self, response: Response, token: str, expires_at: float) -> None:
        if config.BASE_PATH:  # drop a stale copy at "/" that would shadow this one
            response.delete_cookie(self.COOKIE, path="/", secure=self.auth.secure,
                                   httponly=True, samesite="strict")
        response.set_cookie(self.COOKIE, token, max_age=max(60, int(expires_at - time.time())),
                            httponly=True, secure=self.auth.secure, samesite="strict", path=config.BASE_PATH or "/")

    def next_path(self, value: str) -> str:
        # Permit only the UI entry points, never an external redirect.
        if re.fullmatch(r"/pair/ok/[0-9]{6}", value):
            return value
        room, page = split_room_path(value)
        if room is not None:
            return value if page in self.ROOM_PAGES and self.rooms.exists(room) else "/"
        return value if value in self.PAGES else "/"

    async def form(self, request: Request) -> dict[str, str] | Response:
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
            return JSONResponse({"detail": "form required"}, status_code=415)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                return JSONResponse({"detail": "form too large"}, status_code=413)
        return {k: v[0] for k, v in parse_qs(body.decode("utf-8", errors="replace")).items()}

    # -- pages -----------------------------------------------------------
    def login_page(self, next_path: str, message: str = "", status: int = 200) -> Response:
        return page("LocalKTV 登入", f'''<h1>LocalKTV</h1>
<p>這裡是管理員登入。<b>來唱歌的朋友不用登入</b>，請掃描電視上的 QR 加入。</p>
<p role="alert">{html.escape(message)}</p><form method="post" action="{config.url('/auth/login')}">
<input type="hidden" name="next" value="{html.escape(next_path, quote=True)}">
<label for="pw">管理員密碼</label><input id="pw" name="password" type="password" required maxlength="512" autocomplete="current-password" autofocus>
<button type="submit">登入</button></form>
<form method="post" action="{config.url('/auth/logout')}"><button class="ghost" type="submit">登出目前裝置</button></form>
<p><small>電視請開 <a href="{config.url('/pair')}">{config.url('/pair')}</a> 取得配對碼。</small></p>''', status)

    def setup_page(self, message: str = "", status: int = 200) -> Response:
        origin = self.auth.origin + config.BASE_PATH
        return page("LocalKTV 首次設定", f'''<h1>LocalKTV 首次設定</h1>
<p>先設定管理員密碼。之後你用這組密碼登入，就能開電視畫面、點歌、管理房間。</p>
<p role="alert">{html.escape(message)}</p><form method="post" action="{config.url('/auth/setup')}">
<label for="code">設定碼</label><input id="code" name="code" required maxlength="16" autocomplete="off" autocapitalize="characters" placeholder="XXXX-XXXX" autofocus>
<p><small>設定碼印在伺服器的紀錄裡，每次啟動都不同。VPS 上執行
<code>sudo journalctl -u localktv | grep 設定碼</code>，在自己電腦執行則看啟動視窗。</small></p>
<label for="pw">管理員密碼（至少 {MIN_PASSWORD} 個字元）</label><input id="pw" name="password" type="password" required minlength="{MIN_PASSWORD}" maxlength="512" autocomplete="new-password">
<label for="pw2">再輸入一次</label><input id="pw2" name="repeat" type="password" required minlength="{MIN_PASSWORD}" maxlength="512" autocomplete="new-password">
<button type="submit">設定並登入</button></form>
<p><small>網址：<code>{html.escape(origin)}/</code></small></p>''', status)

    async def setup(self, request: Request, path: str, method: str, ip: str) -> Response:
        """No admin password yet: only /setup answers; everything else waits for it."""
        if path == "/auth/setup" and method == "POST":
            if not self.auth.rate_ok("setup:" + ip) or not self.auth.global_failures_ok():
                log.warning("setup rate-limited from %s", ip)
                return self.setup_page("嘗試次數過多，請稍後再試。", 429)
            values = await self.form(request)
            if isinstance(values, Response):
                return values
            if not self.auth.check_setup_code(values.get("code", "")):
                self.auth.failures.append(time.monotonic())
                log.warning("setup from %s: wrong setup code", ip)
                return self.setup_page("設定碼不正確（看伺服器紀錄裡最新的那一組）。", 401)
            password = values.get("password", "")
            if password != values.get("repeat", ""):
                return self.setup_page("兩次輸入的密碼不一樣。", 400)
            try:
                self.store.set_admin_password(password)
            except ValueError:
                return self.setup_page(f"密碼至少要 {MIN_PASSWORD} 個字元。", 400)
            self.auth.setup_code = ""
            log.info("admin password set on /setup from %s", ip)
            return self.sign_in_admin(request, "/")
        if path == "/setup" and method in ("GET", "HEAD"):
            return self.setup_page()
        if method in ("GET", "HEAD") and not path.startswith(("/api/", "/media/", "/lyrics/", "/assets/")):
            return RedirectResponse(config.url("/setup"), status_code=303)
        return JSONResponse({"detail": "setup required"}, status_code=503)

    def sign_in_admin(self, request: Request, next_path: str) -> Response:
        expires = time.time() + self.auth.admin_ttl
        token = self.store.create("admin", "管理員・" + device_label(request.headers.get("user-agent", "")), expires)
        response = RedirectResponse(config.url(next_path), status_code=303)
        self.set_session_cookie(response, token, expires)
        return response

    def room_options(self, selected: str = "") -> str:
        return "".join(
            f'<option value="{html.escape(slug, quote=True)}"{" selected" if slug == selected else ""}>'
            f'{html.escape(name)}</option>' for slug, name in self.rooms.names.items())

    def room_section(self, room: dict) -> str:
        slug = room["slug"]
        esc = html.escape(slug, quote=True)
        hidden = f'<input type="hidden" name="room" value="{esc}">'
        party = self.auth.party(slug)
        if party:
            invite = self.auth.invite_url(slug) or ""
            party_html = f'''<p>這場進行中。電視畫面關掉 {self.auth.party_idle // 60} 分鐘後自動結束，客人會被登出。電視上的點歌 QR 每 {self.auth.invite_step // 60} 分鐘更換。</p>
<img src="{config.url('/api/qr.svg')}?room={esc}&amp;t={int(time.time())}" alt="點歌 QR" style="width:220px;background:#fff;border-radius:8px">
<p><small>目前的邀請連結（約 {self.auth.invite_step // 60} 到 {2 * self.auth.invite_step // 60} 分鐘內有效）：<br><code>{html.escape(invite)}</code></small></p>
<form method="post" action="{config.url('/admin/party/end')}">{hidden}<button class="danger">結束這場（登出這間的客人，電視換一組新 QR）</button></form>'''
        else:
            party_html = f'''<p>目前沒有開場。打開這間的電視畫面就會自動開場、顯示點歌 QR。</p>
<form method="post" action="{config.url('/admin/party/start')}">{hidden}<button class="ghost">現在就開場</button></form>'''
        busy = f"電視 {room['displays']} 台・手機 {room['phones']} 支・清單 {room['queue']} 首"
        if room["current"]:
            busy += f"・正在唱《{html.escape(room['current'])}》"
        links = "・".join(f'<a href="{config.url(room_path(slug, page))}">{label}</a>'
                         for page, label in (("/display", "電視"), ("/remote", "遙控器"), ("/host", "主控")))
        delete = "" if slug == DEFAULT_ROOM else f'''<form method="post" action="{config.url('/admin/room/delete')}">{hidden}
<button class="danger">刪除這間（清單清空、登出這間的客人與電視）</button></form>'''
        return f'''<section class="room"><h2>{html.escape(room["name"])} <small><code>/r/{html.escape(slug)}</code></small></h2>
<p><small>{busy}</small></p><p>{links}</p>{party_html}
<details><summary>改名 / 刪除</summary><form method="post" action="{config.url('/admin/room/rename')}" class="row">{hidden}
<input name="name" maxlength="24" placeholder="新名稱" required><button class="ghost">改名</button></form>{delete}</details></section>'''

    def admin_page(self, current: Session, message: str = "") -> Response:
        rooms_html = "".join(self.room_section(room) for room in self.rooms.summary())
        full = len(self.rooms.names) >= self.rooms.max_rooms
        create = (f"<p><small>已達上限 {self.rooms.max_rooms} 間。</small></p>" if full else
                  f'''<form method="post" action="{config.url('/admin/room/create')}" class="row">
<input name="slug" maxlength="16" pattern="[a-z0-9][a-z0-9\\-]{{0,15}}" placeholder="房號（例：a、vip）" required>
<input name="name" maxlength="24" placeholder="名稱（例：包廂 A）"><button>新增房間</button></form>''')
        rows = []
        for s in self.store.listing():
            me = "（目前裝置）" if s.id == current.id else ""
            if s.role == "display":
                where = f'''<form method="post" action="{config.url('/admin/session/room')}" class="row"><input type="hidden" name="id" value="{s.id}">
<select name="room">{self.room_options(s.home_room)}</select><button class="ghost">移過去</button></form>'''
            elif s.role == "guest":
                where = html.escape(self.rooms.names.get(s.home_room, s.home_room))
            else:
                where = "全部"
            rows.append(f'''<tr><td>{ROLE_NAMES.get(s.role, s.role)}</td><td>{html.escape(s.label)}{me}</td><td>{where}</td><td>{ago(s.last_seen)}</td>
<td><form method="post" action="{config.url('/admin/revoke')}"><input type="hidden" name="id" value="{s.id}"><button class="ghost">登出</button></form></td></tr>''')
        rows_html = "".join(rows)
        return page("LocalKTV 管理", f'''<h1>LocalKTV 管理</h1><p role="alert">{html.escape(message)}</p>
{rooms_html}<section><h2>新增房間</h2><p><small>每間房有自己的電視、遙控器、主控和點歌清單，歌曲快取共用。</small></p>{create}</section>
<section><h2>配對電視</h2><p>在電視上開 <code>{html.escape(self.auth.origin + config.url("/pair"))}</code>，輸入畫面上的 6 位數配對碼並選擇房間。</p>
<form method="post" action="{config.url('/admin/pair')}"><div class="row"><input name="code" inputmode="numeric" pattern="[0-9]{{6}}" maxlength="6" placeholder="配對碼" required>
<input name="label" maxlength="40" placeholder="名稱（例：客廳電視）"></div><div class="row"><select name="room">{self.room_options(DEFAULT_ROOM)}</select><button>配對</button></div></form></section>
<section><h2>已登入的裝置</h2><table><tr><th>身分</th><th>裝置</th><th>房間</th><th>最後使用</th><th></th></tr>{rows_html}</table></section>
<section><details><summary>更改管理員密碼</summary><form method="post" action="{config.url('/admin/password')}">
<input name="old" type="password" required maxlength="512" autocomplete="current-password" placeholder="目前的密碼">
<input name="password" type="password" required minlength="{MIN_PASSWORD}" maxlength="512" autocomplete="new-password" placeholder="新密碼（至少 {MIN_PASSWORD} 個字元）">
<input name="repeat" type="password" required minlength="{MIN_PASSWORD}" maxlength="512" autocomplete="new-password" placeholder="再輸入一次新密碼">
<button class="ghost">更改密碼（其他管理員裝置會被登出）</button></form></details></section>
<p><a href="{config.url('/songs')}">歌曲快取</a>：修歌詞、換歌曲來源、重抓 MV、刪歌。</p>
<p><a href="{config.url('/')}">回到房間列表</a></p>''')

    # -- ASGI ------------------------------------------------------------
    async def __call__(self, scope, receive, send):
        if not self.auth.enabled or scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        conn = HTTPConnection(scope)
        session = self.session(conn)
        if scope["type"] == "websocket":
            await self.websocket(scope, receive, send, conn, session)
            return
        response = await self.http(scope, receive, conn, session)
        if response is None:
            assert session is not None
            scope["ktv_auth"] = self.login_info(session)
            await self.app(scope, receive, send)
            return
        response.headers["Cache-Control"] = "no-store"
        await response(scope, receive, send)

    @staticmethod
    def login_info(session: Session) -> dict[str, str]:
        # the admin is in every room: the page's `?room=` picks one
        room = "" if session.role == "admin" else session.home_room
        return {"role": session.role, "sid": session.id, "room": room}

    async def websocket(self, scope, receive, send, conn, session) -> None:
        if session is None or self.auth.setup_code or not self.origin_ok(conn):
            log.info("ws refused from %s: %s", conn.client.host if conn.client else "unknown",
                     "not signed in" if session is None else "origin rejected")
            await send({"type": "websocket.close", "code": 1008})
            return
        scope["ktv_auth"] = self.login_info(session)
        room = session.room
        # Check expiry/revocation on existing sockets, too — and a TV moved
        # to another room reconnects there.
        task = asyncio.create_task(self.app(scope, receive, send))
        try:
            while self.store.alive(session.id) and session.room == room:
                done, _ = await asyncio.wait({task}, timeout=1)
                if done:
                    await task
                    return
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            log.info("ws closed: session %s expired, signed out or moved", session.role)
            await send({"type": "websocket.close", "code": 1008})
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def http(self, scope, receive, conn, session) -> Response | None:
        """A response for the gate's own routes and refusals; None = pass to the app."""
        request = Request(scope, receive)
        path, method = scope["path"], scope["method"]
        ip = conn.client.host if conn.client else "unknown"
        if method not in ("GET", "HEAD", "OPTIONS") and not self.origin_ok(conn):
            log.warning("origin rejected: %s %s from %s", method, path, ip)
            return JSONResponse({"detail": "origin rejected"}, status_code=403)
        if self.auth.setup_code:
            return await self.setup(request, path, method, ip)

        if path == "/setup" and method in ("GET", "HEAD"):
            return RedirectResponse(config.url("/"), status_code=303)  # already set up
        if path == "/login" and method == "GET":
            next_path = self.next_path(request.query_params.get("next", "/"))
            if session is not None and session.role != "admin":
                return self.signed_in_page(session, "LocalKTV 登入", f'''
<p>要用管理員身分登入，請先登出。</p>''')
            return self.login_page(next_path, "這台裝置已經以管理員身分登入。" if session else "")
        if path == "/auth/login" and method == "POST":
            return await self.login(request, ip, session)
        if path == "/auth/logout" and method == "POST":
            values = await self.form(request)
            target = values.get("next") if isinstance(values, dict) else None
            # Revoke every session this browser sent, stale duplicates included.
            for token in self.cookie_values(conn, self.COOKIE):
                found = self.store.lookup(token)
                if found is not None:
                    self.store.revoke(found.id)
            response = RedirectResponse(config.url("/pair" if target == "/pair" else "/login"), status_code=303)
            self.clear_cookie(response, self.COOKIE)
            return response
        if path.startswith("/j/") and method == "GET":
            room, _, token = path[3:].rpartition("/")
            return self.join(token, room or DEFAULT_ROOM, conn, session, ip)
        if path == "/pair" and method == "GET":
            return self.pair(conn, session, ip)
        if path == "/pair/status" and method == "GET":
            found = self.pairing(conn)
            return JSONResponse({"ready": bool(found and found[1]["label"]), "expired": found is None})
        if path.startswith("/pair/ok/") and method == "GET":
            if session is None:
                return RedirectResponse(config.url("/login") + "?" + urlencode({"next": path}), status_code=303)
            if session.role != "admin":
                return RedirectResponse(config.url(self.home(session)), status_code=303)
            return self.pair_confirm(path[len("/pair/ok/"):])

        room, page_path = split_room_path(path)
        is_page = method in ("GET", "HEAD") and (
            path in self.PAGES or (room is not None and page_path in self.ROOM_PAGES))
        if room is not None and is_page and not self.rooms.exists(room):
            return page("沒有這個房間", f'''<h1>沒有這個房間</h1><p>房號「{html.escape(room)}」不存在，可能已被刪除。</p>
<p><a href="{config.url('/')}">回首頁</a></p>''', 404)

        if session is None:
            if (page_path == "/display" or path == "/") and is_page:
                # the start page: make this a screen, or sign in as the admin
                return RedirectResponse(config.url("/pair"), status_code=303)
            if is_page:
                return RedirectResponse(config.url("/login") + "?" + urlencode({"next": path}), status_code=303)
            return JSONResponse({"detail": "login required"}, status_code=401)

        if is_page and path != "/admin":
            if room is None and path not in self.ROOMLESS and session.role == "admin":
                # pre-rooms URL: the admin lands in main
                return RedirectResponse(config.url(room_path(DEFAULT_ROOM, path)), status_code=303)
            allowed = self.PAGE_ROLES.get(page_path, ("admin", "guest"))
            if session.role != "admin" and (
                    room != session.home_room or session.role not in allowed or page_path == "/"):
                return RedirectResponse(config.url(self.home(session)), status_code=303)
        if path == "/admin" or path.startswith("/admin/"):
            if session.role != "admin":
                if method in ("GET", "HEAD"):
                    return RedirectResponse(config.url(self.home(session)), status_code=303)
                return JSONResponse({"detail": "admin only"}, status_code=403)
            if path == "/admin" and method in ("GET", "HEAD"):
                return self.admin_page(session)
            if method == "POST":
                return await self.admin_action(path, request, session)
        if session.role != "admin" and method not in ("GET", "HEAD", "OPTIONS"):
            # Guests and TVs change things only over the role-checked WebSocket.
            return JSONResponse({"detail": "admin only"}, status_code=403)
        return None

    async def login(self, request: Request, ip: str, session: Session | None) -> Response:
        if not self.auth.rate_ok("login:" + ip) or not self.auth.global_failures_ok():
            log.warning("admin login rate-limited from %s", ip)
            response = self.login_page("/", "嘗試次數過多，請稍後再試。", 429)
            response.headers["Retry-After"] = "300"
            return response
        values = await self.form(request)
        if isinstance(values, Response):
            return values
        next_path = self.next_path(values.get("next", "/"))
        if not self.store.check_admin_password(values.get("password", "")):
            self.auth.failures.append(time.monotonic())
            log.warning("admin login failed from %s (wrong password)", ip)
            return self.login_page(next_path, "密碼不正確。", 401)
        if session:
            self.store.revoke(session.id)
        if len(self.store.sessions) >= 1024:
            return self.login_page(next_path, "登入裝置已達上限，請先到管理頁登出舊裝置。", 503)
        log.info("admin login from %s", ip)
        return self.sign_in_admin(request, next_path)

    def join(self, token: str, room: str, conn: HTTPConnection, session: Session | None, ip: str) -> Response:
        remote = config.url(room_path(room, "/remote"))
        if session is not None and (session.role == "admin" or session.home_room == room):
            return RedirectResponse(remote, status_code=303)
        if session is not None and session.role != "guest":
            return RedirectResponse(config.url(self.home(session)), status_code=303)
        if not self.auth.rate_ok("join:" + ip, limit=30):
            log.warning("guest join rate-limited from %s", ip)
            return page("請稍後再試", "<h1>請稍後再試</h1><p>嘗試次數過多。</p>", 429)
        party = self.auth.check_invite(token, room) if self.rooms.exists(room) else None
        if party is None:
            log.info("guest join from %s with an expired invite (room %s)", ip, room)
            return page("QR 已過期", "<h1>這個 QR 已經失效</h1><p>請重新掃描電視上的點歌 QR。</p>", 403)
        if sum(1 for s in self.store.sessions.values() if s.role == "guest") >= 200:
            return page("人數已滿", "<h1>這場的人數已達上限</h1>", 503)
        if session is not None:  # a guest of another room scanned this one: move over
            self.store.revoke(session.id)
        expires = time.time() + self.auth.party_ttl  # the party may outlive its first expiry
        token = self.store.create("guest", "客人・" + device_label(conn.headers.get("user-agent", "")),
                                  expires, party=party[0], room=room)
        # A same-site page hop, so the SameSite=Strict cookie is sent to /remote.
        response = page("加入 LocalKTV", f'<h1>已加入</h1><p><a href="{remote}">開始點歌</a></p>',
                        refresh=f"0; url={remote}")
        self.set_session_cookie(response, token, expires)
        return response

    def signed_in_page(self, session: Session, title: str, extra: str = "") -> Response:
        """Who this device is signed in as, and where it can go — never an auto-redirect."""
        display = config.url(room_path(session.home_room, "/display"))
        enter = "" if session.role == "guest" else f'<p><a href="{display}">進入 Display</a></p>'
        home = config.url(self.home(session))
        room = "" if session.role == "admin" else f"・{html.escape(self.rooms.names.get(session.home_room, session.home_room))}"
        return page(title, f'''<h1>{html.escape(title)}</h1>
<p>這台裝置目前以「{ROLE_NAMES.get(session.role, session.role)}」身分登入（{html.escape(session.label)}{room}）。</p>{extra}
{enter}<p><a href="{home}">前往 {html.escape(self.home(session))}</a></p>
<form method="post" action="{config.url('/auth/logout')}"><input type="hidden" name="next" value="/pair">
<button class="ghost" type="submit">登出，改用配對碼配對這台電視</button></form>''')

    def pairing(self, conn: HTTPConnection) -> tuple[str, dict] | None:
        """The pending pairing this browser started (its ktv_pair cookie)."""
        for pid in self.cookie_values(conn, self.PAIR_COOKIE):
            found = self.auth.pairing_for(pid)
            if found:
                return found
        return None

    def pair(self, conn: HTTPConnection, session: Session | None, ip: str) -> Response:
        """The start page of a device nobody signed in on: scan to make it a
        screen, or type the admin password."""
        found = self.pairing(conn)
        if found and found[1]["label"]:
            # Approved: this becomes a TV, replacing whatever it was signed in as.
            code, pending = found
            self.auth.pending.pop(code, None)
            if session is not None:
                self.store.revoke(session.id)
            room = pending.get("room") or DEFAULT_ROOM
            # a borrowed laptop, not a fixture: as long as a party, not a month
            expires = time.time() + self.auth.party_ttl
            token = self.store.create("display", pending["label"], expires, room=room)
            response = RedirectResponse(config.url(room_path(room, "/display")), status_code=303)
            self.set_session_cookie(response, token, expires)
            self.clear_cookie(response, self.PAIR_COOKIE, "/pair")
            return response
        if session is not None and not found:
            # Already signed in: say so instead of bouncing to /display.
            return self.signed_in_page(session, "這台裝置已登入")
        pid = None
        if found:
            code = found[0]
        else:
            if not self.auth.rate_ok("pair:" + ip):
                return page("請稍後再試", "<h1>請稍後再試</h1>", 429)
            code, pid = self.auth.new_pairing("螢幕・" + device_label(conn.headers.get("user-agent", "")))
        scan = f"{self.auth.origin}{config.url('/pair/ok/' + code)}"
        body = f'''<h1>LocalKTV</h1>
<section><h2>讓這台當電視螢幕</h2><p>用<b>已登入管理員的手機</b>掃這個 QR，按「確定」就好：</p>
<div class="qr">{qr_svg(scan)}</div>
<p><small>掃不到的話，在管理頁「配對電視」輸入 <b>{code[:3]} {code[3:]}</b>。QR 10 分鐘內有效，過期會自動換新的。</small></p></section>
<section><h2>我是管理員</h2><form method="post" action="{config.url('/auth/login')}">
<input type="hidden" name="next" value="/"><input name="password" type="password" required maxlength="512" autocomplete="current-password" placeholder="管理員密碼">
<button type="submit">登入</button></form></section>
<p><small>來唱歌的朋友不用登入，請用手機掃電視上的點歌 QR。</small></p>'''
        # Poll instead of a meta refresh, which would wipe a half-typed password.
        script = f'''setInterval(async()=>{{try{{const r=await(await fetch({json.dumps(config.url("/pair/status"))},{{cache:"no-store"}})).json();
if(r.ready||r.expired)location.reload()}}catch(e){{}}}},2000)'''
        response = page("LocalKTV", body, script=script)
        if pid:
            response.set_cookie(self.PAIR_COOKIE, pid, max_age=600, httponly=True,
                                secure=self.auth.secure, samesite="strict", path=config.url("/pair"))
        return response

    def pair_confirm(self, code: str) -> Response:
        """What the admin's phone shows after scanning a screen's pairing QR."""
        pending = self.auth.pending.get(code)
        if pending is None or pending["expires"] <= time.time() or pending["label"]:
            return page("配對 QR 已過期", f'''<h1>這個配對 QR 已經失效</h1>
<p>請掃螢幕上目前顯示的 QR（每 10 分鐘會換新的）。</p><p><a href="{config.url('/')}">回首頁</a></p>''', 410)
        if len(self.rooms.names) > 1:
            where = f'<label for="room">放到哪一間</label><select id="room" name="room">{self.room_options(DEFAULT_ROOM)}</select>'
        else:
            where = f'<input type="hidden" name="room" value="{DEFAULT_ROOM}">'
        return page("配對螢幕", f'''<h1>讓這台當電視螢幕？</h1>
<p>配對碼 {code[:3]} {code[3:]}，請確認跟螢幕上的一樣。</p>
<form method="post" action="{config.url('/admin/pair')}"><input type="hidden" name="code" value="{code}">
<input type="hidden" name="done" value="1">{where}
<label for="label">名稱</label><input id="label" name="label" maxlength="40" value="{html.escape(pending.get("hint", ""), quote=True)}">
<button type="submit">確定</button></form><p><a href="{config.url('/')}">取消</a></p>''')
    async def admin_action(self, path: str, request: Request, session: Session) -> Response:
        values = await self.form(request)
        if isinstance(values, Response):
            return values
        message = ""
        room = values.get("room", DEFAULT_ROOM) or DEFAULT_ROOM
        name = self.rooms.names.get(room, room)
        if path in ("/admin/party/start", "/admin/party/end", "/admin/pair", "/admin/session/room",
                    "/admin/room/rename", "/admin/room/delete") and not self.rooms.exists(room):
            return self.admin_page(session, "找不到這個房間。")
        if path == "/admin/party/start":
            self.auth.start_party(room)
        elif path == "/admin/party/end":
            self.auth.end_party(room)
            message = f"「{name}」這場已結束，這間的客人已登出。"
        elif path == "/admin/pair":
            ok = self.auth.approve_pairing(values.get("code", ""), values.get("label", ""), room)
            if ok and values.get("done"):  # from the scan-to-pair page
                return page("配對完成", f'''<h1>配對完成</h1><p>螢幕幾秒內會自動進入「{html.escape(name)}」。</p>
<p><a href="{config.url('/')}">回首頁</a></p>''')
            message = (f"配對完成，電視幾秒內會自動進入「{name}」。" if ok
                       else "找不到這組配對碼，請確認電視畫面上的數字。")
        elif path == "/admin/password":
            if not self.auth.rate_ok("password:" + session.id, limit=5):
                return self.admin_page(session, "嘗試次數過多，請稍後再試。")
            new = values.get("password", "")
            if not self.store.check_admin_password(values.get("old", "")):
                message = "目前的密碼不正確。"
            elif new != values.get("repeat", ""):
                message = "兩次輸入的新密碼不一樣。"
            elif len(new) < MIN_PASSWORD:
                message = f"新密碼至少要 {MIN_PASSWORD} 個字元。"
            else:
                # signs every admin device out, this one included: sign it back in
                self.store.set_admin_password(new)
                log.info("admin password changed")
                return self.sign_in_admin(request, "/admin")
        elif path == "/admin/revoke":
            sid = values.get("id", "")
            if sid == session.id:
                message = "要登出目前裝置請到 /login 按「登出目前裝置」。"
            else:
                self.store.revoke(sid)
                message = "已登出該裝置。"
        elif path == "/admin/session/room":
            ok = self.store.set_room(values.get("id", ""), room)
            message = f"已移到「{name}」，電視幾秒內會切過去。" if ok else "找不到這台裝置。"
        elif path == "/admin/room/create":
            error = self.rooms.create(values.get("slug", ""), values.get("name", ""))
            message = error or f"已新增「{self.rooms.names.get(values.get('slug', '').strip().lower(), '')}」。"
        elif path == "/admin/room/rename":
            message = "已改名。" if self.rooms.rename(room, values.get("name", "")) else "名稱不能空白。"
        elif path == "/admin/room/delete":
            if room == DEFAULT_ROOM:
                message = "大廳不能刪除。"
            else:
                self.auth.end_party(room)
                self.store.revoke_role("display", room)
                await self.rooms.delete(room)
                message = f"已刪除「{name}」。"
        else:
            return JSONResponse({"detail": "not found"}, status_code=404)
        if message:
            return self.admin_page(session, message)
        return RedirectResponse(config.url("/admin"), status_code=303)
