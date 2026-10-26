# meme_agents.py

One Python file. 100 asyncio agents. A ~10 USD paper/live meme-coin bot that runs on a
non-root Android phone (Termux), discovers new Solana tokens from public APIs, filters
scams, scores momentum, and trades in tiny fixed size with hard risk limits.

**Paper mode is the default and needs zero third-party packages.** Live mode needs
`solders` (Solana transaction signing) and a burner wallet.

> **On "sure signals".** There is no such thing as a sure signal in memecoins, and no
> amount of agent count changes that. What this version does instead is make entries
> *harder* (stricter score, real tape required, every filter re-checked on a second look),
> run safely across restarts, and **measure itself** (`--analyze`), so you can see whether
> the rules actually paid rather than guessing.
>
> **The risk, plainly.** Most meme coins go to zero. The filters here reject most scams
> most of the time — not all scams all the time. You can lose the entire 10 USD, and at
> this size fixed Solana costs eat a large share of every position. A green paper run is
> not a prediction of a green live run. This is a risky toy-sized experiment, not income.

---

## 1. Install (Termux, no root)

```bash
pkg update && pkg upgrade -y
pkg install -y python

# PAPER MODE: nothing else. The script is 100% standard library.

# LIVE MODE ONLY (optional; compiles Rust, can be slow or fail on a phone):
pkg install -y rust binutils libsodium
pip install --upgrade pip wheel
pip install solders
```

No `aiohttp`, no `requests`, no API keys required for market data.

---

## 2. Run

### Paper mode (default, simulated fills, no real money)

```bash
python meme_agents.py
```

Ctrl+C (or creating a file named `STOP` in the working directory) closes all open
positions, prints a summary and exits cleanly.

### Offline self test — no network, no keys, no money

```bash
python meme_agents.py --selftest
```

Runs 72 assertions: every safety filter, copycat/name and tape-quality gates, the
second-look confirmation, momentum scoring, the full exit ladder
(stop-loss, +100 %/+300 % take-profits, 15 %/10 % trailing, time stop, all three
emergencies), paper fill maths, the live execution path against a fake `solders` +
fake RPC (including "a failed send is never retried"), and a 26-second end-to-end
pipeline run against canned API payloads, a state save/restore round trip, and the
`--analyze` report. Also proves realised PnL reconciles with equity.

### 24-hour forward paper test

```bash
python meme_agents.py --backtest-free-run          # 24h, then prints + saves a report
python meme_agents.py --backtest-free-run 6        # 6h
python meme_agents.py --free-run-hours 0.5         # same thing, explicit hours
```

There is no historical data for "a token appeared 12 minutes ago", so this is a
**forward** test in paper mode, not a backtest. The report is written to
`free_run_report.txt`, and the signal-quality report is printed at the end.

### 24/7 operation (survives restarts, phone sleeps, crashes)

```bash
termux-wake-lock                              # optional: keep the CPU awake

# restart loop: if the process dies, it comes back with the same portfolio
while true; do
  python meme_agents.py --paper
  echo "restarting in 10s (Ctrl+C twice to stop)"; sleep 10
done
```

Every position, the cash balance, the daily-loss counter and the cooldown list are written
to `state.json` after every change (atomic write: temp file + `fsync` + rename), so a
restart resumes instead of forgetting. `heartbeat.json` is rewritten every 10 s
(`heartbeat_every_s`, independent of the dashboard) — use it from another terminal to
check the bot is alive:

```bash
python -c "import json,time;d=json.load(open('heartbeat.json'));print(round(time.time()-d['epoch']),'s ago |',d['open_positions'],'open |',round(d['equity_usd'],2),'USD')"
```

State is **per mode**: a `state.json` from a live run is refused by paper mode and vice
versa. Start fresh on purpose with `--no-state`, or move the file away.

### `--analyze` — did the filters actually work?

```bash
python meme_agents.py --analyze                 # reads trades.csv from the config default
python meme_agents.py --analyze /path/to/trades.csv
```

Prints a signal-quality report from the ledger alone: how many first looks were held,
how many became confirmed signals, how many became buys, win rate, realized PnL, fees
paid, profit factor, expectancy, best/worst trade, the **top rejection reasons counted
per filter**, and PnL **by momentum-score bucket** (does a higher score make more money?).
It ends with a deliberately blunt verdict:

* fewer than ~20 closed trades → `NOT ENOUGH DATA`, the numbers are noise;
* negative → `NEGATIVE`, do not run it with real money;
* profit factor < 1.2 → `MARGINAL`, one bad trade erases it;
* otherwise `POSITIVE so far` — evidence, not proof.

That verdict is the honest answer to "give me only sure signals": it tells you whether
these rules earned their keep on *your* trades, and refuses to pretend otherwise.

### Live mode — burner wallet only

```bash
export SOLANA_PRIVATE_KEY='<base58 secret key of a BURNER wallet>'
export SOLANA_RPC_URL='https://api.mainnet-beta.solana.com'   # optional
export TELEGRAM_BOT_TOKEN='...'                                # optional
export TELEGRAM_CHAT_ID='...'                                  # optional
python meme_agents.py --live
# then type exactly:  I ACCEPT THE RISK
```

Live mode refuses to start without `--live`, the typed confirmation **and** the
`SOLANA_PRIVATE_KEY` environment variable. The key is never written to disk and never
logged (bot tokens and API keys are redacted from URLs in logs too).

`JUPITER_API_KEY` is optional: without it the keyless Jupiter endpoint is used at a lower
rate limit.

**Fund the burner with only the money you intend to risk.** The bot warns loudly if the
wallet holds much more than its configured capital.

---

## 3. Files it writes

| File | Contents |
|---|---|
| `trades.csv` | every decision: `PASS_SAFETY`, `REJECT` (+reason), `SIGNAL`, `VETO`, `BUY`, `SELL`, `EMERGENCY`, `CLOSE`, `BUY_FAIL`, `SELL_FAIL`, `HALT`. Flushed on every row. |
| `bot.log` | rotating log (4 MB × 3), UTC timestamps, debug detail for every rejection. |
| `free_run_report.txt` / `final_report.txt` | summary + honest reminders at shutdown. |
| `state.json` | restart-safe portfolio: cash, open positions, daily-loss counter, cooldowns, recent reject counts. Atomic write. |
| `heartbeat.json` | liveness snapshot every 10 s (equity, positions, HTTP counters, feed health). |
| `trades.csv.<timestamp>.csv` | automatic backup whenever the ledger schema changes, so old runs are never mixed into a new column layout. |
| `STOP` | create this file to trigger the kill switch. |

---

## 4. How it works

```
scouts (24)          safety (24)            momentum (22)         confirmation       executors (29)
poll 5 feeds ──► enrich + 20 hard checks ──► score tape ──► park 25s, re-fetch ──► risk veto, buy,
                 (2+ providers needed)      (>=70, real tape)  and re-check all ──► manage exits
                                                                                    
                          risk manager (1): daily loss stop, kill switch, absolute veto
```

* **One shared HTTP client** for all 100 tasks: per-host rate limiting (GeckoTerminal
  2.1 s between calls / 25 per minute, DexScreener 0.25 s / 180 per minute, CoinGecko 6.5 s),
  TTL cache, single-flight (24 scouts asking the same URL make **one** request),
  exponential backoff with jitter, `Retry-After` support, gzip and an 8 MB body cap.
* **Data sources, exactly these:** DexScreener (`token-profiles`, `token-boosts`,
  `token-pairs`), GeckoTerminal (`new_pools`, `trending_pools`, `pools/{p}/trades`),
  CoinGecko (`/coins/list/new`, paid tier; keyless fallback is GeckoTerminal
  `/tokens/info_recently_updated`), RugCheck (`/v1/tokens/{mint}/report`), Jupiter
  (quotes, and real swaps in live mode). Telegram is send-only.
* **Signal quality — two gates before anything is bought.**
  1. *Real tape:* ≥ 12 trades in the last 5 minutes, ≥ 6 unique buyers, ≥ $200 of 5 m
     volume. A gorgeous buy/sell ratio over $11 of trades is noise, not a signal.
  2. *Second look:* every candidate that passes the score is parked for
     `confirm_delay_s` (25 s), then re-fetched from fresh endpoints, re-scored and
     re-filtered. Price must not have dropped more than 5 %, the score must still clear
     the (now stricter) threshold of 70, and every safety check must still pass.
     Only then is a `SIGNAL` written. The ledger keeps the whole funnel:
     `AWAIT_CONFIRM` → `SIGNAL` or `REJECT … confirm_*`.
* **Safety filters** (any failure = no trade): liquidity ≥ $50 000; top-10 holders
  ≤ 30 % (pool/locker/vault accounts excluded where RugCheck tags them); mint and freeze
  authority revoked; LP ≥ 90 % locked/burned; creator still holding ≤ 10 %; pair age
  10 min – 6 h; 24 h change ≤ +400 % (already >5x = too late); 5 m change ≤ +40 %
  (never chase); sells/buys ≥ 0.2 over 1 h; RugCheck score ≤ 40 with no danger flags;
  a **simulated $2 sell** through Jupiter must route with ≤ 3 % price impact;
  the symbol/name must not impersonate a major ticker (USDC, SOL, USDT…), contain
  invisible/control characters or read like bait ("claim", "airdrop", "visit …");
  at least **2 independent providers** must have answered (DexScreener, GeckoTerminal,
  RugCheck) so a single flaky API cannot manufacture a candidate; and only **one open
  position per symbol** is allowed, which blocks copycat clones of a name you already
  hold.
  Unknown safety data is treated as a failure (`strict_unknown=True`), so strict filters
  mean **it may trade rarely or not at all** — that is the intended trade-off.
* **Exits:** hard −20 % stop; sell 50 % of the position at +100 %; 25 % at +300 %;
  trailing stop on the remainder, 15 % below the highest price seen, tightened to 10 %
  once the position is up 500 %; emergency exit on a 40 % liquidity drop (or zero
  liquidity), a 3× sell-volume spike, a creator dump of ≥20 % of their balance since we
  entered, or a RugCheck "rugged" flag; 2-hour time stop for positions that never got
  +10 %.

---

## 5. Config

Everything is in the `Config` dataclass at the top of the file (grep for
`SECTION 2. CONFIG`). Useful flags:

```
--live                    real swaps through Jupiter (needs key + confirmation)
--paper                   explicit default
--backtest-free-run [H]   forward paper test, default 24h
--free-run-hours H        same, explicit
--max-runtime-hours H     absolute stop
--threshold 0-100         momentum entry threshold (default 70)
--trade-usd X             trade size, clamped to the hard $2 cap
--dashboard-every S       dashboard interval (default 30)
--ledger PATH / --log-file PATH
--cg-key KEY              optional CoinGecko key for /coins/list/new
--analyze [CSV]           signal-quality report from the ledger, then exit
--state PATH              state file (default state.json)
--no-state                do not save or restore state
--selftest                offline test suite
```

Hard caps that the CLI will not let you exceed: `$2` per trade, `3` open positions,
`$3` daily loss, 3 % entry slippage, 8 % emergency-exit slippage.

Signal-quality knobs in `Config`: `momentum_threshold=70`, `min_corroboration=2`,
`confirm_delay_s=25.0`, `confirm_max_drop_pct=5.0`, `min_trades_5m=12`,
`min_unique_buyers_5m=6`, `min_tape_volume_5m_usd=200`, `reject_copycats=True`,
`max_positions_per_symbol=1`. Tighten them and the bot trades less; that is the point.
Loosen them and you are back to buying noise — `--analyze` will show you the bill.

---

## 6. Known limits and failure modes (honest list)

1. **Most meme coins go to zero.** The filters lower the odds of buying a rug; they do
   not turn a lottery into an edge. Expect a high loss rate; the ladder is designed so a
   rare winner can pay for several losers, and it may not.
2. **You cannot sell at the peak.** No script can. The trailing stop gives back 10–15 %
   from the high — that is the practical approximation, and it is a compromise.
3. **Small capital is structurally disadvantaged.** At $2 per trade the fixed costs
   dominate: token-account rent ≈ 0.00204 SOL (≈ $0.30–0.40, and this bot does **not**
   refund it on exit), plus priority + network fees. In the self test one $2 round trip
   cost ~$0.34 in fees before any price move. At $10 total this is a huge drag.
4. **"Creator sold more than 20 %" is not knowable at discovery time.** Nobody publishes
   the creator's original allocation. What is implemented: reject if the creator still
   holds >10 % now, and remember their balance at entry and emergency-exit if it drops
   ≥20 % while we hold. A dev who sold before we looked is invisible to this bot.
5. **The honeypot check is a quote, not a simulation.** It proves a $2 sell route exists
   with acceptable price impact. A token with a transfer hook, blacklist or pause can
   still block a real sell. Solana has no simulate-sell endpoint for an unheld token.
6. **Public APIs are not an exchange feed.** They are delayed, rate-limited, sometimes
   wrong, and they change shape without notice. Under heavy rate limiting the bot simply
   does less; with the strict filters, that usually means no trades.
7. **"Already risen 5x" uses the 24 h change** as a proxy, because GeckoTerminal and
   DexScreener do not expose a launch price for very new pairs. A token that pumped and
   dumped inside 24 h may look calm.
8. **Paper fills are a model, not reality.** Slippage is `latency + trade/(liquidity/2)`,
   fees are estimates, rent is charged and never refunded. Paper ignores MEV, failed
   transactions, sandwich attacks, and a dev front-running your sell. Real results are
   worse.
9. **Live mode is one RPC endpoint away from trouble.** The default public RPC is heavily
   rate limited; confirmation timeouts are handled by checking the on-chain token balance
   and *never* re-buying, but a stalled RPC can leave a position unmanaged until the next
   poll.
10. **100 agents is concurrency, not parallelism.** They are asyncio tasks in one
    process, sharing one event loop. A phone will do this fine, but nothing here is
    "100x faster"; the swarm is a structure for rate-limited work, not free compute.
11. **Kill switch caveat.** Ctrl+C / `STOP` tries to close everything, but if the network
    or the RPC is down at that moment the sell fails and the position stays open — the
    bot logs `SELL_FAIL` and tells you. Always check the wallet after a live shutdown.
12. **A cooldown, not a memory.** Rejected and exited mints are skipped for 15–30 minutes;
    the bot can re-buy the same loser later in the day if it passes the filters again.
13. **"Only sure signals" is not a thing this can deliver.** The stricter gates here
    (score ≥ 70, real 5-minute tape, 2+ providers, second look) cut the number of trades
    hard — a quiet day may produce **zero** entries, and that is the filter working, not
    a bug. What improved is the *quality of the evidence* per entry, not the outcome odds.
14. **The 25-second second look costs you the fastest moves.** A token that rips inside
    the confirmation window is either bought later (worse price) or rejected by the
    price-drop check on the way back down. Missing a winner is the price of not buying
    the noise.
15. **`state.json` is trusted.** Hand-editing it, or restoring it after manually trading
    the wallet, corrupts the accounting. Live mode re-reads on-chain token balances at
    startup, but entry prices, peaks and the ladder state come from the file. Delete it
    (`--no-state`, or move it away) if you want a clean slate.
16. **`heartbeat.json` proves the loop is alive, not that the prices are.** A stale-feed
    warning fires after 5 minutes without a price, but between that and the next alert a
    position can sit unmanaged.
17. **Not audited.** This is a single-file hobby bot written in one sitting. Read the
    code before you fund it, and treat the $10 as already spent.

---

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `STARTUP FAILED: LIVE mode needs the 'solders' package` | `pkg install rust binutils libsodium && pip install solders`, or stay in paper mode. |
| No trades for hours | Expected with strict filters: liquidity ≥ $50k, LP locked, authorities revoked, age ≥ 10 min, impact ≤ 3 %, tape ≥ 12 trades/6 buyers/$200, and a second look 25 s later. Lower `--threshold` or relax `Config` — at your own risk. |
| Lots of `confirm_*` rejections | The second look is doing its job: the setup died in 25 s. Run `--analyze` to see which filter dominates. |
| `state file is from live mode, current mode is paper` | Deliberate. Delete or move `state.json`, or pass `--state other.json`. |
| Restart resumes with an old position | That is the point. It is managed with the normal exit ladder from the saved peak. |
| `REJECT ... sell_route` | Jupiter's keyless endpoint is rate limited (0.5 rps) or has no route. Set `JUPITER_API_KEY`. |
| Dashboard shows rising `5xx`/`net` | Public APIs are down or blocking you. The bot backs off; nothing else to do. |
| `SELL_FAIL` in `trades.csv` during shutdown | Check the wallet in a Solana explorer; the position is still yours. |
| Log grows fast | `bot.log` rotates at 4 MB × 3; `trades.csv` is small (one row per decision). |
