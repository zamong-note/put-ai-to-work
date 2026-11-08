"""알림 채널. 텔레그램(운영) / 콘솔(로컬 시험) / 피드(웹 UI용 기록).

FeedNotifier 가 모든 메시지와 버튼 카드를 DB에 남기고, 실제 채널(텔레그램·콘솔)로도 전달한다.
그래서 같은 카드를 텔레그램에서 눌러도, 웹 UI에서 눌러도 된다 (먼저 누른 쪽만 실행).
"""
from __future__ import annotations

import itertools
import json
import logging
from typing import Protocol

from . import timeutil

log = logging.getLogger("jangjung")

Buttons = list[list[tuple[str, str]]]  # 행 단위 [(라벨, callback_data)]


class Notifier(Protocol):
    async def send(self, text: str, buttons: Buttons | None = None) -> str | None: ...
    async def clear_buttons(self, message_id: str | None, note: str | None = None) -> None: ...


class ConsoleNotifier:
    """버튼은 '/btn <data>' 명령으로 누른다."""

    def __init__(self):
        self._ids = itertools.count(1)
        self.sent: list[tuple[str, Buttons | None]] = []

    async def send(self, text, buttons=None):
        self.sent.append((text, buttons))
        mid = str(next(self._ids))
        print("\n" + "─" * 40 + f"\n📨 {text}")
        if buttons:
            for row in buttons:
                print("   " + "   ".join(f"[{lbl}] → /btn {data}" for lbl, data in row))
        print("─" * 40)
        return mid

    async def clear_buttons(self, message_id, note=None):
        if note:
            print(f"   (카드 {message_id} 처리됨: {note})")


class FeedNotifier:
    """category 가 있는 알림은 설정(notify)에서 끈 경우 텔레그램으로 보내지 않고 기록만 한다.
    확인 카드(버튼)와 category 없는 알림(안전 경고·실패)은 항상 보낸다."""

    def __init__(self, db, inner: Notifier | None = None, prefs: dict | None = None):
        self.db = db
        self.inner = inner
        self.prefs = prefs if prefs is not None else {}

    def record(self, role: str, text: str, via: str = "", buttons: Buttons | None = None) -> int:
        return self.db.x("INSERT INTO messages(ts,role,via,text,buttons) VALUES(?,?,?,?,?)",
                         (timeutil.now().isoformat(), role, via, text,
                          json.dumps(buttons, ensure_ascii=False) if buttons else None))

    def muted(self, category: str | None) -> bool:
        return bool(category) and self.prefs.get(category, "on") == "off"

    async def send(self, text, buttons=None, category: str | None = None):
        muted = not buttons and self.muted(category)
        fid = self.record("bot", text, via="muted" if muted else "", buttons=buttons)
        inner_id = None
        if self.inner is not None and not muted:
            try:
                inner_id = await self.inner.send(text, buttons)
            except Exception as e:  # noqa: BLE001 — 채널 장애가 매매를 막으면 안 된다
                log.error("알림 전송 실패: %s", e)
        return f"{fid}:{inner_id or ''}"

    async def clear_buttons(self, message_id, note=None):
        if not message_id:
            return
        fid, _, inner_id = str(message_id).partition(":")
        if fid.isdigit():
            self.db.x("UPDATE messages SET resolved=? WHERE id=?", (note or "처리됨", int(fid)))
        if self.inner is not None and inner_id:
            await self.inner.clear_buttons(inner_id, note)
