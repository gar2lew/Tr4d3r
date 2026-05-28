"""Settings loader. Reads from environment / .env. Never hardcode secrets."""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import List

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass


DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
if not DATA_DIR.exists():
    # Fallback to local ./data when not running in container
    fallback = Path(__file__).resolve().parents[2] / "data"
    fallback.mkdir(parents=True, exist_ok=True)
    DATA_DIR = fallback

SETTINGS_FILE = DATA_DIR / "settings.json"


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except Exception:
        return default


def _env_list(name: str, default: List[str]) -> List[str]:
    raw = os.getenv(name)
    if not raw:
        return list(default)
    return [s.strip() for s in raw.split(",") if s.strip()]


DEFAULT_SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "XRP/USDT",
    "DOGE/USDT",
    "AVAX/USDT",
    "LINK/USDT",
    "ADA/USDT",
]

STRATEGY_MODES = [
    {
        "id": "commander_blend",
        "name": "Commander Blend",
        "tagline": "Balanced AI-ranked setup hunter",
        "description": "Blends trend, pullback, breakout, mean reversion, and volume surge logic, then lets the AI committee challenge the best setup.",
        "risk": "medium",
    },
    {
        "id": "trend_rider",
        "name": "Trend Rider",
        "tagline": "Follows clean EMA momentum",
        "description": "Looks for long entries when EMA9 > EMA21 > EMA50, price holds above EMA21, RSI is not overheated, and volume is healthy.",
        "risk": "medium",
    },
    {
        "id": "pullback_sniper",
        "name": "Pullback Sniper",
        "tagline": "Waits for dips inside an uptrend",
        "description": "Targets trend pullbacks near EMA21 with RSI in a controlled range. Designed to avoid chasing green candles.",
        "risk": "low-medium",
    },
    {
        "id": "breakout_hunter",
        "name": "Breakout Hunter",
        "tagline": "Hunts volume-backed breakouts",
        "description": "Watches for 20-bar high breaks with volume expansion and bullish EMA structure.",
        "risk": "medium-high",
    },
    {
        "id": "mean_reversion",
        "name": "Mean Reversion",
        "tagline": "Buys panic dips, carefully",
        "description": "Looks for oversold pushes near the 20-bar low where RSI is stretched and price may snap back.",
        "risk": "high",
    },
    {
        "id": "volume_surge",
        "name": "Volume Surge",
        "tagline": "Follows unusual participation",
        "description": "Prioritises volume spikes that confirm price reclaiming short-term trend levels.",
        "risk": "medium-high",
    },
    {
        "id": "safe_observer",
        "name": "Safe Observer",
        "tagline": "Very selective paper mode",
        "description": "Only allows the cleanest setups. Good for overnight tests where you want fewer but higher-quality attempts.",
        "risk": "low",
    },
]

STRATEGY_MODE_IDS = {m["id"] for m in STRATEGY_MODES}


@dataclass
class RuntimeSettings:
    """User-tunable settings persisted on disk (data/settings.json)."""
    starting_balance_usdt: float = 100.0
    risk_per_trade_pct: float = 1.0          # % of equity risked per trade
    max_position_pct: float = 25.0           # max % of equity in one position
    stop_loss_pct: float = 1.5               # initial stop loss %
    take_profit_pct: float = 3.0             # initial take profit %
    trailing_stop_pct: float = 1.2           # trailing distance %
    daily_loss_cap_pct: float = 5.0          # halts bot if -5% in a day
    # V5.4 — modest defaults for live-readiness. Previously 12; the user's V5.3
    # session generated 27 trades on a tiny account which felt obviously
    # unrealistic. live-readiness profile clamps this to <= 12 in main.py.
    max_trades_per_day: int = 10
    max_open_positions: int = 1              # one paper position by default
    fee_pct: float = 0.1                     # 0.10% taker
    slippage_pct: float = 0.05               # 0.05% slippage
    timeframe: str = "15m"                   # setup timeframe
    bias_timeframe: str = "1h"               # higher-timeframe trend/bias
    price_refresh_seconds: int = 1           # live ticker watcher cadence
    # V7 — default cadence relaxed from 8s -> 30s. The OKX bulk ticker and
    # candle caches mean the scanner doesn't need to spin every few seconds;
    # 30s gives the AI committee + indicator pipeline time to breathe and
    # makes the "Scanner activity" UI clearer.
    scan_interval_seconds: int = 30          # market/AI scan cadence while bot runs
    ai_timeout_seconds: float = 7.0          # max wait per OpenRouter brain
    ai_max_tokens: int = 140                 # keep replies short and fast
    ride_winners_enabled: bool = True        # TP becomes a milestone; trailing stop protects the runner
    active_strategy: str = "commander_blend"
    execution_mode: str = "paper"            # paper | okx_demo | okx_live
    live_min_closed_trades: int = 30         # Hermes proof required before live unlock
    live_min_win_rate: float = 0.55          # 55%+ closed paper win rate
    live_min_total_pnl_usdt: float = 0.0     # paper P/L must be positive
    live_min_data_quality_score: int = 90    # live data must stay clean
    live_max_order_usdt: float = 10.0        # hard cap for first live/demo order size
    # ----- leverage (paper/demo simulation only) -----
    leverage_enabled: bool = False           # default OFF; spot-only behaviour
    leverage_multiplier: float = 1.0         # 1.0 = no leverage
    leverage_max_multiplier: float = 3.0     # UI cap for paper/demo training
    leverage_liquidation_buffer_pct: float = 80.0  # treat -80% of margin as liquidation
    leverage_extra_min_closed_trades: int = 60     # additional gate when leverage on
    leverage_max_daily_loss_pct: float = 3.0       # stricter daily cap when leveraged
    symbols: List[str] = field(default_factory=lambda: list(DEFAULT_SYMBOLS))
    confidence_threshold: float = 0.6        # AI must vote >= this to allow entry
    indicator_only_mode: bool = False        # when AI key missing, only run if true
    # ----- V5.1 active paper training -----
    # When execution_mode == 'paper', these relaxed rules let the bot take more
    # small paper positions so Hermes has data to learn from. They have NO effect
    # on okx_demo / okx_live: those still require the full training gate.
    active_paper_training: bool = True            # master switch for v5.1 behaviour
    paper_training_min_score: float = 0.42        # local-confidence floor for exploratory paper trades
    paper_training_allow_exploration: bool = True # allow entry when AI unreachable / slow in paper mode
    paper_training_max_daily_trades: int = 24     # extra cap purely for paper training
    paper_training_size_pct: float = 50.0         # % of normal sizing used for exploratory paper entries
    # ----- V5.1 auto-strategy selection -----
    # When true, the scanner evaluates ALL strategies per symbol and Hermes's
    # historical strategy_scores act as a tie-breaker bonus. active_strategy
    # still functions as a manual override when this flag is false.
    auto_strategy_selection: bool = True
    # ----- V5.1 dynamic symbol discovery -----
    # When true, in paper mode, the scanner expands the universe with the most
    # liquid USDT spot symbols from OKX public /tickers. The core `symbols` list
    # is always kept. No fake markets; if OKX is unreachable the discovery is
    # skipped silently.
    dynamic_symbol_discovery: bool = True
    # V5.2 — bulk ticker + candle TTL caches keep a 28-symbol scan well below
    # OKX public rate limits. V7 raised the default to 20 (was 10) so the
    # scanner has a meaningfully larger universe in paper/scanner mode without
    # putting the rate limiter at risk. Combined with max_scan_symbols this
    # caps the full universe size visible to the strategy loop.
    max_dynamic_symbols: int = 20
    dynamic_min_quote_volume_usdt: float = 5_000_000.0  # 24h USDT turnover floor
    # V7 — overall scan universe ceiling (core + discovered). Hard upper bound
    # the strategy applies AFTER discovery. Default 25; clamped to [8, 80].
    max_scan_symbols: int = 25
    # V7 — optional market-intel enrichment provider.
    #   "none"       — fully self-contained on OKX public data (default).
    #   "coingecko"  — best-effort, no-key, free public endpoint enrichment
    #                  used only to rank/decorate the UI. NEVER required for
    #                  real-time fills. If unreachable the app keeps working.
    market_intel_provider: str = "none"
    # V5.2 — exclude obviously-broken-for-trading bases. The market_data layer
    # always applies a hardcoded blocklist (stables/wrapped/staked/gold); this
    # list is appended on top so a user can broaden it without code edits.
    dynamic_exclude_bases: List[str] = field(default_factory=lambda: [
        "PAXG", "XAUT", "BETH", "STETH", "OKSOL", "WBTC", "WETH",
    ])
    # ----- V5.1 "learning sample" paper fallback -----
    # When paper mode + active_paper_training are on AND no candidate clears
    # the paper_training_min_score floor, we may open a TINY exploratory
    # "learning sample" position purely so Hermes has trades to learn from.
    # Hard rules (all enforced in strategy.py):
    #   - PAPER mode only. Never reaches okx_demo / okx_live.
    #   - Best candidate score must be >= learning_sample_min_score.
    #   - At most one open position; respects daily caps and data_quality.
    #   - Size is scaled by learning_sample_size_pct (default 30%).
    learning_sample_enabled: bool = True
    learning_sample_min_score: float = 0.30
    learning_sample_size_pct: float = 30.0      # % of normal risk/notional
    learning_sample_max_per_day: int = 5        # V5.4 lowered from 6 → 5
    # ----- V5.4 REALISTIC PAPER MODE -----
    # Paper profile decides how strict the engine is.
    #   live_readiness — counts toward Hermes gate. Modest trade caps,
    #     cash reserve, cooldowns, NO learning samples, realistic fills
    #     with bid/ask + slippage + fees, exit verification mandatory.
    #   learning — exploratory. Allows learning samples + looser cooldowns.
    #     Trades are marked "not live-proof" and excluded from the gate.
    paper_profile: str = "live_readiness"
    # Reserve a slice of cash so paper account doesn't hit $0.0000 like V5.3.
    # The engine refuses to open if it would push cash below this reserve.
    min_cash_reserve_pct: float = 35.0
    # Hard cap on total equity that can be deployed across all open positions.
    max_capital_in_positions_pct: float = 50.0
    # Cooldowns to avoid overtrading. Defaults are for live_readiness — the
    # learning profile uses fractions of these (see strategy.py).
    global_trade_cooldown_seconds: int = 600        # 10 min between any two entries
    per_symbol_cooldown_seconds: int = 2700         # 45 min before retrying same symbol
    # Realistic fills — when on, market buys lift the ask and sells hit the
    # bid, both then offset by slippage_pct. Fees apply to notional.
    realistic_fills_enabled: bool = True
    # ----- V5.4 EXIT VERIFICATION -----
    # If on (default), every SL/TP/trailing exit must be proven by the live
    # ticker bid/ask OR by the most recent candle's high/low actually reaching
    # the trigger price. Otherwise the exit is suppressed and the position
    # stays open until a real touch is observed. This fixes the V5.3 bug
    # where USDG/USDT closed at TP without the market ever touching it.
    exit_verification_enabled: bool = True
    # ----- V5.4 SYMBOL-ADAPTIVE BEHAVIOUR -----
    # When on, the strategy layer computes a symbol profile (volatility,
    # spread, liquidity, trendiness, 24h move) from live data and uses it
    # to widen/tighten SL/TP, scale size, and filter out unsuitable pairs.
    symbol_adaptive_enabled: bool = True
    # Volatility bands as ATR% of price (15m candles).
    symbol_min_atr_pct: float = 0.08         # below = too dead / pegged
    symbol_max_atr_pct: float = 6.0          # above = ungovernable
    # Spread cap for paper entries (basis points / %).
    symbol_max_spread_pct: float = 0.35
    # Minimum 24h notional turnover for paper entries.
    symbol_min_quote_vol_usdt: float = 1_000_000.0
    # Stablecoin/pegged bases the bot must never trade as a long. This is on
    # top of the dynamic_exclude_bases (which only filters discovery).
    paper_excluded_bases: List[str] = field(default_factory=lambda: [
        "USDG", "USDC", "RLUSD", "DAI", "TUSD", "FDUSD", "PYUSD", "BUSD",
        "USDK", "EURT", "GUSD", "USDD", "USTC",
        "PAXG", "XAUT", "TGOLD",
        "WBTC", "WETH", "BETH", "STETH", "RETH", "WBETH", "CBETH", "WSTETH",
        "OKSOL", "OKBTC", "OKETH",
    ])
    # =============================================================
    # V6 — Tiny OKX Live Tester (disabled by default)
    # =============================================================
    # Master switch. Even with this true, no live orders fire unless
    # OKX credentials are valid, LIVE_TRADING_ACK matches, EXECUTION_MODE
    # is okx_live/okx_demo, and the live readiness gate passes.
    live_tester_enabled: bool = False
    # Hard cap per order in USDT terms. Server clamps to [1, 25] when
    # tester is enabled.
    live_max_order_usdt_tester: float = 5.0
    # Stop-loss equivalent for the tester. The bot tracks the SL price
    # locally and submits a market sell when touched; OKX spot does NOT
    # support a universal native stop in this build.
    live_daily_loss_cap_usdt: float = 3.0
    live_max_trades_per_day: int = 3
    live_one_position_per_symbol: bool = True
    live_max_open_positions: int = 1
    live_require_protective_exit: bool = True
    live_spot_only: bool = True
    # Reserve buffer required on top of the order amount before placing
    # a live buy (covers fees + tiny slippage). Multiplier of order size.
    live_free_reserve_multiplier: float = 1.10
    # Optional override that lets the tester unlock BEFORE the full V5.4
    # eligibility journal has accumulated — for very small first-trade
    # sanity checks. UI exposes this very visibly when active.
    live_tester_override: bool = False
    # V9 — OKX native exchange-attached protective orders (algo / OCO /
    # conditional). Default OFF. When ``live_native_protection_enabled``
    # is true the live tester ADDITIONALLY places exchange-side algo
    # sells after a spot buy fills. The bot-managed watcher remains the
    # primary safety net and stays armed in parallel (never replaced).
    #
    # ``live_native_protection_mode`` selects the algo type:
    #   - "oco"         : one OCO order with attached TP + SL triggers (preferred)
    #   - "conditional" : two separate conditional orders (one TP, one SL)
    #   - "off"         : alias of enabled=false (no native protection)
    #
    # ``live_native_protection_dry_run`` (true) short-circuits the OKX
    # algo call entirely and records a dry-run journal entry instead.
    # Tests use this path; operators normally leave it false.
    live_native_protection_enabled: bool = False
    live_native_protection_mode: str = "oco"
    live_native_protection_dry_run: bool = False
    # V9 — startup reconcile path. When true AND native protection is
    # enabled, on the first event-loop tick the live tester iterates every
    # open position, refreshes its OKX inventory + native algo state, and
    # optionally attempts to attach native protection for positions that
    # are currently unprotected. Never opens new trades. Default OFF so a
    # bot restart cannot quietly start sending OKX orders the operator
    # was not expecting.
    live_native_protection_reconcile_on_startup: bool = False
    # =============================================================
    # V9 — Unattended live-test mode (default OFF).
    # =============================================================
    # When ``live_unattended_mode`` is true the operator has explicitly
    # opted in to leaving the bot running without a human watching. The
    # tester then refuses to open new trades unless every check in the
    # "Unattended readiness" gate passes (native protection must be
    # ENABLED and verified on the exchange, caps must be tiny, kill
    # switch off, etc.). When the elapsed time since
    # ``live_unattended_started_at`` exceeds ``live_unattended_max_hours``
    # the tester auto-disables (live_tester_enabled=false) and surfaces
    # an "unattended expired" reason. This does NOT promise safety —
    # crypto markets can gap, exchange-side algos can fail; this mode is
    # a risk-reduced tiny live test envelope, not safe automation.
    live_unattended_mode: bool = False
    live_unattended_max_hours: float = 120.0  # 5 days
    live_unattended_started_at: float = 0.0   # epoch seconds; 0 = not started
    # When true, on critical failures (native placement fails for any
    # open position, OKX auth fails, too many consecutive API errors)
    # the tester refuses to open new entries until the operator looks at
    # it. Existing positions continue to be watched by both layers.
    live_unattended_stop_new_on_failure: bool = True
    commander_model: str = "openai/gpt-4o-mini"
    scout_model: str = "google/gemini-2.5-flash-lite"
    risk_model: str = "deepseek/deepseek-chat"
    skeptic_model: str = "openai/gpt-4o-mini"

    @classmethod
    def load(cls) -> "RuntimeSettings":
        s = cls()
        # Apply env-driven defaults first
        s.commander_model = os.getenv("OPENROUTER_MODEL_COMMANDER", s.commander_model)
        s.scout_model = os.getenv("OPENROUTER_MODEL_SCOUT", s.scout_model)
        s.risk_model = os.getenv("OPENROUTER_MODEL_RISK", s.risk_model)
        s.skeptic_model = os.getenv("OPENROUTER_MODEL_SKEPTIC", s.skeptic_model)
        s.starting_balance_usdt = _env_float("STARTING_BALANCE_USDT", s.starting_balance_usdt)
        s.timeframe = os.getenv("TIMEFRAME", s.timeframe)
        s.bias_timeframe = os.getenv("BIAS_TIMEFRAME", s.bias_timeframe)
        s.price_refresh_seconds = _env_int("PRICE_REFRESH_SECONDS", s.price_refresh_seconds)
        s.scan_interval_seconds = _env_int("SCAN_INTERVAL_SECONDS", s.scan_interval_seconds)
        s.ai_timeout_seconds = _env_float("AI_TIMEOUT_SECONDS", s.ai_timeout_seconds)
        s.ai_max_tokens = _env_int("AI_MAX_TOKENS", s.ai_max_tokens)
        s.ride_winners_enabled = _env_bool("RIDE_WINNERS_ENABLED", s.ride_winners_enabled)
        s.active_strategy = os.getenv("ACTIVE_STRATEGY", s.active_strategy)
        s.execution_mode = os.getenv("EXECUTION_MODE", s.execution_mode)
        if s.execution_mode not in ("paper", "okx_demo", "okx_live"):
            s.execution_mode = "paper"
        s.live_min_closed_trades = _env_int("LIVE_MIN_CLOSED_TRADES", s.live_min_closed_trades)
        s.live_min_win_rate = _env_float("LIVE_MIN_WIN_RATE", s.live_min_win_rate)
        s.live_min_total_pnl_usdt = _env_float("LIVE_MIN_TOTAL_PNL_USDT", s.live_min_total_pnl_usdt)
        s.live_min_data_quality_score = _env_int("LIVE_MIN_DATA_QUALITY_SCORE", s.live_min_data_quality_score)
        s.live_max_order_usdt = _env_float("LIVE_MAX_ORDER_USDT", s.live_max_order_usdt)
        s.leverage_enabled = _env_bool("LEVERAGE_ENABLED", s.leverage_enabled)
        s.leverage_multiplier = _env_float("LEVERAGE_MULTIPLIER", s.leverage_multiplier)
        s.leverage_max_multiplier = _env_float("LEVERAGE_MAX_MULTIPLIER", s.leverage_max_multiplier)
        s.leverage_liquidation_buffer_pct = _env_float("LEVERAGE_LIQUIDATION_BUFFER_PCT", s.leverage_liquidation_buffer_pct)
        s.leverage_extra_min_closed_trades = _env_int("LEVERAGE_EXTRA_MIN_CLOSED_TRADES", s.leverage_extra_min_closed_trades)
        s.leverage_max_daily_loss_pct = _env_float("LEVERAGE_MAX_DAILY_LOSS_PCT", s.leverage_max_daily_loss_pct)
        if s.active_strategy not in STRATEGY_MODE_IDS:
            s.active_strategy = "commander_blend"
        s.symbols = _env_list("SYMBOLS", s.symbols)
        # V5.1 active paper training env hooks (optional)
        s.active_paper_training = _env_bool("ACTIVE_PAPER_TRAINING", s.active_paper_training)
        s.paper_training_min_score = _env_float("PAPER_TRAINING_MIN_SCORE", s.paper_training_min_score)
        s.paper_training_allow_exploration = _env_bool("PAPER_TRAINING_ALLOW_EXPLORATION", s.paper_training_allow_exploration)
        s.paper_training_max_daily_trades = _env_int("PAPER_TRAINING_MAX_DAILY_TRADES", s.paper_training_max_daily_trades)
        s.paper_training_size_pct = _env_float("PAPER_TRAINING_SIZE_PCT", s.paper_training_size_pct)
        s.auto_strategy_selection = _env_bool("AUTO_STRATEGY_SELECTION", s.auto_strategy_selection)
        s.dynamic_symbol_discovery = _env_bool("DYNAMIC_SYMBOL_DISCOVERY", s.dynamic_symbol_discovery)
        s.max_dynamic_symbols = _env_int("MAX_DYNAMIC_SYMBOLS", s.max_dynamic_symbols)
        s.dynamic_min_quote_volume_usdt = _env_float("DYNAMIC_MIN_QUOTE_VOLUME_USDT", s.dynamic_min_quote_volume_usdt)
        # V7 — scan universe ceiling + optional market-intel provider.
        s.max_scan_symbols = _env_int("MAX_SCAN_SYMBOLS", s.max_scan_symbols)
        s.max_scan_symbols = max(8, min(80, int(s.max_scan_symbols)))
        mip = (os.getenv("MARKET_INTEL_PROVIDER", s.market_intel_provider) or "none").strip().lower()
        if mip not in ("none", "coingecko"):
            mip = "none"
        s.market_intel_provider = mip
        s.dynamic_exclude_bases = _env_list("DYNAMIC_EXCLUDE_BASES", s.dynamic_exclude_bases)
        s.learning_sample_enabled = _env_bool("LEARNING_SAMPLE_ENABLED", s.learning_sample_enabled)
        s.learning_sample_min_score = _env_float("LEARNING_SAMPLE_MIN_SCORE", s.learning_sample_min_score)
        s.learning_sample_size_pct = _env_float("LEARNING_SAMPLE_SIZE_PCT", s.learning_sample_size_pct)
        s.learning_sample_max_per_day = _env_int("LEARNING_SAMPLE_MAX_PER_DAY", s.learning_sample_max_per_day)
        # V5.4
        s.paper_profile = os.getenv("PAPER_PROFILE", s.paper_profile)
        if s.paper_profile not in ("live_readiness", "learning"):
            s.paper_profile = "live_readiness"
        s.min_cash_reserve_pct = _env_float("MIN_CASH_RESERVE_PCT", s.min_cash_reserve_pct)
        s.max_capital_in_positions_pct = _env_float("MAX_CAPITAL_IN_POSITIONS_PCT", s.max_capital_in_positions_pct)
        s.global_trade_cooldown_seconds = _env_int("GLOBAL_TRADE_COOLDOWN_SECONDS", s.global_trade_cooldown_seconds)
        s.per_symbol_cooldown_seconds = _env_int("PER_SYMBOL_COOLDOWN_SECONDS", s.per_symbol_cooldown_seconds)
        s.realistic_fills_enabled = _env_bool("REALISTIC_FILLS_ENABLED", s.realistic_fills_enabled)
        s.exit_verification_enabled = _env_bool("EXIT_VERIFICATION_ENABLED", s.exit_verification_enabled)
        s.symbol_adaptive_enabled = _env_bool("SYMBOL_ADAPTIVE_ENABLED", s.symbol_adaptive_enabled)
        s.symbol_min_atr_pct = _env_float("SYMBOL_MIN_ATR_PCT", s.symbol_min_atr_pct)
        s.symbol_max_atr_pct = _env_float("SYMBOL_MAX_ATR_PCT", s.symbol_max_atr_pct)
        s.symbol_max_spread_pct = _env_float("SYMBOL_MAX_SPREAD_PCT", s.symbol_max_spread_pct)
        s.symbol_min_quote_vol_usdt = _env_float("SYMBOL_MIN_QUOTE_VOL_USDT", s.symbol_min_quote_vol_usdt)
        s.paper_excluded_bases = _env_list("PAPER_EXCLUDED_BASES", s.paper_excluded_bases)
        # V6 — Tiny OKX Live Tester (disabled by default)
        s.live_tester_enabled = _env_bool("LIVE_TESTER_ENABLED", s.live_tester_enabled)
        s.live_max_order_usdt_tester = _env_float("LIVE_MAX_ORDER_USDT", s.live_max_order_usdt_tester)
        # Clamp the tester order cap into [1, 25] regardless of source.
        s.live_max_order_usdt_tester = max(1.0, min(25.0, float(s.live_max_order_usdt_tester)))
        s.live_daily_loss_cap_usdt = _env_float("LIVE_DAILY_LOSS_CAP_USDT", s.live_daily_loss_cap_usdt)
        s.live_daily_loss_cap_usdt = max(0.5, min(25.0, float(s.live_daily_loss_cap_usdt)))
        s.live_max_trades_per_day = _env_int("LIVE_MAX_TRADES_PER_DAY", s.live_max_trades_per_day)
        s.live_max_trades_per_day = max(1, min(10, int(s.live_max_trades_per_day)))
        s.live_one_position_per_symbol = _env_bool("LIVE_ONE_POSITION_PER_SYMBOL", s.live_one_position_per_symbol)
        s.live_max_open_positions = _env_int("LIVE_MAX_OPEN_POSITIONS", s.live_max_open_positions)
        s.live_max_open_positions = max(1, min(3, int(s.live_max_open_positions)))
        s.live_require_protective_exit = _env_bool("LIVE_REQUIRE_PROTECTIVE_EXIT", s.live_require_protective_exit)
        s.live_spot_only = _env_bool("LIVE_SPOT_ONLY", s.live_spot_only)
        # V9 — native protection (opt-in; bot-managed exits stay armed).
        s.live_native_protection_enabled = _env_bool(
            "LIVE_NATIVE_PROTECTION_ENABLED", s.live_native_protection_enabled
        )
        mode = (os.getenv("LIVE_NATIVE_PROTECTION_MODE", s.live_native_protection_mode) or "").strip().lower()
        if mode not in ("oco", "conditional", "off"):
            mode = "oco"
        s.live_native_protection_mode = mode
        s.live_native_protection_dry_run = _env_bool(
            "LIVE_NATIVE_PROTECTION_DRY_RUN", s.live_native_protection_dry_run
        )
        s.live_native_protection_reconcile_on_startup = _env_bool(
            "LIVE_NATIVE_PROTECTION_RECONCILE_ON_STARTUP",
            s.live_native_protection_reconcile_on_startup,
        )
        # V9 — Unattended live-test mode (default OFF).
        s.live_unattended_mode = _env_bool("LIVE_UNATTENDED_MODE", s.live_unattended_mode)
        s.live_unattended_max_hours = _env_float(
            "LIVE_UNATTENDED_MAX_HOURS", s.live_unattended_max_hours
        )
        # Clamp to a sane envelope. 0.25h = 15 minutes minimum so a typo
        # cannot make the timer expire instantly; 168h = 7 days max so an
        # accidental huge value cannot disable the auto-expire path.
        s.live_unattended_max_hours = max(0.25, min(168.0, float(s.live_unattended_max_hours)))
        s.live_unattended_started_at = _env_float(
            "LIVE_UNATTENDED_STARTED_AT", s.live_unattended_started_at
        )
        s.live_unattended_stop_new_on_failure = _env_bool(
            "LIVE_UNATTENDED_STOP_NEW_ON_FAILURE", s.live_unattended_stop_new_on_failure
        )
        s.live_free_reserve_multiplier = _env_float("LIVE_FREE_RESERVE_MULTIPLIER", s.live_free_reserve_multiplier)
        # Always require a known token to unlock the override path.
        _override_token = os.getenv("LIVE_TESTER_OVERRIDE", "").strip()
        s.live_tester_override = (_override_token == "I_UNDERSTAND_THIS_IS_A_TINY_TEST")
        # Then overlay persisted file. NOTE (V6.3): the env-derived
        # ``live_tester_override`` is re-asserted *after* the disk overlay
        # so a stale persisted false (from an earlier paper-only session)
        # cannot silently mask a freshly-set LIVE_TESTER_OVERRIDE env var.
        if SETTINGS_FILE.exists():
            try:
                disk = json.loads(SETTINGS_FILE.read_text())
                for k, v in disk.items():
                    if hasattr(s, k):
                        setattr(s, k, v)
            except Exception:
                pass
        # V6.3 — env wins for the override token; the operator's current
        # shell intent is authoritative over a previously-persisted value.
        s.live_tester_override = (_override_token == "I_UNDERSTAND_THIS_IS_A_TINY_TEST")
        return s

    def save(self) -> None:
        SETTINGS_FILE.write_text(json.dumps(asdict(self), indent=2))

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class EnvFlags:
    """Read-only environment flags. LIVE_TRADING_ENABLED is intentionally ignored."""
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    live_trading_requested: bool = False  # env value (for display only)
    paper_mode_locked: bool = True        # always True in this build
    http_host: str = "0.0.0.0"
    http_port: int = 8787
    app_name: str = "AI Hummingbot Brain"
    referer: str = "https://localhost"


def load_env_flags() -> EnvFlags:
    return EnvFlags(
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
        openrouter_base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip(),
        live_trading_requested=_env_bool("LIVE_TRADING_ENABLED", False),
        paper_mode_locked=True,  # hard lock; not configurable
        http_host=os.getenv("HTTP_HOST", "0.0.0.0"),
        http_port=_env_int("HTTP_PORT", 8787),
        app_name=os.getenv("APP_NAME", "AI Hummingbot Brain"),
        referer=os.getenv("OPENROUTER_REFERER", "https://localhost"),
    )


_lock = threading.Lock()
_cached_settings: RuntimeSettings | None = None


def get_settings() -> RuntimeSettings:
    global _cached_settings
    with _lock:
        if _cached_settings is None:
            _cached_settings = RuntimeSettings.load()
        return _cached_settings


def _clamp_v6_patch(patch: dict) -> dict:
    """V6 safety clamps applied at every settings mutation entry point.

    The HTTP route in app.main also clamps for friendly error messages, but
    these caps are repeated here so the tester is also protected when
    settings are mutated from tests, scripts, or future RPC paths.
    Limits mirror the dataclass defaults defined on RuntimeSettings.
    """
    p = dict(patch)
    try:
        if "live_max_order_usdt_tester" in p and p["live_max_order_usdt_tester"] is not None:
            p["live_max_order_usdt_tester"] = max(1.0, min(25.0, float(p["live_max_order_usdt_tester"])))
        if "live_daily_loss_cap_usdt" in p and p["live_daily_loss_cap_usdt"] is not None:
            p["live_daily_loss_cap_usdt"] = max(0.5, min(25.0, float(p["live_daily_loss_cap_usdt"])))
        if "live_max_trades_per_day" in p and p["live_max_trades_per_day"] is not None:
            p["live_max_trades_per_day"] = max(1, min(10, int(p["live_max_trades_per_day"])))
        if "live_max_open_positions" in p and p["live_max_open_positions"] is not None:
            p["live_max_open_positions"] = max(1, min(3, int(p["live_max_open_positions"])))
        if "live_free_reserve_multiplier" in p and p["live_free_reserve_multiplier"] is not None:
            p["live_free_reserve_multiplier"] = max(1.0, min(2.0, float(p["live_free_reserve_multiplier"])))
    except (TypeError, ValueError):
        # If a caller passes garbage, fall through and let setattr coerce/raise.
        pass
    return p


def update_settings(patch: dict) -> RuntimeSettings:
    global _cached_settings
    with _lock:
        s = _cached_settings or RuntimeSettings.load()
        clamped = _clamp_v6_patch(patch)
        for k, v in clamped.items():
            if hasattr(s, k):
                setattr(s, k, v)
        s.save()
        _cached_settings = s
        return s


def reset_settings() -> RuntimeSettings:
    global _cached_settings
    with _lock:
        _cached_settings = RuntimeSettings()
        # Re-apply env defaults
        _cached_settings = RuntimeSettings.load()
        # But discard any disk values:
        fresh = RuntimeSettings()
        fresh.commander_model = _cached_settings.commander_model
        fresh.scout_model = _cached_settings.scout_model
        fresh.risk_model = _cached_settings.risk_model
        fresh.skeptic_model = _cached_settings.skeptic_model
        fresh.symbols = list(_cached_settings.symbols)
        fresh.timeframe = _cached_settings.timeframe
        fresh.bias_timeframe = _cached_settings.bias_timeframe
        fresh.price_refresh_seconds = _cached_settings.price_refresh_seconds
        fresh.scan_interval_seconds = _cached_settings.scan_interval_seconds
        fresh.ai_timeout_seconds = _cached_settings.ai_timeout_seconds
        fresh.ai_max_tokens = _cached_settings.ai_max_tokens
        fresh.ride_winners_enabled = _cached_settings.ride_winners_enabled
        fresh.active_strategy = _cached_settings.active_strategy
        fresh.execution_mode = _cached_settings.execution_mode
        fresh.live_min_closed_trades = _cached_settings.live_min_closed_trades
        fresh.live_min_win_rate = _cached_settings.live_min_win_rate
        fresh.live_min_total_pnl_usdt = _cached_settings.live_min_total_pnl_usdt
        fresh.live_min_data_quality_score = _cached_settings.live_min_data_quality_score
        fresh.live_max_order_usdt = _cached_settings.live_max_order_usdt
        fresh.leverage_enabled = _cached_settings.leverage_enabled
        fresh.leverage_multiplier = _cached_settings.leverage_multiplier
        fresh.leverage_max_multiplier = _cached_settings.leverage_max_multiplier
        fresh.leverage_liquidation_buffer_pct = _cached_settings.leverage_liquidation_buffer_pct
        fresh.leverage_extra_min_closed_trades = _cached_settings.leverage_extra_min_closed_trades
        fresh.leverage_max_daily_loss_pct = _cached_settings.leverage_max_daily_loss_pct
        fresh.active_paper_training = _cached_settings.active_paper_training
        fresh.paper_training_min_score = _cached_settings.paper_training_min_score
        fresh.paper_training_allow_exploration = _cached_settings.paper_training_allow_exploration
        fresh.paper_training_max_daily_trades = _cached_settings.paper_training_max_daily_trades
        fresh.paper_training_size_pct = _cached_settings.paper_training_size_pct
        fresh.auto_strategy_selection = _cached_settings.auto_strategy_selection
        fresh.dynamic_symbol_discovery = _cached_settings.dynamic_symbol_discovery
        fresh.max_dynamic_symbols = _cached_settings.max_dynamic_symbols
        fresh.dynamic_min_quote_volume_usdt = _cached_settings.dynamic_min_quote_volume_usdt
        fresh.max_scan_symbols = _cached_settings.max_scan_symbols
        fresh.market_intel_provider = _cached_settings.market_intel_provider
        fresh.dynamic_exclude_bases = list(_cached_settings.dynamic_exclude_bases)
        fresh.learning_sample_enabled = _cached_settings.learning_sample_enabled
        fresh.learning_sample_min_score = _cached_settings.learning_sample_min_score
        fresh.learning_sample_size_pct = _cached_settings.learning_sample_size_pct
        fresh.learning_sample_max_per_day = _cached_settings.learning_sample_max_per_day
        fresh.paper_profile = _cached_settings.paper_profile
        fresh.min_cash_reserve_pct = _cached_settings.min_cash_reserve_pct
        fresh.max_capital_in_positions_pct = _cached_settings.max_capital_in_positions_pct
        fresh.global_trade_cooldown_seconds = _cached_settings.global_trade_cooldown_seconds
        fresh.per_symbol_cooldown_seconds = _cached_settings.per_symbol_cooldown_seconds
        fresh.realistic_fills_enabled = _cached_settings.realistic_fills_enabled
        fresh.exit_verification_enabled = _cached_settings.exit_verification_enabled
        fresh.symbol_adaptive_enabled = _cached_settings.symbol_adaptive_enabled
        fresh.symbol_min_atr_pct = _cached_settings.symbol_min_atr_pct
        fresh.symbol_max_atr_pct = _cached_settings.symbol_max_atr_pct
        fresh.symbol_max_spread_pct = _cached_settings.symbol_max_spread_pct
        fresh.symbol_min_quote_vol_usdt = _cached_settings.symbol_min_quote_vol_usdt
        fresh.paper_excluded_bases = list(_cached_settings.paper_excluded_bases)
        # V6 — tester fields
        fresh.live_tester_enabled = _cached_settings.live_tester_enabled
        fresh.live_max_order_usdt_tester = _cached_settings.live_max_order_usdt_tester
        fresh.live_daily_loss_cap_usdt = _cached_settings.live_daily_loss_cap_usdt
        fresh.live_max_trades_per_day = _cached_settings.live_max_trades_per_day
        fresh.live_one_position_per_symbol = _cached_settings.live_one_position_per_symbol
        fresh.live_max_open_positions = _cached_settings.live_max_open_positions
        fresh.live_require_protective_exit = _cached_settings.live_require_protective_exit
        fresh.live_spot_only = _cached_settings.live_spot_only
        fresh.live_native_protection_enabled = _cached_settings.live_native_protection_enabled
        fresh.live_native_protection_mode = _cached_settings.live_native_protection_mode
        fresh.live_native_protection_dry_run = _cached_settings.live_native_protection_dry_run
        fresh.live_native_protection_reconcile_on_startup = _cached_settings.live_native_protection_reconcile_on_startup
        fresh.live_unattended_mode = _cached_settings.live_unattended_mode
        fresh.live_unattended_max_hours = _cached_settings.live_unattended_max_hours
        fresh.live_unattended_started_at = _cached_settings.live_unattended_started_at
        fresh.live_unattended_stop_new_on_failure = _cached_settings.live_unattended_stop_new_on_failure
        fresh.live_free_reserve_multiplier = _cached_settings.live_free_reserve_multiplier
        fresh.live_tester_override = _cached_settings.live_tester_override
        fresh.save()
        _cached_settings = fresh
        return fresh
