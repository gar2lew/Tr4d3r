"""OKX private REST adapter.

V6 — extended for the Tiny OKX Live Tester. Adds:

- Detailed account / balance snapshot for the UI (free/total USDT,
  non-zero asset summary, permissions). Keys are never echoed back.
- Best-effort fetch of recent fills for a freshly placed market order so
  the live tester can record realised qty and average price.
- Open spot positions read by base (so the duplicate-symbol guard can
  see whether a coin is already held on the exchange).

V6.1 — OKX AUTH DIAGNOSTIC HOTFIX. The private request helper now
retains the OKX JSON error body on 4xx/5xx responses (instead of
throwing only ``HTTPStatusError``), exposes a structured diagnostics
dict, and a read-only ``diagnose_private_auth`` method that returns
rich hints when ``/api/v5/account/balance`` rejects the request. No
secrets are returned — only redacted fingerprints.

V6.2 — OKX BASE URL HOTFIX. The private REST host is now driven by
the ``OKX_BASE_URL`` env var (default ``https://us.okx.com``). Some
accounts — notably those registered through US / regional landing
pages — only resolve their API keys against ``us.okx.com`` /
``app.okx.com`` and return ``50119 API key doesn't exist`` against
``www.okx.com`` even when the key is perfectly valid. This release
lets the operator pick the correct host without code changes and
surfaces the chosen host in diagnostics and in the UI.

The adapter remains gated: it does not place orders unless explicitly
called by the live tester, which itself requires its own opt-in env
flags. Withdrawals, transfers, margin, futures, options, and earn/yield
endpoints are intentionally not implemented.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx


# V6.2 — default flipped from ``https://www.okx.com`` to
# ``https://us.okx.com``. The user verified that the same signed GET
# ``/api/v5/account/balance`` returns 200 ``code:0`` against both
# ``us.okx.com`` and ``app.okx.com`` but 401 ``code:50119`` against
# ``www.okx.com``. Operators on the EU / global host can override via
# the ``OKX_BASE_URL`` environment variable.
_DEFAULT_OKX_BASE = "https://us.okx.com"
_ALLOWED_OKX_BASES = (
    "https://us.okx.com",
    "https://app.okx.com",
    "https://www.okx.com",
    "https://eea.okx.com",
)


def okx_base_url() -> str:
    """Return the OKX REST base URL, honouring ``OKX_BASE_URL``.

    Trims whitespace and any trailing slash. Falls back to the V6.2
    default (``https://us.okx.com``) when unset or empty. The value is
    intentionally read on every call so a docker-compose restart with a
    new ``.env`` picks up the change without a process restart loop.
    """
    raw = (os.getenv("OKX_BASE_URL") or "").strip()
    if not raw:
        return _DEFAULT_OKX_BASE
    if raw.endswith("/"):
        raw = raw.rstrip("/")
    return raw


# Back-compat: a small number of legacy call-sites used the module-level
# constant. We keep the name pointing at the *default* host so any
# import-time reference resolves, but every live code path goes through
# :func:`okx_base_url` so the env override actually takes effect.
OKX_BASE = _DEFAULT_OKX_BASE


class OKXAuthError(RuntimeError):
    """Raised when OKX rejects a private request.

    Carries the parsed OKX body so callers can surface ``code``/``msg``
    without re-parsing transport errors. Never carries secrets.
    """

    def __init__(
        self,
        message: str,
        *,
        http_status: int = 0,
        okx_code: str = "",
        okx_msg: str = "",
        body: Optional[dict] = None,
    ) -> None:
        super().__init__(message)
        self.http_status = int(http_status or 0)
        self.okx_code = str(okx_code or "")
        self.okx_msg = str(okx_msg or "")
        self.body = body or {}


# Map of well-known OKX error codes to actionable hints. The mapping is
# intentionally narrow: only the codes that the diagnostic flow actually
# triggers on. See https://www.okx.com/docs-v5/en/#error-code .
_OKX_HINTS: Dict[str, str] = {
    "50101": "APIKey does not match current environment. If you are on LIVE (OKX_DEMO=false) the key must be a LIVE key; demo keys are issued separately on the OKX demo site.",
    "50102": "Server timestamp is out of sync. Your local clock is too far from OKX server time — sync NTP / system clock.",
    "50103": "Missing OK-ACCESS-KEY header. Check that OKX_API_KEY is populated and not whitespace.",
    "50104": "Missing OK-ACCESS-PASSPHRASE header. The passphrase you set when creating the API key must be in OKX_API_PASSPHRASE — this is NOT your login password.",
    "50105": "Missing OK-ACCESS-TIMESTAMP header.",
    "50106": "Missing OK-ACCESS-SIGN header.",
    "50107": "Missing Content-Type header.",
    "50111": "Invalid OK-ACCESS-KEY. Double-check OKX_API_KEY for trimming/typos and that the key has not been revoked.",
    "50112": "Invalid OK-ACCESS-PASSPHRASE. The passphrase value does not match the one bound to this API key.",
    "50113": "Invalid signature. The HMAC-SHA256 prehash is being computed incorrectly OR the secret is wrong. Confirm OKX_API_SECRET has no trailing newline/space.",
    "50114": "Invalid Authority — API key permissions are insufficient. Grant the 'Read' permission to this key on OKX.",
    "50110": "IP is not in the API key whitelist. Add this server's outbound IP to the key's IP allow-list on OKX, or remove the whitelist.",
    "50119": "API key does not exist on this host. Re-check OKX_API_KEY — it may belong to a different account, a deleted key, or the wrong environment (demo vs live). V6.2 NOTE: if curl returns 50119 on https://www.okx.com but code 0 on https://us.okx.com or https://app.okx.com, set OKX_BASE_URL=https://us.okx.com in .env (the key is bound to the US / regional host).",
    "50100": "API is frozen or this endpoint is temporarily restricted. Try again later or check the OKX status page.",
}


def _classify_okx_error(
    *,
    http_status: int,
    okx_code: str,
    okx_msg: str,
    demo_header_used: bool,
    base_url: str = "",
) -> dict:
    """Return a structured fix hint for an OKX auth failure.

    The classification is heuristic: when OKX returns a recognised code
    we surface a precise hint; otherwise we degrade gracefully and offer
    the broad set of common 401 causes.
    """
    code = str(okx_code or "")
    msg_lc = (okx_msg or "").lower()
    likely: List[str] = []
    next_steps: List[str] = []

    if code and code in _OKX_HINTS:
        likely.append(_OKX_HINTS[code])
    # V6.2 — host-mismatch surfacing. When 50119 is returned we tell
    # the operator exactly which host was hit so they can compare with
    # the curl test against us.okx.com / app.okx.com.
    if code == "50119" and base_url:
        if "www.okx.com" in base_url:
            likely.append(
                f"You are currently signing against {base_url}. The user verified that the same key returns 200 code:0 on https://us.okx.com and https://app.okx.com. Set OKX_BASE_URL=https://us.okx.com in .env and docker compose up --build --force-recreate."
            )
        else:
            likely.append(
                f"You are currently signing against {base_url}. If your key was created on a different OKX landing page, try OKX_BASE_URL=https://www.okx.com or https://app.okx.com."
            )
    if "ip" in msg_lc and ("whitelist" in msg_lc or "allow" in msg_lc or "not in" in msg_lc):
        likely.append("IP whitelist mismatch — this server's outbound IP is not allowed for this API key.")
    if "passphrase" in msg_lc:
        likely.append("Passphrase mismatch — OKX_API_PASSPHRASE must match the value set when creating the key (NOT your login password).")
    if "sign" in msg_lc or "signature" in msg_lc:
        likely.append("Signature mismatch — verify OKX_API_SECRET is exact (no trailing whitespace) and your server clock is in sync.")
    if "timestamp" in msg_lc:
        likely.append("Timestamp out of range — sync the server clock with NTP.")
    if "permission" in msg_lc or "authority" in msg_lc:
        likely.append("API key is missing the 'Read' permission for the account endpoint.")

    if http_status == 401 and not likely:
        # Generic 401 with no diagnostic body — list the canonical causes.
        likely.extend([
            "Invalid API key, secret, or passphrase (most common).",
            f"Demo/live mismatch — current request used demo header = {demo_header_used}. Demo keys only work when OKX_DEMO=true; live keys only work when OKX_DEMO=false.",
            "IP whitelist — this server's outbound IP is not in the key's allow-list.",
            "API key missing the 'Read' permission.",
            "Clock skew — local UTC must be within ~30s of OKX server time.",
        ])

    # Concrete next steps the user can try right now.
    next_steps.append("Confirm OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE are the new values and free of leading/trailing whitespace.")
    if demo_header_used:
        next_steps.append("You are signing with x-simulated-trading=1 (demo). Make sure the key was created on the OKX demo trading site (not the live site).")
    else:
        next_steps.append("You are signing WITHOUT the demo header (live). Make sure the key was created on the OKX live site, and OKX_DEMO=false in your env.")
    next_steps.append("On OKX, confirm the key has 'Read' permission and that your server's outbound IP is whitelisted (or whitelist is disabled).")
    next_steps.append("Hit /api/okx/diagnostics — it reports server-time delta; if |delta| > 30s, fix the server clock.")

    return {
        "likely_causes": likely,
        "next_steps": next_steps,
    }


def _okx_inst(symbol: str) -> str:
    return symbol.replace("/", "-").upper()


def _redact(s: str) -> str:
    """Return a non-sensitive fingerprint of an API credential."""
    if not s:
        return ""
    if len(s) <= 6:
        return "***"
    return f"{s[:2]}***{s[-2:]} (len={len(s)})"


@dataclass(frozen=True)
class OKXCredentials:
    api_key: str
    api_secret: str
    passphrase: str
    demo: bool = True

    @property
    def present(self) -> bool:
        return bool(self.api_key and self.api_secret and self.passphrase)

    def fingerprint(self) -> dict:
        return {
            "api_key": _redact(self.api_key),
            "api_secret": _redact(self.api_secret),
            "passphrase": _redact(self.passphrase),
            "demo": self.demo,
        }


class OKXPrivateAdapter:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=4.0))
        self._last_status: dict = {
            "configured": False,
            "authenticated": False,
            "mode": "paper",
            "reason": "not checked",
        }
        # Cache the most recent account snapshot so the UI panel can show
        # something even when we can't probe on every poll. The snapshot
        # never contains key material.
        self._last_account: dict = {
            "configured": False,
            "authenticated": False,
            "mode": "paper",
            "demo": True,
            "checked_ts": 0.0,
            "usdt_total": 0.0,
            "usdt_available": 0.0,
            "usdt_frozen": 0.0,
            "total_eq_usd": 0.0,
            "assets": [],
            "permissions": [],
            "last_error": "",
        }
        # V9.1 — instrument-info cache (lotSz/minSz/tickSz). Populated
        # lazily via fetch_instrument(); used by the live tester to round
        # a sellable quantity down to the exchange's lot size before
        # submitting a sell order.
        self._inst_cache: Dict[str, dict] = {}

    @property
    def status(self) -> dict:
        return dict(self._last_status)

    @property
    def last_account(self) -> dict:
        return dict(self._last_account)

    async def close(self) -> None:
        await self._client.aclose()

    def credentials(self) -> OKXCredentials:
        return OKXCredentials(
            api_key=os.getenv("OKX_API_KEY", "").strip(),
            api_secret=os.getenv("OKX_API_SECRET", "").strip(),
            passphrase=os.getenv("OKX_API_PASSPHRASE", "").strip(),
            demo=os.getenv("OKX_DEMO", "true").strip().lower() in ("1", "true", "yes", "on"),
        )

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _headers(self, creds: OKXCredentials, method: str, path: str, body: str = "") -> dict:
        ts = self._timestamp()
        prehash = f"{ts}{method.upper()}{path}{body}"
        sign = base64.b64encode(hmac.new(
            creds.api_secret.encode(),
            prehash.encode(),
            hashlib.sha256,
        ).digest()).decode()
        headers = {
            "OK-ACCESS-KEY": creds.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": creds.passphrase,
            "Content-Type": "application/json",
        }
        if creds.demo:
            headers["x-simulated-trading"] = "1"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        return_meta: bool = False,
    ) -> dict:
        """Sign and send a private OKX REST request.

        V6.1: we no longer call ``raise_for_status()``. Instead we parse
        the response body even when the HTTP status is 4xx/5xx so that
        callers (and the diagnostics endpoint) can surface the OKX
        ``code`` / ``msg`` that explain why the request was rejected.

        When ``return_meta`` is true a second dict is returned alongside
        the body containing diagnostic metadata (timestamp used,
        request path that was signed, demo header flag, http status,
        okx_code, okx_msg). The metadata never contains secret material.
        """
        creds = self.credentials()
        if not creds.present:
            raise RuntimeError("OKX credentials missing")
        # GET requests with query strings — include them in the signed path.
        body = json.dumps(payload, separators=(",", ":")) if (payload and method.upper() != "GET") else ""
        if payload and method.upper() == "GET":
            from urllib.parse import urlencode
            path = path + "?" + urlencode({k: v for k, v in payload.items() if v is not None and v != ""})
        headers = self._headers(creds, method, path, body)
        ts_used = headers.get("OK-ACCESS-TIMESTAMP", "")
        demo_header_used = "x-simulated-trading" in headers
        base_url = okx_base_url()
        r = await self._client.request(
            method,
            base_url + path,
            content=body if body else None,
            headers=headers,
        )
        # Parse JSON body even on 4xx/5xx so OKX error code/msg survive.
        data: dict = {}
        try:
            data = r.json()
        except Exception:
            data = {"_raw_text": (r.text or "")[:512]}
        okx_code = str(data.get("code") or "")
        okx_msg = str(data.get("msg") or "")
        meta = {
            "http_status": r.status_code,
            "request_path": path,
            "method": method.upper(),
            "timestamp_used": ts_used,
            "demo_header_used": demo_header_used,
            "okx_code": okx_code,
            "okx_msg": okx_msg,
            "base_url": base_url,
        }
        if r.status_code >= 400 or (okx_code not in ("", "0")):
            # V9.1 — OKX returns a top-level ``code=1`` envelope with msg
            # "All operations failed" when *any* per-order row in ``data``
            # failed; the real reason (e.g. ``51008`` insufficient balance
            # or ``51400`` lot size) sits in ``data[i].sCode``/``sMsg``.
            # The V9 build hid those rows, which made the live tester
            # surface the unhelpful "All operations failed" string in the
            # last-attempt panel. We surface the first non-zero nested
            # row so the operator sees what actually went wrong.
            nested_code = ""
            nested_msg = ""
            try:
                for row in (data.get("data") or []):
                    s_code = str(row.get("sCode") or "")
                    s_msg = str(row.get("sMsg") or "")
                    if s_code and s_code != "0":
                        nested_code = s_code
                        nested_msg = s_msg
                        break
            except Exception:
                pass
            if nested_code:
                err_msg = (
                    f"OKX {r.status_code} code={okx_code or '-'} "
                    f"(sCode={nested_code}): {nested_msg or okx_msg or 'request failed'}"
                )
            else:
                err_msg = f"OKX {r.status_code} code={okx_code or '-'}: {okx_msg or 'request failed'}"
            err = OKXAuthError(
                err_msg,
                http_status=r.status_code,
                okx_code=okx_code,
                okx_msg=okx_msg,
                body=data,
            )
            # Attach the surfaced nested codes for callers that want
            # structured diagnostics without re-parsing the body.
            err.nested_s_code = nested_code  # type: ignore[attr-defined]
            err.nested_s_msg = nested_msg    # type: ignore[attr-defined]
            if return_meta:
                # Caller wants to inspect the failure — attach meta and
                # raise so the regular code path stays simple.
                err.meta = meta  # type: ignore[attr-defined]
            raise err
        if return_meta:
            return data, meta  # type: ignore[return-value]
        return data

    # ---------------- public helpers ----------------

    async def fetch_server_time(self) -> Tuple[Optional[int], Optional[str]]:
        """Return (server_time_ms, error) from the unsigned /public/time endpoint.

        Used by the diagnostics flow to estimate clock skew. The endpoint
        is unsigned so it works even when the API key is broken.
        """
        try:
            r = await self._client.get(okx_base_url() + "/api/v5/public/time")
            data = r.json() or {}
            rows = data.get("data") or []
            if rows:
                ts = int(rows[0].get("ts") or 0)
                if ts:
                    return ts, None
            return None, f"unexpected body: {str(data)[:120]}"
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"

    # ---------------- account / readiness ----------------

    async def fetch_balance_raw(self) -> dict:
        return await self._request("GET", "/api/v5/account/balance")

    async def fetch_account_config(self) -> dict:
        try:
            return await self._request("GET", "/api/v5/account/config")
        except Exception:
            return {}

    async def get_account_snapshot(self) -> dict:
        """Read-only account snapshot used by /api/okx/account.

        Returns a flat dict suitable for direct UI rendering. Never contains
        API key/secret/passphrase. Caches the result on ``last_account``.
        """
        creds = self.credentials()
        snap = {
            "configured": creds.present,
            "authenticated": False,
            "mode": "okx_demo" if creds.demo else "okx_live",
            "demo": creds.demo,
            "checked_ts": time.time(),
            "usdt_total": 0.0,
            "usdt_available": 0.0,
            "usdt_frozen": 0.0,
            "total_eq_usd": 0.0,
            "assets": [],
            "permissions": [],
            "last_error": "",
            "credentials_fingerprint": creds.fingerprint() if creds.present else {},
            "base_url": okx_base_url(),
        }
        if not creds.present:
            snap["last_error"] = "OKX_API_KEY, OKX_API_SECRET, or OKX_API_PASSPHRASE missing"
            self._last_account = snap
            return snap
        try:
            data = await self.fetch_balance_raw()
            row = (data.get("data") or [{}])[0]
            details = row.get("details", [])
            assets: List[dict] = []
            for d in details:
                ccy = (d.get("ccy") or "").upper()
                try:
                    bal = float(d.get("eq") or d.get("cashBal") or 0)
                except Exception:
                    bal = 0.0
                try:
                    avail = float(d.get("availBal") or d.get("availEq") or 0)
                except Exception:
                    avail = 0.0
                try:
                    frozen = float(d.get("frozenBal") or 0)
                except Exception:
                    frozen = 0.0
                try:
                    usd_value = float(d.get("eqUsd") or 0)
                except Exception:
                    usd_value = 0.0
                if bal == 0 and avail == 0 and frozen == 0 and usd_value == 0:
                    continue
                if ccy == "USDT":
                    snap["usdt_total"] = bal
                    snap["usdt_available"] = avail
                    snap["usdt_frozen"] = frozen
                assets.append({
                    "ccy": ccy,
                    "total": bal,
                    "available": avail,
                    "frozen": frozen,
                    "usd_value": usd_value,
                })
            try:
                snap["total_eq_usd"] = float(row.get("totalEq") or 0)
            except Exception:
                snap["total_eq_usd"] = 0.0
            snap["assets"] = sorted(assets, key=lambda a: a.get("usd_value", 0), reverse=True)
            snap["authenticated"] = True
            # Best-effort permissions read
            try:
                cfg = await self.fetch_account_config()
                cfg_row = (cfg.get("data") or [{}])[0] if cfg else {}
                perms_raw = cfg_row.get("perm") or cfg_row.get("permission") or ""
                if isinstance(perms_raw, str) and perms_raw:
                    snap["permissions"] = [p.strip() for p in perms_raw.split(",") if p.strip()]
            except Exception:
                pass
        except OKXAuthError as e:
            snap["last_error"] = str(e)
            snap["okx_code"] = e.okx_code
            snap["okx_msg"] = e.okx_msg
            snap["http_status"] = e.http_status
        except Exception as e:
            snap["last_error"] = f"{type(e).__name__}: {e}"
        self._last_account = snap
        return snap

    async def diagnose_private_auth(self) -> dict:
        """Read-only diagnostic for the private auth path.

        Calls ``/api/v5/account/balance`` exactly once and returns a
        structured report with timestamps, clock skew vs. OKX server
        time, redacted credential fingerprint, and a list of likely
        causes + next steps when the call fails. Never places orders.
        """
        creds = self.credentials()
        local_ts_ms = int(time.time() * 1000)
        local_iso = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        server_ts, server_err = await self.fetch_server_time()
        skew_ms: Optional[int] = None
        if server_ts:
            skew_ms = local_ts_ms - server_ts
        base_url = okx_base_url()
        env_override = bool((os.getenv("OKX_BASE_URL") or "").strip())
        report: Dict[str, Any] = {
            "checked_ts": time.time(),
            "configured": creds.present,
            "demo": creds.demo,
            "mode": "okx_demo" if creds.demo else "okx_live",
            "local_clock_utc": local_iso,
            "local_clock_ms": local_ts_ms,
            "okx_server_time_ms": server_ts,
            "okx_server_time_error": server_err,
            "clock_skew_ms": skew_ms,
            "clock_skew_warning": (skew_ms is not None and abs(skew_ms) > 30000),
            "credentials_fingerprint": creds.fingerprint() if creds.present else {},
            "private_auth_ok": False,
            "http_status": 0,
            "okx_code": "",
            "okx_msg": "",
            "request_path": "/api/v5/account/balance",
            "timestamp_used": "",
            "demo_header_used": creds.demo,
            "base_url": base_url,
            "base_url_default": _DEFAULT_OKX_BASE,
            "base_url_overridden": env_override,
            "likely_causes": [],
            "next_steps": [],
        }
        if not creds.present:
            report["likely_causes"].append(
                "OKX_API_KEY, OKX_API_SECRET, or OKX_API_PASSPHRASE is empty."
            )
            report["next_steps"].append(
                "Populate all three credentials in .env and restart the app."
            )
            return report
        try:
            data, meta = await self._request(
                "GET", "/api/v5/account/balance", return_meta=True
            )
            report.update({
                "private_auth_ok": True,
                "http_status": meta.get("http_status", 200),
                "okx_code": meta.get("okx_code", "0"),
                "okx_msg": meta.get("okx_msg", ""),
                "request_path": meta.get("request_path", report["request_path"]),
                "timestamp_used": meta.get("timestamp_used", ""),
                "demo_header_used": meta.get("demo_header_used", creds.demo),
                "base_url": meta.get("base_url", report.get("base_url", "")),
            })
            # Light summary of the balance row so the UI can confirm it.
            row = (data.get("data") or [{}])[0] if isinstance(data, dict) else {}
            try:
                report["total_eq_usd"] = float(row.get("totalEq") or 0)
            except Exception:
                report["total_eq_usd"] = 0.0
            report["asset_row_count"] = len(row.get("details") or [])
        except OKXAuthError as e:
            meta = getattr(e, "meta", {}) or {}
            report.update({
                "private_auth_ok": False,
                "http_status": e.http_status,
                "okx_code": e.okx_code,
                "okx_msg": e.okx_msg,
                "request_path": meta.get("request_path", report["request_path"]),
                "timestamp_used": meta.get("timestamp_used", ""),
                "demo_header_used": meta.get("demo_header_used", creds.demo),
                "base_url": meta.get("base_url", report.get("base_url", "")),
            })
            hints = _classify_okx_error(
                http_status=e.http_status,
                okx_code=e.okx_code,
                okx_msg=e.okx_msg,
                demo_header_used=report["demo_header_used"],
                base_url=report.get("base_url", ""),
            )
            report["likely_causes"].extend(hints["likely_causes"])
            report["next_steps"].extend(hints["next_steps"])
        except Exception as e:
            report["private_auth_ok"] = False
            report["okx_msg"] = f"{type(e).__name__}: {e}"
            report["likely_causes"].append(
                f"Network or transport error — could not reach OKX at {report.get('base_url') or okx_base_url()}. Check egress connectivity (DNS, firewall, geo-block)."
            )
            report["next_steps"].append(
                f"Curl {report.get('base_url') or okx_base_url()}/api/v5/public/time from the same host; if that fails it's a network issue, not credentials. If the host itself is unreachable, try OKX_BASE_URL=https://us.okx.com or https://app.okx.com."
            )
        if report["clock_skew_warning"]:
            report["likely_causes"].insert(
                0,
                f"Local clock is {skew_ms} ms off from OKX server time — fix NTP before retrying.",
            )
        return report

    async def check_readiness(self, desired_mode: str) -> dict:
        """Check auth and mode gates without placing orders."""
        creds = self.credentials()
        live_unlock = os.getenv("LIVE_TRADING_ENABLED", "false").strip().lower() == "true"
        live_ack = os.getenv("LIVE_TRADING_ACK", "").strip()
        mode = "okx_demo" if creds.demo else "okx_live"
        status = {
            "configured": creds.present,
            "authenticated": False,
            "mode": mode if creds.present else "paper",
            "desired_mode": desired_mode,
            "demo": creds.demo,
            "live_unlock_env": live_unlock,
            "live_ack_ok": live_ack == "I_ACCEPT_REAL_MONEY_RISK",
            "can_demo_trade": False,
            "can_live_trade": False,
            "reason": "",
            "balance": {},
        }
        if not creds.present:
            status["reason"] = "OKX_API_KEY, OKX_API_SECRET, or OKX_API_PASSPHRASE missing"
            self._last_status = status
            return status
        try:
            snap = await self.get_account_snapshot()
            status["authenticated"] = bool(snap.get("authenticated"))
            status["balance"] = {
                "usdt_available": snap.get("usdt_available", 0.0),
                "usdt_total": snap.get("usdt_total", 0.0),
                "raw_ccy_count": len(snap.get("assets", [])),
            }
            status["can_demo_trade"] = creds.demo and desired_mode == "okx_demo"
            status["can_live_trade"] = (
                (not creds.demo)
                and desired_mode == "okx_live"
                and live_unlock
                and live_ack == "I_ACCEPT_REAL_MONEY_RISK"
            )
            if desired_mode == "okx_live" and not status["can_live_trade"]:
                status["reason"] = "Live mode requires OKX_DEMO=false, LIVE_TRADING_ENABLED=true, and LIVE_TRADING_ACK=I_ACCEPT_REAL_MONEY_RISK"
            elif desired_mode == "okx_demo" and not status["can_demo_trade"]:
                status["reason"] = "Demo mode requires OKX_DEMO=true"
            else:
                status["reason"] = "ready" if status["authenticated"] else (snap.get("last_error") or "auth failed")
        except Exception as e:
            status["reason"] = f"{type(e).__name__}: {e}"
        self._last_status = status
        return status

    # ---------------- spot orders ----------------

    async def market_buy_spot(self, symbol: str, quote_usdt: float, client_order_id: str = "") -> dict:
        """Place a spot market buy using quote currency amount."""
        payload = {
            "instId": _okx_inst(symbol),
            "tdMode": "cash",
            "side": "buy",
            "ordType": "market",
            "sz": f"{max(0.0, quote_usdt):.6f}",
            "tgtCcy": "quote_ccy",
        }
        if client_order_id:
            payload["clOrdId"] = client_order_id[:32]
        return await self._request("POST", "/api/v5/trade/order", payload)

    async def market_sell_spot(self, symbol: str, base_qty: float, client_order_id: str = "") -> dict:
        """Place a spot market sell using base quantity."""
        payload = {
            "instId": _okx_inst(symbol),
            "tdMode": "cash",
            "side": "sell",
            "ordType": "market",
            "sz": f"{max(0.0, base_qty):.8f}",
        }
        if client_order_id:
            payload["clOrdId"] = client_order_id[:32]
        return await self._request("POST", "/api/v5/trade/order", payload)

    # ---------------- post-trade reconciliation ----------------

    async def fetch_order(self, symbol: str, ord_id: str = "", cl_ord_id: str = "") -> dict:
        """Look up an order by exchange ID or client order ID."""
        if not ord_id and not cl_ord_id:
            raise RuntimeError("fetch_order needs ordId or clOrdId")
        params: Dict[str, Any] = {"instId": _okx_inst(symbol)}
        if ord_id:
            params["ordId"] = ord_id
        if cl_ord_id:
            params["clOrdId"] = cl_ord_id
        return await self._request("GET", "/api/v5/trade/order", params)

    async def fetch_fills(self, symbol: str, ord_id: str = "", cl_ord_id: str = "") -> List[dict]:
        """Return fill rows for a given order id (best-effort)."""
        params: Dict[str, Any] = {"instType": "SPOT", "instId": _okx_inst(symbol), "limit": "20"}
        if ord_id:
            params["ordId"] = ord_id
        try:
            data = await self._request("GET", "/api/v5/trade/fills", params)
            rows = data.get("data", []) or []
            if cl_ord_id and not ord_id:
                rows = [r for r in rows if r.get("clOrdId") == cl_ord_id]
            return rows
        except Exception:
            return []

    async def summarize_fills(self, symbol: str, ord_id: str = "", cl_ord_id: str = "") -> dict:
        """Aggregate a list of fills into (qty, avg_px, fee_usdt)."""
        rows = await self.fetch_fills(symbol, ord_id=ord_id, cl_ord_id=cl_ord_id)
        # Try canonical /trade/order endpoint first as it has fillSz/avgPx.
        if ord_id or cl_ord_id:
            try:
                o = await self.fetch_order(symbol, ord_id=ord_id, cl_ord_id=cl_ord_id)
                row = (o.get("data") or [{}])[0]
                fill_sz = float(row.get("accFillSz") or row.get("fillSz") or 0)
                avg_px = float(row.get("avgPx") or 0)
                fee = abs(float(row.get("fee") or 0))
                fee_ccy = (row.get("feeCcy") or "").upper()
                if fill_sz > 0 and avg_px > 0:
                    return {
                        "filled_qty": fill_sz,
                        "avg_px": avg_px,
                        "fee": fee,
                        "fee_ccy": fee_ccy,
                        "source": "trade.order",
                        "raw_rows": rows,
                        "order_row": row,
                    }
            except Exception:
                pass
        # Fall back to aggregating /trade/fills.
        total_qty = 0.0
        total_quote = 0.0
        total_fee = 0.0
        fee_ccy = ""
        for r in rows:
            try:
                sz = float(r.get("fillSz") or 0)
                px = float(r.get("fillPx") or 0)
            except Exception:
                continue
            total_qty += sz
            total_quote += sz * px
            try:
                total_fee += abs(float(r.get("fee") or 0))
            except Exception:
                pass
            fee_ccy = (r.get("feeCcy") or fee_ccy or "").upper()
        if total_qty <= 0:
            return {
                "filled_qty": 0.0,
                "avg_px": 0.0,
                "fee": 0.0,
                "fee_ccy": "",
                "source": "fills_empty",
                "raw_rows": rows,
            }
        return {
            "filled_qty": total_qty,
            "avg_px": (total_quote / total_qty) if total_qty else 0.0,
            "fee": total_fee,
            "fee_ccy": fee_ccy,
            "source": "fills_aggregate",
            "raw_rows": rows,
        }

    # ---------------- V9: native exchange algo orders (spot) ----------------
    #
    # OKX V5 supports attaching protective sells to a spot LONG position
    # via the algo-order endpoints:
    #
    #   POST /api/v5/trade/order-algo  (place oco / conditional)
    #   POST /api/v5/trade/cancel-algos (cancel)
    #   GET  /api/v5/trade/orders-algo-pending  (look up pending)
    #   GET  /api/v5/trade/orders-algo-history  (look up settled/failed)
    #
    # Every helper returns a normalised dict so the live tester does not
    # have to parse OKX's nested ``data[0]`` shape. ``ok=True`` requires
    # both transport success and OKX's per-row ``sCode=="0"``.
    #
    # ``sz`` for spot OCO sells is in the **base** currency (e.g. DOGE
    # qty, not USDT). ``tpOrdPx="-1"`` and ``slOrdPx="-1"`` request a
    # market-on-trigger sell. ``tdMode="cash"`` is mandatory for spot;
    # ``posSide`` / ``reduceOnly`` / ``mgnMode`` are *not* sent.
    #
    # Demo trading uses the same REST path with ``x-simulated-trading: 1``
    # (already added by ``_headers`` when ``OKX_DEMO=true``).

    def _normalise_algo_response(self, resp: dict) -> dict:
        """Pull the structured fields the live tester cares about.

        The order-algo endpoint returns ``{code,msg,data:[{algoId,clOrdId,sCode,sMsg,tag}]}``.
        We surface the per-row ``sCode``/``sMsg`` because OKX returns
        ``code:0`` at the envelope level even when the algo placement
        was rejected (``sCode != "0"``).
        """
        row = (resp.get("data") or [{}])[0] if isinstance(resp, dict) else {}
        s_code = str(row.get("sCode") or "")
        s_msg = str(row.get("sMsg") or "")
        algo_id = str(row.get("algoId") or "")
        cl_ord_id = str(row.get("clOrdId") or row.get("algoClOrdId") or "")
        envelope_code = str(resp.get("code") or "") if isinstance(resp, dict) else ""
        envelope_msg = str(resp.get("msg") or "") if isinstance(resp, dict) else ""
        ok = (envelope_code in ("", "0")) and (s_code in ("", "0")) and bool(algo_id)
        return {
            "ok": ok,
            "algo_id": algo_id,
            "cl_ord_id": cl_ord_id,
            "s_code": s_code or envelope_code,
            "s_msg": s_msg or envelope_msg,
            "raw": resp,
        }

    async def place_algo_oco_spot_sell(
        self,
        symbol: str,
        base_qty: float,
        tp_trigger_px: float,
        sl_trigger_px: float,
        client_algo_id: str = "",
        tag: str = "",
    ) -> dict:
        """Attach a single OCO sell with TP + SL triggers to a spot LONG.

        Both triggers fire a market sell (``tpOrdPx=-1`` / ``slOrdPx=-1``).
        ``base_qty`` is in the base currency (the coin you bought).
        ``client_algo_id`` is forwarded as ``clOrdId`` for idempotency on
        retry/restart — OKX requires ≤32 alphanumeric chars.
        """
        if base_qty <= 0:
            raise RuntimeError("place_algo_oco_spot_sell: base_qty must be > 0")
        if tp_trigger_px <= 0 or sl_trigger_px <= 0:
            raise RuntimeError("place_algo_oco_spot_sell: triggers must be > 0")
        payload: Dict[str, Any] = {
            "instId": _okx_inst(symbol),
            "tdMode": "cash",
            "side": "sell",
            "ordType": "oco",
            "sz": f"{base_qty:.8f}",
            "tpTriggerPx": f"{tp_trigger_px:.8f}",
            "tpOrdPx": "-1",
            "tpTriggerPxType": "last",
            "slTriggerPx": f"{sl_trigger_px:.8f}",
            "slOrdPx": "-1",
            "slTriggerPxType": "last",
        }
        if client_algo_id:
            payload["clOrdId"] = client_algo_id[:32]
        if tag:
            payload["tag"] = tag[:16]
        try:
            resp = await self._request("POST", "/api/v5/trade/order-algo", payload)
        except OKXAuthError as e:
            return {
                "ok": False,
                "algo_id": "",
                "cl_ord_id": client_algo_id,
                "s_code": str(getattr(e, "okx_code", "") or ""),
                "s_msg": str(getattr(e, "okx_msg", "") or str(e)),
                "raw": getattr(e, "body", {}) or {},
            }
        except Exception as e:
            return {
                "ok": False,
                "algo_id": "",
                "cl_ord_id": client_algo_id,
                "s_code": "transport",
                "s_msg": f"{type(e).__name__}: {e}",
                "raw": {},
            }
        return self._normalise_algo_response(resp)

    async def place_algo_conditional_spot_sell(
        self,
        symbol: str,
        base_qty: float,
        trigger_px: float,
        kind: str = "sl",
        client_algo_id: str = "",
        tag: str = "",
    ) -> dict:
        """Place a single conditional algo sell (one trigger).

        Used in ``conditional`` mode where TP and SL are two separate
        algo orders rather than one OCO. ``kind`` is informational only;
        OKX itself only sees one trigger price.
        """
        if base_qty <= 0:
            raise RuntimeError("place_algo_conditional_spot_sell: base_qty must be > 0")
        if trigger_px <= 0:
            raise RuntimeError("place_algo_conditional_spot_sell: trigger must be > 0")
        payload: Dict[str, Any] = {
            "instId": _okx_inst(symbol),
            "tdMode": "cash",
            "side": "sell",
            "ordType": "conditional",
            "sz": f"{base_qty:.8f}",
            "triggerPx": f"{trigger_px:.8f}",
            "orderPx": "-1",
            "triggerPxType": "last",
        }
        if client_algo_id:
            payload["clOrdId"] = client_algo_id[:32]
        if tag:
            payload["tag"] = tag[:16]
        try:
            resp = await self._request("POST", "/api/v5/trade/order-algo", payload)
        except OKXAuthError as e:
            return {
                "ok": False,
                "algo_id": "",
                "cl_ord_id": client_algo_id,
                "s_code": str(getattr(e, "okx_code", "") or ""),
                "s_msg": str(getattr(e, "okx_msg", "") or str(e)),
                "raw": getattr(e, "body", {}) or {},
            }
        except Exception as e:
            return {
                "ok": False,
                "algo_id": "",
                "cl_ord_id": client_algo_id,
                "s_code": "transport",
                "s_msg": f"{type(e).__name__}: {e}",
                "raw": {},
            }
        out = self._normalise_algo_response(resp)
        out["kind"] = kind
        return out

    async def query_algo(self, algo_id: str = "", cl_ord_id: str = "", ord_type: str = "") -> dict:
        """Look up an algo order — try pending first, then history.

        Returns ``{state, algo_id, cl_ord_id, raw}`` where ``state`` is
        one of: ``live`` | ``effective`` | ``cancelled`` | ``order_failed``
        | ``partially_effective`` | ``not_found``.
        """
        if not (algo_id or cl_ord_id):
            return {"state": "not_found", "algo_id": "", "cl_ord_id": "", "raw": {}}
        params: Dict[str, Any] = {}
        if algo_id:
            params["algoId"] = algo_id
        if cl_ord_id:
            params["algoClOrdId"] = cl_ord_id
        if ord_type:
            params["ordType"] = ord_type
        # Pending first.
        for path in ("/api/v5/trade/orders-algo-pending", "/api/v5/trade/orders-algo-history"):
            try:
                resp = await self._request("GET", path, params)
            except Exception:
                continue
            rows = (resp or {}).get("data") or []
            if rows:
                row = rows[0]
                state = str(row.get("state") or "").lower() or "live"
                return {
                    "state": state,
                    "algo_id": str(row.get("algoId") or algo_id or ""),
                    "cl_ord_id": str(row.get("algoClOrdId") or cl_ord_id or ""),
                    "raw": row,
                }
        return {"state": "not_found", "algo_id": algo_id, "cl_ord_id": cl_ord_id, "raw": {}}

    async def cancel_algo(self, symbol: str, algo_id: str) -> dict:
        """Cancel one algo order. Idempotent: already-cancelled is OK."""
        if not algo_id:
            return {"ok": True, "already": True, "s_code": "", "s_msg": "empty algo_id"}
        payload = [{"instId": _okx_inst(symbol), "algoId": algo_id}]
        try:
            resp = await self._request("POST", "/api/v5/trade/cancel-algos", payload)
        except OKXAuthError as e:
            s_code = str(getattr(e, "okx_code", "") or "")
            # 51400-class: algo already cancelled / effective / not found.
            if s_code.startswith("514") or s_code.startswith("515"):
                return {"ok": True, "already": True, "s_code": s_code, "s_msg": str(getattr(e, "okx_msg", "") or "")}
            return {"ok": False, "s_code": s_code, "s_msg": str(getattr(e, "okx_msg", "") or str(e))}
        except Exception as e:
            return {"ok": False, "s_code": "transport", "s_msg": f"{type(e).__name__}: {e}"}
        row = (resp.get("data") or [{}])[0]
        s_code = str(row.get("sCode") or "")
        s_msg = str(row.get("sMsg") or "")
        ok = s_code in ("", "0") or s_code.startswith("514") or s_code.startswith("515")
        return {
            "ok": ok,
            "already": s_code.startswith("514") or s_code.startswith("515"),
            "s_code": s_code,
            "s_msg": s_msg,
            "raw": resp,
        }

    # ---------------- V9: open-orders / algo discovery ----------------
    #
    # Used by the live tester reconciler to detect pre-existing protective
    # sells (placed manually on the OKX app, by a previous bot run, or by
    # any other tool) before submitting a new algo. This prevents the
    # duplicate-sell footgun reported on the V8 screenshot where DOGE was
    # frozen by an unrelated order despite the UI claiming no native
    # protection was active.

    async def list_open_orders_for_inst(self, symbol: str, side: str = "sell") -> List[dict]:
        """Return raw open *regular* orders for an instrument.

        Filters by ``side`` client-side (OKX supports ``side`` server-side
        too but we keep the request narrow to spot only). Never raises.
        """
        params = {"instType": "SPOT", "instId": _okx_inst(symbol)}
        try:
            data = await self._request("GET", "/api/v5/trade/orders-pending", params)
        except Exception:
            return []
        rows = (data or {}).get("data") or []
        if side:
            rows = [r for r in rows if (r.get("side") or "").lower() == side.lower()]
        return rows

    async def list_open_algos_for_inst(self, symbol: str, side: str = "sell") -> List[dict]:
        """Return raw pending algo orders (oco + conditional + trigger) for an instrument.

        OKX orders-algo-pending requires an ``ordType`` filter, so we
        sweep the three spot-relevant types and concatenate. Never raises.
        """
        rows: List[dict] = []
        for ord_type in ("oco", "conditional", "trigger"):
            try:
                data = await self._request(
                    "GET", "/api/v5/trade/orders-algo-pending",
                    {"ordType": ord_type, "instId": _okx_inst(symbol)},
                )
            except Exception:
                continue
            for r in (data or {}).get("data") or []:
                if not side or (r.get("side") or "").lower() == side.lower():
                    rows.append({**r, "_ordType": ord_type})
        return rows

    async def fetch_instrument(self, symbol: str, *, force: bool = False) -> dict:
        """Return the OKX SPOT instrument descriptor for ``symbol``.

        Shape:
            {
              "instId": "DOGE-USDT",
              "baseCcy": "DOGE",
              "quoteCcy": "USDT",
              "lotSz": 1.0,        # min base step
              "minSz": 1.0,        # min base size for a single order
              "tickSz": 0.00001,   # min quote price tick
              "ts": 1234,          # unix seconds cached
              "ok": True/False,    # False on a transport/parse error
            }

        Cached forever per-process (instrument specs change rarely). On
        any failure returns a safe-default descriptor with ``ok=False``
        and ``lotSz=0`` so callers can decide whether to fall back to a
        naive sell (we won't — the live tester treats ok=False as
        "abort exit and surface error").

        Never raises. Uses the unsigned public endpoint.
        """
        inst = _okx_inst(symbol)
        if not force:
            cached = self._inst_cache.get(inst)
            if cached and cached.get("ok"):
                return dict(cached)
        try:
            url = okx_base_url() + "/api/v5/public/instruments"
            r = await self._client.get(url, params={"instType": "SPOT", "instId": inst})
            data = r.json() or {}
        except Exception as e:
            stub = {
                "instId": inst, "baseCcy": "", "quoteCcy": "",
                "lotSz": 0.0, "minSz": 0.0, "tickSz": 0.0,
                "ts": int(time.time()), "ok": False,
                "error": f"{type(e).__name__}: {e}",
            }
            self._inst_cache[inst] = stub
            return dict(stub)
        rows = data.get("data") or []
        if not rows:
            stub = {
                "instId": inst, "baseCcy": "", "quoteCcy": "",
                "lotSz": 0.0, "minSz": 0.0, "tickSz": 0.0,
                "ts": int(time.time()), "ok": False,
                "error": f"empty rows; envelope={str(data)[:120]}",
            }
            self._inst_cache[inst] = stub
            return dict(stub)
        row = rows[0]
        try:
            descriptor = {
                "instId": str(row.get("instId") or inst),
                "baseCcy": str(row.get("baseCcy") or "").upper(),
                "quoteCcy": str(row.get("quoteCcy") or "").upper(),
                "lotSz": float(row.get("lotSz") or 0),
                "minSz": float(row.get("minSz") or 0),
                "tickSz": float(row.get("tickSz") or 0),
                "ts": int(time.time()),
                "ok": True,
            }
        except Exception as e:
            descriptor = {
                "instId": inst, "baseCcy": "", "quoteCcy": "",
                "lotSz": 0.0, "minSz": 0.0, "tickSz": 0.0,
                "ts": int(time.time()), "ok": False,
                "error": f"parse {type(e).__name__}: {e}",
            }
        self._inst_cache[inst] = descriptor
        return dict(descriptor)

    async def inst_inventory_snapshot(self, symbol: str, side: str = "sell") -> dict:
        """Compose free/frozen balance + open regular + open algo snapshot.

        Returns a dict suitable for the live tester journal/UI:
            {
              base_ccy, total, free, frozen,
              open_sells:   [{ordId,sz,px,ordType,clOrdId}, ...],
              open_algos:   [{algoId,ordType,sz,tpTriggerPx,slTriggerPx,triggerPx,state,clOrdId}, ...],
              ts,
            }
        Never raises — all errors degrade to empty lists / zeros.
        """
        base = symbol.split("/")[0].upper() if "/" in symbol else symbol.upper()
        try:
            total, free = await self.spot_base_balance(base)
        except Exception:
            total, free = 0.0, 0.0
        frozen = max(0.0, float(total) - float(free))
        try:
            open_sells_raw = await self.list_open_orders_for_inst(symbol, side=side)
        except Exception:
            open_sells_raw = []
        try:
            open_algos_raw = await self.list_open_algos_for_inst(symbol, side=side)
        except Exception:
            open_algos_raw = []
        open_sells = [
            {
                "ordId": str(r.get("ordId") or ""),
                "clOrdId": str(r.get("clOrdId") or ""),
                "sz": str(r.get("sz") or ""),
                "px": str(r.get("px") or ""),
                "ordType": str(r.get("ordType") or ""),
            }
            for r in open_sells_raw
        ]
        open_algos = [
            {
                "algoId": str(r.get("algoId") or ""),
                "clOrdId": str(r.get("algoClOrdId") or r.get("clOrdId") or ""),
                "ordType": str(r.get("_ordType") or r.get("ordType") or ""),
                "sz": str(r.get("sz") or ""),
                "tpTriggerPx": str(r.get("tpTriggerPx") or ""),
                "slTriggerPx": str(r.get("slTriggerPx") or ""),
                "triggerPx": str(r.get("triggerPx") or ""),
                "state": str(r.get("state") or ""),
                "tag": str(r.get("tag") or ""),
            }
            for r in open_algos_raw
        ]
        return {
            "base_ccy": base,
            "total": float(total),
            "free": float(free),
            "frozen": float(frozen),
            "open_sells": open_sells,
            "open_algos": open_algos,
            "ts": int(time.time()),
        }

    # ---------------- duplicate-symbol guard ----------------

    async def spot_base_balance(self, base_ccy: str) -> Tuple[float, float]:
        """Return (total, available) balance for a base currency such as BTC."""
        try:
            data = await self.fetch_balance_raw()
            details = (data.get("data") or [{}])[0].get("details", [])
            row = next((d for d in details if (d.get("ccy") or "").upper() == base_ccy.upper()), {})
            total = float(row.get("eq") or row.get("cashBal") or 0)
            avail = float(row.get("availBal") or row.get("availEq") or 0)
            return total, avail
        except Exception:
            return 0.0, 0.0


# ---------------------------------------------------------------------------
# V9.1 — lot-size rounding + sell-qty clamp helpers (pure functions)
# ---------------------------------------------------------------------------

def round_down_to_lot(qty: float, lot: float) -> float:
    """Round ``qty`` *down* to the nearest multiple of ``lot``.

    Used to coerce a desired sell quantity to a step OKX accepts. When
    ``lot`` is zero or negative we fall back to a 1e-8 grid which is
    OKX's universal upper bound for SPOT precision — callers should
    treat that path as "no lot info, use as-is" because the OKX-side
    lot for the instrument is unknown.
    """
    try:
        q = float(qty)
        L = float(lot)
    except Exception:
        return 0.0
    if q <= 0:
        return 0.0
    if L <= 0:
        # No lot info; truncate to 8 decimals so we don't send 17-digit floats.
        return float(f"{q:.8f}")
    # Use integer math to avoid float drift (q / L can be 0.999999…).
    # Multiply by a large factor based on lot precision, then floor.
    # Number of decimals implied by lot:
    s = f"{L:.12f}".rstrip("0")
    decimals = 0 if "." not in s else max(0, len(s.split(".")[1]))
    factor = 10 ** decimals
    # Floor(q / L) * L without floating-point edge cases:
    units = int((q * factor) // (L * factor)) if (L * factor) >= 1 else int(q // L)
    out = units * L
    # Re-quantize to ``decimals`` to drop any trailing 1e-17 noise.
    if decimals:
        out = float(f"{out:.{decimals}f}")
    return max(0.0, out)


def clamp_sell_qty(
    journal_qty: float,
    okx_free: float,
    *,
    lot: float = 0.0,
    min_sz: float = 0.0,
) -> dict:
    """Compute the safe live-sell quantity for a spot position.

    Inputs:
      - ``journal_qty``  — the bot's view of the sellable base qty
        (after deducting any base-asset fee).
      - ``okx_free``     — OKX-reported free / available base balance.
      - ``lot``          — OKX SPOT ``lotSz`` (base step). 0 = unknown.
      - ``min_sz``       — OKX SPOT ``minSz`` (min single-order qty).

    Returns:
      {
        "sell_qty":     float,   # what to send to OKX (lot-rounded, clamped)
        "capped_by":    str,     # "journal" | "okx_free" | "none"
        "below_min":    bool,    # rounded qty < min_sz — don't send
        "reason":       str,     # human-readable explanation
      }

    The contract: never returns a sell_qty greater than ``min(journal, free)``.
    If the clamped result rounds to zero or is below ``min_sz``, returns
    ``sell_qty=0`` and ``below_min=True`` so the caller can mark the
    position for manual review instead of spamming rejected sells.
    """
    j = max(0.0, float(journal_qty or 0))
    f = max(0.0, float(okx_free or 0))
    raw = min(j, f)
    capped_by = "none"
    if f < j:
        capped_by = "okx_free"
    elif j < f:
        capped_by = "journal"
    rounded = round_down_to_lot(raw, lot) if lot > 0 else float(f"{raw:.8f}")
    below_min = (min_sz > 0 and rounded < float(min_sz)) or rounded <= 0
    if below_min:
        reason = (
            f"clamped qty {rounded:.8f} below minSz {min_sz:.8f} "
            f"(journal={j:.8f}, free={f:.8f}, lot={lot})"
        )
        return {
            "sell_qty": 0.0,
            "capped_by": capped_by,
            "below_min": True,
            "reason": reason,
            "raw": raw,
            "rounded": rounded,
        }
    return {
        "sell_qty": rounded,
        "capped_by": capped_by,
        "below_min": False,
        "reason": (
            f"sell {rounded:.8f} (journal={j:.8f}, free={f:.8f}, "
            f"lot={lot}, capped_by={capped_by})"
        ),
        "raw": raw,
        "rounded": rounded,
    }


okx_private = OKXPrivateAdapter()
