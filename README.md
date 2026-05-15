<div align="center">

<img src="assets/logo-color.png" alt="CaptionLM" width="128" />

# CaptionLM

**Real-time bilingual subtitle translation for any audio on macOS**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![macOS](https://img.shields.io/badge/macOS-12+-black.svg)](https://www.apple.com/macos/)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/Cai-Ruihe/CaptionLM/pulls)

[简体中文](README_zh.md) · [Installation](docs/INSTALL.md) · [API Setup](docs/API_SETUP.md) · [Design](docs/DESIGN.md)

</div>

---

CaptionLM captures system audio from **any** macOS application — YouTube, Zoom, Netflix, conference calls — transcribes it in real time, and overlays bilingual subtitles on screen. Choose your speech-to-text engine and translation provider independently to balance latency, cost, and quality.

## ✨ Features

- 🎙️ **Captures any app's audio** — uses ScreenCaptureKit, no virtual audio devices needed
- 🌐 **Multi-engine** — mix and match STT (Google Cloud / Qwen ASR / Qwen LiveTranslate / Apple SpeechAnalyzer / Whisper) with translators (Gemini / Qwen-MT / Claude / Google Free)
- 💬 **Bilingual overlay** — original + translation, always-on-top, draggable, customizable opacity
- 📺 **Auto SRT export** — every session is saved as a bilingual `.srt` to `~/Documents/CaptionLM/sessions/`
- 🪙 **Live cost meter** — see per-session token usage and USD cost while it's running
- 🔄 **Auto-reconnect** — when macOS interrupts the audio stream, CaptionLM retries automatically
- 🇨🇳 **Singapore (international) and China-mainland endpoints** — works on both sides of the firewall for Qwen / DashScope

## 🎬 Demo

> Screenshots coming soon. Place yours in `docs/screenshots/` and they'll show up here.

## 🚀 Quick start

### Option 1 — Pre-built .dmg (recommended for end users)

1. Download the latest `.dmg` from [Releases](https://github.com/Cai-Ruihe/CaptionLM/releases)
2. Drag `CaptionLM.app` into `/Applications`
3. Open it — grant **Screen Recording** permission when prompted (System Settings → Privacy & Security → Screen Recording → enable CaptionLM)
4. Click the menubar icon → **Settings** → enter your API keys (see [API Setup](docs/API_SETUP.md))
5. Click **Start** and play any audio in any app

### Option 2 — From source (for developers)

```bash
git clone https://github.com/Cai-Ruihe/CaptionLM.git
cd CaptionLM
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[all]"
captionlm
```

See [docs/INSTALL.md](docs/INSTALL.md) for detailed instructions including Python setup and troubleshooting.

## 🔑 API setup

CaptionLM is free software, but most STT and translation engines require an API key. **You bring your own key** — nothing routes through our servers. The most common combo is:

| Component | Recommended | Cost (estimate) |
|---|---|---|
| **STT** | Google Cloud Speech-to-Text streaming | ~$0.024/min, **60 free min/month** |
| **Translation** | Gemini 2.5 Flash | ~$0.01/hr of speech |
| **Alternative all-Qwen** | Qwen LiveTranslate (ASR+translation in one) | ~$0.005/min, ~¥0.36/hr |

Step-by-step instructions for each provider — including screenshots of the Google Cloud Console and Alibaba Model Studio — are in **[docs/API_SETUP.md](docs/API_SETUP.md)**.

## 🔄 Updating

macOS `.app` bundles are self-contained — no installer, no uninstaller. To update:

1. Quit the running CaptionLM (menubar icon → Quit, or click the ✕)
2. Download the new `.dmg`
3. Drag the new `CaptionLM.app` into `/Applications` — Finder will ask "replace?" → **Replace**
4. Re-open CaptionLM

Your settings and data are kept across updates (they live outside the `.app`):

| Data | Location |
|---|---|
| API keys, UI preferences | `~/Library/Application Support/CaptionLM/settings.toml` |
| Google Cloud credentials | `~/.captionlm/*.json` |
| Bilingual SRT exports | `~/Documents/CaptionLM/sessions/*.srt` |

To uninstall: drag `CaptionLM.app` to Trash. For a clean wipe also `rm -rf ~/Library/Application\ Support/CaptionLM ~/.captionlm ~/.cache/captionlm`.

## 🏗️ Architecture

```
┌────────────────────────────────────────────────────────────────┐
│  macOS app (YouTube / Zoom / anything)                         │
└──────────────────────┬─────────────────────────────────────────┘
                       │ system audio
                       ▼
            ┌──────────────────────┐
            │  capture_audio.swift │  ← ScreenCaptureKit, no kext
            │  (PCM @ 16kHz mono)  │
            └──────────┬───────────┘
                       │ stdout: float32 PCM
                       ▼
        ┌────────────────────────────────────┐
        │  STT engine (pluggable)            │
        │  • Google Cloud Speech streaming   │
        │  • Qwen ASR (WebSocket realtime)   │
        │  • Qwen LiveTranslate (E2E)        │  ← skips translator
        │  • Apple SpeechAnalyzer            │
        │  • Faster-Whisper (local)          │
        └──────────┬─────────────────────────┘
                   │ (text, is_final)
                   ▼
        ┌────────────────────────────────────┐
        │  Translator (pluggable)            │
        │  • Gemini 2.5 Flash (streaming)    │
        │  • Qwen-MT Turbo (streaming)       │
        │  • Anthropic Claude                │
        │  • Google Translate (free)         │
        └──────────┬─────────────────────────┘
                   │ (orig, translated)
                   ▼
        ┌────────────────────────────────────┐
        │  Overlay (PyObjC NSPanel)          │  ← always-on-top
        │  • Live area (current utterance)   │
        │  • Scrollable history (last 50)    │
        └────────────────────────────────────┘
```

See [docs/DESIGN.md](docs/DESIGN.md) for the full architecture write-up including utterance buffering, partial-revision handling, and auto-reconnect logic.

## 🛣️ Roadmap

- [ ] Windows support (WASAPI loopback in place of ScreenCaptureKit)
- [ ] Live retranslation polish across more providers
- [ ] One-click `.dmg` build via GitHub Actions
- [ ] Localization for the Settings panel (currently English-only)

## 🤝 Contributing

PRs welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow. Areas of high interest:

- Windows port (ScreenCaptureKit alternatives)
- Additional STT/translator providers
- UI polish + accessibility

## 📜 License

MIT — see [LICENSE](LICENSE).

---

<div align="center">
<sub>Built with PySide6, PyObjC, and a lot of testing on Japanese-to-Chinese subtitle workflows.</sub>
</div>
