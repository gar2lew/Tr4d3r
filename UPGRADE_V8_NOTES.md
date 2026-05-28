# Upgrade to V8 — Windows PowerShell walkthrough

This walkthrough installs the V8 Full Fix build on Windows 11 using PowerShell.
**No real orders are placed by these steps. No API keys are required to start the app on paper mode.**

> Reminder: this build is **OKX spot-long only**. Shorts, perps, margin, borrow, transfers, and native exchange-attached TP/SL are **disabled by default**. Native protection placeholder `LIVE_NATIVE_PROTECTION_ENABLED` defaults to `false` and is ignored by the order-placement path.

---

## 1. Unpack the zip

```powershell
# Adjust the destination path as you like.
Expand-Archive -Path .\AI_HUMMINGBOT_BRAIN_V8_FULL_FIX.zip -DestinationPath C:\ai-hummingbot-brain-v8
cd C:\ai-hummingbot-brain-v8
```

## 2. Create a fresh virtual environment

PowerShell scripts must be allowed for the current session:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If `python` is not on PATH, install Python 3.11+ from python.org and tick *Add to PATH* in the installer.

## 3. Install dependencies

```powershell
pip install --upgrade pip
pip install -r requirements.txt
```

## 4. Copy the example env file

```powershell
Copy-Item .env.example .env
```

Open `.env` in your editor and review the V8 defaults. The values below ship pre-set; only change them if you know what you are doing.

```dotenv
# --- safety defaults (do not loosen unless you know why) ---
EXECUTION_MODE=paper
LIVE_TRADING_ENABLED=false
LIVE_TESTER_ENABLED=false
LIVE_NATIVE_PROTECTION_ENABLED=false      # V8 placeholder — never enable
LIVE_MAX_ORDER_USDT=5                     # tiny-test envelope
LIVE_DAILY_LOSS_CAP_USDT=3                # tiny-test envelope

# --- OKX host (US accounts) ---
OKX_BASE_URL=https://us.okx.com
# OKX_BASE_URL=https://www.okx.com        # global / EU host (uncomment if your account was created there)
# OKX_BASE_URL=https://eea.okx.com        # EU-only

# --- scanner / market visibility (V7/V8) ---
SCAN_INTERVAL_SECONDS=30
MAX_SCAN_SYMBOLS=25
DYNAMIC_SYMBOL_DISCOVERY=true
MAX_DYNAMIC_SYMBOLS=20
OKX_CANDLE_FETCH_CONCURRENCY=3
MARKET_INTEL_PROVIDER=none
```

If you are a US-domiciled OKX account, keep `OKX_BASE_URL=https://us.okx.com`. If you signed up on the global/EU host, swap to `https://www.okx.com` or `https://eea.okx.com` and let the UI's OKX diagnostic panel confirm which host actually answered.

## 5. Smoke-test the install (no network needed)

```powershell
# Python sanity
python -m compileall -q app

# JS sanity (requires Node 18+)
node --check web\app.js
```

Both commands should exit with code 0 and print nothing.

## 6. Start the server

```powershell
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open <http://localhost:8000> in your browser. You should see:

- A red **"PAPER MODE — LIVE LOCKED"** pill in the topbar.
- A new V8 **"V8 live mode: OKX SPOT LONG only"** pill next to it.
- The V7 sticky status strip with Scanner / Clean / Skipped / Last scan / Queue / **Next scan** / **Universe** / Direction tiles.
- The market scan table with the new **Direction** column.
- A live-tester card carrying the **"Native exchange-attached TP/SL: NOT ENABLED"** placeholder banner.

## 7. (Optional) Enable the tiny live tester

Only do this after the training gate has passed in paper mode and you have read `.env.example` end-to-end.

```dotenv
LIVE_TESTER_ENABLED=true
EXECUTION_MODE=okx_demo            # start on demo first
OKX_API_KEY=...                    # demo keys
OKX_API_SECRET=...
OKX_API_PASSPHRASE=...
# DO NOT touch LIVE_NATIVE_PROTECTION_ENABLED. Leave it false.
```

Restart the server. The live-tester card will surface caps warnings if you go above `LIVE_MAX_ORDER_USDT=5` or `LIVE_DAILY_LOSS_CAP_USDT=3`.

## 8. Stopping / resetting

```powershell
# Stop server: Ctrl+C in the uvicorn window
# Reset paper account (from the UI: Control deck → Reset paper account)
# Wipe the local data folder if you want a totally clean slate:
Remove-Item -Recurse -Force .\data
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|--------|--------------|-----|
| `python` not recognised | Python not on PATH | Re-install Python 3.11+ with "Add to PATH" ticked. |
| `Activate.ps1` blocked | Execution policy | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`. |
| OKX 401 `API key doesn't exist` | Wrong host for the account | Try the other two `OKX_BASE_URL` values (`us.okx.com` / `www.okx.com` / `eea.okx.com`). |
| Live tester says "locked" with override on | Some other gate is failing — check the reason list under "Why the live tester is locked". | Resolve the listed reasons; override only suppresses the training-gate item. |
| Position shows red **UNPROTECTED** banner | Open live position has no recorded SL/TP/trail | Close on OKX manually or hit the kill switch. The bot has no exit plan in that state. |

---

## What V8 explicitly does **not** do

- Place real shorts, perps, margin, borrow, or withdrawals.
- Attach native exchange-side TP/SL to live OKX orders (the `LIVE_NATIVE_PROTECTION_ENABLED` flag is a placeholder and is ignored).
- Promise any profit.
