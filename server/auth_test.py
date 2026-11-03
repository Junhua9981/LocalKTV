"""Offline auth regression tests; no network, providers or extra dependencies."""
import asyncio
import tempfile
import time
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

from ktv import config
from ktv.app import _handle_ws_message, create_app
from ktv.auth import Auth, AuthMiddleware, AuthStore, hash_password, verify_password
from ktv.rooms import Rooms
from ktv.store import TrackCache

PASSWORD = "correct horse battery"
ORIGIN = "https://203.0.113.10"
UA = (b"user-agent", b"Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)")


async def request(app, path="/", method="GET", body=b"", cookie="", origin=None, extra=()):
    path, _, query = path.partition("?")
    headers = [(b"host", b"203.0.113.10"), UA, *extra]
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if body:
        headers.append((b"content-type", b"application/x-www-form-urlencoded"))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "https", "path": path, "raw_path": path.encode(),
             "query_string": query.encode(), "root_path": "", "headers": headers,
             "client": ("192.0.2.1", 1234), "server": ("203.0.113.10", 443)}
    messages = []
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    async def send(message):
        messages.append(message)
    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    headers = {}
    for key, value in start["headers"]:
        headers.setdefault(key, []).append(value)
    return start["status"], headers, b"".join(m.get("body", b"") for m in messages)


def cookie_from(headers, name="ktv_session"):
    for raw in headers.get(b"set-cookie", []):
        jar = SimpleCookie()
        jar.load(raw.decode())
        if name in jar and jar[name].value:
            return f"{name}={jar[name].value}"
    return ""


seen_roles = []


async def backend(scope, receive, send):
    seen_roles.append((scope.get("ktv_auth") or {}).get("role"))
    if scope["type"] == "websocket":
        await send({"type": "websocket.accept"})
        while True:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                return
    else:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"protected"})


class AuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "ktv.db"
        self.auth = Auth(db_path=self.db, required=True, bootstrap_password=PASSWORD, public_url=ORIGIN)
        self.app = AuthMiddleware(backend, auth=self.auth)

    def tearDown(self):
        self.tmp.cleanup()

    async def post(self, path, cookie="", **form):
        body = urlencode(form or {"x": "1"}).encode()
        return await request(self.app, path, "POST", body, cookie=cookie, origin=ORIGIN)

    async def admin(self):
        status, headers, _ = await self.post("/auth/login", password=PASSWORD, next="/admin")
        self.assertEqual(status, 303)
        self.assertEqual(headers[b"location"], [(config.BASE_PATH + "/admin").encode()])
        return cookie_from(headers)

    async def guest(self):
        url = self.auth.invite_url()
        self.assertIsNotNone(url)
        status, headers, _ = await request(self.app, urlsplit(url).path)
        self.assertEqual(status, 200)
        return cookie_from(headers)

    def test_password_hash(self):
        stored = hash_password(PASSWORD)
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertTrue(verify_password(PASSWORD, stored))
        self.assertFalse(verify_password(PASSWORD + "x", stored))
        self.assertFalse(verify_password(PASSWORD, "garbage"))

    async def test_actual_app_every_surface_protected(self):
        with patch.object(config, "ACCESS_KEY", PASSWORD), patch.object(config, "PUBLIC_URL", ORIGIN), \
                patch.object(config, "AUTH_REQUIRED", True), patch.object(config, "DB_PATH", self.db):
            app = create_app(None, SimpleNamespace(status=lambda: {}), None)
            for path in ("/api/health", "/api/session", "/api/search", "/api/qr.svg", "/media/a/source.wav",
                         "/lyrics/a", "/docs", "/openapi.json", "/assets/a.js", "/api/library"):
                self.assertEqual((await request(app, path))[0], 401, path)
            self.assertEqual((await request(app, "/host"))[0], 303)
            self.assertEqual((await request(app, "/display"))[1][b"location"], [b"/pair"])
            status, headers, _ = await request(app, "/auth/login", "POST",
                urlencode({"password": PASSWORD}).encode(), origin=ORIGIN)
            self.assertEqual(status, 303)
            cookie = cookie_from(headers)
            self.assertEqual((await request(app, "/api/health", cookie=cookie))[0], 200)
            # No party yet: the TV (or admin) asking for the invite QR opens one.
            self.assertIsNone(app.state.auth.party())
            status, headers, body = await request(app, "/api/qr.svg", cookie=cookie)
            self.assertIsNotNone(app.state.auth.party())
            self.assertEqual(status, 200)
            self.assertIn(b"<svg", body)
            self.assertEqual(headers[b"cache-control"], [b"no-store"])

    async def test_admin_login_cookie_restart_logout(self):
        _, headers, _ = await self.post("/auth/login", password=PASSWORD)
        raw = headers[b"set-cookie"][0]
        for flag in (b"Secure", b"HttpOnly", b"SameSite=strict"):
            self.assertIn(flag, raw)
        cookie = cookie_from(headers)
        self.assertEqual((await request(self.app, "/api/session", cookie=cookie))[0], 200)
        self.assertEqual(seen_roles[-1], "admin")
        self.assertEqual((await request(self.app, "/api/session", cookie=cookie + "0"))[0], 401)
        self.assertEqual((await self.post("/auth/login", password="wrong")) [0], 401)
        # Restart: a new process loads the same sessions from SQLite.
        restarted = AuthMiddleware(backend, auth=Auth(db_path=self.db, required=True, public_url=ORIGIN))
        self.assertEqual((await request(restarted, "/api/session", cookie=cookie))[0], 200)
        await self.post("/auth/logout", cookie=cookie)
        self.assertEqual((await request(self.app, "/api/session", cookie=cookie))[0], 401)
        # Expiry
        cookie = await self.admin()
        for session in self.auth.store.sessions.values():
            session.expires_at = time.time() - 1
        self.assertEqual((await request(self.app, "/api/session", cookie=cookie))[0], 401)

    async def test_guest_invite_party_and_role_limits(self):
        self.assertIsNone(self.auth.invite_url())
        self.assertEqual((await request(self.app, "/j/1.abc"))[0], 403)
        admin = await self.admin()
        await self.post("/admin/party/start", cookie=admin)
        guest = await self.guest()
        self.assertTrue(guest)
        self.assertEqual((await request(self.app, "/r/main/remote", cookie=guest))[0], 200)
        self.assertEqual(seen_roles[-1], "guest")
        for path in ("/host", "/display", "/remote", "/", "/r/main/host", "/r/main/display", "/songs"):
            status, headers, _ = await request(self.app, path, cookie=guest)
            self.assertEqual((status, headers[b"location"]), (303, [b"/r/main/remote"]), path)
        self.assertEqual((await request(self.app, "/admin", cookie=guest))[1][b"location"], [b"/r/main/remote"])
        # the song cache page belongs to no room: the admin opens it as is
        self.assertEqual((await request(self.app, "/songs", cookie=admin))[0], 200)
        self.assertEqual((await self.post("/admin/party/end", cookie=guest))[0], 403)
        self.assertEqual((await self.post("/api/queue", cookie=guest))[0], 403)
        # An invite from two rotations ago no longer works; tampering never does.
        party_id = self.auth.party()[0]
        old = int(time.time() // self.auth.invite_step) - 2
        self.assertEqual((await request(self.app, f"/j/main/{old}.{self.auth._invite_sig('main', party_id, old)}"))[0], 403)
        self.assertEqual((await request(self.app, urlsplit(self.auth.invite_url()).path[:-1] + "0"))[0], 403)
        # Ending the party signs every guest out and kills the invite.
        await self.post("/admin/party/end", cookie=admin)
        self.assertEqual((await request(self.app, "/api/session", cookie=guest))[0], 401)
        self.assertIsNone(self.auth.invite_url())
        self.assertEqual((await request(self.app, "/api/session", cookie=admin))[0], 200)

    async def test_party_follows_the_tv(self):
        self.auth.start_party()
        guest = await self.guest()
        start = time.time()
        idle = self.auth.party_idle
        # TV connected: the party stays and is topped up long past its first expiry.
        self.assertEqual(self.auth.tend_parties({"main": 1}, start), [])
        for session in self.auth.store.sessions.values():
            self.assertGreater(session.expires_at, start + self.auth.party_ttl - 5)
        # TV gone: nothing happens until the grace period is over...
        self.assertEqual(self.auth.tend_parties({"main": 0}, start), [])
        self.assertEqual(self.auth.tend_parties({"main": 0}, start + idle - 1), [])
        # ...and a TV coming back in time resets it.
        self.assertEqual(self.auth.tend_parties({"main": 1}, start + idle - 1), [])
        self.assertEqual(self.auth.tend_parties({"main": 0}, start + idle), [])
        self.assertEqual((await request(self.app, "/api/session", cookie=guest))[0], 200)
        self.assertEqual(self.auth.tend_parties({"main": 0}, start + 2 * idle), ["main"])
        self.assertIsNone(self.auth.party())
        self.assertEqual((await request(self.app, "/api/session", cookie=guest))[0], 401)
        self.assertEqual(self.auth.tend_parties({"main": 0}, start + 3 * idle), [])

    async def test_tv_pairing(self):
        status, headers, body = await request(self.app, "/pair")
        self.assertEqual(status, 200)
        pair = cookie_from(headers, "ktv_pair")
        code = next(iter(self.auth.pending))
        self.assertIn(f"{code[:3]} {code[3:]}".encode(), body)
        # Not approved yet: same code again, no session.
        status, headers, body = await request(self.app, "/pair", cookie=pair)
        self.assertEqual((status, cookie_from(headers)), (200, ""))
        admin = await self.admin()
        _, _, body = await self.post("/admin/pair", cookie=admin, code="000000" if code != "000000" else "111111")
        self.assertIn("找不到".encode(), body)
        await self.post("/admin/pair", cookie=admin, code=code, label="客廳電視")
        status, headers, _ = await request(self.app, "/pair", cookie=pair)
        self.assertEqual((status, headers[b"location"]), (303, [b"/r/main/display"]))
        tv = cookie_from(headers)
        self.assertEqual((await request(self.app, "/r/main/display", cookie=tv))[0], 200)
        self.assertEqual(seen_roles[-1], "display")
        self.assertEqual((await request(self.app, "/host", cookie=tv))[1][b"location"], [b"/r/main/display"])
        self.assertEqual(len(self.auth.pending), 0)  # code used up
        # The admin page lists the TV by its name and can sign it out.
        _, _, page = await request(self.app, "/admin", cookie=admin)
        self.assertIn("客廳電視".encode(), page)
        sid = next(s.id for s in self.auth.store.sessions.values() if s.role == "display")
        await self.post("/admin/revoke", cookie=admin, id=sid)
        self.assertEqual((await request(self.app, "/r/main/display", cookie=tv))[1][b"location"], [b"/pair"])

    async def test_scan_to_pair_start_page(self):
        # A fresh laptop opening the site lands on the start page.
        self.assertEqual((await request(self.app, "/"))[1][b"location"], [b"/pair"])
        status, headers, body = await request(self.app, "/pair")
        self.assertEqual(status, 200)
        pair = cookie_from(headers, "ktv_pair")
        code = next(iter(self.auth.pending))
        self.assertIn(b"<svg", body)
        self.assertIn(b"/auth/login", body)  # the admin can sign in right here
        csp = headers[b"content-security-policy"][0].decode()
        self.assertIn("script-src 'nonce-", csp)
        self.assertNotIn("unsafe-inline'; connect", csp)
        self.assertEqual(self.auth.pending[code]["hint"], "螢幕・iPhone")
        status, _, body = await request(self.app, "/pair/status", cookie=pair)
        self.assertEqual(body, b'{"ready":false,"expired":false}')
        # The phone scans: not signed in yet -> login, then back to the same page.
        target = f"/pair/ok/{code}"
        status, headers, _ = await request(self.app, target)
        self.assertEqual(headers[b"location"], [b"/login?" + urlencode({"next": target}).encode()])
        status, headers, _ = await self.post("/auth/login", password=PASSWORD, next=target)
        self.assertEqual(headers[b"location"], [target.encode()])
        admin = cookie_from(headers)
        status, _, body = await request(self.app, target, cookie=admin)
        self.assertEqual(status, 200)
        self.assertNotIn(b"<select", body)  # one room: nothing to choose
        _, _, body = await self.post("/admin/pair", cookie=admin, code=code, label="", room="main", done="1")
        self.assertIn("配對完成".encode(), body)
        self.assertEqual((await request(self.app, target, cookie=admin))[0], 410)  # used up
        self.assertEqual((await request(self.app, "/pair/status", cookie=pair))[2], b'{"ready":true,"expired":false}')
        status, headers, _ = await request(self.app, "/pair", cookie=pair)
        self.assertEqual(headers[b"location"], [b"/r/main/display"])
        tv = next(s for s in self.auth.store.sessions.values() if s.role == "display")
        self.assertEqual(tv.label, "螢幕・iPhone")
        self.assertLess(tv.expires_at, time.time() + self.auth.party_ttl + 5)
        # A guest scanning a pairing QR gets nothing.
        self.auth.start_party()
        guest = await self.guest()
        await request(self.app, "/pair")
        code = next(iter(self.auth.pending))
        self.assertEqual((await request(self.app, f"/pair/ok/{code}", cookie=guest))[1][b"location"],
                         [b"/r/main/remote"])
        self.assertEqual((await request(self.app, "/pair/status"))[2], b'{"ready":false,"expired":true}')

    async def test_pair_under_base_path_with_stale_root_cookie(self):
        # Before KTV_BASE_PATH the browser kept a cookie at "/"; after it, a
        # second one at "/ktv". Both are sent, stale root copy last.
        with patch.object(config, "BASE_PATH", "/ktv"):
            stale = await self.admin()
            # Signed in already: /pair says so instead of bouncing to /display.
            status, headers, body = await request(self.app, "/pair", cookie=stale)
            self.assertEqual(status, 200)
            self.assertIn("管理員".encode(), body)
            self.assertIn(b'action="/ktv/auth/logout"', body)
            # "登出，改用配對碼" revokes every sent session and clears both paths.
            status, headers, _ = await request(self.app, "/auth/logout", "POST", b"next=%2Fpair",
                                               cookie=stale, origin=ORIGIN)
            self.assertEqual((status, headers[b"location"]), (303, [b"/ktv/pair"]))
            cleared = b" ".join(headers[b"set-cookie"])
            self.assertIn(b"Path=/ktv", cleared)
            self.assertIn(b"Path=/;", cleared)
            self.assertEqual((await request(self.app, "/api/session", cookie=stale))[0], 401)
            # Pair, while the browser still sends the dead root cookie.
            _, headers, _ = await request(self.app, "/pair", cookie=stale)
            pair = cookie_from(headers, "ktv_pair")
            code = next(iter(self.auth.pending))
            admin = await self.admin()
            await self.post("/admin/pair", cookie=admin, code=code, label="TV")
            status, headers, _ = await request(self.app, "/pair", cookie=f"{pair}; {stale}")
            self.assertEqual((status, headers[b"location"]), (303, [b"/ktv/r/main/display"]))
            tv = cookie_from(headers)
            # New cookie first, a still-valid admin cookie at "/" last: the TV
            # session must win, not the stale one Starlette would pick.
            await request(self.app, "/r/main/display", cookie=f"{tv}; {admin}")
            self.assertEqual(seen_roles[-1], "display")
            # A paired TV opening /pair is not redirected away either.
            self.assertEqual((await request(self.app, "/pair", cookie=tv))[0], 200)

    async def test_rooms_scope_guests_tvs_and_parties(self):
        rooms = Rooms(self.db)
        app = AuthMiddleware(backend, auth=self.auth, rooms=rooms)
        self.app = app
        admin = await self.admin()
        _, _, body = await self.post("/admin/room/create", cookie=admin, slug="vip", name="包廂 VIP")
        self.assertIn("包廂 VIP".encode(), body)
        self.assertIn("房號".encode(), (await self.post("/admin/room/create", cookie=admin, slug="Bad Slug"))[2])
        self.assertEqual(Rooms(self.db).names, {"main": "大廳", "vip": "包廂 VIP"})  # survives a restart
        # Admin: legacy URLs land in main, every room is open, unknown rooms 404.
        self.assertEqual((await request(app, "/host", cookie=admin))[1][b"location"], [b"/r/main/host"])
        self.assertEqual((await request(app, "/r/vip/host", cookie=admin))[0], 200)
        self.assertEqual(seen_roles[-1], "admin")
        self.assertEqual((await request(app, "/r/nope/remote", cookie=admin))[0], 404)
        self.assertEqual((await request(app, "/", cookie=admin))[0], 200)
        # Each room has its own party and invite; one room's QR never opens another.
        await self.post("/admin/party/start", cookie=admin, room="vip")
        self.assertIsNone(self.auth.invite_url("main"))
        invite = urlsplit(self.auth.invite_url("vip")).path
        self.assertTrue(invite.startswith("/j/vip/"))
        self.assertEqual((await request(app, invite.replace("/j/vip/", "/j/main/")))[0], 403)
        self.assertEqual((await request(app, invite.replace("/j/vip/", "/j/")))[0], 403)
        status, headers, _ = await request(app, invite)
        self.assertEqual(status, 200)
        guest = cookie_from(headers)
        self.assertEqual((await request(app, "/r/vip/remote", cookie=guest))[0], 200)
        self.assertEqual(seen_roles[-1], "guest")
        self.assertEqual((await request(app, "/r/main/remote", cookie=guest))[1][b"location"], [b"/r/vip/remote"])
        await request(app, "/api/session?room=main", cookie=guest)
        session = next(s for s in self.auth.store.sessions.values() if s.role == "guest")
        self.assertEqual(session.room, "vip")
        # Ending main's party leaves vip's guests alone; ending vip's signs them out.
        await self.post("/admin/party/start", cookie=admin, room="main")
        await self.post("/admin/party/end", cookie=admin, room="main")
        self.assertEqual((await request(app, "/api/session", cookie=guest))[0], 200)
        # A TV paired to vip opens only vip's display; moving it follows.
        await request(app, "/pair")
        _, headers, _ = await request(app, "/pair")
        pair = cookie_from(headers, "ktv_pair")
        code = next(c for c, p in self.auth.pending.items() if p["pid"] == pair.split("=", 1)[1])
        await self.post("/admin/pair", cookie=admin, code=code, label="包廂電視", room="vip")
        status, headers, _ = await request(app, "/pair", cookie=pair)
        self.assertEqual(headers[b"location"], [b"/r/vip/display"])
        tv = cookie_from(headers)
        self.assertEqual((await request(app, "/r/main/display", cookie=tv))[1][b"location"], [b"/r/vip/display"])
        sid = next(s.id for s in self.auth.store.sessions.values() if s.role == "display")
        await self.post("/admin/session/room", cookie=admin, id=sid, room="main")
        self.assertEqual((await request(app, "/r/main/display", cookie=tv))[0], 200)
        await self.post("/admin/session/room", cookie=admin, id=sid, room="vip")
        # Deleting the room signs out its guests and TVs; main cannot be deleted.
        self.assertIn("不能刪除".encode(), (await self.post("/admin/room/delete", cookie=admin, room="main"))[2])
        await self.post("/admin/room/delete", cookie=admin, room="vip")
        self.assertNotIn("vip", rooms.names)
        for cookie in (guest, tv):
            self.assertEqual((await request(app, "/api/session", cookie=cookie))[0], 401)
        self.assertEqual((await request(app, "/api/session", cookie=admin))[0], 200)

    async def test_origin_redirect_rate_and_body_limits(self):
        admin = await self.admin()
        for origin in (None, "https://evil.example", ORIGIN + ".evil.example"):
            self.assertEqual((await request(self.app, "/api/queue", "POST", cookie=admin, origin=origin))[0], 403)
        self.assertEqual((await request(self.app, "/api/queue", "POST", cookie=admin, origin=ORIGIN))[0], 200)
        _, headers, _ = await self.post("/auth/login", password=PASSWORD, next="https://evil.example")
        self.assertEqual(headers[b"location"], [b"/"])
        self.assertEqual((await request(self.app, "/auth/login", "POST", b"x" * 4097, origin=ORIGIN))[0], 413)
        for _ in range(8):
            await self.post("/auth/login", password="wrong")
        self.assertEqual((await self.post("/auth/login", password=PASSWORD))[0], 429)
        # Forged forwarding headers cannot bypass the limiter at application level.
        self.assertEqual((await request(self.app, "/auth/login", "POST", b"password=wrong", origin=ORIGIN,
            extra=[(b"x-forwarded-for", b"192.0.2.99")]))[0], 429)

    async def test_websocket_rejection_and_revocation(self):
        async def connect(cookie, origin):
            events = asyncio.Queue()
            incoming = asyncio.Queue()
            await incoming.put({"type": "websocket.connect"})
            headers = [(b"cookie", cookie.encode())]
            if origin:
                headers.append((b"origin", origin.encode()))
            scope = {"type": "websocket", "scheme": "wss", "path": "/ws", "headers": headers,
                     "query_string": b"", "client": ("192.0.2.1", 1234), "server": ("203.0.113.10", 443)}
            task = asyncio.create_task(self.app(scope, incoming.get, events.put))
            return task, events, incoming
        admin = await self.admin()
        for candidate, origin in (("", ORIGIN), (admin, None), (admin, "https://evil.example")):
            task, events, _ = await connect(candidate, origin)
            self.assertEqual((await asyncio.wait_for(events.get(), 2))["type"], "websocket.close")
            await task
        await self.post("/admin/party/start", cookie=admin)
        guest = await self.guest()
        task, events, _ = await connect(guest, ORIGIN)
        self.assertEqual((await asyncio.wait_for(events.get(), 2))["type"], "websocket.accept")
        self.assertEqual(seen_roles[-1], "guest")
        await self.post("/admin/party/end", cookie=admin)
        self.assertEqual((await asyncio.wait_for(events.get(), 3))["code"], 1008)
        await task

    async def test_ws_guest_can_remove_only_own_songs(self):
        class FakeState:
            def __init__(self):
                self.queue, self.current, self.sent, self.removed = [], None, [], []
            async def add_item(self, track, **_):
                item = SimpleNamespace(id=f"qi_{len(self.queue)}", track=track)
                self.queue.append(item)
                return item
            async def remove_item(self, item_id):
                self.removed.append(item_id)
            async def send(self, client_id, msg):
                self.sent.append(msg)
        state = FakeState()
        owners = {}
        alice, bob = {"role": "guest", "sid": "a"}, {"role": "guest", "sid": "b"}
        add = {"type": "queue.add", "track": {"id": "youtube_x", "title": "x"}}
        await _handle_ws_message(state, 1, "remote", add, alice, owners)
        await _handle_ws_message(state, 2, "remote", {"type": "queue.remove", "id": "qi_0"}, bob, owners)
        self.assertEqual(state.removed, [])
        self.assertEqual(state.sent[-1]["type"], "error")
        await _handle_ws_message(state, 1, "remote", {"type": "queue.remove", "id": "qi_0"}, alice, owners)
        await _handle_ws_message(state, 3, "host", {"type": "queue.remove", "id": "qi_0"},
                                 {"role": "admin", "sid": "z"}, owners)
        self.assertEqual(state.removed, ["qi_0", "qi_0"])

    async def test_first_run_setup_and_password_change(self):
        empty = Path(self.tmp.name) / "fresh.db"
        auth = Auth(db_path=empty, required=True, public_url=ORIGIN)  # no password: setup mode
        self.assertEqual(len(auth.setup_code), 8)
        app = AuthMiddleware(backend, auth=auth)
        # Every page leads to /setup; APIs and sockets are closed until then.
        for path in ("/", "/r/main/display", "/login", "/pair", "/admin"):
            self.assertEqual((await request(app, path))[1][b"location"], [b"/setup"], path)
        self.assertEqual((await request(app, "/api/session"))[0], 503)
        self.assertEqual((await request(app, "/setup"))[0], 200)
        async def setup(**form):
            return await request(app, "/auth/setup", "POST", urlencode(form).encode(), origin=ORIGIN)
        good = {"password": PASSWORD, "repeat": PASSWORD}
        self.assertEqual((await setup(code="ABCD-EFGH", **good))[0], 401)
        self.assertEqual((await setup(code=auth.setup_code, password=PASSWORD, repeat="other"))[0], 400)
        self.assertEqual((await setup(code=auth.setup_code, password="short", repeat="short"))[0], 400)
        code = auth.setup_code
        # Typed with a dash, lower case: still the same code.
        status, headers, _ = await setup(code=f" {code[:4].lower()}-{code[4:]} ", **good)
        self.assertEqual((status, headers[b"location"]), (303, [b"/"]))
        admin = cookie_from(headers)
        self.assertEqual(auth.setup_code, "")
        self.assertEqual((await request(app, "/api/session", cookie=admin))[0], 200)
        self.assertEqual(seen_roles[-1], "admin")
        # Done: /setup is gone, the code cannot be reused, a restart needs no setup.
        self.assertEqual((await request(app, "/setup"))[1][b"location"], [b"/"])
        self.assertEqual((await setup(code=code, password="x" * 12, repeat="x" * 12))[0], 401)
        self.assertTrue(auth.store.check_admin_password(PASSWORD))
        self.assertEqual(Auth(db_path=empty, required=True, public_url=ORIGIN).setup_code, "")
        # Changing the password on /admin keeps this device signed in, others out.
        other = cookie_from((await request(app, "/auth/login", "POST",
                                           urlencode({"password": PASSWORD}).encode(), origin=ORIGIN))[1])
        async def change(cookie, **form):
            return await request(app, "/admin/password", "POST", urlencode(form).encode(),
                                 cookie=cookie, origin=ORIGIN)
        self.assertIn("不正確".encode(), (await change(admin, old="wrong", password="n" * 12, repeat="n" * 12))[2])
        status, headers, _ = await change(admin, old=PASSWORD, password="n" * 12, repeat="n" * 12)
        self.assertEqual(status, 303)
        admin = cookie_from(headers)
        self.assertEqual((await request(app, "/api/session", cookie=admin))[0], 200)
        self.assertEqual((await request(app, "/api/session", cookie=other))[0], 401)
        self.assertTrue(auth.store.check_admin_password("n" * 12))

    async def test_disabled_local_mode_and_fail_closed(self):
        local = AuthMiddleware(backend, auth=Auth(db_path=self.db))
        self.assertEqual((await request(local, "/api/session"))[0], 200)
        empty = Path(self.tmp.name) / "empty.db"
        for kwargs in ({"bootstrap_password": "short", "public_url": ORIGIN},
                       {"bootstrap_password": PASSWORD},
                       {"bootstrap_password": PASSWORD, "public_url": "http://localhost:8080"},
                       {"bootstrap_password": PASSWORD, "public_url": ORIGIN + "/remote"}):
            with self.assertRaises(ValueError, msg=kwargs):
                Auth(db_path=empty, **kwargs)
        # Once set via the CLI, no bootstrap key is needed.
        AuthStore(empty).set_admin_password(PASSWORD)
        Auth(db_path=empty, required=True, public_url=ORIGIN)
        for track_id in (".", "..", "", "../secret"):
            cache = TrackCache()
            self.assertTrue(cache.dir_for(track_id).resolve().is_relative_to(cache.tracks_dir.resolve()))
            self.assertNotEqual(cache.dir_for(track_id).resolve(), cache.tracks_dir.resolve())


if __name__ == "__main__":
    unittest.main()
