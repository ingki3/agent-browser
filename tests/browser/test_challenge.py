"""차단·캡차 화면 감지 (WS-22) — 로컬 고정 페이지로만 시험한다(외부 요청 없음)."""

from __future__ import annotations

import pytest

from browser.challenge import ChallengeKind, detect_challenge


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        return True
    except Exception:  # noqa: BLE001
        return False


requires_chromium = pytest.mark.skipif(not _chromium_available(), reason="Chromium 없음")

HEAD = "<!doctype html><meta charset=utf-8>"

NAVER_LIMIT = HEAD + """<title>네이버쇼핑</title><div><h2>쇼핑 서비스 접속이 일시적으로 제한되었습니다.</h2>
<p>네이버는 안정적인 쇼핑 서비스 제공을 위하여 비정상적인 접근이 감지될 경우 접속을 제한하고 있습니다.</p></div>"""

NAVER_SECURITY = HEAD + """<title>보안 확인</title><div><h2>보안 확인을 완료해 주세요</h2>
<p>영수증에 표시된 가게 전화번호의 뒤 4자리를 입력하세요.</p><input id=answer></div>"""

AKAMAI = HEAD + """<title>Access Denied</title><h1>Access Denied</h1>
You don't have permission to access "http://www.coupang.com/" on this server.<p>
Reference #18.4f2c1302.1727243125.1a2b3c4d<p>https://errors.edgesuite.net/18.4f2c1302.1727243125.1a2b3c4d</p>"""

CLOUDFLARE = HEAD + """<title>Just a moment...</title><h1>www.example.test</h1>
<p>Verifying you are human. This may take a few seconds.</p>"""

VISIBLE_RECAPTCHA = HEAD + """<title>가입</title><p>가입하기</p>
<iframe title="reCAPTCHA" src="about:blank#https://www.google.com/recaptcha/api2/anchor?k=x"
 class="g-recaptcha" width=304 height=78></iframe>"""

HIDDEN_CAPTCHA = HEAD + """<title>로그인</title><form><input id=id placeholder=아이디>
<input type=password><input type=hidden id=ncaptchaSplit name=captcha value=x>
<input id=captcha_box style="display:none"><button>로그인</button></form>"""

LONG_ARTICLE = HEAD + "<title>HTTP 오류 설명</title><article>" + (
    "<p>웹 서버 오류 코드를 정리한 문서입니다. 403 Access Denied 는 권한 부족을 뜻하며 "
    "Akamai 는 Reference # 번호를 함께 보여 줍니다. 이 문단은 긴 정상 페이지의 본문입니다.</p>" * 40
) + "</article>"

PLAIN = HEAD + "<title>홈</title><h1>환영합니다</h1><a href='#a'>상품 보기</a>"

SHORT_ERROR = HEAD + "<title>오류</title><p>요청을 처리할 수 없습니다.</p>"


async def _detect(html: str, **kw):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        page = await b.new_page()
        await page.set_content(html)
        got = await detect_challenge(page, **kw)
        await b.close()
    return got


@requires_chromium
@pytest.mark.parametrize(
    "html,kind,vendor",
    [
        (NAVER_LIMIT, ChallengeKind.BLOCKED, "naver"),
        (NAVER_SECURITY, ChallengeKind.CAPTCHA, "naver"),
        (AKAMAI, ChallengeKind.BLOCKED, "akamai"),
        (CLOUDFLARE, ChallengeKind.CAPTCHA, "cloudflare"),
        (VISIBLE_RECAPTCHA, ChallengeKind.CAPTCHA, "recaptcha"),
    ],
    ids=["naver-limit", "naver-security", "akamai", "cloudflare", "visible-recaptcha"],
)
async def test_detects_challenge_pages(html, kind, vendor):
    got = await _detect(html)
    assert got.kind is kind, got
    assert got.vendor == vendor
    assert got.reason


@requires_chromium
@pytest.mark.parametrize(
    "html",
    [HIDDEN_CAPTCHA, LONG_ARTICLE, PLAIN],
    ids=["hidden-captcha-input", "long-page-with-words", "plain"],
)
async def test_normal_pages_are_none(html):
    got = await _detect(html)
    assert got.kind is ChallengeKind.NONE, got


@requires_chromium
async def test_block_status_with_short_body_is_blocked():
    got = await _detect(SHORT_ERROR, last_status=418)
    assert got.kind is ChallengeKind.BLOCKED and "418" in got.reason


@requires_chromium
async def test_block_status_with_long_body_is_none():
    got = await _detect(LONG_ARTICLE, last_status=403)
    assert got.kind is ChallengeKind.NONE


@requires_chromium
async def test_ok_status_short_body_is_none():
    got = await _detect(SHORT_ERROR, last_status=200)
    assert got.kind is ChallengeKind.NONE


async def test_unreadable_page_is_none():
    class Broken:
        async def evaluate(self, js):
            raise RuntimeError("Execution context was destroyed")

    assert (await detect_challenge(Broken())).kind is ChallengeKind.NONE
