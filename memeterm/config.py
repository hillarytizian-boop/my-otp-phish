"""
memeterm.config - one configuration object, validated server-side.

Every number that can lose money lives here, has a hard ceiling, and is
re-validated on every order. The UI is allowed to *request* a change; only
`Config.validate()` may accept it.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any, Dict, List, Optional

from .util import ensure_dir, read_json, write_json


class ConfigError(ValueError):
    """Raised when a configuration value is unsafe or nonsensical."""


# Hard ceilings. These are not recommendations: the serializer refuses to go
# past them even if a user (or a bug in the UI) asks nicely.
HARD_CEILINGS = {
    "max_order_usdt": 2_000.0,
    "max_exposure_usdt": 10_000.0,
    "daily_loss_limit_usdt": 1_000.0,
    "max_positions": 20,
    "per_symbol_exposure_usdt": 2_000.0,
}

DEFAULT_WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]


@dataclass
class TerminalConfig:
    # --- process / paths -------------------------------------------------
    data_dir: str = "~/.meme_terminal"
    host: str = "127.0.0.1"
    port: int = 8787
    allow_remote: bool = False        # only honoured together with an access code
    demo: bool = False                # synthetic market data, clearly labelled

    # --- exchange --------------------------------------------------------
    api_base: str = "https://api.binance.com"
    quote_asset: str = "USDT"
    recv_window_ms: int = 5_000
    http_timeout_s: float = 10.0
    max_retries: int = 2              # idempotent GETs only; POSTs are never retried

    # --- money limits (all in quote asset, i.e. USDT) --------------------
    max_order_usdt: float = 10.0
    max_exposure_usdt: float = 20.0
    per_symbol_exposure_usdt: float = 10.0
    daily_loss_limit_usdt: float = 3.0
    max_positions: int = 3

    # --- costs -----------------------------------------------------------
    taker_fee_bps: float = 10.0       # 0.10 % default; refreshed from the exchange
    maker_fee_bps: float = 10.0
    slippage_bps: float = 10.0        # assumed, not measured, in this phase

    # --- order hygiene ---------------------------------------------------
    duplicate_window_s: float = 10.0
    symbol_cooldown_s: float = 30.0
    min_order_notional_usdt: float = 5.0   # Binance Spot MIN_NOTIONAL is 5 USDT

    # --- market data -----------------------------------------------------
    watchlist: List[str] = field(default_factory=lambda: list(DEFAULT_WATCHLIST))
    kline_interval: str = "1m"
    market_refresh_s: float = 5.0
    agents_refresh_s: float = 30.0
    max_watchlist: int = 20

    # --- behaviour -------------------------------------------------------
    paper_start_balance_usdt: float = 500.0
    paper_fill_slippage_bps: float = 5.0
    agents_max_workers: int = 4
    agents_timeout_s: float = 4.0
    audit_max_bytes: int = 4_000_000

    # --- telegram (optional) ---------------------------------------------
    telegram_alerts: bool = True

    # ------------------------------------------------------------------
    def path(self, name: str) -> str:
        return os.path.join(self.resolved_dir(), name)

    def resolved_dir(self) -> str:
        return os.path.abspath(os.path.expanduser(self.data_dir))

    def prepare(self) -> str:
        directory = ensure_dir(self.resolved_dir(), 0o700)
        return directory

    # ------------------------------------------------------------------
    def validate(self) -> "TerminalConfig":
        problems: List[str] = []

        def need(condition: bool, message: str) -> None:
            if not condition:
                problems.append(message)

        need(isinstance(self.port, int) and 1024 <= self.port <= 65535,
             "port must be an integer between 1024 and 65535")
        need(self.host in ("127.0.0.1", "localhost", "::1") or self.allow_remote,
             "binding to {} requires allow_remote=true and an access code".format(self.host))

        for name, ceiling in HARD_CEILINGS.items():
            value = getattr(self, name)
            need(isinstance(value, (int, float)) and not isinstance(value, bool),
                 "{} must be a number".format(name))
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                need(value > 0, "{} must be greater than zero".format(name))
                need(value <= ceiling, "{} may not exceed the hard ceiling {:.0f}".format(
                    name, ceiling))

        need(self.max_order_usdt <= self.max_exposure_usdt,
             "max_order_usdt may not exceed max_exposure_usdt")
        need(self.per_symbol_exposure_usdt <= self.max_exposure_usdt,
             "per_symbol_exposure_usdt may not exceed max_exposure_usdt")
        need(0 <= self.taker_fee_bps <= 100, "taker_fee_bps must be between 0 and 100")
        need(0 <= self.maker_fee_bps <= 100, "maker_fee_bps must be between 0 and 100")
        need(0 <= self.slippage_bps <= 200, "slippage_bps must be between 0 and 200")
        need(self.duplicate_window_s >= 1, "duplicate_window_s must be at least 1 second")
        need(self.symbol_cooldown_s >= 0, "symbol_cooldown_s may not be negative")
        need(self.http_timeout_s > 0, "http_timeout_s must be positive")
        need(1 <= self.max_retries <= 5, "max_retries must be between 1 and 5")
        need(self.min_order_notional_usdt >= 1.0,
             "min_order_notional_usdt must be at least 1 USDT")
        need(1 <= len(self.watchlist) <= self.max_watchlist,
             "watchlist must contain between 1 and {} symbols".format(self.max_watchlist))
        need(all(s.isalnum() and s.isupper() for s in self.watchlist if isinstance(s, str)),
             "watchlist symbols must be uppercase alphanumeric Binance symbols")

        if problems:
            raise ConfigError("; ".join(problems))
        return self

    # ------------------------------------------------------------------
    def to_public_dict(self) -> Dict[str, Any]:
        """Everything the UI may see. There are no secrets in this dataclass."""
        data = asdict(self)
        data["data_dir"] = self.resolved_dir()
        data["version"] = _version()
        return data

    def copy(self, **changes: Any) -> "TerminalConfig":
        return replace(self, **changes)

    def save(self) -> None:
        self.prepare()
        write_json(self.path("config.json"), asdict(self), mode=0o600)

    @classmethod
    def load(cls, data_dir: Optional[str] = None) -> "TerminalConfig":
        base = cls()
        if data_dir:
            base.data_dir = data_dir
        stored = read_json(base.path("config.json"), default={}) or {}
        allowed = {f.name for f in fields(cls)}
        clean = {k: v for k, v in stored.items() if k in allowed}
        if clean:
            base = replace(base, **clean)
        return base

    def apply_updates(self, payload: Dict[str, Any]) -> "TerminalConfig":
        """Merge a JSON dict from the UI, then validate the result."""
        allowed = {f.name for f in fields(self)}
        changed = {}
        for key, value in (payload or {}).items():
            if key in allowed and key not in ("data_dir", "host", "allow_remote",
                                              "demo"):
                changed[key] = value
        merged = replace(self, **changed)
        return merged.validate()

    def with_env(self) -> "TerminalConfig":
        """Environment overrides for headless/advanced use (never secrets)."""
        changes: Dict[str, Any] = {}
        if os.environ.get("MEMETERM_PORT"):
            changes["port"] = int(os.environ["MEMETERM_PORT"])
        if os.environ.get("MEMETERM_HOST"):
            changes["host"] = os.environ["MEMETERM_HOST"]
        if os.environ.get("MEMETERM_DATA_DIR"):
            changes["data_dir"] = os.environ["MEMETERM_DATA_DIR"]
        if os.environ.get("MEMETERM_ALLOW_REMOTE") == "1":
            changes["allow_remote"] = True
        return replace(self, **changes) if changes else self


def _version() -> str:
    from . import __version__
    return __version__
