#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_contracts_freeze.py: Stage 0 계약 동결 검증기
Pre-provisioned by Human Supervisor / Orchestrator

Gate 0 승인 이후 `src/contracts/**`는 읽기 전용이다(src/AGENTS.md §3).
본 스크립트는 동결 기준점(태그) 대비 계약 파일이 변경되었는지 검사하고,
변경이 있으면 CI를 실패시킨다.

정당한 계약 변경 절차:
  1. 사람 감독자가 Stage 0 재동결을 승인
  2. 변경 반영 후 새 동결 태그 생성 (예: contracts-v1.1-frozen)
  3. FREEZE_TAG 상수 갱신 (오케스트레이터 소유 파일이므로 사람이 수행)

우회 수단(환경변수 등)을 의도적으로 제공하지 않는다. 에이전트가 스스로
동결을 해제할 수 있으면 동결이 아니기 때문이다.
"""

import subprocess
import sys

#: 현재 유효한 계약 동결 기준점. 재동결 시 사람 감독자가 갱신한다.
FREEZE_TAG = "contracts-v1.0-frozen"

#: 동결 대상 경로
FROZEN_PATH = "src/contracts"


def run(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(args, capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def main() -> None:
    print("=" * 60)
    print(f" Verifying contract freeze against {FREEZE_TAG} ...")
    print("=" * 60)

    # 태그 존재 확인 (얕은 클론 등으로 태그가 없으면 검증 불가)
    code, _ = run(["git", "rev-parse", "--verify", f"{FREEZE_TAG}^{{commit}}"])
    if code != 0:
        print(f"[-] 동결 태그 '{FREEZE_TAG}' 를 찾을 수 없습니다.")
        print("    CI 체크아웃 시 fetch-depth: 0 및 태그 fetch가 필요합니다.")
        sys.exit(1)

    code, out = run(["git", "diff", "--name-only", FREEZE_TAG, "HEAD", "--", FROZEN_PATH])
    if code != 0:
        print(f"[-] git diff 실행 실패: {out}")
        sys.exit(1)

    changed = [line for line in out.splitlines() if line.strip()]
    if changed:
        print(f"[-] 동결된 계약 파일이 변경되었습니다 ({len(changed)}건):")
        for path in changed:
            print(f"    - {path}")
        print()
        print("    src/contracts/** 는 Gate 0 승인 이후 읽기 전용입니다.")
        print("    변경이 불가피하면 사람 감독자에게 Stage 0 재동결을 요청하십시오.")
        sys.exit(1)

    print(f"[+] {FROZEN_PATH}: 동결 상태 유지 확인 (변경 0건)")
    print("\n[SUCCESS] Contract freeze intact.")
    sys.exit(0)


if __name__ == "__main__":
    main()
