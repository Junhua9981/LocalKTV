"""統計 (usage_events: orders, how songs ended, typed searches) — offline,
in a temporary data folder.

Run:  .venv/Scripts/python server/usage_test.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="ktv-usage-"))
os.environ["KTV_DATA_DIR"] = str(TMP)
os.environ.setdefault("KTV_MV", "0")
os.environ.setdefault("KTV_AGG", "0")
os.environ.setdefault("KTV_AUTH", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_catalog  # noqa: E402
from library_test import Guest, call  # noqa: E402
from ktv.app import _handle_ws_message, create_app  # noqa: E402
from ktv.models import Track  # noqa: E402
from ktv.pipeline import Pipeline  # noqa: E402
from ktv.rooms import Rooms  # noqa: E402
from ktv.state import KTVState  # noqa: E402
from ktv.store import Store, TrackCache  # noqa: E402

FAILED: list[str] = []


def ok(cond: bool, label: str) -> None:
    print(f"  [{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        FAILED.append(label)


class FakeAudio:
    async def acquire(self, track: Track) -> Path:
        raise RuntimeError("offline")


async def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    store = Store()
    cache = TrackCache()
    state = KTVState(store, cache)
    pipeline = Pipeline(state, store, cache, audio=FakeAudio())
    app = create_app(state, pipeline, cache, Rooms.single(state))
    fake_catalog.install(fake_catalog.FakeCatalog())  # offline either way, plug-in or not
    sunny = Track.from_source("catalog", "netease_1", title="晴天", artist="周杰倫", duration_ms=269000)
    rice = Track.from_source("catalog", "netease_2", title="稻香", artist="周杰倫", duration_ms=223000)

    print("1. typing one phrase is one search")
    for q in ("晴", "晴天"):
        await call(app, "GET", f"/api/search?q={q}&cid=phoneA")
    await call(app, "GET", "/api/search?q=晴天&cid=phoneA&page=2")  # 載入更多
    await call(app, "GET", "/api/search?q=晴天")  # chart look-up: no cid
    await call(app, "GET", "/api/search?q=晴天&cid=phoneA&raw=1")  # admin
    rows = store._conn.execute("SELECT query, results FROM usage_events WHERE kind='search'").fetchall()
    ok([r["query"] for r in rows] == ["晴天"], "晴 → 晴天 merged; more / chart / raw not logged")
    ok(rows[0]["results"] == 0, "no cache, no aggregate: 0 results")
    await call(app, "GET", "/api/search?q=七里香&cid=phoneA")
    await call(app, "GET", "/api/search?q=晴天&cid=phoneB")
    n = store._conn.execute("SELECT COUNT(*) FROM usage_events WHERE kind='search'").fetchone()[0]
    ok(n == 3, "another phrase / another phone is a new row")

    print("2. ordering from a search marks it")
    await _handle_ws_message(state, 0, "remote", {
        "type": "queue.add", "track": sunny.model_dump(mode="json"), "requested_by": "小明",
        "via": "search", "cid": "phoneB"})
    hit = store._conn.execute("SELECT ordered FROM usage_events WHERE kind='search' AND client='phoneB'").fetchone()
    ok(hit["ordered"] == 1, "phoneB's 晴天 search led to a song")
    await call(app, "GET", "/api/search?q=晴天天&cid=phoneB")
    n = store._conn.execute("SELECT COUNT(*) FROM usage_events WHERE kind='search' AND client='phoneB'").fetchone()[0]
    ok(n == 2, "a search after ordering is not merged into the used one")
    await _handle_ws_message(state, 0, "remote", {
        "type": "queue.add", "track": rice.model_dump(mode="json"), "requested_by": "小華", "via": "charts"})
    await _handle_ws_message(state, 0, "remote", {
        "type": "queue.add", "track": sunny.model_dump(mode="json"), "requested_by": "小華"})

    print("3. how songs end")
    state.current = state.queue.pop(0)
    state.playback.queue_item_id = state.current.id
    state.playback.position_ms, state.playback.playing = 200000, False
    state.playback.duration_ms, state.playback.updated_at = 269000, time.time()
    await state.play_next(reason="track ended")  # 晴天 sung to the end → 稻香 current
    state.playback.position_ms, state.playback.playing = 30000, True
    state.playback.updated_at = time.time() - 5
    await state.play_next(reason="skipped")  # 稻香 cut after ~35 s
    ends = store._conn.execute("SELECT title, outcome, played_ms FROM usage_events WHERE kind='end'").fetchall()
    ok([(e["title"], e["outcome"]) for e in ends] == [("晴天", "finished"), ("稻香", "skipped")],
       "finished, then skipped")
    ok(ends[0]["played_ms"] == 269000 and 34000 <= ends[1]["played_ms"] <= 36000,
       "finished = whole song; skipped = position incl. time since the last clock")

    print("4. stats page")
    status, body = await call(app, "GET", "/api/library/stats?days=7")
    ok(status == 200, "200 for the admin")
    ok(body["totals"]["orders"] == 3 and body["totals"]["finished"] == 1 and body["totals"]["skipped"] == 1,
       "totals")
    top = body["songs"][0]
    ok(top["title"] == "晴天" and top["orders"] == 2 and top["finished"] == 1, "晴天 is the top song")
    ok(body["searches"][0]["query"] == "晴天" and body["searches"][0]["count"] == 2
       and body["searches"][0]["ordered"] == 1, "晴天 is the top search, used once")
    ok({m["query"] for m in body["missing"]} >= {"七里香"}, "七里香 found nothing")
    ok([u["user"] for u in body["users"]] == ["小華", "小明"], "users by orders")
    ok(len(body["recent"]) == 9 and body["recent"][0]["kind"] == "end", "recent, newest first")
    status, body = await call(app, "GET", "/api/library/stats?room=other")
    ok(body["totals"]["orders"] == 0, "room filter")
    status, _ = await call(Guest(app), "GET", "/api/library/stats")
    ok(status == 403, "guests are refused")


if __name__ == "__main__":
    asyncio.run(main())
    print("\nFAILED:" if FAILED else "\nall passed", *FAILED, sep="\n  ")
    sys.exit(1 if FAILED else 0)
