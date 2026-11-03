<script setup lang="ts">
/**
 * Bottom karaoke zone for /display — the lower ~30% of the screen.
 *
 * Two fixed slots chosen by the line's index, so lines never swap places:
 *   lines 1, 3, 5… (even index) live at the upper-left,
 *   lines 2, 4, 6… (odd index)  live at the lower-right.
 * A line waits in its own slot and starts singing right where it stands;
 * while it sings, the OTHER slot pre-displays the following line (which
 * will later sing in place, too).
 *
 * 兩句同時唱（對唱疊句、和聲，或兩句開始時間一樣）：前一句還沒唱完、下一句
 * 就開始了 → 兩句都算「正在唱」，各在自己的位置照自己的時間掃色；這段時間
 * 不預告下一句（兩個位置都有人），等前一句唱完空出位置才出來。接下來兩句會
 * 同時唱時，前一句一唱完兩個位置就都拿去預告這兩句。
 *
 * Classic karaoke type: white glyphs with a black outline, plus a second
 * blue-with-white-outline copy of the line.  How that copy appears is told
 * apart by `timing` (decided server-side from the parsed lyrics):
 *
 *   "word" — 逐字: the copy is revealed left-to-right, holding at each word
 *            boundary when the provider ships word timings and sweeping the
 *            line evenly otherwise (plain LRC);
 *   "line" — 逐行: no per-glyph timeline exists, so the whole line fades in
 *            blue at once (0.3s) instead of faking a sweep; the next line
 *            waits in white.
 *
 * Pure renderer of the song clock `now()` (already offset-adjusted), read in
 * its own frame loop: Vue re-renders only when the current line changes; the
 * sweep itself is two transforms written straight to the DOM every frame
 * (see `.fill`), so the browser never repaints the glyphs while they fill.
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import type { LyricLine, LyricTiming } from "../lib/lyrics";
import { lyricFont } from "../lib/lyricfont";

const props = defineProps<{
  lines: LyricLine[];
  /** The song clock (ms) — a function, read every frame, never reactive. */
  now: () => number;
  timing?: LyricTiming;
  /** 日文歌：日文字型（lib/lyricfont.ts LYRIC_FAMILY_JA） */
  japanese?: boolean;
  /** 日文歌輔助（遙控器「日文歌」）：漢字上的假名、歌詞下的中文空耳 */
  kana?: boolean;
  sora?: boolean;
}>();

/** 逐行 render — no sweep, just the whole line lighting up. */
const lineMode = computed(() => props.timing !== "word");

/** Index of the line being sung (-1 before the first) — updated by the frame loop. */
const index = ref(-1);
/** 前一句還沒唱完（和 index 那句同時在唱）— 也由 frame loop 更新。 */
const overlapPrev = ref(false);
/** 這句已唱完，下兩句會同時唱（兩個位置都拿去預告）— 也由 frame loop 更新。 */
const pairAhead = ref(false);

function indexAt(now: number): number {
  let found = -1;
  for (let i = 0; i < props.lines.length; i += 1) {
    if (props.lines[i].start <= now) found = i;
    else break;
  }
  return found;
}

interface VisibleLine {
  i: number;
  line: LyricLine;
  slot: "a" | "b"; // a = upper-left, b = lower-right
  current: boolean;
}

const visible = computed<VisibleLine[]>(() => {
  const out: VisibleLine[] = [];
  const push = (i: number, current: boolean) => {
    out.push({
      i,
      line: props.lines[i],
      slot: i % 2 === 0 ? "a" : "b",
      current,
    });
  };
  const cur = index.value;
  const lines = props.lines;
  if (cur >= 0 && pairAhead.value) {
    // 這句唱完了、接下來兩句會同時唱：兩句都先預告，不要等開唱才冒出來
    push(cur + 1, false);
    push(cur + 2, false);
  } else if (cur >= 0) {
    if (overlapPrev.value) push(cur - 1, true);
    push(cur, true);
    if (!overlapPrev.value && cur + 1 < lines.length) push(cur + 1, false);
  } else if (props.lines.length) {
    push(0, false); // before the first line: shown where it will sing
    if (lines.length > 1 && lines[1].start < lines[0].end) push(1, false); // 開頭就兩句同時唱
  }
  return out.sort((x, y) => (x.slot === y.slot ? 0 : x.slot === "a" ? -1 : 1));
});

/** 0..1 sweep position — how far the blue fill has travelled. */
function fillProgress(line: LyricLine, now: number): number {
  if (now <= line.start) return 0;
  if (now >= line.end) return 1;
  const words = line.words;
  if (words.length <= 1) {
    // plain LRC: sweep evenly across the line's duration
    return Math.min(1, Math.max(0, (now - line.start) / Math.max(1, line.end - line.start)));
  }
  // word timings: hold at each word boundary, sweep across the word
  const glyphCount = (w: { text: string }) => Math.max(1, Array.from(w.text).length);
  let total = 0;
  for (const word of words) total += glyphCount(word);
  let before = 0;
  for (const word of words) {
    const n = glyphCount(word);
    if (now < word.start) return before / total;
    if (now <= word.end) {
      const within = (now - word.start) / Math.max(1, word.end - word.start);
      return Math.min(1, (before + within * n) / total);
    }
    before += n;
  }
  return 1;
}

/*
 * The sweep moves by glyphs, but glyphs are not equally wide ("i" vs "m",
 * Latin vs 漢字) — turn the glyph position into a width position so the fill
 * edge sits on the glyph being sung.  Widths are relative, so one font size
 * serves every screen; re-measured once the lyric font has loaded.
 */
const measureCtx = document.createElement("canvas").getContext("2d");
if (measureCtx) measureCtx.font = lyricFont(100, props.japanese);
watch(
  () => props.japanese,
  (ja) => {
    if (measureCtx) measureCtx.font = lyricFont(100, ja);
    prefixCache.clear();
  },
);
// 空耳行是中文字，用繁中字型量（.sora 的字型）
const soraCtx = document.createElement("canvas").getContext("2d");
if (soraCtx) soraCtx.font = lyricFont(100);
const prefixCache = new Map<string, number[]>();
// 字型切片陸續載入（見 lib/lyricfont.ts）：每載完一批就重量
document.fonts?.addEventListener?.("loadingdone", () => prefixCache.clear());

function widthFraction(text: string, glyphFraction: number, ctx = measureCtx, tag = ""): number {
  if (!ctx || glyphFraction <= 0 || glyphFraction >= 1) return glyphFraction;
  let cum = prefixCache.get(tag + text);
  if (!cum) {
    const glyphs = Array.from(text);
    cum = [0];
    let prefix = "";
    for (const g of glyphs) {
      prefix += g;
      cum.push(ctx.measureText(prefix).width);
    }
    if (prefixCache.size > 200) prefixCache.clear();
    prefixCache.set(tag + text, cum);
  }
  const n = cum.length - 1;
  const total = cum[n];
  if (!n || !(total > 0)) return glyphFraction;
  const pos = glyphFraction * n;
  const i = Math.floor(pos);
  return (cum[i] + (pos - i) * (cum[i + 1] - cum[i])) / total;
}

/*
 * The sweep: `.fill` clips to the line's box; inside it `.fill-move` slides
 * left by the unsung share of the width and `.fill-text` slides back by the
 * same amount, so the blue copy stays put and only its visible window grows.
 * Two composited transforms per frame — no clip-path, no repaint of the
 * outlined glyphs (which weak smart TVs could not keep up with).
 */
const stageEl = ref<HTMLElement | null>(null);
let raf = 0;

function frame() {
  raf = requestAnimationFrame(frame);
  const now = props.now();
  const cur = indexAt(now);
  index.value = cur; // same number → no re-render
  const lines = props.lines;
  overlapPrev.value = cur > 0 && now < lines[cur - 1].end;
  pairAhead.value =
    cur >= 0 && cur + 2 < lines.length && now >= lines[cur].end && !overlapPrev.value && lines[cur + 2].start < lines[cur + 1].end;
  // 每句正在唱的行各掃各的（兩句同時唱時有兩行）
  stageEl.value?.querySelectorAll<HTMLElement>(".line[data-i]").forEach((el) => {
    const line = lines[Number(el.dataset.i)];
    const move = el.querySelector<HTMLElement>(".wrap .fill-move");
    const text = move?.firstElementChild as HTMLElement | null | undefined;
    if (!line || !move || !text) return;
    const glyphs = lineMode.value ? 1 : fillProgress(line, now);
    const hidden = lineMode.value ? 0 : 1 - widthFraction(textOf(line), glyphs);
    if (!slide(move, text, (hidden * 100).toFixed(3))) return;
    // 空耳行跟著同一個進度：唱到原文第幾個字，空耳就掃到對應的那幾個中文字
    const soraMove = el.querySelector<HTMLElement>(".sora .fill-move");
    const soraText = soraMove?.firstElementChild as HTMLElement | null | undefined;
    if (soraMove && soraText) {
      const sora = soraOf(line);
      const soraHidden = lineMode.value ? 0 : 1 - widthFraction(sora, soraFraction(line, glyphs), soraCtx, "sora:");
      slide(soraMove, soraText, (soraHidden * 100).toFixed(3));
    }
  });
}

/** 只在位置變了才寫 DOM；回傳有沒有寫。 */
function slide(move: HTMLElement, text: HTMLElement, pct: string): boolean {
  if (move.dataset.pct === pct) return false;
  move.dataset.pct = pct;
  move.style.transform = `translateX(-${pct}%)`;
  text.style.transform = `translateX(${pct}%)`;
  return true;
}

/** 空耳整行文字（遙控器關掉空耳或不是日文歌 → 空字串，不顯示） */
function soraOf(line: LyricLine): string {
  if (!props.sora || !line.sora?.length) return "";
  return line.sora.map((seg) => seg.text).join("");
}

/** 原文唱到的比例（以字數計）→ 空耳行該唱到的比例（以空耳字數計）。 */
function soraFraction(line: LyricLine, glyphFraction: number): number {
  const segs = line.sora ?? [];
  const total = segs.reduce((n, seg) => n + Array.from(seg.text).length, 0);
  if (!total) return glyphFraction;
  const pos = glyphFraction * Array.from(textOf(line)).length;
  let before = 0;
  for (const seg of segs) {
    const n = Array.from(seg.text).length;
    if (pos < seg.at + seg.len) {
      const within = Math.max(0, Math.min(1, (pos - seg.at) / Math.max(1, seg.len)));
      return (before + within * n) / total;
    }
    before += n;
  }
  return 1;
}

onMounted(() => (raf = requestAnimationFrame(frame)));
onBeforeUnmount(() => cancelAnimationFrame(raf));

/** Duet colour: 0 = 合唱 green, 1..3 = per singer (a 4th singer reuses colour 1). */
function partClass(line: LyricLine): string {
  if (typeof line.part !== "number") return "";
  return line.part === 0 ? "part-0" : `part-${((line.part - 1) % 3) + 1}`;
}

/**
 * 對唱的演唱者小標：浮在行頭上方，底色同該聲部的填色；合唱一律寫「合唱」
 * （標記可能是「合」「浩&aMEI」之類）。獨唱歌沒有 singer → 不顯示。
 */
function singerTag(line: LyricLine): string {
  if (!line.singer || typeof line.part !== "number") return "";
  return line.part === 0 ? "合唱" : line.singer;
}

/** 版面要預留的空間：對唱的演唱者小標、翻譯行（見 .slot-a 的位置） */
const hasDuet = computed(() => props.lines.some((line) => !!singerTag(line)));
const hasTranslation = computed(() => props.lines.some((line) => !!line.translated));
/** 日文假名注音：每行上方預留注音的高度（見 .has-ruby） */
const hasRuby = computed(() => !!props.kana && props.lines.some((line) => !!line.ruby?.length));
/** 中文空耳：每行下方一行小字（見 .sora） */
const hasSora = computed(() => !!props.sora && props.lines.some((line) => !!line.sora?.length));

interface Segment {
  text: string;
  ruby?: string;
}
/** 一行切成「一般文字」與「有注音的漢字」幾段（四層字都用同一份） */
function segmentsOf(line: LyricLine): Segment[] {
  const glyphs = Array.from(textOf(line));
  if (!props.kana || !line.ruby?.length) return [{ text: glyphs.join("") }];
  const out: Segment[] = [];
  let pos = 0;
  for (const r of [...line.ruby].sort((x, y) => x.at - y.at)) {
    if (r.at < pos || r.at + r.len > glyphs.length) continue;
    if (r.at > pos) out.push({ text: glyphs.slice(pos, r.at).join("") });
    out.push({ text: glyphs.slice(r.at, r.at + r.len).join(""), ruby: r.text });
    pos = r.at + r.len;
  }
  if (pos < glyphs.length) out.push({ text: glyphs.slice(pos).join("") });
  return out;
}

function textOf(line: LyricLine): string {
  return line.words.map((word) => word.text).join("");
}
</script>

<template>
  <div
    ref="stageEl"
    class="stage"
    :class="{
      'line-mode': lineMode,
      duet: hasDuet,
      'has-translation': hasTranslation,
      'has-ruby': hasRuby,
      'has-sora': hasSora,
      ja: japanese,
    }"
    :lang="japanese ? 'ja' : undefined"
  >
    <div
      v-for="entry in visible"
      :key="entry.slot + '-' + entry.line.start"
      :data-i="entry.current ? entry.i : undefined"
      class="line"
      :class="[entry.slot === 'a' ? 'slot-a' : 'slot-b', partClass(entry.line)]"
    >
      <div class="wrap">
        <div v-if="singerTag(entry.line)" class="singer-tag">{{ singerTag(entry.line) }}</div>
        <!-- 每份字都是「描邊層 + 字面層」兩層疊起來，見 .edge 的說明；日文漢字上的假名見 .rt -->
        <div class="layer edge"><template v-for="(seg, k) in segmentsOf(entry.line)" :key="k"><span v-if="seg.ruby" class="rb">{{ seg.text }}<span class="rt">{{ seg.ruby }}</span></span><template v-else>{{ seg.text }}</template></template></div>
        <div class="layer face"><template v-for="(seg, k) in segmentsOf(entry.line)" :key="k"><span v-if="seg.ruby" class="rb">{{ seg.text }}<span class="rt">{{ seg.ruby }}</span></span><template v-else>{{ seg.text }}</template></template></div>
        <!-- 逐行: no clip at all — the line lights up blue (fading in) -->
        <Transition name="lit">
          <div v-if="entry.current" class="fill">
            <div class="fill-move">
              <div class="fill-text">
                <div class="layer edge"><template v-for="(seg, k) in segmentsOf(entry.line)" :key="k"><span v-if="seg.ruby" class="rb">{{ seg.text }}<span class="rt">{{ seg.ruby }}</span></span><template v-else>{{ seg.text }}</template></template></div>
                <div class="layer face"><template v-for="(seg, k) in segmentsOf(entry.line)" :key="k"><span v-if="seg.ruby" class="rb">{{ seg.text }}<span class="rt">{{ seg.ruby }}</span></span><template v-else>{{ seg.text }}</template></template></div>
              </div>
            </div>
          </div>
        </Transition>
      </div>
      <!-- 中文空耳：和上面一樣「描邊＋字面」兩層，唱到的部分照同一個進度變色 -->
      <div v-if="soraOf(entry.line)" class="sora">
        <div class="swrap">
          <div class="layer edge">{{ soraOf(entry.line) }}</div>
          <div class="layer face">{{ soraOf(entry.line) }}</div>
          <Transition name="lit">
            <div v-if="entry.current" class="fill">
              <div class="fill-move">
                <div class="fill-text">
                  <div class="layer edge">{{ soraOf(entry.line) }}</div>
                  <div class="layer face">{{ soraOf(entry.line) }}</div>
                </div>
              </div>
            </div>
          </Transition>
        </div>
      </div>
      <!-- 兩句同時唱時只有後開始那句顯示翻譯（兩行翻譯會擠進兩行歌詞中間） -->
      <div v-if="entry.current && entry.i === index && entry.line.translated" class="translation">
        {{ entry.line.translated }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.stage {
  position: relative;
  width: 100%;
  height: 100%;
  pointer-events: none;
  user-select: none;
}

/* the two singing slots live in the bottom ~30% of the screen */
.line {
  position: absolute;
  max-width: min(86vw, 1500px);
  /* 和 lib/lyricfont.ts 一致；思源黑體有真正的 900 字重，不靠瀏覽器合成粗體 */
  font-family: "Noto Sans TC", "PingFang TC", "Microsoft JhengHei", sans-serif;
  font-weight: 900;
  /* 字級改了要一起改 lib/lyricfont.ts；前一行給不認得 clamp() 的舊瀏覽器 */
  font-size: 4.6vw;
  font-size: clamp(32px, 4.6vw, 82px);
  line-height: 1.3;
}

/* 日文歌：日文字形在前，和 lib/lyricfont.ts LYRIC_FAMILY_JA 一致 */
.ja .line {
  font-family: "Noto Sans JP", "Noto Sans TC", "Hiragino Sans", "Yu Gothic", "Microsoft JhengHei", sans-serif;
}

/*
 * lines 1, 3, 5… — upper-left, stacked right above the lower-right line.
 * 由下往上算，單位都是 em（= 歌詞字級）：右下行 1.3em、它頭上的演唱者小標、
 * 左上行唱到時才出現在自己下方的翻譯行、間距，再加上自己 1.3em。字級跟著螢幕寬、
 * 歌詞區高度跟著螢幕高，所以不能用百分比放：矮的螢幕（筆電、電視瀏覽器有工具列）
 * 兩行會疊在一起。用 top 而不是 bottom，翻譯行出現時這行才不會往上跳。
 */
.line {
  --sora: 0em; /* 中文空耳行的高度（見 .sora） */
}
.has-sora .line {
  --sora: 0.56em;
}
.slot-a {
  --below: 0em; /* 小標＋翻譯行的預留 */
  --ruby: 0em; /* 假名注音的預留（兩行各一份，見 .has-ruby .layer） */
  left: 6vw;
  top: calc(100% - 1.5vh - 2.6em - 0.35em - var(--below) - 2 * var(--ruby) - 2 * var(--sora));
  text-align: left;
}
.duet .slot-a {
  --below: 0.66em;
}
.has-translation .slot-a {
  --below: 0.7em;
}
.duet.has-translation .slot-a {
  --below: 1.36em;
}
.has-ruby .slot-a {
  --ruby: 0.42em;
}

/* lines 2, 4, 6… — lower-right of the zone */
.slot-b {
  right: 6vw;
  bottom: 1.5vh;
  text-align: right;
}

/* shrink-to-fit so the clip sweep tracks the glyphs, not the box */
.wrap {
  position: relative;
  display: inline-block;
  max-width: 100%;
}

.layer {
  white-space: pre-wrap;
  /* 陰影已移除：黑邊負責對比，粗體負責份量（fill 共用寬度 → 掃描兩層一致） */
}
/*
 * 假名注音：印在漢字正上方、置中，絕對定位所以不佔寬度（掃描、切長句照舊量字）。
 * 有注音的歌每一層字（描邊、字面、藍色那份）頂端都多留一樣高的空間，四層對得齊，
 * 藍色那份也不會把注音裁掉——注音跟著字一起變色。
 */
.has-ruby .wrap .layer {
  padding-top: 0.42em;
}
.rb {
  position: relative;
}
.rt {
  position: absolute;
  left: 50%;
  bottom: 100%;
  margin-bottom: -0.55em;
  transform: translateX(-50%);
  font-size: 0.4em;
  line-height: 1;
  white-space: nowrap;
  letter-spacing: 0.02em;
}

/*
 * 描邊在字的外面：底下一層只畫描邊（字面同色），上面再疊一層沒描邊的字面，
 * 描邊壓在字面下方、只露出外側一半。等於 paint-order: stroke fill，但不靠它：
 * HTML 文字的 paint-order 要 Chrome 123，智慧電視的舊 WebView 會把描邊畫在
 * 字上面，字被吃細、發黑。
 */
.layer.edge {
  position: absolute;
  inset: 0;
  color: #000;
  -webkit-text-stroke: 0.075em #000;
}

/* 字面在描邊之上（同為定位元素，後面的蓋前面）；它也撐開 .wrap 的大小 */
.layer.face {
  position: relative;
  color: #fff;
}

/* the sung copy: solid blue with a white outline, revealed left-to-right */
.fill {
  position: absolute;
  inset: 0;
  overflow: hidden;
}
.fill-move,
.fill-text {
  position: absolute;
  inset: 0;
  will-change: transform;
}
/* the sliding window: shows only the sung share of the blue copy */
.fill-move {
  overflow: hidden;
}
/*
 * 逐字 starts fully hidden; the frame loop then writes inline transforms.
 * Kept in CSS rather than a :style binding, which Vue would re-apply (and
 * so reset the sweep) whenever the stage re-renders.
 */
.stage:not(.line-mode) .fill-move {
  transform: translateX(-100%);
}
.stage:not(.line-mode) .fill-text {
  transform: translateX(100%);
}
.fill .layer.edge {
  color: #fff;
  -webkit-text-stroke-color: #fff;
}
.fill .layer.face {
  color: var(--blue);
}

/* duets: each singer's lines fill in their own colour, 合唱 green */
.part-0 .fill .layer.face {
  color: var(--part-0);
}
.part-1 .fill .layer.face {
  color: var(--part-1);
}
.part-2 .fill .layer.face {
  color: var(--part-2);
}
.part-3 .fill .layer.face {
  color: var(--part-3);
}

/*
 * 演唱者小標：絕對定位在 .wrap 上方、對齊行頭（右下那行也對齊它的第一個字），
 * 不佔版面，不影響掃描與倒數點的量測。底色 = 該聲部顏色（無 part class 時藍）。
 */
.singer-tag {
  position: absolute;
  left: 0;
  bottom: 100%;
  margin-bottom: 0.12em;
  padding: 0.06em 0.62em 0.1em;
  border-radius: 999px;
  background: var(--blue);
  box-shadow: 0 0 0 0.05em #000;
  color: #fff;
  font-size: 0.36em;
  line-height: 1.3;
  white-space: nowrap;
  text-shadow:
    0 0 2px rgba(0, 0, 0, 0.6),
    0 1px 2px rgba(0, 0, 0, 0.5);
}
.part-0 .singer-tag {
  background: var(--part-0);
}
.part-1 .singer-tag {
  background: var(--part-1);
}
.part-2 .singer-tag {
  background: var(--part-2);
}
.part-3 .singer-tag {
  background: var(--part-3);
}

/*
 * 逐行 only (.line-mode): with no per-glyph timeline the whole line becomes
 * the "sung" copy at once — faded in over 0.3s so the switch reads as a
 * deliberate light-up rather than a jump.  逐字 keeps the clip sweep above
 * and never gets these classes' effect (they are scoped to .line-mode).
 */
.line-mode .lit-enter-active {
  transition: opacity 0.3s ease-out;
}

.line-mode .lit-enter-from {
  opacity: 0;
}

/*
 * 中文空耳：歌詞下方一行小字，繁中字型（量字的 soraCtx 也是）。它算在行的高度裡：
 * slot-b 由下往上排所以自動讓出，slot-a 的 top 多扣兩份 --sora（自己的、slot-b 的）。
 */
.sora {
  font-family: "Noto Sans TC", "PingFang TC", "Microsoft JhengHei", sans-serif;
  font-size: 0.42em;
  line-height: 1.2;
  margin-top: 0.14em; /* 0.42 × (1.2 + 0.14) ≈ 0.56em = --sora */
}
.sora .layer.edge {
  -webkit-text-stroke-width: 0.1em;
}
.swrap {
  position: relative;
  display: inline-block;
  max-width: 100%;
}
.translation {
  margin-top: 0.35em;
  font-size: 1.9vw;
  font-size: clamp(16px, 1.9vw, 28px);
  font-weight: 700;
  color: rgba(255, 255, 255, 0.85);
  /* 細字用描邊會被吃掉筆畫（沒有 paint-order 時更糟），改用固定的黑色陰影當邊 */
  text-shadow:
    0 0 2px #000,
    0 0 2px #000,
    0 1px 3px #000;
}
</style>
