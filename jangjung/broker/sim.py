"""오프라인 시뮬레이터 브로커. KIS 키 없이 전체 흐름을 시험할 때 쓴다.

- 가격: 기준가 주변 랜덤워크. data/sim_overrides.json 으로 특정 종목 가격/거래정지를 강제할 수 있다
  예) {"000660": 230000, "005930": {"price": 70000, "halted": true}}
- 체결: 시장가는 즉시 현재가 체결, 지정가는 현재가보다 유리하면 현재가로 체결
- 해외 종목('NASD:AAPL'): 현지 통화 가격, 매수 대금은 원화 현금에서 근사 환율로 차감 (통합증거금 가정)
"""
from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path

from .. import markets, timeutil
from ..db import DB
from .base import Account, BrokerError, FillStatus, Holding, OrderResult, Quote

BASE_PRICES = {
    "069500": 40000, "360750": 22000, "133690": 140000, "114260": 64000, "153130": 112000,
    "459580": 105000, "279530": 15000, "091160": 45000, "266420": 9000, "005930": 70000,
    "000660": 200000, "005380": 230000, "105560": 85000, "035420": 200000, "373220": 350000,
    "207940": 1000000, "012450": 600000,
}


BASE_PRICES_OVERSEAS = {
    "NASD:AAPL": 230, "NASD:MSFT": 420, "NASD:NVDA": 140, "NASD:AMZN": 200, "NASD:GOOGL": 170, "NASD:META": 560,
    "NASD:TSLA": 250, "NASD:QQQ": 500, "AMEX:SPY": 570, "AMEX:VOO": 520, "NYSE:JPM": 220, "NYSE:KO": 70,
    "NASD:TQQQ": 75, "NASD:SQQQ": 30, "TKSE:7203": 2800, "TKSE:6758": 3300, "TKSE:9984": 9000,
    "SHAA:600519": 1500, "SZAA:300750": 250, "SZAA:000858": 130, "SEHK:00700": 400, "SEHK:09988": 90,
    "SEHK:03690": 150, "VNSE:VNM": 65000, "VNSE:VCB": 90000, "VNSE:FPT": 130000,
}


def _tick(price: float, symbol: str = "") -> float:
    if markets.is_overseas(symbol):
        st = markets.tick(symbol, price)
        return round(max(st, round(price / st) * st), 3)
    """한국 주식 호가단위 근사."""
    step = 1 if price < 2000 else 5 if price < 5000 else 10 if price < 20000 else 50 if price < 50000 \
        else 100 if price < 200000 else 500 if price < 500000 else 1000
    return max(step, round(price / step) * step)


class SimBroker:
    name = "sim"

    def __init__(self, db: DB, names, cfg: dict, data_dir: Path, overseas_cfg: dict | None = None):
        self.db = db
        self.overseas_cfg = overseas_cfg if overseas_cfg is not None else {}
        self.names = names
        self.vol = cfg.get("volatility_pct", 0.15) / 100
        self.override_path = data_dir / "sim_overrides.json"
        self.fail_next = 0  # 테스트용: 다음 N번 호출을 API 오류로
        st = db.kv_get("sim_state")
        if st is None:
            st = {"cash": cfg["initial_cash"], "holdings": cfg.get("initial_holdings", {}), "prices": {},
                  "orders": {}, "seq": 0}
            db.kv_set("sim_state", st)

    def _st(self) -> dict:
        return self.db.kv_get("sim_state")

    def _save(self, st: dict) -> None:
        self.db.kv_set("sim_state", st)

    def _overrides(self) -> dict:
        if self.override_path.exists():
            try:
                return json.loads(self.override_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
        return {}

    def _check_fail(self):
        if self.fail_next > 0:
            self.fail_next -= 1
            raise BrokerError("SIM 강제 API 오류")

    async def get_quote(self, symbol: str) -> Quote:
        self._check_fail()
        st = self._st()
        today = timeutil.today_str()
        p = st["prices"].get(symbol)
        if p is None or p.get("date") != today:
            base = p["price"] if p else BASE_PRICES.get(symbol) or BASE_PRICES_OVERSEAS.get(symbol) or (
                10000 + int(hashlib.md5(symbol.encode()).hexdigest(), 16) % 90000)
            p = {"price": _tick(base, symbol), "prev_close": _tick(base, symbol), "date": today}
        ov = self._overrides().get(symbol)
        halted = False
        if ov is not None:
            if isinstance(ov, dict):
                halted = bool(ov.get("halted"))
                if ov.get("price") is not None:
                    p["price"] = float(ov["price"])
            else:
                p["price"] = float(ov)
        else:
            p["price"] = _tick(p["price"] * (1 + random.gauss(0, self.vol)), symbol)
        st["prices"][symbol] = p
        self._save(st)
        chg = (p["price"] / p["prev_close"] - 1) * 100
        return Quote(symbol, p["price"], p["prev_close"], round(chg, 2), halted, "58" if halted else "00",
                     timeutil.now(), markets.currency_of(symbol))

    async def get_account(self) -> Account:
        self._check_fail()
        st = self._st()
        holdings = {}
        total = st["cash"]
        for sym, h in st["holdings"].items():
            if h["qty"] <= 0:
                continue
            cur = st["prices"].get(sym, {}).get("price") or BASE_PRICES.get(sym) or BASE_PRICES_OVERSEAS.get(
                sym, h["avg_price"])
            holdings[sym] = Holding(sym, self.names(sym), h["qty"], h["avg_price"], cur, markets.currency_of(sym))
            total += markets.to_krw(cur * h["qty"], sym, self.overseas_cfg)
        return Account(st["cash"], holdings, total)

    async def place_order(self, symbol, side, qty, order_type, price) -> OrderResult:
        self._check_fail()
        q = await self.get_quote(symbol)
        st = self._st()
        if q.halted:
            return OrderResult(False, message="거래정지 종목")
        fill = q.price
        if order_type == "limit" and price is not None:
            if (side == "buy" and price < q.price) or (side == "sell" and price > q.price):
                fill = None
        st["seq"] += 1
        no = f"SIM{st['seq']:06d}"
        h = st["holdings"].setdefault(symbol, {"qty": 0, "avg_price": 0})
        if fill is not None:
            if side == "buy":
                cost = markets.to_krw(fill * qty, symbol, self.overseas_cfg)
                if cost > st["cash"]:
                    return OrderResult(False, message="주문가능금액 부족")
                st["cash"] -= cost
                h["avg_price"] = (h["avg_price"] * h["qty"] + fill * qty) / (h["qty"] + qty)
                h["qty"] += qty
            else:
                if qty > h["qty"]:
                    return OrderResult(False, message="매도가능수량 부족")
                h["qty"] -= qty
                st["cash"] += markets.to_krw(fill * qty, symbol, self.overseas_cfg)
        st["orders"][no] = {"qty": qty, "filled": qty if fill is not None else 0, "price": fill}
        self._save(st)
        return OrderResult(True, no, "주문 접수")

    async def get_fill(self, order_no, symbol) -> FillStatus:
        o = self._st()["orders"].get(order_no)
        if not o:
            raise BrokerError(f"주문번호 없음 {order_no}")
        return FillStatus(order_no, o["filled"], o["price"], o["qty"] - o["filled"])

    async def close(self):
        pass
