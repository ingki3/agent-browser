"""WS-51 R4 — 성능 행렬(계획 §1.5·§3 R3-B6·B5). 측정 조건 고정: 같은 Mac, 새 프로세스, 5회 중앙값, 매회 캐시를
거치지 않는 본체(`_public_text`·주소 칸 본체), 2배 비율(≤ 2.5)은 256KiB 이상 구간에서만.

실 observe+wire 경로는 tests/interface/test_ws51_r4_e2e.py. 값은 합성 값.
"""

from __future__ import annotations

import json
import os
import random
import statistics
import string
import subprocess
import sys
import time
from pathlib import Path

import pytest

MiB = 1 << 20
SIZES = [256 << 10, 512 << 10, 1 << 20]


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


def _child(case: str) -> None:
    from security import secret_registry
    from security.public_url import _public_text, _public_url_field_uncached

    values = _values(case)
    secret_registry.REGISTRY.add_values(values)
    rows = {"values": len(secret_registry.REGISTRY), "chars": secret_registry.REGISTRY.chars, "text": {}, "url": {}}
    for n in SIZES:
        text = _text(case, n, values)
        for kind, fn in (("text", _public_text), ("url", lambda t: _public_url_field_uncached("/p?" + t))):
            runs = []
            out = ""
            for _ in range(5):
                t = time.perf_counter()
                out = fn(text)
                runs.append(time.perf_counter() - t)
            rows[kind][n] = statistics.median(runs)
            if kind == "text" and n == SIZES[-1]:
                rows["masked"] = out.count("[REDACTED]") > 0
    print(json.dumps(rows))


def _run(case: str) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src") + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run([sys.executable, __file__, case], capture_output=True, text=True, timeout=300, env=env)
    except subprocess.TimeoutExpired:
        pytest.fail(f"{case}: 300초 안에 끝나지 않음")
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("case", CASES)
def test_one_mib_under_a_second_and_linear(case):
    rows = _run(case)
    print(json.dumps({"case": case, **rows}))
    assert rows["values"] == len(_values(case))  # 조건 성립: 모든 값을 기억했다(밀려남 없음)
    assert rows["masked"], case  # 조건 성립: 일치가 실제로 있었다(경계 실패 표본도 구분자 쪽 일치 1개 이상)
    for kind in ("text", "url"):
        times = {int(k): v for k, v in rows[kind].items()}
        assert times[MiB] < 1.0, (case, kind, times)
        for small, big in zip(SIZES, SIZES[1:]):
            assert times[big] <= 2.5 * max(times[small], 0.005), (case, kind, small, big, times)


if __name__ == "__main__":
    _child(sys.argv[1])
