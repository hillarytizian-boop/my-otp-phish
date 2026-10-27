"""
memeterm.backtest - reproducible research, with the boring parts done properly.

Pipeline
--------
1. `collect_candles()` pulls history from Binance (paginated, 1000 bars/call).
2. `build_features()` computes, for every bar *t*, features that use only bars
   `<= t`. This is enforced by a test that recomputes features on every prefix
   and asserts they do not change - the cheapest possible leakage guard.
3. Labels look forward: did the price clear costs over the next `horizon` bars?
4. Split: chronological train / validation / holdout, with an embargo of
   `horizon` bars between segments so a label cannot peek across a boundary.
5. Model: pure-Python logistic regression (no numpy, no sklearn, nothing to
   compile on Termux). The rule-based baseline is always available and is
   scored with exactly the same execution model.
6. Execution: decision at close of bar t, entry at the open of t+1, exit after
   `horizon` bars, fees + slippage on both sides.

Honesty rules baked into the report: sample size is printed, an "insufficient
sample" flag appears under 30 test trades, and nothing here can enable live
trading. A good backtest is a hypothesis, not permission.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .binance import BinanceClient, BinanceError
from .market import INTERVAL_MS, parse_klines
from .util import ensure_dir, percentile, safe_float, stdev, utc_str, write_json

FEATURES = [
    "ret_1", "ret_5", "ret_15", "ema_gap", "rsi14", "vol_z", "range_pos",
    "atr_pct", "body", "slope30", "taker_ratio", "trade_z", "spread_proxy",
]

MIN_TEST_TRADES = 30


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------
def collect_candles(client: BinanceClient, symbol: str, interval: str = "15m",
                    bars: int = 3000) -> List[Dict[str, float]]:
    """Page backwards until we have `bars` candles (max 1000 per call)."""
    step = INTERVAL_MS.get(interval, 900_000)
    out: List[List[Any]] = []
    end_time = None
    remaining = max(1, int(bars))
    while remaining > 0:
        limit = min(1000, remaining)
        rows = client.klines(symbol, interval=interval, limit=limit,
                             end_ms=end_time)
        if not rows:
            break
        out = rows + out
        remaining -= len(rows)
        end_time = int(rows[0][0]) - 1
        if len(rows) < limit:
            break
        time.sleep(0.15)                     # be a good citizen about weight
    # dedupe by open time, keep order
    seen = set()
    clean: List[List[Any]] = []
    for row in out:
        if int(row[0]) in seen:
            continue
        seen.add(int(row[0]))
        clean.append(row)
    return parse_klines(clean[-bars:]) if bars else parse_klines(clean)


# ---------------------------------------------------------------------------
# features
# ---------------------------------------------------------------------------
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
    gains = losses = 0.0
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
    mx = (n - 1) / 2.0
    my = sum(values) / n
    den = sum((x - mx) ** 2 for x in range(n))
    return sum((x - mx) * (y - my) for x, y in enumerate(values)) / den if den else 0.0


def features_at(candles: Sequence[Dict[str, float]], i: int) -> Optional[Dict[str, float]]:
    """Features for bar i using candles[0..i] only. Never reads i+1."""
    if i < 60:
        return None
    window = list(candles[:i + 1])
    closes = [c["c"] for c in window]
    price = closes[-1]
    if price <= 0:
        return None
    ret_1 = closes[-1] / closes[-2] - 1 if closes[-2] else 0.0
    ret_5 = closes[-1] / closes[-6] - 1 if len(closes) > 5 and closes[-6] else 0.0
    ret_15 = closes[-1] / closes[-16] - 1 if len(closes) > 15 and closes[-16] else 0.0
    ema9, ema21 = _ema(closes[-60:], 9), _ema(closes[-60:], 21)
    ema_gap = (ema9 - ema21) / ema21 if ema21 else 0.0
    rsi = _rsi(closes[-60:], 14)
    qvols = [c["qv"] for c in window[-31:]]
    vol_z = 0.0
    if len(qvols) > 5:
        mu = sum(qvols[:-1]) / len(qvols[:-1])
        sd = stdev(qvols[:-1])
        vol_z = (qvols[-1] - mu) / sd if sd else 0.0
    hi = max(c["h"] for c in window[-60:])
    lo = min(c["l"] for c in window[-60:])
    range_pos = (price - lo) / (hi - lo) if hi > lo else 0.5
    trs = []
    for j in range(-14, 0):
        bar, prev = window[j], window[j - 1]
        trs.append(max(bar["h"] - bar["l"], abs(bar["h"] - prev["c"]),
                       abs(bar["l"] - prev["c"])))
    atr_pct = (sum(trs) / len(trs)) / price * 100 if trs else 0.0
    bar = window[-1]
    body = (bar["c"] - bar["o"]) / (bar["h"] - bar["l"]) if bar["h"] > bar["l"] else 0.0
    slope30 = _slope(closes[-30:]) / price
    taker = bar.get("tq") or 0.0
    qv = bar.get("qv") or 0.0
    taker_ratio = (taker / qv) if qv else 0.5
    trades = [c.get("trades", 0) for c in window[-31:]]
    trade_z = 0.0
    if len(trades) > 5:
        mu = sum(trades[:-1]) / len(trades[:-1])
        sd = stdev(trades[:-1])
        trade_z = (trades[-1] - mu) / sd if sd else 0.0
    spread_proxy = (bar["h"] - bar["l"]) / price * 100
    return {
        "ret_1": ret_1, "ret_5": ret_5, "ret_15": ret_15, "ema_gap": ema_gap,
        "rsi14": (rsi - 50.0) / 50.0, "vol_z": vol_z, "range_pos": range_pos,
        "atr_pct": atr_pct, "body": body, "slope30": slope30,
        "taker_ratio": taker_ratio - 0.5, "trade_z": trade_z,
        "spread_proxy": spread_proxy,
    }


def build_dataset(candles: Sequence[Dict[str, float]], *, horizon: int = 6,
                  fee_bps: float = 10.0, slippage_bps: float = 10.0) -> Dict[str, Any]:
    """Features + forward-looking labels. Cost-aware: the label is net of fees."""
    rows: List[Dict[str, Any]] = []
    round_trip_cost = 2.0 * (fee_bps + slippage_bps) / 10_000.0
    for i in range(len(candles) - horizon - 1):
        feats = features_at(candles, i)
        if feats is None:
            continue
        entry = candles[i + 1]["o"]
        exit_price = candles[i + 1 + horizon]["o"]
        if entry <= 0:
            continue
        gross = exit_price / entry - 1.0
        net = gross - round_trip_cost
        rows.append({"ts": candles[i]["ts"], "index": i, "features": feats,
                     "gross": gross, "net": net, "label": 1 if net > 0 else 0,
                     "entry": entry})
    return {"rows": rows, "horizon": horizon, "round_trip_cost": round_trip_cost,
            "feature_names": list(FEATURES)}


def chronological_split(rows: List[Dict[str, Any]], *, embargo: int,
                        train: float = 0.6, val: float = 0.2) -> Dict[str, List[Dict[str, Any]]]:
    """Train / validation / holdout in time order, with a gap between them."""
    n = len(rows)
    train_end = int(n * train)
    val_end = int(n * (train + val))
    return {
        "train": rows[:max(0, train_end - embargo)],
        "val": rows[train_end:max(train_end, val_end - embargo)],
        "test": rows[val_end:],
        "embargo": embargo,
        "sizes": {"train": max(0, train_end - embargo),
                  "val": max(0, val_end - train_end - embargo),
                  "test": max(0, n - val_end)},
    }


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------
@dataclass
class LogisticModel:
    weights: List[float] = field(default_factory=list)
    bias: float = 0.0
    means: List[float] = field(default_factory=list)
    stds: List[float] = field(default_factory=list)
    feature_names: List[str] = field(default_factory=list)
    threshold: float = 0.55

    def _vector(self, features: Dict[str, float]) -> List[float]:
        return [safe_float(features.get(name)) for name in self.feature_names]

    def predict_proba(self, features: Dict[str, float]) -> float:
        if not self.weights:
            return 0.5
        vec = self._vector(features)
        z = self.bias
        for value, weight, mean, sd in zip(vec, self.weights, self.means, self.stds):
            z += weight * ((value - mean) / (sd or 1.0))
        z = max(-30.0, min(30.0, z))
        return 1.0 / (1.0 + math.exp(-z))

    def as_dict(self) -> Dict[str, Any]:
        return {"weights": self.weights, "bias": self.bias, "means": self.means,
                "stds": self.stds, "feature_names": self.feature_names,
                "threshold": self.threshold}

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "LogisticModel":
        return cls(weights=list(payload.get("weights") or []),
                   bias=safe_float(payload.get("bias")),
                   means=list(payload.get("means") or []),
                   stds=list(payload.get("stds") or []),
                   feature_names=list(payload.get("feature_names") or FEATURES),
                   threshold=safe_float(payload.get("threshold"), 0.55))


def train_logistic(rows: List[Dict[str, Any]], feature_names: Sequence[str] = FEATURES,
                   *, epochs: int = 220, lr: float = 0.35, l2: float = 0.01) -> LogisticModel:
    """Plain gradient descent. Deterministic, dependency-free, boring on purpose."""
    if not rows:
        return LogisticModel(feature_names=list(feature_names))
    matrix = [[safe_float(row["features"].get(name)) for name in feature_names]
              for row in rows]
    labels = [float(row["label"]) for row in rows]
    n = len(matrix)
    means = [sum(col) / n for col in zip(*matrix)]
    stds = [stdev(list(col)) for col in zip(*matrix)]
    normed = [[(value - mean) / (sd or 1.0) for value, mean, sd in zip(row, means, stds)]
              for row in matrix]
    weights = [0.0] * len(feature_names)
    bias = 0.0
    for _ in range(epochs):
        grad_w = [0.0] * len(weights)
        grad_b = 0.0
        for row, label in zip(normed, labels):
            z = bias + sum(w * x for w, x in zip(weights, row))
            z = max(-30.0, min(30.0, z))
            pred = 1.0 / (1.0 + math.exp(-z))
            error = pred - label
            for i, value in enumerate(row):
                grad_w[i] += error * value
            grad_b += error
        for i in range(len(weights)):
            weights[i] -= lr * (grad_w[i] / n + l2 * weights[i])
        bias -= lr * grad_b / n
    return LogisticModel(weights=weights, bias=bias, means=means, stds=stds,
                         feature_names=list(feature_names))


# ---------------------------------------------------------------------------
# simulation
# ---------------------------------------------------------------------------
def simulate(rows: List[Dict[str, Any]], signals: List[bool], *, horizon: int,
             capital: float = 100.0, size_usdt: float = 10.0,
             fee_bps: float = 10.0, slippage_bps: float = 10.0) -> Dict[str, Any]:
    """Long-only, one position at a time, entry at t+1 open, exit after horizon."""
    trades: List[Dict[str, Any]] = []
    equity = capital
    peak = capital
    max_dd = 0.0
    index_by_i = {row["index"]: pos for pos, row in enumerate(rows)}
    skip_until = -1
    for pos, row in enumerate(rows):
        i = row["index"]
        if i < skip_until or pos >= len(signals) or not signals[pos]:
            continue
        entry = row["entry"] * (1 + slippage_bps / 10_000.0)
        exit_row = None
        for candidate in rows[pos + horizon: pos + horizon + 1]:
            exit_row = candidate
        if exit_row is None:
            break
        exit_price = exit_row["entry"] * (1 - slippage_bps / 10_000.0)
        qty = size_usdt / entry if entry else 0.0
        gross = (exit_price - entry) * qty
        fee = (entry * qty + exit_price * qty) * fee_bps / 10_000.0
        net = gross - fee
        equity += net
        peak = max(peak, equity)
        max_dd = min(max_dd, (equity - peak) / peak * 100.0)
        trades.append({"entry_ts": row["ts"], "entry": entry, "exit": exit_price,
                       "qty": qty, "gross": gross, "fee": fee, "net": net,
                       "return_pct": (exit_price / entry - 1) * 100.0,
                       "net_return_pct": (net / size_usdt) * 100.0})
        skip_until = i + horizon + 1
    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    gross_profit = sum(t["net"] for t in wins)
    gross_loss = abs(sum(t["net"] for t in losses))
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / len(trades), 2) if trades else 0.0,
        "net_pnl": round(equity - capital, 4),
        "net_return_pct": round((equity / capital - 1) * 100.0, 3),
        "expectancy": round(sum(t["net"] for t in trades) / len(trades), 6) if trades else 0.0,
        "expectancy_pct": round(sum(t["net_return_pct"] for t in trades) / len(trades), 4)
        if trades else 0.0,
        "max_drawdown_pct": round(max_dd, 3),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else (
            None if gross_profit == 0 else 99.0),
        "fees_paid": round(sum(t["fee"] for t in trades), 4),
        "avg_trade_pct": round(sum(t["net_return_pct"] for t in trades) / len(trades), 4)
        if trades else 0.0,
        "trade_log": trades[-40:],
    }


def _signals_from_probs(rows: List[Dict[str, Any]], probs: List[float],
                        threshold: float) -> Tuple[List[bool], Dict[str, float]]:
    signals = [p >= threshold for p in probs]
    tp = sum(1 for row, s in zip(rows, signals) if s and row["label"] == 1)
    fp = sum(1 for row, s in zip(rows, signals) if s and row["label"] == 0)
    fn = sum(1 for row, s in zip(rows, signals) if not s and row["label"] == 1)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    fpr = fp / (fp + (len(rows) - tp - fp - fn)) if rows else 0.0
    return signals, {"precision": round(precision, 4), "recall": round(recall, 4),
                     "false_positive_rate": round(fpr, 4), "tp": tp, "fp": fp, "fn": fn}


def baseline_signals(rows: List[Dict[str, Any]]) -> List[bool]:
    """The dumb rule every model must beat: momentum + trend agreement."""
    out = []
    for row in rows:
        f = row["features"]
        out.append(f.get("ret_5", 0) > 0 and f.get("ema_gap", 0) > 0 and f.get("vol_z", 0) > 0)
    return out


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------
class Backtester:
    def __init__(self, data_dir: str, *, fee_bps: float = 10.0, slippage_bps: float = 10.0,
                 capital: float = 100.0, size_usdt: float = 10.0):
        self.dir = ensure_dir(os.path.abspath(os.path.expanduser(data_dir)), 0o700)
        self.models_dir = ensure_dir(os.path.join(self.dir, "models"), 0o700)
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps
        self.capital = capital
        self.size_usdt = size_usdt

    # -- storage -------------------------------------------------------
    def model_path(self, version: str) -> str:
        return os.path.join(self.models_dir, "{}.json".format(version))

    def latest_path(self) -> str:
        return os.path.join(self.models_dir, "latest.json")

    def load_latest(self) -> Optional[Dict[str, Any]]:
        try:
            with open(self.latest_path(), "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def save_model(self, version: str, model: LogisticModel,
                   report: Dict[str, Any]) -> str:
        payload = {"version": version, "trained_at": time.time(),
                   "trained_utc": utc_str(), "model": model.as_dict(),
                   "report": report,
                   "costs": {"fee_bps": self.fee_bps,
                             "slippage_bps": self.slippage_bps}}
        write_json(self.model_path(version), payload, mode=0o600)
        write_json(self.latest_path(), {"version": version, "trained_at": payload["trained_at"],
                                        "path": self.model_path(version)}, mode=0o600)
        return self.model_path(version)

    # -- main ----------------------------------------------------------
    def run(self, candles: List[Dict[str, float]], *, symbol: str, interval: str,
            horizon: int = 6, train_frac: float = 0.6, val_frac: float = 0.2,
            persist: bool = False) -> Dict[str, Any]:
        dataset = build_dataset(candles, horizon=horizon, fee_bps=self.fee_bps,
                                slippage_bps=self.slippage_bps)
        rows = dataset["rows"]
        if len(rows) < 120:
            return {"ok": False, "error": "not enough history: {} usable bars "
                                          "(need at least 120)".format(len(rows)),
                    "symbol": symbol, "interval": interval}
        split = chronological_split(rows, embargo=horizon, train=train_frac, val=val_frac)
        train_rows, val_rows, test_rows = split["train"], split["val"], split["test"]
        if len(train_rows) < 60 or len(test_rows) < 30:
            return {"ok": False, "error": "split too small for a meaningful test",
                    "symbol": symbol, "split": split["sizes"]}

        model = train_logistic(train_rows)

        # Choose the threshold on validation only, never on the holdout.
        # Labels are rare by construction (a trade must clear round-trip costs),
        # so a fixed 0.5 cut would never fire: the grid includes relative cuts
        # taken from the validation distribution itself.
        val_probs = [model.predict_proba(row["features"]) for row in val_rows]
        grid = [0.50, 0.45, 0.40, 0.35, 0.30]
        for q in (0.999, 0.99, 0.95, 0.90, 0.80):
            grid.append(round(percentile(val_probs, q), 6))
        best_threshold, best_expectancy, chosen = 0.55, -1e9, False
        for threshold in sorted(set(grid), reverse=True):
            signals, _ = _signals_from_probs(val_rows, val_probs, threshold)
            sim = simulate(val_rows, signals, horizon=horizon, capital=self.capital,
                           size_usdt=self.size_usdt, fee_bps=self.fee_bps,
                           slippage_bps=self.slippage_bps)
            if sim["trades"] >= 3 and sim["expectancy"] > best_expectancy:
                best_expectancy = sim["expectancy"]
                best_threshold = threshold
                chosen = True
        model.threshold = best_threshold

        test_probs = [model.predict_proba(row["features"]) for row in test_rows]
        test_signals, class_metrics = _signals_from_probs(test_rows, test_probs, best_threshold)
        model_sim = simulate(test_rows, test_signals, horizon=horizon,
                             capital=self.capital, size_usdt=self.size_usdt,
                             fee_bps=self.fee_bps, slippage_bps=self.slippage_bps)
        base_signals = baseline_signals(test_rows)
        baseline_metrics = _signals_from_probs(test_rows, base_signals, 0.5)[1]
        base_sim = simulate(test_rows, base_signals, horizon=horizon,
                            capital=self.capital, size_usdt=self.size_usdt,
                            fee_bps=self.fee_bps, slippage_bps=self.slippage_bps)

        enough = model_sim["trades"] >= MIN_TEST_TRADES
        report = {
            "ok": True,
            "symbol": symbol.upper(), "interval": interval,
            "generated_utc": utc_str(),
            "bars": {"total": len(candles), "usable": len(rows),
                     "train": len(train_rows), "validation": len(val_rows),
                     "test": len(test_rows)},
            "split": {"chronological": True, "embargo_bars": horizon,
                      "train_frac": train_frac, "val_frac": val_frac,
                      "sizes": split["sizes"]},
            "costs": {"fee_bps": self.fee_bps, "slippage_bps": self.slippage_bps,
                      "round_trip_pct": round(dataset["round_trip_cost"] * 100, 3),
                      "note": "decisions at bar close, fill at next open, fees both sides"},
            "threshold": round(best_threshold, 6),
            "metrics": {
                **{k: model_sim[k] for k in ("trades", "wins", "losses", "win_rate_pct",
                                             "net_pnl", "net_return_pct", "expectancy",
                                             "expectancy_pct", "max_drawdown_pct",
                                             "profit_factor", "fees_paid", "avg_trade_pct")},
                "precision": class_metrics["precision"],
                "recall": class_metrics["recall"],
                "false_positive_rate": class_metrics["false_positive_rate"],
                "oos_net_return_pct": model_sim["net_return_pct"],
                "baseline_expectancy": base_sim["expectancy"],
                "baseline_net_return_pct": base_sim["net_return_pct"],
                "beats_baseline": (model_sim["trades"] >= MIN_TEST_TRADES
                                   and model_sim["expectancy"] > base_sim["expectancy"]),
                "edge_vs_baseline": round(model_sim["expectancy"] - base_sim["expectancy"], 6),
                "threshold_source": ("validation" if chosen else
                                     "default (validation produced fewer than 3 trades)"),
                "sample_sufficient": enough,
                "min_test_trades": MIN_TEST_TRADES,
                "split": {"chronological": True, "embargo_bars": horizon},
            },
            "baseline": {k: base_sim[k] for k in ("trades", "win_rate_pct",
                                                  "net_return_pct", "expectancy",
                                                  "max_drawdown_pct")},
            "baseline_class_metrics": baseline_metrics,
            "model": {"version": "", "type": "logistic-regression (pure python)",
                      "features": list(model.feature_names),
                      "weights": [round(w, 6) for w in model.weights]},
            "honesty": [
                "A backtest is a hypothesis, not a licence to trade.",
                "No slippage model beyond a fixed bps assumption; real fills differ.",
                "One symbol and one regime; the holdout is small by design.",
                "This report can never enable live mode.",
            ],
            "trade_log": model_sim["trade_log"],
        }
        if model_sim["trades"] == 0:
            report["warning"] = ("the model took no trades on the holdout - that is not a "
                                 "result, it is an absence of one")
        elif not enough:
            report["warning"] = ("only {} test trades - below the {} needed for a "
                                 "meaningful claim".format(model_sim["trades"], MIN_TEST_TRADES))
        if persist:
            version = "{}-{}-{}".format(symbol.upper(), interval,
                                        time.strftime("%Y%m%d-%H%M%S"))
            report["model"]["version"] = version
            path = self.save_model(version, model, report)
            report["model"]["path"] = path
        return report
