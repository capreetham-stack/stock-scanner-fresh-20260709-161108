"""
NSE Stock Scanner — Configuration
All thresholds, watchlists, and strategy parameters live here.
"""

# ─── Watchlist ────────────────────────────────────────────────────────────────
# NIFTY 50 symbols (NSE format)
NIFTY50 = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "HINDUNILVR", "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK",
    "LT", "AXISBANK", "ASIANPAINT", "MARUTI", "SUNPHARMA",
    "TITAN", "BAJFINANCE", "WIPRO", "ONGC", "NTPC",
    "POWERGRID", "ULTRACEMCO", "TECHM", "HCLTECH", "INDUSINDBK",
    "TATAMOTORS", "TATASTEEL", "JSWSTEEL", "ADANIENT", "ADANIPORTS",
    "BAJAJFINSV", "COALINDIA", "DIVISLAB", "DRREDDY", "EICHERMOT",
    "GRASIM", "HEROMOTOCO", "HINDALCO", "M&M", "NESTLEIND",
    "SBILIFE", "SHREECEM", "TATACONSUM", "BPCL", "CIPLA",
    "BRITANNIA", "HDFCLIFE", "UPL", "APOLLOHOSP", "LT"
]

# Additional F&O stocks worth scanning
FNO_EXTRAS = [
    "NIFTY", "BANKNIFTY", "FINNIFTY",
    "PIDILITIND", "BERGEPAINT", "MCDOWELL-N", "GODREJCP",
    "BOSCHLTD", "HAVELLS", "PAGEIND", "MUTHOOTFIN", "CHOLAFIN",
    "BANDHANBNK", "IDFCFIRSTB", "PNB", "CANBK", "FEDERALBNK",
    "RECLTD", "PFC", "SAIL", "NMDC", "NATIONALUM",
    "AUROPHARMA", "BIOCON", "TORNTPHARM", "ALKEM", "IPCALAB",
    "MINDTREE", "MPHASIS", "LTTS", "COFORGE", "PERSISTENT",
    "ABCAPITAL", "SBICARD", "MANAPPURAM", "LICHSGFIN",
]

# Full watchlist used for scanning
WATCHLIST = list(dict.fromkeys(NIFTY50 + FNO_EXTRAS))  # deduplicated

# ─── Time Settings ────────────────────────────────────────────────────────────
MARKET_OPEN_TIME  = "09:15"
PRE_MARKET_START  = "09:45"
SCAN_CUTOFF_TIME  = "09:14"   # Scanner must finish by this time
MID_DAY_SCAN_TIME = "12:15"   # 12:15 PM IST Mid-day run

# ─── Technical Indicator Parameters ──────────────────────────────────────────
RSI_PERIOD          = 14
RSI_OVERSOLD        = 35       # below this → bullish signal
RSI_OVERBOUGHT      = 65       # above this → sell / avoid
RSI_NEUTRAL_LOW     = 40
RSI_NEUTRAL_HIGH    = 60

MACD_FAST           = 12
MACD_SLOW           = 26
MACD_SIGNAL         = 9

BB_PERIOD           = 20
BB_STD              = 2.0

EMA_SHORT           = 9
EMA_MID             = 21
EMA_LONG            = 50
EMA_200             = 200

ATR_PERIOD          = 14
SUPERTREND_MULT     = 3.0

VWAP_DEVIATION_PCT  = 0.5     # within ±0.5 % of VWAP = near-VWAP
VWAP_PULLBACK_PCT   = 0.5     # strict pullback zone for high-conviction entries
VWAP_CHASE_PCT      = 1.2     # above this from VWAP = chase risk

# ─── Volume Analysis ──────────────────────────────────────────────────────────
VOLUME_AVG_PERIOD   = 20
VOLUME_SURGE_MULT   = 1.5     # today's vol > 1.5× avg ⇒ surge
RVOL_HIGH_CONVICTION = 1.5    # strict RVOL floor for high-conviction entries

# ─── Market-Down Safety Rules ────────────────────────────────────────────────
MARKET_DOWN_MIN_STOCK_GAIN_PCT = 0.5  # stock must still be up at least this much when market is down
MARKET_DOWN_MIN_RVOL          = 1.5  # require strong relative volume on down-market picks
MARKET_DOWN_MAX_WARNINGS      = 1    # limit negative caution flags in down-market conditions
MARKET_DOWN_SECTOR_BONUS      = 10   # extra weight for resilient sectors when market is weak

# ─── Trend Strength (ADX) ───────────────────────────────────────────────────
ADX_STRONG_MIN      = 25      # ADX must be above this

# ─── Demand / Supply Zone Parameters ─────────────────────────────────────────
DS_LOOKBACK_DAYS    = 60      # candles to look back for zones
DS_ZONE_STRENGTH    = 3       # min touches to call it a strong zone
DS_CLUSTER_PCT      = 0.5     # price within 0.5 % → same zone cluster
DS_FRESHNESS_BARS   = 5       # untested in last N bars = fresh zone
DS_PROXIMITY_PCT    = 1.0     # stock within 1 % of zone → trigger

# ─── Gap Analysis ─────────────────────────────────────────────────────────────
GAP_UP_PCT          = 0.5     # gap ≥ 0.5 % = meaningful gap-up
GAP_DOWN_PCT        = -0.5    # gap ≤ -0.5 % = meaningful gap-down

# ─── Signal Scoring Weights ───────────────────────────────────────────────────
SCORE_WEIGHTS = {
    "rsi_oversold":         15,
    "rsi_recovering":       10,
    "macd_crossover":       15,
    "macd_positive":         8,
    "ema_alignment":        12,   # short > mid > long
    "price_above_vwap":      8,
    "bollinger_bounce":     10,
    "demand_zone_near":     15,
    "volume_surge":         10,
    "supertrend_bullish":   10,
    "gap_up":                5,
    "support_bounce":        7,
    "prev_day_high_break":   8,
    "delivery_pct_high":     7,
    "buy_pressure":         12,

    # --- Hourly / Intraday Strategy Weights ---
    "1h_trend_aligned":     25,
    "15m_vwap_support":     15,
    "5m_breakout_vol":      20,
    "orb_vpoc_bullish":     15,
    "liquidity_sweep_trap": -20,
    "1030_reversal":        15,
}

MIN_SCORE_TO_BUY    = 35      # only recommend stocks scoring ≥ this
TOP_N_STOCKS        = 10      # number of top picks to display

# ─── Risk Management ──────────────────────────────────────────────────────────
DEFAULT_RISK_PCT    = 0.5     # risk 0.5 % of capital per trade
SL_ATR_MULT         = 1.5     # stop-loss = entry - 1.5 × ATR
TARGET_RR           = 2.0     # minimum reward : risk ratio
RR_STRICT_MIN       = 1.5     # strict minimum R:R for actionable entries

# ─── Data Settings ────────────────────────────────────────────────────────────
HISTORICAL_DAYS     = 100     # days of OHLCV to fetch
INTRADAY_INTERVAL   = "5m"    # intraday candle size
DATA_CACHE_DIR      = "logs/cache"

# ─── Output / Logging ─────────────────────────────────────────────────────────
LOG_FILE            = "logs/scanner.log"
OUTPUT_CSV          = "output/buy_signals.csv"
OUTPUT_JSON         = "output/buy_signals.json"
CONSOLE_TOP_N       = 10
