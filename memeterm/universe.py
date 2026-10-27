"""
memeterm.universe - pick *which* Binance Spot symbols to research or train on.

Binance lists thousands of pairs; a phone cannot train all of them. This module
is the explicit, testable filter used by `train-suite` (and available to any
caller that needs a symbol universe):

1. only symbols whose exchangeInfo status is TRADING,
2. only symbols where Spot trading is allowed (never margin/futures symbols),
3. only the requested quote asset (USDT by default),
4. no stablecoin bases (USDT/USDC/FDUSD..., which barely move),
5. no leveraged tokens (BTCUPUSDT, ETHDOWNUSDT, ...),
6. ranked by 24h *quote* volume, because that is the liquidity that decides
   whether a 10 USDT order can even be filled.

Nothing here invents data: a symbol without a ticker row is reported as
skipped rather than assumed liquid.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

USDT_LIKE = {
    "USDT", "USDC", "BUSD", "TUSD", "FDUSD", "DAI", "USDP", "USDD", "PYUSD",
    "EUR", "GBP", "AUD", "BRL", "TRY", "BIDR", "IDRT", "RUB", "UAH", "NGN",
    "ZAR", "PLN", "RON", "ARS", "JPY",
}

LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")


def _ticker_map(tickers: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    for row in tickers or []:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        try:
            out[symbol] = {
                "quote_volume": float(row.get("quoteVolume") or 0.0),
                "price": float(row.get("lastPrice") or 0.0),
                "change_pct": float(row.get("priceChangePercent") or 0.0),
            }
        except (TypeError, ValueError):
            continue
    return out


def _skip_reason(row: Dict[str, Any], quote: str,
                 tickers: Dict[str, Dict[str, float]]) -> Optional[str]:
    symbol = str(row.get("symbol") or "").upper()
    if not symbol:
        return "unnamed"
    if str(row.get("status") or "").upper() != "TRADING":
        return "not trading"
    if row.get("isSpotTradingAllowed") is False:
        return "spot disabled"
    permissions = row.get("permissions")
    if isinstance(permissions, list) and permissions and "SPOT" not in permissions:
        return "no spot permission"
    if str(row.get("quoteAsset") or "").upper() != quote.upper():
        return "other quote asset"
    base = str(row.get("baseAsset") or "").upper()
    if not base:
        return "unnamed"
    if base in USDT_LIKE:
        return "stablecoin"
    if base.endswith(LEVERAGED_SUFFIXES):
        return "leveraged token"
    if symbol not in tickers:
        return "no ticker data"
    if tickers[symbol]["quote_volume"] <= 0:
        return "zero volume"
    return None


def select_symbols(exchange_info: Dict[str, Any],
                   tickers: Sequence[Dict[str, Any]],
                   *, count: int = 45, quote: str = "USDT",
                   include: Optional[Iterable[str]] = None,
                   exclude: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Return the ranked universe plus a transparent account of what was skipped.

    ``{"symbols": [...], "skipped": {reason: n}, "requested": 45, "quote": "USDT"}``
    """
    quote = (quote or "USDT").upper()
    count = max(1, int(count))
    include_set = {s.upper() for s in (include or []) if s}
    exclude_set = {s.upper() for s in (exclude or []) if s}
    tickers_by_symbol = _ticker_map(tickers)

    skipped: Dict[str, int] = {}
    candidates: List[Dict[str, Any]] = []
    rows = (exchange_info or {}).get("symbols") or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        if include_set and symbol not in include_set:
            continue
        if symbol in exclude_set:
            skipped["excluded by request"] = skipped.get("excluded by request", 0) + 1
            continue
        reason = _skip_reason(row, quote, tickers_by_symbol)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        info = tickers_by_symbol[symbol]
        candidates.append({
            "symbol": symbol,
            "base": str(row.get("baseAsset") or "").upper(),
            "quote": quote,
            "quote_volume": round(info["quote_volume"], 2),
            "price": info["price"],
            "change_pct": round(info["change_pct"], 3),
        })

    candidates.sort(key=lambda item: item["quote_volume"], reverse=True)
    chosen = candidates[:count]
    return {
        "symbols": chosen,
        "skipped": skipped,
        "requested": count,
        "available": len(candidates),
        "quote": quote,
    }


def describe_selection(selection: Dict[str, Any]) -> str:
    """One-line, human explanation of a selection (used by the CLI)."""
    chosen = len(selection.get("symbols") or [])
    available = int(selection.get("available") or chosen)
    skipped = selection.get("skipped") or {}
    parts = ["{}/{} {} pairs".format(chosen, available, selection.get("quote", "USDT"))]
    top = sorted(skipped.items(), key=lambda item: item[1], reverse=True)[:3]
    if top:
        parts.append("skipped " + ", ".join("{} {}".format(n, reason) for reason, n in top))
    return " · ".join(parts)
