#!/usr/bin/env python3
"""
Export an Apple Photos album to a folder for use with slideshow.py.

Usage:
    python3 export.py [--output DIR] [--album NAME]
"""

import argparse
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# File types we count as "a photo arrived" during progress polling.
# Includes RAW/video sidecar types Photos may write alongside originals.
PHOTO_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif",
    ".tiff", ".tif", ".gif", ".bmp", ".webp",
    ".mov", ".mp4", ".dng", ".cr2", ".nef", ".arw",
}

AVG_PHOTO_SIZE_MB = 8  # Conservative estimate for originals


# ---------------------------------------------------------------------------
# AppleScript helpers
# ---------------------------------------------------------------------------

def run_applescript(script, timeout=30):
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"osascript exited {result.returncode}")
    return result.stdout.strip()


def check_photos_accessible():
    try:
        run_applescript('tell application "Photos" to get name', timeout=10)
        return True
    except Exception:
        return False


def get_albums():
    """Return sorted list of (name, count) for every album, in one AppleScript call."""
    script = '''tell application "Photos"
    set output to ""
    repeat with a in every album
        set output to output & (name of a) & "|" & (count of media items of a) & linefeed
    end repeat
    return output
end tell'''
    raw = run_applescript(script, timeout=120)
    albums = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # rsplit so album names containing "|" still parse correctly
        parts = line.rsplit("|", 1)
        if len(parts) == 2:
            name = parts[0]
            try:
                count = int(parts[1])
            except ValueError:
                count = 0
            albums.append((name, count))
    return sorted(albums, key=lambda x: x[0].lower())


def run_export(album_name, output_dir):
    """
    Kick off Photos export in a subprocess (blocks until Photos finishes).
    Returns (success: bool, error_message: str).
    """
    safe = _escape(album_name)
    safe_posix = _escape(str(output_dir))
    script = f'''tell application "Photos"
    set theAlbum to album "{safe}"
    set theItems to media items of theAlbum
    export theItems to POSIX file "{safe_posix}" using originals true
end tell'''
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return result.returncode == 0, result.stderr.strip()


def _escape(name):
    """Escape a string for use inside AppleScript double-quoted string."""
    return name.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
SEP    = f"{DIM}{'─' * 44}{RESET}"


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def fmt_size(n_bytes):
    if n_bytes >= 1024 ** 3:
        return f"{n_bytes / 1024 ** 3:.1f} GB"
    return f"{n_bytes / 1024 ** 2:.0f} MB"


def count_photo_files(directory):
    try:
        return sum(
            1 for f in Path(directory).rglob("*")
            if f.is_file() and f.suffix.lower() in PHOTO_EXTENSIONS
        )
    except Exception:
        return 0


def show_progress(output_dir, total, done_event):
    """
    Print a live progress bar by polling output_dir until done_event fires.
    Redraws on the same line using \\r.
    """
    bar_width = 30
    start = time.time()

    while not done_event.is_set():
        _redraw(output_dir, total, bar_width, start)
        time.sleep(0.5)

    # Final redraw after export thread finishes
    _redraw(output_dir, total, bar_width, start, final=True)
    print()  # newline after the bar


def _redraw(output_dir, total, bar_width, start, final=False):
    count = count_photo_files(output_dir)
    fraction = min(1.0, count / total) if total > 0 else 0
    filled = int(bar_width * fraction)
    bar = f"{GREEN}{'█' * filled}{DIM}{'░' * (bar_width - filled)}{RESET}"
    elapsed = time.time() - start
    suffix = f"  {GREEN}{BOLD}done ✓{RESET}" if final else "      "
    print(f"\r  {bar}  {CYAN}{count}/{total}{RESET}  {DIM}({elapsed:.0f}s){RESET}{suffix}", end="", flush=True)


def pick_album_interactive(albums):
    max_name = max(len(name) for name, _ in albums)
    col = max(max_name, 5)  # at least wide enough for "Album"
    print(f"\n{SEP}")
    print(f"  {BOLD}{CYAN}{'#':>3}  {'Album':<{col}}  {'Photos':>6}  Est. size{RESET}  {DIM}(~{AVG_PHOTO_SIZE_MB} MB/photo){RESET}")
    print(f"{SEP}")
    for i, (name, count) in enumerate(albums, 1):
        est = fmt_size(count * AVG_PHOTO_SIZE_MB * 1024 * 1024)
        num = f"{DIM}{i:>3}.{RESET}"
        print(f"  {num}  {name:<{col}}  {CYAN}{count:>6}{RESET}  {DIM}~{est}{RESET}")
    print(f"{SEP}\n")

    while True:
        try:
            raw = input(f"  {BOLD}Enter album number:{RESET} ").strip()
            idx = int(raw) - 1
            if 0 <= idx < len(albums):
                return albums[idx]
            print(f"  Please enter a number between 1 and {len(albums)}.")
        except ValueError:
            print("  Please enter a valid number.")
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            sys.exit(130)


def confirm(prompt, default_no=True):
    hint = "[y/N]" if default_no else "[Y/n]"
    try:
        answer = input(f"{prompt} {hint} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        sys.exit(130)
    if default_no:
        return answer == "y"
    return answer != "n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Export an Apple Photos album to a folder")
    parser.add_argument(
        "--output",
        default=str(Path.home() / "Pictures" / "slideshow-photos"),
        help="Destination folder (default: ~/Pictures/slideshow-photos)",
    )
    parser.add_argument("--album", help="Album name — skips interactive picker")
    args = parser.parse_args()

    output_dir = Path(args.output).expanduser().resolve()

    # ------------------------------------------------------------------
    # 1. Connect to Photos
    # ------------------------------------------------------------------
    print(f"\n{SEP}")
    print(f"  {CYAN}Connecting to Photos...{RESET}", end="", flush=True)
    if not check_photos_accessible():
        print(f" {RED}failed.{RESET}")
        print(f"{RED}Error: Could not reach Photos. Make sure Photos is installed and not blocked.{RESET}", file=sys.stderr)
        sys.exit(1)
    print(f" {GREEN}ok ✓{RESET}")

    # ------------------------------------------------------------------
    # 2. Fetch album list
    # ------------------------------------------------------------------
    albums_result = [None]
    albums_error = [None]

    def _fetch():
        try:
            albums_result[0] = get_albums()
        except Exception as e:
            albums_error[0] = e

    fetch_thread = threading.Thread(target=_fetch, daemon=True)
    fetch_thread.start()

    spinner = ["|", "/", "-", "\\"]
    i = 0
    while fetch_thread.is_alive():
        print(f"\r  {CYAN}Fetching albums...{RESET} {spinner[i % len(spinner)]}", end="", flush=True)
        i += 1
        time.sleep(0.15)

    fetch_thread.join()

    if albums_error[0]:
        print(f"\r  {CYAN}Fetching albums...{RESET} {RED}failed.{RESET}          ")
        print(f"{RED}Error: {albums_error[0]}{RESET}", file=sys.stderr)
        sys.exit(1)

    albums = albums_result[0]
    if not albums:
        print(f"\r  {CYAN}Fetching albums...{RESET} {YELLOW}none found.{RESET}      ")
        print("No albums found in Photos.", file=sys.stderr)
        sys.exit(1)

    print(f"\r  {CYAN}Fetching albums...{RESET} {GREEN}{len(albums)} found ✓{RESET}      ")

    # ------------------------------------------------------------------
    # 3. Pick album
    # ------------------------------------------------------------------
    if args.album:
        match = next(((n, c) for n, c in albums if n == args.album), None)
        if match is None:
            print(f"Error: album '{args.album}' not found.", file=sys.stderr)
            sys.exit(1)
        album_name, count = match
    else:
        album_name, count = pick_album_interactive(albums)

    # ------------------------------------------------------------------
    # 4. Confirm selection and disk estimate
    # ------------------------------------------------------------------
    print(f"\n{SEP}")
    print(f"  {BOLD}Album:{RESET}   {CYAN}{album_name}{RESET}")
    print(f"  {BOLD}Photos:{RESET}  {CYAN}{count}{RESET}")
    estimated_bytes = count * AVG_PHOTO_SIZE_MB * 1024 * 1024
    print(f"  {BOLD}Est. size:{RESET}  {DIM}~{fmt_size(estimated_bytes)} (originals, rough estimate){RESET}")

    # ------------------------------------------------------------------
    # 5. Disk space check
    # ------------------------------------------------------------------
    check_path = output_dir.parent if not output_dir.exists() else output_dir
    try:
        free = shutil.disk_usage(check_path).free
        print(f"  {BOLD}Free disk:{RESET}  {fmt_size(free)}")
        if free < estimated_bytes * 1.2:
            print(f"\n  {YELLOW}Warning: disk space may be tight "
                  f"(need ~{fmt_size(estimated_bytes)}, have {fmt_size(free)}).{RESET}")
            if not confirm("  Continue anyway?"):
                print("Cancelled.")
                sys.exit(130)
    except Exception as e:
        print(f"  {DIM}(Could not check disk space: {e}){RESET}")

    # ------------------------------------------------------------------
    # 6. Prepare output directory
    # ------------------------------------------------------------------
    if output_dir.exists():
        existing = count_photo_files(output_dir)
        if existing > 0:
            print(f"\n  {YELLOW}Output folder already has {existing} photo(s):{RESET}\n    {DIM}{output_dir}{RESET}")
            if not confirm("  Clear it and re-export?"):
                print("Cancelled.")
                sys.exit(130)
            print(f"  {DIM}Deleting: {output_dir}{RESET}")
            shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 7. Export with live progress
    # ------------------------------------------------------------------
    print(f"{SEP}")
    print(f"\n  {BOLD}Exporting to:{RESET} {DIM}{output_dir}{RESET}")
    print(f"  {DIM}(Photos may prompt for permission on first run){RESET}\n")

    done_event = threading.Event()
    export_error = [None]

    def do_export():
        ok, err = run_export(album_name, output_dir)
        if not ok:
            export_error[0] = err
        done_event.set()

    t = threading.Thread(target=do_export, daemon=True)
    t.start()

    show_progress(output_dir, count, done_event)
    t.join()

    if export_error[0]:
        print(f"\n  {RED}Export error: {export_error[0]}{RESET}", file=sys.stderr)
        sys.exit(1)

    # ------------------------------------------------------------------
    # 8. Summary
    # ------------------------------------------------------------------
    final_count = count_photo_files(output_dir)
    print(f"\n{SEP}")
    print(f"  {GREEN}{BOLD}{final_count} photo(s) exported ✓{RESET}")
    print(f"  {DIM}{output_dir}{RESET}")

    if final_count < count:
        print(f"\n  {YELLOW}Note: {count - final_count} photo(s) missing — "
              f"they may be stored only in iCloud and couldn't be downloaded.{RESET}")

    print(f"\n  {DIM}To start the slideshow:{RESET}")
    print(f"  {CYAN}./run.sh {output_dir}{RESET}")
    print(f"{SEP}\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
