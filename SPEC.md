# LocalKTV Minimal Spec v0.1

## 0. 核心目標

建立一套部署於**現場單一 PC** 的 Web KTV。

使用者以手機連入同一 LAN：

```text
Guest Phone ─┐
Guest Phone ─┼── Wi-Fi/LAN ── Local KTV PC ── Speaker / TV
Host Phone  ─┘
```

必須做到：

- 手機搜尋歌曲與點歌
- Queue 自動依序播放
- Host 手機可控制播放與 Queue
- PC 輸出實際音訊
- PC/TV 顯示逐字 Karaoke 歌詞
- 支援原唱
- 支援即時 L/R 去人聲
- 支援 AI 去人聲（UVR MDX-Net KARA_2）
- 支援 0–100% 導唱
- AI 尚未完成時不得阻塞播放

---

# 1. 第一性原理

整個系統只有四個真正必要的 State Owner。

```text
Queue State       → Local Server
Song/Cache State  → Local Server
Playback Clock    → Display Player
Audio Processing  → Local Processing Worker
```

因此不需要：

```text
VPS
Cloud DB
Redis
PostgreSQL
WebRTC
Nginx
Microservices
Distributed Queue
SSR
```

單次部署只服務一個 KTV 場景，因此：

> 不做 multi-room。

一台 PC 就是一個 KTV Room。

---

# 2. 最小架構

```text
                  Local KTV PC
┌──────────────────────────────────────────┐
│                                          │
│         karaoke-server                   │
│                                          │
│                                          │
│   REST / WebSocket                       │
│   Queue / Session                        │
│   Search                                 │
│   Lyrics Resolver                        │
│   Cache                                  │
│   Job Manager                            │
│                                          │
│     ┌───────────────┐                    │
│     │ Processing    │                    │
│     │               │                    │
│     │ yt-dlp        │                    │
│     │ FFmpeg        │                    │
│     │ Mini-BSRF     │                    │
│     └───────────────┘                    │
│                                          │
│          SQLite + Media Cache            │
│                                          │
└─────────────────┬────────────────────────┘
                  │
                  │ localhost
                  ▼
             /display
        Vue + WebAudio + LyricStage
                  │
                  ▼
               Speaker
             TV / Monitor


              Wi-Fi LAN
                  │
          ┌───────┴────────┐
          ▼                ▼
       /host             /remote
     Host Phone         Guest Phone
```

---

# 3. 技術選型

## Backend

```text
Python 3.12
FastAPI
Uvicorn
SQLite
asyncio
```
或是

Rust + Python IPC

---

## Frontend

```text
Vue 3
Vite
TypeScript
```

純 SPA。三個 route：

```text
/display
/host
/remote
```

---

## Lyrics

```text
Server 解析 QRC / LRC / YRC / TXT  →  GET /lyrics  →  Display LyricStage
```

歌詞一律在伺服端解析成 `lines`（逐行 start/end + 逐字 word timings），Display 只渲染。
歌詞置於畫面下方約 30% 高度，雙格固定不換位：第 1、3…句在左上，第 2、4…句在右下，到自己那格原地開唱，另一格預顯下一句；預設白字黑外框，唱到的部分為**逐字模式**（有 word timing）由左往右掃描填滿純藍底白外框、對齊逐字邊界；若整份歌詞都沒有逐字時間軸則走**逐行模式**：整行一次亮起並 0.3s 淡入、不做掃描，下一格維持白色預覽。兩種模式由 `GET /lyrics` 回傳的 `timing`（`word`／`line`）決定——server 依解析結果判定（任一行有 word timing 即 `word`），display 只照做、不自行猜測；可帶翻譯行；對唱歌（歌詞有 `singer`）每行行頭上方浮一個圓角演唱者小標，底色同該聲部填色，合唱寫「合唱」，倒數點排在小標右邊；字體為思源黑體 Noto Sans TC 900（SIL OFL 1.1，`web/public/fonts/noto-sans-tc-900/`，unicode-range 切片載入），字級 `clamp(36px, 5.2vw, 92px)`、**粗體**（字型本身的 900 字重）、**無陰影**（對比全靠黑外框）；黑外框用「描邊層＋字面層」兩層疊出來，不用 `paint-order`（舊的電視 WebView 不支援 HTML 文字的 paint-order）；翻譯行粗體、以細黑陰影當邊。
網路歌詞**逐字優先**：先問網易雲 YRC（`source_id=netease_<id>` 直取同一目錄 → 搜尋，一次請求同時回 YRC + LRC）→ 再問 QQ Music QRC（`qrc` 欄或 `lyric` 欄的 `<QrcInfos>` XML 內 `LyricContent` 都算逐字）→ 兩家都沒有逐字，才交出先前收下的第一份逐行答案 → 酷狗 LRC → LRCLIB LRC（僅在什麼都沒收到時）；本地/手動永遠優先；抓到即存 track cache（`lyrics.yrc`／`lyrics.qrc`／`lyrics.lrc`），簡→繁在入 cache 前完成；manifest 另記 `lyrics_provider`／`lyrics_via`（`source_id`｜`search`）／`lyrics_timing`（`word`｜`line`）。
AMLL 研究時參考其 TTML 能力（逐字時間、背景人聲、對唱、翻譯）：
https://github.com/amll-dev/amll-ttml-db

---

# 4. 三個 UI

## `/display`

運行於 KTV PC。

它是唯一真正播放聲音的 Client。

版面：歌名/歌手與點歌人固定在**左上角**，模式（ORIGINAL/INSTANT/AI）、導唱 %、ONLINE 徽章在**右上角**；底部由上而下為時間（`m:ss / m:ss`）與進度訊息（ready / cache hit 等）列，最下方是**貼底全寬進度條**；歌詞雙格區位於下方約 30%。點歌 QR（`GET /api/qr.svg`，SVG、編碼 Join URL、根路徑 redirect 到 /remote）：**本場第一首載入前**置中大圖＋「用手機掃碼點歌」，**開唱後**縮到右上角（徽章右側、貼邊）；QR 產生失敗只回 503，不影響伺服器。

負責：

```text
Audio Playback
Web Audio mixing
LyricStage lyrics
4-beat count-in before the first vocal line
Background MV (if available)
Full-screen display
Playback clock
```

---

## `/host`

Host 手機。

功能只有：

```text
Play / Pause
Skip
Seek

Queue reorder
Queue remove
Priority play

Karaoke Mode
Guide Vocal

Volume
Pitch Shift ±3 半音（session 級；display 未回報 AudioWorklet 能力時停用）
```

Host 手機**不播放聲音**。

---

## `/remote`

一般使用者。畫面分四個底部頁籤：**首頁 / 搜尋 / 播放清單 / 已點**。功能：

```text
首頁：正在播放（封面佔位、進度、⏮ disabled、中間大顆「點歌」鍵＝導去搜尋頁、
      ⏭ 卡歌）＋ 卡歌／插歌兩顆大按鈕（插歌＝導去搜尋）＋ 導唱 slider ＋
      升降 Key ＋ 音量
搜尋（分段：曲庫 / YouTube / 榜單）
  曲庫＝本地快取＋音源外掛（沒外掛時改問 YouTube）；YouTube 另有專搜分頁，查詢自動附加 KTV 關鍵字
  榜單分頁（由音源外掛提供，沒外掛就沒有；每份榜可分國語／台語，
  不顯示曲號、前三名皇冠；可直接由榜單點歌）
點歌（不選唱法 — 伺服器自動決定；導唱採用本機上次設定的預設值）
升降 Key（±3 半音 chips，與 host 同步；display 非安全環境時停用）
播放清單：Queue（正在播放卡片＋清單；「清空」只清自己的歌）
已點：只看自己點的歌
插歌（⤒ 提到下一位）／取消自己的歌曲
Currently Playing：卡歌（⏭ 跳過）＋ 導唱 0–100% 即時調整
```

榜單來源與快取規則見 §17 `GET /api/charts`。

---

# 5. Playback Authority

實際時間必須來自 `/display`。

```text
/display audio clock
        │
        ├── LyricStage
        │
        └── playback state
```

不能讓 Server：

```text
每 10ms 廣播 currentTime
```

Display 每約 1 秒回報：

```json
{
  "type": "playback_state",
  "track_id": "abc",
  "playing": true,
  "position_ms": 64321
}
```

Server 再同步給手機。手機只需要粗略顯示進度。LyricStage 的逐字動畫則完全使用本地 Display clock。

---

# 6. Karaoke Mode

每首歌只有三種模式。

```text
ORIGINAL
INSTANT
AI
```

## 模式怎麼選

- `/host`：手動指定，三種模式皆可。
- `/remote`：**不提供選擇** — 排隊時由伺服器自動決定：AI 分離器可用 → `AI`
  （`reconcile` 會把**整份 queue** 的 AI 工作排進 worker，後面的歌提早分離）；
  不可用 → `INSTANT`。即「AI 可用就自動處理，否則直接即時」，不讓使用者先挑。

## ORIGINAL

直接播放原始歌曲。

## INSTANT

不跑 AI。使用 stereo center cancellation：`L - R`，並保留低頻（< ~150 Hz）。由 `/display` 的 Web Audio API 即時完成。Server 計算成本 ≈ 0。

## AI

使用 UVR MDX-Net `UVR_MDXNET_KARA_2`（ONNX，`server/fetch_model.py` 下載並把時間軸改成可變長度）產生 `vocals` / `instrumental`。KARA 模型只拿掉主唱、保留和聲。

- 在子行程 `python -m ktv.separate` 執行：ffmpeg 逐段解碼、每次 64 格 STFT 推論、逐段編成 FLAC，記憶體約 300 MB，與歌長無關。
- 優先序最低：`ionice idle`、`nice 19`、`oom_score_adj 1000`（記憶體不足時 kernel 先砍它；service 設 `OOMPolicy=continue`）。
- 一次只做一首。排隊中需要 AI 的歌優先；所有包廂閒置 `KTV_AI_IDLE_DELAY` 秒後，背景補做已快取但未分離的歌（新的先做），有歌在唱就暫停（SIGSTOP），有人點需要 AI 的歌就中止補做、之後重做。失敗 `KTV_AI_MAX_ATTEMPTS` 次的歌不再補做。
- 原本規劃的 `HiDolen/Mini-BS-RoFormer-V2-46.8M` 需要 PyTorch，1 GB VPS 跑不動，因此改用 ONNX。

---

# 7. 導唱

UI 統一只有：`導唱 0% ─────────●──────── 100%`

### AI Mode

```text
output = instrumental + vocals × guide_gain
```

### Instant Mode

```text
output = instant_karaoke × (1 - guide) + original × guide
```

使用者不需要知道兩者底層差異。

---

# 8. 最重要的 Fallback Rule

任何情況都不能因為 AI 處理沒完成而停止 KTV。

```text
AI selected → AI stem ready? → YES: AI Mode / NO: Instant Mode
```

> AI 是品質增強，不是播放必要條件。

---

# 9. Song Processing Pipeline

```text
SEARCH → SELECT → QUEUE → CACHE CHECK
   ├─ cached → READY
   └─ miss → ACQUIRE → (AUDIO ∥ LYRICS) → INSTANT READY → AI requested? → SEPARATE → AI READY
```

> `INSTANT READY` 必須早於 AI separation。

---

# 10. Queue Item State

只保留四個外部狀態：`PREPARING` / `READY` / `PLAYING` / `FAILED`

```text
ai_status: NONE | QUEUED | PROCESSING | READY | FAILED
```

`DOWNLOADING` / `ENCODING` / `MATCHING` 等只作為 progress message，不做 domain state。

---

# 11. Queue Prefetch

只準備即將播放的歌曲：`prefetch_depth = 2`。AI Worker `concurrency = 1`。

---

# 12. Cache

```text
data/tracks/<track-key>/
├── manifest.json
├── source.*
├── lyrics.ttml
├── vocals.m4a          # AI stems: AAC 192k（2026-10-23 前是 FLAC）
└── instrumental.m4a
```

第二次點同一首 → cache hit → READY，不重新下載、不重新 AI inference。

---

# 13. Lyrics Resolution Priority

```text
1. Cached / 手動上傳或修正的 lyrics（POST /api/lyrics、data/lyrics/）
2. 網路・逐字優先（拿到逐字立刻停）：
   a. 網易雲 YRC（source_id 直取 → 搜尋；一次請求同時回 YRC + LRC）
   b. 網路：QQ Music QRC（qrc 欄，或 lyric 欄裡 <QrcInfos> 的 LyricContent）
   — a、b 都無逐字 → 交出先前收下的第一份逐行答案（a／b 先拿到的那份）
3. 網路：酷狗 LRC（僅在尚未收到任何逐行答案時）
4. 網路：LRCLIB LRC（最後手段）
5. YRC / TXT（其他快取格式）
6. No synced lyrics（照常播放，不顯示歌詞）
```

內部統一成 `lines`（`LyricLine[]`：start/end + words）。每份結果在 manifest 記錄
`lyrics_provider`、`lyrics_via`（`source_id`｜`search`）、`lyrics_timing`
（`word`｜`line`）、`lyrics_duration_ms`；`GET /lyrics/{track_id}` 回傳 `timing`，
LyricStage 依它選逐字掃描或整行亮起淡入。所有歌曲必須允許 `lyrics_offset_ms`。

---

# 14. Search / Audio Provider

```python
class SearchProvider:
    async def search(query): ...

class AudioProvider:
    async def acquire(track): ...

class LyricsProvider:
    async def resolve(track): ...
```

搜尋分兩個來源（`GET /api/search?source=main|youtube`）：`main` 先查**本地快取**
（SQLite `tracks` 索引，`textnorm.fold` 精確比對：標題 / 標題＋歌手 / 只輸入歌手 → 列該歌手全部；
Live 版標題 fold 不同自然分開；只列 audio 已落地者）再查**音源外掛**的曲庫（沒裝外掛時改問 YouTube），
同 `fold(title, artist)` 去重、快取勝；`youtube` 為 YouTube 專搜（查詢自動附加 `KTV` 關鍵字），
結果帶 `video_only` — 直接播放下載的影片，歌詞不爬不顯。出站查詢轉**简体**、
入站結果轉**繁體（台灣用字）**（`textnorm.py`，內附 OpenCC `tw2s`／`s2tw` 字典；`zh_keep.txt` 列的名詞保持原樣）。
音源外掛（`server/plugins/ktv_sources/`，不在本 repo）透過 `providers/registry.py` 註冊曲庫
（搜尋＋取檔＋可選的來源清單）、熱門榜與平台封面；外掛自己負責節流與呼叫預算。
取檔：外掛曲目交給外掛；下載成功＋時長驗證通過才算數，失敗由 `AudioRouter`（`router.py`）
fallback 到 YouTube（YouTube 軌跡先 yt-dlp、失敗才請外掛找同一首歌）— 共用同一 track cache 目錄。
host 可在 `/host`「主要音源」從外掛提供的來源中釘選（WS `audio.source`，host 專用；
釘選僅把該源排最前、其餘仍是備用；只影響下一次取檔，重啟回 `auto`；沒外掛時只有 `auto`）。
歌詞 provider 為本機 `LocalLyricsProvider` + 網路鏈（網易雲 → QQ QRC → 酷狗 → LRCLIB）。其他平台之後是 plugin。

---

# 15. 最小資料模型

Track：`id, title, artist, duration_ms, source, source_id, lyrics_offset_ms, video_only`
QueueItem：`id, track_id, requested_by, karaoke_mode, guide_vocal, status, ai_status`
Session：`users[], queue[], current_item, playback_state, audio_source`（只有一個 Session）

---

# 16. WebSocket

只有一條 `/ws`。

```text
session.snapshot
queue.add / queue.remove / queue.move / queue.bump
playback.play / pause / seek / skip / state
karaoke.mode / karaoke.guide
audio.source
track.progress
```

評分：`display.visual` 也帶 `score`／`score_bar`／`score_offset_ms`（全場共用）；
電視唱完送 `score.result {item_id, score, pitch, rhythm, stability}`（僅 display、僅目前這首），
伺服器存檔後對整間廣播 `score.posted {item_id, track_id, title, singer, score, best}`。
`display.load`／`display.update` 帶 `pitch_url`（null＝這首尚未有評分）。

Server 每次修改後 broadcast 新 state，Client 不自行推測 Queue。
角色權限：`playback.*` 中 `skip`/`play`/`pause` 允許 host+remote、`seek` 僅 host
（remote 為協議層能力，UI 只放卡歌 — 播放/暫停按鈕在 host）；
`audio.source` 僅 host（三音源主要來源切換）；`queue.bump` / `karaoke.*` 各角色皆可。

---

# 17. HTTP API

```text
GET  /api/search?q=
GET  /api/charts
GET  /api/charts/{source}
POST /api/queue
DELETE /api/queue/{id}
GET  /api/session
GET  /api/qr.svg
GET  /media/{track}/{file}
GET  /lyrics/{track}
GET  /api/scores?track_id=     本曲高分（所有房間，高分在前）
```

播放控制走 WebSocket。

`GET /api/charts` 回 `[key,label,page]`（外掛提供的榜單；沒外掛時回空陣列）；
`GET /api/charts/{source}` 回該站榜單 —— 一次回**兩個語別**：
`charts:[{lang,label,songs}]`（zh=國語在前、tw=台語在後，切換語別不打伺服器）。
每筆 song 回 `rank,title,artist,no,tag`，但畫面**只顯示排名／歌名／歌手**（不顯示曲號）。
榜單大約一個月更新一次，伺服器端 `data/charts/<source>.json` 快取 30 天 ——
**未過期一律回快取、不打站台**；過期才重抓，抓失敗回舊資料（`stale:true` + `error`），
失敗後一小時內不再嘗試，同一來源同時只會發一個請求。任一語別少於 5 筆視為改版失敗。
榜單只用於「列出歌名／歌手」，點歌仍走 `/api/search` 取得真正的 track。

---

# 18. Local Network

Server `0.0.0.0:8080`，啟動後顯示 Display URL、Join URL 與 QR Code。mDNS 非 MVP 必要條件。

---

# 19. Deployment

```text
start-karaoke  → 啟動 FastAPI、AI worker、打開 /display、顯示 QR Code
```

不要求 Docker。

---

# 20. 不做的東西（v0.1）

Multi-room、User accounts、Cloud sync、VPS、WebRTC、P2P、Redis、PostgreSQL、Native app、Tempo shift、Scoring、Mic recording、Duet mic mixing、Distributed AI workers。

（MV 背景影片已納入：只做背景下載 video-only 480p 供 display 鋪底，不做 MV 搜尋/選片/完整影片播放。）

---

# 21. MVP Acceptance Criteria

```text
1. PC 一條指令啟動。
2. 手機掃 QR 可以加入。
3. 至少 5 台手機可以同時點歌。
4. Queue 可以連續播放，不需人工換歌。
5. Host 手機可以 play / pause / skip / reorder。
6. Display 可以顯示逐字歌詞（LyricStage：QRC 逐字 / LRC 逐行）。
7. 每首歌可選 Original / Instant / AI。
8. Instant 模式不需任何 preprocessing。
9. AI 模式產生 vocals + instrumental。
10. AI 尚未完成時，自動 fallback Instant。
11. Guide vocal 0–100% 可即時修改。
12. 播放過的 AI song 第二次必須 cache hit。
13. lyrics_offset_ms 可以手動調整並保存。
14. AI/歌詞失敗不能使 Queue crash。
15. Internet 暫時中斷時，已 cache 的歌曲仍然可以正常唱。
16. 開唱（第一句歌詞）前有四拍圓點倒數：音檔先播 intro，拍速由整首歌詞的逐字時間推估（web/src/lib/countin.ts），點隨拍消失、數完正好開唱；前奏不足四拍時壓縮，長間奏後同樣倒數。
17. 有 MV 時 display 以影片作為背景；沒有 MV 則用漸層背景。
18. Remote 搜尋同步列本地快取（「快取」badge、立即可播）與音源外掛結果（繁體）；YouTube 另有專搜分頁。
19. Remote 不需選唱法：伺服器自動 AI（可用時）/ 否則 INSTANT；可插歌、卡歌、即時調導唱。
20. YouTube 專搜的歌直接播放下載的影片，歌詞不爬也不顯示。
21. 未開唱時點歌 QR 顯示在畫面正中心（大圖＋「用手機掃碼點歌」）；第一首載入開唱後移到右上角，掃描即開 /remote。
```

---

# 24. Git

使用 Git 在每個進度上進行 commit。
