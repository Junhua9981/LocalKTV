"""Dual-source audio router: catalogue plug-in <-> YouTube (mutual fallback).

Aggregation plan (Phase D):

* track queued from the aggregate search -> try the aggregate resolve chain
  first; when it fails entirely, look the song up on YouTube and download
  that video into the *same* track cache directory (so lyrics/MV/AI paths
  stay untouched);
* track queued from YouTube (i.e. aggregate search was down at queue time)
  -> yt-dlp first — it has a real video id already — then the aggregate
  chain as a fallback when YouTube is unreachable.

YouTube's own retry/timeout policy is preserved; the aggregate side is
bounded by the plug-in's own call budget and per-host pacing.
Cancellation propagates untouched (`except Exception` never sees
CancelledError).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .. import config
from ..models import Track
from ..store import TrackCache
from . import registry
from .youtube import (
    ProviderError,
    YouTubeAudioProvider,
    YouTubeSearchProvider,
    yt_search_queries,
)

log = logging.getLogger(__name__)


class AudioRouter:
    """`AudioProvider`-compatible front door for both sources."""

    def __init__(self, cache: TrackCache | None = None) -> None:
        self.cache = cache or TrackCache()
        self.youtube = YouTubeAudioProvider(self.cache)

    async def acquire(self, track: Track) -> Path:
        if track.source == "youtube":
            try:
                return self._noted(await self.youtube.acquire(track), track)
            except Exception as yt_error:
                if not config.AGG_AUDIO or registry.catalog() is None:
                    raise
                return await self._aggregate(track, cause=yt_error)
        try:
            return await self._aggregate(track)
        except Exception as agg_error:
            return await self._youtube(track, cause=agg_error)

    async def _aggregate(self, track: Track, cause: Exception | None = None) -> Path:
        try:
            catalog = registry.catalog()
            if catalog is None:
                raise ProviderError("no catalogue plug-in installed")
            return await asyncio.to_thread(
                catalog.acquire, track, self.cache.dir_for(track.id)
            )
        except Exception as exc:
            if cause is None:
                raise
            raise ProviderError(
                f"aggregate failed ({exc}); youtube failed ({cause})"
            ) from exc

    def _noted(self, path: Path, track: Track) -> Path:
        """YouTube audio: no catalogue entry — the lyrics chain must search
        (catalogue wins record themselves in the plug-in's acquire)."""
        self.cache.update_manifest(
            track.id,
            audio_via="youtube",
            audio_source="youtube",
            audio_step="youtube",
            audio_platform="",
            audio_song_id="",
        )
        return path

    async def _youtube(self, track: Track, cause: Exception) -> Path:
        shim = track
        if track.source != "youtube":
            # No video id on this track — look one up (query ladder: an
            # over-specific "title+artist" often returns zero results),
            # keep the original cache directory.
            hit = None
            for query in yt_search_queries(track.title, track.artist):
                try:
                    results = await YouTubeSearchProvider().search(query, 1)
                except Exception as exc:
                    log.warning("YouTube fallback search %r failed: %s", query, exc)
                    continue
                if results:
                    hit = results[0]
                    break
            if hit is None:
                raise ProviderError(f"aggregate failed ({cause}); no youtube match")
            shim = Track(
                id=track.id,
                title=track.title,
                artist=track.artist,
                duration_ms=track.duration_ms or hit.duration_ms,
                source="youtube",
                source_id=hit.source_id,
                lyrics_offset_ms=track.lyrics_offset_ms,
            )
        try:
            return self._noted(await self.youtube.acquire(shim), track)
        except Exception as yt_error:
            raise ProviderError(
                f"aggregate failed ({cause}); youtube failed ({yt_error})"
            ) from yt_error

    def download_mv(self, track: Track) -> Path | None:
        """MV works for both sources (ytsearch for aggregate tracks)."""
        return self.youtube.download_mv(track)
