"""tests/recipes 공용 픽스처 (WS-38).

서버(BrowserMCPServer)를 쓰는 테스트가 사용자 홈(~/.agent-browser)을 건드리지 않게 사람 인계 상태
디렉터리와 serve --profile 루트를 테스트마다 임시 디렉터리로 돌린다(tests/interface 와 같은 방식).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_handoff_root(tmp_path_factory, monkeypatch):
    from interface.handoff import STATE_ROOT_ENV

    root = tmp_path_factory.mktemp("ab-servers") / "servers"
    monkeypatch.setenv(STATE_ROOT_ENV, str(root))
    yield root


@pytest.fixture(autouse=True)
def _isolated_profile_root(tmp_path_factory, monkeypatch):
    from browser.serve_profile import PROFILE_ROOT_ENV

    root = tmp_path_factory.mktemp("ab-profiles") / "profiles"
    monkeypatch.setenv(PROFILE_ROOT_ENV, str(root))
    yield root
