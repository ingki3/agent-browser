"""WS-30b 항목 4(b): scroll 무효과 신호.

비교 시험 L07: 본문이 창보다 짧아 스크롤 자체가 안 되는 페이지에서 scroll 5번이 모두 success 로
돌아와 에이전트가 \"항목이 안 늘어난\" 이유를 몰랐다. `_scroll_once` 가 문서 높이만 봤기 때문.
이제 scrollY 전후를 비교해 `data.scrolled_px`(실제 이동량, 부호 있음)를 싣고, 움직이지 않았으면
`data.no_effect: true` + `data.hint` 를 싣는다. **성공/실패 판정과 reobserve_required 규칙은
그대로**(정보만 추가).
"""

from __future__ import annotations

import pytest

from contracts import ActionType

from test_scroll_navigation import requires_chromium  # noqa: E402,F401

SHORT = "<!doctype html><meta charset=utf-8><body style='margin:0'><p>짧은 본문</p></body>"
LONG = "<!doctype html><meta charset=utf-8><body>" + "<p>줄</p>" * 400 + "</body>"
#: 스크롤 끝에서 항목을 덧붙이는 페이지(높이 변화 → reobserve_required, 이동도 있음)
INFINITE = """<!doctype html><meta charset=utf-8><body><div id=list></div><script>
const list = document.getElementById('list');
function add(n){ for(let i=0;i<n;i++){ const p=document.createElement('p'); p.textContent='항목'; list.appendChild(p);} }
add(60);
addEventListener('scroll', () => { if (innerHeight + scrollY >= document.body.scrollHeight - 50) add(20); });
</script></body>"""


async def _scroll(html: str, args_list):
    from playwright.async_api import async_playwright

    from actions import ActionDispatcher, DispatchContext
    from perception import PerceptionEngine

    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        page = await b.new_page(viewport={"width": 800, "height": 600})
        await page.set_content(html)
        d = ActionDispatcher(DispatchContext(page=page, engine=PerceptionEngine()))
        out = [await d.dispatch(ActionType.SCROLL, a) for a in args_list]
        await b.close()
    return out


@requires_chromium
async def test_unscrollable_page_reports_no_effect_but_stays_success():
    (r,) = await _scroll(SHORT, [{"direction": "down", "distance": 500}])
    assert r.success is True, "판정 규칙은 그대로 — 정보만 추가"
    assert r.reobserve_required is False
    assert r.data["scrolled"] == 500, "기존 키(요청량)는 그대로"
    assert r.data["scrolled_px"] == 0
    assert r.data["no_effect"] is True
    assert "스크롤" in r.data["hint"]


@requires_chromium
async def test_long_page_reports_actual_pixels():
    down, up = await _scroll(LONG, [{"direction": "down", "distance": 500},
                                    {"direction": "up", "distance": 200}])
    assert down.success and down.data["scrolled_px"] == 500
    assert "no_effect" not in down.data and "hint" not in down.data
    assert up.data["scrolled_px"] == -200


@requires_chromium
async def test_top_of_page_scroll_up_is_no_effect():
    (r,) = await _scroll(LONG, [{"direction": "up", "distance": 300}])
    assert r.data["scrolled_px"] == 0 and r.data["no_effect"] is True


@requires_chromium
async def test_partial_scroll_at_bottom_reports_real_distance():
    """끝 근처에서는 요청보다 적게 움직인다 — 실제 이동량을 싣는다."""
    results = await _scroll(LONG, [{"direction": "down", "distance": 100_000},
                                   {"direction": "down", "distance": 500}])
    first, second = results
    assert 0 < first.data["scrolled_px"] < 100_000
    assert second.data["scrolled_px"] == 0 and second.data["no_effect"] is True


@requires_chromium
async def test_dynamic_load_keeps_reobserve_rule():
    (r,) = await _scroll(INFINITE, [{"direction": "down", "distance": 100_000}])
    assert r.success and r.reobserve_required is True, "높이 변화 → 재관찰 규칙 유지"
    assert r.data["scrolled_px"] > 0 and "no_effect" not in r.data


async def test_unreadable_scroll_position_does_not_claim_no_effect():
    """위치를 못 읽으면(가짜 페이지 등) scrolled_px·no_effect 를 싣지 않는다 — 추측 금지."""
    from actions import ActionDispatcher, DispatchContext

    class _Page:
        url = "http://fake.test/"

        async def evaluate(self, js):
            return 1000

        async def wait_for_timeout(self, ms):
            return None

    class _Engine:
        epoch = 0

    r = await ActionDispatcher(DispatchContext(page=_Page(), engine=_Engine())).dispatch(
        ActionType.SCROLL, {"direction": "down"})
    assert r.success and "scrolled_px" not in r.data and "no_effect" not in r.data
