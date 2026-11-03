/**
 * LocalKTV 手機麥克風 — 電視播放端（低延遲模式），自己管抖動緩衝。
 *
 * classic script：audioWorklet.addModule() 不接受 import/export。
 *
 * 主執行緒把 DataChannel 收到的封包（見 mic-capture.js 的格式）transfer 進來，
 * 這裡存進環形緩衝，以線性內插換成電視的取樣率播出。
 *
 * 緩衝策略（目標 targetMs，自動在 MIN_MS..MAX_MS 間調整）：
 *   - 開播／斷音後先攢到 targetMs 才出聲
 *   - 讀到見底 = 斷音：補靜音、targetMs +5ms，重新攢（0.2 秒內只算一次）
 *   - 連續 3 秒沒斷音：targetMs -1ms，慢慢往下壓
 *   - 囤太多（約 0.5 秒平均 > target + 6ms）就讀快 1% 追上；> target + 40ms 直接跳到 target
 *     （手機與電視的時脈不會完全一樣，不追的話延遲會越積越多）
 *   - 封包順序亂了：先留空位，晚到的還沒播到就填回去；播過才到的算斷音
 *   - 真的掉的封包就是那段靜音
 * 每 0.5 秒回報 {buffer, target, underruns} 給主執行緒。
 */

const RING = 1 << 15; // 環形緩衝（來源取樣數），24kHz 下約 1.4 秒
const MIN_MS = 10;
const START_MS = 20;
const MAX_MS = 80;
const CATCHUP_MS = 6;
const JUMP_MS = 40;
/** 一次跳號超過這麼久才當作串流中斷（要比最大緩衝大，否則亂序會被誤判）。 */
const MAX_GAP_MS = 250;

class MicPlayout extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ring = new Float32Array(RING);
    this.w = 0; // 已寫入的取樣總數
    this.r = 0; // 讀取位置（可帶小數）
    this.rate = 24000;
    this.lastSeq = -1;
    this.playing = false;
    this.targetMs = START_MS;
    this.underruns = 0;
    this.calm = 0;
    this.glitchAt = -1;
    this.level = 0; // 存量的平滑值（來源取樣數）
    this.sumBuf = 0;
    this.nBuf = 0;
    this.reportFrames = 0;
    this.port.onmessage = (event) => this.push(event.data);
  }

  write(x) {
    this.ring[this.w & (RING - 1)] = x;
    this.w++;
  }

  push(buf) {
    if (!(buf instanceof ArrayBuffer) || buf.byteLength < 10) return;
    const view = new DataView(buf);
    const seq = view.getUint32(0, true);
    const rate = view.getUint32(4, true);
    const pcm = new Int16Array(buf, 8, (buf.byteLength - 8) >> 1);
    const len = pcm.length;
    if (rate >= 8000 && rate <= 96000) this.rate = rate;
    const d = this.lastSeq < 0 ? 1 : (seq - this.lastSeq) | 0; // 序號回捲也算得對
    if (d <= 0) {
      // 晚到的封包：它的位置還沒播到就補回去，播過了就算一次斷音
      const start = this.w - (1 - d) * len;
      if (d === 0 || start < Math.ceil(this.r) + 1 || start < this.w - RING / 2) {
        if (d < 0) this.glitch();
        return;
      }
      for (let i = 0; i < len; i++) this.ring[(start + i) & (RING - 1)] = pcm[i] / 32768;
      return;
    }
    if ((d - 1) * len > MAX_GAP_MS * this.rate / 1000) {
      // 斷太久（手機切到背景之類）：舊的不要了，重新攢緩衝
      this.r = this.w;
      this.playing = false;
    } else {
      // 先留空位（靜音），晚到的封包到了再填進去
      for (let i = 0; i < (d - 1) * len; i++) this.write(0);
    }
    this.lastSeq = seq;
    for (let i = 0; i < len; i++) this.write(pcm[i] / 32768);
    if (this.w - this.r > RING / 2) this.r = this.w - this.targetMs * this.rate / 1000;
  }

  /** 斷音（見底或封包來不及）：多留一點緩衝。短時間內連續發生只算一次。 */
  glitch() {
    if (currentTime - this.glitchAt < 0.2) return;
    this.glitchAt = currentTime;
    this.underruns++;
    this.targetMs = Math.min(MAX_MS, this.targetMs + 5);
    this.calm = 0;
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    const n = out.length;
    const msToSamples = this.rate / 1000;
    const target = this.targetMs * msToSamples;
    const avail = this.w - this.r;
    let i = 0;

    if (!this.playing && avail >= target) {
      this.playing = true;
      this.level = avail;
    }
    if (this.playing) {
      // 封包一陣一陣到，瞬間的存量跳來跳去；用約 0.5 秒的平均判斷要不要追
      this.level += (avail - this.level) * Math.min(1, n / (sampleRate * 0.5));
      if (this.level > target + JUMP_MS * msToSamples) {
        this.r = this.w - target;
        this.level = target;
      }
      const base = this.rate / sampleRate;
      const step = this.level > target + CATCHUP_MS * msToSamples ? base * 1.01 : base;
      for (; i < n; i++) {
        if (this.w - this.r < 2) {
          this.playing = false;
          this.glitch();
          break;
        }
        const p = Math.floor(this.r);
        const frac = this.r - p;
        const a = this.ring[p & (RING - 1)];
        const b = this.ring[(p + 1) & (RING - 1)];
        out[i] = a + (b - a) * frac;
        this.r += step;
      }
    }
    for (; i < n; i++) out[i] = 0;
    for (let c = 1; c < outputs[0].length; c++) outputs[0][c].set(out);

    if (this.playing) {
      this.calm += n;
      if (this.calm > sampleRate * 3) {
        this.calm = 0;
        this.targetMs = Math.max(MIN_MS, this.targetMs - 1);
      }
    }
    this.sumBuf += (this.w - this.r) / msToSamples;
    this.nBuf++;
    this.reportFrames += n;
    if (this.reportFrames >= sampleRate / 2) {
      this.port.postMessage({
        buffer: this.sumBuf / this.nBuf,
        target: this.targetMs,
        underruns: this.underruns,
      });
      this.reportFrames = 0;
      this.sumBuf = 0;
      this.nBuf = 0;
    }
    return true;
  }
}

registerProcessor("ktv-mic-playout", MicPlayout);
