"""KST 시각, 거래일/장 시간 판단. 테스트를 위해 시계를 바꿀 수 있다."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
_fixed: datetime | None = None


def set_clock(dt: datetime | None) -> None:
    global _fixed
    _fixed = dt


def now() -> datetime:
    return _fixed if _fixed is not None else datetime.now(KST)


def today_str() -> str:
    return now().strftime("%Y-%m-%d")


def parse_hm(s: str) -> time:
    parts = [int(p) for p in s.split(":")]
    while len(parts) < 3:
        parts.append(0)
    return time(parts[0], parts[1], parts[2])


def is_trading_day(d: date, holidays: set[str]) -> bool:
    return d.weekday() < 5 and d.strftime("%Y-%m-%d") not in holidays


def in_session(dt: datetime, holidays: set[str], start: str, end: str) -> bool:
    if not is_trading_day(dt.date(), holidays):
        return False
    return parse_hm(start) <= dt.time() < parse_hm(end)


def first_trading_day_of_month(d: date, holidays: set[str]) -> date:
    x = d.replace(day=1)
    while not is_trading_day(x, holidays):
        x += timedelta(days=1)
    return x


def fmt_won(v: float) -> str:
    return f"{int(round(v)):,}원"
