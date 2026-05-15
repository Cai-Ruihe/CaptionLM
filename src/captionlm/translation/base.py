"""Abstract base class for translation engines."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class TranslationResult:
    """Result from a translation operation."""

    text: str               # Translated text
    provider: str           # Engine name (e.g., "Google Translate", "GPT-4o-mini")
    tokens_used: int = 0    # Token consumption (0 for free engines)
    cost_usd: float = 0.0   # Estimated cost in USD
    latency_ms: float = 0.0 # Translation latency in milliseconds
    rate_limited: bool = False  # True if this result was affected by rate limiting


class Translator(ABC):
    """Interface for translation engines.

    All translators accept an optional context parameter containing
    previous (original, translated) pairs for coherent multi-sentence
    translation.
    """

    @abstractmethod
    async def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        context: list[tuple[str, str]] | None = None,
        on_partial: Optional[Callable[[str], None]] = None,
    ) -> TranslationResult:
        """Translate text from source to target language.

        Args:
            text: Text to translate.
            source_lang: Source language code (e.g., "en", "zh", "ja").
            target_lang: Target language code.
            context: Previous N (original, translated) pairs for context.
            on_partial: Optional callback invoked with the partial translated
                text accumulated so far whenever a new streaming chunk arrives.
                Engines that support streaming SHOULD honor this; engines that
                don't may ignore it (the kwarg has a safe default). The
                callback receives the FULL accumulated text each time, not
                just the delta — UIs should replace, not append.
                Threading: callbacks may be invoked from a worker thread; use
                thread-safe signal mechanisms (e.g., Qt QueuedConnection) when
                forwarding to UI.

        Returns:
            TranslationResult with translated text and metadata.
        """
        ...

    @abstractmethod
    def requires_api_key(self) -> bool:
        """Whether this engine requires an API key."""
        ...

    @abstractmethod
    def is_free(self) -> bool:
        """Whether this engine is completely free (no rate limits)."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name of this translation engine."""
        ...
