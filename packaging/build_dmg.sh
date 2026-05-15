#!/usr/bin/env bash
# Build a fully self-contained CaptionLM.app + .dmg.
#
# End users running the resulting .dmg need NOTHING installed — no Python,
# no Xcode, no Homebrew. Everything required is bundled inside the .app:
#   • Python interpreter (embedded by py2app)
#   • All Python packages (PySide6, numpy, google-cloud-speech, etc.)
#   • Pre-compiled capture_audio Swift binary (we build it here)
#   • Logo + icon assets
#
# Prerequisites on the BUILD machine (one-time):
#   xcode-select --install          # for swiftc
#   brew install create-dmg         # for DMG packaging
#   pip install py2app              # in the active Python env
#
# Run from project root:
#   ./packaging/build_dmg.sh
#
# Output:
#   packaging/dist/CaptionLM.app    (run-able locally for testing)
#   packaging/dist/CaptionLM.dmg    (distributable .dmg)

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "════════════════════════════════════════════════════════════════"
echo "  CaptionLM .dmg builder"
echo "  Project root: $PROJECT_ROOT"
echo "════════════════════════════════════════════════════════════════"
echo ""

# ─────────────────────────────────────────────────────────────────
# 0. Sanity checks — refuse to start if anything's missing.
# ─────────────────────────────────────────────────────────────────
echo "[0/6] Checking build environment..."

missing=0

if ! command -v python3 &> /dev/null; then
    echo "  ✗ python3 not found in PATH" >&2
    missing=1
else
    PY_VER=$(python3 --version | awk '{print $2}')
    echo "  ✓ python3: $PY_VER"
    if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"; then
        echo "    ⚠ Python 3.11+ recommended (found $PY_VER)" >&2
    fi
fi

if ! command -v swiftc &> /dev/null; then
    echo "  ✗ swiftc not found. Install Xcode Command Line Tools:" >&2
    echo "      xcode-select --install" >&2
    missing=1
else
    echo "  ✓ swiftc: $(swiftc --version | head -1)"
fi

if ! command -v create-dmg &> /dev/null; then
    echo "  ✗ create-dmg not found. Install via:" >&2
    echo "      brew install create-dmg" >&2
    missing=1
else
    echo "  ✓ create-dmg: $(create-dmg --version 2>&1 | head -1)"
fi

if ! python3 -c "import py2app" 2>/dev/null; then
    echo "  ✗ py2app not installed in the active Python env." >&2
    echo "    Run: pip install py2app" >&2
    missing=1
else
    PY2APP_VER=$(python3 -c "import py2app; print(py2app.__version__)" 2>/dev/null || echo "?")
    echo "  ✓ py2app: $PY2APP_VER"
fi

# setuptools >= 80 breaks py2app 0.28's install_requires plumbing.
# Refuse to start instead of letting the user wait through a long
# `[3/5] Building...` only to fail.
SETUPTOOLS_MAJOR=$(python3 -c "import setuptools; print(int(setuptools.__version__.split('.')[0]))" 2>/dev/null || echo 0)
if [ "$SETUPTOOLS_MAJOR" -ge 80 ]; then
    echo "  ✗ setuptools $(python3 -c 'import setuptools; print(setuptools.__version__)') is too new for py2app." >&2
    echo "    Downgrade with: pip install 'setuptools<80'" >&2
    missing=1
else
    echo "  ✓ setuptools: $(python3 -c 'import setuptools; print(setuptools.__version__)')"
fi

# Confirm captionlm + its providers are importable in the active env.
# If they aren't, the resulting .app would be missing modules at runtime.
if ! python3 -c "import captionlm" 2>/dev/null; then
    echo "  ✗ captionlm not importable in the active Python env. Run:" >&2
    echo "      pip install -e '.[all]'" >&2
    missing=1
else
    echo "  ✓ captionlm module importable"
fi

# Provider packages — these are critical for the .app to actually work.
for mod in PySide6 numpy sounddevice google.cloud.speech google.genai openai anthropic websockets; do
    if ! python3 -c "import $mod" 2>/dev/null; then
        echo "  ⚠ $mod not importable — .app will be missing this provider" >&2
    else
        echo "  ✓ $mod"
    fi
done

if [ $missing -ne 0 ]; then
    echo ""
    echo "Fix the items above and re-run." >&2
    exit 1
fi
echo ""

# ─────────────────────────────────────────────────────────────────
# 1. Pre-compile capture_audio.swift → binary
# ─────────────────────────────────────────────────────────────────
echo "[1/6] Pre-compiling capture_audio Swift binary..."

SWIFT_SRC="src/captionlm/audio/capture_audio.swift"
SWIFT_BIN="src/captionlm/audio/capture_audio"

if [ ! -f "$SWIFT_SRC" ]; then
    echo "  ✗ $SWIFT_SRC missing — not a valid CaptionLM checkout" >&2
    exit 1
fi

# Compile to optimized binary (-O), linking the macOS frameworks
# capture_audio.swift uses. The output goes alongside the .swift
# source so py2app picks it up via DATA_FILES.
swiftc -O \
    -o "$SWIFT_BIN" \
    "$SWIFT_SRC" \
    -framework ScreenCaptureKit \
    -framework CoreMedia \
    -framework AVFoundation

chmod +x "$SWIFT_BIN"
BIN_SIZE=$(stat -f%z "$SWIFT_BIN" 2>/dev/null || stat -c%s "$SWIFT_BIN" 2>/dev/null)
echo "  ✓ $SWIFT_BIN built ($BIN_SIZE bytes)"
echo ""

# ─────────────────────────────────────────────────────────────────
# 2. Clean previous build
# ─────────────────────────────────────────────────────────────────
echo "[2/6] Cleaning previous build artifacts..."
rm -rf packaging/build packaging/dist
echo "  ✓ packaging/build and packaging/dist removed"
echo ""

# ─────────────────────────────────────────────────────────────────
# 3. py2app build
# ─────────────────────────────────────────────────────────────────
echo "[3/6] Building CaptionLM.app via py2app (this takes 3-8 minutes)..."

# Workaround for py2app 0.28.x: it raises
#   `error: install_requires is no longer supported`
# whenever the distribution has any install_requires set — and modern
# setuptools auto-injects pyproject.toml's `dependencies` as install_requires.
# Temporarily move pyproject.toml out of the way so setuptools only sees
# what setup_app.py explicitly passes (which has no install_requires).
# Trap ensures the file is restored even if py2app crashes or user Ctrl-Cs.
mv pyproject.toml pyproject.toml.during-py2app
trap 'mv -f pyproject.toml.during-py2app pyproject.toml 2>/dev/null || true' EXIT INT TERM

python3 packaging/setup_app.py py2app \
    --dist-dir packaging/dist \
    --bdist-base packaging/build

# Restore pyproject.toml on success
mv -f pyproject.toml.during-py2app pyproject.toml
trap - EXIT INT TERM

APP_BUNDLE="packaging/dist/CaptionLM.app"
if [ ! -d "$APP_BUNDLE" ]; then
    echo "  ✗ py2app didn't produce $APP_BUNDLE" >&2
    exit 1
fi
APP_SIZE=$(du -sh "$APP_BUNDLE" | cut -f1)
echo "  ✓ $APP_BUNDLE built ($APP_SIZE)"
echo ""

# ─────────────────────────────────────────────────────────────────
# 4. Smoke-test the .app
# ─────────────────────────────────────────────────────────────────
echo "[4/6] Verifying capture_audio is inside the .app bundle..."

BUNDLED_BIN_PATHS=(
    "$APP_BUNDLE/Contents/Resources/captionlm/audio/capture_audio"
    "$APP_BUNDLE/Contents/Resources/capture_audio"
)
FOUND_BIN=""
for p in "${BUNDLED_BIN_PATHS[@]}"; do
    if [ -f "$p" ]; then
        FOUND_BIN="$p"
        break
    fi
done

if [ -z "$FOUND_BIN" ]; then
    echo "  ✗ capture_audio NOT FOUND inside the .app. End users would" >&2
    echo "    need Xcode CLT to compile it on first run. Aborting." >&2
    exit 1
fi
echo "  ✓ capture_audio bundled at: ${FOUND_BIN#$APP_BUNDLE/}"

# Verify the logo file is also bundled
LOGO_PATH="$APP_BUNDLE/Contents/Resources/captionlm/assets/logo-overlay.png"
if [ -f "$LOGO_PATH" ]; then
    echo "  ✓ logo-overlay.png bundled"
else
    echo "  ⚠ logo-overlay.png not bundled — overlay logo button will be image-less" >&2
fi

# Verify icon.icns made it into the .app
ICON_PATH="$APP_BUNDLE/Contents/Resources/icon.icns"
if [ -f "$ICON_PATH" ]; then
    echo "  ✓ icon.icns bundled"
else
    echo "  ⚠ icon.icns not in .app — Dock icon will be Python's default" >&2
fi
echo ""

# ─────────────────────────────────────────────────────────────────
# 5. SLIM the .app — strip unused Qt frameworks (and optionally
#    lipo-thin universal2 binaries to arm64 only).
#
# Background: PySide6 ships ALL ~80 Qt frameworks in its wheel and
# py2app copies the entire folder. We only import QtCore/QtGui/QtWidgets,
# so QtWebEngineCore (~200MB Chromium), Qt3D*, QtQuick3D*, QtMultimedia*,
# QtPdf, QtCharts, QtVirtualKeyboard, etc. are all dead weight.
#
# Modes (override with env var):
#   SLIM=none          → skip entirely, ship the fat 1.3GB build
#   SLIM=conservative  → delete unused Qt frameworks only (Intel-compatible)
#   SLIM=aggressive    → above + lipo-thin to arm64 (DEFAULT; Apple Silicon only)
#
# After slim the bundle is destructively trimmed. To recover, re-run
# this script (step [2/6] wipes packaging/dist before py2app rebuilds).
# ─────────────────────────────────────────────────────────────────
SLIM="${SLIM:-aggressive}"
echo "[5/6] Slimming the .app (SLIM=$SLIM)..."

if [ "$SLIM" = "none" ]; then
    echo "  → skipped (SLIM=none)"
elif [ "$SLIM" = "conservative" ] || [ "$SLIM" = "aggressive" ]; then
    BEFORE_SIZE=$(du -sh "$APP_BUNDLE" | cut -f1)
    BEFORE_BYTES=$(du -sk "$APP_BUNDLE" | cut -f1)

    # Locate PySide6 inside the .app — py2app puts it under
    # Contents/Resources/lib/python3.13/PySide6/
    PYSIDE_DIR=$(find "$APP_BUNDLE/Contents/Resources/lib" \
        -maxdepth 4 -type d -name PySide6 2>/dev/null | head -1)

    if [ -z "$PYSIDE_DIR" ] || [ ! -d "$PYSIDE_DIR" ]; then
        echo "  ⚠ PySide6 not found inside .app — skipping Qt prune" >&2
    else
        QT_LIB_DIR="$PYSIDE_DIR/Qt/lib"
        QT_QML_DIR="$PYSIDE_DIR/Qt/qml"
        QT_RES_DIR="$PYSIDE_DIR/Qt/resources"
        QT_PLUGINS_DIR="$PYSIDE_DIR/Qt/plugins"
        QT_TRANS_DIR="$PYSIDE_DIR/Qt/translations"

        # ─── Qt frameworks: keep ONLY what we import + safe transitive deps ──
        # Verified via `grep -r 'from PySide6' src/`: we use QtCore + QtGui
        # + QtWidgets. Keep QtNetwork (transitive at C++ level), QtSvg
        # (QIcon may load SVG), QtDBus (harmless macOS dep).
        KEEP_FRAMEWORKS=(QtCore QtGui QtWidgets QtNetwork QtSvg QtDBus)

        if [ -d "$QT_LIB_DIR" ]; then
            DEL_COUNT=0
            for fw_path in "$QT_LIB_DIR"/*.framework; do
                [ -e "$fw_path" ] || continue
                fw_name=$(basename "$fw_path" .framework)
                keep=0
                for k in "${KEEP_FRAMEWORKS[@]}"; do
                    [ "$fw_name" = "$k" ] && { keep=1; break; }
                done
                if [ $keep -eq 0 ]; then
                    rm -rf "$fw_path"
                    DEL_COUNT=$((DEL_COUNT + 1))
                fi
            done
            echo "  ✓ Removed $DEL_COUNT unused Qt frameworks"
            echo "    (kept: ${KEEP_FRAMEWORKS[*]})"
        fi

        # ─── PySide6 Python wrappers for deleted frameworks ──
        # PySide6 ships Qt*.abi3.so wrappers; delete those whose
        # corresponding .framework we just removed. Always keep
        # non-Qt-prefixed PySide6 files (shiboken6, support, etc.).
        for ext in abi3.so pyi; do
            for so_path in "$PYSIDE_DIR"/Qt*."$ext"; do
                [ -e "$so_path" ] || continue
                base=$(basename "$so_path")
                # Extract module name: "QtFoo.abi3.so" → "QtFoo"
                name="${base%%.*}"
                keep=0
                for k in "${KEEP_FRAMEWORKS[@]}"; do
                    [ "$name" = "$k" ] && { keep=1; break; }
                done
                [ $keep -eq 0 ] && rm -f "$so_path"
            done
        done
        echo "  ✓ Removed unused PySide6 Qt*.abi3.so / .pyi wrappers"

        # ─── Qt QML modules: we use QtWidgets, not Quick/QML ──
        if [ -d "$QT_QML_DIR" ]; then
            rm -rf "$QT_QML_DIR"
            echo "  ✓ Removed Qt/qml/ (Quick/QML modules)"
        fi

        # ─── Qt resources: mostly WebEngine ICU + locale data (huge) ──
        if [ -d "$QT_RES_DIR" ]; then
            rm -rf "$QT_RES_DIR"
            echo "  ✓ Removed Qt/resources/ (WebEngine ICU data)"
        fi

        # ─── Qt plugins: keep cocoa platform + image format + style ──
        if [ -d "$QT_PLUGINS_DIR" ]; then
            KEEP_PLUGINS=(platforms imageformats styles tls iconengines)
            DEL_PLUGINS=0
            for plugin_subdir in "$QT_PLUGINS_DIR"/*/; do
                [ -e "$plugin_subdir" ] || continue
                subdir=$(basename "$plugin_subdir")
                keep=0
                for k in "${KEEP_PLUGINS[@]}"; do
                    [ "$subdir" = "$k" ] && { keep=1; break; }
                done
                if [ $keep -eq 0 ]; then
                    rm -rf "$plugin_subdir"
                    DEL_PLUGINS=$((DEL_PLUGINS + 1))
                fi
            done
            echo "  ✓ Removed $DEL_PLUGINS unused Qt plugin categories"
            echo "    (kept: ${KEEP_PLUGINS[*]})"
        fi

        # ─── Qt translations: keep en/zh, delete rest ──
        if [ -d "$QT_TRANS_DIR" ]; then
            find "$QT_TRANS_DIR" -name "*.qm" \
                ! -name "qt_en*" ! -name "qt_zh*" \
                ! -name "qtbase_en*" ! -name "qtbase_zh*" \
                -delete 2>/dev/null || true
            echo "  ✓ Pruned Qt translations (kept: en, zh)"
        fi
    fi

    # ─── Aggressive: lipo-thin universal2 binaries to arm64 only ──
    if [ "$SLIM" = "aggressive" ]; then
        ARCH=$(uname -m)
        if [ "$ARCH" != "arm64" ]; then
            echo "  ⚠ Build host is $ARCH, not arm64 — skipping lipo step" >&2
        else
            echo "  → lipo -thin arm64 on .so/.dylib (the resulting .app"
            echo "    will run on Apple Silicon ONLY, not Intel Macs)"
            THINNED=0
            FAILED=0
            # Find all dylibs and Python .so files. Skip the main app
            # binary and the Python interpreter binary — keep those
            # universal2 so macOS launch is more robust.
            while IFS= read -r -d '' f; do
                # Probe: is this a universal Mach-O?
                if file "$f" 2>/dev/null | grep -q "Mach-O universal"; then
                    if lipo -thin arm64 "$f" -output "$f.lipo" 2>/dev/null; then
                        mv -f "$f.lipo" "$f"
                        THINNED=$((THINNED + 1))
                    else
                        rm -f "$f.lipo"
                        FAILED=$((FAILED + 1))
                    fi
                fi
            done < <(find "$APP_BUNDLE" \
                \( -name "*.so" -o -name "*.dylib" \) \
                -type f -print0)
            echo "  ✓ Thinned $THINNED universal2 binaries to arm64"
            [ $FAILED -gt 0 ] && echo "    ($FAILED files weren't lipo-able, left as-is)"
        fi

        # ─── Strip debug symbols from .so/.dylib ──
        STRIPPED=0
        while IFS= read -r -d '' f; do
            # -S strips debug symbols, -x keeps externals (needed for
            # dynamic linking). Suppress errors for already-stripped.
            strip -S -x "$f" 2>/dev/null && STRIPPED=$((STRIPPED + 1))
        done < <(find "$APP_BUNDLE" \
            \( -name "*.so" -o -name "*.dylib" \) \
            -type f -print0)
        echo "  ✓ Stripped debug symbols from $STRIPPED files"

        # ─── CRITICAL: re-sign every .so/.dylib we touched ──
        # Apple Silicon (arm64) refuses to load Mach-O binaries whose
        # signature doesn't match the file. lipo and strip BOTH modify
        # the binary AFTER py2app's ad-hoc signing pass, so without
        # re-signing here, the .app silently fails to launch (kernel
        # rejects the dyld load, no Python traceback, no Dock icon).
        # Empirical: discovered 2026-05-15 — slim+lipo+strip produced
        # a perfectly-structured .app that simply wouldn't launch
        # until we added this re-sign pass.
        echo "  → Re-signing slim'd binaries (ad-hoc) for arm64 dyld..."
        RESIGNED=0
        RESIGN_FAILED=0
        while IFS= read -r -d '' f; do
            if codesign --force --sign - "$f" 2>/dev/null; then
                RESIGNED=$((RESIGNED + 1))
            else
                RESIGN_FAILED=$((RESIGN_FAILED + 1))
            fi
        done < <(find "$APP_BUNDLE" \
            \( -name "*.so" -o -name "*.dylib" \) \
            -type f -print0)
        echo "  ✓ Re-signed $RESIGNED Mach-O binaries"
        [ $RESIGN_FAILED -gt 0 ] && echo "    ($RESIGN_FAILED files couldn't be re-signed)"

        # Also re-sign Qt framework binaries (these don't have .so/.dylib
        # extension — they're plain Mach-O inside Versions/A/<FrameworkName>).
        # We didn't lipo or strip these, but py2app may have ad-hoc signed
        # them before our prune deleted neighboring frameworks, which can
        # invalidate signatures in some cases.
        FW_RESIGNED=0
        if [ -d "$QT_LIB_DIR" ]; then
            for fw_path in "$QT_LIB_DIR"/*.framework; do
                [ -e "$fw_path" ] || continue
                fw_name=$(basename "$fw_path" .framework)
                fw_binary="$fw_path/Versions/A/$fw_name"
                if [ -f "$fw_binary" ]; then
                    codesign --force --sign - "$fw_binary" 2>/dev/null \
                        && FW_RESIGNED=$((FW_RESIGNED + 1))
                fi
            done
            echo "  ✓ Re-signed $FW_RESIGNED Qt framework binaries"
        fi
    fi

    # ─── Static sanity check: critical files still exist ──
    # We do NOT run an "import" test via the embedded Python because
    # that path requires py2app's __boot__.py to set up PYTHONPATH /
    # DYLD_FRAMEWORK_PATH first — running `Contents/MacOS/python -c
    # "import PySide6"` raw will ALWAYS fail regardless of slim, giving
    # a false negative. Instead, just verify the critical files are
    # present after slim and trust the user to test the actual .app.
    echo "  → Checking critical files still present..."
    MISSING=()
    PYSIDE_DIR=$(find "$APP_BUNDLE/Contents/Resources/lib" \
        -maxdepth 4 -type d -name PySide6 2>/dev/null | head -1)
    for f in QtCore QtGui QtWidgets; do
        if [ -n "$PYSIDE_DIR" ] && [ ! -f "$PYSIDE_DIR/$f.abi3.so" ]; then
            MISSING+=("PySide6/$f.abi3.so")
        fi
        if [ -n "$PYSIDE_DIR" ] && [ ! -f "$PYSIDE_DIR/Qt/lib/$f.framework/Versions/A/$f" ]; then
            MISSING+=("PySide6/Qt/lib/$f.framework binary")
        fi
    done
    if [ -n "$PYSIDE_DIR" ] && [ ! -f "$PYSIDE_DIR/__init__.py" ]; then
        MISSING+=("PySide6/__init__.py")
    fi
    if [ -z "$PYSIDE_DIR" ]; then
        MISSING+=("PySide6 folder itself (couldn't locate)")
    fi
    if [ ${#MISSING[@]} -gt 0 ]; then
        echo "  ✗ SLIM REMOVED CRITICAL FILES:" >&2
        for m in "${MISSING[@]}"; do
            echo "      - $m" >&2
        done
        echo "  Re-run with SLIM=none to skip slim:" >&2
        echo "      SLIM=none ./packaging/build_dmg.sh" >&2
        exit 1
    fi
    echo "  ✓ Static file check passed (test the .app manually)"

    AFTER_SIZE=$(du -sh "$APP_BUNDLE" | cut -f1)
    AFTER_BYTES=$(du -sk "$APP_BUNDLE" | cut -f1)
    SAVED_MB=$(( (BEFORE_BYTES - AFTER_BYTES) / 1024 ))
    echo "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Before slim: $BEFORE_SIZE"
    echo "  After slim:  $AFTER_SIZE  ($SAVED_MB MB saved)"
    echo "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
else
    echo "  ✗ Unknown SLIM value: '$SLIM'. Use none/conservative/aggressive." >&2
    exit 1
fi
echo ""

# ─────────────────────────────────────────────────────────────────
# (Optional) Code signing — uncomment if you have a Developer ID
# ─────────────────────────────────────────────────────────────────
# CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
# echo "[5.5/6] Code-signing the .app..."
# codesign --force --deep --options runtime \
#     --sign "$CODESIGN_IDENTITY" \
#     "$APP_BUNDLE"

# ─────────────────────────────────────────────────────────────────
# 6. Wrap into .dmg
# ─────────────────────────────────────────────────────────────────
echo "[6/6] Creating CaptionLM.dmg..."
DMG_PATH="packaging/dist/CaptionLM.dmg"

# Re-stat the app bundle so the final summary reflects any slim savings.
APP_SIZE=$(du -sh "$APP_BUNDLE" | cut -f1)

create-dmg \
    --volname "CaptionLM" \
    --window-pos 200 120 \
    --window-size 600 380 \
    --icon-size 100 \
    --icon "CaptionLM.app" 150 180 \
    --hide-extension "CaptionLM.app" \
    --app-drop-link 450 180 \
    "$DMG_PATH" \
    "$APP_BUNDLE"

DMG_SIZE=$(du -h "$DMG_PATH" | cut -f1)
echo "  ✓ $DMG_PATH built ($DMG_SIZE)"
echo ""

# ─────────────────────────────────────────────────────────────────
# (Optional) Notarization — uncomment if you have a Developer ID
# ─────────────────────────────────────────────────────────────────
# echo "[6.5/6] Submitting to Apple notarization service..."
# xcrun notarytool submit "$DMG_PATH" \
#     --apple-id "your.apple.id@example.com" \
#     --team-id "TEAMID" \
#     --password "@keychain:AC_PASSWORD" \
#     --wait
# xcrun stapler staple "$DMG_PATH"

echo "════════════════════════════════════════════════════════════════"
echo "  ✓ DONE"
echo ""
echo "  App bundle: $APP_BUNDLE ($APP_SIZE)"
echo "  DMG:        $DMG_PATH ($DMG_SIZE)"
echo ""
echo "  Test locally:    open \"$APP_BUNDLE\""
echo "  Distribute via:  upload \"$DMG_PATH\" to GitHub Releases"
echo ""
echo "  NOTE: this .dmg is UNSIGNED. End users will see a Gatekeeper"
echo "  warning on first open. They need to right-click the .app and"
echo "  choose Open, or run:"
echo "    xattr -d com.apple.quarantine /Applications/CaptionLM.app"
echo ""
echo "  For a notarized build (no warning) you need a \$99/year Apple"
echo "  Developer account. See the commented codesign + notarytool"
echo "  blocks inside this script."
echo "════════════════════════════════════════════════════════════════"
