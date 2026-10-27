# Meme Agents AI Trading Terminal (Binance Spot)

A local, **paper-first** trading terminal for Binance Spot, running on the same
standard-library-only stack as the rest of this repository. It adds a browser
dashboard, 100 research agents, a cost-aware backtester and a guarded live-trading
path on top of the existing `meme_agents.py` bot. The Solana research bot keeps
working exactly as before; nothing in this terminal touches it.

> **Crypto trading can lose money. Most short-term strategies lose money after
> fees.** This is a small, careful tool with hard limits - not income, not
> financial advice, and not a prediction engine. PAPER is the default; live
> trading is opt-in per session and cannot survive a restart.

---

## 1. What is actually implemented

| Area | Status |
|---|---|
| Binance Spot public data (exchangeInfo, ticker, klines, depth, trades) | implemented |
| Signed account access (balances, permissions, orders, fills) | implemented, HMAC-SHA256 |
| Symbol filters, step/tick rounding, min-notional validation | implemented and tested |
| Paper execution with fees + slippage, ledger, restart-safe state | implemented and tested |
| Live Spot execution with idempotent `clientOrderId` | implemented, **never exercised against the real exchange in this session** |
| Ambiguous order handling (timeout → query, never resend) | implemented and tested with a fake exchange |
| Local dashboard (mobile-first, charts, 5 pages) | implemented |
| 100 agents in 10 teams, ensemble score, vetoes | implemented and tested |
| Backtesting with chronological splits, embargo, fees, baseline | implemented and tested |
| Model versioning (pure-Python logistic regression) | implemented and tested |
| Telegram alerts with de-duplication | implemented and tested (mocked transport) |
| Live price streaming (WebSocket) | **not implemented** - polling every 4-5 s |
| Margin, futures, leverage, withdrawals, transfers | **deliberately absent** |
| Seed phrases / wallet private keys | **never requested, never stored** |

## 2. Install on Termux

```bash
pkg update && pkg upgrade -y
pkg install -y python
cd ~/my-otp-phish          # the cloned repository
python meme_terminal.py --version
python meme_terminal.py selftest      # 57 offline tests, no network, ~8 s
```

No pip packages, no compiler, no `solders`. The browser UI is three static files
served by the same Python process.

## 3. Start it

```bash
python meme_terminal.py serve
```

It prints:

```
  data directory : /data/data/com.termux/files/home/.meme_terminal
  mode           : PAPER  (live trading requires an explicit activation)
  exchange       : https://api.binance.com  (Spot only)
  telegram       : off
  dashboard      : http://127.0.0.1:8787   (loopback only)
```

Open **<http://127.0.0.1:8787>** in the Android browser (same device, same
phone). Add it to the home screen for a full-screen terminal. The server binds
`127.0.0.1` only; nothing on your Wi-Fi can reach it.

Useful variants:

```bash
python meme_terminal.py serve --port 9000          # different port
python meme_terminal.py serve --demo               # synthetic data, clearly labelled
python meme_terminal.py serve --allow-remote --host 0.0.0.0   # LAN / preview proxy
```

`--allow-remote` prints a one-time **access code**. The dashboard then asks for
it before serving any data: the session cookie is HttpOnly + SameSite=Strict,
and every state-changing request needs a CSRF token. Loopback is *not* an
encryption boundary - see §9.

## 4. Binance API key - the safe way

1. In Binance: **Profile → API Management → Create API**. Label it
   `meme-terminal-read-and-spot-trade`.
2. On the key's permissions: enable **Enable Spot & Margin Trading** *for spot
   only*, and **leave "Enable Withdrawals" OFF**. This program has no withdrawal
   code path, and a key that can withdraw is a key that can empty the account if
   the phone is lost.
3. Restrict the key to your IP if Binance offers it for your region. If your
   mobile IP changes constantly, consider skipping live trading entirely.
4. In the terminal: **Settings → Exchange connection** → paste the key and
   secret → **Save locally** → **Test connection**.
   * The key is posted only to `127.0.0.1`, stored in
     `~/.meme_terminal/credentials.json` with mode `0600` (directory `0700`).
   * The UI shows only a mask (`abcd…wxyz`) and a one-way fingerprint.
   * **Delete key** removes the file and drops any live mode immediately.
5. `Test connection` is read-only: time sync, market data, key permissions,
   symbol filters. It never places an order.

> If you ever cannot verify the file permissions on your device, treat the phone
> as the security boundary and keep the key spot-only with withdrawals disabled.

## 5. PAPER → LIVE SPOT

* **PAPER** is the default after every start, with or without API keys.
* Saving keys does **not** enable live trading.
* To go live: **Settings → Trading mode → Live Spot**. The pre-flight must pass
  (key stored, connection tested, SPOT permission, trading enabled, withdrawals
  disabled, no emergency stop, filters loaded). Then type the exact phrase
  `I ACCEPT THE RISK`, tick the acknowledgement, and press **Activate live Spot**.
* A red **LIVE SPOT TRADING** banner appears at the top of every page.
* Live mode is **session-only**: a restart, a deleted key, a failed permission
  check or the emergency stop all drop it back to paper.
* **STOP** (top right) blocks new entries and cancels open orders. Open
  positions are **not** market-sold automatically - deliberately, because that
  would realise losses at whatever price the book offers. Close them from the
  dashboard when you decide to.
* Ctrl+C saves state and exits; positions stay in `state.json` and reappear on
  the next start. Nothing is sold on shutdown.

Default limits (all editable in Settings, all validated server-side, all with
hard ceilings):

| Limit | Default | Ceiling |
|---|---|---|
| Max order size | 10 USDT | 2,000 |
| Max total exposure | 20 USDT | 10,000 |
| Per-symbol exposure | 10 USDT | 2,000 |
| Daily loss limit | 3 USDT | 1,000 |
| Max concurrent positions | 3 | 20 |

No leverage, no borrowing, no averaging down, no size increase after a loss.

## 6. The 100 agents

Ten teams of ten. The brief asked for 10×8 + 5 + 5 (= 90); the extra ten were
split 5/5 between portfolio and backtest so the registry is exactly 100, which
the tests assert.

| Team | Weight | What it watches |
|---|---|---|
| Market discovery | 0.7 | liquidity floor, movers, volume surge, trend filters, venue health |
| Data validation | 1.4 | feed age, kline continuity, OHLC sanity, zero-volume bars, kline-vs-ticker agreement |
| Momentum & volume | 1.2 | EMA crosses, RSI, MACD, breakouts, volume z-score, taker buy pressure |
| Liquidity & book | 1.1 | spread, book imbalance, depth value, slippage for a 10 USDT order, trade rate |
| Technical analysis | 0.9 | Bollinger, ATR, range position, slopes, higher highs, VWAP |
| Security & anomalies | 1.5 | symbol status, exchange minimums, wash-trade pattern, pump signature, spread anomaly, chase guard |
| News & social | 0.5 | optional public research; reports "unavailable", never invents sentiment |
| Cross-market | 0.7 | BTC correlation/beta/regime, breadth, volatility regime, relative strength |
| Portfolio & risk | 1.3 | exposure, position count, daily-loss budget, drawdown, fee drag, kill switch |
| Backtest & model | 0.8 | model present/age, sample size, out-of-sample return, leakage guard, baseline, live drift |

Every finding carries `source`, `timestamp`, `confidence`, `missing[]` and
`reason_codes[]`. **Vetoes** (data stale, duplicate candles, crossed book,
not-tradable symbol, withdrawal-safe exchange rules, wash trading, chase risk,
daily loss breached, emergency stop, leakage guard failure) override any bullish
score: the consensus becomes `REJECT`.

Agents are pure functions over one cached snapshot, executed through a bounded
worker pool with a per-agent deadline - 100 logical agents, one set of HTTP
requests, no 100 processes on a phone.

## 7. Backtest and train

```bash
python meme_terminal.py backtest BTCUSDT --interval 15m --bars 1500
python meme_terminal.py train    ETHUSDT --interval 1h  --bars 3000 --save
```

What the pipeline does, in order:

1. Downloads history (paginated, 1000 bars per call).
2. Builds features for every bar `t` using **only** bars `<= t` (a test
   recomputes features on every prefix to prove it).
3. Labels: did the price clear **fees + slippage** over the next `horizon` bars?
4. Splits **chronologically** 60/20/20 with an embargo of `horizon` bars.
5. Trains a pure-Python logistic regression; the decision threshold is chosen on
   the validation split, never on the holdout.
6. Simulates the holdout: decision at bar close, fill at the next open, fees and
   slippage on both sides.
7. Prints sample size, net return, expectancy, drawdown, precision, false
   positive rate, and the same metrics for a momentum **baseline**.

A report with fewer than 30 holdout trades is labelled **insufficient sample**.
A good backtest never enables live trading; there is no code path from a report
to live mode.

Real output from `--demo` (synthetic ETH, 1h bars, 1,400 requested / 1,333 usable,
horizon 6). It is a deliberately unflattering example, because that is what the
pipeline usually says when costs are realistic:

```
 ETHUSDT 1h   1333 usable bars   costs 10.0 bps fee + 10.0 bps slippage
 split: train 793 / validation 261 / test 267 (chronological, embargo 6)

 metric                            model     baseline
 net return %                     -0.305       -0.613
 trades                            9.000       21.000
 win rate %                       22.220       19.050
 max drawdown %                   -0.305       -0.647
 expectancy / trade               -0.034       -0.029
 precision                         0.108            —
 false positive rate               0.153            —

 sample sufficient: NO (9 < 30 test trades)
 beats baseline   : no
```

Read that as: the model lost less than the momentum baseline, but with nine
trades the difference is noise, and the report says so instead of selling you a
strategy.

## 8. Commands

```bash
python meme_terminal.py serve              # dashboard + engine
python meme_terminal.py status             # state summary in the terminal
python meme_terminal.py agents BTCUSDT     # run 100 agents once
python meme_terminal.py registry           # list all 100 roles
python meme_terminal.py backtest BTCUSDT --interval 15m --bars 1500
python meme_terminal.py train BTCUSDT --interval 1h --bars 3000 --save
python meme_terminal.py verify-key         # read-only key/permission check
python meme_terminal.py selftest           # offline test suite
```

Global flags: `--data-dir`, `--port`, `--host`, `--allow-remote`, `--demo`,
`--json`. They may appear before or after the subcommand.

## 9. Security model, honestly

* **Loopback by default.** `127.0.0.1:8787`, no LAN access unless you ask for it
  with `--allow-remote`, which requires the printed access code.
* **Loopback is not encryption.** Malware, a rooted device or a backup can read
  `credentials.json`. The mitigations are a spot-only key with withdrawals
  disabled and deleting the key when you are done.
* **No secrets in the browser.** The key is posted once, stored server-side, and
  never returned. `GET /api/state` and `GET /api/settings` expose only a mask and
  a fingerprint (both asserted in tests).
* **CSRF + origin checks.** Mutations require `X-CSRF-Token` and a same-origin
  `Origin`/`Sec-Fetch-Site`; a foreign `Origin` and an unexpected `Host` are both
  rejected (tested).
* **CSP.** `default-src 'self'`, no inline script, no CDN, no analytics. The UI
  works offline.
* **Audit log.** `audit.jsonl` records orders, rejections, mode changes,
  credential events and connection tests, with secrets redacted. Downloadable
  from Settings.
* **No withdrawals.** The withdrawal endpoint is not implemented anywhere.

## 10. Known limits

1. **Live trading was never executed against the real exchange in this session.**
   This sandbox has no outbound network; every exchange interaction in the tests
   is a mocked transport. The request/response shapes follow the official Spot
   REST documentation (fetched 2026-10-27), but treat the first live order as a
   test with 10 USDT you can afford to lose.
2. **Polling, not streaming.** Prices refresh every 4-5 s. No user-data stream,
   so fills are reconciled by polling open orders.
3. **Region restrictions.** Binance.com is unavailable in some jurisdictions
   (the US uses binance.us, which has a different API). Check what applies to
   you; this program does not work around geo-restrictions.
4. **Backtests are not evidence of future returns.** One symbol, one regime, a
   fixed slippage assumption, and a small holdout. Treat a green report as a
   hypothesis to test in paper mode.
5. **Fee model.** Taker fee defaults to 0.10 % and is refreshed from the account
   when available. BNB discounts and maker rebates are not modelled in paper
   fills.
6. **Partial fills** are booked when the exchange reports them, but paper limit
   orders fill completely or not at all.
7. **No margin/futures/leverage**, by design.
8. **The 100 agents are heuristics**, not a trained ensemble. Their value is
   consistent bookkeeping and vetoes, not alpha.

## 11. Files

```
meme_terminal.py        CLI entry point
memeterm/
  binance.py            Spot REST client (public + signed), filters, errors
  config.py             validated limits with hard ceilings
  secrets_store.py      0600 credential file, masking, deletion
  store.py              state.json, trades.csv ledger, audit.jsonl
  execution.py          paper + live brokers and the single order manager
  risk.py               server-side order checks with reason codes
  agents.py             100 agents, 10 teams, ensemble + vetoes
  market.py             cached market data (tickers, klines, depth, trades)
  backtest.py           features, splits, logistic model, simulation, metrics
  notify.py             optional Telegram with de-duplication
  server.py             HTTP API, security headers, CSRF, static UI
  demo.py               synthetic data for offline evaluation (cannot trade)
  web/                  index.html, app.js, styles.css (no build step)
tests/test_memeterm.py  57 offline tests
```

## 12. Warning

Crypto assets are volatile and largely unregulated. Meme coins and low-liquidity
altcoins can go to zero, and on Binance Spot your position is your asset - there
is no insurance, no chargeback and no support line that can undo a bad trade.
With a 10 USDT order and 0.1 % fees on each side, a round trip needs roughly
0.2 % of movement just to break even, before spread and slippage. Only use money
you can lose entirely, and use PAPER until you have watched the terminal for a
while.
