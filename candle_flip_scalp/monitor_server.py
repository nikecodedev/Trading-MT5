from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Tuple

try:
    from .telemetry import BotTelemetry
except ImportError:  # pragma: no cover - script execution
    from telemetry import BotTelemetry

try:
    from importlib import resources
except ImportError:  # pragma: no cover - python < 3.9
    import importlib_resources as resources  # type: ignore


def _load_monitor_html() -> str:
    try:
        package_files = resources.files("candle_flip_scalp.web")
        with package_files.joinpath("monitor.html").open("r", encoding="utf-8") as fp:
            return fp.read()
    except (FileNotFoundError, ModuleNotFoundError):
        fallback_path = Path(__file__).resolve().parent / "web" / "monitor.html"
        with fallback_path.open("r", encoding="utf-8") as fp:
            return fp.read()


MONITOR_HTML = _load_monitor_html()


class MonitorHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: Tuple[str, int], telemetry: BotTelemetry) -> None:
        super().__init__(server_address, MonitorRequestHandler)
        self.telemetry = telemetry

    def handle_error(self, request, client_address):
        """Suppress harmless connection errors from rapid browser polling."""
        import sys
        exc_type = sys.exc_info()[0]
        # Suppress connection errors that occur when browser closes connection
        if exc_type in (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            return
        # For other errors, use default behavior
        super().handle_error(request, client_address)


class MonitorRequestHandler(BaseHTTPRequestHandler):
    server: MonitorHTTPServer  # type: ignore[assignment]

    def _send_bytes(self, content: bytes, content_type: str, cache: bool = False) -> None:
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)

            # Cache control based on content type
            if cache:
                # Cache static content for 1 hour
                self.send_header("Cache-Control", "public, max-age=3600")
            else:
                # No cache for dynamic content (telemetry)
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")

            # CORS headers for local development
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

            # Content Security Policy for AnyChart CDN
            csp = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' https://cdn.anychart.com https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: https://static.anychart.com http://static.anychart.com; "
                "font-src 'self' data:; "
                "connect-src 'self' https://cdn.jsdelivr.net;"
            )
            self.send_header("Content-Security-Policy", csp)

            # Additional security headers
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")

            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # Client closed connection before response was sent - harmless
            pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html", "/monitor.html"):
            # Cache HTML for 1 hour (static content)
            self._send_bytes(MONITOR_HTML.encode("utf-8"), "text/html; charset=utf-8", cache=True)
            return
        if self.path == "/telemetry":
            # No cache for telemetry (dynamic real-time data)
            payload = json.dumps(self.server.telemetry.snapshot()).encode("utf-8")
            self._send_bytes(payload, "application/json; charset=utf-8", cache=False)
            return
        if self.path == "/health":
            # Health check endpoint
            health = {"status": "ok", "service": "candle_flip_scalp_monitor"}
            payload = json.dumps(health).encode("utf-8")
            self._send_bytes(payload, "application/json; charset=utf-8", cache=False)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Resource not found")

    def do_OPTIONS(self) -> None:  # noqa: N802
        """Handle CORS preflight requests."""
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        # Silence default stdout logging to avoid cluttering console
        return


def start_monitor_server(
    telemetry: BotTelemetry, host: str, port: int
) -> ThreadingHTTPServer:
    server = MonitorHTTPServer((host, port), telemetry)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def stop_monitor_server(server: Optional[ThreadingHTTPServer]) -> None:
    if server is None:
        return
    server.shutdown()
    server.server_close()


