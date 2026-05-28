# V9.1 — OKX Live Sell Qty Hotfix

Urgent hotfix on top of V9. Backend + UI changes only. No new env vars.
Native TP/SL behaviour (V9) is unchanged in scope — it now reuses the same
clamp logic so the algo `sz` it sends to OKX is also safe.

## What broke in V9

Real account screenshot showed:

| Field | Bot journal | OKX real account |
|---|---|---|
| DOGE qty held | `98.106543` | `97.41979720` |
| Buy fee | shown as `$0.6867` | **`0.6867 DOGE` deducted from received base** |
| Latest sell attempt | tried `sz = 98.106543` | rejected `code=1: All operations failed` (nested `sCode=51008 Insufficient available balance`) |
| Kill switch | engaged on every retry | — |

Two root causes:

1. The buy fill record kept the gross filled qty as the position size and rendered the base-asset fee as if it were a quote-asset fee. OKX charged the fee in DOGE (the base), so the bot's "qty held" was always larger than what was actually withdrawable.
2. The exit code path did not look at OKX free balance or `lotSz` / `minSz` before constructing the sell payload, and OKX's `code=1` "All operations failed" envelope hid the real per-order `sCode`/`sMsg`, so the operator could not see *why* the sell was being rejected.

## Fixes shipped in V9.1

### 1. Net filled quantity (entry path)

`app/services/live_tester.py` — `LivePosition` gains four fields:

- `gross_filled_qty: float` — base filled by OKX, before any base-asset fee. PnL cost basis uses this together with `avg_entry_price`. Never overwritten.
- `sellable_qty: float` — `gross_filled_qty - fee_deducted_from_base` when the fee currency is the base asset; otherwise equal to `gross_filled_qty`.
- `fee_deducted_from_base: float` — the actual base-asset units OKX kept as commission. `0.0` when fee was charged in quote / USDT.
- `inventory_sellable_qty: Optional[float]` — set by the inventory-repair path when journal qty exceeds OKX `total` (free + frozen). When present, exits use it instead of `sellable_qty`. PnL math is **not** touched.

`attempt_entry()` now records the base-asset fee correctly. When OKX returns
`feeCcy == baseCcy`, the fee is shown as `Fee 0.6867 DOGE` (not `$0.6867`),
and the position card surfaces both `Gross qty` and `Sellable qty`.

`LiveState.schema` bumped to `"v9.1.tester.live_state.1"`.

### 2. Safe live sell (exit path)

`app/services/okx_private.py` adds three primitives:

- `okx_private.fetch_instrument(symbol, *, force=False)` — unsigned hit to `/api/v5/public/instruments`, caches `{instId, baseCcy, quoteCcy, lotSz, minSz, tickSz, ts, ok}` per symbol. Never raises.
- `round_down_to_lot(qty, lot)` — integer-math floor of `qty` to the OKX lot step. `lot == 0` falls back to an 8-decimal truncation. No float drift.
- `clamp_sell_qty(journal_qty, okx_free, *, lot, min_sz)` — returns `{sell_qty, capped_by, below_min, reason, raw, rounded}`. The chosen `sell_qty` is `min(journal_qty, okx_free)` rounded down to `lot`; if it falls below `min_sz`, `below_min` is true and `sell_qty` is `0.0`.

`live_tester._execute_exit()` now:

1. Cancels native protection first (unchanged from V9).
2. Calls `inst_inventory_snapshot()` + `fetch_instrument()`.
3. Picks `journal_qty = inventory_sellable_qty or sellable_qty or gross_filled_qty or filled_qty` (first truthy).
4. Runs `clamp_sell_qty()`.
5. If `below_min`: **does not** send a sell. Appends a `live_exit_blocked_low_inventory` event, marks the position `needs_reconcile`, latches the kill switch with reason `live_exit_below_minSz`, and returns.
6. Otherwise sends `sz = clamped_qty`.

Exit PnL math now correctly handles base-asset fees:

- If the **entry** fee was in base ccy, `entry_fee_quote = 0.0` — the fee is already realised through a reduced `sellable_qty`, so subtracting it again would double-count.
- If the **exit** fee comes back in base ccy, `exit_fee_quote = sell_fee * avg_sell_px`.

### 3. Existing-position repair (refresh / startup reconcile)

`live_tester._refresh_inventory()`: after writing the OKX snapshot onto the
position, if `okx_total > 0 and okx_total < journal_sellable * 0.999`, the
position's `inventory_sellable_qty` is set to `okx_total`, an
`inventory_repair` event is emitted, and `inventory_repair_note` records the
journal vs OKX delta. **`filled_qty` and `avg_entry_price` are preserved**
so PnL cost basis is intact. When OKX inventory catches up to journal again,
the override is cleared.

This means an operator who clicks **Refresh tester state** after upgrading
will automatically have stale DOGE-like positions clamped to the real
withdrawable balance, without a manual journal edit.

### 4. OKX `code=1` diagnostics

`okx_private._request()`: when OKX returns top-level `code == "1"` (`"All operations failed"`), the helper now scans `data[]` for the first non-zero `sCode` / `sMsg` and surfaces them in the raised `OKXAuthError`:

```
OKX 200 code=1: All operations failed                # before
OKX 200 code=1 (sCode=51008): Order failed. Insufficient available balance.  # after
```

The exception now also carries `.nested_s_code` and `.nested_s_msg`
attributes so callers (and tests) can branch programmatically.

### 5. Native protection placement reuses the clamp

`_place_native_protection()` reads `inventory_sellable_qty or sellable_qty or
gross_filled_qty or filled_qty`, pre-clamps via `inst_inventory_snapshot +
fetch_instrument + clamp_sell_qty` before building the OCO `sz`, and returns
`status="failed", code="insufficient_inventory"` if the clamped qty would be
below `minSz`. Dry-run mode skips the inventory probe (unchanged).

### 6. Kill switch behaviour

**Unchanged in scope**: kill switch is never auto-released. V9.1 only adds
latch reasons (`live_exit_below_minSz`, `native_protection_low_inventory`)
so the operator can see *why* the bot stopped before clicking **Release
kill switch**.

### 7. UI changes (`web/index.html`, `web/app.js`)

- Spot-only badge: `V8 live mode` → `V9.1 live mode`.
- Live tester panel title: `Tiny OKX Live Tester (V9)` → `Tiny OKX Live Tester (V9.1)`.
- Position card grid now shows **Gross qty** and **Sellable qty** explicitly (8-decimal precision so a `0.6867 DOGE` deduction is visible). When OKX inventory is lower, the Sellable cell shows `(OKX: 97.41979720)` inline.
- New "Fee deducted" cell renders the actual fee currency. `0.6867 DOGE` is no longer rendered as `$0.6867`.
- New yellow warning banner above the grid when journal sellable > OKX inventory:
  > ⚠️ Journal quantity exceeds OKX inventory; using sellable balance for exits.
- Bot-managed exits notice now includes a `V9.1 hotfix` paragraph that documents the sellable + clamp + below-minSz + kill-switch behaviour.

## Tests

`_smoke_v9.py` extended from 16 to **23 tests**. New tests (no real HTTP, no real orders):

- `round_down_to_lot floors qty to OKX lot step`
- `clamp_sell_qty floors to min(journal, free) and blocks below minSz` (covers the literal `98.106543 → 97.0` case)
- `entry path deducts base-asset fee from sellable_qty`
- `exit path clamps sell qty to OKX free + lotSz (sent sz=97)`
- `exit path blocks sell + latches kill switch when below minSz`
- `nested sCode surfaces in OKXAuthError message` (asserts `sCode=51008` + nested attrs)
- `inventory_repair clamps stale journal qty to OKX total + emits event`

All 23 pass:

```
$ python _smoke_v9.py
…
[ok] inventory_repair clamps stale journal qty to OKX total + emits event

All V9 + V9.1 smoke tests passed (no real HTTP, no real orders).
```

## Schema / env / safety

- `LiveState.schema` is now `"v9.1.tester.live_state.1"`. Older `v9.tester.live_state.*` state files are read-compatible (new fields default to `None`/`0.0`); the next save migrates them in place.
- No new env vars. No change to `LIVE_NATIVE_PROTECTION_ENABLED`, `LIVE_TESTER_*`, or any cap.
- Spot-long only. Still no shorts, no perps, no leverage.
- Kill switch is **never** auto-released by V9.1.

## Files touched

- `app/services/okx_private.py` — nested `sCode` surfacing, `fetch_instrument`, `round_down_to_lot`, `clamp_sell_qty`.
- `app/services/live_tester.py` — `LivePosition` new fields, entry path captures base-fee, exit path clamps + may refuse, native placement reuses clamp, `_refresh_inventory` repairs stale journal, schema bump.
- `_smoke_v9.py` — 7 new tests.
- `web/index.html` — V9 → V9.1 copy, V9.1 hotfix paragraph.
- `web/app.js` — gross / sellable / fee-deducted cells, inventory-mismatch warning chip.
- `CHANGELOG_V9_1_LIVE_SELL_QTY_HOTFIX.md` (this file).
- `UPGRADE_V9_1_LIVE_SELL_QTY_HOTFIX.md`.
