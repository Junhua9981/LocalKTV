"""State / WebSocket robustness tests — offline, isolated temp data dir.

Run:  .venv/Scripts/python server/state_test.py

Covers:
  1. a stale `ended` (wrong / missing queue_item_id) never skips the current song
  2. broadcast survives clients (dis)connecting while a send is in flight
  3. malformed WS messages answer `error` instead of raising
  4. only the display may report `display.state`
  5. the MV download starts alongside the audio, not after READY
  6. the 文字PV / MV background choice is session-wide and validated
  7. phone-mic signalling goes phone ↔ newest display only
  8. 卡歌 stops the song on the TV even when the next one isn't ready yet
  9. rooms: own queue / clients / mics, one shared download per song, a
     server-wide cap on concurrent downloads, the room picked per socket
 12. 對唱保留原唱: the phone's keep_parts reach the item and the TV
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

# config reads the environment at import time — never touch the real data/
_TMP = tempfile.mkdtemp(prefix="ktv_state_test_")
os.environ["KTV_DATA_DIR"] = _TMP
os.environ.setdefault("KTV_MV", "0")
os.environ.setdefault("KTV_AGG", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ktv.app import _handle_ws_message  # noqa: E402
from ktv.models import ItemStatus, KaraokeMode, QueueItem, Track  # noqa: E402
from ktv.state import KTVState  # noqa: E402
from ktv.store import Store, TrackCache  # noqa: E402


def ok(condition: bool, label: str) -> None:
    print(f"  [{'OK ' if condition else 'FAIL'}] {label}")
    if not condition:
        raise SystemExit(1)


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


class ChurnWS(FakeWS):
    """Connects a new client mid-send — what a phone joining looks like."""

    def __init__(self, state: KTVState) -> None:
        super().__init__()
        self.state = state

    async def send_json(self, message: dict) -> None:
        await super().send_json(message)
        self.state.register(FakeWS(), "remote", "late-phone")
        await asyncio.sleep(0)


def make_state() -> KTVState:
    return KTVState(Store(Path(_TMP) / "ktv.db"), TrackCache(Path(_TMP) / "tracks"))


def queue_ready(state: KTVState, name: str) -> QueueItem:
    item = QueueItem(track=Track.from_source("test", name, title=name), status=ItemStatus.READY)
    state.queue.append(item)
    return item


async def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("1. stale `ended` never skips the song that just started")
    state = make_state()
    a = queue_ready(state, "a")
    b = queue_ready(state, "b")
    c = queue_ready(state, "c")
    await state.play_next()
    ok(state.current is not None and state.current.id == a.id, "a is current")
    await state.play_next(reason="host skip")  # host skips a -> b
    await state.display_state(a.id, False, 0, ended=True)  # late report for a
    ok(state.current.id == b.id, "late ended for a ignored, b keeps playing")
    await state.display_state(None, False, 0, ended=True)
    ok(state.current.id == b.id, "ended without an item id ignored")
    await state.display_state(b.id, False, 0, ended=True)
    ok(state.current.id == c.id, "ended for the current song advances to c")

    print("2. broadcast survives a client connecting mid-send")
    state = make_state()
    for _ in range(3):
        state.register(ChurnWS(state), "remote", "phone")
    try:
        await state.broadcast_snapshot()
        churned = True
    except RuntimeError as exc:
        churned = False
        print(f"    {exc}")
    ok(churned, "no 'dictionary changed size' error")
    ok(len(state.clients) == 6, "late joiners registered")

    print("3. a malformed WS message answers `error` and keeps the socket open")
    from fastapi import WebSocketDisconnect

    from ktv.app import create_app
    from ktv.pipeline import Pipeline

    class ScriptedWS(FakeWS):
        """Feeds the /ws endpoint a fixed script, then disconnects."""

        client = None
        scope: dict = {}
        query_params: dict = {}

        def __init__(self, hello: dict, script: list[str]) -> None:
            super().__init__()
            self.hello = hello
            self.script = list(script)

        async def accept(self) -> None:
            pass

        async def receive_json(self) -> dict:
            return self.hello

        async def receive_text(self) -> str:
            if not self.script:
                raise WebSocketDisconnect(1000)
            return self.script.pop(0)

    state = make_state()
    pipeline = Pipeline(state, state.store, state.cache)
    app = create_app(state, pipeline, state.cache)
    endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/ws")
    sock = ScriptedWS(
        {"type": "hello", "role": "host", "name": "h"},
        [
            '{"type": "karaoke.mode", "mode": "NOPE"}',
            '{"type": "queue.move", "id": "qi_x", "index": "abc"}',
            '{"type": "volume", "value": "loud"}',
            '{"type": "pitch.set", "value": 2}',
        ],
    )
    await endpoint(sock)
    errors = [m for m in sock.sent if m.get("type") == "error"]
    ok(len(errors) == 3, f"3 bad messages -> 3 error replies ({len(errors)})")
    ok(all("bad message" in m["detail"] for m in errors), "error detail says bad message")
    ok(any(m.get("type") == "display.pitch" for m in sock.sent), "later valid message still handled")
    ok(state.pitch_semitones == 2, "pitch applied after the bad messages")

    print("4. only the display may report display.state")
    state = make_state()
    item = queue_ready(state, "y")
    queue_ready(state, "z")
    await state.play_next()
    ws = FakeWS()
    cid = state.register(ws, "remote", "guest")
    await _handle_ws_message(
        state, cid, "remote",
        {"type": "display.state", "queue_item_id": item.id, "ended": True},
    )
    ok(state.current.id == item.id, "remote cannot end the song")
    ok(ws.sent and ws.sent[-1].get("detail") == "display only", "remote told 'display only'")
    dcid = state.register(FakeWS(), "display", "display")
    await _handle_ws_message(
        state, dcid, "display",
        {"type": "display.state", "queue_item_id": item.id, "ended": True},
    )
    ok(state.current.id != item.id, "display can end the song")

    print("5. the MV download starts alongside the audio, not after READY")
    from ktv import config
    from ktv.pipeline import Pipeline

    config.MV_DOWNLOAD = True
    config.LYRICS_NET = False

    class SlowAudio:
        """Audio held back until released; records when the MV download began."""

        def __init__(self, cache: TrackCache) -> None:
            self.cache = cache
            self.release = asyncio.Event()
            self.mv_started_before_audio = False

        async def acquire(self, track: Track) -> Path:
            await self.release.wait()
            path = self.cache.dir_for(track.id) / "source.wav"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"RIFF")
            return path

        def download_mv(self, track: Track) -> Path | None:
            self.mv_started_before_audio = not self.cache.has_source(track.id)
            return None

    state = make_state()
    audio = SlowAudio(state.cache)
    pipeline = Pipeline(state, state.store, state.cache, audio=audio)
    item = await state.add_item(Track.from_source("test", "mv", title="mv", duration_ms=1000))
    for _ in range(50):  # the MV thread runs while the audio is still pending
        if audio.mv_started_before_audio:
            break
        await asyncio.sleep(0.02)
    ok(audio.mv_started_before_audio, "MV download began before the audio finished")
    audio.release.set()
    for _ in range(50):
        if state.find_item(item.id).status != ItemStatus.PREPARING:
            break
        await asyncio.sleep(0.02)
    ok(state.find_item(item.id).status in (ItemStatus.READY, ItemStatus.PLAYING), "song still becomes READY")

    print("6. background mode (MV / 文字PV) and the karaoke-lyrics switch")
    state = make_state()
    snap = state.snapshot()
    ok(snap.bg_mode == "auto" and snap.pv_karaoke is True, "defaults: auto, karaoke lyrics on")
    ws = FakeWS()
    cid = state.register(ws, "remote", "guest")
    await _handle_ws_message(state, cid, "remote", {"type": "display.visual", "bg_mode": "pv"})
    ok(state.bg_mode == "pv", "a phone can switch to 文字PV")
    ok(any(m.get("bg_mode") == "pv" for m in ws.sent if m.get("type") == "session.snapshot"),
       "snapshot carries the new mode")
    await _handle_ws_message(state, cid, "remote", {"type": "display.visual", "pv_karaoke": False})
    ok(state.pv_karaoke is False and state.bg_mode == "pv", "karaoke switch alone keeps the mode")
    await _handle_ws_message(state, cid, "remote", {"type": "display.visual", "bg_mode": "nope"})
    ok(state.bg_mode == "pv", "unknown mode ignored")
    ok(ws.sent[-1].get("detail") == "unknown bg mode", "sender told 'unknown bg mode'")
    try:
        await _handle_ws_message(state, cid, "remote", {"type": "display.visual", "pv_karaoke": "no"})
        raised = False
    except ValueError:
        raised = True  # the /ws loop turns this into a `bad message` error
    ok(raised and state.pv_karaoke is False, "non-boolean karaoke switch rejected")

    ok(state.snapshot().ja_assist == "both", "default: 日文歌 假名＋空耳")
    await _handle_ws_message(state, cid, "remote", {"type": "display.visual", "ja_assist": "sora"})
    ok(state.ja_assist == "sora" and state.snapshot().ja_assist == "sora", "a phone can pick 只要空耳")
    await _handle_ws_message(state, cid, "remote", {"type": "display.visual", "ja_assist": "romaji"})
    ok(state.ja_assist == "sora" and ws.sent[-1].get("detail") == "unknown ja assist", "unknown 日文輔助 rejected")

    ok(state.ambience == "room" and state.beat_lights is True, "defaults: 包廂音場, beat lights on")
    await _handle_ws_message(state, cid, "remote", {"type": "display.visual", "ambience": "concert"})
    ok(state.ambience == "concert" and state.bg_mode == "pv", "a phone can pick 演唱會 alone")
    ok(state.snapshot().ambience == "concert", "snapshot carries the ambience")
    await _handle_ws_message(state, cid, "remote", {"type": "display.visual", "ambience": "disco"})
    ok(state.ambience == "concert", "unknown ambience ignored")
    await _handle_ws_message(state, cid, "remote", {"type": "display.visual", "beat_lights": False})
    ok(state.beat_lights is False and state.ambience == "concert", "lights switch alone keeps the rest")
    ok(state.fx_mode == "auto", "default: 畫面效果 自動")
    await _handle_ws_message(state, cid, "remote", {"type": "display.visual", "fx_mode": "lite"})
    ok(state.fx_mode == "lite" and state.snapshot().fx_mode == "lite" and state.beat_lights is False,
       "省電 alone keeps the rest, snapshot carries it")
    await _handle_ws_message(state, cid, "remote", {"type": "display.visual", "fx_mode": "turbo"})
    ok(state.fx_mode == "lite", "unknown fx mode ignored")

    ok(state.score and state.score_bar and state.score_offset_ms == 150, "defaults: 評分 on, 音高條 on, 150ms")
    await _handle_ws_message(state, cid, "remote", {"type": "display.visual", "score_bar": False})
    ok(not state.score_bar and state.score and state.ambience == "concert", "音高條 switch alone keeps the rest")
    await _handle_ws_message(state, cid, "remote", {"type": "display.visual", "score_offset_ms": 9999})
    ok(state.snapshot().score_offset_ms == 600, "評分延遲 clamped to 600ms, in the snapshot")

    ok(not state.mic_monitor and state.mic_volume == 100 and state.mic_echo == 30,
       "defaults: 麥克風外放 off, 100%, 迴音 30")
    await _handle_ws_message(state, cid, "remote", {"type": "display.visual", "mic_monitor": True, "mic_echo": 250})
    snap = state.snapshot()
    ok(snap.mic_monitor and snap.mic_echo == 100 and snap.mic_volume == 100 and state.score_offset_ms == 600,
       "外放 on, 迴音 clamped to 100, the rest kept")
    await _handle_ws_message(state, cid, "remote", {"type": "display.visual", "mic_volume": -5})
    ok(state.mic_volume == 0 and state.mic_monitor, "外放音量 clamped to 0")
    try:
        await _handle_ws_message(state, cid, "remote", {"type": "display.visual", "mic_echo": "lots"})
        raised = False
    except ValueError:
        raised = True
    ok(raised and state.mic_echo == 100, "non-number 迴音 rejected")
    try:
        await _handle_ws_message(state, cid, "remote", {"type": "display.visual", "score": "yes"})
        raised = False
    except ValueError:
        raised = True
    ok(raised and state.score, "non-boolean score switch rejected")

    print("7. phone-mic signalling: phone ↔ newest display, nothing else")
    state = make_state()
    phone_ws = FakeWS()
    phone = state.register(phone_ws, "remote", "小明")
    await _handle_ws_message(state, phone, "remote", {"type": "mic.signal", "data": {"kind": "offer"}})
    ok(phone_ws.sent[-1]["data"].get("reason") == "no_display", "no TV → phone told no_display")
    old_tv_ws, tv_ws, other_ws = FakeWS(), FakeWS(), FakeWS()
    state.register(old_tv_ws, "display", "display")
    tv = state.register(tv_ws, "display", "display")
    other = state.register(other_ws, "remote", "阿華")
    await _handle_ws_message(state, phone, "remote", {"type": "mic.signal", "data": {"kind": "offer", "sdp": "x"}})
    got = tv_ws.sent[-1]
    ok(got["type"] == "mic.signal" and got["from"] == phone and got["name"] == "小明"
       and got["data"]["sdp"] == "x", "offer reaches the newest display with the singer's name")
    ok(not old_tv_ws.sent and not other_ws.sent, "older display and other phones see nothing")
    await _handle_ws_message(state, tv, "display", {"type": "mic.signal", "to": phone, "data": {"kind": "answer"}})
    ok(phone_ws.sent[-1] == {"type": "mic.signal", "from": tv, "data": {"kind": "answer"}},
       "display answers that one phone")
    ok(not other_ws.sent, "the answer is not broadcast")
    before = len(tv_ws.sent)
    await _handle_ws_message(state, tv, "display", {"type": "mic.signal", "to": tv, "data": {"kind": "answer"}})
    ok(len(tv_ws.sent) == before + 1 and tv_ws.sent[-1]["data"]["reason"] == "no_display",
       "a display cannot signal another display")
    for bad in ({"type": "mic.signal", "data": "x"}, {"type": "mic.signal", "data": {"sdp": "a" * 20000}}):
        try:
            await _handle_ws_message(state, phone, "remote", bad)
            raised = False
        except ValueError:
            raised = True
        ok(raised, "non-object / oversized signal rejected")
    state.unregister(phone)
    await state.mic_gone(phone, "remote")
    ok(tv_ws.sent[-1] == {"type": "mic.signal", "from": phone, "data": {"kind": "bye"}},
       "phone leaving hangs up on the display")
    state.unregister(tv)
    await state.mic_gone(tv, "display")
    ok(other_ws.sent[-1]["data"] == {"kind": "bye"} and other_ws.sent[-1]["from"] == tv,
       "display leaving hangs up on the phones")

    print("8. 卡歌 stops the TV song even when the next one is still downloading")
    state = make_state()
    tv_ws = FakeWS()
    tv = state.register(tv_ws, "display", "display")
    phone = state.register(FakeWS(), "remote", "小明")
    a = queue_ready(state, "a")
    b = QueueItem(track=Track.from_source("test", "b", title="b"), status=ItemStatus.PREPARING)
    state.queue.append(b)
    await state.play_next()
    def loads() -> list[str]:
        return [m["item"]["id"] for m in tv_ws.sent if m.get("type") == "display.load"]

    ok(loads() == [a.id], "a loads on the TV")
    tv_ws.sent.clear()
    await _handle_ws_message(state, phone, "remote", {"type": "playback.skip"})
    ok(state.current is not None and state.current.id == b.id, "skip moves on to b")
    ok({"type": "display.command", "action": "stop"} in tv_ws.sent, "TV told to stop a while b downloads")
    tv_ws.sent.clear()
    await _handle_ws_message(state, phone, "remote", {"type": "playback.skip"})
    ok(state.current is None, "skipping the last song empties the stage")
    ok({"type": "display.command", "action": "stop"} in tv_ws.sent, "TV told to stop with nothing queued")
    c = queue_ready(state, "c")
    await state.notify_item_ready(c)
    ok(loads() == [c.id], "a newly ready song still loads after the stop")

    print("9. rooms share the pipeline but nothing else")
    from ktv.rooms import Rooms

    config.MV_DOWNLOAD = False
    config.PREPARE_CONCURRENCY = 1

    class CountingAudio:
        """Every acquire waits for `release`; counts calls and the peak in flight."""

        def __init__(self, cache: TrackCache) -> None:
            self.cache = cache
            self.release = asyncio.Event()
            self.calls: list[str] = []
            self.running = self.peak = 0

        async def acquire(self, track: Track) -> Path:
            self.calls.append(track.id)
            self.running += 1
            self.peak = max(self.peak, self.running)
            await self.release.wait()
            self.running -= 1
            path = self.cache.dir_for(track.id) / "source.wav"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"RIFF")
            return path

    store = Store(Path(_TMP) / "rooms.db")
    cache = TrackCache(Path(_TMP) / "rooms-tracks")
    rooms = Rooms(Path(_TMP) / "rooms.db", store, cache)
    ok(rooms.create("vip", "包廂") == "", "room created")
    ok(rooms.create("vip", "again") != "" and rooms.create("Bad!", "x") != "", "duplicate / bad slug refused")
    audio = CountingAudio(cache)
    pipeline = Pipeline(rooms.main, store, cache, audio=audio)
    rooms.attach_pipeline(pipeline)
    main, vip = rooms.main, rooms.get("vip")
    ok(pipeline.states == [main, vip], "pipeline serves both rooms")
    main_tv, vip_tv, vip_phone = FakeWS(), FakeWS(), FakeWS()
    main.register(main_tv, "display", "display")
    vip.register(vip_tv, "display", "display")
    phone = vip.register(vip_phone, "remote", "小美")
    song = Track.from_source("test", "shared", title="同一首", duration_ms=1000)
    a = await main.add_item(song)
    b = await vip.add_item(Track.from_source("test", "shared", title="同一首", duration_ms=1000))
    other = await vip.add_item(Track.from_source("test", "other", title="另一首", duration_ms=1000))
    await asyncio.sleep(0.05)
    ok(audio.calls == ["test_shared"], f"one download for the song both rooms queued ({audio.calls})")
    ok(audio.peak == 1, "download cap: the second song waits its turn")
    ok(len(main.queue) == 1 and len(vip.queue) == 2, "queues stay separate")
    audio.release.set()
    for _ in range(100):
        if main.current is not None and vip.current is not None and "test_other" in audio.calls:
            break
        await asyncio.sleep(0.02)
    ok(main.current is not None and main.current.id == a.id, "main plays its copy")
    ok(vip.current is not None and vip.current.id == b.id, "vip plays its copy too")
    ok(any(m.get("type") == "display.load" and m["item"]["id"] == a.id for m in main_tv.sent)
       and not any(m.get("type") == "display.load" and m["item"]["id"] == a.id for m in vip_tv.sent),
       "each TV only loads its own room's song")
    ok("test_other" in audio.calls, "the waiting download starts once a slot frees")
    snap = vip.snapshot()
    ok(snap.room == "vip" and snap.room_name == "包廂" and len(snap.queue) == 1, "snapshot names its room")
    await _handle_ws_message(vip, phone, "remote", {"type": "mic.signal", "data": {"kind": "offer"}})
    ok(vip_tv.sent[-1]["type"] == "mic.signal" and not any(m.get("type") == "mic.signal" for m in main_tv.sent),
       "a phone mic only reaches its own room's TV")
    await _handle_ws_message(vip, phone, "remote", {"type": "volume", "value": 0.3})
    ok(vip.volume == 0.3 and main.volume == 0.9, "volume is per room")
    await rooms.delete("vip")
    ok(rooms.get("vip") is None and pipeline.states == [main], "deleted room leaves the pipeline")
    ok(not await rooms.delete("main"), "main cannot be deleted")

    print("10. the /ws endpoint puts each socket in the room it asked for")
    rooms = Rooms(None, store, cache)
    rooms.create("b", "B")
    pipeline = Pipeline(rooms.main, store, cache, audio=CountingAudio(cache))
    rooms.attach_pipeline(pipeline)
    app = create_app(rooms.main, pipeline, cache, rooms=rooms)
    endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/ws")
    sock = ScriptedWS({"type": "hello", "role": "remote", "name": "p"}, ['{"type": "pitch.set", "value": 1}'])
    sock.query_params = {"room": "b"}
    await endpoint(sock)
    ok(rooms.get("b").pitch_semitones == 1 and rooms.main.pitch_semitones == 0, "?room=b drives room b")
    ok(sock.sent[0].get("room") == "b", "first snapshot says room b")
    guest = ScriptedWS({"type": "hello", "role": "remote"}, ['{"type": "pitch.set", "value": -1}'])
    guest.query_params = {"room": "b"}
    guest.scope = {"ktv_auth": {"role": "guest", "sid": "g", "room": "main"}}
    await endpoint(guest)
    ok(rooms.main.pitch_semitones == -1 and rooms.get("b").pitch_semitones == 1,
       "a guest stays in its login's room whatever it asks for")

    class ClosingWS(ScriptedWS):
        closed = None

        async def close(self, code: int = 1000) -> None:
            self.closed = code

    lost = ClosingWS({"type": "hello"}, [])
    lost.query_params = {"room": "nope"}
    await endpoint(lost)
    ok(lost.closed == 4404 and lost.sent[-1]["detail"] == "unknown room", "unknown room is refused")

    print("11. 評分: the display reports, the room hears, 本曲高分 keeps the best")
    state = make_state()
    tv_ws, phone_ws = FakeWS(), FakeWS()
    tv = state.register(tv_ws, "display", "display")
    phone = state.register(phone_ws, "remote", "小明")
    item = QueueItem(track=Track.from_source("test", "scored", title="唱歌"), status=ItemStatus.READY,
                     requested_by="小明")
    state.current = item
    report = {"type": "score.result", "item_id": item.id, "score": 88, "pitch": 0.9, "rhythm": 0.8,
              "stability": 2}
    await _handle_ws_message(state, phone, "remote", report)
    ok(phone_ws.sent[-1].get("detail") == "display only" and not state.store.song_scores(item.track.id),
       "only the display may report a score")
    await _handle_ws_message(state, tv, "display", report)
    posted = phone_ws.sent[-1]
    ok(posted["type"] == "score.posted" and posted["singer"] == "小明" and posted["best"] is None,
       "the room hears it; first score has no old best")
    await _handle_ws_message(state, tv, "display", {**report, "score": 95})
    ok(phone_ws.sent[-1]["best"]["score"] == 88, "second score sees the old best")
    rows = state.store.song_scores(item.track.id)
    ok([r["score"] for r in rows] == [95, 88], "本曲高分 best first")
    await _handle_ws_message(state, tv, "display", {**report, "item_id": "gone"})
    ok(len(state.store.song_scores(item.track.id)) == 2, "a report for another song is dropped")

    print("12. 對唱保留原唱: keep_parts per item, sent to the TV, junk ignored")
    state = make_state()
    tv_ws, phone_ws = FakeWS(), FakeWS()
    tv = state.register(tv_ws, "display", "display")
    phone = state.register(phone_ws, "remote", "小明")
    item = queue_ready(state, "duet")
    await state.play_next()
    await _handle_ws_message(state, phone, "remote", {"type": "karaoke.keep", "parts": [2, 0, 2]})
    ok(state.current.keep_parts == [0, 2], "a phone picks 女聲 + 合唱 (deduped, sorted)")
    update = [m for m in tv_ws.sent if m.get("type") == "display.update"][-1]
    ok(update["keep_parts"] == [0, 2] and update["item_id"] == item.id, "the TV is told")
    await _handle_ws_message(state, phone, "remote", {"type": "karaoke.keep", "parts": "x"})
    ok(state.current.keep_parts == [], "a bad payload clears instead of raising")
    await _handle_ws_message(state, phone, "remote", {"type": "karaoke.keep", "parts": ["a"]})
    ok(state.current.keep_parts == [], "non-numbers ignored")

    print("PASS")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
