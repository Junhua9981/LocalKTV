# OpenCC 字典（簡體 ⇄ 台灣正體）

`textnorm.to_traditional()` 與 `textnorm.to_simplified()` 用的字典檔，原樣取自
[BYVoid/OpenCC](https://github.com/BYVoid/OpenCC) `data/dictionary/`
（commit `8cf737a4`，Apache-2.0，授權全文見 `LICENSE`）。

轉換流程等同 OpenCC 的 `s2tw` 設定：`STPhrases` + `STCharacters` 做最長詞匹配，
再套 `TWVariantsPhrases` + `TWVariants` 換成台灣用字。只換字形，不做 `s2twp`
那種詞彙替換（軟件→軟體、信息→資訊）。

反方向（出站查詢轉简体）等同 OpenCC 的 `tw2s`：先套 `TWVariantsRevPhrases`
與反查的 `TWVariants`（上游建置時才產生的 `TWVariantsRev` 在程式裡反轉），
再用 `TSPhrases` + `TSCharacters` 轉简体。

只放字典、不引入 OpenCC 套件，維持「不加新依賴」的規則。要更新就從上游覆蓋這七個檔。
