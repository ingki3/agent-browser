"""WS-75 R2 (#199 B3) — 끝 규칙(잘린 칸 끝의 등록 값 앞부분)은 producer 가 **실제로 자른** 칸에만(단위).

* 과잉 가림 0: 안 잘린 일반 글자가 우연히 자르기 길이(12·40·120·200, UTF-16 포함)이거나 말줄임으로 끝나도, 끝 낱말이
  등록 값 앞부분(`2026` ← `2026PrivateKey`)이면 원문 그대로 — 레시피도 저장된다(검증 B3 재현 short12·short40·emoji120).
* 출처 표시: producer(sanitizer·shadow·repeated·recipes.keys)가 자른 칸만 표시하고, 표시된 칸은 길이와 무관하게
  끝 규칙을 적용한다. 표시 정보는 출력 구조(RawElement 필드·대상 기술 desc 키)에 더하지 않는다.

값은 모두 합성 값.
"""

from __future__ import annotations

import asyncio
import dataclasses
import sys

import pytest

import security.public_url  # noqa: F401 - 모듈 적재
from perception import cut_marks
from security import secret_registry as sr
from recipes.store import RecipeError, compile_recipe

pu = sys.modules["security.public_url"]
R = "[REDACTED]"
V = "2026PrivateKey"
EMOJI = "\U0001F600"
PAD = "Ordinary shipment details "
#: 검증 parent-proof 의 안 잘린 일반 이름(원문 길이 = 자르기 길이) + 대조.
ORDINARY = {"short12": "Order # 2026", "short40": "Order details for ordinary shipment 2026",
            "emoji120": EMOJI + (PAD * 6)[:114] + "2026", "near11": "Order: 2026", "plain": "Activate ordinary",
            "value200": (PAD * 9)[:196] + "2026", "ellipsis": "Order list 2026…"}


@pytest.fixture(autouse=True)
def _clean():
    sr.clear()
    cut_marks.clear()
    yield
    sr.clear()
    cut_marks.clear()


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def test_case_lengths_are_cut_lengths():
    assert [len(ORDINARY[k]) for k in ("short12", "short40")] == [12, 40]
    assert _utf16(ORDINARY["emoji120"]) == 120 and len(ORDINARY["value200"]) == 200


@pytest.mark.parametrize("case", sorted(ORDINARY))
def test_uncut_ordinary_text_never_tail_masked(case):
    sr.register_url("http://127.0.0.1/register?password=" + V)
    text = ORDINARY[case]
    assert pu.content_text(text) is text
    summary = f'@e1 button "{text}"'
    assert pu.content_summary(summary) is summary


@pytest.mark.parametrize("case", sorted(ORDINARY))
def test_uncut_ordinary_recipe_saved(case):
    sr.register_url("http://127.0.0.1/register?password=" + V)
    name = ORDINARY[case]
    norm = " ".join(name.split()).lower()
    desc = {"role": "button", "name": name, "tag": "button", "sig": "s", "href_pat": None, "secret": False,
            "ui": {"group": "button", "name": norm, "landmark": "", "pos": [1, 2], "href_pat": None},
            "slot": None, "identity": None, "identity_refused": "없음"}
    entries = [{"action": "click", "args": {}, "desc": desc, "origin": "http://127.0.0.1:9", "url_pat": "/p",
                "skel": "k", "ready": 1, "expect": {"nav": "none", "url_pat": None, "signals": []}}]
    rec = compile_recipe("ordinary proof", entries)
    assert rec["steps"][0]["variants"][0]["target"]["name"] == norm


def test_marked_cut_text_tail_masked_at_any_length():
    """표시된(실제로 잘린) 칸은 길이가 자르기 길이가 아니어도(공백 접기·U+FFFD 대체 뒤) 끝 규칙을 적용한다."""
    sr.register_url("http://127.0.0.1/register?password=" + V)
    for text in ("Order # 2026", "Some heading text 2026Priv", "x" * 57 + " 2026Pr"):
        cut_marks.mark(text)
        shown = pu.content_text(text)
        assert shown.endswith(R) and "2026" not in shown, (text, shown)


def test_memo_follows_provenance_change():
    """같은 글자가 나중에 잘린 칸으로 표시되면 메모가 이전(끝 규칙 없음) 결과를 돌려주지 않는다."""
    sr.register_url("http://127.0.0.1/register?password=" + V)
    text = "Order # 2026"
    assert pu.content_text(text) is text
    cut_marks.mark(text)
    assert pu.content_text(text).endswith(R)


def test_marked_recipe_desc_name_refused_and_unmarked_saved():
    sr.register_secret_values(["n7q4m9Key81"])
    cut = "x" * 113 + " n7q4m9"
    norm = " ".join(cut.split()).lower()
    desc = {"role": "button", "name": cut, "tag": "button", "sig": "s", "href_pat": None, "secret": False,
            "ui": {"group": "button", "name": norm, "landmark": "", "pos": [1, 2], "href_pat": None},
            "slot": None, "identity": None, "identity_refused": "없음"}
    entries = [{"action": "click", "args": {}, "desc": desc, "origin": "http://127.0.0.1:9", "url_pat": "/p",
                "skel": "k", "ready": 1, "expect": {"nav": "none", "url_pat": None, "signals": []}}]
    compile_recipe("r", entries)  # 출처를 모른다 — 끝 규칙 없음(과잉 거부 방지)
    cut_marks.mark(cut)
    cut_marks.mark(norm)
    with pytest.raises(RecipeError):
        compile_recipe("r", entries)


def test_uncut_url_selector_param_names_no_tail_rule():
    """잘리지 않는 주소·url_pat·selector·params 이름은 끝 규칙 없음 — 전체 일치 대조(R1 B2)는 그대로."""
    sr.register_secret_values(["n7q4m9Key81"])
    entries = [{"action": "navigate", "args": {"url": "http://127.0.0.1:9/p/n7q4"}, "origin": "", "url_pat": None,
                "expect": {"nav": "load", "url_pat": "/p/n7q4"}}]
    compile_recipe("open n7q4", entries)
    with pytest.raises(RecipeError):
        compile_recipe("open n7q4m9key81", entries)


# ------------------------------------------------------------------ producer 표시


def test_sanitizer_marks_only_flagged_fields_and_keeps_raw_element_shape():
    from perception.sanitizer import RawElement, parse_collection

    fields = [f.name for f in dataclasses.fields(RawElement)]
    page = parse_collection({"title": "t", "url": "u", "total_dom_nodes": 3, "elements": [
        {"seq": 0, "role": "button", "name": "Order # 2026", "tag": "button", "css_path": "b", "value": None},
        {"seq": 1, "role": "button", "name": "cut name", "name_cut": True, "tag": "button", "css_path": "c",
         "value": "cut value", "value_cut": True},
        {"seq": 2, "role": "textbox", "name": "n", "tag": "input", "css_path": "d", "value": "Order # 2026"}]})
    assert [f.name for f in dataclasses.fields(RawElement)] == fields
    assert [cut_marks.is_cut(e.name) for e in page.elements] == [False, True, False]
    assert cut_marks.is_cut("cut value") and not cut_marks.is_cut("Order # 2026")


def test_repeated_short_and_compact_mark_only_real_cuts():
    from perception import repeated

    assert repeated._short("Order details for ordinary shipment 2026") == "Order details for ordinary shipment 2026"
    assert not cut_marks.is_cut("Order details for ordinary shipment 2026")
    long = "Order details for ordinary shipment 2026PrivateKey"
    shown = repeated._short(long)
    assert shown.endswith("…") and cut_marks.is_cut(shown)
    assert repeated._first_word("Order#2026xx more") == "Order#2026xx" and not cut_marks.is_cut("Order#2026xx")
    word = repeated._first_word("Order#2026Priv more")
    assert word == "Order#2026Pr" and cut_marks.is_cut(word)
    # 원천(sanitizer)이 잘린 이름 — 공백 접기 뒤에도 끝은 잘린 끝이다
    src = "a  " + "b" * 30 + " 2026Pr"
    cut_marks.mark(src)
    assert cut_marks.is_cut(repeated._short(src))
    one = "x2026Pr"
    cut_marks.mark(one)
    assert cut_marks.is_cut(repeated._first_word(one))


def test_repeated_heading_cut_list_marks_and_is_not_output():
    from perception import repeated

    marked = repeated._mark_page_cuts({"sets": [], "cut": ["Heading cut 2026Pr", 7, ""]})
    assert marked == {"sets": []} and cut_marks.is_cut("Heading cut 2026Pr")


def test_keys_snapshot_marks_and_strips_flags():
    from recipes import keys

    target = {"role": "button", "name": "N" * 120, "tag": "button", "sig": "s", "href_pat": None, "secret": False,
              "ui": {"group": "button", "name": "n" * 120, "landmark": "", "pos": [0, 0], "href_pat": None},
              "slot": None, "identity": {"by": "attr", "attr": "id", "value": "b", "group": "button",
                                         "tag": "button", "name": "n" * 120},
              "identity_refused": None, "name_cut": True, "norm_cut": True}
    plain = dict(target, name="Order # 2026", ui=dict(target["ui"], name="order # 2026"), identity=None,
                 name_cut=False, norm_cut=False)

    class Page:
        def __init__(self, t):
            self.t = t

        async def evaluate(self, js, arg):
            return {"url": "u", "skel": "k", "ready": 1, "target": dict(self.t)}

    snap = asyncio.run(keys.snapshot(Page(target), "#b"))
    assert "name_cut" not in snap["target"] and "norm_cut" not in snap["target"]
    assert cut_marks.is_cut("N" * 120) and cut_marks.is_cut("n" * 120)
    snap2 = asyncio.run(keys.snapshot(Page(plain), "#b"))
    assert set(snap2["target"]) == set(target) - {"name_cut", "norm_cut"}
    assert not cut_marks.is_cut("Order # 2026") and not cut_marks.is_cut("order # 2026")


def test_js_sources_report_cut_flags():
    """JS producer 가 실제 자름 여부를 보낸다 — 이름(accessibleName 내용 글자)·값·묶음 표제·레시피 기술."""
    from perception.repeated import REPEATED_JS
    from perception.sanitizer import ACCESSIBLE_NAME_JS, COLLECT_SCRIPT
    from perception.shadow_dom import _EXTRACT_FN
    from recipes.keys import KEYS_JS

    assert "accessibleName.cut = !!text && text.length > 120" in ACCESSIBLE_NAME_JS
    for js in (COLLECT_SCRIPT, _EXTRACT_FN):
        assert "name_cut" in js and "value_cut" in js
    assert "cut: Array.from(RP_CUT)" in REPEATED_JS
    assert "name_cut" in KEYS_JS and "norm_cut" in KEYS_JS
