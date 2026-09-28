"""
futures_trader.py
─────────────────
바이낸스 선물(USDⓈ-M Futures) 자동 거래 봇
- 레버리지 2.5배 / 1회 50 USDT
- LONG / SHORT 동시 운용 가능
- 단계별 익절 + 손절
- 재시작 시 포지션 자동 복구
- 선물 지갑에 1회 증거금 이상 있을 때만 신규 진입 (없으면 대기, 입금되면 자동 시작)
"""
import time
import logging
from decimal import Decimal, ROUND_DOWN
from datetime import datetime

import pandas as pd
from binance.client import Client
from binance.enums import (
    SIDE_BUY, SIDE_SELL,
    FUTURE_ORDER_TYPE_MARKET,
)

from config import (
    API_KEY, SECRET_KEY, TESTNET,
    FUTURES_API_KEY, FUTURES_SECRET_KEY,
    FUTURES_LEVERAGE, FUTURES_TRADE_AMOUNT, FUTURES_MARGIN_TYPE,
    FUTURES_COINS, FUTURES_INTERVAL, FUTURES_SLEEP_SECONDS,
    FUTURES_STOP_LOSS_PCT,
)
from futures_strategy import (
    get_futures_signal, check_futures_exit, analyze_5day_direction,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [FUTURES] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("futures")

# ── 텔레그램 알림 (선택) ─────────────────────────────────
try:
    import requests as _req
    from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

    def send_telegram(msg: str):
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            return
        try:
            _req.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": f"[FUTURES] {msg}"},
                timeout=5,
            )
        except Exception:
            pass
except ImportError:
    def send_telegram(msg: str):
        pass


# ══════════════════════════════════════════════════════════
# 포지션 상태 (심볼별)
# ══════════════════════════════════════════════════════════
# state[symbol] = {
#   "side":         "LONG" | "SHORT" | None,
#   "entry_price":  float,
#   "initial_qty":  float,   # 최초 진입 수량
#   "current_qty":  float,   # 현재 남은 수량
#   "stage":        int,     # 익절 단계
# }
state: dict[str, dict] = {}


# ══════════════════════════════════════════════════════════
# 유틸
# ══════════════════════════════════════════════════════════

def get_client() -> Client:
    if TESTNET:
        client = Client(FUTURES_API_KEY, FUTURES_SECRET_KEY, testnet=True)
        client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"
    else:
        client = Client(FUTURES_API_KEY, FUTURES_SECRET_KEY)
    return client


def get_klines(client: Client, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
    raw = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "qav", "num_trades", "tbbav", "tbqav", "ignore",
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


def get_current_price(client: Client, symbol: str) -> float:
    ticker = client.futures_symbol_ticker(symbol=symbol)
    return float(ticker["price"])


# 심볼별 거래소 주문 규칙: {"step": 수량 단위, "min_qty": 최소 수량, "min_notional": 최소 주문금액}
filters: dict[str, dict] = {}


def load_filters(client: Client):
    """거래소에서 수량 단위 / 최소 수량 / 최소 주문금액을 읽어옴 (실패 시 config 값으로 대체)"""
    try:
        info = client.futures_exchange_info()
        for sym in info["symbols"]:
            if sym["symbol"] not in FUTURES_COINS:
                continue
            f = {x["filterType"]: x for x in sym["filters"]}
            lot = f.get("MARKET_LOT_SIZE") or f.get("LOT_SIZE")
            filters[sym["symbol"]] = {
                "step":         float(lot["stepSize"]),
                "min_qty":      float(lot["minQty"]),
                "min_notional": float(f.get("MIN_NOTIONAL", {}).get("notional", 0)),
            }
    except Exception as e:
        log.warning(f"거래소 주문 규칙 조회 실패 → config 값 사용: {e}")

    for symbol, cfg in FUTURES_COINS.items():
        if symbol not in filters:
            step = 10 ** -cfg["qty_precision"]
            filters[symbol] = {"step": step, "min_qty": step, "min_notional": 0.0}
        fl = filters[symbol]
        log.info(f"{symbol} 주문규칙 | 단위={fl['step']} | 최소수량={fl['min_qty']} | 최소금액={fl['min_notional']} USDT")


def floor_qty(symbol: str, qty: float) -> float:
    """수량을 거래소 단위로 내림 (반올림하면 보유량보다 커지거나 0이 될 수 있음)"""
    if qty <= 0:
        return 0.0
    step = Decimal(str(filters[symbol]["step"]))
    return float((Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step)


# ══════════════════════════════════════════════════════════
# 선물 지갑 잔고 확인 — 돈이 있을 때만 신규 진입
# ══════════════════════════════════════════════════════════
_funded = None   # 마지막 확인 상태 (상태가 바뀔 때만 알림 → 로그 도배 방지)


def get_available_usdt(client: Client):
    """선물 지갑의 사용 가능 USDT. 조회 실패 시 None"""
    try:
        for b in client.futures_account_balance():
            if b["asset"] == "USDT":
                return float(b.get("availableBalance") or b.get("withdrawAvailable") or 0)
        return 0.0
    except Exception as e:
        log.debug(f"선물 잔고 조회 실패: {e}")
        return None


def has_futures_funds(client: Client) -> bool:
    """1회 진입 증거금(+수수료 여유 1%) 이상 있으면 True"""
    global _funded
    avail = get_available_usdt(client)
    need  = FUTURES_TRADE_AMOUNT * 1.01
    ok    = avail is not None and avail >= need

    if ok != _funded:
        if ok:
            msg = f"💰 선물 잔고 {avail:.2f} USDT 확인 → 선물 거래 활성화"
            log.info(msg)
        elif avail is None:
            msg = "⏸ 선물 잔고 조회 실패 (API 키 / 선물 권한 확인) → 신규 진입 중지"
            log.warning(msg)
        else:
            msg = (f"⏸ 선물 잔고 부족 ({avail:.2f} USDT < {need:.2f}) → 신규 진입 중지. "
                   f"입금되면 자동으로 시작합니다")
            log.warning(msg)
        send_telegram(msg)
        _funded = ok
    return ok


# ══════════════════════════════════════════════════════════
# 레버리지 / 마진 초기화
# ══════════════════════════════════════════════════════════

def init_symbol(client: Client, symbol: str):
    """레버리지 설정 및 마진 타입 설정"""
    try:
        client.futures_change_leverage(symbol=symbol, leverage=FUTURES_LEVERAGE)
        log.info(f"{symbol} 레버리지 {FUTURES_LEVERAGE}x 설정 완료")
    except Exception as e:
        log.warning(f"{symbol} 레버리지 설정 실패: {e}")

    try:
        client.futures_change_margin_type(symbol=symbol, marginType=FUTURES_MARGIN_TYPE)
        log.info(f"{symbol} 마진 타입 {FUTURES_MARGIN_TYPE} 설정 완료")
    except Exception as e:
        # 이미 해당 마진 타입이면 에러 무시
        if "No need to change" not in str(e):
            log.warning(f"{symbol} 마진 타입 설정 실패: {e}")


# ══════════════════════════════════════════════════════════
# 포지션 복구 (재시작 시)
# ══════════════════════════════════════════════════════════

def recover_positions(client: Client):
    """바이낸스 서버에서 현재 열린 포지션을 읽어 state 복구"""
    log.info("선물 포지션 복구 시작...")
    try:
        positions = client.futures_position_information()
    except Exception as e:
        log.error(f"포지션 조회 실패: {e}")
        return

    for pos in positions:
        symbol = pos["symbol"]
        if symbol not in FUTURES_COINS:
            continue
        amt = float(pos["positionAmt"])
        if abs(amt) < 1e-8:
            continue

        side         = "LONG" if amt > 0 else "SHORT"
        entry_price  = float(pos["entryPrice"])
        current_qty  = abs(amt)

        state[symbol] = {
            "side":        side,
            "entry_price": entry_price,
            "initial_qty": current_qty,   # 복구 시엔 현재값을 초기값으로
            "current_qty": current_qty,
            "stage":       0,
        }
        log.info(
            f"[복구] {symbol} {side} | 진입가={entry_price:.4f} | 수량={current_qty}"
        )
        send_telegram(
            f"[복구] {symbol} {side} 포지션 | 진입가={entry_price:.4f} | 수량={current_qty}"
        )

    if not any(state):
        log.info("복구할 선물 포지션 없음.")


# ══════════════════════════════════════════════════════════
# 진입 (OPEN)
# ══════════════════════════════════════════════════════════

def open_position(client: Client, symbol: str, side: str) -> bool:
    """
    side: 'LONG' → BUY, 'SHORT' → SELL
    FUTURES_TRADE_AMOUNT USDT 어치 진입
    """
    price     = get_current_price(client, symbol)
    fl        = filters[symbol]

    # 레버리지 적용된 실제 수량 (거래소 단위로 내림)
    notional  = FUTURES_TRADE_AMOUNT * FUTURES_LEVERAGE
    qty       = floor_qty(symbol, notional / price)

    # [버그수정] 최소 수량 / 최소 주문금액 미달이면 주문하지 않음 (거래소가 거절함)
    if qty < fl["min_qty"] or qty * price < fl["min_notional"]:
        log.warning(
            f"{symbol} 진입 불가 — 주문 규모 부족 | 수량={qty} (최소 {fl['min_qty']}) | "
            f"금액={qty * price:.1f} USDT (최소 {fl['min_notional']:.0f}) | "
            f"FUTURES_TRADE_AMOUNT 또는 레버리지를 늘려야 함"
        )
        return False

    binance_side = SIDE_BUY if side == "LONG" else SIDE_SELL
    try:
        order = client.futures_create_order(
            symbol=symbol,
            side=binance_side,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=qty,
        )
    except Exception as e:
        log.error(f"{symbol} 진입 주문 실패: {e}")
        return False

    raw_price = float(order.get("avgPrice") or 0)
    avg_price = raw_price if raw_price > 0 else price
    state[symbol] = {
        "side":        side,
        "entry_price": avg_price,
        "initial_qty": qty,
        "current_qty": qty,
        "stage":       0,
    }
    msg = (
        f"✅ {symbol} {side} 진입 | "
        f"가격={avg_price:.4f} | 수량={qty} | "
        f"레버리지={FUTURES_LEVERAGE}x"
    )
    log.info(msg)
    send_telegram(msg)
    return True


# ══════════════════════════════════════════════════════════
# 청산 (CLOSE)
# ══════════════════════════════════════════════════════════

def close_position(
    client: Client,
    symbol: str,
    close_frac: float,
    reason: str,
) -> bool:
    """close_frac: 청산 비율 (1.0 = 전량)"""
    s         = state.get(symbol)
    if not s or s["side"] is None:
        return False

    if close_frac >= 1.0:
        qty = s["current_qty"]                     # 전량: 보유 수량 그대로
    else:
        qty = floor_qty(symbol, s["current_qty"] * close_frac)
    if qty <= 0:
        return False

    # 반대 사이드로 청산
    binance_side = SIDE_SELL if s["side"] == "LONG" else SIDE_BUY

    try:
        order = client.futures_create_order(
            symbol=symbol,
            side=binance_side,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=qty,
            reduceOnly=True,
        )
    except Exception as e:
        log.error(f"{symbol} 청산 주문 실패: {e}")
        return False

    # 실제 체결가 사용 (avgPrice가 "0"이면 현재가로 대체)
    raw_close = float(order.get("avgPrice") or 0)
    price = raw_close if raw_close > 0 else get_current_price(client, symbol)
    pnl_pct = (
        (price - s["entry_price"]) / s["entry_price"]
        if s["side"] == "LONG"
        else (s["entry_price"] - price) / s["entry_price"]
    ) * 100

    s["current_qty"] = floor_qty(symbol, s["current_qty"] - qty)
    if s["current_qty"] < 1e-8 or close_frac >= 1.0:
        s["side"]         = None
        s["current_qty"]  = 0.0

    msg = (
        f"🔴 {symbol} 청산({reason}) | "
        f"수량={qty} | 가격={price:.4f} | 수익률={pnl_pct:+.2f}%"
    )
    log.info(msg)
    send_telegram(msg)
    return True


# ══════════════════════════════════════════════════════════
# 코인별 처리
# ══════════════════════════════════════════════════════════

def process_symbol(client: Client, symbol: str):
    df    = get_klines(client, symbol, FUTURES_INTERVAL)
    price = get_current_price(client, symbol)
    s     = state.get(symbol, {})

    # ── 포지션 보유 중 ──────────────────────────────────
    if s.get("side"):
        action, frac, next_stage = check_futures_exit(
            side          = s["side"],
            entry_price   = s["entry_price"],
            current_price = price,
            stage         = s["stage"],
            initial_qty   = s["initial_qty"],
        )
        if action == "PARTIAL_CLOSE" and \
                floor_qty(symbol, s["current_qty"] * frac) < filters[symbol]["min_qty"]:
            # [버그수정] 예: BTC 0.001개의 30% = 0.0003 → 최소 단위(0.001) 미만이라 주문 불가
            log.warning(
                f"{symbol} {s['stage']+1}단계 분할청산 불가 (수량이 최소 단위 미만) "
                f"→ 이 단계는 건너뛰고 다음 단계/손절로 관리"
            )
            state[symbol]["stage"] = next_stage
            return
        if action in ("PARTIAL_CLOSE", "CLOSE", "STOP_LOSS"):
            # [버그수정 #6] close_position 반환값 확인 → 주문 실패 시 stage 올리지 않음
            success = close_position(client, symbol, frac, action)
            # 전량 청산 성공 시 side=None → 아래 조건 False → stage 업데이트 불필요
            if success and state.get(symbol, {}).get("side") is not None:
                state[symbol]["stage"] = next_stage
        else:
            pnl_pct = (
                (price - s["entry_price"]) / s["entry_price"]
                if s["side"] == "LONG"
                else (s["entry_price"] - price) / s["entry_price"]
            ) * 100
            log.info(
                f"{symbol} {s['side']} 보유 중 | "
                f"진입={s['entry_price']:.4f} | 현재={price:.4f} | "
                f"수익률={pnl_pct:+.2f}% | 단계={s['stage']}"
            )
        return

    # ── 포지션 없음 → 신호 확인 ─────────────────────────
    analysis = analyze_5day_direction(df)
    signal   = get_futures_signal(df, price)

    log.info(
        f"{symbol} 신호={signal} | 방향점수={analysis['score']} | "
        f"RSI={analysis['rsi']} | 추세={analysis['trend']} | 현재가={price:.4f}"
    )

    if signal in ("LONG", "SHORT"):
        # 진입 직전에 잔고 재확인 (BTC 진입으로 잔고가 줄었을 수 있음)
        if not has_futures_funds(client):
            log.info(f"{symbol} {signal} 신호 있으나 선물 잔고 부족 → 진입 안 함")
            return
        state.setdefault(symbol, {})
        open_position(client, symbol, signal)


# ══════════════════════════════════════════════════════════
# 메인 루프
# ══════════════════════════════════════════════════════════

def run():
    log.info("=== 바이낸스 선물 봇 시작 ===")
    coins = " / ".join(FUTURES_COINS.keys())
    mode = "테스트넷" if TESTNET else "실거래"
    send_telegram(
        f"🚀 선물 봇 시작\n"
        f"모드: {mode}\n"
        f"코인: {coins}\n"
        f"레버리지: {FUTURES_LEVERAGE}x | 1회: {FUTURES_TRADE_AMOUNT} USDT\n"
        f"익절: +3%→30% / +6%→30% / +12%→전액\n"
        f"손절: -{FUTURES_STOP_LOSS_PCT*100:.0f}%"
    )
    client = get_client()

    # 심볼 초기화 (레버리지·마진 설정)
    for symbol in FUTURES_COINS:
        init_symbol(client, symbol)

    # 거래소 주문 규칙 로딩 (수량 단위 / 최소 수량 / 최소 금액)
    load_filters(client)

    # 포지션 복구 (잔고와 상관없이 기존 포지션은 항상 관리 — 손절/익절)
    recover_positions(client)

    # 시작 시 선물 잔고 상태 알림
    has_futures_funds(client)

    while True:
        # 매 주기 잔고 확인 → 입금/출금 시 자동으로 켜짐/꺼짐 (상태 바뀔 때만 알림)
        has_futures_funds(client)
        for symbol in FUTURES_COINS:
            try:
                process_symbol(client, symbol)
            except KeyboardInterrupt:
                log.info("선물 봇 종료 (Ctrl+C)")
                raise
            except Exception as e:
                log.error(f"[{symbol}] 처리 오류: {e}", exc_info=True)

        try:
            time.sleep(FUTURES_SLEEP_SECONDS)
        except KeyboardInterrupt:
            log.info("선물 봇 종료 (Ctrl+C)")
            break


if __name__ == "__main__":
    run()
