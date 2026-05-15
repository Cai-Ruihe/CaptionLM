# Architecture & Design

A brief tour of the pieces and the non-obvious design decisions. For day-to-day usage see [INSTALL.md](INSTALL.md) and [API_SETUP.md](API_SETUP.md).

## Layered overview

```
┌────────────────────────────────────────────────┐
│  app.py            — top-level Qt app          │
│  ├── overlay       (PyObjC NSPanel)            │
│  ├── tray icon     (Qt)                        │
│  └── control panel (Qt)                        │
├────────────────────────────────────────────────┤
│  pipeline.py       — orchestration             │
│  • _run_loop_streaming      (Google / Apple)   │
│  • _run_loop_translation_stream (Qwen LT)      │
├──────────────────┬─────────────────────────────┤
│  stt/            │  translation/               │
│  • google_streaming_stt.py                     │
│  • qwen_asr.py                                 │
│  • qwen_livetranslate.py                       │
│  • apple_speech_analyzer.py                    │
│  • whisper_stt.py                              │
│                  │  • gemini.py                │
│                  │  • qwen.py                  │
│                  │  • claude.py                │
│                  │  • google_free.py           │
├──────────────────┴─────────────────────────────┤
│  audio/capture_audio.swift — system audio      │
│  ScreenCaptureKit → 16kHz mono float32 → stdout│
└────────────────────────────────────────────────┘
```

## Audio capture

`capture_audio.swift` is a tiny Swift CLI compiled on-the-fly (cached in `~/.cache/captionlm/`). It uses **ScreenCaptureKit** (macOS 13+) to attach to the system audio output stream, downsamples to 16 kHz mono float32, and pipes raw PCM to stdout. Python STT engines spawn this subprocess and read its stdout.

Why subprocess instead of in-process: ScreenCaptureKit's Swift API can't be cleanly bridged from PyObjC without sub-second latency and stability issues. A subprocess gives us a clean process boundary + works with system permission dialogs.

## STT abstraction

Every STT engine implements `STTEngine` (in `stt/base.py`). Two flavors:

1. **Standard STT**: yields `(text, is_final)` tuples. Pipeline runs a separate translator on top. (Google Cloud, Apple SpeechAnalyzer, Qwen ASR, Whisper)
2. **End-to-end**: `provides_translation = True`. Engine yields `(orig, trans, is_final)` triples; pipeline skips its own translator. (Qwen LiveTranslate)

This lets you mix Qwen ASR + Gemini, or Google STT + Qwen-MT, etc.

## Pipeline strategy — sentence/utterance buffering

The hardest part isn't the API integrations — it's deciding **when** to translate and **how** to display partials without flashing junk text. Key heuristics (all in `pipeline.py:_run_loop_streaming`):

- **Sentence-end punct flush**: when STT's running transcript reaches `。！？.!?…`, translate the new chunk immediately.
- **Soft-boundary fallback** (`SOFT_FLUSH_MIN_CHARS=35`, soft punct `，、,`): for long runs without hard terminators (continuous Chinese speech), flush at the last comma if buffer ≥ 35 chars.
- **Time-based force flush** (`TIME_FORCE_FLUSH_SEC=4`): every 4 seconds without a flush, translate whatever's buffered. Guarantees no >4s silence in the UI.
- **Hard cap** (`HARD_FORCE_FLUSH_CHARS=100`): worst case — never wait > 100 chars to ship something.
- **Min hard-flush chars** (`MIN_HARD_FLUSH_CHARS=8`): ⚠️ Critical — without this, Japanese sentences like `ずっと踊。` (5 chars ending with `。`) get translated as fragments. We defer short hard-boundary flushes until either is_final fires or buffer grows.

## Overlay accumulator + history dedup

`native_overlay.py:update_subtitle` keeps two buffers:

- **Live area**: shows the current utterance accumulating, with sub-second-level updates
- **History**: scrollable list of finalized utterances, dedup'd by prefix-subsume + exact-match check

Tricky cases handled:
- Same partial fragment seen twice in a row (when STT revises text mid-utterance): use longest-common-prefix, only re-translate diverging tail
- Translator returns identical text twice (rare but happens with retranslation polish): SKIP-exact-dup
- Punct-only chunks (`.`, `。`, `！`): drop entirely — not real content
- Init messages (`Loading STT...`): display but don't push to history
- Partial path emits `(text, "")` instead of `(text, last_good_translation)` — prevents previous utterance's translation from being paired with new partial text and bogus history entries

## Auto-reconnect

ScreenCaptureKit errors happen semi-frequently in real-world use:
- `SCStreamErrorAttemptToStopStreamState` (-3821): another app grabbed the audio source, system alert popped up, Mac woke from sleep
- `Failed during stream due to application connection being interrupted` (-3805): two ScreenCaptureKit streams attached at once

We auto-recover:

1. stderr reader on the Swift subprocess sees the error → sets `_capture_audio_died = True`
2. A `_capture_death_watcher` async task polls this flag, exits when set
3. `asyncio.wait({send_task, recv_task, death_task}, FIRST_COMPLETED)` returns immediately when watcher exits
4. Outer reconnect loop tears down the dead subprocess, sleeps 3 s (in 0.2 s slices so `stop()` can interrupt promptly), spawns a new one, reconnects WS
5. After 3 consecutive failures, escalate to fatal error and surface to user with red heartbeat dot

## Logging

Hot-path runtime logs go to `captionlm/logs/captionlm.log`. Last 10 runs are kept (rotated as `.log.1` ... `.log.10`).

A `_RedactSecretsFilter` strips `Bearer sk-...` and `Bearer AIza...` tokens from log lines before they're written, so logs are reasonably safe to share for bug reports. Belt-and-suspenders: also `grep` your log for `sk-\|AIza` before posting.

## Session export

Every translated subtitle is recorded in a session buffer. On `stop()` (or atexit), a bilingual SRT is written to `~/Documents/CaptionLM/sessions/<timestamp>-<src>-<tgt>.srt`. Streaming partials are collapsed at export time using prefix-subsume + time-gap heuristics so the file matches what you'd remember seeing on screen.

## Why these specific choices

- **PyObjC NSPanel for the overlay** (instead of Qt): Qt's window can't reliably get click-through + always-on-top on macOS. Native NSPanel works.
- **Subprocess for audio capture** (instead of PyObjC bridge to SCStream): cleaner separation, easier to restart on failure, easier to debug (stdout is just bytes).
- **gRPC for Google STT** (via `google-cloud-speech` SDK): the only officially supported streaming protocol. Adds ~3 s cold-start import time but is rock-solid afterwards.
- **WebSocket for Qwen** (via `websockets` library): DashScope's realtime API is WS-only. We use raw frames since the schema differs slightly from the OpenAI Realtime SDK shape.
- **OpenAI-compatible SDK for Qwen-MT** (translation): Alibaba provides this compatibility layer, so we reuse the `openai` Python client. The dashscope-intl base URL routes to their MT models.
