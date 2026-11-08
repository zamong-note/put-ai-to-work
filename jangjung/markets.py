"""해외 시장 정의: 국가 · 거래소 · 통화 · 거래 시간 · KIS 주문 TR · 호가 단위.

종목 코드 표기
  국내: 6자리 코드            예) 005930
  해외: '거래소:티커'          예) NASD:AAPL, TKSE:7203, SHAA:600519, SEHK:00700, VNSE:VNM

한국투자증권 Open API로 직접 주문할 수 있는 해외 시장은 미국·일본·중국(상해·심천)·홍콩·베트남뿐이다.
'전체'는 이 5개국을 뜻한다.
"""
from __future__ import annotations

import math
from datetime import datetime, time
from zoneinfo import ZoneInfo

COUNTRIES = {"US": "미국", "JP": "일본", "CN": "중국", "HK": "홍콩", "VN": "베트남"}
MAJOR3 = ["US", "JP", "CN"]
SCOPES = {"major3": "주요 3개국 (미국·일본·중국)", "all": "전체 (KIS 지원 5개국)", "custom": "직접 선택"}
CURRENCY_LABEL = {"KRW": "원", "USD": "달러", "JPY": "엔", "CNY": "위안", "HKD": "홍콩달러", "VND": "동"}

# OVRS_EXCG_CD → 정보.  excd = 시세 API용 3자리 코드, sessions = 현지 시각 (개장·폐장 직후/직전 몇 분은 피함)
MARKETS: dict[str, dict] = {
    "NASD": dict(country="US", name="나스닥", excd="NAS", tz="America/New_York", ccy="USD",
                 sessions=[("09:31", "15:55")]),
    "NYSE": dict(country="US", name="뉴욕", excd="NYS", tz="America/New_York", ccy="USD",
                 sessions=[("09:31", "15:55")]),
    "AMEX": dict(country="US", name="아멕스", excd="AMS", tz="America/New_York", ccy="USD",
                 sessions=[("09:31", "15:55")]),
    "TKSE": dict(country="JP", name="도쿄", excd="TSE", tz="Asia/Tokyo", ccy="JPY",
                 sessions=[("09:01", "11:29"), ("12:31", "15:24")]),
    "SHAA": dict(country="CN", name="상해", excd="SHS", tz="Asia/Shanghai", ccy="CNY",
                 sessions=[("09:31", "11:29"), ("13:01", "14:56")]),
    "SZAA": dict(country="CN", name="심천", excd="SZS", tz="Asia/Shanghai", ccy="CNY",
                 sessions=[("09:31", "11:29"), ("13:01", "14:56")]),
    "SEHK": dict(country="HK", name="홍콩", excd="HKS", tz="Asia/Hong_Kong", ccy="HKD",
                 sessions=[("09:31", "11:59"), ("13:01", "15:55")]),
    "HASE": dict(country="VN", name="하노이", excd="HNX", tz="Asia/Ho_Chi_Minh", ccy="VND",
                 sessions=[("09:16", "11:29"), ("13:01", "14:29")]),
    "VNSE": dict(country="VN", name="호치민", excd="HSX", tz="Asia/Ho_Chi_Minh", ccy="VND",
                 sessions=[("09:16", "11:29"), ("13:01", "14:29")]),
}
EXCD_TO_MARKET = {v["excd"]: k for k, v in MARKETS.items()}

# 실전 주문 TR (모의는 첫 글자 T → V).  매수, 매도
ORDER_TR = {
    "NASD": ("TTTT1002U", "TTTT1006U"), "NYSE": ("TTTT1002U", "TTTT1006U"), "AMEX": ("TTTT1002U", "TTTT1006U"),
    "TKSE": ("TTTS0308U", "TTTS0307U"),
    "SHAA": ("TTTS0202U", "TTTS1005U"), "SZAA": ("TTTS0305U", "TTTS0304U"),
    "SEHK": ("TTTS1002U", "TTTS1001U"),
    "HASE": ("TTTS0311U", "TTTS0310U"), "VNSE": ("TTTS0311U", "TTTS0310U"),
}
# 해외 종목명에 들어 있으면 거래하지 않는 키워드 (레버리지·인버스·파생)
OVERSEAS_BLOCKED = ["LEVERAGE", "INVERSE", "ULTRA", "2X", "3X", "-1X", "-2X", "-3X", "BEAR", "SHORT", "DAILY",
                    "VIX", "FUTURES", "레버리지", "인버스"]


# ── 종목 코드 ──
def split(symbol: str) -> tuple[str | None, str]:
    if ":" in symbol:
        m, code = symbol.split(":", 1)
        return m.upper(), code
    return None, symbol


def is_overseas(symbol: str) -> bool:
    return split(symbol)[0] is not None


def market_of(symbol: str) -> str:
    return split(symbol)[0] or "KRX"


def country_of(symbol: str) -> str:
    m = split(symbol)[0]
    return MARKETS[m]["country"] if m in MARKETS else "KR"


def currency_of(symbol: str) -> str:
    m = split(symbol)[0]
    return MARKETS[m]["ccy"] if m in MARKETS else "KRW"


# ── 설정 ──
def allowed_countries(cfg: dict) -> set[str]:
    """해외 투자 설정에서 허용한 국가. 꺼져 있으면 빈 집합."""
    if (cfg or {}).get("enabled") != "on":
        return set()
    scope = cfg.get("scope", "major3")
    if scope == "all":
        return set(COUNTRIES)
    if scope == "custom":
        return {c for c in cfg.get("countries", []) if c in COUNTRIES}
    return set(MAJOR3)


def scope_text(cfg: dict) -> str:
    allowed = allowed_countries(cfg)
    if not allowed:
        return "해외 투자 꺼짐 (국내만)"
    return f"{SCOPES.get(cfg.get('scope'), '')}: " + "·".join(COUNTRIES[c] for c in COUNTRIES if c in allowed)


def fx_rate(currency: str, cfg: dict) -> float:
    """1 단위 외화 → 원. 안전 한도·금액 환산용 근사 환율 (설정에서 바꿈)."""
    cfg = cfg or {}
    return {"KRW": 1.0, "USD": cfg.get("fx_usd", 1400), "JPY": cfg.get("fx_jpy100", 930) / 100,
            "CNY": cfg.get("fx_cny", 195), "HKD": cfg.get("fx_hkd", 180),
            "VND": cfg.get("fx_vnd100", 5.5) / 100}.get(currency, 1.0)


def to_krw(amount: float, symbol: str, cfg: dict) -> float:
    return amount * fx_rate(currency_of(symbol), cfg)


# ── 거래 시간 ──
def _hm(s: str) -> time:
    h, m = s.split(":")[:2]
    return time(int(h), int(m))


def market_open(market: str, now: datetime, holidays: dict[str, list[str]] | None = None) -> bool:
    """해외 시장이 지금(현지 시각 기준) 거래 중인지. 현지 휴장일은 settings 의 overseas_holidays."""
    info = MARKETS.get(market)
    if not info:
        return False
    local = now.astimezone(ZoneInfo(info["tz"]))
    if local.weekday() >= 5:
        return False
    if local.strftime("%Y-%m-%d") in set((holidays or {}).get(info["country"], [])):
        return False
    t = local.time()
    return any(_hm(a) <= t < _hm(b) for a, b in info["sessions"])


def local_date(market: str, now: datetime) -> str:
    info = MARKETS.get(market)
    return now.astimezone(ZoneInfo(info["tz"])).strftime("%Y%m%d") if info else now.strftime("%Y%m%d")


# ── 가격 표기 · 호가 ──
def decimals(symbol: str) -> int:
    return {"USD": 2, "CNY": 2, "HKD": 3}.get(currency_of(symbol), 0)


def fmt_price(symbol: str, price: float | None) -> str:
    if price is None:
        return "—"
    ccy = currency_of(symbol)
    d = decimals(symbol)
    num = f"{price:,.{d}f}"
    return f"{num}원" if ccy == "KRW" else f"{num} {ccy}"


def tick(symbol: str, price: float) -> float:
    m = split(symbol)[0]
    ccy = currency_of(symbol)
    if ccy in ("USD", "CNY"):
        return 0.01
    if m == "TKSE":
        return 1 if price < 3000 else 5 if price < 5000 else 10 if price < 30000 else 50 if price < 50000 else 100
    if m == "SEHK":
        for lim, st in ((0.25, 0.001), (0.5, 0.005), (10, 0.01), (20, 0.02), (100, 0.05), (200, 0.1), (500, 0.2),
                        (1000, 0.5), (2000, 1), (5000, 2)):
            if price < lim:
                return st
        return 5
    if ccy == "VND":
        return 10 if price < 10000 else 50 if price < 50000 else 100
    return 1


def limit_price(symbol: str, price: float, side: str, slippage_pct: float) -> float:
    """해외는 시장가 주문이 없어(모의투자 포함) 현재가 ±여유의 지정가로 즉시 체결을 노린다."""
    raw = price * (1 + slippage_pct / 100) if side == "buy" else price * (1 - slippage_pct / 100)
    st = tick(symbol, price)
    n = (math.ceil(raw / st) if side == "buy" else math.floor(raw / st)) * st
    return round(n, 3)
