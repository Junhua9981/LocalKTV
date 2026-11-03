/**
 * 對唱保留原唱：男女對唱時，挑一位（或合唱段）的句子放原唱人聲，其餘自己唱。
 *
 * AI 人聲軌是兩個人混在一起的，拆不開男女，所以照歌詞的演唱者小標
 * （lyrics_parse 給每行的 `part`，0 = 合唱）決定每個時間點要不要放原唱：
 *   - 保留的句子提早 LEAD_MS 打開，好讓淡入在第一個字之前完成；
 *   - 句子之間的空檔維持原狀（下一句也保留就一直開著），不會一句一開關；
 *   - 下一句換你唱時，在下一句開始前 LEAD_MS 關掉，最多留 TAIL_MS 尾音；
 *     保留的那句本身永遠唱完，不會被切掉。
 */
import type { LyricLine } from "./lyrics";

export const LEAD_MS = 150;
export const TAIL_MS = 400;

/** One duet voice on the remote: `part` as in the lyrics, `singer` its label. */
export interface DuetPart {
  part: number;
  singer: string;
  lines: number;
}

/** The voices of a duet in order of first appearance; [] when it is not one. */
export function duetParts(lines: LyricLine[]): DuetPart[] {
  const parts = new Map<number, DuetPart>();
  for (const line of lines) {
    if (typeof line.part !== "number") continue;
    const known = parts.get(line.part);
    if (known) known.lines += 1;
    else parts.set(line.part, { part: line.part, singer: line.singer ?? "", lines: 1 });
  }
  const solos = [...parts.values()].filter((p) => p.part > 0);
  if (solos.length < 2) return []; // one marked voice is not a duet
  return [...parts.values()].sort((a, b) => a.part - b.part);
}

/** Song-time spans [start, end) (ms, lyric time) where the original vocal plays. */
export function keptSpans(lines: LyricLine[], keep: readonly number[]): [number, number][] {
  if (!keep.length) return [];
  const kept = new Set(keep);
  const sorted = [...lines].sort((a, b) => a.start - b.start);
  const spans: [number, number][] = [];
  sorted.forEach((line, i) => {
    if (typeof line.part !== "number" || !kept.has(line.part)) return;
    const next = sorted[i + 1];
    const nextKept = next !== undefined && typeof next.part === "number" && kept.has(next.part);
    const start = line.start - LEAD_MS;
    let end = line.end + TAIL_MS;
    if (next && nextKept) end = Math.max(end, next.start); // bridge the gap
    else if (next) end = Math.max(line.end, Math.min(end, next.start - LEAD_MS));
    const last = spans[spans.length - 1];
    if (last && start <= last[1]) last[1] = Math.max(last[1], end);
    else spans.push([start, end]);
  });
  return spans;
}

/** Is `t` (ms, lyric time) inside one of the spans? */
export function keptAt(spans: readonly [number, number][], t: number): boolean {
  let lo = 0;
  let hi = spans.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const [start, end] = spans[mid];
    if (t < start) hi = mid - 1;
    else if (t >= end) lo = mid + 1;
    else return true;
  }
  return false;
}

/** Lines the singer sings themself — what the score and the pitch bar follow. */
export function sungLines(lines: LyricLine[], keep: readonly number[]): LyricLine[] {
  if (!keep.length) return lines;
  const kept = new Set(keep);
  return lines.filter((line) => typeof line.part !== "number" || !kept.has(line.part));
}
