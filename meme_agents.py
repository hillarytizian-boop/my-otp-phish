#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 MEME AGENTS - a 100-agent meme-coin swarm for a non-root Android phone (Termux)
================================================================================

WHAT THIS IS
------------
One Python process. 100 asyncio tasks ("agents") sharing:
  * one rate-limited, cached, retrying HTTP client (protects your phone + IP),
  * one portfolio / ledger,
  * one risk manager with veto power and a kill switch.

It discovers new Solana meme coins on DexScreener + GeckoTerminal (+ CoinGecko
where its API key allows), screens them through hard safety filters, scores
short-term momentum, and trades with tiny fixed size. PAPER mode is the default
and simulates slippage, price impact, network fees, priority fees and ATA rent.

TRUTH IN ADVERTISING (read this before you run it)
--------------------------------------------------
  * NOBODY can sell exactly at the peak. No script can. The trailing stop is the
    best practical approximation: it gives back 10-15% of the top and nothing
    more. Everything else is marketing.
  * Most meme coins go to zero. The filters here reject most scams most of the
    time, not all scams all of the time. You can lose the entire 10 USD.
  * Public APIs rate-limit, change shape and go down. This bot degrades and
    keeps retrying; it cannot fix a 429 storm.
  * "Creator sold more than 20%" is NOT knowable from public data at discovery
    time (nobody publishes the creator's original allocation). What IS
    implemented: (a) reject if the creator still holds > MAX_CREATOR_HOLD_PCT of
    supply, and (b) remember the creator's balance at entry and emergency-exit
    if it drops 20% or more while we hold. See CHECK_CREATOR note below.
  * The FOMO app has no public trading API. This bot never calls FOMO. Live
    execution is Jupiter (Solana) through a separate, swappable executor class.

INSTALL (Termux, no root)
-------------------------
  pkg update && pkg upgrade -y
  pkg install -y python
  # PAPER MODE NEEDS NOTHING ELSE. This file is 100% standard library.
  #
  # LIVE MODE ONLY (Solana signing). solders compiles Rust; it may take a long
  # time or fail on a phone. That is why paper mode exists.
  pkg install -y rust binutils libsodium
  pip install --upgrade pip wheel
  pip install solders            # if this fails, stay in paper mode or run live
                                 # on a VPS/PC with the same file.

RUN (paper mode, the default)
-----------------------------
  python meme_agents.py
  python meme_agents.py --free-run-hours 24      # 24h forward paper test
  python meme_agents.py --selftest               # offline test suite, no network

RUN (live mode - burner wallet only, money you can lose)
-------------------------------------------------------
  export SOLANA_PRIVATE_KEY='<base58 secret key of a BURNER wallet>'
  export SOLANA_RPC_URL='https://api.mainnet-beta.solana.com'   # optional
  export TELEGRAM_BOT_TOKEN='...'    # optional
  export TELEGRAM_CHAT_ID='...'      # optional
  python meme_agents.py --live
  # it then demands you type exactly: I ACCEPT THE RISK

SECRETS
-------
Never in this file, never in git. The private key is read from the environment
ONLY when --live is passed, and it is never logged or written to disk.

DATA SOURCES (exactly these, nothing else)
------------------------------------------
  1. DexScreener   /token-profiles/latest/v1, /token-boosts/latest/v1,
                   /token-pairs/v1/solana/{mint}
  2. GeckoTerminal /networks/solana/new_pools, /trending_pools,
                   /networks/solana/pools/{pool}, /pools/{pool}/trades
  3. CoinGecko     /coins/list/new (needs a key; auto-falls back to
                   GeckoTerminal /tokens/info_recently_updated when keyless)
  4. RugCheck      /v1/tokens/{mint}/report (mint/freeze authority, LP lock,
                   top holders, creator balance, risk list)
  5. Jupiter       quote endpoints (honeypot route check in paper, real swaps
                   in live)
  6. Telegram      optional outbound alerts only (no market data)

KNOWN LIMITS / FAILURE MODES are documented in README.md next to this file and
summarised at the end of --selftest output.
================================================================================
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import gzip
import json
import logging
import logging.handlers
import math
import os
import random
import re
import signal
import sys
import time
import traceback
import types
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

VERSION = "1.0.0"

# --- well-known Solana mints -------------------------------------------------
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# --- API bases ---------------------------------------------------------------
DEXSCREENER = "https://api.dexscreener.com"
GECKOTERMINAL = "https://api.geckoterminal.com/api/v2"
COINGECKO = "https://api.coingecko.com/api/v3"
RUGCHECK = "https://api.rugcheck.xyz"
JUPITER_LITE = "https://lite-api.jup.ag/swap/v1"
JUPITER_KEYED = "https://api.jup.ag/swap/v1"
DEFAULT_RPC = "https://api.mainnet-beta.solana.com"

AGENT_TOTAL = 100
MAX_BODY_BYTES = 8 * 1024 * 1024          # hard cap so a huge report can't OOM the phone
USER_AGENT = "meme-agents/{} (Termux; +https://github.com/hillarytizian-boop/my-otp-phish)".format(VERSION)

log = logging.getLogger("meme")


# =============================================================================
# SECTION 1.  SMALL UTILITIES
# =============================================================================

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def utc_str(ts: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else now_utc()
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def day_key(ts: Optional[float] = None) -> str:
    """UTC day bucket for the daily-loss limit. UTC keeps it unambiguous."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else now_utc()
    return dt.strftime("%Y-%m-%d")


def fnum(value: Any, default: float = 0.0) -> float:
    """Coerce anything the APIs throw at us into a float, safely."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        try:
            f = float(value)
        except (TypeError, ValueError):
            return default
        return default if math.isnan(f) or math.isinf(f) else f
    try:
        f = float(str(value).strip().replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return default
    return default if math.isnan(f) or math.isinf(f) else f


def clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def short_addr(addr: str, head: int = 4, tail: int = 4) -> str:
    addr = str(addr or "")
    return addr if len(addr) <= head + tail + 3 else "{}..{}".format(addr[:head], addr[-tail:])


def fmt_usd(x: float, places: int = 2) -> str:
    if x is None:
        return "?"
    try:
        sign = "-" if x < 0 else ""
        a = abs(x)
        if a >= 1_000_000:
            return "{}{:.2f}M".format(sign + "$", a / 1_000_000.0)
        if a >= 1_000:
            return "{}{:.1f}K".format(sign + "$", a / 1_000.0)
        return "{}{:.{}f}".format(sign + "$", a, places)
    except (TypeError, ValueError):
        return "?"


def fmt_pct(x: float) -> str:
    return "{:+.1f}%".format(x)


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "{:.1f}{}".format(n, unit)
        n /= 1024.0
    return "{:.1f}GB".format(n)


# --- base58 (stdlib only; needed to read a Solana secret key) ---------------
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}


def b58decode(s: str) -> bytes:
    s = str(s).strip()
    if not s:
        return b""
    n = 0
    for ch in s:
        if ch not in _B58_INDEX:
            raise ValueError("invalid base58 character: {!r}".format(ch))
        n = n * 58 + _B58_INDEX[ch]
    pad = 0
    for ch in s:
        if ch == "1":
            pad += 1
        else:
            break
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * pad + body


# =============================================================================
# SECTION 2.  CONFIG  (everything tunable lives here)
# =============================================================================

@dataclass
class Config:
    # ---- mode ---------------------------------------------------------------
    live: bool = False
    start_capital_usd: float = 10.0

    # ---- the swarm ----------------------------------------------------------
    # Must total 100; normalized_agents() scales it pro-rata if you change it.
    agents: Dict[str, int] = field(default_factory=lambda: {
        "scouts": 24, "safety": 24, "momentum": 22, "executors": 29, "risk": 1,
    })
    exact_agent_total: bool = True       # False = run exactly the counts given (tests)

    # ---- money & risk -------------------------------------------------------
    max_trade_usd: float = 2.0            # HARD: 2 USD per trade
    max_open_positions: int = 3           # HARD: 3 at once
    daily_loss_limit_usd: float = 3.0     # HARD: stop for the day at -3 USD
    min_wallet_buffer_sol: float = 0.01   # keep fees + ATA rent in the wallet

    # ---- safety filters (reject if any fails) ------------------------------
    min_liquidity_usd: float = 50_000.0
    max_top10_pct: float = 30.0
    min_token_age_min: float = 10.0
    max_pair_age_hours: float = 6.0
    max_gain_since_launch_pct: float = 400.0     # >5x from launch = too late
    max_creator_hold_pct: float = 10.0           # creator still holding this much now
    min_lp_locked_pct: float = 90.0
    min_sell_buy_txn_ratio: float = 0.20         # sells/buys below this = honeypot-ish
    min_buys_for_honeypot_check: int = 30
    honeypot_check_usd: float = 2.0              # notional used for the simulated sell
    max_entry_price_impact_pct: float = 3.0      # simulated $2 sell impact
    max_rugcheck_score: float = 40.0             # 0 = safest, 100 = obvious scam
    reject_on_rugcheck_danger: bool = True
    strict_unknown: bool = True                  # unknown safety data = reject

    # ---- momentum -----------------------------------------------------------
    momentum_threshold: float = 55.0             # 0..100
    max_5m_pump_pct: float = 40.0                # never chase a +40% 5m candle

    # ---- exits --------------------------------------------------------------
    stop_loss_pct: float = -20.0
    tp1_pct: float = 100.0
    tp1_frac: float = 0.50                       # sell 50% at +100%
    tp2_pct: float = 300.0
    tp2_frac: float = 0.25                       # sell 25% (of original) at +300%
    trail_start_pct: float = 25.0                # arm the trail once up 25%
    trail_pct: float = 15.0
    trail_tight_pct: float = 10.0
    trail_tight_at_pct: float = 500.0
    emergency_liq_drop_pct: float = 40.0
    emergency_sell_buy_ratio: float = 3.0
    emergency_min_volume_usd: float = 50.0
    emergency_creator_dump_pct: float = 20.0
    time_stop_hours: float = 2.0
    time_stop_mfe_pct: float = 10.0              # "sideways" = never got +10%

    # ---- slippage -----------------------------------------------------------
    entry_slip_cap_bps: int = 300                # 3%
    emergency_slip_cap_bps: int = 800             # 8%
    paper_entry_slip_bps: int = 150
    paper_exit_slip_bps: int = 250
    paper_emergency_slip_bps: int = 500
    jupiter_slippage_bps: int = 100
    live_confirm_timeout_s: float = 45.0

    # ---- fees (Solana reality, small money killer) --------------------------
    sol_network_fee_sol: float = 0.000005
    priority_fee_sol: float = 0.0001
    ata_rent_sol: float = 0.00203928            # token account rent, NOT refunded here
    fallback_sol_usd: float = 150.0
    fallback_token_decimals: int = 6

    # ---- timing / networking -------------------------------------------------
    dashboard_every_s: float = 30.0
    position_refresh_s: float = 15.0
    facts_refresh_s: float = 90.0
    micro_refresh_s: float = 30.0
    risk_poll_s: float = 2.0
    http_concurrency: int = 6
    http_timeout_s: float = 15.0
    http_retries: int = 3
    http_backoff_s: float = 1.5
    seen_ttl_s: float = 1800.0
    max_queue: int = 400
    max_candidates_per_feed: int = 30
    cooldown_after_exit_s: float = 900.0        # do not re-buy the same mint for 15m
    cooldown_after_reject_s: float = 1800.0

    # ---- files ---------------------------------------------------------------
    ledger_csv: str = "trades.csv"
    log_file: str = "bot.log"
    stop_file: str = "STOP"
    report_file: str = ""                       # empty = auto name by mode

    # ---- autonomous paper test ----------------------------------------------
    free_run_hours: float = 0.0                 # >0 = stop after N hours and report
    max_runtime_hours: float = 0.0              # absolute cap (0 = unlimited)

    # ---- misc ----------------------------------------------------------------
    cg_api_key: str = ""                        # optional CoinGecko demo/pro key
    rugcheck_report_max_age_s: float = 120.0
    verbose_progress: bool = True

    # ------------------------------------------------------------------ helpers
    def normalized_agents(self) -> Dict[str, int]:
        pools = ("scouts", "safety", "momentum", "executors")
        counts = {k: max(0, int(self.agents.get(k, 0))) for k in pools}
        counts["risk"] = max(1, int(self.agents.get("risk", 1)))
        total = sum(counts.values())
        if total == AGENT_TOTAL or not self.exact_agent_total:
            return counts
        # Scale the four worker pools pro-rata so the swarm really is 100 agents.
        budget = max(len(pools), AGENT_TOTAL - counts["risk"])
        pool_sum = sum(counts[k] for k in pools) or 1
        scaled = {k: max(1, int(round(counts[k] * budget / float(pool_sum)))) for k in pools}
        drift = budget - sum(scaled.values())
        biggest = max(pools, key=lambda k: scaled[k])
        scaled[biggest] = max(1, scaled[biggest] + drift)
        scaled["risk"] = counts["risk"]
        log.warning("agent mix %s summed to %d; scaled to %d: %s",
                    {k: counts[k] for k in pools}, total, sum(scaled.values()), scaled)
        return scaled


# =============================================================================
# SECTION 3.  LOGGING + CSV LEDGER
# =============================================================================

LEDGER_FIELDS = [
    "ts_utc", "mode", "agent", "event", "mint", "symbol", "side",
    "qty_tokens", "price_usd", "usd_value", "fee_usd", "pnl_usd",
    "reason", "detail",
]


def setup_logging(cfg: Config, console_level: int = logging.INFO) -> None:
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)sZ %(levelname)-7s %(message)s", "%Y-%m-%dT%H:%M:%S")
    fmt.converter = time.gmtime

    fh = logging.handlers.RotatingFileHandler(
        cfg.log_file, maxBytes=4 * 1024 * 1024, backupCount=2, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)

    ch = logging.StreamHandler(stream=sys.stdout)
    ch.setLevel(console_level)
    ch.setFormatter(fmt)
    log.addHandler(ch)


class Ledger:
    """Append-only CSV of *every* decision. Flushed on every row: phones die."""

    def __init__(self, path: str, mode: str):
        self.path = path
        self.mode = mode
        self.lock = asyncio.Lock()
        self._ensure_header()

    def _ensure_header(self) -> None:
        try:
            needs = (not os.path.exists(self.path)) or os.path.getsize(self.path) == 0
            if needs:
                with open(self.path, "w", newline="", encoding="utf-8") as fh:
                    csv.writer(fh).writerow(LEDGER_FIELDS)
        except OSError as exc:                      # read-only storage etc.
            log.error("cannot write ledger %s: %s", self.path, exc)

    async def record(self, event: str, agent: str = "system", mint: str = "",
                     symbol: str = "", side: str = "", qty: float = 0.0,
                     price: float = 0.0, usd: float = 0.0, fee_usd: float = 0.0,
                     pnl_usd: float = 0.0, reason: str = "", detail: str = "") -> None:
        row = [utc_str(), self.mode, agent, event, mint, symbol, side,
               "{:.8f}".format(qty) if qty else "", "{:.10g}".format(price) if price else "",
               "{:.6f}".format(usd) if usd else "", "{:.6f}".format(fee_usd) if fee_usd else "",
               "{:.6f}".format(pnl_usd) if pnl_usd else "", reason, detail[:400]]
        async with self.lock:
            try:
                with open(self.path, "a", newline="", encoding="utf-8") as fh:
                    csv.writer(fh).writerow(row)
                    fh.flush()
            except OSError as exc:
                log.error("ledger write failed: %s", exc)
        level = logging.INFO
        if event in ("REJECT", "VETO"):
            level = logging.DEBUG
        elif event in ("EMERGENCY", "ERROR", "HALT"):
            level = logging.WARNING
        log.log(level, "[%s] %s %s %s %s %s", agent, event, symbol or short_addr(mint),
                side, reason, detail[:160])


# =============================================================================
# SECTION 4.  RATE-LIMITED, CACHED HTTP CLIENT (shared by all 100 agents)
# =============================================================================

class HttpError(Exception):
    def __init__(self, status: int, url: str, message: str = "", body: str = "",
                 retry_after: float = 0.0):
        super().__init__("HTTP {} {}: {}".format(status, _short_url(url),
                                                 redact(message or body[:200])))
        self.status = status
        self.url = url
        self.body = body
        self.retry_after = retry_after

    @property
    def transient(self) -> bool:
        return self.status in (0, 408, 425, 429, 500, 502, 503, 504, 522, 524)


_SECRET_RE = re.compile(r"(bot\d+:[A-Za-z0-9_-]+|x-api-key=[^&\s]+)", re.IGNORECASE)


def redact(text: str) -> str:
    """Never let a bot token or API key reach bot.log, the CSV or the dashboard."""
    return _SECRET_RE.sub("bot<redacted>", str(text))


def _short_url(url: str) -> str:
    url = redact(url)
    return url if len(url) <= 110 else url[:107] + "..."


class HostRateLimiter:
    """Per-host pacing: minimum gap between calls + optional sliding-window cap."""

    def __init__(self, interval_s: float, max_per_minute: Optional[int] = None):
        self.interval = max(0.0, interval_s)
        self.max_per_minute = max_per_minute
        self._stamps: List[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                t = time.monotonic()
                while self._stamps and t - self._stamps[0] > 60.0:
                    self._stamps.pop(0)
                wait = 0.0
                if self._stamps:
                    wait = max(wait, self._stamps[-1] + self.interval - t)
                if self.max_per_minute and len(self._stamps) >= self.max_per_minute:
                    wait = max(wait, self._stamps[0] + 60.05 - t)
                if wait <= 0.0:
                    self._stamps.append(t)
                    return
            await asyncio.sleep(min(wait, 5.0))


def default_transport(url: str, method: str, headers: Dict[str, str],
                      body: Optional[bytes], timeout: float) -> Tuple[int, Dict[str, str], bytes]:
    """Blocking urllib call; always executed inside a thread by HttpClient."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BODY_BYTES + 1)
            if len(raw) > MAX_BODY_BYTES:
                raise HttpError(413, url, "response too large")
            enc = (resp.headers.get("Content-Encoding") or "").lower()
            if "gzip" in enc:
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    raise HttpError(0, url, "bad gzip payload")
            return resp.status, dict(resp.headers), raw
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(MAX_BODY_BYTES) or b""
        except Exception:
            raw = b""
        retry_after = fnum(exc.headers.get("Retry-After") if exc.headers else 0, 0.0)
        raise HttpError(exc.code, url, str(exc.reason), raw.decode("utf-8", "replace"), retry_after)
    except urllib.error.URLError as exc:
        raise HttpError(0, url, "network: {}".format(exc.reason))
    except (TimeoutError, OSError) as exc:
        raise HttpError(0, url, "socket: {}".format(exc))


@dataclass
class HttpStats:
    requests: int = 0
    cache_hits: int = 0
    inflight_waits: int = 0
    retries: int = 0
    errors_4xx: int = 0
    errors_5xx: int = 0
    errors_net: int = 0
    bytes_down: int = 0
    last_error: str = ""
    per_host: Dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        return ("req={} cache={} inflight={} retry={} 4xx={} 5xx={} net={} data={}"
                .format(self.requests, self.cache_hits, self.inflight_waits, self.retries,
                        self.errors_4xx, self.errors_5xx, self.errors_net,
                        human_bytes(self.bytes_down)))


class HttpClient:
    """
    One shared client for the whole swarm.

    * per-host rate limiting (GeckoTerminal 30/min, DexScreener 300/min, ...)
    * TTL cache + single-flight: 24 scouts asking the same URL cause ONE request
    * retries with exponential backoff + jitter, honours Retry-After
    * hard 8 MB body cap and gzip support (mobile data matters)
    * no thread pool explosion: a semaphore bounds concurrent urllib threads
    """

    def __init__(self, cfg: Config, transport: Optional[Callable[..., Any]] = None,
                 stats: Optional[HttpStats] = None):
        self.cfg = cfg
        self.transport = transport or default_transport
        self.stats = stats or HttpStats()
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._inflight: Dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(max(1, cfg.http_concurrency))
        self._limiters: Dict[str, HostRateLimiter] = {}
        self._limiters_lock = asyncio.Lock()

    # -- rate limiting ------------------------------------------------------
    async def _limiter(self, host: str) -> HostRateLimiter:
        async with self._limiters_lock:
            lim = self._limiters.get(host)
            if lim is None:
                # Conservative defaults. Public APIs punish bursts, not totals.
                if "geckoterminal" in host:
                    lim = HostRateLimiter(2.1, max_per_minute=25)
                elif "dexscreener" in host:
                    lim = HostRateLimiter(0.25, max_per_minute=180)
                elif "coingecko" in host:
                    lim = HostRateLimiter(6.5, max_per_minute=8)
                elif "rugcheck" in host:
                    lim = HostRateLimiter(1.2, max_per_minute=40)
                elif "jup.ag" in host:
                    lim = HostRateLimiter(2.1, max_per_minute=25)
                elif "solana" in host or "helius" in host or "quiknode" in host:
                    lim = HostRateLimiter(0.1, max_per_minute=300)
                else:
                    lim = HostRateLimiter(1.0, max_per_minute=30)
                self._limiters[host] = lim
            return lim

    # -- public API ---------------------------------------------------------
    async def get_json(self, url: str, params: Optional[Dict[str, Any]] = None,
                       ttl: float = 15.0, retries: Optional[int] = None,
                       headers: Optional[Dict[str, str]] = None) -> Any:
        full = url if not params else url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        return await self._json("GET", full, None, ttl, retries, headers)

    async def post_json(self, url: str, payload: Dict[str, Any], ttl: float = 0.0,
                        retries: Optional[int] = None,
                        headers: Optional[Dict[str, str]] = None) -> Any:
        return await self._json("POST", url, payload, ttl, retries, headers)

    # -- internals ----------------------------------------------------------
    async def _json(self, method: str, full: str, payload: Optional[Dict[str, Any]],
                    ttl: float, retries: Optional[int],
                    headers: Optional[Dict[str, str]]) -> Any:
        key = "{} {}".format(method, full) + ("" if payload is None else json.dumps(payload, sort_keys=True)[:400])
        now = time.monotonic()

        fut: Optional[asyncio.Future] = None
        owner = False
        async with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now and ttl > 0:
                self.stats.cache_hits += 1
                return hit[1]
            fut = self._inflight.get(key)
            if fut is None:
                fut = asyncio.get_running_loop().create_future()
                self._inflight[key] = fut
                owner = True
            else:
                self.stats.inflight_waits += 1

        if not owner:
            return await fut

        try:
            data = await self._request(method, full, payload, retries, headers)
            if ttl > 0:
                async with self._lock:
                    self._cache[key] = (time.monotonic() + ttl, data)
                    if len(self._cache) > 4000:                 # cheap housekeeping
                        cutoff = time.monotonic()
                        for k in [k for k, (exp, _) in self._cache.items() if exp < cutoff][:2000]:
                            self._cache.pop(k, None)
            fut.set_result(data)
            return data
        except BaseException as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            if fut.done() and not fut.cancelled():
                fut.exception()          # mark retrieved; avoids noisy warnings
            async with self._lock:
                self._inflight.pop(key, None)

    async def _request(self, method: str, url: str, payload: Optional[Dict[str, Any]],
                       retries: Optional[int], extra_headers: Optional[Dict[str, str]]) -> Any:
        host = urllib.parse.urlparse(url).netloc.lower()
        limiter = await self._limiter(host)
        attempts = (self.cfg.http_retries if retries is None else retries) + 1
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        last_exc: Optional[Exception] = None

        for attempt in range(attempts):
            await limiter.acquire()
            headers = {
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": USER_AGENT,
            }
            if body is not None:
                headers["Content-Type"] = "application/json"
            if extra_headers:
                headers.update(extra_headers)
            try:
                async with self._sem:
                    status, resp_headers, raw = await asyncio.to_thread(
                        self.transport, url, method, headers, body, self.cfg.http_timeout_s)
                self.stats.requests += 1
                self.stats.per_host[host] = self.stats.per_host.get(host, 0) + 1
                self.stats.bytes_down += len(raw)
                if not (200 <= int(status) < 300):
                    raise HttpError(int(status), url, "bad status", raw[:300].decode("utf-8", "replace"))
                try:
                    return json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise HttpError(0, url, "invalid JSON: {}".format(exc))
            except HttpError as exc:
                last_exc = exc
                self.stats.last_error = str(exc)[:180]
                if exc.status == 0:
                    self.stats.errors_net += 1
                elif 400 <= exc.status < 500:
                    self.stats.errors_4xx += 1
                elif exc.status >= 500:
                    self.stats.errors_5xx += 1
                if not exc.transient or attempt == attempts - 1:
                    raise                       # 4xx (except 429) is permanent: no retry
                self.stats.retries += 1
                delay = max(exc.retry_after, self.cfg.http_backoff_s * (2 ** attempt))
                delay *= 0.75 + random.random() * 0.5
                log.debug("retry %d/%d in %.1fs: %s", attempt + 1, attempts - 1, delay, exc)
                await asyncio.sleep(delay)
        raise last_exc or HttpError(0, url, "unreachable")


# =============================================================================
# SECTION 5.  DATA MODELS
# =============================================================================

@dataclass
class MarketSnapshot:
    mint: str
    symbol: str = ""
    name: str = ""
    price_usd: float = 0.0
    liquidity_usd: float = 0.0
    fdv_usd: float = 0.0
    pair_address: str = ""
    pool_address: str = ""
    dex_id: str = ""
    pair_created_ms: int = 0
    price_change_5m: float = 0.0
    price_change_1h: float = 0.0
    price_change_6h: float = 0.0
    price_change_24h: float = 0.0
    volume_5m: float = 0.0
    volume_1h: float = 0.0
    volume_6h: float = 0.0
    volume_24h: float = 0.0
    buys_5m: int = 0
    sells_5m: int = 0
    buys_1h: int = 0
    sells_1h: int = 0
    buyers_1h: int = 0
    sellers_1h: int = 0
    source: str = ""
    ts: float = 0.0

    def age_minutes(self, now: Optional[float] = None) -> float:
        if not self.pair_created_ms:
            return -1.0
        now = now or time.time()
        return max(0.0, (now - self.pair_created_ms / 1000.0) / 60.0)


@dataclass
class MicroStats:
    """Short-window trade microstructure rebuilt from GeckoTerminal's tape."""
    trades_seen: int = 0
    window_covered_s: float = 0.0
    buys_5m: int = 0
    sells_5m: int = 0
    buy_vol_5m: float = 0.0
    sell_vol_5m: float = 0.0
    buyers_5m: int = 0
    sellers_5m: int = 0
    buys_1h: int = 0
    sells_1h: int = 0
    buy_vol_1h: float = 0.0
    sell_vol_1h: float = 0.0
    buyers_1h: int = 0
    sellers_1h: int = 0
    last_price: float = 0.0
    ts: float = 0.0

    @property
    def total_5m(self) -> int:
        return self.buys_5m + self.sells_5m


@dataclass
class TokenFacts:
    """Everything RugCheck (plus our arithmetic) knows about the mint."""
    mint: str
    symbol: str = ""
    decimals: int = 6
    supply: float = 0.0
    mint_authority: Optional[str] = None
    freeze_authority: Optional[str] = None
    creator: str = ""
    creator_balance_raw: float = -1.0
    creator_hold_pct: float = -1.0
    top10_pct: float = -1.0
    top10_included_holders: int = 0
    lp_locked_pct: float = -1.0
    lp_market: str = ""
    rugged: bool = False
    score: float = -1.0
    risks: List[Dict[str, Any]] = field(default_factory=list)
    danger_risks: List[str] = field(default_factory=list)
    warn_risks: List[str] = field(default_factory=list)
    total_holders: int = 0
    error: str = ""
    ts: float = 0.0

    @property
    def available(self) -> bool:
        return not self.error


@dataclass
class Check:
    cid: str
    ok: Optional[bool]              # True pass, False fail, None unknown
    detail: str
    critical: bool = True

    @property
    def label(self) -> str:
        return {"PASS": "PASS", "FAIL": "FAIL", "????": "????"}[
            "PASS" if self.ok is True else "FAIL" if self.ok is False else "????"]


@dataclass
class SafetyResult:
    passed: bool
    checks: List[Check]
    reasons: List[str]
    score: float = 0.0

    def line(self) -> str:
        return " ".join("{}:{}".format(c.cid, c.label) for c in self.checks)

    def failures(self) -> List[Check]:
        return [c for c in self.checks if c.ok is False or (c.ok is None and c.critical)]


@dataclass
class MomentumResult:
    score: float
    components: Dict[str, float]
    weights: Dict[str, float]
    veto: str = ""
    notes: List[str] = field(default_factory=list)


@dataclass
class ExitDecision:
    qty: float
    kind: str
    reason: str
    emergency: bool = False
    tag: str = ""            # "tp1" / "tp2" / "" - used to mark the ladder rung


@dataclass
class Fill:
    ok: bool = False
    price: float = 0.0
    qty_tokens: float = 0.0
    usd_value: float = 0.0
    fee_usd: float = 0.0
    slippage_bps: float = 0.0
    tx_sig: str = ""
    error: str = ""
    unconfirmed: bool = False
    detail: str = ""


@dataclass
class Position:
    pos_id: str
    mint: str
    symbol: str
    opened_ts: float
    entry_price: float
    entry_cost_usd: float
    initial_tokens: float
    tokens: float
    peak_price: float
    peak_ts: float
    entry_fee_usd: float = 0.0
    realized_usd: float = 0.0
    fees_usd: float = 0.0
    tp1_done: bool = False
    tp2_done: bool = False
    trail_active: bool = False
    entry_liquidity_usd: float = 0.0
    creator_baseline_raw: float = -1.0
    last_price: float = 0.0
    last_price_ts: float = 0.0
    last_facts_ts: float = 0.0
    last_micro_ts: float = 0.0
    snap: Optional[MarketSnapshot] = None
    micro: Optional[MicroStats] = None
    facts: Optional[TokenFacts] = None
    source: str = ""
    agent: str = ""
    notes: List[str] = field(default_factory=list)
    open: bool = True
    closed_ts: float = 0.0
    exit_reason: str = ""
    locked: bool = False

    def age_s(self, now: Optional[float] = None) -> float:
        return max(0.0, (now or time.time()) - self.opened_ts)

    def remaining_cost_usd(self) -> float:
        return self.entry_price * self.tokens

    def unrealized_usd(self, price: Optional[float] = None) -> float:
        p = price if price is not None else self.last_price
        if p <= 0:
            return 0.0
        return self.tokens * p - self.remaining_cost_usd()

    def pnl_pct(self, price: Optional[float] = None) -> float:
        p = price if price is not None else self.last_price
        if self.entry_price <= 0 or p <= 0:
            return 0.0
        return (p / self.entry_price - 1.0) * 100.0

    def mfe_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (self.peak_price / self.entry_price - 1.0) * 100.0

    def trail_pct_now(self, cfg: "Config") -> float:
        return cfg.trail_tight_pct if self.mfe_pct() >= cfg.trail_tight_at_pct else cfg.trail_pct


# =============================================================================
# SECTION 6.  SOURCE ADAPTERS (the only external data this bot ever reads)
# =============================================================================

def _iso_to_ts(value: Any) -> float:
    if not value:
        return 0.0
    try:
        s = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return 0.0


class Sources:
    def __init__(self, cfg: Config, http: HttpClient):
        self.cfg = cfg
        self.http = http
        self._sol_price = 0.0
        self._sol_price_ts = 0.0

    # ---------------------------------------------------------------- discovery
    async def dexscreener_profiles(self) -> List[Dict[str, Any]]:
        data = await self.http.get_json(DEXSCREENER + "/token-profiles/latest/v1", ttl=45)
        items = data if isinstance(data, list) else (data or {}).get("profiles") or []
        return self._dex_discovery(items, "dexscreener:profiles")

    async def dexscreener_boosts(self) -> List[Dict[str, Any]]:
        data = await self.http.get_json(DEXSCREENER + "/token-boosts/latest/v1", ttl=60)
        items = data if isinstance(data, list) else (data or {}).get("profiles") or []
        return self._dex_discovery(items, "dexscreener:boosts")

    def _dex_discovery(self, items: Any, tag: str) -> List[Dict[str, Any]]:
        out = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            if str(it.get("chainId", "")).lower() != "solana":
                continue
            mint = str(it.get("tokenAddress") or "").strip()
            if not self._looks_like_mint(mint):
                continue
            out.append({"mint": mint, "source": tag})
        return out

    async def geckoterminal_new_pools(self) -> List[Dict[str, Any]]:
        data = await self.http.get_json(GECKOTERMINAL + "/networks/solana/new_pools",
                                        params={"page": 1, "include": "base_token"}, ttl=40)
        return self._gt_discovery(data, "geckoterminal:new_pools")

    async def geckoterminal_trending(self) -> List[Dict[str, Any]]:
        data = await self.http.get_json(GECKOTERMINAL + "/networks/solana/trending_pools",
                                        params={"page": 1, "include": "base_token"}, ttl=60)
        return self._gt_discovery(data, "geckoterminal:trending")

    def _gt_discovery(self, data: Any, tag: str) -> List[Dict[str, Any]]:
        out = []
        for it in (data or {}).get("data") or []:
            if not isinstance(it, dict):
                continue
            attrs = it.get("attributes") or {}
            rel = ((it.get("relationships") or {}).get("base_token") or {}).get("data") or {}
            raw_id = str(rel.get("id") or "")
            mint = raw_id.split("_", 1)[1] if raw_id.startswith("solana_") else ""
            if not self._looks_like_mint(mint):
                continue
            out.append({
                "mint": mint,
                "source": tag,
                "pool_address": str(attrs.get("address") or ""),
                "symbol": str(attrs.get("name") or "").split("/")[0].strip(),
            })
        return out

    async def coingecko_recent(self) -> List[Dict[str, Any]]:
        """
        CoinGecko's /coins/list/new is a paid-tier endpoint. With CG_API_KEY we use
        it (then resolve each id to a Solana contract). Without a key we do NOT
        silently pretend: we fall back to GeckoTerminal's keyless
        /tokens/info_recently_updated and tag the source honestly.
        """
        if not self.cfg.cg_api_key:
            data = await self.http.get_json(
                GECKOTERMINAL + "/tokens/info_recently_updated",
                params={"network": "solana", "include": "network"}, ttl=180)
            out = []
            for it in (data or {}).get("data") or []:
                mint = str((it.get("attributes") or {}).get("address") or "")
                if self._looks_like_mint(mint):
                    out.append({"mint": mint, "source": "geckoterminal:recently_updated(no CG key)"})
            return out[:60]

        headers_pool = [
            {"x-cg-demo-api-key": self.cfg.cg_api_key},
            {"x-cg-pro-api-key": self.cfg.cg_api_key},
        ]
        rows: Any = None
        last_err: Optional[Exception] = None
        for headers in headers_pool:
            try:
                rows = await self.http.get_json(COINGECKO + "/coins/list/new", ttl=300,
                                                headers=headers, retries=1)
                break
            except HttpError as exc:
                last_err = exc
        if rows is None:
            raise last_err or HttpError(0, COINGECKO + "/coins/list/new", "no response")
        if not isinstance(rows, list):
            return []
        out = []
        for coin in rows[:8]:                       # CoinGecko keyless/pro rate limits are brutal
            coin_id = str((coin or {}).get("id") or "")
            if not coin_id:
                continue
            try:
                detail = await self.http.get_json(
                    COINGECKO + "/coins/" + urllib.parse.quote(coin_id),
                    params={"localization": "false", "tickers": "false",
                            "market_data": "false", "community_data": "false",
                            "developer_data": "false"},
                    ttl=900, retries=1,
                    headers={"x-cg-demo-api-key": self.cfg.cg_api_key})
            except HttpError:
                continue
            mint = str(((detail or {}).get("platforms") or {}).get("solana") or "")
            if self._looks_like_mint(mint):
                out.append({"mint": mint, "source": "coingecko:recently_added"})
        return out

    @staticmethod
    def _looks_like_mint(mint: str) -> bool:
        return bool(mint) and 32 <= len(mint) <= 48 and re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]+", mint) is not None

    # ---------------------------------------------------------------- enrichment
    async def dexscreener_snapshot(self, mint: str) -> Optional[MarketSnapshot]:
        pairs: Any = None
        try:
            pairs = await self.http.get_json(
                "{}/token-pairs/v1/solana/{}".format(DEXSCREENER, urllib.parse.quote(mint)), ttl=20)
        except HttpError as exc:
            log.debug("token-pairs failed for %s (%s), trying legacy endpoint", mint, exc)
        if isinstance(pairs, dict):
            pairs = pairs.get("pairs")
        if not isinstance(pairs, list) or not pairs:
            try:
                legacy = await self.http.get_json(
                    "{}/latest/dex/tokens/{}".format(DEXSCREENER, urllib.parse.quote(mint)), ttl=20)
                pairs = (legacy or {}).get("pairs") or []
            except HttpError:
                return None
        best = None
        best_liq = -1.0
        for p in pairs:
            if not isinstance(p, dict):
                continue
            base = (p.get("baseToken") or {}).get("address")
            if base != mint:                        # we want the pair where our mint is the base
                continue
            liq = fnum((p.get("liquidity") or {}).get("usd"))
            if liq > best_liq:
                best, best_liq = p, liq
        if best is None:
            return None
        vol = best.get("volume") or {}
        txns = best.get("txns") or {}
        chg = best.get("priceChange") or {}

        def _tx(window: str, side: str) -> int:
            return int(fnum((txns.get(window) or {}).get(side)))

        return MarketSnapshot(
            mint=mint,
            symbol=str((best.get("baseToken") or {}).get("symbol") or "")[:24],
            name=str((best.get("baseToken") or {}).get("name") or "")[:48],
            price_usd=fnum(best.get("priceUsd")),
            liquidity_usd=best_liq,
            fdv_usd=fnum(best.get("fdv") or best.get("marketCap")),
            pair_address=str(best.get("pairAddress") or ""),
            dex_id=str(best.get("dexId") or ""),
            pair_created_ms=int(fnum(best.get("pairCreatedAt"))),
            price_change_5m=fnum(chg.get("m5")),
            price_change_1h=fnum(chg.get("h1")),
            price_change_6h=fnum(chg.get("h6")),
            price_change_24h=fnum(chg.get("h24")),
            volume_5m=fnum(vol.get("m5")),
            volume_1h=fnum(vol.get("h1")),
            volume_6h=fnum(vol.get("h6")),
            volume_24h=fnum(vol.get("h24")),
            buys_5m=_tx("m5", "buys"), sells_5m=_tx("m5", "sells"),
            buys_1h=_tx("h1", "buys"), sells_1h=_tx("h1", "sells"),
            source="dexscreener", ts=time.time(),
        )

    async def geckoterminal_pool_for_token(self, mint: str) -> Optional[Dict[str, Any]]:
        data = await self.http.get_json(
            "{}/networks/solana/tokens/{}/pools".format(GECKOTERMINAL, urllib.parse.quote(mint)),
            params={"page": 1}, ttl=30)
        pools = (data or {}).get("data") or []
        best, best_liq = None, -1.0
        for p in pools:
            if not isinstance(p, dict):
                continue
            liq = fnum((p.get("attributes") or {}).get("reserve_in_usd"))
            if liq > best_liq:
                best, best_liq = p, liq
        return best

    def apply_gt_pool(self, snap: MarketSnapshot, pool: Dict[str, Any]) -> MarketSnapshot:
        attrs = (pool or {}).get("attributes") or {}
        if not attrs:
            return snap
        if not snap.pool_address:
            snap.pool_address = str(attrs.get("address") or "")
        if snap.price_usd <= 0:
            snap.price_usd = fnum(attrs.get("base_token_price_usd"))
        if snap.liquidity_usd <= 0:
            snap.liquidity_usd = fnum(attrs.get("reserve_in_usd"))
        if not snap.pair_created_ms:
            snap.pair_created_ms = int(_iso_to_ts(attrs.get("pool_created_at")) * 1000)
        if snap.fdv_usd <= 0:
            snap.fdv_usd = fnum(attrs.get("fdv_usd") or attrs.get("market_cap_usd"))
        vol = attrs.get("volume_usd") or {}
        chg = attrs.get("price_change_percentage") or {}
        txns = attrs.get("transactions") or {}
        if snap.volume_5m <= 0:
            snap.volume_5m = fnum(vol.get("m5") or vol.get("m15"))
        if snap.volume_1h <= 0:
            snap.volume_1h = fnum(vol.get("h1"))
        if snap.price_change_5m == 0:
            snap.price_change_5m = fnum(chg.get("m5"))
        if snap.price_change_1h == 0:
            snap.price_change_1h = fnum(chg.get("h1"))
        h1 = txns.get("h1") or {}
        snap.buyers_1h = int(fnum(h1.get("buyers")))
        snap.sellers_1h = int(fnum(h1.get("sellers")))
        if not snap.buys_1h:
            snap.buys_1h = int(fnum(h1.get("buys")))
            snap.sells_1h = int(fnum(h1.get("sells")))
        m5 = txns.get("m5") or {}
        if not snap.buys_5m:
            snap.buys_5m = int(fnum(m5.get("buys")))
            snap.sells_5m = int(fnum(m5.get("sells")))
        snap.source = (snap.source + "+geckoterminal").strip("+")
        return snap

    async def geckoterminal_micro(self, pool_address: str) -> Optional[MicroStats]:
        if not pool_address:
            return None
        try:
            data = await self.http.get_json(
                "{}/networks/solana/pools/{}/trades".format(
                    GECKOTERMINAL, urllib.parse.quote(pool_address)),
                params={"trade_volume_in_usd_greater_than": 1}, ttl=15)
        except HttpError:
            return None
        rows = (data or {}).get("data") or []
        now = time.time()
        stats = MicroStats(ts=now)
        oldest = now
        buyers_5m, sellers_5m, buyers_1h, sellers_1h = set(), set(), set(), set()
        for row in rows:
            attrs = (row or {}).get("attributes") or {}
            ts = _iso_to_ts(attrs.get("block_timestamp"))
            if ts <= 0:
                continue
            oldest = min(oldest, ts)
            age = now - ts
            kind = str(attrs.get("kind") or "").lower()
            vol = fnum(attrs.get("volume_in_usd"))
            who = str(attrs.get("tx_from_address") or "")
            stats.trades_seen += 1
            px = fnum(attrs.get("price_to_in_usd")) if kind == "buy" else fnum(attrs.get("price_from_in_usd"))
            if px <= 0:
                px = fnum(attrs.get("price_from_in_usd")) or fnum(attrs.get("price_to_in_usd"))
            if px > 0 and (ts >= stats.ts or stats.last_price <= 0):
                stats.last_price = px
            if age <= 3600:
                if kind == "buy":
                    stats.buys_1h += 1
                    stats.buy_vol_1h += vol
                    if who:
                        buyers_1h.add(who)
                elif kind == "sell":
                    stats.sells_1h += 1
                    stats.sell_vol_1h += vol
                    if who:
                        sellers_1h.add(who)
            if age <= 300:
                if kind == "buy":
                    stats.buys_5m += 1
                    stats.buy_vol_5m += vol
                    if who:
                        buyers_5m.add(who)
                elif kind == "sell":
                    stats.sells_5m += 1
                    stats.sell_vol_5m += vol
                    if who:
                        sellers_5m.add(who)
        stats.buyers_5m = len(buyers_5m)
        stats.sellers_5m = len(sellers_5m)
        stats.buyers_1h = len(buyers_1h)
        stats.sellers_1h = len(sellers_1h)
        stats.window_covered_s = max(0.0, now - oldest)
        stats.ts = now
        return stats

    # ---------------------------------------------------------------- RugCheck
    async def rugcheck_facts(self, mint: str) -> TokenFacts:
        facts = TokenFacts(mint=mint, ts=time.time())
        try:
            d = await self.http.get_json(
                "{}/v1/tokens/{}/report".format(RUGCHECK, urllib.parse.quote(mint)),
                ttl=self.cfg.rugcheck_report_max_age_s)
        except HttpError as exc:
            facts.error = "rugcheck: {}".format(exc.status or "network")
            facts.ts = time.time()
            return facts
        if not isinstance(d, dict):
            facts.error = "rugcheck: empty"
            return facts

        token = d.get("token") or {}
        meta = d.get("tokenMeta") or {}
        facts.symbol = str(meta.get("symbol") or "")[:24]
        facts.decimals = int(fnum(token.get("decimals"), self.cfg.fallback_token_decimals))
        facts.supply = fnum(token.get("supply"))
        facts.mint_authority = token.get("mintAuthority", d.get("mintAuthority"))
        facts.freeze_authority = token.get("freezeAuthority", d.get("freezeAuthority"))
        facts.creator = str(d.get("creator") or "")
        facts.creator_balance_raw = fnum(d.get("creatorBalance"), -1.0)
        facts.rugged = bool(d.get("rugged"))
        facts.score = fnum(d.get("score_normalised", d.get("score")), -1.0)
        facts.total_holders = int(fnum(d.get("totalHolders")))
        facts.risks = [r for r in (d.get("risks") or []) if isinstance(r, dict)]
        facts.danger_risks = [str(r.get("name")) for r in facts.risks
                              if str(r.get("level")).lower() == "danger"]
        facts.warn_risks = [str(r.get("name")) for r in facts.risks
                            if str(r.get("level")).lower() == "warn"]

        # --- top-10 concentration, excluding pool / locker / vault accounts ----
        holders = [h for h in (d.get("topHolders") or []) if isinstance(h, dict)]
        known = d.get("knownAccounts") or {}
        excluded_types = {"amm", "liquidity", "locker", "vault", "market", "pool", "raydium"}
        kept = []
        for h in holders:
            htype = str(h.get("type") or "").lower()
            owner = str(h.get("owner") or "")
            ka_type = str(((known.get(owner) or {}).get("type")) or "").lower()
            if htype in excluded_types or ka_type in excluded_types:
                continue
            if str(h.get("name") or "").lower() in excluded_types:
                continue
            kept.append(h)
        if kept:
            facts.top10_included_holders = min(10, len(kept))
            facts.top10_pct = sum(max(0.0, fnum(h.get("pct"))) for h in kept[:10])
        elif holders:
            # No classification at all: fall back to raw pct but flag it in the log.
            facts.top10_pct = sum(max(0.0, fnum(h.get("pct"))) for h in holders[:10])
            facts.top10_included_holders = min(10, len(holders))

        # --- LP locked: deepest market wins ------------------------------------
        best_pct, best_name, best_liq = -1.0, "", -1.0
        for m in (d.get("markets") or []):
            if not isinstance(m, dict):
                continue
            lp = m.get("lp") or {}
            liq = fnum(lp.get("lpLockedUSD"), 0.0) + fnum(lp.get("lpUnlockedUSD"), 0.0)
            liq = max(liq, fnum(m.get("liquidityUSD")), fnum(m.get("liquidityA")))
            pct = fnum(lp.get("lpLockedPct"), -1.0)
            if pct > best_pct or (pct == best_pct and liq > best_liq):
                best_pct, best_liq = pct, liq
                best_name = str(m.get("marketType") or m.get("pubkey") or "")
        facts.lp_locked_pct = best_pct
        facts.lp_market = best_name

        if facts.supply > 0 and facts.creator_balance_raw >= 0:
            facts.creator_hold_pct = facts.creator_balance_raw / facts.supply * 100.0
        facts.ts = time.time()
        return facts

    # ---------------------------------------------------------------- misc
    async def sol_price_usd(self) -> float:
        if self._sol_price > 0 and time.time() - self._sol_price_ts < 90:
            return self._sol_price
        try:
            q = await self.jupiter_quote(SOL_MINT, USDC_MINT, 1_000_000_000, 50)
            out = fnum((q or {}).get("outAmount"))
            if out > 0:
                self._sol_price = out / 1_000_000.0
                self._sol_price_ts = time.time()
                return self._sol_price
        except HttpError as exc:
            log.debug("SOL price quote failed: %s", exc)
        self._sol_price = self._sol_price or self.cfg.fallback_sol_usd
        return self._sol_price

    async def jupiter_quote(self, input_mint: str, output_mint: str, amount_raw: int,
                            slippage_bps: int) -> Optional[Dict[str, Any]]:
        base = JUPITER_KEYED if os.environ.get("JUPITER_API_KEY") else JUPITER_LITE
        headers = {}
        if os.environ.get("JUPITER_API_KEY"):
            headers["x-api-key"] = os.environ["JUPITER_API_KEY"]
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": int(amount_raw),
            "slippageBps": int(slippage_bps),
            "restrictIntermediateTokens": "true",
        }
        try:
            return await self.http.get_json(base + "/quote", params=params, ttl=3,
                                            retries=1, headers=headers)
        except HttpError as exc:
            log.debug("jupiter quote %s->%s failed: %s", short_addr(input_mint),
                      short_addr(output_mint), exc)
            return None


# =============================================================================
# SECTION 7.  SAFETY FILTERS  (pure functions: easy to test, hard to cheat)
# =============================================================================

def evaluate_safety(snap: Optional[MarketSnapshot], facts: Optional[TokenFacts],
                    sell_quote: Optional[Dict[str, Any]], cfg: Config,
                    now: Optional[float] = None) -> SafetyResult:
    now = now or time.time()
    checks: List[Check] = []

    def add(cid: str, ok: Optional[bool], detail: str, critical: bool = True) -> None:
        checks.append(Check(cid, ok, detail, critical))

    if snap is None:
        add("market", False, "no DexScreener/GeckoTerminal pair data")
    else:
        age = snap.age_minutes(now)
        add("age_min", None if age < 0 else age >= cfg.min_token_age_min,
            "pair age {}m (min {})".format("{:.1f}".format(age) if age >= 0 else "?", cfg.min_token_age_min))
        add("age_max", None if age < 0 else age <= cfg.max_pair_age_hours * 60.0,
            "pair age {}m (max {})".format("{:.1f}".format(age) if age >= 0 else "?",
                                          int(cfg.max_pair_age_hours * 60)))
        add("liquidity", snap.liquidity_usd >= cfg.min_liquidity_usd,
            "liquidity {} (min {})".format(fmt_usd(snap.liquidity_usd), fmt_usd(cfg.min_liquidity_usd)))
        add("price", snap.price_usd > 0, "price {}".format(snap.price_usd))

        # ">5x since launch, too late" - the only honest proxy is the 24h change.
        add("not_too_late", snap.price_change_24h <= cfg.max_gain_since_launch_pct,
            "24h change {} (max +{:.0f}%)".format(fmt_pct(snap.price_change_24h),
                                                  cfg.max_gain_since_launch_pct))

        # honeypot sign #1: sells far fewer than buys
        total_1h = snap.buys_1h + snap.sells_1h
        if total_1h >= cfg.min_buys_for_honeypot_check and snap.buys_1h > 0:
            ratio = snap.sells_1h / float(snap.buys_1h)
            add("sell_ratio", ratio >= cfg.min_sell_buy_txn_ratio,
                "1h sells/buys {:.2f} (min {:.2f}, n={})".format(ratio, cfg.min_sell_buy_txn_ratio, total_1h))
        else:
            add("sell_ratio", None, "not enough 1h trades ({} < {})".format(total_1h,
                                                                           cfg.min_buys_for_honeypot_check))

        # never chase a vertical candle
        add("not_chasing", snap.price_change_5m <= cfg.max_5m_pump_pct,
            "5m change {} (max +{:.0f}%)".format(fmt_pct(snap.price_change_5m), cfg.max_5m_pump_pct))

    if facts is None or not facts.available:
        add("rugcheck", False, (facts.error if facts else "no rugcheck report"))
    else:
        add("rugged", not facts.rugged, "rugged={}".format(facts.rugged))
        add("mint_authority", facts.mint_authority in (None, ""),
            "mintAuthority={}".format(facts.mint_authority or "revoked"))
        add("freeze_authority", facts.freeze_authority in (None, ""),
            "freezeAuthority={}".format(facts.freeze_authority or "revoked"))
        add("top10", None if facts.top10_pct < 0 else facts.top10_pct <= cfg.max_top10_pct,
            "top10 {:.1f}% of supply (max {:.0f}%, n={})".format(
                facts.top10_pct, cfg.max_top10_pct, facts.top10_included_holders))
        add("lp_locked", None if facts.lp_locked_pct < 0 else facts.lp_locked_pct >= cfg.min_lp_locked_pct,
            "LP locked {:.1f}% (min {:.0f}%)".format(facts.lp_locked_pct, cfg.min_lp_locked_pct))
        add("creator_hold", None if facts.creator_hold_pct < 0 else facts.creator_hold_pct <= cfg.max_creator_hold_pct,
            "creator still holds {:.1f}% (max {:.0f}%) [original allocation is not public]".format(
                facts.creator_hold_pct, cfg.max_creator_hold_pct))
        if cfg.max_rugcheck_score > 0:
            add("rug_score", None if facts.score < 0 else facts.score <= cfg.max_rugcheck_score,
                "RugCheck score {:.0f}/100 (max {:.0f})".format(facts.score, cfg.max_rugcheck_score))
        if cfg.reject_on_rugcheck_danger:
            add("rug_danger", not facts.danger_risks,
                "danger flags: {}".format(", ".join(facts.danger_risks[:4]) or "none"))

    # honeypot sign #2: a real $2 sell route must exist and not be a cliff
    if sell_quote is None:
        add("sell_route", None, "no Jupiter sell quote")
    else:
        impact = abs(fnum(sell_quote.get("priceImpactPct")))
        add("sell_route", impact * 100.0 <= cfg.max_entry_price_impact_pct,
            "simulated sell impact {:.2f}% (max {:.2f}%)".format(
                impact * 100.0, cfg.max_entry_price_impact_pct))

    failing = [c for c in checks if c.ok is False]
    unknown = [c for c in checks if c.ok is None]
    if cfg.strict_unknown:
        failing += [c for c in unknown if c.critical]
    passed = not failing
    reasons = ["{}: {}".format(c.cid, c.detail) for c in failing]
    return SafetyResult(passed=passed, checks=checks, reasons=reasons)


# =============================================================================
# SECTION 8.  MOMENTUM SCORING
# =============================================================================

def momentum_score(snap: Optional[MarketSnapshot], micro: Optional[MicroStats],
                   cfg: Config) -> MomentumResult:
    """
    Score 0..100 from buy/sell pressure, volume acceleration and unique-buyer
    growth on 5m and 1h windows. Missing components are dropped and the
    remaining weights renormalised, so a thin tape cannot fake a high score.
    """
    comps: Dict[str, float] = {}
    weights: Dict[str, float] = {}
    notes: List[str] = []
    veto = ""

    buys5, sells5 = 0, 0
    buyvol5 = sellvol5 = 0.0
    buyers5 = buyers1h = 0
    if micro and micro.trades_seen:
        buys5, sells5 = micro.buys_5m, micro.sells_5m
        buyvol5, sellvol5 = micro.buy_vol_5m, micro.sell_vol_5m
        buyers5, buyers1h = micro.buyers_5m, micro.buyers_1h
    elif snap:
        buys5, sells5 = snap.buys_5m, snap.sells_5m
        buyers1h = snap.buyers_1h
        buyers5 = 0

    total5 = buys5 + sells5
    if total5 >= 4:
        ratio = buys5 / float(total5)
        comps["buy_ratio_5m"] = clip(50.0 + (ratio - 0.5) * 160.0, 0.0, 100.0)
        weights["buy_ratio_5m"] = 0.24
        notes.append("5m buys {}/{}".format(buys5, total5))
    else:
        notes.append("5m tape too thin ({} trades)".format(total5))

    # volume acceleration: last 5m annualised to 1h vs the 1h average
    if snap and snap.volume_1h > 0 and snap.volume_5m > 0:
        accel = (snap.volume_5m * 12.0) / snap.volume_1h
        comps["vol_accel"] = clip(accel * 50.0, 0.0, 100.0)
        weights["vol_accel"] = 0.20
        notes.append("vol accel x{:.2f}".format(accel))

    # unique buyers rising: 5m buyers annualised vs 1h unique buyers
    if buyers1h > 0 and buyers5 > 0:
        growth = (buyers5 * 12.0) / buyers1h
        comps["buyer_growth"] = clip(growth * 50.0, 0.0, 100.0)
        weights["buyer_growth"] = 0.18
        notes.append("unique buyers 5m {} vs 1h {} (x{:.2f})".format(buyers5, buyers1h, growth))

    # buy-side dollar pressure in the last 5 minutes
    if buyvol5 + sellvol5 > 0:
        r = buyvol5 / (buyvol5 + sellvol5)
        comps["buy_vol_pressure"] = clip(50.0 + (r - 0.5) * 160.0, 0.0, 100.0)
        weights["buy_vol_pressure"] = 0.18
        notes.append("5m buy$ {:.0f} / sell$ {:.0f}".format(buyvol5, sellvol5))

    # 1h trend (low weight: the entry rules already forbid chasing)
    if snap:
        comps["trend_1h"] = clip((snap.price_change_1h + 20.0) / 60.0 * 100.0, 0.0, 100.0)
        weights["trend_1h"] = 0.10
        # liquidity quality: deeper is better
        liq = max(1.0, snap.liquidity_usd)
        comps["liq_quality"] = clip((math.log10(liq) - 4.3) / 1.4 * 100.0, 0.0, 100.0)
        weights["liq_quality"] = 0.10
        if snap.price_change_5m > cfg.max_5m_pump_pct:
            veto = "chasing_pump"

    if micro and micro.sell_vol_5m > 0 and micro.buy_vol_5m > 0:
        if micro.total_5m >= 10 and micro.sell_vol_5m > micro.buy_vol_5m * 2.0:
            veto = veto or "sell_wall"

    # an unvetoed but empty score must not pass the threshold by accident
    if not comps:
        return MomentumResult(0.0, {}, {}, veto or "no_data", notes)

    wsum = sum(weights.values())
    score = sum(comps[k] * weights[k] for k in comps) / wsum if wsum > 0 else 0.0
    return MomentumResult(round(score, 2), comps, weights, veto, notes)


# =============================================================================
# SECTION 9.  EXIT RULES (take profit / trailing / stops)
# =============================================================================
#
# Honest note: you cannot sell at the exact peak. Nobody can, not with a bot and
# not by hand. This module sells a fixed ladder into strength and then trails the
# remainder. The trailing stop is the practical approximation of "near the peak":
# it exits 10-15% below the highest price seen, never at the top.
# =============================================================================

def decide_exit(pos: Position, snap: Optional[MarketSnapshot], micro: Optional[MicroStats],
                facts: Optional[TokenFacts], now: float, cfg: Config) -> Optional[ExitDecision]:
    price = 0.0
    if snap and snap.price_usd > 0:
        price = snap.price_usd
    elif pos.last_price > 0:
        price = pos.last_price
    if price <= 0 or pos.tokens <= 0:
        return None

    def qty_for(fraction_of_initial: float) -> float:
        return min(pos.tokens, pos.initial_tokens * fraction_of_initial)

    pnl = pos.pnl_pct(price)
    mfe = (max(pos.peak_price, price) / pos.entry_price - 1.0) * 100.0 if pos.entry_price > 0 else 0.0

    # ---------------- emergencies first: capital preservation beats everything
    if snap and pos.entry_liquidity_usd > 0:
        # Zero reported liquidity = the pool was drained. That is a 100% crash.
        drop = 100.0 if snap.liquidity_usd <= 0 else \
            (1.0 - snap.liquidity_usd / pos.entry_liquidity_usd) * 100.0
        if drop >= cfg.emergency_liq_drop_pct:
            return ExitDecision(pos.tokens, "emergency", "liquidity_crash {:.0f}%".format(drop), True)

    if facts and facts.available and pos.creator_baseline_raw > 0:
        if facts.creator_balance_raw >= 0 and \
                facts.creator_balance_raw < pos.creator_baseline_raw * (1.0 - cfg.emergency_creator_dump_pct / 100.0):
            drop = (1.0 - facts.creator_balance_raw / pos.creator_baseline_raw) * 100.0
            return ExitDecision(pos.tokens, "emergency", "creator_dump {:.0f}% of balance".format(drop), True)

    if facts and facts.available and facts.rugged:
        return ExitDecision(pos.tokens, "emergency", "rugcheck_marked_rugged", True)

    if micro and micro.total_5m >= 8:
        if micro.sell_vol_5m >= cfg.emergency_min_volume_usd and \
                micro.sell_vol_5m > max(micro.buy_vol_5m, 1.0) * cfg.emergency_sell_buy_ratio:
            return ExitDecision(pos.tokens, "emergency",
                                "sell_volume_spike {:.0f} vs {:.0f}".format(
                                    micro.sell_vol_5m, micro.buy_vol_5m), True)

    # ---------------- hard stop loss
    if pnl <= cfg.stop_loss_pct:
        return ExitDecision(pos.tokens, "stop_loss", "stop_loss {:.1f}%".format(pnl))

    # ---------------- take-profit ladder
    if not pos.tp2_done and pnl >= cfg.tp2_pct:
        return ExitDecision(qty_for(cfg.tp2_frac), "take_profit",
                            "tp2 +{:.0f}% sell {:.0f}% of initial".format(cfg.tp2_pct, cfg.tp2_frac * 100),
                            tag="tp2")
    if not pos.tp1_done and pnl >= cfg.tp1_pct:
        return ExitDecision(qty_for(cfg.tp1_frac), "take_profit",
                            "tp1 +{:.0f}% sell {:.0f}% of initial".format(cfg.tp1_pct, cfg.tp1_frac * 100),
                            tag="tp1")

    # ---------------- trailing stop on the remainder
    if mfe >= cfg.trail_start_pct:
        trail = cfg.trail_tight_pct if mfe >= cfg.trail_tight_at_pct else cfg.trail_pct
        peak = max(pos.peak_price, price)
        if peak > 0 and price <= peak * (1.0 - trail / 100.0):
            return ExitDecision(pos.tokens, "trailing_stop",
                                "trailing {:.0f}% from peak {:.10g} (now {:.10g})".format(trail, peak, price))

    # ---------------- time stop: flat for two hours is a dead trade
    age_h = pos.age_s(now) / 3600.0
    if age_h >= cfg.time_stop_hours and mfe < cfg.time_stop_mfe_pct:
        return ExitDecision(pos.tokens, "time_stop",
                            "sideways {:.1f}h, best excursion {:.1f}%".format(age_h, mfe))

    return None


# =============================================================================
# SECTION 10.  PORTFOLIO
# =============================================================================

class Portfolio:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = asyncio.Lock()
        self.cash_usd = cfg.start_capital_usd
        self.start_capital_usd = cfg.start_capital_usd
        self.realized_usd = 0.0
        self.fees_usd = 0.0
        self.positions: Dict[str, Position] = {}
        self.closed: List[Position] = []
        self.wins = 0
        self.losses = 0
        self.trades = 0
        self._seq = 0

    def open_positions(self) -> List[Position]:
        return [p for p in self.positions.values() if p.open]

    def open_mints(self) -> set:
        return {p.mint for p in self.positions.values() if p.open}

    async def can_afford(self, usd: float, fee_usd: float = 0.0) -> bool:
        async with self.lock:
            return self.cash_usd >= usd + fee_usd - 1e-9

    async def buy_fill(self, mint: str, symbol: str, fill: Fill, snap: Optional[MarketSnapshot],
                       agent: str, source: str, facts: Optional[TokenFacts]) -> Position:
        async with self.lock:
            self._seq += 1
            self.cash_usd -= (fill.usd_value + fill.fee_usd)
            self.fees_usd += fill.fee_usd
            self.trades += 1
            pos = Position(
                pos_id="{}-{}".format(int(time.time()), self._seq),
                mint=mint, symbol=symbol or short_addr(mint),
                opened_ts=time.time(),
                entry_price=fill.price,
                entry_cost_usd=fill.usd_value,
                initial_tokens=fill.qty_tokens,
                entry_fee_usd=fill.fee_usd,
                tokens=fill.qty_tokens,
                peak_price=fill.price,
                peak_ts=time.time(),
                entry_liquidity_usd=snap.liquidity_usd if snap else 0.0,
                creator_baseline_raw=(facts.creator_balance_raw if facts and facts.available else -1.0),
                last_price=fill.price,
                last_price_ts=time.time(),
                snap=snap, facts=facts, source=source, agent=agent,
            )
            self.positions[pos.pos_id] = pos
            return pos

    async def sell_fill(self, pos: Position, fill: Fill) -> float:
        """Apply a (partial) sell. Returns realized PnL in USD."""
        async with self.lock:
            qty = min(fill.qty_tokens, pos.tokens)
            cost = pos.entry_price * qty
            # The entry fee is part of the cost basis: without it, per-trade PnL
            # would not reconcile with the cash balance.
            entry_fee_part = (pos.entry_fee_usd * qty / pos.initial_tokens
                              if pos.initial_tokens > 0 else 0.0)
            pnl = fill.usd_value - cost - fill.fee_usd - entry_fee_part
            pos.tokens -= qty
            pos.realized_usd += pnl
            pos.fees_usd += fill.fee_usd
            pos.last_price = fill.price
            pos.last_price_ts = time.time()
            self.cash_usd += fill.usd_value - fill.fee_usd
            self.realized_usd += pnl
            self.fees_usd += fill.fee_usd
            return pnl

    async def close_position(self, pos: Position, reason: str) -> None:
        async with self.lock:
            pos.open = False
            pos.closed_ts = time.time()
            pos.exit_reason = reason
            pos.locked = False
            self.positions.pop(pos.pos_id, None)
            self.closed.append(pos)
            if pos.realized_usd > 0:
                self.wins += 1
            else:
                self.losses += 1

    def equity(self) -> float:
        eq = self.cash_usd
        for p in self.positions.values():
            eq += p.tokens * (p.last_price or p.entry_price)
        return eq

    def win_rate(self) -> float:
        total = self.wins + self.losses
        return (self.wins / total * 100.0) if total else 0.0


# =============================================================================
# SECTION 11.  RISK MANAGER (one agent, absolute veto)
# =============================================================================

class RiskManager:
    def __init__(self, cfg: Config, portfolio: Portfolio, ledger: Ledger):
        self.cfg = cfg
        self.pf = portfolio
        self.ledger = ledger
        self.day = day_key()
        self.daily_realized_usd = 0.0
        self.halted = False
        self.halt_reason = ""
        self.halt_until_day = ""
        self.cooldowns: Dict[str, float] = {}      # mint -> unix ts before which we skip it
        self.last_exit: Dict[str, float] = {}
        self.lock = asyncio.Lock()
        self.veto_counts: Dict[str, int] = {}

    # -- day rollover ------------------------------------------------------
    def rollover(self, now: Optional[float] = None) -> None:
        today = day_key(now)
        if today != self.day:
            log.info("UTC day rollover %s -> %s; daily loss counter reset", self.day, today)
            self.day = today
            self.daily_realized_usd = 0.0
            if self.halted and self.halt_until_day and self.halt_until_day <= today:
                self.halted = False
                self.halt_reason = ""
                self.halt_until_day = ""

    # -- entries -----------------------------------------------------------
    async def can_open(self, mint: str) -> Tuple[bool, str]:
        async with self.lock:
            self.rollover()
            if self.halted:
                return False, "halted: {}".format(self.halt_reason or "risk manager")
            open_mints = self.pf.open_mints()
            if mint in open_mints:
                return False, "already_open (never average down)"
            if len(open_mints) >= self.cfg.max_open_positions:
                return False, "max_positions {} reached".format(self.cfg.max_open_positions)
            until = self.cooldowns.get(mint, 0.0)
            if until > time.time():
                return False, "cooldown {:.0f}s".format(until - time.time())
            if self.daily_realized_usd <= -abs(self.cfg.daily_loss_limit_usd):
                self.halted = True
                self.halt_reason = "daily loss limit {} hit".format(fmt_usd(self.cfg.daily_loss_limit_usd))
                self.halt_until_day = self.day
                return False, self.halt_reason
            if not await self.pf.can_afford(self.cfg.max_trade_usd):
                return False, "insufficient cash {}".format(fmt_usd(self.pf.cash_usd))
            return True, "ok"

    async def on_realized(self, pnl_usd: float, mint: str) -> None:
        async with self.lock:
            self.rollover()
            self.daily_realized_usd += pnl_usd
            if pnl_usd < 0:
                self.cooldowns[mint] = time.time() + self.cfg.cooldown_after_exit_s
            if self.daily_realized_usd <= -abs(self.cfg.daily_loss_limit_usd) and not self.halted:
                self.halted = True
                self.halt_reason = "daily loss limit {} hit".format(fmt_usd(self.cfg.daily_loss_limit_usd))
                self.halt_until_day = self.day
                log.warning("RISK: %s - no new entries until the next UTC day", self.halt_reason)
                await self.ledger.record("HALT", agent="risk", reason=self.halt_reason,
                                         detail="daily realized {}".format(fmt_usd(self.daily_realized_usd)))

    async def on_reject(self, mint: str, reason: str) -> None:
        async with self.lock:
            key = reason.split(":")[0].split(" ")[0]
            self.veto_counts[key] = self.veto_counts.get(key, 0) + 1
            if key in ("max_positions",):
                self.cooldowns[mint] = time.time() + 60.0

    def daily_loss_used(self) -> float:
        return max(0.0, -self.daily_realized_usd)


# =============================================================================
# SECTION 12.  EXECUTION LAYER (swappable)
# =============================================================================
#
# BaseExecutor is the seam. PaperExecutor and JupiterExecutor implement it. If
# you ever get a FOMO (or any other venue) API, write a third class - do not
# touch the swarm, the risk manager or the exit logic.
# =============================================================================

class BaseExecutor:
    name = "base"

    async def buy(self, mint: str, usd_amount: float, ref_price: float,
                  decimals: int, liquidity_usd: float) -> Fill:
        raise NotImplementedError

    async def sell(self, pos: Position, qty_tokens: float, ref_price: float,
                   decimals: int, liquidity_usd: float, emergency: bool) -> Fill:
        raise NotImplementedError

    async def balance_usd(self) -> float:
        raise NotImplementedError

    async def startup_checks(self) -> None:
        return None


class PaperExecutor(BaseExecutor):
    """
    Realistic paper fills:
      * price impact modelled as trade_usd / (liquidity_usd / 2)
      * configurable latency slippage on top
      * Solana network fee + priority fee + token-account rent in USD
      * refuses the fill when impact+slippage exceeds the hard cap
    """
    name = "paper"

    def __init__(self, cfg: Config, portfolio: Portfolio, sources: Sources):
        self.cfg = cfg
        self.pf = portfolio
        self.sources = sources
        self.rent_charged: set = set()

    def _impact_bps(self, usd: float, liquidity_usd: float) -> float:
        if liquidity_usd <= 0:
            return 10_000.0
        return 10_000.0 * usd / max(liquidity_usd * 0.5, 1.0)

    async def buy(self, mint: str, usd_amount: float, ref_price: float,
                  decimals: int, liquidity_usd: float) -> Fill:
        if ref_price <= 0:
            return Fill(ok=False, error="no reference price")
        sol_usd = await self.sources.sol_price_usd()
        sol_fee = (self.cfg.sol_network_fee_sol + self.cfg.priority_fee_sol) * sol_usd
        rent = 0.0 if mint in self.rent_charged else self.cfg.ata_rent_sol * sol_usd
        impact = self._impact_bps(usd_amount, liquidity_usd)
        slip = self.cfg.paper_entry_slip_bps + impact
        if slip > self.cfg.entry_slip_cap_bps:
            return Fill(ok=False, error="slippage cap: {:.0f}bps > {}bps".format(
                slip, self.cfg.entry_slip_cap_bps))
        fee = sol_fee + rent
        if not await self.pf.can_afford(usd_amount, fee):
            return Fill(ok=False, error="insufficient paper cash")
        price = ref_price * (1.0 + slip / 10_000.0)
        self.rent_charged.add(mint)
        return Fill(ok=True, price=price, qty_tokens=usd_amount / price, usd_value=usd_amount,
                    fee_usd=fee, slippage_bps=slip,
                    detail="impact {:.0f}bps + slip {}bps + fee {} (incl rent {})".format(
                        impact, self.cfg.paper_entry_slip_bps, fmt_usd(fee), fmt_usd(rent)))

    async def sell(self, pos: Position, qty_tokens: float, ref_price: float,
                   decimals: int, liquidity_usd: float, emergency: bool) -> Fill:
        if ref_price <= 0 or qty_tokens <= 0:
            return Fill(ok=False, error="no price/qty")
        sol_usd = await self.sources.sol_price_usd()
        fee = (self.cfg.sol_network_fee_sol + self.cfg.priority_fee_sol) * sol_usd
        usd = qty_tokens * ref_price
        base_slip = (self.cfg.paper_emergency_slip_bps if emergency else self.cfg.paper_exit_slip_bps)
        slip = base_slip + self._impact_bps(usd, liquidity_usd)
        cap = self.cfg.emergency_slip_cap_bps if emergency else self.cfg.entry_slip_cap_bps
        note = ""
        if slip > cap:
            # refusing to sell is worse than paying up: fill at the cap, loudly.
            note = "capped at {}bps (wanted {:.0f}bps)".format(cap, slip)
            slip = cap
        price = ref_price * (1.0 - slip / 10_000.0)
        return Fill(ok=True, price=price, qty_tokens=qty_tokens, usd_value=qty_tokens * price,
                    fee_usd=fee, slippage_bps=slip,
                    detail="impact {}; fee {}".format(note or "modelled", fmt_usd(fee)))


class JupiterExecutor(BaseExecutor):
    """
    LIVE Solana swaps through Jupiter's Swap API v1 (lite-api.jup.ag is keyless,
    api.jup.ag needs x-api-key in JUPITER_API_KEY).

    Deliberate safety choices:
      * buys are NOT retried. Ever. A retried buy is how you buy twice.
      * after a confirmation timeout we reconcile against the on-chain token
        balance instead of guessing.
      * the wallet is checked before every buy; ATA rent + fees are reserved.
      * a burner wallet is assumed. A loud warning fires on a big balance.
    """
    name = "jupiter"

    def __init__(self, cfg: Config, sources: Sources, http: HttpClient, ledger: Ledger):
        self.cfg = cfg
        self.sources = sources
        self.http = http
        self.ledger = ledger
        self.rpc_url = os.environ.get("SOLANA_RPC_URL", DEFAULT_RPC).strip() or DEFAULT_RPC
        self.keypair = None
        self.pubkey = ""
        self._solders: Dict[str, Any] = {}
        self._ata_seen: set = set()
        self._warned_balance = False

    # -- SDK loading -------------------------------------------------------
    def _sdk(self) -> Dict[str, Any]:
        if self._solders:
            return self._solders
        try:
            from solders.keypair import Keypair            # type: ignore
            from solders.transaction import VersionedTransaction  # type: ignore
        except ImportError as exc:                          # pragma: no cover
            raise RuntimeError(
                "LIVE mode needs the 'solders' package (Solana transaction signing). "
                "Install it with: pkg install rust binutils libsodium && pip install solders "
                "- or stay in paper mode. Original error: {}".format(exc))
        self._solders = {"Keypair": Keypair,
                         "VersionedTransaction": VersionedTransaction}
        return self._solders

    async def startup_checks(self) -> None:
        sdk = self._sdk()
        secret = os.environ.get("SOLANA_PRIVATE_KEY", "").strip()
        if not secret:
            raise RuntimeError("LIVE mode requires SOLANA_PRIVATE_KEY in the environment "
                               "(base58 secret key of a BURNER wallet).")
        raw = b58decode(secret)
        if len(raw) == 64:
            kp = sdk["Keypair"].from_bytes(raw)
        elif len(raw) == 32:                               # seed only
            kp = sdk["Keypair"].from_seed(raw)
        else:
            raise RuntimeError("SOLANA_PRIVATE_KEY decodes to {} bytes; expected 64 "
                               "(or a 32-byte seed)".format(len(raw)))
        self.keypair = kp
        self.pubkey = str(kp.pubkey())
        sol = await self.balance_sol()
        usd = sol * await self.sources.sol_price_usd()
        log.warning("LIVE wallet %s balance %.6f SOL (~%s). This must be a BURNER wallet.",
                    short_addr(self.pubkey, 6, 6), sol, fmt_usd(usd))
        if usd > 3 * max(self.cfg.start_capital_usd, 10.0) and not self._warned_balance:
            self._warned_balance = True
            log.warning("LIVE wallet holds %s but this bot is designed for %s. "
                        "A bug here is a real loss. Consider moving funds out first.",
                        fmt_usd(usd), fmt_usd(self.cfg.start_capital_usd))
        if sol < self.cfg.min_wallet_buffer_sol:
            raise RuntimeError("wallet has {:.6f} SOL, below the {:.6f} SOL buffer needed "
                               "for fees and rent".format(sol, self.cfg.min_wallet_buffer_sol))

    # -- RPC ---------------------------------------------------------------
    async def _rpc(self, method: str, params: List[Any], retries: int = 2) -> Any:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        data = await self.http.post_json(self.rpc_url, payload, ttl=0.0, retries=retries)
        if not isinstance(data, dict):
            raise HttpError(0, self.rpc_url, "bad RPC response")
        if data.get("error"):
            raise HttpError(0, self.rpc_url, "RPC error: {}".format(str(data["error"])[:200]))
        return data.get("result")

    async def balance_sol(self) -> float:
        res = await self._rpc("getBalance", [self.pubkey, {"commitment": "confirmed"}])
        return fnum((res or {}).get("value")) / 1e9

    async def balance_usd(self) -> float:
        return await self.balance_sol() * await self.sources.sol_price_usd()

    async def token_balance_raw(self, mint: str) -> int:
        sdk = self._sdk()
        res = await self._rpc("getTokenAccountsByOwner", [
            self.pubkey,
            {"mint": mint},
            {"encoding": "jsonParsed", "commitment": "confirmed"},
        ])
        total = 0
        for acc in (res or {}).get("value") or []:
            try:
                amt = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]
                total += int(amt)
            except (KeyError, TypeError, ValueError):
                continue
        return total

    # -- swap plumbing -----------------------------------------------------
    async def _quote(self, input_mint: str, output_mint: str, amount_raw: int,
                     slippage_bps: int) -> Dict[str, Any]:
        q = await self.sources.jupiter_quote(input_mint, output_mint, amount_raw, slippage_bps)
        if not q or not q.get("outAmount"):
            raise HttpError(0, "jupiter quote", "no route")
        return q

    async def _swap_and_send(self, quote: Dict[str, Any]) -> str:
        base = JUPITER_KEYED if os.environ.get("JUPITER_API_KEY") else JUPITER_LITE
        headers = {}
        if os.environ.get("JUPITER_API_KEY"):
            headers["x-api-key"] = os.environ["JUPITER_API_KEY"]
        body = {
            "quoteResponse": quote,
            "userPublicKey": self.pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": {
                "priorityLevelWithMaxLamports": {
                    "priorityLevel": "high",
                    "maxLamports": int(self.cfg.priority_fee_sol * 1e9),
                }
            },
        }
        swap = await self.http.post_json(base + "/swap", body, ttl=0.0, retries=1, headers=headers)
        tx_b64 = (swap or {}).get("swapTransaction")
        if not tx_b64:
            raise HttpError(0, "jupiter swap", "no swapTransaction in response: {}".format(str(swap)[:200]))
        sdk = self._sdk()
        if self.keypair is None:
            raise RuntimeError("live executor not initialised (startup_checks not run)")
        raw_tx = base64.b64decode(tx_b64)
        unsigned = sdk["VersionedTransaction"].from_bytes(raw_tx)
        signed = sdk["VersionedTransaction"](unsigned.message, [self.keypair])
        sig = str(signed.signatures[0])
        encoded = base64.b64encode(bytes(signed)).decode("ascii")
        await self._rpc("sendTransaction", [encoded, {
            "encoding": "base64",
            "skipPreflight": False,
            "preflightCommitment": "confirmed",
            "maxRetries": 0,
        }], retries=0)          # ONE attempt. The tx is never re-signed or re-sent.
        return sig

    async def _confirm(self, sig: str, timeout_s: Optional[float] = None) -> Tuple[bool, str]:
        deadline = time.time() + (timeout_s or self.cfg.live_confirm_timeout_s)
        while time.time() < deadline:
            try:
                res = await self._rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
                value = (res or {}).get("value") or []
                if value and value[0]:
                    st = value[0]
                    if st.get("err"):
                        return False, "tx failed on-chain: {}".format(str(st.get("err"))[:160])
                    if st.get("confirmationStatus") in ("confirmed", "finalized"):
                        return True, "confirmed"
            except HttpError as exc:
                log.debug("confirm poll error: %s", exc)
            await asyncio.sleep(1.8)
        return False, "confirmation timeout"

    def _sol_for_usd(self, usd: float, sol_usd: float) -> int:
        if sol_usd <= 0:
            return 0
        return max(0, int(usd / sol_usd * 1e9))

    async def buy(self, mint: str, usd_amount: float, ref_price: float,
                  decimals: int, liquidity_usd: float) -> Fill:
        sol_usd = await self.sources.sol_price_usd()
        lamports = self._sol_for_usd(usd_amount, sol_usd)
        rent_lamports = int(self.cfg.ata_rent_sol * 1e9) if mint not in self._ata_seen else 0
        needed = (lamports + rent_lamports +
                  int((self.cfg.sol_network_fee_sol + self.cfg.priority_fee_sol) * 1e9))
        sol_balance = await self.balance_sol()
        if sol_balance * 1e9 < needed + self.cfg.min_wallet_buffer_sol * 1e9:
            return Fill(ok=False, error="insufficient SOL: have {:.6f}, need {:.6f}".format(
                sol_balance, needed / 1e9 + self.cfg.min_wallet_buffer_sol))
        try:
            quote = await self._quote(SOL_MINT, mint, lamports,
                                      min(self.cfg.jupiter_slippage_bps, self.cfg.entry_slip_cap_bps))
        except HttpError as exc:
            return Fill(ok=False, error="no route: {}".format(exc))
        impact_pct = abs(fnum(quote.get("priceImpactPct"))) * 100.0
        if impact_pct > self.cfg.max_entry_price_impact_pct:
            return Fill(ok=False, error="price impact {:.2f}% > cap".format(impact_pct))
        out_raw = int(fnum(quote.get("outAmount")))
        if out_raw <= 0:
            return Fill(ok=False, error="quote outAmount=0")
        # ---- single attempt, no retry (see class docstring) ------------------
        try:
            sig = await self._swap_and_send(quote)
        except (HttpError, RuntimeError, ValueError) as exc:
            return Fill(ok=False, error="swap build/send failed: {}".format(str(exc)[:180]))
        ok, why = await self._confirm(sig)
        if not ok:
            # Reconcile instead of retrying: did the tokens actually arrive?
            try:
                got = await self.token_balance_raw(mint)
            except HttpError:
                got = 0
            if got > 0:
                self._ata_seen.add(mint)
                price = usd_amount / (got / (10 ** decimals)) if got else ref_price
                return Fill(ok=True, price=price, qty_tokens=got / (10 ** decimals),
                            usd_value=usd_amount, fee_usd=(self.cfg.sol_network_fee_sol +
                                                           self.cfg.priority_fee_sol) * sol_usd,
                            tx_sig=sig, detail="landed but unconfirmed ({})".format(why))
            return Fill(ok=False, unconfirmed=True, tx_sig=sig,
                        error="buy unconfirmed ({}), token balance 0 - NOT retrying".format(why))
        self._ata_seen.add(mint)
        qty = out_raw / (10 ** decimals)
        fee = (self.cfg.sol_network_fee_sol + self.cfg.priority_fee_sol) * sol_usd
        price = usd_amount / qty if qty > 0 else ref_price
        return Fill(ok=True, price=price, qty_tokens=qty, usd_value=usd_amount,
                    fee_usd=fee, slippage_bps=impact_pct * 100.0, tx_sig=sig, detail="confirmed")

    async def sell(self, pos: Position, qty_tokens: float, ref_price: float,
                   decimals: int, liquidity_usd: float, emergency: bool) -> Fill:
        sol_usd = await self.sources.sol_price_usd()
        try:
            onchain = await self.token_balance_raw(pos.mint)
        except HttpError:
            onchain = 0
        full = qty_tokens >= pos.tokens * 0.999
        if full and onchain > 0:
            amount_raw = onchain                       # sell exactly what is there
            qty = onchain / (10 ** decimals)
        else:
            amount_raw = int(qty_tokens * (10 ** decimals))
            qty = qty_tokens
        if amount_raw <= 0:
            return Fill(ok=False, error="nothing to sell (on-chain balance 0)")
        cap_bps = self.cfg.emergency_slip_cap_bps if emergency else self.cfg.entry_slip_cap_bps
        try:
            quote = await self._quote(pos.mint, SOL_MINT, amount_raw, cap_bps)
        except HttpError as exc:
            return Fill(ok=False, error="no sell route: {}".format(exc))
        out_lamports = int(fnum(quote.get("outAmount")))
        usd_out_est = out_lamports / 1e9 * sol_usd
        impact_bps = abs(fnum(quote.get("priceImpactPct"))) * 10_000.0
        if impact_bps > cap_bps and not emergency:
            return Fill(ok=False, error="sell impact {:.0f}bps over cap {}bps".format(
                impact_bps, cap_bps))
        try:
            sig = await self._swap_and_send(quote)
        except (HttpError, RuntimeError, ValueError) as exc:
            return Fill(ok=False, error="sell failed: {}".format(str(exc)[:180]))
        ok, why = await self._confirm(sig)
        fee = (self.cfg.sol_network_fee_sol + self.cfg.priority_fee_sol) * sol_usd
        if not ok:
            return Fill(ok=False, unconfirmed=True, tx_sig=sig,
                        error="sell unconfirmed ({}), check wallet manually".format(why))
        price = (usd_out_est / qty) if qty > 0 else ref_price
        return Fill(ok=True, price=price, qty_tokens=qty, usd_value=usd_out_est,
                    fee_usd=fee, slippage_bps=impact_bps, tx_sig=sig, detail="confirmed")


# =============================================================================
# SECTION 13.  TELEGRAM (optional, outbound only, never blocks trading)
# =============================================================================

class Telegram:
    def __init__(self, http: HttpClient):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.http = http
        self.enabled = bool(self.token and self.chat_id)
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self.sent = 0
        self.failed = 0

    def notify(self, text: str) -> None:
        if not self.enabled:
            return
        try:
            self.queue.put_nowait(text[:3800])
        except asyncio.QueueFull:
            self.failed += 1

    async def sender_loop(self, stop: asyncio.Event) -> None:
        if not self.enabled:
            return
        url = "https://api.telegram.org/bot{}/sendMessage".format(self.token)
        while not stop.is_set():
            try:
                text = await asyncio.wait_for(self.queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self.http.post_json(url, {"chat_id": self.chat_id, "text": text,
                                                "disable_web_page_preview": True},
                                          ttl=0.0, retries=0)
                self.sent += 1
            except HttpError as exc:
                self.failed += 1
                log.debug("telegram send failed: HTTP %s", exc.status)
            await asyncio.sleep(0.6)


# =============================================================================
# SECTION 14.  THE SWARM  (100 asyncio tasks, one pipeline)
# =============================================================================
#
#   scouts            safety             momentum           executors
#   ------            ------             --------           ---------
#   poll feeds   -->  enrich + filter -> score 5m/1h  -->  risk check -> buy
#   24 agents         24 agents          22 agents          29 agents
#                                                              |
#                                                        manage exits
#                                         risk manager: 1 agent, absolute veto
# =============================================================================

FEEDS: List[Tuple[str, float]] = [
    ("dexscreener_profiles", 45.0),
    ("dexscreener_boosts", 90.0),
    ("geckoterminal_new_pools", 60.0),
    ("geckoterminal_trending", 120.0),
    ("coingecko_recent", 180.0),
]

SOURCE_LIST = [
    "DexScreener  /token-profiles/latest/v1, /token-boosts/latest/v1, /token-pairs/v1/solana/{mint}",
    "GeckoTerminal /networks/solana/new_pools, /trending_pools, /tokens/{mint}/pools, /pools/{pool}/trades",
    "CoinGecko    /coins/list/new (key only) -> keyless fallback: GeckoTerminal /tokens/info_recently_updated",
    "RugCheck     /v1/tokens/{mint}/report (authorities, LP lock, holders, creator, risks)",
    "Jupiter      /swap/v1/quote (honeypot route check in paper; real swaps in live)",
    "Telegram     outbound alerts only (no market data)",
]


class Swarm:
    def __init__(self, cfg: Config, http: HttpClient, sources: Sources, ledger: Ledger,
                 telegram: Telegram, executor: BaseExecutor,
                 portfolio: Optional[Portfolio] = None):
        self.cfg = cfg
        self.http = http
        self.sources = sources
        self.ledger = ledger
        self.tg = telegram
        self.executor = executor
        self.pf = portfolio or Portfolio(cfg)
        self.risk = RiskManager(cfg, self.pf, ledger)
        self.counts = cfg.normalized_agents()
        self.candidates: asyncio.Queue = asyncio.Queue(maxsize=cfg.max_queue)
        self.vetted: asyncio.Queue = asyncio.Queue(maxsize=cfg.max_queue)
        self.signals: asyncio.Queue = asyncio.Queue(maxsize=cfg.max_queue)
        self.stop_event = asyncio.Event()
        self.draining = False
        self.shutting_down = False
        self.tasks: List[asyncio.Task] = []
        self.started = time.time()
        self.seen: Dict[str, float] = {}
        self.blocked: Dict[str, float] = {}
        self.feed_state = {name: {"last": 0.0, "lock": asyncio.Lock(), "ok": 0, "err": 0,
                                  "last_err": ""} for name, _ in FEEDS}
        self.entry_lock = asyncio.Lock()
        self.reject_counts: Dict[str, int] = {}
        self.entries = 0
        self.sells = 0
        self.recent_events: List[str] = []
        self.report_written = False

    # ------------------------------------------------------------------ helper
    def note(self, text: str) -> None:
        self.recent_events.append("{} {}".format(utc_str()[11:19], text))
        if len(self.recent_events) > 60:
            del self.recent_events[:-60]

    def count_reject(self, reasons: Sequence[str]) -> None:
        for r in reasons:
            key = r.split(":")[0].strip()
            self.reject_counts[key] = self.reject_counts.get(key, 0) + 1

    # ------------------------------------------------------------------ banner
    def banner(self) -> None:
        mode = "LIVE (REAL MONEY)" if self.cfg.live else "PAPER (simulated fills, no real money)"
        lines = [
            "=" * 78,
            " MEME AGENTS v{}  |  {} agents  |  {}".format(VERSION, sum(self.counts.values()), mode),
            "=" * 78,
            " capital          : {}   mode: {}".format(fmt_usd(self.cfg.start_capital_usd),
                                                         "LIVE" if self.cfg.live else "PAPER"),
            " agent mix        : " + " | ".join("{} {}".format(k, v) for k, v in self.counts.items()),
            " size / exposure  : {} per trade, max {} open ({} exposure)".format(
                fmt_usd(self.cfg.max_trade_usd), self.cfg.max_open_positions,
                fmt_usd(self.cfg.max_trade_usd * self.cfg.max_open_positions)),
            " safety           : liq>={} top10<={:.0f}% mint/freeze revoked, LP>={:.0f}%, "
            "creator<={:.0f}%, age {:.0f}m-{:.0f}h".format(
                fmt_usd(self.cfg.min_liquidity_usd), self.cfg.max_top10_pct,
                self.cfg.min_lp_locked_pct, self.cfg.max_creator_hold_pct,
                self.cfg.min_token_age_min, self.cfg.max_pair_age_hours),
            " exits            : SL {:.0f}% | TP {:.0f}% -> sell {:.0f}% | TP {:.0f}% -> sell {:.0f}% | "
            "trail {:.0f}%->{:.0f}%".format(
                self.cfg.stop_loss_pct, self.cfg.tp1_pct, self.cfg.tp1_frac * 100,
                self.cfg.tp2_pct, self.cfg.tp2_frac * 100, self.cfg.trail_pct, self.cfg.trail_tight_pct),
            " risk             : daily loss stop {} | slippage caps {}/{} bps | kill: Ctrl+C or "
            "create file '{}'".format(fmt_usd(self.cfg.daily_loss_limit_usd),
                                      self.cfg.entry_slip_cap_bps, self.cfg.emergency_slip_cap_bps,
                                      self.cfg.stop_file),
            "-" * 78,
            " DATA SOURCES (only these):",
        ]
        lines += ["   " + s for s in SOURCE_LIST]
        lines += [
            "-" * 78,
            " files            : {} / {}".format(self.cfg.ledger_csv, self.cfg.log_file),
            " honest note      : most meme coins go to zero; you can lose the",
            "                    whole {}.".format(fmt_usd(self.cfg.start_capital_usd)),
            "=" * 78,
        ]
        for line in lines:
            print(line)
        log.info("startup: %s agents, mode=%s", sum(self.counts.values()),
                 "LIVE" if self.cfg.live else "PAPER")

    # ------------------------------------------------------------------ feeds
    async def _call_feed(self, name: str) -> List[Dict[str, Any]]:
        return await {
            "dexscreener_profiles": self.sources.dexscreener_profiles,
            "dexscreener_boosts": self.sources.dexscreener_boosts,
            "geckoterminal_new_pools": self.sources.geckoterminal_new_pools,
            "geckoterminal_trending": self.sources.geckoterminal_trending,
            "coingecko_recent": self.sources.coingecko_recent,
        }[name]()

    async def fetch_feed(self, name: str) -> List[Dict[str, Any]]:
        interval = dict(FEEDS)[name]
        st = self.feed_state[name]
        if time.time() - st["last"] < interval * 0.8:
            return []
        async with st["lock"]:
            if time.time() - st["last"] < interval * 0.8:
                return []
            st["last"] = time.time()
            try:
                rows = await self._call_feed(name)
                st["ok"] += 1
                return rows[: self.cfg.max_candidates_per_feed]
            except HttpError as exc:
                st["err"] += 1
                st["last_err"] = str(exc)[:120]
                log.debug("feed %s failed: %s", name, exc)
                return []

    async def enqueue(self, row: Dict[str, Any]) -> None:
        mint = str(row.get("mint") or "")
        if not mint:
            return
        now = time.time()
        if mint in self.pf.open_mints():
            return
        if self.blocked.get(mint, 0.0) > now:
            return
        last = self.seen.get(mint, 0.0)
        if now - last < self.cfg.seen_ttl_s:
            return
        self.seen[mint] = now
        if len(self.seen) > 20000:
            cutoff = now - self.cfg.seen_ttl_s * 2
            self.seen = {k: v for k, v in self.seen.items() if v > cutoff}
        if len(self.blocked) > 20000:
            self.blocked = {k: v for k, v in self.blocked.items() if v > now}
        try:
            self.candidates.put_nowait(row)
        except asyncio.QueueFull:
            log.debug("candidate queue full, dropping %s", short_addr(mint))

    # ------------------------------------------------------------------ scouts
    async def scout_worker(self, idx: int) -> None:
        name, interval = FEEDS[idx % len(FEEDS)]
        agent = "scout{}:{}".format(idx, name.split(":")[0])
        rng = random.Random(1000 + idx)
        while not self.stop_event.is_set():
            rows = await self.fetch_feed(name)
            for row in rows:
                await self.enqueue(row)
            if rows and self.cfg.verbose_progress:
                log.debug("%s discovered %d", agent, len(rows))
            await self._sleep(rng.uniform(0.6, 1.4) * max(5.0, interval / 3.0))

    # ------------------------------------------------------------------ safety
    async def simulate_sell_quote(self, mint: str, price: float,
                                  facts: Optional[TokenFacts]) -> Optional[Dict[str, Any]]:
        """Honeypot check #2: can $2 actually be sold, and at what impact?"""
        if price <= 0 or facts is None or not facts.available:
            return None
        decimals = facts.decimals
        tokens = self.cfg.honeypot_check_usd / price
        if facts.supply > 0 and tokens > facts.supply * 0.05:
            return None                      # $2 is >5% of supply: check is meaningless
        raw = int(tokens * (10 ** decimals))
        if raw <= 0:
            return None
        return await self.sources.jupiter_quote(mint, SOL_MINT, raw, 300)

    async def safety_worker(self, idx: int) -> None:
        agent = "safety{}".format(idx)
        while not self.stop_event.is_set():
            try:
                cand = await asyncio.wait_for(self.candidates.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            mint = cand["mint"]
            try:
                snap = await self.sources.dexscreener_snapshot(mint)
                if snap is not None:
                    try:
                        pool = await self.sources.geckoterminal_pool_for_token(mint)
                        if pool:
                            snap = self.sources.apply_gt_pool(snap, pool)
                    except HttpError:
                        pass
                facts = await self.sources.rugcheck_facts(mint)
                price = snap.price_usd if snap else 0.0
                sell_quote = await self.simulate_sell_quote(mint, price, facts)
                result = evaluate_safety(snap, facts, sell_quote, self.cfg)
                symbol = (snap.symbol if snap else "") or facts.symbol or short_addr(mint)
                if not result.passed:
                    self.count_reject(result.reasons)
                    await self.ledger.record(
                        "REJECT", agent=agent, mint=mint, symbol=symbol,
                        reason="; ".join(result.reasons)[:300],
                        detail=result.line())
                    self.note("REJECT {} {}".format(symbol, result.reasons[0].split(":")[0]
                                                    if result.reasons else "?"))
                    self.blocked[mint] = time.time() + self.cfg.cooldown_after_reject_s
                    continue
                await self.ledger.record(
                    "PASS_SAFETY", agent=agent, mint=mint, symbol=symbol,
                    price=price, usd=snap.liquidity_usd if snap else 0.0,
                    reason="all critical checks passed", detail=result.line())
                await self.vetted.put({"mint": mint, "symbol": symbol, "snap": snap,
                                       "facts": facts, "sell_quote": sell_quote,
                                       "source": cand.get("source", ""), "agent": agent})
            except asyncio.CancelledError:
                raise
            except Exception as exc:                     # one bad token must not kill a worker
                log.error("%s crashed on %s: %s", agent, short_addr(mint), exc)
                log.debug("%s", traceback.format_exc())
                await self.ledger.record("ERROR", agent=agent, mint=mint,
                                         reason="safety worker exception", detail=str(exc)[:200])

    # ------------------------------------------------------------------ momentum
    async def momentum_worker(self, idx: int) -> None:
        agent = "momentum{}".format(idx)
        while not self.stop_event.is_set():
            try:
                item = await asyncio.wait_for(self.vetted.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            mint = item["mint"]
            snap: Optional[MarketSnapshot] = item["snap"]
            facts: Optional[TokenFacts] = item["facts"]
            try:
                micro = None
                if snap and snap.pool_address:
                    micro = await self.sources.geckoterminal_micro(snap.pool_address)
                res = momentum_score(snap, micro, self.cfg)
                if res.veto:
                    self.count_reject(["momentum_veto: " + res.veto])
                    await self.ledger.record("REJECT", agent=agent, mint=mint,
                                             symbol=item["symbol"],
                                             reason="momentum veto {}".format(res.veto),
                                             detail="; ".join(res.notes)[:300])
                    self.blocked[mint] = time.time() + self.cfg.cooldown_after_reject_s
                    continue
                if res.score < self.cfg.momentum_threshold:
                    self.count_reject(["momentum_low"])
                    await self.ledger.record(
                        "REJECT", agent=agent, mint=mint, symbol=item["symbol"],
                        reason="momentum {:.1f} < threshold {:.1f}".format(
                            res.score, self.cfg.momentum_threshold),
                        detail=" ".join("{}={:.0f}".format(k, v) for k, v in res.components.items()))
                    self.blocked[mint] = time.time() + self.cfg.cooldown_after_reject_s
                    continue
                await self.ledger.record(
                    "SIGNAL", agent=agent, mint=mint, symbol=item["symbol"],
                    price=snap.price_usd if snap else 0.0, reason="momentum {:.1f}".format(res.score),
                    detail=" ".join("{}={:.0f}".format(k, v) for k, v in res.components.items()))
                self.note("SIGNAL {} momentum {:.0f}".format(item["symbol"], res.score))
                await self.signals.put({"mint": mint, "symbol": item["symbol"], "snap": snap,
                                        "facts": facts, "micro": micro, "score": res.score,
                                        "source": item["source"], "agent": agent,
                                        "ts": time.time()})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("%s crashed on %s: %s", agent, short_addr(mint), exc)
                log.debug("%s", traceback.format_exc())

    # ------------------------------------------------------------------ entries
    async def try_open(self, sig: Dict[str, Any], agent: str) -> None:
        mint = sig["mint"]
        symbol = sig["symbol"]
        async with self.entry_lock:                   # one wallet = one entry at a time
            if self.draining or self.stop_event.is_set():
                return
            ok, why = await self.risk.can_open(mint)
            if not ok:
                await self.risk.on_reject(mint, why)
                await self.ledger.record("VETO", agent=agent, mint=mint, symbol=symbol,
                                         reason=why)
                self.note("VETO {} {}".format(symbol, why))
                return
            snap = await self.sources.dexscreener_snapshot(mint) or sig.get("snap")
            facts = await self.sources.rugcheck_facts(mint) or sig.get("facts")
            if snap is None or snap.price_usd <= 0:
                await self.ledger.record("VETO", agent=agent, mint=mint, symbol=symbol,
                                         reason="stale/no price at execution")
                return
            sig_snap = sig.get("snap")
            signal_price = sig_snap.price_usd if isinstance(sig_snap, MarketSnapshot) else 0.0
            if signal_price > 0 and abs(snap.price_usd / signal_price - 1.0) > 0.10:
                await self.ledger.record("VETO", agent=agent, mint=mint, symbol=symbol,
                                         reason="price moved {:.1f}% since signal".format(
                                             (snap.price_usd / signal_price - 1) * 100))
                return
            if time.time() - fnum(sig.get("ts")) > 90:
                await self.ledger.record("VETO", agent=agent, mint=mint, symbol=symbol,
                                         reason="signal older than 90s")
                return
            decimals = facts.decimals if facts else self.cfg.fallback_token_decimals
            fill = await self.executor.buy(mint, self.cfg.max_trade_usd, snap.price_usd,
                                           decimals, snap.liquidity_usd)
            if not fill.ok:
                await self.ledger.record("BUY_FAIL", agent=agent, mint=mint, symbol=symbol,
                                         reason=fill.error, detail=fill.detail)
                self.note("BUY_FAIL {} {}".format(symbol, fill.error[:40]))
                if fill.unconfirmed:
                    # Unknown chain state: stop touching this mint, tell the human.
                    self.blocked[mint] = time.time() + 3600.0
                    self.tg.notify("[{}] BUY UNCONFIRMED {}\n{}\ncheck the wallet manually."
                                   .format("LIVE" if self.cfg.live else "PAPER", symbol, fill.error))
                return
            pos = await self.pf.buy_fill(mint, symbol, fill, snap, agent, sig.get("source", ""), facts)
            pos.micro = sig.get("micro")
            pos.last_micro_ts = time.time()
            pos.last_facts_ts = time.time()
            self.entries += 1
            self.risk.cooldowns[mint] = time.time() + self.cfg.cooldown_after_exit_s
            await self.ledger.record(
                "BUY", agent=agent, mint=mint, symbol=symbol, side="buy",
                qty=fill.qty_tokens, price=fill.price, usd=fill.usd_value, fee_usd=fill.fee_usd,
                reason="momentum {:.0f}".format(fnum(sig.get("score"))),
                detail="slippage {:.0f}bps; {}".format(fill.slippage_bps, fill.detail))
            self.tg.notify("[{}] BUY {} ${:.2f} @ {:.10g}\nmomentum {:.0f} | liq {}\n{}"
                           .format("LIVE" if self.cfg.live else "PAPER", symbol,
                                   fill.usd_value, fill.price, fnum(sig.get("score")),
                                   fmt_usd(snap.liquidity_usd), short_addr(mint)))
            self.note("BUY {} ${:.2f}".format(symbol, fill.usd_value))

    # ------------------------------------------------------------------ exits
    async def claim_position(self) -> Optional[Position]:
        async with self.pf.lock:
            for p in self.pf.positions.values():
                if p.open and not p.locked:
                    p.locked = True
                    return p
        return None

    async def refresh_position(self, pos: Position) -> None:
        now = time.time()
        cfg = self.cfg
        if now - pos.last_price_ts >= cfg.position_refresh_s or pos.snap is None:
            snap = await self.sources.dexscreener_snapshot(pos.mint)
            if snap and snap.price_usd > 0:
                pos.snap = snap
                pos.last_price = snap.price_usd
                pos.last_price_ts = now
                if snap.price_usd > pos.peak_price:
                    pos.peak_price = snap.price_usd
                    pos.peak_ts = now
        if now - pos.last_micro_ts >= cfg.micro_refresh_s:
            pool = pos.snap.pool_address if pos.snap else ""
            if pool:
                micro = await self.sources.geckoterminal_micro(pool)
                if micro:
                    pos.micro = micro
                    pos.last_micro_ts = now
        if now - pos.last_facts_ts >= cfg.facts_refresh_s:
            facts = await self.sources.rugcheck_facts(pos.mint)
            if facts and facts.available:
                pos.facts = facts
                pos.last_facts_ts = now

    async def manage_position(self, pos: Position, agent: str) -> None:
        try:
            await self.refresh_position(pos)
            snap = pos.snap
            decision = decide_exit(pos, snap, pos.micro, pos.facts, time.time(), self.cfg)
            if decision is None:
                return
            price = (snap.price_usd if snap and snap.price_usd > 0 else pos.last_price)
            decimals = pos.facts.decimals if pos.facts else self.cfg.fallback_token_decimals
            liq = snap.liquidity_usd if snap else pos.entry_liquidity_usd
            fill = await self.executor.sell(pos, decision.qty, price, decimals, liq,
                                            decision.emergency)
            if not fill.ok:
                log.warning("SELL FAILED %s: %s", pos.symbol, fill.error)
                await self.ledger.record("SELL_FAIL", agent=agent, mint=pos.mint,
                                         symbol=pos.symbol, reason=fill.error)
                self.note("SELL_FAIL {} {}".format(pos.symbol, fill.error[:40]))
                if fill.unconfirmed:
                    pos.notes.append("sell unconfirmed at {}".format(utc_str()))
                    self.tg.notify("[{}] SELL UNCONFIRMED {}\n{}\ncheck the wallet manually."
                                   .format("LIVE" if self.cfg.live else "PAPER", pos.symbol, fill.error))
                return
            pnl = await self.pf.sell_fill(pos, fill)
            self.sells += 1
            await self.risk.on_realized(pnl, pos.mint)
            event = "EMERGENCY" if decision.emergency else "SELL"
            await self.ledger.record(
                event, agent=agent, mint=pos.mint, symbol=pos.symbol, side="sell",
                qty=fill.qty_tokens, price=fill.price, usd=fill.usd_value, fee_usd=fill.fee_usd,
                pnl_usd=pnl, reason="{}: {}".format(decision.kind, decision.reason),
                detail=fill.detail)
            self.tg.notify("[{}] SELL {}\n{}\n${:.2f} -> pnl ${:+.2f}"
                           .format("LIVE" if self.cfg.live else "PAPER", pos.symbol,
                                   decision.reason, fill.usd_value, pnl))
            self.note("{} {} {}".format(event, pos.symbol, decision.kind))
            if decision.tag == "tp1":
                pos.tp1_done = True
            elif decision.tag == "tp2":
                pos.tp2_done = True
            if pos.tokens <= max(1e-12, pos.initial_tokens * 1e-6):
                total = pos.realized_usd
                await self.pf.close_position(pos, "{}: {}".format(decision.kind, decision.reason))
                await self.ledger.record("CLOSE", agent=agent, mint=pos.mint, symbol=pos.symbol,
                                         pnl_usd=total, reason=pos.exit_reason)
                self.note("CLOSE {} pnl ${:+.2f}".format(pos.symbol, total))
        finally:
            pos.locked = False

    # ------------------------------------------------------------------ executor
    async def executor_worker(self, idx: int) -> None:
        """
        One position per iteration, then yield.

        This used to loop "claim until nothing is claimable". Because
        manage_position() unlocks the position when there is nothing to do, the
        same worker would re-claim the same position forever and - with no
        suspension point on the no-op path - starve the whole event loop (the
        dashboard, the risk manager and every other agent froze for seconds).
        One item per iteration plus an explicit sleep cannot do that.
        """
        agent = "executor{}".format(idx)
        while not self.stop_event.is_set():
            did = False
            try:
                pos = await self.claim_position()
                if pos is not None:
                    did = True
                    await self.manage_position(pos, agent)
                    if self.stop_event.is_set():
                        return
                elif not self.draining:
                    try:
                        sig = self.signals.get_nowait()
                    except asyncio.QueueEmpty:
                        sig = None
                    if sig is not None:
                        did = True
                        await self.try_open(sig, agent)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("%s error: %s", agent, exc)
                log.debug("%s", traceback.format_exc())
                await self.ledger.record("ERROR", agent=agent,
                                         reason="executor exception", detail=str(exc)[:200])
            # Always yield: when nothing happened we idle cheaply, when we did
            # work we give the other 99 agents a turn.
            await asyncio.sleep(0.15 if did else 1.0)

    # ------------------------------------------------------------------ risk
    async def risk_worker(self) -> None:
        agent = "risk"
        while not self.stop_event.is_set():
            self.risk.rollover()
            if os.path.exists(self.cfg.stop_file):
                log.warning("STOP file detected: %s", self.cfg.stop_file)
                await self.request_shutdown("STOP file present", agent)
                return
            if self.cfg.max_runtime_hours and time.time() - self.started >= self.cfg.max_runtime_hours * 3600:
                await self.request_shutdown("max runtime reached", agent)
                return
            if self.cfg.free_run_hours and time.time() - self.started >= self.cfg.free_run_hours * 3600:
                await self.request_shutdown("free-run window complete", agent)
                return
            if self.risk.halted:
                self.draining = True
            await asyncio.sleep(self.cfg.risk_poll_s)

    # ------------------------------------------------------------------ dashboard
    def dashboard(self) -> str:
        now = time.time()
        runtime = int(now - self.started)
        mode = "LIVE" if self.cfg.live else "PAPER"
        eq = self.pf.equity()
        open_value = sum(p.tokens * (p.last_price or p.entry_price) for p in self.pf.open_positions())
        lines = [
            "",
            "=" * 96,
            " MEME AGENTS v{} | {} | up {}h{:02d}m{:02d}s | equity {} ({})".format(
                VERSION, mode, runtime // 3600, (runtime % 3600) // 60, runtime % 60,
                fmt_usd(eq), fmt_pct((eq / max(self.pf.start_capital_usd, 1e-9) - 1) * 100)),
            "-" * 96,
            " agents: {}   queues: cand {} vetted {} signals {}   seen {}".format(
                " ".join("{}={}".format(k[0].upper(), v) for k, v in self.counts.items()),
                self.candidates.qsize(), self.vetted.qsize(), self.signals.qsize(), len(self.seen)),
            " cash {}  open {} ({})  realized {}  fees {}".format(
                fmt_usd(self.pf.cash_usd), len(self.pf.open_positions()), fmt_usd(open_value),
                fmt_usd(self.pf.realized_usd), fmt_usd(self.pf.fees_usd)),
            " trades {}  wins {}  losses {}  win rate {:.1f}%   daily loss used {}/{}".format(
                self.pf.trades, self.pf.wins, self.pf.losses, self.pf.win_rate(),
                fmt_usd(self.risk.daily_loss_used()), fmt_usd(self.cfg.daily_loss_limit_usd)),
            " state: {}   http: {}".format(
                "HALTED ({})".format(self.risk.halt_reason) if self.risk.halted else "trading",
                self.http.stats.summary()),
        ]
        live_positions = self.pf.open_positions()
        if live_positions:
            lines.append(" OPEN POSITIONS")
            lines.append("  {:<9} {:<9} {:>5} {:>11} {:>11} {:>8} {:>8} {:>8} {:>6} {:>8}".format(
                "symbol", "age", "tp", "entry", "now", "pnl", "unreal$", "peak", "trail", "value"))
            for p in live_positions:
                ladder = "{}{}".format("1" if p.tp1_done else "-", "2" if p.tp2_done else "-")
                lines.append("  {:<9} {:<9} {:>5} {:>11.9g} {:>11.9g} {:>8} {:>8} {:>8} {:>5.0f}% {:>8}".format(
                    p.symbol[:9], "{}m".format(int(p.age_s(now) // 60)), ladder,
                    p.entry_price, p.last_price or p.entry_price, fmt_pct(p.pnl_pct()),
                    fmt_usd(p.unrealized_usd()), fmt_pct(p.mfe_pct()),
                    p.trail_pct_now(self.cfg),
                    fmt_usd(p.tokens * (p.last_price or p.entry_price))))
        else:
            lines.append(" OPEN POSITIONS: none")
        lines.append(" feeds: " + " | ".join(
            "{} ok{} err{}".format(n.split(":")[0][:11], s["ok"], s["err"])
            for n, s in self.feed_state.items()))
        if self.recent_events:
            lines.append(" last events:")
            lines += ["   " + e for e in self.recent_events[-5:]]
        lines.append("=" * 96)
        return "\n".join(lines)

    async def dashboard_loop(self) -> None:
        while not self.stop_event.is_set():
            await self._sleep(self.cfg.dashboard_every_s)
            if self.stop_event.is_set():
                return
            try:
                print(self.dashboard())
            except Exception as exc:
                log.debug("dashboard error: %s", exc)

    # ------------------------------------------------------------------ shutdown
    async def request_shutdown(self, reason: str, agent: str = "system") -> None:
        if self.shutting_down:
            return
        self.shutting_down = True
        self.draining = True
        log.warning("KILL SWITCH: %s", reason)
        await self.ledger.record("HALT", agent=agent, reason=reason)
        self.tg.notify("[{}] KILL SWITCH: {}\nclosing {} position(s)".format(
            "LIVE" if self.cfg.live else "PAPER", reason, len(self.pf.open_positions())))
        try:
            await asyncio.wait_for(self.close_all("shutdown: " + reason), timeout=180)
        except asyncio.TimeoutError:
            log.error("close_all timed out; positions may still be open - check the wallet")
        except Exception as exc:
            log.error("close_all failed: %s", exc)
            log.debug("%s", traceback.format_exc())
        self.stop_event.set()

    async def close_all(self, reason: str) -> None:
        """
        Close every open position, without racing the executors.

        A position an executor is already mid-exit on is locked; we skip it and
        retry after a beat instead of selling it twice (the earlier version did
        exactly that and logged "no price/qty" for a position that was already
        flat).
        """
        for attempt in range(3):
            async with self.pf.lock:
                targets = [p for p in self.pf.positions.values() if p.open and not p.locked]
                for p in targets:
                    p.locked = True
            if not targets:
                if not self.pf.open_positions():
                    return
                await asyncio.sleep(1.0)        # let an in-flight exit finish
                continue
            for pos in targets:
                try:
                    await self._close_one(pos, reason)
                except Exception as exc:
                    log.error("close_all(%s) error on %s: %s", pos.symbol, pos.mint, exc)
                finally:
                    pos.locked = False
            if not self.pf.open_positions():
                return
        left = self.pf.open_positions()
        if left:
            log.error("%d position(s) could not be closed - check the wallet manually: %s",
                      len(left), ", ".join(p.symbol for p in left))

    async def _close_one(self, pos: Position, reason: str) -> None:
        snap = None
        try:
            snap = await asyncio.wait_for(self.sources.dexscreener_snapshot(pos.mint), timeout=20)
        except (asyncio.TimeoutError, HttpError):
            snap = pos.snap
        price = (snap.price_usd if snap and snap.price_usd > 0 else pos.last_price)
        decimals = pos.facts.decimals if pos.facts else self.cfg.fallback_token_decimals
        liq = snap.liquidity_usd if snap else pos.entry_liquidity_usd
        fill = await self.executor.sell(pos, pos.tokens, price, decimals, liq, True)
        if not fill.ok:
            log.error("could not close %s: %s", pos.symbol, fill.error)
            await self.ledger.record("SELL_FAIL", agent="risk", mint=pos.mint,
                                     symbol=pos.symbol, reason=fill.error,
                                     detail="position left open at shutdown")
            return
        pnl = await self.pf.sell_fill(pos, fill)
        await self.risk.on_realized(pnl, pos.mint)
        await self.ledger.record("SELL", agent="risk", mint=pos.mint, symbol=pos.symbol,
                                 side="sell", qty=fill.qty_tokens, price=fill.price,
                                 usd=fill.usd_value, fee_usd=fill.fee_usd, pnl_usd=pnl,
                                 reason=reason, detail=fill.detail)
        await self.pf.close_position(pos, reason)
        await self.ledger.record("CLOSE", agent="risk", mint=pos.mint, symbol=pos.symbol,
                                 pnl_usd=pos.realized_usd, reason=reason)
        self.note("CLOSE {} pnl ${:+.2f}".format(pos.symbol, pos.realized_usd))
        self.tg.notify("[{}] CLOSE {} (${:+.2f}) - {}"
                       .format("LIVE" if self.cfg.live else "PAPER", pos.symbol,
                               pos.realized_usd, reason))

    # ------------------------------------------------------------------ report
    def report(self, reason: str) -> str:
        runtime = time.time() - self.started
        eq = self.pf.equity()
        closed = list(self.pf.closed)
        total_realized = sum(p.realized_usd for p in closed)
        best = max(closed, key=lambda p: p.realized_usd, default=None)
        worst = min(closed, key=lambda p: p.realized_usd, default=None)
        exits: Dict[str, int] = {}
        for p in closed:
            key = (p.exit_reason or "?").split(":")[0]
            exits[key] = exits.get(key, 0) + 1
        st = self.http.stats
        lines = [
            "",
            "#" * 96,
            "# SUMMARY  ({})".format(reason),
            "#" * 96,
            " mode                : {}".format("LIVE" if self.cfg.live else "PAPER"),
            " runtime             : {:.1f}h".format(runtime / 3600.0),
            " start capital       : {}".format(fmt_usd(self.pf.start_capital_usd)),
            " final equity        : {} ({})".format(
                fmt_usd(eq), fmt_pct((eq / max(self.pf.start_capital_usd, 1e-9) - 1) * 100)),
            " cash / open value   : {} / {}".format(
                fmt_usd(self.pf.cash_usd),
                fmt_usd(sum(p.tokens * (p.last_price or p.entry_price) for p in self.pf.open_positions()))),
            " realized pnl        : {}   (fees paid {})".format(
                fmt_usd(total_realized), fmt_usd(self.pf.fees_usd)),
            " trades              : {}  wins {}  losses {}  win rate {:.1f}%".format(
                self.pf.trades, self.pf.wins, self.pf.losses, self.pf.win_rate()),
            " best / worst trade  : {} / {}".format(
                "{} {}".format(best.symbol, fmt_usd(best.realized_usd)) if best else "-",
                "{} {}".format(worst.symbol, fmt_usd(worst.realized_usd)) if worst else "-"),
            " exit reasons        : {}".format(
                ", ".join("{} x{}".format(k, v) for k, v in sorted(exits.items())) or "-"),
            " rejections          : {}".format(
                ", ".join("{} x{}".format(k, v) for k, v in
                          sorted(self.reject_counts.items(), key=lambda kv: -kv[1])[:8]) or "-"),
            " api                 : {}".format(st.summary()),
            " feeds               : {}".format(", ".join(
                "{} ok={} err={}".format(n.split(":")[0], s["ok"], s["err"])
                for n, s in self.feed_state.items())),
            " telegram            : {}".format("sent {} failed {}".format(self.tg.sent, self.tg.failed)
                                               if self.tg.enabled else "disabled (no env vars)"),
            "-" * 96,
            " positions:",
        ]
        if closed:
            lines.append("   {:<9} {:>8} {:>12} {:>11} {:>12}  {}".format(
                "symbol", "age", "entry", "exit", "pnl", "reason"))
            for p in closed:
                lines.append("   {:<9} {:>8} {:>12.9g} {:>11.9g} {:>12}  {}".format(
                    p.symbol[:9], "{}m".format(int((p.closed_ts - p.opened_ts) // 60)),
                    p.entry_price, p.last_price, fmt_usd(p.realized_usd), p.exit_reason[:40]))
        else:
            lines.append("   (no closed trades)")
        for p in self.pf.open_positions():
            lines.append("   STILL OPEN: {} {} tokens @ {:.10g} ({})".format(
                p.symbol, "{:.4f}".format(p.tokens), p.entry_price, p.pos_id))
        lines += [
            "-" * 96,
            (" LIVE accounting: cash/open value come from trade flows, "
             "not mark-to-market of the wallet." if self.cfg.live else
             " PAPER accounting: simulated fills incl. slippage, impact, fees, rent."),
            " HONEST REMINDERS",
            "  * Most meme coins go to zero. A green paper run does not mean a green live run:",
            "    public data is delayed, fills are worse in reality and honeypots still exist.",
            "  * At $2 per trade, fixed Solana costs (ATA rent ~0.00204 SOL, priority fees) are a",
            "    large share of every position. Do the math before funding a live wallet.",
            "  * No bot sells at the peak. This one trails 10-15% below the high and hopes.",
            "#" * 96,
        ]
        return "\n".join(lines)

    def save_report(self, text: str) -> str:
        path = self.cfg.report_file or ("free_run_report.txt" if self.cfg.free_run_hours
                                       else "final_report.txt")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            log.error("could not write %s: %s", path, exc)
        return path

    # ------------------------------------------------------------------ main loop
    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def run(self) -> None:
        self.banner()
        await self.executor.startup_checks()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(
                    self.request_shutdown("signal {}".format(s.name))))
            except (NotImplementedError, RuntimeError):
                pass

        for i in range(self.counts["scouts"]):
            self.tasks.append(asyncio.create_task(self.scout_worker(i), name="scout{}".format(i)))
        for i in range(self.counts["safety"]):
            self.tasks.append(asyncio.create_task(self.safety_worker(i), name="safety{}".format(i)))
        for i in range(self.counts["momentum"]):
            self.tasks.append(asyncio.create_task(self.momentum_worker(i), name="momentum{}".format(i)))
        for i in range(self.counts["executors"]):
            self.tasks.append(asyncio.create_task(self.executor_worker(i), name="executor{}".format(i)))
        for _ in range(self.counts["risk"]):
            self.tasks.append(asyncio.create_task(self.risk_worker(), name="risk"))
        self.tasks.append(asyncio.create_task(self.dashboard_loop(), name="dashboard"))
        self.tasks.append(asyncio.create_task(self.tg.sender_loop(self.stop_event), name="telegram"))

        log.info("%d agents running + dashboard/telegram tasks (Ctrl+C to stop)",
                 sum(self.counts.values()))
        await self.stop_event.wait()
        self.draining = True
        await self._stop_tasks()

        text = self.report("stopped")
        print(text)
        path = self.save_report(text)
        log.info("report written to %s", path)

    async def _stop_tasks(self) -> None:
        # Never cancel a task that may be signing/sending a swap: wait briefly for
        # any remaining position to settle first.
        for _ in range(50):
            if not self.pf.open_positions():
                break
            await asyncio.sleep(0.1)
        for t in self.tasks:
            if not t.done():
                t.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


# =============================================================================
# SECTION 15.  CLI
# =============================================================================

def build_config(argv: Optional[Sequence[str]] = None) -> Tuple[Config, argparse.Namespace]:
    p = argparse.ArgumentParser(
        description="100-agent meme-coin swarm for Termux. Paper mode by default.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--live", action="store_true",
                   help="trade real money through Jupiter (requires SOLANA_PRIVATE_KEY + typed confirmation)")
    p.add_argument("--paper", action="store_true", help="paper mode (this is the default)")
    p.add_argument("--free-run-hours", type=float, default=0.0,
                   help="run paper mode for N hours, then print a summary and exit")
    p.add_argument("--backtest-free-run", nargs="?", const=24.0, type=float, default=None,
                   help="forward paper test (no historical data exists for these signals); "
                        "default 24h, e.g. --backtest-free-run 6")
    p.add_argument("--hours", type=float, default=None, help="alias for --free-run-hours")
    p.add_argument("--max-runtime-hours", type=float, default=0.0, help="absolute stop after N hours")
    p.add_argument("--threshold", type=float, default=None, help="override momentum threshold (0-100)")
    p.add_argument("--trade-usd", type=float, default=None, help="override trade size (max $2)")
    p.add_argument("--dashboard-every", type=float, default=None, help="dashboard interval seconds")
    p.add_argument("--ledger", default=None, help="CSV path")
    p.add_argument("--log-file", default=None, help="log path")
    p.add_argument("--cg-key", default="", help="optional CoinGecko API key for /coins/list/new")
    p.add_argument("--selftest", action="store_true", help="offline test suite, no network, exit")
    p.add_argument("--version", action="version", version="meme_agents {}".format(VERSION))
    args = p.parse_args(argv)

    cfg = Config()
    if args.free_run_hours:
        cfg.free_run_hours = max(0.01, args.free_run_hours)
    if args.hours:
        cfg.free_run_hours = max(0.01, args.hours)
    if args.backtest_free_run is not None:
        cfg.free_run_hours = max(0.01, args.backtest_free_run)
    if args.max_runtime_hours:
        cfg.max_runtime_hours = args.max_runtime_hours
    if args.threshold is not None:
        cfg.momentum_threshold = clip(args.threshold, 0.0, 100.0)
    if args.trade_usd is not None:
        if args.trade_usd > cfg.max_trade_usd:
            print("!! trade size {} clamped to the hard cap of {}".format(
                args.trade_usd, fmt_usd(cfg.max_trade_usd)))
        cfg.max_trade_usd = clip(args.trade_usd, 0.25, cfg.max_trade_usd)
    if args.dashboard_every:
        cfg.dashboard_every_s = max(5.0, args.dashboard_every)
    if args.ledger:
        cfg.ledger_csv = args.ledger
    if args.log_file:
        cfg.log_file = args.log_file
    if args.cg_key:
        cfg.cg_api_key = args.cg_key
    if args.live and args.paper:
        p.error("--paper and --live are mutually exclusive")
    cfg.live = bool(args.live)
    return cfg, args


def confirm_live() -> bool:
    print("=" * 78)
    print(" LIVE MODE. Real SOL will be swapped. Meme coins can and do go to zero.")
    print(" Use a burner wallet holding only what you are willing to lose entirely.")
    print(" Type exactly: I ACCEPT THE RISK")
    print("=" * 78)
    try:
        answer = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer == "I ACCEPT THE RISK"


async def async_main(cfg: Config) -> int:
    setup_logging(cfg)
    mode = "LIVE" if cfg.live else "PAPER"
    ledger = Ledger(cfg.ledger_csv, mode)
    http = HttpClient(cfg)
    sources = Sources(cfg, http)
    telegram = Telegram(http)
    pf = Portfolio(cfg)
    if cfg.live:
        executor: BaseExecutor = JupiterExecutor(cfg, sources, http, ledger)
    else:
        executor = PaperExecutor(cfg, pf, sources)
    swarm = Swarm(cfg, http, sources, ledger, telegram, executor, portfolio=pf)
    try:
        await swarm.run()
    except KeyboardInterrupt:
        await swarm.request_shutdown("KeyboardInterrupt")
        await swarm._stop_tasks()
        text = swarm.report("interrupted")
        print(text)
        swarm.save_report(text)
    return 0


# =============================================================================
# SECTION 16.  SELF TEST  (offline; proves the pipeline and the math)
# =============================================================================

GOOD = "GoodMint" + "1" * 34
BAD = "ScamMint" + "1" * 34
HONEY = "HoneyMint" + "1" * 33
PUMP = "PumpMint" + "1" * 34
YOUNG = "YoungMint" + "1" * 33
OLD = "OldMint" + "1" * 35


class FakeTransport:
    """Canned API responses. No sockets, no keys, no money."""

    def __init__(self) -> None:
        self.t0 = time.time()
        self.calls: Dict[str, int] = {}
        self.good_price_drop_at = 16.0          # seconds after start: GOOD loses 30%
        self.sol_usd = 150.0
        self.decimals = 6

    def good_price(self) -> float:
        return 0.0010 if time.time() - self.t0 < self.good_price_drop_at else 0.00068

    def snapshot_pair(self, mint: str, price: float, liq: float, age_min: float,
                      chg_5m: float = 3.0, chg_1h: float = 12.0, chg_24h: float = 30.0,
                      buys5: int = 18, sells5: int = 9) -> Dict[str, Any]:
        created = int((time.time() - age_min * 60) * 1000)
        return {
            "chainId": "solana", "dexId": "raydium", "pairAddress": "pair" + mint[:6],
            "baseToken": {"address": mint, "name": "Test " + mint[:4], "symbol": mint[:6].upper()},
            "quoteToken": {"address": SOL_MINT, "symbol": "SOL"},
            "priceUsd": "{:.10g}".format(price), "priceNative": "0.000001",
            "txns": {"m5": {"buys": buys5, "sells": sells5},
                     "h1": {"buys": 140, "sells": 90},
                     "h6": {"buys": 400, "sells": 300}, "h24": {"buys": 900, "sells": 700}},
            "volume": {"m5": 900.0, "h1": 6000.0, "h6": 30000.0, "h24": 120000.0},
            "priceChange": {"m5": chg_5m, "h1": chg_1h, "h6": chg_24h, "h24": chg_24h},
            "liquidity": {"usd": liq, "base": 1.0, "quote": 1.0},
            "fdv": liq * 8, "marketCap": liq * 8, "pairCreatedAt": created,
        }

    def rugcheck(self, mint: str, mint_auth=None, freeze_auth=None, top10=18.0,
                 lp=100.0, creator_pct=2.0, rugged=False, score=5.0,
                 danger=None) -> Dict[str, Any]:
        supply = 1_000_000_000.0 * (10 ** self.decimals)
        holders = [{"address": "h{}".format(i), "owner": "o{}".format(i), "pct": top10 / 10.0,
                    "type": "wallet"} for i in range(10)]
        holders.append({"address": "poolacc", "owner": "poolowner", "pct": 45.0, "type": "AMM"})
        risks = []
        if danger:
            risks = [{"name": n, "level": "danger", "description": "", "score": 9000}
                     for n in danger]
        if mint_auth:
            risks.append({"name": "Mint Authority still enabled", "level": "danger", "score": 2500})
        return {
            "mint": mint, "tokenProgram": "spl-token", "tokenType": "spl-token",
            "rugged": rugged, "score": score * 100, "score_normalised": score,
            "token": {"supply": supply, "decimals": self.decimals,
                      "mintAuthority": mint_auth, "freezeAuthority": freeze_auth},
            "tokenMeta": {"name": "Test", "symbol": mint[:6].upper()},
            "creator": "creator" + mint[:4],
            "creatorBalance": supply * creator_pct / 100.0 if creator_pct > 0 else 0,
            "topHolders": holders,
            "markets": [{"marketType": "raydium", "pubkey": "market1",
                         "lp": {"lpLocked": 1, "lpLockedPct": lp, "lpLockedUSD": 120000.0,
                                "lpUnlockedPct": 100.0 - lp}}],
            "risks": risks, "totalHolders": 1200, "totalMarketLiquidity": 120000.0,
        }

    def trades(self, mint: str, price: float, buy_heavy: bool = True,
               n: int = 60, sell_vol: float = 0.0) -> Dict[str, Any]:
        now = time.time()
        rows = []
        for i in range(n):
            # spread the tape over the last 55 minutes, heavier in the last 5
            age = (i % 11) * 300.0 + 12.0 if i < 40 else (i - 39) * 25.0
            kind = "buy" if (buy_heavy and i % 3 != 0) else ("sell" if not buy_heavy else "sell")
            vol = 40.0 if kind == "buy" else (sell_vol or 15.0)
            ts = now - age
            rows.append({"id": "t{}".format(i), "type": "trade", "attributes": {
                "block_number": i, "tx_hash": "sig{}".format(i), "kind": kind,
                "volume_in_usd": "{:.2f}".format(vol),
                "from_token_amount": "1", "to_token_amount": "1",
                "price_from_in_usd": "{:.10g}".format(price),
                "price_to_in_usd": "{:.10g}".format(price),
                "block_timestamp": datetime.fromtimestamp(ts, timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"),
                "tx_from_address": "wallet{}{}".format(i % 23, mint[:3]),
            }})
        return {"data": rows}

    def __call__(self, url: str, method: str, headers: Dict[str, str],
                 body: Optional[bytes], timeout: float):
        self.calls[url] = self.calls.get(url, 0) + 1
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)

        def ok(payload: Any):
            return 200, {"Content-Type": "application/json"}, json.dumps(payload).encode()

        if path.endswith("/token-profiles/latest/v1"):
            return ok([{"chainId": "solana", "tokenAddress": GOOD},
                       {"chainId": "ethereum", "tokenAddress": "0xdead"}, ])
        if path.endswith("/token-boosts/latest/v1"):
            return ok([{"chainId": "solana", "tokenAddress": BAD, "amount": 1}])
        if "/token-pairs/v1/solana/" in path:
            mint = path.rsplit("/", 1)[-1]
            price = self.good_price() if mint == GOOD else 0.0005
            if mint == BAD:
                return ok([self.snapshot_pair(mint, price, 9000.0, 45)])
            if mint == GOOD:
                return ok([self.snapshot_pair(mint, price, 120000.0, 45)])
            return ok([])
        if path.endswith("/latest/dex/tokens/" + GOOD):
            return ok({"pairs": [self.snapshot_pair(GOOD, self.good_price(), 120000.0, 45)]})
        if path.endswith("/networks/solana/new_pools"):
            return ok({"data": []})
        if path.endswith("/networks/solana/trending_pools"):
            return ok({"data": []})
        if "/networks/solana/tokens/" in path and path.endswith("/pools"):
            mint = path.split("/networks/solana/tokens/")[1].split("/")[0]
            price = self.good_price() if mint == GOOD else 0.0005
            return ok({"data": [{"id": "solana_pool" + mint[:5], "type": "pool", "attributes": {
                "address": "pool" + mint[:5], "name": mint[:6] + "/SOL",
                "base_token_price_usd": "{:.10g}".format(price),
                "reserve_in_usd": "120000" if mint == GOOD else "9000",
                "volume_usd": {"m5": "900", "h1": "6000", "h6": "30000", "h24": "120000"},
                "price_change_percentage": {"m5": "3", "h1": "12", "h24": "30"},
                "transactions": {"m5": {"buys": 18, "sells": 9, "buyers": 14, "sellers": 8},
                                 "h1": {"buys": 140, "sells": 90, "buyers": 88, "sellers": 61}},
                "pool_created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "fdv_usd": "960000", "market_cap_usd": "960000"}}]})
        if "/networks/solana/pools/" in path and path.endswith("/trades"):
            pool = path.split("/networks/solana/pools/")[1].split("/")[0]
            mint = GOOD if pool.endswith(GOOD[:5]) else BAD
            return ok(self.trades(mint, self.good_price() if mint == GOOD else 0.0005))
        if "/networks/solana/pools/" in path:
            return ok({"data": {"id": "pool", "type": "pool", "attributes": {}}})
        if "/v1/tokens/" in path and path.endswith("/report"):
            mint = path.split("/v1/tokens/")[1].split("/")[0]
            if mint == GOOD:
                return ok(self.rugcheck(mint))
            if mint == BAD:
                return ok(self.rugcheck(mint, mint_auth="9xQeWv", top10=72.0, lp=0.0,
                                        creator_pct=35.0, score=78.0,
                                        danger=["Mint Authority still enabled"]))
            return ok(self.rugcheck(mint))
        if path.endswith("/swap/v1/quote"):
            inp, out = q.get("inputMint", [""])[0], q.get("outputMint", [""])[0]
            amount = int(fnum(q.get("amount", ["0"])[0]))
            if inp == SOL_MINT and out == USDC_MINT:
                return ok({"outAmount": str(int(self.sol_usd * 1e6)),
                           "priceImpactPct": "0.0001", "routePlan": []})
            if inp == SOL_MINT:
                price = self.good_price() if out == GOOD else 0.0005
                out_amt = int(amount / 1e9 * self.sol_usd / price * 10 ** self.decimals)
                return ok({"outAmount": str(out_amt), "priceImpactPct": "0.004", "routePlan": []})
            if out == SOL_MINT:
                price = self.good_price() if inp == GOOD else 0.0005
                impact = "0.09" if inp == HONEY else "0.004"
                out_amt = int(amount / 10 ** self.decimals * price / self.sol_usd * 1e9)
                return ok({"outAmount": str(out_amt), "priceImpactPct": impact, "routePlan": []})
            return ok({"outAmount": "0", "priceImpactPct": "0.5"})
        if "info_recently_updated" in path:
            return ok({"data": []})
        if "telegram" in (parsed.netloc or ""):
            return ok({"ok": True})
        return 404, {}, b'{"error":"unexpected url %s"}' % url.encode()[:120]


def _mk_snap(mint: str, price: float = 1.0, liq: float = 100000.0, age_min: float = 60.0,
             chg5: float = 2.0, chg24: float = 20.0) -> MarketSnapshot:
    return MarketSnapshot(mint=mint, symbol="TEST", price_usd=price, liquidity_usd=liq,
                          pair_address="pair", pool_address="pool" + mint[:4],
                          pair_created_ms=int((time.time() - age_min * 60) * 1000),
                          price_change_5m=chg5, price_change_1h=8.0, price_change_24h=chg24,
                          volume_5m=800.0, volume_1h=5000.0,
                          buys_5m=15, sells_5m=8, buys_1h=120, sells_1h=80, buyers_1h=70)


def _mk_facts(mint: str, **kw) -> TokenFacts:
    base = dict(mint=mint, symbol="TEST", decimals=6, supply=1e15,
                mint_authority=None, freeze_authority=None, creator="c",
                creator_balance_raw=1e12, creator_hold_pct=1.0, top10_pct=18.0,
                lp_locked_pct=100.0, score=5.0, rugged=False)
    base.update(kw)
    return TokenFacts(**base)


def _mk_pos(price: float = 1.0, tokens: float = 100.0, age_s: float = 0.0) -> Position:
    return Position(pos_id="t", mint=GOOD, symbol="TEST",
                    opened_ts=time.time() - age_s, entry_price=1.0, entry_cost_usd=price * tokens,
                    initial_tokens=tokens, tokens=tokens, peak_price=price, peak_ts=time.time(),
                    entry_liquidity_usd=100000.0, creator_baseline_raw=1e12,
                    last_price=price, last_price_ts=time.time())



# ---------------------------------------------------------------------------
# Fake Solana stack for the self test: proves the LIVE path signs, sends,
# confirms, reconciles and refuses to retry - without a wallet or a network.
# ---------------------------------------------------------------------------

def _fake_solders() -> Dict[str, Any]:
    class _Sig:
        def __str__(self) -> str:
            return "5" * 64

    class _Msg:
        def __init__(self, raw: bytes):
            self.raw = raw

    class _Tx:
        def __init__(self, message: Any, signers: Optional[List[Any]] = None):
            self.message = message
            self.signers = signers or []
            self.signatures = [_Sig()]

        @classmethod
        def from_bytes(cls, raw: bytes) -> "_Tx":
            return cls(_Msg(raw))

        def __bytes__(self) -> bytes:
            return b"SIGNED:" + str(self.signers[0].pubkey()).encode()

    class _Pubkey:
        def __str__(self) -> str:
            return "BURNERpubkey111111111111111111111111111111"

    class _Keypair:
        @classmethod
        def from_bytes(cls, raw: bytes) -> "_Keypair":
            return cls()

        @classmethod
        def from_seed(cls, raw: bytes) -> "_Keypair":
            return cls()

        def pubkey(self) -> _Pubkey:
            return _Pubkey()

    kp_mod = types.ModuleType("solders.keypair")
    kp_mod.Keypair = _Keypair                     # type: ignore[attr-defined]
    tx_mod = types.ModuleType("solders.transaction")
    tx_mod.VersionedTransaction = _Tx             # type: ignore[attr-defined]
    root = types.ModuleType("solders")
    return {"solders": root, "solders.keypair": kp_mod, "solders.transaction": tx_mod}


class FakeRpc:
    """Minimal Solana JSON-RPC + Jupiter endpoints for the live-path test."""

    def __init__(self, mint: str):
        self.mint = mint
        self.sol_lamports = 1_000_000_000
        self.token_raw = 0
        self.sends = 0
        self.confirm = "confirmed"          # confirmed | pending | failed
        self.send_status = 200

    def __call__(self, url: str, method: str, headers: Dict[str, str],
                 body: Optional[bytes], timeout: float) -> Tuple[int, Dict[str, str], bytes]:
        parsed = urllib.parse.urlparse(url)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        payload = json.loads(body) if body else {}

        def ok(data: Any) -> Tuple[int, Dict[str, str], bytes]:
            return 200, {"Content-Type": "application/json"}, json.dumps(data).encode()

        if "solana" in parsed.netloc:
            rpc = payload.get("method")
            if rpc == "getBalance":
                return ok({"result": {"context": {"slot": 1}, "value": self.sol_lamports}})
            if rpc == "getTokenAccountsByOwner":
                if self.token_raw <= 0:
                    return ok({"result": {"value": []}})
                return ok({"result": {"value": [{"account": {"data": {"parsed": {"info": {
                    "tokenAmount": {"amount": str(self.token_raw), "decimals": 6}}}}}}]}})
            if rpc == "sendTransaction":
                self.sends += 1
                if self.send_status != 200:
                    return self.send_status, {}, b'{"error":"send failed"}'
                return ok({"result": "5" * 64})
            if rpc == "getSignatureStatuses":
                status = {"confirmed": {"slot": 1, "err": None,
                                        "confirmationStatus": "confirmed"},
                          "failed": {"slot": 1, "err": {"InstructionError": [0, "Custom"]},
                                     "confirmationStatus": "confirmed"},
                          "pending": None}[self.confirm]
                return ok({"result": {"context": {"slot": 1}, "value": [status]}})
            return ok({"result": None})

        if path.endswith("/quote"):
            src, dst = query["inputMint"][0], query["outputMint"][0]
            amount = int(query["amount"][0])
            if src == SOL_MINT and dst == USDC_MINT:
                return ok({"outAmount": "150000000", "priceImpactPct": "0.0001"})
            if src == SOL_MINT and dst == self.mint:
                return ok({"outAmount": "2000000000", "priceImpactPct": "0.004"})
            if src == self.mint and dst == SOL_MINT:
                lamports = int(amount / 1e6 * 0.001 / 150.0 * 1e9)
                return ok({"outAmount": str(lamports), "priceImpactPct": "0.004"})
            return ok({"outAmount": "0", "priceImpactPct": "0.9"})

        if path.endswith("/swap"):
            return ok({"swapTransaction": base64.b64encode(b"RAW").decode()})
        return 404, {}, b"{}"


def run_selftest() -> int:
    import tempfile
    print("=" * 78)
    print(" SELF TEST (offline; no network, no keys, no money)")
    print("=" * 78)
    tmpdir = tempfile.mkdtemp(prefix="meme_selftest_")
    failures: List[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print("  [{}] {}{}".format("ok" if cond else "FAIL", name,
                                   ("  <- " + detail) if detail and not cond else ""))
        if not cond:
            failures.append(name)

    cfg = Config()
    transport = FakeTransport()
    http = HttpClient(cfg, transport=transport)
    sources = Sources(cfg, http)

    # ---------- 1. safety filters -----------------------------------------
    good = evaluate_safety(_mk_snap(GOOD), _mk_facts(GOOD), {"priceImpactPct": "0.004"}, cfg)
    check("good token passes safety", good.passed, good.line())

    liq = evaluate_safety(_mk_snap(GOOD, liq=9000), _mk_facts(GOOD), None, cfg)
    check("thin liquidity rejected", not liq.passed and
          any("liquidity" in r for r in liq.reasons), str(liq.reasons))

    top = evaluate_safety(_mk_snap(GOOD), _mk_facts(GOOD, top10_pct=55.0), None, cfg)
    check("top10 concentration rejected", not top.passed and
          any("top10" in r for r in top.reasons), str(top.reasons))

    auth = evaluate_safety(_mk_snap(GOOD), _mk_facts(GOOD, mint_authority="x"), None, cfg)
    check("live mint authority rejected", not auth.passed and
          any("mint_authority" in r for r in auth.reasons), str(auth.reasons))

    frz = evaluate_safety(_mk_snap(GOOD), _mk_facts(GOOD, freeze_authority="x"), None, cfg)
    check("live freeze authority rejected", not frz.passed and
          any("freeze_authority" in r for r in frz.reasons), str(frz.reasons))

    lp = evaluate_safety(_mk_snap(GOOD), _mk_facts(GOOD, lp_locked_pct=0.0), None, cfg)
    check("unlocked LP rejected", not lp.passed and any("lp_locked" in r for r in lp.reasons),
          str(lp.reasons))

    yng = evaluate_safety(_mk_snap(GOOD, age_min=4), _mk_facts(GOOD), None, cfg)
    check("token younger than 10m rejected", not yng.passed and
          any("age_min" in r for r in yng.reasons), str(yng.reasons))

    old = evaluate_safety(_mk_snap(GOOD, age_min=9 * 60), _mk_facts(GOOD), None, cfg)
    check("token older than 6h rejected", not old.passed and
          any("age_max" in r for r in old.reasons), str(old.reasons))

    late = evaluate_safety(_mk_snap(GOOD, chg24=650.0), _mk_facts(GOOD), None, cfg)
    check("already >5x rejected", not late.passed and
          any("not_too_late" in r for r in late.reasons), str(late.reasons))

    chase = evaluate_safety(_mk_snap(GOOD, chg5=55.0), _mk_facts(GOOD), None, cfg)
    check("+55% 5m candle not chased", not chase.passed and
          any("not_chasing" in r for r in chase.reasons), str(chase.reasons))

    honey = evaluate_safety(_mk_snap(GOOD), _mk_facts(GOOD), {"priceImpactPct": "0.09"}, cfg)
    check("honeypot sell impact rejected", not honey.passed and
          any("sell_route" in r for r in honey.reasons), str(honey.reasons))

    unknown = TokenFacts(mint=GOOD, error="rugcheck: 404")
    unk = evaluate_safety(_mk_snap(GOOD), unknown, None, cfg)
    check("unknown safety data rejected (strict)", not unk.passed, str(unk.reasons))

    crash = evaluate_safety(_mk_snap(GOOD, price=1.0), _mk_facts(GOOD, rugged=True), None, cfg)
    check("rugged flag rejected", not crash.passed and any("rugged" in r for r in crash.reasons),
          str(crash.reasons))

    # ---------- 2. momentum ------------------------------------------------
    strong = momentum_score(_mk_snap(GOOD), MicroStats(
        trades_seen=60, buys_5m=30, sells_5m=6, buy_vol_5m=3000, sell_vol_5m=200,
        buyers_5m=25, buyers_1h=70, buys_1h=120, sells_1h=60, buy_vol_1h=9000,
        sell_vol_1h=3000), cfg)
    weak = momentum_score(_mk_snap(GOOD), MicroStats(
        trades_seen=60, buys_5m=3, sells_5m=20, buy_vol_5m=100, sell_vol_5m=900,
        buyers_5m=2, buyers_1h=90, buys_1h=40, sells_1h=120), cfg)
    check("strong tape scores {:.0f} > weak {:.0f}".format(strong.score, weak.score),
          strong.score > weak.score + 20, "{:.1f} vs {:.1f}".format(strong.score, weak.score))
    check("strong tape above default threshold", strong.score >= cfg.momentum_threshold,
          "{:.1f}".format(strong.score))
    check("sell wall veto fires", weak.veto != "", weak.veto)
    pump = momentum_score(_mk_snap(GOOD, chg5=60.0), None, cfg)
    check("pump veto fires", pump.veto == "chasing_pump", pump.veto)

    # ---------- 3. exit engine ---------------------------------------------
    pos = _mk_pos()
    check("no exit at flat price", decide_exit(pos, _mk_snap(GOOD, price=1.0), None, None,
                                               time.time(), cfg) is None)
    d = decide_exit(pos, _mk_snap(GOOD, price=0.79), None, None, time.time(), cfg)
    check("hard stop loss at -20%", d is not None and d.kind == "stop_loss", str(d))
    d = decide_exit(pos, _mk_snap(GOOD, price=2.05), None, None, time.time(), cfg)
    check("TP1 at +100% sells half", d is not None and d.tag == "tp1" and
          abs(d.qty - 50.0) < 1e-6, str(d))
    pos2 = _mk_pos()
    pos2.tp1_done = True
    pos2.tokens = 50.0
    d = decide_exit(pos2, _mk_snap(GOOD, price=4.1), None, None, time.time(), cfg)
    check("TP2 at +300% sells 25% of initial", d is not None and d.tag == "tp2" and
          abs(d.qty - 25.0) < 1e-6, str(d))
    pos3 = _mk_pos()
    pos3.peak_price = 10.0
    pos3.tp1_done = pos3.tp2_done = True
    d = decide_exit(pos3, _mk_snap(GOOD, price=8.4), None, None, time.time(), cfg)
    check("trailing stop fires 16% off the peak", d is not None and d.kind == "trailing_stop", str(d))
    pos4 = _mk_pos()
    pos4.peak_price = 6.5                                 # +550% -> trail tightens to 10%
    pos4.tp1_done = pos4.tp2_done = True
    d = decide_exit(pos4, _mk_snap(GOOD, price=5.80), None, None, time.time(), cfg)
    check("trail tightens to 10% above +500%", d is not None and d.kind == "trailing_stop", str(d))
    pos4b = _mk_pos()
    pos4b.peak_price = 3.0                                # +200% -> trail stays 15%
    pos4b.tp1_done = pos4b.tp2_done = True
    d = decide_exit(pos4b, _mk_snap(GOOD, price=2.70), None, None, time.time(), cfg)
    check("loose 15% trail does NOT fire at -10% (only the tight one does)", d is None, str(d))
    pos4c = _mk_pos()
    pos4c.tp1_done = pos4c.tp2_done = True
    d = decide_exit(pos4c, _mk_snap(GOOD, price=1.0, liq=0.0), None, None, time.time(), cfg)
    check("zero liquidity is an emergency", d is not None and d.emergency and
          "liquidity" in d.reason, str(d))
    pos5 = _mk_pos(age_s=3 * 3600)
    d = decide_exit(pos5, _mk_snap(GOOD, price=1.03), None, None, time.time(), cfg)
    check("time stop closes a flat 3h position", d is not None and d.kind == "time_stop", str(d))
    pos6 = _mk_pos()
    d = decide_exit(pos6, _mk_snap(GOOD, price=1.0, liq=55000), None, None, time.time(), cfg)
    check("liquidity -45% triggers emergency", d is not None and d.emergency and
          "liquidity" in d.reason, str(d))
    pos7 = _mk_pos()
    dump = _mk_facts(GOOD, creator_balance_raw=7e11)
    d = decide_exit(pos7, _mk_snap(GOOD, price=1.0), None, dump, time.time(), cfg)
    check("creator dump triggers emergency", d is not None and d.emergency and
          "creator" in d.reason, str(d))
    pos8 = _mk_pos()
    spike = MicroStats(trades_seen=20, buys_5m=5, sells_5m=15, buy_vol_5m=100,
                       sell_vol_5m=800, buyers_5m=5, buyers_1h=20)
    d = decide_exit(pos8, _mk_snap(GOOD, price=1.0), spike, None, time.time(), cfg)
    check("sell-volume spike triggers emergency", d is not None and d.emergency, str(d))

    # ---------- 4. paper fills & accounting --------------------------------
    async def fill_tests() -> None:
        pf = Portfolio(cfg)
        ex = PaperExecutor(cfg, pf, sources)
        f = await ex.buy(GOOD, 2.0, 0.0010, 6, 120000.0)
        check("paper buy fills", f.ok, f.error)
        check("paper buy includes fees (network+priority+rent)", f.fee_usd > 0.2,
              "{:.3f}".format(f.fee_usd))
        check("paper fill price >= reference (slippage)", f.price > 0.0010)
        tiny = await ex.buy(GOOD, 2.0, 0.0010, 6, 10.0)
        check("paper buy refuses >3% slippage", not tiny.ok and "slippage" in tiny.error, tiny.error)
        s = await ex.sell(_mk_pos(), 100.0, 0.0010, 6, 120000.0, False)
        check("paper sell fills below reference", s.ok and s.price < 0.0010, s.error)
        hard = await ex.sell(_mk_pos(), 100.0, 0.0010, 6, 50.0, True)
        check("emergency sell caps slippage at 8%", hard.ok and hard.slippage_bps <= 800,
              "{:.0f}bps".format(hard.slippage_bps))

    asyncio.run(fill_tests())

    # ---------- 4b. LIVE execution path (fake solders + fake RPC) ----------
    saved_mods = {k: sys.modules.get(k) for k in ("solders", "solders.keypair",
                                                  "solders.transaction")}
    saved_key = os.environ.get("SOLANA_PRIVATE_KEY")
    sys.modules.update(_fake_solders())
    os.environ["SOLANA_PRIVATE_KEY"] = "1" * 64        # valid base58, 64 zero bytes

    async def live_tests() -> None:
        lcfg = replace(cfg, live=True, live_confirm_timeout_s=1.5,
                       ledger_csv=os.path.join(tmpdir, "live.csv"))
        net = FakeRpc(GOOD)
        client = HttpClient(lcfg, transport=net)
        lsrc = Sources(lcfg, client)
        ex = JupiterExecutor(lcfg, lsrc, client, Ledger(lcfg.ledger_csv, "LIVE"))
        await ex.startup_checks()
        f = await ex.buy(GOOD, 2.0, 0.001, 6, 120000.0)
        check("live buy: quote->swap->sign->send->confirm", f.ok and f.tx_sig == "5" * 64, f.error)
        net.send_status = 500
        before = net.sends
        f = await ex.buy(GOOD, 2.0, 0.001, 6, 120000.0)
        check("live buy: failed send is NOT retried blindly",
              (not f.ok) and net.sends - before == 1, "attempts={}".format(net.sends - before))
        net.send_status = 200
        net.confirm, net.token_raw = "pending", 2_000_000_000
        f = await ex.buy(GOOD, 2.0, 0.001, 6, 120000.0)
        check("live buy: unconfirmed but tokens arrived = filled",
              f.ok and "unconfirmed" in f.detail, f.error)
        net.token_raw = 0
        f = await ex.buy(GOOD, 2.0, 0.001, 6, 120000.0)
        check("live buy: unconfirmed and empty = flagged, no retry",
              (not f.ok) and f.unconfirmed, f.error)
        net.confirm = "confirmed"
        net.sol_lamports = 12_000_000
        f = await ex.buy(GOOD, 2.0, 0.001, 6, 120000.0)
        check("live buy: wallet balance checked before trading",
              (not f.ok) and "insufficient SOL" in f.error, f.error)
        net.sol_lamports = 1_000_000_000
        lpos = _mk_pos()
        lpos.mint = GOOD
        net.token_raw = 1_999_000_000
        f = await ex.sell(lpos, 100.0, 0.001, 6, 120000.0, False)
        check("live sell: uses the real on-chain token balance",
              f.ok and abs(f.qty_tokens - 1999.0) < 1e-6, f.error)

    try:
        asyncio.run(live_tests())
    finally:
        for k, v in saved_mods.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        if saved_key is None:
            os.environ.pop("SOLANA_PRIVATE_KEY", None)
        else:
            os.environ["SOLANA_PRIVATE_KEY"] = saved_key

    # ---------- 5. full pipeline with a fake network -----------------------
    tmp = tmpdir
    transport.t0 = time.time()          # restart the scripted price crash
    tcfg = replace(cfg, ledger_csv=os.path.join(tmp, "trades.csv"),
                   log_file=os.path.join(tmp, "bot.log"),
                   report_file=os.path.join(tmp, "report.txt"),
                   momentum_threshold=0.0, dashboard_every_s=999.0,
                   position_refresh_s=1.0, micro_refresh_s=2.0, facts_refresh_s=2.0,
                   max_runtime_hours=26.0 / 3600.0, verbose_progress=False,
                   seen_ttl_s=120.0)
    tcfg.agents = {"scouts": 4, "safety": 3, "momentum": 3, "executors": 4, "risk": 1}
    tcfg.exact_agent_total = False
    tcfg.free_run_hours = 0.0
    setup_logging(tcfg, console_level=logging.ERROR)
    print("  running the full 15-agent pipeline for ~26s against canned API data...")

    async def pipeline() -> Tuple[Ledger, Swarm, List[str]]:
        client = HttpClient(tcfg, transport=transport)
        src = Sources(tcfg, client)
        tel = Telegram(client)
        ledger = Ledger(tcfg.ledger_csv, "PAPER")
        pf = Portfolio(tcfg)
        swarm = Swarm(tcfg, client, src, ledger, tel,
                      PaperExecutor(tcfg, pf, src), portfolio=pf)
        await swarm.run()
        rows = []
        try:
            with open(tcfg.ledger_csv, encoding="utf-8") as fh:
                rows = [",".join(r) for r in csv.reader(fh)]
        except OSError:
            pass
        return ledger, swarm, rows

    _, swarm, rows = asyncio.run(pipeline())
    body = "\n".join(rows)
    check("pipeline discovered and bought the good token", "BUY" in body)
    check("pipeline rejected the scam token", "REJECT" in body and BAD in body)
    check("stop loss closed the position when price dropped 32%",
          "stop_loss" in body and "CLOSE" in body)
    check("equity accounting is finite and non-negative",
          swarm.pf.equity() >= 0, "{:.2f}".format(swarm.pf.equity()))
    realized_total = sum(x.realized_usd for x in swarm.pf.closed)
    drift = (swarm.pf.equity() - swarm.pf.start_capital_usd) - realized_total
    check("realized pnl reconciles with equity (entry fees included)",
          abs(drift) < 0.01, "drift {:.4f}".format(drift))
    check("no position left open after shutdown", not swarm.pf.open_positions())
    check("ledger contains a PASS_SAFETY row", "PASS_SAFETY" in body)

    print("-" * 78)
    print("  selftest ledger: {}".format(tcfg.ledger_csv))
    if failures:
        print("  RESULT: {} FAILURE(S): {}".format(len(failures), ", ".join(failures)))
        return 1
    print("  RESULT: ALL CHECKS PASSED")
    print("-" * 78)
    print(" KNOWN LIMITS (same list as README.md):")
    print("  * public APIs are delayed, rate-limited and can change shape without notice")
    print("  * 'creator sold >20%' is only detectable from our entry baseline, not historically")
    print("  * honeypot detection = a Jupiter quote, not a real on-chain sell simulation")
    print("  * paper fills cannot model MEV, failed txs, or a dev front-running your sell")
    print("  * most meme coins go to zero; this bot can lose the entire 10 USD")
    print("  * 100 asyncio agents share ONE event loop: this is concurrency, not parallelism")
    return 0


# =============================================================================
# SECTION 17.  ENTRY POINT
# =============================================================================

def main(argv: Optional[Sequence[str]] = None) -> int:
    cfg, args = build_config(argv)
    if args.selftest:
        return run_selftest()
    if cfg.live:
        if not confirm_live():
            print("Live confirmation failed. Staying safe. Nothing was traded.")
            return 2
        if not os.environ.get("SOLANA_PRIVATE_KEY"):
            print("SOLANA_PRIVATE_KEY is not set. LIVE mode cannot start.")
            return 2
    else:
        print("PAPER MODE: simulated fills only. No real money moves. Use --live for real swaps.")
    try:
        return asyncio.run(async_main(cfg))
    except KeyboardInterrupt:
        print("\nInterrupted. Positions were closed if the kill switch completed.")
        return 130
    except RuntimeError as exc:
        print("\nSTARTUP FAILED: {}".format(exc))
        print("Nothing was traded. Fix the above and try again.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
