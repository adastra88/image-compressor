"""
Small Compressor — a tiny Windows desktop app that squeezes images down to a
user-chosen target size (e.g. under 1 MB) while keeping as much detail as the
size allows. Uses AVIF/WebP/JPEG encoders with binary-searched quality and an
objective perceptual-quality ranking in Auto mode.

Run:            python app.py
Self-test:      python app.py --selftest
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import compressor

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _BASE = TkinterDnD.Tk
    _HAS_DND = True
except Exception:
    _BASE = tk.Tk
    _HAS_DND = False

APP_NAME = "Small Compressor"
SETTINGS_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "SmallCompressor")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")

PRESETS = ["100 KB", "250 KB", "500 KB", "1 MB", "2 MB", "3 MB", "5 MB", "10 MB"]
FORMATS = ["Auto (best quality)"] + [compressor.FMT_JPEG, compressor.FMT_WEBP]
if compressor.HAS_AVIF:
    FORMATS.append(compressor.FMT_AVIF)
FORMATS.append(compressor.FMT_PNG)

MB = 1024 * 1024
KB = 1024


def fmt_size(n: float) -> str:
    if n >= MB:
        return f"{n / MB:.2f} MB"
    if n >= KB:
        return f"{n / KB:.0f} KB"
    return f"{n:.0f} B"


def parse_target(text: str, unit: str) -> int:
    value = float(text.replace(",", "."))
    if value <= 0:
        raise ValueError
    mult = MB if unit == "MB" else KB
    return int(value * mult)


def fmt_to_preset(fmt: str) -> str:
    return {
        compressor.FMT_JPEG: "JPEG",
        compressor.FMT_WEBP: "WebP",
        compressor.FMT_AVIF: "AVIF",
        compressor.FMT_PNG: "PNG",
    }.get(fmt, "Auto (best quality)")


class App(_BASE):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("680x620")
        self.minsize(620, 560)
        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._cancel = threading.Event()
        self._files: list[str] = []
        self._load_settings()
        self._build_ui()
        if _HAS_DND:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._poll)

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}
        self.columnconfigure(0, weight=1)
        self.rowconfigure(5, weight=1)

        sub = ("Squeezes images down to a target size with AVIF / WebP / JPEG"
               + ("" if compressor.HAS_AVIF else "") +
               " — picking the format that keeps the most detail.")
        head = ttk.Frame(self)
        head.grid(row=0, column=0, sticky="ew", **pad)
        ttk.Label(head, text=f"\U0001F5DC {APP_NAME}",
                  font=("Segoe UI Semibold", 15)).pack(side="left")
        ttk.Label(head, text=sub, foreground="#666").pack(side="left", padx=12)

        # --- images list
        grp_files = ttk.LabelFrame(self, text=" Images ")
        grp_files.grid(row=1, column=0, sticky="ew", **pad)
        grp_files.columnconfigure(0, weight=1)
        self._list = tk.Listbox(grp_files, height=6, activestyle="none")
        self._list.grid(row=0, column=0, rowspan=1, sticky="ew", padx=8, pady=6)
        sb = ttk.Scrollbar(grp_files, command=self._list.yview)
        sb.grid(row=0, column=1, sticky="ns", pady=6)
        self._list.config(yscrollcommand=sb.set)
        btns = ttk.Frame(grp_files)
        btns.grid(row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 8))
        ttk.Button(btns, text="Add images…", command=self._add_files).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="Remove selected", command=self._remove_selected).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="Clear", command=self._clear_files).pack(side="left")
        hint = "(drag & drop supported)" if _HAS_DND else ""
        ttk.Label(btns, text=f"  {hint}", foreground="#999").pack(side="left")

        # --- target size
        grp_target = ttk.LabelFrame(self, text=" Target size (compressed file will be under this) ")
        grp_target.grid(row=2, column=0, sticky="ew", **pad)
        ttk.Label(grp_target, text="Preset:").grid(row=0, column=0, padx=8, pady=8)
        self._preset = tk.StringVar()
        cb = ttk.Combobox(grp_target, textvariable=self._preset, values=PRESETS,
                          state="readonly", width=10)
        cb.grid(row=0, column=1, padx=(0, 14))
        cb.bind("<<ComboboxSelected>>", self._on_preset)
        ttk.Label(grp_target, text="or custom:").grid(row=0, column=2)
        self._target_value = tk.StringVar()
        ent = ttk.Entry(grp_target, textvariable=self._target_value, width=8)
        ent.grid(row=0, column=3, padx=4)
        self._unit = tk.StringVar(value="MB")
        ttk.Combobox(grp_target, textvariable=self._unit, values=["KB", "MB"],
                     state="readonly", width=4).grid(row=0, column=4, padx=(0, 8))
        ttk.Label(grp_target, text="Resize smaller if needed:").grid(row=0, column=5, padx=(16, 2))
        self._allow_scale = tk.BooleanVar(value=True)
        ttk.Checkbutton(grp_target, variable=self._allow_scale).grid(row=0, column=6)

        # --- output options
        grp_out = ttk.LabelFrame(self, text=" Output ")
        grp_out.grid(row=3, column=0, sticky="ew", **pad)
        ttk.Label(grp_out, text="Format:").grid(row=0, column=0, padx=8, pady=8)
        self._fmt = tk.StringVar(value=FORMATS[0])
        fmt_box = ttk.Combobox(grp_out, textvariable=self._fmt, values=FORMATS,
                               state="readonly", width=22)
        fmt_box.grid(row=0, column=1, padx=(0, 16))
        self._recompress = tk.BooleanVar(value=False)
        ttk.Checkbutton(grp_out, text="Recompress even if already under target",
                        variable=self._recompress).grid(row=0, column=2)

        # --- action row
        action = ttk.Frame(self)
        action.grid(row=4, column=0, sticky="ew", **pad)
        self._go = ttk.Button(action, text="⚡ Compress", command=self._start)
        self._go.pack(side="left")
        self._stop = ttk.Button(action, text="Cancel", command=self._cancel_job,
                                state="disabled")
        self._stop.pack(side="left", padx=8)
        self._bar = ttk.Progressbar(action, mode="indeterminate", length=180)
        self._bar.pack(side="left", padx=8)
        self._status = ttk.Label(action, text="Ready.", foreground="#444")
        self._status.pack(side="left", padx=8)

        # --- results
        grp_res = ttk.LabelFrame(self, text=" Results ")
        grp_res.grid(row=5, column=0, sticky="nsew", **pad)
        cols = ("out", "fmt", "quality", "size", "saved")
        self._tree = ttk.Treeview(grp_res, columns=cols, show="headings", height=7)
        for cid, text, w, anchor in [
            ("out", "Image", 190, "w"),
            ("fmt", "Format", 60, "center"),
            ("quality", "Quality", 90, "center"),
            ("size", "Size", 130, "center"),
            ("saved", "Saved as", 240, "w"),
        ]:
            self._tree.heading(cid, text=text)
            self._tree.column(cid, width=w, anchor=anchor)
        rsb = ttk.Scrollbar(grp_res, command=self._tree.yview)
        self._tree.configure(yscrollcommand=rsb.set)
        self._tree.grid(row=0, column=0, sticky="nsew", padx=8, pady=(6, 4))
        rsb.grid(row=0, column=1, sticky="ns", pady=(6, 4))
        self._tree.bind("<Double-1>", self._open_result)
        ttk.Label(grp_res, text="Double-click a result to open its folder.",
                  foreground="#999").grid(row=1, column=0, sticky="w", padx=8, pady=(0, 6))
        grp_res.rowconfigure(0, weight=1)
        grp_res.columnconfigure(0, weight=1)

        self._apply_settings()

    # ------------------------------------------------------------ actions
    def _add_files(self):
        paths = filedialog.askopenfilenames(
            parent=self, title="Choose images to compress",
            initialdir=self._last_dir,
            filetypes=[("Images", "*.jpg *.jpeg *.jfif *.png *.webp *.bmp *.gif "
                                  "*.tif *.tiff"
                                  + (" *.avif" if compressor.HAS_AVIF else "")
                                  + (" *.heic *.heif *.hif" if compressor.HAS_HEIF else "")),
                       ("All files", "*.*")])
        self._append_files(list(paths))

    def _on_drop(self, event):
        files = self.tk.splitlist(event.data)
        self._append_files([f for f in files if os.path.isfile(f)])

    def _append_files(self, paths: list):
        added = 0
        for p in paths:
            if compressor.is_supported(p) and p not in self._files:
                self._files.append(os.path.abspath(p))
                self._last_dir = os.path.dirname(os.path.abspath(p))
                added += 1
        self._refresh_list()
        if paths and added == 0:
            self._set_status("No new supported images in that selection.", warn=True)

    def _refresh_list(self):
        self._list.delete(0, tk.END)
        for p in self._files:
            self._list.insert(tk.END, p)

    def _remove_selected(self):
        for idx in sorted(self._list.curselection(), reverse=True):
            del self._files[idx]
        self._refresh_list()

    def _clear_files(self):
        self._files.clear()
        self._refresh_list()

    def _on_preset(self, _event=None):
        preset = self._preset.get()
        value, unit = preset.split()
        self._target_value.set(value)
        self._unit.set(unit)

    def _current_target(self) -> int:
        text = self._target_value.get().strip()
        if not text and self._preset.get():
            text, _ = self._preset.get().split()
        return parse_target(text, self._unit.get())

    def _start(self):
        if not self._files:
            messagebox.showinfo(APP_NAME, "Add at least one image first.")
            return
        try:
            target = self._current_target()
        except ValueError:
            messagebox.showerror(APP_NAME, "Enter a valid target size, e.g. 2 MB or 500 KB.")
            return
        fmt = self._fmt.get()
        output_format = compressor.AUTO if fmt.startswith("Auto") else fmt
        self._cancel.clear()
        self._go.config(state="disabled")
        self._stop.config(state="normal")
        self._bar.start(12)
        self._worker = threading.Thread(target=self._run_job, args=(
            list(self._files), target, output_format,
            self._allow_scale.get(), self._recompress.get()), daemon=True)
        self._worker.start()

    def _cancel_job(self):
        self._cancel.set()
        self._set_status("Cancelling…")

    def _run_job(self, files, target, output_format, allow_scale, recompress):
        q = self._queue
        q.put(("clear_results", None))
        total = len(files)
        for i, path in enumerate(files, 1):
            if self._cancel.is_set():
                break
            q.put(("status", f"[{i}/{total}] {os.path.basename(path)}"))
            res = compressor.compress_file(
                path, target, output_format=output_format,
                allow_downscale=allow_scale,
                skip_if_already_under=not recompress,
                cancel=self._cancel.is_set,
                progress=lambda msg: q.put(("detail", msg)))
            q.put(("result", res))
        q.put(("done", None))

    # -------------------------------------------------------------- polling
    def _poll(self):
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "status":
                    self._set_status(payload)
                elif kind == "detail":
                    self._set_status(payload)
                elif kind == "result":
                    self._add_result(payload)
                elif kind == "clear_results":
                    for item in self._tree.get_children():
                        self._tree.delete(item)
                elif kind == "done":
                    self._bar.stop()
                    self._go.config(state="normal")
                    self._stop.config(state="disabled")
                    if self._cancel.is_set():
                        self._set_status("Cancelled.", warn=True)
                    else:
                        self._set_status("Done.")
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def _set_status(self, text: str, warn: bool = False):
        self._status.config(text=text, foreground="#a33" if warn else "#444")

    def _add_result(self, res: compressor.Result):
        name = os.path.basename(res.src_path)
        if res.error:
            self._tree.insert("", "end", values=(
                name, "—", "—", "—", f"❌ {res.error}"))
            return
        if res.skipped:
            self._tree.insert("", "end", values=(
                name, "—", "—", fmt_size(res.src_bytes),
                f"⏭ {res.note}"))
            return
        quality = f"{res.quality} colors" if res.fmt == compressor.FMT_PNG and res.quality < 100 \
            else f"q{res.quality}"
        scale = f", {res.width}×{res.height}" if res.scaled else ""
        size_txt = f"{fmt_size(res.src_bytes)} → {fmt_size(res.out_bytes)}"
        self._tree.insert("", "end", values=(
            name, res.fmt, quality + scale, size_txt, res.out_path))

    def _open_result(self, _event=None):
        sel = self._tree.selection()
        if not sel:
            return
        out_path = self._tree.item(sel[0], "values")[4]
        folder = out_path if os.path.isdir(out_path) else os.path.dirname(out_path)
        if folder and os.path.isdir(folder):
            os.startfile(folder)

    # ----------------------------------------------------------- settings
    def _load_settings(self):
        self._settings = {}
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as fh:
                self._settings = json.load(fh)
        except Exception:
            pass

    def _apply_settings(self):
        s = self._settings
        if s.get("preset") in PRESETS:
            self._preset.set(s["preset"])
            self._on_preset()
        elif s.get("target_value") and s.get("unit") in ("KB", "MB"):
            self._target_value.set(str(s["target_value"]))
            self._unit.set(s["unit"])
        else:
            self._preset.set("2 MB")
            self._on_preset()
        if s.get("fmt") in FORMATS:
            self._fmt.set(s["fmt"])
        if "allow_scale" in s:
            self._allow_scale.set(bool(s["allow_scale"]))
        if "recompress" in s:
            self._recompress.set(bool(s["recompress"]))
        last_dir = s.get("last_dir")
        if last_dir and os.path.isdir(last_dir):
            self._last_dir = last_dir
        else:
            self._last_dir = os.path.expanduser("~")

    def _save_settings(self):
        s = {
            "preset": self._preset.get(),
            "target_value": self._target_value.get(),
            "unit": self._unit.get(),
            "fmt": self._fmt.get(),
            "allow_scale": self._allow_scale.get(),
            "recompress": self._recompress.get(),
            "last_dir": self._last_dir,
        }
        try:
            os.makedirs(SETTINGS_DIR, exist_ok=True)
            with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
                json.dump(s, fh)
        except Exception:
            pass

    def _on_close(self):
        self._save_settings()
        self._cancel.set()
        self.destroy()


def run_selftest(report_path: str) -> bool:
    """Exercise the engine on synthetic images and write a report file."""
    import random
    import tempfile
    from PIL import Image, ImageDraw, ImageFilter

    lines = []
    ok = True
    try:
        tmp = tempfile.mkdtemp(prefix="sc_selftest_")

        def make_photo(name, size, colors=48):
            im = Image.new("RGB", size)
            dr = ImageDraw.Draw(im)
            rnd = random.Random(7)
            for _ in range(colors * 3):
                x, y = rnd.randint(0, size[0]), rnd.randint(0, size[1])
                r = rnd.randint(40, size[0] // 3)
                dr.ellipse([x - r, y - r, x + r, y + r],
                           fill=(rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255)))
            im = im.filter(ImageFilter.GaussianBlur(2))
            noise = Image.effect_noise(size, 18).convert("RGB")
            im = Image.blend(im, noise, 0.35)
            dr = ImageDraw.Draw(im)
            for _ in range(250):
                x, y = rnd.randint(0, size[0] - 20), rnd.randint(0, size[1] - 20)
                w, h = rnd.randint(4, 18), rnd.randint(4, 18)
                dr.rectangle([x, y, x + w, y + h],
                             fill=(rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255)))
            p = os.path.join(tmp, name)
            im.save(p, "PNG")
            return p

        def make_alpha(name, size=(1200, 800)):
            im = Image.new("RGBA", size, (0, 0, 0, 0))
            dr = ImageDraw.Draw(im)
            dr.ellipse([100, 100, size[0] - 100, size[1] - 100], fill=(30, 120, 220, 255))
            noise = Image.effect_noise(size, 15)
            tex = Image.merge("RGBA", (noise, noise, noise, Image.new("L", size, 255)))
            mask = Image.new("L", size, 0)
            ImageDraw.Draw(mask).ellipse([100, 100, size[0] - 100, size[1] - 100], fill=255)
            im = Image.composite(tex, im, mask)
            p = os.path.join(tmp, name)
            im.save(p, "PNG")
            return p

        targets = [int(1.0 * MB), int(0.5 * MB), int(0.15 * MB)]
        cases = [
            ("photo_big.png", make_photo("photo_big.png", (3600, 2400))),
            ("photo_small.png", make_photo("photo_small.png", (900, 600))),
            ("alpha.png", make_alpha("alpha.png")),
        ]

        for name, path in cases:
            for target in targets:
                res = compressor.compress_file(path, target, output_format=compressor.AUTO,
                                               progress=lambda m: None)
                if res.skipped:
                    good = res.src_bytes <= target
                    lines.append(f"{'PASS' if good else 'FAIL'} {name} target={target} "
                                 f"skipped (src={res.src_bytes} already under target)")
                else:
                    good = res.ok and res.out_bytes <= target
                    lines.append(f"{'PASS' if good else 'FAIL'} {name} target={target} "
                                 f"fmt={res.fmt} q={res.quality} out={res.out_bytes} "
                                 f"src={res.src_bytes} rms={res.rms:.2f} err={res.error}")
                ok &= good

        # explicit format runs
        for fmt in (compressor.FMT_JPEG, compressor.FMT_WEBP, compressor.FMT_PNG):
            res = compressor.compress_file(cases[0][1], int(1.0 * MB), output_format=fmt,
                                           progress=lambda m: None)
            good = res.ok and res.out_bytes <= int(1.0 * MB)
            ok &= good
            lines.append(f"{'PASS' if good else 'FAIL'} explicit-{fmt} fmt={res.fmt} "
                         f"out={res.out_bytes} err={res.error}")
        if compressor.HAS_AVIF:
            res = compressor.compress_file(cases[0][1], int(1.0 * MB),
                                           output_format=compressor.FMT_AVIF,
                                           progress=lambda m: None)
            good = res.ok and res.out_bytes <= int(1.0 * MB)
            ok &= good
            lines.append(f"{'PASS' if good else 'FAIL'} explicit-AVIF fmt={res.fmt} "
                         f"out={res.out_bytes} err={res.error}")
        # no-downscale guard
        res = compressor.compress_file(cases[0][1], int(0.05 * MB),
                                       output_format=compressor.FMT_JPEG,
                                       allow_downscale=False, progress=lambda m: None)
        good = (not res.ok) and "target" in res.error
        ok &= good
        lines.append(f"{'PASS' if good else 'FAIL'} tiny-target-no-scale err={res.error}")
    except Exception as exc:
        ok = False
        lines.append(f"EXCEPTION {type(exc).__name__}: {exc}")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return ok


def main():
    if "--selftest" in sys.argv:
        root = tk.Tk()
        root.withdraw()
        report = os.path.abspath("selftest_report.txt")
        ok = run_selftest(report)
        root.destroy()
        print("SELFTEST", "PASS" if ok else "FAIL", "->", report)
        sys.exit(0 if ok else 1)
    App().mainloop()


if __name__ == "__main__":
    main()
