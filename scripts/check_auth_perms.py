#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_auth_perms.py: Session Storage File Permission Validator
Pre-provisioned by Human Supervisor / Orchestrator for Gate 1 Verification
"""

import glob
import os
import stat
import sys

def main():
    if sys.platform == "win32":
        print("SKIP: POSIX permission check not applicable on Windows")
        sys.exit(0)

    auth_dir = os.path.expanduser("~/.agent-browser/auth")
    files = glob.glob(os.path.join(auth_dir, "*.enc"))
    
    if not files:
        print(f"FAIL: 검증 대상 .enc 파일이 {auth_dir} 에 존재하지 않음 (세션 저장 테스트 선행 필요)")
        sys.exit(1)

    bad_files = []
    for f in files:
        mode = stat.S_IMODE(os.stat(f).st_mode)
        if mode != 0o600:
            bad_files.append((f, oct(mode)))

    if bad_files:
        print(f"FAIL: 권한 위반 파일 {len(bad_files)}건 발견 (0600이어야 함):")
        for path, mode in bad_files:
            print(f"  - {path}: mode {mode}")
        sys.exit(1)

    print(f"PASS: 모든 세션 파일 ({len(files)}개)의 권한이 정상 0600으로 확인됨.")
    sys.exit(0)

if __name__ == "__main__":
    main()
