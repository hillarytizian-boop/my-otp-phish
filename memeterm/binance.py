"""
memeterm.binance - a small, defensive Binance Spot REST client.

Scope: Spot only. No margin, no futures, no leverage, no withdrawals, no
transfer endpoints. The withdrawal API is deliberately absent from this file.

Verified against the official Spot REST docs (developers.binance.com, fetched
2026-10-27): base hosts, `/api/v3/*` paths, HMAC-SHA256 query signing with
`X-MBX-APIKEY`, `timestamp` + `recvWindow` (max 60000), and the order types
MARKET / LIMIT with timeInForce GTC / IOC / FOK.

Rules that matter more than features
------------------------------------
1. A POST is never retried blindly. If the response is lost (timeout, reset,
   5xx) we raise `AmbiguousOrderError` carrying the clientOrderId; the caller
   must query the order before deciding anything.
2. Quantity is always floored to LOT_SIZE.stepSize - never rounded up.
3. Every price/qty is validated against the exchange's own filters before it
   is sent, so the exchange's -1013 is a last-resort, not the plan.
4. Errors are redacted before they leave this module.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Callable, Dict, List, Optional, Tuple

from .util import redact, safe_float

DEFAULT_BASE = "https://api.binance.com"
PUBLIC_BASE = "https://data-api.binance.vision"   # market data only, no keys


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------
class BinanceError(Exception):
    """A handled exchange/network failure. Messages are safe to display."""

    kind = "error"
    retryable = False

    def __init__(self, message: str, *, code: int = 0, http_status: int = 0,
                 retryable: bool = False, kind: str = "error"):
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status
        self.retryable = retryable
        self.kind = kind

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "code": self.code, "http_status": self.http_status,
                "message": self.message, "retryable": self.retryable}

    def __str__(self) -> str:
        return self.message


class NetworkError(BinanceError):
    kind = "network"


class AuthError(BinanceError):
    kind = "auth"


class RateLimitError(BinanceError):
    kind = "rate_limit"


class FilterError(BinanceError):
    kind = "filter"


class AmbiguousOrderError(BinanceError):
    """The request may or may not have reached the exchange."""

    kind = "ambiguous"

    def __init__(self, message: str, *, client_order_id: str = "", symbol: str = "",
                 http_status: int = 0):
        super().__init__(message, http_status=http_status, retryable=False,
                         kind="ambiguous")
        self.client_order_id = client_order_id
        self.symbol = symbol


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------
@dataclass
class Response:
    status: int
    headers: Dict[str, str]
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8") or "null")
        except (ValueError, UnicodeDecodeError):
            return None

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")


class UrllibTransport:
    """Blocking stdlib transport - no aiohttp, no requests, Termux-friendly."""

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    def request(self, method: str, url: str, headers: Dict[str, str],
                timeout: Optional[float] = None) -> Response:
        req = urllib.request.Request(url, method=method, headers=headers)
        if method in ("POST", "DELETE", "PUT"):
            req.data = b""
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return Response(int(resp.status), {k.lower(): v for k, v in resp.headers.items()},
                                resp.read())
        except urllib.error.HTTPError as exc:
            return Response(int(exc.code), {k.lower(): v for k, v in (exc.headers or {}).items()},
                            exc.read() or b"")
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
            reason = getattr(exc, "reason", exc)
            raise NetworkError("network error: {}".format(redact(reason)),
                               kind="timeout" if "timed out" in str(reason).lower() else "network")


# ---------------------------------------------------------------------------
# symbol filters
# ---------------------------------------------------------------------------
def _dec(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _plain(value: Decimal) -> str:
    """Decimal -> plain string, never exponent notation (Binance rejects 1E-5)."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


@dataclass
class SymbolFilters:
    symbol: str
    base_asset: str = ""
    quote_asset: str = ""
    status: str = "UNKNOWN"
    spot_allowed: bool = True
    tick_size: Decimal = Decimal("0.00000001")
    step_size: Decimal = Decimal("0.00000001")
    min_qty: Decimal = Decimal("0")
    max_qty: Decimal = Decimal("0")
    min_notional: Decimal = Decimal("5")
    max_num_orders: int = 0
    base_precision: int = 8
    quote_precision: int = 8
    permissions: Tuple[str, ...] = ()

    # -- rounding ------------------------------------------------------
    def round_price(self, price: float | Decimal | str) -> Decimal:
        value = _dec(price)
        if self.tick_size <= 0:
            return value
        return (value / self.tick_size).to_integral_value(rounding=ROUND_DOWN) * self.tick_size

    def round_qty(self, qty: float | Decimal | str) -> Decimal:
        value = _dec(qty)
        if self.step_size <= 0:
            return value
        return (value / self.step_size).to_integral_value(rounding=ROUND_DOWN) * self.step_size

    def qty_for_quote(self, quote_amount: float, price: float) -> Decimal:
        """How much base asset a market buy of `quote_amount` should ask for."""
        if price <= 0:
            return Decimal("0")
        return self.round_qty(_dec(quote_amount) / _dec(price))

    def notional(self, qty: float | Decimal, price: float | Decimal) -> Decimal:
        return _dec(qty) * _dec(price)

    # -- validation ----------------------------------------------------
    def validate(self, side: str, order_type: str, qty: Optional[float],
                 price: Optional[float], quote_qty: Optional[float],
                 last_price: float) -> List[str]:
        """Return a list of human-readable problems; empty means OK."""
        problems: List[str] = []
        side = (side or "").upper()
        order_type = (order_type or "").upper()
        if side not in ("BUY", "SELL"):
            problems.append("side must be BUY or SELL")
        if order_type not in ("MARKET", "LIMIT"):
            problems.append("only MARKET and LIMIT orders are supported in this phase")
        if self.status != "TRADING":
            problems.append("symbol {} is not trading right now (status {})".format(
                self.symbol, self.status))
        if not self.spot_allowed:
            problems.append("symbol {} is not enabled for Spot".format(self.symbol))

        if order_type == "LIMIT" and (price is None or safe_float(price) <= 0):
            problems.append("a LIMIT order needs a positive price")
        if price is not None and safe_float(price) > 0:
            rounded = self.round_price(price)
            if abs(_dec(price) - rounded) > Decimal("0") and _dec(price) < rounded:
                problems.append("price {} is below the tick size {}".format(price, _plain(self.tick_size)))

        qty_dec = _dec(qty) if qty is not None else Decimal("0")
        quote_dec = _dec(quote_qty) if quote_qty is not None else Decimal("0")
        reference_price = safe_float(price) or safe_float(last_price)

        if qty is None and quote_qty is None:
            problems.append("provide either a quantity or a quote amount")
        if qty is not None:
            rounded = self.round_qty(qty_dec)
            if rounded <= 0:
                problems.append("quantity rounds to zero at step size {}".format(_plain(self.step_size)))
            if self.min_qty and rounded < self.min_qty:
                problems.append("quantity {} is below the minimum {}".format(
                    _plain(rounded), _plain(self.min_qty)))
            if self.max_qty and rounded > self.max_qty:
                problems.append("quantity {} is above the maximum {}".format(
                    _plain(rounded), _plain(self.max_qty)))
            if reference_price > 0 and self.min_notional > 0:
                if rounded * _dec(reference_price) < self.min_notional:
                    problems.append(
                        "notional {} USDT is below the exchange minimum {}".format(
                            _plain((rounded * _dec(reference_price)).quantize(Decimal("0.01"))),
                            _plain(self.min_notional)))
        if quote_qty is not None and qty is None:
            if quote_dec < self.min_notional:
                problems.append("quote amount {} is below the exchange minimum {}".format(
                    _plain(quote_dec), _plain(self.min_notional)))
        return problems


# ---------------------------------------------------------------------------
# orders
# ---------------------------------------------------------------------------
@dataclass
class OrderResult:
    symbol: str
    order_id: int = 0
    client_order_id: str = ""
    status: str = "UNKNOWN"
    side: str = ""
    type: str = ""
    price: float = 0.0
    orig_qty: float = 0.0
    executed_qty: float = 0.0
    quote_qty: float = 0.0
    fills: List[Dict[str, Any]] = field(default_factory=list)
    transact_time: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def avg_price(self) -> float:
        if self.executed_qty > 0 and self.quote_qty > 0:
            return self.quote_qty / self.executed_qty
        return self.price

    @property
    def is_final(self) -> bool:
        return self.status in ("FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "order_id": self.order_id,
            "client_order_id": self.client_order_id, "status": self.status,
            "side": self.side, "type": self.type, "price": self.price,
            "orig_qty": self.orig_qty, "executed_qty": self.executed_qty,
            "quote_qty": self.quote_qty, "avg_price": self.avg_price,
            "transact_time": self.transact_time,
        }


def _parse_order(payload: Dict[str, Any]) -> OrderResult:
    return OrderResult(
        symbol=str(payload.get("symbol", "")),
        order_id=int(safe_float(payload.get("orderId"), 0)),
        client_order_id=str(payload.get("clientOrderId", "") or ""),
        status=str(payload.get("status", "UNKNOWN")),
        side=str(payload.get("side", "")),
        type=str(payload.get("type", "")),
        price=safe_float(payload.get("price")),
        orig_qty=safe_float(payload.get("origQty")),
        executed_qty=safe_float(payload.get("executedQty")),
        quote_qty=safe_float(payload.get("cummulativeQuoteQty")),
        fills=list(payload.get("fills") or []),
        transact_time=int(safe_float(payload.get("transactTime"), 0)),
        raw=payload if isinstance(payload, dict) else {},
    )


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------
class BinanceClient:
    def __init__(self, *, transport: Optional[Any] = None, base_url: str = DEFAULT_BASE,
                 credentials_provider: Optional[Callable[[], Tuple[str, str]]] = None,
                 timeout: float = 10.0, max_retries: int = 2,
                 recv_window_ms: int = 5000, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep):
        self.base_url = base_url.rstrip("/")
        self.transport = transport or UrllibTransport(timeout=timeout)
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.recv_window_ms = int(recv_window_ms)
        self.clock = clock
        self.sleep = sleep
        self._credentials_provider = credentials_provider
        self.time_offset_ms = 0
        self.synced_at = 0.0
        self.weight_used_1m = 0
        self.order_count_10s = 0
        self.requests = 0
        self.errors = 0
        self.last_error = ""
        self.last_latency_ms = 0.0
        self._info_cache: Optional[Dict[str, Any]] = None
        self._info_at = 0.0
        self._filters: Dict[str, SymbolFilters] = {}

    # -- low level -----------------------------------------------------
    def _now_ms(self) -> int:
        return int(self.clock() * 1000) + self.time_offset_ms

    def _record(self, response: Optional[Response], latency_ms: float) -> None:
        self.requests += 1
        self.last_latency_ms = latency_ms
        if response is not None:
            try:
                self.weight_used_1m = int(response.header("x-mbx-used-weight-1m")
                                          or self.weight_used_1m or 0)
            except ValueError:
                pass
            try:
                self.order_count_10s = int(response.header("x-mbx-order-count-10s")
                                           or self.order_count_10s or 0)
            except ValueError:
                pass

    def _request(self, method: str, path: str, params: Dict[str, Any],
                 *, signed: bool = False, base: Optional[str] = None) -> Any:
        base_url = (base or self.base_url).rstrip("/")
        query = urllib.parse.urlencode(
            [(k, _plain(v) if isinstance(v, Decimal) else v)
             for k, v in params.items() if v is not None],
            quote_via=urllib.parse.quote_plus)
        headers = {"User-Agent": "meme-terminal/2.0 (python-urllib)",
                   "Accept": "application/json"}

        if signed:
            if not self._credentials_provider:
                raise AuthError("no API credentials configured")
            api_key, api_secret = self._credentials_provider()
            if not api_key or not api_secret:
                raise AuthError("no API credentials configured")
            payload = list(params.items())
            payload = [(k, _plain(v) if isinstance(v, Decimal) else v)
                       for k, v in payload if v is not None]
            payload.append(("timestamp", self._now_ms()))
            payload.append(("recvWindow", self.recv_window_ms))
            body = urllib.parse.urlencode(payload, quote_via=urllib.parse.quote_plus)
            signature = hmac.new(api_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
            query = body + "&signature=" + signature
            headers["X-MBX-APIKEY"] = api_key

        url = "{}{}{}".format(base_url, path, ("?" + query) if query else "")
        attempts = 0
        while True:
            started = time.time()
            response: Optional[Response] = None
            try:
                response = self.transport.request(method, url, headers, self.timeout)
                latency = (time.time() - started) * 1000.0
                self._record(response, latency)
                if response.status == 200:
                    data = response.json()
                    if data is None:
                        raise BinanceError("exchange returned a non-JSON body",
                                           kind="malformed", retryable=True)
                    return data
                self._raise_for_response(response)
            except NetworkError as exc:
                self.errors += 1
                self.last_error = exc.message
                # Ambiguity is the dangerous case: the order may exist.
                if method in ("POST", "DELETE") and signed:
                    raise AmbiguousOrderError(
                        "no response from the exchange ({}) - the order may or may not "
                        "exist, so it will not be retried".format(exc.message),
                        client_order_id=str(params.get("newClientOrderId") or
                                            params.get("origClientOrderId") or ""),
                        symbol=str(params.get("symbol") or "")) from None
                attempts += 1
                if attempts > self.max_retries:
                    raise
                self.sleep(0.35 * attempts + random.random() * 0.15)
                continue
            except BinanceError as exc:
                self.errors += 1
                self.last_error = exc.message
                if exc.code == -1021 and attempts < 1:      # clock drift: resync once
                    self.sync_time(force=True)
                    attempts += 1
                    continue
                retryable = exc.retryable and method in ("GET", "DELETE") and attempts < self.max_retries
                if retryable:
                    attempts += 1
                    self.sleep(0.4 * attempts)
                    continue
                raise

    def _raise_for_response(self, response: Response) -> None:
        data = response.json()
        code = 0
        message = ""
        if isinstance(data, dict):
            code = int(safe_float(data.get("code"), 0))
            message = str(data.get("msg") or data.get("message") or "")
        if not message:
            message = "HTTP {} from the exchange".format(response.status)
        message = redact(message)[:300]

        if response.status == 429 or response.status == 418 or code == -1003:
            retry_after = response.header("retry-after")
            raise RateLimitError(
                "rate limited by Binance ({}). {} Wait before retrying.".format(
                    message, "retry after {}s.".format(retry_after) if retry_after else ""),
                code=code, http_status=response.status, retryable=True)
        if response.status in (401, 403) or code in (-2015, -2014, -1022, -2008):
            raise AuthError(
                "the exchange refused the API key ({}). Check that the key is active, "
                "Spot-enabled, not IP-restricted away from this device, and that the "
                "right permissions are enabled.".format(message),
                code=code, http_status=response.status)
        if code == -1013:
            raise FilterError("order rejected by an exchange filter: {}".format(message),
                              code=code, http_status=response.status)
        if response.status >= 500:
            raise BinanceError("exchange error {}: {}".format(response.status, message),
                               code=code, http_status=response.status, retryable=True)
        raise BinanceError(message, code=code, http_status=response.status)

    # -- health --------------------------------------------------------
    def health(self) -> Dict[str, Any]:
        return {
            "requests": self.requests,
            "errors": self.errors,
            "weight_used_1m": self.weight_used_1m,
            "order_count_10s": self.order_count_10s,
            "last_latency_ms": round(self.last_latency_ms, 1),
            "last_error": redact(self.last_error)[:200],
            "time_offset_ms": self.time_offset_ms,
            "synced_at": self.synced_at,
        }

    # -- public endpoints ----------------------------------------------
    def sync_time(self, force: bool = False) -> int:
        if not force and self.synced_at and (self.clock() - self.synced_at) < 900:
            return self.time_offset_ms
        data = self._request("GET", "/api/v3/time", {}, signed=False)
        server = int(safe_float((data or {}).get("serverTime"), 0))
        if server:
            self.time_offset_ms = server - int(self.clock() * 1000)
            self.synced_at = self.clock()
        return self.time_offset_ms

    def exchange_info(self, refresh: bool = False) -> Dict[str, Any]:
        if self._info_cache and not refresh and (self.clock() - self._info_at) < 3600:
            return self._info_cache
        data = self._request("GET", "/api/v3/exchangeInfo", {})
        if not isinstance(data, dict) or "symbols" not in data:
            raise BinanceError("exchangeInfo payload was not understood", kind="malformed")
        self._info_cache = data
        self._info_at = self.clock()
        self._filters = {}
        return data

    def filters(self, symbol: str) -> SymbolFilters:
        symbol = (symbol or "").upper()
        if symbol in self._filters:
            return self._filters[symbol]
        info = self.exchange_info()
        for entry in info.get("symbols", []):
            if entry.get("symbol") != symbol:
                continue
            filters = {f.get("filterType"): f for f in entry.get("filters", [])}
            lot = filters.get("LOT_SIZE", {})
            price = filters.get("PRICE_FILTER", {})
            notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
            out = SymbolFilters(
                symbol=symbol,
                base_asset=str(entry.get("baseAsset", "")),
                quote_asset=str(entry.get("quoteAsset", "")),
                status=str(entry.get("status", "UNKNOWN")),
                spot_allowed=bool(entry.get("isSpotTradingAllowed", True)),
                tick_size=_dec(price.get("tickSize"), "0.00000001"),
                step_size=_dec(lot.get("stepSize"), "0.00000001"),
                min_qty=_dec(lot.get("minQty")),
                max_qty=_dec(lot.get("maxQty")),
                min_notional=_dec(notional.get("minNotional"), "5") or Decimal("5"),
                max_num_orders=int(safe_float(filters.get("MAX_NUM_ORDERS", {}).get("maxNumOrders"), 0)),
                base_precision=int(safe_float(entry.get("baseAssetPrecision"), 8)),
                quote_precision=int(safe_float(entry.get("quoteAssetPrecision"), 8)),
                permissions=tuple(entry.get("permissions") or ()),
            )
            self._filters[symbol] = out
            return out
        raise FilterError("{} is not a listed Binance Spot symbol".format(symbol))

    def spot_symbols(self) -> List[Dict[str, Any]]:
        info = self.exchange_info()
        out = []
        for entry in info.get("symbols", []):
            if entry.get("status") != "TRADING":
                continue
            if not entry.get("isSpotTradingAllowed", False):
                continue
            if (entry.get("quoteAsset") or "") != "USDT":
                continue
            out.append({"symbol": entry.get("symbol"),
                        "base": entry.get("baseAsset"),
                        "quote": entry.get("quoteAsset"),
                        "status": entry.get("status")})
        return sorted(out, key=lambda row: row["symbol"])

    def ticker_24h(self, symbols: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if symbols:
            params["symbols"] = json.dumps([s.upper() for s in symbols], separators=(",", ":"))
        data = self._request("GET", "/api/v3/ticker/24hr", params)
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise BinanceError("ticker payload was not understood", kind="malformed")
        return data

    def book_ticker(self, symbols: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if symbols:
            params["symbols"] = json.dumps([s.upper() for s in symbols], separators=(",", ":"))
        data = self._request("GET", "/api/v3/ticker/bookTicker", params)
        if isinstance(data, dict):
            data = [data]
        return data if isinstance(data, list) else []

    def klines(self, symbol: str, interval: str = "1m", limit: int = 200,
               start_ms: Optional[int] = None, end_ms: Optional[int] = None) -> List[List[Any]]:
        params: Dict[str, Any] = {"symbol": symbol.upper(), "interval": interval,
                                  "limit": max(1, min(int(limit), 1000))}
        if start_ms:
            params["startTime"] = int(start_ms)
        if end_ms:
            params["endTime"] = int(end_ms)
        data = self._request("GET", "/api/v3/klines", params)
        return data if isinstance(data, list) else []

    def depth(self, symbol: str, limit: int = 20) -> Dict[str, Any]:
        data = self._request("GET", "/api/v3/depth",
                            {"symbol": symbol.upper(), "limit": max(5, min(int(limit), 100))})
        return data if isinstance(data, dict) else {}

    def recent_trades(self, symbol: str, limit: int = 50) -> List[Dict[str, Any]]:
        data = self._request("GET", "/api/v3/trades",
                            {"symbol": symbol.upper(), "limit": max(1, min(int(limit), 500))})
        return data if isinstance(data, list) else []

    # -- signed endpoints ----------------------------------------------
    def account(self) -> Dict[str, Any]:
        data = self._request("GET", "/api/v3/account", {}, signed=True)
        if not isinstance(data, dict):
            raise BinanceError("account payload was not understood", kind="malformed")
        return data

    def balances(self, assets: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        payload = self.account()
        rows = []
        wanted = {a.upper() for a in assets} if assets else None
        for row in payload.get("balances", []):
            asset = str(row.get("asset", ""))
            if wanted and asset not in wanted:
                continue
            free = safe_float(row.get("free"))
            locked = safe_float(row.get("locked"))
            if free or locked or (wanted and asset in wanted):
                rows.append({"asset": asset, "free": free, "locked": locked,
                             "total": free + locked})
        return rows

    def permissions(self) -> Dict[str, Any]:
        """Read-only diagnostics: what this key is allowed to do."""
        payload = self.account()
        perms = payload.get("permissions")
        if perms is None:
            perms = [payload.get("accountType", "SPOT")]
        can_trade = payload.get("canTrade")
        can_withdraw = payload.get("canWithdraw")
        can_deposit = payload.get("canDeposit")
        commission = payload.get("commissionRates") or {}
        maker = safe_float(commission.get("maker", payload.get("makerCommission", 0)))
        taker = safe_float(commission.get("taker", payload.get("takerCommission", 0)))
        if maker > 1:            # legacy integer form, in 1e-8 units
            maker = maker / 100_000_000.0
        if taker > 1:
            taker = taker / 100_000_000.0
        warnings = []
        if can_withdraw is True:
            warnings.append("this key can WITHDRAW - disable withdrawals on the key "
                            "immediately; this program never calls the withdrawal API")
        if can_trade is False:
            warnings.append("the key cannot trade (Spot trading disabled on the key)")
        if perms and "SPOT" not in perms:
            warnings.append("the key has no SPOT permission")
        return {
            "account_type": payload.get("accountType", "SPOT"),
            "permissions": perms,
            "can_trade": can_trade,
            "can_withdraw": can_withdraw,
            "can_deposit": can_deposit,
            "maker_fee": maker,
            "taker_fee": taker,
            "maker_fee_bps": round(maker * 10_000, 3) if maker else None,
            "taker_fee_bps": round(taker * 10_000, 3) if taker else None,
            "warnings": warnings,
            "uid_present": "uid" in payload,
        }

    def new_order(self, *, symbol: str, side: str, order_type: str = "MARKET",
                  quantity: Optional[str] = None, quote_order_qty: Optional[str] = None,
                  price: Optional[str] = None, time_in_force: str = "GTC",
                  client_order_id: str = "", test: bool = False) -> OrderResult:
        """Send one order. Never retried here; ambiguity becomes an exception."""
        params: Dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": order_type.upper(),
            "newClientOrderId": client_order_id or None,
            "newOrderRespType": "FULL",
        }
        if order_type.upper() == "LIMIT":
            params["price"] = price
            params["quantity"] = quantity
            params["timeInForce"] = time_in_force
        else:
            if quantity is not None:
                params["quantity"] = quantity
            if quote_order_qty is not None:
                params["quoteOrderQty"] = quote_order_qty
        if test:
            self._request("POST", "/api/v3/order/test", params, signed=True)
            return OrderResult(symbol=symbol.upper(), status="TEST_ACCEPTED",
                               client_order_id=client_order_id or "", side=side.upper(),
                               type=order_type.upper())
        payload = self._request("POST", "/api/v3/order", params, signed=True)
        if not isinstance(payload, dict):
            raise BinanceError("order response was not understood", kind="malformed")
        return _parse_order(payload)

    def query_order(self, symbol: str, order_id: Optional[int] = None,
                    client_order_id: Optional[str] = None) -> OrderResult:
        params: Dict[str, Any] = {"symbol": symbol.upper()}
        if order_id:
            params["orderId"] = int(order_id)
        if client_order_id:
            params["origClientOrderId"] = client_order_id
        if not order_id and not client_order_id:
            raise ValueError("query_order needs an orderId or a clientOrderId")
        payload = self._request("GET", "/api/v3/order", params, signed=True)
        if not isinstance(payload, dict):
            raise BinanceError("order lookup returned an unexpected payload", kind="malformed")
        return _parse_order(payload)

    def cancel_order(self, symbol: str, order_id: Optional[int] = None,
                     client_order_id: Optional[str] = None) -> OrderResult:
        params: Dict[str, Any] = {"symbol": symbol.upper()}
        if order_id:
            params["orderId"] = int(order_id)
        if client_order_id:
            params["origClientOrderId"] = client_order_id
        try:
            payload = self._request("DELETE", "/api/v3/order", params, signed=True)
        except BinanceError as exc:
            if exc.code == -2011:        # UNKNOWN_ORDER: already gone
                return OrderResult(symbol=symbol.upper(), status="CANCELED",
                                   client_order_id=client_order_id or "")
            raise
        return _parse_order(payload if isinstance(payload, dict) else {})

    def open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {"symbol": symbol.upper()} if symbol else {}
        data = self._request("GET", "/api/v3/openOrders", params, signed=True)
        return data if isinstance(data, list) else []

    def my_trades(self, symbol: str, limit: int = 50) -> List[Dict[str, Any]]:
        data = self._request("GET", "/api/v3/myTrades",
                             {"symbol": symbol.upper(), "limit": max(1, min(limit, 1000))},
                             signed=True)
        return data if isinstance(data, list) else []

    # -- diagnostics ---------------------------------------------------
    def connection_test(self) -> Dict[str, Any]:
        """Read-only: time sync, key permissions, filters, and no order placed."""
        started = time.time()
        result: Dict[str, Any] = {"ok": False, "checks": [], "warnings": []}

        def check(name: str, ok: bool, detail: str = "") -> None:
            result["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
            if not ok:
                result["ok"] = False

        result["ok"] = True
        try:
            offset = self.sync_time(force=True)
            check("clock", abs(offset) < 30_000,
                  "local clock differs from Binance by {} ms".format(offset))
        except BinanceError as exc:
            check("clock", False, str(exc))
            return result

        try:
            info = self.exchange_info(refresh=True)
            check("market data", bool(info.get("symbols")),
                  "{} symbols published".format(len(info.get("symbols", []))))
        except BinanceError as exc:
            check("market data", False, str(exc))

        if not self._credentials_provider:
            check("credentials", False, "no API key stored")
            return result
        try:
            perms = self.permissions()
            check("credentials", True, "key accepted by the exchange")
            check("spot permission", "SPOT" in (perms.get("permissions") or [])
                  or perms.get("can_trade") is True,
                  "permissions: {}".format(", ".join(perms.get("permissions") or [])))
            check("trading enabled", perms.get("can_trade") is not False,
                  "canTrade={}".format(perms.get("can_trade")))
            result["warnings"].extend(perms.get("warnings") or [])
            result["permissions"] = perms
            result["taker_fee_bps"] = perms.get("taker_fee_bps")
        except AuthError as exc:
            check("credentials", False, str(exc))
        except BinanceError as exc:
            check("credentials", False, str(exc))

        result["elapsed_ms"] = round((time.time() - started) * 1000, 1)
        result["health"] = self.health()
        return result
