"""Local lyrics provider.

MVP resolves lyrics from files only (cache first, then `data/lyrics/`), which
keeps the queue working while offline.  A network AMLL provider can be added
later without changing the pipeline.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .. import config
from ..models import Track
from ..store import TrackCache

LYRICS_EXTENSIONS = (".lrc", ".yrc", ".qrc", ".txt")


def lyrics_format_for(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    return ext or "txt"


class LocalLyricsProvider:
    def __init__(self, cache: TrackCache | None = None, lyrics_dir: Path | None = None) -> None:
        self.cache = cache or TrackCache()
        self.lyrics_dir = lyrics_dir or config.LYRICS_DIR

    async def resolve(self, track: Track) -> Path | None:
        cached = self.cache.lyrics_path(track.id)
        if cached is not None:
            return cached

        for candidate in self._candidates(track):
            if candidate.exists() and candidate.stat().st_size > 0:
                return self._import(track, candidate)
        return None

    def _candidates(self, track: Track) -> list[Path]:
        names: list[str] = []
        if track.source_id:
            names.append(track.source_id)
        names.append(track.id)
        slug = self._slug(f"{track.title} - {track.artist}")
        if slug:
            names.append(slug)
        title_slug = self._slug(track.title)
        if title_slug:
            names.append(title_slug)

        paths: list[Path] = []
        for name in names:
            for ext in LYRICS_EXTENSIONS:
                paths.append(self.lyrics_dir / f"{name}{ext}")
                paths.append(self.lyrics_dir / name / f"lyrics{ext}")
        return paths

    @staticmethod
    def _slug(text: str) -> str:
        cleaned = "".join(c if c.isalnum() or c in "-_ " else "" for c in text or "")
        return "-".join(cleaned.split()).lower()

    def _import(self, track: Track, source: Path) -> Path:
        """Copy an externally provided lyrics file into the track cache."""
        target_dir = self.cache.dir_for(track.id)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"lyrics{source.suffix.lower()}"
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        return target
