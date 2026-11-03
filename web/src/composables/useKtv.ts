/**
 * Single WebSocket client + reactive session store.
 *
 * The server is the source of truth for the queue: clients never guess, they
 * wait for the next `session.snapshot`.
 */

import { reactive } from "vue";
import { room, roomPage, roomUrl, url } from "../base";
import type { SongScore } from "../types";
import type { Ambience, BgMode, Chart, ChartSource, ClientRole, FxMode, JaAssist, PlaybackState, SessionSnapshot } from "../types";

type Handler = (msg: any) => void;

const listeners = new Map<string, Set<Handler>>();

export const store = reactive({
  connected: false,
  role: "remote" as ClientRole,
  name: "",
  session: {
    users: [] as string[],
    queue: [],
    current: null,
    playback: {
      queue_item_id: null,
      track_id: null,
      playing: false,
      position_ms: 0,
      duration_ms: 0,
      updated_at: 0,
    } as PlaybackState,
    ai_available: false,
    audio_source: "auto",
    audio_sources: [],
    charts_available: false,
    pitch_semitones: 0,
    pitch_capable: false,
    bg_mode: "auto",
    pv_karaoke: true,
    ambience: "room",
    beat_lights: true,
    fx_mode: "auto",
    ja_assist: "both",
    score: true,
    score_bar: true,
    score_offset_ms: 150,
    mic_monitor: false,
    mic_volume: 100,
    mic_echo: 30,
    room: room(),
    room_name: "",
  } as SessionSnapshot,
  serverError: "" as string,
});

let ws: WebSocket | null = null;
let retryDelay = 500;
let closedByUs = false;

const connectedHooks: Array<() => void> = [];

/** hello 已送出、連線可用時呼叫（重連也會再呼叫一次）。 */
export function onConnected(fn: () => void): void {
  connectedHooks.push(fn);
}

export function on(type: string, handler: Handler): () => void {
  if (!listeners.has(type)) listeners.set(type, new Set());
  listeners.get(type)!.add(handler);
  return () => listeners.get(type)?.delete(handler);
}

function emit(msg: any) {
  const set = listeners.get(msg?.type);
  if (!set) return;
  for (const handler of [...set]) {
    try {
      handler(msg);
    } catch (err) {
      console.error("ws handler error", msg?.type, err);
    }
  }
}

export function connect(role: ClientRole, name = ""): void {
  store.role = role;
  store.name = name;
  closedByUs = false;
  open();
}

function open() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${proto}://${location.host}${url("/ws")}?room=${encodeURIComponent(room())}`);
  ws = socket;

  socket.onopen = () => {
    store.connected = true;
    retryDelay = 500;
    socket.send(JSON.stringify({ type: "hello", role: store.role, name: store.name }));
    for (const hook of [...connectedHooks]) {
      try {
        hook();
      } catch (err) {
        console.error("onConnected hook error", err);
      }
    }
  };

  socket.onmessage = (event) => {
    let msg: any;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }
    dispatch(msg);
  };

  socket.onclose = async (event) => {
    store.connected = false;
    if (ws === socket) ws = null;
    if (event.code === 4404) {
      // the room was deleted (or never existed): retrying cannot help
      store.serverError = "沒有這個房間";
      return;
    }
    if (!closedByUs) {
      // Expired/revoked login: reload this page instead of retrying forever —
      // the server's gate sends a TV to /pair and everyone else to /login.
      try {
        const response = await fetch(url("/api/health"), { cache: "no-store" });
        if (response.status === 401) {
          location.reload();
          return;
        }
      } catch { /* server temporarily offline — keep the usual retry */ }
      if (!closedByUs) scheduleReconnect();
    }
  };

  socket.onerror = () => socket.close();
}

function scheduleReconnect() {
  setTimeout(() => {
    if (!closedByUs && !ws) open();
  }, retryDelay);
  retryDelay = Math.min(retryDelay * 2, 5000);
}

function dispatch(msg: any) {
  switch (msg.type) {
    case "session.snapshot":
      if (msg.room && msg.room !== room()) {
        // the server put us in another room (a TV moved by the admin, a
        // guest's own room): follow it so the URL and QR match
        location.replace(url(roomUrl(msg.room, roomPage())));
        return;
      }
      store.session = {
        room: msg.room ?? room(),
        room_name: msg.room_name ?? "",
        users: msg.users ?? [],
        queue: msg.queue ?? [],
        current: msg.current ?? null,
        playback: msg.playback ?? store.session.playback,
        ai_available: !!msg.ai_available,
        audio_source: msg.audio_source ?? "auto",
        audio_sources: Array.isArray(msg.audio_sources) ? msg.audio_sources : [],
        charts_available: !!msg.charts_available,
        pitch_semitones: typeof msg.pitch_semitones === "number" ? msg.pitch_semitones : 0,
        pitch_capable: !!msg.pitch_capable,
        bg_mode: msg.bg_mode === "mv" || msg.bg_mode === "pv" ? msg.bg_mode : "auto",
        pv_karaoke: msg.pv_karaoke !== false,
        ambience: msg.ambience === "clean" || msg.ambience === "concert" ? msg.ambience : "room",
        beat_lights: msg.beat_lights !== false,
        fx_mode: msg.fx_mode === "full" || msg.fx_mode === "lite" ? msg.fx_mode : "auto",
        ja_assist: ["kana", "sora", "off"].includes(msg.ja_assist) ? msg.ja_assist : "both",
        score: msg.score !== false,
        score_bar: msg.score_bar !== false,
        score_offset_ms: typeof msg.score_offset_ms === "number" ? msg.score_offset_ms : 150,
        mic_monitor: msg.mic_monitor === true,
        mic_volume: typeof msg.mic_volume === "number" ? msg.mic_volume : 100,
        mic_echo: typeof msg.mic_echo === "number" ? msg.mic_echo : 30,
        server_time: msg.server_time,
      };
      break;
    case "playback.state":
      store.session.playback = {
        queue_item_id: msg.queue_item_id ?? null,
        track_id: msg.track_id ?? null,
        playing: !!msg.playing,
        position_ms: msg.position_ms ?? 0,
        duration_ms: msg.duration_ms ?? store.session.playback.duration_ms,
        updated_at: msg.updated_at ?? Date.now() / 1000,
      };
      break;
    case "error":
      store.serverError = String(msg.detail ?? "error");
      break;
  }
  emit(msg);
}

export function send(msg: Record<string, unknown>): boolean {
  if (!ws || ws.readyState !== WebSocket.OPEN) return false;
  ws.send(JSON.stringify(msg));
  return true;
}

// -- convenience actions -------------------------------------------------

export function queueAdd(trackIdPayload: Record<string, unknown>) {
  return send({ type: "queue.add", ...trackIdPayload });
}

export function queueRemove(id: string) {
  return send({ type: "queue.remove", id });
}

export function queueMove(id: string, index: number) {
  return send({ type: "queue.move", id, index });
}

export function queueBump(id: string) {
  return send({ type: "queue.bump", id });
}

export function playback(action: "play" | "pause" | "seek" | "skip", positionMs?: number) {
  return send({ type: `playback.${action}`, position_ms: positionMs });
}

export function setMode(mode: string, id?: string) {
  return send({ type: "karaoke.mode", mode, id });
}

export function setGuide(guide: number, id?: string) {
  return send({ type: "karaoke.guide", guide, id });
}

/** 對唱保留原唱：哪些聲部的句子放原唱（[] = 全部自己唱） */
export function setKeepParts(parts: number[], id?: string) {
  return send({ type: "karaoke.keep", parts, id });
}

/** 回報這首歌有問題：歌詞錯、版本錯、對唱沒分色；管理員在 /songs 處理。 */
export function reportSong(trackId: string, kind: "lyrics" | "track" | "duet" | "other", note = "") {
  return send({ type: "song.report", track_id: trackId, kind, note, name: store.name });
}

export function setVolume(value: number) {
  return send({ type: "volume", value });
}

export function setPitch(semitones: number) {
  return send({ type: "pitch.set", value: semitones });
}

/** display 回報自己是否具備 AudioWorklet（安全環境）；host 據此停用控制項。 */
export function reportPitchCapable(capable: boolean) {
  return send({ type: "display.pitch_capable", value: capable });
}

/** 大螢幕畫面與氣氛：背景、文字PV 時的卡拉OK歌詞、包廂音場、節拍燈光、評分、麥克風外放。全場共用。 */
export function setVisual(opts: {
  bg_mode?: BgMode;
  pv_karaoke?: boolean;
  ambience?: Ambience;
  beat_lights?: boolean;
  fx_mode?: FxMode;
  ja_assist?: JaAssist;
  score?: boolean;
  score_bar?: boolean;
  score_offset_ms?: number;
  mic_monitor?: boolean;
  mic_volume?: number;
  mic_echo?: number;
}) {
  return send({ type: "display.visual", ...opts });
}

export function setAudioSource(value: string) {
  return send({ type: "audio.source", value });
}

export function setLyricsOffset(trackId: string, offsetMs: number) {
  return send({ type: "lyrics.offset", track_id: trackId, offset_ms: offsetMs });
}

/**
 * 搜尋。`client` 是這支遙控器的代號：同一支送出新搜尋後，伺服器會放棄它
 * 還沒送出的舊搜尋，不浪費音源的請求次數。只有打字搜尋才帶它；榜單點歌
 * 一次可能點好幾首，不能互相取消。
 */
export async function search(
  query: string,
  source: "main" | "youtube" = "main",
  signal?: AbortSignal,
  client?: string,
  page = 1,
): Promise<any[]> {
  const cid = client ? `&cid=${encodeURIComponent(client)}` : "";
  const pg = page > 1 ? `&page=${page}` : "";
  const res = await fetch(
    `${url("/api/search")}?q=${encodeURIComponent(query)}&source=${source}${cid}${pg}`,
    { signal },
  );
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `search failed (${res.status})`);
  }
  return res.json();
}

/** 榜單來源清單（音源外掛提供），含每家的分類。 */
export async function chartIndex(signal?: AbortSignal): Promise<ChartSource[]> {
  const res = await fetch(url("/api/charts"), { signal });
  if (!res.ok) throw new Error(`charts failed (${res.status})`);
  return res.json();
}

/**
 * 抓一份榜單。伺服器端有週級快取（站台每週更新一次），所以這個呼叫
 * 幾乎永遠是讀本機檔案，不會打到站台。
 */
export async function fetchChart(source: string, signal?: AbortSignal): Promise<Chart> {
  const res = await fetch(`${url("/api/charts")}/${encodeURIComponent(source)}`, { signal });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `chart failed (${res.status})`);
  }
  return res.json();
}

/**
 * 熱門榜封面：每首回圖檔名、""（查無，用首字方塊）、null（伺服器還在查，晚點再問）。
 * 伺服器只查被問到的歌，所以只送畫面上看得到的列。
 */
export async function fetchCovers(songs: { title: string; artist: string }[]): Promise<(string | null)[]> {
  const qs = songs.map((s) => `q=${encodeURIComponent(`${s.title}\t${s.artist}`)}`).join("&");
  const res = await fetch(`${url("/api/covers")}?${qs}`);
  if (!res.ok) throw new Error(`covers failed (${res.status})`);
  return (await res.json()).covers;
}

export function coverUrl(name: string): string {
  return url(`/api/covers/img/${encodeURIComponent(name)}`);
}

/** 搜尋結果／清單裡的歌：搜尋時平台順便給的封面，伺服器代抓並快取；"" = 用首字方塊 */
export function trackCover(track: { cover?: string }): string {
  return track.cover ? url(`/api/covers/src?u=${encodeURIComponent(track.cover)}`) : "";
}

/** 封面載入失敗就藏起來，露出底下的首字方塊 */
export function hideImg(event: Event) {
  (event.target as HTMLElement).style.display = "none";
}

/** 本曲高分（所有房間），最高的在前面 */
export async function fetchSongScores(trackId: string): Promise<SongScore[]> {
  try {
    const res = await fetch(`${url("/api/scores")}?track_id=${encodeURIComponent(trackId)}`);
    return res.ok ? res.json() : [];
  } catch {
    return [];
  }
}
