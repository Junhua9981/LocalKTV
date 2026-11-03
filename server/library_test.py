"""Song cache page (/api/library) — runs offline in a temporary data folder.

Run:  .venv/Scripts/python server/library_test.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from urllib.parse import quote
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="ktv-library-"))
os.environ["KTV_DATA_DIR"] = str(TMP)
os.environ.setdefault("KTV_MV", "0")
os.environ.setdefault("KTV_AGG", "0")
os.environ.setdefault("KTV_AUTH", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ktv import config, library  # noqa: E402
from ktv.app import create_app  # noqa: E402
from ktv.models import KaraokeMode, Track  # noqa: E402
from ktv.pipeline import Pipeline  # noqa: E402
from ktv.providers import lyrics_net  # noqa: E402
from ktv.rooms import Rooms  # noqa: E402
from ktv.state import KTVState  # noqa: E402
from ktv.store import Store, TrackCache  # noqa: E402

FAILED: list[str] = []
LRC = "[00:01.00]第一句\n[00:05.00]第二句\n"


def ok(cond: bool, label: str) -> None:
    print(f"  [{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        FAILED.append(label)


class Guest:
    """Pretend the login gate let a guest through."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        scope["ktv_auth"] = {"role": "guest", "sid": "g", "room": "main"}
        await self.app(scope, receive, send)


async def call(app, method: str, path: str, body: dict | None = None):
    path, _, query = path.partition("?")
    raw = json.dumps(body).encode() if body is not None else b""
    headers = [(b"host", b"testserver")]
    if raw:
        headers.append((b"content-type", b"application/json"))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": quote(query, safe="=&").encode(),
             "root_path": "", "headers": headers, "client": ("127.0.0.1", 1), "server": ("testserver", 80)}
    messages = []

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    status = next(m for m in messages if m["type"] == "http.response.start")["status"]
    data = b"".join(m.get("body", b"") for m in messages)
    try:
        return status, json.loads(data)
    except ValueError:
        return status, data


class FakeAudio:
    async def acquire(self, track: Track) -> Path:
        raise RuntimeError("offline")


class FakeRouter:
    """Stands in for AudioRouter: 'downloads' into the staging cache it got."""

    fail = False

    def __init__(self, cache: TrackCache) -> None:
        self.cache = cache

    async def acquire(self, track: Track) -> Path:
        if FakeRouter.fail:
            raise RuntimeError("no source answered")
        directory = self.cache.dir_for(track.id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "source.wav"
        target.write_bytes(b"NEW" * 100)
        self.cache.update_manifest(track.id, audio_via="aggregate", audio_platform="tx",
                                   audio_song_id=track.source_id, audio_duration_ms=200000)
        return target


class FakeYouTube:
    def __init__(self, cache: TrackCache) -> None:
        self.cache = cache

    async def acquire(self, track: Track) -> Path:
        path = self.cache.dir_for(track.id) / "source.wav"
        path.write_bytes(b"YT" * 100)
        return path

    def download_mv(self, track: Track) -> Path | None:
        if track.source_id == "XXXXXXXXXXX":
            return None
        path = self.cache.dir_for(track.id) / "mv.mp4"
        path.write_bytes(track.source_id.encode())
        return path


def seed(cache: TrackCache, store: Store, track: Track, **files: bytes) -> None:
    store.upsert_track(track)
    directory = cache.dir_for(track.id)
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (directory / name.replace("_", ".")).write_bytes(data)
    cache.update_manifest(track.id, source=track.source, source_id=track.source_id,
                          title=track.title, artist=track.artist)


async def wait_job(lib, track_id: str) -> dict:
    for _ in range(100):
        job = lib.jobs.get(track_id)
        if job and job["state"] != "running":
            return job
        await asyncio.sleep(0.05)
    return lib.jobs.get(track_id) or {}


async def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    config.LYRICS_NET = False
    store = Store()
    cache = TrackCache()
    state = KTVState(store, cache)
    pipeline = Pipeline(state, store, cache, audio=FakeAudio())
    app = create_app(state, pipeline, cache, Rooms.single(state))
    library.AudioRouter = FakeRouter
    library.YouTubeAudioProvider = FakeYouTube

    song = Track.from_source("catalog", "netease_1", title="晴天", artist="周杰倫", duration_ms=269000)
    seed(cache, store, song, source_wav=b"OLD" * 100, lyrics_qrc=b"x",
         vocals_flac=b"v", instrumental_flac=b"i", mv_mp4=b"m")
    cache.update_manifest(song.id, ai_attempts=1, separator="KARA_2", lyrics_provider="qq")
    (cache.tracks_dir / "orphan_dir").mkdir()
    (cache.tracks_dir / "orphan_dir" / "source.wav").write_bytes(b"o")
    (cache.tracks_dir / library.STAGING).mkdir()

    print("1. listing")
    status, body = await call(app, "GET", "/api/library")
    ok(status == 200, "200 for the admin / a LAN without login")
    ids = [s["id"] for s in body["songs"]]
    ok(song.id in ids and "orphan_dir" in ids and library.STAGING not in ids,
       "cached songs + orphan folders, not the staging folder")
    row = next(s for s in body["songs"] if s["id"] == song.id)
    ok(row["ai"] == "ready" and row["stems"] and row["mv"] == "mv.mp4" and row["lyrics"] == "lyrics.qrc",
       "row reports stems / MV / lyrics")
    ok(row["manifest"].get("separator") == "KARA_2" and row["busy"] == "", "manifest bits, not busy")

    print("2. guests are refused")
    guest = Guest(app)
    for method, path in (("GET", "/api/library"), ("DELETE", f"/api/library/{song.id}"),
                         ("GET", f"/api/library/{song.id}/lyrics/search?provider=ne&q=a")):
        ok((await call(guest, method, path))[0] == 403, f"guest {method} {path.split('?')[0]} -> 403")
    ok((await call(app, "GET", "/api/library/nope_1/lyrics/search?provider=ne&q=a"))[0] == 404,
       "unknown song -> 404")

    print("3. lyrics: paste, reject garbage, delete")
    status, body = await call(app, "POST", f"/api/library/{song.id}/lyrics", {"content": LRC, "format": "lrc"})
    ok(status == 200 and body["timing"] == "line", "pasted LRC saved")
    ok(cache.lyrics_path(song.id).name == "lyrics.lrc", "old lyrics.qrc replaced")
    manifest = cache.read_manifest(song.id)
    ok(manifest.get("lyrics_manual") is True and manifest.get("lyrics_provider") == "manual",
       "marked manual (auto fetch leaves it alone)")
    status, _ = await call(app, "POST", f"/api/library/{song.id}/lyrics", {"content": "no timing", "format": "lrc"})
    ok(status == 400, "LRC without timestamps refused")
    status, _ = await call(app, "DELETE", f"/api/library/{song.id}/lyrics")
    manifest = cache.read_manifest(song.id)
    ok(status == 200 and cache.lyrics_path(song.id) is None and "lyrics_provider" not in manifest,
       "lyrics deleted, manifest keys removed (not nulled)")

    print("4. lyrics: fetch again")
    real_fetch = lyrics_net._fetch
    lyrics_net._fetch = lambda track, audio: None
    status, _ = await call(app, "POST", f"/api/library/{song.id}/lyrics/refetch")
    ok(status == 404, "nothing found -> 404")
    lyrics_net._fetch = lambda track, audio: ("lrc", "[00:01.00]简体歌词", {"provider": "kg", "via": "search", "timing": "line"})
    status, body = await call(app, "POST", f"/api/library/{song.id}/lyrics/refetch")
    text = cache.lyrics_path(song.id).read_text(encoding="utf-8")
    ok(status == 200 and body["provider"] == "kg" and "簡體歌詞" in text, "found -> saved, 繁體")
    ok(cache.read_manifest(song.id).get("lyrics_provider") == "kg", "manifest records the provider")
    lyrics_net._fetch = real_fetch

    print("5. lyrics: search one provider, preview a version")
    real_search, real_lyric = lyrics_net._ne_search, lyrics_net._ne_lyric
    lyrics_net._ne_search = lambda q: [
        {"id": 7, "title": "晴天", "artists": ["周杰伦"], "duration_ms": 269000},
        {"id": 8, "title": "晴天 (Live)", "artists": ["某人"], "duration_ms": 330000},
    ]
    lyrics_net._ne_lyric = lambda cand: ("[1000,2000](1000,1000,0)故(2000,1000,0)事", LRC)
    status, body = await call(app, "GET", f"/api/library/{song.id}/lyrics/search?provider=ne&q=晴天")
    ok(status == 200 and len(body) == 2 and body[0]["score"] > body[1]["score"], "candidates with scores")
    ok(body[0]["artists"] == ["周杰倫"], "candidate names in 繁體")
    status, body = await call(app, "POST", f"/api/library/{song.id}/lyrics/preview",
                              {"provider": "ne", "cand": body[0]})
    ok(status == 200 and body["format"] == "yrc" and body["timing"] == "word", "逐字 version preferred")
    ok(body["lines"] and body["lines"][0]["text"] == "故事", "preview lines parsed")
    status, saved = await call(app, "POST", f"/api/library/{song.id}/lyrics",
                               {"content": body["content"], "format": body["format"], "provider": "ne"})
    ok(status == 200 and cache.read_manifest(song.id).get("lyrics_provider") == "ne", "picked version saved")
    ok((await call(app, "GET", f"/api/library/{song.id}/lyrics/search?provider=xx&q=a"))[0] == 400,
       "unknown provider -> 400")
    lyrics_net._ne_search, lyrics_net._ne_lyric = real_search, real_lyric

    print("6. songs in use are not touched")
    item = await state.add_item(song, requested_by="t", karaoke_mode=KaraokeMode.INSTANT)
    status, body = await call(app, "POST", f"/api/library/{song.id}/audio", {})
    ok(status == 409 and "清單" in body["detail"], "replace audio refused while queued")
    ok((await call(app, "DELETE", f"/api/library/{song.id}"))[0] == 409, "delete refused while queued")
    await state.remove_item(item.id)
    if state.current is not None:
        state.current = None
    await asyncio.sleep(0.1)

    print("7. replace audio with another search result")
    FakeRouter.fail = True
    status, _ = await call(app, "POST", f"/api/library/{song.id}/audio",
                           {"pick": {"source": "catalog", "source_id": "tencent_abc", "duration_ms": 270000}})
    job = await wait_job(lib_of(app), song.id)
    ok(status == 200 and job.get("state") == "failed", "failed download reported")
    ok(cache.source_path(song.id).read_bytes().startswith(b"OLD") and cache.stem_paths(song.id),
       "old audio and stems kept when the download fails")
    FakeRouter.fail = False
    status, _ = await call(app, "POST", f"/api/library/{song.id}/audio",
                           {"pick": {"source": "catalog", "source_id": "tencent_abc", "duration_ms": 270000}})
    job = await wait_job(lib_of(app), song.id)
    ok(job.get("state") == "done", f"job done: {job.get('message')}")
    ok(cache.source_path(song.id).read_bytes().startswith(b"NEW"), "new audio in place")
    ok(cache.stem_paths(song.id) is None, "old AI stems dropped")
    manifest = cache.read_manifest(song.id)
    ok("ai_attempts" not in manifest and "separator" not in manifest, "AI failure count reset")
    ok(manifest.get("audio_platform") == "tx" and manifest.get("source_id") == "tencent_abc", "manifest follows")
    saved = store.get_track(song.id)
    ok(saved.source_id == "tencent_abc" and saved.duration_ms == 200000, "database follows (id unchanged)")
    ok(cache.lyrics_path(song.id).name == "lyrics.yrc", "hand-picked lyrics kept")
    ok(not (cache.tracks_dir / library.STAGING / song.id).exists(), "staging folder cleaned")

    print("7b. the admin picks one platform of a merged result: no fallback")
    import fake_catalog
    calls = []

    def fake_acquire(track, directory, only_own=False):
        calls.append((track.source_id, only_own))
        if track.source_id == "kugou_404":
            raise RuntimeError("no kugou url for this entry")
        (directory / "source.wav").write_bytes(b"KG" * 100)
        cache_dir = TrackCache(directory.parent)
        cache_dir.update_manifest(track.id, audio_via="aggregate", audio_platform="kg",
                                  audio_song_id=track.source_id.split("_", 1)[1])
        return directory / "source.wav"

    real_catalog = fake_catalog.install(fake_catalog.FakeCatalog(acquire=fake_acquire))
    FakeRouter.fail = True  # the fallback router must not be used
    await call(app, "POST", f"/api/library/{song.id}/audio",
               {"pick": {"source": "catalog", "source_id": "kugou_404", "strict": True}, "refetch_lyrics": False})
    job = await wait_job(lib_of(app), song.id)
    ok(job.get("state") == "failed" and "kugou" in job.get("message", ""), "picked platform failed -> reported")
    ok(cache.source_path(song.id).read_bytes().startswith(b"NEW"), "audio untouched")
    await call(app, "POST", f"/api/library/{song.id}/audio",
               {"pick": {"source": "catalog", "source_id": "kugou_55", "strict": True}, "refetch_lyrics": False})
    job = await wait_job(lib_of(app), song.id)
    ok(job.get("state") == "done" and cache.source_path(song.id).read_bytes().startswith(b"KG"),
       "picked platform downloaded")
    ok(calls[-1] == ("kugou_55", True) and cache.read_manifest(song.id).get("audio_platform") == "kg",
       "only that entry was asked; manifest says 酷狗")
    await call(app, "POST", f"/api/library/{song.id}/audio",
               {"pick": {"source": "youtube", "source_id": "dQw4w9WgXcQ", "strict": True}, "refetch_lyrics": False})
    job = await wait_job(lib_of(app), song.id)
    manifest = cache.read_manifest(song.id)
    ok(job.get("state") == "done" and cache.source_path(song.id).read_bytes().startswith(b"YT"),
       "picked YouTube downloaded")
    ok(manifest.get("audio_via") == "youtube" and manifest.get("audio_platform") == ""
       and manifest.get("source_id") == "dQw4w9WgXcQ", "manifest says YouTube, no platform id")
    FakeRouter.fail = False
    fake_catalog.restore(real_catalog)

    print("8. MV, AI stems, title")
    ok((await call(app, "POST", f"/api/library/{song.id}/mv", {"video": "not a url!"}))[0] == 400, "bad url -> 400")
    await call(app, "POST", f"/api/library/{song.id}/mv", {"video": "https://youtu.be/AAAAAAAAAAA"})
    job = await wait_job(lib_of(app), song.id)
    ok(job.get("state") == "done" and cache.mv_path(song.id).read_bytes() == b"AAAAAAAAAAA", "MV from a given video")
    await call(app, "POST", f"/api/library/{song.id}/mv", {"video": "XXXXXXXXXXX"})
    job = await wait_job(lib_of(app), song.id)
    ok(job.get("state") == "failed" and cache.mv_path(song.id) is not None, "failed MV keeps the old one")
    await call(app, "DELETE", f"/api/library/{song.id}/mv")
    ok(cache.mv_path(song.id) is None, "MV deleted")
    (cache.dir_for(song.id) / "vocals.flac").write_bytes(b"v")
    (cache.dir_for(song.id) / "instrumental.flac").write_bytes(b"i")
    ok(cache.stem_paths(song.id)[0].name == "vocals.flac", "old FLAC stems still played")
    (cache.dir_for(song.id) / config.VOCALS_FILE).write_bytes(b"v")
    ok(cache.stem_paths(song.id)[0].name == "vocals.flac", "half-converted pair: FLAC until both AAC exist")
    (cache.dir_for(song.id) / config.INSTRUMENTAL_FILE).write_bytes(b"i")
    ok(cache.stem_paths(song.id) == (cache.dir_for(song.id) / config.VOCALS_FILE,
                                     cache.dir_for(song.id) / config.INSTRUMENTAL_FILE), "AAC stems preferred")
    cache.update_manifest(song.id, ai_attempts=2)
    await call(app, "DELETE", f"/api/library/{song.id}/stems")
    ok(cache.stem_paths(song.id) is None and "ai_attempts" not in cache.read_manifest(song.id), "stems dropped")
    ok(not list(cache.dir_for(song.id).glob("vocals.*")), "both formats deleted")
    status, _ = await call(app, "PATCH", f"/api/library/{song.id}", {"title": " 晴天 ", "artist": "周杰倫"})
    ok(status == 200 and store.get_track(song.id).title == "晴天", "title saved (trimmed)")
    ok((await call(app, "PATCH", f"/api/library/{song.id}", {"title": " "}))[0] == 400, "empty title refused")

    print("9. search order: pin and hide per keyword")
    real_agg = config.AGG_AUDIO
    config.AGG_AUDIO = True
    wrong = Track.from_source("catalog", "netease_9", title="黑暗的巴洛克", artist="翻唱者")
    other = Track.from_source("catalog", "kugou_5", title="黑暗的巴洛克", artist="別人",
                              alternates=["tencent_001TpIm90McqJY"])
    right = Track.from_source("catalog", "tencent_001TpIm90McqJY",
                              title="黑暗的巴洛克 (Single Version)", artist="原唱")
    real_catalog = fake_catalog.install(fake_catalog.FakeCatalog(search=lambda q, limit, cid="", page=1: [wrong, other, right]))
    status, rows = await call(app, "GET", "/api/search?q=黑暗的巴洛克")
    ok([r["id"] for r in rows] == [wrong.id, other.id, right.id], "no rules: search order as is")
    status, _ = await call(app, "PUT", "/api/library/search-rules",
                           {"q": "黑暗的巴洛克 ", "pins": [right.model_dump()], "hidden": [wrong.id]})
    ok(status == 200, "rules saved")
    status, rows = await call(app, "GET", "/api/search?q=黑暗的巴洛克")
    ids = [r["id"] for r in rows]
    ok(ids == [right.id] and rows[0].get("pinned"), "pinned first; hidden gone; merged row of the pin dropped")
    status, rows = await call(app, "GET", "/api/search?q=黑暗的巴洛克&page=2")
    ok(all(r["id"] != wrong.id for r in rows) and all(not r.get("pinned") for r in rows), "page 2: no pin, still hidden")
    status, rows = await call(app, "GET", "/api/search?q=黑暗的巴洛克&raw=1")
    ok(len(rows) == 3, "raw=1 shows the untouched results (admin page)")
    status, rows = await call(app, "GET", "/api/search?q=晴天")
    ok([r["id"] for r in rows][-3:] == [wrong.id, other.id, right.id] and not any(r.get("pinned") for r in rows),
       "other keywords untouched")
    status, rules = await call(app, "GET", "/api/library/search-rules")
    ok(len(rules) == 1 and rules[0]["label"] == "黑暗的巴洛克" and rules[0]["hidden"] == [wrong.id], "rules listed")
    ok((await call(guest, "PUT", "/api/library/search-rules", {"q": "x"}))[0] == 403, "guest cannot set rules")
    await call(app, "PUT", "/api/library/search-rules", {"q": "黑暗的巴洛克"})
    ok((await call(app, "GET", "/api/library/search-rules"))[1] == [], "empty rules forget the keyword")
    config.AGG_AUDIO = real_agg
    fake_catalog.restore(real_catalog)

    print("10. guests report a wrong song; the admin handles it")
    from ktv.app import _handle_ws_message
    sent = []

    async def fake_send(cid, msg):
        sent.append(msg)

    state.send = fake_send
    await _handle_ws_message(state, 1, "remote", {"type": "song.report", "track_id": song.id, "kind": "duet",
                                                  "note": "男女聲同色", "name": "小明"}, {"role": "guest", "sid": "a"})
    await _handle_ws_message(state, 2, "remote", {"type": "song.report", "track_id": song.id, "kind": "duet",
                                                  "note": "", "name": "阿華"}, {"role": "guest", "sid": "b"})
    await _handle_ws_message(state, 2, "remote", {"type": "song.report", "track_id": song.id, "kind": "lyrics"},
                             {"role": "guest", "sid": "b"})
    await _handle_ws_message(state, 2, "remote", {"type": "song.report", "track_id": song.id, "kind": "bogus"},
                             {"role": "guest", "sid": "b"})
    await _handle_ws_message(state, 3, "display", {"type": "song.report", "track_id": song.id, "kind": "lyrics"},
                             {"role": "display", "sid": "c"})
    ok(len(sent) == 2 and all(m["type"] == "error" for m in sent), "unknown kind / TV refused")
    status, reports = await call(app, "GET", "/api/library/reports")
    duet = next(r for r in reports if r["kind"] == "duet")
    ok(status == 200 and len(reports) == 2, "two open reports (same song + kind merged)")
    ok(duet["count"] == 2 and duet["reporters"] == ["小明", "阿華"] and duet["notes"] == ["男女聲同色"],
       "merged report counts reporters and keeps notes")
    status, body = await call(app, "GET", "/api/library")
    ok(next(s for s in body["songs"] if s["id"] == song.id)["reports"] == 2, "song row shows open reports")
    ok((await call(guest, "GET", "/api/library/reports"))[0] == 403, "guest cannot read reports")
    await call(app, "POST", "/api/library/reports/resolve", {"ids": [duet["id"]]})
    status, reports = await call(app, "GET", "/api/library/reports")
    ok([r["resolved_at"] is not None for r in reports] == [False, True], "resolved one listed last")
    await _handle_ws_message(state, 1, "remote", {"type": "song.report", "track_id": song.id, "kind": "duet"},
                             {"role": "guest", "sid": "a"})
    status, reports = await call(app, "GET", "/api/library/reports")
    ok(sum(1 for r in reports if r["kind"] == "duet") == 2, "a new report after resolving opens a new entry")

    print("11. delete")
    status, _ = await call(app, "DELETE", f"/api/library/{song.id}")
    ok(status == 200 and not cache.dir_for(song.id).exists() and store.get_track(song.id) is None,
       "folder and database row gone")
    status, _ = await call(app, "DELETE", "/api/library/orphan_dir")
    ok(status == 200 and not (cache.tracks_dir / "orphan_dir").exists(), "orphan folder deleted")

    await pipeline.stop()
    print("FAIL: " + "; ".join(FAILED) if FAILED else "PASS")
    sys.exit(1 if FAILED else 0)


def lib_of(app) -> library.Library:
    return app.state.library


if __name__ == "__main__":
    asyncio.run(main())
