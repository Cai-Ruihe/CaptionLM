"""Apple SpeechAnalyzer STT engine (macOS 26 Tahoe+).

This is a SELF-CONTAINED engine: it spawns a Swift subprocess that does
both audio capture (via ScreenCaptureKit) and transcription (via Apple's
new SpeechAnalyzer API introduced in macOS 26 Tahoe, June 2025).

Why self-contained: SpeechAnalyzer expects a continuous AsyncStream of
AVAudioPCMBuffer objects. Routing PCM bytes through a Python pipe just
to send them back out to a separate Swift transcriber would add latency
and unnecessary serialization. Doing capture+transcription in one Swift
process is the cleanest design.

Empirical motivation: on Python 3.14 the user observed:
- PyObjC `import Speech` cold-load: 48 seconds
- faster-whisper "base" model load: pushed total STT init past 100 seconds
SpeechAnalyzer is native Swift — load time is sub-second.

Apple's published benchmark: SpeechAnalyzer is ~55% faster than Whisper
(processing 34 minutes of audio in 45 seconds at WWDC25).

Requirements:
- macOS 26.0 Tahoe or newer
- Apple Silicon (uses Apple Neural Engine)
- Screen Recording permission
- Speech Recognition permission (granted on first run via system dialog)
- Xcode Command Line Tools (for one-time Swift compilation)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import queue
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from captionlm.stt.base import STTEngine

logger = logging.getLogger(__name__)

# Path to the Swift source (lives in audio/ to be next to capture_audio.swift)
_SWIFT_SRC = Path(__file__).parent.parent / "audio" / "capture_and_transcribe.swift"
_BINARY_DIR = Path.home() / ".cache" / "captionlm"
_BINARY_PATH = _BINARY_DIR / "capture_and_transcribe"

# Compilation can be slow on first run — Swift module cache for ScreenCaptureKit
# + Speech needs to be built. Subsequent runs are fast.
_COMPILE_TIMEOUT = 300  # seconds


# Map our short language codes to BCP-47 locale identifiers that
# SpeechTranscriber.supportedLocales returns. The Swift side normalizes
# hyphens vs underscores. Verified locales (from WWDC25 docs):
#   ar_SA, da_DK, de_AT, de_CH, de_DE, en_AU, en_CA, en_GB, en_IE,
#   en_IN, en_NZ, en_SG, en_US, en_ZA, es_CL, es_ES, es_MX, es_US,
#   fi_FI, fr_BE, fr_CA, fr_CH, fr_FR, he_IL, it_CH, it_IT, ja_JP,
#   ko_KR, ms_MY, nb_NO, nl_BE, nl_NL, pt_BR, ru_RU, sv_SE, th_TH,
#   tr_TR, vi_VN, yue_CN, zh_CN, zh_HK, zh_TW
_LANG_TO_LOCALE = {
    "en": "en-US",
    "zh": "zh-CN",
    "zh-tw": "zh-TW",
    "ja": "ja-JP",
    "ko": "ko-KR",
    "fr": "fr-FR",
    "de": "de-DE",
    "es": "es-ES",
    "pt": "pt-BR",
    "ru": "ru-RU",
    "ar": "ar-SA",
    "it": "it-IT",
    "vi": "vi-VN",
    "th": "th-TH",
    "nl": "nl-NL",
    "sv": "sv-SE",
    "tr": "tr-TR",
}


def _ensure_binary() -> Path:
    """Compile capture_and_transcribe.swift if needed, return binary path."""
    if platform.system() != "Darwin":
        raise RuntimeError("AppleSpeechAnalyzerSTT only works on macOS")

    _BINARY_DIR.mkdir(parents=True, exist_ok=True)

    # Recompile if binary missing or older than source
    if _BINARY_PATH.exists():
        src_mtime = _SWIFT_SRC.stat().st_mtime
        bin_mtime = _BINARY_PATH.stat().st_mtime
        if bin_mtime >= src_mtime:
            logger.debug("SpeechAnalyzer binary up to date")
            return _BINARY_PATH

    # Loud + clear logging so user knows what's blocking
    msg = (
        "Compiling SpeechAnalyzer Swift helper (first-run only; "
        "subsequent runs use cached binary). On Apple Silicon + macOS 26 + "
        "Xcode 26 CLT, expect 30-90 seconds while Swift builds the "
        "ScreenCaptureKit + Speech module cache."
    )
    logger.info(msg)
    print(f"  [compile] {msg}", flush=True)
    t0 = time.monotonic()

    try:
        result = subprocess.run(
            [
                "swiftc", "-O",
                "-o", str(_BINARY_PATH),
                str(_SWIFT_SRC),
                "-framework", "ScreenCaptureKit",
                "-framework", "CoreMedia",
                "-framework", "AVFoundation",
                "-framework", "Speech",
            ],
            capture_output=True,
            text=True,
            timeout=_COMPILE_TIMEOUT,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to compile SpeechAnalyzer helper:\n{result.stderr}\n"
                "Ensure Xcode Command Line Tools are installed:\n"
                "  xcode-select --install\n"
                "Note: SpeechAnalyzer requires Xcode 26+ command line tools."
            )

        os.chmod(_BINARY_PATH, 0o755)
        elapsed = time.monotonic() - t0
        logger.info("SpeechAnalyzer helper compiled in %.1fs", elapsed)
        print(f"  [compile] Done in {elapsed:.1f}s — cached at {_BINARY_PATH}",
              flush=True)

        # IMPORTANT: macOS Screen Recording permission is per-binary-path.
        # If the user previously granted permission to a different binary
        # (e.g., the old capture_audio), this NEW binary needs its own grant.
        # Empirical evidence: user saw "Stream was stopped by the system" 40s
        # after pipeline ready — classic symptom of unauthorized binary.
        msg = (
            "If subtitles don't appear, grant Screen Recording permission to "
            f"the new binary at {_BINARY_PATH} in System Settings → Privacy "
            "& Security → Screen Recording. macOS scopes permission per "
            "binary path; previous capture_audio grant does NOT carry over."
        )
        logger.warning(msg)
        print(f"  [permission] {msg}", flush=True)

        return _BINARY_PATH

    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Swift compilation timed out after {_COMPILE_TIMEOUT}s. "
            "First-time builds can be slow. Try again — module cache "
            "should be partially built."
        )
    except FileNotFoundError:
        raise RuntimeError(
            "swiftc not found. Install Xcode Command Line Tools:\n"
            "  xcode-select --install"
        )


class AppleSpeechAnalyzerSTT(STTEngine):
    """Self-contained STT using Apple SpeechAnalyzer (macOS 26+)."""

    is_self_contained = True

    def __init__(self, language: str = "en"):
        self._language = language
        self._locale = _LANG_TO_LOCALE.get(language, language)
        self._process: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        # Queue of (text, is_final) — populated by stdout reader, consumed
        # by next_transcript()
        self._results: queue.Queue[tuple[str, bool]] = queue.Queue(maxsize=200)
        self._ready = threading.Event()
        self._init_error: str | None = None
        self._stopping = False

        self._start_subprocess()

    def _start_subprocess(self):
        """Compile (if needed) and launch the Swift helper."""
        binary = _ensure_binary()

        logger.info("Starting SpeechAnalyzer subprocess (locale=%s)", self._locale)
        # EMPIRICAL FIX: bufsize=1 (line buffering) only works in TEXT mode,
        # not binary. With binary + bufsize=1 Python silently falls back to
        # 8KB block buffering — JSON results are stuck until 8KB accumulates.
        # Use text=True so we get real line-by-line reads as Swift flushes.
        self._process = subprocess.Popen(
            [str(binary), "--locale", self._locale],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        # Reader threads
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, daemon=True, name="speech-analyzer-stderr"
        )
        self._stderr_thread.start()

        self._reader_thread = threading.Thread(
            target=self._read_stdout, daemon=True, name="speech-analyzer-stdout"
        )
        self._reader_thread.start()

        # Subprocess liveness monitor — empirical: Swift can die silently
        # (e.g., crash inside SpeechAnalyzer or SCStream) and Python's reader
        # threads just exit when the pipe closes, with no error logged.
        # This monitor polls the process and loudly reports if it dies.
        self._monitor_thread = threading.Thread(
            target=self._monitor_process, daemon=True, name="speech-analyzer-monitor"
        )
        self._monitor_thread.start()

    def _monitor_process(self):
        """Poll the Swift subprocess every 2 seconds; log if it dies."""
        import time as _time
        while not self._stopping and self._process is not None:
            _time.sleep(2.0)
            if self._process is None:
                return
            rc = self._process.poll()
            if rc is not None:
                if not self._stopping:
                    logger.error(
                        "SpeechAnalyzer subprocess EXITED unexpectedly with code %d. "
                        "Audio capture and transcription have stopped. Restart the app.",
                        rc,
                    )
                return

        # Wait up to 60s for READY (or model download to complete)
        if not self._ready.wait(timeout=60.0):
            if self._init_error:
                raise RuntimeError(
                    f"SpeechAnalyzer failed to initialize: {self._init_error}"
                )
            # If process exited without READY
            if self._process.poll() is not None:
                raise RuntimeError(
                    f"SpeechAnalyzer subprocess exited (code={self._process.returncode}) "
                    "before signaling READY. Check Console.app for stderr output."
                )
            logger.warning(
                "SpeechAnalyzer did not signal READY within 60s — proceeding anyway"
            )

    def _read_stderr(self):
        """Consume stderr: detect READY, log INFO/ERROR lines."""
        if not self._process or not self._process.stderr:
            return
        try:
            # text=True so lines are str, no decode needed
            for raw_line in self._process.stderr:
                line = raw_line.strip()
                if not line:
                    continue
                if line == "READY":
                    self._ready.set()
                    logger.info("SpeechAnalyzer ready (locale=%s)", self._locale)
                elif line.startswith("ERROR:"):
                    msg = line[6:].strip()
                    logger.error("SpeechAnalyzer: %s", msg)
                    if self._init_error is None:
                        self._init_error = msg
                    self._ready.set()  # Unblock waiter to surface error
                elif line.startswith("INFO:"):
                    logger.info("SpeechAnalyzer: %s", line[5:].strip())
                else:
                    logger.debug("SpeechAnalyzer stderr: %s", line)
        except Exception as e:
            if not self._stopping:
                logger.error("Stderr reader exception: %s", e)

    def _read_stdout(self):
        """Consume stdout: parse JSON lines, push (text, is_final) to queue."""
        if not self._process or not self._process.stdout:
            return
        try:
            line_count = 0
            for raw_line in self._process.stdout:
                line = raw_line.strip()
                line_count += 1
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("SpeechAnalyzer non-JSON stdout: %s", line[:200])
                    continue

                text = data.get("text", "").strip()
                is_final = bool(data.get("is_final", False))
                # Log every transcript received — this is what we want to see
                logger.debug("SpeechAnalyzer transcript (final=%s): %s",
                             is_final, text[:80])
                if not text:
                    continue

                # Drop oldest if backlog
                try:
                    self._results.put_nowait((text, is_final))
                except queue.Full:
                    try:
                        self._results.get_nowait()
                    except queue.Empty:
                        pass
                    self._results.put_nowait((text, is_final))
        except Exception as e:
            if not self._stopping:
                logger.error("Stdout reader exception: %s", e)

    async def transcribe(self, audio: np.ndarray) -> str | None:
        """No-op: this is a self-contained engine. Use next_transcript()."""
        return None

    async def next_transcript(self) -> tuple[str, bool] | None:
        """Pull the next transcription result, blocking up to 1s.

        Returns (text, is_final) or None if nothing available within 1s.
        """
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                None, lambda: self._results.get(timeout=1.0)
            )
        except queue.Empty:
            return None

    async def stop(self):
        """Terminate the Swift subprocess."""
        self._stopping = True
        if self._process:
            try:
                self._process.terminate()
                try:
                    self._process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=1)
            except Exception:
                pass
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
        logger.info("SpeechAnalyzer subprocess stopped")

    @property
    def name(self) -> str:
        return f"Apple SpeechAnalyzer ({self._locale})"

    @property
    def requires_download(self) -> bool:
        # Model is downloaded on-demand by AssetInventory in Swift.
        # First-time use of a locale may take a few seconds.
        return True

    @property
    def download_size_mb(self) -> int:
        # Apple speech models are ~50-200MB depending on locale, but we
        # don't have a reliable way to predict before AssetInventory runs.
        return 100
