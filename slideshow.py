#!/usr/bin/env python3
"""
Full-screen photo slideshow for macOS with Ken Burns + cross-fade transitions.

Usage:
    ./run.sh /path/to/photos
    ./run.sh /path/to/photos --delay 10
    ./run.sh /path/to/photos --delay 5 --no-shuffle
    ./run.sh /path/to/photos --display 1

Controls:
    Right arrow / Space  — next photo
    Left arrow           — previous photo
    P                    — pause / resume
    Q / Escape           — quit
"""

import argparse
import ctypes
import json
import ctypes.util
import math
import os
import random
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import pygame
from pygame._sdl2.video import Renderer, Texture
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
# The cascade is not thread-safe, and preload and manual skips can detect at the same time
_face_lock = threading.Lock()


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
    with _face_lock:
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

    # Detect face before scaling (operates on original image coordinates)
    raw_focus = detect_face_focus(img)

    over_w = int(screen_w * KB_OVERSCAN)
    over_h = int(screen_h * KB_OVERSCAN)
    canvas, content_rect = fit_to_screen(img, over_w, over_h)

    # Convert face position (fraction of image) → pan coordinate (fraction of pan range).
    # In kb_viewport, ox = cx + px * (cw - viewport_w), so px=0 puts the viewport's
    # left edge at the content left, and px=1 puts it at the right.  To *center* the face
    # we need: cx + px*(cw-vw) + vw/2 == cx + fx*cw  →  px = (fx*cw - vw/2) / (cw - vw)
    focus_point = None
    if raw_focus is not None:
        fx, fy = raw_focus
        _, _, cw, ch = content_rect
        # Use zoom=1.0 viewport size for the conversion (conservative / largest viewport)
        vw, vh = screen_w, screen_h
        prx = cw - vw
        pry = ch - vh
        px = max(0.0, min(1.0, (fx * cw - vw / 2) / prx)) if prx > 0 else 0.5
        py = max(0.0, min(1.0, (fy * ch - vh / 2) / pry)) if pry > 0 else 0.5
        focus_point = (px, py)

    raw = canvas.tobytes("raw", "RGB")
    surface = pygame.image.frombytes(raw, canvas.size, "RGB")
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
        # Small drift so the face stays in frame
        max_drift = KB_PAN_DRIFT * 0.25
    else:
        px0 = random.random()
        py0 = random.random()
        max_drift = KB_PAN_DRIFT

    angle = random.uniform(0, 2 * math.pi)
    dist = random.uniform(max_drift * 0.5, max_drift)
    px1 = max(0.0, min(1.0, px0 + math.cos(angle) * dist))
    py1 = max(0.0, min(1.0, py0 + math.sin(angle) * dist))

    return {
        "zoom_start": z0,
        "zoom_end": z1,
        "pan_start": (px0, py0),
        "pan_end": (px1, py1),
    }


def kb_viewport(t: float, kb: dict, content_rect: tuple, canvas_size: tuple,
                screen_w: int, screen_h: int) -> tuple:
    """
    Return the (x, y, w, h) part of the oversized canvas to show at time t, as floats
    so pan and zoom move by fractions of a pixel instead of stepping.
    t: 0.0 (slide start) → 1.0 (slide end).
    content_rect: (x, y, w, h) of the actual image within the oversized canvas.
    Pan is constrained to stay within content_rect so black borders never appear.
    """
    zoom = kb["zoom_start"] + (kb["zoom_end"] - kb["zoom_start"]) * t
    px = kb["pan_start"][0] + (kb["pan_end"][0] - kb["pan_start"][0]) * t
    py = kb["pan_start"][1] + (kb["pan_end"][1] - kb["pan_start"][1]) * t

    cx, cy, cw, ch = content_rect

    # Viewport size: smaller = more zoomed in
    viewport_w = screen_w / zoom
    viewport_h = screen_h / zoom

    # Pan range within image content only
    pan_range_x = max(0.0, cw - viewport_w)
    pan_range_y = max(0.0, ch - viewport_h)

    # If viewport is wider/taller than the image, center on the image content
    ox = cx + px * pan_range_x if pan_range_x > 0 else cx + (cw - viewport_w) / 2
    oy = cy + py * pan_range_y if pan_range_y > 0 else cy + (ch - viewport_h) / 2

    src_w, src_h = canvas_size
    ox = max(0.0, min(src_w - viewport_w, ox))
    oy = max(0.0, min(src_h - viewport_h, oy))
    return ox, oy, viewport_w, viewport_h


def draw_kb_frame(texture, screen_w: int, screen_h: int, t: float, kb: dict,
                  content_rect: tuple, alpha: int = 255):
    """Draw texture with Ken Burns pan/zoom applied, filling the screen."""
    ox, oy, vw, _ = kb_viewport(t, kb, content_rect, (texture.width, texture.height),
                                screen_w, screen_h)
    scale = screen_w / vw
    texture.alpha = alpha
    texture.draw(dstrect=pygame.FRect(-ox * scale, -oy * scale,
                                      texture.width * scale, texture.height * scale))


class _NSRect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double),
                ("w", ctypes.c_double), ("h", ctypes.c_double)]


def raise_above_menu_bar():
    """macOS only: lift pygame window to top and stretch over the whole screen"""
    try:
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]

        def send(obj, selector, restype=None, *args):
            argtypes = [type(a) if isinstance(a, ctypes.Structure) else ctypes.c_long for a in args]
            fn = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(("objc_msgSend", objc))
            return fn(obj, objc.sel_registerName(selector), *args)

        app = send(objc.objc_getClass(b"NSApplication"), b"sharedApplication", ctypes.c_void_p)
        window = send(send(app, b"windows", ctypes.c_void_p), b"firstObject", ctypes.c_void_p)

        nsscreen = send(window, b"screen", ctypes.c_void_p)
        frame = send(nsscreen, b"frame", _NSRect)
        send(window, b"setLevel:", None, 25)  # NSStatusWindowLevel, above the menu bar
        send(window, b"setFrame:display:", None, frame, 1)
    except Exception as e:
        print(f"Could not cover the menu bar: {e}", file=sys.stderr)


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


CONTROL_EVENT = pygame.USEREVENT + 1


def read_control_commands():
    """Forward commands from stdin (one per line, e.g. "next", "delay 8") to the
    event loop. Used by gui.py, which runs the slideshow as a subprocess."""
    for line in sys.stdin:
        parts = line.split()
        if parts:
            pygame.event.post(pygame.event.Event(CONTROL_EVENT, command=parts[0], args=parts[1:]))
    # stdin closed: the GUI is gone, so stop too
    pygame.event.post(pygame.event.Event(CONTROL_EVENT, command="quit", args=[]))


def list_displays():
    pygame.init()
    sizes = pygame.display.get_desktop_sizes()
    print(json.dumps([{"index": i, "width": w, "height": h} for i, (w, h) in enumerate(sizes)]))


def main():
    parser = argparse.ArgumentParser(description="Full-screen photo slideshow")
    parser.add_argument("path", nargs="?", help="Photo file or folder")
    parser.add_argument("--delay", type=float, default=4.0,
                        help="Seconds each slide is shown (default: 7)")
    parser.add_argument("--no-shuffle", action="store_true",
                        help="Show photos in alphabetical order")
    parser.add_argument("--display", type=int, default=0,
                        help="Display index to use (0 = primary, 1 = second screen, etc.)")
    parser.add_argument("--list-displays", action="store_true",
                        help="Print the available displays as JSON and exit")
    parser.add_argument("--control", action="store_true",
                        help="Read commands from stdin and print status as JSON lines (used by gui.py)")
    args = parser.parse_args()

    if args.list_displays:
        list_displays()
        return
    if args.path is None:
        parser.error("the following arguments are required: path")

    photos = collect_photos(args.path)
    if not args.no_shuffle:
        random.shuffle(photos)

    CYAN  = "\033[96m"
    BOLD  = "\033[1m"
    DIM   = "\033[2m"
    RESET = "\033[0m"
    SEP   = f"{DIM}{'─' * 40}{RESET}"

    if not args.control:
        print(SEP)
        print(f"{BOLD}{CYAN}  Found {len(photos)} photo(s).{RESET}  {DIM}delay: {args.delay}s · display: {args.display} · Q/Esc to quit{RESET}")
        for i in range(5, -1, -1):
            print(f"\r{CYAN}  Starting slideshow in {i}...{RESET}", end="", flush=True)
            time.sleep(1)
        print(f"\n{SEP}")

    pygame.init()

    num_displays = pygame.display.get_num_displays()
    if args.display >= num_displays:
        print(f"Display {args.display} not found ({num_displays} display(s) available). "
              f"Using display 0.", file=sys.stderr)
        args.display = 0

    # Linear filtering makes sub-pixel pan/zoom positions blend smoothly
    os.environ["SDL_RENDER_SCALE_QUALITY"] = "linear"
    screen_w, screen_h = pygame.display.get_desktop_sizes()[args.display]
    on_display = 0x2FFF0000 | args.display  # SDL_WINDOWPOS_CENTERED_DISPLAY
    window = pygame.Window("Slideshow", size=(screen_w, screen_h),
                           position=(on_display, on_display), borderless=True)
    renderer = Renderer(window, vsync=True)
    renderer.draw_color = (0, 0, 0, 255)
    if sys.platform == "darwin":
        raise_above_menu_bar()
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
        """Return (texture, content_rect, focus_point) for idx, loading synchronously if not cached."""
        with _preload_lock:
            result = _preload_cache.pop(idx, None)
        if result is None:
            result = load_image(idx)
        surface, crect, focus = result
        if surface is None:
            return None, None, None
        texture = Texture.from_surface(renderer, surface)
        texture.blend_mode = 1  # SDL_BLENDMODE_BLEND, so alpha works for the cross-fade
        return texture, crect, focus

    # --- slide stack ---
    # Photos stacked bottom to top, each {"texture", "crect", "kb", "start", "shown"}.
    # The top one is the current photo. Each fades in over the ones below it, which
    # keep animating, so skipping quickly layers fades instead of cutting them off.
    # "shown" is real time and drives the fade, so skipping still fades while paused.
    # "start" is on the slide clock, which stops while paused, and drives Ken Burns
    # and auto-advance.

    index = 0
    slides = []
    paused_at = None      # real ticks when paused, None while running
    paused_total = 0      # ms spent paused, so the slide clock skips them

    def slide_clock():
        if paused_at is not None:
            return paused_at - paused_total
        return pygame.time.get_ticks() - paused_total

    def report_status():
        if args.control:
            print(json.dumps({"index": index, "total": len(photos), "paused": paused_at is not None,
                              "delay": delay_ms / 1000, "photo": os.path.basename(photos[index])}),
                  flush=True)

    def push_slide(idx):
        texture, crect, focus = get_image(idx)
        slides.append({"texture": texture, "crect": crect, "kb": new_kb_params(focus),
                       "start": slide_clock(), "shown": pygame.time.get_ticks()})

    def advance(step):
        nonlocal index
        index = (index + step) % len(photos)
        push_slide(index)
        if step > 0:
            preload((index + 1) % len(photos))
        report_status()

    def set_paused(paused):
        nonlocal paused_at, paused_total
        if paused and paused_at is None:
            paused_at = pygame.time.get_ticks()
        elif not paused and paused_at is not None:
            paused_total += pygame.time.get_ticks() - paused_at
            paused_at = None
        report_status()

    def set_delay(seconds):
        nonlocal delay_ms
        new_ms = int(max(1.0, seconds) * 1000)
        # Keep each photo's Ken Burns position where it is, only change its pace
        now = slide_clock()
        for slide in slides:
            slide["start"] = now - (now - slide["start"]) * new_ms / delay_ms
        delay_ms = new_ms
        report_status()

    push_slide(index)
    slides[0]["shown"] -= trans_ms  # first photo appears without fading in
    preload((index + 1) % len(photos))
    report_status()

    if args.control:
        threading.Thread(target=read_control_commands, daemon=True).start()

    running = True

    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_ESCAPE, pygame.K_q):
                    running = False
                elif event.key in (pygame.K_RIGHT, pygame.K_SPACE):
                    advance(1)
                elif event.key == pygame.K_LEFT:
                    advance(-1)
                elif event.key == pygame.K_p:
                    set_paused(paused_at is None)
            elif event.type == CONTROL_EVENT:
                if event.command == "quit":
                    running = False
                elif event.command == "next":
                    advance(1)
                elif event.command == "prev":
                    advance(-1)
                elif event.command == "pause":
                    set_paused(True)
                elif event.command == "resume":
                    set_paused(False)
                elif event.command == "delay" and event.args:
                    try:
                        set_delay(float(event.args[0]))
                    except ValueError:
                        pass

        # Read the clock after handling keys: a manual skip loads the next photo
        # synchronously and sets its start, which must not be later than now
        now = pygame.time.get_ticks()
        slide_now = slide_clock()

        # Drop photos hidden under one that has fully faded in
        for i in range(len(slides) - 1, 0, -1):
            if slides[i]["texture"] is not None and now - slides[i]["shown"] >= trans_ms:
                del slides[:i]
                break

        renderer.clear()
        for slide in slides:
            if slide["texture"] is None:
                continue
            alpha = min(255, int(255 * (now - slide["shown"]) / trans_ms))
            # Ken Burns t keeps running from the start of the fade-in, so motion is continuous
            t = min(1.0, (slide_now - slide["start"]) / delay_ms)
            draw_kb_frame(slide["texture"], screen_w, screen_h, t, slide["kb"], slide["crect"], alpha)
        renderer.present()

        # Auto-advance
        if slide_now - slides[-1]["start"] >= delay_ms:
            advance(1)

    pygame.quit()


if __name__ == "__main__":
    main()
