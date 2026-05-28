"""Strategy orchestrator: scans symbols, asks the AI committee, requests paper fills.

V5.1 — Active paper training + auto strategy + dynamic symbol discovery.

Strict rules (unchanged):
  - The AI never places orders. This orchestrator + the PaperEngine risk gates do.
  - If live OKX market data is OFFLINE or data_quality.ok is False, NO entries.
  - Demo/live execution is still gated by the Hermes training gate and env acks
    in main.py — nothing here weakens that gate. Active paper training only
    relaxes the *paper* entry threshold so the bot has something to learn from.
"""
from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Optional, Tuple

from app.core.events import bus
from app.core.settings import RuntimeSettings, get_settings, load_env_flags, STRATEGY_MODES
from app.services.ai_brain import brain
from app.services.indicators import analyze
from app.services.market_data import market
from app.services.paper_engine import engine
from app.services.hermes import hermes
from app.services.live_tester import live_tester
from app.services.okx_private import okx_private
from app.services.symbol_profile import (
    SymbolProfile,
    build_profile,
    preferred_strategy_for_profile,
)
from app.services.scanner_activity import scanner_activity


# Strategies that the auto-selector considers. We deliberately exclude
# "safe_observer" (very selective by design — it would silence training) and
# the "commander_blend" meta strategy (which just picks one of the underlying
# strategies; the auto-selector itself plays that role across symbols).
AUTO_STRATEGY_IDS = (
    "trend_rider",
    "pullback_sniper",
    "breakout_hunter",
    "mean_reversion",
    "volume_surge",
)


def _strategy_name(sid: str) -> str:
    for m in STRATEGY_MODES:
        if m["id"] == sid:
            return m["name"]
    return sid


class StrategyRunner:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._running = False
        self._current_task_text = "idle"
        self._last_scan_ts: float = 0.0
        self._last_decision: dict = {}
        # V5.1 surface state for UI/health
        self._best_watched: dict = {}
        self._auto_selected_strategy: str = ""
        self._auto_selected_reason: str = ""
        self._discovered_symbols: List[str] = []
        self._scanned_universe: List[str] = []
        self._paper_training_active: bool = False
        # V5.2 — per-scan skip tracking (symbols dropped from candidate set
        # because they had stale/missing tickers, insufficient candles, or
        # were in 429 cooldown). Surfaced in UI; never blocks clean symbols.
        self._skipped_symbols: Dict[str, str] = {}
        self._clean_symbols: List[str] = []
        # learning-sample bookkeeping (paper-only fallback)
        self._learning_samples_today: int = 0
        self._learning_samples_day_ts: float = 0.0
        self._last_entry_kind: str = ""  # "standard" | "training" | "learning_sample"
        # V5.4 — symbol profile cache for UI + auto-strategy hints
        self._symbol_profiles: Dict[str, SymbolProfile] = {}
        self._profile_skipped: Dict[str, str] = {}

    @property
    def status(self) -> dict:
        return {
            "running": self._running,
            "current_task": self._current_task_text,
            "last_scan_ts": self._last_scan_ts,
            "last_decision": self._last_decision,
            # V5.1
            "best_watched": self._best_watched,
            "auto_selected_strategy": self._auto_selected_strategy,
            "auto_selected_reason": self._auto_selected_reason,
            "discovered_symbols": list(self._discovered_symbols),
            "scanned_universe": list(self._scanned_universe),
            "scanned_count": len(self._scanned_universe),
            "paper_training_active": self._paper_training_active,
            "learning_samples_today": self._learning_samples_today,
            "last_entry_kind": self._last_entry_kind,
            # V5.2
            "skipped_symbols": dict(self._skipped_symbols),
            "skipped_count": len(self._skipped_symbols),
            "clean_symbols": list(self._clean_symbols),
            "clean_count": len(self._clean_symbols),
            # V5.4
            "symbol_profiles": {sym: p.to_dict() for sym, p in self._symbol_profiles.items()},
            "profile_skipped": dict(self._profile_skipped),
            "profile_skipped_count": len(self._profile_skipped),
        }

    async def start(self) -> None:
        if self._running:
            return
        self._stop.clear()
        self._running = True
        self._task = asyncio.create_task(self._loop())
        await bus.publish("bot_started", self.status)

    async def stop(self) -> None:
        if not self._running:
            return
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except Exception:
                pass
        self._running = False
        self._current_task_text = "stopped"
        await bus.publish("bot_stopped", self.status)

    async def _loop(self) -> None:
        next_full_scan = 0.0
        while not self._stop.is_set():
            try:
                settings = get_settings()
                await self._price_watch_once(settings)
                now = time.time()
                if now >= next_full_scan:
                    await self._scan_once()
                    next_full_scan = time.time() + max(3, min(120, int(settings.scan_interval_seconds)))
            except Exception as e:
                self._current_task_text = f"error: {e}"
                await bus.publish("error", {"where": "strategy._loop", "msg": str(e)})
            settings = get_settings()
            interval = max(1, min(10, int(settings.price_refresh_seconds)))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _price_watch_once(self, settings: RuntimeSettings) -> None:
        # Refresh tickers for the scanned universe (core + discovered) so
        # trailing stops keep updating across the wider watch list.
        watch = self._scanned_universe or list(settings.symbols)
        tickers = await market.refresh_tickers(watch)
        prices = {s: t["last"] for s, t in tickers.items()}
        await engine.tick(prices, settings)
        # V6 — bot-managed live tester exits ride the same tick loop. The
        # live tester only acts on positions it actually opened; if the
        # tester is disabled and no live positions exist this is a no-op.
        try:
            await live_tester.tick(tickers, settings)
        except Exception as e:
            await bus.publish("error", {"where": "live_tester.tick", "msg": str(e)})
        # V9 — watchdog heartbeat. Pulsed every price-watch iteration so the
        # unattended readiness panel can detect a stalled loop or stale data.
        try:
            live_tester.heartbeat(market_data_fresh=bool(tickers))
        except Exception:
            pass
        await bus.publish("portfolio", engine.summary(prices))
        await bus.publish("tickers", tickers)
        await bus.publish("market_status", market.status)
        # Live tester summary changes infrequently but the UI watches it.
        try:
            await bus.publish("live_tester", live_tester.summary(settings))
        except Exception:
            pass

    async def find_best_trade_now(self) -> dict:
        return await self._scan_once(force_consider=True)

    # ---------------- helpers ----------------

    async def _resolve_universe(self, settings: RuntimeSettings) -> List[str]:
        """Combine core symbols with dynamically discovered USDT pairs.

        Discovery only runs in paper mode for safety. okx_demo / okx_live keep
        the curated core list so the operator controls which markets touch the
        exchange.
        """
        core = list(dict.fromkeys([s.upper() for s in settings.symbols if s]))
        if settings.execution_mode != "paper" or not getattr(settings, "dynamic_symbol_discovery", False):
            self._discovered_symbols = []
            self._scanned_universe = core
            return core
        max_dyn = max(0, min(80, int(getattr(settings, "max_dynamic_symbols", 20))))
        # V7 — overall universe ceiling (core + discovered). Default 25.
        max_scan = max(len(core), min(80, int(getattr(settings, "max_scan_symbols", 25))))
        if max_dyn == 0:
            self._discovered_symbols = []
            self._scanned_universe = core[:max_scan]
            return core[:max_scan]
        try:
            picked = await market.discover_universe(
                max_symbols=max_dyn,
                min_quote_vol_usdt=float(getattr(settings, "dynamic_min_quote_volume_usdt", 5_000_000.0)),
                exclude=core,
                exclude_bases=list(getattr(settings, "dynamic_exclude_bases", []) or []),
            )
        except Exception:
            picked = []
        self._discovered_symbols = picked
        universe = list(dict.fromkeys(core + picked))
        # V7 — enforce the overall scan ceiling AFTER discovery so the strategy
        # never feeds more than max_scan_symbols symbols downstream.
        if len(universe) > max_scan:
            universe = universe[:max_scan]
        self._scanned_universe = universe
        return universe

    def _strategy_hermes_bonus(self, strategy_id: str) -> float:
        """Tiny historical bonus so Hermes can nudge auto-selection.

        Returns a value in [-0.05, +0.05]. Strategies with no closed trades
        return 0 (neutral). We intentionally keep this bonus small so the live
        indicator signal still dominates.
        """
        try:
            summary = hermes.summary()
            for row in summary.get("strategy_scores", []):
                if row.get("name") != strategy_id:
                    continue
                trades = int(row.get("trades") or 0)
                if trades < 3:
                    return 0.0
                wr = float(row.get("win_rate") or 0.0)
                # Map win rate 30%-70% to -0.05..+0.05 linearly, capped.
                return max(-0.05, min(0.05, (wr - 0.5) * 0.25))
        except Exception:
            return 0.0
        return 0.0

    def _pick_strategy_for_snapshot(
        self,
        snap: dict,
        settings: RuntimeSettings,
    ) -> Tuple[str, dict, float]:
        """Pick the best strategy for a single symbol's snapshot.

        Returns (strategy_id, candidate_dict, adjusted_score).
        Honours auto_strategy_selection: when False, returns the active strategy's
        candidate as-is.
        """
        cands: Dict[str, dict] = snap.get("strategy_candidates") or {}
        if not cands:
            # Fallback to the selected setup the analyzer already produced.
            return (
                snap.get("strategy_mode", settings.active_strategy),
                {
                    "strategy_id": snap.get("strategy_mode", settings.active_strategy),
                    "setup_type": snap.get("setup_type", "—"),
                    "signal": snap.get("signal"),
                    "confidence": float(snap.get("confidence") or 0.0),
                    "raw_score": float(snap.get("confidence") or 0.0),
                    "reasons": list(snap.get("reasons", [])),
                },
                float(snap.get("confidence") or 0.0),
            )
        if not getattr(settings, "auto_strategy_selection", True):
            active = settings.active_strategy
            cand = cands.get(active) or next(iter(cands.values()))
            return active, cand, float(cand.get("confidence") or 0.0)
        # Auto mode: score each strategy = local confidence + small Hermes bonus
        scored: List[Tuple[str, dict, float]] = []
        for sid in AUTO_STRATEGY_IDS:
            cand = cands.get(sid)
            if not cand:
                continue
            base = float(cand.get("confidence") or 0.0)
            # Only buys get the full base; non-buy candidates get raw_score * 0.5
            # so a near-trigger strategy still surfaces in the "best watched" UI.
            if cand.get("signal") != "buy":
                base = float(cand.get("raw_score") or 0.0) * 0.5
            bonus = self._strategy_hermes_bonus(sid)
            scored.append((sid, cand, round(base + bonus, 4)))
        if not scored:
            return settings.active_strategy, next(iter(cands.values())), 0.0
        scored.sort(key=lambda x: x[2], reverse=True)
        return scored[0]

    # ---------------- V7 observability helper ----------------

    def _compute_blockers(
        self,
        *,
        settings: RuntimeSettings,
        status: str,
        reason: str = "",
        decision: Optional[dict] = None,
        top_candidate: Optional[dict] = None,
    ) -> Dict[str, Dict[str, object]]:
        """Compute the structured "why no live trade?" blocker dict.

        Pure read of existing state. No side effects, no settings mutation.
        Output schema is consumed by /api/scanner and the UI panel.
        """
        try:
            env_flags = load_env_flags()
        except Exception:
            env_flags = None
        # Full-live gate (real-money path) — always locked in this build.
        full_live_reasons: List[str] = []
        if (settings.execution_mode or "paper") != "okx_live":
            full_live_reasons.append("execution_mode is not okx_live")
        import os as _os
        if _os.getenv("LIVE_TRADING_ENABLED", "false").strip().lower() != "true":
            full_live_reasons.append("LIVE_TRADING_ENABLED env not set to true")
        if _os.getenv("LIVE_TRADING_ACK", "").strip() != "I_ACCEPT_REAL_MONEY_RISK":
            full_live_reasons.append("LIVE_TRADING_ACK env not set")
        # Tiny live tester gate
        tester_reasons: List[str] = []
        try:
            exec_mode = settings.execution_mode if settings.execution_mode in ("paper", "okx_demo", "okx_live") else "paper"
            okx_status = okx_private.last_account or {}
            okx_auth = bool(okx_status.get("authenticated"))
            gate_training_ok = bool(hermes.training_gate(settings, require_demo_first=(exec_mode == "okx_live")).get("ok"))
            env_unlock = _os.getenv("LIVE_TRADING_ENABLED", "false").strip().lower() == "true"
            env_ack = _os.getenv("LIVE_TRADING_ACK", "").strip() == "I_ACCEPT_REAL_MONEY_RISK"
            tdec = live_tester.gate_check(
                settings,
                env_flags_ack_ok=env_ack,
                env_flags_unlock_ok=env_unlock,
                gate_training_ok=gate_training_ok,
                okx_authenticated=okx_auth,
                execution_mode=exec_mode,
            )
            tester_state = tdec.get("tester_state", "disabled")
            tester_armed = bool(tdec.get("allowed"))
            if not tester_armed:
                tester_reasons = list(tdec.get("reasons", []))
        except Exception as e:
            tester_state = "unknown"
            tester_armed = False
            tester_reasons = [f"tester gate probe failed: {type(e).__name__}: {e}"]
        # Confidence vs threshold (current best candidate)
        threshold = float(getattr(settings, "confidence_threshold", 0.62))
        try:
            active_training = (settings.execution_mode == "paper") and bool(getattr(settings, "active_paper_training", False))
        except Exception:
            active_training = False
        if active_training:
            threshold = float(getattr(settings, "paper_training_min_score", 0.42))
        cand_conf = 0.0
        if decision:
            cand_conf = float(decision.get("consensus_confidence") or 0.0)
        elif top_candidate:
            cand_conf = float(top_candidate.get("confidence") or 0.0)
        # Cooldown / existing position from paper engine
        try:
            open_syms = [p.symbol for p in engine.state.open_positions]
        except Exception:
            open_syms = []
        # Daily caps
        daily_cap_reasons: List[str] = []
        try:
            if engine.state.trades_today >= int(getattr(settings, "max_trades_per_day", 999)):
                daily_cap_reasons.append(
                    f"paper trades_today {engine.state.trades_today} >= cap {int(getattr(settings, 'max_trades_per_day', 0))}"
                )
        except Exception:
            pass
        # Insufficient balance
        bal_blocked = False
        bal_reason = ""
        try:
            eq_now = engine.equity({})
            min_reserve = float(getattr(settings, "min_cash_reserve_pct", 0.0)) / 100.0 * eq_now
            if eq_now < 5.0:
                bal_blocked = True
                bal_reason = f"paper equity {eq_now:.2f} USDT too low"
            elif engine.state.cash_usdt < min_reserve:
                bal_blocked = True
                bal_reason = f"cash {engine.state.cash_usdt:.2f} below reserve {min_reserve:.2f}"
        except Exception:
            pass
        # No qualified signal? (status flags from caller)
        no_signal = status in ("data_hold", "no_setup", "ai_offline_holding")
        no_signal_reason = reason if no_signal else ""
        blockers = {
            "full_live_gate": {
                "blocked": bool(full_live_reasons),
                "reasons": full_live_reasons,
                "label": "Full live (real money)",
            },
            "tiny_live_tester": {
                "blocked": (not tester_armed),
                "state": tester_state,
                "reasons": tester_reasons,
                "label": "Tiny live tester",
            },
            "no_qualified_signal": {
                "blocked": bool(no_signal),
                "reason": no_signal_reason or ("awaiting next scan" if not no_signal else ""),
                "label": "Qualified signal",
            },
            "existing_position": {
                "blocked": bool(open_syms),
                "symbols": open_syms,
                "label": "Open paper position",
            },
            "confidence_below_threshold": {
                "blocked": bool(cand_conf > 0 and cand_conf < threshold),
                "current": round(cand_conf, 3),
                "required": round(threshold, 3),
                "label": "Confidence vs threshold",
            },
            "cooldown": {
                "blocked": bool(status == "blocked" and ("cooldown" in (reason or "").lower())),
                "reason": reason if (status == "blocked" and "cooldown" in (reason or "").lower()) else "",
                "label": "Cooldown",
            },
            "daily_caps": {
                "blocked": bool(daily_cap_reasons),
                "reasons": daily_cap_reasons,
                "label": "Daily caps",
            },
            "insufficient_balance": {
                "blocked": bool(bal_blocked),
                "reason": bal_reason,
                "label": "Balance / free reserve",
            },
        }
        return blockers

    async def _finalize_scan(
        self,
        *,
        settings: RuntimeSettings,
        status: str,
        reason: str = "",
        decision: Optional[dict] = None,
        top_candidate: Optional[dict] = None,
    ) -> None:
        """V7 — Compute blockers + mark scan finished + publish SSE event."""
        try:
            blockers = self._compute_blockers(
                settings=settings,
                status=status,
                reason=reason,
                decision=decision,
                top_candidate=top_candidate,
            )
            scanner_activity.update_blockers(blockers)
            # Human summary for "why no trade?"
            no_trade_reason = ""
            if status == "opened":
                no_trade_reason = ""
            elif status == "data_hold":
                no_trade_reason = f"DATA HOLD: {reason}"
            elif status == "no_setup":
                no_trade_reason = "No qualified setup yet in clean universe"
            elif status == "ai_offline_holding":
                no_trade_reason = "AI offline and exploration disabled — holding"
            elif status == "blocked":
                no_trade_reason = f"Risk engine blocked: {reason}"
            elif status == "blocked_at_open":
                no_trade_reason = "Risk engine blocked at order placement"
            elif status == "hold":
                no_trade_reason = f"Held — {reason or 'verdict below threshold'}"
            scanner_activity.mark_scan_finished(
                text=self._current_task_text or status,
                no_trade_reason=no_trade_reason,
            )
            await bus.publish("scanner_activity", scanner_activity.snapshot())
        except Exception:
            pass

    # ---------------- the main scan ----------------

    async def _scan_once(self, force_consider: bool = False) -> dict:
        env = load_env_flags()
        settings = get_settings()
        self._last_scan_ts = time.time()

        # Resolve scan universe (core + dynamic discovery)
        universe = await self._resolve_universe(settings)
        # V7 — mark a new scan in the observability layer.
        try:
            scanner_activity.mark_scan_start(
                scanned=list(universe),
                core=list(settings.symbols),
                discovered=list(self._discovered_symbols),
                max_scan_symbols=int(getattr(settings, "max_scan_symbols", 25)),
            )
        except Exception:
            pass
        skipped_preview = len(self._skipped_symbols)
        if len(universe) > len(settings.symbols):
            base = (
                f"scanning {len(universe)} markets (core {len(settings.symbols)} + "
                f"{len(self._discovered_symbols)} discovered)"
            )
        else:
            base = f"scanning {len(universe)} markets"
        # V5.2 — reflect skip count in the task line when present
        if skipped_preview:
            base += f"; {skipped_preview} skipped"
        self._current_task_text = base
        await bus.publish("task", {"text": self._current_task_text})
        await bus.publish("universe", {
            "core": list(settings.symbols),
            "discovered": list(self._discovered_symbols),
            "scanned": list(universe),
            "discovered_meta": market.discovered_meta(),
            # V5.2 — skipped/clean diagnostics for the UI
            "clean": list(self._clean_symbols),
            "skipped": dict(self._skipped_symbols),
            "skipped_count": len(self._skipped_symbols),
            "rate_limited": market.in_rate_limit_cooldown,
        })

        tickers = await market.refresh_all(universe, settings.timeframe, [settings.bias_timeframe])
        prices = {s: t["last"] for s, t in tickers.items()}
        data_quality = market.quality_report(universe, [settings.bias_timeframe, settings.timeframe])
        await hermes.set_data_quality(data_quality)

        await engine.tick(prices, settings)
        await bus.publish("portfolio", engine.summary(prices))
        await bus.publish("tickers", tickers)
        await bus.publish("market_status", market.status)
        await bus.publish("strategy_mode", {"active_strategy": settings.active_strategy})

        # V5.2 — per-symbol triage. quality_report now returns clean_symbols/
        # skipped instead of a yes/no for the whole universe. DATA HOLD only
        # fires if (a) the source is broadly broken, or (b) zero clean symbols
        # remain to trade on.
        clean_symbols: List[str] = list(data_quality.get("clean_symbols") or [])
        skipped_map: Dict[str, str] = dict(data_quality.get("skipped") or {})
        self._clean_symbols = clean_symbols
        self._skipped_symbols = skipped_map
        # V7 — publish data-quality view to the observability layer.
        try:
            scanner_activity.update_data_quality(
                clean_symbols=clean_symbols,
                skipped=skipped_map,
                rate_limited=bool(market.in_rate_limit_cooldown),
                rate_limit_until=float(market.status.get("rate_limit_until") or 0.0),
            )
        except Exception:
            pass

        source_broken = (not market.is_live) or (not data_quality.get("ok"))
        if source_broken or not clean_symbols:
            # Real outage — OKX source is down OR every symbol failed checks.
            issues = "; ".join(data_quality.get("issues", [])[:2])
            if not market.is_live:
                why = issues or market.status.get("last_error") or "OKX public data offline"
            elif market.in_rate_limit_cooldown:
                why = (
                    f"OKX rate-limit cooldown active (~{int(max(0, market.status.get('rate_limit_until', 0) - time.time()))}s left); "
                    f"{len(skipped_map)} symbols skipped"
                )
            else:
                why = f"no clean symbols ({len(skipped_map)} skipped)"
            self._current_task_text = f"DATA HOLD — {why}"
            await bus.publish("task", {"text": self._current_task_text})
            await self._finalize_scan(settings=settings, status="data_hold", reason=why)
            return {
                "status": "data_hold",
                "reason": why,
                "data_quality": data_quality,
                "skipped": skipped_map,
                "clean": clean_symbols,
            }

        # Build snapshots ONLY for clean symbols. Skipped symbols are
        # surfaced but never feed the AI/risk path.
        snapshots: Dict[str, dict] = {}
        for sym in clean_symbols:
            candles = market.get_candles(sym, settings.timeframe)
            if not candles:
                # Belt-and-braces: clean_symbols was supposed to filter this.
                skipped_map[sym] = "candles unavailable post-filter"
                continue
            snap = analyze(candles, settings.active_strategy)
            if not snap:
                skipped_map[sym] = "analyzer returned no snapshot"
                continue
            bias_candles = market.get_candles(sym, settings.bias_timeframe)
            bias_snap = analyze(bias_candles, "trend_rider") if bias_candles else None
            if bias_snap:
                snap["higher_timeframe"] = {
                    "timeframe": settings.bias_timeframe,
                    "bias": bias_snap.get("bias"),
                    "last_close": bias_snap.get("last_close"),
                    "ema21": bias_snap.get("ema21"),
                    "ema50": bias_snap.get("ema50"),
                    "rsi14": bias_snap.get("rsi14"),
                    "reasons": bias_snap.get("reasons", [])[:3],
                }
                if bias_snap.get("bias") == "bearish":
                    snap = dict(snap)
                    snap["signal"] = "hold"
                    snap["confidence"] = 0.0
                    snap["reasons"] = [
                        f"{settings.bias_timeframe} bias bearish — no long entry",
                        *snap.get("reasons", [])[:3],
                    ]
                elif bias_snap.get("signal") != "buy" and snap.get("signal") == "buy":
                    snap = dict(snap)
                    snap["confidence"] = round(float(snap.get("confidence", 0.0)) * 0.82, 3)
                    snap["reasons"] = [
                        f"{settings.bias_timeframe} not fully bullish — waiting for stronger confirmation",
                        *snap.get("reasons", [])[:3],
                    ]
            snapshots[sym] = snap
        await bus.publish("snapshots", snapshots)

        # ---- V5.4: symbol-adaptive profile filter ----
        # Drop any symbol whose live profile says it's stable/pegged/thin/wide-spread
        # BEFORE we hand it to the AI committee or auto-strategy ranking. This is
        # the realism gate the user explicitly asked for.
        self._symbol_profiles = {}
        self._profile_skipped = {}
        symbol_adaptive = bool(getattr(settings, "symbol_adaptive_enabled", True))
        excluded_bases = list(getattr(settings, "paper_excluded_bases", []) or [])
        min_atr = float(getattr(settings, "symbol_min_atr_pct", 0.08))
        max_atr = float(getattr(settings, "symbol_max_atr_pct", 6.0))
        max_spread = float(getattr(settings, "symbol_max_spread_pct", 0.35))
        min_qv = float(getattr(settings, "symbol_min_quote_vol_usdt", 1_000_000.0))
        filtered_snapshots: Dict[str, dict] = {}
        for sym, snap in snapshots.items():
            excludable = False
            try:
                excludable = bool(market.is_excludable_symbol(sym, excluded_bases))
            except Exception:
                excludable = False
            ticker = market.get_ticker(sym) or {}
            profile = build_profile(
                sym,
                ticker,
                snap,
                min_atr_pct=min_atr,
                max_atr_pct=max_atr,
                max_spread_pct=max_spread,
                min_quote_vol_usdt=min_qv,
                excluded_symbol=excludable,
            )
            self._symbol_profiles[sym] = profile
            # If adaptive filter is off, only honour the hard stable/pegged block.
            if not symbol_adaptive:
                if excludable:
                    self._profile_skipped[sym] = "base on stable/pegged blocklist"
                    continue
                filtered_snapshots[sym] = snap
                continue
            if not profile.tradeable:
                self._profile_skipped[sym] = "; ".join(profile.block_reasons[:2]) or "profile blocked"
                continue
            filtered_snapshots[sym] = snap
        # Feed downstream code the filtered set ONLY.
        snapshots = filtered_snapshots
        await bus.publish("symbol_profile", {
            "profiles": {sym: p.to_dict() for sym, p in self._symbol_profiles.items()},
            "skipped": dict(self._profile_skipped),
            "tradeable": [sym for sym, p in self._symbol_profiles.items() if p.tradeable and sym not in self._profile_skipped],
        })

        # ---- V5.4: publish a live market view so the engine can validate exits ----
        # The engine uses this to refuse impossible TP/SL fills (USDG/USDT bug).
        market_view: Dict[str, dict] = {}
        for sym in list(snapshots.keys()) + list(engine.state.open_positions and [p.symbol for p in engine.state.open_positions] or []):
            t = market.get_ticker(sym) or {}
            candles = market.get_candles(sym, settings.timeframe) or []
            last_candle = candles[-1] if candles else None
            view = {
                "last": float(t.get("last") or 0.0),
                "bid": float(t.get("bid") or 0.0),
                "ask": float(t.get("ask") or 0.0),
                "ts": float(t.get("ts") or 0.0),
            }
            if last_candle and len(last_candle) >= 5:
                # OKX candle row layout: [ts, open, high, low, close, ...]
                try:
                    view["candle_high"] = float(last_candle[2])
                    view["candle_low"] = float(last_candle[3])
                    view["candle_close"] = float(last_candle[4])
                except Exception:
                    pass
            market_view[sym] = view
        try:
            engine.set_market_view(market_view)
        except Exception:
            pass

        # Auto-strategy selection — score every symbol's best strategy
        ranked: List[Tuple[str, str, dict, float]] = []  # (symbol, strategy_id, candidate, adjusted_score)
        for sym, snap in snapshots.items():
            sid, cand, score = self._pick_strategy_for_snapshot(snap, settings)
            # V5.4 — soft auto-strategy hint from symbol profile.
            prof = self._symbol_profiles.get(sym)
            if symbol_adaptive and prof and bool(getattr(settings, "auto_strategy_selection", True)):
                hint = preferred_strategy_for_profile(prof, sid)
                if hint and hint != sid:
                    # Re-pick under the profile hint, but only adopt if it scores
                    # at least as well as the original auto-pick.
                    try:
                        sid2, cand2, score2 = self._pick_strategy_for_snapshot(
                            {**snap, "strategy_mode": hint}, settings
                        )
                        if float(score2) >= float(score) * 0.95:
                            sid, cand, score = sid2, cand2, score2
                    except Exception:
                        pass
            # Attach profile summary to candidate reasons (UI/journal)
            if prof:
                cand = dict(cand)
                base_reasons = list(cand.get("reasons") or [])
                base_reasons.insert(0, f"profile: {prof.short_summary()}")
                cand["reasons"] = base_reasons[:6]
                cand["symbol_profile"] = prof.to_dict()
            ranked.append((sym, sid, cand, score))
        ranked.sort(key=lambda x: x[3], reverse=True)

        # V7 — push top candidates + rejection reasons to scanner_activity so
        # the UI can show "top candidates and why rejected".
        try:
            scanner_activity.update_profile_skipped(dict(self._profile_skipped))
            threshold_preview = (
                float(getattr(settings, "paper_training_min_score", 0.42))
                if (settings.execution_mode == "paper" and bool(getattr(settings, "active_paper_training", False)))
                else float(settings.confidence_threshold)
            )
            cand_rows: List[dict] = []
            for sym_, sid_, cand_, score_ in ranked[:10]:
                signal_ = cand_.get("signal")
                conf_ = float(cand_.get("confidence") or 0.0)
                reject_reason = ""
                status_ = "ready"
                if signal_ != "buy":
                    reject_reason = f"no buy trigger (signal={signal_ or '—'})"
                    status_ = "watch"
                elif float(score_) < threshold_preview:
                    reject_reason = (
                        f"score {float(score_):.2f} below threshold {threshold_preview:.2f}"
                    )
                    status_ = "watch"
                cand_rows.append({
                    "symbol": sym_,
                    "strategy": _strategy_name(sid_),
                    "strategy_id": sid_,
                    "score": round(float(score_), 3),
                    "signal": signal_,
                    "confidence": round(conf_, 3),
                    "setup": cand_.get("setup_type", "—"),
                    "reasons": list(cand_.get("reasons") or [])[:3],
                    "status": status_,
                    "rejected_reason": reject_reason,
                })
            scanner_activity.update_candidates(cand_rows)
        except Exception:
            pass

        opportunities = self._build_opportunities(snapshots, ranked)
        await bus.publish("opportunities", opportunities)

        # ---- V5.1 best-watched candidate (always published, even when waiting) ----
        if ranked:
            top_sym, top_sid, top_cand, top_score = ranked[0]
            top_snap_full = snapshots[top_sym]
            self._best_watched = {
                "symbol": top_sym,
                "strategy_id": top_sid,
                "strategy_name": _strategy_name(top_sid),
                "setup_type": top_cand.get("setup_type", "—"),
                "signal": top_cand.get("signal"),
                "score": round(float(top_score), 3),
                "local_confidence": float(top_cand.get("confidence") or 0.0),
                "rsi14": top_snap_full.get("rsi14"),
                "vol_ratio": top_snap_full.get("vol_ratio"),
                "bias": top_snap_full.get("bias"),
                "reasons": list(top_cand.get("reasons", []))[:4],
            }
            self._auto_selected_strategy = top_sid
            self._auto_selected_reason = (
                f"{_strategy_name(top_sid)} scored highest on {top_sym} (score {top_score:.2f})"
            )
        else:
            self._best_watched = {}
            self._auto_selected_strategy = ""
            self._auto_selected_reason = "no symbols produced a usable snapshot"

        await bus.publish("auto_strategy", {
            "auto_strategy_selection": bool(getattr(settings, "auto_strategy_selection", True)),
            "selected_strategy": self._auto_selected_strategy,
            "selected_strategy_name": _strategy_name(self._auto_selected_strategy) if self._auto_selected_strategy else "",
            "reason": self._auto_selected_reason,
            "best_watched": self._best_watched,
        })

        # ---- Threshold logic: V5.1 active paper training ----
        is_paper = settings.execution_mode == "paper"
        active_training = is_paper and bool(getattr(settings, "active_paper_training", False))
        self._paper_training_active = active_training

        if active_training:
            min_score = float(getattr(settings, "paper_training_min_score", 0.42))
            # Sort already by adjusted score; require candidate is a "buy" signal.
            buy_ranked = [r for r in ranked if r[2].get("signal") == "buy" and r[3] >= min_score]
        else:
            # Standard V5 behaviour — only original signal==buy at full confidence.
            buy_ranked = [r for r in ranked if r[2].get("signal") == "buy"]

        # ---- LEARNING-SAMPLE FALLBACK (paper-only, tiny size) ----
        # V5.4 — in live_readiness profile, learning samples are disabled. They
        # would otherwise inflate trade counts and bias the readiness gate.
        paper_profile_setting = str(getattr(settings, "paper_profile", "live_readiness"))
        learning_sample_used = False
        if paper_profile_setting == "live_readiness":
            # Skip the fallback path entirely.
            pass
        elif not buy_ranked and self._learning_sample_eligible(settings, ranked, snapshots, prices, data_quality):
            # Promote the best candidate to a learning sample. It will be
            # explicitly labelled and sized down inside the entry block below.
            top = ranked[0]
            buy_ranked = [top]
            learning_sample_used = True

        # V5.2 — reusable skip suffix for current_task lines
        skip_suffix = ""
        if self._skipped_symbols:
            skip_suffix = f"; skipped {len(self._skipped_symbols)} (rate-limited/insufficient)"

        if not buy_ranked:
            best_label = ""
            if ranked:
                top_sym, top_sid, top_cand, top_score = ranked[0]
                best_label = (
                    f"best watched: {top_sym} · {_strategy_name(top_sid)} · "
                    f"score {top_score:.2f}"
                )
                if active_training:
                    floor = float(getattr(settings, "paper_training_min_score", 0.42))
                    ls_floor = float(getattr(settings, "learning_sample_min_score", 0.30))
                    if top_cand.get("signal") != "buy":
                        best_label += f" (no buy trigger yet; training floor {floor:.2f}, sample floor {ls_floor:.2f})"
                    else:
                        best_label += f" (below training floor {floor:.2f})"
                    ls_reason = self._learning_sample_block_reason(settings, ranked, snapshots, prices, data_quality)
                    if ls_reason:
                        best_label += f"; learning sample blocked: {ls_reason}"
                else:
                    best_label += " (no local buy setup yet)"
            self._current_task_text = (
                ("scanning " + str(len(clean_symbols)) + " clean markets — " + best_label + skip_suffix)
                if best_label
                else ("scanning " + str(len(clean_symbols)) + f" clean markets — no setups yet{skip_suffix}")
            )
            await bus.publish("task", {"text": self._current_task_text})
            await self._finalize_scan(settings=settings, status="no_setup")
            return {
                "status": "no_setup",
                "best_watched": self._best_watched,
                "scanned": len(universe),
                "clean": len(clean_symbols),
                "skipped": len(self._skipped_symbols),
            }

        # We have at least one eligible candidate
        top_sym, top_sid, top_cand, top_score = buy_ranked[0]
        top_snap = snapshots[top_sym]
        # Tell analyzer-derived snapshot to reflect the auto-picked strategy so
        # the AI committee judges the right setup type.
        snap_for_ai = dict(top_snap)
        snap_for_ai["setup_type"] = top_cand.get("setup_type", top_snap.get("setup_type"))
        snap_for_ai["strategy_mode"] = top_sid
        snap_for_ai["reasons"] = list(top_cand.get("reasons") or top_snap.get("reasons", []))
        snap_for_ai["confidence"] = float(top_cand.get("confidence") or 0.0)
        snap_for_ai["signal"] = top_cand.get("signal", "buy")

        equity = engine.equity(prices)
        ok, why = engine.can_enter(settings, equity)

        # Extra paper-training daily cap (in addition to max_trades_per_day)
        if active_training:
            cap = int(getattr(settings, "paper_training_max_daily_trades", 24))
            if engine.state.trades_today >= cap:
                ok, why = False, f"paper training daily cap reached ({cap})"

        ai_available = brain.available(env)

        if not ok and not force_consider:
            self._current_task_text = f"holding {top_sym} — {why}"
            await bus.publish("task", {"text": self._current_task_text})
            await self._finalize_scan(settings=settings, status="blocked", reason=why, top_candidate=top_cand)
            return {"status": "blocked", "reason": why, "best_watched": self._best_watched}

        self._current_task_text = (
            f"AI committee deliberating {top_sym} · {_strategy_name(top_sid)} "
            f"({'training' if active_training else 'standard'}){skip_suffix}"
        )
        await bus.publish("task", {"text": self._current_task_text})

        # V7 — record this as the most recent qualified signal considered
        # (it cleared the buy filter + risk gates and is going to deliberation).
        try:
            scanner_activity.record_qualified_signal({
                "ts": time.time(),
                "symbol": top_sym,
                "strategy_id": top_sid,
                "strategy_name": _strategy_name(top_sid),
                "setup_type": top_cand.get("setup_type", "—"),
                "score": round(float(top_score), 3),
                "confidence": round(float(top_cand.get("confidence") or 0.0), 3),
                "signal": top_cand.get("signal"),
                "reasons": list(top_cand.get("reasons") or [])[:4],
                "mode": "training" if active_training else "standard",
                "price": prices.get(top_sym),
            })
        except Exception:
            pass

        if ai_available:
            try:
                decision = await brain.deliberate(env, settings, top_sym, settings.timeframe, snap_for_ai)
                decision["ai_source"] = "openrouter"
            except Exception as e:
                decision = None
                ai_error = f"{type(e).__name__}: {e}"
                await bus.publish("error", {"where": "brain.deliberate", "msg": ai_error})
        else:
            decision = None

        if decision is None:
            # AI offline or failed. In active paper training (with exploration
            # allowed) we proceed with a clearly-labelled deterministic verdict.
            allow_explore = bool(getattr(settings, "paper_training_allow_exploration", True))
            if not active_training and not settings.indicator_only_mode:
                self._current_task_text = "AI offline — indicator-only mode disabled, holding"
                await bus.publish("task", {"text": self._current_task_text})
                await self._finalize_scan(settings=settings, status="ai_offline_holding", reason="AI offline", top_candidate=top_cand)
                return {"status": "ai_offline_holding", "best_watched": self._best_watched}
            if active_training and not allow_explore:
                self._current_task_text = "AI offline and paper training exploration disabled — holding"
                await bus.publish("task", {"text": self._current_task_text})
                await self._finalize_scan(settings=settings, status="ai_offline_holding", reason="AI offline; exploration disabled", top_candidate=top_cand)
                return {"status": "ai_offline_holding", "best_watched": self._best_watched}
            # Deterministic exploratory verdict. EXPLICITLY labelled — never
            # claim an AI call happened when it did not.
            local_conf = float(snap_for_ai["confidence"])
            min_score = float(getattr(settings, "paper_training_min_score", 0.42)) if active_training else float(settings.confidence_threshold)
            verdict = "proceed" if local_conf >= min_score else "wait"
            decision = {
                "symbol": top_sym,
                "timeframe": settings.timeframe,
                "consensus": verdict,
                "consensus_confidence": local_conf,
                # V8 — explicit direction even in the deterministic fallback.
                "direction_code": "LONG" if verdict == "proceed" else "NO_TRADE",
                "direction_label": (
                    "LONG (spot buy)" if verdict == "proceed" else "NO TRADE"
                ),
                "side": "long",
                "candidate_side": "long",
                "agents": [],
                "snapshot": snap_for_ai,
                "available": False,
                "mode": "deterministic_fallback",
                "ai_source": "deterministic (no OpenRouter call)",
                "note": (
                    "AI committee was not consulted. This verdict is the local "
                    "indicator score only — not a real AI opinion."
                ),
            }
            await bus.publish("ai_verdict", decision)

        self._last_decision = decision
        decision_id = await hermes.record_decision(
            decision=decision,
            settings=settings,
            data_quality=data_quality,
            opportunities=opportunities,
            equity=equity,
            price=prices.get(top_sym, 0.0),
            status="ai_voted" if decision.get("ai_source") == "openrouter" else "deterministic_voted",
            strategy_id=top_sid,
            training_mode="active_paper_training" if active_training else "standard",
        )
        decision["hermes_id"] = decision_id

        # ---- Decide whether to open the paper position ----
        threshold = (
            float(getattr(settings, "paper_training_min_score", 0.42))
            if active_training
            else float(settings.confidence_threshold)
        )
        score_to_check = float(decision.get("consensus_confidence") or 0.0)
        proceed_signal = decision.get("consensus") == "proceed" and score_to_check >= threshold

        # Learning samples override the normal threshold check — they have
        # already passed _learning_sample_eligible() above, which is stricter
        # about safety filters (spread/ATR/liquidity/data freshness).
        if learning_sample_used:
            proceed_signal = True
        if proceed_signal and ok:
            price = prices.get(top_sym)
            if price:
                if learning_sample_used:
                    entry_kind = "learning_sample"
                    training_tag = " [PAPER LEARNING SAMPLE — not live-ready signal]"
                elif active_training:
                    entry_kind = "training"
                    training_tag = " [EXPLORATORY PAPER TRAINING]"
                else:
                    entry_kind = "standard"
                    training_tag = ""
                ai_tag = (
                    "AI committee" if decision.get("ai_source") == "openrouter"
                    else "deterministic-fallback (no AI call)"
                )
                reason = (
                    f"{top_cand.get('setup_type', _strategy_name(top_sid))}{training_tag}. "
                    f"{ai_tag} consensus {decision.get('consensus')} "
                    f"({decision.get('consensus_confidence')}). "
                    + "; ".join(list(top_cand.get("reasons", []))[:3])
                )
                # Position sizing by entry kind
                if entry_kind == "learning_sample":
                    size_scale = max(
                        0.05,
                        min(0.6, float(getattr(settings, "learning_sample_size_pct", 30.0)) / 100.0),
                    )
                elif entry_kind == "training":
                    size_scale = max(0.1, min(1.0, float(getattr(settings, "paper_training_size_pct", 50.0)) / 100.0))
                else:
                    size_scale = 1.0
                eff_settings = settings if size_scale >= 1.0 else _scaled_copy(settings, size_scale)
                self._current_task_text = (
                    f"opening paper long {top_sym} @ {price} "
                    f"({entry_kind}, size×{size_scale:.2f})"
                )
                await bus.publish("task", {"text": self._current_task_text})
                # V5.4 — symbol-adaptive overrides + realistic ticker for fills
                prof = self._symbol_profiles.get(top_sym)
                ticker_now = market.get_ticker(top_sym) or {}
                sl_override = tp_override = trail_override = None
                profile_size_scale = 1.0
                if prof and bool(getattr(settings, "symbol_adaptive_enabled", True)):
                    sl_override = max(0.2, float(eff_settings.stop_loss_pct) * prof.sl_mult)
                    tp_override = max(0.2, float(eff_settings.take_profit_pct) * prof.tp_mult)
                    trail_override = max(0.2, float(eff_settings.trailing_stop_pct) * prof.trail_mult)
                    profile_size_scale = float(prof.size_mult or 1.0)
                pos = await engine.open_long(
                    top_sym, price, eff_settings, prices,
                    reason=reason, decision_id=decision_id,
                    entry_kind=entry_kind,
                    ticker=ticker_now,
                    sl_pct_override=sl_override,
                    tp_pct_override=tp_override,
                    trail_pct_override=trail_override,
                    size_scale=profile_size_scale,
                )
                await bus.publish("portfolio", engine.summary(prices))
                if pos:
                    self._last_entry_kind = entry_kind
                    if entry_kind == "learning_sample":
                        self._bump_learning_sample_counter()
                    await hermes.mark_opened(decision_id, pos)
                    await bus.publish("entry_kind", {
                        "kind": entry_kind,
                        "symbol": top_sym,
                        "strategy_id": top_sid,
                        "strategy_name": _strategy_name(top_sid),
                        "size_scale": size_scale,
                        "label": training_tag.strip(" []") or "standard",
                    })
                    # V6 — attempt a tiny live tester entry mirroring the paper
                    # decision. Only fires if every safety flag is satisfied.
                    try:
                        await self._maybe_attempt_live_entry(
                            settings=settings,
                            symbol=top_sym,
                            paper_position=pos,
                            live_price=price,
                            ticker=ticker_now,
                            data_quality_ok=bool(market.is_live),
                            decision_id=decision_id,
                        )
                    except Exception as e:
                        await bus.publish("error", {"where": "live_tester.attempt_entry", "msg": str(e)})
                    await self._finalize_scan(settings=settings, status="opened", decision=decision, top_candidate=top_cand)
                    return {
                        "status": "opened",
                        "entry_kind": entry_kind,
                        "decision": decision,
                        "training_mode": entry_kind,
                        "strategy_id": top_sid,
                    }
                await hermes.mark_held(decision_id, "risk engine blocked at open")
                await self._finalize_scan(settings=settings, status="blocked_at_open", reason="risk engine blocked at open", decision=decision, top_candidate=top_cand)
                return {"status": "blocked_at_open", "decision": decision}

        # Held
        self._current_task_text = (
            f"hold {top_sym} — {decision.get('consensus')} "
            f"(conf {float(decision.get('consensus_confidence') or 0):.2f}, "
            f"threshold {threshold:.2f}, strategy {_strategy_name(top_sid)}){skip_suffix}"
        )
        await bus.publish("task", {"text": self._current_task_text})
        await hermes.mark_held(decision_id, f"verdict was {decision.get('consensus')} below threshold {threshold:.2f}")
        await self._finalize_scan(settings=settings, status="hold", reason=f"verdict={decision.get('consensus')}; conf={float(decision.get('consensus_confidence') or 0):.2f} vs threshold {threshold:.2f}", decision=decision, top_candidate=top_cand)
        return {"status": "hold", "decision": decision, "best_watched": self._best_watched}

    def _build_opportunities(
        self,
        snapshots: Dict[str, dict],
        ranked: List[Tuple[str, str, dict, float]],
    ) -> List[dict]:
        rows = []
        # ranked already covers the universe and has the auto-selected strategy
        by_symbol = {r[0]: r for r in ranked}
        for sym, snap in snapshots.items():
            r = by_symbol.get(sym)
            cand = r[2] if r else None
            sid = r[1] if r else snap.get("strategy_mode", "—")
            score = float(r[3]) if r else float(snap.get("confidence") or 0.0)
            signal = (cand or {}).get("signal") or snap.get("signal")
            bias = snap.get("bias") or "—"
            if signal == "buy":
                status = "ready"
            elif bias == "bullish":
                status = "watch"
                score = max(score, 0.25)
            else:
                status = "avoid"

            # V8 — explicit direction labelling. Spot-only build:
            #   ready/watch (bullish bias) → LONG (spot buy)
            #   bearish bias               → SHORT signal (not executable in spot-only V8)
            #   anything else              → NO TRADE
            if status in ("ready", "watch"):
                direction_label = "LONG (spot buy)"
                direction_code = "LONG"
            elif bias == "bearish":
                direction_label = "SHORT signal — not executable (spot-only V8)"
                direction_code = "SHORT_UNAVAILABLE"
            else:
                direction_label = "NO TRADE"
                direction_code = "NO_TRADE"

            rows.append({
                "symbol": sym,
                "status": status,
                "score": round(score, 3),
                "setup": (cand or {}).get("setup_type") or snap.get("setup_type", "—"),
                "strategy_id": sid,
                "strategy_name": _strategy_name(sid),
                # V6.4 — every candidate is a LONG (spot buy); shorts are not
                # wired. The UI uses this to label opportunity cards.
                "side": "long",
                "candidate_side": "long",
                "shorts_available": False,
                # V8 — explicit direction labels used by UI everywhere.
                "direction_label": direction_label,
                "direction_code": direction_code,
                "bias": snap.get("bias", "—"),
                "rsi14": snap.get("rsi14"),
                "vol_ratio": snap.get("vol_ratio"),
                "reasons": list((cand or {}).get("reasons") or snap.get("reasons", []))[:3],
                "higher_timeframe": snap.get("higher_timeframe"),
                "discovered": sym in (self._discovered_symbols or []),
            })
        return sorted(rows, key=lambda x: x["score"], reverse=True)


    # ---------------- V6: tiny live tester wiring ----------------

    async def _maybe_attempt_live_entry(
        self,
        *,
        settings: RuntimeSettings,
        symbol: str,
        paper_position,
        live_price: float,
        ticker: dict,
        data_quality_ok: bool,
        decision_id: str,
    ) -> None:
        """Mirror a freshly opened paper position into the OKX live tester.

        Refuses unless every safety flag is satisfied. Surfaces the
        attempt + reason via SSE either way so the UI can show the user
        why nothing fired.
        """
        # Step 1 — must be enabled at all.
        if not bool(getattr(settings, "live_tester_enabled", False)):
            return
        # Step 2 — execution mode must be okx_demo or okx_live.
        exec_mode = settings.execution_mode if settings.execution_mode in ("paper", "okx_demo", "okx_live") else "paper"
        if exec_mode not in ("okx_demo", "okx_live"):
            return
        # Step 3 — OKX must be authenticated. We force a probe but tolerate failures.
        try:
            okx_status = await okx_private.check_readiness(exec_mode)
        except Exception as e:
            okx_status = {"authenticated": False, "reason": f"{type(e).__name__}: {e}"}
        okx_auth = bool(okx_status.get("authenticated"))
        if not okx_auth:
            blocked = {
                "symbol": symbol,
                "reason": "okx not authenticated",
                "detail": okx_status.get("reason", ""),
                "phase": "okx_auth",
                "ok": False,
            }
            await bus.publish("live_tester_blocked", blocked)
            try:
                scanner_activity.record_live_attempt({**blocked, "ts": time.time()})
            except Exception:
                pass
            return
        # Step 4 — env unlock for okx_live.
        import os
        env_unlock = os.getenv("LIVE_TRADING_ENABLED", "false").strip().lower() == "true"
        env_ack = os.getenv("LIVE_TRADING_ACK", "").strip() == "I_ACCEPT_REAL_MONEY_RISK"
        gate_training = bool(hermes.training_gate(settings, require_demo_first=(exec_mode == "okx_live")).get("ok"))
        decision = live_tester.gate_check(
            settings,
            env_flags_ack_ok=env_ack,
            env_flags_unlock_ok=env_unlock,
            gate_training_ok=gate_training,
            okx_authenticated=okx_auth,
            execution_mode=exec_mode,
        )
        if not decision.get("allowed"):
            blocked = {
                "symbol": symbol,
                "reasons": decision.get("reasons", []),
                "phase": "tester_gate",
                "ok": False,
            }
            await bus.publish("live_tester_blocked", blocked)
            try:
                scanner_activity.record_live_attempt({**blocked, "ts": time.time()})
            except Exception:
                pass
            return
        # Step 5 — derive a tiny order size from the paper position. The
        # paper engine sized for the user’s configured risk; we cap to the
        # tester ceiling. Always quote USDT.
        cap = float(settings.live_max_order_usdt_tester)
        paper_notional = float(getattr(paper_position, "entry_price", 0.0)) * float(getattr(paper_position, "qty", 0.0))
        order_usdt = max(1.0, min(cap, paper_notional)) if paper_notional > 0 else max(1.0, min(cap, cap))
        # Step 6 — build protective exit prices from the paper position.
        sl_price = float(getattr(paper_position, "stop_loss", 0.0))
        tp_price = float(getattr(paper_position, "take_profit", 0.0))
        trail_pct = float(getattr(settings, "trailing_stop_pct", 0.0))
        # Step 7 — spread / freshness from ticker.
        bid = float((ticker or {}).get("bid") or 0)
        ask = float((ticker or {}).get("ask") or 0)
        last = float((ticker or {}).get("last") or live_price)
        spread_pct = ((ask - bid) / max(last, 1e-9) * 100) if (bid > 0 and ask > 0 and ask > bid) else 0.0
        ts_ms = float((ticker or {}).get("ts") or 0)
        ticker_age = max(0.0, time.time() - (ts_ms / 1000.0)) if ts_ms else 0.0
        # Free USDT from OKX (already cached on okx_private.last_account).
        snap = okx_private.last_account
        free_usdt = float(snap.get("usdt_available") or 0.0)
        preflight = live_tester.preflight_symbol(
            symbol,
            settings=settings,
            intended_quote_usdt=order_usdt,
            live_price=last or live_price,
            spread_pct=spread_pct,
            ticker_age_seconds=ticker_age,
            free_usdt=free_usdt,
            sl_price=sl_price,
            trail_pct=trail_pct,
            data_quality_ok=bool(data_quality_ok),
        )
        if not preflight.get("allowed"):
            blocked = {
                "symbol": symbol,
                "reasons": preflight.get("reasons", []),
                "phase": "preflight",
                "ok": False,
            }
            await bus.publish("live_tester_blocked", blocked)
            try:
                scanner_activity.record_live_attempt({**blocked, "ts": time.time()})
            except Exception:
                pass
            return
        result = await live_tester.attempt_entry(
            settings=settings,
            symbol=symbol,
            live_price=last or live_price,
            intended_quote_usdt=order_usdt,
            sl_price=sl_price,
            take_profit_price=tp_price,
            trail_pct=trail_pct,
            preflight=preflight,
            decision_id=decision_id,
        )
        await bus.publish("live_tester", live_tester.summary(settings))
        await bus.publish("live_tester_attempt", result)
        try:
            scanner_activity.record_live_attempt({
                **(result or {}),
                "symbol": symbol,
                "ts": time.time(),
                "ok": (result or {}).get("status") == "opened",
            })
        except Exception:
            pass

    # ---------------- learning-sample helpers ----------------

    def _bump_learning_sample_counter(self) -> None:
        now = time.time()
        # Reset daily counter at UTC day boundary (~24h since last bump start)
        if (now - self._learning_samples_day_ts) > 24 * 3600:
            self._learning_samples_today = 0
            self._learning_samples_day_ts = now
        if self._learning_samples_day_ts == 0:
            self._learning_samples_day_ts = now
        self._learning_samples_today += 1

    def _learning_sample_safety_ok(
        self,
        sym: str,
        snap: dict,
        prices: Dict[str, float],
    ) -> Tuple[bool, str]:
        """Strict safety filters for tiny exploratory paper trades.

        These are *additional* to the normal data-quality gate. We refuse if
        spread, ATR, or basic indicator sanity look broken — even though the
        only money at risk is paper.
        """
        t = market.get_ticker(sym)
        if not t:
            return False, "no live ticker"
        last = float(t.get("last") or 0)
        bid = float(t.get("bid") or last)
        ask = float(t.get("ask") or last)
        if last <= 0 or bid <= 0 or ask <= 0:
            return False, "non-positive price/quotes"
        spread_pct = (ask - bid) / max(last, 1e-9) * 100
        if spread_pct > 0.35:
            return False, f"spread too wide ({spread_pct:.2f}%)"
        ts = float(t.get("ts") or 0) / 1000.0
        if ts and (time.time() - ts) > 20:
            return False, f"ticker stale ({int(time.time() - ts)}s)"
        atr = float(snap.get("atr14") or 0)
        if atr <= 0:
            return False, "ATR not computable"
        atr_pct = (atr / max(last, 1e-9)) * 100
        # Avoid completely dead markets and absurdly volatile ones
        if atr_pct < 0.04:
            return False, f"ATR too low ({atr_pct:.2f}%)"
        if atr_pct > 6.0:
            return False, f"ATR too high ({atr_pct:.2f}%)"
        # Bias filter mirrors the regular path
        htf = snap.get("higher_timeframe") or {}
        if htf.get("bias") == "bearish":
            return False, f"{htf.get('timeframe', 'HTF')} bias bearish"
        return True, ""

    def _learning_sample_eligible(
        self,
        settings: RuntimeSettings,
        ranked: List[Tuple[str, str, dict, float]],
        snapshots: Dict[str, dict],
        prices: Dict[str, float],
        data_quality: dict,
    ) -> bool:
        ok, _ = self._learning_sample_eligibility(settings, ranked, snapshots, prices, data_quality)
        return ok

    def _learning_sample_block_reason(
        self,
        settings: RuntimeSettings,
        ranked: List[Tuple[str, str, dict, float]],
        snapshots: Dict[str, dict],
        prices: Dict[str, float],
        data_quality: dict,
    ) -> str:
        ok, why = self._learning_sample_eligibility(settings, ranked, snapshots, prices, data_quality)
        return "" if ok else why

    def _learning_sample_eligibility(
        self,
        settings: RuntimeSettings,
        ranked: List[Tuple[str, str, dict, float]],
        snapshots: Dict[str, dict],
        prices: Dict[str, float],
        data_quality: dict,
    ) -> Tuple[bool, str]:
        # Hard gate — PAPER mode only.
        if settings.execution_mode != "paper":
            return False, "not in paper mode"
        # V5.4 — live_readiness profile refuses learning samples outright.
        if str(getattr(settings, "paper_profile", "live_readiness")) == "live_readiness":
            return False, "paper profile is live_readiness (learning samples disabled)"
        if not bool(getattr(settings, "active_paper_training", False)):
            return False, "active paper training off"
        if not bool(getattr(settings, "learning_sample_enabled", False)):
            return False, "learning samples disabled"
        if not (market.is_live and data_quality.get("ok")):
            return False, "data quality not ok"
        if engine.state.halted_reason:
            return False, f"engine halted: {engine.state.halted_reason}"
        if len(engine.state.open_positions) >= int(settings.max_open_positions):
            return False, "max open positions reached"
        if engine.state.trades_today >= int(settings.max_trades_per_day):
            return False, "daily trade cap reached"
        cap = int(getattr(settings, "learning_sample_max_per_day", 6))
        now = time.time()
        if (now - self._learning_samples_day_ts) > 24 * 3600:
            # rollover — counter will be reset on next bump
            today_count = 0
        else:
            today_count = self._learning_samples_today
        if today_count >= cap:
            return False, f"learning sample daily cap reached ({cap})"
        if not ranked:
            return False, "no candidates"
        top_sym, top_sid, top_cand, top_score = ranked[0]
        min_score = float(getattr(settings, "learning_sample_min_score", 0.30))
        if float(top_score) < min_score:
            return False, f"top score {float(top_score):.2f} < sample floor {min_score:.2f}"
        snap = snapshots.get(top_sym) or {}
        ok, why = self._learning_sample_safety_ok(top_sym, snap, prices)
        if not ok:
            return False, why
        return True, ""


def _scaled_copy(s: RuntimeSettings, scale: float) -> RuntimeSettings:
    """Return a shallow copy of settings with risk and position-size shrunk.

    Used only to size down exploratory paper-training entries. Does NOT mutate
    the persisted settings file.
    """
    from copy import copy
    s2 = copy(s)
    s2.risk_per_trade_pct = max(0.05, float(s.risk_per_trade_pct) * scale)
    s2.max_position_pct = max(1.0, float(s.max_position_pct) * scale)
    return s2


strategy = StrategyRunner()
