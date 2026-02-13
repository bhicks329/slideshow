#!/bin/bash
# Launcher: activates the virtual environment and runs the slideshow.
#
# Usage:
#   ./run.sh /path/to/photos [slideshow options]   # run only (existing behaviour)
#   ./run.sh --export                              # export from Photos, then stop
#   ./run.sh --export-and-run [slideshow options]  # export from Photos, then run
#
# Options applying to export modes:
#   --output DIR   Destination folder (default: ~/Pictures/slideshow-photos)
#
# Slideshow options (passed through to slideshow.py):
#   --delay N      Seconds per slide (default: 7)
#   --no-shuffle   Alphabetical order instead of random
#   --display N    Display index (0 = primary, 1 = second screen, etc.)

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MODE=""
EXPORT_DIR="$HOME/Pictures/slideshow-photos"
SLIDESHOW_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --export)
            MODE="export"
            shift
            ;;
        --export-and-run)
            MODE="export-and-run"
            shift
            ;;
        --output)
            EXPORT_DIR="$2"
            shift 2
            ;;
        *)
            SLIDESHOW_ARGS+=("$1")
            shift
            ;;
    esac
done

case "$MODE" in
    export)
        "$DIR/venv/bin/python3" "$DIR/export.py" --output "$EXPORT_DIR"
        ;;
    export-and-run)
        "$DIR/venv/bin/python3" "$DIR/export.py" --output "$EXPORT_DIR" || exit 1
        "$DIR/venv/bin/python3" "$DIR/slideshow.py" "$EXPORT_DIR" "${SLIDESHOW_ARGS[@]}"
        ;;
    *)
        "$DIR/venv/bin/python3" "$DIR/slideshow.py" "${SLIDESHOW_ARGS[@]}"
        ;;
esac
