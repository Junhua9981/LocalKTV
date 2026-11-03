export type KaraokeMode = "ORIGINAL" | "INSTANT" | "AI";
export type ItemStatus = "PREPARING" | "READY" | "PLAYING" | "FAILED";
export type AIStatus = "NONE" | "QUEUED" | "PROCESSING" | "READY" | "FAILED";
export type ClientRole = "display" | "host" | "remote";

export interface Track {
  id: string;
  title: string;
  artist: string;
  duration_ms: number;
  source: string;
  source_id: string;
  lyrics_offset_ms: number;
  /** YouTube-tab queueing: play the video, never crawl/show lyrics. */
  video_only?: boolean;
  /** Search response only: this copy already lives in the local cache. */
  cached?: boolean;
  /** Search merge: the same song on other platforms (their source_ids). */
  alternates?: string[];
  /** 專輯封面（平台圖片網址）；顯示時經 /api/covers/src 由伺服器代抓快取 */
  cover?: string;
}

export interface QueueItem {
  id: string;
  track: Track;
  requested_by: string;
  karaoke_mode: KaraokeMode;
  guide_vocal: number;
  /** 對唱保留原唱：這些聲部（歌詞 part，0 = 合唱）的句子放原唱；舊伺服器沒有這欄 */
  keep_parts?: number[];
  status: ItemStatus;
  ai_status: AIStatus;
  progress_message: string;
  progress_value: number | null;
  added_at: number;
}

export interface PlaybackState {
  queue_item_id: string | null;
  track_id: string | null;
  playing: boolean;
  position_ms: number;
  duration_ms: number;
  updated_at: number;
}

export type BgMode = "auto" | "mv" | "pv";
/** 畫面效果：自動（電視掉幀就省電）／完整／省電。 */
export type FxMode = "auto" | "full" | "lite";
/** 日文歌輔助：假名＋空耳 / 只要假名 / 只要空耳 / 關閉 */
export type JaAssist = "both" | "kana" | "sora" | "off";
/** 包廂音場：原音直出／包廂／演唱會。 */
export type Ambience = "clean" | "room" | "concert";

export interface SessionSnapshot {
  type?: string;
  /** Room (包廂) slug this connection is in, and its display name. */
  room?: string;
  room_name?: string;
  users: string[];
  queue: QueueItem[];
  current: QueueItem | null;
  playback: PlaybackState;
  ai_available: boolean;
  audio_source: string;
  /** /host 主要音源 choices from the server's source plug-in, 「自動」 first. */
  audio_sources: { value: string; text: string }[];
  /** The server's source plug-in serves 熱門榜 (/api/charts); no plug-in = no tab. */
  charts_available: boolean;
  /** Session-wide pitch shift in semitones (-3..3). */
  pitch_semitones: number;
  /** The display reported an AudioWorklet-capable (secure) context. */
  pitch_capable: boolean;
  /** Display background: auto = MV when there is one, else 文字PV. */
  bg_mode: BgMode;
  /** Keep the bottom karaoke lyrics while 文字PV shows. */
  pv_karaoke: boolean;
  /** 包廂音場 on the display audio. */
  ambience: Ambience;
  /** Display glow follows the beat. */
  beat_lights: boolean;
  /** 畫面效果等級（弱的智慧電視用省電）。 */
  fx_mode: FxMode;
  /** 日文歌：漢字上的假名、歌詞下的中文空耳 */
  ja_assist: JaAssist;
  /** 評分：電視聽電腦的麥克風輸入打分 */
  score: boolean;
  /** 評分時顯示音高條 */
  score_bar: boolean;
  /** 評分延遲（ms）：麥克風＋喇叭的延遲，藍牙組常見 100–300 */
  score_offset_ms: number;
  /** 麥克風外放：電視把電腦麥克風的聲音從喇叭放出來 */
  mic_monitor: boolean;
  /** 外放音量 %（0–150） */
  mic_volume: number;
  /** 迴音 0–100（電腦麥克風與手機麥克風都會加） */
  mic_echo: number;
  server_time?: number;
}

export interface Stems {
  vocals: string;
  instrumental: string;
}

/** 一筆分數（本曲高分） */
export interface SongScore {
  singer: string;
  score: number;
  room: string;
  created_at: number;
}

/** 伺服器收到電視的評分後廣播給整間包廂 */
export interface ScorePosted {
  type: "score.posted";
  item_id: string;
  track_id: string;
  title: string;
  singer: string;
  score: number;
  /** 這次之前的本曲最高分；null = 第一次有人唱 */
  best: SongScore | null;
}

export interface DisplayLoad {
  type: "display.load";
  item: QueueItem;
  media_url: string | null;
  lyrics_url: string | null;
  mv_url?: string | null;
  stems: Stems | null;
  volume: number;
  /** Session-wide pitch shift in semitones (-3..3). */
  pitch: number;
  /** 評分的標準音（pitch.json）；null = 這首還沒有 AI 人聲軌，尚未有評分 */
  pitch_url?: string | null;
}

/**
 * 一筆榜單歌曲。`no`（站台曲號）畫面不顯示，曲號對點歌沒有用處；
 * `tag` 是語別，只有「全部」語別的榜單（例如不分語別的總排行）才顯示。
 */
export interface ChartSong {
  rank: number;
  title: string;
  artist: string;
  no: string;
  tag: string;
}

/** 一份榜單 = 站台的一個分類 × 一個語別（例如 點播排行 · 粵語）。 */
export interface ChartBoardMeta {
  /** "<分類>-<語別>"，例如 hot-zh、new-yue。 */
  key: string;
  /** hot=點播排行、newrank=新歌排行、new=新進歌曲、all=總排行 */
  group: string;
  group_label: string;
  /** zh、tw、yue、en、ja、ko、vi，all=不分語別 */
  lang: string;
  label: string;
  /** 新進歌曲只是清單順序，不顯示名次。 */
  ranked: boolean;
}

export interface ChartBoard extends ChartBoardMeta {
  /** 統計期間或那一期的日期（站台有給才有）。 */
  period: string;
  fetched_at: number;
  stale: boolean;
  songs: ChartSong[];
}

export interface ChartSource {
  key: string;
  label: string;
  page: string;
  boards: ChartBoardMeta[];
}

export interface Chart {
  source: string;
  label: string;
  page: string;
  fetched_at: number;
  /** 有某份榜單抓站台失敗、目前回的是舊快取。 */
  stale: boolean;
  error: string | null;
  /** 只有抓到過的榜單；順序 = 站台的分類順序。 */
  boards: ChartBoard[];
}
