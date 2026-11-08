"""시뮬레이터 브로커로 기획안의 시나리오를 처음부터 끝까지 검증 (LLM·네트워크 불필요)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from jangjung import timeutil
from jangjung.app import App
from jangjung.audit import audit
from jangjung.config import load_settings

MON = datetime(2026, 11, 9, 10, 0, tzinfo=timeutil.KST)   # 월요일 장중


@pytest.fixture
def app(tmp_path):
    timeutil.set_clock(MON)
    s = load_settings(tmp_path)
    s.broker = "sim"
    s.engine["fill_wait_seconds"] = 0
    a = App(s)
    yield a
    timeutil.set_clock(None)


def set_price(app, sym, price=None, halted=False):
    p = app.settings.data_dir / "sim_overrides.json"
    ov = json.loads(p.read_text()) if p.exists() else {}
    ov[sym] = {"price": price, "halted": True} if halted else price
    p.write_text(json.dumps(ov))


def advance(minutes):
    timeutil.set_clock(timeutil.now() + timedelta(minutes=minutes))


def last_pending(app, kind):
    rows = app.pending.open_items(kind)
    assert rows, f"{kind} 카드 없음"
    return rows[-1]["id"]


HYNIX_RULES = [
    {"symbol": "000660", "side": "sell", "condition": "return_at_or_above", "threshold": 15,
     "qty_mode": "fraction", "qty_value": 0.5},
    {"symbol": "000660", "side": "sell", "condition": "return_at_or_below", "threshold": -7,
     "qty_mode": "fraction", "qty_value": 1.0},
]


async def test_rule_take_profit_then_stop_loss(app):
    set_price(app, "000660", 200000)
    res = await app.rules.propose("SK하이닉스 수익률 15% 넘으면 절반 팔고, 산 가격보다 7% 떨어지면 전부 팔아 줘", HYNIX_RULES)
    assert res["ok"], res
    # 승인 전에는 규칙이 없다
    assert app.rules.active_count() == 0
    await app.pending.resolve(res["pending_id"], "approve")
    assert app.rules.active_count() == 2

    # 평단 190,000 → 219,000 (+15.3%) : 절반(10주) 매도
    advance(5); set_price(app, "000660", 219000)
    await app.engine.tick()
    o = app.db.q("SELECT * FROM orders")
    assert len(o) == 1 and o[0]["side"] == "sell" and o[0]["qty"] == 10 and o[0]["status"] == "filled"
    assert "규칙 #1" in o[0]["reason"]

    # 같은 조건이 계속돼도 다시 주문하지 않음 (1회 규칙)
    advance(1); await app.engine.tick()
    assert len(app.db.q("SELECT * FROM orders")) == 1

    # 176,000 (-7.4%) : 남은 10주 전량 매도, 묶음 종료
    advance(10); set_price(app, "000660", 176000)
    await app.engine.tick()
    o = app.db.q("SELECT * FROM orders ORDER BY id")
    assert len(o) == 2 and o[1]["qty"] == 10
    assert app.rules.active_count() == 0
    rep = audit(app.db, app.universe)
    assert rep["violations"] == [] and rep["over_60s"] == 0
    assert (app.settings.data_dir / "journal_orders.csv").exists()


async def test_stop_word_halts_everything(app):
    set_price(app, "000660", 200000)
    res = await app.rules.propose("x", HYNIX_RULES[:1])
    await app.pending.resolve(res["pending_id"], "approve")
    reply = await app.handle_text("멈춰!")
    assert "전체 중지" in reply and app.control.halted()
    advance(5); set_price(app, "000660", 222000)
    await app.engine.tick()
    assert app.db.q("SELECT * FROM orders") == []
    # 재개는 카드 확인을 거쳐야 함
    await app.handle_text("/resume")
    await app.pending.resolve(last_pending(app, "resume_all"), "approve")
    assert not app.control.halted()
    await app.engine.tick()
    assert len(app.db.q("SELECT * FROM orders")) == 1


async def test_price_anomaly_suspends_symbol(app):
    set_price(app, "000660", 200000)
    res = await app.rules.propose("x", HYNIX_RULES[:1])
    await app.pending.resolve(res["pending_id"], "approve")
    advance(5); set_price(app, "000660", 240000)   # 전일(200,000) 대비 +20% 급등
    await app.engine.tick()
    assert app.db.q("SELECT * FROM orders") == []
    assert "000660" in app.control.suspended()
    last_pending(app, "symbol_resume")


async def test_halted_stock_not_ordered(app):
    set_price(app, "000660", 200000)
    res = await app.rules.propose("x", HYNIX_RULES[:1])
    await app.pending.resolve(res["pending_id"], "approve")
    advance(5); set_price(app, "000660", halted=True)
    await app.engine.tick()
    assert app.db.q("SELECT * FROM orders") == []


async def test_over_limit_rule_needs_extra_confirmation(app):
    set_price(app, "005930", 70000)
    res = await app.rules.propose("삼성전자 7만원 이하면 500만원어치", [
        {"symbol": "005930", "side": "buy", "condition": "price_at_or_below", "threshold": 70000,
         "qty_mode": "amount", "qty_value": 5_000_000}])
    assert res["ok"] and res["over_limit"]
    await app.pending.resolve(res["pending_id"], "approve_over")
    advance(5); await app.engine.tick()
    # 한도 초과 → 주문하지 않고 확인 카드, 규칙은 확인 대기
    assert app.db.q("SELECT * FROM orders") == []
    pid = last_pending(app, "order_over_limit")
    await app.pending.resolve(pid, "approve")
    o = app.db.q("SELECT * FROM orders")
    assert len(o) == 1 and o[0]["side"] == "buy"
    assert audit(app.db, app.universe)["violations"] == []


async def test_validation_errors(app):
    res = await app.rules.propose("x", [{"symbol": "999999", "side": "buy", "condition": "price_at_or_below",
                                         "threshold": 1000, "qty_mode": "amount", "qty_value": 100000}])
    assert not res["ok"]
    res = await app.rules.propose("x", [{"symbol": "005930", "side": "buy", "condition": "return_at_or_below",
                                         "threshold": -5, "qty_mode": "amount", "qty_value": 100000}])
    assert not res["ok"]  # 보유하지 않은 종목의 수익률 조건


def test_search_and_blocklist(app):
    assert len(app.universe.search("삼성")) > 1           # 모호 → 되묻기
    assert app.universe.search("하이닉스")[0]["symbol"] == "000660"
    app.universe.master["122630"] = {"name": "KODEX 레버리지", "kind": "ETF", "market": "KOSPI"}
    assert app.universe.blocked_reason("122630")


async def test_autoinvest_full_cycle(app):
    for s, p in {"069500": 40000, "360750": 22000, "114260": 64000, "279530": 15000}.items():
        set_price(app, s, p)
    r = await app.autoinvest.propose(10_000_000, 3)
    assert not r["ok"]                                    # 성향 질문 먼저
    app.autoinvest.save_profile([3, 3, 3, 3, 3, 3])
    r = await app.autoinvest.propose(10_000_000, 5)
    assert r.get("needs_user_reconfirm")                  # 추천 3 vs 선택 5 → 한 번 더 확인
    r = await app.autoinvest.propose(10_000_000, 3)
    assert r["ok"]
    await app.pending.resolve(r["pending_id"], "approve")
    st = app.autoinvest.state()
    assert st["status"] == "building" and st["tranches_done"] == 1

    for day in (1, 2):                                    # 화·수 09:31 분할 매수 2·3회차
        timeutil.set_clock(datetime(2026, 11, 9 + day, 9, 31, tzinfo=timeutil.KST))
        await app.engine.step()
    st = app.autoinvest.state()
    assert st["status"] == "active" and st["tranches_done"] == 3
    v = await app.autoinvest.valuation()
    assert all(abs(x["dev"]) <= 5 for x in v["rows"]), v["rows"]

    # 채권 ETF 비중을 인위적으로 31%로 → 12/1(첫 거래일) 10시 리밸런싱
    bond = next(x for x in v["rows"] if x["symbol"] == "114260")
    extra = int(v["total"] * 0.08 / 64000)
    app.db.x("UPDATE positions SET qty=qty+? WHERE bucket='auto' AND symbol='114260'", (extra,))
    timeutil.set_clock(datetime(2026, 12, 1, 10, 1, tzinfo=timeutil.KST))
    await app.engine.step()
    sells = app.db.q("SELECT * FROM orders WHERE source='rebalance' AND symbol='114260' AND side='sell'")
    assert sells, "채권 초과분 매도 없음"
    v = await app.autoinvest.valuation()
    assert all(abs(x["dev"]) <= 5 for x in v["rows"]), v["rows"]
    assert audit(app.db, app.universe)["violations"] == []


async def test_loss_limit_halts_autoinvest(app):
    for s, p in {"069500": 40000, "360750": 22000, "114260": 64000, "279530": 15000}.items():
        set_price(app, s, p)
    app.autoinvest.save_profile([3] * 6)
    r = await app.autoinvest.propose(10_000_000, 3)
    await app.pending.resolve(r["pending_id"], "approve")
    app.db.x("UPDATE autoinvest SET cash=cash-1500000 WHERE id=1")   # 평가 손실 -15% 상황 연출
    await app.engine.tick()
    assert app.autoinvest.state()["status"] == "halted_loss"
    pid = last_pending(app, "loss_resume")
    n = len(app.db.q("SELECT * FROM orders"))
    timeutil.set_clock(datetime(2026, 11, 10, 9, 31, tzinfo=timeutil.KST))
    await app.engine.step()                                          # 중단 중엔 분할매수도 안 함
    assert len(app.db.q("SELECT * FROM orders")) == n
    await app.pending.resolve(pid, "resume")
    assert app.autoinvest.state()["status"] == "building"


async def test_api_errors_trigger_global_halt(app):
    set_price(app, "000660", 200000)
    res = await app.rules.propose("x", HYNIX_RULES[:1])
    await app.pending.resolve(res["pending_id"], "approve")
    app.broker.fail_next = 10
    for _ in range(3):
        advance(1); await app.engine.tick()
    assert app.control.halted() and "API" in app.control.halted()["reason"]


async def test_daily_summary(app):
    timeutil.set_clock(datetime(2026, 11, 9, 15, 41, tzinfo=timeutil.KST))
    await app.engine.step()
    assert any("장 마감 요약" in t for t, _ in app.notifier.inner.sent)
    assert (app.settings.data_dir / "journal_daily.csv").exists()


def test_agent_options_build(app):
    opts = app.agent.options()
    assert opts.tools == [] and all(t.startswith("mcp__jj__") for t in opts.allowed_tools)
    names = {t.name for t in app.agent.tools}
    assert not any("order" in n for n in names)          # LLM에게 주문 도구 없음


# ── 폭락일 손절: 시세 이상으로 멈춘 종목도 손절은 '그래도 팔까요?'로 묻는다 ──
async def _crash_setup(app):
    set_price(app, "000660", 200000)                       # 평단 190,000 · 20주
    res = await app.rules.propose("7% 떨어지면 전부 팔아 줘", HYNIX_RULES[1:])
    await app.pending.resolve(res["pending_id"], "approve")
    advance(5); set_price(app, "000660", 168000)           # 전일 대비 -16% (급락) · 수익률 -11.6%
    await app.engine.tick()


async def test_crash_stop_loss_asks_then_sells(app):
    await _crash_setup(app)
    assert app.db.q("SELECT * FROM orders") == []          # 자동으로 팔지 않음
    assert "000660" in app.control.suspended()
    last_pending(app, "symbol_resume")
    pid = last_pending(app, "stoploss_anomaly")
    assert app.db.one("SELECT status FROM rules")["status"] == "waiting_confirm"
    advance(1); await app.engine.tick()                    # 같은 카드를 또 보내지 않음
    assert len(app.pending.open_items("stoploss_anomaly")) == 1
    msg = await app.pending.resolve(pid, "sell")
    assert "체결" in msg, msg
    o = app.db.q("SELECT * FROM orders")
    assert len(o) == 1 and o[0]["side"] == "sell" and o[0]["qty"] == 20
    assert "시세 이상 중 사용자 확인" in o[0]["reason"] and "anomaly_override" in o[0]["evidence"]
    assert "000660" in app.control.suspended()             # 매수 등 다른 주문은 계속 멈춤
    assert app.rules.active_count() == 0
    assert audit(app.db, app.universe)["violations"] == []


async def test_crash_stop_loss_hold_expire_and_halted(app):
    await _crash_setup(app)
    await app.pending.resolve(last_pending(app, "stoploss_anomaly"), "hold")
    assert app.db.q("SELECT * FROM orders") == []
    assert app.db.one("SELECT status FROM rules")["status"] == "active"
    advance(1); await app.engine.tick()
    assert app.pending.open_items("stoploss_anomaly") == []      # 오늘은 다시 묻지 않음
    # 다음 날: 종목은 여전히 멈춤 → 다시 묻는다. 답이 없으면 만료 후 다시 묻는다
    advance(24 * 60); await app.engine.tick()
    pid = last_pending(app, "stoploss_anomaly")
    advance(31); await app.pending.expire_old()
    assert app.db.q("SELECT status FROM pending WHERE id=?", (pid,))[0]["status"] == "expired"
    assert app.db.one("SELECT status FROM rules")["status"] == "active"
    await app.engine.tick()
    assert last_pending(app, "stoploss_anomaly") != pid
    assert app.db.q("SELECT * FROM orders") == []


async def test_crash_stop_loss_halted_stock_only_alerts(app):
    set_price(app, "000660", 200000)
    res = await app.rules.propose("x", HYNIX_RULES[1:])
    await app.pending.resolve(res["pending_id"], "approve")
    advance(5)
    (app.settings.data_dir / "sim_overrides.json").write_text(json.dumps({"000660": {"price": 168000, "halted": True}}))
    await app.engine.tick()
    assert app.db.q("SELECT * FROM orders") == [] and app.pending.open_items("stoploss_anomaly") == []
    feed = [m["text"] for m in app.db.q("SELECT text FROM messages")]
    assert any("거래정지 상태라 매도할 수 없습니다" in t for t in feed)


async def test_take_profit_not_asked_during_anomaly(app):
    set_price(app, "000660", 200000)
    res = await app.rules.propose("x", HYNIX_RULES[:1])
    await app.pending.resolve(res["pending_id"], "approve")
    advance(5); set_price(app, "000660", 240000)
    await app.engine.tick()
    assert app.pending.open_items("stoploss_anomaly") == [] and app.db.q("SELECT * FROM orders") == []
