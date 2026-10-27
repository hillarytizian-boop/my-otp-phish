"""
memeterm.risk - the last line of defence before any order leaves the process.

Everything here is server-side. The browser can *ask* for an order; it cannot
grant itself an exemption. A rejected order returns the full checklist, so the
UI can show exactly which rule stopped it instead of a generic error.

There is no code path that increases position size after a loss, no martingale,
no averaging down, and no "unlimited" option: the config ceilings in
`config.HARD_CEILINGS` are enforced again here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from .binance import SymbolFilters
from .config import HARD_CEILINGS, TerminalConfig
from .store import OrderRecord
from .util import safe_float

MODE_DISCONNECTED = "DISCONNECTED"
MODE_PAPER = "PAPER"
MODE_LIVE = "LIVE_SPOT"
MODES = (MODE_DISCONNECTED, MODE_PAPER, MODE_LIVE)


@dataclass
class Check:
    code: str
    ok: bool
    detail: str = ""
    severity: str = "error"          # error | warning | info

    def as_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "ok": self.ok, "detail": self.detail,
                "severity": self.severity}


@dataclass
class RiskDecision:
    allowed: bool
    code: str = "OK"
    message: str = "ok"
    checks: List[Check] = field(default_factory=list)
    qty: str = ""
    price: str = ""
    notional: float = 0.0
    estimated_fee: float = 0.0
    estimated_slippage: float = 0.0
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed, "code": self.code, "message": self.message,
            "checks": [c.as_dict() for c in self.checks], "qty": self.qty,
            "price": self.price, "notional": round(self.notional, 8),
            "estimated_fee": round(self.estimated_fee, 8),
            "estimated_slippage": round(self.estimated_slippage, 8),
            "estimated_total_cost": round(self.estimated_fee + self.estimated_slippage, 8),
            "warnings": self.warnings,
        }


class RiskEngine:
    def __init__(self, cfg: TerminalConfig):
        self.cfg = cfg

    # ------------------------------------------------------------------
    def estimate_costs(self, notional: float, *, taker: bool = True) -> Dict[str, float]:
        fee_bps = self.cfg.taker_fee_bps if taker else self.cfg.maker_fee_bps
        fee = max(0.0, notional) * fee_bps / 10_000.0
        slippage = max(0.0, notional) * self.cfg.slippage_bps / 10_000.0
        return {"fee": fee, "slippage": slippage, "total": fee + slippage,
                "fee_bps": fee_bps, "slippage_bps": self.cfg.slippage_bps}

    # ------------------------------------------------------------------
    def evaluate(self, *, mode: str, symbol: str, side: str, order_type: str,
                 qty: Optional[float], price: Optional[float],
                 quote_amount: Optional[float], last_price: float,
                 filters: Optional[SymbolFilters], available_quote: float,
                 open_positions: int, exposure_total: float,
                 exposure_symbol: float, day_pnl: float,
                 recent_orders: Optional[List[OrderRecord]] = None,
                 now: Optional[float] = None,
                 data_age_s: Optional[float] = None) -> RiskDecision:
        cfg = self.cfg
        now = now if now is not None else time.time()
        recent_orders = recent_orders or []
        checks: List[Check] = []
        warnings: List[str] = []
        symbol = (symbol or "").upper()
        side = (side or "").upper()
        order_type = (order_type or "MARKET").upper()

        def add(code: str, ok: bool, detail: str = "", severity: str = "error") -> None:
            checks.append(Check(code, ok, detail, severity))

        # --- mode / availability ---------------------------------------
        if mode not in MODES:
            add("MODE_UNKNOWN", False, "unknown mode {!r}".format(mode))
        if mode == MODE_DISCONNECTED:
            add("MODE_DISCONNECTED", False,
                "no exchange connection: public market data only, orders are disabled")
        add("MODE_OK", mode in (MODE_PAPER, MODE_LIVE), "mode: {}".format(mode))

        # --- symbol / filters ------------------------------------------
        if not symbol:
            add("SYMBOL_MISSING", False, "no symbol requested")
        if filters is None:
            add("SYMBOL_UNKNOWN", False,
                "{} is not a listed Binance Spot symbol".format(symbol or "?"))
        else:
            add("SYMBOL_OK", True, "{} ({}/{}, {})".format(
                filters.symbol, filters.base_asset, filters.quote_asset, filters.status))
            for problem in filters.validate(side, order_type, qty, price,
                                            quote_amount, last_price):
                add("EXCHANGE_FILTER", False, problem)

        if last_price <= 0:
            add("PRICE_UNAVAILABLE", False, "no usable mark price for {}".format(symbol))
        if data_age_s is not None and data_age_s > 60:
            add("DATA_STALE", False,
                "market data is {:.0f}s old; wait for a fresh tick".format(data_age_s))

        # --- size -------------------------------------------------------
        reference_price = safe_float(price) or last_price
        requested_qty = safe_float(qty) if qty is not None else 0.0
        requested_quote = safe_float(quote_amount) if quote_amount is not None else 0.0
        if order_type == "LIMIT" and requested_qty and reference_price:
            notional = requested_qty * reference_price
        elif requested_qty and reference_price:
            notional = requested_qty * reference_price
        else:
            notional = requested_quote

        add("SIZE_POSITIVE", notional > 0, "order notional: {:.2f} {}".format(
            notional, cfg.quote_asset))
        if notional > 0:
            add("SIZE_WITHIN_LIMIT", notional <= cfg.max_order_usdt,
                "max order size is {:.2f} {} (requested {:.2f})".format(
                    cfg.max_order_usdt, cfg.quote_asset, notional))
            add("SIZE_WITHIN_CEILING", notional <= HARD_CEILINGS["max_order_usdt"],
                "hard ceiling is {:.0f} {}".format(HARD_CEILINGS["max_order_usdt"],
                                                   cfg.quote_asset))
            add("SIZE_ABOVE_EXCHANGE_MIN", notional >= cfg.min_order_notional_usdt,
                "exchange minimum notional is {:.2f} {}".format(
                    cfg.min_order_notional_usdt, cfg.quote_asset))

        # --- exposure ---------------------------------------------------
        new_exposure = exposure_total if side == "SELL" else exposure_total + max(0.0, notional)
        per_symbol = exposure_symbol if side == "SELL" else exposure_symbol + max(0.0, notional)
        if side == "BUY":
            add("EXPOSURE_TOTAL", new_exposure <= cfg.max_exposure_usdt,
                "total exposure {:.2f} / {:.2f} {}".format(
                    new_exposure, cfg.max_exposure_usdt, cfg.quote_asset))
            add("EXPOSURE_SYMBOL", per_symbol <= cfg.per_symbol_exposure_usdt,
                "{} exposure {:.2f} / {:.2f}".format(
                    symbol or "?", per_symbol, cfg.per_symbol_exposure_usdt))
            add("MAX_POSITIONS", open_positions < cfg.max_positions,
                "open positions {} / {}".format(open_positions, cfg.max_positions))

        # --- daily loss --------------------------------------------------
        add("DAILY_LOSS", day_pnl > -abs(cfg.daily_loss_limit_usdt),
            "daily PnL {:+.2f} {} (limit -{:.2f})".format(
                day_pnl, cfg.quote_asset, abs(cfg.daily_loss_limit_usdt)))
        if day_pnl <= -abs(cfg.daily_loss_limit_usdt) * 0.6:
            warnings.append("daily loss is at {:.0f}% of the configured limit".format(
                100 * abs(day_pnl) / max(1e-9, abs(cfg.daily_loss_limit_usdt))))

        # --- balance -----------------------------------------------------
        if side == "BUY":
            required = notional + self.estimate_costs(notional)["total"]
            add("BALANCE", available_quote + 1e-9 >= required,
                "available {:.2f} {}, order + costs {:.2f}".format(
                    available_quote, cfg.quote_asset, required))

        # --- duplicate / cooldown ---------------------------------------
        cutoff = now - cfg.duplicate_window_s
        same = [o for o in recent_orders
                if o.symbol == symbol and o.side == side
                and o.created_ts >= cutoff
                and o.status not in ("CANCELED", "REJECTED", "EXPIRED")]
        add("DUPLICATE_ORDER", not same,
            "an identical {} {} was sent less than {:.0f}s ago".format(
                side, symbol, cfg.duplicate_window_s) if same else "no duplicate in flight")
        cooldown_cutoff = now - cfg.symbol_cooldown_s
        recent_symbol = [o for o in recent_orders
                         if o.symbol == symbol and o.created_ts >= cooldown_cutoff
                         and o.status not in ("CANCELED", "REJECTED", "EXPIRED")]
        add("SYMBOL_COOLDOWN", not recent_symbol,
            "{} was traded {:.0f}s ago (cooldown {:.0f}s)".format(
                symbol, cfg.symbol_cooldown_s, cfg.symbol_cooldown_s)
            if recent_symbol else "cooldown clear", severity="warning")
        if recent_symbol:
            # A cooldown is a soft guard for SELL (you may need out), hard for BUY.
            checks[-1].severity = "error" if side == "BUY" else "warning"

        # --- data quality ------------------------------------------------
        if mode == MODE_LIVE and filters is not None and order_type == "LIMIT":
            if price is None:
                add("PRICE_REQUIRED", False, "LIMIT orders need a price")
            elif last_price and abs(safe_float(price) - last_price) / last_price > 0.2:
                warnings.append("limit price is more than 20% away from the last trade")

        # --- verdict ------------------------------------------------------
        hard_failures = [c for c in checks if not c.ok and c.severity == "error"]
        # Informational duplicates: MODE_OK passes for PAPER/LIVE, so skip it.
        fatal = [c for c in hard_failures if c.code not in ("MODE_OK",)]
        costs = self.estimate_costs(notional)
        decision = RiskDecision(
            allowed=not fatal,
            code=fatal[0].code if fatal else "OK",
            message=fatal[0].detail if fatal else "all risk checks passed",
            checks=checks,
            notional=notional,
            estimated_fee=costs["fee"],
            estimated_slippage=costs["slippage"],
            warnings=warnings,
        )
        if filters is not None:
            if order_type == "LIMIT" and price is not None:
                decision.price = str(filters.round_price(price))
            if qty is not None and safe_float(qty) > 0:
                decision.qty = str(filters.round_qty(qty))
            elif quote_amount and last_price > 0:
                decision.qty = str(filters.qty_for_quote(quote_amount, last_price))
        return decision

    # ------------------------------------------------------------------
    def daily_loss_state(self, day_pnl: float) -> Dict[str, Any]:
        limit = abs(self.cfg.daily_loss_limit_usdt)
        used = max(0.0, -day_pnl)
        return {
            "limit": limit,
            "used": round(used, 6),
            "remaining": round(max(0.0, limit - used), 6),
            "pct": round(100.0 * used / limit, 2) if limit else 0.0,
            "breached": used >= limit,
        }
