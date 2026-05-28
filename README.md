# AI Hummingbot Brain — V6 (Tiny OKX Live Tester)

> V6 adds an **opt-in, $5–$25, spot-only** live tester on top of V5.4 paper realism, V5.2 OKX rate-limit fixes and the V5.3 Auto Strategy UI fix — all preserved. The tester is **disabled by default** and refuses to place a real order until every env flag, OKX permission, and safety cap is satisfied. There is no full reckless live mode in V6. No leverage, no margin, no perps, no withdrawals.

## V5.4 — what changed and why

V5.3 paper runs were producing equity $102.52 with $0.00 cash, ~27 trades on a $100 starting balance, and at least one **impossible take-profit fill** (USDG/USDT closed at TP at a price the live market never reached). Those numbers are not safe to treat as live-ready. V5.4 fixes the realism gap without enabling any real-money trading:

1. **Paper profile** (`paper_profile`). Two modes:
   - `live_readiness` (default) — conservative defaults; only these trades count toward the demo/live gate.
   - `learning` — looser exploration. Learning samples are explicitly **excluded** from the readiness gate.
2. **Capital realism.** `min_cash_reserve_pct` (default 35%) and `max_capital_in_positions_pct` (default 50%) are enforced inside `can_enter` AND clamp open quantity inside `open_long`. Cash can no longer drain to zero by default.
3. **Frequency realism.** Global cooldown (`global_trade_cooldown_seconds`, default 600s) and per-symbol cooldown (`per_symbol_cooldown_seconds`, default 2700s). `live_readiness` clamps `max_trades_per_day` to ≤ 12. Learning samples cut cooldowns in half but stay non-zero.
4. **Fill realism.** `realistic_fills_enabled` (default true): market buys fill at `ask × (1 + slippage)`, market sells at `bid × (1 − slippage)`, fees charged on full notional.
5. **Exit verification — the impossible-TP fix.** `exit_verification_enabled` (default true). A paper exit only triggers if live ticker `bid`/`ask`/last, or the most recent candle high/low, proves the price was actually reachable. Long TP requires `max(last, ask, candle_high) ≥ trigger`; long SL requires `min(last, bid, candle_low) ≤ trigger`. Refused exits emit an `exit_blocked` SSE event; the position stays open. Closed trades carry `exit_validated`, `exit_trigger_source`, `exit_trigger_price`, `market_bid/ask/last`, and `candle_high/low` for full audit.
6. **Stable/pegged/synthetic exclusion.** `paper_excluded_bases` defaults include `USDG, USDC, RLUSD, DAI, TUSD, FDUSD, PYUSD, BUSD, USDK, EURT, GUSD, USDD, USTC, PAXG, XAUT, BETH, STETH, OKSOL, WBTC, WETH`. Engine adds a last-line `USD*` / `*USD` block as a safety net. Dynamic discovery never adds these.
7. **Symbol-adaptive intelligence.** `app/services/symbol_profile.py` builds a per-symbol `SymbolProfile` from live OKX ticker + candles (ATR%, spread%, 24h move, quote volume, bias, RSI, vol ratio). The strategy filter drops symbols that are dead/ungovernable/wide-spread/thin. Tradeable symbols receive adaptive multipliers (calm → tighter SL/TP, hot → wider SL/TP + smaller size; wide spread / thin liquidity / choppy → smaller size). Auto-strategy uses the profile as a soft hint per coin.
8. **Readiness gate realism.** The gate now counts **only** trades where `entry_kind != learning_sample`, `exit_validated != false`, and `paper_profile == live_readiness`. The dashboard shows `eligible / total` closed trades and surfaces any unverified exits.

### Why live money is still locked

The USDG/USDT impossible-TP bug means the previous paper run cannot count toward live readiness. V5.4 fixes the bug, but the *new* paper run has to accumulate fresh eligible trades before the gate opens. **No real-money execution path was added in V5.4.** The roadmap section below describes the planned V6 "tiny live tester" mode.

### Live Tester readiness checklist (informational — gate is still LOCKED)

Before any future V6 tiny-live-tester build is considered, this paper run must show:

- [ ] `paper_profile = live_readiness`
- [ ] `closed_eligible ≥ live_min_closed_trades` (default 50)
- [ ] `eligible_win_rate ≥ live_min_win_rate`
- [ ] `eligible_total_pnl_usdt ≥ live_min_total_pnl_usdt`
- [ ] `unverified_exits == 0` over the evaluation window
- [ ] `data_quality.ok == true` and `data_quality.score ≥ live_min_data_quality_score`
- [ ] OKX private key configured **read-only / spot-only** (no withdrawal permission, no margin/perps)
- [ ] `leverage_enabled == false`
- [ ] `live_max_order_usdt` capped to a tiny amount (≤ \$5–\$10)
- [ ] `daily_loss_cap_pct` set tight (≤ 2–3 %)
- [ ] Manual confirmation tokens: `LIVE_DEMO_COMPLETED_ACK`, `LIVE_TRADING_ACK`, `LIVE_TRADING_ENABLED=true`

All of those are visible in the Demo / Live gate card. The dashboard explicitly tells you which items remain.

### Roadmap — V6 Tiny Live Tester (NOT in this build)

A future V6 build can route a single small spot order through the OKX adapter as a tester, gated by every checklist item above plus explicit per-order user confirmation. V5.4 deliberately does **not** ship that path — the realism proof run has to come first.

**Full V6 design notes:** see [`docs/V6_LIVE_TESTER_DESIGN.md`](docs/V6_LIVE_TESTER_DESIGN.md). It maps each of the user's specific live concerns — wrong order value, stop-loss attachment failures, duplicate entries into the same coin, opportunity-comparison swaps, and verifying that SL/trail orders are actually live on the exchange — onto concrete V5.4 hooks that V6 will inherit. V5.4 already enforces a duplicate-symbol guard inside `PaperEngine.can_enter()` so the bot cannot pyramid into a coin it already holds; that same guard becomes V6's first line of defence.

**V6 execution transport:** the preferred path is [OKX Agent TradeKit](https://app.okx.com/en-au/agent-tradekit) (MCP server + CLI + `okx-cex-market` / `okx-cex-trade` / `okx-cex-portfolio` skills). Local-signed, sub-account-isolated, demo mode, read-only mode, per-trade approval, and native OCO/trailing-stop support map cleanly onto our readiness checklist. The design doc breaks down phased rollout (read-only → demo → tiny live) and the risks still to investigate before V6 commits. **V5.4 does not integrate TradeKit** — hooks and notes only.

## V5.3 — what changed

* **Auto Strategy chip is now a clickable toggle.** The chip on the Control deck (next to the strategy arsenal) acts as a one-click ON/OFF switch for `auto_strategy_selection`. It is keyboard-focusable, shows `aria-pressed`, and is updated immediately after the POST returns (no waiting for the next scan).
* **Drawer dropdown highlighted.** The Settings drawer’s **Auto Strategy Selection** row is now visually emphasised at the top of the V5.1 section, with `ON` / `OFF` labels instead of `enabled` / `disabled`, so it can’t be missed.
* **Reflects state on load.** The chip is populated from `state.health.settings.auto_strategy_selection` on first health response, not only when a scan SSE event arrives. Cold-start dashboards now correctly show the current state.
* **CSS guardrails.** New `.v51-chip-toggle` styles ensure the chip is clearly clickable (cursor, hover, focus ring), with no overlay or pointer-event regressions to the rest of the UI.

No `app/` Python files changed in V5.3. The settings allowlist, dataclass, env loader, persistence and reset paths for `auto_strategy_selection` were already correct in V5.2 — V5.3 just makes the UI control obvious and instantly responsive.

---

# AI Hummingbot Brain — V5.2 (OKX rate-limit hotfix)

**AI crypto trading command center, paper-first.** A dark, fast, single-page dashboard backed by a FastAPI service that watches live OKX prices, scans 1h/15m market structure, asks a four-agent OpenRouter committee for a structured verdict, and simulates paper fills with stop-loss / take-profit / trailing-stop. **V5 adds a strict training gate** that blocks OKX demo and live modes until your Hermes journal has enough closed paper trades, a positive win rate, positive P/L, and clean data quality.

> **THE BOT STARTS IN PAPER MODE AND STAYS THERE BY DEFAULT.** Switching to OKX demo or live requires a passing training gate and (for live) explicit env acknowledgements plus a tiny `LIVE_MAX_ORDER_USDT` cap. The OKX adapter is **spot-only** with no withdrawals, no transfers, no perps, and no leverage. The leverage settings exposed in this build are a **paper/demo simulator** — they do not enable real-money leverage. **No profit is guaranteed.** This is research / educational simulation.

---

## V5.2 — OKX rate-limit hotfix (this release)

V5.2 is a targeted hotfix on top of V5.1. It does not change any execution
gate or unlock real money. It fixes the V5.1 complaint of "the dashboard
says DATA HOLD and never trades" by making the market layer kind to OKX
public REST and the strategy layer kind to imperfect symbols.

### What changed

1. **Bulk tickers** — the market layer now pulls all spot tickers in ONE
   call (`GET /api/v5/market/tickers?instType=SPOT`) and serves every
   symbol's price from that snapshot. The per-symbol `/market/ticker`
   endpoint is no longer used in the hot loop.
2. **Snapshot cache** — the bulk pull is cached for `OKX_TICKER_BULK_TTL_SECONDS`
   (default 2s). A 1-second price-watch loop re-reads the cache and never
   hits OKX once per symbol per second.
3. **Candle TTL cache** — 1m/3m/5m/15m/30m candles cached for
   `OKX_CANDLE_TTL_SHORT_SECONDS` (default 45s); 1h/4h/1d for
   `OKX_CANDLE_TTL_LONG_SECONDS` (default 240s). A scan now refetches
   candles only when the cache has actually expired, so a 28-symbol
   universe usually issues 0–3 candle calls per scan instead of 56.
4. **429 backoff** — when OKX returns HTTP 429 or code 50011, the market
   layer enters a global cooldown for `OKX_RATE_LIMIT_COOLDOWN_SECONDS`
   (default 20s). While in cooldown, outbound calls return cached data
   instead of hammering. A per-symbol cooldown
   (`OKX_PER_SYMBOL_COOLDOWN_SECONDS`, default 60s) is also applied to
   instruments that fail individually, so one broken symbol can't burn
   the whole quota. Status is exposed at `/api/health.market_status.rate_limited`.
5. **Safer dynamic universe defaults**
   - `MAX_DYNAMIC_SYMBOLS` default lowered **20 → 10** and the server-side
     clamp tightened from 100 to 30.
   - Stable / wrapped / staked / gold / synthetic bases are excluded by
     default in discovery: `USDT, USDC, DAI, TUSD, BUSD, FDUSD, PYUSD,
     XAUT, PAXG, BETH, STETH, RETH, WBETH, CBETH, WSTETH, WBTC, WETH,
     WBNB, WMATIC, WAVAX, WFTM, OKSOL, OKBTC, OKETH, USDD, USTC, LUNC,
     …` plus a `DYNAMIC_EXCLUDE_BASES` setting you can extend.
6. **Skip-not-block data quality** — `market.quality_report(...)` no
   longer returns one yes/no for the whole universe. It returns
   `clean_symbols` (safe to trade), `skipped` (per-symbol reason), and
   per-source-level `ok`. The strategy layer scans/trades the clean list
   and lists the skipped ones in the UI. **DATA HOLD now only fires when
   the OKX public source is broadly broken (not live, ticker bulk stale
   > 60s) or zero clean symbols remain.**
7. **Current task line** — reads like
   `scanning 16 clean markets — best watched: SOL/USDT · Pullback Sniper ·
   score 0.41 (below training floor 0.42); skipped 5 (rate-limited/insufficient)`
   instead of `DATA HOLD — PAXG ticker stale by 43s`.
8. **Universe panel** shows a `Clean / Skipped` line with the first few
   skip reasons inline. When 429 cooldown is active the line shows
   `[rate-limited]`.

### Same gates, same safety

- The Hermes demo/live training gate is **unchanged**.
- Real OKX execution is still locked. Paper mode is still the only thing
  that auto-runs.
- `learning_sample` and `training` entries still require
  `execution_mode == "paper"`.
- Leverage simulator behaviour is preserved — spot-only, no real leverage.

### Tuning V5.2

| Variable | Default | What it does |
|---|---|---|
| `OKX_TICKER_BULK_TTL_SECONDS` | 2 | All-tickers cache window. |
| `OKX_CANDLE_TTL_SHORT_SECONDS` | 45 | TTL for 1m/3m/5m/15m/30m. |
| `OKX_CANDLE_TTL_LONG_SECONDS` | 240 | TTL for 1h/4h/1d. |
| `OKX_RATE_LIMIT_COOLDOWN_SECONDS` | 20 | Pause after a 429. |
| `OKX_PER_SYMBOL_COOLDOWN_SECONDS` | 60 | Pause for one bad symbol. |
| `OKX_CANDLE_REQUIRED_BARS` | 60 | Min bars for "clean" symbol. |
| `OKX_CANDLE_FETCH_CONCURRENCY` | 4 | Max parallel candle fetches. |
| `MAX_DYNAMIC_SYMBOLS` | 10 | Hard-clamped 0–30. |
| `DYNAMIC_EXCLUDE_BASES` | PAXG,XAUT,BETH,STETH,OKSOL,WBTC,WETH | Appended to built-in blocklist. |

---

## V5 — demo / live training gate

V5 introduces a single readiness endpoint and a dashboard panel that explains, in plain English, exactly why OKX execution is locked.

**Endpoint:** `GET /api/live/readiness`

Returns `execution_mode`, `can_execute`, `reasons[]`, `training_gate{}`, `okx{}`, and `leverage{}`. The dashboard polls this and renders a Demo/Live Gate card on the main grid.

**Default gate requirements (override in `.env` or Settings drawer):**

| Requirement | Default | Notes |
| --- | --- | --- |
| Closed paper trades | 30 | Bumped to 60 when leverage is enabled. |
| Win rate | 55% | Across all closed Hermes-graded trades. |
| Net P/L | ≥ 0 USDT | Must be non-negative on the paper journal. |
| Data quality | ≥ 90 / 100 | Live OKX ticker + candle health. |
| OKX private | authenticated | Required for okx_demo / okx_live. |
| Live order cap | 10 USDT default | Clamped 1–50 USDT server-side. |

**To unlock OKX demo (no real money):**

1. Run paper mode long enough to satisfy the gate above (`/api/live/readiness` returns no reasons).
2. Add `OKX_API_KEY` / `OKX_API_SECRET` / `OKX_API_PASSPHRASE` (Trade + Read scopes, withdrawals OFF).
3. Set `OKX_DEMO=true` and `EXECUTION_MODE=okx_demo`.
4. Restart the server. The Gate card should turn green and the top pill should read `OKX DEMO — READY`.

**To unlock OKX live (real money, spot-only, tiny):**

1. Complete a successful okx_demo run first.
2. Set in `.env`:
   ```env
   EXECUTION_MODE=okx_live
   OKX_DEMO=false
   LIVE_TRADING_ENABLED=true
   LIVE_TRADING_ACK=I_ACCEPT_REAL_MONEY_RISK
   LIVE_DEMO_COMPLETED_ACK=I_RAN_OKX_DEMO_FIRST
   LIVE_MAX_ORDER_USDT=10
   LEVERAGE_ENABLED=false
   ```
3. Restart. The gate will refuse if any requirement (training, OKX, env acks) is missing.

> **Important:** Even when `/api/live/readiness` returns `can_execute: true`, this build does **not** auto-place real OKX orders. The strategy engine continues to journal paper fills. The OKX private adapter is currently a signed-readiness probe plus guarded spot market `buy/sell` helpers (with `clOrdId` required, `LIVE_MAX_ORDER_USDT` enforcement, and no margin/perps). Real-money autonomy is intentionally deferred until exits/stops are wired end-to-end on the exchange side. See `app/services/okx_private.py`.

## V5.1 — Active paper training, auto-strategy, dynamic discovery, learning samples

V5.1 fixes the V5 complaint of “the bot just says no buy setup all the time.” It does **not** weaken the V5 demo/live training gate — OKX demo/live execution still requires the Hermes journal to pass training, OKX credentials, and (for live) explicit env acknowledgements with a tiny `LIVE_MAX_ORDER_USDT` cap. Everything below only widens what happens in **paper mode**.

### Three entry kinds

| Kind | Where it runs | Size | Threshold | Label in journal/UI |
|---|---|---|---|---|
| `standard` | paper / okx_demo / okx_live (gate permitting) | full risk | AI committee `proceed` + score ≥ `confidence_threshold` | normal reason text |
| `training` | **paper only** | `PAPER_TRAINING_SIZE_PCT` of normal (default 50%) | score ≥ `PAPER_TRAINING_MIN_SCORE` (default 0.42) | `[EXPLORATORY PAPER TRAINING] ...` |
| `learning_sample` | **paper only** | `LEARNING_SAMPLE_SIZE_PCT` of normal (default 30%) | score ≥ `LEARNING_SAMPLE_MIN_SCORE` (default 0.30) + safety filters | `[PAPER LEARNING SAMPLE — not live-ready signal] ...` |

`training` and `learning_sample` entries **never execute on OKX demo or live**, regardless of `EXECUTION_MODE`. The strategy engine hard-checks `execution_mode == "paper"` before allowing them through.

### Auto strategy selection

When `AUTO_STRATEGY_SELECTION=true` (default), every scan ranks all five live strategies (`trend_rider`, `pullback_sniper`, `breakout_hunter`, `mean_reversion`, `volume_surge`) on indicator score plus a Hermes historical win-rate bonus (capped ±0.05). The bot picks the best candidate symbol × strategy in one pass. When off, the bot uses the manually selected `ACTIVE_STRATEGY`. The dashboard shows an **Auto Strategy** chip with the current pick and the reason.

### Dynamic symbol discovery

When `DYNAMIC_SYMBOL_DISCOVERY=true` (default), the market service pulls the OKX public spot tickers feed (`/api/v5/market/tickers?instType=SPOT`), filters for `USDT` quote, 24h quote volume ≥ `DYNAMIC_MIN_QUOTE_VOLUME_USDT`, tight spread, and sane price data. Up to `MAX_DYNAMIC_SYMBOLS` (default 20) discovered symbols are added to the scan universe alongside the configured core symbols. Result is cached for 15 minutes. Discovery only widens **paper** scanning; demo/live still respect the explicit `SYMBOLS` whitelist for safety.

### Transparent “no trade” task line

When the bot does not take a trade, `current_task` now reports the best watched candidate and the exact block reason, e.g.

```
Watching SOL/USDT (pullback_sniper, score 0.41) — below training threshold 0.42
```

### V5.1 settings reference

All exposed in the Settings drawer and `.env.example`:

| Setting | Default | Purpose |
|---|---|---|
| `ACTIVE_PAPER_TRAINING` | `true` | Enable the training-entry pathway. |
| `PAPER_TRAINING_MIN_SCORE` | `0.42` | Min indicator score for a training entry. |
| `PAPER_TRAINING_ALLOW_EXPLORATION` | `true` | Allow training entries below normal confidence threshold. |
| `PAPER_TRAINING_SIZE_PCT` | `50` | % of normal risk used for training entries. |
| `PAPER_TRAINING_MAX_DAILY_TRADES` | `12` | Cap on training entries per UTC day. |
| `AUTO_STRATEGY_SELECTION` | `true` | Auto-pick best strategy per scan. |
| `DYNAMIC_SYMBOL_DISCOVERY` | `true` | Add liquid OKX USDT pairs to paper scan universe. |
| `MAX_DYNAMIC_SYMBOLS` | `20` | Cap on discovered symbols. |
| `DYNAMIC_MIN_QUOTE_VOLUME_USDT` | `2_000_000` | 24h quote-volume floor for discovered symbols. |
| `LEARNING_SAMPLE_ENABLED` | `true` | Enable paper-only learning samples. |
| `LEARNING_SAMPLE_MIN_SCORE` | `0.30` | Min score for a learning sample. |
| `LEARNING_SAMPLE_SIZE_PCT` | `30` | % of normal risk for learning samples. |
| `LEARNING_SAMPLE_MAX_PER_DAY` | `6` | Cap on learning samples per UTC day. |

### Safety guarantees (unchanged from V5)

- All trading decisions still require real OKX data and `data_quality` ok. No fake or simulated market data is ever introduced by V5.1.
- The deterministic indicator fallback is clearly labelled in agent reason text. No claim of real AI is made if no OpenRouter call happened.
- Live execution is hard-locked at the code level (`paper_mode_locked=True` in `EnvFlags`); `/api/live/execute` returns HTTP 423.
- Demo/live readiness still requires the Hermes training gate plus env acks.
- `training` and `learning_sample` entries are **paper-only** and refuse to run in any other `execution_mode`.

## Leverage — paper/demo simulator only

The user asked for leverage. The honest answer is: real leverage is the fastest way to lose your money, and this build does **not** wire real OKX perps. What V5 does add is a **leverage simulator** so you can train and stress-test before requesting a perps adapter.

- Toggle in Settings (or `LEVERAGE_ENABLED=true`).
- `leverage_multiplier` (default 1.0) is capped by `leverage_max_multiplier` (default 3, max 10 in the UI).
- Paper engine scales notional by leverage, charges fees on full notional, and records a `liquidation_price` derived from `leverage_liquidation_buffer_pct` (default 80% of margin). Hitting that price closes the position with `exit_reason="liquidated"`.
- When leverage is enabled, the training gate requires more closed trades (`leverage_extra_min_closed_trades`, default 60) and a stricter daily loss cap (`leverage_max_daily_loss_pct`, default 3%).
- The gate **refuses** to mark okx_live as ready while leverage is enabled. Turn it off for live.

## Quick start commands

```bash
# 1. copy the env template
cp .env.example .env
# 2. add your OpenRouter key, optional OKX keys, leave EXECUTION_MODE=paper
# 3. install + run
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8787
# 4. open http://127.0.0.1:8787 and start the paper bot. Watch the Demo/Live Gate card.
```

With Docker: `docker compose up --build` (same result, port 8787).

---

## What you get

- **Dark command-center UI** — control deck, paper portfolio, AI committee cards, market scan table, trade log, live event console. No Freqtrade-ugly defaults.
- **Real public market data** — OKX public REST candles + tickers via HTTP. If the live source fails, the UI shows `Data: OFFLINE — NO LIVE DATA` and the bot refuses to enter trades.
- **Fast price watcher** — refreshes live tickers every second by default so open paper positions can update stops/trailing logic without waiting for the next full AI scan.
- **Pro-style timeframe stack** — default higher-timeframe bias is 1h, setup timeframe is 15m, and fast price watching is used only for timing/management.
- **Hermes learning mode** — persistent paper-trading journal that records AI decisions, links them to paper positions, grades closed trades, and scores models/strategies over time.
- **AI brain committee** (OpenRouter) — four roles: **Commander**, **Scout**, **Risk**, **Skeptic**. Each returns strict JSON: `{vote, confidence, reason, risk_notes}`. The backend aggregates votes; the AI **never** places orders.
- **Selectable strategy arsenal** — Commander Blend, Trend Rider, Pullback Sniper, Breakout Hunter, Mean Reversion, Volume Surge, and Safe Observer.
- **Transparent indicators** — EMA9/21/50 stack, RSI(14), ATR(14), 20-bar high/low, volume ratio. Each strategy mode uses these differently.
- **Paper engine** — simulated fills against live price, configurable fees + slippage, SL / TP / trailing-stop, daily loss cap, max trades/day, max open positions (default 1). Position size is recalculated from current equity on every new entry, so drawdowns automatically shrink the next trade. Ride-winners mode can move TP higher after the first target while the trailing stop protects profit.
- **Data-quality gate** — every full scan validates OKX ticker freshness and candle availability for both the setup and bias timeframes. If data is stale or incomplete, the bot holds instead of guessing.
- **One-command Docker** — `docker compose up --build` → http://127.0.0.1:8787.

---

## Quick start (Windows / PowerShell)

You need **Docker Desktop** running.

```powershell
# 1. Open PowerShell in the project folder
cd C:\path\to\AI_Hummingbot_Brain

# 2. Copy the env template
Copy-Item .env.example .env

# 3. Open .env in Notepad and paste your OpenRouter key after OPENROUTER_API_KEY=
notepad .env

# 4. Build and run
docker compose up --build

# 5. Open the dashboard
Start-Process "http://127.0.0.1:8787"
```

To stop:

```powershell
# Ctrl+C in the docker compose window, then:
docker compose down
```

---

## Quick start (macOS / Linux)

```bash
cd ai-hummingbot-brain
cp .env.example .env
# edit .env and paste your key
docker compose up --build
# open http://127.0.0.1:8787
```

---

## Getting an OpenRouter API key

1. Go to https://openrouter.ai and sign up.
2. Visit https://openrouter.ai/keys and create a key.
3. Paste it into `.env` after `OPENROUTER_API_KEY=`. Restart the container.
4. The default models are cheap (`openai/gpt-4o-mini`, `google/gemini-2.5-flash-lite`, `deepseek/deepseek-chat`). You can swap any of them in the Settings drawer at runtime, or change defaults in `.env`.

---

## How to know it's working

In the topbar:

- **PAPER MODE — LIVE LOCKED** — always shown. This build cannot place real orders.
- **AI: ONLINE** (green) — OpenRouter key valid and a recent verdict succeeded.
- **AI: OFFLINE (no API key)** (red) — no key set; bot will not enter trades unless `indicator_only_mode` is enabled in Settings.
- **Data: LIVE (OKX)** (green) — public market data is fresh (<90s old).
- **Data: OFFLINE — NO LIVE DATA** (red) — OKX requests are failing; bot refuses to enter.

Click **Find best trade now** to force one scan + AI deliberation cycle. The AI cards will show each model's vote, confidence, reason, and latency. Watch the **Event console** at the bottom for the full stream.

Click **Start paper bot** to run continuously. By default, live tickers refresh every 1 second and the full 1h/15m AI scan runs every 8 seconds.

---

## Controls

| Button | What it does |
|---|---|
| **Start paper bot** | Runs the scan loop continuously. |
| **Stop** | Halts the loop. Open positions keep their SL/TP/trailing exits at next tick. |
| **Find best trade now** | One-shot scan + AI deliberation. |
| **Reset paper account** | Clears cash, P/L, and trade log. Uses current `starting_balance_usdt` setting. |
| **Settings** | Drawer for balance, active strategy, risk %, SL/TP/trailing %, fees, slippage, model names, confidence threshold, 1h/15m timeframes, watcher speed, and AI timeout. |

---

## Strategy modes

| Strategy | What it hunts | Risk style |
|---|---|---|
| **Commander Blend** | Picks the best local setup across the full arsenal, then asks the AI committee to challenge it. | Medium |
| **Trend Rider** | EMA9 > EMA21 > EMA50 momentum where price is holding trend. | Medium |
| **Pullback Sniper** | Controlled dips near EMA21 inside an uptrend. | Low-medium |
| **Breakout Hunter** | 20-bar high breaks with volume expansion. | Medium-high |
| **Mean Reversion** | Oversold pushes near the 20-bar low where a snapback may form. | High |
| **Volume Surge** | Unusual participation where price reclaims short-term trend levels. | Medium-high |
| **Safe Observer** | Only the cleanest setups. Fewer trades, stricter filter. | Low |

The AI brain sees the active strategy mode and the local setup type in its prompt. If the trade does not match the selected mode, the committee should vote `wait` or `skip`.

### Committee entry rule

The paper bot can enter when at least **2 AI agents vote `proceed`**, proceed votes outnumber skip votes, and the average proceed confidence is above your `confidence_threshold`. A confident `skip` vote at or above the threshold blocks the trade. This keeps paper mode active without letting one uncertain `wait` vote cancel a strong majority.

## Default daily trading logic

The default mode is built to avoid one-minute noise:

1. **1h bias** — the bot checks the higher-timeframe trend first. In this long-only starter, bearish 1h bias blocks new longs.
2. **15m setup** — the bot hunts the selected strategy setup on the setup timeframe.
3. **Fast price watcher** — live tickers refresh every second by default. This is used for open-position SL/TP/trailing management and dashboard prices.
4. **AI committee gate** — the best setup is sent to Commander, Scout, Risk, and Skeptic. The AI votes, but the paper engine still controls size, stops, daily loss cap, and max positions.
5. **Ride winners** — when enabled, the first TP is treated as a milestone. The bot raises the next TP and lets the trailing stop protect the trade if price keeps running.

## Hermes learning mode

Hermes is the self-teaching paper layer. It keeps a persistent journal in `data/hermes_journal.json` and learns from actual paper outcomes:

- Every AI-voted setup is saved with symbol, strategy, 1h/15m context, data-quality score, model votes, latency, confidence, and reasons.
- If a paper position opens, Hermes links the trade back to the exact AI decision that caused it.
- When the trade closes, Hermes grades the result and writes a lesson such as weak volume, overheated RSI, bearish higher-timeframe conflict, or good runner management.
- The dashboard ranks model/role combinations and strategy modes based on closed paper trades.
- Hermes does **not** execute real orders, rewrite strategy code, or bypass risk controls. It only learns and recommends while paper testing.

Treat Hermes recommendations as early evidence until there are at least 10 closed paper trades. More trades are better because small samples can lie.

## Small-account mode

The default starter balance is **100 USDT**, roughly matching a small test account. The risk engine sizes each new paper trade from current equity using:

- `risk_per_trade_pct` — how much account equity can be lost if the stop is hit.
- `max_position_pct` — maximum account exposure in one position.
- `daily_loss_cap_pct` — stops new entries after the account is down too much for the day.

For a real-money version, the live adapter must read the exchange account equity before each new order and use the same rules. The AI should never be trusted to invent position size on its own.

---

## Architecture

```
ai-hummingbot-brain/
├── app/
│   ├── main.py                # FastAPI app: REST + SSE + static
│   ├── core/
│   │   ├── settings.py        # .env loader + persisted settings.json
│   │   └── events.py          # in-process event bus → SSE
│   └── services/
│       ├── market_data.py     # OKX public REST candles/tickers
│       ├── indicators.py      # EMA / RSI / ATR / breakout / pullback
│       ├── ai_brain.py        # OpenRouter committee (4 agents, JSON verdicts)
│       ├── paper_engine.py    # paper account, SL/TP/trail, risk gates
│       └── strategy.py        # scan loop + AI orchestration
├── web/                       # vanilla HTML/CSS/JS dashboard
├── data/                      # settings.json + paper_state.json (mounted volume)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

**Data flow:**

```
OKX public REST  →  MarketData  →  Indicators
                                       │
                                       ▼
                          Selected strategy ranks setups
                                       │
                  ┌────────────────────┴────────────────────┐
                  ▼                                         ▼
       OpenRouter committee                      Indicator-only mode
       (Commander / Scout / Risk / Skeptic)      (optional, when AI key missing)
                  │                                         │
                  └────────────────────┬────────────────────┘
                                       ▼
                        PaperEngine risk gates
                        (max positions, daily loss cap,
                         max trades/day, size caps)
                                       ▼
                            Simulated fill (fee + slippage)
                                       ▼
                           Open position; SL / TP / trail on every tick
```

---

## Why is Hummingbot not actually embedded?

The official Hummingbot client is a heavy CLI/strategy framework. Embedding it here would (a) make Windows installs painful (native deps, conda environments), and (b) create a real path to live orders before you're ready. The architecture here is **Hummingbot-style** — a strategy orchestrator that produces order intents, plus a paper executor — with a clean adapter seam at `app/services/paper_engine.py`. A future release can add a `LiveHummingbotAdapter` that translates the same intents into real Hummingbot connector calls, once safety review and exchange credentials handling are in place.

The endpoint `POST /api/live/execute` exists and explicitly returns **HTTP 423 Locked**. Treat the live adapter as **locked** until a future, audited release.

---

## REST endpoints (for tinkerers)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | Health, AI status, market status, settings, bot state. |
| GET | `/api/settings` | Current settings. |
| POST | `/api/settings` | Patch settings (JSON body). |
| POST | `/api/bot/start` | Start the scan loop. |
| POST | `/api/bot/stop` | Stop the scan loop. |
| POST | `/api/bot/find-best` | One-shot deliberation. |
| GET | `/api/portfolio` | Equity, cash, P/L, open positions. |
| GET | `/api/trades` | Closed trade log. |
| GET | `/api/market` | Refresh tickers and return current state. |
| POST | `/api/paper/reset` | Reset paper account. |
| GET | `/api/stream` | Server-Sent Events (live updates). |
| POST | `/api/live/execute` | **Always returns 423 Locked.** Legacy V5 endpoint — not used by the V6 tester. |
| GET | `/api/okx/account` | Read-only OKX balance + permissions snapshot (V6). |
| GET | `/api/live/account` | Alias of `/api/okx/account` (V6). |
| GET | `/api/live/tester` | Live tester summary: enabled, caps, kill switch, open positions (V6). |
| POST | `/api/live/tester/kill` | Engage the kill switch (V6). |
| POST | `/api/live/tester/release` | Release the kill switch (V6). |
| POST | `/api/live/tester/reconcile` | Force a fills/balance reconcile pass (V6). |

---

## Safety summary

- Live trading is locked at the code level. No env flag will enable it.
- Daily loss cap (default −5%) auto-halts the bot until the next UTC day.
- Max trades/day (default 12) and max open positions (default 1).
- Position size capped by `risk_per_trade_pct` and `max_position_pct`.
- If OKX data is stale, the bot refuses to open new positions.
- If OpenRouter is missing, the bot refuses to open new positions unless `indicator_only_mode` is explicitly enabled in Settings.

**No profit is guaranteed. Past simulation does not predict future results. Do your own research.**

---

## V6 — Tiny OKX Live Tester (setup)

V6 lets you put **real money on the line in tiny amounts** (default cap: $5 USDT per trade, total daily-loss cap $3, up to 3 trades/day, max 1 open live position). It exists so you can validate the OKX signing path, fill semantics, and bot-managed exits with the smallest possible blast radius. Treat it like a hardware smoke test — not a money-printer.

### Required OKX key configuration

1. Sign in to OKX and create a **new API key** dedicated to this tester.
2. **Permissions:** enable **Read** and **Trade** only. Leave **Withdraw OFF**. The tester never calls withdraw/transfer/earn endpoints, but disabling the permission on the key is your hard backstop.
3. **IP allowlist:** restrict the key to the public IP(s) that will run this bot.
4. **Sub-account or tester wallet:** fund a dedicated sub-account or tester wallet with **$5–$10 USDT** to start. Do not point this key at your main trading account.
5. Save the API key, secret, and passphrase — only the redacted fingerprint is shown in the UI; raw values live in `.env` and never leave the host.

### Required env flags

The tester will not place a real order unless **all** of the following are true:

```bash
OKX_API_KEY=...
OKX_API_SECRET=...
OKX_API_PASSPHRASE=...

EXECUTION_MODE=okx_live              # or okx_demo to dry-run signing only
LIVE_TRADING_ENABLED=true            # V5 flag (live transport)
LIVE_TRADING_ACK=I_ACCEPT_REAL_MONEY_RISK
LIVE_DEMO_COMPLETED_ACK=I_RAN_OKX_DEMO_FIRST

LIVE_TESTER_ENABLED=true             # V6 master switch
LIVE_MAX_ORDER_USDT=5                # clamped to [1, 25]
LIVE_DAILY_LOSS_CAP_USDT=3
LIVE_MAX_TRADES_PER_DAY=3
LIVE_ONE_POSITION_PER_SYMBOL=true
LIVE_MAX_OPEN_POSITIONS=1
LIVE_REQUIRE_PROTECTIVE_EXIT=true
LIVE_SPOT_ONLY=true
LIVE_FREE_RESERVE_MULTIPLIER=1.10
```

If the V5.4 paper readiness gate has **not** yet passed and you still want to take a first tiny smoke trade, add:

```bash
LIVE_TESTER_OVERRIDE=I_UNDERSTAND_THIS_IS_A_TINY_TEST
```

The UI shows a banner whenever the override is active. Remove it as soon as the smoke trade is done.

### Bot-managed stops — read this before enabling

V6 does **not** place native OKX algo orders (stop-loss / take-profit / trailing-stop / OCO). Every protective exit is computed inside this process and submitted as a **market sell** when the trigger condition is observed. The Live Tester card displays `stop mode: bot_managed` and a red warning banner to make this explicit.

Consequences:

- **If the app stops, your stop does NOT trigger.** Crashes, host reboots, OOM kills, network drops, and `Ctrl+C` will all leave a live position uncovered until the process restarts.
- The kill switch engages automatically if a sell fails, if the qty is unknown after entry, or if the daily-loss cap is hit. It only blocks **future entries** — you must still close any open position manually if the app is offline.
- Treat `live_max_order_usdt` as the **maximum you are willing to lose to 100% slippage** while the app is down.

Native OKX algo orders are on the roadmap (see `docs/V6_LIVE_TESTER_DESIGN.md` — “Agent TradeKit”) but are out of scope for V6.

### Recommended first-run checklist

1. Run paper-only for long enough to satisfy the V5.4 readiness gate (or accept the `LIVE_TESTER_OVERRIDE` warning).
2. Open the **OKX real account** panel and confirm: configured = yes, authenticated = yes, USDT free ≥ $6, permissions show `Read, Trade` only.
3. Set `EXECUTION_MODE=okx_demo` first and watch the dashboard for `live_tester_attempt` events with status `would_place_order` — these are signed requests against the OKX demo endpoint, no real funds at risk.
4. Switch to `EXECUTION_MODE=okx_live` once you are happy with the demo behaviour. Start with `LIVE_MAX_ORDER_USDT=5` and `LIVE_MAX_TRADES_PER_DAY=1` for the first session.
5. Keep the bot in the foreground of a terminal you can watch. Engage the kill switch from the UI the instant anything looks wrong.

### What V6 still won’t do

- No leverage, margin, perps, futures, or options.
- No withdrawals, transfers, conversions, earn/yield, savings, or staking.
- No “give me all my capital” full-live mode. The $25 per-order ceiling is hard-coded in `settings.py`.
- No native exchange-side algo orders. Stops are bot-managed only.
