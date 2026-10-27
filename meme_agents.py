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
  * THERE IS NO SUCH THING AS A SURE SIGNAL in memecoins. What this version does
    instead: (a) harder entries - momentum >= 70, a real 5m tape (>= 12 trades,
    >= 6 unique buyers, >= $200 volume), at least 2 independent providers, and a
    SECOND LOOK 25s later that re-fetches, re-scores and re-runs every filter;
    (b) 24/7 operation - state.json makes restarts resume, heartbeat.json lets a
    watchdog see it is alive; (c) self-measurement - `--analyze` reads trades.csv
    and tells you honestly whether the rules made money (they often do not).
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
    if it drops 20% or more while we hold. See evaluate_safety() / decide_exit().
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

SIGNALS ONLY (research; never places an order)
----------------------------------------------
  python meme_agents.py --scan            # one-shot scan, prints signal cards
  python meme_agents.py --scan 5 --json   # five candidates, machine-readable
  python meme_agents.py --watch           # continuous monitor, Ctrl+C / STOP
  python meme_agents.py --watch --refresh 60 --iterations 10
  python meme_agents.py --no-fomo --scan  # ignore FOMO even if a key is set

RUN (paper mode, the default)
-----------------------------
  python meme_agents.py
  python meme_agents.py --free-run-hours 24      # 24h forward paper test
  python meme_agents.py --analyze                # did the signals actually pay?
  python meme_agents.py --research-check BONK    # test the optional browser skill
  python meme_agents.py --selftest               # offline test suite, no network

WEB RESEARCH SKILL (optional, needs the `obscura` headless browser)
-------------------------------------------------------------------
  Before any signal is emitted, and every ~5 minutes for open positions, the bot
  opens public pages with Obscura and looks for the ticker: DexScreener page,
  DuckDuckGo, Bing, Google News, Reddit, public t.me channels (+ x.com search if
  a cookie jar is configured).
    * bad news ("honeypot", "rug", "exploit"...) on 2+ pages -> veto the buy,
      and exit an open position immediately
    * clean chatter -> at most +5 momentum points (never enough on its own)
    * no browser installed, login wall, timeout -> "unavailable"; by default the
      price rules decide alone, with --research-required nothing trades blind.
  Verify it first:  python meme_agents.py --research-check BONK
  Install notes and the Termux/proot caveat: README section 1b.

RUNNING IT ALL DAY
------------------
  The bot itself never stops (no runtime limit unless you pass one) and saves
  state.json after every change, so a crash or a dead Termux session costs you
  nothing but time. Wrap it in a restart loop:

    termux-wake-lock
    while true; do python meme_agents.py --paper; sleep 10; done

  heartbeat.json is rewritten every dashboard tick; if its timestamp stops
  moving, the loop is dead. --no-state starts from a clean portfolio.

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

FOMO (optional, and read the fine print)
----------------------------------------
  fomo.family (FOMO Labs) publishes NO public developer API. There is no key
  flow, no docs, no SDK, and their terms do not allow automating their private
  backend. This bot therefore does not touch fomo.family at all.

  What it CAN use is fomoapi.io, an independent, unofficial service that
  republishes FOMO token flow and warnings. It is optional and off unless you
  export FOMO_API_KEY:
      /v2/token/{mint}/stats     flow: buys/sells, unique buyers, net volume
      /v2/token/{mint}/warnings  buyDisabled / sellDisabled / severe flags
      /v2/token/{mint}/devs      deployer + insider holdings (extra credits)
  It costs credits (250/call; free tier 20 req/min, 250k credits/month), so the
  bot enriches at most 3 candidates per scan, only after they pass every local
  filter, and caches each mint for 15 minutes.
  A FOMO warning is a veto. FOMO silence is NOT a safety pass.

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
import shutil
import signal
import subprocess
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

VERSION = "1.3.0"

# Optional FOMO provider. The file ships next to this one; if it is missing the
# bot still starts and simply reports FOMO as unavailable. It never turns into a
# hard dependency because FOMO data is optional enrichment, not a price feed.
try:
    import fomo_provider as _fomo_mod
    from fomo_provider import (FOMO_API_KEY_ENV, FOMO_STATUS_NOTE, FomoProvider,
                               normalize_mint as fomo_normalize_mint)
    FOMO_IMPORT_ERROR = ""
except Exception as _fomo_exc:                       # noqa: BLE001 - optional module
    _fomo_mod = None
    FOMO_API_KEY_ENV = "FOMO_API_KEY"
    FOMO_STATUS_NOTE = "fomo_provider.py is not installed next to meme_agents.py"
    FomoProvider = None                              # type: ignore[assignment]
    fomo_normalize_mint = None                       # type: ignore[assignment]
    FOMO_IMPORT_ERROR = str(_fomo_exc)

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


def fmt_price(x: Optional[float]) -> str:
    """Readable price for sub-cent meme tokens: 4 sig figs, no exponent."""
    if x is None:
        return "?"
    try:
        if x <= 0:
            return "?"
        if x >= 0.01:
            return "{:.6g}".format(x)
        if x < 1e-9:                      # absurdly small; keep exponent
            return "{:.4g}".format(x)
        digits = min(12, max(4, int(-math.floor(math.log10(x))) + 4))
        return "{:.{}f}".format(x, digits).rstrip("0").rstrip(".")
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
    momentum_threshold: float = 70.0              # 0..100, high-conviction only
    max_5m_pump_pct: float = 40.0                 # never chase a +40% 5m candle

    # ---- signal quality (this is what "only strong signals" actually means) --
    min_corroboration: int = 2                    # independent providers with data
    confirm_delay_s: float = 25.0                 # second look this long after the first
    confirm_max_drop_pct: float = 5.0             # price must hold within -5% of first look
    min_unique_buyers_5m: int = 6
    min_trades_5m: int = 12
    min_tape_volume_5m_usd: float = 200.0
    reject_copycats: bool = True
    max_positions_per_symbol: int = 1

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
    state_file: str = "state.json"              # survives restarts / crashes
    heartbeat_file: str = "heartbeat.json"
    heartbeat_every_s: float = 10.0      # for external watchdogs
    state_enabled: bool = True
    stale_price_alert_s: float = 300.0

    # ---- web research (Obscura headless browser, optional) --------------------
    research_enabled: bool = True             # auto-disables when the binary is missing
    obscura_bin: str = ""                     # empty = $OBSCURA_BIN, PATH, ./obscura
    research_stealth: bool = True             # consistent fingerprint + tracker blocking
    research_obey_robots: bool = False        # polite mode: obey robots.txt per site
    research_timeout_s: float = 25.0          # hard cap for one token's whole lookup
    research_min_interval_s: float = 2.5      # between browser launches (one core phone)
    research_concurrency: int = 1
    research_cache_ttl_s: float = 900.0       # per source URL
    research_max_sources: int = 3             # 3 browser launches per candidate, max
    research_required: bool = False           # True = no research, no trade
    research_boost_points: float = 5.0        # score bonus for confirmed chatter
    research_boost_min_mentions: int = 4
    research_scam_veto_hits: int = 4
    research_scam_min_sources: int = 2
    research_position_interval_s: float = 300.0
    research_channels: str = ""               # comma-separated public t.me channels
    research_storage_dir: str = ""            # cookie jar (env OBSCURA_STORAGE_DIR too)

    # ---- FOMO provider (optional, needs FOMO_API_KEY) -------------------------
    # fomo.family publishes no public API. This talks to the independent
    # fomoapi.io service only when you supply a key; otherwise it stays off and
    # every FOMO field reads "n/a". See fomo_provider.py for the full story.
    fomo_enabled: bool = True                 # auto: on only when a key is present
    fomo_api_key: str = field(default="", repr=False)   # from env, never logged
    fomo_base_url: str = ""                   # empty = documentation default
    fomo_timeout_s: float = 10.0
    fomo_min_interval_s: float = 3.2          # free tier: 20 requests/minute
    fomo_cache_ttl_s: float = 900.0
    fomo_max_tokens_per_scan: int = 3         # 500 credits each; stay frugal
    fomo_include_slow: bool = False           # devs + holders = 500 more credits
    fomo_score_boost: float = 4.0             # max points from flow confirmation
    fomo_veto_severe: bool = True
    fomo_veto_dev_exit: bool = True

    # ---- live signal pipeline (--scan / --watch; research only, never trades) --
    scan_max_candidates: int = 8              # how many mints a scan will enrich
    scan_confirm_delay_s: float = 20.0        # second look before CONFIRMED
    scan_concurrency: int = 3
    signal_expiry_s: float = 1800.0           # WATCH/CANDIDATE go stale after 30 min
    watch_interval_s: float = 120.0           # --watch refresh
    telegram_signal_cooldown_s: float = 1800.0
    signal_file: str = "signals.json"
    dashboard_compact: bool = True            # phone-friendly dashboard

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
    "ts_utc", "mode", "agent", "event", "pos_id", "mint", "symbol", "side",
    "qty_tokens", "price_usd", "usd_value", "fee_usd", "pnl_usd", "score",
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
            if os.path.exists(self.path) and os.path.getsize(self.path) > 0:
                with open(self.path, newline="", encoding="utf-8") as fh:
                    header = next(csv.reader(fh), [])
                if header == LEDGER_FIELDS:
                    return
                # Schema changed: never mix column layouts in one file.
                backup = "{}.{}.csv".format(
                    os.path.splitext(self.path)[0], now_utc().strftime("%Y%m%d-%H%M%S"))
                os.replace(self.path, backup)
                log.warning("old ledger schema moved to %s", backup)
            with open(self.path, "w", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerow(LEDGER_FIELDS)
        except OSError as exc:                      # read-only storage etc.
            log.error("cannot write ledger %s: %s", self.path, exc)

    async def record(self, event: str, agent: str = "system", mint: str = "",
                     symbol: str = "", side: str = "", qty: float = 0.0,
                     price: float = 0.0, usd: float = 0.0, fee_usd: float = 0.0,
                     pnl_usd: float = 0.0, reason: str = "", detail: str = "",
                     pos_id: str = "", score: float = 0.0) -> None:
        row = [utc_str(), self.mode, agent, event, pos_id, mint, symbol, side,
               "{:.8f}".format(qty) if qty else "", "{:.10g}".format(price) if price else "",
               "{:.6f}".format(usd) if usd else "", "{:.6f}".format(fee_usd) if fee_usd else "",
               "{:.6f}".format(pnl_usd) if pnl_usd else "",
               "{:.1f}".format(score) if score else "", reason, detail[:400]]
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
    decimals: int = 6
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
        self._fomo: Optional[Any] = None
        self._fomo_ready = False          # one-shot lazy init, even when keyless

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

    # ---------------------------------------------------------------- fomo
    def fomo_provider(self) -> Optional[Any]:
        """Lazy singleton: created once, only when a key is configured."""
        if not self._fomo_ready:
            self._fomo_ready = True
            self._fomo = None
            if self.cfg.fomo_enabled and FomoProvider is not None:
                key = (self.cfg.fomo_api_key or os.environ.get(FOMO_API_KEY_ENV, "")).strip()
                if key:
                    try:
                        self._fomo = FomoProvider(          # type: ignore[attr-defined]
                            api_key=key, base_url=self.cfg.fomo_base_url,
                            timeout_s=self.cfg.fomo_timeout_s,
                            min_interval_s=self.cfg.fomo_min_interval_s,
                            cache_ttl_s=self.cfg.fomo_cache_ttl_s)
                        log.info("FOMO provider ready (independent fomoapi.io service)")
                    except Exception as exc:                # noqa: BLE001 - optional
                        log.warning("FOMO provider disabled: %s", exc)
                        self._fomo = None
        return self._fomo

    async def fomo_enrich(self, mint: str, include_slow: bool = False) -> Optional[Any]:
        """
        Optional enrichment. Returns None when FOMO is off, keyless, or failed.

        Callers must treat None as "unknown", never as "clean".
        """
        provider = self.fomo_provider()
        if provider is None:
            return None
        try:
            return await asyncio.wait_for(
                provider.enrich(mint, include_slow=include_slow),
                timeout=self.cfg.fomo_timeout_s * 3 + 5.0)
        except asyncio.TimeoutError:
            log.debug("FOMO enrich timed out for %s", short_addr(mint))
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:                        # noqa: BLE001 - optional data
            log.debug("FOMO enrich failed for %s: %s", short_addr(mint), exc)
            return None

    def fomo_health(self) -> Dict[str, Any]:
        provider = self.fomo_provider()
        if provider is None:
            return {"available": False, "status": (
                "disabled (no FOMO_API_KEY)" if self.cfg.fomo_enabled
                else "disabled (--no-fomo)")}
        return provider.health()

    async def simulate_sell_quote(self, mint: str, price: float,
                                  facts: Optional[TokenFacts]) -> Optional[Dict[str, Any]]:
        """Honeypot check #2: can ${honeypot_check_usd} actually be sold, at what impact?"""
        if price <= 0 or facts is None or not facts.available:
            return None
        decimals = facts.decimals
        tokens = self.cfg.honeypot_check_usd / price if price > 0 else 0.0
        if tokens <= 0 or (facts.supply > 0 and tokens > facts.supply * 0.05):
            return None
        raw = int(tokens * (10 ** decimals))
        if raw <= 0:
            return None
        return await self.jupiter_quote(mint, SOL_MINT, raw, 300)

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

    def snapshot_from_gt(self, mint: str, pool: Dict[str, Any]) -> MarketSnapshot:
        """Build a snapshot from GeckoTerminal alone (DexScreener may lag)."""
        snap = MarketSnapshot(mint=mint, source="geckoterminal", ts=time.time())
        return self.apply_gt_pool(snap, pool)

    def apply_gt_pool(self, snap: MarketSnapshot, pool: Dict[str, Any]) -> MarketSnapshot:
        attrs = (pool or {}).get("attributes") or {}
        if not attrs:
            return snap
        if not snap.symbol:
            snap.symbol = str(attrs.get("name") or "").split("/")[0].strip()[:24]
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

# Impersonation and bait patterns. A token whose *symbol* is exactly one of these
# is not "the" token: it is a copycat dressed as it. Nobody needs a 10-minute-old
# "USDC" from a $60k pool.
PROTECTED_SYMBOLS = {
    "SOL", "WSOL", "USDC", "USDT", "USDG", "USDS", "EURC", "JUP", "JITO", "JTO",
    "BONK", "WIF", "PYTH", "RAY", "ORCA", "MSOL", "BSOL", "JITOSOL", "INF",
    "PYUSD", "FDUSD", "DAI", "BTC", "WBTC", "ETH", "WETH", "USDE", "SUSD",
}

# Classic bait wording found in scam token metadata.
SCAM_PATTERNS = (
    "official", "airdrop", "claim", "giveaway", "reward", "verify", "validate",
    "presale", "whitelist", "t.me", "telegram", "discord.gg", "http://",
    "https://", ".com", ".xyz", ".io/", "connect wallet", "free ",
)

# Zero-width / bidi / control characters used to hide a fake ticker.
_HIDDEN_CHARS = set(range(0x00, 0x20)) | set(range(0x7F, 0xA0)) | \
    set(range(0x200B, 0x2010)) | set(range(0x202A, 0x202F)) | {0x2060, 0xFEFF}


def name_quality(symbol: str, name: str) -> Optional[str]:
    """
    Returns a rejection reason, or None when the metadata looks legitimate.

    Cheap, deterministic filters for the most common scam patterns: a token
    impersonating a major ticker, invisible characters in the name, or metadata
    that reads like a phishing link.
    """
    sym = (symbol or "").strip()
    nm = (name or "").strip()
    if not sym:
        return "empty symbol"
    if sym.upper() in PROTECTED_SYMBOLS:
        return "impersonates {} (copycat ticker)".format(sym.upper())
    if len(sym) > 20:
        return "symbol too long ({} chars)".format(len(sym))
    if len(nm) > 48:
        return "name too long ({} chars)".format(len(nm))
    for text in (sym, nm):
        bad = [c for c in text if ord(c) in _HIDDEN_CHARS]
        if bad:
            return "hidden/control characters in metadata"
    haystack = (sym + " " + nm).lower()
    for pat in SCAM_PATTERNS:
        if pat in haystack:
            return "bait text in metadata: {!r}".format(pat)
    return None


def tape_quality(micro: Optional[MicroStats], cfg: Config) -> Optional[str]:
    """
    Is there a real, human-sized tape behind the signal?

    A ratio of "3 buys / 0 sells" is meaningless; a minimum number of
    transactions, unique wallets and dollar volume is not. This is the check that
    stops the bot buying a token with a beautiful score and $11 of volume.
    """
    if micro is None or micro.trades_seen <= 0:
        return "no trade tape available"
    if micro.total_5m < cfg.min_trades_5m:
        return "only {} trades in 5m (min {})".format(micro.total_5m, cfg.min_trades_5m)
    if micro.buyers_5m < cfg.min_unique_buyers_5m:
        return "{} unique buyers in 5m (min {})".format(micro.buyers_5m,
                                                        cfg.min_unique_buyers_5m)
    vol = micro.buy_vol_5m + micro.sell_vol_5m
    if vol < cfg.min_tape_volume_5m_usd:
        return "5m volume {} (min {})".format(fmt_usd(vol), fmt_usd(cfg.min_tape_volume_5m_usd))
    return None


def confirmation_verdict(first_price: float, now_price: float, score: float,
                         cfg: Config) -> Optional[str]:
    """Second-look verdict. Returns a rejection reason, or None if it still qualifies."""
    if now_price <= 0:
        return "confirm_no_price"
    if first_price > 0 and now_price < first_price * (1.0 - cfg.confirm_max_drop_pct / 100.0):
        return "confirm_price_dropped {:.1f}%".format((now_price / first_price - 1.0) * 100.0)
    if score < cfg.momentum_threshold:
        return "confirm_momentum_lost ({:.1f} < {:.1f})".format(score, cfg.momentum_threshold)
    return None


def evaluate_safety(snap: Optional[MarketSnapshot], facts: Optional[TokenFacts],
                    sell_quote: Optional[Dict[str, Any]], cfg: Config,
                    now: Optional[float] = None,
                    providers: Optional[int] = None,
                    fomo: Optional[Any] = None) -> SafetyResult:
    now = now or time.time()
    checks: List[Check] = []

    def add(cid: str, ok: Optional[bool], detail: str, critical: bool = True) -> None:
        checks.append(Check(cid, ok, detail, critical))

    if snap is None:
        add("market", False, "no DexScreener/GeckoTerminal pair data")
    else:
        # Copycats: a convincing ticker is the cheapest scam there is.
        if cfg.reject_copycats:
            bad = name_quality(snap.symbol or (facts.symbol if facts else ""), snap.name)
            add("name_quality", bad is None, bad or "symbol/name clean: {}".format(snap.symbol))
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

    # ---- FOMO (optional). Only added when the provider actually answered.
    # An empty FOMO warnings list is NOT a pass: it only means FOMO said nothing.
    if fomo is not None and getattr(fomo, "available", False):
        warnings_obj = getattr(fomo, "warnings", None)
        if warnings_obj is not None:
            blocks = warnings_obj.blocking_reasons()
            add("fomo_tradable", not blocks,
                "; ".join(blocks) or "FOMO: buying and selling enabled")
        stats_obj = getattr(fomo, "stats", None)
        if stats_obj is not None and getattr(stats_obj, "top10_pct", -1.0) >= 0:
            add("fomo_top10", stats_obj.top10_pct <= cfg.max_top10_pct,
                "FOMO top10 {:.1f}% (max {:.0f}%)".format(stats_obj.top10_pct,
                                                          cfg.max_top10_pct))
        if cfg.fomo_veto_severe and warnings_obj is not None and warnings_obj.severe:
            add("fomo_severe", False, "FOMO flagged a SEVERE risk warning")

    # A deployer who has already sold out blocks the token even when only the
    # devs payload arrived (no stats/warnings): that is the cheapest rug signal.
    if fomo is not None and cfg.fomo_veto_dev_exit and hasattr(fomo, "dev_exit_reason"):
        dev_reason = fomo.dev_exit_reason()
        if dev_reason:
            add("fomo_dev_exit", False, dev_reason)

    # Independent corroboration: one provider can be wrong, late, or lying.
    max_prov = 4 if (fomo is not None and getattr(fomo, "available", False)) else 3
    prov = providers if providers is not None else \
        ((1 if snap else 0) + (1 if facts is not None and facts.available else 0))
    add("corroboration", prov >= cfg.min_corroboration,
        "{}/{} providers returned data (min {})".format(prov, max_prov, cfg.min_corroboration))

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



def fomo_flow_delta(fomo: Optional[Any], cfg: Config) -> Tuple[float, List[str]]:
    """
    Small, explained score bonus when FOMO's independent flow data confirms the
    tape the bot already sees. Capped by cfg.fomo_score_boost and never able to
    push a sub-threshold token over the line on its own.

    Returns (delta, notes). No FOMO data -> (0.0, []): missing is not evidence.
    """
    if fomo is None or not getattr(fomo, "available", False):
        return 0.0, []
    stats_obj = getattr(fomo, "stats", None)
    if stats_obj is None:
        return 0.0, []
    w5 = stats_obj.window("5m")
    w1h = stats_obj.window("1h")
    if w5.trades <= 0 and w1h.trades <= 0:
        return 0.0, []
    delta = 0.0
    notes: List[str] = []
    half = max(0.0, cfg.fomo_score_boost) / 2.0
    if w5.unique_buyers >= 8 and w5.ratio >= 1.3:
        delta += half
        notes.append("FOMO 5m {} buyers, {:.2f} buy/sell".format(w5.unique_buyers, w5.ratio))
    if w5.net_vol_usd > 0 and w1h.net_vol_usd > 0:
        delta += half
        notes.append("FOMO net inflow 5m ${:.0f} / 1h ${:.0f}".format(
            w5.net_vol_usd, w1h.net_vol_usd))
    return round(min(delta, cfg.fomo_score_boost), 2), notes


# =============================================================================
# SECTION 8b.  WEB RESEARCH SKILL  (Obscura headless browser, optional)
#
# The question this answers: "is anything being said about this token right now,
# and is any of it bad?" A momentum score only sees prices. A token can be
# pumping while Twitter, Reddit and the news are full of "rug", "honeypot" or
# "exploit" - that is the one thing a chart cannot show you.
#
# HOW IT IS USED (both directions, never as a promise):
#   * before a buy, a veto-grade news signal rejects the candidate
#     (reason starts with "research_veto"), and confirmed chatter can add a few
#     points - it can never turn a weak setup into a strong one on its own;
#   * while holding, a veto-grade news signal triggers an immediate exit.
#
# HONEST LIMITS (also in README):
#   * X/Twitter shows a login wall to logged-out browsers; without a cookie jar
#     (--research-storage-dir) the X source returns "blocked", and this code
#     says so instead of inventing a sentiment score.
#   * keyword matching is dumb. "not a rug" and "rug" look the same to it. That
#     is why one mention never vetoes and two independent sources are required.
#   * Obscura launches a browser per source. On a phone that costs seconds, so
#     this runs only at the confirmation stage (rare) and while holding, never
#     in the discovery loop.
#   * It is not faster than the market. News you can scrape at 25 seconds is
#     often already priced in, and a bot is the last to know.
#
# INSTALL (see README): official builds need glibc 2.35+, so on Android run it
# inside proot-distro (Ubuntu) and point OBSCURA_BIN at a wrapper script.
# =============================================================================

OBSCURA_BIN_ENV = "OBSCURA_BIN"
OBSCURA_STORAGE_ENV = "OBSCURA_STORAGE_DIR"

# Words that, on a line that also names the token, are bad news.
SCAM_WORDS = (
    "rug", "rugged", "rugpull", "rug pull", "honeypot", "honey pot", "scam",
    "exploit", "hacked", "hack", "stolen", "drained", "drainer", "blacklist",
    "blacklisted", "frozen", "cannot sell", "can't sell", "suspicious", "ponzi",
    "fake", "abandoned", "exit scam", "malicious", "phishing", "do not buy",
    "stay away", "warning",
)
# Words that are mildly positive, only counted together with real mentions.
HYPE_WORDS = (
    "listing", "listed", "partnership", "airdropping", "trending", "breaking out",
    "all-time high", "ath", "buyback", "burn", "liquidity locked", "revived",
    "accumulating", "whale buying", "just launched",
)
# A bare ticker is only a mention when the same line is about crypto.
CONTEXT_WORDS = (
    "solana", "sol ", "spl", "token", "coin", "mint", "dexscreener", "raydium",
    "pump.fun", "pumpfun", "jupiter", "chart", "liquidity", "memecoin", "meme coin",
)
BLOCKED_PAGE_MARKERS = (
    "sign in to x", "log in to x", "enable javascript", "are you a robot",
    "verify you are human", "captcha", "access denied", "unusual traffic",
    "login required", "sign in to continue",
)


@dataclass
class FetchResult:
    """One browser fetch. Never raises: failures are data, not exceptions."""
    ok: bool = False
    url: str = ""
    text: str = ""
    error: str = ""
    ms: float = 0.0
    blocked: bool = False
    from_cache: bool = False


@dataclass
class Mentions:
    """What one page said about one token."""
    mentions: int = 0
    strong: int = 0
    scam_hits: int = 0
    hype_hits: int = 0
    samples: List[str] = field(default_factory=list)
    scam_samples: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"mentions": self.mentions, "strong": self.strong,
                "scam_hits": self.scam_hits, "hype_hits": self.hype_hits,
                "samples": self.samples, "scam_samples": self.scam_samples}


@dataclass
class ResearchResult:
    """Aggregate verdict over every source that answered."""
    verdict: str = "unavailable"      # boost | neutral | veto | unavailable | disabled | timeout
    delta: float = 0.0                # score points added (boost only)
    mentions: int = 0
    scam_hits: int = 0
    hype_hits: int = 0
    sources_ok: int = 0
    sources_tried: int = 0
    blocked: int = 0
    summary: str = ""
    per_source: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def line(self) -> str:
        return "{} | {} | {} sources, {} mentions, {} scam, {} hype".format(
            self.verdict.upper(), self.summary or "-", self.sources_ok,
            self.mentions, self.scam_hits, self.hype_hits)


def _has_context(low: str) -> bool:
    return any(word in low for word in CONTEXT_WORDS)


def research_mentions(text: str, symbol: str, mint: str = "") -> Mentions:
    """
    Count how loudly one page talks about this token, line by line.

    A mention is: the mint address, a $CASHTAG, or a bare ticker on a line that
    also contains a crypto word. Scam/hype keywords only count on those same
    lines, so a page about an unrelated rug cannot veto your trade.
    """
    out = Mentions()
    sym = (symbol or "").strip().upper()
    mint = (mint or "").strip()
    if not text or (not sym and not mint):
        return out
    cashtag = re.compile(r"[$#]" + re.escape(sym) + r"\b", re.IGNORECASE) if sym else None
    bare = re.compile(r"\b" + re.escape(sym) + r"\b", re.IGNORECASE) if sym else None
    seen = set()
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line or len(line) < 4:
            continue
        low = line.lower()
        tagged = bool(cashtag and cashtag.search(line))
        bare_hit = bool(bare and bare.search(line)) and _has_context(low)
        mint_hit = bool(mint) and mint.lower() in low
        if not (tagged or bare_hit or mint_hit):
            continue
        out.mentions += 1
        if tagged or mint_hit:
            out.strong += 1
        hits_here = sum(1 for word in SCAM_WORDS if word in low)
        out.scam_hits += hits_here
        out.hype_hits += sum(1 for word in HYPE_WORDS if word in low)
        if hits_here and line not in seen and len(out.scam_samples) < 6:
            seen.add(line)
            out.scam_samples.append(line[:160])
        elif line not in seen and len(out.samples) < 6:
            seen.add(line)
            out.samples.append(line[:160])
    return out


def research_sources(cfg: Config, symbol: str, mint: str = "") -> List[Dict[str, str]]:
    """
    Where to look. Public, no-login pages first; X needs cookies so it is only
    included when a storage directory is configured. Ordered by usefulness.
    """
    sym = (symbol or "").strip()
    query = urllib.parse.quote_plus('"${}" solana'.format(sym) if sym else mint)
    out: List[Dict[str, str]] = []
    if sym or mint:
        out.append({"name": "dexscreener",
                    "url": "https://dexscreener.com/solana/{}".format(mint or sym),
                    "dump": "markdown", "own": "1"})
        out.append({"name": "duckduckgo",
                    "url": "https://html.duckduckgo.com/html/?q=" + query,
                    "dump": "markdown"})
        out.append({"name": "bing",
                    "url": "https://www.bing.com/search?q=" + query,
                    "dump": "markdown"})
        out.append({"name": "google_news",
                    "url": "https://news.google.com/rss/search?q=" + query,
                    "dump": "original"})
        out.append({"name": "reddit",
                    "url": "https://old.reddit.com/search?q=" + query + "&sort=new&t=week",
                    "dump": "markdown"})
        if cfg.research_storage_dir or os.environ.get(OBSCURA_STORAGE_ENV):
            out.append({"name": "x_search",
                        "url": "https://x.com/search?q=" + query + "&f=live",
                        "dump": "markdown"})
        for channel in [c.strip() for c in (cfg.research_channels or "").split(",") if c.strip()]:
            out.append({"name": "telegram:" + channel[:20],
                        "url": "https://t.me/s/" + channel.strip().lstrip("@"),
                        "dump": "markdown"})
    return out[: max(1, int(cfg.research_max_sources))]


def research_verdict(per_source: Dict[str, Dict[str, Any]], cfg: Config) -> ResearchResult:
    """
    Combine per-source mention counts into one decision.

    Deliberately conservative:
      * veto needs either >= research_scam_min_sources independent pages with a
        scam word on a token line, or >= research_scam_veto_hits total hits;
      * a boost needs real chatter with zero scam words;
      * one silent page is never evidence of anything, so "no sources answered"
        reports "unavailable" instead of a fake neutral.
    """
    res = ResearchResult(sources_tried=len(per_source))
    notes: List[str] = []
    scam_sources = 0
    for name, data in per_source.items():
        if not isinstance(data, dict) or not data.get("ok"):
            if isinstance(data, dict) and data.get("blocked"):
                res.blocked += 1
            continue
        res.sources_ok += 1
        res.mentions += int(fnum(data.get("mentions")))
        res.scam_hits += int(fnum(data.get("scam_hits")))
        res.hype_hits += int(fnum(data.get("hype_hits")))
        if int(fnum(data.get("scam_hits"))) > 0:
            scam_sources += 1
            samples = data.get("scam_samples") or data.get("samples") or []
            if samples:
                notes.append("{}: {}".format(name, samples[0][:130]))
    res.per_source = per_source
    res.notes = notes

    if res.sources_ok == 0:
        res.verdict = "unavailable"
        res.summary = ("no source answered ({}/{} blocked)".format(res.blocked, res.sources_tried)
                       if res.sources_tried else "no sources configured")
        return res

    if (scam_sources >= max(1, int(cfg.research_scam_min_sources))
            or res.scam_hits >= max(1, int(cfg.research_scam_veto_hits))):
        res.verdict = "veto"
        res.summary = "bad news on {}/{} pages ({} hits): {}".format(
            scam_sources, res.sources_ok, res.scam_hits,
            notes[0][:110] if notes else "scam keywords next to the token")
    elif res.scam_hits == 0 and res.mentions >= cfg.research_boost_min_mentions and res.hype_hits >= 1:
        res.verdict = "boost"
        res.delta = cfg.research_boost_points
        res.summary = "{} mentions, {} hype words, no scam words".format(
            res.mentions, res.hype_hits)
    else:
        res.verdict = "neutral"
        res.summary = "{} mentions, {} scam, {} hype".format(
            res.mentions, res.scam_hits, res.hype_hits)
    return res


def _blocked_page(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in BLOCKED_PAGE_MARKERS) and len(text) < 20000


class Obscura:
    """
    Thin, rate-limited, cached wrapper around the `obscura` CLI.

    One browser launch at a time by default: this is meant to run on a phone
    while 100 asyncio agents are working. Every failure mode returns a
    FetchResult with ok=False; nothing here can crash the swarm.
    """

    def __init__(self, cfg: Config, runner: Optional[Callable[[List[str], float], Tuple[int, str, str]]] = None):
        self.cfg = cfg
        self._runner = runner or self._subprocess_runner
        self._cache: Dict[str, Tuple[float, FetchResult]] = {}
        self._sem = asyncio.Semaphore(max(1, int(cfg.research_concurrency)))
        self._pace_lock = asyncio.Lock()
        self._last_launch = 0.0
        self.bin = self.locate()
        self.stats = {"fetches": 0, "ok": 0, "errors": 0, "cache_hits": 0,
                      "timeouts": 0, "blocked": 0, "launches": 0}

    # ---------------------------------------------------------------- discovery
    def locate(self) -> str:
        for candidate in (self.cfg.obscura_bin, os.environ.get(OBSCURA_BIN_ENV, "")):
            candidate = (candidate or "").strip()
            if candidate and os.path.exists(candidate) and os.access(candidate, os.X_OK):
                return candidate
        found = shutil.which("obscura")
        if found:
            return found
        local = os.path.join(os.getcwd(), "obscura")
        if os.path.exists(local) and os.access(local, os.X_OK):
            return local
        return ""

    @property
    def available(self) -> bool:
        return bool(self.bin) and bool(self.cfg.research_enabled)

    def storage_dir(self) -> str:
        return (self.cfg.research_storage_dir
                or os.environ.get(OBSCURA_STORAGE_ENV, "")).strip()

    def command(self, url: str, dump: str = "markdown") -> List[str]:
        args = [self.bin or "obscura", "fetch", url, "--dump", dump, "--quiet",
                "--timeout", "{:.0f}".format(max(5.0, self.cfg.research_timeout_s))]
        if self.cfg.research_stealth:
            args.append("--stealth")
        if self.cfg.research_obey_robots:
            args.append("--obey-robots")
        if self.storage_dir():
            args += ["--storage-dir", self.storage_dir()]
        return args

    @staticmethod
    def _subprocess_runner(args: List[str], timeout: float) -> Tuple[int, str, str]:
        """Blocking; always called through asyncio.to_thread."""
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                  errors="replace")
            return proc.returncode, proc.stdout or "", (proc.stderr or "").strip()[-400:]
        except subprocess.TimeoutExpired:
            return 124, "", "timeout after {:.0f}s".format(timeout)
        except OSError as exc:
            return 127, "", str(exc)[:200]

    # ------------------------------------------------------------------- fetch
    async def fetch(self, url: str, dump: str = "markdown") -> FetchResult:
        if not self.available:
            return FetchResult(url=url, error="obscura not installed or research disabled")
        now = time.time()
        cached = self._cache.get(url)
        if cached and now - cached[0] < self.cfg.research_cache_ttl_s:
            self.stats["cache_hits"] += 1
            hit = replace(cached[1], from_cache=True)
            return hit
        args = self.command(url, dump)
        timeout = max(10.0, self.cfg.research_timeout_s + 15.0)
        started = time.time()
        async with self._sem:
            async with self._pace_lock:
                wait = self.cfg.research_min_interval_s - (time.time() - self._last_launch)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_launch = time.time()
                self.stats["launches"] += 1
            try:
                rc, out, err = await asyncio.to_thread(self._runner, args, timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:                      # noqa: BLE001 - never crash the swarm
                rc, out, err = 1, "", str(exc)[:200]
        ms = (time.time() - started) * 1000.0
        text = (out or "").strip()
        self.stats["fetches"] += 1
        if rc != 0 or not text:
            if rc == 124:
                self.stats["timeouts"] += 1
            else:
                self.stats["errors"] += 1
            result = FetchResult(url=url, error=(err or "empty page")[:200], ms=ms)
        elif _blocked_page(text):
            self.stats["blocked"] += 1
            result = FetchResult(url=url, blocked=True, ms=ms,
                                 error="login/captcha wall", text=text[:2000])
        else:
            self.stats["ok"] += 1
            result = FetchResult(url=url, ok=True, text=text[:400000], ms=ms)
        self._cache[url] = (time.time(), result)
        return result

    async def fetch_many(self, sources: Sequence[Dict[str, str]]) -> Dict[str, FetchResult]:
        """Fetch every source in parallel (bounded by research_concurrency)."""
        tasks = {src["name"]: asyncio.create_task(self.fetch(src["url"], src.get("dump", "markdown")))
                 for src in sources}
        out: Dict[str, FetchResult] = {}
        for name, task in tasks.items():
            try:
                out[name] = await asyncio.wait_for(
                    task, timeout=self.cfg.research_timeout_s + 20.0)
            except asyncio.TimeoutError:
                out[name] = FetchResult(error="timeout")
            except asyncio.CancelledError:
                raise
            except Exception as exc:                      # noqa: BLE001
                out[name] = FetchResult(error=str(exc)[:200])
        return out

    def status_line(self) -> str:
        if not self.cfg.research_enabled:
            return "research: disabled"
        if not self.bin:
            return "research: unavailable (no obscura binary; see README)"
        return ("research: {} | {} ok, {} err, {} blocked, {} cache hits").format(
            self.bin, self.stats["ok"], self.stats["errors"],
            self.stats["blocked"], self.stats["cache_hits"])


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
    async def can_open(self, mint: str, symbol: str = "") -> Tuple[bool, str]:
        async with self.lock:
            self.rollover()
            if self.halted:
                return False, "halted: {}".format(self.halt_reason or "risk manager")
            open_positions = self.pf.open_positions()
            open_mints = {p.mint for p in open_positions}
            if mint in open_mints:
                return False, "already_open (never average down)"
            if symbol:
                same = sum(1 for p in open_positions if p.symbol.upper() == symbol.upper())
                if same >= self.cfg.max_positions_per_symbol:
                    return False, "clone guard: {!r} already open".format(symbol)
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
# SECTION 13b.  SIGNAL MODEL, SIGNAL BOOK AND TERMINAL CARDS
#
# A "signal" here is a research record, not an order. It answers, for one mint:
# what is it, what does the market look like, what did the safety filters say,
# how strong is the momentum setup, where would a trade be entered/stopped/
# scaled out, and how fresh is all of that.
#
# Statuses and what they mean:
#   WATCH        safe enough to look at, momentum below the entry threshold
#   CANDIDATE    momentum + tape pass, waiting for the second look
#   CONFIRMED    the second look passed everything (the only "confirmed" state)
#   REJECTED     a safety filter or the second look failed; reasons attached
#   INVALIDATED  was CONFIRMED, price has now broken the invalidation level
#   EXPIRED      too old to act on (unconfirmed past signal_expiry_s, or
#                confirmed past confirmed_expiry_s)
#
# NOTHING HERE IS A GUARANTEE. Entry/invalidation/targets are arithmetic from
# the exit rules already in Config, not predictions. Fees at $2 size are large.
# =============================================================================

SIGNAL_WATCH = "WATCH"
SIGNAL_CANDIDATE = "CANDIDATE"
SIGNAL_CONFIRMED = "CONFIRMED"
SIGNAL_REJECTED = "REJECTED"
SIGNAL_INVALIDATED = "INVALIDATED"
SIGNAL_EXPIRED = "EXPIRED"

SIGNAL_STATUSES = (SIGNAL_CONFIRMED, SIGNAL_CANDIDATE, SIGNAL_WATCH,
                   SIGNAL_INVALIDATED, SIGNAL_REJECTED, SIGNAL_EXPIRED)
SIGNAL_ALERTABLE = (SIGNAL_CONFIRMED, SIGNAL_INVALIDATED)
_STATUS_RANK = {SIGNAL_CONFIRMED: 0, SIGNAL_CANDIDATE: 1, SIGNAL_WATCH: 2,
                SIGNAL_INVALIDATED: 3, SIGNAL_REJECTED: 4, SIGNAL_EXPIRED: 5}


def _fit(text: str, width: int) -> str:
    """Truncate with an ellipsis so a phone terminal never wraps mid-number."""
    text = " ".join(str(text).split())
    if width <= 3 or len(text) <= width:
        return text
    return text[: width - 3] + "..."


@dataclass
class Signal:
    """Everything the terminal/Telegram card needs about one candidate."""
    mint: str
    symbol: str = ""
    name: str = ""
    status: str = SIGNAL_WATCH
    price: float = 0.0
    price_ts: float = 0.0
    fdv_usd: float = 0.0
    liquidity_usd: float = 0.0
    volume_5m: float = 0.0
    volume_1h: float = 0.0
    change_5m: float = 0.0
    change_1h: float = 0.0
    change_24h: float = 0.0
    buys_5m: int = 0
    sells_5m: int = 0
    unique_buyers_5m: int = 0
    score: float = 0.0
    score_parts: Dict[str, float] = field(default_factory=dict)
    score_notes: List[str] = field(default_factory=list)
    safety_ok: bool = False
    holder_note: str = ""
    authority_note: str = ""
    lp_note: str = ""
    safety_passed: int = 0
    safety_total: int = 0
    safety_failures: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    reject_reasons: List[str] = field(default_factory=list)
    entry_low: float = 0.0
    entry_high: float = 0.0
    invalidation: float = 0.0
    target_1: float = 0.0
    target_2: float = 0.0
    rr: float = 0.0
    sources: List[str] = field(default_factory=list)
    data_age_s: float = 0.0
    first_seen: float = 0.0
    confirmed_at: float = 0.0
    expires_at: float = 0.0
    fomo: Optional[Dict[str, Any]] = None
    fomo_notes: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------ helpers
    def signal_id(self) -> str:
        return "{}:{}".format(self.mint, int(self.first_seen or 0))

    def age_s(self, now: Optional[float] = None) -> float:
        now = now or time.time()
        return max(0.0, now - (self.first_seen or now))

    def data_age_s_now(self, now: Optional[float] = None) -> float:
        now = now or time.time()
        return max(0.0, now - (self.price_ts or now))

    def is_actionable(self) -> bool:
        return self.status == SIGNAL_CONFIRMED

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.signal_id(), "mint": self.mint, "symbol": self.symbol,
            "name": self.name, "status": self.status, "price": self.price,
            "price_ts": self.price_ts, "fdv_usd": self.fdv_usd,
            "liquidity_usd": self.liquidity_usd, "volume_5m": self.volume_5m,
            "volume_1h": self.volume_1h, "change_5m": self.change_5m,
            "change_1h": self.change_1h, "change_24h": self.change_24h,
            "buys_5m": self.buys_5m, "sells_5m": self.sells_5m,
            "unique_buyers_5m": self.unique_buyers_5m, "score": self.score,
            "score_parts": self.score_parts, "safety_ok": self.safety_ok,
            "safety_failures": self.safety_failures, "warnings": self.warnings,
            "reject_reasons": self.reject_reasons, "entry_low": self.entry_low,
            "entry_high": self.entry_high, "invalidation": self.invalidation,
            "target_1": self.target_1, "target_2": self.target_2, "rr": self.rr,
            "sources": self.sources, "data_age_s": round(self.data_age_s, 1),
            "first_seen": self.first_seen, "confirmed_at": self.confirmed_at,
            "expires_at": self.expires_at, "fomo": self.fomo,
            "fomo_notes": self.fomo_notes,
        }

    # -------------------------------------------------------------- rendering
    def card(self, width: int = 58, now: Optional[float] = None) -> str:
        """ASCII card that survives a 60-column Termux window."""
        now = now or time.time()
        inner = width - 2
        line = "| " + "-" * (inner - 2) + " |"

        def row(label: str, value: str) -> str:
            text = _fit("{} {}".format(label.ljust(6), value), inner - 2)
            return "| {:<{w}} |".format(text, w=inner - 2)

        head = "{}  ${}  score {:.0f}".format(self.status, self.symbol or "?", self.score)
        out = ["+" + "-" * inner + "+",
               "| " + _fit(head, inner - 2).ljust(inner - 2) + " |"]
        out.append(line)
        valid = (fomo_normalize_mint or (lambda x: x))(self.mint) == self.mint
        out.append(row("mint", _fit(self.mint, inner - 9)))
        if not valid:
            out.append(row("", "WARNING: not a valid Solana mint address"))
        if self.name:
            out.append(row("name", self.name))
        out.append(row("price", "${}  ({:.0f}s old)".format(
            fmt_price(self.price), self.data_age_s_now(now))))
        out.append(row("FDV", "{}   LIQ {}".format(
            fmt_usd(self.fdv_usd) if self.fdv_usd else "n/a", fmt_usd(self.liquidity_usd))))
        out.append(row("5m", "{}   vol {}".format(fmt_pct(self.change_5m),
                                                  fmt_usd(self.volume_5m))))
        out.append(row("1h", "{}   vol {}".format(fmt_pct(self.change_1h),
                                                  fmt_usd(self.volume_1h))))
        out.append(row("tape", "{} buys / {} sells{}".format(
            self.buys_5m, self.sells_5m,
            "  ({} buyers)".format(self.unique_buyers_5m) if self.unique_buyers_5m else "")))
        if self.score_parts:
            top = sorted(self.score_parts.items(), key=lambda kv: -kv[1])[:3]
            out.append(row("why", " ".join("{} {:.0f}".format(k.replace("_", " "), v)
                                           for k, v in top)))
        out.append(line)
        safety = ("{} checks passed".format(self.safety_passed) if self.safety_ok
                  else "FAILED: " + "; ".join(self.safety_failures[:2]))
        out.append(row("safety", safety))
        audit = " | ".join(x for x in (self.holder_note, self.lp_note,
                                       self.authority_note) if x)
        if audit:
            out.append(row("audit", audit))
        if self.warnings:
            out.append(row("warn", "; ".join(self.warnings[:2])))
        if self.fomo_notes:
            out.append(row("fomo", "; ".join(self.fomo_notes[:2])))
        if self.reject_reasons:
            out.append(row("reject", "; ".join(self.reject_reasons[:2])))
        out.append(line)
        if self.price > 0:
            out.append(row("ENTRY", "${} - ${}".format(fmt_price(self.entry_low),
                                                       fmt_price(self.entry_high))))
            out.append(row("INVAL", "${}  (-{:.0f}% stop rule)".format(
                fmt_price(self.invalidation), self.cfg_stop_pct)))
            out.append(row("TP1", "${}  (+{:.0f}%, sell {:.0f}%)".format(
                fmt_price(self.target_1), self.cfg_tp1_pct, self.cfg_tp1_frac * 100)))
            out.append(row("TP2", "${}  (+{:.0f}%, sell {:.0f}%)".format(
                fmt_price(self.target_2), self.cfg_tp2_pct, self.cfg_tp2_frac * 100)))
            out.append(row("R:R", "{:.1f} : 1 to TP1 (before fees/slippage)".format(self.rr)))
        else:
            out.append(row("ENTRY", "n/a (no price)"))
        out.append(line)
        out.append(row("assume", "paper fills | 3% slip | $0.34 fees/RT"))
        out.append(row("src", "{} | age {:.0f}s".format(
            "+".join(self.sources) if self.sources else "n/a", self.data_age_s_now(now))))
        out.append(row("asof", utc_str(now)))
        out.append("+" + "-" * inner + "+")
        out.append("| " + _fit("PAPER MODE - RESEARCH ONLY, NO REAL TRADE", inner - 2)
                   .ljust(inner - 2) + " |")
        out.append("+" + "-" * inner + "+")
        return "\n".join(out)

    # defaults filled in by build_signal(); kept as attributes so card() is pure
    cfg_stop_pct: float = 20.0
    cfg_tp1_pct: float = 100.0
    cfg_tp2_pct: float = 300.0
    cfg_tp1_frac: float = 0.5
    cfg_tp2_frac: float = 0.25

    def telegram(self, now: Optional[float] = None) -> str:
        """Plain-text Telegram body (no parse_mode, so no escaping surprises)."""
        now = now or time.time()
        kind = "CONFIRMED SIGNAL" if self.status == SIGNAL_CONFIRMED else \
            "SIGNAL " + self.status
        rows = [
            "[{}] {}".format(kind, self.symbol or short_addr(self.mint)),
            "mint: {}".format(self.mint),
            "score: {:.0f}/100".format(self.score),
            "price: ${}  ({} ago)".format(fmt_price(self.price),
                                          _ago(self.data_age_s_now(now))),
            "FDV: {}   liquidity: {}".format(
                fmt_usd(self.fdv_usd) if self.fdv_usd else "n/a", fmt_usd(self.liquidity_usd)),
            "5m: {}  vol {}   |   1h: {}  vol {}".format(
                fmt_pct(self.change_5m), fmt_usd(self.volume_5m),
                fmt_pct(self.change_1h), fmt_usd(self.volume_1h)),
            "tape: {} buys / {} sells".format(self.buys_5m, self.sells_5m),
        ]
        if self.score_notes:
            rows.append("why: " + "; ".join(self.score_notes[:3]))
        if self.safety_ok:
            rows.append("safety: {} checks passed{}".format(
                self.safety_passed,
                " ({})".format(" | ".join(x for x in (self.holder_note, self.lp_note,
                                                      self.authority_note) if x))
                if any((self.holder_note, self.lp_note, self.authority_note)) else ""))
        else:
            rows.append("safety FAILED: " + "; ".join(self.safety_failures[:3]))
        if self.fomo_notes:
            rows.append("fomo: " + "; ".join(self.fomo_notes[:2]))
        if self.warnings:
            rows.append("warnings: " + "; ".join(self.warnings[:3]))
        if self.reject_reasons:
            rows.append("rejected: " + "; ".join(self.reject_reasons[:3]))
        if self.price > 0:
            rows += [
                "entry idea: ${} - ${}".format(fmt_price(self.entry_low),
                                                fmt_price(self.entry_high)),
                "invalidation: ${} (-{:.0f}%)".format(fmt_price(self.invalidation),
                                                           self.cfg_stop_pct),
                "targets: ${} (+{:.0f}%) / ${} (+{:.0f}%)".format(
                    fmt_price(self.target_1), self.cfg_tp1_pct,
                    fmt_price(self.target_2), self.cfg_tp2_pct),
                "R:R {:.1f}:1 to TP1 before fees".format(self.rr),
            ]
        rows += [
            "sources: {}".format("+".join(self.sources) if self.sources else "n/a"),
            "assumptions: paper fills, 3% entry-slip cap, ~$0.34 fixed fees per",
            "round trip at $2 size, no MEV/failed-tx modelling",
            "data age: {:.0f}s  |  asof {}".format(self.data_age_s_now(now), utc_str(now)),
            "",
            "PAPER MODE - NO REAL TRADE EXECUTED",
            "Not financial advice. Entry/stop/targets are arithmetic from the",
            "configured exit rules, not predictions. Most meme coins go to zero.",
        ]
        return "\n".join(rows)


def _ago(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return "{:.0f}s".format(seconds)
    if seconds < 5400:
        return "{:.0f}m".format(seconds / 60.0)
    return "{:.1f}h".format(seconds / 3600.0)


def build_signal(cfg: Config, mint: str, snap: Optional[MarketSnapshot],
                 facts: Optional[TokenFacts], micro: Optional[MicroStats],
                 momentum: Optional[MomentumResult], safety: Optional[SafetyResult],
                 fomo: Optional[Any] = None, source: str = "",
                 now: Optional[float] = None, first_seen: float = 0.0,
                 status: str = "", fomo_delta: float = 0.0,
                 fomo_notes: Optional[List[str]] = None) -> Signal:
    """
    Compose one Signal out of already-fetched pieces. Pure: no I/O, easy to test.

    `status` is only honoured for REJECTED-style overrides; otherwise the status
    follows from the evidence (safety fail -> REJECTED, weak score -> WATCH,
    strong score -> CANDIDATE). Only the confirmation step can produce
    CONFIRMED, and it does so by calling this again with status=SIGNAL_CONFIRMED.
    """
    now = now or time.time()
    sig = Signal(mint=mint, first_seen=first_seen or now, price_ts=now)
    stop_pct = abs(cfg.stop_loss_pct)
    sig.cfg_stop_pct = stop_pct
    sig.cfg_tp1_pct = cfg.tp1_pct
    sig.cfg_tp2_pct = cfg.tp2_pct
    sig.cfg_tp1_frac = cfg.tp1_frac
    sig.cfg_tp2_frac = cfg.tp2_frac

    if snap is not None:
        sig.symbol = snap.symbol or ""
        sig.name = snap.name or ""
        sig.price = snap.price_usd
        sig.price_ts = snap.ts or now
        sig.fdv_usd = snap.fdv_usd
        sig.liquidity_usd = snap.liquidity_usd
        sig.volume_5m = snap.volume_5m
        sig.volume_1h = snap.volume_1h
        sig.change_5m = snap.price_change_5m
        sig.change_1h = snap.price_change_1h
        sig.change_24h = snap.price_change_24h
        sig.buys_5m = snap.buys_5m
        sig.sells_5m = snap.sells_5m
        if snap.source:
            sig.sources.append(snap.source)
    if facts is not None and facts.symbol and not sig.symbol:
        sig.symbol = facts.symbol
    if micro is not None:
        if micro.buys_5m or micro.sells_5m:
            sig.buys_5m, sig.sells_5m = micro.buys_5m, micro.sells_5m
        sig.unique_buyers_5m = micro.buyers_5m
        if micro.trades_seen:
            sig.sources.append("geckoterminal:tape")
    if facts is not None and facts.available:
        if "rugcheck" not in sig.sources:
            sig.sources.append("rugcheck")
        sig.holder_note = ("top10 {:.1f}%".format(facts.top10_pct)
                           if facts.top10_pct >= 0 else "top10 unknown")
        sig.authority_note = ("auth revoked"
                              if facts.mint_authority in (None, "")
                              and facts.freeze_authority in (None, "")
                              else "AUTHORITY LIVE")
        sig.lp_note = ("LP {:.0f}% lock".format(facts.lp_locked_pct)
                       if facts.lp_locked_pct >= 0 else "LP unknown")

    # ---- score
    if momentum is not None:
        sig.score = momentum.score
        sig.score_parts = dict(momentum.components)
        sig.score_notes = list(momentum.notes)
        sig.score = clip(sig.score + fomo_delta, 0.0, 100.0)
        if fomo_delta:
            sig.score_notes = sig.score_notes + [
                "+{:.1f} FOMO flow confirmation".format(fomo_delta)]
    sig.fomo_notes = list(fomo_notes or [])

    # ---- safety
    if safety is not None:
        sig.safety_ok = safety.passed
        sig.safety_total = len(safety.checks)
        sig.safety_passed = sum(1 for c in safety.checks if c.ok is True)
        sig.safety_failures = list(safety.reasons)
        if not safety.passed:
            sig.reject_reasons = list(safety.reasons)
        for check in safety.checks:
            if check.ok is None and check.critical:
                sig.warnings.append("unknown: {}".format(check.cid))
            elif check.ok is False and check.cid not in ("liquidity", "top10"):
                sig.warnings.append("{}: {}".format(check.cid, _fit(check.detail, 60)))

    # ---- fomo payload (kept for display/debug; values already normalized)
    if fomo is not None and getattr(fomo, "available", False):
        sig.fomo = fomo.as_dict()
        if "fomo" not in sig.sources:
            sig.sources.append("fomo")
        if getattr(fomo, "warnings", None) is not None:
            for item in fomo.warnings.items[:3]:
                if item.get("severity") in ("SEVERE", "MODERATE"):
                    sig.warnings.append("{}: {}".format(item.get("name") or "warning",
                                                        item.get("type") or ""))
        if not getattr(fomo, "complete", False):
            sig.warnings.append("FOMO data partial")

    # ---- levels: arithmetic from the exit rules, not predictions
    if sig.price > 0:
        slip = cfg.entry_slip_cap_bps / 10000.0
        sig.entry_low = sig.price * (1.0 - slip)
        sig.entry_high = sig.price * (1.0 + 0.005)
        mid = (sig.entry_low + sig.entry_high) / 2.0
        sig.invalidation = mid * (1.0 - stop_pct / 100.0)
        sig.target_1 = mid * (1.0 + cfg.tp1_pct / 100.0)
        sig.target_2 = mid * (1.0 + cfg.tp2_pct / 100.0)
        risk = mid - sig.invalidation
        sig.rr = ((sig.target_1 - mid) / risk) if risk > 0 else 0.0
    sig.data_age_s = max(0.0, now - sig.price_ts) if sig.price_ts else 0.0
    sig.expires_at = sig.first_seen + cfg.signal_expiry_s

    # ---- status
    if status:
        sig.status = status
    elif safety is not None and not safety.passed:
        sig.status = SIGNAL_REJECTED
    elif momentum is not None and momentum.veto:
        sig.status = SIGNAL_REJECTED
        sig.reject_reasons = sig.reject_reasons + ["momentum veto: " + momentum.veto]
    elif momentum is not None and momentum.score < cfg.momentum_threshold:
        sig.status = SIGNAL_WATCH
    elif momentum is not None:
        sig.status = SIGNAL_CANDIDATE
    else:
        sig.status = SIGNAL_WATCH
    return sig


class SignalBook:
    """
    In-memory registry with status transitions, expiry and alert de-duplication.

    De-duplication is per (mint, status) with a cooldown, which is what stops a
    confirmed signal from being re-sent on every refresh, while still allowing a
    later INVALIDATED notice for the same mint.
    """

    def __init__(self, cfg: Config, now: Optional[float] = None):
        self.cfg = cfg
        self.started = now or time.time()
        self.signals: Dict[str, Signal] = {}
        self.alerted: Dict[str, float] = {}
        self.confirmed_expiry_s = max(cfg.signal_expiry_s * 2.0, 7200.0)

    # ------------------------------------------------------------------ updates
    def upsert(self, sig: Signal, now: Optional[float] = None) -> Signal:
        now = now or time.time()
        prev = self.signals.get(sig.mint)
        if prev is not None:
            sig.first_seen = prev.first_seen or sig.first_seen
            sig.expires_at = prev.expires_at or sig.expires_at
            if prev.status == SIGNAL_CONFIRMED:
                broken = (sig.price > 0 and prev.invalidation > 0
                          and sig.price < prev.invalidation)
                too_old = (now - (prev.confirmed_at or now)) > self.confirmed_expiry_s
                if broken or sig.status == SIGNAL_REJECTED:
                    # A live confirmation is only kept while the setup survives:
                    # price breaking the invalidation OR any safety filter now
                    # failing both invalidate it.
                    sig.status = SIGNAL_INVALIDATED
                    if broken:
                        sig.reject_reasons = sig.reject_reasons + [
                            "price ${:.10g} below invalidation ${:.10g}".format(
                                sig.price, prev.invalidation)]
                    if not sig.reject_reasons:
                        sig.reject_reasons = ["setup no longer valid on re-check"]
                    sig.entry_low, sig.entry_high = prev.entry_low, prev.entry_high
                    sig.invalidation = prev.invalidation
                    sig.target_1, sig.target_2 = prev.target_1, prev.target_2
                    sig.confirmed_at = prev.confirmed_at
                elif too_old:
                    sig.status = SIGNAL_EXPIRED
                    sig.confirmed_at = prev.confirmed_at
                elif sig.status in (SIGNAL_WATCH, SIGNAL_CANDIDATE):
                    # never silently downgrade a live confirmation
                    sig.status = SIGNAL_CONFIRMED
                    sig.confirmed_at = prev.confirmed_at
                    sig.entry_low, sig.entry_high = prev.entry_low, prev.entry_high
                    sig.invalidation = prev.invalidation
                    sig.target_1, sig.target_2 = prev.target_1, prev.target_2
                    sig.rr = prev.rr
            elif sig.status in (SIGNAL_WATCH, SIGNAL_CANDIDATE) and now > sig.expires_at:
                sig.status = SIGNAL_EXPIRED
        elif sig.status in (SIGNAL_WATCH, SIGNAL_CANDIDATE) and now > sig.expires_at:
            sig.status = SIGNAL_EXPIRED

        if sig.status == SIGNAL_CONFIRMED and not sig.confirmed_at:
            sig.confirmed_at = now
        self.signals[sig.mint] = sig
        return sig

    def confirm(self, sig: Signal, now: Optional[float] = None) -> Signal:
        sig.status = SIGNAL_CONFIRMED
        sig.confirmed_at = now or time.time()
        return self.upsert(sig, now)

    # ------------------------------------------------------------------- query
    def by_status(self, status: str) -> List[Signal]:
        return [s for s in self.signals.values() if s.status == status]

    def ranked(self, include_expired: bool = False) -> List[Signal]:
        rows = [s for s in self.signals.values()
                if include_expired or s.status != SIGNAL_EXPIRED]
        return sorted(rows, key=lambda s: (_STATUS_RANK.get(s.status, 9), -s.score))

    def prune(self, now: Optional[float] = None, keep_s: float = 86400.0) -> int:
        now = now or time.time()
        drop = [m for m, s in self.signals.items()
                if s.status in (SIGNAL_EXPIRED, SIGNAL_REJECTED)
                and now - (s.first_seen or now) > keep_s]
        for mint in drop:
            self.signals.pop(mint, None)
        return len(drop)

    # ------------------------------------------------------------------ alerts
    def should_alert(self, sig: Signal, now: Optional[float] = None) -> bool:
        now = now or time.time()
        if sig.status not in SIGNAL_ALERTABLE:
            return False
        key = "{}:{}".format(sig.mint, sig.status)
        last = self.alerted.get(key, 0.0)
        if now - last < self.cfg.telegram_signal_cooldown_s:
            return False
        self.alerted[key] = now
        return True

    def save(self, path: str, now: Optional[float] = None) -> bool:
        """Atomic snapshot of the current book; failure never stops a scan."""
        now = now or time.time()
        data = {
            "saved_at_utc": utc_str(now),
            "mode": "PAPER (research only)",
            "counts": {st: len(self.by_status(st)) for st in SIGNAL_STATUSES},
            "signals": [s.as_dict() for s in self.ranked(include_expired=True)][:100],
        }
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            return True
        except OSError as exc:
            log.debug("cannot write %s: %s", path, exc)
            return False


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
    "Obscura      OPTIONAL web research via a headless browser: DuckDuckGo, Bing,",
    "             Google News, Reddit, DexScreener page, public t.me channels",
    "FOMO         OPTIONAL token flow/warnings via fomoapi.io (independent service;",
    "             needs FOMO_API_KEY). fomo.family itself has no public API.",
    "             Endpoints: /v2/token/{mint}/stats, /warnings, /devs",
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
        self.pending: Dict[str, Dict[str, Any]] = {}      # mint -> awaiting 2nd look
        self.reject_counts: Dict[str, int] = {}
        self.entries = 0
        self.sells = 0
        self.recent_events: List[str] = []
        self.report_written = False
        self._alerted: Dict[str, float] = {}
        self.obscura = Obscura(cfg)
        self._research_cache: Dict[str, Tuple[float, ResearchResult]] = {}
        self.research_runs = 0
        self.research_vetoes = 0
        self.research_boosts = 0
        self.recent_signals: List[Dict[str, Any]] = []      # dashboard feed
        self.last_refresh = 0.0

    # ------------------------------------------------------------------ helper
    def note_signal(self, symbol: str, mint: str, status: str, score: float = 0.0) -> None:
        """Keep the last few signal events for the compact dashboard."""
        self.recent_signals.append({"ts": time.time(), "symbol": symbol, "mint": mint,
                                    "status": status, "score": score})
        if len(self.recent_signals) > 40:
            del self.recent_signals[:-40]
        self.last_refresh = time.time()

    def note(self, text: str) -> None:
        self.recent_events.append("{} {}".format(utc_str()[11:19], text))
        if len(self.recent_events) > 60:
            del self.recent_events[:-60]

    def count_reject(self, reasons: Sequence[str]) -> None:
        for r in reasons:
            key = r.split(":")[0].strip()
            self.reject_counts[key] = self.reject_counts.get(key, 0) + 1

    # ------------------------------------------------------------------ banner
    def _fomo_banner_line(self) -> str:
        if FomoProvider is None:
            return "unavailable (fomo_provider.py not installed)"
        health = self.sources.fomo_health()
        if not health.get("available"):
            return "off - {} (all FOMO fields show n/a)".format(health.get("status", "off"))
        return "on - independent fomoapi.io, {} credits spent, {} tokens".format(
            health.get("credits_spent", 0), health.get("tokens_enriched", 0))

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
            " fomo provider    : " + self._fomo_banner_line(),
            " web research     : " + (self.obscura.status_line()[10:]
                                      if self.obscura.status_line().startswith("research: ")
                                      else self.obscura.status_line()),
            " signal quality   : momentum>={:.0f} | {}+ providers | 2nd look after {:.0f}s | "
            "tape>={} trades, {} buyers, {} volume".format(
                self.cfg.momentum_threshold, self.cfg.min_corroboration,
                self.cfg.confirm_delay_s, self.cfg.min_trades_5m,
                self.cfg.min_unique_buyers_5m, fmt_usd(self.cfg.min_tape_volume_5m_usd)),
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
            " honest note      : these are high-conviction entries, NOT sure signals.",
            "                    No such thing exists. Most meme coins go to zero;",
            "                    you can lose the whole {}.".format(fmt_usd(self.cfg.start_capital_usd)),
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
        """Honeypot check #2, delegated so scan and trade use the same rule."""
        return await self.sources.simulate_sell_quote(mint, price, facts)

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
                pool = None
                try:
                    pool = await self.sources.geckoterminal_pool_for_token(mint)
                except HttpError:
                    pool = None
                if snap is not None:
                    if pool:
                        snap = self.sources.apply_gt_pool(snap, pool)
                elif pool:
                    snap = self.sources.snapshot_from_gt(mint, pool)
                facts = await self.sources.rugcheck_facts(mint)
                price = snap.price_usd if snap else 0.0
                sell_quote = await self.simulate_sell_quote(mint, price, facts)
                fomo = await self.sources.fomo_enrich(mint,
                                                      include_slow=self.cfg.fomo_include_slow)
                providers = ((1 if snap is not None else 0) + (1 if pool else 0) +
                             (1 if facts.available else 0) +
                             (1 if fomo is not None and fomo.available else 0))
                result = evaluate_safety(snap, facts, sell_quote, self.cfg,
                                         providers=providers, fomo=fomo)
                symbol = (snap.symbol if snap else "") or facts.symbol or short_addr(mint)
                self.note_signal(symbol, mint, "CANDIDATE")
                if not result.passed:
                    self.count_reject(result.reasons)
                    await self.ledger.record(
                        "REJECT", agent=agent, mint=mint, symbol=symbol,
                        reason=clip_reason(result.reasons),
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
                        detail=" ".join("{}={:.0f}".format(k, v) for k, v in res.components.items()),
                        score=res.score)
                    self.blocked[mint] = time.time() + self.cfg.cooldown_after_reject_s
                    continue
                # A high score on a dead tape is noise, not a signal.
                bad_tape = tape_quality(micro, self.cfg)
                if bad_tape:
                    self.count_reject(["tape_quality"])
                    await self.ledger.record(
                        "REJECT", agent=agent, mint=mint, symbol=item["symbol"],
                        reason="tape: " + bad_tape, score=res.score,
                        detail=" ".join("{}={:.0f}".format(k, v) for k, v in res.components.items()))
                    self.blocked[mint] = time.time() + self.cfg.cooldown_after_reject_s
                    continue
                # Park it. Nothing is bought on a single observation: a second,
                # independent look runs confirm_delay_s later in confirmation_loop.
                price = snap.price_usd if snap else 0.0
                if len(self.pending) >= 300:
                    log.debug("confirmation queue full, dropping %s", short_addr(mint))
                    continue
                self.pending[mint] = {
                    "due": time.time() + self.cfg.confirm_delay_s,
                    "first_seen": time.time(), "price": price, "score": res.score,
                    "components": res.components, "item": item, "agent": agent,
                }
                await self.ledger.record(
                    "AWAIT_CONFIRM", agent=agent, mint=mint, symbol=item["symbol"],
                    price=price, reason="momentum {:.1f}, second look in {:.0f}s".format(
                        res.score, self.cfg.confirm_delay_s),
                    detail=" ".join("{}={:.0f}".format(k, v) for k, v in res.components.items()),
                    score=res.score)
                self.note("WATCH {} {:.0f}".format(item["symbol"], res.score))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("%s crashed on %s: %s", agent, short_addr(mint), exc)
                log.debug("%s", traceback.format_exc())

    # ---------------------------------------------------------------- research
    async def research(self, mint: str, symbol: str, name: str = "") -> ResearchResult:
        """
        Ask the open web about one token, with a hard time budget.

        Every failure is non-fatal: no binary, no network, login walls and
        timeouts all come back as a verdict the caller can reason about rather
        than an exception. Results are cached per token for research_cache_ttl_s
        so holding a position does not re-launch a browser every 5 minutes.
        """
        if not self.cfg.research_enabled:
            return ResearchResult(verdict="disabled", notes=["research disabled"])
        if not self.obscura.available:
            return ResearchResult(verdict="unavailable",
                                  notes=["obscura binary not found (see README)"])
        key = mint or symbol
        hit = self._research_cache.get(key)
        if hit and time.time() - hit[0] < self.cfg.research_cache_ttl_s:
            return hit[1]

        async def gather() -> ResearchResult:
            sources = research_sources(self.cfg, symbol, mint)
            pages = await self.obscura.fetch_many(sources)
            per: Dict[str, Dict[str, Any]] = {}
            for src in sources:
                got = pages.get(src["name"]) or FetchResult(error="no result")
                if not got.ok:
                    per[src["name"]] = {"ok": False, "blocked": got.blocked,
                                        "error": got.error}
                    continue
                counts = research_mentions(got.text, symbol, mint).as_dict()
                if src.get("own"):
                    # The token's own DEX page repeats the ticker by design. Its
                    # warnings are worth counting; its "mentions" are not chatter.
                    counts["mentions"] = 0
                    counts["hype_hits"] = 0
                    counts["samples"] = []
                per[src["name"]] = dict(counts, ok=True)
            return research_verdict(per, self.cfg)

        self.research_runs += 1
        try:
            result = await asyncio.wait_for(gather(), timeout=self.cfg.research_timeout_s)
        except asyncio.TimeoutError:
            result = ResearchResult(verdict="timeout",
                                    notes=["research exceeded {:.0f}s".format(
                                        self.cfg.research_timeout_s)])
        except asyncio.CancelledError:
            raise
        except Exception as exc:                          # noqa: BLE001 - never fatal
            log.debug("research failed for %s: %s", symbol, exc)
            result = ResearchResult(verdict="unavailable", notes=[str(exc)[:160]])

        self._research_cache[key] = (time.time(), result)
        if len(self._research_cache) > 500:
            for old_key in list(self._research_cache)[:100]:
                self._research_cache.pop(old_key, None)
        if result.verdict == "veto":
            self.research_vetoes += 1
        elif result.verdict == "boost":
            self.research_boosts += 1
        await self.ledger.record(
            "RESEARCH", agent="research", mint=mint, symbol=symbol,
            reason=result.line()[:250],
            detail="; ".join((result.notes or [])[:3])[:400])
        self.note("RESEARCH {} {}".format(symbol, result.verdict))
        return result

    async def research_gate(self, mint: str, symbol: str,
                            name: str = "") -> Tuple[Optional[ResearchResult], str]:
        """
        Returns (result, rejection_reason). Empty reason = allowed through.
        Only a veto (or missing research when research_required) blocks.
        """
        result = await self.research(mint, symbol, name)
        if result.verdict == "veto":
            return result, "research_veto: " + result.summary
        if self.cfg.research_required and result.verdict not in ("boost", "neutral"):
            return result, "research_unavailable: " + result.summary
        return result, ""

    async def news_watch_loop(self) -> None:
        """
        Bad news on a token you already hold is worse than a stop loss is late.
        Runs rarely (default every 5 min), only for open positions, and exits
        through the same _close_one() path as every other emergency.
        """
        while not self.stop_event.is_set():
            await self._sleep(self.cfg.research_position_interval_s)
            if self.stop_event.is_set():
                return
            if not self.obscura.available or self.draining:
                continue
            for pos in self.pf.open_positions():
                if pos.locked:
                    continue
                try:
                    result = await self.research(pos.mint, pos.symbol)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:                  # noqa: BLE001
                    log.debug("news watch failed for %s: %s", pos.symbol, exc)
                    continue
                if result.verdict != "veto":
                    continue
                log.warning("NEWS VETO on held position %s: %s", pos.symbol, result.summary)
                self.tg.notify("[{}] BAD NEWS EXIT {}\n{}\nclosing the position now.".format(
                    "LIVE" if self.cfg.live else "PAPER", pos.symbol, result.summary))
                pos.locked = True
                try:
                    await self._close_one(pos, "bad news (research): " + result.summary[:160])
                finally:
                    pos.locked = False

    # ------------------------------------------------------------ confirmation
    async def _confirm_candidate(self, mint: str, entry: Dict[str, Any]) -> None:
        """
        Independent second look. Re-fetches the market, re-scores momentum, re-runs
        every safety filter and only then forwards a signal to the executors.
        This is the closest thing to a "high conviction" signal that data supports:
        the setup must still be true seconds later, on fresh data, from a second
        provider call - not just once in a snapshot.
        """
        agent = entry.get("agent", "confirm")
        item = entry["item"]
        symbol = item.get("symbol", "") or short_addr(mint)
        now = time.time()

        snap = await self.sources.dexscreener_snapshot(mint)
        pool = None
        try:
            pool = await self.sources.geckoterminal_pool_for_token(mint)
        except HttpError:
            pool = None
        if snap is not None and pool:
            snap = self.sources.apply_gt_pool(snap, pool)
        elif snap is None and pool:
            snap = self.sources.snapshot_from_gt(mint, pool)
        if snap is None or snap.price_usd <= 0:
            await self._confirm_reject(mint, symbol, "confirm_no_price", agent)
            return

        micro = await self.sources.geckoterminal_micro(snap.pool_address) if snap.pool_address else None
        res = momentum_score(snap, micro, self.cfg)
        verdict = confirmation_verdict(entry["price"], snap.price_usd, res.score, self.cfg)
        if verdict is None:
            bad = tape_quality(micro, self.cfg)
            if bad:
                verdict = "confirm_tape: " + bad
        if verdict is None and res.veto:
            verdict = "confirm_veto: " + res.veto
        fomo = None
        if verdict is None:
            facts = await self.sources.rugcheck_facts(mint)
            sell_quote = await self.simulate_sell_quote(mint, snap.price_usd, facts)
            fomo = await self.sources.fomo_enrich(mint,
                                                  include_slow=self.cfg.fomo_include_slow)
            providers = ((1 if snap is not None else 0) + (1 if pool else 0) +
                         (1 if facts.available else 0) +
                         (1 if fomo is not None and fomo.available else 0))
            result = evaluate_safety(snap, facts, sell_quote, self.cfg,
                                     providers=providers, fomo=fomo)
            if not result.passed:
                await self._confirm_reject(mint, symbol, "confirm_safety: " + "; ".join(result.reasons),
                                           agent)
                return
        else:
            facts = item.get("facts")
        if verdict is None and fomo is not None and fomo.available:
            blocks = fomo.blocking_reasons()
            if blocks:
                await self._confirm_reject(mint, symbol, "fomo_veto: " + "; ".join(blocks), agent)
                return
            if self.cfg.fomo_veto_dev_exit and fomo.dev_exit_reason():
                await self._confirm_reject(mint, symbol, "fomo_veto: " + fomo.dev_exit_reason(),
                                           agent)
                return
        if verdict:
            await self._confirm_reject(mint, symbol, verdict, agent)
            return

        if self.draining or self.stop_event.is_set():
            return

        # ---- the web research skill: last gate before a signal exists --------
        research, why = await self.research_gate(mint, symbol, getattr(snap, "name", ""))
        if why:
            await self._confirm_reject(mint, symbol, why, agent)
            return
        score = res.score
        fomo_delta, fomo_notes = fomo_flow_delta(fomo, self.cfg)
        if fomo_delta:
            score = clip(score + fomo_delta, 0.0, 100.0)
        if research is not None and research.delta:
            score = clip(score + research.delta, 0.0, 100.0)
        if (research is not None and research.delta) or fomo_delta:
            if score < self.cfg.momentum_threshold:
                await self._confirm_reject(
                    mint, symbol, "confirm_momentum_lost after research ({:.1f} < {:.1f})".format(
                        score, self.cfg.momentum_threshold), agent)
                return
        if self.stop_event.is_set():
            return

        await self.ledger.record(
            "SIGNAL", agent=agent, mint=mint, symbol=symbol, price=snap.price_usd,
            reason="confirmed after {:.0f}s".format(now - entry["first_seen"]),
            detail="momentum {:.1f} (first {:.1f}) | held {:.0f}s | research {} | fomo {} | {}".format(
                score, entry["score"], now - entry["first_seen"],
                research.line()[:120] if research else "off",
                (fomo.summary() + (" " + "; ".join(fomo_notes) if fomo_notes else ""))
                if fomo is not None and fomo.available else "n/a",
                " ".join("{}={:.0f}".format(k, v) for k, v in res.components.items())),
            score=score)
        self.note_signal(symbol, mint, "CONFIRMED", score)
        self.note("SIGNAL {} {:.0f} (confirmed{})".format(
            symbol, score, ", +{:+.0f} news".format(research.delta) if research and research.delta else ""))
        await self.signals.put({"mint": mint, "symbol": symbol, "snap": snap, "facts": facts,
                                "micro": micro, "score": score,
                                "research": research.line()[:160] if research else "",
                                "source": item.get("source", ""), "agent": agent,
                                "ts": time.time()})

    async def _confirm_reject(self, mint: str, symbol: str, reason: str, agent: str) -> None:
        self.count_reject([reason])
        await self.ledger.record("REJECT", agent=agent, mint=mint, symbol=symbol,
                                 reason="second look: " + reason[:280])
        self.note("DROP {} {}".format(symbol, reason.split(":")[0]))
        self.blocked[mint] = time.time() + self.cfg.cooldown_after_reject_s

    async def confirmation_loop(self) -> None:
        """
        Second looks run concurrently but bounded: each one can spend up to
        research_timeout_s inside a browser, and a single slow lookup must not
        stall every other candidate.
        """
        running: set = set()
        sem = asyncio.Semaphore(2)

        async def one(mint: str, entry: Dict[str, Any]) -> None:
            async with sem:
                if self.draining or self.stop_event.is_set() or mint in self.pf.open_mints():
                    return
                try:
                    await self._confirm_candidate(mint, entry)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.error("confirmation failed for %s: %s", short_addr(mint), exc)
                    log.debug("%s", traceback.format_exc())

        while not self.stop_event.is_set():
            now = time.time()
            for mint, entry in list(self.pending.items()):
                if entry["due"] > now:
                    continue
                self.pending.pop(mint, None)
                if len(running) >= 4:                     # bounded backlog
                    continue
                task = asyncio.create_task(one(mint, entry))
                running.add(task)
                task.add_done_callback(running.discard)
            for mint, entry in list(self.pending.items()):   # expire stale first looks
                if now - entry["first_seen"] > 600:
                    self.pending.pop(mint, None)
            await self._sleep(2.0)
        for task in list(running):
            task.cancel()

    # ------------------------------------------------------------------ entries
    async def try_open(self, sig: Dict[str, Any], agent: str) -> None:
        mint = sig["mint"]
        symbol = sig["symbol"]
        async with self.entry_lock:                   # one wallet = one entry at a time
            if self.draining or self.stop_event.is_set():
                return
            ok, why = await self.risk.can_open(mint, symbol)
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
            pos.decimals = decimals
            pos.micro = sig.get("micro")
            pos.last_micro_ts = time.time()
            pos.last_facts_ts = time.time()
            self.entries += 1
            self.risk.cooldowns[mint] = time.time() + self.cfg.cooldown_after_exit_s
            await self.ledger.record(
                "BUY", agent=agent, mint=mint, symbol=symbol, side="buy",
                qty=fill.qty_tokens, price=fill.price, usd=fill.usd_value, fee_usd=fill.fee_usd,
                pos_id=pos.pos_id, score=fnum(sig.get("score")),
                reason="momentum {:.0f}".format(fnum(sig.get("score"))),
                detail="slippage {:.0f}bps; {}{}".format(
                    fill.slippage_bps, fill.detail,
                    ("; research " + str(sig.get("research")))[:200] if sig.get("research") else ""))
            self.tg.notify("[{}] BUY {} ${:.2f} @ {:.10g}\nmomentum {:.0f} | liq {}\n{}"
                           .format("LIVE" if self.cfg.live else "PAPER", symbol,
                                   fill.usd_value, fill.price, fnum(sig.get("score")),
                                   fmt_usd(snap.liquidity_usd), short_addr(mint)))
            self.note("BUY {} ${:.2f}".format(symbol, fill.usd_value))
            await self.save_state()

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
                pos.decimals = facts.decimals
                pos.last_facts_ts = now

    async def manage_position(self, pos: Position, agent: str) -> None:
        try:
            await self.refresh_position(pos)
            snap = pos.snap
            decision = decide_exit(pos, snap, pos.micro, pos.facts, time.time(), self.cfg)
            if decision is None:
                return
            price = (snap.price_usd if snap and snap.price_usd > 0 else pos.last_price)
            decimals = pos.facts.decimals if pos.facts else pos.decimals
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
                pnl_usd=pnl, pos_id=pos.pos_id,
                reason="{}: {}".format(decision.kind, decision.reason),
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
                                         pnl_usd=total, pos_id=pos.pos_id,
                                         reason=pos.exit_reason)
                self.note("CLOSE {} pnl ${:+.2f}".format(pos.symbol, total))
                await self.save_state()
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

    # ------------------------------------------------------------- persistence
    def _write_json(self, path: str, data: Dict[str, Any]) -> bool:
        """Atomic JSON write: a phone can be killed mid-write at any time."""
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            return True
        except OSError as exc:
            log.error("cannot write %s: %s", path, exc)
            return False

    def _position_state(self, p: Position) -> Dict[str, Any]:
        return {
            "pos_id": p.pos_id, "mint": p.mint, "symbol": p.symbol,
            "opened_ts": p.opened_ts, "entry_price": p.entry_price,
            "entry_cost_usd": p.entry_cost_usd, "initial_tokens": p.initial_tokens,
            "tokens": p.tokens, "peak_price": p.peak_price, "peak_ts": p.peak_ts,
            "entry_fee_usd": p.entry_fee_usd, "realized_usd": p.realized_usd,
            "fees_usd": p.fees_usd, "tp1_done": p.tp1_done, "tp2_done": p.tp2_done,
            "trail_active": p.trail_active, "entry_liquidity_usd": p.entry_liquidity_usd,
            "creator_baseline_raw": p.creator_baseline_raw, "last_price": p.last_price,
            "last_price_ts": p.last_price_ts, "decimals": p.decimals,
            "source": p.source,
        }

    async def save_state(self) -> None:
        """Snapshot cash, positions and risk counters so a restart loses nothing."""
        if not self.cfg.state_enabled:
            return
        now = time.time()
        data = {
            "version": 1,
            "mode": "live" if self.cfg.live else "paper",
            "saved_at_utc": utc_str(),
            "portfolio": {
                "cash_usd": self.pf.cash_usd, "realized_usd": self.pf.realized_usd,
                "fees_usd": self.pf.fees_usd, "wins": self.pf.wins,
                "losses": self.pf.losses, "trades": self.pf.trades,
                "start_capital_usd": self.pf.start_capital_usd,
            },
            "risk": {
                "day": self.risk.day, "daily_realized_usd": self.risk.daily_realized_usd,
                "halted": self.risk.halted, "halt_reason": self.risk.halt_reason,
                "halt_until_day": self.risk.halt_until_day,
                "cooldowns": {k: v for k, v in self.risk.cooldowns.items() if v > now},
            },
            "positions": [self._position_state(p) for p in self.pf.open_positions()],
            "reject_counts": dict(sorted(self.reject_counts.items(), key=lambda kv: -kv[1])[:60]),
        }
        self._write_json(self.cfg.state_file, data)

    async def load_state(self) -> bool:
        if not self.cfg.state_enabled or not os.path.exists(self.cfg.state_file):
            return False
        try:
            with open(self.cfg.state_file, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            log.warning("state file unreadable (%s); starting fresh", exc)
            return False
        if not isinstance(data, dict):
            return False
        mode = "live" if self.cfg.live else "paper"
        if data.get("mode") != mode:
            log.warning("state file is from %s mode, current mode is %s - ignoring it",
                        data.get("mode"), mode)
            return False
        pf = data.get("portfolio") or {}
        self.pf.cash_usd = fnum(pf.get("cash_usd"), self.pf.cash_usd)
        self.pf.realized_usd = fnum(pf.get("realized_usd"))
        self.pf.fees_usd = fnum(pf.get("fees_usd"))
        self.pf.wins = int(fnum(pf.get("wins")))
        self.pf.losses = int(fnum(pf.get("losses")))
        self.pf.trades = int(fnum(pf.get("trades")))
        self.pf.start_capital_usd = fnum(pf.get("start_capital_usd"), self.pf.start_capital_usd)
        risk = data.get("risk") or {}
        self.risk.day = str(risk.get("day") or self.risk.day)
        self.risk.daily_realized_usd = fnum(risk.get("daily_realized_usd"))
        self.risk.halted = bool(risk.get("halted"))
        self.risk.halt_reason = str(risk.get("halt_reason") or "")
        self.risk.halt_until_day = str(risk.get("halt_until_day") or "")
        for k, v in (risk.get("cooldowns") or {}).items():
            if fnum(v) > time.time():
                self.risk.cooldowns[k] = fnum(v)
        self.reject_counts = {str(k): int(fnum(v)) for k, v in
                              (data.get("reject_counts") or {}).items()}
        restored = 0
        for d in data.get("positions") or []:
            try:
                if fnum(d.get("tokens")) <= 0 or fnum(d.get("entry_price")) <= 0:
                    continue
                pos = Position(
                    pos_id=str(d.get("pos_id") or "restored-{}".format(restored)),
                    mint=str(d["mint"]), symbol=str(d.get("symbol") or ""),
                    opened_ts=fnum(d.get("opened_ts"), time.time()),
                    entry_price=fnum(d.get("entry_price")),
                    entry_cost_usd=fnum(d.get("entry_cost_usd")),
                    initial_tokens=fnum(d.get("initial_tokens")),
                    tokens=fnum(d.get("tokens")),
                    peak_price=fnum(d.get("peak_price"), fnum(d.get("entry_price"))),
                    peak_ts=fnum(d.get("peak_ts"), time.time()),
                    entry_fee_usd=fnum(d.get("entry_fee_usd")),
                    realized_usd=fnum(d.get("realized_usd")),
                    fees_usd=fnum(d.get("fees_usd")),
                    tp1_done=bool(d.get("tp1_done")), tp2_done=bool(d.get("tp2_done")),
                    trail_active=bool(d.get("trail_active")),
                    entry_liquidity_usd=fnum(d.get("entry_liquidity_usd")),
                    creator_baseline_raw=fnum(d.get("creator_baseline_raw"), -1.0),
                    last_price=fnum(d.get("last_price")),
                    last_price_ts=fnum(d.get("last_price_ts")),
                    decimals=int(fnum(d.get("decimals"), self.cfg.fallback_token_decimals)),
                    source=str(d.get("source") or ""), agent="restored",
                )
                self.pf.positions[pos.pos_id] = pos
                restored += 1
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("skipping unreadable position in state: %s", exc)
        log.info("state restored: cash %s, %d open position(s), daily pnl %s",
                 fmt_usd(self.pf.cash_usd), restored, fmt_usd(self.risk.daily_realized_usd))
        return True

    async def reconcile_live(self) -> None:
        """After a restart, trust the chain, not the JSON file."""
        if not isinstance(self.executor, JupiterExecutor):
            return
        for pos in list(self.pf.open_positions()):
            try:
                raw = await self.executor.token_balance_raw(pos.mint)
            except Exception as exc:                      # noqa: BLE001 - never block startup
                log.warning("could not reconcile %s: %s", pos.symbol, exc)
                continue
            if raw <= 0:
                log.warning("restored position %s has no tokens on chain; closing it",
                            pos.symbol)
                await self.pf.close_position(pos, "restored_flat")
                continue
            pos.tokens = raw / float(10 ** pos.decimals)
            log.info("reconciled %s: %.6f tokens on chain", pos.symbol, pos.tokens)
        await self.save_state()

    # ------------------------------------------------------------------ dashboard
    def dashboard(self) -> str:
        now = time.time()
        runtime = int(now - self.started)
        mode = "LIVE" if self.cfg.live else "PAPER"
        eq = self.pf.equity()
        try:
            width = shutil.get_terminal_size((96, 24)).columns
        except (OSError, ValueError):
            width = 96
        compact = self.cfg.dashboard_compact and width < 100
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
        if self.cfg.research_enabled:
            lines.append(" research: {} | runs {} vetoes {} boosts {} | awaiting {} first look(s)".format(
                "ON" if self.obscura.available else "UNAVAILABLE",
                self.research_runs, self.research_vetoes, self.research_boosts,
                len(self.pending)))
        fomo_health = self.sources.fomo_health()
        lines.append(" fomo: {} | last refresh {}s ago".format(
            fomo_health.get("status", "off"),
            "{:.0f}".format(now - self.last_refresh) if self.last_refresh else "n/a"))
        if self.recent_signals:
            lines.append(" latest signals:")
            for row in self.recent_signals[-3:]:
                lines.append("   {:<9} {:<10} score {:>3.0f}  {}".format(
                    (row["symbol"] or "?")[:9], row["status"], row["score"],
                    short_addr(row["mint"], 6, 6)))
        if compact:
            # Phone view: keep the money line and the positions, drop the rest.
            keep = [ln for ln in lines if ln.startswith(" MEME AGENTS")
                    or ln.startswith(" cash ") or ln.startswith(" trades ")
                    or ln.startswith(" OPEN POSITIONS") or ln.startswith("  ")
                    or ln.startswith(" fomo:") or ln.startswith(" latest signals:")
                    or ln.startswith("   ")]
            lines = keep + [" (compact view: --full for everything)"]
        if self.recent_events:
            lines.append(" last events:")
            lines += ["   " + e for e in self.recent_events[-5:]]
        lines.append("=" * 96)
        return "\n".join(lines)

    def _write_heartbeat(self) -> None:
        """External watchdogs (and you, the next morning) can tell it is alive."""
        st = self.http.stats
        self._write_json(self.cfg.heartbeat_file, {
            "ts_utc": utc_str(), "epoch": time.time(),
            "mode": "live" if self.cfg.live else "paper",
            "equity_usd": round(self.pf.equity(), 6),
            "cash_usd": round(self.pf.cash_usd, 6),
            "realized_usd": round(self.pf.realized_usd, 6),
            "open_positions": len(self.pf.open_positions()),
            "trades": self.pf.trades, "wins": self.pf.wins, "losses": self.pf.losses,
            "daily_loss_used_usd": round(self.risk.daily_loss_used(), 6),
            "halted": self.risk.halted,
            "awaiting_confirmation": len(self.pending),
            "research": {"available": self.obscura.available,
                         "binary": self.obscura.bin,
                         "runs": self.research_runs,
                         "vetoes": self.research_vetoes,
                         "boosts": self.research_boosts,
                         **{k: self.obscura.stats[k] for k in ("ok", "errors", "blocked",
                                                               "cache_hits", "timeouts")}},
            "http": {"requests": st.requests, "errors_net": st.errors_net,
                     "errors_4xx": st.errors_4xx, "errors_5xx": st.errors_5xx,
                     "retries": st.retries},
            "feeds": {n: {"ok": v["ok"], "err": v["err"], "last": round(v["last"], 1)}
                      for n, v in self.feed_state.items()},
        })

    async def heartbeat_loop(self) -> None:
        """Independent of the dashboard: a watchdog needs a steady pulse."""
        while not self.stop_event.is_set():
            try:
                self._write_heartbeat()
            except Exception as exc:
                log.debug("heartbeat error: %s", exc)
            await self._sleep(self.cfg.heartbeat_every_s)
        try:
            self._write_heartbeat()
        except Exception:
            pass

    async def check_stale_prices(self, now: Optional[float] = None) -> None:
        """
        A position we cannot price is a position we cannot exit. Say so, loudly,
        once every ten minutes - silently holding a blind position is how accounts die.
        """
        now = now or time.time()
        for pos in self.pf.open_positions():
            if now - pos.last_price_ts < self.cfg.stale_price_alert_s:
                continue
            if now - self._alerted.get(pos.mint, 0.0) < 600:
                continue
            self._alerted[pos.mint] = now
            msg = "no price for %s in %.0fs - exits may be blind" % (
                pos.symbol, now - pos.last_price_ts)
            log.warning("STALE: %s", msg)
            self.tg.notify("[{}] STALE PRICE: {}".format(
                "LIVE" if self.cfg.live else "PAPER", msg))

    async def dashboard_loop(self) -> None:
        while not self.stop_event.is_set():
            await self._sleep(self.cfg.dashboard_every_s)
            if self.stop_event.is_set():
                return
            try:
                print(self.dashboard())
                await self.check_stale_prices()
                await self.save_state()
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
        await self.save_state()
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
        await self.save_state()

    async def _close_one(self, pos: Position, reason: str) -> None:
        snap = None
        try:
            snap = await asyncio.wait_for(self.sources.dexscreener_snapshot(pos.mint), timeout=20)
        except (asyncio.TimeoutError, HttpError):
            snap = pos.snap
        price = (snap.price_usd if snap and snap.price_usd > 0 else pos.last_price)
        decimals = pos.facts.decimals if pos.facts else pos.decimals
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
                                 pnl_usd=pos.realized_usd, pos_id=pos.pos_id, reason=reason)
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
        if await self.load_state():
            if self.cfg.live:
                await self.reconcile_live()
            log.info("resumed from %s (%d open position(s))", self.cfg.state_file,
                     len(self.pf.open_positions()))
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
        self.tasks.append(asyncio.create_task(self.confirmation_loop(), name="confirm"))
        self.tasks.append(asyncio.create_task(self.heartbeat_loop(), name="heartbeat"))
        if self.obscura.available:
            self.tasks.append(asyncio.create_task(self.news_watch_loop(), name="news-watch"))
        self.tasks.append(asyncio.create_task(self.dashboard_loop(), name="dashboard"))
        self.tasks.append(asyncio.create_task(self.tg.sender_loop(self.stop_event), name="telegram"))

        log.info("%d agents running + confirmation/dashboard/telegram tasks "
                 "(Ctrl+C to stop)", sum(self.counts.values()))
        await self.stop_event.wait()
        self.draining = True
        await self._stop_tasks()
        await self.save_state()

        text = self.report("stopped")
        print(text)
        if self.cfg.free_run_hours or self.cfg.max_runtime_hours:
            try:
                quality, _ = analyze_ledger(self.cfg.ledger_csv, self.cfg.start_capital_usd)
                print(quality)
            except Exception as exc:                  # never fail shutdown over a report
                log.debug("analyzer failed: %s", exc)
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
# SECTION 14b.  SCANNER  (--scan one-shot, --watch continuous)
#
# Same data sources, same safety filters, same momentum maths as the trading
# swarm - but this pipeline only ever *reports*. It cannot open a position, and
# nothing in this section touches a wallet, a key or an executor.
#
# Flow: discover (5 feeds, de-duplicated by mint) -> enrich (pair, tape,
# RugCheck, optional FOMO) -> safety -> momentum -> second look -> Signal.
# =============================================================================

SCAN_FEEDS: List[str] = ["dexscreener_profiles", "geckoterminal_new_pools",
                         "dexscreener_boosts", "geckoterminal_trending",
                         "coingecko_recent"]


class Scanner:
    def __init__(self, cfg: Config, http: HttpClient, sources: Sources,
                 telegram: Optional[Telegram] = None,
                 book: Optional[SignalBook] = None) -> None:
        self.cfg = cfg
        self.http = http
        self.sources = sources
        self.tg = telegram
        self.book = book or SignalBook(cfg)
        self.feed_health: Dict[str, Dict[str, Any]] = {
            name: {"ok": 0, "err": 0, "last": 0.0, "found": 0, "error": ""}
            for name in SCAN_FEEDS}
        self.last_refresh = 0.0
        self.last_error = ""
        self.scans = 0
        self.fomo_budget = max(0, int(cfg.fomo_max_tokens_per_scan))

    # ---------------------------------------------------------------- discovery
    async def discover(self) -> List[Dict[str, str]]:
        """Poll every feed once, in parallel, and de-duplicate by Solana mint."""
        results = await asyncio.gather(
            *[getattr(self.sources, name)() for name in SCAN_FEEDS],
            return_exceptions=True)
        rows: List[Dict[str, str]] = []
        seen: Dict[str, Dict[str, str]] = {}
        for name, result in zip(SCAN_FEEDS, results):
            state = self.feed_health[name]
            state["last"] = time.time()
            if isinstance(result, Exception):
                state["err"] += 1
                state["error"] = str(result)[:80]
                log.debug("scan feed %s failed: %s", name, result)
                continue
            state["ok"] += 1
            state["found"] = len(result or [])
            for row in result or []:
                mint = (fomo_normalize_mint or (lambda x: str(x or "").strip()))(
                    row.get("mint", ""))
                if not mint:
                    continue
                if mint in seen:
                    # keep the first source, note the corroborating one
                    extra = row.get("source", "")
                    if extra and extra not in seen[mint]["source"]:
                        seen[mint]["source"] += "+" + extra.split(":")[0]
                    continue
                item = {"mint": mint, "source": row.get("source", name)}
                seen[mint] = item
                rows.append(item)
        self.last_refresh = time.time()
        return rows

    # ---------------------------------------------------------------- evaluation
    async def evaluate(self, mint: str, source: str = "",
                       first_seen: float = 0.0) -> Signal:
        """One candidate, fully evaluated, without ever placing an order."""
        now = time.time()
        snap = await self.sources.dexscreener_snapshot(mint)
        pool = None
        try:
            pool = await self.sources.geckoterminal_pool_for_token(mint)
        except HttpError:
            pool = None
        if snap is not None and pool:
            snap = self.sources.apply_gt_pool(snap, pool)
        elif snap is None and pool:
            snap = self.sources.snapshot_from_gt(mint, pool)
        micro = None
        if snap is not None and snap.pool_address:
            micro = await self.sources.geckoterminal_micro(snap.pool_address)
        facts = await self.sources.rugcheck_facts(mint)
        price = snap.price_usd if snap else 0.0
        sell_quote = await self.sources.simulate_sell_quote(mint, price, facts)
        prov = ((1 if snap is not None else 0) + (1 if pool else 0) +
                (1 if facts is not None and facts.available else 0))

        fomo = None
        fomo_delta, fomo_notes = 0.0, []
        safety = evaluate_safety(snap, facts, sell_quote, self.cfg, providers=prov)
        # Only spend FOMO credits on tokens that already survive every local
        # filter: this is enrichment of a real candidate, not a census.
        provider = self.sources.fomo_provider()
        # Free when this mint is already in the provider's 15-minute cache, so
        # the second look reuses the first look's answer instead of re-billing.
        if (safety.passed and provider is not None
                and (self.fomo_budget > 0 or provider.cached(mint))):
            free = provider.cached(mint)
            fomo = await self.sources.fomo_enrich(mint,
                                                  include_slow=self.cfg.fomo_include_slow)
            if fomo is not None and fomo.available:
                if not free:
                    self.fomo_budget -= 1
                prov += 1
                safety = evaluate_safety(snap, facts, sell_quote, self.cfg,
                                         providers=prov, fomo=fomo)
                fomo_delta, fomo_notes = fomo_flow_delta(fomo, self.cfg)
            else:
                fomo = None

        momentum = momentum_score(snap, micro, self.cfg) if snap is not None else None
        signal = build_signal(self.cfg, mint, snap, facts, micro, momentum, safety,
                              fomo=fomo, source=source, now=now,
                              first_seen=first_seen, fomo_delta=fomo_delta,
                              fomo_notes=fomo_notes)
        if not signal.symbol:
            signal.symbol = (facts.symbol if facts is not None else "") or short_addr(mint)
        if micro is not None:
            bad = tape_quality(micro, self.cfg)
            if bad and signal.status in (SIGNAL_WATCH, SIGNAL_CANDIDATE):
                signal.reject_reasons = signal.reject_reasons + ["tape: " + bad]
                signal.status = SIGNAL_REJECTED
        return signal

    async def second_look(self, signal: Signal, delay: Optional[float] = None) -> Signal:
        """
        The confirmation rule, applied outside the trading loop.

        Re-fetch price and safety after a delay and require that the setup is
        still there. If anything regressed, the candidate is REJECTED with the
        reason; only a clean re-check produces CONFIRMED.
        """
        delay = self.cfg.scan_confirm_delay_s if delay is None else delay
        if delay > 0:
            await asyncio.sleep(delay)
        again = await self.evaluate(signal.mint, source=", ".join(signal.sources),
                                    first_seen=signal.first_seen)
        now = time.time()
        verdict = confirmation_verdict(signal.price, again.price, again.score, self.cfg)
        if again.status == SIGNAL_REJECTED:
            again.reject_reasons = again.reject_reasons or ["safety changed on the second look"]
            again.status = SIGNAL_REJECTED
            return self.book.upsert(again, now)
        if verdict:
            again.reject_reasons = again.reject_reasons + [verdict]
            again.status = SIGNAL_REJECTED
            return self.book.upsert(again, now)
        again.status = SIGNAL_CONFIRMED
        again.confirmed_at = now
        return self.book.upsert(again, now)

    async def scan_once(self, limit: Optional[int] = None) -> List[Signal]:
        """One full pass: discover, evaluate, confirm, return ranked signals."""
        self.scans += 1
        self.fomo_budget = max(0, int(self.cfg.fomo_max_tokens_per_scan))
        started = time.time()
        rows = await self.discover()
        limit = max(1, int(limit or self.cfg.scan_max_candidates))
        rows = rows[:limit]
        sem = asyncio.Semaphore(max(1, int(self.cfg.scan_concurrency)))

        async def one(row: Dict[str, str]) -> Optional[Signal]:
            async with sem:
                try:
                    return await self.evaluate(row["mint"], row.get("source", ""))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:                 # noqa: BLE001 - one bad token
                    log.debug("scan evaluate failed for %s: %s", row["mint"], exc)
                    self.last_error = str(exc)[:160]
                    return None

        produced = [s for s in await asyncio.gather(*[one(r) for r in rows]) if s]
        candidates = [s for s in produced if s.status == SIGNAL_CANDIDATE]
        settled: set = set()
        if candidates:
            # one shared wait, then refresh every candidate concurrently
            if self.cfg.scan_confirm_delay_s > 0:
                await asyncio.sleep(self.cfg.scan_confirm_delay_s)

            async def confirm(sig: Signal) -> Signal:
                async with sem:
                    try:
                        return await self.second_look(sig, delay=0.0)
                    except Exception as exc:             # noqa: BLE001
                        log.debug("second look failed for %s: %s", sig.mint, exc)
                        sig.reject_reasons = sig.reject_reasons + ["second look error"]
                        return self.book.upsert(sig)

            settled_rows = await asyncio.gather(*[confirm(s) for s in candidates])
            settled = {s.mint for s in settled_rows if s is not None}

        for sig in produced:
            if sig.mint not in settled:
                self.book.upsert(sig)
        self.book.prune()
        self.last_refresh = time.time()
        log.debug("scan finished in %.1fs (%d candidates)", self.last_refresh - started,
                  len(produced))
        return self.book.ranked()

    # ------------------------------------------------------------------ display
    def _provider_line(self) -> str:
        ok = sum(1 for st in self.feed_health.values() if st["ok"])
        err = sum(1 for st in self.feed_health.values() if st["err"])
        fomo = self.sources.fomo_health()
        fomo_txt = ("fomo off" if not fomo.get("available")
                    else "fomo ok {}cr".format(fomo.get("credits_spent", 0)))
        return "feeds {}/{} ok{}  |  {}  |  rugcheck {}".format(
            ok, len(self.feed_health),
            " ({} err)".format(err) if err else "", fomo_txt,
            "on" if self.cfg.min_corroboration <= 3 else "?")

    def dashboard(self, width: int = 58) -> str:
        """Compact view for a phone: statuses, top rows, health, freshness."""
        now = time.time()
        counts = {st: len(self.book.by_status(st)) for st in SIGNAL_STATUSES}
        lines = [
            "=" * width,
            _fit("SCAN {}  #{}  every {:.0f}s".format(
                utc_str(now), self.scans, self.cfg.watch_interval_s), width),
            "-" * width,
            _fit(self._provider_line(), width),
            _fit("signals: {} confirmed  {} watch  {} rejected  {} expired".format(
                counts[SIGNAL_CONFIRMED], counts[SIGNAL_WATCH],
                counts[SIGNAL_REJECTED], counts[SIGNAL_EXPIRED]), width),
            _fit("last refresh: {:.0f}s ago".format(
                (now - self.last_refresh) if self.last_refresh else -1), width),
        ]
        for status in (SIGNAL_CONFIRMED, SIGNAL_WATCH, SIGNAL_CANDIDATE):
            rows = [s for s in self.book.by_status(status)][:3]
            if not rows:
                continue
            lines.append("-" * width)
            lines.append(_fit("{} ({})".format(status, len(self.book.by_status(status))),
                              width))
            for sig in rows:
                lines.append(_fit("  ${:<9} score {:>3.0f}  ${:<10.6g} {:>7}  "
                                  "liq {}  vol5m {}".format(
                                      (sig.symbol or "?")[:9], sig.score, sig.price,
                                      fmt_pct(sig.change_5m), fmt_usd(sig.liquidity_usd),
                                      fmt_usd(sig.volume_5m)), width))
                lines.append(_fit("    {}  age {:.0f}s".format(
                    short_addr(sig.mint, 6, 6), sig.data_age_s_now(now)), width))
        if self.tg is not None:
            lines.append(_fit("telegram: {}".format(
                "on ({} sent)".format(self.tg.sent) if self.tg.enabled else "off"), width))
        lines.append(_fit("paper: scan mode never trades - no positions, capital idle",
                          width))
        lines.append("=" * width)
        return "\n".join(lines)

    # -------------------------------------------------------------- watch loop
    async def run_watch(self, iterations: int = 0, stop_event: Optional[asyncio.Event] = None,
                        limit: Optional[int] = None, quiet: bool = False,
                        as_json: bool = False) -> int:
        """
        Continuous monitoring. Ctrl+C / STOP file ends it cleanly after the
        current pass, and the book is written to signals.json either way.
        """
        stop_event = stop_event or asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass
        sender = None
        if self.tg is not None and self.tg.enabled:
            sender = asyncio.create_task(self.tg.sender_loop(stop_event), name="tg")
        print("WATCH MODE: research only, no orders. Ctrl+C or STOP file to stop.")
        print("Telegram: " + ("on" if self.tg is not None and self.tg.enabled else "off")
              + "   interval: {:.0f}s".format(self.cfg.watch_interval_s))
        done = 0
        try:
            while not stop_event.is_set():
                if os.path.exists(self.cfg.stop_file):
                    print("STOP file found - finishing cleanly.")
                    break
                started = time.time()
                try:
                    await self.scan_once(limit=limit)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:                 # noqa: BLE001 - keep watching
                    self.last_error = str(exc)[:160]
                    log.error("scan failed: %s", exc)
                self._alert_new()
                self.book.save(self.cfg.signal_file)
                if as_json:
                    print(json.dumps({
                        "scanned_at_utc": utc_str(),
                        "mode": "PAPER (research only)",
                        "fomo": self.sources.fomo_health().get("status", "off"),
                        "signals": [s.as_dict() for s in self.book.ranked()],
                    }, sort_keys=True))
                elif not quiet:
                    print(self.dashboard())
                done += 1
                if iterations and done >= iterations:
                    break
                elapsed = time.time() - started
                await self._sleep(max(1.0, self.cfg.watch_interval_s - elapsed),
                                  stop_event)
        finally:
            stop_event.set()
            if sender is not None:
                try:
                    await asyncio.wait_for(sender, timeout=10)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    sender.cancel()
            self.book.save(self.cfg.signal_file)
            print("watch stopped: {} scan(s), {} signal(s) tracked, saved to {}".format(
                self.scans, len(self.book.signals), self.cfg.signal_file))
        return 0

    def _alert_new(self) -> int:
        """Queue Telegram alerts for new confirmations/invalidations only."""
        if self.tg is None or not self.tg.enabled:
            return 0
        sent = 0
        for sig in self.book.ranked():
            if self.book.should_alert(sig):
                self.tg.notify(sig.telegram())
                sent += 1
        return sent

    @staticmethod
    async def _sleep(seconds: float, stop_event: Optional[asyncio.Event] = None) -> None:
        if stop_event is None:
            await asyncio.sleep(seconds)
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


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
    p.add_argument("--scan", nargs="?", const=0, type=int, default=None, metavar="N",
                   help="one-shot research scan: print N signal cards (default %d) and exit; "
                        "never places an order" % 8)
    p.add_argument("--watch", action="store_true",
                   help="continuous research monitor (Ctrl+C / STOP file to stop); never trades")
    p.add_argument("--refresh", type=float, default=None, metavar="S",
                   help="--watch refresh interval in seconds (default 120)")
    p.add_argument("--iterations", type=int, default=0, metavar="N",
                   help="--watch: stop after N scans (0 = run until stopped)")
    p.add_argument("--json", action="store_true", help="--scan/--watch: machine-readable output")
    p.add_argument("--no-fomo", dest="fomo", action="store_false", default=None,
                   help="disable the optional FOMO provider even if FOMO_API_KEY is set")
    p.add_argument("--fomo-include-slow", action="store_true",
                   help="also fetch FOMO devs + holders (500 extra credits per token)")
    p.add_argument("--fomo-max-tokens", type=int, default=None, metavar="N",
                   help="FOMO lookups per scan (default 3; 500 credits each)")
    p.add_argument("--full", action="store_true",
                   help="full-width dashboard instead of the compact phone view")
    p.add_argument("--research", dest="research", action="store_true", default=None,
                   help="force the Obscura web-research gate on (default: on if installed)")
    p.add_argument("--no-research", dest="research", action="store_false",
                   help="disable web research entirely (pure price/tape rules)")
    p.add_argument("--obscura", default="", metavar="PATH",
                   help="path to the obscura binary (default: $OBSCURA_BIN or PATH)")
    p.add_argument("--research-required", action="store_true",
                   help="no research answer = no trade (strict; trades rarely)")
    p.add_argument("--research-channels", default="", metavar="A,B",
                   help="comma-separated public Telegram channels (t.me/s/...) to scan")
    p.add_argument("--research-storage-dir", default="", metavar="DIR",
                   help="Obscura cookie jar; needed for x.com search")
    p.add_argument("--research-mint", default="", metavar="MINT",
                   help="mint address to include in --research-check (adds the DEX page)")
    p.add_argument("--research-check", default=None, metavar="SYMBOL",
                   help="run the research skill once for SYMBOL, print what it found, exit")
    p.add_argument("--analyze", nargs="?", const="__auto__", default=None, metavar="CSV",
                   help="read trades.csv, print signal quality stats and exit "
                        "(no arg = the default ledger path)")
    p.add_argument("--state", default=None, help="state file path (default state.json)")
    p.add_argument("--no-state", action="store_true",
                   help="do not save/restore state (fresh start every launch)")
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
    if args.scan is not None:
        cfg.scan_max_candidates = max(1, args.scan if args.scan else 8)
    if args.refresh:
        cfg.watch_interval_s = max(15.0, args.refresh)
    if args.fomo is not None:
        cfg.fomo_enabled = bool(args.fomo)
    if args.fomo_include_slow:
        cfg.fomo_include_slow = True
    if args.fomo_max_tokens is not None:
        cfg.fomo_max_tokens_per_scan = max(0, args.fomo_max_tokens)
    if args.full:
        cfg.dashboard_compact = False
    if args.research is not None:
        cfg.research_enabled = bool(args.research)
    if args.obscura:
        cfg.obscura_bin = args.obscura
    if args.research_required:
        cfg.research_required = True
    if args.research_channels:
        cfg.research_channels = args.research_channels
    if args.research_storage_dir:
        cfg.research_storage_dir = args.research_storage_dir
    if args.state:
        cfg.state_file = args.state
    if args.no_state:
        cfg.state_enabled = False
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


async def async_scan(cfg: Config, limit: Optional[int] = None,
                     as_json: bool = False) -> int:
    """
    One-shot research scan. Prints signal cards and exits. No wallet, no orders,
    no ledger rows - scanning must never look like trading.
    """
    setup_logging(cfg)
    http = HttpClient(cfg)
    sources = Sources(cfg, http)
    telegram = Telegram(http)
    scanner = Scanner(cfg, http, sources, telegram)
    if telegram.enabled:
        print("Telegram is configured but --scan is one-shot: alerts are not sent.")
        print("Use --watch to deliver signals, or see README for the test procedure.")
    signals = await scanner.scan_once(limit=limit)
    now = time.time()
    if as_json:
        print(json.dumps({
            "scanned_at_utc": utc_str(now),
            "mode": "PAPER (research only)",
            "fomo": sources.fomo_health(),
            "signals": [s.as_dict() for s in signals],
        }, indent=1, sort_keys=True))
        return 0
    print("=" * 58)
    print(" SIGNAL SCAN  {}".format(utc_str(now)))
    print(" mode: PAPER RESEARCH ONLY - no orders, no wallet, no keys")
    print(" fomo: {}".format(sources.fomo_health().get("status", "off")))
    print(" telegram: {} (one-shot scans do not send)".format(
        "configured" if telegram.enabled else "off"))
    print("=" * 58)
    if not signals:
        print(" No candidates this pass. That is normal with these filters.")
        print(" Feeds answered: {}".format(
            ", ".join("{} {}/{}".format(n.split("_")[0], st["ok"], st["err"])
                      for n, st in scanner.feed_health.items())))
        return 0
    for sig in signals:
        print(sig.card(now=now))
        print("")
    counts: Dict[str, int] = {}
    for sig in signals:
        counts[sig.status] = counts.get(sig.status, 0) + 1
    print("summary: " + ", ".join("{} {}".format(v, k) for k, v in sorted(counts.items())))
    print("reminder: CONFIRMED means 'passed the filters twice', not 'will go up'.")
    print("          A $2 round trip costs roughly $0.34 in fixed Solana fees.")
    return 0


async def async_watch(cfg: Config, iterations: int = 0, limit: Optional[int] = None,
                      quiet: bool = False, as_json: bool = False) -> int:
    setup_logging(cfg)
    http = HttpClient(cfg)
    sources = Sources(cfg, http)
    telegram = Telegram(http)
    scanner = Scanner(cfg, http, sources, telegram)
    try:
        return await scanner.run_watch(iterations=iterations, limit=limit, quiet=quiet,
                                       as_json=as_json)
    except KeyboardInterrupt:
        scanner.book.save(cfg.signal_file)
        print("\nInterrupted. Signals saved to {}.".format(cfg.signal_file))
        return 130


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

    # ---------- 1b. signal quality: copycats, tape, second look ------------
    check("copycat ticker rejected", name_quality("USDC", "USD Coin") is not None,
          str(name_quality("USDC", "USD Coin")))
    check("hidden character in symbol rejected",
          name_quality("TEST\u200b", "Test") is not None)
    check("bait text rejected",
          name_quality("CLAIM", "claim your airdrop now") is not None)
    check("clean metadata accepted", name_quality("BONK2", "Bonk Two") is None,
          str(name_quality("BONK2", "Bonk Two")))
    copy_snap = _mk_snap(GOOD)
    copy_snap.symbol, copy_snap.name = "USDC", "USD Coin"
    copy = evaluate_safety(copy_snap, _mk_facts(GOOD), {"priceImpactPct": "0.004"}, cfg,
                           providers=3)
    check("copycat blocked by the safety filter",
          not copy.passed and any("name_quality" in r for r in copy.reasons), str(copy.reasons))

    one_prov = evaluate_safety(_mk_snap(GOOD), _mk_facts(GOOD), {"priceImpactPct": "0.004"},
                               cfg, providers=1)
    check("single-provider evidence rejected",
          not one_prov.passed and any("corroboration" in r for r in one_prov.reasons),
          str(one_prov.reasons))
    two_prov = evaluate_safety(_mk_snap(GOOD), _mk_facts(GOOD), {"priceImpactPct": "0.004"},
                               cfg, providers=2)
    check("two independent providers pass", two_prov.passed, two_prov.line())

    thin_tape = MicroStats(trades_seen=5, buys_5m=4, sells_5m=1, buy_vol_5m=30.0,
                           sell_vol_5m=5.0, buyers_5m=2)
    check("thin tape rejected", tape_quality(thin_tape, cfg) is not None,
          str(tape_quality(thin_tape, cfg)))
    thin_vol = MicroStats(trades_seen=40, buys_5m=25, sells_5m=15, buy_vol_5m=120.0,
                          sell_vol_5m=60.0, buyers_5m=12)
    check("low-volume tape rejected", tape_quality(thin_vol, cfg) is not None,
          str(tape_quality(thin_vol, cfg)))
    fat_tape = MicroStats(trades_seen=60, buys_5m=38, sells_5m=22, buy_vol_5m=900.0,
                          sell_vol_5m=350.0, buyers_5m=18)
    check("healthy tape accepted", tape_quality(fat_tape, cfg) is None,
          str(tape_quality(fat_tape, cfg)))
    check("no tape at all rejected", tape_quality(None, cfg) is not None)

    check("second look rejects a price drop",
          confirmation_verdict(1.0, 0.94, 90.0, cfg) is not None)
    check("second look rejects a lost score",
          confirmation_verdict(1.0, 1.0, 55.0, cfg) is not None)
    check("second look accepts a still-valid setup",
          confirmation_verdict(1.0, 1.02, 88.0, cfg) is None,
          str(confirmation_verdict(1.0, 1.02, 88.0, cfg)))
    check("momentum threshold is stricter than before", cfg.momentum_threshold >= 70)

    # ---------- 1c. web research skill (Obscura, fake browser) -------------
    def page(lines: List[str]) -> str:
        return "\n".join(lines)

    clean_page = page([
        "Dashboard | $GOODMI price chart",
        "$GOODMI is trending on Solana today, liquidity locked",
        "community says $GOODMI listing announcement coming",
        "new memecoin $GOODMI just launched on pump.fun",
    ])
    # Same scam words, but on lines that never mention the token: must not count.
    unrelated_page = page([
        "Some other coin was a rug pull and a honeypot last week",
        "unrelated scam warning for a different project",
        "weather is nice",
    ])
    bad_page = page([
        "$GOODMI is a honeypot, you cannot sell it",
        "warning: $GOODMI looks like a rug pull",
        "$GOODMI contract exploit reported",
    ])

    m_clean = research_mentions(clean_page, "GOODMI")
    check("research counts token mentions", m_clean.mentions >= 4, str(m_clean.mentions))
    check("research counts hype words on token lines", m_clean.hype_hits >= 2,
          str(m_clean.hype_hits))
    m_unrel = research_mentions(unrelated_page, "GOODMI")
    check("research ignores scam words on unrelated lines",
          m_unrel.mentions == 0 and m_unrel.scam_hits == 0, str(m_unrel))
    m_bad = research_mentions(bad_page, "GOODMI")
    check("research counts scam words next to the ticker", m_bad.scam_hits >= 3,
          str(m_bad.scam_hits))
    m_mint = research_mentions("see " + GOOD + " on solscan", "ZZZ", GOOD)
    check("research matches the mint address", m_mint.mentions >= 1, str(m_mint))

    per_boost = {"a": dict(m_clean.as_dict(), ok=True), "b": dict(m_clean.as_dict(), ok=True)}
    r_boost = research_verdict(per_boost, cfg)
    check("clean chatter boosts the score",
          r_boost.verdict == "boost" and r_boost.delta > 0, r_boost.line())
    per_veto = {"a": dict(m_bad.as_dict(), ok=True), "b": dict(m_bad.as_dict(), ok=True)}
    r_veto = research_verdict(per_veto, cfg)
    check("bad news on two sources vetoes", r_veto.verdict == "veto", r_veto.line())
    m_mild = research_mentions("$GOODMI had a rug scare last year, recovered since", "GOODMI")
    r_one = research_verdict({"a": dict(m_mild.as_dict(), ok=True)}, cfg)
    check("a single mild bad mention is not enough to veto",
          r_one.verdict != "veto", r_one.line())
    r_loud = research_verdict({"a": dict(m_bad.as_dict(), ok=True)}, cfg)
    check("one page screaming honeypot does veto", r_loud.verdict == "veto", r_loud.line())
    r_none = research_verdict({"a": {"ok": False, "error": "boom"}}, cfg)
    check("no answer is reported as unavailable, not neutral",
          r_none.verdict == "unavailable", r_none.line())
    r_blocked = research_verdict({"a": {"ok": False, "blocked": True}}, cfg)
    check("blocked pages are counted", r_blocked.blocked == 1, r_blocked.line())

    base_src = research_sources(cfg, "GOODMI", GOOD)
    check("research sources are capped", len(base_src) <= cfg.research_max_sources,
          str([x["name"] for x in base_src]))
    cfg_x = replace(cfg, research_storage_dir="/tmp/cookies", research_max_sources=9)
    names_x = [x["name"] for x in research_sources(cfg_x, "GOODMI", GOOD)]
    check("x.com search only appears when cookies are configured",
          "x_search" in names_x and "x_search" not in
          [x["name"] for x in research_sources(replace(cfg_x, research_storage_dir=""),
                                               "GOODMI", GOOD)], str(names_x))
    names_tg = [x["name"] for x in research_sources(
        replace(cfg_x, research_channels="pumpfun, solana"), "GOODMI", GOOD)]
    check("telegram channels become sources",
          any(n.startswith("telegram:pumpfun") for n in names_tg), str(names_tg))

    # a fake browser: proves the wrapper's cache, error and blocked handling
    launch_log: List[List[str]] = []

    def fake_runner(args: List[str], timeout: float) -> Tuple[int, str, str]:
        launch_log.append(args)
        url = next((a for a in args if a.startswith("http")), "")
        if "duckduckgo" in url:
            return 0, clean_page, ""
        if "bing" in url:
            return 0, bad_page, ""
        if "reddit" in url:
            return 1, "", "boom"
        if "x.com" in url:
            return 0, "Sign in to X. Enable JavaScript to continue.", ""
        return 0, "", ""

    ob_cfg = replace(cfg, obscura_bin="", research_min_interval_s=0.0,
                     research_max_sources=3)
    os.environ["OBSCURA_BIN"] = "/bin/echo"      # exists, is executable, harmless
    ob = Obscura(ob_cfg, runner=fake_runner)
    check("obscura binary is discovered from the environment", ob.bin == "/bin/echo",
          ob.bin)
    got_ok = asyncio.run(ob.fetch("https://duckduckgo.com/x"))
    check("fetch returns page text", got_ok.ok and "GOODMI" in got_ok.text, got_ok.error)
    again = asyncio.run(ob.fetch("https://duckduckgo.com/x"))
    check("repeat fetch is served from cache",
          again.from_cache and len(launch_log) == 1, str(len(launch_log)))
    got_fail = asyncio.run(ob.fetch("https://reddit.com/x"))
    check("a dead source fails without raising", (not got_fail.ok) and got_fail.error == "boom",
          got_fail.error)
    got_wall = asyncio.run(ob.fetch("https://x.com/search"))
    check("login walls are detected, not parsed", got_wall.blocked and not got_wall.ok,
          got_wall.error)
    check("obscura command carries the documented flags",
          all(flag in ob.command("https://x.com") for flag in
              ("fetch", "--dump", "markdown", "--stealth", "--timeout")),
          str(ob.command("https://x.com")))
    os.environ.pop("OBSCURA_BIN", None)
    check("without a binary research reports unavailable",
          (not Obscura(replace(cfg, obscura_bin="")).available)
          and asyncio.run(Obscura(replace(cfg, obscura_bin="")).fetch("https://x.com")).ok is False,
          "should be unavailable")

    # swarm-level: veto blocks a real (fake-market) confirmation, boost scores it up
    async def research_gate_tests() -> None:
        rcfg = replace(cfg, research_enabled=True, research_min_interval_s=0.0,
                       ledger_csv=os.path.join(tmpdir, "research_trades.csv"),
                       log_file=os.path.join(tmpdir, "research_bot.log"),
                       state_file=os.path.join(tmpdir, "research_state.json"),
                       state_enabled=False, momentum_threshold=0.0,
                       research_max_sources=2)
        client = HttpClient(rcfg, transport=transport)
        src = Sources(rcfg, client)
        lg = Ledger(rcfg.ledger_csv, "PAPER")
        pf = Portfolio(rcfg)
        sw = Swarm(rcfg, client, src, lg, Telegram(client),
                   PaperExecutor(rcfg, pf, src), portfolio=pf)
        sw.obscura = Obscura(rcfg, runner=fake_runner)

        # The token's own DEX page repeats the ticker; that must not read as hype.
        own_sw = Swarm(replace(rcfg, research_max_sources=1, research_min_interval_s=0.0,
                               obscura_bin="/bin/echo", state_enabled=False,
                               ledger_csv=os.path.join(tmpdir, "own_trades.csv")),
                       HttpClient(rcfg, transport=transport), src, lg, Telegram(client),
                       PaperExecutor(rcfg, pf, src), portfolio=pf)

        def own_runner(args: List[str], timeout: float) -> Tuple[int, str, str]:
            if any("dexscreener" in a for a in args):
                return 0, clean_page, ""
            return 1, "", "no"

        own_sw.obscura = Obscura(own_sw.cfg, runner=own_runner)
        own = await own_sw.research(GOOD, "GOODMI")
        check("the token's own DEX page is not counted as chatter",
              own.verdict == "neutral" and own.sources_ok == 1, own.line())

        async def fake_research(mint: str, symbol: str, name: str = "") -> ResearchResult:
            return ResearchResult(verdict="veto", summary="bad news on 2/2 pages",
                                  sources_ok=2, scam_hits=5, mentions=5)

        entry = {"due": 0.0, "first_seen": time.time() - 2, "price": 0.001, "score": 83.0,
                 "components": {}, "agent": "momentum0",
                 "item": {"mint": GOOD, "symbol": "GOODMI", "source": "test"}}
        sw.research = fake_research          # type: ignore[assignment]
        await sw._confirm_candidate(GOOD, entry)
        vetoed = not sw.signals.qsize()
        rows = open(rcfg.ledger_csv, encoding="utf-8").read() if os.path.exists(rcfg.ledger_csv) else ""
        check("research veto stops a would-be signal",
              vetoed and "research_veto" in rows, rows[-200:])

        async def fake_boost(mint: str, symbol: str, name: str = "") -> ResearchResult:
            return ResearchResult(verdict="boost", delta=100.0, summary="lots of clean chatter",
                                  sources_ok=2, mentions=9)

        sw.research = fake_boost             # type: ignore[assignment]
        await sw._confirm_candidate(GOOD, entry)
        sig = sw.signals.get_nowait() if sw.signals.qsize() else {}
        check("research boost is applied and clamped to 100",
              sig.get("score") == 100.0 and bool(sig.get("research")), str(sig.get("score")))
        check("research availability reaches the heartbeat",
              (sw._write_heartbeat() or True) and "research" in
              json.load(open(sw.cfg.heartbeat_file, encoding="utf-8")))

    asyncio.run(research_gate_tests())

    parsed, _args = build_config(["--no-research", "--obscura", "/tmp/x"])
    check("--no-research and --obscura reach the config",
          parsed.research_enabled is False and parsed.obscura_bin == "/tmp/x")
    parsed2, _args2 = build_config(["--research-required", "--research-channels", "a,b"])
    check("--research-required and --research-channels reach the config",
          parsed2.research_required and parsed2.research_channels == "a,b")

    # ---------- 1d. FOMO provider, signals, telegram (all offine) ----------
    if _fomo_mod is None:
        check("fomo_provider.py is importable", False, FOMO_IMPORT_ERROR)
    else:
        fm = _fomo_mod
        from fomo_provider import dedupe_mints as _dd

        check("fomo provider: dedupe drops invalid and duplicate mints",
              _dd([{"mint": GOOD}, {"mint": GOOD}, {"tokenAddress": "0xdead"},
                   "not-a-mint", BAD]) == [GOOD, BAD], str(_dd([{"mint": GOOD}, GOOD])))

        stats_payload = {"holders": 4211, "top10HoldersPercent": 21.5, "windows": {
            "5m": {"buys": 31, "sells": 12, "uniqueBuyers": 17, "uniqueSellers": 6,
                   "buyVolumeUsd": 5200.0, "sellVolumeUsd": 1300.0,
                   "netVolumeUsd": 3900.0, "buySellRatio": 2.58},
            "1h": {"buys": 210, "sells": 150, "uniqueBuyers": 130, "uniqueSellers": 90,
                   "buyVolumeUsd": 61000.0, "sellVolumeUsd": 22000.0,
                   "netVolumeUsd": 39000.0, "buySellRatio": 1.4}}}
        stats = fm.FomoProvider.parse_stats(GOOD, stats_payload)
        check("fomo: stats parsing normalizes windows",
              stats.window("5m").buys == 31 and stats.window("5m").unique_buyers == 17
              and abs(stats.window("5m").net_vol_usd - 3900.0) < 1e-9
              and abs(stats.top10_pct - 21.5) < 1e-9 and stats.holders == 4211,
              str(stats.as_dict())[:120])

        warn_payload = {"buyDisabled": False, "sellDisabled": True, "tradingDisabled": True,
                        "allowlisted": False, "severe": True, "count": 1,
                        "warnings": [{"name": "Cannot sell", "severity": "SEVERE",
                                      "type": "UNSELLABLE"}]}
        warns = fm.FomoProvider.parse_warnings(GOOD, warn_payload)
        blocks = warns.blocking_reasons()
        check("fomo: sell-disabled and severe warnings become blockers",
              warns.sell_disabled and warns.severe and len(blocks) >= 2, str(blocks))
        clean_warn = fm.FomoProvider.parse_warnings(GOOD, {"count": 0, "warnings": []})
        check("fomo: an empty warnings list is not treated as a pass",
              clean_warn.blocking_reasons() == [] and clean_warn.count == 0)

        # ---- transport fakes -------------------------------------------------
        def fomo_ok(url, headers, timeout):
            base = url.split("?")[0]
            if base.endswith("/stats"):
                return 200, {"x-credits-cost": "250", "x-credits-remaining": "240000"}, \
                    json.dumps(stats_payload).encode()
            if base.endswith("/warnings"):
                return 200, {"x-credits-cost": "250"}, json.dumps(
                    {"buyDisabled": False, "sellDisabled": False, "tradingDisabled": False,
                     "allowlisted": True, "severe": False, "count": 0,
                     "warnings": []}).encode()
            if base.endswith("/devs"):
                return 200, {}, json.dumps({"devs": [
                    {"handle": "dev", "amount": 500.0, "valueUsd": 900.0,
                     "realizedPnlUsd": 0.0, "unrealizedPnlUsd": 120.0, "isDev": True}]}).encode()
            return 404, {}, b"{}"

        p_ok = fm.FomoProvider(api_key="test-key", transport=fomo_ok, min_interval_s=0.0,
                               cache_ttl_s=60.0)
        first = p_ok.enrich_sync(GOOD)
        second = p_ok.enrich_sync(GOOD)
        check("fomo: enrichment parses stats + warnings",
              first is not None and first.complete and first.stats.holders == 4211,
              first.summary() if first else "None")
        check("fomo: cached tokens are re-served without new calls",
              second is not None and second.from_cache and p_ok.health()["calls"] == 2,
              "calls={}".format(p_ok.health()["calls"]))
        check("fomo: credits are tracked from response headers",
              p_ok.health()["credits_spent"] == 500, str(p_ok.health()["credits_spent"]))
        check("fomo: clean dev holdings do not trigger a dev-exit veto",
              p_ok.enrich_sync(GOOD).dev_exit_reason() == "")
        devs = fm.FomoProvider.parse_devs({"devs": [
            {"handle": "d", "amount": 0, "valueUsd": 0, "realizedPnlUsd": 4200.0}]})
        exited = fm.FomoEnrichment(mint=GOOD, devs=devs)
        check("fomo: deployer who sold out is detected",
              "sold out" in exited.dev_exit_reason(), exited.dev_exit_reason())

        # 429 then success (retry-after honoured)
        state = {"n": 0}

        def fomo_429(url, headers, timeout):
            state["n"] += 1
            if state["n"] == 1:
                return 429, {"retry-after": "0"}, b'{"error":"rate_limited"}'
            return 200, {}, json.dumps(stats_payload).encode()

        p_429 = fm.FomoProvider(api_key="k", transport=fomo_429, min_interval_s=0.0,
                                max_retries=2, sleep=lambda s: None)
        got = p_429.stats(GOOD)
        check("fomo: 429 is retried and counted",
              got.holders == 4211 and p_429.health()["rate_limited"] == 1
              and p_429.health()["retries"] >= 1, str(p_429.health()))

        # network failure
        def fomo_net(url, headers, timeout):
            raise OSError("connection reset")

        p_net = fm.FomoProvider(api_key="k", transport=fomo_net, min_interval_s=0.0,
                                max_retries=1, sleep=lambda s: None)
        try:
            p_net.stats(GOOD)
            net_raised = False
        except fm.FomoError as exc:
            net_raised = "network" in str(exc)
        check("fomo: network failures raise a typed error and update health",
              net_raised and p_net.health()["errors"] >= 1, str(p_net.health()))

        # malformed JSON
        p_bad = fm.FomoProvider(api_key="k", min_interval_s=0.0, max_retries=0,
                                transport=lambda u, h, t: (200, {}, b"{not json"))
        try:
            p_bad.stats(GOOD)
            bad_raised = ""
        except fm.FomoError as exc:
            bad_raised = exc.code
        check("fomo: malformed JSON is rejected, not parsed", bad_raised == "bad_json",
              bad_raised)

        # 401 must not be retried
        tries = {"n": 0}

        def fomo_401(url, headers, timeout):
            tries["n"] += 1
            return 401, {}, b'{"error":"unauthorized"}'

        p_401 = fm.FomoProvider(api_key="k", min_interval_s=0.0, transport=fomo_401)
        try:
            p_401.stats(GOOD)
        except fm.FomoError as exc:
            code401 = exc.code
        else:
            code401 = ""
        check("fomo: a bad key fails fast without retries",
              code401 == "unauthorized" and tries["n"] == 1, "{} tries={}".format(
                  code401, tries["n"]))

        # partial data: stats ok, warnings down
        def fomo_partial(url, headers, timeout):
            if url.split("?")[0].endswith("/stats"):
                return 200, {}, json.dumps(stats_payload).encode()
            return 503, {}, b'{"error":"upstream_degraded"}'

        p_part = fm.FomoProvider(api_key="k", transport=fomo_partial, min_interval_s=0.0,
                                 max_retries=0, sleep=lambda s: None)
        part = p_part.enrich_sync(GOOD)
        check("fomo: partial data is reported as partial, not complete",
              part is not None and part.available and not part.complete and part.errors,
              str(part.errors)[:100])
        check("fomo: no key means no calls and no fake data",
              fm.FomoProvider().enrich_sync(GOOD) is None
              and fm.FomoProvider().health()["status"].startswith("disabled"))

        # ---- safety integration ---------------------------------------------
        fomo_warn = fm.FomoEnrichment(
            mint=GOOD, stats=fm.FomoProvider.parse_stats(GOOD, stats_payload),
            warnings=fm.FomoProvider.parse_warnings(GOOD, warn_payload))
        fomo_bad = evaluate_safety(_mk_snap(GOOD), _mk_facts(GOOD),
                                   {"priceImpactPct": "0.004"}, cfg, providers=4,
                                   fomo=fomo_warn)
        check("fomo: sell-disabled token is rejected by the safety filter",
              not fomo_bad.passed and any("fomo_tradable" in r for r in fomo_bad.reasons),
              str(fomo_bad.reasons))

        concentrated = dict(stats_payload)
        concentrated["top10HoldersPercent"] = 55.0
        fomo_top = fm.FomoEnrichment(
            mint=GOOD, stats=fm.FomoProvider.parse_stats(GOOD, concentrated),
            warnings=fm.FomoProvider.parse_warnings(GOOD, {"count": 0, "warnings": []}))
        top_res = evaluate_safety(_mk_snap(GOOD), _mk_facts(GOOD),
                                  {"priceImpactPct": "0.004"}, cfg, providers=4,
                                  fomo=fomo_top)
        check("fomo: holder concentration from FOMO is enforced",
              not top_res.passed and any("fomo_top10" in r for r in top_res.reasons),
              str(top_res.reasons))

        dev_res = evaluate_safety(_mk_snap(GOOD), _mk_facts(GOOD),
                                  {"priceImpactPct": "0.004"}, cfg, providers=4,
                                  fomo=exited)
        check("fomo: a dev who exited blocks the token",
              not dev_res.passed and any("fomo_dev_exit" in r for r in dev_res.reasons),
              str(dev_res.reasons))

        no_fomo = evaluate_safety(_mk_snap(GOOD), _mk_facts(GOOD),
                                  {"priceImpactPct": "0.004"}, cfg, providers=3)
        check("fomo: without a provider the checks are unchanged",
              no_fomo.passed and not any("fomo" in r for r in no_fomo.reasons))

        # ---- scoring delta ---------------------------------------------------
        delta, notes = fomo_flow_delta(p_ok.enrich_sync(GOOD), cfg)
        check("fomo: confirming flow adds a capped, explained score bonus",
              0 < delta <= cfg.fomo_score_boost and len(notes) == 2,
              "{} {}".format(delta, notes))
        check("fomo: missing data adds nothing",
              fomo_flow_delta(None, cfg) == (0.0, []))

    # ---- signals, book, telegram, scan pipeline ---------------------------
    good_sig = build_signal(cfg, GOOD, _mk_snap(GOOD), _mk_facts(GOOD), fat_tape,
                            momentum_score(_mk_snap(GOOD), fat_tape, cfg),
                            evaluate_safety(_mk_snap(GOOD), _mk_facts(GOOD),
                                            {"priceImpactPct": "0.004"}, cfg, providers=3),
                            now=time.time())
    check("signal: a strong setup becomes a CANDIDATE",
          good_sig.status == SIGNAL_CANDIDATE and good_sig.score >= 70, good_sig.status)
    check("signal: entry zone brackets the current price",
          good_sig.entry_low < good_sig.price <= good_sig.entry_high,
          "{} {} {}".format(good_sig.entry_low, good_sig.price, good_sig.entry_high))
    check("signal: invalidation is below entry and targets above",
          good_sig.invalidation < good_sig.entry_low < good_sig.target_1 < good_sig.target_2,
          "{} {} {}".format(good_sig.invalidation, good_sig.target_1, good_sig.target_2))
    check("signal: R:R is stop-vs-first-target arithmetic",
          abs(good_sig.rr - abs(cfg.tp1_pct / cfg.stop_loss_pct)) < 0.2, str(good_sig.rr))

    bad_sig = build_signal(cfg, BAD, _mk_snap(BAD, liq=9000), _mk_facts(BAD), None,
                           momentum_score(_mk_snap(BAD, liq=9000), None, cfg),
                           evaluate_safety(_mk_snap(BAD, liq=9000), _mk_facts(BAD), None,
                                           cfg, providers=2), now=time.time())
    check("signal: a failed safety filter is REJECTED with reasons",
          bad_sig.status == SIGNAL_REJECTED and bad_sig.reject_reasons, bad_sig.status)

    weak = MomentumResult(score=cfg.momentum_threshold - 15.0, components={"trend_1h": 10.0},
                          weights={"trend_1h": 1.0}, notes=["synthetic weak tape"])
    weak_sig = build_signal(cfg, GOOD, _mk_snap(GOOD), _mk_facts(GOOD), MicroStats(),
                            weak, evaluate_safety(_mk_snap(GOOD), _mk_facts(GOOD),
                                                 {"priceImpactPct": "0.004"}, cfg,
                                                 providers=3), now=time.time())
    check("signal: weak momentum is WATCH, not a buy signal",
          weak_sig.status in (SIGNAL_WATCH, SIGNAL_REJECTED) and weak_sig.score < cfg.momentum_threshold,
          "{} {}".format(weak_sig.status, weak_sig.score))

    old_snap = _mk_snap(GOOD)
    old_snap.ts = time.time() - 300          # a quote that is five minutes old
    aged = build_signal(cfg, GOOD, old_snap, _mk_facts(GOOD), fat_tape,
                        momentum_score(old_snap, fat_tape, cfg), None, now=time.time())
    _tg = good_sig.telegram()
    _want = "entry idea: ${} - ${}".format(fmt_price(good_sig.entry_low),
                                           fmt_price(good_sig.entry_high))
    check("telegram: card text has no exponent notation for prices",
          _want in _tg and "e-0" not in _tg,
          [ln for ln in _tg.splitlines() if ln.startswith("entry idea")][0][:44])

    check("format: sub-cent prices print as decimals, never as 1.2e-05",
          fmt_price(0.0000123) == "0.0000123" and fmt_price(4.2e-08) == "0.000000042"
          and fmt_price(0.5) == "0.5" and fmt_price(None) == "?",
          "{} / {}".format(fmt_price(0.0000123), fmt_price(4.2e-08)))

    check("signal: data age is reported, not hidden",
          aged.data_age_s_now() >= 295 and "300s old" in aged.card(),
          "{:.0f}s age, card says {}".format(aged.data_age_s_now(),
                                             aged.card().splitlines()[5]))

    card = good_sig.card()
    lines_ok = all(len(ln) <= 60 for ln in card.splitlines())
    check("signal: the terminal card shows mint, levels and the paper notice",
          GOOD in card and "ENTRY" in card and "INVAL" in card
          and "PAPER MODE - RESEARCH ONLY" in card and lines_ok,
          "width_ok={} len={}".format(lines_ok, len(card.splitlines())))
    tg_text = good_sig.telegram()
    check("signal: the telegram message carries mint, score and the paper notice",
          GOOD in tg_text and "score" in tg_text
          and "PAPER MODE - NO REAL TRADE EXECUTED" in tg_text
          and "entry idea" in tg_text, tg_text[:120])

    # ---- signal book: confirmations, invalidation, expiry, alerts ---------
    book = SignalBook(cfg)
    t0 = time.time()
    s1 = build_signal(cfg, GOOD, _mk_snap(GOOD), _mk_facts(GOOD), fat_tape,
                      momentum_score(_mk_snap(GOOD), fat_tape, cfg),
                      evaluate_safety(_mk_snap(GOOD), _mk_facts(GOOD),
                                      {"priceImpactPct": "0.004"}, cfg, providers=3),
                      now=t0, first_seen=t0)
    book.confirm(s1, now=t0)
    check("book: confirmation is recorded",
          book.signals[GOOD].status == SIGNAL_CONFIRMED, book.signals[GOOD].status)
    check("book: alert fires once, then respects the cooldown",
          book.should_alert(s1, now=t0) is True
          and book.should_alert(s1, now=t0 + 5) is False
          and book.should_alert(s1, now=t0 + cfg.telegram_signal_cooldown_s + 1) is True)

    drop = build_signal(cfg, GOOD, _mk_snap(GOOD, price=s1.invalidation * 0.9),
                        _mk_facts(GOOD), fat_tape,
                        momentum_score(_mk_snap(GOOD), fat_tape, cfg), None,
                        now=t0 + 10, first_seen=t0)
    book.upsert(drop, now=t0 + 10)
    check("book: price under invalidation INVALIDATES a confirmed signal",
          book.signals[GOOD].status == SIGNAL_INVALIDATED, book.signals[GOOD].status)

    stale_cfg = replace(cfg, signal_expiry_s=1.0)
    stale_book = SignalBook(stale_cfg)
    stale = build_signal(stale_cfg, BAD, _mk_snap(BAD), _mk_facts(BAD), None, None, None,
                         now=t0, first_seen=t0)
    stale.status = SIGNAL_WATCH
    stale_book.upsert(stale, now=t0 + 5)
    check("book: an old unconfirmed signal EXPIRES",
          stale_book.signals[BAD].status == SIGNAL_EXPIRED, stale_book.signals[BAD].status)

    # ---- telegram: disabled by default, enabled queues, dedupe ------------
    saved_tg = (os.environ.pop("TELEGRAM_BOT_TOKEN", None),
                os.environ.pop("TELEGRAM_CHAT_ID", None))
    try:
        tg_off = Telegram(http)
        tg_off.notify("hello")
        check("telegram: disabled without credentials and never blocks the terminal",
              tg_off.enabled is False and tg_off.queue.qsize() == 0)
        os.environ["TELEGRAM_BOT_TOKEN"] = "123:fake"
        os.environ["TELEGRAM_CHAT_ID"] = "42"
        tg_on = Telegram(http)
        scanner = Scanner(replace(cfg, scan_confirm_delay_s=0.0), http, sources, tg_on)
        scanner.book.confirm(build_signal(
            cfg, GOOD, _mk_snap(GOOD), _mk_facts(GOOD), fat_tape,
            momentum_score(_mk_snap(GOOD), fat_tape, cfg),
            evaluate_safety(_mk_snap(GOOD), _mk_facts(GOOD), {"priceImpactPct": "0.004"},
                            cfg, providers=3), now=t0), now=t0)
        first_alert = scanner._alert_new()
        second_alert = scanner._alert_new()
        check("telegram: a confirmed signal is queued when credentials exist",
              tg_on.enabled and first_alert == 1 and tg_on.queue.qsize() == 1,
              "{} {}".format(first_alert, tg_on.queue.qsize()))
        check("telegram: the same signal is never sent twice in a row",
              second_alert == 0 and tg_on.queue.qsize() == 1, str(second_alert))
        queued = tg_on.queue.get_nowait()
        check("telegram: the queued message is a PAPER-mode signal card",
              "PAPER MODE - NO REAL TRADE EXECUTED" in queued and GOOD in queued)
    finally:
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        if saved_tg[0] is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = saved_tg[0]
        if saved_tg[1] is not None:
            os.environ["TELEGRAM_CHAT_ID"] = saved_tg[1]

    # ---- scan pipeline end to end against the canned feeds ----------------
    scan_cfg = replace(cfg, scan_confirm_delay_s=0.0, scan_max_candidates=4,
                       signal_file=os.path.join(tmpdir, "signals.json"),
                       ledger_csv=os.path.join(tmpdir, "scan_trades.csv"),
                       log_file=os.path.join(tmpdir, "scan_bot.log"),
                       state_file=os.path.join(tmpdir, "scan_state.json"),
                       research_enabled=False, fomo_enabled=False)
    transport.t0 = time.time()

    async def scan_run():
        client = HttpClient(scan_cfg, transport=transport)
        src = Sources(scan_cfg, client)
        sc = Scanner(scan_cfg, client, src, Telegram(client))
        out = await sc.scan_once(limit=4)
        return sc, out

    scan, out = asyncio.run(scan_run())
    by_mint = {s.mint: s for s in out}
    check("scan: finds the good token and confirms it",
          by_mint.get(GOOD) is not None and by_mint[GOOD].status == SIGNAL_CONFIRMED,
          str({k: v.status for k, v in by_mint.items()}))
    check("scan: rejects the scam token with a reason",
          by_mint.get(BAD) is not None and by_mint[BAD].status == SIGNAL_REJECTED
          and by_mint[BAD].reject_reasons, str(by_mint.get(BAD)))
    check("scan: writes signals.json on demand",
          scan.book.save(scan_cfg.signal_file) and os.path.exists(scan_cfg.signal_file)
          and "PAPER" in open(scan_cfg.signal_file, encoding="utf-8").read())
    dash = scan.dashboard()
    check("scan: dashboard shows providers, statuses and the paper notice",
          "feeds" in dash and "CONFIRMED" in dash and "paper:" in dash, dash[:120])
    check("scan: scan mode never touches the portfolio",
          "never trades" in dash)

    # ---- CLI wiring -------------------------------------------------------
    sc_args, _ = build_config(["--scan", "5"])
    check("cli: --scan sets the candidate cap", sc_args.scan_max_candidates == 5,
          str(sc_args.scan_max_candidates))
    w_args, _ = build_config(["--watch", "--refresh", "30", "--iterations", "2"])
    check("cli: --watch sets the refresh interval and iteration cap",
          w_args.watch_interval_s == 30.0,
          str(w_args.watch_interval_s))
    nf, _ = build_config(["--no-fomo"])
    check("cli: --no-fomo disables the provider", nf.fomo_enabled is False)
    slow, _ = build_config(["--fomo-include-slow", "--fomo-max-tokens", "1"])
    check("cli: fomo budget flags reach the config",
          slow.fomo_include_slow is True and slow.fomo_max_tokens_per_scan == 1)

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

    # ---------- 4b. restart-safe state ------------------------------------
    async def state_roundtrip() -> Tuple[bool, str]:
        stcfg = replace(cfg, state_file=os.path.join(tmpdir, "state.json"),
                        heartbeat_file=os.path.join(tmpdir, "heartbeat.json"),
                        ledger_csv=os.path.join(tmpdir, "state_trades.csv"),
                        log_file=os.path.join(tmpdir, "state_bot.log"))
        c1 = HttpClient(stcfg, transport=transport)
        pf1 = Portfolio(stcfg)
        pf1.cash_usd = 6.5
        sw1 = Swarm(stcfg, c1, Sources(stcfg, c1), Ledger(stcfg.ledger_csv, "PAPER"),
                    Telegram(c1), PaperExecutor(stcfg, pf1, Sources(stcfg, c1)), portfolio=pf1)
        pos = _mk_pos()
        pos.pos_id, pos.decimals, pos.tp1_done, pos.realized_usd = "rt1", 9, True, 0.4
        pf1.positions[pos.pos_id] = pos
        sw1.risk.daily_realized_usd = -1.25
        sw1.risk.cooldowns[GOOD] = time.time() + 120
        await sw1.save_state()

        c2 = HttpClient(stcfg, transport=transport)
        pf2 = Portfolio(stcfg)
        sw2 = Swarm(stcfg, c2, Sources(stcfg, c2), Ledger(stcfg.ledger_csv, "PAPER"),
                    Telegram(c2), PaperExecutor(stcfg, pf2, Sources(stcfg, c2)), portfolio=pf2)
        ok = await sw2.load_state()
        restored = sw2.pf.open_positions()
        good = (ok and len(restored) == 1 and restored[0].pos_id == "rt1"
                and restored[0].decimals == 9 and restored[0].tp1_done
                and abs(sw2.pf.cash_usd - 6.5) < 1e-9
                and abs(sw2.risk.daily_realized_usd + 1.25) < 1e-9
                and GOOD in sw2.risk.cooldowns)
        detail = "ok={} n={} cash={} daily={}".format(ok, len(restored), sw2.pf.cash_usd,
                                                      sw2.risk.daily_realized_usd)
        # a state file from another mode must never be imported
        with open(stcfg.state_file, encoding="utf-8") as fh:
            blob = json.load(fh)
        blob["mode"] = "live" if not stcfg.live else "paper"
        with open(stcfg.state_file, "w", encoding="utf-8") as fh:
            json.dump(blob, fh)
        refused = not await sw2.load_state()
        # heartbeat + dashboard must work straight after a restore (no live facts)
        sw2._write_heartbeat()
        try:
            with open(sw2.cfg.heartbeat_file, encoding="utf-8") as fh:
                hb = json.load(fh)
        except (OSError, ValueError):
            hb = {}
        hb_ok = bool(hb) and hb.get("open_positions") == 1 and "http" in hb
        dash_ok = "OPEN POSITIONS" in sw2.dashboard()
        return (good and refused and hb_ok and dash_ok), (
            detail + " cross_mode_refused={} heartbeat={} dashboard={}".format(
                refused, hb_ok, dash_ok))

    state_ok, state_detail = asyncio.run(state_roundtrip())
    check("state survives a restart (cash, position, daily loss, cooldown)", state_ok,
          state_detail)

    # ---------- 4c. signal analyzer ----------------------------------------
    ana_path = os.path.join(tmpdir, "analyze_trades.csv")
    with open(ana_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(LEDGER_FIELDS)
        def row(ev, pos="", pnl=0.0, fee=0.0, reason="", score=0.0, sym="AAA"):
            w.writerow([utc_str(), "PAPER", "t", ev, pos, GOOD, sym, "",
                        "", "", "", fee or "", pnl or "", score or "", reason, ""])
        row("REJECT", reason="second look: confirm_safety: liquidity; top10")
        row("REJECT", reason="liquidity; top10")
        row("REJECT", reason="confirm_price_dropped -6.1%")
        row("RESEARCH", reason="VETO | bad news on 2/2 pages")      # early look: bad
        row("RESEARCH", reason="BOOST | 9 mentions, 0 scam | chatter")  # later: positive
        row("AWAIT_CONFIRM", reason="momentum 86.0")
        row("SIGNAL", reason="confirmed after 25s", score=86.0)
        row("SIGNAL", reason="confirmed after 26s", score=74.0)
        row("BUY", pos="p1", fee=0.17, score=86.0)
        row("BUY", pos="p2", fee=0.17, score=74.0)
        row("SELL", pos="p1", pnl=1.10, fee=0.09, reason="TP1: ladder")
        row("CLOSE", pos="p1", pnl=1.10, reason="TP1: ladder")
        row("SELL", pos="p2", pnl=-1.40, fee=0.09, reason="STOP: hard stop")
        row("CLOSE", pos="p2", pnl=-1.40, reason="STOP: hard stop")
    ana_text, ana = analyze_ledger(ana_path, 10.0)
    check("analyzer counts rejections per filter",
          ana.get("rejects", {}).get("top10") == 2
          and ana.get("rejects", {}).get("liquidity") == 1
          and ana.get("rejects", {}).get("confirm_safety") == 1,
          str(ana.get("rejects")))
    check("analyzer counts signals, buys and closed trades",
          ana.get("signals") == 2 and ana.get("buys") == 2 and ana.get("closed") == 2,
          str({k: ana.get(k) for k in ("signals", "buys", "closed")}))
    check("analyzer computes net pnl and win rate",
          abs(ana.get("realized", 0.0) + 0.30) < 1e-9 and abs(ana.get("win_rate", 0) - 0.5) < 1e-9,
          "realized={} win_rate={}".format(ana.get("realized"), ana.get("win_rate")))
    check("analyzer refuses to call 2 trades a strategy",
          ana.get("verdict", "").startswith("NOT ENOUGH DATA"), ana.get("verdict", ""))
    check("analyzer counts research verdicts",
          ana.get("research", {}).get("boost") == 1
          and ana.get("research", {}).get("veto") == 1, str(ana.get("research")))
    check("analyzer uses the latest news verdict before each buy",
          ana.get("news_by_pos", {}).get("p1") == "boost", str(ana.get("news_by_pos")))
    check("analyzer report is printable", "SIGNAL QUALITY REPORT" in ana_text)
    missing_text, missing = analyze_ledger(os.path.join(tmpdir, "nope.csv"))
    check("analyzer handles a missing ledger", missing.get("ok") is False
          and "No ledger" in missing_text, missing_text[:60])

    # ---------- 5. full pipeline with a fake network -----------------------
    tmp = tmpdir
    transport.t0 = time.time()          # restart the scripted price crash
    tcfg = replace(cfg, ledger_csv=os.path.join(tmp, "trades.csv"),
                   log_file=os.path.join(tmp, "bot.log"),
                   report_file=os.path.join(tmp, "report.txt"),
                   state_file=os.path.join(tmp, "state.json"),
                   heartbeat_file=os.path.join(tmp, "heartbeat.json"),
                   momentum_threshold=0.0, dashboard_every_s=999.0, confirm_delay_s=1.0,
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
    check("entries wait for a second look before they can buy",
          "AWAIT_CONFIRM" in body and "confirmed after" in body)
    check("state file was written on shutdown", os.path.exists(tcfg.state_file),
          tcfg.state_file)

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
# SECTION 16b.  SIGNAL ANALYZER  (--analyze)
#
# The only way to "train" a rule set like this without a research budget is to
# measure it on your own trades and cut what does not pay. This reads trades.csv
# and answers three questions: what blocked entries, how often a signal became a
# trade, and whether the trades that happened made money after fees.
# =============================================================================

def clip_reason(reasons: Sequence[str], limit: int = 300) -> str:
    """
    Join check failures for the ledger without slicing a label in half.

    A truncated 'rug_dang' is not a reason anyone can act on, and the analyzer
    would count it as its own filter, so cut on item boundaries only.
    """
    out: List[str] = []
    used = 0
    for item in reasons:
        item = str(item).strip()
        if not item:
            continue
        if used + len(item) + 2 > limit:
            out.append("...")
            break
        out.append(item)
        used += len(item) + 2
    return "; ".join(out).replace("; ...", "... (more)") if out else "unspecified"


def _reject_labels(reason: str) -> List[str]:
    """'second look: confirm_safety: liquidity; top10' -> ['confirm_safety', ...]"""
    text = (reason or "").strip()
    for prefix in ("second look: ", "veto: "):
        if text.startswith(prefix):
            text = text[len(prefix):]
    labels = []
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        head = part.split(":")[0].strip()
        head = head.split(" ")[0].strip()
        if head:
            labels.append(head[:40])
    return labels or ["unspecified"]


def _score_bucket(score: float) -> str:
    if score <= 0:
        return "unknown"
    if score < 80:
        return "70-79"
    if score < 90:
        return "80-89"
    return "90-100"


def analyze_ledger(path: str, capital_usd: float = 0.0) -> Tuple[str, Dict[str, Any]]:
    """
    Pure function over the CSV: no network, no config, no side effects.
    Returns (human report, stats dict). Selftest asserts on the dict.
    """
    rows: List[Dict[str, str]] = []
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames or "event" not in reader.fieldnames:
                return ("{} does not look like a ledger (no 'event' column).".format(path),
                        {"ok": False, "rows": 0})
            rows = list(reader)
    except FileNotFoundError:
        return ("No ledger at {}. Run the bot (paper is fine) and try again.".format(path),
                {"ok": False, "rows": 0})
    except OSError as exc:
        return ("Cannot read {}: {}".format(path, exc), {"ok": False, "rows": 0})

    def fnum_row(v: Any) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    rejects: Dict[str, int] = {}
    events: Dict[str, int] = {}
    fees_usd = 0.0
    pnl_by_pos: Dict[str, float] = {}
    close_pnl: Dict[str, float] = {}
    score_by_pos: Dict[str, float] = {}
    symbol_by_pos: Dict[str, str] = {}
    buy_pos: List[str] = []
    partial: Dict[str, float] = {}
    research_verdicts: Dict[str, int] = {}
    research_by_mint: Dict[str, str] = {}
    news_by_pos: Dict[str, str] = {}
    signal_count = buy_count = await_count = 0

    for r in rows:
        ev = (r.get("event") or "").strip().upper()
        if not ev:
            continue
        events[ev] = events.get(ev, 0) + 1
        pos_id = (r.get("pos_id") or "").strip()
        reason = r.get("reason") or ""
        if ev in ("REJECT", "VETO", "BUY_FAIL", "SELL_FAIL"):
            for label in _reject_labels(reason):
                rejects[label] = rejects.get(label, 0) + 1
        if ev == "RESEARCH":
            head = (reason or "").split("|")[0].strip().lower() or "unknown"
            research_verdicts[head] = research_verdicts.get(head, 0) + 1
            if (r.get("mint") or "").strip():
                research_by_mint[(r.get("mint") or "").strip()] = head
        elif ev == "SIGNAL":
            signal_count += 1
        elif ev == "AWAIT_CONFIRM":
            await_count += 1
        elif ev == "BUY":
            buy_count += 1
            fees_usd += fnum_row(r.get("fee_usd"))
            if pos_id:
                buy_pos.append(pos_id)
                score_by_pos[pos_id] = fnum_row(r.get("score"))
                news_by_pos[pos_id] = research_by_mint.get(
                    (r.get("mint") or "").strip(), "no-research")
                symbol_by_pos[pos_id] = (r.get("symbol") or "").strip()
        elif ev in ("SELL", "EMERGENCY"):
            fees_usd += fnum_row(r.get("fee_usd"))
            if pos_id:
                partial[pos_id] = partial.get(pos_id, 0.0) + fnum_row(r.get("pnl_usd"))
        elif ev == "CLOSE":
            if pos_id:
                close_pnl[pos_id] = close_pnl.get(pos_id, 0.0) + fnum_row(r.get("pnl_usd"))
                symbol_by_pos.setdefault(pos_id, (r.get("symbol") or "").strip())

    # A trade's result is the CLOSE row; if the process died before writing one,
    # fall back to the sum of its sells so nothing vanishes from the stats.
    trade_pnl: Dict[str, float] = dict(close_pnl)
    for pos_id, total in partial.items():
        if pos_id not in trade_pnl:
            trade_pnl[pos_id] = total
    for pos_id in buy_pos:
        trade_pnl.setdefault(pos_id, 0.0)
    closed = [p for p in trade_pnl if p in close_pnl or p in partial]
    wins = [p for p in closed if trade_pnl[p] > 0]
    losses = [p for p in closed if trade_pnl[p] <= 0]
    realized = sum(trade_pnl.values())
    open_trades = [p for p in buy_pos if p not in closed]

    # Signal -> trade conversion (the number that tells you if the filters are too tight)
    buys_per_signal = (buy_count / signal_count) if signal_count else 0.0
    # Momentum score vs outcome: does a higher score actually mean more money?
    buckets: Dict[str, List[float]] = {}
    for pos_id in closed:
        buckets.setdefault(_score_bucket(score_by_pos.get(pos_id, 0.0)), []).append(trade_pnl[pos_id])

    gross_win = sum(v for v in trade_pnl.values() if v > 0)
    gross_loss = -sum(v for v in trade_pnl.values() if v < 0)
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win else 0.0)
    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss = (-gross_loss / len(losses)) if losses else 0.0
    expectancy = (realized / len(closed)) if closed else 0.0
    win_rate = (len(wins) / len(closed)) if closed else 0.0

    lines = []
    lines.append("=" * 78)
    lines.append(" SIGNAL QUALITY REPORT   {}".format(path))
    lines.append("=" * 78)
    lines.append(" ledger rows              : {:,}".format(len(rows)))
    lines.append(" first looks held         : {:,}".format(await_count))
    lines.append(" confirmed signals        : {:,}".format(signal_count))
    lines.append(" buys executed            : {:,}   ({:.1f}% of signals)".format(
        buy_count, buys_per_signal * 100))
    lines.append(" positions still open     : {:,}".format(len(open_trades)))
    lines.append(" closed trades            : {:,}".format(len(closed)))
    lines.append("")
    lines.append(" MONEY (paper or live, as recorded)")
    lines.append("   realized pnl           : {:>+10.2f} USD{}".format(
        realized, "   ({:+.1f}% of capital)".format(realized / capital_usd * 100)
        if capital_usd > 0 else ""))
    lines.append("   fees paid              : {:>10.2f} USD".format(fees_usd))
    lines.append("   win rate               : {:>10.1f}%  ({}W / {}L)".format(
        win_rate * 100, len(wins), len(losses)))
    lines.append("   avg win / avg loss     : {:>+10.2f} / {:+.2f} USD".format(avg_win, avg_loss))
    lines.append("   profit factor          : {:>10.2f}   (>1.2 is the minimum to trust)".format(
        profit_factor))
    lines.append("   expectancy per trade   : {:>+10.3f} USD".format(expectancy))
    if closed:
        lines.append("   best / worst trade     : {:>+10.2f} / {:+.2f} USD".format(
            max(trade_pnl.values()), min(trade_pnl.values())))
    lines.append("")
    lines.append(" WHAT BLOCKED ENTRIES  (top reasons, counted per filter)")
    if rejects:
        total_rejects = sum(rejects.values())
        for label, n in sorted(rejects.items(), key=lambda kv: -kv[1])[:15]:
            lines.append("   {:<24} {:>6}  {:>5.1f}%".format(
                label[:24], n, n / total_rejects * 100))
    else:
        lines.append("   nothing yet - no rejections logged")
    lines.append("")
    if research_verdicts:
        lines.append(" NEWS GATE (Obscura web research)")
        lines.append("   verdicts seen          : " + ", ".join(
            "{} {}".format(k, v) for k, v in
            sorted(research_verdicts.items(), key=lambda kv: -kv[1])))
        by_news: Dict[str, List[float]] = {}
        for pos_id in closed:
            by_news.setdefault(news_by_pos.get(pos_id, "no-research"), []).append(
                trade_pnl[pos_id])
        for tag in sorted(by_news, key=lambda t: -len(by_news[t])):
            vals = by_news[tag]
            lines.append("   pnl at {:<13} : {:>3} trades  {:>+8.2f} USD".format(
                tag, len(vals), sum(vals)))
        lines.append("   (news verdict recorded before each buy; 'no-research' means the")
        lines.append("    browser was off, not that there was no news)")
        lines.append("")
    lines.append(" DOES A HIGHER SCORE MAKE MORE MONEY?")
    if buckets:
        for label in ("70-79", "80-89", "90-100", "unknown"):
            if label not in buckets:
                continue
            vals = buckets[label]
            w = sum(1 for v in vals if v > 0)
            lines.append("   score {:<8} {:>3} trades  {:>3.0f}% wins  {:>+8.2f} USD".format(
                label, len(vals), (w / len(vals) * 100) if vals else 0.0, sum(vals)))
    else:
        lines.append("   no closed trades yet")
    lines.append("")

    # ---- verdict: deliberately pessimistic, because optimism is expensive here
    if len(closed) < 20:
        verdict = ("NOT ENOUGH DATA. {} closed trade(s). Below ~20 trades the numbers are "
                   "noise; below ~100 they are still a coin flip. Keep it in paper mode."
                   .format(len(closed)))
    elif realized <= 0:
        verdict = ("NEGATIVE. The rules lost {:.2f} USD over {} trades (profit factor {:.2f}). "
                   "Do not run this with real money - loosen nothing, tighten the losing "
                   "filter, or stop.".format(realized, len(closed), profit_factor))
    elif profit_factor < 1.2:
        verdict = ("MARGINAL. +{:.2f} USD over {} trades, profit factor {:.2f}. One bad "
                   "trade erases it. Not a signal you can size up."
                   .format(realized, len(closed), profit_factor))
    elif len(losses) and avg_win < abs(avg_loss) * 1.2:
        verdict = ("FRAGILE. Positive, but wins and losses are too similar ({:+.2f} vs {:+.2f}); "
                   "the edge is fees and luck away from zero.".format(avg_win, avg_loss))
    else:
        verdict = ("POSITIVE so far: {:+.2f} USD over {} trades, profit factor {:.2f}. "
                   "That is evidence, not proof - forward results drift and every strategy "
                   "here has a bad week.".format(realized, len(closed), profit_factor))
    lines.append(" VERDICT")
    lines.append("   " + verdict)
    lines.append("")
    lines.append(" Honest reminder: there is no such thing as a sure signal in memecoins.")
    lines.append(" This report measures whether these filters paid, not whether they will.")
    lines.append("=" * 78)

    stats = {
        "ok": True, "rows": len(rows), "signals": signal_count, "awaits": await_count,
        "buys": buy_count, "closed": len(closed), "open": len(open_trades),
        "wins": len(wins), "losses": len(losses), "win_rate": win_rate,
        "realized": realized, "fees": fees_usd, "profit_factor": profit_factor,
        "rejects": rejects, "buckets": {k: (len(v), sum(v)) for k, v in buckets.items()},
        "research": research_verdicts, "news_by_pos": news_by_pos,
        "verdict": verdict,
    }
    return "\n".join(lines), stats



# =============================================================================
# SECTION 16c.  RESEARCH CHECK  (--research-check SYMBOL)
#
# Proves the browser skill works before it is allowed to influence a trade.
# Prints every source, its status, what it said about the token, and the final
# verdict. No wallet, no orders, no state file changes.
# =============================================================================

def research_check(cfg: Config, symbol: str, mint: str = "") -> int:
    client = Obscura(cfg)
    print("=" * 78)
    print(" RESEARCH CHECK  token={}  mint={}".format(symbol, mint or "(not given)"))
    print("=" * 78)
    print(" binary   : {}".format(client.bin or "NOT FOUND"))
    print(" lookup   : {}".format("$%s solana" % symbol))
    print(" sources  : {}".format(", ".join(src["name"] for src in
                                            research_sources(cfg, symbol, mint))))
    print(" stealth  : {}   cookies: {}   obey_robots: {}".format(
        cfg.research_stealth, client.storage_dir() or "no", cfg.research_obey_robots))
    if not cfg.research_enabled:
        print(" NOTE     : research is off in this config (--no-research); the bot will")
        print("            not use research. This check runs anyway because you asked.")
    if not client.bin:
        print("")
        print(" OBSCURA IS NOT INSTALLED. Nothing was fetched.")
        print(" Linux/macOS/PC:  https://github.com/h4ckf0r0day/obscura/releases/latest")
        print(" Termux/Android:  official builds need glibc 2.35+, so run it inside")
        print("                  proot-distro (see README section 1b) and set")
        print("                  OBSCURA_BIN=/path/to/your/wrapper")
        print(" The bot still runs without it: research becomes 'unavailable' and")
        print(" never blocks or boosts a signal.")
        return 2

    async def run() -> Tuple[Dict[str, Dict[str, Any]], ResearchResult]:
        sources = research_sources(cfg, symbol, mint)
        pages = await client.fetch_many(sources)
        per: Dict[str, Dict[str, Any]] = {}
        for src in sources:
            got = pages.get(src["name"]) or FetchResult(error="no result")
            status = ("blocked" if got.blocked else
                      ("ok" if got.ok else "failed"))
            print("  {:<16} {:<8} {:>6.0f}ms  {}".format(
                src["name"], status, got.ms, (got.error or "")[:60]))
            if got.ok:
                m = research_mentions(got.text, symbol, mint).as_dict()
                if src.get("own"):
                    m["mentions"] = 0            # own page: warnings only
                    m["hype_hits"] = 0
                    m["samples"] = []
                print("  {:<16} {} mentions, {} scam words, {} hype words{}".format(
                    "", m["mentions"], m["scam_hits"], m["hype_hits"],
                    "  (own page: chatter not counted)" if src.get("own") else ""))
                for sample in (m["scam_samples"] or m["samples"])[:2]:
                    print("      > {}".format(sample[:140]))
                per[src["name"]] = dict(m, ok=True)
            else:
                per[src["name"]] = {"ok": False, "blocked": got.blocked,
                                    "error": got.error}
        return per, research_verdict(per, cfg)

    per, result = asyncio.run(run())
    print("-" * 78)
    print(" VERDICT  : {}".format(result.line()))
    if result.verdict == "veto":
        print("            A live buy of this token would now be REJECTED.")
    elif result.verdict == "boost":
        print("            A live buy would get {:+.1f} momentum points (max).".format(
            result.delta))
    elif result.verdict == "neutral":
        print("            Nothing conclusive. The trade decision is unchanged.")
    else:
        print("            No usable answer. With --research-required this blocks")
        print("            every trade; without it, price rules decide alone.")
    if result.notes:
        print("")
        print(" WHY:")
        for note in result.notes[:4]:
            print("   - {}".format(note[:150]))
    print("")
    print(" Reminder: this is keyword matching on public pages, not truth. It can")
    print(" miss a real rug and it can flag a joke about a rug.")
    print("=" * 78)
    return 0


# =============================================================================
# SECTION 17.  ENTRY POINT
# =============================================================================

def main(argv: Optional[Sequence[str]] = None) -> int:
    cfg, args = build_config(argv)
    if args.selftest:
        return run_selftest()
    if args.scan is not None:
        return asyncio.run(async_scan(cfg, limit=cfg.scan_max_candidates,
                                      as_json=args.json))
    if args.watch:
        try:
            return asyncio.run(async_watch(cfg, iterations=args.iterations,
                                           limit=cfg.scan_max_candidates,
                                           quiet=args.json, as_json=args.json))
        except KeyboardInterrupt:
            print("\nInterrupted.")
            return 130
    if args.research_check is not None:
        return research_check(cfg, args.research_check.strip(), args.research_mint.strip())
    if args.analyze is not None:
        path = cfg.ledger_csv if args.analyze == "__auto__" else args.analyze
        text, _stats = analyze_ledger(path, cfg.start_capital_usd)
        print(text)
        return 0
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
