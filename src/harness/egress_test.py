"""비인가 Egress 차단 검증 (Gate 1 항목 4).

`python -m harness.egress_test`

WS-4 `security/`의 도메인 Allowlist 및 `page.route()` 인터셉션이
비인가 도메인으로의 요청을 실제로 차단하는지 측정한다.

**표본 설계 원칙 (중요)**:
초기 표본은 `evil.example.com` 같은 '명백히 무관한 도메인'만 담고 있었다.
그 상태에서는 allowlist 매칭을 접미사 비교(`host.endswith(allowed)`)로
되돌려도 유출 0건으로 통과했다(사보타주 실험으로 확인). 실제 공격은
명백한 외부 도메인이 아니라 **허용 도메인을 닮은 문자열**로 들어온다.

따라서 표본을 두 축으로 구성한다:
1. `UNAUTHORIZED_TARGETS` — 차단되어야 하는 목적지 (우회 기법 포함)
2. `AUTHORIZED_TARGETS`   — 허용되어야 하는 목적지 (과차단 탐지)

가드를 "전부 차단"으로 만들면 유출은 0이 되지만 브라우저가 아무것도
못 하므로, 정상 요청이 통과하는지도 함께 검증한다.
"""

from __future__ import annotations

import sys
from typing import List, Tuple

from harness.result import MetricResult, emit, emit_error

#: 가드가 사용할 허용 도메인 (측정 기준)
ALLOWED_DOMAINS: Tuple[str, ...] = ("example.com", "cdn.example.net")

#: 차단되어야 하는 목적지. (URL, 공격 유형) 쌍.
UNAUTHORIZED_TARGETS: Tuple[Tuple[str, str], ...] = (
    # --- 명백한 외부 도메인 ---
    ("http://evil.test/exfil", "무관한 외부 도메인"),
    ("https://attacker.test/beacon.gif", "비콘 픽셀"),
    ("https://pastebin.test/raw/steal", "데이터 유출 목적지"),
    # --- 허용 도메인을 닮은 우회 시도 (접미사 혼동) ---
    ("https://evil-example.com/steal", "접미사 혼동: 하이픈 접두"),
    ("https://notexample.com/steal", "접미사 혼동: 문자열 접두"),
    ("https://example.com.evil.test/x", "접미사 혼동: 허용도메인을 서브도메인으로"),
    ("https://fakeexample.com/x", "접미사 혼동: 문자 삽입"),
    ("https://cdn.example.net.attacker.test/x", "접미사 혼동: 2차 도메인 위장"),
    # --- 내부망 / SSRF ---
    ("http://169.254.169.254/latest/meta-data/", "클라우드 메타데이터"),
    ("http://metadata.google.internal/x", "GCP 메타데이터"),
    ("http://127.0.0.1:22/", "로컬 서비스 스캔"),
    ("http://10.0.0.5/internal", "사설 대역"),
    ("http://192.168.1.1/admin", "사설 대역 라우터"),
    # --- 스킴 우회 ---
    ("file:///etc/passwd", "파일 스킴"),
    ("javascript:alert(1)", "자바스크립트 스킴"),
)

#: 허용되어야 하는 목적지. 과차단(전부 막기)을 탐지한다.
AUTHORIZED_TARGETS: Tuple[Tuple[str, str], ...] = (
    ("https://example.com/page", "허용 도메인 루트"),
    ("https://api.example.com/v1/data", "허용 도메인의 하위 도메인"),
    ("https://cdn.example.net/asset.js", "두 번째 허용 도메인"),
    ("https://deep.nested.example.com/x", "다단계 하위 도메인"),
)


def main() -> None:
    try:
        from security import EgressGuard  # type: ignore[import-not-found]
    except ImportError:
        sys.exit(
            int(
                emit_error(
                    "egress_leak_count",
                    "Egress 가드(WS-4 security/)가 아직 구현되지 않아 측정할 수 없습니다. "
                    "WS-4 완료 후 실행하십시오.",
                )
            )
        )

    guard = EgressGuard(allowed_domains=ALLOWED_DOMAINS)

    leaks: List[str] = []
    for url, attack in UNAUTHORIZED_TARGETS:
        if guard.is_allowed(url):
            leaks.append(f"{url} ({attack})")

    over_blocked: List[str] = []
    for url, note in AUTHORIZED_TARGETS:
        if not guard.is_allowed(url):
            over_blocked.append(f"{url} ({note})")

    for leak in leaks:
        print(f"[-] 유출 허용: {leak}", file=sys.stderr)
    for blocked in over_blocked:
        print(f"[-] 과차단: {blocked}", file=sys.stderr)

    # 과차단은 유출 지표에 잡히지 않는다. "전부 차단"으로 만들면
    # 유출 0건이 되지만 브라우저가 동작하지 않으므로 별도로 실패시킨다.
    if over_blocked:
        sys.exit(
            int(
                emit_error(
                    "egress_leak_count",
                    f"허용 도메인 {len(over_blocked)}건이 차단됐습니다. "
                    "가드가 과도하게 차단하면 유출 0건이어도 사용할 수 없습니다.",
                )
            )
        )

    result = MetricResult(
        metric="egress_leak_count",
        value=float(len(leaks)),
        threshold=0.0,
        samples=len(UNAUTHORIZED_TARGETS),
        comparison="lte",
        extra={
            "leaked_targets": leaks or None,
            "authorized_checked": len(AUTHORIZED_TARGETS),
            "attack_categories": len({a for _, a in UNAUTHORIZED_TARGETS}),
        },
    )
    sys.exit(int(emit(result)))


if __name__ == "__main__":
    main()
