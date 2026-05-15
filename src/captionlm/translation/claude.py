"""Anthropic Claude translation engine (Phase 3)."""

from __future__ import annotations

import asyncio
import logging
import time

from captionlm.translation.base import Translator, TranslationResult

logger = logging.getLogger(__name__)

PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
}

SYSTEM_PROMPT = (
    "You are a real-time subtitle translator. "
    "Translate speech naturally and conversationally. "
    "Return ONLY the translation, no explanations or notes."
)


class ClaudeTranslator(Translator):
    """Anthropic Claude-based translator."""

    def __init__(self, api_key: str, model: str = "claude-haiku-4-5-20251001"):
        self._api_key = api_key
        self._model = model
        self._client = None
        self._init_client()

    def _init_client(self):
        try:
            from anthropic import Anthropic
            self._client = Anthropic(api_key=self._api_key)
        except ImportError:
            raise ImportError(
                "anthropic is required. Install with: pip install 'captionlm[llm]'"
            )

    async def translate(self, text: str, source_lang: str, target_lang: str,
                        context: list[tuple[str, str]] | None = None,
                        on_partial=None) -> TranslationResult:
        # on_partial: streaming callback per Translator interface; this engine
        # does not currently implement streaming, so the kwarg is accepted
        # but ignored. Caller will see only the final TranslationResult.
        if not text.strip():
            return TranslationResult(text="", provider=self.name)

        start = time.monotonic()
        messages = []

        if context:
            ctx = "\n".join(f"{o} → {t}" for o, t in context)
            messages.append({
                "role": "user",
                "content": f"Previous translations for context:\n{ctx}",
            })
            messages.append({"role": "assistant", "content": "Understood."})

        messages.append({
            "role": "user",
            "content": f"Translate from {source_lang} to {target_lang}:\n{text}",
        })

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self._client.messages.create(
                model=self._model,
                system=SYSTEM_PROMPT,
                messages=messages,
                max_tokens=500,
            ),
        )

        translated = response.content[0].text.strip()
        elapsed_ms = (time.monotonic() - start) * 1000

        tokens = response.usage.input_tokens + response.usage.output_tokens
        pricing = PRICING.get(self._model, {"input": 0, "output": 0})
        cost = (response.usage.input_tokens * pricing["input"] +
                response.usage.output_tokens * pricing["output"]) / 1_000_000

        return TranslationResult(
            text=translated,
            provider=self.name,
            tokens_used=tokens,
            cost_usd=cost,
            latency_ms=elapsed_ms,
        )

    def requires_api_key(self) -> bool:
        return True

    def is_free(self) -> bool:
        return False

    @property
    def name(self) -> str:
        return f"Claude ({self._model})"
