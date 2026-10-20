"""WS-56 — `agent-browser approve-policy add|list|remove` (사람 CLI 만 정책을 추가한다).

임시 프로필 루트(AGENT_BROWSER_PROFILE_ROOT)만 쓴다. 실제 홈 폴더 무접촉.
"""

from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path

import pytest

from browser import serve_profile as sp
from interface import approve_policy_cli, cli
from security import approve_policy as ap


@pytest.fixture
def root(tmp_path: Path, monkeypatch) -> Path:
    r = tmp_path / "profiles"
    r.mkdir()
    monkeypatch.setenv(sp.PROFILE_ROOT_ENV, str(r))
    return r


def _tty(monkeypatch, answer: str) -> None:
    monkeypatch.setattr(approve_policy_cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr("sys.stdin", io.StringIO(answer + "\n"))


ADD = ["approve-policy", "add", "--profile", "p1", "--origin", "https://shop.test", "--action", "click",
       "--target-name", "제출"]


def test_add_requires_terminal(root, capsys):
    assert cli.main(ADD) == 2
    assert "터미널" in capsys.readouterr().err
    assert not (root / "serve-p1" / ap.POLICY_FILE).exists()


def test_add_needs_typed_origin(root, monkeypatch, capsys):
    _tty(monkeypatch, "y")
    assert cli.main(ADD) == 1
    assert not (root / "serve-p1" / ap.POLICY_FILE).exists()


def test_add_list_remove(root, monkeypatch, capsys):
    _tty(monkeypatch, "https://shop.test")
    assert cli.main(ADD) == 0
    path = root / "serve-p1" / ap.POLICY_FILE
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    capsys.readouterr()
    assert cli.main(["approve-policy", "list", "--profile", "p1", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["policies"]
    assert [(r["origin"], r["action"], r["target_name"]) for r in rows] == [("https://shop.test", "click", "제출")]
    assert cli.main(["approve-policy", "list", "--profile", "p1"]) == 0
    assert "https://shop.test" in capsys.readouterr().out
    assert cli.main(["approve-policy", "remove", "--profile", "p1", "--origin", "https://shop.test",
                     "--action", "click", "--target-name", "제출"]) == 0
    assert ap.read_policies(path) == []
    assert cli.main(["approve-policy", "remove", "--profile", "p1", "--origin", "https://shop.test",
                     "--action", "click", "--target-name", "제출"]) == 1


@pytest.mark.parametrize("extra,word", [
    (["--action", "upload_file"], "upload_file"),
    (["--action", "click", "--target-name", "결제하기"], "payment"),
    (["--action", "click", "--target-name", "회원 탈퇴"], "account_termination"),
    (["--action", "click", "--target-name", "관리자 권한 부여"], "permission"),
])
def test_top_risk_refused_at_cli(root, monkeypatch, capsys, extra, word):
    _tty(monkeypatch, "https://shop.test")
    argv = ["approve-policy", "add", "--profile", "p1", "--origin", "https://shop.test"] + extra
    assert cli.main(argv) == 2
    err = capsys.readouterr().err
    assert word in err and "정책으로 열 수 없" in err
    assert not (root / "serve-p1" / ap.POLICY_FILE).exists()


def test_bad_origin_and_profile(root, monkeypatch, capsys):
    _tty(monkeypatch, "x")
    assert cli.main(["approve-policy", "add", "--profile", "p1", "--origin", "https://shop.test/a",
                     "--action", "click"]) == 2
    assert cli.main(["approve-policy", "add", "--profile", "../x", "--origin", "https://shop.test",
                     "--action", "click"]) == 2
    assert cli.main(["approve-policy", "list", "--profile", "NOPE!"]) == 2


def test_list_corrupt_file_reports_fail_closed(root, capsys):
    d = root / "serve-p1"
    d.mkdir(mode=0o700)
    (d / ap.POLICY_FILE).write_text("{broken")
    os.chmod(d / ap.POLICY_FILE, 0o600)
    assert cli.main(["approve-policy", "list", "--profile", "p1"]) == 2
    assert "적용되지 않습니다" in capsys.readouterr().err
