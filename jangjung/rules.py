"""규칙 매매: 정형 규칙 검증 → 확인 카드 → 장중 조건 평가 → 게이트웨이 주문."""
from __future__ import annotations

import logging
import math
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import timeutil
from .broker.base import Account, Quote
from .orders import OrderGateway, OrderIntent
from .pending import PendingStore

log = logging.getLogger("jangjung")

CONDITIONS = {
    "price_at_or_above": ("현재가", "≥"),
    "price_at_or_below": ("현재가", "≤"),
    "return_at_or_above": ("수익률", "≥"),
    "return_at_or_below": ("수익률", "≤"),
}
QTY_MODES = {"shares", "fraction", "amount"}


def is_stop_loss(r: dict) -> bool:
    """손절 규칙 = 가격/수익률이 '이하'가 되면 파는 규칙."""
    return r["side"] == "sell" and r["condition"].endswith("below")


def untradable(q: Quote) -> bool:
    """거래정지 등 주문 자체가 불가능한 시세."""
    return bool(q.halted) or q.status_code == "58" or not q.price


def cond_text(condition: str, threshold: float) -> str:
    what, op = CONDITIONS[condition]
    if what == "현재가":
        return f"현재가 {op} {threshold:,.0f}원"
    return f"수익률 {op} {threshold:+g}%"


def qty_text(side: str, mode: str, value: float) -> str:
    verb = "매수" if side == "buy" else "매도"
    if mode == "shares":
        return f"{int(value)}주 {verb}"
    if mode == "amount":
        return f"{value:,.0f}원어치 {verb}"
    return "전량 매도" if value >= 1 else f"보유의 {value * 100:g}% {verb}"


def rule_line(r: dict) -> str:
    st = {"active": "감시 중", "paused": "일시정지", "waiting_confirm": "확인 대기", "done": "완료",
          "cancelled": "취소"}.get(r["status"], r["status"])
    return (f"#{r['id']} {r['name']}({r['symbol']}) · {cond_text(r['condition'], r['threshold'])} → "
            f"{qty_text(r['side'], r['qty_mode'], r['qty_value'])} · {r['triggered']}/{r['max_triggers']}회 · {st}")


@dataclass
class Evaluation:
    met: bool
    metric: float | None     # 현재가 또는 수익률
    qty: int
    note: str = ""


class RuleService:
    def __init__(self, db, universe, broker, gateway: OrderGateway, pending: PendingStore, notifier, settings):
        self.db, self.universe, self.broker, self.gw = db, universe, broker, gateway
        self.pending, self.notifier, self.settings = pending, notifier, settings
        pending.register("rules_add", self._on_rules_add)
        pending.register("rule_change", self._on_rule_change)
        pending.register("order_over_limit", self._on_order_over_limit)
        pending.register("stoploss_anomaly", self._on_stoploss_anomaly)

    # ── 조회 ──
    def list(self, include_inactive: bool = False) -> list[dict]:
        if include_inactive:
            return self.db.q("SELECT * FROM rules ORDER BY id DESC LIMIT 50")
        return self.db.q("SELECT * FROM rules WHERE status IN ('active','paused','waiting_confirm') ORDER BY id")

    def active_count(self) -> int:
        return self.db.one("SELECT COUNT(*) n FROM rules WHERE status IN ('active','waiting_confirm')")["n"]

    # ── 검증 + 카드 ──
    async def validate(self, drafts: list[dict]) -> tuple[list[dict], list[str], list[str]]:
        """(정규화된 규칙, 오류, 경고). 오류가 하나라도 있으면 등록 카드를 만들지 않는다."""
        out, errors, warnings = [], [], []
        acct: Account | None = None
        for i, d in enumerate(drafts, 1):
            tag = f"규칙{i}"
            sym = str(d.get("symbol", "")).strip()
            if sym not in self.universe.master:
                errors.append(f"{tag}: 종목코드 '{sym}'를 찾을 수 없음 — search_stock으로 확인 필요")
                continue
            if why := self.universe.blocked_reason(sym):
                errors.append(f"{tag}: {self.universe.name(sym)} — {why}")
                continue
            side, cond, mode = d.get("side"), d.get("condition"), d.get("qty_mode")
            th, qv = d.get("threshold"), d.get("qty_value")
            if side not in ("buy", "sell") or cond not in CONDITIONS or mode not in QTY_MODES:
                errors.append(f"{tag}: 방향/조건/수량방식 값이 올바르지 않음")
                continue
            if th is None or qv is None or qv <= 0:
                errors.append(f"{tag}: 조건 값이나 수량/금액이 빠짐 — 사용자에게 물어볼 것")
                continue
            if mode == "fraction" and (side != "sell" or not 0 < qv <= 1):
                errors.append(f"{tag}: 비율(fraction)은 매도에만, 0~1 사이로")
                continue
            if mode == "amount" and side != "buy":
                errors.append(f"{tag}: 금액 지정은 매수에만 가능 (매도는 주수 또는 비율)")
                continue
            if cond.startswith("price") and th <= 0:
                errors.append(f"{tag}: 가격 조건은 0보다 커야 함")
                continue
            try:
                q = await self.broker.get_quote(sym)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{tag}: 시세 조회 실패 ({e}) — 잠시 후 다시")
                continue
            if acct is None:
                acct = await self.broker.get_account()
            held = acct.holdings.get(sym)
            avail = max(0, (held.qty if held else 0) - self.gw.ledger.qty(sym))
            name = self.universe.name(sym)
            if cond.startswith("return") and not held:
                errors.append(f"{tag}: {name}을(를) 보유하고 있지 않아 수익률 조건을 쓸 수 없음")
                continue
            if side == "sell" and avail == 0:
                warnings.append(f"{tag}: 현재 {name} 매도 가능 수량 0주 — 보유 후에만 실행됨")
            if cond.startswith("price") and abs(th / q.price - 1) > 0.3:
                warnings.append(f"{tag}: 조건가 {th:,.0f}원이 현재가 {q.price:,.0f}원과 30% 이상 차이 — 오타가 아닌지 확인")
            if cond == "return_at_or_below" and th > 0:
                warnings.append(f"{tag}: '수익률 ≤ +{th}%'는 이미 충족될 수 있음 — 손절이면 음수여야 함")
            # 예상 주문 금액
            px = th if cond.startswith("price") else q.price
            if mode == "amount":
                est_qty = math.floor(qv / px)
            elif mode == "shares":
                est_qty = int(qv)
            else:
                est_qty = math.floor(avail * qv)
            est_amt = est_qty * px
            limit_issues = self.gw.limit_issues(side, est_amt)
            if side == "buy" and est_qty == 0:
                errors.append(f"{tag}: {qv:,.0f}원으로는 1주도 살 수 없음 (주가 약 {px:,.0f}원)")
                continue
            out.append({
                "symbol": sym, "name": name, "side": side, "condition": cond, "threshold": float(th),
                "qty_mode": mode, "qty_value": float(qv), "max_triggers": int(d.get("max_triggers") or 1),
                "cur_price": q.price, "avg_price": held.avg_price if held else None, "est_qty": est_qty,
                "est_amount": est_amt, "limit_issues": [i for i in limit_issues if "한도" in i and "1회" in i],
            })
        return out, errors, warnings

    async def propose(self, source_text: str, drafts: list[dict]) -> dict:
        rules, errors, warnings = await self.validate(drafts)
        if errors:
            return {"ok": False, "errors": errors, "warnings": warnings}
        over = [r for r in rules if r["limit_issues"]]
        lines = [f"[규칙 등록 확인]", f"원문: \"{source_text}\""]
        for i, r in enumerate(rules, 1):
            cur = f"현재가 {r['cur_price']:,.0f}원"
            if r["avg_price"]:
                ret = (r["cur_price"] / r["avg_price"] - 1) * 100
                cur += f" · 평단 {r['avg_price']:,.0f}원 · 수익률 {ret:+.1f}%"
            lines.append(f"{i}) {r['name']} · {cond_text(r['condition'], r['threshold'])} → "
                         f"{qty_text(r['side'], r['qty_mode'], r['qty_value'])} (예상 {r['est_qty']}주) · 최대 {r['max_triggers']}회\n   {cur}")
        if len(rules) > 1:
            lines.append("※ 한 지시에서 나온 규칙은 한 묶음 — 전량 매도가 실행되면 나머지 매도 규칙은 자동 종료")
        for w in warnings:
            lines.append(f"⚠ {w}")
        if over:
            for r in over:
                lines.append(f"⛔ {r['name']}: {'; '.join(r['limit_issues'])}")
            lines.append("안전 한도를 넘는 규칙이라 바로 등록하지 않았습니다. 그래도 등록하면 실행 시점에 한 번 더 확인을 받습니다.")
            actions = [("한도 초과 알고 등록", "approve_over"), ("취소", "cancel")]
        else:
            actions = [("등록", "approve"), ("취소", "cancel")]
        pid = await self.pending.create("rules_add", {"source_text": source_text, "rules": rules}, "\n".join(lines),
                                        actions)
        return {"ok": True, "pending_id": pid, "rules": len(rules), "over_limit": bool(over), "warnings": warnings}

    async def _on_rules_add(self, action: str, payload: dict, pid: str) -> str:
        if action not in ("approve", "approve_over"):
            return "규칙 등록을 취소했습니다."
        gid = "G" + secrets.token_hex(3)
        now = timeutil.now().isoformat()
        ids = []
        for r in payload["rules"]:
            note = "한도초과 승인됨(실행 시 재확인)" if r["limit_issues"] else None
            ids.append(self.db.x(
                "INSERT INTO rules(group_id,symbol,name,side,condition,threshold,qty_mode,qty_value,max_triggers,"
                "status,source_text,note,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (gid, r["symbol"], r["name"], r["side"], r["condition"], r["threshold"], r["qty_mode"],
                 r["qty_value"], r["max_triggers"], "active", payload["source_text"], note, now, now)))
        lines = [rule_line(self.db.one("SELECT * FROM rules WHERE id=?", (i,))) for i in ids]
        return "규칙을 등록했습니다. 장중에 감시합니다.\n" + "\n".join(lines)

    async def propose_change(self, rule_ids: list[int], action: str) -> dict:
        if action not in ("cancel", "pause", "resume"):
            return {"ok": False, "errors": ["action은 cancel/pause/resume"]}
        rows = [self.db.one("SELECT * FROM rules WHERE id=?", (int(i),)) for i in rule_ids]
        missing = [i for i, r in zip(rule_ids, rows) if not r]
        if missing:
            return {"ok": False, "errors": [f"없는 규칙 번호: {missing}"]}
        label = {"cancel": "삭제", "pause": "일시정지", "resume": "재개"}[action]
        text = f"[규칙 {label} 확인]\n" + "\n".join(rule_line(r) for r in rows)
        pid = await self.pending.create("rule_change", {"ids": [r["id"] for r in rows], "action": action}, text,
                                        [(label, "approve"), ("그대로 두기", "cancel")])
        return {"ok": True, "pending_id": pid}

    async def _on_rule_change(self, action: str, payload: dict, pid: str) -> str:
        if action != "approve":
            return "변경하지 않았습니다."
        status = {"cancel": "cancelled", "pause": "paused", "resume": "active"}[payload["action"]]
        for i in payload["ids"]:
            self.db.x("UPDATE rules SET status=?, updated_at=? WHERE id=? AND status NOT IN ('done','cancelled')",
                      (status, timeutil.now().isoformat(), i))
        return f"규칙 {payload['ids']} → {status}"

    # ── 평가 ──
    def evaluate(self, r: dict, q: Quote, acct: Account, auto_qty: int) -> Evaluation:
        held = acct.holdings.get(r["symbol"])
        avail = max(0, (held.qty if held else 0) - auto_qty)
        if r["condition"].startswith("price"):
            metric = q.price
        else:
            if not held or held.avg_price <= 0:
                return Evaluation(False, None, 0, "보유 없음")
            metric = (q.price / held.avg_price - 1) * 100
        met = metric >= r["threshold"] if r["condition"].endswith("above") else metric <= r["threshold"]
        if not met:
            return Evaluation(False, metric, 0)
        if r["qty_mode"] == "shares":
            qty = int(r["qty_value"])
            if r["side"] == "sell":
                qty = min(qty, avail)
        elif r["qty_mode"] == "amount":
            qty = math.floor(r["qty_value"] / q.price)
        else:
            qty = math.floor(avail * r["qty_value"])
        if qty <= 0:
            return Evaluation(False, metric, 0, "주문 가능 수량 0주")
        return Evaluation(True, metric, qty)

    async def fire(self, r: dict, ev: Evaluation, q: Quote, detected_at: str, confirmed: bool = False,
                   anomaly_override: str | None = None) -> str:
        metric_txt = f"현재 {ev.metric:,.0f}원" if r["condition"].startswith("price") else f"현재 {ev.metric:+.1f}%"
        reason = f"규칙 #{r['id']} '{cond_text(r['condition'], r['threshold'])}' 충족 ({metric_txt}) → " \
                 f"{qty_text(r['side'], r['qty_mode'], r['qty_value'])}"
        it = OrderIntent("rule", str(r["id"]), "rule", r["symbol"], r["side"], ev.qty, q.price, reason,
                         {"rule": {k: r[k] for k in ("condition", "threshold", "qty_mode", "qty_value")},
                          "metric": ev.metric, "quote": {"price": q.price, "prev_close": q.prev_close,
                                                         "change_pct": q.change_pct}},
                         detected_at)
        if anomaly_override:
            it.reason += " · 시세 이상 중 사용자 확인 후 매도"
            it.evidence["anomaly_override"] = anomaly_override
        out = await self.gw.submit(it, confirmed_over_limit=confirmed, allow_suspended=bool(anomaly_override))
        now = timeutil.now().isoformat()
        if out.status == "needs_confirm":
            self.db.x("UPDATE rules SET status='waiting_confirm', updated_at=? WHERE id=?", (now, r["id"]))
            ttl = self.settings.engine.get("over_limit_card_ttl_min", 10)
            await self.pending.create(
                "order_over_limit", {"rule_id": r["id"]},
                f"[한도 초과 주문 확인]\n{r['name']} {'매수' if r['side'] == 'buy' else '매도'} {ev.qty}주 "
                f"(약 {ev.qty * q.price:,.0f}원)\n근거: {reason}\n" + "\n".join(f"⛔ {i}" for i in out.issues) +
                f"\n{ttl}분 안에 누르지 않으면 실행하지 않고 규칙을 일시정지합니다.",
                [("주문 실행", "approve"), ("실행 안 함", "cancel")], ttl_min=ttl)
            return "needs_confirm"
        if out.status == "blocked":
            self.db.x("UPDATE rules SET note=?, updated_at=? WHERE id=?", (out.message, now, r["id"]))
            return "blocked"
        # 접수/체결/실패 모두 '조건 충족 → 주문 시도 1회'로 센다 (같은 조건으로 연속 주문 방지)
        trig = r["triggered"] + 1
        status = "done" if trig >= r["max_triggers"] else "active"
        if out.status == "failed":
            status = "paused"
        self.db.x("UPDATE rules SET triggered=?, status=?, note=?, updated_at=? WHERE id=?",
                  (trig, status, out.message, now, r["id"]))
        if out.status == "failed":
            await self.notifier.send(f"규칙 #{r['id']} 주문이 실패해 규칙을 일시정지했습니다.\n{out.message}")
        if r["side"] == "sell" and out.status in ("filled", "partial", "submitted"):
            await self._close_siblings(r)
        return out.status

    async def _close_siblings(self, r: dict) -> None:
        avail = await self.gw.available_qty(r["symbol"], "rule")
        if avail > 0:
            return
        sib = self.db.q("SELECT * FROM rules WHERE group_id=? AND id<>? AND side='sell' AND status IN ('active','paused')",
                        (r["group_id"], r["id"]))
        for s in sib:
            self.db.x("UPDATE rules SET status='done', note='보유 0주 — 같은 묶음 규칙 실행으로 종료', updated_at=? WHERE id=?",
                      (timeutil.now().isoformat(), s["id"]))
        if sib:
            await self.notifier.send("보유 수량이 0주가 되어 같은 묶음의 매도 규칙 " +
                                     ", ".join(f"#{s['id']}" for s in sib) + "을(를) 종료했습니다.")

    async def _on_order_over_limit(self, action: str, payload: dict, pid: str) -> str:
        r = self.db.one("SELECT * FROM rules WHERE id=?", (payload["rule_id"],))
        if not r or r["status"] != "waiting_confirm":
            return "규칙 상태가 바뀌어 실행하지 않았습니다."
        if action != "approve":
            self.db.x("UPDATE rules SET status='paused', note=?, updated_at=? WHERE id=?",
                      ("한도 초과 주문 미승인/만료로 일시정지", timeutil.now().isoformat(), r["id"]))
            return f"실행하지 않았습니다. 규칙 #{r['id']}는 일시정지 상태입니다 (재개하려면 말씀해 주세요)."
        q = await self.broker.get_quote(r["symbol"])
        acct = await self.broker.get_account()
        ev = self.evaluate(r, q, acct, self.gw.ledger.qty(r["symbol"]))
        if not ev.met:
            self.db.x("UPDATE rules SET status='active', updated_at=? WHERE id=?", (timeutil.now().isoformat(), r["id"]))
            return "확인하는 사이 조건이 더 이상 충족되지 않아 주문하지 않았습니다. 규칙은 계속 감시합니다."
        self._set_status(r["id"], "active")
        r["status"] = "active"
        res = await self.fire(r, ev, q, timeutil.now().isoformat(), confirmed=True)
        return f"주문 처리: {res}"

    def _set_status(self, rule_id: int, status: str, note: str | None = None) -> None:
        if note is None:
            self.db.x("UPDATE rules SET status=?, updated_at=? WHERE id=?", (status, timeutil.now().isoformat(), rule_id))
        else:
            self.db.x("UPDATE rules SET status=?, note=?, updated_at=? WHERE id=?",
                      (status, note, timeutil.now().isoformat(), rule_id))

    # ── 시세 이상 중 손절: 자동으로 팔지 않고 '그래도 팔까요?' 확인 ──
    def _held_until(self, rule_id: int) -> datetime | None:
        v = self.db.kv_get(f"sl_hold:{rule_id}")
        return datetime.fromisoformat(v) if v else None

    async def stop_loss_during_anomaly(self, r: dict, q: Quote, acct: Account, auto_qty: int, why: str) -> str | None:
        """종목이 시세 이상으로 멈춘 동안 손절 조건이 충족되면 확인 카드를 보낸다. 카드를 보냈으면 'card'."""
        if not is_stop_loss(r) or untradable(q):
            return None
        hold = self._held_until(r["id"])
        if hold and timeutil.now() < hold:
            return None
        ev = self.evaluate(r, q, acct, auto_qty)
        if not ev.met:
            return None
        ttl = int(self.settings.engine.get("stop_loss_card_ttl_min", 30))
        metric_txt = f"현재 {ev.metric:,.0f}원" if r["condition"].startswith("price") else f"현재 {ev.metric:+.1f}%"
        issues = self.gw.limit_issues("sell", ev.qty * q.price)
        self._set_status(r["id"], "waiting_confirm", "시세 이상 — 손절 매도 확인 대기")
        await self.pending.create(
            "stoploss_anomaly", {"rule_id": r["id"], "why": why},
            f"[시세 이상 중 손절 조건 충족 — 그래도 팔까요?]\n"
            f"{r['name']} · 규칙 #{r['id']} '{cond_text(r['condition'], r['threshold'])}' 충족 ({metric_txt})\n"
            f"매도 {ev.qty}주 · 약 {ev.qty * q.price:,.0f}원 (현재가 {q.price:,.0f}원, 전일 대비 {q.change_pct:+.1f}%)\n"
            f"자동 매매를 멈춘 이유: {why}\n"
            "⚠ 시장가 주문이라 급변 중에는 체결가가 지금 가격과 크게 다를 수 있습니다.\n"
            + "".join(f"⛔ {i}\n" for i in issues) +
            f"'오늘은 보류'를 누르면 오늘은 다시 묻지 않습니다. {ttl}분 안에 답이 없으면 팔지 않고, "
            "조건이 계속 충족되면 다시 묻습니다.",
            [("그래도 매도", "sell"), ("오늘은 보류", "hold")], ttl_min=ttl)
        return "card"

    async def _on_stoploss_anomaly(self, action: str, payload: dict, pid: str) -> str:
        r = self.db.one("SELECT * FROM rules WHERE id=?", (payload["rule_id"],))
        if not r or r["status"] != "waiting_confirm":
            return "규칙 상태가 바뀌어 실행하지 않았습니다."
        if action == "hold":
            tomorrow = (timeutil.now() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            self.db.kv_set(f"sl_hold:{r['id']}", tomorrow.isoformat())
            self._set_status(r["id"], "active", "시세 이상 중 손절 보류 (오늘)")
            return (f"팔지 않았습니다. 규칙 #{r['id']}는 계속 감시하지만 오늘은 다시 묻지 않습니다.\n"
                    "※ 이 종목 매매를 재개하면 그때부터는 조건 충족 시 바로 매도합니다.")
        if action != "sell":   # 만료
            self._set_status(r["id"], "active", "시세 이상 중 손절 확인 만료")
            return "답이 없어 팔지 않았습니다. 조건이 계속 충족되면 다시 묻습니다."
        self._set_status(r["id"], "active")
        q = await self.broker.get_quote(r["symbol"])
        if untradable(q):
            return "지금은 거래정지 상태라 매도할 수 없습니다. 규칙은 계속 감시합니다."
        acct = await self.broker.get_account()
        ev = self.evaluate(r, q, acct, self.gw.ledger.qty(r["symbol"]))
        if not ev.met:
            return "확인하는 사이 손절 조건이 더 이상 충족되지 않아 팔지 않았습니다. 규칙은 계속 감시합니다."
        r["status"] = "active"
        res = await self.fire(r, ev, q, timeutil.now().isoformat(), confirmed=True,
                              anomaly_override=payload.get("why") or "시세 이상")
        label = {"filled": "체결", "partial": "일부 체결", "submitted": "접수", "blocked": "주문 차단",
                 "failed": "주문 실패"}.get(res, res)
        if res == "blocked":
            note = self.db.one("SELECT note FROM rules WHERE id=?", (r["id"],))["note"]
            return f"손절 매도 처리: {label} — {note}"
        return f"손절 매도 처리: {label}"
