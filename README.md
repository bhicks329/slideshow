# slideshow

A fullscreen photo slideshow for macOS with Ken Burns pan/zoom effects, cross-fade transitions, and optional face-detection-guided panning.

## Background

Apple Photos' built-in slideshow is great, but it doesn't let you control how long each image is displayed — you get a fixed speed with no way to slow it down. This project was built to fill that gap: a simple, configurable slideshow you can point at any folder of photos and run at whatever pace you like.

## Features

- Configurable slide delay (seconds per photo)
- Ken Burns effect — gentle pan and slight zoom on every image
- 1-second cross-fade transition between slides
- Face detection (Haar cascade) — when a face is found, the pan starts centered on it
- Automatic black-border removal (e.g. screenshots with letterboxing)
- EXIF orientation correction
- Background image preloading for smooth transitions
- Second screen / projector support
- Loops indefinitely through the photo library

## Requirements

- macOS (tested on Sequoia)
- Python 3.9+

## Setup

```bash
git clone https://github.com/your-username/slideshow.git
cd slideshow
python3 -m venv venv
source venv/bin/activate
pip install pygame pillow opencv-python-headless numpy
```

## Usage

```bash
./run.sh /path/to/photos
```

Or directly:

```bash
venv/bin/python3 slideshow.py /path/to/photos
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--delay N` | `7` | Seconds each slide is shown |
| `--no-shuffle` | off | Show photos in alphabetical order instead of random |
| `--display N` | `0` | Display index (0 = primary, 1 = second screen, etc.) |

### Examples

```bash
# Slow, relaxed pace
./run.sh ~/Photos --delay 10

# Fast-paced, alphabetical order
./run.sh ~/Photos --delay 3 --no-shuffle

# Send to a projector or second screen
./run.sh ~/Photos --display 1

# Second screen with a custom delay
./run.sh ~/Photos --delay 5 --display 1
```

## Controls

| Key | Action |
|-----|--------|
| Right arrow / Space | Next photo |
| Left arrow | Previous photo |
| Q / Escape | Quit |

## Supported formats

JPEG, PNG, GIF, BMP, TIFF, HEIC, WebP

## Notes

- Start the app **after** connecting an external display — the screen resolution is detected at launch and images are sized to match.
- On macOS you may see a harmless `Class SDLApplication is implemented in both…` warning in the terminal. This is a known conflict between opencv's bundled SDL2 and pygame's copy, and does not affect the slideshow.

## License

MIT
