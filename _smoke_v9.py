"""V9 mocked smoke test \u2014 NO REAL HTTP.

Validates that the new OKX algo helpers and the live tester wiring
produce the exact payload shape OKX expects, without sending any
network request and without touching any user credentials.

Run with:  python _smoke_v9.py

Exit code 0 = all assertions passed.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple


# Make sure we never reach OKX. We will monkey-patch ``_request`` and
# also nuke the env so the credential gate refuses to send anything if
# the patch is ever bypassed.
for k in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"):
    os.environ.pop(k, None)

# Force a per-run data dir so the live state file does not collide
# with a real install.
TMP_DATA = os.path.abspath(os.path.join(os.path.dirname(__file__), "data", "smoke_v9"))
os.makedirs(TMP_DATA, exist_ok=True)
os.environ["DATA_DIR"] = TMP_DATA

# Import after env tweaks so settings.load() sees the empty creds.
from app.services.okx_private import OKXPrivateAdapter, OKXAuthError, round_down_to_lot, clamp_sell_qty  # noqa: E402
from app.services import live_tester as lt_mod  # noqa: E402
from app.services import okx_private as okx_private_mod  # noqa: E402


# ---------------------------------------------------------------------------
# Test 1 \u2014 OCO payload shape
# ---------------------------------------------------------------------------

class _CapturedRequest:
    """Records every (method, path, payload) call instead of sending it."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, Any]] = []
        self.next_response: Dict[str, Any] = {
            "code": "0", "msg": "",
            "data": [{"algoId": "ALG12345", "clOrdId": "v9pdeadbeef",
                      "sCode": "0", "sMsg": "", "tag": "v9tester"}],
        }

    async def __call__(self, method: str, path: str, payload=None, *, return_meta: bool = False):
        self.calls.append((method, path, payload))
        return self.next_response


def _reset_state_file() -> None:
    """Remove any persisted live_state.json so each test starts clean."""
    try:
        if lt_mod.LIVE_STATE_FILE.exists():
            lt_mod.LIVE_STATE_FILE.unlink()
    except Exception:
        pass


def _patched_adapter() -> Tuple[OKXPrivateAdapter, _CapturedRequest]:
    adapter = OKXPrivateAdapter()
    cap = _CapturedRequest()
    # Bypass the credential gate by replacing _request directly. The
    # signing path is therefore never reached \u2014 no secrets, no HTTP.
    adapter._request = cap  # type: ignore[assignment]
    return adapter, cap


async def test_oco_payload() -> None:
    adapter, cap = _patched_adapter()
    res = await adapter.place_algo_oco_spot_sell(
        symbol="DOGE/USDT", base_qty=100.0,
        tp_trigger_px=0.20, sl_trigger_px=0.18,
        client_algo_id="v9pdeadbeef", tag="v9tester",
    )
    assert res["ok"] is True, res
    assert res["algo_id"] == "ALG12345", res
    assert len(cap.calls) == 1
    method, path, payload = cap.calls[0]
    assert method == "POST", method
    assert path == "/api/v5/trade/order-algo", path
    assert payload["instId"] == "DOGE-USDT", payload
    assert payload["tdMode"] == "cash", payload
    assert payload["side"] == "sell", payload
    assert payload["ordType"] == "oco", payload
    assert payload["tpOrdPx"] == "-1", payload
    assert payload["slOrdPx"] == "-1", payload
    assert payload["tpTriggerPxType"] == "last", payload
    assert payload["slTriggerPxType"] == "last", payload
    assert "posSide" not in payload, payload
    assert "mgnMode" not in payload, payload
    assert payload["clOrdId"] == "v9pdeadbeef", payload
    print("[ok] OCO payload shape")


# ---------------------------------------------------------------------------
# Test 2 \u2014 Conditional payload shape
# ---------------------------------------------------------------------------

async def test_conditional_payload() -> None:
    adapter, cap = _patched_adapter()
    cap.next_response = {"code": "0", "data": [{
        "algoId": "CONDTP1", "clOrdId": "v9tpdeadbeef",
        "sCode": "0", "sMsg": "",
    }]}
    res = await adapter.place_algo_conditional_spot_sell(
        symbol="DOGE/USDT", base_qty=50.0, trigger_px=0.21,
        kind="tp", client_algo_id="v9tpdeadbeef",
    )
    assert res["ok"] is True, res
    assert res["kind"] == "tp", res
    method, path, payload = cap.calls[0]
    assert path == "/api/v5/trade/order-algo", path
    assert payload["ordType"] == "conditional", payload
    assert payload["orderPx"] == "-1", payload
    assert payload["triggerPx"].startswith("0.21"), payload
    assert "tpTriggerPx" not in payload, payload
    print("[ok] conditional payload shape")


# ---------------------------------------------------------------------------
# Test 3 \u2014 Cancel-algos payload shape
# ---------------------------------------------------------------------------

async def test_cancel_payload() -> None:
    adapter, cap = _patched_adapter()
    cap.next_response = {"code": "0", "data": [{"sCode": "0", "sMsg": "", "algoId": "ALG12345"}]}
    res = await adapter.cancel_algo("DOGE/USDT", "ALG12345")
    assert res["ok"] is True, res
    method, path, payload = cap.calls[0]
    assert method == "POST", method
    assert path == "/api/v5/trade/cancel-algos", path
    assert isinstance(payload, list), payload
    assert payload[0]["instId"] == "DOGE-USDT", payload
    assert payload[0]["algoId"] == "ALG12345", payload
    print("[ok] cancel-algos payload shape")


# ---------------------------------------------------------------------------
# Test 4 \u2014 cancel_algo treats 51400/51500 as idempotent success
# ---------------------------------------------------------------------------

async def test_cancel_already_cancelled() -> None:
    adapter, cap = _patched_adapter()
    cap.next_response = {"code": "0", "data": [{"sCode": "51400", "sMsg": "Cancellation failed as the order has been filled, cancelled or doesn't exist."}]}
    res = await adapter.cancel_algo("DOGE/USDT", "STALE")
    assert res["ok"] is True, res
    assert res["already"] is True, res
    print("[ok] cancel already-cancelled is idempotent")


# ---------------------------------------------------------------------------
# Test 5 \u2014 sCode != 0 yields ok=False with verbatim error captured
# ---------------------------------------------------------------------------

async def test_failure_surfaces_sCode() -> None:
    adapter, cap = _patched_adapter()
    cap.next_response = {"code": "0", "data": [{
        "algoId": "", "clOrdId": "",
        "sCode": "51000", "sMsg": "Parameter triggerPxType error",
    }]}
    res = await adapter.place_algo_oco_spot_sell(
        symbol="DOGE/USDT", base_qty=100.0,
        tp_trigger_px=0.20, sl_trigger_px=0.18, client_algo_id="x",
    )
    assert res["ok"] is False, res
    assert res["s_code"] == "51000", res
    assert "triggerPxType" in res["s_msg"], res
    print("[ok] non-zero sCode surfaces as failure")


# ---------------------------------------------------------------------------
# Test 6 \u2014 Live tester DRY_RUN short-circuits before any _request call
# ---------------------------------------------------------------------------

async def test_dry_run_short_circuits() -> None:
    # Build a fresh LiveTester so we own the journal.
    _reset_state_file()
    tester = lt_mod.LiveTester()
    # Inject a synthetic open position so _place_native_protection has
    # something to attach to.
    pos = lt_mod.LivePosition(
        id="lv_smoke12345",
        client_order_id="v6tsmoke", okx_order_id="OKX1",
        symbol="DOGE/USDT", base_ccy="DOGE",
        filled_qty=100.0, avg_entry_price=0.19,
        stop_loss_price=0.18, take_profit_price=0.20,
        status="open",
    )
    tester._state.positions.append(pos)

    # Replace okx_private with a fake that explodes if any call is made.
    class _Boom:
        async def place_algo_oco_spot_sell(self, *a, **k):
            raise AssertionError("dry-run must not reach the OKX adapter")
        async def place_algo_conditional_spot_sell(self, *a, **k):
            raise AssertionError("dry-run must not reach the OKX adapter")
        async def cancel_algo(self, *a, **k):
            return {"ok": True}
        async def query_algo(self, *a, **k):
            return {"state": "live"}
    lt_mod.okx_private = _Boom()  # type: ignore[assignment]

    class _S:
        live_native_protection_enabled = True
        live_native_protection_mode = "oco"
        live_native_protection_dry_run = True

    res = await tester._place_native_protection("lv_smoke12345", settings=_S())
    assert res["status"] == "dry_run", res
    assert res["oco_algo_id"] == "DRY_RUN", res
    print("[ok] DRY_RUN never calls OKX")


# ---------------------------------------------------------------------------
# Test 7 \u2014 Disabled flag yields status=none and never calls OKX
# ---------------------------------------------------------------------------

async def test_disabled_is_none() -> None:
    _reset_state_file()
    tester = lt_mod.LiveTester()
    pos = lt_mod.LivePosition(
        id="lv_disabled01",
        client_order_id="v6tsmoke", okx_order_id="OKX1",
        symbol="DOGE/USDT", base_ccy="DOGE",
        filled_qty=100.0, avg_entry_price=0.19,
        stop_loss_price=0.18, take_profit_price=0.20,
        status="open",
    )
    tester._state.positions.append(pos)
    class _S:
        live_native_protection_enabled = False
        live_native_protection_mode = "oco"
        live_native_protection_dry_run = False
    res = await tester._place_native_protection("lv_disabled01", settings=_S())
    assert res["status"] == "none", res
    assert res["enabled"] is False, res
    print("[ok] disabled flag = status:none, no OKX call")


# ---------------------------------------------------------------------------
# Test 8 \u2014 clOrdId is \u226432 alphanumeric chars and stable
# ---------------------------------------------------------------------------

def test_client_algo_id_format() -> None:
    cid1 = lt_mod.LiveTester._client_algo_id_for("lv_abc123def456")
    cid2 = lt_mod.LiveTester._client_algo_id_for("lv_abc123def456")
    assert cid1 == cid2, (cid1, cid2)            # deterministic
    assert len(cid1) <= 32, cid1                  # OKX limit
    assert cid1.isalnum(), cid1                   # alphanumeric only
    assert cid1.startswith("v9p"), cid1
    tp_id = lt_mod.LiveTester._client_algo_id_for("lv_abc", suffix="tp")
    sl_id = lt_mod.LiveTester._client_algo_id_for("lv_abc", suffix="sl")
    assert tp_id.startswith("v9tp") and sl_id.startswith("v9sl"), (tp_id, sl_id)
    print("[ok] clOrdId format is stable and OKX-compliant")


# ---------------------------------------------------------------------------
# Test 9 \u2014 summary() exposes V9 native fields
# ---------------------------------------------------------------------------

def _settings_stub(**over):
    base = dict(
        live_tester_enabled=False,
        live_max_order_usdt_tester=5.0,
        live_daily_loss_cap_usdt=3.0,
        live_max_trades_per_day=3,
        live_one_position_per_symbol=True,
        live_max_open_positions=1,
        live_require_protective_exit=True,
        live_spot_only=True,
        live_native_protection_enabled=True,
        live_native_protection_mode="oco",
        live_native_protection_dry_run=False,
        live_native_protection_reconcile_on_startup=False,
        live_unattended_mode=False,
        live_unattended_max_hours=120.0,
        live_unattended_started_at=0.0,
        live_unattended_stop_new_on_failure=True,
        live_tester_override=False,
    )
    base.update(over)
    class _S:
        pass
    s = _S()
    for k, v in base.items():
        setattr(s, k, v)
    return s


def test_summary_exposes_native_fields() -> None:
    _reset_state_file()
    tester = lt_mod.LiveTester()
    s = tester.summary(_settings_stub())  # type: ignore[arg-type]
    assert s["native_protection_enabled"] is True, s
    assert s["native_protection_mode"] == "oco", s
    assert s["native_protection_dry_run"] is False, s
    assert "native_protection_warning" in s, s
    assert s["stop_mode"] == "exchange_native + bot_fallback", s["stop_mode"]
    assert "watchdog" in s and "unattended" in s and "unattended_readiness" in s, list(s.keys())
    assert s["unattended_readiness"]["banner"] == "DO NOT LEAVE UNATTENDED", s["unattended_readiness"]["banner"]
    print("[ok] summary() exposes native + watchdog + unattended fields, dynamic stop_mode")


# ---------------------------------------------------------------------------
# Test 10 — inventory snapshot payload shape (OKX private adapter)
# ---------------------------------------------------------------------------

async def test_inventory_snapshot_shape() -> None:
    adapter, cap = _patched_adapter()
    # Return different shapes for the three endpoints we call.
    async def fake_request(method, path, payload=None, *, return_meta=False):
        if "orders-pending" in path:
            return {"data": [{"ordId": "O1", "side": "sell", "sz": "50", "px": "0.22", "ordType": "limit", "clOrdId": ""}]}
        if "orders-algo-pending" in path:
            ord_type = (payload or {}).get("ordType") if isinstance(payload, dict) else None
            if ord_type == "oco":
                return {"data": [{
                    "algoId": "A1", "side": "sell", "sz": "40", "ordType": "oco",
                    "tpTriggerPx": "0.25", "slTriggerPx": "0.18",
                    "algoClOrdId": "v9pdeadbeef", "state": "live",
                }]}
            return {"data": []}
        if "balance" in path or "account" in path:
            return {"data": [{"details": [{"ccy": "DOGE", "eq": "100", "availBal": "10"}]}]}
        return {"data": []}
    adapter._request = fake_request  # type: ignore[assignment]
    snap = await adapter.inst_inventory_snapshot("DOGE/USDT")
    assert snap["base_ccy"] == "DOGE", snap
    assert snap["total"] == 100.0 and snap["free"] == 10.0 and snap["frozen"] == 90.0, snap
    assert len(snap["open_sells"]) == 1 and snap["open_sells"][0]["ordId"] == "O1", snap
    assert len(snap["open_algos"]) == 1 and snap["open_algos"][0]["ordType"] == "oco", snap
    assert snap["open_algos"][0]["clOrdId"] == "v9pdeadbeef", snap
    print("[ok] inventory snapshot payload shape")


# ---------------------------------------------------------------------------
# Test 11 — _classify_existing_algos: adopt / skip_duplicate / warn / ok
# ---------------------------------------------------------------------------

def test_classify_existing_algos() -> None:
    _reset_state_file()
    tester = lt_mod.LiveTester()
    pid = "lv_classifyabc"
    expected_cl = lt_mod.LiveTester._client_algo_id_for(pid)
    # adopt by clOrdId match.
    inv = {"open_algos": [{"algoId": "A1", "clOrdId": expected_cl, "ordType": "oco"}], "open_sells": []}
    res = tester._classify_existing_algos(inv, pid, "oco")
    assert res["action"] == "adopt" and res["adopted"]["algoId"] == "A1", res
    # skip duplicate OCO.
    inv = {"open_algos": [{"algoId": "A2", "clOrdId": "other", "ordType": "oco"}], "open_sells": []}
    res = tester._classify_existing_algos(inv, pid, "oco")
    assert res["action"] == "skip_duplicate", res
    # warn_other when unrelated sells exist.
    inv = {"open_algos": [], "open_sells": [{"ordId": "O9"}]}
    res = tester._classify_existing_algos(inv, pid, "oco")
    assert res["action"] == "warn_other" and len(res["warnings"]) >= 1, res
    # ok: clean slate.
    inv = {"open_algos": [], "open_sells": []}
    res = tester._classify_existing_algos(inv, pid, "oco")
    assert res["action"] == "ok", res
    print("[ok] _classify_existing_algos all four branches")


# ---------------------------------------------------------------------------
# Test 12 — fee_ccy capture on entry (entry_fee_ccy populated)
# ---------------------------------------------------------------------------

async def test_fee_ccy_capture() -> None:
    _reset_state_file()
    tester = lt_mod.LiveTester()

    class _OKX:
        async def market_buy_spot(self, *a, **k):
            return {"data": [{"ordId": "ORD1"}]}
        async def summarize_fills(self, *a, **k):
            return {"filled_qty": 52.3, "avg_px": 0.191,
                    "fee": 0.0523, "fee_ccy": "DOGE"}
        async def place_algo_oco_spot_sell(self, *a, **k):
            return {"ok": True, "algo_id": "ALG1", "cl_ord_id": "x"}
        async def cancel_algo(self, *a, **k):
            return {"ok": True}
        async def inst_inventory_snapshot(self, *a, **k):
            return {"base_ccy": "DOGE", "total": 0.0, "free": 0.0, "frozen": 0.0,
                    "open_sells": [], "open_algos": []}
    lt_mod.okx_private = _OKX()  # type: ignore[assignment]

    s = _settings_stub(live_tester_enabled=True, live_native_protection_enabled=False)
    pre = {"allowed": True, "reasons": []}
    res = await tester.attempt_entry(
        settings=s, symbol="DOGE/USDT", live_price=0.19,
        intended_quote_usdt=5.0, sl_price=0.18, take_profit_price=0.20,
        trail_pct=0.0, preflight=pre,
    )
    assert res["status"] == "opened", res
    pos = res["position"]
    assert pos["entry_fee_ccy"] == "DOGE", pos
    assert abs(pos["entry_fee_usdt"] - 0.0523) < 1e-9, pos
    print("[ok] fee_ccy captured from summarize_fills onto position")


# ---------------------------------------------------------------------------
# Test 13 — unattended readiness gate fails by default, passes with all green
# ---------------------------------------------------------------------------

def test_unattended_readiness_default_fails() -> None:
    _reset_state_file()
    tester = lt_mod.LiveTester()
    s = _settings_stub(live_unattended_mode=True)
    r = tester._unattended_readiness(s, tester.open_positions())
    assert r["overall_pass"] is False, r
    assert r["banner"] == "DO NOT LEAVE UNATTENDED", r
    # Native must be enabled, watchdog must be alive.
    ids = [c["id"] for c in r["checks"] if not c["pass"]]
    assert "watchdog_alive" in ids, ids
    assert "market_data_fresh" in ids, ids
    print("[ok] unattended readiness fails by default (no heartbeat / data yet)")


def test_unattended_readiness_can_pass() -> None:
    _reset_state_file()
    tester = lt_mod.LiveTester()
    tester.heartbeat(market_data_fresh=True)
    s = _settings_stub(
        live_tester_enabled=True,
        live_unattended_mode=True,
        live_native_protection_enabled=True,
        live_native_protection_mode="oco",
        live_native_protection_dry_run=False,
        live_max_order_usdt_tester=5.0,
        live_daily_loss_cap_usdt=3.0,
        live_max_open_positions=1,
        live_max_trades_per_day=3,
    )
    r = tester._unattended_readiness(s, [])
    assert r["overall_pass"] is True, r
    assert r["banner"] == "SAFE-ish for unattended tiny test", r
    print("[ok] unattended readiness passes when every check is green")


# ---------------------------------------------------------------------------
# Test 14 — unattended timer expiry latches kill switch
# ---------------------------------------------------------------------------

def test_unattended_expiry_latches_kill_switch() -> None:
    _reset_state_file()
    tester = lt_mod.LiveTester()
    # Force-start unattended 10 hours ago with a 1-hour max.
    import time as _t
    tester._state.unattended_started_at = _t.time() - 36000
    s = _settings_stub(live_unattended_mode=True, live_unattended_max_hours=1.0)
    tester._maybe_expire_unattended(_t.time(), s)
    assert tester._state.unattended_expired is True
    assert tester._state.kill_switch is True
    assert "unattended timer expired" in tester._state.kill_switch_reason
    print("[ok] unattended expiry latches kill switch + persists")


# ---------------------------------------------------------------------------
# Test 15 — startup_reconcile is a no-op when the flag is off
# ---------------------------------------------------------------------------

async def test_startup_reconcile_gated() -> None:
    _reset_state_file()
    tester = lt_mod.LiveTester()
    # Clear any positions from earlier tests so we reconcile exactly one.
    tester._state.positions = []
    pos = lt_mod.LivePosition(
        id="lv_rec00001",
        client_order_id="v6trec", okx_order_id="OKXR",
        symbol="DOGE/USDT", base_ccy="DOGE",
        filled_qty=100.0, avg_entry_price=0.19,
        stop_loss_price=0.18, take_profit_price=0.20,
        status="open",
    )
    tester._state.positions.append(pos)
    s_off = _settings_stub(live_native_protection_reconcile_on_startup=False)
    res = await tester.startup_reconcile(s_off)
    assert res.get("skipped") is True, res
    # When enabled, it iterates open positions; we monkey-patch okx so nothing fires.
    class _OKX:
        async def place_algo_oco_spot_sell(self, *a, **k):
            return {"ok": True, "algo_id": "ALG", "cl_ord_id": "x"}
        async def cancel_algo(self, *a, **k):
            return {"ok": True}
        async def query_algo(self, *a, **k):
            return {"state": "live"}
        async def inst_inventory_snapshot(self, *a, **k):
            return {"base_ccy": "DOGE", "total": 100.0, "free": 100.0, "frozen": 0.0,
                    "open_sells": [], "open_algos": []}
    lt_mod.okx_private = _OKX()  # type: ignore[assignment]
    s_on = _settings_stub(
        live_native_protection_reconcile_on_startup=True,
        live_native_protection_enabled=True,
    )
    res = await tester.startup_reconcile(s_on)
    assert res["ok"] is True and len(res["actions"]) == 1, res
    actions = res["actions"][0]["actions"]
    assert "inventory_refreshed" in actions, actions
    assert "native_placed" in actions, actions
    print("[ok] startup_reconcile gated; attaches native when enabled + position unprotected")


# ---------------------------------------------------------------------------
# V9.1 — sell-qty helper unit tests
# ---------------------------------------------------------------------------

def test_round_down_to_lot() -> None:
    # DOGE on OKX has lotSz = 1; selling 97.41979720 with lot=1 → 97.
    assert round_down_to_lot(97.41979720, 1.0) == 97.0
    assert round_down_to_lot(97.99, 1.0) == 97.0
    assert round_down_to_lot(0.0, 1.0) == 0.0
    # Fractional lot 0.1 — 0.987654 rounds to 0.9.
    assert abs(round_down_to_lot(0.987654, 0.1) - 0.9) < 1e-9
    # Lot 0.0001 — 0.12345 rounds to 0.1234.
    assert abs(round_down_to_lot(0.12345, 0.0001) - 0.1234) < 1e-9
    # lot=0 (unknown) returns 8-decimal truncation, not zero.
    assert round_down_to_lot(97.41979720, 0.0) == 97.41979720
    print("[ok] round_down_to_lot floors qty to OKX lot step")


def test_clamp_sell_qty_below_min_blocks_sell() -> None:
    # Journal says 97, OKX has 96.5 free, lot=1, minSz=1 → sell 96.
    res = clamp_sell_qty(97.0, 96.5, lot=1.0, min_sz=1.0)
    assert res["sell_qty"] == 96.0, res
    assert res["capped_by"] == "okx_free", res
    assert res["below_min"] is False, res
    # Journal 98.1, OKX free 97.42 (fee deducted by exchange), lot=1, minSz=1.
    # Sellable should be floor(min(98.1, 97.42)) = 97.
    res2 = clamp_sell_qty(98.106543, 97.41979720, lot=1.0, min_sz=1.0)
    assert res2["sell_qty"] == 97.0, res2
    assert res2["capped_by"] == "okx_free", res2
    # OKX free 0.5 with minSz 1 → below_min, sell_qty=0.
    res3 = clamp_sell_qty(97.0, 0.5, lot=1.0, min_sz=1.0)
    assert res3["sell_qty"] == 0.0 and res3["below_min"] is True, res3
    # OKX free 0 → below_min, no sell.
    res4 = clamp_sell_qty(97.0, 0.0, lot=1.0, min_sz=1.0)
    assert res4["sell_qty"] == 0.0 and res4["below_min"] is True, res4
    print("[ok] clamp_sell_qty floors to min(journal, free) and blocks below minSz")


# ---------------------------------------------------------------------------
# V9.1 — entry path correctly deducts base-asset fee from sellable_qty
# ---------------------------------------------------------------------------

async def test_entry_deducts_base_fee_from_sellable() -> None:
    _reset_state_file()
    tester = lt_mod.LiveTester()
    # Stub the adapter the same way test_fee_ccy_capture does — the
    # earlier tests already replaced ``lt_mod.okx_private`` with a stub.
    class _OKX:
        async def market_buy_spot(self, *a, **k):
            return {"data": [{"ordId": "OKX1"}]}
        async def summarize_fills(self, *a, **k):
            return {
                "filled_qty": 98.106543, "avg_px": 0.19,
                "fee": 0.0981, "fee_ccy": "DOGE",
            }
        async def place_algo_oco_spot_sell(self, *a, **k):
            return {"ok": True, "algo_id": "ALG1", "cl_ord_id": "x"}
        async def cancel_algo(self, *a, **k):
            return {"ok": True}
        async def inst_inventory_snapshot(self, *a, **k):
            return {"base_ccy": "DOGE", "total": 98.0084430,
                    "free": 98.0084430, "frozen": 0.0,
                    "open_sells": [], "open_algos": []}
        async def fetch_instrument(self, *a, **k):
            return {"instId": "DOGE-USDT", "baseCcy": "DOGE", "quoteCcy": "USDT",
                    "lotSz": 1.0, "minSz": 1.0, "tickSz": 0.00001, "ok": True}

    saved = lt_mod.okx_private
    lt_mod.okx_private = _OKX()  # type: ignore[assignment]
    try:
        s = _settings_stub(
            live_tester_enabled=True,
            live_native_protection_enabled=False,
            live_max_order_usdt_tester=20.0,
            live_max_trades_per_day=5,
            live_daily_loss_cap_usdt=10.0,
            live_max_open_positions=2,
        )
        # Pre-clear kill switch (other tests may have latched it).
        tester._state.kill_switch = False
        tester._state.kill_switch_reason = ""
        tester._state.trades_today = 0
        res = await tester.attempt_entry(
            settings=s,
            symbol="DOGE/USDT",
            live_price=0.19,
            intended_quote_usdt=20.0,
            sl_price=0.18, take_profit_price=0.21,
            trail_pct=0.0,
            preflight={"allowed": True, "reasons": []},
            decision_id="smoke_v91_entry",
        )
        assert res["status"] == "opened", res
        pos = res["position"]
        # gross == fill
        assert abs(pos["gross_filled_qty"] - 98.106543) < 1e-9, pos
        # base-asset fee deducted from sellable
        assert abs(pos["fee_deducted_from_base"] - 0.0981) < 1e-9, pos
        assert abs(pos["sellable_qty"] - (98.106543 - 0.0981)) < 1e-9, pos
        # entry_fee_ccy preserved
        assert pos["entry_fee_ccy"] == "DOGE", pos
    finally:
        lt_mod.okx_private = saved
    print("[ok] entry path deducts base-asset fee from sellable_qty")


# ---------------------------------------------------------------------------
# V9.1 — exit path clamps to OKX free balance + lotSz; blocks below minSz
# ---------------------------------------------------------------------------

async def test_exit_clamps_to_okx_free_balance() -> None:
    _reset_state_file()
    tester = lt_mod.LiveTester()
    pos = lt_mod.LivePosition(
        id="lv_clamp001",
        client_order_id="v6tclamp", okx_order_id="OKXA",
        symbol="DOGE/USDT", base_ccy="DOGE",
        filled_qty=98.106543, avg_entry_price=0.19,
        gross_filled_qty=98.106543,
        sellable_qty=98.0084430,             # post-fee in bot journal
        fee_deducted_from_base=0.0981,
        entry_fee_ccy="DOGE",
        stop_loss_price=0.18, take_profit_price=0.21,
        status="open",
        close_reason="sl",
    )
    tester._state.positions.append(pos)

    sent_sells: List[Dict[str, Any]] = []

    class _OKX:
        async def market_sell_spot(self, *, symbol, base_qty, client_order_id=""):
            sent_sells.append({"symbol": symbol, "sz": base_qty, "clOrdId": client_order_id})
            return {"data": [{"ordId": "OKXSELL", "clOrdId": client_order_id}]}
        async def summarize_fills(self, *a, **k):
            return {"filled_qty": 97.0, "avg_px": 0.20, "fee": 0.0194, "fee_ccy": "USDT"}
        async def inst_inventory_snapshot(self, *a, **k):
            # OKX reports 97.41979720 free DOGE — less than bot's 98.0084430.
            return {"base_ccy": "DOGE", "total": 97.41979720,
                    "free": 97.41979720, "frozen": 0.0,
                    "open_sells": [], "open_algos": []}
        async def fetch_instrument(self, *a, **k):
            return {"instId": "DOGE-USDT", "baseCcy": "DOGE", "quoteCcy": "USDT",
                    "lotSz": 1.0, "minSz": 1.0, "tickSz": 0.00001, "ok": True}
        async def cancel_algo(self, *a, **k):
            return {"ok": True}

    saved = lt_mod.okx_private
    lt_mod.okx_private = _OKX()  # type: ignore[assignment]
    try:
        await tester._execute_exit(pos)
        assert sent_sells, "market sell was never submitted"
        sent_sz = float(sent_sells[0]["sz"])
        # min(98.0084430, 97.41979720) = 97.41979720; floor to lot=1 = 97.0.
        assert sent_sz == 97.0, f"expected 97.0 sent to OKX, got {sent_sz}"
    finally:
        lt_mod.okx_private = saved
    print("[ok] exit path clamps sell qty to OKX free + lotSz (sent sz=97)")


async def test_exit_blocks_when_below_minsz() -> None:
    _reset_state_file()
    tester = lt_mod.LiveTester()
    pos = lt_mod.LivePosition(
        id="lv_clamp002",
        client_order_id="v6tcmin", okx_order_id="OKXB",
        symbol="DOGE/USDT", base_ccy="DOGE",
        filled_qty=10.0, avg_entry_price=0.19,
        gross_filled_qty=10.0, sellable_qty=9.99,
        stop_loss_price=0.18, take_profit_price=0.21,
        status="open", close_reason="sl",
    )
    tester._state.positions.append(pos)

    sent_sells: List[Dict[str, Any]] = []

    class _OKX:
        async def market_sell_spot(self, *, symbol, base_qty, client_order_id=""):
            sent_sells.append({"symbol": symbol, "sz": base_qty})
            return {"data": [{"ordId": "X"}]}
        async def summarize_fills(self, *a, **k):
            return {"filled_qty": 0, "avg_px": 0, "fee": 0, "fee_ccy": ""}
        async def inst_inventory_snapshot(self, *a, **k):
            return {"base_ccy": "DOGE", "total": 0.5, "free": 0.5, "frozen": 0.0,
                    "open_sells": [], "open_algos": []}
        async def fetch_instrument(self, *a, **k):
            return {"instId": "DOGE-USDT", "baseCcy": "DOGE", "quoteCcy": "USDT",
                    "lotSz": 1.0, "minSz": 1.0, "tickSz": 0.00001, "ok": True}
        async def cancel_algo(self, *a, **k):
            return {"ok": True}

    saved = lt_mod.okx_private
    lt_mod.okx_private = _OKX()  # type: ignore[assignment]
    try:
        await tester._execute_exit(pos)
        assert not sent_sells, "market sell should NOT have been sent"
        assert tester._state.kill_switch is True, "kill switch should latch on clamp"
        # The events ring buffer should have a live_exit_blocked_low_inventory entry.
        kinds = [e.get("kind") for e in tester._state.events]
        assert "live_exit_blocked_low_inventory" in kinds, kinds
    finally:
        lt_mod.okx_private = saved
    print("[ok] exit path blocks sell + latches kill switch when below minSz")


# ---------------------------------------------------------------------------
# V9.1 — nested sCode surfaces in error message
# ---------------------------------------------------------------------------

async def test_nested_sCode_surfaces_in_error() -> None:
    # Build a minimal fake httpx response and run the real OKX error path.
    class _R:
        status_code = 200
        text = ""
        def json(self):
            return {
                "code": "1", "msg": "All operations failed",
                "data": [{"sCode": "51008", "sMsg": "Order failed. Insufficient available balance."}],
            }

    adapter = OKXPrivateAdapter()
    # Patch internal httpx client request to return our fake _R.
    class _FakeClient:
        async def request(self, method, url, content=None, headers=None):
            return _R()
    adapter._client = _FakeClient()  # type: ignore[assignment]
    # Patch signing path too, so we don't need a key.
    os.environ["OKX_API_KEY"] = "k"
    os.environ["OKX_API_SECRET"] = "s"
    os.environ["OKX_API_PASSPHRASE"] = "p"
    try:
        from app.core import settings as set_mod
        set_mod.reset_settings()  # pick up env
    except Exception:
        pass
    raised = None
    try:
        await adapter._request("POST", "/api/v5/trade/order", {"instId": "DOGE-USDT"})
    except OKXAuthError as e:
        raised = e
    finally:
        for k in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"):
            os.environ.pop(k, None)
        try:
            from app.core import settings as set_mod
            set_mod.reset_settings()
        except Exception:
            pass
    assert raised is not None, "OKXAuthError was not raised"
    msg = str(raised)
    assert "sCode=51008" in msg, msg
    assert "Insufficient available balance" in msg, msg
    assert getattr(raised, "nested_s_code", "") == "51008", raised
    print("[ok] nested sCode surfaces in OKXAuthError message")


# ---------------------------------------------------------------------------
# V9.1 — inventory repair clamps stale journal qty to OKX total
# ---------------------------------------------------------------------------

async def test_inventory_repair_clamps_stale_journal() -> None:
    _reset_state_file()
    tester = lt_mod.LiveTester()
    pos = lt_mod.LivePosition(
        id="lv_repair001",
        client_order_id="v6tr", okx_order_id="OKXR",
        symbol="DOGE/USDT", base_ccy="DOGE",
        filled_qty=98.106543, avg_entry_price=0.19,
        gross_filled_qty=98.106543, sellable_qty=98.106543,  # legacy: not fee-deducted
        stop_loss_price=0.18, take_profit_price=0.21,
        status="open",
    )
    tester._state.positions.append(pos)

    class _OKX:
        async def inst_inventory_snapshot(self, *a, **k):
            return {"base_ccy": "DOGE", "total": 97.41979720,
                    "free": 97.41979720, "frozen": 0.0,
                    "open_sells": [], "open_algos": []}

    saved = lt_mod.okx_private
    lt_mod.okx_private = _OKX()  # type: ignore[assignment]
    try:
        snap = await tester._refresh_inventory(pos.id)
        assert snap["free"] == 97.41979720, snap
        repaired = tester._state.positions[0]
        assert abs(repaired.inventory_sellable_qty - 97.41979720) < 1e-9, repaired.inventory_sellable_qty
        assert "clamped journal sellable" in repaired.inventory_repair_note, repaired.inventory_repair_note
        kinds = [e.get("kind") for e in tester._state.events]
        assert "inventory_repair" in kinds, kinds
    finally:
        lt_mod.okx_private = saved
    print("[ok] inventory_repair clamps stale journal qty to OKX total + emits event")


# ---------------------------------------------------------------------------

async def main() -> int:
    await test_oco_payload()
    await test_conditional_payload()
    await test_cancel_payload()
    await test_cancel_already_cancelled()
    await test_failure_surfaces_sCode()
    await test_dry_run_short_circuits()
    await test_disabled_is_none()
    test_client_algo_id_format()
    test_summary_exposes_native_fields()
    await test_inventory_snapshot_shape()
    test_classify_existing_algos()
    await test_fee_ccy_capture()
    test_unattended_readiness_default_fails()
    test_unattended_readiness_can_pass()
    test_unattended_expiry_latches_kill_switch()
    await test_startup_reconcile_gated()
    # V9.1 — sell-qty hotfix coverage.
    test_round_down_to_lot()
    test_clamp_sell_qty_below_min_blocks_sell()
    await test_entry_deducts_base_fee_from_sellable()
    await test_exit_clamps_to_okx_free_balance()
    await test_exit_blocks_when_below_minsz()
    await test_nested_sCode_surfaces_in_error()
    await test_inventory_repair_clamps_stale_journal()
    print("\nAll V9 + V9.1 smoke tests passed (no real HTTP, no real orders).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
