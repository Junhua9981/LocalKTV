/**
 * 電腦的錄音輸入（藍牙麥克風組、USB／有線麥克風）：評分（score.ts）和麥克風外放
 * （mic.ts）共用同一路收音。要用就 acquirePcMic()，用完 releasePcMic()；
 * 兩邊都放掉才真的關麥克風。
 */

let pending: Promise<MediaStream> | null = null;
let users = 0;

/** 瀏覽器有沒有辦法開麥克風（HTTPS 或 localhost 才有） */
export function pcMicSupported(): boolean {
  return isSecureContext && !!navigator.mediaDevices?.getUserMedia;
}

/** 開不了麥克風時的原因：使用者沒允許，或是根本沒有裝置 */
export function pcMicError(err: unknown): "denied" | "missing" {
  const name = (err as DOMException)?.name;
  return name === "NotAllowedError" || name === "SecurityError" ? "denied" : "missing";
}

export async function acquirePcMic(): Promise<MediaStream> {
  users += 1;
  try {
    pending ??= navigator.mediaDevices.getUserMedia({
      // 不要瀏覽器的通話處理：回音消除、降噪、自動增益都會吃掉歌聲
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1 },
    });
    return await pending;
  } catch (err) {
    users -= 1;
    pending = null;
    throw err;
  }
}

export function releasePcMic(): void {
  users = Math.max(0, users - 1);
  if (users > 0 || !pending) return;
  const stream = pending;
  pending = null;
  void stream.then((s) => s.getTracks().forEach((t) => t.stop())).catch(() => {});
}
