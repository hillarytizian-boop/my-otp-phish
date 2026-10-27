"""
memeterm.demo - synthetic market data for UI evaluation without a network.

This exists because the machine this was built on has no outbound network to
Binance, and because a dashboard with empty charts teaches nobody anything.

It is deliberately *incapable* of trading: only public market-data endpoints are
implemented, every signed endpoint returns 401. Even if you pointed live mode at
it, the pre-flight would fail. Anything driven by it is labelled DEMO DATA in
the UI and in the console banner.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import time
from typing import Any, Dict, List, Optional

from .binance import Response

SYMBOLS = {
    "BTCUSDT": 60_000.0,
    "ETHUSDT": 3_000.0,
    "SOLUSDT": 150.0,
    "BNBUSDT": 560.0,
    "XRPUSDT": 0.62,
}


class DemoTransport:
    """A deterministic-ish random walk with Binance-shaped payloads."""

    is_demo = True

    def __init__(self, seed: int = 7):
        self.rng = random.Random(seed)
        self.calls = 0
        self.t0 = time.time()
        self.prices = dict(SYMBOLS)
        self.drift = {symbol: 0.0 for symbol in SYMBOLS}

    # ------------------------------------------------------------------
    def _step(self) -> None:
        for symbol, price in list(self.prices.items()):
            self.drift[symbol] = self.drift[symbol] * 0.92 + self.rng.gauss(0, 0.0009)
            self.prices[symbol] = max(1e-8, price * (1 + self.drift[symbol]))

    def _seed(self, symbol: str) -> int:
        digest = hashlib.sha256(symbol.encode()).hexdigest()
        return int(digest[:8], 16)

    def _candles(self, symbol: str, limit: int, interval_ms: int = 60_000) -> List[List[Any]]:
        price = self.prices.get(symbol, 100.0)
        rng = random.Random(self._seed(symbol) + int(time.time() // 60))
        now = int(time.time() * 1000)
        rows: List[List[Any]] = []
        level = price * (1 - 0.012 * rng.random())
        for i in range(limit):
            ts = now - (limit - i) * interval_ms
            open_ = level
            move = rng.gauss(0, 0.0016) + math.sin(i / 18.0) * 0.0004
            close = max(1e-8, open_ * (1 + move))
            high = max(open_, close) * (1 + abs(rng.gauss(0, 0.0007)))
            low = min(open_, close) * (1 - abs(rng.gauss(0, 0.0007)))
            volume = abs(rng.gauss(120, 55)) + 5
            rows.append([ts, "{:.8f}".format(open_), "{:.8f}".format(high),
                         "{:.8f}".format(low), "{:.8f}".format(close),
                         "{:.4f}".format(volume), ts + interval_ms - 1,
                         "{:.4f}".format(volume * close), int(abs(rng.gauss(90, 30))) + 5,
                         "{:.4f}".format(volume * 0.55), "{:.4f}".format(volume * 0.55 * close),
                         "0"])
            level = close
        return rows

    # ------------------------------------------------------------------
    def request(self, method: str, url: str, headers: Dict[str, str],
                timeout: Optional[float] = None) -> Response:
        import urllib.parse
        self.calls += 1
        self._step()
        parsed = urllib.parse.urlparse(url)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        path = parsed.path

        def ok(payload: Any) -> Response:
            return Response(200, {}, json.dumps(payload).encode())

        def err(code: int, msg: str, status: int = 400) -> Response:
            return Response(status, {}, json.dumps({"code": code, "msg": msg}).encode())

        if path == "/api/v3/time":
            return ok({"serverTime": int(time.time() * 1000)})
        if path == "/api/v3/exchangeInfo":
            return ok({"symbols": [self._symbol_row(symbol) for symbol in SYMBOLS]})
        if path == "/api/v3/ticker/24hr":
            rows = []
            wanted = SYMBOLS.keys()
            if query.get("symbols"):
                try:
                    wanted = json.loads(query["symbols"])
                except ValueError:
                    wanted = list(SYMBOLS.keys())
            if query.get("symbol"):
                wanted = [query["symbol"]]
            for symbol in wanted:
                price = self.prices.get(symbol)
                if price is None:
                    continue
                change = (price / SYMBOLS[symbol] - 1) * 100
                rows.append({"symbol": symbol, "lastPrice": "{:.8f}".format(price),
                             "priceChangePercent": "{:.3f}".format(change),
                             "quoteVolume": "{:.2f}".format(25_000_000 + price * 100),
                             "highPrice": "{:.8f}".format(price * 1.03),
                             "lowPrice": "{:.8f}".format(price * 0.97),
                             "bidPrice": "{:.8f}".format(price * 0.9998),
                             "askPrice": "{:.8f}".format(price * 1.0002)})
            return ok(rows if len(rows) != 1 else rows[0])
        if path == "/api/v3/ticker/bookTicker":
            wanted = list(SYMBOLS.keys())
            if query.get("symbols"):
                try:
                    wanted = json.loads(query["symbols"])
                except ValueError:
                    wanted = list(SYMBOLS.keys())
            out = []
            for symbol in wanted:
                price = self.prices.get(symbol)
                if price is None:
                    continue
                out.append({"symbol": symbol, "bidPrice": "{:.8f}".format(price * 0.9997),
                            "askPrice": "{:.8f}".format(price * 1.0003)})
            return ok(out)
        if path == "/api/v3/klines":
            symbol = query.get("symbol", "BTCUSDT")
            limit = min(int(query.get("limit", 200)), 1000)
            interval_ms = {"1m": 60_000, "5m": 300_000, "15m": 900_000,
                           "1h": 3_600_000, "4h": 14_400_000}.get(
                               query.get("interval", "1m"), 60_000)
            return ok(self._candles(symbol, limit, interval_ms))
        if path == "/api/v3/depth":
            symbol = query.get("symbol", "BTCUSDT")
            price = self.prices.get(symbol, 100.0)
            rng = random.Random(self._seed(symbol) + self.calls)
            return ok({"bids": [["{:.8f}".format(price * (1 - 0.0001 * i)),
                                 "{:.4f}".format(abs(rng.gauss(2, 0.6)))]
                                for i in range(1, 21)],
                       "asks": [["{:.8f}".format(price * (1 + 0.0001 * i)),
                                 "{:.4f}".format(abs(rng.gauss(2, 0.6)))]
                                for i in range(1, 21)]})
        if path == "/api/v3/trades":
            symbol = query.get("symbol", "BTCUSDT")
            price = self.prices.get(symbol, 100.0)
            rng = random.Random(self._seed(symbol) + self.calls)
            now = int(time.time() * 1000)
            return ok([{"price": "{:.8f}".format(price * (1 + rng.gauss(0, 0.0002))),
                        "qty": "{:.6f}".format(abs(rng.gauss(0.4, 0.2))),
                        "quoteQty": "{:.2f}".format(abs(rng.gauss(120, 60))),
                        "time": now - i * 2500,
                        "isBuyerMaker": rng.random() < 0.48} for i in range(40)])
        # anything signed is unavailable on purpose
        return err(-2015, "demo transport: signed endpoints are disabled", 401)

    def _symbol_row(self, symbol: str) -> Dict[str, Any]:
        price = SYMBOLS[symbol]
        tick = "0.01" if price > 100 else "0.0001"
        step = "0.00001000" if price > 100 else "0.00100000"
        return {
            "symbol": symbol, "baseAsset": symbol[:-4], "quoteAsset": "USDT",
            "status": "TRADING", "isSpotTradingAllowed": True,
            "baseAssetPrecision": 8, "quoteAssetPrecision": 8,
            "permissions": ["SPOT"],
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": step, "minQty": step,
                 "maxQty": "9000.00000000"},
                {"filterType": "PRICE_FILTER", "tickSize": tick},
                {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
            ],
        }

    def health(self) -> Dict[str, Any]:
        return {"demo": True, "calls": self.calls,
                "note": "synthetic data, no exchange connection"}
