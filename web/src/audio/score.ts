/**
 * 評分的收音端：電視頁（電腦上的瀏覽器）開電腦的錄音輸入，聽音高、對回歌曲時間，
 * 交給 lib/scoring.ts 打分。
 *
 * 聲音來源是電腦的預設錄音裝置，也就是藍牙喇叭麥克風組傳進電腦的收音（手機麥克風
 * 延遲太高，不拿來評分）。這裡只分析、不播出；要從喇叭出聲是「麥克風外放」
 * （mic.ts），兩邊共用同一路收音（pcmic.ts）。
 *
 * 時間對齊：worklet 回報每筆聲音的 AudioContext 時間 t，
 *   歌曲時間 = 現在的播放位置 − (ctx 現在 − t)
 *            − 升降 Key 的延遲 − 播放延遲 − 錄音延遲 − 使用者微調
 * 播放延遲、錄音延遲是瀏覽器回報的（藍牙常常量不到），剩下的靠「評分延遲」微調
 * （遙控器設定，全場共用，store.session.score_offset_ms）。
 */

import { url } from "../base";
import { acquirePcMic, pcMicError, pcMicSupported, releasePcMic } from "./pcmic";
import type { AudioEngine } from "./engine";
import { SongScorer, hzToCents, type LineResult, type PitchDoc, type ScoreLine } from "../lib/scoring";

export type MicState = "off" | "asking" | "denied" | "missing" | "on";

/** 藍牙麥克風組常見的來回延遲；第一次用就先假設這麼多，遙控器可以再調 */
const DEFAULT_OFFSET_MS = 150;
/** 安靜時的雜音底線最少算這麼大（RMS），再小的聲音都當沒唱 */
const MIN_GATE = 0.004;
/** 大於雜音底線幾倍才算在唱 */
const GATE_RATIO = 2.5;
/** 這麼久沒收到任何聲音（RMS 幾乎 0）就提示「沒有收到麥克風聲音」 */
const SILENT_WARN_MS = 20_000;
/** 音高條畫最近這麼多筆（10ms 一筆） */
const TRAIL = 300;

export interface TrailPoint {
  t: number;
  cents: number;
  hit: number | null;
}

export async function fetchPitch(pitchUrl: string): Promise<PitchDoc | null> {
  try {
    const res = await fetch(pitchUrl, { credentials: "same-origin" });
    if (!res.ok) return null;
    const doc = (await res.json()) as PitchDoc;
    return Array.isArray(doc.notes) ? doc : null;
  } catch {
    return null;
  }
}

export class MicScorer {
  state: MicState = "off";
  /** 0..1 麥克風音量（畫面上的小音量條） */
  level = 0;
  /** 最近唱的音高（音高條） */
  trail: TrailPoint[] = [];
  scorer: SongScorer | null = null;
  offsetMs = DEFAULT_OFFSET_MS;
  onLine: ((line: LineResult) => void) | null = null;

  private stream: MediaStream | null = null;
  /** 有沒有跟 pcmic 借著麥克風（要求中也算） */
  private holding = false;
  private source: MediaStreamAudioSourceNode | null = null;
  private node: AudioWorkletNode | null = null;
  private sink: GainNode | null = null;
  private inputLatencyMs = 0;
  private floor = MIN_GATE;
  private lastSoundAt = performance.now();
  private lastSongMs = -1;
  private keyShift = 0;

  constructor(private engine: AudioEngine) {}

  /** 瀏覽器有沒有辦法開麥克風（HTTPS 或 localhost 才有） */
  static get supported(): boolean {
    return typeof AudioWorkletNode === "function" && pcMicSupported();
  }

  /** 是不是太久沒收到聲音（裝置可能沒把收音傳進電腦） */
  get silent(): boolean {
    return this.state === "on" && performance.now() - this.lastSoundAt > SILENT_WARN_MS;
  }

  async start(): Promise<MicState> {
    if (this.state === "on" || this.state === "asking") return this.state;
    if (!MicScorer.supported) return (this.state = "missing");
    this.state = "asking";
    this.holding = true;
    try {
      this.stream = await acquirePcMic();
    } catch (err) {
      this.holding = false;
      return (this.state = pcMicError(err));
    }
    if (this.state !== "asking") {
      // 等允許的時候評分被關掉了（stop() 已經還掉麥克風）
      this.stream = null;
      return this.state;
    }
    const ctx = this.engine.ctx;
    try {
      await ctx.audioWorklet.addModule(url("/worklet/score-pitch.js"));
    } catch (err) {
      console.warn("score worklet failed", err);
      this.stop();
      return (this.state = "missing");
    }
    const track = this.stream.getAudioTracks()[0];
    const latency = (track?.getSettings() as MediaTrackSettings & { latency?: number })?.latency;
    this.inputLatencyMs = typeof latency === "number" ? latency * 1000 : 0;
    this.source = ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(ctx, "ktv-score-pitch", { numberOfInputs: 1, numberOfOutputs: 1 });
    // 接到靜音的 gain 再到喇叭：不接到 destination 的話有些瀏覽器不跑 worklet
    this.sink = ctx.createGain();
    this.sink.gain.value = 0;
    this.source.connect(this.node);
    this.node.connect(this.sink);
    this.sink.connect(ctx.destination);
    this.node.port.onmessage = (e) => this.handle(e.data as { t: number; hz: number; rms: number });
    track?.addEventListener("ended", () => {
      this.stop();
      this.state = "missing";
    });
    this.lastSoundAt = performance.now();
    return (this.state = "on");
  }

  stop(): void {
    this.node?.port.close();
    this.source?.disconnect();
    this.node?.disconnect();
    this.sink?.disconnect();
    if (this.holding) releasePcMic();
    this.holding = false;
    this.stream = this.source = this.node = this.sink = null;
    if (this.state === "on" || this.state === "asking") this.state = "off";
  }

  /** 換歌：doc = null 就是這首尚未有評分；lines 是歌曲時間的歌詞行（逐字歌詞帶 words） */
  load(doc: PitchDoc | null, lines: ScoreLine[]): void {
    this.scorer = doc && doc.notes.length ? new SongScorer(doc, lines) : null;
    this.scorer?.setKey(this.keyShift);
    if (this.scorer) this.scorer.onLine = (line) => this.onLine?.(line);
    this.trail = [];
    this.lastSongMs = -1;
  }

  setKey(semitones: number): void {
    this.keyShift = semitones;
    this.scorer?.setKey(semitones);
  }

  setOffset(ms: number): void {
    this.offsetMs = Math.max(0, Math.min(600, Math.round(ms)));
  }

  /** 播放延遲（喇叭那一段）＋錄音延遲＋微調，毫秒 */
  get totalDelayMs(): number {
    const ctx = this.engine.ctx;
    const out = ((ctx as AudioContext & { outputLatency?: number }).outputLatency || ctx.baseLatency || 0) * 1000;
    return out + this.inputLatencyMs + this.engine.pitchDelayMs + this.offsetMs;
  }

  private handle(msg: { t: number; hz: number; rms: number }): void {
    const now = performance.now();
    if (msg.rms > 1e-4) this.lastSoundAt = now;
    // 雜音底線：安靜時慢慢往上爬、遇到更小的馬上降下來
    this.floor = msg.rms < this.floor ? Math.max(1e-5, msg.rms) : this.floor * 1.0007;
    const gate = Math.max(MIN_GATE, this.floor * GATE_RATIO);
    this.level = Math.max(Math.min(1, msg.rms * 6), this.level * 0.85);
    const scorer = this.scorer;
    if (!scorer || !this.engine.playing) return;
    const ctx = this.engine.ctx;
    const t = this.engine.positionMs - (ctx.currentTime - msg.t) * 1000 - this.totalDelayMs;
    if (t < 0) return;
    if (this.lastSongMs >= 0 && t < this.lastSongMs - 500) scorer.rewind(t); // 往回拉了
    this.lastSongMs = t;
    const cents = msg.hz > 0 && msg.rms > gate ? hzToCents(msg.hz) : null;
    const hit = scorer.feed({ t, cents });
    if (cents !== null) {
      this.trail.push({ t, cents, hit });
      if (this.trail.length > TRAIL) this.trail.splice(0, this.trail.length - TRAIL);
    }
  }
}
