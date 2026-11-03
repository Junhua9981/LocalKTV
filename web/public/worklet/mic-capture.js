/**
 * LocalKTV 手機麥克風 — 收音端（低延遲模式）。
 *
 * classic script：audioWorklet.addModule() 不接受 import/export。
 *
 * 每 256 個取樣（48kHz 約 5.3ms）打包一次：兩兩平均降成一半取樣率
 * （人聲 12kHz 以下就夠，封包小一半），轉成 Int16，前面 8 bytes 標頭：
 *   uint32 序號（電視用來丟掉遲到的封包）・uint32 取樣率
 * 打包好的 ArrayBuffer 直接 transfer 給主執行緒，由 DataChannel 送出。
 */

const BLOCK = 256;

class MicCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.acc = new Float32Array(BLOCK);
    this.n = 0;
    this.seq = 0;
    this.factor = sampleRate >= 32000 ? 2 : 1;
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    const len = ch ? ch.length : 128;
    for (let i = 0; i < len; i++) {
      this.acc[this.n++] = ch ? ch[i] : 0;
      if (this.n === BLOCK) this.flush();
    }
    return true;
  }

  flush() {
    const f = this.factor;
    const count = BLOCK / f;
    const buf = new ArrayBuffer(8 + count * 2);
    const view = new DataView(buf);
    view.setUint32(0, this.seq >>> 0, true);
    view.setUint32(4, Math.round(sampleRate / f), true);
    this.seq = (this.seq + 1) >>> 0;
    const pcm = new Int16Array(buf, 8, count);
    for (let i = 0; i < count; i++) {
      let s = 0;
      for (let k = 0; k < f; k++) s += this.acc[i * f + k];
      s /= f;
      pcm[i] = Math.max(-1, Math.min(1, s)) * 32767;
    }
    this.n = 0;
    this.port.postMessage(buf, [buf]);
  }
}

registerProcessor("ktv-mic-capture", MicCapture);
