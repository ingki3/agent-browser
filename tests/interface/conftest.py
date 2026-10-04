"""tests/interface 공용 픽스처 (WS-5 소유).

WS-29: serve·BrowserMCPServer 는 사람 인계 상태 디렉터리(~/.agent-browser/servers)를 만든다.
테스트가 사용자 홈을 건드리지 않게 테스트마다 임시 디렉터리로 돌린다(하위 프로세스 serve 도
os.environ 을 물려받는다).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_handoff_root(tmp_path_factory, monkeypatch):
    from interface.handoff import STATE_ROOT_ENV

    root = tmp_path_factory.mktemp("ab-servers") / "servers"
    monkeypatch.setenv(STATE_ROOT_ENV, str(root))
    yield root
