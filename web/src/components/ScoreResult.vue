<script setup lang="ts">
/**
 * 唱完的結算畫面：總分從 0 數上去，下面是音準／節奏／穩定三條和每句判定的次數。
 * 顯示幾秒後由 DisplayView 收掉（下一首的開場卡會蓋過來也沒關係）。
 */
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import type { SongResult } from "../lib/scoring";
import type { SongScore } from "../types";

const props = defineProps<{
  result: SongResult;
  title: string;
  singer: string;
  /** 這次之前的本曲最高分：undefined = 還沒問到，null = 第一次有人唱 */
  best?: SongScore | null;
}>();

const shown = ref(0);
let raf = 0;
onMounted(() => {
  const started = performance.now();
  const step = (now: number) => {
    const k = Math.min(1, (now - started) / 1400);
    shown.value = Math.round(props.result.score * (1 - Math.pow(1 - k, 3)));
    if (k < 1) raf = requestAnimationFrame(step);
  };
  raf = requestAnimationFrame(step);
});
onBeforeUnmount(() => cancelAnimationFrame(raf));

const rank = computed(() => {
  const s = props.result.score;
  return s >= 95 ? "S" : s >= 85 ? "A" : s >= 70 ? "B" : s >= 50 ? "C" : "D";
});
const parts = computed(() => [
  { label: "音準", value: props.result.pitch },
  { label: "節奏", value: props.result.rhythm },
  { label: "穩定", value: props.result.stability },
]);
const record = computed(() => props.best === null || (!!props.best && props.result.score > props.best.score));
const counts = computed(() =>
  (["PERFECT", "GREAT", "GOOD", "MISS"] as const).map((k) => ({ k, n: props.result.counts[k] })),
);
</script>

<template>
  <div class="result">
    <div class="card cut">
      <span class="tag cut">SCORE</span>
      <div class="song ellipsis">{{ title }}</div>
      <div v-if="singer" class="singer ellipsis">♪ {{ singer }}</div>
      <div class="big">
        <span class="num">{{ shown }}</span>
        <span class="rank" :class="'r-' + rank">{{ rank }}</span>
      </div>
      <div v-if="best !== undefined" class="best" :class="{ record }">
        <template v-if="record">本曲新紀錄！</template>
        <template v-else-if="best">本曲最高 {{ best.score }} 分 · {{ best.singer }}</template>
      </div>
      <div class="parts">
        <div v-for="p in parts" :key="p.label" class="part">
          <span class="label">{{ p.label }}</span>
          <span class="meter"><i :style="{ width: Math.round(p.value * 100) + '%' }" /></span>
          <span class="pct">{{ Math.round(p.value * 100) }}</span>
        </div>
      </div>
      <div class="counts">
        <span v-for="c in counts" :key="c.k" :class="'j-' + c.k.toLowerCase()">
          <b>{{ c.n }}</b>{{ c.k }}
        </span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.cut {
  clip-path: polygon(var(--cut) 0, 100% 0, 100% calc(100% - var(--cut)), calc(100% - var(--cut)) 100%, 0 100%, 0 var(--cut));
}
.ellipsis {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tag {
  --cut: 5px;
  display: inline-block;
  padding: 5px 10px 4px;
  font: 700 clamp(12px, 0.9vw, 17px) / 1 var(--hud);
  letter-spacing: 0.18em;
  color: var(--void);
  background: var(--volt);
}
.result {
  position: absolute;
  inset: 0;
  z-index: 5;
  display: grid;
  place-items: center;
  background: rgba(7, 6, 13, 0.82);
  pointer-events: none;
}
.card {
  --cut: 18px;
  width: min(720px, 86vw);
  padding: clamp(24px, 3vw, 44px);
  text-align: center;
  background: #0d0b17;
  box-shadow: inset 0 0 0 2px rgba(25, 240, 255, 0.55);
}
.song {
  margin-top: 14px;
  font-size: clamp(20px, 1.8vw, 34px);
  font-weight: 700;
}
.singer {
  margin-top: 4px;
  color: var(--cyan);
  font-size: clamp(16px, 1.2vw, 24px);
}
.big {
  display: flex;
  align-items: baseline;
  justify-content: center;
  gap: 0.25em;
  margin: 8px 0 18px;
  font-family: var(--hud);
}
.num {
  font-size: clamp(96px, 11vw, 200px);
  font-weight: 700;
  line-height: 1;
  color: var(--volt);
  text-shadow: 0 0 30px rgba(242, 255, 73, 0.45);
  font-variant-numeric: tabular-nums;
}
.rank {
  font-size: clamp(40px, 4vw, 76px);
  font-weight: 700;
  color: var(--magenta);
}
.rank.r-S {
  color: var(--volt);
}
.rank.r-A {
  color: var(--cyan);
}
.best {
  margin: -6px 0 18px;
  font-size: clamp(16px, 1.2vw, 22px);
  color: var(--dim);
}
.best.record {
  font-weight: 700;
  color: var(--magenta);
  text-shadow: 0 0 14px var(--magenta);
}
.parts {
  display: grid;
  gap: 10px;
  margin: 0 auto;
  max-width: 520px;
}
.part {
  display: grid;
  grid-template-columns: 3em 1fr 2.5em;
  align-items: center;
  gap: 12px;
  font-size: clamp(16px, 1.2vw, 22px);
}
.label {
  color: var(--dim);
  text-align: left;
}
.meter {
  height: 10px;
  background: rgba(255, 255, 255, 0.1);
}
.meter i {
  display: block;
  height: 100%;
  background: linear-gradient(90deg, var(--cyan), var(--volt));
  transition: width 1.2s ease-out;
}
.pct {
  font-family: var(--hud);
  font-weight: 700;
  text-align: right;
}
.counts {
  display: flex;
  justify-content: center;
  gap: clamp(14px, 2vw, 32px);
  margin-top: 20px;
  font: 700 clamp(13px, 0.9vw, 17px) / 1 var(--hud);
  letter-spacing: 0.12em;
  color: var(--dim);
}
.counts b {
  display: block;
  margin-bottom: 6px;
  font-size: clamp(22px, 1.8vw, 34px);
  color: var(--fg);
}
.j-perfect b {
  color: var(--volt);
}
.j-great b {
  color: var(--cyan);
}
.j-miss b {
  color: var(--danger);
}
</style>
