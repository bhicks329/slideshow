#!/bin/bash
# Launcher: activates the virtual environment and runs the slideshow.
# Usage: ./run.sh /path/to/photos [--delay 10] [--no-shuffle]

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$DIR/venv/bin/python3" "$DIR/slideshow.py" "$@"
