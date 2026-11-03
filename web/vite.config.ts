import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

const backend = process.env.KTV_BACKEND ?? "http://localhost:8080";

export default defineConfig({
  // Relative asset URLs: the same build works at / and under a sub-path (src/base.ts).
  base: "./",
  plugins: [vue()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      "/login": backend,
      "/auth": backend,
      "/admin": backend,
      "/pair": backend,
      "/setup": backend,
      "/j/": backend,
      "/api": backend,
      "/media": backend,
      "/lyrics": backend,
      "/ws": { target: backend, ws: true },
    },
  },
  build: {
    outDir: "dist",
    // 智慧電視內建的瀏覽器（Android TV 的 WebView）常停在很舊的 Chrome：語法降到
    // Chrome 69，CSS 的 inset 等新寫法也一併轉成舊寫法（Vite 預設是 Chrome 111）。
    // 只轉語法、不補 API：新 API 要在程式裡自己做備援（見 lib/compat.ts）。
    target: ["chrome69", "safari13", "firefox78"],
    cssTarget: ["chrome69", "safari13", "firefox78"],
    chunkSizeWarningLimit: 2500,
  },
});
