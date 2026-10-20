"""WS-58 R1 — 로그인 사건 신호를 페이지가 숨길 수 없는 층에서 받는다 + 입력 사건은 실제 입력 frame origin.

B1: 조작권(take~release) 동안 (i) 어떤 탭이든 그 origin 으로 간 POST/PUT 요청(브라우저 Network 계층 —
    페이지 JS 가 이벤트 전파를 막아도 요청은 보인다), (ii) 쿠키 저장소의 추가·값 변경(이름·값 저장 없음)이 있으면
    그 origin 을 human_control 로 기록한다. 감시 설치가 실패하면 그 구간에 방문한 origin 전부를 기록(fail-closed).
B2: 자격증명 치환 입력·secret_wanted 는 에이전트의 활성 frame(실제 입력 대상 문서) origin 을 기록한다.
NB1: session_reuse.open_logged_in 이 login_with_handoff 에 ledger_profile 을 넘긴다.

로컬 루프백 사이트·합성 값만 쓴다.
"""
from __future__ import annotations

import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from browser import login_ledger as ll
from interface.mcp_server import BrowserMCPServer
from test_run_cli import requires_chromium
from test_ws58_login_ledger_retry import _release, _take, native, navigate, renamed, serve_site


@pytest.fixture
def site():
    with serve_site() as state:
        yield state


#: 페이지 스크립트가 window capture 로 이벤트 전파를 끊는다(기본 동작은 막지 않음 — 실제 POST 는 나간다).
HIDE_EVENTS = """() => { for (const name of ['click', 'submit', 'keydown'])
    window.addEventListener(name, e => e.stopImmediatePropagation(), true); }"""


async def _human_password_login(server, site, hide_events):
    page = server._page  # the human, in the window
    await page.goto(site.url + "/form")
    if hide_events:
        await page.evaluate(HIDE_EVENTS)
    await page.fill("#user", "synthetic-user")
    await page.fill("#pw", "synthetic-password-58")
    async with page.expect_navigation():
        await page.click("#go")
    assert len(site.received("/login")) == 1, "a real login POST must reach the site"


# -- B1: 페이지가 숨길 수 없는 신호 -------------------------------------------------------------


@requires_chromium
@pytest.mark.parametrize("hide_events", [False, True])
async def test_real_post_login_recorded_even_when_page_hides_events(site, hide_events):
    # No cookie at all: only the request itself proves the login (rule (b) cannot help).
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        assert server.open_handoff()
        await _take(server)
        await _human_password_login(server, site, hide_events)
        if hide_events:
            assert server._login_watch.submitted == set()  # the DOM watch was blinded
        await _release(server)
        assert server._headless_retry.logins.memory == {site.url: ll.SOURCE_HUMAN_CONTROL}
        native(await navigate(server, site.url + "/one"), "login_recorded")
    assert len(site.received("/one")) == 1 and site.headless("/one")


@requires_chromium
async def test_verifier_repro_hidden_submit_with_js_session_cookie(site):
    # Verifier B1 repro: hidden events + non-HttpOnly session cookie (rule (b) ignores it).
    site.login_cookies = ["signed_in=synthetic-session; Path=/; Max-Age=3600"]
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        assert server.open_handoff()
        await _take(server)
        await _human_password_login(server, site, hide_events=True)
        await _release(server)
        assert site.url in server._headless_retry.logins.memory
        native(await navigate(server, site.url + "/one"), "login_recorded")
    assert ["signed_in=" in h.get("cookie", "") for h in site.received("/one")] == [True]
    assert all("HeadlessChrome" in h["user-agent"] for h in site.received("/one"))


@requires_chromium
async def test_forged_submit_event_without_post_still_records(site):
    # Control group: the page forges a submit event with a password value but sends no request. The DOM
    # watch is kept as an auxiliary signal, so this over-records (refusal direction — allowed).
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        assert server.open_handoff()
        await _take(server)
        await server._page.goto(site.url + "/form")
        await server._page.evaluate("""() => {
            document.querySelector('#pw').value = 'synthetic-fake';
            document.querySelector('form').dispatchEvent(new Event('submit', {bubbles: true}));
        }""")
        assert not site.received("/login")
        await _release(server)
        assert server._headless_retry.logins.memory == {site.url: ll.SOURCE_HUMAN_CONTROL}
        native(await navigate(server, site.url + "/one"), "login_recorded")


@requires_chromium
@pytest.mark.parametrize("method", ["POST", "PUT"])
async def test_script_request_with_body_records_origin(site, method):
    # fetch/XHR logins never fire a form submit — the request is the signal (body never read).
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        assert server.open_handoff()
        await _take(server)
        await server._page.goto(site.url + "/form")
        await server._page.evaluate(HIDE_EVENTS)
        await server._page.evaluate("""(m) => new Promise(done => {
            const x = new XMLHttpRequest(); x.open(m, '/login'); x.onloadend = done; x.send('pw=synthetic');
        })""", method)
        await _release(server)
        assert server._headless_retry.logins.memory == {site.url: ll.SOURCE_HUMAN_CONTROL}
        native(await navigate(server, site.url + "/one"), "login_recorded")


@requires_chromium
async def test_new_js_cookie_during_control_records_cookie_domain(site):
    site.login_cookies = ["visitor=synthetic-v; Path=/; Max-Age=3600"]  # not HttpOnly
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        assert server.open_handoff()
        await _take(server)
        await server._page.goto(site.url + "/plant")
        await _release(server)
        assert server._headless_retry.logins.memory == {"http://127.0.0.1": ll.SOURCE_HUMAN_CONTROL}
        native(await navigate(server, site.url + "/one"), "login_recorded")


@requires_chromium
async def test_cookie_value_change_during_control_records(site):
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        await server._page.goto(site.url + "/form")
        await server._page.context.add_cookies([{"name": "pref", "value": "v1", "url": site.url}])
        assert server.open_handoff()
        await _take(server)
        await server._page.evaluate("() => { document.cookie = 'pref=v2; path=/'; }")
        await _release(server)
        assert server._headless_retry.logins.memory == {"http://127.0.0.1": ll.SOURCE_HUMAN_CONTROL}


@requires_chromium
async def test_reading_only_during_control_records_nothing(site):
    # Opposite direction: GET-only browsing without cookie changes is not a login.
    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        assert server.open_handoff()
        await _take(server)
        await server._page.goto(site.url + "/form")
        await server._page.goto(site.url + "/form?again=1")
        await _release(server)
        assert server._headless_retry.logins.memory == {}
        renamed(await navigate(server, site.url + "/one"))


@requires_chromium
async def test_watch_install_failure_records_visited_origins(site, monkeypatch):
    from playwright.async_api import BrowserContext

    async def broken(self, page):
        raise RuntimeError("no CDP session")

    async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
        monkeypatch.setattr(BrowserContext, "new_cdp_session", broken)  # after start: only the watch fails
        assert server.open_handoff()
        await _take(server)
        await server._page.goto(site.url + "/form")  # reading only — no POST, no cookie
        await _release(server)
        assert server._headless_retry.logins.memory == {site.url: ll.SOURCE_HUMAN_CONTROL}
        native(await navigate(server, site.url + "/one"), "login_recorded")


class _Page:
    def __init__(self, url):
        self.url = url
        self.frames = []
        self.context = None

    def is_closed(self):
        return False


class _NoListenerContext:
    """요청 감시를 걸 수 없는 컨텍스트(이벤트 등록 실패)."""

    def __init__(self, url):
        self.pages = [_Page(url)]

    def on(self, *_):
        raise RuntimeError("listener refused")

    async def cookies(self):
        return []


async def test_request_listener_failure_is_fail_closed():
    from browser.login_watch import ControlLoginWatch

    watch = ControlLoginWatch()
    context = _NoListenerContext("https://shop.example.test/cart")
    await watch.ensure(context)
    assert watch.degraded
    found = await watch.finish(context)
    assert found == {"https://shop.example.test": "watch_unavailable"}


async def test_request_signal_keeps_only_method_and_origin():
    from browser.login_watch import ControlLoginWatch

    class Req:
        def __init__(self, method, url, nav=False):
            self.method, self.url, self._nav = method, url, nav

        def is_navigation_request(self):
            return self._nav

    watch = ControlLoginWatch()
    watch._on_request(Req("POST", "https://login.example.test/auth?token=secret-58"))
    watch._on_request(Req("put", "http://api.example.test:8080/x"))
    watch._on_request(Req("GET", "https://read.example.test/"))
    watch._on_request(Req("POST", "data:text/plain,x"))
    assert watch.posted == {"https://login.example.test", "http://api.example.test:8080"}
    found = await watch.finish(None)
    assert found == {"https://login.example.test": "network_request", "http://api.example.test:8080": "network_request"}
    assert "secret-58" not in repr(watch.__dict__)


# -- B2: 입력 사건은 실제 입력 대상 문서(활성 frame) origin ------------------------------------------

SCOPE_HOSTS = {"portal.test", "auth.test", "scope.test", "login.scope.test", "portal.scope.test", "auth.scope.test"}


@pytest.fixture
def scope_dns(monkeypatch):
    original = socket.getaddrinfo

    def resolve(host, port, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else host
        if name in SCOPE_HOSTS:
            return original("127.0.0.1", port, *args, **kwargs)
        return original(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


@contextmanager
def frame_site():
    """/embed/<host> 는 그 호스트의 /form 을 iframe(#auth)으로 싣는다. /probe 는 헤드리스 이름에 403."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, body):
            raw = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain" if self.path == "/robots.txt" else "text/html")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            host = self.headers["Host"].split(":")[0]
            requests.append((host, self.path, self.headers.get("User-Agent", "")))
            if self.path == "/robots.txt":
                return self._reply(200, "User-agent: *\nAllow: /\n")
            if self.path.startswith("/embed/"):
                inner = self.path.split("/")[-1]
                return self._reply(200, "<!doctype html><title>Portal</title>"
                                   f"<iframe id='auth' src='http://{inner}:{self.server.server_port}/form'></iframe>")
            if self.path == "/form":
                return self._reply(200, "<!doctype html><title>Sign in</title><form method='post' action='/signed'>"
                                   "<input id='pw' name='pw' type='password' aria-label='Password'>"
                                   "<button>Sign in</button></form>")
            if self.path.startswith("/probe"):
                headless = "HeadlessChrome" in self.headers.get("User-Agent", "")
                return self._reply(403 if headless else 200, "<!doctype html><title>Read</title>")
            return self._reply(200, "<!doctype html><title>Home</title>")

        def log_message(self, *_):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_port, requests
    finally:
        httpd.shutdown()
        thread.join()
        httpd.server_close()


#: 검증 scope 표본 4종 — 형제·부모→자식·자식→부모·다른 등록 도메인.
PLACEMENTS = [("portal.test", "auth.test"), ("portal.scope.test", "auth.scope.test"),
              ("auth.scope.test", "scope.test"), ("scope.test", "login.scope.test")]


async def _open_frame(server, root, frame_host):
    assert (await navigate(server, root + "/embed/" + frame_host)).success
    switched = await server.call_tool("browser_switch_frame", {"frame_selector": "#auth"})
    assert switched.success, switched.data


@requires_chromium
@pytest.mark.parametrize("root_host,frame_host", PLACEMENTS)
async def test_credential_fill_in_iframe_records_frame_origin(scope_dns, root_host, frame_host):
    from security.credentials import BoundSecrets, Credential

    with frame_site() as (port, requests):
        root, frame_origin = f"http://{root_host}:{port}", f"http://{frame_host}:{port}"
        cred = Credential(domain=frame_host, username="synthetic-user", password="synthetic-frame-pass")
        async with BrowserMCPServer(nav_settle=False, recipes=False, secrets=BoundSecrets(cred)) as server:
            await _open_frame(server, root, frame_host)
            obs = await server.call_tool("browser_observe_page", {})
            target = next(e for e in obs.data["observation"]["elements"] if e["name"] == "Password")
            typed = await server.call_tool("browser_type_text", {
                "element_id": target["element_id"], "text": "LOGIN_PASSWORD",
                "epoch": obs.data["observation"]["snapshot_epoch"]})
            assert typed.success and typed.data["secret_resolved"] is True, typed.data
            assert server._headless_retry.logins.memory == {frame_origin: ll.SOURCE_LOGIN_FLOW}
            assert (await server.call_tool("browser_switch_frame", {"to_main": True})).success
            native(await navigate(server, frame_origin + "/probe-frame"), "login_recorded")
        assert [ua for host, path, ua in requests if path == "/probe-frame"][0].count("HeadlessChrome") == 1


@requires_chromium
@pytest.mark.parametrize("root_host,frame_host", PLACEMENTS)
async def test_secret_wanted_in_iframe_records_frame_origin(scope_dns, root_host, frame_host):
    with frame_site() as (port, _requests):
        root, frame_origin = f"http://{root_host}:{port}", f"http://{frame_host}:{port}"
        async with BrowserMCPServer(nav_settle=False, recipes=False) as server:
            assert server.open_handoff()
            await _open_frame(server, root, frame_host)
            server.hub.request("비밀번호 입력", secret_wanted=True)
            await _take(server)
            await _release(server)
            assert server._headless_retry.logins.memory == {frame_origin: ll.SOURCE_HUMAN_CONTROL}


def test_agent_page_url_keeps_root_meaning():
    # The root-first helper is used elsewhere; input events use their own source.
    from interface.server_handoff import HandoffMixin

    class Ctx:
        root_page = _Page("https://portal.example.test/")
        page = _Page("https://auth.example.test/form")

    class Fake(HandoffMixin):
        _page = None
        _dispatcher = type("D", (), {"ctx": Ctx()})()

    assert Fake()._agent_page_url() == "https://portal.example.test/"
    assert Fake()._agent_input_url() == "https://auth.example.test/form"


# -- NB1: session_reuse → login_with_handoff 기록 연결 -----------------------------------------------


async def test_open_logged_in_forwards_ledger_profile(tmp_path, monkeypatch):
    from browser import session_reuse
    from browser.login_flow import LoginOutcome, LoginState
    from security.credentials import Credential

    seen = {}

    async def fake_login(page, cred, **kw):
        seen.update(kw)
        return LoginOutcome(method="auto", state=LoginState.LOGIN_FORM, reason="stop here", elapsed_s=0.0)

    class Page:
        url = "https://login.example.test/"

        async def goto(self, *_a, **_k):
            return None

    class Ctx:
        async def new_page(self):
            return Page()

    class Browser:
        async def new_context(self, **_):
            return Ctx()

    class Store:
        def exists(self, _):
            return False

    monkeypatch.setattr(session_reuse, "login_with_handoff", fake_login)
    folder = tmp_path / "root" / "serve-r"
    out = await session_reuse.open_logged_in(
        Browser(), profile="p", cred=Credential(domain="example.test", username="u", password="synthetic"),
        store=Store(), passphrase="synthetic", login_url="https://login.example.test/",
        check_url="https://login.example.test/home", ledger_profile=folder)
    assert out.source == "failed" and seen["ledger_profile"] == folder
