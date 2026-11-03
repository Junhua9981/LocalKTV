/**
 * Four-dot count-in before the vocals: the dots vanish one per beat and the
 * last one goes exactly when the line starts.  Shown before the first sung
 * line and again after a long interlude.
 *
 * There is no BPM in the track data, so the beat is estimated from the
 * lyrics themselves: sung words land on the beat grid, so the gaps between
 * word starts across the whole song pile up at multiples of the beat.
 */
import type { LyricLine } from "./lyrics";

export interface CountIn {
  /** When the line is sung — the last dot vanishes here (lyrics clock, ms). */
  at: number;
  /** Beat length for this count-in (ms); shorter when the intro is short. */
  beat: number;
}

/** Used when the lyrics carry no word timings to estimate from (120 BPM). */
export const DEFAULT_BEAT_MS = 500;
const MIN_BEAT_MS = 375; // 160 BPM
const MAX_BEAT_MS = 1000; // 60 BPM
/** Below this the intro is too short to count anything in. */
/** Prior centre: KTV is mostly ballads, counted in quarter notes (~80 BPM). */
const PRIOR_BEAT_MS = 750;
const MIN_LEAD_MS = 1200;
/** A silence this long between lines (and ≥ 8 beats) gets its own count-in. */
const INTERLUDE_MS = 8000;

const firstSung = (line: LyricLine) => line.words[0]?.start ?? line.start;

/** Beat length (ms) guessed from word onsets, or null with too little data. */
export function estimateBeat(lines: LyricLine[]): number | null {
  const onsets: number[] = [];
  for (const line of lines) for (const word of line.words) onsets.push(word.start);
  onsets.sort((a, b) => a - b);
  const times = onsets.filter((t, i) => i === 0 || t - onsets[i - 1] >= 60);
  if (times.length < 24) return null;

  // histogram of every gap up to ~4 beats, 10 ms bins
  const BIN = 10;
  const maxLag = MAX_BEAT_MS * 4 + 100;
  const hist = new Float64Array(Math.ceil(maxLag / BIN) + 1);
  for (let i = 0; i < times.length; i += 1) {
    for (let j = i + 1; j < times.length; j += 1) {
      const lag = times[j] - times[i];
      if (lag > maxLag) break;
      hist[Math.round(lag / BIN)] += 1;
    }
  }
  // how many gaps fall near `lag` (±4 %, for sloppy timestamps), per bin
  const near = (lag: number) => {
    const tol = Math.max(BIN, lag * 0.04);
    let sum = 0;
    const lo = Math.max(0, Math.floor((lag - tol) / BIN));
    const hi = Math.min(hist.length - 1, Math.ceil((lag + tol) / BIN));
    for (let b = lo; b <= hi; b += 1) sum += hist[b];
    return sum / (hi - lo + 1);
  };
  let best = 0;
  let bestScore = -1;
  for (let beat = MIN_BEAT_MS; beat <= MAX_BEAT_MS; beat += 5) {
    // a real beat also shows up at 2, 3, 4 beats apart
    const fit = near(beat) + 0.8 * near(beat * 2) + 0.6 * near(beat * 3) + 0.5 * near(beat * 4);
    // between equally good octaves (eighths vs quarters) prefer ~80 BPM
    const octaves = Math.log2(beat / PRIOR_BEAT_MS);
    const score = fit * Math.exp(-(octaves * octaves) / (2 * 0.5 * 0.5));
    if (score > bestScore) {
      bestScore = score;
      best = beat;
    }
  }
  return best || null;
}

/** Count-ins for a song: before the first line, then after long interludes.
 *  `wordTimed` = the lyrics carry real 逐字 timings (a line-timed LRC has one
 *  "word" per line, which says nothing about the beat). */
export function buildCountIns(lines: LyricLine[], wordTimed = true): CountIn[] {
  if (!lines.length) return [];
  const beat = (wordTimed ? estimateBeat(lines) : null) ?? DEFAULT_BEAT_MS;
  const out: CountIn[] = [];
  const first = firstSung(lines[0]);
  // short intro: squeeze the four beats into what there is
  if (first >= MIN_LEAD_MS) out.push({ at: first, beat: Math.min(beat, first / 4) });
  for (let i = 1; i < lines.length; i += 1) {
    const at = firstSung(lines[i]);
    const gap = at - lines[i - 1].end;
    if (gap >= Math.max(INTERLUDE_MS, beat * 8)) out.push({ at, beat });
  }
  return out;
}

/** Dots still showing at `now` (0 = hide the count-in). */
export function dotsLeft(countIns: CountIn[], now: number): number {
  for (const c of countIns) {
    const begin = c.at - c.beat * 4;
    if (now < begin) return 0; // sorted: nothing later can be running
    if (now < c.at) return Math.min(4, 4 - Math.floor((now - begin) / c.beat));
  }
  return 0;
}
