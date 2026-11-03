# 外掛資料夾

LocalKTV 本身只內建 YouTube（yt-dlp）。曲庫搜尋、取檔、熱門榜和平台封面都可以由
**音源外掛**提供：伺服器啟動時會從這個資料夾載入名為 `ktv_sources` 的 Python 套件
（也可用環境變數 `KTV_PLUGINS_DIR` 指到別的資料夾），呼叫它的 `register(registry)`。

本 repo 不附帶任何外掛，這份說明告訴你怎麼自己寫一個。這個資料夾除了本說明以外都不進 git。

## 沒有外掛時

- 曲庫搜尋：先列已下載的歌，再問 YouTube（下載原曲音訊，照常找歌詞、做 AI 分離）
- 沒有熱門榜分頁，封面只問 iTunes
- `/host` 的「主要音源」只有「自動」

啟動紀錄（`journalctl -u localktv` 或終端機）會寫：

```text
sources: no ktv_sources plug-in in …/server/plugins — YouTube only
```

裝好外掛後則是 `sources: catalogue=True charts=False`（依你註冊了什麼）。

## 最小的外掛

```text
server/plugins/
└── ktv_sources/
    └── __init__.py
```

```python
# server/plugins/ktv_sources/__init__.py
from __future__ import annotations

import subprocess
from pathlib import Path

from ktv import textnorm
from ktv.media import probe_duration
from ktv.models import Track


class MyCatalog:
    def sources(self) -> list[tuple[str, str]]:
        # /host「主要音源」的選項（不含「自動」）；只有一個來源就回 [] 也可以
        return [("mine", "我的音源")]

    def search(self, query: str, limit: int = 10, client: str = "", page: int = 1) -> list[Track]:
        # 在 worker thread 執行。中國平台的索引多半是简体，先轉再查；
        # 回傳前把歌名、歌手轉回台灣繁體（畫面與本地庫一律繁體）。
        hits = my_api_search(textnorm.to_simplified(query), limit, page)  # 你自己的搜尋
        return [
            Track.from_source(
                "mine",                      # track.source
                f"netease_{hit['id']}",      # track.source_id（見下方「歌詞會跟著 id 走」）
                title=textnorm.to_traditional(hit["title"]),
                artist=textnorm.to_traditional(hit["artist"]),
                duration_ms=hit.get("duration_ms", 0),
                cover=hit.get("cover", ""),  # 封面網址，可留空
            )
            for hit in hits
        ]

    def acquire(self, track: Track, directory: Path, only_own: bool = False) -> Path:
        # 在 worker thread 執行。把音訊存成 directory/source.wav 並回傳那個路徑；
        # 失敗就 raise，伺服器會改找 YouTube。
        directory.mkdir(parents=True, exist_ok=True)
        raw = directory / "download.tmp"
        my_download(track, raw)  # 你自己的下載
        wav = directory / "source.wav"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-ac", "2", "-ar", "44100", str(wav)],
                       check=True)
        raw.unlink(missing_ok=True)
        if not probe_duration(wav):
            raise RuntimeError("downloaded file is not audio")
        return wav

    def set_active_source(self, value: str) -> None:
        if value != "auto" and value not in dict(self.sources()):
            raise ValueError(f"unknown audio source: {value}")
        self._active = value

    def active_source(self) -> str:
        return getattr(self, "_active", "auto")

    def backing_off(self, host: str) -> bool:
        # 某個主機正在冷卻（例如連錯幾次）就回 True，封面查詢會跟著避開它
        return False


def register(registry) -> None:
    registry.register_catalog(MyCatalog())
```

重開服務後，遙控器的「曲庫」搜尋就會先問你的外掛。完整的介面定義在
[`server/ktv/providers/registry.py`](../ktv/providers/registry.py)（`Catalog`、`Charts`），
測試用的假外掛 [`server/fake_catalog.py`](../fake_catalog.py) 也可以拿來參考。

## 介面細節

### 曲庫 `register_catalog(catalog)`

| 方法 | 什麼時候呼叫 | 注意 |
| --- | --- | --- |
| `search(query, limit, client, page)` | 遙控器在「曲庫」打字搜尋 | 在 thread 裡跑。`client` 是遙控器代號：同一支送出新搜尋時，你可以丟出 `registry.SearchSuperseded` 放棄舊的那次，省下請求。`page` 從 1 開始（往下捲動載入更多）。 |
| `acquire(track, directory, only_own)` | 點了你的曲目、要下載音訊時 | 寫 `directory/source.wav`。`only_own=True`（管理員在曲庫管理指定某個版本重抓）時只能用這個 track 本身，不要改找別首。 |
| `sources()` / `set_active_source()` / `active_source()` | `/host`「主要音源」 | 釘選只影響下一次取檔；重啟回 `auto`。 |
| `backing_off(host)` | 查封面前 | 讓封面查詢不要去打你正在冷卻的主機。 |

搜尋結果的 `Track`：

- `id` 由 `Track.from_source(source, source_id)` 組成 `"{source}_{source_id}"`，也是快取資料夾名稱
  （`data/tracks/<id>/`），**同一首歌每次搜尋都要回一樣的 id**，才會命中快取。
- `duration_ms` 不知道可以給 0，下載後伺服器會用 ffprobe 量出來。
- `alternates`：同一首歌在別的平台的 `source_id` 清單，取檔失敗時你可以自己拿來試。
- `cover`：封面網址。圖片主機要用 `registry.register_image_hosts("y.gtimg.cn", ".music.126.net")`
  加進允許清單，遙控器才會透過伺服器代抓。

伺服器在 `search` 外面已經做好：已下載的歌排最前面、同歌名＋歌手（繁簡、大小寫、標點都不計）去重。
簡繁轉換請自己呼叫 `ktv.textnorm`：`to_simplified()` 用在送出的查詢，`to_traditional()` 用在拿回來的歌名、歌手。

### 歌詞會跟著 id 走（QQ 音樂／網易雲音樂）

LocalKTV 內建的網路歌詞（`server/ktv/providers/lyrics_net.py`）會依序問
**網易雲音樂（YRC 逐字）→ QQ 音樂（QRC 逐字）→ 酷狗 → LRCLIB**。
如果你的曲目本來就對應到這兩個平台的歌，把平台的歌曲 id 交給伺服器，
它就會直接用 id 抓那首歌的逐字歌詞，不用靠歌名搜尋猜版本：

- `source_id` 寫成 `netease_<網易雲歌曲 id>` 或 `tencent_<QQ 音樂 songmid 或數字 id>`；或
- 在 `acquire` 裡把實際音訊來自哪一首寫進 `directory/manifest.json`（保留裡面原有的欄位）：

  ```python
  import json

  manifest_path = directory / "manifest.json"
  manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
  manifest.update(
      audio_platform="wy",        # wy = 網易雲、tx = QQ 音樂、"" = 不屬於任何平台（歌詞改用搜尋）
      audio_song_id="1234567",
      audio_duration_ms=269000,   # 實際音檔長度，歌詞搜尋會用它排除別的版本
  )
  manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
  ```

  一定要寫在傳進來的 `directory`：管理員重抓版本時它是暫存資料夾，不是 `data/tracks/<id>/`。

所以做外掛最省事的路線，是**以 QQ 音樂或網易雲音樂的曲庫當搜尋來源**：兩家都有公開的搜尋介面，
回傳的歌曲 id 直接就能拿到逐字歌詞與封面（`y.gtimg.cn`、`music.126.net`）。
音訊要從哪裡來（例如你自己帳號有權播放的內容），由你自己決定、自己負責。

### 熱門榜 `register_charts(charts)`

`charts` 要有兩個方法：

- `index() -> list[dict]`：每個來源一筆
  `{"key", "label", "page", "boards": [{"key", "group", "group_label", "lang", "label", "ranked"}]}`
- `async get_chart(key) -> dict`：
  `{"source", "label", "page", "fetched_at", "stale", "error", "boards": [{…上面的欄位, "period", "fetched_at", "stale", "songs": [{"rank", "title", "artist", "no", "tag"}]}]}`；
  不認得的 key 就 `raise KeyError`。

欄位的意思見 [`web/src/types.ts`](../../web/src/types.ts) 的 `ChartSource`／`Chart`。
榜單只顯示名次、歌名、歌手；點下去仍然走一般搜尋，所以榜單和曲庫可以來自不同地方。
請自己做快取（榜單通常一週到一個月才更新），別讓每次打開分頁都去打對方網站。

### 封面 `register_cover_provider(name, search)`

`search(query) -> list[(title, [artists], image_url)]`，`query` 是「歌名 第一位歌手」。
會排在內建的 iTunes 前面，依註冊順序查。記得同時 `register_image_hosts(...)`。

## 測試與檢查

- 外掛的設定請自己讀環境變數（可以寫在 `/etc/localktv.env`）；`KTV_AGG=0` 會讓伺服器完全不用外掛的曲庫。
- `server/registry_test.py` 示範了有外掛／沒外掛兩種情況的行為。
- 部署到 VPS 時，`deploy/README.md` 的打包指令會把 `server/plugins/` 一起帶上。

## 請有禮貌地使用別人的服務

- 同一個主機的請求要隔開（建議至少幾秒）、連續失敗就冷卻一段時間，不要立即重試。
- 搜尋結果和下載的檔案都要快取，同一首歌不要重複下載。
- 你的外掛用到的內容（音訊、歌詞、封面）版權屬於原權利人；請只使用你有權使用的內容，
  並遵守各平台的使用條款。LocalKTV 不提供、也不散布任何音樂。
