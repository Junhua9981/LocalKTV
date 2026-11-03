/**
 * 卡拉OK 歌詞（LyricStage）的字型與字級，量字寬的地方（切長句、掃描對齊字形）
 * 都要跟畫出來的一致，所以集中在這裡。
 */

import { url } from "../base";

/** 思源黑體 Noto Sans TC Black，切片載入（public/fonts/noto-sans-tc-900/font.css） */
export const LYRIC_FAMILY = `"Noto Sans TC", "PingFang TC", "Microsoft JhengHei", sans-serif`;
/**
 * 日文歌：Noto Sans JP 在前（public/fonts/noto-sans-jp-900），漢字用日本字形，繁中字型
 * 沒有的字也有；JP 沒有的（繁體字）再退回 TC。和 LyricStage `.ja .line` 一致。
 */
export const LYRIC_FAMILY_JA = `"Noto Sans JP", "Noto Sans TC", "Hiragino Sans", "Yu Gothic", "Microsoft JhengHei", sans-serif`;
export const LYRIC_WEIGHT = 900;

/** 和 LyricStage `.line` 的 font-size: clamp(32px, 4.6vw, 82px) 一致 */
export function lyricPx(viewportWidth: number): number {
  return Math.min(82, Math.max(32, viewportWidth * 0.046));
}

/** canvas measureText 用的 font 字串 */
export function lyricFont(px: number, ja = false): string {
  return `${LYRIC_WEIGHT} ${px}px ${ja ? LYRIC_FAMILY_JA : LYRIC_FAMILY}`;
}

/** 掛上歌詞字型（繁中、日文兩套）的 @font-face（只有電視頁需要，所以不放進共用的 CSS）。 */
export function loadLyricFontCss(): void {
  for (const name of ["noto-sans-tc-900", "noto-sans-jp-900"]) {
    if (document.getElementById(`lyric-font-${name}`)) continue;
    const link = document.createElement("link");
    link.id = `lyric-font-${name}`;
    link.rel = "stylesheet";
    link.href = url(`/fonts/${name}/font.css`);
    document.head.appendChild(link);
  }
}

/**
 * 先把這首歌詞用到的字所在的字型切片抓下來。切片字型只在字真的畫出來時才下載，
 * 在那之前量到的是備用字型的寬度；載完 document.fonts 會發 loadingdone，量字的
 * 地方（切長句、掃描對齊）收到就重量。
 */
export function preloadLyricGlyphs(texts: string[], ja = false): void {
  const glyphs = [...new Set(Array.from(texts.join("")))].join("");
  if (!glyphs.trim() || !document.fonts) return;
  document.fonts.load(lyricFont(48, ja), glyphs).catch(() => undefined);
}
