"""
멀티코인 자동 트레이더 (BTC + ETH)
- 단계별 분할 익절 (5%→30%, 10%→30%, 20%→전액)
- 봇이 산 수량만 관리 (spot_state.json 저장 → 재시작 시 복구)
- RSI + 래리 윌리엄스 변동성 돌파 복합 전략
- 손절 3% 자동 처리
- 텔레그램 알림 (선택)
"""

import json
import os
import time
from decimal import Decimal, ROUND_DOWN

import requests
import pandas as pd
from datetime import datetime
from binance.client import Client
from binance.exceptions import BinanceAPIException

import config
import strategy

# 봇이 산 수량을 기록하는 파일 (재시작 시 복구용)
# [버그수정] 테스트넷 / 실거래 기록을 분리 — 같은 파일을 쓰면 테스트넷 포지션을
#            실계좌 포지션으로 착각해 원래 보유 코인을 팔 수 있음
_BASE_DIR         = os.path.dirname(os.path.abspath(__file__))
STATE_FILE        = os.path.join(_BASE_DIR, f"spot_state_{'testnet' if config.TESTNET else 'live'}.json")
LEGACY_STATE_FILE = os.path.join(_BASE_DIR, "spot_state.json")   # 예전 공용 파일 (테스트넷에서만 쓰였음)
DONCHIAN_STATE_FILE = os.path.join(_BASE_DIR, f"spot_donchian_{'testnet' if config.TESTNET else 'live'}.json")
MIN_ORDER_USDT = 5.0   # 바이낸스 현물 최소 주문금액 — 이보다 작은 잔량은 먼지로 간주


# ─────────────────────────────────────────────────────────────
# 텔레그램 알림
# ─────────────────────────────────────────────────────────────
def send_telegram(msg: str):
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": config.TELEGRAM_CHAT_ID, "text": msg}, timeout=5)
    except Exception as e:
        print(f"  [텔레그램 오류] {e}")


# ─────────────────────────────────────────────────────────────
# 바이낸스 클라이언트 초기화
# ─────────────────────────────────────────────────────────────
def init_client() -> Client:
    client = Client(config.API_KEY, config.SECRET_KEY, testnet=config.TESTNET)
    if config.TESTNET:
        client.API_URL = "https://testnet.binance.vision/api"
    return client


# ─────────────────────────────────────────────────────────────
# 캔들 데이터 가져오기
# ─────────────────────────────────────────────────────────────
def get_ohlcv(client: Client, symbol: str, interval: str, limit: int = 1000) -> pd.DataFrame:  # EMA200 계산에 충분한 캔들
    raw = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(raw, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","qav","trades","tbav","tbqv","ignore"
    ])
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    return df


# ─────────────────────────────────────────────────────────────
# 잔고 / 현재가 조회
# ─────────────────────────────────────────────────────────────
def get_asset_balance(client: Client, asset: str) -> float:
    bal = client.get_asset_balance(asset=asset)
    return float(bal["free"]) if bal else 0.0

def get_usdt_balance(client: Client) -> float:
    return get_asset_balance(client, "USDT")

def get_price(client: Client, symbol: str) -> float:
    return float(client.get_symbol_ticker(symbol=symbol)["price"])


# ─────────────────────────────────────────────────────────────
# 수량 내림 (반올림하면 잔고보다 커져서 주문이 거절될 수 있음)
# ─────────────────────────────────────────────────────────────
def floor_qty(qty: float, precision: int) -> float:
    if qty <= 0:
        return 0.0
    step = Decimal(1).scaleb(-precision)
    return float(Decimal(str(qty)).quantize(step, rounding=ROUND_DOWN))


# ─────────────────────────────────────────────────────────────
# 포지션 상태 저장 / 복구
# [버그수정] 지갑 잔고 전체를 봇 포지션으로 보던 방식 제거
#   → 봇이 직접 산 수량만 spot_state.json 에 기록하고 그것만 관리
#   → 원래 갖고 있던 코인은 절대 건드리지 않음
# ─────────────────────────────────────────────────────────────
def empty_state() -> dict:
    return {"in_position": False, "entry_price": 0.0,
            "initial_qty": 0.0, "qty": 0.0, "stage": 0, "dust": 0.0}


def reset_position(state: dict, leftover: float = 0.0):
    """포지션 종료. 팔지 못한 자투리(1주문단위 미만)는 dust 로 남겨 다음 거래 때 같이 매도"""
    dust = state.get("dust", 0.0) + max(leftover, 0.0)
    state.update(empty_state())
    state["dust"] = dust


def save_states(states: dict, path: str = None):
    path = path or STATE_FILE
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(states, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        print(f"  [상태 저장 오류] {e}")


def recover_positions(client: Client) -> dict:
    print(f"\n  [포지션 복구 확인 중...] 기록 파일: {os.path.basename(STATE_FILE)}")
    if os.path.exists(LEGACY_STATE_FILE):
        if config.TESTNET and not os.path.exists(STATE_FILE):
            os.replace(LEGACY_STATE_FILE, STATE_FILE)
            print("  ℹ️ 예전 spot_state.json → spot_state_testnet.json 으로 이름 변경")
        elif not config.TESTNET:
            print("  ℹ️ spot_state.json 은 테스트넷 기록이라 실거래에서는 사용하지 않습니다")
    saved = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                saved = json.load(f)
        except Exception as e:
            print(f"  [상태 파일 읽기 오류] {e} → 포지션 없음으로 시작")

    states = {}
    for symbol, coin_cfg in config.COINS.items():
        st = empty_state()
        st.update(saved.get(symbol, {}))

        if st["in_position"]:
            asset   = coin_cfg["asset"]
            balance = get_asset_balance(client, asset)
            if balance < st["qty"]:
                # 사용자가 직접 팔았거나 수수료로 줄어든 경우 → 실제 잔고에 맞춤
                print(f"  ⚠️ [{symbol}] 기록 수량({st['qty']})보다 잔고({balance})가 적음 → 잔고 기준으로 조정")
                st["qty"] = balance
            price = get_price(client, symbol)
            if st["qty"] * price < MIN_ORDER_USDT:
                print(f"  ⬜ [{symbol}] 남은 수량이 최소 주문금액 미만 → 포지션 종료 처리")
                reset_position(st, st["qty"])
            else:
                print(f"  ✅ [{symbol}] 복구 — {st['qty']:.8f} {asset} / 진입가: {st['entry_price']:,.2f} / 단계: {st['stage']}")
        else:
            print(f"  ⬜ [{symbol}] 포지션 없음")
        states[symbol] = st

    print("  ℹ️ 봇이 사지 않은 기존 보유 코인은 건드리지 않습니다.")
    save_states(states)
    return states


# ─────────────────────────────────────────────────────────────
# 시장가 매수 → (평균체결가, 수수료 제외 실수령 수량)
# ─────────────────────────────────────────────────────────────
def buy(client: Client, symbol: str, coin_cfg: dict, usdt_amount: float, price: float,
        label: str = "") -> tuple:
    precision = coin_cfg["qty_precision"]
    asset     = coin_cfg["asset"]
    qty       = floor_qty(usdt_amount / price, precision)
    if qty <= 0:
        return 0.0, 0.0

    print(f"  [매수 주문] {qty} {asset} @ {price:,.2f} USDT")
    try:
        order = client.order_market_buy(symbol=symbol, quantity=qty)
    except BinanceAPIException as e:
        print(f"  [매수 오류] {e}")
        return 0.0, 0.0

    executed  = float(order.get("executedQty") or qty)
    quote     = float(order.get("cummulativeQuoteQty") or 0)
    avg_price = quote / executed if executed > 0 and quote > 0 else price
    # 수수료가 코인으로 차감되면 실제 받은 수량이 줄어듦
    fee = sum(float(f.get("commission", 0)) for f in order.get("fills", [])
              if f.get("commissionAsset") == asset)
    net_qty = max(executed - fee, 0.0)   # 내림하지 않음 (자투리도 추적)

    msg = (f"🟢 매수 체결!{label}\n코인: {symbol}\n"
           f"수량: {net_qty:.8f} {asset}\n체결가: {avg_price:,.2f} USDT")
    print(f"  {msg}")
    send_telegram(msg)
    return avg_price, net_qty


# ─────────────────────────────────────────────────────────────
# 시장가 매도 → 실제 판 수량 반환 (실패 시 0.0)
# ─────────────────────────────────────────────────────────────
def sell(client: Client, symbol: str, coin_cfg: dict, qty: float, reason: str = "") -> float:
    precision = coin_cfg["qty_precision"]
    asset     = coin_cfg["asset"]
    # 잔고를 넘지 않도록 + 내림
    qty = floor_qty(min(qty, get_asset_balance(client, asset)), precision)

    if qty <= 0:
        print(f"  [{symbol}] 매도 스킵 — 수량 없음")
        return 0.0
    print(f"  [매도 주문] {qty} {asset}  사유: {reason}")
    try:
        order = client.order_market_sell(symbol=symbol, quantity=qty)
    except BinanceAPIException as e:
        print(f"  [매도 오류] {e}")
        send_telegram(f"⚠️ [{symbol}] 매도 실패 ({reason}): {e}")
        return 0.0

    executed  = float(order.get("executedQty") or qty)
    quote     = float(order.get("cummulativeQuoteQty") or 0)
    avg_price = quote / executed if executed > 0 and quote > 0 else 0.0
    msg = (f"🔴 매도 체결! ({reason})\n코인: {symbol}\n"
           f"수량: {executed} {asset}\n체결가: {avg_price:,.2f} USDT")
    print(f"  {msg}")
    send_telegram(msg)
    return executed


# ─────────────────────────────────────────────────────────────
# 봇 포지션 전량 매도
# [버그수정] 매도 성공했을 때만 포지션 초기화 (실패 시 유지 → 다음 주기 재시도)
# ─────────────────────────────────────────────────────────────
def close_all(client: Client, symbol: str, coin_cfg: dict, state: dict,
              reason: str, price: float) -> bool:
    sold = sell(client, symbol, coin_cfg, state["qty"], reason)
    if sold <= 0:
        if state["qty"] * price < MIN_ORDER_USDT:
            print(f"    → 남은 수량이 최소 주문금액 미만(먼지) → 포지션 종료, 다음 거래 때 같이 매도")
            reset_position(state, state["qty"])
            return True
        print(f"    → 매도 실패: 포지션 유지, 다음 주기에 재시도")
        return False

    state["qty"] = max(state["qty"] - sold, 0.0)
    if state["qty"] * price < MIN_ORDER_USDT:
        reset_position(state, state["qty"])
    return True


# ─────────────────────────────────────────────────────────────
# 코인 한 개 처리
# ─────────────────────────────────────────────────────────────
def process_coin(client: Client, symbol: str, coin_cfg: dict, state: dict):
    precision = coin_cfg["qty_precision"]

    try:
        df    = get_ohlcv(client, symbol, config.INTERVAL)
        price = get_price(client, symbol)

        # 수익률 표시
        if state["in_position"] and state["entry_price"] > 0:
            pnl = (price - state["entry_price"]) / state["entry_price"] * 100
            stage_labels = ["미달성", "1단계완료(+5%)", "2단계완료(+10%)", "전량매도완료"]
            stage_str    = stage_labels[min(state["stage"], 3)]
            print(f"\n  [{symbol}]  현재가: {price:,.4f}  수익률: {pnl:+.2f}%  "
                  f"익절단계: {stage_str}  보유: {state['qty']:.8f}")
        else:
            print(f"\n  [{symbol}]  현재가: {price:,.4f} USDT")

        # ── 포지션 보유 중: RSI 과매수 / 손절 / 익절 체크
        if state["in_position"]:
            if config.RSI_EXIT_ENABLED and strategy.get_signal(df, price) == "SELL":
                close_all(client, symbol, coin_cfg, state, "RSI 과매수 (보유중 강제 매도)", price)
                return

            action, sell_frac, next_stage = strategy.check_exit(
                state["entry_price"], price, state["stage"]
            )

            if action == "STOP_LOSS":
                close_all(client, symbol, coin_cfg, state,
                          f"손절 -{config.STOP_LOSS_PCT*100:.0f}%", price)
                return

            if action == "TAKE_PROFIT":
                profit_pct = config.TAKE_PROFIT_STAGES[state["stage"]]["profit_pct"] * 100
                if sell_frac >= 1.0:
                    close_all(client, symbol, coin_cfg, state,
                              f"익절 {state['stage']+1}단계 +{profit_pct:.0f}% (전액)", price)
                else:
                    # 부분 매도 (초기 수량의 sell_frac%)
                    qty = min(floor_qty(state["initial_qty"] * sell_frac, precision), state["qty"])
                    if qty * price < MIN_ORDER_USDT:
                        print(f"    [스킵] 분할매도 금액이 최소 주문금액 미만 → {state['stage']+1}단계 건너뜀")
                        state["stage"] = next_stage
                    else:
                        reason = f"익절 {state['stage']+1}단계 +{profit_pct:.0f}% ({sell_frac*100:.0f}%매도)"
                        sold = sell(client, symbol, coin_cfg, qty, reason)
                        if sold > 0:
                            state["qty"]   = max(state["qty"] - sold, 0.0)
                            state["stage"] = next_stage
                return

            print(f"    → HOLD (포지션: 보유중)")
            return

        # ── 포지션 없음: 매수 신호 확인
        signal = strategy.get_signal(df, price)
        print(f"    신호: {signal}")

        if signal == "BUY":
            usdt   = get_usdt_balance(client)
            amount = min(coin_cfg["trade_amount"], usdt)
            if amount >= MIN_ORDER_USDT:
                ep, qty = buy(client, symbol, coin_cfg, amount, price)
                if ep > 0 and qty > 0:
                    total = qty + state.get("dust", 0.0)   # 이전 거래 자투리 합산 → 전량 매도 때 같이 팔림
                    state.update({"in_position": True, "entry_price": ep,
                                  "initial_qty": total, "qty": total, "stage": 0, "dust": 0.0})
            else:
                print(f"    [스킵] USDT 잔고 부족 (보유: {usdt:.2f})")
        else:
            print(f"    → HOLD (포지션: 없음)")

    except BinanceAPIException as e:
        print(f"  [{symbol}] API 오류: {e}")
        send_telegram(f"⚠️ [{symbol}] API 오류: {e}")
    except Exception as e:
        print(f"  [{symbol}] 오류: {e}")


# ─────────────────────────────────────────────────────────────
# 돈치안 추세추종 (일봉, 현물 롱) — RSI 전략과 별도 자금·별도 기록
# ─────────────────────────────────────────────────────────────
_public_client = None

def get_daily_klines(client: Client, symbol: str) -> pd.DataFrame:
    """테스트넷은 일봉이 2주치뿐이라 55일 계산 불가 → 테스트넷일 때만 바이낸스 공개 시세 서버에서 일봉 조회"""
    global _public_client
    limit = config.DONCHIAN_ENTRY_DAYS + 10
    if config.TESTNET:
        if _public_client is None:
            _public_client = Client()
            _public_client.API_URL = "https://data-api.binance.vision/api"
        return get_ohlcv(_public_client, symbol, "1d", limit)
    return get_ohlcv(client, symbol, "1d", limit)


def recover_donchian(client: Client, main_states: dict) -> dict:
    saved = {}
    if os.path.exists(DONCHIAN_STATE_FILE):
        try:
            with open(DONCHIAN_STATE_FILE, encoding="utf-8") as f:
                saved = json.load(f)
        except Exception as e:
            print(f"  [돈치안 기록 읽기 오류] {e} → 포지션 없음으로 시작")
    states = {}
    for symbol, coin_cfg in config.COINS.items():
        st = empty_state()
        st.update(saved.get(symbol, {}))
        if st["in_position"]:
            # 잔고에서 RSI 전략 몫을 뺀 만큼만 돈치안 몫으로 인정
            balance = get_asset_balance(client, coin_cfg["asset"])
            avail   = max(balance - main_states[symbol]["qty"], 0.0)
            if avail < st["qty"]:
                print(f"  ⚠️ [돈치안 {symbol}] 기록 수량({st['qty']:.8f})보다 잔고가 적음 → {avail:.8f} 로 조정")
                st["qty"] = avail
            if st["qty"] * get_price(client, symbol) < MIN_ORDER_USDT:
                reset_position(st, st["qty"])
                print(f"  ⬜ [돈치안 {symbol}] 남은 수량이 최소 주문금액 미만 → 포지션 종료 처리")
            else:
                print(f"  ✅ [돈치안 {symbol}] 복구 — {st['qty']:.8f} / 진입가: {st['entry_price']:,.2f}")
        else:
            print(f"  ⬜ [돈치안 {symbol}] 포지션 없음")
        states[symbol] = st
    save_states(states, DONCHIAN_STATE_FILE)
    return states


def process_donchian(client: Client, symbol: str, coin_cfg: dict, state: dict):
    n, m = config.DONCHIAN_ENTRY_DAYS, config.DONCHIAN_EXIT_DAYS
    try:
        info  = strategy.get_donchian_signal(get_daily_klines(client, symbol), state["in_position"])
        price = get_price(client, symbol)
        if info["close"] <= 0:
            print(f"    [돈치안] 일봉 데이터 부족 → 대기")
            return

        if state["in_position"]:
            pnl = (price - state["entry_price"]) / state["entry_price"] * 100 if state["entry_price"] > 0 else 0.0
            print(f"    [돈치안] 보유중 수익률 {pnl:+.2f}% | 어제종가 {info['close']:,.2f} | "
                  f"청산선({m}일 최저) {info['lo']:,.2f} → {info['signal']}")
            if info["signal"] == "SELL":
                close_all(client, symbol, coin_cfg, state, f"돈치안 {m}일 최저가 이탈", price)
            return

        print(f"    [돈치안] 어제종가 {info['close']:,.2f} | 진입선({n}일 최고) {info['hi']:,.2f} → {info['signal']}")
        if info["signal"] == "BUY":
            usdt   = get_usdt_balance(client)
            amount = min(config.DONCHIAN_AMOUNT_USDT, usdt)
            if amount >= MIN_ORDER_USDT:
                ep, qty = buy(client, symbol, coin_cfg, amount, price, label=f" [돈치안 {n}일 신고가 돌파]")
                if ep > 0 and qty > 0:
                    total = qty + state.get("dust", 0.0)
                    state.update({"in_position": True, "entry_price": ep,
                                  "initial_qty": total, "qty": total, "stage": 0, "dust": 0.0})
            else:
                print(f"    [돈치안 스킵] USDT 잔고 부족 (보유: {usdt:.2f})")

    except BinanceAPIException as e:
        print(f"  [돈치안 {symbol}] API 오류: {e}")
        send_telegram(f"⚠️ [돈치안 {symbol}] API 오류: {e}")
    except Exception as e:
        print(f"  [돈치안 {symbol}] 오류: {e}")


# ─────────────────────────────────────────────────────────────
# 메인 루프
# ─────────────────────────────────────────────────────────────
def run():
    symbols_str = " / ".join(config.COINS.keys())
    print("=" * 55)
    print("  멀티코인 자동 트레이더 시작")
    print(f"  모드   : {'🧪 테스트넷' if config.TESTNET else '💰 실거래'}")
    print(f"  코인   : {symbols_str}")
    print(f"  인터벌 : {config.INTERVAL}")
    print(f"  전략   : RSI({config.RSI_PERIOD}) + 변동성 돌파(K={config.VOLATILITY_K})"
          f"{f' + EMA{config.TREND_EMA} 추세필터' if config.TREND_FILTER else ''}"
          f"{' / RSI>70 매도 사용' if config.RSI_EXIT_ENABLED else ' / RSI 매도 끔'}")
    print(f"  손절   : -{config.STOP_LOSS_PCT*100:.0f}%")
    print(f"  익절   : +5%→30%매도 / +10%→30%매도 / +20%→전액매도")
    print(f"  금액   : " + " / ".join(f"{s} {c['trade_amount']:.0f}" for s, c in config.COINS.items()) + " USDT")
    if config.DONCHIAN_ENABLED:
        print(f"  돈치안 : {config.DONCHIAN_ENTRY_DAYS}일 신고가 매수 / {config.DONCHIAN_EXIT_DAYS}일 신저가 매도 "
              f"(코인당 {config.DONCHIAN_AMOUNT_USDT:.0f} USDT)")
    print("=" * 55)

    client = init_client()
    states  = recover_positions(client)
    dstates = recover_donchian(client, states) if config.DONCHIAN_ENABLED else {}

    send_telegram(
        f"🤖 멀티코인 트레이더 시작\n"
        f"{'테스트넷' if config.TESTNET else '실거래'} | {symbols_str}\n"
        f"손절: -{config.STOP_LOSS_PCT*100:.0f}% / 익절: +5%→30% / +10%→30% / +20%→전액"
    )

    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n{'='*55}\n[{now}]")

        for symbol, coin_cfg in config.COINS.items():
            process_coin(client, symbol, coin_cfg, states[symbol])
            save_states(states)
            if config.DONCHIAN_ENABLED:
                process_donchian(client, symbol, coin_cfg, dstates[symbol])
                save_states(dstates, DONCHIAN_STATE_FILE)

        print(f"\n  다음 체크까지 {config.SLEEP_SECONDS}초 대기...")
        time.sleep(config.SLEEP_SECONDS)


if __name__ == "__main__":
    run()
