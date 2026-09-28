# 비트코인 자동 트레이더

RSI + 래리 윌리엄스 변동성 돌파 전략 기반 바이낸스 자동매매 봇

## 시작하기

### 1. 패키지 설치
```
pip install -r requirements.txt
```

### 2. API 키 설정
```
.env.example 파일을 복사해서 .env 파일 만들기
cp .env.example .env
```
`.env` 파일에 바이낸스 API Key / Secret Key 입력

### 3. 테스트넷으로 먼저 실행 (권장)
`.env`에서 `TESTNET=True` 유지 후:
```
python trader.py
```

### 4. 실거래 전환
`.env`에서 `TESTNET=False` 로 변경

---

## 파일 구조

| 파일 | 역할 |
|------|------|
| `trader.py` | 메인 봇 (실행 진입점) |
| `strategy.py` | RSI + 변동성 돌파 전략 |
| `config.py` | 설정값 (전략 파라미터, 손절/익절 등) |
| `.env` | API 키 (절대 공유 금지!) |

---

## 전략 설명

**래리 윌리엄스 변동성 돌파**
- 목표가 = 당일 시가 + (전일 고가 - 전일 저가) × K
- 현재가가 목표가를 돌파하면 매수 신호

**RSI 필터**
- RSI < 30 (과매도) → 매수 허용
- RSI > 70 (과매수) → 매도

**손절/익절 (config.py에서 변경 가능)**
- 손절: -2%
- 익절: +4%

---

## 주의사항

- 처음엔 반드시 테스트넷(TESTNET=True)으로 테스트
- `.env` 파일을 절대 GitHub 등에 올리지 말 것
- 자동매매는 수익을 보장하지 않음
