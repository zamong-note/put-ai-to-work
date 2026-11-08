"""환율 자동 갱신 · AI 투자 점검/전략 실행 — LLM·네트워크는 가짜로 바꿔 끼워 검증."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from jangjung import macro, timeutil
from jangjung.app import App
from jangjung.audit import audit
from jangjung.config import load_settings
from jangjung.strategist import StrategyError

KST = timeutil.KST
BASE3 = {"069500": 40, "360750": 20, "114260": 25, "279530": 15}


@pytest.fixture
def app(tmp_path):
    timeutil.set_clock(datetime(2026, 11, 2, 9, 40, tzinfo=KST))
    s = load_settings(tmp_path)
    s.broker = "sim"
    s.engine["fill_wait_seconds"] = 0
    a = App(s, console=False)
    yield a
    timeutil.set_clock(None)


async def build_tier3(app):
    """11/2~11/4 분할 매수 3회 → 자동 투자 3단계 운용 중."""
    app.autoinvest.save_profile([3] * 6)
    res = await app.autoinvest.propose(10_000_000, 3)
    await app.pending.resolve(res["pending_id"], "approve")
    for day in (3, 4):
        timeutil.set_clock(datetime(2026, 11, day, 9, 40, tzinfo=KST))
        await app.autoinvest.build_step()
    assert app.autoinvest.state()["status"] == "active"


def fake_llm(targets, calls=None, **extra):
    async def llm(app, payload):
        if calls is not None:
            calls.append(payload)
        return {"summary": "원화 약세가 이어져 해외 지수 비중을 유지하고 국내 비중을 조금 늘립니다.",
                "country_views": [{"country": "미국", "view": "중립", "reason": "물가 둔화"}],
                "risks": ["환율 변동"], "targets": targets, "confidence": "보통",
                "reasons": {"069500": "국내 지수 20일 +3%"}, **extra}
    return llm


# ── 환율 ──
def er_api(usd=1385.5, jpy=9.21, cny=190.2, hkd=177.3, vnd=0.0545):
    return {"result": "success", "time_last_update_utc": "Mon, 09 Nov 2026 00:00:01 +0000",
            "rates": {"KRW": 1, "USD": 1 / usd, "JPY": 1 / jpy, "CNY": 1 / cny, "HKD": 1 / hkd, "VND": 1 / vnd}}


async def test_fx_update_and_guard(app, monkeypatch):
    async def get(url, params=None):
        return er_api()
    monkeypatch.setattr(macro, "_get_json", get)
    res = await macro.update_fx(app)
    ov = app.settings.overseas
    assert ov["fx_usd"] == 1385.5 and ov["fx_jpy100"] == 921.0 and ov["fx_cny"] == 190.2 and ov["fx_vnd100"] == 5.45
    assert res["info"]["source"] == "open.er-api.com" and not res["skipped"]
    local = json.loads((app.settings.data_dir / "settings.local.json").read_text(encoding="utf-8"))
    assert local["overseas"]["fx_usd"] == 1385.5                                  # 다시 시작해도 유지
    # 하루에 15% 넘게 튀면 반영하지 않음
    async def jump(url, params=None):
        return er_api(usd=1700)
    monkeypatch.setattr(macro, "_get_json", jump)
    res = await macro.update_fx(app)
    assert ov["fx_usd"] == 1385.5 and "USD" in res["skipped"][0]
    # 첫 번째 API가 죽으면 ECB(frankfurter)로
    async def fallback(url, params=None):
        if "er-api" in url:
            raise macro.MacroError("down")
        return {"date": "2026-11-09", "rates": {"USD": 1 / 1390, "JPY": 1 / 9.3, "CNY": 1 / 191, "HKD": 1 / 178}}
    monkeypatch.setattr(macro, "_get_json", fallback)
    res = await macro.update_fx(app)
    assert ov["fx_usd"] == 1390 and "frankfurter" in res["info"]["source"]


async def test_fx_daily_job_and_commands(app, monkeypatch):
    calls = []

    async def get(url, params=None):
        calls.append(url)
        return er_api()
    monkeypatch.setattr(macro, "_get_json", get)
    timeutil.set_clock(datetime(2026, 11, 9, 8, 55, tzinfo=KST))
    await app.engine.step()
    await app.engine.step()
    assert len(calls) == 1 and app.settings.overseas["fx_usd"] == 1385.5          # 하루 한 번
    assert "USD 1,386원" in await app.handle_text("/fx")
    app.settings.overseas["fx_auto"] = "off"
    timeutil.set_clock(datetime(2026, 11, 10, 8, 55, tzinfo=KST))
    await app.engine.step()
    assert len(calls) == 1


# ── AI 전략 ──
async def test_validate_bounds(app):
    v = app.strategist.validate
    assert v({"069500": 45, "360750": 20, "114260": 20, "279530": 15}, BASE3)["069500"] == 45
    with pytest.raises(StrategyError, match="허용 ±10"):
        v({"069500": 55, "360750": 10, "114260": 20, "279530": 15}, BASE3)
    with pytest.raises(StrategyError, match="허용되지 않은 종목"):
        v({"999999": 5, "069500": 35, "360750": 20, "114260": 25, "279530": 15}, BASE3)
    with pytest.raises(StrategyError, match="합계"):
        v({"069500": 40, "360750": 20, "114260": 20, "279530": 10}, BASE3)
    out = v({"069500": 40.04, "360750": 19.98, "114260": 25, "279530": 14.99}, BASE3)
    assert sum(out.values()) == pytest.approx(100)


async def test_propose_then_execute(app):
    await build_tier3(app)
    timeutil.set_clock(datetime(2026, 11, 9, 10, 30, tzinfo=KST))
    calls = []
    app.strategist.llm = fake_llm({"069500": 48, "360750": 20, "114260": 17, "279530": 15}, calls)
    app.settings.ai_strategy["max_turnover_pct"] = 50
    msg = await app.engine.ai_review("test")
    assert "AI 투자 점검" in msg and "KODEX 200 40% → 48%" in msg
    p = calls[0]
    assert p["base_targets"] == BASE3 and p["portfolio"]["tier"] == 3 and "fx_krw" in p["data"]
    pid = app.pending.open_items("ai_strategy")[0]["id"]
    n0 = len(app.db.q("SELECT * FROM orders"))
    out = await app.pending.resolve(pid, "approve")
    assert "AI 전략" in out and "실행" in out
    assert app.autoinvest.state()["targets"]["069500"] == 48
    orders = app.db.q("SELECT * FROM orders WHERE source='ai_strategy'")
    assert orders and len(app.db.q("SELECT * FROM orders")) > n0
    assert any(o["symbol"] == "069500" and o["side"] == "buy" for o in orders)
    assert any(o["symbol"] == "114260" and o["side"] == "sell" for o in orders)
    assert all("AI 전략 #" in o["reason"] for o in orders)
    assert audit(app.db, app.universe)["violations"] == []
    assert app.strategist.latest()["status"] == "executed"
    # 기본 비중으로 되돌리기
    assert "기본값" in await app.handle_text("/strategy reset")
    assert app.autoinvest.state()["targets"] == BASE3


async def test_auto_mode_caps_turnover(app):
    await build_tier3(app)
    timeutil.set_clock(datetime(2026, 11, 9, 10, 30, tzinfo=KST))
    app.settings.ai_strategy.update(mode="auto", max_turnover_pct=5)
    app.strategist.llm = fake_llm({"069500": 50, "360750": 20, "114260": 15, "279530": 15})
    await app.engine.ai_review("test")
    s = app.strategist.latest()
    assert s["status"] == "executed" and "줄임" in s["note"]
    t = app.autoinvest.state()["targets"]
    assert 40 < t["069500"] < 46                                                     # 10%p → 5% 한도로 축소
    assert app.pending.open_items("ai_strategy") == []                              # 자동: 카드 없이 실행


async def test_out_of_range_and_off_and_no_autoinvest(app):
    app.strategist.llm = fake_llm({"069500": 80, "360750": 10, "114260": 5, "279530": 5})
    res = await app.strategist.review("test")                                      # 자동 투자 없음 → 점검만
    assert res["status"] == "analysis"
    await build_tier3(app)
    timeutil.set_clock(datetime(2026, 11, 9, 10, 30, tzinfo=KST))
    res = await app.strategist.review("test")
    assert res["status"] == "rejected" and "반영하지 않음" in res["message"]
    assert app.autoinvest.state()["targets"] == BASE3
    assert app.db.q("SELECT * FROM orders WHERE source='ai_strategy'") == []
    # 손실 한도·전체 중지면 실행하지 않음
    app.strategist.llm = fake_llm({"069500": 45, "360750": 20, "114260": 20, "279530": 15})
    await app.emergency_stop()
    res = await app.strategist.review("test")
    assert res["status"] == "analysis" and app.pending.open_items("ai_strategy") == []


async def test_weekly_schedule(app):
    await build_tier3(app)
    calls = []
    app.strategist.llm = fake_llm(BASE3, calls)
    for day, hm in ((9, (10, 0)), (9, (10, 31)), (9, (11, 0)), (10, (10, 31))):  # 월 10:00, 10:31, 11:00, 화 10:31
        timeutil.set_clock(datetime(2026, 11, day, *hm, tzinfo=KST))
        await app.engine.step()
    assert len(calls) == 1                                                          # 주 첫 거래일 10:30 이후 한 번
    assert app.strategist.latest()["status"] == "no_change"
    app.settings.ai_strategy["mode"] = "off"
    timeutil.set_clock(datetime(2026, 11, 16, 10, 31, tzinfo=KST))
    await app.engine.step()
    assert len(calls) == 1
    assert "AI 투자 점검 #" in await app.handle_text("/strategy")


async def test_report_and_web_endpoints(app):
    from starlette.testclient import TestClient
    from jangjung import pdfreport
    from jangjung.web import Dashboard
    await build_tier3(app)
    timeutil.set_clock(datetime(2026, 11, 9, 10, 30, tzinfo=KST))
    app.strategist.llm = fake_llm({"069500": 45, "360750": 20, "114260": 20, "279530": 15})
    app.settings.ai_strategy.update(mode="auto", max_turnover_pct=50)
    await app.engine.ai_review("test")
    rep = await pdfreport.build_report(app, "weekly", "2026-11-09")
    assert 3 <= rep.pages <= 5 and any("AI 투자 점검 1회" in x for x in rep.summary)
    dash = Dashboard(app, 8765)
    hdr = {"X-JJ-Token": dash.token}
    with TestClient(dash.asgi(), base_url="http://127.0.0.1:8765") as c:
        d = c.get("/api/strategy").json()
        assert d["latest"]["status"] == "executed" and d["base"]["069500"] == 40 and d["current"]["069500"] == 45
        assert set(d["fx"]) == {"USD", "JPY", "CNY", "HKD", "VND"}
        assert c.post("/api/fx/update", headers=hdr).status_code == 502          # 오프라인 → 실패 알림, 값 유지
        assert c.post("/api/strategy/run", headers=hdr).status_code == 200
        assert c.post("/api/strategy/reset", headers=hdr).json()["ok"]
        assert c.post("/api/strategy/run").status_code == 403                     # 토큰 없음
        bad = c.post("/api/settings/update", headers=hdr, json={"section": "ai_strategy", "values": {"run_time": "16:00"}})
        assert bad.status_code == 400
        assert c.post("/api/settings/update", headers=hdr,
                      json={"section": "ai_strategy", "values": {"mode": "off", "max_tilt_pp": 5}}).status_code == 200
        assert app.settings.ai_strategy["mode"] == "off" and app.settings.ai_strategy["max_tilt_pp"] == 5
