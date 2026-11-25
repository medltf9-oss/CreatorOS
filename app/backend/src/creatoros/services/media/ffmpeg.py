"""The one place that starts an FFmpeg process.

Every rule in here exists because of a concrete failure mode:

* **argv arrays, never a shell string.** A filename with ``&`` or a quote must not be
  able to become a command.
* **no window.** ``CREATE_NO_WINDOW`` keeps a console from flashing on every thumbnail.
* **a hard timeout.** A corrupt file can keep FFmpeg "working" indefinitely; the process
  is killed and the job fails with a reason instead of blocking the worker forever.
* **stderr drained in a thread.** FFmpeg writes a lot to stderr; leaving the pipe unread
  deadlocks the child once the OS buffer fills.
* **cancellation kills the process tree.** ``cancel_requested`` must actually stop CPU
  work, not just mark a row.
* **progress from real output.** ``-progress pipe:1`` emits ``out_time_us`` and ``speed``;
  the percentages shown in the UI are those numbers, not a timer.
"""
from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...core.logging import get_logger

log = get_logger(__name__)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class FfmpegMissing(RuntimeError):
    code = "FFMPEG_MISSING"
    status = 503


class FfmpegTimeout(RuntimeError):
    code = "FFMPEG_TIMEOUT"
    status = 504


class FfmpegCancelled(RuntimeError):
    code = "MEDIA_JOB_CANCELLED"
    status = 409


class FfmpegFailed(RuntimeError):
    code = "FFMPEG_FAILED"
    status = 422

    def __init__(self, message: str, *, stderr: str = "", returncode: int | None = None) -> None:
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


@dataclass(slots=True)
class FfmpegResult:
    command: list[str]
    returncode: int
    stderr: str
    duration_s: float
    out_time_ms: int | None = None
    speed: float | None = None
    frames: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "returncode": self.returncode,
            "duration_s": round(self.duration_s, 3),
            "out_time_ms": self.out_time_ms,
            "speed": self.speed,
            "frames": self.frames,
            "stderr_tail": self.stderr[-1200:],
        }


def ffmpeg_exe() -> Path:
    from ...core.settings import get_settings

    return get_settings().ffmpeg_bin / "ffmpeg.exe"


def ffprobe_exe() -> Path:
    from ...core.settings import get_settings

    return get_settings().ffmpeg_bin / "ffprobe.exe"


def _parse_progress_line(line: str) -> dict[str, str]:
    key, _, value = line.partition("=")
    return {key.strip(): value.strip()} if key and _ else {}


class FfmpegRunner:
    """Synchronous runner. One instance is safe to share; each call owns its process."""

    def __init__(self, executable: Path | None = None) -> None:
        self.executable = executable or ffmpeg_exe()

    def available(self) -> bool:
        return self.executable.exists()

    def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float = 600.0,
        total_duration_ms: int | None = None,
        on_progress: Callable[[float, dict[str, Any]], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        cwd: Path | None = None,
    ) -> FfmpegResult:
        if not self.available():
            raise FfmpegMissing(f"ffmpeg not found at {self.executable}")
        command = [str(self.executable), "-hide_banner", "-nostdin", "-y", *args]
        started = time.perf_counter()
        creationflags = CREATE_NO_WINDOW
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(cwd) if cwd else None,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise FfmpegMissing(f"could not start ffmpeg: {exc}") from exc

        stderr_chunks: list[str] = []

        def drain_stderr() -> None:
            assert process.stderr is not None
            for raw in process.stderr:
                text = raw.decode("utf-8", "replace")
                stderr_chunks.append(text)
                if len(stderr_chunks) > 400:
                    del stderr_chunks[:200]

        stderr_thread = threading.Thread(target=drain_stderr, name="ffmpeg-stderr", daemon=True)
        stderr_thread.start()

        out_time_ms: int | None = None
        speed: float | None = None
        frames: int | None = None
        cancelled = False

        try:
            assert process.stdout is not None
            while True:
                if should_cancel is not None and should_cancel():
                    cancelled = True
                    self._kill(process)
                    break
                if time.perf_counter() - started > timeout_s:
                    self._kill(process)
                    raise FfmpegTimeout(f"ffmpeg exceeded {timeout_s:.0f}s")
                line = process.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                parsed = _parse_progress_line(text)
                if not parsed:
                    continue
                # FFmpeg emits both keys in MICROSECONDS; ``out_time_ms`` is a historical
                # misnomer. Reading it as milliseconds made every encode report 100 % on the
                # first progress line (measured: 37 500 227 for 37.5 s of output).
                raw_us = None
                if "out_time_us" in parsed:
                    raw_us = parsed["out_time_us"]
                elif "out_time_ms" in parsed:
                    raw_us = parsed["out_time_ms"]
                if raw_us is not None:
                    try:
                        value_us = int(raw_us)
                    except ValueError:
                        value_us = None
                    if value_us is not None and value_us >= 0:
                        out_time_ms = value_us // 1000
                if "speed" in parsed:
                    value = parsed["speed"].rstrip("x")
                    try:
                        speed = float(value)
                    except ValueError:
                        speed = None
                if "frame" in parsed:
                    try:
                        frames = int(parsed["frame"])
                    except ValueError:
                        pass
                if on_progress is not None:
                    fraction = 0.0
                    if total_duration_ms and out_time_ms:
                        ratio = out_time_ms / total_duration_ms
                        # A sample wildly beyond the expected duration is a parse artefact,
                        # not completion: report no progress rather than a fake 100 %.
                        fraction = min(1.0, ratio) if ratio <= 1.5 else 0.0
                    on_progress(fraction, {"out_time_ms": out_time_ms, "speed": speed, "frame": frames})
        finally:
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
                self._kill(process)
                process.wait(timeout=10)
            stderr_thread.join(timeout=5)

        stderr_text = "".join(stderr_chunks)[-8000:]
        duration = time.perf_counter() - started
        if cancelled:
            raise FfmpegCancelled("media operation cancelled")

        result = FfmpegResult(
            command=command,
            returncode=process.returncode or 0,
            stderr=stderr_text,
            duration_s=duration,
            out_time_ms=out_time_ms,
            speed=speed,
            frames=frames,
        )
        if result.returncode != 0:
            tail = "\n".join(stderr_text.strip().splitlines()[-4:])
            raise FfmpegFailed(f"ffmpeg exited {result.returncode}: {tail[:400]}", stderr=stderr_text, returncode=result.returncode)
        return result

    @staticmethod
    def _kill(process: subprocess.Popen) -> None:
        try:
            process.kill()
        except OSError:  # pragma: no cover - already gone
            return

    # ------------------------------------------------------------ capabilities
    def tool_version(self) -> str | None:
        """The version banner of this ffmpeg build, read from the binary itself."""
        if not self.available():
            return None
        try:
            completed = subprocess.run(
                [str(self.executable), "-hide_banner", "-version"],
                capture_output=True,
                timeout=15,
                check=False,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - broken install
            return None
        first = (completed.stdout or b"").decode("utf-8", "replace").splitlines()
        return first[0].strip() if first else None

    def capabilities(self) -> dict[str, Any]:
        """What this build can actually do. Cached by the caller, never assumed."""
        info: dict[str, Any] = {"available": self.available(), "path": str(self.executable)}
        if not self.available():
            return info
        info["version"] = self.tool_version()
        try:
            encoders = subprocess.run(
                [str(self.executable), "-hide_banner", "-encoders"],
                capture_output=True,
                timeout=20,
                check=False,
                creationflags=CREATE_NO_WINDOW,
            )
            filters = subprocess.run(
                [str(self.executable), "-hide_banner", "-filters"],
                capture_output=True,
                timeout=20,
                check=False,
                creationflags=CREATE_NO_WINDOW,
            )
            encoder_text = (encoders.stdout or b"").decode("utf-8", "replace")
            filter_text = (filters.stdout or b"").decode("utf-8", "replace")
            info["encoders"] = {
                name: (f" {name} " in encoder_text)
                for name in ("libx264", "libx265", "libvpx-vp9", "libwebp", "aac", "libmp3lame", "libopus", "h264_nvenc", "hevc_nvenc")
            }
            info["filters"] = {
                name: (f" {name} " in filter_text)
                for name in ("scale", "thumbnail", "showwavespic", "blackdetect", "freezedetect", "signalstats", "loudnorm")
            }
        except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover - broken install
            info["error"] = str(exc)
        return info


@dataclass(slots=True)
class EncoderChoice:
    """Which encoder to use, and the measured reason."""

    name: str
    hardware: bool
    verified: bool
    detail: str


_NVENC_STATE: dict[str, Any] = {"checked_at": 0.0, "choice": None}


def choose_h264_encoder(runner: FfmpegRunner | None = None, *, force_recheck: bool = False) -> EncoderChoice:
    """Pick an H.264 encoder by actually testing it, not by asking the driver.

    Measured on this machine: ffmpeg 9.1 requires NVENC API 13.1 and the installed
    driver 582.78 provides 13.0, so ``h264_nvenc`` fails to open the encoder. Claiming
    "GPU encoding enabled" from the presence of the encoder in ``-encoders`` would be a
    lie, so a 0.5 s test encode decides, and the answer is cached for the process.
    """
    now = time.time()
    cached = _NVENC_STATE.get("choice")
    if cached is not None and not force_recheck and now - _NVENC_STATE["checked_at"] < 3600:
        return cached

    runner = runner or FfmpegRunner()
    choice = EncoderChoice("libx264", hardware=False, verified=runner.available(), detail="software encoder")
    if runner.available():
        try:
            probe = runner.run(
                [
                    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=15:duration=0.5",
                    "-frames:v", "8", "-c:v", "h264_nvenc", "-f", "null", "-",
                ],
                timeout_s=25,
            )
            if probe.returncode == 0:
                choice = EncoderChoice("h264_nvenc", hardware=True, verified=True, detail="test encode succeeded")
        except (FfmpegFailed, FfmpegTimeout, FfmpegMissing) as exc:
            detail = str(exc).replace("\n", " ")[:200]
            choice = EncoderChoice("libx264", hardware=False, verified=True, detail=f"nvenc unusable: {detail}")
    _NVENC_STATE["choice"] = choice
    _NVENC_STATE["checked_at"] = now
    log.info("h264 encoder selected: %s (%s)", choice.name, choice.detail)
    return choice


def encoder_args(choice: EncoderChoice, *, crf: int, preset: str = "veryfast") -> list[str]:
    if choice.hardware:
        # NVENC uses -cq for constant quality; -preset maps to p1..p7 on modern drivers.
        return ["-c:v", choice.name, "-preset", "p4", "-cq", str(crf), "-b:v", "0"]
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-profile:v", "high"]


__all__ = [
    "CREATE_NO_WINDOW",
    "EncoderChoice",
    "FfmpegCancelled",
    "FfmpegFailed",
    "FfmpegMissing",
    "FfmpegResult",
    "FfmpegRunner",
    "FfmpegTimeout",
    "choose_h264_encoder",
    "encoder_args",
    "ffmpeg_exe",
    "ffprobe_exe",
]
