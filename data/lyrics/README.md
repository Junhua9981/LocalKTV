# 歌詞資料夾

把歌詞檔放進來即可被 `LocalLyricsProvider` 找到，檔名優先順序：

1. `data/lyrics/<source_id>.ttml|.lrc|.yrc|.qrc|.txt`（例如 `abcdEFGH123.lrc`）
2. `data/lyrics/<track-id>.*`（例如 `youtube_abcdEFGH123.lrc`）
3. `data/lyrics/<歌名 - 歌手>.lrc`、`data/lyrics/<歌名>.lrc`

`sample.lrc` 是自寫的格式範例（歌名「範例歌曲」），可直接照著改。

找到後會複製進 `data/tracks/<track-key>/lyrics.*` 建立 cache。
之後歌詞一律以 cache 為優先（可在 /host 調整 `lyrics_offset_ms` 並保存）。

也可用 HTTP 上傳：`POST /api/lyrics/{track_id}`（body: `{"content": "...", "format": "ttml"}`）。
