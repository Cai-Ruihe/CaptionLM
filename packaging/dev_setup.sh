#!/usr/bin/env bash
# One-shot dev environment setup for building CaptionLM.dmg.
# Installs python@3.13 + create-dmg via brew, creates a venv in the
# project root using 3.13, installs all runtime + dev dependencies.
#
# Idempotent — re-running just verifies + tops up missing pieces.
#
# Usage (from project root):
#   ./packaging/dev_setup.sh
# When done:
#   source .venv/bin/activate
#   ./packaging/build_dmg.sh

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "════════════════════════════════════════════════════════════════"
echo "  CaptionLM dev environment setup"
echo "════════════════════════════════════════════════════════════════"
echo ""

# ─── 1. Homebrew ─────────────────────────────────────────────────
echo "[1/5] Checking Homebrew..."
if ! command -v brew &> /dev/null; then
    echo "  ✗ brew not found. Install it first:"
    echo ""
    echo "    /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    echo ""
    exit 1
fi
echo "  ✓ brew: $(brew --version | head -1)"
echo ""

# ─── 2. Xcode Command Line Tools (swiftc) ────────────────────────
echo "[2/5] Checking Xcode Command Line Tools..."
if ! command -v swiftc &> /dev/null; then
    echo "  ⚠ swiftc not found. Installing Xcode CLT (you'll see a GUI dialog)..."
    xcode-select --install
    echo "  Press Enter when the install dialog finishes, then re-run this script."
    exit 0
fi
echo "  ✓ swiftc: $(swiftc --version | head -1)"
echo ""

# ─── 3. python@3.13 + create-dmg via brew ────────────────────────
echo "[3/5] Installing python@3.13 and create-dmg via brew..."
brew list python@3.13 &> /dev/null || brew install python@3.13
brew list create-dmg  &> /dev/null || brew install create-dmg
PY313=$(brew --prefix python@3.13)/bin/python3.13
if [ ! -x "$PY313" ]; then
    echo "  ✗ python3.13 not at $PY313 after brew install" >&2
    exit 1
fi
echo "  ✓ python@3.13: $($PY313 --version)"
echo "  ✓ create-dmg:  $(create-dmg --version 2>&1 | head -1)"
echo ""

# ─── 4. Create venv ──────────────────────────────────────────────
echo "[4/5] Creating .venv (Python 3.13)..."
if [ -d .venv ]; then
    EXISTING_VER=$(.venv/bin/python --version 2>&1)
    if [[ "$EXISTING_VER" != *"3.13"* ]]; then
        echo "  ⚠ Existing .venv is $EXISTING_VER, rebuilding for 3.13..."
        rm -rf .venv
    fi
fi
if [ ! -d .venv ]; then
    "$PY313" -m venv .venv
fi
echo "  ✓ .venv ready: $(.venv/bin/python --version)"
echo ""

# ─── 5. pip install everything ───────────────────────────────────
echo "[5/5] Installing project + all dependencies into .venv..."
echo "      (this takes 2-5 minutes the first time)"
echo ""

# Upgrade pip first
.venv/bin/pip install --quiet --upgrade pip

# CRITICAL: setuptools >= 80 removed support for install_requires /
# setup_requires that py2app 0.28.x still depends on. Pin to 79.x.
# Without this, `python setup_app.py py2app` fails with:
#   error: install_requires is no longer supported
# Verified 2026-05-14 with py2app 0.28.10 on macOS.
.venv/bin/pip install --quiet "setuptools<80"

# Install the project itself with all optional providers + dev tools
.venv/bin/pip install -e ".[all,dev]"

# py2app isn't in pyproject.toml [dev] (some users don't need it);
# install it explicitly here so build_dmg.sh works.
.venv/bin/pip install --quiet py2app

# Re-pin setuptools after the chain installs — pyproject.toml's
# build-system might pull in a newer one transitively.
.venv/bin/pip install --quiet "setuptools<80" --upgrade

# Verify all the imports build_dmg.sh's sanity-check looks for
echo ""
echo "Verifying all critical imports..."
fail=0
for mod in py2app captionlm PySide6 numpy sounddevice "google.cloud.speech" "google.genai" openai anthropic websockets; do
    if .venv/bin/python -c "import ${mod//./.}" 2>/dev/null; then
        echo "  ✓ $mod"
    else
        echo "  ✗ $mod"
        fail=$((fail + 1))
    fi
done

if [ $fail -ne 0 ]; then
    echo ""
    echo "  ⚠ Some modules failed to import — see errors above." >&2
    exit 1
fi

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  ✓ Environment ready"
echo ""
echo "  Next steps:"
echo "    source .venv/bin/activate"
echo "    ./packaging/build_dmg.sh"
echo "════════════════════════════════════════════════════════════════"
