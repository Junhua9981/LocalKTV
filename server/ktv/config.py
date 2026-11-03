"""Runtime configuration for LocalKTV.

Everything is local-first: paths are derived from the repository root so that
`start-karaoke` works regardless of the caller's working directory.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SERVER_DIR = ROOT / "server"
WEB_DIST = ROOT / "web" / "dist"
DATA_DIR = Path(os.environ.get("KTV_DATA_DIR", ROOT / "data"))
TRACKS_DIR = DATA_DIR / "tracks"
LYRICS_DIR = DATA_DIR / "lyrics"
CHARTS_DIR = DATA_DIR / "charts"  # 各 KTV 站台榜單的月級快取
MODELS_DIR = ROOT / "models"
DB_PATH = DATA_DIR / "ktv.db"

HOST = os.environ.get("KTV_HOST", "0.0.0.0")
PORT = int(os.environ.get("KTV_PORT", "8080"))
PUBLIC_URL = os.environ.get("KTV_PUBLIC_URL", "").rstrip("/")
# Serve under a sub-path such as "/ktv" (behind a reverse proxy). Routes stay
# unprefixed internally; every URL sent to the browser goes through url().
_base = os.environ.get("KTV_BASE_PATH", "").strip("/")
BASE_PATH = f"/{_base}" if _base else ""
# Login gate (ktv/auth.py). KTV_ACCESS_KEY only seeds the admin password on
# the first start; without it, /setup asks for the code printed in the log.
ACCESS_KEY = os.environ.get("KTV_ACCESS_KEY", "")
AUTH_REQUIRED = os.environ.get("KTV_AUTH_REQUIRED", "0") == "1"
COOKIE_SECURE = os.environ.get("KTV_COOKIE_SECURE", "1") != "0"
ADMIN_DAYS = int(os.environ.get("KTV_ADMIN_DAYS", "30"))  # admin + paired TV sessions
PARTY_HOURS = int(os.environ.get("KTV_PARTY_HOURS", "12"))  # a party ends this long after its TV last showed
# A room's party starts when its TV shows the invite QR and ends once no TV
# has been connected for this long (laptop closed, tab gone): guests sign out.
PARTY_IDLE_MINUTES = int(os.environ.get("KTV_PARTY_IDLE_MINUTES", "30"))
INVITE_MINUTES = int(os.environ.get("KTV_INVITE_MINUTES", "5"))  # invite QR rotation


def lan_ip() -> str:
    """Best-effort LAN address for the join URL / QR code (never a blocker)."""
    try:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def url(path: str) -> str:
    """Browser-facing URL for an app path ("/remote" -> "/ktv/remote")."""
    return BASE_PATH + path


def join_url(path: str = "/") -> str:
    """What a phone scans to join: the root URL redirects to /remote, a
    room's QR goes straight to /r/<room>/remote."""
    return (PUBLIC_URL or f"http://{lan_ip()}:{PORT}") + url(path)

# Only the next N queue items are prepared ahead of time.
PREFETCH_DEPTH = int(os.environ.get("KTV_PREFETCH_DEPTH", "2"))
# Songs downloading / converting at once, across every room: each yt-dlp +
# ffmpeg run peaks around 50-150 MB, so several busy rooms must not stack up
# on a 1 GB VPS.
PREPARE_CONCURRENCY = max(1, int(os.environ.get("KTV_PREPARE_CONCURRENCY", "2")))
# Rooms (包廂) the admin may create, `main` included (ktv/rooms.py).
MAX_ROOMS = max(1, int(os.environ.get("KTV_MAX_ROOMS", "6")))
# Never run more than one separation job at a time.
AI_CONCURRENCY = int(os.environ.get("KTV_AI_CONCURRENCY", "1"))
# AI is an enhancement: when disabled/unavailable the queue keeps playing in
# INSTANT mode.
AI_ENABLED = os.environ.get("KTV_AI_ENABLED", "1") not in ("0", "false", "no")
# Model file (absolute, or relative to models/); empty = first *.dyn.onnx.
AI_MODEL = os.environ.get("KTV_AI_MODEL", "")
# STFT frames per model call: 64 keeps the peak near 300 MB (256 ~ 1 GB).
AI_FRAMES = int(os.environ.get("KTV_AI_FRAMES", "64"))
AI_THREADS = max(1, int(os.environ.get("KTV_AI_THREADS", "1")))
# Separate already-cached songs while every room is idle (no song playing or
# queued, nothing downloading) for KTV_AI_IDLE_DELAY seconds.  The job pauses
# when a song starts and gives way to songs in a queue.
AI_BACKFILL = os.environ.get("KTV_AI_BACKFILL", "1") not in ("0", "false", "no")
AI_IDLE_DELAY_S = float(os.environ.get("KTV_AI_IDLE_DELAY", "300"))
# A song whose separation failed this many times is skipped by the backfill.
AI_MAX_ATTEMPTS = int(os.environ.get("KTV_AI_MAX_ATTEMPTS", "2"))

# Network lyrics (NetEase LRC first — source_id fast path — then QQ QRC ->
# Kugou LRC -> LRCLIB LRC).  Local
# files always win; a failed/offline fetch resolves to None and never affects
# the queue.
LYRICS_NET = os.environ.get("KTV_LYRICS_NET", "1") not in ("0", "false", "no")
# 简体 -> 繁體 conversion applied to fetched lyrics before caching.
LYRICS_S2T = os.environ.get("KTV_LYRICS_S2T", "1") not in ("0", "false", "no")
# Overall budget for one network lyrics fetch (seconds) — covers up to four
# paced providers plus the one gentle retry each gets for transient errors.
LYRICS_TIMEOUT_S = float(os.environ.get("KTV_LYRICS_TIMEOUT", "45"))

# Background MV video for the display (video-only stream, browser codec).
# Decoration only: playback never waits for it and failure is silent.
MV_DOWNLOAD = os.environ.get("KTV_MV", "1") not in ("0", "false", "no")

# Music-source plug-in (server/plugins/): its catalogue answers search and
# acquire first, YouTube fills in when it cannot serve.  KTV_AGG=0 turns the
# plug-in's catalogue off (search and download then use YouTube only).  The
# plug-in reads its own settings from the environment.
AGG_AUDIO = os.environ.get("KTV_AGG", "1") not in ("0", "false", "no")

# How often the display is expected to report its playback clock (ms).
DISPLAY_REPORT_INTERVAL_MS = 1000

# yt-dlp / FFmpeg
YTDLP_SEARCH_LIMIT = int(os.environ.get("KTV_SEARCH_LIMIT", "10"))
# Netscape cookies.txt for yt-dlp: datacenter IPs get "confirm you're not a bot"
# without a signed-in session. yt-dlp rewrites the file, so it must be writable.
YT_COOKIES = os.environ.get("KTV_YT_COOKIES", "")
ACQUIRE_TIMEOUT_S = int(os.environ.get("KTV_ACQUIRE_TIMEOUT", "240"))
FFMPEG_BIN = os.environ.get("KTV_FFMPEG", "ffmpeg")
FFPROBE_BIN = os.environ.get("KTV_FFPROBE", "ffprobe")

# Media file names inside a track cache directory.
SOURCE_STEM = "source"
SOURCE_EXT = "wav"
LYRICS_FILE = "lyrics.qrc"  # preferred cached format (network source)
# AI stems: AAC 192k (the source is a 320k lossy download, so lossless stems
# kept nothing extra at 4x the size).  Songs separated before 2026-10-23 may
# still hold FLAC stems; they are read until server/convert_stems.py runs.
VOCALS_FILE = "vocals.m4a"
INSTRUMENTAL_FILE = "instrumental.m4a"
LEGACY_STEMS = (("vocals.flac", "instrumental.flac"),)
STEM_FILES = (VOCALS_FILE, INSTRUMENTAL_FILE, *(name for pair in LEGACY_STEMS for name in pair))
PITCH_FILE = "pitch.json"  # reference melody from the vocals stem (ktv.pitch_ref)
PITCH_VERSION = 2  # older pitch.json files are redone by the backfill (ktv.pitch_ref.VERSION)
MV_FILE = "mv.mp4"
MANIFEST_FILE = "manifest.json"


def ensure_dirs() -> None:
    for path in (DATA_DIR, TRACKS_DIR, LYRICS_DIR, CHARTS_DIR, MODELS_DIR):
        path.mkdir(parents=True, exist_ok=True)
