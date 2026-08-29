#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_gate_commands.py: AGENTS.md 게이트 명령어 구문 검증기
Pre-provisioned by Human Supervisor / Orchestrator

src/AGENTS.md §5의 게이트 검증 명령어에 포함된 `python -c "..."` 인라인 스크립트를
추출해 컴파일한다. 문서에 적힌 명령어 자체가 SyntaxError를 내는 사고를 CI에서 차단한다.

배경: AGENTS_REVIEW_02 P0-1 — 빈 시퀀스 통과(vacuous truth)를 고치려고 인라인
one-liner에 `assert` 문을 넣었다가 SyntaxError가 발생해 Gate 1을 영구히 통과할 수
없게 된 사례가 있었다. 검증기 자신도 검증 대상이라는 원칙을 적용한다.

추가로 인라인 명령어가 과도하게 길어지지 않았는지(스크립트 파일로 분리해야 하는지)도
경고한다.
"""

import re
import sys

AGENTS_MD = "src/AGENTS.md"

# 인라인 one-liner 허용 길이 상한. 초과 시 scripts/ 아래 파일로 분리할 것을 권고한다.
MAX_INLINE_LEN = 180


def extract_inline_commands(text: str):
    """`python -c "..."` 형태의 인라인 스크립트를 (행번호, 코드) 목록으로 반환."""
    found = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in re.finditer(r'python -c "(.*?)"(?:\s|$)', line):
            found.append((lineno, match.group(1).replace('\\"', '"')))
    return found


def extract_script_refs(text: str):
    """`python scripts/*.py` 형태로 참조된 스크립트 경로 목록을 반환."""
    return sorted(set(re.findall(r"python (scripts/[\w/]+\.py)", text)))


def main() -> None:
    print("=" * 60)
    print(" Verifying gate command syntax in src/AGENTS.md ...")
    print("=" * 60)

    try:
        with open(AGENTS_MD, "r", encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        print(f"[-] {AGENTS_MD} not found!")
        sys.exit(1)

    passed = True

    # 1. 인라인 python -c 스크립트 구문 검사
    commands = extract_inline_commands(text)
    if not commands:
        print("[~] No inline `python -c` commands found.")
    for lineno, code in commands:
        try:
            compile(code, f"{AGENTS_MD}:{lineno}", "exec")
        except SyntaxError as exc:
            print(f"[-] {AGENTS_MD}:{lineno} SyntaxError -> {exc.msg}")
            print(f"    {code[:100]}")
            passed = False
            continue

        if len(code) > MAX_INLINE_LEN:
            print(
                f"[-] {AGENTS_MD}:{lineno} 인라인 명령어가 {len(code)}자로 너무 깁니다 "
                f"(상한 {MAX_INLINE_LEN}). scripts/ 아래 파일로 분리하십시오."
            )
            passed = False
        else:
            print(f"[+] {AGENTS_MD}:{lineno} inline command OK ({len(code)} chars)")

    # 2. 참조된 게이트 스크립트가 실제로 존재하고 컴파일되는지 확인
    import os
    import py_compile

    for path in extract_script_refs(text):
        if not os.path.exists(path):
            print(f"[-] {AGENTS_MD} references missing script: {path}")
            passed = False
            continue
        try:
            py_compile.compile(path, doraise=True)
            print(f"[+] {path}: exists and compiles")
        except py_compile.PyCompileError as exc:
            print(f"[-] {path}: compile failed -> {exc}")
            passed = False

    if passed:
        print("\n[SUCCESS] All gate commands are syntactically valid.")
        sys.exit(0)

    print("\n[FAILURE] Gate command verification failed!")
    sys.exit(1)


if __name__ == "__main__":
    main()
