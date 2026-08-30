"""목표 키워드 추출 (WS-8).

실환경 검증에서 확인된 사실:
같은 페이지·같은 모델이라도 관찰에 목표 키워드가 반영되지 않으면
정답 요소가 Top-20 밖으로 밀려 LLM이 `null`을 반환한다.

    키워드 없음: 'Log in' 41위 -> Top-20 미포함 -> LLM 판단 null
    키워드 있음: 'Log in'  1위 -> Top-20 포함   -> LLM 판단 @e2

따라서 루프는 매 스텝 관찰 전에 목표에서 키워드를 뽑아 주입한다.
LLM을 쓰지 않고 결정론적으로 처리한다 — 스텝마다 추가 호출을 하면
비용이 2배가 되고, 이 정도 추출에는 규칙이면 충분하다.
"""

from __future__ import annotations

import re
from typing import List, Sequence, Set, Tuple

#: 목표 문장에서 제거할 한국어 조사·어미. 길이 내림차순으로 적용해
#: '으로'가 '로'보다 먼저 매칭되게 한다.
_KOREAN_SUFFIXES: Tuple[str, ...] = (
    "에서를", "에게서", "으로써", "으로서", "이라고", "라고",
    "에서", "에게", "으로", "까지", "부터", "처럼", "보다",
    "한테", "하고", "이나", "거나",
    "을", "를", "이", "가", "은", "는", "에", "의", "와", "과", "도", "로", "만",
)

#: 목표를 서술하는 기능어. 요소 이름과 매칭될 가능성이 낮아 제외한다.
_STOPWORDS: Set[str] = {
    # 한국어
    "해줘", "해주세요", "하기", "하세요", "해라", "한다", "합니다",
    "그리고", "다음", "이번", "해당", "위해", "통해", "관련",
    "사이트", "페이지", "화면", "여기", "거기",
    # 영어
    "the", "a", "an", "and", "or", "to", "for", "of", "in", "on", "at",
    "this", "that", "with", "from", "please", "then", "into",
    "page", "site", "website", "screen",
}

#: 액션 동사 -> 요소에서 흔히 쓰이는 표현.
#: '로그인해줘'라는 목표에 대해 버튼 라벨은 'Log in' / '로그인'일 수 있다.
_ACTION_SYNONYMS = {
    "로그인": ("로그인", "log in", "login", "sign in", "signin"),
    "가입": ("가입", "회원가입", "sign up", "signup", "create account", "register"),
    "검색": ("검색", "search", "find"),
    "구매": ("구매", "buy", "purchase", "order"),
    "결제": ("결제", "checkout", "pay", "payment"),
    "장바구니": ("장바구니", "cart", "basket", "add to cart"),
    "제출": ("제출", "submit", "확인", "저장", "save"),
    "다운로드": ("다운로드", "download", "내려받기"),
    "업로드": ("업로드", "upload", "첨부"),
    "삭제": ("삭제", "delete", "remove"),
    "설정": ("설정", "settings", "preferences", "환경설정"),
    "댓글": ("댓글", "comment", "reply"),
    "다음": ("다음", "next", "continue", "계속"),
    "이전": ("이전", "back", "previous"),
}

_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")


def _strip_korean_suffix(token: str) -> str:
    """한국어 조사를 제거한다. 짧은 토큰은 건드리지 않는다."""
    if len(token) <= 2:
        return token
    for suffix in _KOREAN_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 2:
            return token[: -len(suffix)]
    return token


def extract_keywords(goal: str, *, limit: int = 12) -> List[str]:
    """목표 문장에서 관찰 스코어링에 쓸 키워드를 추출한다.

    반환 순서는 결정론적이다(등장 순서 유지). 같은 목표에 대해 매번
    같은 관찰이 나와야 플레이키율 KPI를 만족한다.
    """
    if not goal:
        return []

    raw = [t.lower() for t in _TOKEN.findall(goal)]
    keywords: List[str] = []
    seen: Set[str] = set()

    def add(word: str) -> None:
        w = word.strip().lower()
        if w and w not in seen and len(w) >= 2:
            seen.add(w)
            keywords.append(w)

    for token in raw:
        stem = _strip_korean_suffix(token)
        if stem in _STOPWORDS or token in _STOPWORDS:
            continue
        add(stem)

        # 동의어 확장: 목표의 '로그인'이 페이지에서는 'Log in'일 수 있다.
        #
        # 주의: 부분 일치를 양방향으로 허용하면 오확장이 발생한다.
        # 실측 사례 — '이 사이트에'의 '이'가 '이전'(back/previous)으로
        # 확장돼 무관한 키워드가 관찰 스코어를 오염시켰다.
        # 트리거를 포함하는 경우만 인정하고, 짧은 토큰은 완전 일치를 요구한다.
        for trigger, synonyms in _ACTION_SYNONYMS.items():
            matched = stem == trigger or (len(stem) >= 3 and trigger in stem)
            if matched:
                for syn in synonyms:
                    add(syn)

    # 영문 다단어 표현도 붙여서 넣는다 ('create account' 같은 라벨 대응)
    lowered = goal.lower()
    for synonyms in _ACTION_SYNONYMS.values():
        for syn in synonyms:
            if " " in syn and syn in lowered:
                add(syn)

    return keywords[:limit]


def keywords_for_step(goal: str, recent_failures: Sequence[str] = ()) -> List[str]:
    """스텝별 키워드. 직전 실패 정보를 반영한다.

    치유 실패나 요소 미발견이 반복되면 목표 키워드만으로는 부족하다는
    신호이므로, 실패 메시지에 등장한 요소 이름을 키워드에 더한다.
    """
    keywords = extract_keywords(goal)
    for failure in recent_failures[-2:]:
        for token in _TOKEN.findall(failure.lower())[:3]:
            if len(token) >= 3 and token not in keywords:
                keywords.append(token)
    return keywords[:16]
