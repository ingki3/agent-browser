"""실환경 에이전트 태스크셋 (WS-9).

Mock 태스크와의 차이:
* 후보 요소가 수백~수천 개 (Mock 최대 69개)
* 페이지 구조가 우리 통제 밖 — 사이트가 개편되면 태스크가 깨진다
* 네트워크 지연과 일시 장애가 존재

**성공 판정은 LLM 자기 보고를 신뢰하지 않는다.** 에이전트가 `finish`를
반환해도, 최종 페이지 상태를 JS로 검증해 실제 달성 여부를 판정한다.
자기 보고를 믿으면 "했다고 주장하지만 안 한" 경우를 걸러낼 수 없다.

태스크 선정 기준:
* 로그인·결제 등 부작용이 있는 동작은 제외 (공개 사이트에 대한 예의)
* robots.txt를 존중하고 읽기/탐색 위주로 구성
* 난이도를 3단계로 분산해 상한과 하한을 함께 관측
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class RealTask:
    """단일 실환경 태스크."""

    task_id: str
    url: str
    goal: str
    #: 최종 상태 검증 JS. true를 반환해야 성공.
    #: 에이전트의 finish 선언과 무관하게 독립 판정한다.
    success_expr: str
    #: 난이도. easy=단일 액션, medium=2~3스텝, hard=탐색 필요
    difficulty: str = "medium"
    #: 이 태스크가 검증하려는 능력
    capability: str = ""
    max_steps: int = 8


TASKS: Tuple[RealTask, ...] = (
    # --- easy: 단일 클릭으로 완료 ---
    RealTask(
        "hn-newest",
        "https://news.ycombinator.com",
        "'new' 링크를 클릭해서 최신 글 목록 페이지로 이동하기",
        "location.pathname.includes('/newest')",
        difficulty="easy",
        capability="네비게이션 링크 식별",
        max_steps=4,
    ),
    RealTask(
        "hn-ask",
        "https://news.ycombinator.com",
        "'ask' 링크를 클릭해서 Ask HN 목록으로 이동하기",
        "location.pathname.includes('/ask')",
        difficulty="easy",
        capability="유사 링크 중 정확한 선택",
        max_steps=4,
    ),
    RealTask(
        "wiki-toc-section",
        "https://en.wikipedia.org/wiki/Web_browser",
        "목차에서 'History' 섹션 링크를 클릭해 해당 절로 이동하기",
        "location.hash.toLowerCase().includes('history')",
        difficulty="easy",
        capability="고밀도 페이지에서 특정 링크 식별 (후보 1000+)",
        max_steps=4,
    ),
    RealTask(
        "iana-example",
        "https://www.iana.org/help/example-domains",
        "'IANA-managed Reserved Domains' 링크를 클릭해 예약 도메인 목록으로 이동하기",
        # 시작 URL(/help/example-domains)에서는 거짓이어야 한다.
        # 실측 — 이전 정의는 시작 URL이 이미 /domains/reserved라
        # includes('/domains')가 초기부터 참이었다(무의미한 검증식).
        "location.pathname.includes('/domains') && "
        "!location.pathname.includes('/help')",
        difficulty="easy",
        capability="단순 네비게이션",
        max_steps=4,
    ),
    # --- medium: 입력 + 제출 ---
    RealTask(
        "wiki-search",
        "https://en.wikipedia.org/wiki/Main_Page",
        "검색창에 'Python programming language'를 입력하고 검색을 실행하기",
        "location.href.toLowerCase().includes('python')",
        difficulty="medium",
        capability="텍스트 입력 + 폼 제출",
        max_steps=6,
    ),
    RealTask(
        "wiki-search-css",
        "https://en.wikipedia.org/wiki/Main_Page",
        "검색창에 'Cascading Style Sheets'를 입력하고 검색 결과로 이동하기",
        "location.href.toLowerCase().includes('cascading') || "
        "location.href.toLowerCase().includes('css')",
        difficulty="medium",
        capability="텍스트 입력 + 폼 제출 (반복 검증)",
        max_steps=6,
    ),
    RealTask(
        "httpbin-form",
        "https://httpbin.org/forms/post",
        "고객 이름(custname) 입력란에 'agent-browser'를 입력하기",
        "document.querySelector('input[name=custname]')?.value === 'agent-browser'",
        difficulty="medium",
        capability="라벨 기반 입력 필드 식별",
        max_steps=5,
    ),
    RealTask(
        "httpbin-size",
        "https://httpbin.org/forms/post",
        "피자 크기에서 'Large' 라디오 버튼을 선택하기",
        "document.querySelector('input[value=large]')?.checked === true",
        difficulty="medium",
        capability="라디오 버튼 선택",
        max_steps=5,
    ),
    RealTask(
        "httpbin-topping",
        "https://httpbin.org/forms/post",
        "토핑 중 'Bacon' 체크박스를 선택하기",
        "document.querySelector('input[value=bacon]')?.checked === true",
        difficulty="medium",
        capability="체크박스 토글",
        max_steps=5,
    ),
    # --- hard: 탐색 또는 다단계 ---
    RealTask(
        "wiki-two-hop",
        "https://en.wikipedia.org/wiki/Main_Page",
        "검색창에서 'HTML'을 검색한 뒤, 결과 문서로 이동하기",
        "location.href.toLowerCase().includes('html')",
        difficulty="hard",
        capability="입력 -> 제출 -> 결과 확인 다단계",
        max_steps=8,
    ),
    RealTask(
        "mdn-search",
        "https://developer.mozilla.org/en-US/",
        "검색창에 'flexbox'를 입력하고 검색하기",
        "location.href.toLowerCase().includes('flexbox') || "
        "location.search.toLowerCase().includes('flexbox')",
        difficulty="hard",
        capability="SPA 검색 인터페이스",
        max_steps=8,
    ),
    RealTask(
        "hn-comments",
        "https://news.ycombinator.com",
        "첫 번째 기사의 댓글 링크(comments)를 클릭해 댓글 페이지로 이동하기",
        "location.pathname.includes('/item')",
        difficulty="hard",
        capability="반복 목록에서 특정 위치 요소 선택",
        max_steps=6,
    ),
    # --- multistep: 5스텝 이상, 상태를 누적해야 완료 ---
    #
    # 기존 태스크는 최대 4스텝이라 긴 호흡의 실패 모드(중간 상태 소실,
    # 에포크 갱신 후 요소 참조, 반복 액션 누적)를 관측할 수 없었다.
    # 아래 태스크들은 이전 스텝의 결과 위에 다음 액션을 쌓아야 한다.
    RealTask(
        "todo-add-two",
        "https://demo.playwright.dev/todomvc/",
        "할 일 목록에 'buy milk'를 추가하고, 이어서 'walk dog'도 추가하기",
        "document.querySelectorAll('.todo-list li').length === 2",
        difficulty="multistep",
        capability="같은 입력창에 반복 입력 (상태 누적)",
        max_steps=10,
    ),
    RealTask(
        "todo-add-complete",
        "https://demo.playwright.dev/todomvc/",
        "할 일에 'write report'를 추가한 뒤, 그 항목의 완료 체크박스를 눌러 완료 처리하기",
        "document.querySelectorAll('.todo-list li.completed').length >= 1",
        difficulty="multistep",
        capability="생성 -> 생성된 요소 조작 (신규 요소 참조)",
        max_steps=10,
    ),
    RealTask(
        "todo-add-three",
        "https://demo.playwright.dev/todomvc/",
        "할 일 목록에 'task one', 'task two', 'task three'를 차례로 모두 추가하기",
        "document.querySelectorAll('.todo-list li').length === 3",
        difficulty="multistep",
        capability="동일 입력창 3회 반복 (긴 호흡의 상태 누적)",
        max_steps=12,
    ),
    RealTask(
        "internet-login",
        "https://the-internet.herokuapp.com/login",
        "사용자명에 'tomsmith', 비밀번호에 'SuperSecretPassword!'를 입력하고 Login 버튼 누르기",
        "location.pathname.includes('/secure')",
        difficulty="multistep",
        capability="다중 필드 입력 후 제출 (공식 테스트 계정)",
        max_steps=8,
    ),
    RealTask(
        "internet-checkbox-both",
        "https://the-internet.herokuapp.com/checkboxes",
        "페이지의 체크박스 두 개를 모두 체크된 상태로 만들기",
        "[...document.querySelectorAll('input[type=checkbox]')]"
        ".every(c => c.checked)",
        difficulty="multistep",
        capability="여러 요소를 순회하며 상태 통일 (초기 상태 상이)",
        max_steps=8,
    ),
    RealTask(
        "internet-dropdown-two",
        "https://the-internet.herokuapp.com/dropdown",
        "드롭다운에서 'Option 1'을 선택한 뒤, 다시 'Option 2'로 변경하기",
        "document.querySelector('#dropdown')?.value === '2'",
        difficulty="multistep",
        capability="같은 요소에 연속 조작 (마지막 상태가 정답)",
        max_steps=8,
    ),
    # --- dynamic: 액션 후 DOM이 바뀌거나 지연 로드되는 상황 ---
    #
    # 기존 태스크는 모두 '이미 존재하는 요소'를 다뤘다. 실제 웹에서는
    # 클릭 후에야 요소가 생기거나(비동기 로드), 비활성이 풀리거나,
    # 스크롤해야 나타난다. 이 구간은 에포크 갱신·재관찰 경로를 검증한다.
    RealTask(
        "dyn-loading-wait",
        "https://the-internet.herokuapp.com/dynamic_loading/2",
        "Start 버튼을 누르고, 로딩이 끝나 'Hello World!' 문구가 나타날 때까지 기다리기",
        # #finish는 초기에 DOM에 아예 없다(실측). 존재 자체가 성공 신호다.
        "!!document.querySelector('#finish') && "
        "document.querySelector('#finish').innerText.includes('Hello World')",
        difficulty="dynamic",
        capability="비동기 로딩 완료 대기 (요소가 나중에 생성됨)",
        max_steps=8,
    ),
    RealTask(
        "dyn-add-elements",
        "https://the-internet.herokuapp.com/add_remove_elements/",
        "'Add Element' 버튼을 두 번 눌러 Delete 버튼을 2개 만들기",
        "document.querySelectorAll('.added-manually').length === 2",
        difficulty="dynamic",
        capability="클릭으로 신규 요소 생성 (반복 시 DOM 증가)",
        max_steps=8,
    ),
    RealTask(
        "dyn-enable-input",
        "https://the-internet.herokuapp.com/dynamic_controls",
        "'Enable' 버튼을 눌러 비활성화된 입력창을 활성화하기",
        # Enable은 비동기다. 버튼 텍스트가 'Disable'로 바뀌면 완료.
        "document.querySelector('#input-example input')?.disabled === false",
        difficulty="dynamic",
        capability="비활성 요소를 활성화 (초기에 조작 불가한 대상)",
        max_steps=8,
    ),
    RealTask(
        "dyn-infinite-scroll",
        "https://the-internet.herokuapp.com/infinite_scroll",
        "페이지 맨 아래로 스크롤해서 새로운 문단이 추가로 로드되게 하기",
        # 초기 2개, 스크롤 시 증가(실측).
        "document.querySelectorAll('.jscroll-added').length >= 3",
        difficulty="dynamic",
        capability="스크롤 기반 지연 로딩 (뷰포트 밖 콘텐츠)",
        max_steps=8,
    ),
    RealTask(
        "dyn-frame-navigate",
        "https://the-internet.herokuapp.com/frames",
        "'iFrame' 링크를 클릭해 iframe 예제 페이지로 이동하기",
        "location.pathname.includes('/iframe')",
        difficulty="dynamic",
        capability="프레임 예제 페이지 네비게이션",
        max_steps=6,
    ),
)

#: 난이도별 태스크 수 (커버리지 검증용)
DIFFICULTY_LEVELS: Tuple[str, ...] = ("easy", "medium", "hard", "multistep", "dynamic")


def tasks_by_difficulty() -> Dict[str, int]:
    counts: Dict[str, int] = {level: 0 for level in DIFFICULTY_LEVELS}
    for task in TASKS:
        counts[task.difficulty] = counts.get(task.difficulty, 0) + 1
    return counts


def get_task(task_id: str) -> Optional[RealTask]:
    return next((t for t in TASKS if t.task_id == task_id), None)


def validate_taskset() -> list:
    """태스크셋 자체의 정합성을 검사한다."""
    problems = []
    seen = set()
    for task in TASKS:
        if task.task_id in seen:
            problems.append(f"중복 task_id: {task.task_id}")
        seen.add(task.task_id)
        if not task.success_expr.strip():
            problems.append(f"{task.task_id}: 성공 검증식 없음")
        if not task.url.startswith("https://"):
            problems.append(f"{task.task_id}: https가 아님")
        if task.difficulty not in DIFFICULTY_LEVELS:
            problems.append(f"{task.task_id}: 알 수 없는 난이도 {task.difficulty}")
        if task.max_steps < 2:
            problems.append(f"{task.task_id}: max_steps가 너무 작음")

    counts = tasks_by_difficulty()
    for level in DIFFICULTY_LEVELS:
        if counts.get(level, 0) < 2:
            problems.append(
                f"난이도 '{level}' 태스크가 {counts.get(level, 0)}개뿐 — "
                "난이도별 최소 2개가 필요하다"
            )
    return problems
