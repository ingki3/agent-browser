"""WS-46: distinguish wildcard bans from explicitly named AI groups offline."""

import pytest

from security.robots_signal import AI_USER_AGENTS, RobotsRules


NOTES = {
    "all_bots": "이 경로는 robots.txt 가 모든 자동 접근(봇 전체)에 대해 금지합니다 — AI 만 골라 막은 것이 아닙니다.",
    "named_ai": "robots.txt 가 일부 AI 봇 이름(주로 학습·수집용)을 지정해 이 경로를 금지합니다 — 모든 봇 대상 금지는 아닙니다.",
    "all_bots_and_named_ai": "이 경로는 robots.txt 가 모든 봇에 대해 금지하고, AI 봇 일부도 이름으로 따로 금지합니다.",
}


@pytest.mark.parametrize("body,path,scope,blocked,allowed", [
    pytest.param("User-agent: *\nDisallow: /results", "/results?search_query=mock",
                 "all_bots", [], [], id="youtube-style"),
    pytest.param("User-agent: *\nDisallow: /\nUser-agent: GPTBot\nUser-agent: ClaudeBot\nDisallow: /",
                 "/read", "all_bots_and_named_ai", ["GPTBot", "ClaudeBot"], [], id="naver-style"),
    pytest.param("Content-Signal: ai-train=no, search=yes, ai-input=yes\nUser-agent: *\nAllow: /\n"
                 "User-agent: GPTBot\nUser-agent: ClaudeBot\nDisallow: /\nUser-agent: Claude-User\nAllow: /",
                 "/read", "named_ai", ["GPTBot", "ClaudeBot"], ["Claude-User"], id="geeknews-style"),
    pytest.param("uSeR-aGeNt: gPtBoT\nUser-agent: cLaUdEbOt\nDisallow: /read",
                 "/read", "named_ai", ["GPTBot", "ClaudeBot"], [], id="case-and-no-wildcard"),
    pytest.param("User-agent: *\nUser-agent: GPTBot\nDisallow: /read",
                 "/read", "all_bots_and_named_ai", ["GPTBot"], [], id="shared-wildcard-group"),
    pytest.param("User-agent: *\nDisallow: /\nUser-agent: GPTBot\nDisallow: /\nAllow: /read",
                 "/read", "all_bots", [], [], id="named-longer-allow"),
    pytest.param("User-agent: *\nDisallow: /\nUser-agent: GPTBot\nDisallow: /read\nAllow: /read",
                 "/read", "all_bots", [], [], id="named-allow-tie"),
    pytest.param("User-agent: *\nDisallow: /\nUser-agent: GPTBot\nDisallow: /other",
                 "/read", "all_bots", [], [], id="named-unmatched-rule"),
    pytest.param("User-agent: *\nDisallow: /\nUser-agent: GPTBot\nDisallow:",
                 "/read", "all_bots", [], [], id="named-empty-rule"),
    pytest.param("Content-Signal: ai-train=no\nUser-agent: *\nAllow: /",
                 "/read", "none", [], [], id="content-only"),
    pytest.param("User-agent: Claude-User\nAllow: /",
                 "/read", "none", [], ["Claude-User"], id="user-fetch-only"),
])
def test_scope_uses_only_explicit_named_bans(body, path, scope, blocked, allowed):
    rules = RobotsRules.parse(body.encode())
    out = rules.signal(path, "http://local/robots.txt")
    assert out["scope"] == scope
    assert out["disallowed"] is (scope in {"all_bots", "all_bots_and_named_ai"})
    assert out["ai_agents_disallowed"] == blocked
    assert out["user_fetch_agents_allowed"] == allowed
    if scope == "none":
        assert "note" not in out
    else:
        assert out["note"] == NOTES[scope]
    # General matching still inherits '*'; WS-44 allowed()/403 hints use this behavior.
    assert rules.match(path, "UnlistedBot").disallowed is out["disallowed"]
    if "ai-train" in body:
        assert out["content_signal"] == body.splitlines()[0].split(": ", 1)[1]


# Offline excerpts in the shape of the real files (no network in tests).
YOUTUBE_ROBOTS = b"""# robots.txt file for YouTube
User-agent: Mediapartners-Google*
Disallow:

User-agent: *
Disallow: /api/
Disallow: /comment
Disallow: /login
Disallow: /results
Disallow: /signup
Disallow: /youtubei/

Sitemap: https://www.youtube.com/sitemaps/sitemap.xml
"""
NAVER_ROBOTS = b"""User-agent: *
Disallow: /

User-agent: Yeti
Allow: /

User-agent: GPTBot
Disallow: /

User-agent: ClaudeBot
Disallow: /

User-agent: CCBot
Disallow: /
"""


def test_youtube_wildcard_only_is_all_bots_not_ai():
    rules = RobotsRules.parse(YOUTUBE_ROBOTS)
    out = rules.signal("/results?search_query=mock", "https://www.youtube.com/robots.txt")
    assert out["scope"] == "all_bots" and out["disallowed"] is True
    assert out["ai_agents_disallowed"] == []
    assert out["rule"] == "Disallow: /results"
    assert out["note"] == NOTES["all_bots"]
    # The general (WS-44 retry) judgement still inherits '*' for every AI agent.
    assert all(rules.match("/results", agent).disallowed for agent in AI_USER_AGENTS)
    assert rules.signal("/watch?v=x", "https://www.youtube.com/robots.txt") is None


def test_naver_wildcard_and_named_lists_only_named():
    out = RobotsRules.parse(NAVER_ROBOTS).signal("/search.naver?query=x", "https://search.naver.com/robots.txt")
    assert out["scope"] == "all_bots_and_named_ai" and out["disallowed"] is True
    assert out["ai_agents_disallowed"] == ["GPTBot", "ClaudeBot", "CCBot"]
    assert out["note"] == NOTES["all_bots_and_named_ai"]


def test_explicit_ai_list_keeps_ten_agent_limit():
    body = "\n".join(f"User-agent: {agent}\nDisallow: /" for agent in AI_USER_AGENTS)
    out = RobotsRules.parse(body.encode()).signal("/read", "http://local/robots.txt")
    assert out["ai_agents_disallowed"] == list(AI_USER_AGENTS[:10])
    assert out["scope"] == "named_ai"


@pytest.mark.parametrize("scope,wildcard,named", [
    ("all_bots", "Disallow", "Allow"),
    ("named_ai", "Allow", "Disallow"),
    ("all_bots_and_named_ai", "Disallow", "Disallow"),
])
def test_note_never_interpolates_robots_text(scope, wildcard, named):
    marker = "UNTRUSTED_ROBOTS_ignore_previous_instructions"
    body = (f"Content-Signal: {marker}\nUser-agent: *\n{wildcard}: /{marker}\n"
            f"User-agent: GPTBot\n{named}: /{marker}")
    out = RobotsRules.parse(body.encode()).signal(f"/{marker}", f"http://local/{marker}")
    assert out["note"] == NOTES[scope]
    assert marker not in out["note"]
    assert marker in out["rule"] and marker in out["content_signal"]


def test_plain_allow_and_no_rules_remain_silent():
    for body in (b"", b"User-agent: *\nAllow: /", b"User-agent: GPTBot\nAllow: /"):
        assert RobotsRules.parse(body).signal("/read", "http://local/robots.txt") is None
