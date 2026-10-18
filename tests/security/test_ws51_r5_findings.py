"""WS-51 R5 — R4 검증 BLOCKING 4건(R4-B1~B4)과 형제 (브라우저 없음, 합성 값).

* R4-B1: logging 의 **비문자열 리프**(int·float·Decimal·bytes·datetime·UUID·Enum·사용자 객체·dict 숫자 키)와 extra
  **속성 이름(키)** — 표준·JSON 형 Formatter 출력(Stream·File) 원문 0. 형식 지정자(`%d`·`%5.2f`·`%x`…)는 가려지지 않은
  숫자면 원래 출력과 같고, 가려진 값이면 형식 오류 없이 표지.
* R4-B2: 살아 있는 값의 representations(v) 전부가 가려진다(표현 일부만 예산에서 버리지 않는다 — 값 단위 원자적).
* R4-B3: 제어 키 면제는 키별 실제 열거 어휘(코드 상수)만 — 자유 문장 칸(reason 등)의 낱말 모양 비밀은 가린다.
* R4-B4: 도구 식별자(레시피 `r_` + 16진 8자리, 레시피 요소 `@rN`)는 등록 제외 + 제어 키(`id`·`recipe_id`) 값 불변.

검증자 재현(`verify51-r4/parent_log_extra.py`·`parent_security_probe.py`·`security/test_review_findings.py`)과 같은
조건을 저장소 테스트로 옮겼다. 실 Chromium 쪽은 tests/interface/test_ws51_r5_e2e.py.
"""

from __future__ import annotations

import datetime as dt
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
    return sys.modules["security.public_url"]  # `security.public_url` 속성은 같은 이름의 함수(재수출)


class _ExtraJSON(logging.Formatter):
    """검증자 재현과 같은 JSON 형 Formatter — record 의 extra 속성(키·값)을 json 으로."""

    def format(self, record):  # noqa: A003
        base = logging.makeLogRecord({}).__dict__
        extra = {k: v for k, v in vars(record).items() if k not in base and k not in ("message", "asctime")}
        return record.getMessage() + " " + json.dumps(extra, ensure_ascii=False, default=str)


def _emit(tmp_path: Path, fmt: logging.Formatter, msg, *args, extra=None, install=True):
    """Stream·File 두 handler 로 한 줄 남기고 (stream, file) 문자열."""
    stream = io.StringIO()
    a = logging.StreamHandler(stream)
    path = tmp_path / f"log-{len(list(tmp_path.iterdir()))}.log"
    b = logging.FileHandler(path, mode="w", encoding="utf-8")
    a.setFormatter(fmt)
    b.setFormatter(fmt)
    log = logging.getLogger("ws51.r5.findings")
    log.handlers = [a, b]
    log.propagate = False
    log.setLevel(logging.INFO)
    restore = install_public_log_filter() if install else (lambda: None)
    try:
        log.info(msg, *args, extra=extra)
    finally:
        restore()
        log.handlers = []
        a.close()
        b.close()
    return stream.getvalue().strip(), path.read_text(encoding="utf-8").strip()


# ------------------------------------------------------------------ R4-B1 로그 비문자열 리프·extra 키


class _Color(enum.StrEnum):
    SECRET = "SYNTHenum8765"


class _Num(enum.IntEnum):
    SECRET = 98765432


class _Obj:
    def __str__(self):
        return "obj SYNTHobj98765"

    __repr__ = __str__


#: (이름, 등록 값, extra 값) — 비문자열 리프 전 유형. 값의 str/repr 에 등록 값이 그대로 들어 있다(조건 성립).
_NONSTR = [
    ("int", "98765432", 98765432),
    ("int6", "876543", 876543),
    ("float", "98765432", 98765432.25),
    ("decimal", "98765432", decimal.Decimal("98765432.10")),
    ("bytes", "SYNTHbytes8765", b"SYNTHbytes8765"),
    ("datetime", "2026-10-17 01:02:03", dt.datetime(2026, 10, 17, 1, 2, 3)),
    ("uuid", "4f1c2a3b-5d6e-4f70-8a9b-0c1d2e3f4a5b", uuid.UUID("4f1c2a3b-5d6e-4f70-8a9b-0c1d2e3f4a5b")),
    ("str_enum", "SYNTHenum8765", _Color.SECRET),
    ("int_enum", "98765432", _Num.SECRET),
    ("object", "SYNTHobj98765", _Obj()),
    ("nested_int", "98765432", {"secret": 98765432}),
    ("dict_int_key", "98765432", {98765432: "key"}),
    ("list_int", "98765432", [1, 98765432]),
    ("tuple_float", "98765432", (98765432.5,)),
    ("set_int", "98765432", {98765432}),
]


@pytest.mark.parametrize("name,secret,meta", _NONSTR, ids=[n for n, _, _ in _NONSTR])
@pytest.mark.parametrize("style", ["std", "json"])
def test_b1_nonstring_extra_leaves_masked(tmp_path, name, secret, meta, style):
    sr.REGISTRY.add_values([secret])
    assert public_text(secret) == R  # 조건 성립: 등록 값
    fmt = logging.Formatter("%(message)s %(metadata)s") if style == "std" else _ExtraJSON()
    plain, _ = _emit(tmp_path, fmt, "retained", extra={"metadata": meta}, install=False)
    assert secret in plain, plain  # 조건 성립: 훅이 없으면 이 출력에 원문이 있다
    line, file = _emit(tmp_path, fmt, "retained", extra={"metadata": meta})
    for out in (line, file):
        assert out.startswith("retained"), out
        assert leaks_in_text(out, secret) == [], (name, out)
        assert R in out, (name, out)


def test_b1_numeric_extra_reproduction(tmp_path):
    """검증자 parent_actual_security 조건: `/seen?token=98765432` 등록 → 문자열·int·중첩 int 세 줄 모두 원문 0."""
    v = "98765432"
    sr.register_url("http://127.0.0.1/seen?token=" + v)
    assert len(sr.REGISTRY) == 1
    for meta in (v, int(v), {"secret": int(v)}):
        line, file = _emit(tmp_path, logging.Formatter("%(message)s %(metadata)s"), "retained",
                           extra={"metadata": meta})
        assert not any(r in line for r in sr.representations(v)), line
        assert not any(r in file for r in sr.representations(v)), file


@pytest.mark.parametrize("key", ["SYNTHExtraKey99", "98765432"])
def test_b1_extra_attribute_name_masked(tmp_path, key):
    """extra 속성 이름(키) 자체가 등록 값 — JSON 형 Formatter 출력에 원문 0, 값은 남는다."""
    sr.register_url("http://127.0.0.1/seen?token=" + key)
    assert len(sr.REGISTRY) == 1
    line, file = _emit(tmp_path, _ExtraJSON(), "retained", extra={key: "value"})
    for out in (line, file):
        assert leaks_in_text(out, key) == [], out
        assert '"value"' in out, out


def test_b1_extra_key_and_value_both_masked_json_without_default(tmp_path):
    """default=str 없는 json.dumps 도 동작(가린 숫자는 str — 직렬화 가능)."""

    class Plain(logging.Formatter):
        def format(self, record):  # noqa: A003
            return json.dumps({"m": getattr(record, "metadata")})

    sr.REGISTRY.add_values(["98765432"])
    line, _ = _emit(tmp_path, Plain(), "x", extra={"metadata": {98765432: 98765432, "n": 98765432.0}})
    assert "98765432" not in line, line
    assert json.loads(line)["m"]  # 직렬화 오류 없이


#: 형식 지정자 의미 유지 — 가려지지 않은 숫자는 훅이 없을 때와 출력이 같다.
_SPECS = [
    ("%d", (42,)), ("%5.2f", (3.14159,)), ("%x", (255,)), ("%X", (255,)), ("%05d", (7,)), ("%e", (12345.678,)),
    ("%o", (8,)), ("%c", (65,)), ("%+d", (5,)), ("%-6d|", (3,)), ("%g", (0.0001,)), ("%i", (9,)), ("%r", (10,)),
    ("%s", (1.5,)), ("%*d", (5, 42)), ("%.*f", (2, 3.14159)), ("%%d %d", (4,)), ("%(a)d %(b).1f", ({"a": 3, "b": 2.25},)),
    ("%d %5.2f", (decimal.Decimal("12.5"), decimal.Decimal("3.14159"))), ("%d", (True,)), ("%s", (None,)),
]


@pytest.mark.parametrize("fmt,args", _SPECS, ids=[f for f, _ in _SPECS])
def test_b1_format_specifiers_unchanged_for_unmasked_numbers(tmp_path, fmt, args):
    sr.REGISTRY.add_values(["98765432", "SYNTHother99"])  # 레지스트리가 비어 있지 않은 조건
    plain, _ = _emit(tmp_path, logging.Formatter("%(message)s"), fmt, *args, install=False)
    hooked, _ = _emit(tmp_path, logging.Formatter("%(message)s"), fmt, *args)
    assert hooked == plain, (hooked, plain)


_MASKED_SPECS = [
    ("n=%d", (98765432,)), ("n=%5.2f", (98765432.0,)), ("n=%x", (98765432,)), ("n=%05d", (876543,)),
    ("n=%e", (98765432,)), ("n=%i|%d", (98765432, 7)), ("n=%c", (98765432,)), ("n=%(a)d %(b)s", ({"a": 98765432, "b": 1},)),
    ("n=%d", (decimal.Decimal("98765432"),)), ("n=%.3f", (decimal.Decimal("98765432.5"),)),
    ("n=%s", (98765432,)), ("n=%r", (98765432,)), ("n=%s", ([98765432],)), ("n=%s", ({98765432: 1},)),
]


@pytest.mark.parametrize("fmt,args", _MASKED_SPECS, ids=[f for f, _ in _MASKED_SPECS])
def test_b1_masked_numbers_render_marker_without_format_error(tmp_path, fmt, args):
    sr.REGISTRY.add_values(["98765432", "876543"])
    line, file = _emit(tmp_path, logging.Formatter("%(message)s"), fmt, *args)
    for out in (line, file):
        assert "형식 오류" not in out and "가림 실패" not in out, out
        assert "98765432" not in out and "876543" not in out, out
        assert "5e30a78" not in out, out  # %x 꼴(16진)도 원 숫자에서 나오지 않는다
        assert out.startswith("n=") and R in out, out
    if fmt == "n=%i|%d":
        assert line.endswith("|7"), line  # 가려지지 않은 이웃 인자는 원래 형식


class _IntLike:
    """숫자로도 바뀌는 사용자 객체 — str 에 등록 값이 든다."""

    def __int__(self):
        return 98765432

    __index__ = __int__

    def __str__(self):
        return "intlike 98765432"

    __repr__ = __str__


@pytest.mark.parametrize("fmt", ["%(message)s %(metadata)d", "%(message)s %(metadata)x", "%(message)s %(metadata)s"])
def test_b1_masked_object_not_converted_to_number_by_formatter(tmp_path, capsys, fmt):
    """Formatter 가 extra 를 숫자 지정자로 찍어도 가려진 객체는 숫자로 바뀌지 않는다(원 숫자·16진 0 — 형식 오류는
    logging 의 오류 처리로, 원문 없이)."""
    sr.REGISTRY.add_values(["98765432"])
    line, file = _emit(tmp_path, logging.Formatter(fmt), "retained", extra={"metadata": _IntLike()})
    err = capsys.readouterr().err
    for out in (line, file, err):
        assert "98765432" not in out and "5e30a78" not in out, out


@pytest.mark.parametrize("fmt", ["n=%d", "n=%x", "n=%s"])
def test_b1_masked_object_in_args(tmp_path, fmt):
    sr.REGISTRY.add_values(["98765432"])
    line, _ = _emit(tmp_path, logging.Formatter("%(message)s"), fmt, _IntLike())
    assert "98765432" not in line and "5e30a78" not in line and "형식 오류" not in line, line
    assert line.startswith("n=") and R in line, line


def test_b1_unmasked_number_identity_kept():
    """가려지지 않은 숫자는 같은 객체 그대로(Formatter 의 `%(x)d` 등이 원래대로)."""
    from interface.mcp_server import log_safe_value

    sr.REGISTRY.add_values(["98765432"])
    for v in (7, 3.5, decimal.Decimal("1.25"), 10 ** 30, True, None):
        assert log_safe_value(v) is v
    assert log_safe_value({"n": 7}) == {"n": 7}


# ------------------------------------------------------------------ R4-B2 표현 예산 — 값 단위 원자적


def _live(value: str) -> bool:
    """값(어느 기저든 — 원문·unquote·unquote_plus)이 레지스트리에 살아 있는가."""
    return any(part.r1 == value for parts in sr.REGISTRY._order.values() for part in parts)


@pytest.mark.parametrize("repeats", [10000, 30000])
def test_b2_live_value_all_representations_masked(repeats):
    """검증자 parent_security_probe 조건: `'SYNTHé<&\"\\'/' * N` 을 등록 → 31표현 각각 제목 칸에서 원문 0."""
    v = "SYNTH\u00e9<&\"'/" * repeats
    sr.register_url("http://127.0.0.1/seen?token=" + quote(v, safe=""))
    assert _live(v) and len(sr.REGISTRY) == 1
    reps = sr.representations(v)
    assert len(reps) >= 20
    leaked = []
    for i, form in enumerate(reps):
        out = public_payload({"data": {"observation": {"title": form}}})["data"]["observation"]["title"]
        if form in out:
            leaked.append((i, len(form)))
    assert leaked == []


def test_b2_value_atomic_storage_invariant():
    """살아 있는 값은 등록 가능한 모든 표현을 저장한다(일부만 버리지 않는다). 글자 합은 저장분의 합과 같다."""
    reg = sr.SecretRegistry(cap=8, max_chars=2_000_000)
    values = ["SYNTH\u00e9<&\"'/" * n for n in (10, 1000, 30000, 50000)] + ["SYNTHsmall%02d" % i for i in range(6)]
    for v in values:
        reg.add_values([v])
        total = 0
        for live, parts in reg._order.items():
            for part in parts:
                want = [f for f in sr.representations(part.r1) if sr._registrable(f)]
                assert list(part.forms) == want, (len(live), len(part.forms), len(want))
                total += part.chars
        assert total == reg.chars
        assert reg.chars <= reg._max_chars or len(reg) == 1  # 예산 초과 단일 값은 혼자 남는다(통째로)
    assert values[-1] in reg._order  # 가장 최근 값은 언제나 산다


def test_b2_single_value_over_budget_kept_whole_and_masked():
    """값 하나의 전 표현이 글자 예산을 넘으면 그 값만 통째로 남기고(다른 값은 LRU 로 퇴출) 모든 표현을 가린다."""
    reg = sr.SecretRegistry(cap=8, max_chars=100_000)
    reg.add_values(["SYNTHolder123"])
    big = "SYNTH\u00e9<&\"'/" * 5000  # 표현 합이 100,000 을 넘는다
    reg.add_values([big])
    assert list(reg._order) == [big]
    for form in sr.representations(big):
        assert form not in reg.mask(form)


# ------------------------------------------------------------------ R4-B3 제어 면제 — 키별 실제 열거 어휘


@pytest.mark.parametrize("value", ["synthsecret99", "synth_secret_123", "secretword", "zz-secret-99"])
@pytest.mark.parametrize("key", ["reason", "status", "state", "holder", "kind", "mode", "op", "outcome", "risk",
                                 "changed", "sticky_reason", "reason_code", "presented_as", "browser_mode"])
def test_b3_word_shaped_secret_in_enum_key_masked(key, value):
    sr.register_url("http://127.0.0.1/seen?token=" + value)
    assert len(sr.REGISTRY) == 1
    out = public_payload({"data": {"control": {key: value}}})
    assert out["data"]["control"][key] == R, out


@pytest.mark.parametrize("key,value", [
    ("holder", "agent"), ("holder", "human"), ("status", "approved"), ("status", "pending"), ("status", "used"),
    ("outcome", "succeeded"), ("outcome", "outcome_unknown"), ("state", "headless"), ("state", "switching"),
    ("mode", "on-demand"), ("browser_mode", "user-chrome"), ("reason", "headless_ua_blocked"),
    ("reason", "request_expired"), ("reason", "http_404"), ("reason", "target_ambiguous"), ("kind", "captcha"),
    ("risk", "high"), ("op", "login_finish"), ("changed", "none_pending"), ("reason_code", "used"),
    ("presented_as", "chrome"),
])
def test_b3_enum_vocabulary_values_kept(key, value):
    sr.REGISTRY.add_values([value])  # 등록 제외가 깨진 경우까지(이중 방어)
    out = public_payload({"data": {"x": {key: value}}})
    assert out["data"]["x"][key] == value


def test_b3_free_text_keys_masked():
    """자유 문장 칸(reason·message·detail·note·hint·sticky_reason·error_message 등)은 낱말 모양 값도 가린다."""
    v = "synthsecret99"
    sr.register_url("http://127.0.0.1/seen?token=" + v)
    keys = ["reason", "message", "detail", "note", "hint", "sticky_reason", "error_message", "how_to_respond",
            "summary", "text", "description", "why", "label", "name", "title"]
    out = public_payload({"data": {k: v for k in keys}})
    assert all(out["data"][k] == R for k in keys), out


# ------------------------------------------------------------------ R4-B4 도구 식별자 — 레시피 id·@rN


@pytest.mark.parametrize("value", ["r_a93f3d09", "r_00000000", "@r1", "@r42"])
def test_b4_recipe_identifiers_not_registered(value):
    sr.register_url("http://127.0.0.1/seen?token=" + quote(value, safe=""))
    assert len(sr.REGISTRY) == 0
    assert public_text(f"레시피 {value} 실행") == f"레시피 {value} 실행"


@pytest.mark.parametrize("key,value", [("id", "r_a93f3d09"), ("element_id", "@r42")])
def test_b4_recipe_identifier_control_key_value_unchanged(monkeypatch, key, value):
    monkeypatch.setattr(sr, "is_tool_token", lambda _v: False)  # 등록 제외가 깨진 경우(이중 방어 — 제어 키만으로)
    sr.REGISTRY.add_values([value])
    assert public_text(f"x {value} y") == f"x {R} y"  # 조건 성립: 값이 실제로 등록됐다
    payload = {"data": {"recipes": [{key: value, "name": "합성"}], "recipe": {key: value}}}
    assert public_payload(payload) == payload


def test_b4_id_key_other_shape_still_masked():
    v = "SYNTHidValue99"
    sr.register_url("http://127.0.0.1/seen?token=" + v)
    out = public_payload({"data": {"recipe": {"id": v}}})
    assert out["data"]["recipe"]["id"] == R


def test_b4_recipe_id_format_matches_producer():
    """생산자(store._new: `"r_" + token_hex(4)`)가 만든 값이 형식에 맞는다(형식과 생산자의 결합)."""
    import secrets

    fmt = sr.TOOL_ID_FORMATS["recipe_id"]
    for _ in range(200):
        assert fmt.fullmatch("r_" + secrets.token_hex(4))
    assert not fmt.fullmatch("r_a93f3d0") and not fmt.fullmatch("r_A93F3D09x")


# ------------------------------------------------------------------ 허용 한계 — '현재 동작 고정'(계획 §1.2·§1.6, 2026-10-17)


def test_limit_page_content_subtree_kept_verbatim():
    """페이지 내용 subtree(extract data.items·관찰 elements·axtree_summary)는 원문 보존 계약 — 범위 밖(§1.2). 같은 응답의
    제목·오류 문구는 가린다. 이 동작이 바뀌면(내용까지 가리거나 제목을 안 가리면) 이 테스트가 바뀐다."""
    v = "SYNTHcontent88"
    sr.register_url("http://127.0.0.1/seen?token=" + v)
    assert len(sr.REGISTRY) == 1
    items = [{"text": v, "href": "/a?token=" + v}]
    elements = [{"element_id": "@e1", "name": v}]
    payload = {"error_message": f"실패 {v}",
               "data": {"items": items, "observation": {"title": v, "elements": elements, "axtree_summary": v}}}
    out = public_payload(payload)
    assert out["data"]["items"] == items and out["data"]["observation"]["elements"] == elements
    assert out["data"]["observation"]["axtree_summary"] == v
    assert out["data"]["observation"]["title"] == R and out["error_message"] == f"실패 {R}"


def test_limit_host_label_partial_match_not_masked():
    """호스트 라벨은 라벨 전체가 등록 값일 때만 가린다 — `xx<V>yy.<도메인>` 은 그대로(정상 도메인 보호, §1.6). 경로·질의의
    같은 값은 가린다."""
    v = "synthsecret99"
    sr.register_url("http://127.0.0.1/seen?token=" + v)
    assert len(sr.REGISTRY) == 1
    exact = public_payload({"current_url": f"http://{v}.localhost:8080/p"})["current_url"]
    assert v not in exact and R in exact
    for host in (f"xx{v}yy.localhost", f"{v}yy.localhost", f"xx{v}.localhost"):
        url = f"http://{host}:8080/p"
        assert public_payload({"current_url": url})["current_url"] == url  # 현재 동작: 부분 일치 라벨은 남는다
    tail = public_payload({"current_url": f"http://xx{v}yy.localhost:8080/{v}"})["current_url"]
    assert tail == f"http://xx{v}yy.localhost:8080/{R}"
