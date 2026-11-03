/**
 * Song-level analysis for 文字PV (components/TextPV.vue): everything here is
 * computed once per song, never per frame.
 *
 *   units    — a line's glyphs grouped into words (Intl.Segmenter + a few
 *              merge rules), each glyph keeping its own sung time;
 *   chorus   — the most repeated line texts;
 *   keywords — content words that keep coming back across the song;
 *   mood     — calm / normal / wild, from how densely the song is sung;
 *   palette  — colours sampled from the song's MV (hue from the title when
 *              there is no MV).
 *
 * The ideas follow the folia-major player's lyric visualiser (word-by-word
 * reveal, chorus emphasis, per-song colours); the code is our own.
 */
import type { LyricLine } from "./lyrics";

export interface Glyph {
  ch: string;
  start: number;
  end: number;
}

export interface Unit {
  text: string;
  glyphs: Glyph[];
  start: number;
  end: number;
  /** Latin-ish text: needs a space before the next unit. */
  latin: boolean;
}

export type Mood = 0 | 1 | 2; // calm / normal / wild

// -- deterministic randomness ---------------------------------------------
export function hash(text: string): number {
  let h = 2166136261;
  for (const c of text) h = Math.imul(h ^ (c.codePointAt(0) ?? 0), 16777619);
  return h >>> 0;
}

export function rng(seed: number): () => number {
  let s = seed >>> 0 || 1;
  return () => (s = (Math.imul(s, 1664525) + 1013904223) >>> 0) / 4294967296;
}

const CJK = /[぀-ヿ㐀-鿿가-힯豈-﫿]/;
const PUNCT = /^[\s\p{P}\p{S}]+$/u;
/** Particles that read as the tail of the word before them. */
const PARTICLES = new Set([..."的了著着嗎吗呢吧啊呀喔哦啦麼么嘛唷囉"]);

export function lineText(line: LyricLine): string {
  return line.words.map((w) => w.text).join("");
}

/** Every glyph of the line with its own time (a word's time split evenly). */
function glyphsOf(line: LyricLine): Glyph[] {
  const out: Glyph[] = [];
  const words = line.words.length ? line.words : [{ text: "", start: line.start, end: line.end }];
  for (const word of words) {
    const chars = [...word.text];
    const step = (word.end - word.start) / Math.max(1, chars.length);
    chars.forEach((ch, i) =>
      out.push({ ch, start: word.start + step * i, end: word.start + step * (i + 1) }),
    );
  }
  return out;
}

const segmenter =
  typeof Intl !== "undefined" && "Segmenter" in Intl
    ? new Intl.Segmenter("zh-Hant", { granularity: "word" })
    : null;

/** Split the line text into words; whitespace / punctuation come back flagged. */
function segments(text: string): { text: string; word: boolean }[] {
  if (segmenter) {
    return [...segmenter.segment(text)].map((s) => ({
      text: s.segment,
      word: !PUNCT.test(s.segment),
    }));
  }
  // fallback: CJK one glyph each, latin runs together
  return (text.match(/[A-Za-z0-9'’]+|\s+|./gu) ?? []).map((t) => ({ text: t, word: !PUNCT.test(t) }));
}

/**
 * The line as words: Intl.Segmenter output, then single CJK glyphs paired up
 * (因|爲 → 因爲), particles stuck to the word before, punctuation stuck to
 * its neighbour, and nothing longer than four CJK glyphs.
 */
export function unitsOf(line: LyricLine): Unit[] {
  const glyphs = glyphsOf(line);
  const text = glyphs.map((g) => g.ch).join("");
  type Seg = { chars: Glyph[]; word: boolean; space: boolean };
  const segs: Seg[] = [];
  let at = 0;
  for (const s of segments(text)) {
    const n = [...s.text].length;
    segs.push({ chars: glyphs.slice(at, at + n), word: s.word, space: /^\s+$/.test(s.text) });
    at += n;
  }

  const groups: Glyph[][] = [];
  let pendingSingles: Glyph[][] = [];
  const isCjk = (g: Glyph[]) => g.length > 0 && CJK.test(g[0].ch);
  const flush = () => {
    // pair up runs of single CJK glyphs; an odd one out joins its neighbour
    for (let i = 0; i < pendingSingles.length; i += 2) {
      const pair = pendingSingles.slice(i, i + 2).flat();
      if (pair.length === 1 && groups.length && isCjk(groups[groups.length - 1]) && groups[groups.length - 1].length <= 3) {
        groups[groups.length - 1].push(...pair);
      } else groups.push(pair);
    }
    pendingSingles = [];
  };
  for (const seg of segs) {
    if (seg.space) {
      flush();
      groups.push([]); // word break marker
      continue;
    }
    if (!seg.word) {
      flush();
      // punctuation: tail of the previous word (or head of the next)
      const prev = groups[groups.length - 1];
      if (prev && prev.length) prev.push(...seg.chars);
      else groups.push([...seg.chars]);
      continue;
    }
    const cjk = CJK.test(seg.chars[0]?.ch ?? "");
    if (cjk && seg.chars.length === 1) {
      const prev = groups[groups.length - 1];
      if (PARTICLES.has(seg.chars[0].ch) && !pendingSingles.length && prev && isCjk(prev) && prev.length <= 3) {
        prev.push(seg.chars[0]);
      } else pendingSingles.push(seg.chars);
      continue;
    }
    flush();
    if (cjk && seg.chars.length > 4) {
      for (let i = 0; i < seg.chars.length; i += 3) groups.push(seg.chars.slice(i, i + 3));
    } else groups.push([...seg.chars]);
  }
  flush();

  return groups
    .filter((g) => g.length && g.some((x) => !/\s/.test(x.ch)))
    .map((g) => {
      const visible = g.filter((x) => !/\s/.test(x.ch));
      return {
        text: visible.map((x) => x.ch).join(""),
        glyphs: visible,
        start: visible[0].start,
        end: visible[visible.length - 1].end,
        latin: !CJK.test(visible[0].ch),
      };
    });
}

function normalise(text: string): string {
  return text.replace(/[\s\p{P}\p{S}]+/gu, "").toLowerCase();
}

/** Indices of chorus lines: the line texts repeated most often (≥ 2×). */
export function chorusOf(lines: LyricLine[]): Set<number> {
  const counts = new Map<string, number>();
  const keys = lines.map((l) => normalise(lineText(l)));
  for (const k of keys) if ([...k].length >= 2) counts.set(k, (counts.get(k) ?? 0) + 1);
  const max = Math.max(0, ...counts.values());
  const out = new Set<number>();
  if (max < 2) return out;
  // the top count, or one below it when the top is a lone hook line
  const floor = max >= 4 ? max - 1 : max;
  keys.forEach((k, i) => {
    if ((counts.get(k) ?? 0) >= floor) out.add(i);
  });
  return out;
}

const STOP = new Set([
  "我們", "你們", "他們", "自己", "什麼", "怎麼", "這樣", "那樣", "沒有", "一個", "還是",
  "就是", "可以", "不是", "已經", "因爲", "因為", "所以", "如果", "the", "and", "you",
  "for", "that", "this", "with", "your", "are", "was", "but", "not",
]);

/** Content words that come back on at least three lines — the song's keywords. */
export function keywordsOf(units: Unit[][], max = 5): string[] {
  const seen = new Map<string, Set<number>>();
  units.forEach((line, li) => {
    for (const u of line) {
      const k = normalise(u.text);
      const len = [...k].length;
      if (STOP.has(k) || (u.latin ? len < 4 : len < 2)) continue;
      if (!seen.has(k)) seen.set(k, new Set());
      seen.get(k)!.add(li);
    }
  });
  return [...seen.entries()]
    .filter(([, ls]) => ls.size >= 3)
    .sort((a, b) => b[1].size - a[1].size)
    .slice(0, max)
    .map(([k]) => k);
}

export function keyOf(text: string): string {
  return normalise(text);
}

/** How busy the singing is: glyphs per second of sung time. */
export function moodOf(lines: LyricLine[]): Mood {
  let glyphs = 0;
  let ms = 0;
  for (const l of lines) {
    const n = [...lineText(l).replace(/\s+/g, "")].length;
    if (!n) continue;
    glyphs += n;
    ms += Math.max(1, l.end - l.start);
  }
  const rate = ms ? glyphs / (ms / 1000) : 0;
  return rate < 1.7 ? 0 : rate > 3.4 ? 2 : 1;
}

// -- palette -----------------------------------------------------------------

export interface Palette {
  bg: string;
  /** Soft background blobs. */
  blobs: string[];
  /** Resting text colour. */
  base: string;
  /** Word being sung. */
  accent: string;
  /** Keyword colours (distinct from accent). */
  keys: string[];
}

type Hsl = [number, number, number];

function hsl([h, s, l]: Hsl, a = 1): string {
  return `hsla(${Math.round(((h % 360) + 360) % 360)}, ${Math.round(s * 100)}%, ${Math.round(l * 100)}%, ${a})`;
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

/** Build a palette from up to four dominant [hue, saturation] pairs. */
export function paletteFrom(hues: [number, number][]): Palette {
  const main = hues[0];
  const others = hues.length > 1 ? hues.slice(1) : [[main[0] + 40, main[1]] as [number, number], [main[0] - 50, main[1]] as [number, number]];
  const sat = (s: number, lo: number, hi: number) => clamp(s, lo, hi);
  const blobs = [main, ...others].slice(0, 4).map(([h, s], i) => hsl([h, sat(s, 0.4, 0.75), 0.4 + (i % 2) * 0.08]));
  // the most saturated hue sings
  const loud = [...hues].sort((a, b) => b[1] - a[1])[0] ?? main;
  const accent: Hsl = [loud[0], sat(loud[1] + 0.25, 0.7, 0.95), 0.76];
  const keys = [...others, [main[0] + 150, 0.7] as [number, number]]
    .filter(([h]) => Math.abs(((h - accent[0] + 540) % 360) - 180) > 35)
    .slice(0, 3)
    .map(([h, s]) => hsl([h, sat(s + 0.2, 0.6, 0.9), 0.74]));
  return {
    bg: hsl([main[0], sat(main[1], 0.3, 0.55), 0.1]),
    blobs,
    base: hsl([main[0], 0.3, 0.93]),
    accent: hsl(accent),
    keys: keys.length ? keys : [hsl([accent[0] + 120, 0.75, 0.74])],
  };
}

/** No MV: three analogous hues from the title. */
export function paletteFromSeed(seed: string): Palette {
  const h = hash(seed) % 360;
  return paletteFrom([
    [h, 0.6],
    [h + 45, 0.55],
    [h - 60, 0.5],
  ]);
}

/**
 * Dominant hues of a few RGBA frames: pixels binned by hue, weighted by
 * saturation × brightness (black bars and greys count for little).
 */
export function huesOfPixels(frames: Uint8ClampedArray[]): [number, number][] {
  const BINS = 24;
  const weight = new Float64Array(BINS);
  const satSum = new Float64Array(BINS);
  const hueX = new Float64Array(BINS);
  const hueY = new Float64Array(BINS);
  for (const data of frames) {
    for (let i = 0; i < data.length; i += 4) {
      const r = data[i] / 255;
      const g = data[i + 1] / 255;
      const b = data[i + 2] / 255;
      const max = Math.max(r, g, b);
      const min = Math.min(r, g, b);
      const v = max;
      const s = max ? (max - min) / max : 0;
      if (v < 0.12 || s < 0.12) continue;
      let h: number;
      const d = max - min;
      if (max === r) h = ((g - b) / d) % 6;
      else if (max === g) h = (b - r) / d + 2;
      else h = (r - g) / d + 4;
      h = (h * 60 + 360) % 360;
      const w = s * v;
      const bin = Math.floor(h / (360 / BINS)) % BINS;
      weight[bin] += w;
      satSum[bin] += s * w;
      hueX[bin] += Math.cos((h * Math.PI) / 180) * w;
      hueY[bin] += Math.sin((h * Math.PI) / 180) * w;
    }
  }
  const total = weight.reduce((a, b) => a + b, 0);
  if (total <= 0) return [];
  const order = [...weight.keys()].sort((a, b) => weight[b] - weight[a]);
  const picked: [number, number][] = [];
  for (const bin of order) {
    if (weight[bin] < total * 0.04 || picked.length >= 4) break;
    const h = ((Math.atan2(hueY[bin], hueX[bin]) * 180) / Math.PI + 360) % 360;
    if (picked.some(([p]) => Math.abs(((p - h + 540) % 360) - 180) < 28)) continue;
    picked.push([h, satSum[bin] / weight[bin]]);
  }
  return picked;
}

const paletteCache = new Map<string, Promise<Palette | null>>();

/** Sample a handful of frames from the MV (same-origin, so pixels are readable). */
export function paletteFromVideo(url: string): Promise<Palette | null> {
  const hit = paletteCache.get(url);
  if (hit) return hit;
  const job = new Promise<Palette | null>((resolve) => {
    const video = document.createElement("video");
    video.muted = true;
    video.preload = "auto";
    video.playsInline = true;
    video.crossOrigin = "anonymous";
    const canvas = document.createElement("canvas");
    canvas.width = 64;
    canvas.height = 36;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    const stops = [0.1, 0.22, 0.34, 0.46, 0.58, 0.7, 0.82];
    const frames: Uint8ClampedArray[] = [];
    let i = 0;
    const done = (p: Palette | null) => {
      window.clearTimeout(timer);
      video.removeAttribute("src");
      video.load();
      resolve(p);
    };
    const timer = window.setTimeout(() => done(null), 15000);
    const next = () => {
      if (i >= stops.length || !Number.isFinite(video.duration)) {
        const hues = huesOfPixels(frames);
        done(hues.length ? paletteFrom(hues) : null);
        return;
      }
      video.currentTime = video.duration * stops[i++];
    };
    video.addEventListener("loadedmetadata", next);
    video.addEventListener("seeked", () => {
      try {
        ctx?.drawImage(video, 0, 0, canvas.width, canvas.height);
        const data = ctx?.getImageData(0, 0, canvas.width, canvas.height).data;
        if (data) frames.push(data);
      } catch {
        // tainted / undecodable frame: skip it
      }
      next();
    });
    video.addEventListener("error", () => done(null));
    video.src = url;
  });
  paletteCache.set(url, job);
  return job;
}

// -- motion --------------------------------------------------------------

/** Damped spring (stiffness 200, damping 20, mass 1) from 0 to 1, t in ms. */
export function spring(tMs: number): number {
  if (tMs <= 0) return 0;
  const t = tMs / 1000;
  const w0 = Math.sqrt(200);
  const zeta = 20 / (2 * w0);
  const wd = w0 * Math.sqrt(1 - zeta * zeta);
  return 1 - Math.exp(-zeta * w0 * t) * (Math.cos(wd * t) + ((zeta * w0) / wd) * Math.sin(wd * t));
}

export function ease(x: number): number {
  const t = clamp(x, 0, 1);
  return 1 - (1 - t) ** 3;
}

export function easeInOut(x: number): number {
  const t = clamp(x, 0, 1);
  return t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2;
}

export function lerp(a: number, b: number, t: number): number {
  return a + (b - a) * t;
}

export { clamp };

// -- colour mixing ---------------------------------------------------------------
// color-mix() needs Chrome 111 — a smart TV's old WebView drops the whole
// colour declaration — so the mix is done here, in Oklab like the CSS was.

type Rgba = [number, number, number, number]; // 0..255 ×3, alpha 0..1

const colourCache = new Map<string, Rgba>();
let colourProbe: CanvasRenderingContext2D | null | undefined;

/** Any CSS colour → RGBA, by letting a canvas parse it (hex, rgb(), hsl(), names). */
export function parseColour(css: string): Rgba {
  const hit = colourCache.get(css);
  if (hit) return hit;
  if (colourProbe === undefined) colourProbe = document.createElement("canvas").getContext("2d");
  let out: Rgba = [0, 0, 0, 1];
  if (colourProbe) {
    colourProbe.fillStyle = "#000";
    colourProbe.fillStyle = css;
    const v = String(colourProbe.fillStyle);
    if (v[0] === "#") {
      out = [parseInt(v.slice(1, 3), 16), parseInt(v.slice(3, 5), 16), parseInt(v.slice(5, 7), 16), 1];
    } else {
      const n = v.match(/[\d.]+/g)?.map(Number) ?? [];
      out = [n[0] ?? 0, n[1] ?? 0, n[2] ?? 0, n[3] ?? 1];
    }
  }
  if (colourCache.size > 500) colourCache.clear();
  colourCache.set(css, out);
  return out;
}

const toLinear = (c: number) => {
  const x = c / 255;
  return x <= 0.04045 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4);
};
const fromLinear = (x: number) => {
  const c = x <= 0.0031308 ? x * 12.92 : 1.055 * Math.pow(Math.max(0, x), 1 / 2.4) - 0.055;
  return Math.round(clamp(c, 0, 1) * 255);
};

function toOklab([r, g, b]: Rgba): [number, number, number] {
  const lr = toLinear(r);
  const lg = toLinear(g);
  const lb = toLinear(b);
  const l = Math.cbrt(0.4122214708 * lr + 0.5363325363 * lg + 0.0514459929 * lb);
  const m = Math.cbrt(0.2119034982 * lr + 0.6806995451 * lg + 0.1073969566 * lb);
  const s = Math.cbrt(0.0883024619 * lr + 0.2817188376 * lg + 0.6299787005 * lb);
  return [
    0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  ];
}

function fromOklab([L, A, B]: [number, number, number]): [number, number, number] {
  const l = (L + 0.3963377774 * A + 0.2158037573 * B) ** 3;
  const m = (L - 0.1055613458 * A - 0.0638541728 * B) ** 3;
  const s = (L - 0.0894841775 * A - 1.291485548 * B) ** 3;
  return [
    fromLinear(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
    fromLinear(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
    fromLinear(-0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s),
  ];
}

/**
 * pctA (0..1) of colour a, the rest b, in Oklab with premultiplied alpha —
 * what `color-mix(in oklab, a pctA%, b)` gives (mixing with "transparent"
 * just fades a out).
 */
export function mixOklab(a: string, b: string, pctA: number): string {
  const ca = parseColour(a);
  const cb = parseColour(b);
  const alpha = ca[3] * pctA + cb[3] * (1 - pctA);
  if (alpha <= 0) return "rgba(0, 0, 0, 0)";
  const la = toOklab(ca);
  const lb = toOklab(cb);
  const mixed = [0, 1, 2].map(
    (i) => (la[i] * ca[3] * pctA + lb[i] * cb[3] * (1 - pctA)) / alpha,
  ) as [number, number, number];
  const [r, g, bl] = fromOklab(mixed);
  return `rgba(${r}, ${g}, ${bl}, ${Math.round(alpha * 1000) / 1000})`;
}
