"""AI vocal separation.

AI is a *quality enhancement*, never a playback requirement: when the model is
missing or the job fails the queue keeps playing in INSTANT mode.

`OnnxSeparator` runs UVR MDX-Net (KARA_2 by default) in a child process
(`ktv.separate`): one job at a time, lowest CPU/IO priority, first in line for
the OOM killer, and it can be paused (SIGSTOP) while someone is singing.
Without onnxruntime or the model file the server falls back to the stub.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from . import config
from .models import Track

log = logging.getLogger(__name__)

ProgressCallback = Callable[[str, float | None], Awaitable[None]]


class SeparationError(RuntimeError):
    """Raised when stems cannot be produced.  Never fatal for the queue."""


class SeparationCancelled(SeparationError):
    """The job was stopped on purpose (pre-empted or shutdown); not a failure."""


class Separator(Protocol):
    name: str

    @property
    def available(self) -> bool: ...

    async def separate(
        self, track: Track, source: Path, on_progress: ProgressCallback
    ) -> tuple[Path, Path]:
        """Return (vocals, instrumental) paths inside the track cache."""
        ...

    def pause(self) -> None: ...

    def resume(self) -> None: ...

    def cancel(self) -> None: ...


class StubSeparator:
    """Used when onnxruntime or the model file is missing."""

    name = "stub"

    @property
    def available(self) -> bool:
        return False

    async def separate(
        self, track: Track, source: Path, on_progress: ProgressCallback
    ) -> tuple[Path, Path]:
        await asyncio.sleep(0)
        raise SeparationError("AI separator not installed (no onnxruntime/model) — falling back to INSTANT")

    def pause(self) -> None:
        pass

    def resume(self) -> None:
        pass

    def cancel(self) -> None:
        pass


def model_path() -> Path | None:
    """The configured model, or the first `*.dyn.onnx` in models/."""
    if config.AI_MODEL:
        path = Path(config.AI_MODEL)
        if not path.is_absolute():
            path = config.MODELS_DIR / path
        return path if path.exists() else None
    found = sorted(config.MODELS_DIR.glob("*.dyn.onnx"))
    return found[0] if found else None


class OnnxSeparator:
    """UVR MDX-Net ONNX inference in a low-priority child process."""

    def __init__(self, model: Path | None = None) -> None:
        self.model = model or model_path()
        self.name = self.model.name.split(".")[0] if self.model else "mdx-net"
        self._proc: asyncio.subprocess.Process | None = None
        self._paused = False
        self._cancelled = False

    @property
    def available(self) -> bool:
        return (
            self.model is not None
            and self.model.exists()
            and importlib.util.find_spec("onnxruntime") is not None
            and shutil.which(config.FFMPEG_BIN) is not None
        )

    def _command(self, source: Path, vocals: Path, instrumental: Path) -> list[str]:
        cmd = [
            sys.executable, "-m", "ktv.separate",
            "--model", str(self.model), "--source", str(source),
            "--vocals", str(vocals), "--instrumental", str(instrumental),
            "--frames", str(config.AI_FRAMES), "--threads", str(config.AI_THREADS),
        ]
        ionice = shutil.which("ionice")
        if ionice and sys.platform.startswith("linux"):
            cmd = [ionice, "-c", "3", *cmd]  # idle IO class
        return cmd

    async def separate(
        self, track: Track, source: Path, on_progress: ProgressCallback
    ) -> tuple[Path, Path]:
        if not self.available:
            raise SeparationError("AI model or onnxruntime missing")
        directory = source.parent
        vocals = directory / config.VOCALS_FILE
        instrumental = directory / config.INSTRUMENTAL_FILE
        self._cancelled = False
        self._paused = False
        started = time.monotonic()
        env = {**os.environ, "PYTHONPATH": str(config.SERVER_DIR), "OMP_NUM_THREADS": str(config.AI_THREADS),
               "KTV_FFMPEG": config.FFMPEG_BIN, "KTV_FFPROBE": config.FFPROBE_BIN}
        self._proc = await asyncio.create_subprocess_exec(
            *self._command(source, vocals, instrumental),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(config.SERVER_DIR),
            start_new_session=True,  # own process group: pause/kill ffmpeg too
        )
        proc = self._proc
        stderr_task = asyncio.ensure_future(proc.stderr.read())
        try:
            await on_progress("AI separating", 0.0)
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if line.startswith("progress "):
                    try:
                        await on_progress("AI separating", float(line.split()[1]))
                    except ValueError:
                        pass
            code = await proc.wait()
            err = (await stderr_task).decode(errors="replace").strip()
        except asyncio.CancelledError:
            self._kill()
            raise
        finally:
            self._proc = None
            if not stderr_task.done():
                stderr_task.cancel()
            # a killed child cannot clean up after itself
            for stem in (vocals, instrumental):
                stem.with_name(stem.name + ".part").unlink(missing_ok=True)
        if self._cancelled:
            raise SeparationCancelled("separation stopped")
        if code != 0:
            reason = "killed (out of memory?)" if code in (-9, 137) else f"exit {code}"
            log.warning("separator %s for %s: %s", reason, source.parent.name, err[-500:])
            raise SeparationError(f"separator {reason}")
        if not vocals.exists() or not instrumental.exists():
            raise SeparationError("separation produced no stems")
        log.info("separated %s in %.0fs", source.parent.name, time.monotonic() - started)
        return vocals, instrumental

    # -- job control (process group, so ffmpeg children follow) ----------
    def _signal(self, sig: int) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, AttributeError):
            pass

    def pause(self) -> None:
        if not self._paused and hasattr(signal, "SIGSTOP"):
            self._signal(signal.SIGSTOP)
            self._paused = True

    def resume(self) -> None:
        if self._paused and hasattr(signal, "SIGCONT"):
            self._signal(signal.SIGCONT)
            self._paused = False

    def _kill(self) -> None:
        if hasattr(signal, "SIGKILL"):
            self._signal(signal.SIGKILL)
        elif self._proc is not None and self._proc.returncode is None:
            self._proc.kill()

    def cancel(self) -> None:
        """Stop the running job; `separate` raises SeparationCancelled."""
        if self._proc is None:
            return
        self._cancelled = True
        self._kill()
        self._paused = False


def get_separator() -> Separator:
    if not config.AI_ENABLED:
        return StubSeparator()
    onnx = OnnxSeparator()
    return onnx if onnx.available else StubSeparator()


def separator_available() -> bool:
    return get_separator().available


# -- reference melody for singing scores (ktv.pitch_ref) -----------------
def pitch_available() -> bool:
    return importlib.util.find_spec("numpy") is not None and shutil.which(config.FFMPEG_BIN) is not None


async def make_pitch(vocals: Path, instrumental: Path | None = None) -> Path:
    """Write pitch.json next to the vocals stem (a few seconds per song).

    The instrumental stem lets ktv.pitch_ref drop the band leaking into the vocals.
    """
    out = vocals.parent / config.PITCH_FILE
    env = {**os.environ, "PYTHONPATH": str(config.SERVER_DIR), "KTV_FFMPEG": config.FFMPEG_BIN}
    extra = ["--instrumental", str(instrumental)] if instrumental is not None else []
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "ktv.pitch_ref", "--vocals", str(vocals), *extra, "--out", str(out),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=env, cwd=str(config.SERVER_DIR),
    )
    try:
        stdout, stderr = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        raise
    finally:
        out.with_name(out.name + ".part").unlink(missing_ok=True)
    if proc.returncode != 0 or not out.exists():
        raise SeparationError(f"pitch exit {proc.returncode}: {stderr.decode(errors='replace')[-300:]}")
    log.info("pitch %s: %s", vocals.parent.name, stdout.decode(errors="replace").strip())
    return out
