"""日文歌詞的假名注音（ruby）— 電視在漢字上方印小字讀音。

`annotate(lines)` 只處理日文歌（textnorm.is_japanese），中文歌原樣返回。
每一行多一個 `ruby` 清單，位置以該行文字（words 接起來）的字元（code point）計：

    {"at": 3, "len": 2, "text": "さいご"}   # 第 3、4 個字「最後」上方印「さいご」

讀音的來源，先後：

1. 歌詞自己寫的「最後（さいご）」「思い出(おもいで)」：括號裡全是假名、緊接在
   漢字後面 → 拿來當注音，括號從歌詞刪掉（括號的時間併進前一個字）。像
   「(ああ)」這種前面不是漢字的和聲照舊保留。
2. 其餘漢字用 SudachiPy（字典 sudachidict_core）斷詞取讀音。送假名不標：
   「通り」只在「通」上印「とお」。沒裝 SudachiPy 就只有第 1 種。

3. 中文空耳（`sora`）：整行的讀音（漢字用上面的注音、假名照原文，助詞
   は／へ／を 依發音改成 わ／え／お）照對照表換成中文字，例如
   「愛してる」→「阿伊西貼嚕」。和 ruby 一樣以原文字位置分段，蓋住整行：

       {"at": 3, "len": 2, "text": "賽勾"}   # 「最後」唱成「賽勾」

   沒有對應中文的字（英文、標點、空白）原樣放著，讓電視照進度掃過去。

斷詞器第一次用時才載入（約 1 秒、常駐約 100MB，字典是 mmap），呼叫端請在
worker thread 裡跑（app.py 的 /lyrics 用 asyncio.to_thread）。
"""

from __future__ import annotations

import logging
import re
import threading
from functools import lru_cache
from typing import Any

from .textnorm import is_japanese

log = logging.getLogger(__name__)

LyricLine = dict[str, Any]

_KANJI = "々〆ヶ㐀-䶿一-鿿豈-﫿"  # 々 〆 ヶ + 漢字
_KANA = "ぁ-ゖゝゞァ-ヺー-ヾ"  # 平假名、片假名、ー
_IS_KANJI = re.compile(f"[{_KANJI}]")
_KANJI_RUN = re.compile(f"[{_KANJI}]+")
# 漢字後面緊接的「（よみ）」；全形、半形括號都收
_INLINE = re.compile(f"(?<=[{_KANJI}])[（(]([{_KANA}]+)[）)]")
# 自帶讀音往前最多看幾個字（「思い出（おもいで）」要往前跨過送假名）
_INLINE_LOOKBACK = 8
_STOP = re.compile(f"[^{_KANJI}{_KANA}]")


def _hira(text: str) -> str:
    """片假名 → 平假名（ー 保留）。"""
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text)


def _align(base: str, reading: str) -> list[tuple[int, int, str]] | None:
    """把整段讀音分給 base 裡的每一串漢字：(在 base 的位置, 長度, 讀音)。

    base 裡的假名（送假名）必須在讀音裡照樣出現，對不上就 None。
    「思い出」+「おもいで」→ [(0, 1, "おも"), (2, 1, "で")]。"""
    reading = _hira(reading)
    runs = list(_KANJI_RUN.finditer(base))
    if not runs or not reading:
        return None
    pattern, pos = "", 0
    for run in runs:
        pattern += re.escape(_hira(base[pos : run.start()])) + "(.+?)"
        pos = run.end()
    pattern += re.escape(_hira(base[pos:]))
    m = re.fullmatch(pattern, reading)
    if not m:
        return None
    out = []
    for run, text in zip(runs, m.groups()):
        if not re.fullmatch(f"[{_KANA}]+", text):
            return None
        out.append((run.start(), run.end() - run.start(), text))
    return out


# ---------------------------------------------------------------------------
# 1. 歌詞自帶的「漢字（よみ）」
# ---------------------------------------------------------------------------

def _inline_ruby(text: str) -> tuple[list[tuple[int, int, str]], list[tuple[int, int]]]:
    """找出自帶讀音：(注音, 要刪掉的括號範圍)；位置都是原文字的字元位置。"""
    ruby: list[tuple[int, int, str]] = []
    cut: list[tuple[int, int]] = []
    for m in _INLINE.finditer(text):
        reading = m.group(1)
        lo = max(0, m.start() - _INLINE_LOOKBACK)
        stop = [s.end() for s in _STOP.finditer(text, lo, m.start())]
        if cut:
            stop.append(cut[-1][1])  # 不跨過前一個括號
        lo = max([lo, *stop])
        best: list[tuple[int, int, str]] | None = None
        # 由長到短試：要從漢字開頭、送假名對得上的最長那段
        for start in range(lo, m.start()):
            if not _IS_KANJI.match(text[start]):
                continue
            got = _align(text[start : m.start()], reading)
            if got:
                best = [(start + at, n, r) for at, n, r in got]
                break
        if best is None:  # 對不上：整串讀音放在括號前那串漢字上
            run_start = m.start()
            while run_start > lo and _IS_KANJI.match(text[run_start - 1]):
                run_start -= 1
            best = [(run_start, m.start() - run_start, _hira(reading))]
        ruby.extend(best)
        cut.append((m.start(), m.end()))
    return ruby, cut


def _remove(line: LyricLine, cut: list[tuple[int, int]]) -> None:
    """從 words 刪掉 cut 範圍的字；刪空的字，時間併進前一個字。"""
    if not cut:
        return
    words = line.get("words") or []
    kept: list[dict[str, Any]] = []
    pos = 0
    for word in words:
        chars = list(word.get("text", ""))
        keep = [c for i, c in enumerate(chars) if not any(a <= pos + i < b for a, b in cut)]
        pos += len(chars)
        if keep:
            kept.append({**word, "text": "".join(keep)})
        elif kept:
            kept[-1]["end"] = max(kept[-1]["end"], word.get("end", kept[-1]["end"]))
    line["words"] = kept


def _shift(ruby: list[tuple[int, int, str]], cut: list[tuple[int, int]]) -> list[tuple[int, int, str]]:
    """原文字位置 → 刪掉括號以後的位置。"""
    def moved(at: int) -> int:
        return at - sum(b - a for a, b in cut if b <= at)

    return [(moved(at), n, r) for at, n, r in ruby]


# ---------------------------------------------------------------------------
# 2. SudachiPy 斷詞
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_tokenizer: Any = None
_tokenizer_failed = False


def _get_tokenizer() -> Any:
    global _tokenizer, _tokenizer_failed
    if _tokenizer is None and not _tokenizer_failed:
        try:
            from sudachipy import Dictionary

            _tokenizer = Dictionary(dict="core").create()
        except Exception as exc:  # 沒裝或字典壞了：只用歌詞自帶的讀音
            _tokenizer_failed = True
            log.warning("furigana: SudachiPy unavailable (%s) — only inline readings", exc)
    return _tokenizer


def available() -> bool:
    with _lock:
        return _get_tokenizer() is not None


# 歌詞的唸法和字典的第一讀音不同的字
_READING_FIX = {"私": "わたし"}
# 助詞照發音唸：は→わ、へ→え、を→お
_PARTICLE_SOUND = {"は": "わ", "へ": "え", "を": "お"}


@lru_cache(maxsize=4096)
def _analyse(text: str) -> tuple[tuple[tuple[int, int, str], ...], tuple[tuple[int, str], ...]]:
    """(漢字讀音, 要改唸法的助詞 (位置, 唸法))；沒有斷詞器就都是空的。"""
    if not re.search(f"[{_KANJI}{_KANA}]", text):
        return (), ()
    with _lock:
        tok = _get_tokenizer()
        if tok is None:
            return (), ()
        from sudachipy import SplitMode

        morphemes = [
            (m.begin(), m.surface(), m.reading_form(), m.part_of_speech()[0])
            for m in tok.tokenize(text, SplitMode.C)
        ]
    out: list[tuple[int, int, str]] = []
    particles: list[tuple[int, str]] = []
    for begin, surface, reading, pos in morphemes:
        if pos == "助詞" and surface in _PARTICLE_SOUND:
            particles.append((begin, _PARTICLE_SOUND[surface]))
        if not _IS_KANJI.search(surface):
            continue
        reading = _READING_FIX.get(surface, reading)
        got = _align(surface, reading)
        if got is None and _KANJI_RUN.fullmatch(surface) and re.fullmatch(f"[{_KANA}]+", reading):
            got = [(0, len(surface), _hira(reading))]
        for at, n, r in got or ():
            out.append((begin + at, n, r))
    return tuple(out), tuple(particles)


# ---------------------------------------------------------------------------

def _text(line: LyricLine) -> str:
    return "".join(word.get("text", "") for word in line.get("words") or [])


def annotate(lines: list[LyricLine]) -> list[LyricLine]:
    """日文歌：刪自帶讀音的括號、每行加 `ruby`。中文歌原樣返回。不會丟例外。"""
    try:
        if not is_japanese("\n".join(_text(line) for line in lines)):
            return lines
        for line in lines:
            _annotate_line(line)
    except Exception:
        log.warning("furigana failed", exc_info=True)
    return lines


def _annotate_line(line: LyricLine) -> None:
    inline, cut = _inline_ruby(_text(line))
    _remove(line, cut)
    ruby = _shift(inline, cut)
    taken = [(at, at + n) for at, n, _ in ruby]
    auto, particles = _analyse(_text(line))
    for at, n, r in auto:
        if not any(a < at + n and at < b for a, b in taken):
            ruby.append((at, n, r))
    ruby.sort()
    if ruby:
        line["ruby"] = [{"at": at, "len": n, "text": r} for at, n, r in ruby]
    sora = _sora_segments(_text(line), ruby, dict(particles))
    if any(seg["text"].strip() for seg in sora):
        line["sora"] = sora


# ---------------------------------------------------------------------------
# 3. 中文空耳
# ---------------------------------------------------------------------------

# 一個音節 → 一個中文字（台灣常見的空耳寫法；國語唸得出來、字也常見）
_SORA = {
    "あ": "阿", "い": "伊", "う": "嗚", "え": "欸", "お": "喔",
    "か": "卡", "き": "奇", "く": "哭", "け": "克", "こ": "摳",
    "さ": "撒", "し": "西", "す": "斯", "せ": "塞", "そ": "搜",
    "た": "他", "ち": "七", "つ": "此", "て": "貼", "と": "偷",
    "な": "那", "に": "你", "ぬ": "奴", "ね": "內", "の": "諾",
    "は": "哈", "ひ": "嘻", "ふ": "呼", "へ": "黑", "ほ": "吼",
    "ま": "媽", "み": "咪", "む": "木", "め": "妹", "も": "摸",
    "や": "呀", "ゆ": "尤", "よ": "喲",
    "ら": "啦", "り": "哩", "る": "嚕", "れ": "勒", "ろ": "囉",
    "わ": "哇", "ゐ": "伊", "ゑ": "欸", "を": "喔", "ん": "嗯",
    "が": "嘎", "ぎ": "吉", "ぐ": "咕", "げ": "給", "ご": "勾",
    "ざ": "紮", "じ": "基", "ず": "茲", "ぜ": "賊", "ぞ": "走",
    "だ": "搭", "ぢ": "基", "づ": "茲", "で": "爹", "ど": "兜",
    "ば": "巴", "び": "比", "ぶ": "布", "べ": "貝", "ぼ": "波",
    "ぱ": "趴", "ぴ": "匹", "ぷ": "噗", "ぺ": "呸", "ぽ": "剖",
    "ゔ": "夫",
    # 拗音
    "きゃ": "卡", "きゅ": "丘", "きょ": "巧", "ぎゃ": "嘉", "ぎゅ": "糾", "ぎょ": "久",
    "しゃ": "蝦", "しゅ": "咻", "しょ": "修", "じゃ": "加", "じゅ": "就", "じょ": "九",
    "ちゃ": "恰", "ちゅ": "啾", "ちょ": "秋", "ぢゃ": "加", "ぢゅ": "就", "ぢょ": "九",
    "にゃ": "娘", "にゅ": "紐", "にょ": "妞", "ひゃ": "夏", "ひゅ": "休", "ひょ": "休",
    "びゃ": "標", "びゅ": "彪", "びょ": "標", "ぴゃ": "飄", "ぴゅ": "彪", "ぴょ": "飄",
    "みゃ": "喵", "みゅ": "謬", "みょ": "苗", "りゃ": "倆", "りゅ": "溜", "りょ": "料",
    # 外來語
    "ふぁ": "發", "ふぃ": "菲", "ふぇ": "非", "ふぉ": "佛", "てぃ": "踢", "でぃ": "低",
    "とぅ": "禿", "どぅ": "杜", "うぃ": "威", "うぇ": "威", "うぉ": "窩",
    "ちぇ": "切", "しぇ": "些", "じぇ": "傑", "ゔぁ": "瓦", "ゔぃ": "維",
    # 音節＋ん 合成一個字
    "あん": "安", "いん": "因", "うん": "溫", "えん": "恩", "おん": "翁",
    "かん": "看", "きん": "金", "くん": "昆", "けん": "肯", "こん": "空",
    "さん": "三", "しん": "新", "すん": "孫", "せん": "森", "そん": "鬆",
    "たん": "炭", "ちん": "親", "つん": "村", "てん": "疼", "とん": "通",
    "なん": "南", "にん": "您", "ぬん": "嫩", "ねん": "嫩", "のん": "農",
    "はん": "漢", "ひん": "欣", "ふん": "分", "へん": "哼", "ほん": "轟",
    "まん": "滿", "みん": "民", "むん": "悶", "めん": "門", "もん": "蒙",
    "やん": "央", "ゆん": "允", "よん": "庸",
    "らん": "藍", "りん": "林", "るん": "倫", "れん": "連", "ろん": "龍",
    "わん": "萬", "がん": "乾", "ぎん": "金", "ぐん": "滾", "げん": "根", "ごん": "貢",
    "ざん": "贊", "じん": "今", "ずん": "尊", "ぜん": "怎", "ぞん": "總",
    "だん": "單", "でん": "燈", "どん": "東",
    "ばん": "班", "びん": "賓", "ぶん": "奔", "べん": "本", "ぼん": "蹦",
    "ぱん": "盤", "ぴん": "拼", "ぷん": "噴", "ぺん": "盆", "ぽん": "碰",
    "しゃん": "香", "しょん": "兄", "じゃん": "將", "ちゃん": "槍", "きょん": "窮",
}
_SORA_LONGEST = max(map(len, _SORA))
_SMALL = {"ぁ": "あ", "ぃ": "い", "ぅ": "う", "ぇ": "え", "ぉ": "お", "ゃ": "や", "ゅ": "ゆ", "ょ": "よ", "ゎ": "わ"}
_O_ROW = "おこそとのほもよろをごぞどぼぽょ"
_E_ROW = "えけせてねへめれげぜでべぺ"
_SILENT = set("っッーゝゞヽヾ")


def _sora_units(text: str, ruby: list[tuple[int, int, str]], particles: dict[int, str]) -> list[tuple[int, int, str, bool]]:
    """整行拆成 (位置, 長度, 讀音或原字, 是不是假名讀音)。"""
    units: list[tuple[int, int, str, bool]] = []
    by_at = {at: (n, r) for at, n, r in ruby}
    i = 0
    while i < len(text):
        if i in by_at:
            n, reading = by_at[i]
            # 漢字的音讀：お段後的「う」、え段後的「い」是長音
            # （東京 とうきょう → 偷巧、先生 せんせい → 森塞）
            reading = re.sub(f"(?<=[{_O_ROW}])う|(?<=[{_E_ROW}])い", "ー", _hira(reading))
            units.append((i, n, reading, True))
            i += n
            continue
        ch = text[i]
        if re.fullmatch(f"[{_KANA}ぁぃぅぇぉゃゅょゎっッ]", ch):
            units.append((i, 1, particles.get(i, _hira(ch)), True))
        else:
            units.append((i, 1, ch, False))
        i += 1
    return units


def _sora_segments(text: str, ruby: list[tuple[int, int, str]], particles: dict[int, str]) -> list[dict[str, Any]]:
    """整行的空耳，按原文字分段（每段 = 原文一個字或一串有注音的漢字）。"""
    units = _sora_units(text, ruby, particles)
    # 讀音接成一長串（拗音、ん 可能跨字），每個假名記得是哪一段的
    kana: list[tuple[str, int]] = []
    out = [[at, n, ""] for at, n, _, _ in units]
    for k, (_, _, value, is_kana) in enumerate(units):
        if is_kana:
            kana.extend((c, k) for c in value)
        else:
            out[k][2] = value  # 英文、標點、空白原樣
    j = 0
    while j < len(kana):
        ch, k = kana[j]
        if ch in _SILENT:
            j += 1
            continue
        for size in range(min(_SORA_LONGEST, len(kana) - j), 0, -1):
            key = "".join(c for c, _ in kana[j : j + size])
            if size == 1:
                key = _SMALL.get(key, key)  # 單獨的小字（ぁ、ゃ）照大字唸
            hit = _SORA.get(key)
            if hit:
                out[k][2] += hit
                j += size
                break
        else:
            j += 1  # 對照表沒有的假名：略過
    return [{"at": at, "len": n, "text": t} for at, n, t in out]
