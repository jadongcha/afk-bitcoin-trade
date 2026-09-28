"""
main.py
───────
현물(Spot) + 선물(Futures) 봇을 동시에 실행하는 진입점.

  python main.py            → 두 봇 모두 실행
  python main.py --spot     → 현물만 실행
  python main.py --futures  → 선물만 실행
"""
import argparse
import threading
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("main")


def start_spot():
    from trader import run as spot_run
    log.info("▶ 현물(Spot) 봇 스레드 시작")
    spot_run()


def start_futures():
    from futures_trader import run as futures_run
    log.info("▶ 선물(Futures) 봇 스레드 시작")
    futures_run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bitcoin Trader")
    parser.add_argument("--spot",    action="store_true", help="현물 봇만 실행")
    parser.add_argument("--futures", action="store_true", help="선물 봇만 실행")
    args = parser.parse_args()

    run_spot    = args.spot    or (not args.spot and not args.futures)
    run_futures = args.futures or (not args.spot and not args.futures)

    threads = []

    if run_spot:
        t_spot = threading.Thread(target=start_spot, name="SpotBot", daemon=True)
        threads.append(t_spot)

    if run_futures:
        t_futures = threading.Thread(target=start_futures, name="FuturesBot", daemon=True)
        threads.append(t_futures)

    log.info(
        f"실행 모드: "
        f"{'현물 ' if run_spot else ''}"
        f"{'선물' if run_futures else ''}"
    )

    for t in threads:
        t.start()

    # 메인 스레드를 살려두기 (Ctrl+C 로 종료)
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        log.info("=== 봇 종료 (Ctrl+C) ===")
