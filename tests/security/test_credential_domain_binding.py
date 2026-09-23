"""자격증명은 발급된 도메인 페이지에서만 입력된다 (디스패처 경로).

에이전트가 다른 사이트(피싱 페이지, 광고 랜딩, 리다이렉트)로 넘어간 뒤
LOGIN_PASSWORD를 입력하려 하면 **값도, 키 이름도 입력하지 않고** 실패한다.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from security.credentials import CredentialStore

PW = "Cr3d!Only-Here"


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _chromium_available(), reason="Chromium 없음")


@pytest.fixture()
def bound():
    fd, path = tempfile.mkstemp(suffix=".yaml")
    os.write(fd, f"credentials:\n  - domain: login.test\n    username: u1\n    password: '{PW}'\n".encode())
    os.close(fd)
    os.chmod(path, 0o600)
    yield CredentialStore.from_file(path).secrets_for("https://login.test/")
    os.unlink(path)


async def _type_password(page_url: str, secrets) -> tuple:
    from playwright.async_api import async_playwright

    from actions import ActionDispatcher, DispatchContext
    from contracts import ActionType
    from perception import PerceptionEngine

    html = "<form><input id='p' type='password' placeholder='비밀번호'></form>"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        # 실제 도메인 없이 URL만 흉내 낸다 — 요청을 가로채 같은 폼을 돌려준다.
        await page.route("**/*", lambda r: r.fulfill(body=html, content_type="text/html"))
        await page.goto(page_url)
        engine = PerceptionEngine()
        cdp = await context.new_cdp_session(page)
        disp = ActionDispatcher(DispatchContext(page=page, engine=engine, cdp=cdp, secrets=secrets))
        obs = await engine.observe_page(page=page, prune_top_n=20)
        result = await disp.dispatch(ActionType.TYPE_TEXT, {
            "element_id": obs.elements[0].element_id,
            "epoch": obs.snapshot_epoch,
            "text": "LOGIN_PASSWORD",
        })
        typed = await page.input_value("input#p")
        await browser.close()
    return result, typed


async def test_types_on_own_domain(bound):
    result, typed = await _type_password("https://accounts.login.test/signin", bound)
    assert result.success and result.data.get("secret_resolved") is True
    assert typed == PW


@pytest.mark.parametrize("url", ["https://login.test.evil.io/", "https://evil-login.test.io/"])
async def test_refuses_other_domain_and_types_nothing(bound, url):
    result, typed = await _type_password(url, bound)
    assert result.success is False
    assert typed == "", "다른 도메인에 무언가 입력됐습니다"
    assert result.data.get("secret_resolved") is False
    assert PW not in (result.error_message or "")
    assert "도메인" in (result.error_message or "")
