"""Small ffmpeg-side helpers shared by the pipeline and the song library."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from . import config

log = logging.getLogger(__name__)


def probe_duration(path: Path) -> float | None:
    """Measured length in seconds (the pipeline back-fills duration_ms for
    search results that carry no duration)."""
    try:
        proc = subprocess.run(
            [
                config.FFPROBE_BIN,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "csv=p=0",
                str(path),
            ],
            capture_output=True,
            timeout=15,
        )
        return float(proc.stdout.decode("ascii", "ignore").strip())
    except Exception:
        log.warning("ffprobe duration failed for %s", path, exc_info=True)
        return None
