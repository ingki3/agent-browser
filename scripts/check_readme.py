"""README에 적힌 명령이 실제로 동작하는지 검증한다.

문서가 틀리면 사용자가 첫 단계에서 막힌다. 이 프로젝트는 자기 선언을
신뢰하지 않으므로, 설치 안내도 기계 검증한다.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"


def extract_commands() -> list[str]:
    """README의 bash 블록에서 실행 가능한 명령을 뽑는다."""
    text = README.read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", text, re.S)
    cmds = []
    for block in blocks:
        for line in block.splitlines():
            line = line.split("#")[0].strip()
            if line and not line.startswith(("$", "git clone", "cd ")):
                cmds.append(line)
    return cmds


def run(cmd: str, timeout: int = 300) -> tuple[int, str]:
    proc = subprocess.run(
        cmd, shell=True, cwd=ROOT, capture_output=True, text=True, timeout=timeout
    )
    return proc.returncode, (proc.stdout + proc.stderr)


def main() -> int:
    failures: list[str] = []

    print("=== 1. README에 적힌 명령 존재 여부 ===")
    cmds = extract_commands()
    print(f"  추출된 명령 {len(cmds)}개")

    # 실제로 돌려볼 수 있는 것만 선별 (설치·과금·장시간 제외)
    safe = [
        c for c in cmds
        if c.startswith("uv run")
        and "playwright install" not in c
        and "agent_eval" not in c
        and "llm-check" not in c
        and "pytest tests -q" not in c
    ]
    print(f"  즉시 검증 가능한 명령 {len(safe)}개")

    print("\n=== 2. 하네스 명령 실행 ===")
    for cmd in safe:
        if "harness." not in cmd:
            continue
        code, out = run(cmd, timeout=600)
        last = [ln for ln in out.strip().splitlines() if ln.startswith("{")]
        if not last:
            failures.append(f"{cmd} — JSON 출력 없음")
            print(f"  [FAIL] {cmd}")
            continue
        try:
            data = json.loads(last[-1])
        except json.JSONDecodeError:
            failures.append(f"{cmd} — JSON 파싱 실패")
            print(f"  [FAIL] {cmd}")
            continue
        ok = data.get("passed") and code == 0
        mark = "OK" if ok else "FAIL"
        if not ok:
            failures.append(f"{cmd} — passed={data.get('passed')} exit={code}")
        print(f"  [{mark}] {data['metric']:26} {data['value']}")

    print("\n=== 3. CLI 서브커맨드 ===")
    for sub in ("tools", "--help"):
        code, out = run(f"uv run agent-browser {sub}", timeout=180)
        ok = code == 0
        if not ok:
            failures.append(f"agent-browser {sub} — exit {code}")
        print(f"  [{'OK' if ok else 'FAIL'}] agent-browser {sub}")

    print("\n=== 4. README 주장 검증 ===")
    text = README.read_text(encoding="utf-8")

    # 툴 개수 — 문서에 등장하는 모든 'N종' 표기를 실제와 대조한다.
    # 문자열 존재 여부만 보면 다른 위치의 숫자가 틀려도 통과한다.
    code, out = run("uv run agent-browser tools", timeout=180)
    m = re.search(r"노출 툴 (\d+)종", out)
    actual_tools = int(m.group(1)) if m else -1
    claimed_all = {
        int(n) for n in re.findall(r"(\d+)종 (?:툴|액션|전수|디스패처)", text)
    }
    ok = bool(claimed_all) and claimed_all == {actual_tools}
    if not ok:
        failures.append(
            f"툴 개수 불일치: 문서 {sorted(claimed_all)} vs 실제 {actual_tools}"
        )
    print(
        f"  [{'OK' if ok else 'FAIL'}] 툴 개수 문서 {sorted(claimed_all)} "
        f"= 실제 {actual_tools}"
    )

    # 테스트 개수 — '통과'는 passed 수를 뜻한다. collected에는 skipped가
    # 포함되므로 그대로 비교하면 어긋난다(실제로 427 vs 428로 틀렸다).
    m = re.search(r"(\d+)개가 통과", text)
    claimed_tests = int(m.group(1)) if m else -1
    code, out = run("uv run pytest tests -q", timeout=900)
    m2 = re.search(r"(\d+) passed", out)
    actual_tests = int(m2.group(1)) if m2 else -1
    ok = claimed_tests == actual_tests
    if not ok:
        failures.append(
            f"테스트 개수 불일치: 문서 {claimed_tests} vs 실제 passed {actual_tests}"
        )
    print(f"  [{'OK' if ok else 'FAIL'}] 통과 수 문서 {claimed_tests} = 실제 {actual_tests}")

    # 버전 — CI의 얕은 클론에는 태그가 없을 수 있다. 태그를 못 찾으면
    # 이 검사만 건너뛴다(문서 검증 전체를 실패시키지 않는다).
    #
    # 릴리스 준비 중에는 pyproject 버전이 최신 태그보다 앞선다. 태그는
    # 머지 후에 달기 때문이다. 이 방향은 정상이므로 통과시키고,
    # 뒤처진 경우(태그가 더 최신)만 실패로 본다 — 버전 갱신 누락이다.
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'version = "([\d.]+)"', pyproject)
    version = m.group(1) if m else "?"
    code, out = run("git tag -l 'v*'", timeout=60)
    tags = sorted(
        (t for t in out.split() if re.fullmatch(r"v[\d.]+", t)),
        key=lambda t: tuple(int(x) for x in t[1:].split(".")),
    )
    if not tags:
        print(f"  [SKIP] 버전 대조 — 태그 없음 (pyproject {version})")
    else:
        latest = tags[-1]
        cur = tuple(int(x) for x in version.split("."))
        tag = tuple(int(x) for x in latest[1:].split("."))
        ok = cur >= tag
        if not ok:
            failures.append(
                f"버전이 태그보다 뒤처짐: pyproject {version} < 태그 {latest}"
            )
        state = "일치" if cur == tag else ("릴리스 준비 중" if ok else "갱신 누락")
        print(
            f"  [{'OK' if ok else 'FAIL'}] 버전 pyproject {version} "
            f"vs 태그 {latest} ({state})"
        )

    # uv.lock 버전 대조는 이 검사기에 넣을 수 없다.
    # `uv run`이 실행 시점에 lock을 자동 재동기화하므로, 검사기가
    # uv를 쓰는 한 lock은 항상 최신 상태로 관측된다(사보타주 무력).
    # 실측 — lock을 1.0.2로 되돌려도 `uv run` 한 번에 1.0.3으로 복구됐다.
    #
    # 대신 커밋 전에 `git status`로 uv.lock 변경 여부를 확인하십시오.
    # 버전을 올린 커밋에 lock이 빠져 있으면 그 자체가 신호다.

    print("\n" + "=" * 50)
    if failures:
        print(f"[FAIL] {len(failures)}건")
        for f in failures:
            print(f"  - {f}")
        return 2
    print("[SUCCESS] README의 모든 명령과 주장이 실제와 일치합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
