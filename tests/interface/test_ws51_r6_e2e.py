"""WS-51 R6 E2E — 실 Chromium(headless) + 로컬 http, R5 검증 BLOCKING 3건의 검증자 재현 조건.

* R5-F1: 실제 `/seen?token=98574321` 이동 뒤 `extra={98574321: 'value'}` × JSON 형 Formatter × Stream·File — 원문 0.
* R5-F2: 실제 `/seen?token=metadata` 이동 뒤 표준 `Formatter('%(message)s %(metadata)s')` — 정상 출력 `retained value`,
  stderr 진단 없음(dd9d3aa 와 같은 출력).
* R5-F3: 실제 `vision.bind_tag` 를 10번 불러 `@s10` 생성 → 별도 탭에서 `/seen?token=%40s10` → 원 ID 로 TYPE_TEXT →
  최종 wire 의 `data.element_id` 를 그대로 후속 TYPE_TEXT 에 재사용(경계 `@s10` 반환 ID 재사용 잠금).

값은 합성 값. 공개 사이트 없음(127.0.0.1 만).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest

from contracts import ActionType
from interface.mcp_server import BrowserMCPServer, envelope_json, install_public_log_filter, tool_name
from security import secret_registry as sr
from security.public_url import public_text

from test_run_cli import requires_chromium

R = "[REDACTED]"
PAGE = b'<!doctype html><meta charset=utf-8><title>clean</title><input aria-label="Synthetic input">'


@pytest.fixture(autouse=True)
def _fresh_registry():
    sr.clear()
    yield
    sr.clear()


@pytest.fixture
def origin():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


class _ExtraJSON(logging.Formatter):
    def format(self, record):  # noqa: A003
        base = logging.makeLogRecord({}).__dict__
        return record.getMessage() + " " + json.dumps(
            {k: v for k, v in vars(record).items() if k not in base}, default=str)


def _emit(tmp_path: Path, key, fmt: logging.Formatter):
    stream, err = io.StringIO(), io.StringIO()
    path = tmp_path / f"{type(key).__name__}.log"
    a, b = logging.StreamHandler(stream), logging.FileHandler(path, mode="w", encoding="utf-8")
    a.setFormatter(fmt)
    b.setFormatter(fmt)
    log = logging.getLogger("ws51.r6.e2e")
    log.handlers, log.propagate = [a, b], False
    log.setLevel(logging.INFO)
    restore = install_public_log_filter()
    try:
        with contextlib.redirect_stderr(err):
            log.info("retained", extra={key: "value"})
    finally:
        restore()
        log.handlers = []
        a.close()
        b.close()
    return {"stream": stream.getvalue(), "file": path.read_text(encoding="utf-8"), "stderr": err.getvalue()}


async def _nav(s, url):
    return await s.call_tool(tool_name(ActionType.NAVIGATE), {"url": url})


@requires_chromium
async def test_f1_f2_extra_keys_after_real_navigate(origin, tmp_path: Path):
    async with BrowserMCPServer(headless=True, nav_settle=False, handoff_root=tmp_path / "hub") as s:
        for case, key in (("numeric_extra_key", 98574321), ("standard_formatter_key", "metadata")):
            sr.clear()
            secret = str(key)
            assert (await _nav(s, origin + "/seen?token=" + secret)).success
            assert len(sr.REGISTRY) == 1 and public_text(secret) == R  # 조건 성립
            fmt = _ExtraJSON() if isinstance(key, int) else logging.Formatter("%(message)s %(" + key + ")s")
            out = _emit(tmp_path, key, fmt)
            leaks = [k for k, v in out.items() if any(form in v for form in sr.representations(secret))]
            assert leaks == [], (case, out)
            assert out["stderr"] == "", (case, out)
            if isinstance(key, str):
                assert out["stream"] == out["file"] == "retained value\n", out  # dd9d3aa 와 같은 정상 출력
            else:
                assert out["stream"].startswith("retained ") and '"value"' in out["stream"], out


#: census 일반화로 드러난 식별자의 생산→소비 페이지(이름 없는 검색 칸 → 자동 레시피 매개변수 text1, 204 링크 → nav_aborted).
_SITE = {
    "/list": ("<!doctype html><meta charset=utf-8><title>목록</title><main><input type='search'>"
              "<button type='button' onclick=\"location.href='/search?q='+encodeURIComponent("
              "document.querySelector('input').value)\">검색</button><a href='/nocontent'>빈 응답</a></main>"),
}


@pytest.fixture
def site():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            path = self.path.split("?", 1)[0]
            if path == "/nocontent":
                self.send_response(204)
                self.end_headers()
                return
            if path == "/search":
                q = self.path.split("q=", 1)[-1]
                body = (f"<!doctype html><meta charset=utf-8><title>결과</title>"
                        f"<a href='/item?id=301'>결과 {q} 1</a>").encode()
            else:
                body = _SITE.get(path, "<!doctype html><title>clean</title><p>clean</p>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", hits
    finally:
        srv.shutdown()
        srv.server_close()


async def _observe(s):
    res = await s.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
    assert res.success, res.error_message
    return json.loads(envelope_json(res))


def _eid(obs, role: str, name: str = "") -> str:
    for el in obs["data"]["observation"]["elements"]:
        if el["role"] == role and (not name or el["name"] == name):
            return el["element_id"]
    raise AssertionError([(e["role"], e["name"]) for e in obs["data"]["observation"]["elements"]])


@requires_chromium
@pytest.mark.parametrize("break_exclusion", [False, True], ids=["exclusion", "control_key_only"])
async def test_f3_recipe_auto_param_name_survives_wire_and_run(site, tmp_path: Path, monkeypatch, break_exclusion):
    """자동 레시피 매개변수 이름 text1(생산: recipes/store.py `f'{prefix}{counters[prefix]}'`) — 주소에서 본 뒤에도
    list 의 params 가 원래와 같고, 그 이름으로 run 이 동작한다(control_key_only: 등록 제외가 깨진 조건 — 이중 방어)."""
    origin, hits = site
    async with BrowserMCPServer(headless=True, nav_settle=False, handoff_root=tmp_path / "hub") as s:
        assert (await _nav(s, origin + "/list")).success
        obs = await _observe(s)
        box = _eid(obs, "searchbox")
        typed = await s.call_tool(tool_name(ActionType.TYPE_TEXT),
                                  {"element_id": box, "epoch": obs["snapshot_epoch"], "text": "노트북"})
        assert typed.success, typed.error_message
        clicked = await s.call_tool(tool_name(ActionType.CLICK),
                                    {"element_id": _eid(obs, "button", "검색"), "epoch": obs["snapshot_epoch"]})
        assert clicked.success, clicked.error_message
        listed = await s.call_server_tool("browser_recipe", {"op": "list"})
        auto = [r for r in listed["data"]["recipes"] if r.get("auto")]
        assert auto and auto[0]["params"] == ["text1"], listed  # 조건 성립: 생산자가 text1 을 만들었다
        rid = auto[0]["id"]
        if break_exclusion:
            monkeypatch.setattr(sr, "is_tool_token", lambda _v: False)
        sr.clear()
        assert (await _nav(s, origin + "/seen?token=text1")).success  # 도구 식별자를 주소에서 봄
        assert len(sr.REGISTRY) == (1 if break_exclusion else 0)
        if break_exclusion:
            assert public_text("x text1 y") == f"x {R} y"  # 조건 성립: 실제로 등록됐다
        listed = await s.call_server_tool("browser_recipe", {"op": "list"})
        wire = json.loads(json.dumps(listed, ensure_ascii=False))
        returned = next(r for r in wire["data"]["recipes"] if r["id"] == rid)["params"]
        assert returned == ["text1"], returned
        hits.clear()
        ran = await s.call_server_tool("browser_recipe", {"op": "run", "id": rid, "params": {returned[0]: "이어폰"}})
        assert ran["success"], ran
        assert any(h.startswith("/search?q=") and "%EC%9D%B4%EC%96%B4%ED%8F%B0" in h for h in hits), hits


@requires_chromium
async def test_f3_nav_aborted_value_survives_wire(site, tmp_path: Path):
    """nav_aborted(생산: actions/dispatcher.py _NavWatch — download·request_failed·`f'status_{status}'`) — 실제로 나온 값을
    주소에서 본 뒤(어휘는 등록된다)에도 다음 같은 동작의 wire 값이 원래와 같다."""
    origin, _ = site
    async with BrowserMCPServer(headless=True, handoff_root=tmp_path / "hub") as s:  # 이동 감시(nav_settle) 켬

        async def click_empty():
            assert (await _nav(s, origin + "/list")).success
            obs = await _observe(s)
            return await s.call_tool(tool_name(ActionType.CLICK),
                                     {"element_id": _eid(obs, "link", "빈 응답"), "epoch": obs["snapshot_epoch"]})

        first = await click_empty()
        value = first.data.get("nav_aborted")
        assert value in _pu_vocabulary("nav_aborted"), first.data  # 조건 성립: 생산자가 어휘 값을 냈다(204 링크)
        assert len(value) >= 8
        assert (await _nav(s, origin + "/seen?token=" + value)).success
        assert len(sr.REGISTRY) == 1 and public_text(f"x {value} y") == f"x {R} y"  # 조건 성립: 등록됐다
        again = await click_empty()
        assert again.data.get("nav_aborted") == value, again.data
        wire = json.loads(envelope_json(again))
        assert wire["data"]["nav_aborted"] == value, wire["data"]


def _pu_vocabulary(key: str):
    import sys

    import security.public_url  # noqa: F401

    return sys.modules["security.public_url"].CONTROL_VOCABULARY[key]


@requires_chromium
async def test_f3_som_id_survives_wire_and_reuse(origin, tmp_path: Path, monkeypatch):
    from vision import bind_tag
    from vision.candidates import collect_candidates

    async with BrowserMCPServer(headless=True, nav_settle=False, handoff_root=tmp_path / "hub") as s:
        assert (await _nav(s, origin + "/form")).success
        sr.clear()
        candidates = await collect_candidates(s._page)
        candidate = next(c for c in candidates if c.role == "textbox")
        ids = [await bind_tag(s._engine, s._page, candidate) for _ in range(10)]
        eid = ids[-1]
        assert eid == "@s10" and s._engine.get_handle(eid) is not None  # 실제 생산자 경계 값
        page = await s._page.context.new_page()
        try:
            await page.goto(origin + "/seen?token=" + quote(eid, safe=""), wait_until="domcontentloaded")
            await asyncio.sleep(0.1)
            assert len(sr.REGISTRY) == 0, "도구 식별자 @sN 은 등록 제외"
            # 이중 방어: 등록 제외가 깨진 조건(검증자 재현 그대로 — 실제 이동으로 @s10 등록)에서도 제어 칸 값은 그대로
            monkeypatch.setattr(sr, "is_tool_token", lambda _v: False)
            await page.goto(origin + "/seen2?token=" + quote(eid, safe=""), wait_until="domcontentloaded")
            await asyncio.sleep(0.1)
            assert len(sr.REGISTRY) == 1 and public_text(eid) == R  # 조건 성립
            first = await s.call_tool(tool_name(ActionType.TYPE_TEXT),
                                      {"element_id": eid, "epoch": s._engine.epoch, "text": "synthetic first"})
            wire = json.loads(envelope_json(first))
            assert first.success, wire
            returned = wire["data"]["element_id"]
            assert returned == eid, wire["data"]
            second = await s.call_tool(tool_name(ActionType.TYPE_TEXT),
                                       {"element_id": returned, "epoch": s._engine.epoch, "text": "synthetic follow"})
            assert second.success, json.loads(envelope_json(second))
            assert json.loads(envelope_json(second))["data"]["element_id"] == eid
        finally:
            await page.close()
