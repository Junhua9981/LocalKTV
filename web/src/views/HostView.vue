<script setup lang="ts">
/**
 * /host — host console, same cyberpunk look as /remote.  Never plays audio.
 *
 * The transport deck (title, seek, 播放／暫停, 卡歌) stays pinned on top; below
 * it three tabs: 清單 (full queue control), 這首 (唱法、導唱、歌詞偏移) and
 * 包廂 (音量、升降 Key、大螢幕畫面、主要音源).  On a tablet or laptop
 * (≥ 900px) the tabs give way to two columns: controls left, queue right.
 */
import { computed, onMounted, ref, watch } from "vue";
import {
  connect,
  hideImg,
  trackCover,
  on,
  playback,
  queueBump,
  queueMove,
  queueRemove,
  setAudioSource,
  setGuide,
  setLyricsOffset,
  setMode,
  setPitch,
  setVisual,
  setVolume,
  store,
} from "../composables/useKtv";
import { fmt, useProgress } from "../composables/useProgress";
import { url } from "../base";
import type { Ambience, BgMode, ItemStatus, KaraokeMode, QueueItem } from "../types";

const view = ref<"queue" | "song" | "room">("queue");

// -- transport ----------------------------------------------------------------
const current = computed(() => store.session.current);
const queue = computed(() => store.session.queue);
const progress = useProgress();
const playing = computed(() => store.session.playback.playing);
const seekMs = ref(0);
const seeking = ref(false);

const shownMs = computed(() => (seeking.value ? seekMs.value : progress.positionMs.value));
const shownPct = computed(() =>
  progress.durationMs.value ? Math.min(100, (shownMs.value / progress.durationMs.value) * 100) : 0,
);

function onSeekInput(event: Event) {
  seeking.value = true;
  seekMs.value = Number((event.target as HTMLInputElement).value);
}

function onSeekChange() {
  seeking.value = false;
  playback("seek", seekMs.value);
}

function togglePlay() {
  playback(playing.value ? "pause" : "play");
}

// -- this song ----------------------------------------------------------------
const MODES: { value: KaraokeMode; text: string }[] = [
  { value: "ORIGINAL", text: "原唱" },
  { value: "INSTANT", text: "即時去人聲" },
  { value: "AI", text: "AI 去人聲" },
];
const mode = ref<KaraokeMode>("ORIGINAL");
const guidePct = ref(0);
const offsetMs = ref(0);

const modeHint = computed(() => {
  const item = current.value;
  if (!item) return "";
  if (item.ai_status === "FAILED" || item.karaoke_mode === "INSTANT") {
    return "AI 還沒好時會先用即時去人聲。";
  }
  return `AI 狀態：${AI_TEXT[item.ai_status] ?? item.ai_status}`;
});
const AI_TEXT: Record<string, string> = {
  NONE: "未啟動",
  QUEUED: "排隊中",
  PROCESSING: "處理中",
  READY: "已完成",
  FAILED: "失敗",
};

function changeMode(next: KaraokeMode) {
  mode.value = next;
  setMode(next);
}

function onGuideInput(event: Event) {
  guidePct.value = Number((event.target as HTMLInputElement).value);
  setGuide(guidePct.value / 100);
}

function adjustOffset(delta: number) {
  if (!current.value) return;
  offsetMs.value = delta === 0 ? 0 : offsetMs.value + delta;
  setLyricsOffset(current.value.track.id, offsetMs.value);
}

const offsetText = computed(() => {
  const s = offsetMs.value / 1000;
  return `${s > 0 ? "+" : ""}${s.toFixed(1)}s`;
});

// -- room ---------------------------------------------------------------------
// volume is session-wide but not broadcast back: remember the last value locally
const volumePct = ref(Number(localStorage.getItem("ktv-volume") ?? 90));
function onVolumeInput(event: Event) {
  volumePct.value = Number((event.target as HTMLInputElement).value);
  localStorage.setItem("ktv-volume", String(volumePct.value));
  setVolume(volumePct.value / 100);
}

const pitch = ref(0);
const pitchCapable = computed(() => store.session.pitch_capable);
const PIPS = [-3, -2, -1, 0, 1, 2, 3];

function stepPitch(delta: number) {
  const next = Math.max(-3, Math.min(3, pitch.value + delta));
  if (next === pitch.value) return;
  pitch.value = next;
  setPitch(next);
}

function pipOn(n: number): boolean {
  if (n === 0) return true;
  return pitch.value < 0 ? n < 0 && n >= pitch.value : n > 0 && n <= pitch.value;
}

const BG_MODES: { value: BgMode; text: string }[] = [
  { value: "auto", text: "自動" },
  { value: "mv", text: "原版 MV" },
  { value: "pv", text: "文字PV" },
];
const bgMode = computed(() => store.session.bg_mode);
const pvKaraoke = computed(() => store.session.pv_karaoke);
const AMBIENCES: { value: Ambience; text: string }[] = [
  { value: "clean", text: "原音" },
  { value: "room", text: "包廂" },
  { value: "concert", text: "演唱會" },
];
const ambience = computed(() => store.session.ambience);
const beatLights = computed(() => store.session.beat_lights);
const AMBIENCE_HINT: Record<Ambience, string> = {
  clean: "音樂直接輸出，不加任何效果。",
  room: "加一點包廂殘響、低音和立體感，聲音比較飽滿。",
  concert: "大場地殘響、更寬的立體聲，像在演唱會現場。",
};
const BG_HINT: Record<BgMode, string> = {
  auto: "有 MV 就播 MV，沒有 MV 或還在下載時顯示文字PV。",
  mv: "只播原版 MV；這首沒有 MV 時是一般背景。",
  pv: "一律顯示文字PV，即使這首有 MV。",
};

// 音源外掛提供的清單（沒裝外掛只有「自動」，整個設定就不顯示）
const audioSources = computed(() => store.session.audio_sources);
const audioSource = computed(() => store.session.audio_source || "auto");

// -- queue --------------------------------------------------------------------
const STATUS_TEXT: Record<ItemStatus, string> = {
  PREPARING: "準備中",
  READY: "可播放",
  PLAYING: "播放中",
  FAILED: "失敗",
};
const STATUS_CLASS: Record<ItemStatus, string> = {
  PREPARING: "prep",
  READY: "ready",
  PLAYING: "ready",
  FAILED: "fail",
};

// tap a row to open its actions; only one row is open at a time
const openId = ref<string | null>(null);
function toggleRow(item: QueueItem) {
  openId.value = openId.value === item.id ? null : item.id;
}

function move(item: QueueItem, index: number) {
  queueMove(item.id, index);
}

function bump(item: QueueItem) {
  queueBump(item.id);
  openId.value = null;
}

function remove(item: QueueItem) {
  queueRemove(item.id);
  openId.value = null;
}

const waitText = computed(() => {
  if (!queue.value.length) return "";
  const remaining = current.value ? Math.max(0, progress.durationMs.value - progress.positionMs.value) : 0;
  const total = queue.value.reduce((sum, item) => sum + (item.track.duration_ms || 0), remaining);
  return `全部唱完約 ${Math.max(1, Math.round(total / 60000))} 分鐘`;
});

// -- tiles (first glyph on a hue derived from the title; 有封面時蓋在上面) --
function hueOf(text: string): number {
  let h = 7;
  for (const ch of text) h = (h * 31 + (ch.codePointAt(0) ?? 0)) % 360;
  return h;
}
function glyphOf(text: string): string {
  return [...text.trim()][0] ?? "♪";
}

// -- server sync --------------------------------------------------------------
function syncFromSession() {
  // 升降 key 是 session 級的（同音量），即使沒有正在播放的歌曲也要同步
  pitch.value = store.session.pitch_semitones ?? 0;
  const item = current.value;
  if (!item) return;
  mode.value = item.karaoke_mode;
  guidePct.value = Math.round(item.guide_vocal * 100);
  offsetMs.value = item.track.lyrics_offset_ms;
}

watch(
  () => queue.value.map((item) => item.id).join(),
  () => {
    if (openId.value && !queue.value.some((item) => item.id === openId.value)) openId.value = null;
  },
);

onMounted(() => {
  connect("host", "Host");
  syncFromSession();
  on("session.snapshot", syncFromSession);
});
</script>

<template>
  <div class="cyber host">
    <header class="top">
      <div class="logo">LOCAL<b>KTV</b></div>
      <span class="role cut">HOST</span>
      <a class="room-link" :href="url('/')" title="切換房間">{{ store.session.room_name || store.session.room }} ▾</a>
      <div class="who">
        <span class="dot" :class="{ on: store.connected }" />
        <span v-if="store.connected">{{ store.session.users.length }} 人在線</span>
        <span v-else>連線中…</span>
      </div>
    </header>

    <div class="body">
      <!-- ===================== 播放台（常駐） ===================== -->
      <section class="deck cut" :class="{ idle: !current }" aria-label="正在播放">
        <template v-if="current">
          <div class="label">
            <i>//</i> {{ playing ? "NOW PLAYING" : "PAUSED" }} · {{ MODES.find((m) => m.value === current!.karaoke_mode)?.text }}
          </div>
          <div class="np">
            <div class="tile big cut" :style="{ '--h': hueOf(current.track.title) }">
              {{ glyphOf(current.track.title) }}
              <img v-if="trackCover(current.track)" :src="trackCover(current.track)" alt="" loading="lazy" decoding="async" @error="hideImg" />
            </div>
            <div class="meta">
              <div class="np-title">{{ current.track.title }}</div>
              <div class="s">
                <span class="ellipsis">{{ current.track.artist }} · <b>{{ current.requested_by }}</b> 點的</span>
              </div>
            </div>
          </div>
          <div class="seek-wrap" :style="{ '--p': `${shownPct}%` }">
            <input
              class="seek"
              type="range"
              min="0"
              :max="Math.max(1, progress.durationMs.value)"
              :value="shownMs"
              aria-label="播放進度"
              @input="onSeekInput"
              @change="onSeekChange"
            />
          </div>
          <div class="times">
            <span>{{ fmt(shownMs) }}</span>
            <span>{{ fmt(progress.durationMs.value) }}</span>
          </div>
          <div class="transport">
            <button class="play cut" :aria-label="playing ? '暫停' : '播放'" @click="togglePlay">
              <svg v-if="playing" width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z" fill="currentColor" />
              </svg>
              <svg v-else width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M7 4.5v15l12.5-7.5z" fill="currentColor" />
              </svg>
              {{ playing ? "暫停" : "播放" }}
            </button>
            <button class="skip cut" @click="playback('skip')">
              <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M5 5v14l10-7zM16.5 5H19v14h-2.5z" fill="currentColor" />
              </svg>
              卡歌
            </button>
          </div>
          <div v-if="queue.length" class="next">下一首：{{ queue[0].track.title }} · {{ queue[0].requested_by }}</div>
        </template>
        <template v-else>
          <div class="label"><i>//</i> STANDBY</div>
          <div class="idle-text">
            {{ queue.length ? `目前沒在播，下一首是「${queue[0].track.title}」` : "目前沒有播放中的歌，等大家點歌" }}
          </div>
          <div v-if="queue.length" class="transport">
            <button class="play cut" @click="playback('play')">
              <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M7 4.5v15l12.5-7.5z" fill="currentColor" />
              </svg>
              開始播放
            </button>
          </div>
        </template>
      </section>

      <!-- ===================== 分頁 ===================== -->
      <div class="panes" :data-view="view">
        <!-- 清單 -->
        <section class="pane pane-queue">
          <div class="q-head">
            <div class="label"><i>//</i> 待唱清單 · {{ queue.length }} 首</div>
            <span class="grow" />
            <span class="wait">{{ waitText }}</span>
          </div>
          <ul v-if="queue.length" class="list">
            <li
              v-for="(item, index) in queue"
              :key="item.id"
              class="q-row"
              :class="{ open: openId === item.id, failed: item.status === 'FAILED' }"
            >
              <button class="q-main" :aria-expanded="openId === item.id" @click="toggleRow(item)">
                <span class="pos">{{ index + 1 }}</span>
                <span class="tile cut" :style="{ '--h': hueOf(item.track.title) }">
                  {{ glyphOf(item.track.title) }}
                  <img v-if="trackCover(item.track)" :src="trackCover(item.track)" alt="" loading="lazy" decoding="async" @error="hideImg" />
                </span>
                <span class="meta">
                  <span class="t">{{ item.track.title }}</span>
                  <span class="s">
                    <span class="state" :class="STATUS_CLASS[item.status]">{{ STATUS_TEXT[item.status] }}</span>
                    <span class="ellipsis">
                      · {{ item.track.artist }} · {{ item.requested_by }}
                      <template v-if="item.status === 'PREPARING' && item.progress_message">
                        · {{ item.progress_message }}
                      </template>
                    </span>
                  </span>
                </span>
                <svg class="chev" width="18" height="18" viewBox="0 0 24 24" aria-hidden="true">
                  <path d="m6 9 6 6 6-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
                </svg>
              </button>
              <div v-if="openId === item.id" class="q-actions">
                <button class="cut" :disabled="index === 0" @click="move(item, index - 1)">
                  <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M12 19V5m-6 6 6-6 6 6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
                  </svg>
                  上移
                </button>
                <button class="cut" :disabled="index === queue.length - 1" @click="move(item, index + 1)">
                  <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M12 5v14m-6-6 6 6 6-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
                  </svg>
                  下移
                </button>
                <button class="cut" :disabled="index === 0" @click="bump(item)">
                  <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M5 4h14M12 20V9m-5 5 5-5 5 5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
                  </svg>
                  插到最前
                </button>
                <button class="cut danger" @click="remove(item)">
                  <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M6 6l12 12M18 6 6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
                  </svg>
                  刪除
                </button>
              </div>
            </li>
          </ul>
          <div v-else class="empty">還沒有人點歌，請大家掃大螢幕的 QR 點歌</div>
        </section>

        <!-- 這首 -->
        <section class="pane pane-song">
          <div class="label"><i>//</i> 這首歌</div>
          <template v-if="current">
            <div class="ctrl cut">
              <div class="ctrl-h"><strong>唱法</strong></div>
              <div class="seg" role="radiogroup" aria-label="唱法">
                <button
                  v-for="m in MODES"
                  :key="m.value"
                  role="radio"
                  :aria-checked="mode === m.value"
                  :class="{ on: mode === m.value }"
                  @click="changeMode(m.value)"
                >
                  {{ m.text }}
                </button>
              </div>
              <div class="hint">{{ modeHint }}</div>
            </div>

            <div class="ctrl cut">
              <div class="ctrl-h"><strong>導唱</strong><span class="ctrl-v">{{ guidePct }}%</span></div>
              <input type="range" min="0" max="100" step="1" :value="guidePct" aria-label="導唱" @input="onGuideInput" />
              <div class="hint">0% 只有伴奏，100% 接近原唱。</div>
            </div>

            <div class="ctrl cut">
              <div class="ctrl-h"><strong>歌詞偏移</strong><span class="ctrl-v">{{ offsetText }}</span></div>
              <div class="nudge">
                <button class="cut" @click="adjustOffset(-500)">−0.5</button>
                <button class="cut" @click="adjustOffset(-100)">−0.1</button>
                <button class="cut zero" :disabled="offsetMs === 0" @click="adjustOffset(0)">歸零</button>
                <button class="cut" @click="adjustOffset(100)">+0.1</button>
                <button class="cut" @click="adjustOffset(500)">+0.5</button>
              </div>
              <div class="hint">歌詞比歌聲早就按 +，晚就按 −。會記住，下次唱這首也套用。</div>
            </div>
          </template>
          <div v-else class="empty">播歌時才能調這首的唱法、導唱和歌詞</div>
        </section>

        <!-- 包廂 -->
        <section class="pane pane-room">
          <div class="label"><i>//</i> 包廂設定 · 全場共用</div>
          <div class="ctrl cut">
            <div class="ctrl-h"><strong>音量</strong><span class="ctrl-v">{{ volumePct }}%</span></div>
            <input type="range" min="0" max="100" step="1" :value="volumePct" aria-label="音量" @input="onVolumeInput" />
          </div>

          <div class="ctrl cut">
            <div class="ctrl-h">
              <strong>升降 Key</strong>
              <span class="ctrl-v">{{ pitch > 0 ? `+${pitch}` : pitch }}</span>
            </div>
            <div class="stepper">
              <button class="cut" :disabled="!pitchCapable || pitch <= -3" aria-label="降半音" @click="stepPitch(-1)">−</button>
              <div class="pips" aria-hidden="true">
                <i v-for="n in PIPS" :key="n" :class="{ on: pipOn(n), zero: n === 0 }" />
              </div>
              <button class="cut" :disabled="!pitchCapable || pitch >= 3" aria-label="升半音" @click="stepPitch(1)">+</button>
            </div>
            <div class="hint">
              {{ pitchCapable ? "每次半音，±3 為上限，切換時平滑過渡。" : "大螢幕要用 localhost 開啟 /display 才能升降 Key" }}
            </div>
          </div>

          <div class="ctrl cut">
            <div class="ctrl-h"><strong>大螢幕畫面</strong></div>
            <div class="seg" role="radiogroup" aria-label="大螢幕背景">
              <button
                v-for="m in BG_MODES"
                :key="m.value"
                role="radio"
                :aria-checked="bgMode === m.value"
                :class="{ on: bgMode === m.value }"
                @click="setVisual({ bg_mode: m.value })"
              >
                {{ m.text }}
              </button>
            </div>
            <button
              class="switch-row"
              role="switch"
              :aria-checked="pvKaraoke"
              :disabled="bgMode === 'mv'"
              @click="setVisual({ pv_karaoke: !pvKaraoke })"
            >
              <span>文字PV 時顯示卡拉OK歌詞</span>
              <i class="switch" :class="{ on: pvKaraoke }" aria-hidden="true" />
            </button>
            <div class="hint">
              {{ BG_HINT[bgMode] }}{{ bgMode !== "mv" && !pvKaraoke ? "文字PV 會佔滿整個畫面。" : "" }}
            </div>
          </div>

          <div class="ctrl cut">
            <div class="ctrl-h"><strong>氣氛</strong></div>
            <div class="seg" role="radiogroup" aria-label="包廂音場">
              <button
                v-for="a in AMBIENCES"
                :key="a.value"
                role="radio"
                :aria-checked="ambience === a.value"
                :class="{ on: ambience === a.value }"
                @click="setVisual({ ambience: a.value })"
              >
                {{ a.text }}
              </button>
            </div>
            <button
              class="switch-row"
              role="switch"
              :aria-checked="beatLights"
              @click="setVisual({ beat_lights: !beatLights })"
            >
              <span>畫面燈光跟著節拍</span>
              <i class="switch" :class="{ on: beatLights }" aria-hidden="true" />
            </button>
            <div class="hint">{{ AMBIENCE_HINT[ambience] }}</div>
          </div>

          <div v-if="audioSources.length > 1" class="ctrl cut">
            <div class="ctrl-h"><strong>主要音源</strong></div>
            <div class="seg four" role="radiogroup" aria-label="主要音源">
              <button
                v-for="s in audioSources"
                :key="s.value"
                role="radio"
                :aria-checked="audioSource === s.value"
                :class="{ on: audioSource === s.value }"
                @click="setAudioSource(s.value)"
              >
                {{ s.text }}
              </button>
            </div>
            <div class="hint">下載新歌時先找這個音源，失敗再自動換其他備用源。</div>
          </div>
        </section>
      </div>
    </div>

    <nav class="nav">
      <button :class="{ on: view === 'queue' }" @click="view = 'queue'">
        <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M4 6h16M4 12h16M4 18h10" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
        </svg>
        清單
        <span v-if="queue.length" class="badge">{{ queue.length }}</span>
      </button>
      <button :class="{ on: view === 'song' }" @click="view = 'song'">
        <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">
          <rect x="9" y="3" width="6" height="11" rx="3" fill="none" stroke="currentColor" stroke-width="1.8" />
          <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
        </svg>
        這首
      </button>
      <button :class="{ on: view === 'room' }" @click="view = 'room'">
        <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M4 7h10M18 7h2M4 17h4M12 17h8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
          <circle cx="16" cy="7" r="2" fill="none" stroke="currentColor" stroke-width="1.8" />
          <circle cx="10" cy="17" r="2" fill="none" stroke="currentColor" stroke-width="1.8" />
        </svg>
        包廂
      </button>
    </nav>
  </div>
</template>

<style scoped>
/* HUD face for labels and numbers only — Chinese text stays in the system font.
   Chakra Petch, SIL OFL 1.1 (web/public/fonts/OFL-ChakraPetch.txt). */
@font-face {
  font-family: "Chakra Petch";
  src: url("/fonts/chakra-petch-600.woff2") format("woff2");
  font-weight: 600;
  font-display: swap;
}
@font-face {
  font-family: "Chakra Petch";
  src: url("/fonts/chakra-petch-700.woff2") format("woff2");
  font-weight: 700;
  font-display: swap;
}

.cyber {
  --void: #07060d;
  --panel: #110f1c;
  --panel-2: #191629;
  --line: rgba(25, 240, 255, 0.16);
  --line-strong: rgba(25, 240, 255, 0.45);
  --cyan: #19f0ff;
  --magenta: #ff2fb4;
  --volt: #f2ff49;
  --ok: #3dffa8;
  --danger: #ff4d6a;
  --fg: #eeedf7;
  --dim: #918cab;
  --hud: "Chakra Petch", "Bahnschrift", "Segoe UI", system-ui, sans-serif;
  --cut: 10px;
  --gutter: max(16px, env(safe-area-inset-left, 0px));

  position: relative;
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  color: var(--fg);
  font-size: 15px;
  line-height: 1.5;
  background:
    linear-gradient(rgba(25, 240, 255, 0.035) 1px, transparent 1px) 0 0 / 100% 28px,
    radial-gradient(700px 420px at 90% -10%, rgba(255, 47, 180, 0.12), transparent 60%),
    radial-gradient(600px 420px at -10% 110%, rgba(25, 240, 255, 0.1), transparent 60%),
    var(--void);
  padding-top: env(safe-area-inset-top, 0px);
}

.cyber button {
  all: unset;
  box-sizing: border-box;
  cursor: pointer;
}
.cyber button:focus-visible {
  outline: 2px solid var(--volt);
  outline-offset: 2px;
}
.cyber button:disabled {
  opacity: 0.3;
  cursor: default;
}

.cut {
  clip-path: polygon(
    var(--cut) 0,
    100% 0,
    100% calc(100% - var(--cut)),
    calc(100% - var(--cut)) 100%,
    0 100%,
    0 var(--cut)
  );
}
.grow {
  flex: 1;
}
.ellipsis {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* -- header -------------------------------------------------------------- */
.top {
  flex: none;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px var(--gutter) 10px;
}
.logo {
  font: 700 18px/1 var(--hud);
  letter-spacing: 0.06em;
}
.logo b {
  color: var(--cyan);
}
.room-link {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  color: var(--fg-dim, #a79fc4);
  text-decoration: none;
}
.role {
  --cut: 5px;
  padding: 4px 8px;
  font: 700 11px/1 var(--hud);
  letter-spacing: 0.16em;
  color: var(--void);
  background: var(--volt);
}
.who {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 12px;
  border: 1px solid var(--line);
  border-radius: 999px;
  font-size: 13px;
  white-space: nowrap;
}
.dot {
  flex: none;
  width: 8px;
  height: 8px;
  margin: 0;
  border-radius: 50%;
  background: var(--danger);
}
.dot.on {
  background: var(--ok);
  box-shadow: 0 0 8px var(--ok);
}

/* -- layout -------------------------------------------------------------- */
.body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.panes {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 14px var(--gutter) 20px;
}
.pane {
  display: none;
  flex-direction: column;
  gap: 14px;
}
.panes[data-view="queue"] .pane-queue,
.panes[data-view="song"] .pane-song,
.panes[data-view="room"] .pane-room {
  display: flex;
}
@media (min-width: 600px) and (max-width: 899px) {
  .top,
  .body {
    width: 100%;
    max-width: 600px;
    margin: 0 auto;
  }
}

.label {
  font: 600 11px/1.4 var(--hud);
  letter-spacing: 0.18em;
  color: var(--dim);
}
.label i {
  font-style: normal;
  color: var(--cyan);
}
.empty {
  color: var(--dim);
  text-align: center;
  padding: 32px 8px;
  font-size: 14px;
}
.hint {
  font-size: 12px;
  color: var(--dim);
}

/* -- deck ---------------------------------------------------------------- */
.deck {
  --cut: 14px;
  flex: none;
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin: 0 var(--gutter);
  padding: 14px 16px 16px;
  background:
    linear-gradient(135deg, rgba(255, 47, 180, 0.12), transparent 55%),
    var(--panel-2);
  border: 1px solid var(--line-strong);
}
.deck.idle {
  border-color: var(--line);
  background: var(--panel);
}
.np {
  display: flex;
  align-items: center;
  gap: 12px;
}
.meta {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.np-title {
  font-size: 22px;
  font-weight: 700;
  line-height: 1.25;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.np .s b {
  color: var(--magenta);
  font-weight: 600;
}
.tile {
  --h: 190;
  --cut: 8px;
  flex: none;
  width: 56px;
  height: 56px;
  display: grid;
  place-items: center;
  font-size: 22px;
  font-weight: 700;
  color: #fff;
  background: linear-gradient(135deg, hsl(var(--h) 90% 45%), hsl(calc(var(--h) + 60) 85% 35%));
}
/* 封面蓋在首字方塊上；載入中或失敗時看到的就是底下的方塊 */
.tile {
  position: relative;
}
.tile img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.tile.big {
  --cut: 10px;
  width: 72px;
  height: 72px;
  font-size: 28px;
}
.idle-text {
  color: var(--dim);
  font-size: 14px;
}

/* seek：track fills to --p, thumb stays draggable */
.seek-wrap {
  margin-top: 2px;
}
.cyber input.seek {
  margin: 0;
  height: 24px;
}
.cyber input.seek::-webkit-slider-runnable-track {
  height: 6px;
  border-radius: 0;
  opacity: 1;
  background: linear-gradient(
    90deg,
    var(--cyan) 0%,
    var(--magenta) var(--p, 0%),
    rgba(255, 255, 255, 0.1) var(--p, 0%)
  );
}
.cyber input.seek::-webkit-slider-thumb {
  width: 14px;
  height: 20px;
  margin-top: -7px;
  border-radius: 2px;
  background: var(--fg);
  border: 0;
  box-shadow: 0 0 12px var(--cyan);
}
.cyber input.seek::-moz-range-track {
  height: 6px;
  border-radius: 0;
  opacity: 1;
  background: linear-gradient(
    90deg,
    var(--cyan) 0%,
    var(--magenta) var(--p, 0%),
    rgba(255, 255, 255, 0.1) var(--p, 0%)
  );
}
.cyber input.seek::-moz-range-thumb {
  width: 12px;
  height: 18px;
  border-radius: 2px;
  background: var(--fg);
  border: 0;
  box-shadow: 0 0 12px var(--cyan);
}
.times {
  display: flex;
  justify-content: space-between;
  margin-top: -6px;
  font: 600 13px/1 var(--hud);
  color: var(--dim);
  font-variant-numeric: tabular-nums;
}
.transport {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.cyber .transport button {
  --cut: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  height: 52px;
  font-size: 16px;
  font-weight: 700;
}
.cyber .transport .play {
  color: var(--void);
  background: var(--cyan);
  box-shadow: 0 0 22px rgba(25, 240, 255, 0.35);
}
.cyber .transport .play:only-child {
  grid-column: 1 / -1;
}
.cyber .transport .skip {
  color: #fff;
  background: var(--magenta);
  box-shadow: 0 0 22px rgba(255, 47, 180, 0.35);
}
.cyber .transport button:active {
  transform: scale(0.97);
}
.next {
  font-size: 13px;
  color: var(--dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* -- queue --------------------------------------------------------------- */
.q-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.wait {
  font-size: 12px;
  color: var(--volt);
}
.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
}
.q-row {
  border-bottom: 1px solid rgba(255, 255, 255, 0.05);
}
.q-row:last-child {
  border-bottom: 0;
}
.q-row.open {
  background: linear-gradient(90deg, rgba(25, 240, 255, 0.08), transparent 80%);
  box-shadow: inset 2px 0 0 var(--cyan);
}
.q-row.failed .t {
  color: var(--danger);
}
.cyber .q-main {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 4px 10px 0;
}
.pos {
  flex: none;
  width: 26px;
  text-align: center;
  font: 700 15px/1 var(--hud);
  color: var(--dim);
  font-variant-numeric: tabular-nums;
}
.q-row:first-child .pos {
  color: var(--volt);
}
.t {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.s {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--dim);
  white-space: nowrap;
  overflow: hidden;
}
.state {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
}
.state::before {
  content: "";
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}
.state.ready {
  color: var(--ok);
}
.state.prep {
  color: var(--volt);
}
.state.fail {
  color: var(--danger);
}
.chev {
  flex: none;
  color: var(--dim);
  transition: transform 0.15s;
}
.q-row.open .chev {
  transform: rotate(180deg);
  color: var(--cyan);
}
.q-actions {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 6px;
  padding: 0 4px 12px 38px;
}
.cyber .q-actions button {
  --cut: 6px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  padding: 8px 0 6px;
  font-size: 12px;
  color: var(--fg);
  background: var(--panel-2);
  border: 1px solid var(--line);
}
.cyber .q-actions button svg {
  color: var(--cyan);
}
.cyber .q-actions button.danger,
.cyber .q-actions button.danger svg {
  color: var(--danger);
}

/* -- controls ------------------------------------------------------------ */
.ctrl {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 14px;
  background: var(--panel);
  border: 1px solid var(--line);
}
.ctrl-h {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}
.ctrl-v {
  font: 700 18px/1 var(--hud);
  color: var(--cyan);
  font-variant-numeric: tabular-nums;
}
.ctrl input[type="range"] {
  margin: 0;
}
.ctrl input[type="range"]::-webkit-slider-runnable-track {
  background: linear-gradient(90deg, var(--cyan), var(--magenta));
  border-radius: 0;
  height: 4px;
  opacity: 1;
}
.ctrl input[type="range"]::-webkit-slider-thumb {
  width: 20px;
  height: 20px;
  margin-top: -8px;
  border-radius: 2px;
  background: var(--void);
  border: 2px solid var(--cyan);
  box-shadow: 0 0 10px var(--cyan);
}
.ctrl input[type="range"]::-moz-range-track {
  background: linear-gradient(90deg, var(--cyan), var(--magenta));
  border-radius: 0;
  height: 4px;
  opacity: 1;
}
.ctrl input[type="range"]::-moz-range-thumb {
  width: 18px;
  height: 18px;
  border-radius: 2px;
  background: var(--void);
  border: 2px solid var(--cyan);
  box-shadow: 0 0 10px var(--cyan);
}
.seg {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 4px;
  padding: 4px;
  background: var(--void);
  border: 1px solid var(--line);
}
.seg.four {
  grid-template-columns: repeat(4, 1fr);
}
.cyber .seg button {
  text-align: center;
  padding: 9px 0;
  font-size: 14px;
  color: var(--dim);
}
.cyber .seg button.on {
  background: var(--panel-2);
  color: var(--fg);
  box-shadow: inset 0 -2px 0 var(--cyan);
}
.nudge {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 6px;
}
.cyber .nudge button {
  --cut: 6px;
  height: 42px;
  display: grid;
  place-items: center;
  font: 700 15px/1 var(--hud);
  color: var(--cyan);
  border: 1px solid var(--line-strong);
}
.cyber .nudge button.zero {
  font: 600 13px/1 inherit;
  color: var(--fg);
  border-color: var(--line);
}
.stepper {
  display: grid;
  grid-template-columns: 48px 1fr 48px;
  align-items: center;
  gap: 10px;
}
.cyber .stepper button {
  --cut: 8px;
  height: 44px;
  display: grid;
  place-items: center;
  font: 700 20px/1 var(--hud);
  color: var(--cyan);
  border: 1px solid var(--line-strong);
}
.pips {
  display: flex;
  justify-content: center;
  gap: 6px;
}
.pips i {
  width: 14px;
  height: 6px;
  background: rgba(255, 255, 255, 0.1);
}
.pips i.zero {
  width: 4px;
}
.pips i.on {
  background: var(--cyan);
  box-shadow: 0 0 6px var(--cyan);
}
.cyber .switch-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 40px;
  font-size: 14px;
  color: var(--fg);
}
.cyber .switch-row:disabled {
  opacity: 0.4;
}
.switch {
  flex: none;
  position: relative;
  width: 44px;
  height: 22px;
  border: 1px solid var(--line-strong);
  background: rgba(255, 255, 255, 0.06);
  transition: background 0.15s, border-color 0.15s;
}
.switch::after {
  content: "";
  position: absolute;
  top: 3px;
  left: 3px;
  width: 14px;
  height: 14px;
  background: var(--dim);
  transition: transform 0.15s, background 0.15s;
}
.switch.on {
  border-color: var(--cyan);
  background: rgba(25, 240, 255, 0.14);
}
.switch.on::after {
  transform: translateX(22px);
  background: var(--cyan);
  box-shadow: 0 0 8px var(--cyan);
}

/* -- bottom nav ---------------------------------------------------------- */
.nav {
  flex: none;
  position: relative;
  z-index: 2;
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  background: var(--void);
  border-top: 1px solid var(--line);
  padding-bottom: max(8px, env(safe-area-inset-bottom, 0px));
}
.cyber .nav button {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 3px;
  padding: 10px 0 4px;
  font-size: 12px;
  color: var(--dim);
}
.cyber .nav button.on {
  color: var(--cyan);
}
.cyber .nav button.on::before {
  content: "";
  position: absolute;
  top: -1px;
  left: 30%;
  right: 30%;
  height: 2px;
  background: var(--cyan);
  box-shadow: 0 0 10px var(--cyan);
}
.badge {
  position: absolute;
  top: 6px;
  left: calc(50% + 8px);
  padding: 2px 5px;
  border-radius: 999px;
  font: 700 10px/1 var(--hud);
  background: var(--magenta);
  color: #fff;
}

/* tablet / laptop: controls left, queue right, no tab bar */
@media (min-width: 900px) {
  .top {
    width: 100%;
    max-width: 1180px;
    margin: 0 auto;
    padding-top: 20px;
  }
  .body {
    overflow-y: auto;
    display: grid;
    grid-template-columns: minmax(360px, 440px) minmax(0, 1fr);
    align-content: start;
    gap: 14px 28px;
    width: 100%;
    max-width: 1180px;
    margin: 0 auto;
    padding: 0 var(--gutter) 24px;
  }
  .deck {
    grid-column: 1;
    margin: 0;
  }
  .panes {
    display: contents;
  }
  .panes .pane {
    display: flex;
  }
  .pane-song {
    grid-column: 1;
    grid-row: 2;
  }
  .pane-room {
    grid-column: 1;
    grid-row: 3;
  }
  .pane-queue {
    grid-column: 2;
    grid-row: 1 / span 3;
    align-self: start;
  }
  .nav {
    display: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .chev,
  .switch,
  .switch::after {
    transition: none;
  }
}
</style>
