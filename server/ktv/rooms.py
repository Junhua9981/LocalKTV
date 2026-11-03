"""Rooms (包廂): one KTVState per room, sharing the track cache and pipeline.

Every room has its own queue, playback, volume / key, display look, TV,
remotes, host and phone mics.  The song cache, lyrics, search and the single
download / AI pipeline are shared, so a song queued in a second room is not
fetched again.

The room list (slug + display name) is kept in SQLite so rooms survive a
restart; queues stay in memory, as they always have.  `main` always exists
and is where the pre-rooms URLs (`/display`, `/remote`, `/host`) land.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import config

if TYPE_CHECKING:
    from .pipeline import Pipeline
    from .state import KTVState
    from .store import Store, TrackCache

DEFAULT = "main"
DEFAULT_NAME = "大廳"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,15}$")
NAME_MAX = 24


def valid_slug(slug: str) -> bool:
    return bool(SLUG_RE.match(slug or ""))


def split_room_path(path: str) -> tuple[str | None, str]:
    """`/r/<slug>/display` -> ("<slug>", "/display"); other paths -> (None, path)."""
    if not path.startswith("/r/"):
        return None, path
    slug, _, rest = path[3:].partition("/")
    return slug, "/" + rest


def room_path(room: str, page: str) -> str:
    """App path of a room page: room_path("a", "/remote") -> "/r/a/remote"."""
    return f"/r/{room or DEFAULT}{page}"


class Rooms:
    """The room directory plus each room's live state.

    `store` / `cache` may be None (auth-only tests): the directory still works
    but no KTVState is created.
    """

    def __init__(self, db_path: Path | None, store: "Store | None" = None,
                 cache: "TrackCache | None" = None, max_rooms: int | None = None) -> None:
        self.store = store
        self.cache = cache
        self.max_rooms = max_rooms or config.MAX_ROOMS
        self.pipeline: "Pipeline | None" = None
        self.states: dict[str, "KTVState"] = {}
        self.names: dict[str, str] = {}
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        if db_path is not None:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
            with self._lock, self._conn:
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS rooms (slug TEXT PRIMARY KEY, name TEXT NOT NULL, "
                    "created_at REAL NOT NULL)")
                rows = self._conn.execute("SELECT slug, name FROM rooms ORDER BY created_at").fetchall()
            for slug, name in rows:
                self.names[slug] = name
        self.names.setdefault(DEFAULT, DEFAULT_NAME)
        # main first, then in creation order
        self.names = {DEFAULT: self.names[DEFAULT], **{k: v for k, v in self.names.items() if k != DEFAULT}}
        for slug in self.names:
            self._make_state(slug)

    @classmethod
    def single(cls, state: "KTVState") -> "Rooms":
        """A one-room, in-memory directory around an existing state (tests, tools)."""
        rooms = cls(None)
        state.room, state.room_name = DEFAULT, rooms.names[DEFAULT]
        rooms.states[DEFAULT] = state
        rooms.pipeline = state.pipeline
        return rooms

    # -- live state ------------------------------------------------------
    def _make_state(self, slug: str) -> None:
        if self.store is None or self.cache is None or slug in self.states:
            return
        from .state import KTVState

        state = KTVState(self.store, self.cache)
        state.room, state.room_name = slug, self.names[slug]
        if self.states:  # the audio-source pin is server-wide
            state.audio_source = next(iter(self.states.values())).audio_source
        self.states[slug] = state
        if self.pipeline is not None:
            self.pipeline.attach(state)

    def attach_pipeline(self, pipeline: "Pipeline") -> None:
        self.pipeline = pipeline
        for state in self.states.values():
            pipeline.attach(state)

    @property
    def main(self) -> "KTVState":
        return self.states[DEFAULT]

    def get(self, slug: str | None) -> "KTVState | None":
        return self.states.get(slug or DEFAULT)

    def all(self) -> list["KTVState"]:
        return list(self.states.values())

    def exists(self, slug: str | None) -> bool:
        return (slug or DEFAULT) in self.names

    # -- directory -------------------------------------------------------
    def create(self, slug: str, name: str) -> str:
        """Add a room; returns an error message, or "" when it worked."""
        slug, name = slug.strip().lower(), name.strip()[:NAME_MAX]
        if not valid_slug(slug):
            return "房號只能用小寫英文、數字和 -，最多 16 字。"
        if slug in self.names:
            return "這個房號已經有了。"
        if len(self.names) >= self.max_rooms:
            return f"最多 {self.max_rooms} 間房。"
        name = name or slug
        if self._conn is not None:
            with self._lock, self._conn:
                self._conn.execute("INSERT INTO rooms VALUES (?, ?, ?)", (slug, name, time.time()))
        self.names[slug] = name
        self._make_state(slug)
        return ""

    def rename(self, slug: str, name: str) -> bool:
        name = name.strip()[:NAME_MAX]
        if slug not in self.names or not name:
            return False
        if self._conn is not None:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO rooms VALUES (?, ?, ?) ON CONFLICT(slug) DO UPDATE SET name = excluded.name",
                    (slug, name, time.time()))
        self.names[slug] = name
        state = self.states.get(slug)
        if state is not None:
            state.room_name = name
        return True

    async def delete(self, slug: str) -> bool:
        """Remove a room: its queue is dropped and its sockets are closed."""
        if slug == DEFAULT or slug not in self.names:
            return False
        if self._conn is not None:
            with self._lock, self._conn:
                self._conn.execute("DELETE FROM rooms WHERE slug = ?", (slug,))
        self.names.pop(slug, None)
        state = self.states.pop(slug, None)
        if state is not None:
            if self.pipeline is not None:
                self.pipeline.detach(state)
            await state.close_all()
        return True

    def summary(self) -> list[dict]:
        """Per room: name, who is connected, what is playing (admin overview)."""
        out = []
        for slug, name in self.names.items():
            state = self.states.get(slug)
            roles = [c.role for c in state.clients.values()] if state else []
            out.append({
                "slug": slug,
                "name": name,
                "displays": roles.count("display"),
                "phones": len(roles) - roles.count("display"),
                "queue": len(state.queue) if state else 0,
                "current": (state.current.track.title if state and state.current else ""),
            })
        return out
