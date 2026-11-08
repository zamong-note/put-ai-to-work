from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


class BrokerError(Exception):
    """API 오류. 엔진은 이 예외가 나면 주문을 멈추고 알린다."""


class BrokerRejected(BrokerError):
    """증권사가 요청을 명시적으로 거절 (주문이면 접수되지 않은 것이 확실)."""


class BrokerNetworkError(BrokerError):
    """통신 실패. 주문이면 접수 여부를 알 수 없음 → 재시도 금지, 사람 확인 필요."""


@dataclass
class Quote:
    symbol: str
    price: float
    prev_close: float
    change_pct: float
    halted: bool = False
    status_code: str = "00"
    ts: datetime | None = None


@dataclass
class Holding:
    symbol: str
    name: str
    qty: int
    avg_price: float
    cur_price: float


@dataclass
class Account:
    cash: float
    holdings: dict[str, Holding] = field(default_factory=dict)
    total_eval: float = 0.0


@dataclass
class OrderResult:
    ok: bool
    order_no: str = ""
    message: str = ""


@dataclass
class FillStatus:
    order_no: str
    filled_qty: int
    avg_price: float | None
    open_qty: int


class Broker(Protocol):
    name: str

    async def get_quote(self, symbol: str) -> Quote: ...
    async def get_account(self) -> Account: ...
    async def place_order(self, symbol: str, side: str, qty: int, order_type: str, price: float | None) -> OrderResult: ...
    async def get_fill(self, order_no: str, symbol: str) -> FillStatus: ...
    async def close(self) -> None: ...
