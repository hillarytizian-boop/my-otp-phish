#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 fomo_provider.py - optional FOMO signal provider for meme_agents.py
================================================================================

READ THIS FIRST: WHICH "FOMO API" EXISTS (checked 2026-10-27)
-------------------------------------------------------------
* fomo.family (FOMO Labs Inc., the social trading app) publishes **no public
  developer API**. No developer portal, no API-key flow, no documented
  endpoints, no SDK. Their terms do not grant permission to automate their
  private backend. This module does NOT touch fomo.family, does NOT ask for a
  FOMO login, wallet, seed phrase or private key, and never scrapes their app.

* The only *documented, credentialed* FOMO data product today is
  https://fomoapi.io  (base URL https://api.fomoapi.io), an **independent,
  unofficial** service that is "not affiliated with fomo.family" in its own
  words. It exposes FOMO-reported trader and token data over REST + WebSocket.
  You need your own API key from https://fomoapi.io/dashboard.

* Anything else (fomo.com, usefomo/fomo-python-sdk, "fomo" npm packages) is a
  different product: fomo.com is a social-proof marketing SaaS, not the
  crypto app. Do not paste wallet secrets into any of them.

So: this provider is **optional, disabled by default, and only activates when
you export FOMO_API_KEY**. With no key the whole bot keeps working exactly as
before (DexScreener + GeckoTerminal + CoinGecko + RugCheck), and every FOMO
field in the UI reads "n/a" instead of being invented.

ENDPOINTS USED (all from https://fomoapi.io/docs, verified 2026-10-27)
----------------------------------------------------------------------
  GET /v2/token/{mint}/stats?networkId=1399811149
      holders, top10HoldersPercent and 5m/1h/4h/24h flow windows
      (buys, sells, uniqueBuyers, uniqueSellers, buyVolumeUsd,
       sellVolumeUsd, netVolumeUsd, buySellRatio).          250 credits.
  GET /v2/token/{mint}/warnings?networkId=1399811149
      buyDisabled / sellDisabled / tradingDisabled / allowlisted / severe /
      warnings[{name,description,severity,type}]. Cached 15 min. 250 credits.
  GET /v2/token/{mint}/devs?networkId=1399811149
      deployer + insider rows with amount, valueUsd, cost basis, realized and
      unrealized PnL and their own thesis.                  250 credits.
  GET /token/{mint}/holders?network=1399811149&limit=N
      FOMO's live top holders (handle, amount, valueUsd, priceUsd).
      Optional: off by default because it is another 250 credits. 250 credits.

  Auth: `authorization: Bearer <key>` (the docs also show `X-API-Key`).
  Errors: 401 bad key, 402 out of credits, 404 unknown token, 429 rate limit,
  503 upstream degraded; `retry-after` is honoured. Free keys: 20 requests per
  minute, 250,000 credits/month. Every response carries `x-credits-cost` and
  `x-credits-remaining`, which this module records in its health block.

BUDGET MATH (why this is deliberately timid)
--------------------------------------------
  stats + warnings = 500 credits per token. A free key's 250,000/month is
  therefore ~500 token lookups per month, i.e. ~16/day. The bot defaults to
  enriching at most 3 candidates per scan and caching each mint for 15 minutes.
  Keep it that way; this is a data source, not a firehose.

WHAT THIS DATA MAY AND MAY NOT BE USED FOR
-------------------------------------------
  * It is flow and metadata ("who is buying", "is selling disabled"), not a
    price feed. Prices still come from DexScreener/GeckoTerminal.
  * `warnings` empty does NOT mean safe (the docs say so explicitly), and
    `allowlisted` is FOMO's opinion, not a security audit.
  * `sellDisabled` / `tradingDisabled` are hard red flags: the app itself will
    not let people sell. This module surfaces them; meme_agents.py treats them
    as a rejection.
  * Everything here can be delayed, wrong, or unavailable. Missing data is
    reported as missing and never converted into a pass.

USAGE (standalone sanity check, no key needed)
----------------------------------------------
  python3 fomo_provider.py            # prints status + doc summary, exits 0
  FOMO_API_KEY=... python3 fomo_provider.py <mint>   # live lookup, 2 calls
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("fomo")

__version__ = "1.0.0"

BASE_URL = "https://api.fomoapi.io"
SOLANA_NETWORK_ID = 1399811149          # documented in the fomoapi.io examples
FOMO_API_KEY_ENV = "FOMO_API_KEY"
FOMO_BASE_URL_ENV = "FOMO_API_BASE"
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_MIN_INTERVAL_S = 3.2            # free tier: 20 requests/minute per key
DEFAULT_CACHE_TTL_S = 900.0             # warnings are cached 15 min upstream too
DEFAULT_MAX_RETRIES = 2
USER_AGENT = "meme_agents/1.3 (+https://github.com/hillarytizian-boop/my-otp-phish)"

FOMO_STATUS_NOTE = (
    "FOMO Labs publishes no public API; this provider uses the independent, "
    "unofficial fomoapi.io service and needs your own FOMO_API_KEY."
)

# Solana mint addresses are base58 of 32 bytes -> 32..44 characters.
_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

Transport = Callable[[str, Dict[str, str], float], Tuple[int, Dict[str, str], bytes]]


# ---------------------------------------------------------------------------
# small helpers (kept local so this module has zero project dependencies)
# ---------------------------------------------------------------------------

def _num(value: Any, default: float = 0.0) -> float:
    """Coerce API JSON into a float without ever raising."""
    if value is None or isinstance(value, bool):
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out or out in (float("inf"), float("-inf")):     # NaN / inf
        return default
    return out


def _int(value: Any, default: int = 0) -> int:
    return int(_num(value, float(default)))


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def is_solana_mint(value: Any) -> bool:
    """True only for a plausible base58 Solana mint. Rejects 0x… EVM addresses."""
    text = str(value or "").strip()
    return bool(_MINT_RE.match(text))


def normalize_mint(value: Any) -> str:
    """Return the mint if it is valid Solana base58, else an empty string."""
    text = str(value or "").strip()
    return text if is_solana_mint(text) else ""


def dedupe_mints(rows: Any) -> List[str]:
    """
    Deduplicate a mixed list of discovery rows into verified Solana mints.

    Accepts dicts ({"mint": ...} or {"tokenAddress": ...}), strings, or a mix.
    Order is preserved; invalid and non-Solana addresses are dropped, which is
    what stops an EVM token from a shared feed polluting the Solana pipeline.
    """
    out: List[str] = []
    seen = set()
    for row in rows or []:
        candidate = ""
        if isinstance(row, dict):
            for key in ("mint", "address", "tokenAddress", "token_address", "token"):
                if row.get(key):
                    candidate = row.get(key)
                    break
        elif isinstance(row, str):
            candidate = row
        mint = normalize_mint(candidate)
        if mint and mint not in seen:
            seen.add(mint)
            out.append(mint)
    return out


class FomoError(Exception):
    """A provider failure with enough detail to decide whether to retry."""

    def __init__(self, message: str, status: int = 0, retryable: bool = False,
                 code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.code = code

    def short(self) -> str:
        return "{} (http {}, code {})".format(self, self.status or "-", self.code or "-")


# ---------------------------------------------------------------------------
# normalized models
# ---------------------------------------------------------------------------

@dataclass
class FlowWindow:
    """One time window of FOMO trade flow (5m / 1h / 4h / 24h)."""
    buys: int = 0
    sells: int = 0
    unique_buyers: int = 0
    unique_sellers: int = 0
    buy_vol_usd: float = 0.0
    sell_vol_usd: float = 0.0
    net_vol_usd: float = 0.0
    ratio: float = 0.0

    @property
    def trades(self) -> int:
        return self.buys + self.sells

    @property
    def total_vol_usd(self) -> float:
        return self.buy_vol_usd + self.sell_vol_usd

    def as_dict(self) -> Dict[str, Any]:
        return {"buys": self.buys, "sells": self.sells, "trades": self.trades,
                "unique_buyers": self.unique_buyers, "unique_sellers": self.unique_sellers,
                "buy_vol_usd": round(self.buy_vol_usd, 2),
                "sell_vol_usd": round(self.sell_vol_usd, 2),
                "net_vol_usd": round(self.net_vol_usd, 2), "ratio": round(self.ratio, 3)}


@dataclass
class TokenStats:
    mint: str = ""
    holders: int = 0
    top10_pct: float = -1.0             # -1 = unknown, never 0 as a fake "safe" value
    windows: Dict[str, FlowWindow] = field(default_factory=dict)
    fetched_at: float = 0.0

    def window(self, name: str) -> FlowWindow:
        return self.windows.get(name) or FlowWindow()

    def as_dict(self) -> Dict[str, Any]:
        return {"mint": self.mint, "holders": self.holders,
                "top10_pct": self.top10_pct if self.top10_pct >= 0 else None,
                "windows": {k: v.as_dict() for k, v in self.windows.items()},
                "fetched_at": self.fetched_at}


@dataclass
class TokenWarnings:
    mint: str = ""
    buy_disabled: bool = False
    sell_disabled: bool = False
    trading_disabled: bool = False
    allowlisted: bool = False
    severe: bool = False
    count: int = 0
    items: List[Dict[str, str]] = field(default_factory=list)
    fetched_at: float = 0.0

    def blocking_reasons(self) -> List[str]:
        """Only the warnings that genuinely mean 'do not buy this'."""
        out: List[str] = []
        if self.sell_disabled:
            out.append("FOMO disables selling this token")
        if self.trading_disabled:
            out.append("FOMO disables trading this token")
        elif self.buy_disabled:
            out.append("FOMO disables buying this token")
        for item in self.items:
            if str(item.get("severity", "")).upper() == "SEVERE":
                name = item.get("name") or item.get("type") or "severe warning"
                out.append("FOMO severe warning: {}".format(name))
                break
        return out

    def as_dict(self) -> Dict[str, Any]:
        return {"mint": self.mint, "buy_disabled": self.buy_disabled,
                "sell_disabled": self.sell_disabled,
                "trading_disabled": self.trading_disabled,
                "allowlisted": self.allowlisted, "severe": self.severe,
                "count": self.count, "items": self.items,
                "fetched_at": self.fetched_at}


@dataclass
class DevHolding:
    handle: str = ""
    amount: float = 0.0
    value_usd: float = 0.0
    realized_usd: float = 0.0
    unrealized_usd: float = 0.0
    is_dev: bool = True
    thesis: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"handle": self.handle, "amount": self.amount,
                "value_usd": round(self.value_usd, 2),
                "realized_usd": round(self.realized_usd, 2),
                "unrealized_usd": round(self.unrealized_usd, 2),
                "is_dev": self.is_dev, "thesis": self.thesis[:200]}


@dataclass
class FomoEnrichment:
    """
    Everything FOMO said about one token, plus what failed.

    `complete` is intentionally strict: it is True only when both stats and
    warnings answered. A partial payload is still useful (warnings alone can
    veto a trade) but it must never be presented as a full picture.
    """
    mint: str = ""
    stats: Optional[TokenStats] = None
    warnings: Optional[TokenWarnings] = None
    devs: List[DevHolding] = field(default_factory=list)
    holders: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    fetched_at: float = 0.0
    from_cache: bool = False

    @property
    def available(self) -> bool:
        return self.stats is not None or self.warnings is not None

    @property
    def complete(self) -> bool:
        return self.stats is not None and self.warnings is not None

    def age_s(self, now: Optional[float] = None) -> float:
        now = now or time.time()
        return max(0.0, now - (self.fetched_at or now))

    def blocking_reasons(self) -> List[str]:
        return self.warnings.blocking_reasons() if self.warnings else []

    def dev_exit_reason(self) -> str:
        """
        'The deployer sold out' as a rejection reason, or ''.

        Documented fields: `amount` / `valueUsd` are what the dev holds NOW and
        `realizedPnlUsd` is what they took off the table. A row with no holdings
        left but realized profit is a dev who exited - the cheapest rug signal
        the upstream docs advertise.
        """
        if not self.devs:
            return ""
        held = sum(d.value_usd for d in self.devs)
        realized = sum(d.realized_usd for d in self.devs)
        if held <= 0.0 and realized > 0.0:
            return "FOMO: deployer/insiders sold out (${:.0f} realized, nothing held)".format(
                realized)
        return ""

    def summary(self) -> str:
        parts = []
        if self.stats:
            w = self.stats.window("5m")
            parts.append("5m {}/{} buys/sells, top10 {}".format(
                w.buys, w.sells,
                "{:.1f}%".format(self.stats.top10_pct) if self.stats.top10_pct >= 0 else "?"))
        if self.warnings:
            parts.append(self.warnings.count and "{} warning(s)".format(self.warnings.count)
                         or "no warnings")
        if not parts:
            parts.append("no data")
        if self.errors:
            parts.append("partial: " + self.errors[0][:60])
        return " | ".join(parts)

    def as_dict(self) -> Dict[str, Any]:
        return {"mint": self.mint,
                "stats": self.stats.as_dict() if self.stats else None,
                "warnings": self.warnings.as_dict() if self.warnings else None,
                "devs": [d.as_dict() for d in self.devs],
                "holders": self.holders,
                "errors": self.errors, "complete": self.complete,
                "fetched_at": self.fetched_at, "from_cache": self.from_cache}


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

def urllib_transport(url: str, headers: Dict[str, str], timeout: float
                     ) -> Tuple[int, Dict[str, str], bytes]:
    """Default transport: stdlib urllib, no third-party packages."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.getcode(), dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:                # 401/402/429/5xx all land here
        body = b""
        try:
            body = exc.read()
        except Exception:                                # noqa: BLE001 - best effort
            pass
        return exc.code, dict(exc.headers or {}), body


class FomoProvider:
    """
    Cached, rate-limited, retrying client for the documented fomoapi.io routes.

    Thread-safety: `enrich_sync()` is safe to call from several threads; an
    internal lock serializes requests so the free-tier request rate is never
    exceeded. The async API (`enrich`) simply runs it in a worker thread, so
    this module stays dependency-free and fits the bot's stdlib-only rule.
    """

    def __init__(self, api_key: Optional[str] = None, base_url: str = "",
                 timeout_s: float = DEFAULT_TIMEOUT_S,
                 min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
                 cache_ttl_s: float = DEFAULT_CACHE_TTL_S,
                 max_retries: int = DEFAULT_MAX_RETRIES,
                 transport: Optional[Transport] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time) -> None:
        self.api_key = (api_key if api_key is not None
                        else os.environ.get(FOMO_API_KEY_ENV, "")).strip()
        self.base_url = (base_url or os.environ.get(FOMO_BASE_URL_ENV, "")
                         or BASE_URL).rstrip("/")
        self.timeout_s = max(2.0, float(timeout_s))
        self.min_interval_s = max(0.0, float(min_interval_s))
        self.cache_ttl_s = max(0.0, float(cache_ttl_s))
        self.max_retries = max(0, int(max_retries))
        self._transport = transport or urllib_transport
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._cache: Dict[str, Tuple[float, FomoEnrichment]] = {}
        self.health_data: Dict[str, Any] = {
            "configured": bool(self.api_key),
            "calls": 0, "errors": 0, "rate_limited": 0, "retries": 0,
            "credits_spent": 0, "credits_remaining": None,
            "last_ok_ts": 0.0, "last_error": "", "last_latency_ms": 0.0,
            "tokens_enriched": 0, "partial": 0,
        }

    # ------------------------------------------------------------------ health
    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def health(self) -> Dict[str, Any]:
        now = self._clock()
        data = dict(self.health_data)
        data.update({
            "base_url": self.base_url,
            "available": self.configured,
            "status": ("disabled (no FOMO_API_KEY)" if not self.configured
                       else "ok" if data["last_ok_ts"] else "configured, no successful call yet"),
            "last_ok_age_s": (round(now - data["last_ok_ts"], 1)
                              if data["last_ok_ts"] else None),
        })
        return data

    def _record_error(self, message: str) -> None:
        self.health_data["errors"] = int(self.health_data["errors"]) + 1
        self.health_data["last_error"] = str(message)[:200]

    # ------------------------------------------------------------------- HTTP
    def _request(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        One documented GET with pacing, retries and strict JSON validation.

        Raises FomoError for everything; callers decide whether a failure is
        fatal for the token or just "unknown".
        """
        if not self.configured:
            raise FomoError("no FOMO_API_KEY configured", code="not_configured")
        url = self.base_url + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            url += "?" + urllib.parse.urlencode(clean)
        headers = {
            "authorization": "Bearer " + self.api_key,
            "accept": "application/json",
            "user-agent": USER_AGENT,
        }
        attempt = 0
        last_error = ""
        while attempt <= self.max_retries:
            with self._lock:
                wait = self.min_interval_s - (self._clock() - self._last_call)
                if wait > 0:
                    self._sleep(wait)
                self._last_call = self._clock()
            started = self._clock()
            try:
                status, resp_headers, body = self._transport(url, headers, self.timeout_s)
            except FomoError:
                raise
            except Exception as exc:                     # noqa: BLE001 - network layer
                last_error = "network: {}".format(exc)[:160]
                status, resp_headers, body = 0, {}, b""
            latency_ms = (self._clock() - started) * 1000.0
            self.health_data["calls"] = int(self.health_data["calls"]) + 1

            headers_low = {str(k).lower(): str(v) for k, v in (resp_headers or {}).items()}
            self._track_credits(headers_low)

            if status == 200:
                self.health_data["last_ok_ts"] = self._clock()
                self.health_data["last_latency_ms"] = round(latency_ms, 1)
                try:
                    parsed = json.loads(body.decode("utf-8", "replace") or "{}")
                except (ValueError, UnicodeDecodeError) as exc:
                    self._record_error("malformed JSON: {}".format(exc))
                    raise FomoError("malformed JSON from {}".format(path), status=200,
                                    retryable=True, code="bad_json")
                if not isinstance(parsed, dict):
                    self._record_error("unexpected payload type {}".format(type(parsed).__name__))
                    raise FomoError("unexpected payload from {}".format(path), status=200,
                                    retryable=False, code="bad_shape")
                return parsed

            # ---- failures worth retrying
            if status == 429:
                self.health_data["rate_limited"] = int(self.health_data["rate_limited"]) + 1
                retry_after = _num(headers_low.get("retry-after"), 0.0)
                last_error = "rate limited (429)"
                if attempt < self.max_retries:
                    self.health_data["retries"] = int(self.health_data["retries"]) + 1
                    self._sleep(_clamp(retry_after or (1.5 * (2 ** attempt)), 0.5, 30.0))
                    attempt += 1
                    continue
                raise FomoError(last_error, status=429, retryable=True, code="rate_limited")

            if status == 0 or status >= 500:
                last_error = last_error or "HTTP {}".format(status or "network error")
                if attempt < self.max_retries:
                    self.health_data["retries"] = int(self.health_data["retries"]) + 1
                    self._sleep(_clamp(1.5 * (2 ** attempt), 0.5, 30.0))
                    attempt += 1
                    continue
                self._record_error(last_error)
                raise FomoError(last_error, status=status, retryable=True, code="upstream")

            # ---- failures not worth retrying
            code = ""
            message = ""
            try:
                parsed = json.loads(body.decode("utf-8", "replace") or "{}")
                if isinstance(parsed, dict):
                    code = str(parsed.get("error") or "")
                    message = str(parsed.get("message") or "")
            except (ValueError, UnicodeDecodeError):
                pass
            if status == 401:
                self._record_error("invalid FOMO_API_KEY (401)")
                raise FomoError("invalid or missing API key", status=401, code="unauthorized")
            if status == 402:
                self._record_error("out of credits (402)")
                raise FomoError("out of credits", status=402, code=code or "credits_exhausted")
            if status == 404:
                raise FomoError("token not found upstream", status=404, code="not_found")
            self._record_error("HTTP {} {}".format(status, code or message)[:180])
            raise FomoError("HTTP {} {}".format(status, code or message).strip(),
                            status=status, code=code)

    def _track_credits(self, headers_low: Dict[str, str]) -> None:
        cost = _int(headers_low.get("x-credits-cost"), -1)
        if cost > 0:
            self.health_data["credits_spent"] = int(self.health_data["credits_spent"]) + cost
        remaining = headers_low.get("x-credits-remaining")
        if remaining is not None:
            self.health_data["credits_remaining"] = _int(remaining, 0)

    # ------------------------------------------------------------ normalizers
    @staticmethod
    def parse_stats(mint: str, payload: Dict[str, Any]) -> TokenStats:
        """Turn a /v2/token/{mint}/stats response into TokenStats (tolerant)."""
        stats = TokenStats(mint=mint, fetched_at=time.time())
        stats.holders = _int(payload.get("holders"), 0)
        if payload.get("top10HoldersPercent") is not None:
            stats.top10_pct = _clamp(_num(payload.get("top10HoldersPercent"), -1.0), -1.0, 100.0)
        windows = payload.get("windows")
        if isinstance(windows, dict):
            for name in ("5m", "1h", "4h", "24h"):
                raw = windows.get(name)
                if not isinstance(raw, dict):
                    continue
                buy_vol = _num(raw.get("buyVolumeUsd"))
                sell_vol = _num(raw.get("sellVolumeUsd"))
                ratio = raw.get("buySellRatio")
                stats.windows[name] = FlowWindow(
                    buys=_int(raw.get("buys"), 0),
                    sells=_int(raw.get("sells"), 0),
                    unique_buyers=_int(raw.get("uniqueBuyers"), 0),
                    unique_sellers=_int(raw.get("uniqueSellers"), 0),
                    buy_vol_usd=buy_vol,
                    sell_vol_usd=sell_vol,
                    net_vol_usd=_num(raw.get("netVolumeUsd"), buy_vol - sell_vol),
                    ratio=(_num(ratio, 0.0) if ratio is not None
                           else (buy_vol / sell_vol if sell_vol > 0 else 0.0)),
                )
        return stats

    @staticmethod
    def parse_warnings(mint: str, payload: Dict[str, Any]) -> TokenWarnings:
        """Turn a /v2/token/{mint}/warnings response into TokenWarnings."""
        warn = TokenWarnings(mint=mint, fetched_at=time.time())
        warn.buy_disabled = _bool(payload.get("buyDisabled"))
        warn.sell_disabled = _bool(payload.get("sellDisabled"))
        warn.trading_disabled = _bool(payload.get("tradingDisabled")) or \
            (warn.buy_disabled and warn.sell_disabled)
        warn.allowlisted = _bool(payload.get("allowlisted"))
        warn.severe = _bool(payload.get("severe"))
        rows = payload.get("warnings")
        if isinstance(rows, list):
            for item in rows:
                if not isinstance(item, dict):
                    continue
                warn.items.append({
                    "name": str(item.get("name") or "")[:80],
                    "type": str(item.get("type") or "")[:40],
                    "severity": str(item.get("severity") or "").upper()[:12],
                    "description": str(item.get("description") or "")[:160],
                })
        warn.count = _int(payload.get("count"), len(warn.items))
        if not warn.severe:
            warn.severe = any(i.get("severity") == "SEVERE" for i in warn.items)
        return warn

    @staticmethod
    def parse_devs(payload: Dict[str, Any]) -> List[DevHolding]:
        rows = payload.get("devs")
        if rows is None:
            rows = payload.get("data")
        out: List[DevHolding] = []
        if not isinstance(rows, list):
            return out
        for item in rows:
            if not isinstance(item, dict):
                continue
            wallet = item.get("wallet") if isinstance(item.get("wallet"), dict) else {}
            out.append(DevHolding(
                handle=str(item.get("handle") or item.get("name") or "")[:40],
                amount=_num(item.get("amount")),
                value_usd=_num(item.get("valueUsd")),
                realized_usd=_num(item.get("realizedPnlUsd")),
                unrealized_usd=_num(item.get("unrealizedPnlUsd")),
                is_dev=_bool(item.get("isDev")) if item.get("isDev") is not None else True,
                thesis=str(item.get("thesis") or "")[:200] or str(wallet.get("solana") or "")[:44],
            ))
        return out

    @staticmethod
    def parse_holders(payload: Dict[str, Any]) -> Dict[str, Any]:
        rows = payload.get("holders")
        if rows is None:
            rows = payload.get("data")
        clean: List[Dict[str, Any]] = []
        if isinstance(rows, list):
            for item in rows:
                if not isinstance(item, dict):
                    continue
                clean.append({"handle": str(item.get("handle") or "")[:40],
                              "amount": _num(item.get("amount")),
                              "value_usd": _num(item.get("valueUsd")),
                              "price_usd": _num(item.get("priceUsd"))})
        return {"count": _int(payload.get("count"), len(clean)),
                "total_holders": (payload.get("totalHolders")
                                  if payload.get("totalHolders") is not None else None),
                "scope": str(payload.get("scope") or ""),
                "stale": _bool(payload.get("stale")),
                "age_s": _num(payload.get("ageSeconds")),
                "available": payload.get("available") is not False,
                "rows": clean[:50]}

    # ------------------------------------------------------------ public API
    def stats(self, mint: str) -> TokenStats:
        payload = self._request("/v2/token/{}/stats".format(urllib.parse.quote(mint)),
                                {"networkId": SOLANA_NETWORK_ID})
        return self.parse_stats(mint, payload)

    def warnings(self, mint: str) -> TokenWarnings:
        payload = self._request("/v2/token/{}/warnings".format(urllib.parse.quote(mint)),
                                {"networkId": SOLANA_NETWORK_ID})
        return self.parse_warnings(mint, payload)

    def devs(self, mint: str) -> List[DevHolding]:
        payload = self._request("/v2/token/{}/devs".format(urllib.parse.quote(mint)),
                                {"networkId": SOLANA_NETWORK_ID})
        return self.parse_devs(payload)

    def holders(self, mint: str, limit: int = 50) -> Dict[str, Any]:
        payload = self._request("/token/{}/holders".format(urllib.parse.quote(mint)),
                                {"network": SOLANA_NETWORK_ID, "limit": max(1, int(limit))})
        return self.parse_holders(payload)

    def enrich_sync(self, mint: str, include_slow: bool = False,
                    force: bool = False) -> Optional[FomoEnrichment]:
        """
        Two cheap calls (stats + warnings) and, optionally, the slow ones
        (devs + holders). Never raises: a total failure returns None, a partial
        failure returns what arrived plus `errors`.
        """
        if not self.configured:
            return None
        mint = normalize_mint(mint)
        if not mint:
            return None
        now = self._clock()
        cached = self._cache.get(mint)
        if cached and not force and (now - cached[0]) < self.cache_ttl_s:
            hit = cached[1]
            return FomoEnrichment(**{**hit.__dict__, "from_cache": True})

        result = FomoEnrichment(mint=mint, fetched_at=now)
        for name, fetcher in (("stats", self.stats), ("warnings", self.warnings)):
            try:
                value = fetcher(mint)
                setattr(result, name, value)
            except FomoError as exc:
                result.errors.append("{}: {}".format(name, exc.short()))
            except Exception as exc:                     # noqa: BLE001 - never crash a scan
                result.errors.append("{}: {}".format(name, str(exc)[:120]))
        if include_slow:
            try:
                result.devs = self.devs(mint)
            except FomoError as exc:
                result.errors.append("devs: {}".format(exc.short()))
            try:
                result.holders = self.holders(mint)
            except FomoError as exc:
                result.errors.append("holders: {}".format(exc.short()))

        if not result.available:
            self._record_error("; ".join(result.errors)[:200] or "no data")
            return None
        if not result.complete:
            self.health_data["partial"] = int(self.health_data["partial"]) + 1
        self.health_data["tokens_enriched"] = int(self.health_data["tokens_enriched"]) + 1
        self._cache[mint] = (now, result)
        if len(self._cache) > 400:
            for key in sorted(self._cache, key=lambda k: self._cache[k][0])[:100]:
                self._cache.pop(key, None)
        return result

    async def enrich(self, mint: str, include_slow: bool = False,
                     force: bool = False) -> Optional[FomoEnrichment]:
        """Async facade: runs the blocking client in a worker thread."""
        import asyncio
        return await asyncio.to_thread(self.enrich_sync, mint, include_slow, force)

    def cached(self, mint: str) -> bool:
        """True when this mint can be enriched again at zero credit cost."""
        mint = normalize_mint(mint)
        hit = self._cache.get(mint)
        return bool(hit and (self._clock() - hit[0]) < self.cache_ttl_s)

    def clear_cache(self) -> None:
        self._cache.clear()


def create_provider(api_key: Optional[str] = None, **kwargs: Any) -> FomoProvider:
    """Factory used by meme_agents.py; returns a disabled provider when keyless."""
    return FomoProvider(api_key=api_key, **kwargs)


# ---------------------------------------------------------------------------
# standalone check
# ---------------------------------------------------------------------------

def _main() -> int:
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    provider = FomoProvider()
    print("=" * 70)
    print(" fomo_provider.py {}".format(__version__))
    print("=" * 70)
    print(" status     : {}".format(provider.health()["status"]))
    print(" base url   : {}".format(provider.base_url))
    print(" note       : {}".format(FOMO_STATUS_NOTE))
    print(" endpoints  : /v2/token/{mint}/stats, /warnings, /devs, /token/{mint}/holders")
    print(" auth       : authorization: Bearer <FOMO_API_KEY>")
    print(" cost       : 250 credits per call (free key: 250,000/month, 20 req/min)")
    if len(sys.argv) > 1:
        mint = normalize_mint(sys.argv[1])
        if not mint:
            print("\n '{}' is not a Solana mint address.".format(sys.argv[1]))
            return 2
        if not provider.configured:
            print("\n No FOMO_API_KEY set: nothing was requested.")
            print(" Export one from https://fomoapi.io/dashboard to enable this provider.")
            return 0
        data = provider.enrich_sync(mint, include_slow=True, force=True)
        print("\n enrich({}) ->".format(mint))
        print(json.dumps(data.as_dict() if data else {"available": False},
                         indent=2, sort_keys=True)[:4000])
        print("\n health: {}".format(json.dumps(provider.health(), sort_keys=True)))
        return 0
    print("\n Run with a mint to test a live lookup:  FOMO_API_KEY=... python3 fomo_provider.py <mint>")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
