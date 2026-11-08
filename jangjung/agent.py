"""Claude Agent SDK 기반 대화 에이전트.

LLM의 역할은 '자연어 → 정형 규칙/요청'으로 바꾸고, 빠진 정보를 되묻고, 확인 카드를 띄우는 것까지다.
- 주문 도구는 LLM에게 주지 않는다. 주문은 엔진이 규칙·전략대로만 낸다.
- 등록·변경·자동투자 시작은 카드 버튼(사용자 클릭)으로만 확정된다.
- 내장 도구(Bash, 파일 읽기/쓰기, 웹)는 모두 끈다.
"""
from __future__ import annotations

import asyncio
import json
import logging

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, TextBlock,
                              create_sdk_mcp_server, tool)

from . import timeutil
from .autoinvest import RISK_QUESTIONS
from .rules import rule_line

log = logging.getLogger("jangjung")

SYSTEM_PROMPT = """너는 '장중대리'다. 회의로 바쁜 직장인 투자자 대신, 그가 정한 매매 규칙을 지키고 위험 단계에 맞춘 자동 투자를 관리하는 비서다.
지금은 한국투자증권 '모의투자' 계좌로만 운영한다.

## 네가 하는 일
1. 규칙 매매: 사용자의 말을 정형 규칙(종목·조건·수량·횟수)으로 바꾸고 propose_rules로 확인 카드를 띄운다.
2. 자동 투자: get_risk_questions로 성향 질문 → 답을 번호로 score_risk_profile → 추천 단계를 알려 주고 사용자가 최종 선택 → propose_autoinvest로 제안 카드.
3. 조회: 규칙 목록, 계좌, 자동투자 현황, 매매일지, 시세.

## 절대 규칙
- 너는 주문을 내지 않는다. 주문은 시스템 엔진이 등록된 규칙과 고정된 전략대로만 낸다. "지금 바로 사 줘" 같은 즉시 주문 요청은 "조건 규칙으로 등록"하는 방법을 안내한다.
- 종목을 추천하거나 고르지 않는다. 자동 투자 종목·비중은 시스템의 단계별 전략에 고정돼 있다. 시장 전망·투자 조언도 하지 않는다.
- 국내 상장 주식·ETF만. 해외 직접투자, 선물·옵션, 레버리지·인버스 ETF는 거절한다. 초단타·고빈도 요청도 거절한다.
- 추측 금지. 종목명이 모호하면 search_stock 결과 후보를 보여 주고 되묻는다(예: "삼성" → 삼성전자/삼성SDI/...). 수량·금액·조건 값이 빠졌으면 되묻는다.
- 등록·변경·자동투자 시작·재개는 카드의 버튼을 사용자가 눌러야 확정된다. 도구가 카드를 보냈으면 "확인 카드를 보냈어요, 버튼으로 확정해 주세요"처럼 짧게 말한다. 네가 대신 확정했다고 말하지 않는다.
- 도구가 errors를 돌려주면 그 내용을 사용자에게 쉽게 설명하고 필요한 정보를 묻는다.
- propose_autoinvest가 needs_user_reconfirm을 돌려주면 그 경고를 그대로 전하고, 사용자가 "그래도 진행"이라고 명확히 답했을 때만 mismatch_confirmed=true로 다시 호출한다.
- 사용자가 전체를 멈추길 원하면(“다 멈춰”, “전부 중지해 줘” 등) 즉시 emergency_stop을 호출한다.

## 규칙 해석 예시
- "SK하이닉스 수익률 15% 넘으면 절반 팔고, 산 가격보다 7% 떨어지면 전부 팔아 줘"
  → propose_rules 한 번에 두 규칙: (sell, return_at_or_above, 15, fraction 0.5, 1회), (sell, return_at_or_below, -7, fraction 1.0, 1회)
- "삼성전자 6만5천원 이하로 내려오면 100만원어치 사 줘" → (buy, price_at_or_below, 65000, amount 1000000, 1회)
- "네이버 22만원 넘으면 10주 팔아" → (sell, price_at_or_above, 220000, shares 10, 1회)
- 횟수를 말하지 않으면 1회. "매번", "두 번까지"처럼 말하면 max_triggers에 반영(최대 10).
- symbol은 반드시 search_stock으로 확인한 6자리 코드를 쓴다.

## 말투
텔레그램 메시지다. 짧은 평문 한국어, 마크다운 기호(**, #, 표)는 쓰지 않는다. 숫자는 1,000,000원처럼 쓴다.
"""

RULE_ITEM = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string", "description": "search_stock으로 확인한 6자리 종목코드"},
        "side": {"type": "string", "enum": ["buy", "sell"]},
        "condition": {"type": "string",
                      "enum": ["price_at_or_above", "price_at_or_below", "return_at_or_above", "return_at_or_below"],
                      "description": "price_*: 현재가(원) 기준, return_*: 평균매입가 대비 수익률(%) 기준"},
        "threshold": {"type": "number", "description": "가격(원) 또는 수익률(%). 손실은 음수(-7)"},
        "qty_mode": {"type": "string", "enum": ["shares", "fraction", "amount"],
                     "description": "shares=주수, fraction=보유 대비 비율(매도만, 0.5=절반, 1=전부), amount=금액(원, 매수만)"},
        "qty_value": {"type": "number"},
        "max_triggers": {"type": "integer", "minimum": 1, "maximum": 10},
    },
    "required": ["symbol", "side", "condition", "threshold", "qty_mode", "qty_value"],
}


def _ok(obj) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False, default=str)}]}


def _err(msg: str) -> dict:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def build_tools(app) -> list:
    @tool("search_stock", "종목명 또는 6자리 코드로 국내 종목을 찾는다. 후보가 여러 개면 사용자에게 되물어야 한다.",
          {"query": str})
    async def search_stock(args):
        rows = app.universe.search(args["query"])
        return _ok({"matches": rows, "ambiguous": len(rows) > 1, "count": len(rows)})

    @tool("get_quote", "종목 현재가를 조회한다.", {"symbol": str})
    async def get_quote(args):
        q = await app.broker.get_quote(args["symbol"])
        return _ok({"symbol": q.symbol, "name": app.universe.name(q.symbol), "price": q.price,
                    "prev_close": q.prev_close, "change_pct": q.change_pct, "halted": q.halted})

    @tool("get_account", "계좌 현금과 보유 종목(평균매입가, 수익률)을 조회한다. 자동투자 몫은 따로 표시한다.", {})
    async def get_account(args):
        a = await app.broker.get_account()
        auto = app.gateway.ledger.positions()
        hs = [{"symbol": h.symbol, "name": h.name, "qty": h.qty, "auto_invest_qty": auto.get(h.symbol, {}).get("qty", 0),
               "avg_price": h.avg_price, "cur_price": h.cur_price,
               "return_pct": round((h.cur_price / h.avg_price - 1) * 100, 2) if h.avg_price else None}
              for h in a.holdings.values()]
        return _ok({"cash": a.cash, "total_eval": a.total_eval, "holdings": hs, "broker": app.broker.name})

    @tool("list_rules", "등록된 규칙 목록. include_inactive=true면 완료·취소 포함.", {"include_inactive": bool})
    async def list_rules(args):
        rows = app.rules.list(bool(args.get("include_inactive")))
        return _ok({"rules": [rule_line(r) for r in rows], "ids": [r["id"] for r in rows]})

    @tool("propose_rules",
          "한 지시문에서 나온 규칙들을 검증하고 사용자에게 등록 확인 카드를 보낸다. 실제 등록은 사용자가 버튼을 눌러야 된다.",
          {"type": "object",
           "properties": {"source_text": {"type": "string", "description": "사용자 원문"},
                          "rules": {"type": "array", "items": RULE_ITEM, "minItems": 1, "maxItems": 5}},
           "required": ["source_text", "rules"]})
    async def propose_rules(args):
        return _ok(await app.rules.propose(args["source_text"], args["rules"]))

    @tool("propose_rule_change", "규칙 삭제/일시정지/재개 확인 카드를 보낸다.",
          {"type": "object",
           "properties": {"rule_ids": {"type": "array", "items": {"type": "integer"}, "minItems": 1},
                          "action": {"type": "string", "enum": ["cancel", "pause", "resume"]}},
           "required": ["rule_ids", "action"]})
    async def propose_rule_change(args):
        return _ok(await app.rules.propose_change(args["rule_ids"], args["action"]))

    @tool("get_risk_questions", "투자 성향 질문 목록(각 1~5번 보기).", {})
    async def get_risk_questions(args):
        return _ok({"questions": [{"no": i + 1, "q": q, "options": {str(j + 1): o for j, o in enumerate(opts)}}
                                  for i, (q, opts) in enumerate(RISK_QUESTIONS)],
                    "tiers": {t: f"{v['label']} · {v['summary']} · 손실한도 -{v['loss_limit_pct']}%"
                              for t, v in app.universe.tiers.items()}})

    @tool("score_risk_profile", "성향 질문 답(질문 순서대로 1~5 번호 배열)을 채점해 추천 위험 단계를 돌려준다.",
          {"type": "object", "properties": {"answers": {"type": "array", "items": {"type": "integer"}}},
           "required": ["answers"]})
    async def score_risk_profile(args):
        try:
            return _ok(app.autoinvest.save_profile(args["answers"]))
        except ValueError as e:
            return _err(str(e))

    @tool("propose_autoinvest",
          "자동 투자 제안 카드를 보낸다(금액, 사용자가 고른 단계). 이미 운용 중이면 단계 변경 제안이 된다.",
          {"type": "object",
           "properties": {"amount": {"type": "number", "description": "원 단위"},
                          "tier": {"type": "integer", "minimum": 1, "maximum": 5},
                          "mismatch_confirmed": {"type": "boolean"}},
           "required": ["amount", "tier"]})
    async def propose_autoinvest(args):
        return _ok(await app.autoinvest.propose(args["amount"], args["tier"], bool(args.get("mismatch_confirmed"))))

    @tool("get_autoinvest_status", "자동 투자 현황: 단계, 원금, 평가액, 종목별 현재/목표 비중.", {})
    async def get_autoinvest_status(args):
        v = await app.autoinvest.valuation()
        if not v:
            return _ok({"status": "없음"})
        st = v["state"]
        return _ok({"tier": st["tier"], "status": st["status"], "principal": st["principal"], "total": round(v["total"]),
                    "pnl_pct": round(v["pnl_pct"], 2), "cash": round(v["cash"]),
                    "build": f"{st['tranches_done']}/{st['tranches_total']}", "rebalance_mode": app.autoinvest.rebalance_mode(),
                    "rows": [{k: (round(r[k], 2) if isinstance(r[k], float) else r[k])
                              for k in ("name", "qty", "price", "weight", "target", "dev")} for r in v["rows"]]})

    @tool("set_rebalance_mode", "리밸런싱 방식: auto(기본, 자동 실행) 또는 confirm(매번 확인).",
          {"type": "object", "properties": {"mode": {"type": "string", "enum": ["auto", "confirm"]}}, "required": ["mode"]})
    async def set_rebalance_mode(args):
        app.autoinvest.set_rebalance_mode(args["mode"])
        return _ok({"rebalance_mode": args["mode"]})

    @tool("get_journal", "최근 주문 기록(근거, 판단 시세, 체결가).", {"limit": int})
    async def get_journal(args):
        n = max(1, min(int(args.get("limit") or 10), 30))
        rows = app.db.q("SELECT created_at,source,name,side,qty,decision_price,filled_qty,avg_fill_price,status,reason "
                        "FROM orders ORDER BY id DESC LIMIT ?", (n,))
        return _ok({"orders": rows})

    @tool("get_safety_limits", "현재 안전 한도와 전체 중지 상태.", {})
    async def get_safety_limits(args):
        return _ok({"limits": {k: v for k, v in app.settings.safety.items() if not k.startswith("_")},
                    "halted": app.control.halted(), "suspended_symbols": app.control.suspended()})

    @tool("emergency_stop", "모든 규칙 매매와 자동 투자를 즉시 중지한다.", {"reason": str})
    async def emergency_stop(args):
        return _ok({"result": await app.emergency_stop(args.get("reason") or "사용자 요청")})

    @tool("request_resume", "전체 중지 해제 확인 카드를 보낸다.", {})
    async def request_resume(args):
        return _ok({"result": await app.request_resume()})

    return [search_stock, get_quote, get_account, list_rules, propose_rules, propose_rule_change, get_risk_questions,
            score_risk_profile, propose_autoinvest, get_autoinvest_status, set_rebalance_mode, get_journal,
            get_safety_limits, emergency_stop, request_resume]


class Agent:
    SERVER = "jj"

    def __init__(self, app):
        self.app = app
        self.tools = build_tools(app)
        self.client: ClaudeSDKClient | None = None
        self._lock = asyncio.Lock()

    def options(self) -> ClaudeAgentOptions:
        server = create_sdk_mcp_server(name=self.SERVER, version="0.1.0", tools=self.tools)
        return ClaudeAgentOptions(
            system_prompt=SYSTEM_PROMPT,
            mcp_servers={self.SERVER: server},
            tools=[],                                         # 내장 도구 전부 끔
            allowed_tools=[f"mcp__{self.SERVER}__{t.name}" for t in self.tools],
            permission_mode="dontAsk",                        # 허용 목록 밖 도구는 거부
            strict_mcp_config=True,
            setting_sources=[],                               # 사용자/프로젝트 설정 로드 안 함
            model=self.app.settings.claude_model,
            thinking={"type": "adaptive"},
            max_turns=20,
            cwd=str(self.app.settings.data_dir),
        )

    async def _ensure(self) -> ClaudeSDKClient:
        if self.client is None:
            self.client = ClaudeSDKClient(self.options())
            await self.client.connect()
        return self.client

    async def ask(self, text: str) -> str:
        async with self._lock:
            now = timeutil.now()
            eng = self.app.settings.engine
            sess = "장중" if timeutil.in_session(now, self.app.settings.holidays, eng["session_start"], eng["session_end"]) else "장외"
            prompt = f"[{now.strftime('%Y-%m-%d %H:%M')} KST · {sess}]\n{text}"
            try:
                client = await self._ensure()
                await client.query(prompt)
                parts: list[str] = []
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for b in msg.content:
                            if isinstance(b, TextBlock) and b.text.strip():
                                parts.append(b.text.strip())
                    elif isinstance(msg, ResultMessage):
                        if msg.is_error:
                            log.warning("agent result error: %s", msg.result)
                        log.info("agent turn: %s turns, $%s", msg.num_turns, msg.total_cost_usd)
                return parts[-1] if parts else "(응답 없음)"
            except Exception as e:  # noqa: BLE001
                log.exception("agent error")
                await self.reset()
                return (f"AI 해석 기능에 문제가 생겼어요 ({type(e).__name__}: {e}).\n"
                        "/rules, /status, /stop 같은 명령과 장중 감시는 계속 동작합니다.")

    async def reset(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self.client = None
