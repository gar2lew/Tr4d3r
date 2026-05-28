"""OKX public market data fetcher.

V5.2 — OKX RATE LIMIT HOTFIX
==============================
This module is the hot-loop on OKX public REST. V5.1 (dynamic universe) made
the bot scan ~28 symbols, which produced rapid-fire per-symbol ticker AND
candle calls. OKX answered with HTTP 429 storms, which then poisoned
``data_quality`` and triggered DATA HOLD on the whole bot.

The V5.2 changes here are deliberately defensive:

1. **Bulk ticker pull.** All spot tickers are fetched in ONE request:
   ``GET /api/v5/market/tickers?instType=SPOT``. We update every symbol's
   ticker from that snapshot. No more per-symbol ``/market/ticker`` calls
   when we just need a price refresh.
2. **All-tickers cache.** The bulk pull is cached for ``TICKER_BULK_TTL``
   seconds (default 2s). A 1-second price-watch loop simply re-reads from
   the cache; it never re-hits OKX more than once every TTL window.
3. **Candle cache with per-timeframe TTL.** 1m/5m/15m candles are cached
   for ``CANDLE_TTL_SHORT`` (default 45s). 1h/4h/1d for ``CANDLE_TTL_LONG``
   (default 240s = 4 min). Refetch only happens when the cached entry is
   expired AND not in cooldown.
4. **429 backoff.** When OKX returns 429 (or any rate-limit-like signal),
   we set a global ``rate_limit_until`` cooldown. While in cooldown,
   ``fetch_candles``/``fetch_ticker`` return the cached value (possibly
   stale) instead of hammering. Per-symbol candle errors also bump a
   per-symbol cooldown so one chronically-broken instrument doesn't blow
   the global quota.
5. **Skip tracking.** Symbols that have insufficient candles or stale/
   missing tickers are exposed via ``skipped_symbols()`` so the strategy
   loop can mark them as "skip but keep scanning the rest".

The data quality report no longer fails the whole universe when individual
symbols are broken — it returns a ``clean_symbols`` list and a
``skipped`` list. The strategy layer decides what to do.

Symbols use ccxt-style "BTC/USDT" which we translate to OKX "BTC-USDT".
If the live source is broadly broken we expose a clear OFFLINE state — we
do NOT fabricate market data.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Dict, List, Optional, Tuple

import httpx


# V6.2 — public market-data host is configurable too. Public endpoints
# (no signing) work on every OKX host — we keep the historical default
# ``https://www.okx.com`` to avoid changing market-data behaviour for
# existing users. Operators who want everything to share one host can
# set ``OKX_PUBLIC_BASE_URL`` (or fall through to ``OKX_BASE_URL``).
_DEFAULT_OKX_PUBLIC_BASE = "https://www.okx.com"


def _okx_public_base() -> str:
    raw = (os.getenv("OKX_PUBLIC_BASE_URL") or "").strip()
    if not raw:
        # Fall through to the private host setting if the operator only
        # wants to configure one URL. This keeps the historical default
        # (www.okx.com) when neither env var is set.
        raw = (os.getenv("OKX_BASE_URL") or "").strip()
    if not raw:
        return _DEFAULT_OKX_PUBLIC_BASE
    return raw.rstrip("/")


# Resolved once at import time. The public endpoints are hit on a tight
# loop and don't need per-request env lookups; a docker compose restart
# picks up changes the same way as before.
OKX_BASE = _okx_public_base()


def to_okx(symbol: str) -> str:
    return symbol.replace("/", "-").upper()


def from_okx(inst_id: str) -> str:
    return inst_id.replace("-", "/").upper()


TF_MAP = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1H",
    "4h": "4H",
    "1d": "1D",
}

SHORT_TIMEFRAMES = {"1m", "3m", "5m", "15m", "30m"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except Exception:
        return default


# All TTLs are configurable via env so an operator can dial things up in
# noisier networks. Defaults reflect what the V5.2 task asked for.
TICKER_BULK_TTL = _env_float("OKX_TICKER_BULK_TTL_SECONDS", 2.0)
CANDLE_TTL_SHORT = _env_float("OKX_CANDLE_TTL_SHORT_SECONDS", 45.0)
CANDLE_TTL_LONG = _env_float("OKX_CANDLE_TTL_LONG_SECONDS", 240.0)
RATE_LIMIT_COOLDOWN = _env_float("OKX_RATE_LIMIT_COOLDOWN_SECONDS", 20.0)
PER_SYMBOL_COOLDOWN = _env_float("OKX_PER_SYMBOL_COOLDOWN_SECONDS", 60.0)
CANDLE_REQUIRED_BARS = _env_int("OKX_CANDLE_REQUIRED_BARS", 60)
CANDLE_FETCH_CONCURRENCY = _env_int("OKX_CANDLE_FETCH_CONCURRENCY", 4)


class MarketData:
    """Caches candles and tickers, tracks live/offline state, and self-throttles."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(7.0, connect=4.0))
        self._candles: Dict[Tuple[str, str], List[List[float]]] = {}
        # Candle fetch bookkeeping
        self._candle_ts: Dict[Tuple[str, str], float] = {}     # last successful fetch
        self._candle_cooldown: Dict[Tuple[str, str], float] = {}  # backoff until ts
        self._candle_errors: Dict[Tuple[str, str], str] = {}
        self._tickers: Dict[str, dict] = {}
        self._tickers_bulk_ts: float = 0.0
        self._last_ok_ts: float = 0.0
        self._last_error: str = ""
        self._consecutive_errors: int = 0
        # Global rate-limit cooldown (set when OKX returns 429)
        self._rate_limit_until: float = 0.0
        self._rate_limit_hits: int = 0
        # Per-symbol skip tracking (transparent, surfaced in status)
        self._skipped: Dict[str, str] = {}  # symbol -> reason
        # Concurrency limiter for candle fetches
        self._candle_sem = asyncio.Semaphore(max(1, CANDLE_FETCH_CONCURRENCY))
        # Dynamic universe discovery cache (unchanged from V5.1 logic)
        self._discovered: List[str] = []
        self._discovered_meta: Dict[str, dict] = {}
        self._discovered_ts: float = 0.0

    # ---------------- state surface ----------------

    @property
    def is_live(self) -> bool:
        # Considered live if we successfully fetched within the last 90s
        return (time.time() - self._last_ok_ts) < 90 and self._last_ok_ts > 0

    @property
    def in_rate_limit_cooldown(self) -> bool:
        return time.time() < self._rate_limit_until

    @property
    def status(self) -> dict:
        return {
            "live": self.is_live,
            "last_ok_ts": self._last_ok_ts,
            "last_error": self._last_error,
            "consecutive_errors": self._consecutive_errors,
            "source": "OKX public REST",
            "rate_limited": self.in_rate_limit_cooldown,
            "rate_limit_until": self._rate_limit_until,
            "rate_limit_hits": self._rate_limit_hits,
            "skipped_symbols_count": len(self._skipped),
            "ticker_cache_age_s": round(time.time() - self._tickers_bulk_ts, 2) if self._tickers_bulk_ts else None,
        }

    def skipped_symbols(self) -> Dict[str, str]:
        return dict(self._skipped)

    def discovered_universe(self) -> List[str]:
        return list(self._discovered)

    def discovered_meta(self) -> Dict[str, dict]:
        return dict(self._discovered_meta)

    async def close(self) -> None:
        await self._client.aclose()

    # ---------------- 429 helpers ----------------

    def _trip_rate_limit(self, where: str) -> None:
        self._rate_limit_until = time.time() + RATE_LIMIT_COOLDOWN
        self._rate_limit_hits += 1
        self._last_error = f"429 rate-limited at {where}; cooling {int(RATE_LIMIT_COOLDOWN)}s"

    def _is_rate_limit_response(self, status_code: int, body: dict | None) -> bool:
        if status_code == 429:
            return True
        if body and isinstance(body, dict):
            code = str(body.get("code") or "")
            # OKX 50011 = Rate limit reached
            if code in ("50011", "50113"):
                return True
        return False

    # ---------------- ticker fetching ----------------

    async def fetch_all_spot_tickers(self, force: bool = False) -> Dict[str, dict]:
        """V5.2 — pull ALL spot tickers in one HTTP call.

        This replaces the per-symbol ticker hot loop. Result is cached for
        ``TICKER_BULK_TTL`` seconds. If we're in the global rate-limit
        cooldown, we return the cached snapshot (possibly stale) instead
        of issuing a new request.
        """
        now = time.time()
        if not force and (now - self._tickers_bulk_ts) < TICKER_BULK_TTL and self._tickers:
            return dict(self._tickers)
        if self.in_rate_limit_cooldown and self._tickers:
            # Stay quiet during cooldown; return what we have.
            return dict(self._tickers)
        try:
            r = await self._client.get(
                f"{OKX_BASE}/api/v5/market/tickers", params={"instType": "SPOT"}
            )
            body: dict | None = None
            try:
                body = r.json()
            except Exception:
                body = None
            if self._is_rate_limit_response(r.status_code, body):
                self._trip_rate_limit("market/tickers")
                self._consecutive_errors += 1
                return dict(self._tickers)
            r.raise_for_status()
            if not body or body.get("code") != "0":
                raise RuntimeError(f"OKX error: {(body or {}).get('msg')}")
            rows = body.get("data", []) or []
            updated: Dict[str, dict] = {}
            for row in rows:
                inst = row.get("instId", "")
                if not inst or "-" not in inst:
                    continue
                sym = from_okx(inst)
                try:
                    last = float(row.get("last") or 0)
                    bid = float(row.get("bidPx") or last)
                    ask = float(row.get("askPx") or last)
                except Exception:
                    continue
                if last <= 0:
                    continue
                t = {
                    "symbol": sym,
                    "last": last,
                    "bid": bid,
                    "ask": ask,
                    "open24h": float(row.get("open24h") or 0),
                    "high24h": float(row.get("high24h") or 0),
                    "low24h": float(row.get("low24h") or 0),
                    "vol24h": float(row.get("vol24h") or 0),
                    "ts": int(row.get("ts") or time.time() * 1000),
                }
                updated[sym] = t
            # Replace the snapshot wholesale — bulk endpoint is authoritative.
            self._tickers = updated
            self._tickers_bulk_ts = now
            self._last_ok_ts = now
            self._consecutive_errors = 0
            self._last_error = ""
            return dict(self._tickers)
        except Exception as e:
            self._consecutive_errors += 1
            self._last_error = f"fetch_all_spot_tickers {type(e).__name__}: {e}"
            return dict(self._tickers)

    async def fetch_ticker(self, symbol: str) -> Optional[dict]:
        """Per-symbol ticker (LEGACY).

        Prefer ``fetch_all_spot_tickers`` — this method now just falls
        through to the bulk cache when fresh and avoids individual REST
        calls during rate-limit cooldown.
        """
        cached = self._tickers.get(symbol)
        if cached and (time.time() - self._tickers_bulk_ts) < TICKER_BULK_TTL:
            return cached
        if self.in_rate_limit_cooldown:
            return cached
        # Refresh from bulk instead of hitting /market/ticker per symbol.
        await self.fetch_all_spot_tickers()
        return self._tickers.get(symbol)

    async def refresh_tickers(self, symbols: List[str]) -> Dict[str, dict]:
        """V5.2 — fast price refresh path.

        Always uses the bulk endpoint and respects the TTL/cooldown. We
        return only the subset asked for so callers don't see hundreds of
        unrelated symbols.
        """
        await self.fetch_all_spot_tickers()
        out: Dict[str, dict] = {}
        for s in symbols:
            t = self._tickers.get(s)
            if t:
                out[s] = t
        return out

    # ---------------- candle fetching ----------------

    def _candle_ttl(self, timeframe: str) -> float:
        return CANDLE_TTL_SHORT if timeframe in SHORT_TIMEFRAMES else CANDLE_TTL_LONG

    async def fetch_candles(self, symbol: str, timeframe: str = "5m", limit: int = 140) -> Optional[List[List[float]]]:
        """Fetch candles with TTL cache + per-symbol cooldown + 429 backoff."""
        key = (symbol, timeframe)
        now = time.time()
        ttl = self._candle_ttl(timeframe)
        cached = self._candles.get(key)
        # Serve from cache if fresh
        if cached and (now - self._candle_ts.get(key, 0.0)) < ttl:
            return cached
        # Respect per-symbol cooldown
        if now < self._candle_cooldown.get(key, 0.0):
            return cached  # may be None — caller treats as missing
        # Respect global rate-limit cooldown
        if self.in_rate_limit_cooldown:
            return cached
        async with self._candle_sem:
            # Re-check freshness after waiting on semaphore
            now = time.time()
            if cached and (now - self._candle_ts.get(key, 0.0)) < ttl:
                return cached
            if now < self._candle_cooldown.get(key, 0.0):
                return cached
            if self.in_rate_limit_cooldown:
                return cached
            bar = TF_MAP.get(timeframe, "5m")
            params = {"instId": to_okx(symbol), "bar": bar, "limit": str(limit)}
            try:
                r = await self._client.get(f"{OKX_BASE}/api/v5/market/candles", params=params)
                body: dict | None = None
                try:
                    body = r.json()
                except Exception:
                    body = None
                if self._is_rate_limit_response(r.status_code, body):
                    self._trip_rate_limit(f"candles {symbol} {timeframe}")
                    # Push this symbol into per-symbol cooldown too
                    self._candle_cooldown[key] = time.time() + PER_SYMBOL_COOLDOWN
                    self._candle_errors[key] = "429 rate limit"
                    self._consecutive_errors += 1
                    return cached
                r.raise_for_status()
                if not body or body.get("code") != "0":
                    raise RuntimeError(f"OKX error: {(body or {}).get('msg')}")
                raw = body.get("data", []) or []
                candles: List[List[float]] = []
                for row in raw:
                    candles.append([
                        int(row[0]),
                        float(row[1]),
                        float(row[2]),
                        float(row[3]),
                        float(row[4]),
                        float(row[5]),
                    ])
                candles.reverse()  # oldest first
                self._candles[key] = candles
                self._candle_ts[key] = time.time()
                self._candle_errors.pop(key, None)
                self._last_ok_ts = time.time()
                self._consecutive_errors = 0
                self._last_error = ""
                return candles
            except Exception as e:
                self._consecutive_errors += 1
                self._last_error = f"candles {symbol} {timeframe}: {type(e).__name__}: {e}"
                self._candle_errors[key] = str(e)
                # If a single symbol is broken, cool it off — don't hammer.
                self._candle_cooldown[key] = time.time() + PER_SYMBOL_COOLDOWN
                return cached

    async def refresh_all(
        self,
        symbols: List[str],
        timeframe: str,
        extra_timeframes: Optional[List[str]] = None,
    ) -> Dict[str, dict]:
        """Refresh tickers (bulk) and candles for the symbol set.

        Candles only refresh for entries whose cache has expired; the rest
        are served from memory. Returns the latest tickers (subset).
        """
        await self.fetch_all_spot_tickers()
        timeframes = [timeframe, *(extra_timeframes or [])]
        unique_timeframes = list(dict.fromkeys([tf for tf in timeframes if tf]))
        # Only schedule fetches that are actually due — this is the biggest
        # win against 429: a typical scan now issues maybe 0-3 candle calls
        # instead of 28 × 2 = 56.
        now = time.time()
        tasks = []
        for s in symbols:
            for tf in unique_timeframes:
                key = (s, tf)
                age = now - self._candle_ts.get(key, 0.0)
                if age >= self._candle_ttl(tf) and now >= self._candle_cooldown.get(key, 0.0):
                    tasks.append(self.fetch_candles(s, tf))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        out: Dict[str, dict] = {}
        for s in symbols:
            t = self._tickers.get(s)
            if t:
                out[s] = t
        return out

    def get_candles(self, symbol: str, timeframe: str) -> Optional[List[List[float]]]:
        return self._candles.get((symbol, timeframe))

    def get_ticker(self, symbol: str) -> Optional[dict]:
        return self._tickers.get(symbol)

    def all_tickers(self) -> Dict[str, dict]:
        return dict(self._tickers)

    # ---------------- data quality (V5.2 — skip-not-block) ----------------

    def quality_report(self, symbols: List[str], timeframes: List[str]) -> dict:
        """V5.2 — per-symbol triage instead of all-or-nothing.

        Returns ``clean_symbols`` (safe to trade) and ``skipped`` (with a
        per-symbol reason). The strategy layer then trades on the clean
        list and exposes the skipped count in the UI. DATA HOLD only fires
        if the live source is broadly broken OR no clean symbols remain.
        """
        now = time.time()
        clean_symbols: list[str] = []
        skipped: Dict[str, str] = {}
        issues: list[str] = []
        ticker_count = 0
        fresh_ticker_count = 0
        candle_checks = 0
        candle_ok = 0
        required_bars = CANDLE_REQUIRED_BARS

        for sym in symbols:
            ticker = self._tickers.get(sym)
            ticker_ok = False
            sym_reasons: list[str] = []
            if ticker:
                ticker_count += 1
                ts = float(ticker.get("ts") or 0) / 1000
                if ts and (now - ts) <= 60:
                    fresh_ticker_count += 1
                    ticker_ok = True
                elif ts:
                    sym_reasons.append(f"ticker stale {int(now - ts)}s")
                else:
                    sym_reasons.append("ticker has no timestamp")
            else:
                sym_reasons.append("ticker missing")
            # Candle check
            tf_results: list[bool] = []
            for tf in timeframes:
                if not tf:
                    continue
                candle_checks += 1
                candles = self._candles.get((sym, tf)) or []
                if len(candles) >= required_bars:
                    candle_ok += 1
                    tf_results.append(True)
                else:
                    tf_results.append(False)
                    sym_reasons.append(f"{tf} candles {len(candles)}/{required_bars}")
            sym_ok = ticker_ok and all(tf_results)
            if sym_ok:
                clean_symbols.append(sym)
            else:
                reason = "; ".join(sym_reasons) or "unknown"
                skipped[sym] = reason
                issues.append(f"{sym}: {reason}")

        self._skipped = skipped

        # Source-broadly-broken signals — these justify a true DATA HOLD.
        live_ratio = 1.0 if self.is_live else 0.0
        ticker_ratio = fresh_ticker_count / max(1, len(symbols))
        candle_ratio = candle_ok / max(1, candle_checks)
        score = round((ticker_ratio * 0.45 + candle_ratio * 0.45 + live_ratio * 0.10) * 100)

        # Source-level OK: live, ticker bulk is fresh, and at least some clean.
        bulk_age = (now - self._tickers_bulk_ts) if self._tickers_bulk_ts else 9999
        source_ok = self.is_live and bulk_age < 60 and len(clean_symbols) > 0
        return {
            "ok": source_ok,                # source-level — strategy decides per-symbol
            "score": score,
            "source": "OKX public REST",
            "simulated": False,
            "rate_limited": self.in_rate_limit_cooldown,
            "rate_limit_until": self._rate_limit_until,
            "last_ok_age_seconds": round(now - self._last_ok_ts, 1) if self._last_ok_ts else None,
            "tickers_present": ticker_count,
            "tickers_fresh": fresh_ticker_count,
            "symbols_expected": len(symbols),
            "candle_timeframes": [tf for tf in timeframes if tf],
            "candle_checks": candle_checks,
            "candle_ok": candle_ok,
            "clean_symbols": clean_symbols,
            "clean_count": len(clean_symbols),
            "skipped": skipped,
            "skipped_count": len(skipped),
            "issues": issues[:12],
            "rule": (
                "V5.2 — source-level OK means tickers are flowing. Per-symbol skips "
                "are tracked separately and do not block clean symbols."
            ),
        }

    # ---------------- dynamic universe ----------------

    # V5.2 — defaults are tighter and stable/wrapped/staked/gold are excluded
    # by default. The strategy layer can pass additional excludes.
    # V5.4 — expanded stablecoin / pegged / synthetic blocklist after the
    # V5.3 USDG/USDT "impossible TP fill" incident. Any base that's
    # designed to track USD ($1) is excluded by default from BOTH dynamic
    # discovery AND from any paper entry path. The strategy layer applies
    # the same list (plus user additions) when validating candidates.
    DEFAULT_BAD_BASES: tuple[str, ...] = (
        # stables / fiat-pegged (USD-trackers)
        "USDT", "USDC", "USDK", "USDG", "DAI", "TUSD", "BUSD", "FDUSD", "PYUSD",
        "RLUSD", "EURT", "GUSD", "USDD", "USTC", "USDE", "SUSDE", "USDP", "USDJ",
        "USDX", "CRVUSD", "FRAX", "LUSD", "GHO", "PYRUSD", "USYC",
        # gold / commodity tokens (track real-world assets — boring on USDT)
        "XAUT", "PAXG", "TGOLD", "XAUTBL",
        # liquid-staked / wrapped variants (price often diverges + thin candles)
        "BETH", "STETH", "RETH", "WBETH", "CBETH", "WSTETH", "OETH", "FRXETH", "SFRXETH",
        "OKSOL", "OKBTC", "OKETH",
        "WBTC", "WETH", "WBNB", "WMATIC", "WAVAX", "WFTM",
        # rebasing / index oddities that often look stale or thin
        "LUNC",
    )

    def _is_excludable_base(self, base: str, custom: set[str]) -> bool:
        b = base.upper()
        if b in custom:
            return True
        if b in self.DEFAULT_BAD_BASES:
            return True
        # V5.4 — catch the long tail of USD-pegged stablecoins by name. Almost
        # all stables either start or end with "USD". This is the safety net
        # that fixed USDG/USDT slipping through discovery in V5.3.
        if b.startswith("USD") or b.endswith("USD"):
            return True
        # heuristics: leading prefixes that almost always indicate
        # wrapped/synthetic/staked variants.
        for prefix in ("W", "ST", "CB", "R", "OK"):
            if b.startswith(prefix) and b[len(prefix):] in ("BTC", "ETH", "SOL", "BNB", "MATIC", "AVAX", "FTM"):
                return True
        return False

    def is_excludable_symbol(self, symbol: str, custom_bases: Optional[List[str]] = None) -> bool:
        """V5.4 — public helper used by the strategy layer to validate any
        candidate symbol (core OR discovered) BEFORE risk gates run. Returns
        True if the base is in the default/custom stable/wrapped blocklist.
        """
        if not symbol or "/" not in symbol:
            return False
        base = symbol.split("/")[0].upper()
        custom = set((b or "").upper() for b in (custom_bases or []))
        return self._is_excludable_base(base, custom)

    async def discover_universe(
        self,
        max_symbols: int = 10,
        min_quote_vol_usdt: float = 5_000_000.0,
        exclude: Optional[List[str]] = None,
        exclude_bases: Optional[List[str]] = None,
        ttl_seconds: int = 900,
    ) -> List[str]:
        """V5.2 — pull all SPOT USDT pairs from the bulk ticker cache.

        Reuses the cached bulk-tickers snapshot when fresh; only re-hits
        OKX when expired. Filters:
          - instType=SPOT, quote ccy = USDT
          - bid > 0, ask > 0, spread <= 0.5%
          - quote_vol >= min_quote_vol_usdt
          - excluded by name OR by base symbol (stable/wrapped/staked/gold)
        """
        now = time.time()
        if self._discovered and (now - self._discovered_ts) < ttl_seconds:
            ex = set(exclude or [])
            return [s for s in self._discovered if s not in ex][:max_symbols]

        # Use bulk snapshot when fresh; otherwise refresh (this is the ONE
        # call we issue here — no per-symbol ticker calls).
        if not self._tickers or (now - self._tickers_bulk_ts) >= TICKER_BULK_TTL * 4:
            await self.fetch_all_spot_tickers(force=True)

        # We still need the raw rows for spread / volCcy24h fields, so re-fetch
        # only if the bulk pull failed entirely.
        if not self._tickers:
            ex = set(exclude or [])
            return [s for s in self._discovered if s not in ex][:max_symbols]

        # Pull the raw rows once (cheap; same endpoint). We could persist
        # this but the bulk-tickers call already gives us last/bid/ask/vol.
        try:
            r = await self._client.get(
                f"{OKX_BASE}/api/v5/market/tickers", params={"instType": "SPOT"}
            )
            body: dict | None = None
            try:
                body = r.json()
            except Exception:
                body = None
            if self._is_rate_limit_response(r.status_code, body):
                self._trip_rate_limit("discover_universe")
                ex = set(exclude or [])
                return [s for s in self._discovered if s not in ex][:max_symbols]
            r.raise_for_status()
            if not body or body.get("code") != "0":
                raise RuntimeError(f"OKX error: {(body or {}).get('msg')}")
            rows = body.get("data", []) or []
            self._last_ok_ts = now
            self._consecutive_errors = 0
            self._last_error = ""
        except Exception as e:
            self._consecutive_errors += 1
            self._last_error = f"discover_universe {type(e).__name__}: {e}"
            ex = set(exclude or [])
            return [s for s in self._discovered if s not in ex][:max_symbols]

        ex = set(exclude or [])
        custom_bases = set((b or "").upper() for b in (exclude_bases or []))
        candidates: List[Tuple[str, float, dict]] = []
        for row in rows:
            inst = row.get("instId", "")
            if not inst.endswith("-USDT"):
                continue
            sym = inst.replace("-", "/")
            if sym in ex:
                continue
            base = inst.split("-")[0]
            if self._is_excludable_base(base, custom_bases):
                continue
            try:
                last = float(row.get("last") or 0)
                bid = float(row.get("bidPx") or 0)
                ask = float(row.get("askPx") or 0)
                open24 = float(row.get("open24h") or 0)
                quote_vol = float(row.get("volCcyQuote24h") or row.get("volCcy24h") or 0) * (
                    last if not row.get("volCcyQuote24h") else 1.0
                )
            except Exception:
                continue
            if last <= 0 or bid <= 0 or ask <= 0:
                continue
            spread_pct = (ask - bid) / max(last, 1e-9) * 100
            if spread_pct > 0.5:
                continue
            if quote_vol < min_quote_vol_usdt:
                continue
            # V5.4 — stable-like motion filter. Anything whose 24h price moved
            # less than 0.15% is treated as a peg/stable even if the base name
            # didn't match the blocklist. Real volatile assets move much more.
            if open24 > 0:
                move_24h_pct = abs(last - open24) / open24 * 100
                if move_24h_pct < 0.15 and last < 5:
                    # tiny-priced + nearly-zero move = almost certainly pegged
                    continue
            candidates.append((sym, quote_vol, {
                "last": last,
                "spread_pct": round(spread_pct, 4),
                "quote_vol_usdt": round(quote_vol, 2),
                "move_24h_pct": round((last - open24) / open24 * 100, 3) if open24 > 0 else None,
            }))
        candidates.sort(key=lambda x: x[1], reverse=True)
        picked = [c[0] for c in candidates[:max_symbols]]
        self._discovered = picked
        self._discovered_meta = {c[0]: c[2] for c in candidates[:max_symbols]}
        self._discovered_ts = now
        return list(picked)


market = MarketData()
