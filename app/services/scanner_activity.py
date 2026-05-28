"""V7 — Scanner activity + blocker observability.

This module aggregates what the strategy loop is doing (without changing any
trade-execution behaviour) so the UI can show the user:

  - which symbols were scanned, when, queue/warming status,
  - which were skipped and why,
  - the top candidates surfaced by the analyzer + the reason they were
    rejected (below confidence, HTF bias bearish, etc.),
  - WHY no live trade fired right now (per category: full-live gate,
    tiny tester, no qualified signal, existing position, confidence below
    threshold, cooldown, daily caps, insufficient balance/free reserve),
  - last qualified signal considered + last live-tester order attempt.

The state is process-local and thread-safe enough for the FastAPI single-
process loop. It is intentionally read/write from the strategy loop only
and read-only from the API layer.

Nothing in this module places orders, mutates settings, or relaxes any
safety check. It only reads/records.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional


class ScannerActivity:
    """Snapshot of what the scanner is doing right now."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Scanner state
        self._last_scan_started: float = 0.0
        self._last_scan_finished: float = 0.0
        self._last_scan_duration_ms: float = 0.0
        self._last_scan_text: str = "idle"
        self._scanned_symbols: List[str] = []
        self._core_symbols: List[str] = []
        self._discovered_symbols: List[str] = []
        self._clean_symbols: List[str] = []
        self._skipped_symbols: Dict[str, str] = {}
        self._profile_skipped: Dict[str, str] = {}
        # Top ranked candidates from the last scan. Each row:
        # {symbol, strategy, score, signal, confidence, reasons, status,
        #  rejected_reason}
        self._top_candidates: List[Dict[str, Any]] = []
        # Blocker categories — booleans + human reasons. Recomputed every scan.
        self._blockers: Dict[str, Dict[str, Any]] = {}
        # Last qualified signal considered (the one that ran the AI committee).
        self._last_qualified_signal: Optional[Dict[str, Any]] = None
        # Last live-tester order attempt (success or refusal).
        self._last_live_attempt: Optional[Dict[str, Any]] = None
        self._last_live_attempt_ts: float = 0.0
        # Last "why no trade" textual summary (for compact UI display).
        self._last_no_trade_reason: str = ""
        # Scanner counters since process start.
        self._scans_total: int = 0
        self._symbols_scanned_total: int = 0
        # Warming state: True if the cache hasn't fully primed yet (< 2 scans).
        self._warming: bool = True
        # Rate-limit visibility (mirrored from market_data layer).
        self._rate_limited: bool = False
        self._rate_limit_until: float = 0.0
        # The size of the resolved universe (may exceed max_scan_symbols
        # briefly if discovery is mid-flight).
        self._universe_size: int = 0
        # Configured cap surfaced for the UI.
        self._max_scan_symbols: int = 0
        # V8 — cached scan interval (seconds) for the next-scan ETA. Updated
        # whenever mark_scan_start is called so the snapshot can report a
        # countdown without having to import settings on every render.
        self._scan_interval_seconds: int = 0

    # ---------------- writers (called from strategy.py) ----------------

    def mark_scan_start(self, *, scanned: List[str], core: List[str],
                        discovered: List[str], max_scan_symbols: int) -> None:
        with self._lock:
            self._last_scan_started = time.time()
            self._scanned_symbols = list(scanned)
            self._core_symbols = list(core)
            self._discovered_symbols = list(discovered)
            self._universe_size = len(scanned)
            self._max_scan_symbols = int(max_scan_symbols or 0)
            # V8 — pick up the live scan interval from settings each scan so
            # operator drawer changes are reflected immediately. Lazy import to
            # avoid circulars at module load.
            try:
                from app.core.settings import get_settings  # local import
                self._scan_interval_seconds = int(getattr(get_settings(), "scan_interval_seconds", 0) or 0)
            except Exception:
                # Never let a settings-load failure break instrumentation.
                self._scan_interval_seconds = self._scan_interval_seconds or 0
            self._scans_total += 1
            self._symbols_scanned_total += len(scanned)
            if self._scans_total >= 2:
                self._warming = False

    def update_data_quality(
        self,
        *,
        clean_symbols: List[str],
        skipped: Dict[str, str],
        rate_limited: bool,
        rate_limit_until: float,
    ) -> None:
        with self._lock:
            self._clean_symbols = list(clean_symbols)
            self._skipped_symbols = dict(skipped)
            self._rate_limited = bool(rate_limited)
            self._rate_limit_until = float(rate_limit_until or 0.0)

    def update_profile_skipped(self, profile_skipped: Dict[str, str]) -> None:
        with self._lock:
            self._profile_skipped = dict(profile_skipped)

    def update_candidates(self, ranked_rows: List[Dict[str, Any]]) -> None:
        """Top candidates ordered by score, each with rejection reason
        (empty string if the row passed)."""
        with self._lock:
            self._top_candidates = [dict(r) for r in ranked_rows[:10]]

    def update_blockers(self, blockers: Dict[str, Dict[str, Any]]) -> None:
        with self._lock:
            self._blockers = {k: dict(v) for k, v in blockers.items()}

    def record_qualified_signal(self, signal: Dict[str, Any]) -> None:
        with self._lock:
            self._last_qualified_signal = dict(signal)

    def record_live_attempt(self, attempt: Dict[str, Any]) -> None:
        with self._lock:
            self._last_live_attempt = dict(attempt)
            self._last_live_attempt_ts = time.time()

    def mark_scan_finished(self, text: str = "", no_trade_reason: str = "") -> None:
        with self._lock:
            self._last_scan_finished = time.time()
            self._last_scan_duration_ms = round(
                (self._last_scan_finished - self._last_scan_started) * 1000.0, 1
            ) if self._last_scan_started else 0.0
            if text:
                self._last_scan_text = text
            if no_trade_reason:
                self._last_no_trade_reason = no_trade_reason

    # ---------------- read (used by API + UI) ----------------

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            rl_remaining = max(0.0, self._rate_limit_until - now) if self._rate_limited else 0.0
            return {
                "warming": bool(self._warming),
                "scans_total": int(self._scans_total),
                "symbols_scanned_total": int(self._symbols_scanned_total),
                "last_scan_started": self._last_scan_started,
                "last_scan_finished": self._last_scan_finished,
                "last_scan_age_seconds": round(now - self._last_scan_finished, 1) if self._last_scan_finished else None,
                "last_scan_duration_ms": self._last_scan_duration_ms,
                "last_scan_text": self._last_scan_text,
                "scanned_symbols": list(self._scanned_symbols),
                "scanned_count": len(self._scanned_symbols),
                "core_symbols": list(self._core_symbols),
                "discovered_symbols": list(self._discovered_symbols),
                "clean_symbols": list(self._clean_symbols),
                "clean_count": len(self._clean_symbols),
                "skipped_symbols": dict(self._skipped_symbols),
                "skipped_count": len(self._skipped_symbols),
                "profile_skipped": dict(self._profile_skipped),
                "profile_skipped_count": len(self._profile_skipped),
                "top_candidates": list(self._top_candidates),
                "blockers": dict(self._blockers),
                "last_qualified_signal": dict(self._last_qualified_signal) if self._last_qualified_signal else None,
                "last_live_attempt": dict(self._last_live_attempt) if self._last_live_attempt else None,
                "last_live_attempt_age_seconds": round(now - self._last_live_attempt_ts, 1) if self._last_live_attempt_ts else None,
                "last_no_trade_reason": self._last_no_trade_reason,
                "rate_limited": bool(self._rate_limited),
                "rate_limit_until": self._rate_limit_until,
                "rate_limit_remaining_seconds": round(rl_remaining, 1),
                "universe_size": int(self._universe_size),
                "max_scan_symbols": int(self._max_scan_symbols),
                # V8 — expose scan interval and a derived ETA for the next
                # scan so the topbar can show "Next scan: 12s". The ETA is
                # computed from last_scan_started + scan_interval_seconds.
                "scan_interval_seconds": int(self._scan_interval_seconds),
                "next_scan_eta_seconds": (
                    round(max(0.0, (self._last_scan_started + self._scan_interval_seconds) - now), 1)
                    if self._last_scan_started and self._scan_interval_seconds else None
                ),
                "total_universe_size": int(
                    len(self._core_symbols) + len(self._discovered_symbols)
                ),
            }


# Process-wide singleton mirrors the rest of this codebase.
scanner_activity = ScannerActivity()
