"""
memeterm.store - restart-safe state, the trade ledger and the audit log.

Three files live in the data directory and none of them ever contains a
credential:

* `state.json`  - positions, open orders, paper balances, PnL, day counters.
                  Written atomically (tmp + fsync + rename) and 0600.
* `trades.csv`  - append-only ledger of every fill, in both modes, labelled.
* `audit.jsonl` - append-only decision log (orders, rejections, mode changes,
                  credential events) with secrets redacted.
"""

from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .util import (append_line, ensure_dir, read_json, redact, utc_now, utc_str,
                   write_json)

SCHEMA_VERSION = 2

LEDGER_COLUMNS = ["ts_utc", "mode", "event", "symbol", "side", "qty", "price",
                  "quote", "fee", "pnl", "position_id", "order_id", "note"]


@dataclass
class Position:
    position_id: str
    symbol: str
    qty: float
    entry_price: float
    entry_ts: float
    quote_spent: float = 0.0
    fee_paid: float = 0.0
    mode: str = "PAPER"
    order_id: str = ""
    note: str = ""

    @property
    def cost_basis(self) -> float:
        return self.quote_spent + self.fee_paid

    def market_value(self, price: float) -> float:
        return self.qty * price

    def unrealized_pnl(self, price: float, fee_bps: float = 10.0) -> float:
        """Marked at `price`, minus the taker fee we would pay to exit."""
        if price <= 0 or self.qty <= 0:
            return 0.0
        gross = (price - self.entry_price) * self.qty
        exit_fee = price * self.qty * fee_bps / 10_000.0
        return gross - exit_fee

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OrderRecord:
    client_order_id: str
    symbol: str
    side: str
    order_type: str
    qty: float = 0.0
    quote_amount: float = 0.0
    price: float = 0.0
    status: str = "NEW"
    mode: str = "PAPER"
    created_ts: float = 0.0
    updated_ts: float = 0.0
    exchange_order_id: int = 0
    filled_qty: float = 0.0
    quote_qty: float = 0.0
    avg_price: float = 0.0
    fee: float = 0.0
    error: str = ""
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "OrderRecord":
        allowed = {f for f in cls.__dataclass_fields__}          # type: ignore[attr-defined]
        return cls(**{k: v for k, v in raw.items() if k in allowed})


@dataclass
class TerminalState:
    schema: int = SCHEMA_VERSION
    paper_quote: float = 0.0            # virtual USDT
    paper_base: Dict[str, float] = field(default_factory=dict)
    positions: List[Position] = field(default_factory=list)
    orders: List[OrderRecord] = field(default_factory=list)
    realized_pnl: float = 0.0
    day_key: str = ""
    day_start_equity: float = 0.0
    day_realized_pnl: float = 0.0
    day_fees: float = 0.0
    emergency_stop: bool = False
    emergency_reason: str = ""
    total_fees: float = 0.0
    wins: int = 0
    losses: int = 0
    last_price: Dict[str, float] = field(default_factory=dict)
    started_at: float = field(default_factory=utc_now)

    def as_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["positions"] = [p.as_dict() for p in self.positions]
        payload["orders"] = [o.as_dict() for o in self.orders]
        return payload

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "TerminalState":
        state = cls()
        if not isinstance(raw, dict):
            return state
        state.paper_quote = float(raw.get("paper_quote") or 0.0)
        state.paper_base = {str(k): float(v) for k, v in (raw.get("paper_base") or {}).items()}
        for row in raw.get("positions") or []:
            try:
                state.positions.append(Position(**row))
            except TypeError:
                continue
        for row in raw.get("orders") or []:
            try:
                state.orders.append(OrderRecord.from_dict(row))
            except TypeError:
                continue
        for name in ("realized_pnl", "day_start_equity", "day_realized_pnl", "day_fees",
                     "total_fees", "started_at"):
            if raw.get(name) is not None:
                setattr(state, name, float(raw[name]))
        state.day_key = str(raw.get("day_key") or "")
        state.emergency_stop = bool(raw.get("emergency_stop"))
        state.emergency_reason = str(raw.get("emergency_reason") or "")
        state.wins = int(raw.get("wins") or 0)
        state.losses = int(raw.get("losses") or 0)
        state.last_price = {str(k): float(v) for k, v in (raw.get("last_price") or {}).items()}
        return state


class StateStore:
    def __init__(self, data_dir: str, *, quote_asset: str = "USDT"):
        self.dir = ensure_dir(os.path.abspath(os.path.expanduser(data_dir)), 0o700)
        self.state_path = os.path.join(self.dir, "state.json")
        self.ledger_path = os.path.join(self.dir, "trades.csv")
        self.audit_path = os.path.join(self.dir, "audit.jsonl")
        self.quote_asset = quote_asset
        self.state = TerminalState()
        self.loaded_from_disk = False
        self.recovered_from_corrupt = False

    # -- persistence ---------------------------------------------------
    def load(self) -> TerminalState:
        raw = read_json(self.state_path, default=None)
        if raw is None:
            if os.path.exists(self.state_path):
                # Corrupt: keep the bytes for forensics, start clean rather than crash.
                try:
                    os.replace(self.state_path, self.state_path + ".corrupt-{}".format(
                        int(time.time())))
                except OSError:
                    pass
                self.recovered_from_corrupt = True
            return self.state
        state = TerminalState.from_dict(raw)
        if int(raw.get("schema", 0)) < SCHEMA_VERSION:
            state.schema = SCHEMA_VERSION
        self.state = state
        self.loaded_from_disk = True
        return state

    def save(self) -> None:
        write_json(self.state_path, self.state.as_dict(), mode=0o600)

    # -- ledger --------------------------------------------------------
    def ledger(self, *, event: str, mode: str, symbol: str = "", side: str = "",
               qty: float = 0.0, price: float = 0.0, quote: float = 0.0,
               fee: float = 0.0, pnl: float = 0.0, position_id: str = "",
               order_id: str = "", note: str = "") -> None:
        row = {"ts_utc": utc_str(), "mode": mode, "event": event, "symbol": symbol,
               "side": side, "qty": _num(qty), "price": _num(price), "quote": _num(quote),
               "fee": _num(fee), "pnl": _num(pnl), "position_id": position_id,
               "order_id": order_id, "note": redact(note)[:200]}
        new = not os.path.exists(self.ledger_path)
        try:
            with open(self.ledger_path, "a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=LEDGER_COLUMNS)
                if new:
                    writer.writeheader()
                writer.writerow(row)
            if new:
                try:
                    os.chmod(self.ledger_path, 0o600)
                except OSError:
                    pass
        except OSError:
            pass

    def read_ledger(self, limit: int = 200) -> List[Dict[str, str]]:
        rows: List[Dict[str, str]] = []
        try:
            with open(self.ledger_path, "r", newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        except (OSError, csv.Error):
            return []
        return rows[-limit:]

    # -- audit ---------------------------------------------------------
    def audit(self, event: str, **payload: Any) -> None:
        record = {"ts": utc_now(), "ts_utc": utc_str(), "event": event}
        for key, value in payload.items():
            if isinstance(value, str):
                record[key] = redact(value)[:500]
            elif isinstance(value, (int, float, bool)) or value is None:
                record[key] = value
            else:
                record[key] = redact(json.dumps(value, default=str))[:500]
        try:
            append_line(self.audit_path, json.dumps(record, sort_keys=True), mode=0o600)
        except OSError:
            pass

    def read_audit(self, limit: int = 200, max_bytes: int = 2_000_000) -> List[Dict[str, Any]]:
        try:
            size = os.path.getsize(self.audit_path)
            with open(self.audit_path, "rb") as handle:
                if size > max_bytes:
                    handle.seek(size - max_bytes)
                    handle.readline()
                lines = handle.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return []
        out: List[Dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    # -- helpers -------------------------------------------------------
    def add_order(self, order: OrderRecord, keep: int = 300) -> None:
        self.state.orders.insert(0, order)
        del self.state.orders[keep:]

    def find_order(self, client_order_id: str) -> Optional[OrderRecord]:
        for order in self.state.orders:
            if order.client_order_id == client_order_id:
                return order
        return None

    def open_orders(self, symbol: Optional[str] = None) -> List[OrderRecord]:
        rows = [o for o in self.state.orders
                if o.status in ("NEW", "PARTIALLY_FILLED", "UNKNOWN", "PENDING")]
        if symbol:
            rows = [o for o in rows if o.symbol == symbol.upper()]
        return rows

    def roll_day_if_needed(self, day_key: str, equity: float) -> bool:
        if self.state.day_key == day_key:
            return False
        self.state.day_key = day_key
        self.state.day_realized_pnl = 0.0
        self.state.day_fees = 0.0
        self.state.day_start_equity = equity
        return True

    def positions_for(self, symbol: str) -> List[Position]:
        return [p for p in self.state.positions if p.symbol == symbol.upper()]

    def exposure(self, last_price: Optional[Dict[str, float]] = None) -> float:
        prices = last_price or self.state.last_price
        total = 0.0
        for pos in self.state.positions:
            price = prices.get(pos.symbol, pos.entry_price)
            total += pos.qty * price
        return total


def _num(value: Any) -> str:
    try:
        return "{:.10f}".format(float(value)).rstrip("0").rstrip(".") or "0"
    except (TypeError, ValueError):
        return "0"
