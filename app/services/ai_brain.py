"""OpenRouter AI committee.

Four agents — Commander, Scout, Risk, Skeptic — each receive a structured
indicator snapshot and must return strict JSON:

  { "vote": "proceed" | "wait" | "skip",
    "confidence": 0.0-1.0,
    "reason": "...",
    "risk_notes": "..." }

The backend never lets the AI place orders; it only votes. The risk engine
decides whether a paper order opens.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import httpx

from app.core.events import bus
from app.core.settings import EnvFlags, RuntimeSettings


@dataclass
class Verdict:
    agent: str
    model: str
    vote: str            # proceed | wait | skip
    confidence: float
    reason: str
    risk_notes: str
    ok: bool = True
    error: str = ""
    latency_ms: int = 0


AGENT_PROMPTS: Dict[str, str] = {
    "Commander": (
        "You are COMMANDER, lead strategist for an AI paper-trading bot. "
        "You weigh trend, momentum, and breakout context. Be decisive but honest."
    ),
    "Scout": (
        "You are SCOUT, a fast pattern reader. You look for setups with strong "
        "volume confirmation and clean trend stacks. Reject chop."
    ),
    "Risk": (
        "You are RISK, a conservative risk manager. Your job is to vote 'wait' "
        "or 'skip' on anything that looks marginal. Prefer caution. Highlight downside."
    ),
    "Skeptic": (
        "You are SKEPTIC. Stress-test the setup. Look for invalidations, "
        "overbought/oversold extremes, and weak volume. Reject confirmation bias."
    ),
}


def _agent_model(agent: str, s: RuntimeSettings) -> str:
    return {
        "Commander": s.commander_model,
        "Scout": s.scout_model,
        "Risk": s.risk_model,
        "Skeptic": s.skeptic_model,
    }[agent]


def _build_user_prompt(symbol: str, timeframe: str, snapshot: dict) -> str:
    htf = snapshot.get("higher_timeframe") or {}
    htf_line = ""
    if htf:
        htf_line = (
            f"  Higher timeframe ({htf.get('timeframe', 'HTF')}) bias = {htf.get('bias')}, "
            f"close = {float(htf.get('last_close') or 0):.6f}, "
            f"EMA21 = {float(htf.get('ema21') or 0):.6f}, EMA50 = {float(htf.get('ema50') or 0):.6f}, "
            f"RSI = {float(htf.get('rsi14') or 0):.2f}\n"
        )
    return (
        f"Symbol: {symbol}  Timeframe: {timeframe}\n"
        f"Active strategy mode: {snapshot.get('strategy_mode', 'commander_blend')}\n"
        f"Local setup type: {snapshot.get('setup_type', 'unknown')}\n"
        f"Indicator snapshot (transparent, computed locally):\n"
        f"  last_close = {snapshot['last_close']:.6f}\n"
        f"  EMA9 = {snapshot['ema9']:.6f}\n"
        f"  EMA21 = {snapshot['ema21']:.6f}\n"
        f"  EMA50 = {snapshot['ema50']:.6f}\n"
        f"  RSI14 = {snapshot['rsi14']:.2f}\n"
        f"  ATR14 = {snapshot['atr14']:.6f}\n"
        f"  Volume vs 20-bar mean = {snapshot['vol_ratio']:.2f}x\n"
        f"  20-bar high = {snapshot['hi20']:.6f}, low = {snapshot['lo20']:.6f}\n"
        f"  Local-bias = {snapshot['bias']}  signal = {snapshot['signal']}  confidence = {snapshot['confidence']}\n"
        f"{htf_line}"
        f"  Local reasons: {'; '.join(snapshot['reasons'])}\n\n"
        f"You must reply with STRICT JSON only — no prose, no markdown — matching:\n"
        '{"vote": "proceed|wait|skip", "confidence": 0.0-1.0, "reason": "short", "risk_notes": "short"}\n'
        f"You are voting on opening a LONG paper position now using the active strategy mode. "
        f"If the setup does not match that strategy, vote wait or skip."
    )


SYSTEM_GUARDRAIL = (
    "You are part of a committee for a paper-trading bot. You NEVER place orders. "
    "You only return JSON verdicts. If unsure, prefer 'wait' or 'skip'. "
    "No financial advice — this is research/educational simulation."
)


_JSON_RE = re.compile(r"\{[\s\S]*\}")


def _parse_verdict(text: str) -> dict:
    # Try strict first, then extract the first JSON object substring
    try:
        return json.loads(text)
    except Exception:
        m = _JSON_RE.search(text or "")
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}


class AIBrain:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=20.0)
        self._last_status: dict = {"available": False, "reason": "uninitialized"}

    @property
    def status(self) -> dict:
        return dict(self._last_status)

    async def close(self) -> None:
        await self._client.aclose()

    def available(self, env: EnvFlags) -> bool:
        return bool(env.openrouter_api_key)

    async def _call_agent(
        self,
        env: EnvFlags,
        settings: RuntimeSettings,
        agent: str,
        model: str,
        symbol: str,
        timeframe: str,
        snapshot: dict,
    ) -> Verdict:
        if not env.openrouter_api_key:
            return Verdict(agent=agent, model=model, vote="wait", confidence=0.0,
                           reason="AI offline (no OPENROUTER_API_KEY)", risk_notes="",
                           ok=False, error="no_api_key")
        url = f"{env.openrouter_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {env.openrouter_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": env.referer,
            "X-Title": env.app_name,
        }
        body = {
            "model": model,
            "temperature": 0.1,
            "max_tokens": max(80, min(260, int(settings.ai_max_tokens))),
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_GUARDRAIL},
                {"role": "system", "content": AGENT_PROMPTS[agent]},
                {"role": "user", "content": _build_user_prompt(symbol, timeframe, snapshot)},
            ],
        }
        t0 = time.time()
        try:
            timeout = max(3.0, min(20.0, float(settings.ai_timeout_seconds)))
            r = await self._client.post(url, headers=headers, json=body, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            parsed = _parse_verdict(content)
            vote = str(parsed.get("vote", "wait")).lower().strip()
            if vote not in ("proceed", "wait", "skip"):
                vote = "wait"
            conf_raw = parsed.get("confidence", 0.0)
            try:
                conf = float(conf_raw)
            except Exception:
                conf = 0.0
            conf = max(0.0, min(1.0, conf))
            return Verdict(
                agent=agent, model=model,
                vote=vote, confidence=conf,
                reason=str(parsed.get("reason", ""))[:280],
                risk_notes=str(parsed.get("risk_notes", ""))[:280],
                ok=True,
                latency_ms=int((time.time() - t0) * 1000),
            )
        except httpx.HTTPStatusError as e:
            return Verdict(agent=agent, model=model, vote="wait", confidence=0.0,
                           reason="", risk_notes="", ok=False,
                           error=f"HTTP {e.response.status_code}: {e.response.text[:160]}",
                           latency_ms=int((time.time() - t0) * 1000))
        except httpx.TimeoutException:
            timeout = max(3.0, min(20.0, float(settings.ai_timeout_seconds)))
            return Verdict(agent=agent, model=model, vote="wait", confidence=0.0,
                           reason=f"{agent} timed out after {timeout:.0f}s",
                           risk_notes="Slow model skipped for this paper decision.",
                           ok=False,
                           error=f"timeout_after_{timeout:.0f}s",
                           latency_ms=int((time.time() - t0) * 1000))
        except Exception as e:
            return Verdict(agent=agent, model=model, vote="wait", confidence=0.0,
                           reason="", risk_notes="", ok=False,
                           error=f"{type(e).__name__}: {e}",
                           latency_ms=int((time.time() - t0) * 1000))

    async def deliberate(
        self,
        env: EnvFlags,
        settings: RuntimeSettings,
        symbol: str,
        timeframe: str,
        snapshot: dict,
    ) -> dict:
        """Call all 4 agents in parallel and aggregate."""
        agents = ["Commander", "Scout", "Risk", "Skeptic"]
        results: List[Verdict] = await asyncio.gather(*[
            self._call_agent(env, settings, a, _agent_model(a, settings), symbol, timeframe, snapshot)
            for a in agents
        ])
        any_ok = any(v.ok for v in results)
        self._last_status = {
            "available": any_ok and bool(env.openrouter_api_key),
            "reason": "" if any_ok else (results[0].error if results else "unknown"),
            "checked_ts": time.time(),
        }
        # Consensus rule for paper trading:
        # - A WAIT vote should slow the bot down, not cancel three PROCEED votes.
        # - Any confident SKIP vote should still block or downgrade the trade.
        # - The threshold is checked against the average confidence of PROCEED voters,
        #   not diluted by WAIT voters. This keeps paper mode active but still gated.
        proceed_votes = [v for v in results if v.ok and v.vote == "proceed"]
        wait_votes = [v for v in results if v.ok and v.vote == "wait"]
        skip_votes = [v for v in results if v.ok and v.vote == "skip"]
        used = len(proceed_votes) + len(wait_votes) + len(skip_votes)
        proceed_conf = sum(v.confidence for v in proceed_votes) / len(proceed_votes) if proceed_votes else 0.0
        skip_conf = sum(v.confidence for v in skip_votes) / len(skip_votes) if skip_votes else 0.0

        score = 0.0
        for v in results:
            if not v.ok:
                continue
            if v.vote == "proceed":
                score += v.confidence
            elif v.vote == "skip":
                score -= v.confidence
        consensus_conf = (score / used) if used else 0.0
        verdict = "wait"

        if skip_votes and skip_conf >= settings.confidence_threshold:
            verdict = "skip"
        elif (
            len(proceed_votes) >= 2
            and len(proceed_votes) > len(skip_votes)
            and proceed_conf >= settings.confidence_threshold
        ):
            verdict = "proceed"
            consensus_conf = proceed_conf
        elif consensus_conf <= -settings.confidence_threshold:
            verdict = "skip"
        out = {
            "symbol": symbol,
            "timeframe": timeframe,
            # V6.4 — every committee vote in this build is on a LONG (spot
            # buy) candidate setup. There is no short adapter; the UI uses
            # this `side` to label the AI committee card as a candidate
            # direction so operators do not confuse the verdict with an
            # already-open position.
            "side": "long",
            "candidate_side": "long",
            "is_candidate": True,
            # V8 — explicit verdict direction labels for the UI so the
            # operator sees "Chosen: LONG" or "Chosen: NO TRADE" instead of
            # a bare verdict word.
            "direction_code": "LONG" if verdict == "proceed" else "NO_TRADE",
            "direction_label": (
                "LONG (spot buy)" if verdict == "proceed" else "NO TRADE"
            ),
            "consensus": verdict,
            "consensus_confidence": round(consensus_conf, 3),
            "vote_counts": {
                "proceed": len(proceed_votes),
                "wait": len(wait_votes),
                "skip": len(skip_votes),
                "ok": used,
            },
            "proceed_confidence": round(proceed_conf, 3),
            "skip_confidence": round(skip_conf, 3),
            "agents": [v.__dict__ for v in results],
            "snapshot": snapshot,
            "available": self._last_status["available"],
        }
        await bus.publish("ai_verdict", out)
        return out


brain = AIBrain()
