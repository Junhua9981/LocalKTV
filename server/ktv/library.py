"""Song cache page (admin): every cached song, and the fixes a song may need.

`/songs` in the web app talks to the `/api/library` routes below:

* list every song in data/tracks with its audio / lyrics / MV / AI stems;
* lyrics: fetch again, pick a version from NetEase / QQ / Kugou by hand,
  paste, delete (the lyrics offset uses the existing PATCH /api/tracks);
* audio: download again, or replace it with another search result — the new
  file lands in a staging folder first and only replaces the old one once it
  is complete;  the AI stems of the old audio are dropped with it, so the
  idle-time separation (pipeline backfill) makes new ones;
* MV: fetch again, take a given YouTube video, delete;  title / artist edit;
  delete the whole song.

A song that is playing or queued in any room, or being downloaded, refuses
the file swaps (`Pipeline.in_use`); an idle-time separation of it is stopped
first (`Pipeline.release`).  Slow work (downloads) runs as a background job
whose state the list reports, so a proxy timeout never cuts it off.

Admin only: with the login gate on, a guest or TV gets 403 here (GETs
included — the gate itself only guards non-GET requests).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from . import config
from .lyrics_parse import parse as parse_lyrics
from .lyrics_parse import timing_of
from .models import Track
from .pipeline import Pipeline
from .providers import lyrics_net
from .media import probe_duration
from .providers import registry
from .providers.router import AudioRouter
from .providers.youtube import YouTubeAudioProvider
from .rooms import Rooms
from .store import Store, TrackCache
from .textnorm import fold

log = logging.getLogger(__name__)

STAGING = ".staging"  # data/tracks/.staging/<id>/ — downloads before the swap
LYRICS_FIELDS = ("lyrics_provider", "lyrics_via", "lyrics_timing", "lyrics_duration_ms",
                 "lyrics_source", "lyrics_manual")
AUDIO_FIELDS = ("audio_via", "audio_source", "audio_step", "audio_platform", "audio_song_id",
                "audio_duration_ms")
AI_FIELDS = ("separator", "ai_attempts", "ai_error", "pitch_error")
MANIFEST_KEYS = AUDIO_FIELDS + LYRICS_FIELDS + AI_FIELDS
LYRICS_FORMATS = ("lrc", "qrc", "yrc", "txt")
JOB_KEEP_S = 600  # a finished job stays on the list this long
_YT_ID = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/)([\w-]{11})|^([\w-]{11})$")


class PickRequest(BaseModel):
    source: str
    source_id: str
    duration_ms: int = 0
    alternates: list[str] = []
    # the admin chose this platform entry: no fallback to other platforms,
    # keyword hits or YouTube — fail instead
    strict: bool = False


class AudioRequest(BaseModel):
    pick: PickRequest | None = None  # None = download the same song again
    refetch_lyrics: bool = True


class MetaRequest(BaseModel):
    title: str | None = None
    artist: str | None = None


class LyricsPreviewRequest(BaseModel):
    provider: str
    cand: dict[str, Any]


class LyricsSaveRequest(BaseModel):
    content: str
    format: str = "lrc"
    provider: str = ""  # "" = pasted by hand


class SearchRulesRequest(BaseModel):
    q: str
    pins: list[Track] = []
    hidden: list[str] = []


class ResolveRequest(BaseModel):
    ids: list[int]
    resolved: bool = True


class MvRequest(BaseModel):
    video: str = ""  # YouTube url / id; "" = search again


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


class Library:
    def __init__(self, store: Store, cache: TrackCache, pipeline: Pipeline, rooms: Rooms) -> None:
        self.store = store
        self.cache = cache
        self.pipeline = pipeline
        self.rooms = rooms
        self.jobs: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    # -- listing ------------------------------------------------------------
    def track(self, track_id: str) -> Track:
        track = self.store.get_track(track_id)
        if track is not None:
            return track
        manifest = self.cache.read_manifest(track_id)
        if not manifest and not self.cache.dir_for(track_id).is_dir():
            raise HTTPException(status_code=404, detail="沒有這首歌")
        source, _, source_id = track_id.partition("_")
        return Track(
            id=track_id,
            title=manifest.get("title") or track_id,
            artist=manifest.get("artist") or "",
            source=manifest.get("source") or source or "youtube",
            source_id=manifest.get("source_id") or source_id or track_id,
        )

    def row(self, track_id: str, db: dict[str, Any] | None) -> dict[str, Any]:
        directory = self.cache.dir_for(track_id)
        manifest = self.cache.read_manifest(track_id)
        files = {p.name: _size(p) for p in directory.iterdir() if p.is_file()} if directory.is_dir() else {}
        source = self.cache.source_path(track_id)
        lyrics = self.cache.lyrics_path(track_id)
        mv = self.cache.mv_path(track_id)
        stems = self.cache.stem_paths(track_id)
        attempts = int(manifest.get("ai_attempts") or 0)
        ai = self.pipeline.ai_state(track_id)
        if not ai:
            if stems is not None:
                ai = "ready"
            elif attempts >= config.AI_MAX_ATTEMPTS:
                ai = "failed"
            else:
                ai = "none"
        job = self.jobs.get(track_id)
        if job is not None and job["state"] != "running" and time.time() - job["at"] > JOB_KEEP_S:
            self.jobs.pop(track_id, None)
            job = None
        return {
            "id": track_id,
            "title": (db or {}).get("title") or manifest.get("title") or track_id,
            "artist": (db or {}).get("artist") or manifest.get("artist") or "",
            "source": (db or {}).get("source") or manifest.get("source") or "",
            "source_id": (db or {}).get("source_id") or manifest.get("source_id") or "",
            "duration_ms": int((db or {}).get("duration_ms") or manifest.get("audio_duration_ms") or 0),
            "lyrics_offset_ms": int((db or {}).get("lyrics_offset_ms") or 0),
            "created_at": float((db or {}).get("created_at") or 0)
            or (directory.stat().st_mtime if directory.is_dir() else 0),
            "in_db": db is not None,
            "size": sum(files.values()),
            "audio": source.name if source else "",
            "audio_size": _size(source) if source else 0,
            "lyrics": lyrics.name if lyrics else "",
            "mv": mv.name if mv else "",
            "mv_size": _size(mv) if mv else 0,
            "stems": [p.name for p in stems] if stems else [],
            "stems_size": sum(_size(p) for p in stems) if stems else 0,
            "pitch": self.cache.pitch_path(track_id) is not None,
            "ai": ai,
            "manifest": {k: manifest[k] for k in MANIFEST_KEYS if k in manifest},
            "busy": self.pipeline.in_use(track_id),
            "job": job,
        }

    def listing(self) -> dict[str, Any]:
        rows = {row["id"]: row for row in self.store.rows()}
        ids = list(rows)
        root = self.cache.tracks_dir
        if root.is_dir():  # folders the database does not know (older caches)
            known = {self.cache.dir_for(i).name for i in ids}
            ids += sorted(p.name for p in root.iterdir()
                          if p.is_dir() and p.name != STAGING and p.name not in known)
        reports: dict[str, int] = {}
        for report in self.store.reports():
            if report["resolved_at"] is None:
                reports[report["track_id"]] = reports.get(report["track_id"], 0) + 1
        songs = []
        for track_id in ids:
            db = rows.get(track_id)
            if db is None or self.cache.dir_for(track_id).is_dir():
                songs.append(self.row(track_id, db))
            elif track_id in self.jobs:  # a row whose files are being fetched
                songs.append(self.row(track_id, db))
        for song in songs:
            song["reports"] = reports.get(song["id"], 0)
        return {
            "songs": songs,
            "ai": self.pipeline.status(),
            "ai_max_attempts": config.AI_MAX_ATTEMPTS,
            "lyrics_net": config.LYRICS_NET,
            "lyrics_providers": lyrics_net.PICK_PROVIDERS,
        }

    # -- helpers --------------------------------------------------------------
    def check_free(self, track_id: str) -> None:
        reason = self.pipeline.in_use(track_id)
        job = self.jobs.get(track_id)
        if not reason and job is not None and job["state"] == "running":
            reason = "這首歌還有一個工作在跑，等它結束"
        if reason:
            raise HTTPException(status_code=409, detail=reason)

    def set_manifest(self, track_id: str, drop: tuple[str, ...] = (), **fields: Any) -> None:
        """Remove `drop` keys from the manifest (readers test `key in`), then set `fields`."""
        manifest = self.cache.read_manifest(track_id)
        for key in drop:
            manifest.pop(key, None)
        manifest.update(fields)
        self.cache.dir_for(track_id).mkdir(parents=True, exist_ok=True)
        self.cache.manifest_path(track_id).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    async def lyrics_changed(self, track_id: str) -> None:
        """Rooms playing the song reload its lyrics; every remote refreshes."""
        for room in self.rooms.all():
            if room.current is not None and room.current.track.id == track_id:
                await room.send_display(
                    {"type": "display.reload_lyrics", "url": config.url(f"/lyrics/{track_id}")}
                )
            await room.broadcast_snapshot()

    def write_lyrics(self, track_id: str, fmt: str, content: str, **fields: Any) -> None:
        directory = self.cache.dir_for(track_id)
        directory.mkdir(parents=True, exist_ok=True)
        # exactly one lyrics file per song (lookups prefer some formats)
        for stale in directory.glob("lyrics.*"):
            if stale.is_file():
                stale.unlink(missing_ok=True)
        (directory / f"lyrics.{fmt}").write_text(content, encoding="utf-8")
        self.set_manifest(track_id, drop=LYRICS_FIELDS, lyrics_source=fmt,
                          **{k: v for k, v in fields.items() if v is not None})

    def start_job(self, track_id: str, kind: str, work) -> dict[str, Any]:
        job = {"kind": kind, "state": "running", "message": "", "at": time.time()}
        self.jobs[track_id] = job

        async def run() -> None:
            try:
                job["message"] = await work(job) or ""
                job["state"] = "done"
            except asyncio.CancelledError:
                job["state"], job["message"] = "failed", "中斷"
                raise
            except Exception as exc:  # reported on the page, never fatal
                log.warning("library %s %s failed: %s", kind, track_id, exc)
                job["state"], job["message"] = "failed", str(exc)[:300] or type(exc).__name__
            finally:
                job["at"] = time.time()
                self._tasks.pop(track_id, None)

        self._tasks[track_id] = asyncio.create_task(run(), name=f"library-{kind}:{track_id}")
        return job

    def staging(self) -> TrackCache:
        return TrackCache(self.cache.tracks_dir / STAGING)

    # -- lyrics ---------------------------------------------------------------
    async def refetch_lyrics(self, track_id: str) -> dict[str, Any]:
        """The automatic chain again (after a title fix, a lost answer…);
        the old lyrics stay unless a new answer arrives."""
        track = self.track(track_id)
        manifest = self.cache.read_manifest(track_id)
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(lyrics_net._fetch, track, manifest),
                timeout=config.LYRICS_TIMEOUT_S,
            )
        except TimeoutError:
            raise HTTPException(status_code=504, detail="找歌詞逾時")
        if result is None:
            raise HTTPException(status_code=404, detail="網路上沒找到這首歌的歌詞")
        fmt, content, meta = result
        content = lyrics_net._to_traditional(content)
        self.write_lyrics(
            track_id, fmt, content,
            lyrics_provider=meta.get("provider"), lyrics_via=meta.get("via") or "",
            lyrics_timing=meta.get("timing") or timing_of(fmt, content),
            lyrics_duration_ms=int(meta.get("duration_ms") or 0),
        )
        await self.lyrics_changed(track_id)
        return {"ok": True, "format": fmt, "provider": meta.get("provider"), "timing": meta.get("timing")}

    # -- audio ----------------------------------------------------------------
    async def replace_audio(self, track_id: str, req: AudioRequest) -> dict[str, Any]:
        self.check_free(track_id)
        old = self.track(track_id)
        if req.pick is not None:
            fetch = Track(
                id=track_id, title=old.title, artist=old.artist,
                duration_ms=req.pick.duration_ms, source=req.pick.source,
                source_id=req.pick.source_id, alternates=req.pick.alternates,
            )
        else:
            fetch = old.model_copy(update={"duration_ms": 0})  # the old length may be the wrong one
        manual = bool(self.cache.read_manifest(track_id).get("lyrics_manual"))

        async def work(job: dict[str, Any]) -> str:
            staged = self.staging()
            shutil.rmtree(staged.dir_for(track_id), ignore_errors=True)
            staged.dir_for(track_id).mkdir(parents=True, exist_ok=True)
            try:
                job["message"] = "下載中"
                if req.pick is not None and req.pick.strict:
                    if fetch.source == "youtube":
                        await YouTubeAudioProvider(staged).acquire(fetch)
                        staged.update_manifest(track_id, audio_via="youtube", audio_source="youtube",
                                               audio_step="youtube", audio_platform="", audio_song_id="")
                    else:
                        catalog = registry.catalog()
                        if catalog is None:
                            raise RuntimeError("沒有安裝音源外掛，只能用 YouTube")
                        await asyncio.to_thread(catalog.acquire, fetch,
                                                staged.dir_for(track_id), only_own=True)
                else:
                    await AudioRouter(staged).acquire(fetch)
                new = staged.source_path(track_id)
                if new is None:
                    raise RuntimeError("下載沒有產生音檔")
                await self.pipeline.release(track_id)
                reason = self.pipeline.in_use(track_id)
                if reason:
                    raise RuntimeError(f"下載好了但沒換上：{reason}")
                directory = self.cache.dir_for(track_id)
                directory.mkdir(parents=True, exist_ok=True)
                for stale in [*directory.glob(f"{config.SOURCE_STEM}.*"), *directory.glob("raw.*")]:
                    stale.unlink(missing_ok=True)
                # stems of the old audio are wrong now: the idle-time
                # separation (pipeline backfill) makes new ones
                for name in (*config.STEM_FILES, config.PITCH_FILE):
                    (directory / name).unlink(missing_ok=True)
                    (directory / (name + ".part")).unlink(missing_ok=True)
                shutil.move(str(new), str(directory / new.name))
                audio = {k: v for k, v in staged.read_manifest(track_id).items() if k in AUDIO_FIELDS}
                duration = int(audio.get("audio_duration_ms") or 0) or int(
                    (probe_duration(directory / new.name) or 0) * 1000)
                audio["audio_duration_ms"] = duration
                self.set_manifest(
                    track_id, drop=AUDIO_FIELDS + AI_FIELDS, **audio,
                    source=fetch.source, source_id=fetch.source_id,
                    title=old.title, artist=old.artist,
                )
                if self.store.get_track(track_id) is None:
                    self.store.upsert_track(old)
                self.store.update_track(track_id, source=fetch.source, source_id=fetch.source_id,
                                        duration_ms=duration)
            finally:
                shutil.rmtree(staged.dir_for(track_id), ignore_errors=True)
            via = "/".join(str(audio[k]) for k in ("audio_via", "audio_platform", "audio_song_id")
                           if audio.get(k))
            if not req.refetch_lyrics or manual:
                return f"音檔已換上（{via}）"
            job["message"] = "找歌詞"
            try:
                found = await self.refetch_lyrics(track_id)
                return f"音檔已換上（{via}），歌詞來自 {found['provider']}"
            except HTTPException as exc:
                return f"音檔已換上（{via}），但歌詞沒更新：{exc.detail}"

        return self.start_job(track_id, "audio", work)

    # -- MV -------------------------------------------------------------------
    async def replace_mv(self, track_id: str, video: str) -> dict[str, Any]:
        track = self.track(track_id)
        if video:
            match = _YT_ID.search(video.strip())
            if match is None:
                raise HTTPException(status_code=400, detail="看不懂這個 YouTube 網址")
            track = track.model_copy(update={"source": "youtube", "source_id": match.group(1) or match.group(2)})
        if self.jobs.get(track_id, {}).get("state") == "running":
            raise HTTPException(status_code=409, detail="這首歌還有一個工作在跑，等它結束")

        async def work(job: dict[str, Any]) -> str:
            staged = self.staging()
            shutil.rmtree(staged.dir_for(track_id), ignore_errors=True)
            staged.dir_for(track_id).mkdir(parents=True, exist_ok=True)
            try:
                job["message"] = "下載 MV"
                path = await asyncio.to_thread(YouTubeAudioProvider(staged).download_mv, track)
                if path is None:
                    raise RuntimeError("YouTube 上沒找到或下載失敗")
                directory = self.cache.dir_for(track_id)
                for stale in directory.glob("mv.*"):
                    stale.unlink(missing_ok=True)
                shutil.move(str(path), str(directory / path.name))
            finally:
                shutil.rmtree(staged.dir_for(track_id), ignore_errors=True)
            for room in self.rooms.all():  # a room playing it gets the new MV
                if room.current is not None and room.current.track.id == track_id:
                    await room._send_display_update(room.current)
            return "MV 已更新"

        return self.start_job(track_id, "mv", work)

    # -- delete ---------------------------------------------------------------
    async def delete(self, track_id: str) -> None:
        self.check_free(track_id)
        await self.pipeline.release(track_id)
        self.check_free(track_id)
        directory = self.cache.dir_for(track_id)
        if directory.is_dir() and directory.parent == self.cache.tracks_dir:
            shutil.rmtree(directory)
        self.store.delete_track(track_id)
        self.jobs.pop(track_id, None)
        log.info("library: deleted %s", track_id)


def register(app: FastAPI, library: Library) -> None:
    app.state.library = library
    def admin_only(request: Request) -> None:
        login = request.scope.get("ktv_auth")
        if login is not None and login.get("role") != "admin":
            raise HTTPException(status_code=403, detail="admin only")

    def known(track_id: str) -> str:
        library.track(track_id)  # 404 for an unknown song
        return track_id

    @app.get("/api/library")
    async def library_list(request: Request) -> dict[str, Any]:
        admin_only(request)
        return library.listing()

    # search order fixes: one keyword -> pinned songs first, wrong ones hidden
    @app.get("/api/library/search-rules")
    async def search_rules_list(request: Request, q: str = "") -> list[dict[str, Any]]:
        """Every keyword with rules, or (q=) just that keyword's."""
        admin_only(request)
        rules = library.store.all_search_rules()
        if q:
            rules = [r for r in rules if r["query"] == fold(q)]
        return [{**r, "pins": [t.model_dump(mode="json") for t in r["pins"]]} for r in rules]

    @app.put("/api/library/search-rules")
    async def search_rules_set(req: SearchRulesRequest, request: Request) -> dict[str, Any]:
        admin_only(request)
        query = fold(req.q)
        if not query:
            raise HTTPException(status_code=400, detail="關鍵字是空的")
        pins = [t.model_copy(update={"video_only": False, "lyrics_offset_ms": 0}) for t in req.pins[:20]]
        library.store.set_search_rules(query, req.q.strip()[:80], pins, req.hidden[:200])
        return {"ok": True, "query": query}

    # 統計: what guests order and search for (ktv.store usage_events)
    @app.get("/api/library/stats")
    async def usage_stats(request: Request, days: int = 30, room: str = "") -> dict[str, Any]:
        """days = 0: everything recorded."""
        admin_only(request)
        since = time.time() - max(0, min(days, 3650)) * 86400 if days > 0 else 0
        return library.store.usage_stats(since=since, room=room[:40])

    # guests' "this song is wrong" reports (sent over the WebSocket)
    @app.get("/api/library/reports")
    async def reports_list(request: Request) -> list[dict[str, Any]]:
        """Open reports, and the ones handled in the last 3 days."""
        admin_only(request)
        cached = {s for s in library.store.track_ids()}
        rows = library.store.reports(include_resolved_since=time.time() - 3 * 86400)
        return [{**r, "cached": r["track_id"] in cached or library.cache.has_source(r["track_id"]),
                 "reporters": [n for n in r["reporters"].split("\n") if n],
                 "notes": [n for n in r["notes"].split("\n") if n]} for r in rows]

    @app.post("/api/library/reports/resolve")
    async def reports_resolve(req: ResolveRequest, request: Request) -> dict[str, Any]:
        admin_only(request)
        library.store.resolve_reports(req.ids[:500], req.resolved)
        return {"ok": True}

    @app.patch("/api/library/{track_id}")
    async def library_meta(track_id: str, req: MetaRequest, request: Request) -> dict[str, Any]:
        admin_only(request)
        track = library.track(known(track_id))
        fields = {k: v.strip() for k, v in (("title", req.title), ("artist", req.artist)) if v is not None}
        if "title" in fields and not fields["title"]:
            raise HTTPException(status_code=400, detail="歌名不能空白")
        if library.store.get_track(track_id) is None:
            library.store.upsert_track(track)
        library.store.update_track(track_id, **fields)
        library.cache.update_manifest(track_id, **fields)
        return {"ok": True}

    @app.delete("/api/library/{track_id}")
    async def library_delete(track_id: str, request: Request) -> dict[str, Any]:
        admin_only(request)
        await library.delete(known(track_id))
        return {"ok": True}

    # lyrics
    @app.post("/api/library/{track_id}/lyrics/refetch")
    async def library_lyrics_refetch(track_id: str, request: Request) -> dict[str, Any]:
        admin_only(request)
        return await library.refetch_lyrics(known(track_id))

    @app.get("/api/library/{track_id}/lyrics/search")
    async def library_lyrics_search(track_id: str, provider: str, q: str, request: Request) -> list[dict[str, Any]]:
        admin_only(request)
        if provider not in lyrics_net.PICK_PROVIDERS:
            raise HTTPException(status_code=400, detail="unknown provider")
        if not q.strip():
            return []
        track = library.track(known(track_id))
        measured = int(library.cache.read_manifest(track_id).get("audio_duration_ms") or 0)
        if measured:
            track = track.model_copy(update={"duration_ms": measured})
        try:
            return await asyncio.to_thread(lyrics_net.pick_candidates, provider, q.strip()[:80], track)
        except Exception as exc:
            log.warning("lyrics search %s %r failed", provider, q, exc_info=True)
            raise HTTPException(status_code=502, detail=f"{lyrics_net.PICK_PROVIDERS[provider]} 搜尋失敗：{exc}")

    @app.post("/api/library/{track_id}/lyrics/preview")
    async def library_lyrics_preview(track_id: str, req: LyricsPreviewRequest, request: Request) -> dict[str, Any]:
        admin_only(request)
        track = library.track(known(track_id))
        try:
            got = await asyncio.to_thread(lyrics_net.pick_fetch, req.provider, req.cand)
        except Exception as exc:
            log.warning("lyrics preview %s failed", req.provider, exc_info=True)
            raise HTTPException(status_code=502, detail=f"抓歌詞失敗：{exc}")
        if got is None:
            raise HTTPException(status_code=404, detail="這個版本沒有對時間的歌詞")
        fmt, content, timing = got
        lines = parse_lyrics(fmt, content, track.duration_ms, title=track.title, artist=track.artist)
        return {"format": fmt, "content": content, "timing": timing,
                "lines": [{"start": line.get("start", 0),
                           "text": "".join(w.get("text", "") for w in line.get("words") or [])}
                          for line in lines]}

    @app.post("/api/library/{track_id}/lyrics")
    async def library_lyrics_save(track_id: str, req: LyricsSaveRequest, request: Request) -> dict[str, Any]:
        admin_only(request)
        known(track_id)
        fmt = req.format.lower().lstrip(".") or "lrc"
        if fmt not in LYRICS_FORMATS:
            raise HTTPException(status_code=400, detail="unsupported lyrics format")
        if not req.content.strip():
            raise HTTPException(status_code=400, detail="歌詞是空的")
        timing = timing_of(fmt, req.content)
        if fmt != "txt" and not timing:
            raise HTTPException(status_code=400, detail=f"看不懂這段 {fmt.upper()} 歌詞（沒有時間標記？）")
        library.write_lyrics(
            track_id, fmt, req.content,
            # a hand-picked or pasted version is never replaced automatically
            lyrics_manual=True, lyrics_provider=req.provider or "manual",
            lyrics_via="admin", lyrics_timing=timing or "line",
        )
        await library.lyrics_changed(track_id)
        return {"ok": True, "format": fmt, "timing": timing}

    @app.delete("/api/library/{track_id}/lyrics")
    async def library_lyrics_delete(track_id: str, request: Request) -> dict[str, Any]:
        admin_only(request)
        directory = library.cache.dir_for(known(track_id))
        for stale in directory.glob("lyrics.*"):
            if stale.is_file():
                stale.unlink(missing_ok=True)
        library.set_manifest(track_id, drop=LYRICS_FIELDS)
        await library.lyrics_changed(track_id)
        return {"ok": True}

    # audio / MV / AI stems
    @app.post("/api/library/{track_id}/audio")
    async def library_audio(track_id: str, req: AudioRequest, request: Request) -> dict[str, Any]:
        admin_only(request)
        return await library.replace_audio(known(track_id), req)

    @app.post("/api/library/{track_id}/mv")
    async def library_mv(track_id: str, req: MvRequest, request: Request) -> dict[str, Any]:
        admin_only(request)
        return await library.replace_mv(known(track_id), req.video)

    @app.delete("/api/library/{track_id}/mv")
    async def library_mv_delete(track_id: str, request: Request) -> dict[str, Any]:
        admin_only(request)
        for stale in library.cache.dir_for(known(track_id)).glob("mv.*"):
            stale.unlink(missing_ok=True)
        return {"ok": True}

    @app.delete("/api/library/{track_id}/stems")
    async def library_stems_delete(track_id: str, request: Request) -> dict[str, Any]:
        """Drop the AI accompaniment (and the failure count): the idle-time
        separation makes it again."""
        admin_only(request)
        library.check_free(known(track_id))
        await library.pipeline.release(track_id)
        directory = library.cache.dir_for(track_id)
        for name in (*config.STEM_FILES, config.PITCH_FILE):
            (directory / name).unlink(missing_ok=True)
        library.set_manifest(track_id, drop=AI_FIELDS)
        return {"ok": True}
