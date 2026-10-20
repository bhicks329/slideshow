#!/usr/bin/env python3
"""
Browser GUI for the slideshow: pick a folder or export an Apple Photos album,
choose delay and display, then start and control the running slideshow.

Usage:
    ./run.sh --gui

Serves a page on 127.0.0.1 only. Every API call must carry the random session
token from the URL, so other websites open in the browser can't use it.
"""

import hmac
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import export

ROOT = Path(__file__).resolve().parent
PAGE = ROOT / "gui" / "index.html"
SETTINGS_FILE = Path.home() / "Library" / "Application Support" / "slideshow" / "settings.json"
DEFAULT_FOLDER = str(Path.home() / "Pictures" / "slideshow-photos")

DEFAULT_SETTINGS = {
    "source": "folder",          # "folder" or "photos"
    "folder": DEFAULT_FOLDER,
    "album": "",
    "export_output": DEFAULT_FOLDER,
    "delay": 7.0,
    "display": 0,
    "shuffle": True,
}

CONTROL_ACTIONS = {"next", "prev", "pause", "resume", "delay", "quit"}
MAX_BODY = 16 * 1024


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        saved = json.loads(SETTINGS_FILE.read_text())
        settings.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    return settings


def save_settings(settings):
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
    except OSError as e:
        print(f"Could not save settings: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# App state shared between request threads
# ---------------------------------------------------------------------------

class App:
    def __init__(self):
        self.lock = threading.Lock()
        self.settings = load_settings()
        self.displays = list_displays()
        self.slideshow = None        # subprocess.Popen while running
        self.status = None           # last status line from the slideshow
        self.export = {"running": False, "album": None, "total": 0, "done": 0,
                       "error": None, "output": None}

    # --- slideshow subprocess ---

    def start_slideshow(self, path, delay, display, shuffle):
        self.stop_slideshow()
        cmd = [sys.executable, str(ROOT / "slideshow.py"), path,
               "--delay", str(delay), "--display", str(display), "--control"]
        if not shuffle:
            cmd.append("--no-shuffle")
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                env={**os.environ, "PYGAME_HIDE_SUPPORT_PROMPT": "1"})
        with self.lock:
            self.slideshow = proc
            self.status = None
        threading.Thread(target=self._read_status, args=(proc,), daemon=True).start()

    def _read_status(self, proc):
        for line in proc.stdout:
            if line.startswith("{"):
                try:
                    status = json.loads(line)
                except ValueError:
                    continue
                with self.lock:
                    if self.slideshow is proc:
                        self.status = status
        proc.wait()
        with self.lock:
            if self.slideshow is proc:
                self.slideshow = None
                self.status = None

    def send_command(self, line):
        with self.lock:
            proc = self.slideshow
        if proc is None:
            return False
        try:
            proc.stdin.write(line + "\n")
            proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            return False

    def stop_slideshow(self):
        with self.lock:
            proc = self.slideshow
        if proc is None:
            return
        self.send_command("quit")
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()

    # --- Photos export ---

    def start_export(self, album, total, output_dir):
        with self.lock:
            self.export = {"running": True, "album": album, "total": total, "done": 0,
                           "error": None, "output": str(output_dir)}

        def run():
            ok, err = export.run_export(album, output_dir)
            with self.lock:
                self.export["running"] = False
                self.export["done"] = export.count_photo_files(output_dir)
                if not ok:
                    self.export["error"] = err or "Export fehlgeschlagen"

        threading.Thread(target=run, daemon=True).start()

    def state(self):
        with self.lock:
            running = self.slideshow is not None
            exp = dict(self.export)
            status = self.status
            settings = dict(self.settings)
        if exp["running"] and exp["output"]:
            exp["done"] = export.count_photo_files(exp["output"])
        return {"settings": settings, "displays": self.displays,
                "slideshow": {"running": running, "status": status}, "export": exp}


def list_displays():
    try:
        out = subprocess.run([sys.executable, str(ROOT / "slideshow.py"), "--list-displays"],
                             capture_output=True, text=True, timeout=30,
                             env={**os.environ, "PYGAME_HIDE_SUPPORT_PROMPT": "1"}).stdout
        return json.loads(out.strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return [{"index": 0, "width": 0, "height": 0}]


def choose_folder(start):
    """Show the native macOS folder picker in front of the browser."""
    default = ""
    if start and Path(start).expanduser().is_dir():
        default = f' default location (POSIX file "{export._escape(str(Path(start).expanduser()))}")'
    script = ('tell application (path to frontmost application as text)\n'
              f'    POSIX path of (choose folder with prompt "Fotoordner wählen"{default})\n'
              'end tell')
    try:
        return export.run_applescript(script, timeout=600).rstrip("/") or "/"
    except Exception:
        return None  # cancelled


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def make_handler(app, token, port, server_ref):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, content_type="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(data)

        def _host_ok(self):
            # Blocks DNS rebinding: a foreign hostname resolving to 127.0.0.1
            return self.headers.get("Host") in allowed_hosts

        def _authorized(self):
            if not self._host_ok():
                return False
            given = self.headers.get("X-Token", "")
            return hmac.compare_digest(given.encode(), token.encode())

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("body too large")
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw or b"{}")
            if not isinstance(data, dict):
                raise ValueError("expected an object")
            return data

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"error": "forbidden"})
            if self.path in ("/", "/index.html"):
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if not self.path.startswith("/api/"):
                return self._send(404, {"error": "not found"})
            if not self._authorized():
                return self._send(403, {"error": "forbidden"})
            if self.path == "/api/state":
                return self._send(200, app.state())
            if self.path == "/api/albums":
                if not export.check_photos_accessible():
                    return self._send(502, {"error": "Fotos ist nicht erreichbar. Bitte den Zugriff "
                                                     "in den Systemeinstellungen erlauben."})
                try:
                    albums = export.get_albums()
                except Exception as e:
                    return self._send(502, {"error": str(e)})
                return self._send(200, {"albums": [{"name": n, "count": c} for n, c in albums],
                                        "mb_per_photo": export.AVG_PHOTO_SIZE_MB})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self.path.startswith("/api/") or not self._authorized():
                return self._send(403, {"error": "forbidden"})
            try:
                body = self._body()
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            handler = {
                "/api/settings": self.post_settings,
                "/api/choose-folder": self.post_choose_folder,
                "/api/export": self.post_export,
                "/api/start": self.post_start,
                "/api/control": self.post_control,
                "/api/quit": self.post_quit,
            }.get(self.path)
            if handler is None:
                return self._send(404, {"error": "not found"})
            try:
                return handler(body)
            except (ValueError, TypeError, KeyError) as e:
                return self._send(400, {"error": f"Ungültige Eingabe: {e}"})

        # --- endpoints ---

        def post_settings(self, body):
            with app.lock:
                for key, default in DEFAULT_SETTINGS.items():
                    if key in body:
                        app.settings[key] = type(default)(body[key])
                settings = dict(app.settings)
            save_settings(settings)
            return self._send(200, {"settings": settings})

        def post_choose_folder(self, body):
            return self._send(200, {"path": choose_folder(body.get("start"))})

        def post_export(self, body):
            album = str(body["album"])
            output_dir = Path(str(body["output"])).expanduser().resolve()
            with app.lock:
                if app.export["running"]:
                    return self._send(409, {"error": "Es läuft bereits ein Export."})
            albums = dict(export.get_albums())
            if album not in albums:
                return self._send(404, {"error": f"Album „{album}“ nicht gefunden."})
            if output_dir.exists():
                existing = export.count_photo_files(output_dir)
                if existing and not body.get("clear"):
                    return self._send(409, {"needs_confirm": True, "existing": existing,
                                            "output": str(output_dir)})
                if existing:
                    if not export.is_safe_to_clear(output_dir):
                        return self._send(400, {"error": "Der Ordner enthält auch andere Dateien oder "
                                                         "Unterordner und wird nicht gelöscht. Bitte "
                                                         "einen leeren Ordner wählen."})
                    shutil.rmtree(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            with app.lock:
                app.settings.update(album=album, export_output=str(output_dir))
                settings = dict(app.settings)
            save_settings(settings)
            app.start_export(album, albums[album], output_dir)
            return self._send(200, {"ok": True})

        def post_start(self, body):
            path = Path(str(body["path"])).expanduser()
            if not path.exists():
                return self._send(400, {"error": f"„{path}“ existiert nicht."})
            delay = min(60.0, max(1.0, float(body["delay"])))
            display = int(body["display"])
            if not 0 <= display < len(app.displays):
                display = 0
            shuffle = bool(body.get("shuffle", True))
            with app.lock:
                app.settings.update(delay=delay, display=display, shuffle=shuffle)
                if body.get("source") in ("folder", "photos"):
                    app.settings["source"] = body["source"]
                if app.settings["source"] == "folder":
                    app.settings["folder"] = str(path)
                settings = dict(app.settings)
            save_settings(settings)
            app.start_slideshow(str(path), delay, display, shuffle)
            return self._send(200, {"ok": True})

        def post_control(self, body):
            action = str(body["action"])
            if action not in CONTROL_ACTIONS:
                return self._send(400, {"error": "unknown action"})
            line = action
            if action == "delay":
                delay = min(60.0, max(1.0, float(body["value"])))
                line = f"delay {delay}"
                with app.lock:
                    app.settings["delay"] = delay
                    settings = dict(app.settings)
                save_settings(settings)
            if not app.send_command(line):
                return self._send(409, {"error": "Die Slideshow läuft nicht."})
            return self._send(200, {"ok": True})

        def post_quit(self, body):
            self._send(200, {"ok": True})
            app.stop_slideshow()
            threading.Thread(target=server_ref[0].shutdown, daemon=True).start()

    return Handler


def main():
    app = App()
    token = secrets.token_urlsafe(24)
    server_ref = [None]
    server = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = server.server_address[1]
    server.RequestHandlerClass = make_handler(app, token, port, server_ref)
    server_ref[0] = server

    # Token in the fragment: never sent in requests or Referer headers
    url = f"http://127.0.0.1:{port}/#{token}"
    print(f"\n  Slideshow GUI: {url}")
    print("  (Nur auf diesem Mac erreichbar. Beenden mit Ctrl+C oder in der Oberfläche.)\n", flush=True)
    webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop_slideshow()
        server.server_close()


if __name__ == "__main__":
    main()
