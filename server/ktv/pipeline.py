"""Song processing pipeline + job manager.

Rules enforced here:

* `INSTANT READY` always happens *before* AI separation starts.
* Only the next `PREFETCH_DEPTH` queue items are prepared.
* The AI worker runs with `concurrency = 1` and can never block playback.
* While any room has songs waiting in its queue no separation runs at all
  (a running one is paused): on the small VPS it starved the downloads.  AI
  songs play as 伴唱 until their stems exist.
* While every room is idle the worker also separates songs that are already
  cached (backfill); that job pauses while a song plays and is dropped (to be
  redone later) when a queued song needs the worker.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from . import config
from .ai import SeparationCancelled, Separator, SeparationError, get_separator, make_pitch, pitch_available
from .media import probe_duration
from .models import AIStatus, ItemStatus, KaraokeMode, QueueItem, Track
from .providers import (
    AudioProvider,
    AudioRouter,
    LocalLyricsProvider,
    LyricsProvider,
    NetworkLyricsProvider,
    lyrics_format_for,
)
from .state import KTVState
from .store import Store, TrackCache

log = logging.getLogger(__name__)


def _label(track: Track) -> str:
    return f"{track.title} - {track.artist} [{track.id}]" if track.artist else f"{track.title} [{track.id}]"


class Pipeline:
    def __init__(
        self,
        state: KTVState,
        store: Store,
        cache: TrackCache,
        audio: AudioProvider | None = None,
        lyrics: LyricsProvider | None = None,
        lyrics_net: LyricsProvider | None = None,
        separator: Separator | None = None,
    ) -> None:
        # Every room's state; the first one is where the pipeline was built.
        self.states: list[KTVState] = []
        self.store = store
        self.cache = cache
        # Dual source: aggregate <-> YouTube, fallback order depends on the
        # track's own source (see providers/router.py).
        self.audio = audio or AudioRouter(cache)
        self.lyrics = lyrics or LocalLyricsProvider(cache)
        # Network lyrics run *after* READY as a background enrichment so a
        # slow/broken internet can never delay or break playback.
        self.lyrics_net = lyrics_net or NetworkLyricsProvider(cache)
        self.separator = separator or get_separator()
        self.ai_available = self.separator.available

        self._track_tasks: dict[str, asyncio.Task] = {}
        self._lyrics_tasks: dict[str, asyncio.Task] = {}
        self._mv_tasks: dict[str, asyncio.Task] = {}
        # one MV download at a time: they are decoration, and three at once (plus
        # the songs themselves) swamped the 2-core / 1 GB VPS
        self._mv_slot = asyncio.Semaphore(1)
        self._ai_queue: asyncio.Queue[str] = asyncio.Queue()
        self._ai_known: set[str] = set()
        self._ai_active: str | None = None
        self._pitch_tasks: dict[str, asyncio.Task] = {}
        self._worker: asyncio.Task | None = None
        # backfill jobs (cached songs nobody queued) waiting or running
        self._backfill: set[str] = set()
        self._backfill_task: asyncio.Task | None = None
        self._last_busy = time.monotonic()
        # downloads / conversions running at once, across every room
        self._slots = asyncio.Semaphore(config.PREPARE_CONCURRENCY)
        self.attach(state)

    # -- rooms ------------------------------------------------------------
    @property
    def state(self) -> KTVState:
        return self.states[0]

    def attach(self, state: KTVState) -> None:
        """Serve one more room: its queue is prefetched alongside the others."""
        if state in self.states:
            return
        self.states.append(state)
        state.pipeline = self
        state.on_mutation.append(self.reconcile)

    def detach(self, state: KTVState) -> None:
        if state in self.states and len(self.states) > 1:
            self.states.remove(state)
        if self.reconcile in state.on_mutation:
            state.on_mutation.remove(self.reconcile)

    def _owned(self, track_id: str) -> list[tuple[KTVState, QueueItem]]:
        """(room, item) for every queue item, in any room, that is this song."""
        out = []
        for state in self.states:
            if state.current is not None and state.current.track.id == track_id:
                out.append((state, state.current))
            out.extend((state, i) for i in state.queue if i.track.id == track_id)
        return out

    def _playing(self, track_id: str) -> list[KTVState]:
        """Rooms whose current song is this one."""
        return [s for s in self.states if s.current is not None and s.current.track.id == track_id]

    def _state_of(self, item: QueueItem) -> KTVState:
        for state in self.states:
            if item is state.current or item in state.queue:
                return state
        return self.states[0]

    # -- lifecycle ------------------------------------------------------
    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._ai_loop(), name="ai-worker")
        if config.AI_BACKFILL and self.ai_available and (
            self._backfill_task is None or self._backfill_task.done()
        ):
            self._backfill_task = asyncio.create_task(self._backfill_loop(), name="ai-backfill")

    async def stop(self) -> None:
        if self._backfill_task is not None:
            self._backfill_task.cancel()
        self.separator.cancel()
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):
                pass
        for task in list(self._track_tasks.values()):
            task.cancel()
        for task in list(self._lyrics_tasks.values()):
            task.cancel()
        for task in list(self._mv_tasks.values()):
            task.cancel()

    # -- prefetch -------------------------------------------------------
    async def reconcile(self) -> None:
        """Prepare the upcoming window; AI jobs cover the whole queue."""
        # current songs of every room first, so the download slots go to
        # what is about to play, then each room's next few
        window: list[QueueItem] = [s.current for s in self.states if s.current is not None]
        for state in self.states:
            window.extend(state.queue[: config.PREFETCH_DEPTH])
        for item in window:
            self.ensure_track(item)

        # Separation is the long pole and the worker is serial — enqueue the
        # full queue so every auto-AI song is split long before its turn.
        upcoming: list[QueueItem] = []
        for state in self.states:
            if state.current is not None:
                upcoming.append(state.current)
            upcoming.extend(state.queue)
        for item in upcoming:
            if (
                item.karaoke_mode == KaraokeMode.AI
                and item.ai_status in (AIStatus.NONE, AIStatus.FAILED)
                and (item.track.id not in self._ai_known or item.track.id in self._backfill)
            ):
                await self._enqueue_ai(item)
        self._steer_backfill()

    def ensure_track(self, item: QueueItem) -> asyncio.Task | None:
        """Track-level dedupe: one preparation task per song.

        A second queue item for the same song reuses the running task, and that
        task settles *every* queue item pointing at the song when it finishes.
        """
        track_id = item.track.id
        task = self._track_tasks.get(track_id)
        if task is not None and not task.done():
            return task
        if item.status != ItemStatus.PREPARING:
            return None
        task = asyncio.create_task(self._prepare(track_id), name=f"prepare:{track_id}")
        task.add_done_callback(lambda _t: self._steer_backfill())  # downloads count as busy
        self._track_tasks[track_id] = task
        return task

    # -- preparation (INSTANT READY) ------------------------------------
    async def _prepare(self, track_id: str) -> None:
        track = self._track_for(track_id)
        if track is None:
            log.warning("prepare %s: unknown track", track_id)
            await self._settle(track_id, ItemStatus.FAILED, "unknown track")
            return
        started = time.monotonic()
        # The MV is its own download (search + video file) and the slow part:
        # start it alongside the audio instead of after READY, so it is
        # usually on disk by the time the song is played.
        self._spawn_mv_fetch(track)
        try:
            if self.cache.has_source(track_id):
                self._backfill_duration(track)
                await self._progress_track(track_id, "cache hit", 1.0)
                await self._resolve_lyrics(track)
                log.info("ready %s from cache", _label(track))
                await self._settle(track_id, ItemStatus.READY, "cache hit", 1.0)
                self._spawn_lyrics_fetch(track)
                return

            log.info("prepare %s (source=%s)", _label(track), track.source)
            lyrics_task = asyncio.ensure_future(self._resolve_lyrics(track))
            if self._slots.locked():
                await self._progress_track(track_id, "waiting for download", 0.02)
            async with self._slots:
                await self._progress_track(track_id, "acquiring audio", 0.05)
                await self.audio.acquire(track)
            await self._progress_track(track_id, "finalising audio", 0.8)
            await lyrics_task

            self._backfill_duration(track)
            self.store.upsert_track(track)
            self.cache.update_manifest(
                track_id,
                source=track.source,
                source_id=track.source_id,
                title=track.title,
                artist=track.artist,
            )
            manifest = self.cache.read_manifest(track_id) or {}
            via = "/".join(
                str(manifest[k]) for k in ("audio_via", "audio_step", "audio_platform") if manifest.get(k)
            ) or "?"
            log.info("ready %s in %.1fs via %s", _label(track), time.monotonic() - started, via)
            await self._settle(track_id, ItemStatus.READY, "ready", 1.0)
            self._spawn_lyrics_fetch(track)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a broken song must never break the queue
            log.exception("prepare failed %s after %.1fs", _label(track), time.monotonic() - started)
            mv_task = self._mv_tasks.get(track_id)
            if mv_task is not None:
                mv_task.cancel()  # nothing to decorate; stop waiting on it
            await self._settle(track_id, ItemStatus.FAILED, f"failed: {exc}")

    def _track_for(self, track_id: str) -> Track | None:
        saved = self.store.get_track(track_id)
        if saved is not None:
            return saved
        for item in self._items_for(track_id):
            return item.track
        return None

    def _backfill_duration(self, track: Track) -> None:
        """Aggregate search results carry no duration — measure the audio.

        Keeps display progress bars, TXT-lyrics matching and the remote
        duration label honest for tracks whose duration_ms starts at 0.
        """
        if track.duration_ms > 0:
            return
        path = self.cache.source_path(track.id)
        if path is None:
            return
        seconds = probe_duration(path)
        if not seconds or seconds <= 0:
            return
        track.duration_ms = int(seconds * 1000)
        for item in self._items_for(track.id):
            if item.track.duration_ms <= 0:
                item.track.duration_ms = track.duration_ms
        for state in self._playing(track.id):
            state.current.track.duration_ms = track.duration_ms
        try:
            self.store.upsert_track(track)
        except Exception:
            log.warning("saving duration for %s failed", track.id, exc_info=True)

    async def _progress_track(self, track_id: str, message: str, value: float | None) -> None:
        for item in self._items_for(track_id):
            await self._progress(item, message, value)

    async def _settle(
        self, track_id: str, status: ItemStatus, message: str, value: float | None = None
    ) -> None:
        """Apply the outcome to every queue item that points at this song."""
        failed = status == ItemStatus.FAILED
        for _ in range(3):  # items can be added while we are settling
            targets = [
                (state, item) for state, item in self._owned(track_id)
                if item.status == ItemStatus.PREPARING
            ]
            if not targets:
                return
            for state, item in targets:
                item.status = status
                item.progress_message = message
                item.progress_value = value
                await state.broadcast(
                    {
                        "type": "track.progress",
                        "item_id": item.id,
                        "track_id": track_id,
                        "status": item.status.value,
                        "ai_status": item.ai_status.value,
                        "message": message,
                        "value": value,
                    }
                )
                if failed:
                    await state.notify_item_failed(item)
                else:
                    await state.notify_item_ready(item)
            for state in {id(s): s for s, _ in targets}.values():
                await state.broadcast_snapshot()
            await asyncio.sleep(0)

    def _video_only(self, track_id: str) -> bool:
        """True when every queue item for this song was queued as video-only.

        Checked on items (not `track`) because `_track_for` prefers the DB row,
        which carries no video_only flag — the flag lives on the queue request.
        """
        items = self._items_for(track_id)
        return bool(items) and all(item.track.video_only for item in items)

    async def _resolve_lyrics(self, track: Track) -> None:
        if self._video_only(track.id):
            return  # video-only request: the picture carries the song
        try:
            path = await self.lyrics.resolve(track)
        except Exception:
            log.warning("local lyrics failed for %s", _label(track), exc_info=True)
            path = None
        if path is None:
            return
        saved = self.store.get_track(track.id)
        if saved is not None:
            track.lyrics_offset_ms = saved.lyrics_offset_ms
        self.cache.update_manifest(track.id, lyrics_source=lyrics_format_for(path))

    def _spawn_lyrics_fetch(self, track: Track) -> None:
        """Background network lookup — starts only after the song is READY."""
        if not config.LYRICS_NET:
            return
        if self._video_only(track.id):
            return  # no crawl for video-only requests
        if self.cache.lyrics_path(track.id) is not None:
            return  # local / manual lyrics exist (they always win)
        task = self._lyrics_tasks.get(track.id)
        if task is not None and not task.done():
            return
        self._lyrics_tasks[track.id] = asyncio.create_task(
            self._fetch_lyrics(track), name=f"lyrics:{track.id}"
        )

    async def _fetch_lyrics(self, track: Track) -> None:
        try:
            path = await self.lyrics_net.resolve(track)
        except Exception:
            log.warning("network lyrics failed for %s", _label(track), exc_info=True)
            return  # lyrics are an enrichment: failure must never surface
        if path is None:
            log.info("no network lyrics for %s", _label(track))
            return
        log.info("lyrics %s: %s", _label(track), path.name)
        self.cache.update_manifest(track.id, lyrics_source=lyrics_format_for(path))
        for state in self._playing(track.id):
            # the display already started without lyrics — hand them over now
            await state.send_display(
                {"type": "display.reload_lyrics", "url": config.url(f"/lyrics/{track.id}")}
            )

    def _spawn_mv_fetch(self, track: Track) -> None:
        """Background MV download — decoration only, runs alongside the audio."""
        if not config.MV_DOWNLOAD:
            return
        if self.cache.mv_path(track.id) is not None and not self.cache.mv_needs_h264(track.id):
            return  # an MV is already cached (AV1/VP9 ones get an H.264 copy)
        task = self._mv_tasks.get(track.id)
        if task is not None and not task.done():
            return
        self._mv_tasks[track.id] = asyncio.create_task(
            self._fetch_mv(track), name=f"mv:{track.id}"
        )

    async def _fetch_mv(self, track: Track) -> None:
        """Download the video-only MV stream (no timeout: minutes are normal).

        Failure stays silent — playback never waits for an MV.  When the
        download lands mid-song, the display is handed the url right away.
        """
        download = getattr(self.audio, "download_mv", None)
        if download is None:
            return
        try:
            async with self._mv_slot:
                started = time.monotonic()
                path = await asyncio.to_thread(download, track)
        except Exception:
            log.warning("MV download failed for %s", _label(track), exc_info=True)
            return
        if path is None:
            log.info("no MV for %s", _label(track))
        else:
            log.info("MV %s in %.0fs", _label(track), time.monotonic() - started)
        for state in self._playing(track.id):
            await state._send_display_update(state.current)

    async def _progress(
        self,
        item: QueueItem,
        message: str,
        value: float | None,
        status: ItemStatus | None = None,
    ) -> None:
        item.progress_message = message
        item.progress_value = value
        if status is not None:
            item.status = status
        await self._state_of(item).broadcast(
            {
                "type": "track.progress",
                "item_id": item.id,
                "track_id": item.track.id,
                "status": item.status.value,
                "ai_status": item.ai_status.value,
                "message": message,
                "value": value,
            }
        )

    # -- AI worker (concurrency = 1) ------------------------------------
    async def _enqueue_ai(self, item: QueueItem) -> None:
        track_id = item.track.id
        if not self.ai_available:
            return
        if self.cache.stem_paths(track_id) is not None:
            # separated before (or by the backfill): cache hit, no inference
            item.ai_status = AIStatus.READY
            await self._progress(item, "AI ready", item.progress_value)
            if not self.cache.pitch_current(track_id):
                self.ensure_pitch(track_id)  # stems from before scoring (or this pitch.json format) existed
            return
        if track_id in self._backfill:
            # someone queued the song the backfill holds: it is wanted now
            self._backfill.discard(track_id)
            item.ai_status = AIStatus.PROCESSING if self._ai_active == track_id else AIStatus.QUEUED
            if self._ai_active == track_id:
                self._steer_backfill()
            # else: still waiting in the AI queue, now as a regular job
            await self._progress(item, "AI queued", item.progress_value)
            return
        if track_id in self._ai_known:
            return
        self._ai_known.add(track_id)
        item.ai_status = AIStatus.QUEUED
        await self._progress(item, "AI queued", item.progress_value)
        await self._ai_queue.put(track_id)
        if self._ai_active is not None and self._ai_active in self._backfill:
            # a queued song beats the backfill; that one is redone later
            log.info("AI backfill %s gives way to a queued song", self._ai_active)
            self.separator.cancel()

    async def _ai_loop(self) -> None:
        while True:
            track_id = await self._ai_queue.get()
            backfill = track_id in self._backfill
            if backfill and (
                self._ai_queue.qsize() or self._busy() or not self.cache.has_source(track_id)
            ):
                # a queued song or a party showed up while it waited (or the
                # admin deleted / is replacing the song on the song cache page)
                self._backfill.discard(track_id)
                self._ai_known.discard(track_id)
                self._ai_queue.task_done()
                continue
            while self._queue_waiting():
                await asyncio.sleep(5)  # see the module docstring
            self._ai_active = track_id
            self._steer_backfill()
            try:
                await self._separate_track(track_id)
            except asyncio.CancelledError:
                raise
            except SeparationCancelled:
                log.info("AI separation of %s stopped; will retry later", track_id)
                if track_id in self._backfill:
                    self._ai_known.discard(track_id)
                else:
                    # a promoted job got caught by the cancel: run it again
                    await self._ai_queue.put(track_id)
            except Exception as exc:  # AI failures must never crash the queue
                log.warning("AI separation failed for %s", track_id, exc_info=True)
                attempts = int(self.cache.read_manifest(track_id).get("ai_attempts", 0)) + 1
                self.cache.update_manifest(track_id, ai_attempts=attempts, ai_error=str(exc)[:200])
                await self._ai_failure(track_id, str(exc))
            finally:
                if track_id in self._backfill:
                    self._backfill.discard(track_id)
                    self._ai_known.discard(track_id)
                self._ai_active = None
                self._ai_queue.task_done()

    async def _separate_track(self, track_id: str) -> None:
        if self.cache.stem_paths(track_id) is not None:
            log.info("AI stems cached for %s", track_id)
        else:
            await self._run_separator(track_id)
        await self._make_pitch(track_id)
        for item in self._items_for(track_id):
            item.ai_status = AIStatus.READY
            await self._progress(item, "AI ready", 1.0)
            state = self._state_of(item)
            if state.current is not None and item.id == state.current.id:
                await state._send_display_update(item)
        await self._snapshot_all()

    async def _run_separator(self, track_id: str) -> None:
        items = [i for i in self._items_for(track_id)]
        for item in items:
            item.ai_status = AIStatus.PROCESSING
        if items:
            await self._snapshot_all()

        source = self.cache.source_path(track_id)
        if source is None:
            # Wait for INSTANT READY first (the separation input is the source).
            task = self._track_tasks.get(track_id)
            if task is not None:
                await task
            source = self.cache.source_path(track_id)
        if source is None:
            raise SeparationError("source audio unavailable")

        for item in items:
            item.ai_status = AIStatus.PROCESSING
            await self._progress(item, "AI separating", None)

        log.info("AI separating %s%s", track_id, " (backfill)" if track_id in self._backfill else "")
        self._steer_backfill()
        await self.separator.separate(
            self.store.get_track(track_id)
            or Track.from_source("youtube", track_id, title=track_id),
            source,
            self._ai_progress_cb(track_id),
        )
        self.cache.update_manifest(track_id, separator=self.separator.name, ai_error=None)

    def _ai_progress_cb(self, track_id: str):
        async def cb(message: str, value: float | None) -> None:
            for item in self._items_for(track_id):
                await self._progress(item, message, value)

        return cb

    # -- reference melody (scoring) ---------------------------------------
    async def _make_pitch(self, track_id: str) -> bool:
        """pitch.json from the vocals stem; a failure only means no score."""
        stems = self.cache.stem_paths(track_id)
        if stems is None or not pitch_available():
            return False
        if self.cache.pitch_current(track_id):
            return True
        try:
            await make_pitch(stems[0], stems[1])
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("pitch failed for %s: %s", track_id, exc)
            self.cache.update_manifest(track_id, pitch_error=str(exc)[:200])
            return False

    def ensure_pitch(self, track_id: str) -> asyncio.Task:
        """Make pitch.json in the background, then tell rooms playing the song."""
        task = self._pitch_tasks.get(track_id)
        if task is not None and not task.done():
            return task

        async def run() -> None:
            if await self._make_pitch(track_id):
                for state in self._playing(track_id):
                    if state.current is not None:
                        await state._send_display_update(state.current)

        task = asyncio.create_task(run(), name=f"pitch-{track_id}")
        self._pitch_tasks[track_id] = task
        task.add_done_callback(lambda _t: self._pitch_tasks.pop(track_id, None))
        return task

    def pitch_candidate(self) -> str | None:
        """A separated song with no (or an old format) reference melody, never failed."""
        if not pitch_available():
            return None
        for track_id in self.store.track_ids():
            if self.cache.stem_paths(track_id) is None or self.cache.pitch_current(track_id):
                continue
            if self.cache.read_manifest(track_id).get("pitch_error") or track_id in self._pitch_tasks:
                continue
            return track_id
        return None

    # -- AI backfill (cached songs, only while every room is idle) --------
    def _busy(self) -> bool:
        """Anyone singing, a song queued, or a download running."""
        if any(s.current is not None or s.queue for s in self.states):
            return True
        return any(not t.done() for t in self._track_tasks.values())

    def _queue_waiting(self) -> bool:
        """Songs waiting in some room's queue: no separation at all then."""
        return any(s.queue for s in self.states)

    def _steer_backfill(self) -> None:
        """Pause the running separation while songs are queued; a backfill
        job also while anyone is singing or downloading."""
        busy = self._busy()
        if busy:
            self._last_busy = time.monotonic()
        if self._ai_active is None:
            return
        if self._queue_waiting() or (busy and self._ai_active in self._backfill):
            self.separator.pause()
        else:
            self.separator.resume()

    def backfill_candidate(self) -> str | None:
        """Newest cached song without stems that has not failed too often."""
        for track_id in self.store.track_ids():
            if track_id in self._ai_known or not self.cache.has_source(track_id):
                continue
            if self.cache.stem_paths(track_id) is not None:
                continue
            attempts = int(self.cache.read_manifest(track_id).get("ai_attempts", 0) or 0)
            if attempts >= config.AI_MAX_ATTEMPTS:
                continue
            return track_id
        return None

    async def backfill_once(self) -> str | None:
        """Start one backfill job if the KTV has been idle long enough."""
        self._steer_backfill()
        if self._busy() or self._ai_active is not None or self._ai_queue.qsize():
            return None
        if time.monotonic() - self._last_busy < config.AI_IDLE_DELAY_S:
            return None
        pitch = self.pitch_candidate()
        if pitch is not None:
            await self.ensure_pitch(pitch)  # seconds; the separation waits a tick
            return None
        track_id = self.backfill_candidate()
        if track_id is None:
            return None
        self._backfill.add(track_id)
        self._ai_known.add(track_id)
        await self._ai_queue.put(track_id)
        return track_id

    async def _backfill_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self.backfill_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("AI backfill check failed", exc_info=True)

    async def _ai_failure(self, track_id: str, reason: str) -> None:
        for item in self._items_for(track_id):
            item.ai_status = AIStatus.FAILED
            await self._progress(item, f"AI unavailable: {reason}", None)
            if item.karaoke_mode == KaraokeMode.AI:
                # Fallback rule: keep playing, display uses INSTANT DSP.
                item.karaoke_mode = KaraokeMode.INSTANT
            state = self._state_of(item)
            if state.current is not None and item.id == state.current.id:
                await state._send_display_update(item)
        await self._snapshot_all()

    async def _snapshot_all(self) -> None:
        for state in self.states:
            await state.broadcast_snapshot()

    def _items_for(self, track_id: str) -> list[QueueItem]:
        return [item for _, item in self._owned(track_id)]

    # -- song cache page (ktv/library.py) ---------------------------------
    def in_use(self, track_id: str) -> str:
        """Why this song's files must not be swapped right now ('' = free)."""
        if self._owned(track_id):
            return "這首歌在點歌清單裡或正在播，先移掉再改"
        task = self._track_tasks.get(track_id)
        if task is not None and not task.done():
            return "這首歌正在下載，等它好了再改"
        if self._ai_active == track_id and track_id not in self._backfill:
            return "這首歌正在為點歌清單做 AI 分離"
        return ""

    def ai_state(self, track_id: str) -> str:
        """running | queued | '' — the AI worker's view of one song."""
        if self._ai_active == track_id:
            return "running"
        return "queued" if track_id in self._ai_known else ""

    async def release(self, track_id: str) -> None:
        """Stop background work on a cached song before its files change:
        an idle-time separation of it is dropped (redone later on the new
        audio), lyrics / MV lookups are forgotten."""
        if self._ai_active == track_id and track_id in self._backfill:
            self.separator.resume()
            self.separator.cancel()
            for _ in range(100):  # the child is SIGKILLed: this is quick
                if self._ai_active != track_id:
                    break
                await asyncio.sleep(0.1)
        if self._ai_active != track_id:
            self._ai_known.discard(track_id)
        for tasks in (self._lyrics_tasks, self._mv_tasks):
            task = tasks.pop(track_id, None)
            if task is not None and not task.done():
                task.cancel()

    # -- introspection ---------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "ai_available": self.ai_available,
            "ai_active": self._ai_active,
            "ai_pending": self._ai_queue.qsize(),
            "ai_backfill": self._ai_active is not None and self._ai_active in self._backfill,
            "preparing": len([t for t in self._track_tasks.values() if not t.done()]),
        }
