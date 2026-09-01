"""WS-16 CJK 이름 품질 판정 및 commercial 티어 테스트.

배경:
상용 사이트(네이버 뉴스)를 태스크에 넣으면서 발견한 결함이다.
같은 줄에 나란히 배치된 섹션 메뉴인데 순위가 극단적으로 갈렸다.

    '정치'      (2자, 한글)  name_quality 0.30  ->  417위
    '생활/문화'  (5자)        name_quality 1.00  ->   15위

`_name_quality`가 2자 이하를 '식별력 부족'으로 감점하는데, 이 기준은
라틴 문자를 전제한 것이다. 'ok', 'go'는 애매하지만 한글 2자는 완전한
단어다. 한국어·중국어·일본어 사이트에서 주요 네비게이션이 통째로
밀려나는 문제였다.

영문 샌드박스만 돌렸다면 발견할 수 없었다.
"""

from __future__ import annotations

import pytest

from perception import prune
from perception.sanitizer import RawElement
from perception.scorer import _has_cjk, _name_quality, score_element


def make_element(name, seq=0, y=52, width=44, role="link"):
    return RawElement(
        seq=seq,
        role=role,
        name=name,
        tag="a",
        css_path=f"a:nth-child({seq + 1})",
        bbox={"x": 0, "y": y, "width": width, "height": 46},
        value=None,
        testid=None,
        href="/section/100",
        disabled=False,
        in_viewport=True,
        is_shadow=False,
        frame_path="",
    )


# ---------------------------------------------------------------------------
# 1. CJK 탐지
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["정치", "경제", "홈", "검색", "ニュース", "新聞", "生活/文化", "IT/과학"],
)
def test_detects_cjk(text):
    assert _has_cjk(text), f"{text!r}를 CJK로 인식하지 못했습니다"


@pytest.mark.parametrize("text", ["ok", "go", "Login", "Sign up", "123", ""])
def test_does_not_flag_latin_as_cjk(text):
    assert not _has_cjk(text)


# ---------------------------------------------------------------------------
# 2. 이름 품질 — 문자 체계별 기준
# ---------------------------------------------------------------------------


def test_korean_two_char_word_is_informative():
    """한글 2자는 완전한 단어다. 실측에서 '정치'가 417위로 밀렸다."""
    assert _name_quality("정치") == 1.0
    assert _name_quality("경제") == 1.0
    assert _name_quality("사회") == 1.0


def test_latin_two_char_stays_penalized():
    """라틴 2자는 여전히 식별력이 낮다 (기존 동작 유지)."""
    assert _name_quality("ok") == 0.3
    assert _name_quality("go") == 0.3


def test_single_cjk_char_is_still_penalized():
    """1자는 CJK라도 감점한다 (너무 모호하다)."""
    assert _name_quality("홈") == 0.3


def test_long_names_unchanged():
    """길이 상한 규칙은 그대로다."""
    assert _name_quality("가" * 30) == 1.0
    assert _name_quality("가" * 60) == 0.6
    assert _name_quality("가" * 150) == 0.25


def test_empty_name_scores_zero():
    assert _name_quality("") == 0.0


# ---------------------------------------------------------------------------
# 3. 실제 순위 영향 (네이버 뉴스 재현)
# ---------------------------------------------------------------------------


def test_korean_nav_ranks_with_longer_siblings():
    """같은 줄의 섹션 메뉴는 이름 길이와 무관하게 비슷한 순위여야 한다.

    실측 — 수정 전 '정치' 417위 vs '생활/문화' 15위.
    같은 y좌표, 같은 role, 같은 성격인데 이름 길이만으로 갈렸다.
    """
    elements = [
        make_element("정치", seq=0, width=44),
        make_element("경제", seq=1, width=44),
        make_element("생활/문화", seq=2, width=73),
        make_element("IT/과학", seq=3, width=61),
    ]
    ranked = prune(elements, top_n=10)
    names = [r.element.name for r in ranked]

    # 2자 항목이 5자 항목보다 크게 밀리면 안 된다
    assert names.index("정치") < len(elements), "'정치'가 순위에서 밀려났습니다"

    scores = {r.element.name: r.score for r in ranked}
    gap = abs(scores["정치"] - scores["생활/문화"])
    assert gap < 1.0, (
        f"같은 성격의 메뉴인데 점수 차가 {gap:.2f}입니다 "
        f"(정치={scores['정치']:.2f}, 생활/문화={scores['생활/문화']:.2f})"
    )


def test_korean_label_not_penalized_versus_latin_equivalent():
    """같은 의미의 한글/영문 라벨이 비슷한 점수를 받아야 한다."""
    ko = score_element(make_element("검색", seq=0))
    en = score_element(make_element("Search", seq=1))
    assert abs(ko.score - en.score) < 0.5, (
        f"한글 '검색'({ko.score:.2f})과 영문 'Search'({en.score:.2f})의 "
        "점수 차가 큽니다"
    )


# ---------------------------------------------------------------------------
# 4. commercial 티어
# ---------------------------------------------------------------------------


def test_commercial_tier_registered():
    from harness.real_tasks import DIFFICULTY_LEVELS

    assert "commercial" in DIFFICULTY_LEVELS


def test_commercial_tasks_exist():
    from harness.real_tasks import tasks_by_difficulty

    assert tasks_by_difficulty().get("commercial", 0) >= 2


def test_blocked_sites_are_excluded():
    """봇 차단 사이트는 태스크에 넣지 않는다.

    쿠팡(403 Akamai), 구글 검색(CAPTCHA)은 100% 실패하는데 원인이
    우리 런타임이 아니므로 완수율 지표를 오염시킨다.
    """
    from harness.real_tasks import TASKS

    for task in TASKS:
        assert "coupang.com" not in task.url, (
            f"{task.task_id}: 쿠팡은 403으로 차단됩니다"
        )
        assert not (
            "google.com" in task.url and "search" in task.success_expr
        ), f"{task.task_id}: 구글 검색은 CAPTCHA로 막힙니다"


def test_commercial_tasks_are_https_and_valid():
    from harness.real_tasks import TASKS, validate_taskset

    commercial = [t for t in TASKS if t.difficulty == "commercial"]
    assert commercial, "commercial 태스크가 없습니다"
    for t in commercial:
        assert t.url.startswith("https://")
        assert t.success_expr.strip()
        assert t.capability.strip()
    assert validate_taskset() == [], validate_taskset()
