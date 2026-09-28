"""WS-30b 항목 4(c): 요소 이름 규칙 사본 통일.

WS-30 R1 이 관찰 엔진의 이름 규칙(`perception.sanitizer.ACCESSIBLE_NAME_JS`)을 HITL 게이트와
공유했지만, staleness 검증(`actions.verification.STALENESS_CHECK_SCRIPT`)과 closed shadow CDP
추출(`perception.shadow_dom._EXTRACT_FN`)에는 옛 사본(aria-label > 텍스트 > placeholder > title)이
남아 있었다. 그래서 label·aria-labelledby·value·alt·name·id 로 이름이 정해지는 요소는 관찰 이름과
검증 이름이 달라 NAME_CHANGED(= 불필요한 자가 치유, 실패 시 E_TOCTOU_MISMATCH)로 갔다.

여기서는 요소 종류마다 관찰 이름 == staleness 이름 == shadow 추출 이름인지 본다.
"""

from __future__ import annotations

import pytest

from actions.verification import STALENESS_CHECK_SCRIPT, StalenessReason, verify_staleness
from perception.sanitizer import ACCESSIBLE_NAME_JS
from perception.shadow_dom import _EXTRACT_FN

from test_scroll_navigation import requires_chromium  # noqa: F401

#: 이름 규칙 갈래별 요소 (id=t). 모두 관찰 대상(상호작용 요소)이다.
KINDS = {
    "aria_label": "<button id=t aria-label='결제하기'>→</button>",
    "labelledby": "<span id=l>다음 단계</span><button id=t aria-labelledby=l>→</button>",
    "label_for": "<label for=t>아이디</label><input id=t>",
    "input_submit_value": "<input id=t type=submit value='주문 확정'>",
    "input_button_value": "<input id=t type=button value='보기'>",
    "image_alt": "<input id=t type=image alt='검색' style='width:30px;height:20px'>",
    "placeholder": "<input id=t placeholder='검색어'>",
    "title_only": "<button id=t title='닫기'></button>",
    "name_only": "<input id=t name=email>",
    "id_only": "<input id=t>",
    "text": "<button id=t>장바구니 담기</button>",
    "long_text": "<a id=t href='#'>" + "가" * 300 + "</a>",
}


def test_copies_share_the_single_rule():
    """사본이 아니라 같은 상수를 끼워 넣는다(소스 수준 확인)."""
    body = ACCESSIBLE_NAME_JS.strip()
    assert body in STALENESS_CHECK_SCRIPT
    assert body in _EXTRACT_FN
    for src in (STALENESS_CHECK_SCRIPT, _EXTRACT_FN):
        assert "const ariaName = el.getAttribute('aria-label');" not in src, "옛 사본 잔존"


@requires_chromium
@pytest.mark.parametrize("kind", sorted(KINDS))
async def test_observe_name_equals_staleness_name(kind):
    from playwright.async_api import async_playwright

    from perception import PerceptionEngine

    html = "<!doctype html><meta charset=utf-8><body>" + KINDS[kind] + "</body>"
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        page = await b.new_page()
        await page.set_content(html)
        engine = PerceptionEngine()
        obs = await engine.observe_page(page=page, prune_top_n=50)
        handle = next(h for h in engine.handles.values() if h.css_path.endswith("#t"))
        probe = await page.evaluate(STALENESS_CHECK_SCRIPT, {"cssPath": handle.css_path})
        verdict = await verify_staleness(page, handle, obs.snapshot_epoch)
        await b.close()
    assert probe["name"] == handle.name, (kind, probe["name"], handle.name)
    assert verdict.fresh, (kind, verdict.reason, verdict.detail)
    assert verdict.reason is StalenessReason.FRESH


@requires_chromium
@pytest.mark.parametrize("kind", sorted(KINDS))
async def test_shadow_extract_name_equals_observe_name(kind):
    """같은 요소를 CDP callFunctionOn(_EXTRACT_FN) 으로 읽어도 관찰과 같은 이름."""
    from playwright.async_api import async_playwright

    from perception import PerceptionEngine

    html = "<!doctype html><meta charset=utf-8><body>" + KINDS[kind] + "</body>"
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        page = await b.new_page()
        await page.set_content(html)
        engine = PerceptionEngine()
        await engine.observe_page(page=page, prune_top_n=50)
        handle = next(h for h in engine.handles.values() if h.css_path.endswith("#t"))
        cdp = await page.context.new_cdp_session(page)
        doc = await cdp.send("DOM.getDocument", {"depth": -1})
        node = await cdp.send("DOM.querySelector", {"nodeId": doc["root"]["nodeId"], "selector": "#t"})
        obj = await cdp.send("DOM.resolveNode", {"nodeId": node["nodeId"]})
        res = await cdp.send("Runtime.callFunctionOn", {
            "objectId": obj["object"]["objectId"], "functionDeclaration": _EXTRACT_FN,
            "returnByValue": True,
        })
        await b.close()
    got = (res.get("result") or {}).get("value") or {}
    if kind == "title_only":
        # 크기 0 버튼은 추출 대상이 아니다(보이지 않음 — 기존 규칙). 관찰 쪽 이름만 확인.
        assert handle.name == "닫기"
        return
    assert got.get("name") == handle.name, (kind, got.get("name"), handle.name)
