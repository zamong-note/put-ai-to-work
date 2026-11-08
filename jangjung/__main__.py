"""실행 진입점.

  uv run python -m jangjung run            # 장중 엔진 + 웹 대시보드(127.0.0.1:8765) + 텔레그램(토큰 있으면)
  uv run python -m jangjung run --lan      # 같은 Wi-Fi 휴대폰에서도 접속 (PIN 로그인)
  uv run python -m jangjung demo           # 시뮬레이터 일주일치 데모 기록으로 대시보드 보기 (data/demo)
  uv run python -m jangjung chat           # 콘솔에서 대화 + 엔진 (텔레그램 없이 시험)
  uv run python -m jangjung check-kis      # KIS 모의투자 연결 점검 (토큰·시세·잔고)
  uv run python -m jangjung update-master  # KOSPI/KOSDAQ 종목 마스터 내려받기
  uv run python -m jangjung sim-price 000660 230000 | --clear | --halt   # 시뮬레이터 가격 강제
  uv run python -m jangjung audit          # 주문 기록이 규칙·전략과 일치하는지 점검
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from .config import load_settings


def _setup_logging(data_dir):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(data_dir / "jangjung.log", encoding="utf-8"),
                                  logging.StreamHandler(sys.stderr)])
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def run_bot(settings, web: bool = True, port: int = 8765, lan: bool = False) -> None:
    """장중 엔진 + 웹 대시보드 + (토큰이 있으면) 텔레그램 봇을 한 프로세스에서."""
    from .app import App
    tasks = []
    if settings.telegram_token:
        from .telegram import TelegramBot, TelegramNotifier
        tn = TelegramNotifier(settings.telegram_token, settings.telegram_chat_id)
        app = App(settings, notifier=tn)
        await app.notifier.send(f"장중대리 시작 ({app.broker.name}). /help")
        tasks.append(TelegramBot(tn, app).run())
    else:
        print("TELEGRAM_BOT_TOKEN 이 없어 텔레그램 없이 실행합니다 (웹 대시보드에서 대화 가능).")
        app = App(settings, console=False)
    tasks.append(app.engine.run())
    if web:
        from .web import serve
        pin = None
        if lan:
            import os
            import secrets
            pin = os.getenv("DASHBOARD_PIN", "").strip()
            if not (pin.isdigit() and 6 <= len(pin) <= 12):
                pin = f"{secrets.randbelow(10**6):06d}"   # 설정이 없으면 실행할 때마다 새 PIN
        tasks.append(serve(app, port, lan=lan, pin=pin))
    elif not settings.telegram_token:
        print("웹도 텔레그램도 꺼져 있습니다. 'chat' 명령으로 콘솔 모드를 쓰세요.")
        return
    try:
        await asyncio.gather(*tasks)
    finally:
        await app.close()


async def run_console(settings, engine: bool = True) -> None:
    from .app import App
    app = App(settings)
    print(f"장중대리 콘솔 모드 (broker={app.broker.name}). /help, 버튼은 '/btn <data>', 종료는 /quit")
    task = asyncio.create_task(app.engine.run()) if engine else None
    try:
        while True:
            line = (await asyncio.to_thread(input, "\n나> ")).strip()
            if not line:
                continue
            if line in ("/quit", "/exit"):
                break
            reply = await app.handle_text(line)
            if reply:
                print(f"\n장중대리> {reply}")
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        if task:
            task.cancel()
        await app.close()


async def check_kis(settings) -> None:
    from .broker.kis import KISBroker
    b = KISBroker(settings.kis_app_key, settings.kis_app_secret, settings.kis_account, settings.kis_account_product,
                  settings.data_dir)
    try:
        q = await b.get_quote("005930")
        print(f"✅ 시세: 삼성전자 {q.price:,.0f}원 ({q.change_pct:+.2f}%) 상태코드 {q.status_code} 정지 {q.halted}")
        a = await b.get_account()
        print(f"✅ 잔고: 현금 {a.cash:,.0f}원 · 평가 {a.total_eval:,.0f}원 · 보유 {len(a.holdings)}종목")
        for h in a.holdings.values():
            print(f"   {h.name}({h.symbol}) {h.qty}주 평단 {h.avg_price:,.0f}")
    finally:
        await b.close()


def main() -> None:
    p = argparse.ArgumentParser(prog="jangjung")
    p.add_argument("cmd", choices=["run", "web", "demo", "chat", "check-kis", "update-master", "sim-price", "audit"])
    p.add_argument("args", nargs="*")
    p.add_argument("--no-engine", action="store_true")
    p.add_argument("--clear", action="store_true")
    p.add_argument("--no-web", action="store_true")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--lan", action="store_true", help="같은 Wi-Fi의 휴대폰에서 접속 (PIN 로그인)")
    p.add_argument("--halt", action="store_true")
    a = p.parse_args()
    settings = load_settings()
    _setup_logging(settings.data_dir)

    if a.cmd in ("run", "web"):
        try:
            asyncio.run(run_bot(settings, web=not a.no_web, port=a.port, lan=a.lan))
        except KeyboardInterrupt:
            pass
    elif a.cmd == "demo":
        from .demo import seed
        demo_dir = settings.data_dir / "demo"
        master = settings.data_dir / "stock_master.json"
        settings = load_settings(demo_dir)
        if master.exists() and not (demo_dir / "stock_master.json").exists():
            (demo_dir / "stock_master.json").write_bytes(master.read_bytes())
        settings.broker = "sim"
        settings.telegram_token = ""
        settings.extra["demo"] = True
        if not (demo_dir / "jangjung.db").exists():
            print("데모 기록(11/2~11/6)을 만드는 중…")
            asyncio.run(seed(settings))
        try:
            asyncio.run(run_bot(settings, web=True, port=a.port, lan=a.lan))
        except KeyboardInterrupt:
            pass
    elif a.cmd == "chat":
        asyncio.run(run_console(settings, engine=not a.no_engine))
    elif a.cmd == "check-kis":
        asyncio.run(check_kis(settings))
    elif a.cmd == "update-master":
        from .universe import update_master
        print(f"종목 {update_master(settings.data_dir):,}개 저장")
    elif a.cmd == "sim-price":
        path = settings.data_dir / "sim_overrides.json"
        ov = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        sym = a.args[0]
        if a.clear:
            ov.pop(sym, None)
        elif a.halt:
            ov[sym] = {"halted": True}
        else:
            ov[sym] = float(a.args[1])
        path.write_text(json.dumps(ov), encoding="utf-8")
        print(ov)
    elif a.cmd == "audit":
        from .audit import audit
        from .db import DB
        from .universe import Universe
        res = audit(DB(settings.data_dir / "jangjung.db"),
                    Universe(settings.strategies, settings.data_dir, settings.safety["blocked_name_keywords"]))
        print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
