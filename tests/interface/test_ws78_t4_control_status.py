"""WS-78 T4(#215) — 사람용 상태 파일(control.json·ack)·CLI 표시·--json 의 사유는 가린 투영본, 끝난 인계 응답 필드 일관.

합성 값만. 리뷰 .hermes/plans/ws78-evidence/review.md B7.
"""

from __future__ import annotations

import contextlib
import io
import json

import pytest

from interface import handoff
from interface.handoff import STATE_ROOT_ENV, HandoffHub, cli_control
from interface.server_control import TELL_ENDED
from security import secret_registry

SECRET = "SYNTHctlSecret7788"
TOKEN = "SYNTHtok123456789"
REASON = f"login needs pw {SECRET} at https://x.test/cb?token={TOKEN}"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(STATE_ROOT_ENV, str(tmp_path / "servers"))
    secret_registry.clear()
    yield
    secret_registry.clear()


def _hub(tmp_path):
    secret_registry.register_secret_values([SECRET])
    hub = HandoffHub(tmp_path / "servers", browser_mode="human").open()
    hub.request(REASON)
    return hub


def _raw_free(text: str) -> None:
    assert SECRET not in text, text
    assert TOKEN not in text, text


def test_control_json_and_ack_hold_projection(tmp_path):
    hub = _hub(tmp_path)
    assert hub.reason == REASON  # 메모리는 원문(창·띠는 자기 가림)
    raw = (hub.dir / "control.json").read_text()
    _raw_free(raw)
    assert "login needs pw" in raw  # 사유 자체는 남는다(가린 모양)
    hub._ack("n1", True, "ok")
    _raw_free((hub.dir / "ack-n1.json").read_text())


def test_late_registered_value_reprojected_on_next_write(tmp_path):
    secret_registry.clear()
    hub = HandoffHub(tmp_path / "servers", browser_mode="human").open()
    late = "SYNTHlateSecret9911"
    hub.request(f"pw {late}")
    assert late in (hub.dir / "control.json").read_text()  # 측정 조건: 그때는 모르는 값
    secret_registry.register_secret_values([late])
    hub.set_window({"state": "headed"})  # 다음 쓰기
    assert late not in (hub.dir / "control.json").read_text()


def test_cli_status_text_and_json(tmp_path):
    hub = _hub(tmp_path)
    for as_json in (False, True):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert cli_control("status", hub.server_id, as_json=as_json) == 0
        _raw_free(buf.getvalue())


def test_cli_reprojects_old_raw_file(tmp_path, monkeypatch):
    """심층 방어 — 원문이 든 옛 파일도 CLI 가 public_text 를 한 번 더(레지스트리 없는 프로세스: 토큰 주소 층)."""
    hub = _hub(tmp_path)
    st = hub.status()
    monkeypatch.setattr(handoff, "read_control", lambda root, sid: dict(st))  # 원문 사유
    secret_registry.clear()  # CLI 프로세스 — 레지스트리 없음
    for as_json in (False, True):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert cli_control("status", hub.server_id, as_json=as_json) == 0
        assert TOKEN not in buf.getvalue(), buf.getvalue()


@pytest.mark.parametrize("event", ["released", "request_expired"])
def test_ended_handoff_view_fields(tmp_path, event):
    from interface.mcp_server import BrowserMCPServer

    s = BrowserMCPServer(handoff_root=tmp_path / "servers", browser_mode="human", nav_settle=False, recipes=False)
    s.hub.request("확인")
    s.hub.requested = False  # 요청이 끝났다(반납·만료 모두)
    s._note_handoff_end(event)
    view = s._handoff_view()
    assert view["state"] == event
    assert view["expires_at"] is None
    assert view["control_window"] == "none"
    assert view["tell_user"] == TELL_ENDED[event]
    from interface.server_control import TELL_CLI, TELL_HEADLESS, TELL_WINDOW, TELL_WINDOW_USER_CHROME

    assert view["tell_user"] not in (TELL_CLI, TELL_HEADLESS, TELL_WINDOW, TELL_WINDOW_USER_CHROME)
    assert json.dumps(view, ensure_ascii=False)
