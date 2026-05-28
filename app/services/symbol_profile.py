"""V5.4 — Symbol-adaptive profile.

Computes a compact "profile" for a trading symbol from LIVE OKX data only
(no fabricated numbers). The strategy layer uses the profile to:

  - reject pegged / stable-like / illiquid / impossibly-wide-spread pairs
    BEFORE any AI call or paper entry,
  - adapt the stop-loss / take-profit / trailing distances to the symbol's
    own volatility (so we don't apply a flat 1.5% stop to a 6% ATR meme coin),
  - scale down size when the symbol is risky (volatile/wide/thin),
  - surface a short, honest reason for the UI like
    "ICP profile: liquid · trending · ATR 0.9% · spread 0.04% → SL ×1.0 size ×1.0".

The profile is a pure dataclass — no I/O, no globals. Build it from the
already-fetched ticker + candle snapshot.

It deliberately does NOT touch order placement; the engine still owns risk
gates. It only produces ratios and labels.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional


@dataclass
class SymbolProfile:
    symbol: str
    # raw measurements
    last: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    spread_pct: float = 0.0
    atr_pct: float = 0.0
    quote_vol_24h_usdt: float = 0.0
    move_24h_pct: float = 0.0
    bias: str = "unknown"
    rsi14: float = 50.0
    vol_ratio: float = 1.0
    candles_count: int = 0
    # derived labels (short, human-readable for UI/journal)
    volatility_label: str = "unknown"   # dead | calm | normal | hot | wild
    liquidity_label: str = "unknown"    # thin | ok | deep
    trend_label: str = "unknown"        # choppy | mixed | trending
    spread_label: str = "unknown"       # tight | wide | very_wide
    # adaptive multipliers (1.0 = no change). All paper-only.
    sl_mult: float = 1.0
    tp_mult: float = 1.0
    trail_mult: float = 1.0
    size_mult: float = 1.0
    # entry verdict
    tradeable: bool = True
    block_reasons: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def short_summary(self) -> str:
        bits = [
            self.volatility_label,
            self.liquidity_label,
            self.trend_label,
            f"ATR {self.atr_pct:.2f}%",
            f"spread {self.spread_pct:.3f}%",
            f"vol24h ${_compact(self.quote_vol_24h_usdt)}",
        ]
        if self.sl_mult != 1.0 or self.tp_mult != 1.0 or self.size_mult != 1.0:
            bits.append(
                f"SL\u00d7{self.sl_mult:.2f} TP\u00d7{self.tp_mult:.2f} "
                f"size\u00d7{self.size_mult:.2f}"
            )
        return " \u00b7 ".join(bits)

    def to_dict(self) -> dict:
        return asdict(self)


def _compact(n: float) -> str:
    n = float(n or 0.0)
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return f"{n:.0f}"


def _label_volatility(atr_pct: float, min_atr: float, max_atr: float) -> str:
    if atr_pct <= 0:
        return "unknown"
    if atr_pct < min_atr:
        return "dead"
    if atr_pct < 0.25:
        return "calm"
    if atr_pct < 1.5:
        return "normal"
    if atr_pct < max_atr:
        return "hot"
    return "wild"


def _label_liquidity(quote_vol_24h: float, min_vol: float) -> str:
    if quote_vol_24h <= 0:
        return "unknown"
    if quote_vol_24h < min_vol:
        return "thin"
    if quote_vol_24h < min_vol * 20:
        return "ok"
    return "deep"


def _label_spread(spread_pct: float, max_spread: float) -> str:
    if spread_pct <= 0:
        return "unknown"
    if spread_pct <= max_spread * 0.5:
        return "tight"
    if spread_pct <= max_spread:
        return "wide"
    return "very_wide"


def _label_trend(bias: str, vol_ratio: float, rsi: float) -> str:
    if bias in ("bullish", "bearish") and vol_ratio >= 1.1:
        return "trending"
    if 45 <= rsi <= 55 and 0.8 <= vol_ratio <= 1.2:
        return "choppy"
    return "mixed"


def build_profile(
    symbol: str,
    ticker: Optional[dict],
    snapshot: Optional[dict],
    *,
    min_atr_pct: float = 0.08,
    max_atr_pct: float = 6.0,
    max_spread_pct: float = 0.35,
    min_quote_vol_usdt: float = 1_000_000.0,
    excluded_symbol: bool = False,
) -> SymbolProfile:
    """Build a SymbolProfile from live ticker + analyzer snapshot.

    ``ticker``  expected keys: last, bid, ask, vol24h, high24h, low24h, open24h
    ``snapshot`` expected keys: atr14, rsi14, vol_ratio, bias, last_close,
                                higher_timeframe, candles_count (optional)
    """
    p = SymbolProfile(symbol=symbol)
    t = ticker or {}
    s = snapshot or {}
    p.last = float(t.get("last") or s.get("last_close") or 0.0)
    p.bid = float(t.get("bid") or p.last)
    p.ask = float(t.get("ask") or p.last)
    if p.last > 0 and p.bid > 0 and p.ask > 0 and p.ask >= p.bid:
        p.spread_pct = (p.ask - p.bid) / p.last * 100
    open24 = float(t.get("open24h") or 0.0)
    if open24 > 0 and p.last > 0:
        p.move_24h_pct = (p.last - open24) / open24 * 100
    # ATR% — strategy layer passes a snapshot from indicators.analyze().
    atr = float(s.get("atr14") or 0.0)
    if atr > 0 and p.last > 0:
        p.atr_pct = (atr / p.last) * 100
    p.rsi14 = float(s.get("rsi14") or 50.0)
    p.vol_ratio = float(s.get("vol_ratio") or 1.0)
    p.bias = str(s.get("bias") or "unknown")
    p.candles_count = int(s.get("candles_count") or 0)
    # OKX vol24h is in BASE coins; convert to quote using last.
    base_vol = float(t.get("vol24h") or 0.0)
    p.quote_vol_24h_usdt = base_vol * p.last if base_vol and p.last else 0.0

    # labels
    p.volatility_label = _label_volatility(p.atr_pct, min_atr_pct, max_atr_pct)
    p.liquidity_label = _label_liquidity(p.quote_vol_24h_usdt, min_quote_vol_usdt)
    p.spread_label = _label_spread(p.spread_pct, max_spread_pct)
    p.trend_label = _label_trend(p.bias, p.vol_ratio, p.rsi14)

    # block reasons
    if excluded_symbol:
        p.block_reasons.append("base on stable/wrapped/pegged blocklist")
    if p.last <= 0:
        p.block_reasons.append("no price")
    if p.spread_pct > max_spread_pct:
        p.block_reasons.append(f"spread {p.spread_pct:.3f}% > cap {max_spread_pct:.3f}%")
    if p.atr_pct > 0 and p.atr_pct < min_atr_pct:
        p.block_reasons.append(f"ATR {p.atr_pct:.3f}% < min {min_atr_pct:.3f}% (pegged-like)")
    if p.atr_pct > max_atr_pct:
        p.block_reasons.append(f"ATR {p.atr_pct:.2f}% > max {max_atr_pct:.2f}% (ungovernable)")
    if p.quote_vol_24h_usdt > 0 and p.quote_vol_24h_usdt < min_quote_vol_usdt:
        p.block_reasons.append(
            f"24h turnover ${_compact(p.quote_vol_24h_usdt)} < min ${_compact(min_quote_vol_usdt)}"
        )
    # 24h almost flat AND in a typical stablecoin price range = peg
    if -0.2 < p.move_24h_pct < 0.2 and 0.5 < p.last < 5:
        p.block_reasons.append(f"24h move {p.move_24h_pct:.2f}% near zero (likely pegged)")

    p.tradeable = not p.block_reasons

    # Adaptive multipliers — only when tradeable. Tuned conservatively so
    # the engine never gets *more* aggressive on risky pairs.
    if p.tradeable and p.atr_pct > 0:
        # Stop & TP widen with volatility but capped.
        # Baseline: 1.0% ATR ~ multiplier 1.0. We use the ratio relative to
        # a 1% ATR reference, gently.
        ratio = p.atr_pct / 1.0
        ratio = max(0.5, min(3.0, ratio))
        # Volatile pairs: wider SL/TP, smaller size.
        if p.volatility_label == "calm":
            p.sl_mult = 0.85
            p.tp_mult = 0.85
            p.trail_mult = 0.85
            p.size_mult = 1.0
            p.notes.append("calm vol \u2192 tighter SL/TP")
        elif p.volatility_label == "normal":
            p.sl_mult = 1.0
            p.tp_mult = 1.0
            p.trail_mult = 1.0
        elif p.volatility_label == "hot":
            p.sl_mult = min(2.0, 1.0 + 0.5 * (ratio - 1.0))
            p.tp_mult = min(2.0, 1.0 + 0.5 * (ratio - 1.0))
            p.trail_mult = min(1.8, 1.0 + 0.4 * (ratio - 1.0))
            p.size_mult = 0.7
            p.notes.append("hot vol \u2192 wider SL/TP, smaller size")
        # widen on wide spread too
        if p.spread_label == "wide":
            p.sl_mult = max(p.sl_mult, 1.15)
            p.size_mult *= 0.85
            p.notes.append("wide spread \u2192 smaller size")
        # cut size on thin liquidity
        if p.liquidity_label == "thin":
            p.size_mult *= 0.6
            p.notes.append("thin liquidity \u2192 smaller size")
        # choppy market: discourage trend-style sizing
        if p.trend_label == "choppy":
            p.size_mult *= 0.8
            p.notes.append("choppy \u2192 smaller size")

        # Final clamps so multipliers stay sane.
        p.sl_mult = max(0.5, min(2.5, p.sl_mult))
        p.tp_mult = max(0.5, min(2.5, p.tp_mult))
        p.trail_mult = max(0.5, min(2.5, p.trail_mult))
        p.size_mult = max(0.2, min(1.0, p.size_mult))

    return p


def preferred_strategy_for_profile(profile: SymbolProfile, default: str) -> str:
    """Suggest a strategy family that matches the profile's character.

    Used by auto-strategy selection as a soft hint, NOT an override. Returns
    the default when the profile doesn't strongly suggest anything.
    """
    if not profile.tradeable:
        return default
    if profile.trend_label == "trending" and profile.volatility_label in ("normal", "hot"):
        # Strong directional with healthy vol \u2192 trend / breakout work best.
        if profile.vol_ratio >= 1.4:
            return "breakout_hunter"
        return "trend_rider"
    if profile.trend_label == "choppy" and profile.volatility_label in ("normal", "hot"):
        return "mean_reversion"
    if profile.bias == "bullish" and profile.rsi14 <= 55:
        return "pullback_sniper"
    if profile.vol_ratio >= 1.8:
        return "volume_surge"
    return default
