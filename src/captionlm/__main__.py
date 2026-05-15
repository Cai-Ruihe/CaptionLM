"""Entry point for CaptionLM."""

import os
import signal
import sys
import threading
import time


def _ensure_stdio_writable():
    """Ensure sys.stdout / sys.stderr can be written to without raising.

    Why this exists (root-caused 2026-05-16):

    When CaptionLM.app is double-clicked from Finder (or launched via
    `open` / `open -a` without explicit `--stdout / --stderr`), macOS
    `launchd` connects the GUI process to stdio file descriptors that
    are either CLOSED or pointed at a fd that immediately rejects writes.
    Any subsequent `print()` / `sys.stderr.write()` — whether from
    OUR code (the `[perf] ...` lines in main() below) or from third-
    party libraries during import (PySide6, urllib3 warnings, etc.) —
    raises BrokenPipeError / OSError. py2app's launcher binary catches
    that exception, treats startup as failed, and shows its generic
    "Launch error" dialog with the actual exception nowhere visible.

    Empirical reproduction:
    - Finder double-click → Launch error dialog ❌
    - `open /Applications/CaptionLM.app` → same ❌
    - `open -a /Applications/CaptionLM.app --stdout F --stderr F` → ✅
       (--stdout/--stderr supply writable fds, prints succeed)
    - `Contents/MacOS/CaptionLM` from terminal → ✅
       (terminal stdio is a writable TTY)

    Fix: at the very top of main(), probe each stdio stream with a
    no-op write. If the write fails, swap the stream for a sink that
    silently accepts and discards. This must happen BEFORE any other
    import or initialization so libraries loaded later inherit the
    safe stdio.

    Side effects: dev-time `print()` output is unaffected when stdio
    is genuinely writable (terminal, redirected). Only the broken-fd
    launchd path is silently no-op'd.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            # py2app sometimes presents sys.stdout/stderr as None
            try:
                setattr(sys, name, open(os.devnull, "w"))
            except Exception:
                pass
            continue
        try:
            stream.write("")
            stream.flush()
        except (BrokenPipeError, OSError, AttributeError, ValueError):
            # Unwritable under launchd: replace with a /dev/null sink so
            # downstream print() / write() calls silently no-op instead
            # of raising and tearing down Python startup.
            try:
                setattr(sys, name, open(os.devnull, "w"))
            except Exception:
                # Last-resort fallback: in-memory sink. Grows over time
                # but at least won't crash. Should never reach this path.
                import io
                setattr(sys, name, io.StringIO())


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
    # Step 0 — MUST be first: harden stdio against launchd's closed-fd
    # behavior. Without this, any print() (including the [perf] lines
    # below) raises BrokenPipeError when launched via Finder double-click,
    # producing the py2app generic "Launch error" dialog. See the docstring
    # on _ensure_stdio_writable for full reproduction notes.
    _ensure_stdio_writable()

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
