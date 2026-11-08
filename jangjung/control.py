"""전체 중지 스위치('멈춰'), 종목별 일시중단, 시세 이상 감지."""
from __future__ import annotations

from datetime import datetime

from . import timeutil
from .broker.base import Quote
from .db import DB


class Control:
    def __init__(self, db: DB, safety: dict):
        self.db = db
        self.safety = safety
        self.api_errors = 0
        self.last_api_error: tuple[str, str] | None = None   # (시각, 메시지)
        self.last_api_ok: str | None = None
        self._last_px: dict[str, tuple[float, datetime]] = {}

    # ── 전체 중지 ──
    def halted(self) -> dict | None:
        return self.db.kv_get("halt")

    def halt(self, reason: str, by: str = "user") -> None:
        if not self.halted():
            self.db.kv_set("halt", {"reason": reason, "by": by, "at": timeutil.now().isoformat()})

    def resume(self) -> None:
        self.db.kv_del("halt")
        self.api_errors = 0

    # ── 종목별 중단 ──
    def suspended(self) -> dict:
        return self.db.kv_get("suspended", {})

    def suspend(self, symbol: str, reason: str) -> bool:
        s = self.suspended()
        if symbol in s:
            return False
        s[symbol] = {"reason": reason, "at": timeutil.now().isoformat()}
        self.db.kv_set("suspended", s)
        return True

    def unsuspend(self, symbol: str) -> None:
        s = self.suspended()
        s.pop(symbol, None)
        self.db.kv_set("suspended", s)
        self._last_px.pop(symbol, None)

    # ── 시세 이상 ──
    def quote_anomaly(self, q: Quote) -> str | None:
        """주문을 막아야 하는 시세 상태면 이유를 돌려준다."""
        if q.halted:
            return "거래정지/임시정지 종목"
        if q.status_code in self.safety.get("blocked_status_codes", []):
            return f"종목상태코드 {q.status_code} (투자위험·단기과열·거래정지 계열)"
        lim = self.safety["max_change_pct_from_prev_close"]
        if abs(q.change_pct) >= lim:
            return f"전일 대비 {q.change_pct:+.1f}% 급등락 (한도 ±{lim}%)"
        prev = self._last_px.get(q.symbol)
        now = q.ts or timeutil.now()
        self._last_px[q.symbol] = (q.price, now)
        if prev:
            p0, t0 = prev
            if (now - t0).total_seconds() <= self.safety["tick_jump_window_sec"] and p0 > 0:
                jump = (q.price / p0 - 1) * 100
                if abs(jump) >= self.safety["max_tick_jump_pct"]:
                    return f"단시간 {jump:+.1f}% 가격 급변 ({p0:,.0f}→{q.price:,.0f})"
        return None
