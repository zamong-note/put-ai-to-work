"""주문 게이트웨이 — 모든 주문이 지나는 단 하나의 길.

순서: 전체중지 확인 → 종목 제한(레버리지·인버스·종목중단) → 수량/보유 검증 → 안전 한도
      (넘으면 주문하지 않고 확인 요청) → 주문 → 체결 확인 → 자동투자 장부 반영 → 매매일지 → 체결 알림
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

from . import timeutil
from .broker.base import Broker, BrokerError, BrokerNetworkError
from .control import Control
from .db import DB
from .journal import Journal
from .notify import Notifier
from .universe import Universe

log = logging.getLogger("jangjung")
SOURCE_LABEL = {"rule": "규칙매매", "auto_build": "자동투자 구성", "rebalance": "리밸런싱"}


@dataclass
class OrderIntent:
    source: str            # rule | auto_build | rebalance
    ref_id: str            # 규칙 번호 또는 'tier3' 등
    bucket: str            # rule | auto
    symbol: str
    side: str              # buy | sell
    qty: int
    decision_price: float
    reason: str            # 사람이 읽는 근거
    evidence: dict = field(default_factory=dict)
    detected_at: str | None = None


@dataclass
class OrderOutcome:
    status: str            # filled | partial | submitted | needs_confirm | blocked | failed
    message: str
    order_id: int | None = None
    issues: list[str] = field(default_factory=list)
    filled_qty: int = 0
    avg_price: float | None = None


class Ledger:
    """자동투자 몫의 가상 장부. 같은 계좌 안에서 '맡긴 돈'과 '직접 고른 종목'을 구분한다."""

    def __init__(self, db: DB):
        self.db = db

    def positions(self, bucket: str = "auto") -> dict[str, dict]:
        return {r["symbol"]: r for r in self.db.q("SELECT * FROM positions WHERE bucket=? AND qty>0", (bucket,))}

    def qty(self, symbol: str, bucket: str = "auto") -> int:
        r = self.db.one("SELECT qty FROM positions WHERE bucket=? AND symbol=?", (bucket, symbol))
        return r["qty"] if r else 0

    def apply_fill(self, symbol: str, side: str, qty: int, price: float, bucket: str = "auto") -> None:
        if qty <= 0:
            return
        r = self.db.one("SELECT * FROM positions WHERE bucket=? AND symbol=?", (bucket, symbol))
        cur_q, cur_avg = (r["qty"], r["avg_price"]) if r else (0, 0.0)
        if side == "buy":
            new_q = cur_q + qty
            new_avg = (cur_q * cur_avg + qty * price) / new_q
        else:
            new_q, new_avg = max(0, cur_q - qty), cur_avg
        self.db.x("INSERT INTO positions(bucket,symbol,qty,avg_price) VALUES(?,?,?,?) "
                  "ON CONFLICT(bucket,symbol) DO UPDATE SET qty=excluded.qty, avg_price=excluded.avg_price",
                  (bucket, symbol, new_q, new_avg))
        if bucket == "auto":
            delta = -qty * price if side == "buy" else qty * price
            self.db.x("UPDATE autoinvest SET cash=cash+?, updated_at=? WHERE id=1",
                      (delta, timeutil.now().isoformat()))


class OrderGateway:
    def __init__(self, db: DB, broker: Broker, universe: Universe, control: Control, journal: Journal,
                 notifier: Notifier, settings):
        self.db, self.broker, self.universe, self.control = db, broker, universe, control
        self.journal, self.notifier, self.settings = journal, notifier, settings
        self.ledger = Ledger(db)
        self._lock = asyncio.Lock()

    # ── 한도 계산 ──
    def today_stats(self) -> tuple[int, float]:
        d = timeutil.today_str()
        r = self.db.one("SELECT COUNT(*) n, COALESCE(SUM(CASE WHEN side='buy' THEN qty*decision_price END),0) b "
                        "FROM orders WHERE substr(created_at,1,10)=? AND status NOT IN ('failed','rejected')", (d,))
        return r["n"], r["b"]

    def limit_issues(self, side: str, amount: float) -> list[str]:
        s = self.settings.safety
        n, buy_sum = self.today_stats()
        issues = []
        cap = s.get("max_buy_order_amount") if side == "buy" else s.get("max_sell_order_amount")
        if cap and amount > cap:
            issues.append(f"1회 {'매수' if side == 'buy' else '매도'} 한도 {cap:,.0f}원 초과 ({amount:,.0f}원)")
        if n + 1 > s["max_daily_orders"]:
            issues.append(f"하루 주문 횟수 한도 {s['max_daily_orders']}회 초과")
        if side == "buy" and buy_sum + amount > s["max_daily_buy_amount"]:
            issues.append(f"하루 매수 한도 {s['max_daily_buy_amount']:,.0f}원 초과 (오늘 {buy_sum:,.0f}원 + {amount:,.0f}원)")
        return issues

    async def available_qty(self, symbol: str, bucket: str) -> int:
        if bucket == "auto":
            return self.ledger.qty(symbol)
        acct = await self.broker.get_account()
        held = acct.holdings.get(symbol)
        return max(0, (held.qty if held else 0) - self.ledger.qty(symbol))

    # ── 주문 ──
    async def submit(self, it: OrderIntent, confirmed_over_limit: bool = False,
                     allow_suspended: bool = False) -> OrderOutcome:
        """allow_suspended: 시세 이상으로 멈춘 종목이라도 사용자가 카드에서 '그래도 매도'를 누른 손절 매도만 True."""
        async with self._lock:
            return await self._submit(it, confirmed_over_limit, allow_suspended and it.side == "sell")

    async def _submit(self, it: OrderIntent, confirmed: bool, allow_suspended: bool = False) -> OrderOutcome:
        if h := self.control.halted():
            return OrderOutcome("blocked", f"전체 중지 상태 ({h['reason']})")
        if it.symbol in self.control.suspended() and not allow_suspended:
            return OrderOutcome("blocked", f"{it.symbol} 매매 일시중단 중: {self.control.suspended()[it.symbol]['reason']}")
        if why := self.universe.blocked_reason(it.symbol):
            return OrderOutcome("blocked", why)
        if it.source in ("auto_build", "rebalance") and it.symbol not in self.universe.candidates:
            return OrderOutcome("blocked", "자동투자는 후보 목록 밖 종목을 살 수 없음")
        if it.qty <= 0 or it.side not in ("buy", "sell"):
            return OrderOutcome("blocked", "수량/방향 오류")
        if it.side == "sell":
            avail = await self.available_qty(it.symbol, it.bucket)
            if it.qty > avail:
                return OrderOutcome("blocked", f"매도 가능 수량 부족 (요청 {it.qty}주, 가능 {avail}주)")
        amount = it.qty * it.decision_price
        issues = self.limit_issues(it.side, amount)
        if issues and not confirmed:
            return OrderOutcome("needs_confirm", "안전 한도 초과 — 확인 필요", issues=issues)

        order_type = self.settings.engine.get("order_type", "market")
        name = self.universe.name(it.symbol)
        now = timeutil.now().isoformat()
        evidence = dict(it.evidence, over_limit_confirmed=bool(issues) and confirmed, limit_issues=issues)
        oid = self.db.x(
            "INSERT INTO orders(created_at,detected_at,source,ref_id,bucket,symbol,name,side,qty,order_type,"
            "decision_price,order_price,status,reason,evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, it.detected_at, it.source, it.ref_id, it.bucket, it.symbol, name, it.side, it.qty, order_type,
             it.decision_price, None if order_type == "market" else it.decision_price, "submitting", it.reason,
             json.dumps(evidence, ensure_ascii=False)))
        try:
            res = await self.broker.place_order(it.symbol, it.side, it.qty, order_type,
                                                None if order_type == "market" else it.decision_price)
        except BrokerNetworkError as e:
            self.db.x("UPDATE orders SET status='failed', error=? WHERE id=?", (f"접수여부 불명: {e}", oid))
            self.control.halt(f"주문 #{oid} 통신 오류 — 접수 여부 불명, MTS 확인 필요", by="system")
            await self._finish(oid)
            return OrderOutcome("failed", f"통신 오류로 접수 여부를 알 수 없어 전체 중지함: {e}", oid)
        except BrokerError as e:
            self.db.x("UPDATE orders SET status='failed', error=? WHERE id=?", (str(e), oid))
            await self._finish(oid)
            return OrderOutcome("failed", f"주문 실패: {e}", oid)
        if not res.ok:
            self.db.x("UPDATE orders SET status='rejected', error=? WHERE id=?", (res.message, oid))
            await self._finish(oid)
            return OrderOutcome("failed", f"주문 거부: {res.message}", oid)
        self.db.x("UPDATE orders SET status='submitted', broker_order_no=? WHERE id=?", (res.order_no, oid))

        # 체결 확인 (짧게 대기). 남은 수량은 엔진이 주기적으로 다시 확인한다.
        deadline = asyncio.get_event_loop().time() + self.settings.engine.get("fill_wait_seconds", 6)
        while True:
            await self.refresh_fill(oid)
            o = self.db.one("SELECT * FROM orders WHERE id=?", (oid,))
            if o["status"] == "filled" or asyncio.get_event_loop().time() > deadline:
                break
            await asyncio.sleep(1.5)
        if o["status"] == "filled":
            await self._finish(oid)
        return OrderOutcome(o["status"], "체결" if o["status"] == "filled" else "접수(미체결 잔량 추적 중)",
                            oid, filled_qty=o["filled_qty"], avg_price=o["avg_fill_price"])

    async def refresh_fill(self, oid: int) -> None:
        o = self.db.one("SELECT * FROM orders WHERE id=?", (oid,))
        if not o or o["status"] not in ("submitted", "partial") or not o["broker_order_no"]:
            return
        try:
            f = await self.broker.get_fill(o["broker_order_no"], o["symbol"])
        except BrokerError as e:
            log.warning("체결조회 실패 #%s: %s", oid, e)
            return
        status = "filled" if f.filled_qty >= o["qty"] else ("partial" if f.filled_qty > 0 else "submitted")
        delta = f.filled_qty - o["ledger_qty"]
        if delta > 0 and o["bucket"] == "auto":
            self.ledger.apply_fill(o["symbol"], o["side"], delta, f.avg_price or o["decision_price"])
        self.db.x("UPDATE orders SET status=?, filled_qty=?, avg_fill_price=?, ledger_qty=? WHERE id=?",
                  (status, f.filled_qty, f.avg_price, max(o["ledger_qty"], f.filled_qty), oid))

    async def reconcile_open(self) -> list[int]:
        """미체결 주문 재확인. 새로 체결 완료된 주문 id를 돌려준다."""
        done = []
        for o in self.db.q("SELECT id FROM orders WHERE status IN ('submitted','partial')"):
            await self.refresh_fill(o["id"])
            if self.db.one("SELECT status FROM orders WHERE id=?", (o["id"],))["status"] == "filled":
                await self._finish(o["id"])
                done.append(o["id"])
        return done

    async def _finish(self, oid: int) -> None:
        """최종 상태 주문 → 매매일지 1줄 + 알림."""
        o = self.db.one("SELECT * FROM orders WHERE id=?", (oid,))
        if o["journaled"]:
            return
        self.db.x("UPDATE orders SET journaled=1 WHERE id=?", (oid,))
        await self.journal.log_order(o, SOURCE_LABEL.get(o["source"], o["source"]))
        side = "매수" if o["side"] == "buy" else "매도"
        if o["status"] == "filled":
            txt = (f"[체결] {o['name']} {side} {o['filled_qty']}주 @ {o['avg_fill_price']:,.0f}원\n"
                   f"근거: {o['reason']}\n판단 시세 {o['decision_price']:,.0f}원 · 주문번호 {o['broker_order_no']}")
        else:
            txt = f"[주문 실패] {o['name']} {side} {o['qty']}주\n근거: {o['reason']}\n오류: {o['error']}"
        await self.notifier.send(txt, category="fill" if o["status"] == "filled" else None)

    async def end_of_day_report_unfilled(self) -> list[dict]:
        return self.db.q("SELECT * FROM orders WHERE status IN ('submitted','partial')")
