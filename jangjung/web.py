"""로컬 웹 대시보드 (http://127.0.0.1:8765).

보안: 이 화면으로 매매를 멈추거나 카드를 승인할 수 있으므로
- 기본은 127.0.0.1 에만 바인딩
- Host 헤더 검사 (DNS 리바인딩 방지)
- 모든 POST 에 실행마다 새로 만든 토큰(X-JJ-Token) 요구 → 다른 웹사이트가 몰래 요청 못 함 (CSRF 방지)
- --lan (휴대폰 접속) 모드: 같은 Wi-Fi 에 열리므로 모든 요청에 PIN 로그인 세션 필요,
  Host 는 IP 주소/localhost 만 허용, PIN 5회 틀리면 5분 잠금
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import timeutil
from .broker.base import BrokerError
from .pdfreport import (PERIODS, MailError, build_report, deliver_reports, mail_config, mail_ready, mask_email,
                        send_mail, telegram_target)
from .report import build_daily, orders_csv, report_dates, to_csv, to_html
from .rules import cond_text, qty_text
from .settings_store import SECTION_LABEL, SettingsError, SettingsStore

log = logging.getLogger("jangjung")
STATIC = Path(__file__).parent / "web_static"


class Dashboard:
    def __init__(self, app, port: int = 8765, lan: bool = False, pin: str | None = None):
        self.app = app
        self.port = port
        self.lan = lan
        self.pin = pin
        if lan and not (pin and pin.isdigit() and 6 <= len(pin) <= 12):
            raise ValueError("LAN 모드에는 6~12자리 숫자 PIN이 필요합니다")
        self.token = secrets.token_urlsafe(24)
        self.sessions: set[str] = set()
        self._fails: list[float] = []
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = asyncio.Lock()
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.store = app.store

    def _host_ok(self, host: str | None) -> bool:
        if not host:
            return False
        if host in self.allowed_hosts:
            return True
        if not self.lan:
            return False
        name, _, port = host.rpartition(":")
        if port != str(self.port):
            return False
        try:                                           # LAN: IP 주소로 들어온 요청만 (도메인 = 리바인딩 의심)
            ipaddress.ip_address(name.strip("[]"))
            return True
        except ValueError:
            return False

    def _authed(self, request: Request) -> bool:
        if not self.lan:
            return True
        return request.cookies.get("jj_session") in self.sessions

    # ── 공통 ──
    async def _cached(self, key: str, ttl: float, fn):
        async with self._lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
            val = await fn()
            self._cache[key] = (time.monotonic(), val)
            return val

    def invalidate(self):
        self._cache.clear()

    def _guard(self, request: Request, write: bool, public: bool = False) -> Response | None:
        host = request.headers.get("host")
        if not self._host_ok(host):
            return JSONResponse({"error": "허용되지 않은 Host"}, 403)
        if not public and not self._authed(request):
            if request.url.path == "/":
                return RedirectResponse("/login", 303)
            return JSONResponse({"error": "로그인이 필요합니다"}, 401)
        if write:
            if request.headers.get("x-jj-token") != self.token:
                return JSONResponse({"error": "토큰 불일치 — 페이지를 새로고침하세요"}, 403)
            origin = request.headers.get("origin")
            if origin and origin.split("//", 1)[-1] != host:
                return JSONResponse({"error": "다른 사이트에서 온 요청"}, 403)
        return None

    def routes(self):
        def r(path, fn, methods=("GET",), public=False, csrf=True):
            write = "POST" in methods and csrf

            async def handler(request: Request):
                if bad := self._guard(request, write, public):
                    return bad
                try:
                    return await fn(request)
                except BrokerError as e:
                    return JSONResponse({"error": f"증권사 API 오류: {e}"}, 502)
                except SettingsError as e:
                    return JSONResponse({"error": str(e)}, 400)
                except Exception as e:  # noqa: BLE001
                    log.exception("web handler")
                    return JSONResponse({"error": f"{type(e).__name__}: {e}"}, 500)
            return Route(path, handler, methods=list(methods))

        return [
            r("/", self.index),
            r("/api/status", self.status),
            r("/api/portfolio", self.portfolio),
            r("/api/feed", self.feed),
            r("/api/rules", self.rules),
            r("/api/orders", self.orders),
            r("/api/settings", self.settings),
            r("/api/reports", self.reports),
            r("/api/report/daily", self.report_daily),
            r("/api/report/pdf", self.report_pdf),
            r("/api/report/email", self.report_email, ("POST",)),
            r("/api/report/send", self.report_send, ("POST",)),
            r("/api/settings/backup", self.settings_backup),
            r("/api/settings/restore", self.settings_restore, ("POST",)),
            r("/api/check/email", self.check_email, ("POST",)),
            r("/api/export/orders.csv", self.export_orders),
            r("/api/export/daily.csv", self.export_daily),
            r("/api/chat", self.chat, ("POST",)),
            r("/api/button", self.button, ("POST",)),
            r("/api/stop", self.stop, ("POST",)),
            r("/api/resume", self.resume, ("POST",)),
            r("/api/mode", self.mode, ("POST",)),
            r("/api/run/{job}", self.run_job, ("POST",)),
            r("/api/check/broker", self.check_broker, ("POST",)),
            r("/api/check/telegram", self.check_telegram, ("POST",)),
            r("/api/check/sheets", self.check_sheets, ("POST",)),
            r("/api/settings/update", self.settings_update, ("POST",)),
            r("/api/settings/reset", self.settings_reset, ("POST",)),
            r("/api/env/update", self.env_update, ("POST",)),
            r("/login", self.login_page, public=True),
            r("/login", self.login_post, ("POST",), public=True, csrf=False),
            r("/logout", self.logout, ("POST",)),
            r("/manifest.webmanifest", self.manifest, public=True),
            r("/icon.svg", self.icon, public=True),
        ]

    def asgi(self) -> Starlette:
        return Starlette(routes=self.routes())

    # ── 페이지 ──
    async def index(self, request):
        html = (STATIC / "index.html").read_text(encoding="utf-8").replace("__JJ_TOKEN__", self.token)
        return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                           "Referrer-Policy": "no-referrer"})

    # ── 읽기 ──
    async def status(self, request):
        a = self.app
        eng = a.settings.engine
        now = timeutil.now()
        st = a.autoinvest.state()
        return JSONResponse({
            "now": now.strftime("%Y-%m-%d %H:%M:%S"),
            "broker": a.broker.name,
            "in_session": timeutil.in_session(now, a.settings.holidays, eng["session_start"], eng["session_end"]),
            "trading_day": timeutil.is_trading_day(now.date(), a.settings.holidays),
            "halted": a.control.halted(),
            "suspended": {s: dict(v, name=a.universe.name(s)) for s, v in a.control.suspended().items()},
            "active_rules": a.rules.active_count(),
            "autoinvest": {"tier": st["tier"], "status": st["status"]} if st else None,
            "open_cards": len(a.pending.open_items()),
            "engine": {"started": a.engine.started, "last_step_at": a.engine.last_step_at},
            "restart_required": sorted(self.store.restart_required),
            "lan": self.lan,
        })

    async def portfolio(self, request):
        async def load():
            a = self.app
            acct = await a.broker.get_account()
            val = await a.autoinvest.valuation()
            auto_pos = a.gateway.ledger.positions()
            own = []
            for h in acct.holdings.values():
                q = h.qty - auto_pos.get(h.symbol, {}).get("qty", 0)
                if q > 0:
                    own.append({"symbol": h.symbol, "name": h.name, "qty": q, "avg_price": h.avg_price,
                                "price": h.cur_price, "value": q * h.cur_price,
                                "ret_pct": (h.cur_price / h.avg_price - 1) * 100 if h.avg_price else None})
            snaps = a.db.q("SELECT date, auto_value, auto_principal FROM daily_snapshots "
                           "WHERE auto_value IS NOT NULL ORDER BY date DESC LIMIT 90")[::-1]
            n, buy_sum = a.gateway.today_stats()
            today = timeutil.today_str()
            fills = a.db.one("SELECT COUNT(*) n FROM orders WHERE status IN ('filled','partial') "
                             "AND substr(created_at,1,10)=?", (today,))["n"]
            auto = None
            if val:
                stt = val["state"]
                prev = next((s["auto_value"] for s in reversed(snaps) if s["date"] < today), None)
                base = prev or stt["principal"]
                auto = {"tier": stt["tier"], "label": a.universe.tier(stt["tier"])["label"], "status": stt["status"],
                        "principal": stt["principal"], "total": val["total"], "cash": val["cash"],
                        "pnl_pct": val["pnl_pct"], "day_pct": (val["total"] / base - 1) * 100 if base else None,
                        "loss_limit_pct": a.universe.tier(stt["tier"])["loss_limit_pct"],
                        "build": [stt["tranches_done"], stt["tranches_total"]],
                        "band_pp": a.autoinvest.cfg["rebalance_band_pp"],
                        "rows": [{k: r[k] for k in ("symbol", "name", "qty", "price", "value", "weight", "target", "dev")}
                                 for r in val["rows"]]}
            return {"account": {"cash": acct.cash, "total_eval": acct.total_eval}, "own": own, "auto": auto,
                    "snapshots": snaps, "today": {"orders": n, "fills": fills, "buy_sum": buy_sum,
                                                  "max_orders": a.settings.safety["max_daily_orders"],
                                                  "max_buy": a.settings.safety["max_daily_buy_amount"]},
                    "as_of": timeutil.now().strftime("%H:%M:%S")}
        return JSONResponse(await self._cached("portfolio", 10, load))

    async def feed(self, request):
        after = int(request.query_params.get("after", 0))
        rows = self.app.db.q("SELECT * FROM messages WHERE id>? ORDER BY id DESC LIMIT 200", (after,))[::-1]
        # 이미 받은 카드의 처리 상태 변화도 알려 준다
        resolved = self.app.db.q("SELECT id, resolved FROM messages WHERE buttons IS NOT NULL AND resolved IS NOT NULL "
                                 "ORDER BY id DESC LIMIT 100")
        return JSONResponse({"messages": rows, "resolved": resolved})

    async def rules(self, request):
        rows = self.app.rules.list(include_inactive=True)
        return JSONResponse({"rules": [dict(r, cond=cond_text(r["condition"], r["threshold"]),
                                            qty=qty_text(r["side"], r["qty_mode"], r["qty_value"])) for r in rows]})

    async def orders(self, request):
        rows = self.app.db.q("SELECT id,created_at,detected_at,source,ref_id,name,symbol,side,qty,decision_price,"
                             "filled_qty,avg_fill_price,status,reason,error,broker_order_no FROM orders "
                             "ORDER BY id DESC LIMIT 200")
        return JSONResponse({"orders": rows})

    async def settings(self, request):
        a, s = self.app, self.app.settings
        mpath = s.data_dir / "stock_master.json"
        upcoming = sorted(d for d in s.holidays if d >= timeutil.today_str())[:3]
        acct = s.kis_account
        return JSONResponse({
            "broker": {"mode": s.broker, "name": a.broker.name, "kis_keys": bool(s.kis_app_key and s.kis_app_secret),
                       "account": (acct[:4] + "****") if acct else "", "api_errors": a.control.api_errors,
                       "last_ok": a.control.last_api_ok, "last_error": a.control.last_api_error,
                       "token_cached": (s.data_dir / "kis_token.json").exists()},
            "telegram": {"token": bool(s.telegram_token), "chat_id": bool(s.telegram_chat_id),
                         "running": a.telegram_running},
            "sheets": {"enabled": a.journal.sheets_enabled, "error": a.journal.sheets_error},
            "telegram_reports": telegram_target(a) is not None,
            "email": {"ready": mail_ready(), "host": mail_config()["host"],
                      "to": [mask_email(t) for t in mail_config()["to"]], "last": a.db.kv_get("report_mail_last"),
                      "report": {k: v for k, v in s.report.items() if not k.startswith("_")}},
            "claude": {"model": s.claude_model,
                       "auth": "ANTHROPIC_API_KEY" if os.getenv("ANTHROPIC_API_KEY") else "Claude Code 로그인",
                       "connected": bool(a._agent and a._agent.client)},
            "master": {"count": len(a.universe.master), "file": mpath.exists(),
                       "updated": time.strftime("%Y-%m-%d", time.localtime(mpath.stat().st_mtime)) if mpath.exists() else None},
            "engine": {k: v for k, v in s.engine.items() if not k.startswith("_")},
            "safety": {k: v for k, v in s.safety.items() if not k.startswith("_")},
            "autoinvest": {k: v for k, v in s.autoinvest.items() if not k.startswith("_")},
            "rebalance_mode": a.autoinvest.rebalance_mode(),
            "schema": self.store.describe(a.autoinvest.rebalance_mode()),
            "section_labels": SECTION_LABEL,
            "env": self.store.env_describe(),
            "env_editable": not s.extra.get("demo"),
            "restart_required": sorted(self.store.restart_required),
            "lan": self.lan,
            "holidays_upcoming": upcoming,
            "data_dir": str(s.data_dir),
            "config_files": ["config/settings.json", "config/strategies.json", ".env"],
        })

    @staticmethod
    def _period_args(period: str, date: str) -> str | None:
        if period not in PERIODS:
            return "period=daily|weekly|monthly"
        try:
            from datetime import date as _d
            _d.fromisoformat(date)
        except ValueError:
            return "date=YYYY-MM-DD"
        return None

    async def report_pdf(self, request):
        period = request.query_params.get("period", "daily")
        date = request.query_params.get("date") or timeutil.today_str()
        if err := self._period_args(period, date):
            return JSONResponse({"error": err}, 400)
        rep = await build_report(self.app, period, date)
        disp = "attachment" if request.query_params.get("download") else "inline"
        return Response(rep.pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f'{disp}; filename="{rep.filename}"',
                                 "X-Report-Pages": str(rep.pages), "Cache-Control": "no-store"})

    async def report_email(self, request):
        return await self.report_send(request, channel="email")

    async def report_send(self, request, channel: str | None = None):
        """지금 보고서 보내기. channel: email | telegram (기본: 설정의 '받는 곳')."""
        body = await self._body(request)
        periods = body.get("periods") or [body.get("period", "daily")]
        date = str(body.get("date") or timeutil.today_str())
        periods = [p for p in PERIODS if p in periods]         # 순서 고정 · 중복 제거
        for p in periods or ["?"]:
            if err := self._period_args(p, date):
                return JSONResponse({"error": err}, 400)
        channel = channel or str(body.get("channel") or self.app.settings.report.get("channel", "both"))
        if channel not in ("email", "telegram", "both"):
            return JSONResponse({"error": "channel=email|telegram|both"}, 400)
        if channel == "email" and not mail_ready():
            return JSONResponse({"error": "메일 설정이 없습니다 — 설정 탭 > 연결 설정에서 SMTP 항목을 입력하세요"}, 400)
        if channel == "telegram" and telegram_target(self.app) is None:
            return JSONResponse({"error": "텔레그램 봇이 연결되어 있지 않습니다 (봇 토큰·chat_id 입력 후 다시 시작)"}, 400)
        from .pdfreport import channels_for
        chans = channels_for(self.app, channel)
        if not chans:
            return JSONResponse({"error": "보낼 곳이 없습니다 — 메일(SMTP) 또는 텔레그램 봇을 연결하세요"}, 400)
        try:
            res = await deliver_reports(self.app, periods, date, chans)
        except MailError as e:
            return JSONResponse({"error": str(e)}, 400)
        await self.app.notifier.send(f"[보고서 발송] {res['message']} (웹에서 요청)",
                                     category=None if res["errors"] else "report")
        return JSONResponse({"ok": True, "message": res["message"]})

    # ── 설정 백업 / 복구 ──
    async def settings_backup(self, request):
        data = self.store.export(self.app.autoinvest.rebalance_mode())
        name = f"jangjung_settings_{timeutil.now().strftime('%Y%m%d_%H%M')}.json"
        return Response(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
                        media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})

    async def settings_restore(self, request):
        if int(request.headers.get("content-length") or 0) > 256_000:
            return JSONResponse({"error": "파일이 너무 큽니다 (256KB 이하)"}, 413)
        body = await self._body(request)
        apply = bool(body.get("apply"))
        env_ok = not self.app.settings.extra.get("demo")
        res = self.store.restore(body.get("data"), self.app.autoinvest, apply=apply, env_ok=env_ok)
        rows = [{"section": sec, "label": lbl, "old": self._fmt(o), "new": self._fmt(n)} for sec, lbl, o, n in res["changes"]]
        if apply and rows:
            if any(r["section"] == "연결 설정" for r in rows):
                await self._apply_live_env()
            lines = [f"· {r['section']} · {r['label']}: {r['old']} → {r['new']}" for r in rows[:20]]
            more = f"\n… 외 {len(rows) - 20}개" if len(rows) > 20 else ""
            await self.app.notifier.send(f"[설정 복구] 백업 파일에서 {len(rows)}개 항목 복구\n" + "\n".join(lines) + more,
                                         category="settings")
            self.invalidate()
        return JSONResponse({"ok": True, "applied": apply, "changes": rows, "ignored": res["ignored"],
                             "env_skipped": res["env_skipped"]})

    async def check_email(self, request):
        if not mail_ready():
            return JSONResponse({"error": "메일 서버 · 계정 · 비밀번호를 먼저 넣으세요"}, 400)
        try:
            to = await asyncio.to_thread(send_mail, "[장중대리] 메일 연결 테스트",
                                         "장중대리 보고서 메일 연결 테스트입니다. 이 메일이 보이면 설정이 맞습니다.", [])
        except MailError as e:
            return JSONResponse({"error": str(e)}, 400)
        return JSONResponse({"ok": True, "message": "테스트 메일 보냄 → " + ", ".join(mask_email(t) for t in to)})

    async def reports(self, request):
        rep = {k: v for k, v in self.app.settings.report.items() if not k.startswith("_")}
        return JSONResponse({"dates": report_dates(self.app.db), "today": timeutil.today_str(),
                             "telegram_ready": telegram_target(self.app) is not None,
                             "email": {"ready": mail_ready(), "to": [mask_email(t) for t in mail_config()["to"]],
                                       "report": rep, "last": self.app.db.kv_get("report_mail_last")}})

    async def report_daily(self, request):
        date = request.query_params.get("date") or timeutil.today_str()
        if len(date) != 10 or date[4] != "-" or date[7] != "-":
            return JSONResponse({"error": "date=YYYY-MM-DD"}, 400)
        fmt = request.query_params.get("format", "html")
        live = None
        if date == timeutil.today_str():
            try:
                live = await self.app.autoinvest.valuation()
            except BrokerError:
                live = None
        rep = build_daily(self.app, date, live)
        name = f"jangjung_daily_{date}"
        if fmt == "csv":
            return Response(to_csv(rep), media_type="text/csv; charset=utf-8",
                            headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
        if fmt == "json":
            return JSONResponse(rep)
        disp = "attachment" if request.query_params.get("download") else "inline"
        return HTMLResponse(to_html(rep), headers={"Content-Disposition": f'{disp}; filename="{name}.html"'})

    async def export_orders(self, request):
        return Response(orders_csv(self.app.db), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="jangjung_orders.csv"'})

    async def export_daily(self, request):
        p = self.app.journal.summary_csv
        data = p.read_bytes() if p.exists() else "날짜\n".encode("utf-8-sig")
        return Response(data, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="jangjung_daily_summary.csv"'})

    # ── 쓰기 ──
    async def _body(self, request) -> dict:
        try:
            return await request.json()
        except Exception:  # noqa: BLE001
            return {}

    async def chat(self, request):
        text = str((await self._body(request)).get("text", "")).strip()[:1000]
        if not text:
            return JSONResponse({"error": "빈 메시지"}, 400)
        reply = await self.app.handle_text(text, via="web")
        if reply:
            self.app.notifier.record("bot", reply, via="web")
        self.invalidate()
        return JSONResponse({"ok": True})

    async def button(self, request):
        data = str((await self._body(request)).get("data", ""))
        reply = await self.app.handle_button(data)
        if reply:
            await self.app.notifier.send(reply)
        self.invalidate()
        return JSONResponse({"ok": True, "reply": reply})

    async def stop(self, request):
        msg = await self.app.emergency_stop("웹 화면에서 '멈춰'")
        self.app.notifier.record("user", "멈춰", via="web")
        await self.app.notifier.send(msg)
        return JSONResponse({"ok": True, "message": msg})

    async def resume(self, request):
        msg = await self.app.request_resume()
        return JSONResponse({"ok": True, "message": msg})

    async def mode(self, request):
        m = str((await self._body(request)).get("mode", ""))
        self.app.autoinvest.set_rebalance_mode(m)
        await self.app.notifier.send(f"리밸런싱 방식을 '{'자동 실행' if m == 'auto' else '매번 확인'}'으로 바꿨습니다.")
        return JSONResponse({"ok": True, "mode": m})

    async def run_job(self, request):
        job = request.path_params["job"]
        a = self.app
        if job == "tick":
            await a.engine.tick()
            res = "감시 1회 실행"
        elif job == "build":
            res = await a.autoinvest.build_step()
        elif job == "rebalance":
            res = await a.autoinvest.monthly_check(force=True)
        elif job == "summary":
            await a.engine.daily_summary()
            res = "장 마감 요약 생성"
        else:
            return JSONResponse({"error": "알 수 없는 작업"}, 400)
        self.invalidate()
        return JSONResponse({"ok": True, "result": res})

    async def check_broker(self, request):
        a = self.app
        t0 = time.monotonic()
        q = await a.broker.get_quote("005930")
        acct = await a.broker.get_account()
        a.control.last_api_ok = timeutil.now().isoformat()
        return JSONResponse({"ok": True, "ms": int((time.monotonic() - t0) * 1000),
                             "quote": f"삼성전자 {q.price:,.0f}원 ({q.change_pct:+.2f}%)",
                             "account": f"현금 {acct.cash:,.0f}원 · 보유 {len(acct.holdings)}종목"})

    # ── 설정 변경 ──
    def _fmt(self, v) -> str:
        if v is None:
            return "해제"
        if isinstance(v, list):
            return ", ".join(map(str, v)) if len(v) <= 6 else f"{len(v)}개"
        if isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) >= 10000:
            return f"{v:,.0f}"
        return str(v)

    async def settings_update(self, request):
        body = await self._body(request)
        section = str(body.get("section", ""))
        changes = self.store.update(section, body.get("values") or {}, self.app.autoinvest)
        if changes:
            lines = [f"· {lbl}: {self._fmt(o)} → {self._fmt(n)}" for lbl, o, n in changes]
            await self.app.notifier.send(f"[설정 변경 · {SECTION_LABEL.get(section, section)}] 웹 화면에서 변경\n"
                                         + "\n".join(lines), category="settings")
        self.invalidate()
        return JSONResponse({"ok": True, "changed": len(changes)})

    async def settings_reset(self, request):
        section = str((await self._body(request)).get("section", ""))
        if section not in SECTION_LABEL:
            return JSONResponse({"error": "알 수 없는 설정 구역"}, 400)
        self.store.reset(section)
        await self.app.notifier.send(f"[설정 변경 · {SECTION_LABEL[section]}] 기본값으로 되돌림", category="settings")
        self.invalidate()
        return JSONResponse({"ok": True})

    async def env_update(self, request):
        s = self.app.settings
        if s.extra.get("demo"):
            return JSONResponse({"error": "데모 모드에서는 연결 설정을 바꿀 수 없습니다"}, 400)
        body = await self._body(request)
        changed, restart = self.store.update_env(body.get("set") or {}, body.get("clear") or [])
        if not changed:
            return JSONResponse({"ok": True, "changed": [], "restart": []})
        await self._apply_live_env()
        msg = "[연결 설정 변경] " + ", ".join(changed) + " (값은 기록하지 않음)"
        if restart:
            msg += "\n다시 시작해야 적용: " + ", ".join(restart)
        await self.app.notifier.send(msg, category="settings")
        self.invalidate()
        return JSONResponse({"ok": True, "changed": changed, "restart": restart})

    async def _apply_live_env(self) -> None:
        """다시 시작하지 않아도 되는 연결 설정을 바로 반영."""
        s = self.app.settings
        s.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        inner = getattr(self.app.notifier, "inner", None)
        if inner is not None and hasattr(inner, "chat_id"):
            inner.chat_id = s.telegram_chat_id
        s.google_sa_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")
        s.google_sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
        j = self.app.journal
        j.sa_file, j.sheet_id, j._sheet, j.sheets_error = s.google_sa_file, s.google_sheet_id, None, None
        s.claude_model = os.getenv("CLAUDE_MODEL", "") or "claude-opus-5-5"
        if self.app._agent:
            await self.app._agent.reset()

    async def check_telegram(self, request):
        import httpx
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat = os.getenv("TELEGRAM_CHAT_ID", "")
        if not token:
            return JSONResponse({"error": "봇 토큰이 없습니다"}, 400)
        async with httpx.AsyncClient(timeout=10) as c:
            me = (await c.get(f"https://api.telegram.org/bot{token}/getMe")).json()
            if not me.get("ok"):
                return JSONResponse({"error": f"토큰 확인 실패: {me.get('description')}"}, 400)
            sent = False
            if chat:
                r = (await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                  json={"chat_id": chat, "text": "장중대리 연결 테스트 메시지입니다."})).json()
                sent = bool(r.get("ok"))
                if not sent:
                    return JSONResponse({"error": f"봇 @{me['result']['username']} 확인됨, 메시지 전송 실패: "
                                                  f"{r.get('description')}"}, 400)
        return JSONResponse({"ok": True, "message": f"봇 @{me['result']['username']} 확인"
                                                    + (" · 테스트 메시지 보냄" if sent else " · chat_id 없음")})

    async def check_sheets(self, request):
        j = self.app.journal
        if not j.sheets_enabled:
            return JSONResponse({"error": "서비스 계정 경로와 시트 ID를 먼저 넣으세요"}, 400)

        def probe():
            import gspread
            sh = gspread.service_account(filename=j.sa_file).open_by_key(j.sheet_id)
            return sh.title, [w.title for w in sh.worksheets()]
        try:
            title, tabs = await asyncio.to_thread(probe)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"시트 접근 실패: {type(e).__name__}: {e}"}, 400)
        return JSONResponse({"ok": True, "message": f"'{title}' 접근 확인 · 탭 {', '.join(tabs)}"})

    # ── 로그인 (LAN 모드) ──
    async def login_page(self, request):
        if not self.lan or self._authed(request):
            return RedirectResponse("/", 303)
        return HTMLResponse(LOGIN_HTML, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})

    async def login_post(self, request):
        if not self.lan:
            return RedirectResponse("/", 303)
        now = time.monotonic()
        self._fails = [t for t in self._fails if now - t < 300]
        if len(self._fails) >= 5:
            return HTMLResponse(LOGIN_HTML.replace("<!--ERR-->", "<p class='err'>5회 틀려 5분간 잠겼습니다.</p>"), 429)
        form = await request.form()
        pin = str(form.get("pin", ""))
        if not hmac.compare_digest(pin.encode(), (self.pin or "").encode()):
            self._fails.append(now)
            await asyncio.sleep(1)
            return HTMLResponse(LOGIN_HTML.replace(
                "<!--ERR-->", f"<p class='err'>PIN이 맞지 않습니다 ({len(self._fails)}/5)</p>"), 401)
        sid = secrets.token_urlsafe(32)
        self.sessions.add(sid)
        resp = RedirectResponse("/", 303)
        resp.set_cookie("jj_session", sid, httponly=True, samesite="strict", max_age=12 * 3600)
        return resp

    async def logout(self, request):
        self.sessions.discard(request.cookies.get("jj_session"))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie("jj_session")
        return resp

    async def manifest(self, request):
        return JSONResponse({"name": "장중대리", "short_name": "장중대리", "start_url": "/", "display": "standalone",
                             "background_color": "#f9f9f7", "theme_color": "#2a78d6",
                             "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}]},
                            media_type="application/manifest+json")

    async def icon(self, request):
        return Response(ICON_SVG, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#2a78d6"/>
<path d="M14 42 L26 30 L34 37 L50 20" fill="none" stroke="#fff" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
<circle cx="50" cy="20" r="4" fill="#fff"/></svg>"""

LOGIN_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>장중대리 로그인</title>
<link rel="icon" href="/icon.svg"><meta name="theme-color" content="#2a78d6">
<style>
:root{color-scheme:light dark;--bg:#f9f9f7;--card:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--line:rgba(11,11,11,.12);--acc:#2a78d6}
@media (prefers-color-scheme:dark){:root{--bg:#0d0d0d;--card:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--line:rgba(255,255,255,.12);--acc:#3987e5}}
body{margin:0;min-height:100dvh;display:grid;place-items:center;background:var(--bg);color:var(--ink);
font:16px/1.5 system-ui,-apple-system,"Segoe UI","Malgun Gothic",sans-serif;padding:16px;box-sizing:border-box}
form{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:28px 22px;width:100%;max-width:340px;box-sizing:border-box}
h1{font-size:20px;margin:0 0 4px}p{color:var(--ink2);font-size:14px;margin:0 0 18px}
input{width:100%;box-sizing:border-box;font-size:24px;letter-spacing:6px;text-align:center;padding:12px;border:1px solid var(--line);
border-radius:12px;background:transparent;color:inherit}
button{margin-top:14px;width:100%;padding:13px;font-size:16px;font-weight:600;border:0;border-radius:12px;background:var(--acc);color:#fff}
.err{color:#d03b3b;margin:12px 0 0}
</style></head><body><form method="post" action="/login">
<h1>장중대리</h1><p>PC 터미널에 표시된 PIN을 입력하세요.</p>
<input name="pin" type="password" inputmode="numeric" pattern="[0-9]*" autocomplete="current-password" autofocus required minlength="6" maxlength="12">
<!--ERR--><button type="submit">들어가기</button></form></body></html>"""


def lan_ip() -> str | None:
    """이 PC의 사설 IP (패킷을 보내지 않고 라우팅 테이블만 조회)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
    except OSError:
        return None


async def serve(app, port: int = 8765, lan: bool = False, pin: str | None = None) -> None:
    import uvicorn
    dash = Dashboard(app, port, lan=lan, pin=pin)
    host = "0.0.0.0" if lan else "127.0.0.1"
    config = uvicorn.Config(dash.asgi(), host=host, port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    print(f"\n  장중대리 대시보드 → http://127.0.0.1:{port}")
    if lan:
        ip = lan_ip()
        print(f"  휴대폰(같은 Wi-Fi) → http://{ip or '<이 PC의 IP>'}:{port}")
        print(f"  접속 PIN: {pin}   ※ 집 Wi-Fi처럼 믿을 수 있는 네트워크에서만 쓰세요 (HTTP 평문)")
    print()
    await server.serve()
