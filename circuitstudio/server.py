"""Local HTTP server + static web UI host.

Binds to 127.0.0.1 only. The browser is the canvas; Python owns the symbols,
routing and file I/O so the editor view and the exported SVG can never drift
apart.
"""
from __future__ import annotations

import base64
import binascii
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .document import Project, is_safe_name, list_projects
from .scene import Scene

WEB_DIR = Path(__file__).resolve().parent / "web"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
MAX_BODY = 32 * 1024 * 1024   # a full-page PNG of a large schematic fits in this
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class AppState:
    def __init__(self, projects_dir: Path, name: str):
        self.projects_dir = projects_dir
        self.lock = threading.Lock()
        self.project = Project(projects_dir, name).load()

    def open(self, name: str) -> None:
        self.project = Project(self.projects_dir, name).load()

    def scene_payload(self) -> dict[str, Any]:
        return {
            "version": self.project.version,
            "project": self.project.name,
            "projects": list_projects(self.projects_dir),
            "scene": Scene(self.project).to_dict(),
            "view": self.project.layout.get("view"),
            "showGrid": bool(self.project.layout.get("showGrid", True)),
            "review": self.project.review_state(),
        }


class Handler(BaseHTTPRequestHandler):
    state: AppState  # injected below
    server_version = "CircuitStudio"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # keep the console readable

    # ── helpers ──────────────────────────────────────────────────────────────

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, code: int, msg: str) -> None:
        self._json({"error": msg}, code)

    def _read_body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return origin.startswith(("http://127.0.0.1:", "http://localhost:"))

    # ── GET ──────────────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]

        if path in STATIC:
            filename, ctype = STATIC[path]
            file = WEB_DIR / filename
            if not file.exists():
                self._error(404, "missing asset")
                return
            self._send(200, file.read_bytes(), ctype)
            return

        if path == "/api/state":
            with self.state.lock:
                self.state.project.reload_circuit_if_changed()
                payload = self.state.scene_payload()
            self._json(payload)
            return

        if path == "/api/version":
            with self.state.lock:
                self.state.project.reload_circuit_if_changed()
                payload = {"version": self.state.project.version,
                           "project": self.state.project.name}
            self._json(payload)
            return

        self._error(404, "not found")

    # ── POST ─────────────────────────────────────────────────────────────────

    def do_POST(self) -> None:
        if not self._same_origin():
            self._error(403, "cross-origin request rejected")
            return

        path = self.path.split("?", 1)[0]
        body = self._read_body()

        if path == "/api/layout":
            if not isinstance(body, dict):
                self._error(400, "expected a JSON object")
                return
            with self.state.lock:
                proj = self.state.project
                positions = body.get("positions")
                if isinstance(positions, dict):
                    proj.set_positions(positions)
                if isinstance(body.get("view"), dict):
                    proj.layout["view"] = body["view"]
                if isinstance(body.get("grid"), int) and body["grid"] > 0:
                    proj.layout["grid"] = body["grid"]
                if isinstance(body.get("showGrid"), bool):
                    proj.layout["showGrid"] = body["showGrid"]
                proj.save_layout()
                payload = self.state.scene_payload()
            self._json(payload)
            return

        if path == "/api/restore":
            if not isinstance(body, dict):
                self._error(400, "expected {positions, wires}")
                return
            with self.state.lock:
                proj = self.state.project
                positions = body.get("positions")
                if isinstance(positions, dict):
                    proj.set_positions(positions)
                wires = body.get("wires")
                if isinstance(wires, dict):
                    proj.layout["wires"] = {
                        k: v for k, v in wires.items() if isinstance(v, list)
                    }
                proj.save_layout()
                payload = self.state.scene_payload()
            self._json(payload)
            return

        if path == "/api/note":
            if not isinstance(body, dict) or not isinstance(body.get("id"), str):
                self._error(400, "expected {id, x?, y?, w?, hidden?}")
                return
            with self.state.lock:
                proj = self.state.project
                proj.set_note(body["id"], x=body.get("x"), y=body.get("y"),
                              w=body.get("w"), hidden=body.get("hidden"))
                proj.save_layout()
                payload = self.state.scene_payload()
            self._json(payload)
            return

        if path == "/api/waypoints":
            if not isinstance(body, dict) or not isinstance(body.get("edge"), str):
                self._error(400, "expected {edge, points}")
                return
            points = body.get("points")
            if not isinstance(points, list):
                points = []
            with self.state.lock:
                proj = self.state.project
                proj.set_waypoints(body["edge"], points)
                proj.save_layout()
                payload = self.state.scene_payload()
            self._json(payload)
            return

        if path == "/api/autoarrange":
            with self.state.lock:
                proj = self.state.project
                proj.layout["positions"] = {}
                proj.layout["wires"] = {}  # old guidance points make no sense now
                proj.autoplace()
                proj.save_layout()
                payload = self.state.scene_payload()
            self._json(payload)
            return

        if path == "/api/open":
            name = (body or {}).get("name") if isinstance(body, dict) else None
            if not isinstance(name, str) or not is_safe_name(name):
                self._error(400, "invalid project name")
                return
            with self.state.lock:
                self.state.open(name)
                payload = self.state.scene_payload()
            self._json(payload)
            return

        if path == "/api/export":
            with self.state.lock:
                proj = self.state.project
                svg = Scene(proj).to_svg()
                proj.svg_path.write_text(svg, encoding="utf-8")
                out = str(proj.svg_path)
            self._json({"path": out})
            return

        if path == "/api/svg":
            with self.state.lock:
                svg = Scene(self.state.project).to_svg()
            self._json({"svg": svg})
            return

        if path == "/api/review":
            # The browser hands back a PNG it rasterised from our own SVG. Doing
            # it there keeps the app dependency-free — Python has no way to turn
            # SVG into a bitmap — and it is WYSIWYG by construction.
            png = (body or {}).get("png") if isinstance(body, dict) else None
            data = b""
            if isinstance(png, str):
                try:
                    data = base64.b64decode(png.split(",", 1)[-1], validate=True)
                except (binascii.Error, ValueError):
                    self._error(400, "png is not valid base64")
                    return
                if not data.startswith(PNG_MAGIC):
                    self._error(400, "png payload is not a PNG")
                    return
            with self.state.lock:
                proj = self.state.project
                proj.svg_path.write_text(Scene(proj).to_svg(), encoding="utf-8")
                if data:
                    proj.png_path.write_bytes(data)
                info = proj.mark_reviewed()
                proj.save_layout()
                payload = self.state.scene_payload()
            payload["reviewedAt"] = info["at"]
            self._json(payload)
            return

        self._error(404, "not found")


class _Server(ThreadingHTTPServer):
    # http.server enables SO_REUSEADDR by default. On Windows that lets a second
    # instance bind the same port and silently serve stale data, so switch it off
    # and let the port scan move on to the next free port instead.
    allow_reuse_address = False
    daemon_threads = True


def _pick_port(preferred: int = 8730) -> int:
    import socket
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free port found in range 8730-8749")


def serve(projects_dir: Path, project_name: str,
          open_browser: bool = True, port: int | None = None) -> None:
    Handler.state = AppState(projects_dir, project_name)
    port = port or _pick_port()
    url = f"http://127.0.0.1:{port}/"

    try:
        httpd = _Server(("127.0.0.1", port), Handler)
    except OSError:
        raise SystemExit(
            f"Port {port} is in use — is CircuitStudio already running? "
            f"Use the existing window, or pick another port with --port."
        )
    print(f"CircuitStudio is running at {url}")
    print(f"Projects: {projects_dir}")
    print("Close this window or press Ctrl+C to stop.")

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
