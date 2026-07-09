"""
src/indicators.py
==================
All technical indicators used by the scanner.

Indicators implemented
──────────────────────
• RSI  (Relative Strength Index)
• MACD (Moving Average Convergence / Divergence)
• Bollinger Bands
• EMA  (9 / 21 / 50 / 200)
• Supertrend
• VWAP  (daily)
• ATR  (Average True Range)
• Volume moving average + surge detection
• Stochastic Oscillator
• ADX  (Average Directional Index — trend strength)
• Support / Resistance (pivot-based)
• Candlestick pattern detection (Hammer, Engulfing, Doji)
"""

import numpy  as np
import pandas as pd
import logging

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config as cfg

logger = logging.getLogger(__name__)


class Indicators:
    """Stateless helper – every method takes a DataFrame and returns a Series or scalar."""

    # ── RSI ───────────────────────────────────────────────────────────────────

    @staticmethod
    def rsi(close: pd.Series, period: int = cfg.RSI_PERIOD) -> pd.Series:
        delta  = close.diff()
        gain   = delta.clip(lower=0)
        loss   = -delta.clip(upper=0)
        avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
        avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
        rs   = avg_gain / avg_loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    # ── MACD ──────────────────────────────────────────────────────────────────

    @staticmethod
    def macd(
        close:  pd.Series,
        fast:   int = cfg.MACD_FAST,
        slow:   int = cfg.MACD_SLOW,
        signal: int = cfg.MACD_SIGNAL,
    ) -> pd.DataFrame:
        """Returns DataFrame with columns: macd, signal, hist."""
        ema_fast = close.ewm(span=fast,   adjust=False).mean()
        ema_slow = close.ewm(span=slow,   adjust=False).mean()
        macd_line    = ema_fast - ema_slow
        signal_line  = macd_line.ewm(span=signal, adjust=False).mean()
        histogram    = macd_line - signal_line
        return pd.DataFrame({
            "macd":   macd_line,
            "signal": signal_line,
            "hist":   histogram,
        })

    # ── Bollinger Bands ───────────────────────────────────────────────────────

    @staticmethod
    def bollinger_bands(
        close:  pd.Series,
        period: int   = cfg.BB_PERIOD,
        std:    float = cfg.BB_STD,
    ) -> pd.DataFrame:
        """Returns DataFrame: upper, mid, lower, %B, bandwidth."""
        mid   = close.rolling(period).mean()
        sigma = close.rolling(period).std(ddof=0)
        upper = mid + std * sigma
        lower = mid - std * sigma
        pct_b = (close - lower) / (upper - lower).replace(0, np.nan)
        bwidth = (upper - lower) / mid.replace(0, np.nan) * 100
        return pd.DataFrame({
            "upper": upper, "mid": mid, "lower": lower,
            "pct_b": pct_b, "bandwidth": bwidth,
        })

    # ── EMA ───────────────────────────────────────────────────────────────────

    @staticmethod
    def ema(close: pd.Series, period: int) -> pd.Series:
        return close.ewm(span=period, adjust=False).mean()

    @staticmethod
    def ema_stack(close: pd.Series) -> pd.DataFrame:
        """Returns all four EMA columns."""
        return pd.DataFrame({
            f"ema{cfg.EMA_SHORT}": Indicators.ema(close, cfg.EMA_SHORT),
            f"ema{cfg.EMA_MID}":   Indicators.ema(close, cfg.EMA_MID),
            f"ema{cfg.EMA_LONG}":  Indicators.ema(close, cfg.EMA_LONG),
            f"ema{cfg.EMA_200}":   Indicators.ema(close, cfg.EMA_200),
        })

    # ── ATR ───────────────────────────────────────────────────────────────────

    @staticmethod
    def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = cfg.ATR_PERIOD) -> pd.Series:
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        return tr.ewm(com=period - 1, min_periods=period).mean()

    # ── Supertrend ────────────────────────────────────────────────────────────

    @staticmethod
    def supertrend(
        high:  pd.Series,
        low:   pd.Series,
        close: pd.Series,
        period: int   = cfg.ATR_PERIOD,
        mult:   float = cfg.SUPERTREND_MULT,
    ) -> pd.DataFrame:
        """Returns DataFrame: supertrend, direction (1=bullish, -1=bearish)."""
        atr_vals = Indicators.atr(high, low, close, period)
        hl2      = (high + low) / 2

        upper_band = hl2 + mult * atr_vals
        lower_band = hl2 - mult * atr_vals

        st        = pd.Series(index=close.index, dtype=float)
        direction = pd.Series(index=close.index, dtype=int)

        for i in range(1, len(close)):
            prev_upper = upper_band.iloc[i - 1]
            prev_lower = lower_band.iloc[i - 1]

            upper_band.iloc[i] = (
                upper_band.iloc[i]
                if upper_band.iloc[i] < prev_upper or close.iloc[i - 1] > prev_upper
                else prev_upper
            )
            lower_band.iloc[i] = (
                lower_band.iloc[i]
                if lower_band.iloc[i] > prev_lower or close.iloc[i - 1] < prev_lower
                else prev_lower
            )

            prev_st  = st.iloc[i - 1] if i > 1 else upper_band.iloc[i]
            if close.iloc[i] > prev_st:
                st.iloc[i]        = lower_band.iloc[i]
                direction.iloc[i] = 1
            else:
                st.iloc[i]        = upper_band.iloc[i]
                direction.iloc[i] = -1

        return pd.DataFrame({"supertrend": st, "direction": direction})

    # ── VWAP ──────────────────────────────────────────────────────────────────

    @staticmethod
    def vwap(
        high:   pd.Series,
        low:    pd.Series,
        close:  pd.Series,
        volume: pd.Series,
    ) -> pd.Series:
        """Daily VWAP. Resets each trading day."""
        tp = (high + low + close) / 3
        cumvol = volume.groupby(volume.index.date).cumsum()
        cumtpvol = (tp * volume).groupby(volume.index.date).cumsum()
        return cumtpvol / cumvol.replace(0, np.nan)

    # ── Volume indicators ─────────────────────────────────────────────────────

    @staticmethod
    def volume_sma(volume: pd.Series, period: int = cfg.VOLUME_AVG_PERIOD) -> pd.Series:
        return volume.rolling(period).mean()

    @staticmethod
    def volume_ratio(volume: pd.Series, period: int = cfg.VOLUME_AVG_PERIOD) -> pd.Series:
        """Current volume / avg volume — ratio > 1 means above-average."""
        return volume / Indicators.volume_sma(volume, period).replace(0, np.nan)

    # ── Volume Profile Point of Control (VPOC) ───────────────────────────────

    @staticmethod
    def vpoc(df_intraday: pd.DataFrame, bins: int = 20) -> float:
        if df_intraday.empty: return 0.0
        price_min = df_intraday['low'].min()
        price_max = df_intraday['high'].max()
        if price_max == price_min: return price_max
        
        bin_size = (price_max - price_min) / bins
        profile = np.zeros(bins)
        
        tp = (df_intraday['high'] + df_intraday['low'] + df_intraday['close']) / 3
        for i in range(len(df_intraday)):
            p = tp.iloc[i]
            v = df_intraday['volume'].iloc[i]
            b = min(int((p - price_min) / bin_size), bins - 1)
            profile[b] += v
            
        max_bin = np.argmax(profile)
        return price_min + (max_bin + 0.5) * bin_size

    # ── Stochastic Oscillator ─────────────────────────────────────────────────

    @staticmethod
    def stochastic(
        high:  pd.Series,
        low:   pd.Series,
        close: pd.Series,
        k_period: int = 14,
        d_period: int = 3,
    ) -> pd.DataFrame:
        lowest_low   = low.rolling(k_period).min()
        highest_high = high.rolling(k_period).max()
        k = 100 * (close - lowest_low) / (highest_high - lowest_low).replace(0, np.nan)
        d = k.rolling(d_period).mean()
        return pd.DataFrame({"stoch_k": k, "stoch_d": d})

    # ── ADX ───────────────────────────────────────────────────────────────────

    @staticmethod
    def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.DataFrame:
        """Returns adx, +DI, -DI."""
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)

        dm_plus  = high.diff().clip(lower=0)
        dm_minus = (-low.diff()).clip(lower=0)

        mask = dm_plus < dm_minus
        dm_plus[mask]  = 0
        mask = dm_minus < dm_plus
        dm_minus[mask] = 0

        tr_smooth   = tr.ewm(com=period - 1,   min_periods=period).mean()
        diplus_sm   = dm_plus.ewm(com=period - 1,  min_periods=period).mean()
        diminus_sm  = dm_minus.ewm(com=period - 1, min_periods=period).mean()

        di_plus  = 100 * diplus_sm  / tr_smooth.replace(0, np.nan)
        di_minus = 100 * diminus_sm / tr_smooth.replace(0, np.nan)
        dx       = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan)
        adx_vals = dx.ewm(com=period - 1, min_periods=period).mean()

        return pd.DataFrame({"adx": adx_vals, "di_plus": di_plus, "di_minus": di_minus})

    # ── Support / Resistance via Pivot Points ─────────────────────────────────

    @staticmethod
    def pivot_points(prev_high: float, prev_low: float, prev_close: float) -> dict:
        """Classic floor-trader pivot points."""
        pp = (prev_high + prev_low + prev_close) / 3
        r1 = 2 * pp - prev_low
        s1 = 2 * pp - prev_high
        r2 = pp + (prev_high - prev_low)
        s2 = pp - (prev_high - prev_low)
        r3 = prev_high + 2 * (pp - prev_low)
        s3 = prev_low  - 2 * (prev_high - pp)
        return {"pp": pp, "r1": r1, "r2": r2, "r3": r3,
                "s1": s1, "s2": s2, "s3": s3}

    # ── Candlestick patterns ───────────────────────────────────────────────────

    @staticmethod
    def detect_patterns(df: pd.DataFrame) -> pd.Series:
        """
        Returns a Series of pattern names for each candle.
        df must have columns: open, high, low, close.
        """
        patterns = pd.Series("none", index=df.index)

        body    = (df["close"] - df["open"]).abs()
        full_rng = df["high"] - df["low"]
        upper_wk = df["high"] - df[["open", "close"]].max(axis=1)
        lower_wk = df[["open", "close"]].min(axis=1) - df["low"]

        # Doji
        doji_mask  = body < 0.1 * full_rng
        patterns[doji_mask] = "doji"

        # Hammer / Hanging man
        hammer_mask = (
            (lower_wk >= 2 * body) &
            (upper_wk <= 0.3 * body) &
            (body > 0)
        )
        patterns[hammer_mask] = "hammer"

        # Shooting star
        star_mask = (
            (upper_wk >= 2 * body) &
            (lower_wk <= 0.3 * body) &
            (body > 0)
        )
        patterns[star_mask] = "shooting_star"

        # Bullish engulfing
        prev_close  = df["close"].shift(1)
        prev_open   = df["open"].shift(1)
        bull_eng = (
            (df["open"]  < prev_close) &
            (df["close"] > prev_open)  &
            (prev_close  < prev_open)  &  # previous was bearish
            (df["close"] > df["open"])    # current is bullish
        )
        patterns[bull_eng] = "bullish_engulfing"

        # Bearish engulfing
        bear_eng = (
            (df["open"]  > prev_close) &
            (df["close"] < prev_open)  &
            (prev_close  > prev_open)  &
            (df["close"] < df["open"])
        )
        patterns[bear_eng] = "bearish_engulfing"

        return patterns

    # ── Summary builder ───────────────────────────────────────────────────────

    @staticmethod
    def compute_all(df: pd.DataFrame) -> pd.DataFrame:
        """
        Adds all indicator columns to df (in-place copy returned).
        df must have: open, high, low, close, volume.
        """
        if df.empty or len(df) < 30:
            return df

        out = df.copy()

        # RSI
        out["rsi"] = Indicators.rsi(out["close"])

        # MACD
        macd_df = Indicators.macd(out["close"])
        out["macd"]        = macd_df["macd"]
        out["macd_signal"] = macd_df["signal"]
        out["macd_hist"]   = macd_df["hist"]

        # Bollinger Bands
        bb_df = Indicators.bollinger_bands(out["close"])
        out["bb_upper"]  = bb_df["upper"]
        out["bb_mid"]    = bb_df["mid"]
        out["bb_lower"]  = bb_df["lower"]
        out["bb_pct_b"]  = bb_df["pct_b"]
        out["bb_bwidth"] = bb_df["bandwidth"]

        # EMAs
        ema_df = Indicators.ema_stack(out["close"])
        for col in ema_df.columns:
            out[col] = ema_df[col]

        # ATR
        out["atr"] = Indicators.atr(out["high"], out["low"], out["close"])

        # Supertrend
        st_df = Indicators.supertrend(out["high"], out["low"], out["close"])
        out["supertrend"]           = st_df["supertrend"]
        out["supertrend_direction"] = st_df["direction"]

        # VWAP (only valid for intraday with time index)
        if hasattr(out.index, "hour"):
            out["vwap"] = Indicators.vwap(out["high"], out["low"], out["close"], out["volume"])
        else:
            out["vwap"] = np.nan

        # Volume
        out["vol_sma"]   = Indicators.volume_sma(out["volume"])
        out["vol_ratio"] = Indicators.volume_ratio(out["volume"])

        # Stochastic
        stoch_df = Indicators.stochastic(out["high"], out["low"], out["close"])
        out["stoch_k"] = stoch_df["stoch_k"]
        out["stoch_d"] = stoch_df["stoch_d"]

        # ADX
        adx_df = Indicators.adx(out["high"], out["low"], out["close"])
        out["adx"]      = adx_df["adx"]
        out["di_plus"]  = adx_df["di_plus"]
        out["di_minus"] = adx_df["di_minus"]

        # Candlestick patterns
        out["pattern"] = Indicators.detect_patterns(out)

        return out

    # ── Convenience signal detectors ──────────────────────────────────────────

    @staticmethod
    def is_rsi_oversold(rsi_val: float) -> bool:
        return rsi_val < cfg.RSI_OVERSOLD

    @staticmethod
    def is_rsi_recovering(rsi_series: pd.Series) -> bool:
        """RSI was oversold 2 bars ago and now trending up."""
        if len(rsi_series) < 3:
            return False
        vals = rsi_series.dropna().tail(3).tolist()
        return vals[0] < cfg.RSI_OVERSOLD and vals[2] > vals[1] > vals[0]

    @staticmethod
    def is_macd_crossover(macd_df: pd.DataFrame) -> bool:
        """MACD line crossed above signal in last 2 bars."""
        if len(macd_df) < 2:
            return False
        prev = macd_df.iloc[-2]
        curr = macd_df.iloc[-1]
        return (prev["macd"] < prev["signal"]) and (curr["macd"] > curr["signal"])

    @staticmethod
    def is_ema_bullish_aligned(row: pd.Series) -> bool:
        """EMA short > mid > long = bullish stack."""
        return (
            row.get(f"ema{cfg.EMA_SHORT}", 0) > row.get(f"ema{cfg.EMA_MID}", 0) >
            row.get(f"ema{cfg.EMA_LONG}", 0)
        )

    @staticmethod
    def is_near_bb_lower(row: pd.Series) -> bool:
        """%B < 0.15 means price near lower band (potential bounce)."""
        return row.get("bb_pct_b", 1.0) < 0.15

    @staticmethod
    def is_supertrend_bullish(row: pd.Series) -> bool:
        return row.get("supertrend_direction", -1) == 1

    @staticmethod
    def is_volume_surge(row: pd.Series) -> bool:
        return row.get("vol_ratio", 0) >= cfg.VOLUME_SURGE_MULT
