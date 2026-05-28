# V9 Upgrade — OKX Native Exchange TP/SL (Windows PowerShell)

This release adds OKX-native exchange-attached protective sells on top
of the bot-managed exits that have shipped since V6. **It is OFF by
default.** Bot-managed protection continues to run in parallel even
when native is on — it is never replaced.

## Before you start

- Native protection only fires for the tiny live tester's spot LONG
  positions. Paper trading is unaffected.
- The existing OKX API key needs the **Trade** permission. You do not
  need any new credential. Withdrawals/transfers/margin/perps are
  **not** required and must remain disabled.
- This build remains OKX SPOT LONG only. No shorts, perps, margin,
  borrow, leverage, withdrawals, or transfers are placed.

## Install steps

```powershell
# 1. Unzip into a fresh folder (do NOT overwrite your old install in-place;
#    keep V8 around until you have verified V9).
Expand-Archive .\AI_HUMMINGBOT_BRAIN_V9_OKX_NATIVE_TPSL.zip -DestinationPath C:\ai-hummingbot-brain-v9

# 2. Move in.
cd C:\ai-hummingbot-brain-v9

# 3. Set up the venv.
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 4. Copy your previous .env over (preferred) OR start from .env.example.
Copy-Item C:\ai-hummingbot-brain-v8\.env .\.env
# - or -
Copy-Item .env.example .env
notepad .env

# 5. Verify the V8 settings you had carry over. Then add the V9 flags:
#       LIVE_NATIVE_PROTECTION_ENABLED=false   # keep false for first run
#       LIVE_NATIVE_PROTECTION_MODE=oco
#       LIVE_NATIVE_PROTECTION_DRY_RUN=false
#    If you want a hands-on UI test that places NO real algo orders,
#    flip DRY_RUN=true; the position will show "EXCHANGE-NATIVE DRY-RUN".

# 6. Smoke checks (no real network calls).
python -m compileall -q app
node --check web/app.js
python _smoke_v9.py

# 7. Start the server.
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
# Then open http://localhost:8000 in a browser.
```

## Enabling native protection (when you're ready)

1. With the server stopped, edit `.env`:
   ```ini
   LIVE_NATIVE_PROTECTION_ENABLED=true
   LIVE_NATIVE_PROTECTION_MODE=oco
   LIVE_NATIVE_PROTECTION_DRY_RUN=false
   ```
2. Restart the server.
3. The Live Tester panel banner should change to a green
   `Native exchange-attached TP/SL: ENABLED (mode: oco)`.
4. On the next tiny spot buy, the position card should display:
   - Chip: `EXCHANGE-NATIVE ACTIVE`
   - A mono-pill row showing `OCO: <algoId>`
   - A green warning line: "Native TP/SL is live on OKX."
5. Cross-check on OKX: open the **Algo Orders → Open** panel for your
   account. You should see one OCO sell row with your `tpTriggerPx`
   and `slTriggerPx` values.

## What happens on bot-managed close

When the bot's price watcher decides to exit (SL/TP/trail hit), the
tester:

1. Calls `POST /api/v5/trade/cancel-algos` with the stored `algoId`s.
2. Then submits the market sell.

This ordering prevents OKX from firing the attached algo at the same
time the bot sells.

## What happens on native failure

If OKX rejects the algo placement (e.g. `sCode 51000` — invalid
parameter, `51404` — algo not exist on cancel, `51015` — order
quantity less than minimum, etc.) the journal records the verbatim
`sCode`/`sMsg` and the UI shows:

```
NATIVE FAILED – BOT FALLBACK
```

The bot-managed watcher continues running and is your active safety
net. Do not exit the process while a live position is open in this
state.

## Manual controls (operator-facing)

| Action | Endpoint | Body |
| --- | --- | --- |
| Cancel attached native algos (without closing position) | `POST /api/live/tester/native_cancel` | `{"position_id":"lv_…","reason":"manual"}` |
| Re-query OKX for the algo state | `POST /api/live/tester/native_refresh` | `{"position_id":"lv_…"}` |
| Engage kill switch (also cancels native) | `POST /api/live/tester/kill` | `{"reason":"manual"}` |

PowerShell example:

```powershell
$body = @{ position_id = "lv_abc1234567" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/live/tester/native_refresh -ContentType 'application/json' -Body $body
```

## Rollback

If native protection causes issues, set
`LIVE_NATIVE_PROTECTION_ENABLED=false` and restart. The tester
immediately reverts to V8 behaviour (bot-managed exits only). Any
open positions retain their bot-managed exits; manually cancel any
already-attached OKX algos from the OKX UI if needed.

## Files changed

- `app/core/settings.py`
- `app/services/okx_private.py`
- `app/services/live_tester.py`
- `app/main.py`
- `web/index.html`
- `web/app.js`
- `.env.example`
- `CHANGELOG_V9_NATIVE_TPSL.md` (new)
- `UPGRADE_V9_NATIVE_TPSL.md` (new)
- `_smoke_v9.py` (new, mocked unit test — no network)

---

## V9 — Unattended live-test mode (5-day envelope)

**This is NOT safe automation.** It is a tight, risk-reduced envelope for
leaving a *tiny* live test running while you sleep / are away. The UI
banner is explicit: **"SAFE-ish for unattended tiny test"** when every
gate passes — note the "-ish".

If you cannot accept the possibility that:

- OKX rate-limits, partially fills, or rejects your order,
- the bot hits a code path that produces a runtime exception,
- your VPS or home machine reboots,
- the network drops between the bot and OKX,

then **do not flip `LIVE_UNATTENDED_MODE=true`**. The native exchange
TP/SL on OKX is your only protection while the bot itself is offline.

### What unattended mode does

1. Latches the kill switch when `LIVE_UNATTENDED_MAX_HOURS` elapses
   (default 120h = 5 days).
2. Refuses **new** entries when the unattended readiness panel shows
   any FAIL: native disabled, native not actually placed on every open
   position, caps too large, kill switch engaged, watchdog stale,
   market data stale, ≥5 consecutive API errors, daily loss cap
   breached, or the timer has expired.
3. Records every meaningful action (`live_order_opened`,
   `live_exit_fired`, `native_protection_placed/failed/cancelled
   /triggered`, `kill_switch_engaged`, `unattended_armed`,
   `unattended_expired`, `startup_reconcile`) to a persisted ring
   buffer (50 entries) you can review in the UI or via
   `GET /api/live/tester/summary`.
4. Heartbeats from the strategy price-watch loop so you can detect
   stalled loops via `summary().watchdog`.

### What unattended mode does NOT do

- It does **not** auto-close existing open positions when the timer
  expires. The native OCO/SL on OKX continues to protect them at the
  exchange even if the bot exits. The bot stops *opening new* trades.
- It does **not** restart the bot if the process dies. Use your OS-level
  supervisor (`systemd`, `pm2`, Docker `restart: unless-stopped`, etc.).
- It does **not** monitor your VPS / hardware health.

### 5-day unattended checklist

Run through every item before flipping the switch. The Unattended
Readiness panel automates checks 4–11, but humans are still on the
hook for 1–3 and 12–13.

| # | Item | How to verify |
|---|---|---|
| 1 | Recommended caps in `.env` | `LIVE_MAX_ORDER_USDT=5`, `LIVE_DAILY_LOSS_CAP_USDT=3`, `LIVE_MAX_OPEN_POSITIONS=1`, `LIVE_MAX_TRADES_PER_DAY=3` |
| 2 | Tiny test funds only | Move down to ~$50–100 USDT total free balance on OKX. Do NOT leave large balances on the trading sub-account. |
| 3 | OKX API key permissions | "Trade" enabled. **"Withdrawal" DISABLED.** "Read" enabled. No IP allowlist conflicts. |
| 4 | `LIVE_NATIVE_PROTECTION_ENABLED=true` | Settings drawer or `.env`. Native protection chip on each open position should read **EXCHANGE-NATIVE ACTIVE**. |
| 5 | Native verified on every open position | Manually attempt one tiny trade *before* flipping unattended on. Confirm the OCO algo appears in the OKX app under "Algo orders" and the position card shows the OCO algoId. |
| 6 | `LIVE_NATIVE_PROTECTION_RECONCILE_ON_STARTUP=true` | So a process restart re-attaches missing exits. |
| 7 | Kill switch released | Banner not red, "kill switch engaged" not visible. |
| 8 | Watchdog heartbeat is fresh | UI `watchdog` row shows `heartbeat_age_seconds < 30`. |
| 9 | Market data is fresh | `market_data_age_seconds < 120`. |
| 10 | No API error burst | `consecutive_api_errors == 0`. |
| 11 | Daily realised PnL is above the loss cap | `realized_pnl_today > -LIVE_DAILY_LOSS_CAP_USDT`. |
| 12 | Process is supervised | `systemd`, `pm2`, or Docker with `restart: unless-stopped`. |
| 13 | You can reach OKX manually | Mobile OKX app installed and logged in, in case you need to cancel positions remotely. |
| 14 | Readiness banner reads **"SAFE-ish for unattended tiny test"** | In the Live Tester card. If it reads "DO NOT LEAVE UNATTENDED", fix the failing check before walking away. |
| 15 | Flip `LIVE_UNATTENDED_MODE=true` and restart | The first heartbeat after restart will persist `LIVE_UNATTENDED_STARTED_AT`. |

### Recovering from an expired timer

When `LIVE_UNATTENDED_MAX_HOURS` elapses, the bot latches the kill
switch with the reason `unattended timer expired after N.Nh — new
entries blocked; review open positions manually`. To resume:

1. Review every open position on OKX. Decide whether to keep, close,
   or move them.
2. In `.env`, set `LIVE_UNATTENDED_STARTED_AT=0` (so the timer rearms
   from zero) **or** leave `LIVE_UNATTENDED_MODE=false` and run in
   attended mode for a while.
3. Release the kill switch from the UI (the kill-switch row clears).
4. The readiness panel should now show PASS again before you flip
   `LIVE_UNATTENDED_MODE=true`.

### Phrasing rule (please respect this in any forks / forks-of-forks)

Wherever this feature is described in the UI, docs, or commit messages,
use **"risk-reduced tiny live test envelope"**. Never **"safe
automation"** or **"set-and-forget"**. The user has explicitly asked us
not to promise safety or profit, and we don't.
