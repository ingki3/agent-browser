"""자격증명 YAML 저장소 테스트.

파일 형식 (권한 600):

    credentials:
      - domain: naver.com
        username: myid
        password: "p@ss"

모델은 키 이름(LOGIN_USERNAME / LOGIN_PASSWORD)만 보고, 실제 값은 **그 도메인
페이지에서 입력할 때만** 들어간다. 저장소는 URL로 해당 도메인의 자격증명을 고른다.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from security.credentials import (
    PASSWORD_KEY,
    USERNAME_KEY,
    CredentialError,
    CredentialStore,
)


def _write(text: str, mode: int = 0o600) -> str:
    fd, path = tempfile.mkstemp(suffix=".yaml")
    os.write(fd, text.encode())
    os.close(fd)
    os.chmod(path, mode)
    return path


@pytest.fixture()
def cred_file():
    path = _write(
        "credentials:\n"
        "  - domain: naver.com\n"
        "    username: naver_user\n"
        "    password: 'n@ver:pw #1'\n"
        "  - domain: mail.example.org\n"
        "    username: ex_user\n"
        "    password: ex_pw\n"
        "  - domain: example.org\n"
        "    username: parent_user\n"
        "    password: parent_pw\n"
    )
    yield path
    os.unlink(path)


# --- 로드·검증 -------------------------------------------------------------


def test_loads_pairs(cred_file):
    store = CredentialStore.from_file(cred_file)
    assert sorted(store.domains()) == ["example.org", "mail.example.org", "naver.com"]


def test_rejects_loose_permissions(cred_file):
    os.chmod(cred_file, 0o644)
    with pytest.raises(CredentialError, match="권한"):
        CredentialStore.from_file(cred_file)


def test_missing_file():
    with pytest.raises(CredentialError, match="없습니다"):
        CredentialStore.from_file("/nonexistent/credentials.yaml")


@pytest.mark.parametrize(
    "value",
    ["123456", "0012", "true", "no", "1.5", "~", "2026-09-23"],
)
def test_non_string_password_is_rejected_not_coerced(value):
    """YAML은 따옴표 없는 123456·0012·no를 숫자/불리언/날짜로 읽는다.

    str()로 되돌리면 0012 → '10'(8진수)·'12', no → 'False' 처럼 **다른 값이
    조용히 입력된다**. 로그인이 실패해도 원인을 알 수 없으므로 거부한다.
    """
    path = _write(
        "credentials:\n"
        f"  - domain: a.com\n    username: u\n    password: {value}\n"
    )
    try:
        with pytest.raises(CredentialError, match="따옴표"):
            CredentialStore.from_file(path)
    finally:
        os.unlink(path)


def test_error_never_contains_secret_value():
    path = _write(
        "credentials:\n  - domain: a.com\n    username: u\n    password: 99887766\n"
    )
    try:
        with pytest.raises(CredentialError) as exc:
            CredentialStore.from_file(path)
        assert "99887766" not in str(exc.value)
    finally:
        os.unlink(path)


@pytest.mark.parametrize(
    "body, fragment",
    [
        ("credentials: []\n", "비어"),
        ("foo: bar\n", "credentials"),
        ("credentials:\n  - domain: a.com\n    username: u\n", "password"),
        ("credentials:\n  - username: u\n    password: p\n", "domain"),
        ("credentials:\n  - domain: 'https://a.com/login'\n    username: u\n    password: p\n",
         "도메인"),
        ("credentials:\n  - domain: a.com\n    username: u\n    password: p\n"
         "  - domain: A.com\n    username: v\n    password: q\n", "중복"),
    ],
)
def test_schema_errors(body, fragment):
    path = _write(body)
    try:
        with pytest.raises(CredentialError, match=fragment):
            CredentialStore.from_file(path)
    finally:
        os.unlink(path)


# --- 도메인 매칭 -----------------------------------------------------------


@pytest.mark.parametrize(
    "url, user",
    [
        ("https://nid.naver.com/nidlogin.login", "naver_user"),
        ("https://naver.com/", "naver_user"),
        ("https://mail.example.org/inbox", "ex_user"),       # 더 구체적인 쪽
        ("https://www.example.org/", "parent_user"),
        ("https://deep.mail.example.org/x", "ex_user"),
    ],
)
def test_for_url_picks_most_specific(cred_file, url, user):
    store = CredentialStore.from_file(cred_file)
    cred = store.for_url(url)
    assert cred is not None and cred.username == user


@pytest.mark.parametrize(
    "url",
    [
        "https://evilnaver.com/login",          # 접미사만 같은 다른 도메인
        "https://naver.com.evil.io/login",       # 앞부분만 같은 다른 도메인
        "http://127.0.0.1/",
        "about:blank",
        "",
    ],
)
def test_for_url_rejects_lookalikes(cred_file, url):
    assert CredentialStore.from_file(cred_file).for_url(url) is None


# --- 시크릿 치환기 연결 -----------------------------------------------------


def test_secrets_for_url_resolves_only_standard_keys(cred_file):
    store = CredentialStore.from_file(cred_file)
    secrets = store.secrets_for("https://nid.naver.com/nidlogin.login")
    assert secrets.resolve(USERNAME_KEY).value == "naver_user"
    assert secrets.resolve(PASSWORD_KEY).value == "n@ver:pw #1"
    assert secrets.resolve("NAVER_PW").resolved is False


def test_bound_secrets_refuse_other_domains(cred_file):
    """치환기는 자기 도메인 안에서만 값을 내준다 (피싱·오입력 방지)."""
    secrets = CredentialStore.from_file(cred_file).secrets_for("https://nid.naver.com/")
    assert secrets.allowed_for("https://nid.naver.com/nidlogin.login")
    assert not secrets.allowed_for("https://evilnaver.com/login")
    assert not secrets.allowed_for("https://mail.example.org/")


def test_repr_hides_values(cred_file):
    store = CredentialStore.from_file(cred_file)
    text = repr(store) + repr(store.for_url("https://naver.com")) + repr(
        store.secrets_for("https://naver.com")
    )
    assert "n@ver:pw #1" not in text
    assert "naver_user" not in text
