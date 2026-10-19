"""WS-54: 헤드리스 재시도 robots 조건 고정(로컬 robots 모의 × 헤드리스 403 서버) + data.robots.user_request 배선.

재시도 조건은 바꾸지 않는다 — `RobotsSignals.allowed()` 는 `*` 규칙만 본다:

| robots 모의              | scope                  | 재시도 | /read 요청 수 | user_request |
| :---                     | :---                   | :---   | :---          | :---         |
| `*` Allow                | (신호 없음)            | 함     | 2             | 없음         |
| AI 이름만 금지           | named_ai               | 함     | 2             | 진행 안내    |
| `*` 금지                 | all_bots               | 안 함  | 1             | 확인 안내    |
| `*` 금지 + AI 이름 금지  | all_bots_and_named_ai  | 안 함  | 1             | 확인 안내    |
| 조회 불가(503)           | (신호 없음)            | 안 함  | 1             | 없음         |
"""
from __future__ import annotations

import pytest

from interface.mcp_server import BrowserMCPServer
from security.robots_signal import RobotsRules
from test_run_cli import requires_chromium
from test_ws44_headless_retry import local_site, navigate

ROWS = {
    "allowed": (b"User-agent: *\nAllow: /\n", 200, None, True),
    "named_ai": (b"User-agent: *\nAllow: /\nUser-agent: GPTBot\nUser-agent: ClaudeBot\nDisallow: /\n", 200,
                 "named_ai", True),
    "all_bots": (b"User-agent: *\nDisallow: /read\n", 200, "all_bots", False),
    "all_bots_and_named_ai": (b"User-agent: *\nDisallow: /\nUser-agent: GPTBot\nDisallow: /\n", 200,
                              "all_bots_and_named_ai", False),
    "unknown": (b"", 503, None, False),
}


@requires_chromium
@pytest.mark.parametrize("row", list(ROWS))
async def test_retry_robots_condition_table(row):
    robots, robots_status, scope, retried = ROWS[row]
    with local_site() as site:
        site.robots, site.robots_status = robots, robots_status
        async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
            result = await navigate(server, site.url + "/read")
        assert len(site.received()) == (2 if retried else 1)
        if retried:
            assert result.success and result.data["last_http_status"] == 200
            assert result.data["identity"]["reason"] == "headless_ua_blocked"
            assert "retry_skipped" not in result.data
        else:
            assert "identity" not in result.data
            assert result.data["last_http_status"] == 403
            assert result.data["retry_skipped"]["reason"] == "robots_not_confirmed"
        if scope is None:
            assert "robots" not in result.data
        else:
            robots_signal = result.data["robots"]
            assert robots_signal["scope"] == scope
            expected = RobotsRules.parse(robots).signal("/read", "http://x/robots.txt")
            assert robots_signal["user_request"] == expected["user_request"]
            if scope == "named_ai":
                assert "진행해도 됩니다" in robots_signal["user_request"]
            else:
                assert "사용자 확인" in robots_signal["user_request"]
