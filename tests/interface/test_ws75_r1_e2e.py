"""WS-75 R1 (#199) E2E — 검증 BLOCKING 재현을 저장소 시험으로 옮겼다(.hermes/verify/ws75 unicode_probe·
unicode_group_probe·recipe_probe). 실 Chromium(headless) + 127.0.0.1 http, 합성 값만.

* B1(R3 사용자 결정 A — 끝 앞부분 가림 제거): 등록 값 앞에 이모지(서로게이트 쌍) 0/1/2/20 개 — 값 전체가 JS 사전 자르기
  (이름 120·값 200, 일반·shadow) 안에 있으면 MCP wire 전체에 값·앞부분 0건. 묶음 표제(JS rpClean 40)도 같다. 경계에 걸친
  앞부분은 문서화된 한계(test_ws75_r2_e2e). 대조: 이모지 섞인 모르는 이름은 원문 그대로.
* B2: 등록 값(13종 + 대소문자 섞인 값)이 든 이름의 단추를 누른 단계는 레시피 파일에 원문·casefold·앞부분(≥4) 0건.
  대조: 일반 이름 단계는 저장된다.
"""

from __future__ import annotations

import base64
import html
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_run_cli import requires_chromium  # noqa: E402

from browser.serve_profile import PROFILE_ROOT_ENV  # noqa: E402
from interface.handoff import STATE_ROOT_ENV  # noqa: E402
from interface.mcp_server import BrowserMCPServer, envelope_json  # noqa: E402
from security import secret_registry  # noqa: E402

V = "Z9vRUnicodeCanary5731"
E = "\U0001F600"


def _b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


#: 검증 a_probe 의 13종(합성) — JWT 는 실행 때 조립한다.
VALUES = {"api": "K7vR_API_91%+&<synth>", "jwt": _b64({"alg": "HS256"}) + "." + _b64({"sub": "synth9001"}) + ".c2lnU3ludGg5MDAx",
          "mixed": "Z4qXvR89tY23aBcD5678hJkLmN90pQrStUvW", "userinfo": "U8pQ71", "password": "P6wZ92", "otp": "738291",
          "placeholder": "N5xK_normal91", "placeholder_pw": "F3dY_password82", "placeholder_otp": "591826",
          "sensitive": "G2bV_sensitive83", "sensitive_otp": "826395", "login_user": "L9rT_user66",
          "login_pw": "W4sM_password99"}
CASES = ([("lower", "novelcanaryabcdefgh"), ("mixed-case", "ZqVxCanaryabcdefgh91"), ("prefix-canary", "n7q4m9Key81")]
         + [("source-" + k.replace("_", "-"), v) for k, v in VALUES.items()])  # 주소·프로필에 쓰는 이름(하이픈)
NAMES = dict(CASES, control="ordinary")
VIA_URL = {"prefix-canary", "source-password"}  # 실제 MCP navigate `?password=` 로 등록


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(STATE_ROOT_ENV, str(tmp_path / "servers"))
    monkeypatch.setenv(PROFILE_ROOT_ENV, str(tmp_path / "profiles"))
    secret_registry.clear()
    yield
    secret_registry.clear()


@pytest.fixture
def site():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path == "/robots.txt":
                body = "User-agent: *\nAllow: /\n"
            elif path.startswith("/case/"):  # 주소에는 사례 이름만 — 값은 단추 이름에만(주소 거부로 새지 않게)
                name = html.escape(NAMES.get(path[6:], "ordinary"))
                body = (f"<!doctype html><meta charset=utf-8><title>recipe proof</title><button id=btn onclick=\""
                        f"document.querySelector('#result').textContent='changed'\">Activate {name}</button>"
                        "<p id=result></p>")
            else:
                body = "<!doctype html><title>r</title><p>ok</p>"
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def _prefix_leaks(value: str, text: str):
    return [k for k in range(4, len(value) + 1) if value[:k] in text]


# ------------------------------------------------------------------ B1


@requires_chromium
@pytest.mark.parametrize("astral", [0, 1, 2, 20])
async def test_b1_astral_prefix_cut_zero_prefix_in_wire(tmp_path, astral):
    secret_registry.register_secret_values([V])
    prefix = E * astral + "y" * (40 - 2 * astral)  # 둘 다 UTF-16 40 단위 — V 전체가 JS 자르기 길이 안에 든다
    name = prefix + "x" * 50 + V + " tail" + "z" * 40
    val = prefix + "x" * 130 + V + " tail" + "z" * 40
    page = f"""<!doctype html><meta charset=utf-8><title>Local Unicode seam</title>
<button id=b>{name}</button>
<input id=value aria-label="value" value="{val}">
<div id=shadow></div><script>const r=document.querySelector('#shadow').attachShadow({{mode:'open'}});
r.innerHTML='<input aria-label="shadow" value="{val}">';</script>"""
    async with BrowserMCPServer(headless=True, nav_settle=False, recipes=False, handoff_root=tmp_path / "h") as s:
        await s._page.set_content(page)
        out = await s.call_tool("browser_observe_page", {"force_full_tree": True})
        wire = envelope_json(out)
        lengths = await s._page.evaluate("() => [b.textContent.slice(0,120).length, value.value.slice(0,200).length]")
    assert out.success
    assert lengths == [120, 200]  # 경계 조건 성립(JS UTF-16 단위)
    data = json.loads(wire)["data"]
    names = [e.get("name") for e in data["observation"]["elements"]]
    values = [e.get("value") for e in data["observation"]["elements"] if e.get("value")]
    assert any(n and n.startswith(prefix) for n in names) and len(values) >= 2  # 대상 칸이 실제로 wire 에 있다
    assert _prefix_leaks(V, wire) == [], (astral, names, values)


@requires_chromium
@pytest.mark.parametrize("astral", [0, 1, 5])
async def test_b1_astral_group_heading_zero_prefix(tmp_path, astral):
    secret_registry.register_secret_values([V])
    heading = E * astral + "y" * (15 - 2 * astral) + V  # UTF-16 36 단위 — 묶음 표제 자르기(40) 안
    members = "".join(f'<li><button onclick="this.dataset.clicked=1">{i}</button></li>' for i in range(1, 5))
    page = f"""<!doctype html><meta charset=utf-8><title>group source seam</title>
<main style="display:flex;gap:50px"><section aria-label="{heading}"><h2>{heading}</h2><ul>{members}</ul></section>
<section aria-label="Separate control"><h2>Separate control</h2><ul>{members}</ul></section></main>"""
    async with BrowserMCPServer(headless=True, nav_settle=False, recipes=False, handoff_root=tmp_path / "h") as s:
        await s._page.set_content(page)
        out = await s.call_tool("browser_observe_page", {"force_full_tree": True})
        wire = envelope_json(out)
    groups = (json.loads(wire)["data"].get("repeated") or {}).get("groups", [])
    assert any(str(g.get("group", "")).startswith(E * astral + "y") for g in groups), groups  # 묶음 표제가 나왔다
    assert _prefix_leaks(V, wire) == [], groups


@requires_chromium
async def test_b1_control_astral_unregistered_name_byte_identical(tmp_path):
    """대조: 등록 값과 무관한 이모지 섞인 이름(자르기 경계)은 원문 그대로."""
    secret_registry.register_secret_values([V])
    name = E + "y" * 38 + "x" * 75 + "Q8wPlainWord55"
    async with BrowserMCPServer(headless=True, nav_settle=False, recipes=False, handoff_root=tmp_path / "h") as s:
        await s._page.set_content(f"<!doctype html><meta charset=utf-8><title>c</title><button id=b>{name}</button>")
        cut = await s._page.evaluate("() => b.textContent.slice(0,120)")
        out = await s.call_tool("browser_observe_page", {"force_full_tree": True})
    names = [e.get("name") for e in out.data["observation"]["elements"]]
    assert cut in names


# ------------------------------------------------------------------ B2


async def _record(site, tmp_path, kind, value, register=True):
    root = tmp_path / kind
    async with BrowserMCPServer(headless=True, profile="recipe-" + kind, nav_settle=False, recipes=True,
                                handoff_root=root / "h", pre_approved_actions=("click:*",)) as s:
        if register and kind in VIA_URL:
            known = await s.call_tool("browser_navigate", {"url": site + "/register?password=" + quote(value, safe="")})
            assert known.success and len(secret_registry.REGISTRY) > 0
        nav = await s.call_tool("browser_navigate", {"url": site + "/case/" + kind})
        assert nav.success, nav.error_message
        if register and kind not in VIA_URL:
            secret_registry.register_secret_values([value])
        obs = await s.call_tool("browser_observe_page", {})
        target = next(e for e in obs.data["observation"]["elements"] if e["role"] == "button")
        done = await s.call_tool("browser_click", {"element_id": target["element_id"], "epoch": obs.snapshot_epoch})
        effect = await s._page.locator("#result").text_content()
    assert done.success and effect == "changed", (kind, done.error_message)
    return [p.read_text(encoding="utf-8") for p in (tmp_path / "profiles").rglob("recipes.json")
            if ("serve-recipe-" + kind) in str(p)]


@requires_chromium
async def test_b2_control_ordinary_step_saved(site, tmp_path):
    secret_registry.register_secret_values(["n7q4m9Key81"])
    texts = await _record(site, tmp_path, "control", "ordinary", register=False)
    assert any("activate ordinary" in t for t in texts), "대조 레시피가 저장되지 않으면 이 시험은 거부를 재지 못한다"


@requires_chromium
@pytest.mark.parametrize("kind,value", CASES, ids=[c[0] for c in CASES])
async def test_b2_registered_value_never_in_recipe_file(site, tmp_path, kind, value):
    secret_registry.clear()
    texts = await _record(site, tmp_path, kind, value)
    blob = "\n".join(texts)
    assert value.casefold() not in blob.casefold(), kind
    if value.isdigit():
        return  # 숫자뿐인 값(OTP)의 4자 앞부분은 시각·통계 숫자와 우연히 겹친다 — 값 전체만 본다
    assert _prefix_leaks(value, blob) == [], kind
    assert _prefix_leaks(value.casefold(), blob.casefold()) == [], kind
