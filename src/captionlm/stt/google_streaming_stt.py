"""Google Cloud Speech-to-Text streaming STT engine.

Why this exists: Apple SpeechAnalyzer empirically batches its output
(emits 30 partial transcripts in one second AFTER the user finished a
sentence, instead of true real-time streaming). This produces a 10-15s
end-to-end latency that's unacceptable for live subtitles.

Google Cloud Speech-to-Text streaming API delivers true sub-second partials
via a long-running gRPC connection. Cost: $0.024/min, 60 free min/month.

Architecture:
- Spawn capture_audio.swift (the legacy raw-PCM Swift binary) as subprocess
- Background thread reads float32 PCM from its stdout, converts to int16
- Background thread feeds int16 chunks to Google streaming API via gRPC
- Background thread receives transcripts (interim + final), pushes to queue
- next_transcript() pops from queue (same interface as AppleSpeechAnalyzerSTT)

Session management: Google streaming has a ~5-minute hard limit per session.
We restart the session automatically.

Credentials: looks for ~/.captionlm/google-cloud-stt.json (Service Account JSON).
"""

from __future__ import annotations

import asyncio
import atexit
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

# ── Subprocess leak defense ──────────────────────────────────────────
# Empirically observed: when Python parent receives SIGTERM (e.g. autotest
# running `kill $PID`), default behavior terminates Python without running
# atexit handlers. The capture_audio Swift subprocess gets reparented to
# launchd and continues consuming ~21% CPU forever. Seven stale processes
# accumulated during a single development session.
#
# Defense layers (this file owns layers 1; layer 2 is in __main__.py;
# layer 3 is in capture_audio.swift):
#   1. Module-level registry of active Popens + atexit handler kills any
#      survivors. Handles graceful exit / SystemExit.
#   2. SIGTERM handler in __main__ converts signal to sys.exit(), which
#      DOES run atexit. Handles autotest kill / supervisor stop.
#   3. Swift parent-PID watchdog self-exits on orphaning. Handles SIGKILL
#      and Python crashes (no Python cleanup runs in those cases).
_active_processes: set[subprocess.Popen] = set()
_active_processes_lock = threading.Lock()
# Module-level flag flipped by atexit to signal "interpreter shutdown in
# progress — any subprocess exits from now on are intentional, not crashes".
# Monitor threads check this to suppress spurious "EXITED unexpectedly" errors
# during the SIGTERM → atexit teardown path (where instance-level _stopping
# never gets set because stop() is bypassed by Python interpreter shutdown).
_global_shutdown = False


def _kill_all_active_processes() -> None:
    """atexit cleanup: terminate any capture_audio subprocesses still alive."""
    global _global_shutdown
    _global_shutdown = True
    with _active_processes_lock:
        survivors = [p for p in _active_processes if p.poll() is None]
        _active_processes.clear()
    for proc in survivors:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
        except Exception:
            pass  # atexit must not raise


atexit.register(_kill_all_active_processes)

# Where the user puts their Service Account credentials JSON. We look for
# any service-account JSON in ~/.captionlm/ — Google's downloaded files have
# names like "gen-lang-client-XXXXX-XXXX.json", so just searching for valid
# service-account JSONs is more user-friendly than enforcing a specific name.
_CREDS_DIR = Path.home() / ".captionlm"


def _find_credentials() -> Path | None:
    """Find a Google Cloud service-account JSON in ~/.captionlm/.

    Search order:
    1. $GOOGLE_APPLICATION_CREDENTIALS environment variable (if set + exists)
    2. ~/.captionlm/google-cloud-stt.json (our recommended name)
    3. Any ~/.captionlm/*.json that contains '"type": "service_account"'
       (handles Google's auto-generated names like gen-lang-client-*.json)
    """
    import json as _json
    # 1. Env var (highest priority — user-set)
    env_val = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if env_val and Path(env_val).exists():
        return Path(env_val)
    # 2. Conventional name
    preferred = _CREDS_DIR / "google-cloud-stt.json"
    if preferred.exists():
        return preferred
    # 3. Any service-account JSON in ~/.captionlm/
    if _CREDS_DIR.is_dir():
        for p in sorted(_CREDS_DIR.glob("*.json")):
            try:
                with open(p, "r") as f:
                    data = _json.load(f)
                if isinstance(data, dict) and data.get("type") == "service_account":
                    return p
            except Exception:
                continue
    return None

# Path to the existing capture_audio.swift binary (raw PCM output)
_SWIFT_SRC = Path(__file__).parent.parent / "audio" / "capture_audio.swift"
_BINARY_DIR = Path.home() / ".cache" / "captionlm"
_BINARY_PATH = _BINARY_DIR / "capture_audio"
_COMPILE_TIMEOUT = 300

# Google streaming has a hard limit (~305 seconds). Restart before that.
_SESSION_RESTART_S = 270

# Map our short language codes to Google BCP-47 codes
_LANG_TO_LOCALE = {
    "en": "en-US",
    "zh": "zh-CN",   # Mandarin (Simplified)
    "zh-tw": "zh-TW",
    "ja": "ja-JP",
    "ko": "ko-KR",
    "fr": "fr-FR",
    "de": "de-DE",
    "es": "es-ES",
    "pt": "pt-BR",
    "ru": "ru-RU",
    "ar": "ar-XA",
    "it": "it-IT",
    "vi": "vi-VN",
    "th": "th-TH",
    "nl": "nl-NL",
    "sv": "sv-SE",
    "tr": "tr-TR",
    "hi": "hi-IN",
}


def _bundled_capture_binary() -> Path | None:
    """When running from a .app bundle (py2app build), look for the
    pre-compiled capture_audio in Resources/ so end users don't need
    Xcode Command Line Tools.

    py2app's DATA_FILES configuration places the binary at
    Contents/Resources/captionlm/audio/capture_audio.  We also check
    Resources/capture_audio as a fallback layout.
    """
    try:
        import sys as _sys
        if not getattr(_sys, "frozen", False):
            return None  # not running from .app — fall through to swiftc
        mac_os = Path(_sys.executable).resolve().parent
        resources = mac_os.parent / "Resources"
        for sub in ("captionlm/audio/capture_audio", "capture_audio"):
            p = resources / sub
            if p.is_file():
                return p
    except Exception:
        pass
    return None


def _ensure_capture_binary() -> Path:
    """Return a path to a usable capture_audio binary.

    Priority:
      1. Pre-compiled binary bundled inside a py2app .app (end-user path)
      2. Cached binary already in ~/.cache/captionlm (warm dev path)
      3. Compile capture_audio.swift with `swiftc` (cold dev path)

    For end users running the released .dmg, only branch 1 ever runs —
    they don't need Xcode CLT. For developers running from source,
    branch 3 produces the cache once, then branch 2 uses it forever.
    """
    if platform.system() != "Darwin":
        raise RuntimeError("Google Streaming STT capture only works on macOS")

    # ── Priority 1: pre-compiled binary inside .app bundle ──
    bundled = _bundled_capture_binary()
    if bundled is not None:
        # Copy to ~/.cache so we always launch from a writable, stable
        # path (Resources/ inside .app may be code-signed/read-only on
        # future builds, and subprocess.Popen with a path that gets
        # remounted by a stapled notarization can misbehave).
        _BINARY_DIR.mkdir(parents=True, exist_ok=True)
        needs_copy = (
            not _BINARY_PATH.exists()
            or _BINARY_PATH.stat().st_mtime < bundled.stat().st_mtime
            or _BINARY_PATH.stat().st_size != bundled.stat().st_size
        )
        if needs_copy:
            import shutil
            shutil.copy2(bundled, _BINARY_PATH)
            os.chmod(_BINARY_PATH, 0o755)
            logger.info(
                "Installed bundled capture_audio → %s (%d bytes)",
                _BINARY_PATH, _BINARY_PATH.stat().st_size,
            )
        return _BINARY_PATH

    # ── Priority 2 + 3: source-tree fallback (swiftc required) ──
    # Stage the .swift source into ~/.cache (out of iCloud's reach so
    # iCloud-touch-mid-build doesn't trigger swiftc "input modified"
    # errors — verified 2026-05-04 incident).
    _BINARY_DIR.mkdir(parents=True, exist_ok=True)
    staged_src = _BINARY_DIR / "capture_audio.swift"
    if not _SWIFT_SRC.exists():
        raise RuntimeError(
            "capture_audio not bundled AND .swift source not found at "
            f"{_SWIFT_SRC}. This is not a valid CaptionLM build."
        )
    src_mtime = _SWIFT_SRC.stat().st_mtime
    if not staged_src.exists() or staged_src.stat().st_mtime < src_mtime:
        import shutil
        shutil.copy2(_SWIFT_SRC, staged_src)
        os.utime(staged_src, None)

    # Cached binary is up-to-date → reuse
    if _BINARY_PATH.exists():
        if _BINARY_PATH.stat().st_mtime >= staged_src.stat().st_mtime:
            return _BINARY_PATH

    # Need to compile — require swiftc
    import shutil as _shutil
    if not _shutil.which("swiftc"):
        raise RuntimeError(
            "swiftc not found. End-user installs should have a bundled "
            "capture_audio binary inside the .app; if you see this in a "
            "released build it's a packaging bug — please report it. "
            "Developers running from source: install Xcode Command Line "
            "Tools with `xcode-select --install`."
        )
    logger.info("Compiling capture_audio.swift via swiftc (dev mode)...")
    t0 = time.monotonic()
    result = subprocess.run(
        [
            "swiftc", "-O",
            "-o", str(_BINARY_PATH),
            str(staged_src),
            "-framework", "ScreenCaptureKit",
            "-framework", "CoreMedia",
            "-framework", "AVFoundation",
        ],
        capture_output=True, text=True, timeout=_COMPILE_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to compile capture_audio:\n{result.stderr}\n"
            "Install Xcode CLT: xcode-select --install"
        )
    os.chmod(_BINARY_PATH, 0o755)
    logger.info("capture_audio compiled in %.1fs", time.monotonic() - t0)
    return _BINARY_PATH


class GoogleStreamingSTT(STTEngine):
    """Self-contained STT using Google Cloud Speech-to-Text streaming API."""

    is_self_contained = True

    def __init__(self, language: str = "en"):
        self._language = language
        self._locale = _LANG_TO_LOCALE.get(language, language)

        # Find credentials (any *.json with "type":"service_account" in
        # ~/.captionlm/, or use $GOOGLE_APPLICATION_CREDENTIALS env var)
        creds_path = _find_credentials()
        if creds_path is None:
            raise RuntimeError(
                f"No Google Cloud service-account JSON found in {_CREDS_DIR}.\n"
                "Setup steps:\n"
                "  1. Create a Service Account at console.cloud.google.com\n"
                "     with role 'Cloud Speech Client'\n"
                "  2. Download its JSON key (any name like 'gen-lang-client-*.json')\n"
                "  3. Move to ~/.captionlm/  (any filename ending in .json works)\n"
                "  4. chmod 600 ~/.captionlm/*.json"
            )
        logger.info("Using Google Cloud credentials: %s", creds_path)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(creds_path)

        # Lazy import google-cloud-speech (heavy ~3-5s)
        try:
            from google.cloud import speech
        except ImportError:
            raise ImportError(
                "google-cloud-speech is required for Google Streaming STT. "
                "Install: pip install google-cloud-speech"
            )
        self._speech = speech
        self._client = speech.SpeechClient()

        self._capture_process: subprocess.Popen | None = None
        self._results_queue: queue.Queue[tuple[str, bool]] = queue.Queue(maxsize=200)
        self._stopping = False
        self._stream_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._monitor_thread: threading.Thread | None = None

        # Health flags introduced 2026-05-13 after real meeting session
        # log analysis: when capture_audio Swift subprocess died (macOS
        # "Stream stopped by the system"), the STT layer kept blindly
        # restarting Google connection every 10s, spawning 6+ gRPC
        # internal threads and emitting cascading 400 Audio Timeout
        # errors. _capture_audio_died gates the restart loop; fatal_error
        # is surfaced via next_transcript so the pipeline knows to stop.
        self._capture_audio_died: bool = False
        self._audio_alive: bool = False  # set True on first READY
        self._last_audio_chunk_time: float = 0.0  # for heartbeat
        self.fatal_error: str | None = None
        # Auto-reconnect bookkeeping for capture_audio subprocess.
        # macOS sometimes returns SCStreamError -3821 transiently
        # (other app using ScreenCaptureKit, system alert, sleep wake).
        # We retry up to _MAX_CAPTURE_RECONNECTS times with a delay
        # before escalating to fatal_error.
        self._capture_reconnect_attempts: int = 0
        self._capture_started_at: float = 0.0

        self._start_capture()
        self._start_streaming_thread()
        self._start_monitor_thread()

    # ── audio capture (raw PCM from Swift helper) ─────────────────

    # Capture-audio auto-reconnect tuning (2026-05-14).
    # See QwenLiveTranslateSTT for rationale — same approach mirrored here.
    _MAX_CAPTURE_RECONNECTS: int = 3
    _CAPTURE_RECONNECT_DELAY_SEC: float = 3.0
    _CAPTURE_HEALTHY_RESET_SEC: float = 30.0

    def _start_capture(self):
        binary = _ensure_capture_binary()
        logger.info("Starting capture_audio subprocess for Google streaming")
        self._capture_process = subprocess.Popen(
            [str(binary)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        # Register for atexit cleanup (Layer 1 of subprocess-leak defense).
        with _active_processes_lock:
            _active_processes.add(self._capture_process)
        self._capture_started_at = time.monotonic()
        # Read stderr in background to detect READY + log INFO/ERROR
        self._stderr_thread = threading.Thread(
            target=self._read_capture_stderr,
            daemon=True,
            name="google-stt-stderr",
        )
        self._stderr_thread.start()

    def _terminate_capture(self):
        """Tear down the current capture_audio subprocess cleanly.
        Used both on full stop() and between auto-reconnect attempts.
        Idempotent."""
        proc = self._capture_process
        if proc is not None:
            with _active_processes_lock:
                _active_processes.discard(proc)
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception as e:
                logger.warning("terminate capture_audio failed: %s", e)
            self._capture_process = None

    def _read_capture_stderr(self):
        if not self._capture_process or not self._capture_process.stderr:
            return
        # Lines that signal "the audio source is permanently gone for
        # this Swift invocation". Detected via stderr substring match.
        # Verified from 2026-05-13 real meeting log:
        #   "capture_audio: ERROR: Stream stopped: Stream was stopped by the system"
        # macOS ScreenCaptureKit decides to stop streaming under various
        # conditions (screen lock, audio device change, app loses focus,
        # SCStreamErrorDomain code 3804/-3819). We treat any of these
        # as terminal for the Swift invocation.
        _DEATH_MARKERS = (
            "Stream stopped",
            "Stream was stopped",
            "stream stopped by the system",
        )
        # gRPC-python writes pre-exec warnings to the child's stderr
        # (inherited from the Python parent that has grpc loaded). These
        # show up as `I0513 23:35:10.853779 62515670 ev_poll_posix.cc:593]
        # FD from fork parent still in poll list: fd(35, generation: 1)`
        # and we get DOZENS of them per subprocess start. They're not
        # from capture_audio and not actionable. Drop them.
        _GRPC_NOISE_TOKENS = (
            "ev_poll_posix.cc",
            "FD from fork parent",
            "absl/log",
        )
        try:
            for raw_line in self._capture_process.stderr:
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue
                # Suppress grpc fork-FD spam.
                if any(tok in line for tok in _GRPC_NOISE_TOKENS):
                    continue
                if line == "READY":
                    logger.info("capture_audio ready (system audio flowing)")
                    self._audio_alive = True
                elif any(m in line for m in _DEATH_MARKERS):
                    # NOTE (2026-05-14): no longer set fatal_error here.
                    # _stream_loop now auto-reconnects up to
                    # _MAX_CAPTURE_RECONNECTS times before escalating.
                    logger.error("capture_audio: %s (AUDIO SOURCE LOST)", line)
                    self._capture_audio_died = True
                    self._audio_alive = False
                elif line.startswith("ERROR"):
                    logger.error("capture_audio: %s", line)
                else:
                    logger.debug("capture_audio: %s", line)
        except Exception as e:
            if not self._stopping:
                logger.error("capture stderr reader error: %s", e)
        # When stderr stream closes, the subprocess has exited. If we
        # weren't asked to stop, that's an unexpected death — _stream_loop
        # will see _capture_audio_died and attempt reconnect.
        if not self._stopping and not self._capture_audio_died:
            logger.warning(
                "capture_audio stderr stream closed unexpectedly — "
                "marking subprocess as dead"
            )
            self._capture_audio_died = True
            self._audio_alive = False

    def _read_pcm_chunk(self, max_bytes: int = 6400) -> bytes | None:
        """Read float32 PCM from capture_audio, convert to int16 LINEAR16.

        Google's streaming API expects 16-bit signed little-endian PCM at
        16kHz. Our Swift helper outputs float32 — we convert.

        max_bytes=6400 = 1600 float32 samples = 100ms at 16kHz.
        Google recommends sending 100ms chunks.
        """
        if not self._capture_process or not self._capture_process.stdout:
            return None
        try:
            raw = self._capture_process.stdout.read(max_bytes)
        except Exception:
            return None
        if not raw or len(raw) < 4:
            return None
        # Track latest audio activity for the overlay heartbeat indicator.
        self._last_audio_chunk_time = time.monotonic()
        # Convert float32 [-1, 1] → int16
        floats = np.frombuffer(raw, dtype=np.float32)
        int16s = (np.clip(floats, -1.0, 1.0) * 32767).astype(np.int16)
        return int16s.tobytes()

    # ── streaming session loop ────────────────────────────────────

    def _start_streaming_thread(self):
        self._stream_thread = threading.Thread(
            target=self._stream_loop,
            daemon=True,
            name="google-stt-stream",
        )
        self._stream_thread.start()

    def _stream_loop(self):
        """Outer loop: restart streaming session every ~4.5 minutes,
        OR on transient errors. EXCEPT if capture_audio has died — in
        that case give up so we don't spin a cascade of dead-end
        Google requests.

        Empirical motivation (2026-05-13 real meeting log): after Swift
        capture_audio was killed by macOS, this loop kept restarting
        the Google connection every 10s for 70 seconds. 6+ gRPC
        threads leaked. User had to manually click Stop to escape.
        """
        while not self._stopping:
            # ── Auto-reconnect logic for capture_audio death ──
            # If capture died OR subprocess exited unexpectedly, try
            # to restart it up to _MAX_CAPTURE_RECONNECTS times before
            # escalating to fatal_error. macOS -3821 is often transient.
            capture_dead = (
                self._capture_audio_died
                or (self._capture_process is not None
                    and self._capture_process.poll() is not None)
            )
            if capture_dead:
                alive_dur = time.monotonic() - self._capture_started_at
                if alive_dur >= self._CAPTURE_HEALTHY_RESET_SEC:
                    if self._capture_reconnect_attempts > 0:
                        logger.info(
                            "Capture was healthy for %.1fs — resetting "
                            "reconnect counter from %d to 0",
                            alive_dur, self._capture_reconnect_attempts,
                        )
                    self._capture_reconnect_attempts = 0

                if self._capture_reconnect_attempts >= self._MAX_CAPTURE_RECONNECTS:
                    self.fatal_error = (
                        f"Audio source lost: macOS stopped the capture "
                        f"stream {self._MAX_CAPTURE_RECONNECTS} times in a "
                        f"row. Possible causes: another app is using "
                        f"ScreenCaptureKit (Zoom screen-share, OBS, "
                        f"QuickTime recording), or the system denied "
                        f"audio capture. Please stop and start the "
                        f"pipeline manually."
                    )
                    logger.error("STT loop giving up: %s", self.fatal_error)
                    return

                self._capture_reconnect_attempts += 1
                logger.warning(
                    "capture_audio died — auto-reconnect attempt %d/%d "
                    "after %.1fs delay",
                    self._capture_reconnect_attempts,
                    self._MAX_CAPTURE_RECONNECTS,
                    self._CAPTURE_RECONNECT_DELAY_SEC,
                )
                self._terminate_capture()
                # Sleep in slices so stop() can interrupt promptly.
                _slept = 0.0
                _slice = 0.2
                while _slept < self._CAPTURE_RECONNECT_DELAY_SEC:
                    if self._stopping:
                        logger.info("Reconnect sleep interrupted by stop()")
                        return
                    time.sleep(_slice)
                    _slept += _slice
                if self._stopping:
                    return
                self._capture_audio_died = False
                self._audio_alive = False
                try:
                    self._start_capture()
                except Exception as e:
                    logger.error("Failed to restart capture_audio: %s", e)
                    self._capture_audio_died = True
                    continue
                # Fall through to spin up a fresh Google streaming session.

            # Cleanup any prior gRPC worker thread before spawning the
            # next session. Doesn't strictly own the threads (gRPC
            # client owns them) but joining-with-timeout helps surface
            # leaks in logs if anything stays alive. Verified leak in
            # 2026-05-13 log: Thread-10/13/16/19/22/25 = 6 threads in
            # 70s of restart loop.
            prior = self._stream_thread
            if prior is not None and prior is not threading.current_thread():
                if prior.is_alive():
                    logger.debug(
                        "Joining prior STT worker thread %s before restart",
                        prior.name,
                    )
                    prior.join(timeout=1.0)
                    if prior.is_alive():
                        logger.warning(
                            "Prior STT worker %s did not exit within 1s — "
                            "potential thread leak",
                            prior.name,
                        )

            try:
                self._run_one_session()
            except Exception as e:
                if not self._stopping:
                    logger.error("Streaming session error: %s — restarting in 1s", e)
                    time.sleep(1)

    def _run_one_session(self):
        """One streaming session, capped at _SESSION_RESTART_S seconds."""
        speech = self._speech
        # Use 'default' model — works across all locales without "model not
        # available for this language" errors. latest_long is English-only
        # in some regions which would silently return empty results for ja-JP.
        # (Diarization was tried 2026-05-06 and didn't improve dialogue
        # scenarios on Japanese — Google returned no useful speaker tags.
        # Removed.)
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=16000,
            audio_channel_count=1,
            language_code=self._locale,
            enable_automatic_punctuation=True,
        )
        streaming_config = speech.StreamingRecognitionConfig(
            config=config,
            interim_results=True,  # ← real-time partials
        )

        session_started = time.monotonic()
        # Diagnostic counters for this session
        chunks_sent = 0
        bytes_sent = 0
        responses_received = 0
        logger.info(
            "Google streaming session start (locale=%s, encoding=LINEAR16, sr=16000)",
            self._locale,
        )

        def request_generator():
            """Yields ONLY audio chunks (no streaming_config in first message).

            EMPIRICAL FIX: google-cloud-speech v2.30+ helper signature is
            streaming_recognize(config, requests, ...) — config is a separate
            positional argument. The old pattern of yielding streaming_config
            as the first request fails with:
              "missing 1 required positional argument: 'config'"
            """
            nonlocal chunks_sent, bytes_sent
            logger.info("Audio request generator started; waiting for first PCM chunk")
            first_chunk_logged = False
            last_diag = time.monotonic()

            while not self._stopping:
                if time.monotonic() - session_started >= _SESSION_RESTART_S:
                    logger.info("Session approaching limit — restarting")
                    return
                chunk = self._read_pcm_chunk()
                if chunk is None:
                    logger.warning("PCM source returned None — ending session")
                    return
                if len(chunk) == 0:
                    time.sleep(0.01)
                    continue
                chunks_sent += 1
                bytes_sent += len(chunk)
                if not first_chunk_logged:
                    logger.info("First PCM chunk sent to Google (%d bytes)", len(chunk))
                    first_chunk_logged = True
                now = time.monotonic()
                if now - last_diag >= 5.0:
                    logger.info(
                        "[google-stt diag] chunks_sent=%d, bytes_sent=%d, "
                        "responses_received=%d, results_queued=%d",
                        chunks_sent, bytes_sent, responses_received,
                        self._results_queue.qsize(),
                    )
                    last_diag = now
                yield speech.StreamingRecognizeRequest(audio_content=chunk)

        # google-cloud-speech v2.30+ helper: config and requests are SEPARATE
        # positional arguments (config is NOT yielded as first request).
        responses = self._client.streaming_recognize(
            config=streaming_config,
            requests=request_generator(),
        )

        first_response_logged = False
        for response in responses:
            if self._stopping:
                break
            responses_received += 1
            if not first_response_logged:
                logger.info("First Google response received (took %.1fs from session start)",
                            time.monotonic() - session_started)
                first_response_logged = True
            # Google STT can return MULTIPLE results in one response:
            # typically a "stable past" result + an "in-progress" result
            # (verified in production log 2026-05-13 — same response
            # contained "韩国食品...创下了136" and "1美元的历史新高"
            # as two non-final results). They are NOT cumulative —
            # each represents a different segment of the audio. The
            # cumulative current transcript is the concatenation of
            # all non-final results in stream order.
            #
            # Strategy:
            #   - All FINAL results are queued individually (each is
            #     a settled segment, downstream wants to see them).
            #   - All NON-FINAL results are concatenated into a single
            #     cumulative text and queued as ONE item — that way
            #     the pipeline's translated_prefix tracking sees
            #     monotonically-growing text rather than alternating
            #     between disjoint segments (which was triggering
            #     constant false utterance-resets).
            non_final_parts: list[str] = []
            for result in response.results:
                if not result.alternatives:
                    continue
                text = result.alternatives[0].transcript.strip()
                if not text:
                    continue
                if bool(result.is_final):
                    logger.debug("Google STT (final=True): %s", text[:80])
                    try:
                        self._results_queue.put_nowait((text, True))
                    except queue.Full:
                        try:
                            self._results_queue.get_nowait()
                        except queue.Empty:
                            pass
                        self._results_queue.put_nowait((text, True))
                else:
                    non_final_parts.append(text)
            if non_final_parts:
                # Concatenate non-final segments. Use no separator —
                # the parts already represent continuous speech; adding
                # a space between segments would put a literal space in
                # the middle of words for Chinese/Japanese.
                combined = "".join(non_final_parts)
                logger.debug(
                    "Google STT (final=False, n_parts=%d): %s",
                    len(non_final_parts), combined[:80],
                )
                try:
                    self._results_queue.put_nowait((combined, False))
                except queue.Full:
                    try:
                        self._results_queue.get_nowait()
                    except queue.Empty:
                        pass
                    self._results_queue.put_nowait((combined, False))

        elapsed = time.monotonic() - session_started
        logger.info(
            "Google streaming session ended after %.1fs "
            "(chunks_sent=%d, bytes_sent=%d, responses_received=%d)",
            elapsed, chunks_sent, bytes_sent, responses_received,
        )

    # ── subprocess liveness monitor ───────────────────────────────

    def _start_monitor_thread(self):
        self._monitor_thread = threading.Thread(
            target=self._monitor, daemon=True, name="google-stt-monitor"
        )
        self._monitor_thread.start()

    def _monitor(self):
        while not self._stopping and self._capture_process is not None:
            time.sleep(2.0)
            if self._capture_process is None:
                return
            rc = self._capture_process.poll()
            if rc is not None:
                # Suppress ERROR if shutdown is in progress through any path:
                #   - self._stopping: instance stop() called normally
                #   - _global_shutdown: atexit handler invoked (SIGTERM path,
                #     where stop() is bypassed by Python interpreter teardown)
                if not self._stopping and not _global_shutdown:
                    logger.error(
                        "capture_audio EXITED unexpectedly (code=%d). "
                        "No more audio for Google STT.", rc,
                    )
                else:
                    logger.info(
                        "capture_audio exited (code=%d) during shutdown.", rc,
                    )
                return

    # ── STTEngine interface ───────────────────────────────────────

    async def transcribe(self, audio: np.ndarray) -> str | None:
        """No-op: this is a self-contained engine. Use next_transcript()."""
        return None

    async def next_transcript(self) -> tuple[str, bool] | None:
        # If we're already stopping, don't touch the executor — it may have
        # been shut down by the SIGTERM-triggered atexit teardown, in which
        # case run_in_executor() raises RuntimeError("cannot schedule new
        # futures after shutdown") and pollutes shutdown logs with a noisy
        # traceback. Empirically observed during autotest's `kill -15` path.
        if self._stopping:
            return None
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                None, lambda: self._results_queue.get(timeout=1.0)
            )
        except queue.Empty:
            return None
        except RuntimeError as e:
            # Defensive: race window where _stopping wasn't yet set but the
            # asyncio executor was already torn down by Python interpreter
            # shutdown. Treat as end-of-stream.
            if "cannot schedule new futures after shutdown" in str(e):
                return None
            raise

    async def drain_transcripts(self) -> list[tuple[str, bool]]:
        """Drain ALL queued transcripts at once — see base.py for rationale.

        Empirical (2026-05-05): under continuous YouTube speech the
        results_queue grew from 7 → 97 over 90 seconds. Using next_transcript()
        in the pipeline pulled one at a time, so translation rate (~1/s) was
        far below STT production rate (~5/s) and lag accumulated linearly.
        Pulling everything available in one call lets the pipeline collapse
        intermediate non-finals and stay current with the speaker.
        """
        if self._stopping:
            return []
        results: list[tuple[str, bool]] = []
        # Block briefly for the first one so a quiet pipeline doesn't busy-loop.
        loop = asyncio.get_event_loop()
        try:
            first = await loop.run_in_executor(
                None, lambda: self._results_queue.get(timeout=1.0)
            )
            results.append(first)
        except queue.Empty:
            return []
        except RuntimeError as e:
            if "cannot schedule new futures after shutdown" in str(e):
                return []
            raise
        # Drain remaining without blocking.
        while True:
            try:
                results.append(self._results_queue.get_nowait())
            except queue.Empty:
                break
        if len(results) > 1:
            logger.debug(
                "drain_transcripts: %d items pulled (queue catch-up)",
                len(results),
            )
        return results

    async def stop(self):
        self._stopping = True
        if self._capture_process:
            proc = self._capture_process
            # Deregister from atexit cleanup set first — we're handling this
            # one here, no need for the global handler to also try.
            with _active_processes_lock:
                _active_processes.discard(proc)
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1)
            except Exception:
                pass
            self._capture_process = None
        logger.info("GoogleStreamingSTT stopped")

    @property
    def name(self) -> str:
        return f"Google Cloud Speech ({self._locale})"

    @property
    def requires_download(self) -> bool:
        return False  # No model download — uses cloud API
