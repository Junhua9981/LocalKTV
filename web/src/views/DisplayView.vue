<script setup lang="ts">
/**
 * /display — runs on the KTV PC.  The only client that actually produces
 * sound and the owner of the playback clock.
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from "vue";
import { AudioEngine } from "../audio/engine";
import { MicReceiver, type MonitorState } from "../audio/mic";
import { MicScorer, fetchPitch, type MicState } from "../audio/score";
import type { Judgement, LineResult, SongResult } from "../lib/scoring";
import { room, url } from "../base";
import { connect, on, onConnected, reportPitchCapable, send, store } from "../composables/useKtv";
import { fetchLyrics } from "../lib/lyrics";
import type { LyricLine, LyricTiming } from "../lib/lyrics";
import { buildCountIns, dotsLeft } from "../lib/countin";
import { fitLines } from "../lib/lyricfit";
import { duetParts, keptAt, keptSpans, sungLines } from "../lib/duet";
import { loadLyricFontCss, lyricFont, lyricPx, preloadLyricGlyphs } from "../lib/lyricfont";
import type { CountIn } from "../lib/countin";
import LyricStage from "../components/LyricStage.vue";
import BeatLights from "../components/BeatLights.vue";
import TextPV from "../components/TextPV.vue";
import PitchBar from "../components/PitchBar.vue";
import ScoreResult from "../components/ScoreResult.vue";
import PerfHud from "../components/PerfHud.vue";
import { PerfMeter, detectCompat } from "../lib/compat";
import type { DisplayLoad, QueueItem, ScorePosted, SongScore, Stems } from "../types";

const engine = shallowRef<AudioEngine | null>(null);
/** 瀏覽器版本與功能支援（智慧電視的舊 WebView 會缺東西）；?perf=1 畫在角落 */
const compat = detectCompat();
const showPerfHud = new URLSearchParams(location.search).has("perf");
/** 播歌時每分鐘回報一次幀率給伺服器 log */
const perfMeter = new PerfMeter();
const PERF_REPORT_MS = 60_000;
let perfTimer = 0;
/**
 * /display?mvtest=1：有 MV 的歌播放時，每 15 秒換一種畫法並各自回報幀率，找出
 * 弱電視上 MV 卡頓的元兇——all 照常／video-only 只剩 MV／no-video 藏起 MV／
 * no-sync 不用播放速度追歌。
 */
const MV_TESTS = ["all", "video-only", "no-video", "no-sync"] as const;
const mvTestOn = new URLSearchParams(location.search).has("mvtest");
const mvTest = ref<(typeof MV_TESTS)[number]>("all");
const mvTestMeter = new PerfMeter();
const MV_TEST_MS = 15_000;
let mvTestTimer = 0;
let mvTestStep = 0;
/**
 * 省電畫面（遙控器「效果」）：完整 / 省電，或自動 = 播歌時連續兩個 5 秒有三成
 * 以上的幀超過 25ms，就改用省電直到重新整理（不來回切換）。
 */
const fxMeter = new PerfMeter();
const FX_WINDOW_MS = 5000;
const FX_SLOW_LIMIT = 0.3;
let fxTimer = 0;
let fxStrikes = 0;
const autoLite = ref(false);
const lite = computed(
  () => store.session.fx_mode === "lite" || (store.session.fx_mode === "auto" && autoLite.value),
);
/** 手機麥克風（WebRTC 直連）。電視上只顯示誰的麥克風開著、誰正在唱。 */
const mics = shallowRef<MicReceiver | null>(null);
const activeItem = ref<QueueItem | null>(null);
/**
 * 評分：電視聽電腦的錄音輸入（藍牙喇叭麥克風組傳進電腦的收音），對伺服器從
 * AI 人聲軌抽出的標準音（pitch.json）打分。手機麥克風延遲太高，不拿來評分。
 */
const scoring = shallowRef<MicScorer | null>(null);
/** 這首歌的標準音；null = 還沒有 AI 人聲軌，尚未有評分 */
const pitchUrl = ref<string | null>(null);
const scoreLoaded = ref(false);
const micState = ref<MicState>("off");
const micSilent = ref(false);
/** 麥克風外放（電腦麥克風從喇叭出聲）的狀態 */
const monitorState = ref<MonitorState>("off");
/** 每句唱完跳出來的判定 */
const judgement = ref<{ text: Judgement; id: number } | null>(null);
let judgeTimer = 0;
/** 唱完的結算畫面 */
const scoreResult = shallowRef<{
  result: SongResult;
  title: string;
  singer: string;
  itemId: string;
  /** 這次之前的本曲最高分（伺服器回 score.posted 才知道） */
  best?: SongScore | null;
} | null>(null);
let resultTimer = 0;
const RESULT_MS = 8000;
// invite QR: dead centre until the first song of the session loads, then it
// tucks into the top-right corner (the page's lifetime is the "session").
const hasSung = ref(false);
// dynamic binding: a static src="/api/..." would be rewritten as a build-time
// asset import — this one is a live server route.  On a VPS the QR is the
// current party invite: it rotates every few minutes, so poll it.  Asking for
// it opens the room's party; hide it while it fails.
const qrStamp = ref(Date.now());
const qrUrl = computed(() => `${url("/api/qr.svg")}?room=${encodeURIComponent(room())}&t=${qrStamp.value}`);
const qrOk = ref(true);
let qrTimer = 0;
onMounted(() => {
  qrTimer = window.setInterval(() => (qrStamp.value = Date.now()), 30_000);
});
onBeforeUnmount(() => window.clearInterval(qrTimer));
/** How long a load error stays on screen before the song is skipped. */
const MEDIA_ERROR_SKIP_MS = 3000;
// kept as raw plain objects: only the rAF clock should trigger re-renders
const lyricLines = shallowRef<LyricLine[]>([]);
const lyricOffsetMs = ref(0);
// "word" =逐字 sweep, "line" = whole-line highlight (LyricStage renders it)
const lyricTiming = ref<LyricTiming>("word");
// 日文歌：歌詞用日文字型（lib/lyricfont.ts LYRIC_FAMILY_JA）
const lyricJapanese = ref(false);
// 遙控器「日文歌」：假名／空耳（舊伺服器沒有這個欄位 → 兩者）
const jaAssist = computed(() => store.session.ja_assist ?? "both");
/**
 * The karaoke lines as LyricStage shows them: a line too wide for its slot
 * is cut and its second half becomes the next line (lower-right) instead of
 * wrapping.  Same width and font as LyricStage's `.line`; re-fitted when the
 * screen size changes or the lyric font finishes loading.
 */
const stageWidth = ref(window.innerWidth);
const fontsReady = ref(0);
const stageLines = computed<LyricLine[]>(() => {
  void fontsReady.value;
  const vw = stageWidth.value;
  const ctx = document.createElement("canvas").getContext("2d");
  if (!ctx) return lyricLines.value;
  ctx.font = lyricFont(lyricPx(vw), lyricJapanese.value);
  // a little slack for the outline and the synthesised bold
  const maxWidth = Math.min(vw * 0.86, 1500) * 0.95;
  return fitLines(lyricLines.value, (text) => ctx.measureText(text).width, maxWidth);
});
const onResize = () => (stageWidth.value = window.innerWidth);
const onFontsLoaded = () => (fontsReady.value += 1);
loadLyricFontCss();
onMounted(() => {
  window.addEventListener("resize", onResize);
  // 歌詞字型是切片載入的，每載完一批切片就重新切句（見 lib/lyricfont.ts）
  document.fonts?.addEventListener?.("loadingdone", onFontsLoaded);
});
onBeforeUnmount(() => {
  window.removeEventListener("resize", onResize);
  document.fonts?.removeEventListener?.("loadingdone", onFontsLoaded);
});
const positionMs = ref(0);
const durationMs = ref(0);
const playing = ref(false);
const audioBlocked = ref(true);
const mode = ref<"ORIGINAL" | "INSTANT" | "AI">("ORIGINAL");
const guide = ref(0);
/** 對唱保留原唱：這些聲部（歌詞 part，0 = 合唱）的句子放原唱人聲 */
const keepParts = ref<number[]>([]);
const duet = computed(() => duetParts(lyricLines.value));
/** 原唱要開著的時段（歌詞時間）；不是對唱或沒選就是空的 */
const keepSpans = computed(() => (duet.value.length ? keptSpans(lyricLines.value, keepParts.value) : []));
/** TV 角落的小標：「原唱 aMEI」 */
const keepLabel = computed(() => {
  if (!keepSpans.value.length) return "";
  const kept = duet.value.filter((p) => keepParts.value.includes(p.part));
  return kept.map((p) => (p.part === 0 ? "合唱" : p.singer)).join("・");
});
const error = ref("");

// background MV (when the server has one for this track)
const mvUrl = ref<string | null>(null);
const mvEl = ref<HTMLVideoElement | null>(null);

// count-in dots: four circles vanish on the beat right before the first sung
// line (and after a long interlude) — the audio, intro and all, starts
// immediately.  The beat is estimated from the whole song's word timings.
const countIns = shallowRef<CountIn[]>([]);

// 升降 key 走延遲線，音訊會晚 15–45ms 出來；歌詞與 MV 都要跟著往後推
const pitchDelayMs = ref(0);

/**
 * The lyric clock, read straight from the engine.  Nothing reactive follows
 * it frame by frame: LyricStage / TextPV / PitchBar animate in their own
 * frame loops, and tick() below only writes refs whose value actually
 * changes (the count-in beat, the clock text every 250ms) — so this page is
 * not re-rendered 60 times a second.
 */
function displayNow(): number {
  const engineInstance = engine.value;
  if (!engineInstance) return 0;
  return Math.max(0, engineInstance.positionMs - engineInstance.pitchDelayMs - lyricOffsetMs.value);
}
/** Dots still lit (0 = no count-in) — set by tick() on the beat. */
const countIn = ref(0);
/** When the counted-in line starts (ms), or null — set by tick(). */
const countInAt = ref<number | null>(null);
/** Index of the line being counted in, or null (no karaoke lyrics on screen). */
const countInLine = computed<number | null>(() => {
  const lines = stageLines.value;
  const at = countInAt.value;
  if (countIn.value === 0 || at === null || !showKaraoke.value || !lines.length) return null;
  let best = 0;
  let bestGap = Infinity;
  lines.forEach((line, i) => {
    const gap = Math.abs((line.words[0]?.start ?? line.start) - at);
    if (gap < bestGap) [best, bestGap] = [i, gap];
  });
  return best;
});
/**
 * Which karaoke slot that line sings in (LyricStage: even index = upper-left
 * "a", odd = lower-right "b") — the dots sit right above its first word.
 * null: dots stay centred.
 */
const countInSlot = computed<"a" | "b" | null>(() =>
  countInLine.value === null ? null : countInLine.value % 2 === 0 ? "a" : "b",
);
/** Dots take the colour the line will fill with (duets: the singer's, 合唱 green). */
const countInColour = computed(() => {
  const part = countInLine.value === null ? undefined : stageLines.value[countInLine.value]?.part;
  if (typeof part !== "number") return "var(--blue)";
  return part === 0 ? "var(--part-0)" : `var(--part-${((part - 1) % 3) + 1})`;
});
const lyricsEl = ref<HTMLElement | null>(null);
/**
 * Pin the dots to the line's first glyph — a lower-right line is right
 * aligned, so its start depends on its length.  Measured once when the
 * counted-in line (or the screen / font) changes — the line does not move
 * while it is counted in; the slot classes are the fallback.
 */
const countInPos = ref<Record<string, string> | null>(null);
function measureCountIn() {
  const area = lyricsEl.value;
  const wrap = countInSlot.value && area?.querySelector(`.slot-${countInSlot.value} .wrap`);
  if (!area || !wrap) {
    countInPos.value = null;
    return;
  }
  const a = area.getBoundingClientRect();
  // 對唱行頭上方有演唱者小標：點排在小標右邊、上下置中，不疊在一起
  const tag = wrap.querySelector(".singer-tag");
  if (tag) {
    const t = tag.getBoundingClientRect();
    countInPos.value = {
      left: `${t.right - a.left + 14}px`,
      right: "auto",
      bottom: `${a.bottom - (t.top + t.bottom) / 2}px`,
      transform: "translateY(50%)",
    };
    return;
  }
  const w = wrap.getBoundingClientRect();
  countInPos.value = { left: `${w.left - a.left}px`, right: "auto", bottom: `${a.bottom - w.top + 10}px` };
}
watch([countInLine, stageWidth, fontsReady], () => {
  countInPos.value = null;
  if (countInLine.value === null) return;
  // after LyricStage has drawn the line (it may arrive in the same tick)
  void nextTick(() => requestAnimationFrame(measureCountIn));
});
const countInStyle = computed(() => ({ "--dot": countInColour.value, ...(countInPos.value ?? {}) }));

const title = computed(() => activeItem.value?.track.title ?? "LocalKTV");
const artist = computed(() => activeItem.value?.track.artist ?? "等待點歌…");
/** Video-only request: the picture carries the song — no lyrics zone at all. */
const videoOnly = computed(() => activeItem.value?.track.video_only ?? false);
/**
 * Background (session setting, see /remote 畫面): auto = the MV when this song
 * has one, 文字PV otherwise; "mv" keeps the plain gradient when there is no
 * MV.  A video-only request always shows its video.
 */
const showPV = computed(() => {
  if (!activeItem.value || videoOnly.value) return false;
  const bgMode = store.session.bg_mode;
  return bgMode === "pv" || (bgMode === "auto" && !mvUrl.value);
});
const showMV = computed(() => !!mvUrl.value && !showPV.value);
/** 文字PV may take the whole screen when the karaoke lyrics are switched off. */
const showKaraoke = computed(() => !showPV.value || store.session.pv_karaoke);
const percent = computed(() =>
  durationMs.value > 0 ? Math.min(100, (positionMs.value / durationMs.value) * 100) : 0,
);
const clockText = computed(() => `${fmt(positionMs.value)} / ${fmt(durationMs.value)}`);

function fmt(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function reportState(ended = false) {
  const engineInstance = engine.value;
  if (!engineInstance) return;
  // 舞台已清空（播完／卡歌）就不報：舊歌的長度不該回填到還在下載的下一首
  if (!ended && !activeItem.value) return;
  send({
    type: "display.state",
    queue_item_id: activeItem.value?.id ?? null,
    playing: ended ? false : engineInstance.playing,
    position_ms: ended ? 0 : engineInstance.positionMs,
    // 聚合來源的曲目 duration 常是 0 → 用實際音訊長度回填，手機的進度條才有總長
    duration_ms: engineInstance.durationMs,
    ended,
  });
}

async function loadLyrics(url: string | null) {
  if (!url) {
    lyricLines.value = [];
    lyricTiming.value = "word";
    lyricJapanese.value = false;
    countIns.value = [];
    return;
  }
  const doc = await fetchLyrics(url);
  lyricLines.value = doc?.lines ?? [];
  lyricTiming.value = doc?.timing ?? "word";
  lyricJapanese.value = doc?.japanese ?? false;
  preloadLyricGlyphs(
    lyricLines.value.flatMap((line) => [
      line.words.map((w) => w.text).join(""),
      ...(line.ruby ?? []).map((r) => r.text),
    ]),
    lyricJapanese.value,
  );
  preloadLyricGlyphs(lyricLines.value.flatMap((line) => (line.sora ?? []).map((s) => s.text)));
  if (doc) lyricOffsetMs.value = doc.offsetMs; // server-side offset wins on reload
  countIns.value = buildCountIns(lyricLines.value, lyricTiming.value === "word");
  void loadScore();
}

/** 換歌、歌詞到了、有了標準音、評分開關：重新準備這首的評分 */
async function loadScore() {
  const micScorer = scoring.value;
  if (!micScorer) return;
  const wanted = store.session.score ? pitchUrl.value : null;
  const doc = wanted ? await fetchPitch(wanted) : null;
  if (wanted !== (store.session.score ? pitchUrl.value : null)) return; // 換歌了
  // 歌詞時間 + 歌詞偏移 = 歌曲時間（見 displayNow）
  const offset = lyricOffsetMs.value;
  // 逐字歌詞：標準音一個字一個音（lib/scoring.ts wordNotes）
  const byWord = lyricTiming.value === "word";
  // 對唱保留原唱：原唱那位的句子不計分，也不畫音高條
  const lines = keepSpans.value.length ? sungLines(lyricLines.value, keepParts.value) : lyricLines.value;
  if (!lines.length && lyricLines.value.length) {
    micScorer.load(null, []); // 全部都放原唱：沒有要唱的
    scoreLoaded.value = false;
    return;
  }
  micScorer.load(
    doc,
    lines.map((l) => ({
      start: l.start + offset,
      end: l.end + offset,
      words: byWord ? l.words.filter((w) => w.text.trim()).map((w) => ({ start: w.start + offset, end: w.end + offset })) : undefined,
    })),
  );
  scoreLoaded.value = !!micScorer.scorer;
}

function showJudgement(line: LineResult) {
  judgement.value = { text: line.judgement, id: performance.now() };
  window.clearTimeout(judgeTimer);
  judgeTimer = window.setTimeout(() => (judgement.value = null), 1300);
}

/** 唱完（自然結束）：至少唱了一句才出結算畫面 */
function finishScore() {
  const song = scoring.value?.scorer;
  const item = activeItem.value;
  if (!song || !item || !store.session.score || song.heardLines === 0) return;
  const result = song.finish();
  scoreResult.value = { result, title: item.track.title, singer: item.requested_by ?? "", itemId: item.id };
  send({
    type: "score.result",
    item_id: item.id,
    score: result.score,
    pitch: result.pitch,
    rhythm: result.rhythm,
    stability: result.stability,
  });
  window.clearTimeout(resultTimer);
  resultTimer = window.setTimeout(() => (scoreResult.value = null), RESULT_MS);
}

/** 音高條的「現在」：唱的人聽到的歌曲時間 */
function scoreNow(): number {
  const engineInstance = engine.value;
  return engineInstance ? engineInstance.positionMs - engineInstance.pitchDelayMs : 0;
}

function handleLoad(msg: DisplayLoad) {
  const engineInstance = engine.value;
  if (!engineInstance || !msg.media_url) return;
  activeItem.value = msg.item;
  hasSung.value = true; // the session has started singing — QR moves aside
  mode.value = msg.item.karaoke_mode;
  guide.value = msg.item.guide_vocal;
  keepParts.value = msg.item.keep_parts ?? [];
  lyricOffsetMs.value = msg.item.track.lyrics_offset_ms;
  durationMs.value = msg.item.track.duration_ms;
  positionMs.value = 0;
  error.value = "";
  mvUrl.value = msg.mv_url ?? null;
  pitchUrl.value = msg.pitch_url ?? null;
  scoreLoaded.value = false;
  scoring.value?.load(null, []);
  engineInstance.load({
    mediaUrl: msg.media_url,
    stems: msg.stems,
    mode: msg.item.karaoke_mode,
    guide: msg.item.guide_vocal,
    volume: msg.volume ?? 0.9,
    pitch: msg.pitch ?? 0,
    autoplay: false, // start below: blocked audio waits for the unlock tap
  });
  countIns.value = []; // rebuilt when this track's lyrics arrive
  if (!audioBlocked.value) void engineInstance.play(); // intro starts now
  void loadLyrics(msg.lyrics_url);
}

function handleUpdate(msg: any) {
  const engineInstance = engine.value;
  if (!engineInstance || !msg.item_id || msg.item_id !== activeItem.value?.id) return;
  if (msg.mode) {
    mode.value = msg.mode;
    engineInstance.setMode(msg.mode);
  }
  if (typeof msg.guide === "number") {
    guide.value = msg.guide;
    engineInstance.setGuide(msg.guide);
  }
  if (Array.isArray(msg.keep_parts) && msg.keep_parts.join() !== keepParts.value.join()) {
    keepParts.value = msg.keep_parts as number[];
    void loadScore(); // 評分改跟著自己唱的句子
  }
  if (msg.stems !== undefined) engineInstance.setStems(msg.stems as Stems | null);
  if (msg.mv_url !== undefined) mvUrl.value = msg.mv_url as string | null;
  if (msg.pitch_url !== undefined && msg.pitch_url !== pitchUrl.value) {
    pitchUrl.value = msg.pitch_url as string | null; // AI 分離剛做完：這首開始有評分
    void loadScore();
  }
  if (typeof msg.lyrics_offset_ms === "number") lyricOffsetMs.value = msg.lyrics_offset_ms;
}

/** Song over or 卡歌: silence it and go back to the lobby until the next load. */
function clearStage() {
  activeItem.value = null;
  pitchUrl.value = null;
  scoreLoaded.value = false;
  scoring.value?.load(null, []);
  lyricLines.value = [];
  lyricTiming.value = "word";
  countIns.value = [];
}

function handleCommand(msg: any) {
  const engineInstance = engine.value;
  if (!engineInstance) return;
  if (msg.action === "stop") {
    engineInstance.pause();
    mvUrl.value = null;
    clearStage();
  } else if (msg.action === "play") {
    void engineInstance.play();
  } else if (msg.action === "pause") {
    engineInstance.pause();
  } else if (msg.action === "seek" && typeof msg.position_ms === "number") {
    engineInstance.seek(msg.position_ms);
  }
}

let reportTimer: number | undefined;
let rafHandle = 0;

/** MV 和歌差這麼多（秒）才跳轉；比這小的用播放速度慢慢追 */
const MV_SEEK_S = 1;
const MV_NUDGE_S = 0.08;
/** MV 對時不用每幀做：弱電視上動 currentTime／playbackRate 都要跟解碼器來回 */
const MV_SYNC_MS = 250;
let mvSyncedAt = 0;

/**
 * Every frame, but only refs whose value changes are written (a same-value
 * write does not re-render): the clock in 250ms steps, the count-in beat,
 * engine / mic states.  The moving pictures live in the child frame loops.
 */
function tick() {
  const engineInstance = engine.value;
  if (engineInstance) {
    positionMs.value = Math.floor(engineInstance.positionMs / 250) * 250;
    const now = displayNow();
    countIn.value = dotsLeft(countIns.value, now);
    countInAt.value =
      countIn.value > 0 ? countIns.value.find((ci) => now >= ci.at - ci.beat * 4 && now < ci.at)?.at ?? null : null;
    if (engineInstance.durationMs > 0) durationMs.value = engineInstance.durationMs;
    playing.value = engineInstance.playing;
    audioBlocked.value = !engineInstance.audioReady;
    pitchDelayMs.value = engineInstance.pitchDelayMs;
    // 對唱保留原唱：增益在升降 key 之前，所以用音訊本身的時間（不扣 pitchDelay）
    engineInstance.setOriginal(
      keepSpans.value.length > 0 && keptAt(keepSpans.value, engineInstance.positionMs - lyricOffsetMs.value),
    );
    const micScorer = scoring.value;
    if (micScorer) {
      micState.value = micScorer.state;
      micSilent.value = micScorer.silent;
    }
    if (mics.value) monitorState.value = mics.value.monitorState;
    const video = mvEl.value;
    const syncDue = performance.now() - mvSyncedAt >= MV_SYNC_MS && mvTest.value !== "no-sync";
    if (video && video.readyState > 0 && syncDue) {
      mvSyncedAt = performance.now();
      // 小落差用播放速度追（0.95／1.05 倍），大落差（換歌、拉進度）才跳轉：
      // 弱的電視解碼慢，一落後就跳轉會「跳轉 → 等關鍵幀 → 又落後」一直卡
      const target = Math.max(0, (engineInstance.positionMs - pitchDelayMs.value) / 1000);
      const drift = video.currentTime - target;
      if (!video.seeking) {
        const far = Math.abs(drift) > (engineInstance.playing ? MV_SEEK_S : 0.35);
        if (far) video.currentTime = target;
        const rate = far || Math.abs(drift) < MV_NUDGE_S ? 1 : drift > 0 ? 0.95 : 1.05;
        if (video.playbackRate !== rate) video.playbackRate = rate;
      }
      if (engineInstance.playing && video.paused) void video.play().catch(() => undefined);
      else if (!engineInstance.playing && !video.paused) video.pause();
    }
  }
  rafHandle = requestAnimationFrame(tick);
}

async function unlock() {
  const engineInstance = engine.value;
  if (!engineInstance) return;
  // 以下全部要在點擊當下同步呼叫（Safari 過了 await 就不算使用者手勢）
  const ready = engineInstance.unlock();
  mics.value?.unlock();
  const video = mvEl.value;
  if (video?.paused) {
    video.play().catch(() => undefined); // 省電模式下連靜音 MV 都要點過才准播
    video.pause();
  }
  // the tap is the user gesture — start the song that waited for it
  if (activeItem.value && !engineInstance.playing) void engineInstance.play();
  await ready;
  audioBlocked.value = !engineInstance.audioReady;
}

onMounted(() => {
  engine.value = new AudioEngine();
  const engineInstance = engine.value;
  // handy for local debugging / diagnostics on the KTV PC
  (window as unknown as Record<string, unknown>).__ktvEngine = engineInstance;
  (window as unknown as Record<string, unknown>).__ktvDisplay = { hasSung };
  engineInstance.onEnded = () => {
    finishScore();
    reportState(true);
    clearStage();
  };
  engineInstance.onError = (message) => (error.value = message);
  // a broken audio file must not stall the queue: show the error briefly,
  // then end this song exactly like a natural end (server checks the id)
  engineInstance.onMediaError = () => {
    const failedId = activeItem.value?.id;
    if (!failedId) return;
    window.setTimeout(() => {
      if (activeItem.value?.id === failedId) engineInstance.onEnded?.();
    }, MEDIA_ERROR_SKIP_MS);
  };
  engineInstance.onStateChange = () => (playing.value = engineInstance.playing);

  // 必須在 connect() 之前註冊，否則第一次 hello 就漏報能力
  onConnected(() => reportPitchCapable(AudioEngine.pitchSupported));
  onConnected(() => send({ type: "display.diag", kind: "compat", ...compat }));
  perfMeter.start();
  perfTimer = window.setInterval(() => {
    const stats = perfMeter.stats();
    if (!activeItem.value || !playing.value) return; // 大廳畫面不用報
    send({
      type: "display.diag",
      kind: "perf",
      ...stats,
      bg: showPV.value ? (showKaraoke.value ? "pv" : "pv-full") : showMV.value ? "mv" : "plain",
      beat_lights: store.session.beat_lights,
      score_bar: !!(scoreLoaded.value && store.session.score_bar),
    });
  }, PERF_REPORT_MS);
  if (mvTestOn) {
    mvTestMeter.start();
    mvTestTimer = window.setInterval(() => {
      const stats = mvTestMeter.stats();
      if (!showMV.value || !playing.value) return;
      send({ type: "display.diag", kind: "mvtest", variant: mvTest.value, ...stats, lite: lite.value });
      mvTestStep += 1;
      mvTest.value = MV_TESTS[mvTestStep % MV_TESTS.length];
      if (mvTest.value === "no-sync" && mvEl.value) mvEl.value.playbackRate = 1;
    }, MV_TEST_MS);
  }
  fxMeter.start();
  fxTimer = window.setInterval(() => {
    const stats = fxMeter.stats();
    if (autoLite.value || !activeItem.value || !playing.value) return;
    fxStrikes = stats.slow > FX_SLOW_LIMIT ? fxStrikes + 1 : 0;
    if (fxStrikes < 2) return;
    autoLite.value = true;
    send({ type: "display.diag", kind: "auto-lite", fps: stats.fps, slow: stats.slow });
  }, FX_WINDOW_MS);
  const micReceiver = new MicReceiver(send);
  mics.value = micReceiver;
  on("mic.signal", (msg) => micReceiver.handle(msg));
  watch(() => store.session.mic_monitor, (enabled) => void micReceiver.setMonitor(enabled), { immediate: true });
  watch(() => store.session.mic_volume, (percent) => micReceiver.setMonitorVolume(percent), { immediate: true });
  watch(() => store.session.mic_echo, (amount) => micReceiver.setEcho(amount), { immediate: true });

  const micScorer = new MicScorer(engineInstance);
  scoring.value = micScorer;
  micScorer.onLine = showJudgement;
  watch(
    () => store.session.score,
    (enabled) => {
      if (enabled) void micScorer.start();
      else micScorer.stop();
      void loadScore();
    },
    { immediate: true },
  );
  watch(() => store.session.score_offset_ms, (ms) => micScorer.setOffset(ms), { immediate: true });
  watch(() => store.session.pitch_semitones, (st) => micScorer.setKey(st), { immediate: true });

  connect("display", "display");

  on("display.prepare", (msg) => {
    if (typeof msg?.pitch === "number") engineInstance.setPitch(msg.pitch);
    engineInstance.resume();
  });
  on("display.load", handleLoad);
  on("score.posted", (msg) => {
    const posted = msg as ScorePosted;
    const shown = scoreResult.value;
    if (shown && shown.itemId === posted.item_id) scoreResult.value = { ...shown, best: posted.best };
  });
  on("display.update", handleUpdate);
  on("display.command", handleCommand);
  on("display.volume", (msg) => engineInstance.setVolume(msg.value));
  on("display.pitch", (msg) => engineInstance.setPitch(Number(msg.value ?? 0)));
  watch(
    () => store.session.ambience,
    (value) => engineInstance.setAmbience(value),
    { immediate: true },
  );
  on("display.reload_lyrics", (msg) => {
    if (videoOnly.value) return; // late network lyrics never override a video-only song
    void loadLyrics(msg.url);
  });
  on("track.progress", (msg) => {
    if (activeItem.value && msg.item_id === activeItem.value.id) {
      activeItem.value = { ...activeItem.value, progress_message: msg.message, status: msg.status };
    }
  });

  reportTimer = window.setInterval(() => reportState(), 1000);
  rafHandle = requestAnimationFrame(tick);
});

onBeforeUnmount(() => {
  if (reportTimer) clearInterval(reportTimer);
  if (rafHandle) cancelAnimationFrame(rafHandle);
  perfMeter.stop();
  window.clearInterval(perfTimer);
  fxMeter.stop();
  window.clearInterval(fxTimer);
  mvTestMeter.stop();
  window.clearInterval(mvTestTimer);
  mics.value?.dispose();
  scoring.value?.stop();
  window.clearTimeout(judgeTimer);
  window.clearTimeout(resultTimer);
});

/** 右上角的評分狀態：這首有沒有評分、麥克風有沒有聲音 */
const scoreChip = computed<{ text: string; cls: string } | null>(() => {
  if (!store.session.score || !activeItem.value || videoOnly.value) return null;
  if (!scoreLoaded.value) return { text: "尚未有評分", cls: "none" };
  if (micState.value === "denied") return { text: "評分：麥克風未允許", cls: "warn" };
  if (micState.value === "missing") return { text: "評分：找不到麥克風", cls: "warn" };
  if (micState.value !== "on") return { text: "評分：等待麥克風", cls: "none" };
  if (micSilent.value) return { text: "沒有收到麥克風聲音", cls: "warn" };
  return { text: "🎤 評分中", cls: "on" };
});

/** 麥克風外放出問題才提示（正常外放時聽得到，不用佔畫面） */
const monitorChip = computed<string | null>(() => {
  if (!store.session.mic_monitor) return null;
  if (monitorState.value === "denied") return "外放：麥克風未允許";
  if (monitorState.value === "missing") return "外放：找不到麥克風";
  return null;
});

const queuePreview = computed(() => store.session.queue.slice(0, 3));
const queueMore = computed(() => Math.max(0, store.session.queue.length - queuePreview.value.length));
/**
 * 大廳的 UP NEXT：下一首還在下載時伺服器已把它設成 current（還沒 display.load），
 * 清單第一首其實是下下首，所以先看 current。
 */
const lobbyNext = computed(() => store.session.current ?? store.session.queue[0] ?? null);
/** Guests read the mode in words, not the engine's enum. */
const modeLabel = computed(
  () => ({ ORIGINAL: "原唱", INSTANT: "伴唱", AI: "AI 伴唱" })[mode.value],
);
/** Pipeline chatter ("cache hit"…) stays off the TV unless the song is still on its way. */
const statusText = computed(() =>
  activeItem.value?.status === "PREPARING" ? activeItem.value.progress_message : "",
);
/**
 * Opening card: who's up and what they sing, for the first seconds of each
 * song — gone as soon as the count-in starts.  文字PV draws its own.
 */
const showIntro = computed(
  () =>
    !!activeItem.value && !showPV.value && countIn.value === 0 && positionMs.value < 7000,
);
</script>

<template>
  <div class="display" :class="[{ idle: !activeItem, lite }, mvTestOn && showMV ? `mvtest-${mvTest}` : '']">
    <TextPV
      v-if="showPV"
      :lines="lyricLines"
      :now="displayNow"
      :seed="activeItem?.track.id ?? ''"
      :title="title"
      :artist="activeItem?.track.artist ?? ''"
      :full="!showKaraoke"
      :mv-url="mvUrl"
      :analyser="engine?.analyser ?? null"
      :playing="playing"
      :lite="lite"
    />
    <video
      v-if="showMV"
      ref="mvEl"
      class="mv-bg"
      :src="mvUrl ?? undefined"
      muted
      playsinline
      preload="auto"
    />

    <!-- dark fades at the top and bottom edges keep the HUD readable over any MV -->
    <div v-if="activeItem" class="scrim top" />
    <div v-if="activeItem" class="scrim bottom" />
    <BeatLights
      v-if="activeItem && store.session.beat_lights"
      :analyser="engine?.analyser ?? null"
      :playing="playing"
      :lite="lite"
    />

    <!-- top band: one quiet line (song + who + state) over the pitch bar, QR on the right -->
    <header v-if="activeItem" class="hud">
      <div class="hud-main grow">
        <div class="hud-line">
          <div class="now ellipsis grow" :class="{ hidden: showIntro }">
            <b>{{ title }}</b>
            <span class="sub">{{ artist }}</span>
            <span v-if="activeItem.requested_by" class="by">♪ {{ activeItem.requested_by }}</span>
          </div>
          <div class="chips">
            <span class="chip" :class="mode.toLowerCase()">{{ modeLabel }}</span>
            <span v-if="guide > 0" class="chip">導唱 {{ Math.round(guide * 100) }}%</span>
            <span v-if="keepLabel" class="chip">原唱 {{ keepLabel }}</span>
            <span
              v-for="mic in mics?.singers ?? []"
              :key="mic.id"
              class="chip mic"
              :class="{ singing: mic.singing }"
              :style="{ '--level': mic.level }"
            >🎤 {{ mic.name }}</span>
            <!-- 評分正常時音高條自己會說，只有尚未有評分、麥克風出問題才顯示 -->
            <span v-if="scoreChip && scoreChip.cls !== 'on'" class="chip score" :class="scoreChip.cls">{{
              scoreChip.text
            }}</span>
            <span v-if="monitorChip" class="chip score warn">{{ monitorChip }}</span>
            <span v-if="!store.connected" class="chip offline">離線</span>
          </div>
        </div>
        <!-- 評分：音高條（遙控器可關）與每句的判定，像 DAM 一樣在畫面上方 -->
        <div v-if="scoring && scoreLoaded" class="pitch-wrap">
          <PitchBar v-if="store.session.score_bar" :scorer="scoring" :now-ms="scoreNow" :lite="lite" />
          <Transition name="judge">
            <div
              v-if="judgement"
              :key="judgement.id"
              class="judge"
              :class="['j-' + judgement.text.toLowerCase(), { floating: !store.session.score_bar }]"
            >{{ judgement.text }}</div>
          </Transition>
        </div>
      </div>
      <div v-if="hasSung" v-show="qrOk" class="qr-corner cut">
        <img :src="qrUrl" alt="點歌 QR" @load="qrOk = true" @error="qrOk = false" />
        <span>掃碼點歌</span>
      </div>
    </header>

    <div ref="lyricsEl" class="lyrics-area">
      <LyricStage
        v-if="lyricLines.length && showKaraoke"
        :lines="stageLines"
        :timing="lyricTiming"
        :japanese="lyricJapanese"
        :kana="jaAssist === 'both' || jaAssist === 'kana'"
        :sora="jaAssist === 'both' || jaAssist === 'sora'"
        :now="displayNow"
      />
      <div v-else-if="activeItem && !videoOnly && !lyricLines.length && !showIntro" class="no-lyrics">
        <div class="no-lyrics-title ellipsis">{{ title }}</div>
        <div class="no-lyrics-sub ellipsis">{{ artist }}</div>
        <div class="no-lyrics-note">這首歌沒有同步歌詞</div>
      </div>
      <!-- count-in: four dots right above the line about to be sung -->
      <div
        v-if="countIn > 0"
        class="countin"
        :class="countInSlot ? 'slot-' + countInSlot : 'centre'"
        :style="countInStyle"
      >
        <span v-for="n in 4" :key="n" class="dot" :class="{ off: n > countIn }" />
      </div>
    </div>

    <!-- opening card: the first seconds of every song -->
    <Transition name="intro">
      <div v-if="showIntro" class="intro">
        <span class="tag cut">NOW SINGING</span>
        <div class="intro-title">{{ title }}</div>
        <div class="intro-sub">
          {{ artist }}
          <span v-if="activeItem?.requested_by" class="by">♪ {{ activeItem.requested_by }}</span>
        </div>
      </div>
    </Transition>

    <footer v-if="activeItem" class="transport">
      <div class="row">
        <div v-if="queuePreview.length" class="upnext grow">
          <span class="tag cut">NEXT</span>
          <span v-for="(item, n) in queuePreview" :key="item.id" class="next-song" :class="{ first: n === 0 }">
            <b class="ellipsis">{{ item.track.title }}</b>
            <i v-if="item.requested_by">{{ item.requested_by }}</i>
          </span>
          <span v-if="queueMore" class="more">+{{ queueMore }}</span>
        </div>
        <div v-else class="upnext grow empty">歌單空了，掃右上 QR 繼續點歌</div>
        <span v-if="statusText" class="status">{{ statusText }}</span>
        <span v-if="error" class="err">{{ error }}</span>
        <span class="clock">{{ clockText }}</span>
      </div>
      <div class="bar">
        <div class="bar-fill" :style="{ width: percent + '%' }" />
      </div>
    </footer>


    <PerfHud v-if="showPerfHud" :compat="compat" :lite="lite" />

    <!-- nothing playing: invite screen (and the queue's next song, if any) -->
    <div v-if="!activeItem" class="lobby">
      <div class="logo">Local<b>KTV</b></div>
      <div v-if="store.session.room_name && store.session.room !== 'main'" class="lobby-room">
        {{ store.session.room_name }}
      </div>
      <div class="lobby-row">
        <div v-show="qrOk" class="qr-frame cut">
          <img class="qr-big" :src="qrUrl" alt="點歌 QR" @load="qrOk = true" @error="qrOk = false" />
        </div>
        <div class="lobby-text">
          <template v-if="lobbyNext">
            <span class="tag cut">UP NEXT</span>
            <div class="lobby-next ellipsis">{{ lobbyNext.track.title }}</div>
            <div class="lobby-sub ellipsis">
              {{ lobbyNext.track.artist }}
              <span v-if="lobbyNext.requested_by" class="by">♪ {{ lobbyNext.requested_by }}</span>
            </div>
            <div class="lobby-note">準備中，馬上開始</div>
          </template>
          <template v-else-if="qrOk">
            <ol class="steps">
              <li><b>01</b>手機掃描 QR</li>
              <li><b>02</b>搜尋歌名或歌手</li>
              <li><b>03</b>按「點歌」，拿起麥克風</li>
            </ol>
          </template>
          <div v-else class="lobby-note">正在準備點歌 QR…</div>
        </div>
      </div>
      <div v-if="mics?.singers.length" class="lobby-mics">
        <span
        v-for="mic in mics?.singers ?? []"
        :key="mic.id"
        class="chip cut mic"
        :class="{ singing: mic.singing }"
        :style="{ '--level': mic.level }"
      >🎤 {{ mic.name }}</span>
      </div>
      <div v-if="!store.connected" class="chip cut offline lobby-offline">離線：正在重新連線…</div>
    </div>

    <Transition name="intro">
      <ScoreResult
        v-if="scoreResult"
        :result="scoreResult.result"
        :title="scoreResult.title"
        :singer="scoreResult.singer"
        :best="scoreResult.best"
      />
    </Transition>

    <div v-if="audioBlocked" class="unlock" @click="unlock">
      <div class="unlock-box cut">
        <span class="tag cut">AUDIO</span>
        <div class="unlock-title">點一下畫面，啟用聲音</div>
        <div class="unlock-note">瀏覽器要求先點一下才允許播放</div>
      </div>
    </div>
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

/* same palette as /host and /remote, sized for a TV seen from the sofa */
.display {
  --void: #07060d;
  --cyan: #19f0ff;
  --magenta: #ff2fb4;
  --volt: #f2ff49;
  --danger: #ff4d6a;
  --fg: #eeedf7;
  --dim: #b7b2cf;
  --hud: "Chakra Petch", "Bahnschrift", "Segoe UI", system-ui, sans-serif;
  --cut: 10px;
  --edge: clamp(20px, 2.5vw, 48px);

  position: fixed;
  inset: 0;
  display: flex;
  flex-direction: column;
  color: var(--fg);
  background:
    linear-gradient(rgba(25, 240, 255, 0.03) 1px, transparent 1px) 0 0 / 100% 32px,
    radial-gradient(1100px 640px at 85% -10%, rgba(255, 47, 180, 0.14), transparent 60%),
    radial-gradient(900px 600px at -10% 110%, rgba(25, 240, 255, 0.12), transparent 60%),
    var(--void);
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
  min-width: 0;
}

.ellipsis {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* small HUD label (NOW SINGING / NEXT …) */
.tag {
  --cut: 5px;
  display: inline-block;
  padding: 5px 10px 4px;
  font: 700 clamp(12px, 0.9vw, 17px) / 1 var(--hud);
  letter-spacing: 0.18em;
  color: var(--void);
  background: var(--volt);
}

.by {
  margin-left: 0.6em;
  color: var(--cyan);
}

/* ?mvtest=1 的對照組（見 MV_TESTS） */
.display.mvtest-video-only > :not(.mv-bg),
.display.mvtest-no-video > .mv-bg {
  visibility: hidden;
}

/* background MV: behind everything, audio always comes from the engine */
.mv-bg {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  z-index: 0;
  pointer-events: none;
}

.scrim {
  position: absolute;
  left: 0;
  right: 0;
  z-index: 0;
  pointer-events: none;
}
.scrim.top {
  top: 0;
  height: 26vh;
  background: linear-gradient(rgba(7, 6, 13, 0.82), transparent);
}
.scrim.bottom {
  bottom: 0;
  height: 14vh;
  background: linear-gradient(transparent, rgba(7, 6, 13, 0.85));
}

.hud,
.lyrics-area,
.transport {
  position: relative;
  z-index: 1;
}

/* -- top band (DAM 式)：一行小字的歌名＋狀態，下面音高條，右邊 QR ---------- */
.hud {
  display: flex;
  align-items: flex-start;
  gap: 14px;
  padding: calc(var(--edge) * 0.6) var(--edge) 0;
  text-shadow: 0 2px 10px rgba(0, 0, 0, 0.7);
}

.hud-main {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.hud-line {
  display: flex;
  align-items: center;
  gap: 14px;
  min-height: 30px;
}

.now {
  border-left: 3px solid var(--cyan);
  padding-left: 12px;
  font-size: clamp(15px, 1.15vw, 22px);
  line-height: 1.2;
  transition: opacity 0.6s ease;
}
.now b {
  font-weight: 700;
}
.now .sub {
  margin-left: 0.8em;
  color: var(--dim);
}
/* the opening card already says it */
.now.hidden {
  opacity: 0;
}

.chips {
  display: flex;
  gap: 8px;
}

.chip {
  --cut: 6px;
  padding: 7px 14px 6px;
  font-size: clamp(14px, 1vw, 20px);
  font-weight: 700;
  background: rgba(7, 6, 13, 0.7);
  box-shadow: inset 0 0 0 1px rgba(25, 240, 255, 0.45);
  text-shadow: none;
}
/* 唱歌時的小標籤：跟歌名那行一樣小、一樣淡 */
.hud .chip {
  padding: 3px 10px;
  font-size: clamp(12px, 0.85vw, 16px);
  background: rgba(7, 6, 13, 0.55);
  box-shadow: inset 0 0 0 1px rgba(25, 240, 255, 0.3);
}
.chip.instant,
.chip.ai {
  color: var(--cyan);
}
.chip.original {
  color: var(--volt);
}
.chip.offline {
  color: #fff;
  background: var(--danger);
}
/* 手機麥克風：連上就有一顆，唱的時候亮起並跟著音量發光 */
.chip.mic {
  color: var(--dim);
  transition: color 0.15s, box-shadow 0.15s;
}
.chip.mic.singing {
  color: var(--magenta);
  box-shadow:
    inset 0 0 0 1px var(--magenta),
    0 0 calc(var(--level, 0) * 28px) var(--magenta);
}
/* 評分狀態：尚未有評分是灰的；出問題是紅框（評分中不顯示，音高條自己會說） */
.chip.score {
  color: var(--dim);
}
/* 省電：音量光暈不跟著麥克風每幀變（模糊陰影每次都要重畫），只留框線 */
.display.lite .chip.mic.singing {
  box-shadow: inset 0 0 0 1px var(--magenta);
}
.chip.score.warn {
  color: #fff;
  box-shadow: inset 0 0 0 1px var(--danger);
}
.pitch-wrap {
  position: relative;
}
.pitch-wrap :deep(.pitchbar) {
  height: clamp(64px, 11vh, 120px);
  margin: 0;
}
/* 判定字浮在音高條右端 */
.judge {
  position: absolute;
  right: 18px;
  top: 50%;
  margin-top: -0.5em;
  font: 700 clamp(26px, 2.6vw, 52px) / 1 var(--hud);
  letter-spacing: 0.12em;
  font-style: italic;
  text-shadow: 0 0 18px currentColor;
  pointer-events: none;
}
.judge.floating {
  top: 0;
  margin-top: 0;
}
.judge.j-perfect {
  color: var(--volt);
}
.judge.j-great {
  color: var(--cyan);
}
.judge.j-good {
  color: var(--fg);
}
.judge.j-miss {
  color: var(--danger);
  opacity: 0.8;
}
.judge-enter-active {
  transition: transform 0.25s cubic-bezier(0.2, 1.6, 0.4, 1), opacity 0.2s;
}
.judge-leave-active {
  transition: opacity 0.4s;
}
.judge-enter-from {
  transform: scale(0.6) translateY(10px);
  opacity: 0;
}
.judge-leave-to {
  opacity: 0;
}
.lobby-mics {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 10px;
}

/* singing has started: a small QR keeps the very top-right corner — anyone
   can still scan it mid-session */
.qr-corner {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 3px;
  padding: 6px 6px 4px;
  background: #fff;
  color: var(--void);
  font-size: clamp(11px, 0.75vw, 14px);
  font-weight: 700;
  text-shadow: none;
}
.qr-corner img {
  display: block;
  width: clamp(64px, 5.6vw, 108px);
  height: auto;
}

/* -- lyrics -------------------------------------------------------------- */
.lyrics-area {
  flex: 1;
  min-height: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 2vh 0 1vh; /* 歌詞貼底：上方留給 MV */
  overflow: hidden;
}

.lyrics-area > :deep(*) {
  width: 100%;
  height: 100%;
  min-width: 0;
  min-height: 0;
}

.no-lyrics {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  text-shadow: 0 3px 16px rgba(0, 0, 0, 0.8);
}
.no-lyrics-title {
  max-width: 80vw;
  font-size: clamp(36px, 4.6vw, 88px);
  font-weight: 800;
}
.no-lyrics-sub {
  max-width: 80vw;
  font-size: clamp(20px, 2vw, 36px);
  color: var(--dim);
}
.no-lyrics-note {
  margin-top: 18px;
  font-size: clamp(16px, 1.2vw, 22px);
  color: var(--dim);
}

/* -- opening card ---------------------------------------------------------- */
.intro {
  position: absolute;
  left: var(--edge);
  top: 26vh;
  z-index: 2;
  max-width: 70vw;
  padding: 22px 30px 24px;
  border-left: 6px solid var(--magenta);
  background: linear-gradient(90deg, rgba(7, 6, 13, 0.82), rgba(7, 6, 13, 0));
  pointer-events: none;
}
.intro-title {
  margin-top: 14px;
  font-size: clamp(40px, 4.4vw, 84px);
  font-weight: 800;
  line-height: 1.15;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.intro-sub {
  margin-top: 6px;
  font-size: clamp(20px, 1.8vw, 34px);
  color: var(--dim);
}
.intro-enter-active,
.intro-leave-active {
  transition: opacity 0.6s ease, transform 0.6s ease;
}
.intro-enter-from,
.intro-leave-to {
  opacity: 0;
  transform: translateX(-24px);
}

/* -- bottom: up next, clock, progress --------------------------------------- */
.transport {
  padding: 0 var(--edge);
  text-shadow: 0 2px 8px rgba(0, 0, 0, 0.8);
}

.transport .row {
  display: flex;
  align-items: center;
  gap: 18px;
  padding-bottom: 6px;
}

.upnext {
  display: flex;
  align-items: center;
  gap: 22px;
  overflow: hidden;
  font-size: clamp(14px, 1.05vw, 20px);
}
.upnext.empty {
  color: var(--dim);
}

.next-song {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
  max-width: 26vw;
  color: var(--dim);
}
.next-song b {
  font-weight: 600;
}
.next-song i {
  flex: none;
  font-style: normal;
  font-size: 0.8em;
  color: var(--cyan);
  opacity: 0.8;
}
.next-song.first {
  color: var(--fg);
}
.next-song.first b {
  font-weight: 700;
}
.more {
  flex: none;
  font: 700 0.9em var(--hud);
  color: var(--dim);
}

.clock {
  flex: none;
  font: 600 clamp(14px, 1.05vw, 20px) / 1 var(--hud);
  letter-spacing: 0.04em;
  font-variant-numeric: tabular-nums;
}

.status {
  flex: none;
  font-size: clamp(14px, 1vw, 20px);
  color: var(--cyan);
}

.err {
  flex: none;
  font-size: clamp(14px, 1vw, 20px);
  color: var(--danger);
}

.bar {
  height: 6px;
  background: rgba(255, 255, 255, 0.12);
  margin: 0 calc(-1 * var(--edge)); /* full-bleed to both screen edges */
}

.bar-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--cyan), var(--magenta));
  box-shadow: 0 0 12px var(--magenta);
  transition: width 0.4s linear;
}

/* four circles counting down the last beats before the vocals start, placed
   over the start of the line about to be sung (slot geometry mirrors
   LyricStage: same box as the stage, i.e. inside .lyrics-area's padding).
   Drawn like the karaoke type itself: sized to the lyrics, sung colour with
   a white rim and a black outline. */
.countin {
  --size: calc(0.45 * clamp(32px, 4.6vw, 82px));
  position: absolute;
  display: flex;
  gap: calc(var(--size) * 0.55);
  z-index: 2;
  pointer-events: none;
}
.countin.centre {
  inset: 2vh 0 1vh;
  align-items: center;
  justify-content: center;
}
.countin.slot-a,
.countin.slot-b {
  width: auto;
  height: auto;
}
/* slot a: stacked right above the slot-b line (LyricStage .slot-a, solo song) */
.countin.slot-a {
  left: 6vw;
  bottom: calc(1vh + 1.5vh + 2.95 * clamp(32px, 4.6vw, 82px) + 10px);
}
/* slot b: the line sits on the bottom edge, one karaoke line tall */
.countin.slot-b {
  right: 6vw;
  bottom: calc(1vh + 1.5vh + 1.3 * clamp(32px, 4.6vw, 82px) + 10px);
}

.dot {
  width: var(--size);
  height: var(--size);
  border-radius: 50%;
  background: var(--dot, var(--blue));
  border: calc(var(--size) * 0.14) solid #fff;
  box-shadow: 0 0 0 calc(var(--size) * 0.1) #000;
  transition: transform 0.3s ease, opacity 0.3s ease;
}

.dot.off {
  transform: scale(0.2);
  opacity: 0;
}

/* -- lobby: nothing playing ------------------------------------------------- */
.lobby {
  position: absolute;
  inset: 0;
  z-index: 2;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6vh;
}

.logo {
  font: 700 clamp(48px, 5vw, 96px) / 1 var(--hud);
  letter-spacing: 0.06em;
  text-shadow: 0 0 30px rgba(255, 47, 180, 0.35);
}
.logo b {
  color: var(--cyan);
  text-shadow: 0 0 30px rgba(25, 240, 255, 0.55);
}

.lobby-room {
  margin-top: -4vh;
  font: 700 clamp(22px, 2.4vw, 44px) / 1 var(--hud);
  letter-spacing: 0.12em;
  color: var(--cyan);
}

.lobby-row {
  display: flex;
  align-items: center;
  gap: 5vw;
  max-width: 88vw;
}

.qr-frame {
  --cut: 18px;
  flex: none;
  padding: 18px;
  background: #fff;
  box-shadow: 0 0 60px rgba(25, 240, 255, 0.3);
}

.qr-big {
  display: block;
  width: clamp(240px, 36vh, 420px);
  height: auto;
}

.lobby-text {
  min-width: 0;
}

.steps {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 3.2vh;
  font-size: clamp(24px, 2.3vw, 44px);
  font-weight: 700;
}
.steps b {
  display: inline-block;
  width: 2.4em;
  font: 700 0.9em var(--hud);
  color: var(--cyan);
}

.lobby-next {
  margin-top: 18px;
  max-width: 48vw;
  font-size: clamp(36px, 3.8vw, 72px);
  font-weight: 800;
}
.lobby-sub {
  max-width: 48vw;
  font-size: clamp(20px, 1.8vw, 34px);
  color: var(--dim);
}
.lobby-note {
  margin-top: 20px;
  font-size: clamp(18px, 1.5vw, 28px);
  color: var(--dim);
}

.lobby-offline {
  font-size: clamp(16px, 1.2vw, 22px);
}

/* -- audio unlock ------------------------------------------------------------ */
.unlock {
  position: fixed;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(7, 6, 13, 0.8);
  z-index: 50;
  cursor: pointer;
}

.unlock-box {
  --cut: 16px;
  padding: 36px 56px 40px;
  text-align: center;
  background: rgba(17, 15, 28, 0.95);
  box-shadow: inset 0 0 0 2px var(--cyan);
}

.unlock-title {
  margin-top: 18px;
  font-size: clamp(28px, 2.6vw, 48px);
  font-weight: 800;
}

.unlock-note {
  margin-top: 8px;
  font-size: clamp(16px, 1.2vw, 22px);
  color: var(--dim);
}

</style>
