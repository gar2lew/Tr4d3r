# V6 Tiny Live Tester — Design Notes (IMPLEMENTED)

> **Status:** **IMPLEMENTED in V6.** This document is preserved as the design
> record. The original V5.4 status banner is reproduced below for context;
> see the “Implementation status — V6” section at the bottom for what was
> actually built, where the code lives, and what is deliberately deferred.
>
> _Original V5.4 banner:_ Design-only. V5.4 deliberately ships **without**
> live execution. The user is tempted to trade real money. The USDG/USDT
> impossible-TP bug found in V5.3 proved that previous paper results could
> not be trusted. V5.4 fixes realism so V6 can be built on a foundation
> that is honest about what the system can and cannot prove. **No code in
> V5.4 can place real orders.** V6 must add execution behind three
> independent manual confirmation tokens and a hard order-size cap.
>
> V6 honours that constraint: nothing in this package will place a real
> order until `LIVE_TESTER_ENABLED=true`, OKX keys are loaded and
> authenticated, `EXECUTION_MODE=okx_live` (or `okx_demo` for signed
> dry-runs), `LIVE_TRADING_ENABLED=true`, both `LIVE_TRADING_ACK` and
> `LIVE_DEMO_COMPLETED_ACK` tokens are set, and the V5.4 readiness gate
> passes (or `LIVE_TESTER_OVERRIDE=I_UNDERSTAND_THIS_IS_A_TINY_TEST` is
> set for a first smoke trade).

## What the user explicitly asked V6 to solve

These are the user's own words and concerns, captured here so V6 inherits them:

1. **"Wrong order value"** — orders sized differently than the bot intends
   (e.g. precision rounding, leverage misread, decimals confusion).
2. **"Inability to set stop loss"** — the live API rejecting the SL order or
   the SL order silently failing to attach, leaving an unhedged position.
3. **"Duplicate entries into same coin"** — the bot opening a second position
   on a symbol that already has one (pyramiding by accident).
4. **"Needing to constantly compare better opportunities"** — capital tied up
   in a mediocre symbol while a clearly better one is available.
5. **"Stop losses and trailing stops actually set"** — the system must
   *verify* via the exchange that protective orders exist after entry, not
   just locally remember that it asked for them.

Every one of these gets a V5.4 hook below.

---

## V5.4 hooks that V6 builds on

### 1. Order-value safety (hook for "wrong order value")

V5.4 already calculates exact USDT margin and clamps it twice:

- `PaperEngine.can_enter()` enforces `min_cash_reserve_pct` and
  `max_capital_in_positions_pct` against equity.
- `PaperEngine.open_long()` clamps `qty` so deployed margin never exceeds the
  cap, then re-checks `cash_usdt` post-fill.

**V6 contract:** before any real `POST /api/v5/trade/order`, V6 must:

1. Compute the *paper* margin from `can_enter()` first; refuse if paper would
   refuse.
2. Floor the order quantity to OKX `lotSz` and `minSz` (already available via
   `market_data.py`).
3. Hard-cap with `live_max_order_usdt` (default **$5**) regardless of what
   strategy requested.
4. Echo the resolved `(symbol, side, qty, notional_usdt, mark, leverage=1)`
   back to the UI and require the user to type the notional into a confirm
   box that matches to within $0.10 — if it doesn't match, abort.

### 2. Stop-loss attachment guarantee (hook for "inability to set stop loss")

V5.4 stores `sl_pct` / `tp_pct` / `trail_pct` on every Position and records
them in `Trade.exit_validated` / `exit_trigger_source` metadata.

**V6 contract:** OKX `algoOrder` for SL **must** be placed in the same
critical section as the entry. If the algo order fails:

1. Immediately submit a market-close for the just-opened position.
2. Log `sl_attach_failed=True` on the Trade.
3. Surface a red banner in the UI and refuse all further entries for the
   rest of the session.
4. SSE event: `sl_attach_failed` (mirrors the existing `exit_blocked` event).

The contract is "no naked positions, ever." There is no graceful degradation.

### 3. Duplicate-symbol guard (hook for "duplicate entries into same coin")

V5.4 adds this guard inside `can_enter()`:

```python
if symbol:
    for _p in self.state.open_positions:
        if getattr(_p, "symbol", "") == symbol:
            return False, f"{symbol} already has an open position (no pyramiding)"
```

**V6 contract:** before sending a live order, re-fetch OKX positions via
`/api/v5/account/positions` and confirm zero open size on `symbol`. If the
local view and the exchange view disagree, halt and require manual ack.

### 4. Opportunity comparison ("compare better opportunities")

V5.4 already computes a `SymbolProfile` per candidate symbol and stores
`hermes.summary()['symbol_scores']`. Today these are advisory.

**V6 contract:** introduce a "swap candidate" rule:

- If an open position has unrealized PnL between `-0.5%` and `+0.3%` for
  >= `swap_grace_minutes` (default 30), AND a candidate symbol has a
  SymbolProfile score `>= open_position.score + swap_min_delta` (default
  0.15), V6 may *propose* a swap.
- A swap is two operations: close current → wait `per_symbol_cooldown` →
  open new. V6 must show both legs in a single confirm dialog with the
  expected slippage on each. The user clicks once to approve both.
- V5.4 already has `per_symbol_cooldown_seconds`; V6 must enforce a
  global cooldown on swaps to prevent thrashing (suggest 1 swap per hour).

### 5. Trailing-stop verification ("stop losses and trailing stops actually set")

V5.4's `_verify_exit()` proves that exits *triggered legitimately* by
demanding live ticker bid/ask or candle high/low evidence. The same idea
applies in reverse for V6:

- Every 60s, V6 must poll OKX `/api/v5/trade/orders-algo-pending` and
  confirm that:
  - Each open position has an attached SL order at the expected price (±0.1%).
  - Each position with `trail_pct > 0` has the trailing order present.
- If any expected algo order is missing, V6 must:
  - Re-submit it once.
  - If re-submission fails, market-close the underlying position.
  - Emit `protective_order_missing` SSE.

The user's exact wording was "stop losses and trailing stops actually set" —
this is the implementation of that.

---

## V6 startup checklist (what must be true before live mode unlocks)

This is the machine-readable version of the "Live Tester Readiness checklist"
in the README. V5.4 already surfaces most of these fields via `hermes.summary()`
and `training_gate`.

```
checks = {
    "paper_profile == 'live_readiness'":     settings.paper_profile,
    "exit_verification_enabled":             settings.exit_verification_enabled,
    "realistic_fills_enabled":               settings.realistic_fills_enabled,
    "symbol_adaptive_enabled":               settings.symbol_adaptive_enabled,
    "closed_eligible >= min_eligible_trades": hermes.closed_eligible,
    "unverified_exits == 0":                 hermes.unverified_exits,
    "eligible_win_rate >= min_win_rate":     hermes.eligible_win_rate,
    "eligible_total_pnl_usdt > 0":           hermes.eligible_total_pnl_usdt,
    "leverage_enabled == False":             settings.leverage_enabled,
    "live_max_order_usdt <= 10":             settings.live_max_order_usdt,
    "paper_excluded_bases includes all stables": settings.paper_excluded_bases,
    "manual_ack_token_1, _2, _3":            three independent typed phrases,
}
```

If any row is False, V6 must refuse to call OKX `place_order`.

---

## V6 execution path: prefer OKX Agent TradeKit over raw REST

The user surfaced [OKX Agent TradeKit](https://app.okx.com/en-au/agent-tradekit)
("Think it. Your agent trades it."). On inspection it is a strong fit for V6
and probably the path we should take instead of writing our own OKX private
adapter from scratch. Key reasons:

- **MCP server + CLI**: V6 can call it from Python via subprocess (CLI) or
  via MCP if we route through Claude/ChatGPT. Either way, our code never
  touches API keys directly.
- **Three pre-built skills cover everything V6 needs**:
  - `okx-cex-market` — real-time tickers, orderbook, candles. **No API key.**
    V5.4 already uses our own market_data.py, but TradeKit could replace it.
  - `okx-cex-trade` — spot/futures/options orders, **including OCO, trailing
    stops, and grid bots**. This directly addresses concerns 2 and 5 above
    (stop-loss and trailing-stop attachment) because the protective order is
    placed by an officially supported, audited tool.
  - `okx-cex-portfolio` — balances, positions, PnL. Solves the
    "read-only balances" requirement without us writing OKX auth headers.
- **Safety model maps directly to our checklist**:
  - Dedicated **sub-account** with its own API key (matches our "spot-only,
    no withdrawal" requirement).
  - **Keys stored locally, never sent to the AI** — fits our threat model.
  - **Demo mode** with simulated funds — fits our "tiny live tester" idea.
  - **Read-only mode** — lets V6 start by *only* reading portfolio state.
  - **Per-trade approval** is a documented feature — matches our manual
    confirm-token requirement.
  - **Signed locally** — V6's order request never leaves the user's box
    unsigned.

**V6 plan (preferred):**

1. Phase 1 — read-only: call `okx-cex-portfolio` to fetch balances and open
   positions. Render in a new "Live Snapshot" panel. **No order placement.**
2. Phase 2 — demo trading: route paper-style decisions through
   `okx-cex-trade` against OKX **demo** funds, with `okx-cex-trade`'s built-in
   TP/SL/trailing/OCO. Compare fills against our paper engine to validate
   `_verify_exit()` realism.
3. Phase 3 — tiny live: same path, real sub-account, `live_max_order_usdt`
   hard-capped, three manual ack tokens, per-trade approval enabled inside
   TradeKit itself, and the V5.4 readiness checklist all green.

**Risks to investigate before committing:**

- License / ToS for embedding TradeKit in a third-party app.
- Whether the CLI can be called headlessly from FastAPI without a TTY.
- Whether grid bot / trailing stop fills surface in a way our Hermes journal
  can ingest (we need the same `exit_validated` evidence chain).
- Region availability — user is in Perth (AU); confirm AU sub-accounts can
  use TradeKit and that demo mode is available in AU.
- Whether keeping our own `protective_orders.py` reconciler is still needed
  if TradeKit owns the OCO. (Probably yes — verify-don't-trust.)

**Bottom line:** V6 should default to TradeKit for execution and reserve raw
OKX REST as a fallback. The hard safety rails in V5.4 (duplicate guard,
cash reserve, capital cap, exit verification, eligibility gate) stay in
place regardless of which transport V6 uses.

**V5.4 stays out:** no TradeKit code is shipped in V5.4. No `npx`, no MCP,
no CLI integration. This section is design notes only.

## V6 module sketch (file-by-file)

| File                                       | Purpose                                                          |
| ------------------------------------------ | ---------------------------------------------------------------- |
| `app/services/live_executor.py`            | Thin wrapper around OKX Agent TradeKit CLI (preferred) or OKX private REST (fallback). Single entry point: `place_order`. |
| `app/services/tradekit_bridge.py`          | Subprocess bridge to `okx/agent-skills` CLI; handles JSON in/out, error capture. |
| `app/services/protective_orders.py`        | Attach SL/TP/trail algo orders via TradeKit OCO; re-verify every 60s. |
| `app/services/live_reconciler.py`          | Pull positions/orders from OKX; reconcile with local state.      |
| `app/services/live_gate.py`                | Implements the readiness checklist above; returns block reasons. |
| `app/services/opportunity_swap.py`         | Swap-candidate proposer (rule from section 4 above).             |
| `web/live_panel.html` (new section)        | Two-step confirm dialog with notional-typing challenge.          |
| `app/main.py` `/api/live/*` routes         | All gated behind `live_gate.allow()`.                            |

No file in V5.4 imports any of these. They do not exist yet.

---

## What V5.4 will not do (deliberate non-goals)

- Place real orders. Period.
- Read live account balances. The dashboard's "Equity" is paper-only.
- Accept any "go live" toggle in the UI. There is none.
- Promote unverified paper results to readiness eligibility.

If the user wants real-money behavior before V6 ships, they should run the
V5.4 paper bot, accumulate `closed_eligible >= 30` trades with `unverified_exits == 0`
and `eligible_win_rate >= 55%`, and then revisit the V6 checklist above.

---

*Last updated: V5.4 release. Owners: brain team. Implementation target: V6.*

---

## Implementation status — V6

This section is the authoritative record of what was actually shipped. The
design above is preserved verbatim from the V5.4 plan so deviations are
visible.

### Components built

| Concern | Module | Status |
|---|---|---|
| OKX REST signing transport (HMAC-SHA256, headers, demo flag) | `app/services/okx_private.py` | Implemented (V5 path, extended) |
| OKX balance / permissions / fills / order lookup | `app/services/okx_private.py` (`get_account_snapshot`, `fetch_balance_raw`, `fetch_account_config`, `fetch_order`, `fetch_fills`, `summarize_fills`, `spot_base_balance`) | Implemented |
| V6 settings (`live_tester_*` fields, clamps, env loaders) | `app/core/settings.py` | Implemented |
| Live tester state machine, gate, executor, kill switch | `app/services/live_tester.py` (`LivePosition`, `LiveState`, `LiveTester`) | Implemented |
| State journal persistence | `data/live_state.json` (runtime; not packaged) | Implemented |
| Strategy entry hook (`_maybe_attempt_live_entry`) | `app/services/strategy.py` | Implemented |
| Strategy tick hook (`live_tester.tick`) | `app/services/strategy.py` | Implemented |
| API routes (`/api/okx/account`, `/api/live/account`, `/api/live/tester`, `/api/live/tester/kill`, `/api/live/tester/release`, `/api/live/tester/reconcile`) | `app/main.py` | Implemented |
| Readiness gate `live_tester` block | `app/main.py` (`_build_readiness`) | Implemented |
| UI: OKX real account panel + Live Tester panel | `web/index.html`, `web/app.js` | Implemented |
| UI: SSE handlers for `live_tester*`, `live_kill_switch`, `live_order_*` | `web/app.js` | Implemented |
| Settings drawer V6 fields | `web/index.html`, `web/app.js` | Implemented |
| Docs (`README.md` V6 section, `.env.example` V6 block) | repo root | Implemented |

### Safety controls implemented

- **Disabled by default.** `live_tester_enabled` defaults to `false`. The
  master switch must be flipped in both env (`LIVE_TESTER_ENABLED=true`)
  and settings.
- **Per-order cap.** `live_max_order_usdt_tester` is hard-clamped to
  `[1, 25]` in `settings.py` AND re-clamped immediately before each order
  submission in `live_tester.attempt_entry()`.
- **Free reserve check.** Entry refuses unless free USDT ≥
  `order_usdt × live_free_reserve_multiplier` (default 1.10) so fees and
  slippage cannot overdraw the account.
- **One position per symbol / max open positions / daily trade cap /
  daily realised-loss cap.** All enforced in `gate_check`.
- **Spot-only.** `live_spot_only` is enforced; `preflight_symbol`
  additionally verifies the OKX instrument is `SPOT` regardless of flag.
- **Protective exit required.** `live_require_protective_exit=true`
  blocks entry if the strategy did not compute SL/TP/trailing-stop.
- **Quote-currency market buys.** Entries use `tgtCcy=quote_ccy` and
  `sz=<usdt>` so the cap is denominated in USDT, not base units.
- **Fills reconciliation.** Post-entry, `/api/v5/trade/order` is polled
  for `accFillSz`/`avgPx`. If unknown the position is flagged
  `needs_reconcile` and blocks further entries until resolved via
  `/api/live/tester/reconcile` or the `fills` fallback.
- **Kill switch.** Engaged automatically on sell failure, on unknown qty
  after entry, or on daily-loss cap breach. Engaged manually from the UI.
  Blocks all future entries until released.
- **Bot-managed stops.** Every `LivePosition.sl_mode = "bot_managed"`.
  The UI banner and README both warn that stops will not fire if the
  app stops. Native OKX algo orders are NOT used in V6.
- **Credential redaction.** API keys never appear in API responses; only
  `AB***CD (len=N)` fingerprints, via `_redact()` in `okx_private.py`.
- **Client order IDs.** Entries `v6t<ts>` and exits `v6x<ts>` for audit.

### Deliberately deferred to a future release

These items appear in the design above but are **not** in V6. They will
not be added without a separate review pass:

- **Native OKX algo orders** (`/api/v5/trade/order-algo` with
  `tdMode=cash`, `ordType=conditional`/`trigger`). Bot-managed only.
- **Agent TradeKit / OKX DEX Aggregator path.** Out of scope; V6 uses
  the signed REST transport already shipped in V5.
- **Multi-exchange (Bybit, Binance).** OKX-only.
- **Auto-reconciliation poller.** V6 reconciles on entry, on tick, and on
  manual `/api/live/tester/reconcile`. No background poller is started.
- **WebSocket order/account stream.** REST polling only in V6.
- **Native trailing-stop algo orders.** Trailing logic runs in-process.

### What the user explicitly asked V6 to solve — coverage

1. **Wrong order value.** Quote-currency market buy with
   `sz=clamped_usdt`, `tgtCcy=quote_ccy`. Cap re-clamped at submission.
2. **Inability to set stop loss.** `live_require_protective_exit` blocks
   entry without SL/TP/trailing. Stops are bot-managed and visualised in
   the UI with a `bot_managed` badge and a red warning banner so the
   user is never surprised about how exits fire.
3. **Duplicate entries into same coin.** `live_one_position_per_symbol`
   plus `live_max_open_positions` plus `LiveState` open-position map all
   refuse a second entry. `client_oid` prefixes (`v6t…`) further
   deduplicate at OKX side.
4. **Comparing better opportunities.** The strategy still uses the
   V5.3 Auto Strategy + V5.4 opportunity ranking; V6 only fires a live
   entry **after** the paper engine successfully opened the same idea.
5. **Trailing stop / SL actually being set.** The Live Tester card lists
   every open position with `SL · TP · trail` columns and an explicit
   `stop mode: bot_managed` flag.
