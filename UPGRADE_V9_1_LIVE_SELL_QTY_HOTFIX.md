# Upgrade notes — V9 → V9.1 (Live Sell Qty Hotfix)

This is a drop-in hotfix on top of V9. **No new env vars. No new deps.**
Backend + UI changes only. Native TP/SL (V9) behaviour is preserved.

## What changed in one line

The bot now sells the **net base** (gross filled minus any base-asset fee
OKX deducted), clamped against the live OKX free balance, rounded down to
`lotSz`. If the result is below `minSz`, the sell is refused and the kill
switch is latched with a clear reason instead of looping on rejections.

## Apply (Windows 11 PowerShell, your usual flow)

1. **Stop the running app** (if any). Close the PowerShell window or `Ctrl+C` in the foreground process.
2. **Back up your data dir** before unpacking, just in case:

   ```powershell
   Copy-Item -Recurse ai-hummingbot-brain\data ai-hummingbot-brain\data.backup_v9
   ```

3. Unzip `AI_HUMMINGBOT_BRAIN_V9_1_LIVE_SELL_QTY_HOTFIX.zip` over your existing checkout, **keeping your `.env` and `data/` directory**. (The zip already excludes `.env`, `data/`, `__pycache__/`, `*.pyc`, `.git/`, `node_modules/`, `*.log`, `.DS_Store`.)

4. Verify nothing else changed in `.env`:

   ```powershell
   Get-Content .\ai-hummingbot-brain\.env.example | Select-String 'LIVE_'
   ```

   You should see the same V9 variables, no new keys.

5. Smoke test (no real HTTP, no real orders — safe to run any time):

   ```powershell
   cd ai-hummingbot-brain
   python _smoke_v9.py
   ```

   Expect: `All V9 + V9.1 smoke tests passed (no real HTTP, no real orders).` (23 checks.)

6. Start the app the same way you always do (uvicorn / `python -m app …`).

## First run after upgrade — do this in order

### A. Inspect the kill switch banner

The kill switch is **not** auto-released by V9.1. If V9 had latched it during the DOGE incident, the banner is still showing in the live tester panel. Read the reason first; it now includes the nested OKX `sCode` if any. Do not release until step B is done.

### B. Click **Refresh tester state**

This is the new key step. The refresh path now compares journal sellable qty against the OKX inventory snapshot and, for any stale position where journal > OKX total, sets `inventory_sellable_qty` to the real OKX `total` and emits an `inventory_repair` event in the events log. PnL cost basis (`filled_qty`, `avg_entry_price`) is preserved — only the qty the bot will actually try to sell is clamped.

You should now see, on the position card:

- a yellow banner: **"Journal quantity exceeds OKX inventory; using sellable balance for exits"**, and
- the **Sellable qty** cell showing the journal value with an inline `(OKX: 97.41979720)` suffix.

If you do not see the banner, the journal already matches OKX and nothing needed repair.

### C. Confirm the sellable plan looks right

For the DOGE-like cases, the bot will now send `sz = floor_to_lotSz(min(inventory_sellable_qty, OKX_free))` on the next exit attempt. The next attempted sell will appear in the "Last live order" box with a phase of `opened` (success) or, if your inventory has drifted below `minSz`, the position will be marked `needs_reconcile` and a `live_exit_blocked_low_inventory` event will appear in the events feed. The bot will not loop on rejections.

### D. Release the kill switch — manually

Once steps B and C look correct, click **Release kill switch**. V9.1 never auto-releases; this is by design.

## Operator behaviour to know

- **Fee currency display**. The "Fee deducted" cell now renders the actual currency. A buy that paid `0.6867 DOGE` in fees shows as `0.6867 DOGE`, never `$0.6867`. The "Quote" cell still shows quote-asset spend in USDT.
- **Gross vs Sellable qty**. The Gross cell is the cost-basis qty (used for PnL). The Sellable cell is what the bot is willing to put into a sell payload. Do not edit either; both are derived from fills.
- **`code=1` errors are now legible**. If OKX rejects an order with its generic `All operations failed`, the bot now logs and surfaces the underlying per-order `sCode`/`sMsg`, e.g. `OKX 200 code=1 (sCode=51008): Order failed. Insufficient available balance.`
- **Native protection (V9) still applies** when `LIVE_NATIVE_PROTECTION_ENABLED=true`. Native placement now reuses the same clamp, so the algo `sz` sent to OKX is the same safe value as a manual sell would use.
- **Spot-long only**. Still no shorts, no perps, no leverage.

## Rolling back

If you need to roll back to V9:

1. Stop the app.
2. Restore the V9 zip over your checkout.
3. Delete `data/live_tester/live_state.json` **only** if you saw schema errors on startup; the new fields default to safe values, so this is rarely needed.

## What's intentionally **not** changed

- No env vars added or renamed.
- No change to caps (`LIVE_MAX_ORDER_USDT_TESTER`, `LIVE_DAILY_LOSS_CAP_USDT`, etc.).
- No change to native TP/SL gating logic (V9).
- No change to the unattended-readiness panel (V9).
- The kill switch never auto-releases.

## Smoke command for after deployment

```powershell
cd ai-hummingbot-brain
python -m compileall -q app
python _smoke_v9.py
```

Exit code 0 + final line `All V9 + V9.1 smoke tests passed` = green light to start the app.
