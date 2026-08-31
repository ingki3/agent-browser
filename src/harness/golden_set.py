"""Recall@20 골든셋 (정답 고정 10종).

`harness.recall --golden`이 사용하는 검증용 데이터셋이다.
각 항목은 "이 페이지에서 이 요소가 반드시 Top-N 안에 남아야 한다"는
사람이 검수한 정답이며, 하네스 자신의 정확성을 확인하는 기준점이다.

골든셋의 목적은 인지 엔진 성능 측정이 아니라 **측정 도구의 검증**이다.
따라서 `recall == 1.0`이 아니면 하네스나 스코어러에 결함이 있다는 뜻이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from harness.mock_sites import SITE_INDEX


@dataclass(frozen=True)
class GoldenCase:
    """단일 골든 케이스: (사이트, 정답 요소 role/name)."""

    site_id: str
    expected_role: str
    expected_name: str
    note: str = ""


#: 사람이 검수한 정답. 각 항목은 mock_sites의 golden_target과 일치해야 한다.
#:
#: **밀도 요건 (중요)**: 후보 요소가 Top-N보다 적은 페이지만 모으면
#: 프루닝이 한 번도 동작하지 않은 채 recall 1.0이 나온다(사보타주 실험에서
#: 스코어러를 무력화해도 통과했다). `s22_dense`는 후보가 60개를 넘어
#: Top-20 프루닝을 강제하며, 스코어링이 깨지면 즉시 recall이 떨어진다.
GOLDEN_SET: Tuple[GoldenCase, ...] = (
    GoldenCase("s01_login", "button", "로그인", "기본 폼 제출 버튼"),
    GoldenCase("s02_twofactor", "button", "인증 확인", "OTP 확인 버튼 (재전송과 구분)"),
    GoldenCase("s03_multistep", "button", "다음 단계", "다단계 폼 진행 버튼"),
    GoldenCase("s04_download", "link", "CSV 내려받기", "download 속성 링크"),
    GoldenCase("s09_ad_rotation", "button", "장바구니 담기", "광고 로테이션 중에도 안정적 식별"),
    GoldenCase("s10_dialog", "button", "계정 삭제", "고위험 액션 (HITL 대상)"),
    GoldenCase("s11_popup", "link", "약관 새 창으로 보기", "target=_blank 팝업 링크"),
    GoldenCase("s13_spa", "button", "설정으로 이동", "SPA 라우팅 트리거"),
    GoldenCase("s14_lazy", "button", "지연 로딩 버튼", "300ms 후 삽입되는 노드"),
    GoldenCase("s19_checkout", "button", "결제 진행", "결제 확인 다이얼로그 트리거"),
    GoldenCase(
        "s22_dense",
        "button",
        "주문 결제하기",
        "노이즈 60개 이상 — Top-20 프루닝이 실제로 동작해야 검출됨",
    ),
)

#: 프루닝 실효성을 보장해야 하는 케이스. 이 사이트에서 정답을 놓치면
#: 스코어링이 무의미하다는 뜻이다.
DENSE_CASE_SITE_ID = "s22_dense"


def validate_golden_set() -> list[str]:
    """골든셋이 Mock 사이트 정의와 일치하는지 검사한다.

    반환값이 비어 있지 않으면 골든셋과 사이트 정의가 어긋난 것이며,
    이 경우 Recall 측정 결과 자체를 신뢰할 수 없다.
    """
    problems: list[str] = []
    for case in GOLDEN_SET:
        site = SITE_INDEX.get(case.site_id)
        if site is None:
            problems.append(f"{case.site_id}: 정의되지 않은 사이트")
            continue
        if site.golden_target != case.expected_name:
            problems.append(
                f"{case.site_id}: 정답 이름 불일치 "
                f"(site={site.golden_target!r}, golden={case.expected_name!r})"
            )
        if site.golden_role != case.expected_role:
            problems.append(
                f"{case.site_id}: 정답 role 불일치 "
                f"(site={site.golden_role!r}, golden={case.expected_role!r})"
            )
        if case.expected_name not in site.html:
            problems.append(f"{case.site_id}: 정답 요소가 HTML에 존재하지 않음")
    return problems
