"""Turn FLAC AI stems from before 2026-10-23 into AAC .m4a.

New separations already write vocals.m4a / instrumental.m4a (ktv.separate);
the server keeps playing old FLAC pairs until this has run:

    python server/convert_stems.py                 # list what would change
    python server/convert_stems.py --write         # convert, FLAC moved to backup
    python server/convert_stems.py --write --delete  # convert, FLAC deleted

Safe while the server runs: the AAC pair is written under temporary names and
renamed only when both are done, so a song never plays half-converted, and
the FLAC files are moved away after that.  pitch.json stays (the melody does
not change).  The backup folder defaults to <data>/stems-flac-backup/<song>/.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ktv import config  # noqa: E402
from ktv.separate import STEM_CODEC  # noqa: E402


def encode(src: Path, dst: Path) -> None:
    proc = subprocess.run(
        [config.FFMPEG_BIN, "-v", "error", "-y", "-i", str(src), "-vn", *STEM_CODEC, str(dst)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError(proc.stderr.strip()[-300:] or "ffmpeg failed")


def mb(size: int) -> str:
    return f"{size / 1e6:.1f} MB"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="convert (default: only list)")
    parser.add_argument("--delete", action="store_true", help="delete the FLAC instead of backing it up")
    parser.add_argument("--backup", type=Path, default=config.DATA_DIR / "stems-flac-backup")
    args = parser.parse_args()

    new_names = (config.VOCALS_FILE, config.INSTRUMENTAL_FILE)
    todo = []
    for directory in sorted(p for p in config.TRACKS_DIR.iterdir() if p.is_dir()):
        if all((directory / n).exists() for n in new_names):
            continue
        for pair in config.LEGACY_STEMS:
            if all((directory / n).exists() for n in pair):
                todo.append((directory, pair))
                break
    if not todo:
        print("沒有要轉的 FLAC 分離檔")
        return 0

    before = after = 0
    failed = 0
    for i, (directory, pair) in enumerate(todo, 1):
        olds = [directory / n for n in pair]
        size = sum(p.stat().st_size for p in olds)
        print(f"[{i}/{len(todo)}] {directory.name}  {mb(size)}", end="", flush=True)
        if not args.write:
            print()
            before += size
            continue
        temps = [directory / (n + ".convert") for n in new_names]
        try:
            for old, temp in zip(olds, temps):
                encode(old, temp)
            for temp, name in zip(temps, new_names):
                os.replace(temp, directory / name)
        except Exception as exc:  # one bad file must not stop the batch
            failed += 1
            print(f"  失敗：{exc}")
            continue
        finally:
            for temp in temps:
                temp.unlink(missing_ok=True)
        if args.delete:
            for old in olds:
                old.unlink()
        else:
            target = args.backup / directory.name
            target.mkdir(parents=True, exist_ok=True)
            for old in olds:
                shutil.move(str(old), str(target / old.name))
        new_size = sum((directory / n).stat().st_size for n in new_names)
        before, after = before + size, after + new_size
        print(f" -> {mb(new_size)}")

    if args.write:
        print(f"完成 {len(todo) - failed} 首，{mb(before)} -> {mb(after)}"
              + (f"，{failed} 首失敗" if failed else "")
              + ("" if args.delete else f"；FLAC 備份在 {args.backup}"))
    else:
        print(f"共 {len(todo)} 首、{mb(before)}；加 --write 開始轉")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
