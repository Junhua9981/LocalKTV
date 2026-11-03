"""Text folding + 简繁 conversion shared by the search and lyrics paths.

Aggregates speak 简体 (catalogues, queries); KTV users and the local cache
speak 繁體 (台灣用字).  Everything crosses this module twice:

    to_simplified()  before a query leaves for an aggregate host
    to_traditional() before an aggregate result / fetched lyric comes back
    fold()           for matching — normalised, variant-free, punctuation-free

to_traditional() is OpenCC's `s2tw` (dictionaries vendored in ./opencc,
Apache-2.0): phrase-aware, Taiwan glyphs (著 為 裡 吃 床 群), and no
vocabulary swaps.  Names listed in zh_keep.txt pass through verbatim.
Timing tags inside lyrics (`字(120,300)` QRC/YRC, `[00:01.00]` LRC) are
lifted out before matching, so a phrase split into one tag per character
still converts as a phrase (头(..)发 -> 頭髮, not 頭發).

to_simplified() is OpenCC's `tw2s` from the same vendored dictionaries:
characters only, no vocabulary swaps — queries only need to be close enough
for the hosts' own search.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_OPENCC_DIR = _HERE / "opencc"
KEEP_FILE = _HERE / "zh_keep.txt"

# word chars (letters/digits/CJK) survive; whitespace and punctuation go away
_PUNCT = re.compile(r"[\W_]+")

# per-word / per-line timing tags of QRC, YRC, LRC and enhanced LRC
_TIMING = re.compile(
    r"(\[\d+,\d+\]|\[\d+:\d+(?:[.:]\d+)?\]|\(\d+,\d+(?:,\d+)?\)|<\d+:\d+(?:[.:]\d+)?>)"
)
# phrases never run across a line break (raw or XML-escaped, as in QRC)
_BREAK = re.compile(r"(\r?\n|&#?\w+;)")

# local fixes on top of OpenCC's Taiwan variants: the everyday spelling over
# the formal one (祕密 / 臺灣 / 瞭解 read odd on a KTV screen)
_TW_LOCAL = {"祕": "秘", "臺": "台", "瞭解": "了解"}


def _load(*names: str) -> dict[str, str]:
    table: dict[str, str] = {}
    for name in names:
        for line in (_OPENCC_DIR / f"{name}.txt").read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            key, _, values = line.partition("\t")
            table.setdefault(key, values.split(" ")[0])
    return table


@lru_cache(maxsize=1)
def _tables() -> tuple[dict[str, str], int, dict[str, str], int]:
    st = _load("STCharacters")
    st.update(_load("STPhrases"))  # phrases win over single characters
    tw = _load("TWVariantsPhrases", "TWVariants")
    tw.update(_TW_LOCAL)
    return st, max(map(len, st)), tw, max(map(len, tw))


def _load_reversed(name: str) -> dict[str, str]:
    """value -> key for every value of a table (OpenCC's *Rev dictionaries)."""
    table: dict[str, str] = {}
    for line in (_OPENCC_DIR / f"{name}.txt").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, _, values = line.partition("	")
        for value in values.split(" "):
            table.setdefault(value, key)
    return table


@lru_cache(maxsize=1)
def _t2s_tables() -> tuple[dict[str, str], int, dict[str, str], int]:
    tw = _load_reversed("TWVariants")
    tw.update(_load("TWVariantsRevPhrases"))  # phrases win over single characters
    ts = _load("TSCharacters")
    ts.update(_load("TSPhrases"))
    return tw, max(map(len, tw)), ts, max(map(len, ts))


def _max_match(text: str, table: dict[str, str], longest: int) -> str:
    """Forward maximum matching, OpenCC style.  Values keep their key's length."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        for size in range(min(longest, n - i), 0, -1):
            hit = table.get(text[i : i + size])
            if hit is not None:
                out.append(hit)
                i += size
                break
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def load_keep() -> dict[str, str]:
    """zh_keep.txt -> {spelling seen in input: spelling to show}."""
    keep: dict[str, str] = {}
    try:
        lines = KEEP_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return keep
    for line in lines:
        term = line.split("#", 1)[0].strip()
        if not term:
            continue
        keep[term] = term
        simplified = to_simplified(term)
        if len(simplified) == len(term):  # positions must line up with timing tags
            keep.setdefault(simplified, term)
    return keep


@lru_cache(maxsize=1)
def _keep_pattern() -> tuple[re.Pattern[str] | None, dict[str, str]]:
    keep = load_keep()
    if not keep:
        return None, keep
    alts = sorted(keep, key=len, reverse=True)
    return re.compile("|".join(map(re.escape, alts))), keep


def reload_keep() -> None:
    """Pick up an edited zh_keep.txt without restarting (tests, tools)."""
    _keep_pattern.cache_clear()


def _s2tw(plain: str) -> str:
    st, st_len, tw, tw_len = _tables()
    pattern, keep = _keep_pattern()
    pieces: list[str] = []
    pos = 0
    for m in pattern.finditer(plain) if pattern else ():
        chunk = plain[pos : m.start()]
        pieces.append(_max_match(_max_match(chunk, st, st_len), tw, tw_len))
        pieces.append(keep[m.group(0)])
        pos = m.end()
    chunk = plain[pos:]
    pieces.append(_max_match(_max_match(chunk, st, st_len), tw, tw_len))
    return "".join(pieces)


def _s2tw_tagged(segment: str) -> str:
    """Convert the words of one line, leaving its timing tags where they are."""
    parts = _TIMING.split(segment)
    words = parts[0::2]
    if len(parts) == 1:
        return _s2tw(segment)
    converted = _s2tw("".join(words))  # length-preserving, so it slices back
    out: list[str] = []
    pos = 0
    for idx, part in enumerate(parts):
        if idx % 2:
            out.append(part)
        else:
            out.append(converted[pos : pos + len(part)])
            pos += len(part)
    return "".join(out)


# 假名（不含長音符「ー」與中點，中文歌詞裡也會出現）／漢字
_KANA = re.compile(r"[\u3041-\u309f\u30a1-\u30fa\u30fd-\u30ff]")
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def is_japanese(text: str) -> bool:
    """日文（歌詞、歌名）：假名夠多，佔假名＋漢字的四分之一以上。

    中文歌裡偶爾一句「の」或一行日文不算；日文歌詞的假名通常過半。
    日文不能做簡轉繁：内→內、盗→盜、未来→未來 都是錯字。"""
    if not text:
        return False
    plain = _TIMING.sub("", text)
    kana = len(_KANA.findall(plain))
    if kana < 8:
        return False
    return kana * 4 >= kana + len(_HAN.findall(plain))


def to_traditional(text: str) -> str:
    """简体 -> 繁體（台灣用字）, unconditional: this is presentation text."""
    if not text:
        return text
    parts = _BREAK.split(text)
    return "".join(p if i % 2 else _s2tw_tagged(p) for i, p in enumerate(parts))


def to_simplified(text: str) -> str:
    """繁體 -> 简体 (queries sent to aggregate hosts), OpenCC `tw2s`."""
    if not text:
        return text
    tw, tw_len, ts, ts_len = _t2s_tables()
    return _max_match(_max_match(text, tw, tw_len), ts, ts_len)


def fold(text: str) -> str:
    """Match key: traditional, lower-cased, no punctuation/whitespace.

    '晴天 (Live)' and '晴天 live' fold alike; plain '晴天' stays distinct from
    the Live cut — the live/studio rule falls out of exact folding.
    """
    if not text:
        return ""
    return _PUNCT.sub("", to_traditional(text).lower())
