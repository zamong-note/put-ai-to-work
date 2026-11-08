"""한국투자증권 KIS Open API 클라이언트 — 모의투자(기본) / 실전.

실전은 BROKER=kis_real 과 KIS_REAL_TRADING=yes 가 .env 에 함께 있을 때만 쓴다 (App._make_broker).
모의와 실전은 도메인과 주문·잔고·체결조회 TR ID만 다르다.
참고: https://apiportal.koreainvestment.com , https://github.com/koreainvestment/open-trading-api
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from .. import markets, timeutil
from .base import Account, BrokerError, BrokerNetworkError, BrokerRejected, FillStatus, Holding, OrderResult, Quote

MOCK_BASE = "https://openapivts.koreainvestment.com:29443"
REAL_BASE = "https://openapi.koreainvestment.com:9443"

TR = {
    "price": "FHKST01010100",        # 주식현재가 시세 (모의·실전 공통)
    "buy": "VTTC0012U",              # 모의 현금 매수 (구 VTTC0802U)
    "sell": "VTTC0011U",             # 모의 현금 매도 (구 VTTC0801U)
    "balance": "VTTC8434R",          # 모의 잔고조회
    "ccld": ["VTTC0081R", "VTTC8001R"],  # 모의 일별주문체결조회 (신/구)
}
LEGACY_ORDER_TR = {"buy": "VTTC0802U", "sell": "VTTC0801U"}
TR_REAL = {
    "price": "FHKST01010100",
    "buy": "TTTC0012U",              # 실전 현금 매수 (구 TTTC0802U)
    "sell": "TTTC0011U",             # 실전 현금 매도 (구 TTTC0801U)
    "balance": "TTTC8434R",          # 실전 잔고조회
    "ccld": ["TTTC0081R", "TTTC8001R"],  # 실전 일별주문체결조회 (3개월 이내, 신/구)
}
LEGACY_ORDER_TR_REAL = {"buy": "TTTC0802U", "sell": "TTTC0801U"}
TOKEN_ERRORS = {"EGW00123", "EGW00121"}


class KISBroker:
    def __init__(self, app_key: str, app_secret: str, account: str, product: str, data_dir: Path,
                 min_interval: float | None = None, real: bool = False):
        if not (app_key and app_secret and account):
            raise BrokerError("KIS_APP_KEY / KIS_APP_SECRET / KIS_ACCOUNT 가 .env 에 없습니다")
        self.real = real
        self.name = "kis-real" if real else "kis-mock"
        self.tr = TR_REAL if real else TR
        self.legacy_tr = LEGACY_ORDER_TR_REAL if real else LEGACY_ORDER_TR
        self.app_key, self.app_secret = app_key, app_secret
        self.cano, self.prdt = account[:8], product
        self.token_path = data_dir / ("kis_token_real.json" if real else "kis_token.json")   # 토큰을 섞지 않음
        self.http = httpx.AsyncClient(base_url=REAL_BASE if real else MOCK_BASE, timeout=10)
        self._lock = asyncio.Lock()
        self._last = 0.0
        # 초당 호출 제한: 모의 약 2건, 실전 약 20건 → 여유 있게
        self._min_interval = min_interval if min_interval is not None else (0.12 if real else 0.6)
        self._token: str | None = None
        self._token_exp: datetime | None = None
        self._ccld_tr = self.tr["ccld"][0]
        # 해외: App 이 설정과 연결해 준다 (허용 국가의 거래소 목록, 통화 → 원 환율)
        self.overseas_markets = lambda: []
        self.fx = lambda ccy: 1.0
        self._ov_cache: tuple[float, dict] = (0.0, {})

    # ── 인증 ──
    async def _get_token(self, force: bool = False) -> str:
        now = datetime.now()
        if not force and self._token and self._token_exp and now < self._token_exp:
            return self._token
        if not force and self.token_path.exists():
            d = json.loads(self.token_path.read_text(encoding="utf-8"))
            exp = datetime.fromisoformat(d["exp"])
            if d.get("app_key") == self.app_key[:6] and now < exp:
                self._token, self._token_exp = d["token"], exp
                return self._token
        r = await self.http.post("/oauth2/tokenP", json={
            "grant_type": "client_credentials", "appkey": self.app_key, "appsecret": self.app_secret})
        d = r.json()
        if "access_token" not in d:
            raise BrokerError(f"토큰 발급 실패: {d.get('error_description') or d}")
        self._token = d["access_token"]
        self._token_exp = now + timedelta(seconds=int(d.get("expires_in", 86400)) - 600)
        self.token_path.write_text(json.dumps({"token": self._token, "exp": self._token_exp.isoformat(),
                                               "app_key": self.app_key[:6]}), encoding="utf-8")
        return self._token

    async def _call(self, method: str, path: str, tr_id: str, params: dict | None = None,
                    body: dict | None = None, tr_cont: str = "", _retry: bool = True) -> tuple[dict, dict]:
        async with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            token = await self._get_token()
            headers = {
                "content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}",
                "appkey": self.app_key, "appsecret": self.app_secret, "tr_id": tr_id, "custtype": "P",
                "tr_cont": tr_cont,
            }
            try:
                r = await self.http.request(method, path, params=params, json=body, headers=headers)
            except httpx.HTTPError as e:
                raise BrokerNetworkError(f"네트워크 오류: {e}") from e
            finally:
                self._last = time.monotonic()
        try:
            d = r.json()
        except ValueError as e:
            raise BrokerError(f"HTTP {r.status_code} 응답 파싱 실패") from e
        if d.get("msg_cd") in TOKEN_ERRORS and _retry:
            await self._get_token(force=True)
            return await self._call(method, path, tr_id, params, body, tr_cont, _retry=False)
        if d.get("rt_cd") != "0":
            raise BrokerRejected(f"[{d.get('msg_cd')}] {d.get('msg1', '').strip()} (tr={tr_id})")
        return d, dict(r.headers)

    # ── 시세 ──
    async def get_quote(self, symbol: str) -> Quote:
        if markets.is_overseas(symbol):
            return await self._ov_quote(symbol)
        d, _ = await self._call("GET", "/uapi/domestic-stock/v1/quotations/inquire-price", self.tr["price"],
                                params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol})
        o = d.get("output") or {}
        price = float(o.get("stck_prpr") or 0)
        if price <= 0:
            raise BrokerError(f"{symbol} 현재가 0 — 시세 이상")
        prev = float(o.get("stck_sdpr") or 0) or price
        status = str(o.get("iscd_stat_cls_code") or "00")
        halted = str(o.get("temp_stop_yn", "N")).upper() == "Y" or status == "58"
        return Quote(symbol, price, prev, float(o.get("prdy_ctrt") or 0), halted, status, timeutil.now())

    # ── 잔고 ──
    async def get_account(self) -> Account:
        params = {"CANO": self.cano, "ACNT_PRDT_CD": self.prdt, "AFHR_FLPR_YN": "N", "OFL_YN": "",
                  "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N",
                  "PRCS_DVSN": "00", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
        holdings: dict[str, Holding] = {}
        summary: dict = {}
        tr_cont = ""
        for _ in range(10):
            d, h = await self._call("GET", "/uapi/domestic-stock/v1/trading/inquire-balance", self.tr["balance"],
                                    params=params, tr_cont=tr_cont)
            for row in d.get("output1") or []:
                qty = int(float(row.get("hldg_qty") or 0))
                if qty > 0:
                    sym = row["pdno"]
                    holdings[sym] = Holding(sym, row.get("prdt_name", sym), qty,
                                            float(row.get("pchs_avg_pric") or 0), float(row.get("prpr") or 0))
            out2 = d.get("output2") or [{}]
            summary = out2[0] if isinstance(out2, list) else out2
            if h.get("tr_cont") in ("F", "M"):
                params["CTX_AREA_FK100"] = d.get("ctx_area_fk100", "")
                params["CTX_AREA_NK100"] = d.get("ctx_area_nk100", "")
                tr_cont = "N"
            else:
                break
        # D+2 예수금(가수도정산금액)을 주문 가능 현금의 근사로 사용
        cash = float(summary.get("prvs_rcdl_excc_amt") or summary.get("dnca_tot_amt") or 0)
        total = float(summary.get("tot_evlu_amt") or 0)
        if self.overseas_markets():
            for sym, h in (await self._ov_holdings()).items():
                holdings[sym] = h
                total += h.qty * h.cur_price * self.fx(h.currency)
        return Account(cash, holdings, total)

    # ── 주문 ──
    async def place_order(self, symbol, side, qty, order_type, price) -> OrderResult:
        if markets.is_overseas(symbol):
            return await self._ov_order(symbol, side, qty, price)
        body = {"CANO": self.cano, "ACNT_PRDT_CD": self.prdt, "PDNO": symbol,
                "ORD_DVSN": "01" if order_type == "market" else "00",
                "ORD_QTY": str(int(qty)), "ORD_UNPR": "0" if order_type == "market" else str(int(price)),
                "EXCG_ID_DVSN_CD": "KRX"}
        if side == "sell":
            body["SLL_TYPE"] = "01"
        try:
            d, _ = await self._call("POST", "/uapi/domestic-stock/v1/trading/order-cash", self.tr[side], body=body)
        except BrokerRejected as e:
            # API가 명시적으로 거절한 경우에만(접수 안 됨이 확실) 구 TR로 한 번 재시도.
            # 네트워크 오류(BrokerNetworkError)는 접수 여부를 알 수 없으므로 재시도하지 않는다 → 이중주문 방지
            if not any(k in str(e) for k in ("TR", "tr_id", "EGW0")):
                raise
            d, _ = await self._call("POST", "/uapi/domestic-stock/v1/trading/order-cash",
                                    self.legacy_tr[side], body=body)
        o = d.get("output") or {}
        return OrderResult(True, str(o.get("ODNO") or o.get("odno") or ""), d.get("msg1", "").strip())

    async def get_fill(self, order_no: str, symbol: str) -> FillStatus:
        if markets.is_overseas(symbol):
            return await self._ov_fill(order_no, symbol)
        day = timeutil.now().strftime("%Y%m%d")
        params = {"CANO": self.cano, "ACNT_PRDT_CD": self.prdt, "INQR_STRT_DT": day, "INQR_END_DT": day,
                  "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00", "PDNO": symbol, "CCLD_DVSN": "00",
                  "ORD_GNO_BRNO": "", "ODNO": order_no, "INQR_DVSN_3": "00", "INQR_DVSN_1": "",
                  "EXCG_ID_DVSN_CD": "KRX", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
        try:
            d, _ = await self._call("GET", "/uapi/domestic-stock/v1/trading/inquire-daily-ccld", self._ccld_tr,
                                    params=params)
        except BrokerError:
            if self._ccld_tr == self.tr["ccld"][1]:
                raise
            self._ccld_tr = self.tr["ccld"][1]
            d, _ = await self._call("GET", "/uapi/domestic-stock/v1/trading/inquire-daily-ccld", self._ccld_tr,
                                    params=params)
        for row in d.get("output1") or []:
            if str(row.get("odno", "")).lstrip("0") == str(order_no).lstrip("0"):
                filled = int(float(row.get("tot_ccld_qty") or 0))
                avg = float(row.get("avg_prvs") or 0) or None
                return FillStatus(order_no, filled, avg, int(float(row.get("rmn_qty") or 0)))
        return FillStatus(order_no, 0, None, -1)

    # ── 해외주식 ──
    async def _ov_quote(self, symbol: str) -> Quote:
        m, code = markets.split(symbol)
        d, _ = await self._call("GET", "/uapi/overseas-price/v1/quotations/price", "HHDFS00000300",
                                params={"AUTH": "", "EXCD": markets.MARKETS[m]["excd"], "SYMB": code})
        o = d.get("output") or {}
        price = float(o.get("last") or 0)
        if price <= 0:
            raise BrokerError(f"{symbol} 현재가 없음 (휴장·거래정지이거나 코드 확인 필요)")
        prev = float(o.get("base") or 0) or price
        return Quote(symbol, price, prev, float(o.get("rate") or 0), False, "00", timeutil.now(),
                     markets.currency_of(symbol))

    async def _ov_holdings(self) -> dict[str, Holding]:
        ts, cached = self._ov_cache
        if time.monotonic() - ts < 30:
            return cached
        tr = "TTTS3012R" if self.real else "VTTS3012R"
        mk = self.overseas_markets()
        # 실전은 NASD 하나로 미국 전체, 모의는 거래소별로 조회
        queries = sorted({("NASD" if self.real and markets.MARKETS[m]["country"] == "US" else m,
                           markets.MARKETS[m]["ccy"]) for m in mk})
        out: dict[str, Holding] = {}
        for ex, ccy in queries:
            d, _ = await self._call("GET", "/uapi/overseas-stock/v1/trading/inquire-balance", tr, params={
                "CANO": self.cano, "ACNT_PRDT_CD": self.prdt, "OVRS_EXCG_CD": ex, "TR_CRCY_CD": ccy,
                "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""})
            for row in d.get("output1") or []:
                qty = int(float(row.get("ovrs_cblc_qty") or 0))
                if qty <= 0:
                    continue
                sym = f"{(row.get('ovrs_excg_cd') or ex).strip()}:{row['ovrs_pdno'].strip()}"
                out[sym] = Holding(sym, row.get("ovrs_item_name", sym), qty, float(row.get("pchs_avg_pric") or 0),
                                   float(row.get("now_pric2") or 0), ccy)
        self._ov_cache = (time.monotonic(), out)
        return out

    async def _ov_order(self, symbol, side, qty, price) -> OrderResult:
        if price is None:
            raise BrokerRejected("해외 주문은 지정가만 가능 (가격 없음)")
        m, code = markets.split(symbol)
        tr = markets.ORDER_TR[m][0 if side == "buy" else 1]
        if not self.real:
            tr = "V" + tr[1:]                                  # 모의투자 TR
        body = {"CANO": self.cano, "ACNT_PRDT_CD": self.prdt, "OVRS_EXCG_CD": m, "PDNO": code,
                "ORD_QTY": str(int(qty)), "OVRS_ORD_UNPR": f"{price:.{max(markets.decimals(symbol), 0)}f}",
                "CTAC_TLNO": "", "MGCO_APTM_ODNO": "", "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": "00",
                "SLL_TYPE": "00" if side == "sell" else ""}
        d, _ = await self._call("POST", "/uapi/overseas-stock/v1/trading/order", tr, body=body)
        self._ov_cache = (0.0, {})                             # 잔고 캐시 무효화
        o = d.get("output") or {}
        return OrderResult(True, str(o.get("ODNO") or o.get("odno") or ""), d.get("msg1", "").strip())

    async def _ov_fill(self, order_no: str, symbol: str) -> FillStatus:
        m, code = markets.split(symbol)
        now = timeutil.now()
        end = markets.local_date(m, now)
        start = markets.local_date(m, now - timedelta(days=2))
        tr = "TTTS3035R" if self.real else "VTTS3035R"
        params = {"CANO": self.cano, "ACNT_PRDT_CD": self.prdt, "PDNO": code if self.real else "%",
                  "ORD_STRT_DT": start, "ORD_END_DT": end, "SLL_BUY_DVSN": "00", "CCLD_NCCS_DVSN": "00",
                  "OVRS_EXCG_CD": m if self.real else "", "SORT_SQN": "DS", "ORD_DT": "", "ORD_GNO_BRNO": "",
                  "ODNO": "", "CTX_AREA_NK200": "", "CTX_AREA_FK200": ""}
        d, _ = await self._call("GET", "/uapi/overseas-stock/v1/trading/inquire-ccnl", tr, params=params)
        for row in d.get("output") or []:
            if str(row.get("odno", "")).lstrip("0") == str(order_no).lstrip("0"):
                filled = int(float(row.get("ft_ccld_qty") or 0))
                avg = float(row.get("ft_ccld_unpr3") or 0) or None
                return FillStatus(order_no, filled, avg, int(float(row.get("nccs_qty") or 0)))
        return FillStatus(order_no, 0, None, -1)

    async def close(self):
        await self.http.aclose()
