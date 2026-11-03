/**
 * 評分用的麥克風音高偵測（public/worklet/score-pitch.js）在 Node 裡跑的測試：
 * 假的 AudioWorklet 環境，餵合成的人聲，檢查音高與「沒在唱」的判斷。
 *
 * Run:  node web/tests/score-pitch.test.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const SR = 48000;
const source = readFileSync(fileURLToPath(new URL("../public/worklet/score-pitch.js", import.meta.url)), "utf8");

function makeProcessor() {
  const messages = [];
  let Klass = null;
  const sandbox = {
    sampleRate: SR,
    currentTime: 0,
    AudioWorkletProcessor: class {
      constructor() {
        this.port = { postMessage: (m) => messages.push(m) };
      }
    },
    registerProcessor: (_name, k) => (Klass = k),
    Math,
    Float32Array,
  };
  vm.runInNewContext(source, sandbox);
  const proc = new Klass();
  return {
    messages,
    feed(samples) {
      for (let i = 0; i < samples.length; i += 128) {
        sandbox.currentTime = i / SR;
        proc.process([[samples.subarray(i, i + 128)]], [[new Float32Array(128)]]);
      }
    },
  };
}

function voice(hz, seconds, amp = 0.2) {
  const out = new Float32Array(Math.round(seconds * SR));
  for (let i = 0; i < out.length; i++) {
    const p = (2 * Math.PI * hz * i) / SR;
    let s = 0;
    for (let k = 1; k <= 6; k++) s += Math.sin(k * p) / k;
    out[i] = amp * s + 0.002 * (Math.random() * 2 - 1);
  }
  return out;
}

let failed = 0;
function ok(cond, label, detail = "") {
  console.log(`  [${cond ? "OK " : "FAIL"}] ${label}${detail ? `  (${detail})` : ""}`);
  if (!cond) failed++;
}

console.log("1. 各音域的音高");
for (const hz of [82.4, 110, 196, 261.6, 440, 659.3, 880]) {
  const p = makeProcessor();
  p.feed(voice(hz, 0.5));
  const tail = p.messages.slice(-20);
  const cents = tail.map((m) => 1200 * Math.log2(m.hz / hz));
  const worst = Math.max(...cents.map(Math.abs));
  ok(tail.length === 20 && worst < 15, `${hz} Hz`, `最差 ${worst.toFixed(1)} 音分`);
}

console.log("2. 每 10ms 一筆、時間往前走");
{
  const p = makeProcessor();
  p.feed(voice(220, 1));
  const n = p.messages.length;
  ok(n >= 95 && n <= 100, "一秒約 100 筆", String(n));
  const steps = p.messages.slice(1).map((m, i) => m.t - p.messages[i].t);
  ok(steps.every((d) => Math.abs(d - 0.01) < 0.001), "間隔 10ms");
}

console.log("3. 雜音不給音高");
{
  const p = makeProcessor();
  const noise = new Float32Array(SR / 2).map(() => 0.1 * (Math.random() * 2 - 1));
  p.feed(noise);
  const voiced = p.messages.filter((m) => m.hz > 0).length;
  ok(voiced < p.messages.length * 0.1, "白雜音幾乎都判沒音高", `${voiced}/${p.messages.length}`);
}

console.log("4. 一筆的運算量");
{
  const p = makeProcessor();
  const clip = voice(300, 5);
  const started = performance.now();
  p.feed(clip);
  const ms = performance.now() - started;
  ok(ms < 1000, "5 秒的聲音 1 秒內算完", `${ms.toFixed(0)}ms`);
}

if (failed) {
  console.log(`FAIL (${failed})`);
  process.exit(1);
}
console.log("PASS");
