"""WS-96 — CI pytest 샤딩(scripts/ci_shard.py)과 ci.yml 배선.

리뷰(ws96-evidence/review.md) B1~B6·NN1·NN4 를 단정한다:
- 배정은 결정적·서로소·전체 포함, 각 shard 출력은 **경로 정렬**(B5 — 섞인 순서는 conftest 격리를 조용히 잃음).
- 빈 목록은 exit 1(pytest 에 빈 목록을 주면 testpaths 전체가 돈다).
- 요약 합산 줄은 0 범주를 생략하고 failed 가 있으면 실패로 표기(check_readme 가 마지막 줄을 읽음).
- 집계 job 이름 = 필수 검사 이름, `!cancelled()` + shard 결과 명시 검사(B1), fail-fast false(B2),
  artifact overwrite(B4), release-gate 에 pytest 재실행 없음.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
_PATH = ROOT / "scripts" / "ci_shard.py"
_spec = importlib.util.spec_from_file_location("ci_shard_under_test", _PATH)
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)  # type: ignore[union-attr]

CI = ROOT / ".github" / "workflows" / "ci.yml"


# ── 배정 ──────────────────────────────────────────────────────────────

def _files(n: int) -> list[str]:
    return [f"tests/d{i % 3}/test_f{i:02d}.py" for i in range(n)]


def test_assign_is_deterministic_disjoint_and_complete():
    files = _files(40)
    table = {f: float((i * 37) % 23 + 1) for i, f in enumerate(files)}
    a = cs.assign(files, table, 6)
    b = cs.assign(list(reversed(files)), dict(reversed(list(table.items()))), 6)
    assert a == b  # 입력 순서와 무관
    flat = [f for shard in a for f in shard]
    assert len(flat) == len(set(flat)) == len(files)
    assert set(flat) == set(files)
    assert len(a) == 6 and all(a)


def test_each_shard_is_path_sorted():
    files = _files(40)
    # 경로가 뒤일수록 길게 — LPT 배정 순서(긴 것부터)를 그대로 내면 정렬이 깨진다
    table = {f: float(1 + i) for i, f in enumerate(sorted(files))}
    for shard in cs.assign(files, table, 6):
        assert shard == sorted(shard)


def test_lpt_balances_and_ties_break_by_path():
    files = ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py", "tests/test_d.py"]
    table = {"tests/test_a.py": 10, "tests/test_b.py": 10, "tests/test_c.py": 10, "tests/test_d.py": 10}
    assert cs.assign(files, table, 2) == [
        ["tests/test_a.py", "tests/test_c.py"],
        ["tests/test_b.py", "tests/test_d.py"],
    ]
    table = {"tests/test_a.py": 9, "tests/test_b.py": 5, "tests/test_c.py": 4, "tests/test_d.py": 1}
    loads = [sum(table[f] for f in s) for s in cs.assign(files, table, 2)]
    assert sorted(loads) == [9, 10]


def test_new_file_gets_table_mean():
    files = ["tests/test_a.py", "tests/test_b.py", "tests/test_new.py"]
    table = {"tests/test_a.py": 30.0, "tests/test_b.py": 10.0, "tests/test_gone.py": 20.0}
    assert cs.expected_seconds(files, table)["tests/test_new.py"] == pytest.approx(20.0)
    # 평균(20) 이 b(10) 보다 커서 b 와 같은 shard 로 — a(30) 혼자
    assert cs.assign(files, table, 2) == [["tests/test_a.py"], ["tests/test_b.py", "tests/test_new.py"]]


def test_discover_finds_test_files_sorted(tmp_path):
    (tmp_path / "tests" / "sub").mkdir(parents=True)
    for rel in ("tests/test_z.py", "tests/sub/test_a.py", "tests/conftest.py", "tests/helper.py"):
        (tmp_path / rel).write_text("", encoding="utf-8")
    assert cs.discover(tmp_path) == ["tests/sub/test_a.py", "tests/test_z.py"]


def test_cli_prints_shard_and_empty_shard_exits_1(tmp_path):
    (tmp_path / "tests").mkdir()
    for name in ("test_a.py", "test_b.py"):
        (tmp_path / "tests" / name).write_text("", encoding="utf-8")
    table = tmp_path / "durations.json"
    table.write_text(json.dumps({"tests/test_a.py": 5, "tests/test_b.py": 3}), encoding="utf-8")
    base = [sys.executable, str(_PATH), "--root", str(tmp_path), "--durations", str(table)]
    out = subprocess.run([*base, "--index", "1", "--total", "2"], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.split() == ["tests/test_a.py"]
    empty = subprocess.run([*base, "--index", "3", "--total", "3"], capture_output=True, text=True)
    assert empty.returncode == 1 and empty.stdout.strip() == ""
    bad = subprocess.run([*base, "--index", "0", "--total", "2"], capture_output=True, text=True)
    assert bad.returncode != 0


def test_repo_table_covers_repo_and_every_shard_nonempty():
    files = cs.discover(ROOT)
    table = cs.load_durations(ROOT / "scripts" / "ci_test_durations.json")
    assert list(table) == sorted(table) and len(table) >= 300
    # 표에 없는 새 파일은 평균으로 배정된다(표 갱신은 --update-durations) — 대부분은 표에 있어야 한다
    assert len(set(files) - set(table)) < 20, sorted(set(files) - set(table))
    shards = cs.assign(files, table, 6)
    assert all(shards) and cs.check_lists(files, shards) == []


# ── 시간표 갱신 ────────────────────────────────────────────────────────

JUNIT = """<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" tests="4">
<testcase classname="tests.test_a" name="t1" file="tests/test_a.py" time="1.20"/>
<testcase classname="tests.test_a.TestK" name="t2" file="tests/test_a.py" time="2.00"><skipped type="pytest.skip" message="x"/></testcase>
<testcase classname="tests.sub.test_b" name="t[a::b]" file="tests/helpers.py" time="0.30"/>
<testcase classname="tests.sub.test_b" name="t3" file="tests/sub/test_b.py" time="0.04"><failure message="boom"/></testcase>
</testsuite></testsuites>
"""


def test_update_durations_sums_per_file(tmp_path):
    j = tmp_path / "s1.xml"
    j.write_text(JUNIT, encoding="utf-8")
    out = tmp_path / "new.json"
    rc = cs.main(["--update-durations", str(j), "--out", str(out)])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    data = json.loads(text)
    # 다른 모듈에 정의된(file=helpers.py) 시험도 classname 으로 수집 파일에 붙인다
    assert data == {"tests/sub/test_b.py": 0.3, "tests/test_a.py": 3.2}
    assert list(data) == sorted(data)


def test_junit_counts_per_file():
    files = ["tests/test_a.py", "tests/sub/test_b.py"]
    cases = cs.junit_cases([JUNIT], files)
    assert cs.count_per_file(cases) == {"tests/test_a.py": 2, "tests/sub/test_b.py": 2}


def test_junit_file_from_classname_prefers_discovered_files():
    # 디렉터리 이름이 test_ 로 시작해도 발견 목록의 실제 파일로 붙인다
    xml = ('<testsuites><testsuite><testcase classname="tests.test_pkg.test_x.TestK" name="t" '
           'file="tests/test_pkg/helpers.py" time="1"/></testsuite></testsuites>')
    [case] = cs.junit_cases([xml], ["tests/test_pkg/test_x.py"])
    assert case["file"] == "tests/test_pkg/test_x.py"
    assert case["nodeid"] == "tests/test_pkg/test_x.py::TestK::t"


# ── 요약 합산 ──────────────────────────────────────────────────────────

def test_combine_summaries_omits_zero_categories():
    logs = [
        "....\n3000 passed, 2 skipped, 5 warnings in 500.50s (0:08:20)\n",
        "== 3000 passed, 6 xfailed in 400.25s ==\n",
        "slowest durations ...\n3333 passed, 6 skipped in 600.00s\n",
    ]
    line, ok = cs.combine_summaries(logs)
    assert ok
    assert line == "9333 passed, 8 skipped, 6 xfailed, 5 warnings in 1500.75s"
    assert "0 " not in line


def test_combine_summaries_marks_failures():
    line, ok = cs.combine_summaries(["10 passed in 1.00s\n", "1 failed, 4 passed, 1 error in 2.00s\n"])
    assert not ok
    assert line == "1 failed, 14 passed, 1 error in 3.00s"


def test_combine_summaries_uses_last_summary_and_requires_one():
    line, ok = cs.combine_summaries(["1 failed in 1.0s\n...\n9 passed in 2.00s\n"])
    assert ok and line == "9 passed in 2.00s"
    _line, ok = cs.combine_summaries(["9 passed in 1.00s\n", "....F...\n"])
    assert not ok


def test_summarize_cli_appends_line_check_readme_reads(tmp_path):
    a, b = tmp_path / "a.log", tmp_path / "b.log"
    a.write_text("x\n5 passed, 1 skipped in 1.00s\n", encoding="utf-8")
    b.write_text("y\n4 passed, 1 xfailed in 2.50s\n", encoding="utf-8")
    full = tmp_path / "pytest-full.log"
    assert cs.main(["--summarize", str(a), str(b), "--out", str(full)]) == 0
    text = full.read_text(encoding="utf-8")
    assert text.rstrip("\n").splitlines()[-1] == "9 passed, 1 skipped, 1 xfailed in 3.50s"
    spec = importlib.util.spec_from_file_location("cr_ws96", ROOT / "scripts" / "check_readme.py")
    cr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cr)  # type: ignore[union-attr]
    assert cr.passed_from_log(text) == 9
    b.write_text("y\n1 failed, 3 passed in 2.50s\n", encoding="utf-8")
    assert cs.main(["--summarize", str(a), str(b), "--out", str(full)]) == 1
    assert cr.passed_from_log(full.read_text(encoding="utf-8")) == -1


# ── --check ───────────────────────────────────────────────────────────

def test_check_lists_catches_duplicate_missing_unsorted_unknown():
    files = ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py"]
    assert cs.check_lists(files, [["tests/test_a.py", "tests/test_c.py"], ["tests/test_b.py"]]) == []
    dup = cs.check_lists(files, [["tests/test_a.py", "tests/test_b.py"], ["tests/test_b.py", "tests/test_c.py"]])
    assert any("중복" in p and "tests/test_b.py" in p for p in dup)
    miss = cs.check_lists(files, [["tests/test_a.py"], ["tests/test_b.py"]])
    assert any("누락" in p and "tests/test_c.py" in p for p in miss)
    unsorted = cs.check_lists(files, [["tests/test_c.py", "tests/test_a.py"], ["tests/test_b.py"]])
    assert any("정렬" in p for p in unsorted)
    extra = cs.check_lists(files, [["tests/test_a.py", "tests/test_x.py"], ["tests/test_b.py", "tests/test_c.py"]])
    assert any("tests/test_x.py" in p for p in extra)
    empty = cs.check_lists(files, [["tests/test_a.py", "tests/test_b.py", "tests/test_c.py"], []])
    assert any("빈" in p for p in empty)


def test_check_node_counts_catches_mismatch():
    collected = {"tests/test_a.py": 2, "tests/sub/test_b.py": 2}
    assert cs.check_counts(collected, {"tests/test_a.py": 2, "tests/sub/test_b.py": 2}) == []
    probs = cs.check_counts(collected, {"tests/test_a.py": 2, "tests/sub/test_b.py": 1})
    assert any("tests/sub/test_b.py" in p for p in probs)
    assert cs.check_counts(collected, {"tests/test_a.py": 2})  # 파일 통째 누락


def test_collect_counts_parses_collect_only():
    text = (
        "tests/test_a.py::test_x\ntests/test_a.py::T::test_y[p::q]\ntests/sub/test_b.py::test_z\n"
        "\n3 tests collected in 0.10s\n"
    )
    assert cs.collect_counts(text) == {"tests/test_a.py": 2, "tests/sub/test_b.py": 1}


def test_check_cli_end_to_end(tmp_path):
    files = ["tests/test_a.py", "tests/sub/test_b.py"]
    l1, l2 = tmp_path / "files-1.txt", tmp_path / "files-2.txt"
    l1.write_text("tests/test_a.py\n", encoding="utf-8")
    l2.write_text("tests/sub/test_b.py\n", encoding="utf-8")
    col = tmp_path / "collect.txt"
    col.write_text(
        "tests/test_a.py::t1\ntests/test_a.py::TestK::t2\ntests/sub/test_b.py::t[a::b]\n"
        "tests/sub/test_b.py::t3\n\n4 tests collected in 0.1s\n",
        encoding="utf-8",
    )
    j = tmp_path / "s.xml"
    j.write_text(JUNIT, encoding="utf-8")
    for f in files:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("", encoding="utf-8")
    args = ["--check", "--root", str(tmp_path), "--total", "2", "--lists", str(l1), str(l2),
            "--collect", str(col), "--junit", str(j)]
    assert cs.main(args) == 0
    l2.write_text("tests/sub/test_b.py\ntests/test_a.py\n", encoding="utf-8")
    assert cs.main(args) == 1  # 중복·비정렬
    l2.write_text("tests/sub/test_b.py\n", encoding="utf-8")
    assert cs.main([*args[:6], str(l1), "--collect", str(col), "--junit", str(j)]) == 1  # 목록 수 != total


def test_report_has_slowest_and_ratio():
    files = ["tests/test_a.py", "tests/sub/test_b.py"]
    md, ratio = cs.report([JUNIT, JUNIT.replace('time="2.00"', 'time="0.5"')], files, {"tests/test_a.py": 1.0})
    assert "max/ideal" in md and "tests/test_a.py::TestK::t2" in md
    assert "tests/sub/test_b.py" in md  # 표에 없는 파일
    assert ratio == pytest.approx(3.54 / ((3.54 + 2.04) / 2), rel=1e-3)


# ── ci.yml 배선 ───────────────────────────────────────────────────────

def _ci() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def _shard_job(jobs: dict) -> tuple[str, dict]:
    [(name, job)] = [(k, v) for k, v in jobs.items() if "matrix" in v.get("strategy", {})]
    return name, job


def _run_text(job: dict) -> str:
    return "\n".join(str(s.get("run", "")) for s in job["steps"])


def test_ci_shard_matrix():
    jobs = _ci()["jobs"]
    name, job = _shard_job(jobs)
    strat = job["strategy"]
    assert strat["fail-fast"] is False
    assert strat["matrix"]["shard"] == [1, 2, 3, 4, 5, 6]
    assert job["name"] == "단위 테스트 샤드 ${{ matrix.shard }}/6"
    assert job["timeout-minutes"] == 40
    steps = job["steps"]
    assert steps[0]["uses"].startswith("actions/checkout") and steps[0]["with"]["fetch-depth"] == 0
    run = _run_text(job)
    assert "uv python install 3.11" in run and "playwright install --with-deps chromium" in run
    assert "Xvfb :99" in run
    assert "ci_shard.py --index ${{ matrix.shard }} --total 6" in run
    assert "--durations=25" in run and "junit_family=xunit1" in run and "@" in run
    assert "${PIPESTATUS[0]}" in run
    [up] = [s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact")]
    assert up["if"] == "always()" and up["with"]["overwrite"] is True
    assert "${{ matrix.shard }}" in up["with"]["name"]


def test_ci_aggregate_job_is_required_check():
    jobs = _ci()["jobs"]
    shard_id, _ = _shard_job(jobs)
    [(agg_id, agg)] = [(k, v) for k, v in jobs.items() if v.get("name") == "단위 테스트 (pytest)"]
    assert agg["needs"] == [shard_id] or agg["needs"] == shard_id
    assert agg["if"].replace(" ", "") == "${{!cancelled()}}"
    first = agg["steps"][0]
    assert f"needs.{shard_id}.result" in first["run"] and "success" in first["run"] and "exit 1" in first["run"]
    run = _run_text(agg)
    assert "ci_shard.py --check" in run and "--collect-only -q tests" in run
    assert "ci_shard.py --summarize" in run and "GITHUB_STEP_SUMMARY" in run
    downloads = [s for s in agg["steps"] if str(s.get("uses", "")).startswith("actions/download-artifact")]
    assert downloads
    uploads = [s for s in agg["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact")]
    assert "pytest-full/pytest-full.log" in run
    assert any(str(s["with"]["path"]).rstrip("/").endswith("/pytest-full") and s["with"].get("overwrite") is True
               for s in uploads)
    # 다른 job 은 같은 이름을 쓰지 않는다
    assert [v["name"] for v in jobs.values()].count("단위 테스트 (pytest)") == 1


def test_ci_release_gate_reuses_aggregate_log():
    jobs = _ci()["jobs"]
    [(agg_id, _)] = [(k, v) for k, v in jobs.items() if v.get("name") == "단위 테스트 (pytest)"]
    gate = jobs["release-gate"]
    assert sorted(gate["needs"]) == sorted(["integrity", agg_id])
    for s in gate["steps"]:
        assert "pytest tests -q" not in str(s.get("run", "")), s
        assert s.get("name") != "전체 회귀 테스트"
    names = [s.get("name") for s in gate["steps"]]
    for g in ("Gate 0 — 계약 동결 검증", "Gate 1 — 하네스 자체 검증", "Gate 2 — 인지 엔진 실측",
              "Gate 3-B — MVP v1.0 릴리스 게이트", "README 설치 안내 검증"):
        assert g in names


def test_ci_triggers_and_concurrency_unchanged():
    ci = _ci()
    on = ci.get("on", ci.get(True))  # yaml 1.1: 'on' → True
    assert on == {"pull_request": {"branches": ["dev", "main"]}, "push": {"branches": ["dev", "main"]}}
    assert ci["concurrency"] == {"group": "ci-${{ github.workflow }}-${{ github.ref }}", "cancel-in-progress": True}
    assert ci["jobs"]["integrity"]["name"] == "문서·스크립트 무결성 (check_docs)"
