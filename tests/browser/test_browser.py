"""WS-1 브라우저 코어 테스트 (Gate 1 항목 1).

세션 암호화, 만료 프로브, 컨텍스트/탭 격리를 검증한다.
Playwright 실브라우저가 필요한 테스트는 별도 마킹 없이 동작하되,
브라우저 바이너리가 없으면 skip 처리한다.
"""

from __future__ import annotations

import os
import stat

import pytest

from browser import (
    CI_ENV_VAR,
    FILE_MODE,
    DecryptionError,
    KeyUnavailableError,
    PageSignals,
    ProbeTier,
    ProfileProbeConfig,
    SessionStore,
    SessionStoreError,
    detect_expiry,
    resolve_passphrase,
)
from browser.session_store import (
    ARGON2_ITERATIONS,
    ARGON2_LANES,
    ARGON2_MEMORY_COST_KIB,
    HEADER_LEN,
    MAGIC,
    NONCE_BYTES,
    SALT_BYTES,
)

SAMPLE_STATE = {
    "cookies": [
        {"name": "sid", "value": "abc123", "domain": "example.com", "path": "/"}
    ],
    "origins": [
        {
            "origin": "https://example.com",
            "localStorage": [{"name": "token", "value": "xyz"}],
        }
    ],
}

PASSPHRASE = "테스트-마스터-패스프레이즈-01"


@pytest.fixture
def store(tmp_path):
    return SessionStore(auth_dir=tmp_path / "auth")


# ---------------------------------------------------------------------------
# 1. 암호화 규격 (PRD §5.1-1)
# ---------------------------------------------------------------------------


def test_argon2_parameters_match_prd():
    assert ARGON2_ITERATIONS == 3
    assert ARGON2_MEMORY_COST_KIB == 65536
    assert ARGON2_LANES == 4
    assert SALT_BYTES == 16
    assert NONCE_BYTES == 12  # 96-bit


def test_encrypt_decrypt_round_trip(store):
    blob = store.encrypt(SAMPLE_STATE, PASSPHRASE)
    assert store.decrypt(blob, PASSPHRASE) == SAMPLE_STATE


def test_ciphertext_has_self_describing_header(store):
    blob = store.encrypt(SAMPLE_STATE, PASSPHRASE)
    assert blob.startswith(MAGIC)
    assert len(blob) > HEADER_LEN


def test_plaintext_is_not_present_in_ciphertext(store):
    blob = store.encrypt(SAMPLE_STATE, PASSPHRASE)
    assert b"abc123" not in blob
    assert b"localStorage" not in blob


def test_nonce_is_unique_per_encryption(store):
    """Nonce 재사용은 GCM에서 치명적이다."""
    nonces = set()
    for _ in range(20):
        blob = store.encrypt(SAMPLE_STATE, PASSPHRASE)
        nonces.add(blob[len(MAGIC) + 1 + SALT_BYTES : HEADER_LEN])
    assert len(nonces) == 20


def test_salt_is_unique_per_encryption(store):
    salts = {
        store.encrypt(SAMPLE_STATE, PASSPHRASE)[
            len(MAGIC) + 1 : len(MAGIC) + 1 + SALT_BYTES
        ]
        for _ in range(20)
    }
    assert len(salts) == 20


def test_wrong_passphrase_raises(store):
    blob = store.encrypt(SAMPLE_STATE, PASSPHRASE)
    with pytest.raises(DecryptionError):
        store.decrypt(blob, "틀린-패스프레이즈")


def test_tampered_ciphertext_is_detected(store):
    """GCM 인증 태그가 변조를 탐지해야 한다."""
    blob = bytearray(store.encrypt(SAMPLE_STATE, PASSPHRASE))
    blob[-1] ^= 0xFF
    with pytest.raises(DecryptionError):
        store.decrypt(bytes(blob), PASSPHRASE)


def test_tampered_header_is_detected(store):
    """헤더가 AAD로 묶여 salt/nonce 변조도 탐지되어야 한다."""
    blob = bytearray(store.encrypt(SAMPLE_STATE, PASSPHRASE))
    blob[len(MAGIC) + 1] ^= 0xFF  # salt 첫 바이트 변조
    with pytest.raises(DecryptionError):
        store.decrypt(bytes(blob), PASSPHRASE)


def test_unknown_format_is_rejected(store):
    with pytest.raises(DecryptionError):
        store.decrypt(b"NOTAVALIDFILE" + b"\x00" * 40, PASSPHRASE)


# ---------------------------------------------------------------------------
# 2. 파일 저장 및 권한 (Gate 1 항목 2)
# ---------------------------------------------------------------------------


def test_save_creates_file_with_0600(store):
    path = store.save("default", SAMPLE_STATE, PASSPHRASE)
    assert path.exists()
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == FILE_MODE
    assert store.verify_permissions("default")


def test_auth_dir_is_not_world_readable(store):
    store.save("default", SAMPLE_STATE, PASSPHRASE)
    if os.name != "nt":
        mode = stat.S_IMODE(store.auth_dir.stat().st_mode)
        assert mode & 0o077 == 0, f"디렉터리 권한이 과도합니다: {oct(mode)}"


def test_save_load_round_trip(store):
    store.save("prof", SAMPLE_STATE, PASSPHRASE)
    assert store.load("prof", PASSPHRASE) == SAMPLE_STATE


def test_no_temp_file_left_behind(store):
    store.save("prof", SAMPLE_STATE, PASSPHRASE)
    leftovers = list(store.auth_dir.glob("*.tmp"))
    assert leftovers == []


def test_load_missing_profile_raises(store):
    with pytest.raises(SessionStoreError):
        store.load("does_not_exist", PASSPHRASE)


def test_path_traversal_is_rejected(store):
    for bad in ("../escape", "a/b", "a\\b", ""):
        with pytest.raises(SessionStoreError):
            store.path_for(bad)


def test_delete_removes_file(store):
    store.save("prof", SAMPLE_STATE, PASSPHRASE)
    assert store.delete("prof") is True
    assert store.exists("prof") is False
    assert store.delete("prof") is False


# ---------------------------------------------------------------------------
# 3. 키 우선순위 (PRD §5.1-1)
# ---------------------------------------------------------------------------


def test_prompt_is_used_when_keyring_empty(monkeypatch):
    monkeypatch.delenv(CI_ENV_VAR, raising=False)
    resolution = resolve_passphrase("p1", prompt_fn=lambda _: "from-prompt")
    assert resolution.passphrase == "from-prompt"
    assert resolution.source == "prompt"


def test_ci_env_is_last_resort(monkeypatch):
    monkeypatch.setenv(CI_ENV_VAR, "from-ci")
    resolution = resolve_passphrase("p1", allow_prompt=False)
    assert resolution.passphrase == "from-ci"
    assert resolution.source == "ci_env"


def test_ci_env_emits_warning(monkeypatch, caplog):
    monkeypatch.setenv(CI_ENV_VAR, "from-ci")
    with caplog.at_level("WARNING"):
        resolve_passphrase("p1", allow_prompt=False)
    assert any(CI_ENV_VAR in r.getMessage() for r in caplog.records)


def test_no_key_source_raises(monkeypatch):
    monkeypatch.delenv(CI_ENV_VAR, raising=False)
    with pytest.raises(KeyUnavailableError):
        resolve_passphrase("p1", allow_prompt=False)


# ---------------------------------------------------------------------------
# 4. 세션 만료 프로브 (PRD §5.1-3)
# ---------------------------------------------------------------------------


def test_custom_endpoint_200_means_valid():
    result = detect_expiry(
        PageSignals(url="https://a.test/login", custom_probe_status=200,
                    visible_password_inputs=1, redirected_to_login=True)
    )
    assert result.expired is False
    assert result.tier is ProbeTier.CUSTOM_ENDPOINT


def test_custom_endpoint_401_means_expired():
    result = detect_expiry(
        PageSignals(url="https://a.test/x", custom_probe_status=401)
    )
    assert result.expired is True
    assert result.tier is ProbeTier.CUSTOM_ENDPOINT


@pytest.mark.parametrize("status", [401, 403])
def test_http_status_tier(status):
    result = detect_expiry(PageSignals(url="https://a.test/x", http_status=status))
    assert result.expired is True
    assert result.tier is ProbeTier.HTTP_STATUS


def test_single_heuristic_signal_is_not_enough():
    """오탐 억제: 로그인 URL 방문만으로 만료 판정하지 않는다."""
    result = detect_expiry(PageSignals(url="https://a.test/login", http_status=200))
    assert result.expired is False


def test_two_heuristic_signals_trigger_expiry():
    result = detect_expiry(
        PageSignals(
            url="https://a.test/login",
            http_status=200,
            visible_password_inputs=1,
            redirected_to_login=True,
        )
    )
    assert result.expired is True
    assert result.tier is ProbeTier.HEURISTIC
    assert len(result.evidence) >= 2


def test_authenticated_markers_suppress_heuristic():
    """대시보드 내 재인증 모달을 만료로 오인하면 안 된다."""
    result = detect_expiry(
        PageSignals(
            url="https://a.test/login",
            http_status=200,
            visible_password_inputs=2,
            redirected_to_login=True,
            has_authenticated_markers=True,
        )
    )
    assert result.expired is False


def test_allowed_login_path_is_not_expiry():
    config = ProfileProbeConfig(login_form_allowed_paths=("/embed/auth",))
    result = detect_expiry(
        PageSignals(
            url="https://a.test/embed/auth",
            http_status=200,
            visible_password_inputs=1,
            redirected_to_login=True,
        ),
        config,
    )
    assert result.expired is False


def test_server_error_is_not_session_expiry():
    result = detect_expiry(PageSignals(url="https://a.test/x", http_status=500))
    assert result.expired is False


# ---------------------------------------------------------------------------
# 5. BrowserCore (실브라우저)
# ---------------------------------------------------------------------------


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:  # noqa: BLE001
        return False


requires_chromium = pytest.mark.skipif(
    not _chromium_available(), reason="Chromium 바이너리 없음"
)


@requires_chromium
async def test_contexts_are_isolated_per_profile(tmp_path):
    from browser import BrowserCore

    async with BrowserCore(session_store=SessionStore(tmp_path / "auth")) as core:
        ctx_a = await core.new_context("profile-a")
        ctx_b = await core.new_context("profile-b")
        assert ctx_a is not ctx_b
        assert core.context_count == 2


@requires_chromium
async def test_same_profile_reuses_context(tmp_path):
    from browser import BrowserCore

    async with BrowserCore(session_store=SessionStore(tmp_path / "auth")) as core:
        first = await core.new_context("p")
        second = await core.new_context("p")
        assert first is second
        assert core.context_count == 1


@requires_chromium
async def test_context_limit_is_enforced(tmp_path):
    from browser import BrowserCore, BrowserCoreError

    async with BrowserCore(
        session_store=SessionStore(tmp_path / "auth"), max_contexts=2
    ) as core:
        await core.new_context("a")
        await core.new_context("b")
        with pytest.raises(BrowserCoreError):
            await core.new_context("c")


@requires_chromium
async def test_tab_limit_is_enforced(tmp_path):
    from browser import BrowserCore, BrowserCoreError

    async with BrowserCore(
        session_store=SessionStore(tmp_path / "auth"), max_tabs=2
    ) as core:
        await core.new_tab("p")
        await core.new_tab("p")
        with pytest.raises(BrowserCoreError):
            await core.new_tab("p")


@requires_chromium
async def test_tab_lifecycle_and_active_tracking(tmp_path):
    from browser import BrowserCore

    async with BrowserCore(session_store=SessionStore(tmp_path / "auth")) as core:
        t1 = await core.new_tab("p")
        t2 = await core.new_tab("p")
        assert core.active_tab_id == t2.tab_id

        core.switch_tab(t1.tab_id)
        assert core.active_tab_id == t1.tab_id

        await core.close_tab(t1.tab_id)
        assert core.tab_count == 1
        assert core.active_tab_id == t2.tab_id


@requires_chromium
async def test_session_save_and_restore_round_trip(tmp_path):
    """Gate 1 핵심: 암호화 저장 → 복원 후 쿠키가 살아있어야 한다."""
    from harness import MockServer

    from browser import BrowserCore

    store = SessionStore(tmp_path / "auth")
    with MockServer() as server:
        async with BrowserCore(session_store=store) as core:
            await core.new_context("saved")
            tab = await core.new_tab("saved", server.site_url("s01_login"))
            await tab.page.context.add_cookies(
                [
                    {
                        "name": "session_id",
                        "value": "restored-value",
                        "url": server.base_url,
                    }
                ]
            )
            path = await core.save_session("saved", PASSPHRASE)

        assert store.verify_permissions("saved")

        async with BrowserCore(session_store=store) as core2:
            ctx = await core2.new_context_with_session("saved", PASSPHRASE)
            cookies = await ctx.cookies(server.base_url)
            names = {c["name"]: c["value"] for c in cookies}
            assert names.get("session_id") == "restored-value"

    assert path.endswith("saved.enc")


@requires_chromium
async def test_cdp_session_is_available(tmp_path):
    from browser import BrowserCore

    async with BrowserCore(session_store=SessionStore(tmp_path / "auth")) as core:
        await core.new_tab("p")
        cdp = await core.new_cdp_session()
        result = await cdp.send("Runtime.evaluate", {"expression": "1 + 1"})
        assert result["result"]["value"] == 2
