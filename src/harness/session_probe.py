"""세션 만료 프로브 오탐율(FPR) 검증 (Gate 1 / Gate 3 항목).

`python -m harness.session_probe --runs 50`

무인 모드에서 오탐은 정상 세션을 만료로 오인해 태스크를 중단시키므로
비용이 크다. PRD §5.1-3은 FPR ≤ 1.0%를 요구한다.

측정 방법: 정상 세션 시나리오(만료가 아님)를 다수 투입해 몇 건이
만료로 잘못 판정되는지 센다. 동시에 진짜 만료 케이스를 놓치지 않는지
(재현율)도 함께 확인해, 프로브를 "항상 유효"로 만들어 FPR을 0으로
위장하는 것을 차단한다.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, List, Tuple

from contracts import thresholds

from harness.result import MetricResult, emit, emit_error

if TYPE_CHECKING:
    from browser import PageSignals as _PageSignals

    CaseList = List[Tuple[str, _PageSignals]]
else:
    CaseList = list


def _negative_cases() -> "CaseList":
    """정상 세션(만료 아님) 시나리오. 여기서 expired=True면 오탐이다."""
    from browser import PageSignals, ProfileProbeConfig

    return [
        (
            "대시보드 정상",
            PageSignals(url="https://app.example.com/dashboard", http_status=200),
        ),
        (
            "보호 엔드포인트 200",
            PageSignals(
                url="https://app.example.com/reports",
                http_status=200,
                custom_probe_status=200,
            ),
        ),
        (
            "로그인 경로 방문했으나 인증 마커 존재",
            PageSignals(
                url="https://app.example.com/login",
                http_status=200,
                visible_password_inputs=1,
                has_authenticated_markers=True,
            ),
        ),
        (
            "비밀번호 변경 페이지 (정상 세션)",
            PageSignals(
                url="https://app.example.com/settings/password",
                http_status=200,
                visible_password_inputs=2,
                has_authenticated_markers=True,
            ),
        ),
        (
            "검색 결과에 password 필드 포함",
            PageSignals(
                url="https://app.example.com/search?q=login",
                http_status=200,
                visible_password_inputs=1,
            ),
        ),
        (
            "로그인 URL 단독 방문 (신호 1개)",
            PageSignals(url="https://app.example.com/login", http_status=200),
        ),
        (
            "404 페이지 (인증과 무관)",
            PageSignals(url="https://app.example.com/missing", http_status=404),
        ),
        (
            "허용된 로그인 폼 노출 경로",
            PageSignals(
                url="https://app.example.com/embed/auth",
                http_status=200,
                visible_password_inputs=1,
                redirected_to_login=True,
            ),
        ),
        (
            "500 서버 오류 (세션 유효)",
            PageSignals(url="https://app.example.com/reports", http_status=500),
        ),
        (
            "SPA 라우팅 후 정상 화면",
            PageSignals(
                url="https://app.example.com/app#/settings",
                http_status=200,
                has_authenticated_markers=True,
            ),
        ),
    ]


def _positive_cases() -> "CaseList":
    """진짜 만료 시나리오. 여기서 expired=False면 미탐(재현율 저하)이다."""
    from browser import PageSignals

    return [
        (
            "보호 엔드포인트 401",
            PageSignals(
                url="https://app.example.com/reports", custom_probe_status=401
            ),
        ),
        (
            "네비게이션 401",
            PageSignals(url="https://app.example.com/reports", http_status=401),
        ),
        (
            "네비게이션 403",
            PageSignals(url="https://app.example.com/admin", http_status=403),
        ),
        (
            "로그인 리다이렉트 + 패스워드 인풋",
            PageSignals(
                url="https://app.example.com/login?next=/reports",
                http_status=200,
                visible_password_inputs=1,
                redirected_to_login=True,
            ),
        ),
        (
            "signin 경로 리다이렉트",
            PageSignals(
                url="https://app.example.com/auth/signin",
                http_status=200,
                visible_password_inputs=1,
                redirected_to_login=True,
            ),
        ),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="세션 만료 프로브 오탐율 검증")
    parser.add_argument("--runs", type=int, default=50, help="반복 횟수")
    args = parser.parse_args()

    try:
        from browser import ProfileProbeConfig, detect_expiry
    except ImportError:
        sys.exit(
            int(
                emit_error(
                    "session_probe_fpr",
                    "세션 프로브(WS-1 browser/)가 아직 구현되지 않아 측정할 수 없습니다.",
                )
            )
        )

    config = ProfileProbeConfig(login_form_allowed_paths=("/embed/auth",))

    negatives = _negative_cases()
    positives = _positive_cases()

    # --runs 만큼 반복해 결정론성도 함께 확인한다.
    repeats = max(1, args.runs // len(negatives))
    false_positives: List[str] = []
    false_negatives: List[str] = []

    for _ in range(repeats):
        for label, signals in negatives:
            if detect_expiry(signals, config).expired:
                false_positives.append(label)
        for label, signals in positives:
            if not detect_expiry(signals, config).expired:
                false_negatives.append(label)

    total_negatives = len(negatives) * repeats
    fpr = len(false_positives) / total_negatives if total_negatives else 1.0

    if false_positives:
        for label in sorted(set(false_positives)):
            print(f"[-] 오탐: {label}", file=sys.stderr)
    if false_negatives:
        # 미탐은 FPR 지표에 잡히지 않으므로 별도로 실패시킨다.
        for label in sorted(set(false_negatives)):
            print(f"[-] 미탐(만료 미검출): {label}", file=sys.stderr)
        sys.exit(
            int(
                emit_error(
                    "session_probe_fpr",
                    f"만료 케이스 {len(set(false_negatives))}종을 검출하지 못했습니다. "
                    "프로브가 '항상 유효'로 동작할 위험이 있습니다.",
                )
            )
        )

    result = MetricResult(
        metric="session_probe_fpr",
        value=round(fpr, 6),
        threshold=thresholds.SESSION_PROBE_FPR,
        samples=total_negatives,
        comparison="lte",
        extra={
            "false_positives": sorted(set(false_positives)) or None,
            "positive_cases_detected": len(positives) * repeats,
        },
    )
    sys.exit(int(emit(result)))


if __name__ == "__main__":
    main()
