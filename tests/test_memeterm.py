"""
Offline test suite for the Meme Agents terminal.

Every exchange interaction is mocked: `FakeTransport` answers Binance REST
calls from canned JSON. No test opens a socket to Binance, and no test can
place a real order. The only real socket is the local HTTP server bound to
127.0.0.1 on an ephemeral port, used to test the security rules end to end.

Run:  python meme_terminal.py selftest
   or python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from memeterm.agents import REGISTRY, TEAMS, AgentRunner, MarketContext          # noqa: E402
from memeterm.backtest import (build_dataset, chronological_split, collect_candles,  # noqa: E402
                               features_at, baseline_signals, simulate, train_logistic)
from memeterm.binance import (AmbiguousOrderError, AuthError, BinanceClient,     # noqa: E402
                              BinanceError, NetworkError, Response, SymbolFilters)
from memeterm.config import ConfigError, TerminalConfig                          # noqa: E402
from memeterm.engine import LIVE_CONFIRMATION, EngineError, TradingEngine        # noqa: E402
from memeterm.execution import OrderManager, PaperBroker                         # noqa: E402
from memeterm.notify import TelegramNotifier                                     # noqa: E402
from memeterm.risk import (MODE_DISCONNECTED, MODE_LIVE, MODE_PAPER,             # noqa: E402
                           RiskEngine)
from memeterm.secrets_store import CredentialError, SecretsStore                 # noqa: E402
from memeterm.server import TerminalServer                                       # noqa: E402
from memeterm.universe import describe_selection, select_symbols                 # noqa: E402
from memeterm.store import StateStore                                            # noqa: E402
from memeterm.util import (fingerprint, mask_key, parse_day_ms, redact)           # noqa: E402

API_KEY = "K" * 64
API_SECRET = "S" * 64
SYMBOL = "BTCUSDT"


# ---------------------------------------------------------------------------
# fake exchange
# ---------------------------------------------------------------------------
class FakeTransport:
    """Canned Binance Spot responses with call recording."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self.orders: Dict[str, Dict[str, Any]] = {}
        self.fail_next: List[str] = []          # 'timeout' | 'network' | status names
        self.ambiguous_next = False
        self.reject_next: Optional[Dict[str, Any]] = None
        self.price = 60_000.0
        self.balance = 1_000.0
        self.withdraw_permission = False
        self.latency = 0.0
        now_ms = int(time.time() * 1000)
        self.candles = []
        for i in range(300):
            base = 60_000.0 * (1 + 0.0004 * i)
            self.candles.append([
                now_ms - (300 - i) * 60_000, str(base), str(base * 1.002),
                str(base * 0.998), str(base * 1.001), "12.5",
                now_ms - (300 - i) * 60_000 + 59_999, "750000.0", 120, "7.1",
                "426000.0", "0"])

    # -- helpers -------------------------------------------------------
    def _json(self, payload: Any, status: int = 200,
              headers: Optional[Dict[str, str]] = None) -> Response:
        return Response(status, {k.lower(): v for k, v in (headers or {}).items()},
                        json.dumps(payload).encode())

    def _err(self, code: int, msg: str, status: int = 400):
        return self._json({"code": code, "msg": msg}, status)

    # -- transport -----------------------------------------------------
    def request(self, method: str, url: str, headers: Dict[str, str],
                timeout: Optional[float] = None) -> Response:
        parsed = urllib.parse.urlparse(url)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        self.calls.append({"method": method, "path": parsed.path, "query": query,
                           "headers": dict(headers), "url": url})
        if self.latency:
            time.sleep(self.latency)
        if self.fail_next:
            mode = self.fail_next.pop(0)
            if mode == "timeout":
                raise NetworkError("network error: timed out", kind="timeout")
            if mode == "network":
                raise NetworkError("network error: connection reset", kind="network")
        path = parsed.path

        if path == "/api/v3/time":
            return self._json({"serverTime": int(time.time() * 1000)})
        if path == "/api/v3/exchangeInfo":
            return self._json({"symbols": [{
                "symbol": SYMBOL, "baseAsset": "BTC", "quoteAsset": "USDT",
                "status": "TRADING", "isSpotTradingAllowed": True,
                "baseAssetPrecision": 8, "quoteAssetPrecision": 8,
                "permissions": ["SPOT"],
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.00001000",
                     "minQty": "0.00001000", "maxQty": "9000.00000000"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
                    {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
                ]}]})
        if path == "/api/v3/ticker/24hr":
            return self._json([{"symbol": SYMBOL, "lastPrice": str(self.price),
                                "priceChangePercent": "2.35",
                                "quoteVolume": "125000000.0",
                                "highPrice": str(self.price * 1.05),
                                "lowPrice": str(self.price * 0.95)}])
        if path == "/api/v3/ticker/bookTicker":
            return self._json([{"symbol": SYMBOL, "bidPrice": str(self.price * 0.9999),
                                "askPrice": str(self.price * 1.0001)}])
        if path == "/api/v3/klines":
            limit = int(query.get("limit", 300))
            return self._json(self.candles[-limit:])
        if path == "/api/v3/depth":
            return self._json({
                "bids": [[str(self.price * (1 - 0.0001 * i)), "1.5"] for i in range(1, 21)],
                "asks": [[str(self.price * (1 + 0.0001 * i)), "1.5"] for i in range(1, 21)],
            })
        if path == "/api/v3/trades":
            return self._json([{"price": str(self.price), "qty": "0.002",
                                "quoteQty": "120.0", "time": int(time.time() * 1000) - i * 3000,
                                "isBuyerMaker": bool(i % 2)} for i in range(30)])
        if path == "/api/v3/account":
            if not headers.get("X-MBX-APIKEY"):
                return self._err(-2015, "Invalid API-key, IP, or permissions", 401)
            return self._json({
                "accountType": "SPOT", "permissions": ["SPOT"],
                "canTrade": True, "canDeposit": True,
                "canWithdraw": self.withdraw_permission,
                "commissionRates": {"maker": "0.00100000", "taker": "0.00100000"},
                "balances": [{"asset": "USDT", "free": str(self.balance),
                              "locked": "0.0"},
                             {"asset": "BTC", "free": "0.0", "locked": "0.0"}],
            })
        if path == "/api/v3/order" and method == "POST":
            if self.reject_next:
                payload = self.reject_next
                self.reject_next = None
                return self._json(payload, 400)
            if self.ambiguous_next:
                self.ambiguous_next = False
                cid = query.get("newClientOrderId", "")
                # Accepted, but the client never sees the response.
                self._store_order(cid, query, status="FILLED")
                raise NetworkError("network error: timed out", kind="timeout")
            cid = query.get("newClientOrderId", "auto")
            order = self._store_order(cid, query, status="FILLED")
            return self._json(order)
        if path == "/api/v3/order" and method == "GET":
            cid = query.get("origClientOrderId")
            if cid and cid in self.orders:
                return self._json(self.orders[cid])
            return self._err(-2013, "Order does not exist.")
        if path == "/api/v3/order" and method == "DELETE":
            cid = query.get("origClientOrderId")
            order = self.orders.get(cid or "", {})
            order.update({"status": "CANCELED"})
            return self._json(order)
        if path == "/api/v3/openOrders":
            return self._json([o for o in self.orders.values()
                               if o.get("status") in ("NEW", "PARTIALLY_FILLED")])
        if path == "/api/v3/myTrades":
            return self._json([])
        return self._err(-1121, "Invalid symbol.")

    def _store_order(self, cid: str, query: Dict[str, str], status: str) -> Dict[str, Any]:
        qty = float(query.get("quantity") or 0)
        quote = float(query.get("quoteOrderQty") or 0)
        if quote and not qty:
            qty = quote / self.price
        order = {
            "symbol": query.get("symbol", SYMBOL),
            "orderId": 1000 + len(self.orders),
            "clientOrderId": cid,
            "status": status, "side": query.get("side", "BUY"),
            "type": query.get("type", "MARKET"),
            "price": query.get("price", "0"),
            "origQty": str(qty), "executedQty": str(qty),
            "cummulativeQuoteQty": str(qty * self.price),
            "transactTime": int(time.time() * 1000),
            "fills": [{"price": str(self.price), "qty": str(qty),
                       "commission": str(qty * self.price * 0.001),
                       "commissionAsset": "USDT"}],
        }
        self.orders[cid] = order
        return order


class CapturingTelegram:
    def __init__(self):
        self.messages: List[str] = []
        self.sent = 0

    def send(self, text: str, **kwargs: Any) -> bool:
        self.messages.append(text)
        self.sent += 1
        return True

    def order_event(self, *a: Any, **k: Any) -> None:
        pass

    def order_rejected(self, **k: Any) -> None:
        pass

    def signal(self, **k: Any) -> None:
        pass

    def status(self) -> Dict[str, Any]:
        return {"enabled": True, "sent": self.sent, "skipped": 0, "failed": 0}


# ---------------------------------------------------------------------------
# base case
# ---------------------------------------------------------------------------
class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="memeterm-test-")
        self.transport = FakeTransport()
        self.cfg = TerminalConfig(data_dir=self.dir, watchlist=[SYMBOL],
                                  market_refresh_s=1.0, agents_refresh_s=3600.0)
        self.cfg = self.cfg.validate()
        self.cfg.save()
        self.engine = self.make_engine()

    def tearDown(self) -> None:
        try:
            self.engine.stop_background()
        except Exception:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)

    def make_engine(self, **kwargs: Any) -> TradingEngine:
        return TradingEngine(self.cfg, transport=self.transport,
                             notifier=CapturingTelegram(), **kwargs)

    def market_ready(self, engine: Optional[TradingEngine] = None) -> TradingEngine:
        engine = engine or self.engine
        result = engine.refresh_market()
        self.assertTrue(result.get("ok"), result)
        return engine

    def store_credentials(self, engine: Optional[TradingEngine] = None) -> TradingEngine:
        engine = engine or self.engine
        engine.save_credentials(API_KEY, API_SECRET)
        self.assertTrue(engine.secrets.get())
        return engine


# ---------------------------------------------------------------------------
# 1. agents
# ---------------------------------------------------------------------------
class TestAgents(BaseCase):
    def test_100_agents_in_ten_teams(self):
        self.assertEqual(len(REGISTRY), 100)
        self.assertEqual(len(TEAMS), 10)
        self.assertEqual(len({a.agent_id for a in REGISTRY}), 100)
        for team in TEAMS:
            members = [a for a in REGISTRY if a.team == team["key"]]
            self.assertEqual(len(members), 10,
                             "team {} has {} agents".format(team["key"], len(members)))
        for agent in REGISTRY:
            self.assertTrue(callable(agent.kernel))
            self.assertTrue(agent.role)
            self.assertIn(agent.team, [t["key"] for t in TEAMS])

    def test_every_agent_returns_structured_findings(self):
        ctx = self.context()
        findings = AgentRunner().run_sync(ctx)
        self.assertEqual(len(findings), 100)
        for finding in findings:
            payload = finding.as_dict()
            for key in ("agent_id", "team", "role", "vote", "score", "confidence",
                        "reason_codes", "evidence", "missing", "source", "timestamp"):
                self.assertIn(key, payload)
            self.assertGreaterEqual(finding.score, -1.0)
            self.assertLessEqual(finding.score, 1.0)
            self.assertGreaterEqual(finding.confidence, 0.0)
            self.assertLessEqual(finding.confidence, 1.0)
            self.assertIn(finding.vote, ("bullish", "bearish", "neutral", "veto"))
            self.assertIn(finding.state, ("ok", "no_data", "error", "timeout"))

    def context(self, *, stale: bool = False, price_scale: float = 1.0) -> MarketContext:
        self.market_ready()
        snapshot = self.engine.market.snapshot(SYMBOL)
        return MarketContext(
            symbol=SYMBOL, candles=snapshot["candles"], ticker=snapshot["ticker"],
            book=snapshot["book"], trades=snapshot["trades"],
            filters=snapshot["filters"], cross={"BTCUSDT": snapshot["btc"]},
            portfolio={"max_exposure": 20, "open_positions": 0, "day_pnl": 0.0,
                       "max_positions": 3, "emergency_stop": False,
                       "max_order_usdt": 10},
            data_age_s=600.0 if stale else 2.0, now=time.time())

    def test_stale_feed_vetoes(self):
        findings = AgentRunner().run_sync(self.context(stale=True))
        vetoes = [f for f in findings if f.vote == "veto"]
        self.assertTrue(vetoes, "a 10-minute-old feed must produce at least one veto")
        self.assertTrue(any("DATA_STALE" in f.reason_codes for f in findings))

    def test_data_integrity_veto_beats_bullish_consensus(self):
        ctx = self.context()
        ctx.candles = list(ctx.candles)
        ctx.candles[-1] = dict(ctx.candles[-1])
        ctx.candles[-1]["ts"] = ctx.candles[-2]["ts"]       # duplicate timestamp
        runner = AgentRunner()
        consensus = runner.consensus(runner.run_sync(ctx))
        self.assertEqual(consensus.verdict, "REJECT")
        self.assertTrue(consensus.vetoes)

    def test_missing_order_book_is_reported_not_invented(self):
        ctx = self.context()
        ctx.book = {}
        findings = AgentRunner().run_sync(ctx)
        book_agents = [f for f in findings if "order_book" in f.missing]
        self.assertTrue(book_agents, "agents must report missing book data")
        for finding in book_agents:
            self.assertEqual(finding.confidence, 0.0)
            self.assertEqual(finding.vote, "neutral")


# ---------------------------------------------------------------------------
# 2. symbol filters / precision / fees
# ---------------------------------------------------------------------------
class TestFiltersAndFees(BaseCase):
    def test_filters_parse_and_floor(self):
        filters = self.engine.client.filters(SYMBOL)
        self.assertEqual(filters.step_size, Decimal("0.00001000"))
        self.assertEqual(filters.tick_size, Decimal("0.01000000"))
        self.assertEqual(filters.round_qty(0.00199999), Decimal("0.00199"))
        self.assertEqual(filters.round_price(60000.019), Decimal("60000.01"))
        # rounding must never round a quantity up
        self.assertLessEqual(filters.round_qty(0.000019999), Decimal("0.00001"))

    def test_filter_validation_rejects_bad_orders(self):
        filters = self.engine.client.filters(SYMBOL)
        self.assertEqual(filters.validate("BUY", "MARKET", 0.001, None, None, 60_000), [])
        problems = filters.validate("BUY", "MARKET", 0.000001, None, None, 60_000)
        self.assertTrue(problems)
        problems = filters.validate("BUY", "LIMIT", 0.001, None, None, 60_000)
        self.assertTrue(any("LIMIT" in p for p in problems))
        problems = filters.validate("HOLD", "MARKET", 0.001, None, None, 60_000)
        self.assertTrue(any("side" in p for p in problems))

    def test_fee_and_slippage_estimate(self):
        risk = RiskEngine(self.cfg)
        costs = risk.estimate_costs(10.0)
        self.assertAlmostEqual(costs["fee"], 10.0 * 10 / 10_000, places=6)
        self.assertAlmostEqual(costs["slippage"], 10.0 * 10 / 10_000, places=6)
        self.assertAlmostEqual(costs["total"], costs["fee"] + costs["slippage"], places=9)

    def test_symbol_filters_missing_blocks_trading(self):
        decision = self.engine.risk.evaluate(
            mode=MODE_PAPER, symbol="NOTREAL", side="BUY", order_type="MARKET",
            qty=None, price=None, quote_amount=10.0, last_price=1.0, filters=None,
            available_quote=100.0, open_positions=0, exposure_total=0.0,
            exposure_symbol=0.0, day_pnl=0.0)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "SYMBOL_UNKNOWN")


# ---------------------------------------------------------------------------
# 3. paper execution + risk limits
# ---------------------------------------------------------------------------
class TestPaperTrading(BaseCase):
    def test_paper_buy_and_sell_bookkeeping(self):
        self.market_ready()
        result = self.engine.place_order(symbol=SYMBOL, side="BUY", order_type="MARKET",
                                         quote_amount=10.0, confirm=True)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.status, "FILLED")
        # a market buy spends *at most* the requested amount: the exchange's
        # step size can leave a few cents unspent (0.00001 BTC ≈ $0.60 here)
        spent = result.qty * result.fill_price
        self.assertLessEqual(spent, 10.0 + 1e-9)
        self.assertGreater(spent, 9.0)
        # slippage makes a paper buy more expensive than the mark
        self.assertGreater(result.fill_price, self.engine.market.price(SYMBOL))
        state = self.engine.store.state
        self.assertEqual(len(state.positions), 1)
        self.assertAlmostEqual(state.paper_quote, 500.0 - spent - result.fee, places=6)

        sell = self.engine.place_order(symbol=SYMBOL, side="SELL", order_type="MARKET",
                                       qty=result.qty, confirm=True)
        self.assertTrue(sell.ok, sell.message)
        self.assertEqual(len(self.engine.store.state.positions), 0)
        self.assertLess(self.engine.store.state.realized_pnl, 0)   # fees + spread
        ledger = self.engine.store.read_ledger()
        self.assertEqual([row["event"] for row in ledger], ["FILL", "FILL"])

    def test_order_needs_confirmation(self):
        self.market_ready()
        result = self.engine.place_order(symbol=SYMBOL, side="BUY", order_type="MARKET",
                                         quote_amount=5.0, confirm=False)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "CONFIRM_REQUIRED")
        self.assertEqual(len(self.engine.store.state.orders), 0)

    def test_size_limit_rejected(self):
        self.market_ready()
        result = self.engine.place_order(symbol=SYMBOL, side="BUY", order_type="MARKET",
                                         quote_amount=25.0, confirm=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "RISK_REJECTED")
        self.assertEqual(result.risk["code"], "SIZE_WITHIN_LIMIT")

    def test_min_notional_rejected(self):
        self.market_ready()
        result = self.engine.place_order(symbol=SYMBOL, side="BUY", order_type="MARKET",
                                         quote_amount=1.0, confirm=True)
        self.assertFalse(result.ok)
        self.assertIn(result.risk["code"],
                      ("SIZE_ABOVE_EXCHANGE_MIN", "EXCHANGE_FILTER"))

    def test_insufficient_balance_rejected(self):
        self.market_ready()
        self.engine.store.state.paper_quote = 3.0
        result = self.engine.place_order(symbol=SYMBOL, side="BUY", order_type="MARKET",
                                         quote_amount=10.0, confirm=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.risk["code"], "BALANCE")

    def test_max_positions_enforced(self):
        self.market_ready()
        self.engine.store.state.positions = [_fake_position(i) for i in range(3)]
        result = self.engine.place_order(symbol=SYMBOL, side="BUY", order_type="MARKET",
                                         quote_amount=5.0, confirm=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.risk["code"], "MAX_POSITIONS")

    def test_daily_loss_limit_blocks_entries(self):
        self.market_ready()
        self.engine.store.state.day_realized_pnl = -3.5
        result = self.engine.place_order(symbol=SYMBOL, side="BUY", order_type="MARKET",
                                         quote_amount=5.0, confirm=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.risk["code"], "DAILY_LOSS")

    def test_duplicate_order_is_refused(self):
        self.market_ready()
        first = self.engine.place_order(symbol=SYMBOL, side="BUY", order_type="MARKET",
                                        quote_amount=5.0, confirm=True)
        self.assertTrue(first.ok, first.message)
        # same duplicate bucket -> the manager must refuse to reuse the id
        second = self.engine.place_order(symbol=SYMBOL, side="BUY", order_type="MARKET",
                                         quote_amount=5.0, confirm=True)
        self.assertFalse(second.ok)
        self.assertIn(second.status, ("DUPLICATE", "RISK_REJECTED"))

    def test_disconnected_mode_blocks_orders(self):
        self.market_ready()
        self.engine.set_mode(MODE_DISCONNECTED)
        result = self.engine.place_order(symbol=SYMBOL, side="BUY", order_type="MARKET",
                                         quote_amount=5.0, confirm=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "MODE_DISCONNECTED")


def _fake_position(index: int):
    from memeterm.store import Position
    return Position(position_id="p{}".format(index), symbol="ETHUSDT", qty=1.0,
                    entry_price=1.0, entry_ts=time.time())


# ---------------------------------------------------------------------------
# 4. live orders: idempotency and ambiguity
# ---------------------------------------------------------------------------
class TestLiveExecution(BaseCase):
    def activate(self) -> TradingEngine:
        engine = self.store_credentials()
        self.market_ready(engine)
        engine.test_connection()
        engine.activate_live(LIVE_CONFIRMATION, acknowledge=True)
        self.assertEqual(engine.mode, MODE_LIVE)
        return engine

    def test_live_requires_exact_confirmation(self):
        engine = self.store_credentials()
        self.market_ready(engine)
        engine.test_connection()
        with self.assertRaises(EngineError):
            engine.activate_live("yes", acknowledge=True)
        with self.assertRaises(EngineError):
            engine.activate_live(LIVE_CONFIRMATION, acknowledge=False)
        self.assertNotEqual(engine.mode, MODE_LIVE)

    def test_live_activation_requires_passing_preflight(self):
        engine = self.store_credentials()
        self.market_ready(engine)
        # no connection test yet -> preflight fails
        with self.assertRaises(EngineError) as caught:
            engine.activate_live(LIVE_CONFIRMATION, acknowledge=True)
        self.assertIn("preflight", str(caught.exception))

        # a key with withdrawal permission is refused
        self.transport.withdraw_permission = True
        engine.test_connection()
        with self.assertRaises(EngineError) as caught:
            engine.activate_live(LIVE_CONFIRMATION, acknowledge=True)
        self.assertIn("withdraw", str(caught.exception).lower())

    def test_live_order_is_sent_once_and_booked(self):
        engine = self.activate()
        result = engine.place_order(symbol=SYMBOL, side="BUY", order_type="MARKET",
                                    quote_amount=10.0, confirm=True)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.status, "FILLED")
        posts = [c for c in self.transport.calls
                 if c["method"] == "POST" and c["path"] == "/api/v3/order"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(engine.store.state.positions), 1)

    def test_ambiguous_order_is_resolved_not_retried(self):
        engine = self.activate()
        self.transport.ambiguous_next = True
        result = engine.place_order(symbol=SYMBOL, side="BUY", order_type="MARKET",
                                    quote_amount=10.0, confirm=True)
        posts = [c for c in self.transport.calls
                 if c["method"] == "POST" and c["path"] == "/api/v3/order"]
        self.assertEqual(len(posts), 1, "an ambiguous POST must never be resent")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.status, "FILLED")
        queries = [c for c in self.transport.calls
                   if c["method"] == "GET" and c["path"] == "/api/v3/order"]
        self.assertTrue(queries, "the terminal must look the order up after a timeout")
        self.assertIn("origClientOrderId", queries[-1]["query"])

    def test_ambiguous_and_missing_order_is_not_retried(self):
        engine = self.activate()
        self.transport.ambiguous_next = True
        original = self.transport._store_order

        def drop(cid: str, query: Dict[str, str], status: str) -> Dict[str, Any]:
            # simulate: exchange never received the order
            return {"symbol": SYMBOL, "clientOrderId": cid, "status": "NEW",
                    "orderId": 1, "origQty": "0", "executedQty": "0",
                    "cummulativeQuoteQty": "0", "side": "BUY", "type": "MARKET",
                    "price": "0", "transactTime": 0}

        self.transport._store_order = drop          # type: ignore[assignment]
        result = engine.place_order(symbol=SYMBOL, side="BUY", order_type="MARKET",
                                    quote_amount=10.0, confirm=True)
        posts = [c for c in self.transport.calls
                 if c["method"] == "POST" and c["path"] == "/api/v3/order"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(result.status, "UNKNOWN")
        queries = [c for c in self.transport.calls
                   if c["method"] == "GET" and c["path"] == "/api/v3/order"]
        self.assertTrue(queries, "the terminal must query the order before giving up")
        self.transport._store_order = original      # type: ignore[assignment]

    def test_exchange_rejection_is_reported_not_hidden(self):
        engine = self.activate()
        self.transport.reject_next = {"code": -2010, "msg": "insufficient balance"}
        result = engine.place_order(symbol=SYMBOL, side="BUY", order_type="MARKET",
                                    quote_amount=10.0, confirm=True)
        self.assertFalse(result.ok)
        self.assertIn("insufficient balance", result.message)
        self.assertEqual(len(engine.store.state.positions), 0)


# ---------------------------------------------------------------------------
# 5. secrets, redaction and settings
# ---------------------------------------------------------------------------
class TestSecrets(BaseCase):
    def test_secrets_are_stored_restrictively_and_never_returned(self):
        engine = self.store_credentials()
        path = engine.secrets.path
        mode = os.stat(path).st_mode & 0o777
        self.assertIn(mode, (0o600, 0o640, 0o644, 0o660))
        status = engine.secrets.status()
        self.assertTrue(status["connected"])
        self.assertNotIn(API_KEY, json.dumps(status))
        self.assertNotIn(API_SECRET, json.dumps(status))
        self.assertEqual(status["fingerprint"], fingerprint(API_KEY, API_SECRET))
        self.assertEqual(status["api_key_masked"], mask_key(API_KEY))

    def test_repr_and_redaction_never_leak(self):
        engine = self.store_credentials()
        creds = engine.secrets.get()
        self.assertNotIn(API_SECRET, repr(creds))
        text = redact("api_secret=" + API_SECRET + " key=" + API_KEY)
        self.assertNotIn(API_SECRET, text)
        self.assertNotIn(API_KEY, text)

    def test_seed_phrase_is_refused(self):
        engine = self.engine
        with self.assertRaises(CredentialError):
            engine.secrets.set("word1 word2 word3 word4 word5 word6",
                               "word7 word8 word9 word10 word11 word12")
        with self.assertRaises(CredentialError):
            engine.secrets.set("short", "short")

    def test_delete_credentials_drops_live_and_mode(self):
        engine = self.store_credentials()
        self.market_ready(engine)
        engine.test_connection()
        engine.activate_live(LIVE_CONFIRMATION, acknowledge=True)
        engine.delete_credentials()
        self.assertFalse(engine.secrets.get())
        self.assertNotEqual(engine.mode, MODE_LIVE)
        self.assertFalse(engine.live_active)


# ---------------------------------------------------------------------------
# 6. emergency stop / restart
# ---------------------------------------------------------------------------
class TestSafetyState(BaseCase):
    def test_emergency_stop_blocks_orders_and_persists(self):
        self.market_ready()
        result = self.engine.emergency_stop("test")
        self.assertTrue(result["emergency_stop"])
        blocked = self.engine.place_order(symbol=SYMBOL, side="BUY", order_type="MARKET",
                                          quote_amount=5.0, confirm=True)
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.status, "EMERGENCY_STOP")

        restarted = self.make_engine()
        self.assertTrue(restarted.store.state.emergency_stop)
        self.assertEqual(restarted.mode, MODE_PAPER)

    def test_live_mode_is_never_restored_on_restart(self):
        engine = self.store_credentials()
        self.market_ready(engine)
        engine.test_connection()
        engine.activate_live(LIVE_CONFIRMATION, acknowledge=True)
        self.assertEqual(engine.mode, MODE_LIVE)
        restarted = self.make_engine()
        self.assertEqual(restarted.mode, MODE_PAPER)
        self.assertFalse(restarted.live_active)

    def test_state_survives_restart(self):
        self.market_ready()
        self.engine.place_order(symbol=SYMBOL, side="BUY", order_type="MARKET",
                                quote_amount=10.0, confirm=True)
        paper_quote = self.engine.store.state.paper_quote
        restarted = self.make_engine()
        self.assertAlmostEqual(restarted.store.state.paper_quote, paper_quote, places=6)
        self.assertEqual(len(restarted.store.state.positions), 1)

    def test_corrupt_state_file_is_quarantined(self):
        path = os.path.join(self.dir, "state.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        store = StateStore(self.dir)
        store.load()
        self.assertTrue(store.recovered_from_corrupt)
        self.assertTrue(any(name.startswith("state.json.corrupt-")
                            for name in os.listdir(self.dir)))


# ---------------------------------------------------------------------------
# 7. config validation
# ---------------------------------------------------------------------------
class TestConfig(BaseCase):
    def test_hard_ceilings(self):
        with self.assertRaises(ConfigError):
            self.cfg.copy(max_order_usdt=5000).validate()
        with self.assertRaises(ConfigError):
            self.cfg.copy(max_order_usdt=0).validate()
        with self.assertRaises(ConfigError):
            self.cfg.copy(max_positions=500).validate()
        with self.assertRaises(ConfigError):
            self.cfg.copy(max_order_usdt=30, max_exposure_usdt=20).validate()
        with self.assertRaises(ConfigError):
            self.cfg.copy(host="0.0.0.0", allow_remote=False).validate()

    def test_settings_update_is_validated_server_side(self):
        updated = self.engine.update_settings({"max_order_usdt": 7.5})
        self.assertEqual(updated["max_order_usdt"], 7.5)
        with self.assertRaises(ConfigError):
            self.engine.update_settings({"max_order_usdt": 99999})


# ---------------------------------------------------------------------------
# 8. telegram
# ---------------------------------------------------------------------------
class RecordingTransport:
    def __init__(self):
        self.calls: List[Any] = []

    def request(self, method: str, url: str, headers: Dict[str, str], payload: Any) -> None:
        self.calls.append({"method": method, "url": url, "payload": payload})


class TestTelegram(unittest.TestCase):
    def test_disabled_without_credentials(self):
        notifier = TelegramNotifier(token="", chat_id="")
        self.assertFalse(notifier.enabled)
        self.assertFalse(notifier.send("hello"))
        self.assertEqual(notifier.sent, 0)

    def test_sends_and_deduplicates(self):
        transport = RecordingTransport()
        notifier = TelegramNotifier(token="123:abc", chat_id="42", transport=transport)
        self.assertTrue(notifier.send("first", key="k", cooldown_s=60))
        self.assertFalse(notifier.send("first again", key="k", cooldown_s=60))
        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(notifier.send("different", key="other", cooldown_s=60))
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(notifier.skipped, 1)

    def test_payload_never_contains_the_bot_token(self):
        transport = RecordingTransport()
        notifier = TelegramNotifier(token="999999:TOKENVALUE", chat_id="7",
                                    transport=transport)
        notifier.send("order filled")
        body = transport.calls[0]["payload"].decode()
        self.assertNotIn("TOKENVALUE", body)
        self.assertIn("order filled", body)


# ---------------------------------------------------------------------------
# 9. backtesting
# ---------------------------------------------------------------------------
class TestUniverse(unittest.TestCase):
    """The ranker that decides which pairs `train-suite` touches."""

    @staticmethod
    def row(symbol, base, quote="USDT", status="TRADING", spot=True,
            permissions=("SPOT",)):
        return {"symbol": symbol, "baseAsset": base, "quoteAsset": quote,
                "status": status, "isSpotTradingAllowed": spot,
                "permissions": list(permissions)}

    def test_filters_and_ranks_by_quote_volume(self):
        info = {"symbols": [
            self.row("AAAUSDT", "AAA"),
            self.row("BBBUSDT", "BBB"),
            self.row("CCCUSDT", "CCC"),
            self.row("HALTUSDT", "HALT", status="BREAK"),
            self.row("MARGINUSDT", "MARGIN", spot=False),
            self.row("NOPERMUSDT", "NOPERM", permissions=("MARGIN",)),
            self.row("ETHBTC", "ETH", quote="BTC"),
            self.row("USDCUSDT", "USDC"),
            self.row("BTCUPUSDT", "BTCUP"),
            self.row("NOTICKERUSDT", "NOTICKER"),
        ]}
        tickers = [
            {"symbol": "AAAUSDT", "quoteVolume": "100", "lastPrice": "1"},
            {"symbol": "BBBUSDT", "quoteVolume": "900", "lastPrice": "2"},
            {"symbol": "CCCUSDT", "quoteVolume": "400", "lastPrice": "3"},
            {"symbol": "HALTUSDT", "quoteVolume": "9999", "lastPrice": "9"},
            {"symbol": "MARGINUSDT", "quoteVolume": "9999", "lastPrice": "9"},
            {"symbol": "NOPERMUSDT", "quoteVolume": "9999", "lastPrice": "9"},
            {"symbol": "ETHBTC", "quoteVolume": "9999", "lastPrice": "9"},
            {"symbol": "USDCUSDT", "quoteVolume": "9999", "lastPrice": "9"},
            {"symbol": "BTCUPUSDT", "quoteVolume": "9999", "lastPrice": "9"},
            {"symbol": "ZEROUSDT", "quoteVolume": "0", "lastPrice": "1"},
        ]
        result = select_symbols(info, tickers, count=10, quote="USDT")
        self.assertEqual([row["symbol"] for row in result["symbols"]],
                         ["BBBUSDT", "CCCUSDT", "AAAUSDT"])
        self.assertEqual(result["skipped"]["not trading"], 1)
        self.assertEqual(result["skipped"]["spot disabled"], 1)
        self.assertEqual(result["skipped"]["no spot permission"], 1)
        self.assertEqual(result["skipped"]["other quote asset"], 1)
        self.assertEqual(result["skipped"]["stablecoin"], 1)
        self.assertEqual(result["skipped"]["leveraged token"], 1)
        self.assertEqual(result["skipped"]["no ticker data"], 1)
        self.assertIn("USDT pairs", describe_selection(result))

    def test_count_include_and_exclude(self):
        info = {"symbols": [self.row("AAAUSDT", "AAA"), self.row("BBBUSDT", "BBB"),
                            self.row("CCCUSDT", "CCC")]}
        tickers = [{"symbol": "AAAUSDT", "quoteVolume": "300"},
                   {"symbol": "BBBUSDT", "quoteVolume": "200"},
                   {"symbol": "CCCUSDT", "quoteVolume": "100"}]
        self.assertEqual(len(select_symbols(info, tickers, count=2)["symbols"]), 2)
        only = select_symbols(info, tickers, count=10, include=["CCCUSDT"])
        self.assertEqual([row["symbol"] for row in only["symbols"]], ["CCCUSDT"])
        without = select_symbols(info, tickers, count=10, exclude=["AAAUSDT"])
        self.assertEqual([row["symbol"] for row in without["symbols"]],
                         ["BBBUSDT", "CCCUSDT"])
        self.assertEqual(without["skipped"]["excluded by request"], 1)


class TestBacktest(BaseCase):
    def candles(self, n: int = 600) -> List[Dict[str, Any]]:
        self.market_ready()
        return self.engine.market.snapshot(SYMBOL)["candles"]

    def test_features_never_look_ahead(self):
        candles = self.candles()
        for i in (60, 90, 150):
            prefix = candles[:i + 1]
            self.assertEqual(features_at(candles, i), features_at(prefix, i))

    def test_dataset_and_chronological_split(self):
        candles = self.candles()
        dataset = build_dataset(candles, horizon=3)
        rows = dataset["rows"]
        self.assertTrue(rows)
        split = chronological_split(rows, embargo=3)
        train_last = split["train"][-1]["ts"]
        val_first = split["val"][0]["ts"] if split["val"] else train_last
        self.assertLessEqual(train_last, val_first)
        if split["val"] and split["test"]:
            self.assertLessEqual(split["val"][-1]["ts"], split["test"][0]["ts"])
        self.assertTrue(all(row["ts"] for row in rows))

    def test_report_reports_sample_size_and_baseline(self):
        candles = self.candles()
        report = self.engine.backtester.run(candles, symbol=SYMBOL, interval="1m",
                                            horizon=3)
        self.assertTrue(report.get("ok"), report.get("error"))
        metrics = report["metrics"]
        for key in ("trades", "win_rate_pct", "net_return_pct", "expectancy",
                    "max_drawdown_pct", "precision", "false_positive_rate",
                    "baseline_expectancy", "sample_sufficient"):
            self.assertIn(key, metrics)
        self.assertIn("baseline", report)
        self.assertTrue(report["split"]["chronological"])
        self.assertGreater(report["split"]["embargo_bars"], 0)
        self.assertIn("honesty", report)

    def test_model_roundtrip_and_threshold(self):
        candles = self.candles()
        dataset = build_dataset(candles, horizon=3)
        split = chronological_split(dataset["rows"], embargo=3)
        model = train_logistic(split["train"], epochs=25)
        proba = model.predict_proba(split["test"][0]["features"])
        self.assertGreaterEqual(proba, 0.0)
        self.assertLessEqual(proba, 1.0)
        clone = type(model).from_dict(model.as_dict())
        self.assertAlmostEqual(clone.predict_proba(split["test"][0]["features"]), proba,
                               places=9)

    def test_simulation_charges_fees(self):
        rows = [{"index": i, "ts": i, "entry": 100.0, "label": 1,
                 "features": {}} for i in range(6)]
        signals = [True, False, False, False, False, False]
        result = simulate(rows, signals, horizon=2, size_usdt=10.0, fee_bps=10,
                          slippage_bps=10)
        self.assertEqual(result["trades"], 1)
        self.assertLess(result["net_pnl"], 0, "a flat price must lose fees")
        self.assertEqual(result["fees_paid"] > 0, True)

    def test_baseline_is_always_available(self):
        rows = [{"features": {"ret_5": 0.01, "ema_gap": 0.01, "vol_z": 1.0}}]
        self.assertEqual(baseline_signals(rows), [True])
        rows = [{"features": {"ret_5": -0.01, "ema_gap": 0.01, "vol_z": 1.0}}]
        self.assertEqual(baseline_signals(rows), [False])


    def test_date_range_selects_the_requested_window(self):
        """A start/end request returns that window, not "the newest N bars"."""

        class WindowedClient:
            def __init__(self):
                self.calls = []

            def klines(self, symbol, interval="1m", limit=200,
                       start_ms=None, end_ms=None):
                self.calls.append({"limit": limit, "end_ms": end_ms})
                step = 60_000
                anchor = end_ms if end_ms else 1_700_000_000_000
                rows = []
                for i in range(limit):
                    open_ms = anchor - i * step
                    rows.append([open_ms, "100", "101", "99", "100.5", "10",
                                 open_ms + step - 1, "1005", "5", "6", "60.3", "0"])
                return list(reversed(rows))

        end = 1_700_000_000_000
        start = end - 100 * 60_000
        client = WindowedClient()
        candles = collect_candles(client, "BTCUSDT", "1m", bars=1000,
                                  start_ms=start, end_ms=end)
        self.assertTrue(candles)
        self.assertGreaterEqual(candles[0]["ts"], start)
        self.assertLessEqual(candles[-1]["ts"], end)
        self.assertLessEqual(len(candles), 101)
        self.assertEqual(client.calls[0]["end_ms"], end)     # the range is used

    def test_date_parsing_is_strict(self):
        self.assertEqual(parse_day_ms("1970-01-02"), 86_400_000)
        self.assertIsNone(parse_day_ms(""))
        self.assertIsNone(parse_day_ms(None))
        with self.assertRaises(ValueError):
            parse_day_ms("31/01/2026")
        with self.assertRaises(ValueError):
            parse_day_ms("tomorrow")


# ---------------------------------------------------------------------------
# 10. HTTP server: security, static UI, API
# ---------------------------------------------------------------------------
class TestServer(BaseCase):
    def start(self, **kwargs: Any) -> TerminalServer:
        server = TerminalServer(self.cfg, self.engine, host="127.0.0.1", port=0,
                                quiet=True, **kwargs)
        server.start()
        self.addCleanup(server.stop)
        return server

    def request(self, server: TerminalServer, path: str, method: str = "GET",
                body: Optional[Dict[str, Any]] = None,
                headers: Optional[Dict[str, str]] = None,
                ) -> urllib.request.Request:
        url = "http://127.0.0.1:{}{}".format(server.port, path)
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        return request

    def fetch(self, server: TerminalServer, path: str, method: str = "GET",
              body: Optional[Dict[str, Any]] = None,
              headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        try:
            response = urllib.request.urlopen(
                self.request(server, path, method, body, headers), timeout=10)
            raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
        return json.loads(raw.decode())

    def bootstrap(self, server: TerminalServer) -> Dict[str, str]:
        url = "http://127.0.0.1:{}/api/bootstrap".format(server.port)
        opener = urllib.request.build_opener()
        response = urllib.request.urlopen(url, timeout=10)
        cookie = response.headers.get("Set-Cookie", "").split(";")[0]
        payload = json.loads(response.read().decode())
        server.cookie = cookie                          # type: ignore[attr-defined]
        return {"Cookie": cookie, "X-CSRF-Token": payload["csrf"]}

    def test_index_served_with_security_headers(self):
        server = self.start()
        response = urllib.request.urlopen(
            "http://127.0.0.1:{}/".format(server.port), timeout=10)
        body = response.read().decode()
        self.assertIn("Meme Agents", body)
        self.assertIn("width=device-width", body)          # mobile viewport
        self.assertIn("bottom-nav", body)                  # mobile navigation
        csp = response.headers.get("Content-Security-Policy")
        self.assertIn("default-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")

    def test_state_endpoint_has_no_secrets(self):
        self.store_credentials()
        self.market_ready()
        server = self.start()
        headers = self.bootstrap(server)
        payload = self.fetch(server, "/api/state", headers=headers)
        blob = json.dumps(payload)
        self.assertNotIn(API_KEY, blob)
        self.assertNotIn(API_SECRET, blob)
        self.assertEqual(payload["mode"], MODE_PAPER)
        self.assertTrue(payload["connection"]["connected"])
        self.assertIn("agents", payload)
        self.assertIn("risk", payload)

    def test_csrf_required_for_mutations(self):
        self.market_ready()
        server = self.start()
        headers = self.bootstrap(server)
        request = self.request(server, "/api/mode", "POST", {"mode": "PAPER"},
                               {"Cookie": headers["Cookie"]})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 403)
        payload = json.loads(caught.exception.read().decode())
        self.assertEqual(payload["code"], "CSRF")

    def test_cross_site_origin_rejected(self):
        server = self.start()
        headers = self.bootstrap(server)
        headers["Origin"] = "http://evil.example"
        request = self.request(server, "/api/emergency-stop", "POST", {},
                               headers)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 403)

    def test_unexpected_host_header_rejected(self):
        server = self.start()
        headers = self.bootstrap(server)
        headers["Host"] = "evil.example"
        request = self.request(server, "/api/state", headers=headers)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 421)

    def test_backtest_rejects_bad_date_window(self):
        server = self.start()
        headers = self.bootstrap(server)
        payload = self.fetch(server, "/api/backtest", "POST",
                             {"symbol": SYMBOL, "interval": "1m",
                              "start": "2026-02-01", "end": "2026-01-01"}, headers)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "BAD_WINDOW")
        one_sided = self.fetch(server, "/api/backtest", "POST",
                               {"symbol": SYMBOL, "start": "2026-01-01"}, headers)
        self.assertEqual(one_sided["code"], "BAD_WINDOW")
        junk = self.fetch(server, "/api/backtest", "POST",
                          {"symbol": SYMBOL, "start": "yesterday", "end": "today"},
                          headers)
        self.assertEqual(junk["code"], "BAD_DATE")
        too_wide = self.fetch(server, "/api/backtest", "POST",
                              {"symbol": SYMBOL, "start": "2020-01-01",
                               "end": "2026-01-01"}, headers)
        self.assertEqual(too_wide["code"], "BAD_WINDOW")

    def test_probe_detects_a_running_terminal(self):
        import meme_terminal as cli
        server = self.start()
        self.assertTrue(cli.probe_terminal(
            "http://127.0.0.1:{}".format(server.port)))
        self.assertFalse(cli.probe_terminal("http://127.0.0.1:1", timeout=0.3))

    def test_remote_mode_requires_access_code(self):
        server = TerminalServer(self.cfg, self.engine, host="0.0.0.0", port=0,
                                quiet=True, access_code="test-code-123")
        server.start()
        self.addCleanup(server.stop)
        self.assertTrue(server.remote)
        self.assertEqual(
            urllib.request.urlopen("http://127.0.0.1:{}/api/health".format(server.port),
                                   timeout=10).status, 200)
        state = self.request(server, "/api/state")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(state, timeout=10)
        self.assertEqual(caught.exception.code, 401)

        bad = self.request(server, "/api/session", "POST", {"access_code": "nope"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(bad, timeout=10)
        self.assertEqual(caught.exception.code, 401)

        good = self.request(server, "/api/session", "POST",
                            {"access_code": "test-code-123"})
        response = urllib.request.urlopen(good, timeout=10)
        self.assertEqual(response.status, 200)
        cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_order_endpoint_refuses_without_confirmation(self):
        self.market_ready()
        server = self.start()
        headers = self.bootstrap(server)
        payload = self.fetch(server, "/api/order", "POST",
                             {"symbol": SYMBOL, "side": "BUY", "order_type": "MARKET",
                              "quote_amount": 5.0}, headers)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "CONFIRM_REQUIRED")

    def test_live_activation_needs_phrase_over_http(self):
        server = self.start()
        headers = self.bootstrap(server)
        payload = self.fetch(server, "/api/live/activate", "POST",
                             {"confirmation": "yes", "acknowledge": True}, headers)
        self.assertEqual(payload["code"], "REFUSED")
        self.assertIn("I ACCEPT THE RISK", payload["error"])

    def test_static_traversal_blocked(self):
        server = self.start()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(
                "http://127.0.0.1:{}/../memeterm/server.py".format(server.port),
                timeout=10)
        self.assertIn(caught.exception.code, (403, 404))

    def test_disconnected_mode_is_reflected_in_state(self):
        server = self.start()
        headers = self.bootstrap(server)
        self.fetch(server, "/api/mode", "POST", {"mode": "DISCONNECTED"}, headers)
        state = self.fetch(server, "/api/state", headers=headers)
        self.assertEqual(state["mode"], MODE_DISCONNECTED)


class TestCliSurface(unittest.TestCase):
    """The CLI must expose exactly the flags the docs claim."""

    def test_backtest_accepts_date_window_and_label(self):
        import meme_terminal as cli
        parser = cli.build_parser()
        args = parser.parse_args(["backtest", "BTCUSDT", "--start", "2026-01-01",
                                  "--end", "2026-02-01", "--label", "v2"])
        self.assertEqual((args.start, args.end, args.label),
                         ("2026-01-01", "2026-02-01", "v2"))
        train = parser.parse_args(["train", "ETHUSDT", "--label", "trend-v2"])
        self.assertEqual(train.label, "trend-v2")

    def test_open_command_wiring(self):
        import meme_terminal as cli
        parser = cli.build_parser()
        for name in ("open", "readme", "dashboard"):
            args = parser.parse_args([name, "--print-only"])
            self.assertTrue(args.open_browser, name)
            self.assertTrue(args.print_only, name)
        outcome = cli.launch_browser("http://127.0.0.1:8787", print_only=True)
        self.assertFalse(outcome["opened"])
        self.assertEqual(outcome["url"], "http://127.0.0.1:8787")
        cfg = cli.TerminalConfig.load(data_dir=tempfile.mkdtemp())
        self.assertEqual(cli.terminal_url(cfg.copy(host="0.0.0.0", port=9000)),
                         "http://127.0.0.1:9000")
        self.assertEqual(cli.terminal_url(cfg.copy(host="127.0.0.1", port=8787)),
                         "http://127.0.0.1:8787")

    def test_train_suite_runs_offline_in_demo_mode(self):
        import contextlib
        import io
        import meme_terminal as cli
        data_dir = tempfile.mkdtemp()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            listed = cli.main(["train-suite", "--demo", "--bars", "500",
                               "--no-save", "--data-dir", data_dir,
                               "--list", "BTCUSDT,ETHUSDT"])
        output = buffer.getvalue()
        self.assertEqual(listed, 0, output[-800:])
        self.assertIn("training 2 symbols", output)
        self.assertIn("trained 2 of 2", output)
        self.assertIn("Live mode stays off", output)

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            ranked = cli.main(["train-suite", "--demo", "--count", "1", "--bars", "500",
                               "--no-save", "--data-dir", data_dir])
        output = buffer.getvalue()
        self.assertEqual(ranked, 0, output[-800:])
        self.assertIn("ranked by 24h quote volume", output)
        self.assertIn("trained 1 of 1", output)

    def test_globals_may_follow_the_subcommand(self):
        import meme_terminal as cli
        argv = cli.normalize_argv(["serve", "--port", "8791", "--demo"])
        self.assertIn("serve", argv)
        self.assertLess(argv.index("--port"), argv.index("serve"))
        args = cli.build_parser().parse_args(argv)
        self.assertEqual(args.port, 8791)
        self.assertTrue(args.demo)


# ---------------------------------------------------------------------------
# 11. market data behaviour
# ---------------------------------------------------------------------------
class TestMarketData(BaseCase):
    def test_snapshot_reports_age_and_sources(self):
        self.market_ready()
        snapshot = self.engine.market.snapshot(SYMBOL)
        self.assertIn("data_age_s", snapshot)
        self.assertGreaterEqual(snapshot["data_age_s"], 0)
        self.assertIn("binance:klines", snapshot["sources"])
        self.assertTrue(snapshot["candles"])

    def test_failed_refresh_keeps_previous_data(self):
        self.market_ready()
        good = self.engine.market.price(SYMBOL)
        self.transport.fail_next = ["network"] * 5
        result = self.engine.refresh_market()
        self.assertFalse(result.get("ok"))
        self.assertEqual(self.engine.market.price(SYMBOL), good)

    def test_client_retries_idempotent_get_only(self):
        first = self.engine.client.klines(SYMBOL, limit=5)
        self.assertTrue(first)
        self.transport.fail_next = ["network"]
        self.engine.client.klines(SYMBOL, limit=5)      # must succeed after one retry

    def test_rate_limit_error_is_actionable(self):
        self.transport.reject_next = None

        def limited(method, url, headers, timeout=None):
            return Response(429, {"retry-after": "30"},
                            json.dumps({"code": -1003, "msg": "Too many requests"}).encode())

        self.transport.request = limited            # type: ignore[assignment]
        with self.assertRaises(BinanceError) as caught:
            self.engine.client.ticker_24h([SYMBOL])
        self.assertIn("rate limited", str(caught.exception).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
