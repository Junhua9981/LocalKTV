/**
 * Lyrics arrive from the server already parsed and normalised:
 * `GET /lyrics/{id}` returns `lines` (see server ktv/lyrics_parse.py) in the
 * shape below, so the display is a pure renderer — no client-side format
 * parsing (the AMLL dependency was removed).
 */

export interface LyricWord {
  text: string;
  start: number; // ms
  end: number; // ms
}

/** 日文假名注音：該行文字第 `at` 個字起 `len` 個字（code point）上方印 `text`。 */
export interface LyricRuby {
  at: number;
  len: number;
  text: string;
}

export interface LyricLine {
  start: number; // ms
  end: number; // ms
  words: LyricWord[];
  translated?: string;
  /** Duet label this line falls under ("浩", "aMEI", "合") — absent for solo songs. */
  singer?: string;
  /** 0 = 合唱, 1.. = solo singers in order of first appearance (colour: --part-N). */
  part?: number;
  /** 日文歌的假名注音（伺服器 ktv/furigana.py），中文歌沒有。 */
  ruby?: LyricRuby[];
  /** 日文歌的中文空耳，依原文字分段、蓋住整行（同 ruby 的位置算法）。 */
  sora?: LyricRuby[];
}

/**
 * How the stage should paint the song:
 *   "word" — the provider shipped 逐字 timings → left-to-right blue sweep;
 *   "line" — only line timestamps exist     → whole line lights up at once.
 * Missing from older payloads, so it defaults to "word" (the original look).
 */
export type LyricTiming = "word" | "line";

export interface LyricDoc {
  lines: LyricLine[];
  offsetMs: number;
  format: string;
  timing: LyricTiming;
  /** 日文歌 → 電視改用日文字型 */
  japanese: boolean;
}

const KANA = /[\u3041-\u309f\u30a1-\u30fa\u30fd-\u30ff]/gu;
const HAN = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/gu;

/** 同伺服器 textnorm.is_japanese：假名至少 8 個、佔假名＋漢字四分之一以上。 */
export function isJapanese(lines: LyricLine[]): boolean {
  const text = lines.map((line) => line.words.map((w) => w.text).join("")).join("\n");
  const kana = text.match(KANA)?.length ?? 0;
  if (kana < 8) return false;
  return kana * 4 >= kana + (text.match(HAN)?.length ?? 0);
}

export async function fetchLyrics(url: string): Promise<LyricDoc | null> {
  try {
    const res = await fetch(url);
    if (!res.ok) return null;
    const body = await res.json();
    const lines: LyricLine[] = Array.isArray(body?.lines) ? body.lines : [];
    if (!lines.length) return null;
    return {
      lines,
      offsetMs: Number(body.offset_ms ?? 0),
      format: String(body.format ?? ""),
      timing: body.timing === "line" ? "line" : "word",
      japanese: isJapanese(lines),
    };
  } catch {
    return null;
  }
}
