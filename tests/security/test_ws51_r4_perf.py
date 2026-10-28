"""WS-51 R4 — 성능 행렬(계획 §1.5·§3 R3-B6·B5). 측정 조건 고정: 같은 Mac, 새 프로세스, 5회 최솟값, 매회 캐시를
거치지 않는 본체(`_public_text`·주소 칸 본체), 2배 비율(≤ 2.5)은 256KiB 이상 구간에서만.

WS-66(#149): 5회 중앙값 → 5회 **최솟값**, 크기를 돌아가며(라운드마다 모든 크기·종류) 잰다. 기준(1.0초·2.5배·바닥 5ms)은
그대로다(규칙 6 비해당) — 최솟값은 부하가 가장 적은 순간의 실제 처리 시간이라 전체 실행 중 순간 부하(WS-64 R1 검증
short256_alnum_dense·short256_other_dense, 단독 통과)만 걸러내고, 이차시간 회귀는 다섯 번 모두 넘어 잡힌다. 반복이 첫 측정의
1/PERF_SAME_WORK 미만이면(결과를 기억해 같은 일을 다시 하지 않았다) 실패시킨다.

WS-70(#180): v1.17.0 릴리스 게이트(Linux CI)에서 long_dense_prefix url 512KiB 0.0512초 → 1MiB 0.181초(3.55배)로 실패했다.
원인은 제품이 아니라 러너 CPU 경합이다 — 로컬 실측(10회)에서 그 경로는 256KiB~2MiB 모두 2배 입력에 1.97~2.03배로 선형이고,
코어보다 바쁜 프로세스가 많을 때(24·48개/12코어)만 큰 크기의 다섯 번이 모두 선점돼 최솟값끼리의 비율이 4~6배로 튄다(긴
측정일수록 한 번도 선점되지 않을 확률이 낮다). 그래서 판정 하나가 어긋나면(비율·1초 상한·같은 일) **새 격리 자식**에서 원래와 같은 크기 집합·라운드 순서(라운드마다
모든 크기·종류)로 PERF_RECHECK_ROUNDS 번 다시 재고, 그 결과에 모든 판정을 다시 적용해 하나라도 어긋나면 실패시킨다(R1:
원래 판정만 다시 보거나 크기를 줄여 재면, 크기가 바뀔 때마다 생기는 지연처럼 측정 순서에 따라 모양이 바뀌는 회귀를 놓친다).
이차시간·상수 지연·결과 캐시·크기 전환 지연은 다시 재도 그대로라 잡히고(사보타주 표는 .hermes/state/ws70/), 한 자식
동안의 부하 덩어리만 걸러진다.
기준(1.0초·2.5배·바닥 5ms·PERF_SAME_WORK)은 그대로다(규칙 6 비해당). 자식 격리는 r1·r2 의 `_run_perf`(WS-66 #173)와 같다.

실 observe+wire 경로는 tests/interface/test_ws51_r4_e2e.py. 값은 합성 값.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import string
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

MiB = 1 << 20
SIZES = [256 << 10, 512 << 10, 1 << 20]
PERF_ROUNDS = 5
PERF_SAME_WORK = 20
PERF_BUDGET_S = 1.0
PERF_GROWTH = 2.5
PERF_FLOOR_S = 0.005
#: WS-70 — 위반 하나를 새 격리 자식에서 다시 잴 때의 라운드 수(원래와 같은 모든 크기·종류를 돌아가며)
PERF_RECHECK_ROUNDS = 9
#: 자식마다 새 빈 임시 디렉터리로 돌리는 환경 변수(r1 PERF_ISOLATED_ENV 와 같다)
PERF_ISOLATED_ENV = ("HOME", "TMPDIR", "TEMP", "TMP", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
                     "PYTHONPYCACHEPREFIX")
PERF_CODE_COPY_IGNORE = ("__pycache__", "*.pyc")


def _rand(n: int, seed: int) -> str:
    rnd = random.Random(seed)
    return "".join(rnd.choice(string.ascii_letters + string.digits) for _ in range(n))


def _values(case: str) -> list:
    if case == "dense_a_8_15":
        return ["a" * n for n in range(8, 16)]
    if case == "periodic256":
        return [("ab" * 200)[:n] for n in range(8, 264)]
    if case in ("short256_alnum_dense", "short256_alnum_boundary_fail"):
        return ["k%03d" % i for i in range(256)]
    if case in ("short256_other_dense", "short256_other_boundary_fail"):
        return ["k-%03d" % i for i in range(256)]
    if case == "long_dense_prefix":
        return ["z" * 65536 + "SYNTHEND8"]
    if case == "long_random_truncated":
        return [_rand(65545, 7)]
    if case == "long16_random_scan":  # 긴 값 16개(블록 8,192개) — 일치가 드문 본문의 위치별 블록 조회 비용
        return [_rand(65545, 100 + i) for i in range(16)]
    if case == "special256":
        return ['SYNTH%03d_\u00e9<&"\\+ /9' % i for i in range(256)]
    if case == "random256_words":
        return [_rand(20, i) for i in range(256)]
    raise KeyError(case)


def _text(case: str, n: int, values: list) -> str:
    rnd = random.Random(n)
    if case == "dense_a_8_15":
        return "a" * n
    if case == "periodic256":
        return ("ab" * (n // 2 + 1))[:n]
    if case.endswith("_dense"):
        return " ".join(rnd.choice(values) for _ in range(n // (len(values[0]) + 1) + 1))[:n]
    if case.endswith("_boundary_fail"):  # 앞뒤가 영숫자라 경계 실패가 조밀 + 끝에 경계 일치 하나
        tail = " " + values[2]
        return (values[1] * (n // len(values[1]) + 1))[:n - len(tail)] + tail
    if case == "long_dense_prefix":
        return "z" * n
    if case == "long_random_truncated":
        v = values[0]
        return ((v[:40000] + "\u2026 " + v[1000:9000] + " x ") * (n // 48000 + 1))[:n]
    if case == "long16_random_scan":
        tail = " " + values[3][5000:5300]
        return ((" ".join(rnd.choice(["lorem", "ipsum", "x?y=1"]) for _ in range(n // 5)))[:n - len(tail)] + tail)
    if case == "special256":
        return (" ".join(rnd.choice(["lorem", "ipsum", "x?y=1", values[5], values[200]]) for _ in range(n // 6)))[:n]
    if case == "random256_words":
        return (" ".join(rnd.choice(["alpha", "beta", "gamma", "x?y=1", values[3]]) for _ in range(n // 5)))[:n]
    raise KeyError(case)


CASES = ["dense_a_8_15", "periodic256", "short256_alnum_dense", "short256_alnum_boundary_fail",
         "short256_other_dense", "short256_other_boundary_fail", "long_dense_prefix", "long_random_truncated", "long16_random_scan",
         "special256", "random256_words"]


def _child(case: str, rounds: int, sizes: list, kinds: list) -> None:
    from security import secret_registry
    from security.public_url import _public_text, _public_url_field_uncached

    values = _values(case)
    secret_registry.REGISTRY.add_values(values)
    rows = {"values": len(secret_registry.REGISTRY), "chars": secret_registry.REGISTRY.chars, "text": {}, "url": {},
            "runs": {"text": {}, "url": {}}}
    fns = {"text": _public_text, "url": lambda t: _public_url_field_uncached("/p?" + t)}
    texts = {n: _text(case, n, values) for n in sizes}
    runs = {(kind, n): [] for kind in kinds for n in sizes}
    for _ in range(rounds):
        for n in sizes:
            for kind in kinds:
                t = time.perf_counter()
                out = fns[kind](texts[n])
                runs[kind, n].append(time.perf_counter() - t)
                if kind == "text" and n == SIZES[-1]:
                    rows["masked"] = out.count("[REDACTED]") > 0
    for (kind, n), got in runs.items():
        rows[kind][n] = min(got)
        rows["runs"][kind][n] = got
    # 조건 성립(r1 과 같다): 홈·임시·캐시 경로가 모두 이 자식의 빈 작업 디렉터리이고, 제품 모듈을 부모가 만든 사본에서 읽었다
    here = os.path.realpath(os.getcwd())
    rows["isolated"] = all(os.path.realpath(os.environ.get(k, "")) == here for k in PERF_ISOLATED_ENV)
    rows["module"] = os.path.realpath(sys.modules["security.public_url"].__file__)
    origin = Path(__file__).resolve().parents[2] / "src"
    loaded = [os.path.realpath(f) for m in list(sys.modules.values()) if isinstance(f := getattr(m, "__file__", None), str)]
    rows["from_origin"] = [f for f in loaded if Path(f).is_relative_to(origin)]
    print(json.dumps(rows))


def _run(case: str, rounds: int = PERF_ROUNDS, sizes: tuple = tuple(SIZES), kinds: tuple = ("text", "url")) -> dict:
    """격리 자식 한 번의 측정. 자식마다 빈 홈·임시 디렉터리와 제품 소스 사본을 새로 만든다(r1 `_run_perf` 와 같다)."""
    env = dict(os.environ)
    src = Path(__file__).resolve().parents[2] / "src"
    rest = [os.path.abspath(p) for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    root = os.path.realpath(tempfile.mkdtemp(prefix="ws70-perf-"))
    box = os.path.join(root, "box")  # 빈 작업 디렉터리 = 홈·임시·캐시
    code = os.path.join(root, "code", "src")  # 제품 소스 사본 — 저장소 고정 경로 캐시도 사본마다 새로
    args = [case, str(rounds), ",".join(map(str, sizes)), ",".join(kinds)]
    try:
        os.mkdir(box)
        shutil.copytree(src, code, ignore=shutil.ignore_patterns(*PERF_CODE_COPY_IGNORE))
        env["PYTHONPATH"] = os.pathsep.join([code, *rest])
        env.update(dict.fromkeys(PERF_ISOLATED_ENV, box))
        try:
            proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), *args], capture_output=True, text=True,
                                  timeout=300, env=env, cwd=box)
        except subprocess.TimeoutExpired:
            pytest.fail(f"{case} {args}: 300초 안에 끝나지 않음")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    assert proc.returncode == 0, proc.stderr[-2000:]
    rows = json.loads(proc.stdout.strip().splitlines()[-1])
    assert rows["isolated"], (case, "자식 환경 격리 실패", rows)
    assert Path(rows["module"]).is_relative_to(code) and not rows["from_origin"], (case, "제품 소스 사본 미사용", code, rows)
    return rows


def _violations(rows: dict, kind: str, sizes: tuple) -> list:
    """한 측정의 판정 위반 목록 — 각 항목은 (판정, 다시 잴 크기들). 기준은 WS-51 그대로."""
    times = {int(k): v for k, v in rows[kind].items()}
    found = []
    for n in sizes:  # 반복도 첫 측정과 같은 일을 했다(캐시 적중이면 수십분의 일)
        got = rows["runs"][kind][str(n)]
        if min(got[1:]) * PERF_SAME_WORK < got[0]:
            found.append((("same_work", n), (n,)))
    if MiB in times and times[MiB] >= PERF_BUDGET_S:
        found.append((("budget", MiB), (MiB,)))
    for small, big in zip(sizes, sizes[1:]):
        if times[big] > PERF_GROWTH * max(times[small], PERF_FLOOR_S):
            found.append((("ratio", small, big), (small, big)))
    return found


def _all_violations(case: str, rows: dict) -> list:
    """한 측정의 모든 판정(조건 성립·1초 상한·2.5배 비율·같은 일 1/20)을 두 종류 모두에 적용한 위반 목록."""
    found = []
    if rows["values"] != len(_values(case)):  # 조건 성립: 모든 값을 기억했다(밀려남 없음)
        found.append(("values", rows["values"]))
    if not rows.get("masked"):  # 조건 성립: 일치가 실제로 있었다(경계 실패 표본도 구분자 쪽 일치 1개 이상)
        found.append(("masked",))
    for kind in ("text", "url"):
        found += [(kind, *verdict) for verdict, _ in _violations(rows, kind, tuple(SIZES))]
    return found


@pytest.mark.parametrize("case", CASES)
def test_one_mib_under_a_second_and_linear(case):
    rows = _run(case)
    print(json.dumps({"case": case, **rows}))
    assert rows["values"] == len(_values(case))  # 조건 성립: 모든 값을 기억했다(밀려남 없음)
    assert rows["masked"], case  # 조건 성립: 일치가 실제로 있었다(경계 실패 표본도 구분자 쪽 일치 1개 이상)
    first = _all_violations(case, rows)
    if first:
        # WS-70: 위반이 하나라도 있으면 새 격리 자식에서 원래와 같은 크기 집합·라운드 순서(라운드마다 모든 크기·종류)로
        # PERF_RECHECK_ROUNDS 번 다시 재고, 그 결과에 모든 판정을 다시 적용한다 — 원래 위반이 사라져도 다른 위반이
        # 하나라도 나면 실패(WS-70 R1 B1: 재측정이 원래 판정만 보면 크기가 바뀔 때마다 생기는 지연 같은 회귀를 놓친다).
        again = _run(case, PERF_RECHECK_ROUNDS)
        record = {"case": case, "first_violations": first, "first": {k: rows[k] for k in ("text", "url", "runs")},
                  "recheck": {k: again[k] for k in ("text", "url", "runs")}}
        print("WS-70 재측정", json.dumps(record))
        assert not _all_violations(case, again), ("재측정에서도 위반", _all_violations(case, again), record)


if __name__ == "__main__":
    _child(sys.argv[1], int(sys.argv[2]), [int(n) for n in sys.argv[3].split(",")], sys.argv[4].split(","))
