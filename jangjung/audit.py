"""성공 기준 점검: '규칙·전략과 다른 주문 0건', '조건 충족 후 1분 안에 주문'.

모든 주문 기록(근거·판단 시세)을 규칙/전략 정의와 다시 대조한다.
"""
from __future__ import annotations

import json
from datetime import datetime

from .rules import CONDITIONS


def audit(db, universe) -> dict:
    violations, latencies = [], []
    orders = db.q("SELECT * FROM orders ORDER BY id")
    tier_syms = {s for t in universe.tiers.values() for s in t["targets"]}
    for o in orders:
        ev = json.loads(o["evidence"] or "{}")
        tag = f"주문#{o['id']} {o['name']} {o['side']} {o['qty']}주"
        if not o["reason"]:
            violations.append(f"{tag}: 근거 없음")
        if universe.product_blocked(o["symbol"]):
            violations.append(f"{tag}: 금지 상품")
        if o["source"] == "rule":
            r = db.one("SELECT * FROM rules WHERE id=?", (int(o["ref_id"]),))
            if not r:
                violations.append(f"{tag}: 연결된 규칙 없음")
                continue
            if r["side"] != o["side"] or r["symbol"] != o["symbol"]:
                violations.append(f"{tag}: 규칙과 종목/방향 불일치")
            m = ev.get("metric")
            if m is None or r["condition"] not in CONDITIONS:
                violations.append(f"{tag}: 조건 근거값 없음")
            else:
                ok = m >= r["threshold"] if r["condition"].endswith("above") else m <= r["threshold"]
                if not ok:
                    violations.append(f"{tag}: 판단값 {m}이 규칙 조건 {r['condition']} {r['threshold']}을 만족하지 않음")
            if ev.get("limit_issues") and not ev.get("over_limit_confirmed"):
                violations.append(f"{tag}: 한도 초과인데 확인 없이 주문")
            if o["detected_at"]:
                latencies.append((datetime.fromisoformat(o["created_at"]) -
                                  datetime.fromisoformat(o["detected_at"])).total_seconds())
        elif o["source"] in ("auto_build", "rebalance"):
            if o["symbol"] not in tier_syms:
                violations.append(f"{tag}: 전략 후보 밖 종목")
            if "target_pct" not in ev:
                violations.append(f"{tag}: 목표 비중 근거 없음")
        elif o["source"] == "ai_strategy":
            # AI 전략 주문: 후보 종목 · 승인/자동 실행된 전략의 목표 비중과 일치해야 함
            s = db.one("SELECT * FROM strategies WHERE id=?", (int(o["ref_id"]),)) if str(o["ref_id"]).isdigit() else None
            targets = json.loads(s["targets"]) if s and s["targets"] else None
            if o["symbol"] not in universe.candidates:
                violations.append(f"{tag}: 전략 후보 밖 종목")
            if targets is None:
                violations.append(f"{tag}: 연결된 AI 전략 없음")
            elif abs(float(ev.get("target_pct", -1)) - float(targets.get(o["symbol"], 0))) > 0.05:
                violations.append(f"{tag}: AI 전략 목표 비중과 다름")
        else:
            violations.append(f"{tag}: 알 수 없는 주문 출처 {o['source']}")
    return {"orders": len(orders), "violations": violations,
            "max_latency_sec": max(latencies) if latencies else None,
            "over_60s": sum(1 for x in latencies if x > 60)}
