#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_harness_coverage.py: 하네스 커버리지 자기검증 강제
Pre-provisioned by Human Supervisor / Orchestrator

src/AGENTS.md §5 "하네스 설계 필수 규칙" 규칙 1을 기계적으로 강제한다.

배경:
게이트가 만점을 보고했으나 실제로는 기능이 파손돼 있어도 통과하던
사고가 4건 발생했다(self_healing 하위 단계, actions_test 11종 누락,
egress_test 우회 표본 부재, recall 프루닝 미동작). 공통 원인은
"측정 범위를 보고하지 않아 미측정 경로를 아무도 몰랐다"는 점이다.

본 스크립트는 판정형 하네스가 다음을 갖췄는지 정적으로 검사한다:
1. 커버리지 관련 필드를 `extra`에 보고하는가
2. 커버리지 미달 시 `emit_error`로 측정을 무효화하는가

주의: 정적 검사이므로 "커버리지 로직이 올바른가"까지는 보증하지 못한다.
실제 탐지 능력은 규칙 5의 사보타주 검증으로 확인해야 한다.
"""

import ast
import os
import sys
from typing import List, Tuple

HARNESS_DIR = os.path.join("src", "harness")

#: 판정형 하네스 — 커버리지 자기검증이 필수인 모듈.
#: (모듈명, 사람이 읽을 커버리지 요건)
JUDGMENT_HARNESSES: List[Tuple[str, str]] = [
    ("actions_test", "ActionType 19종 전수 실행"),
    ("self_healing", "치유 사다리 4단계 전수 발동"),
    ("recall", "프루닝이 실제 동작한 페이지 >= 1"),
    ("selfcheck", "13대 시나리오 전수 커버"),
    ("egress_test", "우회 표본 및 과차단 검사 포함"),
    ("staleness", "미탐(제거된 요소를 fresh로 판정) 검사"),
    ("session_probe", "미탐(만료 미검출) 검사"),
    ("contract_selftest", "규약 키 및 exit code 분기 검증"),
    # --- Gate 3-B ---
    ("mcp_smoke", "19종 툴 전수 왕복 호출"),
    ("flaky_test", "동적 시나리오 포함 및 관찰 오류 구분"),
    ("latency_test", "관찰+액션 구간 모두 측정"),
    ("ipi_test", "정상 표본 대비 오탐율 동시 측정"),
    ("webarena", "멀티스텝 태스크 포함"),
]

#: 커버리지 보고를 나타내는 식별자 조각.
#: 새 하네스가 다른 이름을 쓰면 여기에 추가한다. 단, "이름만 맞추면
#: 통과"하는 검사이므로 실제 탐지 능력은 규칙 5의 사보타주로 확인해야 한다.
COVERAGE_HINTS = (
    "covered",
    "required",
    "missing",
    "unused",
    "missed",
    "stages_",
    "scenarios_",
    "effective",
    "false_positives",
    "over_blocked",
    "authorized",
    "actions_measured",  # latency_test: 측정된 액션 종류 수
    "multistep_run",  # webarena: 멀티스텝 태스크 실행 수
    "observation_errors",  # flaky_test: 관찰 오류 구분
    "tools_called",  # mcp_smoke: 호출된 툴 수
    "benign_samples",  # ipi_test: 정상 표본 수
)


def _module_path(name: str) -> str:
    return os.path.join(HARNESS_DIR, f"{name}.py")


def check_module(name: str, requirement: str) -> List[str]:
    """단일 하네스의 커버리지 자기검증 여부를 검사한다."""
    path = _module_path(name)
    problems: List[str] = []

    if not os.path.exists(path):
        return [f"{name}: 모듈이 존재하지 않습니다 ({path})"]

    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()

    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return [f"{name}: 구문 오류 - {exc}"]

    # 1) 커버리지 관련 식별자가 등장하는가
    identifiers = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {
        node.arg for node in ast.walk(tree) if isinstance(node, ast.arg) and node.arg
    } | {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    joined = " ".join(str(i) for i in identifiers)
    if not any(hint in joined for hint in COVERAGE_HINTS):
        problems.append(
            f"{name}: 커버리지 보고 흔적이 없습니다 (요건: {requirement}). "
            "측정 범위를 extra에 담고 미달 시 실패시키십시오."
        )

    # 2) emit_error 호출이 존재하는가 (측정 무효화 경로)
    has_emit_error = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "emit_error"
        for node in ast.walk(tree)
    )
    if not has_emit_error:
        problems.append(
            f"{name}: emit_error 호출이 없습니다. 커버리지 미달·측정 불가 상황을 "
            "exit 2로 구분해야 합니다."
        )

    return problems


def main() -> int:
    print("[*] 하네스 커버리지 자기검증 검사 (AGENTS.md §5 규칙 1)")
    all_problems: List[str] = []

    for name, requirement in JUDGMENT_HARNESSES:
        problems = check_module(name, requirement)
        if problems:
            all_problems.extend(problems)
        else:
            print(f"    [+] {name}: OK ({requirement})")

    if all_problems:
        print()
        for problem in all_problems:
            print(f"    [-] {problem}")
        print()
        print("[FAILURE] 커버리지 자기검증이 누락된 하네스가 있습니다.")
        print("          측정하지 않은 경로는 파손돼도 게이트를 통과합니다.")
        return 1

    print()
    print("[SUCCESS] 모든 판정형 하네스가 커버리지 자기검증을 갖췄습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
