"""장중 엔진 — LLM 없이 돌아가는 결정론적 루프.

매 poll_seconds(기본 20초)마다: 중지 확인 → 손실 한도 → 규칙 조건 평가/주문 → 미체결 재확인.
정해진 시각에: 분할 매수 회차, 월초 리밸런싱 점검, 장 마감 요약.
"""
from __future__ import annotations

import asyncio
import logging

from . import timeutil
from .broker.base import BrokerError

log = logging.getLogger("jangjung")


class Engine:
    def __init__(self, app):
        self.app = app
        self.s = app.settings
        self._alerted: dict[str, str] = {}   # 같은 경고 반복 방지 (key → 날짜)
        self.running = True
        self.last_step_at: str | None = None
        self.started = False

    async def alert_once(self, key: str, text: str) -> None:
        today = timeutil.today_str()
        if self._alerted.get(key) == today:
            return
        self._alerted[key] = today
        await self.app.notifier.send(text)

    def _due(self, key: str, hm: str) -> bool:
        """오늘 hm 이후이고 아직 실행 안 했으면 True (실행 기록은 kv)."""
        now = timeutil.now()
        if not timeutil.is_trading_day(now.date(), self.s.holidays):
            return False
        if now.time() < timeutil.parse_hm(hm):
            return False
        return self.app.db.kv_get(f"job:{key}") != timeutil.today_str()

    def _mark(self, key: str) -> None:
        self.app.db.kv_set(f"job:{key}", timeutil.today_str())

    async def run(self) -> None:
        log.info("엔진 시작 (poll %ss, broker=%s)", self.s.engine["poll_seconds"], self.app.broker.name)
        self.started = True
        while self.running:
            try:
                await self.step()
            except Exception:  # noqa: BLE001 — 루프는 죽지 않는다
                log.exception("엔진 step 오류")
            await asyncio.sleep(self.s.engine["poll_seconds"])

    async def step(self) -> None:
        app, eng = self.app, self.s.engine
        self.last_step_at = timeutil.now().isoformat()
        await app.pending.expire_old()
        now = timeutil.now()
        in_session = timeutil.in_session(now, self.s.holidays, eng["session_start"], eng["session_end"])
        if in_session:
            await self.tick()
            if not app.control.halted():
                if self._due("build", eng["build_time"]):
                    self._mark("build")
                    st = app.autoinvest.state()
                    if st and st["status"] == "building":
                        await self._guard(app.autoinvest.build_step(), "자동투자 분할매수")
                first = timeutil.first_trading_day_of_month(now.date(), self.s.holidays)
                if now.date() == first and self._due("rebalance", eng["rebalance_time"]):
                    self._mark("rebalance")
                    await self._guard(app.autoinvest.monthly_check(), "월간 리밸런싱")
        if self._due("summary", eng["summary_time"]):
            self._mark("summary")
            await self._guard(self.daily_summary(), "장 마감 요약")

    async def _guard(self, coro, label: str):
        try:
            res = await coro
            self.app.control.api_errors = 0
            return res
        except BrokerError as e:
            await self._api_error(label, e)

    async def _api_error(self, label: str, e: Exception) -> None:
        c = self.app.control
        c.api_errors += 1
        c.last_api_error = (timeutil.now().isoformat(), f"{label}: {e}")
        lim = self.s.safety["max_consecutive_api_errors"]
        log.warning("%s API 오류 (%d/%d): %s", label, c.api_errors, lim, e)
        if c.api_errors >= lim and not c.halted():
            c.halt(f"API 오류 연속 {c.api_errors}회 ({e})", by="system")
            await self.app.pending.create("resume_all", {}, f"[API 오류로 전체 중지]\n{label}: {e}\n"
                                          "모든 주문을 멈췄습니다. 증권사 상태를 확인한 뒤 재개해 주세요.",
                                          [("재개", "approve"), ("중지 유지", "cancel")], ttl_min=7 * 24 * 60)
        else:
            await self.alert_once(f"api:{label}", f"[API 오류] {label}: {e}\n이번 회차 주문은 건너뜁니다.")

    async def tick(self) -> None:
        app = self.app
        if app.control.halted():
            return
        try:
            await app.gateway.reconcile_open()
            msg = await app.autoinvest.check_loss()
            if msg:
                log.warning(msg)
            await self.evaluate_rules()
            app.control.api_errors = 0
            app.control.last_api_ok = timeutil.now().isoformat()
        except BrokerError as e:
            await self._api_error("장중 감시", e)

    async def evaluate_rules(self) -> None:
        app = self.app
        rules = app.db.q("SELECT * FROM rules WHERE status='active' ORDER BY id")
        if not rules:
            return
        acct = await app.broker.get_account()
        quotes = {}
        for r in rules:
            if app.control.halted():
                return
            sym = r["symbol"]
            if sym in app.control.suspended():
                continue
            if sym not in quotes:
                q = await app.broker.get_quote(sym)
                quotes[sym] = q
                if why := app.control.quote_anomaly(q):
                    if app.control.suspend(sym, why):
                        await app.pending.create(
                            "symbol_resume", {"symbol": sym},
                            f"[시세 이상 — {r['name']} 매매 일시중단]\n{why}\n현재가 {q.price:,.0f}원 "
                            f"(전일 {q.prev_close:,.0f}원)\n이 종목 규칙은 주문하지 않고 기다립니다.",
                            [("이 종목 매매 재개", "approve"), ("계속 중단", "cancel")], ttl_min=7 * 24 * 60)
                    continue
            q = quotes[sym]
            if sym in app.control.suspended():
                continue
            ev = app.rules.evaluate(r, q, acct, app.gateway.ledger.qty(sym))
            if ev.met:
                detected = timeutil.now().isoformat()
                log.info("규칙 #%s 충족 → 주문", r["id"])
                await app.rules.fire(r, ev, q, detected)
                acct = await app.broker.get_account()   # 보유 수량 갱신
            elif ev.note == "주문 가능 수량 0주":
                await self.alert_once(f"zero:{r['id']}", f"규칙 #{r['id']} 조건은 충족했지만 주문 가능 수량이 0주라 실행하지 않았습니다.")

    async def daily_summary(self) -> str:
        app = self.app
        today = timeutil.today_str()
        fills = app.db.one("SELECT COUNT(*) n FROM orders WHERE source='rule' AND status IN ('filled','partial') "
                           "AND substr(created_at,1,10)=?", (today,))["n"]
        auto_cnt = app.db.one("SELECT COUNT(*) n FROM orders WHERE source<>'rule' AND status IN ('filled','partial') "
                              "AND substr(created_at,1,10)=?", (today,))["n"]
        remaining = app.rules.active_count()
        val = None
        try:
            val = await app.autoinvest.valuation()
        except BrokerError as e:
            log.warning("요약용 평가 실패: %s", e)
        prev = app.db.one("SELECT auto_value FROM daily_snapshots WHERE date<? AND auto_value IS NOT NULL "
                          "ORDER BY date DESC LIMIT 1", (today,))
        lines = [f"[장 마감 요약 {timeutil.now().strftime('%m/%d')}]"]
        day_pct = None
        if val:
            base = prev["auto_value"] if prev else val["state"]["principal"]
            day_pct = (val["total"] / base - 1) * 100 if base else 0
            lines.append(f"자동 투자 {day_pct:+.1f}% (누적 {val['pnl_pct']:+.1f}%, 평가 {val['total']:,.0f}원) · "
                         f"규칙 매매 체결 {fills}건 · 남은 규칙 {remaining}개")
            off = [f"{r['name']} {r['weight']:.0f}%/{r['target']:g}%" for r in val["rows"]
                   if abs(r["dev"]) > app.autoinvest.cfg["rebalance_band_pp"]]
            if off:
                lines.append("목표 비중 이탈: " + ", ".join(off))
        else:
            lines.append(f"자동 투자 없음 · 규칙 매매 체결 {fills}건 · 남은 규칙 {remaining}개")
        if auto_cnt:
            lines.append(f"자동 투자 체결 {auto_cnt}건")
        unfilled = await app.gateway.end_of_day_report_unfilled()
        if unfilled:
            lines.append(f"⚠ 미체결 주문 {len(unfilled)}건 — MTS에서 확인 필요")
        if h := app.control.halted():
            lines.append(f"⛔ 전체 중지 상태: {h['reason']}")
        if app.journal.sheets_error:
            lines.append(f"⚠ Google Sheets 기록 실패(로컬 CSV에는 기록): {app.journal.sheets_error[:80]}")
        app.db.x("INSERT OR REPLACE INTO daily_snapshots(date,auto_value,auto_principal,rule_fills,active_rules) "
                 "VALUES(?,?,?,?,?)", (today, val["total"] if val else None,
                                       val["state"]["principal"] if val else None, fills, remaining))
        await app.journal.log_summary([today, round(val["total"]) if val else "", val["state"]["principal"] if val else "",
                                       f"{val['pnl_pct']:.2f}" if val else "", f"{day_pct:.2f}" if day_pct is not None else "",
                                       fills, remaining, (h or {}).get("reason", "") if (h := app.control.halted()) else ""])
        text = "\n".join(lines)
        await app.notifier.send(text)
        return text
