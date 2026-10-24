"""WS-62 R14: 거부 안내 changed 의 PII 사각(전각·구분자 변형·계좌) fail-closed (R13 재검증 B1 FAIL, #136).

changed 전용 보수적 숫자 가림(표시 전용이라 과잉 가림 허용):
* 공유 마스킹 뒤 바깥 글자·순서열 이음 문자열을 NFKC + 보이지 않는 글자 제거한 검사 뷰로 만들고, 표시도 그 뷰 기준.
* 숫자 덩어리 사슬(숫자 사이 구분자만 — 빈칸·/·-·.·'·'·_·,·: 와 대시류, 반복·조합)의 숫자 합이 6자리 이상이면 사슬 전체를 가린다.
  비숫자 글자(만·원·요일 등)가 사이에 있으면 사슬이 끊긴다 — 하나투어 달력 순서열은 계속 보인다.
* 가린 뒤에도 PII 로 보이는 잔여가 있으면 그 쌍 생략, 이메일은 NFKC 뷰에서 다시 본다(전각 @).

재현 4건은 검증 evidence/test_blocking_repros.py(실제 DOM → MCP call_tool → 공개 봉투)를 옮겼다. 경계 퍼즈는 검증
evidence/boundary_fuzz.py·fuzz_changed.py 방식(5형태·항목 2~5·8변형·간격 0~80)을 표본 그대로 직접 함수로, DOM 은 축소 표본으로.
모든 번호는 합성값이다.

R15(사용자 결정 A): 숫자 사슬 가림·잔여 4자리 생략·단위 예외는 changed 숫자 전부 '#' 로 대체됐다 — 오라클 `_leak` 은 숫자 성격 글자
하나라도 노출로 본다. 규칙 단위 시험(사슬 가림·달력 숫자 보임·짧은 숫자 보임·잔여 생략·창 끝 잔여·[REDACTED] 옆) 6개는 삭제했다.
"""

from __future__ import annotations

import html
import itertools
import json
import re
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

#: 검증 퍼즈와 같은 합성 PII 5형태·8변형.
TYPES = {"phone": "01073948261", "card": "4532015112830366", "rrn": "9001011234567",
         "email": "audit7394@example.test", "account": "110347928615"}
VARIANTS = ["plain", "dash", "dot", "slash", "nbsp", "fullwidth", "hidden", "en_dash"]
_SEP = {"plain": "", "dash": "-", "dot": ".", "slash": "/", "nbsp": "\u00a0", "hidden": "\u200b",
        "en_dash": "\u2013", "fullwidth": ""}


def _tokens(kind: str, n: int, variant: str) -> List[str]:
    raw = TYPES[kind]
    cuts = sorted({len(raw) * i // n for i in range(1, n)})
    parts = [raw[a:b] for a, b in zip([0] + cuts, cuts + [len(raw)])]
    if variant == "fullwidth":
        parts = ["".join(chr(ord(c) + 0xFEE0) if c.isascii() and c.isalnum() else c for c in p) for p in parts]
    sep = _SEP[variant]
    return [p + (sep if i < len(parts) - 1 else "") for i, p in enumerate(parts)]


def _leak(ch: Any) -> tuple:
    """독립 오라클(제품 마스킹 미사용): text·items 쌍(원문·NFKC)에 숫자 성격 글자(범주 N*)가 하나라도(R15), 또는 이메일 로컬부 잔존."""
    pairs = ((ch or {}).get("text") or []) + ([ch["items"]] if (ch or {}).get("items") else [])
    shown = " ".join(p[k] for p in pairs for k in ("before", "after"))
    b = unicodedata.normalize("NFKC", shown)
    num = any(unicodedata.category(c)[0] == "N" for c in shown + b)
    return bool(num or "audit" in b or "example.test" in b), b


def _pin(outer: List[str], seq: List[str], k: int = 0) -> str:
    return "\n".join(outer + ["S:" + "\x01".join(seq), f"#{k}"])


def _parts(old: str, new: str) -> Optional[Dict[str, Any]]:
    from actions.group_changed import changed_parts

    return changed_parts(old, new)


def _assert_shape(ch: Optional[Dict[str, Any]]) -> None:
    if not ch:
        return
    for p in list(ch.get("text", [])) + ([ch["items"]] if "items" in ch else []):
        assert p["before"] != p["after"], p
        for s in (p["before"], p["after"]):
            assert len(s) <= LIMIT, (len(s), s)
            s.encode("utf-8")


# ------------------------------------------------------------------ 실제 DOM → MCP call_tool → 공개 봉투(검증 재현 4건 편입)


def _repro_page(path: str, parts: List[str]) -> str:
    note = "문의 " + " ".join(parts) + " 이전"
    seq = ("<ul class=sequence>" + "".join("<li>" + html.escape(x) + "</li>" for x in parts + ["이전"]) + "</ul>"
           if path == "seq" else "<p class=note>" + html.escape(note) + "</p>")
    controls = "<ul>" + "".join("<li><button>" + x + "</button></li>" for x in ("선택", "확인", "보관")) + "</ul>"
    return ("<!doctype html><body><div style=\"display:flex\">" + "".join(
        f"<section class=panel><h3>2026년 {m}월</h3>{seq}{controls}</section>" for m in (10, 11)) + "</div>")


REPROS = [
    ("fullwidth-phone", "seq", ["０１０", "７３９４", "８２６１"]),
    ("fullwidth-phone", "outer", ["０１０", "７３９４", "８２６１"]),
    ("account", "seq", ["계좌", "110", "347928", "615"]),
    ("slash-phone", "seq", ["010/", "7394/", "8261"]),
]


async def _refuse(s, site: str, body: str, path: str) -> Dict[str, Any]:
    """페이지를 열고 관찰 → 묶음 안 '이전'→'이후' → 같은 id 클릭 = 기준 거부(실제 클릭 0). 공개 봉투의 changed."""
    from interface.server_envelope import envelope_dict

    await s._page.set_content(body)
    obs = await s.call_tool("browser_observe_page", {"force_full_tree": True, "prune_top_n": 1000})
    assert obs.success
    h = next(h for h in s._engine.handles.values() if h.name == "선택" and h.group_path)
    await s._page.evaluate(
        '(p)=>{for(let e of document.querySelector(".panel").querySelectorAll(p==="seq"?".sequence li":".note"))'
        '{e.textContent=e.textContent.replace("이전","이후")}'
        'window.actualClicks=[];document.addEventListener("click",e=>actualClicks.push(e.target.textContent),true)}', path)
    r = await s.call_tool("browser_click", {"element_id": h.element_id, "epoch": obs.snapshot_epoch})
    assert not r.success and r.error_code is ErrorCode.TOCTOU_MISMATCH, r
    assert await s._page.evaluate("window.actualClicks") == []
    return envelope_dict(r)["data"].get("group_changed", {}).get("changed")


@pytest.fixture
def blank_site():
    yield from _serve({"/": "<!doctype html><title>local</title>"})


async def _with_server(site: str, tmp_path: Path):
    from interface.mcp_server import BrowserMCPServer

    s = BrowserMCPServer(recipes=False, handoff_root=tmp_path / "servers")
    await s.__aenter__()
    await s._page.context.route("**/*", lambda r: r.continue_() if r.request.url.startswith(site + "/") else r.abort())
    assert (await s.call_tool("browser_navigate", {"url": site + "/"})).success
    return s


@requires_chromium
@pytest.mark.parametrize("kind,path,parts", REPROS, ids=[f"{k}-{p}" for k, p, _ in REPROS])
async def test_mcp_changed_never_exposes_four_digit_pii(blank_site, tmp_path, kind, path, parts):
    """B1 재현(검증 reproduce_blocking.sh): 전각 전화(순서열·바깥)·계좌·슬래시 전화 — 공개 봉투 changed 에 4자리 이상 없음,
    그리고 바뀐 부분은 계속 보인다(전부 생략으로 통과 금지)."""
    s = await _with_server(blank_site, tmp_path)
    try:
        ch = await _refuse(s, blank_site, _repro_page(path, parts), path)
    finally:
        await s.__aexit__(None, None, None)
    assert ch and ("items" in ch if path == "seq" else ch.get("text")), ch
    bad, blob = _leak(ch)
    assert not bad, blob
    _assert_shape(ch)


#: DOM 경계 축소 표본 — 형태마다 전각·구분자 변형·계좌가 빠지지 않게 (형태, 경로, 앞/뒤, 간격, 항목 수, 변형).
DOM_SAMPLE = [
    (kind, path, side, offset, n, var)
    for kind in TYPES
    for (path, side, offset, n, var) in (
        ("seq", "before", 0, 3, "fullwidth"), ("seq", "after", 80, 5, "slash"),
        ("outer", "before", 40, 2, "en_dash"), ("outer", "after", 17, 4, "fullwidth"),
        ("seq", "before", 63, 4, "nbsp"), ("outer", "before", 5, 3, "hidden"),
    )
]


def _boundary_parts(kind: str, side: str, offset: int, n: int, var: str) -> List[str]:
    ts = _tokens(kind, n, var)
    if side == "before":
        return ["안내 " + "가" * 90] + ts + ["나" * offset + "이전" + "끝" * 85]
    return ["안내 " + "가" * 90 + "이전" + "나" * offset] + ts + ["끝" * 85]


def _boundary_page(path: str, parts: List[str]) -> str:
    seq = ("<ul class=sequence>" + "".join("<li>" + html.escape(x) + "</li>" for x in parts) + "</ul>"
           if path == "seq" else "<p class=note>" + html.escape(" ".join(parts)) + "</p>")
    controls = "<ul>" + "".join("<li><button>" + x + "</button></li>" for x in ("선택", "확인", "보관")) + "</ul>"
    return ("<!doctype html><body><div style=\"display:flex\">" + "".join(
        f"<section class=panel><h3>2026년 {m}월</h3>{seq}{controls}</section>" for m in (10, 11)) + "</div>")


@requires_chromium
async def test_mcp_boundary_fuzz_sample_zero_exposure(blank_site, tmp_path):
    """검증 독립 경계 퍼즈(실제 items/바깥 text) 축소 표본 30건: 노출 0, 보인 쌍은 실제 차이('이전'/'이후') 포함."""
    s = await _with_server(blank_site, tmp_path)
    leaks, shown = [], 0
    try:
        for kind, path, side, offset, n, var in DOM_SAMPLE:
            parts = _boundary_parts(kind, side, offset, n, var)
            ch = await _refuse(s, blank_site, _boundary_page(path, parts), path)
            bad, blob = _leak(ch)
            if bad:
                leaks.append((kind, path, side, offset, n, var, blob))
            pairs = ((ch.get("text") or []) + ([ch["items"]] if ch.get("items") else [])) if ch else []
            for x in pairs:
                assert "이전" in x["before"] and "이후" in x["after"], (kind, path, x)
            _assert_shape(ch)
            shown += bool(pairs)
    finally:
        await s.__aexit__(None, None, None)
    assert leaks == [], leaks
    assert shown >= len(DOM_SAMPLE) // 2, f"측정 조건: 가려 보여 준 경우가 충분해야 한다(전부 생략 통과 금지) {shown}"


# ------------------------------------------------------------------ 직접 함수 경계 퍼즈(검증 방식 전수: 5형태 × 항목 2~5 × 8변형 × 2경로 × 간격 0~80)


def _fuzz_pin(parts: List[str], path: str, lead: int, after: bool = False) -> str:
    parts = ["안내 " + "가" * lead] + parts + ["이후" if after else "이전"]
    seq = parts if path == "seq" else ["선택", "확인", "보관"]
    return _pin(parts if path == "outer" else [], ["t:" + p for p in seq])


@pytest.mark.parametrize("kind", list(TYPES))
def test_direct_boundary_fuzz_zero_exposure(kind):
    """한 형태당 4 × 8 × 2 × 81 = 5,184건 — 노출 0. 변형마다 보여 준 경우가 있어야 한다(과잉 생략으로 통과 금지)."""
    leaks, shown = [], {}
    for n, var, path, lead in itertools.product(range(2, 6), VARIANTS, ("seq", "outer"), range(81)):
        ts = _tokens(kind, n, var)
        ch = _parts(_fuzz_pin(ts, path, lead), _fuzz_pin(ts, path, lead, True))
        bad, blob = _leak(ch)
        if bad:
            leaks.append((n, var, path, lead, blob))
        _assert_shape(ch)
        shown[var] = shown.get(var, 0) + bool(ch and (ch.get("text") or ch.get("items")))
    assert leaks == [], (len(leaks), leaks[:5])
    # 이메일을 항목 경계(빈칸)로 쪼갠 것은 공유 규칙이 못 잡아 불확실 → 생략이 정답(R13 OMIT_OK 와 같음) — 일부 변형만 보인다.
    assert all(shown.get(v) for v in VARIANTS) if kind != "email" else sum(shown.values()), shown


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize("path", ["seq", "outer"])
@pytest.mark.parametrize("kind", list(TYPES))
def test_direct_window_boundary_fuzz_zero_exposure(kind, path, side):
    """검증 boundary_fuzz 배치(긴 접두 뒤 차이 앞/뒤에 PII, 간격 0~80 전수) — 직접 함수로 노출 0."""
    leaks = []
    for offset in range(81):
        n, var = 2 + offset % 4, VARIANTS[(offset // 4) % len(VARIANTS)]
        parts = _boundary_parts(kind, side, offset, n, var)
        after = [p.replace("이전", "이후") for p in parts]
        if path == "seq":
            ch = _parts(_pin([], ["t:" + p for p in parts]), _pin([], ["t:" + p for p in after]))
        else:
            ch = _parts(_pin([" ".join(parts)], ["i:선택"]), _pin([" ".join(after)], ["i:선택"]))
        bad, blob = _leak(ch)
        if bad:
            leaks.append((offset, n, var, blob))
        _assert_shape(ch)
    assert leaks == [], (len(leaks), leaks[:5])


# ------------------------------------------------------------------ 규칙 단위(주입이 잡히는 고정 표본)


def test_fullwidth_phone_masked_via_nfkc_view():
    """전각 전화 '０１０ ７３９４ ８２６１' — 표시는 정규화 글자 기준이고 숫자는 전부 '#'(R15). 전각 구분자·전각 글자도 정규화."""
    ch = _parts(_pin(["문의 ０１０ ７３９４ ８２６１ 이전"], ["i:1"]), _pin(["문의 ０１０ ７３９４ ８２６１ 이후"], ["i:1"]))
    (p,) = ch["text"]
    assert p["before"] == "문의 ### #### #### 이전" and p["after"] == "문의 ### #### #### 이후", p
    assert not _leak(ch)[0], ch
    for sep in ("／", "．", "－", "："):
        num = sep.join(["０１０", "７３９４", "８２６１"])
        ch = _parts(_pin([], ["t:ＡＢＣ", "t:" + num, "t:이전"]), _pin([], ["t:ＡＢＣ", "t:" + num, "t:이후"]))
        it = (ch or {}).get("items")
        assert it and it["before"].startswith("ABC ") and it["before"].endswith(" 이전") and not _leak(ch)[0], (sep, ch)


def test_fullwidth_email_masked_via_nfkc_view():
    """전각 @ 이메일 — NFKC 뷰에서 다시 검사해 가린다."""
    ch = _parts(_pin(["문의 ａｕｄｉｔ＠ｅｘａｍｐｌｅ．ｔｅｓｔ 이전"], ["i:1"]),
                _pin(["문의 ａｕｄｉｔ＠ｅｘａｍｐｌｅ．ｔｅｓｔ 이후"], ["i:1"]))
    blob = unicodedata.normalize("NFKC", json.dumps(ch, ensure_ascii=False))
    assert "audit" not in blob and "example" not in blob, blob


def test_changed_parts_never_raises(monkeypatch):
    """표시 전용 — 어느 단계에서 실패해도 거부 응답을 깨지 않고 changed 를 싣지 않는다(fail-closed)."""
    import actions.group_changed as g

    def boom(*a, **k):
        raise RuntimeError("window down")

    monkeypatch.setattr(g, "_window", boom)
    assert g.changed_parts(_pin(["가"], ["t:1"]), _pin(["나"], ["t:2"])) is None


# ------------------------------------------------------------------ 하나투어 정상 거부 — changed.items 비지 않음(5/5)


@requires_chromium
async def test_hanatour_next_refusal_items_never_empty_5_of_5():
    """하나투어 봇 정책 '다음달' 거부 5회(css — 무 CSS 는 거부 없이 진행) — 매번 changed.items 가 있고 앞뒤가 다르다.
    R15: 날짜·가격 숫자는 '#'(빈 칸 '_' 배치·'만'·'최저가' 가 달라 숫자만 다른 쌍이 아니다 — 생략되지 않음, 측정 5/5)."""
    from test_ws62_r12 import _hana_next_refusal

    for i in range(5):
        res, _snap, _after, _ids = await _hana_next_refusal(True)
        assert res.error_code is ErrorCode.TOCTOU_MISMATCH, res
        ch = res.data["group_changed"].get("changed") or {}
        items = ch.get("items")
        assert items and items["before"] != items["after"], (i, ch)
        assert "#" in items["before"] and "#" in items["after"] and not _leak(ch)[0], items
