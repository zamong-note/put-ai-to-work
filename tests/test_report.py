"""PDF 보고서(일간·주간·월간)와 메일 발송 — 네트워크 없이 가짜 SMTP로 검증."""
from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from starlette.testclient import TestClient

from jangjung import pdfreport, timeutil
from jangjung.app import App
from jangjung.config import load_settings
from jangjung.web import Dashboard

FRI_CLOSE = datetime(2026, 11, 6, 16, 1, tzinfo=timeutil.KST)


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=None, **kw):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, pw):
        assert pw == "app-pass"

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture
def mail(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(pdfreport.smtplib, "SMTP", FakeSMTP)
    for k, v in {"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587", "SMTP_USER": "me@example.com",
                 "SMTP_PASSWORD": "app-pass", "REPORT_EMAIL_TO": "me@example.com"}.items():
        monkeypatch.setenv(k, v)
    return FakeSMTP


@pytest.fixture
def app(tmp_path):
    timeutil.set_clock(datetime(2026, 11, 2, 9, 40, tzinfo=timeutil.KST))
    s = load_settings(tmp_path)
    s.broker = "sim"
    s.engine["fill_wait_seconds"] = 0
    a = App(s, console=False)
    yield a
    timeutil.set_clock(None)


async def _week_of_trading(app):
    """월요일 자동 투자 시작 → 월~금 분할 매수·장 마감 요약."""
    app.autoinvest.save_profile([3] * 6)
    res = await app.autoinvest.propose(10_000_000, 3)
    await app.pending.resolve(res["pending_id"], "approve")
    for day in range(2, 7):
        timeutil.set_clock(datetime(2026, 11, day, 9, 40, tzinfo=timeutil.KST))
        if day > 2:
            await app.autoinvest.build_step()
        timeutil.set_clock(datetime(2026, 11, day, 15, 41, tzinfo=timeutil.KST))
        await app.engine.daily_summary()
        app.engine._mark("summary")


def test_due_periods():
    hol = {"2026-12-31", "2027-01-01"}
    cfg = {"email_daily": "on", "email_weekly": "on", "email_monthly": "on"}
    assert pdfreport.due_periods(date(2026, 11, 5), hol, cfg) == ["daily"]                     # 목
    assert pdfreport.due_periods(date(2026, 11, 6), hol, cfg) == ["daily", "weekly"]           # 금
    assert pdfreport.due_periods(date(2026, 11, 30), hol, cfg) == ["daily", "monthly"]         # 월말(월)
    assert pdfreport.due_periods(date(2026, 12, 30), hol, cfg) == ["daily", "weekly", "monthly"]  # 연말 휴장 전
    assert pdfreport.due_periods(date(2026, 12, 31), hol, cfg) == []                           # 휴장일
    assert pdfreport.due_periods(date(2026, 11, 6), hol, {**cfg, "email_daily": "off"}) == ["weekly"]
    assert pdfreport.period_range("weekly", date(2026, 11, 6)) == (date(2026, 11, 2), date(2026, 11, 6))
    assert pdfreport.period_range("monthly", date(2026, 11, 6)) == (date(2026, 11, 1), date(2026, 11, 6))


async def test_reports_are_3_to_5_pages_with_summary(app):
    await _week_of_trading(app)
    timeutil.set_clock(FRI_CLOSE)
    for period in ("daily", "weekly", "monthly"):
        rep = await pdfreport.build_report(app, period, "2026-11-06")
        assert rep.pdf.startswith(b"%PDF") and 3 <= rep.pages <= 5, (period, rep.pages)
    data = await pdfreport.collect(app, "weekly", "2026-11-06")
    assert data["counts"]["filled"] == 12 and data["counts"]["buys"] == 12          # 3회 × 4종목
    assert len(data["series"]) == 5 and data["violations"] == []
    assert data["auto"]["rows"] and data["own"]                                    # 비중·보유 종목
    lines = pdfreport.highlights(data)
    assert lines[0].startswith("자동 투자 중립 3단계: 주간") and "체결" in lines[1]
    # 과거 날짜: 장 마감 스냅샷으로 비중을 보여 준다
    timeutil.set_clock(datetime(2026, 11, 20, 16, 0, tzinfo=timeutil.KST))
    past = await pdfreport.collect(app, "daily", "2026-11-04")
    assert past["as_of"] == "11/04 장 마감 기준" and past["auto"]["rows"]


async def test_many_orders_stay_within_5_pages(app):
    await _week_of_trading(app)
    timeutil.set_clock(FRI_CLOSE)
    o = app.db.q("SELECT * FROM orders LIMIT 1")[0]
    cols = [k for k in o if k != "id"]
    for i in range(200):
        row = dict(o, created_at=f"2026-11-{3 + i % 4:02d}T10:{i % 60:02d}:00+09:00", reason=f"테스트 근거 {i} " * 4)
        app.db.x(f"INSERT INTO orders({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [row[c] for c in cols])
    rep = await pdfreport.build_report(app, "monthly", "2026-11-06")
    assert 3 <= rep.pages <= 5
    assert rep.csv and rep.csv.decode("utf-8-sig").count("\n") == 212 + 1          # 전체 주문은 CSV 첨부


async def test_email_reports_one_mail_with_pdfs(app, mail):
    await _week_of_trading(app)
    timeutil.set_clock(FRI_CLOSE)
    msg = await pdfreport.email_reports(app, ["daily", "weekly"], "2026-11-06")
    assert "일간·주간" in msg and "me***@example.com" in msg
    m = mail.sent[-1]
    assert "11/06 일간·주간 보고서" in m["Subject"] and m["To"] == "me@example.com"
    files = [p.get_filename() for p in m.iter_attachments()]
    assert files == ["jangjung_daily_2026-11-06.pdf", "jangjung_weekly_2026-11-06.pdf",
                     "jangjung_orders_2026-11-02_2026-11-06.csv"]
    assert app.db.kv_get("report_mail_last")["ok"]


async def test_engine_mails_after_close_once(app, mail):
    await _week_of_trading(app)
    timeutil.set_clock(datetime(2026, 11, 6, 15, 50, tzinfo=timeutil.KST))
    await app.engine.step()
    assert mail.sent == []                                         # 16:00 전
    timeutil.set_clock(FRI_CLOSE)
    await app.engine.step()
    assert len(mail.sent) == 1 and "일간·주간" in mail.sent[0]["Subject"]
    await app.engine.step()
    assert len(mail.sent) == 1                                     # 하루 한 번
    assert any("[보고서 발송]" in m["text"] for m in app.db.q("SELECT text FROM messages"))


async def test_mail_failure_is_reported_not_raised(app, mail, monkeypatch):
    await _week_of_trading(app)
    timeutil.set_clock(FRI_CLOSE)

    def boom(*a, **k):
        raise pdfreport.MailError("메일 로그인 실패")
    monkeypatch.setattr(pdfreport, "send_mail", boom)
    assert await app.engine.report_mail() is None
    assert app.db.kv_get("report_mail_last")["ok"] is False
    assert any("[보고서 발송 실패]" in m["text"] for m in app.db.q("SELECT text FROM messages"))


def test_web_pdf_email_and_settings(tmp_path, monkeypatch):
    timeutil.set_clock(datetime(2026, 11, 9, 10, 0, tzinfo=timeutil.KST))
    for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "REPORT_EMAIL_TO"):
        monkeypatch.delenv(k, raising=False)
    s = load_settings(tmp_path)
    s.broker = "sim"
    app = App(s, console=False)
    dash = Dashboard(app, 8765)
    hdr = {"X-JJ-Token": dash.token}
    with TestClient(dash.asgi(), base_url="http://127.0.0.1:8765") as c:
        r = c.get("/api/report/pdf?period=weekly&date=2026-11-09&download=1")
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
        assert "attachment" in r.headers["content-disposition"] and 3 <= int(r.headers["x-report-pages"]) <= 5
        assert c.get("/api/report/pdf?period=yearly").status_code == 400
        assert c.post("/api/report/email", json={"period": "daily"}, headers=hdr).status_code == 400   # 메일 미설정
        assert c.post("/api/report/email", json={"period": "daily"}).status_code == 403                # 토큰 없음
        st = c.get("/api/settings").json()
        assert st["email"]["ready"] is False and {f["key"] for f in st["schema"]["report"]} >= {"send_time"}
        assert c.post("/api/settings/update", headers=hdr,
                      json={"section": "report", "values": {"send_time": "15:35"}}).status_code == 400  # 요약(15:40) 전
        r = c.post("/api/settings/update", headers=hdr,
                   json={"section": "report", "values": {"email_daily": "off", "send_time": "17:30"}})
        assert r.status_code == 200 and app.settings.report["email_daily"] == "off"
        local = json.loads((tmp_path / "settings.local.json").read_text(encoding="utf-8"))
        assert local["report"] == {"email_daily": "off", "send_time": "17:30"}
        assert load_settings(tmp_path).report["send_time"] == "17:30"
        assert "email" in c.get("/api/reports").json()
    timeutil.set_clock(None)
