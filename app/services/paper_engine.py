"""Paper trading engine.

- All fills are simulated against real OKX public prices.
- Live execution is permanently disabled at the code level.
- Manages open positions, stop-loss, take-profit, trailing stop.
- Enforces daily loss cap, max trades/day, max position size.

V5.4 — REALISTIC PAPER MODE
=============================
This version makes paper results honest enough to be a real proxy for live
readiness. The previous run (V5.3) produced an Equity $102.52 / Cash $0.0000
/ 27 trades/day account in under an hour, which was clearly unrealistic.

Changes vs V5.3:

1. **Cash reserve guard.** ``min_cash_reserve_pct`` (default 35%) is enforced
   inside ``can_enter`` and inside ``open_long`` sizing — opening a position
   that would push cash below the reserve is rejected. There is also a
   ``max_capital_in_positions_pct`` cap on total deployed margin (default 50%).
2. **Trade cooldowns.** Global cooldown between any two entries
   (``global_trade_cooldown_seconds``, default 600s) and per-symbol cooldown
   (``per_symbol_cooldown_seconds``, default 2700s) prevent the over-firing
   loop that produced 27 paper trades/day.
3. **Realistic fills.** Market buys lift the ask + slippage; market sells hit
   the bid − slippage. Fees apply to leveraged notional, not just margin.
4. **Exit verification (CRITICAL FIX).** The V5.3 USDG/USDT trade closed at TP
   without the market ever touching TP. V5.4 refuses to execute an SL / TP /
   trailing exit unless the live ticker AND/OR the most recent fresh candle
   prove the trigger was reached:
     - long TP requires last/high24h/recent_candle_high >= TP
     - long SL requires last/low24h/recent_candle_low <= SL
   Every closed trade carries ``exit_validated`` metadata + trigger source.
5. **Stable/synthetic blocking.** The strategy layer must validate symbols
   before calling open_long, but the engine itself also rejects bases on the
   excluded list as a last line of defense.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.events import bus
from app.core.settings import DATA_DIR, RuntimeSettings, get_settings


STATE_FILE = DATA_DIR / "paper_state.json"


@dataclass
class Position:
    id: str
    symbol: str
    side: str            # "long" — short selling disabled in starter
    qty: float
    entry_price: float
    opened_ts: float
    stop_loss: float
    take_profit: float
    trailing_stop: float
    high_water: float
    fees_paid: float = 0.0
    decision_id: str = ""
    # Leverage simulation (paper/demo only). 1.0 == spot, no margin.
    leverage: float = 1.0
    margin_used: float = 0.0           # cash actually reserved
    notional: float = 0.0              # qty * entry_price
    liquidation_price: float = 0.0     # 0 when spot
    # V5.4 — entry classification carried with the position so closed trades
    # can be filtered (eligible vs learning sample) without joining records.
    entry_kind: str = "standard"       # standard | training | learning_sample
    paper_profile: str = "live_readiness"  # snapshot of profile at entry


@dataclass
class Trade:
    id: str
    symbol: str
    side: str
    qty: float
    entry_price: float
    exit_price: float
    opened_ts: float
    closed_ts: float
    pnl_usdt: float
    fees_paid: float
    exit_reason: str
    decision_id: str = ""
    # V5.4 — exit verification metadata
    entry_kind: str = "standard"
    paper_profile: str = "live_readiness"
    exit_validated: bool = True
    exit_trigger_source: str = ""      # "ticker" | "candle_high" | "candle_low" | "bid" | "ask" | ""
    exit_trigger_price: float = 0.0
    market_last: float = 0.0
    market_bid: float = 0.0
    market_ask: float = 0.0
    candle_high: float = 0.0
    candle_low: float = 0.0


@dataclass
class PaperState:
    starting_balance: float
    cash_usdt: float
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    open_positions: List[Position] = field(default_factory=list)
    trades: List[Trade] = field(default_factory=list)
    day_start_equity: float = 0.0
    day_start_ts: float = 0.0
    trades_today: int = 0
    halted_reason: str = ""
    # V5.4 cooldown state
    last_entry_ts: float = 0.0
    per_symbol_last_entry_ts: Dict[str, float] = field(default_factory=dict)

    def to_json(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_json(cls, j: dict) -> "PaperState":
        # Tolerate older state files that lack leverage / V5.4 fields.
        def _pos(d: dict) -> Position:
            allowed = {f for f in Position.__dataclass_fields__}
            return Position(**{k: v for k, v in d.items() if k in allowed})
        def _trd(d: dict) -> Trade:
            allowed = {f for f in Trade.__dataclass_fields__}
            return Trade(**{k: v for k, v in d.items() if k in allowed})
        positions = [_pos(p) for p in j.get("open_positions", [])]
        trades = [_trd(t) for t in j.get("trades", [])]
        return cls(
            starting_balance=j.get("starting_balance", 10000.0),
            cash_usdt=j.get("cash_usdt", 10000.0),
            realized_pnl=j.get("realized_pnl", 0.0),
            fees_paid=j.get("fees_paid", 0.0),
            open_positions=positions,
            trades=trades,
            day_start_equity=j.get("day_start_equity", 0.0),
            day_start_ts=j.get("day_start_ts", 0.0),
            trades_today=j.get("trades_today", 0),
            halted_reason=j.get("halted_reason", ""),
            last_entry_ts=j.get("last_entry_ts", 0.0),
            per_symbol_last_entry_ts=dict(j.get("per_symbol_last_entry_ts", {}) or {}),
        )


class PaperEngine:
    """Owns the paper account. All real trade locks live here."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.state = self._load_or_init()
        # last verified market snapshot used during exit verification.
        # Strategy layer pushes recent candle highs/lows via ``tick(prices, settings, market_view=...)``.
        self._last_market_view: Dict[str, dict] = {}

    # -------- persistence ---------
    def _load_or_init(self) -> PaperState:
        s = get_settings()
        if STATE_FILE.exists():
            try:
                return PaperState.from_json(json.loads(STATE_FILE.read_text()))
            except Exception:
                pass
        return self._fresh_state(s.starting_balance_usdt)

    def _fresh_state(self, balance: float) -> PaperState:
        return PaperState(
            starting_balance=balance,
            cash_usdt=balance,
            day_start_equity=balance,
            day_start_ts=time.time(),
        )

    def _save(self) -> None:
        STATE_FILE.write_text(json.dumps(self.state.to_json(), indent=2, default=str))

    async def reset(self) -> None:
        async with self._lock:
            s = get_settings()
            self.state = self._fresh_state(s.starting_balance_usdt)
            self._save()
        await bus.publish("paper_reset", self.summary(prices={}))

    # -------- accounting ---------
    def equity(self, prices: Dict[str, float]) -> float:
        # Equity = cash on hand + (margin reserved + mark-to-market PnL) for open positions.
        # For spot (leverage=1) margin_used == notional, so equity reduces to the prior formula.
        eq = self.state.cash_usdt
        for p in self.state.open_positions:
            last = prices.get(p.symbol, p.entry_price)
            lev = max(1.0, float(getattr(p, "leverage", 1.0) or 1.0))
            margin = float(getattr(p, "margin_used", 0.0) or (p.qty * p.entry_price) / lev)
            pnl = (last - p.entry_price) * p.qty
            eq += margin + pnl
        return eq

    def unrealized_pnl(self, prices: Dict[str, float]) -> float:
        u = 0.0
        for p in self.state.open_positions:
            last = prices.get(p.symbol, p.entry_price)
            u += (last - p.entry_price) * p.qty
        return u

    def deployed_margin(self) -> float:
        return sum(float(getattr(p, "margin_used", 0.0) or 0.0) for p in self.state.open_positions)

    def cash_reserve_required(self, settings: RuntimeSettings, equity: float) -> float:
        pct = max(0.0, min(95.0, float(getattr(settings, "min_cash_reserve_pct", 0.0))))
        return equity * (pct / 100.0)

    def cash_reserve_breached(self, settings: RuntimeSettings, prices: Dict[str, float]) -> bool:
        equity = self.equity(prices)
        return self.state.cash_usdt < self.cash_reserve_required(settings, equity) - 1e-6

    def summary(self, prices: Dict[str, float]) -> dict:
        eq = self.equity(prices)
        settings = get_settings()
        reserve_req = self.cash_reserve_required(settings, eq)
        eligible_trades = [t for t in self.state.trades if getattr(t, "entry_kind", "standard") != "learning_sample"]
        return {
            "starting_balance": self.state.starting_balance,
            "cash_usdt": round(self.state.cash_usdt, 4),
            "equity": round(eq, 4),
            "realized_pnl": round(self.state.realized_pnl, 4),
            "unrealized_pnl": round(self.unrealized_pnl(prices), 4),
            "fees_paid": round(self.state.fees_paid, 4),
            "trades_today": self.state.trades_today,
            "open_positions": [asdict(p) for p in self.state.open_positions],
            "halted_reason": self.state.halted_reason,
            "day_start_equity": self.state.day_start_equity,
            "live_trading_locked": True,
            "leverage_active": any((getattr(p, "leverage", 1.0) or 1.0) > 1.0 for p in self.state.open_positions),
            # V5.4 honesty surface
            "paper_profile": getattr(settings, "paper_profile", "live_readiness"),
            "cash_reserve_required": round(reserve_req, 4),
            "cash_reserve_pct": float(getattr(settings, "min_cash_reserve_pct", 0.0)),
            "cash_reserve_breached": self.state.cash_usdt < (reserve_req - 1e-6),
            "deployed_margin": round(self.deployed_margin(), 4),
            "max_capital_in_positions_pct": float(getattr(settings, "max_capital_in_positions_pct", 50.0)),
            "closed_trades_total": len(self.state.trades),
            "closed_trades_eligible": len(eligible_trades),
            "last_entry_ts": self.state.last_entry_ts,
            "exit_verification_enabled": bool(getattr(settings, "exit_verification_enabled", True)),
            "realistic_fills_enabled": bool(getattr(settings, "realistic_fills_enabled", True)),
        }

    def trade_log(self, limit: int = 100) -> List[dict]:
        return [asdict(t) for t in self.state.trades[-limit:][::-1]]

    # -------- daily roll ---------
    def _roll_day_if_needed(self, current_equity: float) -> None:
        # New UTC day → reset trade counter and day-start equity
        now = time.time()
        day_age = now - self.state.day_start_ts
        if day_age > 24 * 3600:
            self.state.day_start_equity = current_equity
            self.state.day_start_ts = now
            self.state.trades_today = 0
            self.state.halted_reason = ""

    # -------- risk gates ---------
    def can_enter(
        self,
        settings: RuntimeSettings,
        equity: float,
        symbol: Optional[str] = None,
        entry_kind: str = "standard",
    ) -> tuple[bool, str]:
        if self.state.halted_reason:
            return False, f"halted: {self.state.halted_reason}"
        if len(self.state.open_positions) >= settings.max_open_positions:
            return False, f"max open positions reached ({settings.max_open_positions})"
        # V5.4 — duplicate-symbol guard (V6 will inherit this for live).
        # User flagged "duplicate entries into same coin" as a top live concern.
        if symbol:
            for _p in self.state.open_positions:
                if getattr(_p, "symbol", "") == symbol:
                    return False, f"{symbol} already has an open position (no pyramiding)"
        if self.state.trades_today >= settings.max_trades_per_day:
            return False, f"max trades/day reached ({settings.max_trades_per_day})"
        # Daily loss cap
        dd_pct = (equity - self.state.day_start_equity) / max(self.state.day_start_equity, 1e-9) * 100
        if dd_pct <= -abs(settings.daily_loss_cap_pct):
            self.state.halted_reason = f"daily loss cap hit ({dd_pct:.2f}%)"
            return False, self.state.halted_reason
        # V5.4 — cash reserve gate
        reserve_req = self.cash_reserve_required(settings, equity)
        if self.state.cash_usdt < reserve_req - 1e-6:
            return False, (
                f"cash reserve breached "
                f"(${self.state.cash_usdt:.2f} < required ${reserve_req:.2f}, "
                f"reserve {settings.min_cash_reserve_pct:.0f}% of equity)"
            )
        # V5.4 — total capital deployed cap
        max_deployed_pct = max(5.0, min(100.0, float(getattr(settings, "max_capital_in_positions_pct", 50.0))))
        max_deployed = equity * (max_deployed_pct / 100.0)
        if self.deployed_margin() >= max_deployed - 1e-6:
            return False, (
                f"max capital in positions reached "
                f"(${self.deployed_margin():.2f}/{max_deployed:.2f}, cap {max_deployed_pct:.0f}%)"
            )
        # V5.4 — cooldowns. Different lengths per profile/kind.
        now = time.time()
        global_cd = float(getattr(settings, "global_trade_cooldown_seconds", 600))
        per_sym_cd = float(getattr(settings, "per_symbol_cooldown_seconds", 2700))
        profile = getattr(settings, "paper_profile", "live_readiness")
        if profile == "learning" or entry_kind in ("training", "learning_sample"):
            # learning profile cuts cooldowns in half (still not zero \u2014 we
            # never want 27 trades/min)
            global_cd *= 0.5
            per_sym_cd *= 0.5
        if global_cd > 0 and self.state.last_entry_ts > 0:
            since = now - self.state.last_entry_ts
            if since < global_cd:
                return False, (
                    f"global cooldown {int(global_cd - since)}s left "
                    f"(every {int(global_cd)}s between entries)"
                )
        if symbol and per_sym_cd > 0:
            last_sym = float(self.state.per_symbol_last_entry_ts.get(symbol, 0.0) or 0.0)
            if last_sym > 0:
                since = now - last_sym
                if since < per_sym_cd:
                    return False, (
                        f"{symbol} cooldown {int(per_sym_cd - since)}s left "
                        f"(every {int(per_sym_cd)}s per symbol)"
                    )
        return True, ""

    def _is_excluded_base(self, settings: RuntimeSettings, symbol: str) -> bool:
        try:
            base = symbol.split("/")[0].upper()
        except Exception:
            return False
        if not base:
            return False
        excluded = set((b or "").upper() for b in getattr(settings, "paper_excluded_bases", []) or [])
        if base in excluded:
            return True
        # Built-in safety net \u2014 USD* / *USD bases always blocked.
        if base.startswith("USD") or base.endswith("USD"):
            return True
        return False

    # -------- execution (paper only) ---------
    async def open_long(
        self,
        symbol: str,
        price: float,
        settings: RuntimeSettings,
        prices: Dict[str, float],
        reason: str = "",
        decision_id: str = "",
        entry_kind: str = "standard",
        ticker: Optional[dict] = None,
        sl_pct_override: Optional[float] = None,
        tp_pct_override: Optional[float] = None,
        trail_pct_override: Optional[float] = None,
        size_scale: float = 1.0,
    ) -> Optional[Position]:
        async with self._lock:
            # V5.4 — last-line stable/synthetic block. Strategy layer should
            # have already filtered, but we refuse here as a safety net.
            if self._is_excluded_base(settings, symbol):
                await bus.publish("entry_blocked", {
                    "symbol": symbol,
                    "why": f"{symbol.split('/')[0]} is on the stable/pegged blocklist",
                })
                return None
            equity = self.equity(prices)
            self._roll_day_if_needed(equity)
            ok, why = self.can_enter(settings, equity, symbol=symbol, entry_kind=entry_kind)
            if not ok:
                await bus.publish("entry_blocked", {"symbol": symbol, "why": why})
                return None
            # ----- leverage policy (paper/demo simulation only) -----
            lev = 1.0
            if bool(getattr(settings, "leverage_enabled", False)):
                lev = max(1.0, float(getattr(settings, "leverage_multiplier", 1.0)))
                lev_cap = max(1.0, float(getattr(settings, "leverage_max_multiplier", 3.0)))
                lev = min(lev, lev_cap)
            # Resolve SL/TP/trailing distances. Allow strategy layer to widen
            # them via overrides (symbol-adaptive). Multipliers are applied to
            # the base percentages, never below 0.2% or above 25%.
            base_sl_pct = float(sl_pct_override if sl_pct_override is not None else settings.stop_loss_pct)
            base_tp_pct = float(tp_pct_override if tp_pct_override is not None else settings.take_profit_pct)
            base_trail_pct = float(trail_pct_override if trail_pct_override is not None else settings.trailing_stop_pct)
            sl_pct = max(0.2, min(25.0, base_sl_pct))
            tp_pct = max(0.2, min(50.0, base_tp_pct))
            trail_pct = max(0.2, min(25.0, base_trail_pct))
            stop = price * (1 - sl_pct / 100)
            risk_per_unit = max(price - stop, 1e-9)
            risk_usdt = equity * settings.risk_per_trade_pct / 100 * max(0.05, min(1.0, size_scale))
            qty_by_risk = risk_usdt / risk_per_unit
            # Notional cap uses leverage; cash cap divides by leverage (margin only).
            notional_cap = (equity * settings.max_position_pct / 100 * max(0.05, min(1.0, size_scale))) * lev
            qty_by_cap = notional_cap / price
            # V5.4 \u2014 cap cash usage so the reserve survives.
            reserve_req = self.cash_reserve_required(settings, equity)
            cash_available = max(0.0, self.state.cash_usdt - reserve_req)
            max_qty_by_cash = (cash_available * lev) / price
            qty = max(0.0, min(qty_by_risk, qty_by_cap, max_qty_by_cash))
            if qty * price < 5:  # nothing meaningful
                await bus.publish("entry_blocked", {"symbol": symbol, "why": "size too small after reserve guard"})
                return None
            # ----- Realistic fill -----
            realistic = bool(getattr(settings, "realistic_fills_enabled", True))
            ask = float((ticker or {}).get("ask") or 0) or price
            bid = float((ticker or {}).get("bid") or 0) or price
            if realistic and ask > 0:
                fill = ask * (1 + settings.slippage_pct / 100)
            else:
                fill = price * (1 + settings.slippage_pct / 100)
            notional = qty * fill
            margin = notional / lev
            # Fees are charged on the FULL notional, not just margin — that's where leverage hurts.
            fee = notional * settings.fee_pct / 100
            if margin + fee > self.state.cash_usdt - reserve_req:
                # Re-solve for the largest qty that fits available cash minus reserve.
                denom = (1.0 / lev) + (settings.fee_pct / 100.0)
                if denom <= 0:
                    qty = 0.0
                else:
                    qty = max(0.0, cash_available / (fill * denom))
                notional = qty * fill
                margin = notional / lev
                fee = notional * settings.fee_pct / 100
            if qty <= 0:
                await bus.publish("entry_blocked", {"symbol": symbol, "why": "insufficient cash after reserve guard"})
                return None
            self.state.cash_usdt -= (margin + fee)
            self.state.fees_paid += fee
            # Liquidation price (simple linear model): loss equals buffer% of margin.
            liq_price = 0.0
            if lev > 1.0:
                buf = float(getattr(settings, "leverage_liquidation_buffer_pct", 80.0)) / 100.0
                liq_price = max(0.0, fill - (buf * margin) / max(qty, 1e-9))
            pos = Position(
                id=str(uuid.uuid4())[:8],
                symbol=symbol,
                side="long",
                qty=qty,
                entry_price=fill,
                opened_ts=time.time(),
                stop_loss=fill * (1 - sl_pct / 100),
                take_profit=fill * (1 + tp_pct / 100),
                trailing_stop=fill * (1 - trail_pct / 100),
                high_water=fill,
                fees_paid=fee,
                decision_id=decision_id,
                leverage=lev,
                margin_used=margin,
                notional=notional,
                liquidation_price=liq_price,
                entry_kind=entry_kind,
                paper_profile=getattr(settings, "paper_profile", "live_readiness"),
            )
            self.state.open_positions.append(pos)
            self.state.trades_today += 1
            now = time.time()
            self.state.last_entry_ts = now
            self.state.per_symbol_last_entry_ts[symbol] = now
            self._save()
        await bus.publish("position_opened", {"position": asdict(pos), "reason": reason})
        return pos

    async def close_position(
        self,
        pos: Position,
        price: float,
        settings: RuntimeSettings,
        reason: str,
        ticker: Optional[dict] = None,
        validation: Optional[dict] = None,
    ) -> Trade:
        async with self._lock:
            realistic = bool(getattr(settings, "realistic_fills_enabled", True))
            bid = float((ticker or {}).get("bid") or 0) or price
            ask = float((ticker or {}).get("ask") or 0) or price
            last = float((ticker or {}).get("last") or 0) or price
            if realistic and bid > 0:
                fill = bid * (1 - settings.slippage_pct / 100)
            else:
                fill = price * (1 - settings.slippage_pct / 100)
            exit_notional = pos.qty * fill
            fee = exit_notional * settings.fee_pct / 100
            pnl_gross = (fill - pos.entry_price) * pos.qty
            # Return margin + PnL — not full notional — because leverage borrowed the rest.
            margin = float(getattr(pos, "margin_used", 0.0) or (pos.qty * pos.entry_price))
            self.state.cash_usdt += (margin + pnl_gross - fee)
            self.state.fees_paid += fee
            pnl = pnl_gross - fee - pos.fees_paid
            self.state.realized_pnl += pnl
            v = validation or {}
            trade = Trade(
                id=pos.id,
                symbol=pos.symbol,
                side=pos.side,
                qty=pos.qty,
                entry_price=pos.entry_price,
                exit_price=fill,
                opened_ts=pos.opened_ts,
                closed_ts=time.time(),
                pnl_usdt=pnl,
                fees_paid=fee + pos.fees_paid,
                exit_reason=reason,
                decision_id=pos.decision_id,
                entry_kind=getattr(pos, "entry_kind", "standard"),
                paper_profile=getattr(pos, "paper_profile", "live_readiness"),
                exit_validated=bool(v.get("validated", True)),
                exit_trigger_source=str(v.get("source", "")),
                exit_trigger_price=float(v.get("trigger_price", 0.0) or 0.0),
                market_last=last,
                market_bid=bid,
                market_ask=ask,
                candle_high=float(v.get("candle_high", 0.0) or 0.0),
                candle_low=float(v.get("candle_low", 0.0) or 0.0),
            )
            self.state.trades.append(trade)
            self.state.open_positions = [p for p in self.state.open_positions if p.id != pos.id]
            self._save()
        await bus.publish("position_closed", {"trade": asdict(trade)})
        try:
            from app.services.hermes import hermes
            await hermes.grade_trade(trade)
        except Exception as e:
            await bus.publish("error", {"where": "hermes.grade_trade", "msg": str(e)})
        return trade

    def set_market_view(self, view: Dict[str, dict]) -> None:
        """V5.4 \u2014 strategy layer hands us a per-symbol view with last/bid/ask
        and the most recent candle high/low so the tick() loop can verify
        exits without re-fetching anything from OKX.
        """
        self._last_market_view = view or {}

    def _verify_exit(
        self,
        pos: Position,
        kind: str,
        trigger: float,
        view: dict,
        verification_enabled: bool,
    ) -> tuple[bool, dict]:
        """Return (validated, metadata).

        kind: 'tp' | 'sl' | 'trail' | 'liq'
        Validation rules (long-only):
          tp/trail-up: requires last>=trigger OR ask>=trigger OR candle_high>=trigger
          sl/trail-down: requires last<=trigger OR bid<=trigger OR candle_low<=trigger
          liq: trusted (uses live last); only paper liquidations.
        If verification is disabled, all exits pass through (legacy mode).
        """
        last = float(view.get("last") or 0.0)
        bid = float(view.get("bid") or last)
        ask = float(view.get("ask") or last)
        candle_high = float(view.get("candle_high") or 0.0)
        candle_low = float(view.get("candle_low") or 0.0)
        meta = {
            "validated": True,
            "source": "",
            "trigger_price": trigger,
            "candle_high": candle_high,
            "candle_low": candle_low,
            "last": last,
            "bid": bid,
            "ask": ask,
            "kind": kind,
        }
        if not verification_enabled or kind == "liq":
            meta["source"] = "ticker"
            return True, meta
        if kind in ("tp",):
            # need price to actually reach trigger from below
            if last and last >= trigger:
                meta["source"] = "ticker_last"
                return True, meta
            if ask and ask >= trigger:
                meta["source"] = "ticker_ask"
                return True, meta
            if candle_high and candle_high >= trigger:
                meta["source"] = "candle_high"
                return True, meta
            meta["validated"] = False
            meta["source"] = "unproven_tp"
            return False, meta
        if kind in ("sl", "trail"):
            if last and last <= trigger:
                meta["source"] = "ticker_last"
                return True, meta
            if bid and bid <= trigger:
                meta["source"] = "ticker_bid"
                return True, meta
            if candle_low and candle_low <= trigger:
                meta["source"] = "candle_low"
                return True, meta
            meta["validated"] = False
            meta["source"] = "unproven_sl"
            return False, meta
        return True, meta

    async def tick(self, prices: Dict[str, float], settings: RuntimeSettings) -> None:
        """Check all open positions for SL/TP/trailing/liquidation exit."""
        verification_enabled = bool(getattr(settings, "exit_verification_enabled", True))
        to_close: list[tuple[Position, float, str, dict, dict]] = []
        for p in list(self.state.open_positions):
            last = prices.get(p.symbol)
            if last is None:
                continue
            view = dict(self._last_market_view.get(p.symbol, {}))
            view.setdefault("last", last)
            ticker = {
                "last": view.get("last", last),
                "bid": view.get("bid", last),
                "ask": view.get("ask", last),
            }
            # Leverage liquidation first \u2014 always trusted (paper-side wipeout).
            liq = float(getattr(p, "liquidation_price", 0.0) or 0.0)
            if liq > 0 and last <= liq:
                ok, meta = self._verify_exit(p, "liq", liq, view, verification_enabled)
                to_close.append((p, last, "liquidated", ticker, meta))
                continue
            # Update trailing stop (only on confirmed upward last)
            if last > p.high_water:
                p.high_water = last
                new_trail = last * (1 - settings.trailing_stop_pct / 100)
                if new_trail > p.trailing_stop:
                    p.trailing_stop = new_trail
            # Check SL first (priority over TP if both crossed in same tick).
            if last <= p.stop_loss:
                ok, meta = self._verify_exit(p, "sl", p.stop_loss, view, verification_enabled)
                if ok:
                    to_close.append((p, last, "stop_loss", ticker, meta))
                else:
                    await bus.publish("exit_blocked", {
                        "symbol": p.symbol, "kind": "sl",
                        "trigger": p.stop_loss, "last": last,
                        "why": "stop_loss touched but bid/last/candle_low did not confirm",
                    })
                continue
            if last <= p.trailing_stop and p.trailing_stop > p.entry_price:
                ok, meta = self._verify_exit(p, "trail", p.trailing_stop, view, verification_enabled)
                if ok:
                    to_close.append((p, last, "trailing_stop", ticker, meta))
                else:
                    await bus.publish("exit_blocked", {
                        "symbol": p.symbol, "kind": "trail",
                        "trigger": p.trailing_stop, "last": last,
                        "why": "trailing stop touched but not confirmed",
                    })
                continue
            if last >= p.take_profit:
                ok, meta = self._verify_exit(p, "tp", p.take_profit, view, verification_enabled)
                if not ok:
                    # V5.4 critical fix — refuse the "impossible TP" close.
                    await bus.publish("exit_blocked", {
                        "symbol": p.symbol, "kind": "tp",
                        "trigger": p.take_profit, "last": last,
                        "why": "TP touched by ticker but not confirmed by candle/bid/ask \u2014 holding position",
                    })
                    continue
                if settings.ride_winners_enabled:
                    old_tp = p.take_profit
                    p.high_water = max(p.high_water, last)
                    p.take_profit = last * (1 + settings.take_profit_pct / 100)
                    new_trail = last * (1 - settings.trailing_stop_pct / 100)
                    if new_trail > p.trailing_stop:
                        p.trailing_stop = new_trail
                    self._save()
                    await bus.publish("position_managed", {
                        "symbol": p.symbol,
                        "last": last,
                        "old_take_profit": old_tp,
                        "new_take_profit": p.take_profit,
                        "trailing_stop": p.trailing_stop,
                        "reason": "take-profit hit; riding winner with trailing stop",
                    })
                else:
                    to_close.append((p, last, "take_profit", ticker, meta))
        for pos, price, why, ticker, meta in to_close:
            await self.close_position(pos, price, settings, why, ticker=ticker, validation=meta)


engine = PaperEngine()
