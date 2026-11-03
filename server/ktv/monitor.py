"""Stall detection and a periodic health line, for diagnosing a hung server.

* `SlowRequestMiddleware` — an HTTP request whose response headers take more
  than SLOW_REQUEST_S logs a warning (nginx gives up at 60 s; this shows what
  was slow long before that).  Timed to the headers, not the body, so a phone
  slowly streaming a song is not "slow".
* `watch_loop_lag` — something blocking the event loop (sync I/O, a CPU-bound
  call) freezes every socket at once; a 1 s heartbeat that oversleeps says so.
  Past STACK_DUMP_S a watchdog thread also logs what every thread is doing
  right then, so the log names the culprit instead of only the symptom.
* `report_stats` — every STATS_EVERY_S one INFO line: process RSS, system
  MemAvailable, WebSocket clients, queue length (summed over rooms, plus
  `rooms=` with clients/queue of each busy room).  Linux only for the memory
  fields (read from /proc); elsewhere they are simply left out.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable

if TYPE_CHECKING:
    from .state import KTVState

log = logging.getLogger(__name__)

SLOW_REQUEST_S = 2.0
LOOP_LAG_S = 1.0
LOOP_TICK_S = 1.0
STACK_DUMP_S = 3.0
STATS_EVERY_S = 300.0


class SlowRequestMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.monotonic()
        took: list[float] = []

        async def timed_send(message) -> None:
            if message["type"] == "http.response.start" and not took:
                took.append(time.monotonic() - start)
            await send(message)

        try:
            await self.app(scope, receive, timed_send)
        finally:
            elapsed = took[0] if took else time.monotonic() - start
            if elapsed > SLOW_REQUEST_S:
                log.warning("slow request %s %s took %.1fs%s", scope.get("method", ""),
                            scope.get("path", ""), elapsed, "" if took else " (no response)")


def _dump_stacks(loop_thread: int) -> str:
    """Innermost few frames of every thread, the event loop's thread first."""
    names = {t.ident: t.name for t in threading.enumerate()}
    frames = sorted(sys._current_frames().items(), key=lambda item: item[0] != loop_thread)
    parts = []
    for ident, frame in frames:
        if ident == threading.get_ident():
            continue  # the watchdog itself
        stack = "".join(traceback.format_stack(frame, limit=6))
        label = f"{names.get(ident, ident)}{' (event loop)' if ident == loop_thread else ''}"
        parts.append(f"--- {label}\n{stack}")
    return "\n".join(parts)


def _watchdog(heartbeat: list[float], loop_thread: int) -> None:
    dumped_for = 0.0
    while True:
        time.sleep(1.0)
        beat = heartbeat[0]
        if time.monotonic() - beat > STACK_DUMP_S and beat != dumped_for:
            dumped_for = beat  # once per stall
            log.warning("event loop stuck > %.0fs, threads:\n%s", STACK_DUMP_S, _dump_stacks(loop_thread))


async def watch_loop_lag() -> None:
    loop = asyncio.get_running_loop()
    heartbeat = [time.monotonic()]
    threading.Thread(target=_watchdog, args=(heartbeat, threading.get_ident()), name="loop-watchdog",
                     daemon=True).start()
    while True:
        heartbeat[0] = time.monotonic()
        start = loop.time()
        await asyncio.sleep(LOOP_TICK_S)
        lag = loop.time() - start - LOOP_TICK_S
        if lag > LOOP_LAG_S:
            log.warning("event loop blocked for %.1fs", lag)


def _meminfo_kb(path: str, key: str) -> int | None:
    """`key:   12345 kB` from a /proc file, None when unavailable (Windows)."""
    try:
        for line in Path(path).read_text(encoding="ascii", errors="ignore").splitlines():
            if line.startswith(key + ":"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def stats_line(states: "Iterable[KTVState]") -> str:
    states = list(states)
    clients = [client for state in states for client in state.clients.values()]
    roles = Counter(client.role for client in clients)
    parts = []
    rss = _meminfo_kb("/proc/self/status", "VmRSS")
    if rss is not None:
        parts.append(f"rss={rss // 1024}MB")
    available = _meminfo_kb("/proc/meminfo", "MemAvailable")
    if available is not None:
        parts.append(f"mem_available={available // 1024}MB")
    by_role = ",".join(f"{role}:{n}" for role, n in sorted(roles.items())) or "-"
    parts.append(f"ws={len(clients)} ({by_role})")
    parts.append(f"queue={sum(len(state.queue) for state in states)}")
    parts.append(f"playing={sum(state.current is not None for state in states)}")
    busy = [f"{state.room}:{len(state.clients)}/{len(state.queue)}" for state in states if state.clients]
    if len(states) > 1:
        parts.append(f"rooms={len(states)} ({','.join(busy) or '-'})")
    return " ".join(parts)


async def report_stats(states: "Callable[[], Iterable[KTVState]]") -> None:
    while True:
        log.info("stats %s", stats_line(states()))
        await asyncio.sleep(STATS_EVERY_S)


def start(states: "Callable[[], Iterable[KTVState]]") -> list[asyncio.Task]:
    """`states` is asked each time: rooms come and go while the server runs."""
    return [
        asyncio.create_task(watch_loop_lag(), name="loop-lag"),
        asyncio.create_task(report_stats(states), name="stats"),
    ]
