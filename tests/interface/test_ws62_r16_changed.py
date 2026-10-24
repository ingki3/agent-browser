"""WS-62 R16: 마스킹 순서 교정(R15 재검증 B1) + group_changed.before/after 마스킹(#158).

R15 는 NFKC 전에 숫자 성격 글자(Nl 포함)를 '0' 으로 먼저 바꿔, 'pwⅾ'(U+217E → d)·'Authorⅰzation'·'passworⅾ' 처럼 NFKC 로
민감 키가 되는 글자를 깨뜨렸다 — 공유 마스킹이 키를 못 보고 비밀 값이 changed 에 그대로 나갔다.
R16 순서: 원문 → NFKC + 보이지 않는 글자 제거 + 숫자 뒤 결합문자 제거(숫자 치환 없이) → 공유 마스킹 → 숫자 성격 글자 '#'
→ 잔여 검사(공유 마스킹 직후 뷰 + '#' 뷰) → 창 80자. before/after(묶음 표제)도 같은 함수(값만 가림, 같아도 생략 안 함).

오라클은 제품 코드를 쓰지 않는다: 공유 마스킹(security.masking.mask_text)이 원문 NFKC 에서 무언가를 잡는 표본이면, 그 표본의
합성 비밀 값은 changed·before/after 어디에도 없어야 한다. 숫자는 출력과 그 NFKC 에서 범주 N* 0개.
재현은 검증 evidence/reproduce_pii_public.sh(test_pii_public.py — 실제 DOM → MCP call_tool → 공개 봉투) 6건을 옮겼다.
모든 값은 합성값이다.
"""

from __future__ import annotations

import html
import itertools
import json
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from contracts import ErrorCode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "actions"))
from test_scroll_navigation import requires_chromium  # noqa: E402,F401
from test_ws62_r1_server import _serve  # noqa: E402

LIMIT = 80
SECRET = "OrchidCanary"

# ------------------------------------------------------------------ 독립 오라클


def _numeric(s: str) -> List[str]:
    return [c for t in (s, unicodedata.normalize("NFKC", s)) for c in t if unicodedata.category(c)[0] == "N"]


def _changed_strings(ch: Optional[Dict[str, Any]]) -> List[str]:
    if not ch:
        return []
    out = [x[k] for x in ch.get("text", []) for k in ("before", "after")]
    return out + ([ch["items"][k] for k in ("before", "after")] if ch.get("items") else [])


def _caught_by_shared(text: str) -> bool:
    """측정 조건: 공유 마스킹이 원문 NFKC 에서 무언가를 잡는다."""
    from security.masking import mask_text

    return mask_text(unicodedata.normalize("NFKC", text)).masked


def _pin(outer: List[str], seq: List[str], k: int = 0) -> str:
    return "\n".join(outer + ["S:" + "\x01".join(seq), f"#{k}"])


def _pins(path: str, payload: str) -> Tuple[str, str]:
    """검증 pii-audit-repro 와 같은 배치: '<payload> 이전' → '<payload> 이후'(바깥 한 줄 또는 순서열 첫 항목)."""
    def pin(suffix: str) -> str:
        t = payload + " " + suffix
        return _pin([t], ["i:선택", "i:확인", "i:보관"]) if path == "outer" else _pin([], ["t:" + t])

    return pin("이전"), pin("이후")


def _direct(path: str, payload: str) -> Tuple[Optional[Dict[str, Any]], Tuple[Optional[str], Optional[str]]]:
    from actions.group_changed import changed_parts, display_pair

    old, new = _pins(path, payload)
    return changed_parts(old, new), display_pair(old, new)


# ------------------------------------------------------------------ 검증 B1 최소 재현(직접)

B1 = ["pwⅾ=OrchidCanary", "Authorⅰzation: OrchidCanary", '{"passworⅾ":"OrchidCanary"}']


@pytest.mark.parametrize("payload", B1)
@pytest.mark.parametrize("path", ["seq", "outer"])
def test_b1_nl_key_secret_not_exposed(payload, path):
    assert _caught_by_shared(payload), "측정 조건: 공유 마스킹이 NFKC 원문에서 키를 잡아야 한다"
    ch, (before, after) = _direct(path, payload)
    blob = json.dumps({"changed": ch, "before": before, "after": after}, ensure_ascii=False)
    assert SECRET not in blob, blob
    assert not _numeric(blob.replace('"#', "")), blob


# ------------------------------------------------------------------ pii-audit 축소: 민감 키 × Nl·전각·결합·숨은 글자 변형

#: NFKC 로 라틴 소문자가 되는 Nl(로마 숫자 소문자).
NL = {"i": "ⅰ", "l": "ⅼ", "v": "ⅴ", "x": "ⅹ", "c": "ⅽ", "d": "ⅾ", "m": "ⅿ"}
KEYS = ["password", "passwd", "pwd", "secret", "api_key", "apikey", "access_token", "token", "authorization"]
FORMS = ["{k}={s}", "{k}: {s}", '{{"{k}":"{s}"}}', "'{k}'='{s}'"]


def _key_variants(key: str) -> Dict[str, str]:
    out = {"plain": key, "fullwidth": "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in key),
           "zwsp": key[:2] + "\u200b" + key[2:], "soft-hyphen": key[:1] + "\u00ad" + key[1:]}
    for i, c in enumerate(key):
        if c in NL:
            out[f"nl-{i}"] = key[:i] + NL[c] + key[i + 1:]
            out[f"nl-fw-{i}"] = out["fullwidth"][:i] + NL[c] + out["fullwidth"][i + 1:]
    nls = "".join(NL.get(c, c) for c in key)
    if nls != key:
        out["nl-all"] = nls
    return out


def _audit_payloads() -> List[Tuple[str, str]]:
    rows = []
    for key in KEYS:
        for vname, kv in _key_variants(key).items():
            for form in FORMS:
                rows.append((f"{key}/{vname}/{form}", form.format(k=kv, s=SECRET)))
    rows += [("auth-header-nl", "Authorⅰzation: Bearer OrchidCanary"),
             ("bearer", "Bearer OrchidCanary.x"),
             ("email-nl-local", "OrchidCanary@exampⅼe.com"), ("email-fw-at", "OrchidCanary＠example.com"),
             ("email-nl-tld", "OrchidCanary@example.ⅽom"), ("email-zw", "Orchid\u200bCanary@example.com"),
             ("set-cookie-nl", "Set-Cookⅰe: OrchidCanary")]
    return rows


AUDIT = _audit_payloads()


def test_audit_measurement_condition():
    """측정 조건: 표본 대부분을 공유 마스킹이 NFKC 원문에서 잡는다(안 잡히는 표본만 모이면 시험이 아무것도 재지 않는다)."""
    caught = [n for n, p in AUDIT if _caught_by_shared(p)]
    assert len(AUDIT) > 250 and len(caught) >= 0.6 * len(AUDIT), (len(AUDIT), len(caught))
    assert sum("nl-" in n for n in caught) > 100


@pytest.mark.parametrize("path", ["seq", "outer"])
def test_audit_shared_catch_never_survives(path):
    """공유 마스킹이 원문 NFKC 에서 잡는 표본 — 비밀 값은 changed·before/after 어디에도 없다. 출력 숫자 0·80자."""
    leaks, numeric = [], []
    for name, payload in AUDIT:
        if not _caught_by_shared(payload):
            continue
        ch, pair = _direct(path, payload)
        blob = json.dumps({"changed": ch, "pair": pair}, ensure_ascii=False)
        if SECRET not in payload.replace("\u200b", ""):
            continue
        if SECRET in blob.replace("\u200b", "") or "Orchid" in blob or "Canary" in blob:
            leaks.append((name, blob))
        bad = [c for s in _changed_strings(ch) + [x for x in pair if x] for c in _numeric(s)]
        if bad:
            numeric.append((name, bad))
        for s in [x for x in pair if x is not None] + _changed_strings(ch):
            assert len(s) <= LIMIT, (name, s)
    assert leaks == [], (len(leaks), leaks[:5])
    assert numeric == [], numeric[:5]


def test_ordinary_text_still_shown_with_masked_email():
    """과다 생략 방지: 이메일만 든 평범한 바뀜은 가린 채로 보인다(공유 마스킹은 값만 가리고, 잔여 검사는 [REDACTED] 만 보고 걸지 않는다)."""
    ch, (before, after) = _direct("seq", "문의 OrchidCanary@example.com 담당")
    assert ch and ch.get("items"), ch
    assert "[REDACTED]" in ch["items"]["before"] and SECRET not in json.dumps(ch, ensure_ascii=False)
    assert before and after and SECRET not in before + after


# ------------------------------------------------------------------ #158 before/after


def test_before_after_masks_phone_email_key_digits():
    from actions.group_changed import display_pair

    old = _pin(["문의 010-7394-8261 OrchidCanary@example.com 2026년 10월"], ["t:pwd=OrchidCanary", "t:12,500원"])
    new = _pin(["문의 010-7394-8261 OrchidCanary@example.com 2026년 11월"], ["t:pwd=OrchidCanary", "t:12,900원"], 3)
    before, after = display_pair(old, new)
    for s in (before, after):
        assert isinstance(s, str) and s, s
        assert len(s) <= LIMIT, (len(s), s)
        assert not _numeric(s), s
        assert SECRET not in s and "7394" not in s and "8261" not in s, s


def test_before_after_plain_heading_hashes_digits_and_cuts():
    from actions.group_changed import display_pair

    old = _pin(["2026년 10월 " + "가" * 120], ["i:선택", "i:확인"])
    new = _pin(["2026년 11월 " + "가" * 120], ["i:선택", "i:확인"])
    before, after = display_pair(old, new)
    assert before.startswith("####년 ##월 가") and after.startswith("####년 ##월 가"), (before, after)
    assert len(before) == len(after) == LIMIT and before.endswith("…"), before


def test_before_after_equal_kept_and_none_kept():
    """같아져도(숫자만 다름) 생략하지 않는다 — 필드 계약 유지. 한쪽 None 은 None 그대로."""
    from actions.group_changed import display_pair

    old, new = _pin(["2026년 10월"], ["i:선택"]), _pin(["2026년 11월"], ["i:선택"])
    before, after = display_pair(old, new)
    assert before == after and before and "####년 ##월" in before, (before, after)
    b2, a2 = display_pair(None, new)
    assert b2 is None and a2 == after


def test_before_after_nl_key_hidden():
    from actions.group_changed import display_pair

    for payload in B1:
        old, new = _pins("outer", payload)
        before, after = display_pair(old, new)
        assert before is not None and after is not None
        assert SECRET not in before + after, (before, after)


# ------------------------------------------------------------------ 주입(영구)


def test_injection_premap_before_shared_mask_is_detected(monkeypatch):
    """주입 — R15 의 숫자 선치환 되살림(공유 마스킹 전에 숫자 성격 글자 → '0'): 민감 키 시험이 비밀 값을 본다."""
    import actions.group_changed as g

    real = g._nfkc

    def premapped(text: str):
        return real("".join("0" if unicodedata.category(c) in ("Nd", "Nl", "No") else c for c in text))

    monkeypatch.setattr(g, "_nfkc", premapped)
    leaked = [p for p in B1 for path in ("seq", "outer")
              if SECRET in json.dumps(_direct(path, p), ensure_ascii=False)]
    assert leaked, "숫자 선치환을 되살려도 비밀 값이 안 보이면 시험이 B1 을 못 잡는다"


# ------------------------------------------------------------------ 실제 DOM → MCP call_tool → 공개 봉투(검증 6건)


def _page(path: str, text: str) -> str:
    note = ("<p class=note>" + html.escape(text) + "</p>" if path == "outer"
            else "<ul class=sequence><li>" + html.escape(text) + "</li><li>설명</li><li>참고</li></ul>")
    controls = "<ul>" + "".join("<li><button>" + x + "</button></li>" for x in ("선택", "확인", "보관")) + "</ul>"
    return ("<!doctype html><body><div style=\"display:flex\">" + "".join(
        f"<section class=panel><h3>2026년 {m}월</h3>{note}{controls}</section>" for m in (10, 11)) + "</div>")


@pytest.fixture
def blank_site():
    yield from _serve({"/": "<!doctype html><title>local</title>"})


async def _refusal(site: str, tmp_path: Path, path: str, text: str) -> Dict[str, Any]:
    from interface.mcp_server import BrowserMCPServer
    from interface.server_envelope import envelope_dict, envelope_json

    async with BrowserMCPServer(recipes=False, handoff_root=tmp_path / "servers") as s:
        await s._page.context.route("**/*", lambda r: r.continue_() if r.request.url.startswith(site + "/")
                                    else r.abort())
        assert (await s.call_tool("browser_navigate", {"url": site + "/"})).success
        await s._page.set_content(_page(path, text))
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
        env = envelope_dict(r)
        assert json.loads(envelope_json(r)) == env
        return env["data"]["group_changed"]


@requires_chromium
@pytest.mark.parametrize("payload", B1)
@pytest.mark.parametrize("path", ["seq", "outer"])
async def test_mcp_public_envelope_no_secret(blank_site, tmp_path, payload, path):
    """검증 reproduce_pii_public.sh 6건: 공개 봉투의 changed·before/after 어디에도 비밀 값 없음, 숫자 0, 클릭 0."""
    gc = await _refusal(blank_site, tmp_path, path, payload + " 이전")
    blob = json.dumps(gc, ensure_ascii=False)
    assert SECRET not in blob, blob
    for s in _changed_strings(gc.get("changed")) + [gc.get("before") or "", gc.get("after") or ""]:
        assert not _numeric(s) and len(s) <= LIMIT, s


@requires_chromium
async def test_mcp_before_after_masked_and_injection(blank_site, tmp_path, monkeypatch):
    """#158: 공개 봉투 before/after — 전화·이메일·숫자 가림. 주입: before/after 처리 제거(옛 원문 표시)면 같은 단언이 실패한다."""
    import actions.group_changed as g

    text = "문의 010-7394-8261 OrchidCanary@example.com 12,500원 이전"

    def check(gc: Dict[str, Any]) -> List[str]:
        bad = []
        for k in ("before", "after"):
            s = gc.get(k) or ""
            if not s or _numeric(s) or SECRET in s or len(s) > LIMIT:
                bad.append(f"{k}={s!r}")
        return bad

    gc = await _refusal(blank_site, tmp_path, "outer", text)
    assert check(gc) == [], gc

    from actions.verification import safe_page_text
    from perception.repeated_v1160 import pin_display

    def raw_pair(a: Any, b: Any) -> Tuple[Optional[str], Optional[str]]:
        return tuple(None if p is None else safe_page_text(pin_display(p)) for p in (a, b))  # type: ignore[return-value]

    monkeypatch.setattr(g, "display_pair", raw_pair)
    gc = await _refusal(blank_site, tmp_path, "outer", text)
    assert check(gc), "before/after 처리를 빼도 시험이 통과하면 #158 을 못 잡는다"


def test_combinations_smoke():
    """경로 × B1 × 접미 조합 — 예외 없이 결과(fail-closed 는 None 허용)."""
    for path, payload in itertools.product(("seq", "outer"), B1):
        _direct(path, payload)
