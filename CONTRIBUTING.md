# Contributing

Thanks for considering a contribution to CaptionLM! Some ground rules:

## Dev setup

```bash
git clone https://github.com/Cai-Ruihe/CaptionLM.git
cd CaptionLM
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[all,dev]"
captionlm
```

## Running tests

```bash
pytest
```

Tests are minimal right now — PRs adding more are very welcome.

## Code style

```bash
ruff format .
ruff check .
```

## Workflow

1. Fork → branch (`fix/<short-name>` or `feat/<short-name>`)
2. Make changes
3. Run tests + lint
4. PR against `main` with a clear description

## Things we'd love help with

- **Windows port** — replace `capture_audio.swift` with a WASAPI loopback equivalent
- **Localization** — Settings panel is English-only
- **More STT/translator providers** — see `src/captionlm/stt/base.py` and `src/captionlm/translation/base.py` for the interfaces. Adding a provider is ~100 LoC
- **Auto-build DMG via GitHub Actions** — currently manual via `packaging/build_dmg.sh`
- **App icon** — drop a 1024×1024 `.icns` into `assets/icon.icns`

## Reporting bugs

Please include:
1. macOS version (e.g. `sw_vers -productVersion`)
2. Python version (`python3 --version`)
3. STT + Translator combo you were using
4. Last 200 lines of `captionlm/logs/captionlm.log` (will auto-redact API keys, but double-check)
5. Steps to reproduce

## Security

If you discover a security issue (especially anything that could leak API keys), please email instead of opening a public issue.
