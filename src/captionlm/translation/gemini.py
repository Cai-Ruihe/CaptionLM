"""Google Gemini translation engine using the new google-genai SDK.

Uses gemini-2.5-flash — best value for real-time translation.
Pricing: ~$0.15/1M input tokens, ~$0.60/1M output tokens.
Estimated cost: < $0.01 per hour of subtitles.

Paid tier: 2000 RPM (effectively unlimited for our use case).
"""

from __future__ import annotations

import asyncio
import logging
import time

from captionlm.translation.base import Translator, TranslationResult

logger = logging.getLogger(__name__)

# Pricing per 1M tokens (USD)
PRICING = {
    "gemini-2.5-flash": {"input": 0.15, "output": 0.60},
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
}

# Empirical update: short fragments like "あ" alone translated literally to
# "啊" — useless without context. New prompt explicitly tells the LLM the
# fragment is part of a continuous spoken dialogue and asks it to use the
# context to produce a translation that makes sense in flow.
TRANSLATION_PROMPT = """\
You are translating live spoken dialogue from {source_lang} to {target_lang}.

The text below is a CURRENT FRAGMENT of an ongoing conversation. It may be
short or incomplete. Use the prior dialogue context (if any) to produce a
natural, coherent translation that fits the flow of conversation. Do NOT
translate literally word-by-word for ultra-short fragments — instead, give
the most natural rendering given the context.

{context_section}CURRENT FRAGMENT:
{text}

Return ONLY the {target_lang} translation, nothing else."""


class GeminiTranslator(Translator):
    """Google Gemini API translator using google-genai SDK.

    The old google-generativeai package is deprecated and extremely slow
    to import (~180s). google-genai is the replacement — lightweight,
    fast import, same Gemini API underneath.
    """

    MAX_RETRIES = 2

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        # LAZY INIT — empirical fact: `from google import genai` cold-import
        # on Python 3.14 takes 30-60s (no prebuilt wheels for some deps,
        # falls back to source build / dynamic binding generation).
        # If we import in __init__, pipeline init blocks for that whole time
        # before any subtitle can appear. Defer to first translate() call so
        # the pipeline shows "Ready" within seconds.
        self._api_key = api_key
        self._model_name = model
        self._client = None
        self._client_lock = None  # created lazily inside _ensure_client
        # Note: NO _init_client() call here. See _ensure_client() below.

    def _ensure_client(self):
        """Lazily create the Gemini client on first use (thread-safe)."""
        if self._client is not None:
            return self._client
        # Create a lock if needed (cannot use threading.Lock at module load
        # in case threading isn't safely importable yet)
        if self._client_lock is None:
            import threading
            self._client_lock = threading.Lock()
        with self._client_lock:
            if self._client is not None:
                return self._client
            t0 = time.monotonic()
            try:
                from google import genai
            except ImportError:
                raise ImportError(
                    "google-genai is required. Install with: pip install google-genai"
                )
            self._client = genai.Client(api_key=self._api_key)
            logger.info(
                "Gemini client initialized lazily in %.1fs (model=%s)",
                time.monotonic() - t0, self._model_name,
            )
            return self._client

    def _build_prompt(self, text: str, source_lang: str, target_lang: str,
                      context: list[tuple[str, str]] | None) -> str:
        context_section = ""
        if context:
            # Use up to 6 context pairs (was 3) for richer context window.
            # Each pair shows the prior original line and our translation,
            # so the LLM sees the conversation arc.
            pairs = "\n".join(
                f"  [{i+1}] {orig}  →  {trans}"
                for i, (orig, trans) in enumerate(context[-6:])
            )
            context_section = (
                f"PRIOR DIALOGUE (for context, do NOT re-translate):\n"
                f"{pairs}\n\n"
            )

        return TRANSLATION_PROMPT.format(
            source_lang=source_lang,
            target_lang=target_lang,
            context_section=context_section,
            text=text,
        )

    async def translate(self, text: str, source_lang: str, target_lang: str,
                        context: list[tuple[str, str]] | None = None,
                        on_partial=None) -> TranslationResult:
        """Translate via Gemini, always using the streaming API.

        Why always streaming: empirically observed (2026-05-05) that the
        non-streaming generate_content takes 3-4s end-to-end for short
        Japanese→Chinese pairs. Server-Timing header showed dur=3170ms
        (Google-side processing). Streaming doesn't reduce that total time,
        but TTFT (time to first token) is typically <500ms — so when the
        caller passes on_partial, the user sees Chinese characters appear
        ~2.5-3.5 seconds earlier than waiting for the full response.

        When on_partial is None we still use streaming; the only difference
        is we don't invoke any callback. Final text is identical.
        """
        if not text.strip():
            return TranslationResult(text="", provider=self.name)

        start = time.monotonic()
        prompt = self._build_prompt(text, source_lang, target_lang, context)

        loop = asyncio.get_event_loop()
        last_error = None

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                # Lazy-create client on first call (deferred from __init__)
                client = await loop.run_in_executor(None, self._ensure_client)

                # Run the entire stream consumption in an executor thread.
                # Why not the async client (client.aio): keeping a single
                # code path (sync stream + executor) avoids version-specific
                # behavior in the google-genai async API.
                first_chunk_t = [None]  # mutable holder for nested fn
                accumulated = [""]      # ditto

                def _stream_consume():
                    # Disable thinking and AFC for fastest TTFT.
                    #
                    # Empirical (2026-05-05): with default settings, gemini-2.5-flash
                    # gave TTFT ≈ total ≈ 3-9 seconds — server buffers the entire
                    # response and emits it as one SSE event. Hypothesis confirmed
                    # by Gemini docs: 2.5-series models do an internal "thinking"
                    # pass before generating tokens; first token waits for thinking
                    # to complete. For translation we don't need reasoning, so we
                    # set thinking_budget=0 to short-circuit.
                    #
                    # AFC (auto function calling) is also disabled because we
                    # never declare tools — leaving it on adds SDK-side
                    # post-processing overhead per response.
                    try:
                        from google.genai import types as genai_types
                        config = genai_types.GenerateContentConfig(
                            thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                            automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(
                                disable=True,
                            ),
                        )
                    except Exception as cfg_err:
                        # If the SDK version doesn't support these options yet,
                        # log once and fall back to defaults rather than abort.
                        logger.warning(
                            "Could not build fast-path GenerateContentConfig "
                            "(SDK version mismatch?): %s — using defaults",
                            cfg_err,
                        )
                        config = None

                    if config is not None:
                        response = client.models.generate_content_stream(
                            model=self._model_name,
                            contents=prompt,
                            config=config,
                        )
                    else:
                        # Fallback path — SDK didn't accept our config; let it
                        # use defaults (slower but at least functional).
                        response = client.models.generate_content_stream(
                            model=self._model_name,
                            contents=prompt,
                        )
                    for chunk in response:
                        chunk_text = getattr(chunk, "text", None) or ""
                        if not chunk_text:
                            continue
                        if first_chunk_t[0] is None:
                            first_chunk_t[0] = time.monotonic()
                        accumulated[0] += chunk_text
                        if on_partial is not None:
                            try:
                                on_partial(accumulated[0].strip())
                            except Exception as cb_err:
                                # Don't let a UI callback bug abort translation.
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
                    # Promoted DEBUG → INFO 2026-05-13 so console + log
                    # rotation easily show translation latency. After
                    # the real meeting log analysis we want p50/p90
                    # latency observability without --debug flag.
                    logger.info(
                        "Gemini stream: TTFT=%.0fms, total=%.0fms, len=%d",
                        ttft_ms, elapsed_ms, len(translated),
                    )

                # Estimate tokens and cost
                pricing = PRICING.get(self._model_name, {"input": 0.15, "output": 0.60})
                input_tokens = len(prompt) // 4
                output_tokens = len(translated) // 4
                tokens = input_tokens + output_tokens
                cost = (input_tokens * pricing["input"] +
                        output_tokens * pricing["output"]) / 1_000_000

                return TranslationResult(
                    text=translated,
                    provider=self.name,
                    tokens_used=tokens,
                    cost_usd=cost,
                    latency_ms=elapsed_ms,
                )
            except Exception as e:
                last_error = e
                # CRITICAL DEBUG: log full traceback so we can see WHERE the
                # exception came from. Empirical issue: user got "google-genai
                # is required" ImportError in app, but direct test shows
                # _ensure_client succeeds. Need to see actual stack to find
                # the real call site.
                import traceback
                logger.error(
                    "Gemini translate exception attempt %d (type=%s):\n%s",
                    attempt, type(e).__name__, traceback.format_exc(),
                )
                error_str = str(e).lower()
                # Retry on rate limit (429) OR transient server error (503)
                # Empirical: real users hit 503 "high demand" frequently on
                # gemini-2.5-flash. Backoff is shorter for 503 since it's
                # not a quota issue, just a transient blip.
                is_rate = "429" in error_str or "rate" in error_str or "quota" in error_str
                is_503 = "503" in error_str or "unavailable" in error_str
                if (is_rate or is_503) and attempt < self.MAX_RETRIES:
                    delay = 5.0 * (attempt + 1) if is_rate else 1.5 * (attempt + 1)
                    kind = "rate-limited" if is_rate else "503/unavailable"
                    logger.warning("Gemini %s, retry in %.1fs (attempt %d/%d)",
                                   kind, delay, attempt + 1, self.MAX_RETRIES)
                    await asyncio.sleep(delay)
                    continue
                break

        error_str = str(last_error).lower() if last_error else ""
        is_rate_limit = "429" in error_str or "rate" in error_str or "quota" in error_str
        logger.error("Gemini translation failed (rate_limited=%s): %s", is_rate_limit, last_error)
        return TranslationResult(
            text=f"[Gemini error] {text}",
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
        return f"Gemini ({self._model_name})"
