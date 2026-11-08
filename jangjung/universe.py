"""종목 마스터(이름↔코드)와 단계별 전략 정의.

- 종목 검색: data/stock_master.json(KIS 마스터 파일에서 생성) + 전략 후보 + 기본 대형주 목록
- 레버리지·인버스 등 제외 키워드가 들어간 종목은 거래 불가로 표시
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

import httpx

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
    def __init__(self, strategies: dict, data_dir: Path, blocked_keywords: list[str]):
        self.strategies = strategies
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
    def name(self, code: str) -> str:
        return self.master.get(code, {}).get("name", code)

    def blocked_reason(self, code: str) -> str | None:
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

    def search(self, query: str, limit: int = 8) -> list[dict]:
        q = query.strip()
        if re.fullmatch(r"\d{6}", q):
            return [self._row(q)] if q in self.master else []
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
        info = self.master[code]
        return {"symbol": code, "name": info["name"], "kind": info.get("kind"), "market": info.get("market"),
                "blocked": self.blocked_reason(code)}

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
