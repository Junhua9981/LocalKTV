/**
 * 電視端的相容性診斷：智慧電視內建的瀏覽器（Android TV 的 WebView，例如
 * Browser Here）常常停在好幾年前的 Chrome，硬體也比筆電弱很多。
 *
 * - detectCompat()：瀏覽器版本、畫面大小、會影響畫面的功能支援與否（算一次）。
 * - PerfMeter：用 requestAnimationFrame 的間隔算 FPS、慢幀比例，加上長任務次數。
 *
 * /display?perf=1 會把這些畫在角落（PerfHud.vue）；不論有沒有 perf=1，電視
 * 都會用 WebSocket 回報給伺服器記在 log，沒有電視在手邊也查得到。
 */

export interface Compat {
  ua: string;
  /** Chrome／WebView 主版本；不是 Chromium 核心就是 null */
  chrome: number | null;
  /** Android WebView（UA 帶 "; wv)"）或 Android TV */
  webview: boolean;
  screen: string;
  /** WebGL 回報的顯示晶片；SwiftShader／沒有 WebGL = 沒有 GPU 加速，整頁靠 CPU 畫 */
  gpu: string | null;
  features: Record<string, boolean>;
}

function gpuRenderer(): string | null {
  try {
    const gl = document.createElement("canvas").getContext("webgl") as WebGLRenderingContext | null;
    if (!gl) return null;
    const info = gl.getExtension("WEBGL_debug_renderer_info");
    const name = String(gl.getParameter(info ? info.UNMASKED_RENDERER_WEBGL : gl.RENDERER));
    gl.getExtension("WEBGL_lose_context")?.loseContext();
    return name;
  } catch {
    return null;
  }
}

function cssSupports(prop: string, value: string): boolean {
  try {
    return typeof CSS !== "undefined" && typeof CSS.supports === "function" && CSS.supports(prop, value);
  } catch {
    return false;
  }
}

export function detectCompat(): Compat {
  const ua = navigator.userAgent;
  const m = /Chrom(?:e|ium)\/(\d+)/.exec(ua);
  const chrome = m ? Number(m[1]) : null;
  const canvasProto = typeof CanvasRenderingContext2D !== "undefined" ? CanvasRenderingContext2D.prototype : null;
  return {
    ua,
    chrome,
    webview: /; wv\)/.test(ua) || /Android TV|AFT|BRAVIA|SmartTV/i.test(ua),
    screen: `${window.innerWidth}x${window.innerHeight}@${window.devicePixelRatio || 1}`,
    gpu: gpuRenderer(),
    features: {
      // CSS.supports 對 paint-order 會回 true（SVG 早就支援），HTML 文字要 Chrome 123
      paintOrderHtml: chrome === null ? cssSupports("paint-order", "stroke fill") : chrome >= 123,
      colorMix: cssSupports("color", "color-mix(in oklab, red 50%, blue)"),
      textStroke: cssSupports("-webkit-text-stroke", "1px black"),
      clipPath: cssSupports("clip-path", "inset(0 10% 0 0)"),
      backdropFilter: cssSupports("backdrop-filter", "blur(2px)") || cssSupports("-webkit-backdrop-filter", "blur(2px)"),
      mixBlend: cssSupports("mix-blend-mode", "screen"),
      roundRect: !!canvasProto && "roundRect" in canvasProto,
      segmenter: typeof Intl !== "undefined" && "Segmenter" in Intl,
      audioWorklet: typeof AudioWorkletNode === "function" && isSecureContext,
      fontLoading: !!document.fonts,
    },
  };
}

export interface PerfStats {
  fps: number;
  /** 0..1：間隔超過 25ms（掉到 40fps 以下）的幀佔多少 */
  slow: number;
  /** 這段期間超過 50ms 的長任務次數（瀏覽器支援才有） */
  longTasks: number;
  /** JS heap（MB），只有 Chrome 系有 */
  heapMb: number | null;
}

/** 量一段時間的幀率；stats() 取完就歸零重新算。 */
export class PerfMeter {
  private frames = 0;
  private slowFrames = 0;
  private last = 0;
  private since = performance.now();
  private longTasks = 0;
  private raf = 0;
  private observer: PerformanceObserver | null = null;

  start(): void {
    const step = (now: number) => {
      if (this.last) {
        this.frames += 1;
        if (now - this.last > 25) this.slowFrames += 1;
      }
      this.last = now;
      this.raf = requestAnimationFrame(step);
    };
    this.raf = requestAnimationFrame(step);
    try {
      this.observer = new PerformanceObserver((list) => (this.longTasks += list.getEntries().length));
      this.observer.observe({ entryTypes: ["longtask"] });
    } catch {
      this.observer = null; // 舊瀏覽器沒有 longtask
    }
  }

  stop(): void {
    cancelAnimationFrame(this.raf);
    this.observer?.disconnect();
  }

  stats(): PerfStats {
    const now = performance.now();
    const seconds = Math.max(0.001, (now - this.since) / 1000);
    const memory = (performance as Performance & { memory?: { usedJSHeapSize: number } }).memory;
    const out: PerfStats = {
      fps: Math.round((this.frames / seconds) * 10) / 10,
      slow: this.frames ? Math.round((this.slowFrames / this.frames) * 1000) / 1000 : 0,
      longTasks: this.longTasks,
      heapMb: memory ? Math.round(memory.usedJSHeapSize / 1048576) : null,
    };
    this.frames = this.slowFrames = this.longTasks = 0;
    this.since = now;
    return out;
  }
}
