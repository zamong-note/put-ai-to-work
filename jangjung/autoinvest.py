"""자동 투자: 성향 질문 → 단계 추천 → 제안·승인 → 분할 매수 → 월 1회 리밸런싱 → 손실 한도 중단.

종목과 비중은 config/strategies.json 의 단계별 정의에서만 나온다 (LLM이 고르지 않음).
"""
from __future__ import annotations

import json
import logging
import math

from . import timeutil
from .orders import OrderGateway, OrderIntent
from .pending import PendingStore

log = logging.getLogger("jangjung")

RISK_QUESTIONS = [
    ("이 돈을 투자해 둘 기간은?", ["1년 미만", "1~2년", "2~3년", "3~5년", "5년 이상"]),
    ("1년 안에 -10% 손실이 나면?", ["전부 판다", "일부 판다", "그대로 둔다", "조금 더 산다", "적극적으로 더 산다"]),
    ("견딜 수 있는 최대 손실은?", ["-3%", "-5%", "-10%", "-15%", "-20% 이상"]),
    ("투자 목적은?", ["원금 보존", "예금보다 조금 더", "균형 성장", "높은 성장", "최대 수익"]),
    ("이 돈이 필요해질 가능성은?", ["1년 안에 쓸 돈", "2년 안에 쓸 수도", "보통", "거의 안 씀", "당분간 쓸 일 없는 여유자금"]),
    ("투자 경험은?", ["없음", "예금·적금 위주", "펀드·ETF", "주식 직접투자 3년 미만", "주식 직접투자 3년 이상"]),
]


def score_risk(answers: list[int]) -> dict:
    if len(answers) != len(RISK_QUESTIONS) or any(a not in (1, 2, 3, 4, 5) for a in answers):
        raise ValueError(f"답변은 {len(RISK_QUESTIONS)}개, 각각 1~5 번호여야 합니다")
    avg = sum(answers) / len(answers)
    rec = min(max(1, round(avg)), answers[2])  # 감내 가능 손실(3번)을 넘는 단계는 추천하지 않음
    return {"answers": answers, "average": round(avg, 2), "recommended_tier": rec,
            "cap_reason": "최대 손실 감내 답변으로 상한 적용" if rec < round(avg) else None}


class AutoInvest:
    def __init__(self, db, universe, broker, gateway: OrderGateway, pending: PendingStore, notifier, settings):
        self.db, self.universe, self.broker, self.gw = db, universe, broker, gateway
        self.pending, self.notifier, self.settings = pending, notifier, settings
        self.cfg = settings.autoinvest
        pending.register("autoinvest_start", self._on_start)
        pending.register("tier_pick", self._on_tier_pick)
        pending.register("rebalance_confirm", self._on_rebalance_confirm)
        pending.register("loss_resume", self._on_loss_resume)

    # ── 상태 ──
    def state(self) -> dict | None:
        r = self.db.one("SELECT * FROM autoinvest WHERE id=1")
        if r:
            r["targets"] = json.loads(r["targets"])
        return r

    def rebalance_mode(self) -> str:
        return self.db.kv_get("rebalance_mode", self.cfg.get("rebalance_mode", "auto"))

    def set_rebalance_mode(self, mode: str) -> None:
        if mode not in ("auto", "confirm"):
            raise ValueError("auto 또는 confirm")
        self.db.kv_set("rebalance_mode", mode)

    async def valuation(self) -> dict | None:
        st = self.state()
        if not st:
            return None
        pos = self.gw.ledger.positions()
        rows, total = [], st["cash"]
        prices = {}
        for sym in set(pos) | set(st["targets"]):
            q = await self.broker.get_quote(sym)
            prices[sym] = q.price
            qty = pos.get(sym, {}).get("qty", 0)
            total += qty * q.price
            rows.append({"symbol": sym, "name": self.universe.name(sym), "qty": qty, "price": q.price,
                         "value": qty * q.price, "target": st["targets"].get(sym, 0)})
        for r in rows:
            r["weight"] = r["value"] / total * 100 if total > 0 else 0
            r["dev"] = r["weight"] - r["target"]
        rows.sort(key=lambda r: -r["target"])
        pnl = (total / st["principal"] - 1) * 100 if st["principal"] else 0
        return {"state": st, "total": total, "cash": st["cash"], "pnl_pct": pnl, "rows": rows, "prices": prices}

    # ── 성향/제안 ──
    def last_profile(self) -> dict | None:
        return self.db.kv_get("risk_profile")

    def save_profile(self, answers: list[int]) -> dict:
        res = score_risk(answers)
        self.db.kv_set("risk_profile", res)
        return res

    def _proposal_text(self, amount: float, tier: int, mismatch: str | None, change: bool) -> str:
        spec = self.universe.tier(tier)
        groups: dict[str, float] = {}
        for sym, w in spec["targets"].items():
            groups[self.universe.category_label(sym)] = groups.get(self.universe.category_label(sym), 0) + w
        lines = [f"[자동 투자 {'단계 변경 ' if change else ''}제안 · {tier}단계({spec['label']}) · {amount / 10000:,.0f}만 원]",
                 " · ".join(f"{g} {w:g}%" for g, w in groups.items())]
        for sym, w in spec["targets"].items():
            lines.append(f" - {self.universe.name(sym)}({sym}) {w:g}% · 약 {amount * w / 100:,.0f}원")
        mode = "자동 실행" if self.rebalance_mode() == "auto" else "매번 확인"
        lines += [f"손실 한도: 전체 -{spec['loss_limit_pct']}% 도달 시 매매 중단 후 확인 요청",
                  f"리밸런싱: 매월 첫 거래일 {self.settings.engine['rebalance_time']}, 비중 ±{self.cfg['rebalance_band_pp']:g}%p 벗어나면 ({mode})",
                  f"구성: {self.cfg['build_tranches']}회 분할 매수 (거래일 {self.settings.engine['build_time']})" if not change
                  else "구성: 승인 시 목표 비중으로 조정 (매도 먼저, 매수는 분할)",
                  "종목은 미리 정한 후보 목록 안에서만 고릅니다."]
        if mismatch:
            lines.append(f"⚠ {mismatch}")
        return "\n".join(lines)

    async def propose(self, amount: float, tier: int, mismatch_confirmed: bool = False) -> dict:
        if tier not in self.universe.tiers:
            return {"ok": False, "errors": ["위험 단계는 1~5"]}
        prof = self.last_profile()
        if not prof:
            return {"ok": False, "errors": ["성향 질문을 먼저 진행해야 합니다 (get_risk_questions)"]}
        st = self.state()
        change = bool(st and st["status"] in ("building", "active", "halted_loss"))
        if change:
            amount = st["principal"]
        else:
            if amount < 1_000_000:
                return {"ok": False, "errors": ["자동 투자 금액은 100만 원 이상으로 해 주세요"]}
            acct = await self.broker.get_account()
            if amount > acct.cash:
                return {"ok": False, "errors": [f"주문 가능 현금 {acct.cash:,.0f}원보다 큰 금액"]}
        rec = prof["recommended_tier"]
        mismatch = None
        if abs(tier - rec) >= 2:
            mismatch = f"성향 답변 기준 추천은 {rec}단계인데 {tier}단계를 고르셨습니다. 손실 한도 -{self.universe.tier(tier)['loss_limit_pct']}%까지 감수하셔야 합니다."
            if not mismatch_confirmed:
                return {"ok": False, "needs_user_reconfirm": True, "message": mismatch +
                        " 사용자에게 한 번 더 확인받은 뒤 mismatch_confirmed=true로 다시 호출하세요."}
        text = self._proposal_text(amount, tier, mismatch, change)
        actions = [("확인했고 승인" if mismatch else "승인", "approve"), ("단계 변경", "change"), ("취소", "cancel")]
        pid = await self.pending.create("autoinvest_start", {"amount": amount, "tier": tier, "change": change},
                                        text, actions)
        return {"ok": True, "pending_id": pid, "recommended_tier": rec}

    async def _on_tier_pick(self, action: str, payload: dict, pid: str) -> str:
        if not action.startswith("t"):
            return "단계 변경을 취소했습니다."
        tier = int(action[1:])
        res = await self.propose(payload["amount"], tier, mismatch_confirmed=True)
        return "새 제안 카드를 보냈습니다." if res.get("ok") else "; ".join(res.get("errors", []))

    async def _on_start(self, action: str, payload: dict, pid: str) -> str:
        if action == "change":
            rec = (self.last_profile() or {}).get("recommended_tier")
            await self.pending.create("tier_pick", {"amount": payload["amount"]},
                                      f"[단계 선택] 원하는 위험 단계를 고르세요 (성향 추천: {rec}단계)",
                                      [(f"{t}단계 {self.universe.tier(t)['label']}" + (" ★" if t == rec else ""), f"t{t}")
                                       for t in range(1, 6)] + [("취소", "cancel")])
            return "단계 선택 카드를 보냈습니다."
        if action != "approve":
            return "자동 투자 제안을 취소했습니다."
        spec = self.universe.tier(payload["tier"])
        now = timeutil.now().isoformat()
        tranches = self.cfg["build_tranches"]
        if payload["change"]:
            self.db.x("UPDATE autoinvest SET tier=?, targets=?, status='building', tranches_total=?, tranches_done=0, "
                      "last_build_date=NULL, updated_at=? WHERE id=1",
                      (payload["tier"], json.dumps(spec["targets"]), tranches, now))
            self.db.kv_del("loss_ack_pnl")
        else:
            self.db.x("DELETE FROM autoinvest")
            self.db.x("DELETE FROM positions WHERE bucket='auto'")
            self.db.x("INSERT INTO autoinvest(id,tier,principal,cash,targets,status,tranches_total,tranches_done,"
                      "created_at,updated_at) VALUES(1,?,?,?,?,?,?,0,?,?)",
                      (payload["tier"], payload["amount"], payload["amount"], json.dumps(spec["targets"]), "building",
                       tranches, now, now))
        # 시작한 달은 리밸런싱 점검 생략
        self.db.x("UPDATE autoinvest SET last_rebalance_month=? WHERE id=1", (timeutil.now().strftime("%Y-%m"),))
        msg = f"{payload['tier']}단계({spec['label']}) 자동 투자를 시작합니다. {tranches}회에 나눠 매수합니다."
        eng = self.settings.engine
        if timeutil.in_session(timeutil.now(), self.settings.holidays, eng["session_start"], eng["session_end"]):
            res = await self.build_step()
            msg += f"\n1회차 실행: {res}"
        else:
            msg += f"\n다음 거래일 {eng['build_time']}에 1회차를 실행합니다."
        return msg

    # ── 분할 매수 / 목표 조정 ──
    async def build_step(self) -> str:
        st = self.state()
        if not st or st["status"] != "building":
            return "구성 중 아님"
        if st["last_build_date"] == timeutil.today_str():
            return "오늘 회차는 이미 실행"
        k = st["tranches_done"] + 1
        n = st["tranches_total"]
        val = await self.valuation()
        total = val["total"]
        plan = []
        for r in val["rows"]:
            final_qty = math.floor(total * r["target"] / 100 / r["price"])
            if r["qty"] > final_qty:
                desired = final_qty                                   # 줄일 종목은 첫 회차에 바로 목표까지
            else:
                desired = max(r["qty"], math.floor(total * r["target"] / 100 * k / n / r["price"]))
            if desired != r["qty"]:
                plan.append((r, desired - r["qty"], final_qty))
        results = await self._execute(plan, "auto_build", f"tier{st['tier']}",
                                      lambda r, d, f: f"자동투자 구성 {k}/{n}회차 · {r['name']} 목표 {r['target']:g}% "
                                                      f"(현재 {r['weight']:.1f}%) → 이번 회차 {abs(d)}주 {'매수' if d > 0 else '매도'}")
        done = k >= n
        self.db.x("UPDATE autoinvest SET tranches_done=?, last_build_date=?, status=?, updated_at=? WHERE id=1",
                  (k, timeutil.today_str(), "active" if done else "building", timeutil.now().isoformat()))
        tail = " · 구성 완료, 이제 월 1회 리밸런싱으로 관리합니다." if done else ""
        await self.notifier.send(f"[자동 투자 구성 {k}/{n}회차] 주문 {len(results)}건{tail}\n" +
                                 "\n".join(results[:12]), category="auto")
        return f"{k}/{n}회차 주문 {len(results)}건"

    async def _execute(self, plan, source: str, ref: str, reason_fn) -> list[str]:
        """매도 먼저, 그다음 현금 범위 안에서 매수."""
        lines = []
        for r, d, f in sorted(plan, key=lambda x: x[1]):
            side = "buy" if d > 0 else "sell"
            qty = abs(d)
            q = await self.broker.get_quote(r["symbol"])
            if why := self.gw.control.quote_anomaly(q):
                lines.append(f"{r['name']}: 시세 이상으로 이번엔 건너뜀 ({why})")
                continue
            r = dict(r, price=q.price)
            if side == "buy":
                cash = self.state()["cash"]
                qty = min(qty, math.floor(cash / r["price"]))
                if qty <= 0:
                    lines.append(f"{r['name']}: 자동투자 현금 부족으로 매수 생략")
                    continue
            it = OrderIntent(source, ref, "auto", r["symbol"], side, qty, r["price"], reason_fn(r, d, f),
                             {"target_pct": r["target"], "weight_pct": round(r["weight"], 2),
                              "dev_pp": round(r["dev"], 2), "final_qty": f})
            out = await self.gw.submit(it)
            if out.status == "needs_confirm":
                # 자동투자는 한도를 넘으면 그 종목은 이번에 건너뛰고 알린다 (다음 회차/리밸런싱에서 다시 시도)
                lines.append(f"{r['name']}: 안전 한도 초과로 이번엔 건너뜀 ({'; '.join(out.issues)})")
            else:
                label = {"filled": "체결", "partial": "부분체결", "submitted": "접수(미체결)", "failed": "실패",
                         "blocked": "차단"}.get(out.status, out.status)
                extra = "" if out.status == "filled" else f" — {out.message}"
                lines.append(f"{r['name']} {'매수' if side == 'buy' else '매도'} {qty}주: {label}{extra}")
        return lines

    # ── 리밸런싱 ──
    async def rebalance_plan(self) -> tuple[list, dict]:
        val = await self.valuation()
        band = self.cfg["rebalance_band_pp"]
        if not any(abs(r["dev"]) > band for r in val["rows"]):
            return [], val
        plan = []
        for r in val["rows"]:
            if abs(r["dev"]) < self.cfg["min_trade_dev_pp"]:
                continue
            final_qty = math.floor(val["total"] * r["target"] / 100 / r["price"])
            if final_qty != r["qty"]:
                plan.append((r, final_qty - r["qty"], final_qty))
        return plan, val

    def _plan_lines(self, plan) -> list[str]:
        out = []
        for r, d, f in plan:
            label = self.universe.category_label(r["symbol"]) if r["symbol"] in self.universe.candidates else ""
            out.append(f"{label}({r['name']}) 비중 {r['weight']:.0f}% → {r['target']:g}%로 조정 ({abs(d)}주 {'매수' if d > 0 else '매도'})\n"
                       f"  근거: 목표 {r['target']:g}% 대비 {r['dev']:+.1f}%p {'초과' if r['dev'] > 0 else '미달'}")
        return out

    async def monthly_check(self, force: bool = False) -> str:
        st = self.state()
        if not st or st["status"] != "active":
            return "자동투자 활성 상태 아님"
        month = timeutil.now().strftime("%Y-%m")
        if st["last_rebalance_month"] == month and not force:
            return "이번 달 점검 완료"
        self.db.x("UPDATE autoinvest SET last_rebalance_month=? WHERE id=1", (month,))
        plan, val = await self.rebalance_plan()
        md = timeutil.now().strftime("%m/%d")
        if not plan:
            await self.notifier.send(f"[리밸런싱 점검 {md}] 모든 비중이 목표 ±{self.cfg['rebalance_band_pp']:g}%p 안 — 조정 없음",
                                     category="auto")
            return "조정 불필요"
        lines = self._plan_lines(plan)
        if self.rebalance_mode() == "confirm":
            await self.pending.create("rebalance_confirm", {"month": month},
                                      f"[리밸런싱 확인 {md}]\n" + "\n".join(lines),
                                      [("실행", "approve"), ("이번 달 건너뛰기", "cancel")], ttl_min=6 * 60)
            return "확인 요청"
        return await self._run_rebalance(plan, md, lines)

    async def _run_rebalance(self, plan, md, lines) -> str:
        reasons = {r["symbol"]: f"리밸런싱 · {r['name']} 목표 {r['target']:g}% 대비 {r['dev']:+.1f}%p" for r, _, _ in plan}
        res = await self._execute(plan, "rebalance", timeutil.now().strftime("%Y-%m"),
                                  lambda r, d, f: reasons[r["symbol"]])
        await self.notifier.send(f"[리밸런싱 알림 {md}]\n" + "\n".join(lines) + "\n— 결과 —\n" + "\n".join(res),
                                 category="auto")
        return f"리밸런싱 주문 {len(res)}건"

    async def _on_rebalance_confirm(self, action: str, payload: dict, pid: str) -> str:
        if action != "approve":
            return "이번 달 리밸런싱을 건너뜁니다."
        plan, _ = await self.rebalance_plan()
        if not plan:
            return "확인하는 사이 비중이 목표 범위 안으로 돌아와 조정하지 않았습니다."
        return await self._run_rebalance(plan, timeutil.now().strftime("%m/%d"), self._plan_lines(plan))

    # ── 손실 한도 ──
    async def check_loss(self) -> str | None:
        st = self.state()
        if not st or st["status"] not in ("building", "active"):
            return None
        val = await self.valuation()
        limit = self.universe.tier(st["tier"])["loss_limit_pct"]
        ack = self.db.kv_get("loss_ack_pnl")
        trigger = -limit if ack is None else min(-limit, ack - limit / 2)
        if val["pnl_pct"] > trigger:
            return None
        self.db.x("UPDATE autoinvest SET status='halted_loss', updated_at=? WHERE id=1", (timeutil.now().isoformat(),))
        await self.pending.create(
            "loss_resume", {"pnl": val["pnl_pct"], "prev_status": st["status"]},
            f"[손실 한도 도달 — 자동 투자 매매 중단]\n{st['tier']}단계 손실 한도 -{limit}% · 현재 {val['pnl_pct']:+.2f}%\n"
            f"평가액 {val['total']:,.0f}원 / 원금 {st['principal']:,.0f}원\n"
            "보유 종목은 그대로 두고 신규 매매·리밸런싱을 멈췄습니다. 어떻게 할까요?",
            [("매매 재개", "resume"), ("중단 유지", "keep")], ttl_min=7 * 24 * 60)
        return f"손실 한도 도달 {val['pnl_pct']:+.2f}%"

    async def _on_loss_resume(self, action: str, payload: dict, pid: str) -> str:
        if action != "resume":
            return "자동 투자 매매를 계속 중단해 둡니다. 재개하려면 말씀해 주세요."
        self.db.kv_set("loss_ack_pnl", payload["pnl"])
        self.db.x("UPDATE autoinvest SET status=?, updated_at=? WHERE id=1",
                  (payload.get("prev_status") or "active", timeutil.now().isoformat()))
        return "자동 투자를 재개했습니다. 확인 시점 대비 추가로 손실 한도의 절반만큼 더 빠지면 다시 멈춥니다."
