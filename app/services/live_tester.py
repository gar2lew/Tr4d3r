"""V6 — Tiny OKX Live Tester.

Real-money spot tester that is **disabled by default** and only places
orders when the operator has set every safety flag explicitly. The class
encapsulates:

- Live state journal (data/live_state.json) — no secrets are ever written.
- Readiness gating and per-attempt preflight checks.
- Cautious spot-buy entry path (USDT-quoted market buy, client order ID,
  fills reconciliation).
- Bot-managed protective exit (SL / trailing stop / TP) executed by
  watching the live ticker; surfaces a clear ``bot_managed_stop`` flag.
- Kill switch and per-day counters.

The user surfaced five specific concerns that V6 must address (see
docs/V6_LIVE_TESTER_DESIGN.md). Each is hooked here:

1. wrong order value      → quote-currency buy, hard cap clamp, preflight match
2. stop-loss not set      → entry refuses to record success until SL price is stored
3. duplicate entries      → one_position_per_symbol guard + OKX base-balance probe
4. compare opportunities  → exit/replace logic is **advisory only** (see strategy)
5. trailing stop actually set → bot-managed loop validates each tick and logs touches

NOTE: OKX spot does not expose a universal native algo-stop endpoint from
a simple REST call in every region. The tester therefore enforces stops
client-side; the UI must clearly say "bot-managed stop — app must stay
running" whenever a live position is open.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.events import bus
from app.core.settings import RuntimeSettings, get_settings
from app.services.okx_private import okx_private, clamp_sell_qty, round_down_to_lot


DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
if not DATA_DIR.exists():
    fallback = Path(__file__).resolve().parents[2] / "data"
    fallback.mkdir(parents=True, exist_ok=True)
    DATA_DIR = fallback
LIVE_STATE_FILE = DATA_DIR / "live_state.json"


@dataclass
class LivePosition:
    """A live tester position, persisted in data/live_state.json."""
    id: str
    client_order_id: str
    okx_order_id: str
    symbol: str
    base_ccy: str
    side: str = "long"                    # spot tester is long-only
    requested_quote_usdt: float = 0.0     # what we asked for
    filled_qty: float = 0.0               # base coins actually received
    avg_entry_price: float = 0.0          # OKX-reported average fill price
    quote_spent_usdt: float = 0.0         # qty * avg_px (post-fee)
    entry_fee_usdt: float = 0.0
    stop_loss_price: float = 0.0
    take_profit_price: float = 0.0
    trailing_stop_pct: float = 0.0
    high_water_price: float = 0.0
    sl_mode: str = "bot_managed"          # always bot_managed in V6
    opened_ts: float = 0.0
    needs_reconcile: bool = False
    reconcile_notes: str = ""
    status: str = "open"                  # open | closing | closed | needs_reconcile | failed
    closed_ts: float = 0.0
    close_order_id: str = ""
    close_client_order_id: str = ""
    close_reason: str = ""
    exit_price: float = 0.0
    realized_pnl_usdt: float = 0.0
    exit_fee_usdt: float = 0.0
    # V9 — OKX native exchange-attached protective sells. Bot-managed
    # exits stay armed in parallel; native is an additional layer.
    # ``status`` values: "none" | "pending" | "active" | "failed"
    #                    | "cancelled" | "triggered" | "dry_run".
    native_protection: Dict[str, Any] = field(default_factory=dict)
    # V9 — last observed OKX inventory snapshot for this symbol
    # (free/frozen base balance + open regular sells + open algos). The
    # tester refreshes this after entry, on demand from the UI, and on
    # every drift reconcile so the operator can see why their balance
    # is frozen even when the bot reports "no native protection".
    okx_inventory: Dict[str, Any] = field(default_factory=dict)
    # V9 — stored entry fee currency (e.g. "DOGE", "USDT"). OKX returns
    # fees in the *received* currency on a market buy (base) and in the
    # *given* currency on a market sell (quote). The previous build
    # rendered the base-asset fee as USD which was misleading.
    entry_fee_ccy: str = ""
    exit_fee_ccy: str = ""
    # V9.1 — net sellable quantity accounting.
    #
    # On a SPOT BUY, OKX deducts the trading fee from the *received*
    # currency (the base asset). The previous build set filled_qty to
    # the gross fillSz and then attempted to sell that gross qty later,
    # which OKX rejected with ``sCode=51008 insufficient balance`` and
    # wrapped that in the unhelpful ``code=1 All operations failed``.
    #
    # ``gross_filled_qty`` is the raw OKX fillSz (untouched for PnL).
    # ``sellable_qty`` is what the bot may safely sell:
    #     sellable_qty = gross - (fee_amount if fee_ccy == base_ccy else 0)
    # and may be further clamped downward to OKX free-balance / lotSz
    # before every exit attempt (see clamp_sell_qty()).
    # ``fee_deducted_from_base`` mirrors entry_fee_usdt but is the qty
    # in base ccy (e.g. 0.6867 DOGE), purely for UI display.
    gross_filled_qty: float = 0.0
    sellable_qty: float = 0.0
    fee_deducted_from_base: float = 0.0
    # Optional override populated by startup_reconcile when the OKX-side
    # free+frozen balance does not cover sellable_qty (e.g. partial
    # withdrawal, manual sell outside the bot). When set, the live tester
    # uses this for exits instead of ``sellable_qty``. PnL still uses
    # ``filled_qty * avg_entry_price`` so cost basis stays intact.
    inventory_sellable_qty: float = 0.0
    inventory_repair_note: str = ""


@dataclass
class LiveState:
    """Live tester journal."""
    # V9 bumps the schema marker so a future migration knows the
    # journal may contain native_protection blocks.
    schema: str = "v9.1.tester.live_state.1"
    positions: List[LivePosition] = field(default_factory=list)
    attempts: List[dict] = field(default_factory=list)       # successful + failed entries
    closes: List[dict] = field(default_factory=list)         # exit attempts (success/fail)
    trades_today: int = 0
    day_start_ts: float = 0.0
    realized_pnl_today: float = 0.0
    kill_switch: bool = False
    kill_switch_reason: str = ""
    last_attempt_ts: float = 0.0
    last_error: str = ""
    # V9 — watchdog / heartbeat. ``last_heartbeat_ts`` is bumped by the
    # strategy loop every time it ticks the tester; ``last_market_data_ts``
    # is bumped when fresh tickers arrive. Both are used by the readiness
    # panel to detect a stalled loop or stale data.
    last_heartbeat_ts: float = 0.0
    last_market_data_ts: float = 0.0
    # V9 — consecutive OKX-private API errors. Reset on any success.
    # When this exceeds a threshold the unattended gate refuses new
    # entries and surfaces a warning to the operator.
    consecutive_api_errors: int = 0
    last_api_error: str = ""
    # V9 — unattended mode bookkeeping. ``unattended_started_at`` is
    # the epoch second the operator armed unattended mode; ``expired``
    # latches true when max_hours elapsed.
    unattended_started_at: float = 0.0
    unattended_expired: bool = False
    unattended_expired_at: float = 0.0
    # Persisted event log (small; capped at 200 entries) so the UI can
    # show key autonomous-action events even on a fresh page load.
    events: List[dict] = field(default_factory=list)


def _utc_day_start(ts: float) -> float:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _base_of(symbol: str) -> str:
    return symbol.split("/")[0].upper()


def _quote_of(symbol: str) -> str:
    parts = symbol.split("/")
    return parts[1].upper() if len(parts) > 1 else "USDT"


class LiveTester:
    """Owns the live-tester state and the entry/exit operations.

    All public mutators are protected by an asyncio.Lock so a single
    OKX round trip is never racing another tester operation. The lock
    is short — it does not span a price-watch loop.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._state: LiveState = self._load_or_init()

    # ----- persistence -----

    def _load_or_init(self) -> LiveState:
        if LIVE_STATE_FILE.exists():
            try:
                j = json.loads(LIVE_STATE_FILE.read_text())
                positions = [LivePosition(**{k: v for k, v in p.items() if k in LivePosition.__dataclass_fields__})
                             for p in j.get("positions", [])]
                return LiveState(
                    schema=j.get("schema", "v9.1.tester.live_state.1"),
                    positions=positions,
                    attempts=list(j.get("attempts", []))[-200:],
                    closes=list(j.get("closes", []))[-200:],
                    trades_today=int(j.get("trades_today", 0)),
                    day_start_ts=float(j.get("day_start_ts", 0.0)),
                    realized_pnl_today=float(j.get("realized_pnl_today", 0.0)),
                    kill_switch=bool(j.get("kill_switch", False)),
                    kill_switch_reason=str(j.get("kill_switch_reason", "")),
                    last_attempt_ts=float(j.get("last_attempt_ts", 0.0)),
                    last_error=str(j.get("last_error", "")),
                    last_heartbeat_ts=float(j.get("last_heartbeat_ts", 0.0)),
                    last_market_data_ts=float(j.get("last_market_data_ts", 0.0)),
                    consecutive_api_errors=int(j.get("consecutive_api_errors", 0)),
                    last_api_error=str(j.get("last_api_error", "")),
                    unattended_started_at=float(j.get("unattended_started_at", 0.0)),
                    unattended_expired=bool(j.get("unattended_expired", False)),
                    unattended_expired_at=float(j.get("unattended_expired_at", 0.0)),
                    events=list(j.get("events", []))[-200:],
                )
            except Exception:
                pass
        return LiveState()

    def _persist(self) -> None:
        try:
            tmp = LIVE_STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "schema": self._state.schema,
                "positions": [asdict(p) for p in self._state.positions],
                "attempts": list(self._state.attempts)[-200:],
                "closes": list(self._state.closes)[-200:],
                "trades_today": self._state.trades_today,
                "day_start_ts": self._state.day_start_ts,
                "realized_pnl_today": self._state.realized_pnl_today,
                "kill_switch": self._state.kill_switch,
                "kill_switch_reason": self._state.kill_switch_reason,
                "last_attempt_ts": self._state.last_attempt_ts,
                "last_error": self._state.last_error,
                "last_heartbeat_ts": self._state.last_heartbeat_ts,
                "last_market_data_ts": self._state.last_market_data_ts,
                "consecutive_api_errors": self._state.consecutive_api_errors,
                "last_api_error": self._state.last_api_error,
                "unattended_started_at": self._state.unattended_started_at,
                "unattended_expired": self._state.unattended_expired,
                "unattended_expired_at": self._state.unattended_expired_at,
                "events": list(self._state.events)[-200:],
            }, indent=2))
            tmp.replace(LIVE_STATE_FILE)
        except Exception:
            pass

    def _maybe_rollover_day(self, now: float) -> None:
        today = _utc_day_start(now)
        if self._state.day_start_ts < today:
            self._state.day_start_ts = today
            self._state.trades_today = 0
            self._state.realized_pnl_today = 0.0

    # ----- public state views (do not require lock for read) -----

    def positions(self) -> List[LivePosition]:
        return [LivePosition(**asdict(p)) for p in self._state.positions]

    def open_positions(self) -> List[LivePosition]:
        return [p for p in self.positions() if p.status in ("open", "needs_reconcile", "closing")]

    def find_position_by_symbol(self, symbol: str) -> Optional[LivePosition]:
        for p in self._state.positions:
            if p.symbol == symbol and p.status in ("open", "needs_reconcile", "closing"):
                return p
        return None

    def summary(self, settings: Optional[RuntimeSettings] = None) -> dict:
        s = settings or get_settings()
        now = time.time()
        self._maybe_rollover_day(now)
        # V9 — auto-disable on unattended timer expiry. We check this at
        # every summary() call so the UI cannot lag the auto-shutoff. The
        # actual shutoff side-effects (clearing live_tester_enabled) are
        # applied lazily so the summary response stays a pure-read; the
        # gate_check() path then surfaces the expired reason.
        self._maybe_expire_unattended(now, s)
        open_pos = self.open_positions()
        # V9 — dynamic stop_mode reflecting the operator's configuration.
        # Important: the bot-managed watcher is ALWAYS armed in this build;
        # native protection is additive, never a replacement. So when
        # native is enabled we report the combined mode so the UI cannot
        # mistakenly claim native is the only safety net.
        np_enabled = bool(getattr(s, "live_native_protection_enabled", False))
        np_mode = str(getattr(s, "live_native_protection_mode", "oco") or "oco").lower()
        np_dry = bool(getattr(s, "live_native_protection_dry_run", False))
        if np_enabled and np_mode != "off" and not np_dry:
            stop_mode = "exchange_native + bot_fallback"
        elif np_enabled and np_dry:
            stop_mode = "bot_managed (native dry-run)"
        else:
            stop_mode = "bot_managed"
        return {
            "schema": self._state.schema,
            "enabled": bool(s.live_tester_enabled),
            "kill_switch": bool(self._state.kill_switch),
            "kill_switch_reason": self._state.kill_switch_reason,
            "max_order_usdt": float(s.live_max_order_usdt_tester),
            "daily_loss_cap_usdt": float(s.live_daily_loss_cap_usdt),
            "max_trades_per_day": int(s.live_max_trades_per_day),
            "trades_today": int(self._state.trades_today),
            "realized_pnl_today": float(self._state.realized_pnl_today),
            "one_position_per_symbol": bool(s.live_one_position_per_symbol),
            "max_open_positions": int(s.live_max_open_positions),
            "require_protective_exit": bool(s.live_require_protective_exit),
            "spot_only": bool(s.live_spot_only),
            "open_positions": [asdict(p) for p in open_pos],
            "open_position_count": len(open_pos),
            "stop_mode": stop_mode,
            "stop_warning": (
                "Stops are managed by this app while it is running. "
                "If the process exits, stops will NOT trigger. "
                "Set tiny order sizes and keep an exchange-side mental stop."
            ),
            # V9 — native exchange protection telemetry. Always reports
            # the *settings* state plus the bot-managed-stays-armed truth
            # so the UI cannot accidentally claim bot-managed is OFF.
            "native_protection_enabled": bool(getattr(s, "live_native_protection_enabled", False)),
            "native_protection_mode": str(getattr(s, "live_native_protection_mode", "oco")),
            "native_protection_dry_run": bool(getattr(s, "live_native_protection_dry_run", False)),
            "native_protection_warning": (
                "OKX native TP/SL covers ONLY the configured triggers (OCO or conditional). "
                "Bot-managed trailing stop and all other exits remain active in parallel. "
                "If native placement fails, the bot-managed watcher is your only protection — "
                "do not exit the process while a live position is open."
            ),
            "attempts": list(self._state.attempts)[-30:],
            "closes": list(self._state.closes)[-30:],
            "last_attempt_ts": self._state.last_attempt_ts,
            "last_error": self._state.last_error,
            # V6.3 — boolean only, never reveal the raw token value.
            "override_active": bool(s.live_tester_override),
            # V9 — watchdog telemetry + unattended state + readiness gate.
            "watchdog": {
                "last_heartbeat_ts": float(self._state.last_heartbeat_ts),
                "last_market_data_ts": float(self._state.last_market_data_ts),
                "heartbeat_age_seconds": (now - self._state.last_heartbeat_ts) if self._state.last_heartbeat_ts else None,
                "market_data_age_seconds": (now - self._state.last_market_data_ts) if self._state.last_market_data_ts else None,
                "consecutive_api_errors": int(self._state.consecutive_api_errors),
                "last_api_error": str(self._state.last_api_error),
            },
            "unattended": {
                "enabled": bool(getattr(s, "live_unattended_mode", False)),
                "max_hours": float(getattr(s, "live_unattended_max_hours", 0.0)),
                "started_at": float(self._state.unattended_started_at or getattr(s, "live_unattended_started_at", 0.0) or 0.0),
                "expires_at": self._unattended_expires_at(s),
                "expired": bool(self._state.unattended_expired),
                "expired_at": float(self._state.unattended_expired_at),
                "stop_new_on_failure": bool(getattr(s, "live_unattended_stop_new_on_failure", True)),
            },
            "unattended_readiness": self._unattended_readiness(s, open_pos),
            "events": list(self._state.events)[-50:],
        }

    # ----- V9: unattended mode helpers -----

    def _unattended_started(self, s: RuntimeSettings) -> float:
        """Resolve the canonical "unattended started" epoch.

        Persisted state wins (so we survive restarts), falling back to
        the env-derived value.  When the operator first toggles
        ``live_unattended_mode`` true via the settings drawer we lazily
        populate the persisted value in ``_maybe_expire_unattended``.
        """
        if self._state.unattended_started_at > 0:
            return float(self._state.unattended_started_at)
        return float(getattr(s, "live_unattended_started_at", 0.0) or 0.0)

    def _unattended_expires_at(self, s: RuntimeSettings) -> float:
        started = self._unattended_started(s)
        if started <= 0:
            return 0.0
        return started + float(getattr(s, "live_unattended_max_hours", 0.0) or 0.0) * 3600.0

    def _maybe_expire_unattended(self, now: float, s: RuntimeSettings) -> None:
        """Latch unattended_expired when the timer elapses.

        We only act if the operator opted into unattended mode. If the
        persisted ``unattended_started_at`` is zero and the env is
        unset, we stamp it now — first sight of unattended mode counts
        as the start of the window. This keeps the math honest even if
        the operator forgot to set ``LIVE_UNATTENDED_STARTED_AT``.
        """
        if not bool(getattr(s, "live_unattended_mode", False)):
            # Reset latch when unattended is turned off so re-arming
            # gives the operator a clean window without manual surgery.
            if self._state.unattended_started_at != 0 or self._state.unattended_expired:
                self._state.unattended_started_at = 0.0
                self._state.unattended_expired = False
                self._state.unattended_expired_at = 0.0
                self._persist()
            return
        # First time we see unattended mode armed — stamp the journal.
        if self._state.unattended_started_at <= 0:
            env_started = float(getattr(s, "live_unattended_started_at", 0.0) or 0.0)
            self._state.unattended_started_at = env_started if env_started > 0 else now
            self._append_event(
                "unattended_armed",
                f"unattended mode armed for up to {getattr(s, 'live_unattended_max_hours', 0):.1f}h",
            )
            self._persist()
        if self._state.unattended_expired:
            return
        expires_at = self._unattended_expires_at(s)
        if expires_at > 0 and now >= expires_at:
            self._state.unattended_expired = True
            self._state.unattended_expired_at = now
            self._state.kill_switch = True
            self._state.kill_switch_reason = (
                f"unattended timer expired after {getattr(s, 'live_unattended_max_hours', 0):.1f}h — "
                f"new entries blocked; review open positions manually"
            )
            self._state.last_error = self._state.kill_switch_reason
            self._append_event("unattended_expired", self._state.kill_switch_reason)
            self._persist()
            # Best-effort event-bus publish. ``bus.publish`` is async; we
            # can't await from a sync method, so we fire-and-forget via
            # asyncio if a loop is running.
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(bus.publish("live_unattended_expired", {
                        "reason": self._state.kill_switch_reason,
                        "expired_at": now,
                    }))
            except Exception:
                pass

    def _unattended_readiness(self, s: RuntimeSettings, open_pos: List[LivePosition]) -> dict:
        """Compute PASS/FAIL checks for the "Safe-ish for unattended" gate.

        Returns a stable shape the UI can iterate. ``overall_pass`` is
        true only when every required check passes. The UI must surface
        the unhappy checks so the operator knows what to fix — we never
        hide a failure.

        IMPORTANT: passing this gate does NOT promise safety. Crypto
        markets can gap through a stop, OKX algos can fail, the bot
        process can die. This is a risk-reduced tiny live test.
        """
        now = time.time()
        np_enabled = bool(getattr(s, "live_native_protection_enabled", False))
        np_mode = str(getattr(s, "live_native_protection_mode", "oco") or "oco").lower()
        np_dry = bool(getattr(s, "live_native_protection_dry_run", False))
        max_order = float(getattr(s, "live_max_order_usdt_tester", 5.0))
        daily_cap = float(getattr(s, "live_daily_loss_cap_usdt", 3.0))
        max_open = int(getattr(s, "live_max_open_positions", 1))
        max_trades = int(getattr(s, "live_max_trades_per_day", 3))
        hb_age = (now - self._state.last_heartbeat_ts) if self._state.last_heartbeat_ts else None
        md_age = (now - self._state.last_market_data_ts) if self._state.last_market_data_ts else None
        # Per-position native verification.
        per_pos: List[dict] = []
        any_native_missing = False
        for p in open_pos:
            np_state = (p.native_protection or {})
            status = (np_state.get("status") or "none").lower()
            verified = status == "active"
            if not verified:
                any_native_missing = True
            per_pos.append({
                "position_id": p.id,
                "symbol": p.symbol,
                "native_status": status,
                "verified": verified,
                "last_error_code": np_state.get("last_error_code", ""),
                "last_error_msg": np_state.get("last_error_msg", ""),
            })
        # Build the checks list. Each check is required for overall_pass.
        checks = [
            {
                "id": "native_enabled",
                "label": "OKX native TP/SL enabled (not dry-run)",
                "pass": np_enabled and np_mode != "off" and not np_dry,
                "detail": (
                    f"enabled={np_enabled} mode={np_mode} dry_run={np_dry}"
                ),
            },
            {
                "id": "native_verified_for_open",
                "label": "Native protection verified for every open position",
                "pass": (not open_pos) or (np_enabled and not any_native_missing),
                "detail": (
                    "no open positions" if not open_pos else
                    ("all open positions report native_protection.status=active" if not any_native_missing
                     else "one or more open positions are missing exchange-side native protection")
                ),
                "per_position": per_pos,
            },
            {
                "id": "caps_tiny",
                "label": "Caps within tiny-test envelope (order ≤ 5, daily loss ≤ 3)",
                "pass": max_order <= 5.0 + 1e-6 and daily_cap <= 3.0 + 1e-6,
                "detail": f"max_order_usdt={max_order:.2f} daily_loss_cap_usdt={daily_cap:.2f}",
            },
            {
                "id": "max_open_one",
                "label": "Max open positions = 1 (recommended)",
                "pass": max_open <= 1,
                "detail": f"max_open_positions={max_open}",
            },
            {
                "id": "max_trades_small",
                "label": "Max trades per day ≤ 3 (recommended)",
                "pass": max_trades <= 3,
                "detail": f"max_trades_per_day={max_trades}",
            },
            {
                "id": "kill_switch_off",
                "label": "Kill switch OFF",
                "pass": not self._state.kill_switch,
                "detail": self._state.kill_switch_reason or "clear",
            },
            {
                "id": "watchdog_alive",
                "label": "Watchdog alive (heartbeat < 120s)",
                "pass": (hb_age is not None and hb_age <= 120.0),
                "detail": (
                    f"heartbeat_age_seconds={hb_age:.1f}" if hb_age is not None else "no heartbeat yet"
                ),
            },
            {
                "id": "market_data_fresh",
                "label": "Market data fresh (< 60s)",
                "pass": (md_age is not None and md_age <= 60.0),
                "detail": (
                    f"market_data_age_seconds={md_age:.1f}" if md_age is not None else "no market tick yet"
                ),
            },
            {
                "id": "no_api_error_burst",
                "label": "No OKX API error burst (< 5 consecutive)",
                "pass": int(self._state.consecutive_api_errors) < 5,
                "detail": (
                    f"consecutive_api_errors={self._state.consecutive_api_errors}"
                    + (f" last={self._state.last_api_error}" if self._state.last_api_error else "")
                ),
            },
            {
                "id": "unattended_not_expired",
                "label": "Unattended timer not expired",
                "pass": not bool(self._state.unattended_expired),
                "detail": (
                    "expired" if self._state.unattended_expired else "timer ok"
                ),
            },
            {
                "id": "daily_loss_under_cap",
                "label": "Daily realized loss under cap",
                "pass": -float(self._state.realized_pnl_today) < float(daily_cap),
                "detail": f"realized_today={self._state.realized_pnl_today:.2f} cap={daily_cap:.2f}",
            },
        ]
        overall_pass = all(c["pass"] for c in checks)
        return {
            "overall_pass": overall_pass,
            "banner": (
                "SAFE-ish for unattended tiny test" if overall_pass
                else "DO NOT LEAVE UNATTENDED"
            ),
            "checks": checks,
            "note": (
                "This is a risk-reduced tiny live test envelope, NOT safe automation. "
                "Crypto markets can gap through a stop. Exchange-side algos can fail. "
                "This bot or your network can die. Tiny caps + native protection + "
                "watching the daily realized P/L are the only things keeping risk small."
            ),
        }

    # ----- V9: heartbeat / API error bookkeeping (called from strategy loop) -----

    def heartbeat(self, *, market_data_fresh: bool = True) -> None:
        """Bump the watchdog heartbeat. Safe to call from any async path.

        The strategy loop calls this each tick. ``market_data_fresh``
        should reflect whether the most recent ticker pull succeeded.
        """
        now = time.time()
        self._state.last_heartbeat_ts = now
        if market_data_fresh:
            self._state.last_market_data_ts = now
        # Don't persist on every heartbeat — too noisy. The summary read
        # uses the in-memory values; persistence happens on attempts/exits.

    def record_api_error(self, error: str) -> None:
        self._state.consecutive_api_errors = int(self._state.consecutive_api_errors) + 1
        self._state.last_api_error = str(error)[:200]

    def clear_api_errors(self) -> None:
        if self._state.consecutive_api_errors:
            self._state.consecutive_api_errors = 0
            self._state.last_api_error = ""

    def _append_event(self, kind: str, message: str, extra: Optional[dict] = None) -> None:
        evt = {
            "ts": time.time(),
            "kind": str(kind),
            "message": str(message),
        }
        if extra:
            evt["extra"] = dict(extra)
        self._state.events.append(evt)
        # Keep the persisted log small.
        if len(self._state.events) > 200:
            self._state.events = self._state.events[-200:]
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(bus.publish("live_event", dict(evt)))
        except Exception:
            pass

    # ----- gates -----

    def gate_check(
        self,
        settings: RuntimeSettings,
        *,
        env_flags_ack_ok: bool,
        env_flags_unlock_ok: bool,
        gate_training_ok: bool,
        okx_authenticated: bool,
        execution_mode: str,
    ) -> dict:
        """Pure-Python preflight that returns the same shape on every call.

        ``execution_mode`` should be either okx_demo or okx_live for the tester
        to be eligible. ``gate_training_ok`` is the V5.4 training gate result.
        """
        reasons: List[str] = []
        override_on = bool(settings.live_tester_override)
        if not settings.live_tester_enabled:
            reasons.append("LIVE_TESTER_ENABLED is false")
        if execution_mode not in ("okx_demo", "okx_live"):
            reasons.append("EXECUTION_MODE must be okx_demo or okx_live for the live tester")
        if not okx_authenticated:
            reasons.append("OKX private API is not authenticated (check API key + IP allowlist)")
        if execution_mode == "okx_live":
            if not env_flags_unlock_ok:
                reasons.append("LIVE_TRADING_ENABLED must be true for okx_live")
            if not env_flags_ack_ok:
                reasons.append("LIVE_TRADING_ACK must equal I_ACCEPT_REAL_MONEY_RISK")
        # V6.3 — the training gate is *advisory only* for the tiny tester
        # when LIVE_TESTER_OVERRIDE is set to the exact bypass token. The
        # full okx_live strategy mode in /api/live/readiness still enforces
        # training_gate + LIVE_DEMO_COMPLETED_ACK + LIVE_TRADING_ACK + the
        # LIVE_TRADING_ENABLED env independently — see _build_readiness().
        # This branch only governs the tiny-tester preflight surface.
        if not gate_training_ok and not override_on:
            reasons.append(
                "Training gate not satisfied (set LIVE_TESTER_OVERRIDE="
                "I_UNDERSTAND_THIS_IS_A_TINY_TEST for the tiny tester only)"
            )
        if self._state.kill_switch:
            reasons.append(f"Kill switch is engaged: {self._state.kill_switch_reason or 'manual'}")
        self._maybe_rollover_day(time.time())
        if self._state.trades_today >= int(settings.live_max_trades_per_day):
            reasons.append(f"Live trades today {self._state.trades_today} >= cap {settings.live_max_trades_per_day}")
        if -float(self._state.realized_pnl_today) >= float(settings.live_daily_loss_cap_usdt):
            reasons.append(f"Daily loss cap hit: realized {self._state.realized_pnl_today:.2f} USDT")
        if len(self.open_positions()) >= int(settings.live_max_open_positions):
            reasons.append(f"Already at max open live positions ({settings.live_max_open_positions})")
        # V9 — unattended-mode hard gates. When the operator opts in to
        # unattended mode the entry gate becomes stricter: the readiness
        # checks (native enabled+verified, caps tiny, watchdog alive,
        # no API error burst, timer not expired) MUST pass before any
        # new entry. Existing bot-managed exits continue running on
        # already-open positions regardless of this gate.
        if bool(getattr(settings, "live_unattended_mode", False)):
            if bool(self._state.unattended_expired):
                reasons.append(
                    "Unattended timer expired — new entries blocked. "
                    "Disable LIVE_UNATTENDED_MODE or release the kill switch to resume."
                )
            readiness = self._unattended_readiness(settings, self.open_positions())
            if not readiness.get("overall_pass"):
                failing = [c["label"] for c in readiness.get("checks", []) if not c.get("pass")]
                reasons.append(
                    "Unattended readiness FAIL: " + "; ".join(failing[:6])
                )
            # Optional: refuse new entries on consecutive API errors.
            if bool(getattr(settings, "live_unattended_stop_new_on_failure", True)) and int(self._state.consecutive_api_errors) >= 5:
                reasons.append(
                    f"Unattended: {self._state.consecutive_api_errors} consecutive OKX API errors — "
                    f"new entries paused. Last error: {self._state.last_api_error or 'unknown'}"
                )
        # V6.3 — derive a tester-specific lifecycle state for the UI.
        allowed = len(reasons) == 0
        if not settings.live_tester_enabled:
            tester_state = "disabled"
        elif self._state.kill_switch:
            tester_state = "kill_switch"
        elif allowed:
            tester_state = "armed"
        else:
            tester_state = "locked"
        unlocked = (
            bool(settings.live_tester_enabled)
            and okx_authenticated
            and not self._state.kill_switch
            and execution_mode in ("okx_demo", "okx_live")
            and (gate_training_ok or override_on)
        )
        ready_message = (
            "Tiny tester armed; waiting for a qualified signal."
            if allowed else ""
        )
        return {
            "allowed": allowed,
            "reasons": reasons,
            "override_active": override_on,
            "tester_state": tester_state,
            "unlocked": unlocked,
            "ready_message": ready_message,
        }

    # ----- preflight per-symbol -----

    def preflight_symbol(
        self,
        symbol: str,
        *,
        settings: RuntimeSettings,
        intended_quote_usdt: float,
        live_price: float,
        spread_pct: float,
        ticker_age_seconds: float,
        free_usdt: float,
        sl_price: float,
        trail_pct: float,
        data_quality_ok: bool,
    ) -> dict:
        """Per-symbol preflight that the strategy must pass before live entry."""
        reasons: List[str] = []
        symbol = symbol.upper()
        base = _base_of(symbol)
        quote = _quote_of(symbol)
        if quote != "USDT":
            reasons.append("Tester only supports USDT-quoted spot pairs")
        if base in (getattr(settings, "paper_excluded_bases", []) or []):
            reasons.append(f"Base {base} is in the excluded list")
        if not data_quality_ok:
            reasons.append("Market data quality not OK")
        if live_price <= 0:
            reasons.append("No fresh live price")
        if ticker_age_seconds > 8:
            reasons.append(f"Ticker stale ({ticker_age_seconds:.1f}s old)")
        if spread_pct > float(settings.symbol_max_spread_pct):
            reasons.append(f"Spread {spread_pct:.2f}% > cap {settings.symbol_max_spread_pct:.2f}%")
        cap = float(settings.live_max_order_usdt_tester)
        if intended_quote_usdt <= 0:
            reasons.append("Intended order amount is zero or negative")
        if intended_quote_usdt > cap + 1e-6:
            reasons.append(f"Intended order {intended_quote_usdt:.2f} USDT > tester cap {cap:.2f}")
        if intended_quote_usdt < 1.0:
            reasons.append(f"Intended order {intended_quote_usdt:.2f} USDT < $1 minimum")
        # USDT free balance must cover the order plus a small reserve.
        reserve_mult = max(1.0, min(2.0, float(settings.live_free_reserve_multiplier)))
        required = intended_quote_usdt * reserve_mult
        if free_usdt < required - 1e-6:
            reasons.append(f"Free USDT {free_usdt:.2f} below required {required:.2f} (incl. {(reserve_mult - 1) * 100:.0f}% reserve)")
        # Protective exit must exist if required.
        if bool(settings.live_require_protective_exit):
            if sl_price <= 0 or sl_price >= live_price:
                reasons.append("Stop-loss price not computed below the entry price")
            if trail_pct <= 0:
                # trailing optional, but log it
                pass
        # Duplicate guard inside local state.
        if settings.live_one_position_per_symbol and self.find_position_by_symbol(symbol) is not None:
            reasons.append(f"{symbol} already has an open live position")
        return {"allowed": len(reasons) == 0, "reasons": reasons}

    # ----- entry path -----

    async def attempt_entry(
        self,
        *,
        settings: RuntimeSettings,
        symbol: str,
        live_price: float,
        intended_quote_usdt: float,
        sl_price: float,
        take_profit_price: float,
        trail_pct: float,
        preflight: dict,
        decision_id: str = "",
    ) -> dict:
        """Place a tiny OKX spot market buy.

        Returns a dict containing ``status`` (``opened`` | ``failed`` | ``blocked``)
        and either the freshly recorded position or the failure reason. Every
        attempt is logged into ``attempts``.
        """
        async with self._lock:
            ts = time.time()
            self._maybe_rollover_day(ts)
            self._state.last_attempt_ts = ts
            base = _base_of(symbol)
            # Final preflight inside the lock — covers daily caps, kill switch.
            if self._state.kill_switch:
                attempt = {
                    "ts": ts, "symbol": symbol, "ok": False, "phase": "kill_switch",
                    "reason": self._state.kill_switch_reason or "kill switch engaged",
                    "intended_quote_usdt": intended_quote_usdt,
                }
                self._state.attempts.append(attempt)
                self._persist()
                return {"status": "blocked", "attempt": attempt}
            if self._state.trades_today >= int(settings.live_max_trades_per_day):
                attempt = {
                    "ts": ts, "symbol": symbol, "ok": False, "phase": "cap_trades_today",
                    "reason": f"trades_today={self._state.trades_today} >= cap {settings.live_max_trades_per_day}",
                    "intended_quote_usdt": intended_quote_usdt,
                }
                self._state.attempts.append(attempt)
                self._persist()
                return {"status": "blocked", "attempt": attempt}
            if not preflight.get("allowed"):
                attempt = {
                    "ts": ts, "symbol": symbol, "ok": False, "phase": "preflight",
                    "reason": "; ".join(preflight.get("reasons", []) or []),
                    "intended_quote_usdt": intended_quote_usdt,
                }
                self._state.attempts.append(attempt)
                self._persist()
                return {"status": "blocked", "attempt": attempt}

            client_id = f"v6t{uuid.uuid4().hex[:24]}"
            try:
                quote_amt = float(intended_quote_usdt)
                # Defense in depth: clamp again right before sending.
                quote_amt = min(quote_amt, float(settings.live_max_order_usdt_tester))
                quote_amt = max(1.0, quote_amt)
                order_resp = await okx_private.market_buy_spot(
                    symbol=symbol,
                    quote_usdt=quote_amt,
                    client_order_id=client_id,
                )
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                attempt = {
                    "ts": ts, "symbol": symbol, "ok": False, "phase": "order_post",
                    "reason": err, "intended_quote_usdt": intended_quote_usdt,
                    "client_order_id": client_id,
                }
                self._state.attempts.append(attempt)
                self._state.last_error = err
                self._persist()
                await bus.publish("live_order_failed", attempt)
                return {"status": "failed", "attempt": attempt}

            ord_id = ""
            try:
                ord_id = (order_resp.get("data") or [{}])[0].get("ordId") or ""
            except Exception:
                pass

            # Reconcile fills — best effort.
            summary = {}
            try:
                summary = await okx_private.summarize_fills(symbol, ord_id=ord_id, cl_ord_id=client_id)
            except Exception as e:
                summary = {"filled_qty": 0.0, "avg_px": 0.0, "fee": 0.0, "source": f"err:{e}"}

            filled_qty = float(summary.get("filled_qty") or 0)
            avg_px = float(summary.get("avg_px") or 0)
            fee = float(summary.get("fee") or 0)
            fee_ccy = str(summary.get("fee_ccy") or "").upper()
            needs_reconcile = filled_qty <= 0 or avg_px <= 0
            # V9.1 — net sellable quantity. On a SPOT BUY OKX charges fees
            # in the *received* currency (base). The journal must record
            # both the gross fill (for PnL cost basis) and the net amount
            # we can actually sell back. Mismatching these is what produced
            # the V9 ``OKX 200 code=1: All operations failed`` symptom —
            # the underlying sCode was 51008 insufficient balance.
            gross_filled_qty = filled_qty
            base_ccy_upper = (base or "").upper()
            fee_in_base = (fee_ccy == base_ccy_upper) and fee > 0
            fee_deducted_from_base = float(fee) if fee_in_base else 0.0
            sellable_qty = max(0.0, gross_filled_qty - fee_deducted_from_base)

            stop = float(sl_price)
            tp = float(take_profit_price) if take_profit_price > 0 else 0.0
            trail = max(0.0, float(trail_pct))
            high_water = avg_px if avg_px > 0 else live_price

            pos = LivePosition(
                id=f"lv_{uuid.uuid4().hex[:10]}",
                client_order_id=client_id,
                okx_order_id=ord_id,
                symbol=symbol,
                base_ccy=base,
                requested_quote_usdt=quote_amt,
                filled_qty=filled_qty,
                avg_entry_price=avg_px,
                quote_spent_usdt=(filled_qty * avg_px) if (filled_qty and avg_px) else 0.0,
                entry_fee_usdt=fee,
                entry_fee_ccy=fee_ccy,
                # V9.1 gross/net accounting fields:
                gross_filled_qty=gross_filled_qty,
                sellable_qty=sellable_qty,
                fee_deducted_from_base=fee_deducted_from_base,
                stop_loss_price=stop,
                take_profit_price=tp,
                trailing_stop_pct=trail,
                high_water_price=high_water,
                sl_mode="bot_managed",
                opened_ts=ts,
                needs_reconcile=needs_reconcile,
                reconcile_notes=("fills not yet visible — strategy must not re-enter until reconciled" if needs_reconcile else ""),
                status="needs_reconcile" if needs_reconcile else "open",
            )
            self._state.positions.append(pos)
            self._state.trades_today += 1
            attempt = {
                "ts": ts, "symbol": symbol, "ok": True, "phase": "opened",
                "intended_quote_usdt": intended_quote_usdt,
                "filled_qty": filled_qty, "avg_px": avg_px,
                "fee": fee, "fee_ccy": fee_ccy,
                # V9.1 — gross/sellable breakdown surfaced to UI/event feed.
                "gross_filled_qty": gross_filled_qty,
                "sellable_qty": sellable_qty,
                "fee_deducted_from_base": fee_deducted_from_base,
                "client_order_id": client_id, "okx_order_id": ord_id,
                "decision_id": decision_id,
                "needs_reconcile": needs_reconcile,
            }
            self._state.attempts.append(attempt)
            self._state.last_error = "" if not needs_reconcile else "needs_reconcile after entry"
            self._persist()
            opened_position_id = pos.id
            opened_snapshot = asdict(pos)
        # ----- V9: out of the entry lock, attach native protection (best effort).
        # We only attempt if the fill was actually reconciled (filled_qty > 0
        # and avg_px > 0); otherwise we cannot price TP/SL correctly and we
        # leave bot-managed exits to handle it. The bot-managed watcher
        # remains armed regardless of the native outcome.
        try:
            if (
                getattr(settings, "live_native_protection_enabled", False)
                and (getattr(settings, "live_native_protection_mode", "oco") or "").lower() != "off"
                and filled_qty > 0
                and avg_px > 0
                and not needs_reconcile
            ):
                await self._place_native_protection(opened_position_id, settings=settings)
        except Exception:
            # _place_native_protection is supposed to never raise; this is
            # a belt-and-braces guard so a buggy native path can never
            # mask a successful buy. The bot-managed watcher remains armed.
            pass
        await bus.publish("live_order_opened", {**attempt, "position": opened_snapshot})
        # V9 — record an autonomous-action event for the operator timeline.
        async with self._lock:
            self._append_event(
                "live_order_opened",
                f"BUY {symbol} qty={filled_qty:.6f} avg={avg_px:.6f} fee={fee:.6f} {fee_ccy or ''}",
                extra={"position_id": opened_position_id, "needs_reconcile": needs_reconcile},
            )
            self._persist()
        return {"status": "opened", "position": opened_snapshot, "attempt": attempt}

    # ----- V9: OKX native exchange-attached protection -----

    @staticmethod
    def _client_algo_id_for(position_id: str, suffix: str = "") -> str:
        """Build a stable, OKX-compliant ``clOrdId`` from a position id.

        OKX requires ≤32 alphanumeric chars. We strip the ``lv_`` prefix
        so the position id contributes more entropy, then prepend ``v9p``
        (or ``v9tp`` / ``v9sl``) so log readers can spot it. Using a
        deterministic value means a restart that re-enters the post-buy
        path produces the same clOrdId and OKX will return the existing
        algo via the ``clOrdId`` collision sCode, giving us idempotency.
        """
        clean = "".join(c for c in (position_id or "") if c.isalnum())[-24:]
        prefix = "v9p"
        if suffix in ("tp", "sl"):
            prefix = f"v9{suffix}"
        out = f"{prefix}{clean}"
        return out[:32]

    async def _refresh_inventory(self, position_id: str) -> dict:
        """Probe OKX for free/frozen balance + open sells + open algos.

        Stores the snapshot on the position. Never raises. The output is
        also returned so callers can run pre-placement checks.
        """
        async with self._lock:
            pos = next((p for p in self._state.positions if p.id == position_id), None)
            if pos is None:
                return {}
            symbol = pos.symbol
        try:
            snap = await okx_private.inst_inventory_snapshot(symbol)
        except Exception as e:
            snap = {"base_ccy": symbol.split("/")[0].upper(),
                    "total": 0.0, "free": 0.0, "frozen": 0.0,
                    "open_sells": [], "open_algos": [],
                    "error": f"{type(e).__name__}: {e}",
                    "ts": int(time.time())}
        async with self._lock:
            for p in self._state.positions:
                if p.id == position_id:
                    p.okx_inventory = dict(snap)
                    # V9.1 — journal-vs-OKX inventory drift repair. If the
                    # journal believes we hold more base than OKX reports
                    # in free+frozen (e.g. user sold manually outside the
                    # bot, or a previous V9 build over-recorded gross qty
                    # without deducting a base-asset fee), clamp the
                    # sellable qty to OKX total so exits don't trip 51008.
                    # PnL still uses ``filled_qty * avg_entry_price`` for
                    # cost basis — we don't overwrite that.
                    try:
                        journal_sellable = float(
                            p.sellable_qty or p.gross_filled_qty or p.filled_qty or 0
                        )
                        okx_total = float(snap.get("total") or 0)
                        if journal_sellable > 0 and okx_total > 0 and okx_total < journal_sellable * 0.999:
                            # Use OKX total (free + frozen) as the upper
                            # bound — frozen base can still be unfrozen by
                            # cancelling an algo. ``inventory_sellable_qty``
                            # is the field the exit path reads first.
                            p.inventory_sellable_qty = max(0.0, okx_total)
                            p.inventory_repair_note = (
                                f"clamped journal sellable {journal_sellable:.8f} "
                                f"to OKX total {okx_total:.8f} "
                                f"(free={float(snap.get('free') or 0):.8f}, "
                                f"frozen={float(snap.get('frozen') or 0):.8f})"
                            )
                            try:
                                self._append_event(
                                    "inventory_repair",
                                    f"{p.symbol} journal qty exceeds OKX inventory; "
                                    f"using {okx_total:.8f} for exits",
                                    extra={
                                        "position_id": p.id,
                                        "journal_sellable": journal_sellable,
                                        "okx_total": okx_total,
                                        "okx_free": float(snap.get("free") or 0),
                                        "okx_frozen": float(snap.get("frozen") or 0),
                                    },
                                )
                            except Exception:
                                pass
                        elif p.inventory_sellable_qty and okx_total >= journal_sellable * 0.999:
                            # Inventory has caught back up — clear the override.
                            p.inventory_sellable_qty = 0.0
                            p.inventory_repair_note = ""
                    except Exception:
                        # Repair is best-effort — never let it mask a
                        # successful inventory probe.
                        pass
                    self._persist()
                    break
        return snap

    async def refresh_inventory_for_position(self, position_id: str) -> dict:
        """Public wrapper for the UI "refresh inventory" button."""
        snap = await self._refresh_inventory(position_id)
        return {"ok": True, "inventory": snap}

    def _classify_existing_algos(
        self,
        inventory: dict,
        position_id: str,
        mode: str,
    ) -> dict:
        """Inspect OKX inventory for pre-existing protective orders.

        Returns ``{action: "adopt"|"skip_duplicate"|"warn_other"|"ok",
                    adopted: {...}, warnings: [...]}``.

        - ``adopt``: we found an algo whose ``algoClOrdId`` matches the
          deterministic ``clOrdId`` we would have used. This means a
          previous run already attached protection; we re-use that
          algoId instead of placing a duplicate.
        - ``skip_duplicate``: an OCO algo for the same instrument and a
          similar size already exists on OKX. We refuse to place a new
          one to avoid double-selling — operator is warned in the UI.
        - ``warn_other``: there are unrelated open sells / algos for
          this instrument that may be eating the free balance. We
          place native protection anyway but flag the situation.
        - ``ok``: clean slate, safe to place.
        """
        warnings: List[str] = []
        open_algos = list(inventory.get("open_algos") or [])
        open_sells = list(inventory.get("open_sells") or [])
        expected_cl = self._client_algo_id_for(position_id)
        expected_tp = self._client_algo_id_for(position_id, "tp")
        expected_sl = self._client_algo_id_for(position_id, "sl")
        # 1. Adoption by exact clOrdId match.
        for a in open_algos:
            cl = (a.get("clOrdId") or "").strip()
            if cl and cl in (expected_cl, expected_tp, expected_sl):
                return {
                    "action": "adopt",
                    "adopted": {
                        "algoId": a.get("algoId"),
                        "ordType": a.get("ordType"),
                        "clOrdId": cl,
                    },
                    "warnings": [],
                }
        # 2. Duplicate-by-shape: OCO sell already exists.
        for a in open_algos:
            if (a.get("ordType") or "").lower() == "oco":
                return {
                    "action": "skip_duplicate",
                    "adopted": {},
                    "warnings": [
                        f"OKX already has an OCO sell algoId={a.get('algoId')} for this instrument; refusing to place a duplicate"
                    ],
                }
        # 3. Conditional-mode duplicate: same trigger side already exists.
        if mode == "conditional":
            for a in open_algos:
                if (a.get("ordType") or "").lower() in ("conditional", "trigger"):
                    return {
                        "action": "skip_duplicate",
                        "adopted": {},
                        "warnings": [
                            f"OKX already has a {a.get('ordType')} sell algoId={a.get('algoId')} for this instrument; refusing to place a duplicate"
                        ],
                    }
        # 4. Unrelated open sells / non-OCO algos — warn but proceed.
        if open_sells:
            ids = ", ".join(s.get("ordId", "?") for s in open_sells[:3])
            warnings.append(
                f"OKX has {len(open_sells)} open regular sell order(s) for this instrument (e.g. {ids}); they may freeze the balance separately"
            )
        for a in open_algos:
            warnings.append(
                f"OKX has an unrelated {a.get('ordType')} algoId={a.get('algoId')} for this instrument"
            )
        return {"action": "warn_other" if warnings else "ok", "adopted": {}, "warnings": warnings}

    async def _place_native_protection(
        self,
        position_id: str,
        settings: Optional[RuntimeSettings] = None,
    ) -> dict:
        """Attach OKX-native protective sells after a successful buy fill.

        Never raises. Returns a ``native_protection`` dict that is also
        persisted onto the LivePosition. Bot-managed exits remain armed
        regardless of the outcome — a native failure must NOT remove the
        watcher; this is a defense-in-depth layer, not a replacement.
        """
        s = settings or get_settings()
        ts_now = time.time()
        enabled = bool(getattr(s, "live_native_protection_enabled", False))
        mode = str(getattr(s, "live_native_protection_mode", "oco")).lower()
        dry_run = bool(getattr(s, "live_native_protection_dry_run", False))
        if not enabled or mode == "off":
            return {
                "enabled": False, "mode": mode, "status": "none",
                "oco_algo_id": "", "tp_algo_id": "", "sl_algo_id": "",
                "cl_ord_id": "", "last_error_code": "", "last_error_msg": "",
                "placed_ts": 0.0, "cancelled_ts": 0.0,
            }

        # Snapshot the position under the lock.
        async with self._lock:
            pos = next((p for p in self._state.positions if p.id == position_id), None)
            if pos is None:
                return {
                    "enabled": True, "mode": mode, "status": "failed",
                    "oco_algo_id": "", "tp_algo_id": "", "sl_algo_id": "",
                    "cl_ord_id": "", "last_error_code": "missing_position",
                    "last_error_msg": "position id not found",
                    "placed_ts": 0.0, "cancelled_ts": 0.0,
                }
            symbol = pos.symbol
            # V9.1 — native algo sells must use the net sellable quantity,
            # not the gross fill. Selling gross would freeze a balance OKX
            # doesn't have and the algo would trip the same 51008 path on
            # trigger. Fall back to gross for legacy positions.
            base_qty = float(
                pos.inventory_sellable_qty
                or pos.sellable_qty
                or pos.gross_filled_qty
                or pos.filled_qty
                or 0
            )
            tp_px = float(pos.take_profit_price or 0)
            sl_px = float(pos.stop_loss_price or 0)
            # Mark as pending immediately so concurrent reads see the
            # in-flight state. If the OKX call fails we update it.
            pos.native_protection = {
                "enabled": True, "mode": mode, "status": "pending",
                "oco_algo_id": "", "tp_algo_id": "", "sl_algo_id": "",
                "cl_ord_id": self._client_algo_id_for(position_id),
                "last_error_code": "", "last_error_msg": "",
                "placed_ts": ts_now, "cancelled_ts": 0.0,
            }
            self._persist()

        # V9.1 — clamp against the live OKX inventory + lotSz BEFORE we
        # send the algo. We only re-clamp when not in dry-run; the
        # duplicate-guard block below also refreshes inventory but uses
        # the result for classification, not sizing.
        if not dry_run and base_qty > 0:
            try:
                _inv_pre = await okx_private.inst_inventory_snapshot(symbol, side="sell")
            except Exception:
                _inv_pre = {}
            try:
                _inst_pre = await okx_private.fetch_instrument(symbol)
            except Exception:
                _inst_pre = {"lotSz": 0.0, "minSz": 0.0}
            _clamp_pre = clamp_sell_qty(
                journal_qty=base_qty,
                okx_free=float(_inv_pre.get("free") or 0),
                lot=float(_inst_pre.get("lotSz") or 0),
                min_sz=float(_inst_pre.get("minSz") or 0),
            )
            if _clamp_pre.get("below_min"):
                return await self._record_native_status(position_id, {
                    "status": "failed",
                    "last_error_code": "insufficient_inventory",
                    "last_error_msg": (
                        "native protection not placed — "
                        + str(_clamp_pre.get("reason") or "qty below minSz")
                    ),
                })
            base_qty = float(_clamp_pre.get("sell_qty") or base_qty)

        # Validation: spot OCO needs base qty, TP > entry > SL with both > 0.
        if base_qty <= 0:
            return await self._record_native_status(position_id, {
                "status": "failed",
                "last_error_code": "invalid_qty",
                "last_error_msg": "base qty unknown — cannot place native protection",
            })
        if tp_px <= 0 and sl_px <= 0:
            return await self._record_native_status(position_id, {
                "status": "failed",
                "last_error_code": "no_triggers",
                "last_error_msg": "neither TP nor SL set — nothing to attach",
            })

        if dry_run:
            # No network call. Surface a clear marker so the UI can
            # distinguish a real exchange-active state from a test.
            return await self._record_native_status(position_id, {
                "status": "dry_run",
                "last_error_code": "",
                "last_error_msg": "LIVE_NATIVE_PROTECTION_DRY_RUN=true; no order-algo call sent",
                "oco_algo_id": "DRY_RUN",
            })

        # ---- V9: pre-placement OKX state probe + duplicate guard ----
        # Before we send order-algo, refresh OKX state so we can:
        #   (a) adopt an algo we already placed in a previous run
        #       (matched by deterministic clOrdId), avoiding a duplicate;
        #   (b) refuse to place a second OCO when one is already pending;
        #   (c) surface unrelated open sells / algos that are freezing
        #       the base balance (the V8 screenshot symptom).
        try:
            inv = await self._refresh_inventory(position_id)
        except Exception:
            inv = {}
        classification = self._classify_existing_algos(inv or {}, position_id, mode)
        if classification.get("action") == "adopt":
            adopted = classification.get("adopted") or {}
            patch = {
                "status": "active",
                "last_error_code": "",
                "last_error_msg": "adopted existing OKX algo (clOrdId match)",
                "cl_ord_id": adopted.get("clOrdId", ""),
            }
            if (adopted.get("ordType") or "").lower() == "oco":
                patch["oco_algo_id"] = adopted.get("algoId", "")
            else:
                # conditional/trigger — we can only safely associate one leg
                patch["tp_algo_id"] = adopted.get("algoId", "")
            return await self._record_native_status(position_id, patch)
        if classification.get("action") == "skip_duplicate":
            warn_msg = (classification.get("warnings") or ["duplicate detected"])[0]
            return await self._record_native_status(position_id, {
                "status": "failed",
                "last_error_code": "duplicate_guard",
                "last_error_msg": warn_msg + " — bot-managed exits remain armed",
                "environment_warnings": list(classification.get("warnings") or []),
            })
        env_warnings = list(classification.get("warnings") or [])

        # ---- real placement ----
        if mode == "oco":
            if tp_px <= 0 or sl_px <= 0:
                return await self._record_native_status(position_id, {
                    "status": "failed",
                    "last_error_code": "oco_requires_both",
                    "last_error_msg": "OCO mode requires both TP and SL prices — falling back to bot-managed",
                })
            cl_id = self._client_algo_id_for(position_id)
            res = await okx_private.place_algo_oco_spot_sell(
                symbol=symbol, base_qty=base_qty,
                tp_trigger_px=tp_px, sl_trigger_px=sl_px,
                client_algo_id=cl_id, tag="v9tester",
            )
            if res.get("ok"):
                return await self._record_native_status(position_id, {
                    "status": "active",
                    "oco_algo_id": str(res.get("algo_id") or ""),
                    "cl_ord_id": str(res.get("cl_ord_id") or cl_id),
                    "last_error_code": "",
                    "last_error_msg": "",
                    "environment_warnings": env_warnings,
                })
            return await self._record_native_status(position_id, {
                "status": "failed",
                "last_error_code": str(res.get("s_code") or ""),
                "last_error_msg": str(res.get("s_msg") or "unknown OKX error"),
                "cl_ord_id": cl_id,
                "environment_warnings": env_warnings,
            })

        # mode == "conditional"
        tp_id = self._client_algo_id_for(position_id, "tp")
        sl_id = self._client_algo_id_for(position_id, "sl")
        tp_algo = ""
        sl_algo = ""
        errors: List[str] = []
        if tp_px > 0:
            r = await okx_private.place_algo_conditional_spot_sell(
                symbol=symbol, base_qty=base_qty, trigger_px=tp_px,
                kind="tp", client_algo_id=tp_id, tag="v9tester",
            )
            if r.get("ok"):
                tp_algo = str(r.get("algo_id") or "")
            else:
                errors.append(f"TP {r.get('s_code')}: {r.get('s_msg')}")
        if sl_px > 0:
            r = await okx_private.place_algo_conditional_spot_sell(
                symbol=symbol, base_qty=base_qty, trigger_px=sl_px,
                kind="sl", client_algo_id=sl_id, tag="v9tester",
            )
            if r.get("ok"):
                sl_algo = str(r.get("algo_id") or "")
            else:
                errors.append(f"SL {r.get('s_code')}: {r.get('s_msg')}")
        if (tp_algo or sl_algo) and not errors:
            return await self._record_native_status(position_id, {
                "status": "active",
                "tp_algo_id": tp_algo,
                "sl_algo_id": sl_algo,
                "cl_ord_id": tp_id or sl_id,
                "last_error_code": "",
                "last_error_msg": "",
                "environment_warnings": env_warnings,
            })
        # Partial or full failure — cancel anything that did succeed so we
        # don't leave a stale leg dangling on the exchange.
        for left_id in (tp_algo, sl_algo):
            if left_id:
                try:
                    await okx_private.cancel_algo(symbol, left_id)
                except Exception:
                    pass
        return await self._record_native_status(position_id, {
            "status": "failed",
            "tp_algo_id": "",
            "sl_algo_id": "",
            "cl_ord_id": tp_id or sl_id,
            "last_error_code": "conditional_failed",
            "last_error_msg": " | ".join(errors) or "both conditional placements failed",
        })

    async def _record_native_status(self, position_id: str, patch: dict) -> dict:
        """Merge ``patch`` into the position's native_protection dict and persist."""
        async with self._lock:
            for p in self._state.positions:
                if p.id == position_id:
                    np_state = dict(p.native_protection or {})
                    np_state.update(patch)
                    if patch.get("status") in ("active", "dry_run"):
                        np_state["placed_ts"] = np_state.get("placed_ts") or time.time()
                    if patch.get("status") == "cancelled":
                        np_state["cancelled_ts"] = time.time()
                    p.native_protection = np_state
                    # V9 — event log entry for key native transitions.
                    new_status = (patch.get("status") or "").lower()
                    if new_status in ("active", "dry_run", "failed", "cancelled", "triggered"):
                        if new_status == "active":
                            self._append_event(
                                "native_protection_placed",
                                f"native TP/SL placed on OKX for {p.symbol}",
                                extra={"position_id": position_id, "mode": np_state.get("mode", "")},
                            )
                        elif new_status == "failed":
                            self._append_event(
                                "native_protection_failed",
                                f"native TP/SL placement failed for {p.symbol}: "
                                f"{np_state.get('last_error_code','')}/{np_state.get('last_error_msg','')}",
                                extra={"position_id": position_id},
                            )
                        elif new_status == "cancelled":
                            self._append_event(
                                "native_protection_cancelled",
                                f"native TP/SL cancelled for {p.symbol}",
                                extra={"position_id": position_id},
                            )
                        elif new_status == "triggered":
                            self._append_event(
                                "native_protection_triggered",
                                f"native TP/SL triggered on OKX for {p.symbol}",
                                extra={"position_id": position_id},
                            )
                    self._persist()
                    await bus.publish("live_native_protection", {
                        "position_id": position_id,
                        "symbol": p.symbol,
                        "native_protection": dict(np_state),
                    })
                    return dict(np_state)
            return {}

    async def _cancel_native_for_position(self, position_id: str, reason: str = "") -> dict:
        """Cancel any native algos attached to a position. Idempotent.

        Called from every code path that closes/abandons a position so we
        never leave a stale sell algo on OKX that could fire after the
        bot-managed exit already sold the inventory.
        """
        async with self._lock:
            pos = next((p for p in self._state.positions if p.id == position_id), None)
            if pos is None:
                return {"ok": True, "skipped": True, "reason": "position not found"}
            np_state = dict(pos.native_protection or {})
            if not np_state or np_state.get("status") in ("none", "", "cancelled", "triggered", "failed", "dry_run"):
                return {"ok": True, "skipped": True, "reason": np_state.get("status") or "no native protection"}
            symbol = pos.symbol
            algo_ids = [a for a in (
                np_state.get("oco_algo_id"),
                np_state.get("tp_algo_id"),
                np_state.get("sl_algo_id"),
            ) if a and a != "DRY_RUN"]
        results: List[dict] = []
        for algo_id in algo_ids:
            try:
                r = await okx_private.cancel_algo(symbol, algo_id)
            except Exception as e:
                r = {"ok": False, "s_code": "transport", "s_msg": f"{type(e).__name__}: {e}"}
            results.append({"algo_id": algo_id, **r})
        all_ok = all(r.get("ok") for r in results) if results else True
        await self._record_native_status(position_id, {
            "status": "cancelled" if all_ok else "failed",
            "last_error_code": "" if all_ok else "cancel_failed",
            "last_error_msg": (reason or "cancelled by bot") if all_ok else "; ".join(
                f"{r.get('algo_id')}: {r.get('s_msg')}" for r in results if not r.get("ok")
            ),
        })
        return {"ok": all_ok, "results": results}

    # ----- exit / tick path -----

    async def tick(self, tickers: Dict[str, dict], settings: Optional[RuntimeSettings] = None) -> None:
        """Watch live tickers and execute bot-managed exits.

        Should be called every second (or whatever the price-watch loop
        cadence is) by the strategy.
        """
        s = settings or get_settings()
        if not self._state.positions:
            return
        triggers: List[LivePosition] = []
        async with self._lock:
            for pos in self._state.positions:
                if pos.status not in ("open",):
                    continue
                t = tickers.get(pos.symbol) or {}
                last = float(t.get("last") or t.get("close") or 0)
                bid = float(t.get("bid") or 0)
                if last <= 0 and bid <= 0:
                    continue
                px = last if last > 0 else bid
                if px > pos.high_water_price:
                    pos.high_water_price = px
                reason = ""
                if pos.stop_loss_price > 0 and px <= pos.stop_loss_price:
                    reason = "stop_loss"
                elif pos.take_profit_price > 0 and px >= pos.take_profit_price:
                    reason = "take_profit"
                elif pos.trailing_stop_pct > 0:
                    trail_price = pos.high_water_price * (1.0 - pos.trailing_stop_pct / 100.0)
                    if px <= trail_price:
                        reason = "trailing_stop"
                if reason:
                    pos.status = "closing"
                    pos.close_reason = reason
                    triggers.append(LivePosition(**asdict(pos)))
            if triggers:
                self._persist()
        for trigger in triggers:
            await self._execute_exit(trigger)

    async def _execute_exit(self, snapshot: LivePosition) -> None:
        client_id = f"v6x{uuid.uuid4().hex[:24]}"
        # V9.1 — derive the *requested* sell qty from the net sellable
        # accounting, falling back to gross filled qty when the position
        # predates the V9.1 migration. The actual sz sent to OKX is then
        # re-clamped to OKX free balance and rounded to lotSz below.
        sell_qty = float(
            snapshot.inventory_sellable_qty
            or snapshot.sellable_qty
            or snapshot.gross_filled_qty
            or snapshot.filled_qty
        )
        ts = time.time()
        if sell_qty <= 0:
            # Cannot exit if we don't know the qty — mark for reconcile and stop entries.
            async with self._lock:
                for p in self._state.positions:
                    if p.id == snapshot.id:
                        p.status = "needs_reconcile"
                        p.reconcile_notes = "exit blocked — fill qty unknown"
                self._state.kill_switch = True
                self._state.kill_switch_reason = (
                    f"unable to exit {snapshot.symbol}: filled qty unknown"
                )
                self._state.last_error = self._state.kill_switch_reason
                self._persist()
            await bus.publish("live_exit_failed", {
                "ts": ts, "symbol": snapshot.symbol, "reason": "fill_qty_unknown",
            })
            return
        # V9 — cancel any native exchange algo sells BEFORE we submit the
        # bot-managed market sell. If we did not cancel first, OKX could
        # fire the algo concurrently and we would oversell (the algo
        # would try to sell base qty that the manual sell already moved).
        # ``_cancel_native_for_position`` is idempotent and never raises.
        try:
            await self._cancel_native_for_position(
                snapshot.id, reason=f"bot-managed {snapshot.close_reason or 'exit'} preempting native"
            )
        except Exception:
            pass
        # V9.1 — clamp the sell qty to OKX free balance (post-cancel) and
        # round to lotSz. This is the fix for the screenshot error
        # "OKX 200 code=1: All operations failed" — the underlying
        # sCode=51008 was insufficient balance because we tried to sell
        # gross qty when only gross−fee was free.
        try:
            inv = await okx_private.inst_inventory_snapshot(snapshot.symbol, side="sell")
        except Exception:
            inv = {}
        try:
            inst = await okx_private.fetch_instrument(snapshot.symbol)
        except Exception:
            inst = {"lotSz": 0.0, "minSz": 0.0, "ok": False}
        clamp = clamp_sell_qty(
            journal_qty=sell_qty,
            okx_free=float(inv.get("free") or 0),
            lot=float(inst.get("lotSz") or 0),
            min_sz=float(inst.get("minSz") or 0),
        )
        if clamp.get("below_min"):
            # Insufficient free balance to safely sell. Don't spam OKX.
            async with self._lock:
                for p in self._state.positions:
                    if p.id == snapshot.id:
                        p.status = "needs_reconcile"
                        p.reconcile_notes = (
                            f"exit blocked — {clamp.get('reason') or 'sell qty below minSz'}"
                        )
                self._state.kill_switch = True
                self._state.kill_switch_reason = (
                    f"live sell skipped for {snapshot.symbol}: {clamp.get('reason')}"
                )
                self._state.last_error = self._state.kill_switch_reason
                self._state.closes.append({
                    "ts": ts, "symbol": snapshot.symbol, "ok": False,
                    "phase": "qty_clamp", "reason": clamp.get("reason"),
                    "client_order_id": client_id,
                    "journal_qty": sell_qty,
                    "okx_free": float(inv.get("free") or 0),
                    "lot_sz": float(inst.get("lotSz") or 0),
                    "min_sz": float(inst.get("minSz") or 0),
                })
                self._append_event(
                    "live_exit_blocked_low_inventory",
                    f"{snapshot.symbol} exit blocked: {clamp.get('reason')}",
                    extra={
                        "position_id": snapshot.id,
                        "journal_qty": sell_qty,
                        "okx_free": float(inv.get("free") or 0),
                    },
                )
                self._persist()
            await bus.publish("live_exit_failed", {
                "ts": ts, "symbol": snapshot.symbol,
                "reason": clamp.get("reason"),
                "client_order_id": client_id,
            })
            return
        clamped_qty = float(clamp.get("sell_qty") or 0)
        try:
            resp = await okx_private.market_sell_spot(
                symbol=snapshot.symbol,
                base_qty=clamped_qty,
                client_order_id=client_id,
            )
            ord_id = (resp.get("data") or [{}])[0].get("ordId") or ""
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            async with self._lock:
                for p in self._state.positions:
                    if p.id == snapshot.id:
                        p.status = "needs_reconcile"
                        p.reconcile_notes = f"sell failed: {err}"
                self._state.kill_switch = True
                self._state.kill_switch_reason = f"live sell failed for {snapshot.symbol}: {err}"
                self._state.last_error = err
                self._state.closes.append({
                    "ts": ts, "symbol": snapshot.symbol, "ok": False,
                    "phase": "order_post", "reason": err,
                    "client_order_id": client_id,
                })
                self._persist()
            await bus.publish("live_exit_failed", {
                "ts": ts, "symbol": snapshot.symbol, "reason": err, "client_order_id": client_id,
            })
            return
        # Reconcile fills for the sell.
        try:
            summary = await okx_private.summarize_fills(snapshot.symbol, ord_id=ord_id, cl_ord_id=client_id)
        except Exception:
            summary = {}
        sell_qty_actual = float(summary.get("filled_qty") or clamped_qty)
        avg_sell_px = float(summary.get("avg_px") or 0)
        sell_fee = float(summary.get("fee") or 0)
        sell_fee_ccy = str(summary.get("fee_ccy") or "").upper()
        gross = sell_qty_actual * avg_sell_px if avg_sell_px > 0 else 0
        cost = snapshot.filled_qty * snapshot.avg_entry_price
        # V9.1 PnL hygiene:
        #   - entry_fee_usdt stores the OKX fee amount in entry_fee_ccy.
        #     When fee_ccy == base, the fee was deducted in base coins and
        #     is already implicit in selling fewer base; subtracting it as
        #     quote here would double-count. Convert it to quote using
        #     avg_entry_price only when the fee was in quote.
        #   - exit fee_ccy is usually the quote ccy on a market sell, so
        #     it's safe to subtract directly. If OKX charged it in base
        #     (rare for SPOT sells), convert via avg_sell_px.
        base_ccy_upper = (snapshot.base_ccy or "").upper()
        if snapshot.entry_fee_ccy and snapshot.entry_fee_ccy != base_ccy_upper:
            entry_fee_quote = float(snapshot.entry_fee_usdt or 0)
        else:
            entry_fee_quote = 0.0  # already realised via reduced sellable
        if sell_fee_ccy and sell_fee_ccy == base_ccy_upper and avg_sell_px > 0:
            exit_fee_quote = float(sell_fee) * float(avg_sell_px)
        else:
            exit_fee_quote = float(sell_fee)
        pnl = (gross - cost) - entry_fee_quote - exit_fee_quote
        async with self._lock:
            for p in self._state.positions:
                if p.id == snapshot.id:
                    p.status = "closed"
                    p.closed_ts = ts
                    p.close_order_id = ord_id
                    p.close_client_order_id = client_id
                    p.exit_price = avg_sell_px
                    p.realized_pnl_usdt = pnl
                    p.exit_fee_usdt = sell_fee
                    p.exit_fee_ccy = sell_fee_ccy
                    p.close_reason = snapshot.close_reason
                    break
            self._state.realized_pnl_today += pnl
            self._state.closes.append({
                "ts": ts, "symbol": snapshot.symbol, "ok": True,
                "phase": "exited", "reason": snapshot.close_reason,
                "client_order_id": client_id, "okx_order_id": ord_id,
                "filled_qty": sell_qty_actual, "avg_px": avg_sell_px, "pnl": pnl,
                "fee": sell_fee, "fee_ccy": sell_fee_ccy,
            })
            # If we crossed the daily loss cap on the way down, engage kill switch.
            if -float(self._state.realized_pnl_today) >= float(get_settings().live_daily_loss_cap_usdt):
                self._state.kill_switch = True
                self._state.kill_switch_reason = (
                    f"daily loss cap reached: realized {self._state.realized_pnl_today:.2f} USDT"
                )
            self._persist()
        await bus.publish("live_order_closed", {
            "ts": ts, "symbol": snapshot.symbol, "pnl": pnl,
            "reason": snapshot.close_reason, "client_order_id": client_id,
        })
        async with self._lock:
            self._append_event(
                "live_exit_fired",
                f"EXIT {snapshot.symbol} reason={snapshot.close_reason} pnl={pnl:.4f}",
                extra={"position_id": snapshot.id},
            )
            self._persist()

    # ----- kill switch / manual ops -----

    async def engage_kill_switch(self, reason: str = "manual") -> dict:
        async with self._lock:
            self._state.kill_switch = True
            self._state.kill_switch_reason = reason
            # Capture position ids that still hold native algos so we can
            # cancel them outside the lock. We do NOT touch positions
            # themselves here — the kill switch only blocks new entries
            # and disarms any pending OKX-side legs.
            ids_with_native = [
                p.id for p in self._state.positions
                if (p.native_protection or {}).get("status") in ("pending", "active")
            ]
            self._persist()
        # V9 — cancel native algos for every open position so the kill
        # switch immediately disarms exchange-side sells. Failures are
        # logged into each position's native_protection block.
        for pid in ids_with_native:
            try:
                await self._cancel_native_for_position(pid, reason=f"kill_switch: {reason}")
            except Exception:
                pass
        await bus.publish("live_kill_switch", {"reason": reason, "engaged": True})
        async with self._lock:
            self._append_event("kill_switch_engaged", f"kill switch engaged: {reason}")
            self._persist()
        return self.summary()

    async def cancel_native_protection(self, position_id: str, reason: str = "manual") -> dict:
        """Public entry point for cancelling native protection on demand.

        Used by the manual-close UI button and the drift reconciler. The
        position itself is NOT marked closed by this method — only the
        attached exchange algo orders are cancelled. The bot-managed
        watcher continues to manage the actual exit.
        """
        return await self._cancel_native_for_position(position_id, reason=reason)

    async def refresh_native_protection(self, position_id: str) -> dict:
        """Re-query OKX for the algo state and update the journal.

        Used by the UI "refresh" button and by drift reconcile. Never
        raises. If the algo is missing on the exchange (e.g. operator
        cancelled it via the OKX UI) we mark the journal as cancelled.
        """
        async with self._lock:
            pos = next((p for p in self._state.positions if p.id == position_id), None)
            if pos is None:
                return {"ok": False, "reason": "position not found"}
            np_state = dict(pos.native_protection or {})
        if np_state.get("status") not in ("pending", "active"):
            return {"ok": True, "skipped": True, "status": np_state.get("status") or "none"}
        algo_ids = [a for a in (
            np_state.get("oco_algo_id"),
            np_state.get("tp_algo_id"),
            np_state.get("sl_algo_id"),
        ) if a and a != "DRY_RUN"]
        if not algo_ids:
            return {"ok": True, "skipped": True, "reason": "no algo ids to refresh"}
        any_live = False
        any_triggered = False
        last_state = ""
        for algo_id in algo_ids:
            try:
                q = await okx_private.query_algo(algo_id=algo_id)
            except Exception:
                continue
            last_state = (q.get("state") or "").lower()
            if last_state in ("live", ""):
                any_live = True
            elif last_state in ("effective", "partially_effective"):
                any_triggered = True
        if any_triggered:
            new_status = "triggered"
        elif any_live:
            new_status = "active"
        else:
            new_status = "cancelled"
        await self._record_native_status(position_id, {
            "status": new_status,
            "last_error_code": "",
            "last_error_msg": f"refresh: last okx state={last_state or 'none'}",
        })
        return {"ok": True, "status": new_status}

    async def release_kill_switch(self) -> dict:
        async with self._lock:
            self._state.kill_switch = False
            self._state.kill_switch_reason = ""
            self._persist()
        await bus.publish("live_kill_switch", {"engaged": False})
        return self.summary()

    async def reconcile_position(self, position_id: str) -> dict:
        async with self._lock:
            target = next((p for p in self._state.positions if p.id == position_id), None)
            if target is None:
                return {"ok": False, "reason": "position not found"}
            if not target.client_order_id:
                return {"ok": False, "reason": "position has no client order id"}
        try:
            summary = await okx_private.summarize_fills(
                target.symbol, ord_id=target.okx_order_id, cl_ord_id=target.client_order_id
            )
        except Exception as e:
            return {"ok": False, "reason": f"{type(e).__name__}: {e}"}
        async with self._lock:
            for p in self._state.positions:
                if p.id == position_id:
                    p.filled_qty = float(summary.get("filled_qty") or p.filled_qty)
                    p.avg_entry_price = float(summary.get("avg_px") or p.avg_entry_price)
                    p.entry_fee_usdt = float(summary.get("fee") or p.entry_fee_usdt)
                    if p.filled_qty > 0 and p.avg_entry_price > 0:
                        p.quote_spent_usdt = p.filled_qty * p.avg_entry_price
                        p.status = "open"
                        p.needs_reconcile = False
                        p.reconcile_notes = ""
                    break
            self._persist()
        return {"ok": True, "summary": self.summary()}


    # ----- V9: startup reconcile path -----

    async def startup_reconcile(self, settings: Optional[RuntimeSettings] = None) -> dict:
        """Refresh inventory + native state for every open position on boot.

        Strictly opt-in. The FastAPI startup hook calls this only when
        ``LIVE_NATIVE_PROTECTION_RECONCILE_ON_STARTUP`` is true. Even
        then, this method does NOT open new trades — it merely:

        1. Refreshes OKX inventory (free/frozen + open sells + algos)
           for every open position so the UI shows accurate state
           after a process restart.
        2. Re-queries the OKX algo state for any open position whose
           journal records pending/active native protection, so we
           detect operator-cancelled algos.
        3. If native protection is enabled, for positions reporting
           ``status=="none"`` with bot-managed exits set, optionally
           attempts to attach native protection (gated; never opens
           a new trade).

        Returns a dict summarising the actions taken.
        """
        s = settings or get_settings()
        if not bool(getattr(s, "live_native_protection_reconcile_on_startup", False)):
            return {"ok": True, "skipped": True, "reason": "flag off"}
        actions: List[dict] = []
        open_pos = self.open_positions()
        for p in open_pos:
            entry = {"position_id": p.id, "symbol": p.symbol, "actions": []}
            try:
                await self._refresh_inventory(p.id)
                entry["actions"].append("inventory_refreshed")
            except Exception as e:
                entry["actions"].append(f"inventory_error:{type(e).__name__}")
            # Refresh native state for anything we already attached.
            np_state = (p.native_protection or {})
            if (np_state.get("status") or "").lower() in ("pending", "active"):
                try:
                    await self.refresh_native_protection(p.id)
                    entry["actions"].append("native_refreshed")
                except Exception as e:
                    entry["actions"].append(f"native_refresh_error:{type(e).__name__}")
            # Optionally attach native protection for unprotected open positions.
            if (
                bool(getattr(s, "live_native_protection_enabled", False))
                and (np_state.get("status") or "none").lower() in ("none", "failed", "cancelled")
                and (p.stop_loss_price > 0 or p.take_profit_price > 0)
                and p.filled_qty > 0
                and p.avg_entry_price > 0
                and not p.needs_reconcile
            ):
                try:
                    await self._place_native_protection(p.id, settings=s)
                    entry["actions"].append("native_placed")
                except Exception as e:
                    entry["actions"].append(f"native_place_error:{type(e).__name__}")
            actions.append(entry)
        async with self._lock:
            self._append_event(
                "startup_reconcile",
                f"startup reconcile complete for {len(actions)} open position(s)",
                extra={"actions": actions},
            )
            self._persist()
        return {"ok": True, "actions": actions}


live_tester = LiveTester()
