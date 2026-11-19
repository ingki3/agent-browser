#!/usr/bin/env python3
"""CI pytest 샤딩 도구(WS-96) — 표준 라이브러리만 쓴다.

사용:
  ci_shard.py --index I --total N             tests 아래 test_*.py 를 N 개로 나눈 것 중 I 번째(1부터) 출력
  ci_shard.py --update-durations J... [--out]  junit(xunit1) 에서 파일별 시간 합으로 시간표 갱신
  ci_shard.py --check --total N --lists L... [--collect C --junit J...]
                                               목록 서로소·합집합 == 발견 파일·각 목록 정렬,
                                               collect-only 파일별 노드 수 == junit 파일별 노드 수
  ci_shard.py --summarize LOG... [--out F]     각 shard pytest 로그의 마지막 요약 줄을 합산해 F 끝에 덧붙임
  ci_shard.py --report --junit J... [--out F]  느린 시험 상위 30·shard 별 시간·max/ideal (markdown)

시간표 갱신 절차: CI 집계 job(단위 테스트 (pytest))의 job summary 에 max/ideal 이 1.25 를 넘는다는 경고가 뜨면,
그 run 의 artifact `pytest-full-log` 안 `ci_test_durations.json`(6 shard junit 으로 만든 새 표)을
scripts/ci_test_durations.json 에 덮어써 PR 로 올린다. 로컬에서는 shard 별 junit 을 모아
`ci_shard.py --update-durations junit-*.xml` 로 같은 표를 만든다. 표는 git 의존 시험이 skip 되지 않는
checkout(fetch-depth 0, .git 있음)에서 잰 값이어야 한다 — .git 없는 사본 값은 그 시험들을 0 에 가깝게 잰다.

배정: 파일 단위 LPT(긴 것부터, 가장 가벼운 shard 로; 동률은 경로·shard 번호 순). 시간표에 없는 파일은
표 평균. 각 shard 출력은 **경로 정렬** — 섞인 순서는 pytest 가 conftest 격리를 조용히 빠뜨린다(리뷰 B5).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DURATIONS = ROOT / "scripts" / "ci_test_durations.json"
MAX_IDEAL_WARN = 1.25
SLOWEST = 30

# ── 발견·배정 ─────────────────────────────────────────────────────────


def discover(root: Path) -> list[str]:
    """root/tests 아래 test_*.py (root 기준 상대 경로, 정렬)."""
    base = Path(root)
    return sorted(
        p.relative_to(base).as_posix()
        for p in (base / "tests").rglob("test_*.py")
        if "__pycache__" not in p.parts
    )


def load_durations(path: Path) -> dict[str, float]:
    p = Path(path)
    if not p.is_file():
        return {}
    return {str(k): float(v) for k, v in json.loads(p.read_text(encoding="utf-8")).items()}


def expected_seconds(files: list[str], table: dict[str, float]) -> dict[str, float]:
    mean = sum(table.values()) / len(table) if table else 1.0
    return {f: float(table.get(f, mean)) for f in files}


def assign(files: list[str], table: dict[str, float], total: int) -> list[list[str]]:
    """LPT 배정. 반환 = shard 별 경로 정렬 목록(길이 total). 같은 입력 → 같은 출력."""
    if total < 1:
        raise ValueError("total 은 1 이상")
    secs = expected_seconds(sorted(set(files)), table)
    loads = [0.0] * total
    bins: list[list[str]] = [[] for _ in range(total)]
    for f in sorted(secs, key=lambda f: (-secs[f], f)):
        i = min(range(total), key=lambda k: (loads[k], k))
        loads[i] += secs[f]
        bins[i].append(f)
    return [sorted(b) for b in bins]


# ── junit ─────────────────────────────────────────────────────────────


def _case_file(classname: str, attr_file: str, files: set[str]) -> tuple[str, list[str]]:
    """classname(수집 모듈 기준)으로 파일을 찾는다. xunit1 의 file 속성은 함수 정의 위치라 다른 모듈일 수 있다."""
    parts = classname.split(".") if classname else []
    for i in range(len(parts), 0, -1):
        cand = "/".join(parts[:i]) + ".py"
        if cand in files:
            return cand, parts[i:]
    for i, part in enumerate(parts):  # 발견 목록 밖(다른 checkout 의 junit): 첫 test_* 부분이 모듈
        if part.startswith("test_"):
            return "/".join(parts[: i + 1]) + ".py", parts[i + 1:]
    return attr_file or classname, []


def junit_cases(texts: list[str], files: list[str]) -> list[dict]:
    """junit 문자열들 → testcase 목록 {file, nodeid, time, outcome, source}.

    같은 junit 안의 같은 nodeid 는 하나로 센다. 다른 junit(shard) 사이 중복은 그대로 두어 노드 수 대조에서 드러나게 한다.
    """
    fset = set(files)
    seen: set[tuple[int, str]] = set()
    out: list[dict] = []
    for src, text in enumerate(texts):
        for tc in ET.fromstring(text).iter("testcase"):
            f, rest = _case_file(tc.get("classname", ""), tc.get("file", ""), fset)
            nodeid = "::".join([f, *rest, tc.get("name", "")])
            if (src, nodeid) in seen:
                continue
            seen.add((src, nodeid))
            outcome = "passed"
            for child in tc:
                if child.tag in ("failure", "error"):
                    outcome = child.tag
                elif child.tag == "skipped" and outcome == "passed":
                    outcome = "skipped"
            out.append({"file": f, "nodeid": nodeid, "time": float(tc.get("time") or 0),
                        "outcome": outcome, "source": src})
    return out


def count_per_file(cases: list[dict]) -> dict[str, int]:
    d: dict[str, int] = defaultdict(int)
    for c in cases:
        d[c["file"]] += 1
    return dict(d)


def durations_from_junit(texts: list[str], files: list[str]) -> dict[str, float]:
    d: dict[str, float] = defaultdict(float)
    for c in junit_cases(texts, files):
        d[c["file"]] += c["time"]
    return {k: round(v, 1) for k, v in sorted(d.items())}


def write_durations(path: Path, table: dict[str, float]) -> None:
    data = {k: round(float(v), 1) for k, v in sorted(table.items())}
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


# ── 점검 ──────────────────────────────────────────────────────────────


def check_lists(files: list[str], shards: list[list[str]]) -> list[str]:
    """문제 목록(빈 리스트 = 통과): 빈 shard·비정렬·중복·누락·발견 안 된 파일."""
    probs: list[str] = []
    seen: dict[str, int] = {}
    for i, shard in enumerate(shards, 1):
        if not shard:
            probs.append(f"shard {i}: 빈 목록")
        if shard != sorted(shard):
            probs.append(f"shard {i}: 경로 정렬 아님")
        for f in shard:
            if f in seen:
                probs.append(f"중복: {f} (shard {seen[f]}, {i})")
            else:
                seen[f] = i
    known = set(files)
    for f in sorted(known - set(seen)):
        probs.append(f"누락: {f}")
    for f in sorted(set(seen) - known):
        probs.append(f"발견 목록에 없는 파일: {f}")
    return probs


def collect_counts(text: str) -> dict[str, int]:
    """`pytest --collect-only -q` 출력 → 파일별 노드 수."""
    d: dict[str, int] = defaultdict(int)
    for line in text.splitlines():
        line = line.strip()
        if "::" in line and not line.startswith(("=", "<")):
            d[line.split("::", 1)[0]] += 1
    return dict(d)


def check_counts(collected: dict[str, int], ran: dict[str, int]) -> list[str]:
    probs = []
    for f in sorted(set(collected) | set(ran)):
        a, b = collected.get(f, 0), ran.get(f, 0)
        if a != b:
            probs.append(f"노드 수 불일치: {f} collect={a} junit={b}")
    return probs


# ── 요약 합산 ─────────────────────────────────────────────────────────

_SUMMARY = re.compile(r"^=*\s*(?P<body>(?:\d+ \w+(?:, )?)+) in (?P<secs>[\d.]+)s", re.M)
_ORDER = ["failed", "passed", "skipped", "deselected", "xfailed", "xpassed", "warnings", "error"]
_ALIAS = {"warning": "warnings", "errors": "error"}
_SINGULAR = {"warnings": "warning"}
_PLURAL = {"error": "errors"}


def last_summary(text: str) -> tuple[dict[str, int], float] | None:
    found = list(_SUMMARY.finditer(text))
    if not found:
        return None
    m = found[-1]
    counts: dict[str, int] = defaultdict(int)
    for n, word in re.findall(r"(\d+) (\w+)", m.group("body")):
        counts[_ALIAS.get(word, word)] += int(n)
    return dict(counts), float(m.group("secs"))


def combine_summaries(texts: list[str]) -> tuple[str, bool]:
    """(합산 요약 줄, 성공 여부). 0 인 범주는 쓰지 않는다. 요약 없는 로그·failed·error 는 실패."""
    total: dict[str, int] = defaultdict(int)
    secs = 0.0
    ok = True
    for t in texts:
        got = last_summary(t)
        if got is None:
            ok = False
            continue
        counts, s = got
        secs += s
        for k, v in counts.items():
            total[k] += v
    if total.get("failed") or total.get("error"):
        ok = False
    keys = [k for k in _ORDER if total.get(k)] + sorted(k for k in total if k not in _ORDER and total[k])
    parts = []
    for k in keys:
        n = total[k]
        word = _SINGULAR.get(k, k) if n == 1 else _PLURAL.get(k, k)
        parts.append(f"{n} {word}")
    if not parts:
        ok = False
        parts = ["no tests ran"]
    return f"{', '.join(parts)} in {secs:.2f}s", ok


# ── 보고 ──────────────────────────────────────────────────────────────


def report(texts: list[str], files: list[str], table: dict[str, float]) -> tuple[str, float]:
    """(markdown, max/ideal). shard 시간 = 그 junit 의 testcase time 합."""
    cases = junit_cases(texts, files)
    loads = [0.0] * len(texts)
    for c in cases:
        loads[c["source"]] += c["time"]
    ideal = sum(loads) / len(loads) if loads else 0.0
    ratio = max(loads) / ideal if ideal else 0.0
    lines = ["### shard 별 시험 시간", "", "| shard | 초 |", "|---|---|"]
    lines += [f"| {i} | {t:.1f} |" for i, t in enumerate(loads, 1)]
    lines += ["", f"max/ideal = {ratio:.2f} (max {max(loads, default=0):.1f}s, ideal {ideal:.1f}s)", ""]
    if ratio > MAX_IDEAL_WARN:
        lines += [f"⚠️ max/ideal > {MAX_IDEAL_WARN} — 시간표(scripts/ci_test_durations.json) 갱신 필요", ""]
    lines += [f"### 느린 시험 상위 {SLOWEST}", "", "| 초 | 시험 |", "|---|---|"]
    for c in sorted(cases, key=lambda c: (-c["time"], c["nodeid"]))[:SLOWEST]:
        lines.append(f"| {c['time']:.2f} | `{c['nodeid']}` |")
    missing = [f for f in files if f not in table]
    lines += ["", f"### 시간표에 없는 파일 {len(missing)}개", ""]
    lines += [f"- `{f}`" for f in missing]
    return "\n".join(lines) + "\n", ratio


# ── CLI ───────────────────────────────────────────────────────────────


def _read(paths: list[str]) -> list[str]:
    return [Path(p).read_text(encoding="utf-8", errors="replace") for p in paths]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--durations", default=None, help="시간표 JSON (기본 scripts/ci_test_durations.json)")
    ap.add_argument("--index", type=int)
    ap.add_argument("--total", type=int)
    ap.add_argument("--update-durations", nargs="+", metavar="JUNIT")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--lists", nargs="+")
    ap.add_argument("--collect")
    ap.add_argument("--junit", nargs="+")
    ap.add_argument("--summarize", nargs="+", metavar="LOG")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    root = Path(a.root)
    dur_path = Path(a.durations) if a.durations else root / "scripts" / "ci_test_durations.json"
    files = discover(root)

    if a.update_durations:
        table = durations_from_junit(_read(a.update_durations), files)
        out = Path(a.out) if a.out else dur_path
        write_durations(out, table)
        print(f"시간표 {len(table)}개 파일 → {out}")
        return 0

    if a.summarize:
        line, ok = combine_summaries(_read(a.summarize))
        if a.out:
            with open(a.out, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        print(line)
        if not ok:
            print("실패: shard 요약에 failed/error 가 있거나 요약 줄이 없는 로그가 있음", file=sys.stderr)
        return 0 if ok else 1

    if a.report:
        md, ratio = report(_read(a.junit or []), files, load_durations(dur_path))
        if a.out:
            Path(a.out).write_text(md, encoding="utf-8")
        print(md)
        if ratio > MAX_IDEAL_WARN:
            print(f"::warning::shard max/ideal {ratio:.2f} > {MAX_IDEAL_WARN} — 시간표 갱신 필요")
        return 0

    if a.check:
        probs: list[str] = []
        lists = [[ln.strip() for ln in t.splitlines() if ln.strip()] for t in _read(a.lists or [])]
        if a.total is not None and len(lists) != a.total:
            probs.append(f"목록 {len(lists)}개 != total {a.total}")
        probs += check_lists(files, lists)
        if a.collect or a.junit:
            if not (a.collect and a.junit):
                probs.append("--collect 와 --junit 은 함께 줘야 함")
            else:
                collected = collect_counts(Path(a.collect).read_text(encoding="utf-8", errors="replace"))
                ran = count_per_file(junit_cases(_read(a.junit), files))
                probs += check_counts(collected, ran)
                print(f"collect {sum(collected.values())} 노드 / junit {sum(ran.values())} 노드, "
                      f"파일 {len(collected)} / {len(ran)}")
        for p in probs:
            print(f"::error::{p}")
        print(f"점검: 발견 파일 {len(files)}개, 목록 {len(lists)}개, 문제 {len(probs)}건")
        return 1 if probs else 0

    if a.index is None or a.total is None:
        ap.error("--index 와 --total 이 필요")
    if not 1 <= a.index <= a.total:
        ap.error("--index 는 1..total")
    shard = assign(files, load_durations(dur_path), a.total)[a.index - 1]
    if not shard:
        print(f"shard {a.index}/{a.total}: 빈 목록 — pytest 가 testpaths 전체를 돌게 되므로 실패", file=sys.stderr)
        return 1
    sys.stdout.write("".join(f + "\n" for f in shard))
    return 0


if __name__ == "__main__":
    sys.exit(main())
