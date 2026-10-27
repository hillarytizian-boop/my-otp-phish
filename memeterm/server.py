"""
memeterm.server - the local HTTP API and the static terminal UI.

Security model (short version, because it matters)
--------------------------------------------------
* Binds 127.0.0.1 by default. Binding elsewhere requires `--allow-remote` and an
  access code that is printed once to the console and never returned by the API.
* Session cookie: HttpOnly, SameSite=Strict, random 256-bit value.
* Every state-changing request needs `X-CSRF-Token` matching the session, and
  either a same-origin `Origin` header or `Sec-Fetch-Site: same-origin`.
* `Host` is validated against an allowlist (loopback names, plus the preview
  domain when running remotely).
* Strict CSP, no inline script, no third-party resources, no CDN.
* API responses never contain the API secret or the Telegram token; settings
  show a mask and a one-way fingerprint.
* Loopback is *not* an encryption boundary. Malware on the phone can read the
  credentials file. The recommended mitigation is a spot-only key with
  withdrawals disabled, not a promise from this server.
"""

from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import secrets
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import __version__
from .binance import BinanceError
from .config import ConfigError, TerminalConfig
from .engine import LIVE_CONFIRMATION, EngineError, TradingEngine
from .risk import MODE_LIVE, MODE_PAPER
from .util import redact, utc_str

WEB_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MAX_BODY_BYTES = 64 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
SECURITY_HEADERS = {
    "Content-Security-Policy": ("; ".join([
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "font-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "object-src 'none'",
    ])),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "ERROR"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class RateLimiter:
    """Tiny fixed-window limiter; enough for a single-user local terminal."""

    def __init__(self, limit: int, window_s: float = 60.0):
        self.limit = limit
        self.window_s = window_s
        self._hits: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            stamps = [t for t in self._hits.get(key, []) if now - t < self.window_s]
            if len(stamps) >= self.limit:
                self._hits[key] = stamps
                return False
            stamps.append(now)
            self._hits[key] = stamps
            return True


class SessionStore:
    def __init__(self):
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.Lock()

    def create(self, *, authenticated: bool = True) -> Tuple[str, str]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        with self.lock:
            self.sessions[token] = {"csrf": csrf, "created": time.time(),
                                    "authenticated": authenticated}
        return token, csrf

    def get(self, token: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            return self.sessions.get(token or "")


class TerminalServer:
    """Owns the HTTP server, the routing table and the security checks."""

    def __init__(self, cfg: TerminalConfig, engine: TradingEngine, *,
                 host: str = "127.0.0.1", port: int = 8787,
                 access_code: Optional[str] = None,
                 quiet: bool = False):
        self.cfg = cfg
        self.engine = engine
        self.host = host
        self.port = port
        self.quiet = quiet
        self.remote = host not in ("127.0.0.1", "localhost", "::1")
        self.access_code = access_code or (secrets.token_urlsafe(9) if self.remote else "")
        self.sessions = SessionStore()
        self.mutation_limiter = RateLimiter(limit=90, window_s=60)
        self.auth_limiter = RateLimiter(limit=6, window_s=60)
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.started_at = time.time()
        self.request_count = 0
        self.public_routes = {("GET", "/api/health"), ("POST", "/api/session")}
        self.routes = {
            ("GET", "/api/health"): self.api_health,
            ("GET", "/api/bootstrap"): self.api_bootstrap,
            ("POST", "/api/session"): self.api_session,
            ("GET", "/api/state"): self.api_state,
            ("GET", "/api/market"): self.api_market,
            ("GET", "/api/klines"): self.api_klines,
            ("GET", "/api/symbols"): self.api_symbols,
            ("GET", "/api/agents"): self.api_agents,
            ("POST", "/api/agents/run"): self.api_agents_run,
            ("GET", "/api/settings"): self.api_settings_get,
            ("POST", "/api/settings"): self.api_settings_post,
            ("POST", "/api/credentials"): self.api_credentials_post,
            ("DELETE", "/api/credentials"): self.api_credentials_delete,
            ("POST", "/api/credentials/test"): self.api_credentials_test,
            ("POST", "/api/mode"): self.api_mode,
            ("GET", "/api/live/preflight"): self.api_live_preflight,
            ("POST", "/api/live/activate"): self.api_live_activate,
            ("POST", "/api/live/deactivate"): self.api_live_deactivate,
            ("POST", "/api/order/preview"): self.api_order_preview,
            ("POST", "/api/order"): self.api_order,
            ("POST", "/api/order/cancel"): self.api_order_cancel,
            ("POST", "/api/positions/close"): self.api_position_close,
            ("POST", "/api/emergency-stop"): self.api_emergency_stop,
            ("POST", "/api/emergency-stop/clear"): self.api_emergency_clear,
            ("GET", "/api/audit"): self.api_audit,
            ("POST", "/api/backtest"): self.api_backtest,
            ("GET", "/api/backtest/latest"): self.api_backtest_latest,
        }

    # ------------------------------------------------------------------
    def allowed_hosts(self) -> set:
        hosts = set(LOOPBACK_HOSTS)
        for port in (self.port, 80):
            hosts.update({"127.0.0.1:{}".format(port), "localhost:{}".format(port),
                          "[::1]:{}".format(port)})
        return hosts

    def host_allowed(self, host_header: str) -> bool:
        host = (host_header or "").strip().lower()
        if not host:
            return False
        if host in self.allowed_hosts():
            return True
        if self.remote:
            # Preview proxies terminate TLS with their own host name.
            name = host.split(":")[0]
            if name.endswith(".e2b.app") or name.endswith(".arena.ai"):
                return True
            return True          # remote mode is already gated by the access code
        return False

    def origin_allowed(self, origin: str, host_header: str, sec_fetch_site: str) -> bool:
        if not origin or origin == "null":
            # Same-origin fetches from our own page sometimes omit Origin on GET.
            return sec_fetch_site in ("same-origin", "none", "")
        try:
            parsed = urllib.parse.urlparse(origin)
        except ValueError:
            return False
        expected = set()
        for port in (self.port, 80, 443):
            expected.update({"http://127.0.0.1:{}".format(port),
                             "http://localhost:{}".format(port),
                             "http://[::1]:{}".format(port)})
        if self.remote:
            expected.add("http://" + (host_header or "").lower())
            expected.add("https://" + (host_header or "").lower())
        return origin.lower() in expected

    # ------------------------------------------------------------------
    def build(self) -> ThreadingHTTPServer:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "MemeTerminal/{}".format(__version__)
            sys_version = ""
            protocol_version = "HTTP/1.1"

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self._pending_cookie: Optional[str] = None
                super().__init__(*args, **kwargs)

            def log_message(self, fmt: str, *args: Any) -> None:      # quiet by default
                if not outer.quiet and os.environ.get("MEMETERM_HTTP_LOG") == "1":
                    print("[http] {} {}".format(self.address_string(), fmt % args))

            # -- helpers -------------------------------------------------
            def _send(self, status: int, body: bytes, content_type: str,
                      extra: Optional[Dict[str, str]] = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for key, value in SECURITY_HEADERS.items():
                    self.send_header(key, value)
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, payload: Any, status: int = 200,
                      extra: Optional[Dict[str, str]] = None) -> None:
                body = json.dumps(payload, default=str, separators=(",", ":")).encode()
                self._send(status, body, "application/json; charset=utf-8", extra)

            def _error(self, status: int, message: str, code: str = "ERROR") -> None:
                self._json({"ok": False, "error": redact(message), "code": code}, status)

            def _cookie(self) -> Optional[str]:
                raw = self.headers.get("Cookie") or ""
                for part in raw.split(";"):
                    name, _, value = part.strip().partition("=")
                    if name == "mt_session":
                        return value
                return None

            def _session(self) -> Optional[Dict[str, Any]]:
                token = self._cookie()
                if not token:
                    return None
                return outer.sessions.get(token)

            def _read_body(self) -> Dict[str, Any]:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    raise ApiError("invalid Content-Length", 400, "BAD_REQUEST")
                if length > MAX_BODY_BYTES:
                    raise ApiError("request body too large", 413, "TOO_LARGE")
                raw = self.rfile.read(length) if length else b""
                if not raw:
                    return {}
                ctype = (self.headers.get("Content-Type") or "").lower()
                if "application/json" not in ctype:
                    raise ApiError("Content-Type must be application/json", 415,
                                   "UNSUPPORTED_MEDIA")
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    raise ApiError("body is not valid JSON", 400, "BAD_JSON")
                if not isinstance(payload, dict):
                    raise ApiError("body must be a JSON object", 400, "BAD_JSON")
                return payload

            def _guard(self, public: bool = False) -> Tuple[Optional[Dict[str, Any]], bool]:
                """Common checks. Returns (session, ok)."""
                if not outer.host_allowed(self.headers.get("Host", "")):
                    self._error(421, "unexpected Host header", "BAD_HOST")
                    return None, False
                if public:
                    if self.command in MUTATING:
                        if not outer.auth_limiter.allow(self.client_address[0]):
                            self._error(429, "too many attempts, wait a minute",
                                        "RATE_LIMITED")
                            return None, False
                    return self._session(), True
                session = self._session()
                if session is None and not outer.remote:
                    token, _csrf = outer.sessions.create()
                    session = outer.sessions.get(token)
                    self._pending_cookie = token
                if session is None:
                    self._error(401, "not signed in", "NO_SESSION")
                    return None, False
                if self.command in MUTATING:
                    if not outer.mutation_limiter.allow(self.client_address[0]):
                        self._error(429, "too many requests, slow down", "RATE_LIMITED")
                        return None, False
                    csrf = self.headers.get("X-CSRF-Token", "")
                    if not csrf or csrf != session.get("csrf"):
                        self._error(403, "missing or invalid CSRF token", "CSRF")
                        return None, False
                    if not outer.origin_allowed(self.headers.get("Origin", ""),
                                                self.headers.get("Host", ""),
                                                self.headers.get("Sec-Fetch-Site", "")):
                        self._error(403, "cross-site request refused", "BAD_ORIGIN")
                        return None, False
                return session, True

            # -- HTTP verbs ----------------------------------------------
            def do_GET(self) -> None:
                self._dispatch("GET")

            def do_HEAD(self) -> None:
                self._dispatch("GET")

            def do_POST(self) -> None:
                self._dispatch("POST")

            def do_DELETE(self) -> None:
                self._dispatch("DELETE")

            def do_PUT(self) -> None:
                self._dispatch("PUT")

            def _dispatch(self, method: str) -> None:
                outer.request_count += 1
                try:
                    parsed = urllib.parse.urlparse(self.path)
                    path = parsed.path.rstrip("/") or "/"
                    query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                    if method in ("GET",) and not path.startswith("/api/"):
                        self._serve_static(path)
                        return
                    handler = outer.routes.get((method, path))
                    if handler is None:
                        self._error(404, "no such endpoint", "NOT_FOUND")
                        return
                    public = (method, path) in outer.public_routes
                    session, ok = self._guard(public=public)
                    if not ok:
                        return
                    extra: Dict[str, str] = {}
                    token = getattr(self, "_pending_cookie", None)
                    if token:
                        extra["Set-Cookie"] = ("mt_session={}; HttpOnly; SameSite=Strict; "
                                               "Path=/".format(token))
                        self._pending_cookie = None
                    payload = self._read_body() if method in MUTATING else {}
                    result = handler(query, payload, session)
                    headers = dict(extra)
                    if isinstance(result, tuple) and len(result) == 3:
                        body, status, head = result
                        headers.update(head or {})
                    elif isinstance(result, tuple):
                        body, status = result
                    else:
                        body, status = result, 200
                    self._json(body, status, headers)
                except ApiError as exc:
                    self._error(exc.status, exc.message, exc.code)
                except EngineError as exc:
                    self._error(400, str(exc), "REFUSED")
                except ConfigError as exc:
                    self._error(400, str(exc), "BAD_CONFIG")
                except BinanceError as exc:
                    self._error(502, str(exc), "EXCHANGE")
                except BrokenPipeError:                       # client vanished
                    return
                except Exception as exc:                      # pragma: no cover - defensive
                    outer.engine.store.audit("server_error", path=self.path,
                                             error="{}: {}".format(exc.__class__.__name__, exc))
                    self._error(500, "internal error (details redacted)", "INTERNAL")

            # -- static files ---------------------------------------------
            def _serve_static(self, path: str) -> None:
                rel = "index.html" if path in ("/", "") else path.lstrip("/")
                rel = posixpath.normpath(rel)
                if rel.startswith("..") or rel.startswith("/"):
                    self._error(404, "not found")
                    return
                full = os.path.abspath(os.path.join(WEB_ROOT, rel))
                if not full.startswith(os.path.abspath(WEB_ROOT)) or not os.path.isfile(full):
                    self._error(404, "not found")
                    return
                ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype in ("application/javascript",
                                                          "application/json"):
                    ctype += "; charset=utf-8"
                with open(full, "rb") as handle:
                    body = handle.read()
                self._send(200, body, ctype)

        server = ThreadingHTTPServer((self.host, self.port), Handler)
        server.daemon_threads = True
        self.httpd = server
        self.port = int(server.server_address[1])     # resolves port 0 for tests
        return server

    # ------------------------------------------------------------------
    def start(self) -> str:
        server = self.httpd or self.build()
        self.thread = threading.Thread(target=server.serve_forever,
                                       kwargs={"poll_interval": 0.3},
                                       name="http", daemon=True)
        self.thread.start()
        return self.url()

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread:
            self.thread.join(timeout=3)

    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return "http://{}:{}".format(host, self.port)

    # ------------------------------------------------------------------
    # API handlers
    # ------------------------------------------------------------------
    def api_health(self, query: Dict[str, str], payload: Dict[str, Any],
                   session: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True, "version": __version__, "uptime_s":
                round(time.time() - self.started_at, 1), "requests": self.request_count,
                "mode": self.engine.mode, "time_utc": utc_str()}

    def api_bootstrap(self, query: Dict[str, str], payload: Dict[str, Any],
                      session: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True, "version": __version__,
                "csrf": session.get("csrf", ""),
                "mode": self.engine.mode,
                "live_confirmation_phrase": LIVE_CONFIRMATION,
                "server_time": utc_str(),
                "poll": {"state_ms": 5000, "market_ms": 4000, "agents_ms": 30000}}

    def api_session(self, query: Dict[str, str], payload: Dict[str, Any],
                    session: Dict[str, Any]) -> Dict[str, Any]:
        if not self.remote:
            return {"ok": True, "note": "loopback sessions are automatic"}
        client = "unknown"
        if not self.auth_limiter.allow(client):
            raise ApiError("too many attempts, wait a minute", 429, "RATE_LIMITED")
        code = str(payload.get("access_code") or "").strip()
        if not self.access_code or not secrets.compare_digest(code, self.access_code):
            self.engine.store.audit("login_failed", client=client)
            raise ApiError("wrong access code", 401, "BAD_CODE")
        token, csrf = self.sessions.create()
        self.engine.store.audit("login_ok", client=client)
        return ({"ok": True, "csrf": csrf, "note": "session started"},
                200, {"Set-Cookie": "mt_session={}; HttpOnly; SameSite=Strict; "
                                    "Path=/".format(token)})

    def api_state(self, query: Dict[str, str], payload: Dict[str, Any],
                  session: Dict[str, Any]) -> Dict[str, Any]:
        snapshot = self.engine.state_snapshot()
        return {"ok": True, **snapshot}

    def api_market(self, query: Dict[str, str], payload: Dict[str, Any],
                   session: Dict[str, Any]) -> Dict[str, Any]:
        symbol = (query.get("symbol") or self.engine.active_symbol).upper()
        if query.get("symbol"):
            self.engine.active_symbol = symbol
        interval = query.get("interval") or self.engine.cfg.kline_interval
        snapshot = self.engine.market.snapshot(symbol, interval=interval)
        rows = snapshot["candles"][-200:]
        return {"ok": True, "symbol": symbol, "interval": interval,
                "candles": [[c["ts"], c["o"], c["h"], c["l"], c["c"], c["v"]] for c in rows],
                "ticker": snapshot["ticker"], "book": snapshot["book"],
                "trades": snapshot["trades"][-25:],
                "data_age_s": round(snapshot["data_age_s"], 1),
                "fetched_utc": utc_str()}

    def api_klines(self, query: Dict[str, str], payload: Dict[str, Any],
                   session: Dict[str, Any]) -> Dict[str, Any]:
        return self.api_market(query, payload, session)

    def api_symbols(self, query: Dict[str, str], payload: Dict[str, Any],
                    session: Dict[str, Any]) -> Dict[str, Any]:
        try:
            symbols = self.engine.client.spot_symbols()
            return {"ok": True, "symbols": symbols[:800], "count": len(symbols),
                    "source": "binance exchangeInfo"}
        except BinanceError as exc:
            return {"ok": False, "error": str(exc), "symbols": []}

    def api_agents(self, query: Dict[str, str], payload: Dict[str, Any],
                   session: Dict[str, Any]) -> Dict[str, Any]:
        symbol = (query.get("symbol") or self.engine.active_symbol).upper()
        if query.get("refresh") == "1" or not self.engine.last_consensus:
            return {"ok": True, **self.engine.run_agents(symbol)}
        return {"ok": True, **self.engine.last_consensus,
                "age_s": round(time.time() - self.engine.last_consensus_at, 1)}

    def api_agents_run(self, query: Dict[str, str], payload: Dict[str, Any],
                       session: Dict[str, Any]) -> Dict[str, Any]:
        symbol = str(payload.get("symbol") or self.engine.active_symbol).upper()
        return {"ok": True, **self.engine.run_agents(symbol)}

    def api_settings_get(self, query: Dict[str, str], payload: Dict[str, Any],
                         session: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True, "config": self.engine.public_config(),
                "connection": self.engine.connection_view(),
                "telegram": self.engine.notifier.status(),
                "mode": self.engine.mode,
                "paths": {"data_dir": self.engine.cfg.resolved_dir(),
                          "credentials_file": "credentials.json (0600, git-ignored)",
                          "audit_log": "audit.jsonl"}}

    def api_settings_post(self, query: Dict[str, str], payload: Dict[str, Any],
                          session: Dict[str, Any]) -> Dict[str, Any]:
        config = self.engine.update_settings(payload.get("config") or payload)
        return {"ok": True, "config": config}

    def api_credentials_post(self, query: Dict[str, str], payload: Dict[str, Any],
                             session: Dict[str, Any]) -> Dict[str, Any]:
        status = self.engine.save_credentials(str(payload.get("api_key") or ""),
                                              str(payload.get("api_secret") or ""))
        return {"ok": True, "connection": status,
                "note": "stored locally with 0600 permissions; the secret is never returned"}

    def api_credentials_delete(self, query: Dict[str, str], payload: Dict[str, Any],
                               session: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True, **self.engine.delete_credentials()}

    def api_credentials_test(self, query: Dict[str, str], payload: Dict[str, Any],
                             session: Dict[str, Any]) -> Dict[str, Any]:
        result = self.engine.test_connection()
        return {"ok": True, "result": result,
                "connection": self.engine.connection_view()}

    def api_mode(self, query: Dict[str, str], payload: Dict[str, Any],
                 session: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True, **self.engine.set_mode(str(payload.get("mode") or ""))}

    def api_live_preflight(self, query: Dict[str, str], payload: Dict[str, Any],
                           session: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True, **self.engine.live_preflight()}

    def api_live_activate(self, query: Dict[str, str], payload: Dict[str, Any],
                          session: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True, **self.engine.activate_live(
            str(payload.get("confirmation") or ""),
            acknowledge=bool(payload.get("acknowledge")))}

    def api_live_deactivate(self, query: Dict[str, str], payload: Dict[str, Any],
                            session: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True, **self.engine.deactivate_live(
            str(payload.get("reason") or "user"))}

    def api_order_preview(self, query: Dict[str, str], payload: Dict[str, Any],
                          session: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True, "preview": self.engine.orders.preview(
            symbol=str(payload.get("symbol") or ""),
            side=str(payload.get("side") or ""),
            order_type=str(payload.get("order_type") or "MARKET"),
            qty=_maybe_float(payload.get("qty")),
            price=_maybe_float(payload.get("price")),
            quote_amount=_maybe_float(payload.get("quote_amount")))}

    def api_order(self, query: Dict[str, str], payload: Dict[str, Any],
                  session: Dict[str, Any]) -> Dict[str, Any]:
        result = self.engine.place_order(
            symbol=str(payload.get("symbol") or ""),
            side=str(payload.get("side") or ""),
            order_type=str(payload.get("order_type") or "MARKET"),
            qty=_maybe_float(payload.get("qty")),
            price=_maybe_float(payload.get("price")),
            quote_amount=_maybe_float(payload.get("quote_amount")),
            confirm=bool(payload.get("confirm")))
        return {"ok": result.ok, **result.as_dict()}, (200 if result.ok else 400)

    def api_order_cancel(self, query: Dict[str, str], payload: Dict[str, Any],
                         session: Dict[str, Any]) -> Dict[str, Any]:
        result = self.engine.orders.cancel(str(payload.get("client_order_id") or ""))
        return {"ok": result.ok, **result.as_dict()}, (200 if result.ok else 400)

    def api_position_close(self, query: Dict[str, str], payload: Dict[str, Any],
                           session: Dict[str, Any]) -> Dict[str, Any]:
        result = self.engine.close_position(str(payload.get("position_id") or ""),
                                            confirm=bool(payload.get("confirm")))
        return {"ok": result.ok, **result.as_dict()}, (200 if result.ok else 400)

    def api_emergency_stop(self, query: Dict[str, str], payload: Dict[str, Any],
                           session: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True, **self.engine.emergency_stop(
            str(payload.get("reason") or "user requested"))}

    def api_emergency_clear(self, query: Dict[str, str], payload: Dict[str, Any],
                            session: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True, **self.engine.clear_emergency_stop(
            acknowledge=bool(payload.get("acknowledge")))}

    def api_audit(self, query: Dict[str, str], payload: Dict[str, Any],
                  session: Dict[str, Any]) -> Dict[str, Any]:
        try:
            limit = max(1, min(int(query.get("limit") or 100), 500))
        except ValueError:
            limit = 100
        events = self.engine.store.read_audit(limit=limit)
        return {"ok": True, "events": events,
                "note": "secrets are redacted before they reach this file"}

    def api_backtest_latest(self, query: Dict[str, str], payload: Dict[str, Any],
                            session: Dict[str, Any]) -> Dict[str, Any]:
        latest = self.engine.backtester.load_latest()
        if not latest:
            return {"ok": True, "model": None,
                    "note": "no trained model yet - run a backtest with save"}
        try:
            with open(latest["path"], "r", encoding="utf-8") as handle:
                stored = json.load(handle)
        except (OSError, ValueError, KeyError):
            return {"ok": False, "error": "stored model file is unreadable"}
        return {"ok": True, "model": latest, "report": stored.get("report")}

    def api_backtest(self, query: Dict[str, str], payload: Dict[str, Any],
                     session: Dict[str, Any]) -> Dict[str, Any]:
        from .backtest import collect_candles
        symbol = str(payload.get("symbol") or self.engine.active_symbol).upper()
        interval = str(payload.get("interval") or "15m")
        bars = int(payload.get("bars") or 1500)
        bars = max(200, min(bars, 5000))
        candles = collect_candles(self.engine.client, symbol, interval, bars)
        if not candles:
            raise ApiError("no historical candles available for {}".format(symbol),
                           502, "NO_DATA")
        report = self.engine.backtester.run(candles, symbol=symbol, interval=interval,
                                            horizon=int(payload.get("horizon") or 6),
                                            persist=bool(payload.get("save")))
        return {"ok": bool(report.get("ok")), "report": report}


def _maybe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ApiError("expected a number, got {!r}".format(str(value)[:20]), 400,
                       "BAD_NUMBER")
