"""일일보고서: 특정 날짜의 수익 상태·주문(근거/판단시세/체결가)·규칙 현황·감사 결과.

- CSV  : 엑셀에서 바로 열리도록 UTF-8 BOM
- HTML : 브라우저에서 열어 인쇄 → PDF 저장 가능한 단독 문서
"""
from __future__ import annotations

import csv
import html
import io
import json

from . import timeutil
from .audit import audit
from .rules import rule_line

SOURCE = {"rule": "규칙매매", "auto_build": "자동투자 구성", "rebalance": "리밸런싱"}
STATUS = {"filled": "체결", "partial": "부분체결", "submitted": "미체결", "failed": "실패", "rejected": "거부",
          "submitting": "접수중"}


def report_dates(db) -> list[str]:
    rows = db.q("SELECT date d FROM daily_snapshots UNION SELECT substr(created_at,1,10) d FROM orders "
                "ORDER BY d DESC LIMIT 120")
    dates = [r["d"] for r in rows]
    today = timeutil.today_str()
    if today not in dates:
        dates.insert(0, today)
    return dates


def build_daily(app, date: str, live: dict | None = None) -> dict:
    db = app.db
    snap = db.one("SELECT * FROM daily_snapshots WHERE date=?", (date,))
    prev = db.one("SELECT * FROM daily_snapshots WHERE date<? AND auto_value IS NOT NULL ORDER BY date DESC LIMIT 1",
                  (date,))
    orders = db.q("SELECT * FROM orders WHERE substr(created_at,1,10)=? ORDER BY id", (date,))
    rows = []
    for o in orders:
        gap = None
        if o["avg_fill_price"] and o["decision_price"]:
            gap = (o["avg_fill_price"] / o["decision_price"] - 1) * 100
        rows.append({"time": o["created_at"][11:19], "source": SOURCE.get(o["source"], o["source"]),
                     "ref": o["ref_id"], "name": o["name"], "symbol": o["symbol"],
                     "side": "매수" if o["side"] == "buy" else "매도", "qty": o["qty"], "reason": o["reason"],
                     "decision_price": o["decision_price"], "filled_qty": o["filled_qty"],
                     "fill_price": o["avg_fill_price"], "gap_pct": gap, "status": STATUS.get(o["status"], o["status"]),
                     "order_no": o["broker_order_no"] or "", "error": o["error"] or ""})
    auto_value = snap["auto_value"] if snap else None
    principal = snap["auto_principal"] if snap else None
    holdings = None
    if live and date == timeutil.today_str():
        auto_value = live["total"]
        principal = live["state"]["principal"]
        holdings = [{"name": r["name"], "qty": r["qty"], "price": r["price"], "value": r["value"],
                     "weight": r["weight"], "target": r["target"], "dev": r["dev"]} for r in live["rows"]]
    base = (prev or {}).get("auto_value") or principal
    # 감사: 그날 주문만
    full = audit(db, app.universe)
    ids = {f"주문#{o['id']} " for o in orders}
    day_viol = [v for v in full["violations"] if any(v.startswith(i) for i in ids)]
    return {
        "date": date, "generated_at": timeutil.now().strftime("%Y-%m-%d %H:%M"), "broker": app.broker.name,
        "auto": {"value": auto_value, "principal": principal,
                 "cum_pct": (auto_value / principal - 1) * 100 if auto_value and principal else None,
                 "day_pct": (auto_value / base - 1) * 100 if auto_value and base else None,
                 "tier": (app.autoinvest.state() or {}).get("tier")},
        "counts": {"orders": len(orders), "filled": sum(1 for o in orders if o["status"] in ("filled", "partial")),
                   "rule_filled": sum(1 for o in orders if o["source"] == "rule" and o["status"] in ("filled", "partial")),
                   "failed": sum(1 for o in orders if o["status"] in ("failed", "rejected")),
                   "active_rules": snap["active_rules"] if snap else app.rules.active_count()},
        "orders": rows, "holdings": holdings,
        "rules": [rule_line(r) for r in app.rules.list()],
        "audit": {"violations": day_viol, "checked": len(orders)},
        "halted": app.control.halted(),
    }


def _won(v) -> str:
    return "" if v is None else f"{v:,.0f}"


def _pct(v) -> str:
    return "" if v is None else f"{v:+.2f}"


def to_csv(r: dict) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"장중대리 일일보고서 {r['date']}", f"생성 {r['generated_at']}", f"브로커 {r['broker']}"])
    w.writerow([])
    a, c = r["auto"], r["counts"]
    w.writerow(["[요약]"])
    w.writerow(["자동투자 단계", "평가액", "원금", "누적(%)", "당일(%)", "주문", "체결", "규칙 체결", "실패", "남은 규칙"])
    w.writerow([a["tier"] or "", _won(a["value"]), _won(a["principal"]), _pct(a["cum_pct"]), _pct(a["day_pct"]),
                c["orders"], c["filled"], c["rule_filled"], c["failed"], c["active_rules"]])
    w.writerow([])
    w.writerow(["[주문 내역]"])
    w.writerow(["시각", "구분", "규칙/전략", "종목", "코드", "매수/매도", "주문수량", "근거", "판단시세", "체결수량",
                "평균체결가", "괴리(%)", "상태", "주문번호", "오류"])
    for o in r["orders"]:
        w.writerow([o["time"], o["source"], o["ref"], o["name"], o["symbol"], o["side"], o["qty"], o["reason"],
                    _won(o["decision_price"]), o["filled_qty"], _won(o["fill_price"]), _pct(o["gap_pct"]),
                    o["status"], o["order_no"], o["error"]])
    if r["holdings"]:
        w.writerow([])
        w.writerow(["[자동투자 비중]"])
        w.writerow(["종목", "수량", "현재가", "평가액", "현재 비중(%)", "목표(%)", "차이(%p)"])
        for h in r["holdings"]:
            w.writerow([h["name"], h["qty"], _won(h["price"]), _won(h["value"]), f"{h['weight']:.1f}",
                        f"{h['target']:g}", f"{h['dev']:+.1f}"])
    w.writerow([])
    w.writerow(["[규칙 현황]"])
    for line in r["rules"] or ["감시 중인 규칙 없음"]:
        w.writerow([line])
    w.writerow([])
    w.writerow(["[주문 감사]", f"점검 {r['audit']['checked']}건", f"규칙·전략과 다른 주문 {len(r['audit']['violations'])}건"])
    for v in r["audit"]["violations"]:
        w.writerow([v])
    return buf.getvalue().encode("utf-8-sig")


def to_html(r: dict) -> str:
    e = html.escape
    a, c = r["auto"], r["counts"]

    def sign_cls(v):
        return "" if v is None else ("up" if v > 0 else "down" if v < 0 else "")

    def arrow(v):
        return "" if v is None else ("▲ " if v > 0 else "▼ " if v < 0 else "")

    def tile(label, value, cls=""):
        return f'<div class="tile"><div class="l">{e(label)}</div><div class="v {cls}">{e(value)}</div></div>'

    tiles = "".join([
        tile("자동투자 평가액", f"{_won(a['value'])}원" if a["value"] else "—"),
        tile("누적 수익률", f"{arrow(a['cum_pct'])}{_pct(a['cum_pct'])}%" if a["cum_pct"] is not None else "—", sign_cls(a["cum_pct"])),
        tile("당일 수익률", f"{arrow(a['day_pct'])}{_pct(a['day_pct'])}%" if a["day_pct"] is not None else "—", sign_cls(a["day_pct"])),
        tile("체결", f"{c['filled']}건 (규칙 {c['rule_filled']})"),
        tile("남은 규칙", f"{c['active_rules']}개"),
    ])
    orows = "".join(
        f"<tr><td class='nw'>{e(o['time'])}</td><td class='nw'>{e(o['source'])}</td><td>{e(o['name'])}</td><td class='nw'>{e(o['side'])}</td>"
        f"<td class='n'>{o['qty']}</td><td>{e(o['reason'])}</td><td class='n'>{_won(o['decision_price'])}</td>"
        f"<td class='n'>{_won(o['fill_price'])}</td><td class='n'>{_pct(o['gap_pct'])}</td><td class='nw'>{e(o['status'])}</td></tr>"
        for o in r["orders"]) or "<tr><td colspan='10' class='empty'>주문 없음</td></tr>"
    hold = ""
    if r["holdings"]:
        hold = "<h2>자동투자 비중</h2><table><thead><tr><th>종목</th><th>수량</th><th>평가액</th><th>현재</th><th>목표</th><th>차이</th></tr></thead><tbody>" + "".join(
            f"<tr><td>{e(h['name'])}</td><td class='n'>{h['qty']}</td><td class='n'>{_won(h['value'])}</td>"
            f"<td class='n'>{h['weight']:.1f}%</td><td class='n'>{h['target']:g}%</td><td class='n'>{h['dev']:+.1f}%p</td></tr>"
            for h in r["holdings"]) + "</tbody></table>"
    rules = "".join(f"<li>{e(x)}</li>" for x in r["rules"]) or "<li>감시 중인 규칙 없음</li>"
    viol = r["audit"]["violations"]
    audit_html = (f"<p class='ok'>✓ 점검 {r['audit']['checked']}건 · 규칙·전략과 다른 주문 0건</p>" if not viol else
                  f"<p class='bad'>⚠ 규칙·전략과 다른 주문 {len(viol)}건</p><ul>" + "".join(f"<li>{e(v)}</li>" for v in viol) + "</ul>")
    halt = f"<p class='bad'>⛔ 전체 중지 상태: {e(r['halted']['reason'])}</p>" if r["halted"] else ""
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>장중대리 일일보고서 {e(r['date'])}</title>
<style>
body{{font-family:system-ui,-apple-system,"Segoe UI","Malgun Gothic",sans-serif;color:#0b0b0b;background:#fff;margin:32px auto;max-width:1000px;padding:0 16px;line-height:1.5}}
h1{{font-size:22px;margin:0}} h2{{font-size:16px;margin:28px 0 8px}} .meta{{color:#52514e;font-size:13px;margin:4px 0 20px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}
.tile{{border:1px solid #e1e0d9;border-radius:10px;padding:12px}} .tile .l{{font-size:12px;color:#52514e}} .tile .v{{font-size:20px;font-weight:600}}
.up{{color:#d03b3b}} .down{{color:#2a78d6}}
table{{width:100%;border-collapse:collapse;font-size:12.5px}} th,td{{border-bottom:1px solid #e1e0d9;padding:6px 6px;text-align:left;vertical-align:top}}
th{{color:#52514e;font-weight:600;white-space:nowrap}} td.nw{{white-space:nowrap}} td.n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}} .empty{{color:#898781;text-align:center}}
.ok{{color:#006300}} .bad{{color:#d03b3b}} ul{{padding-left:18px;font-size:13px}}
@media print{{body{{margin:0}} .noprint{{display:none}}}}
</style></head><body>
<h1>장중대리 일일보고서 · {e(r['date'])}</h1>
<div class="meta">생성 {e(r['generated_at'])} · 브로커 {e(r['broker'])} · 자동투자 {e(str(a['tier']) + '단계' if a['tier'] else '없음')} · 상승 빨강 ▲ / 하락 파랑 ▼</div>
{halt}<div class="tiles">{tiles}</div>
<h2>주문 내역 (근거 · 판단 시세 · 체결가)</h2>
<table><thead><tr><th>시각</th><th>구분</th><th>종목</th><th>방향</th><th>수량</th><th>근거</th><th>판단시세</th><th>체결가</th><th>괴리%</th><th>상태</th></tr></thead><tbody>{orows}</tbody></table>
{hold}<h2>규칙 현황 (보고서 생성 시점)</h2><ul>{rules}</ul>
<h2>주문 감사</h2>{audit_html}
<p class="noprint meta">브라우저의 인쇄(Ctrl+P) → 'PDF로 저장'으로 PDF를 만들 수 있습니다. 모의투자 기록이며 투자 조언이 아닙니다.</p>
</body></html>"""


def orders_csv(db, start: str | None = None, end: str | None = None) -> bytes:
    """전체 매매일지 CSV. start/end(YYYY-MM-DD)를 주면 그 기간만."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["주문시각", "조건감지시각", "구분", "규칙/전략", "종목코드", "종목명", "매수/매도", "주문수량", "근거",
                "판단시세", "체결수량", "평균체결가", "괴리(%)", "상태", "주문번호", "오류", "근거데이터"])
    if start and end:
        rows = db.q("SELECT * FROM orders WHERE substr(created_at,1,10) BETWEEN ? AND ? ORDER BY id", (start, end))
    else:
        rows = db.q("SELECT * FROM orders ORDER BY id")
    for o in rows:
        gap = _pct((o["avg_fill_price"] / o["decision_price"] - 1) * 100) if o["avg_fill_price"] else ""
        w.writerow([o["created_at"][:19], (o["detected_at"] or "")[:19], SOURCE.get(o["source"], o["source"]),
                    o["ref_id"], o["symbol"], o["name"], "매수" if o["side"] == "buy" else "매도", o["qty"], o["reason"],
                    o["decision_price"], o["filled_qty"], o["avg_fill_price"] or "", gap,
                    STATUS.get(o["status"], o["status"]), o["broker_order_no"] or "", o["error"] or "",
                    json.dumps(json.loads(o["evidence"] or "{}"), ensure_ascii=False)])
    return buf.getvalue().encode("utf-8-sig")
