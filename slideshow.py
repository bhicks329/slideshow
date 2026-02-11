#!/usr/bin/env python3
"""
Full-screen photo slideshow for macOS.

Usage:
    ./run.sh /path/to/photos
    ./run.sh /path/to/photos --delay 10
    ./run.sh /path/to/photos --delay 5 --no-shuffle

Controls:
    Right arrow / Space  — next photo
    Left arrow           — previous photo
    Q / Escape           — quit
"""

import argparse
import os
import random
import sys
from pathlib import Path

import pygame
from PIL import Image, ImageOps

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".heic", ".webp"}

BLACK_THRESHOLD = 15
BORDER_SCAN_FRACTION = 0.02
MIN_CROP_RATIO = 0.80


def detect_black_borders(img: Image.Image) -> tuple:
    """Return (left, top, right, bottom) crop box with black borders removed."""
    gray = img.convert("L")
    w, h = gray.size

    scan_w = max(1, int(w * BORDER_SCAN_FRACTION))
    scan_h = max(1, int(h * BORDER_SCAN_FRACTION))

    def row_is_black(y: int) -> bool:
        strip = gray.crop((0, y, w, min(h, y + 1)))
        return max(strip.getdata()) < BLACK_THRESHOLD

    def col_is_black(x: int) -> bool:
        strip = gray.crop((x, 0, min(w, x + 1), h))
        return max(strip.getdata()) < BLACK_THRESHOLD

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
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


def prepare_image(path: str, screen_w: int, screen_h: int) -> pygame.Surface:
    """Load, fix orientation, remove borders, zoom-crop, return a pygame Surface."""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")

    box = detect_black_borders(img)
    if box != (0, 0, img.width, img.height):
        img = img.crop(box)

    img = zoom_crop_to_fit(img, screen_w, screen_h)

    # Convert Pillow image → pygame Surface
    raw = img.tobytes("raw", "RGB")
    surface = pygame.image.fromstring(raw, img.size, "RGB")
    return surface


def collect_photos(path: str) -> list:
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

    pygame.init()
    info = pygame.display.Info()
    screen_w, screen_h = info.current_w, info.current_h
    screen = pygame.display.set_mode((screen_w, screen_h), pygame.FULLSCREEN | pygame.NOFRAME)
    pygame.display.set_caption("Slideshow")
    pygame.mouse.set_visible(False)

    NEXT_SLIDE = pygame.USEREVENT + 1
    delay_ms = int(args.delay * 1000)
    pygame.time.set_timer(NEXT_SLIDE, delay_ms)

    index = 0
    current_surface = None

    def load_photo(idx):
        path = photos[idx]
        try:
            return prepare_image(path, screen_w, screen_h)
        except Exception as e:
            print(f"Skipping {path}: {e}", file=sys.stderr)
            return None

    current_surface = load_photo(index)

    clock = pygame.time.Clock()
    running = True

    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False

            elif event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_ESCAPE, pygame.K_q):
                    running = False
                elif event.key in (pygame.K_RIGHT, pygame.K_SPACE):
                    index = (index + 1) % len(photos)
                    current_surface = load_photo(index)
                    pygame.time.set_timer(NEXT_SLIDE, delay_ms)
                elif event.key == pygame.K_LEFT:
                    index = (index - 1) % len(photos)
                    current_surface = load_photo(index)
                    pygame.time.set_timer(NEXT_SLIDE, delay_ms)

            elif event.type == NEXT_SLIDE:
                index = (index + 1) % len(photos)
                current_surface = load_photo(index)

        screen.fill((0, 0, 0))
        if current_surface:
            screen.blit(current_surface, (0, 0))
        pygame.display.flip()
        clock.tick(30)

    pygame.quit()


if __name__ == "__main__":
    main()
