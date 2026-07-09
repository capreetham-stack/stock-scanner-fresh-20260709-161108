"""
src/demand_supply.py
=====================
Demand-and-Supply zone detection engine.

Theory
------
• A **Demand Zone** is a price range where buyers (institutions) absorbed
  heavy selling and price moved UP sharply — creating a "base" before a
  rally.  Price tends to revisit and bounce from these zones.

• A **Supply Zone** is the mirror: heavy buying absorbed, price fell away —
  institutions distributing. Price tends to reject from these zones.

Detection method
----------------
1. Find all swing highs / swing lows using a N-bar pivot algorithm.
2. Cluster nearby pivots within DS_CLUSTER_PCT into single zones.
3. Classify each zone as Demand (low region) or Supply (high region) based
   on the candle body percentage and the direction of the move after the zone.
4. Score each zone by:
     - Number of touches (tested and respected)
     - Freshness (untested in last N bars)
     - Width (tight zones are stronger)
5. Return zones sorted by score descending.
"""

import numpy  as np
import pandas as pd
import logging

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config as cfg

logger = logging.getLogger(__name__)


class Zone:
    def __init__(self, zone_type: str, top: float, bottom: float):
        self.zone_type = zone_type   # "demand" or "supply"
        self.top       = top
        self.bottom    = bottom
        self.mid       = (top + bottom) / 2
        self.width_pct = (top - bottom) / self.mid * 100 if self.mid else 0
        self.touches   = 1
        self.fresh     = True        # not yet retested from formation
        self.strength  = 0.0        # composite score

    def contains(self, price: float, buffer_pct: float = cfg.DS_CLUSTER_PCT) -> bool:
        buf = self.mid * buffer_pct / 100
        return (self.bottom - buf) <= price <= (self.top + buf)

    def __repr__(self):
        return (f"Zone({self.zone_type}, {self.bottom:.2f}–{self.top:.2f}, "
                f"touches={self.touches}, fresh={self.fresh}, score={self.strength:.1f})")


class DemandSupplyAnalyzer:
    """Identifies and scores D&S zones from OHLCV data."""

    def __init__(self, lookback: int = cfg.DS_LOOKBACK_DAYS):
        self.lookback = lookback

    # ── pivot detection ───────────────────────────────────────────────────────

    @staticmethod
    def _find_pivots(df: pd.DataFrame, n: int = 3) -> tuple[list[int], list[int]]:
        """
        Returns (pivot_high_indices, pivot_low_indices).
        A pivot high at index i: high[i] is the max over [i-n .. i+n].
        """
        high  = df["high"].values
        low   = df["low"].values
        ph, pl = [], []
        for i in range(n, len(df) - n):
            if high[i] == max(high[i - n: i + n + 1]):
                ph.append(i)
            if low[i]  == min(low[i - n: i + n + 1]):
                pl.append(i)
        return ph, pl

    # ── zone builder ─────────────────────────────────────────────────────────

    def _build_raw_zones(self, df: pd.DataFrame) -> list[Zone]:
        """Build initial Zone objects from pivot swing points."""
        data = df.tail(self.lookback).reset_index(drop=True)
        ph_idx, pl_idx = self._find_pivots(data, n=3)

        raw_zones: list[Zone] = []

        # Supply zones from pivot highs (base-drop formations)
        for idx in ph_idx:
            # Find the candle that formed the base (lowest body in last 3 bars)
            start = max(0, idx - 3)
            segment = data.iloc[start: idx + 1]
            body_low  = segment[["open", "close"]].min(axis=1)
            body_high = segment[["open", "close"]].max(axis=1)
            zone_bottom = float(body_low.min())
            zone_top    = float(body_high.max())
            if zone_top > zone_bottom:
                raw_zones.append(Zone("supply", zone_top, zone_bottom))

        # Demand zones from pivot lows (base-rally formations)
        for idx in pl_idx:
            start = max(0, idx - 3)
            segment = data.iloc[start: idx + 1]
            body_low  = segment[["open", "close"]].min(axis=1)
            body_high = segment[["open", "close"]].max(axis=1)
            zone_bottom = float(body_low.min())
            zone_top    = float(body_high.max())
            if zone_top > zone_bottom:
                raw_zones.append(Zone("demand", zone_top, zone_bottom))

        return raw_zones

    # ── clustering ────────────────────────────────────────────────────────────

    @staticmethod
    def _cluster_zones(raw: list[Zone]) -> list[Zone]:
        """Merge overlapping / nearby zones of the same type."""
        demand_zones = sorted([z for z in raw if z.zone_type == "demand"], key=lambda z: z.mid)
        supply_zones = sorted([z for z in raw if z.zone_type == "supply"], key=lambda z: z.mid)

        def merge_group(zones: list[Zone]) -> list[Zone]:
            if not zones:
                return []
            merged: list[Zone] = [zones[0]]
            for z in zones[1:]:
                last = merged[-1]
                gap  = abs(z.mid - last.mid) / last.mid * 100
                if gap < cfg.DS_CLUSTER_PCT * 2:
                    # merge — expand the zone
                    top    = max(last.top,    z.top)
                    bottom = min(last.bottom, z.bottom)
                    new_z  = Zone(last.zone_type, top, bottom)
                    new_z.touches = last.touches + z.touches
                    merged[-1] = new_z
                else:
                    merged.append(z)
            return merged

        return merge_group(demand_zones) + merge_group(supply_zones)

    # ── freshness check ───────────────────────────────────────────────────────

    @staticmethod
    def _mark_freshness(zones: list[Zone], df: pd.DataFrame) -> list[Zone]:
        """Mark zones as stale if price closed inside them in recent bars."""
        recent_close = df["close"].tail(cfg.DS_FRESHNESS_BARS).values
        for z in zones:
            for price in recent_close:
                if z.bottom <= price <= z.top:
                    z.fresh = False
                    break
        return zones

    # ── scoring ───────────────────────────────────────────────────────────────

    @staticmethod
    def _score_zones(zones: list[Zone], df: pd.DataFrame) -> list[Zone]:
        """Assign composite strength score."""
        for z in zones:
            score = 0.0
            # More touches = stronger zone
            score += min(z.touches * 10, 40)
            # Fresh zones score higher
            score += 30 if z.fresh else 0
            # Tight zones (lower width %) are preferred
            if z.width_pct < 1.5:
                score += 20
            elif z.width_pct < 3.0:
                score += 10
            # Zones that are near 52-week highs/lows score higher
            yearly_high = float(df["high"].tail(252).max())
            yearly_low  = float(df["low"].tail(252).min())
            # Demand near 52w low is strong
            if z.zone_type == "demand" and z.mid < yearly_low * 1.05:
                score += 10
            # Supply near 52w high is strong
            if z.zone_type == "supply" and z.mid > yearly_high * 0.95:
                score += 10

            z.strength = round(score, 1)
        return sorted(zones, key=lambda z: z.strength, reverse=True)

    # ── main public method ────────────────────────────────────────────────────

    def analyze(self, df: pd.DataFrame) -> dict:
        """
        Returns:
        {
            "demand_zones": [Zone, ...],
            "supply_zones": [Zone, ...],
            "nearest_demand": Zone | None,
            "nearest_supply": Zone | None,
            "in_demand_zone": bool,
            "in_supply_zone": bool,
            "demand_proximity_pct": float,
            "supply_proximity_pct": float,
        }
        """
        result = {
            "demand_zones":         [],
            "supply_zones":         [],
            "nearest_demand":       None,
            "nearest_supply":       None,
            "in_demand_zone":       False,
            "in_supply_zone":       False,
            "demand_proximity_pct": 999.0,
            "supply_proximity_pct": 999.0,
        }

        if df.empty or len(df) < 20:
            return result

        raw    = self._build_raw_zones(df)
        zones  = self._cluster_zones(raw)
        zones  = self._mark_freshness(zones, df)
        zones  = self._score_zones(zones, df)

        current_price = float(df["close"].iloc[-1])

        demand_zones = [z for z in zones if z.zone_type == "demand"]
        supply_zones = [z for z in zones if z.zone_type == "supply"]

        result["demand_zones"] = demand_zones
        result["supply_zones"] = supply_zones

        # nearest demand below current price
        below_demand = [z for z in demand_zones if z.top < current_price * 1.02]
        if below_demand:
            nearest_d = min(below_demand, key=lambda z: abs(z.mid - current_price))
            result["nearest_demand"]       = nearest_d
            result["demand_proximity_pct"] = abs(current_price - nearest_d.top) / current_price * 100
            result["in_demand_zone"]       = nearest_d.contains(current_price)

        # nearest supply above current price
        above_supply = [z for z in supply_zones if z.bottom > current_price * 0.98]
        if above_supply:
            nearest_s = min(above_supply, key=lambda z: abs(z.mid - current_price))
            result["nearest_supply"]       = nearest_s
            result["supply_proximity_pct"] = abs(nearest_s.bottom - current_price) / current_price * 100
            result["in_supply_zone"]       = nearest_s.contains(current_price)

        return result

    # ── convenience ───────────────────────────────────────────────────────────

    def is_near_demand(self, ds_result: dict) -> bool:
        """Price is within DS_PROXIMITY_PCT of the nearest demand zone."""
        return ds_result["demand_proximity_pct"] <= cfg.DS_PROXIMITY_PCT

    def is_near_supply(self, ds_result: dict) -> bool:
        return ds_result["supply_proximity_pct"] <= cfg.DS_PROXIMITY_PCT

    def reward_risk(self, ds_result: dict, entry: float, atr: float) -> float:
        """
        Estimate R:R ratio.
        Stop  = entry - 1.5 × ATR
        Target = nearest supply zone bottom
        """
        sl     = entry - cfg.SL_ATR_MULT * atr
        risk   = entry - sl
        if risk <= 0:
            return 0.0
        target = ds_result["nearest_supply"].bottom if ds_result["nearest_supply"] else entry + 2 * risk
        reward = target - entry
        return round(reward / risk, 2)
