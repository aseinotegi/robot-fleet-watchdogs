"""Small read-only HTTP API (standard library only) for dashboards and health checks.

GET /health                       -> {"status": "ok"}
GET /status                       -> watchdog state per robot, error counters
GET /robots/<id>/battery?hours=24 -> battery history from SQLite
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .battery import BatteryStore

log = logging.getLogger(__name__)


def make_server(host: str, port: int, status: Callable[[], dict], store: BatteryStore | None) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            if parts == ["health"]:
                return self._json(200, {"status": "ok"})
            if parts == ["status"]:
                return self._json(200, status())
            if len(parts) == 3 and parts[0] == "robots" and parts[2] == "battery" and store:
                try:
                    hours = float(parse_qs(url.query).get("hours", ["24"])[0])
                except ValueError:
                    return self._json(400, {"error": "hours must be a number"})
                return self._json(
                    200, {"robot": parts[1], "readings": store.history(parts[1], time.time() - hours * 3600)}
                )
            return self._json(404, {"error": "not found"})

        def _json(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args) -> None:
            log.debug("api: " + fmt, *args)

    return ThreadingHTTPServer((host, port), Handler)


def serve_in_background(server: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, name="api", daemon=True)
    thread.start()
    log.info("API listening on http://%s:%d", *server.server_address[:2])
    return thread
