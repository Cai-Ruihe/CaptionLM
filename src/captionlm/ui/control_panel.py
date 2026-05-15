"""Settings panel for CaptionLM.

Provides controls for:
- Select source and target languages
- Choose STT engine and translation provider
- Enter API keys for LLM providers
- View status and errors
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QGroupBox, QLineEdit, QMessageBox, QScrollArea,
    QFormLayout, QFileDialog, QColorDialog, QSlider,
)

from captionlm.config.settings import Settings, LANGUAGES, STT_ENGINES, TRANSLATION_PROVIDERS

logger = logging.getLogger(__name__)

# macOS-aligned dark-mode palette. Color values are chosen to match
# Apple HIG semantic colors when rendered against a typical dark
# window background:
#   windowBackgroundColor (dark)  ≈ #1e1e1f
#   controlBackgroundColor (dark) ≈ #2c2c2e
#   labelColor (dark)             ≈ #ffffff @ ~92% alpha
#   secondaryLabelColor (dark)    ≈ #ebebf5 @ ~60% alpha
#   controlAccentColor (Blue)     ≈ #0a84ff (Big Sur+)
PANEL_STYLE = """
QWidget#SettingsPanel {
    background-color: #1e1e20;
    border-radius: 12px;
}
QLabel {
    color: rgba(235, 235, 245, 230);
    font-size: 13px;
}
QLabel#title {
    color: white;
    font-size: 17px;
    font-weight: 600;
}
QLabel#sectionHeader {
    color: rgba(235, 235, 245, 140);
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    padding: 0;
}
QLabel#hintLabel {
    color: rgba(235, 235, 245, 120);
    font-size: 11px;
}
QGroupBox {
    border: none;
    margin: 0;
    padding: 0;
}
QComboBox {
    background-color: #2c2c2e;
    color: white;
    border: 1px solid #3a3a3c;
    border-radius: 6px;
    padding: 5px 10px;
    font-size: 13px;
    min-height: 30px;
}
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView {
    background-color: #2c2c2e;
    color: white;
    selection-background-color: #0a84ff;
    border: 1px solid #3a3a3c;
}
QLineEdit {
    background-color: #2c2c2e;
    color: white;
    border: 1px solid #3a3a3c;
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 13px;
}
QLineEdit:focus {
    border: 1px solid #0a84ff;
}
QPushButton#closeBtn {
    background-color: transparent;
    color: rgba(235, 235, 245, 140);
    font-size: 16px;
    padding: 2px 6px;
    min-width: 22px;
    min-height: 22px;
    border: none;
    border-radius: 11px;
}
QPushButton#closeBtn:hover {
    color: white;
    background-color: rgba(255, 255, 255, 30);
}
QPushButton#smallBtn {
    background-color: #3a3a3c;
    color: white;
    border: 1px solid #48484a;
    border-radius: 5px;
    padding: 4px 12px;
    font-size: 12px;
    min-height: 28px;
}
QPushButton#smallBtn:hover {
    background-color: #48484a;
}
QPushButton#saveBtn {
    background-color: #0a84ff;
    color: white;
    border: none;
    border-radius: 7px;
    padding: 8px 22px;
    font-size: 13px;
    font-weight: 600;
    min-height: 32px;
}
QPushButton#saveBtn:hover {
    background-color: #2691ff;
}
QPushButton#saveBtn:pressed {
    background-color: #006edc;
}
QFrame[frameShape="4"] {
    color: rgba(235, 235, 245, 30);
    background-color: rgba(235, 235, 245, 30);
    max-height: 1px;
}
"""


class ControlPanel(QWidget):
    """Settings panel for CaptionLM — opened from gear icon in overlay."""

    start_requested = Signal()
    stop_requested = Signal()
    settings_changed = Signal()

    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self._is_running = False
        self._setup_ui()

    def _setup_ui(self):
        self.setObjectName("SettingsPanel")
        self.setWindowTitle("CaptionLM Settings")
        # NOTE: Qt.WindowType.Tool removed (2026-05-06). On macOS, Tool
        # maps to NSPanel which defaults to setHidesOnDeactivate:YES —
        # i.e., the panel disappears the moment the user clicks outside
        # it. User complained "我在菜单外点一下，菜单就自动消失了". A
        # plain frameless window stays open until the user explicitly
        # clicks the × close button.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Resizable. Initial size 520x820. Minimums enforced on BOTH
        # axes:
        #   width 500  — wide enough so QLineEdit (with shorter
        #                placeholders, see below) doesn't trigger
        #                QFormLayout's wrap-rows fallback.
        #   height 800 — measured total content height is ~794px
        #                (title + 4 sections + save). Below that
        #                QFormLayout starts wrapping fields below
        #                labels (the "OpenAI/Claude/Doubao labels
        #                stacked tightly + inputs stacked below"
        #                bug user reported). Plus a 6px buffer = 800.
        # 2026-05-14: user reported API key rows appearing squished on
        # FIRST open until window was manually dragged taller. Root
        # cause: 800px min height left QFormLayout in "wrapped" mode
        # where labels stacked above inputs because vertical space was
        # too tight to keep them on the same row. Bumping the
        # min-height + initial resize beyond the natural sizeHint of
        # the full form (title bar + 4 sections + API keys + buttons)
        # ensures the first paint is already in the expanded layout.
        self.setMinimumSize(540, 900)
        self.resize(580, 940)
        self.setStyleSheet(PANEL_STYLE)
        # Enable interactive resize on a frameless window via the
        # SizeGripEnabled flag — Qt draws a small grip at bottom-right.
        # We can't get the native macOS bottom-right resize triangle
        # without a title bar, so this grip is our resize affordance.
        from PySide6.QtWidgets import QSizeGrip
        self._size_grip = QSizeGrip(self)
        self._size_grip.resize(16, 16)
        # Reposition on resize — handled in resizeEvent below.

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(18)

        # --- Header: title + close button ---
        header = QHBoxLayout()
        title = QLabel("Settings")
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setObjectName("closeBtn")
        close_btn.clicked.connect(self.hide)
        header.addWidget(close_btn)
        layout.addLayout(header)

        # --- Languages section ---
        layout.addLayout(self._section_header("Languages"))
        lang_form = self._form_layout()
        self.source_combo = QComboBox()
        for code, name in LANGUAGES.items():
            self.source_combo.addItem(name, code)
        self._set_combo_value(self.source_combo, self.settings.source_lang)
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        lang_form.addRow("From", self.source_combo)
        self.target_combo = QComboBox()
        for code, name in LANGUAGES.items():
            self.target_combo.addItem(name, code)
        self._set_combo_value(self.target_combo, self.settings.target_lang)
        self.target_combo.currentIndexChanged.connect(self._on_target_changed)
        lang_form.addRow("To", self.target_combo)
        layout.addLayout(lang_form)

        # --- Engines section ---
        layout.addLayout(self._section_header("Engines"))
        engines_form = self._form_layout()
        self.stt_combo = QComboBox()
        for key, label in STT_ENGINES.items():
            self.stt_combo.addItem(label, key)
        self._set_combo_value(self.stt_combo, self.settings.stt_engine)
        self.stt_combo.currentIndexChanged.connect(self._on_stt_changed)
        engines_form.addRow("STT", self.stt_combo)
        self.trans_combo = QComboBox()
        for key, label in TRANSLATION_PROVIDERS.items():
            self.trans_combo.addItem(label, key)
        self._set_combo_value(self.trans_combo, self.settings.translation_provider)
        self.trans_combo.currentIndexChanged.connect(self._on_provider_changed)
        engines_form.addRow("Translation", self.trans_combo)
        layout.addLayout(engines_form)

        # --- API Keys section ---
        layout.addLayout(self._section_header("API Keys"))
        keys_form = self._form_layout()

        # Google Cloud Speech uses a Service Account JSON file, not a
        # plain API-key string — different flow from the other providers.
        # We expose a "Choose JSON..." picker that copies the user's
        # downloaded service-account JSON into ~/.captionlm/, where
        # google_streaming_stt._find_credentials() looks for it.
        self._gcp_status_label = QLabel("Not configured")
        self._gcp_status_label.setStyleSheet(
            "color: rgba(235, 235, 245, 180); font-size: 12px;"
        )
        gcp_choose_btn = QPushButton("Choose JSON…")
        gcp_choose_btn.setObjectName("smallBtn")
        gcp_choose_btn.clicked.connect(self._on_choose_gcp_json)
        gcp_row = QWidget()
        gcp_layout = QHBoxLayout(gcp_row)
        gcp_layout.setContentsMargins(0, 0, 0, 0)
        gcp_layout.setSpacing(8)
        gcp_layout.addWidget(self._gcp_status_label, stretch=1)
        gcp_layout.addWidget(gcp_choose_btn)
        keys_form.addRow("Google Cloud", gcp_row)
        self._refresh_gcp_status()

        # Short placeholders intentionally: long URL placeholders
        # (e.g. "platform.openai.com") inflate QLineEdit's preferred
        # width, which trips QFormLayout's "wrap field below label"
        # fallback when the form is even slightly cramped vertically.
        # Short hints keep widths predictable so labels + fields stay
        # on the same row regardless of window size.
        self._key_inputs: dict[str, QLineEdit] = {}
        key_providers = [
            ("gemini", "Gemini", "paste key"),
            ("claude", "Claude", "sk-ant-..."),
            # Aliyun DashScope key — used by BOTH Qwen LiveTranslate STT
            # AND Qwen-MT translator (same key for both).
            ("dashscope", "DashScope (Qwen)", "sk-..."),
        ]
        for provider_id, label, hint in key_providers:
            key_input = QLineEdit()
            key_input.setPlaceholderText(hint)
            key_input.setEchoMode(QLineEdit.EchoMode.Password)
            key_input.setMinimumHeight(32)
            # Explicit min width so the field is always visible.
            # Earlier attempt used QSizePolicy.Ignored, which let
            # Qt shrink the widget to ZERO width — inputs disappeared.
            # MinimumWidth keeps a sensible floor without overriding
            # the layout's stretching behavior.
            key_input.setMinimumWidth(200)
            existing = self.settings.get_api_key(provider_id)
            if existing:
                key_input.setText(existing)
            self._key_inputs[provider_id] = key_input
            keys_form.addRow(label, key_input)

        # DashScope region selector — keys are NOT interchangeable
        # between the cn and intl regions (verified 2026-05-13 — user's
        # intl key got HTTP 401 from the cn endpoint). User picks the
        # region matching their account portal:
        #   intl = modelstudio.console.aliyun.com/ap-southeast-1 (Singapore)
        #   cn   = bailian.console.aliyun.com (China mainland)
        self._dashscope_region_combo = QComboBox()
        self._dashscope_region_combo.addItem("International (Singapore)", "intl")
        self._dashscope_region_combo.addItem("China Mainland", "cn")
        current_region = getattr(self.settings, "dashscope_region", "intl")
        idx = self._dashscope_region_combo.findData(current_region)
        if idx >= 0:
            self._dashscope_region_combo.setCurrentIndex(idx)
        self._dashscope_region_combo.currentIndexChanged.connect(
            self._on_dashscope_region_changed
        )
        keys_form.addRow("DashScope region", self._dashscope_region_combo)

        layout.addLayout(keys_form)
        env_note = QLabel("Or use env vars: GEMINI_API_KEY, OPENAI_API_KEY, etc.")
        env_note.setObjectName("hintLabel")
        env_note.setWordWrap(True)
        layout.addWidget(env_note)

        # --- Subtitle style section ---
        # User-customizable text color, background color, and background
        # opacity. Lets the user adapt the subtitle window to videos
        # where the default dark navy bg / white text doesn't sit well
        # (e.g. on bright YouTube content the default may look harsh).
        layout.addLayout(self._section_header("Subtitle style"))
        style_form = QFormLayout()
        style_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        style_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        style_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        style_form.setHorizontalSpacing(12)
        style_form.setVerticalSpacing(8)

        # Text color picker
        self._text_color_btn = QPushButton()
        self._text_color_btn.setObjectName("colorSwatch")
        self._text_color_btn.setFixedHeight(28)
        self._text_color_btn.setMinimumWidth(120)
        self._text_color_btn.clicked.connect(self._pick_text_color)
        style_form.addRow(QLabel("Text color"), self._text_color_btn)

        # Background color picker
        self._bg_color_btn = QPushButton()
        self._bg_color_btn.setObjectName("colorSwatch")
        self._bg_color_btn.setFixedHeight(28)
        self._bg_color_btn.setMinimumWidth(120)
        self._bg_color_btn.clicked.connect(self._pick_bg_color)
        style_form.addRow(QLabel("Background color"), self._bg_color_btn)

        # Background opacity slider
        opacity_row = QHBoxLayout()
        opacity_row.setSpacing(8)
        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(0, 100)
        self._opacity_slider.setValue(int(self.settings.overlay_opacity))
        self._opacity_slider.valueChanged.connect(self._on_opacity_changed)
        self._opacity_label = QLabel(f"{int(self.settings.overlay_opacity)}%")
        self._opacity_label.setMinimumWidth(40)
        opacity_row.addWidget(self._opacity_slider, stretch=1)
        opacity_row.addWidget(self._opacity_label)
        opacity_widget = QWidget()
        opacity_widget.setLayout(opacity_row)
        style_form.addRow(QLabel("Background opacity"), opacity_widget)

        layout.addLayout(style_form)
        self._refresh_color_swatches()

        # --- Session usage section ---
        layout.addLayout(self._section_header("Session usage"))
        self._usage_label = QLabel("Tokens: 0   ·   Cost: $0.0000")
        self._usage_label.setStyleSheet("color: rgba(235, 235, 245, 230); font-size: 13px;")
        layout.addWidget(self._usage_label)
        usage_hint = QLabel("Includes STT (~$0.024/min, billed by audio duration) + translation.")
        usage_hint.setObjectName("hintLabel")
        usage_hint.setWordWrap(True)
        layout.addWidget(usage_hint)

        # --- Save button ---
        layout.addStretch()
        save_btn = QPushButton("Save  Apply")
        save_btn.setObjectName("saveBtn")
        save_btn.clicked.connect(self._on_save)
        layout.addWidget(save_btn)

    def _section_header(self, text: str) -> QHBoxLayout:
        """Build a small uppercase section-header layout per macOS HIG.
        Returns a QHBoxLayout containing the label (left-aligned) and
        a thin hairline separator (right-aligned, fills the rest of
        the row). Visually mirrors System Settings group headers.
        """
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        from PySide6.QtWidgets import QFrame
        lbl = QLabel(text.upper())
        lbl.setObjectName("sectionHeader")
        h.addWidget(lbl)
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Plain)
        h.addWidget(sep, stretch=1)
        return h

    def _form_layout(self) -> QFormLayout:
        """Pre-configured QFormLayout for section content. macOS-style:
        labels right-aligned in their own column, fields stretch to
        fill the rest of the row. RowWrapPolicy explicitly set so
        Qt never decides to put labels on a separate line above their
        fields (the API Keys section was being rendered that way on
        narrower windows — wrap mode kicked in unexpectedly).
        """
        f = QFormLayout()
        f.setHorizontalSpacing(16)
        f.setVerticalSpacing(14)
        f.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        f.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        f.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        f.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        return f

    # ──────────────────────────────────────────────────────────────
    # Google Cloud STT credentials handling
    # ──────────────────────────────────────────────────────────────

    _GCP_CREDS_DIR_NAME = ".captionlm"
    _GCP_CREDS_FILE_NAME = "google-stt-credentials.json"

    def _gcp_creds_dir(self):
        from pathlib import Path
        return Path.home() / self._GCP_CREDS_DIR_NAME

    def _find_existing_gcp_json(self):
        """Return Path to any service-account JSON already in
        ~/.captionlm/, or None. Mirrors the matching logic used by
        google_streaming_stt._find_credentials so the status label
        reflects what the STT engine will actually pick up."""
        import json as _json
        d = self._gcp_creds_dir()
        if not d.is_dir():
            return None
        for p in sorted(d.glob("*.json")):
            try:
                with open(p, "r") as f:
                    data = _json.load(f)
                if isinstance(data, dict) and data.get("type") == "service_account":
                    return p
            except Exception:
                continue
        return None

    def _refresh_gcp_status(self):
        """Update the status label next to the Choose JSON button."""
        existing = self._find_existing_gcp_json()
        if existing is not None:
            short = f"~/{self._GCP_CREDS_DIR_NAME}/{existing.name}"
            self._gcp_status_label.setText(f"✓ {short}")
            self._gcp_status_label.setStyleSheet(
                "color: rgb(60, 200, 120); font-size: 12px;"
            )
        else:
            self._gcp_status_label.setText("Not configured")
            self._gcp_status_label.setStyleSheet(
                "color: rgba(235, 235, 245, 180); font-size: 12px;"
            )

    def _on_choose_gcp_json(self):
        """Pick a Service Account JSON via file dialog and copy it
        into ~/.captionlm/. Validates that the file is a real service
        account JSON before copying."""
        import json as _json
        import shutil
        path_str, _filter = QFileDialog.getOpenFileName(
            self,
            "Select Google Cloud service-account JSON",
            "",
            "JSON files (*.json)",
        )
        if not path_str:
            return
        from pathlib import Path
        src = Path(path_str)
        try:
            with open(src, "r") as f:
                data = _json.load(f)
            if not (isinstance(data, dict) and data.get("type") == "service_account"):
                self.show_error(
                    "That file isn't a Google Cloud service-account JSON "
                    "(missing 'type': 'service_account')."
                )
                return
        except Exception as e:
            self.show_error(f"Couldn't read JSON file: {e}")
            return

        target_dir = self._gcp_creds_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / self._GCP_CREDS_FILE_NAME
        try:
            shutil.copy(src, target)
            # Lock down perms — file contains a private key.
            import os
            os.chmod(target, 0o600)
        except Exception as e:
            self.show_error(f"Couldn't save credentials: {e}")
            return
        self._refresh_gcp_status()
        # Notify the pipeline that settings changed so STT picks up the new key.
        self.settings_changed.emit()

    # ──────────────────────────────────────────────────────────────
    # Subtitle style: color pickers + opacity slider
    # ──────────────────────────────────────────────────────────────

    def _on_dashscope_region_changed(self, index: int):
        """User changed DashScope region — persist to settings.
        Doesn't auto-restart the pipeline; user clicks Save Apply to
        take effect."""
        region = self._dashscope_region_combo.currentData()
        if region in ("intl", "cn"):
            self.settings.dashscope_region = region
            logger.info("DashScope region changed to %r", region)

    def _pick_text_color(self):
        """Open a color picker for subtitle text color and apply
        immediately on accept."""
        current = QColor(self.settings.overlay_text_color)
        color = QColorDialog.getColor(
            current, self, "Subtitle text color",
        )
        if color.isValid():
            self.settings.overlay_text_color = color.name().upper()  # "#RRGGBB"
            self._refresh_color_swatches()
            self.settings.save()
            self.settings_changed.emit()

    def _pick_bg_color(self):
        """Open a color picker for overlay background color."""
        current = QColor(self.settings.overlay_bg_color)
        color = QColorDialog.getColor(
            current, self, "Background color",
        )
        if color.isValid():
            self.settings.overlay_bg_color = color.name().upper()
            self._refresh_color_swatches()
            self.settings.save()
            self.settings_changed.emit()

    def _on_opacity_changed(self, value: int):
        """Background opacity slider value changed. Live-apply: update
        the label, store the setting, emit settings_changed so the
        overlay can re-render the bg layer. Saves on slider release
        (via valueChanged firing only on final value if user is
        dragging — Qt fires on every step, which is what we want for
        live preview)."""
        self._opacity_label.setText(f"{int(value)}%")
        self.settings.overlay_opacity = int(value)
        # Don't save on every step (TOML write is wasteful); rely on
        # _on_save() at the end or natural quit-time save.
        self.settings_changed.emit()

    def _refresh_color_swatches(self):
        """Update the color-button swatches to show the currently
        selected text/bg colors. Uses an inline stylesheet so the
        button face directly shows the color (macOS HIG color-well
        style)."""
        try:
            tc = self.settings.overlay_text_color
            bc = self.settings.overlay_bg_color
            self._text_color_btn.setStyleSheet(
                f"QPushButton {{ background-color: {tc}; "
                f"border: 1px solid #3a3a3c; border-radius: 6px; "
                f"color: {'#000000' if QColor(tc).lightnessF() > 0.6 else '#ffffff'}; }}"
            )
            self._text_color_btn.setText(tc)
            self._bg_color_btn.setStyleSheet(
                f"QPushButton {{ background-color: {bc}; "
                f"border: 1px solid #3a3a3c; border-radius: 6px; "
                f"color: {'#000000' if QColor(bc).lightnessF() > 0.6 else '#ffffff'}; }}"
            )
            self._bg_color_btn.setText(bc)
        except Exception as e:
            logger.warning("Could not refresh color swatches: %s", e)

    def update_session_usage(self, tokens: int, cost_usd: float):
        """Slot for pipeline.session_usage_updated. Refreshes the
        live cost meter. Called whenever a translation completes
        AND on the pipeline's 2s tick for STT cost updates during
        silence."""
        if hasattr(self, "_usage_label"):
            self._usage_label.setText(
                f"Tokens: {tokens:,}   ·   Cost: ${cost_usd:.4f}"
            )

    def _on_save(self):
        """Save API keys and apply settings."""
        for provider_id, key_input in self._key_inputs.items():
            key = key_input.text().strip()
            if key:
                self.settings.set_api_key(provider_id, key)
        self.settings.save()
        self.settings_changed.emit()
        self.hide()

    def set_running(self, running: bool):
        """Update UI to reflect running state."""
        self._is_running = running

    def show_error(self, message: str):
        """Display an error message."""
        logger.error("Panel error: %s", message)

    def toggle_or_show(self):
        """Toggle visibility. If hidden, sync and show. If visible, hide."""
        if self.isVisible():
            self.hide()
            return
        self._sync_and_show()

    def show(self):
        """Show panel (called externally). Delegates to _sync_and_show."""
        if self.isVisible():
            self.raise_()
            return
        self._sync_and_show()

    def _sync_and_show(self):
        """Sync combo values with current settings, then show."""
        self._set_combo_value(self.source_combo, self.settings.source_lang)
        self._set_combo_value(self.target_combo, self.settings.target_lang)
        self._set_combo_value(self.stt_combo, self.settings.stt_engine)
        self._set_combo_value(self.trans_combo, self.settings.translation_provider)
        # Refresh API key fields
        for provider_id, key_input in self._key_inputs.items():
            existing = self.settings.get_api_key(provider_id)
            if existing and not key_input.text():
                key_input.setText(existing)
        # Google Cloud STT credentials state may have changed since
        # last show (e.g. user dropped a file in ~/.captionlm/).
        self._refresh_gcp_status()
        super().show()
        self.raise_()
        self.activateWindow()

    def paintEvent(self, event):
        """Draw rounded background — macOS-aligned dark mode color."""
        from PySide6.QtGui import QPainter, QColor
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(30, 30, 32, 250))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(self.rect(), 12, 12)

    def resizeEvent(self, event):
        """Keep the bottom-right size grip pinned to its corner so
        the user can always find it for resizing."""
        super().resizeEvent(event)
        if hasattr(self, "_size_grip") and self._size_grip is not None:
            grip_size = self._size_grip.size()
            self._size_grip.move(
                self.width() - grip_size.width() - 4,
                self.height() - grip_size.height() - 4,
            )
            self._size_grip.raise_()

    def _on_source_changed(self, index: int):
        self.settings.source_lang = self.source_combo.currentData()
        self.settings_changed.emit()

    def _on_target_changed(self, index: int):
        self.settings.target_lang = self.target_combo.currentData()
        self.settings_changed.emit()

    def _on_stt_changed(self, index: int):
        self.settings.stt_engine = self.stt_combo.currentData()
        self.settings_changed.emit()

    def _on_provider_changed(self, index: int):
        self.settings.translation_provider = self.trans_combo.currentData()
        self.settings_changed.emit()

    @staticmethod
    def _set_combo_value(combo: QComboBox, value: str):
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    # --- Drag support ---
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if hasattr(self, '_drag_pos') and self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
