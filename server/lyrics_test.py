"""Lyric parser tests — QRC / LRC / YRC / TXT / translation merge.

Run:  .venv/Scripts/python server/lyrics_test.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ktv.lyrics_parse import attach_translations, parse, parse_translation

QRC_WRAPPED = (
    '<Lyric_1 LyricType="1" LyricContent="[ti:&#10;晴天]&#10;'
    "[27750,3180]故(27750,200)事的小黄花(27950,600)从(28550,150)出生那年就飘着(28700,1230)&#10;"
    "[30850,3000]童(30850,300)年(31150,300)的荡秋千(31450,700)&#10;"
    '[33800,600](33800,600)&#10;'
    '[35000,900]爱&amp;你(35000,900)"/>'
)

LRC_PLAIN = """[ar:Sample Singer]
[ti:Sample Song]

[00:00.00] Good evening
[00:04.50] Here we go
[00:10.00][01:20.00] Sing it again
[00:15.50] under the city lights
"""

LRC_ENHANCED = """[00:00.00]<00:15.50>When<00:15.80> I<00:16.10> call<00:16.60>
[00:20.00] plain tail
"""

YRC_SAMPLE = """[14900,1790](14900,390,0)多(15290,400,0)少(15690,500,0)年(16190,500,0)
[17000,600](17000,600,0)
[18000,900](18000,900,0)再(18450,450,0)也(18900,0,0)没有
"""


def ok(condition: bool, label: str) -> None:
    print(f"  [{'OK ' if condition else 'FAIL'}] {label}")
    if not condition:
        raise SystemExit(1)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # cp950 consoles choke on 简体
    except Exception:
        pass
    print("1. QRC: wrapper + entities + word timings")
    lines = parse("qrc", QRC_WRAPPED)
    ok(len(lines) == 3, f"3 lyric lines (marker line dropped) — got {len(lines)}")
    first = lines[0]
    ok(first["start"] == 27750 and first["end"] == 30930, "line start/end from [start,dur]")
    ok(len(first["words"]) == 4, "four words")
    ok(first["words"][0] == {"text": "故", "start": 27750, "end": 27950}, "first word timing")
    ok(first["words"][1]["text"] == "事的小黄花", "multi-char word kept as one")
    ok(lines[1]["end"] == 33850, "second line end")
    ok(lines[2]["words"][0]["text"] == "爱&你", "html entities unescaped")
    ok(all(w["end"] > w["start"] for line in lines for w in line["words"]), "every word has a span")

    print("2. QRC: raw body without the XML wrapper still parses")
    raw = "[1000,500]哈(1000,500)\n[2000,500]呵(2000,500)"
    ok(len(parse("qrc", raw)) == 2, "2 lines from raw QRC body")

    print("3. LRC: metadata ignored, multi-timestamp duplicated, end = next start")
    lines = parse("lrc", LRC_PLAIN)
    ok(len(lines) == 5, f"5 lines — got {len(lines)}")
    ok(lines[0]["start"] == 0 and lines[1]["start"] == 4500, "timestamps in ms")
    ok(lines[0]["end"] == 4500, "plain line ends where the next begins")
    repeats = [line for line in lines if line["words"][0]["text"] == "Sing it again"]
    ok(len(repeats) == 2 and repeats[1]["start"] == 80000, "repeated line for both timestamps")
    ok(lines[-1]["end"] == lines[-1]["start"] + 5000, "last line gets a fallback end")

    print("4. LRC: enhanced `<mm:ss.xx>` word tags")
    lines = parse("lrc", LRC_ENHANCED)
    words = lines[0]["words"]
    ok(len(words) == 3, f"3 timed words — got {len(words)}")
    ok(words[0] == {"text": "When", "start": 15500, "end": 15800}, "first enhanced word")
    ok(words[2]["text"] == " call" and words[2]["end"] == 16600, "last enhanced word")
    ok(lines[1]["words"][0]["text"] == "plain tail", "plain line after enhanced one")

    print("5. YRC: (start,dur,?) word tokens")
    lines = parse("yrc", YRC_SAMPLE)
    ok(len(lines) == 2, f"marker line dropped — got {len(lines)}")
    ok(lines[0]["words"][0] == {"text": "多", "start": 14900, "end": 15290}, "first word")
    ok(lines[1]["words"][-1]["text"] == "没有", "zero-duration word still rendered")

    print("6. TXT: evenly spread across the track duration")
    lines = parse("txt", "one\ntwo\nthree\nfour", duration_ms=8000)
    ok(len(lines) == 4, "4 lines")
    ok(lines[1]["start"] == 2000 and lines[3]["end"] == 8000, "spread over 8s")
    ok(parse("txt", "one\ntwo", duration_ms=0)[1]["end"] == 8000, "fallback span without duration")

    print("7. unknown / broken input returns [] instead of raising")
    ok(parse("ttml", "<tt></tt>") == [], "ttml (legacy) -> []")
    ok(parse("qrc", "") == [], "empty -> []")
    ok(parse("lrc", "\x00\x01 garbage") == [], "garbage -> []")

    print("8. translation merge by start time")
    orig = parse("lrc", "[00:04.50] Here we go\n[00:10.00] Sing it again")
    trans = parse_translation("lrc", "[00:04.50] 是我\n[00:10.02] 我在想")
    attach_translations(orig, trans)
    ok(orig[0].get("translated") == "是我", "exact start match")
    ok(orig[1].get("translated") == "我在想", "near start (<=250ms) match")

    credits_tests()
    matching_tests()
    decrypt_tests()
    s2t_tests()
    furigana_tests()
    throttle_tests()
    retry_tests()
    sig_tests()
    chain_tests()
    follow_audio_tests()
    timing_tests()
    ne_qq_flavour_tests()
    asyncio.run(provider_tests())
    print("PASS")


DUET_QRC = """[1744,809]詞(1744,128)Lyricist：(1872,505)李(2377,37)榮(2414,41)浩(2456,36)Ronghao (2491,25)Li(2516,36)
[2552,222]曲(2552,26)Composer：(2578,39)李(2616,28)榮(2645,32)浩(2677,33)Ronghao (2710,45)Li(2755,19)
[2774,277]編(2774,43)曲(2817,33)Arrangement：(2851,31)李(2882,30)榮(2911,26)浩(2938,32)Ronghao (2969,44)Li(3014,38)
[5000,2000]我(5000,500)說(5500,500)：(6000,100)愛(6100,500)你(6600,400)
[75493,238]浩：(75493,238)
[75730,4057]慢(75730,540)慢(76271,1272)走(77543,371)遠(77914,520)
[79788,1047]aMEI：(79788,1047)
[80835,3139]說(80835,179)的(81014,220)話(81234,463)
[83974,944]合：(83974,944)
[84918,7170]不(84918,781)會(85699,549)忘(86248,426)
[92000,3000]浩：(92000,300)再(92300,500)見(92800,500)
"""

LRC_CREDITS = """[00:00.00]晴天 - 周杰倫 (Jay Chou)
[00:00.50]藝術指導：程楚楚
[00:00.60]錄音師（周杰倫）Recording Engineer（Jay Chou）：楊瑞代
[00:01.00]词：周杰伦
[00:02.00]曲：周杰伦
[00:03.00]编曲：钟兴民
[00:04.50]制作人 : 周杰伦
[00:05.00]OP：JVR Music
[00:05.10]合聲：周傑倫
[00:05.20]合聲編寫：周傑倫
[00:05.30]錄音工程：楊瑞代（Alfa Studio）
[00:05.40]混音工程：楊大緯（楊大緯錄音工作室）
[00:05.50]曲 Compose：黃俊榮 Amsden/林倛玉
[00:05.60]詞 Lyrics by：吳易緯
[00:05.50]編曲：
[00:20.00]這是第一句歌詞
[00:24.00]男：從前從前有一首歌
[00:28.00]女：大家一起來唱
[03:40.00]監制：lvy Li@索尼音樂
[03:45.00]特別鳴謝：看見文化
[03:50.00]未经著作权人许可 不得翻唱翻录或使用
"""


# a credit sheet like QQ's 對等關係: bilingual labels, and 絃樂 (missing from
# the vocabulary until now) in the middle of the block
LRC_CREDIT_SHEET = """[00:00.00]對等關係 - 李榮浩/張惠妹
[00:01.70]詞Lyricist：李榮浩Ronghao Li
[00:02.50]編曲Arrangement：李榮浩Ronghao Li
[00:03.80]鼓Drum：李彥超Yanchao Li
[00:04.10]絃樂Strings：國際首席愛樂樂團International Master Philharmonic Orchestra
[00:04.50]錄音工作室Recording Studio：北京一樣音樂錄音室Beijing Young Music Studio
[00:05.00]樂手：某某
[00:05.50]神秘職稱：路人甲
[00:06.00]混音：王超敏
[00:29.40]浩：
[00:33.30]也許我 真的我 想說些什麼
[00:47.90]aMEI：
[00:48.30]那就一起唱吧
"""


def credits_tests() -> None:
    def texts(lines):
        return ["".join(w["text"] for w in line["words"]) for line in lines]

    print("8b. credit lines dropped, duet markers become singer/part")
    duet = parse("qrc", DUET_QRC, title="年少有為", artist="李榮浩/張惠妹")
    ok(texts(duet) == ["我說：愛你", "慢慢走遠", "說的話", "不會忘", "再見"],
       f"詞/曲/編曲 credits and marker lines gone — {texts(duet)}")
    ok("part" not in duet[0], "a lyric with a colon stays a plain lyric")
    ok([(l.get("singer"), l.get("part")) for l in duet[1:]]
       == [("浩", 1), ("aMEI", 2), ("合", 0), ("浩", 1)],
       "parts: first singer 1, second 2, 合 = 0, a returning singer keeps their part")
    ok(duet[-1]["start"] == 92300, "inline label stripped, line starts on its first sung word")

    lrc = parse("lrc", LRC_CREDITS, title="晴天", artist="周杰倫")
    ok(texts(parse("lrc", "[00:00.00]屋頂 - 温嵐/周杰倫\n[00:10.00]半夜睡不着覺", title="屋顶"))
       == ["半夜睡不着覺"], "header matched across 简/繁 (屋顶 vs 屋頂)")
    ok(texts(parse("lrc", "[00:00.00]雨愛（旋律DJ 版）\n[00:10.00]聽雨的聲音", title="雨愛 (DJ版)"))
       == ["聽雨的聲音"], "a bare title line at 0s is a header too")
    ok(texts(lrc) == ["這是第一句歌詞", "從前從前有一首歌", "大家一起來唱"],
       f"header / 词曲 / 制作人 / OP / 合聲 / 錄音混音工程 / copyright dropped — {texts(lrc)}")
    ok(all("singer" not in l for l in lrc[:1]), "「合聲：」is a credit, never a 合唱 marker")
    ok([l.get("part") for l in lrc] == [None, 1, 2], "男 / 女 labels in front of a line")

    sheet = parse("lrc", LRC_CREDIT_SHEET, title="對等關係", artist="李榮浩/張惠妹")
    ok(texts(sheet) == ["也許我 真的我 想說些什麼", "那就一起唱吧"],
       f"bilingual credit labels and a missed credit inside the block dropped — {texts(sheet)}")
    ok([l.get("singer") for l in sheet] == ["浩", "aMEI"], "duet labels after the credit sheet still work")
    ok(texts(parse("lrc", "[00:01.00]周杰倫Jay：\n[00:05.00]你好嗎", artist="周杰倫")) == ["你好嗎"],
       "a bilingual singer label is a duet marker, not a credit")


def matching_tests() -> None:
    from ktv.models import Track
    from ktv.providers import best_candidate, fold, score_candidate, search_titles

    print("9. candidate matching: official beats cover, wrong artist rejected")
    official_track = Track.from_source(
        "youtube",
        "sHD_z90ZKV0",
        title="周杰倫 Jay Chou【稻香 Rice Field】-Official Music Video",
        artist="周杰倫 Jay Chou",
        duration_ms=224000,
    )
    official = {"title": "稻香", "artists": ["周杰伦"], "duration_ms": 224000}
    cover = {"title": "稻香 Cover by 悠然YOU", "artists": ["AMAO Music"], "duration_ms": 224000}

    def score(cand: dict) -> float:
        return score_candidate(
            official_track.title,
            official_track.artist,
            official_track.duration_ms,
            cand["title"],
            cand["artists"],
            cand["duration_ms"],
        )

    s_official, s_cover = score(official), score(cover)
    ok(s_official > s_cover, f"official scores higher ({s_official} > {s_cover})")
    ok(s_cover < 2.0, f"same-song cover without artist match rejected ({s_cover})")
    ok(best_candidate(official_track, [cover, official]) is official, "best_candidate = official")

    ok(
        "稻香" in fold(official_track.title) and "official" not in fold(official_track.title),
        "fold strips noise but keeps the real title",
    )
    ok("cover" not in fold(cover["title"]), "cover marker stripped when folding")

    adele = Track.from_source("youtube", "YQHsXMglC9A", title="Adele - Hello", artist="Adele", duration_ms=367000)
    lionel = score_candidate("Adele - Hello", "Adele", 367000, "Hello", ["Lionel Richie"], 324000)
    ok(lionel < 2.0, f"same title, different artist + duration rejected ({lionel})")

    ok(search_titles(official_track)[0] == "稻香 Rice Field", "【…】 yields the real song title")
    ok(search_titles(adele)[0] == "Hello", '"Artist - Title" split yields the title')
    ok(len(search_titles(adele)) == 2, "second query adds the artist hint")

    from ktv.providers.lyrics_net import _to_hans

    qingtian = Track.from_source(
        "youtube", "DYptgVvkVLQ", title="周杰倫 晴天", artist="周杰倫", duration_ms=319000
    )
    ok(search_titles(qingtian)[0] == "晴天", "leading artist prefix stripped from the title")
    ok(search_titles(qingtian)[1] == "晴天 周杰倫", "second query is title + artist hint")
    ok(_to_hans("周杰倫 晴天") == "周杰伦 晴天", "provider queries are sent simplified")
    dashed = Track.from_source(
        "youtube",
        "DYptgVvkVLQ",
        title="晴天 - 周杰伦 (Jay Chou)Official MV",
        artist="周杰伦",
        duration_ms=319000,
    )
    ok(
        search_titles(dashed)[0] == "周杰伦 (Jay Chou)Official",
        'dash-tail query kept (CJK guard refuses a hollow remainder)',
    )


def decrypt_tests() -> None:
    import random
    import zlib

    from Crypto.Cipher import DES3

    from ktv import qrc_des
    from ktv.providers.lyrics_net import QRC_KEY, _decode_lyric

    print("10. QQ QRC decrypt: QQ client cipher + zlib, stock-3DES fallback")
    plain = '<Lyric_1 LyricType="1" LyricContent="[1000,500]你(1000,500)"/>'
    blob = zlib.compress(plain.encode("utf-8"))
    blob += b"\x00" * (-len(blob) % 8)  # block padding like the real service

    hexed = qrc_des.qq_des3_ecb(blob).hex()
    ok(_decode_lyric(hexed) == plain, "QQ client cipher round-trip")

    stock = DES3.new(QRC_KEY, DES3.MODE_ECB).encrypt(blob).hex()
    ok(_decode_lyric(stock) == plain, "stock 3DES blob accepted via fallback")

    comp = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    raw_blob = comp.compress(plain.encode("utf-8")) + comp.flush()
    raw_blob += b"\x00" * (-len(raw_blob) % 8)
    hexed_raw = qrc_des.qq_des3_ecb(raw_blob).hex()
    ok(_decode_lyric(hexed_raw) == plain, "raw-deflate payload accepted")

    ok(_decode_lyric(plain) == plain, "plain QRC passes through untouched")
    ok(_decode_lyric("[00:01.00] hi") == "[00:01.00] hi", "plain LRC passes through")
    ok(_decode_lyric("") == "" and _decode_lyric("not-hex!") == "", "junk -> empty")

    # a real GetPlayLyricInfo blob (copyrighted lyrics, so it lives with the
    # private plug-in's tests, not in this repo) — checked when present
    fixture = Path(__file__).resolve().parent / "plugins" / "ktv_sources" / "tests" / "testdata" / "qq_qrc_97773.hex"
    if fixture.exists():
        real = _decode_lyric(fixture.read_text(encoding="utf-8"))
        ok("[ti:晴天]" in real, "real QQ blob (晴天) decrypts to QRC XML")
        real_lines = parse("qrc", real)
        joined = ["".join(w["text"] for w in line["words"]) for line in real_lines]
        ok(any(len(line["words"]) > 3 and text for line, text in zip(real_lines, joined)),
           "real QRC has word-timed lines")
        ok(len(real_lines) >= 10, f"real QRC parses to >=10 lines — got {len(real_lines)}")
    else:
        print("  [skip] real QQ blob fixture not present (ships with the plug-in)")

    # standard variant must be bit-identical with pycryptodome (independent impls)
    rng = random.Random(20260929)
    for i in range(3):
        key = bytes(rng.randrange(256) for _ in range(24))
        msg = bytes(rng.randrange(256) for _ in range(64))
        ref = DES3.new(key, DES3.MODE_ECB).encrypt(msg)
        ok(qrc_des.des3_ecb(key, msg) == ref, f"standard 3DES matches pycryptodome #{i}")
        ok(
            qrc_des.des3_ecb(key, ref, decrypt=True) == msg,
            f"standard 3DES decrypt round-trip #{i}",
        )
    qq = qrc_des.qq_des3_ecb(msg)
    ok(qq != ref, "QQ client cipher differs from stock 3DES")


def throttle_tests() -> None:
    import time

    from ktv.providers import lyrics_net as ln

    print("12. provider pacing: per-host gap + back-off after repeated errors")
    saved_gap = ln.HOST_GAP_S
    saved = (dict(ln._slots), dict(ln._failures), dict(ln._cooldowns))
    try:
        ln._slots.clear()
        ln._failures.clear()
        ln._cooldowns.clear()

        ln.HOST_GAP_S = 0.15
        start = time.monotonic()
        ln._throttle("https://one.test/a")
        ln._throttle("https://one.test/b")
        elapsed = time.monotonic() - start
        # sleep() can wake a hair early on Windows' timer granularity — the
        # point is "roughly HOST_GAP_S apart", not an exact 0.15
        ok(0.13 <= elapsed < 2.0, f"same host spaced by HOST_GAP_S ({elapsed:.2f}s)")

        start = time.monotonic()
        ln._throttle("https://two.test/a")
        ok(time.monotonic() - start < 0.1, "different host not delayed")

        for _ in range(ln.COOLDOWN_AFTER):
            ln._note_fail("one.test")
        try:
            ln._throttle("https://one.test/c")
            ok(False, "host in back-off must refuse")
        except ln.LyricsNetError:
            ok(True, f"{ln.COOLDOWN_AFTER} failures -> {int(ln.COOLDOWN_S)}s back-off")

        ln._note_ok("one.test")
        ok(ln._failures["one.test"] == 0, "a success resets the failure counter")
        ln._cooldowns["one.test"] = 0.0
        ln._throttle("https://one.test/d")
        ok(True, "back-off expiry allows requests again")

        # app-level errors (qq code=2001) must accumulate even though the
        # HTTP round-trip itself succeeded — regression guard
        ln._slots.clear()
        ln._failures.clear()
        ln._cooldowns.clear()
        original_http = ln._http
        ln._http = lambda *a, **k: b'{"code":0,"request":{"code":2001}}'
        try:
            raised = 0
            for _ in range(ln.COOLDOWN_AFTER):
                try:
                    ln._qq_call("AnyMethod", "music.anything.Anything", {})
                except ln.LyricsNetError:
                    raised += 1
            host = ln._host(ln.QQ_ENDPOINT)
            ok(raised == ln.COOLDOWN_AFTER, "app errors surface as LyricsNetError")
            ok(ln._failures.get(host, 0) >= ln.COOLDOWN_AFTER,
               f"transport-ok + app-error accumulates ({ln._failures.get(host, 0)})")
            try:
                ln._throttle(ln.QQ_ENDPOINT)
                ok(False, "armed back-off must refuse qq")
            except ln.LyricsNetError:
                ok(True, "3x code=2001 -> qq in back-off")
        finally:
            ln._http = original_http
    finally:
        ln.HOST_GAP_S = saved_gap
        ln._slots.clear()
        ln._failures.clear()
        ln._cooldowns.clear()
        ln._slots.update(saved[0])
        ln._failures.update(saved[1])
        ln._cooldowns.update(saved[2])


def retry_tests() -> None:
    from ktv.providers import lyrics_net as ln

    print("14. gentle retries: one for qq 2001 / lrclib 503, never more")
    saved_delays = ln.QQ_2001_RETRY_S, ln.LRCLIB_RETRY_S
    ln.QQ_2001_RETRY_S = 0.0
    ln.LRCLIB_RETRY_S = 0.0
    original_call = ln._qq_call
    original_json = ln._http_json
    try:
        calls = {"n": 0}

        def qq_flaky(method: str, module: str, param: dict) -> dict:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ln.LyricsNetError("qq request code=2001")
            return {"body": {"item_song": []}}

        ln._qq_call = qq_flaky
        out = ln._qq_search_gently("q")
        ok(out == [] and calls["n"] == 2, f"code=2001 retried once -> 2 calls (got {calls['n']})")

        def qq_broken(method: str, module: str, param: dict) -> dict:
            calls["n"] += 1
            raise ln.LyricsNetError("qq code=-1")

        ln._qq_call = qq_broken
        calls["n"] = 0
        try:
            ln._qq_search_gently("q")
            ok(False, "other qq errors must raise")
        except ln.LyricsNetError:
            ok(calls["n"] == 1, f"non-2001 error not retried — {calls['n']} call(s)")

        def lrclib_flaky(url: str, **kwargs: object) -> object:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ln.LyricsNetError("HTTP Error 503: Service Unavailable")
            return []

        ln._http_json = lrclib_flaky
        calls["n"] = 0
        got = ln._lrclib_json("https://lrclib.net/api/search")
        ok(got == [] and calls["n"] == 2, f"503 retried once -> 2 calls (got {calls['n']})")

        def lrclib_bad(url: str, **kwargs: object) -> object:
            calls["n"] += 1
            raise ln.LyricsNetError("bad json")

        ln._http_json = lrclib_bad
        calls["n"] = 0
        try:
            ln._lrclib_json("https://lrclib.net/api/search")
            ok(False, "non-5xx must raise")
        except ln.LyricsNetError:
            ok(calls["n"] == 1, f"non-5xx error not retried — {calls['n']} call(s)")
    finally:
        ln._qq_call = original_call
        ln._http_json = original_json
        ln.QQ_2001_RETRY_S, ln.LRCLIB_RETRY_S = saved_delays


def sig_tests() -> None:
    import re

    from ktv.providers import lyrics_net as ln

    print("15. request signatures: zzc sign / eapi seal / kg md5")
    body = '{"comm":{"ct":"11"},"x":1}'
    sign = ln._qq_sign(body)
    ok(re.fullmatch(r"zzc[0-9a-z]+", sign) is not None, f"zzc format — {sign[:24]}…")
    ok(ln._qq_sign(body) == sign, "zzc deterministic for one body")
    ok(ln._qq_sign(body + " ") != sign, "zzc changes with the body")

    sealed = ln._ne_eapi_params(ln.NE_SEARCH_PATH, {"keyword": "晴天", "limit": 10})
    ok(re.fullmatch(r"[0-9A-F]+", sealed), "eapi params are uppercase hex")
    try:
        from Crypto.Cipher import AES

        raw = AES.new(ln._NE_AES_KEY, AES.MODE_ECB).decrypt(bytes.fromhex(sealed))
        pad = raw[-1]
        text = raw[:-pad].decode("utf-8")
        path, payload, digest = text.split("-36cd479b6b5-")
        import hashlib

        expect = hashlib.md5(
            f"nobody{path}use{payload}md5forencrypt".encode()
        ).hexdigest()
        ok(path == ln.NE_SEARCH_PATH and digest == expect, "eapi seal verifies (md5 round-trip)")
    except Exception as exc:
        ok(False, f"eapi seal round-trip: {exc}")

    p1 = {"b": "2", "a": "1", "appid": "3116"}
    p2 = {"a": "1", "appid": "3116", "b": "2"}
    sig1, sig2 = ln._kg_signature(p1), ln._kg_signature(p2)
    ok(sig1 == sig2, "kg signature ignores dict order")
    ok(len(sig1) == 32, "kg signature is an md5 hex digest")
    ok(ln._kg_signature({**p1, "a": "9"}) != sig1, "kg signature tracks content")


def chain_tests() -> None:
    from ktv.models import Track
    from ktv.providers import lyrics_net as ln

    print("16. chain: 逐字優先 (ne yrc -> qq qrc), 逐行留作 fallback, via+timing")
    track = Track(id="chain16", title="测试歌", artist="测试人", duration_ms=200000)
    ne_track = Track(
        id="catalog_netease_987",
        source="catalog",
        source_id="netease_987",
        title="测试歌",
        artist="测试人",
        duration_ms=200000,
    )
    yrc = "[14980,3300](14980,630,0)翻(15610,300,0)着"
    lrc = "[00:14.98]翻着我们的照片"
    qrc = '<Lyric_1 LyricType="1" LyricContent="[14980,3300]翻(14980,630)着(15610,300)"/>'
    saved = (
        ln.search_titles, ln._qq_search_gently, ln._qq_lyrics,
        ln._ne_search, ln._ne_lyric, ln._kg_search, ln._kg_lyric,
        ln._lrclib_fetch,
    )
    calls: list[str] = []
    cand = {"id": 1, "title": "测试歌", "artists": ["测试人"], "duration_ms": 200000}
    ln.search_titles = lambda _t: ["q1"]

    def qq_down(_q):
        calls.append("qq.search")
        raise ln.LyricsNetError("throttled")

    def qq_up(_q):
        calls.append("qq.search")
        return [cand]

    ln._ne_search = lambda _q: (calls.append("ne.search"), [cand])[1]
    ln._kg_search = lambda _q: (calls.append("kg.search"), [cand])[1]
    ln._kg_lyric = lambda _c: (calls.append("kg.lyric"), lrc)[1]
    ln._lrclib_fetch = lambda _t: (calls.append("lrclib"), lrc)[1]
    try:
        # (a) source_id fast path answers 逐字 -> the chain stops right there
        ln._ne_lyric = lambda _c: (calls.append(f"ne.lyric:{_c['id']}"), (yrc, lrc))[1]
        ln._qq_search_gently = qq_up
        ln._qq_lyrics = lambda _c: (calls.append("qq.lyric"), (qrc, ""))[1]

        fmt, content, meta = ln._fetch(ne_track)
        ok(fmt == "yrc" and meta.get("provider") == "ne",
           f"ne by-id YRC wins immediately — {fmt}/{meta}")
        ok(meta.get("via") == "source_id" and meta.get("timing") == "word",
           f"via=source_id, timing=word — {meta}")
        ok(meta.get("duration_ms") == 200000, "catalogue duration recorded")
        ok(calls == ["ne.lyric:987"], f"word answer ends the chain — {calls}")

        # (b) ne only ships 逐行 -> keep hunting, QQ's QRC (逐字) beats the hold
        calls.clear()
        ln._ne_lyric = lambda _c: (calls.append(f"ne.lyric:{_c['id']}"), ("", lrc))[1]
        fmt, content, meta = ln._fetch(ne_track)
        ok(fmt == "qrc" and meta.get("provider") == "qq",
           f"QQ QRC beats the held NE LRC — {fmt}/{meta}")
        ok(meta.get("timing") == "word" and meta.get("via") == "search",
           f"word found via search — {meta}")
        ok(calls == ["ne.lyric:987", "qq.search", "qq.lyric"],
           f"NE search skipped (same song), kg/lrclib never asked — {calls}")

        # (c) ne 逐行 + qq down -> hand over the held answer, no line provider
        calls.clear()
        ln._qq_search_gently = qq_down
        fmt, content, meta = ln._fetch(ne_track)
        ok(fmt == "lrc" and meta.get("provider") == "ne",
           f"held line-level answer handed over — {fmt}/{meta}")
        ok(meta.get("timing") == "line" and meta.get("via") == "source_id",
           f"line timing, via=source_id — {meta}")
        ok("kg.search" not in calls and "lrclib" not in calls,
           f"kg/lrclib only when nothing was held — {calls}")

        # (d) plain track: ne search path answers 逐字
        calls.clear()
        ln._ne_lyric = lambda _c: (calls.append(f"ne.lyric:{_c['id']}"), (yrc, lrc))[1]
        ln._qq_search_gently = qq_up
        fmt, content, meta = ln._fetch(track)
        ok(fmt == "yrc" and meta.get("via") == "search", f"ne search path — {meta}")
        ok(calls == ["ne.search", "ne.lyric:1"], f"chain stops at ne — {calls}")

        # (e) ne search gives 逐行 only, qq has no lyrics -> ne's answer, not kg
        calls.clear()
        ln._ne_lyric = lambda _c: (calls.append(f"ne.lyric:{_c['id']}"), ("", lrc))[1]
        ln._qq_lyrics = lambda _c: (calls.append("qq.lyric"), ("", ""))[1]
        fmt, content, meta = ln._fetch(track)
        ok(meta.get("provider") == "ne" and meta.get("timing") == "line",
           f"ne line-level held through the qq attempt — {meta}")
        ok("kg.search" not in calls and "lrclib" not in calls,
           f"no line provider needed — {calls}")

        # (f) ne + qq answer nothing -> kugou supplies the line-level lyrics
        calls.clear()
        ln._ne_lyric = lambda _c: (calls.append(f"ne.lyric:{_c['id']}"), ("", ""))[1]
        ln._kg_lyric = lambda _c: (calls.append("kg.lyric"), lrc)[1]
        fmt, content, meta = ln._fetch(track)
        ok(meta.get("provider") == "kg", f"kg after ne+qq answer nothing — {meta}")
        ok(meta.get("via") == "search" and meta.get("timing") == "line",
           f"kg meta complete — {meta}")
        ok("lrclib" not in calls, f"chain stops at kg success — {calls}")

        # (g) kg misses too -> lrclib, still 逐行, still with via
        calls.clear()
        ln._kg_lyric = lambda _c: (calls.append("kg.lyric"), "")[1]
        fmt, content, meta = ln._fetch(track)
        ok(meta.get("provider") == "lrclib", f"lrclib is the last resort — {meta}")
        ok(meta.get("via") == "search" and meta.get("timing") == "line",
           f"lrclib meta complete — {meta}")
    finally:
        (
            ln.search_titles, ln._qq_search_gently, ln._qq_lyrics,
            ln._ne_search, ln._ne_lyric, ln._kg_search, ln._kg_lyric,
            ln._lrclib_fetch,
        ) = saved


def follow_audio_tests() -> None:
    from ktv.models import Track
    from ktv.providers import lyrics_net as ln

    print("16b. lyrics follow the audio: QQ audio → QQ by id; length guard")
    track = Track(id="catalog_netease_987", source="catalog", source_id="netease_987",
                  title="晴天", artist="周杰倫", duration_ms=269000)
    qrc = '<Lyric_1 LyricType="1" LyricContent="[14980,3300]故(14980,630)事(15610,300)"/>'
    yrc = "[14980,3300](14980,630,0)故(15610,300,0)事"
    saved = (ln.search_titles, ln._qq_search_gently, ln._qq_lyrics, ln._ne_search,
             ln._ne_lyric, ln._kg_search, ln._kg_lyric, ln._lrclib_fetch)
    calls: list[str] = []
    ln.search_titles = lambda _t: ["q1"]
    ln._ne_lyric = lambda c: (calls.append(f"ne.lyric:{c['id']}"), (yrc, ""))[1]
    ln._qq_lyrics = lambda c: (calls.append(f"qq.lyric:{c['id']}"), (qrc, ""))[1]
    ln._ne_search = lambda _q: (calls.append("ne.search"), [])[1]
    ln._qq_search_gently = lambda _q: (calls.append("qq.search"), [])[1]
    ln._kg_search = lambda _q: []
    ln._lrclib_fetch = lambda _t: ""
    try:
        # queued as a NetEase entry, but the audio came from QQ's same song
        audio = {"audio_via": "aggregate", "audio_platform": "tx", "audio_song_id": "97773"}
        fmt, _content, meta = ln._fetch(track, audio)
        ok(fmt == "qrc" and meta["provider"] == "qq" and meta["via"] == "source_id",
           f"QQ lyrics by the played id — {fmt}/{meta}")
        ok(calls == ["qq.lyric:97773"], f"NetEase by-id never asked for QQ audio — {calls}")

        # keyword / YouTube audio: no catalogue entry → search only
        calls.clear()
        fmt_none = ln._fetch(track, {"audio_via": "youtube", "audio_platform": ""})
        ok("ne.lyric:987" not in calls, f"no by-id fetch for keyword audio — {calls}")
        ok(fmt_none is None, "nothing found by search → None")

        # legacy cache (no audio_* fields): source_id still drives NetEase by id
        calls.clear()
        fmt, _content, meta = ln._fetch(track, {})
        ok(calls == ["ne.lyric:987"] and fmt == "yrc", f"legacy source_id path — {calls}")

        # a version 40 s longer than the measured audio is another recording
        long_cut = {"id": 5, "title": "晴天", "artists": ["周杰倫"], "duration_ms": 309000}
        right = {"id": 6, "title": "晴天", "artists": ["周杰倫"], "duration_ms": 270000}
        measured = track.model_copy(update={"duration_ms": 269500})
        ok(ln.best_candidate(measured, [long_cut, right]) is right,
           "the 309 s cut is skipped, the 270 s one taken")
        ok(ln.best_candidate(measured, [long_cut]) is None, "only a far-off length → no pick")
    finally:
        (ln.search_titles, ln._qq_search_gently, ln._qq_lyrics, ln._ne_search,
         ln._ne_lyric, ln._kg_search, ln._kg_lyric, ln._lrclib_fetch) = saved


def timing_tests() -> None:
    from ktv.lyrics_parse import timing_of, timing_of_lines

    print("17. timing classification: decided by the data, not the extension")
    ok(timing_of("qrc", QRC_WRAPPED) == "word", "QRC -> word")
    ok(timing_of("yrc", YRC_SAMPLE) == "word", "YRC -> word")
    ok(timing_of("lrc", LRC_PLAIN) == "line", "plain LRC -> line")
    ok(timing_of("lrc", LRC_ENHANCED) == "word", "enhanced LRC -> word (逐字 tags)")
    ok(timing_of("txt", "one\ntwo") == "line", "TXT -> line")
    ok(timing_of("lrc", "") == "", "empty -> unusable")
    ok(timing_of("qrc", "<QrcInfos/>") == "", "no lyric lines -> unusable")
    ok(timing_of("lrc", "no timestamps here") == "", "unsynced text -> unusable")
    ok(timing_of_lines([]) == "line", "no lines -> line (nothing renders anyway)")
    # a single-token line has no per-glyph timeline to sweep
    one_word = [{"start": 0, "end": 1000, "words": [{"text": "整行", "start": 0, "end": 1000}]}]
    ok(timing_of_lines(one_word) == "line", "one token per line -> line")


def ne_qq_flavour_tests() -> None:
    from ktv.lyrics_parse import timing_of
    from ktv.providers import lyrics_net as ln

    print("18. one request per provider: NE yields YRC+LRC, QQ yields QRC+LRC")
    saved_ne, saved_qq = ln._ne_post, ln._qq_call
    try:
        credits = '{"t":0,"c":[{"tx":"作词: "},{"tx":"方文山"}]}'
        ln._ne_post = lambda *_a, **_k: {
            "code": 200,
            "yrc": {"lyric": f"{credits}\n[14980,3300](14980,630,0)翻(15610,300,0)着"},
            "lrc": {"lyric": f"{credits}\n[00:14.98]翻着我们的照片"},
        }
        yrc, lrc = ln._ne_lyric({"id": 1})
        ok(yrc.startswith("[14980,3300]"), f"yrc handed over — {yrc[:34]!r}")
        ok(lrc.startswith("[00:14.98]"), f"lrc handed over too — {lrc[:34]!r}")
        ok('{"t"' not in yrc and '{"t"' not in lrc, "JSON credit rows stripped")
        ok(timing_of("yrc", yrc) == "word" and timing_of("lrc", lrc) == "line",
           "both flavours classify correctly")

        ln._ne_post = lambda *_a, **_k: {"code": 200, "lrc": {"lyric": "[00:01.00]hi"}}
        yrc, lrc = ln._ne_lyric({"id": 1})
        ok(yrc == "" and lrc == "[00:01.00]hi",
           "song without YRC still hands over its LRC")

        ln._ne_post = lambda *_a, **_k: {"code": 200}
        ok(ln._ne_lyric({"id": 1}) == ("", ""), "no lyric nodes -> ('', '')")
        ln._ne_post = lambda *_a, **_k: {"code": 200, "yrc": {"lyric": 42}}
        ok(ln._ne_lyric({"id": 1}) == ("", ""), "non-string lyric node -> ''")

        ln._qq_call = lambda *_a, **_k: {
            "qrc": '<Lyric_1 LyricType="1" LyricContent="[14980,3300]翻(14980,630)着(15610,300)"/>',
            "lyric": "[00:14.98]翻着我们的照片",
        }
        qrc, lyric = ln._qq_lyrics({"id": 1, "mid": "m"})
        ok(qrc.startswith("<Lyric_1"), f"qrc (逐字) — {qrc[:34]!r}")
        ok(lyric.startswith("[00:14.98]"), f"lyric (逐行) — {lyric[:34]!r}")
        ok(timing_of("qrc", qrc) == "word", "qrc classifies as word")

        ln._qq_call = lambda *_a, **_k: {"qrc": 0, "lyric": "[00:01.00]hi"}
        ok(ln._qq_lyrics({"id": 1}) == ("", "[00:01.00]hi"),
           "qrc holding a code (int) is ignored, the LRC still arrives")
        ln._qq_call = lambda *_a, **_k: {"qrc": "not-hex", "lyric": 7}
        ok(ln._qq_lyrics({"id": 1}) == ("", ""), "undecodable fields -> ('', '')")

        # QQ's real-life shape: `qrc` empty, `lyric` carrying the whole
        # <QrcInfos> XML — the 逐字 payload hides inside LyricContent
        xml = (
            '<?xml version="1.0" encoding="utf-8"?>\n<QrcInfos>\n'
            '<Lyric_1 LyricType="1" LyricContent="[ti:测试]\n'
            '[69,1992]甲(69,175)乙(244,144)"/>\n</QrcInfos>'
        )
        ln._qq_call = lambda *_a, **_k: {"qrc": "", "lyric": xml}
        word, line = ln._qq_lyrics({"id": 1})
        ok(word.startswith("<?xml"),
           f"<QrcInfos> wrapper recognised as 逐字 — {word[:30]!r}")
        ok(line == "", "…and not mistaken for line-level LRC")
        ok(timing_of("qrc", word) == "word", "its LyricContent is word-timed")
    finally:
        ln._ne_post, ln._qq_call = saved_ne, saved_qq


def furigana_tests() -> None:
    from ktv import furigana

    print("11b. 日文假名注音")

    def ja(*texts: str) -> list[dict]:
        lines = parse("qrc", "\n".join(
            f"[{i * 1000},900]" + "".join(f"{c}({i * 1000 + k * 10},10)" for k, c in enumerate(t))
            for i, t in enumerate(texts)))
        return furigana.annotate(lines)

    def shown(line: dict) -> list[tuple[str, str]]:
        text = "".join(w["text"] for w in line["words"])
        return [(text[r["at"]:r["at"] + r["len"]], r["text"]) for r in line.get("ruby", [])]

    lines = ja("これが最後（さいご）の思い出(おもいで)だよ", "(ああ)ゆっくりとめざめてく")
    text = "".join(w["text"] for w in lines[0]["words"])
    ok(text == "これが最後の思い出だよ", f"inline readings removed -> {text}")
    ok(shown(lines[0])[:3] == [("最後", "さいご"), ("思", "おも"), ("出", "で")],
       f"inline readings become ruby, okurigana skipped -> {shown(lines[0])}")
    ok(lines[0]["words"][4]["end"] == 100, "bracket time merged into the kanji before it")
    ok("".join(w["text"] for w in lines[1]["words"]).startswith("(ああ)"), "(ああ) backing vocal kept")
    zh = furigana.annotate(parse("lrc", "[00:01.00]最後（さいご）我們都一樣"))
    ok("ruby" not in zh[0] and "（さいご）" in zh[0]["words"][0]["text"], "Chinese songs untouched")
    if furigana.available():
        lines = ja("それはいつも通り", "慣れてしまえば悪くはないけど", "今日は上手に歌えた")
        ok(shown(lines[0]) == [("通", "とお")], f"auto reading, okurigana skipped -> {shown(lines[0])}")
        ok(shown(lines[1]) == [("慣", "な"), ("悪", "わる")], f"-> {shown(lines[1])}")
        ok(shown(lines[2])[:2] == [("今日", "きょう"), ("上手", "じょうず")], f"compound readings -> {shown(lines[2])}")
        lines = ja("私は東京へ行く、君を愛してる", "ちょっと待ってbaby")
        sora = "".join(seg["text"] for seg in lines[0]["sora"])
        ok(sora == "哇他西哇偷巧欸伊哭、奇咪喔阿伊西貼嚕", f"空耳: particles by sound, long vowels -> {sora}")
        ok(lines[0]["sora"][2] == {"at": 2, "len": 2, "text": "偷巧"}, "空耳 segments follow the ruby spans")
        sora = "".join(seg["text"] for seg in lines[1]["sora"])
        ok(sora == "秋偷媽貼baby", f"空耳: 拗音, っ silent, English kept -> {sora}")
        text = "".join(w["text"] for w in lines[1]["words"])
        ok([(s["at"], s["len"]) for s in lines[1]["sora"]] == [(i, 1) for i in range(len(text))],
           "空耳 segments cover the whole line")
    else:
        print("  (SudachiPy not installed — auto readings skipped)")


def s2t_tests() -> None:
    from ktv import config
    from ktv.providers.lyrics_net import _to_traditional

    print("11. 簡體 -> 繁體 conversion")
    enabled = config.LYRICS_S2T
    config.LYRICS_S2T = True
    once = _to_traditional("如果我说简化字体")
    ok("說" in once and "簡" in once, f"simplified chars converted -> {once}")
    ok(_to_traditional(once) == once, "conversion is idempotent")
    # 台灣用字, not the 港式／舊字形 zhconv used to hand out
    tw = _to_traditional("我们看着你 为了你 吃饭 床前 人群 一丝不舍 千里之外 北斗 温岚 喂")
    ok(tw == "我們看著你 為了你 吃飯 床前 人群 一絲不捨 千里之外 北斗 溫嵐 喂", f"Taiwan glyphs -> {tw}")
    # no s2twp-style vocabulary swaps
    ok(_to_traditional("软件 信息 网络") == "軟件 信息 網絡", "no vocabulary replacement")
    # already-traditional text (TW songs) comes through untouched
    ok(_to_traditional("擱淺 我們的愛 頭髮亂了") == "擱淺 我們的愛 頭髮亂了", "traditional passes through")
    # phrases split by per-word timing tags still convert as phrases
    qrc = "[0,900]剪(0,300)短(300,300)头(600,150)发(750,150)\n[900,600]舍(900,300)不(1200,150)得(1350,150)"
    ok(_to_traditional(qrc) == "[0,900]剪(0,300)短(300,300)頭(600,150)髮(750,150)\n[900,600]捨(900,300)不(1200,150)得(1350,150)",
       "timing tags lifted out before phrase matching")
    ok(_to_traditional("[00:01.00]<00:01.00>为<00:01.50>了") == "[00:01.00]<00:01.00>為<00:01.50>了", "LRC tags kept")
    # zh_keep.txt: 周杰倫 is shown exactly so, from either script, even across tags
    ok(_to_traditional("周杰伦 - 周年") == "周杰倫 - 週年", "keep list: 周杰伦 -> 周杰倫 only")
    ok(_to_traditional("周(0,1)杰(1,1)伦(2,1)") == "周(0,1)杰(1,1)倫(2,1)", "keep list across timing tags")
    # 日文歌詞不轉：内／盗／未来 是日文的正字
    ja = "[0,900]内(0,300)なる(300,600)心の影よ\n[900,600]心盗む未来を目指し、ゆっくりとめざめてく"
    ok(_to_traditional(ja) == ja, "Japanese lyrics left as-is")
    ok(_to_traditional("我们一起唱 さくら") == "我們一起唱 さくら", "a stray kana does not stop Chinese")
    config.LYRICS_S2T = False
    ok(_to_traditional("简化") == "简化", "flag off leaves text untouched")
    config.LYRICS_S2T = enabled

    # 出站查詢：繁體 -> 简体（OpenCC tw2s，內附字典）
    from ktv.textnorm import to_simplified

    hans = to_simplified("周杰倫 頭髮 後來 臺灣 台灣 為什麼 裡面 一隻 麵包")
    ok(hans == "周杰伦 头发 后来 台湾 台湾 为什么 里面 一只 面包", f"traditional -> simplified -> {hans}")
    ok(to_simplified("说好不哭 什么") == "说好不哭 什么", "simplified passes through")
    ok(to_simplified("晴天 (Live)") == "晴天 (Live)", "Latin and punctuation untouched")
    ok(to_simplified("") == "", "empty query")


async def provider_tests() -> None:
    import shutil
    import tempfile

    from ktv import config
    from ktv.models import Track
    from ktv.providers import NetworkLyricsProvider
    from ktv.providers import lyrics_net
    from ktv.store import TrackCache

    print("13. network provider: fail-soft, cached, manual lyrics win")
    tmp = Path(tempfile.mkdtemp(prefix="ktv_net_"))
    try:
        cache = TrackCache(tracks_dir=tmp)
        provider = NetworkLyricsProvider(cache)
        track = Track.from_source("youtube", "net1", title="晴天", artist="周杰伦", duration_ms=269000)

        saved_flag = config.LYRICS_NET
        config.LYRICS_NET = False
        ok(await provider.resolve(track) is None, "disabled by config -> None")
        config.LYRICS_NET = True

        original_fetch = lyrics_net._fetch

        def boom(_track: Track, _audio=None):
            raise RuntimeError("offline")

        lyrics_net._fetch = boom
        ok(await provider.resolve(track) is None, "fetch error -> None (never raises)")
        ok(cache.lyrics_path(track.id) is None, "no file written on failure")

        calls = {"n": 0}

        def fake(_track: Track, _audio=None):
            calls["n"] += 1
            return "lrc", "[00:01.00]简体测试"

        lyrics_net._fetch = fake
        path = await provider.resolve(track)
        ok(path is not None and path.name == "lyrics.lrc", "result cached as lyrics.lrc")
        ok("簡" in path.read_text(encoding="utf-8"), "simplified converted before caching")
        again = await provider.resolve(track)
        ok(again == path and calls["n"] == 1, "second resolve uses the cache (no refetch)")

        manual = cache.dir_for(track.id) / "lyrics.qrc"
        manual.write_text('<Lyric_1 LyricType="1" LyricContent="[0,100]手(0,100)"/>', encoding="utf-8")
        ok(await provider.resolve(track) == manual, "manual lyrics returned as-is")
        ok(calls["n"] == 1, "manual lyrics are never overwritten / refetched")

        # meta from _fetch lands in the manifest: provider / via / timing
        meta_track = Track.from_source(
            "youtube", "net_meta", title="晴天", artist="周杰伦", duration_ms=269000
        )

        def fake_meta(_track: Track, _audio=None):
            return "yrc", "[14980,3300](14980,630,0)多(15610,300,0)少", {
                "provider": "ne",
                "via": "source_id",
                "duration_ms": 269000,
                "timing": "word",
            }

        lyrics_net._fetch = fake_meta
        meta_path = await provider.resolve(meta_track)
        ok(meta_path is not None and meta_path.name == "lyrics.yrc",
           "yrc result cached as lyrics.yrc")
        manifest = cache.read_manifest(meta_track.id)
        ok(manifest.get("lyrics_provider") == "ne", f"manifest lyrics_provider — {manifest}")
        ok(manifest.get("lyrics_via") == "source_id", "manifest lyrics_via (source_id | search)")
        ok(manifest.get("lyrics_timing") == "word", "manifest lyrics_timing (word | line)")
        ok(manifest.get("lyrics_duration_ms") == 269000, "manifest lyrics_duration_ms")

        lyrics_net._fetch = original_fetch
        config.LYRICS_NET = saved_flag
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
