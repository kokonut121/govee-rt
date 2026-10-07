"""Browser control panel for the effects engine: python -m govee_rt studio ...

Stdlib only. Binds to 127.0.0.1. Writes require Content-Type application/json (so a plain
cross-site form can't post) and the Host header must be local (so DNS rebinding can't reach it).

    GET  /                      the UI (govee_rt/web/index.html)
    GET  /api/catalog           effects + param schemas, palettes, mod sources, segment count, device
    GET  /api/scene             current scene
    PUT  /api/scene             replace the scene (validated, normalized copy returned)
    GET  /api/live              server-sent events: colors, audio features, fps, link stats
    GET  /api/presets           [{name, builtin}]
    GET  /api/presets/<name>    load a preset (does not apply it)
    PUT  /api/presets/<name>    save a scene as a user preset
    DELETE /api/presets/<name>  delete a user preset
    POST /api/strobe            {"on": true|false, "hz": 1..15}: hold-to-strobe; re-send on=true
                                while held, it stops by itself 0.6 s after the last one
"""

import json
import os
import re
import socketserver
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from .effects import catalog, normalize_scene

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
BUILTIN_PRESETS = os.path.join(HERE, "presets")
USER_PRESETS = os.path.join(os.path.expanduser("~"), ".govee-rt", "presets")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _\-]{0,59}$")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


class Presets:
    def __init__(self, user_dir=USER_PRESETS, builtin_dir=BUILTIN_PRESETS):
        self.user_dir = user_dir
        self.builtin_dir = builtin_dir

    def _path(self, d, name):
        if not NAME_RE.match(name or ""):
            raise ValueError("preset names: letters, digits, space, _ and -, up to 60 chars")
        return os.path.join(d, name + ".json")

    def list(self):
        out = {}
        for d, builtin in ((self.builtin_dir, True), (self.user_dir, False)):
            if os.path.isdir(d):
                for fn in sorted(os.listdir(d)):
                    if fn.endswith(".json"):
                        out[fn[:-5]] = {"name": fn[:-5], "builtin": builtin}
        return sorted(out.values(), key=lambda p: (not p["builtin"], p["name"].lower()))

    def load(self, name):
        for d in (self.user_dir, self.builtin_dir):
            p = self._path(d, name)
            if os.path.exists(p):
                with open(p) as f:
                    scene = normalize_scene(json.load(f))
                scene["name"] = name
                return scene
        raise KeyError(name)

    def save(self, name, scene):
        scene = normalize_scene(scene)
        scene["name"] = name
        os.makedirs(self.user_dir, exist_ok=True)
        with open(self._path(self.user_dir, name), "w") as f:
            json.dump(scene, f, indent=1)
        return scene

    def delete(self, name):
        p = self._path(self.user_dir, name)
        if not os.path.exists(p):
            raise KeyError(name)
        os.remove(p)


def make_handler(engine, presets, device=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "govee-rt"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _err(self, code, msg):
            self._send(code, {"error": msg})

        def _host_ok(self):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host not in LOCAL_HOSTS:
                self._err(403, "local access only")
                return False
            return True

        def _json_body(self):
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                raise ValueError("Content-Type must be application/json")
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                raise ValueError("body too large")
            return json.loads(self.rfile.read(n) or b"null")

        def _preset_name(self, path):
            return unquote(path[len("/api/presets/"):])

        def do_GET(self):
            if not self._host_ok():
                return
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/catalog":
                return self._send(200, dict(catalog(), segments=engine.n, presets=presets.list(),
                                            device=device))
            if path == "/api/scene":
                return self._send(200, engine.get_scene())
            if path == "/api/presets":
                return self._send(200, presets.list())
            if path.startswith("/api/presets/"):
                try:
                    return self._send(200, presets.load(self._preset_name(path)))
                except (KeyError, ValueError) as e:
                    return self._err(404, f"no preset {e}")
            if path == "/api/live":
                return self._live()
            self._err(404, "not found")

        def do_PUT(self):
            if not self._host_ok():
                return
            path = urlparse(self.path).path
            try:
                body = self._json_body()
                if path == "/api/scene":
                    return self._send(200, engine.set_scene(body))
                if path == "/api/strobe" and self.command == "POST":
                    body = body or {}
                    engine.strobe(bool(body.get("on")), hz=body.get("hz"))
                    return self._send(200, {"strobe": engine.strobing(), "hz": engine.strobe_hz})
                if path.startswith("/api/presets/"):
                    scene = presets.save(self._preset_name(path), body)
                    return self._send(200, {"saved": scene["name"], "presets": presets.list()})
            except (ValueError, TypeError, KeyError) as e:
                return self._err(400, str(e))
            self._err(404, "not found")

        do_POST = do_PUT

        def do_DELETE(self):
            if not self._host_ok():
                return
            path = urlparse(self.path).path
            if path.startswith("/api/presets/"):
                try:
                    presets.delete(self._preset_name(path))
                    return self._send(200, {"presets": presets.list()})
                except (KeyError, ValueError) as e:
                    return self._err(404, f"no user preset {e}")
            self._err(404, "not found")

        def _live(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while not engine._stop.is_set():
                    self.wfile.write(b"data: " + json.dumps(engine.live()).encode() + b"\n\n")
                    self.wfile.flush()
                    time.sleep(1 / 30)
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind() reverse-resolves the address with getfqdn(), which can hang
        # for tens of seconds on networks without reverse DNS. We know the name already.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def serve(engine, presets=None, port=8765, device=None):
    """Create the HTTP server (caller runs serve_forever). device: the saved profile, for display."""
    return _Server(("127.0.0.1", port), make_handler(engine, presets or Presets(), device))
