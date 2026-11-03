"""Smoke test: N concurrent phone clients + one host, over the real /ws.

Usage:  python server/ws_smoke.py [clients] [url]
"""

from __future__ import annotations

import asyncio
import json
import sys

import websockets

DEFAULT_URL = "ws://localhost:8080/ws"

TRACK = {
    "id": "youtube_YQHsXMglC9A",
    "title": "Adele - Hello",
    "artist": "Adele",
    "duration_ms": 367000,
    "source": "youtube",
    "source_id": "YQHsXMglC9A",
    "lyrics_offset_ms": 0,
}


async def phone(name: str, url: str, queue: asyncio.Queue) -> None:
    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "hello", "role": "remote", "name": name}))
        snapshot = json.loads(await ws.recv())
        if snapshot.get("type") != "session.snapshot":
            raise AssertionError(f"{name}: expected snapshot, got {snapshot.get('type')}")
        await ws.send(
            json.dumps(
                {
                    "type": "queue.add",
                    "track": TRACK,
                    "requested_by": name,
                    "karaoke_mode": "INSTANT",
                }
            )
        )
        # consume broadcasts until this client sees its own item in the session
        for _ in range(40):
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if msg.get("type") != "session.snapshot":
                continue
            items = [*(msg.get("queue") or []), *( [msg.get("current")] if msg.get("current") else [])]
            if any(i["requested_by"] == name for i in items):
                await queue.put((name, msg))
                return
        raise AssertionError(f"{name}: queue.add was never reflected in a snapshot")


async def host(url: str, queue: asyncio.Queue) -> None:
    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "hello", "role": "host", "name": "Host"}))
        json.loads(await ws.recv())  # initial snapshot
        await ws.send(json.dumps({"type": "playback.pause"}))
        await ws.send(json.dumps({"type": "playback.play"}))
        for _ in range(40):
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if msg.get("type") == "session.snapshot" and len(msg.get("users") or []) >= 6:
                await queue.put(("host", msg))
                return
        raise AssertionError("host: never saw all 6 users connected")


async def main() -> None:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    url = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_URL
    results: asyncio.Queue = asyncio.Queue()

    phones = [asyncio.create_task(phone(f"phone{i+1}", url, results)) for i in range(count)]
    host_task = asyncio.create_task(host(url, results))

    done, pending = await asyncio.wait([*phones, host_task], timeout=60)
    for task in pending:
        task.cancel()
    errors = [t.exception() for t in done if t.exception()]
    if errors:
        for err in errors:
            print(f"FAIL: {err!r}")
        raise SystemExit(1)

    snapshots = []
    while not results.empty():
        snapshots.append(results.get_nowait())
    final = snapshots[-1][1]
    total = len([*(final.get("queue") or []), final.get("current")])
    users = len(final.get("users") or [])

    print(f"clients        : {count} phones + 1 host")
    print(f"users reported : {users}")
    print(f"session items  : {total} (current + queue)")
    print(f"snapshots seen : {len(snapshots)}")
    ok = total >= count and users >= count + 1
    print("PASS" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
