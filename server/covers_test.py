"""熱門榜封面 tests — 比對規則 + 查詢佇列/快取（全離線，平台與圖片下載都換成假的）.

Run:  .venv/Scripts/python server/covers_test.py
Live: KTV_LIVE_COVERS=1 再對每家平台各查一首歌（約 8 個請求，手動跑）。
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ktv import config, covers

CACHE_DIR = config.ROOT / "temp" / "covers_test"
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 2000


def ok(condition: bool, label: str) -> None:
    print(f"  [{'OK ' if condition else 'FAIL'}] {label}")
    if not condition:
        raise SystemExit(1)


def wait_for(songs: list[tuple[str, str]], timeout: float = 5.0) -> list[str | None]:
    deadline = time.time() + timeout
    while True:
        got = covers.lookup(songs)
        if all(v is not None for v in got) or time.time() > deadline:
            return got
        time.sleep(0.05)


def test_matching() -> None:
    print("matching")
    ok(covers.artist_names("李榮浩、張惠妹(aMEI)") == ["李榮浩", "張惠妹", "amei"], "artist split")
    ok(covers.matches("對等關係", "李榮浩、張惠妹(aMEI)", "对等关系", ["李荣浩", "张惠妹"]), "簡繁 + 合唱")
    ok(covers.matches("天后", "陳勢安", "天后 (Live)", ["陈势安"]), "括號版本")
    ok(not covers.matches("天后", "陳勢安", "天后", ["王菲"]), "同名不同歌手不收")
    ok(not covers.matches("我懷念的", "孫燕姿", "懷念", ["孙燕姿"]), "歌名不同不收")
    ok(covers.matches("Shape of You", "Ed Sheeran", "Shape of You", ["Ed Sheeran"]), "英文")
    ok(covers.matches("My Heart Will Go On", "Celine Dion", "My Heart Will Go On (Love Theme from \"Titanic\")", ["Céline Dion"]), "重音符號")
    ok(covers.artist_names("平井  堅") == ["平井堅"], "歌手名中間的空白")
    ok(covers._query("對等關係", "李榮浩、張惠妹(aMEI)") == "對等關係 李榮浩", "query 用第一位歌手")


def test_queue() -> None:
    print("queue + cache")
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    covers.reset(CACHE_DIR)
    calls: list[str] = []

    def tx(query: str):
        calls.append(f"tx:{query}")
        if query.startswith("甲乙丙丁"):
            return [("甲乙丙丁", ["李佳薇"], "https://img.test/a.jpg")]
        if query.startswith("壞掉"):
            raise covers.CoverError("tx down")
        return [("別首歌", ["李佳薇"], "https://img.test/x.jpg")]

    def itunes(query: str):
        calls.append(f"itunes:{query}")
        if query.startswith("Hello"):
            return [("Hello", ["Adele"], "https://img.test/b.jpg")]
        if query.startswith("壞掉"):
            raise covers.CoverError("itunes down")
        return []

    covers.PROVIDERS[:] = [("tx", tx), ("itunes", itunes)]
    covers._http = lambda url, **kw: JPEG  # type: ignore[assignment]

    songs = [("甲乙丙丁", "李佳薇"), ("Hello", "Adele"), ("查無此歌", "某人"), ("壞掉", "某人")]
    first = covers.lookup(songs)
    ok(first == [None, None, None, None], "第一次全部排隊中")
    got = wait_for(songs)
    ok(bool(got[0]) and got[0].endswith(".jpg"), "QQ 找到")
    ok(bool(got[1]), "QQ 歌名不對 → 換 iTunes 找到")
    ok(got[2] == "", "每家查無 → 空字串")
    ok(got[3] == "", "全部出錯 → 先當查無")
    path = covers.image_path(got[0] or "")
    ok(path is not None and path.read_bytes() == JPEG, "圖存到本機")
    ok(covers.image_path("../index.json") is None, "檔名驗證")

    covers._save(force=True)
    covers.reset()
    before = len(calls)
    ok(covers.lookup(songs) == got, "重開後從 index.json 讀回")
    time.sleep(0.2)
    ok(len(calls) == before, "已查過的不再打 API")

    index = covers._load()
    index[covers.key_of("壞掉", "某人")]["t"] = time.time() - covers.ERROR_RETRY_S - 1
    index[covers.key_of("查無此歌", "某人")]["t"] = time.time() - covers.ERROR_RETRY_S - 1
    again = covers.lookup(songs)
    ok(again[3] is None and again[2] == "", "出錯一天後重查，查無要 30 天")


def test_evict() -> None:
    print("evict")
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    covers.reset(CACHE_DIR)
    covers.PROVIDERS[:] = [("tx", lambda q: [(q.split()[0], ["歌手"], "https://img.test/c.jpg")])]
    covers._http = lambda url, **kw: JPEG  # type: ignore[assignment]
    songs = [(f"歌{i}", "歌手") for i in range(5)]
    ok(covers.MAX_BYTES == 0, "預設不設上限")
    wait_for(songs)
    ok(len(list((CACHE_DIR / "img").iterdir())) == 5, "不設上限全部留著")
    covers.MAX_BYTES = len(JPEG) * 3
    covers.lookup([("歌5", "歌手")])
    wait_for([("歌5", "歌手")])
    ok(len(list((CACHE_DIR / "img").iterdir())) <= 3, "設了上限會刪舊的")
    covers.MAX_BYTES = 0


def test_search_covers() -> None:
    print("search covers")
    from ktv.providers import registry

    ok(not covers.image_host_ok("https://y.gtimg.cn/a.jpg"), "沒外掛：平台 CDN 不收")
    ok(covers.image_host_ok("https://i.ytimg.com/vi/x/hqdefault.jpg"), "YouTube 縮圖照收")
    registry.register_image_hosts("y.gtimg.cn", ".music.126.net")  # 音源外掛會加的
    ok(covers.image_host_ok("https://y.gtimg.cn/music/photo_new/T002R150x150M000x.jpg"), "QQ CDN")
    ok(covers.image_host_ok("http://p1.music.126.net/a/b.jpg?param=150y150"), "網易 CDN")
    ok(not covers.image_host_ok("http://127.0.0.1/x.jpg"), "不收內網")
    ok(not covers.image_host_ok("https://evil.com/y.gtimg.cn.jpg"), "不收別的網域")
    ok(not covers.image_host_ok("https://xmusic.126.net.evil.com/a.jpg"), "不收後綴騙人")
    ok(not covers.image_host_ok("https://y.gtimg.cn:8080/a.jpg"), "不收指定 port")
    ok(not covers.image_host_ok("file:///etc/passwd"), "不收 file:")

    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    covers.reset(CACHE_DIR)
    hits: list[str] = []

    def fake_http(url: str, **kw):
        hits.append(url)
        if "broken" in url:
            raise covers.CoverError("404")
        return JPEG

    covers._http = fake_http  # type: ignore[assignment]
    url = "https://y.gtimg.cn/music/photo_new/T002R150x150M000AB.jpg"
    path = covers.remote_image(url)
    ok(path is not None and path.read_bytes() == JPEG, "代抓並存檔")
    ok(covers.remote_image(url) == path and len(hits) == 1, "第二次直接用快取")
    ok(covers.image_path(path.name) == path, "檔名驗證接受 u 開頭")
    ok(covers.remote_image("https://evil.com/a.jpg") is None and len(hits) == 1, "不在名單不抓")
    bad = "https://y.gtimg.cn/broken.jpg"
    ok(covers.remote_image(bad) is None and covers.remote_image(bad) is None and len(hits) == 2,
       "抓不到的記一天，不重打")


def test_backfill() -> None:
    print("backfill downloaded songs")
    import tempfile

    from ktv.models import Track
    from ktv.store import Store

    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    covers.reset(CACHE_DIR)
    store = Store(Path(tempfile.mkdtemp()) / "ktv.db")
    songs = [
        Track.from_source("catalog", "tencent_A", title="甲乙丙丁", artist="李佳薇"),
        Track.from_source("youtube", "VID123", title="某 MV", artist="頻道"),
        Track.from_source("catalog", "netease_B", title="查無此歌", artist="某人"),
        Track.from_source("catalog", "netease_C", title="沒下載", artist="某人"),
        Track.from_source("catalog", "kugou_D", title="有封面", artist="某人", cover="https://y.gtimg.cn/x.jpg"),
    ]
    for t in songs:
        store.upsert_track(t)
    ok(len(store.tracks_missing_cover()) == 4, "舊資料庫補欄位，空的算沒封面")
    calls: list[str] = []

    def tx(query: str):
        calls.append(query)
        if query.startswith("甲乙丙丁"):
            return [("甲乙丙丁", ["李佳薇"], "https://y.gtimg.cn/a.jpg")]
        if query.startswith("舊快取"):
            return [("舊快取", ["某人"], "https://y.gtimg.cn/e.jpg")]
        return []

    covers.PROVIDERS[:] = [("tx", tx)]
    covers._http = lambda url, **kw: JPEG  # type: ignore[assignment]
    done = covers.backfill(store, lambda tid: not tid.endswith("_C"))
    ok(done == 2, "補了 2 首（QQ 查到 + YouTube 縮圖）")
    ok(store.get_track("catalog_tencent_A").cover == "https://y.gtimg.cn/a.jpg", "網址寫回資料庫")
    ok(store.get_track("youtube_VID123").cover.endswith("/VID123/mqdefault.jpg"), "YouTube 用縮圖不打 API")
    ok(covers.remote_image("https://y.gtimg.cn/a.jpg") is not None, "圖已在快取，/api/covers/src 直接命中")
    ok(all(not q.startswith("沒下載") for q in calls), "沒下載完的不查")
    store.upsert_track(Track.from_source("catalog", "tencent_A", title="甲乙丙丁", artist="李佳薇"))
    ok(store.get_track("catalog_tencent_A").cover == "https://y.gtimg.cn/a.jpg", "再點一次（沒帶封面）不會洗掉")
    now = time.time()
    with covers._lock:
        covers._load()[covers.key_of("舊快取", "某人")] = {"f": "x.jpg", "t": now, "u": now}
    store.upsert_track(Track.from_source("catalog", "qq_E", title="舊快取", artist="某人"))
    covers.backfill(store, lambda tid: True)
    ok(store.get_track("catalog_qq_E").cover == "https://y.gtimg.cn/e.jpg", "熱門榜舊快取有圖沒網址：再查一次拿網址")
    old_key = covers.key_of("改名了", "某人")
    with covers._lock:
        covers._load()[old_key] = {"f": "y.jpg", "t": now, "u": now}
    store.upsert_track(Track.from_source("catalog", "qq_F", title="改名了", artist="某人"))
    covers.backfill(store, lambda tid: True)
    ok(covers._load()[old_key]["f"] == "y.jpg", "重查查無：熱門榜那張圖留著")
    before = len(calls)
    covers.backfill(store, lambda tid: True)
    ok(len(calls) == before + 1, "第二次只查還沒查過的（查無的 30 天內不重查；舊快取查無下次啟動再試）")


def test_live() -> None:
    print("live")
    from ktv.providers import registry

    registry.load()  # 音源外掛的平台（有裝的話）排在 iTunes 前面
    covers.reset(CACHE_DIR)
    for name, search in list(covers.PROVIDERS):
        started = time.time()
        try:
            cands = search("對等關係 李榮浩")
            hit = next((c for c in cands if covers.matches("對等關係", "李榮浩、張惠妹(aMEI)", c[0], c[1])), None)
            print(f"  {name}: {len(cands)} 筆, 對上={hit is not None}, {time.time() - started:.1f}s"
                  + (f", {hit[2]}" if hit else ""))
        except covers.CoverError as exc:
            print(f"  {name}: 出錯 {exc}")
    found = covers.resolve("甲乙丙丁", "李佳薇")
    print(f"  resolve 甲乙丙丁: {found[0] if found else '查無'} {len(found[1]) if found else 0} bytes")


REAL_HTTP = covers._http

if __name__ == "__main__":
    if os.environ.get("KTV_LIVE_COVERS") == "1":
        test_live()
    test_matching()
    test_queue()
    test_evict()
    test_search_covers()
    test_backfill()
    covers._http = REAL_HTTP  # type: ignore[assignment]
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    print("all ok")
