"""자격증명 플레이스홀더 치환 테스트 (PRD 5.3, WS-20).

목적: 자격증명 평문이 LLM 컨텍스트에 유입되지 않는가.

    LLM이 보내는 것    type_text(text="X-PASSWORD")
    실제 입력되는 것    실제 비밀번호
    트레이스에 남는 것  "X-PASSWORD"

WS-19의 마스킹은 사후 방어였다(값이 이미 프롬프트로 전송된 뒤 기록할
때만 가림). 이 기능은 전송 자체를 없앤다.

한계: 치환된 값은 DOM에 존재하므로 이후 관찰·스크린샷에 노출될 수
있다. "LLM 컨텍스트 유입 차단"만 보장하며 종단 간 기밀성은 보장하지
않는다.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from security import SecretsError, SecretStore


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            b.close()
        return True
    except Exception:  # noqa: BLE001
        return False


requires_chromium = pytest.mark.skipif(
    not _chromium_available(), reason="Chromium 바이너리 없음"
)

SECRET = "P@ssw0rd!RealSecret"


@pytest.fixture()
def secrets_file():
    fd, path = tempfile.mkstemp(suffix=".env")
    os.write(fd, f"X-LOGIN=myaccount\nX-PASSWORD={SECRET}\n".encode())
    os.close(fd)
    os.chmod(path, 0o600)
    yield path
    if os.path.exists(path):
        os.unlink(path)


# ---------------------------------------------------------------------------
# 1. 파일 로드 및 권한
# ---------------------------------------------------------------------------


def test_loads_dotenv_keys(secrets_file):
    store = SecretStore.from_file(secrets_file)
    assert len(store) == 2
    assert store.is_key("X-PASSWORD")


def test_rejects_loose_permissions(secrets_file):
    """0600이 아니면 기동을 거부해야 한다.

    자격증명 파일이 다른 사용자에게 읽히면 치환의 의미가 없다.
    """
    os.chmod(secrets_file, 0o644)
    with pytest.raises(SecretsError) as exc:
        SecretStore.from_file(secrets_file)
    assert "권한" in str(exc.value)


def test_missing_file_raises():
    with pytest.raises(SecretsError):
        SecretStore.from_file("/nonexistent/secrets.env")


def test_repr_never_exposes_values(secrets_file):
    """디버깅 출력에 값이 새면 안 된다."""
    store = SecretStore.from_file(secrets_file)
    assert SECRET not in repr(store)
    assert "X-PASSWORD" in repr(store)


# ---------------------------------------------------------------------------
# 2. 해석 규칙
# ---------------------------------------------------------------------------


def test_resolves_registered_key(secrets_file):
    store = SecretStore.from_file(secrets_file)
    r = store.resolve("X-PASSWORD")
    assert r.resolved is True
    assert r.value == SECRET


def test_unregistered_key_passes_through(secrets_file):
    """미등록 키는 원본 그대로 — 조용한 실패를 만들지 않는다.

    빈 문자열로 바꾸면 사용자는 왜 로그인이 실패하는지 알 수 없다.
    """
    store = SecretStore.from_file(secrets_file)
    r = store.resolve("X-TYPO")
    assert r.resolved is False
    assert r.value == "X-TYPO"


@pytest.mark.parametrize(
    "text", ["그냥 텍스트", "hello world", "", "lowercase", "x", "A"]
)
def test_non_key_text_untouched(secrets_file, text):
    """키 형식이 아닌 일반 입력은 절대 건드리지 않는다."""
    store = SecretStore.from_file(secrets_file)
    r = store.resolve(text)
    assert r.resolved is False
    assert r.value == text


def test_parser_skips_invalid_keys():
    """소문자·짧은 키는 우연한 일치를 막기 위해 등록하지 않는다."""
    store = SecretStore(SecretStore._parse(
        "lower=v\nAB=v\nGOOD_KEY=v\n# comment\nnoequals\n"
    ))
    assert store.is_key("GOOD_KEY")
    assert not store.is_key("lower")
    assert not store.is_key("AB")


def test_parser_strips_quotes():
    store = SecretStore(SecretStore._parse('X-QUOTED="value with space"\n'))
    assert store.resolve("X-QUOTED").value == "value with space"


# ---------------------------------------------------------------------------
# 3. 디스패처 통합 — 핵심 검증
# ---------------------------------------------------------------------------


@requires_chromium
async def test_dispatcher_substitutes_without_leaking(secrets_file):
    """LLM은 키만 보는데 DOM에는 실제 값이 들어가야 한다."""
    from playwright.async_api import async_playwright

    from actions import ActionDispatcher, DispatchContext
    from contracts import ActionType
    from interface.observability import StepRecord
    from perception import PerceptionEngine

    store = SecretStore.from_file(secrets_file)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content(
            "<form><input id='p' type='password' placeholder='비밀번호'></form>"
        )

        engine = PerceptionEngine()
        cdp = await context.new_cdp_session(page)
        disp = ActionDispatcher(
            DispatchContext(
                page=page, engine=engine, cdp=cdp, secrets=store
            )
        )
        obs = await engine.observe_page(page=page, prune_top_n=20)
        el = obs.elements[0]

        llm_params = {
            "element_id": el.element_id,
            "epoch": obs.snapshot_epoch,
            "text": "X-PASSWORD",
        }
        # 사본을 만들지 않고 그대로 넘긴다. 디스패처가 원본을 변형하면
        # 호출자의 트레이스에 평문이 섞인다(사보타주로 확인된 경로).
        result = await disp.dispatch(ActionType.TYPE_TEXT, llm_params)
        actual = await page.input_value("input#p")

        # 호출자가 들고 있는 params가 오염되지 않았는지 확인
        caller_params_intact = llm_params["text"] == "X-PASSWORD"

        record = StepRecord(
            correlation_id="c1", step=1, action="type_text",
            snapshot_epoch=0, success=True, latency_ms=1.0,
            observation_tokens=10, observation_summary="",
            action_input=llm_params,
        )
        trace_line = json.dumps(record.to_masked_dict(), ensure_ascii=False)

        await browser.close()

    assert result.success is True
    assert result.data.get("secret_resolved") is True
    assert actual == SECRET, "실제 값이 입력되지 않았습니다"
    assert caller_params_intact, (
        "디스패처가 호출자의 params를 변형했습니다 — 트레이스에 평문이 샙니다"
    )
    assert SECRET not in trace_line, "트레이스에 평문이 남았습니다"
    assert "X-PASSWORD" in trace_line, "키 이름이 기록되지 않았습니다"


@requires_chromium
async def test_unregistered_key_reported_not_silent(secrets_file):
    """오타 난 키는 그대로 입력되고 secret_resolved=False로 보고된다."""
    from playwright.async_api import async_playwright

    from actions import ActionDispatcher, DispatchContext
    from contracts import ActionType
    from perception import PerceptionEngine

    store = SecretStore.from_file(secrets_file)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content("<form><input id='p' type='text'></form>")

        engine = PerceptionEngine()
        cdp = await context.new_cdp_session(page)
        disp = ActionDispatcher(
            DispatchContext(page=page, engine=engine, cdp=cdp, secrets=store)
        )
        obs = await engine.observe_page(page=page, prune_top_n=20)

        result = await disp.dispatch(ActionType.TYPE_TEXT, {
            "element_id": obs.elements[0].element_id,
            "epoch": obs.snapshot_epoch,
            "text": "X-TYPO",
        })
        actual = await page.input_value("input#p")
        await browser.close()

    assert result.data.get("secret_resolved") is False, (
        "미등록 키가 조용히 넘어갔습니다"
    )
    assert actual == "X-TYPO"


@requires_chromium
async def test_no_store_means_no_substitution():
    """해석기 미주입 시 기존 동작이 그대로여야 한다 (하위 호환)."""
    from playwright.async_api import async_playwright

    from actions import ActionDispatcher, DispatchContext
    from contracts import ActionType
    from perception import PerceptionEngine

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content("<form><input id='p' type='text'></form>")

        engine = PerceptionEngine()
        cdp = await context.new_cdp_session(page)
        disp = ActionDispatcher(
            DispatchContext(page=page, engine=engine, cdp=cdp)
        )
        obs = await engine.observe_page(page=page, prune_top_n=20)

        result = await disp.dispatch(ActionType.TYPE_TEXT, {
            "element_id": obs.elements[0].element_id,
            "epoch": obs.snapshot_epoch,
            "text": "X-PASSWORD",
        })
        actual = await page.input_value("input#p")
        await browser.close()

    assert "secret_resolved" not in result.data
    assert actual == "X-PASSWORD"


# ---------------------------------------------------------------------------
# 결과 페이로드 누출 (2026-09-23 네이버 로그인 테스트 전 점검에서 발견)
#
# 사후조건 검증이 type_text의 기대값을 **치환된 실제 값**으로 받는다. 검증
# 신호(`value_applied: '<값>'`)와 실패 메시지(`기대값 '<값>' != 실제 ...`)에
# 그대로 실려 ActionResult로 나간다. MCP 서버는 ActionResult를 호출한 LLM에
# 돌려주므로 "LLM에는 키 이름만" 보장이 깨진다. 기존 테스트는 트레이스만 봤다.
# 값을 가공해 받는 입력(마스킹·자동 포맷)이 흔하므로 실패 경로가 현실적이다.
# ---------------------------------------------------------------------------


def _all_text(result) -> str:
    return json.dumps(result.model_dump(mode="json"), ensure_ascii=False)


@requires_chromium
@pytest.mark.parametrize(
    "html",
    [
        # 그대로 들어가는 입력 — 성공 신호에 값이 실렸다
        "<form><input id='p' type='password' placeholder='비밀번호'></form>",
        # 입력을 가공하는 필드(대문자 변환) — 실패 메시지에 값이 실렸다
        "<form><input id='p' type='text' placeholder='비밀번호' "
        "oninput=\"this.value=this.value.toUpperCase()\"></form>",
    ],
    ids=["applied", "rewritten"],
)
async def test_resolved_secret_never_appears_in_action_result(secrets_file, html):
    from playwright.async_api import async_playwright

    from actions import ActionDispatcher, DispatchContext
    from contracts import ActionType
    from perception import PerceptionEngine

    store = SecretStore.from_file(secrets_file)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content(html)
        engine = PerceptionEngine()
        cdp = await context.new_cdp_session(page)
        disp = ActionDispatcher(
            DispatchContext(page=page, engine=engine, cdp=cdp, secrets=store)
        )
        obs = await engine.observe_page(page=page, prune_top_n=20)
        result = await disp.dispatch(ActionType.TYPE_TEXT, {
            "element_id": obs.elements[0].element_id,
            "epoch": obs.snapshot_epoch,
            "text": "X-PASSWORD",
        })
        await browser.close()

    assert result.data.get("secret_resolved") is True
    assert SECRET not in _all_text(result), "ActionResult에 평문이 실렸습니다"
    assert SECRET.upper() not in _all_text(result), "가공된 평문이 실렸습니다"


@requires_chromium
async def test_observation_never_carries_password_field_value():
    """비밀번호 필드 값은 관찰 결과에 실리지 않는다.

    MCP `observe_page`는 관찰 모델 전체(요소 `value` 포함)를 호출한 LLM에
    돌려준다. 입력 직후 관찰하면 방금 넣은 비밀번호가 그대로 나갔다.
    일반/shadow DOM 양쪽을 본다.
    """
    from playwright.async_api import async_playwright

    from perception import PerceptionEngine

    html = (
        "<input id='a' type='password' value='%s'>"
        "<div id='h'></div>"
        "<script>const r=document.getElementById('h').attachShadow({mode:'open'});"
        "r.innerHTML=\"<input type='password' value='%s'>\";</script>"
    ) % (SECRET, SECRET)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(html)
        obs = await PerceptionEngine().observe_page(page=page, prune_top_n=20)
        await browser.close()

    dumped = json.dumps(obs.model_dump(mode="json"), ensure_ascii=False)
    assert len(obs.elements) >= 2, "비밀번호 필드가 관찰되지 않아 검증이 무의미합니다"
    assert SECRET not in dumped, "관찰 결과에 비밀번호 필드 값이 실렸습니다"


# ---------------------------------------------------------------------------
# 4. CLI / 서버 배선
# ---------------------------------------------------------------------------


def test_cli_exposes_secrets_option():
    import inspect

    from interface import cli

    assert "--secrets" in inspect.getsource(cli._build_parser)


def test_run_stdio_accepts_secrets_path():
    import inspect

    from interface.mcp_server import run_stdio

    assert "secrets_path" in inspect.signature(run_stdio).parameters


def test_dispatch_context_has_secrets_field():
    import dataclasses

    from actions import DispatchContext

    names = {f.name for f in dataclasses.fields(DispatchContext)}
    assert "secrets" in names
