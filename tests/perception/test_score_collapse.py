"""WS-13 점수 밀집 해소 테스트.

문제:
실제 페이지에서 role/viewport가 대다수 요소에 동일해 점수가 뭉쳤다.
Top-40의 reasons 조합이 위키백과 2종, 해커뉴스 **1종**이었고,
해커뉴스는 40개가 전부 7.15 동점이었다. 순위가 사실상 DOM 순서로
결정되어 네비게이션이 기사 링크에 밀렸다(past 50위, ask 54위).

해결:
수직 위치(상단 근접)와 라벨 길이를 변별 축으로 추가했다.
실측 — 네비 top=12px/1단어 vs 기사 top=44~1056px/8단어.
"""

from __future__ import annotations

import pytest

from perception.scorer import (
    SHORT_LABEL_MAX_CHARS,
    TOP_PROXIMITY_LIMIT_PX,
    _short_label_score,
    _top_proximity_score,
    prune,
)
from perception.sanitizer import RawElement


def make_element(
    name: str = "버튼",
    role: str = "link",
    y: int = 0,
    seq: int = 0,
) -> RawElement:
    return RawElement(
        seq=seq,
        role=role,
        name=name,
        tag="a" if role == "link" else "button",
        css_path=f"a:nth-child({seq + 1})",
        bbox={"x": 0, "y": y, "width": 80, "height": 24},
        testid=None,
        href="/page",
        disabled=False,
        in_viewport=True,
        is_shadow=False,
    )


# ---------------------------------------------------------------------------
# 1. 상단 근접 신호
# ---------------------------------------------------------------------------


def test_top_of_page_scores_highest():
    assert _top_proximity_score({"y": 0}) == 1.0


def test_score_decreases_with_depth():
    near = _top_proximity_score({"y": 20})
    far = _top_proximity_score({"y": 150})
    assert near > far > 0


def test_below_limit_gets_no_bonus():
    assert _top_proximity_score({"y": int(TOP_PROXIMITY_LIMIT_PX)}) == 0.0
    assert _top_proximity_score({"y": 5000}) == 0.0


def test_negative_y_gets_no_bonus():
    """스크롤로 위로 밀려난 요소는 '상단'이 아니다."""
    assert _top_proximity_score({"y": -300}) == 0.0


# ---------------------------------------------------------------------------
# 2. 짧은 라벨 신호
# ---------------------------------------------------------------------------


def test_short_label_scores_higher_than_long():
    assert _short_label_score("new") > _short_label_score("Sort branches by date")


def test_long_label_gets_no_bonus():
    long_text = "x" * SHORT_LABEL_MAX_CHARS
    assert _short_label_score(long_text) == 0.0


def test_empty_name_gets_no_bonus():
    """이름 없음은 _name_quality가 이미 감점한다. 중복 보상하지 않는다."""
    assert _short_label_score("") == 0.0
    assert _short_label_score("   ") == 0.0


# ---------------------------------------------------------------------------
# 3. 밀집 해소 — 통합
# ---------------------------------------------------------------------------


def test_navigation_outranks_content_links():
    """상단 네비 vs 아래쪽 콘텐츠 링크 — 이름 길이가 비슷한 조건.

    이름 길이가 크게 다르면 `_name_quality`만으로도 갈린다. 실제 밀집은
    길이가 비슷해 다른 신호가 모두 같을 때 발생한다. 이 경우 수직 위치가
    유일한 변별 축이다.
    """
    elements = []
    # 콘텐츠 링크 30개 — 아래쪽. 이름 길이는 네비와 동급(8~10자).
    for i in range(30):
        elements.append(
            make_element(name=f"항목 {i:02d} 링크", y=300 + i * 20, seq=i)
        )
    # 네비게이션 5개 — 최상단, 같은 길이대 (DOM 순서상 나중)
    for j, label in enumerate(["새 소식", "지난 글", "질문 글", "공유 글", "제출하기"]):
        elements.append(make_element(name=label, y=12, seq=30 + j))

    ranked = prune(elements, top_n=10)
    top10 = {s.element.name for s in ranked}
    nav = {"새 소식", "지난 글", "질문 글", "공유 글", "제출하기"}
    found = len(nav & top10)
    assert found >= 4, (
        f"상단 네비가 Top-10에 {found}개뿐 — 수직 위치 신호가 동작하지 않음"
    )


def test_long_content_link_loses_to_short_nav_at_same_position():
    """같은 위치라면 짧은 라벨이 우선한다 (액션 대상 우선).

    단, 2자 이하는 `_name_quality`가 식별력 부족으로 감점하므로
    현실적인 네비 라벨 길이(3~8자)로 비교한다.
    """
    nav = make_element(name="지난 글", y=100, seq=1)
    article = make_element(
        name="이것은 아주 긴 기사 제목이며 콘텐츠 링크입니다", y=100, seq=0
    )
    ranked = prune([article, nav], top_n=2)
    assert ranked[0].element.name == "지난 글", (
        f"짧은 라벨이 밀림: {[(round(s.score, 2), s.element.name) for s in ranked]}"
    )


def test_scores_are_not_degenerate():
    """이름 길이가 같아도 위치가 다르면 점수가 갈려야 한다.

    실측(해커뉴스) — Top-40이 전부 7.15 동점이었다. 동점이면 순위가
    DOM 순서로 결정되어 목표 요소가 임의로 밀린다.
    """
    elements = [
        make_element(name="메뉴 항목", y=10, seq=0),
        make_element(name="본문 링크", y=250, seq=1),
        make_element(name="하단 링크", y=650, seq=2),
        make_element(name="중간 링크", y=120, seq=3),
    ]
    scores = [round(s.score, 2) for s in prune(elements, top_n=4)]
    assert len(set(scores)) >= 3, (
        f"이름 길이가 같은데 점수가 뭉침: {scores} — 위치 신호 부재"
    )


def test_ordering_is_deterministic():
    """같은 입력은 항상 같은 순위여야 한다 (플레이키율 KPI)."""
    elements = [
        make_element(name=f"항목 {i}", y=i * 30, seq=i) for i in range(10)
    ]
    first = [s.element.name for s in prune(elements, top_n=10)]
    second = [s.element.name for s in prune(elements, top_n=10)]
    assert first == second


def test_content_links_still_reachable():
    """네비를 올리느라 콘텐츠 링크가 사라지면 안 된다."""
    elements = [make_element(name="상단 메뉴", y=5, seq=0)]
    elements += [
        make_element(name=f"Article headline {i}", y=200 + i * 20, seq=i + 1)
        for i in range(5)
    ]
    names = {s.element.name for s in prune(elements, top_n=6)}
    assert "Article headline 0" in names
    assert len(names) == 6
