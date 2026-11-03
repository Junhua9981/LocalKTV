"""SQLite track store + on-disk media cache helpers.

Cache layout (the most important performance optimisation in the system):

    data/tracks/<track-key>/
        manifest.json
        source.wav
        lyrics.qrc / lyrics.lrc / lyrics.yrc / lyrics.txt
        vocals.m4a
        instrumental.m4a
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path

from . import config
from .models import Track


def _track_from_row(row: sqlite3.Row) -> Track:
    return Track(
        cover=row["cover"] if "cover" in row.keys() else "",
        id=row["id"],
        title=row["title"],
        artist=row["artist"],
        duration_ms=row["duration_ms"],
        source=row["source"],
        source_id=row["source_id"],
        lyrics_offset_ms=row["lyrics_offset_ms"],
    )


class Store:
    def __init__(self, db_path: Path | None = None) -> None:
        config.ensure_dirs()
        self.db_path = db_path or config.DB_PATH
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tracks (
                    id              TEXT PRIMARY KEY,
                    title           TEXT NOT NULL,
                    artist          TEXT NOT NULL DEFAULT '',
                    duration_ms     INTEGER NOT NULL DEFAULT 0,
                    source          TEXT NOT NULL,
                    source_id       TEXT NOT NULL,
                    lyrics_offset_ms INTEGER NOT NULL DEFAULT 0,
                    created_at      REAL NOT NULL DEFAULT 0
                );
                -- a guest's "this song is wrong" (lyrics / track / duet colours)
                CREATE TABLE IF NOT EXISTS song_reports (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    track_id    TEXT NOT NULL,
                    title       TEXT NOT NULL DEFAULT '',
                    artist      TEXT NOT NULL DEFAULT '',
                    kind        TEXT NOT NULL,
                    notes       TEXT NOT NULL DEFAULT '',
                    reporters   TEXT NOT NULL DEFAULT '',
                    count       INTEGER NOT NULL DEFAULT 1,
                    room        TEXT NOT NULL DEFAULT '',
                    created_at  REAL NOT NULL,
                    updated_at  REAL NOT NULL,
                    resolved_at REAL
                );
                -- 評分: one row per song sung with the score on (display reports it)
                CREATE TABLE IF NOT EXISTS scores (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    track_id    TEXT NOT NULL,
                    title       TEXT NOT NULL DEFAULT '',
                    artist      TEXT NOT NULL DEFAULT '',
                    singer      TEXT NOT NULL DEFAULT '',
                    room        TEXT NOT NULL DEFAULT '',
                    score       INTEGER NOT NULL,
                    pitch       REAL NOT NULL DEFAULT 0,
                    rhythm      REAL NOT NULL DEFAULT 0,
                    stability   REAL NOT NULL DEFAULT 0,
                    created_at  REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS scores_by_track ON scores (track_id, score DESC);
                -- admin fixes for one search keyword (folded): songs pinned
                -- to the top in `position` order, wrong results hidden
                CREATE TABLE IF NOT EXISTS search_rules (
                    query       TEXT NOT NULL,
                    label       TEXT NOT NULL DEFAULT '',
                    kind        TEXT NOT NULL,
                    track_id    TEXT NOT NULL,
                    track_json  TEXT NOT NULL DEFAULT '',
                    position    INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (query, kind, track_id)
                );
                -- 統計: what guests order and search for.  kind = order (a song
                -- queued), end (it finished / was skipped / failed), search (a
                -- typed search; one row per phrase, see log_search)
                CREATE TABLE IF NOT EXISTS usage_events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind        TEXT NOT NULL,
                    room        TEXT NOT NULL DEFAULT '',
                    user        TEXT NOT NULL DEFAULT '',
                    client      TEXT NOT NULL DEFAULT '',
                    track_id    TEXT NOT NULL DEFAULT '',
                    title       TEXT NOT NULL DEFAULT '',
                    artist      TEXT NOT NULL DEFAULT '',
                    query       TEXT NOT NULL DEFAULT '',
                    query_key   TEXT NOT NULL DEFAULT '',
                    results     INTEGER NOT NULL DEFAULT 0,
                    ordered     INTEGER NOT NULL DEFAULT 0,
                    via         TEXT NOT NULL DEFAULT '',
                    outcome     TEXT NOT NULL DEFAULT '',
                    played_ms   INTEGER NOT NULL DEFAULT 0,
                    created_at  REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS usage_by_time ON usage_events (created_at);
                CREATE INDEX IF NOT EXISTS usage_by_client ON usage_events (client, kind, created_at);
                """
            )
            columns = {r[1] for r in self._conn.execute("PRAGMA table_info(tracks)")}
            if "cover" not in columns:  # 2026-10-28：封面網址（Track.cover）
                self._conn.execute("ALTER TABLE tracks ADD COLUMN cover TEXT NOT NULL DEFAULT ''")

    # -- tracks ---------------------------------------------------------
    def upsert_track(self, track: Track) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO tracks (id, title, artist, duration_ms, source, source_id,
                                    lyrics_offset_ms, cover, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%s','now'))
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    artist = excluded.artist,
                    duration_ms = MAX(excluded.duration_ms, tracks.duration_ms),
                    cover = CASE WHEN excluded.cover != '' THEN excluded.cover ELSE tracks.cover END
                """,
                (
                    track.id,
                    track.title,
                    track.artist,
                    track.duration_ms,
                    track.source,
                    track.source_id,
                    track.lyrics_offset_ms,
                    track.cover,
                ),
            )

    def set_cover(self, track_id: str, cover: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE tracks SET cover = ? WHERE id = ?", (cover, track_id))

    def tracks_missing_cover(self) -> list[Track]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tracks WHERE cover = '' ORDER BY created_at DESC"
            ).fetchall()
        return [_track_from_row(row) for row in rows]

    def get_track(self, track_id: str) -> Track | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        if row is None:
            return None
        return _track_from_row(row)

    def search_cached(self, query: str, limit: int = 10) -> list[Track]:
        """Local cache index search — same song = identical folded title.

        Matching is exact after `textnorm.fold` (简繁/大小寫/標點正規化):

            fold(q) == fold(title)                      — title query
            fold(q) == fold(title + artist) (兩序)        — title + artist
            fold(q) == fold(artist)                     — artist browse

        Because folding keeps bracket text, '晴天' never matches '晴天 (Live)':
        the live/studio rule needs no special case.  Newest first.
        """
        from .textnorm import fold

        needle = fold(query)
        if not needle:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tracks ORDER BY created_at DESC"
            ).fetchall()
        hits: list[Track] = []
        for row in rows:
            track = _track_from_row(row)
            title = fold(track.title)
            artist = fold(track.artist)
            if (
                needle == title
                or needle == f"{title}{artist}"
                or needle == f"{artist}{title}"
                or (artist and needle == artist)
            ):
                hits.append(track)
                if len(hits) >= limit:
                    break
        return hits

    def track_ids(self) -> list[str]:
        """Every known track id, newest first."""
        with self._lock:
            rows = self._conn.execute("SELECT id FROM tracks ORDER BY created_at DESC").fetchall()
        return [row["id"] for row in rows]

    def rows(self) -> list[dict]:
        """Every track row (with created_at), newest first — the song cache page."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM tracks ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def update_track(self, track_id: str, **fields) -> None:
        """Admin fix-ups of a cached song: title / artist / source / source_id / duration."""
        allowed = {k: v for k, v in fields.items() if k in ("title", "artist", "source", "source_id", "duration_ms")}
        if not allowed:
            return
        sets = ", ".join(f"{k} = ?" for k in allowed)
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE tracks SET {sets} WHERE id = ?", (*allowed.values(), track_id))

    def delete_track(self, track_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))

    # -- song reports ---------------------------------------------------
    REPORT_KINDS = ("lyrics", "track", "duet", "other")

    def add_report(self, track: Track, kind: str, note: str, reporter: str, room: str) -> int:
        """File a report; an open one for the same song and kind just counts
        up (and collects the note), so a chorus of guests makes one entry."""
        import time

        now = time.time()
        note, reporter = note.strip()[:200], reporter.strip()[:24]
        with self._lock, self._conn:
            open_ = self._conn.execute(
                "SELECT id, notes, reporters FROM song_reports"
                " WHERE track_id = ? AND kind = ? AND resolved_at IS NULL",
                (track.id, kind),
            ).fetchone()
            if open_ is not None:
                notes = "\n".join(x for x in (open_["notes"], note) if x)[-2000:]
                names = [n for n in open_["reporters"].split("\n") if n]
                if reporter and reporter not in names:
                    names.append(reporter)
                self._conn.execute(
                    "UPDATE song_reports SET notes = ?, reporters = ?, count = count + 1,"
                    " updated_at = ? WHERE id = ?",
                    (notes, "\n".join(names[-20:]), now, open_["id"]),
                )
                return int(open_["id"])
            if self._conn.execute(
                "SELECT COUNT(*) FROM song_reports WHERE resolved_at IS NULL"
            ).fetchone()[0] >= 500:
                return 0  # someone is flooding: keep what is there
            cur = self._conn.execute(
                "INSERT INTO song_reports (track_id, title, artist, kind, notes, reporters, room,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (track.id, track.title[:120], track.artist[:120], kind, note, reporter, room, now, now),
            )
            return int(cur.lastrowid)

    def reports(self, include_resolved_since: float = 0) -> list[dict]:
        """Open reports (newest first), plus those resolved after the given time."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM song_reports WHERE resolved_at IS NULL OR resolved_at >= ?"
                " ORDER BY resolved_at IS NOT NULL, updated_at DESC",
                (include_resolved_since,),
            ).fetchall()
        return [dict(r) for r in rows]

    def resolve_reports(self, ids: list[int], resolved: bool = True) -> None:
        import time

        with self._lock, self._conn:
            self._conn.executemany(
                "UPDATE song_reports SET resolved_at = ? WHERE id = ?",
                [(time.time() if resolved else None, i) for i in ids],
            )

    # -- 評分 -----------------------------------------------------------
    def add_score(self, track: Track, singer: str, room: str, score: int,
                  pitch: float, rhythm: float, stability: float) -> int:
        import time

        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO scores (track_id, title, artist, singer, room, score, pitch, rhythm,"
                " stability, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (track.id, track.title[:120], track.artist[:120], singer.strip()[:24], room,
                 int(score), float(pitch), float(rhythm), float(stability), time.time()),
            )
            return int(cur.lastrowid or 0)

    def song_scores(self, track_id: str, limit: int = 5) -> list[dict]:
        """本曲高分: best first, ties go to whoever got there first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT singer, score, room, created_at FROM scores WHERE track_id = ?"
                " ORDER BY score DESC, created_at ASC LIMIT ?",
                (track_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- search rules (admin: pin / hide results of one keyword) --------
    def search_rules(self, query: str) -> tuple[list[Track], set[str]]:
        """(pinned tracks in order, hidden track ids) for a folded query."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, track_id, track_json FROM search_rules WHERE query = ? ORDER BY position",
                (query,),
            ).fetchall()
        pins = [Track.model_validate_json(r["track_json"]) for r in rows if r["kind"] == "pin"]
        return pins, {r["track_id"] for r in rows if r["kind"] == "hide"}

    def all_search_rules(self) -> list[dict]:
        """Every keyword with rules: {query, label, pins: [Track], hidden: [id]}."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM search_rules ORDER BY query, position"
            ).fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            entry = out.setdefault(r["query"], {"query": r["query"], "label": r["label"], "pins": [], "hidden": []})
            if r["kind"] == "pin":
                entry["pins"].append(Track.model_validate_json(r["track_json"]))
            else:
                entry["hidden"].append(r["track_id"])
        return list(out.values())

    def set_search_rules(self, query: str, label: str, pins: list[Track], hidden: list[str]) -> None:
        """Replace every rule of one folded query (empty lists = forget it)."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM search_rules WHERE query = ?", (query,))
            self._conn.executemany(
                "INSERT OR IGNORE INTO search_rules (query, label, kind, track_id, track_json, position)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [(query, label, "pin", t.id, t.model_dump_json(), i) for i, t in enumerate(pins)]
                + [(query, label, "hide", tid, "", 0) for tid in dict.fromkeys(hidden)],
            )

    # -- 統計 (usage) ---------------------------------------------------
    SEARCH_MERGE_S = 15     # a phrase still being typed: one row, not five
    ORDER_AFTER_SEARCH_S = 600  # a song queued this soon after a search came from it

    def log_order(self, track: Track, user: str, room: str, client: str = "", via: str = "") -> None:
        """A song was queued; the asking phone's last search counts as a hit."""
        import time

        now = time.time()
        client = client[:64]
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO usage_events (kind, room, user, client, track_id, title, artist, via,"
                " created_at) VALUES ('order', ?, ?, ?, ?, ?, ?, ?, ?)",
                (room, user.strip()[:24], client, track.id, track.title[:120],
                 track.artist[:120], via[:16], now),
            )
            if client and via in ("search", "youtube"):
                self._conn.execute(
                    "UPDATE usage_events SET ordered = ordered + 1 WHERE id = ("
                    " SELECT id FROM usage_events WHERE kind = 'search' AND client = ?"
                    " AND created_at >= ? ORDER BY created_at DESC LIMIT 1)",
                    (client, now - self.ORDER_AFTER_SEARCH_S),
                )

    def log_end(self, track: Track, user: str, room: str, outcome: str, played_ms: int) -> None:
        """The current song stopped: outcome finished / skipped / failed."""
        import time

        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO usage_events (kind, room, user, track_id, title, artist, outcome,"
                " played_ms, created_at) VALUES ('end', ?, ?, ?, ?, ?, ?, ?, ?)",
                (room, user.strip()[:24], track.id, track.title[:120], track.artist[:120],
                 outcome, max(0, int(played_ms)), time.time()),
            )

    def log_search(self, query: str, query_key: str, results: int, room: str,
                   client: str, source: str = "main") -> None:
        """A typed search.  The remote searches while the guest types, so the
        same phone's search a few seconds after the last one, when one phrase
        starts the other (晴 → 晴天, or a backspace), replaces it."""
        import time

        now = time.time()
        query, client = query.strip()[:80], client[:64]
        if not query_key:
            return
        with self._lock, self._conn:
            last = self._conn.execute(
                "SELECT id, query_key, ordered FROM usage_events WHERE kind = 'search'"
                " AND client = ? AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
                (client, now - self.SEARCH_MERGE_S),
            ).fetchone() if client else None
            if (last is not None and not last["ordered"]
                    and (query_key.startswith(last["query_key"]) or last["query_key"].startswith(query_key))):
                self._conn.execute(
                    "UPDATE usage_events SET query = ?, query_key = ?, results = ?, via = ?,"
                    " created_at = ? WHERE id = ?",
                    (query, query_key, results, source[:16], now, last["id"]),
                )
                return
            self._conn.execute(
                "INSERT INTO usage_events (kind, room, client, query, query_key, results, via,"
                " created_at) VALUES ('search', ?, ?, ?, ?, ?, ?, ?)",
                (room, client, query, query_key, results, source[:16], now),
            )

    def usage_stats(self, since: float = 0, room: str = "", limit: int = 30) -> dict:
        """熱門點歌 / 熱門搜尋 / 搜不到 / 點歌最多的人 / 最近紀錄 since a time."""
        where, args = "created_at >= ?", [since]
        if room:
            where += " AND room = ?"
            args.append(room)
        with self._lock:
            q = lambda sql, extra=(): [dict(r) for r in self._conn.execute(sql, (*args, *extra)).fetchall()]
            totals = q(
                f"SELECT SUM(kind = 'order') AS orders, SUM(kind = 'search') AS searches,"
                f" SUM(kind = 'end' AND outcome = 'finished') AS finished,"
                f" SUM(kind = 'end' AND outcome = 'skipped') AS skipped,"
                f" COUNT(DISTINCT CASE WHEN kind = 'order' AND user != '' THEN user END) AS users"
                f" FROM usage_events WHERE {where}"
            )[0]
            songs = q(
                f"SELECT track_id, MAX(title) AS title, MAX(artist) AS artist,"
                f" SUM(kind = 'order') AS orders,"
                f" SUM(kind = 'end' AND outcome = 'finished') AS finished,"
                f" SUM(kind = 'end' AND outcome = 'skipped') AS skipped,"
                f" MAX(created_at) AS last_at"
                f" FROM usage_events WHERE {where} AND kind IN ('order', 'end') AND track_id != ''"
                f" GROUP BY track_id HAVING orders > 0"
                f" ORDER BY orders DESC, last_at DESC LIMIT ?", (limit,),
            )
            searches = q(
                f"SELECT query_key, COUNT(*) AS count, SUM(ordered > 0) AS ordered,"
                f" MAX(results) AS results, MAX(created_at) AS last_at,"
                f" (SELECT u.query FROM usage_events u WHERE u.kind = 'search'"
                f"  AND u.query_key = e.query_key ORDER BY u.created_at DESC LIMIT 1) AS query"
                f" FROM usage_events e WHERE {where} AND kind = 'search'"
                f" GROUP BY query_key ORDER BY count DESC, last_at DESC LIMIT ?", (limit,),
            )
            missing = q(
                f"SELECT query_key, COUNT(*) AS count, MAX(created_at) AS last_at,"
                f" (SELECT u.query FROM usage_events u WHERE u.kind = 'search'"
                f"  AND u.query_key = e.query_key ORDER BY u.created_at DESC LIMIT 1) AS query"
                f" FROM usage_events e WHERE {where} AND kind = 'search'"
                f" GROUP BY query_key HAVING MAX(results) = 0"
                f" ORDER BY count DESC, last_at DESC LIMIT ?", (limit,),
            )
            users = q(
                f"SELECT user, COUNT(*) AS orders, COUNT(DISTINCT track_id) AS songs,"
                f" MAX(created_at) AS last_at FROM usage_events"
                f" WHERE {where} AND kind = 'order' AND user != ''"
                f" GROUP BY user ORDER BY orders DESC, last_at DESC LIMIT ?", (limit,),
            )
            recent = q(
                f"SELECT kind, room, user, track_id, title, artist, query, results, ordered,"
                f" via, outcome, played_ms, created_at FROM usage_events WHERE {where}"
                f" ORDER BY created_at DESC, id DESC LIMIT 50"
            )
            rooms = [r[0] for r in self._conn.execute(
                "SELECT DISTINCT room FROM usage_events WHERE room != '' ORDER BY room"
            ).fetchall()]
        totals = {k: int(v or 0) for k, v in totals.items()}
        return {"totals": totals, "songs": songs, "searches": searches, "missing": missing,
                "users": users, "recent": recent, "rooms": rooms}

    def set_lyrics_offset(self, track_id: str, offset_ms: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE tracks SET lyrics_offset_ms = ? WHERE id = ?", (offset_ms, track_id)
            )


class TrackCache:
    """Filesystem layout helpers for a single track."""

    def __init__(self, tracks_dir: Path | None = None) -> None:
        self.tracks_dir = tracks_dir or config.TRACKS_DIR

    def dir_for(self, track_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in track_id)
        if safe in ("", ".", ".."):
            safe = "_"
        return self.tracks_dir / safe

    def source_path(self, track_id: str) -> Path | None:
        base = self.dir_for(track_id) / config.SOURCE_STEM
        for ext in (config.SOURCE_EXT, "m4a", "mp3", "opus", "webm", "mkv"):
            candidate = base.with_suffix(f".{ext}")
            if candidate.exists() and candidate.stat().st_size > 0:
                return candidate
        return None

    def has_source(self, track_id: str) -> bool:
        return self.source_path(track_id) is not None

    def lyrics_path(self, track_id: str) -> Path | None:
        directory = self.dir_for(track_id)
        for name in (config.LYRICS_FILE, "lyrics.lrc", "lyrics.yrc", "lyrics.txt"):
            candidate = directory / name
            if candidate.exists() and candidate.stat().st_size > 0:
                return candidate
        return None

    def mv_path(self, track_id: str) -> Path | None:
        """Background MV video (if one was downloaded), in a browser-playable codec."""
        directory = self.dir_for(track_id)
        for ext in ("mp4", "webm", "m4v"):
            candidate = directory / f"mv.{ext}"
            if candidate.exists() and candidate.stat().st_size > 0:
                return candidate
        return None

    def mv_needs_h264(self, track_id: str) -> bool:
        """The cached MV is AV1/VP9/HEVC instead of H.264.

        iPhone/iPad Safari 多半解不了 AV1、VP9（只有最新晶片才有 AV1 硬解），
        影片就整段黑屏；H.264 每個瀏覽器都能播。只看檔頭的 sample entry
        （yt-dlp 的 mp4 moov 在最前面），認不出來就當沒問題，不要一直重抓。
        """
        mv = self.mv_path(track_id)
        if mv is None or (mv.parent / "mv.no-h264").exists():
            return False  # 這支影片 YouTube 本來就沒有 H.264，別每次都重抓
        if mv.suffix == ".webm":
            return True
        try:
            with mv.open("rb") as f:
                head = f.read(512 * 1024)
        except OSError:
            return False
        if b"avc1" in head or b"avc3" in head:
            return False
        return any(tag in head for tag in (b"av01", b"vp09", b"hev1", b"hvc1"))

    def stem_paths(self, track_id: str) -> tuple[Path, Path] | None:
        directory = self.dir_for(track_id)
        for names in ((config.VOCALS_FILE, config.INSTRUMENTAL_FILE), *config.LEGACY_STEMS):
            vocals, instrumental = (directory / name for name in names)
            if vocals.exists() and instrumental.exists():
                return vocals, instrumental
        return None

    def pitch_path(self, track_id: str) -> Path | None:
        path = self.dir_for(track_id) / config.PITCH_FILE
        return path if path.exists() and path.stat().st_size > 0 else None

    def pitch_current(self, track_id: str) -> bool:
        """pitch.json exists and is the format ktv.pitch_ref writes now.

        Older files still score (the display reads both); the backfill just
        redoes them, which takes seconds, not a new separation.
        """
        path = self.pitch_path(track_id)
        if path is None:
            return False
        try:
            with path.open("rb") as f:
                head = f.read(64)
        except OSError:
            return False
        match = re.match(rb'\{"version":(\d+)', head)
        return match is not None and int(match.group(1)) >= config.PITCH_VERSION

    def manifest_path(self, track_id: str) -> Path:
        return self.dir_for(track_id) / config.MANIFEST_FILE

    def read_manifest(self, track_id: str) -> dict:
        path = self.manifest_path(track_id)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def update_manifest(self, track_id: str, **fields) -> dict:
        directory = self.dir_for(track_id)
        directory.mkdir(parents=True, exist_ok=True)
        manifest = self.read_manifest(track_id)
        manifest.update(fields)
        self.manifest_path(track_id).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return manifest
