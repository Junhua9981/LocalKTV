<script setup lang="ts">
/**
 * 文字PV — the kinetic-lyrics background for /display, shown instead of the
 * MV (session setting `bg_mode`).  Modelled on the lyric visualiser of the
 * folia-major player (word-by-word reveal, chorus emphasis, colours taken
 * from the song); written from scratch for LocalKTV, no third-party code.
 *
 * One line at a time, built word by word as it is sung:
 *   waiting — not on screen yet;
 *   active  — springs in from a scattered pose (blurred, small, tilted) to its
 *             place and grows, taking the singing colour while a halo runs
 *             over its glyphs;
 *   passed  — settles back to its resting size and colour at 82 % and keeps
 *             turning slowly, so the line stays readable as it fills up.
 * The finished line leaves by growing and blurring away.  Words come from
 * lib/textpv.ts (Intl.Segmenter + merge rules), so 中文 moves 一個詞一個詞.
 *
 * What ties it to the song:
 *   colours  — sampled from this song's MV (hue from the title without one);
 *   mood     — calm / normal / wild from how densely the song is sung, which
 *              sets how far words scatter and tilt;
 *   chorus   — the most repeated lines are bigger and ring on every word;
 *   keywords — words that come back across the song get their own colour;
 *   duets    — a line with `part` sings in that singer's colour (--part-N)
 *              under a small singer tag;
 *   music    — the background flow and the singing word pulse with the audio
 *              (AnalyserNode from the audio engine).
 * Everything on the text side is a pure function of the song clock, so seek
 * and pause just work.  Vue only renders which lines are on screen; the
 * frame loop writes the moving parts straight to the DOM (no per-frame
 * re-render, no blur filters — smart-TV GPUs could not keep up).
 */
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from "vue";
import type { LyricLine } from "../lib/lyrics";
import {
  chorusOf,
  clamp,
  ease,
  easeInOut,
  hash,
  keyOf,
  keywordsOf,
  lerp,
  lineText,
  mixOklab,
  moodOf,
  paletteFromSeed,
  paletteFromVideo,
  rng,
  spring,
  unitsOf,
  type Mood,
  type Palette,
  type Unit,
} from "../lib/textpv";

const props = defineProps<{
  lines: LyricLine[];
  /** The song clock (ms, offset-adjusted) — read every frame, never reactive. */
  now: () => number;
  /** Track id — seeds layouts and the fallback colours. */
  seed: string;
  title: string;
  artist: string;
  /** The bottom karaoke lyrics are off: use the whole screen. */
  full: boolean;
  /** This song's MV, used only to pick colours. */
  mvUrl?: string | null;
  /** Taps the audio output for the beat. */
  analyser?: AnalyserNode | null;
  playing?: boolean;
  /** 省電畫面：不畫光暈與漣漪，背景少畫幾次。 */
  lite?: boolean;
}>();

/** A word shows up this long before it is sung. */
const LOOKAHEAD_MS = 150;
/** Line container fade-in before its first word, and the exit length. */
const ENTER_MS = 300;
const EXIT_MS = 320;
/** A gap longer than this between two lines shows the title card. */
const INTERLUDE_MS = 4500;
const FONT_STACK = `"Noto Sans TC", "Microsoft JhengHei", "PingFang TC", "Heiti TC", "Source Han Sans TC", sans-serif`;
const FONT_WEIGHT = 500;

/** Scatter (px at a 72 px font) / tilt / drift (degrees) / size range / float per mood. */
const MOODS = [
  { spread: 12, rot: 3, drift: 16, scale: [0.95, 0.15], float: 10 },
  { spread: 20, rot: 6, drift: 30, scale: [0.92, 0.25], float: 14 },
  { spread: 38, rot: 13, drift: 45, scale: [0.85, 0.42], float: 18 },
] as const;

const PART_FALLBACK = ["#2fd16a", "#3d7bff", "#ff4fa3", "#ffa23d"];

// -- per-song analysis ---------------------------------------------------------
const songSeed = computed(() => hash(props.seed || props.title));
const units = computed<Unit[][]>(() => props.lines.map(unitsOf));
const chorus = computed(() => chorusOf(props.lines));
const mood = computed<Mood>(() => moodOf(props.lines));
const keywords = computed(() => keywordsOf(units.value));
const hasParts = computed(() => props.lines.some((l) => typeof l.part === "number"));

const palette = shallowRef<Palette>(paletteFromSeed(props.title || props.seed));
watch(
  () => [props.mvUrl, props.title, props.seed] as const,
  ([url, title, seed]) => {
    palette.value = paletteFromSeed(title || seed);
    if (!url) return;
    void paletteFromVideo(url).then((p) => {
      if (p && props.mvUrl === url) palette.value = p;
    });
  },
  { immediate: true },
);

const partColors = ref<string[]>(PART_FALLBACK);
onMounted(() => {
  const css = getComputedStyle(document.documentElement);
  partColors.value = PART_FALLBACK.map((c, i) => css.getPropertyValue(`--part-${i}`).trim() || c);
});

/** 0 = 合唱; solo singers past the third cycle back to colour 1. */
function partColor(part: number | undefined): string | null {
  if (typeof part !== "number") return null;
  return partColors.value[part <= 0 ? 0 : ((part - 1) % 3) + 1];
}

/** pctA of colour a, the rest b — like color-mix(in oklab, …), computed here */
function mix(a: string, b: string, pctA: number): string {
  const p = Math.round(clamp(pctA, 0, 1) * 100);
  if (p >= 100) return a;
  if (p <= 0) return b;
  return mixOklab(a, b, p / 100);
}

// -- stage size / text measuring ----------------------------------------------
const stageEl = ref<HTMLElement | null>(null);
const stageW = ref(1280);
const stageH = ref(400);
let observer: ResizeObserver | null = null;

const measureCtx = document.createElement("canvas").getContext("2d");
function measure(text: string, px: number): number {
  if (!measureCtx) return [...text].length * px;
  measureCtx.font = `${FONT_WEIGHT} ${px}px ${FONT_STACK}`;
  return measureCtx.measureText(text).width;
}

interface UnitLayout {
  x: number; // centre, px in the stage
  y: number;
  scale: number;
  rot: number;
  drift: number; // degrees turned over 5 s once sung
  wx: number; // waiting-pose offset
  wy: number;
  ripple: number;
  color: string; // resting colour
  active: string; // singing colour
}

interface LineLayout {
  fontPx: number;
  units: UnitLayout[];
  top: number; // block top, for the singer tag
  left: number;
}

/** Word positions for every line — recomputed on resize / new song only. */
const layouts = computed<LineLayout[]>(() => {
  const W = stageW.value;
  const H = stageH.value;
  const m = MOODS[mood.value];
  const pal = palette.value;
  const keys = hasParts.value ? [] : keywords.value;
  return units.value.map((line, li) => {
    const r = rng(songSeed.value + li * 7919);
    const isChorus = chorus.value.has(li);
    const singer = partColor(props.lines[li].part);
    const scales = line.map(() => m.scale[0] + r() * m.scale[1]);
    let fontPx = Math.min(H * 0.26, W * 0.08) * (isChorus ? 1.14 : 1);
    let rows: number[][] = [];
    let widths: number[] = [];
    for (let attempt = 0; attempt < 10; attempt += 1) {
      // room for the 1.25× pop of the word being sung
      widths = line.map((u, ui) => measure(u.text, fontPx) * scales[ui] * 1.12);
      const gap = fontPx * 0.24;
      rows = [];
      let cur: number[] = [];
      let curW = 0;
      line.forEach((u, ui) => {
        const w = widths[ui] + (u.latin ? fontPx * 0.18 : 0);
        if (cur.length && curW + gap + w > W * 0.92) {
          rows.push(cur);
          cur = [];
          curW = 0;
        }
        curW += (cur.length ? gap : 0) + w;
        cur.push(ui);
      });
      if (cur.length) rows.push(cur);
      const tooTall = rows.length * fontPx * 1.45 > H * 0.92;
      if (rows.length <= 3 && !tooTall) break;
      fontPx *= 0.88;
    }
    const k = fontPx / 72;
    const gap = fontPx * 0.24;
    const rowH = fontPx * 1.42;
    // 0 left / 1 centre / 2 right (calm songs lean to the centre)
    const justify = mood.value === 0 ? [0, 1, 1, 2][Math.floor(r() * 4)] : [0, 1, 2, 1][Math.floor(r() * 4)];
    const anchor = [0.4, 0.5, 0.6][Math.floor(r() * 3)];
    const blockH = rows.length * rowH;
    const top = clamp(H * anchor - blockH / 2, 0, Math.max(0, H - blockH));
    const out: UnitLayout[] = [];
    let left = W;
    rows.forEach((row, ri) => {
      const rowW = row.reduce((s, ui, i) => s + widths[ui] + (i ? gap : 0), 0);
      let x =
        justify === 0
          ? W * 0.04 + ri * fontPx * 0.7
          : justify === 2
            ? W * 0.96 - rowW - (rows.length - 1 - ri) * fontPx * 0.7
            : (W - rowW) / 2;
      x = clamp(x, 0, Math.max(0, W - rowW));
      left = Math.min(left, x);
      const y = top + ri * rowH + rowH / 2;
      for (const ui of row) {
        const u = line[ui];
        const key = keys.indexOf(keyOf(u.text));
        const keyColor = key >= 0 ? pal.keys[key % pal.keys.length] : null;
        const active = singer ?? keyColor ?? pal.accent;
        const color = singer ? mix(singer, pal.base, 0.35) : keyColor ? mix(keyColor, pal.base, 0.7) : pal.base;
        out[ui] = {
          x: x + widths[ui] / 2 + (r() - 0.5) * m.spread * k,
          y: y + (r() - 0.5) * 2 * m.spread * k,
          scale: scales[ui],
          rot: (r() - 0.5) * 2 * m.rot,
          drift: (r() - 0.5) * m.drift,
          wx: (r() - 0.5) * 200 * k,
          wy: (r() - 0.5) * 100 * k,
          ripple: 1.5 + r() * 2,
          color,
          active,
        };
        x += widths[ui] + gap;
      }
    });
    return { fontPx, units: out, top, left };
  });
});

// -- timing --------------------------------------------------------------------

/** When line i starts to leave: a beat after its last word, before the next line. */
const holdEnds = computed(() =>
  props.lines.map((line, i) => {
    const next = props.lines[i + 1];
    const lineUnits = units.value[i];
    const end = Math.max(line.end, lineUnits?.[lineUnits.length - 1]?.end ?? line.end);
    if (!next) return end + 2500;
    return Math.max(end, Math.min(end + 700, next.start - 80));
  }),
);

/** Lines on screen at `now` (the leaving one may overlap the next). */
function visibleAt(now: number): number[] {
  const out: number[] = [];
  for (let i = 0; i < props.lines.length; i += 1) {
    const first = units.value[i]?.[0];
    if ((first?.start ?? props.lines[i].start) - LOOKAHEAD_MS - ENTER_MS > now) break;
    if (first && now < holdEnds.value[i] + EXIT_MS) out.push(i);
  }
  return out.slice(-2);
}

/**
 * What Vue renders: only which lines are on screen and the coming line's
 * text — they change a few times per line.  Everything that moves every
 * frame (position, size, colour, opacity) is written straight to the DOM by
 * the frame loop below, so the song clock never re-renders the component.
 */
const visible = shallowRef<number[]>([]);
const previewIdx = ref(-1);

// -- audio ---------------------------------------------------------------------
// non-reactive: the canvas loop writes, the scene reads it once per frame
const audio = { energy: 0, prevEnergy: 0, onset: 0, peak: 0.05, speed: 0 };
let freq: Uint8Array<ArrayBuffer> | null = null;

function sampleAudio() {
  const a = props.analyser;
  if (!a || props.playing === false) {
    audio.energy *= 0.95;
    audio.onset *= 0.84;
    return;
  }
  if (!freq || freq.length !== a.frequencyBinCount) freq = new Uint8Array(a.frequencyBinCount);
  const bins = freq;
  a.getByteFrequencyData(bins);
  const hzPerBin = a.context.sampleRate / a.fftSize;
  const band = (lo: number, hi: number) => {
    const from = Math.max(1, Math.floor(lo / hzPerBin));
    const to = Math.min(bins.length, Math.max(from + 1, Math.floor(hi / hzPerBin)));
    let s = 0;
    for (let i = from; i < to; i += 1) s += bins[i];
    return s / Math.max(1, to - from) / 255;
  };
  const raw =
    band(30, 150) * 0.3 +
    band(150, 500) * 0.18 +
    band(500, 2000) * 0.22 +
    band(2000, 5000) * 0.2 +
    band(5000, 12000) * 0.1;
  // follows the volume knob: normalise by a slowly falling peak
  audio.peak = Math.max(raw, audio.peak * 0.999, 0.05);
  audio.prevEnergy = audio.energy;
  audio.energy = Math.pow(raw / audio.peak, 0.8);
  audio.onset = Math.max(audio.onset * 0.84, clamp((audio.energy - audio.prevEnergy) * 5, 0, 1));
}

// -- scene (direct DOM writes) ----------------------------------------------------

/**
 * Last value written per element and property: the frame loop sets the
 * same values over and over (a passed word sits still), and skipping those
 * writes keeps the browser from re-checking styles it does not need to.
 */
const written = new WeakMap<HTMLElement, Record<string, string>>();
function put(el: HTMLElement, prop: string, value: string) {
  let seen = written.get(el);
  if (!seen) written.set(el, (seen = {}));
  if (seen[prop] === value) return;
  seen[prop] = value;
  el.style.setProperty(prop, value);
}

const floatEl = ref<HTMLElement | null>(null);
const titleEl = ref<HTMLElement | null>(null);
const previewEl = ref<HTMLElement | null>(null);

/** A glow made once per word colour (two soft shadows), switched on while the word is sung. */
function glowOf(color: string): string {
  return `0 0 0.28em ${mix(color, "transparent", 0.9)}, 0 0 0.56em ${mix(color, "transparent", 0.7)}`;
}

function paintLines(now: number) {
  const stage = stageEl.value;
  if (!stage) return;
  const W = stageW.value;
  const H = stageH.value;
  for (const lineEl of stage.querySelectorAll<HTMLElement>(".pv-line")) {
    const li = Number(lineEl.dataset.l);
    const lay = layouts.value[li];
    const us = units.value[li];
    if (!lay || !us?.length) continue;
    const isChorus = chorus.value.has(li);
    const enterAt = us[0].start - LOOKAHEAD_MS - ENTER_MS;
    const tin = ease((now - enterAt) / ENTER_MS);
    const out = easeInOut((now - holdEnds.value[li]) / EXIT_MS);
    // 進場從 90% 長到原大、離場再放大 10%；不用 blur（智慧電視的 GPU 每幀重做模糊會掉幀）
    const cScale = (0.9 + 0.1 * tin) * (1 + 0.1 * out);
    put(lineEl, "opacity", (tin * (1 - out)).toFixed(3));
    put(lineEl, "transform-origin", `${W / 2}px ${H / 2}px`);
    put(lineEl, "transform", `scale(${cScale.toFixed(4)})`);
    const unitEls = lineEl.querySelectorAll<HTMLElement>(".u");
    const ringEls = lineEl.querySelectorAll<HTMLElement>(".ring");
    us.forEach((u, ui) => {
      const el = unitEls[ui];
      const L = lay.units[ui];
      if (!el || !L) return;
      const ring = ringEls[ui];
      const t = now - (u.start - LOOKAHEAD_MS);
      if (t < 0) {
        put(el, "display", "none");
        if (ring) put(ring, "display", "none");
        return;
      }
      put(el, "display", "");
      const activeEnd = Math.max(u.end, u.start + 120);
      const dur = activeEnd - u.start;
      const passedT = now - activeEnd;
      const sp = spring(t);
      const p = passedT > 0 ? ease(passedT / 500) : 0;
      const pulse = passedT > 0 ? 0 : audio.onset * 0.06;
      const scale = lerp(lerp(0.5 * L.scale, L.scale * 1.25 * (1 + pulse), sp), L.scale, p);
      const x = L.x + L.wx * (1 - sp);
      const y = L.y + L.wy * (1 - sp);
      const rot = L.rot + 20 * (1 - sp) + (passedT > 0 ? L.drift * clamp(passedT / 5000, 0, 1) : 0);
      const opacity = clamp(t / 100, 0, 1) * lerp(1, 0.82, p);
      // colour: resting → singing over the word, back over 0.8 s after it
      const sing = clamp((now - u.start) / dur, 0, 1) * (1 - easeInOut(passedT / 800));
      put(el, "left", `${L.x.toFixed(1)}px`);
      put(el, "top", `${L.y.toFixed(1)}px`);
      put(
        el,
        "transform",
        `translate(-50%, -50%) translate(${(x - L.x).toFixed(1)}px, ${(y - L.y).toFixed(1)}px) rotate(${rot.toFixed(2)}deg) scale(${scale.toFixed(3)})`,
      );
      put(el, "opacity", opacity.toFixed(3));
      put(el, "color", mix(L.active, L.color, Math.round(sing * 20) / 20));
      // 唱到的字亮起光暈：固定的陰影、只在開始與結束時切換，不每幀重算（省電模式不開）
      const glowing = !props.lite && now >= u.start && passedT < 600;
      put(el, "text-shadow", glowing ? glowOf(L.active) : "none");
      if (ring) {
        const rt = (now - u.start) / 500;
        if (isChorus && !props.lite && rt >= 0 && rt < 1) {
          const size = lay.fontPx * 1.5;
          put(ring, "display", "");
          put(ring, "left", `${L.x}px`);
          put(ring, "top", `${L.y}px`);
          put(ring, "width", `${size}px`);
          put(ring, "height", `${size}px`);
          put(ring, "border-color", L.active);
          put(ring, "opacity", (0.8 * (1 - ease(rt))).toFixed(3));
          put(ring, "transform", `translate(-50%, -50%) scale(${lerp(0.2, L.ripple, ease(rt)).toFixed(3)})`);
        } else {
          put(ring, "display", "none");
        }
      }
    });
  }
}

/** Title card: before the first line and in long gaps between lines (0..1). */
function titleCardAt(now: number): number {
  const lines = props.lines;
  if (!lines.length) return 1;
  let i = -1;
  for (let j = 0; j < lines.length; j += 1) {
    if (lines[j].start - LOOKAHEAD_MS - ENTER_MS <= now) i = j;
    else break;
  }
  const next = lines[i + 1];
  if (!next) return i < 0 ? 1 : 0; // outro: let the last line linger
  if (i >= 0 && next.start - lines[i].end < INTERLUDE_MS) return 0;
  const fadeIn = i < 0 ? 1 : ease((now - holdEnds.value[i] - EXIT_MS) / 700);
  const fadeOut = ease((next.start - LOOKAHEAD_MS - ENTER_MS - 200 - now) / 500);
  return Math.min(fadeIn, fadeOut);
}

/**
 * Full screen has no karaoke strip, and words only appear as they are sung:
 * preview the coming line small at the bottom so people can sing along.
 */
const preview = computed(() => {
  const line = props.lines[previewIdx.value];
  if (!props.full || !line) return null;
  const text = lineText(line).trim();
  return text ? { text, color: partColor(line.part) } : null;
});

function previewAt(now: number): { idx: number; opacity: number } {
  if (!props.full) return { idx: -1, opacity: 0 };
  const idx = props.lines.findIndex((l) => l.start - LOOKAHEAD_MS > now);
  if (idx < 0) return { idx: -1, opacity: 0 };
  const until = props.lines[idx].start - now;
  if (until > 6000) return { idx: -1, opacity: 0 };
  return { idx, opacity: 0.55 * ease((6000 - until) / 600) };
}

/** Duet singer tag over the line's top-left. */
function tagOf(li: number): { text: string; style: Record<string, string> } | null {
  const line = props.lines[li];
  const lay = layouts.value[li];
  const singer = line?.singer?.trim();
  if (!singer || !lay) return null;
  const tagColor = partColor(line.part) ?? palette.value.accent;
  return {
    text: singer,
    style: {
      left: `${lay.left}px`,
      top: `${Math.max(0, lay.top - lay.fontPx * 0.5)}px`,
      color: tagColor,
      borderColor: tagColor,
    },
  };
}

function sameLines(a: number[], b: number[]): boolean {
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

/** One frame of the text side — a pure function of the song clock. */
function paintText() {
  const now = props.now();
  const vis = visibleAt(now);
  if (!sameLines(vis, visible.value)) visible.value = vis; // DOM catches up next frame
  paintLines(now);
  if (floatEl.value) {
    // the lyric block breathes slowly (on the song clock: still when paused)
    const t = (now / 7000) * Math.PI * 2;
    const k = stageH.value / 400;
    const y = Math.sin(t) * MOODS[mood.value].float * k;
    put(floatEl.value, "transform", `translateY(${y.toFixed(1)}px) scale(${(1 + Math.sin(t * 2) * 0.006).toFixed(4)})`);
  }
  if (titleEl.value) {
    const card = titleCardAt(now);
    put(titleEl.value, "display", card > 0 ? "" : "none");
    put(titleEl.value, "opacity", card.toFixed(3));
  }
  const pv = previewAt(now);
  previewIdx.value = pv.idx;
  if (previewEl.value) put(previewEl.value, "opacity", pv.opacity.toFixed(3));
}

// -- background: soft colour blobs flowing with the music -----------------------
const canvas = ref<HTMLCanvasElement | null>(null);
let raf = 0;
let last = 0;
let lastDraw = 0;
let phase = 0;
let lift = 0;
const reduceMotion =
  typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

/**
 * The flowing background is drawn tiny (96×54) and stretched over the screen
 * — the stretch alone smooths it; no CSS blur (a full-screen blur every
 * frame is what made weak TVs stutter).  ~30 fps is plenty for slow blobs;
 * 省電 draws it 8 times a second.
 */
function drawBackground(ts: number) {
  const el = canvas.value;
  const ctx = el?.getContext("2d");
  if (!el || !ctx) return;
  const dt = Math.min(0.1, (ts - (last || ts)) / 1000);
  last = ts;
  const w = 96;
  const h = 54;
  if (el.width !== w || el.height !== h) {
    el.width = w;
    el.height = h;
  }
  const target = 0.05 + audio.energy * 0.22 + audio.onset * 0.55;
  audio.speed += (target - audio.speed) * Math.min(1, dt * 4);
  if (!reduceMotion) phase += dt * audio.speed;
  const vis = visible.value;
  const live = vis[vis.length - 1];
  lift += ((live !== undefined && chorus.value.has(live) ? 1 : 0) - lift) * Math.min(1, dt * 1.5);
  if (ts - lastDraw < (props.lite ? 120 : 30)) return;
  lastDraw = ts;
  const pal = palette.value;
  ctx.globalAlpha = 1;
  ctx.fillStyle = pal.bg;
  ctx.fillRect(0, 0, w, h);
  const r = rng(songSeed.value);
  pal.blobs.forEach((color, i) => {
    const fx = 0.6 + r() * 0.8;
    const fy = 0.5 + r() * 0.7;
    const ox = r() * 6.28;
    const oy = r() * 6.28;
    const cx = (0.5 + 0.42 * Math.sin(phase * fx + ox)) * w;
    const cy = (0.5 + 0.4 * Math.cos(phase * fy + oy)) * h;
    const rad = (0.45 + 0.15 * Math.sin(phase * 0.7 + i)) * w * (1 + audio.energy * 0.12);
    const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, rad);
    g.addColorStop(0, color);
    g.addColorStop(1, "transparent");
    ctx.globalAlpha = clamp(0.62 + audio.energy * 0.25 + lift * 0.15, 0, 0.95);
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, w, h);
  });
  ctx.globalAlpha = 1;
}

function frame(ts: number) {
  raf = requestAnimationFrame(frame);
  sampleAudio();
  paintText();
  drawBackground(ts);
}

onMounted(() => {
  if (stageEl.value) {
    observer = new ResizeObserver(([entry]) => {
      stageW.value = entry.contentRect.width;
      stageH.value = entry.contentRect.height;
    });
    observer.observe(stageEl.value);
  }
  raf = requestAnimationFrame(frame);
});

onBeforeUnmount(() => {
  observer?.disconnect();
  cancelAnimationFrame(raf);
});
</script>

<template>
  <div class="textpv" :style="{ background: palette.bg }" aria-hidden="true">
    <canvas ref="canvas" class="flow" />
    <div class="vignette" />
    <div ref="stageEl" class="pv-stage" :class="{ full }">
      <div ref="floatEl" class="pv-float">
        <div
          v-for="li in visible"
          :key="li"
          class="pv-line"
          :data-l="li"
          :style="{ fontSize: `${layouts[li]?.fontPx ?? 48}px` }"
        >
          <div v-if="tagOf(li)" class="tag" :style="tagOf(li)!.style">{{ tagOf(li)!.text }}</div>
          <template v-if="chorus.has(li)">
            <span v-for="(u, ui) in units[li]" :key="'r' + ui" class="ring" style="display: none" />
          </template>
          <span v-for="(u, ui) in units[li]" :key="ui" class="u" style="display: none">{{ u.text }}</span>
        </div>
      </div>
      <div ref="titleEl" class="title-card">
        <div class="tc-title">{{ title }}</div>
        <div class="tc-artist">{{ artist }}</div>
      </div>
    </div>
    <div ref="previewEl" class="preview" :style="{ color: preview?.color ?? undefined }">
      {{ preview?.text ?? "" }}
    </div>
  </div>
</template>

<style scoped>
.textpv {
  position: absolute;
  inset: 0;
  overflow: hidden;
  pointer-events: none;
  user-select: none;
  font-family: "Noto Sans TC", "Microsoft JhengHei", "PingFang TC", "Heiti TC", "Source Han Sans TC",
    sans-serif;
}

.flow {
  position: absolute;
  inset: -4%;
  width: 108%;
  height: 108%;
  /* no blur: the 96×54 canvas stretched this far is soft already */
}

.vignette {
  position: absolute;
  inset: 0;
  background: radial-gradient(ellipse at center, transparent 45%, rgba(0, 0, 0, 0.5) 100%);
}

/* below the HUD, above the karaoke zone (or nearly the whole screen) */
.pv-stage {
  position: absolute;
  left: 5vw;
  right: 5vw;
  top: 14vh;
  height: 40vh;
}

.pv-stage.full {
  height: 64vh;
}

.pv-float,
.pv-line {
  position: absolute;
  inset: 0;
}

/*
 * opacity starts at 0 here, not in a :style binding — Vue re-applies a bound
 * style on every re-render and would wipe what the frame loop wrote.
 */
.pv-line {
  opacity: 0;
  font-weight: 500;
  line-height: 1.22;
}

.u {
  position: absolute;
  white-space: pre;
  will-change: transform, opacity;
}

.ring {
  position: absolute;
  border: 2px solid;
  border-radius: 50%;
}

.tag {
  position: absolute;
  font-size: 0.26em;
  font-weight: 700;
  letter-spacing: 0.08em;
  padding: 0.1em 0.7em;
  border: 2px solid;
  border-radius: 999px;
  background: rgba(0, 0, 0, 0.35);
  white-space: nowrap;
}

.title-card {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  gap: 0.7em;
}

.tc-title {
  font-weight: 300;
  font-size: clamp(36px, 5.4vw, 104px);
  letter-spacing: 0.06em;
  line-height: 1.15;
  max-width: 100%;
  overflow-wrap: anywhere;
  color: rgba(244, 241, 251, 0.95);
  text-shadow: 0 0 0.4em rgba(0, 0, 0, 0.45);
}

.tc-artist {
  font-size: clamp(16px, 1.7vw, 30px);
  letter-spacing: 0.4em;
  color: rgba(238, 237, 247, 0.6);
}

.preview {
  opacity: 0;
  position: absolute;
  left: 6vw;
  right: 6vw;
  bottom: 9vh;
  text-align: center;
  font-size: clamp(18px, 2.4vw, 44px);
  letter-spacing: 0.06em;
  color: rgba(238, 237, 247, 0.9);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
