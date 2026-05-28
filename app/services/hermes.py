"""Hermes learning journal.

Hermes is the paper-learning layer. It does not place orders and it does not
rewrite strategy code. It records every AI decision, links opened paper
positions back to that decision, grades closed trades, and produces a compact
learning summary for the dashboard.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.events import bus
from app.core.settings import DATA_DIR, RuntimeSettings


JOURNAL_FILE = DATA_DIR / "hermes_journal.json"
MAX_RECORDS = 800


def _safe(obj: Any) -> Any:
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, dict):
        return {str(k): _safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_safe(v) for v in obj]
    if isinstance(obj, tuple):
        return [_safe(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


class HermesJournal:
    def __init__(self) -> None:
        self.records: List[dict] = self._load()
        self.last_data_quality: dict = {}

    def _load(self) -> List[dict]:
        if JOURNAL_FILE.exists():
            try:
                payload = json.loads(JOURNAL_FILE.read_text())
                if isinstance(payload, dict):
                    return list(payload.get("records", []))[-MAX_RECORDS:]
                if isinstance(payload, list):
                    return payload[-MAX_RECORDS:]
            except Exception:
                pass
        return []

    def _save(self) -> None:
        JOURNAL_FILE.write_text(json.dumps({"records": self.records[-MAX_RECORDS:]}, indent=2))

    async def reset(self) -> None:
        self.records = []
        self.last_data_quality = {}
        self._save()
        await bus.publish("hermes_updated", self.summary())

    async def set_data_quality(self, report: dict) -> None:
        self.last_data_quality = _safe(report)
        await bus.publish("data_quality", self.last_data_quality)

    async def record_decision(
        self,
        decision: dict,
        settings: RuntimeSettings,
        data_quality: dict,
        opportunities: List[dict],
        equity: float,
        price: float,
        status: str = "considered",
        reason: str = "",
        strategy_id: str | None = None,
        training_mode: str | None = None,
    ) -> str:
        decision_id = str(uuid.uuid4())[:10]
        snapshot = decision.get("snapshot") or {}
        record = {
            "id": decision_id,
            "ts": time.time(),
            "status": status,
            "reason": reason,
            "symbol": decision.get("symbol"),
            "side": "long",
            # V5.1: when auto-strategy selection chooses a different strategy,
            # we record the chosen one so Hermes learns per-strategy not
            # per-manual-toggle.
            "strategy": strategy_id or settings.active_strategy,
            "training_mode": training_mode or ("active_paper_training" if settings.execution_mode == "paper" and getattr(settings, "active_paper_training", False) else "standard"),
            "setup_type": snapshot.get("setup_type"),
            "timeframe": settings.timeframe,
            "bias_timeframe": settings.bias_timeframe,
            "entry_price_seen": price,
            "equity_seen": round(equity, 6),
            "consensus": decision.get("consensus"),
            "consensus_confidence": decision.get("consensus_confidence"),
            "vote_counts": decision.get("vote_counts", {}),
            "agents": [
                {
                    "agent": a.get("agent"),
                    "model": a.get("model"),
                    "vote": a.get("vote"),
                    "confidence": a.get("confidence"),
                    "latency_ms": a.get("latency_ms"),
                    "ok": a.get("ok"),
                    "reason": a.get("reason"),
                    "risk_notes": a.get("risk_notes"),
                }
                for a in decision.get("agents", [])
            ],
            "snapshot": {
                "bias": snapshot.get("bias"),
                "signal": snapshot.get("signal"),
                "confidence": snapshot.get("confidence"),
                "rsi14": snapshot.get("rsi14"),
                "atr14": snapshot.get("atr14"),
                "vol_ratio": snapshot.get("vol_ratio"),
                "higher_timeframe": snapshot.get("higher_timeframe"),
                "reasons": snapshot.get("reasons", [])[:6],
            },
            "data_quality": _safe(data_quality),
            "opportunity_rank": [
                {
                    "symbol": o.get("symbol"),
                    "status": o.get("status"),
                    "score": o.get("score"),
                    "setup": o.get("setup"),
                    "bias": o.get("bias"),
                }
                for o in (opportunities or [])[:8]
            ],
            "position_id": "",
            "opened": None,
            "outcome": None,
            "lesson": "",
            # V5.4 — captured on grade_trade once the position closes
            "entry_kind": "",
            "paper_profile": str(getattr(settings, "paper_profile", "live_readiness")),
            "exit_validated": None,
        }
        self.records.append(record)
        self.records = self.records[-MAX_RECORDS:]
        self._save()
        await bus.publish("hermes_updated", self.summary())
        return decision_id

    async def mark_opened(self, decision_id: str, position: Any) -> None:
        rec = self._find(decision_id)
        if not rec:
            return
        p = _safe(position)
        rec["status"] = "paper_opened"
        rec["position_id"] = p.get("id", "")
        rec["opened"] = {
            "entry_price": p.get("entry_price"),
            "qty": p.get("qty"),
            "stop_loss": p.get("stop_loss"),
            "take_profit": p.get("take_profit"),
            "trailing_stop": p.get("trailing_stop"),
            "fees_paid": p.get("fees_paid"),
            "opened_ts": p.get("opened_ts"),
        }
        self._save()
        await bus.publish("hermes_updated", self.summary())

    async def mark_held(self, decision_id: str, reason: str) -> None:
        rec = self._find(decision_id)
        if not rec:
            return
        rec["status"] = "held"
        rec["reason"] = reason
        self._save()
        await bus.publish("hermes_updated", self.summary())

    async def grade_trade(self, trade: Any) -> None:
        t = _safe(trade)
        rec = self._find_by_trade(t)
        if not rec:
            return
        pnl = float(t.get("pnl_usdt") or 0.0)
        entry = float(t.get("entry_price") or 0.0)
        exit_price = float(t.get("exit_price") or 0.0)
        pnl_pct = ((exit_price - entry) / entry * 100) if entry else 0.0
        exit_reason = str(t.get("exit_reason") or "")
        grade, lesson = self._grade(rec, pnl, pnl_pct, exit_reason)
        rec["status"] = "closed"
        rec["outcome"] = {
            "trade_id": t.get("id"),
            "pnl_usdt": pnl,
            "pnl_pct": pnl_pct,
            "fees_paid": t.get("fees_paid"),
            "exit_reason": exit_reason,
            "closed_ts": t.get("closed_ts"),
            "grade": grade,
            # V5.4 — propagate verification + entry-kind metadata so the
            # readiness gate can exclude unverified / non-standard trades.
            "exit_validated": t.get("exit_validated"),
            "exit_trigger_source": t.get("exit_trigger_source"),
            "exit_trigger_price": t.get("exit_trigger_price"),
            "market_last": t.get("market_last"),
            "market_bid": t.get("market_bid"),
            "market_ask": t.get("market_ask"),
            "candle_high": t.get("candle_high"),
            "candle_low": t.get("candle_low"),
        }
        rec["entry_kind"] = t.get("entry_kind") or rec.get("entry_kind") or "standard"
        rec["paper_profile"] = t.get("paper_profile") or rec.get("paper_profile") or "live_readiness"
        rec["exit_validated"] = t.get("exit_validated")
        rec["lesson"] = lesson
        self._save()
        await bus.publish("hermes_updated", self.summary())

    def _grade(self, rec: dict, pnl: float, pnl_pct: float, exit_reason: str) -> tuple[str, str]:
        snap = rec.get("snapshot", {})
        vol = float(snap.get("vol_ratio") or 0)
        rsi = float(snap.get("rsi14") or 50)
        htf = (snap.get("higher_timeframe") or {}).get("bias", "unknown")
        setup = rec.get("setup_type") or rec.get("strategy") or "setup"

        if pnl > 0:
            if exit_reason == "trailing_stop":
                return "A", f"{setup} worked and runner management protected profit. Keep watching this pattern."
            if exit_reason == "take_profit":
                return "A-", f"{setup} reached target. Good validation of entry timing and TP logic."
            return "B+", f"{setup} closed green. Keep tracking whether this setup repeats."

        if exit_reason == "stop_loss":
            if htf == "bearish":
                return "D", "Loss while higher timeframe was bearish. Hermes should reduce confidence on longs against HTF bias."
            if vol < 0.9:
                return "C-", "Loss with weak volume. Require stronger participation before similar entries."
            if rsi > 70:
                return "C-", "Loss after overheated RSI. Avoid chasing late moves."
            return "C", "Stop hit. Entry may have been early; wait for stronger candle confirmation next time."
        return "C", "Trade closed red. Review whether the setup still matched the selected strategy."

    def _find(self, decision_id: str) -> Optional[dict]:
        for rec in reversed(self.records):
            if rec.get("id") == decision_id:
                return rec
        return None

    def _find_by_trade(self, trade: dict) -> Optional[dict]:
        decision_id = trade.get("decision_id")
        if decision_id:
            rec = self._find(str(decision_id))
            if rec:
                return rec
        trade_id = trade.get("id")
        for rec in reversed(self.records):
            if rec.get("position_id") == trade_id:
                return rec
        return None

    def summary(self) -> dict:
        records = list(self.records)
        decisions = len(records)
        entries = [r for r in records if r.get("opened")]
        closed = [r for r in records if r.get("outcome")]
        wins = [r for r in closed if float((r.get("outcome") or {}).get("pnl_usdt") or 0) > 0]
        losses = [r for r in closed if float((r.get("outcome") or {}).get("pnl_usdt") or 0) <= 0]
        total_pnl = sum(float((r.get("outcome") or {}).get("pnl_usdt") or 0) for r in closed)
        model_scores = self._model_scores(closed)
        strategy_scores = self._group_scores(closed, "strategy")
        setup_scores = self._group_scores(closed, "setup_type")
        symbol_scores = self._group_scores(closed, "symbol")
        lessons = [r.get("lesson") for r in reversed(closed) if r.get("lesson")][:6]
        open_records = [r for r in records if r.get("status") == "paper_opened"]

        # V5.4 — eligibility for live-readiness: only standard trades whose
        # exits were proved reachable by the market view, under the
        # live_readiness profile.
        eligible = [
            r for r in closed
            if (r.get("entry_kind") or "standard") not in ("learning_sample",)
            and r.get("exit_validated") is not False
            and (r.get("paper_profile") or "live_readiness") == "live_readiness"
        ]
        eligible_wins = [r for r in eligible if float((r.get("outcome") or {}).get("pnl_usdt") or 0) > 0]
        eligible_pnl = sum(float((r.get("outcome") or {}).get("pnl_usdt") or 0) for r in eligible)
        unverified = [r for r in closed if r.get("exit_validated") is False]

        return {
            "goal": "Paper-train Hermes until it has enough clean, live-data decisions to identify reliable strategy/model combinations before any live-money build.",
            "safety_rule": "Hermes can learn and recommend in paper mode, but it cannot place live orders or rewrite live risk controls.",
            "decisions": decisions,
            "entries": len(entries),
            "open_entries": len(open_records),
            "closed": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / len(closed)) if closed else 0.0,
            "total_pnl_usdt": round(total_pnl, 6),
            "model_scores": model_scores,
            "strategy_scores": strategy_scores,
            "setup_scores": setup_scores,
            "symbol_scores": symbol_scores,
            "closed_eligible": len(eligible),
            "eligible_wins": len(eligible_wins),
            "eligible_win_rate": (len(eligible_wins) / len(eligible)) if eligible else 0.0,
            "eligible_total_pnl_usdt": round(eligible_pnl, 6),
            "unverified_exits": len(unverified),
            "latest_lessons": lessons,
            "latest_records": list(reversed(records[-10:])),
            "data_quality": self.last_data_quality,
            "recommendation": self._recommendation(closed, model_scores, strategy_scores),
        }

    def training_gate(self, settings: RuntimeSettings, require_demo_first: bool = False) -> dict:
        """Return whether Hermes has enough evidence to unlock exchange execution.

        This is intentionally conservative. It blocks live/demo modes until the
        paper journal has a real sample of closed trades, positive performance,
        and clean data quality.
        """
        s = self.summary()
        dq = s.get("data_quality") or {}
        # V5.4 — readiness uses ONLY eligible trades, never raw closed count.
        eligible = int(s.get("closed_eligible") or 0)
        closed_total = int(s.get("closed") or 0)
        win_rate = float(s.get("eligible_win_rate") or 0.0)
        pnl = float(s.get("eligible_total_pnl_usdt") or 0.0)
        dq_score = int(dq.get("score") or 0)
        unverified = int(s.get("unverified_exits") or 0)
        reasons: list[str] = []
        closed = eligible  # keep variable for downstream uses below
        # Effective minimum closed trades — leverage raises the bar.
        min_closed = int(settings.live_min_closed_trades)
        if bool(getattr(settings, "leverage_enabled", False)):
            min_closed = max(min_closed, int(getattr(settings, "leverage_extra_min_closed_trades", min_closed)))
        if eligible < min_closed:
            reasons.append(
                f"Hermes needs {min_closed} ELIGIBLE closed paper trades "
                f"(standard entries with verified exits, in live_readiness profile); "
                f"currently {eligible} eligible / {closed_total} total"
            )
        if win_rate < float(settings.live_min_win_rate):
            reasons.append(f"Eligible paper win rate must be at least {settings.live_min_win_rate:.0%}; currently {win_rate:.0%}")
        if pnl < float(settings.live_min_total_pnl_usdt):
            reasons.append(f"Eligible paper net P/L must be >= {settings.live_min_total_pnl_usdt:.2f} USDT; currently {pnl:.2f}")
        if unverified > 0:
            reasons.append(
                f"{unverified} paper trade(s) closed with unverified exits (price was not proved reachable); "
                f"these are ignored by the readiness gate — review the trade log"
            )
        if str(getattr(settings, "paper_profile", "live_readiness")) != "live_readiness":
            reasons.append(
                "Switch paper_profile back to 'live_readiness' before unlocking demo/live. "
                "'learning' mode produces samples that do not count toward readiness."
            )
        if dq_score < int(settings.live_min_data_quality_score) or dq.get("ok") is False:
            reasons.append(f"Data quality must be >= {settings.live_min_data_quality_score}/100 and OK; currently {dq_score}/100")
        # Leverage: real-money leveraged execution is not supported in this build.
        if bool(getattr(settings, "leverage_enabled", False)):
            reasons.append(
                "Leverage simulator is paper/demo only — real-money leverage requires a separate perps adapter not in this build"
            )
            # Stricter daily-loss policy when leveraged
            max_daily = float(getattr(settings, "leverage_max_daily_loss_pct", 3.0))
            if float(settings.daily_loss_cap_pct) > max_daily:
                reasons.append(
                    f"Daily loss cap must be <= {max_daily:.1f}% when leverage is enabled; currently {settings.daily_loss_cap_pct:.1f}%"
                )
        if require_demo_first:
            # A later version can split demo vs paper journals. For now this
            # flag communicates the policy in the readiness output.
            reasons.append("Run OKX demo mode successfully before real live mode")
        return {
            "ok": not reasons,
            "blocked_reasons": reasons,
            "closed": closed_total,
            "closed_eligible": eligible,
            "unverified_exits": unverified,
            "win_rate": win_rate,
            "total_pnl_usdt": pnl,
            "data_quality_score": dq_score,
            "paper_profile": str(getattr(settings, "paper_profile", "live_readiness")),
            "leverage_enabled": bool(getattr(settings, "leverage_enabled", False)),
            "leverage_multiplier": float(getattr(settings, "leverage_multiplier", 1.0)),
            "requirements": {
                "min_closed_trades": min_closed,
                "min_win_rate": settings.live_min_win_rate,
                "min_total_pnl_usdt": settings.live_min_total_pnl_usdt,
                "min_data_quality_score": settings.live_min_data_quality_score,
                "leverage_max_daily_loss_pct": float(getattr(settings, "leverage_max_daily_loss_pct", 3.0)),
            },
        }

    def _model_scores(self, closed: List[dict]) -> List[dict]:
        scores: Dict[str, dict] = {}
        for rec in closed:
            pnl = float((rec.get("outcome") or {}).get("pnl_usdt") or 0)
            for a in rec.get("agents", []):
                model = a.get("model") or "unknown"
                vote = a.get("vote")
                key = f"{a.get('agent')} · {model}"
                row = scores.setdefault(key, {
                    "name": key,
                    "model": model,
                    "agent": a.get("agent"),
                    "calls": 0,
                    "good_calls": 0,
                    "bad_calls": 0,
                    "avg_latency_ms": 0.0,
                    "net_pnl_usdt": 0.0,
                })
                row["calls"] += 1
                row["avg_latency_ms"] += float(a.get("latency_ms") or 0)
                row["net_pnl_usdt"] += pnl if vote == "proceed" else 0.0
                good = (vote == "proceed" and pnl > 0) or (vote in ("skip", "wait") and pnl <= 0)
                if good:
                    row["good_calls"] += 1
                else:
                    row["bad_calls"] += 1
        out = []
        for row in scores.values():
            if row["calls"]:
                row["accuracy"] = row["good_calls"] / row["calls"]
                row["avg_latency_ms"] = round(row["avg_latency_ms"] / row["calls"])
                row["net_pnl_usdt"] = round(row["net_pnl_usdt"], 6)
            out.append(row)
        return sorted(out, key=lambda r: (r.get("accuracy", 0), r.get("net_pnl_usdt", 0)), reverse=True)

    def _group_scores(self, closed: List[dict], key: str) -> List[dict]:
        groups: Dict[str, dict] = {}
        for rec in closed:
            name = rec.get(key) or "unknown"
            pnl = float((rec.get("outcome") or {}).get("pnl_usdt") or 0)
            row = groups.setdefault(name, {"name": name, "trades": 0, "wins": 0, "losses": 0, "net_pnl_usdt": 0.0})
            row["trades"] += 1
            row["net_pnl_usdt"] += pnl
            if pnl > 0:
                row["wins"] += 1
            else:
                row["losses"] += 1
        out = []
        for row in groups.values():
            row["win_rate"] = row["wins"] / row["trades"] if row["trades"] else 0.0
            row["net_pnl_usdt"] = round(row["net_pnl_usdt"], 6)
            out.append(row)
        return sorted(out, key=lambda r: (r["net_pnl_usdt"], r["win_rate"]), reverse=True)

    def _recommendation(self, closed: List[dict], model_scores: List[dict], strategy_scores: List[dict]) -> str:
        if len(closed) < 10:
            return "Collect at least 10 closed paper trades before trusting Hermes recommendations. Until then, treat every lesson as early evidence only."
        best_strategy = strategy_scores[0]["name"] if strategy_scores else "unknown"
        best_model = model_scores[0]["name"] if model_scores else "unknown"
        return f"Early edge: strategy {best_strategy}; strongest model/role {best_model}. Keep paper testing before live money."


hermes = HermesJournal()
