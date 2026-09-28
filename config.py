import os
from dotenv import load_dotenv

load_dotenv()

# ── 바이낸스 API ──────────────────────────────────────
API_KEY    = os.getenv("BINANCE_API_KEY", "")
SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")
TESTNET    = os.getenv("TESTNET", "True").lower() == "true"

# ── 거래 코인 목록 ────────────────────────────────────
COINS = {
    "BTCUSDT": {
        "asset":          "BTC",
        "trade_amount":   float(os.getenv("BTC_AMOUNT_USDT", "100")),
        "qty_precision":  5,
    },
    "ETHUSDT": {
        "asset":          "ETH",
        "trade_amount":   float(os.getenv("ETH_AMOUNT_USDT", "100")),
        "qty_precision":  4,
    },
}

# ── RSI 설정 ──────────────────────────────────────────
RSI_PERIOD       = 14
RSI_OVERSOLD     = 30
RSI_OVERBOUGHT   = 70

# ── 래리 윌리엄스 변동성 돌파 설정 ───────────────────
VOLATILITY_K     = 0.5

# ── 현물 전략 스위치 (2년 백테스트 결과로 결정, 2026-09) ──
# TREND_FILTER    : 1시간봉 EMA200 위에서만 매수 (하락장 매수 차단)
# RSI_EXIT_ENABLED: RSI>70 전량매도 사용 여부 — 수익을 너무 일찍 잘라서 끔
#                   (예전 방식으로 되돌리려면 TREND_FILTER=False, RSI_EXIT_ENABLED=True)
TREND_FILTER     = True
TREND_EMA        = 200
RSI_EXIT_ENABLED = False

# ── 돈치안 추세추종 (현물 롱, 일봉 기준) ──────────────
# 어제 일봉 종가가 직전 55일 최고가 돌파 → 매수
# 어제 일봉 종가가 직전 20일 최저가 이탈 → 전량 매도 (별도 손절 없음)
# 위 RSI 전략과 자금·기록을 따로 써서 동시에 운용 (spot_donchian_*.json)
# 백테스트(코인당 50 USDT): 2020 +116 / 2021 +133 / 2022 -26 / 2023 +21 / 2024.9~2026.9 +51
DONCHIAN_ENABLED     = True
DONCHIAN_ENTRY_DAYS  = 55
DONCHIAN_EXIT_DAYS   = 20
DONCHIAN_AMOUNT_USDT = float(os.getenv("DONCHIAN_AMOUNT_USDT", "50"))   # 코인당 1회 매수 금액

# ── 단계별 익절 전략 ──────────────────────────────────
# profit_pct : 진입가 대비 수익률 도달 시
# sell_pct   : 최초 매수 수량 대비 매도 비율
TAKE_PROFIT_STAGES = [
    {"profit_pct": 0.05, "sell_pct": 0.30},   # 5%  수익 → 30% 매도
    {"profit_pct": 0.10, "sell_pct": 0.30},   # 10% 수익 → 30% 추가 매도
    {"profit_pct": 0.20, "sell_pct": 1.00},   # 20% 수익 → 잔여 전액 매도
]

# ── 손절 설정 ─────────────────────────────────────────
STOP_LOSS_PCT    = 0.03   # 손절 3%  ← 실제 손절값

# ── 텔레그램 ──────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── 봇 실행 주기 ──────────────────────────────────────
INTERVAL         = "1h"
SLEEP_SECONDS    = 60

# ── 선물(Futures) 설정 ────────────────────────────────
FUTURES_LEVERAGE       = 2            # 레버리지 배수 (바이낸스는 정수만 허용 — 2.5 넣으면 실제로는 2로 설정됐음)
FUTURES_TRADE_AMOUNT   = 100.0        # 1회 거래 금액 (USDT) — BTC 최소 주문금액(100 USDT) 충족 위해 100으로
FUTURES_MARGIN_TYPE    = "ISOLATED"   # ISOLATED or CROSSED

# 선물 거래 코인 목록
FUTURES_COINS = {
    "BTCUSDT": {"qty_precision": 3},
    "ETHUSDT": {"qty_precision": 3},
}

# 선물 단계별 익절
FUTURES_TAKE_PROFIT_STAGES = [
    {"profit_pct": 0.03, "close_pct": 0.30},   # 3%  수익 → 30% 청산
    {"profit_pct": 0.06, "close_pct": 0.30},   # 6%  수익 → 30% 청산
    {"profit_pct": 0.12, "close_pct": 1.00},   # 12% 수익 → 전량 청산
]

FUTURES_STOP_LOSS_PCT  = 0.03   # 손절 3%

# 3일 캔들 분석 주기
FUTURES_CANDLE_DAYS    = 3
FUTURES_INTERVAL       = "4h"   # 신호 분석용 캔들 주기
FUTURES_SLEEP_SECONDS  = 60

# ── 선물 전용 API 키 (testnet.binancefuture.com에서 발급) ─
FUTURES_API_KEY    = os.getenv("FUTURES_API_KEY", API_KEY)
FUTURES_SECRET_KEY = os.getenv("FUTURES_SECRET_KEY", SECRET_KEY)
