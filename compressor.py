"""
Small Compressor — compression engine.

Hits a user-chosen target file size while preserving as much visual detail as
possible. Strategy:

  * Modern encoders: AVIF (AV1), WebP, and high-efficiency JPEG, plus
    lossless/quantized PNG.
  * Binary search over each encoder's quality parameter to find the highest
    quality that still fits the target size.
  * Auto mode encodes every viable format, decodes the results, and scores
    each one against the original with a perceptual RMS metric — the winner
    is objectively the most faithful image at the requested size.
  * If a target is unreachable at full resolution, the image is progressively
    downscaled (with light sharpening) until it fits.
"""

from __future__ import annotations

import io
import math
import os
import time
from dataclasses import dataclass, field

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat

Image.MAX_IMAGE_PIXELS = None

try:
    import pillow_avif  # noqa: F401  (registers the AVIF codec)
    HAS_AVIF = True
except Exception:
    HAS_AVIF = False

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HAS_HEIF = True
except Exception:
    HAS_HEIF = False

AUTO = "Auto"
FMT_JPEG = "JPEG"
FMT_WEBP = "WebP"
FMT_AVIF = "AVIF"
FMT_PNG = "PNG"

# Larger = more compatible when two formats score nearly the same.
_COMPAT_RANK = {FMT_JPEG: 0, FMT_WEBP: 1, FMT_AVIF: 2, FMT_PNG: 0}
_QUALITY_RANGE = {FMT_JPEG: (1, 95), FMT_WEBP: (1, 100), FMT_AVIF: (1, 92)}
_EXT = {FMT_JPEG: ".jpg", FMT_WEBP: ".webp", FMT_AVIF: ".avif", FMT_PNG: ".png"}

_INPUT_EXTS = [
    ".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp", ".gif",
    ".tif", ".tiff",
]
if HAS_AVIF:
    _INPUT_EXTS.append(".avif")
if HAS_HEIF:
    _INPUT_EXTS += [".heic", ".heif", ".hif"]


@dataclass
class Result:
    src_path: str
    out_path: str = ""
    fmt: str = ""
    quality: int = 0
    out_bytes: int = 0
    src_bytes: int = 0
    width: int = 0
    height: int = 0
    src_width: int = 0
    src_height: int = 0
    scaled: bool = False
    rms: float = 0.0
    seconds: float = 0.0
    skipped: bool = False
    note: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and not self.skipped and bool(self.out_path)


def input_filter() -> str:
    """Windows file-dialog filter string."""
    exts = " ".join("*" + e for e in _INPUT_EXTS)
    all_known = f"Images ({exts})|{exts}|All files (*.*)|*.*"
    return all_known


def is_supported(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in _INPUT_EXTS


def _flatten_rgb(im: Image.Image) -> Image.Image:
    """Flatten any transparency onto white and force an RGB-compatible mode."""
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(bg, rgba).convert("RGB")
    if im.mode == "P":
        return im.convert("RGB")
    if im.mode == "L":
        return im
    return im.convert("RGB")


def _has_meaningful_alpha(im: Image.Image) -> bool:
    if im.mode not in ("RGBA", "LA") and not (im.mode == "P" and "transparency" in im.info):
        return False
    alpha = im.convert("RGBA").getchannel("A")
    lo, hi = alpha.getextrema()
    return lo < 250


def load_image(path: str) -> Image.Image:
    im = Image.open(path)
    im.load()
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGB", "RGBA", "L"):
        return im
    if im.mode == "LA":
        return im.convert("RGBA")
    if im.mode == "P":
        return im.convert("RGBA" if "transparency" in im.info else "RGB")
    return im.convert("RGB")


def _encode(im: Image.Image, fmt: str, quality: int) -> bytes:
    buf = io.BytesIO()
    meta = {}
    if "exif" in im.info:
        meta["exif"] = im.info["exif"]
    if "icc_profile" in im.info:
        meta["icc_profile"] = im.info["icc_profile"]
    if fmt == FMT_JPEG:
        params = {"quality": quality, "optimize": True, "progressive": True}
        params.update(meta)
        im.save(buf, FMT_JPEG, **params)
    elif fmt == FMT_WEBP:
        params = {"quality": quality, "method": 6}
        params.update(meta)
        im.save(buf, FMT_WEBP, **params)
    elif fmt == FMT_AVIF:
        params = {"quality": quality, "speed": 6}
        params.update(meta)
        im.save(buf, FMT_AVIF, **params)
    elif fmt == FMT_PNG:
        im.save(buf, FMT_PNG, optimize=True)
    else:
        raise ValueError(f"Unknown format: {fmt}")
    return buf.getvalue()


def _encode_tolerant(im: Image.Image, fmt: str, quality: int) -> bytes:
    """Encode, falling back to a metadata-free save if a codec rejects EXIF/ICC."""
    try:
        return _encode(im, fmt, quality)
    except Exception:
        if fmt == FMT_PNG:
            raise
        stripped = im.copy()
        stripped.info = {k: v for k, v in im.info.items()
                         if k not in ("exif", "icc_profile")}
        buf = io.BytesIO()
        if fmt == FMT_JPEG:
            stripped.save(buf, FMT_JPEG, quality=quality, optimize=True, progressive=True)
        elif fmt == FMT_WEBP:
            stripped.save(buf, FMT_WEBP, quality=quality, method=6)
        elif fmt == FMT_AVIF:
            stripped.save(buf, FMT_AVIF, quality=quality, speed=6)
        return buf.getvalue()


def _resize(im: Image.Image, scale: float) -> Image.Image:
    w = max(16, round(im.width * scale))
    h = max(16, round(im.height * scale))
    out = im.resize((w, h), Image.LANCZOS)
    # Light sharpening restores perceived detail after downscaling.
    if "A" in out.getbands():
        rgb = out.convert("RGB").filter(
            ImageFilter.UnsharpMask(radius=1.1, percent=55, threshold=2))
        out = Image.merge("RGBA", (*rgb.split(), out.getchannel("A")))
    else:
        out = out.filter(ImageFilter.UnsharpMask(radius=1.1, percent=55, threshold=2))
    return out


def perceptual_rms(original: Image.Image, encoded: bytes) -> float:
    """Root-mean-square pixel error (0-255 scale) between original and encoded."""
    try:
        dec = Image.open(io.BytesIO(encoded))
        dec.load()
    except Exception:
        return 999.0
    a = _flatten_rgb(original)
    b = _flatten_rgb(dec)
    if a.size != b.size:
        b = b.resize(a.size, Image.LANCZOS)
    long_side = max(a.size)
    if long_side > 320:
        s = 320 / long_side
        box = (max(8, round(a.width * s)), max(8, round(a.height * s)))
        a = a.resize(box, Image.LANCZOS)
        b = b.resize(box, Image.LANCZOS)
    diff = ImageChops.difference(a, b)
    rms = ImageStat.Stat(diff).rms
    return math.sqrt(sum(v * v for v in rms) / len(rms))


def _binary_search_quality(im: Image.Image, fmt: str, target: int,
                           cache: dict, cancel) -> tuple | None:
    """Largest quality whose encoded size fits target. Returns (quality, bytes)."""
    qmin, qmax = _QUALITY_RANGE[fmt]

    def size_at(q: int) -> int:
        if q not in cache:
            cache[q] = _encode_tolerant(im, fmt, q)
        return len(cache[q])

    if size_at(qmin) > target:
        return None
    lo, hi, best = qmin, qmax, qmin
    while lo < hi:
        if cancel():
            return None
        mid = (lo + hi + 1) // 2
        if size_at(mid) <= target:
            best, lo = mid, mid
        else:
            hi = mid - 1
    # Quality/size is near-monotonic but not perfectly; probe two steps up.
    for q in range(best + 1, min(best + 2, qmax) + 1):
        if size_at(q) <= target:
            best = q
        else:
            break
    return best, cache[best]


def _fit_lossy(im: Image.Image, fmt: str, target: int, allow_downscale: bool,
               cancel, progress) -> tuple | None:
    """Returns (bytes, quality, scale) or None if target unreachable."""
    qmin = _QUALITY_RANGE[fmt][0]
    work = _flatten_rgb(im) if fmt == FMT_JPEG else im
    if fmt in (FMT_WEBP, FMT_AVIF) and work.mode not in ("RGB", "RGBA"):
        work = work.convert("RGB")
    cache: dict = {}
    scale = 1.0
    for _ in range(7):
        if cancel():
            return None
        cur = work if scale >= 0.999 else _resize(work, scale)
        cache.clear()
        found = _binary_search_quality(cur, fmt, target, cache, cancel)
        if cancel():
            return None
        if found:
            quality, data = found
            return data, quality, scale
        if not allow_downscale:
            return None
        floor_size = len(cache[qmin])
        factor = (target / floor_size) ** 0.8
        factor = min(max(factor, 0.15), 0.9)
        new_scale = scale * factor
        if scale - new_scale < 0.02:
            new_scale = scale * 0.8
        progress(f"{fmt}: full resolution needs {floor_size / 1048576:.2f} MB "
                 f"at minimum quality — downscaling to {new_scale:.0%}")
        scale = new_scale
    return None


def _fit_png_lossless(im: Image.Image, target: int, cancel) -> bytes | None:
    if cancel():
        return None
    data = _encode_tolerant(im, FMT_PNG, 0)
    return data if len(data) <= target else None


_PALETTE_SIZES = [256, 192, 128, 96, 64, 48, 32, 24, 16, 12, 8, 6, 4]


def _fit_png_quantized(im: Image.Image, target: int, allow_downscale: bool,
                       cancel, progress) -> tuple | None:
    """Explicit-PNG mode: palette quantization, then downscaling if allowed."""
    work = im
    scale = 1.0
    for _ in range(7):
        if cancel():
            return None
        cur = work if scale >= 0.999 else _resize(work, scale)
        rgb = cur.convert("RGB")
        for colors in _PALETTE_SIZES:
            if cancel():
                return None
            progress(f"PNG: trying {colors}-color palette "
                     f"({cur.width}x{cur.height})")
            q = rgb.quantize(colors, method=Image.MEDIANCUT,
                             dither=Image.FLOYDSTEINBERG)
            buf = io.BytesIO()
            q.save(buf, FMT_PNG, optimize=True)
            data = buf.getvalue()
            if len(data) <= target:
                return data, colors, scale
        if not allow_downscale:
            return None
        scale = scale * 0.8 if scale > 0.2 else scale * 0.5
        if scale < 0.1:
            return None
    return None


def _auto_candidates(im: Image.Image) -> list:
    cands = []
    if HAS_AVIF:
        cands.append(FMT_AVIF)
    cands += [FMT_WEBP, FMT_JPEG]
    cands.append(FMT_PNG)
    if _has_meaningful_alpha(im):
        cands.remove(FMT_JPEG)
    return cands


def _pick_best(candidates: list) -> tuple | None:
    """candidates: list of dicts with fmt, bytes, quality, scale, rms.
    Prefer full-resolution results; then lowest RMS; then compatibility."""
    if not candidates:
        return None
    fullres = [c for c in candidates if c["scale"] >= 0.999]
    pool = fullres if fullres else candidates
    best = min(pool, key=lambda c: c["rms"])
    # Within 0.6 RMS (~visually identical) prefer the more compatible format.
    near = [c for c in pool if c["rms"] - best["rms"] <= 0.6]
    return min(near, key=lambda c: (_COMPAT_RANK[c["fmt"]], c["rms"],
                                    len(c["bytes"])))


def unique_output_path(src_path: str, ext: str) -> str:
    folder = os.path.dirname(src_path)
    stem = os.path.splitext(os.path.basename(src_path))[0]
    base = os.path.join(folder, f"{stem}_compressed{ext}")
    if not os.path.exists(base):
        return base
    n = 2
    while True:
        cand = os.path.join(folder, f"{stem}_compressed ({n}){ext}")
        if not os.path.exists(cand):
            return cand
        n += 1


def compress_file(src_path: str, target_bytes: int, output_format: str = AUTO,
                  allow_downscale: bool = True,
                  skip_if_already_under: bool = True,
                  cancel=None, progress=None) -> Result:
    """Compress one image to <= target_bytes, saving next to the original."""
    if cancel is None:
        cancel = lambda: False
    if progress is None:
        progress = lambda msg: None
    res = Result(src_path=src_path)
    res.src_bytes = os.path.getsize(src_path)
    t0 = time.perf_counter()
    try:
        progress(f"Loading {os.path.basename(src_path)}")
        im = load_image(src_path)
        res.src_width, res.src_height = im.size

        if skip_if_already_under and res.src_bytes <= target_bytes:
            res.skipped = True
            res.note = "already under target size — not recompressed"
            res.seconds = time.perf_counter() - t0
            return res

        if output_format == AUTO:
            formats = _auto_candidates(im)
        elif output_format == FMT_PNG:
            formats = [FMT_PNG]
        else:
            if output_format == FMT_AVIF and not HAS_AVIF:
                raise RuntimeError("AVIF encoder not available")
            formats = [output_format]

        scored = []
        for fmt in formats:
            if cancel():
                res.error = "cancelled"
                return res
            progress(f"Optimizing {os.path.basename(src_path)} as {fmt}")
            if fmt == FMT_PNG:
                if output_format == FMT_PNG:
                    found = _fit_png_quantized(im, target_bytes,
                                               allow_downscale, cancel, progress)
                else:
                    data = _fit_png_lossless(im, target_bytes, cancel)
                    found = (data, 100, 1.0) if data else None
            else:
                found = _fit_lossy(im, fmt, target_bytes, allow_downscale,
                                   cancel, progress)
            if not found:
                continue
            data, quality, scale = found
            scored.append({
                "fmt": fmt, "bytes": data, "quality": quality, "scale": scale,
                "rms": perceptual_rms(im, data),
            })

        if not scored:
            res.error = ("target too small even with maximum compression"
                         if not allow_downscale else
                         "could not reach the target size")
            res.seconds = time.perf_counter() - t0
            return res

        winner = _pick_best(scored)
        out_path = unique_output_path(src_path, _EXT[winner["fmt"]])
        with open(out_path, "wb") as fh:
            fh.write(winner["bytes"])

        res.out_path = out_path
        res.fmt = winner["fmt"]
        res.quality = winner["quality"]
        res.out_bytes = len(winner["bytes"])
        res.rms = winner["rms"]
        res.scaled = winner["scale"] < 0.999
        w, h = im.size
        if res.scaled:
            s = winner["scale"]
            res.width, res.height = max(1, round(w * s)), max(1, round(h * s))
        else:
            res.width, res.height = w, h
        res.seconds = time.perf_counter() - t0
        return res
    except Exception as exc:
        res.error = f"{type(exc).__name__}: {exc}"
        res.seconds = time.perf_counter() - t0
        return res
