"""
memeterm.execution - paper fills, live orders, and the one order manager.

Invariants
----------
* Exactly one order per (symbol, side) can be in flight; the manager holds a
  per-symbol lock and a duplicate key, so an impatient double-click cannot
  become a double position.
* LIVE orders always carry a deterministic `clientOrderId`. If the HTTP
  response is lost, the manager *queries the exchange* for that id before it
  decides anything. It never re-sends.
* Paper and live share the same bookkeeping path (`_apply_fill`), so a paper
  run exercises the same arithmetic as a live one - it just cannot move money.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from .binance import (AmbiguousOrderError, BinanceClient, BinanceError, OrderResult,
                      SymbolFilters)
from .config import TerminalConfig
from .risk import MODE_LIVE, MODE_PAPER, RiskDecision, RiskEngine
from .store import OrderRecord, Position, StateStore
from .util import redact, safe_float, utc_now

FINAL_STATES = ("FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH",
                "TEST_ACCEPTED")


@dataclass
class ExecutionResult:
    ok: bool
    status: str
    message: str
    order: Optional[OrderRecord] = None
    fill_price: float = 0.0
    qty: float = 0.0
    quote: float = 0.0
    fee: float = 0.0
    ambiguous: bool = False
    risk: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok, "status": self.status, "message": self.message,
            "fill_price": self.fill_price, "qty": self.qty, "quote": self.quote,
            "fee": self.fee, "ambiguous": self.ambiguous,
            "order": self.order.as_dict() if self.order else None,
            "risk": self.risk,
        }


class Bookkeeping:
    """Shared fill accounting for both modes (fees included)."""

    def __init__(self, cfg: TerminalConfig, store: StateStore):
        self.cfg = cfg
        self.store = store

    def _position(self, symbol: str) -> Optional[Position]:
        rows = self.store.positions_for(symbol)
        return rows[0] if rows else None

    def apply_fill(self, *, symbol: str, side: str, qty: float, price: float,
                   fee: float, mode: str, order_id: str = "", ts: Optional[float] = None,
                   note: str = "") -> Dict[str, Any]:
        state = self.store.state
        ts = ts or utc_now()
        quote = qty * price
        realized = 0.0
        position_id = ""

        if side == "BUY":
            pos = self._position(symbol)
            if pos is None:
                pos = Position(position_id="pos-{}".format(int(ts * 1000)),
                               symbol=symbol, qty=0.0, entry_price=0.0, entry_ts=ts,
                               mode=mode)
                state.positions.append(pos)
            total_qty = pos.qty + qty
            if total_qty > 0:
                pos.entry_price = (pos.entry_price * pos.qty + price * qty) / total_qty
            pos.qty = total_qty
            pos.quote_spent += quote
            pos.fee_paid += fee
            pos.order_id = order_id or pos.order_id
            position_id = pos.position_id
            if mode == MODE_PAPER:
                state.paper_quote -= quote + fee
                state.paper_base[symbol] = state.paper_base.get(symbol, 0.0) + qty
        else:
            pos = self._position(symbol)
            if pos is None:
                return {"ok": False, "reason": "no open position in {}".format(symbol)}
            sell_qty = min(qty, pos.qty)
            entry = pos.entry_price
            realized = (price - entry) * sell_qty - fee
            pos.qty -= sell_qty
            pos.quote_spent = max(0.0, pos.quote_spent - entry * sell_qty)
            pos.fee_paid += fee
            position_id = pos.position_id
            if pos.qty <= 1e-12:
                state.positions = [p for p in state.positions if p is not pos]
            state.realized_pnl += realized
            state.day_realized_pnl += realized
            if realized >= 0:
                state.wins += 1
            else:
                state.losses += 1
            if mode == MODE_PAPER:
                state.paper_quote += quote - fee
                state.paper_base[symbol] = max(0.0, state.paper_base.get(symbol, 0.0) - sell_qty)

        state.total_fees += fee
        state.day_fees += fee
        state.last_price[symbol] = price
        self.store.ledger(event="FILL", mode=mode, symbol=symbol, side=side, qty=qty,
                          price=price, quote=quote, fee=fee, pnl=realized,
                          position_id=position_id, order_id=order_id, note=note)
        self.store.save()
        return {"ok": True, "realized": realized, "position_id": position_id}


class PaperBroker(Bookkeeping):
    """Simulated fills with fees and slippage. Deterministic, no randomness."""

    def __init__(self, cfg: TerminalConfig, store: StateStore):
        super().__init__(cfg, store)

    def _slip(self, side: str, price: float) -> float:
        bps = self.cfg.paper_fill_slippage_bps
        return price * (1 + bps / 10_000.0) if side == "BUY" else price * (1 - bps / 10_000.0)

    def execute(self, order: OrderRecord, mark_price: float,
                filters: Optional[SymbolFilters]) -> ExecutionResult:
        price = self._slip(order.side, mark_price)
        qty = order.qty
        if qty <= 0 and order.quote_amount > 0 and price > 0:
            qty = order.quote_amount / price
            if filters is not None:
                qty = float(filters.round_qty(qty))
        if qty <= 0 or price <= 0:
            order.status = "REJECTED"
            order.error = "no usable price or quantity for the simulated fill"
            self.store.save()
            return ExecutionResult(False, "REJECTED", order.error, order=order)
        if order.order_type == "LIMIT":
            # A resting limit order is not filled on submission.
            order.status = "NEW"
            order.price = safe_float(order.price) or price
            order.qty = qty
            self.store.save()
            return ExecutionResult(True, "NEW",
                                   "paper limit order resting at {:.8g}".format(order.price),
                                   order=order, qty=qty, fill_price=order.price)
        fee = qty * price * self.cfg.taker_fee_bps / 10_000.0
        result = self.apply_fill(symbol=order.symbol, side=order.side, qty=qty,
                                 price=price, fee=fee, mode=MODE_PAPER,
                                 order_id=order.client_order_id, note="paper")
        if not result.get("ok"):
            order.status = "REJECTED"
            order.error = str(result.get("reason") or "fill rejected")
            self.store.save()
            return ExecutionResult(False, "REJECTED", order.error, order=order)
        order.status = "FILLED"
        order.filled_qty = qty
        order.quote_qty = qty * price
        order.avg_price = price
        order.fee = fee
        order.updated_ts = utc_now()
        self.store.save()
        return ExecutionResult(True, "FILLED",
                               "paper {} {} {:.8g} @ {:.8g}".format(
                                   order.side, order.symbol, qty, price),
                               order=order, fill_price=price, qty=qty,
                               quote=qty * price, fee=fee)

    def poll_limits(self, prices: Dict[str, float]) -> List[ExecutionResult]:
        """Fill resting paper limit orders when the market crosses them."""
        out: List[ExecutionResult] = []
        for order in self.store.open_orders():
            if order.mode != MODE_PAPER or order.order_type != "LIMIT":
                continue
            mark = prices.get(order.symbol, 0.0)
            if mark <= 0:
                continue
            crossed = (order.side == "BUY" and mark <= order.price) or \
                      (order.side == "SELL" and mark >= order.price)
            if not crossed:
                continue
            fee = order.qty * order.price * self.cfg.taker_fee_bps / 10_000.0
            result = self.apply_fill(symbol=order.symbol, side=order.side, qty=order.qty,
                                     price=order.price, fee=fee, mode=MODE_PAPER,
                                     order_id=order.client_order_id, note="paper limit")
            if result.get("ok"):
                order.status = "FILLED"
                order.filled_qty = order.qty
                order.quote_qty = order.qty * order.price
                order.avg_price = order.price
                order.fee = fee
                order.updated_ts = utc_now()
                out.append(ExecutionResult(True, "FILLED",
                                           "paper limit filled at {:.8g}".format(order.price),
                                           order=order, fill_price=order.price,
                                           qty=order.qty, quote=order.quote_qty, fee=fee))
            else:
                order.status = "REJECTED"
                order.error = str(result.get("reason"))
                out.append(ExecutionResult(False, "REJECTED", order.error, order=order))
        if out:
            self.store.save()
        return out


class LiveBroker(Bookkeeping):
    """Real Spot orders. Small, deliberate, and never retried blindly."""

    def __init__(self, cfg: TerminalConfig, store: StateStore, client: BinanceClient):
        super().__init__(cfg, store)
        self.client = client
        self.resolve_attempts = 4
        self.resolve_gap_s = 0.8

    # -- helpers -------------------------------------------------------
    def _resolve(self, order: OrderRecord) -> Optional[OrderResult]:
        """After an ambiguous send: ask the exchange whether the order exists."""
        for attempt in range(self.resolve_attempts):
            try:
                return self.client.query_order(order.symbol,
                                               client_order_id=order.client_order_id)
            except BinanceError as exc:
                if exc.code == -2013:            # no such order -> not accepted
                    return None
                if attempt == self.resolve_attempts - 1:
                    return None
            time.sleep(self.resolve_gap_s * (attempt + 1))
        return None

    def _record_from(self, order: OrderRecord, result: OrderResult) -> None:
        order.exchange_order_id = result.order_id
        order.status = result.status or "UNKNOWN"
        order.filled_qty = result.executed_qty
        order.quote_qty = result.quote_qty
        order.avg_price = result.avg_price
        order.qty = result.orig_qty or order.qty
        order.updated_ts = utc_now()

    def execute(self, order: OrderRecord, mark_price: float,
                filters: Optional[SymbolFilters]) -> ExecutionResult:
        kwargs: Dict[str, Any] = {
            "symbol": order.symbol,
            "side": order.side,
            "order_type": order.order_type,
            "client_order_id": order.client_order_id,
        }
        if order.order_type == "LIMIT":
            kwargs.update({"quantity": _plain(order.qty), "price": _plain(order.price),
                           "time_in_force": "GTC"})
        else:
            # Market BUY uses quoteOrderQty so the spend is exact; SELL uses qty.
            if order.side == "BUY" and order.quote_amount > 0:
                kwargs["quote_order_qty"] = _plain(order.quote_amount)
            else:
                kwargs["quantity"] = _plain(order.qty)
            kwargs.pop("client_order_id")
            kwargs["client_order_id"] = order.client_order_id

        try:
            result = self.client.new_order(**kwargs)
        except AmbiguousOrderError as exc:
            order.status = "UNKNOWN"
            order.error = "ambiguous response - checked with the exchange"
            self.store.audit("order_ambiguous", symbol=order.symbol,
                             client_order_id=order.client_order_id, detail=str(exc))
            resolved = self._resolve(order)
            if resolved is None:
                order.status = "UNKNOWN"
                order.error = ("the exchange did not confirm this order and no matching "
                               "order was found; nothing was retried. Check open orders "
                               "before trying again.")
                self.store.save()
                return ExecutionResult(False, "UNKNOWN", order.error, order=order,
                                       ambiguous=True)
            self._record_from(order, resolved)
            self.store.save()
            return self._settle(order, resolved, note="resolved after timeout")
        except BinanceError as exc:
            order.status = "REJECTED"
            order.error = redact(str(exc))[:200]
            order.updated_ts = utc_now()
            self.store.save()
            self.store.audit("order_rejected", symbol=order.symbol, side=order.side,
                             detail=order.error)
            return ExecutionResult(False, "REJECTED", order.error, order=order)

        self._record_from(order, result)
        self.store.save()
        return self._settle(order, result, note="live")

    def _settle(self, order: OrderRecord, result: OrderResult, note: str) -> ExecutionResult:
        if result.status == "FILLED" and result.executed_qty > 0:
            price = result.avg_price or order.price
            fee = self._fee_from(result, price * result.executed_qty)
            booked = self.apply_fill(symbol=order.symbol, side=order.side,
                                     qty=result.executed_qty, price=price, fee=fee,
                                     mode=MODE_LIVE, order_id=order.client_order_id,
                                     note=note)
            order.fee = fee
            self.store.save()
            if not booked.get("ok"):
                return ExecutionResult(False, "BOOKKEEPING", str(booked.get("reason")),
                                       order=order, ambiguous=False)
            return ExecutionResult(True, "FILLED",
                                   "{} {} {:.8g} @ {:.8g}".format(
                                       order.side, order.symbol, result.executed_qty, price),
                                   order=order, fill_price=price, qty=result.executed_qty,
                                   quote=price * result.executed_qty, fee=fee)
        if result.status == "PARTIALLY_FILLED" and result.executed_qty > 0:
            price = result.avg_price or order.price
            fee = self._fee_from(result, price * result.executed_qty)
            self.apply_fill(symbol=order.symbol, side=order.side, qty=result.executed_qty,
                            price=price, fee=fee, mode=MODE_LIVE,
                            order_id=order.client_order_id, note="partial")
            order.status = "PARTIALLY_FILLED"
            self.store.save()
            return ExecutionResult(True, "PARTIALLY_FILLED",
                                   "partially filled {:.8g} @ {:.8g}".format(
                                       result.executed_qty, price),
                                   order=order, fill_price=price, qty=result.executed_qty,
                                   quote=price * result.executed_qty, fee=fee)
        return ExecutionResult(True, order.status,
                               "order {} accepted by the exchange".format(order.status),
                               order=order)

    def _fee_from(self, result: OrderResult, quote: float) -> float:
        fee = 0.0
        for fill in result.fills or []:
            commission = safe_float(fill.get("commission"))
            asset = str(fill.get("commissionAsset") or "")
            if asset == self.cfg.quote_asset:
                fee += commission
            elif asset and commission:
                fee += 0.0     # BNB/other asset: reported, not converted (documented)
        if fee == 0.0 and quote:
            fee = quote * self.cfg.taker_fee_bps / 10_000.0
        return fee

    def cancel(self, order: OrderRecord) -> ExecutionResult:
        try:
            result = self.client.cancel_order(order.symbol,
                                              order_id=order.exchange_order_id or None,
                                              client_order_id=order.client_order_id)
        except BinanceError as exc:
            order.error = redact(str(exc))[:200]
            self.store.save()
            return ExecutionResult(False, "ERROR", order.error, order=order)
        order.status = "CANCELED"
        order.updated_ts = utc_now()
        self.store.save()
        self.store.audit("order_canceled", symbol=order.symbol,
                         client_order_id=order.client_order_id)
        return ExecutionResult(True, "CANCELED", "cancel confirmed by the exchange",
                               order=order)

    def reconcile(self) -> List[Dict[str, Any]]:
        """Pull open orders from the exchange and sync their local status."""
        changes: List[Dict[str, Any]] = []
        try:
            remote = {str(o.get("clientOrderId")): o for o in self.client.open_orders()}
        except BinanceError:
            return changes
        for order in self.store.open_orders():
            if order.mode != MODE_LIVE:
                continue
            remote_row = remote.get(order.client_order_id)
            if remote_row is None and order.status in ("NEW", "PARTIALLY_FILLED"):
                try:
                    result = self.client.query_order(order.symbol,
                                                     client_order_id=order.client_order_id)
                    if result.status in FINAL_STATES:
                        self._record_from(order, result)
                        changes.append({"order": order.client_order_id,
                                        "status": order.status})
                except BinanceError:
                    continue
        self.store.save()
        return changes

    def cancel_all_open(self) -> List[ExecutionResult]:
        out = []
        for order in list(self.store.open_orders()):
            if order.mode == MODE_LIVE:
                out.append(self.cancel(order))
        return out


def _plain(value: Any) -> str:
    """Format a number for the API without exponent notation."""
    try:
        text = format(Decimal(str(value)), "f")
    except Exception:
        return str(value)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


class OrderManager:
    """Single gateway for orders in both modes."""

    def __init__(self, cfg: TerminalConfig, store: StateStore, risk: RiskEngine,
                 paper: PaperBroker, live: Optional[LiveBroker] = None,
                 mode_provider: Optional[Callable[[], str]] = None,
                 balance_provider: Optional[Callable[[], Dict[str, float]]] = None,
                 price_provider: Optional[Callable[[str], float]] = None,
                 filters_provider: Optional[Callable[[str], Optional[SymbolFilters]]] = None,
                 notifier: Optional[Any] = None):
        self.cfg = cfg
        self.store = store
        self.risk = risk
        self.paper = paper
        self.live = live
        self.mode_provider = mode_provider or (lambda: MODE_PAPER)
        self.balance_provider = balance_provider or (lambda: {"quote": 0.0})
        self.price_provider = price_provider or (lambda symbol: 0.0)
        self.filters_provider = filters_provider or (lambda symbol: None)
        self.notifier = notifier
        self._locks: Dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self.last_decision: Optional[RiskDecision] = None
        self.submitted = 0
        self.rejected = 0

    # ------------------------------------------------------------------
    def _lock_for(self, symbol: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(symbol)
            if lock is None:
                lock = threading.Lock()
                self._locks[symbol] = lock
            return lock

    def client_order_id(self, symbol: str, side: str, bucket: Optional[int] = None) -> str:
        """Deterministic within the duplicate window, unique after it."""
        bucket = bucket if bucket is not None else int(time.time() // max(1, self.cfg.duplicate_window_s))
        return "mt-{}-{}-{}-{}".format(symbol[:6].lower(), side.lower()[:1], bucket,
                                       abs(hash((symbol, side))) % 10_000)

    # ------------------------------------------------------------------
    def preview(self, *, symbol: str, side: str, order_type: str,
                qty: Optional[float] = None, price: Optional[float] = None,
                quote_amount: Optional[float] = None) -> Dict[str, Any]:
        mode = self.mode_provider()
        mark = self.price_provider(symbol)
        filters = self.filters_provider(symbol)
        balances = self.balance_provider()
        decision = self.risk.evaluate(
            mode=mode, symbol=symbol, side=side, order_type=order_type,
            qty=qty, price=price, quote_amount=quote_amount, last_price=mark,
            filters=filters, available_quote=balances.get("quote", 0.0),
            open_positions=len(self.store.state.positions),
            exposure_total=self.store.exposure(),
            exposure_symbol=sum(p.qty * mark for p in self.store.positions_for(symbol)),
            day_pnl=self.store.state.day_realized_pnl,
            recent_orders=self.store.state.orders,
        )
        self.last_decision = decision
        payload = decision.as_dict()
        payload["mode"] = mode
        payload["mark_price"] = mark
        payload["symbol"] = symbol.upper()
        payload["side"] = side.upper()
        payload["order_type"] = order_type.upper()
        payload["fee_bps"] = self.cfg.taker_fee_bps
        payload["slippage_bps"] = self.cfg.slippage_bps
        return payload

    # ------------------------------------------------------------------
    def submit(self, *, symbol: str, side: str, order_type: str,
               qty: Optional[float] = None, price: Optional[float] = None,
               quote_amount: Optional[float] = None,
               confirm: bool = False) -> ExecutionResult:
        symbol = (symbol or "").upper()
        side = (side or "").upper()
        order_type = (order_type or "MARKET").upper()
        mode = self.mode_provider()

        if not confirm:
            return ExecutionResult(False, "CONFIRM_REQUIRED",
                                   "orders must be confirmed in the UI first",
                                   risk={"blocked": "confirmation"})

        with self._lock_for(symbol):
            preview = self.preview(symbol=symbol, side=side, order_type=order_type,
                                   qty=qty, price=price, quote_amount=quote_amount)
            if not preview["allowed"]:
                self.rejected += 1
                self.store.audit("order_blocked", symbol=symbol, side=side,
                                 code=preview["code"], detail=preview["message"])
                self._notify_rejection(symbol, side, preview["message"])
                return ExecutionResult(False, "RISK_REJECTED", preview["message"],
                                       risk=preview)

            filters = self.filters_provider(symbol)
            mark = self.price_provider(symbol)
            cid = self.client_order_id(symbol, side)
            if self.store.find_order(cid) is not None:
                return ExecutionResult(False, "DUPLICATE", "this order was already sent",
                                       risk=preview)

            record = OrderRecord(
                client_order_id=cid, symbol=symbol, side=side, order_type=order_type,
                qty=safe_float(preview.get("qty")) or safe_float(qty),
                quote_amount=safe_float(quote_amount) if order_type == "MARKET" and side == "BUY"
                else 0.0,
                price=safe_float(preview.get("price")) or safe_float(price),
                status="PENDING", mode=mode, created_ts=utc_now(), updated_ts=utc_now(),
            )
            if record.qty <= 0 and record.quote_amount > 0 and mark > 0:
                record.qty = record.quote_amount / mark
                if filters is not None:
                    record.qty = float(filters.round_qty(record.qty))
            self.store.add_order(record)
            self.store.audit("order_submitted", mode=mode, symbol=symbol, side=side,
                             type=order_type, qty=record.qty, price=record.price,
                             client_order_id=cid)
            self.submitted += 1

            if mode == MODE_PAPER:
                result = self.paper.execute(record, mark, filters)
            else:
                if self.live is None:
                    record.status = "REJECTED"
                    record.error = "live execution is not configured"
                    self.store.save()
                    return ExecutionResult(False, "NO_LIVE_BROKER", record.error,
                                           order=record)
                result = self.live.execute(record, mark, filters)

            result.risk = preview
            self.store.audit("order_result", client_order_id=cid, status=result.status,
                             detail=result.message)
            if self.notifier is not None:
                try:
                    self.notifier.order_event(result, mode=mode)
                except Exception:               # notifications must never break trading
                    pass
            return result

    def cancel(self, client_order_id: str) -> ExecutionResult:
        order = self.store.find_order(client_order_id)
        if order is None:
            return ExecutionResult(False, "NOT_FOUND", "no such order")
        if order.status in FINAL_STATES:
            return ExecutionResult(False, "ALREADY_FINAL",
                                   "order is already {}".format(order.status), order=order)
        if order.mode == MODE_PAPER:
            order.status = "CANCELED"
            order.updated_ts = utc_now()
            self.store.save()
            self.store.audit("order_canceled", client_order_id=client_order_id, mode="PAPER")
            return ExecutionResult(True, "CANCELED", "paper order canceled", order=order)
        if self.live is None:
            return ExecutionResult(False, "NO_LIVE_BROKER", "live execution is not configured",
                                   order=order)
        return self.live.cancel(order)

    # ------------------------------------------------------------------
    def _notify_rejection(self, symbol: str, side: str, message: str) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.order_rejected(symbol=symbol, side=side, reason=message)
        except Exception:
            pass
