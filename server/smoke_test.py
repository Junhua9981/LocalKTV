"""LocalKTV smoke test — exercises the pipeline rules without a network.

Run:  .venv/Scripts/python server/smoke_test.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

# config reads the environment at import time: the smoke test must never
# reach the aggregate source nor download MVs in background threads.
os.environ.setdefault("KTV_MV", "0")
os.environ.setdefault("KTV_AGG", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ktv import config
from ktv.models import ItemStatus, KaraokeMode, Track
from ktv.pipeline import Pipeline
from ktv.providers import ProviderError
from ktv.state import KTVState
from ktv.store import Store, TrackCache

CACHED_TRACK = Track.from_source(
    "youtube", "YQHsXMglC9A", title="Adele - Hello", artist="Adele", duration_ms=367000
)
MISSING_TRACK = Track.from_source(
    "youtube", "no-such-video-id", title="Missing", artist="Nobody", duration_ms=1000
)
# audio cached, no lyrics — exercises the background network-lyrics enrichment
NET_LYRICS_TRACK = Track.from_source(
    "youtube", "test_net_lyrics", title="測試歌", artist="測試人", duration_ms=180000
)
FAIL_NET_TRACK = Track.from_source(
    "youtube", "test_net_lyrics_failing", title="測試歌2", artist="測試人", duration_ms=180000
)


def ok(condition: bool, label: str) -> None:
    print(f"  [{'OK ' if condition else 'FAIL'}] {label}")
    if not condition:
        raise SystemExit(1)


async def wait_status(state: KTVState, item_id: str, timeout: float = 20) -> "ItemStatus":
    for _ in range(int(timeout * 5)):
        item = state.find_item(item_id)
        if item and item.status in (ItemStatus.READY, ItemStatus.FAILED, ItemStatus.PLAYING):
            return item.status
        await asyncio.sleep(0.2)
    return ItemStatus.PREPARING


class OfflineAudio:
    """Pretends the internet is down (acceptance #15)."""

    async def acquire(self, track: Track) -> Path:
        raise ProviderError("offline: no network")


class FakeNetLyrics:
    """Stands in for the QQ/LRCLIB fetch: writes lyrics, or fails on demand."""

    def __init__(self, cache: TrackCache) -> None:
        self.cache = cache
        self.calls = 0
        self.fail = False

    async def resolve(self, track: Track) -> Path:
        self.calls += 1
        if self.fail:
            raise RuntimeError("network down")
        path = self.cache.dir_for(track.id) / "lyrics.lrc"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[00:01.00]測試歌詞", encoding="utf-8")
        return path


class FakeWS:
    """Minimal WebSocket stand-in so KTVState can broadcast in tests."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


async def wait_lyrics(cache: TrackCache, track_id: str, timeout: float = 5) -> bool:
    for _ in range(int(timeout * 10)):
        if cache.lyrics_path(track_id) is not None:
            return True
        await asyncio.sleep(0.1)
    return False


async def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    config.ensure_dirs()
    config.LYRICS_NET = False  # sections 1-4 must run without touching the network
    config.MV_DOWNLOAD = False  # no video downloads during tests either
    store = Store()
    cache = TrackCache()
    state = KTVState(store, cache)
    pipeline = Pipeline(state, store, cache)
    pipeline.start()

    print("1. cached song reaches READY/PLAYING without downloading")
    first = await state.add_item(CACHED_TRACK, requested_by="t1", karaoke_mode=KaraokeMode.INSTANT)
    status = await wait_status(state, first.id)
    ok(status in (ItemStatus.READY, ItemStatus.PLAYING), f"first item {status.value}")
    ok(cache.has_source(CACHED_TRACK.id), "audio served from cache")

    load_msg = state._load_message(state.current or first)
    ok(
        "mv_url" in load_msg
        and (
            load_msg["mv_url"] is None
            or load_msg["mv_url"].startswith(f"/media/{CACHED_TRACK.id}/")
        ),
        "display.load carries mv_url (None when no MV is cached)",
    )

    print("2. the same song queued twice both become READY (track-level dedupe)")
    second = await state.add_item(CACHED_TRACK, requested_by="t2", karaoke_mode=KaraokeMode.AI)
    status = await wait_status(state, second.id)
    ok(status in (ItemStatus.READY, ItemStatus.PLAYING), f"second item {status.value}")

    print("3. lyrics offset is persisted (acceptance #13)")
    await state.set_lyrics_offset(CACHED_TRACK.id, 750)
    saved = store.get_track(CACHED_TRACK.id)
    ok(saved is not None and saved.lyrics_offset_ms == 750, "lyrics_offset_ms = 750")
    await state.set_lyrics_offset(CACHED_TRACK.id, 0)

    print("4. offline: cached song still READY, uncached song FAILS without killing the queue")
    await pipeline.stop()
    state.on_mutation.clear()  # detach the online pipeline before starting the offline one
    state.queue.clear()
    state.current = None
    offline = Pipeline(state, store, cache, audio=OfflineAudio())
    offline.start()
    cached = await state.add_item(CACHED_TRACK, requested_by="t3")
    ok(await wait_status(state, cached.id) in (ItemStatus.READY, ItemStatus.PLAYING), "cached OK offline")

    missing = await state.add_item(MISSING_TRACK, requested_by="t4")
    ok(await wait_status(state, missing.id) == ItemStatus.FAILED, "uncached FAILS offline")

    alive = await state.add_item(CACHED_TRACK, requested_by="t5")
    ok(await wait_status(state, alive.id) in (ItemStatus.READY, ItemStatus.PLAYING), "queue still alive")
    ok(len(state.queue) + (1 if state.current else 0) >= 3, "session still holds the items")

    await offline.stop()

    print("5. network lyrics enrichment: background after READY, failure is non-fatal")
    state.on_mutation.clear()  # detach the offline pipeline
    state.queue.clear()
    state.current = None
    config.LYRICS_NET = True
    for track in (NET_LYRICS_TRACK, FAIL_NET_TRACK):  # deterministic across re-runs
        directory = cache.dir_for(track.id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "source.wav").write_bytes(b"RIFF0000WAVEfake")
        for stale in directory.glob("lyrics.*"):
            stale.unlink(missing_ok=True)
    fake = FakeNetLyrics(cache)
    net = Pipeline(state, store, cache, lyrics_net=fake)
    net.start()

    first = await state.add_item(NET_LYRICS_TRACK, requested_by="t6")
    ok(
        await wait_status(state, first.id) in (ItemStatus.READY, ItemStatus.PLAYING),
        "READY does not wait for the network fetch",
    )
    ok(await wait_lyrics(cache, NET_LYRICS_TRACK.id), "lyrics appear in the background")
    ok(fake.calls >= 1, "network provider invoked")
    ok(
        cache.read_manifest(NET_LYRICS_TRACK.id).get("lyrics_source") == "lrc",
        "manifest records lyrics_source=lrc",
    )

    fake.fail = True  # the network "dies"
    second = await state.add_item(FAIL_NET_TRACK, requested_by="t7")
    ok(
        await wait_status(state, second.id) in (ItemStatus.READY, ItemStatus.PLAYING),
        "song still READY when the lyrics network fails",
    )
    await asyncio.sleep(0.6)
    ok(cache.lyrics_path(FAIL_NET_TRACK.id) is None, "no lyrics file after the failure")
    third = await state.add_item(NET_LYRICS_TRACK, requested_by="t8")
    ok(
        await wait_status(state, third.id) in (ItemStatus.READY, ItemStatus.PLAYING),
        "queue still alive after the lyrics failure",
    )

    await net.stop()
    for track in (NET_LYRICS_TRACK, FAIL_NET_TRACK):  # leave no fixtures behind
        shutil.rmtree(cache.dir_for(track.id), ignore_errors=True)
    config.LYRICS_NET = False

    print("6. cache search folds 简繁/标点; video_only never shows lyrics")
    state.on_mutation.clear()  # detach the lyrics pipeline
    state.queue.clear()
    state.current = None

    studio = Track.from_source(
        "youtube", "test_search_studio", title="測試查尋", artist="測試歌手"
    )
    live = Track.from_source(
        "youtube", "test_search_live", title="測試查尋 (Live)", artist="測試歌手"
    )
    store.upsert_track(studio)
    store.upsert_track(live)

    def cached_ids(q: str) -> list[str]:
        return [t.id for t in store.search_cached(q)]

    ok(studio.id in cached_ids("測試查尋"), "exact title hit")
    ok(live.id not in cached_ids("測試查尋"), "plain query skips the Live cut")
    ok(live.id in cached_ids("測試查尋 live"), "Live query hits the Live cut")
    ok(
        studio.id in cached_ids("测试查寻 测试歌手"),
        "simplified query matches the traditional title+artist",
    )
    ok(
        studio.id in cached_ids("測試歌手") and live.id in cached_ids("測試歌手"),
        "artist browse lists both cuts",
    )

    # video_only request: cached audio, no network crawl, no lyrics to display
    config.LYRICS_NET = True
    vo = Track.from_source(
        "youtube", "test_video_only", title="測試影片曲", artist="無歌詞",
        duration_ms=60000, video_only=True,
    )
    vo_cached = Track.from_source(
        "youtube", "test_video_only_cached", title="測試影片曲2", artist="無歌詞",
        duration_ms=60000, video_only=True,
    )
    for track, with_lyrics in ((vo, False), (vo_cached, True)):
        directory = cache.dir_for(track.id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "source.wav").write_bytes(b"RIFF0000WAVEfake")
        if with_lyrics:
            (directory / "lyrics.lrc").write_text("[00:01.00]不該顯示", encoding="utf-8")
        else:
            for stale in directory.glob("lyrics.*"):
                stale.unlink(missing_ok=True)

    fake_vo = FakeNetLyrics(cache)
    vo_pipe = Pipeline(state, store, cache, lyrics_net=fake_vo)
    vo_pipe.start()
    first_vo = await state.add_item(vo, requested_by="t10")
    ok(
        await wait_status(state, first_vo.id) in (ItemStatus.READY, ItemStatus.PLAYING),
        "video_only song reaches READY from cache",
    )
    await asyncio.sleep(0.5)
    ok(fake_vo.calls == 0, "video_only never crawls lyrics over the network")
    ok(
        state._load_message(first_vo)["mv_url"] is None,
        "display.load mv_url is None when no MV is cached",
    )

    second_vo = await state.add_item(vo_cached, requested_by="t11")
    ok(
        await wait_status(state, second_vo.id) in (ItemStatus.READY, ItemStatus.PLAYING),
        "video_only song with cached lyrics still READY",
    )
    load = state._load_message(second_vo)
    ok(load["lyrics_url"] is None, "display.load hides lyrics for a video_only song")

    # auto mode: guests never pick — INSTANT until a real separator exists
    auto = await state.add_item(CACHED_TRACK, requested_by="auto")
    ok(
        auto.karaoke_mode == KaraokeMode.INSTANT,
        "queueing without a mode auto-resolves to INSTANT (AI unavailable)",
    )
    ok(
        await wait_status(state, auto.id) in (ItemStatus.READY, ItemStatus.PLAYING),
        "auto-mode item reaches READY",
    )

    await vo_pipe.stop()
    config.LYRICS_NET = False
    for track in (vo, vo_cached):
        shutil.rmtree(cache.dir_for(track.id), ignore_errors=True)

    print("7. audio.source switch: snapshot field + invalid values rejected")
    import fake_catalog

    agg = fake_catalog.FakeCatalog(sources=[("source_b", "音源B")])
    real_catalog = fake_catalog.install(agg)
    ok(await state.set_audio_source("source_b"), "host pins source_b")
    ok(state.audio_source == "source_b", "state carries the pinned source")
    ok(agg.active_source() == "source_b", "provider side applied the pin")
    ok(state.snapshot().audio_source == "source_b",
       "snapshot exposes audio_source to every client")
    ok(not await state.set_audio_source("nope"), "unknown source rejected")
    ok(agg.active_source() == "source_b", "failed switch changed nothing")
    ok(await state.set_audio_source("auto"), "reset back to auto")
    ok(state.snapshot().audio_source == "auto", "snapshot back to auto")
    fake_catalog.restore(real_catalog)
    print("PASS")

    print("8. pitch shift: clamped, broadcast, per-display capability aggregate")
    host_ws = FakeWS()
    host_id = state.register(host_ws, "host", "Host")
    insecure_id = state.register(FakeWS(), "display", "display")

    ok(state.snapshot().pitch_semitones == 0, "default key is 0")
    ok(not state.snapshot().pitch_capable, "capable stays false until a display says otherwise")

    await state.set_pitch(2)
    ok(state.snapshot().pitch_semitones == 2, "set_pitch stored")
    ok(
        any(m.get("type") == "display.pitch" and m.get("value") == 2 for m in host_ws.sent),
        "display.pitch broadcast to every client",
    )
    await state.set_pitch(99)
    ok(state.snapshot().pitch_semitones == 3, "clamped to +3")
    await state.set_pitch(-99)
    ok(state.snapshot().pitch_semitones == -3, "clamped to -3")
    await state.set_pitch(0)
    ok(state.snapshot().pitch_semitones == 0, "reset back to 0")

    await state.set_display_capability(insecure_id, False)
    ok(not state.snapshot().pitch_capable, "insecure display keeps the host control off")
    secure_id = state.register(FakeWS(), "display", "display")
    await state.set_display_capability(secure_id, True)
    ok(state.snapshot().pitch_capable, "any capable display turns it on")
    state.unregister(secure_id)
    ok(not state.snapshot().pitch_capable, "losing the capable display turns it off again")
    await state.set_display_capability(insecure_id, False)
    ok(not state.snapshot().pitch_capable, "repeating the same report changes nothing")

    state.unregister(host_id)
    state.unregister(insecure_id)
    print("PASS")


if __name__ == "__main__":
    asyncio.run(main())
