"""비인가 Egress 차단 검증 (Gate 1 항목 4).

`python -m harness.egress_test`

WS-4 `security/`의 도메인 Allowlist 및 `page.route()` 인터셉션이
비인가 도메인으로의 요청을 실제로 차단하는지 측정한다.

WS-4가 아직 구현되지 않은 Stage 1 초기에는 exit 2(측정 불가)로 명확히
실패한다. 0건 유출을 반환해 "통과"로 위장하면 게이트가 무의미해진다.
"""

from __future__ import annotations

import sys

from harness.result import MetricResult, emit, emit_error

#: 차단되어야 하는 비인가 목적지 (실제 연결을 시도하지 않는 표본)
UNAUTHORIZED_TARGETS = (
    "http://evil.example.com/exfil",
    "https://attacker.test/beacon.gif",
    "http://169.254.169.254/latest/meta-data/",  # 클라우드 메타데이터
    "http://127.0.0.1:22/",  # 로컬 서비스 스캔
    "https://pastebin.example/raw/steal",
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

    guard = EgressGuard()  # pragma: no cover
    leaks = [t for t in UNAUTHORIZED_TARGETS if guard.is_allowed(t)]  # pragma: no cover

    result = MetricResult(  # pragma: no cover
        metric="egress_leak_count",
        value=float(len(leaks)),
        threshold=0.0,
        samples=len(UNAUTHORIZED_TARGETS),
        comparison="lte",
        extra={"leaked_targets": leaks or None},
    )
    sys.exit(int(emit(result)))  # pragma: no cover


if __name__ == "__main__":
    main()
