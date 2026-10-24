"""WS-62 R15: changed 의 숫자를 모두 '#' 로 (사용자 결정 A, R14 재검증 B1 FAIL, #136).

R12~R14 세 라운드 연속 숫자 가림 규칙(사슬·잔여·단위 예외)이 새 변형('010만7394원8261명', 숫자 사이 결합문자)으로 뚫렸다.
결정 A: changed 의 text·items 쌍에는 숫자 성격 문자를 하나도 내보내지 않는다 — NFKC 전 원문 또는 NFKC 뒤 유니코드 범주
Nd·Nl·No 인 글자는 전부 '#'. 숫자 뒤 결합문자·보이지 않는 글자는 지운다. '#' 연속(길이)은 둔다.

오라클은 제품 코드를 쓰지 않는다: 출력 문자열과 그 NFKC 양쪽에서 범주 N* 글자 0개.
재현은 검증 evidence/independent_variants.py·test_independent_repros.py(실제 DOM → MCP call_tool → 공개 봉투)를 옮겼다.
모든 번호는 합성값이다.
"""

from __future__ import annotations

import html
import itertools
import json
import random
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from contracts import ErrorCode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "actions"))
from test_scroll_navigation import requires_chromium  # noqa: E402,F401
from test_ws62_r1_server import _serve  # noqa: E402

LIMIT = 80

# ------------------------------------------------------------------ 독립 오라클


def _numeric(s: str) -> List[str]:
    """s 와 NFKC(s) 에서 범주 N*(Nd·Nl·No) 글자."""
    return [c for t in (s, unicodedata.normalize("NFKC", s)) for c in t if unicodedata.category(c)[0] == "N"]


def _strings(ch: Optional[Dict[str, Any]]) -> List[str]:
    if not ch:
        return []
    out = [x[k] for x in ch.get("text", []) for k in ("before", "after")]
    return out + ([ch["items"][k] for k in ("before", "after")] if ch.get("items") else [])


def _bad(ch: Optional[Dict[str, Any]]) -> List[str]:
    """text·items 쌍의 숫자 성격 글자(오라클)."""
    return [c for s in _strings(ch) for c in _numeric(s)]


def _shown(ch: Optional[Dict[str, Any]], path: str) -> bool:
    return bool(ch and (ch.get("items") if path == "seq" else ch.get("text")))


def _assert_shape(ch: Optional[Dict[str, Any]]) -> None:
    if not ch:
        return
    for p in list(ch.get("text", [])) + ([ch["items"]] if "items" in ch else []):
        assert p["before"] != p["after"], p
        for s in (p["before"], p["after"]):
            assert len(s) <= LIMIT, (len(s), s)
            s.encode("utf-8")


def _pin(outer: List[str], seq: List[str], k: int = 0) -> str:
    return "\n".join(outer + ["S:" + "\x01".join(seq), f"#{k}"])


def _parts(old: str, new: str) -> Optional[Dict[str, Any]]:
    from actions.group_changed import changed_parts

    return changed_parts(old, new)


def _pair(path: str, payload: str, offset: int = 0) -> Optional[Dict[str, Any]]:
    """검증 independent_variants.pin 과 같은 배치: '문의 전화 <payload> <가*offset>이전' → '…이후'."""
    def pin(after: bool) -> str:
        text = "문의 전화 " + payload + " " + "가" * offset + ("이후" if after else "이전")
        return _pin([text] if path == "outer" else [], ["t:" + text] if path == "seq" else ["i:선택", "i:확인", "i:보관"])

    return _parts(pin(False), pin(True))


# ------------------------------------------------------------------ 검증 독립 변형(233종 — 단위·결합문자·구분자·각 문자계 숫자)

RAW = "01073948261"


def _trans(chars: str) -> str:
    return "".join(chars[int(x)] for x in RAW)


ALPHABETS = ["٠١٢٣٤٥٦٧٨٩", "۰۱۲۳۴۵۶۷۸۹", "०१२३४५६७८९", "০১২৩৪৫৬৭৮৯", "๐๑๒๓๔๕๖๗๘๙", "၀၁၂၃၄၅၆၇၈၉",
             "𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗", "０１２３４５６７８９"]
MARKS = ["\u0301", "\u0338", "\u034f", "\u0488", "\u20e3", "\ufe0f"]
VARIANTS: Dict[str, str] = {
    "unit-given": "010만7394원8261", "unit-all": "010만7394원8261명",
    "letter-given": "010a7394b8261", "letter-units": "010a7394원8261명",
    "mixed-roman": "zero10a7394원8261명", "mixed-korean": "공일공7394원8261명",
    "superscript": _trans("⁰¹²³⁴⁵⁶⁷⁸⁹"), "subscript": _trans("₀₁₂₃₄₅₆₇₈₉"), "circled": _trans("⓪①②③④⑤⑥⑦⑧⑨"),
    "dingbat": _trans("⓿❶❷❸❹❺❻❼❽❾"), "roman": "ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ",
}
for _a in ALPHABETS:
    VARIANTS[f"alphabet-{ord(_a[0]):x}"] = _trans(_a)
    for _p in range(len(RAW)):
        VARIANTS[f"mixed-nd-{ord(_a[0]):x}-{_p}"] = RAW[:_p] + _a[int(RAW[_p])] + RAW[_p + 1:]
for _u in "만원일월년명개박시분":
    VARIANTS["unit-" + _u] = "010" + _u + "7394" + _u + "8261" + _u
    for _x, _y in [(1, 4), (2, 6), (3, 8), (5, 9)]:
        VARIANTS[f"unit-splits-{_u}-{_x}-{_y}"] = RAW[:_x] + _u + RAW[_x:_y] + _u + RAW[_y:] + _u
for _m in MARKS:
    VARIANTS[f"combining-{ord(_m):x}"] = _m.join(RAW)
    for _p in range(len(RAW)):
        VARIANTS[f"single-mark-{ord(_m):x}-{_p}"] = RAW[:_p + 1] + _m + RAW[_p + 1:]
for _s in ["a", "가", "Ⅰ", "壹", "\u200b", "\u2060"]:
    VARIANTS[f"separator-{ord(_s):x}"] = "010" + _s + "7394" + _s + "8261"
assert len(set(VARIANTS.values())) == len(VARIANTS) >= 230


@pytest.mark.parametrize("path", ["seq", "outer"])
def test_independent_variants_zero_numeric_chars(path):
    """검증 독립 변형 전부 × 간격 5종 — 출력에 숫자 성격 글자 0, 그리고 바뀐 부분(이전→이후)은 매번 보인다(전부 생략 통과 금지)."""
    bad, hidden = [], []
    for (name, payload), offset in itertools.product(VARIANTS.items(), (0, 7, 19, 40, 79)):
        ch = _pair(path, payload, offset)
        if _bad(ch):
            bad.append((name, offset, _strings(ch)))
        if not _shown(ch, path):
            hidden.append((name, offset, ch))
        _assert_shape(ch)
    assert bad == [], (len(bad), bad[:5])
    assert hidden == [], (len(hidden), hidden[:5])


def test_unit_and_combining_bypass_shows_hashes():
    """검증 B1 두 우회: 단위 끼움·숫자 사이 결합문자 — 숫자는 '#'(길이 유지), 숫자 뒤 결합문자는 지운다."""
    ch = _pair("seq", "010만7394원8261명")
    assert ch["items"]["before"].endswith("전화 ###만####원####명 이전"), ch
    ch = _pair("outer", "\u0301".join(RAW))
    (p,) = ch["text"]
    assert p["before"].endswith("전화 ########### 이전") and p["after"].endswith("이후"), p
    assert not any(unicodedata.category(c)[0] == "M" for s in _strings(ch) for c in s), ch


# ------------------------------------------------------------------ 범주별 고정 표본(주입이 잡히는 곳)


@pytest.mark.parametrize("char,why", [
    ("❶", "No — NFKC 분해 없음(원문자)"), ("⓿", "No — NFKC 분해 없음"), ("➀", "No — NFKC 분해 없음"),
    ("²", "No — 상첨자"), ("₃", "No — 하첨자"), ("①", "No — 원문자"), ("½", "No — 분수"),
    ("٣", "Nd — 아랍-인도"), ("３", "Nd — 전각"), ("𝟕", "Nd — 수학"),
    ("Ⅻ", "Nl — NFKC 가 'XII' 로 바꿈(NFKC 전 판정)"), ("ⅰ", "Nl — NFKC 가 'i'"), ("〇", "Nl"),
    ("㋀", "So — NFKC 뒤 '1月'(NFKC 뒤 판정)"), ("⒈", "No"),
])
@pytest.mark.parametrize("path", ["seq", "outer"])
def test_numeric_category_char_becomes_hash(char, why, path):
    ch = _pair(path, "가" + char + "나")
    s = _strings(ch)
    assert s and not _bad(ch), (why, s)
    assert "가#" in s[0] and "#나" in s[0].replace("月", ""), (why, s)
    if char in "Ⅻⅰ":
        assert "XII" not in s[0] and "가i" not in s[0], (why, s)


def test_hash_runs_keep_length_and_identical_pairs_omitted():
    """'#' 연속은 그대로(길이 정보 허용). 숫자만 다른 쌍은 '#' 뒤 같아 생략(R13 규칙)."""
    ch = _parts(_pin(["가 12,500원 이전"], ["i:1"]), _pin(["가 12,500원 이후"], ["i:1"]))
    assert ch["text"] == [{"before": "가 ##,###원 이전", "after": "가 ##,###원 이후"}], ch
    ch = _parts(_pin(["가 12,500원"], ["t:19", "t:148만"]), _pin(["가 13,900원"], ["t:20", "t:127만"]))
    assert ch == {"text": []}, ch


# ------------------------------------------------------------------ 속성 시험: 무작위 문자열(유니코드 숫자 범주 전 범위 표본)


def _all_numeric() -> List[str]:
    return [chr(c) for c in range(0x110000) if unicodedata.category(chr(c))[0] == "N"]


def _nfkc_numeric() -> List[str]:
    """범주는 N* 가 아닌데 NFKC 뒤 숫자를 낳는 글자(㋀·㏠ 등)."""
    out = []
    for c in range(0x110000):
        ch = chr(c)
        if unicodedata.category(ch)[0] != "N" and any(unicodedata.category(x)[0] == "N"
                                                       for x in unicodedata.normalize("NFKC", ch)):
            out.append(ch)
    return out


NUMERIC = _all_numeric()
NFKC_NUMERIC = _nfkc_numeric()


def test_every_numeric_code_point_becomes_hash():
    """N* 전 범위·NFKC 숫자 글자 전부를 한 글자씩 — 출력 숫자 0."""
    assert len(NUMERIC) > 1500 and {"Nd", "Nl", "No"} <= {unicodedata.category(c) for c in NUMERIC}
    assert len(NFKC_NUMERIC) >= 50  # 측정 80(㋀~㋋·㏠~㏾·㍘~㍰ 등)
    bad = []
    for i, c in enumerate(NUMERIC + NFKC_NUMERIC):
        ch = _pair("seq" if i % 2 else "outer", "가" + c + "나")
        if _bad(ch) or not _strings(ch):
            bad.append((hex(ord(c)), _strings(ch)))
    assert bad == [], (len(bad), bad[:10])


FILL = list("가나다라abcXYZ ,.-/:_@") + ["\u200b", "\u2060", "\u00ad", "\ufeff", "\u0301", "\u20e3", "\ufe0f", "\u00a0",
                                           "\u3000", "만", "원", "명", "\n"]


@pytest.mark.parametrize("seed", range(4))
def test_random_strings_zero_numeric_property(seed):
    """무작위 문자열 1,000건(숫자 범주 전 범위 표본·NFKC 숫자 글자·구분자·보이지 않는 글자·결합문자 섞음) — 출력 숫자 0."""
    rnd = random.Random(1500 + seed)
    bad, shown = [], 0
    for _ in range(1000):
        def word() -> str:
            pool = [rnd.choice(NUMERIC), rnd.choice(NFKC_NUMERIC), rnd.choice("0123456789"), rnd.choice(FILL)]
            return "".join(rnd.choice(pool) for _ in range(rnd.randint(1, 30)))

        outer = [word() for _ in range(rnd.randint(0, 3))]
        seq = [word() for _ in range(rnd.randint(1, 5))]
        old_o, new_o = list(outer), list(outer)
        old_s, new_s = list(seq), list(seq)
        if outer and rnd.random() < 0.5:
            new_o[-1] = new_o[-1] + word()
        else:
            new_s.append(word())
        ch = _parts(_pin([o.replace("\n", " ") for o in old_o], ["t:" + s for s in old_s]),
                    _pin([o.replace("\n", " ") for o in new_o], ["t:" + s for s in new_s]))
        if _bad(ch):
            bad.append(_strings(ch))
        _assert_shape(ch)
        shown += bool(_strings(ch))
    assert bad == [], (len(bad), bad[:5])
    assert shown > 300, f"측정 조건: 보인 경우가 충분해야 한다 {shown}"


# ------------------------------------------------------------------ 주입(영구): 숫자 범주 축소·NFKC 전 판정 제거는 위 고정 표본이 잡는다


def test_injection_nd_only_is_detected(monkeypatch):
    """주입 — Nd 만 치환(No·Nl 누락): 분해 없는 원문자(❶)가 그대로 나와 오라클이 잡는다."""
    import actions.group_changed as g

    monkeypatch.setattr(g, "_NUMERIC_CATS", ("Nd",))
    assert _bad(_pair("seq", "가❶나")), "Nd 만 치환해도 원문자가 가려지면 시험이 주입을 못 잡는다"
    assert _bad(_pair("outer", "가➀나"))


def test_injection_without_pre_nfkc_judgment_is_detected(monkeypatch):
    """주입 — NFKC 전 판정 제거: 로마 숫자 Ⅻ 가 NFKC 로 'XII' 가 되어 '#' 이 아니다(고정 표본 단언이 잡는다)."""
    import actions.group_changed as g

    monkeypatch.setattr(g, "_premap", lambda text: text)
    s = _strings(_pair("seq", "가Ⅻ나"))
    assert "가XII나" in s[0], s


# ------------------------------------------------------------------ 실제 DOM → MCP call_tool → 공개 봉투(검증 B1 재현 4건)


def _page(path: str, payload: str) -> str:
    text = "문의 전화 " + payload + " 이전"
    note = ("<p class=note>" + html.escape(text) + "</p>" if path == "outer"
            else "<ul class=sequence><li>" + html.escape(text) + "</li><li>설명</li><li>참고</li></ul>")
    controls = "<ul>" + "".join("<li><button>" + x + "</button></li>" for x in ("선택", "확인", "보관")) + "</ul>"
    return ("<!doctype html><body><div style=\"display:flex\">" + "".join(
        f"<section class=panel><h3>2026년 {m}월</h3>{note}{controls}</section>" for m in (10, 11)) + "</div>")


@pytest.fixture
def blank_site():
    yield from _serve({"/": "<!doctype html><title>local</title>"})


@requires_chromium
@pytest.mark.parametrize("variant", ["unit-all", "combining-301"])
@pytest.mark.parametrize("path", ["seq", "outer"])
async def test_mcp_verifier_bypass_zero_numeric(blank_site, tmp_path, variant, path):
    """검증 R14 B1 재현(reproduce_independent.sh 4건): 공개 봉투 changed 에 숫자 성격 글자 0, 바뀐 부분은 보인다, 클릭 0."""
    from interface.mcp_server import BrowserMCPServer
    from interface.server_envelope import envelope_dict

    async with BrowserMCPServer(recipes=False, handoff_root=tmp_path / "servers") as s:
        await s._page.context.route("**/*", lambda r: r.continue_() if r.request.url.startswith(blank_site + "/")
                                    else r.abort())
        assert (await s.call_tool("browser_navigate", {"url": blank_site + "/"})).success
        await s._page.set_content(_page(path, VARIANTS[variant]))
        obs = await s.call_tool("browser_observe_page", {"force_full_tree": True, "prune_top_n": 1000})
        assert obs.success
        h = next(h for h in s._engine.handles.values() if h.name == "선택" and h.group_path)
        await s._page.evaluate(
            '()=>{for(let e of document.querySelector(".panel").querySelectorAll(".sequence li,.note"))'
            'e.textContent=e.textContent.replace("이전","이후");'
            'window.actualClicks=[];document.addEventListener("click",e=>actualClicks.push(e.target.textContent),true)}')
        r = await s.call_tool("browser_click", {"element_id": h.element_id, "epoch": obs.snapshot_epoch})
        assert not r.success and r.error_code is ErrorCode.TOCTOU_MISMATCH, r
        assert await s._page.evaluate("window.actualClicks") == []
        ch = envelope_dict(r)["data"]["group_changed"].get("changed")
    assert _shown(ch, path), ch
    assert not _bad(ch), json.dumps(ch, ensure_ascii=False)
    _assert_shape(ch)
