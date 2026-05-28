# V7 — Market Visibility Scanner + Professional Dashboard

UI + observability + safer larger-universe scanning. **No new trading
behaviour. No relaxed safety. No native exchange-attached TP/SL.**

This release improves what the operator *sees*. The trading rules,
risk gates, and live-tester caps are unchanged from V6.4. Live trading
is still locked behind the same env tokens and Hermes training journal.

## What changed

### 1. Bigger but safer scan universe
- Core 8 symbols + **top ~20 dynamically discovered** USDT spot pairs by
  liquidity, capped by a new `MAX_SCAN_SYMBOLS` ceiling (default 25, clamp
  8..80).
- Discovery still excludes stables / wrapped / staked / gold per the V5.2
  blocklist; symbol-profile gates (ATR / spread / volume) still apply.
- `SCAN_INTERVAL_SECONDS` default raised 8 → 30 to keep OKX rate-limit
  cooldowns headroom-safe with the larger universe.
- `OKX_CANDLE_FETCH_CONCURRENCY` default lowered 4 → 3 for the same reason.
- New `MARKET_INTEL_PROVIDER` env (`none` / `coingecko`). Default `none` —
  OKX public ticker data is sufficient for all fills. CoinGecko is a
  placeholder for future enrichment and is not required; unknown values
  silently fall back to `none` so offline / no-key use never breaks.

### 2. Backend observability (read-only)
- New `app/services/scanner_activity.py`: thread-safe singleton aggregating
  scan state, top candidates with rejection reasons, last qualified signal
  considered, last live-tester order attempt, and structured blockers.
- New `GET /api/scanner` returns the snapshot plus a freshly-computed
  blocker dict (`full_live_gate`, `tiny_live_tester`, `no_qualified_signal`,
  `existing_position`, `confidence_below_threshold`, `cooldown`,
  `daily_caps`, `insufficient_balance`).
- `GET /api/health` now embeds the scanner snapshot + market-intel provider.
- New SSE event `scanner_activity` published at the end of every scan.
- `app/services/strategy.py`: new `_finalize_scan` helper wired to every
  scan return path (`data_hold`, `no_setup`, `blocked`, `ai_offline_holding`,
  `opened`, `blocked_at_open`, `hold`). Live-tester attempts (refusals at
  okx-auth, tester-gate, pre-flight, and the successful submission path)
  are now recorded on the same activity object. **All instrumentation is
  wrapped in try/except — it can never affect trading flow.**

### 3. Professional dashboard layer
- **Sticky status strip** under the topbar: scanner counts, clean / skipped,
  last-scan age and duration, queue/warming status, and a permanent
  `LONG (spot buy only)` direction badge. Tabular nums everywhere, dark
  trading-console aesthetic, no AI-gradient styling.
- **Scanner activity** card: core + discovered universe, top candidates
  table (symbol / strategy / score / confidence / signal / status), skipped
  symbols with reasons, last qualified signal considered, last live tester
  attempt.
- **Why no live trade?** card: 8 blocker categories with red/green dots,
  per-category reasons, and explicit distinction between *paper bot*,
  *tiny live tester*, and *full live*.
- **Market data source** card: shows the active provider and the
  "OKX public is enough" stance.
- **Live tester visibility** upgrade — *no behaviour change, only display*:
  - Each open live position now shows a prominent **`PROT: BOT-MANAGED`**
    chip plus tabular qty / entry / quote / SL / TP / trail with %-from-
    entry annotations.
  - New **"Last live order"** panel showing the submitted order, fill,
    fee, OKX order id, timestamp, and whether the protective watcher is
    active.
  - New **drift alert** (heuristic): if OKX reports non-USDT balance but
    the bot has no matching bot-managed live position, the UI warns that
    the bot will not watch or exit that holding.
  - New explainer banner: *"OKX order history will show TP and SL as blank
    on the original buy — this build uses bot-managed exits, not native
    exchange-attached algos. If the bot stops while a position is open,
    the stop is no longer being watched. Native exchange-attached TP/SL is
    planned for V8."*
  - V6.4 direction labels (`LONG (spot buy)` badges, "Spot LONG-only"
    messaging, candidate-vs-open-position clarification) are preserved
    end-to-end.
- **"Live tester is not guaranteed to trade immediately"** copy added to
  the live tester card so the operator does not interpret an armed but
  idle tester as a bug.

### 4. Safety preserved
- No forced trades. No lowered live thresholds. No relaxed caps.
- `LIVE_MAX_ORDER_USDT_TESTER`, `LIVE_DAILY_LOSS_CAP_USDT`, and the full
  V6 live-tester gate are unchanged.
- Full-live gate (real money) is still locked behind `LIVE_TRADING_ENABLED`,
  `LIVE_TRADING_ACK`, and the Hermes training journal.
- No shorts, no perps, no leverage, no withdrawals.

## Known limitations / V8 roadmap

- **Native exchange-attached TP / SL (OKX algo orders) is NOT implemented in
  V7.** Live tester continues to use bot-managed exits. OKX order history
  will therefore show TP and SL as blank on the original buy. The bot
  watches every price tick and submits the exit itself. If the bot stops
  while a live position is open, **the stop is no longer being watched**.
  Adding native OKX `attachAlgoOrds` (or one-cancels-other algo orders) is
  the V8 work item.
- CoinGecko enrichment is a placeholder; `MARKET_INTEL_PROVIDER=coingecko`
  currently has no effect beyond surfacing the provider name in the UI.

## Suggested .env additions / changes

```
SCAN_INTERVAL_SECONDS=30
MAX_SCAN_SYMBOLS=25
DYNAMIC_SYMBOL_DISCOVERY=true
MAX_DYNAMIC_SYMBOLS=20
OKX_CANDLE_FETCH_CONCURRENCY=3
MARKET_INTEL_PROVIDER=none
```

All five are optional; the app defaults to these values when the keys are
absent. Existing `.env` files are not overwritten.

## Changed files

Backend:
- `app/core/settings.py` — added `max_scan_symbols`, `market_intel_provider`,
  re-tuned `max_dynamic_symbols`, `scan_interval_seconds` defaults.
- `app/main.py` — added settings allow-list entries for the two new keys
  with clamping/enum checks; new `/api/scanner` route; scanner snapshot in
  `/api/health`.
- `app/services/scanner_activity.py` — new module (ScannerActivity singleton).
- `app/services/strategy.py` — wired observability calls + `_finalize_scan`
  helper + `_compute_blockers`.

Frontend:
- `web/index.html` — sticky V7 status strip, new Scanner Activity / Why-no-
  Trade / Market Data Source cards, expanded Live Tester card (Last Live
  Order panel, drift alert, BOT-MANAGED explainer banner), V7 settings
  fields in the drawer.
- `web/app.js` — `renderScannerActivity`, `renderBlockers`,
  `renderLastLiveOrder`, `renderLiveDriftWarning`, SSE event subscription,
  V7 settings save list.
- `web/styles.css` — V7 professional trading-console layer (statusbar,
  pro-table, blockers, lt-pos-grid, protection warnings).

Docs:
- `.env.example` — V7 defaults documented inline.
- `CHANGELOG_V7_MARKET_VISIBILITY.md` (this file).

## Install steps

1. Pull the V7 zip.
2. Diff your existing `.env` against the new `.env.example`; copy in the
   V7 keys you want (or leave them unset to use defaults).
3. Restart: `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000`
4. Hard-refresh the browser (Ctrl+Shift+R) so the new CSS / JS load.
5. Confirm the sticky status strip appears under the header and the
   three new V7 cards render below the Live Tester card.

## Smoke tests

```
cd ai-hummingbot-brain
python -m compileall -q app    # → exit 0
node --check web/app.js        # → no output, exit 0
```
