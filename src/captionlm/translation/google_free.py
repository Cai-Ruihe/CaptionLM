"""Free Google Translate integration (no API key required).

Uses the deep-translator library which wraps the free Google Translate
web API. This is the default translation engine for zero-config startup.

Known limitations:
- Rate limiting may occur with heavy usage
- Translation quality is moderate (no context awareness)
- May break if Google changes their API (historically stable though)
"""

from __future__ import annotations

import asyncio
import logging
import time

from captionlm.translation.base import Translator, TranslationResult

logger = logging.getLogger(__name__)

# Language code mapping: CaptionLM internal codes → Google Translate codes
LANG_MAP = {
    "en": "en",
    "zh": "zh-CN",
    "zh-tw": "zh-TW",
    "ja": "ja",
    "ko": "ko",
    "fr": "fr",
    "de": "de",
    "es": "es",
    "pt": "pt",
    "ru": "ru",
    "ar": "ar",
    "it": "it",
    "vi": "vi",
    "th": "th",
    "id": "id",
    "hi": "hi",
}


class GoogleFreeTranslator(Translator):
    """Free Google Translate — no API key, no cost.

    This is the default translator for Phase 1.
    Quality is decent for common language pairs.
    Context-aware translation is not supported (each sentence
    is translated independently).
    """

    def __init__(self):
        self._translator = None
        self._last_source = None
        self._last_target = None

    def _get_translator(self, source: str, target: str):
        """Lazy-initialize the translator for the given language pair."""
        from deep_translator import GoogleTranslator

        src = LANG_MAP.get(source, source)
        tgt = LANG_MAP.get(target, target)

        if self._translator is None or src != self._last_source or tgt != self._last_target:
            self._translator = GoogleTranslator(source=src, target=tgt)
            self._last_source = src
            self._last_target = tgt

        return self._translator

    async def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        context: list[tuple[str, str]] | None = None,
        on_partial=None,
    ) -> TranslationResult:
        """Translate using free Google Translate.

        Note: context parameter is accepted but ignored — Google Translate
        free API doesn't support context-aware translation. LLM-based
        translators use the context for improved coherence.

        on_partial parameter is accepted but ignored — this engine returns
        atomic results, not streaming chunks.
        """
        if not text.strip():
            return TranslationResult(text="", provider=self.name)

        # Same language — no translation needed
        if source_lang == target_lang:
            return TranslationResult(text=text, provider=self.name)

        start = time.monotonic()

        loop = asyncio.get_event_loop()
        translator = self._get_translator(source_lang, target_lang)

        try:
            translated = await loop.run_in_executor(
                None, translator.translate, text
            )
        except Exception as e:
            error_str = str(e).lower()
            is_rate = "too many" in error_str or "rate" in error_str or "limit" in error_str
            logger.error("Google Translate error (rate_limited=%s): %s", is_rate, e)
            return TranslationResult(
                text=f"[Translation error] {text}",
                provider=self.name,
                latency_ms=(time.monotonic() - start) * 1000,
                rate_limited=is_rate,
            )

        elapsed_ms = (time.monotonic() - start) * 1000
        logger.debug(
            "Translated (%s→%s, %.0fms): %s → %s",
            source_lang, target_lang, elapsed_ms, text[:50], translated[:50],
        )

        return TranslationResult(
            text=translated or text,
            provider=self.name,
            tokens_used=0,
            cost_usd=0.0,
            latency_ms=elapsed_ms,
        )

    def requires_api_key(self) -> bool:
        return False

    def is_free(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "Google Translate (Free)"
