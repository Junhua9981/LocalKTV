/**
 * Long lines on the TV: a line too wide for its karaoke slot is cut into
 * shorter lines *before* the stage sees it, so the second half simply
 * becomes the next line (lower-right slot) instead of wrapping under the
 * first half — wrapping made the fill sweep both rows at once and pushed
 * the two slots into each other.
 *
 * Cuts land on spaces (English) or after punctuation / spaces (Chinese);
 * a Chinese run with neither is cut between any two characters.  The cut
 * is the most balanced one, and a half that is still too wide is cut again.
 * Word timings are kept — a word that straddles a cut (plain LRC ships the
 * whole line as one "word") is split with its time spread over its glyphs.
 */
import type { LyricLine, LyricRuby, LyricWord } from "./lyrics";

/** Text width in px at the stage's font size. */
export type Measure = (text: string) => number;

interface Glyph {
  ch: string;
  start: number;
  end: number;
  word: number; // index of the source word
  pos: number; // index in the source line's text (for its ruby)
  ruby: boolean; // inside a 假名注音 span (never cut there)
}

const SPACE = /\s/;
// a cut right after one of these reads naturally
const PAUSE = /[\s,，、。．.!！?？;；:：…~～)）」』】》〉]/;
// never start a line with one of these
const NO_START = /[,，、。．.!！?？;；:：…~～)）」』】》〉'’"”]/;
const CJK = /[⺀-鿿豈-﫿぀-ヿ가-힯]/;

function glyphsOf(line: LyricLine): Glyph[] {
  const out: Glyph[] = [];
  const inRuby = (pos: number) => (line.ruby ?? []).some((r) => r.at < pos && pos < r.at + r.len);
  line.words.forEach((word, wi) => {
    const chars = Array.from(word.text);
    const step = (word.end - word.start) / Math.max(1, chars.length);
    chars.forEach((ch, i) =>
      out.push({
        ch,
        start: word.start + i * step,
        end: word.start + (i + 1) * step,
        word: wi,
        pos: out.length,
        ruby: inRuby(out.length),
      }),
    );
  });
  return out;
}

const textOf = (glyphs: Glyph[]) => glyphs.map((g) => g.ch).join("");

/** Glyph indices a line may be cut before: at a pause, or anywhere else. */
function cutPoints(glyphs: Glyph[]): { soft: number[]; hard: number[] } {
  const text = textOf(glyphs);
  const hasSpace = /\S\s+\S/.test(text.trim());
  const soft: number[] = [];
  const hard: number[] = [];
  for (let i = 1; i < glyphs.length; i += 1) {
    const prev = glyphs[i - 1].ch;
    const next = glyphs[i].ch;
    if (SPACE.test(next) || NO_START.test(next) || glyphs[i].ruby) continue;
    if (PAUSE.test(prev)) soft.push(i);
    else if (!hasSpace && CJK.test(prev) && CJK.test(next)) hard.push(i);
    else if (CJK.test(prev) !== CJK.test(next) && !SPACE.test(prev)) hard.push(i); // 中英交界
  }
  return { soft, hard };
}

function trimmed(glyphs: Glyph[]): Glyph[] {
  let a = 0;
  let b = glyphs.length;
  while (a < b && SPACE.test(glyphs[a].ch)) a += 1;
  while (b > a && SPACE.test(glyphs[b - 1].ch)) b -= 1;
  return glyphs.slice(a, b);
}

/** Rebuild words from glyphs, merging back the glyphs of each source word. */
function toLine(src: LyricLine, glyphs: Glyph[], first: boolean, last: boolean): LyricLine {
  const words: LyricWord[] = [];
  let cur: { word: number; glyphs: Glyph[] } | null = null;
  const flush = () => {
    if (!cur) return;
    const g = cur.glyphs;
    words.push({ text: textOf(g), start: Math.round(g[0].start), end: Math.round(g[g.length - 1].end) });
  };
  for (const g of glyphs) {
    if (!cur || cur.word !== g.word) {
      flush();
      cur = { word: g.word, glyphs: [] };
    }
    cur.glyphs.push(g);
  }
  flush();
  const { translated, ruby, sora, ...rest } = src;
  const from = glyphs[0].pos;
  const to = glyphs[glyphs.length - 1].pos + 1;
  const out: LyricLine = {
    ...rest,
    words,
    start: first ? src.start : words[0].start,
    end: last ? src.end : words[words.length - 1].end,
  };
  if (first && translated) out.translated = translated;
  // 注音、空耳跟著字走（切點不會落在注音中間）
  const within = (spans: LyricRuby[] | undefined) =>
    (spans ?? []).filter((r) => r.at >= from && r.at + r.len <= to).map((r) => ({ ...r, at: r.at - from }));
  const keptRuby = within(ruby);
  if (keptRuby.length) out.ruby = keptRuby;
  const keptSora = within(sora);
  if (keptSora.length) out.sora = keptSora;
  return out;
}

function split(glyphs: Glyph[], measure: Measure, maxWidth: number, depth: number): Glyph[][] {
  const text = textOf(glyphs);
  if (depth >= 3 || measure(text) <= maxWidth) return [glyphs];
  const { soft, hard } = cutPoints(glyphs);
  const widest = (p: number) =>
    Math.max(measure(textOf(trimmed(glyphs.slice(0, p)))), measure(textOf(trimmed(glyphs.slice(p)))));
  const bestOf = (points: number[]) => {
    let best = -1;
    let bestScore = Infinity;
    for (const p of points) {
      const score = widest(p);
      if (score < bestScore) [best, bestScore] = [p, score];
    }
    return { best, bestScore };
  };
  // a pause wins unless it leaves the halves badly lopsided
  const atPause = bestOf(soft);
  const anywhere = bestOf(hard);
  const best =
    atPause.best >= 0 && (anywhere.best < 0 || atPause.bestScore <= anywhere.bestScore * 1.25)
      ? atPause.best
      : anywhere.best;
  if (best < 0) return [glyphs];
  const left = trimmed(glyphs.slice(0, best));
  const right = trimmed(glyphs.slice(best));
  if (!left.length || !right.length) return [glyphs];
  return [
    ...split(left, measure, maxWidth, depth + 1),
    ...split(right, measure, maxWidth, depth + 1),
  ];
}

/** `lines` with every line wider than `maxWidth` cut into lines that fit. */
export function fitLines(lines: LyricLine[], measure: Measure, maxWidth: number): LyricLine[] {
  if (!(maxWidth > 0)) return lines;
  const out: LyricLine[] = [];
  for (const line of lines) {
    const glyphs = glyphsOf(line);
    const pieces = glyphs.length ? split(glyphs, measure, maxWidth, 0) : [glyphs];
    if (pieces.length <= 1) {
      out.push(line);
      continue;
    }
    pieces.forEach((piece, i) => out.push(toLine(line, piece, i === 0, i === pieces.length - 1)));
  }
  return out;
}
