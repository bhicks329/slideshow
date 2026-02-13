#!/bin/bash
# Launcher: checks the Python environment, then runs the slideshow.
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
#
# Setup options:
#   --skip-setup   Skip Python environment checks and go straight to running

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MODE=""
EXPORT_DIR="$HOME/Pictures/slideshow-photos"
SKIP_SETUP=0
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
        --skip-setup)
            SKIP_SETUP=1
            shift
            ;;
        *)
            SLIDESHOW_ARGS+=("$1")
            shift
            ;;
    esac
done

setup_env() {
    # 1. Check Python 3.9+ is available
    if ! command -v python3 &>/dev/null; then
        echo "Error: python3 not found."
        echo "Install it with Homebrew: brew install python"
        echo "(If you don't have Homebrew: https://brew.sh)"
        exit 1
    fi

    PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
    PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

    if [[ "$PYTHON_MAJOR" -lt 3 ]] || { [[ "$PYTHON_MAJOR" -eq 3 ]] && [[ "$PYTHON_MINOR" -lt 9 ]]; }; then
        echo "Error: Python 3.9 or later is required (found $PYTHON_VERSION)."
        echo "Upgrade with Homebrew: brew install python"
        exit 1
    fi

    # 2. Create venv if missing
    if [[ ! -f "$DIR/venv/bin/python3" ]]; then
        echo "Setting up virtual environment..."
        python3 -m venv "$DIR/venv"
        if [[ $? -ne 0 ]]; then
            echo "Error: failed to create virtual environment."
            exit 1
        fi
        echo "Virtual environment created."
    fi

    # 3. Install any missing dependencies
    MISSING=0
    while IFS= read -r pkg || [[ -n "$pkg" ]]; do
        # Strip version specifiers for the import check
        import_name=$(echo "$pkg" | sed 's/[>=<].*//' | tr '-' '_' | tr '[:upper:]' '[:lower:]')
        # pillow installs as PIL
        if [[ "$import_name" == "pillow" ]]; then import_name="PIL"; fi
        # opencv-python-headless installs as cv2
        if [[ "$import_name" == "opencv_python_headless" ]]; then import_name="cv2"; fi

        if ! "$DIR/venv/bin/python3" -c "import $import_name" &>/dev/null; then
            MISSING=1
            break
        fi
    done < "$DIR/requirements.txt"

    if [[ "$MISSING" -eq 1 ]]; then
        echo "Installing dependencies..."
        "$DIR/venv/bin/pip" install --quiet -r "$DIR/requirements.txt"
        if [[ $? -ne 0 ]]; then
            echo "Error: dependency installation failed."
            exit 1
        fi
        echo "Dependencies installed."
    fi
}

if [[ "$SKIP_SETUP" -eq 0 ]]; then
    setup_env
fi

case "$MODE" in
    export)
        "$DIR/venv/bin/python3" "$DIR/export.py" --output "$EXPORT_DIR"
        ;;
    export-and-run)
        "$DIR/venv/bin/python3" "$DIR/export.py" --output "$EXPORT_DIR" || exit 1
        "$DIR/venv/bin/python3" "$DIR/slideshow.py" "$EXPORT_DIR" "${SLIDESHOW_ARGS[@]}"
        ;;
    *)
        # Default to the standard export folder if no path was given
        if [[ ${#SLIDESHOW_ARGS[@]} -eq 0 ]] || [[ "${SLIDESHOW_ARGS[0]}" == --* ]]; then
            SLIDESHOW_ARGS=("$EXPORT_DIR" "${SLIDESHOW_ARGS[@]}")
        fi
        "$DIR/venv/bin/python3" "$DIR/slideshow.py" "${SLIDESHOW_ARGS[@]}"
        ;;
esac
