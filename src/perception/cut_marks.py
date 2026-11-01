"""잘림 출처 표(WS-75 R2 #199 B3) — perception producer 가 **실제로 자른** 페이지 글자를 기억한다.

내용 가림(`security.public_url.content_text`)과 레시피 저장 대조(`recipes.store`)는 잘린 칸의 끝에 남은 등록 값
앞부분(≥ 4자)을 더 본다. R1 은 길이(12·40·120·200)·말줄임만 보고 그 규칙을 적용해, 안 잘린 일반 글자(`Order # 2026`)
의 연도까지 가리고 정상 레시피를 거부했다. 이제 자른 producer 가 결과 글자를 여기에 표시하고, 표시된 글자에만 끝
규칙을 적용한다.

producer(표시하는 곳):
* `perception.sanitizer` — 이름(JS `accessibleName` 내용 글자 120)·값(JS 200), JS 가 실제 자름 여부를 함께 보낸다.
* `perception.shadow_dom` — closed shadow 의 이름 120·값 200(같은 JS 규칙).
* `perception.repeated` — 묶음 표제(JS `rpClean` 40, 잘린 결과 목록을 함께 보냄)·이름/표제 표시(`_short` 40 말줄임)·
  압축 이름 첫 낱말(12).
* `recipes.keys.snapshot` — 레시피 대상 기술의 이름(120)·정규화 이름(JS 가 보낸 자름 여부).

표는 글자 자체를 키로 하는 내부 정보다 — 응답·요소 이름·핀·지문·레시피 키에는 아무것도 더하지 않는다(바이트 동일).
출처 표시가 없는 글자(페이지가 직접 쓴 말줄임 `…`·extract 항목의 page_data 자르기 등 위 producer 밖)는 끝 규칙을
적용하지 않는다 — 도구가 자르지 않은 글자이고, 등록 값 전체가 있으면 전체 일치 가림이 잡는다(코디네이터 결정 A).
같은 글자가 다른 곳에서 안 잘린 채 나오면 그 글자도 잘린 칸으로 본다(더 가리는 쪽 — 누출 방향 아님). 표는 최근
`MAX_MARKS` 개만 둔다(넘으면 가장 오래된 것부터 버린다).
"""

from __future__ import annotations

from typing import Any, Dict

#: 기억하는 잘린 글자 수 상한(관찰 한 번에 실제로 잘리는 칸은 보통 수십 개 — 세션 동안 충분하다).
MAX_MARKS = 1 << 17

_MARKS: Dict[str, None] = {}


def mark(text: Any) -> Any:
    """producer 가 실제로 자른 결과 글자를 표시한다(같은 값을 그대로 돌려준다). 빈 글자·비문자열은 무시."""
    if isinstance(text, str) and text:
        if text in _MARKS:
            del _MARKS[text]  # 최근 표시로 옮긴다
        _MARKS[text] = None
        if len(_MARKS) > MAX_MARKS:
            del _MARKS[next(iter(_MARKS))]
    return text


def is_cut(text: Any) -> bool:
    """producer 가 실제로 잘라 낸 글자인가(표시가 없으면 False — 출처를 모르면 끝 규칙을 적용하지 않는다)."""
    return isinstance(text, str) and text in _MARKS


def clear() -> None:
    """표를 비운다(시험용)."""
    _MARKS.clear()
