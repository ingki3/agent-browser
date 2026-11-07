"""WS-85 R2 (재검증 BLOCKING): manual-sites 목록이 있어도(현재 페이지는 목록 밖) R1 의 게이트 묶음이 같다.

목록이 비어 있지 않으면 요소 입력은 _bound_input(WS-64 R4)으로, 좌표 클릭은 단계별 재판정으로 갔고 R1 의 묶음(문서 식별 대조 +
판정 노드 결속 + 판정 노드가 아니면 새 대상 재판정)을 건너뛰었다 — 같은 B3 sweep 에서 제출 49/150. 여기서는 R1 시험
(test_ws85_r1_bindings)의 B2·B3·과잉 0 시나리오를 그대로, 서버만 무관한 합성 목록 1항목(unrelated.invalid)으로 돌린다.
목록 사이트 안 입력(manual 보호)은 test_ws64* 가 그대로 맡는다.
"""

from __future__ import annotations

from typing import Any

import pytest

import test_ws85_r1_bindings as r1
from interface.mcp_server import BrowserMCPServer
from security.manual_sites import ManualSites

from test_run_cli import requires_chromium  # noqa: F401
from test_ws85_r1_bindings import fx  # noqa: F401 — 로컬 HTTP 픽스처


class _ManualServer(BrowserMCPServer):
    """현재 페이지(127.0.0.1)와 무관한 합성 목록 1항목 — 접속하지 않는다."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("manual_sites", ManualSites.of(["unrelated.invalid"]))
        super().__init__(*args, **kwargs)


@pytest.fixture
def manual(monkeypatch):
    monkeypatch.setattr(r1, "BrowserMCPServer", _ManualServer)
    yield


def _manual_on(server: BrowserMCPServer) -> None:
    assert server._dispatcher._manual_active()  # noqa: SLF001 — 전제: 목록 경로(_bound_input)로 간다


@requires_chromium
async def test_r2_manual_missing_at_gate_rejudges_real_node(fx, manual, monkeypatch):  # noqa: F811
    _check_manual(monkeypatch)
    await r1.test_b3_missing_at_gate_rejudges_real_node(fx)


@requires_chromium
@pytest.mark.parametrize("route", ["element_id", "selector"])
async def test_r2_manual_same_css_swap_after_gate_is_rejudged(fx, manual, monkeypatch, route):  # noqa: F811
    _check_manual(monkeypatch)
    await r1.test_b3_same_css_swap_after_gate_is_rejudged(fx, route)


@requires_chromium
async def test_r2_manual_benign_rerender_after_gate_still_clicks(fx, manual, monkeypatch):  # noqa: F811
    _check_manual(monkeypatch)
    await r1.test_b3_benign_rerender_after_gate_still_clicks(fx)


@requires_chromium
@pytest.mark.parametrize("route", ["element_id", "selector", "point", "newline", "enter"])
async def test_r2_manual_document_swap_after_gate_sends_nothing(fx, manual, monkeypatch, route):  # noqa: F811
    _check_manual(monkeypatch)
    await r1.test_b2_document_swap_after_gate_sends_nothing(fx, route)


@requires_chromium
@pytest.mark.parametrize("route", ["element_id", "selector", "newline"])
async def test_r2_manual_document_swap_to_benign_target_still_refused(fx, manual, monkeypatch, route):  # noqa: F811
    _check_manual(monkeypatch)
    await r1.test_b2_document_swap_to_benign_target_still_refused(fx, route)


@requires_chromium
@pytest.mark.parametrize("route", ["element_id", "selector", "point", "newline", "enter", "plain"])
async def test_r2_manual_same_document_pushstate_is_not_refused(fx, manual, monkeypatch, route):  # noqa: F811
    _check_manual(monkeypatch)
    await r1.test_b2_same_document_pushstate_is_not_refused(fx, route)


def _check_manual(monkeypatch) -> None:
    """R1 시나리오의 _call 이 목록 경로에서 도는지(첫 호출 때 한 번) 확인한다 — 목록이 꺼져 있으면 이 시험은 무의미."""
    real = r1._call
    seen = {"checked": False}

    async def call(server: BrowserMCPServer, *args: Any) -> Any:
        if not seen["checked"]:
            _manual_on(server)
            seen["checked"] = True
        return await real(server, *args)

    monkeypatch.setattr(r1, "_call", call)
