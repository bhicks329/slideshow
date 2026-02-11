#!/usr/bin/env python3
"""
Full-screen photo slideshow for macOS.

Usage:
    python3 slideshow.py /path/to/photos
    python3 slideshow.py /path/to/photos --delay 10
    python3 slideshow.py /path/to/photos --delay 5 --no-shuffle

Controls:
    Right arrow / Space  — next photo
    Left arrow           — previous photo
    Q / Escape           — quit
    F                    — toggle fullscreen
"""

import argparse
import os
import random
import sys
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageTk, ImageFilter

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".heic", ".webp"}

BLACK_THRESHOLD = 15      # pixel values below this are "black"
BORDER_SCAN_FRACTION = 0.02  # scan this fraction of image width/height for border detection
MIN_CROP_RATIO = 0.80     # don't crop more than 20% of the image


def detect_black_borders(img: Image.Image) -> tuple[int, int, int, int]:
    """Return (left, top, right, bottom) crop box with black borders removed."""
    # Work on a small grayscale version for speed
    gray = img.convert("L")
    w, h = gray.size

    scan_w = max(1, int(w * BORDER_SCAN_FRACTION))
    scan_h = max(1, int(h * BORDER_SCAN_FRACTION))

    def row_is_black(y: int) -> bool:
        strip = gray.crop((0, y, w, y + 1))
        return max(strip.getdata()) < BLACK_THRESHOLD  # type: ignore[arg-type]

    def col_is_black(x: int) -> bool:
        strip = gray.crop((x, 0, x + 1, h))
        return max(strip.getdata()) < BLACK_THRESHOLD  # type: ignore[arg-type]

    # Scan inward from each edge
    top = 0
    while top < h * (1 - MIN_CROP_RATIO) and row_is_black(top):
        top += scan_h

    bottom = h - 1
    while bottom > h * MIN_CROP_RATIO and row_is_black(max(0, bottom - scan_h)):
        bottom -= scan_h

    left = 0
    while left < w * (1 - MIN_CROP_RATIO) and col_is_black(left):
        left += scan_w

    right = w - 1
    while right > w * MIN_CROP_RATIO and col_is_black(max(0, right - scan_w)):
        right -= scan_w

    # Round to scan step boundaries for cleanliness
    top = min(top, int(h * (1 - MIN_CROP_RATIO)))
    bottom = max(bottom, int(h * MIN_CROP_RATIO))
    left = min(left, int(w * (1 - MIN_CROP_RATIO)))
    right = max(right, int(w * MIN_CROP_RATIO))

    if right <= left or bottom <= top:
        return (0, 0, w, h)

    return (left, top, right + 1, bottom + 1)


def zoom_crop_to_fit(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Scale image up so it fills target dimensions, then center-crop."""
    src_w, src_h = img.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w = int(src_w * scale)
    new_h = int(src_h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    # Center crop
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


def prepare_image(path: str, screen_w: int, screen_h: int) -> Image.Image:
    """Load, clean borders, and fit image to screen."""
    img = Image.open(path)

    # Handle EXIF orientation
    try:
        from PIL import ImageOps
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    img = img.convert("RGB")

    # Remove black borders
    box = detect_black_borders(img)
    crop_w = box[2] - box[0]
    crop_h = box[3] - box[1]
    if crop_w < img.width or crop_h < img.height:
        img = img.crop(box)

    # Zoom-crop to fill the screen
    img = zoom_crop_to_fit(img, screen_w, screen_h)

    return img


class Slideshow:
    def __init__(self, root: tk.Tk, photos: list[str], delay_ms: int):
        self.root = root
        self.photos = photos
        self.delay_ms = delay_ms
        self.index = 0
        self._after_id = None

        root.title("Slideshow")
        root.configure(background="black")
        root.attributes("-fullscreen", True)

        self.label = tk.Label(root, bg="black", bd=0)
        self.label.pack(fill=tk.BOTH, expand=True)

        root.bind("<Escape>", self.quit)
        root.bind("q", self.quit)
        root.bind("Q", self.quit)
        root.bind("<Right>", self.next_photo)
        root.bind("<space>", self.next_photo)
        root.bind("<Left>", self.prev_photo)
        root.bind("f", self.toggle_fullscreen)
        root.bind("F", self.toggle_fullscreen)

        self._screen_w = root.winfo_screenwidth()
        self._screen_h = root.winfo_screenheight()

        self.show_photo()

    def quit(self, event=None):
        self.root.destroy()

    def toggle_fullscreen(self, event=None):
        state = self.root.attributes("-fullscreen")
        self.root.attributes("-fullscreen", not state)

    def show_photo(self):
        if self._after_id:
            self.root.after_cancel(self._after_id)

        path = self.photos[self.index]
        try:
            img = prepare_image(path, self._screen_w, self._screen_h)
            tk_img = ImageTk.PhotoImage(img)
            self.label.configure(image=tk_img)
            self.label.image = tk_img  # keep reference
        except Exception as e:
            print(f"Skipping {path}: {e}", file=sys.stderr)
            self.next_photo()
            return

        self._after_id = self.root.after(self.delay_ms, self.advance)

    def advance(self):
        self.index = (self.index + 1) % len(self.photos)
        self.show_photo()

    def next_photo(self, event=None):
        self.index = (self.index + 1) % len(self.photos)
        self.show_photo()

    def prev_photo(self, event=None):
        self.index = (self.index - 1) % len(self.photos)
        self.show_photo()


def collect_photos(path: str) -> list[str]:
    p = Path(path)
    if p.is_file():
        return [str(p)]
    photos = [
        str(f) for f in sorted(p.rglob("*"))
        if f.suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    if not photos:
        print(f"No supported photos found in {path}", file=sys.stderr)
        sys.exit(1)
    return photos


def main():
    parser = argparse.ArgumentParser(description="Full-screen photo slideshow")
    parser.add_argument("path", help="Photo file or folder")
    parser.add_argument("--delay", type=float, default=5.0, help="Seconds between slides (default: 5)")
    parser.add_argument("--no-shuffle", action="store_true", help="Show photos in alphabetical order")
    args = parser.parse_args()

    photos = collect_photos(args.path)
    if not args.no_shuffle:
        random.shuffle(photos)

    print(f"Found {len(photos)} photo(s). Starting slideshow (delay: {args.delay}s). Press Q or Esc to quit.")

    root = tk.Tk()
    Slideshow(root, photos, delay_ms=int(args.delay * 1000))
    root.mainloop()


if __name__ == "__main__":
    main()
