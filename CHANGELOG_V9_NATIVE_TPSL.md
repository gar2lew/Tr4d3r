# V9 — OKX Native Exchange TP/SL for Tiny Spot Live Tester

**Delta vs V8.** This release wires real OKX V5 algo-order placement so
that after a tiny spot **LONG** buy fills, the tester additionally
attaches exchange-side protective sells (OCO or two conditional algos)
on OKX. The trigger prices now show up in the OKX algo-orders panel
and will fire **even if this bot process stops** — closing the gap the
operator reported on V8 (OKX order history showed `TP | SL` blank
because exits were purely bot-managed).

Bot-managed protection is **not removed** by this release. It remains
armed in parallel as a defense-in-depth fallback. If native placement
fails (any non-zero `sCode`), the UI prominently displays
`NATIVE FAILED – BOT FALLBACK` and the watcher keeps managing the exit.

## Safety constraints honoured

- **Native protection is OFF by default.** Operator must explicitly set
  `LIVE_NATIVE_PROTECTION_ENABLED=true` in `.env`.
- No real orders are placed during tests. Unit tests monkey-patch
  `OKXPrivateAdapter._request` so no HTTP is actually sent.
- **No new credentials required.** Uses the existing OKX API key the
  operator already supplied for the V6 live tester.
- **Spot LONG only.** No shorts, perps, margin, borrow, leverage,
  withdrawals, or transfers are touched. The new endpoints
  (`/api/v5/trade/order-algo`, `/api/v5/trade/cancel-algos`,
  `/api/v5/trade/orders-algo-pending`, `/api/v5/trade/orders-algo-history`)
  are all called with `tdMode: "cash"` and `side: "sell"`.
- **Never claims success without OKX confirmation.** Native is only
  marked `active` after the per-row `sCode == "0"` and an `algoId` is
  returned. Otherwise the journal records `failed` with the OKX error
  code/message verbatim.

## What changed

### `app/core/settings.py`

- New runtime fields:
  - `live_native_protection_enabled: bool = False`
  - `live_native_protection_mode: str = "oco"` (allowed: `oco`,
    `conditional`, `off`)
  - `live_native_protection_dry_run: bool = False`
- Env loaders: `LIVE_NATIVE_PROTECTION_ENABLED`,
  `LIVE_NATIVE_PROTECTION_MODE`, `LIVE_NATIVE_PROTECTION_DRY_RUN`.
- Settings reload copies all three flags so an in-process update
  doesn't drop them.

### `app/services/okx_private.py`

Added four new helpers (all signed via the existing `_request`, so the
V6.2 base-URL selection and the V6.1 OKX-error classification apply):

- `place_algo_oco_spot_sell(symbol, base_qty, tp_trigger_px, sl_trigger_px, client_algo_id, tag)`
  → `POST /api/v5/trade/order-algo` with `ordType: "oco"`,
  `tdMode: "cash"`, `tpOrdPx: "-1"`, `slOrdPx: "-1"`,
  `tpTriggerPxType: "last"`, `slTriggerPxType: "last"`,
  `sz` = base-currency qty (e.g. DOGE amount, not USDT).
- `place_algo_conditional_spot_sell(symbol, base_qty, trigger_px, kind, client_algo_id, tag)`
  → same endpoint with `ordType: "conditional"`, single `triggerPx`,
  `orderPx: "-1"`.
- `query_algo(algo_id, cl_ord_id, ord_type)` → tries
  `/api/v5/trade/orders-algo-pending` first, then
  `/api/v5/trade/orders-algo-history`. Returns a normalised `{state,
  algo_id, cl_ord_id, raw}` dict.
- `cancel_algo(symbol, algo_id)` → `POST /api/v5/trade/cancel-algos`
  with `[{instId, algoId}]`. Treats OKX `514xx`/`515xx` ("already
  cancelled") as idempotent success.

Every helper returns a structured dict (`ok`, `algo_id`, `cl_ord_id`,
`s_code`, `s_msg`, `raw`) so callers never have to parse the OKX
`data[0]` envelope themselves. None of them raise — failure is in the
return dict.

### `app/services/live_tester.py`

- `LivePosition` gains a `native_protection: Dict[str, Any]` block
  with: `enabled`, `mode`, `status`, `oco_algo_id`, `tp_algo_id`,
  `sl_algo_id`, `cl_ord_id`, `last_error_code`, `last_error_msg`,
  `placed_ts`, `cancelled_ts`.
- New journal schema marker: `v9.tester.live_state.1`. Old journals
  load unchanged (extra fields default to empty dict).
- New method `_place_native_protection(position_id, settings)` is
  called after a successful buy fill (`filled_qty > 0`, `avg_px > 0`,
  not `needs_reconcile`, env flag on, mode != off). Never raises.
- New method `_cancel_native_for_position(position_id, reason)` is
  invoked **before** every bot-managed market sell, on the kill
  switch, and on demand from the new public method
  `cancel_native_protection`. Cancels all attached algos so OKX
  cannot fire concurrently with the bot's sell.
- New method `refresh_native_protection(position_id)` re-queries OKX
  and updates the journal (`active` / `cancelled` / `triggered`).
- `clOrdId` for native algos is deterministic
  (`v9p<position_id_stripped>`, `v9tp...`, `v9sl...`), capped at 32
  alphanumeric chars so a restart that re-enters the post-buy path
  is idempotent.
- `summary()` exposes `native_protection_enabled`,
  `native_protection_mode`, `native_protection_dry_run`, and
  `native_protection_warning` to the UI.

### `app/main.py`

Two new POST endpoints (operator/UI controls):

- `POST /api/live/tester/native_cancel` — body
  `{"position_id":"lv_…","reason":"manual"}`. Cancels native algos
  for one position. Does **not** close the position itself.
- `POST /api/live/tester/native_refresh` — body
  `{"position_id":"lv_…"}`. Re-queries OKX for the algo state.

### `web/index.html`, `web/app.js`

- The V8 placeholder "Native TP/SL: NOT ENABLED" banner is replaced
  with a **dynamic** banner driven by the live-tester summary. When
  enabled it goes green and tells the operator native + bot-managed
  are both armed. When disabled it stays warning-coloured and
  explains how to enable.
- Per-position protection chip now has a five-state matrix:
  `EXCHANGE-NATIVE ACTIVE` / `EXCHANGE-NATIVE PENDING` /
  `EXCHANGE-NATIVE DRY-RUN` / `NATIVE FAILED – BOT FALLBACK` /
  `BOT-MANAGED ACTIVE` / `UNPROTECTED`.
- The OKX algo IDs (OCO or TP/SL) render under each position as
  monospace pills so the operator can cross-check the OKX algo
  panel.
- A failure card surfaces the verbatim OKX `sCode`/`sMsg` when
  native placement fails.

### `.env.example`

- The V8 placeholder block is replaced with a full V9 block
  documenting the three new flags, the mode choices, the safety
  guarantees, and the cancellation behaviour.

### `_smoke_v9.py`

A standalone mocked test that monkey-patches the OKX request method
and asserts:

- The OCO payload has `instId`, `tdMode: "cash"`, `side: "sell"`,
  `ordType: "oco"`, `tpOrdPx: "-1"`, `slOrdPx: "-1"`.
- The conditional payload uses `ordType: "conditional"`, `orderPx:
  "-1"`, and a single `triggerPx`.
- `cancel_algo` sends a JSON list body to `/api/v5/trade/cancel-algos`.
- `LIVE_NATIVE_PROTECTION_DRY_RUN=true` short-circuits and never
  reaches `_request`.
- The live tester records `status: active` only when OKX returns
  `sCode: "0"` and `algoId` is non-empty.

No real HTTP is sent.

## How native TP/SL works (operator-facing)

1. Operator sets `LIVE_NATIVE_PROTECTION_ENABLED=true` and chooses
   `LIVE_NATIVE_PROTECTION_MODE=oco` (default) or `conditional`.
2. The tester's existing buy path places the spot market buy.
3. After the fill reconciles (`filled_qty > 0`, `avg_px > 0`), the
   tester computes TP/SL trigger prices from the position's existing
   stored stop-loss and take-profit values.
4. The tester signs and submits `/api/v5/trade/order-algo`. The
   request body for OCO is:
   ```json
   {
     "instId": "DOGE-USDT",
     "tdMode": "cash",
     "side": "sell",
     "ordType": "oco",
     "sz": "<base qty>",
     "tpTriggerPx": "<px>", "tpOrdPx": "-1", "tpTriggerPxType": "last",
     "slTriggerPx": "<px>", "slOrdPx": "-1", "slTriggerPxType": "last",
     "clOrdId": "v9p<position_id>"
   }
   ```
5. On `sCode == "0"`: the returned `algoId` is recorded on the
   position and the UI flips to `EXCHANGE-NATIVE ACTIVE`. The OKX
   "Open Algo Orders" page now shows the trigger prices.
6. On any non-zero `sCode`: the journal records `status: "failed"`
   with the OKX error code and message verbatim. The UI shows
   `NATIVE FAILED – BOT FALLBACK` and the bot-managed watcher keeps
   running. No retry storm — operator can manually retry via the
   `native_refresh` endpoint (or by closing/re-entering).
7. When the bot-managed watcher decides to close (SL/TP/trail hit) or
   the operator engages the kill switch, the tester first calls
   `/api/v5/trade/cancel-algos` so OKX cannot fire the attached algo
   concurrently with the market sell.

## Limitations

- **Trailing stop is bot-managed only.** OKX's algo trailing-stop is
  exposed via `ordType: "move_order_stop"` but it is not used in V9
  because the existing bot watcher already enforces trail with full
  fidelity. Operators get OCO TP+SL on OKX plus the bot-managed
  trailing stop.
- **OCO per-instrument cap is 100.** Not an issue for the tiny tester
  (1–2 open positions).
- **If OKX rejects OCO for the configured instrument** (rare; some
  thinly traded coins), set `LIVE_NATIVE_PROTECTION_MODE=conditional`
  to use two separate conditional algos instead.
- **No partial-fill rebalancing.** If `filled_qty` ends up smaller
  than expected after a re-fill, the native algo `sz` is unchanged.
  Reconciliation just refreshes journal state; it does not amend the
  algo. Operators can cancel + re-place by calling `native_cancel`.

## Sources

- OKX V5 algo trading: <https://www.okx.com/docs-v5/en/#order-book-trading-algo-trading>
- OCO vs conditional explainer: <https://www.okx.com/en-us/help/xi-strategy-order-types>
- Reference OCO payload (ccxt issue): <https://github.com/ccxt/ccxt/issues/18142>
- python-okx endpoint constants: <https://github.com/okxapi/python-okx/blob/master/okx/consts.py>

---

## V9 — Unattended live-test mode (5-day envelope) + screenshot follow-ups

Implemented in the same V9 release. Configured via `.env` (defaults OFF).

### New settings

| Env var | Default | Purpose |
|---|---|---|
| `LIVE_NATIVE_PROTECTION_RECONCILE_ON_STARTUP` | `false` | On boot, walk open positions and re-attach native exits if missing. Never opens new positions. |
| `LIVE_UNATTENDED_MODE` | `false` | Master switch for the 5-day envelope. |
| `LIVE_UNATTENDED_MAX_HOURS` | `120` | Auto-kill-switch timer (clamped 0.25–168h). |
| `LIVE_UNATTENDED_STARTED_AT` | `0` | Bot-managed timestamp; persisted to `data/live_state.json`. |
| `LIVE_UNATTENDED_STOP_NEW_ON_FAILURE` | `true` | Also refuse new entries on stale data / API error burst. |

### New backend behaviour

- `LiveState` now tracks `last_heartbeat_ts`, `last_market_data_ts`,
  `consecutive_api_errors`, `last_api_error`, `unattended_started_at`,
  `unattended_expired`, `unattended_expired_at`, and an `events` ring
  buffer (50 entries, persisted).
- `summary()` adds `stop_mode` (now dynamic — `"exchange_native + bot_fallback"`,
  `"bot_managed (native dry-run)"`, or `"bot_managed"`), `watchdog`,
  `unattended`, `unattended_readiness`, and `events` keys.
- `_unattended_readiness()` computes 11 PASS/FAIL checks: `native_enabled`,
  `native_verified_for_open` (with per-position sub-list), `caps_tiny`,
  `max_open_one`, `max_trades_small`, `kill_switch_off`, `watchdog_alive`,
  `market_data_fresh`, `no_api_error_burst`, `unattended_not_expired`,
  `daily_loss_under_cap`.
- `gate_check()` extension: when `LIVE_UNATTENDED_MODE=true`, blocks new
  entries on readiness FAIL, expired timer, or ≥5 consecutive API errors.
- New methods: `heartbeat()` (called from the strategy price-watch loop),
  `record_api_error()`, `clear_api_errors()`, `_maybe_expire_unattended()`
  (latches kill switch on timer expiry), `_append_event()`,
  `startup_reconcile()` (gated by the env flag, never opens new positions).
- Event log emits for: `live_order_opened`, `live_exit_fired`,
  `native_protection_placed`, `native_protection_failed`,
  `native_protection_cancelled`, `native_protection_triggered`,
  `kill_switch_engaged`, `unattended_armed`, `unattended_expired`,
  `startup_reconcile`.

### New API endpoints

- `POST /api/live/tester/refresh_inventory` — Force a fresh OKX inventory
  probe for all open positions. Useful when the UI shows a stale
  `okx_inventory.frozen` value (e.g. an OCO algo was cancelled outside
  the bot).
- `POST /api/live/tester/startup_reconcile` — Manually trigger the same
  reconcile path the startup hook runs.

### Per-position UI additions

- `okx_inventory` strip: `OKX <CCY>: total / free / frozen / open sells / open algos`.
- `environment_warnings` chips (e.g. `existing-algo-warn-other`,
  `adopted-existing-algo`, `frozen-balance-detected`).
- `entry_fee_ccy` rendered next to fee value (e.g. `0.000123 USDT` instead
  of `$0.000123`).

### New UI panels

- **Unattended readiness panel** (hidden when `LIVE_UNATTENDED_MODE=false`).
  Renders all 11 checks with per-position sub-rows for `native_verified_for_open`.
  Banner reads **"SAFE-ish for unattended tiny test"** (green) when every check
  passes, otherwise **"DO NOT LEAVE UNATTENDED"** (red). Timer shows
  `expires <UTC> · ~N.Nh left` or `EXPIRED at <UTC>`.
- **Recent events feed** — newest 20 rows from `summary().events`,
  colour-coded (red for failed/kill_switch_engaged, green for placed
  /unattended_armed, amber for triggered/exit_fired).

### Text-cleanup follow-ups from the screenshots

- Panel title: `Live Tester (V6 — tiny real-money tester)` → `Tiny OKX Live Tester (V9)`.
- Settings drawer sub-heading: `V6 — Tiny OKX live tester` → `Tiny OKX live tester (V9)`.
- Native TP/SL "planned for V8" copy replaced with the actual V9 state.
- `stop_mode` KPI is now driven by the backend summary; UI no longer
  hard-codes `bot_managed`.
- "Last live tester attempt" in scanner activity now falls back to
  `live_tester.attempts[-1]` when the scanner has no record.

### Tests

`_smoke_v9.py` now runs 15 offline checks (was 9 in V9 pre-unattended):

1–9. Original V9 native TP/SL checks (payload shapes, dry-run, etc.).
10. `okx_private.inst_inventory_snapshot` payload shape.
11. `_classify_existing_algos` all four branches (adopt / skip_duplicate / warn_other / ok).
12. `fee_ccy` captured from `summarize_fills` onto position.
13. Unattended readiness fails by default (no heartbeat / data yet).
14. Unattended readiness passes when every check is green.
15. Unattended expiry latches kill switch + persists state.
+ startup_reconcile gated; attaches native when enabled + position unprotected.

All 15 tests pass with no real HTTP and no real orders placed.

### Hard safety constraints (unchanged from V9)

- No real orders during implementation or tests.
- No user API keys required or included.
- Native protection OFF by default; bot-managed remains the fallback.
- SPOT LONG only. No shorts, perps, margin, borrow, leverage, withdrawals, transfers.
- "Risk-reduced tiny live test envelope" — **never** "safe automation".
