"""Reference melody (ktv.pitch_ref) tests - offline, needs numpy + ffmpeg.

Run:  .venv/Scripts/python server/pitch_test.py [--vocals path/to/vocals.m4a]

Covers:
  1. a synthetic "voice" (harmonics + vibrato + breath noise) singing known
     notes comes back as those notes, within 30 cents and 30 ms
  2. silence and noise give no notes
  3. the CLI writes pitch.json atomically (no .part left behind), with the
     raw contour (frames) beside the notes
  4. band leak: a quiet tone in the vocals while the instrumental stem is
     loud, and a bass line far under the melody, are dropped
With --vocals: runs on a real stem and prints note count, voiced share, time.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ktv import pitch_ref  # noqa: E402

SR = 44100
# (start s, duration s, midi) - bass to soprano, with rests between
MELODY = [(0.5, 0.6, 45), (1.3, 0.4, 52), (1.8, 0.8, 57), (2.9, 0.5, 64), (3.5, 1.0, 69),
          (4.8, 0.3, 76), (5.2, 0.7, 81), (6.2, 0.5, 60)]


def voice(melody: list[tuple[float, float, int]], length: float) -> np.ndarray:
    rng = np.random.default_rng(1)
    t = np.arange(int(length * SR)) / SR
    out = 0.003 * rng.standard_normal(len(t))  # room noise
    for start, dur, midi in melody:
        m = (t >= start) & (t < start + dur)
        tt = t[m] - start
        f = 440 * 2 ** ((midi - 69) / 12) * (1 + 0.01 * np.sin(2 * np.pi * 5.5 * tt))  # vibrato
        phase = 2 * np.pi * np.cumsum(f) / SR
        tone = sum(np.sin(k * phase) / k for k in range(1, 8))
        env = np.minimum(1, np.minimum(tt, dur - tt) / 0.02)
        out[m] += 0.3 * env * tone
    return out.astype(np.float32)


def write_wav(path: Path, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    if not ok:
        raise SystemExit(1)


def test_melody(tmp: Path) -> None:
    path = tmp / "voice.wav"
    write_wav(path, voice(MELODY, 7.5))
    # the tracker alone (the leak gates have their own test)
    notes = pitch_ref.notes(pitch_ref.yin(pitch_ref.decode(path)))
    check("one note per sung note", len(notes) == len(MELODY), f"{len(notes)} notes: {notes}")
    for (start, dur, midi), (n_start, n_dur, n_pitch) in zip(MELODY, notes):
        cents = n_pitch - midi * 100
        check(f"midi {midi} pitch", abs(cents) <= 30, f"{cents:+d} cents")
        check(f"midi {midi} timing", abs(n_start - start * 1000) <= 30
              and abs(n_start + n_dur - (start + dur) * 1000) <= 40, f"{n_start}+{n_dur} ms")


def test_silence(tmp: Path) -> None:
    path = tmp / "quiet.wav"
    rng = np.random.default_rng(2)
    noise = np.concatenate([np.zeros(SR), 0.05 * rng.standard_normal(2 * SR)]).astype(np.float32)
    write_wav(path, noise)
    notes = pitch_ref.extract(path)["notes"]
    check("silence and noise give no notes", not notes, str(notes))


def test_leak(tmp: Path) -> None:
    melody = [(0.5, 1.0, 64), (2.0, 1.0, 67)]
    vocals = voice(melody, 6.0)
    leak = voice([(3.5, 1.0, 62)], 6.0) * 0.05  # band bleeding in, -26 dB
    bass = voice([(5.0, 0.8, 40)], 6.0)  # loud, but two octaves under the melody
    band = voice([(3.5, 1.0, 62), (5.0, 0.8, 40)], 6.0)
    write_wav(tmp / "v.wav", vocals + leak + bass)
    write_wav(tmp / "i.wav", band)
    notes = pitch_ref.extract(tmp / "v.wav", tmp / "i.wav")["notes"]
    check("leak and bass dropped", [round(n[2] / 100) for n in notes] == [64, 67], str(notes))
    unchecked = pitch_ref.extract(tmp / "v.wav")["notes"]
    check("without the instrumental only the bass goes", len(unchecked) == 3, str(unchecked))


def test_cli(tmp: Path) -> None:
    path = tmp / "voice.wav"
    out = tmp / "pitch.json"
    subprocess.run([sys.executable, "-m", "ktv.pitch_ref", "--vocals", str(path), "--out", str(out)],
                   check=True, cwd=Path(__file__).resolve().parent, capture_output=True)
    data = json.loads(out.read_text(encoding="utf-8"))
    check("cli writes pitch.json", data["version"] == 2 and data["notes"] == pitch_ref.extract(path)["notes"])
    voiced = sum(len(run[1]) for run in data["frames"])
    sung = sum(dur for _s, dur, _m in MELODY) * 100
    check("frames hold the contour", abs(voiced - sung) <= 0.1 * sung, f"{voiced} frames, {sung:.0f} sung")
    first, values = data["frames"][0]
    check("frames are midi cents", abs(values[len(values) // 2] - 4500) <= 30
          and abs(first * data["hop"] + data["first_ms"] - 500) <= 30, f"run at frame {first}")
    check("no .part left", not (tmp / "pitch.json.part").exists())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vocals", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_melody(tmp)
        test_silence(tmp)
        test_leak(tmp)
        test_cli(tmp)
    if args.vocals:
        started = time.monotonic()
        f0 = pitch_ref.yin(pitch_ref.decode(args.vocals))
        notes = pitch_ref.notes(f0)
        print(f"{args.vocals.name}: {len(notes)} notes, voiced {np.mean(f0 > 0):.0%}, "
              f"{len(f0) / 100:.0f}s audio in {time.monotonic() - started:.1f}s")
    print("all pitch tests passed")


if __name__ == "__main__":
    main()
