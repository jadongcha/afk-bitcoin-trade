"""
전략 모듈
- RSI + 래리 윌리엄스 변동성 돌파 복합 전략
- 단계별 익절 (5%→30%, 10%→30%, 20%→전액)

[매수 조건] RSI < 45  AND  변동성 돌파  AND  (TREND_FILTER 시) 현재가 > EMA200
[매도 조건] RSI > 70  (RSI_EXIT_ENABLED=True 일 때만) / 손절 / 단계별 익절
"""

import pandas as pd
import numpy as np
import config


# ─────────────────────────────────────────────────────────────
# RSI 계산
# ─────────────────────────────────────────────────────────────
def calc_rsi(closes: pd.Series, period: int = config.RSI_PERIOD) -> pd.Series:
    delta    = closes.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


# ─────────────────────────────────────────────────────────────
# 래리 윌리엄스 변동성 돌파 목표가
# ─────────────────────────────────────────────────────────────
def calc_target_price(df: pd.DataFrame, k: float = config.VOLATILITY_K) -> float:
    prev_high  = float(df["high"].iloc[-2])
    prev_low   = float(df["low"].iloc[-2])
    today_open = float(df["open"].iloc[-1])
    return today_open + (prev_high - prev_low) * k


# ─────────────────────────────────────────────────────────────
# 매수/매도 신호
# ─────────────────────────────────────────────────────────────
def get_signal(df: pd.DataFrame, current_price: float) -> str:
    """
    반환값: 'BUY' | 'SELL' | 'HOLD'

    [버그수정 #1] 변동성 돌파 단독 매수 제거 → RSI AND 변동성 돌파 (둘 다 충족)
    [버그수정 #2] RSI 기준 30 → 45 완화 (30은 너무 희귀해서 신호 거의 안 남)
    """
    rsi_series = calc_rsi(df["close"])
    rsi_now    = float(rsi_series.iloc[-1])
    target     = calc_target_price(df)

    rsi_buy      = rsi_now < 45                        # RSI 완화 기준 (과매도 아니어도 OK)
    vol_breakout = current_price >= target             # 변동성 돌파
    rsi_sell     = config.RSI_EXIT_ENABLED and rsi_now > config.RSI_OVERBOUGHT

    # 추세 필터: 완성된 캔들 기준 EMA200 위에서만 매수
    trend_ok, ema_str = True, ""
    if config.TREND_FILTER:
        ema = float(df["close"].iloc[:-1].ewm(span=config.TREND_EMA, adjust=False).mean().iloc[-1])
        trend_ok = current_price > ema
        ema_str  = f"  EMA{config.TREND_EMA}={ema:,.2f}{'' if trend_ok else '(하락추세→매수금지)'}"

    buy_signal   = rsi_buy and vol_breakout and trend_ok

    print(
        f"    [전략] RSI={rsi_now:.2f}  목표가={target:,.2f}  현재가={current_price:,.2f}{ema_str}"
        + ("  → 📈매수신호!" if buy_signal else "")
        + ("  → 📉매도신호!" if rsi_sell  else "")
    )

    # 매도 우선
    if rsi_sell:
        return "SELL"

    if buy_signal:
        return "BUY"

    return "HOLD"


# ─────────────────────────────────────────────────────────────
# 단계별 익절 / 손절 판단
# ─────────────────────────────────────────────────────────────
def check_exit(entry_price: float, current_price: float, stage: int):
    """
    반환값: (action, sell_fraction, next_stage)
    action: 'STOP_LOSS' | 'TAKE_PROFIT' | 'HOLD'
    """
    if entry_price <= 0:
        return "HOLD", 0.0, stage

    change = (current_price - entry_price) / entry_price

    # 손절
    if change <= -config.STOP_LOSS_PCT:
        return "STOP_LOSS", 1.0, 0

    # 단계별 익절
    stages = config.TAKE_PROFIT_STAGES
    if stage < len(stages):
        target = stages[stage]
        if change >= target["profit_pct"]:
            return "TAKE_PROFIT", target["sell_pct"], stage + 1

    return "HOLD", 0.0, stage


# ─────────────────────────────────────────────────────────────
# 돈치안 추세추종 (일봉)
# ─────────────────────────────────────────────────────────────
def get_donchian_signal(df: pd.DataFrame, in_position: bool) -> dict:
    """
    df: 일봉 (마지막 행 = 오늘 진행 중인 캔들) → 어제 완성된 캔들 종가로 판단
    반환: {"signal": 'BUY'|'SELL'|'HOLD', "close": 어제종가, "hi": 진입선, "lo": 청산선}
    """
    n, m = config.DONCHIAN_ENTRY_DAYS, config.DONCHIAN_EXIT_DAYS
    if len(df) < n + 2:
        return {"signal": "HOLD", "close": 0.0, "hi": 0.0, "lo": 0.0}
    close = float(df["close"].iloc[-2])
    hi    = float(df["high"].iloc[-(n + 2):-2].max())   # 어제 이전 55일 최고가
    lo    = float(df["low"].iloc[-(m + 2):-2].min())    # 어제 이전 20일 최저가
    if in_position:
        signal = "SELL" if close < lo else "HOLD"
    else:
        signal = "BUY" if close > hi else "HOLD"
    return {"signal": signal, "close": close, "hi": hi, "lo": lo}
