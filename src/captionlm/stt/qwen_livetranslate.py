"""Qwen LiveTranslate (DashScope) realtime audio→translated-text engine.

Why this exists (2026-05-13): Aliyun's qwen3-livetranslate-flash-realtime
is an END-TO-END audio→translation model with a ~3-second simultaneous-
translation delay. Unlike our other STT engines (which only do speech
recognition; we then run Gemini/etc. on top), this single API does
both, which:
  1. Reduces total moving parts (no separate translator API call)
  2. Lowers per-utterance latency (no STT→Translator round-trip)
  3. Improves accuracy for low-frequency / homophone terms because the
     model sees raw audio when picking the translation (the marketing
     pitch — empirically TBD)

Architecture:
- WebSocket to wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=...
- Bearer-token auth via DASHSCOPE_API_KEY (or settings.get_api_key("dashscope"))
- Reuses the existing Swift capture_audio subprocess (same one Google
  STT uses) for raw PCM @ 16kHz mono. Stream chunks base64-encoded as
  input_audio_buffer.append events.
- Configure the session with:
    - session.input_audio_transcription.language  (source lang)
    - session.input_audio_transcription.model = qwen3-asr-flash-realtime
      → opt-IN to ALSO get the source-language transcript so we can
      show bilingual (orig + trans) subtitles. Without this we only
      get the translation, no original.
    - session.translation.language                (target lang)
    - session.modalities = ["text"]               (text-only — we
      don't need TTS audio playback, only subtitles)
- Receive events:
    - conversation.item.input_audio_transcription.text / .completed
      → source-language text (streamed + final)
    - response.text.done  → translation text (text-only mode is batch
      per utterance, not streaming — model emits the full translated
      sentence once the server-side VAD detects end of speech)
    - error events
- Implements provides_translation=True so the pipeline skips its own
  Gemini/etc. translator call. Pipeline reads (orig, trans, is_final)
  triples via next_translation() / drain_translations().

Cost: 12.5 token/sec for audio in, plus output text tokens (10x cheaper
than full LLM calls).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import queue
import subprocess
import threading
import time

import numpy as np

from captionlm.stt.base import STTEngine
# Reuse the Swift capture_audio binary builder + process registry.
from captionlm.stt.google_streaming_stt import (
    _ensure_capture_binary,
    _active_processes,
    _active_processes_lock,
    _global_shutdown,
)

logger = logging.getLogger(__name__)

# DashScope realtime endpoints — region-dependent.
# Verified 2026-05-13: keys are NOT interchangeable across regions.
# User's intl key got HTTP 401 from the cn endpoint.
_API_URLS = {
    "intl": (
        "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime"
        "?model=qwen3-livetranslate-flash-realtime"
    ),
    "cn": (
        "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
        "?model=qwen3-livetranslate-flash-realtime"
    ),
}

# Map our short language codes to Qwen's expected codes (BCP-47-ish).
# Reference: docs §"支持的语种" — codes are plain ISO 639-1 mostly.
_LANG_TO_QWEN = {
    "en": "en",
    "zh": "zh",
    "zh-tw": "zh",   # Qwen doesn't distinguish; falls back to zh
    "ja": "ja",
    "ko": "ko",
    "fr": "fr",
    "de": "de",
    "es": "es",
    "pt": "pt",
    "ru": "ru",
    "ar": "ar",
    "it": "it",
    "vi": "vi",
    "th": "th",
    "id": "id",
    "tr": "tr",
    "hi": "hi",
    "el": "el",
    "yue": "yue",
}


class QwenLiveTranslateSTT(STTEngine):
    """Self-contained streaming engine that does STT+translation in one
    WebSocket session against Aliyun's qwen3-livetranslate-flash-realtime.
    """

    is_self_contained = True
    provides_translation = True  # ← pipeline will skip its translator

    # Capture-audio auto-reconnect tuning (2026-05-14).
    # macOS occasionally returns SCStreamError -3821 "Stream stopped by
    # the system" for transient reasons (other apps grabbing the audio
    # source, system alerts, sleep/wake, etc.). Previously user had to
    # manually press stop/start to recover. Now we auto-restart the
    # Swift subprocess up to _MAX_CAPTURE_RECONNECTS times with a
    # _CAPTURE_RECONNECT_DELAY_SEC delay between attempts (giving
    # macOS time to release the prior SCStream). Only after that many
    # consecutive failures do we escalate to fatal_error.
    _MAX_CAPTURE_RECONNECTS: int = 3
    _CAPTURE_RECONNECT_DELAY_SEC: float = 3.0
    # If capture stays alive this long without failing, reset the
    # consecutive-failure counter — the recent issues are unrelated.
    _CAPTURE_HEALTHY_RESET_SEC: float = 30.0

    def __init__(
        self,
        api_key: str,
        source_lang: str = "en",
        target_lang: str = "zh",
        region: str = "intl",
    ):
        # 2026-05-14: User reports Qwen "启动很慢比Google慢很多"; log
        # showed Qwen STT init = 2.9-4.8s vs Google 0.1-0.5s. The
        # difference's root cause isn't obvious from code reading —
        # Qwen __init__ should be ~all non-blocking (subprocess.Popen
        # + threading.Thread.start). Adding step-by-step timing logs
        # so the NEXT log run can grep "Qwen init step" and see
        # exactly which line is slow.
        _t_init0 = time.monotonic()
        if not api_key:
            raise RuntimeError(
                "DashScope API key required for Qwen LiveTranslate STT. "
                "Set DASHSCOPE_API_KEY env var, or enter it in the "
                "Control Panel under API Keys."
            )
        if region not in _API_URLS:
            logger.warning(
                "Unknown DashScope region %r, falling back to 'intl'",
                region,
            )
            region = "intl"
        self._api_key = api_key
        self._source = _LANG_TO_QWEN.get(source_lang, source_lang)
        self._target = _LANG_TO_QWEN.get(target_lang, target_lang)
        self._region = region
        self._api_url = _API_URLS[region]

        # Subprocess / threads / queue
        self._capture_process: subprocess.Popen | None = None
        self._results_queue: queue.Queue[tuple[str, str, bool]] = queue.Queue(maxsize=200)
        self._stopping = False
        self._ws_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        # Per-utterance state for pairing source + translation
        self._current_orig: str = ""
        self._current_trans: str = ""
        # Health flags (same shape as Google STT for pipeline reuse).
        self._capture_audio_died: bool = False
        self._audio_alive: bool = False
        self._last_audio_chunk_time: float = 0.0
        self.fatal_error: str | None = None
        # Auto-reconnect bookkeeping for capture_audio subprocess.
        self._capture_reconnect_attempts: int = 0
        self._capture_started_at: float = 0.0

        # Async event loop for the websockets client. Runs in its own
        # thread so we don't fight with Qt's main loop or the pipeline's
        # asyncio loop. The PCM-read thread (sync) posts coroutines into
        # this loop via run_coroutine_threadsafe.
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._ws = None  # websockets.WebSocketClientProtocol

        _t_fields = time.monotonic()
        self._start_capture()
        _t_cap = time.monotonic()
        self._start_ws_thread()
        _t_ws = time.monotonic()
        logger.info(
            "Qwen init steps (ms): fields=%.0f  _start_capture=%.0f  "
            "_start_ws_thread=%.0f  TOTAL=%.0f",
            (_t_fields - _t_init0) * 1000,
            (_t_cap - _t_fields) * 1000,
            (_t_ws - _t_cap) * 1000,
            (_t_ws - _t_init0) * 1000,
        )

    @property
    def name(self) -> str:
        return "QwenLiveTranslate"

    async def transcribe(self, audio):
        # Self-contained engine — chunk-based interface is a no-op.
        return None

    # ── audio capture (raw PCM from Swift helper) ─────────────────

    def _start_capture(self):
        _t0 = time.monotonic()
        binary = _ensure_capture_binary()
        _t_bin = time.monotonic()
        logger.info("Starting capture_audio subprocess for Qwen LiveTranslate")
        self._capture_process = subprocess.Popen(
            [str(binary)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        _t_popen = time.monotonic()
        with _active_processes_lock:
            _active_processes.add(self._capture_process)
        self._capture_started_at = time.monotonic()
        # stderr reader — same DEATH-marker logic as Google STT.
        self._stderr_thread = threading.Thread(
            target=self._read_capture_stderr,
            daemon=True,
            name="qwen-stt-stderr",
        )
        self._stderr_thread.start()
        _t_thread = time.monotonic()
        logger.info(
            "Qwen _start_capture (ms): ensure_binary=%.0f  Popen=%.0f  "
            "stderr_thread=%.0f  TOTAL=%.0f",
            (_t_bin - _t0) * 1000,
            (_t_popen - _t_bin) * 1000,
            (_t_thread - _t_popen) * 1000,
            (_t_thread - _t0) * 1000,
        )

    def _terminate_capture(self):
        """Tear down the current capture_audio subprocess + stderr thread
        cleanly. Used both on full stop() and between auto-reconnect
        attempts. Idempotent — safe to call when capture is already gone.
        """
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
        # Don't join _stderr_thread here — it exits naturally when its
        # stdin (the subprocess stderr) closes after terminate above.
        # Joining would deadlock if the thread is mid-readline.

    def _read_capture_stderr(self):
        if not self._capture_process or not self._capture_process.stderr:
            return
        _DEATH = ("Stream stopped", "Stream was stopped")
        # Same gRPC noise filter as Google STT — when grpc has been
        # loaded in the parent (which happens once Google STT runs in
        # the same process), every subsequent subprocess.Popen() shows
        # gRPC fork-FD warnings on the child's stderr before exec.
        _GRPC_NOISE_TOKENS = (
            "ev_poll_posix.cc",
            "FD from fork parent",
            "absl/log",
        )
        try:
            for raw in self._capture_process.stderr:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                if any(tok in line for tok in _GRPC_NOISE_TOKENS):
                    continue
                if line == "READY":
                    logger.info("capture_audio ready (Qwen audio flowing)")
                    self._audio_alive = True
                elif any(m in line for m in _DEATH):
                    # NOTE (2026-05-14): we no longer set fatal_error here.
                    # _ws_main detects _capture_audio_died and tries up to
                    # _MAX_CAPTURE_RECONNECTS auto-reconnects before
                    # escalating. Only after that many failures does
                    # fatal_error get set (and pipeline see the death).
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
        if not self._stopping and not self._capture_audio_died:
            # Stderr closed unexpectedly — also a death signal but
            # without an explicit -3821-style line. Same auto-reconnect
            # path: just flip the flag, _ws_main decides what to do.
            self._capture_audio_died = True
            self._audio_alive = False

    def _read_pcm_chunk(self, max_bytes: int = 6400) -> bytes | None:
        """Read float32 from Swift, convert to int16 LINEAR16 PCM.

        Same conversion as Google STT (float32 → int16). Qwen accepts
        LINEAR16 mono 16kHz audio (input_audio_format = "pcm").
        """
        if not self._capture_process or not self._capture_process.stdout:
            return None
        try:
            raw = self._capture_process.stdout.read(max_bytes)
        except Exception:
            return None
        if not raw or len(raw) < 4:
            return None
        self._last_audio_chunk_time = time.monotonic()
        floats = np.frombuffer(raw, dtype=np.float32)
        int16s = (np.clip(floats, -1.0, 1.0) * 32767).astype(np.int16)
        return int16s.tobytes()

    # ── WebSocket session loop ────────────────────────────────────

    def _start_ws_thread(self):
        self._ws_thread = threading.Thread(
            target=self._ws_thread_entry,
            daemon=True,
            name="qwen-livetranslate-ws",
        )
        self._ws_thread.start()

    def _ws_thread_entry(self):
        """Owns the async event loop where the WebSocket lives."""
        loop = asyncio.new_event_loop()
        self._ws_loop = loop
        try:
            loop.run_until_complete(self._ws_main())
        except Exception as e:
            if not self._stopping:
                logger.exception("Qwen WS thread fatal: %s", e)
                self.fatal_error = f"Qwen WS thread crashed: {e}"
        finally:
            try:
                loop.close()
            except Exception:
                pass
            self._ws_loop = None

    async def _ws_main(self):
        """Outer loop: connect, run, restart on disconnect.

        Health-gated like Google STT — bail out if capture_audio died.
        """
        try:
            import websockets
        except ImportError as e:
            self.fatal_error = (
                f"websockets package not installed: {e}. "
                "Run: pip install websockets"
            )
            logger.error(self.fatal_error)
            return

        while not self._stopping:
            # ── Auto-reconnect logic for capture_audio death ──
            # If capture died OR the subprocess exited, try to restart
            # it up to _MAX_CAPTURE_RECONNECTS times. macOS -3821 is
            # often transient (other app grabbed the audio source,
            # system alert, sleep wake) — a 3s delay + restart usually
            # recovers without user intervention.
            capture_dead = (
                self._capture_audio_died
                or (self._capture_process is not None
                    and self._capture_process.poll() is not None)
            )
            if capture_dead:
                # Was the prior capture session alive long enough to
                # consider its failure unrelated to the previous one?
                # If yes, reset the consecutive-failure counter.
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
                    # Used up retries — escalate to fatal.
                    self.fatal_error = (
                        f"Audio source lost: macOS stopped the capture "
                        f"stream {self._MAX_CAPTURE_RECONNECTS} times in a "
                        f"row. Possible causes: another app is using "
                        f"ScreenCaptureKit (Zoom screen-share, OBS, "
                        f"QuickTime recording), or the system denied "
                        f"audio capture. Please stop and start the "
                        f"pipeline manually."
                    )
                    logger.error("Qwen WS giving up: %s", self.fatal_error)
                    return

                self._capture_reconnect_attempts += 1
                logger.warning(
                    "capture_audio died — auto-reconnect attempt %d/%d "
                    "after %.1fs delay",
                    self._capture_reconnect_attempts,
                    self._MAX_CAPTURE_RECONNECTS,
                    self._CAPTURE_RECONNECT_DELAY_SEC,
                )
                # Tear down the dead subprocess (and its stderr thread
                # which will exit on stderr close).
                self._terminate_capture()
                # Give macOS time to release the prior SCStream. Without
                # this delay the new SCStream often fails to attach.
                # Sleep in 0.2s slices so stop() can interrupt promptly
                # — verified 2026-05-14 that a single long sleep() let
                # a stale STT instance respawn capture_audio while a new
                # pipeline was already starting (SCStreamErrorDomain -3805).
                _slept = 0.0
                _slice = 0.2
                while _slept < self._CAPTURE_RECONNECT_DELAY_SEC:
                    if self._stopping:
                        logger.info("Reconnect sleep interrupted by stop()")
                        return
                    await asyncio.sleep(_slice)
                    _slept += _slice
                if self._stopping:
                    return
                # Restart subprocess. Reset death flag — if it fails to
                # start the stderr thread will flip it back True quickly.
                self._capture_audio_died = False
                self._audio_alive = False
                try:
                    self._start_capture()
                except Exception as e:
                    logger.error("Failed to restart capture_audio: %s", e)
                    self._capture_audio_died = True
                    continue  # next iteration tries again (or escalates)
                # Continue to a fresh WS session below. The bad WS from
                # before is already closed by our send/recv tasks dying
                # when stdin closed.

            try:
                await self._run_one_session(websockets)
            except Exception as e:
                if not self._stopping:
                    # Handshake-level auth failures are fatal — retrying
                    # with the same bad key is pointless and pollutes
                    # the log (verified 2026-05-13 — log had 15 retry
                    # iterations in 30s, all 401, before user manually
                    # stopped). Detect by the websockets error class
                    # AND by the substring (different lib versions use
                    # different class hierarchies for handshake errors).
                    err_str = str(e)
                    is_auth_failure = (
                        "401" in err_str
                        or "Unauthorized" in err_str
                        or "InvalidStatus" in type(e).__name__
                        or "InvalidStatusCode" in type(e).__name__
                    )
                    is_forbidden = (
                        "403" in err_str or "Forbidden" in err_str
                    )
                    if is_auth_failure or is_forbidden:
                        self.fatal_error = (
                            "Qwen authentication failed (HTTP "
                            + ("403" if is_forbidden else "401")
                            + "). Check that:\n"
                            "  1. Your DashScope API key is valid and active\n"
                            "  2. Your account has access to the "
                            "qwen3-livetranslate-flash-realtime model\n"
                            "  3. You're using a key from the China-mainland "
                            "account (not international); both keys exist "
                            "and are not interchangeable.\n"
                            f"Server response: {err_str[:200]}"
                        )
                        logger.error("Qwen WS FATAL: %s", self.fatal_error)
                        return  # exit _ws_main entirely, no more retries
                    logger.error(
                        "Qwen WS session error: %s — retrying in 1s", e
                    )
                    await asyncio.sleep(1)

    async def _run_one_session(self, websockets):
        """One WebSocket session: connect → configure → stream PCM →
        receive translation events → close."""
        headers = [
            ("Authorization", f"Bearer {self._api_key}"),
        ]
        logger.info(
            "Qwen WS connecting (source=%s, target=%s, region=%s)",
            self._source, self._target, self._region,
        )
        # Different versions of `websockets` use different kwargs for
        # extra headers (additional_headers in 12+, extra_headers in
        # older). Try both.
        try:
            ws = await websockets.connect(
                self._api_url, additional_headers=headers,
            )
        except TypeError:
            ws = await websockets.connect(
                self._api_url, extra_headers=headers,
            )
        self._ws = ws

        try:
            # 1. session.update — set source/target lang, text-only output,
            #    enable source-language transcription for bilingual UI.
            session_update = {
                "event_id": f"ev_{int(time.time() * 1000)}",
                "type": "session.update",
                "session": {
                    "modalities": ["text"],  # don't synthesize TTS audio
                    "input_audio_format": "pcm",
                    "input_audio_transcription": {
                        "model": "qwen3-asr-flash-realtime",
                        "language": self._source,
                    },
                    "translation": {
                        "language": self._target,
                    },
                },
            }
            await ws.send(json.dumps(session_update))
            logger.info(
                "Qwen WS session.update sent (target=%s)", self._target,
            )

            # 2. Run PCM-streaming task and message-handler task in parallel.
            # Plus a "capture death watcher" so we can exit promptly when
            # capture_audio dies — without it the send_task can block in
            # subprocess.stdout.read() (the Swift subprocess doesn't always
            # close stdout immediately when ScreenCaptureKit stops), so
            # `_capture_audio_died = True` from the stderr reader never
            # gets observed and _ws_main's reconnect path never runs.
            # Verified 2026-05-14 log.2 line 632: 14-second silence after
            # `Stream stopped` until user manually pressed stop.
            send_task = asyncio.create_task(self._pcm_send_loop(ws))
            recv_task = asyncio.create_task(self._ws_recv_loop(ws))
            death_task = asyncio.create_task(self._capture_death_watcher())
            try:
                done, pending = await asyncio.wait(
                    {send_task, recv_task, death_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                # If death watcher fired, log it visibly so we can
                # confirm the reconnect path is taking over.
                if death_task in done:
                    logger.warning(
                        "Qwen WS session ending: capture_death_watcher "
                        "fired (_capture_audio_died=%s)",
                        self._capture_audio_died,
                    )
                # surface any exception
                for t in done:
                    exc = t.exception()
                    if exc:
                        raise exc
            finally:
                for t in (send_task, recv_task, death_task):
                    if not t.done():
                        t.cancel()
        finally:
            try:
                await ws.close()
            except Exception:
                pass
            self._ws = None

    async def _capture_death_watcher(self):
        """Polls _capture_audio_died every 500ms. Returns (exits) when
        the flag flips True so the asyncio.wait in _run_one_session
        unblocks and _ws_main can reach its reconnect logic.

        Necessary because _pcm_send_loop can block indefinitely in
        subprocess.stdout.read() when the Swift capture_audio process
        prints an error to stderr but doesn't close stdout immediately.
        """
        while not self._stopping and not self._capture_audio_died:
            if (
                self._capture_process is not None
                and self._capture_process.poll() is not None
            ):
                logger.warning(
                    "capture_death_watcher: subprocess exited "
                    "(returncode=%s)", self._capture_process.returncode,
                )
                self._capture_audio_died = True
                return
            await asyncio.sleep(0.5)

    async def _pcm_send_loop(self, ws):
        """Read PCM from Swift subprocess, base64-encode, send as
        input_audio_buffer.append events."""
        loop = asyncio.get_event_loop()
        while not self._stopping:
            # Read off-loop so we don't block the asyncio scheduler.
            chunk = await loop.run_in_executor(None, self._read_pcm_chunk)
            if chunk is None:
                if self._capture_audio_died:
                    logger.warning("Qwen PCM loop: capture audio died")
                    return
                await asyncio.sleep(0.01)
                continue
            event = {
                "event_id": f"ev_{int(time.time() * 1000)}",
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(chunk).decode(),
            }
            try:
                await ws.send(json.dumps(event))
            except Exception as e:
                if not self._stopping:
                    logger.warning("Qwen WS send failed: %s", e)
                return

    async def _ws_recv_loop(self, ws):
        """Handle inbound server events. Pair source transcription with
        translation, push (orig, trans, is_final) triples into the
        results queue for the pipeline to consume."""
        try:
            async for raw in ws:
                if self._stopping:
                    return
                try:
                    ev = json.loads(raw)
                except Exception as e:
                    logger.warning("Qwen WS bad JSON: %s", e)
                    continue
                etype = ev.get("type", "")

                # ── Source-language transcript (input audio recognition) ──
                # Qwen's actual event names for this still unclear — log
                # showed 0 `conversation.item.input_audio_transcription.*`
                # events. Keep the old handlers as fallback for the docs-
                # claimed schema, AND handle the empirically-observed
                # `conversation.item.created` event by extracting whatever
                # transcript-like field is present.
                if etype == "conversation.item.input_audio_transcription.text":
                    stash = ev.get("stash") or ""
                    if stash:
                        self._current_orig = stash
                        self._push((self._current_orig, self._current_trans, False))
                elif etype == "conversation.item.input_audio_transcription.completed":
                    transcript = ev.get("transcript") or ""
                    if transcript:
                        self._current_orig = transcript
                        self._push((self._current_orig, self._current_trans, False))
                elif etype == "conversation.item.created":
                    # Per Qwen log: 1 per utterance. The item field may
                    # carry transcript/audio_transcript fields once the
                    # ASR completes. Try several candidate field paths.
                    item = ev.get("item", {}) or {}
                    transcript = (
                        item.get("transcript")
                        or item.get("audio_transcript")
                        or self._extract_transcript_from_content(item.get("content"))
                        or ""
                    )
                    if transcript and transcript != self._current_orig:
                        self._current_orig = transcript
                        self._push((self._current_orig, self._current_trans, False))

                # ── Translation streaming ──
                # Empirically: Qwen sends `response.text.text` (~25-30
                # per utterance) carrying incremental translation text.
                # Field name unknown — try `text`, `delta`, then dump.
                elif etype == "response.text.text":
                    delta = (
                        ev.get("text")
                        or ev.get("delta")
                        or ev.get("content")
                        or ""
                    )
                    if delta:
                        # 2 possibilities:
                        # (a) cumulative — replace
                        # (b) incremental — append
                        # If `delta` starts with current_trans, it's
                        # cumulative; else treat as incremental delta.
                        if self._current_trans and delta.startswith(self._current_trans):
                            self._current_trans = delta  # cumulative
                        else:
                            self._current_trans += delta  # incremental
                        self._push((self._current_orig, self._current_trans, False))
                elif etype == "response.audio_transcript.text":
                    delta = ev.get("text") or ev.get("delta") or ""
                    if delta:
                        self._current_trans = delta
                        self._push((self._current_orig, self._current_trans, False))
                elif etype == "response.text.delta":
                    delta = ev.get("delta") or ""
                    if delta:
                        self._current_trans += delta
                        self._push((self._current_orig, self._current_trans, False))

                # ── Per-response completion (final translation) ──
                # `response.content_part.done` / `response.output_item.done`
                # are emitted at end of each Qwen response with full content.
                # Try to extract the final translation text + source text.
                elif etype == "response.content_part.done":
                    part = ev.get("part", {}) or {}
                    final_text = (
                        part.get("text") or part.get("transcript") or ""
                    )
                    if final_text:
                        self._current_trans = final_text
                        # Don't push True yet — wait for response.done
                        # so source-text extraction has a chance too.
                        self._push((self._current_orig, self._current_trans, False))
                elif etype == "response.output_item.done":
                    item = ev.get("item", {}) or {}
                    final_text = (
                        item.get("transcript")
                        or self._extract_transcript_from_content(item.get("content"))
                        or ""
                    )
                    if final_text:
                        # response.output_item.done carries the ASSISTANT's
                        # output (= the translation), NOT the source-language
                        # input. Earlier code had `elif not self._current_orig:
                        # self._current_orig = final_text` which leaked the
                        # translation into the orig field (verified 2026-05-14
                        # log: history entry [50] orig='我想要' trans='我想要'
                        # both Chinese). Removed — only update trans here.
                        if final_text != self._current_trans:
                            self._current_trans = final_text
                            self._push((self._current_orig, self._current_trans, False))

                # ── Final utterance boundary ──
                # IMPORTANT (2026-05-14): response.done is the ONLY
                # authoritative final-push site. Earlier code pushed
                # is_final=True from BOTH response.text.done AND
                # response.done, causing utterance_finalized to fire
                # twice per utterance (verified in log: 26 finalize
                # signals for 13 response.done events). Trust Qwen's
                # response.done as the single source of truth —
                # subsequent .text.done / .audio_transcript.done just
                # refresh _current_trans and push as non-final.
                elif etype == "response.text.done":
                    text = ev.get("text") or ""
                    if text and text != self._current_trans:
                        self._current_trans = text
                        self._push((self._current_orig, self._current_trans, False))
                elif etype == "response.audio_transcript.done":
                    text = ev.get("transcript") or ""
                    if text and text != self._current_trans:
                        self._current_trans = text
                        self._push((self._current_orig, self._current_trans, False))
                elif etype == "response.done":
                    usage = ev.get("response", {}).get("usage", {})
                    if usage:
                        logger.info("Qwen response.done usage=%s", usage)
                    # Treat response.done as authoritative utterance
                    # boundary — push current state as final, reset.
                    # Skip if both empty (e.g. system response with no text).
                    if self._current_orig or self._current_trans:
                        logger.info(
                            "Qwen FINAL push (response.done): orig=%r trans=%r",
                            (self._current_orig or "")[:60],
                            (self._current_trans or "")[:60],
                        )
                        self._push((self._current_orig, self._current_trans, True))
                    self._current_orig = ""
                    self._current_trans = ""

                # ── Speech boundaries (VAD) ──
                elif etype == "input_audio_buffer.speech_started":
                    # Server detected user start-of-speech.
                    pass
                elif etype == "input_audio_buffer.speech_stopped":
                    # Server detected user end-of-speech. response.done
                    # follows shortly with translation.
                    pass
                elif etype in ("response.created", "response.output_item.added",
                               "response.content_part.added"):
                    # Response lifecycle events — no content yet.
                    pass

                elif etype == "error":
                    err = ev.get("error", {})
                    msg = err.get("message", "unknown")
                    code = err.get("code", "unknown")
                    logger.error("Qwen server error code=%s: %s", code, msg)
                    if code in ("invalid_api_key", "unauthorized") or "401" in str(code):
                        self.fatal_error = f"Qwen auth error: {msg}"
                        return
                elif etype in ("session.created", "session.updated"):
                    logger.debug("Qwen WS: %s", etype)
                else:
                    # Unknown event — dump full JSON at INFO so we can
                    # learn the actual schema from a real run.
                    # 2026-05-14: prior run showed `response.text.text` as
                    # the actual streaming event (now handled above);
                    # this branch catches any other surprises.
                    try:
                        payload = json.dumps(ev, ensure_ascii=False)[:500]
                    except Exception:
                        payload = "<unserializable>"
                    logger.info("Qwen WS unknown event %s: %s", etype, payload)
        except Exception as e:
            if not self._stopping:
                logger.warning("Qwen WS recv loop ended: %s", e)

    @staticmethod
    def _extract_transcript_from_content(content) -> str | None:
        """Search nested 'content' arrays for a transcript-like string.
        Qwen / OpenAI-Realtime-style schemas often have content shaped
        like [{"type": "text", "text": "..."}] or [{"transcript": "..."}].
        """
        if not content:
            return None
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                for k in ("transcript", "text", "audio_transcript"):
                    v = part.get(k)
                    if isinstance(v, str) and v:
                        return v
        elif isinstance(content, dict):
            for k in ("transcript", "text", "audio_transcript"):
                v = content.get(k)
                if isinstance(v, str) and v:
                    return v
        return None

    def _push(self, item: tuple[str, str, bool]):
        try:
            self._results_queue.put_nowait(item)
        except queue.Full:
            try:
                self._results_queue.get_nowait()
            except queue.Empty:
                pass
            self._results_queue.put_nowait(item)

    # ── STT public API ────────────────────────────────────────────

    async def next_translation(self) -> tuple[str, str, bool] | None:
        if self._stopping:
            return None
        loop = asyncio.get_event_loop()
        try:
            item = await loop.run_in_executor(
                None, lambda: self._results_queue.get(timeout=1.0)
            )
            return item
        except queue.Empty:
            return None
        except Exception:
            return None

    async def drain_translations(self) -> list[tuple[str, str, bool]]:
        if self._stopping:
            return []
        items = []
        # Block for first one (mirrors Google STT drain).
        first = await self.next_translation()
        if first is None:
            return []
        items.append(first)
        # Non-blocking drain after that.
        while True:
            try:
                items.append(self._results_queue.get_nowait())
            except queue.Empty:
                break
        return items

    async def stop(self) -> None:
        self._stopping = True
        # Close WS via the event loop it lives on.
        if self._ws_loop is not None and self._ws is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    self._ws.close(), self._ws_loop,
                )
                fut.result(timeout=1.0)
            except Exception:
                pass
        # Terminate Swift subprocess.
        if self._capture_process is not None:
            with _active_processes_lock:
                _active_processes.discard(self._capture_process)
            try:
                self._capture_process.terminate()
                try:
                    self._capture_process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self._capture_process.kill()
            except Exception as e:
                logger.warning("Failed to terminate capture_audio: %s", e)
            self._capture_process = None
        # Join WS thread.
        if self._ws_thread is not None and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=2.0)
        logger.info("QwenLiveTranslateSTT stopped")
