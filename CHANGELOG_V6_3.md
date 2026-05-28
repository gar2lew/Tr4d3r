# V6.3 — Live Tester Override Fix

Hotfix on top of V6.2. OKX private auth is healthy (after V6.2 +
`OKX_BASE_URL=https://us.okx.com`) and the operator has set every tiny-tester
env flag — yet the UI still showed:

> Live Tester locked — "Training gate not satisfied (use
> LIVE_TESTER_OVERRIDE=...)"

even though `LIVE_TESTER_OVERRIDE=I_UNDERSTAND_THIS_IS_A_TINY_TEST` was loaded.

## Symptom this release fixes

`data/settings.json` (persisted across previous paper-only sessions) silently
overlaid the env-derived `live_tester_override` back to `false` during
`RuntimeSettings.load()`. The downstream tester gate then re-added the
training-gate blocking reason even though the operator's current shell
intent was to bypass it for a $5 USDT-capped test.

A second source of confusion was the UI: the tester panel always rendered
its blockers under a heading that read "Why the live tester is locked",
even when the tester was actually armed and waiting for a signal.

## Behavior change (tester-only)

1. `RuntimeSettings.load()` now **re-asserts** the env-derived
   `live_tester_override` flag *after* the persisted disk overlay. The
   token must still match `I_UNDERSTAND_THIS_IS_A_TINY_TEST` exactly; an
   unset, empty, or mistyped env value continues to mean
   `live_tester_override = False`.
2. `LiveTester.gate_check()` keeps the training gate as a blocking reason
   **only** for the tiny tester when `live_tester_override` is false. The
   full `okx_live` strategy mode gating in `_build_readiness()` is
   **unchanged**: training gate + `LIVE_DEMO_COMPLETED_ACK` +
   `LIVE_TRADING_ACK` + `LIVE_TRADING_ENABLED` are still all required for
   regular live execution.
3. `gate_check()` now also returns a lifecycle hint for the UI:
   `tester_state` ∈ `{disabled, locked, armed, kill_switch}`, an
   `unlocked` boolean, and a human-readable `ready_message`.
4. The readiness payload (`/api/health` and `/api/live/readiness`) carries
   these fields under `readiness.live_tester`. `summary()` continues to
   expose `override_active` as a **boolean only** — the raw env token
   value is never echoed back.

## UI changes

- The tester badge now reads **armed** / **locked** / **kill switch** /
  **disabled** based on the new lifecycle field. The heading above the
  reasons list flips from "Why the live tester is locked" to
  "Live tester status" when the tester is armed, and the list shows a
  single positive line: *"Tiny tester armed; waiting for a qualified
  signal."*
- The top gate banner is now context-aware. When the full OKX live gate
  is locked but the tiny tester is armed, it explicitly says so — instead
  of the previous flat "okx_live gate is LOCKED" which read as if nothing
  could run.

## Safety preserved

- **No** new endpoint that places a trade.
- Tester order caps unchanged: `LIVE_MAX_ORDER_USDT` is still clamped to
  `[1, 25]` USDT, `LIVE_DAILY_LOSS_CAP_USDT` to `[0.5, 25]`,
  `LIVE_MAX_TRADES_PER_DAY` to `[1, 10]`, `LIVE_MAX_OPEN_POSITIONS` to
  `[1, 3]`.
- Protective-exit requirement, spot-only, USDT-quoted, one-position-per-symbol,
  bot-managed stops, kill switch — all untouched.
- No leverage, no perps, no withdrawals, no transfers.
- Override token still **only** unlocks the tiny tester. It does not
  unlock real live execution under `okx_live` strategy mode — the demo
  ack / risk ack / training gate / live-unlock-env are independent checks
  enforced by `_build_readiness()`.

## Files changed

- `app/core/settings.py` — re-assert env-derived `live_tester_override`
  after disk overlay (one comment block + one assignment).
- `app/services/live_tester.py` — `gate_check()` returns lifecycle hints;
  `summary()` clarifies that `override_active` is boolean-only.
- `app/main.py` — propagates `tester_state`, `unlocked`, `ready_message`
  to the readiness payload at `/api/health` and `/api/live/readiness`.
- `web/app.js` — context-aware gate banner, armed/locked badge and
  heading copy, positive ready-message rendering.
- `web/index.html` — added stable `id="ltReasonsHeading"` so the heading
  can be flipped cleanly between "locked" and "armed" copy.
- `.env.example` — clarified comments around `LIVE_TESTER_OVERRIDE`.
- `CHANGELOG_V6_3.md` — this file.

## Tests

- `python -m compileall -q app` — passes.
- `node --check web/app.js` — passes.
- `python _smoke_v6.py` — existing V6 smoke continues to pass
  (`gate_check` rejects when disabled; rejects when OKX not authenticated;
  kill switch engage/release round-trip).

Manual UI sanity:

1. With `LIVE_TESTER_ENABLED=true`, `LIVE_TRADING_ENABLED=true`,
   `LIVE_TRADING_ACK=I_ACCEPT_REAL_MONEY_RISK`,
   `LIVE_TESTER_OVERRIDE=I_UNDERSTAND_THIS_IS_A_TINY_TEST`,
   `LIVE_MAX_ORDER_USDT=5`, `OKX_BASE_URL=https://us.okx.com`, OKX
   authenticated, kill switch off: tester badge reads **armed**, the
   reasons list shows *"Tiny tester armed; waiting for a qualified
   signal."*, and the top banner clarifies that the full live gate is
   still locked.
2. Unset `LIVE_TESTER_OVERRIDE` and restart: tester badge flips back to
   **locked**, list shows the training-gate blocker again.
3. Engage the kill switch: badge flips to **kill switch**.

## Windows install steps

```powershell
# 1. Stop any running brain process (Ctrl-C in the uvicorn window).

# 2. From the project root, extract the hotfix zip on top of the
#    existing checkout. (cd to wherever ai-hummingbot-brain lives.)
cd C:\path\to\ai-hummingbot-brain
Expand-Archive -Force -Path C:\Downloads\AI_HUMMINGBOT_BRAIN_V6_3_LIVE_TESTER_OVERRIDE_FIX.zip -DestinationPath .

# 3. Confirm the env file has every tester flag:
#    LIVE_TESTER_ENABLED=true
#    LIVE_TRADING_ENABLED=true
#    LIVE_TRADING_ACK=I_ACCEPT_REAL_MONEY_RISK
#    LIVE_TESTER_OVERRIDE=I_UNDERSTAND_THIS_IS_A_TINY_TEST
#    LIVE_MAX_ORDER_USDT=5
#    OKX_BASE_URL=https://us.okx.com

# 4. (Optional but recommended) clear the stale persisted override in
#    data/settings.json so a previously-saved `false` does not race the
#    env re-assertion logic on the first request:
#
#    notepad data\settings.json
#      -> delete the "live_tester_override": false line and save,
#         or just delete the whole file (it will be recreated).

# 5. Activate the venv and restart:
.\.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 6. Open http://127.0.0.1:8000 and confirm:
#    - Top banner notes the tiny tester is ARMED while full live is locked.
#    - Live Tester card badge reads "armed".
#    - "Live tester status" section shows the single positive line.
```
