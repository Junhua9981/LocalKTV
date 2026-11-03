"""音源外掛註冊（providers/registry.py）— offline, in a temporary data folder.

Checks the `ktv_sources` plug-in is preferred when present, and that with
nothing registered the main search asks YouTube instead, downloads go to
YouTube, there are no charts and offers only 「自動」 on /host.

Run:  .venv/Scripts/python server/registry_test.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="ktv-registry-"))
os.environ["KTV_DATA_DIR"] = str(TMP)
os.environ.setdefault("KTV_MV", "0")
os.environ.setdefault("KTV_AUTH", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from library_test import call  # noqa: E402
from ktv import config, covers  # noqa: E402
from ktv.app import create_app  # noqa: E402
from ktv.models import Track  # noqa: E402
from ktv.pipeline import Pipeline  # noqa: E402
from ktv.providers import registry  # noqa: E402
from ktv.providers import router as rt  # noqa: E402
from ktv.rooms import Rooms  # noqa: E402
from ktv.state import KTVState  # noqa: E402
from ktv.store import Store, TrackCache  # noqa: E402

FAILED: list[str] = []


def ok(cond: bool, label: str) -> None:
    print(f"  [{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        FAILED.append(label)


class FakeCatalog:
    def __init__(self) -> None:
        self.active = "auto"
        self.acquired: list[str] = []

    def sources(self) -> list[tuple[str, str]]:
        return [("fake", "假音源")]

    def search(self, query: str, limit: int = 10, client: str = "", page: int = 1) -> list[Track]:
        if client == "stale":
            raise registry.SearchSuperseded()
        return [Track.from_source("fake", f"tx_{query}", title=query, artist="某人", duration_ms=1000)]

    def acquire(self, track: Track, directory: Path, only_own: bool = False) -> Path:
        self.acquired.append(track.id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "source.wav").write_bytes(b"RIFF" * 100)
        return directory / "source.wav"

    def set_active_source(self, value: str) -> None:
        if value not in ("auto", "fake"):
            raise ValueError(value)
        self.active = value

    def active_source(self) -> str:
        return self.active

    def backing_off(self, host: str) -> bool:
        return host == "resting.test"


class FakeCharts:
    def index(self) -> list[dict]:
        return [{"key": "demo", "name": "示範榜"}]

    async def get_chart(self, key: str) -> dict:
        if key != "demo":
            raise KeyError(key)
        return {"key": key, "songs": []}


class FakeAudio:
    async def acquire(self, track: Track) -> Path:
        raise RuntimeError("offline")


def fake_cover(query: str) -> list[tuple[str, list[str], str]]:
    return []


def reset_registry() -> None:
    registry._loaded = False
    registry._catalog = None
    registry._charts = None
    covers.PROVIDERS[:] = [p for p in covers.PROVIDERS if p[0] in covers.BUILTIN_PROVIDERS]


async def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    config.AGG_AUDIO = True
    fake = FakeCatalog()
    plugin = types.ModuleType(registry.PLUGIN)

    def register(reg) -> None:
        reg.register_catalog(fake)
        reg.register_charts(FakeCharts())
        reg.register_cover_provider("fake", fake_cover)

    plugin.register = register  # type: ignore[attr-defined]

    print("1. a ktv_sources package wins over the built-in catalogue")
    reset_registry()
    sys.modules[registry.PLUGIN] = plugin
    registry.load()
    registry.load()  # once only
    ok(registry.catalog() is fake, "plug-in catalogue registered")
    ok(registry.audio_sources() == [{"value": "auto", "text": "自動"}, {"value": "fake", "text": "假音源"}],
       "/host choices: auto + the plug-in's")
    ok([n for n, _ in covers.PROVIDERS] == ["fake", "itunes"], "plug-in covers before iTunes, registered once")
    ok(not covers._available("https://resting.test/a"), "covers skip a host the catalogue rests")
    ok(covers._available("https://fine.test/a"), "… and use the others")

    store = Store()
    cache = TrackCache()
    state = KTVState(store, cache)
    pipeline = Pipeline(state, store, cache, audio=FakeAudio())
    app = create_app(state, pipeline, cache, Rooms.single(state))

    status, rows = await call(app, "GET", "/api/search?q=晴天")
    ok(status == 200 and [r["title"] for r in rows] == ["晴天"], "main search asks the catalogue")
    status, rows = await call(app, "GET", "/api/search?q=晴天&cid=stale")
    ok(status == 200 and rows == [], "a superseded search returns nothing")
    status, body = await call(app, "GET", "/api/charts")
    ok(body == [{"key": "demo", "name": "示範榜"}], "charts index from the plug-in")
    status, _ = await call(app, "GET", "/api/charts/nope")
    ok(status == 404, "unknown chart 404")
    ok(await state.set_audio_source("fake"), "host pins the plug-in source")
    ok(fake.active == "fake" and state.snapshot().audio_source == "fake", "pin applied + in snapshot")
    ok(len(state.snapshot().audio_sources) == 2, "snapshot carries the choices")
    ok(not await state.set_audio_source("nope"), "unknown source rejected")
    await state.set_audio_source("auto")

    with tempfile.TemporaryDirectory(prefix="ktv-router-") as tmp:
        track = Track.from_source("fake", "tx_1", title="T", artist="A")
        path = await rt.AudioRouter(TrackCache(Path(tmp))).acquire(track)
        ok(path.name == "source.wav" and fake.acquired == [track.id], "router downloads through the plug-in")

    print("2. nothing registered: YouTube-only")
    reset_registry()
    sys.modules.pop(registry.PLUGIN, None)
    registry._loaded = True  # pretend load() found no plug-in at all

    ok(registry.catalog() is None and registry.charts() is None, "no catalogue, no charts")
    ok(registry.audio_sources() == [{"value": "auto", "text": "自動"}], "/host offers only 自動")
    ok([n for n, _ in covers.PROVIDERS] == ["itunes"], "covers: iTunes only")
    import ktv.providers as providers

    class FakeSearch:
        async def search(self, query: str, limit: int) -> list[Track]:
            return [Track.from_source("youtube", "vid1", title=query, artist="", duration_ms=1000)]

    saved_search = providers.YouTubeSearchProvider
    providers.YouTubeSearchProvider = FakeSearch
    status, rows = await call(app, "GET", "/api/search?q=稻香")
    ok(status == 200 and [r["source"] for r in rows] == ["youtube"] and not rows[0].get("video_only"),
       "main search: YouTube stands in (original audio, not a KTV video)")
    status, rows = await call(app, "GET", "/api/search?q=稻香&page=2")
    ok(status == 200 and rows == [], "… page 1 only")
    providers.YouTubeSearchProvider = saved_search
    status, body = await call(app, "GET", "/api/charts")
    ok(status == 200 and body == [], "charts index empty")
    status, _ = await call(app, "GET", "/api/charts/demo")
    ok(status == 404, "any chart 404")
    ok(not await state.set_audio_source("fake"), "a source name is rejected")
    ok(await state.set_audio_source("auto"), "auto still accepted")
    ok(state.snapshot().audio_sources == [{"value": "auto", "text": "自動"}], "snapshot: 自動 only")

    calls: list[str] = []

    class FakeYT:
        async def acquire(self, track: Track) -> Path:
            calls.append(track.source_id)
            return Path("/fake/youtube.wav")

    saved = rt.YouTubeSearchProvider
    rt.YouTubeSearchProvider = FakeSearch
    try:
        with tempfile.TemporaryDirectory(prefix="ktv-router-") as tmp:
            router = rt.AudioRouter(TrackCache(Path(tmp)))
            router.youtube = FakeYT()
            path = await router.acquire(Track.from_source("fake", "tx_2", title="T", artist="A"))
            ok(path.name == "youtube.wav" and calls == ["vid1"], "catalogue track falls back to YouTube")
    finally:
        rt.YouTubeSearchProvider = saved

    if FAILED:
        print(f"FAILED: {len(FAILED)}")
        raise SystemExit(1)
    print("PASS")


if __name__ == "__main__":
    asyncio.run(main())
