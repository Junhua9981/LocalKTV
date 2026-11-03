/**
 * LocalKTV 評分 — 電腦麥克風的音高偵測（YIN）。
 *
 * classic script：audioWorklet.addModule() 不接受 import/export。
 *
 * 輸入是電腦的錄音裝置（藍牙喇叭麥克風組），只分析、不出聲：輸出永遠是 0。
 * 先每 3 個取樣平均一次降到 ~16kHz（人聲基頻 1kHz 以下，夠用、算得快），
 * 每 10ms 用最近 32ms 的聲音做一次 YIN，送一筆給主執行緒：
 *   { t: 這段聲音最後一個取樣的 AudioContext 時間（秒）, hz: 音高（0 = 沒在唱）, rms }
 * 主執行緒再扣掉錄音／播放延遲，對回歌曲時間（見 src/audio/score.ts）。
 */

const F_MIN = 70; // 最低的男低音
const F_MAX = 1000; // 女高音 C6
const THRESHOLD = 0.15; // 正規化差分第一次低於這個值的地方就是週期
const APERIODIC = 0.3; // 最好的谷底還高於這個 → 氣音、子音或安靜

class ScorePitch extends AudioWorkletProcessor {
  constructor() {
    super();
    this.factor = Math.max(1, Math.round(sampleRate / 16000));
    this.rate = sampleRate / this.factor;
    this.window = Math.round(this.rate * 0.032);
    this.tauMin = Math.floor(this.rate / F_MAX);
    this.tauMax = Math.ceil(this.rate / F_MIN);
    this.size = this.window + this.tauMax + 1;
    this.hop = Math.round(this.rate * 0.01);
    this.ring = new Float32Array(this.size);
    this.frame = new Float32Array(this.size);
    this.diff = new Float32Array(this.tauMax + 1);
    this.pos = 0; // 環狀緩衝下一個寫入位置
    this.filled = 0;
    this.sinceHop = 0;
    this.acc = 0;
    this.accN = 0;
  }

  process(inputs, outputs) {
    for (const out of outputs[0] || []) out.fill(0);
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    const blockEnd = currentTime + ch.length / sampleRate;
    for (let i = 0; i < ch.length; i++) {
      this.acc += ch[i];
      if (++this.accN < this.factor) continue;
      this.ring[this.pos] = this.acc / this.factor;
      this.pos = (this.pos + 1) % this.size;
      this.acc = 0;
      this.accN = 0;
      if (this.filled < this.size) this.filled++;
      if (++this.sinceHop >= this.hop && this.filled === this.size) {
        this.sinceHop = 0;
        const t = blockEnd - (ch.length - 1 - i) / sampleRate;
        this.analyse(t);
      }
    }
    return true;
  }

  analyse(t) {
    const n = this.size;
    const x = this.frame;
    for (let i = 0; i < n; i++) x[i] = this.ring[(this.pos + i) % n];
    const W = this.window;
    // 最近的 W 個取樣當積分窗（最後面），往前找週期
    const base = n - W;
    let energy = 0;
    for (let j = 0; j < W; j++) energy += x[base + j] * x[base + j];
    const rms = Math.sqrt(energy / W);
    const d = this.diff;
    d[0] = 0;
    let running = 0;
    let found = -1;
    let best = -1;
    let bestVal = Infinity;
    for (let tau = 1; tau <= this.tauMax; tau++) {
      let s = 0;
      for (let j = 0; j < W; j++) {
        const v = x[base + j] - x[base + j - tau];
        s += v * v;
      }
      running += s;
      d[tau] = running > 0 ? (s * tau) / running : 1;
      if (tau < this.tauMin) continue;
      if (found < 0 && d[tau] < THRESHOLD) found = tau;
      if (found > 0 && tau > found && d[tau] >= d[tau - 1]) break;
      if (d[tau] < bestVal) {
        bestVal = d[tau];
        best = tau;
      }
    }
    let tau = found > 0 ? best : bestVal <= APERIODIC ? best : -1;
    let hz = 0;
    if (tau > 0 && tau < this.tauMax) {
      const a = d[tau - 1], b = d[tau], c = d[tau + 1];
      const den = a - 2 * b + c;
      const shift = den > 1e-9 ? (0.5 * (a - c)) / den : 0;
      hz = this.rate / (tau + shift);
    }
    this.port.postMessage({ t, hz, rms });
  }
}

registerProcessor("ktv-score-pitch", ScorePitch);
