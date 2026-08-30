"""WS-7 LLM 어댑터 테스트.

네트워크를 호출하지 않는다. 실제 API 호출은 `harness.llm_probe`가 담당한다.
여기서는 설정 파싱, 키 노출 방지, 예산 강제, 응답 파싱을 검증한다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contracts import thresholds
from llm import (
    BudgetExceeded,
    BudgetGuard,
    LLMConfig,
    LLMError,
    LLMResponse,
    estimate_cost,
    load_config,
    parse_env_text,
)
from llm.config import DEFAULT_MODEL


# ---------------------------------------------------------------------------
# 1. .env 파싱
# ---------------------------------------------------------------------------


def test_parses_basic_pairs():
    env = parse_env_text("OPENROUTER_API_KEY=abc123\nOPENROUTER_MODEL=openai/gpt-4o")
    assert env["OPENROUTER_API_KEY"] == "abc123"
    assert env["OPENROUTER_MODEL"] == "openai/gpt-4o"


def test_ignores_comments_and_blank_lines():
    env = parse_env_text("# 주석\n\nKEY=value\n  # 들여쓴 주석\n")
    assert env == {"KEY": "value"}


def test_strips_export_prefix():
    assert parse_env_text("export KEY=value")["KEY"] == "value"


@pytest.mark.parametrize(
    "line,expected",
    [
        ("KEY='single'", "single"),
        ('KEY="double"', "double"),
        ("KEY=bare", "bare"),
        ("KEY=  spaced  ", "spaced"),
    ],
)
def test_handles_quoting(line, expected):
    assert parse_env_text(line)["KEY"] == expected


def test_strips_inline_comment_only_when_unquoted():
    assert parse_env_text("KEY=value # 주석")["KEY"] == "value"
    # 따옴표 안의 #은 값의 일부다. 키에 #이 들어갈 수 있으므로 중요하다.
    assert parse_env_text('KEY="value # 진짜값"')["KEY"] == "value # 진짜값"


def test_ignores_malformed_lines():
    env = parse_env_text("이건=키가아님은아니고\n그냥텍스트\nGOOD=ok")
    assert env["GOOD"] == "ok"
    assert "그냥텍스트" not in env


# ---------------------------------------------------------------------------
# 2. 설정 로딩 우선순위
# ---------------------------------------------------------------------------


def test_loads_from_env_file(tmp_path: Path):
    path = tmp_path / ".env"
    path.write_text(
        "OPENROUTER_API_KEY=filekey\nOPENROUTER_MODEL=file/model\n", encoding="utf-8"
    )
    config = load_config(path)
    assert config.api_key == "filekey"
    assert config.model == "file/model"


def test_environ_wins_over_env_file(tmp_path: Path, monkeypatch):
    """CI 시크릿이 로컬 .env에 덮이면 안 된다."""
    path = tmp_path / ".env"
    path.write_text("OPENROUTER_API_KEY=filekey\n", encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY", "envkey")
    assert load_config(path).api_key == "envkey"


def test_explicit_model_override_wins(tmp_path: Path):
    path = tmp_path / ".env"
    path.write_text("OPENROUTER_MODEL=file/model\n", encoding="utf-8")
    config = load_config(path, model_override="cli/model")
    assert config.model == "cli/model"


def test_missing_env_file_is_not_an_error(tmp_path: Path):
    config = load_config(tmp_path / "does_not_exist")
    assert config.api_key == ""
    assert config.configured is False
    assert config.model == DEFAULT_MODEL


def test_configured_flag_reflects_key():
    assert LLMConfig(api_key="x").configured is True
    assert LLMConfig().configured is False


# ---------------------------------------------------------------------------
# 3. 키 노출 방지 (보안)
# ---------------------------------------------------------------------------

SECRET = "sk-or-v1-supersecret-value-9876543210"


def test_key_absent_from_repr():
    assert SECRET not in repr(LLMConfig(api_key=SECRET))


def test_key_absent_from_str_and_summary():
    config = LLMConfig(api_key=SECRET)
    assert SECRET not in str(config)
    assert SECRET not in config.summary()


def test_masked_key_shows_prefix_and_length_only():
    masked = LLMConfig(api_key=SECRET).masked_key
    assert masked.startswith("sk-or-")
    assert SECRET not in masked
    assert str(len(SECRET)) in masked


def test_short_key_is_not_partially_revealed():
    """짧은 키는 앞부분도 보여주지 않는다."""
    assert LLMConfig(api_key="short").masked_key == "<5자>"


def test_summary_reports_unconfigured_state():
    assert "미설정" in LLMConfig().summary()


# ---------------------------------------------------------------------------
# 4. 예산 가드 — 상한 강제
# ---------------------------------------------------------------------------


def test_defaults_come_from_contract():
    guard = BudgetGuard()
    assert guard.max_usd == thresholds.MAX_USD_PER_TASK
    assert guard.max_tokens == thresholds.MAX_TOKENS_PER_TASK
    assert guard.max_steps == thresholds.MAX_STEPS_PER_TASK


def test_check_passes_when_under_budget():
    BudgetGuard().check()  # 예외 없음


def test_cost_limit_blocks_further_calls():
    guard = BudgetGuard(max_usd=0.01)
    guard.record(prompt_tokens=100_000, completion_tokens=100_000, model="openai/gpt-4o")
    with pytest.raises(BudgetExceeded) as exc:
        guard.check()
    assert exc.value.kind == "비용"


def test_token_limit_blocks_further_calls():
    guard = BudgetGuard(max_tokens=100)
    guard.record(prompt_tokens=90, completion_tokens=20, model="openai/gpt-4o-mini")
    with pytest.raises(BudgetExceeded) as exc:
        guard.check()
    assert exc.value.kind == "토큰"


def test_step_limit_blocks_next_step():
    guard = BudgetGuard(max_steps=2)
    guard.begin_step()
    guard.begin_step()
    with pytest.raises(BudgetExceeded) as exc:
        guard.begin_step()
    assert exc.value.kind == "스텝"


def test_exceeded_message_contains_numbers():
    guard = BudgetGuard(max_tokens=10)
    guard.record(prompt_tokens=20, completion_tokens=0, model="openai/gpt-4o-mini")
    with pytest.raises(BudgetExceeded, match="토큰"):
        guard.check()


def test_actual_cost_overrides_estimate():
    """OpenRouter가 실제 과금액을 주면 추정치 대신 사용한다."""
    guard = BudgetGuard()
    cost = guard.record(
        prompt_tokens=1000,
        completion_tokens=1000,
        model="openai/gpt-4o-mini",
        actual_usd=0.123456,
    )
    assert cost == 0.123456
    assert guard.used_usd == 0.123456


def test_unknown_model_uses_conservative_pricing():
    """미등록 모델을 싸게 추정하면 상한을 넘겨도 통과한다."""
    known = estimate_cost("openai/gpt-4o-mini", 1_000_000, 0)
    unknown = estimate_cost("some/brand-new-model", 1_000_000, 0)
    assert unknown > known


def test_snapshot_reports_usage_and_limits():
    guard = BudgetGuard()
    guard.begin_step()
    guard.record(prompt_tokens=10, completion_tokens=5, model="openai/gpt-4o-mini")
    snap = guard.snapshot()
    assert snap["steps"] == 1
    assert snap["llm_calls"] == 1
    assert snap["tokens"] == 15
    assert snap["usd_limit"] == thresholds.MAX_USD_PER_TASK


def test_remaining_never_goes_negative():
    guard = BudgetGuard(max_usd=0.001, max_tokens=10)
    guard.record(prompt_tokens=1_000_000, completion_tokens=0, model="openai/gpt-4o")
    assert guard.remaining_usd == 0.0
    assert guard.remaining_tokens == 0


# ---------------------------------------------------------------------------
# 5. 응답 파싱
# ---------------------------------------------------------------------------


def _response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        model="test/model",
        prompt_tokens=1,
        completion_tokens=1,
        cost_usd=0.0,
    )


def test_parses_plain_json():
    assert _response('{"action": "click"}').parse_json() == {"action": "click"}


def test_strips_json_code_fence():
    """모델이 ```json 펜스를 붙이는 경우가 흔하다."""
    fenced = '```json\n{"action": "click", "element_id": "@e1"}\n```'
    assert _response(fenced).parse_json()["element_id"] == "@e1"


def test_strips_bare_code_fence():
    assert _response('```\n{"ok": true}\n```').parse_json() == {"ok": True}


def test_invalid_json_raises_with_snippet():
    with pytest.raises(LLMError, match="JSON 파싱 실패"):
        _response("이건 JSON이 아닙니다").parse_json()


def test_total_tokens_sums_both_sides():
    assert LLMResponse("", "m", 30, 12, 0.0).total_tokens == 42


# ---------------------------------------------------------------------------
# 6. 플레이스홀더 판정 — 미기입 .env를 "설정됨"으로 오인하면 안 된다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "sk-or-v1-여기에_실제_키를_넣으십시오",
        "your_api_key_here",
        "YOUR_KEY",
        "xxxxxxxx",
        "<키를 입력하세요>",
        "changeme",
        "sk-or-v1-example-key",
        "",
    ],
)
def test_placeholder_values_are_not_configured(value):
    assert LLMConfig(api_key=value).configured is False


@pytest.mark.parametrize(
    "value",
    [
        "sk-or-v1-a1b2c3d4e5f6708192a3b4c5d6e7f8091a2b3c4d5e6f70819",
        "sk-or-v1-0123456789abcdef0123456789abcdef",
    ],
)
def test_real_looking_keys_are_configured(value):
    assert LLMConfig(api_key=value).configured is True


def test_placeholder_state_is_distinguishable():
    """미설정과 플레이스홀더는 안내 문구가 달라야 한다."""
    empty = LLMConfig(api_key="")
    placeholder = LLMConfig(api_key="sk-or-v1-여기에_실제_키를_넣으십시오")

    assert empty.has_placeholder_key is False
    assert placeholder.has_placeholder_key is True
    assert "미설정" in empty.summary()
    assert "플레이스홀더" in placeholder.summary()


def test_placeholder_masked_key_prompts_replacement():
    masked = LLMConfig(api_key="your_key_here").masked_key
    assert "교체" in masked


def test_copied_example_file_is_not_treated_as_configured(tmp_path: Path):
    """`cp .env.example .env` 직후 상태를 정확히 잡아야 한다.

    실제로 이 경로에서 '설정됨'으로 오인되어 401을 받고서야 원인을
    알게 되는 사고가 있었다.
    """
    example = Path(".env.example")
    if not example.exists():
        pytest.skip(".env.example이 없습니다")
    copied = tmp_path / ".env"
    copied.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    assert load_config(copied).configured is False
