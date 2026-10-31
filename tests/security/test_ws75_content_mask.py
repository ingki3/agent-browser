"""WS-75 (#199, 사용자 결정 B) — 도구가 비밀로 아는 값은 페이지 내용 경로에서도 가린다(단위).

* 등급(계획 v4): 자격증명(SecretStore·민감칸·로그인·userinfo·password/otp 계열 키) = 4~7자 경계 규칙 포함,
  주소 비밀 = 내용 경로 ≥ 8 만, 비밀 키가 아닌 키 아래 순수 16진(ObjectId 류) = 내용 경로 원문.
* 내용 경로(data.items·observation.elements·axtree_summary·repeated)의 페이지 글자 칸만 — element_id·role·구조 키 불변.
* 레지스트리가 비었거나 응답에 등록 값이 없으면 바이트 동일.
* perception 이 먼저 자른 칸(…·자르기 길이)의 끝에 남은 등록 값 앞부분도 가린다(코디네이터 승인 B, 2026-10-31).
* 레시피: 저장 문자열에 등록 값이 있으면 저장 거부.

값은 모두 합성 값.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from contracts import ActionResult, ActionType
import security.public_url  # noqa: F401 - 모듈 적재(아래 sys.modules 조회)
import sys

from security import secret_registry as sr
from security.public_url import graded_secret_values, public_payload

pu = sys.modules["security.public_url"]  # `security.public_url` 이름은 패키지가 함수로 재수출한다
R = "[REDACTED]"
SRC = Path(__file__).resolve().parents[2] / "src"


@pytest.fixture(autouse=True)
def _clean():
    sr.clear()
    yield
    sr.clear()


def _wire(payload):
    return json.dumps(public_payload(payload), ensure_ascii=False, separators=(",", ":"))


def _obs_payload(text):
    return {"current_url": "http://127.0.0.1/p",
            "data": {"items": [{"text": text, "href": "/n?x=" + text}],
                     "observation": {"title": "t", "url": "http://127.0.0.1/p",
                                     "elements": [{"element_id": "@e1", "role": "link", "name": text, "value": text}],
                                     "axtree_summary": f'@e1 link "{text}"'},
                     "repeated": {"how": "h", "groups": [{"group": text, "group_source": "text", "x": 1, "y": 2,
                                                          "members": [{"id": "@e1", "role": "link", "name": text}]}]}}}


# ------------------------------------------------------------------ 등급(수집 지점별 표)


def test_grades_per_collection_point():
    H, U, C = sr.GRADE_HEURISTIC, sr.GRADE_URL, sr.GRADE_CREDENTIAL
    hex24 = "507f1f77bcf86cd799439011"
    assert graded_secret_values("http://h/o?id=" + hex24) == [(hex24, H)]          # 비밀 키 아님 + 순수 16진
    assert graded_secret_values("http://h/o?key=2024") == [("2024", U)]            # 일반 비밀 키
    assert graded_secret_values("http://h/o?promoCode=SALE2024") == [("SALE2024", U)]
    assert graded_secret_values("http://h/o?password=abc123") == [("abc123", C)]   # 자격증명 키(N3)
    assert graded_secret_values("http://h/o?otp=123456") == [("123456", C)]
    assert graded_secret_values("http://h/o#otp=123456") == [("123456", C)]
    assert graded_secret_values("http://h/o?new_password=ab12cd") == [("ab12cd", C)]
    assert graded_secret_values("http://u:pw12ab@h/o") == [("pw12ab", C)]          # userinfo
    assert graded_secret_values("http://h/o?t=" + "Zq9" * 9) == [("Zq9" * 9, U)]   # 비밀 키 아님 + 섞인 토큰
    assert graded_secret_values("http://h/o?" + hex24) == [(hex24, H)]             # 키 없는 값
    assert graded_secret_values("http://h/o#" + hex24) == [(hex24, H)]             # 조각
    assert graded_secret_values("http://h/o?token=" + hex24) == [(hex24, U)]       # 비밀 키면 16진이어도 주소 비밀
    assert pu.secret_values("http://h/o?otp=123456") == ["123456"]                 # 기존 함수는 값만


def test_regrade_keeps_higher():
    sr.REGISTRY.add_values(["SYNTHregrade9"], sr.GRADE_HEURISTIC)
    assert pu.content_text("x SYNTHregrade9") == "x SYNTHregrade9"
    v = sr.version()
    sr.REGISTRY.add_values(["SYNTHregrade9"], sr.GRADE_CREDENTIAL)
    assert sr.version() == v + 1
    assert pu.content_text("x SYNTHregrade9") == f"x {R}"
    sr.REGISTRY.add_values(["SYNTHregrade9"], sr.GRADE_URL)  # 낮은 등급 재등록은 그대로
    assert sr.version() == v + 1 and pu.content_text("x SYNTHregrade9") == f"x {R}"


# ------------------------------------------------------------------ 등급 fixture(본문 반사)


def test_objectid_body_stays_raw():
    hex24 = "507f1f77bcf86cd799439011"
    sr.register_url("http://h/order?id=" + hex24)
    out = public_payload(_obs_payload("주문번호 " + hex24))
    assert out["data"]["items"][0]["text"] == "주문번호 " + hex24
    assert out["data"]["observation"]["elements"][0]["name"] == "주문번호 " + hex24
    assert out["current_url"] == "http://127.0.0.1/p"
    # 주소 칸·자유 문장은 예전대로 가린다
    assert hex24 not in json.dumps(public_payload({"current_url": "http://h/order?id=" + hex24,
                                                   "data": {"observation": {"title": "t " + hex24}}}))


def test_short_url_key_value_body_stays_raw():
    sr.register_url("http://h/p?key=2024")
    out = public_payload(_obs_payload("2024-10 결산"))
    assert out["data"]["items"][0]["text"] == "2024-10 결산"


def test_promocode_body_masked():
    sr.register_url("http://h/p?promoCode=SALE2024")
    out = public_payload(_obs_payload("쿠폰 SALE2024 적용"))
    assert "SALE2024" not in json.dumps(out, ensure_ascii=False)
    assert out["data"]["items"][0]["text"] == f"쿠폰 {R} 적용"


@pytest.mark.parametrize("url,value", [("http://h/p?password=abc123", "abc123"), ("http://h/p?otp=123456", "123456"),
                                       ("http://u:pw12ab@h/p", "pw12ab")])
def test_short_credentials_in_url_masked_in_body(url, value):
    sr.register_url(url)
    out = public_payload(_obs_payload(f"code: {value}."))
    assert value not in json.dumps(out, ensure_ascii=False)
    assert out["data"]["observation"]["elements"][0]["name"] == f"code: {R}."


def test_six_digit_otp_credential_masked_with_boundary_rule():
    sr.register_secret_values(["482913"])
    out = public_payload(_obs_payload("인증번호 482913 입력"))
    assert out["data"]["items"][0]["text"] == f"인증번호 {R} 입력"
    # 경계 규칙: 앞뒤가 영숫자면 남는다(과잉 가림 방지 — 기존 4~7자 규칙 그대로)
    assert pu.content_text("id9482913x") == "id9482913x"


def test_url_grade_short_value_not_masked_but_credential_is():
    sr.REGISTRY.add_values(["7391"], sr.GRADE_URL)
    assert pu.content_text("방 7391 호") == "방 7391 호"
    sr.REGISTRY.add_values(["8402"], sr.GRADE_CREDENTIAL)
    assert pu.content_text("PIN 8402") == f"PIN {R}"


# ------------------------------------------------------------------ 칸 범위·구조 키


def test_only_page_text_fields_masked():
    v = "SYNTHfield1234"
    sr.register_secret_values([v])
    payload = _obs_payload(v)
    payload["data"]["observation"]["elements"][0]["role"] = v
    payload["data"]["repeated"]["groups"][0]["group_source"] = v
    out = public_payload(payload)
    el = out["data"]["observation"]["elements"][0]
    assert el["element_id"] == "@e1" and el["role"] == v      # 구조 칸은 그대로(NB3)
    assert el["name"] == R and el["value"] == R
    g = out["data"]["repeated"]["groups"][0]
    assert g["group"] == R and g["members"][0]["name"] == R and g["group_source"] == v
    assert g["members"][0]["id"] == "@e1" and out["data"]["repeated"]["how"] == "h"
    assert out["data"]["items"][0] == {"text": R, "href": "/n?x=" + R}
    assert out["data"]["observation"]["axtree_summary"] == f'@e1 link "{R}"'


def test_unregistered_page_text_untouched():
    sr.register_secret_values(["SYNTHknown1234"])
    text = "api_key=sk_live_PATTERN password=HUNTER2SYNTH synth.user@example.com 4111 1111 1111 1111"
    out = public_payload(_obs_payload(text))
    assert out["data"]["items"][0]["text"] == text
    assert out["data"]["observation"]["elements"][0]["name"] == text


def test_content_mask_idempotent():
    sr.register_secret_values(["SYNTHidem1234", "4821"])
    once = public_payload(_obs_payload("a SYNTHidem1234 b 4821 c"))
    twice = public_payload(once)
    assert once == twice


def test_input_not_mutated():
    sr.register_secret_values(["SYNTHkeep1234"])
    payload = _obs_payload("SYNTHkeep1234")
    before = json.dumps(payload)
    public_payload(payload)
    pu.content_view(payload["data"])
    assert json.dumps(payload) == before


# ------------------------------------------------------------------ 바이트 동일


def test_byte_identical_when_registry_empty_or_value_absent():
    payloads = [_obs_payload(f"page {i} text /a?b={i} SYNTH{i}") for i in range(12)]
    base = [_wire(p) for p in payloads]
    for p in payloads:  # 비었을 때 내용 subtree 는 같은 객체(공유 — 복사 비용 0)
        assert public_payload(p)["data"]["items"] is p["data"]["items"]
    sr.register_secret_values(["SYNTHabsentAAAA"])
    sr.register_url("http://h/?token=SYNTHabsentBBBB")
    assert [_wire(p) for p in payloads] == base


# ------------------------------------------------------------------ 잘린 칸의 끝(perception 사전 자르기)


def _cut_sources():
    return {
        "compact": int(re.search(r"^COMPACT_WORD = (\d+)", (SRC / "perception/repeated.py").read_text(), re.M)[1]),
        "name_limit": int(re.search(r"^NAME_LIMIT = (\d+)", (SRC / "perception/repeated.py").read_text(), re.M)[1]),
        "text": int(re.search(r"text\.slice\(0, (\d+)\)", (SRC / "perception/sanitizer.py").read_text())[1]),
        "value": {int(x) for f in ("sanitizer.py", "shadow_dom.py")
                  for x in re.findall(r"String\(el\.value\)\.slice\(0, (\d+)\)", (SRC / "perception" / f).read_text())},
    }


def test_cut_lengths_match_perception_sources():
    src = _cut_sources()
    assert len(src["value"]) == 1
    assert pu.CONTENT_CUT_LENGTHS == {src["compact"], src["name_limit"], src["text"], *src["value"]}


@pytest.mark.parametrize("limit", sorted(pu.CONTENT_CUT_LENGTHS))
def test_cut_tail_prefix_masked_every_position(limit):
    """원문을 limit 자에서 자른 칸(말줄임 없음) — 등록 값이 경계를 걸치는 모든 위치에서 앞부분(≥4자)이 남지 않는다."""
    v = "SYNTHcutvalue77"
    sr.register_secret_values([v])
    for start in range(max(0, limit - len(v) + 1), limit):
        raw = ("w " * limit)[:start] + v + " tail"
        cut = raw[:limit]
        shown = pu.content_text(cut)
        kept = limit - start
        if kept >= 4:
            assert v[:kept] not in shown, (limit, start, shown)
            assert shown.endswith(R)
        assert shown.startswith(cut[:start].rstrip(" ")[:max(0, start - 1)]) or start <= 1


def test_ellipsis_tail_prefix_masked_every_position():
    """display_safe(…, 40) 처럼 말줄임으로 끝나는 칸 — 표제·묶음 이름."""
    from interface.handoff import display_safe

    v = "SYNTHheadvalue88"
    sr.register_url("http://h/p?token=" + v)
    for start in range(0, 45):
        cut = display_safe(" ".join(("가" * start + v + " 뒤쪽 글자 이어짐").split()), 40)
        shown = pu.content_text(cut)
        for k in range(4, len(v)):
            assert v[:k] + "…" not in shown and not shown.rstrip("…").endswith(v[:k]), (start, shown)


def test_cut_tail_unrelated_byte_identical_and_short_value_boundary():
    sr.register_secret_values(["SYNTHother9999", "7a3k9"])
    for limit in sorted(pu.CONTENT_CUT_LENGTHS):
        cut = ("평범한 글자 SYNT" * 40)[:limit - 4] + "abcd"
        assert pu.content_text(cut) == cut
    # 4~7자 자격증명 앞부분은 앞 글자가 영숫자가 아닐 때만
    assert pu.content_text(("x" * 115) + " 7a3k") == ("x" * 115) + " " + R
    assert pu.content_text(("x" * 115) + "z7a3k") == ("x" * 115) + "z7a3k"


def test_tail_rule_respects_grades():
    hex24 = "507f1f77bcf86cd799439011"
    sr.register_url("http://h/o?id=" + hex24)  # 휴리스틱 — 내용 경로 대상 아님
    cut = ("w" * 100) + " " + hex24[:19]
    assert len(cut) == 120 and pu.content_text(cut) == cut


# ------------------------------------------------------------------ 파이프라인 문맥(한 번 가린 사본)


def test_content_masked_context_skips_rescan_only_for_same_version():
    v = "SYNTHctx12345"
    sr.register_secret_values([v])
    data = pu.content_view(_obs_payload(v)["data"])
    assert v not in json.dumps(data)
    raw = _obs_payload(v)
    with pu.content_masked(sr.version()):
        shared = public_payload(raw)
        assert shared["data"]["items"] is raw["data"]["items"]  # 같은 version — 다시 훑지 않는다(공유)
        sr.register_secret_values(["SYNTHnewer6789"])            # version 이 바뀌면 다시 가린다
        assert v not in json.dumps(public_payload(raw))


def test_content_view_shallow_copies_repeated_when_empty():
    payload = _obs_payload("plain")
    view = pu.content_view(payload["data"])
    view["repeated"]["groups"].pop()
    view["repeated"]["omitted"] = 1
    assert len(payload["data"]["repeated"]["groups"]) == 1 and "omitted" not in payload["data"]["repeated"]
    assert view["items"] is payload["data"]["items"]


def test_envelope_dict_masks_content_of_raw_result():
    from interface.server_envelope import envelope_json

    v = "SYNTHenvelope77"
    sr.register_secret_values([v])
    r = ActionResult(success=True, action=ActionType.EXTRACT, current_url="http://127.0.0.1/", snapshot_epoch=1,
                     tab_id="tab-1", retry_safe=True, data={"items": [{"text": "key " + v}]})
    assert v not in envelope_json(r)
    assert json.loads(envelope_json(r))["data"]["items"][0]["text"] == "key " + R


# ------------------------------------------------------------------ 레시피


def test_recipe_step_with_registered_value_refused():
    from recipes.store import RecipeError, compile_recipe

    v = "SYNTHrecipe4321"
    entry = {"action": "click", "args": {}, "origin": "http://127.0.0.1:1", "url": "http://127.0.0.1:1/p",
             "url_pat": "/p", "skel": "s", "ready": 3, "secret": False,
             "desc": {"ui": {"role": "button", "name": "copy key " + v, "pos": [0, 0]}},
             "expect": {"nav": "none", "signals": []}}
    clean = dict(entry, desc={"ui": {"role": "button", "name": "copy key", "pos": [0, 0]}})
    compile_recipe("ok", [clean])  # 등록 전 — 저장된다
    compile_recipe("ok", [entry])
    sr.register_secret_values([v])
    with pytest.raises(RecipeError, match="비밀로 아는 값"):
        compile_recipe("x", [entry])
    compile_recipe("ok", [clean])
