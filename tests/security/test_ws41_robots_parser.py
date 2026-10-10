"""WS-41 synthetic REP fixtures; no public-site content or requests."""

import pytest

from security.robots_signal import AI_USER_AGENTS, MAX_ROBOTS_BYTES, RobotsRules


def signal(body, path="/", agent="*"):
    return RobotsRules.parse(body.encode()).match(path, agent)


def test_group_members_and_repeated_groups_merge():
    body = "User-agent: GPTBot\nUser-agent: ClaudeBot\nDisallow: /a\n\nUser-agent: GPTBot\nDisallow: /b"
    for agent in ("GPTBot", "ClaudeBot"):
        assert signal(body, "/a", agent).disallowed
    assert signal(body, "/b", "GPTBot").disallowed
    assert not signal(body, "/b", "ClaudeBot").disallowed
    assert not signal(body, "/a").disallowed


def test_longest_match_and_allow_tie():
    body = "User-agent: *\nDisallow: /\nAllow: /public\nDisallow: /public/private\nAllow: /public/private"
    assert not signal(body, "/public/a").disallowed
    assert not signal(body, "/public/private").disallowed
    assert signal(body, "/other").rule == "Disallow: /"


def test_specificity_counts_pattern_octets_including_anchor_and_wildcard():
    assert signal("User-agent: *\nAllow: /a\nDisallow: /a$", "/a").disallowed
    assert signal("User-agent: *\nAllow: /ac\nDisallow: /a*b", "/acb").disallowed


@pytest.mark.parametrize("path,blocked", [("/docs/a.pdf", True), ("/docs/a.pdf?x=1", False),
                                         ("/docs/a.txt", False), ("/docs/x/y.pdf", True)])
def test_wildcard_and_end_anchor(path, blocked):
    assert signal("User-agent: *\nDisallow: /docs/*.pdf$", path).disallowed is blocked


def test_bom_comments_case_empty_and_malformed_lines():
    body = "\ufeffuSeR-aGeNt: * # all\nDISALLOW: /Case # comment\nDisallow:\nAllow: relative\nbroken\nDisallow /bad\nDisallow: /\x00bad"
    assert signal(body, "/Case").rule == "Disallow: /Case"
    assert not signal(body, "/case").disallowed
    assert not signal(body, "/bad").disallowed
    assert not signal("Disallow: /\nUser-agent: *\nDisallow:").disallowed


def test_specific_agent_overrides_wildcard_and_agent_case_is_insensitive():
    body = "User-agent: *\nDisallow: /\nUser-agent: gptbot\nAllow: /"
    assert signal(body).disallowed
    assert not signal(body, agent="GPTBot").disallowed


def test_unsupported_record_does_not_split_group():
    body = "User-agent: GPTBot\nSitemap: /map\nUser-agent: ClaudeBot\nDisallow: /"
    assert signal(body, agent="GPTBot").disallowed
    assert signal(body, agent="ClaudeBot").disallowed


def test_malformed_rule_does_not_split_consecutive_agent_group():
    body = "User-agent: GPTBot\nDisallow: relative\nUser-agent: ClaudeBot\nDisallow: /"
    assert signal(body, agent="GPTBot").disallowed
    assert signal(body, agent="ClaudeBot").disallowed


def test_utf8_percent_encoding_reserved_and_query():
    body = "User-agent: *\nDisallow: /café\nDisallow: /a%62\nDisallow: /x%2Fy\nDisallow: /find?q=private$"
    for path in ("/caf%C3%A9", "/café", "/ab", "/x%2fy", "/find?q=private"):
        assert signal(body, path).disallowed
    for path in ("/x/y", "/find?q=public"):
        assert not signal(body, path).disallowed


def test_byte_limit_ignores_tail_and_incomplete_line():
    prefix = b"User-agent: *\n"
    body = prefix + b"#" + b"x" * (MAX_ROBOTS_BYTES - len(prefix) - 1) + b"\nDisallow: /"
    assert not RobotsRules.parse(body).match("/", "*").disallowed
    prefix = b"User-agent: *\nDisallow: /"
    body = prefix + b"a" * (MAX_ROBOTS_BYTES - len(prefix)) + b"b\n"
    assert not RobotsRules.parse(body).match("/", "*").disallowed


def test_ai_specific_ban_and_bounded_signal():
    body = "User-agent: *\nAllow: /\nUser-agent: GPTBot\nDisallow: /private"
    parsed = RobotsRules.parse(body.encode())
    assert parsed.signal("/private", "http://local/robots.txt") == {
        "disallowed": False, "ai_agents_disallowed": ["GPTBot"],
        "rule": "Disallow: /private", "robots_url": "http://local/robots.txt",
    }
    assert parsed.signal("/public", "http://local/robots.txt") is None
    all_banned = RobotsRules.parse(b"User-agent: *\nDisallow: /").signal("/", "http://local/robots.txt")
    assert all_banned["ai_agents_disallowed"] == list(AI_USER_AGENTS[:10])


@pytest.mark.parametrize("agent", ["GPTBot", "ChatGPT-User", "OAI-SearchBot", "ClaudeBot", "Claude-User",
                                   "anthropic-ai", "PerplexityBot", "Google-Extended", "CCBot",
                                   "Bytespider", "Applebot-Extended", "meta-externalagent"])
def test_known_ai_agent_list(agent):
    assert agent in AI_USER_AGENTS
    parsed = RobotsRules.parse(f"User-agent: {agent}\nDisallow: /".encode())
    assert parsed.signal("/", "http://local/robots.txt")["ai_agents_disallowed"] == [agent]


def test_rule_summary_strips_controls_and_truncates():
    parsed = RobotsRules.parse(("User-agent: *\nDisallow: /" + "x" * 100).encode())
    out = parsed.signal("/" + "x" * 100, "http://local/robots.txt")
    assert len(out["rule"]) == 80


@pytest.mark.parametrize("ending", [b"\n", b"\r", b"\r\n"])
def test_line_endings_and_invalid_utf8(ending):
    body = ending.join([b"User-agent: *", b"Disallow: /ok", b"Disallow: /broken\xff"])
    parsed = RobotsRules.parse(body)
    assert parsed.match("/ok", "*").disallowed
    assert not parsed.match("/broken", "*").disallowed
