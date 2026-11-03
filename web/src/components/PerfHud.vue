<script setup lang="ts">
/**
 * /display?perf=1 的診斷角落：瀏覽器版本、功能支援、字型、FPS。
 * 每秒更新一次文字，本身幾乎不花效能。
 */
import { onBeforeUnmount, onMounted, ref } from "vue";
import { PerfMeter, type Compat, type PerfStats } from "../lib/compat";

const props = defineProps<{ compat: Compat; lite: boolean }>();

const stats = ref<PerfStats | null>(null);
const fonts = ref("");
const meter = new PerfMeter();
let timer = 0;

/** 每個字型家族：載好幾個檔（切片字型會有很多個）、失敗幾個 */
function fontSummary(): string {
  const set = document.fonts as FontFaceSet & Iterable<FontFace>;
  if (!set || typeof (set as any)[Symbol.iterator] !== "function") return "不支援 FontFaceSet";
  const by = new Map<string, { ok: number; err: number; all: number }>();
  for (const face of set) {
    const name = face.family.replace(/["']/g, "");
    const row = by.get(name) ?? { ok: 0, err: 0, all: 0 };
    row.all += 1;
    if (face.status === "loaded") row.ok += 1;
    if (face.status === "error") row.err += 1;
    by.set(name, row);
  }
  return [...by]
    .filter(([, r]) => r.ok || r.err)
    .map(([name, r]) => `${name} ${r.ok}/${r.all}${r.err ? ` ✗${r.err}` : ""}`)
    .join("　") || "（尚未載入）";
}

onMounted(() => {
  meter.start();
  const tick = () => {
    stats.value = meter.stats();
    fonts.value = fontSummary();
  };
  tick();
  timer = window.setInterval(tick, 1000);
});
onBeforeUnmount(() => {
  meter.stop();
  window.clearInterval(timer);
});

const missing = Object.entries(props.compat.features)
  .filter(([, ok]) => !ok)
  .map(([name]) => name);
</script>

<template>
  <div class="perf-hud">
    <div>
      <b>{{ compat.chrome ? `Chrome ${compat.chrome}` : "非 Chromium" }}</b>
      <span v-if="compat.webview"> · WebView/TV</span> · {{ compat.screen }}
    </div>
    <div :class="{ bad: !compat.gpu || /swiftshader/i.test(compat.gpu) }">GPU：{{ compat.gpu ?? "無 WebGL" }}</div>
    <div v-if="stats">
      FPS {{ stats.fps }} · 慢幀 {{ Math.round(stats.slow * 100) }}%
      · 長任務 {{ stats.longTasks }}<span v-if="stats.heapMb !== null"> · heap {{ stats.heapMb }}MB</span>
      · 效果 {{ lite ? "省電" : "完整" }}
    </div>
    <div :class="{ bad: missing.length }">不支援：{{ missing.length ? missing.join(", ") : "無" }}</div>
    <div>字型：{{ fonts }}</div>
    <div class="ua">{{ compat.ua }}</div>
  </div>
</template>

<style scoped>
.perf-hud {
  position: absolute;
  left: 50%;
  top: 8px;
  transform: translateX(-50%);
  z-index: 50;
  max-width: min(720px, 60vw);
  padding: 8px 10px;
  font: 13px/1.45 ui-monospace, Consolas, monospace;
  color: #d8ffe0;
  background: rgba(0, 0, 0, 0.78);
  border: 1px solid rgba(120, 255, 160, 0.4);
  pointer-events: none;
  white-space: normal;
  word-break: break-all;
}
.bad {
  color: #ffb3b3;
}
.ua {
  opacity: 0.7;
  font-size: 11px;
}
</style>
