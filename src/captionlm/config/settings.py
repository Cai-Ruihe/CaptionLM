"""Settings management for CaptionLM.

Settings are stored in ~/.config/captionlm/settings.toml
and loaded/saved using tomli/tomli-w.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

logger = logging.getLogger(__name__)

# Supported languages (code → display name)
LANGUAGES = {
    "en": "English",
    "zh": "Chinese (Simplified)",
    "zh-tw": "Chinese (Traditional)",
    "ja": "Japanese",
    "ko": "Korean",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "pt": "Portuguese",
    "ru": "Russian",
    "ar": "Arabic",
    "it": "Italian",
    "vi": "Vietnamese",
    "th": "Thai",
    "hi": "Hindi",
}

# STT engine options
STT_ENGINES = {
    "google_streaming": "Google Cloud Speech (Real-time, ~$0.024/min)",
    "speech_analyzer": "Apple SpeechAnalyzer (macOS 26+, batched ~10s lag)",
    "qwen_asr": "Qwen ASR (Streaming, ~$0.0054/min)",
    "qwen_livetranslate": "Qwen LiveTranslate (Audio→Translation, ~3s lag)",
    "system": "System Native (Quick Start)",
    "whisper": "Whisper (Local, slower load)",
}

# Translation provider options
TRANSLATION_PROVIDERS = {
    "google_free": "Google Translate (Free)",
    "gemini": "Gemini 2.5 Flash (~$0.01/hr)",
    "qwen": "Qwen-MT Turbo (Dedicated translation, fast)",
    "claude": "Anthropic Claude",
}


def _config_dir() -> Path:
    """Get the config directory path."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "CaptionLM"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home())) / "CaptionLM"
    else:
        base = Path.home() / ".config" / "captionlm"
    base.mkdir(parents=True, exist_ok=True)
    return base


@dataclass
class Settings:
    """Application settings with sensible defaults."""

    # First run flag
    is_first_run: bool = True

    # Language settings
    source_lang: str = "en"
    target_lang: str = "zh"

    # STT settings — default to SpeechAnalyzer on macOS 26+ (instant load,
    # ANE-accelerated, ~55% faster than Whisper per Apple's WWDC25 benchmark).
    stt_engine: str = "speech_analyzer" # "speech_analyzer", "system", or "whisper"
    whisper_model_size: str = "base"    # "tiny", "base", "small", "medium", "large-v3"

    # Translation settings
    translation_provider: str = "google_free"

    # DashScope (Aliyun) region — used by qwen_livetranslate STT.
    # "intl" → wss://dashscope-intl.aliyuncs.com (Singapore, ap-southeast-1)
    # "cn"   → wss://dashscope.aliyuncs.com      (China mainland)
    # Keys from one region do NOT work with the other endpoint (verified
    # 2026-05-13 — user's intl key got HTTP 401 from the cn endpoint).
    # Default to intl since most users outside mainland China will be
    # signing up via the international portal.
    dashscope_region: str = "intl"

    # Overlay display settings
    display_mode: str = "bilingual"     # "bilingual", "translation_only", "original_only"
    font_family: str = ".AppleSystemUIFont"
    original_font_size: int = 13
    translated_font_size: int = 18
    overlay_opacity: int = 80           # 0-100, applies to BACKGROUND
    overlay_text_color: str = "#FFFFFF"      # hex string for subtitle text
    overlay_bg_color: str = "#0F0F1E"        # hex string for window bg (alpha = overlay_opacity)
    click_through: bool = False

    # API keys (stored separately for security)
    _api_keys: dict[str, str] = field(default_factory=dict)

    # Environment variable names for API keys
    _ENV_KEY_MAP = {
        "gemini": "GEMINI_API_KEY",
        "claude": "ANTHROPIC_API_KEY",
        # Aliyun DashScope key — used by Qwen LiveTranslate STT engine
        # AND the Qwen-MT translator (same key for both).
        "dashscope": "DASHSCOPE_API_KEY",
    }

    def get_api_key(self, provider: str) -> str:
        """Get API key for a provider.

        Priority: saved setting > environment variable.
        """
        key = self._api_keys.get(provider, "")
        if not key:
            env_var = self._ENV_KEY_MAP.get(provider, "")
            if env_var:
                key = os.environ.get(env_var, "")
        return key

    def set_api_key(self, provider: str, key: str):
        """Set API key for a provider."""
        self._api_keys[provider] = key

    def save(self):
        """Save settings to TOML file."""
        import tomli_w

        config_path = _config_dir() / "settings.toml"
        data = {
            "general": {
                "is_first_run": self.is_first_run,
            },
            "language": {
                "source": self.source_lang,
                "target": self.target_lang,
            },
            "stt": {
                "engine": self.stt_engine,
                "whisper_model": self.whisper_model_size,
            },
            "translation": {
                "provider": self.translation_provider,
            },
            "dashscope": {
                "region": self.dashscope_region,
            },
            "overlay": {
                "display_mode": self.display_mode,
                "font_family": self.font_family,
                "original_font_size": self.original_font_size,
                "translated_font_size": self.translated_font_size,
                "opacity": self.overlay_opacity,
                "text_color": self.overlay_text_color,
                "bg_color": self.overlay_bg_color,
                "click_through": self.click_through,
            },
            "api_keys": dict(self._api_keys),
        }

        with open(config_path, "wb") as f:
            tomli_w.dump(data, f)
        logger.info("Settings saved to %s", config_path)

    @classmethod
    def load(cls) -> Settings:
        """Load settings from TOML file, or return defaults."""
        config_path = _config_dir() / "settings.toml"
        if not config_path.exists():
            logger.info("No settings file found, using defaults")
            return cls()

        try:
            if sys.version_info >= (3, 11):
                import tomllib
                with open(config_path, "rb") as f:
                    data = tomllib.load(f)
            else:
                import tomli
                with open(config_path, "rb") as f:
                    data = tomli.load(f)

            settings = cls()
            general = data.get("general", {})
            settings.is_first_run = general.get("is_first_run", True)

            lang = data.get("language", {})
            settings.source_lang = lang.get("source", "en")
            settings.target_lang = lang.get("target", "zh")

            stt = data.get("stt", {})
            settings.stt_engine = stt.get("engine", "system")
            settings.whisper_model_size = stt.get("whisper_model", "small")

            trans = data.get("translation", {})
            settings.translation_provider = trans.get("provider", "google_free")

            ds = data.get("dashscope", {})
            settings.dashscope_region = ds.get("region", "intl")

            overlay = data.get("overlay", {})
            settings.display_mode = overlay.get("display_mode", "bilingual")
            settings.font_family = overlay.get("font_family", ".AppleSystemUIFont")
            settings.original_font_size = overlay.get("original_font_size", 14)
            settings.translated_font_size = overlay.get("translated_font_size", 20)
            settings.overlay_opacity = overlay.get("opacity", 80)
            settings.overlay_text_color = overlay.get("text_color", "#FFFFFF")
            settings.overlay_bg_color = overlay.get("bg_color", "#0F0F1E")
            settings.click_through = overlay.get("click_through", False)

            settings._api_keys = data.get("api_keys", {})

            logger.info("Settings loaded from %s", config_path)
            return settings

        except Exception as e:
            logger.warning("Failed to load settings: %s. Using defaults.", e)
            return cls()
