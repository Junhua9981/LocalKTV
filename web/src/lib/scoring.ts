/**
 * 評分規則（純計算，不碰瀏覽器 API，方便測試）。
 *
 * 標準答案來自伺服器從 AI 人聲軌抽出的音高（pitch.json，ktv/pitch_ref.py），時間是
 * 「歌曲音檔」的時間，音高是 MIDI × 100 音分。麥克風那邊每 10ms 送一筆
 * {t: 歌曲時間 ms, cents: 唱的音高（null = 沒唱）}。
 *
 * 標準音怎麼切（wordNotes）：有逐字歌詞時一個字一個音——只看那個字唱的時間
 * （前 50ms、後 100ms），伴奏漏進人聲軌、沒人唱的地方就不會出標準音；字裡面前後
 * 兩段各穩 120ms 以上、差 80 音分以上才當轉音拆開。合唱時人聲軌裡同時有兩個人，
 * 一個字的音高會分成交錯出現的兩群：兩個聲部都當標準音，唱哪一個都算對。
 * 沒有逐字歌詞就用伺服器照音高切好的 notes，有整行歌詞時只留歌詞行裡的。
 *
 *   音準 70％：有標準音的時間點，唱的音高離標準多近。不分八度（男生唱女生的歌
 *             低八度也算對）；±50 音分內滿分，到 ±150 音分線性降到 0。
 *   節奏 20％：該唱的時候有沒有出聲。
 *   穩定 10％：0.4 秒以上的長音，音高有沒有抖掉（標準差 30 音分內滿分，120 歸零）。
 *
 * 時間容許前後 SLACK_MS 的誤差：同一筆聲音會跟附近的標準音都比一次（各算進離它最近
 * 的一格），錄音延遲估得不準時才不會整首被判走音。
 *
 * 一句（歌詞的一行；沒有歌詞時用停頓切句）結束就給一次判定，整首結束換成 0–100 分。
 */

export type RefNote = [start: number, dur: number, cents: number];

/** 一段連續有音高的格子：[第幾格, 每格的音高（MIDI 音分）] */
export type PitchRun = [first: number, cents: number[]];

export interface PitchDoc {
  version: number;
  notes: RefNote[];
  /** 第 2 版起：逐格音高（第 i 格在 first_ms + i × hop 毫秒） */
  hop?: number;
  first_ms?: number;
  frames?: PitchRun[];
}

/** 歌詞的一行（已換成歌曲時間）；words 只有逐字歌詞才給 */
export interface ScoreLine {
  start: number;
  end: number;
  words?: { start: number; end: number }[];
}

/** 切好的標準音。group ≥ 0：合唱同一個字的不同聲部，唱哪一個都算 */
export interface BuiltNote {
  start: number;
  end: number;
  cents: number;
  group: number;
}

export interface ScoreFrame {
  /** 歌曲時間（ms），已扣掉所有延遲 */
  t: number;
  /** 唱的音高（MIDI 音分），null = 沒出聲或聽不出音高 */
  cents: number | null;
}

export type Judgement = "PERFECT" | "GREAT" | "GOOD" | "MISS";

export interface LineResult {
  index: number;
  start: number;
  end: number;
  /** 0..1 */
  pitch: number;
  rhythm: number;
  stability: number | null;
  score: number;
  judgement: Judgement;
}

export interface SongResult {
  /** 0–100，給人看的分數 */
  score: number;
  /** 0..1 各項平均（依每句的標準音長度加權） */
  pitch: number;
  rhythm: number;
  stability: number;
  lines: LineResult[];
  counts: Record<Judgement, number>;
}

export const FULL_CENTS = 50;
export const ZERO_CENTS = 150;
export const SLACK_MS = 120;
const LONG_NOTE_MS = 400;
const STABLE_SD = 30;
const UNSTABLE_SD = 120;
/** 沒有歌詞時，標準音之間停這麼久就算換句 */
const PHRASE_GAP_MS = 900;
/** 一個字前後多看多久（唱得比歌詞早一點、尾音拖長一點） */
const WORD_PRE_MS = 50;
const WORD_POST_MS = 100;
/** 只有整行歌詞時，行外多久內的標準音還留著 */
const LINE_SLACK_MS = 100;
/** 一個字至少要有幾格音高才出標準音（60ms） */
const MIN_FRAMES = 6;
/** 同一個字裡的音高差這麼多就分成兩群（合唱的兩個聲部，或大跳的轉音） */
const VOICE_GAP_CENTS = 250;
/** 兩個聲部不分八度只差這麼多音分：男女差八度齊唱，算一個音 */
const OCTAVE_UNISON_CENTS = 100;
/** 一群至少要佔這個字的幾成才算數（偶爾跳八度的格子不算一群） */
const VOICE_SHARE = 0.2;
/** 轉音：前後兩段各穩這麼多格（120ms）、差這麼多音分才拆 */
const PLATEAU_FRAMES = 12;
const MELISMA_CENTS = 80;
/** 一個字最多拆成幾個音 */
const MAX_SPLIT_DEPTH = 2;
const WEIGHTS = { pitch: 0.7, rhythm: 0.2, stability: 0.1 };

/** a 和 b 差幾音分，不分八度：結果在 −600..600 */
export function centsOff(sung: number, ref: number): number {
  const d = (((sung - ref) % 1200) + 1200) % 1200;
  return d > 600 ? d - 1200 : d;
}

/** 音高差 → 0..1 */
export function closeness(off: number): number {
  const a = Math.abs(off);
  if (a <= FULL_CENTS) return 1;
  if (a >= ZERO_CENTS) return 0;
  return 1 - (a - FULL_CENTS) / (ZERO_CENTS - FULL_CENTS);
}

export function judge(score: number): Judgement {
  if (score >= 0.85) return "PERFECT";
  if (score >= 0.7) return "GREAT";
  if (score >= 0.5) return "GOOD";
  return "MISS";
}

/** 0..1 → 0–100。刻意放寬：認真唱的人大多落在 70–90。 */
export function displayScore(raw: number): number {
  return Math.round(100 * Math.pow(Math.max(0, Math.min(1, raw)), 0.6));
}

interface NoteStat {
  start: number;
  end: number;
  cents: number;
  group: number;
  /** 每 10ms 一格：該格唱得多準（0..1），沒唱 = −1，還沒聽到 = NaN */
  cells: Float32Array;
  /** 每格唱的音高（穩定度用），沒唱 = NaN */
  sung: Float32Array;
}

interface Phrase {
  start: number;
  end: number;
  notes: number[];
}

const CELL_MS = 10;

export class SongScorer {
  readonly notes: NoteStat[];
  readonly phrases: Phrase[];
  private keyShift = 0;
  private done = new Set<number>();
  private results: (LineResult | null)[];
  /** 每句結束（歌曲時間過了句尾 + SLACK）時呼叫 */
  onLine: ((line: LineResult) => void) | null = null;

  /**
   * @param lines 歌詞每行的 [開始, 結束]（已換成歌曲時間）；空的就用停頓切句
   */
  constructor(doc: PitchDoc, lines: ScoreLine[] = []) {
    this.notes = buildNotes(doc, lines)
      .filter((n) => n.end > n.start)
      .sort((a, b) => a.start - b.start)
      .map((n) => {
        const count = Math.max(1, Math.round((n.end - n.start) / CELL_MS));
        return {
          ...n,
          cells: new Float32Array(count).fill(NaN),
          sung: new Float32Array(count).fill(NaN),
        };
      });
    this.notes.forEach((n, i) => {
      if (n.group < 0) return;
      const list = this.groups.get(n.group) ?? [];
      list.push(i);
      this.groups.set(n.group, list);
    });
    this.phrases = lines.length ? this.byLines(lines) : this.byGaps();
    this.results = this.phrases.map(() => null);
  }

  /** 合唱：group → 那個字的各聲部（音符編號） */
  private groups = new Map<number, number[]>();

  /** 合唱的字目前唱得比較貼近哪個聲部（還沒唱就是第一個） */
  private preferred(group: number): number {
    const list = this.groups.get(group) ?? [];
    let best = list[0];
    let bestSum = -1;
    for (const i of list) {
      let sum = 0;
      for (const v of this.notes[i].cells) if (v > 0) sum += v;
      if (sum > bestSum) {
        bestSum = sum;
        best = i;
      }
    }
    return best;
  }

  /** 這句要算分的音符：合唱的字只算唱得比較準的那個聲部 */
  private counted(p: Phrase): number[] {
    return p.notes.filter((i) => {
      const g = this.notes[i].group;
      return g < 0 || this.preferred(g) === i;
    });
  }

  /** 升降 Key（半音）：標準音一起移 */
  setKey(semitones: number): void {
    this.keyShift = semitones * 100;
  }

  get keyCents(): number {
    return this.keyShift;
  }

  private byLines(lines: { start: number; end: number }[]): Phrase[] {
    const phrases: Phrase[] = lines.map((l) => ({ start: l.start, end: l.end, notes: [] }));
    this.notes.forEach((note, i) => {
      // 音符歸給重疊最多的那一行
      let best = -1;
      let overlap = 0;
      phrases.forEach((p, k) => {
        const o = Math.min(p.end, note.end) - Math.max(p.start, note.start);
        if (o > overlap) {
          overlap = o;
          best = k;
        }
      });
      if (best >= 0) phrases[best].notes.push(i);
    });
    return phrases
      .filter((p) => p.notes.length)
      .map((p) => ({
        start: Math.min(p.start, this.notes[p.notes[0]].start),
        end: Math.max(p.end, this.notes[p.notes[p.notes.length - 1]].end),
        notes: p.notes,
      }));
  }

  private byGaps(): Phrase[] {
    const phrases: Phrase[] = [];
    this.notes.forEach((note, i) => {
      const last = phrases[phrases.length - 1];
      if (last && note.start - last.end < PHRASE_GAP_MS) {
        last.notes.push(i);
        last.end = Math.max(last.end, note.end);
      } else {
        phrases.push({ start: note.start, end: note.end, notes: [i] });
      }
    });
    return phrases;
  }

  /** 第一個 end > t 的音符 */
  private firstAfter(t: number): number {
    let lo = 0;
    let hi = this.notes.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (this.notes[mid].end <= t) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }

  /**
   * 現在這個時間點的標準音（音高條用），含升降 Key。合唱有兩個聲部時，給了 sung
   * 就挑離它近的那個（不分八度）。
   */
  noteAt(t: number, sung?: number): { start: number; end: number; cents: number } | null {
    let best: NoteStat | null = null;
    for (let i = this.firstAfter(t); i < this.notes.length && this.notes[i].start <= t; i++) {
      const n = this.notes[i];
      if (n.end <= t) continue;
      if (!best || (sung !== undefined &&
          Math.abs(centsOff(sung, n.cents + this.keyShift)) < Math.abs(centsOff(sung, best.cents + this.keyShift)))) {
        best = n;
      }
    }
    return best ? { start: best.start, end: best.end, cents: best.cents + this.keyShift } : null;
  }

  /**
   * t 前後一段時間內的標準音（音高條畫面用）。lead = false：合唱裡目前唱得比較
   * 不像的那個聲部，畫淡一點。
   */
  notesBetween(from: number, to: number): { start: number; end: number; cents: number; lead: boolean }[] {
    const out = [];
    // firstAfter 照 end 找，合唱兩個聲部長短不同時往前多看一點
    for (let i = Math.max(0, this.firstAfter(from) - 2); i < this.notes.length && this.notes[i].start < to; i++) {
      const n = this.notes[i];
      if (n.end <= from) continue;
      const lead = n.group < 0 || this.preferred(n.group) === i;
      out.push({ start: n.start, end: n.end, cents: n.cents + this.keyShift, lead });
    }
    return out;
  }

  /**
   * 收一筆麥克風的聲音。回傳這筆唱得多準（0..1，沒有標準音可比 = null），
   * 音高條拿來決定點要不要亮。
   */
  feed(frame: ScoreFrame): number | null {
    let hit: number | null = null;
    const from = this.firstAfter(frame.t - SLACK_MS);
    for (let i = from; i < this.notes.length; i++) {
      const n = this.notes[i];
      if (n.start > frame.t + SLACK_MS) break;
      // 這筆聲音只算進這個音離它最近的一格：唱多久就填多少格，延遲估得不準時
      // 也只是對到音的邊上，不會一筆聲音就把整個音填滿
      const c = Math.max(0, Math.min(n.cells.length - 1, Math.floor((frame.t - n.start) / CELL_MS)));
      const q = frame.cents === null ? -1 : closeness(centsOff(frame.cents, n.cents + this.keyShift));
      if (q >= 0) hit = Math.max(hit ?? 0, q);
      const old = n.cells[c];
      if (Number.isNaN(old) || q > old) {
        n.cells[c] = q;
        if (q >= 0 && frame.cents !== null) n.sung[c] = frame.cents;
      }
    }
    this.flush(frame.t);
    return hit;
  }

  /** 跳段（往回拉）：從 t 之後重新算 */
  rewind(t: number): void {
    for (const n of this.notes) {
      if (n.end <= t) continue;
      const c0 = Math.max(0, Math.floor((t - n.start) / CELL_MS));
      n.cells.fill(NaN, c0);
      n.sung.fill(NaN, c0);
    }
    this.phrases.forEach((p, k) => {
      if (p.end + SLACK_MS > t) {
        this.done.delete(k);
        this.results[k] = null;
      }
    });
  }

  /** 句尾過了就結算那一句 */
  private flush(t: number): void {
    this.phrases.forEach((p, k) => {
      if (this.done.has(k) || p.end + SLACK_MS > t) return;
      this.done.add(k);
      const line = this.scoreLine(k);
      this.results[k] = line;
      this.onLine?.(line);
    });
  }

  private scoreLine(k: number): LineResult {
    const p = this.phrases[k];
    let total = 0;
    let pitchSum = 0;
    let voiced = 0;
    let stabSum = 0;
    let stabW = 0;
    for (const i of this.counted(p)) {
      const n = this.notes[i];
      for (let c = 0; c < n.cells.length; c++) {
        const v = n.cells[c];
        total++;
        if (v >= 0) {
          pitchSum += v;
          voiced++;
        }
      }
      if (n.end - n.start >= LONG_NOTE_MS) {
        const sd = spread(n.sung);
        if (sd !== null) {
          const w = n.end - n.start;
          stabSum += w * (sd <= STABLE_SD ? 1 : sd >= UNSTABLE_SD ? 0 : 1 - (sd - STABLE_SD) / (UNSTABLE_SD - STABLE_SD));
          stabW += w;
        } else {
          stabW += n.end - n.start; // 長音整個沒唱：穩定度 0
        }
      }
    }
    const pitch = total ? pitchSum / total : 0;
    const rhythm = total ? voiced / total : 0;
    const stability = stabW ? stabSum / stabW : null;
    const score =
      stability === null
        ? (WEIGHTS.pitch * pitch + WEIGHTS.rhythm * rhythm) / (WEIGHTS.pitch + WEIGHTS.rhythm)
        : WEIGHTS.pitch * pitch + WEIGHTS.rhythm * rhythm + WEIGHTS.stability * stability;
    return { index: k, start: p.start, end: p.end, pitch, rhythm, stability, score, judgement: judge(score) };
  }

  /** 唱到最後（或中途切歌）：還沒結算的句子照目前聽到的算，沒聽到的算沒唱 */
  finish(): SongResult {
    const lines = this.phrases.map((_p, k) => this.results[k] ?? this.scoreLine(k));
    const counts: Record<Judgement, number> = { PERFECT: 0, GREAT: 0, GOOD: 0, MISS: 0 };
    let w = 0;
    let raw = 0;
    let pitch = 0;
    let rhythm = 0;
    let stab = 0;
    let stabW = 0;
    lines.forEach((line, k) => {
      counts[line.judgement]++;
      const weight = this.counted(this.phrases[k]).reduce((s, i) => s + this.notes[i].end - this.notes[i].start, 0);
      w += weight;
      raw += weight * line.score;
      pitch += weight * line.pitch;
      rhythm += weight * line.rhythm;
      if (line.stability !== null) {
        stab += weight * line.stability;
        stabW += weight;
      }
    });
    return {
      score: displayScore(w ? raw / w : 0),
      pitch: w ? pitch / w : 0,
      rhythm: w ? rhythm / w : 0,
      stability: stabW ? stab / stabW : 0,
      lines,
      counts,
    };
  }

  /** 已經唱過（有聽到任何聲音）的句數，切歌時判斷要不要出分數 */
  get heardLines(): number {
    return this.results.filter((r) => r !== null && r.rhythm > 0).length;
  }
}

/** 長音唱的音高的標準差（音分），先去掉八度跳動；唱不到一半回傳 null */
function spread(sung: Float32Array): number | null {
  const vals: number[] = [];
  for (const v of sung) if (!Number.isNaN(v)) vals.push(v);
  if (vals.length < sung.length / 2) return null;
  const ref = vals[0];
  const folded = vals.map((v) => ref + centsOff(v, ref));
  const mean = folded.reduce((a, b) => a + b, 0) / folded.length;
  return Math.sqrt(folded.reduce((a, b) => a + (b - mean) ** 2, 0) / folded.length);
}

/** pitch.json + 歌詞 → 標準音（見檔頭「標準音怎麼切」） */
export function buildNotes(doc: PitchDoc, lines: ScoreLine[]): BuiltNote[] {
  const words = lines
    .flatMap((l) => l.words ?? [])
    .filter((w) => w.end > w.start)
    .sort((a, b) => a.start - b.start);
  if (doc.frames?.length && words.length) return wordNotes(doc, words);
  const notes = doc.notes.map(([start, dur, cents]) => ({ start, end: start + dur, cents, group: -1 }));
  if (!lines.length) return notes;
  return notes.filter((n) =>
    lines.some((l) => n.end > l.start - LINE_SLACK_MS && n.start < l.end + LINE_SLACK_MS),
  );
}

/** 逐格音高照歌詞的字切成音符 */
export function wordNotes(doc: PitchDoc, words: { start: number; end: number }[]): BuiltNote[] {
  const hop = doc.hop ?? 10;
  const first = doc.first_ms ?? 0;
  const runs = doc.frames ?? [];
  const last = runs.reduce((m, [f, c]) => Math.max(m, f + c.length), 0);
  const grid = new Float32Array(last).fill(NaN);
  for (const [f, cents] of runs) grid.set(cents, f);
  const at = (i: number) => first + i * hop;

  const out: BuiltNote[] = [];
  let group = 0;
  words.forEach((w, k) => {
    // 前後多看一點，但不吃到隔壁字的時間
    const from = Math.max(w.start - WORD_PRE_MS, k > 0 ? words[k - 1].end : -Infinity);
    const to = Math.min(w.end + WORD_POST_MS, k + 1 < words.length ? words[k + 1].start : Infinity);
    const lo = Math.max(0, Math.ceil((from - first) / hop));
    const hi = Math.min(last, Math.ceil((to - first) / hop));
    const idx: number[] = [];
    for (let i = lo; i < hi; i++) if (!Number.isNaN(grid[i])) idx.push(i);
    if (idx.length < MIN_FRAMES) return;

    const voices = clusters(idx, grid).filter((c) => c.length >= Math.max(MIN_FRAMES, VOICE_SHARE * idx.length));
    if (!voices.length) return;
    voices.sort((a, b) => b.length - a.length);
    const [a, b] = voices;
    if (b && interleaved(a, b)) {
      // 差八度的兩個人（男女齊唱）不分八度評分時就是同一個音
      if (Math.abs(centsOff(median(b.map((i) => grid[i])), median(a.map((i) => grid[i])))) < OCTAVE_UNISON_CENTS) {
        for (const n of steps(a, grid, 0)) out.push(note(n, -1));
        return;
      }
      // 合唱：兩個聲部各一個音（各自也可能轉音）
      for (const v of [a, b]) for (const n of steps(v, grid, 0)) out.push(note(n, group));
      group++;
      return;
    }
    // 一個人唱：所有群照時間接回來（大跳的轉音），再看要不要拆
    const all = voices.flat().sort((x, y) => x - y);
    for (const n of steps(all, grid, 0)) out.push(note(n, -1));
  });
  return out;

  function note(n: { first: number; last: number; cents: number }, g: number): BuiltNote {
    return { start: at(n.first), end: at(n.last) + hop, cents: n.cents, group: g };
  }
}

/** 一個字裡的格子照音高分群：排好序後相鄰差超過 VOICE_GAP_CENTS 就斷開。回傳格子編號（依時間） */
function clusters(idx: number[], grid: Float32Array): number[][] {
  const byPitch = [...idx].sort((x, y) => grid[x] - grid[y]);
  const groups: number[][] = [[byPitch[0]]];
  for (let k = 1; k < byPitch.length; k++) {
    if (grid[byPitch[k]] - grid[byPitch[k - 1]] > VOICE_GAP_CENTS) groups.push([]);
    groups[groups.length - 1].push(byPitch[k]);
  }
  return groups.map((g) => g.sort((x, y) => x - y));
}

/** 兩群在時間上交錯（兩個人同時唱），而不是一前一後（轉音） */
function interleaved(a: number[], b: number[]): boolean {
  const overlap = Math.min(a[a.length - 1], b[b.length - 1]) - Math.max(a[0], b[0]);
  const shorter = Math.min(a[a.length - 1] - a[0], b[b.length - 1] - b[0]);
  return overlap > 0 && overlap >= 0.5 * shorter;
}

function median(values: number[]): number {
  const v = [...values].sort((x, y) => x - y);
  const m = v.length >> 1;
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}

/** 一段格子 → 一個音；前後兩段明顯不同音高（轉音）才拆開，最多拆兩層（4 個音） */
function steps(idx: number[], grid: Float32Array, depth: number): { first: number; last: number; cents: number }[] {
  const cents = idx.map((i) => grid[i]);
  const whole = median(cents);
  if (depth < MAX_SPLIT_DEPTH && idx.length >= 2 * PLATEAU_FRAMES) {
    const flat = cents.reduce((s, c) => s + Math.abs(c - whole), 0);
    let best = -1;
    let bestCost = Infinity;
    for (let k = PLATEAU_FRAMES; k <= idx.length - PLATEAU_FRAMES; k++) {
      const a = median(cents.slice(0, k));
      const b = median(cents.slice(k));
      if (Math.abs(a - b) <= MELISMA_CENTS) continue;
      let cost = 0;
      for (let j = 0; j < cents.length; j++) cost += Math.abs(cents[j] - (j < k ? a : b));
      if (cost < bestCost) {
        bestCost = cost;
        best = k;
      }
    }
    // 拆開要明顯比一個平的音貼近才算轉音（滑音、抖音不拆）
    if (best > 0 && bestCost < 0.6 * flat) {
      return [...steps(idx.slice(0, best), grid, depth + 1), ...steps(idx.slice(best), grid, depth + 1)];
    }
  }
  return [{ first: idx[0], last: idx[idx.length - 1], cents: Math.round(whole) }];
}

/** Hz → MIDI 音分 */
export function hzToCents(hz: number): number {
  return 6900 + 1200 * Math.log2(hz / 440);
}
