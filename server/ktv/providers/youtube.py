"""YouTube search + audio acquisition via yt-dlp.

Search runs yt-dlp as a library; downloads run it in a niced child process
(see `_download`), so its minutes of parsing never starve the server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .. import config
from ..models import Track
from ..store import TrackCache


class ProviderError(RuntimeError):
    pass


log = logging.getLogger(__name__)


def _ydl():
    import yt_dlp  # imported lazily so the server can start without it

    return yt_dlp


def _opts(opts: dict) -> dict:
    """yt-dlp options plus the signed-in cookies, when configured."""
    if config.YT_COOKIES and Path(config.YT_COOKIES).is_file():
        opts["cookiefile"] = config.YT_COOKIES
    return opts


_DOWNLOAD_SCRIPT = "import json, sys, yt_dlp; yt_dlp.YoutubeDL(json.loads(sys.argv[1])).download([sys.argv[2]])"
DOWNLOAD_TIMEOUT_S = 15 * 60


def _download(opts: dict, url: str) -> None:
    """`YoutubeDL(opts).download([url])` in a child process at low CPU priority.

    In-process, yt-dlp's long stretches of Python (plus the downloads around
    it) froze the event loop for up to 25 s on the 2-core VPS: search and
    covers waited 2–16 s.  `opts` must be plain JSON (no hooks or loggers).
    """
    cmd = [sys.executable, "-c", _DOWNLOAD_SCRIPT, json.dumps(_opts(opts)), url]
    nice = shutil.which("nice")
    if nice and sys.platform.startswith("linux"):
        cmd = [nice, "-n", "10", *cmd]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"yt-dlp timed out after {DOWNLOAD_TIMEOUT_S}s") from exc
    if proc.returncode != 0:
        lines = [line for line in proc.stderr.strip().splitlines() if line.strip()]
        raise RuntimeError(lines[-1] if lines else f"yt-dlp exit {proc.returncode}")


def yt_search_queries(title: str, artist: str) -> list[str]:
    """Query ladder for lookups by song name.

    YouTube answers *zero* results for over-specific queries like
    "晴天(深情版) Lucky小爱", so callers walk from precise to broad:
    exact title+artist -> bracket-stripped title+artist -> bare title.
    """
    title = (title or "").strip()
    artist = (artist or "").strip()
    bare = re.sub(r"[（(][^)）]{0,20}[)）]", "", title).strip()
    candidates = [
        f"{title} {artist}".strip(),
        f"{bare} {artist}".strip() if bare and bare != title else "",
        bare,
    ]
    queries: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in queries:
            queries.append(candidate)
    return queries


class YouTubeSearchProvider:
    async def search(self, query: str, limit: int | None = None) -> list[Track]:
        limit = limit or config.YTDLP_SEARCH_LIMIT
        return await asyncio.to_thread(self._search_sync, query, limit)

    def _search_sync(self, query: str, limit: int) -> list[Track]:
        yt_dlp = _ydl()
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": "in_playlist",
            "socket_timeout": 15,
        }
        try:
            with yt_dlp.YoutubeDL(_opts(opts)) as ydl:
                info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        except Exception as exc:  # yt-dlp raises many error types
            raise ProviderError(f"search failed: {exc}") from exc

        tracks: list[Track] = []
        for entry in (info or {}).get("entries") or []:
            if not entry:
                continue
            source_id = entry.get("id")
            if not source_id:
                continue
            duration = entry.get("duration") or 0
            tracks.append(
                Track.from_source(
                    "youtube",
                    source_id,
                    title=entry.get("title") or source_id,
                    artist=entry.get("channel") or entry.get("uploader") or "",
                    duration_ms=int(duration * 1000) if duration else 0,
                    cover=f"https://i.ytimg.com/vi/{source_id}/mqdefault.jpg",
                )
            )
        return tracks


class YouTubeAudioProvider:
    """Downloads the audio stream and transcodes it to a local stereo WAV."""

    def __init__(self, cache: TrackCache | None = None) -> None:
        self.cache = cache or TrackCache()

    async def acquire(self, track: Track) -> Path:
        """Download with retries — YouTube occasionally answers 403 on the
        first signed URL, and a KTV queue must survive that."""
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(self._acquire_sync, track),
                    timeout=config.ACQUIRE_TIMEOUT_S,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise ProviderError(str(last_error) or "download failed")

    def _acquire_sync(self, track: Track) -> Path:
        existing = self.cache.source_path(track.id)
        if existing is not None:
            return existing

        if shutil.which(config.FFMPEG_BIN) is None:
            raise ProviderError("ffmpeg not found on PATH")

        directory = self.cache.dir_for(track.id)
        directory.mkdir(parents=True, exist_ok=True)
        for stale in directory.glob("raw.*"):  # partial leftovers from a failed try
            stale.unlink(missing_ok=True)
        outtmpl = str(directory / "raw.%(ext)s")

        opts = {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 30,
            "retries": 3,
            "overwrites": True,
        }
        try:
            _download(opts, f"https://www.youtube.com/watch?v={track.source_id}")
        except Exception as exc:
            raise ProviderError(f"download failed: {exc}") from exc

        raws = sorted(
            (p for p in directory.glob("raw.*") if p.is_file()),
            key=lambda p: p.stat().st_size,
            reverse=True,
        )
        if not raws:
            raise ProviderError("yt-dlp produced no audio file")
        raw = raws[0]

        target = directory / f"{config.SOURCE_STEM}.{config.SOURCE_EXT}"
        self._transcode(raw, target)
        if raw != target:
            raw.unlink(missing_ok=True)
        return target

    def _mv_video_id(self, track: Track) -> str | None:
        """Video id for the MV background.

        Tracks queued from the aggregate source have no YouTube id — walk
        the query ladder until a video turns up.  Failure simply means no
        MV (never blocks playback).
        """
        if track.source == "youtube":
            return track.source_id
        for query in yt_search_queries(track.title, track.artist):
            try:
                results = YouTubeSearchProvider()._search_sync(query, 1)
            except Exception as exc:
                log.warning("MV search %r failed: %s", query, exc)
                continue
            if results:
                return results[0].source_id
        return None

    def download_mv(self, track: Track) -> Path | None:
        """Optional background MV: a video-only stream in a browser codec.

        Returns the finished file, or None when there is none (failure is not
        fatal — the MV is decoration).  yt-dlp downloads into `*.part` and
        renames on success, so an interrupted try never leaves a half-file
        that `mv_path()` could mistake for a real MV.
        """
        existing = self.cache.mv_path(track.id)
        if existing is not None and not self.cache.mv_needs_h264(track.id):
            return existing
        directory = self.cache.dir_for(track.id)
        directory.mkdir(parents=True, exist_ok=True)
        opts = {
            # video only — audio always comes from the WAV player
            # H.264 first: iOS Safari can't decode the AV1/VP9 YouTube prefers
            "format": (
                "bv*[height<=480][vcodec^=avc1]/bv*[height<=720][vcodec^=avc1]/bv*[vcodec^=avc1]/"
                "bv*[height<=480][ext=mp4]/bv*[height<=480]/bv*[ext=mp4]/bv*"
            ),
            "outtmpl": str(directory / "mv.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 30,
            "retries": 3,
            "overwrites": False,
            # ranged requests: YouTube throttles one long-lived video stream,
            # 10 MB chunks keep each request at full speed
            "http_chunk_size": 10 * 1024 * 1024,
            # DASH/HLS formats come as fragments: fetch a few at once
            "concurrent_fragment_downloads": 4,
        }
        video_id = self._mv_video_id(track)
        if not video_id:
            return existing
        # 舊的 AV1/VP9 MV 先挪開（mv_path 不認這個名字），抓不到 H.264 就放回去
        backup = None
        if existing is not None:
            backup = directory / f"mv-old{existing.suffix}"
            existing.replace(backup)
        try:
            _download(opts, video_id)
        except Exception as exc:
            log.warning("MV download %s for %s failed: %s", video_id, track.id, exc)
        fresh = self.cache.mv_path(track.id)
        if backup is not None:
            if fresh is None:
                backup.replace(existing)
                return existing
            backup.unlink(missing_ok=True)
            if self.cache.mv_needs_h264(track.id):
                (directory / "mv.no-h264").touch()
        return fresh

    @staticmethod
    def _transcode(src: Path, dst: Path) -> None:
        import subprocess

        cmd = [
            config.FFMPEG_BIN,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-vn",
            "-ac",
            "2",
            "-ar",
            "44100",
            str(dst),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
            raise ProviderError(f"ffmpeg transcode failed: {proc.stderr[-500:]}")
