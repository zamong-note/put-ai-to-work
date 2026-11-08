"""웹 대시보드 API · 보안 검사 (LLM 호출 없음)."""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from starlette.testclient import TestClient

from jangjung import timeutil
from jangjung.app import App
from jangjung.config import load_settings
from jangjung.web import Dashboard

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def ctx(tmp_path):
    timeutil.set_clock(datetime(2026, 11, 9, 10, 0, tzinfo=timeutil.KST))
    s = load_settings(tmp_path)
    s.broker = "sim"
    s.engine["fill_wait_seconds"] = 0
    app = App(s, console=False)
    dash = Dashboard(app, 8765)
    with TestClient(dash.asgi(), base_url=BASE) as c:
        yield app, dash, c
    timeutil.set_clock(None)


def hdr(dash):
    return {"X-JJ-Token": dash.token}


def test_index_has_token(ctx):
    app, dash, c = ctx
    r = c.get("/")
    assert r.status_code == 200 and dash.token in r.text and "__JJ_TOKEN__" not in r.text


def test_security_guards(ctx):
    app, dash, c = ctx
    assert c.post("/api/stop").status_code == 403                                   # 토큰 없음
    assert c.post("/api/stop", headers={"X-JJ-Token": "x"}).status_code == 403      # 틀린 토큰
    assert c.post("/api/stop", headers={**hdr(dash), "Origin": "https://evil.example"}).status_code == 403
    assert c.get("/api/status", headers={"Host": "evil.example:8765"}).status_code == 403  # DNS 리바인딩
    assert not app.control.halted()
    assert c.post("/api/stop", headers=hdr(dash)).status_code == 200
    assert app.control.halted()


def test_card_approve_through_web(ctx):
    app, dash, c = ctx
    import asyncio
    rules = [{"symbol": "000660", "side": "sell", "condition": "return_at_or_above", "threshold": 15,
              "qty_mode": "fraction", "qty_value": 0.5}]
    c.portal.call(app.rules.propose, "테스트", rules)
    feed = c.get("/api/feed").json()["messages"]
    card = next(m for m in feed if m["buttons"])
    data = json.loads(card["buttons"])[0][0][1]
    assert data.endswith("|approve")
    r = c.post("/api/button", json={"data": data}, headers=hdr(dash))
    assert r.status_code == 200 and "등록" in r.json()["reply"]
    assert app.rules.active_count() == 1
    resolved = c.get("/api/feed").json()["resolved"]
    assert any(x["id"] == card["id"] for x in resolved)
    # 같은 버튼 두 번 → 한 번만 실행
    c.post("/api/button", json={"data": data}, headers=hdr(dash))
    assert app.rules.active_count() == 1
    assert len(c.get("/api/rules").json()["rules"]) == 1


def test_command_chat_and_reads(ctx):
    app, dash, c = ctx
    assert c.post("/api/chat", json={"text": "/status"}, headers=hdr(dash)).status_code == 200
    msgs = c.get("/api/feed").json()["messages"]
    assert [m["role"] for m in msgs[-2:]] == ["user", "bot"]
    for path in ("/api/status", "/api/portfolio", "/api/rules", "/api/orders", "/api/settings", "/api/reports"):
        assert c.get(path).status_code == 200, path
    st = c.get("/api/settings").json()
    assert st["broker"]["mode"] == "sim" and "token" not in json.dumps(st["broker"]).lower().replace("token_cached", "")


def test_autoinvest_and_reports(ctx):
    app, dash, c = ctx
    app.autoinvest.save_profile([3] * 6)
    res = c.portal.call(app.autoinvest.propose, 10_000_000, 3)
    c.post("/api/button", json={"data": f"pd|{res['pending_id']}|approve"}, headers=hdr(dash))
    p = c.get("/api/portfolio").json()
    assert p["auto"]["tier"] == 3 and len(p["auto"]["rows"]) == 4
    assert c.post("/api/run/summary", headers=hdr(dash)).status_code == 200
    day = "2026-11-09"
    csv = c.get(f"/api/report/daily?date={day}&format=csv")
    assert csv.status_code == 200 and "attachment" in csv.headers["content-disposition"]
    text = csv.content.decode("utf-8-sig")
    assert "[주문 내역]" in text and "자동투자 구성" in text and "규칙·전략과 다른 주문 0건" in text
    html = c.get(f"/api/report/daily?date={day}&format=html").text
    assert "일일보고서" in html and "KODEX 200" in html
    assert c.get("/api/report/daily?date=bad").status_code == 400
    assert c.get("/api/export/orders.csv").status_code == 200
    assert c.get("/api/export/daily.csv").status_code == 200


# ── 설정 편집 ──
def test_settings_edit_apply_and_reset(ctx):
    app, dash, c = ctx
    sch = c.get("/api/settings").json()["schema"]
    assert {f["key"] for f in sch["safety"]} >= {"max_buy_order_amount", "blocked_name_keywords"}
    r = c.post("/api/settings/update", headers=hdr(dash), json={"section": "safety", "values": {
        "max_buy_order_amount": 5_000_000, "blocked_name_keywords": ["곱버스"], "blocked_status_codes": []}})
    assert r.status_code == 200 and r.json()["changed"] == 3
    s = app.settings.safety
    assert s["max_buy_order_amount"] == 5_000_000                       # 즉시 적용 (같은 dict)
    assert app.gateway.limit_issues("buy", 4_000_000) == []
    assert {"레버리지", "인버스"} <= set(s["blocked_name_keywords"])      # 필수 키워드는 못 지움
    assert app.universe.blocked_keywords is s["blocked_name_keywords"]   # 다른 객체도 같은 리스트를 봄
    assert "58" in s["blocked_status_codes"]                             # 거래정지는 항상 차단
    local = json.loads((app.settings.data_dir / "settings.local.json").read_text(encoding="utf-8"))
    assert local["safety"]["max_buy_order_amount"] == 5_000_000
    assert any("설정 변경" in m["text"] for m in c.get("/api/feed").json()["messages"])
    # 범위를 깨는 값은 거부
    for bad in ({"section": "engine", "values": {"poll_seconds": 1}},
                {"section": "engine", "values": {"build_time": "16:00"}},
                {"section": "engine", "values": {"session_start": "15:00:00", "session_end": "10:00:00"}},
                {"section": "safety", "values": {"max_daily_orders": 500}},
                {"section": "autoinvest", "values": {"min_trade_dev_pp": 9, "rebalance_band_pp": 5}},
                {"section": "safety", "values": {"unknown": 1}},
                {"section": "holidays", "values": {"holidays": ["2026-13-01"]}}):
        assert c.post("/api/settings/update", headers=hdr(dash), json=bad).status_code == 400, bad
    assert app.settings.engine["poll_seconds"] == 20
    # 리밸런싱 방식, 휴장일
    c.post("/api/settings/update", headers=hdr(dash), json={"section": "autoinvest", "values": {"rebalance_mode": "confirm"}})
    assert app.autoinvest.rebalance_mode() == "confirm"
    c.post("/api/settings/update", headers=hdr(dash), json={"section": "holidays", "values": {"holidays": ["2026-11-10"]}})
    assert app.settings.holidays == {"2026-11-10"}
    # 되돌리기
    assert c.post("/api/settings/reset", headers=hdr(dash), json={"section": "safety"}).status_code == 200
    assert s["max_buy_order_amount"] == 3_000_000 and "2X" in s["blocked_name_keywords"]
    # 재시작해도 남은 변경(휴장일)은 다시 로드됨
    from jangjung.config import load_settings
    s2 = load_settings(app.settings.data_dir)
    assert s2.holidays == {"2026-11-10"} and s2.safety["max_buy_order_amount"] == 3_000_000


def test_env_edit_is_write_only(ctx, tmp_path, monkeypatch):
    app, dash, c = ctx
    import jangjung.settings_store as ss
    env = tmp_path / ".env"
    monkeypatch.setattr(ss, "ENV_PATH", env)
    for k in ("TELEGRAM_CHAT_ID", "KIS_APP_KEY", "CLAUDE_MODEL"):
        monkeypatch.delenv(k, raising=False)
    r = c.post("/api/env/update", headers=hdr(dash), json={"set": {
        "TELEGRAM_CHAT_ID": "123456789", "KIS_APP_KEY": "PSsecretKEY123", "CLAUDE_MODEL": "claude-sonnet-5-5"}})
    assert r.status_code == 200 and "KIS 앱키" in r.json()["restart"]
    assert app.settings.telegram_chat_id == "123456789" and app.settings.claude_model == "claude-sonnet-5-5"
    assert "PSsecretKEY123" in env.read_text()
    st = c.get("/api/settings")
    assert "PSsecretKEY123" not in st.text                                # 비밀값은 다시 내보내지 않음
    kis = next(e for e in st.json()["env"] if e["key"] == "KIS_APP_KEY")
    assert kis["is_set"] and kis["value"] is None
    assert "PSsecretKEY123" not in json.dumps(c.get("/api/feed").json())   # 알림에도 값은 안 남김
    assert c.post("/api/env/update", headers=hdr(dash), json={"set": {"TELEGRAM_CHAT_ID": "12\nX=1"}}).status_code == 400
    assert c.post("/api/env/update", headers=hdr(dash), json={"set": {"ANTHROPIC_BASE_URL": "http://evil"}}).status_code == 400
    assert c.post("/api/env/update", headers=hdr(dash), json={"clear": ["KIS_APP_KEY"]}).status_code == 200
    assert "KIS_APP_KEY" not in env.read_text()
    app.settings.extra["demo"] = True
    assert c.post("/api/env/update", headers=hdr(dash), json={"set": {"TELEGRAM_CHAT_ID": "1234"}}).status_code == 400


# ── 휴대폰 접속 (LAN) ──
def test_lan_mode_requires_pin(tmp_path):
    timeutil.set_clock(datetime(2026, 11, 9, 10, 0, tzinfo=timeutil.KST))
    s = load_settings(tmp_path)
    s.broker = "sim"
    app = App(s, console=False)
    with pytest.raises(ValueError):
        Dashboard(app, 8765, lan=True, pin="12")
    dash = Dashboard(app, 8765, lan=True, pin="246810")
    with TestClient(dash.asgi(), base_url="http://192.168.0.23:8765", follow_redirects=False) as c:
        assert c.get("/").status_code == 303 and c.get("/").headers["location"] == "/login"
        assert c.get("/api/status").status_code == 401
        assert c.get("/api/status", headers={"Host": "jangjung.evil.com:8765"}).status_code == 403   # 도메인 Host 거부
        assert c.post("/login", data={"pin": "000000"}).status_code == 401
        r = c.post("/login", data={"pin": "246810"})
        assert r.status_code == 303 and "jj_session" in r.headers.get("set-cookie", "")
        assert c.get("/api/status").status_code == 200
        assert dash.token in c.get("/").text
        assert c.post("/api/stop").status_code == 403                    # 로그인해도 CSRF 토큰은 필요
    with TestClient(dash.asgi(), base_url="http://192.168.0.23:8765", follow_redirects=False) as c2:
        for _ in range(4):
            c2.post("/login", data={"pin": "111111"})
        assert c2.post("/login", data={"pin": "246810"}).status_code == 429   # 5회 실패 → 잠금
    timeutil.set_clock(None)
