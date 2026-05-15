# Packaging

This directory contains scripts to build `CaptionLM.app` and wrap it in a `.dmg` for distribution.

## Prerequisites (one-time)

```bash
brew install create-dmg
pip install py2app
```

## Build

From the project root:

```bash
./packaging/build_dmg.sh
```

Outputs:
- `packaging/dist/CaptionLM.app` — the `.app` bundle (run-able locally)
- `packaging/dist/CaptionLM.dmg` — distributable `.dmg`

## Distribution caveat

The `.dmg` is **unsigned**. When users download it from GitHub, macOS Gatekeeper will block it on first open. They have two options:

1. Right-click `CaptionLM.app` → **Open** (allows once, then trusts forever)
2. `xattr -d com.apple.quarantine /Applications/CaptionLM.app` (removes the quarantine flag entirely)

Document this on the GitHub Releases page.

## Optional: signing + notarization

To eliminate the Gatekeeper warning, you need a $99/year Apple Developer account. Then:

1. Get a Developer ID Application certificate via Xcode → Settings → Accounts → Manage Certificates
2. Store an app-specific password in Keychain:
   ```bash
   xcrun notarytool store-credentials "AC_PASSWORD" \
     --apple-id "your.apple.id@example.com" \
     --team-id "ABCDE12345" \
     --password "abcd-efgh-ijkl-mnop"
   ```
3. Uncomment the `codesign` and `xcrun notarytool` blocks in `build_dmg.sh`
4. Re-run `./packaging/build_dmg.sh`

The resulting `.dmg` is notarized — users will see "CaptionLM was downloaded from the internet" once, then it'll open without further warnings.
