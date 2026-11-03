<script setup lang="ts">
/**
 * 歌曲快取（管理員）：伺服器上每首下載過的歌，和它可能要修的東西——
 * 歌詞錯了就重抓、自己挑版本或貼上；音檔版本錯了就換一個搜尋結果；
 * MV 不對就重抓或指定 YouTube 網址。後端在 server/ktv/library.py。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { url } from "../base";
import type { Track } from "../types";

interface Job {
  kind: string;
  state: "running" | "done" | "failed";
  message: string;
}
interface Song {
  id: string;
  title: string;
  artist: string;
  source: string;
  source_id: string;
  duration_ms: number;
  lyrics_offset_ms: number;
  created_at: number;
  in_db: boolean;
  size: number;
  audio: string;
  audio_size: number;
  lyrics: string;
  mv: string;
  mv_size: number;
  stems: string[];
  stems_size: number;
  ai: "ready" | "running" | "queued" | "failed" | "none";
  manifest: Record<string, any>;
  busy: string;
  job: Job | null;
  reports: number;
}
interface Listing {
  songs: Song[];
  ai: { ai_available: boolean; ai_active: string | null; ai_backfill: boolean };
  ai_max_attempts: number;
  lyrics_net: boolean;
  lyrics_providers: Record<string, string>;
}
interface Candidate {
  id: number;
  mid?: string;
  title: string;
  artists: string[];
  album?: string;
  duration_ms: number;
  score: number;
  provider: string;
  [key: string]: unknown;
}
interface Preview {
  format: string;
  content: string;
  timing: string;
  lines: { start: number; text: string }[];
}

const data = ref<Listing | null>(null);
const error = ref("");
const notice = ref("");
const filter = ref("");
const show = ref<"all" | "nolyrics" | "line" | "nomv" | "ai" | "busy">("all");
const sort = ref<"new" | "name" | "size">("new");
const openId = ref("");
const tab = ref<"lyrics" | "audio" | "mv" | "more">("lyrics");
const working = ref(false);
let timer: number | undefined;

// -- naming ------------------------------------------------------------------
const PLATFORMS: Record<string, string> = {
  wy: "網易", tx: "QQ", kg: "酷狗", kw: "酷我", mg: "咪咕",
  netease: "網易", tencent: "QQ", kugou: "酷狗", kuwo: "酷我", migu: "咪咕",
};
const LYRICS_PROVIDERS: Record<string, string> = {
  ne: "網易", qq: "QQ", kg: "酷狗", lrclib: "LRCLIB", manual: "手動",
};

function audioLabel(song: Song): string {
  const m = song.manifest;
  if (!song.audio) return "沒有音檔";
  if (m.audio_via === "youtube" || (!m.audio_via && song.source === "youtube")) return "YouTube";
  if (m.audio_platform) return PLATFORMS[m.audio_platform] ?? m.audio_platform;
  if (m.audio_step) return "關鍵字";
  return PLATFORMS[song.source_id.split("_")[0]] ?? (song.source || "來源不明");
}
function lyricsLabel(song: Song): { text: string; cls: string } {
  if (!song.lyrics) return { text: "沒歌詞", cls: "bad" };
  const m = song.manifest;
  const who = m.lyrics_manual && !m.lyrics_provider ? "手動" : LYRICS_PROVIDERS[m.lyrics_provider] ?? "";
  const timing = m.lyrics_timing === "word" ? "逐字" : m.lyrics_timing === "line" ? "逐行" : "";
  const pinned = m.lyrics_manual && m.lyrics_provider && m.lyrics_provider !== "manual" ? "（手選）" : "";
  return { text: [who, timing].filter(Boolean).join(" ") + pinned || "有歌詞", cls: m.lyrics_timing === "line" ? "meh" : "" };
}
function aiLabel(song: Song): { text: string; cls: string } {
  switch (song.ai) {
    case "ready": return { text: "AI 伴奏", cls: "" };
    case "running": return { text: data.value?.ai.ai_backfill ? "AI 分離中（閒置補做）" : "AI 分離中", cls: "run" };
    case "queued": return { text: "AI 排隊", cls: "run" };
    case "failed": return { text: "AI 失敗", cls: "bad" };
    default:
      return song.manifest.ai_attempts ? { text: `AI 失敗 ${song.manifest.ai_attempts} 次`, cls: "meh" } : { text: "", cls: "" };
  }
}
function mb(bytes: number): string {
  return bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)} GB` : `${(bytes / 1e6).toFixed(bytes < 1e7 ? 1 : 0)} MB`;
}
function mmss(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
function media(song: Song, name: string): string {
  return url(`/media/${encodeURIComponent(song.id)}/${encodeURIComponent(name)}`);
}

// -- list ----------------------------------------------------------------------
const songs = computed(() => {
  const rows = data.value?.songs ?? [];
  const q = filter.value.trim().toLowerCase();
  const out = rows.filter((s) => {
    if (q && !`${s.title} ${s.artist} ${s.id}`.toLowerCase().includes(q)) return false;
    switch (show.value) {
      case "nolyrics": return !s.lyrics;
      case "line": return !!s.lyrics && s.manifest.lyrics_timing === "line";
      case "nomv": return !s.mv;
      case "ai": return s.ai === "failed" || !!s.manifest.ai_error;
      case "busy": return !!s.busy || s.job?.state === "running";
      default: return true;
    }
  });
  if (sort.value === "new") out.sort((a, b) => b.created_at - a.created_at);
  else if (sort.value === "name") out.sort((a, b) => a.title.localeCompare(b.title, "zh-Hant"));
  else if (sort.value === "size") out.sort((a, b) => b.size - a.size);
  return out;
});
const totalSize = computed(() => (data.value?.songs ?? []).reduce((n, s) => n + s.size, 0));
const counts = computed(() => {
  const rows = data.value?.songs ?? [];
  return {
    nolyrics: rows.filter((s) => !s.lyrics).length,
    line: rows.filter((s) => s.lyrics && s.manifest.lyrics_timing === "line").length,
    nomv: rows.filter((s) => !s.mv).length,
    ai: rows.filter((s) => s.ai === "failed" || s.manifest.ai_error).length,
    busy: rows.filter((s) => s.busy || s.job?.state === "running").length,
  };
});
const current = computed(() => data.value?.songs.find((s) => s.id === openId.value) ?? null);
const anyJob = computed(() => (data.value?.songs ?? []).some((s) => s.job?.state === "running"));

async function load() {
  try {
    const res = await fetch(url("/api/library"), { cache: "no-store" });
    if (res.status === 401 || res.status === 403) {
      location.href = url("/login") + "?next=" + encodeURIComponent("/songs");
      return;
    }
    if (!res.ok) throw new Error(String(res.status));
    data.value = await res.json();
    error.value = "";
    await loadReports();
  } catch {
    error.value = "連不到伺服器";
  }
}

function schedule() {
  clearTimeout(timer);
  timer = window.setTimeout(async () => {
    await load();
    schedule();
  }, anyJob.value ? 2000 : 15000);
}

/** One API call; the answer's `detail` becomes the error line. */
async function api(method: string, path: string, body?: unknown): Promise<any> {
  working.value = true;
  error.value = "";
  try {
    const res = await fetch(url(path), {
      method,
      headers: body !== undefined ? { "content-type": "application/json" } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    const out = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(out.detail ?? `失敗（${res.status}）`);
    return out;
  } catch (exc) {
    error.value = (exc as Error).message;
    throw exc;
  } finally {
    working.value = false;
  }
}
const songPath = (song: Song, rest = "") => `/api/library/${encodeURIComponent(song.id)}${rest}`;

function toggle(song: Song) {
  openId.value = openId.value === song.id ? "" : song.id;
}

// -- detail: lyrics ------------------------------------------------------------------
const lyricLines = ref<{ start: number; text: string }[]>([]);
const lyricsInfo = ref("");
const offset = ref(0);
const provider = ref("ne");
const lyricQuery = ref("");
const candidates = ref<Candidate[]>([]);
const searched = ref(false);
const preview = ref<Preview | null>(null);
const previewOf = ref<Candidate | null>(null);
const pasteText = ref("");
const pasteFormat = ref("lrc");

async function loadLyrics(song: Song) {
  lyricLines.value = [];
  lyricsInfo.value = "";
  if (!song.lyrics) return;
  try {
    const res = await fetch(url(`/lyrics/${encodeURIComponent(song.id)}`), { cache: "no-store" });
    if (!res.ok) return;
    const body = await res.json();
    lyricLines.value = (body.lines ?? []).map((l: any) => ({
      start: l.start ?? 0,
      text: (l.words ?? []).map((w: any) => w.text).join(""),
    }));
    lyricsInfo.value = `${String(body.format).toUpperCase()}・${lyricLines.value.length} 行`;
  } catch { /* the list still works */ }
}

async function refetchLyrics(song: Song) {
  const out = await api("POST", songPath(song, "/lyrics/refetch"));
  notice.value = `歌詞已更新（${LYRICS_PROVIDERS[out.provider] ?? out.provider}，${out.timing === "word" ? "逐字" : "逐行"}）`;
  await load();
  await loadLyrics(song);
}
async function deleteLyrics(song: Song) {
  if (!confirm(`刪除《${song.title}》的歌詞？`)) return;
  await api("DELETE", songPath(song, "/lyrics"));
  await load();
  await loadLyrics(song);
}
async function searchLyrics(song: Song) {
  preview.value = null;
  candidates.value = await api(
    "GET",
    songPath(song, `/lyrics/search?provider=${provider.value}&q=${encodeURIComponent(lyricQuery.value)}`),
  );
  searched.value = true;
}
async function previewLyrics(song: Song, cand: Candidate) {
  previewOf.value = cand;
  preview.value = null;
  preview.value = await api("POST", songPath(song, "/lyrics/preview"), { provider: cand.provider, cand });
}
async function applyPreview(song: Song) {
  if (!preview.value || !previewOf.value) return;
  await api("POST", songPath(song, "/lyrics"), {
    content: preview.value.content,
    format: preview.value.format,
    provider: previewOf.value.provider,
  });
  notice.value = "已換上這個版本的歌詞";
  preview.value = null;
  candidates.value = [];
  searched.value = false;
  await load();
  await loadLyrics(song);
}
async function savePaste(song: Song) {
  await api("POST", songPath(song, "/lyrics"), { content: pasteText.value, format: pasteFormat.value });
  notice.value = "已存入貼上的歌詞";
  pasteText.value = "";
  await load();
  await loadLyrics(song);
}
async function saveOffset(song: Song) {
  await api("PATCH", `/api/tracks/${encodeURIComponent(song.id)}`, { lyrics_offset_ms: Math.round(offset.value) });
  notice.value = `歌詞時間偏移 ${offset.value > 0 ? "+" : ""}${offset.value} ms`;
  await load();
}

// -- detail: audio -----------------------------------------------------------------------
const audioQuery = ref("");
const audioHits = ref<Track[]>([]);
const audioSearched = ref(false);
const refetchWithAudio = ref(true);

function hitPlatforms(hit: Track): string {
  const names = [hit.source_id, ...(hit.alternates ?? [])]
    .map((id) => PLATFORMS[(id ?? "").split("_")[0]])
    .filter((name, i, all): name is string => !!name && all.indexOf(name) === i);
  return hit.source === "youtube" ? "YouTube" : names.join("・") || hit.source;
}
const YT_URL = /(?:youtube\.com\/(?:watch\?.*v=|shorts\/|embed\/|live\/)|youtu\.be\/)([\w-]{11})/;
/** `from` = "youtube" searches YouTube for the plain query; a pasted YouTube link is a hit by itself. */
async function searchAudio(from: "main" | "youtube" = "main") {
  audioHits.value = [];
  const q = audioQuery.value.trim();
  const link = YT_URL.exec(q);
  if (link) {
    audioHits.value = [{ id: `youtube_${link[1]}`, title: "這支 YouTube 影片", artist: link[1], duration_ms: 0, source: "youtube", source_id: link[1] } as Track];
    audioSearched.value = true;
    return;
  }
  const res = await fetch(url(`/api/search?source=${from}&limit=10&raw=1&q=${encodeURIComponent(q)}`));
  if (!res.ok) {
    error.value = from === "youtube" ? "YouTube 搜尋失敗" : "搜尋失敗";
    return;
  }
  const hits = (await res.json()) as Track[];
  audioHits.value = from === "youtube" ? hits : hits.filter((t) => !t.cached && !t.video_only);
  audioSearched.value = true;
}
/** Each platform a (merged) search row carries: [{source_id, name}], the row's own first. */
function hitSources(hit: Track): { sourceId: string; name: string }[] {
  if (hit.source === "youtube") return [{ sourceId: hit.source_id, name: "YouTube" }];
  const out: { sourceId: string; name: string }[] = [];
  for (const id of [hit.source_id, ...(hit.alternates ?? [])]) {
    const name = PLATFORMS[(id ?? "").split("_")[0]] ?? id;
    if (id && !out.some((o) => o.sourceId === id)) out.push({ sourceId: id, name });
  }
  return out;
}
/** `sourceId` = the admin picked that platform: download exactly it, no fallback. */
async function replaceAudio(song: Song, hit: Track | null, sourceId = "") {
  const from = sourceId ? hitSources(hit!).find((o) => o.sourceId === sourceId)?.name : "";
  const what = hit
    ? `「${hit.title} - ${hit.artist}」（${from ? `只用${from}` : hitPlatforms(hit)}）`
    : "同一個來源重新下載";
  if (!confirm(`《${song.title}》的音檔換成${what}？\n舊的 AI 伴奏會刪掉，之後閒置時重新分離。`)) return;
  await api("POST", songPath(song, "/audio"), {
    pick: hit
      ? sourceId
        ? { source: hit.source, source_id: sourceId, duration_ms: hit.duration_ms, alternates: [], strict: true }
        : { source: hit.source, source_id: hit.source_id, duration_ms: hit.duration_ms, alternates: hit.alternates ?? [] }
      : null,
    refetch_lyrics: refetchWithAudio.value,
  });
  audioHits.value = [];
  audioSearched.value = false;
  await load();
  schedule();
}

// -- detail: MV / more ------------------------------------------------------------
const mvUrl = ref("");
const editTitle = ref("");
const editArtist = ref("");

async function replaceMv(song: Song, video: string) {
  await api("POST", songPath(song, "/mv"), { video });
  mvUrl.value = "";
  await load();
  schedule();
}
async function deleteMv(song: Song) {
  if (!confirm(`刪除《${song.title}》的 MV？（之後點這首會再自動找一次）`)) return;
  await api("DELETE", songPath(song, "/mv"));
  await load();
}
async function deleteStems(song: Song) {
  if (!confirm(`刪除《${song.title}》的 AI 伴奏？閒置時會重新分離。`)) return;
  await api("DELETE", songPath(song, "/stems"));
  await load();
}
async function saveMeta(song: Song) {
  await api("PATCH", songPath(song), { title: editTitle.value, artist: editArtist.value });
  notice.value = "歌名已存；要用新歌名找歌詞就按「歌詞 → 重抓」";
  await load();
}
async function deleteSong(song: Song) {
  if (!confirm(`整首刪除《${song.title}》？音檔、歌詞、MV、AI 伴奏都會刪掉，下次點會重新下載。`)) return;
  await api("DELETE", songPath(song));
  openId.value = "";
  await load();
}

watch(openId, async () => {
  const song = current.value;
  notice.value = "";
  candidates.value = [];
  searched.value = false;
  preview.value = null;
  audioHits.value = [];
  audioSearched.value = false;
  if (!song) return;
  const q = `${song.title} ${song.artist}`.trim();
  lyricQuery.value = q;
  audioQuery.value = q;
  offset.value = song.lyrics_offset_ms;
  editTitle.value = song.title;
  editArtist.value = song.artist;
  refetchWithAudio.value = !song.manifest.lyrics_manual;
  await loadLyrics(song);
});
watch(anyJob, schedule);

// -- 回報（客人在遙控器上按「這首歌有問題？」） -------------------------------------
interface Report {
  id: number;
  track_id: string;
  title: string;
  artist: string;
  kind: "lyrics" | "track" | "duet" | "other";
  notes: string[];
  reporters: string[];
  count: number;
  room: string;
  updated_at: number;
  resolved_at: number | null;
  cached: boolean;
}
const REPORT_TEXT: Record<string, string> = {
  lyrics: "歌詞錯",
  track: "不是這首／版本錯",
  duet: "對唱沒分顏色",
  other: "其他",
};
const view = ref<"songs" | "reports" | "order" | "stats">("songs");
const reports = ref<Report[]>([]);
const openReports = computed(() => reports.value.filter((r) => !r.resolved_at));

async function loadReports() {
  try {
    const res = await fetch(url("/api/library/reports"), { cache: "no-store" });
    if (res.ok) reports.value = await res.json();
  } catch { /* load() reports the connection */ }
}
async function resolveReport(report: Report, resolved = true) {
  await api("POST", "/api/library/reports/resolve", { ids: [report.id], resolved });
  await loadReports();
  await load();
}
function ago(stamp: number): string {
  const minutes = Math.floor((Date.now() / 1000 - stamp) / 60);
  if (minutes < 2) return "剛剛";
  if (minutes < 120) return `${minutes} 分鐘前`;
  if (minutes < 2880) return `${Math.floor(minutes / 60)} 小時前`;
  return `${Math.floor(minutes / 1440)} 天前`;
}
/** Jump to the song with the tab that fixes this kind of report. */
async function fixReport(report: Report) {
  view.value = "songs";
  filter.value = "";
  show.value = "all";
  openId.value = report.track_id;
  tab.value = report.kind === "track" ? "audio" : "lyrics";
  await nextTick();
  document.getElementById(`song-${report.track_id}`)?.scrollIntoView({ block: "start", behavior: "smooth" });
}

// -- 搜尋排序（某個關鍵字：把對的歌釘到最前面、錯的隱藏） ---------------------------
interface Rule {
  query: string;
  label: string;
  pins: Track[];
  hidden: string[];
}
const rules = ref<Rule[]>([]);
const orderQuery = ref("");
const orderFor = ref(""); // keyword the lists below belong to
const orderHits = ref<Track[]>([]);
const pins = ref<Track[]>([]);
const hidden = ref<string[]>([]);
const orderDirty = ref(false);

async function loadRules() {
  try {
    const res = await fetch(url("/api/library/search-rules"), { cache: "no-store" });
    if (res.ok) rules.value = await res.json();
  } catch { /* shown by load() */ }
}
async function orderSearch(q = orderQuery.value) {
  q = q.trim();
  if (!q) return;
  orderQuery.value = q;
  working.value = true;
  error.value = "";
  try {
    const enc = encodeURIComponent(q);
    const [hitsRes, ruleRes] = await Promise.all([
      fetch(url(`/api/search?source=main&limit=10&raw=1&q=${enc}`)),
      fetch(url(`/api/library/search-rules?q=${enc}`), { cache: "no-store" }),
    ]);
    if (!hitsRes.ok || !ruleRes.ok) throw new Error("搜尋失敗");
    orderHits.value = await hitsRes.json();
    const [rule] = (await ruleRes.json()) as Rule[];
    pins.value = rule?.pins ?? [];
    hidden.value = rule?.hidden ?? [];
    orderFor.value = q;
    orderDirty.value = false;
  } catch (exc) {
    error.value = (exc as Error).message;
  } finally {
    working.value = false;
  }
}
const pinnedIds = computed(() => new Set(pins.value.map((p) => p.id)));
function pin(hit: Track) {
  if (pinnedIds.value.has(hit.id)) return;
  const { cached: _c, pinned: _p, ...track } = hit as Track & { pinned?: boolean };
  pins.value = [...pins.value, track as Track];
  hidden.value = hidden.value.filter((id) => id !== hit.id);
  orderDirty.value = true;
}
function unpin(index: number) {
  pins.value = pins.value.filter((_, i) => i !== index);
  orderDirty.value = true;
}
function movePin(index: number, delta: number) {
  const list = [...pins.value];
  const [item] = list.splice(index, 1);
  list.splice(Math.max(0, Math.min(list.length, index + delta)), 0, item);
  pins.value = list;
  orderDirty.value = true;
}
function toggleHide(hit: Track) {
  hidden.value = hidden.value.includes(hit.id)
    ? hidden.value.filter((id) => id !== hit.id)
    : [...hidden.value, hit.id];
  orderDirty.value = true;
}
async function saveOrder() {
  await api("PUT", "/api/library/search-rules", { q: orderFor.value, pins: pins.value, hidden: hidden.value });
  orderDirty.value = false;
  notice.value = `「${orderFor.value}」的搜尋排序已存，遙控器上馬上生效`;
  await loadRules();
}
async function clearOrder(rule: Rule) {
  if (!confirm(`清除「${rule.label}」的搜尋排序？`)) return;
  await api("PUT", "/api/library/search-rules", { q: rule.label, pins: [], hidden: [] });
  if (orderFor.value && rule.label === orderFor.value) {
    pins.value = [];
    hidden.value = [];
  }
  await loadRules();
}
watch(view, (value) => {
  notice.value = "";
  if (value === "order") loadRules();
  if (value === "reports") loadReports();
  if (value === "stats") loadStats();
});

// -- 統計（客人點了什麼、搜了什麼） -------------------------------------------------
interface StatSong {
  track_id: string;
  title: string;
  artist: string;
  orders: number;
  finished: number;
  skipped: number;
}
interface StatSearch {
  query: string;
  count: number;
  ordered?: number;
  results?: number;
}
interface StatEvent {
  kind: "order" | "end" | "search";
  room: string;
  user: string;
  title: string;
  artist: string;
  query: string;
  results: number;
  ordered: number;
  via: string;
  outcome: string;
  played_ms: number;
  created_at: number;
}
interface Stats {
  totals: { orders: number; searches: number; finished: number; skipped: number; users: number };
  songs: StatSong[];
  searches: StatSearch[];
  missing: StatSearch[];
  users: { user: string; orders: number; songs: number }[];
  recent: StatEvent[];
  rooms: string[];
}
const stats = ref<Stats | null>(null);
const statDays = ref(30);
const statRoom = ref("");
const recentAll = ref(false);
const VIA_TEXT: Record<string, string> = { search: "搜尋", youtube: "YouTube", charts: "排行榜" };
const OUTCOME_TEXT: Record<string, string> = { finished: "唱完", skipped: "切歌", failed: "播放失敗" };

async function loadStats() {
  try {
    const q = `days=${statDays.value}&room=${encodeURIComponent(statRoom.value)}`;
    const res = await fetch(url(`/api/library/stats?${q}`), { cache: "no-store" });
    if (res.ok) stats.value = await res.json();
  } catch { /* load() reports the connection */ }
}
watch([statDays, statRoom], () => void loadStats());

/** 唱完率 of one song: finished out of the times it ended, not ordered */
function finishRate(song: StatSong): string {
  const ended = song.finished + song.skipped;
  return ended ? `唱完 ${Math.round((song.finished / ended) * 100)}%` : "";
}
function eventText(e: StatEvent): string {
  if (e.kind === "search") {
    const found = e.results ? `${e.results} 筆` : "搜不到";
    return `搜「${e.query}」・${found}${e.ordered ? "・有點歌" : ""}`;
  }
  if (e.kind === "order") return `點了「${e.title}」${e.via ? `・${VIA_TEXT[e.via] ?? e.via}` : ""}`;
  const how = OUTCOME_TEXT[e.outcome] ?? e.outcome;
  return `「${e.title}」${how}${e.outcome === "skipped" ? `（唱到 ${mmss(e.played_ms)}）` : ""}`;
}
/** 搜不到的字 → 搜尋排序 to check what the search finds now */
function fixSearch(q: string) {
  view.value = "order";
  void orderSearch(q);
}

onMounted(async () => {
  await load();
  schedule();
});
onUnmounted(() => clearTimeout(timer));
</script>

<template>
  <main class="songs">
    <header class="top">
      <h1 class="logo">歌曲<b>快取</b></h1>
      <a class="back" :href="url('/')">房間列表</a>
    </header>
    <p v-if="data" class="sum">
      {{ data.songs.length }} 首・{{ mb(totalSize) }}
      <span v-if="data.ai.ai_available">・AI 分離{{ data.ai.ai_active ? "中" : "待命" }}</span>
      <span v-else>・AI 分離沒開</span>
    </p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <nav class="views seg">
      <button :class="{ on: view === 'songs' }" @click="view = 'songs'">歌曲</button>
      <button :class="{ on: view === 'reports' }" @click="view = 'reports'">
        回報<b v-if="openReports.length" class="count">{{ openReports.length }}</b>
      </button>
      <button :class="{ on: view === 'order' }" @click="view = 'order'">搜尋排序</button>
      <button :class="{ on: view === 'stats' }" @click="view = 'stats'">統計</button>
    </nav>

    <!-- ===== 統計 ===== -->
    <template v-if="view === 'stats'">
      <div class="chips">
        <button :class="{ on: statDays === 7 }" @click="statDays = 7">7 天</button>
        <button :class="{ on: statDays === 30 }" @click="statDays = 30">30 天</button>
        <button :class="{ on: statDays === 0 }" @click="statDays = 0">全部</button>
        <select v-if="stats && stats.rooms.length > 1" v-model="statRoom" aria-label="包廂">
          <option value="">所有包廂</option>
          <option v-for="r in stats.rooms" :key="r" :value="r">{{ r === "main" ? "主包廂" : r }}</option>
        </select>
      </div>
      <p v-if="!stats" class="hint">讀取中…</p>
      <template v-else>
        <p class="sum">
          點歌 {{ stats.totals.orders }} 次・唱完 {{ stats.totals.finished }}・切歌 {{ stats.totals.skipped }}
          ・搜尋 {{ stats.totals.searches }} 次・{{ stats.totals.users }} 人點過歌
        </p>

        <h3>熱門點歌</h3>
        <p v-if="!stats.songs.length" class="hint">這段期間還沒有人點歌。</p>
        <ol v-else class="picks">
          <li v-for="(song, i) in stats.songs" :key="song.track_id">
            <span class="rank">{{ i + 1 }}</span>
            <span class="name">
              <b>{{ song.title }}</b>
              <small>{{ song.artist }}<template v-if="finishRate(song)">・{{ finishRate(song) }}</template></small>
            </span>
            <i class="times">{{ song.orders }} 次</i>
          </li>
        </ol>

        <h3>熱門搜尋</h3>
        <p v-if="!stats.searches.length" class="hint">還沒有搜尋紀錄。</p>
        <ol v-else class="picks">
          <li v-for="(hit, i) in stats.searches" :key="hit.query">
            <span class="rank">{{ i + 1 }}</span>
            <span class="name">
              <b>{{ hit.query }}</b>
              <small>{{ hit.ordered ? `${hit.ordered} 次有點歌` : "搜了沒點歌" }}</small>
            </span>
            <i class="times">{{ hit.count }} 次</i>
          </li>
        </ol>

        <h3>搜不到的字</h3>
        <p class="hint">客人搜了但一筆結果都沒有。按「看搜尋結果」可以重搜一次，或到搜尋排序調整。</p>
        <ul v-if="stats.missing.length" class="picks">
          <li v-for="hit in stats.missing" :key="hit.query">
            <span class="name"><b>{{ hit.query }}</b></span>
            <i class="times">{{ hit.count }} 次</i>
            <button class="ghost" @click="fixSearch(hit.query)">看搜尋結果</button>
          </li>
        </ul>

        <h3>最常點歌的人</h3>
        <p v-if="!stats.users.length" class="hint">還沒有。</p>
        <ol v-else class="picks">
          <li v-for="(u, i) in stats.users" :key="u.user">
            <span class="rank">{{ i + 1 }}</span>
            <span class="name">
              <b>{{ u.user }}</b>
              <small>{{ u.songs }} 首不同的歌</small>
            </span>
            <i class="times">{{ u.orders }} 次</i>
          </li>
        </ol>

        <h3>最近紀錄</h3>
        <p v-if="!stats.recent.length" class="hint">還沒有。</p>
        <ul v-else class="picks recent">
          <li v-for="(e, i) in recentAll ? stats.recent : stats.recent.slice(0, 15)" :key="i">
            <span class="name">
              <b>{{ eventText(e) }}</b>
              <small>
                {{ ago(e.created_at) }}<template v-if="e.user">・{{ e.user }}</template>
                <template v-if="e.room && e.room !== 'main'">・房間 {{ e.room }}</template>
              </small>
            </span>
          </li>
        </ul>
        <div v-if="!recentAll && stats.recent.length > 15" class="row">
          <button class="ghost" @click="recentAll = true">顯示更多（最近 {{ stats.recent.length }} 筆）</button>
        </div>
      </template>
    </template>

    <!-- ===== 回報 ===== -->
    <template v-else-if="view === 'reports'">
      <p class="hint">客人在遙控器「正在播放」裡按「這首歌有問題？」送來的。修好之後按「已處理」。</p>
      <p v-if="!reports.length" class="hint">目前沒有回報。</p>
      <section v-for="r in reports" :key="r.id" class="report" :class="{ done: r.resolved_at }">
        <div class="report-head">
          <span class="name">
            <b>{{ r.title }}</b>
            <small>{{ r.artist }}</small>
          </span>
          <i class="kind" :class="r.kind">{{ REPORT_TEXT[r.kind] ?? r.kind }}</i>
        </div>
        <p class="meta">
          {{ r.count }} 人回報<template v-if="r.reporters.length">（{{ r.reporters.join("、") }}）</template>
          ・{{ ago(r.updated_at) }}<template v-if="r.room && r.room !== 'main'">・房間 {{ r.room }}</template>
          <template v-if="r.resolved_at">・{{ ago(r.resolved_at) }}處理</template>
        </p>
        <ul v-if="r.notes.length" class="notes">
          <li v-for="(n, i) in r.notes" :key="i">{{ n }}</li>
        </ul>
        <div class="row">
          <button v-if="r.cached" :disabled="working" @click="fixReport(r)">去修這首</button>
          <span v-else class="hint">這首已不在快取裡</span>
          <button v-if="!r.resolved_at" class="ghost" :disabled="working" @click="resolveReport(r)">已處理</button>
          <button v-else class="ghost" :disabled="working" @click="resolveReport(r, false)">重新打開</button>
        </div>
      </section>
    </template>

    <!-- ===== 搜尋排序 ===== -->
    <template v-else-if="view === 'order'">
      <p class="hint">
        某個關鍵字搜出來前面都是錯的？輸入那個關鍵字，把對的歌「釘到最前面」，錯的「隱藏」，按儲存。
        之後誰用同樣的關鍵字搜尋（不分簡繁、大小寫、空格），都會照這個順序。
      </p>
      <p v-if="notice" class="notice">{{ notice }}</p>
      <div class="row">
        <input v-model="orderQuery" class="grow" placeholder="搜尋關鍵字（例：黑暗的巴洛克）" @keyup.enter="orderSearch()" />
        <button :disabled="working || !orderQuery.trim()" @click="orderSearch()">搜尋</button>
      </div>

      <template v-if="orderFor">
        <h3>「{{ orderFor }}」釘在最前面</h3>
        <p v-if="!pins.length" class="hint">還沒有。從下面的搜尋結果按「釘選」。</p>
        <ol v-else class="picks pinned">
          <li v-for="(p, i) in pins" :key="p.id">
            <span class="rank">{{ i + 1 }}</span>
            <span class="name">
              <b>{{ p.title }}</b>
              <small>{{ p.artist }}・{{ hitPlatforms(p) }}<template v-if="p.duration_ms">・{{ mmss(p.duration_ms) }}</template></small>
            </span>
            <button class="ghost" :disabled="i === 0" aria-label="往上" @click="movePin(i, -1)">↑</button>
            <button class="ghost" :disabled="i === pins.length - 1" aria-label="往下" @click="movePin(i, 1)">↓</button>
            <button class="ghost" aria-label="取消釘選" @click="unpin(i)">✕</button>
          </li>
        </ol>
        <h3>目前的搜尋結果（還沒套用排序）</h3>
        <p v-if="!orderHits.length" class="hint">沒有搜尋結果。</p>
        <ul class="picks">
          <li v-for="hit in orderHits" :key="hit.id" :class="{ hidden: hidden.includes(hit.id) }">
            <span class="name">
              <b>{{ hit.title }}</b>
              <small>{{ hit.artist }}・{{ hit.cached ? "已下載" : hitPlatforms(hit) }}<template v-if="hit.duration_ms">・{{ mmss(hit.duration_ms) }}</template></small>
              <small class="mono">{{ hit.id }}</small>
            </span>
            <button class="ghost" :disabled="pinnedIds.has(hit.id)" @click="pin(hit)">{{ pinnedIds.has(hit.id) ? "已釘選" : "釘選" }}</button>
            <button class="ghost" :disabled="pinnedIds.has(hit.id)" @click="toggleHide(hit)">{{ hidden.includes(hit.id) ? "取消隱藏" : "隱藏" }}</button>
          </li>
        </ul>
        <div class="row">
          <button :disabled="working || !orderDirty" @click="saveOrder">儲存「{{ orderFor }}」的排序</button>
        </div>
      </template>

      <h3>已調整過的關鍵字</h3>
      <p v-if="!rules.length" class="hint">還沒有。</p>
      <ul v-else class="picks">
        <li v-for="rule in rules" :key="rule.query">
          <span class="name">
            <b>{{ rule.label }}</b>
            <small>釘選 {{ rule.pins.length }} 首<template v-if="rule.pins.length">（{{ rule.pins.map((p) => p.title).join("、") }}）</template>・隱藏 {{ rule.hidden.length }} 首</small>
          </span>
          <button class="ghost" @click="orderSearch(rule.label)">編輯</button>
          <button class="ghost" @click="clearOrder(rule)">清除</button>
        </li>
      </ul>
    </template>

    <template v-else>
    <div class="tools">
      <input v-model="filter" type="search" placeholder="找歌名、歌手" />
      <select v-model="sort" aria-label="排序">
        <option value="new">最新下載</option>
        <option value="name">歌名</option>
        <option value="size">最佔空間</option>
      </select>
    </div>
    <div class="chips">
      <button :class="{ on: show === 'all' }" @click="show = 'all'">全部</button>
      <button :class="{ on: show === 'nolyrics' }" @click="show = 'nolyrics'">沒歌詞 {{ counts.nolyrics }}</button>
      <button :class="{ on: show === 'line' }" @click="show = 'line'">只有逐行 {{ counts.line }}</button>
      <button :class="{ on: show === 'nomv' }" @click="show = 'nomv'">沒 MV {{ counts.nomv }}</button>
      <button :class="{ on: show === 'ai' }" @click="show = 'ai'">AI 失敗 {{ counts.ai }}</button>
      <button v-if="counts.busy" :class="{ on: show === 'busy' }" @click="show = 'busy'">使用中 {{ counts.busy }}</button>
    </div>

    <p v-if="data && !songs.length" class="hint">沒有符合的歌。</p>

    <section v-for="song in songs" :id="`song-${song.id}`" :key="song.id" class="song" :class="{ open: openId === song.id }">
      <button class="head" @click="toggle(song)">
        <span class="name">
          <b>{{ song.title }}</b>
          <small>{{ song.artist || "（沒有歌手）" }}<template v-if="song.duration_ms">・{{ mmss(song.duration_ms) }}</template></small>
        </span>
        <span class="tags">
          <i :class="lyricsLabel(song).cls">詞・{{ lyricsLabel(song).text }}</i>
          <i :class="{ bad: !song.audio }">音・{{ audioLabel(song) }}</i>
          <i v-if="song.mv">MV</i>
          <i v-if="aiLabel(song).text" :class="aiLabel(song).cls">{{ aiLabel(song).text }}</i>
          <i v-if="song.reports" class="bad">回報 {{ song.reports }}</i>
          <i class="size">{{ mb(song.size) }}</i>
        </span>
      </button>
      <p v-if="song.job" class="job" :class="song.job.state">
        {{ song.job.state === "running" ? "⏳" : song.job.state === "done" ? "✓" : "✗" }}
        {{ { audio: "換音檔", mv: "MV" }[song.job.kind] ?? song.job.kind }}：{{ song.job.message || "進行中" }}
      </p>

      <div v-if="openId === song.id" class="detail">
        <p v-if="song.busy" class="warn">{{ song.busy }}（還是可以改歌詞）</p>
        <p v-if="notice" class="notice">{{ notice }}</p>
        <nav class="seg">
          <button :class="{ on: tab === 'lyrics' }" @click="tab = 'lyrics'">歌詞</button>
          <button :class="{ on: tab === 'audio' }" @click="tab = 'audio'">音檔來源</button>
          <button :class="{ on: tab === 'mv' }" @click="tab = 'mv'">MV</button>
          <button :class="{ on: tab === 'more' }" @click="tab = 'more'">其他</button>
        </nav>

        <!-- 歌詞 -->
        <div v-if="tab === 'lyrics'" class="pane">
          <p class="meta">
            <template v-if="song.lyrics">{{ lyricsLabel(song).text }}・{{ lyricsInfo }}</template>
            <template v-else>目前沒有歌詞</template>
          </p>
          <ol v-if="lyricLines.length" class="lines">
            <li v-for="(line, i) in lyricLines" :key="i"><time>{{ mmss(line.start) }}</time>{{ line.text }}</li>
          </ol>
          <div class="row">
            <button :disabled="working || !data?.lyrics_net" @click="refetchLyrics(song)">重抓（自動挑）</button>
            <button v-if="song.lyrics" class="ghost" :disabled="working" @click="deleteLyrics(song)">刪除歌詞</button>
          </div>
          <div v-if="song.lyrics" class="row offset">
            <label>時間偏移（ms，正數＝歌詞晚一點）</label>
            <button class="ghost" @click="offset -= 250">−250</button>
            <input v-model.number="offset" type="number" step="50" />
            <button class="ghost" @click="offset += 250">+250</button>
            <button :disabled="working || offset === song.lyrics_offset_ms" @click="saveOffset(song)">存</button>
          </div>

          <h3>換一個版本</h3>
          <div class="row">
            <select v-model="provider" aria-label="歌詞來源">
              <option v-for="(name, key) in data?.lyrics_providers" :key="key" :value="key">{{ name }}</option>
            </select>
            <input v-model="lyricQuery" class="grow" @keyup.enter="searchLyrics(song)" />
            <button :disabled="working || !lyricQuery.trim()" @click="searchLyrics(song)">搜尋</button>
          </div>
          <p v-if="searched && !candidates.length" class="hint">這裡沒找到，換個來源或關鍵字試試。</p>
          <ul v-if="candidates.length" class="picks">
            <li v-for="cand in candidates" :key="`${cand.provider}-${cand.id}-${cand.mid}`" :class="{ sel: previewOf === cand }">
              <span class="name">
                <b>{{ cand.title }}</b>
                <small>{{ cand.artists.join("、") }}<template v-if="cand.album">・{{ cand.album }}</template>・{{ mmss(cand.duration_ms) }}</small>
              </span>
              <i :class="cand.score >= 4 ? 'good' : cand.score >= 2 ? '' : 'bad'" title="跟這首歌的相符程度">{{ cand.score }}</i>
              <button class="ghost" :disabled="working" @click="previewLyrics(song, cand)">預覽</button>
            </li>
          </ul>
          <div v-if="preview" class="preview">
            <p class="meta">
              {{ previewOf?.title }}・{{ preview.timing === "word" ? "逐字" : "逐行" }}・{{ preview.lines.length }} 行
            </p>
            <ol class="lines">
              <li v-for="(line, i) in preview.lines" :key="i"><time>{{ mmss(line.start) }}</time>{{ line.text }}</li>
            </ol>
            <button :disabled="working" @click="applyPreview(song)">用這個版本</button>
          </div>

          <details class="paste">
            <summary>自己貼歌詞</summary>
            <div class="row">
              <select v-model="pasteFormat" aria-label="格式">
                <option value="lrc">LRC（[mm:ss.xx]）</option>
                <option value="qrc">QRC</option>
                <option value="yrc">YRC</option>
                <option value="txt">純文字（平均分配時間）</option>
              </select>
            </div>
            <textarea v-model="pasteText" rows="8" placeholder="[00:12.34]第一句歌詞"></textarea>
            <button :disabled="working || !pasteText.trim()" @click="savePaste(song)">存入</button>
          </details>
        </div>

        <!-- 音檔 -->
        <div v-else-if="tab === 'audio'" class="pane">
          <p class="meta">
            {{ audioLabel(song) }}
            <template v-if="song.manifest.audio_song_id">・編號 {{ song.manifest.audio_song_id }}</template>
            <template v-if="song.manifest.audio_step">・{{ song.manifest.audio_step }}</template>
            <template v-if="song.audio">・{{ mb(song.audio_size) }}</template>
          </p>
          <audio v-if="song.audio" controls preload="none" :src="media(song, song.audio)"></audio>
          <p class="hint">版本不對（翻唱、Live、別首歌）就搜尋正確的版本換上：「用網易」「用 QQ」「用YouTube」只從那個來源下載，「自動」會依序試每個平台。也可以直接貼 YouTube 網址。新音檔下載成功才會替換。</p>
          <div class="row">
            <input v-model="audioQuery" class="grow" placeholder="歌名、歌手，或貼 YouTube 網址" @keyup.enter="searchAudio()" />
            <button :disabled="working || !audioQuery.trim()" @click="searchAudio()">搜尋</button>
            <button class="ghost" :disabled="working || !audioQuery.trim()" @click="searchAudio('youtube')">搜 YouTube</button>
          </div>
          <label class="check">
            <input v-model="refetchWithAudio" type="checkbox" :disabled="!!song.manifest.lyrics_manual" />
            換好後也重抓歌詞<small v-if="song.manifest.lyrics_manual">（歌詞是手動挑的，保留）</small>
          </label>
          <p v-if="audioSearched && !audioHits.length" class="hint">沒有搜尋結果。</p>
          <ul v-if="audioHits.length" class="picks">
            <li v-for="hit in audioHits" :key="hit.id">
              <span class="name">
                <b>{{ hit.title }}</b>
                <small>{{ hit.artist }}・{{ hitPlatforms(hit) }}<template v-if="hit.duration_ms">・{{ mmss(hit.duration_ms) }}</template></small>
              </span>
              <span class="srcs">
                <button
                  v-for="src in hitSources(hit)"
                  :key="src.sourceId"
                  class="ghost"
                  :title="`只從${src.name}下載，失敗也不換別的平台`"
                  :disabled="working || !!song.busy || song.job?.state === 'running'"
                  @click="replaceAudio(song, hit, src.sourceId)"
                >用{{ src.name }}</button>
                <button
                  v-if="hitSources(hit).length > 1"
                  class="ghost"
                  title="先試第一個平台，不行再換下一個"
                  :disabled="working || !!song.busy || song.job?.state === 'running'"
                  @click="replaceAudio(song, hit)"
                >自動</button>
              </span>
            </li>
          </ul>
          <div class="row">
            <button class="ghost" :disabled="working || !!song.busy || song.job?.state === 'running'" @click="replaceAudio(song, null)">同一個來源重新下載</button>
          </div>
        </div>

        <!-- MV -->
        <div v-else-if="tab === 'mv'" class="pane">
          <template v-if="song.mv">
            <video controls preload="none" muted :src="media(song, song.mv)"></video>
            <p class="meta">{{ song.mv }}・{{ mb(song.mv_size) }}</p>
          </template>
          <p v-else class="meta">目前沒有 MV</p>
          <div class="row">
            <input v-model="mvUrl" class="grow" placeholder="YouTube 網址（例：https://youtu.be/…）" />
            <button :disabled="working || !mvUrl.trim() || song.job?.state === 'running'" @click="replaceMv(song, mvUrl)">用這支</button>
          </div>
          <div class="row">
            <button class="ghost" :disabled="working || song.job?.state === 'running'" @click="replaceMv(song, '')">重新搜尋下載</button>
            <button v-if="song.mv" class="ghost" :disabled="working" @click="deleteMv(song)">刪除 MV</button>
          </div>
        </div>

        <!-- 其他 -->
        <div v-else class="pane">
          <h3>歌名</h3>
          <div class="row">
            <input v-model="editTitle" class="grow" placeholder="歌名" />
            <input v-model="editArtist" class="grow" placeholder="歌手" />
            <button :disabled="working || !editTitle.trim()" @click="saveMeta(song)">存</button>
          </div>
          <h3>AI 伴奏</h3>
          <p class="meta">
            {{ aiLabel(song).text || "還沒分離" }}
            <template v-if="song.manifest.separator">・{{ song.manifest.separator }}</template>
            <template v-if="song.stems_size">・{{ mb(song.stems_size) }}</template>
          </p>
          <p v-if="song.manifest.ai_error" class="hint">上次錯誤：{{ song.manifest.ai_error }}</p>
          <audio v-if="song.stems.length" controls preload="none" :src="media(song, song.stems[1])"></audio>
          <div class="row">
            <button
              v-if="song.stems.length || song.manifest.ai_attempts"
              class="ghost"
              :disabled="working || !!song.busy"
              @click="deleteStems(song)"
            >{{ song.stems.length ? "刪除伴奏，之後重新分離" : "清除失敗次數，再試一次" }}</button>
          </div>
          <h3>整首</h3>
          <p class="meta"><code>{{ song.id }}</code>・{{ mb(song.size) }}</p>
          <button class="danger" :disabled="working || !!song.busy" @click="deleteSong(song)">整首刪除</button>
        </div>
      </div>
    </section>
    </template>
  </main>
</template>

<style scoped>
.songs {
  max-width: 820px;
  margin: 0 auto;
  padding: 20px 16px 64px;
}
.top {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
}
.logo {
  margin: 0;
  font: 700 26px/1.2 "Chakra Petch", system-ui, sans-serif;
  letter-spacing: 0.06em;
}
.logo b {
  color: #19f0ff;
}
.back {
  color: #19f0ff;
}
.sum,
.hint,
.meta {
  color: var(--fg-dim);
  font-size: 14px;
}
.error {
  color: #ff6b8a;
}
.warn {
  color: var(--warn);
  font-size: 14px;
}
.notice {
  color: var(--ok);
  font-size: 14px;
}
input,
select,
textarea {
  font: inherit;
  color: var(--fg);
  background: var(--bg-3);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 8px 10px;
  min-width: 0;
}
textarea {
  width: 100%;
  font-family: ui-monospace, Consolas, monospace;
  font-size: 13px;
}
.tools {
  display: flex;
  gap: 8px;
  margin: 12px 0 8px;
}
.tools input {
  flex: 1;
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 12px;
}
.chips button {
  padding: 6px 12px;
  font-size: 14px;
  border-radius: 999px;
}
.chips button.on {
  background: var(--grad);
  border-color: transparent;
}
.song {
  margin: 8px 0;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--bg-2);
  overflow: hidden;
}
.song.open {
  border-color: #19f0ff;
}
.head {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 12px;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 10px 14px;
  border: 0;
  border-radius: 0;
  background: transparent;
  text-align: left;
}
.name {
  display: grid;
  min-width: 0;
}
.name b {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.name small {
  color: var(--fg-dim);
  font-size: 13px;
}
.tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.tags i,
.picks i {
  font-style: normal;
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--line);
  color: var(--fg-dim);
  white-space: nowrap;
}
i.bad {
  color: #ff6b8a;
  border-color: rgba(255, 107, 138, 0.5);
}
i.meh {
  color: var(--warn);
  border-color: rgba(255, 214, 10, 0.4);
}
i.run {
  color: #19f0ff;
  border-color: rgba(25, 240, 255, 0.5);
}
i.good {
  color: var(--ok);
  border-color: rgba(48, 209, 88, 0.5);
}
i.size {
  border-color: transparent;
}
.job {
  margin: 0;
  padding: 0 14px 8px;
  font-size: 13px;
  color: #19f0ff;
}
.job.failed {
  color: #ff6b8a;
}
.job.done {
  color: var(--ok);
}
.detail {
  padding: 4px 14px 14px;
  border-top: 1px solid var(--line);
}
.seg {
  display: flex;
  gap: 4px;
  margin: 10px 0;
}
.seg button {
  flex: 1;
  padding: 8px 6px;
  font-size: 14px;
}
.seg button.on {
  background: var(--grad);
  border-color: transparent;
}
.pane h3 {
  margin: 18px 0 6px;
  font-size: 15px;
}
.row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  margin: 8px 0;
}
.row .grow {
  flex: 1 1 160px;
}
.offset label {
  width: 100%;
  color: var(--fg-dim);
  font-size: 13px;
}
.offset input {
  width: 90px;
}
.lines {
  max-height: 260px;
  overflow: auto;
  margin: 6px 0;
  padding: 8px 10px;
  list-style: none;
  border-radius: 10px;
  background: var(--bg);
  font-size: 14px;
  line-height: 1.7;
}
.lines time {
  display: inline-block;
  width: 3.4em;
  color: var(--fg-dim);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.picks {
  margin: 6px 0;
  padding: 0;
  list-style: none;
}
.picks li {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 6px 0;
  border-bottom: 1px solid var(--line);
}
.picks li.sel {
  background: rgba(25, 240, 255, 0.06);
}
.picks .name {
  flex: 1;
}
.picks button {
  padding: 6px 10px;
  font-size: 14px;
}
.preview {
  margin: 8px 0;
  padding: 10px;
  border: 1px solid #19f0ff;
  border-radius: 12px;
}
.paste {
  margin-top: 16px;
}
.paste summary {
  cursor: pointer;
  color: var(--fg-dim);
}
.check {
  display: block;
  margin: 6px 0;
  font-size: 14px;
}
.check small {
  color: var(--fg-dim);
}
audio,
video {
  width: 100%;
  margin: 6px 0;
}
video {
  max-height: 260px;
  background: #000;
  border-radius: 10px;
}
.ghost {
  background: transparent;
}
.danger {
  color: #ff6b8a;
  border-color: rgba(255, 107, 138, 0.6);
  background: transparent;
}
.views {
  margin: 12px 0;
}
.count {
  margin-left: 6px;
  padding: 0 7px;
  border-radius: 999px;
  background: #ff6b8a;
  color: #fff;
  font-size: 12px;
}
.report {
  margin: 8px 0;
  padding: 10px 14px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--bg-2);
}
.report.done {
  opacity: 0.55;
}
.report-head {
  display: flex;
  gap: 8px;
  align-items: center;
  justify-content: space-between;
}
.kind {
  font-style: normal;
  font-size: 13px;
  padding: 2px 10px;
  border-radius: 999px;
  border: 1px solid rgba(255, 107, 138, 0.5);
  color: #ff6b8a;
  white-space: nowrap;
}
.kind.duet {
  color: var(--part-2);
  border-color: var(--part-2);
}
.notes {
  margin: 4px 0;
  padding-left: 18px;
  font-size: 14px;
}
.picks li.hidden .name {
  opacity: 0.4;
  text-decoration: line-through;
}
.picks .srcs {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 4px;
  max-width: 55%;
}
.picks .times {
  flex: none;
  color: var(--fg);
  font-variant-numeric: tabular-nums;
}
.picks.recent .name b {
  white-space: normal;
  font-weight: 500;
}
.chips select {
  padding: 6px 10px;
  font-size: 14px;
}
.picks .rank {
  color: #19f0ff;
  font-weight: 700;
  min-width: 1.4em;
}
.mono {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 11px !important;
  opacity: 0.7;
}
button:disabled {
  opacity: 0.45;
  cursor: default;
}
</style>
