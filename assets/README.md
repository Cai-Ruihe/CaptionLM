# Top-level assets

This folder holds **brand assets** (icons, logos, marketing images). Files here are not loaded by the running app — they're for distribution channels (GitHub README, app icon for .dmg, etc.).

## Suggested file layout

| Filename | Source design | Used for |
|---|---|---|
| `logo-dark.png` | **dark navy bg, white "C"** (1st design) | README dark-mode badge, GitHub avatar |
| `logo-light.png` | **white bg, dark "C"** (2nd design) | README light-mode badge, social preview |
| `logo-color.png` | **orange "C"** (3rd design) | Primary brand color, marketing, screenshots |
| `icon.icns` | (generate from `logo-color.png`) | macOS `.app` icon (Dock, Launchpad) |
| `icon.ico` | (generate from `logo-color.png`) | Windows future port |

The 3 PNGs go here as-is. For the `.icns` (macOS app icon), use **Image2icon** (App Store) or this CLI sequence:

```bash
# from project root
mkdir -p assets/icon.iconset
sips -z 16   16   assets/logo-color.png --out assets/icon.iconset/icon_16x16.png
sips -z 32   32   assets/logo-color.png --out assets/icon.iconset/icon_16x16@2x.png
sips -z 32   32   assets/logo-color.png --out assets/icon.iconset/icon_32x32.png
sips -z 64   64   assets/logo-color.png --out assets/icon.iconset/icon_32x32@2x.png
sips -z 128  128  assets/logo-color.png --out assets/icon.iconset/icon_128x128.png
sips -z 256  256  assets/logo-color.png --out assets/icon.iconset/icon_128x128@2x.png
sips -z 256  256  assets/logo-color.png --out assets/icon.iconset/icon_256x256.png
sips -z 512  512  assets/logo-color.png --out assets/icon.iconset/icon_256x256@2x.png
sips -z 512  512  assets/logo-color.png --out assets/icon.iconset/icon_512x512.png
cp assets/logo-color.png assets/icon.iconset/icon_512x512@2x.png   # assumes source is 1024×1024
iconutil -c icns assets/icon.iconset -o assets/icon.icns
rm -rf assets/icon.iconset
```

The packaging script `packaging/setup_app.py` reads `assets/icon.icns` automatically if present.
