"""텔레그램 Bot API (long polling). 등록된 chat_id 한 곳에서 온 메시지만 처리한다."""
from __future__ import annotations

import asyncio
import logging

import httpx

from .notify import Buttons

log = logging.getLogger("jangjung")


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.http = httpx.AsyncClient(timeout=40)

    async def call(self, method: str, **payload):
        r = await self.http.post(f"{self.base}/{method}", json=payload)
        d = r.json()
        if not d.get("ok"):
            raise RuntimeError(f"Telegram {method} 실패: {d.get('description')}")
        return d["result"]

    async def send(self, text: str, buttons: Buttons | None = None) -> str | None:
        if not self.chat_id:
            log.warning("TELEGRAM_CHAT_ID 미설정 — 메시지 보류: %s", text[:80])
            return None
        payload = {"chat_id": self.chat_id, "text": text[:4000]}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": l, "callback_data": d} for l, d in row]
                                                           for row in buttons]}
        try:
            res = await self.call("sendMessage", **payload)
            return str(res["message_id"])
        except Exception as e:  # noqa: BLE001
            log.error("텔레그램 전송 실패: %s", e)
            return None

    async def send_document(self, filename: str, data: bytes, caption: str = "",
                            mime: str = "application/pdf") -> None:
        """PDF·CSV 파일 전송 (보고서)."""
        if not self.chat_id:
            raise RuntimeError("TELEGRAM_CHAT_ID 미설정")
        r = await self.http.post(f"{self.base}/sendDocument", data={"chat_id": self.chat_id, "caption": caption[:1000]},
                                 files={"document": (filename, data, mime)}, timeout=60)
        d = r.json()
        if not d.get("ok"):
            raise RuntimeError(f"파일 전송 실패: {d.get('description')}")

    async def clear_buttons(self, message_id: str | None, note: str | None = None) -> None:
        if not message_id or not self.chat_id:
            return
        try:
            await self.call("editMessageReplyMarkup", chat_id=self.chat_id, message_id=int(message_id),
                            reply_markup={"inline_keyboard": []})
        except Exception:  # noqa: BLE001
            pass


BOT_COMMANDS = [
    ("status", "전체 상태"), ("rules", "감시 중인 규칙"), ("portfolio", "자동 투자 현황"),
    ("journal", "최근 주문 5건"), ("report", "PDF 보고서 받기: /report weekly"),
    ("alerts", "알림 설정 보기·바꾸기: /alerts 체결 off"), ("strategy", "AI 투자 점검: /strategy run"),
    ("fx", "환율: /fx update"), ("summary", "지금 장 마감 요약"),
    ("mode", "리밸런싱 방식: /mode auto|confirm"), ("stop", "모든 매매 즉시 중지"), ("resume", "중지 해제"),
    ("new", "대화 초기화"), ("help", "사용법"),
]


class TelegramBot:
    def __init__(self, notifier: TelegramNotifier, app):
        self.n = notifier
        self.app = app
        self.offset = 0

    def _allowed(self, chat_id) -> bool:
        if not self.n.chat_id:
            print(f"\n>>> 텔레그램 chat_id = {chat_id}  — .env 의 TELEGRAM_CHAT_ID 에 넣고 다시 실행하세요.\n")
            return False
        return str(chat_id) == str(self.n.chat_id)

    async def run(self) -> None:
        log.info("텔레그램 봇 폴링 시작")
        self.app.telegram_running = True
        try:                                   # 텔레그램 입력창의 '/' 메뉴에 명령어 목록 표시
            await self.n.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in BOT_COMMANDS])
        except Exception as e:  # noqa: BLE001
            log.warning("명령어 메뉴 등록 실패: %s", e)
        while True:
            try:
                updates = await self.n.call("getUpdates", offset=self.offset, timeout=30,
                                            allowed_updates=["message", "callback_query"])
            except Exception as e:  # noqa: BLE001
                log.warning("getUpdates 실패: %s", e)
                await asyncio.sleep(5)
                continue
            for u in updates:
                self.offset = u["update_id"] + 1
                asyncio.create_task(self._handle(u))

    async def _handle(self, u: dict) -> None:
        try:
            if cq := u.get("callback_query"):
                if not self._allowed(cq["message"]["chat"]["id"]):
                    return
                await self.n.call("answerCallbackQuery", callback_query_id=cq["id"], text="처리 중…")
                reply = await self.app.handle_button(cq.get("data", ""))
                if reply:
                    await self.app.notifier.send(reply)
            elif msg := u.get("message"):
                if not self._allowed(msg["chat"]["id"]) or "text" not in msg:
                    return
                text = msg["text"]
                if not self.app.is_command_or_stop(text):
                    await self.n.call("sendChatAction", chat_id=self.n.chat_id, action="typing")
                reply = await self.app.handle_text(text, via="telegram")
                if reply:
                    await self.app.notifier.send(reply)
        except Exception:  # noqa: BLE001
            log.exception("update 처리 실패")
