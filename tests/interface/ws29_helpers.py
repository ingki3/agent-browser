"""WS-29 R1 테스트 공용 도구 — 사람 역할(확인 코드 포함 승인)."""

from __future__ import annotations

import re
from typing import Any, List, Optional

from interface import handoff
from interface.handoff import HandoffHub, write_command

CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def code_from_banner(texts: List[Optional[str]]) -> str:
    """창 오버레이에 띄운 문구(테스트가 가로챈 것)에서 6자리 코드를 꺼낸다 — 사람이 창을 보는 역할."""
    for text in reversed(texts):
        m = CODE_RE.search(text or "")
        if m:
            return m.group(1)
    raise AssertionError(f"오버레이에 코드가 없음: {texts!r}")


def hub_show_code(hub: HandoffHub, approval_id: str) -> str:
    """hub 만 있는 테스트: 사람이 코드 표시를 요청 → 서버(여기서는 테스트)가 창에 띄웠다고 알림."""
    shown = handoff.read_pending_approval(hub.root, hub.server_id, approval_id)
    write_command(hub.root, hub.server_id, "show_code", approval_id=approval_id,
                  action_digest=shown["action_digest"])
    hub.poll()
    job = hub.take_code_job()
    assert job is not None and job.approval_id == approval_id
    hub.code_shown(job.nonce, True)
    return job.code


def hub_approve(hub: HandoffHub, approval_id: str, code: Optional[str] = None) -> str:
    """hub 만 있는 테스트: 코드 표시 → 그 코드로 승인. 쓴 코드를 돌려준다."""
    if code is None:
        code = hub_show_code(hub, approval_id)
    shown = handoff.read_pending_approval(hub.root, hub.server_id, approval_id)
    write_command(hub.root, hub.server_id, "approve", approval_id=approval_id,
                  action_digest=shown["action_digest"], code=code)
    hub.poll()
    return code


async def srv_approve(srv: Any, approval_id: str) -> str:
    """BrowserMCPServer 테스트: 사람이 CLI 로 코드 요청 → 창(가로챈 오버레이)에서 읽고 → 입력."""
    root, sid = srv.hub.root, srv.hub.server_id
    shown = handoff.read_pending_approval(root, sid, approval_id)
    write_command(root, sid, "show_code", approval_id=approval_id,
                  action_digest=shown["action_digest"])
    await srv._poll_handoff()
    code = code_from_banner(srv.banners)
    write_command(root, sid, "approve", approval_id=approval_id,
                  action_digest=shown["action_digest"], code=code)
    await srv._poll_handoff()
    return code
