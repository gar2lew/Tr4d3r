"""V5.4 smoke test — no network, no live execution.

Exercises:
  - settings defaults + clamps
  - market_data.is_excludable_symbol on stable/pegged/synthetic bases
  - symbol_profile.build_profile on representative inputs
  - paper_engine cash reserve gate
  - paper_engine._verify_exit on a long TP that price did NOT reach
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

# Run in an isolated tmp dir so we don't touch user data
tmp = tempfile.mkdtemp(prefix="v54smoke_")
os.environ["DATA_DIR"] = tmp  # if app honours this; otherwise we just don't persist real changes

sys.path.insert(0, os.path.dirname(__file__))

from app.core.settings import get_settings, update_settings, RuntimeSettings  # noqa: E402
from app.services.market_data import market  # noqa: E402
from app.services.symbol_profile import build_profile  # noqa: E402
from app.services.paper_engine import engine, Position  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if not cond:
        failures.append(label)
        print(f"  FAIL  {label}")
    else:
        print(f"  ok    {label}")


def test_settings():
    print("[1] settings defaults")
    s = get_settings()
    check(s.paper_profile == "live_readiness", "paper_profile defaults to live_readiness")
    check(s.min_cash_reserve_pct >= 0, "min_cash_reserve_pct present")
    check(s.max_capital_in_positions_pct >= 5, "max_capital_in_positions_pct present")
    check(s.global_trade_cooldown_seconds >= 0, "global_trade_cooldown_seconds present")
    check(s.per_symbol_cooldown_seconds >= 0, "per_symbol_cooldown_seconds present")
    check(s.realistic_fills_enabled is True, "realistic_fills_enabled default True")
    check(s.exit_verification_enabled is True, "exit_verification_enabled default True")
    check(s.symbol_adaptive_enabled is True, "symbol_adaptive_enabled default True")
    for b in ("USDG", "USDC", "RLUSD", "PAXG", "XAUT", "BETH", "STETH", "OKSOL", "WBTC", "WETH"):
        check(b in [x.upper() for x in s.paper_excluded_bases], f"{b} in default paper_excluded_bases")


def test_market_excludable():
    print("[2] market.is_excludable_symbol")
    s = get_settings()
    bases = list(s.paper_excluded_bases)
    for sym in ("USDG/USDT", "USDC/USDT", "PAXG/USDT", "STETH/USDT", "WBTC/USDT"):
        check(market.is_excludable_symbol(sym, bases), f"{sym} excluded")
    # USD-prefix safety net even if not in list
    check(market.is_excludable_symbol("USDX/USDT", []), "USD* prefix safety net")
    # Real coin must pass
    check(not market.is_excludable_symbol("BTC/USDT", bases), "BTC/USDT NOT excluded")


def test_symbol_profile():
    print("[3] symbol_profile.build_profile")
    # USDG-like: tiny ATR, near-flat 24h move, price ~ $1
    ticker = {"last": 1.0001, "bid": 1.0000, "ask": 1.0002, "vol24h": 5_000_000,
              "open24h": 1.0000}
    snap = {"atr14": 0.0002, "rsi14": 50.0, "vol_ratio": 1.0, "bias": "unknown"}
    p = build_profile("USDG/USDT", ticker, snap, excluded_symbol=True)
    check(not p.tradeable, "USDG-like profile tradeable=False")
    check(any("blocklist" in r for r in p.block_reasons), "USDG-like has blocklist reason")
    # Healthy coin
    t2 = {"last": 60000.0, "bid": 59995.0, "ask": 60005.0, "vol24h": 20000, "open24h": 59000.0}
    s2 = {"atr14": 600.0, "rsi14": 55.0, "vol_ratio": 1.2, "bias": "bullish"}
    p2 = build_profile("BTC/USDT", t2, s2)
    check(p2.tradeable, "BTC-like profile tradeable=True")
    check(0.5 <= p2.sl_mult <= 2.5, "sl_mult within clamps")
    check(0.2 <= p2.size_mult <= 1.0, "size_mult within clamps")


async def test_cash_reserve_and_verify():
    print("[4] paper_engine cash reserve + exit verification")
    s = get_settings()
    # Force a clean engine state with a known starting balance
    await engine.reset()
    eq = engine.equity({})
    check(eq > 0, f"engine equity after reset > 0 (got {eq})")
    # can_enter should pass with empty state
    ok, why = engine.can_enter(s, eq, symbol="BTC/USDT", entry_kind="standard")
    check(ok, f"can_enter ok on fresh state (why={why})")
    # Simulate cash drained below reserve → can_enter should refuse
    saved_cash = engine.state.cash_usdt
    engine.state.cash_usdt = 1.0
    ok, why = engine.can_enter(s, eq, symbol="BTC/USDT", entry_kind="standard")
    check(not ok and "reserve" in why.lower(), f"can_enter refuses below reserve (why={why})")
    engine.state.cash_usdt = saved_cash

    # Exit verification — long TP not actually reached
    pos = Position(
        id="t1", symbol="BTC/USDT", side="long", qty=0.001,
        entry_price=60000.0, opened_ts=0.0,
        stop_loss=59000.0, take_profit=61000.0, trailing_stop=59500.0,
        high_water=60000.0, fees_paid=0.0, decision_id="", leverage=1.0,
        margin_used=60.0, notional=60.0, liquidation_price=0.0,
        entry_kind="standard", paper_profile="live_readiness",
    )
    # market view says last/bid/ask and candle high are all below the TP
    view_unreach = {"last": 60500.0, "bid": 60495.0, "ask": 60505.0,
                    "candle_high": 60800.0, "candle_low": 60100.0}
    engine.set_market_view({"BTC/USDT": view_unreach})
    ok1, meta1 = engine._verify_exit(pos, "tp", pos.take_profit, view_unreach, verification_enabled=True)
    check(not ok1, f"TP refused when market did not reach it (meta={meta1})")
    check(meta1.get("validated") is False, "meta marks validated=False")
    # Now make candle reach TP → verification allows it
    view_reach = {"last": 60500.0, "bid": 60495.0, "ask": 60505.0,
                  "candle_high": 61200.0, "candle_low": 60100.0}
    engine.set_market_view({"BTC/USDT": view_reach})
    ok2, meta2 = engine._verify_exit(pos, "tp", pos.take_profit, view_reach, verification_enabled=True)
    check(ok2, f"TP allowed when candle_high reached (meta={meta2})")
    check(meta2.get("source") == "candle_high", f"trigger source is candle_high (got {meta2.get('source')})")
    # Long SL not actually touched
    view_sl_unreach = {"last": 60500.0, "bid": 60495.0, "ask": 60505.0,
                       "candle_high": 61200.0, "candle_low": 60100.0}
    ok3, meta3 = engine._verify_exit(pos, "sl", pos.stop_loss, view_sl_unreach, verification_enabled=True)
    check(not ok3, f"SL refused when market did not breach it (meta={meta3})")


async def test_duplicate_symbol_guard():
    print("[5] paper_engine duplicate-symbol guard (V6 hook)")
    await engine.reset()
    s = get_settings()
    s.max_open_positions = 3
    s.global_trade_cooldown_seconds = 0
    s.per_symbol_cooldown_seconds = 0
    engine.set_market_view({"BTC/USDT": {
        "last": 60000.0, "bid": 59995.0, "ask": 60005.0,
        "candle_high": 60100.0, "candle_low": 59900.0,
    }})
    ok, _ = engine.can_enter(s, engine.equity({"BTC/USDT": 60000.0}), symbol="BTC/USDT")
    check(ok, "first BTC entry permitted")
    await engine.open_long("BTC/USDT", 60000.0, s, {"BTC/USDT": 60000.0},
                           reason="smoke", decision_id="dup-1")
    ok2, why2 = engine.can_enter(s, engine.equity({"BTC/USDT": 60000.0}), symbol="BTC/USDT")
    check(not ok2, f"duplicate BTC entry blocked (why={why2!r})")
    check("already has an open position" in why2, "duplicate guard reason text correct")


async def main():
    test_settings()
    test_market_excludable()
    test_symbol_profile()
    await test_cash_reserve_and_verify()
    await test_duplicate_symbol_guard()
    if failures:
        print(f"\nFAILED: {len(failures)} check(s)")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("\nAll V5.4 smoke checks passed.")


asyncio.run(main())
