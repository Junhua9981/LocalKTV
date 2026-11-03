"""FastAPI application: REST + the single `/ws` WebSocket + static SPA."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel
from starlette.websockets import WebSocketState

from . import config, covers, furigana, library, monitor
from .auth import WS_ROLES, Auth, AuthMiddleware
from .lyrics_parse import parse as parse_lyrics
from .lyrics_parse import timing_of_lines
from .models import AIStatus, KaraokeMode, Track
from .pipeline import Pipeline
from .providers import lyrics_format_for, registry
from .rooms import DEFAULT as DEFAULT_ROOM
from .rooms import Rooms, room_path
from .state import BG_MODES, FX_MODES, JA_ASSISTS, KTVState
from .store import TrackCache

log = logging.getLogger(__name__)
diag_log = logging.getLogger("ktv.display")

HELLO_TIMEOUT_S = 5.0
# An SDP offer is ~2–5 KB; anything far bigger is not a phone mic.
MIC_SIGNAL_MAX = 16_000
# display.diag：電視回報的診斷資料，log 一行最多這麼長
DIAG_MAX = 2000

# Formats the manual-upload endpoint accepts. TTML is gone with AMLL — the
# server only renders QRC / LRC / YRC / TXT (see ktv/lyrics_parse.py).
LYRICS_UPLOAD_FORMATS = ("lrc", "qrc", "yrc", "txt")

# Windows' mimetypes registry maps .js to text/plain, which browsers reject
# for modules — always declare the media type ourselves.
MEDIA_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".wasm": "application/wasm",
    ".map": "application/json; charset=utf-8",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".woff2": "font/woff2",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
}


def file_response(path: Path) -> FileResponse:
    media_type = MEDIA_TYPES.get(path.suffix.lower())
    return FileResponse(path, media_type=media_type) if media_type else FileResponse(path)


class QueueRequest(BaseModel):
    track: Track | None = None
    id: str | None = None
    title: str | None = None
    artist: str | None = None
    duration_ms: int = 0
    source: str = "youtube"
    source_id: str | None = None
    requested_by: str = "guest"
    # None = auto: AI when the separator is available, else INSTANT
    karaoke_mode: KaraokeMode | None = None
    guide_vocal: float = 0.0


class LyricsRequest(BaseModel):
    content: str
    format: str = "lrc"


class RoomRequest(BaseModel):
    slug: str
    name: str = ""


class OffsetRequest(BaseModel):
    lyrics_offset_ms: int


def _track_from_request(req: QueueRequest) -> Track:
    if req.track is not None:
        return req.track
    if req.id:
        # allow a raw id like "youtube_abc123"
        parts = req.id.split("_", 1)
        if len(parts) == 2 and parts[0] == "youtube":
            return Track.from_source(
                "youtube",
                parts[1],
                title=req.title or parts[1],
                artist=req.artist or "",
                duration_ms=req.duration_ms,
                lyrics_offset_ms=0,
            )
    if req.source_id:
        return Track.from_source(
            req.source,
            req.source_id,
            title=req.title or req.source_id,
            artist=req.artist or "",
            duration_ms=req.duration_ms,
        )
    raise HTTPException(status_code=400, detail="track id / source_id required")


class BasePathMiddleware:
    """Accept requests under config.BASE_PATH by stripping it before routing.

    Unprefixed requests still work, so a proxy may forward with or without the
    prefix. Outgoing URLs are prefixed separately via config.url().
    """

    def __init__(self, app, base: str):
        self.app = app
        self.base = base

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope["path"]
            if path == self.base and scope["type"] == "http":
                query = scope.get("query_string", b"").decode("latin-1")
                await RedirectResponse(self.base + "/" + (f"?{query}" if query else ""),
                                       status_code=308)(scope, receive, send)
                return
            if path.startswith(self.base + "/"):
                scope = dict(scope, path=path[len(self.base):])
                if scope.get("raw_path"):
                    scope["raw_path"] = scope["raw_path"][len(self.base):]
        await self.app(scope, receive, send)


def create_app(state: KTVState, pipeline: Pipeline, cache: TrackCache,
               rooms: Rooms | None = None) -> FastAPI:
    """`state` is the `main` room; `rooms` holds every room (one room if omitted)."""
    if rooms is None:
        rooms = Rooms.single(state) if state is not None else Rooms(None)
    registry.load()  # 音源外掛（曲庫搜尋、取檔、榜單、平台封面）
    app = FastAPI(title="LocalKTV", version="0.1.0")
    app.state.ktv = {"state": state, "pipeline": pipeline, "cache": cache, "rooms": rooms}
    auth = Auth(
        db_path=config.DB_PATH, required=config.AUTH_REQUIRED,
        bootstrap_password=config.ACCESS_KEY, public_url=config.PUBLIC_URL,
        secure=config.COOKIE_SECURE, admin_days=config.ADMIN_DAYS,
        party_hours=config.PARTY_HOURS, invite_minutes=config.INVITE_MINUTES,
        party_idle_minutes=config.PARTY_IDLE_MINUTES,
    )
    app.state.auth = auth
    app.add_middleware(AuthMiddleware, auth=auth, rooms=rooms)
    if config.BASE_PATH:
        app.add_middleware(BasePathMiddleware, base=config.BASE_PATH)
    app.add_middleware(monitor.SlowRequestMiddleware)  # outermost: times everything
    # queue item id -> session that queued it: guests may only remove their own
    owners: dict[str, str] = {}
    monitor_tasks: list[asyncio.Task] = []

    def room_slug(conn: Request | WebSocket) -> str:
        """The room a request is about: a guest / TV is pinned to its own
        room by its login; the admin (or a LAN without login) picks one with
        `?room=`."""
        login = conn.scope.get("ktv_auth")
        if login and login.get("role") != "admin":
            return login.get("room") or DEFAULT_ROOM
        return conn.query_params.get("room") or DEFAULT_ROOM

    def room_state(request: Request) -> KTVState:
        found = rooms.get(room_slug(request))
        if found is None:
            raise HTTPException(status_code=404, detail="unknown room")
        return found

    async def tend_parties() -> None:
        """Every minute: a party stays open while its room's TV is connected
        and ends a while after the last TV left (ktv/auth.py)."""
        while True:
            await asyncio.sleep(60)
            try:
                displays = {row["slug"]: row["displays"] for row in rooms.summary()}
                for slug in auth.tend_parties(displays):
                    log.info("party in room %s ended: no TV for %d min", slug, auth.party_idle // 60)
            except Exception:  # never let the loop die
                log.warning("tend_parties failed", exc_info=True)

    @app.on_event("startup")
    async def _startup() -> None:
        pipeline.start()
        # 已下載但沒封面的歌（封面功能上線前下載的）在背景補一次
        threading.Thread(
            target=covers.backfill, args=(state.store, state.cache.has_source),
            name="covers-backfill", daemon=True,
        ).start()
        monitor_tasks.extend(monitor.start(rooms.all))
        if auth.enabled:
            monitor_tasks.append(asyncio.create_task(tend_parties()))
        log.info("LocalKTV started (base path %r, login gate %s)",
                 config.BASE_PATH or "/", "on" if auth.enabled else "off")

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        log.info("LocalKTV shutting down")
        for task in monitor_tasks:
            task.cancel()
        await pipeline.stop()

    # ------------------------------------------------------------------
    # REST
    # ------------------------------------------------------------------
    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "pipeline": pipeline.status(), "auth": auth.enabled}

    # The QR the display shows: phones scan it to open /remote (the root URL
    # redirects there).  SVG stays crisp at any size and needs no Pillow;
    # same fail-soft rule as the ASCII QR in main.py, a broken QR must never
    # break the server.  With the login gate on, it carries the current party
    # invite (rotates every few minutes) and is 404 while no party is open.
    qr_cache: dict[str, bytes] = {}

    def qr_bytes(data: str) -> bytes:
        if data not in qr_cache:
            import io

            import qrcode
            from qrcode.image.svg import SvgPathImage

            qr = qrcode.QRCode(border=2, box_size=10)
            qr.add_data(data)
            qr.make(fit=True)
            buf = io.BytesIO()
            qr.make_image(image_factory=SvgPathImage).save(buf)
            qr_cache.clear()  # only the current invite is ever needed
            qr_cache[data] = buf.getvalue()
        return qr_cache[data]

    @app.get("/api/qr.svg")
    async def qr_svg(request: Request) -> Response:
        slug = room_slug(request)
        if not rooms.exists(slug):
            return Response(status_code=404, headers={"Cache-Control": "no-store"})
        data = auth.invite_url(slug) if auth.enabled else config.join_url(room_path(slug, "/remote"))
        login = request.scope.get("ktv_auth") or {}
        if data is None and login.get("role") in ("admin", "display"):
            # the TV showing its invite is what opens the room for guests
            auth.start_party(slug)
            log.info("party in room %s started by its %s", slug, login.get("role"))
            data = auth.invite_url(slug)
        if data is None:
            return Response(status_code=404, headers={"Cache-Control": "no-store"})
        try:
            content = qr_bytes(data)
        except Exception:
            log.warning("QR render failed", exc_info=True)
            return Response(status_code=503)
        return Response(
            content=content,
            media_type="image/svg+xml",
            headers={"Cache-Control": "no-store" if auth.enabled else "max-age=3600"},
        )

    @app.get("/api/search")
    async def search(
        request: Request, q: str, limit: int = 10, source: str = "main", cid: str = "",
        page: int = 1, raw: bool = False,
    ) -> list[dict[str, Any]]:
        from .providers import YouTubeSearchProvider
        from .textnorm import fold

        if not q.strip():
            return []
        limit = max(1, min(limit, 30))
        page = max(1, min(page, 20))

        if source == "youtube":
            # Dedicated tab: KTV-flavoured YouTube lookup; queued results play
            # the downloaded video as-is (no lyrics crawl, no lyrics display).
            # raw (admin picking a replacement audio): the plain query, since
            # the original recording is wanted, not a KTV video.
            try:
                tracks = await YouTubeSearchProvider().search(q if raw else f"{q} KTV", limit)
            except Exception as exc:
                log.warning("YouTube search failed for %r: %s", q, exc)
                raise HTTPException(status_code=502, detail=str(exc))
            if not raw:
                log_search(request, q, len(tracks), cid, "youtube", page)
            return [{**t.model_dump(mode="json"), "video_only": True} for t in tracks]

        # main: local cache first (instant, page 1 only), the catalogue plug-in
        # second, no YouTube fallback — the YouTube tab owns that.  `limit` is
        # the page size each platform is asked for; merged rows are not cut
        # back to it.  No plug-in: YouTube stands in for the catalogue (the
        # original recording, so lyrics + AI separation work as usual).
        cache_hits: list[Track] = []
        for track in state.store.search_cached(q, limit=limit * 3) if page == 1 else []:
            if track.id in {t.id for t in cache_hits}:
                continue
            if not state.cache.has_source(track.id):
                continue  # queued once but never acquired — not a real hit
            cache_hits.append(track)
            if len(cache_hits) >= limit:
                break

        hits: list[Track] = list(cache_hits)
        seen_ids = {t.id for t in hits}
        # same folded title+artist = the same song → the cached copy wins
        seen_songs = {(fold(t.title), fold(t.artist)) for t in hits}

        agg: list[Track] = []
        catalog = registry.catalog()
        if config.AGG_AUDIO and catalog is not None:
            try:
                # cid: the asking remote — its newer query drops this one
                agg = await asyncio.to_thread(
                    catalog.search, q, limit, cid[:64], page
                )
            except registry.SearchSuperseded:
                agg = []  # the remote moved on to a newer query
                cid = ""  # … so this one is not a search worth counting
            except Exception:
                log.warning("aggregate search failed for %r", q, exc_info=True)
                agg = []
                cid = ""  # a broken source is not 「搜不到」
        elif catalog is None and page == 1:
            try:
                agg = await YouTubeSearchProvider().search(q, limit)
            except Exception:
                log.warning("YouTube search failed for %r", q, exc_info=True)
                cid = ""
        for track in agg:
            song = (fold(track.title), fold(track.artist))
            if track.id in seen_ids or song in seen_songs:
                continue
            seen_ids.add(track.id)
            seen_songs.add(song)
            hits.append(track)

        # 已下載的那份沒封面：借平台搜尋結果裡同一首歌的，順便記進資料庫
        # （YouTube 縮圖是 16:9 影片截圖，不拿來當封面）
        catalog_covers = {(fold(t.title), fold(t.artist)): t.cover for t in agg if t.cover and t.source != "youtube"}
        for track in cache_hits:
            cover = catalog_covers.get((fold(track.title), fold(track.artist)))
            if not track.cover and cover:
                track.cover = cover
                await asyncio.to_thread(state.store.set_cover, track.id, cover)

        out = [{**t.model_dump(mode="json"), "cached": True} for t in cache_hits]
        out += [t.model_dump(mode="json") for t in hits[len(cache_hits):]]
        if raw:
            return out
        out = apply_search_rules(q, out, page)
        log_search(request, q, len(out), cid, "main", page)
        return out

    def log_search(request: Request, q: str, results: int, cid: str, source: str, page: int) -> None:
        """統計: a guest's typed search.  Only the remote's search box sends
        a cid; 管理員換音檔 (raw), 載入更多 (page > 1) and chart look-ups do not
        count."""
        from .textnorm import fold

        if not cid or page != 1:
            return
        try:
            state.store.log_search(q, fold(q), results, room_slug(request), cid, source)
        except Exception:
            log.warning("usage log failed", exc_info=True)

    def apply_search_rules(q: str, rows: list[dict[str, Any]], page: int) -> list[dict[str, Any]]:
        """The admin's fixes for this keyword (/songs → 搜尋排序): pinned songs
        lead page 1 in their order, hidden results never show."""
        from .textnorm import fold

        pins, hidden = state.store.search_rules(fold(q))
        if not pins and not hidden:
            return rows
        # a merged row carrying a pinned entry among its platforms is that song
        pinned_ids = {p.id for p in pins} | {f"{p.source}_{a}" for p in pins for a in [p.source_id, *p.alternates]}

        def same(row: dict[str, Any]) -> bool:
            ids = {row["id"]} | {f"{row.get('source')}_{a}" for a in [row.get("source_id"), *(row.get("alternates") or [])]}
            return bool(ids & pinned_ids)

        rest = [r for r in rows if r["id"] not in hidden and not same(r)]
        if page != 1:
            return rest
        lead = []
        for pin in pins:
            row = {**pin.model_dump(mode="json"), "pinned": True}
            if state.cache.has_source(pin.id):
                row["cached"] = True
            lead.append(row)
        return lead + rest

    # 各 KTV 站台的榜單（音源外掛提供；沒裝 = 沒有榜單）：週級快取，抓失敗回舊資料。
    @app.get("/api/charts")
    async def charts_index() -> list[dict[str, Any]]:
        provider = registry.charts()
        return provider.index() if provider is not None else []

    @app.get("/api/charts/{source}")
    async def chart(source: str) -> dict[str, Any]:
        provider = registry.charts()
        if provider is None:
            raise HTTPException(status_code=404, detail="unknown chart source")
        try:
            return await provider.get_chart(source)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown chart source")
        except Exception as exc:  # noqa: BLE001 — 站台掛掉要有清楚的 502 訊息
            log.warning("chart %s failed: %s", source, exc)
            raise HTTPException(status_code=502, detail=str(exc))

    # 熱門榜封面：q = "歌名\t歌手"，回 [檔名 | ""（查無）| null（查詢中，晚點再問）]
    @app.get("/api/covers")
    async def covers_lookup(q: list[str] = Query(default=[])) -> dict[str, Any]:
        songs = [(t, a) for t, _, a in (item.partition("\t") for item in q[: covers.BATCH_MAX])]
        return {"covers": await asyncio.to_thread(covers.lookup, songs)}

    # 搜尋結果的封面：u = Track.cover（平台圖片網址），伺服器代抓並快取
    @app.get("/api/covers/src")
    async def cover_source(u: str = ""):
        path = await asyncio.to_thread(covers.remote_image, u)
        if path is None:
            raise HTTPException(status_code=404, detail="cover not found")
        response = file_response(path)
        response.headers["cache-control"] = "public, max-age=31536000, immutable"
        return response

    @app.get("/api/covers/img/{name}")
    async def cover_image(name: str):
        path = covers.image_path(name)
        if path is None:
            raise HTTPException(status_code=404, detail="cover not found")
        response = file_response(path)
        response.headers["cache-control"] = "public, max-age=31536000, immutable"
        return response

    @app.get("/api/session")
    async def session(request: Request) -> dict[str, Any]:
        return room_state(request).snapshot().model_dump(mode="json")

    @app.get("/api/rooms")
    async def rooms_list(request: Request) -> list[dict[str, Any]]:
        """Admin: every room with who is in it; a guest / TV: just its own."""
        login = request.scope.get("ktv_auth")
        if login and login.get("role") != "admin":
            slug = login.get("room") or DEFAULT_ROOM
            return [{"slug": slug, "name": rooms.names.get(slug, slug)}]
        return rooms.summary()

    # Non-GET requests are admin-only at the login gate already.
    @app.post("/api/rooms")
    async def rooms_create(req: RoomRequest) -> dict[str, Any]:
        error = rooms.create(req.slug, req.name)
        if error:
            raise HTTPException(status_code=400, detail=error)
        return {"ok": True, "slug": req.slug.strip().lower()}

    @app.delete("/api/rooms/{slug}")
    async def rooms_delete(slug: str) -> dict[str, Any]:
        if slug == DEFAULT_ROOM or not rooms.exists(slug):
            raise HTTPException(status_code=404, detail="unknown room")
        if auth.enabled:  # its guests and TVs have nowhere to go
            auth.end_party(slug)
            auth.store.revoke_role("display", slug)
        await rooms.delete(slug)
        return {"ok": True}

    @app.post("/api/queue")
    async def queue_add(req: QueueRequest, request: Request) -> dict[str, Any]:
        track = _track_from_request(req)
        item = await room_state(request).add_item(
            track,
            requested_by=req.requested_by,
            karaoke_mode=req.karaoke_mode,
            guide_vocal=req.guide_vocal,
        )
        return item.model_dump(mode="json")

    @app.delete("/api/queue/{item_id}")
    async def queue_remove(item_id: str, request: Request) -> dict[str, Any]:
        removed = await room_state(request).remove_item(item_id)
        if not removed:
            raise HTTPException(status_code=404, detail="queue item not found")
        return {"ok": True, "id": item_id}

    @app.get("/api/scores")
    async def scores(track_id: str, limit: int = 5) -> list[dict[str, Any]]:
        """本曲高分 (every room)."""
        return state.store.song_scores(track_id, max(1, min(20, limit)))

    @app.get("/api/tracks/{track_id}")
    async def track_info(track_id: str) -> dict[str, Any]:
        track = state.store.get_track(track_id)
        if track is None:
            track = Track.from_source("youtube", track_id)
        stems = cache.stem_paths(track_id)
        return {
            "track": track.model_dump(mode="json"),
            "manifest": cache.read_manifest(track_id),
            "has_source": cache.has_source(track_id),
            "has_lyrics": cache.lyrics_path(track_id) is not None,
            "stems": (
                {
                    "vocals": config.url(f"/media/{track_id}/{stems[0].name}"),
                    "instrumental": config.url(f"/media/{track_id}/{stems[1].name}"),
                }
                if stems
                else None
            ),
        }

    @app.patch("/api/tracks/{track_id}")
    async def track_patch(track_id: str, req: OffsetRequest) -> dict[str, Any]:
        # the offset belongs to the song: every room playing it follows
        for room in rooms.all():
            await room.set_lyrics_offset(track_id, req.lyrics_offset_ms)
        return {"ok": True, "lyrics_offset_ms": req.lyrics_offset_ms}

    @app.post("/api/lyrics/{track_id}")
    async def lyrics_upload(track_id: str, req: LyricsRequest) -> dict[str, Any]:
        directory = cache.dir_for(track_id)
        directory.mkdir(parents=True, exist_ok=True)
        ext = req.format.lower().lstrip(".") or "lrc"
        if ext not in LYRICS_UPLOAD_FORMATS:
            raise HTTPException(status_code=400, detail="unsupported lyrics format")
        # exactly one lyrics file per track: drop every previous variant so
        # cache lookups can never pick a stale format over the manual one
        for stale in directory.glob("lyrics.*"):
            if stale.is_file():
                stale.unlink(missing_ok=True)
        (directory / f"lyrics.{ext}").write_text(req.content, encoding="utf-8")
        cache.update_manifest(track_id, lyrics_source=ext, lyrics_manual=True)
        for room in rooms.all():
            if room.current is not None and room.current.track.id == track_id:
                await room.send_display(
                    {"type": "display.reload_lyrics", "url": config.url(f"/lyrics/{track_id}")}
                )
            await room.broadcast_snapshot()
        return {"ok": True, "format": ext}

    @app.get("/lyrics/{track_id}")
    async def lyrics(track_id: str) -> JSONResponse:
        path = cache.lyrics_path(track_id)
        if path is None:
            raise HTTPException(status_code=404, detail="no lyrics cached")
        track = state.store.get_track(track_id)
        offset = track.lyrics_offset_ms if track else 0
        duration = track.duration_ms if track else 0
        content = path.read_text(encoding="utf-8", errors="replace")
        fmt = lyrics_format_for(path)
        title = track.title if track else ""
        artist = track.artist if track else ""

        def parsed() -> list[dict[str, Any]]:
            # 日文歌加假名注音（斷詞器第一次要載入約 1 秒，所以在 worker thread）
            return furigana.annotate(parse_lyrics(fmt, content, duration, title=title, artist=artist))

        lines = await asyncio.to_thread(parsed)
        return JSONResponse(
            {
                "track_id": track_id,
                "format": fmt,
                "offset_ms": offset,
                # word =逐字 sweep, line = whole-line highlight (LyricStage picks)
                "timing": timing_of_lines(lines),
                # the display renders this; `content` stays for debugging/tools
                "lines": lines,
                "content": content,
            }
        )

    @app.get("/media/{track_id}/{file_name}")
    async def media(track_id: str, file_name: str):
        base = cache.dir_for(track_id).resolve()
        target = (base / file_name).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            raise HTTPException(status_code=404, detail="media not found")
        return file_response(target)

    # song cache page (admin): /api/library…
    library.register(app, library.Library(getattr(pipeline, "store", None), cache, pipeline, rooms))

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        ip = ws.client.host if ws.client else "unknown"
        client_id: int | None = None
        try:
            hello = await asyncio.wait_for(ws.receive_json(), timeout=HELLO_TIMEOUT_S)
        except WebSocketDisconnect:
            # gone before saying hello: the socket is closed, receiving again
            # would raise "Need to call accept first" — just let it go
            log.info("ws from %s closed before hello", ip)
            return
        except Exception:
            hello = {}
        if not isinstance(hello, dict):
            hello = {}
        role = str(hello.get("role", "remote"))
        # The login session decides what this socket may be, not the page.
        scope = getattr(ws, "scope", None) or {}
        login = scope.get("ktv_auth") or {"role": "admin", "sid": ""}
        if login["role"] == "admin":
            params = getattr(ws, "query_params", None) or {}
            slug = params.get("room") or DEFAULT_ROOM
        else:
            slug = login.get("room") or DEFAULT_ROOM
        state = rooms.get(slug)
        if state is None:
            log.info("ws from %s for unknown room %r", ip, slug)
            await ws.send_json({"type": "error", "detail": "unknown room", "room": slug})
            await ws.close(code=4404)
            return
        allowed = WS_ROLES.get(login["role"], ("remote",))
        if role not in allowed:
            role = allowed[0]
        name = str(hello.get("name", "") or role)
        client_id = state.register(ws, role, name)
        opened = time.monotonic()
        log.info("ws #%d connected: room=%s role=%s name=%r ip=%s (clients=%d)",
                 client_id, slug, role, name, ip, len(state.clients))
        await state.send(client_id, state.snapshot().model_dump(mode="json"))
        if role == "display":
            await state.handle_display_connect(client_id)
        else:
            await state.broadcast_snapshot()

        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                try:
                    await _handle_ws_message(state, client_id, role, msg, login, owners)
                except WebSocketDisconnect:
                    raise
                except Exception as exc:  # a malformed message must not drop the socket
                    mtype = msg.get("type") if isinstance(msg, dict) else type(msg).__name__
                    log.warning("ws #%d bad message type=%r: %s", client_id, mtype, exc)
                    await state.send(client_id, {"type": "error", "detail": f"bad message: {exc}"})
        except WebSocketDisconnect:
            pass
        except RuntimeError as exc:
            # starlette raises RuntimeError once the socket is already closed
            if ws.client_state != WebSocketState.DISCONNECTED and \
                    ws.application_state != WebSocketState.DISCONNECTED:
                raise
            log.debug("ws #%d receive after close: %s", client_id, exc)
        finally:
            if client_id is not None:
                state.unregister(client_id)
                log.info("ws #%d disconnected: room=%s role=%s ip=%s after %.0fs (clients=%d)",
                         client_id, slug, role, ip, time.monotonic() - opened, len(state.clients))
                await state.mic_gone(client_id, role)
                await state.broadcast_snapshot()

    # ------------------------------------------------------------------
    # SPA
    # ------------------------------------------------------------------
    dist: Path = config.WEB_DIST
    if dist.is_dir():

        @app.get("/{full_path:path}")
        async def spa(full_path: str):
            if full_path.startswith(("api/", "media/", "lyrics/", "ws")):
                return JSONResponse({"detail": "not found"}, status_code=404)
            candidate = (dist / full_path).resolve() if full_path else dist
            if full_path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
                return file_response(candidate)
            index = dist / "index.html"
            if index.exists():
                # The build uses relative asset URLs ("./assets/…"); a room
                # page sits deeper (/r/<room>/display), so pin them to the
                # app root or the bundle would load from /r/<room>/assets.
                html = index.read_text(encoding="utf-8")
                html = html.replace('="./', f'="{config.url("/")}')
                return HTMLResponse(html, headers={"Cache-Control": "no-cache"})
            return JSONResponse(
                {"detail": "web build missing — run `npm run build` in web/"}, status_code=503
            )
    else:

        @app.get("/")
        async def no_dist():
            return JSONResponse(
                {"detail": "web build missing — run `npm run build` in web/"}, status_code=503
            )

    return app


async def _handle_ws_message(
    state: KTVState, client_id: int, role: str, msg: dict[str, Any],
    login: dict[str, str] | None = None, owners: dict[str, str] | None = None,
) -> None:
    login = login or {"role": "admin", "sid": ""}
    owners = owners if owners is not None else {}
    mtype = str(msg.get("type", ""))

    if mtype == "hello":
        return

    if mtype == "queue.add":
        track = msg.get("track") or {}
        if not track.get("id"):
            return
        item = Track(**{k: track[k] for k in Track.model_fields if k in track})
        mode_raw = msg.get("karaoke_mode")
        added = await state.add_item(
            item,
            requested_by=str(msg.get("requested_by", "") or role),
            karaoke_mode=KaraokeMode(mode_raw) if mode_raw else None,
            guide_vocal=float(msg.get("guide_vocal", 0.0) or 0.0),
            via=str(msg.get("via", "") or ""),
            client=str(msg.get("cid", "") or ""),
        )
        live = {q.id for q in state.queue} | ({state.current.id} if state.current else set())
        for stale in [k for k in owners if k not in live]:
            owners.pop(stale, None)
        owners[added.id] = login["sid"]
        return

    if mtype == "queue.remove":
        item_id = str(msg.get("id", ""))
        is_current = state.current is not None and state.current.id == item_id
        # removing the current song is a skip, which guests may do anyway
        if login["role"] == "guest" and not is_current and owners.get(item_id) != login["sid"]:
            await state.send(client_id, {"type": "error", "detail": "只能刪除自己點的歌"})
            return
        await state.remove_item(item_id)
        return

    if mtype == "song.report":
        # anyone in the room may flag a song (lyrics wrong, wrong recording,
        # duet not coloured); the admin handles it on /songs
        kind = str(msg.get("kind", ""))
        track_id = str(msg.get("track_id", ""))
        known = [i.track for i in [state.current, *state.queue] if i is not None and i.track.id == track_id]
        track = known[0] if known else state.store.get_track(track_id)
        if role not in ("remote", "host") or track is None or kind not in state.store.REPORT_KINDS:
            await state.send(client_id, {"type": "error", "detail": "回報失敗"})
            return
        state.store.add_report(track, kind, str(msg.get("note", "") or ""),
                               str(msg.get("name", "") or role), state.room)
        log.info("song report: %s on %s (room %s)", kind, track_id, state.room)
        return

    if mtype == "queue.move":
        await state.move_item(str(msg.get("id", "")), int(msg.get("index", 0)))
        return

    if mtype == "queue.bump":
        await state.bump_item(str(msg.get("id", "")))
        return

    if mtype.startswith("playback."):
        action = mtype.split(".", 1)[1]
        # skip (卡歌) 與 play/pause 是客人手勢；seek（倒帶/快轉）仍 host-only
        if role != "host" and not (
            role == "remote" and action in ("skip", "play", "pause")
        ):
            await state.send(client_id, {"type": "error", "detail": "host only"})
            return
        await state.control(action, msg.get("position_ms"))
        return

    if mtype == "karaoke.mode":
        await state.set_mode(msg.get("id"), KaraokeMode(str(msg.get("mode", "ORIGINAL"))))
        return

    if mtype == "karaoke.guide":
        await state.set_guide(msg.get("id"), float(msg.get("guide", 0.0) or 0.0))
        return

    if mtype == "karaoke.keep":
        raw = msg.get("parts")
        try:
            parts = [int(p) for p in raw] if isinstance(raw, list) else []
        except (TypeError, ValueError):
            parts = []
        await state.set_keep_parts(msg.get("id"), parts)
        return

    if mtype == "audio.source":
        if role != "host":
            await state.send(client_id, {"type": "error", "detail": "host only"})
            return
        if not await state.set_audio_source(str(msg.get("value", "auto"))):
            await state.send(client_id, {"type": "error", "detail": "unknown audio source"})
        return

    if mtype == "volume":
        await state.set_volume(float(msg.get("value", 0.9) or 0.0))
        return

    if mtype == "pitch.set":
        # same trust level as `volume` — guests can turn the music down, so
        # they can turn the key too; the host UI is where the control lives
        await state.set_pitch(float(msg.get("value", 0) or 0.0))
        return

    if mtype == "display.visual":
        # same trust level as `pitch.set` — it only changes what the screen shows
        karaoke = msg.get("pv_karaoke")
        lights = msg.get("beat_lights")
        score = msg.get("score")
        score_bar = msg.get("score_bar")
        monitor = msg.get("mic_monitor")
        for name, flag in (("pv_karaoke", karaoke), ("beat_lights", lights), ("score", score),
                           ("score_bar", score_bar), ("mic_monitor", monitor)):
            if flag is not None and not isinstance(flag, bool):
                raise ValueError(f"{name} must be a boolean")
        numbers = {}
        for name in ("score_offset_ms", "mic_volume", "mic_echo"):
            value = msg.get(name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise ValueError(f"{name} must be a number")
            numbers[name] = None if value is None else int(value)
        mode = msg.get("bg_mode")
        ambience = msg.get("ambience")
        fx = msg.get("fx_mode")
        ja = msg.get("ja_assist")
        if not await state.set_visual(
            None if mode is None else str(mode),
            karaoke,
            None if ambience is None else str(ambience),
            lights,
            score,
            score_bar,
            numbers["score_offset_ms"],
            monitor,
            numbers["mic_volume"],
            numbers["mic_echo"],
            fx_mode=None if fx is None else str(fx),
            ja_assist=None if ja is None else str(ja),
        ):
            if fx is not None and str(fx) not in FX_MODES:
                detail = "unknown fx mode"
            elif ja is not None and str(ja) not in JA_ASSISTS:
                detail = "unknown ja assist"
            else:
                detail = "unknown ambience" if mode is None or str(mode) in BG_MODES else "unknown bg mode"
            await state.send(client_id, {"type": "error", "detail": detail})
        return

    if mtype == "display.pitch_capable":
        if role != "display":
            await state.send(client_id, {"type": "error", "detail": "display only"})
            return
        await state.set_display_capability(client_id, bool(msg.get("value", False)))
        return

    if mtype == "mic.signal":
        data = msg.get("data")
        if not isinstance(data, dict) or len(json.dumps(data)) > MIC_SIGNAL_MAX:
            raise ValueError("mic.signal needs a small data object")
        if not await state.relay_mic(client_id, data, msg.get("to")):
            await state.send(client_id, {"type": "mic.signal", "from": None,
                                         "data": {"kind": "error", "reason": "no_display"}})
        return

    if mtype == "lyrics.offset":
        await state.set_lyrics_offset(str(msg.get("track_id", "")), int(msg.get("offset_ms", 0)))
        return

    if mtype == "display.diag":
        # 電視的瀏覽器版本、功能支援、幀率（見 web/src/lib/compat.ts）：只記 log，
        # 智慧電視不在手邊也能從 journalctl 查出是哪個 WebView、卡不卡
        if role != "display":
            return
        detail = {k: v for k, v in msg.items() if k != "type"}
        text = json.dumps(detail, ensure_ascii=False)[:DIAG_MAX]
        diag_log.info("display diag room=%s %s", state.room, text)
        return

    if mtype == "display.hello":
        await state.display_hello(client_id)
        return

    if mtype == "score.result":
        if role != "display":
            await state.send(client_id, {"type": "error", "detail": "display only"})
            return

        def part(name: str) -> float:
            value = msg.get(name, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number")
            return max(0.0, min(1.0, float(value)))

        score = msg.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError("score must be a number")
        await state.record_score(
            str(msg.get("item_id") or ""), max(0, min(100, int(score))),
            part("pitch"), part("rhythm"), part("stability"),
        )
        return

    if mtype == "display.state":
        if role != "display":
            await state.send(client_id, {"type": "error", "detail": "display only"})
            return
        await state.display_state(
            queue_item_id=msg.get("queue_item_id"),
            playing=bool(msg.get("playing", False)),
            position_ms=int(msg.get("position_ms", 0) or 0),
            duration_ms=int(msg.get("duration_ms", 0) or 0),
            ended=bool(msg.get("ended", False)),
        )
        return
