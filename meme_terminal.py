#!/usr/bin/env python3
"""
meme_terminal.py - Meme Agents AI Trading Terminal (Binance Spot).

A local, paper-first trading terminal layered on the existing meme_agents
project. Standard library only: it runs on Termux with no compiler and no pip
install. The browser UI is served from ./memeterm/web as static files.

    python meme_terminal.py serve                 # http://127.0.0.1:8787
    python meme_terminal.py serve --allow-remote  # adds an access code (preview/LAN)
    python meme_terminal.py agents BTCUSDT        # run the 100 agents once
    python meme_terminal.py backtest BTCUSDT      # chronological, cost-aware test
    python meme_terminal.py train BTCUSDT --save  # same, and store the model
    python meme_terminal.py verify-key            # read-only permission check
    python meme_terminal.py selftest              # offline test suite

Safety: PAPER is the default. Live Spot trading needs stored credentials, a
passing pre-flight, and typing the confirmation phrase in the UI. Nothing in
this program can withdraw funds, and it never asks for a seed phrase.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from memeterm import __version__                                  # noqa: E402
from memeterm.agents import REGISTRY, TEAMS                        # noqa: E402
from memeterm.backtest import Backtester, collect_candles          # noqa: E402
from memeterm.binance import BinanceError                          # noqa: E402
from memeterm.config import ConfigError, TerminalConfig            # noqa: E402
from memeterm.engine import LIVE_CONFIRMATION, EngineError, TradingEngine  # noqa: E402
from memeterm.server import TerminalServer                         # noqa: E402
from memeterm.util import fmt_money, parse_day_ms, utc_str          # noqa: E402


BANNER = r"""
  ___  ___ __ __ ___   ___ ___ _  _ _____ ____    _
 |  \/  | \ \ / / __| / __| __| \| |_   _| __|  /_\  __ _ ___ _ _  ___
 | |\/| |  \ V /| _|  | (_ | _|| .` | | | | _|  / _ \/ _` / -_) ' \(_-<
 |_|  |_|   \_/ |___|  \___|___|_|\_| |_| |___|/_/ \_\__, \___|_||_/__/
                                                     |___/
 AI trading terminal · Binance Spot · PAPER by default · v{}
""".format(__version__)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def build_config(args: argparse.Namespace) -> TerminalConfig:
    cfg = TerminalConfig.load(data_dir=args.data_dir).with_env()
    if getattr(args, "port", None):
        cfg = cfg.copy(port=int(args.port))
    if getattr(args, "host", None):
        cfg = cfg.copy(host=str(args.host))
    if getattr(args, "allow_remote", False):
        cfg = cfg.copy(allow_remote=True)
    if getattr(args, "demo", False):
        cfg = cfg.copy(demo=True)
    return cfg.validate()


def build_engine(cfg: TerminalConfig) -> TradingEngine:
    if getattr(cfg, "demo", False):
        from memeterm.demo import DemoTransport
        return TradingEngine(cfg, transport=DemoTransport())
    return TradingEngine(cfg)


def print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str, sort_keys=False))


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_serve(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    engine = build_engine(cfg)
    remote = cfg.host not in ("127.0.0.1", "localhost", "::1")
    server = TerminalServer(cfg, engine, host=cfg.host, port=cfg.port,
                            quiet=args.quiet)
    url = server.start()
    engine.start_background()

    print(BANNER)
    print("  data directory : {}".format(cfg.resolved_dir()))
    print("  mode           : {}  (live trading requires an explicit activation)".format(
        engine.mode))
    if getattr(cfg, "demo", False):
        print("  DATA           : DEMO - synthetic prices, no exchange connection,")
        print("                   and signed endpoints are disabled by construction.")
    print("  exchange       : {}  (Spot only)".format(cfg.api_base))
    print("  telegram       : {}".format("on" if engine.notifier.enabled else "off"))
    print()
    if remote:
        print("  dashboard      : {}   <-- bound beyond loopback".format(url))
        print("  ACCESS CODE    : {}".format(server.access_code))
        print("  The access code is shown once here and never returned by the API.")
    else:
        print("  dashboard      : {}   (loopback only)".format(url))
    print("  open it in the Android browser at the address above; add it to your")
    print("  home screen for a full-screen terminal. Ctrl+C stops cleanly.")
    print()

    stopping = {"done": False}

    def shutdown(signum=None, frame=None):
        if stopping["done"]:
            return
        stopping["done"] = True
        print("\nstopping: saving state (open positions are kept, not sold)…")
        engine.shutdown()
        server.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        while not stopping["done"]:
            time.sleep(0.4)
    except KeyboardInterrupt:
        shutdown()
    print("bye. state saved to {}".format(os.path.join(cfg.resolved_dir(), "state.json")))
    return 0


def cmd_agents(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    engine = build_engine(cfg)
    symbol = (args.symbol or cfg.watchlist[0]).upper()
    cfg_ok = engine.refresh_market()
    if not cfg_ok.get("ok"):
        print("market data unavailable: {}".format(cfg_ok.get("error")))
        print("(agents will report missing data instead of inventing values)")
    consensus = engine.run_agents(symbol)
    if args.json:
        print_json(consensus)
        return 0 if consensus.get("agents_total") == 100 else 1
    print(BANNER)
    print(" symbol           : {}".format(symbol))
    print(" agents           : {} ({} ok, {} no data, {} error)".format(
        consensus["agents_total"], consensus["agents_ok"],
        consensus["agents_no_data"], consensus["agents_error"]))
    print(" verdict          : {}   score {:+.1f}/100   coverage {:.0%}".format(
        consensus["verdict"], consensus["score"], consensus["confidence"]))
    print(" latency          : {:.0f} ms".format(consensus["latency_ms"]))
    if consensus["vetoes"]:
        print(" vetoes           :")
        for veto in consensus["vetoes"]:
            print("   - {} [{}] {}".format(veto["role"], veto["agent_id"],
                                           ", ".join(veto["codes"])))
    print("\n teams")
    for team in consensus["teams"]:
        print("   {:<22} {:+.2f}  {} ({} agents)".format(
            team["name"], team["score"], team["verdict"], team["count"]))
    print("\n top evidence")
    for row in consensus["top_evidence"][:6]:
        print("   {:+.2f}  {:<32} {}".format(row["score"], row["role"],
                                             ", ".join(row["codes"])))
    if consensus["missing_sources"]:
        print("\n missing data     : {}".format(", ".join(consensus["missing_sources"])))
    print("\n A consensus is research, not an order. PAPER mode by default.")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    engine = build_engine(cfg)
    symbol = (args.symbol or cfg.watchlist[0]).upper()
    start_raw = getattr(args, "start", None)
    end_raw = getattr(args, "end", None)
    start_ms = end_ms = None
    if start_raw or end_raw:
        if not (start_raw and end_raw):
            print("--start and --end must be given together")
            return 2
        try:
            start_ms = parse_day_ms(start_raw)
            end_ms = parse_day_ms(end_raw)
        except ValueError as exc:
            print("bad date: {}".format(exc))
            return 2
        if end_ms <= start_ms:
            print("--end must be after --start")
            return 2
        if end_ms - start_ms > 400 * 86_400_000:
            print("range is longer than 400 days; narrow it")
            return 2
    candles = collect_candles(engine.client, symbol, args.interval, args.bars,
                              start_ms=start_ms, end_ms=end_ms)
    if not candles:
        print("no historical candles returned for {} (offline or bad symbol)".format(symbol))
        return 2
    label = re.sub(r"[^A-Za-z0-9._-]", "", str(getattr(args, "label", "") or ""))[:24]
    report = engine.backtester.run(candles, symbol=symbol, interval=args.interval,
                                   horizon=args.horizon, persist=args.save,
                                   label=label)
    if args.json:
        print_json(report)
        return 0 if report.get("ok") else 1
    if not report.get("ok"):
        print("backtest failed: {}".format(report.get("error")))
        return 1
    m, b = report["metrics"], report["baseline"]
    print(BANNER)
    print(" {} {}   {} usable bars   costs {} bps fee + {} bps slippage".format(
        report["symbol"], report["interval"], report["bars"]["usable"],
        report["costs"]["fee_bps"], report["costs"]["slippage_bps"]))
    print(" split: train {} / validation {} / test {} (chronological, embargo {})".format(
        report["bars"]["train"], report["bars"]["validation"], report["bars"]["test"],
        report["split"]["embargo_bars"]))
    if start_ms:
        print(" window: {} .. {}".format(utc_str(start_ms / 1000.0),
                                        utc_str(end_ms / 1000.0)))
    print()
    print(" {:<26} {:>12} {:>12}".format("metric", "model", "baseline"))
    for label, key in [("net return %", "net_return_pct"), ("trades", "trades"),
                       ("win rate %", "win_rate_pct"), ("max drawdown %", "max_drawdown_pct"),
                       ("expectancy / trade", "expectancy")]:
        print(" {:<26} {:>12.3f} {:>12.3f}".format(label, float(m.get(key) or 0),
                                                   float(b.get(key) or 0)))
    print(" {:<26} {:>12.3f} {:>12}".format("precision", float(m.get("precision") or 0), "—"))
    print(" {:<26} {:>12.3f} {:>12}".format("false positive rate",
                                            float(m.get("false_positive_rate") or 0), "—"))
    print()
    print(" sample sufficient: {}".format("yes" if m["sample_sufficient"] else
                                          "NO ({} < {} test trades)".format(
                                              m["trades"], m["min_test_trades"])))
    print(" beats baseline   : {}".format("yes" if m["beats_baseline"] else "no"))
    if report.get("model", {}).get("path"):
        print(" model saved      : {}".format(report["model"]["path"]))
    print("\n A backtest is a hypothesis, not permission to trade. Live mode is")
    print(" never enabled by a backtest result.")
    return 0


def cmd_verify_key(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    engine = build_engine(cfg)
    if not engine.secrets.get():
        print("no API key stored. Start `python meme_terminal.py serve`, open Settings")
        print("and save a Spot-only key pair first (or set it in the browser).")
        return 2
    result = engine.test_connection()
    checks = result.get("checks", [])
    print(BANNER)
    print(" connection test (read-only, no order placed)")
    for check in checks:
        print("   [{}] {:<18} {}".format("ok" if check["ok"] else "!!", check["name"],
                                         check.get("detail", "")))
    for warning in result.get("warnings", []):
        print("   WARNING: {}".format(warning))
    perms = result.get("permissions") or {}
    if perms:
        print("\n permissions: {}".format(", ".join(perms.get("permissions") or [])))
        print(" can_trade  : {}".format(perms.get("can_trade")))
        print(" can_withdraw: {}  <-- must be False; this program never withdraws".format(
            perms.get("can_withdraw")))
        if perms.get("taker_fee_bps"):
            print(" taker fee  : {} bps".format(perms["taker_fee_bps"]))
    return 0 if result.get("ok") else 1


def cmd_status(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    engine = build_engine(cfg)
    snapshot = engine.state_snapshot()
    portfolio = snapshot["portfolio"]
    print(BANNER)
    print(" mode        : {}".format(snapshot["mode"]))
    print(" equity      : {}".format(fmt_money(portfolio["equity"])))
    print(" available   : {}".format(fmt_money(portfolio["quote_balance"])))
    print(" day pnl     : {}".format(fmt_money(portfolio["day_pnl"])))
    print(" positions   : {} / {}".format(portfolio["open_positions"],
                                          portfolio["max_positions"]))
    print(" exposure    : {} / {}".format(fmt_money(portfolio["exposure"]),
                                          fmt_money(portfolio["max_exposure"])))
    print(" daily loss  : {} of {}".format(fmt_money(portfolio["daily_loss"]["used"]),
                                           fmt_money(portfolio["daily_loss"]["limit"])))
    print(" emergency   : {}".format(portfolio["emergency_stop"]))
    print(" key stored  : {}".format(snapshot["connection"]["connected"]))
    print(" agents      : {}".format("run" if snapshot["agents"]["consensus"]
                                      else "not run yet"))
    print(" data dir    : {}".format(cfg.resolved_dir()))
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    tests_dir = os.path.join(HERE, "tests")
    print(BANNER)
    print("running the offline test suite (mocked exchange, no network, no orders)\n")
    command = [sys.executable, "-m", "unittest", "discover", "-s", tests_dir, "-v"]
    result = subprocess.run(command, cwd=HERE)
    return 0 if result.returncode == 0 else 1


def cmd_registry(args: argparse.Namespace) -> int:
    print(BANNER)
    if args.json:
        print_json([agent.as_dict() for agent in REGISTRY])
        return 0
    for team in TEAMS:
        members = [a for a in REGISTRY if a.team == team["key"]]
        print("{:<22} {} agents  (weight {})".format(team["name"], len(members),
                                                     team["weight"]))
        for agent in members:
            print("   {}  {}".format(agent.agent_id, agent.role))
        print()
    print("total: {} agents in {} teams".format(len(REGISTRY), len(TEAMS)))
    return 0


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meme_terminal.py",
        description="Meme Agents AI Trading Terminal (Binance Spot, paper first).",
        epilog="Live trading is opt-in per session and never restored on restart. "
               "This program cannot withdraw funds and never asks for a seed phrase.")
    parser.add_argument("--version", action="version",
                        version="meme-terminal {}".format(__version__))
    parser.add_argument("--data-dir", default=None,
                        help="state/config directory (default ~/.meme_terminal)")
    parser.add_argument("--port", type=int, default=None, help="dashboard port (default 8787)")
    parser.add_argument("--host", default=None,
                        help="bind address; 127.0.0.1 by default")
    parser.add_argument("--allow-remote", action="store_true",
                        help="allow binding beyond loopback (requires the printed access code)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--demo", action="store_true",
                        help="synthetic market data (clearly labelled; no exchange calls)")

    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="start the terminal and the dashboard")
    serve.add_argument("--quiet", action="store_true", help="do not log HTTP requests")
    serve.set_defaults(func=cmd_serve)

    agents = sub.add_parser("agents", help="run the 100 agents once against live data")
    agents.add_argument("symbol", nargs="?", help="Binance Spot symbol, e.g. BTCUSDT")
    agents.set_defaults(func=cmd_agents)

    backtest = sub.add_parser("backtest", help="run a chronological backtest")
    backtest.add_argument("symbol", nargs="?", help="Binance Spot symbol")
    backtest.add_argument("--interval", default="15m")
    backtest.add_argument("--bars", type=int, default=1500)
    backtest.add_argument("--horizon", type=int, default=6)
    backtest.add_argument("--start", help="window start, YYYY-MM-DD (needs --end)")
    backtest.add_argument("--end", help="window end, YYYY-MM-DD (needs --start)")
    backtest.add_argument("--label", help="short label appended to the saved model version")
    backtest.add_argument("--save", action="store_true", help="store the model version")
    backtest.set_defaults(func=cmd_backtest)

    train = sub.add_parser("train", help="backtest and persist a model version")
    train.add_argument("symbol", nargs="?", help="Binance Spot symbol")
    train.add_argument("--interval", default="15m")
    train.add_argument("--bars", type=int, default=2000)
    train.add_argument("--horizon", type=int, default=6)
    train.add_argument("--start", help="window start, YYYY-MM-DD (needs --end)")
    train.add_argument("--end", help="window end, YYYY-MM-DD (needs --start)")
    train.add_argument("--label", help="short label appended to the saved model version")
    train.add_argument("--save", action="store_true", default=True)
    train.set_defaults(func=cmd_backtest, save=True)

    verify = sub.add_parser("verify-key", help="read-only API key and permission check")
    verify.set_defaults(func=cmd_verify_key)

    status = sub.add_parser("status", help="print the current terminal state")
    status.set_defaults(func=cmd_status)

    registry = sub.add_parser("registry", help="list the 100 agents")
    registry.set_defaults(func=cmd_registry)

    selftest = sub.add_parser("selftest", help="run the offline test suite")
    selftest.set_defaults(func=cmd_selftest)

    return parser


GLOBAL_WITH_VALUE = {"--data-dir", "--port", "--host"}
GLOBAL_FLAGS = {"--allow-remote", "--json", "--quiet", "--demo"}


def normalize_argv(argv: List[str]) -> List[str]:
    """Let `serve --port 8791` work as well as `--port 8791 serve`."""
    head: List[str] = []
    rest: List[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        name = token.split("=", 1)[0]
        if name in GLOBAL_WITH_VALUE:
            if "=" in token:
                head.append(token)
                index += 1
            elif index + 1 < len(argv):
                head.extend([token, argv[index + 1]])
                index += 2
            else:
                head.append(token)
                index += 1
            continue
        if token in GLOBAL_FLAGS:
            head.append(token)
            index += 1
            continue
        rest.append(token)
        index += 1
    return head + rest


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(normalize_argv(raw))
    if not getattr(args, "command", None):
        args = parser.parse_args((argv or []) + ["serve"])
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print("configuration refused: {}".format(exc))
        return 2
    except EngineError as exc:
        print("refused: {}".format(exc))
        return 2
    except BinanceError as exc:
        print("exchange error: {}".format(exc))
        return 3
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
