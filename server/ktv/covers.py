"""熱門榜封面：歌名＋歌手 → 專輯封面圖，查一次就永久存在本機。

KTV 站台的榜單只有歌名和歌手，封面要另外去音樂平台搜：
音源外掛註冊的平台（QQ 音樂 → 網易雲 → 酷狗）→ iTunes，前一家沒有
（或歌手對不上）才換下一家。

Rate rules:
    * 只有一條背景 worker thread，一次一個請求；同一個 host 兩次請求至少
      隔 GAP_S（搜尋 API），圖片 CDN 隔 IMG_GAP_S；
    * 只查遙控器畫面上看得到的列（前端 IntersectionObserver），排隊上限
      QUEUE_MAX，舊的請求被新的擠掉；
    * 同一個 host 連續失敗 COOLDOWN_AFTER 次就休息 COOLDOWN_S；點歌搜尋
      那邊正在退避的 host 也一併跳過；
    * 查無封面記住 MISS_RETRY_S 才再查，平台出錯則 ERROR_RETRY_S。

Storage: data/covers/index.json（key → 檔名／查無）＋ data/covers/img/*.jpg，
預設不設上限；設了 KTV_COVER_MAX_MB 才會在超過時先刪最久沒被看到的圖。
"""

from __future__ import annotations

import collections
import hashlib
import json
import logging
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from . import config
from .providers import registry
from .textnorm import fold

log = logging.getLogger(__name__)

ENABLED = os.environ.get("KTV_COVERS", "1") != "0"
COVERS_DIR = config.DATA_DIR / "covers"
IMG_DIR = COVERS_DIR / "img"
INDEX_PATH = COVERS_DIR / "index.json"

SIZE = 150  # 遙控器方塊 44px，3x 螢幕也夠
GAP_S = 1.0
IMG_GAP_S = 0.25
TIMEOUT_S = 8.0
QUEUE_MAX = 120
BATCH_MAX = 40
# 0 = 不設上限（JunHua 2026-10-28：VPS 上的封面快取不用上限）
MAX_BYTES = int(float(os.environ.get("KTV_COVER_MAX_MB", "0")) * 1024 * 1024)
MAX_IMAGE_BYTES = 400 * 1024
MISS_RETRY_S = 30 * 86400.0
ERROR_RETRY_S = 86400.0
COOLDOWN_S = 600.0
COOLDOWN_AFTER = 3
SAVE_EVERY_S = 30.0

_NAME = re.compile(r"^u?[0-9a-f]{19,20}\.(jpg|png|webp)$")
_BRACKETS = re.compile(r"[(（\[【《<].*?[)）\]】》>]")
_ARTIST_SPLIT = re.compile(r"[、,，/&＆;；+×]|\bfeat\.?|\bft\.|\bwith\b|[(（)）]", re.IGNORECASE)


HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


class CoverError(RuntimeError):
    """平台出錯（連不上、格式不對），跟「查無」分開算。"""


Candidate = tuple[str, list[str], str]  # (title, artists, image url)


# ---------------------------------------------------------------------------
# 比對
# ---------------------------------------------------------------------------

def key_of(title: str, artist: str) -> str:
    return hashlib.sha1(f"{fold(title)}\t{fold(artist)}".encode("utf-8")).hexdigest()[:20]


def _plain(text: str) -> str:
    """去掉重音符號：iTunes/QQ 寫 Céline Dion，榜單寫 Celine Dion。"""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return unicodedata.normalize("NFC", "".join(c for c in decomposed if not unicodedata.combining(c)))


def _title_fold(text: str) -> str:
    return fold(_plain(_BRACKETS.sub(" ", text))) or fold(_plain(text))


def artist_names(artist: str) -> list[str]:
    """'李榮浩、張惠妹(aMEI)' -> ['李榮浩', '張惠妹', 'amei']"""
    return [f for f in (fold(_plain(part)) for part in _ARTIST_SPLIT.split(artist or "")) if f]


def matches(title: str, artist: str, cand_title: str, cand_artists: list[str]) -> bool:
    """歌名要對上（去掉括號後相等或互相包含），有歌手的話至少一位要對上 ——
    寧可退回首字方塊，也不要掛別人的封面。"""
    want, got = _title_fold(title), _title_fold(cand_title)
    if not want or not got:
        return False
    if not (want == got or (min(len(want), len(got)) >= 2 and (want in got or got in want))):
        return False
    names = artist_names(artist)
    if not names:
        return True
    cands = [f for a in cand_artists for f in artist_names(a)]
    joined = "".join(cands)
    return any(n in joined for n in names) or any(c in fold(_plain(artist)) for c in cands if len(c) >= 2)


def _query(title: str, artist: str) -> str:
    names = [p.strip() for p in _ARTIST_SPLIT.split(artist or "") if p.strip()]
    return f"{title} {names[0]}".strip() if names else title.strip()


# ---------------------------------------------------------------------------
# HTTP（單一 worker thread，自己的節流，不佔點歌搜尋的 host 時段）
# ---------------------------------------------------------------------------

_next_at: dict[str, float] = {}
_failures: dict[str, int] = {}
_cooldowns: dict[str, float] = {}


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc


def safe_url(url: str) -> str:
    """Percent-encode what urllib refuses (non-ASCII, spaces), keep escapes."""
    if url.isascii() and " " not in url:
        return url
    return urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")


def _available(url: str) -> bool:
    host, now = _host(url), time.time()
    if _cooldowns.get(host, 0.0) > now:
        return False
    cat = registry.catalog(load_plugin=False)
    return cat is None or not cat.backing_off(host)  # 點歌搜尋正在退避就別再打


def _http(url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None,
          gap: float = GAP_S, limit: int = 2 * 1024 * 1024) -> bytes:
    host = _host(url)
    if not _available(url):
        raise CoverError(f"{host} in back-off")
    wait = _next_at.get(host, 0.0) - time.time()
    if wait > 0:
        time.sleep(wait)
    _next_at[host] = time.time() + gap
    request = urllib.request.Request(
        safe_url(url), data=data,
        headers={**HEADERS, **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            raw = response.read(limit + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _fail(host)
        raise CoverError(f"{host}: {exc}") from exc
    if len(raw) > limit:
        _fail(host)
        raise CoverError(f"{host}: reply too large")
    _failures[host] = 0
    return raw


def _fail(host: str) -> None:
    _failures[host] = _failures.get(host, 0) + 1
    if _failures[host] >= COOLDOWN_AFTER:
        _cooldowns[host] = time.time() + COOLDOWN_S
        _failures[host] = 0
        log.warning("covers: %s backing off %ds", host, COOLDOWN_S)


def _json(url: str, **kw: Any) -> Any:
    raw = _http(url, **kw)
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        _fail(_host(url))
        raise CoverError(f"{_host(url)}: non-json reply") from exc


# ---------------------------------------------------------------------------
# 平台：每家回 [(歌名, [歌手], 圖網址)]
# ---------------------------------------------------------------------------

ITUNES_API = "https://itunes.apple.com/search"


def itunes_candidates(query: str) -> list[Candidate]:
    params = {"term": query, "country": "TW", "media": "music", "entity": "song", "limit": 8}
    payload = _json(f"{ITUNES_API}?{urllib.parse.urlencode(params)}", gap=3.0)  # 官方建議 ~20 次/分
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise CoverError("itunes: reply without results")
    out: list[Candidate] = []
    for raw in payload["results"]:
        art = str(raw.get("artworkUrl100") or "") if isinstance(raw, dict) else ""
        if art:
            out.append((str(raw.get("trackName") or ""), [str(raw.get("artistName") or "")],
                        art.replace("100x100bb", f"{SIZE}x{SIZE}bb")))
    return out


# 音源外掛（providers/registry.py）註冊的平台排在內建的前面。
BUILTIN_PROVIDERS = ("itunes",)
PROVIDERS: list[tuple[str, Callable[[str], list[Candidate]]]] = [
    ("itunes", itunes_candidates),
]


def _sniff(raw: bytes) -> str:
    if raw[:3] == b"\xff\xd8\xff":
        return "jpg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return ""


def resolve(title: str, artist: str) -> tuple[str, bytes, str, str] | None:
    """(平台, 圖片 bytes, 副檔名, 圖片網址)；None = 每家都查無。全部出錯則丟 CoverError。"""
    query = _query(title, artist)
    errors = 0
    for name, search in PROVIDERS:
        try:
            cands = search(query)
            pick = next((c for c in cands if matches(title, artist, c[0], c[1])), None)
            if pick is None:
                continue
            raw = _http(pick[2], gap=IMG_GAP_S, limit=MAX_IMAGE_BYTES)
            ext = _sniff(raw)
            if not ext or len(raw) < 500:  # QQ 沒圖時回一張極小的預設圖
                continue
            return name, raw, ext, pick[2]
        except CoverError as exc:
            errors += 1
            log.info("covers: %s failed for %s: %s", name, query, exc)
    if errors == len(PROVIDERS):
        raise CoverError("every provider failed")
    return None


# ---------------------------------------------------------------------------
# 快取 + 背景 worker
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_save_lock = threading.Lock()
_index: dict[str, dict[str, Any]] | None = None
_bytes = 0
_dirty = False
_saved_at = 0.0
_queue: collections.OrderedDict[str, tuple[str, str]] = collections.OrderedDict()
_wake = threading.Event()
_worker: threading.Thread | None = None


def _load() -> dict[str, dict[str, Any]]:
    global _index, _bytes
    if _index is None:
        try:
            data = json.loads(INDEX_PATH.read_text("utf-8"))
            _index = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _index = {}
        _bytes = 0
        for key, entry in list(_index.items()):
            if entry.get("f"):
                try:
                    _bytes += (IMG_DIR / entry["f"]).stat().st_size
                except OSError:
                    del _index[key]  # 圖被刪掉了，下次再查
    return _index


def _save(force: bool = False) -> None:
    global _dirty, _saved_at
    with _save_lock:
        with _lock:
            if not _dirty or (not force and time.time() - _saved_at < SAVE_EVERY_S):
                return
            text = json.dumps(_index, ensure_ascii=False, separators=(",", ":"))
            _dirty, _saved_at = False, time.time()
        try:
            COVERS_DIR.mkdir(parents=True, exist_ok=True)
            tmp = INDEX_PATH.with_suffix(".tmp")
            tmp.write_text(text, "utf-8")
            os.replace(tmp, INDEX_PATH)
        except OSError as exc:
            log.warning("covers: index save failed: %s", exc)
            with _lock:
                _dirty = True


def _fresh(entry: dict[str, Any] | None, now: float) -> bool:
    if entry is None:
        return False
    if entry.get("f"):
        return True
    return now - float(entry.get("t", 0)) < (ERROR_RETRY_S if entry.get("e") else MISS_RETRY_S)


def lookup(songs: list[tuple[str, str]]) -> list[str | None]:
    """每首歌：圖檔名（有封面）、""（查無）、None（排隊中，晚點再問）。"""
    global _dirty
    now = time.time()
    out: list[str | None] = []
    with _lock:
        index = _load()
        for title, artist in songs[:BATCH_MAX]:
            key = key_of(title, artist)
            entry = index.get(key)
            if _fresh(entry, now):
                if entry.get("f") and now - float(entry.get("u", 0)) > 3600:
                    entry["u"] = now
                    _dirty = True
                out.append(entry.get("f") or "")
                continue
            out.append(None if ENABLED else "")
            if ENABLED:
                _queue.pop(key, None)
                _queue[key] = (title, artist)  # 新問的排最後 = 最先處理
        while len(_queue) > QUEUE_MAX:
            _queue.popitem(last=False)
    if ENABLED and any(v is None for v in out):
        _start()
    return out


def _start() -> None:
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="covers", daemon=True)
            _worker.start()
    _wake.set()


def _run() -> None:
    while True:
        with _lock:
            item = _queue.popitem(last=True) if _queue else None
        if item is None:
            _save(force=True)
            _wake.clear()
            _wake.wait(300)
            continue
        key, (title, artist) = item
        try:
            found = resolve(title, artist)
            _store(key, found[:3] if found else None, found[3] if found else "")
        except CoverError:
            _store_error(key)
        except Exception:  # noqa: BLE001 — worker 不能死
            log.exception("covers: %s %s", title, artist)
            _store_error(key)
        _save()


def _store(key: str, found: tuple[str, bytes, str] | None, src: str = "") -> None:
    global _bytes, _dirty
    now = time.time()
    if found is None:
        with _lock:
            _load()[key] = {"f": "", "t": now}
            _dirty = True
        return
    source, raw, ext = found
    name = f"{key}.{ext}"
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = IMG_DIR / f"{name}.tmp"
    tmp.write_bytes(raw)
    os.replace(tmp, IMG_DIR / name)
    with _lock:
        index = _load()
        old = index.get(key)
        if old and old.get("f") and old["f"] != name:
            _bytes -= _unlink(old["f"])
        index[key] = {"f": name, "s": source, "t": now, "u": now}
        if src:
            index[key]["src"] = src  # 圖片原網址：已下載的歌補封面時直接拿來用
        _bytes += len(raw)
        _dirty = True
        _evict()


def _store_error(key: str) -> None:
    global _dirty
    with _lock:
        _load()[key] = {"f": "", "t": time.time(), "e": 1}
        _dirty = True


def _unlink(name: str) -> int:
    path = IMG_DIR / name
    try:
        size = path.stat().st_size
        path.unlink()
        return size
    except OSError:
        return 0


def _evict() -> None:
    """超過上限就刪最久沒被看到的圖（刪掉的下次有人看到會再查）。呼叫時持有 _lock。"""
    global _bytes
    if MAX_BYTES <= 0 or _bytes <= MAX_BYTES:
        return
    index = _index or {}
    hits = sorted((float(e.get("u", 0)), k) for k, e in index.items() if e.get("f"))
    for _, key in hits:
        if _bytes <= MAX_BYTES * 0.9:
            break
        _bytes -= _unlink(index.pop(key)["f"])


def image_path(name: str) -> Path | None:
    if not _NAME.match(name):
        return None
    path = IMG_DIR / name
    return path if path.is_file() else None


# ---------------------------------------------------------------------------
# 搜尋結果的封面：網址是搜尋時平台順便給的（Track.cover），這裡只代抓圖片
# ---------------------------------------------------------------------------

# 只代抓這些圖片 CDN — 網址是遙控器送來的，不能讓人拿 VPS 去打任意網址。
# 音源外掛會再加上平台的 CDN（providers/registry.py register_image_hosts）
IMAGE_HOSTS = [
    "i.ytimg.com",      # YouTube 縮圖
    ".mzstatic.com",    # iTunes
]
_fetching: dict[str, threading.Lock] = {}
_fetch_slots = threading.BoundedSemaphore(4)


def url_key(url: str) -> str:
    return "u" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:19]


def image_host_ok(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or parts.port is not None or parts.username:
        return False
    return any(host == h.lstrip(".") or (h.startswith(".") and host.endswith(h)) for h in IMAGE_HOSTS)


def remote_image(url: str) -> Path | None:
    """Track.cover 網址 → 本機快取的圖（第一次才下載）；None = 不收這個網址或抓不到。"""
    global _bytes, _dirty
    if not ENABLED or not url or len(url) > 1000 or not image_host_ok(url):
        return None
    key = url_key(url)
    with _lock:
        entry = _load().get(key)
        if _fresh(entry, time.time()):
            if not entry.get("f"):
                return None
            if time.time() - float(entry.get("u", 0)) > 3600:
                entry["u"] = time.time()
                _dirty = True
            path = IMG_DIR / entry["f"]
            if path.is_file():
                return path
        lock = _fetching.setdefault(key, threading.Lock())
    with lock, _fetch_slots:
        with _lock:  # 等鎖的期間別人可能已經抓好了
            entry = _load().get(key)
            if entry and entry.get("f") and (IMG_DIR / entry["f"]).is_file():
                return IMG_DIR / entry["f"]
        try:
            raw = _http(url, gap=0.0, limit=MAX_IMAGE_BYTES)
            ext = _sniff(raw)
            if not ext or len(raw) < 500:
                raise CoverError("not an image")
        except CoverError as exc:
            log.info("covers: image %s failed: %s", url, exc)
            _store_error(key)
            return None
        finally:
            with _lock:
                _fetching.pop(key, None)
        _store(key, ("search", raw, ext))
    _save()
    return image_path(f"{key}.{ext}")


# ---------------------------------------------------------------------------
# 已下載的歌補封面（2026-10-28 前下載的沒有記封面網址）
# ---------------------------------------------------------------------------

def backfill(store: Any, has_source: Callable[[str], bool]) -> int:
    """資料庫裡沒封面的已下載歌曲：YouTube 的用影片縮圖，其他用歌名＋歌手
    查一次（跟熱門榜同一套比對、節流和快取），找到就把網址寫回資料庫。
    啟動時在背景跑一次；查無的 30 天內不再查。回傳補上的首數。"""
    if not ENABLED:
        return 0
    done = 0
    for track in store.tracks_missing_cover():
        if not has_source(track.id):
            continue
        if track.source == "youtube" and track.source_id:
            store.set_cover(track.id, f"https://i.ytimg.com/vi/{track.source_id}/mqdefault.jpg")
            done += 1
            continue
        key = key_of(track.title, track.artist)
        with _lock:
            entry = _load().get(key)
        if entry and entry.get("f") and entry.get("src"):  # 熱門榜查過了
            store.set_cover(track.id, entry["src"])
            done += 1
            continue
        if _fresh(entry, time.time()) and not entry.get("f"):
            continue  # 最近查無／出錯；有圖但沒記網址的舊快取就再查一次
        stale = bool(entry and entry.get("f"))  # 重查失敗也別蓋掉熱門榜那張圖
        try:
            found = resolve(track.title, track.artist)
        except CoverError:
            if not stale:
                _store_error(key)
            continue
        except Exception:  # noqa: BLE001 — 背景工作不能把服務帶走
            log.exception("covers: backfill %s", track.id)
            continue
        if found is None:
            if not stale:
                _store(key, None)
            continue
        source, raw, ext, src = found
        _store(key, (source, raw, ext), src)
        _store(url_key(src), (source, raw, ext), src)  # /api/covers/src 直接命中
        store.set_cover(track.id, src)
        done += 1
    _save(force=True)
    if done:
        log.info("covers: backfilled %d downloaded songs", done)
    return done


def reset(directory: Path | None = None) -> None:
    """Test hook — forget the in-memory index and pacing (optionally move the cache)."""
    global _index, _bytes, _dirty, COVERS_DIR, IMG_DIR, INDEX_PATH
    with _lock:
        if directory is not None:
            COVERS_DIR, IMG_DIR, INDEX_PATH = directory, directory / "img", directory / "index.json"
        _index, _bytes, _dirty = None, 0, False
        _queue.clear()
    _next_at.clear()
    _failures.clear()
    _cooldowns.clear()
