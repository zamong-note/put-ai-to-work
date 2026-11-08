"""화면에서 바꿀 수 있는 설정: 스키마 · 검증 · 저장 · 즉시 적용.

- 운영 설정(안전 한도·일정·자동 투자·휴장일): config/settings.json 이 기본값,
  사용자가 바꾼 값만 data/settings.local.json 에 저장 (git 에 안 올라감)
- 연결 설정(.env): 비밀값은 쓰기 전용 — 화면으로 다시 내보내지 않는다
- 기획안의 범위를 깨는 값(초단타, 레버리지·인버스 허용, 거래정지 종목 주문)은 거부한다
"""
from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path

from .config import ROOT

REQUIRED_KEYWORDS = ["레버리지", "인버스"]          # 범위 관리: 절대 해제 불가
REQUIRED_STATUS = ["58"]                           # 거래정지는 항상 차단
STATUS_OPTIONS = {"51": "관리종목", "52": "투자위험", "53": "투자경고", "54": "투자주의", "58": "거래정지", "59": "단기과열"}

# (키, 라벨, 타입, 단위, 최소, 최대, 도움말)
SCHEMA: dict[str, list[dict]] = {
    "safety": [
        dict(key="max_buy_order_amount", label="1회 매수 한도", type="int", unit="원", min=100_000, max=100_000_000, step=100_000,
             help="넘는 매수는 실행하지 않고 확인을 요청합니다"),
        dict(key="max_sell_order_amount", label="1회 매도 한도", type="int", unit="원", min=100_000, max=1_000_000_000,
             step=100_000, nullable=True, help="비우면 해제 — 손절이 확인 대기로 막히지 않게 기본 해제"),
        dict(key="max_daily_orders", label="하루 주문 횟수", type="int", unit="회", min=1, max=50,
             help="초단타 방지를 위해 최대 50회"),
        dict(key="max_daily_buy_amount", label="하루 매수 금액", type="int", unit="원", min=100_000, max=1_000_000_000,
             step=100_000),
        dict(key="max_change_pct_from_prev_close", label="급등락 차단 (전일 대비)", type="float", unit="%", min=1, max=30,
             step=0.5, help="이 이상 오르내리면 그 종목 주문을 멈추고 확인 요청"),
        dict(key="max_tick_jump_pct", label="단시간 급변 차단", type="float", unit="%", min=0.5, max=30, step=0.5),
        dict(key="tick_jump_window_sec", label="급변 판단 시간", type="int", unit="초", min=30, max=900),
        dict(key="max_consecutive_api_errors", label="API 연속 오류 시 전체 중지", type="int", unit="회", min=1, max=10),
        dict(key="blocked_status_codes", label="주문을 막을 종목 상태", type="codes", options=STATUS_OPTIONS,
             required=REQUIRED_STATUS, help="거래정지(58)는 항상 차단"),
        dict(key="blocked_name_keywords", label="제외 상품 키워드", type="keywords", required=REQUIRED_KEYWORDS,
             help="종목명에 들어 있으면 거래하지 않음 · 레버리지·인버스는 항상 제외"),
    ],
    "engine": [
        dict(key="poll_seconds", label="감시 주기", type="int", unit="초", min=10, max=60,
             help="조건 충족 후 1분 안 주문을 위해 최대 60초, 초단타 방지를 위해 최소 10초"),
        dict(key="order_type", label="주문 방식", type="enum", options={"market": "시장가", "limit": "지정가 (판단 시세)"}),
        dict(key="session_start", label="감시 시작", type="time", min="09:00:00", max="15:30:00"),
        dict(key="session_end", label="감시 종료", type="time", min="09:00:00", max="15:30:00",
             help="15:20 이후는 장 마감 동시호가"),
        dict(key="build_time", label="분할 매수 시각", type="time", min="09:00:00", max="15:20:00"),
        dict(key="rebalance_time", label="리밸런싱 점검 시각", type="time", min="09:00:00", max="15:20:00",
             help="매월 첫 거래일"),
        dict(key="summary_time", label="장 마감 요약 시각", type="time", min="15:30:00", max="23:59:00"),
        dict(key="fill_wait_seconds", label="체결 확인 대기", type="int", unit="초", min=0, max=30),
        dict(key="over_limit_card_ttl_min", label="한도 초과 확인 유효 시간", type="int", unit="분", min=1, max=60,
             help="이 시간 안에 승인하지 않으면 주문하지 않고 규칙을 일시정지"),
    ],
    "autoinvest": [
        dict(key="rebalance_mode", label="리밸런싱 방식", type="enum", options={"auto": "자동 실행", "confirm": "매번 확인"}),
        dict(key="build_tranches", label="분할 매수 횟수", type="int", unit="회", min=1, max=10,
             help="다음에 시작하는 자동 투자부터 적용"),
        dict(key="rebalance_band_pp", label="리밸런싱 허용 범위", type="float", unit="%p", min=1, max=20, step=0.5,
             help="목표 비중에서 이만큼 벗어나면 조정"),
        dict(key="min_trade_dev_pp", label="조정 최소 차이", type="float", unit="%p", min=0, max=10, step=0.5,
             help="리밸런싱 때 이보다 작게 벗어난 종목은 건드리지 않음"),
    ],
}
SECTION_LABEL = {"safety": "안전 한도", "engine": "운영 일정", "autoinvest": "자동 투자", "holidays": "휴장일"}

# .env: (키, 라벨, 종류, 적용 시점)
ENV_SCHEMA = [
    dict(key="BROKER", label="브로커", kind="enum", options={"sim": "시뮬레이터 (가상 계좌)", "kis": "KIS 모의투자"}, live=False),
    dict(key="KIS_APP_KEY", label="KIS 앱키", kind="secret", live=False),
    dict(key="KIS_APP_SECRET", label="KIS 앱시크릿", kind="secret", live=False),
    dict(key="KIS_ACCOUNT", label="모의투자 계좌번호 (앞 8자리)", kind="account", live=False),
    dict(key="KIS_ACCOUNT_PRODUCT", label="계좌 상품코드", kind="text", live=False, pattern=r"^\d{2}$"),
    dict(key="TELEGRAM_BOT_TOKEN", label="텔레그램 봇 토큰", kind="secret", live=False),
    dict(key="TELEGRAM_CHAT_ID", label="텔레그램 chat_id", kind="text", live=True, pattern=r"^-?\d{3,20}$"),
    dict(key="GOOGLE_SERVICE_ACCOUNT_FILE", label="Google 서비스 계정 JSON 경로", kind="text", live=True),
    dict(key="GOOGLE_SHEET_ID", label="Google 시트 ID", kind="text", live=True, pattern=r"^[A-Za-z0-9_\-]{20,80}$"),
    dict(key="CLAUDE_MODEL", label="Claude 모델", kind="model", live=True,
         options=["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]),
    dict(key="ANTHROPIC_API_KEY", label="Anthropic API 키 (비우면 Claude Code 로그인)", kind="secret", live=True),
    dict(key="DASHBOARD_PIN", label="휴대폰 접속 PIN (6~12자리 숫자)", kind="secret", live=False, pattern=r"^\d{6,12}$"),
]
ENV_PATH = ROOT / ".env"


class SettingsError(ValueError):
    pass


def _tsec(s: str) -> int:
    parts = [int(p) for p in s.split(":")]
    while len(parts) < 3:
        parts.append(0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _clean(field: dict, v):
    t, label = field["type"], field["label"]
    if t in ("int", "float"):
        if v in (None, "") and field.get("nullable"):
            return None
        try:
            x = int(float(v)) if t == "int" else round(float(v), 2)
        except (TypeError, ValueError):
            raise SettingsError(f"{label}: 숫자를 입력하세요")
        if not field["min"] <= x <= field["max"]:
            raise SettingsError(f"{label}: {field['min']:,} ~ {field['max']:,} 사이여야 합니다")
        return x
    if t == "enum":
        if v not in field["options"]:
            raise SettingsError(f"{label}: 허용되지 않는 값")
        return v
    if t == "time":
        if not isinstance(v, str) or not re.fullmatch(r"\d{2}:\d{2}(:\d{2})?", v):
            raise SettingsError(f"{label}: HH:MM 형식")
        v = v if len(v) == 8 else v + ":00"
        if not _tsec(field["min"]) <= _tsec(v) <= _tsec(field["max"]):
            raise SettingsError(f"{label}: {field['min'][:5]} ~ {field['max'][:5]} 사이여야 합니다")
        return v if field["key"].startswith("session") else v[:5]   # 감시 시간만 초 단위
    if t == "codes":
        codes = sorted({str(c) for c in (v or []) if str(c) in field["options"]} | set(field["required"]))
        return codes
    if t == "keywords":
        items = v if isinstance(v, list) else str(v or "").replace("\n", ",").split(",")
        kws = []
        for k in [*field["required"], *items]:
            k = str(k).strip()
            if k and len(k) <= 20 and k not in kws:
                kws.append(k)
        return kws[:30]
    raise SettingsError(f"알 수 없는 타입 {t}")


def _clean_holidays(v) -> list[str]:
    items = v if isinstance(v, list) else str(v or "").split()
    out = set()
    for d in items:
        d = str(d).strip()
        try:
            date.fromisoformat(d)
        except ValueError:
            raise SettingsError(f"휴장일 '{d}': YYYY-MM-DD 형식")
        out.add(d)
    return sorted(out)


class SettingsStore:
    def __init__(self, settings, defaults: dict):
        self.s = settings
        self.defaults = defaults                       # config/settings.json 원본
        self.path: Path = settings.data_dir / "settings.local.json"
        self.restart_required: set[str] = set()

    # ── 읽기 ──
    def overrides(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
        return {}

    def current(self, section: str) -> dict:
        return getattr(self.s, section)

    def describe(self, rebalance_mode: str) -> dict:
        out = {}
        for sec, fields in SCHEMA.items():
            cur = self.current(sec)
            rows = []
            for f in fields:
                val = rebalance_mode if f["key"] == "rebalance_mode" else cur.get(f["key"])
                rows.append(dict(f, value=val, default=self.defaults[sec].get(f["key"])))
            out[sec] = rows
        out["holidays"] = {"value": sorted(self.s.holidays), "default": sorted(self.defaults.get("holidays", []))}
        return out

    def env_describe(self) -> list[dict]:
        rows = []
        for f in ENV_SCHEMA:
            v = os.getenv(f["key"], "")
            row = {k: f[k] for k in ("key", "label", "kind", "live") if k in f}
            if "options" in f:
                row["options"] = f["options"]
            row["is_set"] = bool(v)
            if f["kind"] == "secret":
                row["value"] = None                     # 비밀값은 절대 내보내지 않음
            elif f["kind"] == "account":
                row["value"] = (v[:4] + "****") if v else ""
            else:
                row["value"] = v
            rows.append(row)
        return rows

    # ── 쓰기 ──
    def update(self, section: str, values: dict, autoinvest=None) -> list[tuple[str, object, object]]:
        """검증 → 즉시 적용(dict 제자리 갱신) → 저장. 바뀐 (라벨, 이전, 이후) 목록을 돌려준다."""
        if section == "holidays":
            new = _clean_holidays(values.get("holidays"))
            old = sorted(self.s.holidays)
            if new == old:
                return []
            self.s.holidays.clear()
            self.s.holidays.update(new)
            self._save("holidays", new)
            return [("휴장일", f"{len(old)}일", f"{len(new)}일")]
        if section not in SCHEMA:
            raise SettingsError("알 수 없는 설정 구역")
        fields = {f["key"]: f for f in SCHEMA[section]}
        cur = self.current(section)
        cleaned = {}
        for k, v in values.items():
            if k not in fields:
                raise SettingsError(f"바꿀 수 없는 항목: {k}")
            cleaned[k] = _clean(fields[k], v)
        merged = {**cur, **cleaned}
        if section == "engine":
            if _tsec(merged["session_start"]) >= _tsec(merged["session_end"]):
                raise SettingsError("감시 시작은 감시 종료보다 빨라야 합니다")
            for k in ("build_time", "rebalance_time"):
                if not _tsec(merged["session_start"]) <= _tsec(merged[k]) < _tsec(merged["session_end"]):
                    raise SettingsError(f"{fields[k]['label']}은 감시 시간 안이어야 합니다")
        if section == "autoinvest" and merged["min_trade_dev_pp"] >= merged["rebalance_band_pp"]:
            raise SettingsError("조정 최소 차이는 리밸런싱 허용 범위보다 작아야 합니다")
        changes = []
        for k, v in cleaned.items():
            if k == "rebalance_mode":
                old = autoinvest.rebalance_mode() if autoinvest else cur.get(k)
                if old != v and autoinvest:
                    autoinvest.set_rebalance_mode(v)
            else:
                old = cur.get(k)
                old = list(old) if isinstance(old, list) else old
            if old != v:
                changes.append((fields[k]["label"], old, v))
        for k, v in cleaned.items():
            if k == "rebalance_mode":
                continue
            if isinstance(cur.get(k), list):
                cur[k][:] = v                           # 다른 객체가 같은 리스트를 참조 중 → 제자리 갱신
            else:
                cur[k] = v
        self._save(section, {k: cur[k] for k in cur if not k.startswith("_") and k != "rebalance_mode"})
        return changes

    def reset(self, section: str) -> None:
        if section == "holidays":
            self.s.holidays.clear()
            self.s.holidays.update(self.defaults.get("holidays", []))
        else:
            cur = self.current(section)
            for k, v in self.defaults[section].items():
                if isinstance(cur.get(k), list):
                    cur[k][:] = list(v)
                else:
                    cur[k] = v
        ov = self.overrides()
        ov.pop(section, None)
        self._write(ov)

    def _save(self, section: str, values) -> None:
        ov = self.overrides()
        if section == "holidays":
            if values == sorted(self.defaults.get("holidays", [])):
                ov.pop("holidays", None)
            else:
                ov["holidays"] = values
        else:
            diff = {k: v for k, v in values.items() if self.defaults[section].get(k) != v}
            if diff:
                ov[section] = diff
            else:
                ov.pop(section, None)
        self._write(ov)

    def _write(self, ov: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(ov, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # ── .env ──
    def update_env(self, set_values: dict, clear: list[str]) -> tuple[list[str], list[str]]:
        """(.env 에 반영된 라벨들, 재시작이 필요한 라벨들)."""
        from dotenv import set_key, unset_key
        spec = {f["key"]: f for f in ENV_SCHEMA}
        todo: dict[str, str | None] = {}
        for k, v in (set_values or {}).items():
            if k not in spec:
                raise SettingsError(f"바꿀 수 없는 항목: {k}")
            v = str(v).strip()
            if not v:
                continue                                # 빈칸 = 그대로 둠
            if len(v) > 512 or "\n" in v or "\r" in v or "'" in v:
                raise SettingsError(f"{spec[k]['label']}: 허용되지 않는 문자")
            f = spec[k]
            if f["kind"] == "enum" and v not in f["options"]:
                raise SettingsError(f"{f['label']}: 허용되지 않는 값")
            if f["kind"] == "account" and not re.fullmatch(r"\d{8}(-?\d{2})?", v):
                raise SettingsError(f"{f['label']}: 숫자 8자리")
            if f["kind"] == "model" and not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{2,60}", v):
                raise SettingsError(f"{f['label']}: 모델 ID 형식이 아님")
            if f.get("pattern") and not re.fullmatch(f["pattern"], v):
                raise SettingsError(f"{f['label']}: 형식이 맞지 않음")
            todo[k] = v[:8] if f["kind"] == "account" else v
        for k in clear or []:
            if k not in spec:
                raise SettingsError(f"바꿀 수 없는 항목: {k}")
            if k == "BROKER":
                raise SettingsError("브로커는 지울 수 없습니다 (sim 또는 kis 선택)")
            todo[k] = None
        if not ENV_PATH.exists():
            ENV_PATH.write_text("", encoding="utf-8")
        changed, restart = [], []
        for k, v in todo.items():
            if v is None:
                unset_key(str(ENV_PATH), k)
                os.environ.pop(k, None)
            else:
                set_key(str(ENV_PATH), k, v, quote_mode="always")
                os.environ[k] = v
            changed.append(spec[k]["label"])
            if not spec[k]["live"]:
                restart.append(spec[k]["label"])
                self.restart_required.add(spec[k]["label"])
        return changed, restart


def load_overrides(settings) -> None:
    """load_settings 직후 호출: data/settings.local.json 의 값을 제자리 반영."""
    p = settings.data_dir / "settings.local.json"
    if not p.exists():
        return
    try:
        ov = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    for sec in ("safety", "engine", "autoinvest"):
        fields = {f["key"]: f for f in SCHEMA[sec]}
        for k, v in (ov.get(sec) or {}).items():
            if k in fields and k != "rebalance_mode":
                try:
                    getattr(settings, sec)[k] = _clean(fields[k], v)
                except SettingsError:
                    pass                                # 손상된 값은 무시하고 기본값 유지
    if "holidays" in ov:
        try:
            settings.holidays.clear()
            settings.holidays.update(_clean_holidays(ov["holidays"]))
        except SettingsError:
            pass
