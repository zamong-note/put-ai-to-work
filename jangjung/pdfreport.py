"""일간·주간·월간 PDF 보고서 + 메일·텔레그램 발송.

구성 (총 3~5장):
  1장  핵심 요약 — 수익, 주문, 안전 장치, 점검 결과, 확인할 일, 다음 일정
  2장  수익·자산 상세 — 날짜별 평가액, 자동 투자 비중(목표 대비), 직접 보유 종목
  3장~ 주문 내역(근거·판단가·체결가) → 안전 장치 기록 → 규칙 현황 → 주문 점검 → 현재 안전 설정
5장을 넘으면 주문·기록 표를 줄이고, 전체 주문은 CSV로 함께 첨부한다.

모든 내용은 DB 기록에서 결정론적으로 만든다 (LLM 미사용).
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from . import timeutil
from .audit import audit
from .broker.base import BrokerError
from .report import SOURCE, STATUS, orders_csv
from .rules import rule_line

log = logging.getLogger("jangjung")

PERIODS = {"daily": "일간", "weekly": "주간", "monthly": "월간"}
BROKER_NAMES = {"sim": "시뮬레이터", "kis-mock": "KIS 모의투자", "kis-real": "KIS 실전"}
MAX_PAGES, MIN_PAGES = 5, 3
SAFETY_KINDS = {
    "symbol_resume": "시세 이상 · 종목 매매 중단",
    "stoploss_anomaly": "시세 이상 중 손절 확인",
    "order_over_limit": "안전 한도 초과 주문 확인",
    "loss_resume": "자동 투자 손실 한도 도달",
    "resume_all": "전체 중지 해제 요청",
}
ACTION_LABEL = {"approve": "승인", "approve_over": "한도 초과 알고 승인", "cancel": "취소", "sell": "그래도 매도",
                "hold": "보류", "resume": "재개", "keep": "중단 유지", "expired": "만료"}


# ───────────────────────────── 기간 ─────────────────────────────
def period_range(period: str, end: date) -> tuple[date, date]:
    if period == "daily":
        return end, end
    if period == "weekly":
        return end - timedelta(days=end.weekday()), end
    if period == "monthly":
        return end.replace(day=1), end
    raise ValueError(f"알 수 없는 기간: {period}")


def next_trading_day(d: date, holidays: set[str]) -> date:
    x = d + timedelta(days=1)
    while not timeutil.is_trading_day(x, holidays):
        x += timedelta(days=1)
    return x


def due_periods(d: date, holidays: set[str], cfg: dict) -> list[str]:
    """d(거래일)에 보내야 할 보고서들. 주간 = 그 주 마지막 거래일, 월간 = 그 달 마지막 거래일."""
    if not timeutil.is_trading_day(d, holidays):
        return []
    nxt = next_trading_day(d, holidays)
    out = []
    if cfg.get("email_daily") == "on":
        out.append("daily")
    if cfg.get("email_weekly") == "on" and nxt.isocalendar()[:2] != d.isocalendar()[:2]:
        out.append("weekly")
    if cfg.get("email_monthly") == "on" and (nxt.year, nxt.month) != (d.year, d.month):
        out.append("monthly")
    return out


# ───────────────────────────── 데이터 ─────────────────────────────
def _first_line(text: str | None) -> str:
    if not text:
        return ""
    t = text.strip().split("\n")
    head = t[0].strip("[] ")
    return head + (f" — {t[1].strip()}" if len(t) > 1 and len(head) < 40 else "")


async def collect(app, period: str, end: str) -> dict:
    """보고서에 들어갈 모든 숫자를 모은다. end 가 오늘이면 현재 시세로 비중·보유 종목도 넣는다."""
    db = app.db
    end_d = date.fromisoformat(end)
    start_d, _ = period_range(period, end_d)
    start = start_d.isoformat()
    today = timeutil.today_str()
    live_val = live_acct = None
    if end == today:
        try:
            live_val = await app.autoinvest.valuation()
            live_acct = await app.broker.get_account()
        except BrokerError as e:
            log.warning("보고서용 시세 조회 실패: %s", e)

    # 수익 (자동 투자 평가액 스냅샷)
    snaps = db.q("SELECT * FROM daily_snapshots WHERE date BETWEEN ? AND ? ORDER BY date", (start, end))
    before = db.one("SELECT * FROM daily_snapshots WHERE date<? AND auto_value IS NOT NULL ORDER BY date DESC LIMIT 1",
                    (start,))
    series = [{"date": s["date"], "value": s["auto_value"], "principal": s["auto_principal"],
               "rule_fills": s["rule_fills"], "active_rules": s["active_rules"]} for s in snaps if s["auto_value"]]
    if live_val and (not series or series[-1]["date"] != end):
        series.append({"date": end, "value": live_val["total"], "principal": live_val["state"]["principal"],
                       "rule_fills": None, "active_rules": app.rules.active_count(), "live": True})
    prev_v = before["auto_value"] if before else None
    for s in series:
        base = prev_v or s["principal"]
        s["day_pct"] = (s["value"] / base - 1) * 100 if base else None
        s["cum_pct"] = (s["value"] / s["principal"] - 1) * 100 if s["principal"] else None
        prev_v = s["value"]
    st = app.autoinvest.state()
    auto = None
    if series or st:
        last = series[-1] if series else None
        principal = (last or {}).get("principal") or (st or {}).get("principal")
        base = (before or {}).get("auto_value") or (series[0]["principal"] if series else principal)
        value = last["value"] if last else None
        tier = (st or {}).get("tier")
        spec = app.universe.tier(tier) if tier else None
        auto = {"tier": tier, "label": spec["label"] if spec else "", "status": (st or {}).get("status"),
                "principal": principal, "value": value,
                "period_pct": (value / base - 1) * 100 if value and base else None,
                "cum_pct": (value / principal - 1) * 100 if value and principal else None,
                "loss_limit_pct": spec["loss_limit_pct"] if spec else None,
                "build": [(st or {}).get("tranches_done"), (st or {}).get("tranches_total")] if st else None,
                "rows": [dict(r) for r in live_val["rows"]] if live_val else None,
                "cash": live_val["cash"] if live_val else None}

    # 비중·보유 종목: 오늘이면 현재 시세, 과거면 기간 마지막 장 마감 스냅샷
    as_of, own = ("보고서 생성 시점 시세" if (live_val or live_acct) else None), None
    snap = None if (live_val or live_acct) else next((s for s in reversed(snaps) if s.get("detail")), None)
    if snap:
        det = json.loads(snap["detail"])
        as_of = f"{snap['date'][5:].replace('-', '/')} 장 마감 기준"
        if auto and det.get("auto_rows"):
            auto["rows"], auto["cash"] = det["auto_rows"], det.get("cash")
        if det.get("own") is not None:
            own = sorted((dict(o, value=o["qty"] * o["price"],
                               ret=(o["price"] / o["avg"] - 1) * 100 if o["avg"] else None) for o in det["own"]),
                         key=lambda x: -x["value"])
    if live_acct:
        auto_pos = app.gateway.ledger.positions()
        own = []
        for h in live_acct.holdings.values():
            q = h.qty - auto_pos.get(h.symbol, {}).get("qty", 0)
            if q > 0:
                own.append({"name": h.name, "symbol": h.symbol, "qty": q, "avg": h.avg_price, "price": h.cur_price,
                            "value": q * h.cur_price,
                            "ret": (h.cur_price / h.avg_price - 1) * 100 if h.avg_price else None})
        own.sort(key=lambda x: -x["value"])

    # 주문
    orders = db.q("SELECT * FROM orders WHERE substr(created_at,1,10) BETWEEN ? AND ? ORDER BY id", (start, end))
    rows = []
    for o in orders:
        gap = (o["avg_fill_price"] / o["decision_price"] - 1) * 100 if o["avg_fill_price"] and o["decision_price"] else None
        lat = None
        if o["detected_at"]:
            lat = (datetime.fromisoformat(o["created_at"]) - datetime.fromisoformat(o["detected_at"])).total_seconds()
        rows.append({"id": o["id"], "at": o["created_at"][5:16].replace("T", " "), "source": SOURCE.get(o["source"], o["source"]),
                     "src": o["source"], "name": o["name"], "side": o["side"], "qty": o["qty"],
                     "filled": o["filled_qty"] or 0, "decision": o["decision_price"], "fill": o["avg_fill_price"],
                     "gap": gap, "lat": lat, "status": o["status"], "status_label": STATUS.get(o["status"], o["status"]),
                     "reason": o["reason"] or "", "error": o["error"] or ""})
    done = [r for r in rows if r["status"] in ("filled", "partial")]
    by_sym: dict[str, dict] = {}
    for r in done:
        b = by_sym.setdefault(r["name"], {"name": r["name"], "buy_n": 0, "buy_q": 0, "buy_amt": 0.0,
                                          "sell_n": 0, "sell_q": 0, "sell_amt": 0.0})
        k = "buy" if r["side"] == "buy" else "sell"
        b[f"{k}_n"] += 1
        b[f"{k}_q"] += r["filled"]
        b[f"{k}_amt"] += r["filled"] * (r["fill"] or 0)
    trades = sorted(by_sym.values(), key=lambda b: -(b["buy_amt"] + b["sell_amt"]))
    buy_amt = sum(r["filled"] * (r["fill"] or 0) for r in done if r["side"] == "buy")
    sell_amt = sum(r["filled"] * (r["fill"] or 0) for r in done if r["side"] == "sell")
    gaps = [r["gap"] for r in done if r["gap"] is not None]
    lats = [r["lat"] for r in rows if r["lat"] is not None]
    counts = {
        "orders": len(rows), "filled": len(done),
        "buys": sum(1 for r in done if r["side"] == "buy"), "sells": sum(1 for r in done if r["side"] == "sell"),
        "buy_amt": buy_amt, "sell_amt": sell_amt,
        "rule": sum(1 for r in done if r["src"] == "rule"), "auto": sum(1 for r in done if r["src"] != "rule"),
        "failed": sum(1 for r in rows if r["status"] in ("failed", "rejected")),
        "open": sum(1 for r in rows if r["status"] in ("submitted", "submitting")),
        "rules_fired": len({r["id"] for r in rows if r["src"] == "rule"}),
        "avg_gap": sum(gaps) / len(gaps) if gaps else None,
        "max_abs_gap": max((abs(g) for g in gaps), default=None),
        "max_lat": max(lats) if lats else None,
    }
    ids = {f"주문#{r['id']} " for r in rows}
    violations = [v for v in audit(db, app.universe)["violations"] if any(v.startswith(i) for i in ids)]

    # 안전 장치·확인 카드 기록
    pend = db.q("SELECT * FROM pending WHERE substr(created_at,1,10) BETWEEN ? AND ? ORDER BY created_at", (start, end))
    events = []
    for p in pend:
        if p["kind"] not in SAFETY_KINDS:
            continue
        text = None
        if p["message_id"]:
            mid = str(p["message_id"]).split(":")[0]
            if mid.isdigit():
                m = db.one("SELECT text FROM messages WHERE id=?", (int(mid),))
                text = m["text"] if m else None
        res = (p["result"] or "").split(":", 1)
        outcome = ACTION_LABEL.get(res[0], res[0]) if p["status"] != "expired" else "만료"
        if p["status"] == "open":
            outcome = "대기 중"
        events.append({"at": p["created_at"][5:16].replace("T", " "), "kind": SAFETY_KINDS[p["kind"]],
                       "detail": _first_line(text), "outcome": outcome, "k": p["kind"]})
    halts = db.q("SELECT ts, text FROM messages WHERE substr(ts,1,10) BETWEEN ? AND ? AND "
                 "(text LIKE '⛔ 전체 중지했습니다%' OR text LIKE '[API 오류로 전체 중지]%') ORDER BY id", (start, end))
    for hm in halts:
        events.append({"at": hm["ts"][5:16].replace("T", " "), "kind": "전체 중지",
                       "detail": _first_line(hm["text"]).lstrip("⛔ "), "outcome": "", "k": "halt"})
    events.sort(key=lambda e: e["at"])
    ev_count = {k: sum(1 for e in events if e["k"] == k) for k in (*SAFETY_KINDS, "halt")}
    changes = db.q("SELECT ts, text FROM messages WHERE substr(ts,1,10) BETWEEN ? AND ? AND "
                   "(text LIKE '[설정 변경%' OR text LIKE '[연결 설정 변경]%') ORDER BY id", (start, end))

    # 확인할 일 · 다음 일정
    attention = []
    if h := app.control.halted():
        attention.append(f"지금 전체 중지 상태입니다 ({h['reason']}). 재개하려면 /resume")
    for sym, v in app.control.suspended().items():
        attention.append(f"{app.universe.name(sym)} 매매 일시중단 중 ({v['reason']})")
    if counts["open"]:
        attention.append(f"미체결 주문 {counts['open']}건 — MTS에서 확인")
    if counts["failed"]:
        attention.append(f"실패·거부된 주문 {counts['failed']}건 — 주문 내역의 오류 확인")
    if auto and auto["rows"]:
        band = app.autoinvest.cfg["rebalance_band_pp"]
        off = [f"{r['name']} {r['weight']:.1f}% (목표 {r['target']:g}%)" for r in auto["rows"] if abs(r["dev"]) > band]
        if off:
            attention.append(f"목표 비중 ±{band:g}%p 이탈: " + ", ".join(off))
    if auto and auto["cum_pct"] is not None and auto["loss_limit_pct"]:
        room = auto["cum_pct"] + auto["loss_limit_pct"]
        if room < auto["loss_limit_pct"] / 2:
            attention.append(f"손실 한도(-{auto['loss_limit_pct']}%)까지 {room:.1f}%p 남음")
    if violations:
        attention.append(f"규칙·전략과 다른 주문 {len(violations)}건 — 4장 주문 점검 확인")
    n_open = len(app.pending.open_items())
    if n_open:
        attention.append(f"답하지 않은 확인 카드 {n_open}개")
    hol = app.settings.holidays
    nxt = next_trading_day(end_d, hol)
    nxt_month = date(end_d.year + (end_d.month == 12), end_d.month % 12 + 1, 1)
    rebal = timeutil.first_trading_day_of_month(nxt_month if end_d >= timeutil.first_trading_day_of_month(end_d, hol)
                                               else end_d, hol)
    schedule = [f"다음 거래일 {nxt.strftime('%m/%d')}({'월화수목금토일'[nxt.weekday()]})"]
    if st:
        schedule.append(f"다음 리밸런싱 점검 {rebal.strftime('%m/%d')} {app.settings.engine['rebalance_time']} "
                        f"({'자동 실행' if app.autoinvest.rebalance_mode() == 'auto' else '확인 후 실행'})")
        if st["status"] == "building":
            schedule.append(f"분할 매수 {st['tranches_done']}/{st['tranches_total']}회 완료 · 다음 회차 "
                            f"{nxt.strftime('%m/%d')} {app.settings.engine['build_time']}")
    schedule.append(f"감시 중 규칙 {app.rules.active_count()}개")

    s = app.settings.safety
    limits = [("1회 매수 한도", f"{s['max_buy_order_amount']:,.0f}원"),
              ("1회 매도 한도", f"{s['max_sell_order_amount']:,.0f}원" if s.get("max_sell_order_amount") else "해제 (손절 우선)"),
              ("하루 주문 / 매수 금액", f"{s['max_daily_orders']}회 / {s['max_daily_buy_amount']:,.0f}원"),
              ("급등락 차단", f"전일 대비 ±{s['max_change_pct_from_prev_close']:g}% · {s['tick_jump_window_sec']}초 내 ±{s['max_tick_jump_pct']:g}%"),
              ("API 연속 오류", f"{s['max_consecutive_api_errors']}회면 전체 중지"),
              ("제외 상품", ", ".join(s["blocked_name_keywords"]))]
    if auto and auto["loss_limit_pct"]:
        limits.insert(0, ("자동 투자 손실 한도", f"-{auto['loss_limit_pct']}% (도달 시 매매 중단 후 확인)"))

    return {
        "period": period, "label": PERIODS[period], "start": start, "end": end,
        "generated_at": timeutil.now().strftime("%Y-%m-%d %H:%M"),
        "broker": BROKER_NAMES.get(app.broker.name, app.broker.name),
        "live": live_val is not None or live_acct is not None,
        "auto": auto, "series": series, "own": own, "orders": rows, "counts": counts, "trades": trades,
        "as_of": as_of,
        "violations": violations, "events": events, "ev_count": ev_count, "changes": changes,
        "attention": attention, "schedule": schedule, "limits": limits,
        "rules": [rule_line(r) for r in app.rules.list()],
        "halted": app.control.halted(),
    }


def highlights(d: dict) -> list[str]:
    """1장 '핵심 요약' 문장 (숫자는 모두 기록에서)."""
    a, c, e = d["auto"], d["counts"], d["ev_count"]
    out = []
    if a and a["value"]:
        line = f"자동 투자 {a['label']} {a['tier']}단계: {d['label']} {_spct(a['period_pct'])}, 누적 {_spct(a['cum_pct'])}"
        out.append(line + f" (평가 {_won(a['value'])} / 원금 {_won(a['principal'])})")
    elif a:
        out.append(f"자동 투자 {a['tier']}단계 · 아직 평가 기록 없음")
    else:
        out.append("자동 투자를 시작하지 않았습니다.")
    if c["orders"]:
        out.append(f"주문 {c['orders']}건 중 {c['filled']}건 체결 — 매수 {c['buys']}건 {_won(c['buy_amt'])}, "
                   f"매도 {c['sells']}건 {_won(c['sell_amt'])}" + (f", 실패 {c['failed']}건" if c["failed"] else ""))
        out.append(f"규칙 매매 {c['rule']}건 · 자동 투자(분할 매수·리밸런싱) {c['auto']}건 체결")
    else:
        out.append("이 기간에는 주문이 없었습니다.")
    acts = [f"{lbl} {e[k]}회" for k, lbl in (("halt", "전체 중지"), ("symbol_resume", "시세 이상 중단"),
                                             ("stoploss_anomaly", "손절 확인"), ("order_over_limit", "한도 초과 확인"),
                                             ("loss_resume", "손실 한도")) if e.get(k)]
    out.append("안전 장치: " + (", ".join(acts) if acts else "작동한 일 없음"))
    chk = f"주문 점검: 규칙·전략과 다른 주문 {len(d['violations'])}건"
    if c["max_lat"] is not None:
        chk += f" · 조건 충족→주문 최대 {c['max_lat']:.0f}초"
    if c["avg_gap"] is not None:
        chk += f" · 체결가 괴리 평균 {0 if abs(c['avg_gap']) < 0.005 else c['avg_gap']:+.2f}%"
    out.append(chk)
    return out


def _won(v) -> str:
    return "—" if v is None else f"{v:,.0f}원"


def _num(v) -> str:
    return "—" if v is None else f"{v:,.0f}"


def _spct(v) -> str:
    if v is None:
        return "—"
    arrow = "▲" if v > 0.005 else "▼" if v < -0.005 else ""
    return f"{arrow}{v:+.2f}%"


# ───────────────────────────── PDF ─────────────────────────────
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SURFACE2, LINE = "#e1e0d9", "#c3c2b7", "#f3f2ee", "#dcdbd5"
SERIES1, UP, DOWN, CRIT, GOOD = "#2a78d6", "#c8312f", "#1c5cab", "#d03b3b", "#006300"
_FONTS: tuple[str, str] | None = None
_EMOJI = re.compile("[\u2600-\u27bf\U0001F000-\U0001FFFF\ufe0f]")


def _font_paths() -> list[tuple[str | None, str | None]]:
    home = Path.home()
    return [
        (os.getenv("REPORT_FONT"), os.getenv("REPORT_FONT_BOLD")),
        ("C:/Windows/Fonts/malgun.ttf", "C:/Windows/Fonts/malgunbd.ttf"),
        ("/usr/share/fonts/truetype/nanum/NanumGothic.ttf", "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf"),
        ("/Library/Fonts/NanumGothic.ttf", "/Library/Fonts/NanumGothicBold.ttf"),
        (str(home / "Library/Fonts/NanumGothic.ttf"), str(home / "Library/Fonts/NanumGothicBold.ttf")),
    ]


def fonts() -> tuple[str, str]:
    """한글 TTF(맑은 고딕·나눔고딕)를 찾아 PDF에 포함한다. 없으면 reportlab 내장 CID 폰트."""
    global _FONTS
    if _FONTS:
        return _FONTS
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for reg, bold in _font_paths():
        if reg and Path(reg).exists():
            pdfmetrics.registerFont(TTFont("JJ", reg))
            pdfmetrics.registerFont(TTFont("JJB", bold if bold and Path(bold).exists() else reg))
            for b, i, n in ((0, 0, "JJ"), (1, 0, "JJB"), (0, 1, "JJ"), (1, 1, "JJB")):
                addMapping("JJ", b, i, n)
            _FONTS = ("JJ", "JJB")
            return _FONTS
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    pdfmetrics.registerFont(UnicodeCIDFont("HYGothic-Medium"))
    for b, i in ((0, 0), (1, 0), (0, 1), (1, 1)):
        addMapping("HYGothic-Medium", b, i, "HYGothic-Medium")
    _FONTS = ("HYGothic-Medium", "HYGothic-Medium")
    return _FONTS


def _clean(t) -> str:
    """PDF 폰트에 없는 이모지를 빼고 XML 특수문자를 이스케이프."""
    from xml.sax.saxutils import escape
    return escape(_EMOJI.sub("", str(t if t is not None else "")).strip())


def _signed(v, fmt="{:+.2f}%") -> str:
    if v is None:
        return "—"
    color = UP if v > 0.005 else DOWN if v < -0.005 else INK2
    arrow = "▲ " if v > 0.005 else "▼ " if v < -0.005 else ""
    return f'<font color="{color}">{arrow}{fmt.format(v)}</font>'


def _line_chart(series: list[dict], width: float, height: float, font: str):
    """평가액 추이: 2px 선 + 원금 기준선(점선). 축은 데이터 범위로 확대 (면 채우기 없음)."""
    from reportlab.graphics.shapes import Circle, Drawing, Line, PolyLine, String
    from reportlab.lib import colors
    d = Drawing(width, height)
    vals = [s["value"] for s in series]
    principal = series[-1]["principal"]
    lo, hi = min(vals), max(vals)
    if principal and abs(principal - lo) / max(lo, 1) < 0.08:
        lo, hi = min(lo, principal), max(hi, principal)
    pad = (hi - lo) * 0.12 or max(hi * 0.004, 1)
    lo, hi = lo - pad, hi + pad
    left, right, top, bottom = 58, 70, 10, 22
    pw, ph = width - left - right, height - top - bottom

    def x(i):
        return left + (pw * i / (len(series) - 1) if len(series) > 1 else pw / 2)

    def y(v):
        return bottom + (v - lo) / (hi - lo) * ph

    for k in range(4):
        v = lo + (hi - lo) * k / 3
        d.add(Line(left, y(v), left + pw, y(v), strokeColor=colors.HexColor(GRID), strokeWidth=0.5))
        d.add(String(left - 6, y(v) - 3, f"{v / 10000:,.0f}만", fontName=font, fontSize=7,
                     fillColor=colors.HexColor(MUTED), textAnchor="end"))
    d.add(Line(left, bottom, left + pw, bottom, strokeColor=colors.HexColor(AXIS), strokeWidth=0.8))
    if principal and lo < principal < hi:
        d.add(Line(left, y(principal), left + pw, y(principal), strokeColor=colors.HexColor(INK2), strokeWidth=0.7,
                   strokeDashArray=[3, 2]))
        d.add(String(left + pw + 4, y(principal) - 3, "원금", fontName=font, fontSize=7, fillColor=colors.HexColor(INK2)))
    step = max(1, len(series) // 8)
    for i, s in enumerate(series):
        if i % step == 0 or i == len(series) - 1:
            d.add(String(x(i), bottom - 12, s["date"][5:].replace("-", "/"), fontName=font, fontSize=7,
                         fillColor=colors.HexColor(MUTED), textAnchor="middle"))
    pts = []
    for i, s in enumerate(series):
        pts += [x(i), y(s["value"])]
    if len(series) > 1:
        d.add(PolyLine(pts, strokeColor=colors.HexColor(SERIES1), strokeWidth=2, strokeLineJoin=1, strokeLineCap=1))
    for i, s in enumerate(series):
        d.add(Circle(x(i), y(s["value"]), 2.8, fillColor=colors.HexColor(SERIES1), strokeColor=colors.white,
                     strokeWidth=1))
    last = series[-1]
    d.add(String(x(len(series) - 1) + 6, y(last["value"]) + 4, f"{last['value'] / 10000:,.0f}만원", fontName=font,
                 fontSize=7.5, fillColor=colors.HexColor(INK)))
    return d


def _weight_chart(rows: list[dict], width: float, font: str, band: float):
    """목표 대비 현재 비중 (불릿 차트): 막대 = 현재, 세로 눈금 = 목표, 옅은 띠 = 허용 범위."""
    from reportlab.graphics.shapes import Drawing, Line, Rect, String
    from reportlab.lib import colors
    rows = [r for r in rows if r["target"] or r["value"]]
    bar_h, gap, top = 9, 9, 6
    height = top + len(rows) * (bar_h + gap) + 14
    d = Drawing(width, height)
    left, right = 124, 92
    pw = width - left - right
    mx = max([r["weight"] for r in rows] + [r["target"] + band for r in rows] + [10])
    mx = min(100, (int(mx / 10) + 1) * 10)

    def x(v):
        return left + pw * max(0, v) / mx

    for i, r in enumerate(rows):
        yb = height - top - (i + 1) * (bar_h + gap) + gap / 2
        d.add(String(left - 6, yb + 1.5, r["name"][:16], fontName=font, fontSize=7.5,
                     fillColor=colors.HexColor(INK2), textAnchor="end"))
        d.add(Rect(x(r["target"] - band), yb - 3, x(r["target"] + band) - x(r["target"] - band), bar_h + 6,
                   fillColor=colors.HexColor(SURFACE2), strokeColor=None))
        d.add(Rect(left, yb, max(0.5, x(r["weight"]) - left), bar_h, fillColor=colors.HexColor(SERIES1),
                   strokeColor=None, rx=2, ry=2))
        d.add(Line(x(r["target"]), yb - 3, x(r["target"]), yb + bar_h + 3, strokeColor=colors.HexColor(INK),
                   strokeWidth=1.6))
        d.add(String(left + pw + 6, yb + 1.5, f"{r['weight']:.1f}% / 목표 {r['target']:g}%", fontName=font,
                     fontSize=7.5, fillColor=colors.HexColor(INK)))
    for v in range(0, int(mx) + 1, 10 if mx <= 60 else 20):
        d.add(String(x(v), 2, f"{v}%", fontName=font, fontSize=6.5, fillColor=colors.HexColor(MUTED),
                     textAnchor="middle"))
    return d


def render_pdf(d: dict, max_orders: int | None = None, max_events: int | None = None) -> tuple[bytes, int]:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.platypus import (CondPageBreak, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
                                    Table, TableStyle)

    font, bold = fonts()
    W = A4[0] - 32 * mm
    st = {
        "title": ParagraphStyle("t", fontName=bold, fontSize=17, leading=22, textColor=colors.HexColor(INK)),
        "meta": ParagraphStyle("m", fontName=font, fontSize=8.5, leading=12, textColor=colors.HexColor(INK2)),
        "h2": ParagraphStyle("h2", fontName=bold, fontSize=12, leading=16, spaceBefore=10, spaceAfter=5,
                             textColor=colors.HexColor(INK)),
        "h3": ParagraphStyle("h3", fontName=bold, fontSize=9.5, leading=13, spaceBefore=6, spaceAfter=3,
                             textColor=colors.HexColor(INK)),
        "body": ParagraphStyle("b", fontName=font, fontSize=9, leading=13.5, textColor=colors.HexColor(INK)),
        "bullet": ParagraphStyle("bl", fontName=font, fontSize=9.2, leading=14, leftIndent=10, bulletIndent=0,
                                 textColor=colors.HexColor(INK), spaceAfter=2),
        "cell": ParagraphStyle("c", fontName=font, fontSize=7.8, leading=10.2, textColor=colors.HexColor(INK),
                               alignment=TA_LEFT),
        "small": ParagraphStyle("s", fontName=font, fontSize=7.2, leading=9.6, textColor=colors.HexColor(INK2)),
        "tl": ParagraphStyle("tl", fontName=font, fontSize=7.8, leading=10, textColor=colors.HexColor(INK2)),
        "tv": ParagraphStyle("tv", fontName=bold, fontSize=13, leading=17, textColor=colors.HexColor(INK)),
        "ts": ParagraphStyle("ts", fontName=font, fontSize=7.8, leading=10, textColor=colors.HexColor(INK2)),
    }
    P = Paragraph

    def table(header, rows, widths, num_cols=(), font_size=7.8):
        data = [[P(f"<b>{_clean(h)}</b>", st["cell"]) for h in header]] + rows
        t = Table(data, colWidths=widths, repeatRows=1)
        style = [("FONT", (0, 0), (-1, -1), font, font_size), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                 ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor(AXIS)),
                 ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.HexColor(LINE)),
                 ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                 ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4)]
        for c in num_cols:
            style.append(("ALIGN", (c, 1), (c, -1), "RIGHT"))
        t.setStyle(TableStyle(style))
        return t

    def cell(v, align_right=False):
        s = st["cell"] if not align_right else ParagraphStyle("cr", parent=st["cell"], alignment=2)
        return P(v if isinstance(v, str) and v.startswith("<font") else _clean(v), s)

    a, c = d["auto"], d["counts"]
    story = []
    # ── 1장: 핵심 요약 ──
    rng = d["start"] if d["start"] == d["end"] else f"{d['start']} ~ {d['end']}"
    story.append(P(f"장중대리 {d['label']} 보고서", st["title"]))
    story.append(P(_clean(f"{rng} · 생성 {d['generated_at']} · 계좌 {d['broker']}"), st["meta"]))
    state = "전체 중지" if d["halted"] else ("운용 중" if a and a.get("status") in ("active", "building") else
                                             "손실 한도로 중단" if a and a.get("status") == "halted_loss" else "감시 중")
    story.append(P(_clean(f"현재 상태: {state}") + (f' · <font color="{CRIT}">{_clean(d["halted"]["reason"])}</font>'
                                                     if d["halted"] else ""), st["meta"]))
    story.append(Spacer(1, 8))

    def tile(label, value, sub=""):
        return [P(_clean(label), st["tl"]), P(value, st["tv"]), P(sub, st["ts"])]

    tiles = [
        tile("자동 투자 평가액", _clean(_won(a["value"])) if a and a["value"] else "—",
             _clean(f"원금 {_won(a['principal'])}") if a and a["principal"] else "자동 투자 없음"),
        tile(f"{d['label']} 수익률", _signed(a["period_pct"]) if a else "—",
             _clean(f"누적 {_spct(a['cum_pct'])}") if a and a["cum_pct"] is not None else ""),
        tile("체결", _clean(f"{c['filled']}건"), _clean(f"매수 {c['buys']} · 매도 {c['sells']} · 실패 {c['failed']}")),
        tile("규칙 매매", _clean(f"{c['rule']}건 체결"), _clean(f"감시 중 {len(d['rules'])}개")),
    ]
    tw = W / 4
    tt = Table([[t[0] for t in tiles], [t[1] for t in tiles], [t[2] for t in tiles]], colWidths=[tw] * 4)
    tt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(SURFACE2)),
                            ("LINEAFTER", (0, 0), (-2, -1), 3, colors.white),
                            ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, 0), 7),
                            ("BOTTOMPADDING", (0, -1), (-1, -1), 7), ("TOPPADDING", (0, 1), (-1, -1), 1),
                            ("BOTTOMPADDING", (0, 0), (-1, -2), 1)]))
    story.append(tt)
    story.append(Spacer(1, 6))
    if len(d["series"]) >= 2:
        story.append(P("자동 투자 평가액 추이", st["h3"]))
        story.append(_line_chart(d["series"], W, 46 * mm, font))
    elif a and a.get("rows"):
        story.append(P("자동 투자 비중 — 목표 대비 (막대 = 현재, 세로선 = 목표, 옅은 띠 = 허용 범위)", st["h3"]))
        story.append(_weight_chart(a["rows"], W, font, d.get("band", 5)))
    story.append(P("핵심 요약", st["h2"]))
    for line in highlights(d):
        story.append(P(_clean(line).replace("▲", f'<font color="{UP}">▲</font>').replace("▼", f'<font color="{DOWN}">▼</font>'),
                       st["bullet"], bulletText="•"))
    story.append(P("확인할 일", st["h2"]))
    for line in d["attention"][:6] or ["없음 — 모든 주문이 규칙·전략대로 처리됐습니다."]:
        story.append(P(_clean(line), st["bullet"], bulletText="•"))
    story.append(P("다음 일정", st["h2"]))
    story.append(P(_clean(" · ".join(d["schedule"])), st["body"]))
    story.append(PageBreak())

    # ── 2장: 수익·자산 상세 ──
    story.append(P("수익·자산 상세", st["h2"]))
    if d["series"]:
        rows = [[cell(s["date"] + (" (현재)" if s.get("live") else "")), cell(_num(s["value"]), True),
                 cell(_signed(s["day_pct"]), True), cell(_signed(s["cum_pct"]), True),
                 cell("—" if s["rule_fills"] is None else f"{s['rule_fills']}건", True),
                 cell("—" if s["active_rules"] is None else f"{s['active_rules']}개", True)] for s in d["series"]]
        story.append(P("날짜별 자동 투자 평가액 (장 마감 요약 기준)", st["h3"]))
        story.append(table(["날짜", "평가액(원)", "전일 대비", "누적", "규칙 체결", "남은 규칙"], rows,
                           [W * 0.2, W * 0.2, W * 0.16, W * 0.16, W * 0.14, W * 0.14]))
    else:
        story.append(P("이 기간의 평가액 기록이 없습니다 (자동 투자 전이거나 장 마감 요약 전).", st["body"]))
    if d["trades"]:
        story.append(P("종목별 체결 요약", st["h3"]))
        rows = [[cell(t["name"]),
                 cell(f"{t['buy_n']}건 · {t['buy_q']:,}주" if t["buy_n"] else "—", True), cell(_num(t["buy_amt"] or None), True),
                 cell(f"{t['sell_n']}건 · {t['sell_q']:,}주" if t["sell_n"] else "—", True), cell(_num(t["sell_amt"] or None), True),
                 cell(_num(t["buy_amt"] - t["sell_amt"]), True)] for t in d["trades"]]
        story.append(table(["종목", "매수", "매수 금액(원)", "매도", "매도 금액(원)", "순매수(원)"], rows,
                           [W * 0.26, W * 0.14, W * 0.16, W * 0.14, W * 0.15, W * 0.15]))
    if a and a.get("rows"):
        story.append(P(f"자동 투자 구성 — {a['label']} {a['tier']}단계 ({d['as_of'] or ''})", st["h3"]))
        rows = [[cell(r["name"]), cell(f"{r['qty']:,}주", True), cell(_num(r["price"]), True), cell(_num(r["value"]), True),
                 cell(f"{r['weight']:.1f}%", True), cell(f"{r['target']:g}%", True), cell(f"{0 if abs(r['dev']) < 0.05 else r['dev']:+.1f}%p", True)]
                for r in a["rows"]]
        rows.append([cell("현금"), cell(""), cell(""), cell(_num(a["cash"]), True), cell(""), cell(""), cell("")])
        story.append(table(["종목", "수량", "현재가", "평가액(원)", "현재 비중", "목표", "차이"], rows,
                           [W * 0.28, W * 0.1, W * 0.12, W * 0.15, W * 0.12, W * 0.1, W * 0.13]))
        if len(d["series"]) >= 2:
            story.append(Spacer(1, 4))
            story.append(_weight_chart(a["rows"], W, font, d.get("band", 5)))
            story.append(P("막대 = 현재 비중, 세로선 = 목표, 옅은 띠 = 리밸런싱 허용 범위", st["small"]))
        if a.get("build") and a["build"][0] is not None and a["build"][0] < a["build"][1]:
            story.append(P(_clean(f"분할 매수 진행: {a['build'][0]}/{a['build'][1]}회"), st["small"]))
    elif a is None:
        story.append(P("자동 투자를 시작하지 않았습니다.", st["body"]))
    if d["own"] is not None:
        story.append(P(f"직접 보유 종목 (자동 투자 몫 제외 · 규칙 매매 대상 · {d['as_of'] or ''})", st["h3"]))
        if d["own"]:
            rows = [[cell(o["name"]), cell(f"{o['qty']:,}주", True), cell(_num(o["avg"]), True), cell(_num(o["price"]), True),
                     cell(_num(o["value"]), True), cell(_signed(o["ret"]), True)] for o in d["own"]]
            story.append(table(["종목", "수량", "평균 매수가", "현재가", "평가액(원)", "수익률"], rows,
                               [W * 0.3, W * 0.12, W * 0.15, W * 0.13, W * 0.16, W * 0.14]))
        else:
            story.append(P("없음", st["body"]))
    if not d["as_of"]:
        story.append(P("이 기간에는 장 마감 보유·비중 기록이 없어 구성 표를 생략합니다.", st["small"]))
    story.append(PageBreak())

    # ── 3장~: 주문 내역 ──
    orders = d["orders"]
    shown = orders if max_orders is None or len(orders) <= max_orders else orders[-max_orders:]
    story.append(P(f"주문 내역 ({len(orders)}건 · 근거 · 판단 시세 · 체결가)", st["h2"]))
    if shown:
        rows, spans = [], []
        for o in shown:
            side = "매수" if o["side"] == "buy" else "매도"
            stat = o["status_label"] + (f" {o['filled']}/{o['qty']}" if o["status"] == "partial" else "")
            if o["status"] in ("failed", "rejected"):
                stat = f'<font color="{CRIT}">{_clean(stat)}</font>'
            rows.append([cell(o["at"]), cell(o["source"]), cell(o["name"]), cell(side), cell(f"{o['qty']:,}", True),
                         cell(_num(o["decision"]), True), cell(_num(o["fill"]), True),
                         cell("—" if o["gap"] is None else f"{o['gap']:+.2f}%", True), cell(stat)])
            reason = "근거: " + o["reason"] + (f" · 오류: {o['error']}" if o["error"] else "")
            rows.append([P(_clean(reason), st["small"])] + [""] * 8)
            spans.append(len(rows))     # 헤더가 0행이므로 본문 행 번호 = len(rows)
        t = table(["일시", "구분", "종목", "방향", "수량", "판단가", "체결가", "괴리", "상태"], rows,
                  [W * 0.11, W * 0.13, W * 0.19, W * 0.07, W * 0.08, W * 0.11, W * 0.11, W * 0.09, W * 0.11])
        extra = []
        for rnum in spans:
            extra += [("SPAN", (0, rnum), (-1, rnum)), ("LINEBELOW", (0, rnum - 1), (-1, rnum - 1), 0.8, colors.white),
                      ("TOPPADDING", (0, rnum), (-1, rnum), 0), ("BOTTOMPADDING", (0, rnum), (-1, rnum), 5)]
        t.setStyle(TableStyle(extra))
        story.append(t)
        if len(shown) < len(orders):
            story.append(P(_clean(f"분량 제한으로 최근 {len(shown)}건만 표시 — 전체 {len(orders)}건은 첨부한 CSV에 있습니다."),
                           st["small"]))
    else:
        story.append(P("이 기간에는 주문이 없었습니다.", st["body"]))

    # 안전 장치 기록
    ev = d["events"]
    ev_shown = ev if max_events is None or len(ev) <= max_events else ev[-max_events:]
    block = [CondPageBreak(40 * mm), P(f"안전 장치 기록 ({len(ev)}건)", st["h2"])]
    if ev_shown:
        rows = [[cell(e["at"]), cell(e["kind"]), cell(e["detail"]), cell(e["outcome"])] for e in ev_shown]
        block.append(table(["일시", "구분", "내용", "처리"], rows, [W * 0.12, W * 0.22, W * 0.5, W * 0.16]))
        if len(ev_shown) < len(ev):
            block.append(P(_clean(f"최근 {len(ev_shown)}건만 표시"), st["small"]))
    else:
        block.append(P("전체 중지·시세 이상·한도 초과·손실 한도 등 안전 장치가 작동한 기록이 없습니다.", st["body"]))
    story += block

    # 규칙 현황 + 점검 + 설정
    story.append(CondPageBreak(35 * mm))
    story.append(P(f"규칙 현황 (보고서 생성 시점 · {len(d['rules'])}개)", st["h2"]))
    for line in d["rules"] or ["감시 중인 규칙이 없습니다."]:
        story.append(P(_clean(line), st["bullet"], bulletText="•"))
    story.append(KeepTogether([
        P("주문 점검", st["h2"]),
        P(_clean(f"점검 {c['orders']}건 · 규칙·전략과 다른 주문 {len(d['violations'])}건"
                 + (f" · 조건 충족→주문 최대 {c['max_lat']:.0f}초 (기준 60초)" if c["max_lat"] is not None else "")
                 + (f" · 체결가 괴리 최대 {c['max_abs_gap']:.2f}%" if c["max_abs_gap"] is not None else "")), st["body"]),
        *[P(_clean(v), st["bullet"], bulletText="!") for v in d["violations"][:10]],
    ]))
    lim_rows = [[cell(k), cell(v)] for k, v in d["limits"]]
    story.append(KeepTogether([P("현재 안전 설정", st["h2"]),
                               table(["항목", "값"], lim_rows, [W * 0.3, W * 0.7])]))
    if d["changes"]:
        story.append(P(_clean(f"이 기간 설정 변경 {len(d['changes'])}건: ")
                       + _clean(" / ".join(_first_line(ch["text"]) for ch in d["changes"][-5:])), st["small"]))
    story.append(Spacer(1, 8))
    story.append(P(f"{d['broker']} 기록으로 만든 보고서이며 투자 조언이 아닙니다. 주문은 등록된 규칙과 고정된 전략으로만 실행됩니다.",
                   st["small"]))

    label, rng_txt = d["label"], rng
    pages = {"n": 0}

    class NumberedCanvas(rl_canvas.Canvas):
        def __init__(self, *args, **kw):
            super().__init__(*args, **kw)
            self._saved = []

        def showPage(self):
            self._saved.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            n = len(self._saved)
            pages["n"] = n
            for s in self._saved:
                self.__dict__.update(s)
                self.setFont(font, 7.5)
                self.setFillColor(colors.HexColor(MUTED))
                self.drawString(16 * mm, 10 * mm, f"장중대리 {label} 보고서 · {rng_txt}")
                self.drawRightString(A4[0] - 16 * mm, 10 * mm, f"{self._pageNumber} / {n}")
                super().showPage()
            super().save()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm,
                            bottomMargin=16 * mm, title=f"장중대리 {label} 보고서 {rng_txt}", author="장중대리")
    doc.build(story, canvasmaker=NumberedCanvas)
    return buf.getvalue(), pages["n"]


@dataclass
class Report:
    period: str
    start: str
    end: str
    pdf: bytes
    pages: int
    csv: bytes | None
    summary: list[str] = field(default_factory=list)

    @property
    def filename(self) -> str:
        return f"jangjung_{self.period}_{self.end}.pdf"

    @property
    def title(self) -> str:
        rng = self.end if self.start == self.end else f"{self.start} ~ {self.end}"
        return f"{PERIODS[self.period]} 보고서 ({rng})"


async def build_report(app, period: str, end: str | None = None) -> Report:
    end = end or timeutil.today_str()
    date.fromisoformat(end)                                # 형식 검증
    data = await collect(app, period, end)
    data["band"] = app.autoinvest.cfg["rebalance_band_pp"]

    def render():
        pdf, n = render_pdf(data)
        for lim in (60, 40, 25, 15, 8, 4):                 # 5장 넘으면 표를 줄인다
            if n <= MAX_PAGES:
                break
            pdf, n = render_pdf(data, max_orders=lim, max_events=lim)
        return pdf, n

    pdf, n = await asyncio.to_thread(render)
    csv = orders_csv(app.db, data["start"], data["end"]) if data["orders"] else None
    return Report(period, data["start"], end, pdf, n, csv, highlights(data))


# ───────────────────────────── 메일 ─────────────────────────────
class MailError(RuntimeError):
    pass


def mail_config() -> dict:
    user = os.getenv("SMTP_USER", "").strip()
    return {"host": os.getenv("SMTP_HOST", "").strip(), "port": int(os.getenv("SMTP_PORT", "") or 587),
            "user": user, "password": os.getenv("SMTP_PASSWORD", ""),
            "to": [x.strip() for x in (os.getenv("REPORT_EMAIL_TO", "") or user).split(",") if x.strip()]}


def mail_ready() -> bool:
    c = mail_config()
    return bool(c["host"] and c["user"] and c["password"] and c["to"])


def mask_email(addr: str) -> str:
    name, _, dom = addr.partition("@")
    return (name[:2] + "***@" + dom) if dom else "***"


def send_mail(subject: str, body: str, attachments: list[tuple[str, bytes, str]]) -> list[str]:
    """SMTP로 보낸다 (465 = SSL, 그 외 = STARTTLS). 받는 사람 목록을 돌려준다."""
    c = mail_config()
    if not mail_ready():
        raise MailError("메일 설정이 없습니다 (SMTP_HOST · SMTP_USER · SMTP_PASSWORD · REPORT_EMAIL_TO)")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("장중대리", c["user"]))
    msg["To"] = ", ".join(c["to"])
    msg.set_content(body)
    for name, data, mime in attachments:
        main, sub = mime.split("/", 1)
        msg.add_attachment(data, maintype=main, subtype=sub, filename=name)
    ctx = ssl.create_default_context()
    try:
        if c["port"] == 465:
            with smtplib.SMTP_SSL(c["host"], c["port"], context=ctx, timeout=30) as s:
                s.login(c["user"], c["password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(c["host"], c["port"], timeout=30) as s:
                s.starttls(context=ctx)
                s.login(c["user"], c["password"])
                s.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise MailError("메일 로그인 실패 — Gmail은 '앱 비밀번호'가 필요합니다") from e
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"메일 전송 실패: {type(e).__name__}: {e}") from e
    return c["to"]


def telegram_target(app):
    """보고서를 보낼 텔레그램 채널 (봇 토큰·chat_id 가 있을 때만)."""
    inner = getattr(app.notifier, "inner", None)
    if inner is not None and hasattr(inner, "send_document") and getattr(inner, "chat_id", ""):
        return inner
    return None


def channels_for(app, channel: str) -> list[str]:
    """설정의 '받는 곳' 중 실제로 연결된 채널만."""
    want = ["email", "telegram"] if channel == "both" else [channel]
    out = []
    if "email" in want and mail_ready():
        out.append("email")
    if "telegram" in want and telegram_target(app):
        out.append("telegram")
    return out


async def deliver_reports(app, periods: list[str], end: str | None = None,
                          channels: list[str] | None = None) -> dict:
    """보고서들을 한 번 만들어 메일(한 통)·텔레그램(PDF 파일)으로 보낸다.
    한 채널이라도 성공하면 {"message", "errors"}, 모두 실패하면 MailError."""
    end = end or timeutil.today_str()
    channels = channels_for(app, app.settings.report.get("channel", "both")) if channels is None else channels
    if not channels:
        raise MailError("보낼 곳이 없습니다 — 메일(SMTP) 또는 텔레그램 봇을 연결하세요")
    reps = [await build_report(app, p, end) for p in periods]
    names = "·".join(PERIODS[r.period] for r in reps)
    md = end[5:].replace("-", "/")
    longest = max(reps, key=lambda r: date.fromisoformat(r.end) - date.fromisoformat(r.start))
    csv_att = (f"jangjung_orders_{longest.start}_{longest.end}.csv", longest.csv) if longest.csv else None
    sent, errors = [], []
    if "email" in channels:
        body = [f"장중대리 {names} 보고서입니다. ({BROKER_NAMES.get(app.broker.name, app.broker.name)} · 투자 조언 아님)", ""]
        for r in reps:
            body.append(f"■ {r.title} · PDF {r.pages}장")
            body += [f"  - {line}" for line in r.summary]
            body.append("")
        body.append("자세한 내용은 첨부 PDF를 확인하세요. 전체 주문은 CSV로 함께 첨부했습니다.")
        atts = [(r.filename, r.pdf, "application/pdf") for r in reps]
        if csv_att:
            atts.append((csv_att[0], csv_att[1], "text/csv"))
        try:
            to = await asyncio.to_thread(send_mail, f"[장중대리] {md} {names} 보고서", "\n".join(body), atts)
            sent.append("메일 " + ", ".join(mask_email(t) for t in to))
        except MailError as e:
            errors.append(f"메일: {e}")
    if "telegram" in channels:
        tg = telegram_target(app)
        try:
            if tg is None:
                raise MailError("텔레그램 봇이 연결되지 않았습니다")
            for r in reps:
                caption = f"장중대리 {r.title} · {r.pages}장\n" + "\n".join(f"- {line}" for line in r.summary)
                await tg.send_document(r.filename, r.pdf, caption[:1000])
            if csv_att:
                await tg.send_document(csv_att[0], csv_att[1], "전체 주문 (CSV)", mime="text/csv")
            sent.append("텔레그램")
        except Exception as e:  # noqa: BLE001
            errors.append(f"텔레그램: {e}")
    app.db.kv_set("report_mail_last", {"at": timeutil.now().isoformat(), "periods": periods, "end": end,
                                       "ok": not errors, "sent": sent, "error": "; ".join(errors)[:300] or None})
    if not sent:
        raise MailError("; ".join(errors))
    msg = f"{names} 보고서를 {' · '.join(sent)}(으)로 보냈습니다 (PDF {sum(r.pages for r in reps)}장)."
    if errors:
        msg += " 실패: " + "; ".join(errors)
    return {"message": msg, "errors": errors, "sent": sent}


async def email_reports(app, periods: list[str], end: str | None = None) -> str:
    """메일로만 보낸다 (결과 문장)."""
    res = await deliver_reports(app, periods, end, ["email"])
    return res["message"]
