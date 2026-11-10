"""WS-91 R1 — 검증 FAIL B1~B4 회귀(.hermes/verify/ws91 재현을 저장소 시험으로).

원칙: 응답 상태 숫자가 아니라 **반환 직전의 최종 문서 상태**로 판정한다.

* B1: 즉시 404·500 결과 문서에 같은 id·값 칸이 있으면 기준(7b7a6f0)처럼 성공 — 상태 숫자로 일괄 실패하지 않는다.
  빈 404·500 결과는 기준처럼 nav_settle=False 실패·True 성공.
* B2: 닫힌 포트·redirect loop·allowlist 밖(.invalid) 제출은 반환 때 이미 chrome-error 면 성공 0(기준 E_TIMEOUT 문구).
  경쟁이라 '반환 직전 확인'을 결정적으로 강제하는 주입(제출을 Enter 뒤 50ms 로 미루고 서버 꼬리에서 오류 문서 커밋을
  기다림)으로 RED 를 재현하고, 실제 경로도 20회씩 돈다.
* B3: 주소 `?token=same_node` 관측(등록) 뒤에도 wire 의 after_enter 는 고정 어휘 그대로.
* B4: 다른 칸에 SecretStore 값을 친 뒤 일반 칸 press_key Enter 는 after_enter 가 있다(그 비밀 칸 Enter 는 없음).

실제 BrowserMCPServer(헤드리스 Chromium) + 로컬 HTTP fixture 만, 값은 합성 값.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

import pytest

from contracts import ActionType, ErrorCode
from interface.mcp_server import BrowserMCPServer, envelope_json

from test_run_cli import requires_chromium  # noqa: F401
from test_ws91_enter_tail import HEAD, _call, _closed_port, _target, _why, probe  # noqa: F401

_FORM = (HEAD.format(t="검색") + "<form method=get action='{dest}'><input id=t name=q aria-label='검색어'>"
         "</form>")
#: Enter 의 제출을 50ms 미룬다(디스패처 사후 판독이 요청 전에 끝나게) — 오류 이동이 서버 꼬리 구간에 온다.
_LATE_FORM = (HEAD.format(t="검색") + "<form id=f method=get action='{dest}'><input id=t name=q aria-label='검색어'>"
              "</form><script>document.getElementById('t').addEventListener('keydown',e=>{{if(e.key==='Enter'){{"
              "e.preventDefault();setTimeout(()=>document.getElementById('f').submit(),50);}}}});</script>")
_SAME = HEAD.format(t="결과") + "<input id=t aria-label='검색어' value=abc><p>Done</p>"
_EMPTY = HEAD.format(t="결과") + "<p>Missing or upstream failed</p>"
_MULTI = HEAD.format(t="Memo") + "<input id=t aria-label=Memo><input id=u aria-label=Other>"


class _Srv:
    base = ""
    closed = ""
    refused = ""


@pytest.fixture(scope="module")
def srv():
    st = _Srv()
    # 검증 재현과 같은 주소(포트 9 — 브라우저가 연결 전에 거부, 요청 실패 → chrome-error). 아무도 듣지 않는 임의 포트는
    # 기본 egress 프록시가 502 문서(upstream connect_failed)로 답한다(proxy502 — 최종 문서로 판정).
    st.closed = "http://127.0.0.1:9/x"
    st.refused = f"http://127.0.0.1:{_closed_port()}/x"

    class H(BaseHTTPRequestHandler):
        def _send(self, status: int, body: str, headers: Dict[str, str] | None = None) -> None:
            b = body.encode()
            try:
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):  # noqa: N802
            p = urlparse(self.path).path
            parts = p.strip("/").split("/")
            if p == "/robots.txt":
                return self._send(200, "User-agent: *\nAllow: /\n")
            if p == "/loop":
                return self._send(302, "", {"Location": "/loop"})
            if p == "/multi":
                return self._send(200, _MULTI)
            if parts[0] in ("form", "late"):
                kind = parts[1]
                dest = {"closed": st.closed, "proxy502": st.refused, "loop": "/loop", "invalid": "http://unlisted.invalid/x"}.get(
                    kind, f"/result/{kind}")
                return self._send(200, (_LATE_FORM if parts[0] == "late" else _FORM).format(dest=dest))
            if parts[0] == "result":
                code, body = parts[1].split("-") if "-" in parts[1] else (parts[1], "same")
                return self._send(int(code), _SAME if body == "same" else _EMPTY)
            return self._send(404, "<p>Missing</p>")

        def log_message(self, *a):  # noqa: ANN002
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    st.base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield st
    finally:
        server.shutdown()
        server.server_close()


async def _submit(s: BrowserMCPServer, url: str, text: str = "abc") -> Any:
    eid, epoch = await _target(s, url, "검색어")
    return await _call(s, ActionType.TYPE_TEXT, {"element_id": eid, "epoch": epoch, "text": text, "press_enter": True})


def _kw(kind: str) -> Dict[str, Any]:
    """검증 재현과 같은 서버 설정: 닫힌 포트·loop 는 기본(allowlist 없음), .invalid 는 allowlist(127.0.0.1) 밖."""
    return {"allowed_domains": ("127.0.0.1",)} if kind == "invalid" else {}


def _base_failure(r: Any) -> None:
    assert not r.success, r.data
    assert r.error_code is ErrorCode.TIMEOUT, (r.error_code, r.error_message)
    assert (r.error_message or "").startswith("사후조건 미충족(기대값 'abc' != 실제 None)"), r.error_message
    assert r.reobserve_required and not r.retry_safe
    assert "signals" not in r.data and "element_id" not in r.data, r.data


# ------------------------------------------------------------------ B1: 상태 숫자로 판정하지 않는다


@requires_chromium
@pytest.mark.parametrize("nav_settle", [True, False])
@pytest.mark.parametrize("code", ["404", "500"])
async def test_b1_error_status_result_with_same_field_succeeds_like_base(srv, probe, nav_settle, code):
    """기준 성공 3/3(value_applied) — 즉시 404·500 결과 문서에 같은 id·값 칸. 대상도 성공 3/3."""
    async with BrowserMCPServer(nav_settle=nav_settle, recipes=False) as s:
        for _ in range(3):
            r = await _submit(s, srv.base + f"/form/{code}")
            assert r.success, (r.error_code, r.error_message, r.data)
            assert r.data.get("last_http_status") == int(code), r.data  # 상태는 기존 data 신호로만


@requires_chromium
@pytest.mark.parametrize("nav_settle", [True, False])
@pytest.mark.parametrize("code", ["404", "500"])
async def test_b1_empty_error_status_result_matches_base(srv, probe, nav_settle, code):
    """빈 404·500 결과(칸 없음): 기준 nav_settle=False 실패(E_TIMEOUT 기대값 != None)·True 성공(navigated) 그대로."""
    async with BrowserMCPServer(nav_settle=nav_settle, recipes=False) as s:
        for _ in range(3):
            r = await _submit(s, srv.base + f"/form/{code}-empty")
            if nav_settle:
                assert r.success, (r.error_code, r.error_message)
            else:
                _base_failure(r)


# ------------------------------------------------------------------ B2: 반환 직전 동기 확인


def _hold_tail_until_error_doc(monkeypatch: Any, s: BrowserMCPServer) -> List[float]:
    """서버 꼬리(차단 판정 페이지 프로브)가 주 프레임이 오류 문서로 바뀔 때까지(≤1s) 기다리게 한다 — 오류 커밋이 디스패처
    반환 뒤·call_tool 반환 전에 오는 경우를 결정적으로 만든다. F3 마감(Enter 뒤 0.3s)이 이 대기를 자르면 그대로."""
    from browser import challenge as challenge_mod

    seen: List[float] = []
    orig = challenge_mod.detect_challenge

    async def detect(page: Any, *a: Any, **k: Any) -> Any:
        t0 = time.monotonic()
        while not str(s._page.url).startswith("chrome-error://") and time.monotonic() - t0 < 1.0:  # noqa: SLF001
            await asyncio.sleep(0.005)
        seen.append(time.monotonic() - t0)
        return await orig(page, *a, **k)

    monkeypatch.setattr(challenge_mod, "detect_challenge", detect)
    return seen


@requires_chromium
@pytest.mark.parametrize("kind", ["closed", "loop", "invalid"])
async def test_b2_error_navigation_in_server_tail_is_base_failure(srv, probe, monkeypatch, kind):
    """결정적 RED: 디스패처는 요청 전에 판독을 끝내 성공을 냈고, 오류 문서 커밋은 서버 꼬리 구간에 온다 → 반환 직전
    확인이 기준 실패 계열로 바꾼다(반환 때 chrome-error 인데 성공 0)."""
    async with BrowserMCPServer(nav_settle=False, recipes=False, **_kw(kind)) as s:
        seen = _hold_tail_until_error_doc(monkeypatch, s)
        for _ in range(3):
            r = await _submit(s, srv.base + f"/late/{kind}")
            at_return = s._page.url  # noqa: SLF001
            assert at_return.startswith("chrome-error://"), (at_return, r.data)
            _base_failure(r)
            if "after_enter" in r.data:
                assert r.data["after_enter"]["document"] in ("navigating", "navigated"), r.data
            assert "egress" not in r.data, r.data  # 기준과 같은 egress 판정(대상 호스트가 호출 주소에 없음 — 신호 없음)
        assert seen and min(seen) > 0.0, seen  # 주입이 실제로 꼬리에서 기다렸다(조건 성립)


@requires_chromium
@pytest.mark.parametrize("kind", ["closed", "loop", "invalid", "proxy502"])
async def test_b2_error_navigation_real_20_runs_no_success(srv, probe, kind):
    """실제 경로(주입 없음) 각 20회: 반환 때 chrome-error 인데 성공이면 안 된다(B2).

    오류 요청·커밋이 반환 뒤에 오면(문서화된 '마감·반환 뒤는 모름' 한계) 성공일 수 있다 — 그때는 반환 때
    문서는 오류 문서가 아니다. 성공 0 을 단정하면
    그 한계 때문에 흔들린다(검증 ws91-r1 NB: proxy502 1/20). 반환 때 오류 문서였던 성공(B2 원 결함)은 여기서 실패한다."""
    successes: List[Tuple[int, Dict[str, Any]]] = []
    async with BrowserMCPServer(nav_settle=False, recipes=False, **_kw(kind)) as s:
        for i in range(20):
            r = await _submit(s, srv.base + f"/form/{kind}")
            at_return = s._page.url  # noqa: SLF001
            if r.success:
                assert not at_return.startswith("chrome-error://"), (i, at_return, r.data)
                successes.append((i, {"url": at_return, "data": r.data}))
            else:
                assert r.error_code in (ErrorCode.TIMEOUT, ErrorCode.PAGE_CRASHED), (r.error_code, r.error_message)
                if kind != "proxy502":
                    assert "egress" not in r.data, r.data  # 기준과 같다(대상 호스트가 호출 주소 후보에 없음)
                else:
                    assert r.data.get("egress", {}).get("code") == "connect_failed", r.data  # 기준과 같은 차단 신호
    assert len(successes) < 20, successes  # 전부 '반환 뒤'로 미뤄지면 이 시험이 판정 경로를 못 잰 것


@requires_chromium
async def test_b2_aborted_and_ok_paths_keep_success(srv, probe):
    """이웃: 같은 칸이 있는 200 결과 성공, 이동 없는 Enter(일반 칸) 성공 — 반환 직전 확인은 오류 문서일 때만 바꾼다."""
    async with BrowserMCPServer(nav_settle=False, recipes=False) as s:
        r = await _submit(s, srv.base + "/form/200")
        assert r.success, (r.error_code, r.error_message)
        eid, epoch = await _target(s, srv.base + "/multi", "Memo")
        r = await _call(s, ActionType.TYPE_TEXT, {"element_id": eid, "epoch": epoch, "text": "abc", "press_enter": True})
        assert r.success and r.data.get("after_enter", {}).get("document") == "same", r.data
        assert s._dispatcher.__dict__.get("_enter_watch") is None  # noqa: SLF001 — 반환 직전 확인이 리스너를 뗐다


# ------------------------------------------------------------------ B3: 주소 등록 뒤에도 어휘 그대로


@requires_chromium
async def test_b3_after_enter_vocabulary_survives_real_url_registration(srv, probe):
    from security import secret_registry as sr

    sr.clear()
    try:
        async with BrowserMCPServer(nav_settle=False, recipes=False) as s:
            eid, epoch = await _target(s, srv.base + "/multi?token=same_node", "Memo")
            assert len(sr.REGISTRY) > 0  # 조건 성립: same_node 가 실제 주소 관측으로 등록됐다
            r = await _call(s, ActionType.TYPE_TEXT, {"element_id": eid, "epoch": epoch, "text": "운중",
                                                      "press_enter": True})
            wire = json.loads(envelope_json(r))
        assert wire["data"]["after_enter"] == {"value": "kept", "field": "same_node", "listbox": "none", "options": 0,
                                               "document": "same"}, wire["data"]
    finally:
        sr.clear()


# ------------------------------------------------------------------ B4: 노드 단위 민감 판정


@requires_chromium
async def test_b4_press_key_enter_other_field_after_secret_keeps_after_enter(srv, probe, caplog):
    from security.secrets import SecretStore

    secret = "SyntheticCedarRiverWs91R1"
    caplog.set_level("DEBUG")
    async with BrowserMCPServer(nav_settle=False, secrets=SecretStore({"WS91_R1_CANARY": secret})) as s:
        eid, epoch = await _target(s, srv.base + "/multi", "Memo")
        typed = await _call(s, ActionType.TYPE_TEXT, {"element_id": eid, "epoch": epoch, "text": "WS91_R1_CANARY"})
        assert typed.success and typed.data.get("secret_resolved") is True, typed.data
        o = (await _call(s, ActionType.OBSERVE_PAGE, {})).data["observation"]
        other = next(e for e in o["elements"] if e["name"] == "Other")
        memo = next(e for e in o["elements"] if e["name"] == "Memo")
        assert (await _call(s, ActionType.TYPE_TEXT, {"element_id": other["element_id"], "epoch": o["snapshot_epoch"],
                                                      "text": "ordinary"})).success
        plain = await _call(s, ActionType.PRESS_KEY, {"key": "Enter"})
        assert plain.success, plain.error_message
        assert plain.data.get("after_enter") == {"value": "kept", "field": "same_node", "listbox": "none",
                                                 "options": 0, "document": "same"}, plain.data
        # 그 비밀 칸으로 돌아가 Enter — 노드 표식이 있어 생략(게이트가 막아도 막지 않아도 after_enter 없음)
        o = (await _call(s, ActionType.OBSERVE_PAGE, {})).data["observation"]
        clicked = await _call(s, ActionType.CLICK, {"element_id": memo["element_id"], "epoch": o["snapshot_epoch"]})
        assert clicked.success, clicked.error_message
        hidden = await _call(s, ActionType.PRESS_KEY, {"key": "Enter"})
        assert "after_enter" not in hidden.data, hidden.data
        listed = await s.call_server_tool("browser_recipe", {"op": "list"})
    blob = envelope_json(typed) + envelope_json(plain) + envelope_json(hidden) + json.dumps(listed) + caplog.text
    assert secret not in blob
    assert str(len(secret)) not in json.dumps(hidden.data) + json.dumps(plain.data)


def test_b4_node_sensitivity_rule():
    """순수 판정: 노드 표식(cm)·비밀번호(s)면 민감, 표식을 넣은 세션이면 키 전 그 노드의 '표식 없음'이 보여야 일반."""
    from actions.dispatcher import ActionDispatcher

    d = ActionDispatcher.__new__(ActionDispatcher)
    d._concealed_marked = False  # noqa: SLF001
    assert d._enter_key_sensitive({"s": False, "k": "1.2", "cm": False}, "1.2") is False  # noqa: SLF001
    assert d._enter_key_sensitive({"s": True, "k": "1.2", "cm": False}, "1.2") is True  # noqa: SLF001
    assert d._enter_key_sensitive(None, "1.2") is False  # noqa: SLF001
    d._concealed_marked = True  # noqa: SLF001
    assert d._enter_key_sensitive({"s": False, "k": "1.2", "cm": False}, "1.2") is False  # noqa: SLF001
    assert d._enter_key_sensitive({"s": False, "k": "1.2", "cm": True}, "1.2") is True  # noqa: SLF001
    assert d._enter_key_sensitive({"s": False, "k": "9.9", "cm": False}, "1.2") is True  # noqa: SLF001 — 다른 노드
    assert d._enter_key_sensitive(None, "1.2") is True  # noqa: SLF001 — 못 읽음: 보수
