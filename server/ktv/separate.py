"""AI vocal separation worker (UVR MDX-Net ONNX), run as its own process.

    python -m ktv.separate --model M --source S --vocals V --instrumental I

Built for a 1 GB VPS: audio is decoded, separated and encoded one chunk at a
time (ffmpeg pipes in and out), so memory stays flat at ~300 MB whatever the
song length.  The model must have a variable time axis (`fetch_model.py`
patches the stock UVR export, whose fixed 256 frames peak near 1 GB).

The process lowers its own priority (nice 19, oom_score_adj 1000) so the
kernel kills it - never the KTV server - when memory runs out.  Outputs are
written to `*.part` files and renamed only on success.  Progress goes to
stdout as `progress <0..1>` lines.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

SAMPLE_RATE = 44100  # MDX-Net models are trained at 44.1 kHz stereo
# Stems are AAC in .m4a: every browser (iOS Safari too) plays it, and the
# moov atom goes first (faststart) so <audio> can start and seek while streaming.
STEM_CODEC = ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-f", "mp4"]
HOP = 1024

# name -> (n_fft, primary stem, compensate) for the UVR MDX-Net exports
MODELS = {
    "UVR_MDXNET_KARA_2": (5120, "instrumental", 1.065),
    "UVR-MDX-NET-Inst_HQ_3": (6144, "instrumental", 1.022),
    "Kim_Vocal_2": (7680, "vocals", 1.009),
}


def model_params(model: Path) -> tuple[int, str, float]:
    for name, params in MODELS.items():
        if model.stem.startswith(name):
            return params
    raise SystemExit(f"unknown model {model.name}; expected one of {', '.join(MODELS)}")


def lower_priority() -> None:
    try:
        os.nice(19)
    except (AttributeError, OSError):
        pass
    try:  # let the OOM killer pick this process first
        Path("/proc/self/oom_score_adj").write_text("1000")
    except OSError:
        pass


def _hann(n: int) -> np.ndarray:
    return (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n) / n)).astype(np.float32)


def _stft(x: np.ndarray, n_fft: int, window: np.ndarray) -> np.ndarray:
    """(C, T) -> (C, frames, F); same framing as torch.stft(center=True)."""
    x = np.pad(x, ((0, 0), (n_fft // 2, n_fft // 2)), mode="reflect")
    frames = np.lib.stride_tricks.sliding_window_view(x, n_fft, axis=1)[:, ::HOP]
    return np.fft.rfft(frames * window, axis=-1).astype(np.complex64)


def _istft(spec: np.ndarray, n_fft: int, window: np.ndarray, length: int) -> np.ndarray:
    frames = np.fft.irfft(spec, n=n_fft, axis=-1).astype(np.float32) * window
    channels, count, _ = frames.shape
    total = n_fft + HOP * (count - 1)
    out = np.zeros((channels, total), np.float32)
    norm = np.zeros(total, np.float32)
    square = window * window
    for i in range(count):
        out[:, i * HOP : i * HOP + n_fft] += frames[:, i]
        norm[i * HOP : i * HOP + n_fft] += square
    out /= np.maximum(norm, 1e-8)
    pad = n_fft // 2
    return out[:, pad : pad + length]


class Separator:
    def __init__(self, model: Path, frames: int = 64, threads: int = 1) -> None:
        import onnxruntime as ort

        self.n_fft, self.primary, self.compensate = model_params(model)
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        # arenas keep the activation peak resident; without them it is freed
        options.enable_cpu_mem_arena = False
        options.enable_mem_pattern = False
        self.session = ort.InferenceSession(
            str(model), options, providers=["CPUExecutionProvider"]
        )
        shape = self.session.get_inputs()[0].shape
        self.dim_f = shape[2]
        if isinstance(shape[3], int):
            frames = shape[3]  # stock export: fixed time axis
        self.frames = frames
        self.window = _hann(self.n_fft)
        self.chunk = HOP * (frames - 1)
        self.trim = self.n_fft // 2
        self.step = self.chunk - 2 * self.trim
        if self.step <= 0:
            raise SystemExit(f"--frames {frames} is too small for n_fft {self.n_fft}")

    def primary_stem(self, segment: np.ndarray) -> np.ndarray:
        """segment: (2, chunk) -> primary stem for the same span."""
        spec = _stft(segment, self.n_fft, self.window)[:, :, : self.dim_f]
        x = np.stack([spec.real, spec.imag], 1).transpose(0, 1, 3, 2)
        x = np.ascontiguousarray(x.reshape(1, 4, self.dim_f, self.frames), np.float32)
        y = self.session.run(None, {"input": x})[0][0].reshape(2, 2, self.dim_f, self.frames)
        full = np.zeros((2, self.frames, self.n_fft // 2 + 1), np.complex64)
        full[:, :, : self.dim_f] = (y[:, 0] + 1j * y[:, 1]).transpose(0, 2, 1)
        return _istft(full, self.n_fft, self.window, self.chunk)

    def run(self, read, write_vocals, write_instrumental, total: int | None, progress) -> None:
        """Stream (2, n) float32 blocks from `read(n)` to the two writers.

        Each step separates `trim` samples of context on both sides plus
        `step` new samples, and keeps only the middle.
        """
        trim, step, chunk = self.trim, self.step, self.chunk
        buf = np.zeros((2, trim), np.float32)  # left context: silence
        ahead = read(step + trim)
        buf = np.concatenate([buf, ahead], 1)
        done = 0
        while buf.shape[1] > trim:
            new = min(step, buf.shape[1] - trim)
            segment = buf[:, :chunk]
            if segment.shape[1] < chunk:
                segment = np.pad(segment, ((0, 0), (0, chunk - segment.shape[1])))
            primary = self.primary_stem(segment)[:, trim : trim + new]
            mix = segment[:, trim : trim + new]
            other = mix - primary
            primary = primary * self.compensate  # UVR's per-model level fix
            vocals, instrumental = (primary, other) if self.primary == "vocals" else (other, primary)
            write_vocals(vocals)
            write_instrumental(instrumental)
            done += new
            if total:
                progress(min(1.0, done / total))
            buf = np.concatenate([buf[:, step:], read(step)], 1)


def _decoder(source: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [FFMPEG, "-v", "error", "-i", str(source), "-f", "f32le", "-ac", "2",
         "-ar", str(SAMPLE_RATE), "-"],
        stdout=subprocess.PIPE,
    )


def _encoder(target: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [FFMPEG, "-v", "error", "-y", "-f", "s16le", "-ac", "2", "-ar", str(SAMPLE_RATE),
         "-i", "-", *STEM_CODEC, str(target)],
        stdin=subprocess.PIPE,
    )


def _duration(source: Path) -> int | None:
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
             str(source)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        return int(float(out) * SAMPLE_RATE)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


FFMPEG = os.environ.get("KTV_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("KTV_FFPROBE", "ffprobe")


def separate(model: Path, source: Path, vocals: Path, instrumental: Path,
             frames: int = 64, threads: int = 1) -> None:
    separator = Separator(model, frames, threads)
    total = _duration(source)
    parts = [vocals.with_name(vocals.name + ".part"), instrumental.with_name(instrumental.name + ".part")]
    decoder = _decoder(source)
    encoders = [_encoder(p) for p in parts]

    def read(samples: int) -> np.ndarray:
        raw = decoder.stdout.read(samples * 8)  # 2 channels x float32
        usable = len(raw) - len(raw) % 8
        return np.frombuffer(raw[:usable], np.float32).reshape(-1, 2).T

    def writer(proc: subprocess.Popen):
        def write(block: np.ndarray) -> None:
            pcm = (np.clip(block, -1.0, 1.0) * 32767).astype("<i2").T
            proc.stdin.write(pcm.tobytes())
        return write

    last = [-1]

    def progress(value: float) -> None:
        pct = int(value * 100)
        if pct != last[0]:
            last[0] = pct
            print(f"progress {value:.3f}", flush=True)

    try:
        separator.run(read, writer(encoders[0]), writer(encoders[1]), total, progress)
        for proc in encoders:
            proc.stdin.close()
        failed = [p for p in [decoder, *encoders] if p.wait() != 0]
        if failed:
            raise SystemExit("ffmpeg failed")
        for part, final in zip(parts, (vocals, instrumental)):
            os.replace(part, final)
    finally:
        for proc in [decoder, *encoders]:
            if proc.poll() is None:
                proc.kill()
        for part in parts:
            part.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--vocals", type=Path, required=True)
    parser.add_argument("--instrumental", type=Path, required=True)
    parser.add_argument("--frames", type=int, default=64, help="STFT frames per model call (memory)")
    parser.add_argument("--threads", type=int, default=1)
    args = parser.parse_args(argv)
    lower_priority()
    separate(args.model, args.source, args.vocals, args.instrumental, args.frames, args.threads)
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
