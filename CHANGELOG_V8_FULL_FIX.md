# V8 Full Fix — AI Hummingbot Brain

Build tag: **V8 Full Fix (consolidation release)**
Scope: V7 market-visibility scanner + urgent V7.x safety/labeling fixes from the live DOGE/USDT incident, rolled into one shippable build.

Hard build truths preserved verbatim from the V8 brief:

- **Current build is OKX SPOT ONLY.** Real shorts/perps/margin/borrow are NOT wired.
- **No real orders are placed in tests.** No API keys are required to run smoke tests.
- **No user secrets are shipped** in this zip.
- **No profit is ever promised.**
- `OKX_BASE_URL` default in code is still `https://www.okx.com` *only if env is unset*; `.env.example` ships with `OKX_BASE_URL=https://us.okx.com` and an inline note for US accounts.
- `LIVE_NATIVE_PROTECTION_ENABLED` is a **placeholder flag**, default `false`. The order-placement path **does not read it**. Native exchange-attached TP/SL is **not implemented** in V8. The UI says so.

---

## What V8 changes vs V7

### 1. Direction labeling everywhere (LONG / SHORT-unavailable / NO TRADE)
- `app/services/strategy.py::_build_opportunities` now emits two new fields per opportunity row:
  - `direction_label`: one of `"LONG (spot buy)"`, `"SHORT signal — not executable (spot-only V8)"`, `"NO TRADE"`.
  - `direction_code`: one of `LONG`, `SHORT_UNAVAILABLE`, `NO_TRADE`.
- `app/services/ai_brain.py` now stamps the committee verdict with the same `direction_label` / `direction_code` (always `LONG` on proceed, `NO_TRADE` otherwise).
- `app/services/strategy.py` deterministic-fallback verdict also carries the same fields.
- `web/app.js::renderAgents` shows `Chosen: LONG <SYM> (spot buy)` or `Chosen: NO TRADE on <SYM>` or `Chosen: SHORT signal observed — not executable in spot-only V8`.
- `web/app.js::renderOpportunities` reads `direction_code` first; falls back to the V6.4 bias heuristic only for older payloads.
- `web/app.js::renderMarket` adds a new **Direction** column (LONG / SHORT n/a / NO TRADE).
- `web/index.html` market table header gained a `<th>Direction</th>` cell; the empty colspan moved from 7 to 8.
- `web/app.js::renderTrades` normalizes legacy `"buy"` side to `"LONG (spot buy)"` so the trade log never shows an ambiguous bare “buy”.

### 2. Spot-only truth in the topbar
- Permanent `#spotOnlyPill` pill in the topbar status cluster: **"V8 live mode: OKX SPOT LONG only"**. Tooltip names the disabled surfaces (shorts, perps, margin, borrow). The pill is not driven by settings — this is a build-time truth.

### 3. Native / protection visibility per live position
- Each open live-tester position now renders a V8 protection-status chip with one of four explicit states:
  - `BOT-MANAGED ACTIVE` (warn): position has a recorded SL, TP, or trailing stop and the bot watcher is alive.
  - `EXCHANGE-NATIVE ACTIVE` (ok): reserved label for the future native-protection adapter (not enabled in V8).
  - `BOT-MANAGED MISSING` (warn): no SL/TP/trail recorded but position is not currently open.
  - **`UNPROTECTED`** (error): open position with **no** SL, **no** TP, **no** trail. Renders a red 🚨 banner: *"Close manually on OKX or engage the kill switch immediately. The bot has no exit plan for this position."*
- A permanent **"Native exchange-attached TP/SL: NOT ENABLED"** banner is added to the live tester card. Even if `LIVE_NATIVE_PROTECTION_ENABLED=true` is set in env, the panel will still say not enabled — we refuse to fake it.

### 4. Live tester clarity
- Override precedence fix: when `lifecycle === "armed"` and `t.override_active` is true, the live-tester heading reads **"ARMED (override): waiting for qualified LONG spot signal"** instead of the legacy "Why the live tester is locked" copy.
- New `#ltCapsWarning` banner appears when:
  - `LIVE_MAX_ORDER_USDT > 5` (recommended tiny-test cap is 5 USDT), or
  - `LIVE_DAILY_LOSS_CAP_USDT > 3` (recommended tiny-test cap is 3 USDT).
- The "Why no live trade?" content already shipped in V7 (`#noTradeCard` powered by `_compute_blockers`) is retained.

### 5. Scanner visibility (V7 + ETA)
- The V7 sticky status strip already shows scanner / clean / skipped / last-scan / queue. V8 adds:
  - **Next scan ETA** — countdown to the next scheduled scan (`scanner_activity.snapshot.next_scan_eta_seconds`).
  - **Universe size** — `<scanned>/<max>` to show what fraction of the cap is in use.
- `app/services/scanner_activity.py` now snapshots `scan_interval_seconds`, `next_scan_eta_seconds`, and `total_universe_size`.

### 6. Native-protection placeholder env wired
- `app/core/settings.py` gained a `live_native_protection_enabled: bool = False` field plus env loader and `reset_settings` copy. **No order-placement code reads the flag** — it is purely a forward-compat marker.
- `.env.example` documents the flag with a stern "DO NOT ENABLE" comment block.

### 7. Validation
- `python -m compileall -q app` → exit 0.
- `node --check web/app.js` → exit 0.

---

## Files added in V8

- `CHANGELOG_V8_FULL_FIX.md` (this file)
- `UPGRADE_V8_NOTES.md` (Windows PowerShell upgrade walkthrough)

## Files modified in V8

- `app/core/settings.py` — `live_native_protection_enabled` placeholder.
- `app/services/strategy.py` — `direction_label` / `direction_code` on opportunities + deterministic-fallback verdict.
- `app/services/ai_brain.py` — `direction_label` / `direction_code` on committee verdicts.
- `app/services/scanner_activity.py` — `scan_interval_seconds`, `next_scan_eta_seconds`, `total_universe_size` in snapshot.
- `web/index.html` — topbar spot-only pill; market table Direction column; live tester caps warning slot + native-protection placeholder banner; scan ETA + universe size in V7 status strip.
- `web/app.js` — verdict / market / opportunities / trades direction copy; live tester override precedence + V8 protection-status chips + UNPROTECTED critical banner + caps-too-large warning + scan ETA / universe rendering.
- `.env.example` — `LIVE_NATIVE_PROTECTION_ENABLED=false` documented; `OKX_BASE_URL=https://us.okx.com` retained for US accounts.

## What V8 does NOT do (deferred)

- Native exchange-attached TP/SL on OKX orders. Reserved for a follow-up build; the placeholder flag exists only so future operators don't have to chase a config rename.
- Short / perps / margin / borrow / withdrawal / transfer surfaces. All explicitly disabled.
- Profit promises. Never made. This is a paper-first decision tool with a tiny optional live tester capped at single-digit USDT.
