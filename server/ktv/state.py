"""Session / queue / playback state — one of the four state owners.

`/display` owns the real playback clock; this module only stores the coarse
state that phones need, and pushes a fresh snapshot after every mutation so no
client ever guesses the queue.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from fastapi import WebSocket

from . import config
from .models import (
    AIStatus,
    ItemStatus,
    KaraokeMode,
    PlaybackState,
    QueueItem,
    SessionSnapshot,
    Track,
)
from .providers import registry
from .store import Store, TrackCache

if TYPE_CHECKING:
    from .pipeline import Pipeline

log = logging.getLogger(__name__)

SCORE_OFFSET_RANGE = (0, 600)  # ms
MIC_VOLUME_RANGE = (0, 150)  # %
MIC_ECHO_RANGE = (0, 100)
BG_MODES = ("auto", "mv", "pv")
AMBIENCES = ("clean", "room", "concert")  # 包廂音場: 原音 / 包廂 / 演唱會
FX_MODES = ("auto", "full", "lite")  # 畫面效果: 自動 / 完整 / 省電（弱的智慧電視）
JA_ASSISTS = ("both", "kana", "sora", "off")  # 日文歌詞輔助: 假名＋空耳 / 假名 / 空耳 / 關閉


def describe(item: QueueItem) -> str:
    """`title - artist [track id]` for log lines."""
    track = item.track
    return f"{track.title} - {track.artist} [{track.id}]" if track.artist else f"{track.title} [{track.id}]"

MutationHook = Callable[[], Awaitable[None]]


def _mv_url(track_id: str, mv: Path | None) -> str | None:
    """MV url with its mtime: a re-downloaded MV keeps its name, the browser must not reuse the old one."""
    if mv is None:
        return None
    try:
        version = int(mv.stat().st_mtime)
    except OSError:
        version = 0
    return config.url(f"/media/{track_id}/{mv.name}?v={version}")


class Client:
    def __init__(self, ws: WebSocket, role: str, name: str) -> None:
        self.ws = ws
        self.role = role
        self.name = name
        self.pitch_capable = False  # display: AudioWorklet usable here


class KTVState:
    """One room: its queue, playback, look and connected clients."""

    def __init__(self, store: Store, cache: TrackCache) -> None:
        self.room = "main"  # slug, set by ktv.rooms.Rooms
        self.room_name = ""
        self.store = store
        self.cache = cache
        self.queue: list[QueueItem] = []
        self.current: QueueItem | None = None
        self.playback = PlaybackState()
        self.volume = 0.9
        self.pitch_semitones = 0  # session-wide pitch shift, -3..3
        self.audio_source = "auto"  # host-pinned primary acquisition source
        self.bg_mode = "auto"  # display background: auto | mv | pv (文字PV)
        self.pv_karaoke = True  # bottom karaoke lyrics while 文字PV shows
        self.ambience = "room"  # 包廂音場 on the display audio (AMBIENCES)
        self.beat_lights = True  # display glow follows the beat
        self.fx_mode = "auto"  # display effects (FX_MODES): auto = 省電 once the TV drops frames
        self.ja_assist = "both"  # 日文歌：漢字上的假名、歌詞下的中文空耳 (JA_ASSISTS)
        self.score = True  # 評分 (display listens to the PC's mic input)
        self.score_bar = True  # 音高條 while scoring
        self.score_offset_ms = 150  # mic + speaker delay, SCORE_OFFSET_RANGE
        self.mic_monitor = False  # 麥克風外放 (display plays the PC's mic)
        self.mic_volume = 100  # 外放音量 %, MIC_VOLUME_RANGE
        self.mic_echo = 30  # 迴音, MIC_ECHO_RANGE
        self.clients: dict[int, Client] = {}
        self.pipeline: "Pipeline | None" = None
        self.on_mutation: list[MutationHook] = []
        self._next_client_id = 1
        self._loaded_item_id: str | None = None
        self._auto_skip_failed = True

    # -- clients --------------------------------------------------------
    def register(self, ws: WebSocket, role: str, name: str) -> int:
        client_id = self._next_client_id
        self._next_client_id += 1
        self.clients[client_id] = Client(ws, role, name)
        return client_id

    def unregister(self, client_id: int) -> None:
        self.clients.pop(client_id, None)

    async def close_all(self, code: int = 1008) -> None:
        """The room was deleted: hang up on everyone in it."""
        for client_id, client in list(self.clients.items()):
            try:
                await client.ws.close(code=code)
            except Exception:
                pass
            self.unregister(client_id)
        self.queue.clear()
        self.current = None

    def display_clients(self) -> list[Client]:
        return [c for c in self.clients.values() if c.role == "display"]

    def display_pitch_capable(self) -> bool:
        """Any connected display that can actually run the AudioWorklet."""
        return any(c.pitch_capable for c in self.display_clients())

    def user_names(self) -> list[str]:
        names = []
        for client in self.clients.values():
            label = client.name or client.role
            if label not in names:
                names.append(label)
        return names

    async def send(self, client_id: int, message: dict[str, Any]) -> None:
        client = self.clients.get(client_id)
        if client is None:
            return
        try:
            await client.ws.send_json(message)
        except Exception:
            self.unregister(client_id)

    async def send_display(self, message: dict[str, Any]) -> None:
        for client in self.display_clients():
            try:
                await client.ws.send_json(message)
            except Exception:
                pass

    async def broadcast(self, message: dict[str, Any], roles: set[str] | None = None) -> None:
        dead: list[int] = []
        # snapshot: phones may (dis)connect while we await a send below
        for client_id, client in list(self.clients.items()):
            if roles is not None and client.role not in roles:
                continue
            try:
                await client.ws.send_json(message)
            except Exception:
                dead.append(client_id)
        for client_id in dead:
            self.unregister(client_id)

    # -- phone microphones ---------------------------------------------
    def mic_display_id(self) -> int | None:
        """The display that hosts phone mics: the newest one (a reloaded TV)."""
        ids = [cid for cid, c in self.clients.items() if c.role == "display"]
        return max(ids) if ids else None

    async def relay_mic(self, client_id: int, data: dict[str, Any], to: Any = None) -> bool:
        """Pass WebRTC signalling between a phone and the display.

        The audio itself flows phone → TV directly; the server only forwards
        offers / answers / ICE candidates.  Phones always talk to the mic
        display, the display answers one phone by id.  Returns False when
        there is nobody to deliver to.
        """
        sender = self.clients.get(client_id)
        if sender is None:
            return False
        if sender.role == "display":
            target = self.clients.get(to) if isinstance(to, int) else None
            if target is None or target.role == "display":
                return False
            await self.send(to, {"type": "mic.signal", "from": client_id, "data": data})
            return True
        display_id = self.mic_display_id()
        if display_id is None:
            return False
        await self.send(display_id, {
            "type": "mic.signal", "from": client_id, "name": sender.name or sender.role,
            "data": data,
        })
        return True

    async def mic_gone(self, client_id: int, role: str) -> None:
        """A socket closed: hang up whatever mic connections it had."""
        bye = {"type": "mic.signal", "from": client_id, "data": {"kind": "bye"}}
        if role == "display":
            await self.broadcast(bye, roles={"remote", "host"})
        else:
            display_id = self.mic_display_id()
            if display_id is not None:
                await self.send(display_id, bye)

    # -- snapshots ------------------------------------------------------
    def snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(
            room=self.room,
            room_name=self.room_name,
            users=self.user_names(),
            queue=self.queue,
            current=self.current,
            playback=self.playback,
            ai_available=False if self.pipeline is None else self.pipeline.ai_available,
            audio_source=self.audio_source,
            audio_sources=registry.audio_sources(),
            charts_available=registry.charts() is not None,
            pitch_semitones=self.pitch_semitones,
            pitch_capable=self.display_pitch_capable(),
            bg_mode=self.bg_mode,
            pv_karaoke=self.pv_karaoke,
            ambience=self.ambience,
            beat_lights=self.beat_lights,
            fx_mode=self.fx_mode,
            ja_assist=self.ja_assist,
            score=self.score,
            score_bar=self.score_bar,
            score_offset_ms=self.score_offset_ms,
            mic_monitor=self.mic_monitor,
            mic_volume=self.mic_volume,
            mic_echo=self.mic_echo,
        )

    async def broadcast_snapshot(self) -> None:
        await self.broadcast(self.snapshot().model_dump(mode="json"))

    async def _changed(self) -> None:
        for hook in list(self.on_mutation):
            try:
                await hook()
            except Exception:  # never let a hook break the session
                log.warning("mutation hook failed", exc_info=True)
        await self.broadcast_snapshot()

    # -- queue mutations ------------------------------------------------
    async def add_item(
        self,
        track: Track,
        requested_by: str = "guest",
        karaoke_mode: KaraokeMode | None = None,
        guide_vocal: float = 0.0,
        via: str = "",
        client: str = "",
    ) -> QueueItem:
        """`via` (search / youtube / charts) and `client` (the phone's search
        id) only feed the 統計 log."""
        self.store.upsert_track(track)
        saved = self.store.get_track(track.id)
        if saved is not None and saved.lyrics_offset_ms:
            track.lyrics_offset_ms = saved.lyrics_offset_ms
        if karaoke_mode is None:
            # auto (remote sends no mode): AI when the separator is real,
            # otherwise realtime vocal removal — never make guests pick.
            karaoke_mode = (
                KaraokeMode.AI
                if self.pipeline is not None and self.pipeline.ai_available
                else KaraokeMode.INSTANT
            )
        item = QueueItem(
            track=track,
            requested_by=requested_by or "guest",
            karaoke_mode=karaoke_mode,
            guide_vocal=max(0.0, min(1.0, guide_vocal)),
        )
        self.queue.append(item)
        try:
            self.store.log_order(track, item.requested_by, self.room, client, via)
        except Exception:  # stats must never stop a song being queued
            log.warning("usage log failed", exc_info=True)
        log.info("queue add %s by %r mode=%s (queue=%d)", describe(item), item.requested_by,
                 item.karaoke_mode.value, len(self.queue))
        await self._changed()
        return item

    def find_item(self, item_id: str) -> QueueItem | None:
        if self.current is not None and self.current.id == item_id:
            return self.current
        for item in self.queue:
            if item.id == item_id:
                return item
        return None

    async def remove_item(self, item_id: str) -> bool:
        if self.current is not None and self.current.id == item_id:
            await self.play_next(reason="removed")
            return True
        for item in list(self.queue):
            if item.id == item_id:
                self.queue.remove(item)
                log.info("queue remove %s (queue=%d)", describe(item), len(self.queue))
                await self._changed()
                return True
        return False

    async def move_item(self, item_id: str, index: int) -> bool:
        for position, item in enumerate(self.queue):
            if item.id == item_id:
                self.queue.pop(position)
                index = max(0, min(len(self.queue), index))
                self.queue.insert(index, item)
                log.info("queue move %s to #%d", describe(item), index + 1)
                await self._changed()
                return True
        return False

    async def bump_item(self, item_id: str) -> bool:
        for position, item in enumerate(self.queue):
            if item.id == item_id:
                self.queue.pop(position)
                self.queue.insert(0, item)
                log.info("queue bump %s to the top", describe(item))
                await self._changed()
                return True
        return False

    async def set_mode(self, item_id: str | None, mode: KaraokeMode) -> QueueItem | None:
        item = self.find_item(item_id) if item_id else self.current
        if item is None:
            return None
        item.karaoke_mode = mode
        if mode == KaraokeMode.AI and item.ai_status in (AIStatus.NONE, AIStatus.FAILED):
            if self.pipeline is not None and self.pipeline.ai_available:
                item.ai_status = AIStatus.QUEUED
        await self._changed()
        if self.current is not None and item.id == self.current.id:
            await self._send_display_update(item)
        if self.pipeline is not None:
            await self.pipeline.reconcile()
        return item

    async def set_guide(self, item_id: str | None, guide: float) -> QueueItem | None:
        item = self.find_item(item_id) if item_id else self.current
        if item is None:
            return None
        item.guide_vocal = max(0.0, min(1.0, float(guide)))
        await self._changed()
        if self.current is not None and item.id == self.current.id:
            await self._send_display_update(item)
        return item

    async def set_keep_parts(self, item_id: str | None, parts: list[int]) -> QueueItem | None:
        """對唱保留原唱：哪些聲部的句子要放原唱（空 = 全部自己唱）。"""
        item = self.find_item(item_id) if item_id else self.current
        if item is None:
            return None
        item.keep_parts = sorted({int(p) for p in parts if 0 <= int(p) <= 16})
        await self._changed()
        if self.current is not None and item.id == self.current.id:
            await self._send_display_update(item)
        return item

    async def set_volume(self, value: float) -> None:
        self.volume = max(0.0, min(1.0, float(value)))
        await self.broadcast({"type": "display.volume", "value": self.volume})
        await self.broadcast_snapshot()

    async def set_pitch(self, value: float) -> None:
        """Session-wide key shift in semitones (spec §14: like volume, not per item)."""
        self.pitch_semitones = int(max(-3, min(3, float(value))))
        await self.broadcast({"type": "display.pitch", "value": self.pitch_semitones})
        await self.broadcast_snapshot()

    async def set_visual(
        self,
        bg_mode: str | None = None,
        pv_karaoke: bool | None = None,
        ambience: str | None = None,
        beat_lights: bool | None = None,
        score: bool | None = None,
        score_bar: bool | None = None,
        score_offset_ms: int | None = None,
        mic_monitor: bool | None = None,
        mic_volume: int | None = None,
        mic_echo: int | None = None,
        fx_mode: str | None = None,
        ja_assist: str | None = None,
    ) -> bool:
        """Session-wide display look and feel: MV or 文字PV background, whether the
        karaoke lyrics stay on under 文字PV, the 包廂音場, the beat lights and
        評分 (on/off, 音高條, delay), 麥克風外放 (on/off, volume, 迴音) and the
        畫面效果 level, 日文歌的假名／空耳. False = unknown mode, nothing changed."""
        if bg_mode is not None and bg_mode not in BG_MODES:
            return False
        if fx_mode is not None and fx_mode not in FX_MODES:
            return False
        if ja_assist is not None and ja_assist not in JA_ASSISTS:
            return False
        if ja_assist is not None:
            self.ja_assist = ja_assist
        if ambience is not None and ambience not in AMBIENCES:
            return False
        if ambience is not None:
            self.ambience = ambience
        if beat_lights is not None:
            self.beat_lights = bool(beat_lights)
        if fx_mode is not None:
            self.fx_mode = fx_mode
        if bg_mode is not None:
            self.bg_mode = bg_mode
        if pv_karaoke is not None:
            self.pv_karaoke = bool(pv_karaoke)
        if score is not None:
            self.score = bool(score)
        if score_bar is not None:
            self.score_bar = bool(score_bar)
        if score_offset_ms is not None:
            lo, hi = SCORE_OFFSET_RANGE
            self.score_offset_ms = max(lo, min(hi, int(score_offset_ms)))
        if mic_monitor is not None:
            self.mic_monitor = bool(mic_monitor)
        if mic_volume is not None:
            lo, hi = MIC_VOLUME_RANGE
            self.mic_volume = max(lo, min(hi, int(mic_volume)))
        if mic_echo is not None:
            lo, hi = MIC_ECHO_RANGE
            self.mic_echo = max(lo, min(hi, int(mic_echo)))
        await self.broadcast_snapshot()
        return True

    async def record_score(self, item_id: str, score: int, pitch: float, rhythm: float,
                           stability: float) -> dict[str, Any] | None:
        """The display finished scoring the song it is playing: keep it and tell
        the room (phones toast it, the TV's result card shows the old best)."""
        item = self.current
        if item is None or item.id != item_id:
            return None  # a late report for a song that is already gone
        best = self.store.song_scores(item.track.id, limit=1)
        self.store.add_score(item.track, item.requested_by, self.room, score, pitch, rhythm, stability)
        message = {
            "type": "score.posted",
            "item_id": item.id,
            "track_id": item.track.id,
            "title": item.track.title,
            "singer": item.requested_by,
            "score": score,
            "best": best[0] if best else None,
        }
        await self.broadcast(message)
        return message

    async def set_display_capability(self, client_id: int, capable: bool) -> None:
        """The display tells us whether AudioWorklet is usable (secure context).
        Stored per client so several displays aggregate to `any`."""
        client = self.clients.get(client_id)
        if client is None:
            return
        capable = bool(capable)
        if client.pitch_capable == capable:
            return
        before = self.display_pitch_capable()
        client.pitch_capable = capable
        if before != self.display_pitch_capable():
            await self.broadcast_snapshot()

    async def set_audio_source(self, value: str) -> bool:
        """Host switch: pin the primary acquisition source (others stay as
        backups).  False = unknown value, nothing changed."""
        catalog = registry.catalog()
        try:
            if catalog is None:
                if value != "auto":
                    raise ValueError("no catalogue plug-in")
            else:
                catalog.set_active_source(value)
        except Exception as exc:
            log.warning("audio source %r rejected: %s", value, exc)
            return False
        log.info("audio source set to %s", value)
        # one server-wide setting: every room shows the new choice
        peers = self.pipeline.states if self.pipeline is not None else [self]
        for state in peers if self in peers else [self, *peers]:
            state.audio_source = catalog.active_source() if catalog is not None else "auto"
            await state.broadcast_snapshot()
        return True

    async def set_lyrics_offset(self, track_id: str, offset_ms: int) -> None:
        self.store.set_lyrics_offset(track_id, int(offset_ms))
        track = self.store.get_track(track_id)
        if track is None:
            return
        for item in [self.current, *self.queue]:
            if item is not None and item.track.id == track_id:
                item.track.lyrics_offset_ms = int(offset_ms)
        if self.current is not None and self.current.track.id == track_id:
            await self.send_display(
                {
                    "type": "display.update",
                    "item_id": self.current.id,
                    "lyrics_offset_ms": int(offset_ms),
                }
            )
        await self.broadcast_snapshot()

    # -- playback -------------------------------------------------------
    async def control(self, action: str, position_ms: int | None = None) -> None:
        if action == "skip":
            await self.play_next(reason="skipped")
            return
        if action == "play":
            self.playback.playing = True
        elif action == "pause":
            self.playback.playing = False
        elif action == "seek" and position_ms is not None:
            self.playback.position_ms = max(0, int(position_ms))
        self.playback.updated_at = time.time()
        await self.broadcast({"type": "playback.state", **self.playback.model_dump(mode="json")})
        await self.send_display(
            {"type": "display.command", "action": action, "position_ms": position_ms}
        )

    END_OUTCOMES = {"track ended": "finished", "skipped": "skipped", "removed": "skipped",
                    "item failed": "failed", "current failed": "failed"}

    def _log_end(self, item: QueueItem, reason: str) -> None:
        """統計: how the current song ended and how far it got."""
        outcome = self.END_OUTCOMES.get(reason)
        pb = self.playback
        if outcome is None or pb.queue_item_id != item.id:
            return
        played = pb.position_ms
        if pb.playing and pb.updated_at:
            played += int((time.time() - pb.updated_at) * 1000)
        if pb.duration_ms:
            played = pb.duration_ms if outcome == "finished" else min(played, pb.duration_ms)
        try:
            self.store.log_end(item.track, item.requested_by, self.room, outcome, played)
        except Exception:
            log.warning("usage log failed", exc_info=True)

    async def play_next(self, reason: str = "") -> QueueItem | None:
        """Drop the finished/skipped item and promote the next one."""
        was_playing = self.current is not None
        if self.current is not None:
            log.info("end %s (%s)", describe(self.current), reason or "next")
            self._log_end(self.current, reason)
        self._loaded_item_id = None
        self.current = None

        while self.queue:
            item = self.queue.pop(0)
            if item.status == ItemStatus.FAILED and self._auto_skip_failed:
                log.info("skip failed %s", describe(item))
                continue
            self.current = item
            break
        if self.current is not None:
            log.info("up next %s status=%s (queue=%d)", describe(self.current),
                     self.current.status.value, len(self.queue))

        self.playback = PlaybackState(
            queue_item_id=self.current.id if self.current else None,
            track_id=self.current.track.id if self.current else None,
            playing=False,
            position_ms=0,
            duration_ms=self.current.track.duration_ms if self.current else 0,
            updated_at=time.time(),
        )
        if was_playing:
            # 卡歌時下一首可能還在下載（或清單已空）→ 不會馬上 display.load，
            # 先叫電視停掉舊歌，否則舊歌照唱、手機卻顯示已換歌
            await self.send_display({"type": "display.command", "action": "stop"})
        await self._changed()
        if self.current is not None:
            await self._maybe_load_current()
        return self.current

    async def _maybe_load_current(self) -> None:
        item = self.current
        if item is None or self._loaded_item_id == item.id:
            return
        if item.status == ItemStatus.FAILED:
            await self.play_next(reason="item failed")
            return
        if item.status != ItemStatus.READY:
            return  # pipeline will call us again once the item is READY
        item.status = ItemStatus.PLAYING
        self._loaded_item_id = item.id
        log.info("play %s", describe(item))
        self.playback.playing = True
        self.playback.position_ms = 0
        self.playback.queue_item_id = item.id
        self.playback.track_id = item.track.id
        self.playback.updated_at = time.time()
        await self.send_display(self._load_message(item))
        await self.broadcast_snapshot()

    def _load_message(self, item: QueueItem) -> dict[str, Any]:
        source = self.cache.source_path(item.track.id)
        stems = self.cache.stem_paths(item.track.id)
        lyrics = self.cache.lyrics_path(item.track.id)
        mv = self.cache.mv_path(item.track.id)
        if item.track.video_only:
            lyrics = None  # video-only request: never hand lyrics to the display
        return {
            "type": "display.load",
            "item": item.model_dump(mode="json"),
            "media_url": config.url(f"/media/{item.track.id}/{source.name}") if source else None,
            "lyrics_url": config.url(f"/lyrics/{item.track.id}") if lyrics else None,
            "mv_url": _mv_url(item.track.id, mv),
            "stems": (
                {
                    "vocals": config.url(f"/media/{item.track.id}/{stems[0].name}"),
                    "instrumental": config.url(f"/media/{item.track.id}/{stems[1].name}"),
                }
                if stems
                else None
            ),
            "volume": self.volume,
            "pitch": self.pitch_semitones,
            "pitch_url": self._pitch_url(item),
        }

    def _pitch_url(self, item: QueueItem) -> str | None:
        """Reference melody for the score; None = 尚未有評分 (no AI stems yet)."""
        path = self.cache.pitch_path(item.track.id)
        if path is None:
            return None
        # with its mtime: a redone pitch.json keeps its name, the display must fetch it again
        return config.url(f"/media/{item.track.id}/{config.PITCH_FILE}?v={int(path.stat().st_mtime)}")

    async def _send_display_update(self, item: QueueItem) -> None:
        stems = self.cache.stem_paths(item.track.id)
        mv = self.cache.mv_path(item.track.id)
        await self.send_display(
            {
                "type": "display.update",
                "item_id": item.id,
                "mode": item.karaoke_mode.value,
                "guide": item.guide_vocal,
                "keep_parts": item.keep_parts,
                "mv_url": _mv_url(item.track.id, mv),
                "stems": (
                    {
                        "vocals": config.url(f"/media/{item.track.id}/{stems[0].name}"),
                        "instrumental": config.url(f"/media/{item.track.id}/{stems[1].name}"),
                    }
                    if stems
                    else None
                ),
                "pitch_url": self._pitch_url(item),
            }
        )

    # -- called by the display client ----------------------------------
    async def display_hello(self, client_id: int) -> None:
        self._loaded_item_id = None  # (re)load after every (re)connect
        await self.send(
            client_id,
            {"type": "display.prepare", "volume": self.volume, "pitch": self.pitch_semitones},
        )
        if self.current is not None:
            item = self.current
            if item.status == ItemStatus.PLAYING:
                item.status = ItemStatus.READY
            await self._maybe_load_current()

    async def display_state(
        self,
        queue_item_id: str | None,
        playing: bool,
        position_ms: int,
        duration_ms: int = 0,
        ended: bool = False,
    ) -> None:
        if ended:
            # only the song that is actually current may end: a late report
            # (host skipped meanwhile, or a second /display tab) must not
            # skip the song that just started
            if self.current is not None and queue_item_id == self.current.id:
                await self.play_next(reason="track ended")
            return
        if queue_item_id and self.current is not None and queue_item_id != self.current.id:
            return  # stale clock from a track we already replaced
        self.playback.playing = playing
        self.playback.position_ms = max(0, int(position_ms))
        if duration_ms > 0:
            # 聚合來源曲目查不到長度 — display 報的是真實音訊長度，回填給手機進度條
            self.playback.duration_ms = int(duration_ms)
        self.playback.updated_at = time.time()
        # 電視自己就是時鐘，不必把它剛回報的位置再送回去（每秒一次的白工）
        await self.broadcast(
            {"type": "playback.state", **self.playback.model_dump(mode="json")},
            roles={"host", "remote"},
        )

    # -- pipeline callbacks --------------------------------------------
    async def notify_item_ready(self, item: QueueItem) -> None:
        if self.current is not None and item.id == self.current.id:
            await self._maybe_load_current()
        elif self.current is None and item.status == ItemStatus.READY:
            await self.play_next(reason="idle start")

    async def notify_item_failed(self, item: QueueItem) -> None:
        if self.current is not None and item.id == self.current.id:
            await self.play_next(reason="current failed")

    async def handle_display_connect(self, client_id: int) -> None:
        await self.display_hello(client_id)

    async def idle_report(self) -> None:
        await self.broadcast_snapshot()
