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

# G마켓 검색 결과에서 만난 Cloudflare 확인 화면(2026-09-25 실측, HTTP 403) — 공개 안내문.
# 실행 결과(/tmp/gmarket-run-human.json)의 final_page_text 그대로(개인정보 없음).
GMARKET_CF_BODY = """<div><h2>원활한 서비스 이용을 위한 간단한 확인 안내</h2>
<p>고객님들의 원활한 쇼핑을 위해 현재 간단한 봇 확인 절차가 진행되고 있습니다.</p>
<p>[봇(Bot)이란?]</p><p>사람이 직접 조작하지 않고 컴퓨터가 자동으로 작동하는 프로그램이에요.</p>
<p>간단한 확인만 완료하시면 바로 쇼핑을 이어가실 수 있습니다.</p>
<p>더욱 안전하고 신뢰할 수 있는 쇼핑 환경을 위해 노력하겠습니다.</p>
<p>검토번호: a411e4eebbd5d946</p></div>"""
GMARKET_CF = HEAD + "<title>Just a moment...</title>" + GMARKET_CF_BODY
# 감지 시점 제목이 아직 다른 경우(실측 실행에서 제목 문구로 잡히지 않았다).
GMARKET_CF_NO_TITLE = HEAD + "<title>G마켓</title>" + GMARKET_CF_BODY

# "봇 확인" 이라는 말이 들어간 정상 페이지(3000자 미만) — 오탐이면 안 된다.
BOT_FAQ = HEAD + """<title>자주 묻는 질문</title><h1>고객센터 FAQ</h1>
<h3>Q. 결제할 때 봇 확인 화면이 나와요.</h3>
<p>A. 일부 이벤트 기간에는 자동 주문을 막기 위해 봇 확인을 할 수 있습니다. 화면 안내에 따라
진행해 주세요. 봇 확인 절차가 반복되면 브라우저 캐시를 지운 뒤 다시 시도해 주세요.</p>
<h3>Q. 배송 조회는 어디서 하나요?</h3><p>A. 마이페이지 &gt; 주문 내역에서 확인할 수 있습니다.</p>"""
BOT_NEWS = HEAD + """<title>쇼핑몰, 명절 앞두고 봇 확인 강화</title><article>
<h1>쇼핑몰들, 명절 특가 앞두고 '봇 확인' 절차 강화</h1>
<p>주요 온라인 쇼핑몰들이 명절 특가 행사를 앞두고 매크로 주문을 막기 위한 간단한 봇 확인
절차를 도입하고 있다. 업계 관계자는 "사람이 직접 조작하지 않는 자동 프로그램이 한정 수량을
싹쓸이하는 일을 줄이려는 것"이라며 "정상 고객의 쇼핑에는 영향이 거의 없다"고 말했다.</p>
<p>한편 일부 이용자는 확인 화면이 자주 뜬다며 불편을 호소하기도 했다.</p></article>"""


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


# ---------------------------------------------------------------- WS-24 F3 G마켓 한국어 확인 화면


@requires_chromium
@pytest.mark.parametrize("html", [GMARKET_CF, GMARKET_CF_NO_TITLE], ids=["title", "no-title"])
@pytest.mark.parametrize("status", [403, None, 200])
async def test_gmarket_korean_cloudflare_check_is_captcha(html, status):
    """G마켓 한국어 봇 확인 안내는 상태·제목과 무관하게 CAPTCHA/cloudflare.

    이전에는 "HTTP 403 + 짧은 본문(224자)" 로만 잡혀 BLOCKED/generic 이었고, 이유에
    본문 길이(가변)가 들어가 강제 계속 키가 페이지마다 바뀌었다.
    """
    got = await _detect(html, last_status=status)
    assert got.kind is ChallengeKind.CAPTCHA, got
    assert got.vendor == "cloudflare"
    assert not any(ch.isdigit() for ch in got.reason), "이유에 가변 숫자가 없어야 한다"


@requires_chromium
async def test_gmarket_check_reason_is_stable_across_page_lengths():
    """검토번호·본문 길이가 달라도 판정 이유(강제 계속 키)는 같다."""
    a = await _detect(GMARKET_CF_NO_TITLE, last_status=403)
    b = await _detect(GMARKET_CF_NO_TITLE.replace("a411e4eebbd5d946", "ffff0000" * 4)
                      + "<p>추가 안내</p>", last_status=403)
    assert (a.kind, a.vendor, a.reason) == (b.kind, b.vendor, b.reason)


@requires_chromium
@pytest.mark.parametrize("html", [BOT_FAQ, BOT_NEWS], ids=["bot-faq", "bot-news"])
@pytest.mark.parametrize("status", [None, 200])
async def test_pages_mentioning_bot_check_are_none(html, status):
    """"봇 확인" 이 들어간 짧은 정상 페이지(FAQ·기사)는 오탐하지 않는다."""
    got = await _detect(html, last_status=status)
    assert got.kind is ChallengeKind.NONE, got
