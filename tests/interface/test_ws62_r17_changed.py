"""WS-62 R17: 거부 안내에서 페이지 원문 제거 — changed 는 바뀐 종류(고정 어휘)만, before/after 는 고정 표식(사용자 결정 A).

R13~R16 은 changed/before/after 의 원문 조각에서 네 번 연속 새 형태의 비밀·PII 가 나왔다(전화·이메일·계좌·전각/Nl/결합·
줄/요소 경계로 나뉜 Cookie/Bearer·민감 키). R17 의 구조 단정: GROUP_CHANGED 응답의 MCP 공개 봉투 전체 JSON 에 옛·지금 지문의
**어떤 부분 문자열(길이 ≥2, 공백 없음 — 원문과 그 NFKC 둘 다)도** 나타나지 않는다. 허용 목록은 페이지와 무관한 고정 글자뿐이다:
종류 어휘·표식·안내 틀·오류 문구 틀, 그리고 봉투의 data/error_message 밖 칸(주소·탭·epoch 등)과 data 의 키 이름·숫자 값.

오라클은 제품 코드를 쓰지 않는다(고정 어휘·틀 상수만 읽는다). 모든 값은 합성값이다.
"""

from __future__ import annotations

import html
import json
import random
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pytest

from contracts import ActionResult, ActionType, ErrorCode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "actions"))
from test_scroll_navigation import requires_chromium  # noqa: E402,F401
from test_ws62_r1_server import _serve  # noqa: E402

SECRET = "OrchidCanary"
ERR_PREFIX = "Staleness 검증 실패(묶음 바뀜) — 치유하지 않음: "
_TAIL = re.compile(r"^#-?\d+$")

# ------------------------------------------------------------------ 구조 단정(독립 오라클)


def _pin(outer: List[str], seq: List[str], k: int = 0) -> str:
    return "\n".join(outer + ["S:" + "\x01".join(seq), f"#{k}"])


def _page_words(pin: Any) -> Set[str]:
    """지문에서 페이지 유래 낱말(구조 접두 S:·i:·t:·L:·A: 와 끝 줄 #k 는 뺀다) — 원문과 NFKC 둘 다."""
    out: Set[str] = set()
    if not isinstance(pin, str):
        return out
    for line in pin.split("\n"):
        if _TAIL.match(line):
            continue
        if line.startswith("S:"):
            line = line[2:]
        for tok in line.split("\x01"):
            if tok[:2] in ("i:", "t:", "L:", "A:"):
                tok = tok[2:]
            for form in (tok, unicodedata.normalize("NFKC", tok)):
                out.update(form.split())
    return out


def _substrings(pins: Tuple[Any, ...]) -> Set[str]:
    subs: Set[str] = set()
    for w in set().union(*(_page_words(p) for p in pins)):
        for i in range(len(w)):
            for j in range(i + 2, len(w) + 1):
                subs.add(w[i:j])
    return subs


def _fixed_corpus(env: Dict[str, Any]) -> str:
    """허용 목록: 페이지와 무관한 고정 글자 — 종류 어휘·표식·안내 틀·오류 틀 + data/error_message 밖 봉투 칸 + data 키·숫자."""
    from actions.group_changed import HINT_TEMPLATE, KINDS, MARK_CHANGED, MARK_SAME

    parts = [*KINDS, MARK_CHANGED, MARK_SAME, HINT_TEMPLATE, ERR_PREFIX,
             json.dumps({k: v for k, v in env.items() if k not in ("data", "error_message")}, ensure_ascii=False),
             json.dumps(list(env), ensure_ascii=False)]  # 봉투 키 이름(data·error_message 포함)

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                parts.append(json.dumps(k, ensure_ascii=False))
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif not isinstance(x, str):
            parts.append(json.dumps(x))

    walk(env.get("data"))
    return "\n".join(parts)


def leaks(env: Dict[str, Any], *pins: Any) -> List[str]:
    """봉투 전체 JSON 에 나타난 지문 부분 문자열 중 허용 목록 밖인 것."""
    blob = json.dumps(env, ensure_ascii=False)
    blob_nfkc = unicodedata.normalize("NFKC", blob)
    fixed = _fixed_corpus(env)
    return sorted(s for s in _substrings(pins) if (s in blob or s in blob_nfkc) and s not in fixed)


def check_shape(env: Dict[str, Any]) -> List[str]:
    """data.group_changed·hint 의 문자열 값은 고정 어휘·표식·틀에서만."""
    from actions.group_changed import HINT_TEMPLATE, KINDS, MARK_CHANGED, MARK_SAME

    gc, bad = env["data"]["group_changed"], []
    if set(gc) != {"before", "after", "changed"} or set(gc["changed"]) != {"kinds"}:
        bad.append(f"keys {sorted(gc)} {sorted(gc.get('changed') or {})}")
    kinds = gc.get("changed", {}).get("kinds")
    if not kinds or not isinstance(kinds, list) or any(k not in KINDS for k in kinds):
        bad.append(f"kinds {kinds!r}")
    for side in ("before", "after"):
        if gc.get(side) not in (MARK_CHANGED, MARK_SAME, None):
            bad.append(f"{side} {gc.get(side)!r}")
    if kinds and env["data"].get("hint") != HINT_TEMPLATE.format(kinds=", ".join(kinds)):
        bad.append("hint")
    if env.get("error_message") != ERR_PREFIX + str(env["data"].get("hint")):
        bad.append("error_message")
    return bad


def _direct_env(old: Any, new: Any) -> Dict[str, Any]:
    """디스패처와 같은 조립(refusal_data → ActionResult) → 공개 봉투(envelope_dict)."""
    from actions.group_changed import refusal_data
    from interface.server_envelope import envelope_dict, envelope_json

    gc, hint = refusal_data(old, new)
    r = ActionResult(success=False, action=ActionType.CLICK, current_url="about:blank", snapshot_epoch=0, tab_id="t0",
                     retry_safe=True, error_code=ErrorCode.TOCTOU_MISMATCH, error_message=ERR_PREFIX + hint,
                     reobserve_required=True, data={"group_changed": gc, "hint": hint})
    env = envelope_dict(r)
    assert json.loads(envelope_json(r)) == env
    return env


def _kinds(old: Any, new: Any) -> List[str]:
    from actions.group_changed import changed_kinds

    return changed_kinds(old, new)


# ------------------------------------------------------------------ R13~R16 검증 재현 표본(직접 경로)

PAYLOADS = [
    ("phone3-seq", ["010", "1234", "5678", "안내"]),
    ("email-split", ["kimcs", "@example.com"]),
    ("email4", ["kimcs", "@", "example", ".com"]),
    ("card4", ["4111", "1111", "1111", "1111"]),
    ("rrn", ["900101-", "1234567"]),
    ("fullwidth-phone", ["０１０", "７３９４", "８２６１"]),
    ("account", ["계좌", "110", "347928", "615"]),
    ("slash-phone", ["010/", "7394/", "8261"]),
    ("unit-all", ["010만7394원8261명"]),
    ("combining-301", ["\u0301".join("01073948261")]),
    ("superscript", ["⁰¹⁰⁷³⁹⁴⁸²⁶¹"]),
    ("roman", ["ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ"]),
    ("nl-pwd", ["pwⅾ=OrchidCanary"]),
    ("nl-auth", ["Authorⅰzation: OrchidCanary"]),
    ("nl-json", ['{"passworⅾ":"OrchidCanary"}']),
    ("cookie-split", ["Cookie:", "session=OrchidCanary"]),
    ("cookie-plain", ["Cookie: session=OrchidCanary"]),
    ("bearer-split", ["Bearer", "OrchidCanary"]),
    ("set-cookie-nl", ["Set-Cookⅰe: OrchidCanary"]),
    ("fw-email", ["ａｕｄｉｔ＠ｅｘａｍｐｌｅ．ｔｅｓｔ"]),
    ("zw-email", ["Orchid\u200bCanary@example.com"]),
    ("api-key-fw", ["ａｐｉ＿ｋｅｙ=OrchidCanary"]),
]


@pytest.mark.parametrize("name,parts", PAYLOADS, ids=[n for n, _ in PAYLOADS])
@pytest.mark.parametrize("path", ["seq", "outer"])
def test_direct_reproductions_zero_page_substrings(name, parts, path):
    if path == "seq":
        old, new = (_pin([], ["t:" + p for p in parts + [w]]) for w in ("이전", "이후"))
    else:
        old, new = (_pin(parts + [w], ["i:선택", "i:확인", "i:보관"]) for w in ("이전", "이후"))
    env = _direct_env(old, new)
    assert check_shape(env) == [], env
    assert leaks(env, old, new) == [], (name, leaks(env, old, new))
    assert SECRET not in json.dumps(env, ensure_ascii=False)
    assert env["data"]["group_changed"]["changed"]["kinds"] == ["text"], env


# ------------------------------------------------------------------ 무작위 ≥2000 페이지(구조 단정 + 종류 정확성)

HANGUL = "가나다라마바사아자차카타파하상품항공호텔좌석출발도착안내최저가요일월화수목금토"
LATIN = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
DIGITS = "0123456789"
ODD = ["\u200b", "\u0301", "\u00ad", "ⅰ", "ⅾ", "ⅼ", "＠", "：", "－", "\u2060"]
PII = ["010-7394-8261", "audit7394@example.test", "4532015112830366", "900101-1234567", "110-347928-615",
       "Cookie: session=OrchidCanary", "Bearer OrchidCanary", "password=OrchidCanary", "Authorⅰzation: OrchidCanary",
       "０１０７３９４８２６１", "pwⅾ=OrchidCanary"]


def _word(rng: random.Random, letters: bool = True) -> str:
    pools = [HANGUL, LATIN] if letters else [DIGITS]
    w = "".join(rng.choice(rng.choice(pools)) for _ in range(rng.randint(2, 8)))
    if letters and rng.random() < 0.4:
        w += "".join(rng.choice(DIGITS) for _ in range(rng.randint(1, 4))) + rng.choice(["만", "원", "", "명"])
    if rng.random() < 0.15:
        i = rng.randint(0, len(w))
        w = w[:i] + rng.choice(ODD) + w[i:]
    return w


def _token(rng: random.Random) -> str:
    r = rng.random()
    if r < 0.1:
        return rng.choice(PII)
    if r < 0.25:
        return "".join(rng.choice(DIGITS) for _ in range(rng.randint(1, 3)))
    if r < 0.32:
        return ""
    return " ".join(_word(rng) for _ in range(rng.randint(1, 3)))


def _wrap(seq: List[str]) -> List[str]:
    return ["_" if t == "" and i % 2 else ("t:" + t if i % 3 == 0 else "i:" + t) for i, t in enumerate(seq)]


def _mutate(rng: random.Random, outer: List[str], seq: List[str], kind: str) -> Optional[Tuple[List[str], List[str]]]:
    outer, seq = list(outer), list(seq)
    letter_idx = [i for i, t in enumerate(seq) if any(not c.isdigit() and not c.isspace() for c in t)]
    if kind == "number":
        idx = [i for i, t in enumerate(seq) if any(c in DIGITS for c in t)]
        if not idx:
            return None
        i = rng.choice(idx)
        seq[i] = "".join(rng.choice(DIGITS.replace(c, "")) if c in DIGITS else c for c in seq[i])
    elif kind == "text":
        if not letter_idx:
            return None
        i = rng.choice(letter_idx)
        seq[i] = "새로운상품" + rng.choice(LATIN) + "명칭"
    elif kind == "order":
        if len(set(seq)) < 2:
            return None
        while True:
            new = list(seq)
            rng.shuffle(new)
            if new != seq:
                break
        seq = new
    elif kind == "added":
        seq.insert(rng.randint(0, len(seq)), "추가된항목" + rng.choice(LATIN))
    elif kind == "removed":
        if not letter_idx:
            return None
        del seq[rng.choice(letter_idx)]
    elif kind == "outer":
        if not outer:
            return None
        outer[rng.randrange(len(outer))] += " 바뀐표제" + rng.choice(LATIN)
    return outer, seq


def _random_cases(n: int, seed: int = 1700):
    rng = random.Random(seed)
    kinds = ["number", "text", "order", "added", "removed", "outer"]
    out = []
    while len(out) < n:
        outer = [" ".join(_word(rng) for _ in range(rng.randint(1, 4))) for _ in range(rng.randint(0, 3))]
        if rng.random() < 0.2:
            outer.append(rng.choice(PII))
        seq = [_token(rng) for _ in range(rng.randint(2, 14))]
        kind = kinds[len(out) % len(kinds)]
        m = _mutate(rng, outer, seq, kind)
        if m is None:
            continue
        k = rng.randint(0, 5)
        out.append((kind, _pin(outer, _wrap(seq), k), _pin(m[0], _wrap(m[1]), k)))
    return out


RANDOM = _random_cases(2400)


def test_random_pages_zero_page_substrings_and_kind_accuracy():
    """무작위 2,400 페이지 — 봉투 전체 JSON 에 지문 부분 문자열 0, 문자열 값은 고정 어휘에서만. 만든 변화의 종류가 kinds 에
    든다(바깥 줄 변화 → text). 한 가지만 바꾼 경우 대부분은 kinds 가 정확히 그 하나다."""
    bad, wrong, exact = [], [], 0
    for kind, old, new in RANDOM:
        env = _direct_env(old, new)
        problems = check_shape(env) + leaks(env, old, new)
        if problems:
            bad.append((kind, problems[:5], old, new))
        kinds = env["data"]["group_changed"]["changed"]["kinds"]
        want = "text" if kind == "outer" else kind
        if want not in kinds:
            wrong.append((kind, kinds, old, new))
        exact += kinds == [want]
    assert bad == [], (len(bad), bad[:3])
    assert wrong == [], (len(wrong), wrong[:3])
    assert exact >= 0.9 * len(RANDOM), exact
    assert len(RANDOM) >= 2000


def test_random_measurement_condition():
    """측정 조건: 표본 지문에 비밀·PII·숫자·이상 글자가 실제로 든다(빈 표본으로 통과 금지)."""
    blob = "".join(o + n for _k, o, n in RANDOM)
    assert blob.count(SECRET) > 300 and "010-7394-8261" in blob and "ⅾ" in blob and "\u0301" in blob
    assert sum(len(_substrings((o, n))) for _k, o, n in RANDOM[:50]) > 5000


# ------------------------------------------------------------------ 종류 정확성(고정 표본)


def test_kind_rules():
    from perception.repeated_v1160 import PIN_LIMIT

    base = ["i:가람항공", "i:12,500원", "i:누리항공", "i:9,900원", "_", "i:다온항공"]
    old = _pin(["L:추천 상품"], base, 2)
    assert _kinds(old, _pin(["L:추천 상품"], ["i:가람항공", "i:13,900원"] + base[2:], 2)) == ["number"]
    assert _kinds(old, _pin(["L:추천 상품"], ["i:가람항공", "i:112,500원"] + base[2:], 2)) == ["number"]
    assert _kinds(old, _pin(["L:추천 상품"], ["i:가람항공", "i:12,500원", "i:바다항공"] + base[3:], 2)) == ["text"]
    assert _kinds(old, _pin(["L:추천 상품"], [base[2], base[3], base[0], base[1]] + base[4:], 2)) == ["order"]
    assert _kinds(old, _pin(["L:추천 상품"], base + ["i:하늘항공"], 2)) == ["added"]
    assert _kinds(old, _pin(["L:추천 상품"], base[:-1], 2)) == ["removed"]
    assert _kinds(old, _pin(["L:추천 상품"], base, 3)) == ["order"]  # 대상 순번만 바뀜
    assert _kinds(old, _pin(["L:추천 상품 (마감)"], base, 2)) == ["text"]
    assert _kinds(_pin(["2026년 10월"], ["i:1"]), _pin(["2026년 11월"], ["i:1"])) == ["number"]
    # 달력: 빈 칸·날짜 칸 수가 달라도 숫자·빈칸뿐인 칸이면 number.
    assert _kinds(_pin([], ["_", "_", "i:1", "i:2", "i:3"]), _pin([], ["i:1", "i:2", "i:3", "i:4"])) == ["number"]
    # 판정 불가 → unknown
    assert _kinds(old, old) == ["unknown"]
    assert _kinds(old, None) == ["unknown"] and _kinds(old, old.replace("\nS:", "\nQ:")) == ["unknown"]
    long_ = _pin(["가" * PIN_LIMIT], ["i:1"])[:PIN_LIMIT]
    assert _kinds(long_, old) == ["unknown"] and _kinds(old, long_) == ["unknown"]


def test_kind_failure_is_unknown(monkeypatch):
    import actions.group_changed as g

    def boom(*a, **k):
        raise RuntimeError("diff down")

    monkeypatch.setattr(g, "_diff", boom)
    assert g.changed_kinds(_pin(["가"], ["t:1"]), _pin(["나"], ["t:2"])) == ["unknown"]
    gc, hint = g.refusal_data(_pin(["가"], ["t:1"]), _pin(["나"], ["t:2"]))
    assert gc["changed"] == {"kinds": ["unknown"]} and "unknown" in hint


def test_before_after_fixed_marks():
    """#158: 필드 계약 유지, 값은 고정 표식. 같은 지문이면 '(같음)', 없는 쪽은 None."""
    from actions.group_changed import MARK_CHANGED, MARK_SAME, display_pair

    a, b = _pin(["문의 010-7394-8261"], ["i:선택"]), _pin(["문의 010-7394-8262"], ["i:선택"])
    assert display_pair(a, b) == (MARK_CHANGED, MARK_CHANGED)
    assert display_pair(a, a) == (MARK_SAME, MARK_SAME)
    assert display_pair(None, b) == (None, MARK_CHANGED) and display_pair(a, None) == (MARK_CHANGED, None)


def test_raw_processing_code_removed():
    """R13~R16 원문 가공 코드(숫자 '#'·마스킹·80자 창·_carry 등)는 남기지 않는다."""
    import actions.group_changed as g

    src = Path(g.__file__).read_text(encoding="utf-8")
    for name in ("_carry", "_nfkc", "_window", "_cut", "_mask", "_residual_pii", "_hash", "_escape", "changed_parts",
                 "CHANGED_CHARS", "security.masking", "safe_page_text"):
        assert not hasattr(g, name) and name not in src.split('"""', 2)[2], name


# ------------------------------------------------------------------ 주입(영구): 원문을 되살리면 구조 단정이 실패한다


def _first_word(pin: str) -> str:
    return max(_page_words(pin), key=len)


def test_injection_raw_fragment_in_changed_is_detected(monkeypatch):
    import actions.group_changed as g

    real = g.refusal_data

    def leaky(old, new):
        gc, hint = real(old, new)
        gc["changed"]["kinds"] = gc["changed"]["kinds"] + [_first_word(new)]
        return gc, hint

    monkeypatch.setattr(g, "refusal_data", leaky)
    caught = sum(bool(leaks(_direct_env(o, n), o, n)) for _k, o, n in RANDOM[:200])
    assert caught == 200, caught


def test_injection_raw_before_after_is_detected(monkeypatch):
    import actions.group_changed as g
    from perception.repeated_v1160 import pin_display

    monkeypatch.setattr(g, "display_pair", lambda a, b: (pin_display(a), pin_display(b)))
    caught = sum(bool(leaks(_direct_env(o, n), o, n)) for _k, o, n in RANDOM[:200])
    assert caught == 200, caught
    old, new = (_pin(["Cookie:", f"session={SECRET} " + w], ["i:선택"]) for w in ("이전", "이후"))
    assert any(SECRET in s for s in leaks(_direct_env(old, new), old, new))


# ------------------------------------------------------------------ 실제 DOM → MCP call_tool → 공개 봉투(검증 재현 전부)


def _panel_html(path: str, parts: List[str], raw: Optional[str]) -> str:
    if raw is not None:
        note = raw
    elif path == "outer":
        note = "".join("<p class=note>" + html.escape(p) + "</p>" for p in parts + ["이전"])
    else:
        note = "<ul class=sequence>" + "".join("<li>" + html.escape(p) + "</li>" for p in parts + ["이전"]) + "</ul>"
    controls = "<ul>" + "".join("<li><button>" + x + "</button></li>" for x in ("선택", "확인", "보관")) + "</ul>"
    return ("<!doctype html><body><div style=\"display:flex\">" + "".join(
        f"<section class=panel><h3>2026년 {m}월</h3>{note}{controls}</section>" for m in (10, 11)) + "</div>")


#: R16 검증 parent_reproduce.py 3건(합성값) — 다른 요소에 나뉜 Cookie/Bearer.
RAW_CASES = [
    ("split-cookie", "<p>Cookie:</p><aside>session=OrchidCanary 이전</aside>"),
    ("plain-cookie", "<p>Cookie: session=OrchidCanary 이전</p>"),
    ("split-bearer", "<p>Bearer</p><aside>OrchidCanary 이전</aside>"),
]
MCP_CASES = ([(n, p, parts, None) for n, parts in PAYLOADS for p in ("seq", "outer")]
             + [(n, "outer", [], raw) for n, raw in RAW_CASES])


@pytest.fixture
def blank_site():
    yield from _serve({"/": "<!doctype html><title>local</title>"})


async def _refuse(s, body: str, spy: List[Tuple[Any, Any]]) -> Dict[str, Any]:
    from interface.server_envelope import envelope_dict, envelope_json

    await s._page.set_content(body)
    obs = await s.call_tool("browser_observe_page", {"force_full_tree": True, "prune_top_n": 1000})
    assert obs.success
    h = next(h for h in s._engine.handles.values() if h.name == "선택" and h.group_path)
    await s._page.evaluate(
        '()=>{const w=document.createTreeWalker(document.querySelector(".panel"),NodeFilter.SHOW_TEXT);'
        'while(w.nextNode()){w.currentNode.textContent=w.currentNode.textContent.replace("이전","이후")}'
        'window.actualClicks=[];document.addEventListener("click",e=>actualClicks.push(e.target.textContent),true)}')
    spy.clear()
    r = await s.call_tool("browser_click", {"element_id": h.element_id, "epoch": obs.snapshot_epoch})
    assert not r.success and r.error_code is ErrorCode.TOCTOU_MISMATCH, r
    assert await s._page.evaluate("window.actualClicks") == []
    env = envelope_dict(r)
    assert json.loads(envelope_json(r)) == env
    return env


def _install_spy(monkeypatch) -> List[Tuple[Any, Any]]:
    import actions.group_changed as g

    spy: List[Tuple[Any, Any]] = []
    real = g.refusal_data

    def wrapped(old, new):
        spy.append((old, new))
        return real(old, new)

    monkeypatch.setattr(g, "refusal_data", wrapped)
    return spy


@requires_chromium
async def test_mcp_public_envelope_reproductions_zero_page_substrings(blank_site, tmp_path, monkeypatch):
    """R13~R16 검증 재현 전부(전화·이메일·계좌·카드·주민·전각/Nl/결합·민감 키·나뉜 Cookie/Bearer) × 순서열/바깥 — 실제 DOM →
    MCP call_tool → 공개 봉투 전체 JSON 에 지문 부분 문자열 0, 비밀 값 0, 클릭 0."""
    from interface.mcp_server import BrowserMCPServer

    spy = _install_spy(monkeypatch)
    bad = []
    async with BrowserMCPServer(recipes=False, handoff_root=tmp_path / "servers") as s:
        await s._page.context.route("**/*", lambda r: r.continue_() if r.request.url.startswith(blank_site + "/")
                                    else r.abort())
        assert (await s.call_tool("browser_navigate", {"url": blank_site + "/"})).success
        for name, path, parts, raw in MCP_CASES:
            env = await _refuse(s, _panel_html(path, parts, raw), spy)
            assert len(spy) == 1, (name, spy)
            old, new = spy[0]
            assert SECRET not in (old + new) or SECRET in "".join(parts) + (raw or ""), name
            problems = check_shape(env) + leaks(env, old, new)
            if problems or SECRET in json.dumps(env, ensure_ascii=False):
                bad.append((name, path, problems[:5]))
            assert "text" in env["data"]["group_changed"]["changed"]["kinds"], (name, env["data"])
    assert bad == [], bad
    assert len(MCP_CASES) >= 45


@requires_chromium
async def test_mcp_injection_raw_changed_is_detected(blank_site, tmp_path, monkeypatch):
    """주입(공개 봉투): changed 에 원문 조각을 다시 넣으면 구조 단정이 실패한다."""
    import actions.group_changed as g
    from interface.mcp_server import BrowserMCPServer

    spy = _install_spy(monkeypatch)
    inner = g.refusal_data

    def leaky(old, new):
        gc, hint = inner(old, new)
        gc["changed"]["kinds"].append(_first_word(new))
        return gc, hint

    monkeypatch.setattr(g, "refusal_data", leaky)
    async with BrowserMCPServer(recipes=False, handoff_root=tmp_path / "servers") as s:
        await s._page.context.route("**/*", lambda r: r.continue_() if r.request.url.startswith(blank_site + "/")
                                    else r.abort())
        assert (await s.call_tool("browser_navigate", {"url": blank_site + "/"})).success
        env = await _refuse(s, _panel_html("outer", [], RAW_CASES[0][1]), spy)
    old, new = spy[0]
    assert leaks(env, old, new) and check_shape(env), env["data"]


# ------------------------------------------------------------------ 하나투어 다음달 거부 5흐름


@requires_chromium
async def test_hanatour_next_refusal_5_flows_kinds_and_zero_page_substrings(monkeypatch):
    """하나투어 봇 정책 '다음달' 거부 5회 — 매번 kinds 에 number(두 달 창 이동: 날짜 칸·가격 + 요일 머리글·today 표식 추가/삭제,
    조정자 결정 A: 다중 종류 정직 판정), before/after 고정 표식, 공개 봉투에 지문 부분 문자열 0."""
    from actions.group_changed import MARK_CHANGED
    from interface.server_envelope import envelope_dict
    from test_ws62_r12 import _hana_next_refusal

    spy = _install_spy(monkeypatch)
    for i in range(5):
        spy.clear()
        res, _snap, _after, _ids = await _hana_next_refusal(True)
        assert res.error_code is ErrorCode.TOCTOU_MISMATCH and len(spy) == 1, (i, res)
        env = envelope_dict(res)
        old, new = spy[0]
        assert check_shape(env) == [] and leaks(env, old, new) == [], (i, check_shape(env), leaks(env, old, new))
        gc = env["data"]["group_changed"]
        assert "number" in gc["changed"]["kinds"], (i, gc)
        assert gc["before"] == gc["after"] == MARK_CHANGED
        assert len(_substrings((old, new))) > 300  # 측정 조건: 날짜·가격·상품 글자가 지문에 든다
