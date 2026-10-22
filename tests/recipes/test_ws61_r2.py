"""WS-61 R2 — 재검증 FAIL 2건 회귀.

* BLOCKING 1: 한 쿼리 값·경로 조각의 디코딩 불능(%FF)·제어/서식 문자(%00·%E2%80%8B)가 같은 URL 의 다른 값 PII 검사를
  끄던 문제 — 구성요소별 디코드·검사(나쁜 구성요소는 원문 그대로 검사)하고, saved page 저장은 거부(fail-closed).
  레시피 이름 경로도 같은 helper(store._pii) — 거부.
* BLOCKING 2: 창/복구 보고(cap 뒤 부착)까지 포함한 최종 직렬화 크기로 후보 제거·최종 cap 을 판단.
"""

from __future__ import annotations

from types import SimpleNamespace as NS
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest

from contracts import ActionResult, ActionType
from recipes import saved_pages as sp
from recipes import store as rs
from recipes.saved_pages import SavedPageError, check_name, check_url

from test_ws38b_autosave import _desc, _entry, _nav
from test_ws61_r1 import URL, _route_server, _svc

POISONS = ["%FF", "%00", "%E2%80%8B", "%C3%28", "%7F", "%E2%80%AE"]

# ---------------------------------------------------------------------------
# BLOCKING 1 — 구성요소별 디코드·검사, 디코딩 불능은 저장 거부
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tail", [
    "?q=010+1234+5678&x=%FF",
    "?q=010+1234+5678&x=%00",
    "?q=hong%2540example.com&x=%FF",
    "?q=010+1234+5678&x=%E2%80%8B",
    "?x=%FF&q=010%252D1234%252D5678",
    "a#x=%00&tel=010+9876+5432",
])
def test_r2_bad_neighbor_does_not_hide_query_pii(tail):
    """검증 재현(test_pii_bad_neighbor) — 다른 값의 %FF·%00 이 PII 검사를 취소하지 않는다."""
    url = "https://shop.test/" + tail
    assert rs._pii(url)  # 구성요소별 검사로 PII 자체를 찾는다(거부 이유가 디코딩 불능만이 아님)
    with pytest.raises(SavedPageError):
        check_url(url)


def test_r2_component_isolation_finds_pii_in_decoded_neighbor():
    """나쁜 구성요소는 그대로 두고 나머지는 디코드한 형태가 나와야 한다(전체 디코드 실패로 원문만 보지 않음)."""
    forms = rs._decoded_forms("https://shop.test/?q=010%252D1234%252D5678&x=%FF")
    assert "https://shop.test/?q=010-1234-5678&x=%FF" in forms
    assert rs._undecodable("https://shop.test/?q=a&x=%FF")
    assert not rs._undecodable("https://shop.test/?q=%EB%85%B8%ED%8A%B8%EB%B6%81+%EA%B0%80%EB%B0%A9")


@pytest.mark.parametrize("poison", POISONS)
def test_r2_undecodable_component_alone_is_refused(poison):
    """PII 가 없어도 구성요소 디코딩 불능·제어/서식 문자는 저장 거부(fail-closed) — 경로·쿼리·해시 모두."""
    for url in (f"https://shop.test/?x={poison}", f"https://shop.test/p/{poison}/1", f"https://shop.test/a#k={poison}"):
        with pytest.raises(SavedPageError) as exc:
            check_url(url)
        assert str(exc.value) == sp.E_DECODE


def test_r2_remember_page_keeps_nothing_for_bad_neighbor(tmp_path):
    from recipes.saved_pages import SavedPageStore

    path = tmp_path / sp.FILE_NAME
    srv, svc = _svc()
    svc._store = SavedPageStore(path)
    for poison in ("%FF", "%00", "%E2%80%8B"):
        srv.page.url = "https://shop.test/?q=010+1234+5678&x=" + poison
        out = svc.tool({"op": "remember_page", "name": "검색"})
        assert not out["success"]
    assert len(svc.store) == 0
    assert not path.exists() or "1234" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("poison", ["%FF", "%00", "%E2%80%8B"])
def test_r2_recipe_name_bad_neighbor_refused_and_not_persisted(poison, tmp_path):
    """형제 경로(sibling_r1_poison): 레시피 이름 속 URL 의 PII 가 다른 값의 %FF 등에 가려지지 않는다."""
    url = "http://mock.test/list"
    name = "https://shop.test/?q=010+1234+5678&x=" + poison
    with pytest.raises(rs.RecipeError):
        rs.compile_recipe(name, [_nav(url), _entry("click", {}, desc=_desc("계속"), url=url)])
    # 대상 이름(페이지 유래)도 같은 helper — 거부.
    with pytest.raises(rs.RecipeError):
        rs.compile_recipe("문의", [_nav(url), _entry("click", {}, desc=_desc(name, slot=False), url=url)])
    # PII 없는 디코딩 불능 이름도 레시피 저장 쪽은 거부(fail-closed).
    with pytest.raises(rs.RecipeError):
        rs.compile_recipe("https://shop.test/?x=" + poison + " 정리",
                          [_nav(url), _entry("click", {}, desc=_desc("계속"), url=url)])


@pytest.mark.parametrize("url", [
    "https://shop.test/search?q=%EB%85%B8%ED%8A%B8%EB%B6%81+%EA%B0%80%EB%B0%A9&sort=rel",
    "https://shop.test/search?q=c%2B%2B+books&page=2",
    "https://shop.test/search?q=laptop+bag&sort=price",
    "https://shop.test/p/red-leather-wallet",
    "https://shop.test/p/a+b/1234",
    "https://shop.test/search?q=%25EB%2585%25B8",  # 이중 인코딩된 정상 한글
    "https://shop.test/sale?pct=50%25",
])
def test_r2_ordinary_urls_still_accepted(url):
    assert check_url(url) == url


@pytest.mark.parametrize("name", [
    "주문 조회", "노트북 가방 검색", "c++ books", "50% 할인 상품", "100%정품 할인",
    "https://shop.test/search?q=c%2B%2B+books", "https://shop.test/p/red-leather-wallet 상세",
])
def test_r2_ordinary_names_not_over_refused(name):
    url = "http://mock.test/list"
    rec = rs.compile_recipe(name, [_nav(url), _entry("click", {}, desc=_desc("계속"), url=url)])
    assert rec["name"] == name
    assert check_name(name) == name


# ---------------------------------------------------------------------------
# BLOCKING 2 — 창/복구 보고까지 포함한 최종 크기
# ---------------------------------------------------------------------------

LIMIT = 20_000
WINDOW_INFO = {"state": "headless", "mode": "on-demand"}
LAST_WINDOW = {"reopened": True, "tabs": [], "hint": "복원되었습니다"}


def _obs(title: str) -> Dict[str, Any]:
    return {"title": title, "url": URL, "snapshot_epoch": 1, "token_count": 1000,
            "elements": [{"element_id": f"@e{i}", "role": "link", "name": "정상 기사 " + "가" * 64,
                          "bbox": {"x": 0, "y": i * 20, "width": 100, "height": 20}, "interactable": True,
                          "score": 1.0} for i in range(65)],
            "axtree_summary": "\n".join(f"[@e{i}] link 정상 기사" for i in range(65))}


def _expected(res, title: str, saved: Any = None) -> ActionResult:
    """창 보고를 붙인 최종 모양(같은 키 순서)."""
    data: Dict[str, Any] = {"observation": _obs(title), "last_http_status": 200}  # 실제 결과와 같은 키 순서
    if saved is not None:
        data["saved_pages"] = saved
    out = res(ActionType.OBSERVE_PAGE, **data)
    out.data["window"] = {**LAST_WINDOW, **WINDOW_INFO}
    return out


async def _route(title: str, candidates: bool, recovered: Any = False):
    from interface.mcp_server import tool_name

    s, _page, res = _route_server()
    s.max_result_chars = LIMIT
    s._window = NS(info=lambda: dict(WINDOW_INFO))
    s._window_unreported = True
    s._recovered_notice = recovered
    s._last_window_result = dict(LAST_WINDOW)
    s._on_demand_enter = AsyncMock(return_value=None)
    s._note_tabs = lambda: None
    if candidates:
        s._saved_pages.store.add("default", "https://shop.test/other", "다른 페이지")
    s._call_tool = AsyncMock(return_value=res(ActionType.OBSERVE_PAGE, observation=_obs(title)))
    out = await s._route_tool_call(tool_name(ActionType.OBSERVE_PAGE), {})
    return s, out, res


async def _candidate_block() -> Dict[str, Any]:
    """실제 서비스가 붙이는 후보 블록(id 포함)."""
    _s, out, _res = await _route("", True)
    return out.data["saved_pages"]


def _title_for(res, target: int, saved: Any) -> str:
    """창 보고 포함 최종 크기가 정확히 target 이 되는 제목."""
    from interface.server_envelope import envelope_json

    base = len(envelope_json(_expected(res, "", saved)))
    return "가" * (target - base)


@pytest.mark.asyncio
async def test_r2_window_report_counted_before_dropping_candidates():
    """검증 재현(test_window_cap): 창 보고를 빼고 재면 후보가 남아 최종 20091 > 20000 이 됐다."""
    from interface.server_envelope import envelope_json

    block = await _candidate_block()
    _s, _o, res = await _route("", False)
    # 측정 조건: 창 보고 없이는 후보 포함이 상한 안, 창 보고를 붙이면 상한을 넘는다.
    title = "가" * (LIMIT - len(envelope_json(res(ActionType.OBSERVE_PAGE, observation=_obs(""), saved_pages=block))) - 1)
    assert len(envelope_json(res(ActionType.OBSERVE_PAGE, observation=_obs(title), saved_pages=block))) < LIMIT
    assert len(envelope_json(_expected(res, title, block))) > LIMIT
    _s, plain, _ = await _route(title, False)
    s, saved, _ = await _route(title, True)
    assert len(envelope_json(saved)) <= LIMIT
    assert "saved_pages" not in saved.data and "window" in saved.data
    assert "truncated" not in saved.data
    assert len(saved.data["observation"]["elements"]) == len(plain.data["observation"]["elements"]) == 65
    assert envelope_json(saved) == envelope_json(plain)  # 후보를 뺀 결과 = 후보 없는 대조군과 같은 바이트
    assert s._window_unreported is False  # 시험 계산은 보고를 소비하지 않고, 실제 부착이 1회 소비


@pytest.mark.asyncio
async def test_r2_control_without_candidates_bytes_unchanged():
    """후보 없는 대조군: 상한 안이면 창 보고만 붙고 바이트가 기존과 같다(자르기·변형 없음)."""
    from interface.server_envelope import envelope_json

    _s, _o, res = await _route("", False)
    for title in ("", "가" * 500, _title_for(res, LIMIT, None)):
        _s, out, _ = await _route(title, False)
        assert envelope_json(out) == envelope_json(_expected(res, title))
        assert len(envelope_json(out)) <= LIMIT


@pytest.mark.asyncio
async def test_r2_candidates_with_window_boundary():
    """후보+창 보고 경계: 최종이 정확히 상한이면 후보 유지, 1자 넘으면 후보만 빠지고 요소는 그대로."""
    from interface.server_envelope import envelope_json

    block = await _candidate_block()
    _s, _o, res = await _route("", False)
    title = _title_for(res, LIMIT, block)
    _s, at, _ = await _route(title, True)
    assert "saved_pages" in at.data and "window" in at.data
    assert len(envelope_json(at)) == LIMIT
    assert envelope_json(at) == envelope_json(_expected(res, title, at.data["saved_pages"]))  # id 는 무작위·같은 길이
    _s, over, _ = await _route(title + "가", True)
    assert "saved_pages" not in over.data and "truncated" not in over.data
    assert len(over.data["observation"]["elements"]) == 65
    assert len(envelope_json(over)) <= LIMIT


@pytest.mark.asyncio
@pytest.mark.parametrize("recovered", [False, True, "browser_recovered"])
async def test_r2_final_cap_includes_window_report(recovered):
    """후보가 없어도 창/복구 보고 때문에 상한을 넘으면 최종 cap 이 그 보고까지 넣어 자른다."""
    from interface.server_envelope import envelope_json

    _s, _o, res = await _route("", False)
    title = _title_for(res, LIMIT + 5, None)
    s, out, _ = await _route(title, False, recovered=recovered)
    assert "window" in out.data
    assert len(envelope_json(out)) <= LIMIT
    if recovered == "browser_recovered":
        assert out.data["window"]["notice"]["reason"] == "browser_recovered" and out.reobserve_required
    elif recovered:
        assert out.data["window"]["recovered"] is True
    assert s._recovered_notice is False and s._window_unreported is False
