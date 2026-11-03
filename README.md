# LocalKTV

Single-room, local-first Web KTV。一台 PC 同時負責 HTTP/WebSocket、Queue、歌曲取得、歌詞解析、AI 去人聲與快取；真正的音樂與歌詞由 PC 上的 `/display` Web App 播放，其他手機只作為 Host Remote 或點歌 Client。**AI 永遠是 enhancement，Instant DSP 永遠是 fallback。**

規格：見 [SPEC.md](./SPEC.md)。

## Oracle VPS 與登入

已提供 Ubuntu 24.04、公網 IP 的 HTTPS 部署範本：見 [deploy/README.md](./deploy/README.md)。
包含 systemd、Nginx、IP 憑證自動續期與登入保護；頁面、API、音檔及 WebSocket 都受保護。
第一次打開網站會進「首次設定」，用服務紀錄裡的設定碼設管理員密碼（不用 SSH 下指令）。
登入分三種身分：管理員用密碼登入，首頁選「這台當螢幕／這台當遙控器」；別人的筆電打開網站顯示配對 QR，
管理員手機掃了就變成螢幕；客人掃螢幕上的限時點歌 QR 加入，只能用 `/remote`。
螢幕打開就自動開場，螢幕全關 30 分鐘後自動結束、客人登出。
VPS 設 `KTV_AUTH_REQUIRED=1` 與 `KTV_PUBLIC_URL` 啟用；未配置登入的本機 LAN 模式維持原行為。

## 多房間（包廂）

一台伺服器可以同時開好幾間房，每間有自己的點歌清單、播放、音量／升降 Key／背景模式、電視、遙控器、主控與手機麥克風：

| 頁面 | 網址 |
|---|---|
| 房間列表（管理員） | `/` |
| 電視 / 遙控器 / 主控 | `/r/<房號>/display`、`/r/<房號>/remote`、`/r/<房號>/host` |

- 預設房 `main`（大廳）永遠存在；舊網址 `/display` `/remote` `/host` 會進 `main`，已配對的電視不用重配。
- 房間在首頁或 `/admin` 新增（房號：小寫英數與 `-`，最多 16 字），上限 `KTV_MAX_ROOMS`（預設 6）。房間清單存在 SQLite，點歌清單只在記憶體。
- 共用的：歌曲快取、歌詞、搜尋、榜單、下載與 AI 分離。同一首歌兩間都點只下載一次；全伺服器同時最多下載 `KTV_PREPARE_CONCURRENCY`（預設 2）首，避免 1 GB VPS 被幾間房同時點歌拖垮。「主要音源」是全伺服器設定。
- 登入模式下：每間的螢幕打開就自動開場（螢幕全關 30 分鐘後結束），各有一組輪換的點歌 QR，A 房的 QR 進不了 B 房；客人只能用自己那間的遙控器；螢幕配對（手機掃螢幕上的配對 QR）時選房間，之後可在 `/admin` 移到別間；主控仍只有管理員能用。

## 啟動

```powershell
.\start-karaoke.ps1        # 或直接雙擊 start-karaoke.bat
```

Linux：

```bash
./start-karaoke.sh
```

第一次執行會自動：建立 `.venv`、安裝 Python 相關套件、`npm install` + `npm run build` 建置 UI。
`start-karaoke.sh` 另外會檢查 ffmpeg 是否可用品（缺了會警告），無圖形介面的主機上
自動補 `--no-browser`（有 DISPLAY/WAYLAND_DISPLAY 則照舊自動開瀏覽器）。
啟動後終端機會顯示：

```text
KTV ready
Display: http://localhost:8080/display
Join:    http://192.168.x.x:8080
[QR CODE]
```

並自動開啟 `/display`。

> `/display` 必須用 `http://localhost:8080` 開啟：升降 Key 走 AudioWorklet，只在
> 安全環境（localhost / HTTPS）可用。開成 `http://192.168.x.x:8080/display` 時
> `isSecureContext` 為 false、`audioWorklet` 不存在 → Host 的升降 Key 控制項會
> 自動停用並提示，其餘功能不受影響。（掃碼給手機的 join 網址本來就不是 display）

| Route     | 裝置        | 職責 |
|-----------|-------------|------|
| `/display` | KTV PC     | 唯一播放聲音的 client：WebAudio 混音、LyricStage 逐字歌詞、開唱前四拍圓點倒數、背景可選原版 MV 或文字PV（逐字動態歌詞，可關掉底部卡拉OK歌詞讓它佔滿畫面；遙控器／host 的「畫面」切換，全場共用）、點歌 QR（未開唱置中／開唱後右上角）、播放時鐘（每秒回報 server） |
| `/host`    | Host 手機  | play / pause / skip / seek、queue 上下移 / 優先 / 刪除、唱法、導唱 0–100%、音量、升降 Key ±3 半音（session 級）、**主要音源切換**（自動＋音源外掛提供的來源）、歌詞偏移（不發聲） |
| `/remote`  | 訪客手機   | 兩個頁籤：**點歌**（曲庫／熱門榜／YouTube）、**清單**（全部／我點的，顯示「你的下一首還有幾首」）；底部常駐正在播放（卡歌），點開調導唱、升降 Key、音量；自己的歌可插到最前面或取消 |

## 開發

```powershell
# 後端（8080）
.\.venv\Scripts\python.exe server\main.py

# 前端（5173，proxy 到 8080）
cd web; npm run dev

# 型別檢查 / 建置
cd web; npm run typecheck; npm run build
```

## 測試

```powershell
# pipeline 規則：cache hit、同曲 dedupe、lyrics offset 持久化、離線 fallback
.\.venv\Scripts\python.exe server\smoke_test.py

# 登入：管理員／電視配對／客人邀請、角色限制、重啟保留、Origin、暴力猜測限制、fail-closed
.\.venv\Scripts\python.exe server\auth_test.py

# 歌詞：QRC/LRC/YRC 解析、QQ QRC 解密、簡→繁、供應商節流與重試
.\.venv\Scripts\python.exe server\lyrics_test.py

# 音源外掛註冊：有外掛走外掛、沒外掛只剩 YouTube（曲庫改搜 YouTube、主要音源只剩自動、沒熱門榜、封面只問 iTunes）
.\.venv\Scripts\python.exe server\registry_test.py

# 5 台手機 + Host 同時連線點歌
.\.venv\Scripts\python.exe server\ws_smoke.py 5

# AI 分離與評分的標準音：cache hit、閒置補做、暫停、失敗計次、pitch.json 補做（要 numpy + ffmpeg）
.\.venv\Scripts\python.exe server\ai_test.py
.\.venv\Scripts\python.exe server\pitch_test.py --vocals data\tracks\<歌>\vocals.m4a

# 評分規則與電視端音高偵測（Node 22）
node --experimental-strip-types web\tests\scoring.test.ts
node web\tests\score-pitch.test.mjs
```

## 目錄

```text
LocalKTV/
├── server/           FastAPI + Queue/Session + WS + Providers + Pipeline + AI worker
│   ├── ktv/
│   │   ├── app.py        REST / WebSocket / SPA
│   │   ├── state.py      Session・Queue・Playback state owner
│   │   ├── pipeline.py   prefetch、INSTANT READY、AI job manager
│   │   ├── ai.py         Separator 介面、ONNX 子行程（暫停／中止）、stub
│   │   ├── separate.py   AI 分離程式（UVR MDX-Net KARA_2，逐段處理，約 300 MB）
│   │   ├── store.py      SQLite + media cache
│   │   ├── textnorm.py   简→繁轉換 + fold 比對鍵（搜尋/去重共用）
│   │   ├── lyrics_parse.py  伺服端歌詞解析（QRC/LRC/YRC/TXT → lines）
│   │   ├── qrc_des.py    QQ QRC 解密（Apache-2.0 移植 + 標準 DES/3DES fallback）
│   │   ├── providers/registry.py  音源外掛的註冊點：曲庫搜尋/取檔・榜單・平台封面都經過這裡
│   │   ├── providers/    YouTube・Router・歌詞（本機＋網路網易→QQ→酷狗→LRCLIB）
│   ├── smoke_test.py     pipeline 驗收
│   ├── lyrics_test.py    歌詞解析 / 歌詞供應商
│   ├── registry_test.py  音源外掛註冊 / 沒外掛時的 YouTube-only 模式
│   ├── plugins/          音源外掛 `ktv_sources`（不在這個 repo）：曲庫搜尋與取檔、熱門榜、平台封面
│   └── ws_smoke.py       5 client 並發驗收
├── web/              Vue 3 + Vite + TS（display / host / remote）
├── models/           AI 模型權重
├── data/
│   ├── tracks/<track-key>/   manifest.json・source.wav・lyrics・stems（AAC .m4a）
│   ├── lyrics/               外部歌詞投放處（見 data/lyrics/README.md）
│   └── ktv.db                tracks + lyrics_offset_ms
└── start-karaoke.ps1 / .bat / .sh
```

## 音源（搜尋與取檔）

1. 搜尋分兩個來源（`GET /api/search?source=main|youtube`）：
   - `main`（曲庫）＝**本地快取優先**（SQLite `tracks` 索引，`textnorm.fold` 精確比對：標題 / 標題＋歌手 /
     只輸入歌手→列該歌手全部，Live 版 fold 不同自然分開，只列已下載者）＋**音源外掛**的曲庫搜尋
     （`server/plugins/`，見 [server/plugins/README.md](server/plugins/README.md)；`KTV_AGG=0` 關閉）。
     **沒裝外掛時改問 YouTube**（下載原曲音訊，照常找歌詞、做 AI 分離）。同 `fold(title, artist)` 跨源去重、快取勝。
   - `youtube`＝YouTube 專搜，查詢**自動附加 `KTV` 關鍵字**；結果帶 `video_only` —
     直接播放下載的影片，**歌詞不爬也不顯示**。
   - 簡繁：送出的查詢轉简体、外部結果轉台灣用字繁體（`textnorm.py`：出站 `tw2s`、入站 `s2tw`，都用內附的 OpenCC 字典，逐字時間標記不影響詞組判斷），畫面與本地庫一律繁體。不想被轉的專有名詞（如 周杰倫）寫進 `server/ktv/zh_keep.txt`；改完後 `server/retrad.py --write` 可重轉已快取的歌名與歌詞。
2. 取檔：外掛的曲目由外掛下載；YouTube 曲目走 yt-dlp，失敗時才請外掛找同一首歌。
   `/host`「主要音源」列出外掛提供的來源（沒外掛時只有「自動」），只影響下一次取檔。
   全部共用同一 `data/tracks/<track-key>/` 目錄（歌詞／MV／cache hit 不變）。
3. 下載後 ffprobe 時長校驗（±15s）才轉 `source.wav`；搜尋結果不帶時長的，READY 前用 ffprobe 回填 `duration_ms`。
4. `/remote` 搜尋結果帶「已下載 / 線上 / 影片」標籤（對應快取 / 外掛 / YouTube）。

## 榜單

`/remote` 點歌頁的「熱門榜」由音源外掛提供（`registry.register_charts`）；沒裝外掛就沒有熱門榜。
榜單只列歌名／歌手，點歌仍走 `GET /api/search`。

## 音訊模式（Display 端 WebAudio 即時運算）

| 模式 | 路徑 |
|------|------|
| ORIGINAL | 原訊號直出 |
| INSTANT | `L - R` 中置消除，保留 < ~150 Hz 低頻；輸出 `instant × (1 - 導唱) + original × 導唱` |
| AI | `instrumental + vocals × 導唱`（stems 未就緒時**自動退回 INSTANT**） |

排隊時的預設選擇（`/remote` 不提供挑選）：AI 分離器可用 → **AI**（整份 queue 預先排入分離 worker），否則 → **INSTANT**；`/host` 仍可手動改任意模式。

## 手機麥克風（原型）

`/remote` 的「麥克風」分頁把手機變成麥克風：手機收音後用 WebRTC **直連** `/display`，伺服器只轉送連線資訊（WS `mic.signal`），不經手聲音。

- 只能在 **HTTPS**（或 localhost）下用；區網 `http://192.168…` 手機瀏覽器會擋麥克風。
- 手機和電視連同一個 Wi‑Fi 延遲最低，實際約 0.1–0.2 秒（瀏覽器的極限，比不上實體麥克風）。STUN 用 Google 公用伺服器，沒有 TURN。
- 短按開／關、按住超過 0.35 秒放開＝只在按住時收音；各自調音量。電視同時最多 4 支。
- 傳輸方式（手機上選，記在手機 localStorage `ktv-mic-transport`）：
  - **低延遲**（預設）：不用瀏覽器的語音軌，自己傳。手機 AudioWorklet（`web/public/worklet/mic-capture.js`）每 256 取樣（約 5ms）打包、降成約 24kHz 16-bit，經不重送、不排序的 DataChannel 送出；電視 AudioWorklet（`mic-playout.js`）自己管抖動緩衝：目標 20ms 起跳、斷音就 +5ms、3 秒沒斷音就 -1ms（10–80ms 之間），亂序封包補回原位，囤太多就讀快 1% 追上。Wi‑Fi 抖動大時會偶爾斷音，手機頁顯示斷音次數。
  - **穩定**：瀏覽器內建 WebRTC 語音軌，Opus 10ms 封包（電視回的 answer 帶 `a=ptime:10`）、接收端抖動緩衝設為最低；緩衝仍由瀏覽器決定，實測常超過 100ms。
- 手機麥克風頁每秒顯示電視回報的延遲拆解（收音／網路／電視緩衝／輸出），以及是否區網直連。
- 電視端麥克風用**自己的 AudioContext**（`latencyHint: 0`，向瀏覽器要最小輸出緩衝），不經過伴奏的混音與升降 key（不變調、不吃伴奏音量），經過 120 Hz 低頻濾波與限幅器；手機端關閉回音消除／降噪／自動增益。
- 「穩定」模式下出聲方式可在手機麥克風頁切換（全場共用，電視記在 localStorage）：「混音器」＝上述低延遲混音器；「通話輸出」＝瀏覽器 WebRTC 自己的輸出（`<audio>` 直接播），可能更快但沒有濾波與限幅、量不到輸出延遲、音量最大 100%。
- 防嘯叫靠音量與距離：別對著喇叭唱。

## 唱歌評分

電視（`/display`，電腦上的瀏覽器）聽**電腦本身的錄音輸入**打分，也就是藍牙喇叭麥克風組傳進電腦的收音。手機麥克風延遲太高，不拿來評分。

- **標準答案**：AI 人聲分離做完後，伺服器用 YIN 從 `vocals.m4a` 抽出每 10ms 的音高存成 `pitch.json`（`server/ktv/pitch_ref.py`，子行程、只用 numpy + ffmpeg，4 分鐘的歌幾秒鐘）。人聲軌裡漏進來的伴奏（比伴奏軌小 15 dB 以上）和貝斯先濾掉；電視頁再照逐字歌詞一個字切一個音，只看字唱的那段時間，字裡真的轉音才拆，合唱同時兩個人唱就兩個聲部都算（`web/src/lib/scoring.ts` wordNotes）。只有整行歌詞的歌用伺服器照音高切好的音、只留歌詞行裡的。以前分離好的歌（或舊格式的 pitch.json）在點歌時或閒置補做時補上。**沒有 AI 人聲軌的歌不評分**，電視右上角寫「尚未有評分」；點過的歌閒置時會被補做分離，所以通常幾天內就有評分。
- **收音**：電視頁開 `getUserMedia`（關掉回音消除／降噪／自動增益），只分析、不播出。AudioWorklet（`web/public/worklet/score-pitch.js`）降到約 16kHz，每 10ms 一個音高。要 HTTPS 或 localhost，第一次會跳麥克風權限。藍牙麥克風組要設成 Windows 的**預設錄音裝置**；有些麥克風組只在喇叭裡混音、不傳進電腦，這種電視會顯示「沒有收到麥克風聲音」。
- **時間對齊**：歌曲時間 = 播放位置 − 升降 Key 延遲 − 瀏覽器回報的播放／錄音延遲 − **評分延遲**（遙控器調，預設 150ms，藍牙常見 100–300ms）。唱準了軌跡卻老是落在音條後面，就把評分延遲調大。
- **計分**（`web/src/lib/scoring.ts`）：不分八度；±50 音分滿分、到 ±150 歸零；時間容許 ±120ms。每句（歌詞一行，沒歌詞就用停頓切句）＝音準 70％＋節奏 20％（該唱時有沒有出聲）＋穩定 10％（0.4 秒以上長音的抖動），判定 PERFECT／GREAT／GOOD／MISS。整首換成 0–100 分（`100 × 原始分^0.6`，認真唱大多 70–90），S／A／B／C／D。
- **畫面**：歌詞下方的音高條（遙控器可關）、每句判定、唱完 8 秒結算畫面（至少唱了一句才出）。
- **紀錄**：分數存在 SQLite `scores`（歌、點歌的人、房間），遙控器跳通知、顯示本曲前三名，結算畫面寫本曲新紀錄或本曲最高。
- **遙控器**「評分」：唱歌評分開關、音高條開關、評分延遲，全場共用。

## 歌詞

1. 本地/手動優先：放進 `data/lyrics/<source_id>.lrc|.qrc|.yrc|.txt`（或用 `POST /api/lyrics/{track_id}` 上傳）
2. 點歌後複製進 `data/tracks/<track-key>/lyrics.*` 建立 cache
3. 網路歌詞為 READY 後的背景 enrichment（不阻塞播放），**歌詞跟著音檔走**＋**逐字優先**：manifest 記錄的實際音源是 QQ 就先按該 id 取 QQ 歌詞、是網易就按 id 取網易歌詞（關鍵詞／YouTube 音檔沒有目錄 id，只走搜尋；搜尋結果與實測音檔長度差 >20s 的版本不採用）；網易雲 YRC（一次請求同時回 YRC + LRC）→ QQ Music QRC（`qrc` 欄，或 `lyric` 欄裡 `<QrcInfos>` 的 `LyricContent`）→ **兩家都沒有逐字才交出先前收下的第一份逐行答案** → 酷狗 LRC → LRCLIB LRC（僅在什麼都沒收到時）；
   每個供應商只做節流式請求（間隔 1.5s、連錯 3 次冷卻 10 分鐘、瞬時錯誤只重試一次），抓到即存 track cache（`lyrics.yrc`／`lyrics.qrc`／`lyrics.lrc`），簡→繁在入 cache 前完成
   manifest 同時記錄 `lyrics_provider`／`lyrics_via`（`source_id`｜`search`）／`lyrics_timing`（`word`｜`line`）
4. `GET /lyrics/{track_id}` 由伺服端解析回傳 `lines` 與 `timing`（解析時去掉作詞／作曲／編曲／製作人／OP 等資訊行與開頭「歌名 - 歌手」行；合唱標記「浩：」「合：」轉成每行 `singer`／`part`，卡拉OK 依 part 上色：1 藍、2 粉紅、3 橘、合唱綠），Display 的 LyricStage 在下方約 30% 高度渲染 karaoke
   （第 1、3…句左上、第 2、4…句右下，原地開唱不換位；白字黑框、**粗體、無陰影**，可帶翻譯行；
   `timing=word` 逐字掃描由左往右填入純藍底白框，`timing=line` 整行一次亮起 + 0.3s 淡入、不做掃描）
5. `/host` 可即時調整 `lyrics_offset_ms` 並保存到 SQLite

## 驗收狀態（MVP Acceptance）

| # | 標準 | 狀態 |
|---|------|------|
| 1 | PC 一條指令啟動 | ✅ `start-karaoke.ps1` / `.bat` / `.sh`（Linux） |
| 2 | 手機掃 QR 加入 | ✅ 啟動時印 QR（ASCII）＋ /display 顯示可掃描的點歌 QR |
| 3 | ≥5 手機同時點歌 | ✅ `ws_smoke.py 5` PASS |
| 4 | Queue 連續自動播放 | ✅ Display 回報 ended → server 自動換歌 |
| 5 | Host play/pause/skip/reorder | ✅ `/host` |
| 6 | Display 顯示逐字歌詞 | ✅ 逐字掃描（`timing=word`）／整行亮起淡入（`timing=line`，無逐字歌詞的專屬顯示） |
| 7 | 每首歌可選 Original / Instant / AI | ✅ `/host` 手動選；`/remote` 自動（AI 可用→AI，否則 INSTANT，不讓使用者挑） |
| 8 | Instant 模式不需 preprocessing | ✅ 純 WebAudio 即時運算 |
| 9 | AI 模式產生 vocals + instrumental | ✅ UVR MDX-Net KARA_2（ONNX，`ai_test.py --model`）；閒置時自動補做已快取的歌 |
| 10 | AI 未完成自動 fallback Instant | ✅ stems 缺少時 display 自動走 INSTANT |
| 11 | 導唱 0–100% 即時可改 | ✅ 即時改變 gain，不需重載 |
| 12 | 播放過的 AI song 第二次 cache hit | ✅ stems 已在快取 → 直接 AI READY（`ai_test.py`） |
| 13 | lyrics_offset_ms 可調整並保存 | ✅ `smoke_test.py` |
| 14 | AI/歌詞失敗不使 Queue crash | ✅ `smoke_test.py`（離線失敗案例） |
| 15 | 斷網時已 cache 歌曲仍可唱 | ✅ `smoke_test.py`（offline 案例） |
| 16 | 開唱前四拍圓點倒數 | ✅ 音檔先播 intro，拍速由整首歌詞的逐字時間推估（逐行歌詞用 0.5 秒），數完正好開唱；前奏不足四拍就壓縮、短於 1.2 秒不倒數；長間奏（≥8 秒且 ≥8 拍）後也倒數（無歌詞不倒數） |
| 17 | 有 MV 時作為背景播放 | ✅ READY 後背景下載 480p video-only（`KTV_MV=0` 關閉），無 MV 則用漸層背景 |
| 18 | Remote 搜尋併入本地快取 | ✅ 快取置頂立即可播、外掛結果轉繁體、Live 版分開；YouTube 專搜分頁自動附加 KTV |
| 19 | Remote 唱法自動＋插歌/卡歌/導唱 | ✅ 無 mode → 自動 AI/INSTANT；⤒ 插歌、⏭ 卡歌、導唱 slider 即時生效（smoke + WS 實測） |
| 20 | YouTube 專搜不抓不顯歌詞 | ✅ `video_only` 跳過歌詞鏈、`display.load` 不送 lyrics_url、display 隱藏無歌詞文案 |
| 21 | Display QR 未開唱置中／開唱後右上 | ✅ `/api/qr.svg`（SVG，掃描開 /remote）；`hasSung` 鎖存，DOM 實測兩態與自然觸發 |

## 不做的東西（v0.1）

Multi-room、User accounts、Cloud sync、Redis、PostgreSQL、Native app、Tempo shift、Scoring、Mic recording、Distributed AI workers。

## 研究來源（attribution）

- 歌詞協議研究：[ibratabian17/lyricsplus](https://github.com/ibratabian17/lyricsplus)（Apache-2.0）— QQ 簽名、`GetPlayLyricInfo` 參數、QRC 解密思路；本專案依其原理重新實作（`lyrics_net.py` / `qrc_des.py` docstring 有註記）。
- [chenmozhijin/LDDC](https://github.com/chenmozhijin/LDDC)、[Robotxm/ESLyric-LyricsSource](https://github.com/Robotxm/ESLyric-LyricsSource)（GPL-3.0-only）**僅供協議研究，未複製其程式碼**。
- 歌詞字型：[思源黑體 Noto Sans TC](https://fonts.google.com/noto/specimen/Noto+Sans+TC)（SIL OFL 1.1）900 字重 — 取自 `@fontsource/noto-sans-tc` 5.3.1 的切片 woff2，內嵌於 `web/public/fonts/noto-sans-tc-900/`（`font.css` 只在 /display 載入），瀏覽器只下載用到的字所在的切片，僅 /display 歌詞使用。
- 遙控器 HUD 字型：[Chakra Petch](https://github.com/m4rc1e/Chakra-Petch)（SIL OFL 1.1）— 內嵌於 `web/public/fonts/chakra-petch-*.woff2`（Latin 子集），僅 /remote 的標籤與數字使用。

## 授權與聲明

- 本專案程式碼以 [Apache License 2.0](LICENSE) 授權（另見 [`NOTICE`](NOTICE)）。內附的第三方素材（OpenCC 字典、Noto Sans TC/JP 與 Chakra Petch 字型等）維持各自的原授權，清單見 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
- **本專案不附帶任何音樂、歌詞、MV 或封面。** 歌曲與歌詞都是執行時從使用者自行設定的來源取得，使用者須自行確認有權使用這些內容；授權只涵蓋本專案的程式碼，不涵蓋任何歌曲的版權。
- 音源外掛（`server/plugins/`）不屬於本 repo，也不以本授權發布。
