#!/usr/bin/env python3
"""
Full-screen photo slideshow for macOS with Ken Burns + cross-fade transitions.

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
import math
import os
import random
import sys
import threading
from pathlib import Path

import cv2
import numpy as np
import pygame
from PIL import Image, ImageOps

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".heic", ".webp"}

BLACK_THRESHOLD = 15
BORDER_SCAN_FRACTION = 0.02
MIN_CROP_RATIO = 0.80

# Ken Burns — image is loaded this much larger than the screen to allow pan/zoom room.
# Needs to be large enough that pan speed exceeds 1px/frame even at short delays.
KB_OVERSCAN = 1.22
# Zoom travel: how much the viewport shrinks to simulate zoom-in (fraction of screen size)
KB_ZOOM_TRAVEL = 0.08
# Max pan drift per slide (fraction of the available extra space, 0–1)
KB_PAN_DRIFT = 0.75

TRANSITION_SECS = 1.0  # cross-fade duration
FPS = 60


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


_face_cascade = None


def _get_face_cascade():
    global _face_cascade
    if _face_cascade is None:
        path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        _face_cascade = cv2.CascadeClassifier(path)
    return _face_cascade


def detect_face_focus(img: Image.Image):
    """Return (fx, fy) center of largest detected face in [0,1], or None if no face found.
    Runs on a downscaled copy for speed."""
    cascade = _get_face_cascade()

    # Downscale to max 800px wide for speed
    w, h = img.size
    scale = min(1.0, 800 / w)
    small = img.resize((int(w * scale), int(h * scale)), Image.BILINEAR)

    gray = np.array(small.convert("L"))
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30))

    if len(faces) == 0:
        return None

    # Largest face by area
    x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
    return ((x + fw / 2) / small.width, (y + fh / 2) / small.height)


def fit_to_screen(img: Image.Image, target_w: int, target_h: int) -> tuple:
    """Scale image to fit within target, centered on black background.
    Returns (pil_image, content_rect) where content_rect is (x, y, w, h) of the image
    within the canvas."""
    src_w, src_h = img.size
    scale = min(target_w / src_w, target_h / src_h)
    new_w = int(src_w * scale)
    new_h = int(src_h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGB", (target_w, target_h), (0, 0, 0))
    off_x = (target_w - new_w) // 2
    off_y = (target_h - new_h) // 2
    canvas.paste(img, (off_x, off_y))
    return canvas, (off_x, off_y, new_w, new_h)


def prepare_image(path: str, screen_w: int, screen_h: int) -> tuple:
    """Load, fix orientation, remove borders, fit to oversized canvas.
    Returns (surface, content_rect, focus_point) where:
      content_rect  — (x, y, w, h) of the actual image in the oversized canvas
      focus_point   — (fx, fy) center of largest detected face in [0,1], or None"""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")

    box = detect_black_borders(img)
    if box != (0, 0, img.width, img.height):
        img = img.crop(box)

    # Detect face before scaling
    focus_point = detect_face_focus(img)

    over_w = int(screen_w * KB_OVERSCAN)
    over_h = int(screen_h * KB_OVERSCAN)
    canvas, content_rect = fit_to_screen(img, over_w, over_h)

    raw = canvas.tobytes("raw", "RGB")
    surface = pygame.image.fromstring(raw, canvas.size, "RGB")
    return surface, content_rect, focus_point


def new_kb_params(focus_point=None) -> dict:
    """Generate Ken Burns start/end zoom and pan parameters for one slide.
    If focus_point=(fx,fy) is given (face center in [0,1]), the pan starts
    near the face and drifts slightly. Otherwise pan is fully random."""
    zoom_in = random.random() < 0.5
    z0 = 1.0 if zoom_in else 1.0 + KB_ZOOM_TRAVEL
    z1 = 1.0 + KB_ZOOM_TRAVEL if zoom_in else 1.0

    if focus_point is not None:
        px0 = max(0.0, min(1.0, focus_point[0]))
        py0 = max(0.0, min(1.0, focus_point[1]))
    else:
        px0 = random.random()
        py0 = random.random()

    angle = random.uniform(0, 2 * math.pi)
    dist = random.uniform(KB_PAN_DRIFT * 0.5, KB_PAN_DRIFT)
    px1 = max(0.0, min(1.0, px0 + math.cos(angle) * dist))
    py1 = max(0.0, min(1.0, py0 + math.sin(angle) * dist))

    return {
        "zoom_start": z0,
        "zoom_end": z1,
        "pan_start": (px0, py0),
        "pan_end": (px1, py1),
    }


def render_kb_frame(surface: pygame.Surface, screen_w: int, screen_h: int,
                    t: float, kb: dict, content_rect: tuple) -> pygame.Surface:
    """
    Return a (screen_w x screen_h) surface with Ken Burns pan/zoom applied.
    t: 0.0 (slide start) → 1.0 (slide end).
    content_rect: (x, y, w, h) of the actual image within the oversized surface.
    Pan is constrained to stay within content_rect so black borders never appear.
    """
    zoom = kb["zoom_start"] + (kb["zoom_end"] - kb["zoom_start"]) * t
    px = kb["pan_start"][0] + (kb["pan_end"][0] - kb["pan_start"][0]) * t
    py = kb["pan_start"][1] + (kb["pan_end"][1] - kb["pan_start"][1]) * t

    cx, cy, cw, ch = content_rect

    # Viewport size: smaller = more zoomed in
    viewport_w = int(screen_w / zoom)
    viewport_h = int(screen_h / zoom)

    # Pan range within image content only
    pan_range_x = max(0, cw - viewport_w)
    pan_range_y = max(0, ch - viewport_h)

    # If viewport is wider/taller than the image, center on the image content
    if pan_range_x > 0:
        ox = cx + int(px * pan_range_x)
    else:
        ox = cx + (cw - viewport_w) // 2

    if pan_range_y > 0:
        oy = cy + int(py * pan_range_y)
    else:
        oy = cy + (ch - viewport_h) // 2

    src_w, src_h = surface.get_size()
    ox = max(0, min(src_w - viewport_w, ox))
    oy = max(0, min(src_h - viewport_h, oy))

    crop = surface.subsurface((ox, oy, viewport_w, viewport_h))
    return pygame.transform.smoothscale(crop, (screen_w, screen_h))


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
    parser.add_argument("--delay", type=float, default=7.0,
                        help="Seconds each slide is shown (default: 7)")
    parser.add_argument("--no-shuffle", action="store_true",
                        help="Show photos in alphabetical order")
    args = parser.parse_args()

    photos = collect_photos(args.path)
    if not args.no_shuffle:
        random.shuffle(photos)

    print(f"Found {len(photos)} photo(s). Starting slideshow (delay: {args.delay}s). "
          "Press Q or Esc to quit.")

    pygame.init()
    info = pygame.display.Info()
    screen_w, screen_h = info.current_w, info.current_h
    screen = pygame.display.set_mode((screen_w, screen_h), pygame.FULLSCREEN | pygame.NOFRAME)
    pygame.display.set_caption("Slideshow")
    pygame.mouse.set_visible(False)

    delay_ms = int(args.delay * 1000)
    trans_ms = int(TRANSITION_SECS * 1000)

    # --- image loading helpers ---

    _preload_cache = {}   # index → surface
    _preload_lock = threading.Lock()

    def load_image(idx):
        path = photos[idx]
        try:
            return prepare_image(path, screen_w, screen_h)  # (surface, content_rect, focus_point)
        except Exception as e:
            print(f"Skipping {path}: {e}", file=sys.stderr)
            return None, None, None

    def preload(idx):
        """Load image in background thread and store in cache."""
        def _worker():
            result = load_image(idx)
            with _preload_lock:
                _preload_cache[idx] = result
        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    def get_image(idx):
        """Return (surface, content_rect) for idx, loading synchronously if not cached."""
        with _preload_lock:
            if idx in _preload_cache:
                return _preload_cache.pop(idx)
        return load_image(idx)

    # --- initial state ---

    index = 0
    current_surface, current_crect, current_focus = get_image(index)
    current_kb = new_kb_params(current_focus)
    next_surface, next_crect, next_focus = None, None, None
    next_kb = None

    # Preload next
    preload((index + 1) % len(photos))

    slide_start = pygame.time.get_ticks()
    trans_start = None          # set when cross-fade begins
    transitioning = False
    frozen_frame = None         # last Ken Burns frame of current slide, used during fade-out

    clock = pygame.time.Clock()
    running = True

    def begin_transition():
        nonlocal next_surface, next_crect, next_focus, next_kb
        nonlocal trans_start, transitioning, frozen_frame, index
        nonlocal slide_start, current_surface, current_crect, current_kb

        now = pygame.time.get_ticks()
        elapsed = now - slide_start
        t = min(1.0, elapsed / delay_ms)
        if current_surface and current_kb and current_crect:
            frozen_frame = render_kb_frame(current_surface, screen_w, screen_h, t, current_kb, current_crect)
        else:
            frozen_frame = None

        index = (index + 1) % len(photos)
        next_surface, next_crect, next_focus = get_image(index)
        next_kb = new_kb_params(next_focus)
        trans_start = pygame.time.get_ticks()
        transitioning = True

        preload((index + 1) % len(photos))

    def begin_transition_prev():
        nonlocal next_surface, next_crect, next_focus, next_kb
        nonlocal trans_start, transitioning, frozen_frame, index
        nonlocal slide_start, current_surface, current_crect, current_kb

        now = pygame.time.get_ticks()
        elapsed = now - slide_start
        t = min(1.0, elapsed / delay_ms)
        if current_surface and current_kb and current_crect:
            frozen_frame = render_kb_frame(current_surface, screen_w, screen_h, t, current_kb, current_crect)
        else:
            frozen_frame = None

        index = (index - 1) % len(photos)
        next_surface, next_crect, next_focus = get_image(index)
        next_kb = new_kb_params(next_focus)
        trans_start = pygame.time.get_ticks()
        transitioning = True

    while running:
        now = pygame.time.get_ticks()

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_ESCAPE, pygame.K_q):
                    running = False
                elif event.key in (pygame.K_RIGHT, pygame.K_SPACE):
                    begin_transition()
                elif event.key == pygame.K_LEFT:
                    begin_transition_prev()

        if transitioning:
            trans_elapsed = now - trans_start
            alpha = min(255, int(255 * trans_elapsed / trans_ms))

            # Draw frozen outgoing frame
            screen.fill((0, 0, 0))
            if frozen_frame:
                screen.blit(frozen_frame, (0, 0))

            # Draw incoming frame with increasing alpha
            if next_surface and next_kb and next_crect:
                t_next = min(1.0, trans_elapsed / delay_ms)
                incoming = render_kb_frame(next_surface, screen_w, screen_h, t_next, next_kb, next_crect)
                incoming.set_alpha(alpha)
                screen.blit(incoming, (0, 0))

            if trans_elapsed >= trans_ms:
                # Transition done — next becomes current.
                # Use trans_start (not now) so the Ken Burns t value is continuous
                # through the transition and doesn't jump back to 0.
                transitioning = False
                current_surface = next_surface
                current_crect = next_crect
                current_kb = next_kb
                next_surface, next_crect, next_focus = None, None, None
                next_kb = None
                frozen_frame = None
                slide_start = trans_start
        else:
            slide_elapsed = now - slide_start
            t = min(1.0, slide_elapsed / delay_ms)

            screen.fill((0, 0, 0))
            if current_surface and current_kb and current_crect:
                frame = render_kb_frame(current_surface, screen_w, screen_h, t, current_kb, current_crect)
                screen.blit(frame, (0, 0))

            # Auto-advance
            if slide_elapsed >= delay_ms:
                begin_transition()

        pygame.display.flip()
        clock.tick(FPS)

    pygame.quit()


if __name__ == "__main__":
    main()
