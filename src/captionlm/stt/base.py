"""Abstract base class for speech-to-text engines."""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class STTEngine(ABC):
    """Interface for speech-to-text engines.

    Implementations include system-native speech recognition (zero setup)
    and Whisper models (higher accuracy, requires model download).

    Two operating modes:
    1. Chunk-based (default): pipeline captures audio, chunks it on silence
       boundaries, calls transcribe(audio_chunk) for each chunk.
    2. Self-contained: the engine handles its own audio capture and emits
       transcripts asynchronously. Pipeline calls next_transcript() to pull
       results. Used by AppleSpeechAnalyzerSTT (macOS 26+) where the Swift
       SpeechAnalyzer process owns the audio pipeline end-to-end for
       maximum efficiency.
    """

    # Override to True in engines that handle their own audio capture.
    # Pipeline detects this and skips its own AudioCapture / chunking logic.
    is_self_contained: bool = False

    # Override to True in engines that ALSO produce target-language
    # translation as part of their normal output (e.g. Qwen LiveTranslate).
    # When True, pipeline:
    #   - uses next_translation() instead of next_transcript()
    #   - SKIPS its own translator call (no Gemini/OpenAI invoked)
    #   - emits subtitle_ready directly with (orig, trans) from the engine
    # When False (default), pipeline assumes orig-language transcription
    # and routes through its translator. Most engines (Google, Apple,
    # Whisper) keep False since they only do STT.
    provides_translation: bool = False

    @abstractmethod
    async def transcribe(self, audio: np.ndarray) -> str | None:
        """Transcribe an audio chunk to text.

        Args:
            audio: Float32 numpy array, mono, 16kHz, normalized to [-1, 1].

        Returns:
            Transcribed text, or None if no speech detected.

        For self-contained engines this method may be a no-op.
        """
        ...

    async def next_transcript(self) -> tuple[str, bool] | None:
        """For self-contained engines: pull the next available transcript.

        Returns:
            (text, is_final) tuple, or None if no transcript available.
            is_final=True indicates a finalized sentence; False is a partial
            in-progress result (typically lower-confidence, may be revised).

        Default implementation raises — only override in self-contained engines.
        """
        raise NotImplementedError(
            "next_transcript() is only valid for self-contained STT engines"
        )

    async def drain_transcripts(self) -> list[tuple[str, bool]]:
        """Drain ALL pending transcripts from the engine, blocking only for
        the first one. Subsequent results are pulled non-blocking until queue
        is empty.

        This exists to prevent latency accumulation under sustained speech:
        when STT produces partials faster than the translator can keep up
        (~5/sec vs ~1/sec), the queue grows unbounded and pipeline ends up
        translating audio from minutes ago. Pipeline calls drain_transcripts()
        instead of next_transcript() to skip ahead — finals are preserved
        (utterance boundaries are critical) but stale non-finals can be dropped.

        Default implementation calls next_transcript() once. Override in
        self-contained engines for true draining behavior.
        """
        first = await self.next_transcript()
        return [first] if first is not None else []

    async def next_translation(self) -> tuple[str, str, bool] | None:
        """For engines with provides_translation=True: pull the next
        (orig, trans, is_final) triple from the engine's translation
        stream.

        Returns:
            (original_text, translated_text, is_final), or None if
            nothing's available. is_final=True means the utterance
            has finalized and downstream can commit it to history.

        Default raises — only override in provides_translation engines.
        """
        raise NotImplementedError(
            "next_translation() is only valid for engines with "
            "provides_translation=True"
        )

    async def drain_translations(self) -> list[tuple[str, str, bool]]:
        """Drain all pending (orig, trans, is_final) triples. Same
        latency-accumulation defense as drain_transcripts but for
        provides_translation engines."""
        first = await self.next_translation()
        return [first] if first is not None else []

    async def stop(self) -> None:
        """Clean up engine resources (subprocess, model, etc.).

        Default no-op. Override if your engine holds external resources.
        """
        return

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name of this STT engine."""
        ...

    @property
    def requires_download(self) -> bool:
        """Whether this engine requires downloading a model first."""
        return False

    @property
    def download_size_mb(self) -> int:
        """Approximate download size in MB (0 if no download needed)."""
        return 0
