/**
 * 播放進度的本地插值：display 每約 1 秒回報一次 playback.state，
 * 手機端用「基準值 + (now - updated_at)」補間，進度條才不會一秒跳一格。
 */
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { store } from "./useKtv";

export function useProgress() {
  const now = ref(Date.now());
  let timer: number | undefined;

  onMounted(() => {
    timer = window.setInterval(() => {
      now.value = Date.now();
    }, 500);
  });
  onBeforeUnmount(() => window.clearInterval(timer));

  const durationMs = computed(
    () => store.session.playback.duration_ms || store.session.current?.track.duration_ms || 0,
  );

  const positionMs = computed(() => {
    const pb = store.session.playback;
    let base = pb.position_ms || 0;
    if (pb.playing && pb.updated_at) {
      base += now.value - pb.updated_at * 1000;
    }
    return Math.max(0, Math.min(base, durationMs.value || base));
  });

  const percent = computed(() =>
    durationMs.value ? Math.min(100, (positionMs.value / durationMs.value) * 100) : 0,
  );

  return { positionMs, durationMs, percent, playing: computed(() => store.session.playback.playing), fmt };
}

/** ms → m:ss */
export function fmt(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}
