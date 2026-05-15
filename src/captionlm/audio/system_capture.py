"""macOS system audio capture using a Swift ScreenCaptureKit helper.

ScreenCaptureKit (macOS 13+) captures system audio output directly.
We use a small Swift CLI tool (capture_audio.swift) compiled on first run,
which outputs raw float32 PCM to stdout. Python reads from this pipe.

This approach avoids PyObjC/dispatch compatibility issues entirely.
Requires: Xcode Command Line Tools (for swiftc).
Requires: Screen Recording permission granted in System Settings.
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

from captionlm.audio.base import AudioCapture

logger = logging.getLogger(__name__)

# Path to the Swift source and compiled binary
_SWIFT_SRC = Path(__file__).parent / "capture_audio.swift"
_BINARY_DIR = Path.home() / ".cache" / "captionlm"
_BINARY_PATH = _BINARY_DIR / "capture_audio"

# First compilation on Apple Silicon builds the Swift module cache for
# ScreenCaptureKit / CoreMedia / AVFoundation — can take 2-3 minutes.
_COMPILE_TIMEOUT = 300  # seconds


def _ensure_binary() -> Path:
    """Compile the Swift audio capture helper if needed."""
    _BINARY_DIR.mkdir(parents=True, exist_ok=True)

    # Recompile if binary doesn't exist or source is newer
    if _BINARY_PATH.exists():
        src_mtime = _SWIFT_SRC.stat().st_mtime
        bin_mtime = _BINARY_PATH.stat().st_mtime
        if bin_mtime >= src_mtime:
            logger.debug("Audio capture binary up to date")
            return _BINARY_PATH

    logger.info(
        "Compiling audio capture helper (first run may take 2-3 minutes "
        "while Swift builds the module cache)..."
    )
    start_time = time.monotonic()

    try:
        result = subprocess.run(
            [
                "swiftc", "-O",
                "-o", str(_BINARY_PATH),
                str(_SWIFT_SRC),
                "-framework", "ScreenCaptureKit",
                "-framework", "CoreMedia",
                "-framework", "AVFoundation",
            ],
            capture_output=True,
            text=True,
            timeout=_COMPILE_TIMEOUT,
        )
        elapsed = time.monotonic() - start_time

        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to compile audio helper:\n{result.stderr}\n"
                "Make sure Xcode Command Line Tools are installed:\n"
                "  xcode-select --install"
            )

        # Make executable
        os.chmod(_BINARY_PATH, 0o755)
        logger.info(
            "Audio capture helper compiled in %.1fs: %s",
            elapsed, _BINARY_PATH,
        )
        return _BINARY_PATH

    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Swift compilation timed out after {_COMPILE_TIMEOUT}s. "
            "This usually happens on first run. Try again — the module "
            "cache should be partially built and it will be faster."
        )
    except FileNotFoundError:
        raise RuntimeError(
            "swiftc not found. Install Xcode Command Line Tools:\n"
            "  xcode-select --install"
        )


class MacOSAudioCapture(AudioCapture):
    """Capture system audio on macOS via ScreenCaptureKit.

    Uses a compiled Swift helper that captures all system audio output
    and streams raw float32 PCM data through a pipe.

    Falls back to sounddevice (microphone input) if the Swift helper
    cannot be compiled or ScreenCaptureKit is unavailable.
    """

    def __init__(self, sample_rate: int = 16000, channels: int = 1):
        super().__init__(sample_rate, channels)
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)
        self._capturing = False
        self._process: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._sd_stream = None
        self._use_swift = False
        self._compile_error: str | None = None
        self._ready_event = threading.Event()

    def start(self) -> None:
        """Start capturing system audio.

        Compilation + capture startup run in a background thread
        so the Qt event loop is never blocked.
        """
        if self._capturing:
            return

        self._capturing = True
        self._ready_event.clear()

        # Run the potentially slow startup in a background thread
        t = threading.Thread(
            target=self._background_start,
            daemon=True,
            name="audio-startup",
        )
        t.start()

    def _background_start(self) -> None:
        """Background thread: compile helper + start capture."""
        # Try Swift ScreenCaptureKit helper first
        try:
            self._start_swift_capture()
            self._use_swift = True
            self._ready_event.set()
            return
        except Exception as e:
            logger.warning(
                "ScreenCaptureKit unavailable (%s). "
                "Falling back to microphone input.", e
            )

        # Fallback: sounddevice (microphone)
        try:
            self._start_sounddevice()
        except Exception as e:
            logger.error("All audio capture methods failed: %s", e)
            self._capturing = False
        self._ready_event.set()

    def _start_swift_capture(self) -> None:
        """Start capture using the Swift ScreenCaptureKit helper."""
        binary = _ensure_binary()

        logger.info("Starting audio capture process...")

        # Start the capture process
        self._process = subprocess.Popen(
            [str(binary)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Wait for READY signal on stderr using a reader thread
        # (avoids select/fd issues that cause [Errno 9])
        ready_event = threading.Event()
        error_holder = [None]

        def _read_stderr():
            try:
                for raw_line in self._process.stderr:
                    line = raw_line.decode().strip()
                    if line:
                        logger.debug("Audio helper stderr: %s", line)
                    if line == "READY":
                        ready_event.set()
                        return
                    elif line.startswith("ERROR"):
                        error_holder[0] = line
                        ready_event.set()
                        return
            except Exception as e:
                error_holder[0] = str(e)
                ready_event.set()

        stderr_thread = threading.Thread(
            target=_read_stderr, daemon=True, name="stderr-reader"
        )
        stderr_thread.start()

        # Wait up to 10 seconds for READY
        ready_event.wait(timeout=10.0)

        if error_holder[0]:
            raise RuntimeError(f"Audio capture error: {error_holder[0]}")

        if not ready_event.is_set():
            if self._process.poll() is not None:
                raise RuntimeError("Audio capture process exited before READY")
            logger.warning("Audio capture did not signal READY, continuing anyway")

        # Start reader thread
        self._reader_thread = threading.Thread(
            target=self._read_audio_pipe,
            daemon=True,
            name="audio-reader",
        )
        self._reader_thread.start()

        logger.info(
            "System audio capture started (ScreenCaptureKit, rate=%d)",
            self.sample_rate,
        )

    def _read_audio_pipe(self) -> None:
        """Read raw float32 PCM from the Swift helper's stdout."""
        READ_SIZE = self.sample_rate * 4 // 2  # Read 0.5 seconds at a time

        while self._capturing and self._process and self._process.poll() is None:
            try:
                raw = self._process.stdout.read(READ_SIZE)
                if not raw:
                    break

                # Convert bytes to float32 numpy array
                n_samples = len(raw) // 4
                if n_samples == 0:
                    continue

                audio = np.frombuffer(raw[:n_samples * 4], dtype=np.float32).copy()

                try:
                    self._queue.put_nowait(audio)
                except queue.Full:
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        pass
                    self._queue.put_nowait(audio)

            except Exception as e:
                if self._capturing:
                    logger.error("Audio pipe read error: %s", e)
                break

        if self._capturing:
            logger.warning("Audio capture pipe closed unexpectedly")

    def _start_sounddevice(self) -> None:
        """Fallback: capture from microphone via sounddevice."""
        import sounddevice as sd

        def _audio_callback(indata, frames, time_info, status):
            if status:
                logger.warning("Audio capture status: %s", status)
            audio = indata[:, 0] if indata.ndim > 1 else indata.flatten()
            try:
                self._queue.put_nowait(audio.copy())
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
                self._queue.put_nowait(audio.copy())

        try:
            self._sd_stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="float32",
                blocksize=int(self.sample_rate * 0.5),
                callback=_audio_callback,
            )
            self._sd_stream.start()
            logger.info(
                "Audio capture started (sounddevice/mic fallback, rate=%d, device=%s)",
                self.sample_rate,
                sd.query_devices(sd.default.device[0], "input")["name"],
            )
        except Exception as e:
            logger.error("Failed to start audio capture: %s", e)
            raise RuntimeError(
                "Could not start audio capture. Check permissions in "
                "System Settings > Privacy & Security > Screen Recording."
            ) from e

    def stop(self) -> None:
        """Stop audio capture and fully clean up resources."""
        self._capturing = False

        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=1)
            except Exception:
                pass
            # Close all pipes to avoid bad file descriptors on restart
            try:
                if self._process.stdout:
                    self._process.stdout.close()
            except Exception:
                pass
            try:
                if self._process.stderr:
                    self._process.stderr.close()
            except Exception:
                pass
            self._process = None

        if self._reader_thread:
            self._reader_thread.join(timeout=2)
            self._reader_thread = None

        if self._sd_stream:
            try:
                self._sd_stream.stop()
                self._sd_stream.close()
            except Exception:
                pass
            self._sd_stream = None

        self._use_swift = False

        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

        logger.info("Audio capture stopped")

    def read_chunk(self, max_seconds: float = 3.0) -> np.ndarray | None:
        """Read accumulated audio, capped at max_seconds.

        If more audio has accumulated than max_seconds, the oldest
        audio is discarded to keep latency low.
        """
        chunks = []
        total_samples = 0
        max_samples = int(self.sample_rate * max_seconds)

        while not self._queue.empty():
            try:
                chunks.append(self._queue.get_nowait())
                total_samples += len(chunks[-1])
            except queue.Empty:
                break

        if not chunks:
            return None

        audio = np.concatenate(chunks)

        # If we have too much audio, keep only the most recent portion
        if len(audio) > max_samples:
            discarded_s = (len(audio) - max_samples) / self.sample_rate
            logger.debug(
                "Audio backlog: discarding %.1fs of old audio", discarded_s
            )
            audio = audio[-max_samples:]

        return audio

    @property
    def is_capturing(self) -> bool:
        return self._capturing
