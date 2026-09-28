"""
futures_strategy.py
──────────────────
바이낸스 선물 전용 전략 모듈
- 5일치 캔들 분석으로 LONG / SHORT 방향 결정
- RSI + 추세(EMA) + 변동성 돌파 복합 신호
"""
import pandas as pd
import numpy as np
from config import (
    RSI_PERIOD, RSI_OVERSOLD, RSI_OVERBOUGHT,
    VOLATILITY_K, FUTURES_CANDLE_DAYS,
    FUTURES_TAKE_PROFIT_STAGES, FUTURES_STOP_LOSS_PCT,
)


# ── RSI 계산 ────────────────────────────────────────────
def calc_rsi(closes: pd.Series, period: int = RSI_PERIOD) -> float:
    delta = closes.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_g = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_l = loss.ewm(com=period - 1, min_periods=period).mean()
    rs    = avg_g / avg_l.replace(0, np.nan)
    rsi   = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


# ── EMA 추세 분석 ────────────────────────────────────────
def calc_ema_trend(closes: pd.Series, short: int = 12, long: int = 26) -> str:
    """EMA 12 > EMA 26 → 상승추세(BULL), 반대 → 하락추세(BEAR)"""
    ema_s = closes.ewm(span=short, adjust=False).mean().iloc[-1]
    ema_l = closes.ewm(span=long,  adjust=False).mean().iloc[-1]
    return "BULL" if ema_s > ema_l else "BEAR"


# ── 5일 캔들 종합 방향 분석 ─────────────────────────────
def analyze_5day_direction(df: pd.DataFrame) -> dict:
    """
    최근 FUTURES_CANDLE_DAYS 일의 캔들을 분석해
    종합 방향(LONG / SHORT / NEUTRAL)과 신호 강도를 반환.

    df 컬럼: open, high, low, close, volume (float)
    """
    days = FUTURES_CANDLE_DAYS
    recent = df.tail(days * 6)          # 4h 캔들 기준 분석 구간

    # [버그수정] EMA는 전체 df(200개)로 계산해야 수렴값이 정확함
    # recent(18개)로 EMA26 계산 시 신뢰할 수 없는 값 반환
    closes_full   = df["close"].astype(float)       # EMA 계산용 (전체)
    closes = recent["close"].astype(float)          # RSI / 비율 분석용
    highs  = recent["high"].astype(float)
    lows   = recent["low"].astype(float)

    # [버그수정] RSI도 전체 df로 계산 (18개로는 워밍업 부족 → 값 불안정)
    rsi       = calc_rsi(closes_full)
    trend     = calc_ema_trend(closes_full)         # 전체 데이터로 EMA 계산

    # 최근 5일 캔들 중 상승 / 하락 봉 비율
    bull_bars = (recent["close"] > recent["open"]).sum()
    total_bars = len(recent)
    bull_ratio = bull_bars / total_bars if total_bars else 0.5

    # 최근 고점·저점 대비 현재가 위치
    high_max   = float(highs.max())
    low_min    = float(lows.min())
    current    = float(closes.iloc[-1])
    range_pct  = (current - low_min) / (high_max - low_min + 1e-9)

    # 점수 산정: +1 LONG 신호, -1 SHORT 신호
    score = 0

    # RSI
    if rsi < RSI_OVERSOLD:      score += 2   # 강한 LONG
    elif rsi < 45:               score += 1
    elif rsi > RSI_OVERBOUGHT:   score -= 2   # 강한 SHORT
    elif rsi > 55:               score -= 1

    # EMA 추세
    if trend == "BULL":   score += 1
    else:                  score -= 1

    # 상승/하락 봉 비율
    if bull_ratio > 0.6:   score += 1
    elif bull_ratio < 0.4: score -= 1

    # 가격 위치
    if range_pct < 0.35:   score += 1   # 저점권 → LONG 유리
    elif range_pct > 0.65: score -= 1   # 고점권 → SHORT 유리

    if score >= 2:
        direction = "LONG"
    elif score <= -2:
        direction = "SHORT"
    else:
        direction = "NEUTRAL"

    return {
        "direction": direction,
        "score":     score,
        "rsi":       round(rsi, 2),
        "trend":     trend,
        "bull_ratio": round(bull_ratio, 2),
        "range_pct":  round(range_pct, 2),
    }


# ── 변동성 돌파 목표가 (선물용) ─────────────────────────
def calc_futures_target(df: pd.DataFrame, k: float = VOLATILITY_K) -> float:
    """당일 시가 + 전일 변동폭 * K"""
    if len(df) < 2:
        return 0.0
    prev      = df.iloc[-2]
    today_open = float(df.iloc[-1]["open"])
    range_prev = float(prev["high"]) - float(prev["low"])
    return today_open + range_prev * k


# ── SHORT용 하락 목표가 ─────────────────────────────────
def calc_futures_short_target(df: pd.DataFrame, k: float = VOLATILITY_K) -> float:
    """당일 시가 - 전일 변동폭 * K  (하락 돌파 기준)"""
    if len(df) < 2:
        return float("inf")
    prev       = df.iloc[-2]
    today_open = float(df.iloc[-1]["open"])
    range_prev = float(prev["high"]) - float(prev["low"])
    return today_open - range_prev * k


# ── 최종 진입 신호 ───────────────────────────────────────
def get_futures_signal(df: pd.DataFrame, current_price: float) -> str:
    """
    'LONG' / 'SHORT' / 'HOLD'
    [버그수정 #4] SHORT 방향에 하락 돌파 목표가 별도 계산
    LONG  : 현재가 >= 시가 + 변동폭*K  (상승 돌파)
    SHORT : 현재가 <= 시가 - 변동폭*K  (하락 돌파)
    """
    analysis = analyze_5day_direction(df)
    direction = analysis["direction"]

    if direction == "NEUTRAL":
        return "HOLD"

    if direction == "LONG":
        target = calc_futures_target(df)
        if target > 0 and current_price >= target:
            return "LONG"

    elif direction == "SHORT":
        short_target = calc_futures_short_target(df)
        if short_target < float("inf") and current_price <= short_target:
            return "SHORT"

    return "HOLD"


# ── 선물 청산 조건 확인 ──────────────────────────────────
def check_futures_exit(
    side: str,           # 'LONG' or 'SHORT'
    entry_price: float,
    current_price: float,
    stage: int,
    initial_qty: float = 0.0,   # 현재 미사용 (향후 확장용)
) -> tuple:
    """
    returns: (action, close_fraction, next_stage)
    action: 'PARTIAL_CLOSE' | 'CLOSE' | 'STOP_LOSS' | 'HOLD'
    """
    if side == "LONG":
        pnl_pct = (current_price - entry_price) / entry_price
    else:  # SHORT
        pnl_pct = (entry_price - current_price) / entry_price

    # 손절
    if pnl_pct <= -FUTURES_STOP_LOSS_PCT:
        return ("STOP_LOSS", 1.0, stage)

    # 단계별 익절
    if stage < len(FUTURES_TAKE_PROFIT_STAGES):
        s = FUTURES_TAKE_PROFIT_STAGES[stage]
        if pnl_pct >= s["profit_pct"]:
            frac = s["close_pct"]
            if frac >= 1.0 or stage == len(FUTURES_TAKE_PROFIT_STAGES) - 1:
                return ("CLOSE", 1.0, stage + 1)
            return ("PARTIAL_CLOSE", frac, stage + 1)

    return ("HOLD", 0.0, stage)
