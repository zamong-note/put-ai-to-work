"""장중 엔진 — LLM 없이 돌아가는 결정론적 루프.

매 poll_seconds(기본 20초)마다: 중지 확인 → 손실 한도 → 규칙 조건 평가/주문 → 미체결 재확인.
정해진 시각에: 분할 매수 회차, 월초 리밸런싱 점검, 장 마감 요약.
"""
from __future__ import annotations

import asyncio
import logging

from . import markets, timeutil
from .broker.base import BrokerError
from .rules import is_stop_loss, untradable

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
        overseas = self.open_overseas(now)
        if in_session or overseas:
            await self.tick(domestic=in_session, overseas=overseas)
        # 환율 자동 갱신 (매 거래일 08:50)
        if self.s.overseas.get("fx_auto", "on") == "on" and self._due("fx", "08:50"):
            self._mark("fx")
            await self.update_fx()
        if in_session:
            if not app.control.halted():
                await app.strategist.run_approved()           # 장외에 승인한 AI 전략
                ai = self.s.ai_strategy
                if (ai.get("mode", "off") != "off" and app.strategist.due(now)
                        and self._due("ai_strategy", ai.get("run_time", "10:30"))):
                    self._mark("ai_strategy")
                    await self.ai_review("schedule")
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
        # 보고서 메일: 그날 장 마감 요약(평가액 기록)이 끝난 뒤에만
        if (app.db.kv_get("job:summary") == timeutil.today_str()
                and self._due("report_mail", self.s.report.get("send_time", "16:00"))):
            self._mark("report_mail")
            await self.report_mail()

    async def update_fx(self) -> str:
        from .macro import update_fx
        try:
            res = await update_fx(self.app)
        except Exception as e:  # noqa: BLE001 — 환율 실패가 매매를 막지 않음 (이전 값 유지)
            await self.alert_once("fx", f"[환율 갱신 실패] {e}\n이전 환율을 그대로 씁니다.")
            return f"환율 갱신 실패: {e}"
        if res["skipped"]:
            await self.alert_once("fx_skip", "[환율 확인 필요] " + "; ".join(res["skipped"]))
        return "환율 갱신: " + (", ".join(f"{l} {o}→{n}" for l, o, n in res["changes"]) or "변동 없음")

    async def ai_review(self, trigger: str) -> str:
        try:
            res = await self.app.strategist.review(trigger)
            return res["message"]
        except Exception as e:  # noqa: BLE001 — AI 점검 실패가 매매를 막지 않음
            log.exception("AI 점검 실패")
            await self.app.notifier.send(f"[AI 투자 점검 실패] {type(e).__name__}: {e}\n목표 비중과 매매는 그대로입니다.")
            return f"AI 점검 실패: {e}"

    async def report_mail(self, force_periods: list[str] | None = None) -> str | None:
        """그날 받을 보고서(일간·주간·월간 중 켠 것)를 설정한 곳(메일·텔레그램)으로 보낸다."""
        from .pdfreport import channels_for, deliver_reports, due_periods
        periods = force_periods or due_periods(timeutil.now().date(), self.s.holidays, self.s.report)
        if not periods:
            return None
        channels = channels_for(self.app, self.s.report.get("channel", "both"))
        if not channels:
            log.info("보고서를 보낼 곳(메일·텔레그램)이 연결되지 않아 보내지 않음 (%s)", periods)
            return None
        try:
            res = await deliver_reports(self.app, periods, None, channels)
        except Exception as e:  # noqa: BLE001 — 발송 실패가 매매를 막지 않는다
            log.warning("보고서 발송 실패: %s", e)
            self.app.db.kv_set("report_mail_last", {"at": timeutil.now().isoformat(), "periods": periods,
                                                    "ok": False, "error": str(e)[:300]})
            await self.app.notifier.send(f"[보고서 발송 실패] {e}\n대시보드 보고서 탭에서 다시 보낼 수 있습니다.")
            return None
        await self.app.notifier.send(f"[보고서 발송] {res['message']}",
                                     category=None if res["errors"] else "report")
        return res["message"]

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

    def open_overseas(self, now=None) -> set[str]:
        """허용한 국가 중 지금(현지 시각) 거래 중인 해외 거래소."""
        allowed = markets.allowed_countries(self.s.overseas)
        if not allowed:
            return set()
        now = now or timeutil.now()
        return {m for m, i in markets.MARKETS.items()
                if i["country"] in allowed and markets.market_open(m, now, self.s.overseas_holidays)}

    async def tick(self, domestic: bool = True, overseas: set[str] | None = None) -> None:
        app = self.app
        if app.control.halted():
            return
        if overseas is None:
            overseas = self.open_overseas()
        try:
            await app.gateway.reconcile_open()
            if domestic:
                msg = await app.autoinvest.check_loss()
                if msg:
                    log.warning(msg)
            await self.evaluate_rules(domestic, overseas)
            app.control.api_errors = 0
            app.control.last_api_ok = timeutil.now().isoformat()
        except BrokerError as e:
            await self._api_error("장중 감시", e)

    async def evaluate_rules(self, domestic: bool = True, overseas: set[str] | None = None) -> None:
        app = self.app
        rules = app.db.q("SELECT * FROM rules WHERE status='active' ORDER BY id")
        # 각 시장이 열려 있을 때만 그 시장 규칙을 본다 (국내 장중 / 해외 현지 장중)
        overseas = overseas or set()
        rules = [r for r in rules if (domestic if not markets.is_overseas(r["symbol"])
                                      else markets.market_of(r["symbol"]) in overseas)]
        if not rules:
            return
        acct = await app.broker.get_account()
        quotes = {}
        for r in rules:
            if app.control.halted():
                return
            sym = r["symbol"]
            stop_loss = is_stop_loss(r)
            # 멈춘 종목은 손절 규칙만 계속 본다 (자동으로 팔지는 않고 확인 카드만)
            if sym in app.control.suspended() and not stop_loss:
                continue
            if sym not in quotes:
                q = await app.broker.get_quote(sym)
                quotes[sym] = q
                if sym not in app.control.suspended() and (why := app.control.quote_anomaly(q)):
                    if app.control.suspend(sym, why):
                        await app.pending.create(
                            "symbol_resume", {"symbol": sym},
                            f"[시세 이상 — {r['name']} 매매 일시중단]\n{why}\n현재가 {markets.fmt_price(sym, q.price)} "
                            f"(전일 {markets.fmt_price(sym, q.prev_close)})\n이 종목 규칙은 자동으로 주문하지 않습니다. "
                            "손절 조건이 충족되면 '그래도 팔까요?'를 따로 묻습니다.",
                            [("이 종목 매매 재개", "approve"), ("계속 중단", "cancel")], ttl_min=7 * 24 * 60)
            q = quotes[sym]
            if sus := app.control.suspended().get(sym):
                if stop_loss:
                    if untradable(q):
                        if app.rules.evaluate(r, q, acct, app.gateway.ledger.qty(sym)).met:
                            await self.alert_once(f"sl_halt:{r['id']}",
                                                  f"규칙 #{r['id']} {r['name']} 손절 조건이 충족됐지만 거래정지 상태라 "
                                                  "매도할 수 없습니다. 거래가 재개되면 다시 확인합니다.")
                    else:
                        await app.rules.stop_loss_during_anomaly(r, q, acct, app.gateway.ledger.qty(sym),
                                                                 sus["reason"])
                continue
            ev = app.rules.evaluate(r, q, acct, app.gateway.ledger.qty(sym))
            if ev.met:
                detected = timeutil.now().isoformat()
                log.info("규칙 #%s 충족 → 주문", r["id"])
                await app.rules.fire(r, ev, q, detected)
                acct = await app.broker.get_account()   # 보유 수량 갱신
            elif ev.note == "주문 가능 수량 0주":
                await self.alert_once(f"zero:{r['id']}", f"규칙 #{r['id']} 조건은 충족했지만 주문 가능 수량이 0주라 실행하지 않았습니다.")

    async def _snapshot_detail(self, val: dict | None) -> str | None:
        """보고서에서 과거 날짜의 비중·보유 종목을 보여 주기 위한 장 마감 스냅샷."""
        import json
        app = self.app
        out = {}
        if val:
            out["auto_rows"] = [{k: r[k] for k in ("symbol", "name", "qty", "price", "value", "weight", "target", "dev")}
                                for r in val["rows"]]
            out["cash"] = val["cash"]
        try:
            acct = await app.broker.get_account()
        except BrokerError:
            acct = None
        if acct:
            auto_pos = app.gateway.ledger.positions()
            out["own"] = [{"symbol": h.symbol, "name": h.name, "qty": h.qty - auto_pos.get(h.symbol, {}).get("qty", 0),
                           "avg": h.avg_price, "price": h.cur_price, "currency": h.currency}
                          for h in acct.holdings.values()
                          if h.qty - auto_pos.get(h.symbol, {}).get("qty", 0) > 0]
            out["account_total"] = acct.total_eval
        return json.dumps(out, ensure_ascii=False) if out else None

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
        app.db.x("INSERT OR REPLACE INTO daily_snapshots(date,auto_value,auto_principal,rule_fills,active_rules,detail) "
                 "VALUES(?,?,?,?,?,?)", (today, val["total"] if val else None,
                                         val["state"]["principal"] if val else None, fills, remaining,
                                         await self._snapshot_detail(val)))
        await app.journal.log_summary([today, round(val["total"]) if val else "", val["state"]["principal"] if val else "",
                                       f"{val['pnl_pct']:.2f}" if val else "", f"{day_pct:.2f}" if day_pct is not None else "",
                                       fills, remaining, (h or {}).get("reason", "") if (h := app.control.halted()) else ""])
        text = "\n".join(lines)
        await app.notifier.send(text, category="summary")
        await self._record_closes(val)
        return text

    async def _record_closes(self, val: dict | None) -> None:
        """AI 점검의 '시장 흐름'용: 자동 투자 후보 종목의 장 마감 가격 기록."""
        from .macro import record_closes
        prices = dict((val or {}).get("prices") or {})
        for sym in self.app.universe.candidates:
            if sym not in prices:
                try:
                    prices[sym] = (await self.app.broker.get_quote(sym)).price
                except BrokerError:
                    pass
        record_closes(self.app.db, prices)
