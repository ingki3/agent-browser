"""WS-56 R5 — 재검증 FAIL(BLOCKING-1) 재현을 저장소 테스트로. 실제 Chromium(headless) + 로컬 http(127.0.0.1)만.

Enter 암묵 제출의 기본 제출자는 폼 소유 요소 트리 순서의 첫 submit 버튼이다 — `input type=image` 포함(Chromium 의
form.elements 는 image 를 빼므로 그 목록만 돌면 뒤의 일반 버튼을 승인 목적지로 잘못 보여 준다), form 속성으로 폼 밖에
연결된 제출자 포함. 첫 제출자가 비활성이면 암묵 제출이 없다(뒤 버튼으로 넘어가지 않음). 판별 불가면 결속하지 않는다
(버튼 승인·정책 제외 — R3 원칙).

원 재현: .hermes/verify/ws56-r3/evidence/binding/test_image_submitter.py · parent-image-recheck.log.
"""

from __future__ import annotations

from typing import Any

import pytest

from interface import handoff
from test_run_cli import requires_chromium
from test_ws56_r2_binding import _run, site  # noqa: F401 — site 는 pytest 픽스처

pytestmark = requires_chromium


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv(handoff.STATE_ROOT_ENV, str(tmp_path / "state"))


def _labels(hits: Any) -> list:
    return [h.split("b=")[1].split("&")[0] for h in hits]


def _destination(out: Any) -> str:
    return next((x for x in out["messages"][0].split("\n") if x.startswith("목적지: ")), "")


def _mutation(expr: str) -> Any:
    async def run(b: Any) -> None:
        await b._dispatcher.ctx.page.evaluate(expr)
    return run


# ------------------------------------------------------------------ image 기본 제출자 (검증 재현 3건)

_IMAGE = ('<!doctype html><meta charset=utf-8><p id=out>대기</p>'
          '<form method=post action="/clicked?b=form-default">'
          '<input id=pay aria-label="결제하기" value=x>'
          '<input id=submitter type=image name=confirm alt="결제하기" formaction="/clicked?b=alice" formtarget=_self>'
          '<button id=later formaction="/clicked?b=later-button">결제하기</button></form>')


@pytest.mark.parametrize("token", [False, True], ids=["elicit", "approval_id"])
async def test_image_default_submitter_mutation_is_not_run(site, token):
    site["html"] = _IMAGE
    out = await _run(site, "key", _mutation("document.querySelector('#submitter').formAction='/clicked?b=bob'"),
                     token=token)
    assert not out["hits"], out
    assert out["env"]["success"] is False and out["env"]["error_code"] == "E_TOCTOU_MISMATCH", out


async def test_unchanged_image_destination_is_shown(site):
    site["html"] = _IMAGE
    out = await _run(site, "key")
    assert out["env"]["success"] is True and _labels(out["hits"]) == ["alice"], out
    line = _destination(out)
    assert line.startswith("목적지: POST ") and line.endswith("/clicked?b=alice"), out


# ------------------------------------------------------------------ form 속성으로 폼 밖에 연결된 제출자

_OUTSIDE = ('<!doctype html><meta charset=utf-8><p id=out>대기</p>'
            '<input id=outer type=submit form=f value="결제하기" formaction="/clicked?b=outer">'
            '<form id=f method=post action="/clicked?b=form-default">'
            '<input id=pay aria-label="결제하기" value=x>'
            '<button id=later formaction="/clicked?b=later-button">결제하기</button></form>'
            # 폼 안에 있지만 다른 폼 소유(form=g) — f 의 제출자가 아니다.
            '<form id=g action="/clicked?b=other-form"></form>')


async def test_form_attribute_outside_submitter_is_default(site):
    site["html"] = _OUTSIDE
    out = await _run(site, "key")
    assert out["env"]["success"] is True and _labels(out["hits"]) == ["outer"], out
    assert _destination(out).endswith("/clicked?b=outer"), out


@pytest.mark.parametrize("token", [False, True], ids=["elicit", "approval_id"])
async def test_form_attribute_outside_submitter_mutation_is_not_run(site, token):
    site["html"] = _OUTSIDE
    out = await _run(site, "key", _mutation("document.querySelector('#outer').formAction='/clicked?b=bob'"),
                     token=token)
    assert not out["hits"], out
    assert out["env"]["success"] is False and out["env"]["error_code"] == "E_TOCTOU_MISMATCH", out


async def test_submitter_owned_by_other_form_is_skipped(site):
    """폼 안에 있어도 form 속성으로 다른 폼에 속한 버튼은 이 폼의 기본 제출자가 아니다."""
    site["html"] = ('<!doctype html><meta charset=utf-8><p id=out>대기</p>'
                    '<form id=f method=post action="/clicked?b=form-default">'
                    '<input id=pay aria-label="결제하기" value=x>'
                    '<button id=foreign form=g formaction="/clicked?b=foreign">결제하기</button>'
                    '<button id=mine formaction="/clicked?b=mine">결제하기</button></form>'
                    '<form id=g action="/clicked?b=other-form"></form>')
    out = await _run(site, "key")
    assert out["env"]["success"] is True and _labels(out["hits"]) == ["mine"], out
    assert _destination(out).endswith("/clicked?b=mine"), out


# ------------------------------------------------------------------ 비활성 첫 제출자 — 암묵 제출 없음

_DISABLED = ('<!doctype html><meta charset=utf-8><p id=out>대기</p>'
             '<form method=post action="/clicked?b=form-default">'
             '<input id=pay aria-label="결제하기" value=x>'
             '<button id=first disabled formaction="/clicked?b=alice">결제하기</button>'
             '<button id=later formaction="/clicked?b=later-button">결제하기</button></form>')


async def test_disabled_first_submitter_means_no_submission(site):
    """대조: 첫 제출자가 비활성이면 Chromium 은 제출하지 않는다 — 승인 문구도 뒤 버튼 목적지를 보이지 않는다."""
    site["html"] = _DISABLED
    out = await _run(site, "key")
    assert out["hits"] == [], out
    assert "later-button" not in out["messages"][0], out
    assert "alice" not in out["messages"][0], out


@pytest.mark.parametrize("token", [False, True], ids=["elicit", "approval_id"])
async def test_disabled_first_submitter_enabled_while_waiting_is_not_run(site, token):
    """승인 대기 중 비활성 첫 제출자를 켜면 '제출 없음' 이 '제출' 로 바뀐 것 — 실행하지 않는다."""
    site["html"] = _DISABLED
    out = await _run(site, "key", _mutation("document.querySelector('#first').disabled=false"), token=token)
    assert not out["hits"], out
    assert out["env"]["success"] is False and out["env"]["error_code"] == "E_TOCTOU_MISMATCH", out


# ------------------------------------------------------------------ 판별 불가 — 결속하지 않음(버튼 승인 제외)

# 버튼의 폼 소유자 getter 를 지운 페이지 — 포커스 입력칸의 폼(게이트의 고위험 판정)은 그대로 보이지만, 폼 소유 제출자
# 후보(뒤 버튼)의 소유 폼을 확인할 수 없다.
_UNDETERMINED = _IMAGE + "<script>delete HTMLButtonElement.prototype.form;</script>"


async def test_undetermined_default_submitter_is_not_elicited(site):
    site["html"] = _UNDETERMINED
    out = await _run(site, "key")
    assert out["messages"] == [], out
    assert out["env"]["success"] is False and not out["hits"], out


async def test_undetermined_default_submitter_token_is_refused(site):
    site["html"] = _UNDETERMINED
    out = await _run(site, "key", token=True)
    assert not out["hits"], out
    assert out["env"]["success"] is False and out["env"]["error_code"] == "E_TOCTOU_MISMATCH", out
