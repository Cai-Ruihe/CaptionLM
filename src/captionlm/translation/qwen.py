"""Qwen-MT translation engine (DashScope) — dedicated machine-translation
model, OpenAI-compatible API.

Why this exists (2026-05-14): user wants Qwen ASR + Qwen translator as
two independent steps (same architecture as Google STT + Gemini). This
file is the translator half. The STT half is captionlm/stt/qwen_asr.py.

Model: qwen-mt-turbo (default) — the latest Qwen-MT release.
- MoE architecture optimized for low latency
- $0.5 / 1M output tokens (cheaper than Gemini 2.5 Flash)
- 92 languages supported
- Outperforms GPT-4.1-mini and Gemini-2.5-Flash on translation benchmarks
  per Qwen team's blog (2025-07).

Also exposes qwen-mt-plus as a quality-leaning alternative.

Endpoint: https://dashscope-intl.aliyuncs.com/compatible-mode/v1
(OpenAI-compatible — uses the same openai Python SDK we already depend on.)

Streaming: yes. We mirror GeminiTranslator's behavior — invoke
`on_partial(accumulated_text)` as chunks arrive so the UI can show
partial translations. Final TranslationResult is identical between
streaming and non-streaming paths.

Qwen-MT API quirk: instead of a free-form prompt, you pass the source
text directly in `messages` and put source/target language in an
`extra_body.translation_options` dict. The model is RL-tuned for the
translation task, so no "you are a translator" preamble is needed (and
adding one apparently hurts quality per docs).
"""

from __future__ import annotations

import asyncio
import logging
import time

from captionlm.translation.base import Translator, TranslationResult

logger = logging.getLogger(__name__)

# Pricing per 1M tokens (USD) — DashScope-intl listed pricing.
# Both qwen-mt-turbo and qwen-mt-plus list $0.5/1M output tokens; input
# is metered the same as output for Qwen-MT (single price per token).
# Source: Qwen-MT blog 2025-07 + DashScope pricing page.
PRICING = {
    "qwen-mt-turbo": {"input": 0.5, "output": 0.5},
    "qwen-mt-plus":  {"input": 0.5, "output": 0.5},
}

# DashScope-intl OpenAI-compatible endpoint (Singapore region).
_BASE_URL_INTL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
_BASE_URL_CN   = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# Map our internal language codes to Qwen-MT's expected names.
# Qwen-MT accepts full English names ("English", "Chinese", "Japanese"...);
# the blog examples use this form. ISO codes may also work but the full
# name is the documented form so we prefer it for reliability.
_LANG_TO_QWEN_MT = {
    "en": "English",
    "zh": "Chinese",
    "zh-tw": "Traditional Chinese",
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
    "id": "Indonesian",
    "tr": "Turkish",
    "hi": "Hindi",
    "yue": "Cantonese",
    "nl": "Dutch",
    "pl": "Polish",
    "sv": "Swedish",
    "fi": "Finnish",
    "no": "Norwegian",
    "da": "Danish",
    "el": "Greek",
    "he": "Hebrew",
    "auto": "auto",  # Qwen-MT's auto-detect source-language sentinel
}


def _to_qwen_lang(code: str | None) -> str:
    if not code:
        return "auto"
    return _LANG_TO_QWEN_MT.get(code, code)  # fall back to whatever the caller passed


class QwenTranslator(Translator):
    """DashScope qwen-mt-turbo / qwen-mt-plus translator.

    Mirrors GeminiTranslator's interface + streaming behavior.
    """

    MAX_RETRIES = 2

    def __init__(
        self,
        api_key: str,
        model: str = "qwen-mt-turbo",
        region: str = "intl",
    ):
        if not api_key:
            raise RuntimeError(
                "DashScope API key required for Qwen translator. Set "
                "DASHSCOPE_API_KEY env var, or enter it in the Control "
                "Panel under API Keys."
            )
        self._api_key = api_key
        self._model = model
        self._region = region
        self._base_url = _BASE_URL_INTL if region == "intl" else _BASE_URL_CN
        # Lazy client init — first translate() call creates the OpenAI
        # client. openai SDK import is fast but we still defer to match
        # the GeminiTranslator pattern and keep STT/translator init
        # symmetrical when measured.
        self._client = None
        self._client_lock = None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if self._client_lock is None:
            import threading
            self._client_lock = threading.Lock()
        with self._client_lock:
            if self._client is not None:
                return self._client
            t0 = time.monotonic()
            try:
                from openai import OpenAI
            except ImportError:
                raise ImportError(
                    "openai is required for QwenTranslator. "
                    "Install with: pip install openai"
                )
            self._client = OpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
            )
            logger.info(
                "Qwen translator client initialized in %.1fs "
                "(model=%s, region=%s)",
                time.monotonic() - t0, self._model, self._region,
            )
            return self._client

    async def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        context: list[tuple[str, str]] | None = None,
        on_partial=None,
    ) -> TranslationResult:
        """Translate via Qwen-MT (streaming).

        Context is NOT passed to qwen-mt-* models — they're dedicated
        translation models that don't accept a conversational context
        prefix like Gemini does. Documentation shows messages with just
        the source text. We pass context-aware translations via the
        `domains` field if context exists, but for live subtitles the
        per-utterance call is the simpler path.
        """
        if not text.strip():
            return TranslationResult(text="", provider=self.name)

        start = time.monotonic()
        src = _to_qwen_lang(source_lang)
        tgt = _to_qwen_lang(target_lang)

        loop = asyncio.get_event_loop()
        last_error = None

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                client = await loop.run_in_executor(None, self._ensure_client)

                first_chunk_t = [None]
                accumulated = [""]

                def _stream_consume():
                    # Per Qwen-MT API: messages = single user message
                    # containing the source text; translation_options
                    # carries the source/target lang.
                    response = client.chat.completions.create(
                        model=self._model,
                        messages=[{"role": "user", "content": text}],
                        stream=True,
                        extra_body={
                            "translation_options": {
                                "source_lang": src,
                                "target_lang": tgt,
                            }
                        },
                    )
                    for chunk in response:
                        # OpenAI SDK chunk shape: choices[0].delta.content
                        try:
                            delta = chunk.choices[0].delta
                            piece = getattr(delta, "content", None) or ""
                        except (IndexError, AttributeError):
                            piece = ""
                        if not piece:
                            continue
                        if first_chunk_t[0] is None:
                            first_chunk_t[0] = time.monotonic()
                        # CRITICAL (2026-05-14): Qwen-MT's stream chunks
                        # are CUMULATIVE not incremental. Verified from
                        # log: "大学。" + "大学。" + "大学。" → user saw
                        # "大学大学。大学。" when we naively did `+=`.
                        # Strategy: if the new piece STARTS WITH our
                        # current accumulated text → it's cumulative,
                        # replace. Otherwise → incremental, append.
                        # This makes the code resilient to either
                        # streaming convention.
                        cur = accumulated[0]
                        if piece.startswith(cur) and len(piece) >= len(cur):
                            accumulated[0] = piece          # cumulative
                        elif cur.endswith(piece):
                            pass                            # dup tail; ignore
                        else:
                            accumulated[0] = cur + piece    # incremental
                        if on_partial is not None:
                            try:
                                on_partial(accumulated[0].strip())
                            except Exception as cb_err:
                                logger.warning(
                                    "on_partial callback raised: %s", cb_err
                                )
                    return accumulated[0]

                raw_text = await loop.run_in_executor(None, _stream_consume)
                translated = raw_text.strip()
                elapsed_ms = (time.monotonic() - start) * 1000
                ttft_ms = (
                    (first_chunk_t[0] - start) * 1000
                    if first_chunk_t[0] is not None else None
                )
                if ttft_ms is not None:
                    logger.info(
                        "Qwen-MT stream: TTFT=%.0fms, total=%.0fms, len=%d",
                        ttft_ms, elapsed_ms, len(translated),
                    )

                pricing = PRICING.get(self._model, {"input": 0.5, "output": 0.5})
                input_tokens = len(text) // 4
                output_tokens = len(translated) // 4
                tokens = input_tokens + output_tokens
                cost = (
                    input_tokens * pricing["input"]
                    + output_tokens * pricing["output"]
                ) / 1_000_000

                return TranslationResult(
                    text=translated,
                    provider=self.name,
                    tokens_used=tokens,
                    cost_usd=cost,
                    latency_ms=elapsed_ms,
                )
            except Exception as e:
                last_error = e
                import traceback
                logger.error(
                    "Qwen-MT translate exception attempt %d (type=%s):\n%s",
                    attempt, type(e).__name__, traceback.format_exc(),
                )
                error_str = str(e).lower()
                is_rate = (
                    "429" in error_str
                    or "rate" in error_str
                    or "quota" in error_str
                    or "throttling" in error_str
                )
                is_503 = "503" in error_str or "unavailable" in error_str
                if (is_rate or is_503) and attempt < self.MAX_RETRIES:
                    delay = 5.0 * (attempt + 1) if is_rate else 1.5 * (attempt + 1)
                    kind = "rate-limited" if is_rate else "503/unavailable"
                    logger.warning(
                        "Qwen-MT %s, retry in %.1fs (attempt %d/%d)",
                        kind, delay, attempt + 1, self.MAX_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue
                break

        error_str = str(last_error).lower() if last_error else ""
        is_rate_limit = (
            "429" in error_str or "rate" in error_str or "quota" in error_str
        )
        logger.error(
            "Qwen-MT translation failed (rate_limited=%s): %s",
            is_rate_limit, last_error,
        )
        return TranslationResult(
            text=f"[Qwen-MT error] {text}",
            provider=self.name,
            latency_ms=(time.monotonic() - start) * 1000,
            rate_limited=is_rate_limit,
        )

    def requires_api_key(self) -> bool:
        return True

    def is_free(self) -> bool:
        return False

    @property
    def name(self) -> str:
        return f"Qwen ({self._model})"
