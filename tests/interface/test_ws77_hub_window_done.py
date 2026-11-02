"""WS-77 B4 — 제어 창의 [다 했어요] 는 hub 대기 사건 큐 → poll() 'released'(출처 window_done) 로만 반납된다."""

from __future__ import annotations

from pathlib import Path

import pytest

from interface import handoff
from interface.handoff import HOLDER_AGENT, HOLDER_HUMAN, HandoffHub


@pytest.fixture
def hub(tmp_path: Path):
    h = HandoffHub(root=tmp_path / "servers", browser_mode="human").open()
    yield h
    h.close()


def test_window_done_releases_current_request_through_poll(hub):
    rid = hub.request("로그인", secret_wanted=True)["request_id"]
    hub.queue_window_done(rid)
    # 큐에 넣기만 해서는 상태가 바뀌지 않는다 — poll 이 기존 반납 흐름으로 처리한다.
    assert hub.requested and hub.secret_wanted
    v = hub.version
    assert hub.poll() == ["released"]
    assert hub.holder == HOLDER_AGENT and not hub.requested and not hub.secret_wanted
    assert hub.release_source == "window_done"
    assert hub.version == v + 1 and hub.last_event == "released"
    assert hub.poll() == []  # 한 번만


def test_window_done_while_human_holds_releases(hub):
    rid = hub.request("캡차")["request_id"]
    handoff.write_command(hub.root, hub.server_id, "take")
    assert hub.poll() == ["taken"]
    assert hub.holder == HOLDER_HUMAN
    hub.queue_window_done(rid)
    assert hub.poll() == ["released"]
    assert hub.holder == HOLDER_AGENT and hub.release_source == "window_done"


@pytest.mark.parametrize("which", ["old", "none", "garbage"])
def test_stale_or_foreign_nonce_is_ignored(hub, which):
    old = hub.request("첫 요청")["request_id"]
    hub.queue_window_done(old)
    assert hub.poll() == ["released"]
    new = hub.request("두 번째", secret_wanted=True)["request_id"]
    assert new != old
    hub.queue_window_done({"old": old, "none": None, "garbage": "rq_x" * 40}[which])
    assert hub.poll() == []
    assert hub.requested and hub.secret_wanted and hub.request_id == new


def test_done_without_live_request_does_nothing(hub):
    rid = hub.request("요청")["request_id"]
    assert hub.cancel_request()  # 만료로 거둠
    hub.queue_window_done(rid)
    assert hub.poll() == []
    assert hub.holder == HOLDER_AGENT and hub.last_event == "request_expired"


def test_release_sources_of_other_paths(hub):
    hub.request("a")
    handoff.write_command(hub.root, hub.server_id, "take")
    hub.poll()
    handoff.write_command(hub.root, hub.server_id, "release")
    assert hub.poll() == ["released"]
    assert hub.release_source == "cli"
    hub.request("b")
    hub.release_by_server(release_source="window_closed")
    assert hub.release_source == "window_closed"
    hub.release_by_server()
    assert hub.release_source == "server"


def test_queue_is_bounded(hub):
    rid = hub.request("x")["request_id"]
    for _ in range(1000):
        hub.queue_window_done("rq_forged")
    hub.queue_window_done(rid)
    assert hub.poll() == ["released"]
    assert len(hub._window_done) == 0
