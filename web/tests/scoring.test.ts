/**
 * 評分規則測試（lib/scoring.ts）。
 *
 * Run:  node --experimental-strip-types web/tests/scoring.test.ts
 */

import {
  SongScorer,
  buildNotes,
  centsOff,
  closeness,
  displayScore,
  type LineResult,
  type PitchDoc,
  type PitchRun,
  type RefNote,
} from "../src/lib/scoring.ts";

let failed = 0;
function ok(cond: boolean, label: string, detail = ""): void {
  console.log(`  [${cond ? "OK " : "FAIL"}] ${label}${detail ? `  (${detail})` : ""}`);
  if (!cond) failed++;
}

// 兩句：每句四個音，音之間留 100ms
const NOTES: RefNote[] = [];
for (let line = 0; line < 2; line++) {
  for (let k = 0; k < 4; k++) {
    NOTES.push([line * 4500 + k * 600, 500, 6000 + k * 200]);
  }
}
NOTES.push([2400, 900, 6700]); // 第一句的長音
const DOC: PitchDoc = { version: 1, notes: NOTES };
const LINES = [
  { start: 0, end: 3400 },
  { start: 4500, end: 6900 },
];

/** 照 sung(t) 唱一整首（每 10ms 一筆），回傳結果 */
function sing(sung: (t: number) => number | null, opts: { key?: number; lines?: typeof LINES } = {}) {
  const s = new SongScorer(DOC, opts.lines ?? LINES);
  if (opts.key) s.setKey(opts.key);
  const got: LineResult[] = [];
  s.onLine = (l) => got.push(l);
  for (let t = 0; t <= 7500; t += 10) s.feed({ t, cents: sung(t) });
  return { result: s.finish(), lines: got, scorer: s };
}

function refAt(t: number): number | null {
  const n = NOTES.find(([start, dur]) => t >= start && t < start + dur);
  return n ? n[2] : null;
}

console.log("1. 音高差");
ok(centsOff(4800, 6000) === 0, "低一個八度算對");
ok(centsOff(6030, 6000) === 30 && centsOff(5970, 6000) === -30, "上下 30 音分");
ok(Math.abs(centsOff(6000 + 1200 + 590, 6000) - 590) < 1e-9, "差距落在 ±600 內");
ok(closeness(50) === 1 && closeness(100) === 0.5 && closeness(150) === 0, "±50 滿分、±150 歸零");

console.log("2. 完全唱對");
const perfect = sing(refAt);
ok(perfect.result.score >= 98, "接近 100 分", String(perfect.result.score));
ok(perfect.lines.length === 2 && perfect.lines.every((l) => l.judgement === "PERFECT"), "兩句都 PERFECT");
ok(perfect.lines[0].index === 0 && perfect.lines[1].index === 1, "句子依序結算");

console.log("3. 低八度唱");
const octave = sing((t) => (refAt(t) === null ? null : refAt(t)! - 1200));
ok(octave.result.score === perfect.result.score, "跟唱對一樣分", String(octave.result.score));

console.log("4. 每個音都差 100 音分");
const off = sing((t) => (refAt(t) === null ? null : refAt(t)! + 100));
ok(Math.abs(off.result.pitch - 0.5) < 0.02, "音準一半", off.result.pitch.toFixed(2));
ok(off.result.rhythm > 0.98, "節奏滿分");
ok(off.result.score < perfect.result.score && off.result.score > 50, "中間的分數", String(off.result.score));

console.log("5. 完全沒唱");
const silent = sing(() => null);
ok(silent.result.score === 0, "0 分", String(silent.result.score));
ok(silent.lines.every((l) => l.judgement === "MISS"), "全部 MISS");

console.log("6. 慢 80ms（延遲沒校準好）");
const late = sing((t) => refAt(t - 80));
ok(late.result.score >= 85, "還是高分", String(late.result.score));

console.log("7. 每個音只唱前半");
const half = sing((t) => {
  const n = NOTES.find(([start, dur]) => t >= start && t < start + dur);
  return n && t < n[0] + n[1] / 2 ? n[2] : null;
});
ok(Math.abs(half.result.rhythm - 0.5) < 0.15, "節奏約一半", half.result.rhythm.toFixed(2));

console.log("8. 升 2 Key");
const key = sing((t) => (refAt(t) === null ? null : refAt(t)! + 200), { key: 2 });
ok(key.result.score === perfect.result.score, "跟著升 Key 唱就是唱對", String(key.result.score));

console.log("9. 沒有歌詞：用停頓切句");
const gaps = sing(refAt, { lines: [] });
ok(gaps.lines.length === 2, "切成兩句", String(gaps.lines.length));

console.log("10. 往回拉重唱");
const s = new SongScorer(DOC, LINES);
for (let t = 0; t <= 7500; t += 10) s.feed({ t, cents: null });
s.rewind(4500);
for (let t = 4500; t <= 7500; t += 10) s.feed({ t, cents: refAt(t) });
const redo = s.finish();
ok(redo.lines[0].judgement === "MISS" && redo.lines[1].judgement === "PERFECT", "第二句重算");

console.log("11. 中途切歌");
const cut = new SongScorer(DOC, LINES);
for (let t = 0; t <= 3600; t += 10) cut.feed({ t, cents: refAt(t) });
const cutResult = cut.finish();
ok(cut.heardLines === 1, "唱了一句", String(cut.heardLines));
ok(cutResult.lines[1].judgement === "MISS", "沒唱到的句子算 MISS");

console.log("12. 分數曲線");
ok(displayScore(1) === 100 && displayScore(0) === 0, "0..1 → 0–100");
ok(displayScore(0.7) >= 75 && displayScore(0.7) <= 85, "七成唱對約 80 分", String(displayScore(0.7)));

// ---- 第 2 版 pitch.json：逐格音高照歌詞的字切 ----
/** 把 [開始 ms, 結束 ms, 音高(t)] 這些片段做成 frames（10ms 一格、first_ms 0） */
function framesOf(parts: [number, number, (t: number) => number][]): PitchRun[] {
  return parts.map(([from, to, f]) => {
    const cents: number[] = [];
    for (let t = from; t < to; t += 10) cents.push(Math.round(f(t)));
    return [from / 10, cents];
  });
}
const flat = (c: number) => () => c;

console.log("13. 一字一音，伴奏漏音不算");
{
  const doc: PitchDoc = {
    version: 2, hop: 10, first_ms: 0, notes: [],
    frames: framesOf([
      [0, 800, flat(4000)], // 前奏：貝斯漏進人聲軌，沒有字
      [1000, 1400, (t) => 6000 + 40 * Math.sin(t / 30)], // 「你」：抖音，不拆
      [1400, 1500, (t) => 6000 + (t - 1400) * 2], // 滑上去
      [1500, 2000, flat(6200)], // 「好」
      [2000, 2600, (t) => (t < 2300 ? 6400 : 6600)], // 「嗎」：轉音，拆成兩個
      [3000, 3500, flat(6100)], // 字之間的空檔：沒人唱卻有音高
    ]),
  };
  const lines = [{ start: 1000, end: 2600, words: [
    { start: 1000, end: 1450 }, { start: 1450, end: 2000 }, { start: 2000, end: 2600 },
  ] }];
  const notes = buildNotes(doc, lines);
  const got = notes.map((n) => `${n.start}-${n.end}:${Math.round(n.cents / 100)}`).join(" ");
  ok(notes.length === 4, "四個音：你、好、嗎（轉音兩個）", got);
  ok(notes.every((n) => n.start >= 950 && n.end <= 2700), "前奏和空檔沒有標準音", got);
  ok(Math.round(notes[0].cents / 100) === 60 && Math.round(notes[1].cents / 100) === 62, "抖音、滑音不拆", got);
  ok(Math.round(notes[2].cents / 100) === 64 && Math.round(notes[3].cents / 100) === 66, "轉音拆開", got);
  ok(notes.every((n) => n.group < 0), "不是合唱");

  const s = new SongScorer(doc, lines);
  for (let t = 900; t <= 3600; t += 10) s.feed({ t, cents: s.noteAt(t)?.cents ?? null });
  ok(s.finish().score >= 98, "照著唱接近 100 分", String(s.finish().score));
}

console.log("14. 合唱：兩個聲部唱哪個都算");
{
  // 兩個人同時唱：人聲軌的音高每格在 5000 和 6400 之間跳
  const duet = (t: number) => (Math.floor(t / 10) % 3 === 0 ? 5000 : 6400);
  const doc: PitchDoc = { version: 2, hop: 10, first_ms: 0, notes: [], frames: framesOf([[0, 1000, duet]]) };
  const lines = [{ start: 0, end: 1000, words: [{ start: 0, end: 500 }, { start: 500, end: 1000 }] }];
  const notes = buildNotes(doc, lines);
  ok(notes.length === 4 && notes.every((n) => n.group >= 0), "每個字兩個聲部", JSON.stringify(notes));
  for (const sung of [5000, 6400, 5000 + 1200]) {
    const s = new SongScorer(doc, lines);
    for (let t = 0; t <= 1300; t += 10) s.feed({ t, cents: t < 1000 ? sung : null });
    const r = s.finish();
    ok(r.score >= 98 && r.rhythm > 0.95, `唱 ${sung} 滿分，另一聲部不扣節奏`, `${r.score} rhythm ${r.rhythm.toFixed(2)}`);
    const bar = s.notesBetween(0, 1000);
    ok(bar.filter((n) => n.lead).every((n) => Math.abs(centsOff(sung, n.cents)) < 50), "音高條亮唱的那一條");
    ok(Math.abs(centsOff(sung, s.noteAt(300, sung)!.cents)) < 50, "音高條的軌跡貼著唱的那一條");
  }
  const off = new SongScorer(doc, lines);
  for (let t = 0; t <= 1300; t += 10) off.feed({ t, cents: t < 1000 ? 5700 : null });
  ok(off.finish().score < 50, "兩個聲部都沒唱到就不準", String(off.finish().score));
}

{
  // 男女差一個八度齊唱：不分八度評分，就是同一個音
  const unison = (t: number) => (Math.floor(t / 10) % 3 === 0 ? 5000 : 6200);
  const doc: PitchDoc = { version: 2, hop: 10, first_ms: 0, notes: [], frames: framesOf([[0, 500, unison]]) };
  const notes = buildNotes(doc, [{ start: 0, end: 500, words: [{ start: 0, end: 500 }] }]);
  ok(notes.length === 1 && notes[0].group < 0, "差八度齊唱只畫一條", JSON.stringify(notes));
}

console.log("15. 只有整行歌詞：行外的音不算");
{
  const doc: PitchDoc = { version: 1, notes: [[0, 500, 4000], [1000, 500, 6000], [3000, 400, 6100]] };
  const notes = buildNotes(doc, [{ start: 950, end: 1600 }]);
  ok(notes.length === 1 && notes[0].start === 1000, "只留歌詞行裡的", JSON.stringify(notes));
  ok(buildNotes(doc, []).length === 3, "沒有歌詞全留");
}

if (failed) {
  console.log(`FAIL (${failed})`);
  process.exit(1);
}
console.log("PASS");
