"""Abstract base class for audio capture backends."""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class AudioCapture(ABC):
    """Interface for capturing system audio.

    Implementations must handle platform-specific audio capture
    (ScreenCaptureKit on macOS, WASAPI on Windows).
    """

    def __init__(self, sample_rate: int = 16000, channels: int = 1):
        self.sample_rate = sample_rate
        self.channels = channels

    @abstractmethod
    def start(self) -> None:
        """Start capturing audio."""
        ...

    @abstractmethod
    def stop(self) -> None:
        """Stop capturing audio and release resources."""
        ...

    @abstractmethod
    def read_chunk(self, duration_ms: int = 500) -> np.ndarray | None:
        """Read an audio chunk.

        Args:
            duration_ms: Desired chunk duration in milliseconds.

        Returns:
            Float32 numpy array of audio samples (mono, normalized to [-1, 1]),
            or None if no audio is available.
        """
        ...

    @property
    @abstractmethod
    def is_capturing(self) -> bool:
        """Whether audio capture is currently active."""
        ...
