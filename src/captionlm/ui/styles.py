"""QSS styles and theming for CaptionLM UI."""

# Color palette
COLORS = {
    "bg_dark": "#1a1a2e",
    "bg_panel": "#16213e",
    "bg_card": "#0f3460",
    "accent": "#e94560",
    "accent_hover": "#ff6b6b",
    "text_primary": "#ffffff",
    "text_secondary": "#a0a0b0",
    "text_muted": "#6c6c80",
    "success": "#4ade80",
    "warning": "#fbbf24",
    "error": "#ef4444",
    "border": "#2a2a4a",
}

CONTROL_PANEL_STYLE = """
QWidget#ControlPanel {
    background-color: #1a1a2e;
    border-radius: 16px;
}
QLabel {
    color: #ffffff;
    font-size: 14px;
}
QLabel#title {
    font-size: 22px;
    font-weight: bold;
    color: #ffffff;
}
QLabel#subtitle {
    font-size: 12px;
    color: #a0a0b0;
}
QPushButton {
    background-color: #0f3460;
    color: #ffffff;
    border: 1px solid #2a2a4a;
    border-radius: 12px;
    padding: 10px 24px;
    font-size: 14px;
    font-weight: bold;
}
QPushButton:hover {
    background-color: #1a4a7a;
    border-color: #e94560;
}
QPushButton#startBtn {
    background-color: #e94560;
    border: none;
    border-radius: 14px;
    font-size: 16px;
    padding: 14px 32px;
}
QPushButton#startBtn:hover {
    background-color: #ff6b6b;
}
QPushButton#stopBtn {
    background-color: #4a1a2e;
    border: 1px solid #e94560;
    border-radius: 14px;
}
QPushButton#stopBtn:hover {
    background-color: #6a2a3e;
}
QComboBox {
    background-color: #0f3460;
    color: #ffffff;
    border: 1px solid #2a2a4a;
    border-radius: 10px;
    padding: 8px 14px;
    font-size: 13px;
    min-width: 160px;
}
QComboBox:hover {
    border-color: #e94560;
}
QComboBox::drop-down {
    border: none;
    width: 30px;
}
QComboBox QAbstractItemView {
    background-color: #16213e;
    color: #ffffff;
    selection-background-color: #0f3460;
    border: 1px solid #2a2a4a;
    border-radius: 8px;
}
QGroupBox {
    color: #a0a0b0;
    font-size: 13px;
    font-weight: bold;
    border: 1px solid #2a2a4a;
    border-radius: 14px;
    margin-top: 16px;
    padding-top: 20px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 14px;
    padding: 0 8px;
}
QStatusBar {
    color: #a0a0b0;
    font-size: 12px;
}
"""

OVERLAY_STYLE = """
QLabel#originalText {
    color: rgba(200, 200, 220, 200);
    font-size: {original_size}px;
}
QLabel#translatedText {
    color: rgba(255, 255, 255, 240);
    font-size: {translated_size}px;
    font-weight: bold;
}
"""
