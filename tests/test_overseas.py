"""해외 투자 설정(꺼짐 / 주요 3개국 / 전체 / 직접 선택)과 해외 규칙 매매 — 시뮬레이터, 네트워크 없음."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from jangjung import markets, timeutil
from jangjung.app import App
from jangjung.audit import audit
from jangjung.config import load_settings
from jangjung.settings_store import SettingsError

KST = timeutil.KST
MON_KR = datetime(2026, 11, 9, 10, 0, tzinfo=KST)      # 한국 장중 · 미국 일요일 밤
MON_US = datetime(2026, 11, 9, 23, 40, tzinfo=KST)     # 뉴욕 월요일 09:40


@pytest.fixture
def app(tmp_path):
    timeutil.set_clock(MON_KR)
    s = load_settings(tmp_path)
    s.broker = "sim"
    s.engine["fill_wait_seconds"] = 0
    a = App(s, console=False)
    yield a
    timeutil.set_clock(None)


def set_price(app, sym, price):
    p = app.settings.data_dir / "sim_overrides.json"
    ov = json.loads(p.read_text()) if p.exists() else {}
    ov[sym] = price
    p.write_text(json.dumps(ov))


def enable(app, scope="major3", countries=None):
    vals = {"enabled": "on", "scope": scope}
    if countries is not None:
        vals["countries"] = countries
    app.store.update("overseas", vals)


def test_scope_choices():
    assert markets.allowed_countries({"enabled": "off", "scope": "all"}) == set()
    assert markets.allowed_countries({"enabled": "on", "scope": "major3"}) == {"US", "JP", "CN"}
    assert markets.allowed_countries({"enabled": "on", "scope": "all"}) == {"US", "JP", "CN", "HK", "VN"}
    assert markets.allowed_countries({"enabled": "on", "scope": "custom", "countries": ["HK", "VN"]}) == {"HK", "VN"}
    assert "미국·일본·중국" in markets.scope_text({"enabled": "on", "scope": "major3"})


def test_settings_validation_and_persist(app):
    with pytest.raises(SettingsError):
        app.store.update("overseas", {"enabled": "on", "scope": "custom", "countries": []})
    with pytest.raises(SettingsError):
        app.store.update("overseas", {"scope": "europe"})
    app.store.update("overseas", {"enabled": "on", "scope": "custom", "countries": ["US", "XX", "VN"]})
    assert app.settings.overseas["countries"] == ["US", "VN"]               # 지원하지 않는 나라는 버림
    s2 = load_settings(app.settings.data_dir)
    assert s2.overseas["enabled"] == "on" and s2.overseas["countries"] == ["US", "VN"]


def test_search_and_permissions(app):
    u = app.universe
    r = u.search("애플")
    assert r[0]["symbol"] == "NASD:AAPL" and "꺼져" in r[0]["blocked"]       # 기본: 해외 꺼짐
    enable(app, "major3")
    assert u.search("애플")[0]["blocked"] is None
    assert u.search("AAPL")[0]["symbol"] == "NASD:AAPL" and u.search("Apple")[0]["symbol"] == "NASD:AAPL"
    assert u.blocked_reason("TKSE:7203") is None and u.blocked_reason("SHAA:600519") is None
    assert "홍콩" in u.blocked_reason("SEHK:00700") and "베트남" in u.blocked_reason("VNSE:VNM")
    assert "ULTRA" in u.blocked_reason("NASD:TQQQ").upper()                # 해외 레버리지도 차단
    assert u.search("삼성전자")[0]["symbol"] == "005930"                     # 국내 검색은 그대로
    enable(app, "custom", ["HK"])
    assert u.blocked_reason("SEHK:00700") is None and "미국" in u.blocked_reason("NASD:AAPL")
    enable(app, "all")
    assert u.blocked_reason("VNSE:VNM") is None


def test_market_hours():
    hol = {"US": ["2026-11-26"]}
    assert markets.market_open("NASD", MON_US, hol)
    assert not markets.market_open("NASD", MON_KR, hol)                        # 뉴욕 일요일 밤
    assert markets.market_open("TKSE", MON_KR, hol)                            # 도쿄 10:00
    assert not markets.market_open("TKSE", datetime(2026, 11, 9, 12, 0, tzinfo=KST), hol)   # 도쿄 점심
    assert not markets.market_open("NASD", datetime(2026, 11, 27, 0, 30, tzinfo=KST), hol)  # 추수감사절
    summer = datetime(2026, 7, 6, 22, 40, tzinfo=KST)                         # 서머타임: 뉴욕 09:40
    assert markets.market_open("NASD", summer, hol)


async def test_overseas_rule_runs_only_in_us_session(app):
    enable(app, "major3")
    set_price(app, "NASD:AAPL", 230)
    res = await app.rules.propose("애플 220달러 이하면 100만원어치", [
        {"symbol": "nasd:aapl", "side": "buy", "condition": "price_at_or_below", "threshold": 220,
         "qty_mode": "amount", "qty_value": 1_000_000}])
    assert res["ok"], res
    card = app.db.q("SELECT text FROM messages ORDER BY id DESC LIMIT 1")[0]["text"]
    assert "220.00 USD" in card and "해외(미국)" in card
    await app.pending.resolve(res["pending_id"], "approve")
    set_price(app, "NASD:AAPL", 215)
    await app.engine.step()                                                  # 한국 10:00 → 미국장 닫힘
    assert app.db.q("SELECT * FROM orders") == []
    cash0 = (await app.broker.get_account()).cash
    timeutil.set_clock(MON_US)
    await app.engine.step()                                                  # 뉴욕 09:40
    o = app.db.q("SELECT * FROM orders")
    assert len(o) == 1, o
    o = o[0]
    assert o["symbol"] == "NASD:AAPL" and o["qty"] == 3                     # 1,000,000 / (215 × 1,400) = 3주
    assert o["order_type"] == "limit" and o["order_price"] == pytest.approx(217.15)   # 215 × 1.01, 0.01 단위 올림
    assert o["currency"] == "USD" and o["fx"] == 1400 and o["status"] == "filled"
    assert cash0 - (await app.broker.get_account()).cash == pytest.approx(215 * 3 * 1400)
    alert = [m["text"] for m in app.db.q("SELECT text FROM messages") if m["text"].startswith("[체결]")][0]
    assert "215.00 USD" in alert
    assert audit(app.db, app.universe)["violations"] == []
    # 나중에 해외 투자를 꺼도 과거 주문은 감사 위반이 아님
    app.store.update("overseas", {"enabled": "off"})
    assert audit(app.db, app.universe)["violations"] == []


async def test_overseas_limits_are_in_krw_and_off_blocks(app):
    set_price(app, "NASD:AAPL", 230)
    res = await app.rules.propose("x", [{"symbol": "NASD:AAPL", "side": "buy", "condition": "price_at_or_below",
                                         "threshold": 220, "qty_mode": "shares", "qty_value": 1}])
    assert not res["ok"] and "꺼져" in res["errors"][0]                        # 해외 꺼짐 → 등록 불가
    enable(app, "major3")
    res = await app.rules.propose("x", [{"symbol": "NASD:AAPL", "side": "buy", "condition": "price_at_or_below",
                                         "threshold": 220, "qty_mode": "shares", "qty_value": 12}])
    assert res["ok"] and res["over_limit"]                                   # 12 × 220 × 1,400 = 369만원 > 300만원
    res = await app.rules.propose("x", [{"symbol": "SEHK:00700", "side": "buy", "condition": "price_at_or_below",
                                         "threshold": 380, "qty_mode": "shares", "qty_value": 1}])
    assert not res["ok"] and "홍콩" in res["errors"][0]
    # 규칙 등록 뒤 해외를 끄면 미국장이 열려도 주문하지 않음
    res = await app.rules.propose("x", [{"symbol": "NASD:AAPL", "side": "buy", "condition": "price_at_or_below",
                                         "threshold": 220, "qty_mode": "shares", "qty_value": 1}])
    await app.pending.resolve(res["pending_id"], "approve")
    app.store.update("overseas", {"enabled": "off"})
    set_price(app, "NASD:AAPL", 210)
    timeutil.set_clock(MON_US)
    await app.engine.step()
    assert app.db.q("SELECT * FROM orders") == []


async def test_kis_overseas_order_tr(tmp_path):
    from jangjung.broker.kis import KISBroker
    calls = []

    async def fake_call(method, path, tr_id, params=None, body=None, **kw):
        calls.append((path, tr_id, body))
        return {"rt_cd": "0", "output": {"ODNO": "0001"}, "msg1": "ok"}, {}
    for real, buy, sell in ((False, "VTTT1002U", "VTTT1006U"), (True, "TTTT1002U", "TTTT1006U")):
        b = KISBroker("k" * 36, "s" * 180, "12345678", "01", tmp_path, real=real)
        b._call = fake_call
        await b.place_order("NASD:AAPL", "buy", 3, "limit", 217.15)
        await b.place_order("NASD:AAPL", "sell", 3, "limit", 212.85)
        await b.place_order("TKSE:7203", "buy", 100, "limit", 2830)
        assert [c[1] for c in calls[-3:]] == [buy, sell, ("T" if real else "V") + "TTS0308U"]
        body = calls[-3][2]
        assert body["OVRS_EXCG_CD"] == "NASD" and body["PDNO"] == "AAPL" and body["OVRS_ORD_UNPR"] == "217.15"
        assert body["ORD_DVSN"] == "00" and calls[-2][2]["SLL_TYPE"] == "00"
        assert calls[-1][0] == "/uapi/overseas-stock/v1/trading/order"
        await b.close()
