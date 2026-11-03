<script setup lang="ts">
/**
 * 評分的音高條：標準音（AI 人聲軌抽出來的音符）是往左捲的橫條，
 * 麥克風唱的音高是一條軌跡，唱準是青色、差一點是黃色、走音是暗紅。
 *
 * 不分八度：唱的音高會摺到離標準音最近的那個八度再畫，男生唱女生的歌也貼得上。
 * 合唱的字有兩個聲部（兩條橫條），目前唱得比較貼近的那條照常亮，另一條畫淡。
 * 畫在 canvas 上，每個畫面更新一次，不經過 Vue 的響應式（每秒 100 筆）。
 */
import { onBeforeUnmount, onMounted, ref } from "vue";
import type { MicScorer } from "../audio/score";
import { centsOff } from "../lib/scoring";

const props = defineProps<{
  scorer: MicScorer;
  /** 現在唱到的歌曲時間（ms）：播放位置扣掉升降 Key 與播放延遲 */
  nowMs: () => number;
  /** 省電畫面：每秒畫 30 次、亮點不加模糊光暈 */
  lite?: boolean;
}>();

const canvas = ref<HTMLCanvasElement | null>(null);
/** 畫面上看得到過去多久、未來多久 */
const PAST_MS = 2500;
const FUTURE_MS = 3500;
let raf = 0;
let range: { lo: number; hi: number; doc: unknown } | null = null;

/** 整首歌的音域（去掉頭尾 5％的極端值），上下各留一點 */
function songRange(): { lo: number; hi: number } {
  const song = props.scorer.scorer;
  if (!song) return { lo: 5400, hi: 7200 };
  if (range && range.doc === song) return range;
  const pitches = song.notes.map((n) => n.cents).sort((a, b) => a - b);
  const pick = (q: number) => pitches[Math.min(pitches.length - 1, Math.floor(q * pitches.length))] ?? 6000;
  let lo = pick(0.05) - 250;
  let hi = pick(0.95) + 250;
  if (hi - lo < 1400) {
    const mid = (hi + lo) / 2;
    lo = mid - 700;
    hi = mid + 700;
  }
  range = { lo, hi, doc: song };
  return range;
}

/** 圓角長條。canvas 的 roundRect() 要 Chrome 99，舊的電視 WebView 沒有就自己畫 */
function pill(g: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  if (typeof g.roundRect === "function") {
    g.roundRect(x, y, w, h, r);
    return;
  }
  const rr = Math.min(r, w / 2, h / 2);
  g.moveTo(x + rr, y);
  g.arcTo(x + w, y, x + w, y + h, rr);
  g.arcTo(x + w, y + h, x, y + h, rr);
  g.arcTo(x, y + h, x, y, rr);
  g.arcTo(x, y, x + w, y, rr);
  g.closePath();
}

let lastDraw = 0;
/** canvas 的 CSS 大小：ResizeObserver 給，不在每幀讀 clientWidth（會逼瀏覽器先排版） */
const size = { w: 0, h: 0 };
let observer: ResizeObserver | null = null;

function draw(ts: number) {
  raf = requestAnimationFrame(draw);
  if (props.lite && ts - lastDraw < 30) return;
  lastDraw = ts;
  const el = canvas.value;
  const song = props.scorer.scorer;
  if (!el || !song) return;
  const dpr = window.devicePixelRatio || 1;
  const w = size.w;
  const h = size.h;
  if (!w || !h) return;
  if (el.width !== Math.round(w * dpr) || el.height !== Math.round(h * dpr)) {
    el.width = Math.round(w * dpr);
    el.height = Math.round(h * dpr);
  }
  const g = el.getContext("2d");
  if (!g) return;
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);

  const now = props.nowMs();
  // 升降 Key 時標準音整條一起移，音域也跟著移
  const lo = songRange().lo + song.keyCents;
  const hi = songRange().hi + song.keyCents;
  const x = (t: number) => ((t - now + PAST_MS) / (PAST_MS + FUTURE_MS)) * w;
  const pad = 8;
  const y = (cents: number) => {
    // 超出音域的音摺回來畫（不分八度）
    let c = cents;
    while (c > hi) c -= 1200;
    while (c < lo) c += 1200;
    return pad + (1 - (c - lo) / (hi - lo)) * (h - pad * 2);
  };
  const barH = Math.max(6, Math.min(14, h / 9));

  // 現在的位置
  const playX = x(now);
  g.fillStyle = "rgba(242, 255, 73, 0.55)";
  g.fillRect(playX - 1, 0, 2, h);

  // 標準音
  for (const n of song.notesBetween(now - PAST_MS, now + FUTURE_MS)) {
    const x0 = x(n.start);
    const x1 = x(n.end);
    const yy = y(n.cents) - barH / 2;
    const active = n.start <= now && now < n.end;
    g.fillStyle = !n.lead ? "rgba(25, 240, 255, 0.08)" : active ? "rgba(25, 240, 255, 0.55)" : "rgba(25, 240, 255, 0.22)";
    g.strokeStyle = n.lead ? "rgba(25, 240, 255, 0.85)" : "rgba(25, 240, 255, 0.35)";
    g.lineWidth = 1.5;
    g.beginPath();
    pill(g, x0, yy, Math.max(2, x1 - x0 - 2), barH, barH / 2);
    g.fill();
    g.stroke();
  }

  // 唱的音高：摺到離標準音最近的八度
  const trail = props.scorer.trail;
  let prev: { px: number; py: number; t: number } | null = null;
  g.lineCap = "round";
  g.lineWidth = Math.max(3, barH * 0.45);
  for (const p of trail) {
    if (p.t < now - PAST_MS) continue;
    const ref = song.noteAt(p.t, p.cents)?.cents ?? (lo + hi) / 2;
    const folded = ref + centsOff(p.cents, ref);
    const px = x(p.t);
    const py = y(folded);
    g.strokeStyle =
      p.hit === null ? "rgba(183, 178, 207, 0.6)"
      : p.hit >= 0.8 ? "#19f0ff"
      : p.hit >= 0.4 ? "#f2ff49"
      : "rgba(255, 77, 106, 0.8)";
    if (prev && p.t - prev.t < 40 && Math.abs(py - prev.py) < h / 3) {
      g.beginPath();
      g.moveTo(prev.px, prev.py);
      g.lineTo(px, py);
      g.stroke();
    }
    prev = { px, py, t: p.t };
  }
  // 最新一筆：亮點
  const last = trail[trail.length - 1];
  if (last && now - last.t < 300 && prev) {
    g.fillStyle = "#fff";
    g.shadowColor = last.hit !== null && last.hit >= 0.8 ? "#19f0ff" : "#f2ff49";
    g.shadowBlur = props.lite ? 0 : 14;
    g.beginPath();
    g.arc(prev.px, prev.py, barH * 0.55, 0, Math.PI * 2);
    g.fill();
    g.shadowBlur = 0;
  }
}

onMounted(() => {
  const el = canvas.value;
  if (el) {
    size.w = el.clientWidth;
    size.h = el.clientHeight;
    observer = new ResizeObserver(([entry]) => {
      size.w = entry.contentRect.width;
      size.h = entry.contentRect.height;
    });
    observer.observe(el);
  }
  raf = requestAnimationFrame(draw);
});
onBeforeUnmount(() => {
  cancelAnimationFrame(raf);
  observer?.disconnect();
});
</script>

<template>
  <div class="pitchbar cut">
    <canvas ref="canvas" />
  </div>
</template>

<style scoped>
.pitchbar {
  --cut: 10px;
  position: relative;
  height: clamp(70px, 12vh, 140px);
  margin: 0 var(--edge, 32px) 10px;
  background: rgba(7, 6, 13, 0.55);
  box-shadow: inset 0 0 0 1px rgba(25, 240, 255, 0.25);
  /* 沒有 backdrop-filter：底下是會動的 MV，背景模糊每幀都得重做，55% 的底色已夠 */
}
canvas {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
}
</style>
