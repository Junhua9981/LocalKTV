"""Provider interfaces.

The core system must not depend on any specific platform: everything goes
through these three protocols so NetEase / QQ Music / ... can be added later
as plugins without touching the core.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..models import Track


class SearchProvider(Protocol):
    async def search(self, query: str, limit: int = 10) -> list[Track]: ...


class AudioProvider(Protocol):
    async def acquire(self, track: Track) -> Path:
        """Materialise the audio for `track` locally and return its path."""
        ...


class LyricsProvider(Protocol):
    async def resolve(self, track: Track) -> Path | None:
        """Return a local lyrics file (QRC/LRC/...) or None."""
        ...
