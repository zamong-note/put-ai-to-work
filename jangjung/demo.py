"""데모 데이터: 시뮬레이터로 일주일치(11/2~11/6) 운용 기록을 만든다. 키·LLM 불필요.

  uv run python -m jangjung demo    → data/demo 에 기록을 만들고 대시보드를 띄운다
실제 운용 데이터(data/jangjung.db)와는 폴더가 분리돼 섞이지 않는다.
"""
from __future__ import annotations

import json
from datetime import datetime

from . import timeutil
from .app import App


def _at(d: int, h: int, m: int) -> None:
    timeutil.set_clock(datetime(2026, 11, d, h, m, tzinfo=timeutil.KST))


def _price(app, sym: str, v) -> None:
    p = app.settings.data_dir / "sim_overrides.json"
    ov = json.loads(p.read_text()) if p.exists() else {}
    if v is None:
        ov.pop(sym, None)
    else:
        ov[sym] = v
    p.write_text(json.dumps(ov))


async def seed(settings) -> None:
    settings.broker = "sim"
    fill_wait = settings.engine.get("fill_wait_seconds")
    settings.engine["fill_wait_seconds"] = 0
    app = App(settings, console=False)
    try:
        _at(2, 10, 0)
        app.autoinvest.save_profile([4, 3, 3, 3, 4, 3])
        r = await app.autoinvest.propose(10_000_000, 3)
        await app.pending.resolve(r["pending_id"], "approve")
        _price(app, "000660", 200000)
        r = await app.rules.propose("SK하이닉스 수익률 15% 넘으면 절반 팔고, 산 가격보다 7% 떨어지면 전부 팔아 줘", [
            {"symbol": "000660", "side": "sell", "condition": "return_at_or_above", "threshold": 15,
             "qty_mode": "fraction", "qty_value": 0.5},
            {"symbol": "000660", "side": "sell", "condition": "return_at_or_below", "threshold": -7,
             "qty_mode": "fraction", "qty_value": 1.0}])
        await app.pending.resolve(r["pending_id"], "approve")
        r = await app.rules.propose("삼성전자 6만5천원 이하로 내려오면 100만원어치 사 줘", [
            {"symbol": "005930", "side": "buy", "condition": "price_at_or_below", "threshold": 65000,
             "qty_mode": "amount", "qty_value": 1_000_000}])
        await app.pending.resolve(r["pending_id"], "approve")
        _price(app, "000660", None)
        _at(2, 15, 41)
        await app.engine.step()
        for d in (3, 4, 5, 6):
            _at(d, 9, 31)
            await app.engine.step()
            if d == 4:                                   # 익절 조건 충족 → 절반 매도
                _at(d, 11, 5)
                _price(app, "000660", 219000)
                await app.engine.tick()
                _price(app, "000660", None)
            _at(d, 15, 41)
            await app.engine.step()
        timeutil.set_clock(None)
        await app.rules.propose("NAVER 22만원 넘으면 10주 팔아", [      # 확인 대기 카드 1개 남겨 두기
            {"symbol": "035420", "side": "sell", "condition": "price_at_or_above", "threshold": 220000,
             "qty_mode": "shares", "qty_value": 10}])
    finally:
        timeutil.set_clock(None)
        settings.engine["fill_wait_seconds"] = fill_wait
        await app.close()
