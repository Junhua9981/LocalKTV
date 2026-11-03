/**
 * 手機當麥克風：手機收音 → WebRTC 直連 → 電視（/display）混進喇叭。
 *
 * 伺服器只轉送連線資訊（WS `mic.signal`：offer / answer / ICE），聲音本身
 * 走手機與電視之間的直連，同一個 Wi‑Fi 下不繞出去，延遲最低。
 *
 * 兩種傳輸方式（手機上選，記在手機的 localStorage）：
 *   fast   — 自己傳：AudioWorklet 收原始聲音 → 不重送、不排序的 DataChannel
 *            → 電視的 AudioWorklet 自己管緩衝（目標 10–80ms，自動調整）。
 *            Wi‑Fi 抖動大時可能偶爾斷音，延遲最低。見 public/worklet/。
 *   stable — 瀏覽器內建的語音軌（Opus），緩衝由瀏覽器決定，穩但常常上百毫秒。
 *
 *   手機 → 電視 data.kind：offer {sdp}・ice {candidate}・gain {value}・bye
 *   電視 → 手機 data.kind：answer {sdp}・ice {candidate}・busy・bye
 *   伺服器 → 手機：error {reason: "no_display"}
 *
 * 瀏覽器只在安全環境（HTTPS 或 localhost）才給開麥克風；區網 http 下
 * `navigator.mediaDevices` 根本不存在。
 */

import { reactive } from "vue";
import { url } from "../base";
import { makeImpulse } from "./engine";
import { acquirePcMic, pcMicError, pcMicSupported, releasePcMic } from "./pcmic";

/** 同一個 Wi‑Fi 通常用不到；手機走行動網路時才靠它找到彼此。 */
const ICE_SERVERS: RTCIceServer[] = [{ urls: "stun:stun.l.google.com:19302" }];
/** 電視同時接受的手機數。再多電視端解碼、混音的負擔與回授風險都會上來。 */
export const MAX_MICS = 4;
const GAIN_MAX = 1.5;
/** 電壓 RMS 超過這個值算「正在唱」（電視角落的提示亮起）。 */
const SINGING_RMS = 0.02;
/** 字與字之間的空檔不算停：大聲過後這麼久內都還算在唱，提示才不會一直閃。 */
const SINGING_HOLD_MS = 800;
/** 切掉 120Hz 以下：手持摩擦、喇叭回授最容易從低頻起來。 */
const MIC_HIGHPASS_HZ = 120;
/** 迴音每一次重複的間隔（秒）：短的 slapback 回音，緊貼原聲、不會覺得慢半拍。 */
const ECHO_DELAY_S = 0.09;
/** 回聲每重複一次就切掉一點高音，越後面越悶，像真的空間。 */
const ECHO_DAMP_HZ = 3500;
/** 迴音轉到最大時：第一聲回聲的大小、每次重複剩多少、殘響多少 */
const ECHO_SEND_MAX = 0.55;
const ECHO_FEEDBACK_MIN = 0.2;
const ECHO_FEEDBACK_MAX = 0.45;
const ECHO_VERB_MAX = 0.3;
/** 電腦麥克風外放的音量上限（和手機麥克風一樣 150%） */
const MONITOR_MAX = 1.5;

/** 電腦麥克風外放的狀態，電視角落提示用 */
export type MonitorState = "off" | "asking" | "denied" | "missing" | "on";

/**
 * 電視端麥克風怎麼出聲（全場共用，手機上切換，電視記在 localStorage）：
 *   mix  — 麥克風專用的低延遲 WebAudio 混音器，有低頻濾波與限幅器
 *   call — 瀏覽器通話用的輸出（<audio> 直接播），可能更快，但沒有濾波與限幅
 */
export type MicRoute = "mix" | "call";
const ROUTE_KEY = "ktv-mic-route";

type Send = (msg: Record<string, unknown>) => boolean;

export type MicTransport = "fast" | "stable";
const TRANSPORT_KEY = "ktv-mic-transport";
/** 自己傳的封包：每 256 個取樣一包（48kHz 約 5ms）。 */
const FAST_BLOCK_MS = (256 / 48000) * 1000;
/** DataChannel 還沒送出去的量超過這個就丟包：寧可斷一下也不要越積越慢。 */
const FAST_MAX_BUFFERED = 16_384;

/** 封包長度。Opus 預設 20ms，縮成 10ms 每段少等 10ms（封包數加倍，區網無感）。 */
const PTIME_MS = 10;

/**
 * 電視回給手機的 answer 裡要求 10ms 封包、單聲道、開 FEC（掉包時補聲）。
 * 送出端照 answer 的 ptime 切封包，所以只改這一份就夠了。
 */
export function lowLatencySdp(sdp: string): string {
  const pt = /a=rtpmap:(\d+) opus\/48000/i.exec(sdp)?.[1];
  if (!pt) return sdp;
  return sdp
    .replace(/a=ptime:\d+\r?\n/g, "")
    .replace(new RegExp(`a=fmtp:${pt} ([^\\r\\n]*)(\\r?\\n)`), (_line, params: string, eol: string) => {
      const kept = params.split(";").filter((p) => !/^\s*(minptime|useinbandfec|stereo)=/.test(p));
      const fmtp = [...kept, `minptime=${PTIME_MS}`, "useinbandfec=1", "stereo=0"].join(";");
      return `a=fmtp:${pt} ${fmtp}${eol}a=ptime:${PTIME_MS}${eol}`;
    });
}

/** 電視每秒回報一次的延遲拆解（毫秒）；手機上顯示，找出卡在哪一段。 */
export interface MicLatency {
  /** 手機收音（瀏覽器有回報才有）＋封包長度 */
  capture: number;
  /** 單程網路 ≈ 來回時間的一半 */
  network: number;
  /** 電視端抖動緩衝 */
  buffer: number;
  /** 電視瀏覽器到音效卡的輸出；通話輸出量不到，是 -1 */
  output: number;
  /** 電視目前的出聲方式（只對 stable 有意義） */
  mode: MicRoute;
  transport: MicTransport;
  /** 電視端緩衝見底（斷音）的累計次數，只有 fast 有 */
  underruns: number;
  /** host = 同一個區網直連；其他 = 繞到外網再回來 */
  route: string;
}

export function micSupported(): boolean {
  return (
    isSecureContext &&
    typeof RTCPeerConnection === "function" &&
    !!navigator.mediaDevices?.getUserMedia
  );
}

// -- phone side --------------------------------------------------------------

export type MicStatus = "off" | "connecting" | "ready" | "error";

export class MicSender {
  readonly state = reactive({
    status: "off" as MicStatus,
    /** 收音中（按住或鎖定）。關掉時送的是靜音，連線保持著，再開不用等。 */
    live: false,
    gain: 1,
    error: "",
    latency: null as MicLatency | null,
    transport: loadTransport(),
  });

  private pc: RTCPeerConnection | null = null;
  private stream: MediaStream | null = null;
  /** fast 用的收音 AudioContext：在使用者手勢裡建一次，重連時沿用。 */
  private ctx: AudioContext | null = null;
  private worklet: Promise<void> | null = null;
  private nodes: AudioNode[] = [];
  private channel: RTCDataChannel | null = null;
  private peer: number | null = null;
  private chain: Promise<unknown> = Promise.resolve();
  private retry: ReturnType<typeof setTimeout> | null = null;
  /** 使用者要麥克風開著（電視重連時自動重撥）。 */
  private wanted = false;

  constructor(private send: Send) {}

  async start(): Promise<void> {
    this.wanted = true;
    this.teardown(false);
    if (!micSupported()) {
      this.fail(isSecureContext ? "這個瀏覽器不支援麥克風" : "要用 HTTPS 網址開啟才能用麥克風");
      return;
    }
    this.state.status = "connecting";
    this.state.error = "";
    const fast = this.state.transport === "fast";
    if (fast) this.prepareContext(); // 要在點擊的當下建立／恢復，否則手機不給出聲
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          // 這三個是為通話設計的：會把歌聲當回音／雜音吃掉，音量忽大忽小
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
          channelCount: 1,
        },
      });
    } catch (err) {
      const name = (err as DOMException | undefined)?.name;
      this.fail(name === "NotAllowedError" ? "麥克風權限被拒絕，請到瀏覽器設定允許" : "打不開麥克風");
      return;
    }
    if (!this.wanted) {
      this.teardown(false);
      return;
    }
    const track = this.stream.getAudioTracks()[0];
    track.enabled = this.state.live;
    track.onended = () => this.fail("麥克風被系統中斷（切到別的 App？）");

    const pc = new RTCPeerConnection({ iceServers: ICE_SERVERS });
    this.pc = pc;
    if (fast) {
      try {
        await this.startCapture(pc, this.stream);
      } catch (err) {
        console.warn("mic capture worklet failed", err);
        this.fail("這支手機不支援低延遲模式，請改用「穩定」");
        return;
      }
      if (pc !== this.pc) return;
    } else {
      pc.addTrack(track, this.stream);
    }
    pc.onicecandidate = (event) => {
      if (event.candidate) this.signal({ kind: "ice", candidate: event.candidate.toJSON() });
    };
    pc.onconnectionstatechange = () => {
      if (pc !== this.pc) return;
      if (pc.connectionState === "connected") {
        this.state.status = "ready";
        this.signal({ kind: "gain", value: this.state.gain });
      } else if (pc.connectionState === "failed") {
        this.fail("連不到電視（手機和電視要在同一個 Wi‑Fi）");
      }
    };
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    this.signal({ kind: "offer", sdp: offer.sdp });
  }

  stop(): void {
    this.wanted = false;
    this.teardown(true);
    this.state.status = "off";
    this.state.error = "";
  }

  /** 切換傳輸方式；連線中就馬上重撥。 */
  setTransport(transport: MicTransport): void {
    if (transport === this.state.transport) return;
    this.state.transport = transport;
    try {
      localStorage.setItem(TRANSPORT_KEY, transport);
    } catch { /* 記不住就只在這次有效 */ }
    if (this.wanted) {
      this.teardown(true);
      void this.start();
    }
  }

  private prepareContext(): void {
    if (!this.ctx || this.ctx.state === "closed") {
      try {
        this.ctx = new AudioContext({ latencyHint: 0 });
      } catch {
        this.ctx = new AudioContext({ latencyHint: "interactive" });
      }
      this.worklet = null;
    }
    if (this.ctx.state !== "running") void this.ctx.resume().catch(() => {});
  }

  /** 收音 worklet → DataChannel。worklet 要被「拉」才會跑，所以接一個靜音的出口。 */
  private async startCapture(pc: RTCPeerConnection, stream: MediaStream): Promise<void> {
    const ctx = this.ctx!;
    const channel = pc.createDataChannel("mic", { ordered: false, maxRetransmits: 0 });
    channel.binaryType = "arraybuffer";
    this.channel = channel;
    this.worklet ??= ctx.audioWorklet.addModule(url("/worklet/mic-capture.js"));
    await this.worklet;
    if (channel !== this.channel) return;
    const source = ctx.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(ctx, "ktv-mic-capture", { numberOfOutputs: 1, outputChannelCount: [1] });
    const mute = ctx.createGain();
    mute.gain.value = 0;
    source.connect(node);
    node.connect(mute);
    mute.connect(ctx.destination);
    node.port.onmessage = (event: MessageEvent<ArrayBuffer>) => {
      if (channel.readyState === "open" && channel.bufferedAmount < FAST_MAX_BUFFERED) {
        try {
          channel.send(event.data);
        } catch { /* 通道剛好關掉 */ }
      }
    };
    this.nodes = [source, node, mute];
  }

  setLive(on: boolean): void {
    this.state.live = on;
    const track = this.stream?.getAudioTracks()[0];
    if (track) track.enabled = on;
  }

  setGain(value: number): void {
    this.state.gain = Math.max(0, Math.min(GAIN_MAX, value));
    if (this.state.status === "ready") this.signal({ kind: "gain", value: this.state.gain });
  }

  /** 切換電視的出聲方式（全場共用）。 */
  setRoute(route: MicRoute): void {
    if (this.state.status === "ready") this.signal({ kind: "route", value: route });
  }

  /** WS 重連後自己的編號變了，電視那端已經掛斷：重撥一次。 */
  reconnect(): void {
    if (this.wanted) void this.start();
  }

  /** 收到 `mic.signal`。 */
  handle(msg: { from: number | null; data: Record<string, any> }): void {
    const data = msg.data ?? {};
    if (msg.from === null) {
      if (data.reason === "no_display" && this.wanted) this.fail("電視還沒開，先打開電視畫面");
      return;
    }
    if (data.kind === "answer") {
      if (!this.pc || this.peer !== null) return;
      this.peer = msg.from;
      const pc = this.pc;
      this.chain = this.chain.then(() => pc.setRemoteDescription({ type: "answer", sdp: data.sdp }));
      return;
    }
    if (data.kind === "busy") {
      if (this.pc && this.peer === null) this.fail(`電視的麥克風已滿（最多 ${MAX_MICS} 支）`);
      return;
    }
    if (msg.from !== this.peer) return;
    if (data.kind === "ice" && this.pc) {
      const pc = this.pc;
      this.chain = this.chain.then(() => pc.addIceCandidate(data.candidate)).catch(() => {});
    } else if (data.kind === "stats") {
      const settings = this.stream?.getAudioTracks()[0]?.getSettings() as { latency?: number } | undefined;
      const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? Math.max(0, Math.round(v)) : 0);
      const fast = data.transport === "fast";
      // fast：收音 AudioContext 的緩衝＋一包的長度；stable：Opus 封包長度
      const extra = fast ? (this.ctx?.baseLatency ?? 0) * 1000 + FAST_BLOCK_MS : PTIME_MS;
      this.state.latency = {
        capture: num((settings?.latency ?? 0) * 1000 + extra),
        network: num(data.network),
        buffer: num(data.buffer),
        output: typeof data.output === "number" && data.output < 0 ? -1 : num(data.output),
        mode: data.mode === "call" ? "call" : "mix",
        transport: fast ? "fast" : "stable",
        underruns: num(data.underruns),
        route: String(data.route ?? ""),
      };
    } else if (data.kind === "bye") {
      // 電視重新整理或斷線：稍等一下再撥給新的電視
      this.teardown(false);
      if (this.wanted) {
        this.state.status = "connecting";
        this.retry = setTimeout(() => void this.start(), 1500);
      }
    }
  }

  private signal(data: Record<string, unknown>): void {
    this.send({ type: "mic.signal", to: this.peer, data });
  }

  private fail(message: string): void {
    this.wanted = false;
    this.teardown(true);
    this.state.status = "error";
    this.state.error = message;
  }

  private teardown(sayBye: boolean): void {
    if (this.retry) clearTimeout(this.retry);
    this.retry = null;
    if (sayBye && this.pc && this.peer !== null) this.signal({ kind: "bye" });
    for (const node of this.nodes) node.disconnect();
    this.nodes = [];
    this.channel?.close();
    this.channel = null;
    this.pc?.close();
    this.pc = null;
    this.peer = null;
    this.chain = Promise.resolve();
    this.state.latency = null;
    for (const track of this.stream?.getTracks() ?? []) track.stop();
    this.stream = null;
  }
}

function loadTransport(): MicTransport {
  try {
    return localStorage.getItem(TRANSPORT_KEY) === "stable" ? "stable" : "fast";
  } catch {
    return "fast";
  }
}

// -- display side ------------------------------------------------------------

export interface MicSinger {
  id: number;
  name: string;
  /** 0..1 的音量表，給畫面角落的提示用。 */
  level: number;
  singing: boolean;
}

interface Peer {
  pc: RTCPeerConnection;
  gain: GainNode;
  /** 手機上調的音量（0..GAIN_MAX） */
  volume: number;
  analyser: AnalyserNode | null;
  /**
   * 遠端串流掛在 <audio> 上：mix 模式靜音（Chrome 的老問題，不掛 WebAudio
   * 收不到聲音），call 模式就由它直接出聲。
   */
  sink: HTMLAudioElement | null;
  chain: Promise<unknown>;
  loudAt: number;
  /** 上一次的抖動緩衝累計值，算這一秒的平均 */
  jitter: { delay: number; count: number };
  /** fast：電視自己的播放 worklet 與它最近的回報 */
  playout: AudioWorkletNode | null;
  fast: { buffer: number; underruns: number } | null;
}

export class MicReceiver {
  readonly singers = reactive<MicSinger[]>([]);
  private peers = new Map<number, Peer>();
  private meter: ReturnType<typeof setInterval>;
  private statsTimer: ReturnType<typeof setInterval>;
  private buf = new Float32Array(1024);

  /**
   * 麥克風自己的 AudioContext，不跟伴奏共用：向瀏覽器要最小的輸出緩衝
   * （latencyHint 0），伴奏那邊維持一般設定不受影響。
   */
  readonly ctx: AudioContext;
  private bus: GainNode;
  private worklet: Promise<void> | null = null;
  route: MicRoute = "mix";
  private echoSend: GainNode;
  private echoFeedback: GainNode;
  private verbSend: GainNode;
  /** 電腦麥克風外放（評分用的同一路收音，pcmic.ts） */
  monitorState: MonitorState = "off";
  private monitorWanted = false;
  private monitorVolume = 1;
  private monitor: { source: MediaStreamAudioSourceNode; gain: GainNode } | null = null;

  constructor(private send: Send) {
    try {
      this.ctx = new AudioContext({ latencyHint: 0 });
    } catch {
      this.ctx = new AudioContext({ latencyHint: "interactive" });
    }
    this.bus = this.ctx.createGain();
    const highpass = this.ctx.createBiquadFilter();
    highpass.type = "highpass";
    highpass.frequency.value = MIC_HIGHPASS_HZ;
    // 限幅器：防爆音、壓住嘯叫的尖峰
    const limiter = this.ctx.createDynamicsCompressor();
    limiter.threshold.value = -12;
    limiter.knee.value = 6;
    limiter.ratio.value = 12;
    limiter.attack.value = 0.003;
    limiter.release.value = 0.15;
    this.bus.connect(highpass);
    highpass.connect(limiter);
    limiter.connect(this.ctx.destination);

    // 迴音：延遲回聲（每次重複更小、更悶）＋一點殘響，量由 setEcho() 決定。
    // 接在限幅器前面，回聲疊起來也不會爆音。
    const ctx = this.ctx;
    this.echoSend = ctx.createGain();
    this.echoSend.gain.value = 0;
    const delay = ctx.createDelay(1);
    delay.delayTime.value = ECHO_DELAY_S;
    const damp = ctx.createBiquadFilter();
    damp.type = "lowpass";
    damp.frequency.value = ECHO_DAMP_HZ;
    this.echoFeedback = ctx.createGain();
    this.echoFeedback.gain.value = 0;
    highpass.connect(this.echoSend);
    this.echoSend.connect(delay);
    delay.connect(damp);
    damp.connect(this.echoFeedback);
    this.echoFeedback.connect(delay);
    damp.connect(limiter);
    this.verbSend = ctx.createGain();
    this.verbSend.gain.value = 0;
    const verb = ctx.createConvolver();
    verb.buffer = makeImpulse(ctx, 0.8, 3);
    highpass.connect(this.verbSend);
    this.verbSend.connect(verb);
    verb.connect(limiter);
    try {
      if (localStorage.getItem(ROUTE_KEY) === "call") this.route = "call";
    } catch { /* 沒有 localStorage 就用預設 */ }
    this.meter = setInterval(() => this.measure(), 120);
    this.statsTimer = setInterval(() => void this.reportLatency(), 1000);
  }

  handle(msg: { from: number | null; name?: string; data: Record<string, any> }): void {
    const from = msg.from;
    const data = msg.data ?? {};
    if (typeof from !== "number") return;
    if (data.kind === "offer") {
      void this.accept(from, String(msg.name ?? "麥克風"), String(data.sdp ?? ""));
      return;
    }
    const peer = this.peers.get(from);
    if (!peer) return;
    if (data.kind === "ice") {
      peer.chain = peer.chain.then(() => peer.pc.addIceCandidate(data.candidate)).catch(() => {});
    } else if (data.kind === "gain") {
      const value = Number(data.value);
      if (Number.isFinite(value)) {
        peer.volume = Math.max(0, Math.min(GAIN_MAX, value));
        this.apply(peer);
      }
    } else if (data.kind === "route") {
      this.setRoute(data.value === "call" ? "call" : "mix");
    } else if (data.kind === "bye") {
      this.drop(from);
    }
  }

  dispose(): void {
    clearInterval(this.meter);
    clearInterval(this.statsTimer);
    for (const id of [...this.peers.keys()]) this.drop(id);
    this.monitorWanted = false;
    this.dropMonitor();
    void this.ctx.close();
  }

  /** 迴音 0..100：0 = 乾淨原聲。電腦麥克風和手機麥克風（mix 出聲）都會加。 */
  setEcho(amount: number): void {
    const a = Math.max(0, Math.min(100, amount)) / 100;
    const now = this.ctx.currentTime;
    this.echoSend.gain.setTargetAtTime(ECHO_SEND_MAX * a, now, 0.05);
    this.echoFeedback.gain.setTargetAtTime(a > 0 ? ECHO_FEEDBACK_MIN + (ECHO_FEEDBACK_MAX - ECHO_FEEDBACK_MIN) * a : 0, now, 0.05);
    this.verbSend.gain.setTargetAtTime(ECHO_VERB_MAX * a, now, 0.05);
  }

  /** 麥克風外放：電腦的錄音輸入直接從喇叭放出來（走這個低延遲混音器）。 */
  async setMonitor(on: boolean): Promise<void> {
    this.monitorWanted = on;
    if (!on) {
      this.dropMonitor();
      return;
    }
    if (this.monitorState === "on" || this.monitorState === "asking") return;
    if (!pcMicSupported()) {
      this.monitorState = "missing";
      return;
    }
    this.unlock();
    this.monitorState = "asking";
    let stream: MediaStream;
    try {
      stream = await acquirePcMic();
    } catch (err) {
      this.monitorState = pcMicError(err);
      return;
    }
    if (!this.monitorWanted || this.ctx.state === "closed") {
      // 等允許的時候外放被關掉了
      releasePcMic();
      this.monitorState = "off";
      return;
    }
    const source = this.ctx.createMediaStreamSource(stream);
    const gain = this.ctx.createGain();
    gain.gain.value = this.monitorVolume;
    source.connect(gain);
    gain.connect(this.bus);
    this.monitor = { source, gain };
    stream.getAudioTracks()[0]?.addEventListener("ended", () => {
      this.dropMonitor();
      this.monitorState = "missing";
    });
    this.monitorState = "on";
  }

  /** 外放音量 %（0–150） */
  setMonitorVolume(percent: number): void {
    this.monitorVolume = Math.max(0, Math.min(MONITOR_MAX, percent / 100));
    this.monitor?.gain.gain.setTargetAtTime(this.monitorVolume, this.ctx.currentTime, 0.02);
  }

  private dropMonitor(): void {
    if (this.monitor) {
      this.monitor.source.disconnect();
      this.monitor.gain.disconnect();
      this.monitor = null;
      releasePcMic();
    }
    // 要求中的那一次會自己收尾（setMonitor 裡看 monitorWanted）
    if (this.monitorState !== "asking") this.monitorState = "off";
  }

  /** 電視被點一下（使用者手勢）時一起解鎖麥克風的混音器。 */
  unlock(): void {
    if (this.ctx.state !== "running") void this.ctx.resume().catch(() => {});
  }

  setRoute(route: MicRoute): void {
    this.route = route;
    try {
      localStorage.setItem(ROUTE_KEY, route);
    } catch { /* 記不住就只在這次有效 */ }
    for (const peer of this.peers.values()) this.apply(peer);
  }

  /** 依出聲方式套用音量：只讓 mix 或 call 其中一條出聲（fast 一定走混音器）。 */
  private apply(peer: Peer): void {
    const call = this.route === "call" && !peer.playout;
    peer.gain.gain.setTargetAtTime(call ? 0 : peer.volume, this.ctx.currentTime, 0.02);
    if (peer.sink) {
      peer.sink.volume = Math.min(1, peer.volume); // <audio> 最大只到 100%
      peer.sink.muted = !call;
    }
  }

  private async accept(from: number, name: string, sdp: string): Promise<void> {
    this.drop(from); // 同一支手機重撥
    if (this.peers.size >= MAX_MICS) {
      this.send({ type: "mic.signal", to: from, data: { kind: "busy" } });
      return;
    }
    const ctx = this.ctx;
    this.unlock(); // 電視先前被點過就能直接恢復
    const pc = new RTCPeerConnection({ iceServers: ICE_SERVERS });
    const gain = ctx.createGain();
    gain.gain.value = 0;
    gain.connect(this.bus);
    const peer: Peer = {
      pc, gain, volume: 1, analyser: null, sink: null, chain: Promise.resolve(), loudAt: 0,
      jitter: { delay: 0, count: 0 }, playout: null, fast: null,
    };
    this.peers.set(from, peer);
    this.singers.push({ id: from, name, level: 0, singing: false });

    pc.onicecandidate = (event) => {
      if (event.candidate) {
        this.send({ type: "mic.signal", to: from, data: { kind: "ice", candidate: event.candidate.toJSON() } });
      }
    };
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === "failed" || pc.connectionState === "closed") this.drop(from);
    };
    pc.ondatachannel = (event) => void this.playFast(from, peer, event.channel);
    pc.ontrack = (event) => {
      const stream = event.streams[0] ?? new MediaStream([event.track]);
      // 能多低就多低：預設的抖動緩衝會為了穩定多等上百毫秒
      const receiver = event.receiver as unknown as Record<string, unknown>;
      try {
        if ("jitterBufferTarget" in receiver) receiver.jitterBufferTarget = 0;
        else receiver.playoutDelayHint = 0; // 舊版 Chrome 的同一個設定
      } catch { /* 不支援就用預設 */ }
      const sink = new Audio();
      sink.muted = true;
      sink.srcObject = stream;
      void sink.play().catch(() => {});
      peer.sink = sink;
      this.apply(peer);
      const source = ctx.createMediaStreamSource(stream);
      source.connect(gain);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 1024;
      source.connect(analyser);
      peer.analyser = analyser;
    };

    peer.chain = peer.chain.then(async () => {
      await pc.setRemoteDescription({ type: "offer", sdp });
      const answer = await pc.createAnswer();
      const tuned = lowLatencySdp(answer.sdp ?? "");
      try {
        await pc.setLocalDescription({ type: "answer", sdp: tuned });
      } catch {
        await pc.setLocalDescription(answer); // 瀏覽器不收改過的版本就用原版
      }
      this.send({ type: "mic.signal", to: from, data: { kind: "answer", sdp: tuned } });
    }).catch((err) => {
      console.warn("mic answer failed", err);
      this.drop(from);
    });
  }

  /** fast：DataChannel 收到的封包直接交給播放 worklet。 */
  private async playFast(from: number, peer: Peer, channel: RTCDataChannel): Promise<void> {
    if (channel.label !== "mic") return;
    channel.binaryType = "arraybuffer";
    const ctx = this.ctx;
    this.worklet ??= ctx.audioWorklet.addModule(url("/worklet/mic-playout.js"));
    try {
      await this.worklet;
    } catch (err) {
      console.warn("mic playout worklet failed", err);
      this.worklet = null;
      return;
    }
    if (this.peers.get(from) !== peer) return;
    const node = new AudioWorkletNode(ctx, "ktv-mic-playout", {
      numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1],
    });
    node.port.onmessage = (event) => {
      const d = event.data as { buffer: number; underruns: number };
      peer.fast = { buffer: d.buffer, underruns: d.underruns };
    };
    channel.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) node.port.postMessage(event.data, [event.data]);
    };
    peer.playout = node;
    node.connect(peer.gain);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    node.connect(analyser);
    peer.analyser = analyser;
    this.apply(peer);
  }

  private drop(id: number): void {
    const peer = this.peers.get(id);
    if (!peer) return;
    this.peers.delete(id);
    peer.pc.close();
    peer.gain.disconnect();
    peer.analyser?.disconnect();
    peer.playout?.disconnect();
    if (peer.playout) peer.playout.port.onmessage = null;
    if (peer.sink) peer.sink.srcObject = null;
    const index = this.singers.findIndex((s) => s.id === id);
    if (index >= 0) this.singers.splice(index, 1);
  }

  /** 每秒把延遲拆解回報給各支手機。 */
  private async reportLatency(): Promise<void> {
    const ctx = this.ctx;
    const mode = this.route;
    const mixOutput = ((ctx.baseLatency ?? 0) + ((ctx as { outputLatency?: number }).outputLatency ?? 0)) * 1000;
    for (const [id, peer] of this.peers) {
      const fast = !!peer.playout;
      // 通話輸出走瀏覽器內部，網頁量不到
      const output = mode === "call" && !fast ? -1 : mixOutput;
      if (peer.pc.connectionState !== "connected") continue;
      let report: RTCStatsReport;
      try {
        report = await peer.pc.getStats();
      } catch {
        continue;
      }
      let network = 0;
      let buffer = 0;
      let route = "";
      let localId = "";
      report.forEach((r) => {
        if (r.type === "inbound-rtp" && r.kind === "audio") {
          const delay = Number(r.jitterBufferDelay ?? 0);
          const count = Number(r.jitterBufferEmittedCount ?? 0);
          const dc = count - peer.jitter.count;
          if (dc > 0) buffer = ((delay - peer.jitter.delay) / dc) * 1000;
          peer.jitter = { delay, count };
        } else if (r.type === "candidate-pair" && r.nominated && r.state === "succeeded") {
          network = (Number(r.currentRoundTripTime ?? 0) * 1000) / 2;
          localId = r.localCandidateId;
        }
      });
      if (localId) route = String(report.get(localId)?.candidateType ?? "");
      const data: Record<string, unknown> = { kind: "stats", network, buffer, output, route, mode, transport: "stable" };
      if (fast) {
        data.transport = "fast";
        data.buffer = peer.fast?.buffer ?? 0;
        data.underruns = peer.fast?.underruns ?? 0;
      }
      this.send({ type: "mic.signal", to: id, data });
    }
  }

  private measure(): void {
    const now = performance.now();
    for (const singer of this.singers) {
      const peer = this.peers.get(singer.id);
      if (!peer?.analyser) continue;
      peer.analyser.getFloatTimeDomainData(this.buf);
      let sum = 0;
      for (const v of this.buf) sum += v * v;
      const rms = Math.sqrt(sum / this.buf.length);
      singer.level = Math.max(Math.min(1, rms * 5), singer.level * 0.75);
      if (rms > SINGING_RMS) peer.loudAt = now;
      singer.singing = now - peer.loudAt < SINGING_HOLD_MS;
    }
  }
}
