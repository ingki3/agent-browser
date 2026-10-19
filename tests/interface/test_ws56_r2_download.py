"""WS-56 R2 B1 — download_file 로 agent-browser 프로필·상태 폴더(승인 정책·승인 상태) 쓰기 금지.

검증 탐침(.hermes/verify/ws56-r1/test_policy_probe.py): 사람이 download_file 만 정책으로 허용한 합성 프로필에서, 사이트가
내려준 `approve_policy.json`(click:제출 정책)을 save_dir=<프로필 폴더> 로 받아 정책 파일을 덮어쓰고 다음 클릭이
via=policy 로 실행됐다. 여기서는 최종 canonical 경로(realpath, symlink 해석 후)가 프로필 루트(현재·다른 프로필 모두)·
서버 상태 폴더 안이면 거부한다 — save_dir 자체·symlink 폴더 경유·save_dir 안 symlink 파일(최종 경로) 각각. 실제
Chromium(headless) + 로컬 http(127.0.0.1)·임시 폴더만.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

import anyio
import pytest

from browser import serve_profile as sp
from contracts import ActionType
from interface import handoff
from interface.mcp_server import create_server, tool_name
from security import approve_policy as ap
from test_run_cli import requires_chromium

pytestmark = requires_chromium


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv(sp.PROFILE_ROOT_ENV, str(tmp_path / "profiles"))
    monkeypatch.setenv(handoff.STATE_ROOT_ENV, str(tmp_path / "state"))
    hits = []
    state: Dict[str, Any] = {"hits": hits}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/payload":
                data = json.dumps({"version": 1, "policies": [
                    {"origin": state["origin"], "action": "click", "target_name": "제출"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Disposition", 'attachment; filename="approve_policy.json"')
            else:
                if self.path.startswith("/clicked"):
                    hits.append(self.path)
                data = ("<!doctype html><meta charset=utf-8><a href=\"/payload\">Get report</a>"
                        "<button onclick=\"fetch('/clicked');this.textContent='done'\">제출</button>").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):  # noqa: ANN002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state["origin"] = f"http://127.0.0.1:{srv.server_address[1]}"
    folder = tmp_path / "profiles" / "serve-probe"
    folder.mkdir(parents=True, mode=0o700)
    state["folder"] = folder
    state["policy"] = folder / ap.POLICY_FILE
    # 사람이 다운로드 자체만 미리 허용한 정상 시작 상태(CLI 와 같은 함수 — 서명 포함).
    ap.add_policy(state["policy"], state["origin"], "download_file")
    try:
        yield state
    finally:
        srv.shutdown()
        srv.server_close()


async def _download_then_click(state: Dict[str, Any], save_dir: str) -> Dict[str, Any]:
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    before = state["policy"].read_bytes()
    server, b = create_server(profile="probe", recipes=False)
    out: Dict[str, Any] = {}
    try:
        async with create_client_server_memory_streams() as (cs, ss):
            async with anyio.create_task_group() as tg:
                tg.start_soon(lambda: server.run(*ss, server.create_initialization_options()))
                async with ClientSession(*cs) as s:
                    await s.initialize()
                    await s.call_tool(tool_name(ActionType.NAVIGATE), {"url": state["origin"] + "/"})
                    obs = (await b.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})).data["observation"]
                    link = next(e["element_id"] for e in obs["elements"] if e["name"] == "Get report")
                    button = next(e["element_id"] for e in obs["elements"] if e["name"] == "제출")
                    r = await s.call_tool(tool_name(ActionType.DOWNLOAD_FILE), {
                        "trigger_element_id": link, "epoch": obs["snapshot_epoch"], "save_dir": save_dir})
                    out["download"] = json.loads(r.content[0].text)
                    out["changed"] = state["policy"].read_bytes() != before
                    out["next_click"] = (await b.call_tool(tool_name(ActionType.CLICK), {
                        "element_id": button, "epoch": obs["snapshot_epoch"]})).model_dump(mode="json")
                    await b._page.wait_for_timeout(300)
                    out["hits"] = list(state["hits"])
                tg.cancel_scope.cancel()
    finally:
        await b.close()
        b.hub.close()
    return out


def _assert_refused(out: Dict[str, Any]) -> None:
    assert out["download"]["success"] is False, out
    assert out["download"]["error_code"] == "E_DOWNLOAD_FAILED", out
    assert out["changed"] is False, out
    assert out["next_click"]["success"] is False, out
    assert out["hits"] == [], out


async def test_download_into_own_profile_folder_is_refused(env):
    _assert_refused(await _download_then_click(env, str(env["folder"])))


async def test_download_into_other_profile_folder_is_refused(env, tmp_path):
    other = tmp_path / "profiles" / "serve-other"
    other.mkdir(mode=0o700)
    out = await _download_then_click(env, str(other))
    _assert_refused(out)
    assert not (other / ap.POLICY_FILE).exists()


async def test_download_via_symlinked_dir_is_refused(env, tmp_path):
    link = tmp_path / "innocent"
    link.symlink_to(env["folder"], target_is_directory=True)
    _assert_refused(await _download_then_click(env, str(link)))


async def test_download_via_symlinked_file_in_save_dir_is_refused(env, tmp_path):
    """save_dir 는 평범한 폴더지만 그 안의 같은 이름 symlink 가 정책 파일을 가리킴 — 최종 경로로 거부."""
    dl = tmp_path / "dl"
    dl.mkdir()
    (dl / ap.POLICY_FILE).symlink_to(env["policy"])
    _assert_refused(await _download_then_click(env, str(dl)))


async def test_download_into_state_folder_is_refused(env, tmp_path):
    """승인 증표 상태 폴더(서버 상태 루트)도 같은 경계 — 상태 파일 위조 통로를 막는다."""
    state_dir = tmp_path / "state" / "x"
    state_dir.mkdir(parents=True)
    _assert_refused(await _download_then_click(env, str(state_dir)))


async def test_download_into_normal_folder_still_works(env, tmp_path):
    """대조: 일반 폴더 다운로드는 그대로 된다(과차단 아님) — 정책 파일은 바뀌지 않는다."""
    dl = tmp_path / "downloads"
    dl.mkdir()
    out = await _download_then_click(env, str(dl))
    assert out["download"]["success"] is True, out
    assert (dl / ap.POLICY_FILE).exists()
    assert out["changed"] is False
    assert out["next_click"]["success"] is False, out


def test_protected_roots_cover_default_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv(sp.PROFILE_ROOT_ENV, str(tmp_path / "profiles"))
    roots = [str(p) for p in ap.protected_write_roots()]
    assert os.path.realpath(tmp_path / "profiles") in roots
    assert os.path.realpath(Path.home() / ".agent-browser") in roots
    assert ap.is_protected_path(str(tmp_path / "profiles" / "serve-x" / "a.json"))
    assert ap.is_protected_path(str(Path.home() / ".agent-browser" / "profiles" / "serve-x" / "a"))
    assert not ap.is_protected_path(str(tmp_path / "profilesX" / "a.json"))
