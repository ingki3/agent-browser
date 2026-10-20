"""WS-51 R6 — R5 검증 BLOCKING 3건(R5-F1~F3)과 바로 옆 (브라우저 없음, 합성 값).

* R5-F1: top-level extra 의 **비문자열 키**(int·float·Decimal·IntEnum·bytes·tuple) — 레코드 속성 이름이 중첩 키
  (`_log_safe_key`)와 같은 정책으로 가려지고, JSON 형 Formatter 출력(Stream·File)에 원문 0.
* R5-F2: 문자열 extra 키는 **재명명하지 않는다** — 기존 Formatter 의 `%(name)s`·`{name}`·`${name}` 조회가 훅이 없을
  때와 같은 출력(비밀 아닌 값). 키 이름이 비밀이면 출력 단계(Handler.format 결과·JSON 키 직렬화·handleError 진단)에서
  원문 0. 메시지가 비거나 형식 오류가 나는 것은 누출 0 이 아니라 기능 회귀다.
* R5-F3: `@sN`(vision/bridge.py SOM_ID_PREFIX) 과 census 일반화로 드러난 도구 식별자(레시피 자동 매개변수 이름
  `textN`·`choiceN`, 이동 중단 신호 `nav_aborted`) — 등록 제외 + 제어 면제.

검증자 재현(`verify51-r5/actual_log.py`·`compare_keys.py`·`controls/som_id_actual.py`)과 같은 조건. 실 Chromium 쪽은
tests/interface/test_ws51_r6_e2e.py.
"""

from __future__ import annotations

import contextlib
import decimal
import enum
import io
import json
import logging
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

import pytest

from interface.mcp_server import install_public_log_filter
from security import secret_registry as sr
from security.public_url import public_payload, public_text

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ws51_r4_leak import leaks_in_text  # noqa: E402

R = "[REDACTED]"


@pytest.fixture(autouse=True)
def _fresh_registry():
    sr.clear()
    yield
    sr.clear()


def _pu():
    return sys.modules["security.public_url"]


class _ExtraJSON(logging.Formatter):
    """검증자 재현과 같은 JSON 형 Formatter — record 의 extra 속성(키·값)을 json 으로(default=str)."""

    def format(self, record):  # noqa: A003
        base = logging.makeLogRecord({}).__dict__
        extra = {k: v for k, v in vars(record).items() if k not in base and k not in ("message", "asctime")}
        return record.getMessage() + " " + json.dumps(extra, default=str)


class _Records(logging.Handler):
    """레코드를 그대로 모은다(속성 이름 검사용)."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _emit(tmp_path: Path, fmt: logging.Formatter, msg="retained", *args, extra=None, install=True):
    """Stream·File 두 handler 로 한 줄 남기고 (stream, file, stderr, records)."""
    stream = io.StringIO()
    err = io.StringIO()
    a = logging.StreamHandler(stream)
    path = tmp_path / f"log-{len(list(tmp_path.iterdir()))}.log"
    b = logging.FileHandler(path, mode="w", encoding="utf-8")
    keep = _Records()
    a.setFormatter(fmt)
    b.setFormatter(fmt)
    log = logging.getLogger("ws51.r6.findings")
    log.handlers = [a, b, keep]
    log.propagate = False
    log.setLevel(logging.INFO)
    restore = install_public_log_filter() if install else (lambda: None)
    try:
        with contextlib.redirect_stderr(err):
            log.info(msg, *args, extra=extra)
    finally:
        restore()
        log.handlers = []
        a.close()
        b.close()
    return stream.getvalue(), path.read_text(encoding="utf-8"), err.getvalue(), keep.records


def _no_form(text: str, secret: str) -> bool:
    return not any(form in text for form in sr.representations(secret))


# ------------------------------------------------------------------ R5-F1 top-level 비문자열 키


class _Num(enum.IntEnum):
    SECRET = 98574321


#: (이름, 등록 값, extra 키) — 비문자열 top-level 키. str(키)·repr(키) 에 등록 값이 그대로 든다(조건 성립).
_NONSTR_KEYS = [
    ("int", "98574321", 98574321),
    ("float", "98574321", 98574321.5),
    ("decimal", "98574321", decimal.Decimal("98574321")),
    ("int_enum", "98574321", _Num.SECRET),
    ("bytes", "SYNTHbyteskey9", b"SYNTHbyteskey9"),
    ("tuple", "98574321", (1, 98574321)),
]


@pytest.mark.parametrize("name,secret,key", _NONSTR_KEYS, ids=[n for n, _, _ in _NONSTR_KEYS])
def test_f1_nonstring_toplevel_key_record_attribute_masked(tmp_path, name, secret, key):
    """레코드 속성 이름(키)이 중첩 키와 같은 정책으로 가려진다 — 원 키 객체가 레코드에 남지 않는다."""
    sr.REGISTRY.add_values([secret])
    *_, plain_records = _emit(tmp_path, _ExtraJSON(), extra={key: "value"}, install=False)
    assert key in plain_records[0].__dict__  # 조건 성립: 훅이 없으면 원 키 그대로
    *_, records = _emit(tmp_path, _ExtraJSON(), extra={key: "value"})
    attrs = records[0].__dict__
    assert key not in attrs, name
    assert all(_no_form(str(k), secret) and _no_form(repr(k), secret) for k in attrs), (name, list(attrs))
    assert "value" in attrs.values()  # 값은 남는다


@pytest.mark.parametrize("name,secret,key", [k for k in _NONSTR_KEYS if k[0] in ("int", "float", "int_enum")],
                         ids=["int", "float", "int_enum"])
def test_f1_nonstring_toplevel_key_json_output_masked(tmp_path, name, secret, key):
    """검증자 재현 조건: JSON 형 Formatter × Stream·File — 원문 0, 값·메시지는 남는다, stderr 진단 없음."""
    sr.REGISTRY.add_values([secret])
    plain, *_ = _emit(tmp_path, _ExtraJSON(), extra={key: "value"}, install=False)
    assert secret in plain  # 조건 성립
    line, file, err, _ = _emit(tmp_path, _ExtraJSON(), extra={key: "value"})
    for out in (line, file, err):
        assert _no_form(out, secret), (name, out)
    for out in (line, file):
        assert out.startswith("retained ") and '"value"' in out, (name, out)
    assert err == ""


def test_f1_policy_same_as_nested_key(tmp_path):
    """top-level 과 중첩 dict 키가 한 정책 — 같은 숫자 키가 두 자리에서 같은 표지가 된다."""
    from interface.mcp_server import _log_safe_key

    sr.REGISTRY.add_values(["98574321"])
    *_, records = _emit(tmp_path, _ExtraJSON(), extra={98574321: "top", "metadata": {98574321: "nested"}})
    attrs = records[0].__dict__
    nested_key = next(iter(attrs["metadata"]))
    top_key = next(k for k, v in attrs.items() if v == "top")
    assert top_key == nested_key == _log_safe_key(98574321, 0) == R


def test_f1_unmasked_nonstring_key_unchanged(tmp_path):
    """가려지지 않는 비문자열 키는 그대로(같은 객체) — 출력도 훅이 없을 때와 같다."""
    sr.REGISTRY.add_values(["98574321"])
    plain, *_ = _emit(tmp_path, _ExtraJSON(), extra={4242: "value"}, install=False)
    line, _, err, records = _emit(tmp_path, _ExtraJSON(), extra={4242: "value"})
    assert 4242 in records[0].__dict__
    assert line == plain and err == ""
    for key in (b"plainkey", uuid.UUID(int=7)):  # 객체 키(안전 래퍼 대상)도 가려지지 않으면 원 객체 그대로
        *_, records = _emit(tmp_path, _ExtraJSON(), extra={key: "value"})
        assert any(k is key for k in records[0].__dict__), key
        *_, records = _emit(tmp_path, _ExtraJSON(), extra={"metadata": {key: "value"}})
        assert next(iter(records[0].__dict__["metadata"])) is key  # 중첩 키도 같은 정책


#: 값 단계(레코드) — 출력 단계(handler_format)와 독립. Formatter 를 거치지 않고 레코드를 직접 읽는 handler(Queue·
#: 사용자 handler)도 원문을 못 본다. (값, 등록 값)
_RECORD_VALUES = [(98574321, "98574321"), (98574321.5, "98574321"), (decimal.Decimal("98574321"), "98574321"),
                  (b"SYNTHbytes8765", "SYNTHbytes8765"), ({"k": 98574321}, "98574321"), ({98574321: "v"}, "98574321"),
                  ([1, 98574321], "98574321"), ("tok SYNTHplain987", "SYNTHplain987")]


@pytest.mark.parametrize("value,secret", _RECORD_VALUES, ids=[type(v).__name__ + str(i) for i, (v, _) in
                                                              enumerate(_RECORD_VALUES)])
def test_value_layer_record_args_and_extra_masked(tmp_path, value, secret):
    sr.REGISTRY.add_values([secret])
    *_, records = _emit(tmp_path, _ExtraJSON(), "m %s", value, extra={"metadata": value})
    rec = records[0]
    for text in (rec.getMessage(), repr(rec.args), str(rec.__dict__["metadata"]), repr(rec.__dict__["metadata"])):
        assert _no_form(text, secret), text


# ------------------------------------------------------------------ R5-F2 문자열 키 재명명 금지 · 출력 단계 보호

_STD_FORMATS = [
    ("percent", lambda k: logging.Formatter("%(message)s %(" + k + ")s")),
    ("brace", lambda k: logging.Formatter("{message} {" + k + "}", style="{")),
    ("dollar", lambda k: logging.Formatter("${message} ${" + k + "}", style="$")),
]


@pytest.mark.parametrize("style,make", _STD_FORMATS, ids=[s for s, _ in _STD_FORMATS])
@pytest.mark.parametrize("key", ["metadata", "SYNTHkey98765"])
def test_f2_secret_key_name_standard_formatter_works(tmp_path, style, make, key):
    """검증자 재현: 키 이름이 등록 값 + 표준 Formatter 가 그 키를 조회 — 정상 출력(훅이 없을 때와 같음), 진단 없음."""
    sr.register_url("http://127.0.0.1/seen?token=" + key)
    assert len(sr.REGISTRY) == 1 and public_text(key) == R  # 조건 성립
    plain, *_ = _emit(tmp_path, make(key), extra={key: "value"}, install=False)
    assert plain == "retained value\n"
    line, file, err, records = _emit(tmp_path, make(key), extra={key: "value"})
    assert line == plain and file == plain, (line, file)
    assert err == "", err  # KeyError·'Formatting field not found' 진단 없음
    assert records[0].__dict__[key] == "value"  # 속성 이름 그대로(재명명 금지)


@pytest.mark.parametrize("key", ["metadata", "SYNTHkey98765", "98574321"])
def test_f2_secret_key_name_json_output_masked(tmp_path, key):
    """JSON 형 Formatter 가 키 이름을 찍는 경로 — 출력 단계에서 원문 0, 값은 남는다."""
    sr.register_url("http://127.0.0.1/seen?token=" + key)
    plain, *_ = _emit(tmp_path, _ExtraJSON(), extra={key: "value"}, install=False)
    assert key in plain  # 조건 성립
    line, file, err, _ = _emit(tmp_path, _ExtraJSON(), extra={key: "value"})
    for out in (line, file):
        assert _no_form(out, key) and leaks_in_text(out, key) == [], out
        assert out.startswith("retained ") and '"value"' in out, out
    assert err == ""


def test_f2_handle_error_diagnostic_masked(tmp_path):
    """Formatter 오류 진단(handleError → stderr) — 원문 0, 진단 자체는 남는다(조용히 버리지 않는다)."""
    key = "SYNTHmissing987"
    sr.register_url("http://127.0.0.1/seen?token=" + key)
    fmt = logging.Formatter("%(message)s %(" + key + ")s")  # 없는 속성 — KeyError
    _, _, plain_err, _ = _emit(tmp_path, fmt, install=False)
    assert key in plain_err  # 조건 성립: 진단에 키 이름 원문
    line, file, err, _ = _emit(tmp_path, fmt)
    assert "--- Logging error ---" in err and "KeyError" in err, err
    assert err.count("--- Logging error ---") == 2  # Stream·File 각각
    assert _no_form(err, key), err
    assert line == "" and file == ""


def test_f2_handle_error_emit_exception_masked(tmp_path):
    """handler 의 emit 이 비밀이 든 예외를 내는 경로 — traceback 진단 원문 0."""
    secret = "SYNTHemitboom98"
    sr.REGISTRY.add_values([secret])

    class Boom(logging.Handler):
        def emit(self, record):
            try:
                raise RuntimeError("sink failed for " + secret)
            except Exception:  # noqa: BLE001
                self.handleError(record)

    err = io.StringIO()
    log = logging.getLogger("ws51.r6.boom")
    log.handlers, log.propagate = [Boom()], False
    restore = install_public_log_filter()
    try:
        with contextlib.redirect_stderr(err):
            log.warning("x")
    finally:
        restore()
        log.handlers = []
    text = err.getvalue()
    assert "--- Logging error ---" in text and "RuntimeError" in text, text
    assert _no_form(text, secret), text


def test_f2_raise_exceptions_false_silent(tmp_path, monkeypatch):
    """logging.raiseExceptions=False 면 진단 없음 — 훅이 없을 때와 같다."""
    key = "SYNTHmissing987"
    sr.REGISTRY.add_values([key])
    monkeypatch.setattr(logging, "raiseExceptions", False)
    fmt = logging.Formatter("%(message)s %(" + key + ")s")
    *_, plain_err, _ = _emit(tmp_path, fmt, install=False)
    *_, err, _ = _emit(tmp_path, fmt)
    assert plain_err == "" and err == ""


def test_f2_hooks_uninstalled():
    """설치·해제 대칭 — 해제 뒤 Handler.format·handleError 는 원래 함수."""
    fmt0, err0 = logging.Handler.format, logging.Handler.handleError
    restore = install_public_log_filter()
    assert logging.Handler.format is not fmt0 and logging.Handler.handleError is not err0
    restore()
    assert logging.Handler.format is fmt0 and logging.Handler.handleError is err0


#: 비밀 아닌 값 — 기존 Formatter 출력이 훅이 없을 때(= dd9d3aa)와 같아야 하는 표본.
_BENIGN = [
    ("std_extra", "%(levelname)s %(name)s %(message)s %(metadata)s", "%", "n=%d", (42,), {"metadata": "value"}),
    ("brace_extra", "{levelname} {message} {metadata}", "{", "plain", (), {"metadata": {"a": 1}}),
    ("dollar_extra", "${message} ${metadata}", "$", "plain %s", ("arg",), {"metadata": [1, 2.5]}),
    ("url_free", "%(message)s", "%", "go %s", ("http://127.0.0.1/a?page=2",), None),
    ("number_spec", "%(message)s %(count)05d", "%", "c", (), {"count": 7}),
    ("key_snake", "%(message)s %(request_meta)s", "%", "m", (), {"request_meta": "ok"}),
    ("unicode", "%(message)s %(meta)s", "%", "한글 %s", ("값",), {"meta": "é"}),
]


@pytest.mark.parametrize("name,fmt,style,msg,args,extra", _BENIGN, ids=[b[0] for b in _BENIGN])
def test_f2_benign_formatter_output_unchanged(tmp_path, name, fmt, style, msg, args, extra):
    sr.REGISTRY.add_values(["98574321", "SYNTHother99"])  # 레지스트리가 비어 있지 않은 조건
    formatter = logging.Formatter(fmt, style=style)
    plain = _emit(tmp_path, formatter, msg, *args, extra=extra, install=False)
    hooked = _emit(tmp_path, formatter, msg, *args, extra=extra)
    assert hooked[:3] == plain[:3], (hooked[:3], plain[:3])
    for json_out in (_emit(tmp_path, _ExtraJSON(), msg, *args, extra=extra, install=False)[0],):
        assert _emit(tmp_path, _ExtraJSON(), msg, *args, extra=extra)[0] == json_out


# ------------------------------------------------------------------ R5-F3 @sN · census 로 드러난 식별자


@pytest.mark.parametrize("value", ["@s10", "@s1", "@s99", "@s123456"])
def test_f3_som_id_tool_token_not_registered(value):
    assert sr.is_tool_token(value)
    sr.register_url("http://127.0.0.1/seen?token=" + quote(value, safe=""))
    assert len(sr.REGISTRY) == 0


@pytest.mark.parametrize("key", ["element_id", "trigger_element_id"])
def test_f3_som_id_control_value_kept(monkeypatch, key):
    monkeypatch.setattr(sr, "is_tool_token", lambda _v: False)  # 등록 제외가 깨진 경우(이중 방어 — 제어 키만으로)
    sr.REGISTRY.add_values(["@s10"])
    assert public_text("x @s10 y") == f"x {R} y"  # 조건 성립: 값이 실제로 등록됐다
    payload = {"data": {key: "@s10"}}
    assert public_payload(payload) == payload
    assert _pu().control_value(key, "@s10")


def test_f3_som_id_format_matches_producer():
    from vision.bridge import SOM_ID_PREFIX, _next_id

    class Engine:
        handles = {f"{SOM_ID_PREFIX}{n}": None for n in range(1, 10)}

    produced = _next_id(Engine())
    assert produced == "@s10" and sr.TOOL_ID_FORMATS["element_id"].fullmatch(produced)


def test_f3_other_shapes_still_masked():
    """형식이 다르면 정제(방어) — `@s` 뒤가 숫자가 아니거나 다른 칸."""
    sr.REGISTRY.add_values(["@sx10"])
    out = public_payload({"data": {"element_id": "@sx10", "note": "@sx10"}})
    assert out["data"]["element_id"] == R and out["data"]["note"] == R


@pytest.mark.parametrize("value", ["text1", "text12", "choice1", "choice30"])
def test_f3_recipe_auto_param_names_not_registered_and_kept(monkeypatch, value):
    """레시피 자동 매개변수 이름(`textN`·`choiceN` — recipes/store.py `f'{prefix}{counters[prefix]}'`)."""
    assert sr.is_tool_token(value)
    sr.register_url("http://127.0.0.1/seen?token=" + value)
    assert len(sr.REGISTRY) == 0
    monkeypatch.setattr(sr, "is_tool_token", lambda _v: False)  # 이중 방어 — 제어 키(recipe.params)만으로
    sr.REGISTRY.add_values([value])
    assert public_text(f"x {value} y") == f"x {R} y"
    payload = {"data": {"recipe": {"params": [value, "query"]}}}
    assert public_payload(payload)["data"]["recipe"]["params"][0] == value


def test_f3_recipe_auto_param_name_matches_producer():
    from recipes.store import AUTO_CHOICE_PREFIX, AUTO_PARAM_PREFIX

    for prefix in (AUTO_PARAM_PREFIX, AUTO_CHOICE_PREFIX):
        assert sr.TOOL_ID_FORMATS["param_name"].fullmatch(f"{prefix}{7}")


@pytest.mark.parametrize("value", ["download", "request_failed", "status_204", "status_205"])
def test_f3_nav_aborted_signal_kept(value):
    """이동 중단 신호(`nav_aborted` — actions/dispatcher.py `f'status_{status}'`·download·request_failed)."""
    sr.REGISTRY.add_values([value])
    payload = {"data": {"nav_aborted": value}}
    assert public_payload(payload) == payload


def test_f3_nav_aborted_vocabulary_matches_producer():
    """어휘가 생산자(_NavWatch.aborted: download·request_failed·f'status_{s}' for s in _NO_DOCUMENT_STATUS)와 같다."""
    from actions.dispatcher import _NO_DOCUMENT_STATUS

    assert _pu().CONTROL_VOCABULARY["nav_aborted"] == frozenset(
        {"download", "request_failed"} | {f"status_{s}" for s in _NO_DOCUMENT_STATUS})


def test_f3_nav_aborted_other_value_masked():
    sr.REGISTRY.add_values(["SYNTHabort987"])
    assert public_payload({"data": {"nav_aborted": "SYNTHabort987"}})["data"]["nav_aborted"] == R
