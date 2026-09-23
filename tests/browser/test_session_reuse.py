"""로그인 세션 재사용 — 저장된 세션이 유효하면 로그인 생략, 아니면 로그인 후 저장.

    open_logged_in(...)
      1) 저장된 세션이 있으면 그것으로 컨텍스트를 열고 check_url로 확인
         LOGGED_IN → 끝 (source="session", 자격증명·사람 불필요)
      2) 없거나 만료 → 새 컨텍스트에서 login_with_handoff
         성공 → storage_state를 암호화 저장 (source="login")
         실패 → 저장하지 않음

패스프레이즈는 호출자가 준다(키체인 등). 세션 파일은 SessionStore 형식 그대로다.
"""

from __future__ import annotations

import pytest

from browser.login_flow import LoginState
from browser.session_reuse import open_logged_in
from browser.session_store import SessionStore
from security.credentials import Credential

CRED = Credential(domain="login.test", username="user_1", password="Pw!-login-9")
PASS = "test-passphrase-123"

LOGIN = """<!doctype html><meta charset=utf-8><form action="/submit" method="post">
<input name=id placeholder=아이디><input name=pw type=password placeholder=비밀번호>
<button>로그인</button></form>"""
HOME = "<!doctype html><meta charset=utf-8><h1>받은메일함</h1>"


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _chromium_available(), reason="Chromium 없음")


class Site:
    """쿠키 sid=ok 가 있으면 메일함, 없으면 로그인으로 보낸다."""

    def __init__(self, valid_sid: str = "ok") -> None:
        self.valid_sid = valid_sid
        self.logins = 0

    async def handle(self, route):
        req = route.request
        cookie = (await req.all_headers()).get("cookie", "")
        authed = f"sid={self.valid_sid}" in cookie
        if req.url.endswith("/submit"):
            self.logins += 1
            await route.fulfill(
                status=302,
                headers={"location": "https://mail.login.test/inbox",
                         "set-cookie": f"sid={self.valid_sid}; Domain=login.test; Path=/"},
            )
        elif "/inbox" in req.url and authed:
            await route.fulfill(body=HOME, content_type="text/html")
        else:
            # 미인증 /inbox도 로그인 화면을 그 자리에서 보여 준다. (route로 가로챈
            # 응답의 302는 다른 호스트로 따라갈 때 다시 가로채지지 않아 DNS 오류가 난다.)
            await route.fulfill(body=LOGIN, content_type="text/html")


async def _open(pw, tmp_path, site: Site, **kw):
    browser = await pw.chromium.launch(headless=True)
    store = SessionStore(auth_dir=tmp_path)
    result = await open_logged_in(
        browser, profile="mail", cred=CRED, store=store, passphrase=PASS,
        login_url="https://nid.login.test/nidlogin.login",
        check_url="https://mail.login.test/inbox",
        context_setup=lambda ctx: ctx.route("**/*", site.handle),
        settle_ms=300, human_timeout_s=2, poll_ms=100, **kw,
    )
    return browser, store, result


async def test_first_run_logs_in_and_saves(tmp_path):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        site = Site()
        browser, store, res = await _open(pw, tmp_path, site)
        await browser.close()
    assert res.source == "login" and res.state is LoginState.LOGGED_IN
    assert site.logins == 1
    assert store.exists("mail") and store.verify_permissions("mail")
    raw = store.path_for("mail").read_bytes()
    assert b"sid=ok" not in raw and b"Pw!-login-9" not in raw, "세션 파일은 암호화돼야 합니다"


async def test_second_run_reuses_session_without_login(tmp_path):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        site = Site()
        b1, _, _ = await _open(pw, tmp_path, site)
        await b1.close()
        b2, _, res = await _open(pw, tmp_path, site)
        url = res.page.url
        await b2.close()
    assert res.source == "session"
    assert site.logins == 1, "유효한 세션이 있는데 다시 로그인했습니다"
    assert "/inbox" in url


async def test_expired_session_falls_back_to_login_and_resaves(tmp_path):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b1, _, _ = await _open(pw, tmp_path, Site(valid_sid="ok"))
        await b1.close()
        site2 = Site(valid_sid="rotated")  # 서버가 세션을 만료시킴
        b2, store, res = await _open(pw, tmp_path, site2)
        await b2.close()
        b3, _, res3 = await _open(pw, tmp_path, site2)
        await b3.close()
    assert res.source == "login" and site2.logins == 1
    assert res3.source == "session", "재로그인 후 새 세션을 저장하지 않았습니다"


async def test_failed_login_does_not_save(tmp_path):
    from playwright.async_api import async_playwright

    class Broken(Site):
        async def handle(self, route):
            if route.request.url.endswith("/submit"):
                self.logins += 1
                await route.fulfill(body=LOGIN, content_type="text/html")  # 다시 로그인 화면
            else:
                await super().handle(route)

    async with async_playwright() as pw:
        browser, store, res = await _open(pw, tmp_path, Broken())
        await browser.close()
    assert res.source == "failed" and res.state is not LoginState.LOGGED_IN
    assert not store.exists("mail")


async def test_wrong_passphrase_is_treated_as_no_session(tmp_path):
    """복호화 실패(키체인 교체 등)는 치명적 오류가 아니라 '세션 없음'이다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b1, _, _ = await _open(pw, tmp_path, Site())
        await b1.close()
        browser = await pw.chromium.launch(headless=True)
        site = Site()
        res = await open_logged_in(
            browser, profile="mail", cred=CRED, store=SessionStore(auth_dir=tmp_path),
            passphrase="different-pass", login_url="https://nid.login.test/nidlogin.login",
            check_url="https://mail.login.test/inbox",
            context_setup=lambda ctx: ctx.route("**/*", site.handle),
            settle_ms=300, human_timeout_s=2, poll_ms=100,
        )
        await browser.close()
    assert res.source == "login" and site.logins == 1
    assert "복호화" in res.note
