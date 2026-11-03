"""Server-side lyric parsing — QRC / LRC / YRC / TXT → normalised JSON lines.

The display never parses text itself: `GET /lyrics/{track_id}` serves the
already-normalised structure below, which keeps every format decision in one
testable place and lets the renderer stay a pure view.

    {
      "timing": "word",             # "word" (逐字) | "line" (逐行) — how to render
      "lines": [
        {
          "start": 1234,            # ms
          "end":   5678,            # ms
          "words": [
            {"text": "晴", "start": 1234, "end": 1490},
            ...
          ],
          "translated": "…",         # optional
          "singer": "aMEI",          # optional — duet marker this line falls under
          "part": 2,                 # optional — 0 = 合唱, 1.. = solo singers in
                                     #   order of first appearance
          "ruby": [{"at": 0, "len": 2, "text": "さいご"}]
                                     # optional — 日文假名注音, added by
        }                            #   ktv/furigana.py in GET /lyrics
      ]
    }

Clean-up after the format parse (`_annotate`):

* credit lines ("作詞：…", "詞 Lyricist：…", "編曲 Arrangement：…",
  "未經著作權人許可…") and a leading "歌名 - 歌手" line are dropped — they
  carry timestamps but nobody sings them;
* duet markers — a whole line "浩：" / "aMEI：" / "合：", or the same label in
  front of a sung line — are removed from the text and become `singer` /
  `part` on the lines they introduce, so the display can colour each voice
  (KTV convention: 合唱 green).

Formats:

* QRC  — QQ Music word-by-word XML (`<Lyric_1 … LyricContent="…"/>` wrapper,
         lines `[start,dur]text(start,dur)…`).
* LRC  — line timestamps `[mm:ss.xx]`, optional enhanced word tags
         `<mm:ss.xx>word`, multiple timestamps per line (duet/repeat).
* YRC  — NetEase word-by-word (`[start,dur](start,dur,?)word…`).
* TXT  — unsynced text, spread evenly across the track duration.
"""

from __future__ import annotations

import html
import re
from typing import Any

LyricWord = dict[str, Any]
LyricLine = dict[str, Any]

_QRC_WRAPPER = re.compile(r'<Lyric_1\s+LyricType="1"\s+LyricContent="(.*?)"\s*/>', re.DOTALL)
_MS_LINE = re.compile(r"^\[(\d+),(\d+)\](.*)$")  # QRC / YRC line header
_LRC_LINE = re.compile(r"^\[(\d+):(\d+)(?:[.:](\d+))?\](.*)$")
_LRC_TAG = re.compile(r"^\[(\w+):([^]\[]*)\]$")
_ENHANCED = re.compile(r"<(\d+):(\d+)(?:[.:](\d+))?>")
_FALLBACK_LINE_MS = 4000


def _to_ms(minute: str, second: str, frac: str | None) -> int:
    ms = int(minute) * 60_000 + int(second) * 1000
    if frac:
        ms += int(frac.ljust(3, "0")[:3])
    return ms


def _finish(lines: list[LyricLine]) -> list[LyricLine]:
    """Sort, repair end times and drop lines nobody can sing along to."""
    lines.sort(key=lambda line: line["start"])
    for i, line in enumerate(lines):
        if line["end"] <= line["start"]:
            nxt = lines[i + 1]["start"] if i + 1 < len(lines) else None
            line["end"] = nxt if nxt and nxt > line["start"] else line["start"] + 3000
        line["words"] = [w for w in line["words"] if w["text"] != ""]
        for word in line["words"]:
            if word["end"] <= word["start"]:
                word["end"] = max(word["start"] + 100, line["start"])
    return [line for line in lines if line["words"] or line["end"] > line["start"]]


# ---------------------------------------------------------------------------
# QRC
# ---------------------------------------------------------------------------

def _parse_qrc(content: str) -> list[LyricLine]:
    wrapped = _QRC_WRAPPER.search(content)
    body = html.unescape(wrapped.group(1)) if wrapped else content

    lines: list[LyricLine] = []
    for raw in body.splitlines():
        match = _MS_LINE.match(raw.strip())
        if not match:
            continue
        start, dur, text = int(match.group(1)), int(match.group(2)), match.group(3)
        if not text.strip() or re.fullmatch(r"\(\d+,\d+\)", text.strip()):
            continue  # blank or instrumental-marker line
        # "爱(100,200)你(300,150)" -> ["爱", "100", "200", "你", "300", "150", ""]
        parts = re.split(r"\((\d+),(\d+)\)", text)
        words: list[LyricWord] = []
        for i in range((len(parts) - 1) // 3):
            w_text = parts[i * 3]
            w_start = int(parts[i * 3 + 1])
            w_end = w_start + int(parts[i * 3 + 2])
            if w_text:
                words.append({"text": w_text, "start": w_start, "end": w_end})
        if not words:  # timestamp-only line (instrumental marker)
            words = [{"text": text.strip(), "start": start, "end": start + dur}]
        lines.append({"start": start, "end": start + dur, "words": words})
    return _finish(lines)


# ---------------------------------------------------------------------------
# LRC (plain + enhanced)
# ---------------------------------------------------------------------------

def _parse_lrc(content: str) -> list[LyricLine]:
    # (start, text) — a line with several timestamps yields several entries
    entries: list[tuple[int, str]] = []
    for raw in content.splitlines():
        line = raw.strip().lstrip("﻿")
        if not line or not line.startswith("["):
            continue
        # consume every leading timestamp: "[00:10.00][01:20.00] chorus"
        starts: list[int] = []
        rest = line
        while True:
            match = _LRC_LINE.match(rest)
            if not match or not match.group(4):
                break
            starts.append(_to_ms(match.group(1), match.group(2), match.group(3)))
            rest = match.group(4).lstrip()
        text = rest.strip()
        if starts and text:
            entries.extend((start, text) for start in starts)
        # [ar:…] / [ti:…] / [offset:…] metadata has no numeric timestamp
    entries.sort(key=lambda item: item[0])

    lines: list[LyricLine] = []
    for i, (start, text) in enumerate(entries):
        end_hint = entries[i + 1][0] if i + 1 < len(entries) else None
        lines.append(_lrc_line(start, text, end_hint))
    return _finish(lines)


def _lrc_line(start: int, text: str, end_hint: int | None) -> LyricLine:
    if "<" not in text or not _ENHANCED.search(text):
        end = end_hint or (start + 5000)
        return {"start": start, "end": end, "words": [{"text": text, "start": start, "end": end}]}

    # Enhanced LRC: `<mm:ss.xx>word` — the tag times the text that follows it.
    words: list[LyricWord] = []
    cursor = 0
    prev_start: int | None = None
    for match in _ENHANCED.finditer(text):
        between = text[cursor : match.start()]
        tag_start = _to_ms(match.group(1), match.group(2), match.group(3))
        if between:
            words.append({"text": between, "start": prev_start if prev_start is not None else start, "end": tag_start})
        prev_start = tag_start
        cursor = match.end()
    tail = text[cursor:]
    if tail:
        tail_start = prev_start if prev_start is not None else start
        words.append({"text": tail, "start": tail_start, "end": end_hint or tail_start + 2000})
    if not words:
        return {"start": start, "end": end_hint or (start + 5000), "words": [{"text": text, "start": start, "end": end_hint or start + 5000}]}
    end = end_hint or words[-1]["end"]
    return {"start": start, "end": end, "words": words}


# ---------------------------------------------------------------------------
# YRC
# ---------------------------------------------------------------------------

def _parse_yrc(content: str) -> list[LyricLine]:
    lines: list[LyricLine] = []
    for raw in content.splitlines():
        match = _MS_LINE.match(raw.strip())
        if not match:
            continue
        start, dur, text = int(match.group(1)), int(match.group(2)), match.group(3)
        if not text.strip() or re.fullmatch(r"\(\d+,\d+(?:,-?\d+)?\)", text.strip()):
            continue  # blank or instrumental-marker line
        # "(100,200,0)爱(300,150,0)你" -> ["", "100","200","0", "爱", "300","150","0", "你"]
        parts = re.split(r"\((\d+),(\d+),(-?\d+)\)", text)
        words: list[LyricWord] = []
        for i in range((len(parts) - 1) // 4):
            w_start = int(parts[i * 4 + 1])
            w_end = w_start + int(parts[i * 4 + 2])
            w_text = parts[i * 4 + 4] if i * 4 + 4 < len(parts) else ""
            if w_text:
                words.append({"text": w_text, "start": w_start, "end": w_end})
        if not words:
            words = [{"text": text.strip(), "start": start, "end": start + dur}]
        lines.append({"start": start, "end": start + dur, "words": words})
    return _finish(lines)


# ---------------------------------------------------------------------------
# TXT
# ---------------------------------------------------------------------------

def _parse_txt(content: str, duration_ms: int) -> list[LyricLine]:
    texts = [t for t in (raw.strip() for raw in content.splitlines()) if t]
    if not texts:
        return []
    span = (duration_ms if duration_ms > 0 else len(texts) * _FALLBACK_LINE_MS) // len(texts)
    span = max(span, 500)
    return [
        {"start": i * span, "end": (i + 1) * span, "words": [{"text": t, "start": i * span, "end": (i + 1) * span}]}
        for i, t in enumerate(texts)
    ]


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def _parse_raw(fmt: str, content: str, duration_ms: int) -> list[LyricLine]:
    kind = (fmt or "").lower().lstrip(".")
    if kind == "qrc":
        return _parse_qrc(content)
    if kind == "yrc":
        return _parse_yrc(content)
    if kind == "txt":
        return _parse_txt(content, duration_ms)
    if kind == "lrc":
        return _parse_lrc(content)
    return []  # unknown format (e.g. legacy ttml) renders nothing


def parse(
    fmt: str, content: str, duration_ms: int = 0, title: str = "", artist: str = ""
) -> list[LyricLine]:
    """Parse `content` as `fmt` into normalised lines (never raises).

    `title` / `artist` (optional) help recognise a "歌名 - 歌手" header line
    and duet labels that abbreviate a singer's name."""
    try:
        lines = _parse_raw(fmt, content, duration_ms)
    except Exception:
        return []
    try:
        return _annotate(lines, title, artist)
    except Exception:
        return lines  # clean-up is best effort — never lose the lyrics


# ---------------------------------------------------------------------------
# credits / duet markers
# ---------------------------------------------------------------------------

# A credit label is made only of these pieces: "作詞", "詞 Lyricist",
# "編曲 Arrangement", "Mixing Engineer", "OP" …  Matching the WHOLE label
# keeps real lyrics such as "我說：愛你" untouched.
_CREDIT_CJK = (
    "作词作曲|作詞作曲|词曲|詞曲|作词|作詞|作曲|编曲|編曲|词|詞|曲|原唱|演唱|翻唱|原曲|"
    "制作人|製作人|制作|製作|监制|監製|出品人|出品|发行|發行|统筹|統籌|企划|企劃|策划|策劃|"
    "混音师|混音師|混音|母带|母帶|录音师|錄音師|录音室|錄音室|录音|錄音|缩混|縮混|后期|後期|"
    "和声编写|和聲編寫|和声|和聲|合声|合聲|和音|配唱|伴唱|监唱|監唱|人声|人聲|编写|編寫|"
    "编配|編配|编程|編程|配器|弦乐|弦樂|絃樂|弦|絃|演奏|乐手|樂手|小提琴|大提琴|口琴|手风琴|手風琴|"
    "乐团|樂團|乐队|樂隊|指挥|指揮|工作室|"
    "工程|音效|"
    "吉他|贝斯|貝斯|鼓|键盘|鍵盤|钢琴|鋼琴|合成器|打击乐|打擊樂|管乐|管樂|"
    "音乐总监|音樂總監|音乐制作|音樂製作|音频编辑|音頻編輯|工程师|工程師|助理|"
    "版权|版權|文案|设计|設計|封面|宣传|宣傳|营销|營銷|艺人|藝人|经纪|經紀|"
    "艺术指导|藝術指導|指导|指導|团队|團隊|推广|推廣|支持|联合|聯合|协力|協力|处理|處理|"
    "歌曲|视觉|視覺|摄影|攝影|出版|总监|總監|監制|监製|总监制|總監制|鸣谢|鳴謝|特别|特別"
)
_CREDIT_EN = (
    "lyricist|lyrics|lyric|words|composer|composed|composition|compose|music|arranger|"
    "assistance|assistant|artistic|art|team|promotion|marketing|planning|"
    "arrangement|arranged|producer|produced|production|executive|mixing|mixed|mix|"
    "mastering|mastered|master|recording|recorded|engineer|engineers|studio|"
    "guitar|guitars|bass|drums|drum|piano|keyboard|keyboards|keys|strings|synth|"
    "programming|vocal|vocals|backing|harmony|director|supervisor|publisher|"
    "label|copyright|isrc|op|sp|by|and|written|writer|writers|assistant|editor"
)
_CREDIT_LABEL = re.compile(rf"^(?:{_CREDIT_CJK}|{_CREDIT_EN})+$", re.IGNORECASE)
_CREDIT_CJK_LABEL = re.compile(rf"^(?:{_CREDIT_CJK})+$")
_CREDIT_EN_LABEL = re.compile(rf"^(?:{_CREDIT_EN})+$", re.IGNORECASE)
_LABEL_SPLIT = re.compile(r"^\s*([^:：]{1,60}?)\s*[:：]\s*(.*)$", re.DOTALL)
_PAREN = re.compile(r"[（(【\[][^）)】\]]*[）)】\]]+")
_CREDIT_PIECE = re.compile(rf"{_CREDIT_CJK}", re.IGNORECASE)
_COPYRIGHT_NOTE = re.compile(r"未经.{0,12}许可|未經.{0,12}許可|不得翻[唱录錄]|版权所有|版權所有")
# duet labels that mean "everyone sings this"
_TOGETHER = re.compile(r"^(?:合|合唱|齐|齊|齐唱|齊唱|all|together|both|chorus)$", re.IGNORECASE)
_MULTI = re.compile(r"[&＆/／、,，+＋]| and ", re.IGNORECASE)
_HEADER_DASH = re.compile(r"\s[-–—]\s")


def _text(line: LyricLine) -> str:
    return "".join(word["text"] for word in line.get("words") or [])


def _fold(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text or "").lower()


def _is_credit(text: str, leading: bool = False) -> bool:
    """Credit / copyright line.  Anywhere in the song the label must be made
    of credit words only; in the leading block (before the first sung line)
    one Chinese credit word inside the label is enough — "藝術指導", "營銷團隊",
    "錄音師（李榮浩）Recording Engineer（Ronghao Li）" all sit up there."""
    if _COPYRIGHT_NOTE.search(text):
        return True
    match = _LABEL_SPLIT.match(text)
    if not match or not match.group(2).strip():
        return False
    label = re.sub(r"[\s/&＆、·.\-]+", "", _PAREN.sub("", match.group(1)))
    if not label:
        return False
    if _CREDIT_LABEL.match(label):
        return True
    if leading and len(label) <= 40 and _CREDIT_PIECE.search(label) and not _TOGETHER.match(label):
        return True
    # "曲 Compose" / "詞 Lyrics by": a credit word in Chinese, glossed in
    # English — the Chinese half alone decides, whatever the English says
    cjk = re.sub(r"[A-Za-z]+", "", label)
    if not cjk or cjk == label:
        return False
    if _CREDIT_CJK_LABEL.match(cjk):
        return True
    # bilingual labels ("錄音工作室Recording Studio", "絃樂Strings") are a
    # credit-sheet habit lyrics never have: one credit word in either half
    # is enough ("周杰倫Jay：" has none, so a duet label stays a label)
    latin = "".join(re.findall(r"[A-Za-z]+", label))
    return len(label) <= 40 and (
        bool(_CREDIT_PIECE.search(cjk)) or bool(_CREDIT_EN_LABEL.match(latin))
    )


def _is_labelled(text: str) -> bool:
    """"label：value" shape — what every credit line looks like."""
    match = _LABEL_SPLIT.match(text)
    return bool(match and match.group(2).strip())


def _tfold(text: str) -> str:
    """`_fold` across 简/繁 — "屋顶" and "屋頂" are the same title."""
    from .textnorm import fold

    return fold(text or "")


def _base(text: str) -> str:
    """Folded title without its bracketed notes: "擱淺 (Live)" -> "擱淺"."""
    return _tfold(_PAREN.sub("", text or ""))


def _is_header(line: LyricLine, text: str, title: str, artist: str) -> bool:
    """"晴天 - 周杰倫" style title line in front of the lyrics — or just the
    title on its own ("雨愛（旋律DJ 版）") right at the start of the song."""
    base_title = _base(title)
    if not _HEADER_DASH.search(text):
        return bool(base_title) and line["start"] < 3000 and _base(text) == base_title
    folded = _tfold(text)
    left = _base(_HEADER_DASH.split(text, 1)[0])
    keys = [k for k in (_tfold(title), _tfold(artist), base_title) if k]
    if keys:
        return any(k in folded for k in keys) or (bool(left) and left == base_title)
    return len(text) <= 40


def _marker(text: str, artist: str, known: set[str]) -> tuple[str, str] | None:
    """(label, rest) when the line starts with a duet label.

    A whole-line label ("浩：") always counts; a label in front of sung text
    ("浩：慢慢走遠") only when it is a known voice — 合/男/女, a label seen as a
    whole line before, or a piece of the artist field — so "我說：愛你"
    stays a lyric."""
    match = _LABEL_SPLIT.match(text)
    if not match:
        return None
    label = match.group(1).strip().strip("()（）【】[]「」")
    rest = match.group(2)
    if not label or len(label) > 16 or _is_credit(text if rest.strip() else text + "x"):
        return None
    if not rest.strip():
        return label, ""
    folded = _fold(label)
    if (
        _TOGETHER.match(label)
        or label in ("男", "女", "男声", "女声", "男聲", "女聲")
        or label in known
        or (folded and len(folded) <= 12 and folded in _fold(artist))
    ):
        return label, rest
    return None


def _strip_prefix(line: LyricLine, rest: str) -> None:
    """Drop the label's characters from the front of the line's words."""
    drop = len(_text(line)) - len(rest)
    kept: list[LyricWord] = []
    for word in line["words"]:
        if drop <= 0:
            kept.append(word)
            continue
        if len(word["text"]) <= drop:
            drop -= len(word["text"])
            continue
        word = {**word, "text": word["text"][drop:]}
        drop = 0
        kept.append(word)
    while kept and not kept[0]["text"].strip():
        kept.pop(0)
    if kept:
        kept[0] = {**kept[0], "text": kept[0]["text"].lstrip()}
        line["start"] = min(line["end"], max(line["start"], kept[0]["start"]))
    line["words"] = kept


def _annotate(lines: list[LyricLine], title: str, artist: str) -> list[LyricLine]:
    out: list[LyricLine] = []
    known: set[str] = set()
    parts: dict[str, int] = {}
    singer = ""
    texts = [_text(line).strip() for line in lines]
    texts = [t for t in texts if t]
    lines = [line for line in lines if _text(line).strip()]
    # The leading block lasts until the first line that is not "label：value"
    # — a credit the vocabulary misses must not switch the rest of the block
    # to the strict test.  A "label：value" line wedged between two credits
    # up there is one too ("絃樂：國際首席愛樂樂團").
    sung = False
    for i, line in enumerate(lines):
        text = texts[i]
        if _is_credit(text, leading=not sung):
            continue
        if (
            not sung
            and _is_labelled(text)
            and 0 < i < len(lines) - 1
            and _is_credit(texts[i - 1], leading=True)
            and _is_credit(texts[i + 1], leading=True)
        ):
            continue
        if not out and _is_header(line, text, title, artist):
            continue
        marked = _marker(text, artist, known)
        if marked is not None:
            label, rest = marked
            sung = True  # a duet label: the song itself has begun
            known.add(label)
            singer = label
            if not rest.strip():
                continue  # the marker line itself is never sung
            _strip_prefix(line, rest)
            if not line["words"]:
                continue
        elif text.endswith(("：", ":")) and len(text) <= 18:
            continue  # a bare "編曲：" style label — nothing to sing
        if singer:
            if _TOGETHER.match(singer) or _MULTI.search(singer):
                part = 0
            else:
                part = parts.setdefault(singer, len(parts) + 1)
            line["singer"] = singer
            line["part"] = part
        out.append(line)
        if not _is_labelled(text):
            sung = True
    # credits rolled at the end ("監制：…", "特別鳴謝：…") — same lenient test
    # as the leading block, walking back from the last line
    while out and _is_credit(_text(out[-1]).strip(), leading=True):
        out.pop()
    return out


def parse_translation(fmt: str, content: str) -> dict[int, str]:
    """Extract `start_ms -> text` from a translation/romanisation file."""
    try:
        lines = parse(fmt, content)
        return {line["start"]: "".join(w["text"] for w in line["words"]) for line in lines}
    except Exception:
        return {}


def attach_translations(lines: list[LyricLine], translations: dict[int, str]) -> list[LyricLine]:
    """Merge a parallel translation track onto parsed lines by start time."""
    if not translations:
        return lines
    for line in lines:
        text = translations.get(line["start"])
        if text is None:
            near = min(translations, key=lambda k: abs(k - line["start"]), default=None)
            if near is not None and abs(near - line["start"]) <= 250:
                text = translations[near]
        if text:
            line["translated"] = text
    return lines


def timing_of_lines(lines: list[LyricLine]) -> str:
    """`"word"` when any line carries word-level timings, else `"line"`.

    Decided from the data, not the file extension: an enhanced LRC
    (`<mm:ss.xx>word`) is word-timed even though it is stored as `.lrc`,
    while a `.yrc` whose every line collapsed into one token has no
    per-glyph timeline to sweep.  The display renders `"word"` with the
    left-to-right fill and `"line"` with the whole-line highlight.
    """
    for line in lines:
        if len(line.get("words") or []) > 1:
            return "word"
    return "line"


def timing_of(fmt: str, content: str) -> str:
    """`timing_of_lines(parse(fmt, content))`, or `""` when nothing parses.

    The provider chain reads `""` as "this answer is unusable": an unsynced
    body or a blob the parser does not understand never reaches the cache.
    """
    lines = parse(fmt, content)
    return timing_of_lines(lines) if lines else ""
