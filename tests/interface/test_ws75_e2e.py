"""WS-75 (#199) E2E — 실 Chromium(headless) + 127.0.0.1 http, 합성 값. 조사 probe(.hermes/plans/ws199-evidence/probe.py)
를 저장소 시험으로 옮겼다: 도구가 비밀로 아는 값은 MCP wire **전체**(내용 경로 포함)에서 0건, 모르는 페이지 글자는 원문.

* Q1: 주소로 등록된 비밀(`?token=SYNTHSECRET1234`) — observe(기본·force_full_tree)·extract(본문·href·목록)·오류문·
  browser_batch(가린 이름으로 대상 해석)·레시피 파일.
* Q2: 자리표시자로 친 자격증명(아이디·비밀번호·일반 칸) — 관찰 value·페이지 반사(extract).
* 등급 fixture(계획 v4): `?id=<ObjectId>` 본문 id·`?key=2024` 본문 '2024-10' 원문, `promoCode=SALE2024`·`?password=abc123`·
  `?otp=123456`·6자리 OTP 자리표시자 입력 반사는 가림.
* batch: 가린 이름·묶음 표제로 해석(하나투어 fixture 포함), 가린 결과가 같은 두 이름 → target_ambiguous, 묶음 중 등록 →
  target_not_found(안전한 멈춤).
"""

from __future__ import annotations

import json
import re
import sys
import threading
from urllib.parse import quote, unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_run_cli import requires_chromium  # noqa: E402
from ws73_pages import hana_html  # noqa: E402

from browser.serve_profile import PROFILE_ROOT_ENV  # noqa: E402
from interface.handoff import STATE_ROOT_ENV  # noqa: E402
from interface.mcp_server import BrowserMCPServer, envelope_json  # noqa: E402
from security import secret_registry  # noqa: E402
from security.secrets import SecretStore  # noqa: E402

R = "[REDACTED]"
S = "SYNTHSECRET1234"
USERV, PASSV = "SYNTHUSERVAL777", "SYNTHPASSVAL888"
UNREGISTERED = ["HUNTER2SYNTH", "synth.user@example.com", "4111 1111 1111 1111", "010-1234-5678"]
CARDS = "".join(f"<li class=card><a href='/item/{i}?token={S}'>Key {i} {S}</a><span>created 2026-10-{i + 10}</span></li>"
                for i in range(4))
ACCT = f"""<!doctype html><title>Account {S}</title>
<h1>API keys</h1><p id=body>Your API key: {S}</p>
<a id=lnk href="/next?token={S}">Open {S}</a>
<button aria-label="copy key {S}" onclick="this.dataset.n=(+this.dataset.n||0)+1; document.getElementById('out').textContent='copied'">copy</button>
<label>Key field<input id=kf value="{S}" readonly></label>
<p id=pat>config: api_key=sk_live_PATTERNONLY999 password=HUNTER2SYNTH</p>
<p id=pii>mail synth.user@example.com card 4111 1111 1111 1111 phone 010-1234-5678</p>
<p id=out></p>
<ul>{CARDS}</ul>"""
_ECHO = "document.getElementById('{0}').textContent='{1}:'+this.value"
LOGIN = f"""<!doctype html><title>login</title>
<label>User<input id=user oninput="{_ECHO.format('echo', 'echo')}"></label>
<label>Password<input id=pw type=password oninput="{_ECHO.format('echo2', 'pw-echo')}"></label>
<label>Notes<input id=note></label>
<label>OTP<input id=otp oninput="{_ECHO.format('echo3', 'otp')}"></label>
<p id=echo></p><p id=echo2></p><p id=echo3></p>"""
HEX24 = "507f1f77bcf86cd799439011"
GRADES = f"""<!doctype html><title>grades</title>
<p id=oid>주문번호 {HEX24}</p><p id=year>2024-10 결산</p><p id=promo>쿠폰 SALE2024 적용</p>
<p id=pw>비밀번호 abc123 확인</p><p id=otp>인증 123456 완료</p><a href="/order?id={HEX24}">주문 {HEX24}</a>"""
TWINS = """<!doctype html><title>twins</title><p id=out></p>
<button onclick="out.textContent='A'">Copy SYNTHAAAA1111</button>
<button onclick="out.textContent='B'">Copy SYNTHBBBB2222</button>
<label>Code<input id=code></label>
<button onclick="out.textContent='U'">Use SYNTHmidValue77</button>"""
PAGES = {"/acct": ACCT, "/next": ACCT, "/login": LOGIN, "/grades": GRADES, "/twins": TWINS}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(STATE_ROOT_ENV, str(tmp_path / "servers"))
    monkeypatch.setenv(PROFILE_ROOT_ENV, str(tmp_path / "profiles"))
    secret_registry.clear()
    yield
    secret_registry.clear()


@pytest.fixture
def site():
    hana = hana_html()

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path == "/robots.txt":
                body = b"User-agent: *\nAllow: /\n"
            elif path.startswith("/btn/"):  # 단추 하나(레시피 대상이 ui 종류 — 이름을 저장한다)
                name = unquote(path[5:])
                body = (f"<!doctype html><title>b</title><p id=o></p><button onclick=\"o.textContent='x'\">{name}</button>"
                        ).encode()
            else:
                body = (hana if path == "/hana" else PAGES.get(path, ACCT)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def _server(tmp_path, **kw):
    kw.setdefault("recipes", False)
    return BrowserMCPServer(headless=True, nav_settle=False, handoff_root=tmp_path / "h", **kw)


def _hits(wire: str, needle: str) -> int:
    return wire.count(needle)


async def _wires(s, base):
    out = {}
    nav = await s.call_tool("browser_navigate", {"url": f"{base}/acct?token={S}"})
    assert nav.success, nav.error_message
    out["navigate"] = envelope_json(nav)
    out["observe"] = envelope_json(await s.call_tool("browser_observe_page", {}))
    out["observe_full"] = envelope_json(await s.call_tool("browser_observe_page", {"force_full_tree": True}))
    for sel, kw in [("#body", {}), ("#lnk", {"attributes": ["href"]}),
                    ("li.card a", {"attributes": ["href"], "extract_all": True}), ("#kf", {"attributes": ["value"]}),
                    ("#pii", {}), ("#pat", {})]:
        out["extract " + sel] = envelope_json(await s.call_tool("browser_extract", {"selector": sel, **kw}))
    out["extract_err"] = envelope_json(await s.call_tool("browser_extract", {"selector": f"#missing-{S}"}))
    out["wait_for"] = envelope_json(await s.call_tool("browser_wait_for", {"condition": "selector",
                                                                           "selector": f"#nope-{S}",
                                                                           "timeout_ms": 300}))
    return out


@requires_chromium
async def test_q1_registered_url_secret_zero_in_full_wire(site, tmp_path):
    async with _server(tmp_path, pre_approved_actions=("click:*",)) as s:
        wires = await _wires(s, site)
        assert len(secret_registry.REGISTRY) >= 1
        # 가린 이름으로 batch 대상 해석(에이전트가 본 글자 = 해석 글자)
        batch = await s.call_server_tool("browser_batch", {"steps": [
            {"action": "click", "target": {"role": "button", "name": f"copy key {R}"}}]})
        clicked = await s._page.evaluate("() => document.querySelector('button').dataset.n")
    wires["batch"] = json.dumps(batch, ensure_ascii=False)
    leaked = {k: _hits(w, S) for k, w in wires.items() if _hits(w, S)}
    assert leaked == {}, leaked
    assert batch["success"] is True, batch["data"]["batch"]
    assert clicked == "1"
    obs = json.loads(wires["observe"])["data"]
    names = [e["name"] for e in obs["observation"]["elements"]]
    assert f"copy key {R}" in names and f"Open {R}" in names
    assert f'"copy key {R}"' in obs["observation"]["axtree_summary"]
    members = [m["name"] for g in obs["repeated"]["groups"] for m in g["members"]]
    assert members and all(m.endswith(R) for m in members)
    href = json.loads(wires["extract #lnk"])["data"]["items"]["href"]
    assert href == f"/next?token={R}"
    # 과잉 가림 0: 도구가 모르는 페이지 글자는 원문
    assert "HUNTER2SYNTH" in wires["extract #pat"] and "sk_live_PATTERNONLY999" in wires["extract #pat"]
    for needle in UNREGISTERED[1:]:
        assert needle in wires["extract #pii"], needle
    assert "created 2026-10-10" in wires["observe"]


@requires_chromium
async def test_q1_masked_href_is_not_navigable_but_element_click_follows(site, tmp_path):
    async with _server(tmp_path, pre_approved_actions=("click:*",)) as s:
        await s.call_tool("browser_navigate", {"url": f"{site}/acct?token={S}"})
        ex = json.loads(envelope_json(await s.call_tool("browser_extract", {"selector": "#lnk",
                                                                            "attributes": ["href"]})))
        refused = await s.call_tool("browser_navigate", {"url": site + ex["data"]["items"]["href"]})
        assert not refused.success and refused.error_code.value == "E_INVALID_URL"
        obs = await s.call_tool("browser_observe_page", {})
        eid = next(e["element_id"] for e in obs.data["observation"]["elements"] if e["name"] == f"Open {R}")
        done = await s.call_tool("browser_click", {"element_id": eid, "epoch": obs.snapshot_epoch})
        assert done.success, done.error_message
        assert s._page.url.endswith(f"/next?token={S}")  # 실제 이동은 원문 주소(서버 내부)
        assert S not in envelope_json(done)


@requires_chromium
async def test_q2_typed_credentials_hidden_in_values_and_reflection(site, tmp_path):
    store = SecretStore({"X-USER": USERV, "X-PASS": PASSV, "X-OTP": "482913"})
    async with _server(tmp_path, secrets=store, pre_approved_actions=("type_text:*",)) as s:
        await s.call_tool("browser_navigate", {"url": f"{site}/login"})
        for field, key in [("user", "X-USER"), ("pw", "X-PASS"), ("note", "X-PASS"), ("otp", "X-OTP")]:
            obs = await s.call_tool("browser_observe_page", {})
            boxes = [e for e in obs.data["observation"]["elements"] if e.get("role") == "textbox"]
            target = next(e for e in boxes if e["name"] == field)  # 이름 = id 폴백(라벨 감싸기)
            typed = await s.call_tool("browser_type_text", {"element_id": target["element_id"],
                                                            "epoch": obs.snapshot_epoch, "text": key})
            assert typed.success and typed.data.get("secret_resolved") is True, typed.error_message
        dom = await s._page.evaluate("() => [user.value, pw.value, note.value, otp.value]")
        assert dom == [USERV, PASSV, PASSV, "482913"]  # 측정 조건: 실제 값이 들어갔다
        observe = envelope_json(await s.call_tool("browser_observe_page", {}))
        echo = envelope_json(await s.call_tool("browser_extract", {"selector": "p", "extract_all": True}))
    for wire in (observe, echo):
        for v in (USERV, PASSV, "482913"):
            assert v not in wire, (v, wire[:400])
    items = [i["text"] for i in json.loads(echo)["data"]["items"]]
    assert items == [f"echo:{R}", f"pw-echo:{R}", f"otp:{R}"]


@requires_chromium
async def test_grade_fixtures_on_real_page(site, tmp_path):
    async with _server(tmp_path) as s:
        for q in (f"id={HEX24}", "key=2024", "promoCode=SALE2024", "password=abc123", "otp=123456"):
            nav = await s.call_tool("browser_navigate", {"url": f"{site}/grades?{q}"})
            assert nav.success, nav.error_message
        observe = envelope_json(await s.call_tool("browser_observe_page", {}))
        body = envelope_json(await s.call_tool("browser_extract", {"selector": "p", "extract_all": True}))
    texts = [i["text"] for i in json.loads(body)["data"]["items"]]
    assert texts == [f"주문번호 {HEX24}", "2024-10 결산", f"쿠폰 {R} 적용", f"비밀번호 {R} 확인", f"인증 {R} 완료"]
    assert f"주문 {HEX24}" in observe  # 휴리스틱 id 는 내용 경로 원문(주소 칸은 예전대로 가림)


@requires_chromium
async def test_batch_masked_names_ambiguous_and_registry_change_mid_batch(site, tmp_path):
    store = SecretStore({"X-MID": "SYNTHmidValue77"})
    async with _server(tmp_path, secrets=store, pre_approved_actions=("click:*", "type_text:*")) as s:
        await s.call_tool("browser_navigate", {"url": f"{site}/twins"})
        secret_registry.register_secret_values(["SYNTHAAAA1111", "SYNTHBBBB2222"])
        amb = await s.call_server_tool("browser_batch", {"steps": [
            {"action": "click", "target": {"role": "button", "name": f"Copy {R}"}}]})
        raw = await s.call_server_tool("browser_batch", {"steps": [  # 원문 이름은 맞지 않는다(가린 이름 기준)
            {"action": "click", "target": {"role": "button", "name": "Copy SYNTHAAAA1111"}}]})
        # 묶음 중 등록: 1단계 자리표시자 입력이 값을 등록 → 2단계의 (등록 전에 본) 원문 이름은 이제 가려져 해석되지 않는다
        mid = await s.call_server_tool("browser_batch", {"steps": [
            {"action": "type_text", "target": {"role": "textbox", "name": "code"}, "args": {"text": "X-MID"}},
            {"action": "click", "target": {"role": "button", "name": "Use SYNTHmidValue77"}}]})
        out = await s._page.evaluate("() => out.textContent")
    assert amb["data"]["batch"]["reason"] == "target_ambiguous", amb["data"]["batch"]
    assert raw["data"]["batch"]["reason"] == "target_not_found", raw["data"]["batch"]
    b = mid["data"]["batch"]
    assert (b["reason"], b["ran"], b["stopped_at"]) == ("target_not_found", 1, 1), b
    assert out == ""  # 어떤 단추도 눌리지 않았다
    for wire in (amb, raw, mid):
        dumped = json.dumps(wire, ensure_ascii=False)
        assert "SYNTHAAAA1111" not in dumped and "SYNTHBBBB2222" not in dumped
    assert "Use SYNTHmidValue77" in json.dumps(amb, ensure_ascii=False)  # 등록 전에는 모르는 글자 — 원문
    assert "SYNTHmidValue77" not in json.dumps(mid, ensure_ascii=False)


@requires_chromium
async def test_batch_hanatour_masked_group_heading_resolves(site, tmp_path):
    """하나투어 캡처: 묶음 표제의 등록 값('2026년' 을 자격증명으로 등록)이 가려져도 가린 표제로 날짜 둘을 고른다."""
    async with _server(tmp_path) as s:
        await s.call_tool("browser_navigate", {"url": f"{site}/hana"})
        secret_registry.register_secret_values(["2026년"])
        obs = json.loads(envelope_json(await s.call_tool("browser_observe_page", {"prune_top_n": 800})))
        groups = [g.get("group") for g in (obs["data"].get("repeated") or {}).get("groups", [])]
        assert f"{R} 10월" in groups and f"{R} 11월" in groups, groups
        out = await s.call_server_tool("browser_batch", {"steps": [
            {"action": "click", "target": {"role": "link", "name": "19", "group": f"{R} 10월"}},
            {"action": "click", "target": {"role": "link", "name": "6", "group": f"{R} 11월"}}]})
        clicks = await s._page.evaluate("() => window.__clicks")
    assert out["success"] is True, out["data"]["batch"]
    assert clicks == [["2026년 10월", 19, 0], ["2026년 11월", 6, 1]]
    assert "2026년" not in json.dumps(out["data"].get("repeated") or {}, ensure_ascii=False)


@requires_chromium
async def test_recipe_file_never_stores_registered_value(site, tmp_path):
    """자동 저장 레시피: 등록 값이 든 대상 이름을 누른 단계는 저장하지 않는다(파일 0건). 대조: 모르는 이름은 저장된다."""
    async def click(s, name):
        obs = await s.call_tool("browser_observe_page", {})
        eid = next(e["element_id"] for e in obs.data["observation"]["elements"] if e["name"] == name)
        done = await s.call_tool("browser_click", {"element_id": eid, "epoch": obs.snapshot_epoch})
        assert done.success, done.error_message

    async with BrowserMCPServer(headless=True, nav_settle=False, handoff_root=tmp_path / "h", recipes=True,
                                profile="ws75", pre_approved_actions=("click:*",)) as s:
        await s.call_tool("browser_navigate", {"url": f"{site}/btn/" + quote("Use SYNTHmidValue77")})
        await click(s, "Use SYNTHmidValue77")  # 대조 — 도구가 모르는 값(저장된다)
        await s.call_tool("browser_navigate", {"url": f"{site}/btn/" + quote("Copy SYNTHAAAA1111")})
        secret_registry.register_secret_values(["SYNTHAAAA1111"])
        await click(s, "Copy [REDACTED]")
        listed = await s.call_server_tool("browser_recipe", {"op": "list"})
    texts = [p.read_text(encoding="utf-8", errors="replace") for p in Path(tmp_path).rglob("*.json")]
    assert any("SYNTHmidValue77" in t for t in texts), "대조 레시피가 저장되지 않으면 이 시험은 저장 거부를 재지 못한다"
    assert not any("SYNTHAAAA1111" in t for t in texts)
    assert "SYNTHAAAA1111" not in json.dumps(listed, ensure_ascii=False)
