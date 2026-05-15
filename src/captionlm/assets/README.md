# Asset placement

This folder holds the runtime image assets the app loads at startup.

## Required files

Drop your logo PNGs here with these exact filenames:

| Filename | Source design | Used for |
|---|---|---|
| `logo-overlay.png` | **dark navy with white "C" mark** | Subtitle overlay's bottom-left corner (22×22 px displayed, supply ≥ 256×256 for Retina sharpness) |

## Fallback

If `logo-overlay.png` is missing, the bottom-left button still renders (invisible) and clicking it still opens `https://github.com/Cai-Ruihe/CaptionLM`.
