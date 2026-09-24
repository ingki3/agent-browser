"""사이트별 브라우저 프로필 + 사람 먼저 로그인 (Playwright MCP·Browserbase·ChatGPT 방식).

    open_site_context()  — 사이트마다 영속 프로필 폴더(권한 700)로 브라우저를 연다
    ensure_logged_in()   — 보호 페이지를 열어 보고 로그인돼 있으면 끝.
                           아니면 칸을 채워 두고(제출 안 함) 사람에게 넘긴다.
                           완료 판정은 '화면이 안정된 뒤 새 탭으로 보호 페이지 확인'.
    install_navigation_guard() — 에이전트가 그 사이트 밖으로 이동하지 못하게 한다

실측 교정(2026-09-24 네이버):
  - 쿠키만 옮겨 담은 세션은 다음 실행에서 거부됐다 → 프로필 폴더를 통째로 유지
  - 자동 제출은 매번 캡차 → 기본은 제출하지 않고 사람에게
  - 로그인 직후 곧바로 이동해 확인하다 '실패'로 끝냈다 → 안정 대기 + 새 탭 확인,
    확인이 실패해도 끝내지 않고 계속 기다린다
  - 캡차 화면에서 칸이 다시 생기면 비어 있었다 → 빈 칸은 다시 채운다
"""

from __future__ import annotations

import asyncio
import stat

import pytest

from browser.login_flow import LoginState
from browser.site_session import (
    ensure_logged_in,
    install_navigation_guard,
    open_site_context,
    profile_dir_for,
)
from security.credentials import Credential

CRED = Credential(domain="login.test", username="user_1", password="Pw!-login-9")
LOGIN_URL = "https://nid.login.test/nidlogin.login"
CHECK_URL = "https://mail.login.test/inbox"

LOGIN = """<!doctype html><meta charset=utf-8><form action="/submit" method="post">
<input name=id placeholder=아이디><input name=pw type=password placeholder=비밀번호>
<button>로그인</button></form>"""
HOME = "<!doctype html><meta charset=utf-8><h1>받은메일함</h1>"
# 제출 뒤 잠깐 머무는 중간 화면 — 로그인 화면도 아니고 아직 쿠키도 없다.
BRIDGE = """<!doctype html><meta charset=utf-8><p>이동 중…</p>
<script>setTimeout(()=>location.href='/finish', 1200)</script>"""


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
    """영속 쿠키(Max-Age) 사이트. 제출 → 중간 화면 → /finish에서 쿠키 발급."""

    def __init__(self) -> None:
        self.submits = 0
        self.captcha_next = False

    async def handle(self, route):
        req = route.request
        url = req.url
        cookie = (await req.all_headers()).get("cookie", "")
        if url.endswith("/submit"):
            self.submits += 1
            if self.captcha_next:
                self.captcha_next = False
                # 칸이 비어 있는 새 폼 + 보이는 캡차 입력
                body = LOGIN.replace("<button", "<input id=captcha name=captcha><button")
                await route.fulfill(body=body, content_type="text/html")
                return
            await route.fulfill(body=BRIDGE, content_type="text/html")
        elif url.endswith("/finish"):
            await route.fulfill(status=200, body=HOME, content_type="text/html", headers={
                "set-cookie": "sid=ok; Domain=login.test; Path=/; Max-Age=86400"})
        elif "/inbox" in url and "sid=ok" in cookie:
            await route.fulfill(body=HOME, content_type="text/html")
        else:
            await route.fulfill(body=LOGIN, content_type="text/html")


async def _ctx(pw, tmp_path, site):
    ctx = await open_site_context(pw, "login.test", root=tmp_path, headless=True)
    await ctx.route("**/*", site.handle)
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    return ctx, page


def _human_submits(page, seen, *, delay=0.3):
    """사람 흉내 — 넘겨받았을 때 칸 값을 기록하고 제출한다."""

    def on_handoff(info):
        async def act():
            await asyncio.sleep(delay)
            seen.append({"id": await page.input_value("input[name=id]"),
                         "pw": await page.input_value("input[name=pw]"), "info": info})
            await page.click("button")
        asyncio.get_running_loop().create_task(act())
    return on_handoff


# --- 프로필 폴더 -------------------------------------------------------------


def test_profile_dir_is_per_domain_and_validated(tmp_path):
    assert profile_dir_for("naver.com", root=tmp_path) == tmp_path / "naver.com"
    for bad in ("../etc", "naver.com/../x", "", "https://naver.com"):
        with pytest.raises(ValueError):
            profile_dir_for(bad, root=tmp_path)


async def test_profile_dir_is_private(tmp_path):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        ctx, _ = await _ctx(pw, tmp_path, Site())
        await ctx.close()
    mode = stat.S_IMODE((tmp_path / "login.test").stat().st_mode)
    assert mode == 0o700


# --- 로그인 흐름 -------------------------------------------------------------


async def test_first_run_prefills_without_submitting_then_human_logs_in(tmp_path):
    from playwright.async_api import async_playwright

    seen: list = []
    async with async_playwright() as pw:
        site = Site()
        ctx, page = await _ctx(pw, tmp_path, site)
        res = await ensure_logged_in(
            ctx, page, CRED, login_url=LOGIN_URL, check_url=CHECK_URL,
            on_handoff=_human_submits(page, seen), human_timeout_s=15,
            poll_ms=100, stable_ms=500)
        url = page.url
        await ctx.close()
    assert res.source == "human" and res.state is LoginState.LOGGED_IN
    assert seen[0]["id"] == "user_1" and seen[0]["pw"] == "Pw!-login-9"
    assert site.submits == 1, "코드가 제출하면 안 됩니다(사람만 제출)"
    assert "/inbox" in url, "로그인 뒤 페이지는 확인 주소에 있어야 합니다"


async def test_second_run_uses_profile_without_handoff(tmp_path):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        site = Site()
        ctx, page = await _ctx(pw, tmp_path, site)
        await ensure_logged_in(ctx, page, CRED, login_url=LOGIN_URL, check_url=CHECK_URL,
                               on_handoff=_human_submits(page, []), human_timeout_s=15,
                               poll_ms=100, stable_ms=500)
        await ctx.close()

        handed = []
        ctx2, page2 = await _ctx(pw, tmp_path, site)  # 새 브라우저, 같은 프로필 폴더
        res = await ensure_logged_in(ctx2, page2, CRED, login_url=LOGIN_URL,
                                     check_url=CHECK_URL, on_handoff=handed.append,
                                     human_timeout_s=2, poll_ms=100, stable_ms=300)
        await ctx2.close()
    assert res.source == "profile"
    assert handed == [] and site.submits == 1


async def test_bridge_page_is_not_mistaken_for_failure(tmp_path):
    """제출 직후 중간 화면(로그인 화면 아님, 쿠키 아직 없음)에서 성급히 확인해
    실패로 끝내던 결함 — 확인이 실패해도 끝내지 않고 계속 기다려야 한다."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        site = Site()
        ctx, page = await _ctx(pw, tmp_path, site)
        res = await ensure_logged_in(
            ctx, page, CRED, login_url=LOGIN_URL, check_url=CHECK_URL,
            on_handoff=_human_submits(page, []), human_timeout_s=15,
            poll_ms=100, stable_ms=200)  # 짧은 안정 대기 → 중간 화면에서 한 번 확인하게 됨
        await ctx.close()
    assert res.source == "human", res.note


async def test_fields_refilled_when_captcha_page_replaces_form(tmp_path):
    from playwright.async_api import async_playwright

    seen: list = []
    async with async_playwright() as pw:
        site = Site()
        site.captcha_next = True
        ctx, page = await _ctx(pw, tmp_path, site)

        def on_handoff(info):
            async def act():
                await asyncio.sleep(0.3)
                await page.click("button")            # 1차 제출 → 빈 칸 + 캡차 화면
                await asyncio.sleep(1.0)              # 다시 채워질 시간
                seen.append({"id": await page.input_value("input[name=id]"),
                             "pw": await page.input_value("input[name=pw]")})
                await page.fill("#captcha", "abcd")
                await page.click("button")            # 2차 제출 → 성공
            asyncio.get_running_loop().create_task(act())

        res = await ensure_logged_in(ctx, page, CRED, login_url=LOGIN_URL, check_url=CHECK_URL,
                                     on_handoff=on_handoff, human_timeout_s=15,
                                     poll_ms=100, stable_ms=500)
        await ctx.close()
    assert seen and seen[0] == {"id": "user_1", "pw": "Pw!-login-9"}
    assert res.source == "human"


async def test_done_event_forces_check(tmp_path):
    """사람이 '완료'를 알리면(이벤트) 곧바로 확인한다 — 자동 감지보다 우선."""
    from playwright.async_api import async_playwright

    done = asyncio.Event()
    async with async_playwright() as pw:
        site = Site()
        ctx, page = await _ctx(pw, tmp_path, site)

        def on_handoff(info):
            async def act():
                await page.click("button")
                await asyncio.sleep(2.0)
                done.set()
            asyncio.get_running_loop().create_task(act())

        res = await ensure_logged_in(ctx, page, CRED, login_url=LOGIN_URL, check_url=CHECK_URL,
                                     on_handoff=on_handoff, done_event=done,
                                     human_timeout_s=15, poll_ms=100,
                                     stable_ms=60_000)  # 자동 감지로는 안 끝나게
        await ctx.close()
    assert res.source == "human"
    assert res.elapsed_s < 10, f"완료 신호 뒤 곧바로 확인해야 합니다({res.elapsed_s:.1f}s)"


async def test_timeout_fails_and_hides_secrets(tmp_path):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        ctx, page = await _ctx(pw, tmp_path, Site())
        res = await ensure_logged_in(ctx, page, CRED, login_url=LOGIN_URL, check_url=CHECK_URL,
                                     on_handoff=lambda i: None, human_timeout_s=1,
                                     poll_ms=100, stable_ms=300)
        await ctx.close()
    assert res.source == "failed"
    assert "Pw!-login-9" not in repr(res) and "user_1" not in repr(res)


# --- 이동 제한 ---------------------------------------------------------------


async def test_navigation_guard_blocks_other_sites_but_not_subresources(tmp_path):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        ctx = await open_site_context(pw, "login.test", root=tmp_path, headless=True)

        async def serve(route):
            u = route.request.url
            if "cdn.other.test" in u:
                await route.fulfill(body="window.cdnLoaded = true;",
                                    content_type="application/javascript")
            else:
                await route.fulfill(content_type="text/html", body=(
                    "<script src='https://cdn.other.test/x.js'></script><p>ok</p>"))
        await ctx.route("**/*", serve)
        blocked = await install_navigation_guard(ctx, ["login.test"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        await page.goto("https://mail.login.test/inbox")
        cdn = await page.evaluate("window.cdnLoaded === true")

        async def back_home():
            for _ in range(50):
                await asyncio.sleep(0.1)
                if "mail.login.test" in page.url:
                    return True
            return False

        with pytest.raises(Exception):
            await page.goto("https://evil.test/steal")
        assert await back_home(), "막힌 뒤 원래 주소로 돌아와야 합니다"
        with pytest.raises(Exception):
            await page.goto("https://login.test.evil.io/")
        assert await back_home()
        url = page.url
        await ctx.close()
    assert cdn, "다른 도메인의 하위 리소스(CDN)는 막으면 안 됩니다"
    assert "mail.login.test" in url
    assert [b.split("/")[2] for b in blocked] == ["evil.test", "login.test.evil.io"]
