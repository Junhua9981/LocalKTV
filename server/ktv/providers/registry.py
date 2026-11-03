"""Plug-in point for music catalogues, KTV charts and extra cover sources.

The core only talks to what is registered here, never to a platform module
directly, so the catalogue code can live outside this repository:

* a **catalogue** searches songs and downloads their audio (the 「主要音源」
  switch on /host picks among its sources);
* a **chart provider** serves the 熱門榜 tab (`/api/charts`);
* **cover providers** are tried before iTunes (`covers.PROVIDERS`), and
  their image hosts join the /api/covers/src allow-list.

Nothing registered = YouTube-only: the main search returns downloaded songs
and then asks YouTube (audio downloaded, lyrics and AI separation as usual),
the YouTube tab and the router's YouTube path work as before, /api/charts is
empty and covers come from iTunes / YouTube thumbnails.

A plug-in is a package named `ktv_sources` exposing `register(registry)`;
`load()` imports it once at start-up from server/plugins/ (KTV_PLUGINS_DIR),
which the public repository leaves empty.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Protocol

from ..models import Track

log = logging.getLogger(__name__)

PLUGIN = "ktv_sources"
PLUGINS_DIR = Path(os.environ.get("KTV_PLUGINS_DIR") or Path(__file__).resolve().parents[2] / "plugins")


class SearchSuperseded(RuntimeError):
    """The asking remote sent a newer query; this one's results are moot."""


class Catalog(Protocol):
    def sources(self) -> list[tuple[str, str]]:
        """(value, label) for /host 主要音源, without `auto`."""
        ...

    def search(self, query: str, limit: int = 10, client: str = "", page: int = 1) -> list[Track]:
        """Runs in a worker thread; may raise SearchSuperseded."""
        ...

    def acquire(self, track: Track, directory: Path, only_own: bool = False) -> Path:
        """Runs in a worker thread; writes `directory/source.wav`."""
        ...

    def set_active_source(self, value: str) -> None:
        """`auto` or one of sources(); raise on anything else."""
        ...

    def active_source(self) -> str: ...

    def backing_off(self, host: str) -> bool:
        """True while the catalogue rests this host (covers skip it too)."""
        ...


class Charts(Protocol):
    def index(self) -> list[dict[str, Any]]: ...

    async def get_chart(self, key: str) -> dict[str, Any]:
        """KeyError = unknown chart."""
        ...


_lock = threading.RLock()  # a plug-in's register() may ask catalog()
_loaded = False
_catalog: Catalog | None = None
_charts: Charts | None = None


def register_catalog(catalog: Catalog) -> None:
    global _catalog
    _catalog = catalog


def register_charts(charts: Charts) -> None:
    global _charts
    _charts = charts


def register_cover_provider(name: str, search: Callable[[str], list[tuple[str, list[str], str]]]) -> None:
    """Queried before the built-in ones, in registration order."""
    from .. import covers

    builtin = sum(1 for n, _ in covers.PROVIDERS if n in covers.BUILTIN_PROVIDERS)
    covers.PROVIDERS.insert(len(covers.PROVIDERS) - builtin, (name, search))


def register_image_hosts(*hosts: str) -> None:
    """Cover CDNs the plug-in's results point at (`.example.com` = any subdomain)."""
    from .. import covers

    covers.IMAGE_HOSTS.extend(h for h in hosts if h not in covers.IMAGE_HOSTS)


def load() -> None:
    """Import the plug-in once; a missing one is not an error."""
    global _loaded
    with _lock:
        if _loaded:
            return
        _loaded = True
        if PLUGINS_DIR.is_dir() and str(PLUGINS_DIR) not in sys.path:
            sys.path.append(str(PLUGINS_DIR))
        try:
            plugin = importlib.import_module(PLUGIN)
        except ModuleNotFoundError as exc:
            if exc.name != PLUGIN:
                raise
            log.info("sources: no %s plug-in in %s — YouTube only", PLUGIN, PLUGINS_DIR)
            return
        plugin.register(sys.modules[__name__])
        log.info("sources: catalogue=%s charts=%s", _catalog is not None, _charts is not None)


def catalog(load_plugin: bool = True) -> Catalog | None:
    if load_plugin:
        load()
    return _catalog


def charts() -> Charts | None:
    load()
    return _charts


def audio_sources() -> list[dict[str, str]]:
    """The /host 主要音源 choices, `auto` first."""
    cat = catalog()
    rows = [("auto", "自動"), *(cat.sources() if cat is not None else [])]
    return [{"value": value, "text": text} for value, text in rows]
