"""AI separation tests — offline, isolated temp data dir.

Run:  .venv/Scripts/python server/ai_test.py [--model models/UVR_MDXNET_KARA_2.dyn.onnx]

Covers (fake separator):
  1. a song separated before is a cache hit: READY without inference
  2. backfill waits until every room has been idle for KTV_AI_IDLE_DELAY
  3. a backfill job pauses while a song plays and resumes when idle again
  4. a queued song beats the backfill job, which is redone later
  5. queueing the song the backfill is working on keeps that job going
  6. failed songs are counted and skipped after KTV_AI_MAX_ATTEMPTS
  7. separated songs without a reference melody (or with an old format one)
     get pitch.json when idle,
     and the display is told where it is (needs numpy + ffmpeg)
With --model (needs onnxruntime + ffmpeg): the real separator on a 12 s clip,
output length and peak memory of the child process.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ktv_ai_test_")
os.environ["KTV_DATA_DIR"] = _TMP
os.environ["KTV_MV"] = "0"
os.environ["KTV_AGG"] = "0"
os.environ["KTV_LYRICS_NET"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ktv import config  # noqa: E402
from ktv.ai import SeparationCancelled, SeparationError, pitch_available  # noqa: E402
from ktv.models import AIStatus, ItemStatus, KaraokeMode, Track  # noqa: E402
from ktv.pipeline import Pipeline  # noqa: E402
from ktv.state import KTVState  # noqa: E402
from ktv.store import Store, TrackCache  # noqa: E402
from pitch_test import MELODY, voice, write_wav  # noqa: E402


def ok(condition: bool, label: str) -> None:
    print(f"  [{'OK ' if condition else 'FAIL'}] {label}")
    if not condition:
        raise SystemExit(1)


class FakeSeparator:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.paused = False
        self.release = asyncio.Event()
        self.fail = False
        self._cancelled = False

    @property
    def available(self) -> bool:
        return True

    async def separate(self, track, source, on_progress):
        self.calls.append(track.id)
        self._cancelled = False
        await on_progress("AI separating", 0.5)
        await self.release.wait()
        self.release.clear()
        if self._cancelled:
            raise SeparationCancelled("stopped")
        if self.fail:
            raise SeparationError("boom")
        vocals = source.parent / config.VOCALS_FILE
        instrumental = source.parent / config.INSTRUMENTAL_FILE
        vocals.write_bytes(b"v")
        instrumental.write_bytes(b"i")
        return vocals, instrumental

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def cancel(self) -> None:
        self._cancelled = True
        self.paused = False
        self.release.set()


async def settle(pred, label: str, tries: int = 100) -> None:
    for _ in range(tries):
        if pred():
            break
        await asyncio.sleep(0.01)
    ok(pred(), label)


def cached(store: Store, cache: TrackCache, name: str, created: float) -> Track:
    track = Track.from_source("test", name, title=name, duration_ms=1000)
    store.upsert_track(track)
    with store._lock, store._conn:  # deterministic "newest first"
        store._conn.execute("UPDATE tracks SET created_at = ? WHERE id = ?", (created, track.id))
    directory = cache.dir_for(track.id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "source.wav").write_bytes(b"RIFF")
    return track


async def fake_tests() -> None:
    store = Store(Path(_TMP) / "ktv.db")
    cache = TrackCache(Path(_TMP) / "tracks")
    state = KTVState(store, cache)
    sep = FakeSeparator()
    pipeline = Pipeline(state, store, cache, separator=sep)
    pipeline.start()
    config.AI_IDLE_DELAY_S = 60

    old = cached(store, cache, "old", 1)
    new = cached(store, cache, "new", 2)
    done = cached(store, cache, "done", 3)
    (cache.dir_for(done.id) / config.VOCALS_FILE).write_bytes(b"v")
    (cache.dir_for(done.id) / config.INSTRUMENTAL_FILE).write_bytes(b"i")

    print("1. cache hit")
    item = await state.add_item(store.get_track(done.id))
    ok(item.karaoke_mode == KaraokeMode.AI, "auto mode picks AI when the separator is available")
    await settle(lambda: item.ai_status == AIStatus.READY, "separated song is AI READY")
    ok(sep.calls == [], "no inference for a cached song")
    await state.remove_item(item.id)
    await settle(lambda: not pipeline._busy(), "room idle again")

    print("2. idle delay")
    ok(await pipeline.backfill_once() is None, "no backfill right after the room was busy")
    pipeline._last_busy -= 61
    picked = await pipeline.backfill_once()
    ok(picked == new.id, "newest cached song without stems is picked first")
    await settle(lambda: sep.calls == [new.id], "backfill job started")
    ok(await pipeline.backfill_once() is None, "only one job at a time")

    print("3. pause while singing")
    party = cached(store, cache, "party", -1)  # already separated: needs no job
    (cache.dir_for(party.id) / config.VOCALS_FILE).write_bytes(b"v")
    (cache.dir_for(party.id) / config.INSTRUMENTAL_FILE).write_bytes(b"i")
    song = await state.add_item(store.get_track(party.id))
    ok(sep.paused, "backfill paused once a song is queued")
    await state.remove_item(song.id)
    await settle(lambda: not sep.paused, "resumed when the room is idle")
    sep.release.set()
    await settle(lambda: cache.stem_paths(new.id) is not None, "backfill wrote stems")
    await settle(lambda: pipeline._ai_active is None, "worker idle")

    print("4. a queued song beats the backfill")
    pipeline._last_busy -= 61
    ok(await pipeline.backfill_once() == old.id, "next candidate picked")
    await settle(lambda: sep.calls[-1] == old.id, "backfill job running")
    fresh = cached(store, cache, "fresh", 0)  # not the newest: never a candidate first
    queued = await state.add_item(store.get_track(fresh.id))
    await settle(lambda: sep.calls[-1] == fresh.id, "queued song runs right away")
    ok(cache.stem_paths(old.id) is None, "dropped backfill left no stems")
    sep.release.set()
    await settle(lambda: queued.ai_status == AIStatus.READY, "queued song AI READY")
    await state.remove_item(queued.id)
    await settle(lambda: pipeline._ai_active is None, "worker idle")
    pipeline._last_busy -= 61
    ok(await pipeline.backfill_once() == old.id, "dropped song is picked again later")
    await settle(lambda: sep.calls[-1] == old.id and pipeline._ai_active == old.id, "backfill job running")

    print("5. queueing the song the backfill holds")
    calls = len(sep.calls)
    wanted = await state.add_item(store.get_track(old.id))
    ok(not sep.paused, "the job keeps running for the queued song")
    ok(wanted.ai_status == AIStatus.PROCESSING, "item shows AI PROCESSING")
    sep.release.set()
    await settle(lambda: wanted.ai_status == AIStatus.READY, "item AI READY from the same job")
    ok(len(sep.calls) == calls, "no second inference")
    await state.remove_item(wanted.id)
    await settle(lambda: pipeline._ai_active is None, "worker idle")

    print("6. failures are counted")
    bad = cached(store, cache, "bad", 10)
    sep.fail = True
    for attempt in range(config.AI_MAX_ATTEMPTS):
        pipeline._last_busy -= 61
        ok(await pipeline.backfill_once() == bad.id, f"attempt {attempt + 1} picked")
        await settle(lambda: pipeline._ai_active == bad.id, "job running")
        sep.release.set()
        await settle(lambda: pipeline._ai_active is None, "job finished")
    ok(cache.read_manifest(bad.id).get("ai_attempts") == config.AI_MAX_ATTEMPTS, "attempts stored")
    pipeline._last_busy -= 61
    ok(await pipeline.backfill_once() is None, "skipped after the last attempt")
    sep.fail = False

    if pitch_available():
        print("7. reference melody for scoring")
        sung = cached(store, cache, "sung", 11)
        write_wav(cache.dir_for(sung.id) / config.VOCALS_FILE, voice(MELODY, 7.5))
        (cache.dir_for(sung.id) / config.INSTRUMENTAL_FILE).write_bytes(b"i")
        item = await state.add_item(store.get_track(sung.id))
        ok(state._pitch_url(item) is None, "no pitch_url before pitch.json exists")
        await settle(lambda: cache.pitch_path(sung.id) is not None and not pipeline._pitch_tasks,
                     "queueing a separated song makes its pitch.json", tries=1000)
        await state.remove_item(item.id)
        (cache.dir_for(sung.id) / config.PITCH_FILE).unlink()
        pipeline._last_busy -= 61
        ok(pipeline.pitch_candidate() == sung.id, "backfill finds the song without pitch.json")
        await pipeline.backfill_once()
        ok(cache.pitch_path(sung.id) is not None, "idle backfill made pitch.json")
        item = await state.add_item(store.get_track(sung.id))
        ok((state._pitch_url(item) or "").startswith(config.url(f"/media/{sung.id}/{config.PITCH_FILE}?v=")),
           "display gets pitch_url (with its mtime)")
        ok(pipeline.pitch_candidate() is None, "nothing left to do")
        await state.remove_item(item.id)
        pitch = cache.dir_for(sung.id) / config.PITCH_FILE
        pitch.write_text('{"version":1,"notes":[[0,100,6000]]}', encoding="utf-8")
        ok(not cache.pitch_current(sung.id), "a version 1 pitch.json is outdated")
        ok(pipeline.pitch_candidate() == sung.id, "backfill redoes an old pitch.json")
        await settle(lambda: not pipeline._busy(), "room idle again")
        pipeline._last_busy -= 61
        await pipeline.backfill_once()
        ok(cache.pitch_current(sung.id) and "frames" in pitch.read_text(encoding="utf-8"), "redone as version 2 " + pitch.read_text(encoding="utf-8")[:60])
    await pipeline.stop()


def real_test(model: Path) -> None:
    print("7. real separator")
    from ktv.ai import OnnxSeparator

    sep = OnnxSeparator(model)
    ok(sep.available, f"{model.name} available (onnxruntime + ffmpeg)")
    directory = Path(_TMP) / "real"
    directory.mkdir()
    source = directory / "source.wav"
    subprocess.run(
        [config.FFMPEG_BIN, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
         "-f", "lavfi", "-i", "anoisesrc=d=12:a=0.1", "-filter_complex", "amix=2,aformat=channel_layouts=stereo",
         "-ar", "44100", str(source)],
        check=True,
    )
    seen: list[float] = []

    async def progress(_msg, value):
        if value is not None:
            seen.append(value)

    started = time.monotonic()
    vocals, instrumental = asyncio.run(sep.separate(Track.from_source("test", "real", title="real"), source, progress))
    elapsed = time.monotonic() - started
    dur = float(subprocess.run(
        [config.FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
         str(instrumental)], capture_output=True, text=True).stdout)
    ok(abs(dur - 12) < 0.05, f"instrumental is 12 s ({dur:.2f})")
    ok(vocals.stat().st_size > 0, "vocals written")
    ok(seen and seen[-1] == 1.0, "progress reaches 1.0")
    ok(not list(directory.glob("*.part")), "no .part files left")
    print(f"     {elapsed:.1f}s for 12 s of audio")

    if sys.platform.startswith("linux"):
        async def control() -> str:
            vocals.unlink()
            job = asyncio.ensure_future(sep.separate(Track.from_source("test", "real", title="real"), source, progress))
            while sep._proc is None:
                await asyncio.sleep(0.05)
            await asyncio.sleep(1.5)
            pid = sep._proc.pid
            sep.pause()
            await asyncio.sleep(0.3)
            state = Path(f"/proc/{pid}/stat").read_text().split()[2]
            sep.resume()
            await asyncio.sleep(0.3)
            sep.cancel()
            try:
                await job
                return state + ":finished"
            except SeparationCancelled:
                return state + ":cancelled"

        result = asyncio.run(control())
        ok(result.startswith("T"), "pause stops the child process (SIGSTOP)")
        ok(result.endswith(":cancelled"), "cancel raises SeparationCancelled")
        ok(not list(directory.glob("*.part")), "cancelled job leaves no .part files")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path)
    args = parser.parse_args()
    try:
        asyncio.run(fake_tests())
        if args.model:
            real_test(args.model)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("PASS")


if __name__ == "__main__":
    main()
