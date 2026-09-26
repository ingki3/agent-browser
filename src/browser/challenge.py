"""차단·캡차 화면 감지 (WS-22).

실측(2026-09-25):
* 네이버 쇼핑 헤드리스 → HTTP 418 + "쇼핑 서비스 접속이 일시적으로 제한되었습니다"
* 네이버 쇼핑 창 보임 → "보안 확인을 완료해 주세요"(영수증 캡차, HTTP 405 본문)
* 쿠팡 → Akamai "Access Denied … errors.edgesuite.net Reference #…"(403 또는 200 본문)

이런 화면에서 에이전트는 scroll/read_text를 7~9번 헛돌다 포기했다. 업계
표준대로 **멈추고 사람에게 넘긴다.** 이 모듈은 판정만 한다 — 캡차를 풀거나
우회하는 코드는 두지 않는다(네이버 약관도 외부 솔루션 캡차 우회를 금지).

판정 원칙(login_flow 의 캡차 판정과 같다):
* 보이는 텍스트·제목·**보이는** 알려진 요소만 본다. 입력값·쿠키는 읽지 않는다.
* 숨은 캡차 요소는 캡차가 아니다(네이버 로그인 화면의 숨은 input#ncaptchaSplit).
* 긴 정상 페이지(본문 > LONG_PAGE_CHARS)에 단어만 섞인 경우는 NONE — 기사·문서에
  "Access Denied" 같은 말이 나올 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

#: 이보다 긴 본문에서는 문구 일치만으로 차단/캡차로 보지 않는다.
LONG_PAGE_CHARS = 3000
#: 차단 상태코드 + 이보다 짧은 본문이면 차단 화면으로 본다.
SHORT_BODY_CHARS = 800
#: 차단 화면에서 흔한 상태코드(403 거부, 418 네이버, 429 과다 요청).
BLOCK_STATUSES = frozenset({403, 418, 429})


class ChallengeKind(str, Enum):
    NONE = "none"
    #: 사람이 풀 수 있는 보안 확인(캡차·체크박스·영수증 문제 등)
    CAPTCHA = "captcha"
    #: 거부 화면 — 풀 대상이 없다(사람이 창을 바꿔도 대개 소용없다)
    BLOCKED = "blocked"


@dataclass(frozen=True)
class Challenge:
    kind: ChallengeKind = ChallengeKind.NONE
    reason: str = ""
    vendor: str = ""

    @property
    def detected(self) -> bool:
        return self.kind is not ChallengeKind.NONE


NO_CHALLENGE = Challenge()

#: 보이는 요소만 센다. 값·쿠키는 읽지 않는다(텍스트·제목·요소 존재만).
_PROBE_JS = r"""
() => {
  const vis = el => {
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden'
      && cs.display !== 'none' && parseFloat(cs.opacity || '1') > 0;
  };
  // reCAPTCHA v3 배지는 보이지만 풀 대상이 아니다(점수형 — 사람이 할 일 없음).
  const isBadge = el => !!el.closest('.grecaptcha-badge')
    || /[?&]size=invisible/i.test(el.getAttribute('src') || '');
  const sel = "input[id*=captcha i], input[name*=captcha i], " +
    "iframe[src*=captcha i], iframe[src*=recaptcha i], iframe[src*=hcaptcha i], " +
    "iframe[src*='challenges.cloudflare.com'], " +
    "[class*=g-recaptcha], [class*=h-captcha], [class*=cf-turnstile]";
  let widget = '';
  for (const el of document.querySelectorAll(sel)) {
    if ((el.getAttribute('type') || '').toLowerCase() === 'hidden') continue;
    if (isBadge(el) || !vis(el)) continue;
    const s = ((el.getAttribute('src') || '') + ' ' + (el.className || '')).toLowerCase();
    widget = s.includes('hcaptcha') || s.includes('h-captcha') ? 'hcaptcha'
      : (s.includes('cloudflare') || s.includes('turnstile')) ? 'cloudflare'
      : s.includes('recaptcha') ? 'recaptcha' : 'generic';
    break;
  }
  const body = document.body ? (document.body.innerText || '') : '';
  return {title: document.title || '', text: body.slice(0, 4000), len: body.length, widget};
}
"""

# (문구, 벤더, 이유) — 소문자 비교.
_CAPTCHA_PHRASES = (
    ("보안 확인을 완료해 주세요", "naver", "네이버 보안 확인 화면"),
    ("just a moment", "cloudflare", "Cloudflare 브라우저 확인 화면"),
    ("verify you are human", "cloudflare", "Cloudflare 사람 확인 요구"),
    ("checking your browser", "cloudflare", "Cloudflare 브라우저 확인 화면"),
    # G마켓 검색 결과에서 만난 Cloudflare 확인 화면(2026-09-25 실측, HTTP 403). 감지
    # 시점 제목에 "Just a moment" 가 없어 "HTTP 403 + 짧은 본문" 으로만 잡혔다(이유에
    # 가변 길이가 들어가 강제 계속 키가 흔들림). 짧은 단어("봇 확인")는 FAQ·기사에도
    # 나오므로 안내문의 완결된 문장만 쓴다.
    ("간단한 봇 확인 절차가 진행되고 있습니다", "cloudflare", "Cloudflare 봇 확인 화면(한국어 안내)"),
)
_BLOCKED_PHRASES = (
    ("접속이 일시적으로 제한", "naver", "네이버 접속 일시 제한"),
    ("sorry, you have been blocked", "cloudflare", "Cloudflare 차단 화면"),
)


def classify(title: str, text: str, length: int, widget: str,
             last_status: Optional[int] = None) -> Challenge:
    """프로브 결과를 판정한다(순수 함수 — 브라우저 없이 시험 가능)."""
    if widget:
        vendor = "generic" if widget == "generic" else widget
        return Challenge(ChallengeKind.CAPTCHA, f"보이는 캡차 위젯({widget})", vendor)
    if length > LONG_PAGE_CHARS:
        return NO_CHALLENGE
    hay = f"{title}\n{text}".lower()
    for phrase, vendor, reason in _CAPTCHA_PHRASES:
        if phrase in hay:
            return Challenge(ChallengeKind.CAPTCHA, reason, vendor)
    for phrase, vendor, reason in _BLOCKED_PHRASES:
        if phrase in hay:
            return Challenge(ChallengeKind.BLOCKED, reason, vendor)
    if "access denied" in hay and ("edgesuite" in hay or "reference #" in hay):
        return Challenge(ChallengeKind.BLOCKED, "Akamai 접근 거부(Access Denied)", "akamai")
    if last_status in BLOCK_STATUSES and length < SHORT_BODY_CHARS:
        return Challenge(ChallengeKind.BLOCKED,
                         f"HTTP {last_status} + 짧은 본문({length}자)", "generic")
    return NO_CHALLENGE


async def detect_challenge(page: Any, *, last_status: Optional[int] = None) -> Challenge:
    """현재 화면이 차단/캡차인지 본다. 읽기 실패(전환 중 등)는 NONE."""
    try:
        s = await page.evaluate(_PROBE_JS)
    except Exception:  # noqa: BLE001 - 판정 불가면 막지 않는다(다음 스텝에 다시 본다)
        return NO_CHALLENGE
    if not isinstance(s, dict):
        return NO_CHALLENGE
    return classify(
        str(s.get("title") or ""),
        str(s.get("text") or ""),
        int(s.get("len") or 0),
        str(s.get("widget") or ""),
        last_status,
    )
