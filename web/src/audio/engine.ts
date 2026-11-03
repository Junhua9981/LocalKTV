/**
 * Display audio engine — the only place where sound is produced.
 *
 * ORIGINAL : dry signal
 * INSTANT  : centre cancellation (L - R) with the low band kept (< ~150 Hz),
 *            blended with the dry signal by the guide-vocal amount
 *            instant × (1 - guide) + original × guide
 * AI       : instrumental + vocals × guide (falls back to INSTANT when the
 *            stems are not ready — AI is an enhancement, never a requirement)
 * 對唱保留原唱：原唱那位的句子 guide 當成 1（setOriginal）
 *
 * Output chain: master → (pitch worklet) → 包廂音場 → speakers.  The 音場
 * (clean / room / concert) adds reverb, stereo width, a bass shelf and a
 * compressor after the key shift so the reverb tail is never pitch-shifted.
 */

import type { Ambience, KaraokeMode, Stems } from "../types";
import { url } from "../base";

const LOW_CUTOFF_HZ = 150;
const PITCH_MIN = -3;
const PITCH_MAX = 3;
/** 半音切換的平滑時間常數（秒）。太長會聽出整段滑音，4ms ≈ 18ms 內到位。 */
const PITCH_SMOOTH_S = 0.004;
/** 音場切換的過渡時間常數（秒）：慢慢淡入淡出，不會一下子「啪」地變聲。 */
const FX_SMOOTH_S = 0.25;
/** 音量變化的時間常數（秒）：約 0.1 秒內到位，拖滑桿聽起來是連續的。 */
const VOLUME_SMOOTH_S = 0.03;
/** 對唱保留原唱的切換：約 80ms 內淡入／淡出，換句時不會「啪」一聲。 */
const DUET_SMOOTH_S = 0.025;

interface AmbienceParams {
  room: number; // 小殘響（包廂）送出量
  hall: number; // 大殘響（演唱會）送出量
  width: number; // 立體聲加寬：side 額外增益
  bassDb: number; // 120Hz 低音 shelf
  airDb: number; // 8kHz 高音 shelf，讓殘響不悶
  threshold: number; // 壓縮器門檻 dB
  ratio: number;
  /** 輸出音量修正：Chrome 的壓縮器會自動補音量，這裡扣回來，切換時才不會突然變大聲。 */
  makeupDb: number;
}

const AMBIENCE: Record<Ambience, AmbienceParams> = {
  clean: { room: 0, hall: 0, width: 0, bassDb: 0, airDb: 0, threshold: 0, ratio: 1, makeupDb: 0 },
  room: { room: 0.22, hall: 0, width: 0.18, bassDb: 3, airDb: 1, threshold: -20, ratio: 3, makeupDb: -3.5 },
  concert: { room: 0.06, hall: 0.3, width: 0.35, bassDb: 4.5, airDb: 2, threshold: -22, ratio: 4, makeupDb: -4.5 },
};

/**
 * 合成的殘響脈衝響應：左右聲道各自的衰減白噪音（左右不相關 → 聽起來包圍）。
 * 早期的幾毫秒先壓低，避免和原音疊在一起變「糊」。
 */
export function makeImpulse(ctx: BaseAudioContext, seconds: number, decay: number): AudioBuffer {
  const length = Math.max(1, Math.floor(ctx.sampleRate * seconds));
  const buffer = ctx.createBuffer(2, length, ctx.sampleRate);
  const fadeIn = Math.floor(ctx.sampleRate * 0.006);
  for (let ch = 0; ch < 2; ch += 1) {
    const data = buffer.getChannelData(ch);
    for (let i = 0; i < length; i += 1) {
      const t = i / length;
      const env = Math.pow(1 - t, decay) * (i < fadeIn ? i / fadeIn : 1);
      data[i] = (Math.random() * 2 - 1) * env;
    }
  }
  return buffer;
}

export class AudioEngine {
  readonly ctx: AudioContext;
  private main: HTMLAudioElement;
  private vocalsEl: HTMLAudioElement | null = null;
  private instEl: HTMLAudioElement | null = null;
  private vocalsSrc: MediaElementAudioSourceNode | null = null;
  private instSrc: MediaElementAudioSourceNode | null = null;

  private master: GainNode;
  /** 音場的入口：master（或升降 key 的 worklet）接到這裡。 */
  private fxIn: GainNode;
  private roomSend: GainNode;
  private hallSend: GainNode;
  private sideGain: GainNode;
  private bassShelf: BiquadFilterNode;
  private airShelf: BiquadFilterNode;
  private comp: DynamicsCompressorNode;
  private makeup: GainNode;
  private ambience: Ambience = "room";
  /** Read-only tap on what is heard — the 文字PV background follows the beat. */
  readonly analyser: AnalyserNode;
  private dryGain: GainNode;
  private instantGain: GainNode;
  private vocGain: GainNode;
  private instGain: GainNode;

  private mode: KaraokeMode = "ORIGINAL";
  private guide = 0;
  /** 對唱保留原唱：現在這句是原唱那位的 → 人聲全開（不管導唱滑桿）。 */
  private original = false;
  private volume = 0.9;
  private stems: Stems | null = null;
  private stemsReady = false;
  /**
   * 上一次 play() 被瀏覽器擋下（NotAllowedError）。Safari 的自動播放是「每個
   * <audio> 各自」解鎖，AudioContext 跑起來不代表元素能播 → 這時也要叫人點一下。
   */
  private blocked = false;

  private pitchNode: AudioWorkletNode | null = null;
  private pitchInit: Promise<boolean> | null = null;
  private pitchValue = 0;
  private pitchDelay = 0;

  onEnded: (() => void) | null = null;
  onError: ((message: string) => void) | null = null;
  /** The song's audio itself failed to load/decode (not autoplay blocking). */
  onMediaError: (() => void) | null = null;
  onStateChange: (() => void) | null = null;

  /**
   * AudioWorklet 需要安全環境（localhost 或 HTTPS）。開在內網 IP 的
   * `http://172.x.x.x` 上 `isSecureContext` 為 false、`audioWorklet` 根本
   * 不存在 → 直接判定不支援，host 控制項隨之停用。
   */
  static get pitchSupported(): boolean {
    return (
      typeof AudioWorkletNode === "function" &&
      isSecureContext &&
      typeof AudioContext === "function" &&
      "audioWorklet" in AudioContext.prototype
    );
  }

  constructor() {
    this.ctx = new AudioContext();
    this.master = this.ctx.createGain();
    this.master.gain.value = this.volume;
    const fx = this.buildAmbience();
    this.fxIn = fx.fxIn;
    this.roomSend = fx.roomSend;
    this.hallSend = fx.hallSend;
    this.sideGain = fx.sideGain;
    this.bassShelf = fx.bassShelf;
    this.airShelf = fx.airShelf;
    this.comp = fx.comp;
    this.makeup = fx.makeup;
    this.master.connect(this.fxIn);
    this.applyAmbience(true);
    this.analyser = this.ctx.createAnalyser();
    this.analyser.fftSize = 1024;
    this.analyser.smoothingTimeConstant = 0.6;
    this.master.connect(this.analyser); // a dead end: never reaches the speakers
    // AudioWorklet 只在安全環境（localhost / HTTPS）可用；失敗就維持直連
    void this.initPitch();

    this.main = document.createElement("audio");
    this.main.crossOrigin = "anonymous";
    this.main.preload = "auto";
    this.main.addEventListener("ended", () => this.onEnded?.());
    this.main.addEventListener("error", () => {
      const code = (this.main as HTMLMediaElement & { error?: { code: number } }).error?.code;
      this.onError?.(`audio error (${code ?? "?"})`);
      this.onMediaError?.();
    });

    const mainSrc = this.ctx.createMediaElementSource(this.main);

    // dry path (ORIGINAL / guide blend)
    this.dryGain = this.ctx.createGain();
    this.dryGain.gain.value = 1;
    mainSrc.connect(this.dryGain);
    this.dryGain.connect(this.master);

    // centre cancellation: L - R
    const splitter = this.ctx.createChannelSplitter(2);
    mainSrc.connect(splitter);
    const midSum = this.ctx.createGain();
    const lGain = this.ctx.createGain();
    const rGain = this.ctx.createGain();
    lGain.gain.value = 1;
    rGain.gain.value = -1;
    splitter.connect(lGain, 0);
    splitter.connect(rGain, 1);
    lGain.connect(midSum);
    rGain.connect(midSum);

    // keep the low band from the original signal (mono so both channels match)
    const monoSum = this.ctx.createGain();
    monoSum.gain.value = 0.5;
    splitter.connect(monoSum, 0);
    splitter.connect(monoSum, 1);
    const lowpass = this.ctx.createBiquadFilter();
    lowpass.type = "lowpass";
    lowpass.frequency.value = LOW_CUTOFF_HZ;
    lowpass.Q.value = 0.707;
    monoSum.connect(lowpass);

    const instantSum = this.ctx.createGain();
    midSum.connect(instantSum);
    lowpass.connect(instantSum);
    this.instantGain = this.ctx.createGain();
    this.instantGain.gain.value = 0;
    instantSum.connect(this.instantGain);
    this.instantGain.connect(this.master);

    // AI stems
    this.vocGain = this.ctx.createGain();
    this.instGain = this.ctx.createGain();
    this.vocGain.gain.value = 0;
    this.instGain.gain.value = 0;
    this.vocGain.connect(this.master);
    this.instGain.connect(this.master);

    this.applyGains();
    setInterval(() => this.syncStems(), 300);
  }

  // -- lifecycle ---------------------------------------------------------
  load(opts: {
    mediaUrl: string;
    stems: Stems | null;
    mode: KaraokeMode;
    guide: number;
    volume: number;
    pitch?: number;
    autoplay?: boolean;
  }) {
    this.stems = opts.stems;
    this.stemsReady = false;
    this.mode = opts.mode;
    this.guide = opts.guide;
    this.original = false;
    this.volume = opts.volume;
    this.master.gain.cancelScheduledValues(this.ctx.currentTime);
    this.master.gain.value = this.volume;
    if (typeof opts.pitch === "number") this.setPitch(opts.pitch);
    this.main.src = opts.mediaUrl;
    this.main.load();
    this.teardownStems();
    this.applyGains();
    if (opts.autoplay !== false) void this.play();
  }

  /**
   * 必須在使用者手勢（點擊）當下同步呼叫。WebKit 只在手勢中呼叫過 play() 的
   * 媒體元素之後才放行自動播放，所以這裡對每個 <audio> 都 play() 一次再停掉
   * （之後換歌只換 src，同一個元素一直有效）；AudioContext 的 resume 也要先發。
   */
  async unlock(): Promise<boolean> {
    this.ensureStemEls();
    for (const el of [this.main, this.vocalsEl!, this.instEl!]) {
      if (!el.paused) continue;
      el.play().catch(() => undefined); // 立刻 pause 會讓它 AbortError，沒關係
      el.pause();
    }
    this.blocked = false;
    const resumed = this.ctx.state === "running" ? Promise.resolve() : this.ctx.resume();
    try {
      await resumed;
    } catch { /* 狀態看 ctx.state */ }
    this.onStateChange?.();
    return this.ctx.state === "running";
  }

  /** 不在手勢裡也能試的：只叫 AudioContext 恢復（先前點過就會成功）。 */
  resume(): void {
    if (this.ctx.state !== "running") void this.ctx.resume().catch(() => undefined);
  }

  async play(): Promise<void> {
    // 不能先 await resume：Safari 過了 await 就不算在手勢裡，main.play() 會被擋
    this.resume();
    try {
      await this.main.play();
    } catch (err) {
      if ((err as DOMException)?.name === "NotAllowedError") {
        this.blocked = true; // display 會再顯示「點一下」，點了就從這裡接著播
        this.onStateChange?.();
        return;
      }
      this.onError?.(`play failed: ${err}`);
      return;
    }
    this.blocked = false;
    if (this.mode === "AI" && this.stems) await this.activateStems();
    this.onStateChange?.();
  }

  pause(): void {
    this.main.pause();
    this.vocalsEl?.pause();
    this.instEl?.pause();
    this.onStateChange?.();
  }

  seek(ms: number): void {
    const seconds = Math.max(0, ms / 1000);
    this.main.currentTime = seconds;
    if (this.stemsReady) {
      if (this.vocalsEl) this.vocalsEl.currentTime = seconds;
      if (this.instEl) this.instEl.currentTime = seconds;
    }
    this.onStateChange?.();
  }

  // -- controls ----------------------------------------------------------
  setMode(mode: KaraokeMode): void {
    this.mode = mode;
    if (mode === "AI" && this.stems && !this.stemsReady) void this.activateStems();
    if (mode !== "AI") this.pauseStems();
    this.applyGains();
  }

  setGuide(guide: number): void {
    this.guide = Math.max(0, Math.min(1, guide));
    this.applyGains();
  }

  /**
   * 對唱保留原唱：display 照歌詞時軸每幀告訴引擎「現在要不要放原唱」。
   * AI 是把人聲軌開到 100%；即時消人聲是改放原曲。原曲模式本來就有人聲。
   */
  setOriginal(on: boolean): void {
    if (on === this.original) return;
    this.original = on;
    this.applyGains(DUET_SMOOTH_S);
  }

  /** 拖滑桿時一格一格送來：用很短的時間常數滑過去，不會一階一階跳出喀喀聲。 */
  setVolume(volume: number): void {
    this.volume = Math.max(0, Math.min(1, volume));
    const now = this.ctx.currentTime;
    this.master.gain.cancelScheduledValues(now);
    this.master.gain.setTargetAtTime(this.volume, now, VOLUME_SMOOTH_S);
  }

  /**
   * 升降 key（半音）。AudioParam 走 setTargetAtTime 平滑過渡，
   * 避免參數瞬跳造成的咔聲。
   */
  setPitch(semitones: number): void {
    this.pitchValue = Math.max(PITCH_MIN, Math.min(PITCH_MAX, semitones));
    if (this.pitchNode) this.pushPitch();
    else void this.initPitch();
  }

  /** 包廂音場：clean = 原音直出，room = 包廂，concert = 演唱會。 */
  setAmbience(value: Ambience): void {
    if (!(value in AMBIENCE) || value === this.ambience) return;
    this.ambience = value;
    this.applyAmbience();
  }

  setStems(stems: Stems | null): void {
    // 每次調導唱、換模式，伺服器都會把同一組 stem 再送一次；同樣的檔案就不要重載，
    // 否則兩條音軌每一格都重新載入、重新對時，聽起來就是滋滋聲。
    const same =
      stems?.vocals === this.stems?.vocals && stems?.instrumental === this.stems?.instrumental;
    if (same) return;
    this.stems = stems;
    if (!stems) {
      this.teardownStems();
    } else if (this.mode === "AI" && this.stemsReady) {
      // new stems for the same track: reload sources
      this.teardownStems();
      void this.activateStems();
    }
    this.applyGains();
  }

  // -- state -------------------------------------------------------------
  get positionMs(): number {
    return Math.round(this.main.currentTime * 1000);
  }

  get durationMs(): number {
    return Math.round((this.main.duration || 0) * 1000);
  }

  get playing(): boolean {
    return !this.main.paused;
  }

  get audioReady(): boolean {
    return this.ctx.state === "running" && !this.blocked;
  }

  /**
   * worklet 內部的固定延遲（15–45ms）。升降 key 走延遲線，這段延遲會讓
   * 音訊比歌詞 MV 晚 → display 必須從歌詞時軸扣掉。
   */
  get pitchDelayMs(): number {
    return this.pitchDelay;
  }

  get currentMode(): KaraokeMode {
    return this.mode;
  }

  get usingStems(): boolean {
    return this.mode === "AI" && this.stemsReady;
  }

  // -- internals ---------------------------------------------------------
  /** AI without stems is exactly the INSTANT fallback (spec §8). */
  private effectiveMode(): "ORIGINAL" | "INSTANT" | "AI" {
    if (this.mode === "AI") return this.stemsReady ? "AI" : "INSTANT";
    return this.mode;
  }

  /**
   * 掛上升降 key 的 AudioWorklet：master → worklet → destination。
   * 任何失敗（非安全環境、模組載入失敗）都靜默降級為原本的直連。
   * 置換必須在任何聲音播出前完成，否則插入瞬間會有咔聲。
   */
  private initPitch(): Promise<boolean> {
    if (!this.pitchInit) {
      this.pitchInit = (async () => {
        if (!AudioEngine.pitchSupported) return false;
        try {
          await this.ctx.audioWorklet.addModule(url("/worklet/pitch-processor.js"));
          const node = new AudioWorkletNode(this.ctx, "ktv-pitch", {
            numberOfInputs: 1,
            numberOfOutputs: 1,
            outputChannelCount: [2],
          });
          node.port.onmessage = (event) => {
            const delayMs = (event.data as { delayMs?: unknown } | undefined)?.delayMs;
            if (typeof delayMs === "number") this.pitchDelay = delayMs;
          };
          this.master.disconnect(this.fxIn);
          this.master.connect(node);
          node.connect(this.fxIn);
          this.pitchNode = node;
          this.pushPitch();
          return true;
        } catch (err) {
          console.warn("pitch worklet unavailable", err);
          if (!this.pitchNode) {
            try {
              this.master.connect(this.fxIn);
            } catch {
              /* 已經連過了 */
            }
          }
          return false;
        }
      })();
    }
    return this.pitchInit;
  }

  /**
   * fxIn ─┬─ 加寬（L/R ± side）─ 低音 ─ 高音 ─┬─ 壓縮 ─ 音量修正 ─ 喇叭
   *       └─ 高通/低通 ─ 預延遲 ─┬─ 包廂殘響 ─┘
   *                              └─ 演唱會殘響 ┘
   * 兩個殘響一直接著，切換只動送出量，所以不會有換 buffer 的咔聲。
   */
  private buildAmbience() {
    const ctx = this.ctx;
    const fxIn = ctx.createGain();
    // 單聲道的歌也當成雙聲道處理，否則右聲道會不見
    fxIn.channelCount = 2;
    fxIn.channelCountMode = "explicit";
    fxIn.channelInterpretation = "speakers";

    // stereo width: L' = L + w·(L−R)/2, R' = R − w·(L−R)/2
    const split = ctx.createChannelSplitter(2);
    const merge = ctx.createChannelMerger(2);
    fxIn.connect(split);
    split.connect(merge, 0, 0);
    split.connect(merge, 1, 1);
    const sideL = ctx.createGain();
    const sideR = ctx.createGain();
    sideL.gain.value = 0.5;
    sideR.gain.value = -0.5;
    const sideGain = ctx.createGain();
    sideGain.gain.value = 0;
    split.connect(sideL, 0);
    split.connect(sideR, 1);
    sideL.connect(sideGain);
    sideR.connect(sideGain);
    const sideNeg = ctx.createGain();
    sideNeg.gain.value = -1;
    sideGain.connect(merge, 0, 0);
    sideGain.connect(sideNeg);
    sideNeg.connect(merge, 0, 1);

    const bassShelf = ctx.createBiquadFilter();
    bassShelf.type = "lowshelf";
    bassShelf.frequency.value = 120;
    const airShelf = ctx.createBiquadFilter();
    airShelf.type = "highshelf";
    airShelf.frequency.value = 8000;
    merge.connect(bassShelf);
    bassShelf.connect(airShelf);

    const comp = ctx.createDynamicsCompressor();
    comp.knee.value = 12;
    comp.attack.value = 0.01;
    comp.release.value = 0.25;
    const makeup = ctx.createGain();
    airShelf.connect(comp);
    comp.connect(makeup);
    makeup.connect(ctx.destination);

    // reverb: no low end (muddy) and no harsh top, a little pre-delay
    const hp = ctx.createBiquadFilter();
    hp.type = "highpass";
    hp.frequency.value = 220;
    const lp = ctx.createBiquadFilter();
    lp.type = "lowpass";
    lp.frequency.value = 7000;
    const preDelay = ctx.createDelay(0.1);
    preDelay.delayTime.value = 0.025;
    fxIn.connect(hp);
    hp.connect(lp);
    lp.connect(preDelay);
    const roomVerb = ctx.createConvolver();
    roomVerb.buffer = makeImpulse(ctx, 1.1, 3.2);
    const hallVerb = ctx.createConvolver();
    hallVerb.buffer = makeImpulse(ctx, 2.8, 2.4);
    const roomSend = ctx.createGain();
    const hallSend = ctx.createGain();
    roomSend.gain.value = 0;
    hallSend.gain.value = 0;
    preDelay.connect(roomSend);
    preDelay.connect(hallSend);
    roomSend.connect(roomVerb);
    hallSend.connect(hallVerb);
    roomVerb.connect(comp);
    hallVerb.connect(comp);

    return { fxIn, roomSend, hallSend, sideGain, bassShelf, airShelf, comp, makeup };
  }

  private applyAmbience(immediate = false) {
    const p = AMBIENCE[this.ambience];
    const now = this.ctx.currentTime;
    const set = (param: AudioParam, value: number) => {
      if (immediate) param.value = value;
      else param.setTargetAtTime(value, now, FX_SMOOTH_S);
    };
    set(this.roomSend.gain, p.room);
    set(this.hallSend.gain, p.hall);
    set(this.sideGain.gain, p.width);
    set(this.bassShelf.gain, p.bassDb);
    set(this.airShelf.gain, p.airDb);
    set(this.comp.threshold, p.threshold);
    set(this.comp.ratio, p.ratio);
    set(this.makeup.gain, Math.pow(10, p.makeupDb / 20));
  }

  /** 平滑過渡，避免參數瞬跳造成咔聲。 */
  private pushPitch(): void {
    const param = this.pitchNode?.parameters.get("pitchSemitones");
    param?.setTargetAtTime(this.pitchValue, this.ctx.currentTime, PITCH_SMOOTH_S);
  }

  private applyGains(smooth = 0.02) {
    const now = this.ctx.currentTime;
    const mode = this.effectiveMode();
    const guide = this.original ? 1 : this.guide;
    let dry = 0;
    let instant = 0;
    if (mode === "ORIGINAL") {
      dry = 1;
      instant = 0;
    } else if (mode === "INSTANT") {
      dry = guide;
      instant = 1 - guide;
    } else {
      dry = 0;
      instant = 0;
    }
    this.dryGain.gain.setTargetAtTime(dry, now, smooth);
    this.instantGain.gain.setTargetAtTime(instant, now, smooth);
    this.instGain.gain.setTargetAtTime(mode === "AI" ? 1 : 0, now, smooth);
    this.vocGain.gain.setTargetAtTime(mode === "AI" ? guide : 0, now, smooth);
  }

  private async activateStems() {
    if (!this.stems || this.stemsReady) return;
    if (!this.ensureStemEls()) return;
    if (!this.vocalsEl!.getAttribute("src")) {
      this.vocalsEl!.src = this.stems.vocals;
      this.instEl!.src = this.stems.instrumental;
    }
    const target = this.main.currentTime;
    this.vocalsEl!.currentTime = target;
    this.instEl!.currentTime = target;
    try {
      await Promise.all([this.vocalsEl!.play(), this.instEl!.play()]);
    } catch (err) {
      console.warn("stem playback blocked", err);
      return;
    }
    if (this.main.paused) {
      this.vocalsEl!.pause();
      this.instEl!.pause();
      return;
    }
    this.stemsReady = true;
    this.applyGains();
    this.onStateChange?.();
  }

  /**
   * 人聲／伴奏元素建一次就一直用（換歌只換 src）：Safari 要在點擊當下 play()
   * 過的元素才准自動播放，每首歌新建的元素永遠過不了。
   */
  private ensureStemEls(): boolean {
    if (this.vocalsSrc && this.instSrc) return true;
    if (!this.vocalsEl) {
      this.vocalsEl = document.createElement("audio");
      this.instEl = document.createElement("audio");
      for (const el of [this.vocalsEl, this.instEl]) {
        el.preload = "auto";
        el.crossOrigin = "anonymous";
      }
    }
    try {
      this.vocalsSrc = this.ctx.createMediaElementSource(this.vocalsEl);
      this.instSrc = this.ctx.createMediaElementSource(this.instEl!);
      this.vocalsSrc.connect(this.vocGain);
      this.instSrc.connect(this.instGain);
    } catch (err) {
      console.error("stem source failed", err);
      return false;
    }
    return true;
  }

  private pauseStems() {
    this.vocalsEl?.pause();
    this.instEl?.pause();
  }

  private syncStems() {
    if (!this.stemsReady || !this.vocalsEl || !this.instEl) return;
    const masterTime = this.main.currentTime;
    for (const el of [this.vocalsEl, this.instEl]) {
      if (Math.abs(el.currentTime - masterTime) > 0.15) {
        el.currentTime = masterTime;
      }
      if (this.main.paused && !el.paused) el.pause();
      else if (!this.main.paused && el.paused) void el.play().catch(() => undefined);
    }
  }

  private teardownStems() {
    this.pauseStems();
    // 元素和音源節點留著（見 ensureStemEls），只清掉這首的檔案
    for (const el of [this.vocalsEl, this.instEl]) {
      if (!el?.getAttribute("src")) continue;
      el.removeAttribute("src");
      el.load();
    }
    this.stemsReady = false;
    this.applyGains();
  }
}
