"""WS-75 R1 (#199) — 검증 BLOCKING 2건의 단위 회귀 시험. 값은 모두 합성 값.

* B1: perception 의 JS 사전 자르기는 UTF-16 단위다. 앞에 이모지(서로게이트 쌍)가 있으면 JS 120/200/40 자 결과가
  Python 으로는 119/199/39 자 — 그래도 끝에 남은 등록 값 앞부분을 가린다. 반쪽 서로게이트로 잘린 칸도 처리한다.
  대조: 이모지가 섞인 모르는 글자는 바이트 동일.
* B2: 레시피 저장 대조는 정규화(소문자화) 전 원문과, 정규화 뒤 문자열의 casefold 형태 둘 다 — 형제 저장 경로
  (target ui·identity·slot, args, url_pat, params, 이름) 전수. 대조: 일반 단계는 저장된다.
"""

from __future__ import annotations

import sys

import pytest

import security.public_url  # noqa: F401 - 모듈 적재
from security import secret_registry as sr
from recipes.store import RecipeError, compile_recipe

pu = sys.modules["security.public_url"]
R = "[REDACTED]"
V = "Z9vRUnicodeCanary5731"
E = "\U0001F600"  # 서로게이트 쌍(UTF-16 2단위)


@pytest.fixture(autouse=True)
def _clean():
    sr.clear()
    yield
    sr.clear()


def _js_slice(text: str, limit: int) -> str:
    """JS `String.prototype.slice(0, limit)` — UTF-16 단위(반쪽 서로게이트는 짝 없는 서로게이트로 남는다)."""
    units = text.encode("utf-16-le", "surrogatepass")[:2 * limit]
    return units.decode("utf-16-le", "surrogatepass")


# ------------------------------------------------------------------ B1: UTF-16 경계


@pytest.mark.parametrize("limit,astral", [(lim, n) for lim in sorted(pu.CONTENT_CUT_LENGTHS) for n in (0, 1, 2, 5)
                                          if 2 * n + 6 <= lim])  # 접두가 자르기 길이 안에 드는 조합만
def test_b1_utf16_cut_tail_masked(limit, astral):
    """앞에 이모지 astral 개 + 등록 값이 JS 자르기 경계를 걸침 — 남은 앞부분(≥4)이 보이지 않는다."""
    sr.register_secret_values([V])
    for kept in range(4, len(V)):
        pad = limit - 2 * astral - kept
        if pad < 1:
            continue
        cut = _js_slice(E * astral + "y" * (pad - 1) + " " + V + " tail", limit)
        assert cut.endswith(V[:kept])
        shown = pu.content_text(cut)
        assert V[:kept] not in shown, (limit, astral, kept, shown)
        assert shown.endswith(R) and shown.startswith(E * astral)


def test_b1_half_surrogate_cut_does_not_crash_and_masks_nothing_else():
    sr.register_secret_values([V])
    cut = _js_slice("y" * 119 + E + V, 120)  # 이모지 한가운데서 잘림 — 짝 없는 서로게이트로 끝난다
    assert cut[-1] == "\ud83d"
    assert pu.content_text(cut) == cut
    # 반쪽 서로게이트 뒤가 아니라 앞에 등록 값 앞부분이 걸친 경우(JS 199단위 + 반쪽 = 200)
    cut2 = _js_slice(E + "y" * 190 + " " + V[:5] + E, 200)
    assert pu.content_text(cut2) == cut2  # 끝 조각이 등록 값 앞부분이 아니다(반쪽 서로게이트) — 원문


def test_b1_emoji_unregistered_byte_identical():
    """대조: 이모지가 섞인 모르는 글자(같은 자르기 길이)는 그대로 — 같은 객체."""
    sr.register_secret_values([V, "SYNTHother9999"])
    for limit in sorted(pu.CONTENT_CUT_LENGTHS):
        for astral in (1, 2):
            cut = _js_slice(E * astral + "평범한 글자 Z9vX 아님 " * 30, limit)
            assert pu.content_text(cut) is cut
            cut2 = _js_slice(E * astral + "w" * 300, limit - 4) + "Z9vq"
            assert pu.content_text(cut2) is cut2


def test_b1_summary_line_uses_same_rule():
    sr.register_secret_values([V])
    name = _js_slice(E + "y" * 113 + " " + V, 120)
    shown = pu.content_summary(f'@e1 button "{name}"')
    assert V[:5] not in shown and shown.endswith(R + '"')


# ------------------------------------------------------------------ B2: 레시피 저장 대조


def _desc(name: str, *, slot=None, identity=None, href_pat=None):
    norm = " ".join(name.split()).lower()
    return {"role": "button", "name": name[:120], "tag": "button", "sig": "s", "href_pat": href_pat, "secret": False,
            "ui": {"group": "button", "name": norm, "landmark": "", "pos": [1, 2], "href_pat": href_pat},
            "slot": slot, "identity": identity, "identity_refused": None if identity else "없음"}


def _entries(desc, *, action="click", args=None, url_pat="/p", nav_url="http://127.0.0.1:9/p"):
    return [{"action": "navigate", "args": {"url": nav_url}, "origin": "", "url_pat": None,
             "expect": {"nav": "load", "url_pat": url_pat}},
            {"action": action, "args": dict(args or {}), "desc": desc,
             "origin": "http://127.0.0.1:9", "url_pat": url_pat, "skel": "k", "ready": 1,
             "expect": {"nav": "none", "url_pat": None, "signals": []}}]


def _refused(entries, **kw):
    with pytest.raises(RecipeError):
        compile_recipe("r", entries, **kw)


def test_b2_lowercased_target_name_refused_ui():
    """`Activate n7q4m9Key81` → 저장 문자열 `activate n7q4m9key81`(검증 재현) — 거부."""
    sr.register_secret_values(["n7q4m9Key81"])
    _refused(_entries(_desc("Activate n7q4m9Key81")))


@pytest.mark.parametrize("value", ["K7vR_API_91%+&<synth>", "U8pQ71", "P6wZ92", "N5xK_normal91", "ZqVxCanaryabcdefgh91",
                                   "L9rT_user66", "W4sM_password99", "G2bV_sensitive83"])
def test_b2_mixed_case_values_refused_every_target_kind(value):
    sr.register_secret_values([value])
    low = value.lower()
    _refused(_entries(_desc("Activate " + value)))                                    # ui.name(정규화)
    ident = {"by": "attr", "attr": "id", "value": "x", "group": "button", "tag": "button", "name": "activate " + low}
    _refused(_entries(_desc("Activate " + value, identity=ident)), pins={1: "identity"})    # identity.name
    slot = {"chain": "c", "csel": "ul." + low, "itpl": "t", "ordinal": 0, "rel": "", "tsig": "s", "role": "button",
            "href_pat": None}
    _refused(_entries(_desc("ok", slot=slot)))                                          # slot.csel(정규화 형태만)


def test_b2_raw_name_before_whitespace_normalization_refused():
    """정규화(공백 접기·소문자화)로 등록 값 모양이 사라지는 경우 — 정규화 **전** 원문(desc.name)으로 대조한다."""
    v = "Qx7  Zr91 pass"
    sr.register_secret_values([v])
    desc = _desc("Activate " + v)
    assert v.casefold() not in desc["ui"]["name"]  # 정규화 뒤 문자열만으로는 찾을 수 없다(시험 조건)
    _refused(_entries(desc))


def test_b2_truncated_prefix_crossing_refused():
    """원문 이름이 120 자에서 잘려 등록 값 앞부분(≥4)만 남은 경우도 저장하지 않는다."""
    sr.register_secret_values(["n7q4m9Key81"])
    cut = "x" * 113 + " n7q4m9"  # JS slice(0, 120) 결과 길이
    assert len(cut) == 120
    _refused(_entries(_desc(cut)))
    # 대조: 잘리지 않은 짧은 이름이 우연히 등록 값 앞부분으로 끝나도 저장된다(끝 규칙은 잘린 칸만)
    rec = compile_recipe("r", _entries(_desc("주문 n7q4")))
    assert rec["steps"][1]["variants"][0]["target"]["name"] == "주문 n7q4"


def test_b2_sibling_paths_refused():
    v = "SyNthPathVal42x"
    sr.register_secret_values([v])
    # 정규화된 url_pat(호스트 소문자화 등)
    _refused(_entries(_desc("ok"), url_pat="/p/" + v.lower()))
    # type_text 인자(params 아닌 원문)
    _refused(_entries(_desc("Field"), action="type_text", args={"text": v.upper()}))
    # href_pat
    _refused(_entries(_desc("ok", href_pat="/go/" + v.lower())))
    # 레시피 이름(파일에 그대로 남는다)
    with pytest.raises(RecipeError):
        compile_recipe("open " + v.lower(), _entries(_desc("ok")))


def test_b2_controls_ordinary_step_saved_and_unregistered_kept():
    sr.register_secret_values(["n7q4m9Key81", "U8pQ71"])
    rec = compile_recipe("r", _entries(_desc("Activate ordinary")))
    assert rec["steps"][1]["variants"][0]["target"]["name"] == "activate ordinary"
    # 4~7자 자격증명 경계 규칙: 영숫자에 붙은 조각은 다른 낱말 — 저장된다
    rec2 = compile_recipe("r", _entries(_desc("Activate xu8pq71y")))
    assert rec2["steps"][1]["variants"][0]["target"]["name"] == "activate xu8pq71y"
    # 등급: 주소 비밀(≥ 8 만) 4~7 자는 내용 규칙상 대상 아님
    sr.clear()
    sr.register_url("http://127.0.0.1/o?token=AbC123")
    rec3 = compile_recipe("r", _entries(_desc("Activate abc123")))
    assert rec3["steps"][1]["variants"][0]["target"]["name"] == "activate abc123"
