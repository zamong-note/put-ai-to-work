"""환율 · 경제지표 수집 (키 없이 쓰는 공개 API 위주).

- 환율: open.er-api.com (원화 기준 전 통화, 일 1회 갱신) → 실패하면 frankfurter.app (ECB, VND 없음)
- 국가별 경제지표: World Bank API (GDP 성장률·물가·실업률·경상수지, 연간 · 최신값) — 7일 캐시
- 미국 금리·변동성: FRED (선택, .env 의 FRED_API_KEY 가 있을 때만)
- 시장 흐름: 자동 투자 후보 종목의 장 마감 가격 기록(price_history)으로 5·20·60거래일 수익률

수집 실패는 매매를 막지 않는다. 빠진 데이터는 '없음'으로 넘기고, AI는 데이터가 부족하면 비중을 바꾸지 않는다.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

import httpx

from . import markets, timeutil

log = logging.getLogger("jangjung")

FX_CCYS = ["USD", "JPY", "CNY", "HKD", "VND"]
FX_SETTING = {"USD": ("fx_usd", 1), "JPY": ("fx_jpy100", 100), "CNY": ("fx_cny", 1), "HKD": ("fx_hkd", 1),
              "VND": ("fx_vnd100", 100)}
FX_MAX_JUMP_PCT = 15.0          # 직전 값 대비 이보다 크게 바뀌면 오류로 보고 반영하지 않음

WB_COUNTRIES = {"KR": "한국", "US": "미국", "JP": "일본", "CN": "중국", "HK": "홍콩", "VN": "베트남"}
WB_INDICATORS = {"NY.GDP.MKTP.KD.ZG": "GDP 성장률(%)", "FP.CPI.TOTL.ZG": "물가상승률(%)",
                 "SL.UEM.TOTL.ZS": "실업률(%)", "BN.CAB.XOKA.GD.ZS": "경상수지(GDP 대비 %)"}
FRED_SERIES = {"DGS10": "미국 10년 국채금리(%)", "FEDFUNDS": "미국 기준금리(%)", "VIXCLS": "VIX 변동성지수"}


class MacroError(RuntimeError):
    pass


async def _get_json(url: str, params: dict | None = None) -> object:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        r = await c.get(url, params=params)
        r.raise_for_status()
        return r.json()


# ── 환율 ──
async def fetch_fx() -> dict:
    """1 외화 = ? 원. {"rates": {...}, "source": ..., "as_of": ...}"""
    errors = []
    try:
        d = await _get_json("https://open.er-api.com/v6/latest/KRW")
        if d.get("result") != "success":
            raise MacroError(d.get("error-type") or "응답 오류")
        rates = {c: 1 / d["rates"][c] for c in FX_CCYS if d["rates"].get(c)}
        return {"rates": rates, "source": "open.er-api.com", "as_of": d.get("time_last_update_utc", "")}
    except Exception as e:  # noqa: BLE001
        errors.append(f"open.er-api: {e}")
    try:
        d = await _get_json("https://api.frankfurter.app/latest", {"from": "KRW", "to": "USD,JPY,CNY,HKD"})
        rates = {c: 1 / v for c, v in d["rates"].items() if v}
        return {"rates": rates, "source": "frankfurter.app (ECB)", "as_of": d.get("date", "")}
    except Exception as e:  # noqa: BLE001
        errors.append(f"frankfurter: {e}")
    raise MacroError("환율을 가져오지 못했습니다 — " + "; ".join(errors))


async def update_fx(app, force: bool = False) -> dict:
    """환율을 받아 설정(해외 투자의 환율 값)에 반영하고 기록한다. 바뀐 값과 건너뛴 값을 돌려준다."""
    db, ov = app.db, app.settings.overseas
    got = await fetch_fx()
    values, skipped = {}, []
    for ccy, rate in got["rates"].items():
        key, unit = FX_SETTING[ccy]
        new = round(rate * unit, 3 if ccy == "VND" else 2)
        old = ov.get(key)
        if old and not force and abs(new / old - 1) * 100 > FX_MAX_JUMP_PCT:
            skipped.append(f"{ccy} {old:g}→{new:g} (변동 {FX_MAX_JUMP_PCT:g}% 초과, 수동 확인 필요)")
            continue
        values[key] = new
    changes = []
    if values:
        try:
            changes = app.store.update("overseas", values)
        except Exception as e:  # noqa: BLE001 — 설정 범위 밖이면 반영하지 않음
            skipped.append(f"설정 범위 밖: {e}")
    hist = db.kv_get("fx_history", {})
    hist[timeutil.today_str()] = {c: round(r, 6) for c, r in got["rates"].items()}
    for d in sorted(hist)[:-400]:
        hist.pop(d)
    db.kv_set("fx_history", hist)
    info = {"at": timeutil.now().isoformat(timespec="seconds"), "source": got["source"], "as_of": got["as_of"],
            "rates": {c: round(r, 4) for c, r in got["rates"].items()}, "skipped": skipped}
    db.kv_set("fx_last", info)
    return {"changes": changes, "skipped": skipped, "info": info}


def fx_summary(db, cfg: dict) -> dict:
    """현재 환율(설정 값)과 1일·30일 변화율 (환율 기록 기준)."""
    hist = db.kv_get("fx_history", {})
    days = sorted(hist)
    out = {}
    for ccy in FX_CCYS:
        cur = markets.fx_rate(ccy, cfg)
        row = {"krw_per_unit": round(cur, 4)}
        for label, n in (("chg_1d_pct", 1), ("chg_30d_pct", 30)):
            past = None
            if days:
                target = (datetime.fromisoformat(days[-1]) - timedelta(days=n)).strftime("%Y-%m-%d")
                cands = [d for d in days if d <= target]
                if cands and hist[cands[-1]].get(ccy):
                    past = hist[cands[-1]][ccy]
            row[label] = round((cur / past - 1) * 100, 2) if past else None
        out[ccy] = row
    return out


# ── 경제지표 ──
async def fetch_worldbank() -> dict:
    codes = ";".join(WB_COUNTRIES)
    out: dict[str, dict] = {name: {} for name in WB_COUNTRIES.values()}
    for ind, label in WB_INDICATORS.items():
        d = await _get_json(f"https://api.worldbank.org/v2/country/{codes}/indicator/{ind}",
                            {"format": "json", "mrnev": 1, "per_page": 100})
        rows = d[1] if isinstance(d, list) and len(d) > 1 and d[1] else []
        for row in rows:
            iso2 = (row.get("country") or {}).get("id")
            name = WB_COUNTRIES.get(iso2)
            if name and row.get("value") is not None:
                out[name][label] = {"value": round(float(row["value"]), 2), "year": row.get("date")}
    return out


async def fetch_fred() -> dict:
    key = os.getenv("FRED_API_KEY", "").strip()
    if not key:
        return {}
    out = {}
    for sid, label in FRED_SERIES.items():
        d = await _get_json("https://api.stlouisfed.org/fred/series/observations",
                            {"series_id": sid, "api_key": key, "file_type": "json", "sort_order": "desc", "limit": 5})
        for obs in d.get("observations", []):
            if obs.get("value") not in (None, "."):
                out[label] = {"value": float(obs["value"]), "date": obs["date"]}
                break
    return out


async def cached(db, key: str, max_age: timedelta, fn) -> tuple[dict, str | None]:
    """(데이터, 오류). 캐시가 신선하면 그대로, 아니면 새로 받고 실패하면 오래된 캐시라도 쓴다."""
    c = db.kv_get(key)
    if c and timeutil.now() - datetime.fromisoformat(c["at"]) < max_age:
        return c["data"], None
    try:
        data = await fn()
        db.kv_set(key, {"at": timeutil.now().isoformat(), "data": data})
        return data, None
    except Exception as e:  # noqa: BLE001
        log.warning("%s 수집 실패: %s", key, e)
        return (c or {}).get("data") or {}, f"{type(e).__name__}: {e}"


# ── 시장 흐름 (장 마감 가격 기록) ──
def record_closes(db, prices: dict[str, float]) -> None:
    hist = db.kv_get("price_history", {})
    hist[timeutil.today_str()] = {s: p for s, p in prices.items() if p}
    for d in sorted(hist)[:-130]:
        hist.pop(d)
    db.kv_set("price_history", hist)


def momentum(db, names: dict[str, str]) -> dict:
    hist = db.kv_get("price_history", {})
    days = sorted(hist)
    out = {}
    if not days:
        return out
    last = hist[days[-1]]
    for sym, price in last.items():
        row = {"name": names.get(sym, sym)}
        for n in (5, 20, 60):
            if len(days) > n and hist[days[-1 - n]].get(sym):
                row[f"ret_{n}d_pct"] = round((price / hist[days[-1 - n]][sym] - 1) * 100, 2)
        out[sym] = row
    return out


async def snapshot(app) -> dict:
    """AI 전략 점검에 넘길 데이터 전체."""
    db = app.db
    wb, wb_err = await cached(db, "macro_worldbank", timedelta(days=7), fetch_worldbank)
    fred, fred_err = await cached(db, "macro_fred", timedelta(hours=20), fetch_fred)
    names = {s: app.universe.name(s) for s in app.universe.candidates}
    return {
        "as_of": timeutil.now().strftime("%Y-%m-%d %H:%M KST"),
        "fx_krw": fx_summary(db, app.settings.overseas),
        "fx_source": db.kv_get("fx_last"),
        "economy_by_country": wb,
        "us_rates": fred,
        "market_momentum": momentum(db, names),
        "errors": [e for e in (wb_err and f"World Bank: {wb_err}", fred_err and f"FRED: {fred_err}") if e],
    }
