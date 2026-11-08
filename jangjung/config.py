"""설정 로딩: 비밀값은 .env, 운영 파라미터는 config/*.json."""
from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
load_dotenv(ROOT / ".env")


def _load_json(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return json.load(f)


@dataclass
class Settings:
    data_dir: Path
    broker: str
    safety: dict
    engine: dict
    autoinvest: dict
    sim: dict
    holidays: set[str]
    strategies: dict
    kis_app_key: str = ""
    kis_app_secret: str = ""
    kis_account: str = ""
    kis_account_product: str = "01"
    telegram_token: str = ""
    telegram_chat_id: str = ""
    google_sa_file: str = ""
    google_sheet_id: str = ""
    claude_model: str = "claude-opus-5-5"
    report: dict = field(default_factory=lambda: {"email_daily": "on", "email_weekly": "on", "email_monthly": "on",
                                                  "channel": "both", "send_time": "16:00"})
    notify: dict = field(default_factory=lambda: {"fill": "on", "auto": "on", "summary": "on", "settings": "on",
                                                  "report": "on", "strategy": "on"})
    overseas: dict = field(default_factory=lambda: {"enabled": "off", "scope": "major3", "countries": ["US"],
                                                    "limit_slippage_pct": 1.0, "fx_auto": "on", "fx_usd": 1400, "fx_jpy100": 930,
                                                    "fx_cny": 195, "fx_hkd": 180, "fx_vnd100": 5.5})
    overseas_holidays: dict = field(default_factory=dict)
    ai_strategy: dict = field(default_factory=lambda: {"mode": "propose", "cadence": "weekly", "run_time": "10:30",
                                                       "max_tilt_pp": 10, "max_turnover_pct": 20})
    overseas_symbols: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


def load_settings(data_dir: str | Path | None = None) -> Settings:
    s = _load_json("settings.json")
    data = Path(data_dir or os.getenv("JJ_DATA_DIR") or ROOT / "data")
    data.mkdir(parents=True, exist_ok=True)
    st = Settings(
        data_dir=data,
        broker=os.getenv("BROKER", "sim").strip().lower(),
        safety=s["safety"],
        engine=s["engine"],
        autoinvest=s["autoinvest"],
        sim=s["sim"],
        holidays=set(s.get("holidays", [])),
        strategies=_load_json("strategies.json"),
        kis_app_key=os.getenv("KIS_APP_KEY", ""),
        kis_app_secret=os.getenv("KIS_APP_SECRET", ""),
        kis_account=os.getenv("KIS_ACCOUNT", ""),
        kis_account_product=os.getenv("KIS_ACCOUNT_PRODUCT", "01"),
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        google_sa_file=os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", ""),
        google_sheet_id=os.getenv("GOOGLE_SHEET_ID", ""),
        claude_model=os.getenv("CLAUDE_MODEL", "claude-opus-5-5") or "claude-opus-5-5",
        extra={"defaults": copy.deepcopy(s)},
        **{k: s[k] for k in ("report", "notify", "overseas", "overseas_holidays", "ai_strategy") if k in s},
        overseas_symbols=_load_json("overseas.json")["symbols"] if (CONFIG_DIR / "overseas.json").exists() else {},
    )
    from .settings_store import load_overrides   # 화면에서 바꾼 값(data/settings.local.json) 반영
    load_overrides(st)
    return st
