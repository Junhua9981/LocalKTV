"""Reference melody for singing scores, taken from the AI vocals stem.

    python -m ktv.pitch_ref --vocals V [--instrumental I] --out O

The vocals stem (UVR KARA_2) is a mostly clean lead vocal, so a plain YIN
pitch tracker gets the sung melody out of it.  What it is not: silent where
nobody sings.  Some of the backing band leaks into it (about -25 dB), so two
gates drop frames that are leak, not voice:

  * the vocals are more than LEAK_DB quieter than the instrumental stem at
    that moment (needs --instrumental);
  * the pitch is over an octave below the song's typical sung pitch and
    under G#2 (bass).

pitch.json (version 2):

    {"version": 2,
     "hop": 10, "first_ms": 16,
     "frames": [[first_frame, [midi_x100, ...]], ...],
     "notes": [[start_ms, dur_ms, midi_x100], ...]}

`frames` is the raw contour, one value per 10 ms, as runs of voiced frames
(frame i is heard at first_ms + i * hop).  The display cuts it into one note
per lyric word itself (web/src/lib/scoring.ts), so a lyric offset or a new
lyrics file never needs a new pitch.json.  `notes` is the contour cut by
pitch alone, for songs without word-timed lyrics.  midi_x100 is the MIDI note
number in cents (6900 = A4 440 Hz); the display adds the room's key shift.

Runs as its own low-priority process like ktv.separate (numpy only comes with
onnxruntime, and the server never imports it): ffmpeg decodes to 16 kHz mono,
frames are analysed a block at a time, so memory stays at a few MB.  A 4
minute song takes a few seconds.  Output is written to `O.part` and renamed
on success.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
HOP = 160  # 10 ms per frame
WINDOW = 512  # YIN integration window (32 ms)
F_MIN, F_MAX = 70.0, 1000.0  # lowest bass to a soprano's C6
TAU_MIN = int(SAMPLE_RATE / F_MAX)
TAU_MAX = int(SAMPLE_RATE / F_MIN) + 1
FRAME = WINDOW + TAU_MAX  # samples each frame needs
YIN_THRESHOLD = 0.15  # first dip of the normalised difference below this
APERIODIC = 0.3  # best dip above this: breath, consonant or silence
SILENCE_DB = -40.0  # frames this far below the song's loudest are unvoiced
LEAK_DB = -15.0  # vocals this far under the instrumental stem: band leaking in
BASS_CENTS = 1300  # this far below the median sung pitch: a bass line leaking in ...
BASS_FLOOR = 4400  # ... but never above E2..G#2 territory: a duet's male part can sit an
#                    octave under the song's median and still be singing (A2 = 4500)
VERSION = 2
BLOCK = 1024  # frames per numpy batch

# note segmentation
MEDIAN = 5  # frames of median smoothing before cutting notes
SPLIT_CENTS = 60  # a frame this far from the running note starts a new one
GAP_FILL = 3  # unvoiced runs this short (frames) inside a note are bridged
MIN_NOTE = 8  # frames (80 ms): shorter notes are slides, dropped
CENTER_MS = WINDOW * 1000 // SAMPLE_RATE // 2  # frame i is heard at i*10 ms + this


def ffmpeg_bin() -> str:
    return os.environ.get("KTV_FFMPEG", "ffmpeg")


def decode(path: Path) -> np.ndarray:
    """Whole file as 16 kHz mono float32 (4 minutes = 15 MB)."""
    out = subprocess.run(
        [ffmpeg_bin(), "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SAMPLE_RATE),
         "-f", "f32le", "-"],
        check=True, stdout=subprocess.PIPE,
    ).stdout
    return np.frombuffer(out, dtype=np.float32)


def yin(samples: np.ndarray) -> np.ndarray:
    """Pitch in Hz per 10 ms frame, 0 where unvoiced."""
    padded = np.concatenate([samples, np.zeros(FRAME, dtype=np.float32)])
    n_frames = max(0, (len(samples) - 1) // HOP + 1)
    f0 = np.zeros(n_frames, dtype=np.float32)
    power = np.zeros(n_frames, dtype=np.float32)
    nfft = 1 << int(np.ceil(np.log2(FRAME + WINDOW)))
    taus = np.arange(TAU_MAX + 1)
    for first in range(0, n_frames, BLOCK):
        count = min(BLOCK, n_frames - first)
        idx = (first + np.arange(count))[:, None] * HOP + np.arange(FRAME)[None, :]
        frames = padded[idx].astype(np.float64)
        head = frames[:, :WINDOW]
        # r(tau) = sum_j x[j] x[j + tau], for every tau at once
        spec = np.fft.rfft(frames, nfft) * np.conj(np.fft.rfft(head, nfft))
        r = np.fft.irfft(spec, nfft)[:, : TAU_MAX + 1]
        sq = np.concatenate([np.zeros((count, 1)), np.cumsum(frames**2, axis=1)], axis=1)
        energy = sq[:, taus + WINDOW] - sq[:, taus]  # sum of x[j + tau]^2 over the window
        diff = np.maximum(energy[:, :1] + energy - 2 * r, 0.0)
        # cumulative mean normalised difference
        cum = np.cumsum(diff[:, 1:], axis=1)
        cmnd = np.ones_like(diff)
        cmnd[:, 1:] = diff[:, 1:] * taus[1:] / np.maximum(cum, 1e-12)
        power[first : first + count] = energy[:, 0] / WINDOW
        for i in range(count):
            row = cmnd[i]
            below = np.nonzero(row[TAU_MIN:TAU_MAX] < YIN_THRESHOLD)[0]
            if len(below):
                tau = TAU_MIN + below[0]
                while tau + 1 < TAU_MAX and row[tau + 1] < row[tau]:
                    tau += 1
            else:
                tau = TAU_MIN + int(np.argmin(row[TAU_MIN:TAU_MAX]))
                if row[tau] > APERIODIC:
                    continue
            # parabolic interpolation around the dip
            a, b, c = row[tau - 1], row[tau], row[min(tau + 1, TAU_MAX)]
            denom = a - 2 * b + c
            shift = 0.5 * (a - c) / denom if denom > 1e-12 else 0.0
            f0[first + i] = SAMPLE_RATE / (tau + shift)
    if n_frames and power.max() > 0:
        db = 10 * np.log10(np.maximum(power, 1e-12) / power.max())
        f0[db < SILENCE_DB] = 0.0
    return f0


def frame_power(samples: np.ndarray, n_frames: int) -> np.ndarray:
    """Mean square over each frame's YIN window (frame i starts at i * HOP)."""
    padded = np.concatenate([samples, np.zeros(n_frames * HOP + WINDOW, dtype=np.float32)])
    sq = np.concatenate([[0.0], np.cumsum(padded.astype(np.float64) ** 2)])
    starts = np.arange(n_frames) * HOP
    return (sq[starts + WINDOW] - sq[starts]) / WINDOW


def drop_leak(f0: np.ndarray, vocals: np.ndarray, instrumental: np.ndarray | None) -> np.ndarray:
    """Unvoice frames that are the band leaking into the vocals stem."""
    f0 = f0.copy()
    if instrumental is not None and len(f0):
        voc = frame_power(vocals, len(f0))
        ins = frame_power(instrumental, len(f0))
        ratio = 10 * np.log10(np.maximum(voc, 1e-12) / np.maximum(ins, 1e-12))
        f0[ratio < LEAK_DB] = 0.0
    voiced = f0 > 0
    if voiced.sum() >= 50:
        cents = 6900 + 1200 * np.log2(f0[voiced] / 440.0)
        floor = min(np.median(cents) - BASS_CENTS, BASS_FLOOR)
        low = np.zeros(len(f0), dtype=bool)
        low[voiced] = cents < floor
        f0[low] = 0.0
    return f0


def runs(f0: np.ndarray) -> list[list]:
    """Voiced stretches as [first_frame, [midi_x100, ...]] (pitch.json frames)."""
    cents = to_cents(f0)
    out: list[list] = []
    start = -1
    for i, value in enumerate(np.append(cents, np.nan)):
        if np.isnan(value):
            if start >= 0:
                out.append([start, [int(round(v)) for v in cents[start:i]]])
                start = -1
        elif start < 0:
            start = i
    return out


def to_cents(f0: np.ndarray) -> np.ndarray:
    """MIDI cents per frame, NaN where unvoiced."""
    cents = np.full(len(f0), np.nan)
    voiced = f0 > 0
    cents[voiced] = 6900 + 1200 * np.log2(f0[voiced] / 440.0)
    return cents


def _median(cents: np.ndarray) -> np.ndarray:
    """Median over voiced neighbours only, so note edges stay sharp."""
    out = cents.copy()
    half = MEDIAN // 2
    for i in np.nonzero(~np.isnan(cents))[0]:
        window = cents[max(0, i - half) : i + half + 1]
        out[i] = np.nanmedian(window)
    return out


def notes(f0: np.ndarray) -> list[list[int]]:
    """Cut the contour into [start_ms, dur_ms, midi_x100] notes."""
    cents = _median(to_cents(f0))
    result: list[list[int]] = []
    start, run, gap = -1, [], 0

    def close(end: int) -> None:
        if start >= 0 and end - start >= MIN_NOTE and run:
            pitch = int(round(float(np.median(run))))
            result.append([start * 10 + CENTER_MS, (end - start) * 10, pitch])

    for i, value in enumerate(cents):
        if np.isnan(value):
            if start >= 0:
                gap += 1
                if gap > GAP_FILL:
                    close(i - gap + 1)
                    start, run, gap = -1, [], 0
            continue
        if start >= 0 and abs(value - np.median(run[-MIN_NOTE:])) > SPLIT_CENTS:
            close(i - gap)
            start, run = -1, []
        if start < 0:
            start = i
        gap = 0
        run.append(value)
    if start >= 0:
        close(len(cents) - gap)
    return result


def extract(vocals: Path, instrumental: Path | None = None) -> dict:
    samples = decode(vocals)
    backing = None
    if instrumental is not None:
        try:
            backing = decode(instrumental)
        except subprocess.CalledProcessError:
            print("instrumental stem unreadable: no leak gate", file=sys.stderr, flush=True)
    f0 = drop_leak(yin(samples), samples, backing)
    return {"version": VERSION, "hop": HOP * 1000 // SAMPLE_RATE, "first_ms": CENTER_MS,
            "frames": runs(f0), "notes": notes(f0)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--vocals", type=Path, required=True)
    parser.add_argument("--instrumental", type=Path, help="the other stem: gates band leak")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if hasattr(os, "nice"):
        os.nice(19)
    if sys.platform.startswith("linux"):
        try:
            Path("/proc/self/oom_score_adj").write_text("1000")
        except OSError:
            pass
    data = extract(args.vocals, args.instrumental)
    part = args.out.with_name(args.out.name + ".part")
    part.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    part.replace(args.out)
    print(f"notes {len(data['notes'])}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
