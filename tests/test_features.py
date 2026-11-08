"""실전 계좌 잠금 · 알림 설정 · 텔레그램 보고서 · 설정 백업/복구 (네트워크 없이)."""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from starlette.testclient import TestClient

from jangjung import pdfreport, timeutil
from jangjung.app import App, RealTradingLocked
from jangjung.config import load_settings
from jangjung.notify import ConsoleNotifier
from jangjung.web import Dashboard


class FakeTelegram(ConsoleNotifier):
    """텔레그램 대신 받은 메시지·파일을 기록."""

    def __init__(self):
        super().__init__()
        self.chat_id = "12345"
        self.docs: list[tuple[str, bytes, str]] = []

    async def send(self, text, buttons=None):
        self.sent.append((text, buttons))
        return str(len(self.sent))

    async def send_document(self, filename, data, caption="", mime="application/pdf"):
        self.docs.append((filename, data, caption))


@pytest.fixture
def make(tmp_path):
    timeutil.set_clock(datetime(2026, 11, 6, 16, 5, tzinfo=timeutil.KST))

    def _make(notifier=None, broker="sim"):
        s = load_settings(tmp_path)
        s.broker = broker
        s.engine["fill_wait_seconds"] = 0
        return App(s, notifier=notifier, console=False)
    yield _make
    timeutil.set_clock(None)


# ── 1. 실전 계좌: 이중 잠금 ──
def test_real_trading_requires_explicit_unlock(make, monkeypatch):
    for k, v in {"KIS_APP_KEY": "k" * 36, "KIS_APP_SECRET": "s" * 180}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("KIS_REAL_TRADING", raising=False)
    with pytest.raises(RealTradingLocked):
        make(broker="kis_real")
    monkeypatch.setenv("KIS_REAL_TRADING", "yes")
    tmp = make(broker="sim").settings
    tmp.broker, tmp.kis_app_key, tmp.kis_app_secret, tmp.kis_account = "kis_real", "k" * 36, "s" * 180, "12345678"
    app = App(tmp, console=False)
    b = app.broker
    assert b.name == "kis-real" and b.real
    assert str(b.http.base_url).startswith("https://openapi.koreainvestment.com:9443")
    assert b.tr["buy"] == "TTTC0012U" and b.tr["sell"] == "TTTC0011U" and b.tr["balance"] == "TTTC8434R"
    assert b.token_path.name == "kis_token_real.json"
    tmp.broker = "kis"
    mock = App(tmp, console=False).broker
    assert mock.name == "kis-mock" and mock.tr["buy"] == "VTTC0012U" and "openapivts" in str(mock.http.base_url)


def test_ui_cannot_switch_to_real(make, monkeypatch, tmp_path):
    import jangjung.settings_store as ss
    monkeypatch.setattr(ss, "ENV_PATH", tmp_path / ".env")
    app = make()
    dash = Dashboard(app, 8765)
    with TestClient(dash.asgi(), base_url="http://127.0.0.1:8765") as c:
        r = c.post("/api/env/update", headers={"X-JJ-Token": dash.token}, json={"set": {"BROKER": "kis_real"}})
        assert r.status_code == 400 and "실전" in r.json()["error"]
    monkeypatch.setenv("BROKER", "kis_real")
    row = next(e for e in app.store.env_describe() if e["key"] == "BROKER")
    assert row["locked"]
    with pytest.raises(ss.SettingsError):
        app.store.update_env({"BROKER": "sim"}, [])


# ── 4. 알림 설정 ──
async def test_muted_alerts_stay_in_dashboard_only(make):
    tg = FakeTelegram()
    app = make(tg)
    app.settings.notify["fill"] = "off"
    await app.notifier.send("[체결] 테스트", category="fill")
    await app.notifier.send("[장 마감 요약] 테스트", category="summary")
    await app.notifier.send("[시세 이상] 경고")                                    # 안전 경고: 항상
    await app.notifier.send("[확인 카드]", [[("승인", "pd|P1|approve")]], category="fill")   # 카드: 항상
    sent = [t for t, _ in tg.sent]
    assert sent == ["[장 마감 요약] 테스트", "[시세 이상] 경고", "[확인 카드]"]
    rows = app.db.q("SELECT text, via FROM messages ORDER BY id")
    assert rows[0] == {"text": "[체결] 테스트", "via": "muted"}                     # 대시보드에는 남음


async def test_alerts_command(make):
    tg = FakeTelegram()
    app = make(tg)
    out = await app.handle_text("/alerts", via="telegram")
    assert "체결 알림: 켜짐" in out
    out = await app.handle_text("/alerts 체결 off", via="telegram")
    assert "체결 알림: 꺼짐" in out and app.settings.notify["fill"] == "off"
    local = json.loads((app.settings.data_dir / "settings.local.json").read_text(encoding="utf-8"))
    assert local["notify"] == {"fill": "off"}
    assert "사용법" in await app.handle_text("/alerts 없는것 off", via="telegram")
    assert load_settings(app.settings.data_dir).notify["fill"] == "off"           # 다시 시작해도 유지


# ── 5. 보고서: 텔레그램 · 메일 선택, 세 종류 모두 ──
async def test_report_command_sends_pdf_to_telegram(make):
    tg = FakeTelegram()
    app = make(tg)
    out = await app.handle_text("/report all 2026-11-06", via="telegram")
    assert "일간·주간·월간 보고서를 텔레그램" in out, out
    assert [d[0] for d in tg.docs] == ["jangjung_daily_2026-11-06.pdf", "jangjung_weekly_2026-11-06.pdf",
                                       "jangjung_monthly_2026-11-06.pdf"]
    assert all(d[1].startswith(b"%PDF") for d in tg.docs)
    assert "사용법" in await app.handle_text("/report yearly", via="telegram")
    assert "텔레그램 봇이 연결" in await make(None).handle_text("/report daily")


async def test_engine_delivers_all_periods_to_chosen_channels(make, monkeypatch):
    tg = FakeTelegram()
    app = make(tg)
    sent_mail = []
    monkeypatch.setattr(pdfreport, "send_mail", lambda subj, body, atts: sent_mail.append((subj, atts)) or ["me@x.com"])
    monkeypatch.setattr(pdfreport, "mail_ready", lambda: True)
    timeutil.set_clock(datetime(2026, 11, 30, 16, 5, tzinfo=timeutil.KST))     # 월말(월)
    app.settings.report.update(email_daily="on", email_weekly="on", email_monthly="on", channel="both")
    assert pdfreport.channels_for(app, "both") == ["email", "telegram"]
    msg = await app.engine.report_mail()
    assert "일간·월간" in msg and "메일" in msg and "텔레그램" in msg
    assert [a[0] for a in sent_mail[0][1]] == ["jangjung_daily_2026-11-30.pdf", "jangjung_monthly_2026-11-30.pdf"]
    assert [d[0] for d in tg.docs] == ["jangjung_daily_2026-11-30.pdf", "jangjung_monthly_2026-11-30.pdf"]
    # 텔레그램만
    tg.docs.clear(); sent_mail.clear()
    app.settings.report["channel"] = "telegram"
    await app.engine.report_mail()
    assert sent_mail == [] and len(tg.docs) == 2
    # 보낼 곳이 없으면 조용히 건너뜀
    app.settings.report["channel"] = "email"
    monkeypatch.setattr(pdfreport, "mail_ready", lambda: False)
    assert await app.engine.report_mail() is None


# ── 7. 설정 백업 / 복구 ──
def test_settings_backup_and_restore(make, monkeypatch, tmp_path):
    import jangjung.settings_store as ss
    monkeypatch.setattr(ss, "ENV_PATH", tmp_path / ".env")
    for k, v in {"KIS_APP_KEY": "PSsecretKEY", "SMTP_PASSWORD": "app-pass", "SMTP_HOST": "smtp.gmail.com",
                 "KIS_ACCOUNT": "12345678"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    app = make()
    dash = Dashboard(app, 8765)
    hdr = {"X-JJ-Token": dash.token}
    with TestClient(dash.asgi(), base_url="http://127.0.0.1:8765") as c:
        c.post("/api/settings/update", headers=hdr, json={"section": "safety", "values": {"max_buy_order_amount": 2_000_000}})
        r = c.get("/api/settings/backup")
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
        assert "PSsecretKEY" not in r.text and "app-pass" not in r.text and "12345678" not in r.text   # 비밀값·계좌 제외
        backup = r.json()
        assert backup["settings"]["safety"]["max_buy_order_amount"] == 2_000_000
        assert backup["env"]["SMTP_HOST"] == "smtp.gmail.com" and "BROKER" not in backup["env"]
        # 설정을 바꾼 뒤 백업으로 되돌리기
        c.post("/api/settings/update", headers=hdr, json={"section": "safety", "values": {"max_buy_order_amount": 5_000_000}})
        c.post("/api/settings/update", headers=hdr, json={"section": "notify", "values": {"summary": "off"}})
        backup["env"]["TELEGRAM_CHAT_ID"] = "987654321"
        prev = c.post("/api/settings/restore", headers=hdr, json={"data": backup}).json()
        labels = {(x["section"], x["label"]) for x in prev["changes"]}
        assert ("안전 한도", "1회 매수 한도") in labels and ("알림", "장 마감 요약") in labels
        assert ("연결 설정", "텔레그램 chat_id") in labels
        assert app.settings.safety["max_buy_order_amount"] == 5_000_000                 # 미리보기는 적용 안 함
        r = c.post("/api/settings/restore", headers=hdr, json={"data": backup, "apply": True})
        assert r.status_code == 200 and r.json()["applied"]
        assert app.settings.safety["max_buy_order_amount"] == 2_000_000 and app.settings.notify["summary"] == "on"
        assert app.settings.telegram_chat_id == "987654321"
        assert any("[설정 복구]" in m["text"] for m in c.get("/api/feed").json()["messages"])
        # 범위를 깨는 값이 하나라도 있으면 아무것도 바꾸지 않음
        bad = json.loads(json.dumps(backup))
        bad["settings"]["engine"]["poll_seconds"] = 1
        bad["settings"]["safety"]["max_buy_order_amount"] = 9_000_000
        r = c.post("/api/settings/restore", headers=hdr, json={"data": bad, "apply": True})
        assert r.status_code == 400 and "운영 일정" in r.json()["error"]
        assert app.settings.safety["max_buy_order_amount"] == 2_000_000
        # 필수 안전 키워드는 복구로도 지울 수 없음
        bad = json.loads(json.dumps(backup))
        bad["settings"]["safety"]["blocked_name_keywords"] = []
        c.post("/api/settings/restore", headers=hdr, json={"data": bad, "apply": True})
        assert {"레버리지", "인버스"} <= set(app.settings.safety["blocked_name_keywords"])
        # 다른 파일 · 토큰 없는 요청
        assert c.post("/api/settings/restore", headers=hdr, json={"data": {"foo": 1}}).status_code == 400
        assert c.post("/api/settings/restore", json={"data": backup, "apply": True}).status_code == 403
