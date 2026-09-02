"""PRD 5.3 비밀번호 마스킹 규정 준수 테스트 (WS-19).

PRD 525행이 규정한 4대 마스킹 대상 중 2건이 미준수였다.

    [누출] 비밀번호 인풋 필드   {"text": "..."} -> 그대로
    [ OK ] Authorization 헤더
    [누출] Set-Cookie 헤더      부분 마스킹만, 원본 토큰 잔존
    [ OK ] URL 쿼리스트링 토큰

원인:
1. 마스킹기는 키 이름(password/secret/...)으로 판정하는데 액션
   파라미터의 키는 `text`라는 중립적 이름이다. 값도 평범한 문자열이라
   정규식으로 잡히지 않는다.
2. 헤더 규칙은 `set-cookie: v` 문자열만 매칭한다. 구조체가
   {"Set-Cookie": "session=..."} 처럼 키/값으로 분리돼 있으면 미적용.

한계(중요): 이 수정은 트레이스 기록 경로만 덮는다. Playwright MCP도
같은 문제를 겪었고, 응답 마스킹을 넣은 뒤 1년이 지나서야 콘솔 로그
경로를 막았으며 트레이스/HAR은 아직 미해결이다. 마스킹은 방어의 한
겹일 뿐 보안 경계가 아니다.
"""

from __future__ import annotations

import json

import pytest

from interface.observability import StepRecord
from security import mask_mapping, mask_text

SECRET = "P@ssw0rd!Secret123"
TOKEN = "sk-abcdef1234567890abcdef1234567890"


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            b.close()
        return True
    except Exception:  # noqa: BLE001
        return False


requires_chromium = pytest.mark.skipif(
    not _chromium_available(), reason="Chromium 바이너리 없음"
)


def _record(action_input, sensitive=False):
    return StepRecord(
        correlation_id="c1",
        step=1,
        action="type_text",
        snapshot_epoch=0,
        success=True,
        latency_ms=10.0,
        observation_tokens=100,
        observation_summary="",
        action_input=action_input,
        sensitive_input=sensitive,
    )


# ---------------------------------------------------------------------------
# 1. PRD 525행 — 4대 마스킹 대상
# ---------------------------------------------------------------------------


def test_authorization_header_masked():
    out = str(mask_mapping({"headers": {"Authorization": f"Bearer {TOKEN}"}}))
    assert TOKEN not in out


def test_set_cookie_in_structured_dict_masked():
    """구조체로 분리된 Set-Cookie도 마스킹돼야 한다.

    수정 전: 'session=sk-abc...7890; HttpOnly' — 부분 마스킹만 되고
    원본 토큰이 그대로 남았다.
    """
    out = str(mask_mapping({"headers": {"Set-Cookie": f"session={TOKEN}"}}))
    assert TOKEN not in out, "Set-Cookie 값에 원본 토큰이 남았습니다"


def test_cookie_in_structured_dict_masked():
    out = str(mask_mapping({"headers": {"Cookie": f"sid={TOKEN}"}}))
    assert TOKEN not in out


def test_url_query_token_masked():
    out = str(mask_mapping({"url": f"https://x.com/cb?access_token={TOKEN}"}))
    assert TOKEN not in out


def test_password_field_input_masked():
    """비밀번호 필드에 입력한 값은 키 이름과 무관하게 지워야 한다.

    액션 파라미터의 키는 `text`라 키 기반 규칙에 걸리지 않는다.
    """
    rec = _record(
        {"element_id": "@e2", "epoch": 0, "text": SECRET},
        sensitive=True,
    )
    line = json.dumps(rec.to_masked_dict(), ensure_ascii=False)
    assert SECRET not in line, "비밀번호가 트레이스에 평문으로 남았습니다"


# ---------------------------------------------------------------------------
# 2. 과잉 마스킹 방지
# ---------------------------------------------------------------------------


def test_normal_input_is_preserved():
    """일반 입력까지 지우면 트레이스가 쓸모없어진다."""
    rec = _record({"element_id": "@e1", "epoch": 0, "text": "myusername"})
    line = json.dumps(rec.to_masked_dict(), ensure_ascii=False)
    assert "myusername" in line, "일반 입력까지 마스킹됐습니다"


def test_non_value_keys_preserved_on_sensitive_step():
    """민감 스텝이라도 element_id/epoch는 남아야 디버깅이 된다."""
    rec = _record(
        {"element_id": "@e2", "epoch": 7, "text": SECRET},
        sensitive=True,
    )
    out = rec.to_masked_dict()["action_input"]
    assert out["element_id"] == "@e2"
    assert out["epoch"] == 7


def test_existing_pii_rules_unchanged():
    """기존 PII 규칙에 회귀가 없어야 한다."""
    assert mask_text("901231-1234567").text != "901231-1234567"
    assert mask_text("4111-1111-1111-1111").text != "4111-1111-1111-1111"
    assert mask_text("user@example.com").text != "user@example.com"


# ---------------------------------------------------------------------------
# 3. 디스패처 판정
# ---------------------------------------------------------------------------


@requires_chromium
async def test_dispatcher_detects_password_field():
    """비밀번호 필드는 role로 구분되지 않는다 — DOM을 봐야 한다.

    실측: input[type=password]와 일반 텍스트 입력이 모두 role=textbox.
    """
    from playwright.async_api import async_playwright

    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content(
            "<form>"
            "<input id='u' type='text' placeholder='아이디'>"
            "<input id='p' type='password' placeholder='비밀번호'>"
            "</form>"
        )

        engine = PerceptionEngine()
        cdp = await context.new_cdp_session(page)
        disp = ActionDispatcher(
            DispatchContext(page=page, engine=engine, cdp=cdp)
        )
        obs = await engine.observe_page(page=page, prune_top_n=20)

        roles = {e.name: e.role for e in obs.elements}
        results = {}
        for el in obs.elements:
            handle = engine.get_handle(el.element_id)
            if handle is not None:
                results[el.name] = await disp.is_sensitive_field(handle)

        await browser.close()

    assert roles.get("아이디") == roles.get("비밀번호") == "textbox", (
        "전제가 바뀌었습니다 — role로 구분된다면 이 헬퍼는 불필요합니다"
    )
    assert results.get("비밀번호") is True, "비밀번호 필드를 못 잡았습니다"
    assert results.get("아이디") is False, "일반 필드를 민감으로 오판했습니다"
