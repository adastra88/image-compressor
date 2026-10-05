# Small Compressor

A tiny, fast Windows desktop app that compresses images down to a size you choose — **under 1 MB, 2 MB, 3 MB**, or any custom target — while keeping as much visible detail as that size allows. Compressed files are saved **next to the original**, with nothing to configure.

Built with Python + Tkinter and modern codecs (AVIF / AV1, WebP, optimized JPEG, PNG), it ships as a single self-contained `.exe` with no installer and no external dependencies.

![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows-0078D6)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Target-size compression** — set a byte budget and the app finds the highest quality that fits it.
- **Objective Auto mode** — encodes every viable format, then scores each result against the original with a perceptual metric and keeps the real winner.
- **Cutting-edge codecs** — AVIF (AV1), WebP, optimized progressive JPEG, plus lossless and quantized PNG.
- **Wide input support** — JPEG, PNG, WebP, AVIF, HEIC/HEIF (iPhone photos), BMP, GIF, TIFF.
- **Same-folder output** — `photo_compressed.webp` lands right beside `photo.jpg`.
- **Batch friendly** — drag & drop many images, watch per-file progress, cancel any time.
- **Metadata safe** — EXIF orientation, EXIF data, ICC color profiles, and transparency are preserved where the format supports them.
- **Optional downscaling** — if a target is physically impossible at full resolution, the image is progressively resized (Lanczos + light sharpening) instead of turning to mush.
- **Smart skipping** — files already under the target are skipped untouched, because re-encoding an already-small JPEG only adds loss.

## Download

Grab `SmallCompressor.exe` from the [Releases](../../releases) page and run it. No installation required.

## Usage

1. **Add images** — drag & drop them into the window, or click *Add images…*
2. **Pick a target size** — presets (100 KB … 10 MB) or a custom value in KB/MB
3. **Choose an output format** — or leave it on **Auto (best quality)**
4. Hit **⚡ Compress** — results appear below; double-click a row to open the folder

## How it stays sharp at small sizes

| Technique | What it does |
|---|---|
| Binary-searched quality | For each encoder, finds the *highest* quality setting that still fits the target byte budget. |
| Perceptual Auto ranking | Encodes all viable formats, decodes each, and scores it against the original with a perceptual RMS metric. Ties go to the more widely compatible format (JPEG/WebP over AVIF). |
| Downscale fallback | If even minimum quality overshoots the target, the image is progressively downscaled until it fits. Can be disabled. |
| Alpha awareness | JPEG is excluded as a candidate when meaningful transparency is present. |
| Metadata preservation | EXIF/ICC are carried through with tolerant fallbacks when a format can't store everything. |

## Building from source

Requires **Python 3.12+** on Windows.

```bash
pip install pillow pillow-avif-plugin pillow-heif tkinterdnd2
python app.py
```

To produce the standalone single-file executable:

```bash
pip install pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name SmallCompressor ^
    --collect-all tkinterdnd2 --collect-all pillow_avif --collect-all pillow_heif app.py
```

The exe lands in `dist\` (~45 MB, all codecs bundled).

## Testing

```bash
python test_engine.py     # end-to-end engine verification
python app.py --selftest  # engine smoke test, writes selftest_report.txt
```

Both suites cover target-size fitting at 1 MB / 0.5 MB / 150 KB, alpha handling, explicit-format output, skip behavior, and the no-downscale guard.

## Project layout

| File | Purpose |
|------|---------|
| `compressor.py` | Compression engine — encoding, quality search, perceptual scoring |
| `app.py` | Tkinter GUI — drag & drop, batch queue, settings, self-test |
| `test_engine.py` | Standalone engine test suite |
| `SmallCompressor.spec` | PyInstaller build recipe |

## License

MIT — see [LICENSE](LICENSE).
