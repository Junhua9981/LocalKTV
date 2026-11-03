"""Re-run 简繁 conversion over what is already cached.

Network lyrics and aggregate titles/artists were converted once, at fetch
time, and stored that way.  After the converter changes (台灣用字, the
zh_keep.txt list) run this to bring the cache in line:

    python server/retrad.py          # show what would change
    python server/retrad.py --write  # apply

Touched: lyrics fetched from the network (the manifest names a
`lyrics_provider`; uploaded ones only with --include-manual), manifest
title/artist, and the `tracks` rows of non-YouTube sources (YouTube titles
are shown as the uploader wrote them).  Text goes back to 简体 character by character first,
so earlier mis-conversions (頭發, 千裏, 北鬥) are converted afresh.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ktv import config, textnorm  # noqa: E402


def reconvert(text: str) -> str:
    # to_simplified is characters only, no vocabulary swaps on the way back
    if not text or textnorm.is_japanese(text):
        return text  # 日文不轉（簡繁轉換會把 内/盗/未来 變成 內/盜/未來）
    return textnorm.to_traditional(textnorm.to_simplified(text))


def show(label: str, old: str, new: str) -> None:
    pairs = sorted({f"{a}→{b}" for a, b in zip(old, new) if a != b})
    print(f"  {label}: {' '.join(pairs[:20])}{' …' if len(pairs) > 20 else ''}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="apply the changes")
    parser.add_argument(
        "--include-manual", action="store_true", help="also re-convert uploaded lyrics"
    )
    args = parser.parse_args()
    changed = 0

    for directory in sorted(config.TRACKS_DIR.glob("*/")):
        manifest_path = directory / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        youtube = directory.name.startswith("youtube_")
        updates = {}
        for key in ("title", "artist"):
            if not youtube and isinstance(manifest.get(key), str):
                new = reconvert(manifest[key])
                if new != manifest[key]:
                    updates[key] = new
        lyric_changes = []
        if manifest.get("lyrics_provider") or (
            args.include_manual and manifest.get("lyrics_manual")
        ):
            for path in sorted(directory.glob("lyrics.*")):
                old = path.read_text(encoding="utf-8")
                new = reconvert(old)
                if new != old:
                    lyric_changes.append((path, old, new))
        if not updates and not lyric_changes:
            continue
        changed += 1
        print(directory.name)
        for key, new in updates.items():
            show(key, manifest[key], new)
        for path, old, new in lyric_changes:
            show(path.name, old, new)
        if args.write:
            if updates:
                manifest.update(updates)
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            for path, _, new in lyric_changes:
                path.write_text(new, encoding="utf-8")

    if config.DB_PATH.exists():
        conn = sqlite3.connect(str(config.DB_PATH))
        with conn:
            rows = conn.execute(
                "SELECT id, title, artist FROM tracks WHERE source != 'youtube'"
            ).fetchall()
            for track_id, title, artist in rows:
                new_title, new_artist = reconvert(title), reconvert(artist)
                if (new_title, new_artist) == (title, artist):
                    continue
                changed += 1
                print(f"db {track_id}: {title} / {artist} -> {new_title} / {new_artist}")
                if args.write:
                    conn.execute(
                        "UPDATE tracks SET title = ?, artist = ? WHERE id = ?",
                        (new_title, new_artist, track_id),
                    )
        conn.close()

    verb = "updated" if args.write else "would change (re-run with --write)"
    print(f"{changed} item(s) {verb}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
