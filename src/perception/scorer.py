"""Prune4Web 스코어러 (PRD §3.1, Gate 2 Recall@20 >= 95%).

수백 개 후보 요소에서 상위 N개만 남겨 관찰 토큰을 p50 2,500 이하로
억제하면서도 정답 요소를 놓치지 않아야 한다.

스코어링 원칙:
* **재현율 우선** — 애매하면 남긴다. Top-20 밖으로 밀려난 정답은 복구
  사다리(§3.2)를 타야 하므로 비용이 크다.
* **결정론적** — 동일 입력에 항상 동일 순위. LLM이나 난수를 쓰지 않는다.
  플레이키율 <= 2% KPI에 직결된다.
* **CPU 오프로딩** — Levenshtein 등 무거운 연산은 `asyncio.to_thread`로
  이벤트 루프에서 분리한다 (PRD §5.2).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from perception.sanitizer import RawElement

# --- 가중치 (합이 1.0이 되도록 정규화하지 않고 가산 점수로 사용) ----------

#: role별 기본 가중치. 클릭 대상이 될 확률이 높은 순.
ROLE_WEIGHTS: Dict[str, float] = {
    "button": 1.00,
    "link": 0.95,
    "textbox": 0.90,
    "searchbox": 0.90,
    "combobox": 0.85,
    "checkbox": 0.80,
    "radio": 0.80,
    "switch": 0.75,
    "tab": 0.70,
    "menuitem": 0.70,
    "option": 0.55,
    "slider": 0.50,
    "spinbutton": 0.50,
    "generic": 0.20,
}

W_ROLE = 3.0
W_NAME = 2.5
W_VIEWPORT = 1.2
W_TESTID = 0.8
W_SIZE = 0.6
W_KEYWORD = 4.0  # 목표 키워드 일치는 가장 강한 신호
W_SHADOW = 0.3  # shadow 내부 요소는 놓치기 쉬우므로 소폭 가산

#: 반복 패턴 감점.
#: 목록형 페이지에서 '관련 상품 0~39' 같은 동형 링크가 수십 개 쌓이면,
#: 각 항목이 개별로는 평범한 점수를 받아도 Top-N을 통째로 점유해
#: 정작 핵심 CTA(결제/제출 버튼)를 밀어낸다. 실측에서 정답 버튼이
#: 69개 중 35위로 밀려 Recall@20이 0.91로 떨어졌다.
#:
#: 이름에서 숫자를 제거한 형태가 동일한 요소를 '반복 패턴'으로 보고,
#: 같은 그룹의 3번째부터 점진 감점한다. 그룹의 대표 몇 개는 남으므로
#: 목록 자체를 못 보는 문제는 생기지 않는다.
W_REPEAT_PENALTY = 1.6
REPEAT_GROUP_FREE = 2  # 그룹당 감점 없이 통과시킬 개수

#: 페이지 내부 앵커('#section') 감점.
#: 실환경 검증에서 Wikipedia 목차 링크 14개가 Top-20을 점유해
#: 'Log in'(51위), 'Create account'(31위) 같은 실제 액션 요소를 밀어냈다.
#: 목차·각주 앵커는 페이지 내 스크롤일 뿐 상태를 바꾸지 않으므로,
#: 같은 조건이면 실제 네비게이션/제출 요소가 우선해야 한다.
#: 완전히 제거하지는 않는다. '목차에서 X 절로 이동' 같은 목표도 있기 때문이다.
W_INPAGE_ANCHOR_PENALTY = 1.2

#: 페이지 상단 근접 가산.
#: 실측 — 해커뉴스 네비게이션(new/past/ask/show/submit/login)은 전부
#: top=12px에 있고, 기사 제목은 top=44~1056px에 분포한다. 그런데
#: 스코어러가 수직 위치를 보지 않아 네비가 기사 40개에 밀려
#: 21~54위로 내려갔다(Top-20 밖). 사이트 전역 네비게이션은 거의 항상
#: 최상단에 있으므로, 이를 변별 축으로 쓴다.
#:
#: 주의: 이 신호만으로 '상단이면 무조건 중요'라고 판단하면 안 된다.
#: 배너·광고도 상단에 있다. 다른 신호와 합산되는 보조 축이다.
W_TOP_PROXIMITY = 1.0

#: 상단 가산이 적용되는 최대 y좌표(px). 이보다 아래는 가산이 0이다.
TOP_PROXIMITY_LIMIT_PX = 200.0

#: 짧은 라벨 가산.
#: 실측 — 네비게이션은 1단어(3~8자), 기사 제목은 8단어(30~79자)다.
#: 액션 대상(버튼/네비 링크)은 짧고, 콘텐츠 링크는 길다.
W_SHORT_LABEL = 0.6

#: 짧은 라벨로 간주하는 최대 글자 수
SHORT_LABEL_MAX_CHARS = 20

#: 비활성 요소 감점 (제거하지 않고 순위만 낮춘다)
P_DISABLED = -1.5

#: 이름이 없는 요소 감점
P_NO_NAME = -1.0


@dataclass
class ScoredElement:
    """스코어링된 요소."""

    element: RawElement
    score: float
    reasons: Tuple[str, ...] = ()

    @property
    def role(self) -> str:
        return self.element.role

    @property
    def name(self) -> str:
        return self.element.name


def _normalize(text: str) -> str:
    return " ".join((text or "").lower().split())


def levenshtein(a: str, b: str) -> int:
    """편집 거리. 자가 치유 사다리 3단계에서도 재사용한다."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def similarity(a: str, b: str) -> float:
    """0.0~1.0 정규화 유사도 (순수 편집 거리 기반).

    스코어링의 키워드 매칭에 사용한다. UI 라벨 비교에는
    `label_similarity`를 사용하십시오.
    """
    a, b = _normalize(a), _normalize(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    longest = max(len(a), len(b))
    return 1.0 - (levenshtein(a, b) / longest)


#: 접두 확장에 적용할 감점 계수. 추가된 길이 비율에 곱해 감점한다.
_PREFIX_EXTENSION_PENALTY = 0.35


def label_similarity(a: str, b: str) -> float:
    """UI 라벨 비교 전용 유사도 (자가 치유 3단계용).

    순수 편집 거리 비율은 짧은 라벨의 **접두 확장**에 지나치게 가혹하다.
    실측값을 보면 문제가 분명하다:

        '로그인'  -> '로그인하기'    0.60
        '저장'    -> '저장하기'      0.50
        'Save'    -> 'Save Changes'  0.33

    셋 다 같은 버튼의 문구 변경(A/B 테스트, i18n, 카피 수정)이며 치유
    대상이 되어야 하지만 임계값 0.75에 한참 못 미친다.

    따라서 **짧은 쪽이 긴 쪽의 접두사인 경우에만** 별도 점수를 계산해
    최댓값을 취한다. 접두사 조건은 의미 보존의 대리 지표다:

    * 접미 확장('삭제' -> '삭제하기')은 활용형이라 의미가 유지된다.
    * 접두 추가('삭제' -> '전체 삭제')는 한정어가 붙어 의미가 바뀐다.
      이 경우는 접두사가 아니므로 보너스를 받지 못하고 기각된다.

    남은 위험: '삭제' -> '삭제 취소'처럼 접두사이면서 의미가 반대인
    경우는 이 함수만으로 구분할 수 없다. `heal()`의 모호성 가드가
    이를 담당한다.
    """
    base = similarity(a, b)
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return base

    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    if shorter != longer and longer.startswith(shorter):
        extra_ratio = (len(longer) - len(shorter)) / len(longer)
        prefix_score = 1.0 - _PREFIX_EXTENSION_PENALTY * extra_ratio
        return max(base, prefix_score)
    return base


def _name_quality(name: str) -> float:
    """이름의 정보량을 0.0~1.0으로 평가한다.

    너무 짧으면 식별력이 낮고, 너무 길면 본문 덩어리일 가능성이 높다.
    """
    text = _normalize(name)
    if not text:
        return 0.0
    length = len(text)
    if length <= 2:
        return 0.3
    if length <= 40:
        return 1.0
    if length <= 100:
        return 0.6
    return 0.25


def _size_score(bbox: Dict[str, int]) -> float:
    """클릭 타깃 크기를 로그 스케일로 평가한다."""
    area = max(0, bbox.get("width", 0)) * max(0, bbox.get("height", 0))
    if area <= 0:
        return 0.0
    # 44x44(권장 최소 터치 타깃) 부근에서 1.0에 근접하도록 정규화
    return min(1.0, math.log10(area + 1) / math.log10(44 * 44 + 1))


def _top_proximity_score(bbox: Dict[str, int]) -> float:
    """페이지 상단 근접도 (0.0~1.0). 뷰포트 상단일수록 1.0에 가깝다.

    사이트 전역 네비게이션은 거의 항상 최상단에 배치된다. 반면 콘텐츠
    링크는 아래로 분포한다. 실측(해커뉴스) — 네비 top=12px, 기사
    top=44~1056px.
    """
    y = bbox.get("y", 0)
    if y < 0:
        # 스크롤로 위로 밀려난 요소는 상단 가산 대상이 아니다.
        return 0.0
    if y >= TOP_PROXIMITY_LIMIT_PX:
        return 0.0
    return 1.0 - (y / TOP_PROXIMITY_LIMIT_PX)


def _short_label_score(name: str) -> float:
    """짧은 라벨일수록 1.0에 가깝다.

    액션 대상(버튼, 네비 링크)은 짧고, 콘텐츠 링크(기사 제목)는 길다.
    이름이 없으면 0.0 — `_name_quality`가 이미 감점하므로 중복 보상하지
    않는다.
    """
    text = (name or "").strip()
    if not text:
        return 0.0
    if len(text) >= SHORT_LABEL_MAX_CHARS:
        return 0.0
    return 1.0 - (len(text) / SHORT_LABEL_MAX_CHARS)


def _is_inpage_anchor(element: RawElement) -> bool:
    """페이지 내부 앵커인가 (목차·각주 링크).

    `href="#section"`처럼 프래그먼트만 있는 링크는 클릭해도 스크롤만
    발생하고 페이지 상태가 바뀌지 않는다. `href="#"`(JS 핸들러용)은
    실제 동작을 가질 수 있으므로 제외한다.
    """
    if element.role != "link":
        return False
    href = (element.href or "").strip()
    return href.startswith("#") and len(href) > 1


def score_element(
    element: RawElement, goal_keywords: Sequence[str] = ()
) -> ScoredElement:
    """단일 요소의 점수를 계산한다."""
    reasons: List[str] = []
    score = 0.0

    role_w = ROLE_WEIGHTS.get(element.role, ROLE_WEIGHTS["generic"])
    score += W_ROLE * role_w
    reasons.append(f"role={element.role}({role_w:.2f})")

    name_q = _name_quality(element.name)
    score += W_NAME * name_q
    if name_q == 0.0:
        score += P_NO_NAME
        reasons.append("no_name")

    if element.in_viewport:
        score += W_VIEWPORT
        reasons.append("in_viewport")

    if element.testid:
        score += W_TESTID
        reasons.append("has_testid")

    size_s = _size_score(element.bbox)
    score += W_SIZE * size_s

    # 상단 근접 — 사이트 전역 네비게이션의 위치 신호
    top_s = _top_proximity_score(element.bbox)
    if top_s > 0:
        score += W_TOP_PROXIMITY * top_s
        reasons.append(f"near_top({top_s:.2f})")

    # 짧은 라벨 — 액션 대상은 짧고 콘텐츠 링크는 길다
    short_s = _short_label_score(element.name)
    if short_s > 0:
        score += W_SHORT_LABEL * short_s
        reasons.append(f"short_label({short_s:.2f})")

    if element.is_shadow:
        score += W_SHADOW
        reasons.append("shadow")

    if element.disabled:
        score += P_DISABLED
        reasons.append("disabled")

    # 페이지 내부 앵커는 상태를 바꾸지 않으므로 실제 액션 요소보다 낮게 둔다.
    if _is_inpage_anchor(element):
        score -= W_INPAGE_ANCHOR_PENALTY
        reasons.append("inpage_anchor")

    # 목표 키워드 일치는 가장 강한 신호
    if goal_keywords:
        best = 0.0
        matched = ""
        haystack = _normalize(f"{element.name} {element.testid or ''} {element.href or ''}")
        for kw in goal_keywords:
            k = _normalize(kw)
            if not k:
                continue
            if k in haystack:
                best = max(best, 1.0)
                matched = kw
            else:
                sim = similarity(k, _normalize(element.name))
                if sim > best:
                    best, matched = sim, kw
        if best >= 0.6:
            score += W_KEYWORD * best
            reasons.append(f"keyword~{matched}({best:.2f})")

    return ScoredElement(element=element, score=round(score, 4), reasons=tuple(reasons))


_DIGITS = re.compile(r"\d+")


def repeat_key(element: RawElement) -> str:
    """반복 패턴 그룹 키. 이름의 숫자를 제거한 형태로 묶는다.

    '관련 상품 12'와 '관련 상품 37'은 같은 그룹이 된다.
    """
    return f"{element.role}|{_DIGITS.sub('#', _normalize(element.name))}"


def prune(
    elements: Iterable[RawElement],
    top_n: int = 20,
    goal_keywords: Sequence[str] = (),
) -> List[ScoredElement]:
    """상위 N개 후보를 결정론적으로 선별한다.

    동점자는 원본 DOM 순서(seq)로 안정 정렬해 실행마다 순위가 흔들리지
    않게 한다 (플레이키율 KPI).

    반복 패턴(동형 목록 항목)은 그룹당 상위 몇 개만 온전한 점수를 받고
    나머지는 감점된다. 목록이 Top-N을 통째로 점유해 핵심 액션 요소를
    밀어내는 것을 막기 위함이다.
    """
    items = list(elements)
    scored = [score_element(e, goal_keywords) for e in items]

    # 반복 그룹별로 DOM 순서상 뒤쪽 항목에 점진 감점을 적용한다.
    seen: Dict[str, int] = {}
    adjusted: List[ScoredElement] = []
    for item in scored:
        key = repeat_key(item.element)
        rank_in_group = seen.get(key, 0)
        seen[key] = rank_in_group + 1

        if rank_in_group < REPEAT_GROUP_FREE:
            adjusted.append(item)
            continue

        # 3번째부터 감점. 그룹이 커질수록 감점 폭이 커진다.
        depth = rank_in_group - REPEAT_GROUP_FREE + 1
        penalty = W_REPEAT_PENALTY * min(1.0, depth / 3.0)
        adjusted.append(
            ScoredElement(
                element=item.element,
                score=round(item.score - penalty, 4),
                reasons=item.reasons + (f"repeat_penalty(-{penalty:.2f})",),
            )
        )

    adjusted.sort(key=lambda s: (-s.score, s.element.seq))
    return adjusted[:top_n]


async def prune_async(
    elements: Sequence[RawElement],
    top_n: int = 20,
    goal_keywords: Sequence[str] = (),
) -> List[ScoredElement]:
    """스코어링을 별도 스레드로 오프로딩한다 (이벤트 루프 블로킹 방지).

    요소가 적을 때는 스레드 전환 비용이 더 크므로 인라인 처리한다.
    """
    import asyncio

    if len(elements) < 200:
        return prune(elements, top_n, goal_keywords)
    return await asyncio.to_thread(prune, elements, top_n, goal_keywords)


def expand_top_n(current: int) -> int:
    """복구 사다리 1단계: N 확장 (PRD §3.2)."""
    return 50 if current < 50 else current * 2


def filter_by_keywords(
    elements: Iterable[RawElement], keywords: Sequence[str], threshold: float = 0.55
) -> List[RawElement]:
    """복구 사다리 2단계: 시맨틱 키워드 정밀 매칭 (PRD §3.2)."""
    if not keywords:
        return list(elements)
    matched: List[RawElement] = []
    for el in elements:
        haystack = _normalize(f"{el.name} {el.testid or ''} {el.href or ''}")
        for kw in keywords:
            k = _normalize(kw)
            if not k:
                continue
            if k in haystack or similarity(k, _normalize(el.name)) >= threshold:
                matched.append(el)
                break
    return matched
