# V6.1 — OKX AUTH DIAGNOSTIC HOTFIX

## Symptom this release fixes

After switching to a new OKX API key the live tester still reported

```
HTTPStatusError 401 Unauthorized for url 'https://www.okx.com/api/v5/account/balance'
```

…with no further detail. The 401 response body from OKX (which contains
the *real* error code such as `50111 Invalid OK-ACCESS-KEY` or
`50113 Invalid Sign`) was being thrown away by `httpx.raise_for_status()`
before the JSON payload was parsed.

## What was reviewed

The signing implementation in `app/services/okx_private.py` was audited
against OKX v5 spec. Conclusion: **the signature scheme is correct**.

| Requirement | Implementation | OK? |
|---|---|---|
| Timestamp ISO8601 UTC w/ milliseconds + `Z` | `datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")` | yes |
| Prehash = `ts + UPPER(method) + path(?query) + body` | `f"{ts}{method.upper()}{path}{body}"` | yes |
| HMAC-SHA256 with secret, base64 of digest | `base64.b64encode(hmac.new(secret, prehash, sha256).digest())` | yes |
| Headers (`OK-ACCESS-KEY/SIGN/TIMESTAMP/PASSPHRASE`, `Content-Type`) | All present | yes |
| `x-simulated-trading` only when `OKX_DEMO=true` | Conditional on `creds.demo` | yes |
| GET body empty string in prehash | `body = ""` when method is GET | yes |
| Query string included in signed path | `path + "?" + urlencode(...)` before `_headers` | yes |

The persistent 401 therefore points at the credentials / key environment
(demo vs. live, passphrase, IP whitelist, permissions, clock skew) — not
at the signing logic. This release surfaces those signals.

## Changes

### `app/services/okx_private.py`

- New exception `OKXAuthError(RuntimeError)` carrying `http_status`,
  `okx_code`, `okx_msg`, parsed body.
- `_request()` no longer calls `r.raise_for_status()`. The response body
  is parsed even on 4xx/5xx so OKX `code`/`msg` survive. Failed calls
  raise `OKXAuthError` with the parsed fields. When `return_meta=True`
  the call returns `(data, meta)` and attaches `meta` to the exception.
- New `fetch_server_time()` — unsigned `GET /api/v5/public/time` used to
  measure clock skew without needing valid credentials.
- New `diagnose_private_auth()` — read-only single call to
  `/api/v5/account/balance` returning:
  - `local_clock_utc`, `local_clock_ms`
  - `okx_server_time_ms`, `clock_skew_ms`, `clock_skew_warning`
  - `credentials_fingerprint` (redacted — `xx***yy (len=N)` only)
  - `http_status`, `okx_code`, `okx_msg`, `request_path`,
    `timestamp_used`, `demo_header_used`
  - `likely_causes[]`, `next_steps[]` — actionable hint strings.
- New `_classify_okx_error()` — maps known OKX codes (50101, 50102,
  50104, 50110, 50111, 50112, 50113, 50114, 50119, …) to fix hints.
- `get_account_snapshot()` now records `okx_code`, `okx_msg`,
  `http_status` on its returned dict when the call fails.
- No order-placement, withdrawal, transfer, margin, futures, or earn
  endpoints added or modified. Spot read-only remains the only call
  path used by the diagnostic.

### `app/main.py`

- New `GET /api/okx/diagnostics` — returns `{ "diagnostic": {...} }`
  containing the full report. **Read-only. Does not place orders.**
- `GET /api/okx/account` now also returns a `diagnostic` block when the
  account is configured but not authenticated.

### `web/index.html` + `web/app.js`

- New “Test OKX private auth” button in the OKX real-account card.
- New diagnostic sub-panel showing: HTTP status, OKX code/msg,
  request path, demo-header flag, clock skew, redacted key fingerprint,
  likely causes, next steps. Hidden when auth is healthy.
- Existing “Last error” line now prefixes the OKX `code=` when present.

## Safety preserved

- Live execution gating untouched: `LIVE_TRADING_ENABLED`,
  `LIVE_TRADING_ACK`, `OKX_DEMO`, live-tester caps still required.
- Diagnostics path performs exactly one read-only `GET` and one unsigned
  `GET /public/time`. No POST endpoints are added or invoked.
- Secrets never leave the server. UI shows only fingerprints
  (`xx***yy (len=N)`). Logs do not contain raw key material.

## Smoke tests

```
$ python -m compileall -q app  →  PY OK
$ node --check web/app.js      →  JS OK
```

End-to-end smoke against OKX with deliberately invalid credentials:

```
http_status 401  okx_code 50111  okx_msg "Invalid OK-ACCESS-KEY"
likely_causes[0] = Invalid OK-ACCESS-KEY. Double-check OKX_API_KEY ...
clock_skew_ms = -161  (no warning)
fingerprint   = api_key fa***90 (len=17) · secret FA***90 (len=20) · passphrase Fa***1! (len=10) · demo=False
```

confirming both signing and HTTP-error parsing are correct.
