"""
Alert engine: computes RRG values for sectors and stocks,
tracks quadrant path history, and returns structured alerts
matching the desired format.
"""
import logging
import sys
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'src'))

from rrg_calculator import RRGCalculator
from sectors import BENCHMARKS
from token_fetcher import get_token_from_symbol
from bot_state import get_all_previous_rrg, save_rrg_state

logger = logging.getLogger(__name__)

MAX_PATH_LENGTH = 4

SECTOR_INDICES = {
    "Nifty 100": "Nifty 100",
    "Nifty 200": "Nifty 200",
    "Nifty 500": "Nifty 500",
    "Nifty Auto": "Nifty Auto",
    "Nifty Bank": "Nifty Bank",
    "Nifty Commodities": "Nifty Commodities",
    "Nifty Consumption": "Nifty Consumption",
    "Nifty CPSE": "Nifty CPSE",
    "Nifty Energy": "Nifty Energy",
    "Nifty Fin Service": "Nifty Fin Service",
    "Nifty FMCG": "Nifty FMCG",
    "Nifty Infra": "Nifty Infra",
    "Nifty IT": "Nifty IT",
    "Nifty Media": "Nifty Media",
    "Nifty Metal": "Nifty Metal",
    "Nifty Mid Select": "Nifty Mid Select",
    "Nifty MNC": "Nifty MNC",
    "Nifty Next 50": "Nifty Next 50",
    "Nifty Pharma": "Nifty Pharma",
    "Nifty PSE": "Nifty PSE",
    "Nifty PSU Bank": "Nifty PSU Bank",
    "Nifty Pvt Bank": "Nifty Pvt Bank",
    "Nifty Realty": "Nifty Realty",
}

RRGConfig = {
    "roc_shift": 10,
    "ema_roc_span": 14,
    "tail_count": 8,
    "period": 200,
    "timeframe": "daily",
    "use_standard_jdk": False,
}

CATEGORY_MAP = {
    "Lagging":   {"emoji": "\U0001f534", "title": "SECTOR BREAKDOWN (MACRO)"},
    "Weakening": {"emoji": "\U0001f7e1", "title": "SECTOR COOLING (MACRO)"},
    "Improving": {"emoji": "\U0001f7e2", "title": "SECTOR RECOVERY (MACRO)"},
    "Leading":   {"emoji": "\U0001f535", "title": "SECTOR LEADING (MACRO)"},
}

def compute_rrg(loader, symbol: str, token: str, benchmark_closes: pd.Series) -> Optional[Tuple[float, float, str]]:
    try:
        df = loader.get(symbol, token)
        if df is None or df.empty:
            return None
    except Exception as e:
        logger.warning(f"Failed to fetch data for {symbol}: {e}")
        return None

    item_closes = df["Close"]
    if item_closes.index.has_duplicates:
        item_closes = item_closes.loc[~item_closes.index.duplicated()]
    if not item_closes.index.is_monotonic_increasing:
        item_closes = item_closes.sort_index()

    common = item_closes.index.intersection(benchmark_closes.index)
    if len(common) < 30:
        return None

    item_aligned = item_closes.loc[common]
    bench_aligned = benchmark_closes.loc[common]

    calc = RRGCalculator(
        roc_shift=RRGConfig["roc_shift"],
        ema_roc_span=RRGConfig["ema_roc_span"],
        use_standard_jdk=RRGConfig["use_standard_jdk"],
    )
    try:
        rs = calc.calculate_rs(item_aligned, bench_aligned)
        mom = calc.calculate_momentum(rs)
    except Exception as e:
        logger.warning(f"RRG calculation failed for {symbol}: {e}")
        return None

    if rs.empty or mom.empty:
        return None

    rs_val = float(rs.iloc[-1])
    mom_val = float(mom.iloc[-1])
    quadrant = calc.get_quadrant(rs_val, mom_val)
    return rs_val, mom_val, quadrant


def compute_sector_rrgs(loader, benchmark_closes: pd.Series) -> Dict[str, dict]:
    results = {}
    for display_name, bench_symbol in SECTOR_INDICES.items():
        token = get_token_from_symbol(bench_symbol)
        if not token:
            continue
        result = compute_rrg(loader, bench_symbol, token, benchmark_closes)
        if result:
            rs_val, mom_val, quadrant = result
            results[display_name] = {
                "rs_ratio": round(rs_val, 2),
                "momentum": round(mom_val, 2),
                "quadrant": quadrant,
            }
    return results


def compute_stock_rrg(loader, symbol: str, benchmark_closes: pd.Series) -> Optional[dict]:
    token = get_token_from_symbol(symbol)
    if not token:
        return None
    result = compute_rrg(loader, symbol, token, benchmark_closes)
    if not result:
        return None
    rs_val, mom_val, quadrant = result
    return {"rs_ratio": round(rs_val, 2), "momentum": round(mom_val, 2), "quadrant": quadrant}


def build_path_string(path: List[dict], current_quadrant: str) -> str:
    """Build path string like 'Weakening (2) -> Lagging (1) -> **Lagging**'."""
    parts = []
    for entry in path:
        parts.append(f"{entry['q']} ({entry['n']})")
    # Last entry is current quadrant, make it bold (telegram markdown)
    if parts:
        last = parts.pop()
        parts.append(f"*{last}*")
    return " -> ".join(parts)


def update_path(prev_path: Optional[List[dict]], current_quadrant: str) -> List[dict]:
    """Update quadrant path history with new quadrant reading."""
    if not prev_path:
        return [{"q": current_quadrant, "n": 1}]

    path = list(prev_path)
    if path[-1]["q"] == current_quadrant:
        path[-1]["n"] += 1
    else:
        path.append({"q": current_quadrant, "n": 1})
        # Keep only last MAX_PATH_LENGTH entries
        if len(path) > MAX_PATH_LENGTH:
            path = path[-MAX_PATH_LENGTH:]
    return path


def detect_alerts(current_data: Dict[str, dict]) -> Dict[str, List[dict]]:
    """
    Compare current RRG data with stored state, track path history,
    and return alerts grouped by category.
    Returns: { "Lagging": [alert_dict, ...], "Weakening": [...], ... }
    """
    previous_all = get_all_previous_rrg()
    # Group alerts by the quadrant they ENTERED (their new quadrant)
    grouped: Dict[str, List[dict]] = {}

    for key, cur in current_data.items():
        prev_data = previous_all.get(key)
        prev_quadrant = prev_data["quadrant"] if prev_data else None
        cur_quadrant = cur["quadrant"]

        prev_path = prev_data.get("path") if prev_data else None
        updated_path = update_path(prev_path, cur_quadrant)

        # Save with updated path
        save_rrg_state(key, cur["rs_ratio"], cur["momentum"], cur_quadrant, updated_path)

        if prev_quadrant is None:
            # First time seeing this item, no alert
            continue

        if cur_quadrant == prev_quadrant:
            continue

        # Build alert for quadrant change
        cat_info = CATEGORY_MAP.get(cur_quadrant)
        if not cat_info:
            continue

        path_str = build_path_string(updated_path, cur_quadrant)

        alert = {
            "name": key,
            "quadrant": cur_quadrant,
            "prev_quadrant": prev_quadrant,
            "emoji": cat_info["emoji"],
            "path_str": path_str,
        }

        grouped.setdefault(cur_quadrant, []).append(alert)

    return grouped


def format_status_table(data: Dict[str, dict]) -> str:
    """RRG data as a formatted status card."""
    quad_emoji = {
        "Leading": "\U0001f7e2",
        "Weakening": "\U0001f7e1",
        "Improving": "\U0001f535",
        "Lagging": "\U0001f534",
    }
    lines = []
    lines.append("\U0001f4ca *RRG Status*")
    lines.append("")
    for name in sorted(data.keys()):
        d = data[name]
        emoji = quad_emoji.get(d["quadrant"], "")
        arrow = "\U0001f4c8" if d["momentum"] > 100 else "\U0001f4c9"
        lines.append(
            f"{emoji} *{name}*  \u2014  RS-Ratio `{d['rs_ratio']:<6.1f}`  "
            f"Momentum `{d['momentum']:<6.1f}`  {arrow} {d['quadrant']}"
        )
    lines.append("")
    return "\n".join(lines)
