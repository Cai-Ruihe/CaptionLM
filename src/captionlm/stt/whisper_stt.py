"""Whisper-based speech-to-text using faster-whisper.

Optimized for real-time subtitle translation:
- beam_size=1 (greedy decoding) for lowest latency
- VAD filter to skip silence
- Runs inference in a thread pool to keep asyncio responsive

Model recommendations for real-time use:
- "base"  (150 MB) — best latency, good for clear audio
- "small" (500 MB) — best balance of speed and accuracy
- "medium" (1.5 GB) — high accuracy, ~2x slower than small
"""

from __future__ import annotations

import asyncio
import logging
import time

import numpy as np

from captionlm.stt.base import STTEngine

logger = logging.getLogger(__name__)

MODEL_SIZES = {
    "tiny": {"download_mb": 75, "description": "Fastest, lowest accuracy"},
    "base": {"download_mb": 150, "description": "Fast, good for real-time"},
    "small": {"download_mb": 500, "description": "Recommended balance"},
    "medium": {"download_mb": 1500, "description": "High accuracy"},
    "large-v3": {"download_mb": 3000, "description": "Best accuracy"},
}


class WhisperSTT(STTEngine):
    """Speech-to-text using faster-whisper (CTranslate2 backend).

    Optimized for real-time subtitle use:
    - Greedy decoding (beam_size=1) for ~3x faster inference vs beam_size=5
    - VAD filter skips silence segments automatically
    - Apple Silicon optimized via CTranslate2 ARM backend
    """

    def __init__(self, model_size: str = "base", language: str = "en"):
        self._model_size = model_size
        self._language = language
        self._model = None
        self._load_model()

    def _load_model(self):
        """Load the Whisper model.

        First load downloads the model from HuggingFace (~150MB for base).
        Subsequent loads use the cached version (~1 second).
        """
        try:
            from faster_whisper import WhisperModel

            t0 = time.monotonic()
            # cpu + int8 is fastest on Apple Silicon for real-time use
            self._model = WhisperModel(
                self._model_size,
                device="cpu",
                compute_type="int8",
            )
            elapsed = time.monotonic() - t0
            logger.info("Whisper model loaded: %s (%.1fs)", self._model_size, elapsed)
        except ImportError:
            raise ImportError(
                "faster-whisper is required for Whisper STT. "
                "Install with: pip install faster-whisper"
            )
        except Exception as e:
            logger.error("Failed to load Whisper model: %s", e)
            raise

    async def transcribe(self, audio: np.ndarray) -> str | None:
        """Transcribe audio using Whisper."""
        if self._model is None:
            return None

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._transcribe_sync, audio)

    def _transcribe_sync(self, audio: np.ndarray) -> str | None:
        """Synchronous transcription (runs in thread pool).

        Uses greedy decoding (beam_size=1) for lowest latency.
        VAD filter automatically skips silence segments.
        """
        t0 = time.monotonic()
        segments, info = self._model.transcribe(
            audio,
            language=self._language if self._language != "auto" else None,
            beam_size=1,           # greedy — fastest for real-time
            best_of=1,             # no sampling alternatives
            vad_filter=True,
            vad_parameters={
                "min_silence_duration_ms": 300,   # faster sentence splits
                "speech_pad_ms": 200,
            },
            without_timestamps=True,  # skip timestamp computation
        )

        texts = []
        for segment in segments:
            text = segment.text.strip()
            if text:
                texts.append(text)

        result = " ".join(texts)
        elapsed_ms = (time.monotonic() - t0) * 1000
        if result:
            logger.debug("Whisper STT (%.0fms): %s", elapsed_ms, result[:60])
        return result if result else None

    @property
    def name(self) -> str:
        return f"Whisper {self._model_size}"

    @property
    def requires_download(self) -> bool:
        return True

    @property
    def download_size_mb(self) -> int:
        return MODEL_SIZES.get(self._model_size, {}).get("download_mb", 0)
