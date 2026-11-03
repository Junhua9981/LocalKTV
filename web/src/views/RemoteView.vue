<script setup lang="ts">
/**
 * /remote — guest phone, cyberpunk console.
 *
 * Three tabs: 點歌（搜尋 / 熱門榜 / YouTube）、清單（全部 / 我點的）and
 * 麥克風（手機收音，WebRTC 直連電視；見 audio/mic.ts）.
 * "Now playing" docks above the tab bar on every screen; tapping it opens a
 * sheet with what changes per song (卡歌、音量、導唱、升降 Key、對唱).  The
 * room-wide settings that are set once (畫面、氣氛、外放、評分…) live on their
 * own 包廂設定 sheet behind the header's gear, reachable with nothing playing.
 * Styles are scoped to this view so /host keeps the shared look from style.css.
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { MicSender, micSupported } from "../audio/mic";
import RemoteTour, { type TourStep } from "../components/RemoteTour.vue";
import {
  chartIndex,
  connect,
  coverUrl,
  fetchChart,
  fetchCovers,
  hideImg,
  fetchSongScores,
  on,
  onConnected,
  playback,
  queueAdd,
  queueBump,
  queueMove,
  queueRemove,
  reportSong,
  search,
  send,
  setGuide,
  setKeepParts,
  setPitch,
  setVisual,
  setVolume,
  store,
  trackCover,
} from "../composables/useKtv";
import { fmt, useProgress } from "../composables/useProgress";
import { url } from "../base";
import { duetParts, type DuetPart } from "../lib/duet";
import { fetchLyrics } from "../lib/lyrics";
import type { ScorePosted, SongScore } from "../types";
import type {
  Ambience,
  BgMode,
  FxMode,
  JaAssist,
  Chart,
  ChartSong,
  ChartSource,
  ItemStatus,
  KaraokeMode,
  QueueItem,
  Track,
} from "../types";

// -- tabs ---------------------------------------------------------------------
const view = ref<"pick" | "queue" | "mic">("pick");
const sheetOpen = ref(false);

// 正在播放 sheet: opening it pushes a history entry, so the phone's back
// key / back gesture closes the sheet instead of leaving the page
function openSheet() {
  if (sheetOpen.value) return;
  sheetOpen.value = true;
  // keep vue-router's own state fields so its position bookkeeping is unchanged
  history.pushState({ ...history.state, ktvSheet: true }, "");
}

function closeSheet() {
  if (!sheetOpen.value) return;
  sheetOpen.value = false;
  if (history.state?.ktvSheet) history.back();
}

// 包廂設定 stacks over the now-playing sheet the same way: back closes only it
const settingsOpen = ref(false);
function openSettings() {
  if (settingsOpen.value) return;
  settingsOpen.value = true;
  history.pushState({ ...history.state, ktvSettings: true }, "");
}

function closeSettings() {
  if (!settingsOpen.value) return;
  settingsOpen.value = false;
  setRow.value = "";
  if (history.state?.ktvSettings) history.back();
}

function onPopState() {
  if (settingsOpen.value && !history.state?.ktvSettings) {
    settingsOpen.value = false;
    setRow.value = "";
  }
  if (sheetOpen.value && !history.state?.ktvSheet) sheetOpen.value = false;
}

// pull the sheet down from its top to close it
const sheetEl = ref<HTMLElement | null>(null);
const sheetDrag = ref(0);
let dragStartY: number | null = null;
function onSheetTouchStart(event: TouchEvent) {
  const target = event.target as HTMLElement;
  if (target.closest("input") || (sheetEl.value?.scrollTop ?? 0) > 0) return;
  dragStartY = event.touches[0].clientY;
}
function onSheetTouchMove(event: TouchEvent) {
  if (dragStartY === null) return;
  sheetDrag.value = Math.max(0, event.touches[0].clientY - dragStartY);
}
function onSheetTouchEnd() {
  if (dragStartY === null) return;
  dragStartY = null;
  if (sheetDrag.value > 90) closeSheet();
  sheetDrag.value = 0;
}

// -- identity -----------------------------------------------------------------
const name = ref(localStorage.getItem("ktv-name") ?? "");
const editingName = ref(false);
const nameDraft = ref("");

function ensureName(): string {
  if (!name.value) {
    name.value = `Guest-${Math.random().toString(36).slice(2, 6)}`;
    localStorage.setItem("ktv-name", name.value);
  }
  return name.value;
}

function startEditName() {
  nameDraft.value = name.value;
  editingName.value = true;
}

function saveName() {
  const value = nameDraft.value.trim().slice(0, 16);
  if (value) {
    name.value = value;
    localStorage.setItem("ktv-name", value);
  }
  editingName.value = false;
}

// -- 新手教學 -----------------------------------------------------------------
const TOUR_KEY = "ktv-tour-v1";
const TOUR_STEPS: TourStep[] = [
  {
    target: "search",
    title: "搜尋點歌",
    body: "輸入歌名或歌手，按歌曲右邊的＋就點好了。不知道唱什麼可以看「熱門榜」。",
  },
  {
    target: "queue",
    title: "待唱清單",
    body: "看自己排第幾首。點任何一首都能往前、往後或插到下一首；自己點的歌也能取消。",
  },
  {
    target: "now",
    title: "正在播放",
    body: "按「卡歌」換下一首。點一下這條打開控制頁：音量、導唱、升降 Key。",
  },
  {
    target: "now",
    title: "唱歌評分",
    body: "電視會聽電腦的麥克風幫你打分，唱完出分數，還有本曲高分。新點的歌要先在伺服器做完 AI 人聲分離才能評分，通常點過幾天內就會開放，還沒好的歌電視上會寫「尚未有評分」。",
  },
  {
    target: "mic",
    title: "手機當麥克風",
    body: "開麥後聲音從電視喇叭出來。手機和電視要連同一個 Wi‑Fi。",
  },
  {
    target: "settings",
    title: "包廂設定",
    body: "電視畫面、氣氛、麥克風外放、評分這些全場共用的設定都在這裡，沒有歌在播也能改。",
  },
  {
    target: "name",
    title: "你的暱稱",
    body: "點這裡改名字，清單上會顯示是誰點的。右邊的「?」可以再看一次教學。",
  },
];
const tourOpen = ref(false);

function startTour() {
  closeSettings();
  closeSheet();
  view.value = "pick";
  seg.value = "main";
  tourOpen.value = true;
}

function endTour() {
  tourOpen.value = false;
  try {
    localStorage.setItem(TOUR_KEY, "1");
  } catch {
    // private mode: the tour just shows again next visit
  }
}

function tourSeen(): boolean {
  try {
    return localStorage.getItem(TOUR_KEY) === "1";
  } catch {
    return false;
  }
}

// -- toast --------------------------------------------------------------------
const toastText = ref("");
let toastTimer: number | undefined;
function toast(message: string) {
  toastText.value = message;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => (toastText.value = ""), 1800);
}

// -- search -------------------------------------------------------------------
const seg = ref<"main" | "charts" | "youtube">("main");
const query = ref("");
const results = ref<Track[]>([]);
const loading = ref(false);
// 載入更多: the next page of every platform, appended below
const page = ref(1);
const moreLoading = ref(false);
const hasMore = ref(false);
const error = ref("");
/** ids (search) or "title artist" keys (charts) requested in the last moment:
 *  the ✓ shows briefly and swallows double taps, then the song can be requested again */
const added = ref<Set<string>>(new Set());
const ADDED_MS = 1500;
function markAdded(key: string) {
  added.value = new Set(added.value).add(key);
  window.setTimeout(() => {
    const next = new Set(added.value);
    next.delete(key);
    added.value = next;
  }, ADDED_MS);
}
/** track ids playing or waiting in the queue, for the 清單中 tag */
const queuedIds = computed(() => {
  const ids = new Set(store.session.queue.map((item) => item.track.id));
  if (store.session.current) ids.add(store.session.current.track.id);
  return ids;
});
const pending = ref<Set<string>>(new Set());

// this page's id for the server: its newer search drops its older ones
const clientId = Math.random().toString(36).slice(2, 12);
let inflight: AbortController | undefined;
let debounce: number | undefined;

async function runSearch() {
  window.clearTimeout(debounce); // Enter during the pause: search once, not twice
  const q = query.value.trim();
  if (!q || seg.value === "charts") return;
  inflight?.abort(); // an older answer must not land over this one
  const ctrl = (inflight = new AbortController());
  loading.value = true;
  moreLoading.value = false;
  error.value = "";
  page.value = 1;
  hasMore.value = false;
  try {
    results.value = await search(
      q,
      seg.value === "youtube" ? "youtube" : "main",
      ctrl.signal,
      clientId,
    );
    hasMore.value = seg.value === "main" && results.value.length > 0;
  } catch (err) {
    if (ctrl.signal.aborted) return;
    error.value = err instanceof Error ? err.message : String(err);
    results.value = [];
  } finally {
    if (inflight === ctrl) loading.value = false;
  }
}

/** a song already listed (same id, or same title + artist) is not repeated */
function songKey(track: Track): string {
  return `${track.title}\u0000${track.artist}`.toLowerCase().replace(/\s+/g, "");
}

async function loadMore() {
  const q = query.value.trim();
  if (!q || seg.value !== "main" || moreLoading.value || loading.value) return;
  inflight?.abort();
  const ctrl = (inflight = new AbortController());
  moreLoading.value = true;
  error.value = "";
  const next = page.value + 1;
  try {
    const found: Track[] = await search(q, "main", ctrl.signal, clientId, next);
    const ids = new Set(results.value.map((t) => t.id));
    const songs = new Set(results.value.map(songKey));
    const fresh = found.filter((t) => !ids.has(t.id) && !songs.has(songKey(t)));
    results.value = [...results.value, ...fresh];
    page.value = next;
    hasMore.value = found.length > 0;
  } catch (err) {
    if (ctrl.signal.aborted) return;
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    if (inflight === ctrl) moreLoading.value = false;
  }
}

// typing searches on its own after a pause (Enter searches at once)
watch(query, (value) => {
  window.clearTimeout(debounce);
  if (!value.trim()) {
    inflight?.abort();
    results.value = [];
    hasMore.value = false;
    error.value = "";
    loading.value = false;
    return;
  }
  debounce = window.setTimeout(() => void runSearch(), 800);
});

function clearQuery() {
  query.value = "";
}

// 導唱 the guest last chose also applies to the songs they request later
const guidePct = ref(Number(localStorage.getItem("ktv-guide") ?? 0));
const defaultGuidePct = ref(guidePct.value);

function enqueue(track: Track, again = false, via = seg.value === "main" ? "search" : seg.value) {
  queueAdd({
    track,
    requested_by: ensureName(),
    // 統計: where the song came from; cid ties it to this phone's last search
    via,
    cid: clientId,
    // no karaoke_mode: the server auto-picks AI when available, else INSTANT
    guide_vocal: defaultGuidePct.value / 100,
  });
  // the snapshot lands right after; the new song is the last one in the queue
  const position = store.session.queue.length + 1;
  toast(`${again ? "再點一次" : "已點"}「${track.title}」，排在第 ${position} 首`);
}

function request(track: Track) {
  if (added.value.has(track.id)) return;
  const again = queuedIds.value.has(track.id);
  enqueue(track, again);
  markAdded(track.id);
}

// aggregate catalogue entries carry "<platform>_<id>" in source_id
const PLATFORM_NAMES: Record<string, string> = {
  netease: "網易",
  tencent: "QQ",
  kugou: "酷狗",
  kuwo: "酷我",
  migu: "咪咕",
};

function tagOf(track: Track): { cls: string; text: string } {
  if (track.cached) return { cls: "cache", text: "已下載" };
  if (track.video_only) return { cls: "yt", text: "影片" };
  // merged search row: every platform carrying this song, the played one first
  const names = [track.source_id, ...(track.alternates ?? [])]
    .map((id) => PLATFORM_NAMES[(id ?? "").split("_")[0]])
    .filter((name, i, all): name is string => !!name && all.indexOf(name) === i);
  return { cls: "agg", text: names.length ? names.join(" · ") : "線上" };
}

// -- charts -------------------------------------------------------------------
const chartSources = ref<ChartSource[]>([]);
const chartKey = ref("");
const chart = ref<Chart | null>(null);
const chartLoading = ref(false);
const chartError = ref("");
// every board of a site (分類 × 語別) arrives in one response: switching never hits the server.
// The picked 分類/語別 is kept when switching sites, as long as the other site has it.
const chartGroup = ref("hot");
const chartLang = ref("zh");
const chartGroups = computed(() => {
  const seen = new Map<string, string>();
  for (const b of chart.value?.boards ?? []) if (!seen.has(b.group)) seen.set(b.group, b.group_label);
  return [...seen].map(([key, label]) => ({ key, label }));
});
const chartLangs = computed(() => (chart.value?.boards ?? []).filter((b) => b.group === chartGroup.value));
const chartBoard = computed(
  () => chartLangs.value.find((b) => b.lang === chartLang.value) ?? chartLangs.value[0],
);
const chartSongs = computed(() => chartBoard.value?.songs ?? []);
const chartTitle = computed(() => {
  const b = chartBoard.value;
  if (!chart.value || !b) return "熱門榜";
  return `${chart.value.label} ${b.group_label}${b.lang === "all" ? "" : ` · ${b.label}`}`;
});

/** Switch 分類; the 語別 stays if that 分類 has it, otherwise its first one. */
function pickChartGroup(group: string) {
  const boards = chart.value?.boards ?? [];
  if (!boards.some((b) => b.group === group)) group = boards[0]?.group ?? group;
  const inGroup = boards.filter((b) => b.group === group);
  if (!inGroup.length) return;
  chartGroup.value = group;
  if (!inGroup.some((b) => b.lang === chartLang.value)) chartLang.value = inGroup[0].lang;
}

function chartKeyOf(song: ChartSong): string {
  return `${song.title} ${song.artist}`.trim();
}

async function loadChart(key: string) {
  if (!key) return;
  chartKey.value = key;
  chartLoading.value = true;
  chartError.value = "";
  try {
    chart.value = await fetchChart(key);
    pickChartGroup(chartGroup.value);
  } catch (err) {
    chart.value = null;
    chartError.value = err instanceof Error ? err.message : String(err);
  } finally {
    chartLoading.value = false;
  }
}

// -- 熱門榜封面：看得到的列才問伺服器，還在查的每 3 秒再問一次 --------------
const COVER_BATCH = 30;
const COVER_POLL_MS = 3000;
const COVER_POLL_MAX = 40; // 約 2 分鐘還沒查完就算了，下次打開再問
/** "歌名\t歌手" -> 圖檔名，"" = 查無 */
const covers = ref(new Map<string, string>());
const coverBroken = ref(new Set<string>());
const coverWant = new Map<string, ChartSong>();
const coverRows = new WeakMap<Element, ChartSong>();
let coverTimer = 0;
let coverPolls = 0;
let coverBusy = false;

function coverKeyOf(song: ChartSong): string {
  return `${song.title}\t${song.artist}`;
}
function coverOf(song: ChartSong): string {
  const key = coverKeyOf(song);
  const name = covers.value.get(key);
  return name && !coverBroken.value.has(key) ? coverUrl(name) : "";
}
function onCoverError(song: ChartSong) {
  coverBroken.value = new Set(coverBroken.value).add(coverKeyOf(song));
}
const coverObserver =
  typeof IntersectionObserver === "undefined"
    ? null
    : new IntersectionObserver(
        (entries) => {
          let added = false;
          for (const entry of entries) {
            if (!entry.isIntersecting) continue;
            const song = coverRows.get(entry.target);
            coverObserver?.unobserve(entry.target);
            if (!song || covers.value.has(coverKeyOf(song))) continue;
            coverWant.set(coverKeyOf(song), song);
            added = true;
          }
          if (added) {
            coverPolls = 0;
            scheduleCovers(250);
          }
        },
        { rootMargin: "300px 0px" },
      );
function watchCoverRow(el: unknown, song: ChartSong) {
  if (!(el instanceof Element) || !coverObserver || covers.value.has(coverKeyOf(song))) return;
  coverRows.set(el, song);
  coverObserver.observe(el);
}
function scheduleCovers(ms: number) {
  window.clearTimeout(coverTimer);
  coverTimer = window.setTimeout(askCovers, ms);
}
async function askCovers() {
  if (coverBusy || !coverWant.size || seg.value !== "charts" || document.hidden) return;
  coverBusy = true;
  const batch = [...coverWant.values()].slice(0, COVER_BATCH);
  try {
    const answers = await fetchCovers(batch);
    const next = new Map(covers.value);
    batch.forEach((song, i) => {
      const name = answers[i];
      if (name === null || name === undefined) return;
      next.set(coverKeyOf(song), name);
      coverWant.delete(coverKeyOf(song));
    });
    covers.value = next;
  } catch {
    // 封面只是裝飾：失敗就維持首字方塊，等下一輪
  } finally {
    coverBusy = false;
  }
  if (!coverWant.size) return;
  if (++coverPolls < COVER_POLL_MAX) scheduleCovers(coverWant.size > COVER_BATCH ? 250 : COVER_POLL_MS);
}
/** 換榜單就清掉上一份還沒問完的 */
watch(chartSongs, () => {
  coverWant.clear();
  coverPolls = 0;
});
onBeforeUnmount(() => {
  window.clearTimeout(coverTimer);
  coverObserver?.disconnect();
});

/** Charts load the first time the tab opens (server side it is a monthly cache). */
async function ensureCharts() {
  if (!chartSources.value.length) {
    try {
      chartSources.value = await chartIndex();
    } catch (err) {
      chartError.value = err instanceof Error ? err.message : String(err);
      return;
    }
  }
  if (!chart.value && !chartLoading.value) await loadChart(chartSources.value[0]?.key ?? "");
}

/** A chart row is only title + artist: resolve it through search, then queue the top hit. */
async function requestChart(song: ChartSong) {
  const key = chartKeyOf(song);
  if (pending.value.has(key) || added.value.has(key)) return;
  pending.value = new Set(pending.value).add(key);
  chartError.value = "";
  try {
    let hits: Track[] = await search(key, "main");
    if (!hits.length) hits = await search(key, "youtube");
    if (!hits.length) {
      chartError.value = `找不到「${song.title}」可以播放的版本，請改用搜尋`;
      return;
    }
    enqueue(hits[0], queuedIds.value.has(hits[0].id), "charts");
    markAdded(key);
  } catch (err) {
    chartError.value = err instanceof Error ? err.message : String(err);
  } finally {
    const next = new Set(pending.value);
    next.delete(key);
    pending.value = next;
  }
}

watch(seg, (value) => {
  if (value === "charts") void ensureCharts();
  else if (query.value.trim()) void runSearch();
});

// -- queue --------------------------------------------------------------------
const queueFilter = ref<"all" | "mine">("all");
const queue = computed(() => store.session.queue);
const mine = computed(() => queue.value.filter((item) => item.requested_by === name.value));
const shownQueue = computed(() => (queueFilter.value === "mine" ? mine.value : queue.value));
const waitText = computed(() => {
  const first = queue.value.findIndex((item) => item.requested_by === name.value);
  if (first < 0) return "你還沒點歌";
  if (first === 0) return "你的歌是下一首";
  return `你的下一首：還有 ${first} 首`;
});

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

// 誰都能調整順序（取消仍只限自己點的歌）：點一首展開操作列
const queueOpen = ref("");
function toggleQueueItem(item: QueueItem) {
  queueOpen.value = queueOpen.value === item.id ? "" : item.id;
}
// the opened song left the queue (played, removed): fold its actions away
watch(queue, (items) => {
  if (queueOpen.value && !items.some((item) => item.id === queueOpen.value)) queueOpen.value = "";
});

function bump(item: QueueItem) {
  queueBump(item.id);
  toast(`「${item.track.title}」插到下一首`);
}

function moveBy(item: QueueItem, delta: number) {
  const index = queue.value.indexOf(item) + delta;
  if (index < 0 || index >= queue.value.length) return;
  queueMove(item.id, index);
  toast(`「${item.track.title}」移到第 ${index + 1} 首`);
}

function remove(item: QueueItem) {
  queueRemove(item.id);
  queueOpen.value = "";
  toast(`已取消「${item.track.title}」`);
}

// -- now playing --------------------------------------------------------------
const current = computed(() => store.session.current);
const progress = useProgress();
const playing = computed(() => store.session.playback.playing);
const nextUp = computed(() => queue.value[0]?.track.title ?? "");
// the song ended while the sheet was open: it hides, so drop its history entry too
watch(current, (value) => {
  if (!value) closeSheet();
});
/** 說明文字收起來，點標題旁的 i 才顯示 */
const tip = ref("");
function toggleTip(key: string) {
  tip.value = tip.value === key ? "" : key;
}
const MODE_TEXT: Record<KaraokeMode, string> = {
  ORIGINAL: "原唱",
  INSTANT: "即時去人聲",
  AI: "AI 去人聲",
};

// -- 回報這首歌有問題 ------------------------------------------------------------
const REPORT_KINDS = [
  { value: "lyrics", text: "歌詞錯" },
  { value: "track", text: "不是這首／版本錯" },
  { value: "duet", text: "對唱沒分顏色" },
  { value: "other", text: "其他" },
] as const;
const reportOpen = ref(false);
const reportKind = ref<(typeof REPORT_KINDS)[number]["value"] | "">("");
const reportNote = ref("");
const reportedIds = ref(new Set<string>());
watch(current, () => {
  reportOpen.value = false;
  reportKind.value = "";
  reportNote.value = "";
});
function sendReport() {
  const song = current.value;
  if (!song || !reportKind.value) return;
  if (!reportSong(song.track.id, reportKind.value, reportNote.value.trim())) {
    toast("連線中斷，稍後再試");
    return;
  }
  reportedIds.value = new Set([...reportedIds.value, song.track.id]);
  reportOpen.value = false;
  reportKind.value = "";
  reportNote.value = "";
  toast("已回報，謝謝！管理員會修正");
}

function togglePlay() {
  playback(playing.value ? "pause" : "play");
}

function skip() {
  playback("skip");
  closeSheet();
  toast("已卡歌，換下一首");
}

function onGuideInput(event: Event) {
  const percent = Number((event.target as HTMLInputElement).value);
  guidePct.value = percent;
  defaultGuidePct.value = percent;
  localStorage.setItem("ktv-guide", String(percent));
  setGuide(percent / 100);
}

watch(
  () => current.value?.guide_vocal,
  (value) => {
    if (typeof value === "number") guidePct.value = Math.round(value * 100);
  },
  { immediate: true },
);

// 對唱保留原唱：這首的聲部從歌詞來（電視用的同一份 /lyrics）
const duet = ref<DuetPart[]>([]);
const duetSolos = computed(() => duet.value.filter((p) => p.part > 0));
const duetHasChorus = computed(() => duet.value.some((p) => p.part === 0));
const keepParts = computed(() => current.value?.keep_parts ?? []);
const keepSolo = computed(() => keepParts.value.find((p) => p > 0) ?? null);
/** 合唱段也放原唱：還沒選人時記住偏好，預設開 */
const keepChorus = ref(localStorage.getItem("ktv-keep-chorus") !== "0");
watch(
  () => [current.value?.id, current.value?.track.id, current.value?.track.video_only] as const,
  async ([itemId, trackId, videoOnly]) => {
    duet.value = [];
    if (!itemId || !trackId || videoOnly) return;
    const doc = await fetchLyrics(url(`/lyrics/${encodeURIComponent(trackId)}`));
    if (current.value?.id === itemId) duet.value = doc ? duetParts(doc.lines) : [];
  },
  { immediate: true },
);
watch(keepParts, (parts) => {
  if (parts.some((p) => p > 0)) keepChorus.value = parts.includes(0);
});
function pickKeep(solo: number | null) {
  setKeepParts(solo === null ? [] : keepChorus.value && duetHasChorus.value ? [solo, 0] : [solo]);
}
function toggleKeepChorus() {
  keepChorus.value = !keepChorus.value;
  localStorage.setItem("ktv-keep-chorus", keepChorus.value ? "1" : "0");
  if (keepSolo.value !== null) pickKeep(keepSolo.value);
}

// -- 包廂設定 ---------------------------------------------------------------------
/** one row of the settings list is open at a time */
const setRow = ref("");
function toggleSetRow(key: string) {
  setRow.value = setRow.value === key ? "" : key;
}
function textOf<T>(options: { value: T; text: string }[], value: T): string {
  return options.find((o) => o.value === value)?.text ?? "";
}
/** Feather "settings" (MIT) */
const GEAR =
  "M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z";

// key shift is session-wide (like volume): host and every remote share one value
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

// 大螢幕畫面 — session-wide like the key: MV or 文字PV, karaoke lyrics on/off
const BG_MODES: { value: BgMode; text: string }[] = [
  { value: "auto", text: "自動" },
  { value: "mv", text: "原版 MV" },
  { value: "pv", text: "文字PV" },
];
const bgMode = computed(() => store.session.bg_mode);
const pvKaraoke = computed(() => store.session.pv_karaoke);
/** 畫面效果：弱的智慧電視跑不動文字PV 光暈、節拍燈光時用省電 */
const FX_MODES: { value: FxMode; text: string }[] = [
  { value: "auto", text: "自動" },
  { value: "full", text: "完整" },
  { value: "lite", text: "省電" },
];
const FX_HINT: Record<FxMode, string> = {
  auto: "電視播放時明顯掉幀，就自動改用省電效果。",
  full: "一律用完整效果。",
  lite: "關掉文字PV 的光暈與漣漪、節拍燈光只留邊框，舊電視比較順。",
};
const fxMode = computed(() => store.session.fx_mode);
/** 日文歌：漢字上的假名、歌詞下的中文空耳 */
const JA_ASSISTS: { value: JaAssist; text: string }[] = [
  { value: "both", text: "兩者" },
  { value: "kana", text: "假名" },
  { value: "sora", text: "空耳" },
  { value: "off", text: "關閉" },
];
const JA_HINT: Record<JaAssist, string> = {
  both: "日文歌的漢字上方標假名，歌詞下方加中文空耳。",
  kana: "日文歌的漢字上方標假名。",
  sora: "日文歌的歌詞下方加中文空耳。",
  off: "日文歌只顯示原本的歌詞。",
};
const jaAssist = computed(() => store.session.ja_assist ?? "both");
const AMBIENCES: { value: Ambience; text: string }[] = [
  { value: "clean", text: "原音" },
  { value: "room", text: "包廂" },
  { value: "concert", text: "演唱會" },
];
const ambience = computed(() => store.session.ambience);
const beatLights = computed(() => store.session.beat_lights);
// 評分：電視聽電腦的麥克風輸入；延遲是藍牙麥克風組＋喇叭的延遲，唱準了分數還低就調大
const scoreOn = computed(() => store.session.score);
const scoreBar = computed(() => store.session.score_bar);
const scoreOffset = computed(() => store.session.score_offset_ms);
const SCORE_OFFSET_STEP = 25;
/** 本曲高分：換歌時抓一次，有人唱完這首再更新 */
const songScores = ref<SongScore[]>([]);
async function loadSongScores() {
  const trackId = current.value?.track.id;
  songScores.value = trackId ? await fetchSongScores(trackId) : [];
}
watch(() => current.value?.track.id, () => void loadSongScores(), { immediate: true });
function onScorePosted(posted: ScorePosted) {
  const record = !posted.best || posted.score > posted.best.score;
  toast(`${posted.singer || "有人"}唱〈${posted.title}〉${posted.score} 分${record ? "，本曲新紀錄！" : ""}`);
  if (posted.track_id === current.value?.track.id) void loadSongScores();
}
// 麥克風外放：電視把電腦麥克風的聲音從喇叭放出來，加迴音；迴音也套用到手機麥克風
const micMonitor = computed(() => store.session.mic_monitor);
const micVolume = computed(() => store.session.mic_volume);
const micEcho = computed(() => store.session.mic_echo);
function onMicVolumeInput(event: Event) {
  setVisual({ mic_volume: Number((event.target as HTMLInputElement).value) });
}
function onMicEchoInput(event: Event) {
  setVisual({ mic_echo: Number((event.target as HTMLInputElement).value) });
}
function stepScoreOffset(dir: number) {
  const next = Math.max(0, Math.min(600, scoreOffset.value + dir * SCORE_OFFSET_STEP));
  setVisual({ score_offset_ms: next });
}
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

watch(
  () => store.session.pitch_semitones,
  (value) => {
    pitch.value = typeof value === "number" ? value : 0;
  },
  { immediate: true },
);

// volume is session-wide but not broadcast back: remember the last value locally
const volumePct = ref(Number(localStorage.getItem("ktv-volume") ?? 90));
function onVolumeInput(event: Event) {
  volumePct.value = Number((event.target as HTMLInputElement).value);
  localStorage.setItem("ktv-volume", String(volumePct.value));
  setVolume(volumePct.value / 100);
}

// -- tiles (first glyph on a hue derived from the title; 熱門榜有封面時蓋在上面) --
function hueOf(text: string): number {
  let h = 7;
  for (const ch of text) h = (h * 31 + (ch.codePointAt(0) ?? 0)) % 360;
  return h;
}
function glyphOf(text: string): string {
  return [...text.trim()][0] ?? "♪";
}

// -- 麥克風 -------------------------------------------------------------------
const mic = new MicSender(send);
const micState = mic.state;
const micAvailable = micSupported();
/** 傳輸方式、出聲方式、延遲拆解：多數人不用碰，收在「連線細節」 */
const micDetails = ref(false);
/** 短按＝開／關（唱整首不用一直按著），按住超過這麼久放開＝只在按住時收音。 */
const HOLD_MS = 350;
let pressAt = 0;
let pressWasLive = false;
function micDown() {
  if (micState.status !== "ready") return;
  pressAt = performance.now();
  pressWasLive = micState.live;
  if (!pressWasLive) mic.setLive(true);
}
function micUp() {
  if (!pressAt) return;
  const held = performance.now() - pressAt;
  pressAt = 0;
  if (pressWasLive || held > HOLD_MS) mic.setLive(false);
}
const micGainPct = computed(() => Math.round(micState.gain * 100));
function onMicGainInput(event: Event) {
  mic.setGain(Number((event.target as HTMLInputElement).value) / 100);
}
/** 電視回報的延遲拆解。不含電視機本身的畫質處理（量不到）。 */
const micLatency = computed(() => {
  const l = micState.latency;
  if (!l) return null;
  const known = l.output >= 0;
  return {
    total: l.capture + l.network + l.buffer + (known ? l.output : 0),
    known,
    parts: [
      ["收音", String(l.capture)],
      ["網路", String(l.network)],
      ["電視緩衝", String(l.buffer)],
      ["輸出", known ? String(l.output) : "?"],
    ] as const,
    direct: l.route === "host",
    mode: l.mode,
    fast: l.transport === "fast",
    underruns: l.underruns,
  };
});
const micLabel = computed(() => {
  if (micState.status === "connecting") return "連線中…";
  if (micState.status !== "ready") return "";
  return micState.live ? "收音中 · 點一下關閉" : "點一下開麥 · 或按住唱";
});

onBeforeUnmount(() => window.removeEventListener("popstate", onPopState));

onMounted(() => {
  window.addEventListener("popstate", onPopState);
  ensureName();
  if (!tourSeen()) startTour();
  on("mic.signal", (msg) => mic.handle(msg));
  on("score.posted", (msg) => onScorePosted(msg as ScorePosted));
  // 重連後伺服器給了新編號，電視那邊已經掛斷這支手機
  let first = true;
  onConnected(() => {
    if (!first) mic.reconnect();
    first = false;
  });
  connect("remote", name.value);
});
</script>

<template>
  <div class="cyber">
    <header class="top">
      <div class="logo">LOCAL<b>KTV</b></div>
      <span v-if="store.session.room_name && store.session.room !== 'main'" class="room-tag">
        {{ store.session.room_name }}
      </span>
      <form v-if="editingName" class="name-edit" @submit.prevent="saveName">
        <input
          id="remote-name"
          v-model="nameDraft"
          maxlength="16"
          aria-label="暱稱"
          autofocus
          @blur="saveName"
        />
      </form>
      <button v-else class="who" title="改暱稱" data-tour="name" @click="startEditName">
        <span class="dot" :class="{ on: store.connected }" />
        {{ name }}
      </button>
      <button class="help" title="包廂設定" aria-label="包廂設定" data-tour="settings" @click="openSettings">
        <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true"><path :d="GEAR" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" /><circle cx="12" cy="12" r="3" fill="none" stroke="currentColor" stroke-width="1.8" /></svg>
      </button>
      <button class="help" title="使用教學" aria-label="使用教學" @click="startTour">?</button>
    </header>

    <!-- ===================== 點歌 ===================== -->
    <main v-if="view === 'pick'" class="screen">
      <form v-if="seg !== 'charts'" class="search cut" data-tour="search" @submit.prevent="runSearch">
        <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="11" cy="11" r="7" fill="none" stroke="currentColor" stroke-width="2" />
          <path d="M16.5 16.5 21 21" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
        </svg>
        <input
          id="remote-search"
          v-model="query"
          type="search"
          enterkeyhint="search"
          autocomplete="off"
          :placeholder="seg === 'youtube' ? '搜尋 YouTube 影片' : '歌名、歌手'"
        />
        <button v-if="query" type="button" class="clear" aria-label="清除" @click="clearQuery">✕</button>
      </form>

      <div class="seg cut" role="tablist">
        <button :class="{ on: seg === 'main' }" role="tab" @click="seg = 'main'">曲庫</button>
        <button v-if="store.session.charts_available" :class="{ on: seg === 'charts' }" role="tab" @click="seg = 'charts'">熱門榜</button>
        <button :class="{ on: seg === 'youtube' }" role="tab" @click="seg = 'youtube'">YouTube</button>
      </div>

      <!-- ---------- 熱門榜 ---------- -->
      <template v-if="seg === 'charts'">
        <div class="chips">
          <button
            v-for="s in chartSources"
            :key="s.key"
            class="chip"
            :class="{ on: s.key === chartKey }"
            @click="loadChart(s.key)"
          >
            {{ s.label }}
          </button>
        </div>
        <div v-if="chartGroups.length > 1" class="chips">
          <button
            v-for="g in chartGroups"
            :key="g.key"
            class="chip"
            :class="{ on: g.key === chartBoard?.group }"
            @click="pickChartGroup(g.key)"
          >
            {{ g.label }}
          </button>
        </div>
        <div v-if="chartLangs.length > 1" class="chips">
          <button
            v-for="b in chartLangs"
            :key="b.key"
            class="chip sub"
            :class="{ on: b.key === chartBoard?.key }"
            @click="chartLang = b.lang"
          >
            {{ b.label }}
          </button>
        </div>
        <div class="label">
          <i>//</i> {{ chartTitle }}
          <span v-if="chartBoard?.period"> · {{ chartBoard.period }}</span>
          <span v-if="chartBoard?.stale"> · 顯示上次的榜單</span>
        </div>
        <div v-if="chartError" class="err">{{ chartError }}</div>
        <div v-if="chartLoading || (!chart && !chartError)" class="empty">榜單載入中…</div>
        <ul v-else-if="chartSongs.length" class="list">
          <li
            v-for="(song, i) in chartSongs"
            :key="`${chartKey}-${chartBoard?.key}-${song.rank}-${i}`"
            :ref="(el) => watchCoverRow(el, song)"
            class="row"
          >
            <span v-if="chartBoard?.ranked" class="rank" :class="{ podium: song.rank <= 3 }">{{ song.rank }}</span>
            <div class="tile cut" :style="{ '--h': hueOf(song.title) }">
              {{ glyphOf(song.title) }}
              <img v-if="coverOf(song)" :src="coverOf(song)" alt="" decoding="async" @error="onCoverError(song)" />
            </div>
            <div class="meta">
              <div class="t">{{ song.title }}</div>
              <div class="s">{{ song.artist }}<template v-if="chartBoard?.lang === 'all' && song.tag"> · {{ song.tag }}</template></div>
            </div>
            <button
              class="add cut"
              :class="{ done: added.has(chartKeyOf(song)), busy: pending.has(chartKeyOf(song)) }"
              :aria-label="`點 ${song.title}`"
              @click="requestChart(song)"
            >
              <svg v-if="added.has(chartKeyOf(song))" width="20" height="20" viewBox="0 0 24 24" aria-hidden="true">
                <path d="m5 12.5 4.5 4.5L19 7.5" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
              <span v-else-if="pending.has(chartKeyOf(song))" class="spin" aria-hidden="true" />
              <svg v-else width="20" height="20" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" />
              </svg>
            </button>
          </li>
        </ul>
        <div v-else-if="chart" class="empty">這個榜單目前沒有資料</div>
      </template>

      <!-- ---------- 曲庫 / YouTube ---------- -->
      <template v-else>
        <div v-if="error" class="err">{{ error }}</div>
        <template v-if="results.length">
          <div class="label"><i>//</i> 搜尋結果 · {{ results.length }} 首</div>
          <ul class="list">
            <li v-for="track in results" :key="track.id" class="row">
              <div class="tile cut" :style="{ '--h': hueOf(track.title) }">
                {{ glyphOf(track.title) }}
                <img v-if="trackCover(track)" :src="trackCover(track)" alt="" loading="lazy" decoding="async" @error="hideImg" />
              </div>
              <div class="meta">
                <div class="t">{{ track.title }}</div>
                <div class="s">
                  <span class="tag" :class="tagOf(track).cls">{{ tagOf(track).text }}</span>
                  <span v-if="queuedIds.has(track.id)" class="tag queued">清單中</span>
                  <span class="ellipsis">{{ track.artist }}</span>
                  <span v-if="track.duration_ms">· {{ fmt(track.duration_ms) }}</span>
                </div>
              </div>
              <button
                class="add cut"
                :class="{ done: added.has(track.id) }"
                :aria-label="`點 ${track.title}`"
                @click="request(track)"
              >
                <svg v-if="added.has(track.id)" width="20" height="20" viewBox="0 0 24 24" aria-hidden="true">
                  <path d="m5 12.5 4.5 4.5L19 7.5" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" />
                </svg>
                <svg v-else width="20" height="20" viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" />
                </svg>
              </button>
            </li>
          </ul>
          <button
            v-if="hasMore && seg === 'main'"
            class="more cut"
            :disabled="moreLoading"
            @click="loadMore"
          >
            {{ moreLoading ? "載入中…" : "載入更多" }}
          </button>
        </template>
        <div v-else-if="loading" class="empty">搜尋中…</div>
        <div v-else-if="query.trim() && !error" class="empty">找不到符合的歌曲，換個關鍵字試試</div>
        <div v-else-if="!error" class="empty">
          {{
            seg === "youtube"
              ? "搜尋 YouTube 的 KTV 影片，點了會直接播放影片，不顯示歌詞"
              : "輸入歌名或歌手。標示「已下載」的歌點了馬上就能唱"
          }}
        </div>
      </template>
    </main>

    <!-- ===================== 麥克風 ===================== -->
    <main v-else-if="view === 'mic'" class="screen mic-screen">
      <div class="label"><i>//</i> 手機麥克風</div>

      <template v-if="!micAvailable">
        <div class="ctrl cut">
          <strong>這個網址不能開麥克風</strong>
          <div class="hint">
            手機瀏覽器只在 HTTPS 網址允許使用麥克風。請改用管理員給的 https 網址或 QR 進來。
          </div>
        </div>
      </template>

      <template v-else-if="micState.status === 'off' || micState.status === 'error'">
        <div v-if="micState.error" class="ctrl cut mic-err">{{ micState.error }}</div>
        <button class="big-skip cut" @click="mic.start()">連接麥克風</button>
        <div class="hint">
          聲音從手機傳到電視喇叭。手機和電視連同一個 Wi‑Fi，別對著喇叭唱，以免嘯叫。
        </div>
        <div class="ctrl cut fold">
          <button class="set-row" :aria-expanded="micDetails" @click="micDetails = !micDetails">
            <span>連線細節</span>
            <span class="set-v">{{ micState.transport === "fast" ? "低延遲" : "穩定" }}</span>
            <i class="chev" :class="{ open: micDetails }" aria-hidden="true" />
          </button>
          <template v-if="micDetails">
            <div class="ctrl-h"><strong>傳輸方式</strong></div>
            <div class="seg route-seg" role="radiogroup" aria-label="傳輸方式">
              <button
                role="radio"
                :aria-checked="micState.transport === 'fast'"
                :class="{ on: micState.transport === 'fast' }"
                @click="mic.setTransport('fast')"
              >
                低延遲
              </button>
              <button
                role="radio"
                :aria-checked="micState.transport === 'stable'"
                :class="{ on: micState.transport === 'stable' }"
                @click="mic.setTransport('stable')"
              >
                穩定
              </button>
            </div>
            <div class="hint">
              {{ micState.transport === "fast"
                ? "低延遲：電視只留約 10–20ms 緩衝，Wi‑Fi 不穩時偶爾會斷音。"
                : "穩定：用瀏覽器內建的語音傳輸，不容易斷音，但電視緩衝常超過 100ms。" }}
            </div>
          </template>
        </div>
      </template>

      <template v-else>
        <button
          class="mic-btn"
          :class="{ live: micState.live, wait: micState.status !== 'ready' }"
          :disabled="micState.status !== 'ready'"
          :aria-pressed="micState.live"
          aria-label="麥克風開關"
          @pointerdown.prevent="micDown"
          @pointerup="micUp"
          @pointercancel="micUp"
          @pointerleave="micUp"
          @contextmenu.prevent
        >
          <svg width="64" height="64" viewBox="0 0 24 24" aria-hidden="true">
            <rect x="9" y="3" width="6" height="11" rx="3" fill="none" stroke="currentColor" stroke-width="1.6" />
            <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
          </svg>
          <span>{{ micState.live ? "ON AIR" : "MIC" }}</span>
        </button>
        <div class="mic-label">{{ micLabel }}</div>

        <div class="ctrl cut">
          <div class="ctrl-h"><strong>我的麥克風音量</strong><span class="ctrl-v">{{ micGainPct }}%</span></div>
          <input
            id="remote-mic-gain"
            type="range"
            min="0"
            max="150"
            step="5"
            :value="micGainPct"
            aria-label="我的麥克風音量"
            @input="onMicGainInput"
          />
          <div class="hint">一嘯叫就先調低音量或離喇叭遠一點。</div>
        </div>

        <div class="ctrl cut fold">
          <button class="set-row" :aria-expanded="micDetails" @click="micDetails = !micDetails">
            <span>{{ micLatency ? `延遲 約 ${micLatency.total} ms${micLatency.known ? "" : " + 輸出"}` : "連線細節" }}</span>
            <span class="set-v">{{ micState.transport === "fast" ? "低延遲" : "穩定" }}</span>
            <i class="chev" :class="{ open: micDetails }" aria-hidden="true" />
          </button>
          <template v-if="micDetails">
            <template v-if="micLatency">
              <div class="lat-parts">
                <span v-for="[label, ms] in micLatency.parts" :key="label"><b>{{ ms }}</b>{{ label }}</span>
              </div>
              <div v-if="micLatency.fast" class="ctrl-h">
                <span>斷音次數</span><span class="ctrl-v">{{ micLatency.underruns }}</span>
              </div>
              <div class="hint">
                {{ micLatency.direct ? "手機與電視在同一個網路直連。" : "沒有走區網直連（繞到外網），確認手機和電視連同一個 Wi‑Fi。" }}
                實際會再多一些：手機硬體收音和電視機的畫質處理量不到，電視請開「遊戲模式」。
              </div>
            </template>
            <div class="ctrl-h"><strong>傳輸方式</strong></div>
            <div class="seg route-seg" role="radiogroup" aria-label="傳輸方式">
              <button
                role="radio"
                :aria-checked="micState.transport === 'fast'"
                :class="{ on: micState.transport === 'fast' }"
                @click="mic.setTransport('fast')"
              >
                低延遲
              </button>
              <button
                role="radio"
                :aria-checked="micState.transport === 'stable'"
                :class="{ on: micState.transport === 'stable' }"
                @click="mic.setTransport('stable')"
              >
                穩定
              </button>
            </div>
            <div class="hint">
              {{ micState.transport === "fast"
                ? "低延遲：電視只留約 10–20ms 緩衝，Wi‑Fi 不穩時偶爾會斷音。"
                : "穩定：用瀏覽器內建的語音傳輸，不容易斷音，但電視緩衝常超過 100ms。" }}
            </div>
            <template v-if="micLatency && !micLatency.fast">
              <div class="ctrl-h"><strong>電視出聲方式</strong></div>
              <div class="seg route-seg" role="radiogroup" aria-label="電視出聲方式">
                <button
                  role="radio"
                  :aria-checked="micLatency.mode === 'mix'"
                  :class="{ on: micLatency.mode === 'mix' }"
                  @click="mic.setRoute('mix')"
                >
                  混音器
                </button>
                <button
                  role="radio"
                  :aria-checked="micLatency.mode === 'call'"
                  :class="{ on: micLatency.mode === 'call' }"
                  @click="mic.setRoute('call')"
                >
                  通話輸出
                </button>
              </div>
              <div class="hint">
                {{ micLatency.mode === "call"
                  ? "通話輸出可能比較快，但沒有防爆音與低頻濾波，量不到輸出延遲，請用耳朵比較。音量最大 100%。"
                  : "混音器有防爆音與低頻濾波。兩種都試唱看看，選聽起來比較不拖的。" }}
                全場共用。
              </div>
            </template>
          </template>
        </div>
        <button class="mic-off" @click="mic.stop()">中斷麥克風</button>
      </template>
    </main>

    <main v-else class="screen">
      <div class="q-head">
        <div class="label"><i>//</i> 待唱清單</div>
        <span class="grow" />
        <span class="wait">{{ waitText }}</span>
      </div>
      <div class="chips">
        <button class="chip" :class="{ on: queueFilter === 'all' }" @click="queueFilter = 'all'">
          全部 {{ queue.length }}
        </button>
        <button class="chip" :class="{ on: queueFilter === 'mine' }" @click="queueFilter = 'mine'">
          我點的 {{ mine.length }}
        </button>
        <span v-if="queue.length > 1" class="hint q-tip">點一首歌可以調整順序</span>
      </div>
      <ul v-if="shownQueue.length" class="list">
        <li
          v-for="item in shownQueue"
          :key="item.id"
          class="q-item"
          :class="{ mine: item.requested_by === name, open: queueOpen === item.id }"
        >
          <button class="row q-row" :aria-expanded="queueOpen === item.id" @click="toggleQueueItem(item)">
            <span class="pos">{{ queue.indexOf(item) + 1 }}</span>
            <div class="tile cut" :style="{ '--h': hueOf(item.track.title) }">
              {{ glyphOf(item.track.title) }}
              <img v-if="trackCover(item.track)" :src="trackCover(item.track)" alt="" loading="lazy" decoding="async" @error="hideImg" />
            </div>
            <div class="meta">
              <div class="t">{{ item.track.title }}</div>
              <div class="s">
                <span class="state" :class="STATUS_CLASS[item.status]">{{ STATUS_TEXT[item.status] }}</span>
                <span class="ellipsis">
                  · {{ item.track.artist }} · {{ item.requested_by === name ? "我" : item.requested_by }}
                </span>
              </div>
            </div>
            <i class="chev" :class="{ open: queueOpen === item.id }" aria-hidden="true" />
          </button>
          <div v-if="queueOpen === item.id" class="q-actions">
            <button class="cut" :disabled="queue.indexOf(item) === 0" @click="bump(item)">插到下一首</button>
            <button class="cut" :disabled="queue.indexOf(item) === 0" aria-label="往前一首" @click="moveBy(item, -1)">
              ↑ 往前
            </button>
            <button
              class="cut"
              :disabled="queue.indexOf(item) === queue.length - 1"
              aria-label="往後一首"
              @click="moveBy(item, 1)"
            >
              ↓ 往後
            </button>
            <button v-if="item.requested_by === name" class="cut danger" @click="remove(item)">取消</button>
          </div>
        </li>
      </ul>
      <div v-else class="empty">
        {{ queueFilter === "mine" ? "你還沒點歌，回「點歌」找一首吧" : "還沒有人點歌" }}
      </div>
    </main>

    <!-- ===================== 正在播放（常駐） ===================== -->
    <div
      v-if="current"
      class="mini cut"
      data-tour="now"
      role="button"
      tabindex="0"
      aria-label="打開正在播放"
      @click="openSheet"
      @keydown.enter.prevent="openSheet"
      @keydown.space.prevent="openSheet"
    >
      <div class="meta">
        <div class="eyebrow">NOW PLAYING</div>
        <div class="t">{{ current.track.title }}</div>
        <div class="s">{{ current.track.artist }} · {{ current.requested_by }}點的</div>
      </div>
      <button
        class="mini-play cut"
        :aria-label="playing ? '暫停' : '繼續播放'"
        @click.stop="togglePlay"
      >{{ playing ? "❚❚" : "▶" }}</button>
      <button class="mini-skip cut" @click.stop="skip">卡歌</button>
      <div class="track"><span :style="{ width: `${progress.percent.value}%` }" /></div>
    </div>
    <div v-else class="mini idle cut" data-tour="now">
      <div class="meta">
        <div class="eyebrow">STANDBY</div>
        <div class="s">目前沒有播放中的歌，點一首就會開始</div>
      </div>
    </div>

    <nav class="nav">
      <button :class="{ on: view === 'pick' }" @click="view = 'pick'">
        <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M9 18V6l11-2v12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" />
          <circle cx="6.5" cy="18" r="2.5" fill="none" stroke="currentColor" stroke-width="1.8" />
          <circle cx="17.5" cy="16" r="2.5" fill="none" stroke="currentColor" stroke-width="1.8" />
        </svg>
        點歌
      </button>
      <button :class="{ on: view === 'queue' }" data-tour="queue" @click="view = 'queue'">
        <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M4 6h16M4 12h16M4 18h10" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
        </svg>
        清單
        <span v-if="queue.length" class="badge">{{ queue.length }}</span>
      </button>
      <button :class="{ on: view === 'mic' }" data-tour="mic" @click="view = 'mic'">
        <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">
          <rect x="9" y="3" width="6" height="11" rx="3" fill="none" stroke="currentColor" stroke-width="1.8" />
          <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
        </svg>
        麥克風
        <span v-if="micState.live" class="badge">ON</span>
      </button>
    </nav>

    <!-- ===================== 正在播放 sheet ===================== -->
    <section
      ref="sheetEl"
      class="sheet"
      :class="{ open: sheetOpen && current, dragging: sheetDrag > 0 }"
      :style="sheetDrag ? { transform: `translateY(${sheetDrag}px)` } : undefined"
      aria-label="正在播放"
      @touchstart.passive="onSheetTouchStart"
      @touchmove.passive="onSheetTouchMove"
      @touchend="onSheetTouchEnd"
      @touchcancel="onSheetTouchEnd"
    >
      <div class="sheet-bar">
        <button class="back cut" aria-label="返回" @click="closeSheet">
          <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M15 5 8 12l7 7" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
          返回
        </button>
        <span class="grab" aria-hidden="true" />
        <span class="sheet-hint">往下滑也能收起</span>
      </div>
      <template v-if="current">
        <div>
          <div class="label"><i>//</i> 正在播放 · {{ MODE_TEXT[current.karaoke_mode] }}</div>
          <div class="np-title">{{ current.track.title }}</div>
          <div class="np-sub">{{ current.track.artist }}</div>
          <div class="np-by">
            由 <b>{{ current.requested_by }}</b> 點播<template v-if="nextUp"> · 下一首：{{ nextUp }}</template>
          </div>
        </div>
        <div>
          <div class="prog"><span :style="{ width: `${progress.percent.value}%` }" /></div>
          <div class="times">
            <span>{{ fmt(progress.positionMs.value) }}</span>
            <span>{{ fmt(progress.durationMs.value) }}</span>
          </div>
        </div>
        <button class="big-play cut" @click="togglePlay">{{ playing ? "❚❚ 暫停" : "▶ 繼續播放" }}</button>
        <button class="big-skip cut" @click="skip">卡歌，換下一首</button>

        <div class="ctrl cut">
          <div class="ctrl-h"><strong>音量</strong><span class="ctrl-v">{{ volumePct }}%</span></div>
          <input
            id="remote-volume"
            type="range"
            min="0"
            max="100"
            step="1"
            :value="volumePct"
            aria-label="音量"
            @input="onVolumeInput"
          />
          <div class="ctrl-h">
            <button class="tip-h" :aria-expanded="tip === 'guide'" @click="toggleTip('guide')">
              <strong>導唱</strong><i class="tip-i" aria-hidden="true">i</i>
            </button>
            <span class="ctrl-v">{{ guidePct }}%</span>
          </div>
          <input
            id="remote-guide"
            type="range"
            min="0"
            max="100"
            step="1"
            :value="guidePct"
            aria-label="導唱"
            @input="onGuideInput"
          />
          <div v-if="tip === 'guide'" class="hint">0% 只有伴奏，100% 是原唱。也會套用到你之後點的歌。</div>
          <div class="ctrl-h">
            <button class="tip-h" :aria-expanded="tip === 'key'" @click="toggleTip('key')">
              <strong>升降 Key</strong><i class="tip-i" aria-hidden="true">i</i>
            </button>
            <span class="ctrl-v">{{ pitch > 0 ? `+${pitch}` : pitch }}</span>
          </div>
          <div class="stepper">
            <button class="cut" :disabled="!pitchCapable || pitch <= -3" aria-label="降半音" @click="stepPitch(-1)">−</button>
            <div class="pips" aria-hidden="true">
              <i v-for="n in PIPS" :key="n" :class="{ on: pipOn(n), zero: n === 0 }" />
            </div>
            <button class="cut" :disabled="!pitchCapable || pitch >= 3" aria-label="升半音" @click="stepPitch(1)">+</button>
          </div>
          <div v-if="!pitchCapable || tip === 'key'" class="hint">
            {{ pitchCapable ? "每次半音，±3 為上限。全場共用。" : "大螢幕要用 localhost 開啟 /display 才能升降 Key" }}
          </div>
        </div>

        <div v-if="duetSolos.length && current.karaoke_mode !== 'ORIGINAL'" class="ctrl cut">
          <div class="ctrl-h">
            <button class="tip-h" :aria-expanded="tip === 'duet'" @click="toggleTip('duet')">
              <strong>對唱：保留誰的原唱</strong><i class="tip-i" aria-hidden="true">i</i>
            </button>
          </div>
          <div
            class="seg"
            role="radiogroup"
            aria-label="對唱保留原唱"
            :style="{ gridTemplateColumns: `repeat(${duetSolos.length + 1}, 1fr)` }"
          >
            <button role="radio" :aria-checked="keepSolo === null" :class="{ on: keepSolo === null }" @click="pickKeep(null)">
              不保留
            </button>
            <button
              v-for="p in duetSolos"
              :key="p.part"
              role="radio"
              :aria-checked="keepSolo === p.part"
              :class="{ on: keepSolo === p.part }"
              @click="pickKeep(p.part)"
            >
              {{ p.singer }}
            </button>
          </div>
          <button
            v-if="duetHasChorus && keepSolo !== null"
            class="switch-row"
            role="switch"
            :aria-checked="keepChorus"
            @click="toggleKeepChorus"
          >
            <span>合唱段也放原唱</span>
            <i class="switch" :class="{ on: keepChorus }" aria-hidden="true" />
          </button>
          <div v-if="tip === 'duet'" class="hint">選的那位唱的句子放原唱，你唱另一位的部分，換句時自動切換。只算你唱的句子的分數。</div>
        </div>

        <div v-if="songScores.length" class="ctrl cut score-board">
          <div class="ctrl-h"><strong>本曲高分</strong></div>
          <ol>
            <li v-for="(s, n) in songScores.slice(0, 3)" :key="n">
              <b>{{ s.score }}</b><span class="ellipsis">{{ s.singer || "無名" }}</span>
            </li>
          </ol>
        </div>

        <div class="np-links">
          <button class="cut accent" @click="openSettings">
            <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true"><path :d="GEAR" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" /><circle cx="12" cy="12" r="3" fill="none" stroke="currentColor" stroke-width="1.8" /></svg>
            包廂設定
          </button>
          <button class="cut" :aria-expanded="reportOpen" @click="reportOpen = !reportOpen">
            {{ reportedIds.has(current.track.id) ? "已回報 ✓" : "回報這首歌" }}
          </button>
        </div>

        <div v-if="reportOpen" class="ctrl cut">
          <div class="ctrl-h"><strong>這首歌哪裡有問題？</strong></div>
          <div class="seg report-kinds" role="radiogroup" aria-label="問題">
            <button
              v-for="k in REPORT_KINDS"
              :key="k.value"
              role="radio"
              :aria-checked="reportKind === k.value"
              :class="{ on: reportKind === k.value }"
              @click="reportKind = k.value"
            >
              {{ k.text }}
            </button>
          </div>
          <input v-model="reportNote" class="report-note" maxlength="200" placeholder="補充說明（可不填，例：副歌那段歌詞對不上）" />
          <div class="report-actions">
            <button class="cut" @click="reportOpen = false">取消</button>
            <button class="cut primary" :disabled="!reportKind" @click="sendReport">送出回報</button>
          </div>
        </div>
      </template>
    </section>

    <!-- ===================== 包廂設定 ===================== -->
    <section class="sheet settings" :class="{ open: settingsOpen }" aria-label="包廂設定">
      <div class="sheet-bar">
        <button class="back cut" aria-label="返回" @click="closeSettings">
          <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M15 5 8 12l7 7" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
          返回
        </button>
        <strong class="sheet-title">包廂設定</strong>
        <span class="bar-pad" />
      </div>
      <div class="hint">全場共用，改了大家都會看到。沒有歌在播也能先設好。</div>

      <div class="set-group">
        <div class="label"><i>//</i> 畫面</div>
        <div class="ctrl cut set-list">
          <div class="set-item">
            <button class="set-row" :aria-expanded="setRow === 'bg'" @click="toggleSetRow('bg')">
              <span>大螢幕背景</span><span class="set-v">{{ textOf(BG_MODES, bgMode) }}</span>
              <i class="chev" :class="{ open: setRow === 'bg' }" aria-hidden="true" />
            </button>
            <div v-if="setRow === 'bg'" class="set-body">
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
              <div class="hint">{{ BG_HINT[bgMode] }}</div>
            </div>
          </div>
          <div class="set-item">
            <button class="set-row" :aria-expanded="setRow === 'ja'" @click="toggleSetRow('ja')">
              <span>日文歌注音</span><span class="set-v">{{ textOf(JA_ASSISTS, jaAssist) }}</span>
              <i class="chev" :class="{ open: setRow === 'ja' }" aria-hidden="true" />
            </button>
            <div v-if="setRow === 'ja'" class="set-body">
              <div class="seg ja-seg" role="radiogroup" aria-label="日文歌輔助">
                <button
                  v-for="m in JA_ASSISTS"
                  :key="m.value"
                  role="radio"
                  :aria-checked="jaAssist === m.value"
                  :class="{ on: jaAssist === m.value }"
                  @click="setVisual({ ja_assist: m.value })"
                >
                  {{ m.text }}
                </button>
              </div>
              <div class="hint">{{ JA_HINT[jaAssist] }}</div>
            </div>
          </div>
          <button
            class="switch-row set-item"
            role="switch"
            :aria-checked="pvKaraoke"
            :disabled="bgMode === 'mv'"
            @click="setVisual({ pv_karaoke: !pvKaraoke })"
          >
            <span>文字PV 也顯示卡拉OK歌詞</span>
            <i class="switch" :class="{ on: pvKaraoke }" aria-hidden="true" />
          </button>
          <button
            class="switch-row set-item"
            role="switch"
            :aria-checked="beatLights"
            @click="setVisual({ beat_lights: !beatLights })"
          >
            <span>燈光跟著節拍</span>
            <i class="switch" :class="{ on: beatLights }" aria-hidden="true" />
          </button>
        </div>
      </div>

      <div class="set-group">
        <div class="label"><i>//</i> 聲音</div>
        <div class="ctrl cut set-list">
          <div class="set-item">
            <button class="set-row" :aria-expanded="setRow === 'amb'" @click="toggleSetRow('amb')">
              <span>氣氛</span><span class="set-v">{{ textOf(AMBIENCES, ambience) }}</span>
              <i class="chev" :class="{ open: setRow === 'amb' }" aria-hidden="true" />
            </button>
            <div v-if="setRow === 'amb'" class="set-body">
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
              <div class="hint">{{ AMBIENCE_HINT[ambience] }}</div>
            </div>
          </div>
          <button
            class="switch-row set-item"
            role="switch"
            :aria-checked="micMonitor"
            @click="setVisual({ mic_monitor: !micMonitor })"
          >
            <span>電腦麥克風外放</span>
            <i class="switch" :class="{ on: micMonitor }" aria-hidden="true" />
          </button>
          <div class="set-item">
            <button class="set-row" :aria-expanded="setRow === 'mic'" @click="toggleSetRow('mic')">
              <span>外放音量・迴音</span><span class="set-v">{{ micVolume }}% · {{ micEcho }}</span>
              <i class="chev" :class="{ open: setRow === 'mic' }" aria-hidden="true" />
            </button>
            <div v-if="setRow === 'mic'" class="set-body">
              <div class="ctrl-h"><span>外放音量</span><span class="ctrl-v">{{ micVolume }}%</span></div>
              <input
                id="remote-mic-volume"
                type="range"
                min="0"
                max="150"
                step="5"
                :value="micVolume"
                :disabled="!micMonitor"
                aria-label="麥克風外放音量"
                @input="onMicVolumeInput"
              />
              <div class="ctrl-h"><span>迴音</span><span class="ctrl-v">{{ micEcho }}</span></div>
              <input
                id="remote-mic-echo"
                type="range"
                min="0"
                max="100"
                step="5"
                :value="micEcho"
                aria-label="迴音"
                @input="onMicEchoInput"
              />
              <div class="hint">
                電視那台電腦收到的麥克風聲音從喇叭放出來，迴音手機麥克風也會套用。藍牙麥克風或喇叭會慢 0.2 秒以上，像多一層回音，建議用有線或 USB 麥克風；麥克風組本身就會出聲的話不用開。嘯叫就把音量調小或離喇叭遠一點。
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="set-group">
        <div class="label"><i>//</i> 評分</div>
        <div class="ctrl cut set-list">
          <button class="switch-row set-item" role="switch" :aria-checked="scoreOn" @click="setVisual({ score: !scoreOn })">
            <span>唱歌評分</span>
            <i class="switch" :class="{ on: scoreOn }" aria-hidden="true" />
          </button>
          <button
            class="switch-row set-item"
            role="switch"
            :aria-checked="scoreBar"
            :disabled="!scoreOn"
            @click="setVisual({ score_bar: !scoreBar })"
          >
            <span>顯示音高條</span>
            <i class="switch" :class="{ on: scoreBar }" aria-hidden="true" />
          </button>
          <div class="set-item">
            <button class="set-row" :aria-expanded="setRow === 'delay'" @click="toggleSetRow('delay')">
              <span>評分延遲校正</span><span class="set-v">{{ scoreOffset }} ms</span>
              <i class="chev" :class="{ open: setRow === 'delay' }" aria-hidden="true" />
            </button>
            <div v-if="setRow === 'delay'" class="set-body">
              <div class="stepper">
                <button class="cut" :disabled="!scoreOn || scoreOffset <= 0" aria-label="評分延遲減少" @click="stepScoreOffset(-1)">−</button>
                <div class="hint center">唱的軌跡老是落在音條後面就調大</div>
                <button class="cut" :disabled="!scoreOn || scoreOffset >= 600" aria-label="評分延遲增加" @click="stepScoreOffset(1)">+</button>
              </div>
            </div>
          </div>
        </div>
        <div class="hint">
          電視那台電腦收到的麥克風聲音拿來評分（藍牙麥克風組要設成電腦的預設錄音裝置）。歌曲要先做完 AI 人聲分離才有評分，點過的歌通常幾天內就會開放。
        </div>
      </div>

      <div class="set-group">
        <div class="label"><i>//</i> 進階</div>
        <div class="ctrl cut set-list">
          <div class="set-item">
            <button class="set-row" :aria-expanded="setRow === 'fx'" @click="toggleSetRow('fx')">
              <span>畫面效果<small>舊電視卡頓選省電</small></span><span class="set-v">{{ textOf(FX_MODES, fxMode) }}</span>
              <i class="chev" :class="{ open: setRow === 'fx' }" aria-hidden="true" />
            </button>
            <div v-if="setRow === 'fx'" class="set-body">
              <div class="seg" role="radiogroup" aria-label="畫面效果">
                <button
                  v-for="m in FX_MODES"
                  :key="m.value"
                  role="radio"
                  :aria-checked="fxMode === m.value"
                  :class="{ on: fxMode === m.value }"
                  @click="setVisual({ fx_mode: m.value })"
                >
                  {{ m.text }}
                </button>
              </div>
              <div class="hint">{{ FX_HINT[fxMode] }}</div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <div class="toast cut" :class="{ show: toastText }" role="status">{{ toastText }}</div>

    <RemoteTour v-if="tourOpen" :steps="TOUR_STEPS" @done="endTour" />
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
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px max(16px, env(safe-area-inset-right, 0px)) 10px max(16px, env(safe-area-inset-left, 0px));
}
.logo {
  font: 700 18px/1 var(--hud);
  letter-spacing: 0.06em;
}
.logo b {
  color: var(--cyan);
}
.room-tag {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  color: var(--cyan);
}
.cyber .who {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
  max-width: 55%;
  padding: 7px 12px;
  border: 1px solid var(--line);
  border-radius: 999px;
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.cyber .help {
  flex: none;
  width: 32px;
  height: 32px;
  display: grid;
  place-items: center;
  font: 700 15px/1 var(--hud);
  color: var(--cyan);
  border: 1px solid var(--line-strong);
  border-radius: 50%;
}
.name-edit {
  margin-left: auto;
  width: 55%;
}
.name-edit input {
  width: 100%;
  font: inherit;
  color: var(--fg);
  background: var(--panel);
  border: 1px solid var(--cyan);
  border-radius: 999px;
  padding: 6px 12px;
  outline: none;
}
.dot {
  flex: none;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--danger);
}
.dot.on {
  background: var(--ok);
  box-shadow: 0 0 8px var(--ok);
}

/* -- screens ------------------------------------------------------------- */
.screen {
  flex: 1;
  overflow-y: auto;
  padding: 0 max(16px, env(safe-area-inset-right, 0px)) 16px max(16px, env(safe-area-inset-left, 0px));
  display: flex;
  flex-direction: column;
  gap: 14px;
}
@media (min-width: 600px) {
  .screen,
  .top {
    width: 100%;
    max-width: 560px;
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
.err {
  color: var(--danger);
  font-size: 13px;
}
.empty {
  color: var(--dim);
  text-align: center;
  padding: 32px 8px;
  font-size: 14px;
}

/* search */
.search {
  flex: none;
  display: flex;
  align-items: center;
  gap: 10px;
  height: 52px;
  padding: 0 12px 0 14px;
  background: var(--panel);
  border: 1px solid var(--line-strong);
}
.search:focus-within {
  box-shadow: 0 0 0 1px var(--cyan), 0 0 18px rgba(25, 240, 255, 0.25);
}
.search svg {
  flex: none;
  color: var(--cyan);
}
.search input {
  flex: 1;
  min-width: 0;
  font: inherit;
  font-size: 16px; /* 16px keeps iOS from zooming on focus */
  color: var(--fg);
  background: transparent;
  border: 0;
  outline: none;
  padding: 0;
}
.search input::placeholder {
  color: var(--dim);
}
.search input::-webkit-search-cancel-button {
  display: none;
}
.cyber .clear {
  flex: none;
  width: 32px;
  height: 32px;
  display: grid;
  place-items: center;
  color: var(--dim);
  font-size: 14px;
}

.seg {
  flex: none;
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 4px;
  padding: 4px;
  background: var(--panel);
  border: 1px solid var(--line);
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

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.cyber .chip {
  padding: 7px 12px;
  border: 1px solid var(--line);
  font-size: 13px;
  color: var(--dim);
}
.cyber .chip.sub {
  padding: 5px 10px;
  font-size: 12px;
}
.cyber .chip.on {
  border-color: var(--magenta);
  color: var(--fg);
  background: rgba(255, 47, 180, 0.1);
}

/* rows */
.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
}
.row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 0;
  border-bottom: 1px solid rgba(255, 255, 255, 0.05);
}
.row:last-child {
  border-bottom: 0;
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
/* 封面（熱門榜、搜尋結果、清單）蓋在首字方塊上；載入中或失敗時看到的就是底下的方塊 */
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
.meta {
  flex: 1;
  min-width: 0;
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
.tag {
  flex: none;
  font: 600 10px/1 var(--hud);
  letter-spacing: 0.04em;
  padding: 3px 5px;
  border: 1px solid currentColor;
}
.cyber .more {
  --cut: 8px;
  display: block;
  width: 100%;
  margin: 14px 0 4px;
  padding: 13px;
  text-align: center;
  font: 600 13px/1 var(--hud);
  letter-spacing: 0.12em;
  color: var(--cyan);
  border: 1px solid var(--cyan);
}
.cyber .more:active {
  transform: scale(0.98);
}
.cyber .more:disabled {
  opacity: 0.6;
}
.tag.cache {
  color: var(--ok);
}
.tag.agg {
  color: var(--cyan);
}
.tag.yt {
  color: var(--magenta);
}
.tag.queued {
  color: var(--void);
  background: var(--volt);
  border-color: var(--volt);
}
.rank {
  flex: none;
  width: 32px; /* 有的榜單到 500 名 */
  text-align: center;
  font: 700 16px/1 var(--hud);
  color: var(--dim);
  font-variant-numeric: tabular-nums;
}
/* 不能叫 .top：會吃到頁首 .top 的 padding，前三名整列往右偏 */
.rank.podium {
  color: var(--volt);
}
.cyber .add {
  --cut: 8px;
  flex: none;
  width: 44px;
  height: 44px;
  display: grid;
  place-items: center;
  border: 1px solid var(--cyan);
  color: var(--cyan);
  transition: background 0.15s, color 0.15s;
}
.cyber .add:active {
  transform: scale(0.95);
}
.cyber .add.done {
  background: var(--cyan);
  color: var(--void);
}
.spin {
  width: 16px;
  height: 16px;
  border: 2px solid currentColor;
  border-right-color: transparent;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

/* queue */
.q-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.wait {
  font: 600 13px/1 var(--hud);
  color: var(--volt);
}
.pos {
  flex: none;
  width: 26px;
  text-align: center;
  font: 700 15px/1 var(--hud);
  color: var(--dim);
  font-variant-numeric: tabular-nums;
}
.q-tip {
  margin-left: auto;
}
.q-item {
  border-bottom: 1px solid rgba(255, 255, 255, 0.05);
}
.q-item:last-child {
  border-bottom: 0;
}
.cyber .q-row {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  padding: 10px 0;
  border-bottom: 0;
}
.q-item.open {
  background: rgba(25, 240, 255, 0.05);
}
.q-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 0 0 12px 38px;
}
.cyber .q-actions button {
  --cut: 7px;
  padding: 8px 12px;
  font-size: 13px;
  color: var(--cyan);
  border: 1px solid var(--line-strong);
}
.cyber .q-actions button.danger {
  margin-left: auto;
  color: var(--danger);
  border-color: rgba(255, 77, 106, 0.5);
}
.q-item.mine {
  padding-left: 8px;
  background: linear-gradient(90deg, rgba(255, 47, 180, 0.1), transparent 70%);
  box-shadow: inset 2px 0 0 var(--magenta);
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
  width: 8px;
  height: 8px;
  margin: 0 4px 0 2px;
  border-right: 2px solid var(--dim);
  border-bottom: 2px solid var(--dim);
  transform: rotate(-45deg);
  transition: transform 0.15s;
}
.chev.open {
  transform: rotate(45deg);
}
.cyber .icon-btn {
  flex: none;
  width: 40px;
  height: 44px;
  display: grid;
  place-items: center;
  color: var(--dim);
}
.cyber .icon-btn:hover {
  color: var(--fg);
}
.cyber .icon-btn.danger:hover {
  color: var(--danger);
}

/* -- mini player --------------------------------------------------------- */
.mini {
  flex: none;
  position: relative;
  z-index: 2;
  display: grid;
  grid-template-columns: 1fr auto auto;
  align-items: center;
  gap: 2px 8px;
  margin: 0 max(12px, env(safe-area-inset-right, 0px)) 8px max(12px, env(safe-area-inset-left, 0px));
  padding: 10px 10px 0 14px;
  background: var(--panel-2);
  border: 1px solid var(--line-strong);
  cursor: pointer;
}
.mini.idle {
  cursor: default;
  padding-bottom: 10px;
  border-color: var(--line);
}
.mini:focus-visible {
  outline: 2px solid var(--volt);
}
.eyebrow {
  font: 600 10px/1.4 var(--hud);
  letter-spacing: 0.16em;
  color: var(--magenta);
}
.mini.idle .eyebrow {
  color: var(--dim);
}
.cyber .mini-skip {
  --cut: 7px;
  padding: 9px 14px;
  font-size: 14px;
  font-weight: 600;
  color: var(--magenta);
  border: 1px solid var(--magenta);
}
.cyber .mini-play {
  --cut: 7px;
  min-width: 44px;
  padding: 9px 12px;
  font-size: 14px;
  font-weight: 600;
  color: var(--cyan);
  border: 1px solid var(--cyan);
}
.track {
  grid-column: 1 / -1;
  height: 3px;
  margin: 8px -10px 0 -14px;
  background: rgba(255, 255, 255, 0.08);
}
.track span,
.prog span {
  display: block;
  height: 100%;
  background: linear-gradient(90deg, var(--cyan), var(--magenta));
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

/* -- now playing sheet --------------------------------------------------- */
.sheet {
  position: absolute;
  inset: 0;
  z-index: 5;
  display: flex;
  flex-direction: column;
  gap: 18px;
  padding: calc(14px + env(safe-area-inset-top, 0px)) max(18px, env(safe-area-inset-right, 0px))
    calc(22px + env(safe-area-inset-bottom, 0px)) max(18px, env(safe-area-inset-left, 0px));
  overflow-y: auto;
  background: var(--void);
  transform: translateY(100%);
  visibility: hidden;
  transition: transform 0.28s cubic-bezier(0.2, 0.8, 0.2, 1), visibility 0s 0.28s;
}
.sheet.open {
  transform: none;
  visibility: visible;
  transition: transform 0.28s cubic-bezier(0.2, 0.8, 0.2, 1);
}
.sheet > * {
  width: 100%;
  max-width: 560px;
  margin: 0 auto;
}
.sheet.dragging {
  transition: none;
}
.sheet-bar {
  flex: none;
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.cyber .back {
  --cut: 7px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 8px 14px 8px 8px;
  font-size: 15px;
  font-weight: 600;
  color: var(--cyan);
  border: 1px solid var(--line-strong);
}
.grab {
  position: absolute;
  left: 50%;
  top: -6px;
  transform: translateX(-50%);
  display: grid;
  place-items: center;
}
.sheet-hint {
  font-size: 12px;
  color: var(--dim);
}
.grab::before {
  content: "";
  width: 44px;
  height: 5px;
  border-radius: 3px;
  background: var(--dim);
  opacity: 0.6;
}
.np-title {
  margin-top: 10px;
  font-size: 26px;
  font-weight: 700;
  line-height: 1.25;
  text-wrap: balance;
}
.np-sub {
  color: var(--dim);
}
.np-by {
  margin-top: 6px;
  font-size: 13px;
  color: var(--dim);
}
.np-by b {
  color: var(--magenta);
  font-weight: 600;
}
.prog {
  height: 6px;
  background: rgba(255, 255, 255, 0.08);
}
.prog span {
  box-shadow: 0 0 12px rgba(25, 240, 255, 0.6);
}
.times {
  display: flex;
  justify-content: space-between;
  margin-top: 8px;
  font: 600 13px/1 var(--hud);
  color: var(--dim);
  font-variant-numeric: tabular-nums;
}
.cyber .tip-h {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.tip-i {
  width: 16px;
  height: 16px;
  display: grid;
  place-items: center;
  font: 700 11px/1 var(--hud);
  font-style: normal;
  color: var(--dim);
  border: 1px solid var(--dim);
  border-radius: 50%;
}
.np-links {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.cyber .np-links button {
  --cut: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 12px;
  font-size: 14px;
  color: var(--dim);
  border: 1px solid var(--line-strong);
}
.cyber .np-links button.accent {
  color: var(--cyan);
  border-color: var(--cyan);
}

/* -- 包廂設定 ------------------------------------------------------------- */
.sheet.settings {
  z-index: 6;
  gap: 14px;
}
.sheet-title {
  font-size: 16px;
}
.bar-pad {
  width: 80px;
}
.set-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ctrl.set-list {
  gap: 0;
  padding: 0 14px;
}
.set-item {
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}
.set-item:last-child {
  border-bottom: 0;
}
.cyber .set-row {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-height: 48px;
  font-size: 15px;
  color: var(--fg);
}
.set-row > span:first-child {
  flex: 1;
  min-width: 0;
}
.set-row small {
  display: block;
  font-size: 12px;
  color: var(--dim);
}
.set-v {
  font-size: 14px;
  color: var(--cyan);
  white-space: nowrap;
}
.cyber .switch-row.set-item {
  width: 100%;
  min-height: 48px;
  font-size: 15px;
}
.set-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 0 0 14px;
}
.cyber .ja-seg {
  grid-template-columns: repeat(4, 1fr);
}
.ctrl.fold {
  padding-top: 4px;
  padding-bottom: 4px;
}
.ctrl.fold > .set-row + * {
  margin-top: -4px;
}
.ctrl.fold > :last-child:not(.set-row) {
  margin-bottom: 10px;
}

.cyber .big-play {
  --cut: 12px;
  text-align: center;
  padding: 14px;
  font-size: 17px;
  font-weight: 700;
  color: var(--cyan);
  border: 1px solid var(--cyan);
}
.cyber .big-skip {
  --cut: 12px;
  text-align: center;
  padding: 16px;
  font-size: 17px;
  font-weight: 700;
  color: #fff;
  background: var(--magenta);
  box-shadow: 0 0 24px rgba(255, 47, 180, 0.35);
}
/* -- microphone ---------------------------------------------------------- */
.mic-screen {
  align-items: stretch;
}
.cyber .mic-btn {
  align-self: center;
  margin: 12px 0 0;
  width: min(62vw, 240px);
  aspect-ratio: 1;
  border-radius: 50%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  font: 700 18px/1 var(--hud);
  letter-spacing: 0.2em;
  color: var(--dim);
  background: var(--panel);
  border: 2px solid var(--line-strong);
  touch-action: none;
  user-select: none;
  -webkit-user-select: none;
  -webkit-touch-callout: none;
  transition: color 0.12s, background 0.12s, box-shadow 0.12s;
}
.cyber .mic-btn.live {
  color: #fff;
  background: var(--magenta);
  border-color: var(--magenta);
  box-shadow: 0 0 40px rgba(255, 47, 180, 0.6);
}
.cyber .mic-btn.wait {
  opacity: 0.5;
}
.mic-label {
  text-align: center;
  color: var(--dim);
  font-size: 14px;
}
.cyber .route-seg {
  grid-template-columns: repeat(2, 1fr);
}
.lat-parts {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 4px;
  text-align: center;
  font-size: 12px;
  color: var(--dim);
}
.lat-parts b {
  display: block;
  font: 700 16px/1.3 var(--hud);
  color: var(--fg);
  font-variant-numeric: tabular-nums;
}
.mic-err {
  color: var(--danger);
}
.cyber .mic-off {
  align-self: center;
  padding: 8px 16px;
  font-size: 13px;
  color: var(--dim);
  text-decoration: underline;
}
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
.hint {
  font-size: 12px;
  color: var(--dim);
}
.ctrl input[type="range"]::-webkit-slider-runnable-track {
  background: linear-gradient(90deg, var(--cyan), var(--magenta));
  border-radius: 0;
  height: 4px;
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
}
.ctrl input[type="range"]::-moz-range-thumb {
  width: 18px;
  height: 18px;
  border-radius: 2px;
  background: var(--void);
  border: 2px solid var(--cyan);
  box-shadow: 0 0 10px var(--cyan);
}
.score-delay {
  margin-top: 6px;
}
.score-board ol {
  margin: 4px 0 6px;
  padding: 0;
  list-style: none;
  display: grid;
  gap: 4px;
}
.score-board li {
  display: grid;
  grid-template-columns: 3em 1fr;
  align-items: baseline;
  gap: 10px;
}
.score-board b {
  font: 700 20px/1 var(--hud);
  color: var(--volt);
}
.hint.center {
  margin: 0;
  text-align: center;
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

/* -- 回報 ---------------------------------------------------------------- */
.seg.report-kinds {
  grid-template-columns: repeat(2, 1fr);
}
.report-note {
  width: 100%;
  margin-top: 8px;
  padding: 10px 12px;
  font: inherit;
  color: var(--fg);
  background: var(--bg);
  border: 1px solid var(--line);
  border-radius: 0;
}
.report-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
  margin-top: 8px;
}
.report-actions button {
  padding: 10px 16px;
  font: inherit;
  color: var(--fg);
  background: var(--panel);
  border: 1px solid var(--line);
}
.report-actions .primary {
  background: var(--grad);
  border-color: transparent;
}
.report-actions .primary:disabled {
  opacity: 0.4;
}

/* -- toast --------------------------------------------------------------- */
.toast {
  --cut: 8px;
  position: absolute;
  left: max(16px, env(safe-area-inset-left, 0px));
  right: max(16px, env(safe-area-inset-right, 0px));
  bottom: calc(150px + env(safe-area-inset-bottom, 0px));
  z-index: 7;
  max-width: 528px;
  margin: 0 auto;
  padding: 12px 14px;
  font-weight: 700;
  color: var(--void);
  background: var(--volt);
  opacity: 0;
  transform: translateY(8px);
  transition: opacity 0.2s, transform 0.2s;
  pointer-events: none;
}
.toast.show {
  opacity: 1;
  transform: none;
}

@media (prefers-reduced-motion: reduce) {
  .sheet,
  .sheet.open,
  .toast,
  .cyber .add {
    transition: none;
  }
  .spin {
    animation: none;
  }
}
</style>
