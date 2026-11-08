"""구성요소 조립 + 메시지 라우팅.

'멈춰'와 슬래시 명령은 LLM을 거치지 않고 코드가 바로 처리한다 (빠르고 확실하게).
"""
from __future__ import annotations

import logging
import os
import re

from . import timeutil
from .autoinvest import AutoInvest
from .broker.sim import SimBroker
from .config import Settings
from .control import Control
from .db import DB
from .engine import Engine
from .journal import Journal
from .notify import ConsoleNotifier, FeedNotifier
from .orders import OrderGateway
from .pending import PendingStore
from .rules import RuleService, rule_line
from .universe import Universe

log = logging.getLogger("jangjung")
STOP_WORDS = {"멈춰", "멈춰줘", "멈춰요", "다멈춰", "전부멈춰", "모두멈춰", "다멈춰줘", "정지", "전체정지", "중지",
              "전체중지", "올스톱", "stop", "/stop", "스톱"}

HELP = """장중대리 사용법
· 규칙 등록: "SK하이닉스 수익률 15% 넘으면 절반 팔고, 산 가격보다 7% 떨어지면 전부 팔아 줘"
· 자동 투자: "자동 투자 1,000만 원, 3단계로 시작해 줘" (처음엔 성향 질문 6개)
· 멈춰 — 모든 규칙과 자동 투자 즉시 중지
/status 상태 · /rules 규칙 · /portfolio 자동투자 · /journal 최근 주문
/report daily|weekly|monthly|all [날짜] — PDF 보고서를 텔레그램으로
/alerts — 알림 설정 보기 · /alerts 체결 off — 끄기 (체결·자동투자·요약·설정·보고서·전략)
/strategy — 최근 AI 투자 점검 · /strategy run 지금 점검 · /strategy reset 기본 비중으로
/fx — 현재 환율 · /fx update 지금 갱신
/mode auto|confirm 리밸런싱 방식 · /resume 중지 해제 · /summary 지금 요약 · /new 대화 초기화"""


class RealTradingLocked(RuntimeError):
    """실전 계좌 잠금 — 두 가지 설정이 모두 있어야 실전 주문 브로커를 만든다."""


BROKER_LABEL = {"sim": "시뮬레이터", "kis-mock": "KIS 모의투자", "kis-real": "KIS 실전"}


class App:
    def __init__(self, settings: Settings, notifier=None, console: bool = True):
        self.settings = settings
        self.db = DB(settings.data_dir / "jangjung.db")
        self.universe = Universe(settings.strategies, settings.data_dir, settings.safety["blocked_name_keywords"],
                                 settings.overseas, settings.overseas_symbols)
        self.broker = self._make_broker()
        self.control = Control(self.db, settings.safety)
        self.journal = Journal(settings.data_dir, settings.google_sa_file, settings.google_sheet_id)
        inner = notifier if notifier is not None else (ConsoleNotifier() if console else None)
        self.notifier = FeedNotifier(self.db, inner, settings.notify)
        from .settings_store import SettingsStore
        self.store = SettingsStore(settings, settings.extra.get("defaults") or {})
        self.pending = PendingStore(self.db, self.notifier)
        self.gateway = OrderGateway(self.db, self.broker, self.universe, self.control, self.journal, self.notifier,
                                    settings)
        self.rules = RuleService(self.db, self.universe, self.broker, self.gateway, self.pending, self.notifier, settings)
        self.autoinvest = AutoInvest(self.db, self.universe, self.broker, self.gateway, self.pending, self.notifier,
                                     settings)
        self.pending.register("resume_all", self._on_resume_all)
        self.pending.register("symbol_resume", self._on_symbol_resume)
        self.engine = Engine(self)
        from .strategist import Strategist
        self.strategist = Strategist(self)
        self._agent = None
        self.telegram_running = False

    def _make_broker(self):
        s = self.settings
        if s.broker in ("kis", "kis_real"):
            from .broker.kis import KISBroker
            real = s.broker == "kis_real"
            if real and os.getenv("KIS_REAL_TRADING", "").strip().lower() != "yes":
                raise RealTradingLocked("BROKER=kis_real 은 실제 돈으로 주문합니다. 정말 실전으로 운영하려면 "
                                        ".env 에 KIS_REAL_TRADING=yes 를 함께 넣으세요.")
            b = KISBroker(s.kis_app_key, s.kis_app_secret, s.kis_account, s.kis_account_product, s.data_dir,
                          real=real)
            from . import markets
            b.overseas_markets = lambda: [m for m, i in markets.MARKETS.items()
                                          if i["country"] in markets.allowed_countries(s.overseas)]
            b.fx = lambda ccy: markets.fx_rate(ccy, s.overseas)
            return b
        if s.broker not in ("sim", ""):
            raise RealTradingLocked(f"알 수 없는 BROKER 값: {s.broker} (sim | kis | kis_real)")
        return SimBroker(self.db, self.universe.name, s.sim, s.data_dir, s.overseas)

    @property
    def agent(self):
        if self._agent is None:
            from .agent import Agent
            self._agent = Agent(self)
        return self._agent

    # ── 중지/재개 ──
    async def emergency_stop(self, reason: str = "사용자 '멈춰'") -> str:
        already = self.control.halted()
        self.control.halt(reason)
        n_rules = self.rules.active_count()
        st = self.autoinvest.state()
        msg = ("이미 전체 중지 상태입니다." if already else
               f"⛔ 전체 중지했습니다. 감시 중이던 규칙 {n_rules}개와 자동 투자"
               f"({'없음' if not st else str(st['tier']) + '단계'})의 신규 주문을 모두 멈췄습니다.")
        return msg + "\n이미 접수된 미체결 주문이 있다면 MTS에서 확인해 주세요. 재개: /resume"

    async def request_resume(self) -> str:
        h = self.control.halted()
        if not h:
            return "중지 상태가 아닙니다."
        await self.pending.create("resume_all", {}, f"[전체 중지 해제 확인]\n중지 사유: {h['reason']}\n"
                                  f"해제하면 규칙 {self.rules.active_count()}개 감시와 자동 투자가 다시 시작됩니다.",
                                  [("재개", "approve"), ("중지 유지", "cancel")])
        return "재개 확인 카드를 보냈습니다."

    async def _on_resume_all(self, action, payload, pid) -> str:
        if action != "approve":
            return "중지 상태를 유지합니다."
        self.control.resume()
        return "✅ 전체 중지를 해제했습니다. 다음 감시 주기부터 다시 동작합니다."

    async def _on_symbol_resume(self, action, payload, pid) -> str:
        if action != "approve":
            return f"{payload['symbol']} 매매 중단을 유지합니다."
        self.control.unsuspend(payload["symbol"])
        return f"{self.universe.name(payload['symbol'])} 매매를 재개했습니다."

    # ── 라우팅 ──
    @staticmethod
    def _norm(text: str) -> str:
        return re.sub(r"[\s!.~?ㅠㅜ]", "", text).lower()

    def is_command_or_stop(self, text: str) -> bool:
        return text.strip().startswith("/") or self._norm(text) in STOP_WORDS

    async def handle_button(self, data: str) -> str:
        parts = data.split("|")
        if len(parts) != 3 or parts[0] != "pd":
            return "알 수 없는 버튼입니다."
        return await self.pending.resolve(parts[1], parts[2])

    async def handle_text(self, text: str, via: str = "console") -> str | None:
        t = text.strip()
        self.notifier.record("user", t, via=via)
        if self._norm(t) in STOP_WORDS:
            return await self.emergency_stop()
        if t.startswith("/"):
            return await self.command(t, via)
        return await self.agent.ask(t)

    def alerts_text(self) -> str:
        from .settings_store import SCHEMA
        lines = ["[알림 설정] 꺼 둔 알림은 텔레그램으로 보내지 않고 대시보드에만 남깁니다."]
        for f in SCHEMA["notify"]:
            lines.append(f"· {f['label']}: {'켜짐' if self.settings.notify.get(f['key'], 'on') == 'on' else '꺼짐'}")
        lines.append("확인 카드·안전 경고(전체 중지·시세 이상·손실 한도·API 오류)·주문 실패는 항상 보냅니다.")
        lines.append("바꾸기: /alerts 체결 off · /alerts 요약 on (체결·자동투자·요약·설정·보고서)")
        return "\n".join(lines)

    async def command(self, t: str, via: str = "console") -> str | None:
        cmd, _, arg = t.partition(" ")
        cmd = cmd.lower().split("@")[0]
        if cmd in ("/start", "/help"):
            return HELP
        if cmd == "/status":
            return await self.status_text()
        if cmd == "/rules":
            rows = self.rules.list()
            return "\n".join(rule_line(r) for r in rows) if rows else "감시 중인 규칙이 없습니다."
        if cmd == "/portfolio":
            v = await self.autoinvest.valuation()
            if not v:
                return "자동 투자가 없습니다."
            st = v["state"]
            lines = [f"자동 투자 {st['tier']}단계 · {st['status']} · 구성 {st['tranches_done']}/{st['tranches_total']}",
                     f"평가 {v['total']:,.0f}원 / 원금 {st['principal']:,.0f}원 ({v['pnl_pct']:+.2f}%) · 현금 {v['cash']:,.0f}원"]
            lines += [f" - {r['name']} {r['qty']}주 · {r['weight']:.1f}% (목표 {r['target']:g}%, {r['dev']:+.1f}%p)"
                      for r in v["rows"]]
            return "\n".join(lines)
        if cmd == "/journal":
            rows = self.db.q("SELECT * FROM orders ORDER BY id DESC LIMIT 5")
            if not rows:
                return "주문 기록이 없습니다."
            return "\n".join(f"{o['created_at'][5:16]} {o['name']} {'매수' if o['side'] == 'buy' else '매도'} {o['qty']}주 "
                             f"· 판단 {o['decision_price']:,.0f} · 체결 {o['avg_fill_price'] or 0:,.0f} · {o['status']}\n  {o['reason']}"
                             for o in rows)
        if cmd == "/alerts":
            from .settings_store import NOTIFY_ALIASES, SCHEMA
            parts = arg.split()
            if not parts:
                return self.alerts_text()
            keys = {f["key"] for f in SCHEMA["notify"]}
            key = NOTIFY_ALIASES.get(parts[0], parts[0].lower())
            val = {"on": "on", "켜": "on", "켜기": "on", "off": "off", "꺼": "off", "끄기": "off"}.get(
                parts[1].lower() if len(parts) > 1 else "")
            if key not in keys or not val:
                return "사용법: /alerts 체결 off · /alerts 요약 on (체결·자동투자·요약·설정·보고서)"
            self.store.update("notify", {key: val})
            await self.notifier.send(f"[설정 변경 · 알림] {via}에서 변경: {parts[0]} → {'켜짐' if val == 'on' else '꺼짐'}",
                                     category="settings")
            return self.alerts_text()
        if cmd == "/report":
            from .pdfreport import PERIODS, deliver_reports, telegram_target
            parts = arg.split()
            period = (parts[0] if parts else "daily").lower()
            periods = list(PERIODS) if period == "all" else [period]
            if any(p not in PERIODS for p in periods):
                return "사용법: /report daily|weekly|monthly|all [YYYY-MM-DD]"
            if telegram_target(self) is None:
                return "텔레그램 봇이 연결되어 있지 않습니다. 대시보드 보고서 탭에서 PDF를 받거나 메일로 보내세요."
            try:
                res = await deliver_reports(self, periods, parts[1] if len(parts) > 1 else None, ["telegram"])
            except ValueError:
                return "날짜는 YYYY-MM-DD 형식으로 적어 주세요."
            except Exception as e:  # noqa: BLE001
                return f"보고서를 보내지 못했습니다: {e}"
            return res["message"]
        if cmd == "/strategy":
            sub = arg.strip().lower()
            if sub == "run":
                return await self.engine.ai_review(f"{via} 요청")
            if sub == "reset":
                return await self.strategist.reset_targets()
            s = self.strategist.latest()
            if not s:
                return "아직 AI 투자 점검 기록이 없습니다. /strategy run 으로 지금 점검할 수 있습니다."
            res = s["result"] or {}
            st = {"proposed": "확인 대기", "executed": "실행함", "approved": "장 시작 후 실행", "declined": "건너뜀",
                  "no_change": "변경 없음", "analysis": "점검만", "rejected": "제안 거부(범위 밖)", "skipped": "실행 안 함"}
            return (f"[AI 투자 점검 #{s['id']} · {s['created_at'][5:16].replace('T', ' ')}] {st.get(s['status'], s['status'])}\n"
                    f"{res.get('summary', '')}" + (f"\n※ {s['note']}" if s.get("note") else "")
                    + f"\n설정: {self.settings.ai_strategy.get('mode')} · {self.settings.ai_strategy.get('cadence')}")
        if cmd == "/fx":
            from . import macro
            if arg.strip().lower() == "update":
                return await self.engine.update_fx()
            fx = macro.fx_summary(self.db, self.settings.overseas)
            last = self.db.kv_get("fx_last") or {}
            lines = [f"{c} {v['krw_per_unit']:,.4g}원" + (f" (1일 {v['chg_1d_pct']:+.2f}%)" if v["chg_1d_pct"] is not None else "")
                     for c, v in fx.items()]
            return "[환율] 1단위 = 원\n" + "\n".join(lines) + (f"\n갱신 {last.get('at', '')[5:16].replace('T', ' ')} · {last.get('source')}"
                                                              if last else "\n아직 자동 갱신 기록 없음 (/fx update)")
        if cmd == "/stop":
            return await self.emergency_stop()
        if cmd == "/resume":
            return await self.request_resume()
        if cmd == "/mode":
            try:
                self.autoinvest.set_rebalance_mode(arg.strip())
                return f"리밸런싱 방식: {arg.strip()}"
            except ValueError:
                return "사용법: /mode auto  또는  /mode confirm"
        if cmd == "/summary":
            return await self.engine.daily_summary() and None
        if cmd == "/new":
            if self._agent:
                await self._agent.reset()
            return "대화를 초기화했습니다."
        if cmd == "/btn":
            return await self.handle_button(arg.strip())
        if cmd == "/tick":
            await self.engine.tick()
            return "감시 1회 실행"
        if cmd == "/build":
            return await self.autoinvest.build_step()
        if cmd == "/rebalance":
            return await self.autoinvest.monthly_check(force=True)
        return "모르는 명령입니다. /help"

    async def status_text(self) -> str:
        h = self.control.halted()
        st = self.autoinvest.state()
        eng = self.settings.engine
        sess = timeutil.in_session(timeutil.now(), self.settings.holidays, eng["session_start"], eng["session_end"])
        lines = [f"브로커: {self.broker.name} · {'장중' if sess else '장외'} · 감시 주기 {eng['poll_seconds']}초",
                 f"전체 중지: {'⛔ ' + h['reason'] if h else '아니오'}",
                 f"감시 중 규칙: {self.rules.active_count()}개",
                 f"자동 투자: {'없음' if not st else str(st['tier']) + '단계 · ' + st['status']} · 리밸런싱 {self.autoinvest.rebalance_mode()}",
                 f"확인 대기 카드: {len(self.pending.open_items())}개",
                 f"매매일지: CSV{' + Google Sheets' if self.journal.sheets_enabled else ''}"]
        if sus := self.control.suspended():
            lines.append("일시중단 종목: " + ", ".join(f"{self.universe.name(s)}({v['reason']})" for s, v in sus.items()))
        return "\n".join(lines)

    async def close(self) -> None:
        if self._agent:
            await self._agent.reset()
        await self.broker.close()
