"""로그인 흐름: 코드로 1회 자동 로그인 → 실패하면 칸을 채워 둔 채 사람에게 넘긴다.

가짜 도메인(login.test)을 page.route로 흉내 낸다. 폼 제출 결과는 테스트가
정한다(성공 페이지 / 캡차 페이지). '사람'은 on_handoff 콜백이 흉내 낸다.
"""

from __future__ import annotations

import asyncio

import pytest

from browser.login_flow import (
    LoginState,
    fill_login_fields,
    looks_like_login_url,
    login_with_handoff,
    read_login_state,
)
from security.credentials import Credential

CRED = Credential(domain="login.test", username="user_1", password="Pw!-login-9")

LOGIN = """<!doctype html><meta charset=utf-8><form action="/submit" method="post">
<input type=text name=q placeholder=검색>
<input id=uid name=id placeholder=아이디>
<input id=upw name=pw type=password placeholder=비밀번호>
<button type=submit>로그인</button></form>"""
HOME = "<!doctype html><meta charset=utf-8><h1>받은메일함</h1><a href=/logout>로그아웃</a>"
CAPTCHA = """<!doctype html><meta charset=utf-8><form action="/submit" method="post">
<input id=uid name=id><input id=upw name=pw type=password>
<img src=/cap.png><input id=captcha name=captcha placeholder='자동입력 방지문자'>
<button type=submit>로그인</button></form>"""


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
    """submit 결과를 바꿀 수 있는 가짜 사이트."""

    def __init__(self, on_submit: str) -> None:
        self.on_submit = on_submit  # "home" | "captcha"
        self.submitted: list[str] = []

    async def handle(self, route):
        url = route.request.url
        if url.endswith("/submit"):
            self.submitted.append(route.request.post_data or "")
            body = HOME if self.on_submit == "home" else CAPTCHA
            await route.fulfill(body=body, content_type="text/html")
        elif url.endswith("/home"):
            await route.fulfill(body=HOME, content_type="text/html")
        else:
            await route.fulfill(body=LOGIN, content_type="text/html")


async def _page(pw, site: Site, url="https://nid.login.test/nidlogin.login"):
    browser = await pw.chromium.launch(headless=True)
    page = await browser.new_page()
    await page.route("**/*", site.handle)
    await page.goto(url)
    return browser, page


# --- 순수 함수 ------------------------------------------------------------


@pytest.mark.parametrize("url, expected", [
    ("https://nid.naver.com/nidlogin.login", True),
    ("https://accounts.google.com/signin/v2", True),
    ("https://example.com/auth/sign-in", True),
    ("https://mail.naver.com/v2/folders/0", False),
    ("https://blog.example.com/loginsight-review", True),  # 보수적으로 로그인 쪽
])
def test_looks_like_login_url(url, expected):
    assert looks_like_login_url(url) is expected


# --- 상태 판정 ------------------------------------------------------------


async def test_state_login_form_then_captcha_then_logged_in():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page = await _page(pw, Site("captcha"))
        assert await read_login_state(page) is LoginState.LOGIN_FORM
        await page.set_content(CAPTCHA)
        assert await read_login_state(page) is LoginState.CAPTCHA
        await page.goto("https://mail.login.test/home")
        assert await read_login_state(page) is LoginState.LOGGED_IN
        await browser.close()


async def test_hidden_captcha_field_on_plain_login_page_is_not_captcha():
    """실측(2026-09-24 네이버): 평범한 로그인 화면에도 숨은 `ncaptchaSplit` 입력이
    있다. 이걸 캡차로 보면 자동 로그인을 한 번도 시도하지 않고 바로 사람에게 넘긴다."""
    from playwright.async_api import async_playwright

    html = LOGIN.replace(
        "<button", "<input type=hidden id=ncaptchaSplit name=ncaptchaSplit value=none><button"
    )
    async with async_playwright() as pw:
        browser, page = await _page(pw, Site("home"))
        await page.set_content(html)
        state = await read_login_state(page)
        await browser.close()
    assert state is LoginState.LOGIN_FORM


# --- 칸 채우기 ------------------------------------------------------------


async def test_fill_picks_field_before_password_not_search_box():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page = await _page(pw, Site("home"))
        report = await fill_login_fields(page, CRED)
        vals = {k: await page.input_value(f"input[name={k}]") for k in ("q", "id", "pw")}
        await browser.close()
    assert report.username and report.password
    assert vals == {"q": "", "id": "user_1", "pw": "Pw!-login-9"}


async def test_fill_refuses_other_domain():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page = await _page(pw, Site("home"), url="https://login.test.evil.io/login")
        with pytest.raises(PermissionError):
            await fill_login_fields(page, CRED)
        assert await page.input_value("input[name=pw]") == ""
        await browser.close()


# --- 전체 흐름 ------------------------------------------------------------


async def test_auto_login_succeeds_without_human():
    from playwright.async_api import async_playwright

    handed = []
    async with async_playwright() as pw:
        site = Site("home")
        browser, page = await _page(pw, site)
        out = await login_with_handoff(page, CRED, settle_ms=300,
                                       human_timeout_s=5, on_handoff=handed.append)
        await browser.close()
    assert out.method == "auto" and out.state is LoginState.LOGGED_IN
    assert handed == []
    assert len(site.submitted) == 1


async def test_captcha_hands_off_with_fields_filled_and_waits_for_human():
    from playwright.async_api import async_playwright

    seen: dict = {}
    async with async_playwright() as pw:
        site = Site("captcha")
        browser, page = await _page(pw, site)

        def on_handoff(info):
            seen["info"] = info
            seen["submits_at_handoff"] = len(site.submitted)

            async def human():
                # 사람이 보게 될 화면: 아이디·비밀번호가 채워져 있어야 한다
                seen["id"] = await page.input_value("input[name=id]")
                seen["pw"] = await page.input_value("input[name=pw]")
                await asyncio.sleep(0.5)
                await page.goto("https://mail.login.test/home")  # 사람이 로그인 완료
            asyncio.get_running_loop().create_task(human())

        out = await login_with_handoff(page, CRED, settle_ms=300,
                                       human_timeout_s=10, poll_ms=100,
                                       on_handoff=on_handoff)
        await browser.close()
    assert out.method == "human" and out.state is LoginState.LOGGED_IN
    assert seen["submits_at_handoff"] == 1, "자동 제출은 1회뿐이어야 합니다(인계 전 재제출 금지)"
    assert seen["info"].state is LoginState.CAPTCHA
    assert seen["id"] == "user_1" and seen["pw"] == "Pw!-login-9"


async def test_human_timeout_reports_failure_without_secret():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, page = await _page(pw, Site("captcha"))
        out = await login_with_handoff(page, CRED, settle_ms=300,
                                       human_timeout_s=1, poll_ms=100,
                                       on_handoff=lambda info: None)
        await browser.close()
    assert out.method == "failed" and out.state is LoginState.CAPTCHA
    assert "Pw!-login-9" not in repr(out) and "user_1" not in repr(out)
