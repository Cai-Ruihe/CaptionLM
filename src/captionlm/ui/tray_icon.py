"""System tray icon for CaptionLM.

Provides quick access to:
- Toggle subtitle overlay visibility
- Open control panel
- Quit application
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QIcon, QAction, QPixmap, QPainter, QColor, QFont
from PySide6.QtWidgets import QSystemTrayIcon, QMenu, QApplication

from captionlm.config.settings import Settings

_log = logging.getLogger("captionlm.ui.tray_icon")


def _find_logo_for_tray() -> Path | None:
    """Locate the CaptionLM logo PNG to use as the menubar icon.

    Looks in the package's `assets/` folder (same pattern that
    native_overlay.py uses for the bottom-left overlay logo), which
    resolves correctly in both source-checkout mode and py2app .app
    bundle mode because the package layout is identical in both.
    """
    candidates = [
        # Bundled location — works in source + py2app modes
        Path(__file__).resolve().parent.parent / "assets" / "logo-overlay.png",
    ]
    # py2app sometimes places resources outside the package — try
    # the .app's Resources directory as a secondary lookup.
    if getattr(sys, "frozen", False):
        try:
            macos_dir = Path(sys.executable).resolve().parent
            resources = macos_dir.parent / "Resources"
            candidates.append(
                resources / "captionlm" / "assets" / "logo-overlay.png"
            )
        except Exception:
            pass
    for c in candidates:
        if c.is_file():
            return c
    return None


def _load_tray_icon() -> QIcon:
    """Build the CaptionLM menubar icon.

    Strategy:
      1. Try to load the real logo PNG (white "C" on transparent).
      2. On macOS, mark it as a template image — macOS uses the alpha
         channel as the shape and auto-tints it based on light/dark
         menubar mode, so a white-on-transparent source renders as
         dark in light menubar and light in dark menubar correctly.
      3. If the logo file is missing for any reason, fall back to the
         procedural pink-circle-with-C placeholder so the user still
         has *something* to click.
    """
    logo_path = _find_logo_for_tray()
    if logo_path is not None:
        pix = QPixmap(str(logo_path))
        if not pix.isNull():
            icon = QIcon(pix)
            if sys.platform == "darwin":
                # Template image: alpha → shape, macOS picks the color.
                # Without this, a white logo would be invisible on the
                # light menubar.
                try:
                    icon.setIsMask(True)
                except Exception as e:
                    _log.warning("setIsMask failed: %s", e)
            _log.info("Tray icon loaded from %s (template=%s)",
                      logo_path,
                      sys.platform == "darwin")
            return icon
        else:
            _log.warning("Tray logo %s is unreadable; using placeholder",
                         logo_path)
    else:
        _log.warning("Tray logo not found in any candidate path; "
                     "using placeholder")
    return _create_default_icon()


def _create_default_icon() -> QIcon:
    """Procedural fallback icon — only used if logo PNG is missing."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor(0, 0, 0, 0))

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Background circle
    painter.setBrush(QColor("#e94560"))
    painter.setPen(QColor(0, 0, 0, 0))
    painter.drawEllipse(4, 4, 56, 56)

    # "C" letter
    painter.setPen(QColor("#ffffff"))
    painter.setFont(QFont(".AppleSystemUIFont", 32, QFont.Weight.Bold))
    painter.drawText(pixmap.rect(), 0x0084, "C")  # AlignCenter

    painter.end()
    return QIcon(pixmap)


class TrayIcon(QSystemTrayIcon):
    """System tray icon with context menu."""

    toggle_overlay = Signal()
    show_control_panel = Signal()
    quit_app = Signal()

    def __init__(self, app: QApplication, settings: Settings):
        super().__init__(_load_tray_icon(), app)
        self.setToolTip("CaptionLM — Real-time subtitle translation")

        menu = QMenu()

        toggle_action = QAction("Toggle Subtitles", menu)
        toggle_action.triggered.connect(self.toggle_overlay.emit)
        menu.addAction(toggle_action)

        panel_action = QAction("Control Panel", menu)
        panel_action.triggered.connect(self.show_control_panel.emit)
        menu.addAction(panel_action)

        menu.addSeparator()

        quit_action = QAction("Quit CaptionLM", menu)
        quit_action.triggered.connect(self.quit_app.emit)
        menu.addAction(quit_action)

        self.setContextMenu(menu)

        # Double-click tray icon → show control panel
        self.activated.connect(self._on_activated)

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_control_panel.emit()
