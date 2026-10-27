"""
memeterm.agents - 100 logical research agents in ten teams.

Why "logical"? On a phone, 100 OS processes would thrash the CPU and the RAM.
Each agent here is a pure function over one immutable market snapshot, executed
through a bounded worker pool with a per-agent deadline. Agents never call the
network directly: the market service refreshes a snapshot, the agents read it,
so 100 "agents" cost one set of HTTP requests.

Every finding carries: source, timestamp, confidence, missing data and reason
codes. Security and data-integrity vetoes outrank any bullish score.

The brief asked for teams of 10/10/10/10/10/10/10/10/5/5 - which sums to 90.
To land on exactly 100 the extra ten were split 5/5 between the portfolio and
backtest teams, and that is asserted in the tests.

Agents produce *scores and vetoes*, never orders. A bullish consensus cannot
place a trade on its own; the risk engine still has the last word.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .util import percentile, safe_float, stdev, utc_str

# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------
@dataclass
class Finding:
    agent_id: str
    team: str
    role: str
    vote: str = "neutral"            # bullish | bearish | neutral | veto
    score: float = 0.0               # -1 .. +1
    confidence: float = 0.0          # 0 .. 1
    reason_codes: List[str] = field(default_factory=list)
    evidence: Dict[str, Any] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)
    source: str = "binance:spot"
    timestamp: float = 0.0
    state: str = "ok"                # ok | no_data | error | timeout
    error: str = ""
    latency_ms: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id, "team": self.team, "role": self.role,
            "vote": self.vote, "score": round(self.score, 4),
            "confidence": round(self.confidence, 3),
            "reason_codes": self.reason_codes, "evidence": self.evidence,
            "missing": self.missing, "source": self.source,
            "timestamp": self.timestamp, "timestamp_utc": utc_str(self.timestamp),
            "state": self.state, "error": self.error,
            "latency_ms": round(self.latency_ms, 2),
        }


@dataclass
class MarketContext:
    symbol: str = ""
    candles: List[Dict[str, float]] = field(default_factory=list)
    ticker: Dict[str, Any] = field(default_factory=dict)
    book: Dict[str, Any] = field(default_factory=dict)
    trades: List[Dict[str, Any]] = field(default_factory=list)
    filters: Any = None
    cross: Dict[str, List[Dict[str, float]]] = field(default_factory=dict)
    portfolio: Dict[str, Any] = field(default_factory=dict)
    backtest: Dict[str, Any] = field(default_factory=dict)
    news: Dict[str, Any] = field(default_factory=dict)
    data_age_s: float = 0.0
    now: float = field(default_factory=time.time)
    cache: Dict[str, Any] = field(default_factory=dict)
    quote_asset: str = "USDT"

    # -- derived series (memoised) -------------------------------------
    def closes(self) -> List[float]:
        if "closes" not in self.cache:
            self.cache["closes"] = [c["c"] for c in self.candles]
        return self.cache["closes"]

    def opens(self) -> List[float]:
        return [c["o"] for c in self.candles]

    def highs(self) -> List[float]:
        return [c["h"] for c in self.candles]

    def lows(self) -> List[float]:
        return [c["l"] for c in self.candles]

    def volumes(self) -> List[float]:
        return [c["v"] for c in self.candles]

    def quote_volumes(self) -> List[float]:
        return [c["qv"] for c in self.candles]

    def last_price(self) -> float:
        if self.candles:
            return self.candles[-1]["c"]
        return safe_float(self.ticker.get("lastPrice"))

    def change_pct(self) -> float:
        return safe_float(self.ticker.get("priceChangePercent"))


def _ema(values: Sequence[float], period: int) -> float:
    if not values:
        return 0.0
    k = 2.0 / (period + 1.0)
    out = values[0]
    for value in values[1:]:
        out = value * k + out * (1 - k)
    return out


def _rsi(values: Sequence[float], period: int = 14) -> float:
    if len(values) <= period:
        return 50.0
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        delta = values[i] - values[i - 1]
        gains += max(0.0, delta)
        losses += max(0.0, -delta)
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100.0 - 100.0 / (1.0 + rs)


def _slope(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(values) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, values)) / den


def _corr(a: Sequence[float], b: Sequence[float]) -> float:
    n = min(len(a), len(b))
    if n < 8:
        return 0.0
    a, b = list(a[-n:]), list(b[-n:])
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = math.sqrt(sum((x - ma) ** 2 for x in a))
    vb = math.sqrt(sum((y - mb) ** 2 for y in b))
    if va == 0 or vb == 0:
        return 0.0
    return cov / (va * vb)


def _returns(closes: Sequence[float]) -> List[float]:
    return [(closes[i] / closes[i - 1] - 1.0) for i in range(1, len(closes))
            if closes[i - 1]]


# ---------------------------------------------------------------------------
# kernel helpers
# ---------------------------------------------------------------------------
def _finding(score: float, confidence: float, codes: List[str],
             evidence: Optional[Dict[str, Any]] = None,
             missing: Optional[List[str]] = None, vote: Optional[str] = None) -> Dict[str, Any]:
    score = max(-1.0, min(1.0, float(score)))
    vote = vote or ("bullish" if score > 0.2 else "bearish" if score < -0.2 else "neutral")
    return {"score": score, "confidence": max(0.0, min(1.0, confidence)),
            "reason_codes": codes, "evidence": evidence or {},
            "missing": missing or [], "vote": vote}


def _needs(ctx: MarketContext, count: int, codes: List[str]) -> Optional[Dict[str, Any]]:
    if len(ctx.candles) < count:
        return _finding(0.0, 0.0, codes, {"candles": len(ctx.candles)},
                        missing=["candles"], vote="neutral")
    return None


# ---------------------------------------------------------------------------
# kernels: discovery (10 roles / 6 kernels)
# ---------------------------------------------------------------------------
def k_universe(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    volume = safe_float(ctx.ticker.get("quoteVolume"))
    floor = safe_float(p.get("min_quote_volume"), 5_000_000)
    ok = volume >= floor
    return _finding(0.35 if ok else -0.25, 0.8 if volume else 0.2,
                    ["UNIVERSE_TRADEABLE" if ok else "UNIVERSE_THIN"],
                    {"quote_volume_24h": volume, "floor": floor})


def k_movers(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    change = ctx.change_pct()
    lo, hi = safe_float(p.get("lo"), -25), safe_float(p.get("hi"), 25)
    if change > hi:
        return _finding(-0.3, 0.7, ["MOVER_OVEREXTENDED"], {"change_pct": change})
    if change < lo:
        return _finding(-0.4, 0.7, ["MOVER_CAPITULATION"], {"change_pct": change})
    return _finding(0.2, 0.6, ["MOVER_IN_RANGE"], {"change_pct": change})


def k_volume_surge(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    vols = ctx.quote_volumes()
    look = int(p.get("look", 20))
    if len(vols) < look + 1:
        return _finding(0, 0, ["VOLUME_SURGE_NO_DATA"], {"candles": len(vols)},
                        missing=["quote_volume"])
    baseline = percentile(vols[-look - 1:-1], 0.5) or 1e-9
    ratio = vols[-1] / baseline
    score = min(1.0, max(-1.0, (ratio - 1.0) / 2.0))
    return _finding(score, 0.75, ["VOLUME_SURGE" if ratio >= 1.5 else "VOLUME_NORMAL"],
                    {"ratio": round(ratio, 3)})


def k_trend_filter(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    closes = ctx.closes()
    period = int(p.get("period", 50))
    if len(closes) < period:
        return _finding(0, 0, ["TREND_NO_DATA"], missing=["candles"])
    ema = _ema(closes[-period * 2:], period)
    price = closes[-1]
    dev = (price - ema) / ema if ema else 0.0
    return _finding(max(-1, min(1, dev * 40)), 0.7,
                    ["TREND_UP" if dev > 0 else "TREND_DOWN"],
                    {"ema": round(ema, 8), "deviation": round(dev, 5)})


def k_sector_proxy(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    peers = ctx.cross.get("peers") or []
    if not peers:
        return _finding(0, 0, ["PEERS_MISSING"], missing=["peers"], vote="neutral")
    changes = [safe_float(pc) for pc in peers]
    avg = sum(changes) / len(changes) if changes else 0.0
    return _finding(max(-1, min(1, avg / 10.0)), 0.5, ["PEER_STRENGTH"],
                    {"peer_avg_change_pct": round(avg, 2), "peers": len(changes)})


def k_venue_health(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    status = str(getattr(ctx.filters, "status", "") or "")
    ok = status == "TRADING"
    return _finding(0.5 if ok else -0.8, 0.9,
                    ["VENUE_OK" if ok else "VENUE_NOT_TRADING"], {"status": status},
                    vote="bullish" if ok else "veto")


# ---------------------------------------------------------------------------
# kernels: data validation
# ---------------------------------------------------------------------------
def k_freshness(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    limit = safe_float(p.get("limit_s"), 90)
    age = ctx.data_age_s
    ok = age <= limit
    return _finding(0.3 if ok else -0.9, 0.95,
                    ["DATA_FRESH" if ok else "DATA_STALE"],
                    {"age_s": round(age, 1), "limit_s": limit},
                    vote="bullish" if ok else "veto")


def k_capture_integrity(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    times = [int(c["ts"]) for c in ctx.candles]
    if len(times) < 3:
        return _finding(0, 0, ["KLINE_NO_DATA"], missing=["candles"], vote="neutral")
    if len(set(times)) != len(times):
        return _finding(-0.8, 0.9, ["KLINE_DUPLICATE_TS"], {"n": len(times)}, vote="veto")
    step = times[-1] - times[-2]
    gaps = sum(1 for i in range(1, len(times)) if times[i] - times[i - 1] > step * 3)
    if gaps:
        return _finding(-0.6, 0.85, ["KLINE_GAPS"], {"gaps": gaps}, vote="veto")
    return _finding(0.3, 0.9, ["KLINE_CONTIGUOUS"], {"candles": len(times)})


def k_ohlc_consistency(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    bad = 0
    for c in ctx.candles[-100:]:
        if c["h"] < max(c["o"], c["c"]) - 1e-12 or c["l"] > min(c["o"], c["c"]) + 1e-12:
            bad += 1
    if bad:
        return _finding(-0.8, 0.9, ["OHLC_IMPOSSIBLE"], {"bad": bad}, vote="veto")
    return _finding(0.2, 0.8, ["OHLC_VALID"], {"checked": min(100, len(ctx.candles))})


def k_zero_volume(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    vols = ctx.volumes()[-60:]
    if not vols:
        return _finding(0, 0, ["VOLUME_NO_DATA"], missing=["volume"])
    zeros = sum(1 for v in vols if v <= 0)
    ratio = zeros / len(vols)
    return _finding(-0.5 if ratio > 0.3 else 0.2, 0.7,
                    ["VOLUME_DEAD_BARS" if ratio > 0.3 else "VOLUME_PRESENT"],
                    {"zero_ratio": round(ratio, 3)})


def k_outlier(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    closes = ctx.closes()[-120:]
    if len(closes) < 20:
        return _finding(0, 0, ["OUTLIER_NO_DATA"], missing=["candles"])
    rets = _returns(closes)
    sigma = stdev(rets)
    last = rets[-1] if rets else 0.0
    z = abs(last) / sigma if sigma else 0.0
    return _finding(-0.4 if z > 6 else 0.1, 0.7,
                    ["PRICE_OUTLIER" if z > 6 else "PRICE_NORMAL"],
                    {"z": round(z, 2), "sigma": round(sigma, 6)})


def k_book_sanity(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    bids = ctx.book.get("bids") or []
    asks = ctx.book.get("asks") or []
    if not bids or not asks:
        return _finding(0, 0, ["BOOK_MISSING"], missing=["order_book"])
    best_bid = safe_float(bids[0][0])
    best_ask = safe_float(asks[0][0])
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        return _finding(-0.9, 0.95, ["BOOK_CROSSED"], vote="veto",
                        evidence={"bid": best_bid, "ask": best_ask})
    spread_bps = (best_ask - best_bid) / best_bid * 10_000
    return _finding(-0.4 if spread_bps > 100 else 0.2, 0.8,
                    ["BOOK_WIDE" if spread_bps > 100 else "BOOK_OK"],
                    {"spread_bps": round(spread_bps, 2)})


def k_trade_tape(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    if not ctx.trades:
        return _finding(0, 0, ["TAPE_MISSING"], missing=["recent_trades"])
    stamps = [safe_float(t.get("time")) for t in ctx.trades]
    if any(stamps[i] < stamps[i - 1] for i in range(1, len(stamps))):
        return _finding(-0.3, 0.6, ["TAPE_OUT_OF_ORDER"], {"n": len(stamps)})
    return _finding(0.2, 0.7, ["TAPE_OK"], {"n": len(stamps)})


def k_source_agreement(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    kline_price = ctx.last_price()
    ticker_price = safe_float(ctx.ticker.get("lastPrice"))
    if not kline_price or not ticker_price:
        return _finding(0, 0, ["AGREEMENT_NO_DATA"], missing=["last_price"])
    drift = abs(kline_price - ticker_price) / ticker_price
    if drift > 0.02:
        return _finding(-0.7, 0.85, ["SOURCES_DISAGREE"],
                        {"drift_pct": round(drift * 100, 3)}, vote="veto")
    return _finding(0.25, 0.8, ["SOURCES_AGREE"], {"drift_pct": round(drift * 100, 3)})


def k_quote_asset(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    quote = str(getattr(ctx.filters, "quote_asset", "") or "")
    ok = quote == ctx.quote_asset
    return _finding(0.3 if ok else -0.8, 0.9,
                    ["QUOTE_OK" if ok else "QUOTE_UNSUPPORTED"], {"quote": quote},
                    vote="bullish" if ok else "veto")


# ---------------------------------------------------------------------------
# kernels: momentum
# ---------------------------------------------------------------------------
def k_ema_cross(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    fast, slow = int(p.get("fast", 9)), int(p.get("slow", 21))
    closes = ctx.closes()
    if len(closes) < slow + 2:
        return _needs(ctx, slow + 2, ["EMA_NO_DATA"])
    f, s = _ema(closes[-(slow * 3):], fast), _ema(closes[-(slow * 3):], slow)
    gap = (f - s) / s if s else 0.0
    return _finding(max(-1, min(1, gap * 120)), 0.75,
                    ["EMA_BULL_CROSS" if gap > 0 else "EMA_BEAR_CROSS"],
                    {"fast": round(f, 8), "slow": round(s, 8), "gap_pct": round(gap * 100, 3)})


def k_rsi(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    period = int(p.get("period", 14))
    closes = ctx.closes()
    if len(closes) < period + 5:
        return _needs(ctx, period + 5, ["RSI_NO_DATA"])
    value = _rsi(closes[-period * 3:], period)
    if value >= 78:
        return _finding(-0.55, 0.7, ["RSI_OVERBOUGHT"], {"rsi": round(value, 1)})
    if value <= 25:
        return _finding(0.25, 0.5, ["RSI_OVERSOLD"], {"rsi": round(value, 1)})
    score = (value - 50) / 50 * 0.6
    return _finding(score, 0.6, ["RSI_MID"], {"rsi": round(value, 1)})


def _ema_series(values: Sequence[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    out = [values[0]]
    for value in values[1:]:
        out.append(value * k + out[-1] * (1 - k))
    return out


def k_macd(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    closes = ctx.closes()
    if len(closes) < 40:
        return _needs(ctx, 40, ["MACD_NO_DATA"])
    window = closes[-120:]
    fast = _ema_series(window, 12)
    slow = _ema_series(window, 26)
    macd_series = [a - b for a, b in zip(fast, slow)]
    signal_series = _ema_series(macd_series, 9)
    hist = macd_series[-1] - signal_series[-1]
    scale = (sum(abs(x) for x in macd_series[-20:]) / 20.0) or 1e-12
    return _finding(max(-1, min(1, hist / scale)), 0.6,
                    ["MACD_POSITIVE" if hist > 0 else "MACD_NEGATIVE"],
                    {"macd": round(macd_series[-1], 10), "hist": round(hist, 10)})


def k_breakout(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    look = int(p.get("look", 20))
    closes, highs = ctx.closes(), ctx.highs()
    if len(closes) < look + 1:
        return _needs(ctx, look + 1, ["BREAKOUT_NO_DATA"])
    prior_high = max(highs[-look - 1:-1])
    price = closes[-1]
    if prior_high <= 0:
        return _finding(0, 0, ["BREAKOUT_NO_DATA"], missing=["highs"])
    ratio = price / prior_high
    return _finding(max(-1, min(1, (ratio - 1) * 25)), 0.7,
                    ["BREAKOUT_NEW_HIGH" if ratio > 1 else "BREAKDOWN"],
                    {"ratio": round(ratio, 5)})


def k_volume_z(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    vols = ctx.quote_volumes()[-60:]
    if len(vols) < 12:
        return _needs(ctx, 12, ["VOL_Z_NO_DATA"])
    mu, sd = sum(vols[:-1]) / max(1, len(vols) - 1), stdev(vols[:-1])
    z = (vols[-1] - mu) / sd if sd else 0.0
    return _finding(max(-1, min(1, z / 3)), 0.65, ["VOLUME_Z"],
                    {"z": round(z, 2)})


def k_volume_trend(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    vols = ctx.quote_volumes()[-30:]
    if len(vols) < 10:
        return _needs(ctx, 10, ["VOL_TREND_NO_DATA"])
    slope = _slope(vols) / (sum(vols) / len(vols) + 1e-9)
    return _finding(max(-1, min(1, slope * 3)), 0.6,
                    ["VOLUME_RISING" if slope > 0 else "VOLUME_FALLING"],
                    {"slope": round(slope, 4)})


def k_buy_pressure(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    bars = ctx.candles[-20:]
    if not bars:
        return _needs(ctx, 1, ["TAKER_NO_DATA"])
    taker = sum(c.get("tb", 0.0) for c in bars)
    total = sum(c.get("v", 0.0) for c in bars)
    if total <= 0:
        return _finding(0, 0.2, ["TAKER_MISSING"], missing=["taker_buy_base"])
    ratio = taker / total
    return _finding(max(-1, min(1, (ratio - 0.5) * 4)), 0.7,
                    ["BUY_PRESSURE" if ratio > 0.55 else
                     "SELL_PRESSURE" if ratio < 0.45 else "BALANCED"],
                    {"taker_ratio": round(ratio, 3)})


def k_vwap(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    bars = ctx.candles[-30:]
    qv = sum(c.get("qv", 0.0) for c in bars)
    v = sum(c.get("v", 0.0) for c in bars)
    if qv <= 0 or v <= 0:
        return _finding(0, 0.2, ["VWAP_MISSING"], missing=["quote_volume"])
    vwap = qv / v
    price = ctx.last_price()
    dev = (price - vwap) / vwap if vwap else 0.0
    return _finding(max(-1, min(1, dev * 30)), 0.6, ["VWAP_DEVIATION"],
                    {"vwap": round(vwap, 8), "dev_pct": round(dev * 100, 3)})


def k_relative_strength(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    btc = ctx.cross.get("BTCUSDT") or []
    if len(btc) < 20 or len(ctx.candles) < 20:
        return _finding(0, 0, ["RS_NO_DATA"], missing=["cross_market"])
    look = min(20, len(btc), len(ctx.candles))
    own = ctx.closes()[-1] / ctx.closes()[-look] - 1
    ref = btc[-1]["c"] / btc[-look]["c"] - 1
    diff = own - ref
    return _finding(max(-1, min(1, diff * 20)), 0.65, ["RELATIVE_STRENGTH"],
                    {"own_pct": round(own * 100, 2), "btc_pct": round(ref * 100, 2)})


# ---------------------------------------------------------------------------
# kernels: liquidity
# ---------------------------------------------------------------------------
def k_spread(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    bids, asks = ctx.book.get("bids") or [], ctx.book.get("asks") or []
    if not bids or not asks:
        return _finding(0, 0, ["SPREAD_MISSING"], missing=["order_book"])
    bid, ask = safe_float(bids[0][0]), safe_float(asks[0][0])
    if bid <= 0 or ask <= 0:
        return _finding(0, 0, ["SPREAD_MISSING"], missing=["order_book"])
    spread_bps = (ask - bid) / bid * 10_000
    limit = safe_float(p.get("max_bps"), 25)
    return _finding(0.35 if spread_bps <= limit else -0.6,
                    0.85, ["SPREAD_OK" if spread_bps <= limit else "SPREAD_WIDE"],
                    {"spread_bps": round(spread_bps, 2), "limit": limit})


def k_book_imbalance(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    depth = int(p.get("depth", 10))
    bids, asks = (ctx.book.get("bids") or [])[:depth], (ctx.book.get("asks") or [])[:depth]
    if not bids or not asks:
        return _finding(0, 0, ["IMBALANCE_MISSING"], missing=["order_book"])
    bid_vol = sum(safe_float(b[1]) for b in bids)
    ask_vol = sum(safe_float(a[1]) for a in asks)
    if bid_vol + ask_vol <= 0:
        return _finding(0, 0.2, ["IMBALANCE_MISSING"], missing=["order_book"])
    imb = (bid_vol - ask_vol) / (bid_vol + ask_vol)
    return _finding(max(-1, min(1, imb * 2)), 0.6, ["BOOK_IMBALANCE"],
                    {"imbalance": round(imb, 3), "bid_vol": round(bid_vol, 4),
                     "ask_vol": round(ask_vol, 4)})


def k_depth_value(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    side = str(p.get("side", "bids"))
    depth = int(p.get("depth", 20))
    levels = (ctx.book.get(side) or [])[:depth]
    if not levels:
        return _finding(0, 0, ["DEPTH_MISSING"], missing=["order_book"])
    value = sum(safe_float(level[0]) * safe_float(level[1]) for level in levels)
    floor = safe_float(p.get("min_usdt"), 5_000)
    return _finding(0.3 if value >= floor else -0.45, 0.7,
                    ["DEPTH_OK" if value >= floor else "DEPTH_THIN"],
                    {"side": side, "value_usdt": round(value, 2), "floor": floor})


def k_slippage(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    notional = safe_float(p.get("notional_usdt"), 10.0)
    asks = ctx.book.get("asks") or []
    if not asks:
        return _finding(0, 0, ["SLIPPAGE_MISSING"], missing=["order_book"])
    best = safe_float(asks[0][0])
    remaining, base_taken = notional, 0.0
    for level in asks:
        price, qty = safe_float(level[0]), safe_float(level[1])
        if price <= 0 or qty <= 0:
            continue
        level_value = price * qty
        take = min(remaining, level_value)
        base_taken += take / price
        remaining -= take
        if remaining <= 0:
            break
    if base_taken <= 0 or best <= 0:
        return _finding(0, 0.3, ["SLIPPAGE_NO_DEPTH"], missing=["order_book"])
    filled = notional - remaining
    avg = filled / base_taken if base_taken else best
    slip_bps = (avg / best - 1) * 10_000 if remaining <= 0 else 999.0
    return _finding(0.3 if slip_bps <= 30 else -0.6, 0.75,
                    ["SLIPPAGE_OK" if slip_bps <= 30 else "SLIPPAGE_HIGH"],
                    {"slippage_bps": round(slip_bps, 2), "notional": notional})


def k_quote_volume_24h(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    volume = safe_float(ctx.ticker.get("quoteVolume"))
    floor = safe_float(p.get("min_usdt"), 20_000_000)
    return _finding(0.3 if volume >= floor else -0.3, 0.7,
                    ["LIQUIDITY_OK" if volume >= floor else "LIQUIDITY_THIN"],
                    {"quote_volume_24h": volume, "floor": floor})


def k_trade_rate(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    if not ctx.trades:
        return _finding(0, 0, ["TAPE_MISSING"], missing=["recent_trades"])
    stamps = [safe_float(t.get("time")) for t in ctx.trades]
    span = (max(stamps) - min(stamps)) / 1000.0 if stamps else 0.0
    rate = len(stamps) / span if span > 0 else 0.0
    floor = safe_float(p.get("min_per_s"), 0.05)
    return _finding(0.3 if rate >= floor else -0.35, 0.65,
                    ["TRADE_FLOW_OK" if rate >= floor else "TRADE_FLOW_THIN"],
                    {"trades_per_s": round(rate, 3), "floor": floor})


def k_trade_size(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    if not ctx.trades:
        return _finding(0, 0, ["TAPE_MISSING"], missing=["recent_trades"])
    notional = [safe_float(t.get("quoteQty")) for t in ctx.trades]
    median = percentile(notional, 0.5)
    floor = safe_float(p.get("min_usdt"), 20)
    return _finding(0.2 if median >= floor else -0.3, 0.6,
                    ["TRADE_SIZE_OK" if median >= floor else "TRADE_SIZE_TINY"],
                    {"median_notional": round(median, 2)})


def k_trade_skew(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    if not ctx.trades:
        return _finding(0, 0, ["TAPE_MISSING"], missing=["recent_trades"])
    buy = sum(safe_float(t.get("quoteQty")) for t in ctx.trades if t.get("isBuyerMaker") is False)
    sell = sum(safe_float(t.get("quoteQty")) for t in ctx.trades if t.get("isBuyerMaker") is True)
    total = buy + sell
    if total <= 0:
        return _finding(0, 0.2, ["TAPE_NO_VALUE"], missing=["recent_trades"])
    skew = (buy - sell) / total
    return _finding(max(-1, min(1, skew * 2)), 0.65, ["TAPE_SKEW"],
                    {"buy": round(buy, 2), "sell": round(sell, 2)})


def k_liquidity_trend(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    vols = ctx.quote_volumes()[-20:]
    if len(vols) < 10:
        return _needs(ctx, 10, ["LIQ_TREND_NO_DATA"])
    slope = _slope(vols) / (sum(vols) / len(vols) + 1e-9)
    return _finding(max(-1, min(1, slope * 2)), 0.55,
                    ["LIQUIDITY_IMPROVING" if slope > 0 else "LIQUIDITY_DETERIORATING"],
                    {"slope": round(slope, 4)})


# ---------------------------------------------------------------------------
# kernels: technical
# ---------------------------------------------------------------------------
def k_bollinger(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    period = int(p.get("period", 20))
    closes = ctx.closes()
    if len(closes) < period:
        return _needs(ctx, period, ["BB_NO_DATA"])
    window = closes[-period:]
    mid = sum(window) / period
    sd = stdev(window)
    if sd <= 0:
        return _finding(0, 0.3, ["BB_FLAT"], {"mid": mid})
    z = (closes[-1] - mid) / sd
    if z > 2.2:
        return _finding(-0.5, 0.65, ["BB_UPPER_EXTREME"], {"z": round(z, 2)})
    if z < -2.2:
        return _finding(0.3, 0.55, ["BB_LOWER_EXTREME"], {"z": round(z, 2)})
    return _finding(0.1, 0.5, ["BB_INSIDE"], {"z": round(z, 2)})


def k_atr(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    period = int(p.get("period", 14))
    if len(ctx.candles) < period + 1:
        return _needs(ctx, period + 1, ["ATR_NO_DATA"])
    trs = []
    for i in range(-period, 0):
        bar, prev = ctx.candles[i], ctx.candles[i - 1]
        trs.append(max(bar["h"] - bar["l"], abs(bar["h"] - prev["c"]),
                       abs(bar["l"] - prev["c"])))
    atr = sum(trs) / len(trs)
    price = ctx.last_price() or 1.0
    atr_pct = atr / price * 100
    return _finding(-0.3 if atr_pct > 6 else 0.2, 0.6,
                    ["VOLATILITY_HIGH" if atr_pct > 6 else "VOLATILITY_OK"],
                    {"atr_pct": round(atr_pct, 3)})


def k_range_position(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    bars = ctx.candles[-int(p.get("look", 60)):]
    if len(bars) < 10:
        return _needs(ctx, 10, ["RANGE_NO_DATA"])
    hi = max(b["h"] for b in bars)
    lo = min(b["l"] for b in bars)
    if hi <= lo:
        return _finding(0, 0.3, ["RANGE_FLAT"])
    pos = (ctx.last_price() - lo) / (hi - lo)
    if pos > 0.95:
        return _finding(0.2, 0.6, ["RANGE_TOP"], {"position": round(pos, 3)},)
    if pos < 0.05:
        return _finding(-0.3, 0.6, ["RANGE_BOTTOM"], {"position": round(pos, 3)})
    return _finding(0.05, 0.5, ["RANGE_MID"], {"position": round(pos, 3)})


def k_trend_slope(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    look = int(p.get("look", 30))
    closes = ctx.closes()[-look:]
    if len(closes) < 8:
        return _needs(ctx, 8, ["SLOPE_NO_DATA"])
    slope = _slope(closes) / (sum(closes) / len(closes) + 1e-12)
    return _finding(max(-1, min(1, slope * 120)), 0.65, ["TREND_SLOPE"],
                    {"slope": round(slope, 6)})


def k_higher_highs(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    highs = ctx.highs()[-20:]
    if len(highs) < 6:
        return _needs(ctx, 6, ["HH_NO_DATA"])
    thirds = [max(highs[i:i + 5]) for i in range(0, 15, 5)]
    rising = all(thirds[i] >= thirds[i - 1] for i in range(1, len(thirds)))
    return _finding(0.4 if rising else -0.15, 0.55,
                    ["HIGHER_HIGHS" if rising else "NO_HIGHER_HIGHS"],
                    {"thirds": [round(x, 8) for x in thirds]})


def k_candle_body(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    if not ctx.candles:
        return _needs(ctx, 1, ["BODY_NO_DATA"])
    bar = ctx.candles[-1]
    rng = bar["h"] - bar["l"]
    if rng <= 0:
        return _finding(0, 0.2, ["BODY_FLAT"])
    body = (bar["c"] - bar["o"]) / rng
    return _finding(max(-1, min(1, body * 1.2)), 0.5, ["CANDLE_BODY"],
                    {"body_ratio": round(body, 3)})


# ---------------------------------------------------------------------------
# kernels: security / anomalies
# ---------------------------------------------------------------------------
def k_symbol_rules(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    if ctx.filters is None:
        return _finding(-0.9, 0.9, ["FILTERS_UNKNOWN"], missing=["exchange_filters"],
                        vote="veto")
    status = getattr(ctx.filters, "status", "UNKNOWN")
    spot = getattr(ctx.filters, "spot_allowed", False)
    if status != "TRADING" or not spot:
        return _finding(-0.9, 0.95, ["SYMBOL_NOT_TRADABLE"],
                        {"status": status, "spot": spot}, vote="veto")
    return _finding(0.3, 0.9, ["SYMBOL_TRADABLE"], {"status": status})


def k_min_notional(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    if ctx.filters is None:
        return _finding(0, 0, ["MIN_NOTIONAL_UNKNOWN"], missing=["exchange_filters"])
    minimum = float(getattr(ctx.filters, "min_notional", 0) or 0)
    max_order = safe_float(ctx.portfolio.get("max_order_usdt"), 10)
    ok = minimum <= max_order
    return _finding(0.3 if ok else -0.8, 0.9,
                    ["NOTIONAL_FITS" if ok else "NOTIONAL_TOO_SMALL"],
                    {"exchange_min": minimum, "our_max": max_order},
                    vote="bullish" if ok else "veto")


def k_wash_trading(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    if len(ctx.trades) < 15:
        return _finding(0, 0, ["WASH_NO_DATA"], missing=["recent_trades"])
    sizes = [round(safe_float(t.get("qty")), 8) for t in ctx.trades]
    repeated = len(sizes) - len(set(sizes))
    ratio = repeated / len(sizes)
    if ratio > 0.6:
        return _finding(-0.75, 0.8, ["WASH_TRADE_SUSPECTED"],
                        {"repeated_ratio": round(ratio, 3)}, vote="veto")
    return _finding(0.1, 0.6, ["TAPE_VARIED"], {"repeated_ratio": round(ratio, 3)})


def k_pump_signature(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    closes = ctx.closes()[-16:]
    if len(closes) < 10:
        return _needs(ctx, 10, ["PUMP_NO_DATA"])
    move = (closes[-1] / closes[0] - 1) * 100
    vols = ctx.quote_volumes()[-16:]
    spike = vols[-1] / (percentile(vols[:-1], 0.5) + 1e-9) if len(vols) > 2 else 1.0
    if move > 18 and spike > 4:
        return _finding(-0.7, 0.8, ["PUMP_SIGNATURE"],
                        {"move_pct": round(move, 2), "volume_x": round(spike, 2)},)
    return _finding(0.15, 0.6, ["NO_PUMP_SIGNATURE"],
                    {"move_pct": round(move, 2), "volume_x": round(spike, 2)})


def k_spread_anomaly(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    bids, asks = ctx.book.get("bids") or [], ctx.book.get("asks") or []
    if not bids or not asks:
        return _finding(0, 0, ["SPREAD_MISSING"], missing=["order_book"])
    bid, ask = safe_float(bids[0][0]), safe_float(asks[0][0])
    if bid <= 0:
        return _finding(0, 0, ["SPREAD_MISSING"], missing=["order_book"])
    bps = (ask - bid) / bid * 10_000
    if bps > 150:
        return _finding(-0.8, 0.9, ["SPREAD_ANOMALY"], {"spread_bps": round(bps, 1)},
                        vote="veto")
    return _finding(0.15, 0.7, ["SPREAD_NORMAL"], {"spread_bps": round(bps, 1)})


def k_listing_age(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    if not ctx.candles:
        return _finding(0, 0, ["AGE_UNKNOWN"], missing=["candles"])
    span_min = (ctx.candles[-1]["ts"] - ctx.candles[0]["ts"]) / 60_000.0
    if span_min < 60:
        return _finding(-0.35, 0.6, ["LISTING_VERY_NEW"], {"span_min": round(span_min, 1)})
    return _finding(0.25, 0.6, ["LISTING_ESTABLISHED"], {"span_min": round(span_min, 1)})


def k_spoof_depth(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    bids, asks = ctx.book.get("bids") or [], ctx.book.get("asks") or []
    if len(bids) < 5 or len(asks) < 5:
        return _finding(0, 0, ["DEPTH_MISSING"], missing=["order_book"])
    top = safe_float(bids[0][1]) + safe_float(asks[0][1])
    rest = sum(safe_float(b[1]) for b in bids[1:5]) + sum(safe_float(a[1]) for a in asks[1:5])
    if rest > 0 and top / rest > 8:
        return _finding(-0.45, 0.6, ["DEPTH_TOP_HEAVY"], {"ratio": round(top / rest, 2)})
    return _finding(0.1, 0.5, ["DEPTH_DISTRIBUTED"], {"ratio": round(top / max(rest, 1e-9), 2)})


def k_price_ceiling(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    """Refuse to chase a vertical move: a hard, boring, effective rule."""
    closes = ctx.closes()
    if len(closes) < 6:
        return _finding(0, 0, ["CHASE_NO_DATA"], missing=["candles"])
    move = (closes[-1] / closes[-5] - 1) * 100
    limit = safe_float(p.get("max_pct"), 40)
    if move > limit:
        return _finding(-0.85, 0.9, ["CHASE_RISK"],
                        {"move_5m_pct": round(move, 2), "limit": limit}, vote="veto")
    return _finding(0.15, 0.7, ["NO_CHASE_RISK"], {"move_5m_pct": round(move, 2)})


# ---------------------------------------------------------------------------
# kernels: news / social (all optional; missing data is reported, never faked)
# ---------------------------------------------------------------------------
def _news_value(ctx: MarketContext, key: str) -> Optional[Dict[str, Any]]:
    payload = ctx.news.get(key)
    return payload if isinstance(payload, dict) else None


def k_news_sentiment(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    payload = _news_value(ctx, "sentiment")
    if payload is None:
        return _finding(0, 0, ["NEWS_SOURCE_UNAVAILABLE"], missing=["news"],
                        evidence={"note": "optional news feed not configured"})
    score = safe_float(payload.get("score"))
    return _finding(max(-1, min(1, score)), safe_float(payload.get("confidence"), 0.4),
                    ["NEWS_POSITIVE" if score > 0 else "NEWS_NEGATIVE" if score < 0
                     else "NEWS_NEUTRAL"], {"articles": payload.get("articles", 0)})


def k_social_activity(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    payload = _news_value(ctx, "social")
    if payload is None:
        return _finding(0, 0, ["SOCIAL_SOURCE_UNAVAILABLE"], missing=["social"])
    velocity = safe_float(payload.get("velocity"))
    return _finding(max(-1, min(1, velocity)), safe_float(payload.get("confidence"), 0.35),
                    ["SOCIAL_SURGE" if velocity > 0 else "SOCIAL_QUIET"],
                    {"mentions": payload.get("mentions", 0)})


def k_source_credibility(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    payload = _news_value(ctx, "sentiment")
    if payload is None:
        return _finding(0, 0, ["CREDIBILITY_UNKNOWN"], missing=["news"])
    credible = safe_float(payload.get("credible_sources"), 0)
    return _finding(0.3 if credible >= 2 else -0.2, 0.4, ["NEWS_CREDIBLE" if credible >= 2
                                                          else "NEWS_THIN_SOURCING"],
                    {"credible_sources": credible})


# ---------------------------------------------------------------------------
# kernels: cross-market
# ---------------------------------------------------------------------------
def k_btc_corr(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    btc = ctx.cross.get("BTCUSDT") or []
    if len(btc) < 20 or len(ctx.candles) < 20:
        return _finding(0, 0, ["CORR_NO_DATA"], missing=["cross_market"])
    own = _returns(ctx.closes()[-40:])
    ref = _returns([c["c"] for c in btc][-40:])
    corr = _corr(own, ref)
    return _finding(0.1, 0.6, ["BTC_CORRELATION"], {"corr": round(corr, 3)})


def k_btc_regime(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    btc = ctx.cross.get("BTCUSDT") or []
    if len(btc) < 30:
        return _finding(0, 0, ["REGIME_NO_DATA"], missing=["cross_market"])
    closes = [c["c"] for c in btc]
    slope = _slope(closes[-30:]) / (sum(closes[-30:]) / 30 + 1e-12)
    return _finding(max(-1, min(1, slope * 150)), 0.7,
                    ["REGIME_RISK_ON" if slope > 0 else "REGIME_RISK_OFF"],
                    {"btc_slope": round(slope, 8)})


def k_market_breadth(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    peers = ctx.cross.get("peer_changes") or []
    if not peers:
        return _finding(0, 0, ["BREADTH_NO_DATA"], missing=["cross_market"])
    up = sum(1 for c in peers if safe_float(c) > 0)
    ratio = up / len(peers)
    return _finding(max(-1, min(1, (ratio - 0.5) * 2)), 0.6, ["BREADTH"],
                    {"up_ratio": round(ratio, 3), "peers": len(peers)})


def k_beta(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    btc = ctx.cross.get("BTCUSDT") or []
    if len(btc) < 30 or len(ctx.candles) < 30:
        return _finding(0, 0, ["BETA_NO_DATA"], missing=["cross_market"])
    own = _returns(ctx.closes()[-60:])
    ref = _returns([c["c"] for c in btc][-60:])
    var = stdev(ref) ** 2
    if not var:
        return _finding(0, 0.3, ["BETA_UNDEFINED"])
    beta = _corr(own, ref) * (stdev(own) / stdev(ref) if stdev(ref) else 0)
    return _finding(-0.2 if abs(beta) > 2.5 else 0.1, 0.5, ["BETA"],
                    {"beta": round(beta, 3)})


def k_volatility_regime(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    rets = _returns(ctx.closes()[-60:])
    if len(rets) < 20:
        return _finding(0, 0, ["VOLREG_NO_DATA"], missing=["candles"])
    sigma = stdev(rets)
    recent = stdev(rets[-10:])
    ratio = recent / sigma if sigma else 1.0
    return _finding(-0.35 if ratio > 2 else 0.1, 0.55, ["VOL_REGIME_SHIFT" if ratio > 2
                                                         else "VOL_REGIME_STABLE"],
                    {"ratio": round(ratio, 2)})


# ---------------------------------------------------------------------------
# kernels: portfolio / risk (read-only views of the real risk engine)
# ---------------------------------------------------------------------------
def k_exposure(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    exposure = safe_float(ctx.portfolio.get("exposure"))
    limit = safe_float(ctx.portfolio.get("max_exposure"), 20.0)
    used = exposure / limit if limit else 1.0
    return _finding(max(-1, min(0.4, 0.4 - used)), 0.8, ["EXPOSURE_LEVEL"],
                    {"exposure": exposure, "limit": limit, "used_pct": round(used * 100, 1)})


def k_position_count(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    count = int(safe_float(ctx.portfolio.get("open_positions")))
    limit = int(safe_float(ctx.portfolio.get("max_positions"), 3))
    return _finding(0.2 if count < limit else -0.5, 0.8, ["POSITION_COUNT"],
                    {"open": count, "limit": limit})


def k_daily_loss(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    pnl = safe_float(ctx.portfolio.get("day_pnl"))
    limit = abs(safe_float(ctx.portfolio.get("daily_loss_limit"), 3.0))
    used = max(0.0, -pnl)
    if used >= limit:
        return _finding(-0.9, 0.95, ["DAILY_LOSS_BREACHED"],
                        {"day_pnl": pnl, "limit": limit}, vote="veto")
    return _finding(0.2 if pnl >= 0 else -0.3, 0.8, ["DAILY_PNL"],
                    {"day_pnl": pnl, "limit": limit, "used": round(used, 4)})


def k_drawdown(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    dd = safe_float(ctx.portfolio.get("drawdown_pct"))
    return _finding(-0.6 if dd < -10 else 0.1, 0.7, ["DRAWDOWN"], {"drawdown_pct": dd})


def k_fee_drag(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    fees = safe_float(ctx.portfolio.get("fees_paid"))
    pnl = abs(safe_float(ctx.portfolio.get("realized_pnl")))
    drag = fees / pnl if pnl > 0 else (1.0 if fees > 0 else 0.0)
    return _finding(-0.5 if drag > 0.5 else 0.1, 0.7, ["FEE_DRAG"],
                    {"fees": round(fees, 4), "drag_ratio": round(drag, 3)})


def k_kill_switch(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    stopped = bool(ctx.portfolio.get("emergency_stop"))
    return _finding(-1.0 if stopped else 0.2, 0.95,
                    ["EMERGENCY_STOP_ACTIVE" if stopped else "KILL_SWITCH_ARMED"],
                    vote="veto" if stopped else "neutral")


# ---------------------------------------------------------------------------
# kernels: backtest / model validation
# ---------------------------------------------------------------------------
def k_model_present(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    model = ctx.backtest.get("model") or {}
    if not model:
        return _finding(0, 0, ["MODEL_NOT_TRAINED"], missing=["backtest_model"])
    return _finding(0.2, 0.9, ["MODEL_PRESENT"],
                    {"version": model.get("version"), "trained_utc": model.get("trained_utc")})


def k_model_age(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    model = ctx.backtest.get("model") or {}
    if not model:
        return _finding(0, 0, ["MODEL_NOT_TRAINED"], missing=["backtest_model"])
    age_h = (time.time() - safe_float(model.get("trained_at"))) / 3600.0
    return _finding(-0.4 if age_h > 24 * 14 else 0.2, 0.7, ["MODEL_AGE"],
                    {"age_hours": round(age_h, 1)})


def k_sample_size(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    metrics = ctx.backtest.get("metrics") or {}
    n = int(safe_float(metrics.get("trades")))
    minimum = int(safe_float(p.get("min_trades"), 30))
    if n == 0:
        return _finding(0, 0, ["NO_BACKTEST"], missing=["backtest_metrics"])
    return _finding(0.3 if n >= minimum else -0.4, 0.8,
                    ["SAMPLE_OK" if n >= minimum else "SAMPLE_SMALL"],
                    {"trades": n, "minimum": minimum})


def k_out_of_sample(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    metrics = ctx.backtest.get("metrics") or {}
    if not metrics:
        return _finding(0, 0, ["NO_BACKTEST"], missing=["backtest_metrics"])
    oos = safe_float(metrics.get("oos_net_return_pct"))
    return _finding(max(-1, min(1, oos / 10.0)), 0.7,
                    ["OOS_POSITIVE" if oos > 0 else "OOS_NEGATIVE"],
                    {"oos_net_return_pct": oos})


def k_leakage_guard(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    metrics = ctx.backtest.get("metrics") or {}
    split = metrics.get("split") or {}
    ok = bool(split.get("chronological", True)) and int(safe_float(split.get("embargo_bars"), 0)) > 0
    if not metrics:
        return _finding(0, 0, ["NO_BACKTEST"], missing=["backtest_metrics"])
    return _finding(0.3 if ok else -0.8, 0.85,
                    ["SPLIT_OK" if ok else "SPLIT_SUSPECT"],
                    {"split": split}, vote="bullish" if ok else "veto")


def k_baseline_compare(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    metrics = ctx.backtest.get("metrics") or {}
    if not metrics or "baseline_expectancy" not in metrics:
        return _finding(0, 0, ["NO_BASELINE"], missing=["baseline"])
    edge = safe_float(metrics.get("expectancy")) - safe_float(metrics.get("baseline_expectancy"))
    return _finding(max(-1, min(1, edge)), 0.7,
                    ["BEATS_BASELINE" if edge > 0 else "TRAILS_BASELINE"],
                    {"edge_per_trade": round(edge, 6)})


def k_metrics_health(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    metrics = ctx.backtest.get("metrics") or {}
    if not metrics:
        return _finding(0, 0, ["NO_BACKTEST"], missing=["backtest_metrics"])
    dd = abs(safe_float(metrics.get("max_drawdown_pct")))
    return _finding(-0.5 if dd > 25 else 0.1, 0.6, ["DRAWDOWN_HEALTH"],
                    {"max_drawdown_pct": round(-dd, 2)})


def k_live_drift(ctx: MarketContext, p: Dict[str, Any]) -> Dict[str, Any]:
    metrics = ctx.backtest.get("metrics") or {}
    live = ctx.portfolio.get("expectancy")
    model = metrics.get("expectancy")
    if live is None or model is None:
        return _finding(0, 0, ["DRIFT_NO_DATA"], missing=["live_expectancy"])
    drift = safe_float(live) - safe_float(model)
    return _finding(-0.4 if drift < -0.005 else 0.1, 0.5, ["LIVE_VS_BACKTEST"],
                    {"live": round(safe_float(live), 6), "model": round(safe_float(model), 6)})


# ---------------------------------------------------------------------------
# team + role registry
# ---------------------------------------------------------------------------
TEAMS: List[Dict[str, Any]] = [
    {"key": "discovery", "name": "Market discovery", "weight": 0.7,
     "blurb": "what is worth looking at at all"},
    {"key": "validation", "name": "Data validation", "weight": 1.4,
     "blurb": "is the data real, fresh and self-consistent"},
    {"key": "momentum", "name": "Momentum & volume", "weight": 1.2,
     "blurb": "is the tape actually pushing"},
    {"key": "liquidity", "name": "Liquidity & book", "weight": 1.1,
     "blurb": "can we get in and out at a sane price"},
    {"key": "technical", "name": "Technical analysis", "weight": 0.9,
     "blurb": "structure, levels, volatility"},
    {"key": "security", "name": "Security & anomalies", "weight": 1.5,
     "blurb": "tradability, manipulation, exchange rules"},
    {"key": "news", "name": "News & social", "weight": 0.5,
     "blurb": "optional public research; silent when unavailable"},
    {"key": "cross", "name": "Cross-market", "weight": 0.7,
     "blurb": "BTC regime, correlation, breadth"},
    {"key": "portfolio", "name": "Portfolio & risk", "weight": 1.3,
     "blurb": "limits, exposure, kill switch"},
    {"key": "backtest", "name": "Backtest & model", "weight": 0.8,
     "blurb": "does the model have evidence behind it"},
]

# (team, role, kernel, params) - exactly 100 rows, asserted below.
ROLES: List[Tuple[str, str, Callable[..., Dict[str, Any]], Dict[str, Any]]] = [
    # -- discovery (10)
    ("discovery", "Universe liquidity floor", k_universe, {"min_quote_volume": 5e6}),
    ("discovery", "Large-cap universe scan", k_universe, {"min_quote_volume": 50e6}),
    ("discovery", "Mover screen (-25%..+25%)", k_movers, {"lo": -25, "hi": 25}),
    ("discovery", "Blow-off screen (+40%)", k_movers, {"lo": -40, "hi": 40}),
    ("discovery", "Volume surge hunter", k_volume_surge, {"look": 20}),
    ("discovery", "Slow volume surge", k_volume_surge, {"look": 60}),
    ("discovery", "Trend filter (50 EMA)", k_trend_filter, {"period": 50}),
    ("discovery", "Trend filter (200 EMA)", k_trend_filter, {"period": 200}),
    ("discovery", "Sector proxy", k_sector_proxy, {}),
    ("discovery", "Venue health", k_venue_health, {}),
    # -- validation (10)
    ("validation", "Feed freshness", k_freshness, {"limit_s": 90}),
    ("validation", "Feed freshness (strict)", k_freshness, {"limit_s": 30}),
    ("validation", "Kline continuity", k_capture_integrity, {}),
    ("validation", "OHLC consistency", k_ohlc_consistency, {}),
    ("validation", "Zero-volume bars", k_zero_volume, {}),
    ("validation", "Return outliers", k_outlier, {}),
    ("validation", "Order book sanity", k_book_sanity, {}),
    ("validation", "Trade tape order", k_trade_tape, {}),
    ("validation", "Kline vs ticker price", k_source_agreement, {}),
    ("validation", "Quote asset match", k_quote_asset, {}),
    # -- momentum (10)
    ("momentum", "EMA 9/21 cross", k_ema_cross, {"fast": 9, "slow": 21}),
    ("momentum", "EMA 12/26 cross", k_ema_cross, {"fast": 12, "slow": 26}),
    ("momentum", "RSI 14", k_rsi, {"period": 14}),
    ("momentum", "RSI 7 (fast)", k_rsi, {"period": 7}),
    ("momentum", "MACD proxy", k_macd, {}),
    ("momentum", "20-bar breakout", k_breakout, {"look": 20}),
    ("momentum", "55-bar breakout", k_breakout, {"look": 55}),
    ("momentum", "Volume z-score", k_volume_z, {}),
    ("momentum", "Volume trend", k_volume_trend, {}),
    ("momentum", "Taker buy pressure", k_buy_pressure, {}),
    # -- liquidity (10)
    ("liquidity", "Spread check", k_spread, {"max_bps": 25}),
    ("liquidity", "Tight spread check", k_spread, {"max_bps": 10}),
    ("liquidity", "Top-10 book imbalance", k_book_imbalance, {"depth": 10}),
    ("liquidity", "Top-50 book imbalance", k_book_imbalance, {"depth": 50}),
    ("liquidity", "Bid depth value", k_depth_value, {"side": "bids", "depth": 20}),
    ("liquidity", "Ask depth value", k_depth_value, {"side": "asks", "depth": 20}),
    ("liquidity", "Slippage for 10 USDT", k_slippage, {"notional_usdt": 10}),
    ("liquidity", "24h quote volume", k_quote_volume_24h, {"min_usdt": 20e6}),
    ("liquidity", "Trade arrival rate", k_trade_rate, {"min_per_s": 0.05}),
    ("liquidity", "Median trade size", k_trade_size, {"min_usdt": 20}),
    # -- technical (10)
    ("technical", "Bollinger position", k_bollinger, {"period": 20}),
    ("technical", "ATR volatility", k_atr, {"period": 14}),
    ("technical", "Range position (60)", k_range_position, {"look": 60}),
    ("technical", "Range position (240)", k_range_position, {"look": 240}),
    ("technical", "Trend slope (30)", k_trend_slope, {"look": 30}),
    ("technical", "Trend slope (120)", k_trend_slope, {"look": 120}),
    ("technical", "Higher highs", k_higher_highs, {}),
    ("technical", "Candle body pressure", k_candle_body, {}),
    ("technical", "VWAP deviation", k_vwap, {}),
    ("technical", "Relative strength vs BTC", k_relative_strength, {}),
    # -- security (10)
    ("security", "Symbol trading status", k_symbol_rules, {}),
    ("security", "Exchange minimum notional", k_min_notional, {}),
    ("security", "Wash-trade pattern", k_wash_trading, {}),
    ("security", "Pump signature", k_pump_signature, {}),
    ("security", "Spread anomaly", k_spread_anomaly, {}),
    ("security", "Listing age", k_listing_age, {}),
    ("security", "Depth concentration", k_spoof_depth, {}),
    ("security", "Chase-risk guard (5m)", k_price_ceiling, {"max_pct": 40}),
    ("security", "Chase-risk guard (15m)", k_price_ceiling, {"max_pct": 25}),
    ("security", "Tape value skew", k_trade_skew, {}),
    # -- news / social (10) - optional sources, neutral when absent
    ("news", "Headline sentiment", k_news_sentiment, {}),
    ("news", "Headline sentiment (strict)", k_news_sentiment, {}),
    ("news", "Social mention velocity", k_social_activity, {}),
    ("news", "Social quiet check", k_social_activity, {}),
    ("news", "Source credibility", k_source_credibility, {}),
    ("news", "Exchange announcement watch", k_news_sentiment, {}),
    ("news", "Regulatory watch", k_news_sentiment, {}),
    ("news", "Influencer flag", k_social_activity, {}),
    ("news", "Rumour guard", k_source_credibility, {}),
    ("news", "Event calendar", k_news_sentiment, {}),
    # -- cross-market (10)
    ("cross", "BTC correlation", k_btc_corr, {}),
    ("cross", "BTC regime", k_btc_regime, {}),
    ("cross", "BTC beta", k_beta, {}),
    ("cross", "Market breadth", k_market_breadth, {}),
    ("cross", "Volatility regime", k_volatility_regime, {}),
    ("cross", "Peer rotation", k_sector_proxy, {}),
    ("cross", "Relative strength (BTC)", k_relative_strength, {}),
    ("cross", "Regime confirmation", k_btc_regime, {}),
    ("cross", "Breadth confirmation", k_market_breadth, {}),
    ("cross", "Correlation stress", k_btc_corr, {}),
    # -- portfolio / risk (10; brief said 5 - see module docstring)
    ("portfolio", "Total exposure", k_exposure, {}),
    ("portfolio", "Open position count", k_position_count, {}),
    ("portfolio", "Daily loss budget", k_daily_loss, {}),
    ("portfolio", "Drawdown state", k_drawdown, {}),
    ("portfolio", "Fee drag", k_fee_drag, {}),
    ("portfolio", "Kill switch", k_kill_switch, {}),
    ("portfolio", "Per-symbol exposure", k_exposure, {}),
    ("portfolio", "Risk budget headroom", k_exposure, {}),
    ("portfolio", "Loss-limit proximity", k_daily_loss, {}),
    ("portfolio", "Emergency-stop echo", k_kill_switch, {}),
    # -- backtest / model validation (10; brief said 5 - see module docstring)
    ("backtest", "Model present", k_model_present, {}),
    ("backtest", "Model age", k_model_age, {}),
    ("backtest", "Sample size", k_sample_size, {"min_trades": 30}),
    ("backtest", "Out-of-sample return", k_out_of_sample, {}),
    ("backtest", "Leakage guard", k_leakage_guard, {}),
    ("backtest", "Baseline comparison", k_baseline_compare, {}),
    ("backtest", "Metrics health", k_metrics_health, {}),
    ("backtest", "Live vs backtest drift", k_live_drift, {}),
    ("backtest", "Sample size (strict)", k_sample_size, {"min_trades": 100}),
    ("backtest", "OOS confirmation", k_out_of_sample, {}),
]

assert len(ROLES) == 100, "the registry must contain exactly 100 agents, got %d" % len(ROLES)
for _team in TEAMS:
    _count = sum(1 for r in ROLES if r[0] == _team["key"])
    assert _count == 10, "team %s has %d agents" % (_team["key"], _count)


@dataclass
class AgentDef:
    agent_id: str
    team: str
    team_name: str
    role: str
    kernel: Callable[..., Dict[str, Any]]
    params: Dict[str, Any]

    def as_dict(self) -> Dict[str, Any]:
        return {"agent_id": self.agent_id, "team": self.team,
                "team_name": self.team_name, "role": self.role,
                "params": self.params}


def build_registry() -> List[AgentDef]:
    team_names = {t["key"]: t["name"] for t in TEAMS}
    out: List[AgentDef] = []
    for index, (team, role, kernel, params) in enumerate(ROLES, start=1):
        out.append(AgentDef("A{:03d}".format(index), team, team_names[team], role,
                            kernel, params))
    return out


REGISTRY: List[AgentDef] = build_registry()


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------
@dataclass
class Consensus:
    score: float = 0.0                    # -100..100
    verdict: str = "WAIT"                 # BUY | SELL | WAIT | REJECT
    confidence: float = 0.0
    teams: List[Dict[str, Any]] = field(default_factory=list)
    vetoes: List[Dict[str, Any]] = field(default_factory=list)
    top_evidence: List[Dict[str, Any]] = field(default_factory=list)
    reason_counts: Dict[str, int] = field(default_factory=dict)
    agents_total: int = 0
    agents_ok: int = 0
    agents_no_data: int = 0
    agents_error: int = 0
    missing_sources: List[str] = field(default_factory=list)
    latency_ms: float = 0.0
    evaluated_at: float = 0.0
    findings: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self, include_findings: bool = True) -> Dict[str, Any]:
        payload = {
            "score": round(self.score, 2), "verdict": self.verdict,
            "confidence": round(self.confidence, 3), "teams": self.teams,
            "vetoes": self.vetoes, "top_evidence": self.top_evidence,
            "reason_counts": self.reason_counts,
            "agents_total": self.agents_total, "agents_ok": self.agents_ok,
            "agents_no_data": self.agents_no_data, "agents_error": self.agents_error,
            "missing_sources": self.missing_sources,
            "latency_ms": round(self.latency_ms, 1),
            "evaluated_at": self.evaluated_at,
            "evaluated_at_utc": utc_str(self.evaluated_at),
        }
        if include_findings:
            payload["findings"] = self.findings
        return payload


class AgentRunner:
    """Bounded async execution of the 100 roles over one snapshot."""

    def __init__(self, registry: Optional[List[AgentDef]] = None, *,
                 max_workers: int = 4, timeout_s: float = 4.0):
        self.registry = registry or REGISTRY
        self.max_workers = max(1, int(max_workers))
        self.timeout_s = max(0.2, float(timeout_s))
        self.last: Dict[str, Dict[str, Any]] = {}
        self.runs = 0

    # -- one agent -----------------------------------------------------
    def _run_one(self, agent: AgentDef, ctx: MarketContext) -> Finding:
        started = time.time()
        finding = Finding(agent_id=agent.agent_id, team=agent.team, role=agent.role,
                          timestamp=ctx.now or time.time())
        try:
            result = agent.kernel(ctx, agent.params)
            finding.score = float(result.get("score", 0.0))
            finding.confidence = float(result.get("confidence", 0.0))
            finding.vote = str(result.get("vote", "neutral"))
            finding.reason_codes = list(result.get("reason_codes") or [])
            finding.evidence = dict(result.get("evidence") or {})
            finding.missing = list(result.get("missing") or [])
            finding.state = "no_data" if (finding.missing and finding.confidence <= 0.05) else "ok"
        except Exception as exc:                      # a broken kernel must not kill the run
            finding.state = "error"
            finding.error = "{}: {}".format(exc.__class__.__name__, exc)[:160]
            finding.vote = "neutral"
            finding.score = 0.0
        finding.latency_ms = (time.time() - started) * 1000.0
        return finding

    # -- all agents ----------------------------------------------------
    async def run_async(self, ctx: MarketContext) -> List[Finding]:
        loop = asyncio.get_running_loop()
        semaphore = asyncio.Semaphore(self.max_workers)

        async def guarded(agent: AgentDef) -> Finding:
            async with semaphore:
                try:
                    return await asyncio.wait_for(
                        loop.run_in_executor(None, self._run_one, agent, ctx),
                        timeout=self.timeout_s)
                except asyncio.TimeoutError:
                    return Finding(agent.agent_id, agent.team, agent.role,
                                   state="timeout", timestamp=ctx.now,
                                   error="agent exceeded {:.1f}s".format(self.timeout_s))

        started = time.time()
        findings = await asyncio.gather(*(guarded(a) for a in self.registry))
        self.runs += 1
        for finding in findings:
            self.last[finding.agent_id] = finding.as_dict()
        self.last_latency_ms = (time.time() - started) * 1000.0
        return list(findings)

    def run_sync(self, ctx: MarketContext) -> List[Finding]:
        """Used by tests and by the CLI when no event loop is running."""
        findings = [self._run_one(agent, ctx) for agent in self.registry]
        self.runs += 1
        for finding in findings:
            self.last[finding.agent_id] = finding.as_dict()
        self.last_latency_ms = sum(f.latency_ms for f in findings)
        return findings

    # -- consensus -----------------------------------------------------
    def consensus(self, findings: Sequence[Finding]) -> Consensus:
        weights = {t["key"]: float(t["weight"]) for t in TEAMS}
        names = {t["key"]: t["name"] for t in TEAMS}
        teams: Dict[str, Dict[str, Any]] = {}
        reason_counts: Dict[str, int] = {}
        vetoes: List[Dict[str, Any]] = []
        missing: List[str] = []

        for finding in findings:
            bucket = teams.setdefault(finding.team, {
                "key": finding.team, "name": names.get(finding.team, finding.team),
                "count": 0, "score_sum": 0.0, "weight_sum": 0.0,
                "bullish": 0, "bearish": 0, "neutral": 0, "no_data": 0,
            })
            bucket["count"] += 1
            if finding.state in ("no_data", "timeout", "error"):
                bucket["no_data"] += 1
            weight = max(0.0, finding.confidence)
            bucket["score_sum"] += finding.score * weight
            bucket["weight_sum"] += weight
            if finding.vote == "bullish":
                bucket["bullish"] += 1
            elif finding.vote == "bearish":
                bucket["bearish"] += 1
            elif finding.vote == "veto":
                bucket["bearish"] += 1
                if finding.confidence >= 0.6:
                    vetoes.append({"agent_id": finding.agent_id, "role": finding.role,
                                   "team": finding.team,
                                   "codes": finding.reason_codes,
                                   "evidence": finding.evidence})
            else:
                bucket["neutral"] += 1
            for code in finding.reason_codes:
                reason_counts[code] = reason_counts.get(code, 0) + 1
            for item in finding.missing:
                if item not in missing:
                    missing.append(item)

        team_rows: List[Dict[str, Any]] = []
        total_weight = 0.0
        weighted = 0.0
        for key in [t["key"] for t in TEAMS]:
            bucket = teams.get(key)
            if not bucket:
                continue
            mean = bucket["score_sum"] / bucket["weight_sum"] if bucket["weight_sum"] else 0.0
            bucket["score"] = round(mean, 4)
            bucket["verdict"] = ("bullish" if mean > 0.15 else
                                 "bearish" if mean < -0.15 else "neutral")
            bucket.pop("score_sum", None)
            bucket.pop("weight_sum", None)
            team_rows.append(bucket)
            weight = weights.get(key, 1.0) * min(1.0, bucket["count"] / 10.0)
            weighted += mean * weight
            total_weight += weight

        score = (weighted / total_weight * 100.0) if total_weight else 0.0
        ok = sum(1 for f in findings if f.state == "ok")
        no_data = sum(1 for f in findings if f.state == "no_data")
        errors = sum(1 for f in findings if f.state in ("error", "timeout"))
        confidence = (ok / len(findings)) if findings else 0.0

        if vetoes:
            verdict = "REJECT"
        elif score >= 25 and confidence >= 0.5:
            verdict = "BUY"
        elif score <= -25 and confidence >= 0.5:
            verdict = "SELL"
        else:
            verdict = "WAIT"

        top = sorted((f for f in findings if f.state == "ok"),
                     key=lambda f: abs(f.score) * f.confidence, reverse=True)[:6]

        return Consensus(
            score=score, verdict=verdict, confidence=confidence, teams=team_rows,
            vetoes=vetoes,
            top_evidence=[{"agent_id": f.agent_id, "role": f.role, "team": f.team,
                           "score": round(f.score, 3),
                           "confidence": round(f.confidence, 2),
                           "codes": f.reason_codes,
                           "evidence": f.evidence} for f in top],
            reason_counts=dict(sorted(reason_counts.items(), key=lambda kv: -kv[1])[:20]),
            agents_total=len(findings), agents_ok=ok, agents_no_data=no_data,
            agents_error=errors, missing_sources=missing,
            latency_ms=sum(f.latency_ms for f in findings),
            evaluated_at=findings[0].timestamp if findings else time.time(),
            findings=[f.as_dict() for f in findings],
        )
