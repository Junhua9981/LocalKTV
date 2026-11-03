<script setup lang="ts">
/**
 * 節拍燈光 — KTV 包廂的霓虹燈：畫面四邊的光暈跟著大鼓閃，角落的光團跟著
 * 整首歌的音量起伏（副歌比主歌亮），每隔幾拍換一次顏色。
 *
 * 只讀 engine.analyser（不碰聲音），每幀把數值寫進 CSS 變數，所以 Vue 不會
 * 每幀重繪；整層 pointer-events: none，疊在 MV／文字PV 之上、歌詞之下。
 */
import { onBeforeUnmount, onMounted, ref, watch } from "vue";

const props = defineProps<{
  analyser: AnalyserNode | null;
  playing: boolean;
  /** 省電畫面：不畫角落的大光團、不用 mix-blend-mode，只留四邊的霓虹框 */
  lite?: boolean;
}>();

const el = ref<HTMLDivElement | null>(null);

/** 兩次大鼓之間至少隔這麼久（毫秒），避免一拍閃兩下。 */
const MIN_BEAT_GAP_MS = 220;
/** 每幾拍換一次燈色。 */
const BEATS_PER_COLOUR = 8;
const PALETTE: [string, string][] = [
  ["255, 47, 180", "25, 240, 255"], // 洋紅 / 青
  ["178, 61, 255", "242, 255, 73"], // 紫 / 螢光黃
  ["61, 123, 255", "255, 47, 180"], // 藍 / 洋紅
  ["25, 240, 255", "178, 61, 255"], // 青 / 紫
];

const reduceMotion =
  typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

// non-reactive: written every frame
const s = {
  bassAvg: 0,
  bassVar: 0,
  lastBeat: 0,
  beats: 0,
  kick: 0,
  short: 0, // 最近 ~1 秒的音量
  long: 0, // 最近 ~20 秒的音量：拿來判斷現在是不是副歌
  peak: 0.05,
  level: 0,
  n: 0, // frames since the song started: the long average warms up faster
};
let bins: Uint8Array<ArrayBuffer> | null = null;
let raf = 0;
let colour = -1;

function band(a: AnalyserNode, data: Uint8Array, lo: number, hi: number): number {
  const hzPerBin = a.context.sampleRate / a.fftSize;
  const from = Math.max(1, Math.floor(lo / hzPerBin));
  const to = Math.min(data.length, Math.max(from + 1, Math.floor(hi / hzPerBin)));
  let sum = 0;
  for (let i = from; i < to; i += 1) sum += data[i];
  return sum / Math.max(1, to - from) / 255;
}

function frame(now: number) {
  raf = requestAnimationFrame(frame);
  const a = props.analyser;
  if (a && props.playing) {
    if (!bins || bins.length !== a.frequencyBinCount) bins = new Uint8Array(a.frequencyBinCount);
    a.getByteFrequencyData(bins);
    const bass = band(a, bins, 35, 140);
    const body = band(a, bins, 140, 4000);

    // onset: bass well above its running mean (adaptive to each song's mix)
    const std = Math.sqrt(s.bassVar);
    if (
      bass > 0.18 &&
      bass > s.bassAvg + Math.max(0.04, std * 1.4) &&
      now - s.lastBeat > MIN_BEAT_GAP_MS
    ) {
      s.lastBeat = now;
      s.beats += 1;
      s.kick = 1;
    }
    const d = bass - s.bassAvg;
    s.bassAvg += d * 0.06;
    s.bassVar = s.bassVar * 0.94 + d * d * 0.06;

    const loud = bass * 0.45 + body * 0.55;
    s.peak = Math.max(loud, s.peak * 0.9995, 0.05);
    s.short += (loud - s.short) * 0.05;
    s.n += 1;
    s.long += (loud - s.long) * Math.max(0.0015, 1 / s.n);
    // 0 = quieter than usual for this song, 1 = clearly louder (chorus)
    const lift = s.long > 0.01 ? (s.short / s.long - 0.85) / 0.4 : 0;
    s.level = Math.max(0, Math.min(1, 0.35 * (s.short / s.peak) + 0.65 * lift));
  } else {
    s.level *= 0.96;
  }
  s.kick *= 0.9;
  // 暫停後燈光淡到看不見就不再寫 CSS 變數（播放時 frame 繼續算）
  if (!props.playing && s.level < 0.002 && s.kick < 0.002) return;

  const node = el.value;
  if (!node) return;
  const kick = reduceMotion ? s.kick * 0.3 : s.kick;
  node.style.setProperty("--kick", kick.toFixed(3));
  node.style.setProperty("--level", s.level.toFixed(3));
  const next = Math.floor(s.beats / BEATS_PER_COLOUR) % PALETTE.length;
  if (next !== colour) {
    colour = next;
    node.style.setProperty("--c1", PALETTE[next][0]);
    node.style.setProperty("--c2", PALETTE[next][1]);
  }
}

watch(
  () => props.analyser,
  () => {
    s.bassAvg = s.bassVar = s.short = s.long = s.n = 0;
    s.peak = 0.05;
  },
);

onMounted(() => {
  raf = requestAnimationFrame(frame);
});
onBeforeUnmount(() => cancelAnimationFrame(raf));
</script>

<template>
  <div ref="el" class="beat-lights" :class="{ lite }" aria-hidden="true">
    <template v-if="!lite">
      <div class="bloom a" />
      <div class="bloom b" />
    </template>
    <div class="edge glow" />
    <div class="edge kick" />
  </div>
</template>

<style scoped>
.beat-lights {
  --kick: 0;
  --level: 0;
  --c1: 255, 47, 180;
  --c2: 25, 240, 255;
  position: absolute;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  overflow: hidden;
  mix-blend-mode: screen;
}
/* 全螢幕的 screen 混色要和底下會動的 MV 每幀重新混合；省電改成一般疊加 */
.beat-lights.lite {
  mix-blend-mode: normal;
}

/* 角落的光團：跟著整體音量（副歌更亮），大鼓時稍微脹大 */
.bloom {
  position: absolute;
  width: 70vmax;
  height: 70vmax;
  border-radius: 50%;
  opacity: calc(0.1 + var(--level) * 0.32 + var(--kick) * 0.16);
  transform: scale(calc(0.9 + var(--kick) * 0.12));
  will-change: opacity, transform;
}
.bloom.a {
  left: -35vmax;
  bottom: -38vmax;
  background: radial-gradient(closest-side, rgba(var(--c1), 0.9), transparent);
}
.bloom.b {
  right: -35vmax;
  top: -40vmax;
  background: radial-gradient(closest-side, rgba(var(--c2), 0.8), transparent);
}

/* 四邊的霓虹框：一層跟著大鼓閃、一層跟著音量；只動 opacity，筆電也跑得順 */
.edge {
  position: absolute;
  inset: 0;
  will-change: opacity;
}
.edge.kick {
  box-shadow: inset 0 0 8vmin rgba(var(--c1), 0.9);
  opacity: calc(0.15 + var(--kick) * 0.85);
}
.edge.glow {
  box-shadow: inset 0 0 18vmin rgba(var(--c2), 0.35);
  opacity: calc(0.15 + var(--level) * 0.85);
}
</style>
