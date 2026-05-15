"""Qwen ASR (DashScope) realtime speech-recognition engine.

Why this exists (2026-05-14): user wants to use Qwen's ASR + a separate
Qwen translator (qwen-mt-turbo) in the SAME architecture as Google STT
+ Gemini — two independent steps instead of the end-to-end Qwen
LiveTranslate path. This file is the STT half.

Differences from qwen_livetranslate.py:
- Uses qwen3-asr-flash-realtime (ASR only, no translation)
- Implements the standard next_transcript() / drain_transcripts()
  interface (same as Google STT), NOT next_translation()
- provides_translation = False  (pipeline runs its own translator)
- Cheaper: $0.00009/sec ≈ ¼ Google STT pricing

Architecture mirrors qwen_livetranslate:
- WebSocket to dashscope-intl.aliyuncs.com (intl) or dashscope.aliyuncs.com (cn)
- Bearer-token auth via DASHSCOPE_API_KEY
- Reuses Swift capture_audio subprocess for raw PCM @ 16kHz mono
- Auto-reconnect on capture death (up to 3 attempts with 3s delay)

Protocol (per https://www.alibabacloud.com/help/en/model-studio/qwen-real-time-speech-recognition):
- session.update with input_audio_transcription.language + turn_detection.type=server_vad
- input_audio_buffer.append carries base64 PCM
- conversation.item.input_audio_transcription.text  → interim (field: text)
- conversation.item.input_audio_transcription.completed  → final (field: transcript)
- input_audio_buffer.speech_started / speech_stopped → VAD boundaries
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
# API keys are NOT interchangeable across regions (intl key gets HTTP 401
# from cn endpoint, vice versa). Verified 2026-05-13 in qwen_livetranslate.
_API_URLS = {
    "intl": (
        "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime"
        "?model=qwen3-asr-flash-realtime"
    ),
    "cn": (
        "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
        "?model=qwen3-asr-flash-realtime"
    ),
}

# Map our short codes to Qwen-expected language identifiers.
# qwen3-asr-flash supported langs per docs: Chinese, English, Japanese,
# German, Korean, Russian, French, Portuguese, Arabic, Italian, Spanish,
# Hindi, Indonesian, Thai, Turkish, Ukrainian, Vietnamese, Czech, Danish,
# Filipino, Finnish, Icelandic, Malay, Norwegian, Polish, Swedish.
_LANG_TO_QWEN = {
    "en": "en",
    "zh": "zh",
    "zh-tw": "zh",
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
    "yue": "yue",
}


class QwenASRSTT(STTEngine):
    """DashScope qwen3-asr-flash-realtime engine. ASR only — pipeline
    runs a separate translator (Gemini / qwen-mt / etc.) on top, same
    architecture as Google Streaming STT.
    """

    is_self_contained = True  # owns audio capture
    # NOT provides_translation — pipeline will instantiate a translator.

    # Auto-reconnect tuning — same as qwen_livetranslate / google_streaming.
    _MAX_CAPTURE_RECONNECTS: int = 3
    _CAPTURE_RECONNECT_DELAY_SEC: float = 3.0
    _CAPTURE_HEALTHY_RESET_SEC: float = 30.0

    def __init__(
        self,
        api_key: str,
        language: str = "en",
        region: str = "intl",
    ):
        _t_init0 = time.monotonic()
        if not api_key:
            raise RuntimeError(
                "DashScope API key required for Qwen ASR. Set "
                "DASHSCOPE_API_KEY env var, or enter it in the Control "
                "Panel under API Keys."
            )
        if region not in _API_URLS:
            logger.warning(
                "Unknown DashScope region %r, falling back to 'intl'",
                region,
            )
            region = "intl"
        self._api_key = api_key
        self._language = _LANG_TO_QWEN.get(language, language)
        self._region = region
        self._api_url = _API_URLS[region]

        # Subprocess + threads + queue. Queue value is (text, is_final)
        # to match Google STT's interface.
        self._capture_process: subprocess.Popen | None = None
        self._results_queue: queue.Queue[tuple[str, bool]] = queue.Queue(maxsize=200)
        self._stopping = False
        self._ws_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None

        # Per-utterance partial accumulator — concatenate streamed
        # `.text` events until the matching `.completed` fires.
        self._current_text: str = ""

        # Health flags (same shape as Google STT).
        self._capture_audio_died: bool = False
        self._audio_alive: bool = False
        self._last_audio_chunk_time: float = 0.0
        self.fatal_error: str | None = None
        self._capture_reconnect_attempts: int = 0
        self._capture_started_at: float = 0.0

        # WS event-loop bookkeeping.
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._ws = None

        _t_fields = time.monotonic()
        self._start_capture()
        _t_cap = time.monotonic()
        self._start_ws_thread()
        _t_ws = time.monotonic()
        logger.info(
            "QwenASR init steps (ms): fields=%.0f  _start_capture=%.0f  "
            "_start_ws_thread=%.0f  TOTAL=%.0f",
            (_t_fields - _t_init0) * 1000,
            (_t_cap - _t_fields) * 1000,
            (_t_ws - _t_cap) * 1000,
            (_t_ws - _t_init0) * 1000,
        )

    @property
    def name(self) -> str:
        return "QwenASR"

    async def transcribe(self, audio):
        # Self-contained engine — chunk-based interface is a no-op.
        return None

    # ── audio capture (raw PCM from Swift helper) ─────────────────

    def _start_capture(self):
        binary = _ensure_capture_binary()
        logger.info("Starting capture_audio subprocess for Qwen ASR")
        self._capture_process = subprocess.Popen(
            [str(binary)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        with _active_processes_lock:
            _active_processes.add(self._capture_process)
        self._capture_started_at = time.monotonic()
        self._stderr_thread = threading.Thread(
            target=self._read_capture_stderr,
            daemon=True,
            name="qwen-asr-stderr",
        )
        self._stderr_thread.start()

    def _terminate_capture(self):
        """Tear down the current capture_audio subprocess cleanly.
        Idempotent — safe between auto-reconnect attempts."""
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
        _DEATH = ("Stream stopped", "Stream was stopped")
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
                    logger.info("capture_audio ready (Qwen ASR audio flowing)")
                    self._audio_alive = True
                elif any(m in line for m in _DEATH):
                    # Don't set fatal_error here — _ws_main auto-reconnects.
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
            self._capture_audio_died = True
            self._audio_alive = False

    def _read_pcm_chunk(self, max_bytes: int = 6400) -> bytes | None:
        """Read float32 PCM from Swift, convert to int16 LINEAR16.
        Qwen accepts pcm (16-bit signed, mono, 16kHz)."""
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
            name="qwen-asr-ws",
        )
        self._ws_thread.start()

    def _ws_thread_entry(self):
        loop = asyncio.new_event_loop()
        self._ws_loop = loop
        try:
            loop.run_until_complete(self._ws_main())
        except Exception as e:
            if not self._stopping:
                logger.exception("QwenASR WS thread fatal: %s", e)
                self.fatal_error = f"QwenASR WS thread crashed: {e}"
        finally:
            try:
                loop.close()
            except Exception:
                pass
            self._ws_loop = None

    async def _ws_main(self):
        """Outer loop: connect → run session → reconnect on disconnect or
        capture death. Mirrors qwen_livetranslate._ws_main + the same
        auto-reconnect path on capture_audio death."""
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
            # Auto-reconnect: same logic as qwen_livetranslate.
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
                        f"stream {self._MAX_CAPTURE_RECONNECTS} times in "
                        f"a row. Please stop and start the pipeline "
                        f"manually."
                    )
                    logger.error("QwenASR WS giving up: %s", self.fatal_error)
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
                # Sleep in slices so stop() can interrupt promptly —
                # see qwen_livetranslate.py for the rationale.
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
                self._capture_audio_died = False
                self._audio_alive = False
                try:
                    self._start_capture()
                except Exception as e:
                    logger.error("Failed to restart capture_audio: %s", e)
                    self._capture_audio_died = True
                    continue

            try:
                await self._run_one_session(websockets)
            except Exception as e:
                if not self._stopping:
                    err_str = str(e)
                    is_auth_failure = (
                        "401" in err_str
                        or "Unauthorized" in err_str
                        or "InvalidStatus" in type(e).__name__
                        or "InvalidStatusCode" in type(e).__name__
                    )
                    is_forbidden = "403" in err_str or "Forbidden" in err_str
                    if is_auth_failure or is_forbidden:
                        self.fatal_error = (
                            "Qwen authentication failed (HTTP "
                            + ("403" if is_forbidden else "401")
                            + "). Check that:\n"
                            "  1. Your DashScope API key is valid and active\n"
                            "  2. You're using the key matching the "
                            "configured region (intl vs cn — keys are "
                            "NOT interchangeable)\n"
                            f"Server response: {err_str[:200]}"
                        )
                        logger.error("QwenASR WS FATAL: %s", self.fatal_error)
                        return
                    logger.error(
                        "QwenASR WS session error: %s — retrying in 1s", e
                    )
                    await asyncio.sleep(1)

    async def _run_one_session(self, websockets):
        """One WebSocket session: connect → configure → stream PCM →
        receive transcripts → close."""
        headers = [
            ("Authorization", f"Bearer {self._api_key}"),
            ("OpenAI-Beta", "realtime=v1"),
        ]
        logger.info(
            "QwenASR WS connecting (lang=%s, region=%s)",
            self._language, self._region,
        )
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
            # session.update — configure ASR session.
            # NOTE (2026-05-14): the previous version of this config was
            # missing the `model` field inside input_audio_transcription
            # and explicitly set turn_detection.threshold=0.0 — together
            # these silenced the ASR pipeline entirely (Qwen accepted
            # the session but emitted no transcripts during a 50s test).
            # Mirroring the working Qwen LiveTranslate config:
            # - input_audio_transcription needs both model + language
            # - omit turn_detection so server uses its default VAD
            #   (verified in qwen_livetranslate.py — same pattern works)
            session_update = {
                "event_id": f"ev_{int(time.time() * 1000)}",
                "type": "session.update",
                "session": {
                    "modalities": ["text"],
                    "input_audio_format": "pcm",
                    "input_audio_transcription": {
                        "model": "qwen3-asr-flash-realtime",
                        "language": self._language,
                    },
                },
            }
            await ws.send(json.dumps(session_update))
            logger.info(
                "QwenASR WS session.update sent (lang=%s)", self._language,
            )

            # See qwen_livetranslate.py for the rationale on death_task:
            # _pcm_send_loop can block in subprocess.stdout.read() after
            # capture death, preventing the reconnect path from running.
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
                if death_task in done:
                    logger.warning(
                        "QwenASR WS session ending: capture_death_watcher "
                        "fired (_capture_audio_died=%s)",
                        self._capture_audio_died,
                    )
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
        """Poll _capture_audio_died every 500ms. See qwen_livetranslate.py
        for the rationale — subprocess.stdout.read() can block forever
        after Swift prints an error to stderr, so we need a separate
        coroutine that wakes asyncio.wait() when the death flag flips.
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
            chunk = await loop.run_in_executor(None, self._read_pcm_chunk)
            if chunk is None:
                if self._capture_audio_died:
                    logger.warning("QwenASR PCM loop: capture audio died")
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
                    logger.warning("QwenASR WS send failed: %s", e)
                return

    async def _ws_recv_loop(self, ws):
        """Handle server events: VAD boundaries + transcripts."""
        try:
            async for raw in ws:
                if self._stopping:
                    return
                try:
                    ev = json.loads(raw)
                except Exception as e:
                    logger.warning("QwenASR WS bad JSON: %s", e)
                    continue
                etype = ev.get("type", "")

                if etype == "conversation.item.input_audio_transcription.text":
                    # Interim transcript — try multiple field names since
                    # the protocol varies between SDK / raw WS forms.
                    text = (
                        ev.get("text")
                        or ev.get("stash")
                        or ev.get("transcript")
                        or ""
                    )
                    if text and text != self._current_text:
                        self._current_text = text
                        self._push((text, False))
                elif etype == "conversation.item.input_audio_transcription.completed":
                    transcript = ev.get("transcript") or ev.get("text") or ""
                    if transcript:
                        logger.info(
                            "QwenASR FINAL (transcription.completed): %r",
                            transcript[:80],
                        )
                        self._push((transcript, True))
                    self._current_text = ""
                elif etype == "conversation.item.created":
                    # Empirical fallback path: Qwen LiveTranslate's log
                    # showed that for that endpoint, source-language
                    # transcripts arrived via conversation.item.created
                    # rather than via input_audio_transcription.*. Try
                    # the same extraction here in case ASR endpoint
                    # behaves the same way.
                    item = ev.get("item", {}) or {}
                    transcript = (
                        item.get("transcript")
                        or item.get("audio_transcript")
                        or self._extract_transcript_from_content(item.get("content"))
                        or ""
                    )
                    if transcript and transcript != self._current_text:
                        self._current_text = transcript
                        # item.created is usually emitted at end of utterance,
                        # so treat as final unless we see clear evidence
                        # otherwise.
                        logger.info(
                            "QwenASR FINAL (item.created): %r",
                            transcript[:80],
                        )
                        self._push((transcript, True))
                        self._current_text = ""
                elif etype == "input_audio_buffer.speech_started":
                    logger.info("QwenASR VAD: speech_started")
                elif etype == "input_audio_buffer.speech_stopped":
                    logger.info("QwenASR VAD: speech_stopped")
                elif etype == "input_audio_buffer.committed":
                    logger.info("QwenASR VAD: committed")
                elif etype == "session.created":
                    logger.info("QwenASR session.created")
                elif etype == "session.updated":
                    logger.info("QwenASR session.updated")
                elif etype == "session.finished":
                    logger.info("QwenASR session.finished")
                    return
                elif etype == "error":
                    err = ev.get("error", {})
                    msg = err.get("message", "unknown")
                    code = err.get("code", "unknown")
                    logger.error("QwenASR server error code=%s: %s", code, msg)
                    if code in ("invalid_api_key", "unauthorized") or "401" in str(code):
                        self.fatal_error = f"QwenASR auth error: {msg}"
                        return
                else:
                    # Surface unknown events for protocol debugging.
                    # Until we've confirmed the actual event schema,
                    # dump the full payload at INFO so the next test
                    # run can grep these and identify the real names.
                    try:
                        payload = json.dumps(ev, ensure_ascii=False)[:500]
                    except Exception:
                        payload = "<unserializable>"
                    logger.info("QwenASR unknown event %s: %s", etype, payload)
        except Exception as e:
            if not self._stopping:
                logger.warning("QwenASR recv loop error: %s", e)

    @staticmethod
    def _extract_transcript_from_content(content) -> str:
        """Pull text from a Qwen `item.content` field that's either a
        plain string, a list of dicts with {type, text, transcript}, or
        a dict. Mirrors the same helper in qwen_livetranslate.py.
        """
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict):
                    txt = (
                        c.get("text")
                        or c.get("transcript")
                        or c.get("audio_transcript")
                        or ""
                    )
                    if txt:
                        parts.append(txt)
            return "".join(parts)
        if isinstance(content, dict):
            return (
                content.get("text")
                or content.get("transcript")
                or content.get("audio_transcript")
                or ""
            )
        return ""

    def _push(self, item: tuple[str, bool]):
        """Push a (text, is_final) tuple to the results queue.
        Drops the oldest item if queue is full (rare but defensive)."""
        try:
            self._results_queue.put_nowait(item)
        except queue.Full:
            try:
                self._results_queue.get_nowait()
            except queue.Empty:
                pass
            self._results_queue.put_nowait(item)

    # ── STT public API ────────────────────────────────────────────

    async def next_transcript(self) -> tuple[str, bool] | None:
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
            if "cannot schedule new futures after shutdown" in str(e):
                return None
            raise

    async def drain_transcripts(self) -> list[tuple[str, bool]]:
        if self._stopping:
            return []
        results: list[tuple[str, bool]] = []
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
        while True:
            try:
                results.append(self._results_queue.get_nowait())
            except queue.Empty:
                break
        return results

    async def stop(self) -> None:
        self._stopping = True
        # Close WS via its own event loop.
        if self._ws_loop is not None and self._ws is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    self._ws.close(), self._ws_loop,
                )
                fut.result(timeout=1.0)
            except Exception:
                pass
        # Terminate Swift subprocess.
        self._terminate_capture()
        # Join WS thread.
        if self._ws_thread is not None and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=2.0)
        logger.info("QwenASRSTT stopped")
