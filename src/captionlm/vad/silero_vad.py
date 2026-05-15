"""Voice Activity Detection using Silero VAD (Phase 2).

Silero VAD detects speech segments in audio, allowing the pipeline
to skip silent periods and only send speech segments to STT.
This reduces API costs and improves latency.
"""

from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)


class SileroVAD:
    """Voice Activity Detection using Silero VAD model.

    Placeholder for Phase 2 implementation.
    """

    def __init__(self, threshold: float = 0.5, sample_rate: int = 16000):
        self.threshold = threshold
        self.sample_rate = sample_rate
        self._model = None
        logger.info("SileroVAD initialized (threshold=%.2f)", threshold)

    def is_speech(self, audio: np.ndarray) -> bool:
        """Check if audio chunk contains speech.

        Args:
            audio: Float32 numpy array, mono, 16kHz.

        Returns:
            True if speech is detected above threshold.
        """
        # Simple energy-based detection as placeholder
        # Phase 2 will use actual Silero VAD model
        energy = np.sqrt(np.mean(audio**2))
        return energy > 0.01
