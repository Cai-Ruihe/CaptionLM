"""py2app setup script for building a fully self-contained CaptionLM.app.

End-user requirements: ZERO. No Python, no Xcode, no brew packages.
Everything required at runtime ships inside the .app bundle:
  • Python 3.11+ interpreter (embedded by py2app)
  • All Python deps (PySide6, numpy, sounddevice, google-cloud-speech,
    google-genai, openai, anthropic, websockets, deep-translator)
  • Pre-compiled capture_audio Swift binary (built earlier by build_dmg.sh)
  • Logo assets (logo-overlay.png)
  • Application icon (icon.icns, generated from logo-color.png)

Usage:
    python packaging/setup_app.py py2app

Output: packaging/dist/CaptionLM.app
"""

from setuptools import setup
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

APP = [str(ROOT / "src" / "captionlm" / "__main__.py")]

# ─────────────────────────────────────────────────────────────────
# DATA_FILES — non-Python resources copied into the .app at fixed paths.
# Each tuple is (destination_dir_relative_to_Resources/, [source_files]).
# ─────────────────────────────────────────────────────────────────
DATA_FILES = []

# 1) Runtime image assets (logo-overlay.png)
_runtime_assets_src = ROOT / "src" / "captionlm" / "assets"
_runtime_assets_files = [
    str(p) for p in _runtime_assets_src.glob("*")
    if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".svg")
]
if _runtime_assets_files:
    DATA_FILES.append(("captionlm/assets", _runtime_assets_files))

# 2) Pre-compiled Swift capture binary — CRITICAL for end users (no swiftc)
_capture_binary = ROOT / "src" / "captionlm" / "audio" / "capture_audio"
if _capture_binary.exists():
    DATA_FILES.append(("captionlm/audio", [str(_capture_binary)]))
    print(f"[setup_app.py] Bundling pre-compiled capture_audio: "
          f"{_capture_binary} ({_capture_binary.stat().st_size:,} bytes)")
else:
    print(f"[setup_app.py] WARNING: {_capture_binary} not found. End users "
          "will fall back to requiring Xcode CLT. Run "
          "`./packaging/build_dmg.sh` which compiles capture_audio before "
          "invoking py2app.")

# 3) Swift source as backup (for the swiftc-fallback path if the binary
#    somehow won't load)
_swift_src = ROOT / "src" / "captionlm" / "audio" / "capture_audio.swift"
if _swift_src.exists():
    DATA_FILES.append(("captionlm/audio", [str(_swift_src)]))


# ─────────────────────────────────────────────────────────────────
# py2app OPTIONS
# ─────────────────────────────────────────────────────────────────
OPTIONS = {
    "argv_emulation": False,

    # Top-level packages whose entire contents must be copied. py2app's
    # static analyzer misses indirect imports inside these (especially
    # gRPC's dynamically-loaded sub-modules), so we list them
    # explicitly. Without this, end users hit ImportError on first run.
    "packages": [
        "captionlm",
        # Qt — gigantic, all needed
        "PySide6",
        "shiboken6",
        # Numerics / audio
        "numpy",
        "sounddevice",
        # Provider clients.
        # NOTE: do NOT put bare "google" here — that's a PEP 420
        # namespace package with no __init__.py, and py2app's
        # imp_find_module raises ImportError on it. List concrete
        # sub-packages in `includes` further down instead.
        "openai",
        "anthropic",
        "websockets",
        "httpx",             # transitive dep of google-genai + openai
        "httpcore",
        # Translation fallback
        "deep_translator",
        # Settings (toml on Python 3.11+ uses stdlib `tomllib`, but our
        # code falls back to `tomli` on <3.12; safer to include both)
        "tomli",
        "tomli_w",
        # PyObjC frameworks (overlay needs these at runtime)
        "Cocoa",
        "AppKit",
        "Foundation",
        "objc",
    ],

    # Modules that aren't packages but still need to be present.
    "includes": [
        # Concrete google.* sub-packages. We list these instead of bare
        # "google" because google is a PEP 420 namespace package with
        # no __init__.py; py2app's modulegraph can't recurse a bare
        # "google" but can follow these specific sub-package imports.
        "google.cloud",
        "google.cloud.speech",
        "google.cloud.speech_v1",
        "google.cloud.speech_v1.services",
        "google.cloud.speech_v1.types",
        "google.genai",
        "google.api_core",
        "google.auth",
        "google.auth.transport",
        "google.auth.transport.requests",
        "google.auth.transport.grpc",
        "google.protobuf",
        "google.rpc",
        # Internal entry points
        "captionlm.app",
        "captionlm.pipeline",
        "captionlm.__main__",
        # STT engines (lazy-imported in pipeline.py, so the static
        # analyzer can miss them)
        "captionlm.stt.base",
        "captionlm.stt.google_streaming_stt",
        "captionlm.stt.qwen_asr",
        "captionlm.stt.qwen_livetranslate",
        "captionlm.stt.apple_speech_analyzer",
        "captionlm.stt.system_stt",
        # Translators (also lazy-imported)
        "captionlm.translation.base",
        "captionlm.translation.gemini",
        "captionlm.translation.qwen",
        "captionlm.translation.claude",
        "captionlm.translation.google_free",
        # UI
        "captionlm.ui.native_overlay",
        "captionlm.ui.control_panel",
        "captionlm.ui.tray_icon",
        "captionlm.ui.first_run",
        "captionlm.ui.styles",
        # Audio
        "captionlm.audio.system_capture",
        # Recorder (SRT export)
        "captionlm.recorder.base",
        # Config
        "captionlm.config.settings",
    ],

    # Modules to NEVER bundle (saves ~80 MB and avoids cross-platform code
    # path bugs).
    "excludes": [
        "tkinter",
        "PyQt5",
        "PyQt6",
        "test",
        "tests",
        "pytest",
    ],

    # ─── App bundle metadata (Info.plist) ───
    "plist": {
        "CFBundleName": "CaptionLM",
        "CFBundleDisplayName": "CaptionLM",
        "CFBundleIdentifier": "com.captionlm.app",
        "CFBundleVersion": "0.1.0",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleExecutable": "CaptionLM",
        "NSHighResolutionCapable": True,
        # REQUIRED — macOS won't allow ScreenCaptureKit access without
        # this human-readable explanation.
        "NSScreenCaptureUsageDescription":
            "CaptionLM captures system audio so it can transcribe and "
            "translate whatever you're listening to. Audio is processed "
            "via the speech-to-text API key you configure; nothing is "
            "uploaded to CaptionLM's servers (we don't have any).",
        # Microphone fallback when ScreenCaptureKit is unavailable.
        "NSMicrophoneUsageDescription":
            "CaptionLM may fall back to the microphone if system audio "
            "capture fails.",
        # Hide the Dock icon when the app starts — it's a menubar utility.
        # Users can open the overlay panel from the system tray icon.
        "LSUIElement": True,
        # Minimum macOS = 12 (Monterey). ScreenCaptureKit needs 13+,
        # but Whisper / SpeechAnalyzer paths work on 12. We set 12 here
        # so the bundle isn't artificially blocked from older Macs.
        "LSMinimumSystemVersion": "12.0",
        # When the user double-clicks an .srt file, route it to us (helps
        # users find their exported session files).
        "CFBundleDocumentTypes": [
            {
                "CFBundleTypeName": "SubRip Subtitle",
                "CFBundleTypeExtensions": ["srt"],
                "CFBundleTypeRole": "Viewer",
            }
        ],
        # Network-access usage strings (macOS Sequoia 15+ may prompt).
        # gRPC / WebSocket / HTTPS to provider APIs.
        "NSAppTransportSecurity": {
            "NSAllowsArbitraryLoads": False,
            # Allow the specific provider hosts we actually contact.
            "NSExceptionDomains": {
                "googleapis.com":              {"NSIncludesSubdomains": True},
                "generativelanguage.googleapis.com": {"NSIncludesSubdomains": True},
                "speech.googleapis.com":       {"NSIncludesSubdomains": True},
                "openai.com":                  {"NSIncludesSubdomains": True},
                "anthropic.com":               {"NSIncludesSubdomains": True},
                "dashscope.aliyuncs.com":      {"NSIncludesSubdomains": True},
                "dashscope-intl.aliyuncs.com": {"NSIncludesSubdomains": True},
                "translate.googleapis.com":    {"NSIncludesSubdomains": True},
            },
        },
    },

    # Use the icon.icns generated from logo-color.png. py2app falls
    # back to a default icon if this file doesn't exist.
    "iconfile": (
        str(ROOT / "assets" / "icon.icns")
        if (ROOT / "assets" / "icon.icns").exists() else None
    ),

    # Improve startup time — keep frequently-loaded packages OUTSIDE
    # the site-packages.zip so Python doesn't have to unzip them.
    "site_packages": True,
    "strip": True,           # strip debug symbols from .so files
    "optimize": 0,           # don't pre-compile .pyc with -O (keep asserts)
}


setup(
    app=APP,
    name="CaptionLM",
    version="0.1.0",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    # CRITICAL: py2app 0.28.x raises
    #   "error: install_requires is no longer supported"
    # if the distribution has ANY install_requires. Setuptools auto-
    # injects pyproject.toml's `dependencies` field as install_requires,
    # so build_dmg.sh moves pyproject.toml out of the way during build.
    # Explicitly setting [] here is belt-and-suspenders — if pyproject.toml
    # is still visible for any reason, this empty list wins.
    install_requires=[],
    # NOTE: setup_requires=["py2app"] also removed — setuptools >= 80
    # doesn't support it. build_dmg.sh installs py2app via pip.
)
