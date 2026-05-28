"""Transparent technical indicators. No external TA libraries.

Inputs are OHLCV rows: [ts, open, high, low, close, volume].
"""
from __future__ import annotations

from typing import List, Optional


def ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def sma(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return []
    out = []
    s = sum(values[:period])
    out.append(s / period)
    for i in range(period, len(values)):
        s += values[i] - values[i - period]
        out.append(s / period)
    # Pad to align with input length
    return [out[0]] * (period - 1) + out


def rsi(closes: List[float], period: int = 14) -> List[float]:
    if len(closes) < period + 1:
        return [50.0] * len(closes)
    gains = []
    losses = []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsis = [50.0] * (period)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsis.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsis.append(100 - (100 / (1 + rs)))
    # Pad with first value to match closes length
    return [rsis[0]] * (len(closes) - len(rsis)) + rsis


def atr(candles: List[List[float]], period: int = 14) -> List[float]:
    if len(candles) < 2:
        return [0.0] * len(candles)
    trs = [0.0]
    for i in range(1, len(candles)):
        high = candles[i][2]
        low = candles[i][3]
        prev_close = candles[i - 1][4]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return ema(trs, period)


def _score_candidate(name: str, ok: bool, confidence: float, reasons: list[str], bias: str = "bullish") -> dict:
    # raw_score is preserved even when ok=False so V5.1 can show the best
    # candidate's underlying score in current_task / dashboard reasons.
    raw = round(max(0.0, min(0.95, float(confidence))), 3)
    return {
        "setup_type": name,
        "bias": bias,
        "signal": "buy" if ok else "hold",
        "confidence": round(max(0.0, min(0.95, confidence if ok else 0.0)), 3),
        "raw_score": raw,
        "reasons": reasons,
    }


def analyze(candles: List[List[float]], strategy_mode: str = "commander_blend") -> Optional[dict]:
    """Return a snapshot of indicator state and a transparent signal."""
    if not candles or len(candles) < 60:
        return None
    closes = [c[4] for c in candles]
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]
    vols = [c[5] for c in candles]
    ema_fast = ema(closes, 9)
    ema_mid = ema(closes, 21)
    ema_slow = ema(closes, 50)
    rsi_vals = rsi(closes, 14)
    atr_vals = atr(candles, 14)
    last_close = closes[-1]
    # Recent volume vs 20-bar mean
    vol_window = vols[-20:]
    vol_mean = sum(vol_window) / len(vol_window) if vol_window else 0.0
    vol_ratio = (vols[-1] / vol_mean) if vol_mean > 0 else 1.0
    # 20-bar high/low for breakout/pullback context
    hi20 = max(highs[-20:])
    lo20 = min(lows[-20:])

    rsi_last = rsi_vals[-1]
    atr_pct = (atr_vals[-1] / last_close) * 100 if last_close else 0.0
    ema_spread = abs(ema_fast[-1] - ema_slow[-1]) / max(last_close, 1e-9) * 100
    trend_up = ema_fast[-1] > ema_mid[-1] > ema_slow[-1]
    trend_dn = ema_fast[-1] < ema_mid[-1] < ema_slow[-1]
    price_above_mid = last_close > ema_mid[-1]
    near_ema21 = abs(last_close - ema_mid[-1]) / max(last_close, 1e-9) <= 0.008

    candidates: dict[str, dict] = {}
    candidates["trend_rider"] = _score_candidate(
        "Trend Rider",
        trend_up and price_above_mid and 45 <= rsi_last <= 72 and vol_ratio >= 0.75,
        0.52 + min(0.18, ema_spread / 4) + min(0.14, max(0.0, vol_ratio - 0.75) * 0.1),
        [
            "EMA stack 9>21>50",
            "price holding above EMA21",
            f"RSI controlled at {rsi_last:.1f}",
            f"volume {vol_ratio:.2f}x average",
        ],
    )
    candidates["pullback_sniper"] = _score_candidate(
        "Pullback Sniper",
        trend_up and near_ema21 and 38 <= rsi_last <= 58 and last_close > ema_slow[-1],
        0.58 + min(0.12, max(0.0, 58 - rsi_last) / 200) + min(0.1, vol_ratio * 0.04),
        [
            "uptrend intact",
            "price pulled back near EMA21",
            f"RSI reset zone at {rsi_last:.1f}",
        ],
    )
    candidates["breakout_hunter"] = _score_candidate(
        "Breakout Hunter",
        trend_up and last_close >= hi20 * 0.9985 and vol_ratio >= 1.05 and rsi_last <= 78,
        0.56 + min(0.25, (vol_ratio - 1.0) * 0.16),
        [
            f"testing/breaking 20-bar high {hi20:.4f}",
            f"volume expansion {vol_ratio:.2f}x",
            "bullish EMA structure",
        ],
    )
    candidates["mean_reversion"] = _score_candidate(
        "Mean Reversion",
        last_close <= lo20 * 1.006 and rsi_last <= 38 and not trend_dn and atr_pct > 0.05,
        0.50 + min(0.18, (40 - rsi_last) / 100) + min(0.1, atr_pct / 10),
        [
            f"price stretched near 20-bar low {lo20:.4f}",
            f"RSI oversold at {rsi_last:.1f}",
            "downtrend filter not fully bearish",
        ],
    )
    candidates["volume_surge"] = _score_candidate(
        "Volume Surge",
        vol_ratio >= 1.35 and price_above_mid and 42 <= rsi_last <= 74,
        0.50 + min(0.28, (vol_ratio - 1.0) * 0.14),
        [
            f"unusual volume {vol_ratio:.2f}x",
            "price reclaimed EMA21",
            f"RSI usable at {rsi_last:.1f}",
        ],
    )

    if strategy_mode == "safe_observer":
        base = max(candidates.values(), key=lambda c: c["confidence"])
        selected = dict(base)
        if selected["confidence"] < 0.72:
            selected.update({
                "signal": "hold",
                "confidence": 0.0,
                "setup_type": "Safe Observer",
                "reasons": ["safe observer requires 0.72+ local confidence", *base["reasons"]],
            })
        else:
            selected["setup_type"] = "Safe Observer"
            selected["reasons"] = ["ultra-selective filter passed", *base["reasons"]]
    elif strategy_mode == "commander_blend":
        selected = max(candidates.values(), key=lambda c: c["confidence"])
        if selected["signal"] == "buy":
            selected = dict(selected)
            selected["setup_type"] = f"Commander Blend · {selected['setup_type']}"
            selected["reasons"] = ["best local setup chosen from strategy arsenal", *selected["reasons"]]
    else:
        selected = candidates.get(strategy_mode) or candidates["trend_rider"]

    bias = selected["bias"]
    signal = selected["signal"]
    confidence = selected["confidence"]
    reasons = selected["reasons"]
    setup_type = selected["setup_type"]

    if trend_dn and signal != "buy":
        bias = "bearish"
        reasons = ["EMA stack 9<21<50 — downtrend, no longs", *reasons[:2]]
    elif signal != "buy" and not reasons:
        reasons = ["no clean setup; chop"]

    # Expose per-strategy candidate scores so V5.1 auto-strategy selection can
    # rank strategies across symbols and apply Hermes historical bonuses.
    strategy_candidates = {
        cid: {
            "strategy_id": cid,
            "setup_type": c["setup_type"],
            "signal": c["signal"],
            "confidence": c["confidence"],
            "raw_score": c.get("raw_score", c["confidence"]),
            "reasons": list(c["reasons"]),
        }
        for cid, c in candidates.items()
    }

    return {
        "last_close": last_close,
        "ema9": ema_fast[-1],
        "ema21": ema_mid[-1],
        "ema50": ema_slow[-1],
        "rsi14": rsi_vals[-1],
        "atr14": atr_vals[-1],
        "vol_ratio": vol_ratio,
        "hi20": hi20,
        "lo20": lo20,
        "bias": bias,
        "signal": signal,
        "confidence": round(confidence, 3),
        "reasons": reasons,
        "setup_type": setup_type,
        "strategy_mode": strategy_mode,
        "strategy_candidates": strategy_candidates,
        # The "best_candidate" is the highest-scoring local idea regardless of
        # whether it crossed the buy threshold. Used by current_task transparency.
        "best_candidate": max(
            strategy_candidates.values(),
            key=lambda c: (1 if c["signal"] == "buy" else 0, c["raw_score"]),
            default=None,
        ),
    }
