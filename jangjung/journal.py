"""매매일지: 로컬 CSV(항상) + Google Sheets(설정 시).

한 줄에서 '근거 · 판단 당시 시세 · 체결가'를 바로 대조할 수 있게 기록한다.
"""
from __future__ import annotations

import asyncio
import csv
import logging
from pathlib import Path

log = logging.getLogger("jangjung")

ORDER_HEADER = ["주문시각", "조건감지시각", "구분", "규칙/전략", "종목코드", "종목명", "매수/매도", "주문수량",
                "근거", "판단시세", "체결수량", "평균체결가", "괴리(%)", "상태", "주문번호", "오류"]
SUMMARY_HEADER = ["날짜", "자동투자 평가액", "자동투자 원금", "자동투자 누적(%)", "자동투자 당일(%)",
                  "규칙 체결", "남은 규칙", "중지 상태"]


class Journal:
    def __init__(self, data_dir: Path, sa_file: str = "", sheet_id: str = ""):
        self.orders_csv = data_dir / "journal_orders.csv"
        self.summary_csv = data_dir / "journal_daily.csv"
        self.sa_file, self.sheet_id = sa_file, sheet_id
        self._sheet = None
        self.sheets_error: str | None = None

    @property
    def sheets_enabled(self) -> bool:
        return bool(self.sa_file and self.sheet_id)

    def _ws(self, title: str, header: list[str]):
        import gspread
        if self._sheet is None:
            gc = gspread.service_account(filename=self.sa_file)
            self._sheet = gc.open_by_key(self.sheet_id)
        try:
            ws = self._sheet.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self._sheet.add_worksheet(title=title, rows=1000, cols=len(header))
            ws.append_row(header)
        return ws

    @staticmethod
    def _csv(path: Path, header: list[str], row: list) -> None:
        new = not path.exists()
        with open(path, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new:
                w.writerow(header)
            w.writerow(row)

    async def _append(self, path: Path, title: str, header: list[str], row: list) -> None:
        self._csv(path, header, row)
        if not self.sheets_enabled:
            return
        try:
            await asyncio.to_thread(lambda: self._ws(title, header).append_row(
                [str(v) if v is not None else "" for v in row], value_input_option="USER_ENTERED"))
            self.sheets_error = None
        except Exception as e:  # noqa: BLE001 — 시트 장애가 매매를 막으면 안 된다
            self.sheets_error = str(e)
            log.warning("Google Sheets 기록 실패(CSV에는 기록됨): %s", e)

    async def log_order(self, o: dict, source_label: str) -> None:
        gap = ""
        if o.get("avg_fill_price") and o.get("decision_price"):
            gap = f"{(o['avg_fill_price'] / o['decision_price'] - 1) * 100:+.2f}"
        row = [o["created_at"][:19], (o.get("detected_at") or "")[:19], source_label, o.get("ref_id") or "",
               o["symbol"], o["name"], "매수" if o["side"] == "buy" else "매도", o["qty"], o["reason"],
               o["decision_price"], o.get("filled_qty") or 0, o.get("avg_fill_price") or "", gap, o["status"],
               o.get("broker_order_no") or "", o.get("error") or ""]
        await self._append(self.orders_csv, "매매일지", ORDER_HEADER, row)

    async def log_summary(self, row: list) -> None:
        await self._append(self.summary_csv, "일일요약", SUMMARY_HEADER, row)
