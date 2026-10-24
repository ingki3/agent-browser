"""WS-62 R13: 거부 안내 changed 결함 2건(R12 재검증 FAIL, #136).

B1 — 순서열·바깥 글자 전체를 먼저 PII 마스킹한 뒤 창을 고르고 자른다(토큰·줄 경계로 나뉜 전화·카드·주민·이메일 포함).
     마스킹 실패·불확실이면 해당 부분 생략(fail-closed).
B2 — 창은 before/after 의 실제 첫 문자 차이를 중심으로(앞 문맥 일부 + 차이), 잘린 쪽은 '…'. 결과가 같으면 그 쌍 생략.

재현 2건은 검증 evidence/test_changed_repros.py(실제 DOM → MCP call_tool → 공개 봉투)를 그대로 옮겼다(합성 번호, 로컬 HTTP).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pytest

from contracts import ErrorCode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "actions"))
from test_scroll_navigation import requires_chromium  # noqa: E402,F401
from test_ws62_r1_server import _serve  # noqa: E402

LIMIT = 80


def _pin(outer: List[str], seq: List[str], k: int = 0) -> str:
    return "\n".join(outer + ["S:" + "\x01".join(seq), f"#{k}"])


def _parts(old: str, new: str) -> Optional[Dict[str, Any]]:
    from actions.group_changed import changed_parts

    return changed_parts(old, new)


def _strings(ch: Optional[Dict[str, Any]]) -> List[str]:
    if not ch:
        return []
    out = [x[k] for x in ch.get("text", []) for k in ("before", "after")]
    out += [ch["items"][k] for k in ("before", "after")] if "items" in ch else []
    return out


def _assert_shape(ch: Optional[Dict[str, Any]]) -> None:
    """모든 표시 조각: ≤80자, 앞/뒤가 다름, 짝 없는 서로게이트 없음(UTF-8 로 인코딩 가능)."""
    if not ch:
        return
    pairs = list(ch.get("text", [])) + ([ch["items"]] if "items" in ch else [])
    for p in pairs:
        assert p["before"] != p["after"], p
        for s in (p["before"], p["after"]):
            assert len(s) <= LIMIT, (len(s), s)
            s.encode("utf-8")


# ------------------------------------------------------------------ 실제 DOM → MCP call_tool → 공개 봉투(검증 재현 편입)


def _page_for(kind: str) -> str:
    seq = ["010", "1234", "5678", "안내", "이전"] if kind == "pii" else ["가" * 90 + "12월", "요일", "좌석"]
    static = "<ul class=sequence>" + "".join("<li>" + x + "</li>" for x in seq) + "</ul>"
    controls = "<ul>" + "".join("<li><button>" + x + "</button></li>" for x in ("선택", "확인", "보관")) + "</ul>"
    return ("<!doctype html><html><body><div style=\"display:flex\">" + "".join(
        f"<section class=panel><h3>2026년 {m}월</h3>{static}{controls}</section>" for m in (10, 11))
        + "</div></body></html>")


@pytest.fixture
def sequence_site():
    yield from _serve({"/": _page_for("pii"), "/pii": _page_for("pii"), "/long": _page_for("long")})


async def _mcp_refusal(site: str, tmp_path: Path, kind: str) -> Dict[str, Any]:
    from interface.mcp_server import BrowserMCPServer
    from interface.server_envelope import envelope_dict

    async with BrowserMCPServer(recipes=False, handoff_root=tmp_path / "servers") as s:
        assert (await s.call_tool("browser_navigate", {"url": site + "/" + kind})).success
        await s._page.context.route("**/*", lambda r: r.continue_() if r.request.url.startswith(site + "/") else r.abort())
        obs = await s.call_tool("browser_observe_page", {"force_full_tree": True, "prune_top_n": 1000})
        assert obs.success
        hs = [h for h in s._engine.handles.values() if h.name == "선택" and h.group_path]
        assert hs, "측정 조건: 고정된 같은 이름 버튼"
        h = hs[0]
        if kind == "pii":
            await s._page.evaluate('document.querySelector(".sequence li:nth-child(5)").textContent="이후"')
        else:
            await s._page.evaluate('(v)=>document.querySelector(".sequence li").textContent=v', "가" * 90 + "1월")
        await s._page.evaluate('window.actualClicks=[];document.addEventListener("click",e=>window.actualClicks.push('
                               'e.target.textContent),true)')
        r = await s.call_tool("browser_click", {"element_id": h.element_id, "epoch": obs.snapshot_epoch})
        assert not r.success and r.error_code is ErrorCode.TOCTOU_MISMATCH, r
        assert await s._page.evaluate("window.actualClicks") == []
        env = envelope_dict(r)
        return env["data"]["group_changed"]["changed"]


@requires_chromium
async def test_mcp_items_pii_is_masked_before_window(sequence_site, tmp_path):
    """B1 재현: 비대화형 반복 항목 '010 / 1234 / 5678' — 공개 봉투 changed 에 번호 어느 부분도 남지 않는다."""
    ch = await _mcp_refusal(sequence_site, tmp_path, "pii")
    blob = json.dumps(ch, ensure_ascii=False)
    for part in ("1234", "5678", "010"):
        assert part not in blob, blob
    assert "items" in ch and "이전" in ch["items"]["before"] and "이후" in ch["items"]["after"], blob
    _assert_shape(ch)


@requires_chromium
async def test_mcp_items_long_common_prefix_keeps_actual_difference(sequence_site, tmp_path):
    """B2 재현: 공통 접두어 90자 뒤 12월→1월 — before≠after, 차이 글자가 창 안에, 앞은 '…'."""
    ch = await _mcp_refusal(sequence_site, tmp_path, "long")
    items = ch["items"]
    assert items["before"] != items["after"], ch
    assert "12월" in items["before"] and "1월" in items["after"] and "12월" not in items["after"], items
    assert items["before"].startswith("…") and items["after"].startswith("…"), items
    _assert_shape(ch)


# ------------------------------------------------------------------ B1: 토큰·줄 경계 PII


#: (이름, PII 를 이루는 항목들, 화면에 남으면 안 되는 조각들)
PII_CASES = [
    ("phone3", ["010", "1234", "5678"], ["1234", "5678"]),
    ("phone2", ["010-1234", "5678"], ["1234", "5678"]),
    ("phone_dash_edge", ["010-", "1234-", "5678"], ["1234", "5678"]),
    ("phone4", ["0", "10", "1234", "5678"], ["1234", "5678"]),
    ("card4", ["4111", "1111", "1111", "1111"], ["4111", "1111"]),
    ("card2", ["4111 1111", "1111 1111"], ["4111", "1111"]),
    ("rrn2", ["900101", "1234567"], ["900101", "1234567"]),
    ("rrn_dash", ["900101-", "1234567"], ["900101", "1234567"]),
    ("email2", ["kimcs", "@example.com"], ["kimcs", "example"]),
    ("email2b", ["kimcs@", "example.com"], ["kimcs", "example"]),
    ("email4", ["kimcs", "@", "example", ".com"], ["kimcs", "example"]),
    ("email3", ["kimcs@exa", "mple", ".com"], ["kimcs", "mple"]),
]

#: 이음 문자열 정규식이 바로 못 잡는 조각(숫자 사이 빈칸이 둘 이상·도메인 중간 쪼갬) — 불확실 → 생략이 정답.
OMIT_OK = {"phone4", "email3"}

#: PII 와 차이 지점의 상대 위치 — 창 앞 경계 밖(잘려 나감)/창 앞 경계 걸침/차이 바로 앞/차이 뒤/창 뒤 경계 걸침.
FILLER = ["안내", "보기", "선택"]


def _seq_variants(pii: List[str]) -> Iterable[tuple]:
    """(이름, before 항목, after 항목) — 마지막·가운데·첫 항목이 바뀌고 PII 는 창 경계 앞/뒤/한가운데에 걸친다."""
    for lead in (0, 1, 3, 6, 9, 14):
        pre = (FILLER * 6)[:lead]
        yield (f"before_diff_lead{lead}", pre + pii + ["이전"], pre + pii + ["이후"])
    for gap in (0, 2, 8, 13, 18, 25):
        tail = (FILLER * 10)[:gap]
        yield (f"after_diff_gap{gap}", ["이전"] + tail + pii + ["끝"], ["이후"] + tail + pii + ["끝"])
    # 창 한가운데: 차이 바로 앞·바로 뒤에 PII(앞 문맥과 뒤 부분 양쪽에 걸침).
    yield ("diff_mid", ["가" * 10] + pii + ["이전"] + pii + ["끝"], ["가" * 10] + pii + ["이후"] + pii + ["끝"])


@pytest.mark.parametrize("case,pii,parts", PII_CASES, ids=[c[0] for c in PII_CASES])
def test_items_token_boundary_pii_never_shown(case, pii, parts):
    """항목 2~4개에 걸친 PII 는 창 경계(앞/뒤/한가운데)와 무관하게 changed 에 조각도 실리지 않는다(마스킹 또는 생략)."""
    shown = 0
    for name, before, after in _seq_variants(pii):
        ch = _parts(_pin(["2026년 10월"], ["t:" + x for x in before]), _pin(["2026년 10월"], ["t:" + x for x in after]))
        blob = json.dumps(ch, ensure_ascii=False)
        for p in parts:
            assert p not in blob, (case, name, blob)
        _assert_shape(ch)
        shown += bool(ch and "items" in ch)
    if case not in OMIT_OK:
        assert shown, f"{case}: 측정 조건 — 마스킹해 보여 준 경우가 하나는 있어야 한다(전부 생략으로 통과 금지)"


def test_items_pii_mask_keeps_display_for_plain_case():
    """조건 성립: 단순 전화번호 순서열은 생략이 아니라 마스킹되어 보인다(차이 글자 포함)."""
    ch = _parts(_pin([], ["t:010", "t:1234", "t:5678", "t:안내", "t:이전"]),
                _pin([], ["t:010", "t:1234", "t:5678", "t:안내", "t:이후"]))
    assert ch and ch["items"]["before"].endswith("이전") and ch["items"]["after"].endswith("이후"), ch
    assert "[REDACTED]" in ch["items"]["before"], ch


@pytest.mark.parametrize("case,pii,parts", PII_CASES, ids=[c[0] for c in PII_CASES])
def test_outer_lines_boundary_pii_never_shown(case, pii, parts):
    """바깥 글자 줄 경로도 같은 성질: 줄 사이(바뀐 줄과 같은 줄 사이 포함)에 걸친 PII 는 마스킹 또는 생략."""
    for lead in (0, 2, 5, 12):
        pre = ["안내 " * lead] if lead else []
        old = _pin(pre + pii[:-1] + [pii[-1] + " 이전"], ["i:1"])
        new = _pin(pre + pii[:-1] + [pii[-1] + " 이후"], ["i:1"])
        ch = _parts(old, new)
        blob = json.dumps(ch, ensure_ascii=False)
        for p in parts:
            assert p not in blob, (case, lead, blob)
        _assert_shape(ch)
    one = _parts(_pin([" ".join(pii) + " 이전"], ["i:1"]), _pin([" ".join(pii) + " 이후"], ["i:1"]))
    for p in parts:
        assert p not in json.dumps(one, ensure_ascii=False), one


def test_pii_split_across_outer_and_items_never_shown():
    """바깥 마지막 줄과 순서열 첫 항목에 걸친 전화번호 — 어느 쪽 창에도 조각이 남지 않는다."""
    ch = _parts(_pin(["문의 010"], ["t:1234", "t:5678", "t:이전"]), _pin(["문의 010"], ["t:1234", "t:5678", "t:이후"]))
    blob = json.dumps(ch, ensure_ascii=False)
    assert "1234" not in blob and "5678" not in blob, blob


def test_hidden_char_split_pii_is_omitted():
    """보이지 않는 글자로 쪼갠 번호는 정규식이 못 잡는다 — 불확실이면 items 생략(fail-closed)."""
    ch = _parts(_pin([], ["t:010\u200b1234\u200b5678", "t:이전"]), _pin([], ["t:010\u200b1234\u200b5678", "t:이후"]))
    blob = json.dumps(ch, ensure_ascii=False)
    assert "1234" not in blob and "5678" not in blob, blob


def test_mask_failure_omits_items(monkeypatch):
    """마스킹 자체가 실패하면 changed 를 싣지 않는다(fail-closed)."""
    import security.masking as m

    def boom(text):
        raise RuntimeError("mask down")

    monkeypatch.setattr(m, "mask", boom)
    monkeypatch.setattr(m, "mask_text", boom)
    ch = _parts(_pin(["가"], ["t:010", "t:1234", "t:5678", "t:이전"]), _pin(["나"], ["t:010", "t:1234", "t:5678", "t:이후"]))
    assert not ch or ("items" not in ch and not ch.get("text")), ch


# ------------------------------------------------------------------ B2: 실제 첫 문자 차이를 중심으로


@pytest.mark.parametrize("n", [0, 19, 20, 21, 79, 80, 81, 200])
def test_items_common_prefix_window(n):
    ch = _parts(_pin([], ["t:" + "가" * n + "12월", "t:요일", "t:좌석"]), _pin([], ["t:" + "가" * n + "1월", "t:요일", "t:좌석"]))
    it = ch["items"]
    assert "12월" in it["before"] and "1월 요일" in it["after"], it
    assert it["before"].startswith("…") == (n + 1 > 20) == it["after"].startswith("…"), it  # 공통 접두 = n + "1"
    _assert_shape(ch)


@pytest.mark.parametrize("n", [0, 79, 80, 81, 200])
def test_outer_common_prefix_window(n):
    ch = _parts(_pin(["L:" + "가" * n + "12월 안내"], ["i:1"]), _pin(["L:" + "가" * n + "1월 안내"], ["i:1"]))
    (p,) = ch["text"]
    assert "12월" in p["before"] and "1월 안내" in p["after"], p
    _assert_shape(ch)


@pytest.mark.parametrize("where", ["start", "middle", "end", "append", "truncate"])
def test_diff_position_start_middle_end(where):
    base = ["항목%d" % i for i in range(60)]
    b, a = list(base), list(base)
    if where == "start":
        a[0] = "바뀜"
    elif where == "middle":
        a[30] = "바뀜"
    elif where == "end":
        a[-1] = "바뀜"
    elif where == "append":
        a.append("바뀜")
    else:
        b.append("사라짐")
    ch = _parts(_pin([], ["t:" + x for x in b]), _pin([], ["t:" + x for x in a]))
    it = ch["items"]
    assert ("바뀜" in it["after"]) if where != "truncate" else ("사라짐" in it["before"]), it
    if where in ("middle", "end", "append", "truncate"):
        assert it["before"].startswith("…") and it["after"].startswith("…"), it
    if where in ("start", "middle"):
        assert it["before"].endswith("…") and it["after"].endswith("…"), it
    _assert_shape(ch)


def test_identical_after_masking_pair_is_omitted():
    """차이가 PII 안에만 있어 마스킹 뒤 같아지면 그 쌍은 생략(before==after 를 싣지 않음)."""
    ch = _parts(_pin(["문의 010-1234-5678"], ["t:010", "t:1234", "t:5678"]),
                _pin(["문의 010-1234-5679"], ["t:010", "t:1234", "t:5679"]))
    assert ch is None or ("items" not in ch and ch["text"] == []), ch


@pytest.mark.parametrize("unit", ["가", "😀", "👨\u200d👩\u200d👧", "e\u0301", "한😀"])
@pytest.mark.parametrize("n", [19, 20, 21, 78, 79, 80, 81, 150])
def test_hangul_emoji_boundaries(unit, n):
    """한글·이모지(서로게이트 쌍)·ZWJ·결합 문자 경계 — 짝 없는 서로게이트 없이 ≤80자, 차이 글자 포함."""
    pre = (unit * n)[:max(n, 1) * len(unit)]
    for outer in (False, True):
        b, a = pre + "12월" + unit * 5, pre + "1월" + unit * 5
        ch = _parts(_pin([b], ["i:1"]), _pin([a], ["i:1"])) if outer else _parts(_pin([], ["t:" + b]), _pin([], ["t:" + a]))
        p = ch["text"][0] if outer else ch["items"]
        assert "12월" in p["before"] and "1월" in p["after"] and "12월" not in p["after"], p
        _assert_shape(ch)
        json.dumps(ch, ensure_ascii=False).encode("utf-8")
