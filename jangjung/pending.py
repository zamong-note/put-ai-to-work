"""사용자 확인 카드. 등록·변경·첫 구성·한도 초과·손실한도 등은 반드시 여기를 거친다.

LLM은 카드를 '만들' 수만 있고, 승인은 사용자의 버튼 클릭(코드 경로)으로만 일어난다.
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from . import timeutil
from .db import DB
from .notify import Notifier

log = logging.getLogger("jangjung")
Handler = Callable[[str, dict, str], Awaitable[str]]


class PendingStore:
    def __init__(self, db: DB, notifier: Notifier):
        self.db = db
        self.notifier = notifier
        self.handlers: dict[str, Handler] = {}

    def register(self, kind: str, handler: Handler) -> None:
        self.handlers[kind] = handler

    async def create(self, kind: str, payload: dict, text: str, actions: list[tuple[str, str]],
                     ttl_min: int = 24 * 60) -> str:
        pid = "P" + secrets.token_hex(3).upper()
        now = timeutil.now()
        exp = now + timedelta(minutes=ttl_min)
        self.db.x("INSERT INTO pending(id,kind,payload,status,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                  (pid, kind, json.dumps(payload, ensure_ascii=False), "open", now.isoformat(), exp.isoformat()))
        rows, row = [], []
        for label, action in actions:
            row.append((label, f"pd|{pid}|{action}"))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        mid = await self.notifier.send(f"{text}\n(확인번호 {pid})", rows)
        self.db.x("UPDATE pending SET message_id=? WHERE id=?", (mid, pid))
        return pid

    async def resolve(self, pid: str, action: str) -> str:
        p = self.db.one("SELECT * FROM pending WHERE id=?", (pid,))
        if not p:
            return "없는 확인번호입니다."
        if p["status"] != "open":
            return f"이미 처리된 확인입니다 ({p['result'] or p['status']})."
        if p["expires_at"] and datetime.fromisoformat(p["expires_at"]) < timeutil.now():
            self.db.x("UPDATE pending SET status='expired' WHERE id=?", (pid,))
            await self.notifier.clear_buttons(p["message_id"], "만료")
            return "확인 시간이 지나 만료되었습니다. 다시 요청해 주세요."
        # 두 번 눌러도 한 번만 실행되도록 먼저 닫는다
        self.db.x("UPDATE pending SET status='done', result=? WHERE id=? AND status='open'", (action, pid))
        handler = self.handlers.get(p["kind"])
        if not handler:
            return f"처리기 없음: {p['kind']}"
        try:
            msg = await handler(action, json.loads(p["payload"]), pid)
        except Exception as e:  # noqa: BLE001
            log.exception("pending handler failed")
            msg = f"처리 중 오류: {e}"
        self.db.x("UPDATE pending SET result=? WHERE id=?", (f"{action}: {msg[:200]}", pid))
        await self.notifier.clear_buttons(p["message_id"], action)
        return msg

    def open_items(self, kind: str | None = None) -> list[dict]:
        if kind:
            return self.db.q("SELECT * FROM pending WHERE status='open' AND kind=?", (kind,))
        return self.db.q("SELECT * FROM pending WHERE status='open'")

    async def expire_old(self) -> None:
        now = timeutil.now()
        for p in self.open_items():
            if p["expires_at"] and datetime.fromisoformat(p["expires_at"]) < now:
                self.db.x("UPDATE pending SET status='expired' WHERE id=?", (p["id"],))
                await self.notifier.clear_buttons(p["message_id"], "만료")
                handler = self.handlers.get(p["kind"])
                if handler:
                    try:
                        await handler("expired", json.loads(p["payload"]), p["id"])
                    except Exception:  # noqa: BLE001
                        log.exception("expire handler failed")
