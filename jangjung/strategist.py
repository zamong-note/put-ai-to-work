"""AI 전략: 환율·경제지표·시장 흐름·투자 현황을 주기적으로 점검하고, 자동 투자의 목표 비중을 조정·실행한다.

LLM이 하는 일과 코드가 막는 일
- Claude: 데이터를 읽고 국가별 판단과 '목표 비중'을 근거와 함께 제출 (submit_strategy 도구 하나만 씀)
- 코드:   ① 자동 투자 후보 종목만 ② 단계 기본 비중 ± max_tilt_pp 안에서만 ③ 합계 100%
          ④ 한 번에 바꾸는 금액은 평가액의 max_turnover_pct 이하 ⑤ 주문은 기존 게이트웨이(안전 한도·중지·시세 이상)
          ⑥ mode: off(점검 안 함) / propose(제안 카드 → 사용자가 [실행]) / auto(바로 실행 후 보고)
- 손실 한도·전체 중지·'멈춰'는 그대로 우선한다. 사용자가 직접 등록한 규칙은 건드리지 않는다.
"""
from __future__ import annotations

import json
import logging
import math

from . import macro, timeutil
from .orders import OrderIntent

log = logging.getLogger("jangjung")

STRATEGIST_PROMPT = """너는 '장중대리'의 전략 담당이다. 사용자가 고른 위험 단계의 자동 투자 포트폴리오를 점검하고,
환율·국가별 경제지표·시장 흐름을 근거로 목표 비중을 조정할지 정한다. 결과는 submit_strategy 도구로 한 번만 제출한다.

원칙
- 기본은 '바꾸지 않음'이다. 데이터가 부족하거나 신호가 약하면 기본 비중(base_targets)을 그대로 제출한다.
- 조정은 allowed_symbols 안에서만, 각 종목은 기본 비중 ± max_tilt_pp 안에서만, 합계는 정확히 100이다.
- 근거는 입력 데이터에 있는 숫자만 인용한다 (예: "USD/KRW 30일 +3.2%", "미국 물가상승률 2.9%(2025)"). 없는 데이터를 지어내지 않는다.
- 위험 단계를 넘는 공격적 조정, 레버리지·인버스, 단기 매매 성격의 판단은 하지 않는다.
- 환율: 원화 약세(외화 강세)는 해외 자산 원화 평가에 유리하지만 신규 매수 단가는 비싸진다. 양쪽을 함께 고려한다.
- user_rule_stocks_reference_only 는 사용자가 직접 규칙으로 관리하는 종목이다. 참고만 하고 목표 비중에 넣거나 언급하지 않는다.
- market_momentum·fx 변화율이 비어 있으면 '아직 기록이 쌓이는 중'으로 보고, 그것만으로 위험을 강조하지 않는다.
- 문장은 한국어, 짧게. 사용자에게 그대로 보여 준다. 투자 권유 표현("꼭 사세요")은 쓰지 않는다.
"""

SUBMIT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "투자 현황·시장 점검 요약 2~4문장"},
        "country_views": {"type": "array", "maxItems": 6, "items": {
            "type": "object", "properties": {"country": {"type": "string"}, "view": {"type": "string",
                                             "enum": ["긍정", "중립", "부정"]}, "reason": {"type": "string"}},
            "required": ["country", "view", "reason"]}},
        "risks": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "targets": {"type": "object", "description": "종목코드 → 목표 비중(%), 합계 100",
                    "additionalProperties": {"type": "number"}},
        "reasons": {"type": "object", "description": "바꾸는 종목코드 → 한 줄 근거", "additionalProperties": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["낮음", "보통", "높음"]},
    },
    "required": ["summary", "targets", "confidence"],
}


class StrategyError(ValueError):
    pass


async def ask_claude(app, payload: dict) -> dict:
    """Claude에게 데이터를 주고 submit_strategy 결과를 받는다 (내장 도구 없음, 주문 권한 없음)."""
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, create_sdk_mcp_server, tool
    captured: dict = {}

    @tool("submit_strategy", "점검 결과와 목표 비중을 제출한다. 한 번만 호출한다.", SUBMIT_SCHEMA)
    async def submit_strategy(args):
        captured.clear()
        captured.update(args)
        return {"content": [{"type": "text", "text": "제출됨"}]}

    server = create_sdk_mcp_server(name="st", version="0.1.0", tools=[submit_strategy])
    opts = ClaudeAgentOptions(system_prompt=STRATEGIST_PROMPT, mcp_servers={"st": server}, tools=[],
                              allowed_tools=["mcp__st__submit_strategy"], permission_mode="dontAsk",
                              strict_mcp_config=True, setting_sources=[], model=app.settings.claude_model,
                              thinking={"type": "adaptive"}, max_turns=4, cwd=str(app.settings.data_dir))
    client = ClaudeSDKClient(opts)
    await client.connect()
    try:
        await client.query("다음 데이터를 점검하고 submit_strategy 로 제출하세요.\n"
                           + json.dumps(payload, ensure_ascii=False, default=str))
        async for _ in client.receive_response():
            pass
    finally:
        await client.disconnect()
    if not captured:
        raise StrategyError("AI가 점검 결과를 제출하지 않았습니다")
    return captured


class Strategist:
    def __init__(self, app):
        self.app = app
        self.llm = ask_claude                    # 테스트에서 바꿔 끼움
        app.pending.register("ai_strategy", self._on_card)
        app.db.x("""CREATE TABLE IF NOT EXISTS strategies (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, trigger TEXT, status TEXT,
            tier INTEGER, base TEXT, before TEXT, targets TEXT, result TEXT, data TEXT, note TEXT,
            executed_at TEXT, orders TEXT)""")

    @property
    def cfg(self) -> dict:
        return self.app.settings.ai_strategy

    def latest(self) -> dict | None:
        r = self.app.db.one("SELECT * FROM strategies ORDER BY id DESC LIMIT 1")
        return self._row(r) if r else None

    def history(self, limit: int = 10) -> list[dict]:
        return [self._row(r) for r in self.app.db.q("SELECT * FROM strategies ORDER BY id DESC LIMIT ?", (limit,))]

    @staticmethod
    def _row(r: dict) -> dict:
        r = dict(r)
        for k in ("base", "before", "targets", "result", "orders"):
            r[k] = json.loads(r[k]) if r.get(k) else None
        r.pop("data", None)
        return r

    # ── 검증 (LLM 결과를 그대로 믿지 않는다) ──
    def validate(self, targets: dict, base: dict) -> dict:
        u = self.app.universe
        tilt = float(self.cfg.get("max_tilt_pp", 10))
        allowed = set(base) | {s for s in u.candidates if not u.blocked_reason(s)}
        out = {}
        for sym, v in (targets or {}).items():
            if sym not in allowed:
                raise StrategyError(f"허용되지 않은 종목: {sym}")
            try:
                v = float(v)
            except (TypeError, ValueError) as e:
                raise StrategyError(f"{sym}: 숫자가 아님") from e
            if v < 0:
                raise StrategyError(f"{sym}: 음수 비중")
            if v > 0:
                out[sym] = v
        for sym in base:
            out.setdefault(sym, 0.0)
        for sym, v in out.items():
            if abs(v - base.get(sym, 0)) > tilt + 1e-6:
                raise StrategyError(f"{u.name(sym)}: 기본 {base.get(sym, 0):g}% → {v:g}% (허용 ±{tilt:g}%p 초과)")
        total = sum(out.values())
        if abs(total - 100) > 0.5:
            raise StrategyError(f"비중 합계가 100%가 아님 ({total:g}%)")
        out = {s: round(v * 100 / total, 1) for s, v in out.items() if v > 0}        # 반올림 오차 보정
        drift = round(100 - sum(out.values()), 1)
        if drift:
            k = max(out, key=out.get)
            out[k] = round(out[k] + drift, 1)
        return out

    def cap_turnover(self, cur: dict, new: dict, val: dict) -> tuple[dict, float]:
        """한 번에 바꾸는 금액이 한도를 넘으면 현재 목표 쪽으로 줄인다. (새 목표, 축소 비율)"""
        cap = float(self.cfg.get("max_turnover_pct", 20))
        weights = {r["symbol"]: r["weight"] for r in val["rows"]}
        syms = set(cur) | set(new) | set(weights)
        turnover = sum(abs(new.get(s, 0) - weights.get(s, 0)) for s in syms) / 2
        if turnover <= cap or turnover == 0:
            return new, 1.0
        k = cap / turnover
        scaled = {s: weights.get(s, 0) + (new.get(s, 0) - weights.get(s, 0)) * k for s in syms}
        tot = sum(scaled.values())
        scaled = {s: round(v * 100 / tot, 1) for s, v in scaled.items() if v > 0.05}
        drift = round(100 - sum(scaled.values()), 1)
        if drift:
            mx = max(scaled, key=scaled.get)
            scaled[mx] = round(scaled[mx] + drift, 1)
        return scaled, k

    # ── 점검 ──
    async def review(self, trigger: str = "schedule") -> dict:
        app = self.app
        mode = self.cfg.get("mode", "propose")
        st = app.autoinvest.state()
        data = await macro.snapshot(app)
        val = await app.autoinvest.valuation() if st else None
        can_trade = bool(st and st["status"] == "active" and not app.control.halted() and mode != "off")
        base = app.universe.tier(st["tier"])["targets"] if st else {}
        payload = {
            "data": data,
            "portfolio": None if not val else {
                "tier": st["tier"], "tier_label": app.universe.tier(st["tier"])["label"], "status": st["status"],
                "principal": st["principal"], "value": round(val["total"]), "pnl_pct": round(val["pnl_pct"], 2),
                "loss_limit_pct": app.universe.tier(st["tier"])["loss_limit_pct"],
                "holdings": [{"symbol": r["symbol"], "name": r["name"], "weight": round(r["weight"], 1),
                              "target": r["target"]} for r in val["rows"]]},
            "base_targets": base, "current_targets": st["targets"] if st else {},
            "allowed_symbols": {s: {"name": c["name"], "category": app.universe.category_label(s)}
                                for s, c in app.universe.candidates.items() if not app.universe.blocked_reason(s)},
            "max_tilt_pp": self.cfg.get("max_tilt_pp", 10),
            "user_rule_stocks_reference_only": sorted({r["name"] for r in app.rules.list()}),
        }
        if not st:
            payload["note"] = "자동 투자가 없어 비중 조정은 하지 않는다. 시장 점검만 하고 targets 는 빈 객체로 제출."
        res = await self.llm(app, payload)
        now = timeutil.now().isoformat(timespec="seconds")
        targets, note, status = None, None, "analysis"
        if st:
            try:
                targets = self.validate(res.get("targets") or base, base)
            except StrategyError as e:
                note, status = f"AI 제안을 반영하지 않음: {e}", "rejected"
        if targets is not None:
            changed = any(abs(targets.get(s, 0) - st["targets"].get(s, 0)) >= 0.5
                          for s in set(targets) | set(st["targets"]))
            if not changed:
                status, note = "no_change", "현재 목표 비중 유지"
            elif not can_trade:
                status = "analysis"
                note = "자동 투자가 운용 중이 아니거나 중지 상태라 실행하지 않음" if mode != "off" else "AI 전략 꺼짐"
            else:
                targets, k = self.cap_turnover(st["targets"], targets, val)
                if k < 1:
                    note = f"한 번 변경 한도({self.cfg.get('max_turnover_pct', 20):g}%)에 맞춰 조정 폭을 {k * 100:.0f}%로 줄임"
                status = "proposed"
        sid = app.db.x("INSERT INTO strategies(created_at,trigger,status,tier,base,before,targets,result,data,note) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (now, trigger, status, st["tier"] if st else None, json.dumps(base),
                        json.dumps(st["targets"] if st else {}), json.dumps(targets) if targets else None,
                        json.dumps(res, ensure_ascii=False), json.dumps(data, ensure_ascii=False, default=str), note))
        text = self._message(sid, res, st, targets, status, note, data)
        if status == "proposed" and mode == "propose":
            await app.pending.create("ai_strategy", {"id": sid}, text + "\n실행하면 목표 비중을 바꾸고 바로 조정 주문을 냅니다.",
                                     [("실행", "approve"), ("이번엔 건너뛰기", "cancel")], ttl_min=180)
        elif status == "proposed" and mode == "auto":
            await app.notifier.send(text, category="strategy")
            out = await self.execute(sid)
            return {"id": sid, "status": "executed", "message": out}
        else:
            await app.notifier.send(text, category="strategy")
        return {"id": sid, "status": status, "message": text}

    def _message(self, sid, res, st, targets, status, note, data) -> str:
        u = self.app.universe
        lines = [f"[AI 투자 점검 #{sid} · {timeutil.now().strftime('%m/%d %H:%M')}] 확신도 {res.get('confidence', '-')}",
                 res.get("summary", "").strip()]
        for cv in res.get("country_views", [])[:6]:
            lines.append(f"· {cv['country']} {cv['view']}: {cv['reason']}")
        fx = data.get("fx_krw", {}).get("USD", {})
        if fx:
            chg = fx.get("chg_30d_pct")
            lines.append(f"환율 USD {fx['krw_per_unit']:,.1f}원" + (f" (30일 {chg:+.1f}%)" if chg is not None else ""))
        if status == "proposed" and st:
            lines.append("목표 비중 변경 제안:")
            reasons = res.get("reasons") or {}
            for s in sorted(set(targets) | set(st["targets"]), key=lambda x: -targets.get(x, 0)):
                a, b = st["targets"].get(s, 0), targets.get(s, 0)
                if abs(a - b) >= 0.5:
                    lines.append(f" - {u.name(s)} {a:g}% → {b:g}%" + (f" · {reasons[s]}" if reasons.get(s) else ""))
        for r in res.get("risks", [])[:3]:
            lines.append(f"⚠ {r}")
        if note:
            lines.append(f"※ {note}")
        if data.get("errors"):
            lines.append("데이터 일부 없음: " + "; ".join(data["errors"]))
        return "\n".join(x for x in lines if x)

    # ── 실행 ──
    async def execute(self, sid: int) -> str:
        app = self.app
        row = app.db.one("SELECT * FROM strategies WHERE id=?", (sid,))
        st = app.autoinvest.state()
        if not row or row["status"] not in ("proposed", "approved"):
            return "이미 처리했거나 없는 전략입니다."
        if not st or st["status"] != "active" or app.control.halted():
            app.db.x("UPDATE strategies SET status='skipped', note=? WHERE id=?", ("실행 시점에 자동 투자 운용 중이 아님", sid))
            return "자동 투자가 운용 중이 아니거나 전체 중지 상태라 실행하지 않았습니다."
        eng = app.settings.engine
        if not timeutil.in_session(timeutil.now(), app.settings.holidays, eng["session_start"], eng["session_end"]):
            app.db.x("UPDATE strategies SET status='approved' WHERE id=?", (sid,))
            return "장 시간이 아니라 다음 장 시작 뒤에 실행합니다."
        targets = json.loads(row["targets"])
        app.db.x("UPDATE autoinvest SET targets=?, updated_at=? WHERE id=1",
                 (json.dumps(targets), timeutil.now().isoformat()))
        val = await app.autoinvest.valuation()
        plan = []
        for r in val["rows"]:
            if abs(r["dev"]) < app.autoinvest.cfg["min_trade_dev_pp"] and r["target"] > 0:
                continue
            final = math.floor(val["total"] * r["target"] / 100 / r["price"])
            if final != r["qty"]:
                plan.append((r, final - r["qty"], final))
        res = json.loads(row["result"])
        reasons = res.get("reasons") or {}
        lines = await app.autoinvest._execute(
            plan, "ai_strategy", str(sid),
            lambda r, d, f: f"AI 전략 #{sid} · {r['name']} 목표 {r['target']:g}% (현재 {r['weight']:.1f}%)"
                            + (f" · {reasons[r['symbol']]}" if reasons.get(r["symbol"]) else ""))
        app.db.x("UPDATE strategies SET status='executed', executed_at=?, orders=? WHERE id=?",
                 (timeutil.now().isoformat(timespec="seconds"), json.dumps(lines, ensure_ascii=False), sid))
        msg = f"[AI 전략 #{sid} 실행] 목표 비중 변경 · 주문 {len(lines)}건\n" + ("\n".join(lines) or "조정할 주문 없음")
        await app.notifier.send(msg, category="strategy")
        return msg

    async def run_approved(self) -> None:
        for r in self.app.db.q("SELECT id FROM strategies WHERE status='approved' ORDER BY id"):
            await self.execute(r["id"])

    async def _on_card(self, action: str, payload: dict, pid: str) -> str:
        sid = payload["id"]
        if action != "approve":
            self.app.db.x("UPDATE strategies SET status='declined' WHERE id=? AND status='proposed'", (sid,))
            return "이번 AI 전략은 실행하지 않았습니다. 목표 비중은 그대로입니다."
        return await self.execute(sid)

    async def reset_targets(self) -> str:
        """AI 조정을 없애고 위험 단계 기본 비중으로 되돌린다 (다음 리밸런싱부터 반영)."""
        st = self.app.autoinvest.state()
        if not st:
            return "자동 투자가 없습니다."
        base = self.app.universe.tier(st["tier"])["targets"]
        self.app.db.x("UPDATE autoinvest SET targets=?, updated_at=? WHERE id=1",
                      (json.dumps(base), timeutil.now().isoformat()))
        return f"목표 비중을 {st['tier']}단계 기본값으로 되돌렸습니다. 다음 리밸런싱 점검 때 맞춰 조정합니다."

    def due(self, now) -> bool:
        """오늘이 점검 주기 날인지 (매일 / 주 첫 거래일 / 월 첫 거래일)."""
        hol = self.app.settings.holidays
        if not timeutil.is_trading_day(now.date(), hol):
            return False
        cad = self.cfg.get("cadence", "weekly")
        if cad == "daily":
            return True
        if cad == "monthly":
            return now.date() == timeutil.first_trading_day_of_month(now.date(), hol)
        from datetime import timedelta
        d = now.date() - timedelta(days=1)
        while not timeutil.is_trading_day(d, hol):
            d -= timedelta(days=1)
        return d.isocalendar()[:2] != now.date().isocalendar()[:2]          # 이번 주 첫 거래일
