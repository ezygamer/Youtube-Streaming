"""Tiny static HTTP server for single-page web games.

SumLink uses client-side routes (/english, /hindi, ...), so it cannot be opened
from file:// and needs every unknown path to fall back to index.html.
The game files are served read-only and never modified.

Extra directories can be mounted under a URL prefix (e.g. the stream overlay
at /__stream__/), which puts them on the same origin as the game so the
overlay page can host the game in an iframe.
"""
import functools
import logging
import posixpath
import threading
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("streamer.server")


class _SpaHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, mounts: dict[str, Path] | None = None, **kwargs):
        self.mounts = mounts or {}
        super().__init__(*args, **kwargs)

    def _mount_for(self, url_path: str) -> tuple[str, Path] | None:
        path = urllib.parse.urlsplit(url_path).path
        for prefix, directory in self.mounts.items():
            if path == prefix or path.startswith(prefix + "/"):
                return prefix, directory
        return None

    def translate_path(self, path):
        mount = self._mount_for(path)
        if not mount:
            return super().translate_path(path)
        prefix, directory = mount
        rel = posixpath.normpath(urllib.parse.unquote(urllib.parse.urlsplit(path).path[len(prefix):]))
        target = (directory / rel.lstrip("/")).resolve()
        # Never serve files outside the mounted directory.
        return str(target) if target.is_relative_to(directory) else str(directory / "__forbidden__")

    def send_head(self):
        # Any game path that is not a real file is served as index.html (SPA fallback).
        # Mounted directories get normal 404s.
        path = Path(self.translate_path(self.path))
        if not path.is_file() and not self._mount_for(self.path):
            self.path = "/index.html"
        return super().send_head()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)


class GameServer:
    """Serves a game directory on 127.0.0.1 in a background thread."""

    def __init__(self, root: str | Path, port: int = 0, host: str = "127.0.0.1",
                 mounts: dict[str, str | Path] | None = None):
        self.root = Path(root).resolve()
        self.mounts = {prefix.rstrip("/"): Path(d).resolve() for prefix, d in (mounts or {}).items()}
        if not (self.root / "index.html").is_file():
            raise FileNotFoundError(f"No index.html in {self.root}")
        self.host = host
        self.port = port  # 0 = pick a free port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> "GameServer":
        handler = functools.partial(_SpaHandler, directory=str(self.root), mounts=self.mounts)
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="game-server", daemon=True)
        self._thread.start()
        log.info("Serving %s at %s", self.root, self.base_url)
        return self

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
            log.info("Game server stopped")

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
