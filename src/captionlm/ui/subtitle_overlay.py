"""DEPRECATED (2026-05-06).

The Qt-based SubtitleOverlay was replaced by the PyObjC-based
NativeSubtitleOverlay in captionlm.ui.native_overlay. The Qt version had
intractable issues with always-on-top + click-through behavior on macOS;
NSPanel via PyObjC provides those primitives natively and reliably.

This file is kept as an empty stub purely so any external tooling or
documentation pointing at this import path doesn't hard-error. The
actual implementation lives at:

    from captionlm.ui.native_overlay import NativeSubtitleOverlay
"""
