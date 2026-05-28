"""FastAPI entrypoint — REST + SSE + static frontend.

LIVE EXECUTION IS HARD-LOCKED. The `live_trading_requested` env flag is
displayed but never honored: all "execute" paths refuse to run.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.core.events import bus
from app.core.settings import (
    EnvFlags,
    RuntimeSettings,
    get_settings,
    load_env_flags,
    update_settings,
    DEFAULT_SYMBOLS,
    STRATEGY_MODES,
    STRATEGY_MODE_IDS,
)
from app.services.ai_brain import brain
from app.services.market_data import market
from app.services.paper_engine import engine
from app.services.strategy import strategy
from app.services.hermes import hermes
from app.services.okx_private import okx_private
from app.services.live_tester import live_tester
from app.services.scanner_activity import scanner_activity


EXECUTION_MODES = ("paper", "okx_demo", "okx_live")
LIVE_DEMO_COMPLETED_TOKEN = "I_RAN_OKX_DEMO_FIRST"
LIVE_RISK_ACK_TOKEN = "I_ACCEPT_REAL_MONEY_RISK"
# V6 — optional override that unlocks the tiny tester before the V5.4
# eligibility journal has fully accumulated. Used only by very early test
# runs; the UI flags it loudly when active.
LIVE_TESTER_OVERRIDE_TOKEN = "I_UNDERSTAND_THIS_IS_A_TINY_TEST"


WEB_DIR = Path(__file__).resolve().parent.parent / "web"


app = FastAPI(
    title="AI Hummingbot Brain",
    description="Paper-only AI trading command center. Live execution is locked.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup() -> None:
    # Warm-up: fetch tickers once
    s = get_settings()
    try:
        await market.refresh_all(s.symbols, s.timeframe, [s.bias_timeframe])
    except Exception:
        pass
    # V9 — opt-in startup reconcile for live tester native protection.
    # Strictly gated by LIVE_NATIVE_PROTECTION_RECONCILE_ON_STARTUP. Runs
    # as a background task so a slow OKX call cannot delay startup.
    if bool(getattr(s, "live_native_protection_reconcile_on_startup", False)):
        async def _do_reconcile():
            try:
                await live_tester.startup_reconcile(s)
            except Exception:
                pass
        try:
            asyncio.create_task(_do_reconcile())
        except Exception:
            pass


@app.on_event("shutdown")
async def _shutdown() -> None:
    await strategy.stop()
    await market.close()
    await brain.close()
    try:
        await okx_private.close()
    except Exception:
        pass


# ----------------- helpers -----------------

def _build_readiness(settings: RuntimeSettings) -> dict:
    """Pure synchronous readiness assembly minus the live OKX REST probe.

    The OKX probe is performed by the /api/live/readiness route which awaits it.
    This helper builds the deterministic portion so /api/health can include it
    cheaply on every poll.
    """
    mode = settings.execution_mode if settings.execution_mode in EXECUTION_MODES else "paper"
    gate = hermes.training_gate(settings, require_demo_first=(mode == "okx_live"))
    live_demo_ack = os.getenv("LIVE_DEMO_COMPLETED_ACK", "").strip()
    live_risk_ack = os.getenv("LIVE_TRADING_ACK", "").strip()
    live_unlock = os.getenv("LIVE_TRADING_ENABLED", "false").strip().lower() == "true"
    reasons: list[str] = []
    can_execute = False
    if mode == "paper":
        reasons.append("Paper mode selected — OKX execution is disabled. Set EXECUTION_MODE=okx_demo to begin demo training.")
    else:
        if not gate.get("ok"):
            reasons.extend(gate.get("blocked_reasons", []))
        if mode == "okx_live":
            if live_demo_ack != LIVE_DEMO_COMPLETED_TOKEN:
                reasons.append(
                    f"Set LIVE_DEMO_COMPLETED_ACK={LIVE_DEMO_COMPLETED_TOKEN} after a successful OKX demo run"
                )
            if not live_unlock:
                reasons.append("Set LIVE_TRADING_ENABLED=true for live mode")
            if live_risk_ack != LIVE_RISK_ACK_TOKEN:
                reasons.append(f"Set LIVE_TRADING_ACK={LIVE_RISK_ACK_TOKEN} to acknowledge real-money risk")
            if bool(getattr(settings, "leverage_enabled", False)):
                reasons.append("Real-money leverage is disabled in this build — turn LEVERAGE_ENABLED off for live")
    # Pre-compute structural OKX status without hitting the network. The
    # live route refreshes this with check_readiness().
    okx_status = okx_private.status
    okx_authenticated = bool(okx_status.get("authenticated"))
    tester_decision = live_tester.gate_check(
        settings,
        env_flags_ack_ok=(live_risk_ack == LIVE_RISK_ACK_TOKEN),
        env_flags_unlock_ok=live_unlock,
        gate_training_ok=bool(gate.get("ok")),
        okx_authenticated=okx_authenticated,
        execution_mode=mode,
    )
    tester_summary = live_tester.summary(settings)
    return {
        "execution_mode": mode,
        "can_execute": can_execute,           # always False until readiness route confirms OKX
        "reasons": reasons,
        "training_gate": gate,
        "okx": okx_status,
        "live_demo_ack_ok": live_demo_ack == LIVE_DEMO_COMPLETED_TOKEN,
        "live_risk_ack_ok": live_risk_ack == LIVE_RISK_ACK_TOKEN,
        "live_unlock_env": live_unlock,
        "live_max_order_usdt": float(settings.live_max_order_usdt),
        "live_tester": {
            "enabled": bool(settings.live_tester_enabled),
            "override_active": bool(settings.live_tester_override),
            "override_token_required": LIVE_TESTER_OVERRIDE_TOKEN,
            "allowed": tester_decision["allowed"],
            "blocked_reasons": tester_decision["reasons"],
            # V6.3 — tester lifecycle hints for the UI banner.
            "tester_state": tester_decision.get("tester_state", "disabled"),
            "unlocked": bool(tester_decision.get("unlocked", False)),
            "ready_message": tester_decision.get("ready_message", ""),
            "summary": tester_summary,
        },
        "leverage": {
            "enabled": bool(getattr(settings, "leverage_enabled", False)),
            "multiplier": float(getattr(settings, "leverage_multiplier", 1.0)),
            "max_multiplier": float(getattr(settings, "leverage_max_multiplier", 3.0)),
            "paper_demo_only": True,
            "notice": "Leverage is simulated for paper/demo training only. Real OKX leverage/perps are NOT wired in this build.",
        },
        "policy": {
            "spot_only": True,
            "no_withdrawals": True,
            "no_perps": True,
            "client_order_id_required": True,
            "live_max_order_usdt": float(settings.live_max_order_usdt),
        },
    }


# ----------------- REST -----------------

@app.get("/api/health")
async def health() -> dict:
    env = load_env_flags()
    s = get_settings()
    readiness = _build_readiness(s)
    return {
        "ok": True,
        "app": env.app_name,
        "paper_mode": s.execution_mode == "paper",
        "execution_mode": s.execution_mode,
        "live_trading_locked": True,    # always true at /api/health — confirm via /api/live/readiness
        "live_trading_requested_env": env.live_trading_requested,
        "ai_key_present": bool(env.openrouter_api_key),
        "ai_status": brain.status,
        "market_status": market.status,
        "data_quality": hermes.last_data_quality,
        "hermes": hermes.summary(),
        "settings": s.to_dict(),
        "strategies": STRATEGY_MODES,
        "bot": strategy.status,
        "training_gate": readiness["training_gate"],
        "okx_private": readiness["okx"],
        "okx_account": okx_private.last_account,
        "live_tester": live_tester.summary(s),
        "readiness": readiness,
        # V7 — scanner activity snapshot for the dashboard.
        "scanner": scanner_activity.snapshot(),
        "market_intel": {
            "provider": getattr(s, "market_intel_provider", "none"),
            "note": "OKX public data is sufficient. CoinGecko/CoinMarketCap are optional enrichment only.",
        },
    }


@app.get("/api/settings")
async def get_settings_route() -> dict:
    return {
        "settings": get_settings().to_dict(),
        "defaults": {"symbols": list(DEFAULT_SYMBOLS), "strategies": STRATEGY_MODES},
    }


@app.get("/api/strategies")
async def strategies_route() -> dict:
    return {"strategies": STRATEGY_MODES}


@app.post("/api/settings")
async def update_settings_route(payload: Dict[str, Any]) -> dict:
    # Filter to known keys and coerce primitive types
    allowed_numeric = {
        "starting_balance_usdt", "risk_per_trade_pct", "max_position_pct",
        "stop_loss_pct", "take_profit_pct", "trailing_stop_pct",
        "daily_loss_cap_pct", "fee_pct", "slippage_pct", "confidence_threshold",
        "ai_timeout_seconds",
        # V5 — demo/live readiness gates
        "live_min_win_rate", "live_min_total_pnl_usdt", "live_max_order_usdt",
        # V5 — leverage simulator (paper/demo only)
        "leverage_multiplier", "leverage_max_multiplier",
        "leverage_liquidation_buffer_pct", "leverage_max_daily_loss_pct",
        # V5.1 — active paper training / dynamic discovery / learning samples
        "paper_training_min_score", "paper_training_size_pct",
        "dynamic_min_quote_volume_usdt",
        # V7 — nothing numeric specific here; max_scan_symbols is an int below
        "learning_sample_min_score", "learning_sample_size_pct",
        # V5.4 — realism
        "min_cash_reserve_pct", "max_capital_in_positions_pct",
        "symbol_min_atr_pct", "symbol_max_atr_pct",
        "symbol_max_spread_pct", "symbol_min_quote_vol_usdt",
        # V6 — tester numeric caps
        "live_max_order_usdt_tester", "live_daily_loss_cap_usdt",
        "live_free_reserve_multiplier",
    }
    allowed_int = {
        "max_trades_per_day", "max_open_positions", "price_refresh_seconds",
        "scan_interval_seconds", "ai_max_tokens",
        "live_min_closed_trades", "live_min_data_quality_score",
        "leverage_extra_min_closed_trades",
        # V5.1
        "max_dynamic_symbols", "paper_training_max_daily_trades",
        "learning_sample_max_per_day",
        # V7 — scan universe ceiling (core + discovered).
        "max_scan_symbols",
        # V5.4 — cooldowns
        "global_trade_cooldown_seconds", "per_symbol_cooldown_seconds",
        # V6 — tester caps (ints)
        "live_max_trades_per_day", "live_max_open_positions",
    }
    allowed_str = {
        "timeframe", "bias_timeframe", "active_strategy",
        "commander_model", "scout_model", "risk_model", "skeptic_model",
        "execution_mode",
        # V5.4
        "paper_profile",
        # V7 — optional market-intel provider ("none" or "coingecko").
        "market_intel_provider",
    }
    allowed_bool = {
        "indicator_only_mode", "ride_winners_enabled", "leverage_enabled",
        # V5.1 toggles
        "active_paper_training", "paper_training_allow_exploration",
        "auto_strategy_selection", "dynamic_symbol_discovery",
        "learning_sample_enabled",
        # V5.4 toggles
        "realistic_fills_enabled", "exit_verification_enabled",
        "symbol_adaptive_enabled",
        # V6 — tester toggles. Note: live_tester_enabled is settable via the
        # settings panel BUT a non-paper EXECUTION_MODE and valid OKX creds are
        # required for it to actually act. Override stays env-only on purpose.
        "live_tester_enabled",
        "live_one_position_per_symbol", "live_require_protective_exit",
        "live_spot_only",
    }

    patch: Dict[str, Any] = {}
    for k, v in payload.items():
        try:
            if k in allowed_numeric:
                patch[k] = float(v)
            elif k in allowed_int:
                patch[k] = int(v)
            elif k in allowed_str:
                value = str(v)
                if k == "active_strategy" and value not in STRATEGY_MODE_IDS:
                    continue
                if k == "execution_mode" and value not in EXECUTION_MODES:
                    continue
                patch[k] = value
            elif k in allowed_bool:
                if isinstance(v, str):
                    patch[k] = v.strip().lower() in ("1", "true", "yes", "on")
                else:
                    patch[k] = bool(v)
            elif k == "symbols" and isinstance(v, list):
                patch[k] = [str(x).upper() for x in v if str(x).strip()]
            elif k == "dynamic_exclude_bases":
                # V5.2 — allow either CSV string or array of bases
                if isinstance(v, list):
                    patch[k] = [str(x).upper().strip() for x in v if str(x).strip()]
                elif isinstance(v, str):
                    patch[k] = [s.strip().upper() for s in v.split(",") if s.strip()]
            elif k == "paper_excluded_bases":
                # V5.4 — stable/pegged/synthetic exclusion list (CSV or array)
                if isinstance(v, list):
                    patch[k] = [str(x).upper().strip() for x in v if str(x).strip()]
                elif isinstance(v, str):
                    patch[k] = [s.strip().upper() for s in v.split(",") if s.strip()]
        except Exception:
            continue

    # ---- safety clamps for V5 gate fields ----
    if "live_min_closed_trades" in patch:
        patch["live_min_closed_trades"] = max(10, min(2000, int(patch["live_min_closed_trades"])))
    if "live_min_win_rate" in patch:
        patch["live_min_win_rate"] = max(0.0, min(0.95, float(patch["live_min_win_rate"])))
    if "live_min_total_pnl_usdt" in patch:
        # Net P/L can be negative for testing but cap absurd values.
        patch["live_min_total_pnl_usdt"] = max(-10000.0, min(1_000_000.0, float(patch["live_min_total_pnl_usdt"])))
    if "live_min_data_quality_score" in patch:
        patch["live_min_data_quality_score"] = max(50, min(100, int(patch["live_min_data_quality_score"])))
    if "live_max_order_usdt" in patch:
        # Hard cap — even if a user types 10,000 we keep it tiny by policy.
        patch["live_max_order_usdt"] = max(1.0, min(50.0, float(patch["live_max_order_usdt"])))
    if "leverage_multiplier" in patch:
        patch["leverage_multiplier"] = max(1.0, min(10.0, float(patch["leverage_multiplier"])))
    if "leverage_max_multiplier" in patch:
        patch["leverage_max_multiplier"] = max(1.0, min(10.0, float(patch["leverage_max_multiplier"])))
    if "leverage_liquidation_buffer_pct" in patch:
        patch["leverage_liquidation_buffer_pct"] = max(40.0, min(95.0, float(patch["leverage_liquidation_buffer_pct"])))
    if "leverage_max_daily_loss_pct" in patch:
        patch["leverage_max_daily_loss_pct"] = max(0.5, min(10.0, float(patch["leverage_max_daily_loss_pct"])))
    if "leverage_extra_min_closed_trades" in patch:
        patch["leverage_extra_min_closed_trades"] = max(20, min(2000, int(patch["leverage_extra_min_closed_trades"])))

    # ---- safety clamps for V5.1 fields ----
    if "paper_training_min_score" in patch:
        patch["paper_training_min_score"] = max(0.20, min(0.95, float(patch["paper_training_min_score"])))
    if "paper_training_size_pct" in patch:
        patch["paper_training_size_pct"] = max(10.0, min(100.0, float(patch["paper_training_size_pct"])))
    if "paper_training_max_daily_trades" in patch:
        patch["paper_training_max_daily_trades"] = max(1, min(100, int(patch["paper_training_max_daily_trades"])))
    if "max_dynamic_symbols" in patch:
        # V5.2 — cap tightened from 100 → 30. Combined with bulk-ticker +
        # candle caches in market_data.py this stays well under OKX public
        # rate limits even at the cap.
        patch["max_dynamic_symbols"] = max(0, min(30, int(patch["max_dynamic_symbols"])))
    # V7 — hard upper bound on full scan universe (core + discovered).
    if "max_scan_symbols" in patch:
        patch["max_scan_symbols"] = max(8, min(80, int(patch["max_scan_symbols"])))
    # V7 — market-intel provider is enum-style. Anything unknown silently
    # falls back to "none" so the app keeps working without external APIs.
    if "market_intel_provider" in patch:
        val = str(patch["market_intel_provider"]).strip().lower()
        patch["market_intel_provider"] = val if val in ("none", "coingecko") else "none"
    if "dynamic_min_quote_volume_usdt" in patch:
        patch["dynamic_min_quote_volume_usdt"] = max(10_000.0, min(500_000_000.0, float(patch["dynamic_min_quote_volume_usdt"])))
    if "learning_sample_min_score" in patch:
        patch["learning_sample_min_score"] = max(0.15, min(0.80, float(patch["learning_sample_min_score"])))
    if "learning_sample_size_pct" in patch:
        patch["learning_sample_size_pct"] = max(10.0, min(100.0, float(patch["learning_sample_size_pct"])))
    if "learning_sample_max_per_day" in patch:
        patch["learning_sample_max_per_day"] = max(1, min(50, int(patch["learning_sample_max_per_day"])))

    # ---- V5.4 realism clamps ----
    if "paper_profile" in patch and patch["paper_profile"] not in ("learning", "live_readiness"):
        patch["paper_profile"] = "live_readiness"
    if "min_cash_reserve_pct" in patch:
        patch["min_cash_reserve_pct"] = max(0.0, min(80.0, float(patch["min_cash_reserve_pct"])))
    if "max_capital_in_positions_pct" in patch:
        patch["max_capital_in_positions_pct"] = max(5.0, min(100.0, float(patch["max_capital_in_positions_pct"])))
    if "global_trade_cooldown_seconds" in patch:
        patch["global_trade_cooldown_seconds"] = max(0, min(7200, int(patch["global_trade_cooldown_seconds"])))
    if "per_symbol_cooldown_seconds" in patch:
        patch["per_symbol_cooldown_seconds"] = max(0, min(86400, int(patch["per_symbol_cooldown_seconds"])))
    if "symbol_min_atr_pct" in patch:
        patch["symbol_min_atr_pct"] = max(0.0, min(2.0, float(patch["symbol_min_atr_pct"])))
    if "symbol_max_atr_pct" in patch:
        patch["symbol_max_atr_pct"] = max(0.5, min(30.0, float(patch["symbol_max_atr_pct"])))
    if "symbol_max_spread_pct" in patch:
        patch["symbol_max_spread_pct"] = max(0.01, min(5.0, float(patch["symbol_max_spread_pct"])))
    if "symbol_min_quote_vol_usdt" in patch:
        patch["symbol_min_quote_vol_usdt"] = max(1_000.0, min(5_000_000_000.0, float(patch["symbol_min_quote_vol_usdt"])))

    # ---- V6 tester clamps ----
    if "live_max_order_usdt_tester" in patch:
        patch["live_max_order_usdt_tester"] = max(1.0, min(25.0, float(patch["live_max_order_usdt_tester"])))
    if "live_daily_loss_cap_usdt" in patch:
        patch["live_daily_loss_cap_usdt"] = max(0.5, min(25.0, float(patch["live_daily_loss_cap_usdt"])))
    if "live_free_reserve_multiplier" in patch:
        patch["live_free_reserve_multiplier"] = max(1.0, min(2.0, float(patch["live_free_reserve_multiplier"])))
    if "live_max_trades_per_day" in patch:
        patch["live_max_trades_per_day"] = max(1, min(10, int(patch["live_max_trades_per_day"])))
    if "live_max_open_positions" in patch:
        patch["live_max_open_positions"] = max(1, min(3, int(patch["live_max_open_positions"])))

    # V5.4 — live_readiness profile caps daily trade count for realism.
    preview_profile = patch.get("paper_profile", get_settings().paper_profile)
    if preview_profile == "live_readiness":
        cur_cap = patch.get("max_trades_per_day", get_settings().max_trades_per_day)
        try:
            cap_int = int(cur_cap)
            if cap_int > 12:
                patch["max_trades_per_day"] = 12
        except Exception:
            pass

    # Ensure leverage_multiplier never exceeds the cap.
    new_settings_preview = {**get_settings().to_dict(), **patch}
    if float(new_settings_preview.get("leverage_multiplier", 1.0)) > float(new_settings_preview.get("leverage_max_multiplier", 3.0)):
        patch["leverage_multiplier"] = float(new_settings_preview["leverage_max_multiplier"])

    s = update_settings(patch)
    await bus.publish("settings_updated", s.to_dict())
    return {"settings": s.to_dict()}


@app.post("/api/bot/start")
async def bot_start() -> dict:
    await strategy.start()
    return {"ok": True, "bot": strategy.status}


@app.post("/api/bot/stop")
async def bot_stop() -> dict:
    await strategy.stop()
    return {"ok": True, "bot": strategy.status}


@app.post("/api/bot/find-best")
async def bot_find_best() -> dict:
    result = await strategy.find_best_trade_now()
    return {"ok": True, "result": result}


@app.post("/api/paper/reset")
async def paper_reset() -> dict:
    await engine.reset()
    prices = {s: t.get("last", 0.0) for s, t in market.all_tickers().items()}
    return {"ok": True, "portfolio": engine.summary(prices)}


@app.get("/api/portfolio")
async def portfolio() -> dict:
    prices = {s: t.get("last", 0.0) for s, t in market.all_tickers().items()}
    return engine.summary(prices)


@app.get("/api/trades")
async def trades() -> dict:
    return {"trades": engine.trade_log(200)}


@app.get("/api/hermes")
async def hermes_route() -> dict:
    return hermes.summary()


@app.post("/api/hermes/reset")
async def hermes_reset() -> dict:
    await hermes.reset()
    return {"ok": True, "hermes": hermes.summary()}


@app.get("/api/market")
async def market_endpoint() -> dict:
    s = get_settings()
    tickers = await market.refresh_all(s.symbols, s.timeframe, [s.bias_timeframe])
    quality = market.quality_report(s.symbols, [s.bias_timeframe, s.timeframe])
    await hermes.set_data_quality(quality)
    return {"status": market.status, "quality": quality, "tickers": tickers}


# ---- V7: Scanner activity + blockers ----
@app.get("/api/scanner")
async def scanner_endpoint() -> dict:
    """V7 — observability snapshot for the dashboard.

    Returns what the scanner has been doing this cycle, top candidates and
    why they were rejected, the structured blocker categories for the
    'Why no live trade?' panel, last qualified signal considered and the
    last live-tester order attempt (if any). Read-only.
    """
    s = get_settings()
    snap = scanner_activity.snapshot()
    # Always recompute blockers from current state so a polling client can
    # get fresh data between scans.
    try:
        blockers = strategy._compute_blockers(settings=s, status="poll")
        snap["blockers"] = blockers
    except Exception:
        pass
    return {
        "scanner": snap,
        "market_intel": {
            "provider": getattr(s, "market_intel_provider", "none"),
            "available_providers": ["none", "coingecko"],
            "note": "OKX public is sufficient for fills. CoinGecko/CoinMarketCap are optional enrichment only.",
        },
        "safety": {
            "shorts_available": False,
            "perps_available": False,
            "leverage_available": False,
            "direction": "LONG (spot buy only)",
        },
    }


# ---- live/demo readiness ----
@app.get("/api/live/readiness")
async def live_readiness() -> dict:
    """Single source of truth for whether OKX demo/live could execute.

    NOTE: A `True` here means the *gates* pass. The strategy/execution layer
    still does not auto-place real-money OKX orders in this build. See
    README — the OKX adapter is currently a readiness check only.
    """
    s = get_settings()
    readiness = _build_readiness(s)
    mode = readiness["execution_mode"]
    # Refresh OKX status by hitting balance endpoint when creds present.
    okx_check: dict = readiness["okx"]
    if mode in ("okx_demo", "okx_live"):
        try:
            okx_check = await okx_private.check_readiness(mode)
        except Exception as e:
            okx_check = {
                "configured": False,
                "authenticated": False,
                "reason": f"okx probe failed: {type(e).__name__}: {e}",
            }
        readiness["okx"] = okx_check
    # Final can_execute decision
    gate_ok = bool(readiness["training_gate"].get("ok"))
    if mode == "paper":
        readiness["can_execute"] = False
        if not readiness["reasons"]:
            readiness["reasons"].append("Paper mode selected.")
    elif mode == "okx_demo":
        readiness["can_execute"] = gate_ok and bool(okx_check.get("can_demo_trade"))
        if not okx_check.get("configured"):
            readiness["reasons"].append("OKX API key/secret/passphrase not configured")
        elif not okx_check.get("authenticated"):
            readiness["reasons"].append(f"OKX demo auth failed: {okx_check.get('reason') or 'unknown'}")
    elif mode == "okx_live":
        readiness["can_execute"] = (
            gate_ok
            and bool(okx_check.get("can_live_trade"))
            and readiness["live_demo_ack_ok"]
            and readiness["live_risk_ack_ok"]
            and readiness["live_unlock_env"]
        )
        if not okx_check.get("configured"):
            readiness["reasons"].append("OKX API key/secret/passphrase not configured")
    # V6 — attach a refreshed account snapshot + tester gate state for UI.
    try:
        readiness["okx_account"] = okx_private.last_account
    except Exception:
        pass
    try:
        tester_decision = live_tester.gate_check(
            s,
            env_flags_ack_ok=readiness["live_risk_ack_ok"],
            env_flags_unlock_ok=readiness["live_unlock_env"],
            gate_training_ok=gate_ok,
            okx_authenticated=bool(okx_check.get("authenticated")),
            execution_mode=mode,
        )
        readiness["live_tester"] = {
            **readiness.get("live_tester", {}),
            "allowed": tester_decision["allowed"],
            "blocked_reasons": tester_decision["reasons"],
            # V6.3 — lifecycle hints refreshed alongside OKX readiness.
            "tester_state": tester_decision.get("tester_state", "disabled"),
            "unlocked": bool(tester_decision.get("unlocked", False)),
            "ready_message": tester_decision.get("ready_message", ""),
            "summary": live_tester.summary(s),
        }
    except Exception as e:
        readiness.setdefault("live_tester", {})["error"] = f"{type(e).__name__}: {e}"
    return readiness


# ---- V6: OKX account snapshot ----
@app.get("/api/okx/account")
async def okx_account() -> dict:
    """Read-only snapshot of the configured OKX account.

    Returns demo/live mode, total/free USDT, non-zero asset summary,
    permissions, and the last error if any. Credentials are returned
    only as a redacted fingerprint — the raw key/secret/passphrase are
    never echoed back.

    V6.1: when the snapshot fails (typically 401 Unauthorized) the
    response also contains a ``diagnostic`` block sourced from
    :pymeth:`OKXPrivateAdapter.diagnose_private_auth` so the UI can
    surface the OKX code/msg and concrete fix steps.
    """
    try:
        snap = await okx_private.get_account_snapshot()
    except Exception as e:
        snap = {
            "configured": False,
            "authenticated": False,
            "mode": "paper",
            "last_error": f"{type(e).__name__}: {e}",
        }
    body: Dict[str, Any] = {"account": snap}
    if snap.get("configured") and not snap.get("authenticated"):
        try:
            body["diagnostic"] = await okx_private.diagnose_private_auth()
        except Exception as e:
            body["diagnostic"] = {
                "private_auth_ok": False,
                "okx_msg": f"{type(e).__name__}: {e}",
                "likely_causes": ["Diagnostic run failed before reaching OKX."],
                "next_steps": ["Inspect server logs for details."],
            }
    return body


@app.get("/api/live/account")
async def live_account_alias() -> dict:
    """Alias of /api/okx/account so the V6 UI can call a stable name."""
    return await okx_account()


# ---- V6.1: OKX auth diagnostics (read-only) ----
@app.get("/api/okx/diagnostics")
async def okx_diagnostics() -> dict:
    """Run a read-only diagnostic against the OKX private auth path.

    Calls ``/api/v5/account/balance`` exactly once and returns the
    parsed OKX code/msg, the local & server timestamps, the demo header
    flag, and a list of likely causes + next steps if the call fails.
    No orders are placed. Secrets are never returned — only redacted
    fingerprints (first 2 + last 2 chars + length).
    """
    try:
        report = await okx_private.diagnose_private_auth()
    except Exception as e:
        report = {
            "private_auth_ok": False,
            "okx_msg": f"{type(e).__name__}: {e}",
            "likely_causes": ["Diagnostic run failed before reaching OKX."],
            "next_steps": ["Inspect server logs for details."],
        }
    return {"diagnostic": report}


# ---- V6: live tester state + controls ----
@app.get("/api/live/tester")
async def live_tester_state() -> dict:
    s = get_settings()
    return {"tester": live_tester.summary(s)}


@app.post("/api/live/tester/kill")
async def live_tester_kill(payload: Dict[str, Any] | None = None) -> dict:
    reason = (payload or {}).get("reason") or "manual"
    state = await live_tester.engage_kill_switch(reason=str(reason)[:120])
    return {"ok": True, "tester": state}


@app.post("/api/live/tester/release")
async def live_tester_release() -> dict:
    state = await live_tester.release_kill_switch()
    return {"ok": True, "tester": state}


@app.post("/api/live/tester/reconcile")
async def live_tester_reconcile(payload: Dict[str, Any]) -> dict:
    pid = (payload or {}).get("position_id") or ""
    if not pid:
        raise HTTPException(status_code=400, detail="position_id required")
    result = await live_tester.reconcile_position(str(pid))
    return result


# ---- V9: native exchange-attached protection controls ----
@app.post("/api/live/tester/native_cancel")
async def live_tester_native_cancel(payload: Dict[str, Any]) -> dict:
    """Cancel native OKX algo orders attached to a live position.

    Does NOT close the position itself — only disarms the exchange-side
    algo legs. Bot-managed exits continue running.
    """
    pid = (payload or {}).get("position_id") or ""
    reason = str((payload or {}).get("reason") or "manual")[:120]
    if not pid:
        raise HTTPException(status_code=400, detail="position_id required")
    result = await live_tester.cancel_native_protection(str(pid), reason=reason)
    return {"ok": bool(result.get("ok")), "result": result, "tester": live_tester.summary(get_settings())}


@app.post("/api/live/tester/native_refresh")
async def live_tester_native_refresh(payload: Dict[str, Any]) -> dict:
    """Re-query OKX for the live algo state and patch the journal."""
    pid = (payload or {}).get("position_id") or ""
    if not pid:
        raise HTTPException(status_code=400, detail="position_id required")
    result = await live_tester.refresh_native_protection(str(pid))
    return {"ok": bool(result.get("ok")), "result": result, "tester": live_tester.summary(get_settings())}


@app.post("/api/live/tester/refresh_inventory")
async def live_tester_refresh_inventory(payload: Dict[str, Any]) -> dict:
    """Probe OKX for free/frozen balance + open sells + open algos.

    Stores the snapshot on the position and returns the latest state.
    Useful for the "why is my balance frozen" UI in the position card.
    """
    pid = (payload or {}).get("position_id") or ""
    if not pid:
        raise HTTPException(status_code=400, detail="position_id required")
    result = await live_tester.refresh_inventory_for_position(str(pid))
    return {"ok": bool(result.get("ok")), "result": result, "tester": live_tester.summary(get_settings())}


@app.post("/api/live/tester/startup_reconcile")
async def live_tester_startup_reconcile() -> dict:
    """Manually trigger the V9 startup-reconcile path.

    Still gated by ``live_native_protection_reconcile_on_startup`` — if
    the flag is false this returns ``{ok: true, skipped: true}`` and
    does NOT touch any positions. Provided for operator use after
    enabling the flag without a process restart.
    """
    s = get_settings()
    result = await live_tester.startup_reconcile(s)
    return {"ok": True, "result": result, "tester": live_tester.summary(s)}


# ---- explicit live-execution lock (legacy compatibility) ----
@app.post("/api/live/execute")
async def live_execute_locked() -> JSONResponse:
    return JSONResponse(
        status_code=423,  # Locked
        content={
            "error": "live_trading_locked",
            "message": (
                "Direct live execution endpoint is locked. This build does not place real "
                "OKX orders automatically. Check /api/live/readiness for the gate state "
                "and the README for the policy."
            ),
        },
    )


# ----------------- SSE -----------------

@app.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    async def event_generator():
        async for chunk in bus.subscribe():
            if await request.is_disconnected():
                break
            yield chunk

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(event_generator(), media_type="text/event-stream", headers=headers)


# ----------------- Static frontend -----------------

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(WEB_DIR / "index.html"))


@app.get("/{path:path}")
async def fallback(path: str) -> FileResponse:
    # Serve other top-level files (favicon, etc.) but only if they exist in /web
    target = WEB_DIR / path
    if target.exists() and target.is_file():
        return FileResponse(str(target))
    raise HTTPException(status_code=404, detail="not found")
