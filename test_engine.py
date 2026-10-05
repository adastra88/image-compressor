"""Quick engine verification: compress synthetic images to several targets."""
import os
import random
import sys
import tempfile
import time

from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compressor

MB = 1024 * 1024


def make_photo(path, size, colors=60):
    im = Image.new("RGB", size)
    dr = ImageDraw.Draw(im)
    rnd = random.Random(7)
    for _ in range(colors * 3):
        x, y = rnd.randint(0, size[0]), rnd.randint(0, size[1])
        r = rnd.randint(40, size[0] // 3)
        dr.ellipse([x - r, y - r, x + r, y + r],
                   fill=(rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255)))
    im = im.filter(ImageFilter.GaussianBlur(2))
    # sensor-like noise + sharp details so lossless PNG can't cheat
    noise = Image.effect_noise(size, 18).convert("RGB")
    im = Image.blend(im, noise, 0.35)
    dr = ImageDraw.Draw(im)
    for _ in range(250):
        x, y = rnd.randint(0, size[0] - 20), rnd.randint(0, size[1] - 20)
        w, h = rnd.randint(4, 18), rnd.randint(4, 18)
        dr.rectangle([x, y, x + w, y + h],
                     fill=(rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255)))
    im.save(path, "PNG")


def make_alpha(path, size=(1200, 800)):
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    dr = ImageDraw.Draw(im)
    dr.ellipse([100, 100, size[0] - 100, size[1] - 100], fill=(30, 120, 220, 255))
    # noise-textured disc keeps the PNG large so real compression kicks in
    noise = Image.effect_noise(size, 15)
    tex = Image.merge("RGBA", (noise, noise, noise, Image.new("L", size, 255)))
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).ellipse([100, 100, size[0] - 100, size[1] - 100], fill=255)
    im = Image.composite(tex, im, mask)
    im.save(path, "PNG")


def run():
    tmp = tempfile.mkdtemp(prefix="sc_test_")
    big = os.path.join(tmp, "big.png")
    small = os.path.join(tmp, "small.png")
    alpha = os.path.join(tmp, "alpha.png")
    make_photo(big, (3600, 2400))
    make_photo(small, (900, 600))
    make_alpha(alpha)
    print("src sizes:", {p: os.path.getsize(p) for p in (big, small, alpha)})

    failures = []

    def check(name, cond, extra=""):
        print(("PASS " if cond else "FAIL ") + name, extra)
        if not cond:
            failures.append(name)

    for name, path in (("big", big), ("small", small), ("alpha", alpha)):
        for target, label in ((int(1.0 * MB), "1MB"), (int(0.5 * MB), "0.5MB"),
                              (int(0.15 * MB), "150KB")):
            t0 = time.perf_counter()
            res = compressor.compress_file(path, target, progress=print)
            dt = time.perf_counter() - t0
            if res.skipped:
                check(f"auto {name}->{label}", res.src_bytes <= target,
                      f"correctly skipped (src {fmt_kb(res.src_bytes)} already under target)")
                continue
            check(f"auto {name}->{label}",
                  res.ok and res.out_bytes <= target and os.path.exists(res.out_path),
                  f"fmt={res.fmt} q={res.quality} out={fmt_kb(res.out_bytes)} "
                  f"rms={res.rms:.2f} scaled={res.scaled} {dt:.1f}s")
            if res.ok:
                # verify the file on disk matches the reported size
                check(f"auto {name}->{label} file-size",
                      os.path.getsize(res.out_path) == res.out_bytes)

    # explicit formats on big photo
    for fmt in (compressor.FMT_JPEG, compressor.FMT_WEBP, compressor.FMT_AVIF,
                compressor.FMT_PNG):
        res = compressor.compress_file(big, int(1.0 * MB), output_format=fmt,
                                       progress=lambda m: None)
        if fmt == compressor.FMT_PNG:
            check(f"explicit {fmt}", res.ok and res.out_bytes <= MB,
                  f"q={res.quality} out={fmt_kb(res.out_bytes)} err={res.error}")
        else:
            check(f"explicit {fmt}", res.ok and res.out_bytes <= MB,
                  f"q={res.quality} out={fmt_kb(res.out_bytes)} err={res.error}")

    # no downscale guard
    res = compressor.compress_file(big, int(0.05 * MB),
                                   output_format=compressor.FMT_JPEG,
                                   allow_downscale=False, progress=lambda m: None)
    check("tiny target, no downscale -> failure", not res.ok and not res.out_path,
          res.error)

    # already-under-target skip
    tiny = os.path.join(tmp, "tiny.jpg")
    Image.new("RGB", (200, 200), (10, 200, 30)).save(tiny, "JPEG", quality=80)
    res = compressor.compress_file(tiny, int(1.0 * MB), progress=lambda m: None)
    check("skip already-under-target", res.skipped and not res.out_path, res.note)

    # alpha preserved by WebP choice (not JPEG) in auto mode
    res = compressor.compress_file(alpha, int(0.5 * MB), progress=lambda m: None)
    check("alpha keeps alpha-capable format", res.ok and res.fmt != "JPEG", res.fmt)

    print("FAILURES:", failures if failures else "none")
    return 0 if not failures else 1


def fmt_kb(n):
    return f"{n / 1024:.0f}KB"


if __name__ == "__main__":
    sys.exit(run())
