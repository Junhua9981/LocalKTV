"""A stand-in catalogue plug-in for the offline tests (providers/registry.py).

The tests must not depend on server/plugins/ktv_sources being installed, nor
reach a real music API when it is: `install()` loads whatever plug-in is
there first, then puts this fake in front of it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ktv.models import Track
from ktv.providers import registry


class FakeCatalog:
    def __init__(
        self,
        search: Callable[..., list[Track]] | None = None,
        acquire: Callable[..., Path] | None = None,
        sources: list[tuple[str, str]] | None = None,
    ) -> None:
        self._search = search or (lambda query, limit, client="", page=1: [])
        self._acquire = acquire
        self._sources = sources if sources is not None else [("fake", "假音源")]
        self.active = "auto"

    def sources(self) -> list[tuple[str, str]]:
        return list(self._sources)

    def search(self, query: str, limit: int = 10, client: str = "", page: int = 1) -> list[Track]:
        return self._search(query, limit, client, page)

    def acquire(self, track: Track, directory: Path, only_own: bool = False) -> Path:
        if self._acquire is None:
            raise RuntimeError("offline: fake catalogue")
        return self._acquire(track, directory, only_own=only_own)

    def set_active_source(self, value: str) -> None:
        if value != "auto" and value not in dict(self._sources):
            raise ValueError(f"unknown audio source: {value}")
        self.active = value

    def active_source(self) -> str:
        return self.active

    def backing_off(self, host: str) -> bool:
        return False


def install(catalog: FakeCatalog) -> registry.Catalog | None:
    """Register `catalog`; returns the one it replaced (for `restore`)."""
    registry.load()
    before = registry.catalog()
    registry.register_catalog(catalog)
    return before


def restore(before: registry.Catalog | None) -> None:
    registry._catalog = before
