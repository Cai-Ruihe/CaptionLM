"""First-run wizard for CaptionLM.

Guides new users through:
1. Welcome + what CaptionLM does
2. Audio permission request (macOS)
3. Language pair selection
4. Ready to go!

Goal: user sees their first subtitle within 60 seconds of install.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QStackedWidget, QWidget,
)

from captionlm.config.settings import Settings, LANGUAGES


class FirstRunWizard(QDialog):
    """Setup wizard shown on first launch."""

    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.setWindowTitle("Welcome to CaptionLM")
        self.setFixedSize(500, 400)
        self.setStyleSheet("""
            QDialog {
                background-color: #1a1a2e;
            }
            QLabel {
                color: #ffffff;
            }
            QPushButton {
                background-color: #e94560;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 10px 24px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #ff6b6b;
            }
            QPushButton#secondary {
                background-color: #0f3460;
                border: 1px solid #2a2a4a;
            }
            QComboBox {
                background-color: #0f3460;
                color: #ffffff;
                border: 1px solid #2a2a4a;
                border-radius: 6px;
                padding: 8px 12px;
                font-size: 14px;
                min-width: 200px;
            }
            QComboBox QAbstractItemView {
                background-color: #16213e;
                color: #ffffff;
                selection-background-color: #0f3460;
            }
        """)

        self._current_page = 0
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 24, 32, 24)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._create_welcome_page())
        self.stack.addWidget(self._create_language_page())
        self.stack.addWidget(self._create_ready_page())
        layout.addWidget(self.stack)

        # Navigation buttons
        nav = QHBoxLayout()
        self.back_btn = QPushButton("Back")
        self.back_btn.setObjectName("secondary")
        self.back_btn.clicked.connect(self._go_back)
        self.back_btn.hide()
        nav.addWidget(self.back_btn)

        nav.addStretch()

        self.next_btn = QPushButton("Get Started")
        self.next_btn.clicked.connect(self._go_next)
        nav.addWidget(self.next_btn)

        layout.addLayout(nav)

    def _create_welcome_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title = QLabel("Welcome to CaptionLM")
        title.setFont(QFont(".AppleSystemUIFont", 24, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        layout.addSpacing(16)

        desc = QLabel(
            "Real-time subtitle translation for any audio on your desktop.\n\n"
            "CaptionLM captures audio from any app — browser, video calls,\n"
            "games — and displays translated subtitles in real time.\n\n"
            "No API key needed. Works out of the box."
        )
        desc.setFont(QFont(".AppleSystemUIFont", 14))
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc.setStyleSheet("color: #a0a0b0;")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        return page

    def _create_language_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title = QLabel("Choose Your Languages")
        title.setFont(QFont(".AppleSystemUIFont", 20, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        layout.addSpacing(24)

        # Source language
        src_label = QLabel("I want to translate FROM:")
        src_label.setFont(QFont(".AppleSystemUIFont", 14))
        layout.addWidget(src_label)

        self.source_combo = QComboBox()
        for code, name in LANGUAGES.items():
            self.source_combo.addItem(name, code)
        # Default: English
        idx = self.source_combo.findData("en")
        if idx >= 0:
            self.source_combo.setCurrentIndex(idx)
        layout.addWidget(self.source_combo)

        layout.addSpacing(16)

        # Target language
        tgt_label = QLabel("Translate TO:")
        tgt_label.setFont(QFont(".AppleSystemUIFont", 14))
        layout.addWidget(tgt_label)

        self.target_combo = QComboBox()
        for code, name in LANGUAGES.items():
            self.target_combo.addItem(name, code)
        # Default: Chinese
        idx = self.target_combo.findData("zh")
        if idx >= 0:
            self.target_combo.setCurrentIndex(idx)
        layout.addWidget(self.target_combo)

        return page

    def _create_ready_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title = QLabel("You're All Set!")
        title.setFont(QFont(".AppleSystemUIFont", 24, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        layout.addSpacing(16)

        desc = QLabel(
            "CaptionLM will now start capturing audio and\n"
            "displaying translated subtitles.\n\n"
            "Tips:\n"
            "• Drag the subtitle bar to reposition it\n"
            "• Double-click subtitles to switch display mode\n"
            "• Right-click the tray icon for more options\n"
            "• Change settings anytime from the control panel"
        )
        desc.setFont(QFont(".AppleSystemUIFont", 14))
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc.setStyleSheet("color: #a0a0b0;")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        note = QLabel(
            "Note: On macOS, you may need to grant microphone\n"
            "permission in System Settings → Privacy & Security."
        )
        note.setFont(QFont(".AppleSystemUIFont", 12))
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        note.setStyleSheet("color: #fbbf24;")
        note.setWordWrap(True)
        layout.addWidget(note)

        return page

    def _go_next(self):
        if self._current_page == 0:
            # Welcome → Language selection
            self._current_page = 1
            self.stack.setCurrentIndex(1)
            self.back_btn.show()
            self.next_btn.setText("Continue")
        elif self._current_page == 1:
            # Save language selections
            self.settings.source_lang = self.source_combo.currentData()
            self.settings.target_lang = self.target_combo.currentData()
            self._current_page = 2
            self.stack.setCurrentIndex(2)
            self.next_btn.setText("Start CaptionLM")
        elif self._current_page == 2:
            # Done!
            self.accept()

    def _go_back(self):
        if self._current_page > 0:
            self._current_page -= 1
            self.stack.setCurrentIndex(self._current_page)
            if self._current_page == 0:
                self.back_btn.hide()
                self.next_btn.setText("Get Started")
            else:
                self.next_btn.setText("Continue")
