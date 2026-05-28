"""V6 smoke test — no network, no live execution.

Exercises (without ever signing or submitting an OKX request):
  - V6 settings defaults + clamps (live_max_order_usdt_tester, daily cap, etc.)
  - live_tester.gate_check rejects when disabled
  - live_tester.gate_check rejects when OKX is unauthenticated
  - live_tester.engage_kill_switch / release_kill_switch (async)
  - live_tester.summary() shape
  - okx_private._redact never leaks raw secrets

Run:  python _smoke_v6.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

tmp = tempfile.mkdtemp(prefix="v6smoke_")
os.environ.setdefault("DATA_DIR", tmp)
# Make sure live tester cannot reach the network even by accident.
os.environ.pop("OKX_API_KEY", None)
os.environ.pop("OKX_API_SECRET", None)
os.environ.pop("OKX_API_PASSPHRASE", None)

sys.path.insert(0, os.path.dirname(__file__))

from app.core.settings import get_settings, update_settings, reset_settings  # noqa: E402
from app.services.live_tester import live_tester  # noqa: E402
from app.services import okx_private  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}")
        failures.append(label)


async def main() -> int:
    print("\n=== V6 smoke ===")

    print("[1] settings defaults")
    reset_settings()
    s = get_settings()
    check(s.live_tester_enabled is False, "live_tester_enabled defaults False")
    check(1.0 <= float(s.live_max_order_usdt_tester) <= 25.0,
          f"live_max_order_usdt_tester in [1,25] (got {s.live_max_order_usdt_tester})")
    check(0.5 <= float(s.live_daily_loss_cap_usdt) <= 25.0,
          f"live_daily_loss_cap_usdt clamped (got {s.live_daily_loss_cap_usdt})")
    check(1 <= int(s.live_max_trades_per_day) <= 10,
          f"live_max_trades_per_day clamped (got {s.live_max_trades_per_day})")
    check(s.live_one_position_per_symbol is True, "live_one_position_per_symbol default True")
    check(1 <= int(s.live_max_open_positions) <= 3,
          f"live_max_open_positions clamped (got {s.live_max_open_positions})")
    check(s.live_require_protective_exit is True, "live_require_protective_exit default True")
    check(s.live_spot_only is True, "live_spot_only default True")
    check(float(s.live_free_reserve_multiplier) >= 1.0, "live_free_reserve_multiplier >= 1.0")
    check(s.live_tester_override is False, "live_tester_override default False")

    print("[2] settings clamp enforcement on update (high)")
    update_settings({
        "live_max_order_usdt_tester": 9999.0,
        "live_daily_loss_cap_usdt": 9999.0,
        "live_max_trades_per_day": 999,
        "live_max_open_positions": 999,
    })
    s = get_settings()
    check(float(s.live_max_order_usdt_tester) <= 25.0,
          f"max_order high clamp ({s.live_max_order_usdt_tester} <= 25)")
    check(float(s.live_daily_loss_cap_usdt) <= 25.0,
          f"daily cap high clamp ({s.live_daily_loss_cap_usdt} <= 25)")
    check(int(s.live_max_trades_per_day) <= 10,
          f"trades/day high clamp ({s.live_max_trades_per_day} <= 10)")
    check(int(s.live_max_open_positions) <= 3,
          f"open positions high clamp ({s.live_max_open_positions} <= 3)")

    print("[3] settings clamp enforcement on update (low)")
    update_settings({
        "live_max_order_usdt_tester": -5.0,
        "live_daily_loss_cap_usdt": 0.0,
        "live_max_trades_per_day": 0,
        "live_max_open_positions": 0,
    })
    s = get_settings()
    check(float(s.live_max_order_usdt_tester) >= 1.0,
          f"max_order low clamp ({s.live_max_order_usdt_tester} >= 1)")
    check(float(s.live_daily_loss_cap_usdt) >= 0.5,
          f"daily cap low clamp ({s.live_daily_loss_cap_usdt} >= 0.5)")
    check(int(s.live_max_trades_per_day) >= 1,
          f"trades/day low clamp ({s.live_max_trades_per_day} >= 1)")
    check(int(s.live_max_open_positions) >= 1,
          f"open positions low clamp ({s.live_max_open_positions} >= 1)")
    reset_settings()

    print("[4] gate_check rejects when disabled")
    s = get_settings()
    result = live_tester.gate_check(
        s,
        env_flags_ack_ok=True,
        env_flags_unlock_ok=True,
        gate_training_ok=True,
        okx_authenticated=True,
        execution_mode="okx_live",
    )
    check(result.get("allowed") is False, "tester not allowed when disabled")
    reasons = result.get("reasons") or []
    check(any("LIVE_TESTER_ENABLED" in r for r in reasons),
          f"reason mentions LIVE_TESTER_ENABLED (reasons={reasons[:3]})")

    print("[5] gate_check rejects without OKX auth even when enabled")
    update_settings({"live_tester_enabled": True})
    s = get_settings()
    result = live_tester.gate_check(
        s,
        env_flags_ack_ok=True,
        env_flags_unlock_ok=True,
        gate_training_ok=True,
        okx_authenticated=False,
        execution_mode="okx_live",
    )
    check(result.get("allowed") is False, "still blocked without OKX auth")
    reasons = result.get("reasons") or []
    check(any("OKX" in r or "okx" in r for r in reasons),
          f"reason mentions OKX (reasons={reasons[:3]})")
    reset_settings()

    print("[6] gate_check rejects paper mode")
    update_settings({"live_tester_enabled": True})
    s = get_settings()
    result = live_tester.gate_check(
        s,
        env_flags_ack_ok=True,
        env_flags_unlock_ok=True,
        gate_training_ok=True,
        okx_authenticated=True,
        execution_mode="paper",
    )
    check(result.get("allowed") is False, "tester blocked in paper mode")
    reasons = result.get("reasons") or []
    check(any("EXECUTION_MODE" in r for r in reasons),
          f"reason mentions EXECUTION_MODE (reasons={reasons[:3]})")
    reset_settings()

    print("[7] gate_check rejects okx_live without LIVE_TRADING_ENABLED/ACK")
    update_settings({"live_tester_enabled": True})
    s = get_settings()
    result = live_tester.gate_check(
        s,
        env_flags_ack_ok=False,
        env_flags_unlock_ok=False,
        gate_training_ok=True,
        okx_authenticated=True,
        execution_mode="okx_live",
    )
    check(result.get("allowed") is False, "okx_live blocked without LIVE_TRADING_*")
    reasons = result.get("reasons") or []
    check(any("LIVE_TRADING_ENABLED" in r for r in reasons),
          f"reason mentions LIVE_TRADING_ENABLED ({reasons[:4]})")
    check(any("LIVE_TRADING_ACK" in r for r in reasons),
          f"reason mentions LIVE_TRADING_ACK ({reasons[:4]})")
    reset_settings()

    print("[8] gate_check rejects when training gate fails (no override)")
    update_settings({"live_tester_enabled": True})
    s = get_settings()
    result = live_tester.gate_check(
        s,
        env_flags_ack_ok=True,
        env_flags_unlock_ok=True,
        gate_training_ok=False,
        okx_authenticated=True,
        execution_mode="okx_live",
    )
    check(result.get("allowed") is False, "training-gate failure blocks tester")
    reasons = result.get("reasons") or []
    check(any("Training gate" in r or "training gate" in r for r in reasons),
          f"reason mentions training gate ({reasons[:4]})")
    reset_settings()

    print("[9] kill switch lifecycle (async)")
    # Try to release first to ensure a clean baseline.
    try:
        await live_tester.release_kill_switch()
    except Exception:
        pass
    summary = live_tester.summary()
    check(summary.get("kill_switch") is False, "kill switch starts released")

    await live_tester.engage_kill_switch(reason="smoke_test_engage")
    summary = live_tester.summary()
    check(summary.get("kill_switch") is True, "kill switch engaged after call")
    check("smoke_test_engage" in str(summary.get("kill_switch_reason") or ""),
          f"kill switch reason recorded ({summary.get('kill_switch_reason')})")

    update_settings({"live_tester_enabled": True})
    s = get_settings()
    result = live_tester.gate_check(
        s,
        env_flags_ack_ok=True,
        env_flags_unlock_ok=True,
        gate_training_ok=True,
        okx_authenticated=True,
        execution_mode="okx_live",
    )
    check(result.get("allowed") is False, "kill switch blocks gate_check")
    reasons = result.get("reasons") or []
    check(any("Kill switch" in r or "kill switch" in r.lower() for r in reasons),
          f"reason mentions kill switch ({reasons[:4]})")
    await live_tester.release_kill_switch()
    summary = live_tester.summary()
    check(summary.get("kill_switch") is False, "kill switch releases")
    reset_settings()

    print("[10] summary shape")
    summary = live_tester.summary()
    expected_keys = {
        "enabled", "kill_switch", "max_order_usdt", "daily_loss_cap_usdt",
        "trades_today", "max_trades_per_day", "realized_pnl_today",
        "open_position_count", "max_open_positions", "stop_mode",
        "open_positions",
    }
    missing = expected_keys - set(summary.keys())
    check(not missing, f"summary has all required keys (missing={missing})")
    check(summary.get("stop_mode") == "bot_managed",
          f"stop_mode is bot_managed (got {summary.get('stop_mode')})")
    check(isinstance(summary.get("open_positions"), list),
          "open_positions is a list")
    check("stop_warning" in summary and "running" in summary["stop_warning"].lower(),
          "stop_warning explicitly notes app must be running")

    print("[11] okx_private redaction never leaks secrets")
    sample = "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
    red = okx_private._redact(sample)
    check(sample not in red, f"raw secret not in redacted output (got {red!r})")
    check("***" in red or "len=" in red, f"redacted form has marker (got {red!r})")
    short = okx_private._redact("a")
    check("a" not in short or "***" in short or "len=" in short,
          f"short value redacts ({short!r})")
    none_red = okx_private._redact(None)
    check(isinstance(none_red, str), f"redact(None) returns a string (got {none_red!r})")

    print("\n=== summary ===")
    if failures:
        print(f"FAILED: {len(failures)} checks")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All V6 smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
