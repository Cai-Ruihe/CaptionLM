"""Entry point for CaptionLM."""

import os
import signal
import sys
import threading
import time


def _install_sigterm_handler():
    """Convert SIGTERM into a normal Python exit so atexit handlers fire.

    Why this exists: default SIGTERM action is process termination WITHOUT
    running atexit handlers. The autotest harness (and any supervisor)
    invokes `kill $PID`, which sends SIGTERM. Without this conversion, the
    capture_audio Swift subprocess in google_streaming_stt.py gets orphaned
    and accumulates as a long-lived zombie consuming ~21% CPU each.

    Empirical: observed 7 stale capture_audio processes after a single dev
    session, oldest running 1h25m of CPU time.

    sys.exit() raises SystemExit, which DOES trigger atexit handlers,
    cleaning up subprocesses registered in google_streaming_stt._active_processes.
    """
    def _handler(signum, frame):
        sys.exit(128 + signum)
    signal.signal(signal.SIGTERM, _handler)


def _prewarm_heavy_imports():
    """Pre-load heavy Python packages in background threads.

    Empirical: on Python 3.14 these packages take 30-60s to cold-import
    because some transitive deps lack prebuilt wheels and fall back to
    slow source-build / dynamic binding generation. Pre-warming in
    background while PySide6 (5s) loads makes total wait closer to
    max(prewarm, ui) than sum.

    Targets:
    - `google.genai` — used by GeminiTranslator. Was the dominant blocker
      of pipeline init in user's empirical test (>51s).
    - `Speech, Foundation` — used by MacOSSystemSTT (legacy path). Skipped
      now since the default STT is SpeechAnalyzer which is native Swift.
      Re-enable this if you build a system_stt user.
    """
    if sys.platform != "darwin":
        return

    def _import_genai():
        try:
            t0 = time.monotonic()
            from google import genai  # noqa: F401
            print(f"  [perf] Pre-warmed google.genai: {(time.monotonic()-t0)*1000:.0f}ms")
        except ImportError:
            pass  # User may not have google-genai installed if not using Gemini
        except Exception as e:
            print(f"  [perf] google.genai pre-warm failed: {e}")

    threading.Thread(target=_import_genai, daemon=True,
                     name="genai-prewarm").start()


def _fix_qt_plugin_path():
    """Ensure Qt can find the cocoa platform plugin on macOS.

    Sets QT_QPA_PLATFORM_PLUGIN_PATH to point directly to the platforms/
    directory inside PySide6. This is more reliable than QT_PLUGIN_PATH
    when the venv path contains spaces.
    """
    try:
        import PySide6
        pyside_dir = os.path.dirname(PySide6.__file__)

        # Point directly to the platforms directory (most specific)
        platforms_dir = os.path.join(pyside_dir, "Qt", "plugins", "platforms")
        if os.path.isdir(platforms_dir):
            os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = platforms_dir

        # Also set the general plugin path
        plugin_dir = os.path.join(pyside_dir, "Qt", "plugins")
        if os.path.isdir(plugin_dir):
            os.environ["QT_PLUGIN_PATH"] = plugin_dir

        # Set Qt library path for other Qt plugins
        lib_dir = os.path.join(pyside_dir, "Qt", "lib")
        if os.path.isdir(lib_dir):
            os.environ.setdefault("DYLD_FRAMEWORK_PATH", lib_dir)

    except Exception:
        pass


def main():
    """Launch CaptionLM application."""
    t0 = time.monotonic()

    # Install SIGTERM handler EARLY so it's active before any subprocess
    # gets spawned. Layer 2 of the capture_audio leak defense.
    _install_sigterm_handler()

    # NOTE: removed _prewarm_heavy_imports() — empirically suspected of
    # polluting sys.modules with partial 'google' import state when it
    # races with the lazy gemini import in worker thread, leading to
    # spurious "google-genai is required" ImportError. Lazy init in
    # gemini.py handles cold-load timing fine without prewarm.

    _fix_qt_plugin_path()
    print(f"  [perf] Qt plugin path: {(time.monotonic()-t0)*1000:.0f}ms")

    t1 = time.monotonic()
    from captionlm.app import CaptionLMApp
    print(f"  [perf] Import app: {(time.monotonic()-t1)*1000:.0f}ms")

    t2 = time.monotonic()
    app = CaptionLMApp(sys.argv)
    print(f"  [perf] App init: {(time.monotonic()-t2)*1000:.0f}ms")

    t3 = time.monotonic()
    print(f"  [perf] Total to run(): {(time.monotonic()-t0)*1000:.0f}ms")
    sys.exit(app.run())


if __name__ == "__main__":
    main()
