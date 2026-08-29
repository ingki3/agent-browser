#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_docs.py: Repository and Specification Document Integrity Checker
Pre-provisioned by Human Supervisor / Architecture Council
"""

import os
import sys
import glob
import re

def check_byte_integrity(file_path: str) -> bool:
    if not os.path.exists(file_path):
        print(f"[-] File not found: {file_path}")
        return False
    
    with open(file_path, "rb") as f:
        raw = f.read()
    
    cr_count = raw.count(b"\r")
    bel_count = raw.count(b"\x07")
    latex_count = raw.count(b"\\rightarrow") + raw.count(b"\\approx") + raw.count(b"\\times")
    
    passed = True
    if cr_count > 0:
        print(f"[-] {file_path}: Found {cr_count} Carriage Return (0x0D) bytes!")
        passed = False
    if bel_count > 0:
        print(f"[-] {file_path}: Found {bel_count} ASCII Bell (0x07) bytes!")
        passed = False
    if latex_count > 0:
        print(f"[-] {file_path}: Found {latex_count} LaTeX backslashes!")
        passed = False
        
    return passed

def check_prd_spec() -> bool:
    """
    PRD 스펙 무결성 검사.
    기준 사양서는 레포지토리 루트의 PRD.md (추적 대상)이다.
    """
    spec_path = os.environ.get("PRD_SPEC_PATH", "PRD.md")
    if not os.path.exists(spec_path):
        print(f"[-] {spec_path} does not exist!")
        return False

    with open(spec_path, "r", encoding="utf-8") as f:
        text = f.read()

    passed = True

    # 0. Byte integrity of the spec itself
    if not check_byte_integrity(spec_path):
        passed = False

    # 1. Section 4.1 scoped Action tools count
    sec41_match = re.search(r"### 4\.1 19종 액션 툴 전수 명세표(.*?)(?=### 4\.2|\Z)", text, re.DOTALL)
    if not sec41_match:
        print(f"[-] Could not locate Section 4.1 in {spec_path}!")
        passed = False
    else:
        sec41_text = sec41_match.group(1)
        table_lines = [line for line in sec41_text.splitlines() if line.startswith("| `") and "` |" in line]
        if len(table_lines) != 19:
            print(f"[-] {spec_path} §4.1 Action Tools table has {len(table_lines)} rows, expected 19!")
            passed = False

    # 2. Traceability matrix rows (at least 104)
    matrix_lines = [line for line in text.splitlines() if line.startswith("| **REVIEW_")]
    if len(matrix_lines) < 104:
        print(f"[-] {spec_path} §9 Matrix has {len(matrix_lines)} rows, expected at least 104!")
        passed = False

    if passed:
        print(f"[+] {spec_path}: Scoped table counts (19) and matrix rows (>=104) aligned.")
    return passed

def check_contracts_input_models() -> bool:
    """
    contracts/inputs.py의 액션 입력 모델 수를 검증한다.

    언더스코어로 시작하는 내부 베이스 클래스(_ElementTargetInput 등)는
    ActionType에 대응하지 않는 공통 상속 기반이므로 집계에서 제외한다.
    ACTION_INPUT_MAP 항목 수와의 일치는 tests/contracts에서 별도 검증한다.
    """
    inputs_file = os.path.join("src", "contracts", "inputs.py")
    if not os.path.exists(inputs_file):
        return True

    with open(inputs_file, "r", encoding="utf-8") as f:
        content = f.read()

    # 공개 액션 입력 모델만 집계 (선행 언더스코어 제외)
    input_classes = re.findall(r"^class\s+([A-Za-z][A-Za-z0-9_]*Input)\s*\(", content, re.M)
    if len(input_classes) != 19:
        print(
            f"[-] {inputs_file} has {len(input_classes)} public Input classes, expected 19!"
        )
        print(f"    found: {sorted(input_classes)}")
        return False

    # ACTION_INPUT_MAP 엔트리 수도 함께 확인
    map_match = re.search(r"ACTION_INPUT_MAP[^=]*=\s*\{(.*?)\n\}", content, re.DOTALL)
    if not map_match:
        print(f"[-] {inputs_file}: ACTION_INPUT_MAP 정의를 찾을 수 없습니다!")
        return False
    entries = re.findall(r"ActionType\.[A-Z_]+\s*:", map_match.group(1))
    if len(entries) != 19:
        print(f"[-] {inputs_file}: ACTION_INPUT_MAP has {len(entries)} entries, expected 19!")
        return False

    return True

def main():
    print("=" * 60)
    print(" Running Document & Specification Integrity Checks...")
    print("=" * 60)
    
    # Active specification, scripts, and handbook files
    # (PRD.md는 check_prd_spec()에서 바이트 무결성까지 함께 검증)
    active_files = [
        "README.md",
        "src/AGENTS.md"
    ] + glob.glob("src/**/*.md", recursive=True) + glob.glob("scripts/**/*.py", recursive=True)
    active_files = list(set(active_files))
    all_passed = True
    
    for f in active_files:
        if os.path.exists(f):
            if not check_byte_integrity(f):
                all_passed = False
            else:
                print(f"[+] {f}: Byte integrity clean (0 CR, 0 BEL, 0 LaTeX).")
            
    if not check_prd_spec():
        all_passed = False
        
    if not check_contracts_input_models():
        all_passed = False
    else:
        print("[+] Input models alignment verified.")
        
    if all_passed:
        print("\n[SUCCESS] All active document and script integrity checks passed!")
        sys.exit(0)
    else:
        print("\n[FAILURE] Integrity checks failed!")
        sys.exit(1)

if __name__ == "__main__":
    main()
