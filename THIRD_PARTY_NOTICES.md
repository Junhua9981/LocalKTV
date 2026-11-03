# Third-party notices

LocalKTV itself is licensed under the Apache License 2.0 (`LICENSE`, `NOTICE`).
The material below is bundled in this repository, or installed as a
dependency, under its own licence.

## Bundled in this repository

| Material | Where | Licence |
| --- | --- | --- |
| [OpenCC](https://github.com/BYVoid/OpenCC) dictionaries (STCharacters, STPhrases, TSCharacters, TSPhrases, TWVariants, TWVariantsPhrases, TWVariantsRevPhrases; commit `8cf737a4`) | `server/ktv/opencc/` | Apache-2.0 — full text in `server/ktv/opencc/LICENSE` |
| Noto Sans TC, weight 900 (woff2 slices from `@fontsource/noto-sans-tc` 5.3.1), © Google Inc. | `web/public/fonts/noto-sans-tc-900/` | SIL Open Font License 1.1 — `OFL.txt` in that folder |
| Noto Sans JP, weight 900 (woff2 slices from `@fontsource/noto-sans-jp`), © Google Inc. | `web/public/fonts/noto-sans-jp-900/` | SIL Open Font License 1.1 — `OFL.txt` in that folder |
| [Chakra Petch](https://github.com/m4rc1e/Chakra-Petch) 600/700 (Latin subset), © 2018 The Chakra Petch Project Authors | `web/public/fonts/chakra-petch-*.woff2` | SIL Open Font License 1.1 — `web/public/fonts/OFL-ChakraPetch.txt` |
| QQ Music client cipher, transcribed from `qqmusic_crypto.go` in [ibratabian17/lyricsplus](https://github.com/ibratabian17/lyricsplus) | `server/ktv/qrc_des.py` (`qq_des3` / `qq_decrypt_hex`) | Apache-2.0 |

[chenmozhijin/LDDC](https://github.com/chenmozhijin/LDDC) and
[Robotxm/ESLyric-LyricsSource](https://github.com/Robotxm/ESLyric-LyricsSource)
(GPL-3.0) were read for protocol research only; no code was copied from them.

## Python dependencies (`server/requirements*.txt`, installed by pip)

| Package | Licence | Notes |
| --- | --- | --- |
| fastapi | MIT | |
| uvicorn[standard] | BSD-3-Clause | |
| qrcode | BSD | |
| yt-dlp | Unlicense | |
| aiofiles | Apache-2.0 | |
| sudachipy | Apache-2.0 | |
| sudachidict_core | Apache-2.0 | |
| pycryptodome | BSD / Public Domain | tests only |
| onnxruntime (optional, AI separation) | MIT | pulls in numpy (BSD-3-Clause and others) |

## Web dependencies (`web/package.json`, installed by npm)

| Package | Licence |
| --- | --- |
| vue, vue-router | MIT |
| vite, @vitejs/plugin-vue, vue-tsc, @types/node | MIT |
| typescript | Apache-2.0 |

## Promo video

| Material | Licence |
| --- | --- |
| Soundtrack of the README promo video: "Let's Go Fishin'" by Karissa Hobbs, from Free Music Archive | [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/) |

The footage is screen recordings of LocalKTV itself. The 熱門榜 (charts)
scene shows a feature that needs a music-source plug-in.

## Downloaded at setup, not bundled

- The vocal-separation model (`UVR_MDXNET_KARA_2`) is fetched by
  `server/fetch_model.py` from the Ultimate Vocal Remover public model
  repository into `models/`, which git ignores. Check that repository for the
  model's terms before redistributing it.

## Not part of this project

Apart from the promo video's soundtrack (below), this repository ships no
music, lyrics, music videos or cover art. Songs,
lyrics and videos are fetched at run time from sources the user configures,
and the user is responsible for having the right to use them. Music-source
plug-ins (`server/plugins/`) are not part of this repository.
