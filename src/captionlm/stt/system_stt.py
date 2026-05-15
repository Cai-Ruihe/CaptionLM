"""macOS system-native speech recognition using SFSpeechRecognizer.

This is the "Quick Start" tier: zero download, instant startup.
Uses Apple's on-device speech recognition (macOS 13+).

Accuracy is moderate compared to Whisper, but the zero-friction
experience makes it ideal for first-time users.

Note: SFSpeechRecognizer requires the Speech framework via PyObjC.
If PyObjC is not installed, this falls back to a simple
energy-based silence detector that passes audio through
without transcription (useful for testing the pipeline).
"""

from __future__ import annotations

import asyncio
import io
import logging
import struct
import tempfile
import wave

import numpy as np

from captionlm.stt.base import STTEngine

logger = logging.getLogger(__name__)


def _audio_to_wav_bytes(audio: np.ndarray, sample_rate: int = 16000) -> bytes:
    """Convert float32 numpy audio to WAV bytes."""
    # Convert float32 [-1, 1] to int16
    audio_int16 = (audio * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_int16.tobytes())
    return buf.getvalue()


class MacOSSystemSTT(STTEngine):
    """macOS speech recognition using SFSpeechRecognizer.

    Uses Apple's on-device speech recognition for zero-download,
    instant startup speech-to-text.
    """

    def __init__(self, language: str = "en"):
        self._language = language
        self._locale = self._lang_to_locale(language)
        self._recognizer = None
        self._available = False
        self._init_recognizer()

    def _init_recognizer(self):
        """Try to initialize the Apple Speech recognizer.

        Each sub-step is timed so the actual bottleneck is visible in logs:
        - `import Speech, Foundation` is the dominant cold-load cost (3-8s)
        - `SFSpeechRecognizer.alloc().initWithLocale_()` is usually fast
        - `isAvailable()` can trigger an on-device model download for some locales
        """
        try:
            import time as _time
            t0 = _time.monotonic()
            import Speech
            import Foundation
            t_import = (_time.monotonic() - t0) * 1000

            t1 = _time.monotonic()
            locale = Foundation.NSLocale.alloc().initWithLocaleIdentifier_(self._locale)
            self._recognizer = Speech.SFSpeechRecognizer.alloc().initWithLocale_(locale)
            t_alloc = (_time.monotonic() - t1) * 1000

            t2 = _time.monotonic()
            available = self._recognizer and self._recognizer.isAvailable()
            t_check = (_time.monotonic() - t2) * 1000

            logger.info(
                "STT init breakdown: import=%.0fms, alloc=%.0fms, isAvailable=%.0fms",
                t_import, t_alloc, t_check,
            )

            if available:
                self._available = True
                logger.info("macOS SFSpeechRecognizer initialized (locale=%s)", self._locale)
                self._request_authorization_async()
            else:
                logger.warning(
                    "SFSpeechRecognizer not available for locale %s. "
                    "Speech recognition may not work.",
                    self._locale,
                )
                self._available = False
        except ImportError:
            logger.warning(
                "PyObjC Speech framework not installed. "
                "Install with: pip install 'captionlm[macos]'. "
                "Falling back to no-op STT (pipeline test mode)."
            )
            self._available = False
        except Exception as e:
            logger.warning("Failed to init SFSpeechRecognizer: %s", e)
            self._available = False

    def _request_authorization_async(self):
        """Request user permission to use Apple Speech Recognition.

        Apple's Speech framework requires explicit authorization before any
        SFSpeechRecognitionTask will succeed — without this, recognition
        silently returns no result. This is an empirical bug we found:
        previous code never called requestAuthorization, so transcription
        always returned None even though init succeeded.

        We request asynchronously: the call returns immediately and the
        completion handler fires when the user approves/denies. The first
        time this runs, macOS shows a permission dialog. Subsequent runs
        use the cached choice.
        """
        try:
            import Speech
            STATUS_NAMES = {
                0: "notDetermined",
                1: "denied",
                2: "restricted",
                3: "authorized",
            }

            def _completion(status):
                name = STATUS_NAMES.get(int(status), f"unknown({status})")
                if int(status) == 3:
                    logger.info("Speech recognition authorized")
                else:
                    logger.warning(
                        "Speech recognition authorization status: %s — "
                        "transcription will silently return nothing. "
                        "Grant permission in System Settings → Privacy & "
                        "Security → Speech Recognition.",
                        name,
                    )

            Speech.SFSpeechRecognizer.requestAuthorization_(_completion)
        except Exception as e:
            logger.warning("Could not request Speech authorization: %s", e)

    async def transcribe(self, audio: np.ndarray) -> str | None:
        """Transcribe audio using Apple's speech recognition."""
        if not self._available:
            return await self._fallback_transcribe(audio)

        try:
            return await self._apple_transcribe(audio)
        except Exception as e:
            logger.error("Apple STT error: %s", e)
            return None

    async def _apple_transcribe(self, audio: np.ndarray) -> str | None:
        """Transcribe using SFSpeechRecognizer."""
        import Speech
        import Foundation

        # Write audio to a temporary WAV file
        wav_data = _audio_to_wav_bytes(audio)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_data)
            tmp_path = f.name

        # Create recognition request
        url = Foundation.NSURL.fileURLWithPath_(tmp_path)
        request = Speech.SFSpeechURLRecognitionRequest.alloc().initWithURL_(url)
        request.setShouldReportPartialResults_(False)

        # Run recognition in a thread to avoid blocking
        loop = asyncio.get_event_loop()
        result_text = await loop.run_in_executor(
            None, self._recognize_sync, request
        )

        # Clean up temp file
        import os
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

        return result_text

    def _recognize_sync(self, request) -> str | None:
        """Synchronous recognition helper (runs in thread pool)."""
        import threading

        event = threading.Event()
        result_holder = [None]

        def handler(result, error):
            if result:
                best = result.bestTranscription()
                if best:
                    result_holder[0] = best.formattedString()
            event.set()

        self._recognizer.recognitionTaskWithRequest_resultHandler_(request, handler)

        # Wait up to 10 seconds for result
        event.wait(timeout=10.0)
        return result_holder[0]

    async def _fallback_transcribe(self, audio: np.ndarray) -> str | None:
        """Fallback: detect if there's speech based on energy level.

        This doesn't actually transcribe — it just checks if the audio
        has enough energy to likely contain speech. Used when the Speech
        framework is not available, to keep the pipeline testable.
        """
        energy = np.sqrt(np.mean(audio**2))
        if energy < 0.01:  # silence threshold
            return None
        # Can't transcribe without a real STT engine
        logger.debug("Audio detected (energy=%.4f) but no STT engine available", energy)
        return None

    @property
    def name(self) -> str:
        return f"macOS System STT ({self._locale})"

    @staticmethod
    def _lang_to_locale(lang: str) -> str:
        """Convert short language code to macOS locale identifier."""
        mapping = {
            "en": "en-US",
            "zh": "zh-CN",
            "ja": "ja-JP",
            "ko": "ko-KR",
            "fr": "fr-FR",
            "de": "de-DE",
            "es": "es-ES",
            "pt": "pt-BR",
            "ru": "ru-RU",
            "ar": "ar-SA",
            "it": "it-IT",
        }
        return mapping.get(lang, lang)
