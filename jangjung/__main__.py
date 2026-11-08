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
import os
import sys

from .config import load_settings

log = logging.getLogger("jangjung")


def _setup_logging(data_dir):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(data_dir / "jangjung.log", encoding="utf-8"),
                                  logging.StreamHandler(sys.stderr)])
    logging.getLogger("httpx").setLevel(logging.WARNING)


def keep_awake() -> None:
    """Windows: 이 프로그램이 도는 동안 절전(잠자기)으로 들어가지 않게 한다 (화면은 꺼져도 됨).
    프로그램을 끄면 원래 전원 설정으로 돌아간다."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        print("절전 방지: 실행 중에는 PC가 잠자기로 들어가지 않습니다 (끄려면 --allow-sleep)")
    except Exception as e:  # noqa: BLE001
        log.warning("절전 방지 설정 실패: %s", e)


def public_options(host_arg: str | None):
    """--public: (공개 주소, Access 검증기). 공개 주소는 인자나 .env 의 DASHBOARD_PUBLIC_HOST."""
    host = (host_arg or os.getenv("DASHBOARD_PUBLIC_HOST", "")).strip().lower()
    if not host:
        raise SystemExit("--public 에는 공개 주소가 필요합니다: .env 의 DASHBOARD_PUBLIC_HOST=jangjung.example.com "
                         "또는 --public-host jangjung.example.com")
    team, aud = os.getenv("CF_ACCESS_TEAM", "").strip(), os.getenv("CF_ACCESS_AUD", "").strip()
    access = None
    if team and aud:
        from .web import AccessVerifier
        access = AccessVerifier(team, aud, os.getenv("ACCESS_ALLOWED_EMAILS", "").split(","))
    return host, access


async def run_bot(settings, web: bool = True, port: int = 8765, lan: bool = False,
                  public_host: str | None = None, access=None) -> None:
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
    if app.broker.name == "kis-real":
        warn = ("[실전 계좌] 실제 돈으로 주문합니다. 등록된 규칙·자동 투자가 사용자 확인 없이 체결됩니다. "
                "멈추려면 '멈춰'.")
        print("\n  !!! " + warn + "\n")
        await app.notifier.send(warn)
    tasks.append(app.engine.run())
    if web:
        from .web import serve
        pin = None
        if lan or (public_host and not access):
            import secrets
            pin = os.getenv("DASHBOARD_PIN", "").strip()
            if not (pin.isdigit() and 6 <= len(pin) <= 12):
                pin = f"{secrets.randbelow(10**6):06d}"   # 설정이 없으면 실행할 때마다 새 PIN
        tasks.append(serve(app, port, lan=lan, pin=pin, public_host=public_host, access=access))
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
    """토큰·시세·잔고만 조회한다 (주문하지 않음). BROKER=kis_real 이면 실전 서버로 조회."""
    from .broker.kis import KISBroker
    real = settings.broker == "kis_real"
    print(f"{'실전' if real else '모의투자'} 서버로 조회합니다 (주문은 하지 않음)")
    b = KISBroker(settings.kis_app_key, settings.kis_app_secret, settings.kis_account, settings.kis_account_product,
                  settings.data_dir, real=real)
    try:
        q = await b.get_quote("005930")
        print(f"✅ 시세: 삼성전자 {q.price:,.0f}원 ({q.change_pct:+.2f}%) 상태코드 {q.status_code} 정지 {q.halted}")
        a = await b.get_account()
        print(f"✅ 잔고: 현금 {a.cash:,.0f}원 · 평가 {a.total_eval:,.0f}원 · 보유 {len(a.holdings)}종목")
        for h in a.holdings.values():
            print(f"   {h.name}({h.symbol}) {h.qty}주 평단 {h.avg_price:,.0f}")
    finally:
        await b.close()


async def make_report(settings, period: str, date: str | None, email: bool) -> None:
    """PDF 보고서를 data/reports/ 에 저장하고, --email 이면 메일로도 보낸다."""
    from .app import App
    from .pdfreport import PERIODS, build_report, email_reports
    periods = list(PERIODS) if period == "all" else [period]
    app = App(settings, console=False)
    try:
        out = settings.data_dir / "reports"
        out.mkdir(exist_ok=True)
        for p in periods:
            rep = await build_report(app, p, date)
            (out / rep.filename).write_bytes(rep.pdf)
            print(f"[완료] {rep.title} · {rep.pages}장 -> {out / rep.filename}")
        if email:
            print("[메일] " + await email_reports(app, periods, date))
    finally:
        await app.close()


def main() -> None:
    for stream in (sys.stdout, sys.stderr):      # Windows 콘솔(cp949)에서 이모지 때문에 죽지 않도록
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="jangjung")
    p.add_argument("cmd", choices=["run", "web", "demo", "chat", "check-kis", "update-master", "sim-price", "audit",
                                      "report"])
    p.add_argument("args", nargs="*")
    p.add_argument("--no-engine", action="store_true")
    p.add_argument("--clear", action="store_true")
    p.add_argument("--no-web", action="store_true")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--lan", action="store_true", help="같은 Wi-Fi의 휴대폰에서 접속 (PIN 로그인)")
    p.add_argument("--public", action="store_true",
                   help="Cloudflare Tunnel 공개 주소로 밖에서 접속 (Access 이메일 로그인 또는 PIN)")
    p.add_argument("--public-host", help="공개 주소 (기본: .env 의 DASHBOARD_PUBLIC_HOST)")
    p.add_argument("--allow-sleep", action="store_true", help="실행 중에도 PC 절전을 허용")
    p.add_argument("--halt", action="store_true")
    p.add_argument("--email", action="store_true", help="report: 만든 PDF를 메일로 보냄")
    p.add_argument("--demo", action="store_true", help="report: 데모 기록(data/demo)으로 만듦")
    p.add_argument("--overseas", action="store_true", help="update-master: 해외 종목 마스터도 받음")
    a = p.parse_args()
    settings = load_settings()
    if a.cmd == "report" and a.demo:
        settings = load_settings(settings.data_dir / "demo")
        settings.broker = "sim"
    _setup_logging(settings.data_dir)

    if a.cmd in ("run", "web"):
        from .app import RealTradingLocked
        public_host, access = public_options(a.public_host) if (a.public or a.public_host) else (None, None)
        if not a.allow_sleep:
            keep_awake()
        try:
            asyncio.run(run_bot(settings, web=not a.no_web, port=a.port, lan=a.lan, public_host=public_host,
                                access=access))
        except KeyboardInterrupt:
            pass
        except RealTradingLocked as e:
            print(f"\n[실행 중단] {e}\n")
            sys.exit(2)
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
        from .universe import update_master, update_overseas_master
        print(f"국내 종목 {update_master(settings.data_dir):,}개 저장")
        if a.overseas:
            print(f"해외 종목 {update_overseas_master(settings.data_dir):,}개 저장 (미국·일본·중국·홍콩·베트남)")
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
    elif a.cmd == "report":
        period = (a.args[0] if a.args else "daily").lower()
        if period not in ("daily", "weekly", "monthly", "all"):
            p.error("report daily|weekly|monthly|all [YYYY-MM-DD] [--email]")
        asyncio.run(make_report(settings, period, a.args[1] if len(a.args) > 1 else None, a.email))
    elif a.cmd == "audit":
        from .audit import audit
        from .db import DB
        from .universe import Universe
        res = audit(DB(settings.data_dir / "jangjung.db"),
                    Universe(settings.strategies, settings.data_dir, settings.safety["blocked_name_keywords"]))
        print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
