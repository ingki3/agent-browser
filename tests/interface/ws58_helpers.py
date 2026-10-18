"""WS-58 — WS-52 테스트가 지키던 '장부에 합치지 않음' 불변식을 판정과 분리해 검사하는 도구.

WS-58 전에는 '합치지 않은 쿠키 변화'가 곧 '다음 방문 이름 유지'였다. WS-58 뒤에는 비 HttpOnly 변화가 판정을 막지
않으므로, WS-52 테스트는 (1) 장부를 WS-52 의 엄격 대조(정확한 지문, ``js`` 키는 HttpOnly 아닐 때 값 무관)로 보아
그 변화가 합쳐지지 않았음을 확인하고, (2) 다음 방문 결과는 WS-58 규칙(b)으로 기대값을 계산해 확인한다.
"""
from __future__ import annotations

from browser.headless_retry import skip_reason
from security.robots_signal import _origin


def _key(entry):
    return (entry["name"], entry["domain"], entry["path"])


def _strict_ok(ledger, entry, http_only):
    for known in ledger:
        if _key(known) == _key(entry):
            if known.get("js"):
                return not http_only
            return known["value_digest"] == entry["value_digest"]
    return False


async def strict_drift(srv, url):
    """WS-52 엄격 대조로 본 장부 밖 변화: (new_cookie | known_value_changed | None, [(entry, http_only)])."""
    retry = srv._headless_retry
    current = await retry._cookies(srv._page.context, url)
    assert current is not None
    ledger = retry.ledger.get(_origin(url) or "", [])
    changed = [(e, h) for e, h in current if not _strict_ok(ledger, e, h)]
    if not changed:
        return None, []
    keys = {_key(e) for e in ledger}
    return ("new_cookie" if any(_key(e) not in keys for e, _ in changed) else "known_value_changed"), changed


def ws58_blocking(srv, url, changed):
    """WS-58 규칙(b): 장부에 서버 쿠키로 없는 HttpOnly 키(새 키, 또는 js 키가 HttpOnly 로)."""
    ledger = srv._headless_retry.ledger.get(_origin(url) or "", [])
    by_key = {_key(e): e for e in ledger}
    return [e for e, h in changed if h and (_key(e) not in by_key or by_key[_key(e)].get("js"))]


async def assert_unmerged_next(srv, site, change, navigate, path="/next", recorded=False):
    """WS-52 불변식(엄격 대조로 change 가 남아 있음 = 합치지 않음) + WS-58 판정에 맞는 다음 방문.

    recorded: 그 변화가 사람 조작권 구간에 생겨 로그인 기록(human_control)이 된 경우(WS-58 R1 — 조작권 중 쿠키
    변화는 로그인으로 간주) — 다음 방문은 규칙 (a) ``login_recorded``."""
    kind, changed = await strict_drift(srv, site.url)
    assert kind == change, (kind, change)
    blocking = ws58_blocking(srv, site.url, changed)
    keys = {_key(e) for e in srv._headless_retry.ledger.get(site.url, [])}
    assert srv._headless_retry.logins.covers(site.url) is recorded
    result = await navigate(srv, site.url + path)
    if recorded:
        assert result.data["last_http_status"] == 403, result.data
        assert "identity" not in result.data
        assert result.data["retry_skipped"] == skip_reason("login_recorded")
        assert site.headless(path)
    elif blocking:
        detail = "new_cookie" if any(_key(e) not in keys for e in blocking) else "known_value_changed"
        assert result.data["last_http_status"] == 403, result.data
        assert "identity" not in result.data
        assert result.data["retry_skipped"] == skip_reason("new_server_cookie", detail)
        assert site.headless(path)
    else:
        # WS-58 (intended change): a non-HttpOnly change no longer keeps the native name.
        assert result.data["last_http_status"] == 200, result.data.get("retry_skipped")
        assert result.data["identity"]["presented_as"] == "chrome"
        assert not site.headless(path, len(site.received(path)) - 1)
    return result
