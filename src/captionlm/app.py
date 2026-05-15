"""Main application class for CaptionLM."""

from __future__ import annotations

import os
import signal
import logging
from pathlib import Path

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

from captionlm.config.settings import Settings

logger = logging.getLogger(__name__)


def _setup_file_logging(verbose_console: bool = False) -> Path | None:
    """Set up rotating file logging in <project>/logs/.

    Always logs DEBUG level to file (regardless of console verbosity), so we
    have full diagnostic data in case of issues. Keeps the last LOG_KEEP_RUNS
    runs (bumped 2026-05-13 from 3 → 10 per user request — needed to
    cross-reference between sessions when troubleshooting).

    Returns the path to the active log file, or None if setup failed.
    """
    LOG_KEEP_RUNS = 10  # current + .log.1 ... .log.{N-1}
    try:
        # Project root: __file__ is .../captionlm/src/captionlm/app.py
        # We want .../captionlm/logs/
        project_root = Path(__file__).resolve().parent.parent.parent
        logs_dir = project_root / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)

        # Rotate manually so we keep one log per RUN (not per file size).
        # Layout after rotation:
        #   captionlm.log     = current run (just starting)
        #   captionlm.log.1   = previous run
        #   captionlm.log.2   = 2 runs ago
        #   ...
        #   captionlm.log.{N-1} = oldest kept run
        log_file = logs_dir / "captionlm.log"
        # 1. Delete anything older than the keep horizon.
        oldest = logs_dir / f"captionlm.log.{LOG_KEEP_RUNS - 1}"
        if oldest.exists():
            oldest.unlink()
        # 2. Shift each numbered slot up by one (.{N-2} → .{N-1}, ..., .1 → .2).
        for i in range(LOG_KEEP_RUNS - 2, 0, -1):
            src = logs_dir / f"captionlm.log.{i}"
            dst = logs_dir / f"captionlm.log.{i + 1}"
            if src.exists():
                src.rename(dst)
        # 3. Current .log → .log.1
        if log_file.exists():
            log_file.rename(logs_dir / "captionlm.log.1")

        # Configure root logger: file always DEBUG, console INFO (or DEBUG if --debug)
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)

        # Clear any prior handlers from previous calls (in case of restart)
        for h in list(root.handlers):
            root.removeHandler(h)

        # Include the date in the timestamp so cross-session log analysis
        # can disambiguate runs that happen across midnight or across days.
        # Previously the format was HH:MM:SS only, which made it impossible
        # to tell which day a log entry was from when cross-referencing
        # the user's chat-message timestamp (2026-05-13 incident).
        fmt = logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s %(threadName)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # Secret-redaction filter — masks "Authorization: Bearer sk-…"
        # tokens before they hit any handler. Critical: 2026-05-13 incident
        # had the websockets library log the full Authorization header at
        # DEBUG (which our file handler captures), leaking the user's
        # DashScope API key in plaintext. Filter handles BOTH the
        # message string and any positional args.
        import re as _re
        _BEARER_RE = _re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}", _re.IGNORECASE)
        _SK_RE = _re.compile(r"\bsk-[A-Za-z0-9]{16,}\b")

        def _redact(value):
            if not isinstance(value, str):
                return value
            v = _BEARER_RE.sub(r"\1[REDACTED]", value)
            v = _SK_RE.sub("sk-[REDACTED]", v)
            return v

        class _RedactSecretsFilter(logging.Filter):
            def filter(self, record):
                try:
                    if isinstance(record.msg, str):
                        record.msg = _redact(record.msg)
                    if record.args:
                        if isinstance(record.args, tuple):
                            record.args = tuple(_redact(a) for a in record.args)
                        elif isinstance(record.args, dict):
                            record.args = {k: _redact(v) for k, v in record.args.items()}
                except Exception:
                    pass
                return True

        _redact_filter = _RedactSecretsFilter()

        # Cap noisy third-party loggers at INFO so they don't drown the
        # file log AND don't leak request headers at DEBUG.
        # websockets in particular logs the full handshake including
        # the Authorization header at DEBUG — security risk.
        logging.getLogger("websockets").setLevel(logging.INFO)
        logging.getLogger("websockets.client").setLevel(logging.INFO)
        logging.getLogger("websockets.server").setLevel(logging.INFO)
        logging.getLogger("httpcore").setLevel(logging.INFO)
        # (httpx already logs at INFO; we keep it visible since it
        # doesn't dump headers.)

        # File handler — always DEBUG, captures everything (after the
        # redact filter has cleaned the record).
        fh = logging.FileHandler(log_file, mode="w", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        fh.addFilter(_redact_filter)
        root.addHandler(fh)

        # Console handler — INFO (or DEBUG if user passed --debug)
        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG if verbose_console else logging.INFO)
        ch.setFormatter(fmt)
        ch.addFilter(_redact_filter)  # console also gets redacted
        root.addHandler(ch)

        # Header line with environment info — useful when diagnosing later
        import platform, sys
        logger.info("=" * 70)
        logger.info("CaptionLM session log: %s", log_file)
        logger.info("Python:   %s", sys.version.replace("\n", " "))
        logger.info("Platform: %s %s (%s)", platform.system(),
                    platform.release(), platform.machine())
        logger.info("CWD:      %s", os.getcwd())
        logger.info("=" * 70)
        return log_file
    except Exception as e:
        # Don't crash the app if logging setup fails — fall back to stderr only
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
        logger.warning("File logging setup failed: %s — using console only", e)
        return None


class CaptionLMApp:
    """Main application orchestrator.

    On startup: only the subtitle overlay and tray icon are shown.
    The pipeline auto-starts. The control panel is hidden and can
    be opened from the tray icon.
    """

    def __init__(self, argv: list[str]):
        self.qt_app = QApplication(argv)
        self.qt_app.setApplicationName("CaptionLM")
        self.qt_app.setApplicationVersion("0.1.0")
        self.qt_app.setQuitOnLastWindowClosed(False)

        self.settings = Settings.load()
        self.pipeline = None
        self.overlay = None
        self.control_panel = None
        self.tray = None

        # Set up file + console logging. File always DEBUG, console INFO unless
        # user passed --debug. Keeps last 3 runs in <project>/logs/.
        self._log_file = _setup_file_logging(verbose_console="--debug" in argv)

    def run(self) -> int:
        """Start the application."""
        # 2026-05-14: detailed timing logs through the startup sequence.
        # User reported "全程启动慢"; previously we only measured the
        # Pipeline init segment, but Log analysis showed a 17-second gap
        # between NSPanel-created and Pipeline-started that had nothing
        # logged in it. These steps fill that gap.
        import time as _time
        t_run0 = _time.monotonic()

        signal.signal(signal.SIGINT, signal.SIG_DFL)

        if self.settings.is_first_run:
            self.settings.is_first_run = False
            self.settings.save()

        t_imp0 = _time.monotonic()
        # Lazy-import UI components (avoids loading all widgets at module level)
        from captionlm.ui.control_panel import ControlPanel
        from captionlm.ui.tray_icon import TrayIcon

        # Subtitle overlay is the native NSPanel (PyObjC). The old Qt
        # SubtitleOverlay was deleted on 2026-05-06 along with its
        # fallback path — the native overlay is the only supported
        # implementation now.
        from captionlm.ui.native_overlay import NativeSubtitleOverlay
        t_imp1 = _time.monotonic()

        self.overlay = NativeSubtitleOverlay(self.settings)
        t_ovl = _time.monotonic()
        logger.info("Using NativeSubtitleOverlay (NSPanel via PyObjC)")

        self.control_panel = ControlPanel(self.settings)
        t_cp = _time.monotonic()

        self.tray = TrayIcon(self.qt_app, self.settings)
        t_tray = _time.monotonic()

        # Wire up signals
        self.control_panel.start_requested.connect(self._on_start)
        self.control_panel.stop_requested.connect(self._on_stop)
        self.control_panel.settings_changed.connect(self._on_settings_changed)
        self.overlay.settings_changed.connect(self._on_settings_changed)
        self.overlay.start_stop_clicked.connect(self._on_toggle)
        self.overlay.quit_requested.connect(self._quit)
        self.overlay.show_settings_panel.connect(self.control_panel.toggle_or_show)
        self.tray.toggle_overlay.connect(self.overlay.toggle_visibility)
        self.tray.show_control_panel.connect(self.control_panel.toggle_or_show)
        self.tray.quit_app.connect(self._quit)
        t_signals = _time.monotonic()

        # Only show overlay and tray — control panel stays hidden
        self.overlay.show()
        t_ovl_show = _time.monotonic()
        self.tray.show()
        t_tray_show = _time.monotonic()

        logger.info(
            "Startup steps (ms): pre_import=%.0f  imports=%.0f  "
            "overlay_create=%.0f  control_panel_create=%.0f  "
            "tray_create=%.0f  signals=%.0f  overlay.show=%.0f  "
            "tray.show=%.0f  TOTAL_to_event_loop=%.0f",
            (t_imp0 - t_run0) * 1000,
            (t_imp1 - t_imp0) * 1000,
            (t_ovl - t_imp1) * 1000,
            (t_cp - t_ovl) * 1000,
            (t_tray - t_cp) * 1000,
            (t_signals - t_tray) * 1000,
            (t_ovl_show - t_signals) * 1000,
            (t_tray_show - t_ovl_show) * 1000,
            (t_tray_show - t_run0) * 1000,
        )

        # Auto-start the pipeline after a brief delay (let UI render first)
        QTimer.singleShot(100, self._on_start)

        return self.qt_app.exec()

    def _on_toggle(self):
        """Toggle pipeline from overlay Start/Stop button."""
        if self.pipeline and self.pipeline.is_running:
            self._on_stop()
        else:
            self._on_start()

    def _on_start(self):
        """Start the caption pipeline."""
        import time as _time
        _t0 = _time.monotonic()
        if self.pipeline and self.pipeline.is_running:
            return

        # 2026-05-14 fix: if there's a stale pipeline object (is_running=False
        # but never disposed), force its full teardown BEFORE creating a new
        # one. Otherwise the old STT's _ws_thread keeps running its
        # reconnect-sleep loop, will spawn ANOTHER capture_audio subprocess
        # right when the new STT also spawns its own, and macOS rejects the
        # second one with SCStreamErrorDomain -3805 "application connection
        # being interrupted". Verified 2026-05-14 log line 944-958: two
        # `Starting capture_audio` lines in the same second, two PIDs, two
        # WS sessions, then -3805.
        if self.pipeline is not None:
            try:
                logger.info("Tearing down stale pipeline before restart")
                self.pipeline.stop()
            except Exception as e:
                logger.warning("Stale pipeline stop error: %s", e)
            self.pipeline = None

        # Reset overlay history before new session — otherwise the fading
        # history strip would show last session's final sentence at the top.
        self.overlay.clear_history()
        _t_clear = _time.monotonic()

        from captionlm.pipeline import CaptionPipeline
        _t_import = _time.monotonic()
        self.pipeline = CaptionPipeline(self.settings)
        _t_pipeline = _time.monotonic()
        logger.info(
            "_on_start steps (ms): clear_history=%.0f  import_pipeline=%.0f  "
            "CaptionPipeline_init=%.0f",
            (_t_clear - _t0) * 1000,
            (_t_import - _t_clear) * 1000,
            (_t_pipeline - _t_import) * 1000,
        )
        self.pipeline.subtitle_ready.connect(self.overlay.update_subtitle)
        # Authoritative utterance-end signal — overlay commits current to
        # history immediately on this, instead of waiting for its 2-second
        # silence timer. Crucial for back-and-forth dialogue where each
        # speaker's turn is < 2s apart.
        self.pipeline.utterance_finalized.connect(self.overlay.on_utterance_finalized)
        self.pipeline.error_occurred.connect(self._on_pipeline_error)
        self.pipeline.rate_limit_warning.connect(self.overlay.show_rate_limit_warning)
        # Live cost meter in the settings panel.
        self.pipeline.session_usage_updated.connect(self.control_panel.update_session_usage)
        # Retranslation polish — pipeline re-translates prior utterances
        # with newer context; overlay updates the history entry in place.
        if hasattr(self.overlay, "on_translation_updated"):
            self.pipeline.translation_updated.connect(self.overlay.on_translation_updated)
        # Audio heartbeat — green/gray/red dot in overlay corner reflects
        # whether audio is flowing. Helps users notice when capture_audio
        # dies mid-session (verified in 2026-05-13 real meeting log).
        if hasattr(self.overlay, "on_audio_health"):
            self.pipeline.audio_health.connect(self.overlay.on_audio_health)
        # Streaming-translator engines (Qwen LiveTranslate) produce
        # authoritative is_final boundaries themselves. Tell the overlay
        # to skip its sentence-end-punct heuristic + 2s silence-timer
        # commit, otherwise we get duplicate/fragmented history entries
        # (verified 2026-05-14 log: 69 APPENDs for 13 real utterances).
        engine_provides_trans = self.settings.stt_engine == "qwen_livetranslate"
        if hasattr(self.overlay, "set_streaming_translator_mode"):
            self.overlay.set_streaming_translator_mode(engine_provides_trans)
        self.pipeline.start()
        self.control_panel.set_running(True)
        self.overlay.set_running(True)
        logger.info("Pipeline started")

    def _on_stop(self):
        """Stop the caption pipeline."""
        if self.pipeline:
            self.pipeline.stop()
            self.pipeline = None
            self.control_panel.set_running(False)
            self.overlay.set_running(False)
            logger.info("Pipeline stopped")

    def _on_settings_changed(self):
        was_running = self.pipeline and self.pipeline.is_running
        if was_running:
            self._on_stop()
        self.settings.save()
        self.overlay.apply_settings(self.settings)
        if was_running:
            self._on_start()

    def _on_pipeline_error(self, error_msg: str):
        logger.error("Pipeline error: %s", error_msg)
        self.control_panel.show_error(error_msg)
        # Also show error in subtitle overlay so user can see it
        self.overlay.update_subtitle("Error", error_msg[:100])

    def _quit(self):
        """Quit the entire application — kill everything including background threads.

        Empirical (2026-05-05): the original implementation only set
        is_running=False and stopped the audio source, then called
        os._exit(0) immediately. That bypassed pipeline.stop()'s real
        cleanup — SRT auto-save, capture_audio subprocess termination,
        and worker thread join — because os._exit skips ALL Python
        teardown including atexit handlers. After a real YouTube test
        the user lost the entire SRT history because of this.

        Fix: call pipeline.stop() explicitly. Its body already does the
        right cleanup (deregister subprocess from atexit set, terminate,
        join worker thread with timeout, then auto-save SRT). os._exit
        stays as a hard backstop for any rogue daemon thread that
        ignores stop signals (e.g. third-party libs that spawn threads
        we don't control).
        """
        logger.info("Quitting CaptionLM...")
        if self.pipeline:
            try:
                self.pipeline.stop()
            except Exception as e:
                logger.warning("Pipeline stop on quit failed: %s", e)
        self.qt_app.quit()
        # Belt-and-suspenders: any lingering daemon threads (Whisper, etc.)
        # would otherwise keep the process alive after pipeline.stop returns.
        os._exit(0)
