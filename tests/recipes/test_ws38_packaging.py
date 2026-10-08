"""WS-38: 새 src/ 패키지가 wheel packages 목록에 빠지면 실패한다.

빠지면 설치는 성공하는데 import 만 실패한다(pyproject 주석: agent/ llm/ 이 실제로 빠져 있었다).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: 알려진 누락 — 코디네이터가 별도 후속으로 처리(WS-38 범위 밖, 2026-10-09 결정). 고치면 지운다.
KNOWN_MISSING = {"src/vision"}


def test_every_src_package_is_in_wheel_packages():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    listed = set(data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"])
    on_disk = {
        f"src/{p.name}" for p in (ROOT / "src").iterdir()
        if p.is_dir() and (p / "__init__.py").is_file()
    }
    assert "src/recipes" in listed
    assert on_disk - listed - KNOWN_MISSING == set()
