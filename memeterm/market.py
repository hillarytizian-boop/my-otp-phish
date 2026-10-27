"""
memeterm.market - one cached, rate-aware market data service.

Everything that reads the exchange goes through here, so the number of HTTP
requests stays small and predictable on a phone:

* one `ticker/24hr` call for the whole watchlist every `market_refresh_s`
* one `bookTicker` call for the same list
* klines for the *active* symbol only, cached for 20 s
* depth + recent trades for the active symbol, cached for 10 s
* BTC klines once a minute, shared by every cross-market agent

If a fetch fails, the previous snapshot is kept and marked stale rather than
being replaced by zeros. Agents see `data_age_s` and can veto on it.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from .binance import BinanceClient, BinanceError, SymbolFilters
from .util import iso_age, safe_float, utc_now

INTERVAL_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
               "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


class MarketService:
    def __init__(self, client: BinanceClient, *, kline_ttl_s: float = 20.0,
                 depth_ttl_s: float = 10.0, btc_ttl_s: float = 60.0,
                 clock=time.time):
        self.client = client
        self.clock = clock
        self.kline_ttl_s = kline_ttl_s
        self.depth_ttl_s = depth_ttl_s
        self.btc_ttl_s = btc_ttl_s
        self.lock = threading.RLock()
        self.tickers: Dict[str, Dict[str, Any]] = {}
        self.books: Dict[str, Dict[str, Any]] = {}
        self.tickers_at = 0.0
        self.books_at = 0.0
        self._klines: Dict[str, Dict[str, Any]] = {}
        self._depth: Dict[str, Dict[str, Any]] = {}
        self._trades: Dict[str, Dict[str, Any]] = {}
        self._btc: Dict[str, Any] = {}
        self._filters: Dict[str, SymbolFilters] = {}
        self._filters_at = 0.0
        self.errors: List[str] = []
        self.last_refresh_at = 0.0
        self.refresh_count = 0

    # ------------------------------------------------------------------
    def _note_error(self, message: str) -> None:
        self.errors.append("{} {}".format(iso_age(utc_now()), message)[:200])
        del self.errors[:-20]

    # ------------------------------------------------------------------
    def refresh_tickers(self, symbols: List[str]) -> Dict[str, Any]:
        symbols = [s.upper() for s in symbols if s]
        if not symbols:
            return {"ok": False, "error": "empty watchlist"}
        try:
            rows = self.client.ticker_24h(symbols)
            books = self.client.book_ticker(symbols)
        except BinanceError as exc:
            self._note_error(str(exc))
            return {"ok": False, "error": str(exc), "age_s": self.ticker_age(), }
        now = self.clock()
        with self.lock:
            for row in rows:
                symbol = str(row.get("symbol", "")).upper()
                if symbol:
                    self.tickers[symbol] = row
            for row in books:
                symbol = str(row.get("symbol", "")).upper()
                if symbol:
                    self.books[symbol] = row
            self.tickers_at = now
            self.books_at = now
            self.last_refresh_at = now
            self.refresh_count += 1
        return {"ok": True, "symbols": len(rows), "age_s": 0.0}

    def ticker_age(self) -> float:
        return (self.clock() - self.tickers_at) if self.tickers_at else 1e9

    def klines(self, symbol: str, interval: str = "1m", limit: int = 240,
               force: bool = False) -> List[List[Any]]:
        symbol = symbol.upper()
        key = "{}:{}:{}".format(symbol, interval, limit)
        cached = self._klines.get(key)
        now = self.clock()
        if cached and not force and (now - cached["at"]) < self.kline_ttl_s:
            return cached["rows"]
        try:
            rows = self.client.klines(symbol, interval=interval, limit=limit)
        except BinanceError as exc:
            self._note_error("klines {}: {}".format(symbol, exc))
            return cached["rows"] if cached else []
        self._klines[key] = {"rows": rows, "at": now}
        return rows

    def depth(self, symbol: str, limit: int = 20, force: bool = False) -> Dict[str, Any]:
        symbol = symbol.upper()
        cached = self._depth.get(symbol)
        now = self.clock()
        if cached and not force and (now - cached["at"]) < self.depth_ttl_s:
            return cached["rows"]
        try:
            rows = self.client.depth(symbol, limit=limit)
        except BinanceError as exc:
            self._note_error("depth {}: {}".format(symbol, exc))
            return cached["rows"] if cached else {}
        self._depth[symbol] = {"rows": rows, "at": now}
        return rows

    def trades(self, symbol: str, limit: int = 50, force: bool = False) -> List[Dict[str, Any]]:
        symbol = symbol.upper()
        cached = self._trades.get(symbol)
        now = self.clock()
        if cached and not force and (now - cached["at"]) < self.depth_ttl_s:
            return cached["rows"]
        try:
            rows = self.client.recent_trades(symbol, limit=limit)
        except BinanceError as exc:
            self._note_error("trades {}: {}".format(symbol, exc))
            return cached["rows"] if cached else []
        self._trades[symbol] = {"rows": rows, "at": now}
        return rows

    def btc_candles(self, interval: str = "5m", limit: int = 120) -> List[Dict[str, float]]:
        cached = self._btc.get(interval)
        now = self.clock()
        if cached and (now - cached["at"]) < self.btc_ttl_s:
            return cached["rows"]
        try:
            rows = self.client.klines("BTCUSDT", interval=interval, limit=limit)
        except BinanceError as exc:
            self._note_error("btc: {}".format(exc))
            return cached["rows"] if cached else []
        parsed = parse_klines(rows)
        self._btc[interval] = {"rows": parsed, "at": now}
        return parsed

    def filters(self, symbol: str, force: bool = False) -> Optional[SymbolFilters]:
        symbol = symbol.upper()
        now = self.clock()
        if not force and symbol in self._filters and (now - self._filters_at) < 1800:
            return self._filters[symbol]
        try:
            filters = self.client.filters(symbol)
            with self.lock:
                self._filters[symbol] = filters
                self._filters_at = now
            return filters
        except BinanceError as exc:
            self._note_error("filters {}: {}".format(symbol, exc))
            return self._filters.get(symbol)

    # ------------------------------------------------------------------
    def ticker(self, symbol: str) -> Dict[str, Any]:
        return self.tickers.get(symbol.upper(), {})

    def price(self, symbol: str) -> float:
        row = self.ticker(symbol)
        price = safe_float(row.get("lastPrice"))
        if price:
            return price
        book = self.books.get(symbol.upper(), {})
        return safe_float(book.get("bidPrice"))

    def watchlist_rows(self, symbols: List[str]) -> List[Dict[str, Any]]:
        rows = []
        for symbol in symbols:
            ticker = self.ticker(symbol)
            book = self.books.get(symbol.upper(), {})
            bid = safe_float(book.get("bidPrice"))
            ask = safe_float(book.get("askPrice"))
            spread_bps = ((ask - bid) / bid * 10_000) if bid and ask else 0.0
            rows.append({
                "symbol": symbol.upper(),
                "price": safe_float(ticker.get("lastPrice")) or bid,
                "change_pct": safe_float(ticker.get("priceChangePercent")),
                "quote_volume": safe_float(ticker.get("quoteVolume")),
                "bid": bid, "ask": ask, "spread_bps": round(spread_bps, 2),
                "stale": self.ticker_age() > 30,
            })
        return rows

    def snapshot(self, symbol: str, *, interval: str = "1m", limit: int = 240) -> Dict[str, Any]:
        """One immutable-ish dict that the agent context is built from."""
        symbol = symbol.upper()
        rows = self.klines(symbol, interval=interval, limit=limit)
        candles = parse_klines(rows)
        latest_ts = candles[-1]["ts"] if candles else 0
        interval_ms = INTERVAL_MS.get(interval, 60_000)
        age = max(0.0, (self.clock() * 1000 - latest_ts) / 1000.0) if latest_ts else 1e9
        return {
            "symbol": symbol,
            "candles": candles,
            "ticker": self.ticker(symbol),
            "book": self.depth(symbol),
            "trades": self.trades(symbol),
            "filters": self.filters(symbol),
            "btc": self.btc_candles(),
            "data_age_s": min(age, self.ticker_age()),
            "kline_age_s": age,
            "interval_ms": interval_ms,
            "fetched_at": self.clock(),
            "sources": ["binance:klines", "binance:ticker24h", "binance:bookTicker",
                        "binance:depth", "binance:trades"],
        }

    # ------------------------------------------------------------------
    def health(self) -> Dict[str, Any]:
        return {
            "last_refresh_age_s": round(self.ticker_age(), 1) if self.tickers_at else None,
            "refresh_count": self.refresh_count,
            "symbols_cached": len(self.tickers),
            "errors": self.errors[-5:],
            "client": self.client.health(),
        }


def parse_klines(rows: List[List[Any]]) -> List[Dict[str, float]]:
    """Binance kline array -> dicts with the fields the agents use."""
    out: List[Dict[str, float]] = []
    for row in rows or []:
        try:
            out.append({
                "ts": int(row[0]),
                "o": float(row[1]), "h": float(row[2]), "l": float(row[3]),
                "c": float(row[4]), "v": float(row[5]),
                "close_ts": int(row[6]),
                "qv": float(row[7]), "trades": int(row[8]),
                "tb": float(row[9]), "tq": float(row[10]),
                "ignore": float(row[11]) if len(row) > 11 else 0.0,
            })
        except (IndexError, TypeError, ValueError):
            continue
    return out
