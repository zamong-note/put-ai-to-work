"""모든 테스트 공통: 외부 공개 API(환율·경제지표)를 부르지 않는다 (오프라인 · 결과 고정)."""
import pytest


@pytest.fixture(autouse=True)
def _offline_macro(monkeypatch):
    from jangjung import macro

    async def offline(url, params=None):
        raise macro.MacroError("테스트: 외부 API 호출 안 함")
    monkeypatch.setattr(macro, "_get_json", offline)
