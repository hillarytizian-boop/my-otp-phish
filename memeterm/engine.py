"""
memeterm.engine - the trading terminal's brain, independent of the browser.

The server is a thin adapter over this class; the tests exercise the engine
directly, with mocked exchange responses, so none of the risk logic depends on
HTTP or on JavaScript.

Modes
-----
DISCONNECTED  public market data only; order buttons are refused server-side.
PAPER         simulated fills against the live tape (default after install).
LIVE_SPOT     real Spot orders, only after an explicit, in-session activation.

Live mode is never restored on restart. It is also dropped whenever the API key
is deleted, the connection test fails, or the emergency stop is engaged.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .agents import REGISTRY, AgentRunner, MarketContext
from .backtest import Backtester
from .binance import (BinanceClient, BinanceError, SymbolFilters, UrllibTransport)
from .config import TerminalConfig
from .execution import ExecutionResult, LiveBroker, OrderManager, PaperBroker
from .market import MarketService
from .notify import TelegramNotifier
from .risk import MODE_DISCONNECTED, MODE_LIVE, MODE_PAPER, RiskEngine
from .secrets_store import CredentialError, SecretsStore
from .store import OrderRecord, StateStore
from .util import iso_age, redact, safe_float, utc_now, utc_str

LIVE_CONFIRMATION = "I ACCEPT THE RISK"


class EngineError(RuntimeError):
    """A refusal with a message that is safe to show the user."""


class TradingEngine:
    def __init__(self, cfg: TerminalConfig, *, client: Optional[BinanceClient] = None,
                 store: Optional[StateStore] = None,
                 secrets: Optional[SecretsStore] = None,
                 notifier: Optional[TelegramNotifier] = None,
                 transport: Optional[Any] = None,
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg.validate()
        self.cfg.prepare()
        self.clock = clock
        self.store = store or StateStore(cfg.resolved_dir(), quote_asset=cfg.quote_asset)
        self.store.load()
        self.secrets = secrets or SecretsStore(cfg.resolved_dir())
        self.notifier = notifier if notifier is not None else TelegramNotifier()
        self.transport = transport or UrllibTransport(timeout=cfg.http_timeout_s)
        self.client = client or self._build_client()
        self.market = MarketService(self.client, clock=clock)
        self.risk = RiskEngine(cfg)
        self.paper = PaperBroker(cfg, self.store)
        self.live: Optional[LiveBroker] = None
        self.agents = AgentRunner(max_workers=cfg.agents_max_workers,
                                  timeout_s=cfg.agents_timeout_s)
        self.backtester = Backtester(cfg.resolved_dir(), fee_bps=cfg.taker_fee_bps,
                                     slippage_bps=cfg.slippage_bps)
        self.orders = OrderManager(
            cfg, self.store, self.risk, self.paper, self.live,
            mode_provider=lambda: self.mode,
            balance_provider=self.balance_view,
            price_provider=self.market.price,
            filters_provider=self.market.filters,
            notifier=self.notifier,
        )

        # PAPER is the default after every start, with or without API keys.
        self.mode = MODE_PAPER
        self.live_active = False
        self.last_test: Dict[str, Any] = {}
        self.last_consensus: Dict[str, Any] = {}
        self.last_consensus_at = 0.0
        self.last_error = ""
        self.active_symbol = (cfg.watchlist or ["BTCUSDT"])[0]
        self.symbols: List[str] = list(cfg.watchlist)
        self.started_at = utc_now()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._last_agents_at = 0.0
        self._last_equity = 0.0
        self._live_synced_at = 0.0
        self._account_cache: Dict[str, Any] = {}
        self._daily_checked = ""
        self._bootstrap_state()

    # ------------------------------------------------------------------
    def _build_client(self) -> BinanceClient:
        creds = self.secrets.get()
        return BinanceClient(
            transport=self.transport,
            base_url=self.cfg.api_base,
            credentials_provider=(lambda: self._credentials_tuple()) if creds else None,
            timeout=self.cfg.http_timeout_s,
            max_retries=self.cfg.max_retries,
            recv_window_ms=self.cfg.recv_window_ms,
            clock=self.clock,
        )

    def _credentials_tuple(self) -> tuple:
        creds = self.secrets.get()
        if not creds:
            raise BinanceError("no API credentials configured", kind="auth")
        return creds.api_key, creds.api_secret

    def _bootstrap_state(self) -> None:
        state = self.store.state
        if state.paper_quote <= 0 and not state.positions and state.realized_pnl == 0:
            state.paper_quote = self.cfg.paper_start_balance_usdt
        if not state.day_key:
            state.day_key = time.strftime("%Y-%m-%d", time.gmtime())
            state.day_start_equity = self.equity()
        if state.emergency_stop:
            self.live_active = False
        self.store.save()
        self.store.audit("engine_start", mode=self.mode, paper_quote=state.paper_quote,
                         positions=len(state.positions), emergency=state.emergency_stop)

    # ------------------------------------------------------------------
    # credentials
    # ------------------------------------------------------------------
    def save_credentials(self, api_key: str, api_secret: str,
                         *, source: str = "browser") -> Dict[str, Any]:
        try:
            self.secrets.set(api_key, api_secret)
        except CredentialError as exc:
            raise EngineError(str(exc))
        # Rebuild the client so the signer picks up the new key.
        self.client = self._build_client()
        self.market.client = self.client
        self.store.audit("credentials_saved", source=source,
                         fingerprint=self.secrets.status()["fingerprint"])
        if self.mode == MODE_DISCONNECTED:
            self.mode = MODE_PAPER
        self.live_active = False
        self.last_test = {}
        return self.secrets.status()

    def delete_credentials(self) -> Dict[str, Any]:
        removed = self.secrets.delete()
        self.live_active = False
        self.mode = MODE_DISCONNECTED
        self.client = self._build_client()
        self.market.client = self.client
        self.store.audit("credentials_deleted", removed=removed)
        return {"removed": removed, "mode": self.mode}

    def test_connection(self) -> Dict[str, Any]:
        result = self.client.connection_test()
        self.last_test = {"at": utc_now(), "ok": bool(result.get("ok")),
                          "checks": result.get("checks", []),
                          "warnings": result.get("warnings", []),
                          "permissions": result.get("permissions") or {},
                          "taker_fee_bps": result.get("taker_fee_bps")}
        self.store.audit("connection_test", ok=result.get("ok"),
                         warnings="; ".join(result.get("warnings") or [])[:200])
        return result

    # ------------------------------------------------------------------
    # modes
    # ------------------------------------------------------------------
    def set_mode(self, mode: str) -> Dict[str, Any]:
        mode = (mode or "").upper()
        if mode == MODE_DISCONNECTED:
            self.mode = MODE_DISCONNECTED
            self.live_active = False
            self.store.audit("mode_change", mode=mode, reason="user")
            return {"mode": self.mode, "live_active": False}
        if mode == MODE_PAPER:
            self.mode = MODE_PAPER
            self.live_active = False
            self.store.audit("mode_change", mode=mode, reason="user")
            return {"mode": self.mode, "live_active": False}
        if mode == MODE_LIVE:
            raise EngineError("live trading needs an explicit activation (see /api/live/activate)")
        raise EngineError("unknown mode {!r}".format(mode))

    def live_preflight(self) -> Dict[str, Any]:
        creds = self.secrets.get()
        checks: List[Dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str = "") -> None:
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        add("credentials stored", bool(creds),
            "API key present" if creds else "no API key")
        add("connection tested", bool(self.last_test.get("ok")),
            "run the connection test first" if not self.last_test else
            "last test {}".format(iso_age(self.last_test.get("at"))))
        perms = (self.last_test.get("permissions") or {})
        add("spot permission", "SPOT" in (perms.get("permissions") or [])
            or perms.get("can_trade") is True, str(perms.get("permissions") or "unknown"))
        add("trading enabled", perms.get("can_trade") is not False,
            "canTrade={}".format(perms.get("can_trade")))
        add("withdrawals disabled", perms.get("can_withdraw") is not True,
            "the key must NOT have withdrawal permission")
        add("emergency stop clear", not self.store.state.emergency_stop,
            "emergency stop is engaged" if self.store.state.emergency_stop else "clear")
        filters_ok = True
        for symbol in self.symbols[:5]:
            try:
                self.client.filters(symbol)
            except BinanceError:
                filters_ok = False
                break
        add("symbol filters loaded", filters_ok, "exchangeInfo reachable")
        return {"ok": all(c["ok"] for c in checks), "checks": checks,
                "limits": {"max_order_usdt": self.cfg.max_order_usdt,
                           "max_exposure_usdt": self.cfg.max_exposure_usdt,
                           "daily_loss_limit_usdt": self.cfg.daily_loss_limit_usdt,
                           "max_positions": self.cfg.max_positions},
                "balance": self.balance_view()}

    def activate_live(self, confirmation: str, *, acknowledge: bool = False) -> Dict[str, Any]:
        if (confirmation or "").strip().upper() != LIVE_CONFIRMATION:
            raise EngineError('type exactly "{}" to activate live trading'.format(
                LIVE_CONFIRMATION))
        if not acknowledge:
            raise EngineError("the live-trading acknowledgement checkbox is required")
        preflight = self.live_preflight()
        if not preflight["ok"]:
            bad = [c for c in preflight["checks"] if not c["ok"]]
            raise EngineError("live preflight failed: {}".format(
                "; ".join("{} ({})".format(c["name"], c["detail"]) for c in bad)))
        self.live = LiveBroker(self.cfg, self.store, self.client)
        self.orders.live = self.live
        self.mode = MODE_LIVE
        self.live_active = True
        self.store.audit("live_activated", confirmation=True,
                         balance=preflight.get("balance"), limits=preflight.get("limits"))
        self.notifier.send("[LIVE] live Spot trading activated on this session.\n"
                           "limits: {:.2f} USDT/order, {:.2f} USDT exposure, "
                           "{:.2f} USDT daily loss".format(
                               self.cfg.max_order_usdt, self.cfg.max_exposure_usdt,
                               self.cfg.daily_loss_limit_usdt),
                           key="live:activated", cooldown_s=60)
        return {"mode": self.mode, "live_active": True, "preflight": preflight}

    def deactivate_live(self, reason: str = "user") -> Dict[str, Any]:
        self.live_active = False
        if self.mode == MODE_LIVE:
            self.mode = MODE_PAPER
        self.store.audit("live_deactivated", reason=reason)
        return {"mode": self.mode, "live_active": False}

    # ------------------------------------------------------------------
    # risk / emergency
    # ------------------------------------------------------------------
    def emergency_stop(self, reason: str = "user requested") -> Dict[str, Any]:
        state = self.store.state
        state.emergency_stop = True
        state.emergency_reason = reason[:200]
        self.store.save()
        self.deactivate_live("emergency stop")
        canceled: List[Dict[str, Any]] = []
        if self.live is not None:
            for result in self.live.cancel_all_open():
                canceled.append({"order": getattr(result.order, "client_order_id", ""),
                                 "status": result.status})
        for order in self.store.open_orders():
            if order.mode == MODE_PAPER:
                order.status = "CANCELED"
                order.updated_ts = utc_now()
        self.store.save()
        self.store.audit("emergency_stop", reason=reason, canceled=len(canceled))
        self.notifier.send("[EMERGENCY STOP] new entries are blocked.\n"
                           "{} open order(s) cancelled.\nExisting positions are NOT "
                           "sold automatically - review and close them manually."
                           .format(len(canceled) + len([o for o in self.store.state.orders
                                                        if o.status == "CANCELED"])),
                           key="emergency", cooldown_s=10)
        return {"emergency_stop": True, "canceled": canceled,
                "policy": ("New entries blocked, open orders cancelled. Open positions are "
                           "kept and must be closed by you; nothing is market-sold "
                           "automatically because that would realise losses at an unknown "
                           "price.")}

    def clear_emergency_stop(self, *, acknowledge: bool = False) -> Dict[str, Any]:
        if not acknowledge:
            raise EngineError("acknowledge the reset to clear the emergency stop")
        self.store.state.emergency_stop = False
        self.store.state.emergency_reason = ""
        self.store.save()
        self.store.audit("emergency_stop_cleared")
        return {"emergency_stop": False}

    # ------------------------------------------------------------------
    # market loop
    # ------------------------------------------------------------------
    def refresh_market(self) -> Dict[str, Any]:
        result = self.market.refresh_tickers(self.symbols)
        if result.get("ok"):
            for symbol in self.symbols:
                price = self.market.price(symbol)
                if price:
                    self.store.state.last_price[symbol] = price
            self._maybe_roll_day()
            self.store.save()
        else:
            self.last_error = result.get("error", "")
        return result

    def _maybe_roll_day(self) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime(self.clock()))
        if self.store.roll_day_if_needed(day, self.equity()):
            self.store.audit("day_rollover", day=day, equity=self.equity())

    def run_agents(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        symbol = (symbol or self.active_symbol).upper()
        snapshot = self.market.snapshot(
            symbol, interval=self.cfg.kline_interval,
            limit=240 if self.cfg.kline_interval != "1m" else 300)
        ctx = MarketContext(
            symbol=symbol,
            candles=snapshot["candles"],
            ticker=snapshot["ticker"],
            book=snapshot["book"],
            trades=snapshot["trades"],
            filters=snapshot["filters"],
            cross={"BTCUSDT": snapshot["btc"]},
            portfolio=self.portfolio_view(symbol),
            backtest=self.backtest_view(),
            data_age_s=snapshot["data_age_s"],
            now=self.clock(),
            quote_asset=self.cfg.quote_asset,
        )
        findings = self.agents.run_sync(ctx)
        consensus = self.agents.consensus(findings)
        payload = consensus.as_dict(include_findings=True)
        payload["symbol"] = symbol
        payload["data_age_s"] = round(snapshot["data_age_s"], 1)
        self.last_consensus = payload
        self.last_consensus_at = self.clock()
        # Only CONFIRMED-looking consensus becomes a notification, never an order.
        if payload["verdict"] in ("BUY", "SELL") and payload["confidence"] >= 0.5:
            self.notifier.signal(symbol=symbol, verdict=payload["verdict"],
                                 score=payload["score"],
                                 price=self.market.price(symbol),
                                 detail="agents: {}/{} ok, {} veto(es)".format(
                                     payload["agents_ok"], payload["agents_total"],
                                     len(payload["vetoes"])))
        return payload

    def backtest_view(self) -> Dict[str, Any]:
        latest = self.backtester.load_latest()
        if not latest:
            return {}
        try:
            import json
            with open(latest["path"], "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError, KeyError):
            return {}
        report = payload.get("report") or {}
        return {"model": {"version": payload.get("version"),
                          "trained_at": payload.get("trained_at"),
                          "trained_utc": payload.get("trained_utc")},
                "metrics": report.get("metrics") or {},
                "report": report}

    # ------------------------------------------------------------------
    # views
    # ------------------------------------------------------------------
    def equity(self) -> float:
        state = self.store.state
        if self.mode == MODE_LIVE and self._account_cache:
            quote = safe_float((self._account_cache.get("balances") or {}).get("quote"))
            return quote + self._positions_value()
        return state.paper_quote + self._positions_value()

    def _positions_value(self) -> float:
        total = 0.0
        for pos in self.store.state.positions:
            price = self.market.price(pos.symbol) or self.store.state.last_price.get(
                pos.symbol, pos.entry_price)
            total += pos.qty * price
        return total

    def balance_view(self) -> Dict[str, float]:
        if self.mode == MODE_LIVE and self._account_cache.get("balances"):
            return {"quote": safe_float(self._account_cache["balances"].get("quote")),
                    "available": safe_float(self._account_cache["balances"].get("available"))}
        state = self.store.state
        return {"quote": state.paper_quote, "available": state.paper_quote}

    def portfolio_view(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        state = self.store.state
        prices = {s: self.market.price(s) for s in self.symbols}
        positions = []
        unrealized = 0.0
        for pos in state.positions:
            mark = prices.get(pos.symbol) or state.last_price.get(pos.symbol, pos.entry_price)
            pnl = pos.unrealized_pnl(mark, self.cfg.taker_fee_bps)
            unrealized += pnl
            positions.append({
                "position_id": pos.position_id, "symbol": pos.symbol, "qty": pos.qty,
                "entry_price": pos.entry_price, "mark_price": mark,
                "quote_spent": pos.quote_spent, "fee_paid": pos.fee_paid,
                "unrealized_pnl": round(pnl, 6),
                "unrealized_pct": round(pnl / pos.cost_basis * 100, 3) if pos.cost_basis else 0.0,
                "mode": pos.mode, "opened_utc": utc_str(pos.entry_ts),
                "value": round(pos.qty * mark, 6),
            })
        exposure = sum(row["value"] for row in positions)
        wins = state.wins
        losses = state.losses
        return {
            "equity": round(self.equity(), 4),
            "quote_balance": round(self.balance_view()["quote"], 4),
            "realized_pnl": round(state.realized_pnl, 4),
            "unrealized_pnl": round(unrealized, 4),
            "day_pnl": round(state.day_realized_pnl, 4),
            "fees_paid": round(state.total_fees, 4),
            "exposure": round(exposure, 4),
            "max_exposure": self.cfg.max_exposure_usdt,
            "open_positions": len(positions),
            "max_positions": self.cfg.max_positions,
            "max_order_usdt": self.cfg.max_order_usdt,
            "daily_loss_limit": self.cfg.daily_loss_limit_usdt,
            "daily_loss": self.risk.daily_loss_state(state.day_realized_pnl),
            "win_rate_pct": round(100.0 * wins / (wins + losses), 2) if (wins + losses) else 0.0,
            "wins": wins, "losses": losses,
            "expectancy": round((state.realized_pnl / (wins + losses)), 6)
            if (wins + losses) else None,
            "positions": positions,
            "emergency_stop": state.emergency_stop,
            "emergency_reason": state.emergency_reason,
            "symbol": symbol,
        }

    def state_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            state = self.store.state
            watchlist = self.market.watchlist_rows(self.symbols)
            orders = [o.as_dict() for o in state.orders[:40]]
            agent_state = {}
            for agent in REGISTRY:
                agent_state[agent.agent_id] = self.agents.last.get(agent.agent_id, {
                    "agent_id": agent.agent_id, "state": "idle", "vote": "neutral",
                    "score": 0.0, "confidence": 0.0, "role": agent.role,
                    "team": agent.team, "reason_codes": [], "latency_ms": 0.0,
                })
            return {
                "mode": self.mode,
                "live_active": bool(self.live_active),
                "demo": bool(getattr(self.cfg, "demo", False)),
                "connection": self.connection_view(),
                "portfolio": self.portfolio_view(),
                "watchlist": watchlist,
                "active_symbol": self.active_symbol,
                "kline_interval": self.cfg.kline_interval,
                "open_orders": [o for o in orders if o["status"] in
                                ("NEW", "PARTIALLY_FILLED", "UNKNOWN", "PENDING")],
                "recent_orders": orders,
                "recent_fills": self.store.read_ledger(limit=25)[::-1],
                "agents": {
                    "consensus": self.last_consensus,
                    "age_s": round(self.clock() - self.last_consensus_at, 1)
                    if self.last_consensus_at else None,
                    "registry": [a.as_dict() for a in REGISTRY],
                    "states": agent_state,
                },
                "risk": {
                    "limits": {"max_order_usdt": self.cfg.max_order_usdt,
                               "max_exposure_usdt": self.cfg.max_exposure_usdt,
                               "per_symbol_exposure_usdt": self.cfg.per_symbol_exposure_usdt,
                               "daily_loss_limit_usdt": self.cfg.daily_loss_limit_usdt,
                               "max_positions": self.cfg.max_positions},
                    "daily_loss": self.risk.daily_loss_state(state.day_realized_pnl),
                    "emergency_stop": state.emergency_stop,
                    "emergency_reason": state.emergency_reason,
                },
                "health": {
                    "market": self.market.health(),
                    "telegram": self.notifier.status(),
                    "last_error": redact(self.last_error)[:200],
                    "uptime_s": round(self.clock() - self.started_at, 1),
                    "last_update_utc": utc_str(),
                },
                "config": self.public_config(),
                "generated_at": self.clock(),
                "generated_at_utc": utc_str(),
            }

    def connection_view(self) -> Dict[str, Any]:
        creds = self.secrets.status(self.last_test)
        creds["live_available"] = bool(creds.get("connected"))
        creds["last_test"] = self.last_test
        return creds

    def public_config(self) -> Dict[str, Any]:
        payload = self.cfg.to_public_dict()
        payload["live_confirmation_phrase"] = LIVE_CONFIRMATION
        return payload

    # ------------------------------------------------------------------
    # orders (delegated)
    # ------------------------------------------------------------------
    def place_order(self, *, symbol: str, side: str, order_type: str,
                    qty: Optional[float] = None, price: Optional[float] = None,
                    quote_amount: Optional[float] = None,
                    confirm: bool = False) -> ExecutionResult:
        if self.mode == MODE_DISCONNECTED:
            return ExecutionResult(False, "MODE_DISCONNECTED",
                                   "disconnected: public data only")
        if self.store.state.emergency_stop:
            return ExecutionResult(False, "EMERGENCY_STOP",
                                   "emergency stop is engaged")
        return self.orders.submit(symbol=symbol, side=side, order_type=order_type,
                                  qty=qty, price=price, quote_amount=quote_amount,
                                  confirm=confirm)

    def close_position(self, position_id: str, *, confirm: bool = False) -> ExecutionResult:
        pos = next((p for p in self.store.state.positions
                    if p.position_id == position_id), None)
        if pos is None:
            return ExecutionResult(False, "NOT_FOUND", "no such position")
        return self.place_order(symbol=pos.symbol, side="SELL", order_type="MARKET",
                                qty=pos.qty, confirm=confirm)

    def update_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        merged = self.cfg.apply_updates(payload)
        self.cfg = merged
        merged.save()
        self.risk.cfg = merged
        self.orders.cfg = merged
        self.paper.cfg = merged
        if self.live is not None:
            self.live.cfg = merged
        self.backtester.fee_bps = merged.taker_fee_bps
        self.backtester.slippage_bps = merged.slippage_bps
        if merged.watchlist:
            self.symbols = list(merged.watchlist)
            if self.active_symbol not in self.symbols:
                self.active_symbol = self.symbols[0]
        self.store.audit("settings_updated", changed=list((payload or {}).keys()))
        self.refresh_market()
        return self.public_config()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def tick(self) -> Dict[str, Any]:
        """One pass of the background loop. Exposed for tests."""
        out: Dict[str, Any] = {"market": None, "agents": None, "fills": []}
        out["market"] = self.refresh_market()
        try:
            out["fills"] = [r.as_dict() for r in
                            self.paper.poll_limits({s: self.market.price(s)
                                                    for s in self.symbols})]
        except Exception as exc:                      # pragma: no cover - defensive
            self.last_error = "paper poll: {}".format(exc)
        now = self.clock()
        if (now - self._last_agents_at) >= self.cfg.agents_refresh_s:
            self._last_agents_at = now
            try:
                out["agents"] = self.run_agents()
            except Exception as exc:
                self.last_error = "agents: {}".format(exc)
        if self.live_active and self.live is not None and (now - self._live_synced_at) > 45:
            self._live_synced_at = now
            try:
                self.live.reconcile()
                self._sync_account()
            except BinanceError as exc:
                self.last_error = "live sync: {}".format(exc)
        self._last_equity = self.equity()
        return out

    def _sync_account(self) -> None:
        try:
            balances = self.client.balances([self.cfg.quote_asset])
            quote = next((b for b in balances if b["asset"] == self.cfg.quote_asset), None)
            self._account_cache = {"balances": {
                "quote": (quote or {}).get("total", 0.0),
                "available": (quote or {}).get("free", 0.0)},
                "at": self.clock()}
        except BinanceError as exc:
            self.last_error = "account: {}".format(exc)

    def start_background(self, interval: Optional[float] = None) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                started = time.time()
                try:
                    self.tick()
                except Exception as exc:              # keep the terminal alive
                    self.last_error = "loop: {}".format(exc)
                elapsed = time.time() - started
                self._stop.wait(max(0.2, (interval or self.cfg.market_refresh_s) - elapsed))

        self._thread = threading.Thread(target=loop, name="engine-loop", daemon=True)
        self._thread.start()

    def stop_background(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def shutdown(self) -> None:
        """Ctrl+C path: stop the loop, save state, keep positions (documented)."""
        self.stop_background()
        self.store.audit("engine_shutdown", mode=self.mode,
                         positions=len(self.store.state.positions))
        self.store.save()
