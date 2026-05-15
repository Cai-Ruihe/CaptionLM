"""Core caption pipeline: Audio → STT → Translation → Display.

Runs entirely in a dedicated background thread with its own asyncio
event loop. Communicates with the Qt UI via thread-safe Qt Signals.
This avoids the fragile QTimer-based async pump that caused bus errors.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass

import numpy as np

from PySide6.QtCore import QObject, Signal

from captionlm.audio.base import AudioCapture
from captionlm.stt.base import STTEngine
from captionlm.translation.base import Translator, TranslationResult
from captionlm.config.settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class SubtitleEntry:
    """A single subtitle entry with original and translated text."""
    timestamp: float
    original: str
    translated: str
    source_lang: str
    target_lang: str
    provider: str
    cost_usd: float = 0.0


class CaptionPipeline(QObject):
    """Orchestrates the Audio → STT → Translation → Display pipeline.

    Runs in a dedicated background thread. Qt Signals are thread-safe,
    so subtitle_ready can be emitted from the worker thread and received
    on the Qt main thread.
    """

    subtitle_ready = Signal(str, str)  # (original_text, translated_text)
    # Emitted when STT reports is_final=True — the current utterance is
    # acoustically complete (silence detected). Overlay uses this as the
    # authoritative "commit current to history NOW" signal so back-and-forth
    # dialogue gets one history entry per turn even when turns are < 2s apart.
    # Subscribers should treat this as "the previous subtitle_ready emission
    # was the final form of that utterance".
    utterance_finalized = Signal()
    error_occurred = Signal(str)
    rate_limit_warning = Signal(str)  # provider name that hit rate limit
    # Cumulative session totals — re-emitted after every translation
    # so the settings panel can show a live cost meter. Tokens are
    # estimated (rough proxy for input + output characters), cost in USD.
    session_usage_updated = Signal(int, float)  # (total_tokens, total_cost_usd)
    # Retranslation polish — emitted after a previously committed
    # utterance has been re-translated with newer context. Overlay
    # listens and updates the matching history entry in place.
    # Args: (orig_text_used_as_key, new_translation).
    translation_updated = Signal(str, str)
    # Audio source health for the overlay heartbeat indicator.
    # State is one of: "alive" (audio flowing within last 2s),
    # "idle" (no audio for >2s but subprocess is healthy — silence),
    # "dead" (capture_audio subprocess died, fatal). Emitted at most
    # ~1Hz from the pipeline's 2s usage tick so the UI dot can update.
    audio_health = Signal(str)

    MAX_CONTEXT = 8  # number of (orig, trans) pairs sent as context to LLM
    # Bumped from 5 → 8 per user feedback: short transcripts need more
    # surrounding context for the LLM to produce a coherent translation.

    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.is_running = False

        self._audio: AudioCapture | None = None
        self._stt: STTEngine | None = None
        self._translator: Translator | None = None
        self._context: list[tuple[str, str]] = []
        self._history: list[SubtitleEntry] = []
        # Cumulative session totals for the cost meter in the settings
        # panel. Reset to 0 on start(). Each successful translation
        # adds tokens_used + cost_usd to the TRANSLATION subtotal.
        # STT cost is computed on the fly from elapsed session time
        # (Google STT bills by streamed audio seconds, ~$0.024/min,
        # accumulates whether or not anyone is speaking).
        self._session_tokens: int = 0
        self._session_translation_cost: float = 0.0
        # ~$0.024 per minute = 0.0004 USD/sec. Source: Google Cloud
        # Speech-to-Text streaming pricing page. Update if you ever
        # switch model tier or region.
        self._GOOGLE_STT_USD_PER_SEC: float = 0.024 / 60.0
        # Live-tick timer so the meter advances during silence too —
        # otherwise STT cost only updates when a translation lands.
        from PySide6.QtCore import QTimer as _QTimer
        self._usage_tick_timer = _QTimer(self)
        self._usage_tick_timer.setInterval(2000)  # every 2 seconds
        self._usage_tick_timer.timeout.connect(self._emit_session_usage)

        # ── Retranslation polish buffer ──────────────────────────────
        # Document-level NMT research (Voita+2019, Maruf+2021) shows
        # 3-5 sentence context is the sweet spot. We pass 8 prior
        # pairs already, so going DEEPER won't help much. What DOES
        # help is using FUTURE context for completed utterances —
        # disambiguates pronouns / lexical choice / ellipsis that
        # were under-determined at first-pass time.
        #
        # Approach: after each is_final commit, retranslate the prior
        # 1-2 utterances with the now-richer context (which includes
        # the new utterance). Capped at 3 total versions per utterance
        # (v1 initial, v2 with +1 future, v3 with +2 future). 3 API
        # calls total per utterance — user explicitly accepted this
        # cost trade-off for quality.
        self._RETRANS_MAX_VERSIONS: int = 3
        self._RETRANS_BUFFER_SIZE: int = 12
        self._retrans_buffer: list[dict] = []  # [{orig, trans, version}]
        # Tracked across the flush_and_translate closure so the
        # is_final commit block can find the latest pair.
        self._last_committed_orig: str = ""
        self._last_committed_trans: str = ""
        # Session recorder: parallel store fed alongside _history. On stop()
        # we auto-export an SRT so the user can review every translation
        # produced during the session — useful for QA on real-world videos
        # where you can't eyeball every subtitle as it scrolls past.
        from captionlm.recorder.base import SessionRecorder
        self._recorder = SessionRecorder()
        self._session_started_at: float = 0.0  # set in start()
        self._session_saved = False  # idempotency guard for SRT export
        self._thread: threading.Thread | None = None

        # Register atexit fallback: if the process is killed via SIGTERM
        # (autotest's `kill $PID`, supervisor stop, etc.) Qt's quit path
        # never runs, so pipeline.stop() never runs, so SRT never saves.
        # The atexit handler ensures the SRT is written even on abrupt exits.
        # The SIGTERM→sys.exit handler in __main__ converts the signal into
        # a normal Python exit, which DOES fire atexit.
        import atexit as _atexit
        _atexit.register(self._auto_save_session_safe)

    def start(self):
        """Start the pipeline — returns immediately, all init runs in background.

        This is critical: STT model loading (Whisper download/load) and
        translator init (google.generativeai import) can take seconds to minutes.
        Running them on the main thread would freeze the UI.
        """
        if self.is_running:
            return

        self.is_running = True
        # Reset recorder state for a fresh session and stamp wallclock start.
        # Why: session SRT files are named with this timestamp so the user can
        # tell sessions apart. Also _recorder._start_time gets set by the
        # first add_entry call, but having an explicit pipeline-side stamp
        # makes the filename deterministic even if no entries are recorded.
        self._recorder.clear()
        self._session_started_at = time.time()
        self._session_saved = False  # allow new save on this fresh session
        self._session_tokens = 0
        self._session_translation_cost = 0.0
        self.session_usage_updated.emit(0, 0.0)
        self._usage_tick_timer.start()
        # Fresh retranslation state for the new session.
        self._retrans_buffer = []
        self._last_committed_orig = ""
        self._last_committed_trans = ""
        self._thread = threading.Thread(
            target=self._thread_main,
            daemon=True,
            name="pipeline-worker",
        )
        self._thread.start()

    def stop(self):
        """Stop the pipeline and clean up resources."""
        self.is_running = False
        # Pause the usage meter — STT is no longer billing for audio.
        try:
            self._usage_tick_timer.stop()
        except Exception:
            pass

        # Auto-export the session as SRT BEFORE we tear anything else down,
        # so even if subsequent stop steps raise we still have the user's
        # subtitle history saved to disk. Failures here are non-fatal — the
        # user shouldn't lose subtitles to an export bug.
        try:
            self._auto_save_session()
        except Exception as e:
            logger.warning("Session auto-save failed: %s", e)

        if self._audio:
            try:
                self._audio.stop()
            except Exception as e:
                logger.warning("Audio stop error: %s", e)

        # For self-contained STT (e.g. SpeechAnalyzer) we must terminate
        # the Swift subprocess explicitly. Run synchronously since stop()
        # is called from the Qt main thread and we don't have an event loop.
        if self._stt is not None and getattr(self._stt, "is_self_contained", False):
            try:
                # The engine's stop is async but trivially fast (just a kill)
                import asyncio as _asyncio
                _asyncio.run(self._stt.stop())
            except Exception as e:
                logger.warning("STT stop error: %s", e)

        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

        logger.info("Pipeline stopped")

    # ──────────────────────────────────────────────────────────────
    # Retranslation polish
    # ──────────────────────────────────────────────────────────────

    def _add_to_retrans_buffer(self, orig: str, trans: str) -> None:
        """Append a freshly-committed utterance to the retranslation
        buffer. Bounded size to prevent unbounded re-translation work
        on long sessions."""
        if not orig:
            return
        # Don't re-add if same orig already at the tail (e.g. utterance
        # finalized twice via different code paths).
        if (
            self._retrans_buffer
            and self._retrans_buffer[-1].get("orig") == orig
        ):
            return
        self._retrans_buffer.append({
            "orig": orig,
            "trans": trans,
            "version": 1,
        })
        if len(self._retrans_buffer) > self._RETRANS_BUFFER_SIZE:
            self._retrans_buffer = self._retrans_buffer[-self._RETRANS_BUFFER_SIZE:]

    async def _do_retranslations(self) -> None:
        """Background polish pass: re-translate prior utterances with
        the now-available future context.

        Targets the LAST 2 PRIOR entries (index -3 and -2). The most
        recent entry (-1) is the one just committed — it has no future
        context yet, so we can't polish it. Earlier than -3 are
        already stable (multiple futures elapsed; further polishing
        gives marginal returns per Voita 2019 / Maruf 2021).

        Each utterance capped at _RETRANS_MAX_VERSIONS (=3) total:
          v1 (initial) → v2 (with +1 future) → v3 (with +2 futures).
        After that, no more retranslation calls — bounds cost.
        """
        if not self._translator or len(self._retrans_buffer) < 2:
            return
        # Look at the second-to-last and third-to-last entries.
        # (The last is the one just committed; it has no future.)
        eligible_idx = list(range(max(0, len(self._retrans_buffer) - 3),
                                  len(self._retrans_buffer) - 1))
        for idx in eligible_idx:
            entry = self._retrans_buffer[idx]
            if entry["version"] >= self._RETRANS_MAX_VERSIONS:
                continue
            # Build context from surrounding entries (excluding this one).
            past = self._retrans_buffer[max(0, idx - 3):idx]
            future = self._retrans_buffer[idx + 1:idx + 3]
            ctx = [(e["orig"], e["trans"]) for e in past + future]
            try:
                result = await self._translator.translate(
                    text=entry["orig"],
                    source_lang=self.settings.source_lang,
                    target_lang=self.settings.target_lang,
                    context=ctx,
                )
                if result.text and not result.text.startswith("[Error]") \
                        and not result.text.startswith("[Gemini error]"):
                    old_trans = entry["trans"]
                    entry["trans"] = result.text
                    entry["version"] += 1
                    if result.text != old_trans:
                        logger.info(
                            "Retrans v%d: %s\n  v1: %s\n  vN: %s",
                            entry["version"], entry["orig"][:40],
                            old_trans[:60], result.text[:60],
                        )
                        # Notify overlay to update the history entry.
                        self.translation_updated.emit(entry["orig"], result.text)
                    # Also bump session cost since this used the API.
                    self._session_tokens += int(getattr(result, "tokens_used", 0))
                    self._session_translation_cost += float(getattr(result, "cost_usd", 0.0))
                    self._emit_session_usage()
            except Exception as e:
                logger.debug("Retranslation skipped (err: %s)", e)

    def _emit_session_usage(self) -> None:
        """Compute combined STT + translation cost for the current
        session and emit session_usage_updated.

        STT cost: Google Cloud Speech-to-Text streaming bills by the
        duration of audio streamed (not by transcript count). Once the
        pipeline is running, the mic capture is continuous, so we just
        multiply elapsed wallclock time since start() by the per-second
        rate. If the user pauses (Stop button) we stop the tick timer
        and the meter freezes at the last value.

        Note: this overestimates slightly if the audio source is
        actually quiet long enough that Google ends the streaming
        session and restarts (it might re-bill the restart). Close
        enough for a "how much did this video cost me" display.
        """
        if not self._session_started_at:
            return
        elapsed_sec = max(0.0, time.time() - self._session_started_at)
        stt_cost = elapsed_sec * self._GOOGLE_STT_USD_PER_SEC
        total = self._session_translation_cost + stt_cost
        self.session_usage_updated.emit(self._session_tokens, total)

        # Audio health heartbeat — emitted on the same 2s tick so the
        # overlay's status dot can refresh. Computed from the STT
        # engine's flags + last-chunk-time.
        try:
            stt = self._stt
            if stt is None:
                state = "idle"
            elif getattr(stt, "fatal_error", None) or getattr(stt, "_capture_audio_died", False):
                state = "dead"
            else:
                last_t = float(getattr(stt, "_last_audio_chunk_time", 0.0) or 0.0)
                if last_t == 0.0:
                    state = "idle"
                else:
                    # Use monotonic clock — _last_audio_chunk_time
                    # comes from STT's _read_pcm_chunk which uses
                    # time.monotonic(). 3s grace before going to idle.
                    age = time.monotonic() - last_t
                    state = "alive" if age < 3.0 else "idle"
            self.audio_health.emit(state)
        except Exception as e:
            logger.debug("audio_health emit failed: %s", e)

    def _auto_save_session(self) -> None:
        """Auto-export the session as a bilingual SRT on stop.

        Output path: ~/Documents/CaptionLM/sessions/{ISO-timestamp}-{src}-{tgt}.srt

        Why ~/Documents/CaptionLM and not the project tree: keeps user data
        separate from source so it doesn't pollute git, AND so the user can
        find their files in a stable location regardless of where the
        project is checked out. Documents is iCloud-synced by default on
        macOS, which is fine for SRT files (small, useful to sync).

        Idempotent: a second call (from atexit fallback after stop()
        already ran) is a no-op.
        """
        if self._session_saved:
            return
        if self._recorder.entry_count == 0:
            logger.info("No subtitles recorded this session; skipping SRT export")
            self._session_saved = True
            return

        from datetime import datetime
        from pathlib import Path

        sessions_dir = Path.home() / "Documents" / "CaptionLM" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        # Filename: 2026-05-05-114200-ja-zh.srt
        # Use the wallclock start so the file is named after WHEN the user
        # started watching, not when they stopped (which is usually random).
        ts = datetime.fromtimestamp(self._session_started_at or 0).strftime(
            "%Y-%m-%d-%H%M%S"
        )
        src = self.settings.source_lang or "src"
        tgt = self.settings.target_lang or "tgt"
        srt_path = sessions_dir / f"{ts}-{src}-{tgt}.srt"

        n_written = self._recorder.export_srt(str(srt_path), bilingual=True)
        logger.info(
            "Session SRT auto-saved: %s (%d entries)",
            srt_path, n_written,
        )
        self._session_saved = True

    def _auto_save_session_safe(self) -> None:
        """atexit-safe wrapper: catches all exceptions so atexit doesn't
        crash on cleanup paths."""
        try:
            self._auto_save_session()
        except Exception as e:
            # logger may already be torn down at this point in atexit chain;
            # use print as a last resort so we at least see the failure.
            try:
                logger.warning("atexit SRT save failed: %s", e)
            except Exception:
                print(f"[CaptionLM] atexit SRT save failed: {e}")

    def _thread_main(self):
        """Background thread entry point — initializes components, then runs loop.

        ALL heavy initialization (audio capture, STT model, translator)
        runs IN PARALLEL on three sub-threads. Total perceived wait =
        max(audio, stt, translator) instead of sum. Since audio capture
        is typically the longest (10-15s for ScreenCaptureKit cold start),
        STT and translator init "for free" inside that window.

        Live progress is emitted to the overlay every 0.5s so the user
        sees what's still pending and elapsed time.
        """
        try:
            t0 = time.monotonic()
            self.subtitle_ready.emit("Starting...", "初始化中...")

            # Determine if STT will be self-contained. We need to peek at the
            # engine name BEFORE instantiation so we know whether to skip the
            # audio init thread (the Swift binary handles capture itself).
            # CRITICAL: keep this list in sync with engines that set
            # is_self_contained=True. Otherwise pipeline starts a duplicate
            # ScreenCaptureKit instance that fights with the STT's own one
            # (empirical: "Stream stopped: application connection interrupted").
            _SELF_CONTAINED_ENGINES = {
                "speech_analyzer", "google_streaming", "qwen_livetranslate",
                "qwen_asr",
            }
            is_self_contained_stt = self.settings.stt_engine in _SELF_CONTAINED_ENGINES

            # State for parallel init — populated by sub-threads, errors collected
            init_state = {
                "audio_done": is_self_contained_stt,  # Skip if self-contained
                "audio_err": None, "audio_t": 0.0,
                "stt_done":   False, "stt_err":   None, "stt_t":   0.0,
                "trans_done": False, "trans_err": None, "trans_t": 0.0,
            }

            def _init_audio():
                t = time.monotonic()
                try:
                    self._audio = self._create_audio_capture()
                    self._audio.start()
                    # Wait for actual readiness (not just object construction)
                    if hasattr(self._audio, "_ready_event"):
                        self._audio._ready_event.wait(timeout=60)
                    init_state["audio_t"] = time.monotonic() - t
                    init_state["audio_done"] = True
                except Exception as e:
                    init_state["audio_err"] = e
                    init_state["audio_done"] = True

            def _init_stt():
                t = time.monotonic()
                try:
                    self._stt = self._create_stt_engine()
                    init_state["stt_t"] = time.monotonic() - t
                    init_state["stt_done"] = True
                except Exception as e:
                    init_state["stt_err"] = e
                    init_state["stt_done"] = True

            # Skip translator init when the chosen STT engine produces
            # translations end-to-end (Qwen LiveTranslate). The pipeline's
            # provides_translation branch reads (orig, trans) pairs from
            # the STT directly — no separate translator API call.
            _engine_provides_trans = self.settings.stt_engine == "qwen_livetranslate"

            def _init_trans():
                if _engine_provides_trans:
                    init_state["trans_t"] = 0.0
                    init_state["trans_done"] = True
                    return
                t = time.monotonic()
                try:
                    self._translator = self._create_translator()
                    init_state["trans_t"] = time.monotonic() - t
                    init_state["trans_done"] = True
                except Exception as e:
                    init_state["trans_err"] = e
                    init_state["trans_done"] = True

            # Only start audio init if STT isn't self-contained.
            # Self-contained engines (Apple SpeechAnalyzer) own the audio
            # capture inside their Swift subprocess.
            ts = threading.Thread(target=_init_stt,   daemon=True, name="init-stt")
            tt = threading.Thread(target=_init_trans, daemon=True, name="init-trans")
            ts.start()
            tt.start()
            if not is_self_contained_stt:
                ta = threading.Thread(target=_init_audio, daemon=True, name="init-audio")
                ta.start()

            # Live progress display: update overlay every 0.5s with what's pending
            STATUS_LABELS = {
                "audio": ("audio", "音频"),
                "stt":   ("STT",   "语音识别"),
                "trans": ("translator", "翻译"),
            }
            # 2026-05-14: split the wait. audio + translator are usually
            # ≤1s, but STT can take 2-5s for Qwen LiveTranslate (WS
            # connect + module imports). Show "Ready" as soon as audio
            # + translator are up so the user doesn't stare at "Loading
            # STT..." for 5 seconds. STT keeps initializing in the
            # background; while it's coming up the Swift capture_audio
            # subprocess is already running and its stdout buffer is
            # holding the early PCM — once STT thread starts reading,
            # it picks up where the stream began. No audio lost.
            ready_emitted = False
            while not (init_state["audio_done"] and init_state["stt_done"] and init_state["trans_done"]):
                if not self.is_running:
                    return
                # Stage 1: audio + trans done → flip the UI to "Ready"
                # even if STT is still connecting. Only do this once.
                if (
                    not ready_emitted
                    and init_state["audio_done"]
                    and init_state["trans_done"]
                    and not init_state["stt_done"]
                ):
                    self.subtitle_ready.emit("Ready", "就绪")
                    ready_emitted = True
                    logger.info(
                        "Pipeline UI marked 'Ready' early at %.1fs "
                        "(STT still initializing in background)",
                        time.monotonic() - t0,
                    )
                # While we're still actually loading something, show
                # the loading message — but skip if we already emitted
                # 'Ready', otherwise the UI would flicker back to
                # "Loading STT..." after showing "Ready".
                if not ready_emitted:
                    pending_en, pending_zh = [], []
                    for k in ("audio", "stt", "trans"):
                        if not init_state[f"{k}_done"]:
                            pending_en.append(STATUS_LABELS[k][0])
                            pending_zh.append(STATUS_LABELS[k][1])
                    elapsed = time.monotonic() - t0
                    self.subtitle_ready.emit(
                        f"Loading {', '.join(pending_en)}... ({elapsed:.0f}s)",
                        f"加载 {', '.join(pending_zh)}... ({elapsed:.0f}s)",
                    )
                time.sleep(0.2 if ready_emitted else 0.5)

            # Collect any errors
            errs = [
                ("audio",      init_state["audio_err"]),
                ("STT",        init_state["stt_err"]),
                ("translator", init_state["trans_err"]),
            ]
            errs = [(name, e) for name, e in errs if e is not None]
            if errs:
                msg = "; ".join(f"{name}: {e}" for name, e in errs)
                logger.error("Pipeline init failed: %s", msg)
                self.is_running = False
                self.error_occurred.emit(msg)
                return

            logger.info(
                "Pipeline init (parallel) total=%.1fs: audio=%.1fs, STT=%.1fs, translator=%.1fs",
                time.monotonic() - t0,
                init_state["audio_t"], init_state["stt_t"], init_state["trans_t"],
            )
            logger.info(
                "Pipeline ready: STT=%s, Translator=%s",
                type(self._stt).__name__,
                type(self._translator).__name__ if self._translator else "(none — STT provides translation)",
            )
            # Fallback "Ready" emit — only fires if the early branch above
            # didn't already do it (e.g. when STT finishes before
            # audio/trans, which can happen on Google STT cold start
            # where Gemini import takes longer than Google's gRPC init).
            if not ready_emitted:
                self.subtitle_ready.emit("Ready", "就绪")
        except Exception as e:
            self.is_running = False
            self.error_occurred.emit(str(e))
            logger.exception("Failed to initialize pipeline")
            return

        # Run the main processing loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run_loop())
        except Exception as e:
            logger.exception("Pipeline thread crashed")
            self.error_occurred.emit(str(e))
        finally:
            loop.close()

    @staticmethod
    def _detect_silence(audio: np.ndarray, sample_rate: int = 16000,
                        tail_seconds: float = 0.4, threshold: float = 0.008) -> bool:
        """Check if the tail of the audio is silent (speaker paused)."""
        tail_samples = int(sample_rate * tail_seconds)
        if len(audio) < tail_samples:
            return False
        tail = audio[-tail_samples:]
        energy = np.sqrt(np.mean(tail ** 2))
        return energy < threshold

    async def _run_loop_translation_stream(self):
        """Streaming loop for engines with provides_translation=True
        (Qwen LiveTranslate). The engine produces (orig, trans, is_final)
        triples directly; we relay them to subtitle_ready and emit
        utterance_finalized on is_final=True. No translator is invoked.

        This is a much simpler loop than _run_loop_streaming because
        all the partial-revision / LCP / time-flush / chunk-boundary
        logic lives in the engine (or its remote server), not here.
        """
        logger.info(
            "Pipeline entering translation-stream mode (engine.provides_translation=True)"
        )
        while self.is_running:
            try:
                fatal = getattr(self._stt, "fatal_error", None)
                if fatal:
                    logger.error("STT fatal: %s — stopping translation stream", fatal)
                    self.error_occurred.emit(f"Audio source lost. {fatal}")
                    break
                items = await self._stt.drain_translations()
                if not items:
                    continue
                # Among consecutive non-finals, only keep the latest —
                # they supersede earlier partials. Always emit ALL finals.
                # CRITICAL: filter out empty triples ('','',is_final).
                # Verified 2026-05-14 log line 452: an empty push wiped
                # the live area 0.2s after the subtitle showed up.
                # Empty triples come from edge cases where the engine
                # pushes "(self._current_orig, self._current_trans, True)"
                # after a reset, both fields already cleared.
                def _nonempty(it):
                    return bool(it[0].strip() or it[1].strip())
                finals = [it for it in items if it[2] and _nonempty(it)]
                non_finals = [it for it in items if not it[2] and _nonempty(it)]
                latest_non_final = non_finals[-1] if non_finals else None
                for orig, trans, is_final in finals:
                    self.subtitle_ready.emit(orig, trans)
                    self.utterance_finalized.emit()
                    # Record into recorder for SRT export
                    self._record_translation_result(orig, trans)
                if latest_non_final is not None:
                    o, t, _ = latest_non_final
                    self.subtitle_ready.emit(o, t)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("translation-stream loop error")
                self.error_occurred.emit(str(e))
                await asyncio.sleep(1)

    def _record_translation_result(self, orig: str, trans: str) -> None:
        """Add a (orig, trans) pair to the SessionRecorder so the SRT
        auto-export at session end captures Qwen-mode subtitles.
        Translator-provided pairs already get recorded inside
        flush_and_translate; this method is the equivalent for the
        provides_translation engine path."""
        if not orig and not trans:
            return
        try:
            entry = SubtitleEntry(
                timestamp=time.time(),
                original=orig,
                translated=trans,
                source_lang=self.settings.source_lang,
                target_lang=self.settings.target_lang,
                provider="qwen_livetranslate",
                cost_usd=0.0,  # Qwen cost tracked separately via response.done usage
            )
            self._history.append(entry)
            from captionlm.recorder.base import SubtitleEntry as _RecEntry
            self._recorder.add_entry(_RecEntry(
                timestamp=entry.timestamp,
                original=entry.original,
                translated=entry.translated,
                source_lang=entry.source_lang,
                target_lang=entry.target_lang,
                provider=entry.provider,
                cost_usd=entry.cost_usd,
            ))
        except Exception as e:
            logger.debug("recorder add failed: %s", e)

    async def _run_loop_streaming(self):
        """Streaming loop for self-contained STT engines (e.g. SpeechAnalyzer).

        Strategy (post user feedback "long delay between original and trans"):
        - PARTIAL results from SpeechAnalyzer GROW within an utterance.
          Example partial sequence:
              "可愛い"
              "可愛いでしょ"
              "可愛いでしょ。"        ← FIRST 。— translate "可愛いでしょ。"
              "可愛いでしょ。私"
              "可愛いでしょ。私も嬉しい"
              "可愛いでしょ。私も嬉しいわ。"  ← SECOND 。— translate "私も嬉しいわ。"
              ...
        - We track translated_prefix = text already translated this utterance.
          On each new transcript, find sentence boundaries in the untranslated
          tail and translate each complete sentence IMMEDIATELY — without
          waiting for is_final. Long utterances get sentence-by-sentence
          translation in real time instead of one giant lagging translation.
        - On is_final: translate any remainder; reset translated_prefix.
        - On utterance reset (new transcript that doesn't extend prior):
          drop translated_prefix.
        - During waits: original updates, translation slot keeps last good
          translation (no "翻译中" flicker).
        """
        logger.info("Pipeline entering streaming mode (self-contained STT)")

        last_translated_text: str | None = None  # dedup retranslate
        last_good_translation: str = ""  # keep showing while new is loading
        translated_prefix: str = ""  # text within current utterance already translated
        MIN_FINAL_REMAINDER_CHARS = 3  # min length to translate a final without punctuation
        SENTENCE_END_CHARS = {"。", "？", "！", "?", "!", ".", "．", "…"}
        # Soft boundaries — Chinese/Japanese commas and enumeration marks.
        # These are NOT real sentence enders, so we only use them as
        # flush points when new_part exceeds SOFT_FLUSH_MIN_CHARS,
        # which keeps short utterances un-fragmented but prevents
        # runaway 1-2 minute single sentences in continuous Chinese
        # speech where hard terminators (。？！) may never appear.
        # User feedback (2026-05-12): "翻译中文到英文的时候，发现一句话太长了，
        # 好像有一两分钟长".
        SOFT_BOUNDARY_CHARS = {"，", "、", ","}
        # Tightened 2026-05-12 from 50→35 because user reported Chinese
        # sentences still felt very long; at typical Mandarin pace of
        # ~3-4 chars/sec, 50 chars meant ~12-15s wait for a flush, 35
        # gives ~9-10s which is closer to what feels real-time.
        SOFT_FLUSH_MIN_CHARS = 35
        # HARD force-flush cap — worst case Google STT emits long
        # Chinese stretches with NO punctuation at all (no 。 no ，).
        # In that case there is no boundary to flush at, and we wait
        # for is_final, which can be minutes for continuous speech.
        # User feedback (2026-05-12): "翻译中文到英文的时候，发现一句话
        # 太长了，好像有一两分钟长". 100 chars ≈ 25-30s of speech is the
        # absolute outer bound before we force-cut.
        HARD_FORCE_FLUSH_CHARS = 100
        # Time-based force flush — independent of character count.
        # Rationale (2026-05-13): Google STT does NOT emit punctuation
        # in interim/partial results; punctuation only appears at
        # is_final. So our punctuation-based boundary detection finds
        # nothing in partials, and we waited for either 100-char hard
        # cap or is_final, both of which can take 20-30+ seconds for
        # continuous speech. The time-based fallback guarantees the
        # user sees fresh translation at least every TIME_FORCE_FLUSH_SEC
        # seconds, regardless of what Google STT emits.
        #
        # Tightened to 4s on 2026-05-13 after user reported "一句都
        # 1分钟多了" — either they were running pre-change code OR
        # the 5s threshold + Gemini latency still felt too long. 4s
        # gives ~5s end-to-end freshness; if too aggressive (cuts
        # short clauses mid-thought) raise back to 5-6s.
        TIME_FORCE_FLUSH_SEC = 4.0
        # Don't fire time-flush for trivially-short tails; wait until
        # there's something meaningful to translate.
        MIN_TIME_FLUSH_CHARS = 10
        last_flush_time = time.monotonic()
        # Per-utterance accumulators REMOVED 2026-05-13 (later that day).
        # The accumulator was trying to make 1 utterance = 1 history
        # entry, but Chinese STT only fires is_final every ~30s so each
        # utterance became a 5-6 line wall of text. User feedback:
        # "每一句还是太长，长度能变成现在1/3就好了". New design: emit
        # EACH chunk to subtitle_ready directly (so it lands in history
        # as its own short entry, ~ one sentence), and let the overlay
        # accumulate within an utterance for live-area display only.

        async def flush_and_translate(text_to_translate: str):
            """Translate text, emit subtitle, update last_good_translation.

            on_partial streaming DISABLED 2026-05-13 per user feedback:
            "每一次实时翻译重复刷新的时候，等到整段话出来了再一次性更新，
            不要一个个字跳出来". The progressive token-by-token visual
            churn was distracting. We still get streaming under the hood
            from the translator (faster API return), but only fire
            subtitle_ready ONCE per translation, with the final text.
            """
            nonlocal last_translated_text, last_good_translation, last_flush_time
            if not text_to_translate or text_to_translate == last_translated_text:
                return
            t1 = time.monotonic()

            try:
                trans_result: TranslationResult = await self._translator.translate(
                    text=text_to_translate,
                    source_lang=self.settings.source_lang,
                    target_lang=self.settings.target_lang,
                    context=self._context[-self.MAX_CONTEXT:] if self._context else None,
                    on_partial=None,  # ← suppress per-chunk emissions
                )
            except Exception as e:
                logger.error("Translation error: %s", e)
                trans_result = TranslationResult(
                    text=f"[Error] {text_to_translate}",
                    provider="error",
                )
            trans_ms = (time.monotonic() - t1) * 1000

            # Detect if translation actually succeeded (not an error placeholder)
            is_error = (
                trans_result.provider == "error"
                or trans_result.text.startswith("[Error]")
                or trans_result.text.startswith("[Gemini error]")
            )

            if not is_error:
                last_good_translation = trans_result.text
                self._context.append((text_to_translate, trans_result.text))
                if len(self._context) > self.MAX_CONTEXT * 2:
                    self._context = self._context[-self.MAX_CONTEXT:]
                # Per-chunk retrans (2026-05-13 redesign): each chunk
                # becomes its own history entry, so retrans buffer
                # entries also key on chunk-level orig/trans. The
                # translation_updated signal then matches the chunk
                # entry in the overlay's history list.
                self._last_committed_orig = text_to_translate
                self._last_committed_trans = trans_result.text

            entry = SubtitleEntry(
                timestamp=time.time(),
                original=text_to_translate,
                translated=trans_result.text,
                source_lang=self.settings.source_lang,
                target_lang=self.settings.target_lang,
                provider=trans_result.provider,
                cost_usd=trans_result.cost_usd,
            )
            self._history.append(entry)
            # Mirror to SessionRecorder for SRT/CSV/TXT export on stop().
            # Convert to recorder's SubtitleEntry — same fields, different module.
            from captionlm.recorder.base import SubtitleEntry as _RecEntry
            self._recorder.add_entry(_RecEntry(
                timestamp=entry.timestamp,
                original=entry.original,
                translated=entry.translated,
                source_lang=entry.source_lang,
                target_lang=entry.target_lang,
                provider=entry.provider,
                cost_usd=entry.cost_usd,
            ))
            # Update session totals + notify settings panel so the cost
            # meter is live. tokens_used is a rough estimate from the
            # translator (e.g. GeminiTranslator computes len(prompt)//4
            # + len(translated)//4), good enough for a display.
            if not is_error:
                self._session_tokens += int(getattr(trans_result, "tokens_used", 0))
                self._session_translation_cost += float(getattr(trans_result, "cost_usd", 0.0))
                self._emit_session_usage()

            if getattr(trans_result, "rate_limited", False):
                self.rate_limit_warning.emit(trans_result.provider)

            # Emit the CHUNK directly — each chunk is a separate
            # history entry in the overlay (user wants ~1/3 the
            # previous accumulated-entry length, 2026-05-13).
            self.subtitle_ready.emit(text_to_translate, trans_result.text)
            last_translated_text = text_to_translate
            # Reset the time-flush clock on any successful translation
            # (errors don't reset it — we want to keep trying).
            if not is_error:
                last_flush_time = time.monotonic()
            logger.debug(
                "Streaming subtitle (Trans %.0fms, error=%s): %s → %s",
                trans_ms, is_error, text_to_translate[:60], trans_result.text[:60],
            )

        async def process_one(text: str, is_final: bool):
            """Process a single transcript through the utterance state machine.
            Same logic as before; factored out so we can apply it in a loop
            after draining the STT queue."""
            # CRITICAL (2026-05-14): last_good_translation MUST be nonlocal.
            # Previously this declaration was missing while is_final block
            # tried to assign `last_good_translation = ""`, which made
            # Python treat the name as a local variable for the whole
            # function — including the earlier `read` in the partial-emit
            # branch — and raise UnboundLocalError. The except in
            # flush_and_translate caught it and produced bogus history
            # entries like 'Error ...' → 'cannot access local variable'
            # leaking English error text into Chinese subtitles. Verified
            # 2026-05-14 log line 92, 168, etc.
            nonlocal translated_prefix, last_flush_time, last_good_translation
            text = text.strip()
            if not text:
                return

            # Detect utterance reset — but be LENIENT about trailing
            # punctuation. Google STT often inserts 。 then removes it
            # later when more speech follows. translated_prefix ends
            # with 。 while new text continues without it → naive
            # startswith fails → false reset on every chunk (verified
            # in production log 2026-05-13: every Streaming subtitle
            # was followed by an Utterance reset line).
            #
            # Fix: rstrip BOTH Chinese and English sentence-terminal
            # punctuation from translated_prefix before the startswith
            # check; if text starts with the stripped form, NOT a
            # reset — treat as continuation, and lower our prefix to
            # the stripped form so new_part calculation aligns.
            _PUNCT_FOR_PREFIX_CHECK = "。、，,.!?？！…．： :; ；\n\r\t "
            prefix_stripped = translated_prefix.rstrip(_PUNCT_FOR_PREFIX_CHECK) if translated_prefix else ""
            if translated_prefix and not text.startswith(prefix_stripped):
                # Major revision: STT changed content INSIDE the
                # already-translated portion (not just the trailing
                # punct). Don't clear translated_prefix entirely —
                # that would re-emit everything we've already
                # translated to subtitle_ready, producing visible
                # duplication in the overlay accumulator (live area
                # showed "Let's go! Let's go!" 2026-05-13).
                #
                # Instead, find the longest common char prefix
                # between new text and prefix_stripped. Keep that
                # as the new translated_prefix — only the diverging
                # tail will be re-translated.
                common_len = 0
                shorter = min(len(text), len(prefix_stripped))
                while common_len < shorter and text[common_len] == prefix_stripped[common_len]:
                    common_len += 1
                if common_len >= 5:  # at least a few chars survived
                    logger.info(
                        "Utterance partial revision — keeping %d/%d "
                        "common chars (diverging tail will re-translate)",
                        common_len, len(prefix_stripped),
                    )
                    translated_prefix = text[:common_len]
                else:
                    logger.info(
                        "Utterance reset (major revision, only %d "
                        "common chars) — fully resetting prefix",
                        common_len,
                    )
                    translated_prefix = ""
                    # Defense in depth: also clear last_good_translation
                    # here so the next utterance's partials never inherit
                    # the previous utterance's translation. The partial
                    # path now emits (text, "") not (text,
                    # last_good_translation), so this is belt-and-
                    # suspenders, but it documents the invariant.
                    last_good_translation = ""
            elif translated_prefix and prefix_stripped != translated_prefix:
                # Continuation case but STT dropped our trailing punct.
                # Reset prefix to stripped form so new_part covers the
                # right untranslated portion (and not skip a char).
                translated_prefix = prefix_stripped

            # The new untranslated portion of the current utterance
            new_part = text[len(translated_prefix):]

            # Find LAST sentence-ending punctuation in new_part — translate
            # everything up to and including it (one or more complete sentences)
            last_boundary = -1
            for i, ch in enumerate(new_part):
                if ch in SENTENCE_END_CHARS:
                    last_boundary = i

            # P0 fix (2026-05-14): minimum chars gate for hard-boundary flush.
            # Reason — verified from log.2: Google STT for Japanese emits
            # short chunks ending with 。like 'ずっと踊。' (5 chars) and
            # 'ったりしてて。' (8 chars), each translated INDEPENDENTLY as
            # '一直在跳舞。' / '之类的吗？'. The original sentence
            # 'ずっと踊ったりしててよくわからないな' was sliced into
            # 3 fragments that read like garbage word-by-word.
            # Strategy: if the hard-boundary cut leaves a piece shorter
            # than MIN_HARD_FLUSH_CHARS (Chinese/Japanese 1 char ≈ 1
            # word, so 8 chars = ~2-3 word fragment), defer until either
            # more text accumulates, time-flush triggers, or is_final
            # arrives. is_final ALWAYS forces a flush regardless of
            # length (it's the true utterance end — translating short
            # remainders there is correct).
            MIN_HARD_FLUSH_CHARS = 8
            if (
                last_boundary >= 0
                and (last_boundary + 1) < MIN_HARD_FLUSH_CHARS
                and not is_final
            ):
                logger.debug(
                    "Deferring short hard-boundary flush "
                    "(piece=%d < %d chars): %r",
                    last_boundary + 1, MIN_HARD_FLUSH_CHARS,
                    new_part[:last_boundary + 1],
                )
                last_boundary = -1

            # Soft-boundary fallback for runaway sentences (no hard
            # terminator yet, but enough length accumulated that the
            # user shouldn't have to wait for is_final to see anything).
            # Picks the LAST comma in new_part — keeps clauses intact
            # while ensuring forward progress. Only activates if new_part
            # is already long enough to justify a mid-sentence flush.
            if last_boundary < 0 and len(new_part) >= SOFT_FLUSH_MIN_CHARS:
                for i, ch in enumerate(new_part):
                    if ch in SOFT_BOUNDARY_CHARS:
                        last_boundary = i

            # TIME-based force flush — Google STT doesn't emit punctuation
            # in partials, so punctuation-based detection finds nothing
            # until is_final. This makes sure we ship SOMETHING every
            # TIME_FORCE_FLUSH_SEC seconds regardless. Same word-space
            # preference as the hard-char cap.
            if (
                last_boundary < 0
                and len(new_part) >= MIN_TIME_FLUSH_CHARS
                and (time.monotonic() - last_flush_time) >= TIME_FORCE_FLUSH_SEC
            ):
                last_space = new_part.rfind(" ")
                if last_space >= MIN_TIME_FLUSH_CHARS // 2:
                    last_boundary = last_space
                else:
                    last_boundary = len(new_part) - 1
                # INFO level so this shows in console — user reported
                # "中文一句一分多钟" suggesting force-flush wasn't firing
                # (or they were running pre-change code). Visible log
                # lets us verify the path is hot on each test run.
                logger.info(
                    "TIME-FORCE-FLUSH after %.1fs (new_part=%d chars, cut@%d)",
                    time.monotonic() - last_flush_time, len(new_part),
                    last_boundary + 1,
                )

            # HARD force-flush — worst case where STT produces a long
            # stretch with NO punctuation at all (common in continuous
            # Chinese speech). Picks a word-space if one exists in the
            # back half of new_part (good for English/Japanese with
            # spaces), otherwise just cuts at the current end (fine
            # for Chinese where each character is roughly word-equivalent).
            if last_boundary < 0 and len(new_part) >= HARD_FORCE_FLUSH_CHARS:
                # Prefer splitting at a whitespace boundary in the back
                # half so Latin-script languages don't break mid-word.
                half = HARD_FORCE_FLUSH_CHARS // 2
                last_space = new_part.rfind(" ")
                if last_space >= half:
                    last_boundary = last_space
                else:
                    last_boundary = len(new_part) - 1

            # Track whether THIS call to process_one already emitted a
            # subtitle_ready via flush_and_translate. If yes, suppress
            # the partial-progress emit at the bottom of this function
            # to prevent the double-emit bug (verified 2026-05-13 from
            # log: pipeline 50 emits → overlay 103 receives = exactly
            # 2× because both the chunk path AND the partial path were
            # firing for every chunked partial transcript).
            chunk_emitted_this_call = False
            if last_boundary >= 0:
                # Translate the complete-sentence chunk immediately.
                # ALSO strip leading punctuation from the chunk —
                # when our prefix-strip path (in the partial revision
                # block above) shortens translated_prefix to drop a
                # trailing 。, that punctuation character is still in
                # `text` and becomes the LEADING char of the next
                # chunk. Verified 2026-05-13 ja-JP log: entries like
                # '。危険怖い。'→'。好危險，好可怕。' starting with
                # leading 「。」 were polluting history.
                chunk = new_part[:last_boundary + 1].strip().lstrip(_PUNCT_FOR_PREFIX_CHECK)
                if chunk and chunk != last_translated_text:
                    await flush_and_translate(chunk)
                    chunk_emitted_this_call = True
                    # Advance our prefix past the translated chunk
                    translated_prefix = text[:len(translated_prefix) + last_boundary + 1]

            if is_final:
                # End of utterance. Translate any tail without punctuation
                # (only if substantial — avoid translating "あ" alone).
                # Same leading-punct strip as the chunk path above.
                tail = text[len(translated_prefix):].strip().lstrip(_PUNCT_FOR_PREFIX_CHECK)
                if tail and len(tail) >= MIN_FINAL_REMAINDER_CHARS \
                        and tail != last_translated_text:
                    await flush_and_translate(tail)
                # Reset for next utterance
                translated_prefix = ""
                # Add the just-finalized utterance to the retrans buffer
                # and trigger background retranslation of the prior 1-2
                # utterances (which now have richer future context).
                if (
                    self._last_committed_orig
                    and self._last_committed_trans
                    and not self._last_committed_trans.startswith("[Error]")
                    and not self._last_committed_trans.startswith("[Gemini error]")
                ):
                    self._add_to_retrans_buffer(
                        self._last_committed_orig,
                        self._last_committed_trans,
                    )
                    # Fire-and-forget — retranslation runs in the
                    # background and emits translation_updated when done.
                    asyncio.ensure_future(self._do_retranslations())
                # Signal to subscribers that the previous subtitle_ready
                # emission was the FINAL form of that utterance — overlay
                # uses this to reset its live-area accumulator so the
                # next utterance starts fresh in the live display.
                self.utterance_finalized.emit()
                # CRITICAL (2026-05-14 P0+): clear last_good_translation
                # so partials of the NEXT utterance don't pair their new
                # orig with the previous utterance's translation. Without
                # this clear, when STT emits a partial for utterance N+1
                # (e.g. 'うん。' deferred by P0 hard-boundary gate), the
                # partial branch below runs
                # `self.subtitle_ready.emit(text, last_good_translation)`
                # with last_good_translation = utterance N's translation.
                # Overlay then sees (new_orig='うん。', old_trans='反正
                # 马上就能回去的话…') as a fresh tuple, hits its
                # sentence-commit because old_trans ends with '。', and
                # APPENDs a bogus history entry. Verified from real log
                # 2026-05-14 line 2200-2230: 257 REPLACE + 36
                # SKIP-prefix-shorter for QwenASR+QwenMT path, with
                # history entries like
                # [47] orig='うん。' trans='反正马上就能回去的话…'.
                last_good_translation = ""
            else:
                # Partial — show STT progress (text = raw STT cumulative
                # so far). Translation slot is EMPTY (not last_good_translation)
                # to prevent the previous utterance's translation from being
                # paired with new partial text — overlay's sentence-commit
                # then pushes that mismatch as a history entry.
                #
                # 2026-05-14: verified bug from log line 19:
                #   [18] orig='ダメを持って...' trans='明明我都把不好的都揽下来...'
                #   [19] orig='お母さん。'       trans='明明我都把不好的都揽下来...'  ❌
                # Root cause: previous version emitted (text, last_good_translation),
                # where last_good_translation was the previous utterance's
                # finalized trans. When trans ends with '。' the overlay treats
                # it as a complete sentence and APPENDs to history — but orig
                # is the NEXT utterance's partial. Hybrid garbage entry.
                #
                # Fix: emit "" for trans in partial path. The user briefly sees
                # raw STT text with an empty trans area (≤1s until the next
                # flush_and_translate fires) — minor UX cost in exchange for
                # eliminating the bogus history entries entirely.
                # GUARD also kept: don't double-emit if a chunk was already
                # flushed in this process_one call.
                if not chunk_emitted_this_call:
                    self.subtitle_ready.emit(text, "")

        while self.is_running:
            try:
                # Drain ALL pending transcripts at once (blocks 1s for first one).
                # This is the key latency-accumulation fix: under sustained speech
                # STT produces ~5 partials/sec but Gemini translates ~1/sec, so a
                # naive next_transcript() loop fell ever further behind. By pulling
                # everything available and skipping intermediate non-finals we keep
                # the pipeline current with the speaker.
                # Check STT fatal-error flag (set when capture_audio
                # subprocess dies — see google_streaming_stt._stream_loop).
                # If set, we exit the streaming loop and let user
                # restart the pipeline. Verified via 2026-05-13 real
                # meeting log: previously we kept calling drain in a
                # tight loop forever, with no audio coming through.
                fatal = getattr(self._stt, "fatal_error", None)
                if fatal:
                    logger.error("STT fatal: %s — stopping pipeline streaming loop", fatal)
                    self.error_occurred.emit(f"Audio source lost. {fatal}")
                    break

                results = await self._stt.drain_transcripts()
                if not results:
                    continue

                # Preserve ALL is_final results — they're sentence boundaries and
                # carry the most accurate STT output for that utterance.
                # Among non-final partials, keep ONLY the latest — earlier ones
                # are superseded by later ones in the same utterance.
                finals = [r for r in results if r[1]]
                non_finals = [r for r in results if not r[1]]
                latest_non_final = non_finals[-1] if non_finals else None
                dropped = len(non_finals) - (1 if latest_non_final else 0)
                if dropped > 0:
                    logger.debug(
                        "drain: %d total → %d finals + 1 latest partial "
                        "(dropped %d intermediate partials)",
                        len(results), len(finals), dropped,
                    )

                # Process finals first (in order), then the freshest partial.
                # Finals advance state cleanly; the trailing partial gives the
                # user immediate visibility into the next in-progress utterance.
                for text, is_final in finals:
                    await process_one(text, is_final)
                if latest_non_final is not None:
                    await process_one(latest_non_final[0], latest_non_final[1])

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Streaming loop error")
                self.error_occurred.emit(str(e))
                await asyncio.sleep(1)

    async def _run_loop(self):
        """Main pipeline loop. Two paths depending on STT engine type:

        1. Self-contained STT (Apple SpeechAnalyzer): the Swift subprocess
           captures audio and emits transcripts asynchronously. We just pull
           transcripts via stt.next_transcript() and translate them.

        2. Chunk-based STT (Whisper, system): we accumulate audio from the
           AudioCapture queue, detect silence, and call stt.transcribe() per
           chunk.
        """
        # Branch: self-contained engine bypasses all audio chunking logic
        if getattr(self._stt, "is_self_contained", False):
            # Sub-branch: engine that produces translations end-to-end
            # (Qwen LiveTranslate). Pipeline skips its own translator
            # call and just relays (orig, trans) pairs from the engine.
            if getattr(self._stt, "provides_translation", False):
                await self._run_loop_translation_stream()
            else:
                await self._run_loop_streaming()
            return

        # Wait for audio capture to be ready
        if self._audio is None:
            logger.error("Audio capture is None but STT is not self-contained")
            return
        if hasattr(self._audio, '_ready_event'):
            logger.info("Waiting for audio capture to initialize...")
            while self.is_running and not self._audio._ready_event.is_set():
                await asyncio.sleep(0.2)
            if not self.is_running:
                return
            logger.info("Audio capture ready, starting pipeline loop")

        SAMPLE_RATE = 16000
        # Whisper has its own VAD and handles shorter chunks well,
        # so we can use shorter accumulation windows for lower latency.
        is_whisper = self.settings.stt_engine == "whisper"
        MIN_SECONDS = 2.0 if is_whisper else 3.0
        MAX_SECONDS = 6.0 if is_whisper else 10.0
        FIRST_MIN = 1.5 if is_whisper else 2.0
        MIN_SAMPLES = int(SAMPLE_RATE * MIN_SECONDS)
        MAX_SAMPLES = int(SAMPLE_RATE * MAX_SECONDS)
        FIRST_MIN_SAMPLES = int(SAMPLE_RATE * FIRST_MIN)

        # Audio accumulation buffer
        accumulated = np.array([], dtype=np.float32)
        is_first_chunk = True

        while self.is_running:
            try:
                # Read new audio (don't cap here — we manage accumulation ourselves)
                new_audio = self._audio.read_chunk(max_seconds=10.0)
                if new_audio is not None and len(new_audio) > 0:
                    accumulated = np.concatenate([accumulated, new_audio])

                # Use shorter minimum for the first chunk (faster startup)
                current_min = FIRST_MIN_SAMPLES if is_first_chunk else MIN_SAMPLES

                # Check if we should process
                should_process = False
                if len(accumulated) >= current_min:
                    if self._detect_silence(accumulated, SAMPLE_RATE):
                        should_process = True  # Natural pause detected
                if len(accumulated) >= MAX_SAMPLES:
                    should_process = True  # Force process — too long

                if not should_process:
                    await asyncio.sleep(0.1)
                    continue

                # Take the accumulated audio and reset buffer
                audio_chunk = accumulated
                accumulated = np.array([], dtype=np.float32)
                is_first_chunk = False

                t0 = time.monotonic()
                chunk_seconds = len(audio_chunk) / SAMPLE_RATE

                # Transcribe
                text = await self._stt.transcribe(audio_chunk)
                if not text or not text.strip():
                    continue

                text = text.strip()
                stt_ms = (time.monotonic() - t0) * 1000
                logger.debug("STT (%.0fms, %.1fs audio): %s", stt_ms, chunk_seconds, text)

                # Translate
                t1 = time.monotonic()
                try:
                    result: TranslationResult = await self._translator.translate(
                        text=text,
                        source_lang=self.settings.source_lang,
                        target_lang=self.settings.target_lang,
                        context=self._context[-self.MAX_CONTEXT:] if self._context else None,
                    )
                except Exception as e:
                    logger.error("Translation error: %s", e)
                    result = TranslationResult(
                        text=f"[Error] {text}",
                        provider="error",
                    )
                trans_ms = (time.monotonic() - t1) * 1000

                # Update context
                self._context.append((text, result.text))
                if len(self._context) > self.MAX_CONTEXT * 2:
                    self._context = self._context[-self.MAX_CONTEXT:]

                # Record history (and mirror to SessionRecorder for export)
                entry = SubtitleEntry(
                    timestamp=time.time(),
                    original=text,
                    translated=result.text,
                    source_lang=self.settings.source_lang,
                    target_lang=self.settings.target_lang,
                    provider=result.provider,
                    cost_usd=result.cost_usd,
                )
                self._history.append(entry)
                from captionlm.recorder.base import SubtitleEntry as _RecEntry
                self._recorder.add_entry(_RecEntry(
                    timestamp=entry.timestamp,
                    original=entry.original,
                    translated=entry.translated,
                    source_lang=entry.source_lang,
                    target_lang=entry.target_lang,
                    provider=entry.provider,
                    cost_usd=entry.cost_usd,
                ))

                # Warn UI about rate limiting
                if result.rate_limited:
                    self.rate_limit_warning.emit(result.provider)

                # Emit to UI
                total_ms = (time.monotonic() - t0) * 1000
                self.subtitle_ready.emit(text, result.text)
                logger.debug(
                    "Subtitle (STT %.0fms + Trans %.0fms = %.0fms): %s → %s",
                    stt_ms, trans_ms, total_ms, text[:40], result.text[:40],
                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Pipeline loop error")
                self.error_occurred.emit(str(e))
                await asyncio.sleep(1)

    def get_history(self) -> list[SubtitleEntry]:
        return list(self._history)

    def clear_history(self):
        self._history.clear()
        self._context.clear()

    def _create_audio_capture(self) -> AudioCapture:
        import platform

        if platform.system() == "Darwin":
            from captionlm.audio.system_capture import MacOSAudioCapture
            return MacOSAudioCapture(sample_rate=16000)
        elif platform.system() == "Windows":
            raise NotImplementedError("Windows audio capture not yet implemented")
        else:
            raise RuntimeError(f"Unsupported platform: {platform.system()}")

    def _create_stt_engine(self) -> STTEngine:
        engine_name = self.settings.stt_engine

        if engine_name == "google_streaming":
            import platform
            if platform.system() != "Darwin":
                raise NotImplementedError(
                    "Google Streaming STT capture is macOS-only "
                    "(uses ScreenCaptureKit for system audio)"
                )
            from captionlm.stt.google_streaming_stt import GoogleStreamingSTT
            return GoogleStreamingSTT(language=self.settings.source_lang)
        elif engine_name == "speech_analyzer":
            import platform
            if platform.system() != "Darwin":
                raise NotImplementedError(
                    "Apple SpeechAnalyzer is macOS-only (requires macOS 26+)"
                )
            from captionlm.stt.apple_speech_analyzer import AppleSpeechAnalyzerSTT
            return AppleSpeechAnalyzerSTT(language=self.settings.source_lang)
        elif engine_name == "qwen_asr":
            # Qwen ASR — DashScope qwen3-asr-flash-realtime (Singapore /
            # mainland). Independent STT step; pipeline runs its own
            # translator on top (same architecture as Google STT).
            import platform
            if platform.system() != "Darwin":
                raise NotImplementedError(
                    "Qwen ASR uses Swift capture_audio (macOS-only)"
                )
            from captionlm.stt.qwen_asr import QwenASRSTT
            api_key = self.settings.get_api_key("dashscope")
            if not api_key:
                raise RuntimeError(
                    "Qwen ASR requires DashScope API key. Set it in the "
                    "Control Panel under API Keys, or export "
                    "DASHSCOPE_API_KEY in your shell."
                )
            return QwenASRSTT(
                api_key=api_key,
                language=self.settings.source_lang,
                region=self.settings.dashscope_region,
            )
        elif engine_name == "qwen_livetranslate":
            # Qwen LiveTranslate is end-to-end audio→translated-text.
            # Pipeline detects provides_translation=True and skips the
            # standalone translator step. (Aliyun's qwen3-livetranslate-
            # flash-realtime model, WebSocket-based, ~3s same-language
            # delay.)
            import platform
            if platform.system() != "Darwin":
                raise NotImplementedError(
                    "Qwen LiveTranslate STT uses Swift capture_audio (macOS-only)"
                )
            # 2026-05-14 timing diagnostic — measure how much of Qwen
            # init is module import (first-time numpy etc.) vs the
            # constructor itself.
            _t_imp0 = time.monotonic()
            from captionlm.stt.qwen_livetranslate import QwenLiveTranslateSTT
            _t_imp1 = time.monotonic()
            api_key = self.settings.get_api_key("dashscope")
            if not api_key:
                raise RuntimeError(
                    "Qwen LiveTranslate requires DashScope API key. "
                    "Set it in the Control Panel under API Keys, or "
                    "export DASHSCOPE_API_KEY in your shell."
                )
            instance = QwenLiveTranslateSTT(
                api_key=api_key,
                source_lang=self.settings.source_lang,
                target_lang=self.settings.target_lang,
                region=self.settings.dashscope_region,
            )
            _t_imp2 = time.monotonic()
            logger.info(
                "Qwen _create_stt_engine (ms): import_module=%.0f  "
                "constructor=%.0f",
                (_t_imp1 - _t_imp0) * 1000,
                (_t_imp2 - _t_imp1) * 1000,
            )
            return instance
        elif engine_name == "system":
            import platform
            if platform.system() == "Darwin":
                from captionlm.stt.system_stt import MacOSSystemSTT
                return MacOSSystemSTT(language=self.settings.source_lang)
            else:
                raise NotImplementedError("System STT not available on this platform")
        elif engine_name == "whisper":
            from captionlm.stt.whisper_stt import WhisperSTT
            return WhisperSTT(
                model_size=self.settings.whisper_model_size,
                language=self.settings.source_lang,
            )
        else:
            raise ValueError(f"Unknown STT engine: {engine_name}")

    def _create_translator(self) -> Translator:
        provider = self.settings.translation_provider

        if provider == "google_free":
            from captionlm.translation.google_free import GoogleFreeTranslator
            return GoogleFreeTranslator()
        elif provider == "gemini":
            from captionlm.translation.gemini import GeminiTranslator
            return GeminiTranslator(api_key=self.settings.get_api_key("gemini"))
        elif provider == "claude":
            from captionlm.translation.claude import ClaudeTranslator
            return ClaudeTranslator(api_key=self.settings.get_api_key("claude"))
        elif provider == "qwen":
            # DashScope qwen-mt-turbo dedicated translation model (OpenAI-
            # compatible API). Reuses the same DASHSCOPE_API_KEY as the
            # Qwen ASR / LiveTranslate STT engines.
            from captionlm.translation.qwen import QwenTranslator
            api_key = self.settings.get_api_key("dashscope")
            if not api_key:
                raise RuntimeError(
                    "Qwen translator requires DashScope API key. Set it in "
                    "the Control Panel under API Keys, or export "
                    "DASHSCOPE_API_KEY in your shell."
                )
            return QwenTranslator(
                api_key=api_key,
                model="qwen-mt-turbo",
                region=self.settings.dashscope_region,
            )
        else:
            raise ValueError(f"Unknown translation provider: {provider}")
