/**
 * LocalKTV 升降 Key — 雙 tap 交叉淡入延遲線 pitch shifter。
 *
 * classic script：audioWorklet.addModule() 不接受 import/export。
 *
 * 原理：延遲線輸出 y(i) = x(i - d)，瞬時讀速 = 1 - d'。
 * 要得到頻率比 r = 2^(st/12)，令 d' = 1 - r ≡ u。
 *
 *   d1 = D0 + a               a 每樣本 + u，越界就回捲
 *   d2 = D0 + (a - L/2) mod L 兩個 tap 的回捲永遠差半個週期
 *   g1 = sin^2(pi * a/L)      恰在 d1 回捲處為 0 → 跳變被靜音掩蓋
 *   g2 = cos^2(pi * a/L)      恰在 d2 回捲處（a = L/2）為 0
 *
 * idle（|st| 極小）時 a 凍結 → 讀速 = 1，音高不變；但兩個 tap 仍相距 L/2
 * 會留下靜態梳狀波，所以 idle 只輸出「離回捲點最遠」的那個 tap（權重 (1,0)）。
 * 啟動時 12ms（3*MIX_TAU）內淡入 active 權重。安全窗口 [L/4, 3L/4] 保證：
 *   - idleTap1 = true  → tap1 首次回捲距 >= L/4（啟動後 >= 79ms，遠大於 12ms
 *                         淡入），淡入完成後 g1 = sin^2 = 0 正確掩蓋；
 *                         tap2 回捲時被 idleW2 = 0 掩蓋
 *   - idleTap1 = false → 反之，兩者對稱
 *
 * 實測（temp/pitch_psel.py，220/440Hz，>= 3 個完整週期）：
 *   +-1..+-3 音高誤差 4.2 - 11.7 cents；+-4 劣化到 18.7 cents → 範圍限 +-3。
 *
 * idle 延遲 = D0 + 務必回報給 display 用於歌詞同步補償。
 */

const MIN_SEMITONES = -3;
const MAX_SEMITONES = 3;
const SPAN_MS = 60;   // 掃幅 L；tap 間距 = L/2 = 30ms（低於回聲門檻）
const IDLE_ST = 1e-3; // |st| 小於此視為無升降
const MIX_TAU = 0.004; // 權重淡入淡出時間常數（秒）；必須 << L/(4|u|) = 79ms（±3 時最壞）
const D0 = 4;         // 基準延遲，整數樣本

class PitchProcessor extends AudioWorkletProcessor {
  static get parameterDescriptors() {
    return [{
      name: "pitchSemitones",
      defaultValue: 0,
      minValue: MIN_SEMITONES,
      maxValue: MAX_SEMITONES,
      automationRate: "k-rate",
    }];
  }

  constructor() {
    super();
    const span = Math.max(8, Math.round(SPAN_MS / 1000 * sampleRate));
    let len = 1;
    while (len < D0 + span + 4) len <<= 1;
    this.span = span;
    this.mask = len - 1;
    this.bufA = new Float32Array(len);
    this.bufB = new Float32Array(len);
    this.w = 0;
    this.a = span / 4;      // 起點落在安全窗口內
    this.mix = 0;
    this.idleTap1 = true;
    this.wasActive = false;
    this.reported = -1;
    this.sinceReport = 0;
    this.forceBlocks = 32; // 態轉換後前 32 個 block（≈85ms）每塊都回報，涵蓋 crossfade 收斂
  }

  /** 4 點 Catmull-Rom；d 為小數延遲，w 是剛寫入的位置。
   *  注意 p = k - d（負值）：ring 裡 base+1 是「較新」的樣本（延遲 k-1），
   *  插值朝 base-1 走才會讓延遲變大，故取 -frac(d) ∈ (-1, 0]。 */
  read(buf, w, d) {
    const k = Math.floor(d);
    const p = k - d;
    const base = (w - k) & this.mask;
    const m = this.mask;
    const ym1 = buf[(base - 1) & m];
    const y0 = buf[base];
    const y1 = buf[(base + 1) & m];
    const y2 = buf[(base + 2) & m];
    return y0 + 0.5 * p * (
      y1 - ym1 + p * (2 * ym1 - 5 * y0 + 4 * y1 - y2
        + p * (3 * (y0 - y1) + y2 - ym1))
    );
  }

  process(inputs, outputs, parameters) {
    const output = outputs[0];
    if (!output || !output[0]) return true;
    const outA = output[0];
    const outB = output[1] || outA;
    const n = outA.length;

    const input = inputs[0];
    const inA = input && input.length ? input[0] : null;
    const inB = input && input.length > 1 ? input[1] : inA;

    let st = parameters.pitchSemitones[0];
    if (st < MIN_SEMITONES) st = MIN_SEMITONES;
    else if (st > MAX_SEMITONES) st = MAX_SEMITONES;

    const active = Math.abs(st) > IDLE_ST;
    if (active !== this.wasActive) {
      // 進入 idle：挑離回捲點最遠的 tap 當唯一輸出
      if (!active) {
        const half = this.span / 2;
        this.idleTap1 = this.a >= half - half / 2 && this.a <= half + half / 2;
      }
      this.wasActive = active;
      this.forceBlocks = 32; // 態轉換後 crossfade 收斂前必須持續回報精確延遲
    }

    const r = Math.pow(2, st / 12);
    const u = 1 - r;

    // 每樣本淡變。block 級更新會在塊邊界直接跳權重（首塊最多 0.24 全刻度）→ 咔聲
    const targetMix = active ? 1 : 0;
    const mixStep = 1 - Math.exp(-1 / (MIX_TAU * sampleRate));
    let mix = this.mix;

    const span = this.span;
    const half = span / 2;
    const idleW1 = this.idleTap1 ? 1 : 0;
    const idleW2 = 1 - idleW1;
    const pi = Math.PI;
    const bufA = this.bufA;
    const bufB = this.bufB;
    const mask = this.mask;

    let a = this.a;
    let w = this.w;
    let delaySum = 0;

    for (let i = 0; i < n; i++) {
      mix += (targetMix - mix) * mixStep;
      if (active) {
        a += u;
        if (a >= span) a -= span;
        else if (a < 0) a += span;
      }
      const s = Math.sin(pi * (a / span));
      const aW1 = s * s;
      const imix = 1 - mix;
      const g1 = mix * aW1 + imix * idleW1;
      const g2 = mix * (1 - aW1) + imix * idleW2;
      const d1 = D0 + a;
      let a2 = a - half;
      if (a2 < 0) a2 += span;
      const d2 = D0 + a2;

      const ya = inA ? inA[i] : 0;
      bufA[w] = ya;
      bufB[w] = inB ? inB[i] : ya;

      outA[i] = g1 * this.read(bufA, w, d1) + g2 * this.read(bufA, w, d2);
      outB[i] = g1 * this.read(bufB, w, d1) + g2 * this.read(bufB, w, d2);

      delaySum += g1 * d1 + g2 * d2;
      w = (w + 1) & mask;
    }

    this.a = a;
    this.w = w;
    this.mix = mix;

    const delayMs = delaySum / n / sampleRate * 1000;
    this.sinceReport += 1;
    if (this.forceBlocks > 0 || (this.sinceReport >= 3 && Math.abs(delayMs - this.reported) > 0.5)) {
      this.reported = delayMs;
      this.sinceReport = 0;
      if (this.forceBlocks > 0) this.forceBlocks -= 1;
      this.port.postMessage({ delayMs });
    }
    return true;
  }
}

registerProcessor("ktv-pitch", PitchProcessor);
