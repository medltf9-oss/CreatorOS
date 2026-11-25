"""Loopback OAuth callback listener.

A provider will only redirect to a URI that is registered **byte for byte**. A backend that
listens on an OS-assigned port therefore cannot receive OAuth codes, so this listener binds one
fixed loopback port (default 47821, `CREATOROS_SOCIAL_CALLBACK_PORT`).

Why a separate stdlib server rather than a route on the main app: the main port is ephemeral for
security reasons (ADR-06), and mixing an unauthenticated browser callback into the token-protected
API surface would widen the attack surface for no benefit. This listener:

* binds 127.0.0.1 only - never 0.0.0.0;
* accepts GET on exactly ``/oauth/callback/<route>`` and answers 404 for everything else;
* never returns a token: it renders a fixed HTML page with a code and a short explanation;
* hands the query to the async engine and waits with a bounded timeout;
* refuses new work when it is not running, so a disabled listener produces a clear error.
"""
from __future__ import annotations

import asyncio
import html
import threading
import time
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ...integrations.social.errors import SocialError

MAX_PARAMS = 12
MAX_VALUE_LENGTH = 4096
CALLBACK_PREFIX = "/oauth/callback/"


class _Handler(BaseHTTPRequestHandler):
    server_version = "CreatorOS-OAuth/1.0"
    sys_version = ""

    def log_message(self, fmt: str, *args) -> None:
        # Silence stdlib request logging: the query string can contain an authorization code.
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith(CALLBACK_PREFIX):
            self._send(404, _page("Not found", "This address only accepts OAuth callbacks."))
            return
        route = parsed.path[len(CALLBACK_PREFIX) :].strip("/")
        if not route or "/" in route:
            self._send(404, _page("Not found", "Unknown callback path."))
            return
        params = _parse(parsed.query)
        listener: CallbackListener = self.server.creatoros_listener  # type: ignore[attr-defined]
        outcome = listener.dispatch(route, params)
        code = outcome.get("code", "UNKNOWN")
        status = 200 if outcome.get("ok") else 400
        body = _page(
            outcome.get("title") or ("Connected" if outcome.get("ok") else "Not connected"),
            outcome.get("message") or "",
            detail=code,
        )
        self._send(status, body)

    def do_POST(self) -> None:
        self._send(405, _page("Method not allowed", "The OAuth callback only accepts GET."))

    def _send(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(payload)


def _parse(query: str) -> dict[str, str]:
    parsed = parse_qs(query, keep_blank_values=True, max_num_fields=MAX_PARAMS)
    out: dict[str, str] = {}
    for key, values in parsed.items():
        if not values:
            continue
        out[key[:64]] = str(values[0])[:MAX_VALUE_LENGTH]
    return out


def _page(title: str, message: str, *, detail: str | None = None) -> str:
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    safe_detail = html.escape(detail or "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{safe_title} - CreatorOS Studio</title>
<style>
:root {{ color-scheme: dark }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center;
  background:#0f1115; color:#e6e8ee;
  font:15px/1.6 "Segoe UI",system-ui,sans-serif }}
.card {{ max-width:520px; padding:32px 36px; border:1px solid #262b36; border-radius:14px;
  background:#151922 }}
h1 {{ margin:0 0 10px; font-size:19px }}
p {{ margin:0; color:#a9b1c1 }}
code {{ display:inline-block; margin-top:18px; padding:4px 8px; border-radius:6px;
  background:#0f1115; border:1px solid #262b36; color:#8b93a5; font-size:12px }}
</style></head>
<body><div class="card"><h1>{safe_title}</h1><p>{safe_message}</p>
{"<code>" + safe_detail + "</code>" if safe_detail else ""}
</div></body></html>"""


class CallbackListener:
    """Owns the loopback socket and bridges a browser GET to the async engine."""

    def __init__(
        self,
        port: int,
        handler: Callable[[str, Mapping[str, str]], dict],
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        timeout_s: float = 60.0,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.requested_port = port
        self.port = port
        self._handler = handler
        self._loop = loop
        self._timeout = timeout_s
        self._logger = logger or (lambda _line: None)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.started_at: float | None = None
        self.requests = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> int:
        if self._server is not None:
            return self.port
        server = ThreadingHTTPServer(("127.0.0.1", self.requested_port), _Handler)
        # port 0 means "let the OS choose": record what was actually bound so callers can use it.
        self.port = int(server.server_address[1])
        server.daemon_threads = True
        server.creatoros_listener = self  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="oauth-callback", daemon=True)
        self._thread.start()
        self.started_at = time.time()
        self._logger(f"oauth callback listener on http://127.0.0.1:{self.port}")
        return self.port

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        self._thread = None
        self._logger("oauth callback listener stopped")

    @property
    def running(self) -> bool:
        return self._server is not None

    def status(self) -> dict:
        return {
            "running": self.running,
            "port": self.port,
            "url_base": f"http://127.0.0.1:{self.port}{CALLBACK_PREFIX}",
            "requests": self.requests,
            "started_at": self.started_at,
        }

    # -- dispatch ----------------------------------------------------------
    def dispatch(self, route: str, params: Mapping[str, str]) -> dict:
        if self._loop is None:
            return {
                "ok": False,
                "code": "CALLBACK_NOT_READY",
                "message": "The backend is not ready to complete this connection yet.",
            }
        self.requests += 1
        try:
            future = asyncio.run_coroutine_threadsafe(self._handler(route, dict(params)), self._loop)
            return future.result(timeout=self._timeout)
        except TimeoutError:
            return {
                "ok": False,
                "code": "CALLBACK_TIMEOUT",
                "message": "The connection took too long to complete. Try connecting again.",
            }
        except SocialError as error:
            return {"ok": False, "code": error.code, "message": error.message}
        except Exception as error:  # noqa: BLE001 - the browser must still get an honest page
            self._logger(f"callback dispatch failed: {error}")
            return {
                "ok": False,
                "code": "CALLBACK_FAILED",
                "message": "The connection could not be completed. The CreatorOS window has the details.",
            }


_listener: CallbackListener | None = None


def get_listener() -> CallbackListener | None:
    return _listener


def set_listener(listener: CallbackListener | None) -> None:
    global _listener
    _listener = listener
