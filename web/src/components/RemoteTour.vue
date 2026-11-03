<script setup lang="ts">
/**
 * First-visit walkthrough for /remote: dims the page, cuts a hole around one
 * `[data-tour="<key>"]` element at a time and explains it in a card beside it.
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

export interface TourStep {
  target: string;
  title: string;
  body: string;
}

const props = defineProps<{ steps: TourStep[] }>();
const emit = defineEmits<{ done: [] }>();

const index = ref(0);
const rect = ref<DOMRect | null>(null);
const step = computed(() => props.steps[index.value]);
const last = computed(() => index.value === props.steps.length - 1);

function measure() {
  const el = document.querySelector(`[data-tour="${step.value.target}"]`);
  rect.value = el ? el.getBoundingClientRect() : null;
}

const PAD = 6;
const hole = computed(() => {
  const r = rect.value;
  if (!r) return null;
  return {
    left: `${r.left - PAD}px`,
    top: `${r.top - PAD}px`,
    width: `${r.width + PAD * 2}px`,
    height: `${r.height + PAD * 2}px`,
  };
});

// the card goes on whichever side of the target has more room
const cardStyle = computed(() => {
  const r = rect.value;
  if (!r) return { top: "50%", transform: "translateY(-50%)" };
  const below = r.top + r.height / 2 < window.innerHeight / 2;
  return below
    ? { top: `${r.bottom + PAD + 12}px` }
    : { bottom: `${window.innerHeight - r.top + PAD + 12}px` };
});

function next() {
  if (last.value) emit("done");
  else index.value += 1;
}

watch(index, () => nextTick(measure));
onMounted(() => {
  void nextTick(measure);
  window.addEventListener("resize", measure);
});
onBeforeUnmount(() => window.removeEventListener("resize", measure));
</script>

<template>
  <div class="tour" role="dialog" aria-modal="true" :aria-label="`使用教學 ${index + 1}/${steps.length}`">
    <div v-if="hole" class="hole" :style="hole" />
    <div v-else class="dim" />
    <div class="card" :style="cardStyle">
      <div class="count">{{ index + 1 }} / {{ steps.length }}</div>
      <strong>{{ step.title }}</strong>
      <p>{{ step.body }}</p>
      <div class="actions">
        <button v-if="!last" class="skip" @click="emit('done')">略過教學</button>
        <span class="grow" />
        <button v-if="index > 0" class="prev" @click="index -= 1">上一步</button>
        <button class="next" @click="next">{{ last ? "開始點歌" : "下一步" }}</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tour {
  position: fixed;
  inset: 0;
  z-index: 20;
}
.hole,
.dim {
  position: absolute;
  transition: all 0.25s cubic-bezier(0.2, 0.8, 0.2, 1);
}
.hole {
  border: 2px solid var(--volt, #f2ff49);
  box-shadow:
    0 0 14px rgba(242, 255, 73, 0.6),
    0 0 0 9999px rgba(4, 3, 10, 0.78);
  pointer-events: none;
}
.dim {
  inset: 0;
  background: rgba(4, 3, 10, 0.78);
}
.card {
  position: absolute;
  left: max(16px, env(safe-area-inset-left, 0px));
  right: max(16px, env(safe-area-inset-right, 0px));
  max-width: 420px;
  margin: 0 auto;
  padding: 14px 16px 12px;
  background: var(--panel-2, #191629);
  border: 1px solid var(--cyan, #19f0ff);
  clip-path: polygon(10px 0, 100% 0, 100% calc(100% - 10px), calc(100% - 10px) 100%, 0 100%, 0 10px);
}
.count {
  font: 600 11px/1.4 var(--hud);
  letter-spacing: 0.16em;
  color: var(--cyan, #19f0ff);
}
strong {
  display: block;
  margin-top: 2px;
  font-size: 17px;
}
p {
  margin: 6px 0 12px;
  color: var(--fg, #eeedf7);
  line-height: 1.55;
}
.actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.grow {
  flex: 1;
}
.tour button {
  all: unset;
  box-sizing: border-box;
  cursor: pointer;
  padding: 8px 12px;
  font-size: 14px;
  font-weight: 600;
}
.tour button:focus-visible {
  outline: 2px solid var(--volt, #f2ff49);
}
.skip {
  padding-left: 0 !important;
  color: var(--dim, #918cab);
}
.prev {
  color: var(--cyan, #19f0ff);
}
.next {
  color: var(--void, #07060d);
  background: var(--cyan, #19f0ff);
}
</style>
