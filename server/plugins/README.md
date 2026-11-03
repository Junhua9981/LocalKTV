# 外掛資料夾

LocalKTV 啟動時會從這裡載入名為 `ktv_sources` 的 Python 套件（也可用環境變數
`KTV_PLUGINS_DIR` 指到別的資料夾）。外掛在 `register(registry)` 裡向
`server/ktv/providers/registry.py` 註冊：

- **曲庫**（`register_catalog`）：搜尋、取檔、/host「主要音源」的選項
- **榜單**（`register_charts`）：遙控器的「熱門榜」
- **封面**（`register_cover_provider`、`register_image_hosts`）：在 iTunes 之前查的平台

沒有外掛也能用：曲庫搜尋改問 YouTube（下載原曲音訊，照常找歌詞、做 AI 分離），
沒有熱門榜，封面只問 iTunes，「主要音源」設定不顯示。介面細節見 `registry.py`
的 `Catalog` / `Charts`。

這個資料夾除了本說明以外都不進 git。
