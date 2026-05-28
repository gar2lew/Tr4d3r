# V6.2 — OKX PRIVATE REST HOST HOTFIX

## Symptom this release fixes

Even after V6.1 surfaced the real OKX error code, the live tester and
`/api/okx/diagnostics` were still returning:

```
HTTP 401  {"code":"50119","msg":"API key doesn't exist"}
url = https://www.okx.com/api/v5/account/balance
```

The user verified, from **inside the same Docker container** running
this app, that the identical signed `GET /api/v5/account/balance`
request returns:

| Host                  | Result                                        |
|-----------------------|-----------------------------------------------|
| `https://www.okx.com` | 401 `{"code":"50119","msg":"API key doesn't exist"}` ❌ |
| `https://us.okx.com`  | 200 `{"code":"0", ...}` ✅                    |
| `https://app.okx.com` | 200 `{"code":"0", ...}` ✅                    |

So the key, secret, passphrase, signature, headers, clock, and IP are
all correct — the request was simply hitting the **wrong OKX host**.
Accounts registered through the US / regional landing pages bind their
API keys to `us.okx.com` / `app.okx.com`; `www.okx.com` rejects them
with the misleading `50119` "API key doesn't exist".

V6.1 hardcoded `OKX_BASE = "https://www.okx.com"`, which is why no
amount of credential re-checking would unstick the auth path.

## Fix

The OKX REST host is now an env-driven setting. Default flipped to
`https://us.okx.com`; operators on the global / EU host can override.

### Changed files

- `app/services/okx_private.py`
  - New `okx_base_url()` reader. Honours `OKX_BASE_URL` (trimmed,
    trailing-slash stripped), defaults to `https://us.okx.com`.
  - `_request()` now signs and sends against `okx_base_url()` and
    records `base_url` in the returned `meta` dict.
  - `fetch_server_time()` also uses `okx_base_url()`.
  - `diagnose_private_auth()` returns `base_url`, `base_url_default`,
    and `base_url_overridden` so the UI can show the active host.
  - `get_account_snapshot()` returns `base_url`.
  - `_classify_okx_error()` accepts `base_url` and, on code `50119`,
    appends a host-mismatch hint that names the exact env var and
    value to set (`OKX_BASE_URL=https://us.okx.com`).
  - Hint text for `50119` updated to mention the V6.2 escape hatch.
  - Network-error hint now reports the host that was unreachable.
  - The legacy module-level `OKX_BASE` constant is preserved (pointed
    at the default) for back-compat, but no live code path reads it.

- `app/services/market_data.py`
  - New `_okx_public_base()` reader. Honours `OKX_PUBLIC_BASE_URL`,
    falls through to `OKX_BASE_URL`, defaults to `https://www.okx.com`
    (unsigned public endpoints work on every host; default kept for
    backward compatibility).

- `web/index.html`
  - OKX account card now shows the active **Base URL (private)**.
  - OKX auth diagnostic panel adds the same line plus a source hint
    ("from `OKX_BASE_URL` env" vs "default").

- `web/app.js`
  - Renders `diag.base_url` / `diag.base_url_overridden` and
    `snap.base_url` from the existing payload.

- `.env.example`
  - Adds `OKX_BASE_URL=https://us.okx.com` with a documented block
    explaining the `50119` failure mode and which host to pick.
  - Adds optional `OKX_PUBLIC_BASE_URL` for the public market-data
    host (blank by default → inherits `OKX_BASE_URL`).

### Routes the env reaches

- `/api/okx/account` — uses `okx_private.get_account_snapshot()`.
- `/api/okx/diagnostics` — uses `okx_private.diagnose_private_auth()`.
- `/api/live/account` — alias of `/api/okx/account`.
- Live-tester order placement — uses `okx_private.market_buy_spot()`,
  `market_sell_spot()`, `fetch_order()`, `summarize_fills()`,
  `spot_base_balance()` — all of which sign through `_request()` and
  therefore inherit `OKX_BASE_URL`.

## Safety preserved

- Spot-only. No new POST endpoints, no withdrawal / margin / futures.
- Live-trade gating (`LIVE_TRADING_ENABLED`,
  `LIVE_TRADING_ACK=I_ACCEPT_REAL_MONEY_RISK`, `OKX_DEMO`, V5.4 gate)
  is untouched.
- Secrets remain server-side. Diagnostics still echo only redacted
  `xx***yy (len=N)` fingerprints.
- Public market data continues to default to `https://www.okx.com`
  unless you opt in to a shared host via `OKX_PUBLIC_BASE_URL`.

## Tests run

```
$ python -m compileall -q app          → PY OK
$ node --check web/app.js              → JS OK
```

## Likely-cause text shown by the UI

When OKX returns `50119` and the signed call was made against
`www.okx.com`, the diagnostic panel now adds:

> You are currently signing against `https://www.okx.com`. The user
> verified that the same key returns 200 code:0 on
> `https://us.okx.com` and `https://app.okx.com`. Set
> `OKX_BASE_URL=https://us.okx.com` in `.env` and
> `docker compose up --build --force-recreate`.

## Windows PowerShell install — V6.2 hotfix

```powershell
# 1. From the project folder containing docker-compose.yml
cd C:\path\to\ai-hummingbot-brain

# 2. Extract the hotfix zip on top of the existing tree (your .env and
#    your data/ directory are NOT in the zip and will be preserved).
Expand-Archive -Path .\AI_HUMMINGBOT_BRAIN_V6_2_OKX_BASE_URL_HOTFIX.zip `
               -DestinationPath . -Force

# 3. Add the new env var to YOUR .env (NOT .env.example):
Add-Content -Path .\.env -Value "OKX_BASE_URL=https://us.okx.com"
#    If you already have OKX_BASE_URL, edit it instead.

# 4. Rebuild and restart the container so the new code + env load:
docker compose down
docker compose up --build --force-recreate -d

# 5. Open http://127.0.0.1:8787 → OKX card should now show
#    "Base URL (private): https://us.okx.com" and the diagnostic should
#    flip to "private auth ok" (http 200, code 0).
```

If `https://us.okx.com` does not work for your account, try
`https://www.okx.com` or `https://app.okx.com` in step 3.
