"""Network lyrics provider — word-by-word first (NetEase YRC / QQ QRC),
line-level LRC (Kugou / LRCLIB) only when nobody ships 逐字.

The request shapes were learned from public API research (LDDC / lyricsplus /
ESLyric); the implementation below is original.  Everything fails soft: any
error, timeout or disabled flag resolves to `None`, so lyrics — the least
critical part of a song — can never break the queue.

Flow (inside one worker thread):

    NE:  source_id "netease_<id>" -> lyric by id (exact catalogue entry,
         no search) ; otherwise eapi (AES-ECB) batch search ->
         /eapi/song/lyric/v1 -> YRC (逐字) + LRC in one reply
    QQ:  musics.fcg?sign=zzc… (signed, web/mobile client shape) ->
         DoSearchForQQMusicMobile -> score candidates -> GetPlayLyricInfo
         -> QQ client cipher + zlib -> QRC (逐字) + LRC in one reply
    KG:  complexsearch (md5-signed) -> lyrics.kugou.com v1/search -> download
    LRCLIB: /api/search (keyless) -> score candidates -> syncedLyrics (LRC)

NetEase leads because most aggregate audio is netease-sourced: its lyrics
match the recorded arrangement, and with the song id from source_id the
answer comes from the very same catalogue the audio was taken from.

Only NetEase (YRC) and QQ (QRC) can serve word timings, so the chain asks
those two in order and stops at the first 逐字 answer.  The first usable
line-timed answer is held back instead of returned — the hunt for 逐字 goes
on — and is only handed over once every word-by-word source has failed
(Kugou / LRCLIB come last and only ever serve line-level LRC).  Each answer
records `provider`, `via` (how the song was found) and `timing`
("word" | "line") in the track manifest.

Requests to one provider are paced (>= HOST_GAP_S apart) and three
consecutive failures put that host into a COOLDOWN_S back-off, so a flaky or
rate-limiting provider is never hammered.

The winning text is converted 简体 -> 繁體 (KTV_LYRICS_S2T; Japanese lyrics are
left as they are) and written into
the track cache as `lyrics.yrc` / `lyrics.qrc` / `lyrics.lrc`; existing
(manual) lyrics are never overwritten.

The QQ zzc signature / musics.fcg request shape follows ibratabian17/lyricsplus
(Apache-2.0); LDDC and ESLyric-LyricsSource were consulted as API references
only (GPL-3.0 — no code copied).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Any

from .. import config, textnorm
from .. import qrc_des
from ..lyrics_parse import timing_of
from ..models import Track
from ..store import TrackCache

log = logging.getLogger(__name__)

# Signed web/mobile endpoint — an unsigned musicu.fcg post looks like a bot
# and earns `code=2001` throttling within a handful of requests.
QQ_ENDPOINT = "https://u.y.qq.com/cgi-bin/musics.fcg"
QQ_HEADERS = {
    "content-type": "application/json",
    "referer": "https://y.qq.com/",
    "origin": "https://y.qq.com",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}
QQ_CLIENT_VER = 13020508  # cv/v of the current desktop client
LRCLIB_BASE = "https://lrclib.net/api"
LRCLIB_HEADERS = {"user-agent": "LocalKTV/0.1", "accept": "application/json"}

# 24-byte 3DES key shipped in the QQ mobile client for cloud lyrics.
QRC_KEY = b"!@#)(*$%123ZXC!@!@#)(NHL"

REQUEST_TIMEOUT_S = 5.0
# Pacing / back-off per provider host (protects us from IP bans).
HOST_GAP_S = 1.5
COOLDOWN_AFTER = 3
COOLDOWN_S = 600.0
# One *gentle* retry each for known-transient provider answers — never more.
QQ_2001_RETRY_S = 3.0   # qq answers "request code=2001" under bursts
LRCLIB_RETRY_S = 3.0    # lrclib answers HTTP 503 when loaded
# A candidate must beat this to be trusted (title 2 + artist 2 / duration).
MIN_CANDIDATE_SCORE = 2.0
# A lyric version whose song length differs more than this from the audio
# we actually play belongs to another recording.
MAX_LENGTH_DELTA_MS = 20_000

_NOISE = re.compile(
    r"official\s+(music\s+)?video|official\s+audio|music\s+video|lyric\s+video|"
    r"\blyrics?\b|\bcover\b|翻唱|\b(m/v|mv|hq|hd|4k|audio|visualizer)\b|"
    r"-\s*topic|vevo",
    re.IGNORECASE,
)
# A candidate flagged as a cover loses points unless the queued song is one too
_COVER_MARK = re.compile(r"\bcover\b|翻唱|cover\s+by", re.IGNORECASE)


class LyricsNetError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# text folding / candidate scoring (pure — unit tested)
# ---------------------------------------------------------------------------

def fold(text: str) -> str:
    """Lower-cased, simplified->traditional, punctuation/noise stripped."""
    text = textnorm.to_traditional(text or "").lower()
    text = _NOISE.sub(" ", text)
    text = re.sub(r"[\[\]()（）【】「」『』:：!！?？,，.。·\-—_~`'\"“”|/\\]+", " ", text)
    return " ".join(text.split())


def _tokens(text: str) -> set[str]:
    return set(fold(text).split())


def score_candidate(
    track_title: str,
    track_artist: str,
    duration_ms: int,
    cand_title: str,
    cand_artists: list[str],
    cand_duration_ms: int,
) -> float:
    """How well a search result matches the queued song.

    * title agreement is mandatory (2 pts) — substring or token overlap,
      computed on folded text so 简/繁 and noisy YouTube titles still match;
    * artist agreement adds 2 pts and is what separates the official upload
      from covers of the same song;
    * duration agreement (±5s/±15s/±30s) catches remakes/long versions.
    """
    t_fold = fold(track_title)
    c_fold = fold(cand_title)
    if not t_fold or not c_fold:
        return -1.0
    title_ok = (
        c_fold in t_fold
        or t_fold in c_fold
        or bool(_tokens(track_title) & _tokens(cand_title))
    )
    if not title_ok:
        return -1.0

    score = 2.0
    if _tokens(track_artist) & {t for a in cand_artists for t in _tokens(a)}:
        score += 2.0
    elif _COVER_MARK.search(cand_title or "") and not _COVER_MARK.search(track_title or ""):
        score -= 2.0  # a cover of the song, not the song itself

    if duration_ms > 0 and cand_duration_ms > 0:
        delta = abs(duration_ms - cand_duration_ms) / 1000
        if delta <= 5:
            score += 1.5
        elif delta <= 15:
            score += 0.5
        elif delta <= 30:
            score -= 0.5
        else:
            score -= 1.5
    return score


def best_candidate(track: Track, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Highest-scoring trusted candidate.  When both lengths are known, a
    version more than MAX_LENGTH_DELTA_MS away is a different arrangement
    (live / remix / extended) — its timings would drift, so it never wins."""
    best: dict[str, Any] | None = None
    best_score = 0.0
    for cand in candidates:
        cand_ms = int(cand.get("duration_ms") or 0)
        if track.duration_ms > 0 and cand_ms > 0 and abs(track.duration_ms - cand_ms) > MAX_LENGTH_DELTA_MS:
            continue
        score = score_candidate(
            track.title,
            track.artist,
            track.duration_ms,
            cand.get("title", ""),
            cand.get("artists", []),
            cand.get("duration_ms", 0),
        )
        if score >= MIN_CANDIDATE_SCORE and (best is None or score > best_score):
            best, best_score = cand, score
    return best


_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")


def search_titles(track: Track) -> list[str]:
    """Build at most two search queries from a noisy YouTube title."""
    title = track.title or ""
    bracket = re.search(r"[【\[]([^】\]]+)[】\]]", title)
    if bracket and bracket.group(1).strip():
        title = bracket.group(1)
    elif " - " in title:
        title = title.rsplit(" - ", 1)[-1]
    title = _NOISE.sub(" ", title)
    title = " ".join(title.split())
    artist = (track.artist or "").strip()
    if artist and title.startswith(artist):
        rest = title[len(artist):].strip(" -–—|:：·/")
        # only cut "周杰伦 晴天" style titles; after "周杰伦 (Jay Chou)Official"
        # nothing usable would remain, so keep those intact
        if _CJK.search(rest):
            title = rest
    if not title:
        title = artist

    queries = [title]
    if artist and fold(artist) not in fold(title):
        queries.append(f"{title} {artist}")
    return [q for q in queries if q]


def _to_hans(text: str) -> str:
    """Provider-side query text — their indexes are simplified-first."""
    return textnorm.to_simplified(text or "")


# ---------------------------------------------------------------------------
# HTTP (stdlib only — runs in worker threads) with per-host pacing
# ---------------------------------------------------------------------------

_throttle_lock = threading.Lock()
_slots: dict[str, float] = {}      # host -> time of the next allowed request
_failures: dict[str, int] = {}     # host -> consecutive error count
_cooldowns: dict[str, float] = {}  # host -> back-off until (unix time)


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc


def _throttle(url: str) -> None:
    """Reserve the next request slot for this host; refuse while in back-off.

    Two calls to the same provider are at least HOST_GAP_S apart and after
    COOLDOWN_AFTER consecutive errors the host is left alone for COOLDOWN_S —
    a rate-limiting lyrics site should never see a burst from us.
    """
    host = _host(url)
    now = time.time()
    with _throttle_lock:
        if now < _cooldowns.get(host, 0.0):
            raise LyricsNetError(f"{host} in back-off")
        slot = max(now, _slots.get(host, 0.0) + HOST_GAP_S)
        _slots[host] = slot
    delay = slot - now
    if delay > 0:
        time.sleep(delay)


def _note_ok(host: str) -> None:
    with _throttle_lock:
        _failures[host] = 0


def _note_fail(host: str) -> None:
    with _throttle_lock:
        count = _failures.get(host, 0) + 1
        _failures[host] = count
        if count >= COOLDOWN_AFTER:
            _cooldowns[host] = time.time() + COOLDOWN_S


def _http(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = REQUEST_TIMEOUT_S,
) -> bytes:
    host = _host(url)
    _throttle(url)
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _note_fail(host)
        raise LyricsNetError(str(exc)) from exc
    return body  # transport OK; callers note app-level success (see _qq_call)


def _http_json(url: str, **kwargs: Any) -> Any:
    raw = _http(url, **kwargs)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _note_fail(_host(url))  # HTML/error page instead of JSON counts too
        raise LyricsNetError("bad json") from exc


# ---------------------------------------------------------------------------
# QQ Music — signed musics.fcg (see module docstring)
# ---------------------------------------------------------------------------

# 20-byte XOR mask from the zzc signature algorithm (lyricsplus, Apache-2.0).
_QQ_XOR = bytes(
    [89, 39, 179, 150, 218, 82, 58, 252, 177, 52, 186, 123, 120, 64, 242, 133, 143, 161, 121, 179]
)
_QQ_PART1 = (23, 14, 6, 36, 16, 40, 7, 19)
_QQ_PART2 = (16, 1, 32, 12, 19, 27, 8, 5)


def _qq_guid() -> str:
    return "".join(random.choice("0123456789ABCDEF") for _ in range(32))


def _qq_sign(body: str) -> str:
    """`zzc<part1><b64><part2>` — the musics.fcg request signature.

    sha1 of the exact request body: fixed-position characters form the outer
    parts, the last 20 hash bytes are XOR-scrambled and base64-stripped in the
    middle.  An unsigned post is rejected as a bot; this is the shape the real
    web/mobile clients send.
    """
    digest = hashlib.sha1(body.encode("utf-8")).hexdigest()
    part1 = "".join(digest[i] for i in _QQ_PART1 if i < len(digest))
    scrambled = bytes(
        _QQ_XOR[i] ^ int(digest[i * 2 : i * 2 + 2], 16) for i in range(len(_QQ_XOR))
    )
    b64 = base64.b64encode(scrambled).decode("ascii")
    b64 = b64.replace("/", "").replace("+", "").replace("=", "")
    part2 = "".join(digest[i] for i in _QQ_PART2)
    return ("zzc" + part1 + b64 + part2).lower()


def _qq_comm() -> dict[str, Any]:
    """Common params of the current desktop/mobile client."""
    return {
        "wid": _qq_guid(),
        "cv": QQ_CLIENT_VER,
        "v": QQ_CLIENT_VER,
        "QIMEI36": "8888888888888888",
        "ct": "11",
        "tmeAppID": "qqmusic",
        "format": "json",
        "inCharset": "utf-8",
        "outCharset": "utf-8",
        "uid": "3931641530",
    }


def _qq_call(module: str, method: str, param: dict[str, Any]) -> dict[str, Any]:
    key = f"{module}.{method}"
    body = json.dumps(
        {"comm": _qq_comm(), key: {"module": module, "method": method, "param": param}},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    url = f"{QQ_ENDPOINT}?sign={_qq_sign(body)}"
    payload = _http_json(url, data=body.encode("utf-8"), headers=QQ_HEADERS)
    if not isinstance(payload, dict):
        _note_fail(_host(QQ_ENDPOINT))
        raise LyricsNetError("qq non-json reply")
    sub = payload.get(key)
    if not isinstance(sub, dict):
        _note_fail(_host(QQ_ENDPOINT))
        raise LyricsNetError(f"qq code={payload.get('code', '?')}")
    if sub.get("code", 0) != 0:
        _note_fail(_host(QQ_ENDPOINT))
        raise LyricsNetError(f"qq request code={sub.get('code')}")
    _note_ok(_host(QQ_ENDPOINT))  # app-level success resets the strike counter
    data = sub.get("data")
    return data if isinstance(data, dict) else {}


def _qq_search(query: str) -> list[dict[str, Any]]:
    param = {
        "search_id": str(
            random.randint(1, 20) * 18014398509481984
            + random.randint(0, 4194304) * 4294967296
            + round(time.time() * 1000) % 86400000
        ),
        "remoteplace": "search.android.keyboard",
        "query": _to_hans(query),
        "search_type": 0,  # song
        "num_per_page": 10,
        "page_num": 1,
        "highlight": 0,
        "nqc_flag": 0,
        "page_id": 1,
        "grp": 1,
    }
    # Lite via the signed endpoint: the mobile method is cookie-gated
    # (is_filter=-11, empty item_song) while Lite answers anonymously, and
    # the zzc signature keeps the request looking like a real client.
    data = _qq_call("music.search.SearchCgiService", "DoSearchForQQMusicLite", param)
    songs = ((data.get("body") or {}).get("item_song")) or []
    out: list[dict[str, Any]] = []
    for song in songs:
        if not song.get("id") and not song.get("mid"):
            continue
        album = song.get("album") or {}
        out.append(
            {
                "id": int(song["id"]) if song.get("id") else 0,
                "mid": song.get("mid") or "",
                "title": song.get("title") or "",
                "artists": [
                    s.get("name") for s in (song.get("singer") or []) if s.get("name")
                ],
                "album": album.get("name") or album.get("title") or "",
                "duration_ms": int(song.get("interval") or 0) * 1000,
            }
        )
    return out


def _qq_search_gently(query: str) -> list[dict[str, Any]]:
    """One gentle retry for qq's transient `request code=2001` throttle."""
    try:
        return _qq_search(query)
    except LyricsNetError as exc:
        if "2001" not in str(exc):
            raise
        time.sleep(QQ_2001_RETRY_S)
        return _qq_search(query)


def _b64(value: str) -> str:
    return base64.b64encode((value or "").encode("utf-8")).decode("ascii")


def _qq_lyrics(cand: dict[str, Any]) -> tuple[str, str]:
    """`(word, line)` — both lyric shapes one GetPlayLyricInfo reply carries.

    The fields are labelled loosely: word timings arrive in `qrc`, *or* in
    `lyric` wrapped in `<QrcInfos>` XML (`<Lyric_1 … LyricContent="…"/>`),
    *or* nowhere at all.  So each decoded value is identified by its own shape
    — a QRC wrapper / `[ms,ms]` body versus `[mm:ss]` LRC — instead of by the
    field it came in.  A song without 逐字 still hands its line-level lyrics
    over from the very same round-trip.
    """
    param: dict[str, Any] = {
        "crypt": 1,
        "ct": 11,
        "cv": QQ_CLIENT_VER,
        "lrc_t": 0,
        "qrc": 1,
        "qrc_t": 0,
        "roma": 0,
        "roma_t": 0,
        "trans": 0,
        "trans_t": 0,
    }
    if cand.get("mid"):
        param.update({"songMid": cand["mid"], "type": 1})
    else:  # older/foreign candidates without a mid — metadata form
        param.update(
            {
                "albumName": _b64(cand.get("album", "")),
                "interval": int(cand.get("duration_ms", 0)) // 1000,
                "singerName": _b64("/".join(cand.get("artists") or [])),
                "songID": int(cand["id"]),
                "songName": _b64(cand.get("title", "")),
                "type": 0,
            }
        )
    data = _qq_call("music.musichallSong.PlayLyricInfo", "GetPlayLyricInfo", param)
    word = line = ""
    for value in (data.get("qrc"), data.get("lyric")):
        text = _decode_lyric(value)  # non-string codes decode to ""
        if not text:
            continue
        if not word and timing_of("qrc", text):
            word = text  # QRC body or the whole <QrcInfos> XML around it
        elif not line and timing_of("lrc", text):
            line = text
    return word, line


def _decode_lyric(value: Any) -> str:
    """hex -> QQ client cipher + zlib -> utf-8, plain text passthrough.

    The primary path is `qrc_des.qq_decrypt_hex` — QQ's own client cipher
    (a stock 3DES cannot read these blobs), with zlib and raw-deflate both
    accepted.  A plain stock-3DES decrypt is kept as a fallback in case the
    service ever switches schemes.
    """
    if not isinstance(value, str) or not value.strip():
        return ""
    text = value.strip()
    if text.startswith(("<", "[")):  # already decrypted / plain LRC
        return text
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        return ""
    if not raw or len(raw) % 8:
        return ""
    try:
        return qrc_des.qq_decrypt_hex(text)
    except ValueError:
        pass
    decrypted = qrc_des.des3_ecb(QRC_KEY, raw, decrypt=True)  # stock-DES fallback
    for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
        try:
            out = zlib.decompress(decrypted, wbits)
        except Exception:
            continue
        if out:
            return out.decode("utf-8")
    return ""


# ---------------------------------------------------------------------------
# NetEase Cloud Music — eapi (AES-128-ECB params, plaintext responses)
# ---------------------------------------------------------------------------

NE_SEARCH_URL = "https://interface.music.163.com/eapi/batch"
NE_SEARCH_PATH = "/api/search/song/list/page"
NE_LYRIC_URL = "https://interface3.music.163.com/eapi/song/lyric/v1"
NE_LYRIC_PATH = "/api/song/lyric/v1"
_NE_AES_KEY = b"e82ckenh8dichen8"
NE_HEADERS = {
    "content-type": "application/x-www-form-urlencoded",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "origin": "https://music.163.com",
    "referer": "https://music.163.com",
}


def _ne_eapi_params(path: str, payload: dict[str, Any]) -> str:
    """Encrypt one eapi request body: AES-128-ECB over the md5-sealed json."""
    from Crypto.Cipher import AES  # pycryptodome (already a dependency)

    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.md5(f"nobody{path}use{text}md5forencrypt".encode()).hexdigest()
    message = f"{path}-36cd479b6b5-{text}-36cd479b6b5-{digest}".encode("utf-8")
    pad = 16 - (len(message) % 16)
    message += bytes([pad]) * pad
    return AES.new(_NE_AES_KEY, AES.MODE_ECB).encrypt(message).hex().upper()


def _ne_post(url: str, path: str, payload: dict[str, Any]) -> Any:
    body = urllib.parse.urlencode({"params": _ne_eapi_params(path, payload)}).encode("ascii")
    data = _http_json(url, data=body, headers=NE_HEADERS)
    if isinstance(data, dict) and data.get("code") not in (None, 200):
        _note_fail(_host(url))
        raise LyricsNetError(f"ne code={data.get('code')}")
    return data


def _ne_search(query: str) -> list[dict[str, Any]]:
    data = _ne_post(
        NE_SEARCH_URL,
        NE_SEARCH_PATH,
        {
            "keyword": _to_hans(query),
            "needCorrect": "1",
            "channel": "typing",
            "offset": 0,
            "scene": "normal",
            "total": True,
            "limit": 10,
        },
    )
    resources = ((data.get("data") or {}).get("resources")) or []
    out: list[dict[str, Any]] = []
    for resource in resources:
        song = ((resource.get("baseInfo") or {}).get("simpleSongData")) or {}
        if not song.get("id"):
            continue
        out.append(
            {
                "id": int(song["id"]),
                "title": song.get("name") or "",
                "artists": [
                    a.get("name") for a in (song.get("ar") or []) if a.get("name")
                ],
                "album": (song.get("al") or {}).get("name") or "",
                "duration_ms": int(song.get("dt") or 0),
            }
        )
    return out


def _ne_body(node: Any) -> str:
    """One eapi `lyric` node -> text, minus NetEase's JSON credit rows.

    Both `lrc` and `yrc` arrive prefixed with `{"t":ms,"c":[{"tx":"作词: "},…]}`
    credit lines (作词/作曲/编曲…).  The parsers would skip those rows anyway,
    but stripping keeps the cached file to real lyrics only.
    """
    if not isinstance(node, dict):
        return ""
    body = node.get("lyric")
    if not isinstance(body, str) or not body.strip():
        return ""
    return re.sub(r"^\{.*\}\s*\n?", "", body.strip(), flags=re.M).strip()


def _ne_lyric(cand: dict[str, Any]) -> tuple[str, str]:
    """`(yrc, lrc)` — both lyric flavours one eapi reply carries.

    `/eapi/song/lyric/v1` answers with `yrc` (逐字, `[ms,ms](ms,ms,?)字…`) *and*
    `lrc` (逐行) in a single payload whenever the song has them, so one
    round-trip yields the word-timed candidate and its line-level fallback —
    the host is never asked twice for the same song.  An empty string means
    the song ships no such flavour (or nothing synced); `timing_of` decides
    which of the two is usable.
    """
    data = _ne_post(
        NE_LYRIC_URL,
        NE_LYRIC_PATH,
        {
            "id": int(cand["id"]),
            "cp": False,
            "tv": 0,
            "lv": 0,
            "rv": 0,
            "kv": 0,
            "yv": 0,
            "ytv": 0,
            "yrv": 0,
        },
    )
    return _ne_body(data.get("yrc")), _ne_body(data.get("lrc"))


# ---------------------------------------------------------------------------
# Kugou — md5-signed mobile API (LDDC-derived request shape)
# ---------------------------------------------------------------------------

KG_SEARCH_URL = "http://complexsearch.kugou.com/v2/search/song"
KG_LYRIC_SEARCH_URL = "https://lyrics.kugou.com/v1/search"
KG_LYRIC_DOWNLOAD_URL = "http://lyrics.kugou.com/download"
KG_SECRET = "LnT6xpN3khm36zse0QzvmgTZ3waWdRSA"


def _kg_signature(params: dict[str, Any], data: str = "") -> str:
    joined = "".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hashlib.md5(f"{KG_SECRET}{joined}{data}{KG_SECRET}".encode()).hexdigest()


def _kg_get(url: str, params: dict[str, Any], module: str,
            extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
    mid = hashlib.md5(str(int(time.time() * 1000)).encode("utf-8")).hexdigest()
    if module == "Lyric":
        merged: dict[str, Any] = {"appid": "3116", "clientver": "11070", **params}
    else:
        merged = {
            "userid": "0",
            "appid": "3116",
            "token": "",
            "clienttime": int(time.time()),
            "iscorrection": "1",
            "uuid": "-",
            "mid": mid,
            "dfid": "-",
            "clientver": "11070",
            "platform": "AndroidFilter",
            **params,
        }
    merged["signature"] = _kg_signature(merged)
    headers = {
        "user-agent": f"Android14-1070-11070-201-0-{module}-wifi",
        "connection": "Keep-Alive",
        "KG-Rec": "1",
        "KG-RC": "1",
        "KG-CLIENTTIMEMS": str(int(time.time() * 1000)),
        "mid": mid,
        **(extra_headers or {}),
    }
    query = urllib.parse.urlencode(merged)
    data = _http_json(f"{url}{'&' if '?' in url else '?'}{query}", headers=headers)
    if isinstance(data, dict) and data.get("error_code") not in (None, 0, 200):
        _note_fail(_host(url))
        raise LyricsNetError(
            f"kg code={data.get('error_code')} {data.get('error_msg', '')}"
        )
    return data


def _kg_search(query: str) -> list[dict[str, Any]]:
    data = _kg_get(
        KG_SEARCH_URL,
        {"sorttype": "0", "keyword": _to_hans(query), "pagesize": "20", "page": "1"},
        "SearchSong",
        extra_headers={"x-router": "complexsearch.kugou.com"},
    )
    lists = ((data.get("data") or {}).get("lists")) or []
    out: list[dict[str, Any]] = []
    for item in lists:
        if not item.get("FileHash"):
            continue
        out.append(
            {
                "id": int(item.get("ID") or 0),
                "hash": item.get("FileHash") or "",
                "title": item.get("SongName") or "",
                "artists": [
                    s.get("name") for s in (item.get("Singers") or []) if s.get("name")
                ],
                "album": item.get("AlbumName") or "",
                "duration_ms": int(item.get("Duration") or 0) * 1000,
            }
        )
    return out


def _kg_lyric(cand: dict[str, Any]) -> str:
    keyword = _to_hans(f"{'、'.join(cand.get('artists') or [])} - {cand.get('title', '')}")
    data = _kg_get(
        KG_LYRIC_SEARCH_URL,
        {
            "album_audio_id": cand["id"],
            "duration": cand["duration_ms"],  # milliseconds
            "hash": cand.get("hash", ""),
            "keyword": keyword,
            "lrctxt": "1",
            "man": "no",
        },
        "Lyric",
    )
    candidates = data.get("candidates") or []
    if not candidates:
        return ""
    pick = candidates[0]  # kugou ranks its own candidates; top one first (LDDC too)
    blob = _kg_get(
        KG_LYRIC_DOWNLOAD_URL,
        {
            "accesskey": pick.get("accesskey", ""),
            "charset": "utf8",
            "client": "mobi",
            "fmt": "lrc",
            "id": pick.get("id", ""),
            "ver": "1",
        },
        "Lyric",
    )
    if not blob.get("content"):
        return ""
    try:
        content = base64.b64decode(blob["content"]).decode("utf-8")
    except Exception:
        return ""
    return content if re.search(r"\[\d+:\d{2}", content) else ""


# ---------------------------------------------------------------------------
# LRCLIB (keyless)
# ---------------------------------------------------------------------------

def _lrclib_json(url: str) -> Any:
    """GET + parse with one gentle retry: lrclib answers 503 under load."""
    try:
        return _http_json(url, headers=LRCLIB_HEADERS)
    except LyricsNetError as exc:
        if "HTTP Error 5" not in str(exc):  # only transient 5xx
            raise
        time.sleep(LRCLIB_RETRY_S)
        return _http_json(url, headers=LRCLIB_HEADERS)


def _lrclib_fetch(track: Track) -> str:
    queries = search_titles(track)
    if not queries:
        return ""
    params: dict[str, str] = {"track_name": _to_hans(queries[0])}
    artist_hint = _to_hans((track.artist or "").strip())
    if artist_hint:
        params["artist_name"] = artist_hint
    url = f"{LRCLIB_BASE}/search?{urllib.parse.urlencode(params)}"
    items = _lrclib_json(url)
    _note_ok(_host(url))
    if not isinstance(items, list) or not items:
        # retry without the artist hint (channel names rarely match)
        params.pop("artist_name", None)
        url = f"{LRCLIB_BASE}/search?{urllib.parse.urlencode(params)}"
        items = _lrclib_json(url)
        _note_ok(_host(url))
    if not isinstance(items, list):
        return ""

    candidates = [
        {
            "title": item.get("name") or "",
            "artists": [item.get("artistName") or ""],
            "duration_ms": int(item.get("durationSec") or 0) * 1000,
            "synced": item.get("syncedLyrics") or "",
        }
        for item in items
        if isinstance(item, dict)
    ]
    best = best_candidate(track, candidates)
    if best is None and len(candidates) == 1:
        best = candidates[0]  # single result: close enough for LRCLIB
    return (best or {}).get("synced", "")


# ---------------------------------------------------------------------------
# provider
# ---------------------------------------------------------------------------

def _to_traditional(text: str) -> str:
    """简体 -> 繁體（台灣用字）; timing tags stay put, zh_keep.txt names stay as-is.
    日文歌詞原樣保留（textnorm.is_japanese）。"""
    if not config.LYRICS_S2T or not text or textnorm.is_japanese(text):
        return text
    return textnorm.to_traditional(text)


def _ne_source_id(track: Track) -> int | None:
    """NetEase song id when the audio itself came from NetEase.

    Aggregates hand out `source_id="netease_<id>"` — lyrics fetched with that
    id come from the very catalogue entry the audio was taken from, so they
    match the arrangement instead of merely the song title.
    """
    match = re.match(r"netease_(\d+)$", track.source_id or "")
    return int(match.group(1)) if match else None


_PLATFORM_PREFIX = {"netease": "wy", "tencent": "tx"}


def audio_entry(track: Track, audio: dict[str, Any] | None) -> tuple[str, str]:
    """(platform, song id) of the recording that is actually played.

    `audio` is the track manifest: acquire records `audio_platform` /
    `audio_song_id` there — which may differ from the queued `source_id`
    when the audio came from the same song on another platform, and is
    empty for keyword / YouTube audio (no catalogue entry → search).  Tracks
    cached before that bookkeeping fall back to `source_id`."""
    if audio and "audio_platform" in audio:
        return str(audio.get("audio_platform") or ""), str(audio.get("audio_song_id") or "")
    source, _, song_id = (track.source_id or "").partition("_")
    return _PLATFORM_PREFIX.get(source, ""), song_id


def _fetch(
    track: Track, audio: dict[str, Any] | None = None
) -> tuple[str, str, dict[str, Any]] | None:
    """Return (format, content, meta) from the network, or None.  Runs in a thread.

    逐字優先: the providers that can ship word timings (NetEase YRC, QQ QRC) are
    asked in order — NetEase leads, most aggregate audio is netease-sourced —
    and the first word-timed answer wins immediately.  The first *usable*
    line-timed answer is held back rather than returned, so the hunt for 逐字
    goes on; only once every word-by-word source has failed is that held
    answer handed over (Kugou / LRCLIB, which only ever serve line-level LRC,
    are reached last and only when nothing was held).  `meta` records the
    provider, how the song was found (`via`), the duration its catalogue
    believes in and the rendering level (`timing`).

    歌詞跟著音檔走: `audio` (the manifest) names the catalogue entry the
    audio really came from.  QQ audio asks QQ by that id first, NetEase
    audio NetEase by id; searches compare against the measured audio length
    (`audio_duration_ms`) so another arrangement's lyrics are not taken.
    """
    platform, song_id = audio_entry(track, audio)
    measured = int((audio or {}).get("audio_duration_ms") or 0)
    if measured > 0 and measured != track.duration_ms:
        track = track.model_copy(update={"duration_ms": measured})
    ne_id = int(song_id) if platform == "wy" and song_id.isdigit() else None
    # QQ entries: songmid since 2026-10-18, the numeric id before that
    qq_id = int(song_id) if platform == "tx" and song_id.isdigit() else None
    qq_mid = song_id if platform == "tx" and song_id and not song_id.isdigit() else ""
    line_hit: tuple[str, str, dict[str, Any]] | None = None  # held 逐行 answer
    ne_answered = False  # NetEase already told us it only has line-level
    qq_answered = False  # same for QQ

    def consider(
        flavours: list[tuple[str, str]],
        provider: str,
        via: str,
        duration_ms: int,
    ) -> tuple[str, str, dict[str, Any]] | None:
        """Word-timed hit (return at once), else hold the first line-level one."""
        nonlocal line_hit
        for fmt, content in flavours:
            timing = timing_of(fmt, content)  # "" = unusable answer
            if not timing:
                continue
            if timing == "word":
                return fmt, content, {
                    "provider": provider,
                    "via": via,
                    "duration_ms": duration_ms,
                    "timing": "word",
                }
            if line_hit is None:  # keep the first, providers are ordered
                line_hit = fmt, content, {
                    "provider": provider,
                    "via": via,
                    "duration_ms": duration_ms,
                    "timing": "line",
                }
        return None

    # 0) QQ — the audio is QQ's own entry: its lyrics by that very id
    if qq_id is not None or qq_mid:
        try:
            word, line = _qq_lyrics(
                {
                    "id": qq_id or 0,
                    "mid": qq_mid,
                    # QQ's catalogue is 简体 — names travel the way it stores them
                    "title": _to_hans(track.title),
                    "artists": [_to_hans(a) for a in re.split(r"\s*/\s*", track.artist or "") if a],
                    "album": "",
                    "duration_ms": track.duration_ms or 0,
                }
            )
            hit = consider(
                [("qrc", word), ("lrc", line)], "qq", "source_id", track.duration_ms or 0
            )
            if hit:
                return hit
            qq_answered = line_hit is not None
        except Exception:
            log.warning("lyrics qq by-id failed for %s", track.id, exc_info=True)

    # 1) NetEase — exact catalogue entry: one reply carries YRC + LRC
    if ne_id is not None:
        try:
            yrc, lrc = _ne_lyric({"id": ne_id})
            hit = consider(
                [("yrc", yrc), ("lrc", lrc)], "ne", "source_id", track.duration_ms or 0
            )
            if hit:
                return hit
            # the by-id fetch answered for this very song: asking NetEase to
            # search it again would only buy the same catalogue entry back
            ne_answered = line_hit is not None
        except Exception:  # transport failed — search is still worth a try
            log.warning("lyrics netease by-id failed for %s", track.id, exc_info=True)

    # 2) NetEase — search + score (only when by-id never answered)
    if not ne_answered:
        try:
            for query in search_titles(track)[:2]:
                picked = best_candidate(track, _ne_search(query))
                if picked is not None:
                    yrc, lrc = _ne_lyric(picked)
                    hit = consider(
                        [("yrc", yrc), ("lrc", lrc)],
                        "ne",
                        "search",
                        picked.get("duration_ms", 0),
                    )
                    if hit:
                        return hit
                    break  # matched a song but it has no word-timed lyrics yet
        except Exception:
            log.warning("lyrics netease search failed for %s", track.id, exc_info=True)

    # 3) QQ Music — the other provider that ships word-by-word QRC
    try:
        picked: dict[str, Any] | None = None
        # skipped when QQ already answered for the very entry we play
        for query in [] if qq_answered else search_titles(track)[:2]:
            picked = best_candidate(track, _qq_search_gently(query))
            if picked is not None:
                break
        if picked is not None:
            word, line = _qq_lyrics(picked)  # shape-identified, not name-identified
            hit = consider(
                [("qrc", word), ("lrc", line)],
                "qq",
                "search",
                picked.get("duration_ms", 0),
            )
            if hit:
                return hit
    except Exception:  # fall through to the next provider
        log.warning("lyrics qq search failed for %s", track.id, exc_info=True)

    # 4) no 逐字 anywhere: the providers below only serve line-level, so a
    #    held answer is already as good as it gets
    if line_hit is not None:
        return line_hit

    # 5) Kugou — line-level LRC
    try:
        for query in search_titles(track)[:2]:
            picked = best_candidate(track, _kg_search(query))
            if picked is not None:
                hit = consider(
                    [("lrc", _kg_lyric(picked))],
                    "kg",
                    "search",
                    picked.get("duration_ms", 0),
                )
                if hit:
                    return hit
                if line_hit is not None:
                    return line_hit  # kugou answered 逐行: as good as it gets
                break
    except Exception:
        log.warning("lyrics kugou failed for %s", track.id, exc_info=True)

    # 6) LRCLIB — line-level LRC, last resort
    try:
        hit = consider([("lrc", _lrclib_fetch(track))], "lrclib", "search", 0)
        if hit:
            return hit
    except Exception:
        log.warning("lyrics lrclib failed for %s", track.id, exc_info=True)
    return line_hit


# ---------------------------------------------------------------------------
# hand-picked lyrics (admin song cache page)
# ---------------------------------------------------------------------------

PICK_PROVIDERS = {"ne": "網易雲", "qq": "QQ 音樂", "kg": "酷狗"}


def pick_candidates(provider: str, query: str, track: Track) -> list[dict[str, Any]]:
    """Search one provider for `query`; every hit with its match score.

    Runs in a worker thread.  Unlike the automatic chain nothing is filtered:
    the admin sees the covers and live versions too and picks by ear."""
    search = {"ne": _ne_search, "qq": _qq_search_gently, "kg": _kg_search}.get(provider)
    if search is None:
        raise LyricsNetError(f"unknown lyrics provider {provider!r}")
    out = []
    for cand in search(query)[:15]:
        score = score_candidate(
            track.title, track.artist, track.duration_ms,
            cand.get("title", ""), cand.get("artists", []), cand.get("duration_ms", 0),
        )
        out.append({**cand, "provider": provider, "score": round(score, 1),
                    "title": _to_traditional(cand.get("title", "")),
                    "artists": [_to_traditional(a) for a in cand.get("artists") or []]})
    return out


def pick_fetch(provider: str, cand: dict[str, Any]) -> tuple[str, str, str] | None:
    """(format, content, timing) for one picked candidate, 逐字 first; None = no
    synced lyrics there.  Runs in a worker thread; the text comes back 繁體."""
    if provider == "ne":
        flavours = list(zip(("yrc", "lrc"), _ne_lyric(cand)))
    elif provider == "qq":
        flavours = list(zip(("qrc", "lrc"), _qq_lyrics(cand)))
    elif provider == "kg":
        flavours = [("lrc", _kg_lyric(cand))]
    else:
        raise LyricsNetError(f"unknown lyrics provider {provider!r}")
    usable = [(fmt, text, timing_of(fmt, text)) for fmt, text in flavours if text]
    usable = [hit for hit in usable if hit[2]]
    if not usable:
        return None
    fmt, text, timing = next((hit for hit in usable if hit[2] == "word"), usable[0])
    return fmt, _to_traditional(text), timing


class NetworkLyricsProvider:
    """Resolves lyrics for a track over the network; `None` on any failure."""

    def __init__(self, cache: TrackCache | None = None) -> None:
        self.cache = cache or TrackCache()

    async def resolve(self, track: Track) -> Path | None:
        if not config.LYRICS_NET:
            return None
        existing = self.cache.lyrics_path(track.id)
        if existing is not None:
            return existing  # manual/local lyrics always win
        try:
            # the manifest names the catalogue entry the audio came from
            audio = self.cache.read_manifest(track.id)
            result = await asyncio.wait_for(
                asyncio.to_thread(_fetch, track, audio),
                timeout=config.LYRICS_TIMEOUT_S,
            )
        except TimeoutError:
            log.warning("network lyrics timed out after %ss for %s", config.LYRICS_TIMEOUT_S, track.id)
            return None
        except Exception:  # offline / API change — never fatal
            log.warning("network lyrics failed for %s", track.id, exc_info=True)
            return None
        if result is None:
            return None
        fmt, content, *rest = result
        meta = rest[0] if rest and isinstance(rest[0], dict) else {}
        content = _to_traditional(content)
        if not content.strip():
            return None
        directory = self.cache.dir_for(track.id)
        directory.mkdir(parents=True, exist_ok=True)
        if self.cache.lyrics_path(track.id) is not None:
            return None  # a manual upload landed while we were fetching
        target = directory / f"lyrics.{fmt}"
        target.write_text(content, encoding="utf-8")
        if meta.get("provider"):
            self.cache.update_manifest(
                track.id,
                lyrics_provider=meta["provider"],
                # source_id | search — how the song behind these lyrics was found
                lyrics_via=meta.get("via") or "",
                # word | line — which render the display should use
                lyrics_timing=meta.get("timing") or timing_of(fmt, content),
                lyrics_duration_ms=int(meta.get("duration_ms") or 0),
            )
        return target
