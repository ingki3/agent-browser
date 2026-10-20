"""WS-54: data.robots.user_request — 사용자가 직접 요청한 한 페이지 읽기 안내(오프라인 표).

신호 필드 표(허용 / 미확인 / named_ai / all_bots / all_bots_and_named_ai / content-only)와 MCP instructions 문구를
고정한다. 헤드리스 재시도 동작 표는 tests/interface/test_ws54_robots_retry.py.
"""

from __future__ import annotations

import pytest

from security.robots_signal import ROBOTS_INSTRUCTIONS, RobotsRules

NAMED_AI = ("사용자가 이 주소를 직접 주거나 이 페이지를 콕 집어 요청한 한 페이지 읽기는 사람이 브라우저로 여는 것과 "
            "같은 성격이라 진행해도 됩니다 — 결과에 robots 신호(note)를 한 줄로 알리세요. "
            "같은 사이트의 여러 페이지를 스스로 돌며 모으는 수집·반복 크롤은 하지 마세요.")
ALL_BOTS = ("이 경로는 robots.txt 가 `User-agent: *` 로 모든 봇을 막았습니다 — 사용자가 이 주소를 직접 줬더라도 "
            "robots 신호(note)를 알리고 사용자 확인을 받기 전에는 진행하지 마세요. "
            "같은 사이트의 여러 페이지를 스스로 돌며 모으는 수집·반복 크롤은 하지 마세요.")

URL = "http://local/robots.txt"


@pytest.mark.parametrize("body,path,scope,expected", [
    pytest.param("User-agent: *\nAllow: /\n", "/read", None, None, id="allowed-no-signal"),
    pytest.param("", "/read", None, None, id="empty-robots-no-signal"),
    pytest.param("Content-Signal: ai-train=no\nUser-agent: *\nAllow: /\n", "/read", "none", None,
                 id="content-only-no-field"),
    pytest.param("User-agent: Claude-User\nAllow: /\n", "/read", "none", None, id="user-fetch-allow-no-field"),
    pytest.param("User-agent: *\nAllow: /\nUser-agent: GPTBot\nUser-agent: ClaudeBot\nDisallow: /\n", "/read",
                 "named_ai", NAMED_AI, id="named_ai"),
    pytest.param("User-agent: *\nDisallow: /read\n", "/read", "all_bots", ALL_BOTS, id="all_bots"),
    pytest.param("User-agent: *\nDisallow: /\nUser-agent: GPTBot\nDisallow: /\n", "/read",
                 "all_bots_and_named_ai", ALL_BOTS, id="all_bots_and_named_ai"),
])
def test_user_request_field_table(body, path, scope, expected):
    out = RobotsRules.parse(body.encode()).signal(path, URL)
    if scope is None:
        assert out is None
        return
    assert out["scope"] == scope
    if expected is None:
        assert "user_request" not in out
    else:
        assert out["user_request"] == expected
        # 기존 필드는 그대로(의도된 추가만).
        assert set(out) == {"disallowed", "ai_agents_disallowed", "rule", "robots_url",
                            "user_fetch_agents_allowed", "content_signal", "scope", "note", "user_request"}


def test_scope_branches_differ_in_meaning():
    assert "진행해도 됩니다" in NAMED_AI and "사용자 확인" not in NAMED_AI
    assert "User-agent: *" in ALL_BOTS and "사용자 확인" in ALL_BOTS and "진행해도 됩니다" not in ALL_BOTS
    for text in (NAMED_AI, ALL_BOTS):
        assert "수집·반복 크롤은 하지 마세요" in text


def test_instructions_describe_user_request_without_contradiction():
    text = ROBOTS_INSTRUCTIONS
    # 기존 원칙 유지.
    assert "계속할지는 사용자 뜻에 따라 판단하세요." in text
    assert "사용자에게 robots 를 설명할 때 note 를 그대로 쓰세요." in text
    assert "ai_agents_disallowed 는 이름으로 지정된 것만입니다." in text
    # WS-54 안내.
    assert "data.robots.user_request" in text
    assert "한 페이지 읽기" in text and "named_ai" in text
    assert "수집·반복 크롤" in text
    assert "all_bots" in text and "사용자 확인" in text
    assert len(text) < 700  # 짧게
