import numpy as np
import pandas as pd
# pyrefly: ignore [missing-import]
from scipy.signal import find_peaks
# pyrefly: ignore [missing-import]
import talib


def get_pattern_config(
    df: pd.DataFrame,
    atr_period: int = 14,
) -> dict:
    """
    Returns dynamic pattern detection parameters.

    Returns:
        {
            "peak_prominence": float,
            "peak_distance": int,
            "tolerance": float,
            "allowed_peak_diff": float
        }
    """

    atr = talib.ATR(
        df["high"].values,
        df["low"].values,
        df["close"].values,
        timeperiod=atr_period
    )[-1]

    close = df["close"].iloc[-1]

    return {
        # Price difference allowed between two peaks
        "allowed_peak_diff": max(close * 0.015, atr * 0.5),
        # Percentage tolerance (optional)
        "tolerance": max(0.015, atr / close),
        # Peak must stand out at least this much
        "peak_prominence": max(close * 0.03, atr * 1.5),
        # Minimum candles between peaks
        "peak_distance": max(10, len(df) // 20)
    }



def _find_peaks(df):
    highs = df["high"].values
    config = get_pattern_config(df)
    PEAK_PROMINENCE = config["peak_prominence"]
    PEAK_DISTANCE = config["peak_distance"]

    peaks, _ = find_peaks(
        highs,
        prominence=PEAK_PROMINENCE,
        distance=PEAK_DISTANCE,
    )
    return peaks

def find_pivot_highs(df: pd.DataFrame, left =3, right=3):
    """
    Find pivot highs in the DataFrame.
    A pivot high is defined as a high that is higher than its left and right neighbors.

    Args:
        df (pd.DataFrame): DataFrame containing 'high' column.
        left (int): Number of bars to the left to consider.
        right (int): Number of bars to the right to consider.

    Returns:
        list: List of indices of pivot highs.
    """
    pivot_highs = []
    highs = df['high'].values
    for i in range(left, len(highs) - right):
        current_high = highs[i]
        if (
            current_high == max(highs[i - left:i+right+1]) and
            current_high > max(highs[i-left:i]) and
            current_high > max(highs[i+1:i+right+1])
        ):

            pivot_highs.append(i)
    return pivot_highs





def _find_troughs(df):
    lows = -df["low"].values
    PEAK_PROMINENCE = get_pattern_config(df)["peak_prominence"]
    PEAK_DISTANCE = get_pattern_config(df)["peak_distance"]
    troughs, _ = find_peaks(
        lows,
        prominence=PEAK_PROMINENCE,
        distance=PEAK_DISTANCE,
    )
    return troughs


def detect_double_top(df: pd.DataFrame) -> dict:
    peaks = _find_peaks(df)

    if len(peaks) < 2:
        return {"pattern": "Double Top", "detected": False}
    TOLERANCE = get_pattern_config(df)["tolerance"]
    for i in range(len(peaks) - 2, -1, -1):
        p1 = peaks[i]
        p2 = peaks[i + 1]

        h1 = df.iloc[p1]["high"]
        h2 = df.iloc[p2]["high"]

        if abs(h1 - h2) / max(h1, h2) <= TOLERANCE:
            neckline = df.iloc[p1:p2]["low"].min()

            if df.iloc[-1]["close"] < neckline:
                left_dt = df.iloc[p1]["date"]
                right_dt = df.iloc[p2]["date"]
                left_date_str = left_dt.strftime("%Y-%m-%d %H:%M:%S") if hasattr(left_dt, "strftime") else str(left_dt)
                right_date_str = right_dt.strftime("%Y-%m-%d %H:%M:%S") if hasattr(right_dt, "strftime") else str(right_dt)

                return {
                    "pattern": "Double Top",
                    "detected": True,
                    "left_peak": int(p1),
                    "right_peak": int(p2),
                    "left_peak_date": left_date_str,
                    "right_peak_date": right_date_str,
                    "left_peak_price": float(h1),
                    "right_peak_price": float(h2),
                    "left_peak_price_date": left_date_str,
                    "right_peak_price_date": right_date_str,
                    "neckline": float(neckline),
                }

    return {"pattern": "Double Top", "detected": False}


def detect_double_bottom(df: pd.DataFrame) -> dict:
    troughs = _find_troughs(df)

    if len(troughs) < 2:
        return {"pattern": "Double Bottom", "detected": False}
    TOLERANCE = get_pattern_config(df)["tolerance"]
    for i in range(len(troughs) - 2, -1, -1):
        t1 = troughs[i]
        t2 = troughs[i + 1]

        l1 = df.iloc[t1]["low"]
        l2 = df.iloc[t2]["low"]

        if abs(l1 - l2) / max(l1, l2) <= TOLERANCE:
            neckline = df.iloc[t1:t2]["high"].max()

            if df.iloc[-1]["close"] > neckline:
                left_dt = df.iloc[t1]["date"]
                right_dt = df.iloc[t2]["date"]
                left_date_str = left_dt.strftime("%Y-%m-%d %H:%M:%S") if hasattr(left_dt, "strftime") else str(left_dt)
                right_date_str = right_dt.strftime("%Y-%m-%d %H:%M:%S") if hasattr(right_dt, "strftime") else str(right_dt)

                return {
                    "pattern": "Double Bottom",
                    "detected": True,
                    "left_bottom": int(t1),
                    "right_bottom": int(t2),
                    "left_bottom_date": left_date_str,
                    "right_bottom_date": right_date_str,
                    "left_bottom_price": float(l1),
                    "right_bottom_price": float(l2),
                    "left_bottom_price_date": left_date_str,
                    "right_bottom_price_date": right_date_str,
                    "neckline": float(neckline),
                }

    return {"pattern": "Double Bottom", "detected": False}

def detect_head_shoulders(df):

    peaks = find_pivot_highs(df)
    config = get_pattern_config(df)
    tolerance = config["tolerance"]
    atr = talib.ATR(
        df["high"].values,
        df["low"].values,
        df["close"].values,
        timeperiod=14,
    )[-1]

    confirmed_patterns = []
    forming_patterns = []

    for head in peaks:

        head_high = df.iloc[head]["high"]

        # -----------------------------
        # Find Left Shoulder Candidates
        # -----------------------------

        left_candidates = []

        for ls in peaks:

            if ls >= head:
                break

            ls_high = df.iloc[ls]["high"]

            # Head must be higher
            if head_high <= ls_high + atr * 0.5:
                continue

            left_candidates.append(ls)

        # -----------------------------
        # Find Right Shoulder Candidates
        # -----------------------------

        right_candidates = []

        for rs in peaks:

            if rs <= head:
                continue

            rs_high = df.iloc[rs]["high"]

            if head_high <= rs_high + atr * 0.5:
                continue

            right_candidates.append(rs)

        # -----------------------------
        # Match Shoulders
        # -----------------------------

        for ls in left_candidates:

            ls_high = df.iloc[ls]["high"]

            for rs in right_candidates:

                rs_high = df.iloc[rs]["high"]

                shoulder_diff = abs(ls_high-rs_high)/max(ls_high,rs_high)

                if shoulder_diff > tolerance:
                    continue

                # -------------------------
                # Valleys
                # -------------------------

                left_valley = df.iloc[ls:head]["low"].min()
                right_valley = df.iloc[head:rs]["low"].min()

                neckline = min(left_valley,right_valley)

                latest_close = df.iloc[-1]["close"]

                # -------------------------
                # Score
                # -------------------------

                head_score = (head_high-max(ls_high,rs_high))/atr

                shoulder_score = (
                    1-shoulder_diff/tolerance
                )

                shoulder_score=max(0,shoulder_score)

                head_gap=head-ls
                rs_gap=rs-head

                gap_score=1-abs(head_gap-rs_gap)/max(head_gap,rs_gap)

                gap_score=max(0,gap_score)

                neckline_score=1 if latest_close<neckline else 0

                score=(
                    head_score*35+
                    shoulder_score*30+
                    gap_score*25+
                    neckline_score*10
                )

                pat = {
                    "pattern": "Head & Shoulders",
                    "status": "confirmed" if latest_close < neckline else "forming",
                    "detected": latest_close < neckline,
                    "score": round(score, 2),
                    "left_shoulder": int(ls),
                    "head": int(head),
                    "right_shoulder": int(rs),
                    "left_shoulder_date": df.iloc[ls]["date"].isoformat(),
                    "head_date": df.iloc[head]["date"].isoformat(),
                    "right_shoulder_date": df.iloc[rs]["date"].isoformat(),
                    "left_shoulder_price": float(ls_high),
                    "head_price": float(head_high),
                    "right_shoulder_price": float(rs_high),
                    "neckline": float(neckline),
                    "latest_close": float(latest_close),
                }

                if pat["detected"]:
                    confirmed_patterns.append(pat)
                else:
                    forming_patterns.append(pat)

    # Prioritize confirmed patterns first, sorting chronologically by right shoulder, head, and then score
    if confirmed_patterns:
        confirmed_patterns.sort(key=lambda x: (x["right_shoulder"], x["head"], x["score"]), reverse=True)
        return confirmed_patterns[0]

    # Otherwise return the latest forming pattern
    if forming_patterns:
        forming_patterns.sort(key=lambda x: (x["right_shoulder"], x["head"], x["score"]), reverse=True)
        return forming_patterns[0]

    return {
        "pattern": "Head & Shoulders",
        "detected": False
    }

def detect_bullish_rectangle(df: pd.DataFrame, window: int = 20) -> dict:
    recent = df.tail(window)

    resistance = recent["high"].max()
    support = recent["low"].min()

    width = resistance - support

    if width / support < 0.05:
        if df.iloc[-1]["close"] > resistance:
            return {
                "pattern": "Bullish Rectangle",
                "detected": True,
                "support": support,
                "resistance": resistance,
            }

    return {"pattern": "Bullish Rectangle", "detected": False}


def detect_bearish_rectangle(df: pd.DataFrame, window: int = 20) -> dict:
    recent = df.tail(window)

    resistance = recent["high"].max()
    support = recent["low"].min()

    width = resistance - support

    if width / support < 0.05:
        if df.iloc[-1]["close"] < support:
            return {
                "pattern": "Bearish Rectangle",
                "detected": True,
                "support": support,
                "resistance": resistance,
            }

    return {"pattern": "Bearish Rectangle", "detected": False}


def detect_all_patterns(df: pd.DataFrame):
    detectors = [
        detect_double_top,
        detect_double_bottom,
        detect_head_shoulders,
        detect_bullish_rectangle,
        detect_bearish_rectangle,
    ]

    results = []

    for detector in detectors:
        result = detector(df)
        if result["detected"]:
            results.append(result)

    return results