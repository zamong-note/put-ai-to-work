"""종목 마스터(이름↔코드)와 단계별 전략 정의.

- 종목 검색: data/stock_master.json(KIS 마스터 파일에서 생성) + 전략 후보 + 기본 대형주 목록
- 레버리지·인버스 등 제외 키워드가 들어간 종목은 거래 불가로 표시
- 해외 종목('거래소:티커'): config/overseas.json 기본 목록 + data/overseas_master.json(KIS 해외 마스터).
  해외 투자 설정(꺼짐 / 주요 3개국 / 전체 / 직접 선택)에서 허용한 국가만 거래 가능
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

import httpx

from . import markets

# 마스터 파일이 없을 때 쓰는 기본 목록 (시총 상위권 일부)
FALLBACK_STOCKS = {
    "005930": "삼성전자", "000660": "SK하이닉스", "373220": "LG에너지솔루션", "207940": "삼성바이오로직스",
    "005380": "현대차", "000270": "기아", "068270": "셀트리온", "105560": "KB금융", "035420": "NAVER",
    "055550": "신한지주", "012450": "한화에어로스페이스", "329180": "HD현대중공업", "005490": "POSCO홀딩스",
    "028260": "삼성물산", "035720": "카카오", "012330": "현대모비스", "006400": "삼성SDI", "051910": "LG화학",
    "005935": "삼성전자우", "086790": "하나금융지주", "032830": "삼성생명", "015760": "한국전력",
    "066570": "LG전자", "003550": "LG", "034730": "SK", "017670": "SK텔레콤", "030200": "KT",
    "009150": "삼성전기", "010130": "고려아연", "042660": "한화오션", "247540": "에코프로비엠", "086520": "에코프로",
}

MASTER_URLS = {
    "KOSPI": ("https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip", "kospi_code.mst", 228),
    "KOSDAQ": ("https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip", "kosdaq_code.mst", 222),
}


def _norm(s: str) -> str:
    return re.sub(r"[\s\-_.()]", "", s).upper()


class Universe:
    def __init__(self, strategies: dict, data_dir: Path, blocked_keywords: list[str],
                 overseas_cfg: dict | None = None, overseas_symbols: dict | None = None):
        self.strategies = strategies
        self.overseas_cfg = overseas_cfg if overseas_cfg is not None else {}   # 설정 dict 를 그대로 참조 (즉시 반영)
        self.overseas: dict[str, dict] = {}
        for sym, info in (overseas_symbols or {}).items():
            self.overseas[sym.upper()] = dict(info)
        opath = data_dir / "overseas_master.json"
        if opath.exists():
            for sym, info in json.loads(opath.read_text(encoding="utf-8")).items():
                self.overseas.setdefault(sym, info)
        self.categories: dict[str, str] = strategies["categories"]
        self.candidates: dict[str, dict] = strategies["universe"]
        self.tiers: dict[int, dict] = {int(k): v for k, v in strategies["tiers"].items()}
        self.blocked_keywords = blocked_keywords
        self.master: dict[str, dict] = {}
        for code, name in FALLBACK_STOCKS.items():
            self.master[code] = {"name": name, "market": "KOSPI", "kind": "STOCK"}
        mpath = data_dir / "stock_master.json"
        if mpath.exists():
            self.master.update(json.loads(mpath.read_text(encoding="utf-8")))
        for code, c in self.candidates.items():
            self.master[code] = {"name": c["name"], "market": "KOSPI", "kind": c["kind"]}
        self._validate_tiers()

    def _validate_tiers(self) -> None:
        for t, spec in self.tiers.items():
            total = sum(spec["targets"].values())
            if abs(total - 100) > 1e-6:
                raise ValueError(f"{t}단계 목표 비중 합계가 100이 아님: {total}")
            for code in spec["targets"]:
                if code not in self.candidates:
                    raise ValueError(f"{t}단계 종목 {code}가 후보 목록에 없음")

    # ── 조회 ──
    def info(self, code: str) -> dict | None:
        return self.overseas.get(code.upper()) if markets.is_overseas(code) else self.master.get(code)

    def exists(self, code: str) -> bool:
        return self.info(code) is not None

    def name(self, code: str) -> str:
        return (self.info(code) or {}).get("name", code)

    def blocked_reason(self, code: str) -> str | None:
        if markets.is_overseas(code):
            return self._overseas_blocked(code)
        info = self.master.get(code)
        if not info:
            return "종목 마스터에 없는 코드"
        n = info["name"].upper()
        for kw in self.blocked_keywords:
            if kw.upper() in n:
                return f"'{kw}' 상품은 다루지 않음 (레버리지·인버스·파생 제외)"
        if info.get("kind") not in ("STOCK", "ETF"):
            return f"{info.get('kind')} 상품은 다루지 않음 (국내 주식·ETF만)"
        return None

    def _overseas_blocked(self, code: str) -> str | None:
        info = self.overseas.get(code.upper())
        if not info:
            return "해외 종목 목록에 없는 코드"
        country = markets.country_of(code)
        allowed = markets.allowed_countries(self.overseas_cfg)
        if not allowed:
            return "해외 투자가 꺼져 있음 (설정 > 해외 투자에서 켤 수 있음)"
        if country not in allowed:
            return f"허용하지 않은 국가: {markets.COUNTRIES.get(country, country)} (설정 > 해외 투자)"
        return self.product_blocked(code)

    def product_blocked(self, code: str) -> str | None:
        """상품 자체가 범위 밖인지 (레버리지·인버스·파생). 국가 허용 여부와 무관 — 주문 감사용."""
        if not markets.is_overseas(code):
            return self.blocked_reason(code)
        info = self.overseas.get(code.upper())
        if not info:
            return "해외 종목 목록에 없는 코드"
        text = f"{info.get('name', '')} {info.get('name_en', '')}".upper()
        for kw in [*markets.OVERSEAS_BLOCKED, *self.blocked_keywords]:
            if kw.upper() in text:
                return f"'{kw}' 상품은 다루지 않음 (레버리지·인버스·파생 제외)"
        if info.get("kind") not in ("STOCK", "ETF"):
            return f"{info.get('kind')} 상품은 다루지 않음 (주식·ETF만)"
        return None

    def _search_overseas(self, q: str) -> list[str]:
        uq, nq = q.upper(), _norm(q)
        if uq in self.overseas:
            return [uq]
        exact, prefix, contains = [], [], []
        for sym, info in self.overseas.items():
            ticker = sym.split(":", 1)[1]
            names = [_norm(info.get("name", "")), _norm(info.get("name_en", ""))]
            if ticker == uq or nq in names:
                exact.append(sym)
            elif any(n.startswith(nq) for n in names if n):
                prefix.append(sym)
            elif len(nq) >= 2 and any(nq in n for n in names if n):
                contains.append(sym)
        return exact or (prefix + contains)

    def search(self, query: str, limit: int = 8) -> list[dict]:
        q = query.strip()
        if re.fullmatch(r"\d{6}", q):
            return [self._row(q)] if q in self.master else []
        if ":" in q:
            return [self._row(q.upper())] if q.upper() in self.overseas else []
        ov = [self._row(s) for s in self._search_overseas(q)]
        dom = self._search_domestic(q, limit)
        rows = dom + ov
        if any(r["name"] == q or r["symbol"].split(":")[-1] == q.upper() for r in rows):
            rows = [r for r in rows if r["name"] == q or r["symbol"].split(":")[-1] == q.upper()] or rows
        rows.sort(key=lambda r: (r["blocked"] is not None, r["symbol"] not in [d["symbol"] for d in dom]))
        return rows[:limit]

    def _search_domestic(self, q: str, limit: int) -> list[dict]:
        nq = _norm(q)
        exact, prefix, contains = [], [], []
        for code, info in self.master.items():
            nn = _norm(info["name"])
            if nn == nq:
                exact.append(code)
            elif nn.startswith(nq):
                prefix.append(code)
            elif nq in nn:
                contains.append(code)
        if exact:
            return [self._row(c) for c in exact]
        rows = [self._row(c) for c in prefix + contains]
        rows.sort(key=lambda r: (r["blocked"] is not None, len(r["name"])))
        return rows[:limit]

    def _row(self, code: str) -> dict:
        if markets.is_overseas(code):
            info = self.overseas[code]
            m = markets.split(code)[0]
            return {"symbol": code, "name": info["name"], "name_en": info.get("name_en"), "kind": info.get("kind"),
                    "market": markets.MARKETS[m]["name"], "country": markets.COUNTRIES[markets.country_of(code)],
                    "currency": markets.currency_of(code), "blocked": self.blocked_reason(code)}
        info = self.master[code]
        return {"symbol": code, "name": info["name"], "kind": info.get("kind"), "market": info.get("market"),
                "country": "한국", "currency": "KRW", "blocked": self.blocked_reason(code)}

    def tier(self, t: int) -> dict:
        if t not in self.tiers:
            raise ValueError("위험 단계는 1~5만 가능")
        return self.tiers[t]

    def category_label(self, code: str) -> str:
        return self.categories[self.candidates[code]["category"]]


def update_master(data_dir: Path) -> int:
    """KIS 종목 마스터(KOSPI/KOSDAQ)를 내려받아 data/stock_master.json 생성."""
    out: dict[str, dict] = {}
    for market, (url, fname, tail) in MASTER_URLS.items():
        r = httpx.get(url, timeout=60, follow_redirects=True)
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            raw = z.read(fname).decode("cp949", errors="replace")
        for row in raw.splitlines():
            tail_n = tail - 1  # KIS 샘플은 줄바꿈 포함 길이 기준 → splitlines() 후엔 1 작음
            if len(row) <= tail_n:
                continue
            head, rest = row[: len(row) - tail_n], row[len(row) - tail_n:]
            code = head[0:9].strip()
            name = head[21:].strip()
            group = rest[0:2]
            if not re.fullmatch(r"\d{6}", code):
                continue
            kind = {"ST": "STOCK", "EF": "ETF"}.get(group, group or "ETC")
            out[code] = {"name": name, "market": market, "kind": kind}
    (data_dir / "stock_master.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return len(out)


OVERSEAS_MASTER = ["nas", "nys", "ams", "shs", "szs", "tse", "hks", "hnx", "hsx"]


def update_overseas_master(data_dir: Path, files: list[str] | None = None) -> int:
    """KIS 해외 종목 마스터를 내려받아 data/overseas_master.json 생성 (주식·ETF만).

    파일: https://new.real.download.dws.co.kr/common/master/{nas|nys|...}mst.cod.zip (탭 구분, cp949)
    열: 국가코드, 거래소ID, 거래소코드, 거래소명, 심볼, 실시간심볼, 한글명, 영문명, 종목구분(2 주식·3 ETF), 통화, ...
    """
    out: dict[str, dict] = {}
    for f in files or OVERSEAS_MASTER:
        r = httpx.get(f"https://new.real.download.dws.co.kr/common/master/{f}mst.cod.zip", timeout=120,
                      follow_redirects=True)
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            raw = z.read(z.namelist()[0]).decode("cp949", errors="replace")
        for line in raw.splitlines():
            c = line.split("\t")
            if len(c) < 10:
                continue
            market = markets.EXCD_TO_MARKET.get(c[2].strip())
            kind = {"2": "STOCK", "3": "ETF"}.get(c[8].strip())
            sym = c[4].strip()
            if not market or not kind or not sym:
                continue
            out[f"{market}:{sym}"] = {"name": c[6].strip() or c[7].strip(), "name_en": c[7].strip(), "kind": kind}
    (data_dir / "overseas_master.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return len(out)
