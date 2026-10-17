"""WS-51 R4 — 비밀 값 레지스트리 결함 7건(R3-B1~B7) + 리뷰 신규 2건의 단위 행렬(계획 §3·§4·§1.5).

* 표현 집합 R 은 `security.secret_registry.representations(v)` 한 곳(검증자도 import) — 여기서는 표준 라이브러리로
  독립 구성한 집합과 같은지(스냅샷)를 고정한다.
* 누출 행렬(§4): S 대표 값 × 등록 경로(§1.1 열거) × representations(v) × 출구 O(a)~(d) × 위치. 실 Chromium 대표 경로는
  tests/interface/test_ws51_r4_e2e.py, 서버 도구·MCP 예외 경로는 tests/interface/test_ws51_r4_server.py.
* 범위 밖(§1.3·§1.6)은 '현재 동작 고정' 테스트로 따로.

값은 모두 합성 값. 공개 사이트 없음.
"""

from __future__ import annotations

import html
import io
import json
import logging
import sys
import threading
import time
from collections import OrderedDict, namedtuple
from pathlib import Path
from urllib.parse import quote, quote_plus

import pytest

from contracts import ActionResult, ActionType, ErrorCode
from security import secret_registry
from security.public_url import public_payload, public_text, public_url

from ws51_r4_leak import LEAK_VALUES, VALUES, leaks_in_text, leaks_in_wire

R = "[REDACTED]"
ORIGIN = "http://127.0.0.1:1"


@pytest.fixture(autouse=True)
def _fresh_registry():
    secret_registry.clear()
    yield
    secret_registry.clear()


# ------------------------------------------------------------------ P6 표현 집합 스냅샷(§1.3)


def _spec_representations(v: str) -> set:
    """계획 §1.3 문장을 표준 라이브러리로 그대로 옮긴 독립 구성(구현과 비교하는 스냅샷)."""
    import re

    def lower(s: str) -> str:
        return re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), s)

    def r2_upper(s: str) -> list:
        return [quote(s, safe=""), quote_plus(s, safe=""), quote(s, safe="!*'()"), quote(s, safe="!*'();/?:@&=+$,#")]

    def r2(s: str) -> list:
        return [f for u in r2_upper(s) for f in (u, lower(u))]

    def r3(s: str) -> list:
        return [json.dumps(s)[1:-1], json.dumps(s, ensure_ascii=False)[1:-1]]

    num = {"&": ("&#38;", "&#x26;"), "<": ("&#60;", "&#x3c;"), ">": ("&#62;", "&#x3e;"),
           '"': ("&#34;", "&#x22;"), "'": ("&#39;", "&#x27;")}
    r5 = [html.escape(v, quote=True), "".join(num.get(c, (c,))[0] for c in v),
          "".join(num[c][1] if c in num else c for c in v)]
    out = {v, *r2(v), *[quote(u, safe="") for u in r2_upper(v)], *r3(v), repr(v)[1:-1], *r5}
    out |= {p for j in r3(v) for p in r2(j)} | {j for h in r5 for j in r3(h)}
    return out


@pytest.mark.parametrize("name", sorted(VALUES))
def test_representations_match_spec_snapshot(name):
    v = VALUES[name] if name != "long64k" else VALUES[name][:300] + "é<"
    reps = secret_registry.representations(v)
    assert len(reps) == len(set(reps))  # 중복 없음
    assert reps[0] == v                  # R1 이 맨 앞(예산이 모자라도 원문은 남긴다)
    assert set(reps) == _spec_representations(v)
    assert secret_registry.representations(v) == reps  # 결정적 순서


def test_representations_known_counts_and_url_subset():
    """집합 크기 스냅샷: 영숫자 값은 1개, 특수 문자 값은 계획 §1.3 의 모든 계열."""
    assert secret_registry.representations("SYNTH83Secret") == ["SYNTH83Secret"]
    special = 'x "&\u00e9'
    reps = secret_registry.representations(special)
    assert len(reps) == 30
    url = secret_registry.url_representations(special)
    assert url[0] == special and set(url) <= set(reps)
    assert {"x%20%22%26%C3%A9", "x%20%22%26%c3%a9", "x+%22%26%C3%A9", "x%20%22&%C3%A9",
            "x%2520%2522%2526%25C3%25A9"} <= set(url)
    assert not any("&quot;" in f or "\\" in f for f in url)  # 주소 칸 계열은 R1·R2 만


def test_invariant_anchor_is_in_every_representation():
    """사전 거르기 닻(퍼센트·JSON·repr·HTML 이 바꾸지 않는 `[A-Za-z0-9_.~-]` 덩어리)이 모든 표현에 든다."""
    for v in (VALUES["special"], VALUES["unicode"], "ab<cd>ef&gh'ij\"kl", "%41BCD%2b+~x.y-z_"):
        runs = secret_registry._INVARIANT.findall(v)
        anchor = max(runs, key=len)
        assert all(anchor in r for r in secret_registry.representations(v)), v


# ------------------------------------------------------------------ 등록 경로(§1.1 열거)


class _Frame:
    def __init__(self, url, parent=None):
        self.url, self.parent_frame = url, parent


class _Req:
    def __init__(self, url, redirected_from=None):
        self.url, self.frame, self.redirected_from = url, _Frame(url), redirected_from

    def is_navigation_request(self):
        return True


class _Emitter:
    def __init__(self, url=""):
        self.handlers, self.pages, self.url = {}, [], url

    def on(self, name, fn):
        self.handlers.setdefault(name, []).append(fn)

    def emit(self, name, arg):
        for fn in self.handlers.get(name, []):
            fn(arg)


def _reg_goto(url, tmp):
    from actions.dispatcher import ActionDispatcher

    d = ActionDispatcher.__new__(ActionDispatcher)
    assert d._refuse_redacted_url(ActionType.NAVIGATE, url) is None


def _reg_redirect(url, tmp):
    secret_registry._on_request(_Req(f"{ORIGIN}/clean", redirected_from=_Req(url)))


def _reg_framenavigated(url, tmp):
    ctx, page = _Emitter(), _Emitter()
    secret_registry.watch_browser_context(ctx)
    ctx.emit("page", page)
    page.emit("framenavigated", _Frame(url))


def _reg_popup(url, tmp):
    ctx = _Emitter()
    secret_registry.watch_browser_context(ctx)
    ctx.emit("page", _Emitter(url))


def _reg_response_url_field(url, tmp):
    public_payload({"data": {"window": {"tabs": [{"url": url}]}}})


def _reg_restored_tab(url, tmp):
    secret_registry.register_urls([{"url": url}["url"]])  # server_lifecycle/_window 의 보관 탭 등록 함수


def _reg_recipe_load(url, tmp):
    from recipes.store import VERSION, RecipeStore

    path = Path(tmp) / f"recipes-{abs(hash(url))}.json"
    rec = {"id": "r1", "steps": [{"action": "navigate", "args": {"url": url}}], "stats": {}, "origin": ORIGIN}
    path.write_text(json.dumps({"version": VERSION, "recipes": {"r1": rec}}), encoding="utf-8")
    path.chmod(0o600)
    RecipeStore(path)


PATHS = {
    "goto": _reg_goto, "redirect_hop": _reg_redirect, "framenavigated": _reg_framenavigated,
    "popup_tab": _reg_popup, "response_url_field": _reg_response_url_field,
    "restored_tab": _reg_restored_tab, "recipe_load": _reg_recipe_load,
}  # 다운로드 대상은 실 Chromium(test_ws51_r4_e2e) — 가짜 download 객체로는 경로를 증명하지 못한다.


def _seen_url(value: str) -> str:
    return f"{ORIGIN}/seen?token=" + quote(value, safe="")


# ------------------------------------------------------------------ 출구(§1.2)


def _wire_action(text: str, url: str) -> str:
    from interface.server_envelope import envelope_json

    return envelope_json(ActionResult(
        action=ActionType.OBSERVE_PAGE, success=False, error_code=ErrorCode.TIMEOUT,
        error_message="goto failed: " + text, snapshot_epoch=1, current_url=url, tab_id="tab-1",
        retry_safe=True, data={"observation": {"title": text, "url": url}}))


def _wire_server_tool(text: str, url: str) -> str:
    return json.dumps(public_payload({"success": True, "data": {
        "control": {"reason": text, "holder": "agent"},
        "window": {"tabs": [{"tab_id": "tab-1", "url": url, "title": text}]}}}), ensure_ascii=False)


class _Obj:
    def __init__(self, text):
        self.text = text

    def __repr__(self):
        return f"Obj(url={self.text!r})"


def _logs(text: str) -> str:
    """같은 문장을 로그의 모든 자리(§1.2 c)로 — %s·%r·예외 %s/%r·객체 repr·extra 문자열/dict/list/중첩/키·exc_text·
    stack_info·미리 만든 f-string. StreamHandler 원문."""
    from interface.mcp_server import install_public_log_filter

    lg = logging.getLogger("ws51.r4.matrix")
    prior = (lg.level, lg.propagate, list(lg.handlers))
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    out = io.StringIO()
    handler = logging.StreamHandler(out)
    handler.setFormatter(logging.Formatter("%(message)s | %(meta)s"))
    lg.handlers = [handler]
    restore = install_public_log_filter()
    try:
        lg.warning("s %s", text, extra={"meta": "-"})
        lg.warning("r %r", text, extra={"meta": "-"})
        lg.warning("es %s", ValueError(text), extra={"meta": "-"})
        lg.warning("er %r", ValueError(text), extra={"meta": "-"})
        lg.warning("o %s", [_Obj(text)], extra={"meta": "-"})
        lg.warning(f"f {text!r}", extra={"meta": "-"})
        lg.warning("d %s", {"k": text, text: 1}, extra={"meta": "-"})
        for meta in (text, {"secret": text}, [text], {"n": [{"m": (text,)}]}, {text: "key"},
                     OrderedDict(a=text), namedtuple("NT", "a b")(text, 1), {text}):
            lg.warning("extra", extra={"meta": meta})
        try:
            raise RuntimeError(f"Page.goto: navigating to \"{text}\"")
        except RuntimeError:
            lg.exception("exc", extra={"meta": "-"})
        lg.warning("stack", stack_info=True, extra={"meta": "-"})
    finally:
        restore()
        lg.handlers = prior[2]
        lg.setLevel(prior[0])
        lg.propagate = prior[1]
    return out.getvalue()


def _excepthooks(text: str, capsys) -> str:
    from interface.mcp_server import install_public_log_filter

    capsys.readouterr()
    restore = install_public_log_filter()
    try:
        try:
            raise ValueError(f"boom {text}")
        except ValueError as exc:
            sys.excepthook(type(exc), exc, exc.__traceback__)
            args = threading.ExceptHookArgs((type(exc), exc, exc.__traceback__, threading.current_thread()))
            threading.excepthook(args)
    finally:
        restore()
    return capsys.readouterr().err


def _stderr_line(text: str, capfd) -> str:
    from interface.server_common import _stderr_line as line

    capfd.readouterr()
    line(text)
    return capfd.readouterr().err


def _mcp_error(text: str) -> str:
    from interface.mcp_server import public_tool_error

    return str(public_tool_error(ValueError(f"serialize failed at {text}")))


def _emit_all(text: str, url: str, capfd) -> dict:
    return {
        "a_envelope": ("wire", _wire_action(text, url)),
        "b_server_tool": ("wire", _wire_server_tool(text, url)),
        "c_log": ("text", _logs(text)),
        "c_stderr_line": ("text", _stderr_line(text, capfd)),
        "c_excepthook": ("text", _excepthooks(text, capfd)),
        "d_mcp_error": ("text", _mcp_error(text)),
    }


# ------------------------------------------------------------------ 누출 행렬(§4)


@pytest.mark.parametrize("path", sorted(PATHS))
@pytest.mark.parametrize("name", LEAK_VALUES)
def test_leak_matrix(name, path, tmp_path, capfd):
    """S × 등록 경로 × representations(v) × 출구 O(a)~(d) × 위치(제목·error_message·주소 칸·로그·extra·exc_text·예외 문구)
    → 누출 0(§1.4). 주소 칸 위치는 ≥ 8 값의 R1·R2 표현(§2 P3 — 4~7 은 범위 밖, 아래 '현재 동작 고정')."""
    value = VALUES[name]
    PATHS[path](_seen_url(value), tmp_path)
    assert len(secret_registry.REGISTRY) >= 1, "조건 성립: 등록 경로가 값을 등록했다"
    long = len(value) >= secret_registry.MIN_SUBSTR_CHARS
    url_forms = secret_registry.url_representations(value) if long else []
    bad = {}
    for i, rep in enumerate(secret_registry.representations(value)):
        url = f"{ORIGIN}/alias?ref={rep}" if rep in url_forms else f"{ORIGIN}/clean"
        text = f"세션 {rep} 만료"
        for exit_name, (kind, out) in _emit_all(text, url, capfd).items():
            found = leaks_in_wire(out, value) if kind == "wire" else leaks_in_text(out, value)
            if found:
                bad[f"rep{i}/{exit_name}"] = found
        if rep in url_forms:  # 경로 자리
            found = leaks_in_wire(_wire_action("t", f"{ORIGIN}/{rep}"), value)
            if found:
                bad[f"rep{i}/path"] = found
    assert bad == {}, bad


def test_tool_vocabulary_value_is_not_registered():
    """도구 어휘(§1.1 제외): `?token=navigate` 를 봐도 등록되지 않고 문장·제어 필드가 그대로다."""
    for value in ("navigate", "observe_page", "E_INVALID_URL", "tab-1", "tab-12", "@e3", "ap_AbCdEf123456_-xy",
                  "rq_AbCdEf123456", "browser_navigate", "12345-abcdef"):
        secret_registry.register_url(_seen_url(value))
    assert len(secret_registry.REGISTRY) == 0
    assert public_text("navigate 실패: tab-1 E_INVALID_URL") == "navigate 실패: tab-1 E_INVALID_URL"


# ------------------------------------------------------------------ P2 제어 필드(§1.5, R3-B7)


@pytest.mark.parametrize("value", ["navigate", "observe_page", "tab-1", "E_INVALID_URL", "approved"])
def test_control_fields_keep_original_values(value):
    """제어 키 + 도구 어휘·식별자 형식이면 값이 원래와 같다(파싱 가능만이 아니라 동일). 등록이 우회된 경우까지 막으려고
    레지스트리에 직접 넣어 본다(등록 제외가 깨져도 응답 계약은 유지 — 이중 방어)."""
    secret_registry.REGISTRY.add_values([value])
    secret_registry.register_url(_seen_url(value))
    payload = {
        "action": "navigate", "error_code": "E_INVALID_URL", "tab_id": "tab-1", "popup_tab_id": "tab-2",
        "data": {"tabs": [{"tab_id": "tab-1", "active": True}], "active_tab_id": "tab-1", "closed_tab_id": "tab-3",
                 "window": {"tabs": [{"tab_id": "tab-1"}], "state": "headless"},
                 "approval": {"approval_id": "ap_AbCdEf123456_-xy", "status": "approved", "outcome": "succeeded",
                              "expires_at": "2026-10-16T12:00:00Z"},
                 "control": {"holder": "agent", "request_id": "rq_AbCdEf123456", "server_id": "123-abcdef",
                             "since": "2026-10-16T12:00:00Z", "reason": "request_expired"},  # R5: reason 은 어휘만
                 "element_id": "@e1"},
    }
    out = public_payload(payload)
    assert out == payload, json.dumps(out, ensure_ascii=False)


def test_control_field_with_other_shape_is_still_sanitized():
    """형식이 다르면 정제(방어): 제어 키라도 자유 문장·주소가 실리면 가린다."""
    secret_registry.register_url(_seen_url("SYNTHctrlValue9"))
    payload = {"action": "SYNTHctrlValue9", "tab_id": "tab-SYNTHctrlValue9", "status": "x SYNTHctrlValue9",
               "reason": "/a?token=abc 끝", "error_code": "E_SYNTHctrlValue9"}
    out = json.dumps(public_payload(payload))
    assert "SYNTHctrlValue9" not in out and "token=abc" not in out


def test_control_key_inventory_is_fixed():
    """제어 키 전수 목록(보고서 표) — 키를 빼거나 형식을 넓히면 이 테스트가 바뀐다."""
    pu = sys.modules["security.public_url"]  # `security.public_url` 속성은 같은 이름의 함수(재수출)
    assert pu.CONTROL_ENUM_KEYS == {"status", "state", "holder", "reason", "reason_code", "kind", "mode",
                                    "browser_mode", "outcome", "op", "risk", "presented_as", "sticky_reason",
                                    "changed", "nav_aborted",  # R6: census 일반화로 드러난 이동 중단 신호
                                    # R7: 리터럴 제어값 census(소비자 표 독립)로 드러난 키
                                    "nav_frame", "resolved_from", "coordinate_target", "handled", "blocked_by",
                                    "captured", "code", "category", "scope", "storage", "source", "how_to_wait",
                                    # R8: 속성 전달값 census 로 드러난 키(목록 경로 dialogs[].type 은 아래 별도 표)
                                    "vendor"}
    assert pu.CONTROL_ITEM_VOCABULARY == {("dialogs", "type"): {"alert", "beforeunload", "confirm", "prompt",
                                                                "unknown"}}
    # R9: 목록 바로 아래 문자열 원소 경로 어휘(healing_attempts 복합 코드·signals 순수 코드 — test_ws51_r9_census 가 도출)
    assert set(pu.CONTROL_LIST_VOCABULARY) == {"healing_attempts", "signals"}
    assert pu.CONTROL_TIME_KEYS == {"since", "expires_at", "started_at", "created_at", "detected_at", "reset_at"}
    assert pu._CONTROL_ID_KEYS == {"tab_id": "tab_id", "approval_id": "approval_id", "request_id": "request_id",
                                   "server_id": "server_id", "element_id": "element_id",
                                   "trigger_element_id": "element_id", "id": "recipe_id",  # R5 B4: 레시피 id
                                   "params": "param_name"}  # R6: 레시피 자동 매개변수 이름 textN·choiceN
    assert pu.control_value("popup_tab_id", "tab-3") and pu.control_value("closed_tab_id", "tab-9")
    assert not pu.control_value("tab_id", "tab-1x") and not pu.control_value("status", "Approved!")


def test_payload_dict_keys_sanitized_unless_tool_key():
    """dict 키: 도구가 만든 고정 이름(소문자 snake)은 그대로, 그 밖의 키는 public_text."""
    secret_registry.register_url(_seen_url("SYNTHkeyValue77"))
    out = public_payload({"data": {"SYNTHkeyValue77-x": 1, "Weird Key SYNTHkeyValue77": 2, "status": "ok"}})
    assert "SYNTHkeyValue77" not in json.dumps(out) and out["data"]["status"] == "ok"


# ------------------------------------------------------------------ P1 로그(R3-B2·B3·B4)


def _registered(value: str) -> None:
    secret_registry.register_url(_seen_url(value))


@pytest.mark.parametrize("kind", ["str", "dict", "list", "nested", "dict_key", "namedtuple", "ordereddict", "set"])
def test_b3_extra_containers_stream_and_file(kind, tmp_path):
    """R3-B3: extra 의 dict·list·중첩·dict 키 — StreamHandler 와 FileHandler 둘 다 원문 0."""
    from interface.mcp_server import install_public_log_filter

    marker = "SYNTHNestedLog99"
    _registered(marker)
    meta = {"str": marker, "dict": {"secret": marker}, "list": [marker], "nested": {"a": [{"b": (marker, 1)}]},
            "dict_key": {marker: 1}, "namedtuple": namedtuple("NT", "x")(marker),
            "ordereddict": OrderedDict(s=marker), "set": {marker}}[kind]
    lg = logging.getLogger("ws51.r4.b3")
    lg.propagate = False
    lg.setLevel(logging.INFO)
    stream = io.StringIO()
    a = logging.StreamHandler(stream)
    b = logging.FileHandler(tmp_path / "log.txt", mode="w", encoding="utf-8")
    fmt = logging.Formatter("%(message)s %(metadata)s")
    a.setFormatter(fmt)
    b.setFormatter(fmt)
    lg.handlers = [a, b]
    restore = install_public_log_filter()
    try:
        lg.warning("retained", extra={"metadata": meta})
    finally:
        restore()
        a.close()
        b.close()
        lg.handlers = []
    for text in (stream.getvalue(), (tmp_path / "log.txt").read_text(encoding="utf-8")):
        assert "retained" in text and R in text
        assert marker not in text, text


UNREG = "SYNTH_UNREGISTERED_99"


def _unreg_samples() -> dict:
    s = UNREG
    pad = "w " * 600
    return {
        "escaped_double": '/a?token=\\"' + s + '\\"', "escaped_single": "/a?token=\\'" + s + "\\'",
        "escaped_inner": '/a?token="prefix \\"' + s + '\\""', "json_quoted_url": json.dumps({"url": '/a?token="' + s + '"'}),
        "unmatched_double": '/a?token="' + s, "unmatched_single": "/a?token='" + s,
        "control_quoted": '/a?token="' + s + '"', "multiple_quoted": '/a?token="' + s + '"&sid="' + s + '"',
        "cap_quote_1014": '/a?token="' + pad[:1014] + s + '"', "cap_quote_1024": '/a?token="' + pad[:1024] + s + '"',
        "cap_quote_1025": '/a?token="' + pad[:1025] + s + '"', "cap_quote_1030": '/a?token="' + pad[:1030] + s + '"',
    }


@pytest.mark.parametrize("fmt", ["%s", "%r"])
@pytest.mark.parametrize("shape", ["str", "exception", "object_repr", "fstring"])
@pytest.mark.parametrize("name", sorted(_unreg_samples()))
def test_b4_unregistered_quoted_log(name, shape, fmt):
    """R3-B4: 미등록 인용형 12표본 × {str, 예외, 객체 repr, 미리 만든 f-string} × {%s, %r} — 원문 0."""
    from interface.mcp_server import install_public_log_filter

    assert len(secret_registry.REGISTRY) == 0
    text = _unreg_samples()[name]
    lg = logging.getLogger("ws51.r4.b4")
    lg.propagate = False
    lg.setLevel(logging.INFO)
    stream = io.StringIO()
    lg.handlers = [logging.StreamHandler(stream)]
    restore = install_public_log_filter()
    try:
        if shape == "fstring":
            lg.warning(f"x {text!r}" if fmt == "%r" else f"x {text}")
        else:
            arg = {"str": text, "exception": ValueError(text), "object_repr": [_Obj(text)]}[shape]
            lg.warning("x " + fmt, arg)
    finally:
        restore()
        lg.handlers = []
    out = stream.getvalue()
    assert out.startswith("x "), out
    assert UNREG not in out, out


@pytest.mark.parametrize("depth", [0, 1, 2, 3])
@pytest.mark.parametrize("escape", ["json", "repr", "mixed"])
def test_quote_escape_depth_property(depth, escape):
    """P1 백스톱 일반화: 인용 판정이 임의 깊이 백슬래시+따옴표 — 이스케이프 깊이 0~3 × 12표본 × 인코더."""
    for name, text in _unreg_samples().items():
        cur = text
        for level in range(depth):
            use = escape if escape != "mixed" else ("json" if level % 2 == 0 else "repr")
            cur = json.dumps(cur)[1:-1] if use == "json" else repr(cur)[1:-1]
        out = public_text(f"log: {cur} 끝")
        assert UNREG not in out, (name, depth, escape, out[:300])
        assert public_text(out) == out


def test_b2_registered_compound_representations_in_logs():
    """R3-B2: 등록한 특수 문자 값의 JSON·HTML·repr·소문자 퍼센트를 %r 로 다시 표현해도 원문 0(가린 뒤 표현)."""
    value = 'SYNTH_R3_\u00e9<&"\\+ /9'
    _registered(value)
    forms = secret_registry.representations(value)
    logs = "".join(_logs(form) for form in forms)
    assert leaks_in_text(logs, value) == []
    assert R in logs


def test_log_args_shape_and_identity_kept_when_clean():
    """같은 모양 정제: 가릴 것이 없으면 레코드의 msg·args 를 그대로 둔다(기존 소비자 호환)."""
    from interface.mcp_server import install_public_log_filter, log_safe_value

    clean = ("a", 1, None, [1, "b"], {"k": "v"})
    assert log_safe_value(clean) is clean
    _registered("SYNTHshapeVal01")
    nt = namedtuple("NT", "a b")("SYNTHshapeVal01", 2)
    out = log_safe_value(nt)
    assert type(out) is type(nt) and out.a == R and out.b == 2
    od = log_safe_value(OrderedDict(z="SYNTHshapeVal01"))
    assert isinstance(od, OrderedDict) and od["z"] == R
    records = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(record)

    lg = logging.getLogger("ws51.r4.shape")
    lg.propagate = False
    lg.handlers = [Keep()]
    restore = install_public_log_filter()
    try:
        lg.warning("plain %s %d", "ok", 3)
    finally:
        restore()
        lg.handlers = []
    assert records[0].msg == "plain %s %d" and records[0].args == ("ok", 3)


def test_log_limits_depth_items_length():
    from interface.mcp_server import LOG_MAX_DEPTH, LOG_MAX_ITEMS, LOG_MAX_STR, log_safe_value

    deep = cur = []
    for _ in range(LOG_MAX_DEPTH + 3):
        nxt: list = []
        cur.append(nxt)
        cur = nxt
    assert "REDACTED-DEPTH" in repr(log_safe_value(deep))
    assert "원소" in log_safe_value(list(range(LOG_MAX_ITEMS + 1)))
    assert "상한" in log_safe_value("x" * (LOG_MAX_STR + 1))


def test_excepthooks_installed_and_restored(capsys):
    from interface.mcp_server import install_public_log_filter

    before = (sys.excepthook, threading.excepthook)
    _registered("SYNTHhookValue1")
    out = _excepthooks("세션 SYNTHhookValue1 만료", capsys)
    assert "SYNTHhookValue1" not in out and "ValueError" in out and "Exception in thread" in out
    assert (sys.excepthook, threading.excepthook) == before
    restore = install_public_log_filter()
    try:
        assert sys.excepthook is not before[0]
    finally:
        restore()


def test_p1_record_cost_measured():
    """§8: P1 레코드당 비용(짧은 문자열 인자 3개 + dict extra). 보고서 수치 — 상한은 넉넉히(회귀 감지용)."""
    from interface.mcp_server import install_public_log_filter

    for i in range(200):
        secret_registry.register_url(f"{ORIGIN}/p?gateToken=SYNTH{i:04d}abcdefXYZ")
    lg = logging.getLogger("ws51.r4.cost")
    lg.propagate = False
    lg.handlers = [logging.NullHandler()]
    lg.setLevel(logging.DEBUG)
    n = 2000
    restore = install_public_log_filter()
    try:
        t = time.perf_counter()
        for i in range(n):
            lg.debug("요청 %s 상태 %s 경로 %s", "observe", i, "/a?q=1", extra={"meta": {"k": "v", "n": i}})
        per = (time.perf_counter() - t) / n
    finally:
        restore()
        lg.handlers = []
    print(json.dumps({"p1_record_cost_us": round(per * 1e6, 1)}))
    assert per < 0.002, per


# ------------------------------------------------------------------ P4·P5 긴 값·용량(R3-B5)


def test_b5_long_value_registered_and_masked():
    """R3-B5: 65,545자 값도 등록(정확 저장) → 제목 0. 잘린 값·마지막 글자 변경 → 원문 연속 256자 이상 0."""
    value = "z" * 65536 + "SYNTHEND8"
    secret_registry.register_url(f"{ORIGIN}/seen?token={value}")
    assert len(secret_registry.REGISTRY) == 1
    for text in (value, "title " + value[:40000] + "\u2026", value[:-1] + "X", "x" + value[1000:30000] + "y"):
        out = public_text(text)
        assert leaks_in_text(out, value) == [], (len(text), out[:200])
    rnd_value = VALUES["long64k"]
    secret_registry.register_url(_seen_url(rnd_value))
    for text in (rnd_value, rnd_value[:40000] + "\u2026", rnd_value[:-1] + "X", "앞 " + rnd_value[777:5000] + " 뒤",
                 rnd_value[:30000] + "MIDDLE" + rnd_value[30006:]):
        out = public_text(text)
        assert leaks_in_text(out, rnd_value) == [], out[:200]


def test_p5_capacity_by_values_not_variants():
    """리뷰 신규: 특수 문자 값(표현 30개 안팎) 9개 이상 등록해도 첫 값이 계속 가려진다."""
    first = 'SYNTH_first_\u00e9<&"\\+ /0'
    secret_registry.register_url(_seen_url(first))
    for i in range(20):
        secret_registry.register_url(_seen_url(f'SYNTH_more{i:02d}_\u00e9<&"\\+ /9'))
    assert len(secret_registry.REGISTRY) == 21
    for rep in secret_registry.representations(first):
        assert leaks_in_text(public_text(f"x {rep} y"), first) == [], rep


def test_p5_lru_value_cap_and_char_budget():
    reg = secret_registry.SecretRegistry(cap=4, max_chars=10_000)
    for i in range(6):
        reg.add_values([f"SYNTHcap{i:04d}xx"])
    assert len(reg) == 4 and reg.mask("SYNTHcap0000xx") == "SYNTHcap0000xx" and reg.mask("SYNTHcap0005xx") == R
    big = secret_registry.SecretRegistry(cap=256, max_chars=20_000)
    big.add_values(["A" + _ascii(i, 6000) for i in range(5)])
    assert big.chars <= 20_000 and len(big) < 5  # 글자 예산으로 밀려남
    assert repr(big).count("values hidden") == 1


def _ascii(seed: int, n: int) -> str:
    import random
    import string

    rnd = random.Random(seed)
    return "".join(rnd.choice(string.ascii_letters) for _ in range(n))


def test_p4_short_values_tokenized_and_bounded():
    """4~7자: 영숫자뿐이면 덩어리 집합 조회, 그 밖의 글자가 있으면 find + 경계. 영숫자 인접은 남는다(NB)."""
    secret_registry.register_url(f"{ORIGIN}/x?code=SYN7X2&sid=Zq7-k2&otp=1234")
    assert public_text("코드 SYN7X2, Zq7-k2 그리고 1234!") == f"코드 {R}, {R} 그리고 {R}!"
    assert public_text("wSYN7X2 aZq7-k2b 12345") == "wSYN7X2 aZq7-k2b 12345"
    assert public_text("Zq7-k2Zq7-k2") == "Zq7-k2Zq7-k2"  # 앞뒤가 영숫자(경계 실패)
    assert public_text("x_SYN7X2_y") == f"x_{R}_y"  # `_` 는 경계(str.isalnum 판정)


# ------------------------------------------------------------------ P3·P8 주소 칸(R3-B1)


BENIGN_URLS = [
    "https://shop.example.test/products/2026/list?page=2&sort=price",
    "https://shop.example.test/archive/2026/10/16/",
    "https://news.example.test/article/12345-local-election-results",
    "https://search.example.test/?q=camping+sites&lang=ko",
    "https://maps.example.test/@37.5665,126.9780,15z",
    "https://docs.example.test/guide/getting-started#installation",
    "https://docs.example.test/api/v2/users?limit=50&offset=100",
    "https://blog.example.test/2026/10/how-to-cook-rice",
    "https://cdn.example.test/assets/app.min.js?v=3.2.1",
    "https://example.test/login?next=/dashboard",
    "https://example.test/cart?item=1234&qty=2",
    "https://example.test/user/alice/profile",
    "https://example.test/search?query=hello+world&page=3",
    "https://example.test/ko/help/faq#shipping",
    "https://example.test/files/report-2026-q3.pdf",
    "https://video.example.test/watch?v=dQw4w9WgXcQ&t=42",
    "https://example.test/category/electronics/phones?brand=acme",
    "https://example.test/events/2026-10-16/seoul",
    "https://example.test/compare?a=1001&b=1002",
    "https://example.test/tags/python,rust,go",
    "https://example.test/~user/index.html",
    "https://example.test/path/with%20space/file.txt",
    "https://example.test/%ED%95%9C%EA%B8%80/%EB%AC%B8%EC%84%9C",
    "https://example.test/booking?date=2026-10-16&guests=2&room=deluxe",
    "https://example.test/redirect?url=https%3A%2F%2Fother.test%2Fpage",
    "https://sub.domain.example.test:8443/a/b/c?x=1",
    "http://127.0.0.1:8080/health",
    "http://localhost:3000/app/settings",
    "http://[::1]:9000/status",
    "about:blank",
    "data:text/plain,hello",
    "https://example.test/p?id=550e8400-e29b-41d4-a716-446655440000",
    "https://example.test/feed.xml?format=rss&count=20",
    "https://example.test/products?color=red&size=M&size=L",
    "https://example.test/a;b;c/d",
    "https://example.test/weather/seoul/2026/10",
    "https://example.test/order/history?year=2026",
    "https://example.test/jobs?role=engineer&remote=true",
    "https://example.test/catalog?page=12&per_page=48",
    "https://example.test/blog/archive/2026",
    "https://example.test/download/v1.2.3/setup.exe",
    "https://example.test/forum/thread/98765?page=4#post-12",
    "https://example.test/en-us/store/b/laptops",
    "https://example.test/item/B08N5WRWNW",
    "https://example.test/hotel/seoul-grand?checkin=2026-10-20&checkout=2026-10-22",
    "https://example.test/flights?from=ICN&to=NRT&date=2026-11-01",
    "https://example.test/recipes/kimchi-jjigae",
    "https://example.test/u/1234/photos?album=summer",
    "https://example.test/settings/notifications#email",
    "https://example.test/static/img/logo@2x.png",
    "https://example.test/movies/now-playing?region=KR",
    "https://example.test/api/search?q=%EA%B9%80%EC%B9%98&sort=recent",
]


def test_b1_benign_urls_preserved_with_registry_loaded():
    """정상 주소 ≥ 50개: 레지스트리가 찬 상태(정상 4~7자 값 포함)에서도 주소 칸 결과가 public_url 만 거친 것과 같다
    (ca09586 은 주소 칸에 레지스트리를 쓰지 않았다 — 나빠지지 않음)."""
    assert len(BENIGN_URLS) >= 50
    for i in range(40):
        secret_registry.register_url(f"{ORIGIN}/p?gateToken=SYNTH{i:04d}abcdefXYZ&sid=s{i:06d}x")
    for short in ("2026", "1234", "48", "12", "10", "v=3"):
        secret_registry.register_url(f"{ORIGIN}/cb?code={short}")
    assert len(secret_registry.REGISTRY) >= 80
    changed = []
    for url in BENIGN_URLS:
        out = public_payload({"current_url": url})["current_url"]
        if out != public_url(url):
            changed.append((url, out))
    assert changed == []


def test_b1_registered_value_in_url_fields():
    """`/seen?token=V` 후 `/alias?ref=V`·`/V`·`http://v.localhost/`(호스트 라벨, 소문자화)·퍼센트 꼴 — 주소 칸 원문 0."""
    v = "SYNTH83Secret"
    secret_registry.register_url(f"{ORIGIN}/seen?token={v}")
    cases = {
        "alias": (f"{ORIGIN}/alias?ref={v}", f"{ORIGIN}/alias?ref={R}"),
        "path": (f"{ORIGIN}/{v}", f"{ORIGIN}/{R}"),
        "host_lower": (f"http://{v.lower()}.localhost:1/", f"http://{R}.localhost:1/"),
        "host_same": (f"http://{v}.localhost:1/x", f"http://{R}.localhost:1/x"),
        "frag": (f"{ORIGIN}/a#{v}", f"{ORIGIN}/a#{R}"),
        "about": (f"about:blank#{v}", f"about:blank#{R}"),
        "relative": (f"/p/{v}/q", f"/p/{R}/q"),
    }
    for name, (url, expected) in cases.items():
        out = public_payload({"current_url": url, "data": {"url": url}})
        assert out["current_url"] == expected and out["data"]["url"] == expected, (name, out)
    special = "SYNTH+case/\u00e9&99"
    secret_registry.register_url(f"{ORIGIN}/seen?token=" + quote(special, safe=""))
    for form in secret_registry.url_representations(special):
        out = public_payload({"current_url": f"{ORIGIN}/alias?ref={form}"})["current_url"]
        assert leaks_in_text(out, special) == [], (form, out)


def test_b1_short_values_not_applied_to_url_fields():
    """현재 동작 고정(§1.6): 4~7자·순수 숫자 값은 주소 칸에 쓰지 않는다 — `/archive/2026/` 보존, 이동 거부 없음."""
    from security.public_url import has_redaction_mark

    secret_registry.register_url(f"{ORIGIN}/cb?code=2026&sid=Zq7-k2")
    url = f"{ORIGIN}/archive/2026/Zq7-k2/list"
    out = public_payload({"current_url": url})["current_url"]
    assert out == url and not has_redaction_mark(out)
    assert public_text(f"보관 2026 / Zq7-k2") == f"보관 {R} / {R}"  # 자유 문장 run 일치는 적용


def test_p8_long_url_field_cache_tracks_registry_version():
    """P8: 16KiB 이상 주소 칸 결과 캐시도 레지스트리 버전을 키에 — 등록 뒤 옛 결과를 돌려주지 않는다."""
    v = "SYNTHlongUrlVal1"
    url = f"{ORIGIN}/alias/{v}?pad=" + "w" * (20 * 1024)
    assert public_payload({"current_url": url})["current_url"] == url
    secret_registry.register_url(f"{ORIGIN}/seen?token={v}")
    assert v not in public_payload({"current_url": url})["current_url"]


def test_p8_long_text_cache_tracks_registry_version():
    v = "SYNTHlongTxtVal1"
    text = "w " * 10_000 + v
    assert public_text(text).endswith(v)
    secret_registry.register_url(f"{ORIGIN}/seen?token={v}")
    assert public_text(text).endswith(R)


# ------------------------------------------------------------------ 범위 밖 — 현재 동작 고정(§1.3·§1.6)


def test_out_of_scope_forms_current_behavior():
    import base64
    import unicodedata

    v = "SYNTHscope\u00e9Val"
    secret_registry.register_url(_seen_url(v))
    b64 = base64.b64encode(v.encode()).decode()
    assert public_text(f"x {b64} y") == f"x {b64} y"                         # base64
    mixed = quote(v, safe="").replace("%C3", "%c3")                          # 대소문자 섞인 16진
    assert public_text(f"x {mixed} y") == f"x {mixed} y"
    nfd = unicodedata.normalize("NFD", v)                                    # NFC 로 본 값의 NFD 꼴
    assert public_text(f"x {nfd} y") == f"x {nfd} y"
    ellipsis = v[:4] + "\u2026" + v[-4:]                                     # 말줄임
    assert public_text(f"x {ellipsis} y") == f"x {ellipsis} y"
    assert public_text("x login y") == "x login y"                           # 4~7자 순수 알파벳(미등록)


def test_out_of_scope_subframe_navigation_not_registered():
    """switch_frame 전 하위 프레임 이동은 범위 밖(§1.1) — 등록하지 않는다(현재 동작 고정)."""
    ctx, page = _Emitter(), _Emitter()
    secret_registry.watch_browser_context(ctx)
    ctx.emit("page", page)
    page.emit("framenavigated", _Frame(f"{ORIGIN}/if?token=SYNTHsubFrame01", parent=_Frame("")))
    assert len(secret_registry.REGISTRY) == 0


def test_registry_values_never_serialized():
    """§1.5: 레지스트리 값은 repr·pickle·copy·JSON 어디에도 직렬화되지 않는다."""
    import copy
    import pickle

    secret_registry.register_url(_seen_url("SYNTHnoSerial01"))
    reg = secret_registry.REGISTRY
    for text in (repr(reg), str(reg), f"{reg}", repr(reg._snapshot.__class__)):
        assert "SYNTHnoSerial01" not in text
    for fn in (pickle.dumps, copy.copy, copy.deepcopy, json.dumps):
        with pytest.raises(TypeError):
            fn(reg)
