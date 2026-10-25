"""이동 실패 종류(data.nav_failure) 판정 — WS-69.

오류 코드(계약 동결)는 그대로 두고, 왜 이동하지 못했는지를 data.nav_failure={kind[, net_error]} 와 kind 별
짧은 hint 로 알린다. 판정 순서: egress 신호 코드(프록시 경유 — serve 는 항상) → Playwright 시간 초과 문구 →
Chrome net::ERR_* 표. 닫힌 페이지(E_PAGE_CRASHED)·다운로드(E_DOWNLOAD_FAILED)는 이동 실패 종류가 아니라
호출하는 쪽에서 붙이지 않는다.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

#: data.egress.code → kind. 업스트림 실패(프록시가 502 를 만든 경우)와 정책 차단.
_EGRESS_KINDS = {
    "resolve_failed": "dns",
    "connect_failed": "connect",
    "egress_blocked": "blocked",
}

_TIMEOUT_RE = re.compile(r"Timeout \d+(?:\.\d+)?ms exceeded")
_NET_ERR_RE = re.compile(r"net::(ERR_[A-Z0-9_]+)")

_EXACT_KINDS = {
    "ERR_NAME_NOT_RESOLVED": "dns",
    "ERR_NAME_RESOLUTION_FAILED": "dns",
    "ERR_CONNECTION_REFUSED": "connect",
    "ERR_CONNECTION_RESET": "connect",
    "ERR_CONNECTION_CLOSED": "connect",
    "ERR_CONNECTION_FAILED": "connect",
    # 접속 단계의 시간 초과다 — 문서가 느린 'timeout' 과 다르다(리뷰 N4).
    "ERR_CONNECTION_TIMED_OUT": "connect",
    "ERR_ADDRESS_UNREACHABLE": "connect",
    "ERR_INTERNET_DISCONNECTED": "connect",
    "ERR_EMPTY_RESPONSE": "connect",
    # 프록시 경유에서 egress 정보가 없을 때(https CONNECT 실패 등).
    "ERR_TUNNEL_CONNECTION_FAILED": "connect",
    "ERR_PROXY_CONNECTION_FAILED": "connect",
    "ERR_ABORTED": "aborted",
    # 리다이렉트 고리는 'other' — net_error 로 구분한다(확정 N5).
    "ERR_TOO_MANY_REDIRECTS": "other",
}

NAV_FAILURE_HINTS: Dict[str, str] = {
    "dns": "주소(호스트 이름)를 찾지 못함 — 주소를 확인하세요. 같은 주소로 다시 시도해도 소용없습니다.",
    "connect": "서버에 접속하지 못함(연결 거부·끊김) — 주소·포트를 확인하거나 잠시 뒤 다시 시도하세요.",
    "tls": "인증서·보안 연결 문제 — 같은 주소로 다시 시도해도 소용없습니다.",
    "protocol": "사이트와의 통신 규약 오류(HTTP/2·QUIC) — 사이트 쪽 문제일 수 있고, 같은 주소 재시도로 풀릴 수 있습니다.",
    "timeout": "응답이 느려 문서가 열리지 않음 — 주소를 확인하거나 timeout_ms 를 늘려 다시 시도하세요.",
    "aborted": "이동이 중단됨(다른 이동·다운로드 등) — 지금 페이지를 확인하세요.",
    "other": "이동 실패 — error_message 를 확인하세요.",
}
_REDIRECT_HINT = "리다이렉트가 끝없이 이어짐 — 같은 주소 재시도는 소용없습니다(로그인·쿠키 문제일 수 있음)."

#: 2단계(load/networkidle 대기) 시간 초과 — 문서는 열렸다(부분 성공).
LOAD_STATE_TIMED_OUT_HINT = ("네트워크 요청이 계속돼 {requested} 까지 기다리지 못했지만 문서는 열렸습니다"
                             "(domcontentloaded) — 바로 읽어도 됩니다.")
#: 2단계 시간 초과인데 1단계 문서가 다른 문서로 바뀜(R1) — 새 문서의 열림은 증명되지 않아 실패로 둔다.
DOCUMENT_CHANGED_HINT = ("이동 중 다른 문서로 바뀜 — 처음 열린 문서가 {requested} 전에 다른 문서로 넘어갔습니다"
                         "(리다이렉트·스크립트 이동). observe_page 로 지금 페이지를 확인하세요.")


def _net_error_kind(net_error: str) -> str:
    if net_error in _EXACT_KINDS:
        return _EXACT_KINDS[net_error]
    if net_error.startswith(("ERR_CERT_", "ERR_SSL_")) or net_error in (
            "ERR_BAD_SSL_CLIENT_AUTH_CERT", "ERR_INSECURE_RESPONSE"):
        return "tls"
    if net_error.startswith(("ERR_HTTP2_", "ERR_QUIC_")):
        return "protocol"
    return "other"


def classify_nav_failure(message: Any, egress_code: Optional[str] = None) -> Dict[str, str]:
    """이동 실패의 종류 — {kind[, net_error]}. 판정 순서: egress 코드 → 시간 초과 → net::ERR_*."""
    if egress_code in _EGRESS_KINDS:
        return {"kind": _EGRESS_KINDS[egress_code]}
    text = str(message or "")
    if _TIMEOUT_RE.search(text):
        return {"kind": "timeout"}
    m = _NET_ERR_RE.search(text)
    if m is None:
        return {"kind": "other"}
    return {"kind": _net_error_kind(m.group(1)), "net_error": m.group(1)}


def nav_failure_hint(info: Dict[str, str]) -> Optional[str]:
    """kind 별 hint. blocked 는 None — 기존 egress 신호(data.egress 의 reason·open_with)가 그대로 안내한다(확정 N5)."""
    if info.get("kind") == "blocked":
        return None
    if info.get("net_error") == "ERR_TOO_MANY_REDIRECTS":
        return _REDIRECT_HINT
    return NAV_FAILURE_HINTS.get(info.get("kind", "other"), NAV_FAILURE_HINTS["other"])


def is_nav_failure_hint(text: Any) -> bool:
    """이 모듈이 붙인 hint 인가(서버 층이 종류를 고칠 때 바꿔 써도 되는가)."""
    return text in NAV_FAILURE_HINTS.values() or text == _REDIRECT_HINT


def attach_nav_failure(data: Dict[str, Any], message: Any, egress_code: Optional[str] = None) -> Dict[str, Any]:
    """data 에 nav_failure 와 hint 를 붙인다(기존 hint 가 이 모듈 것이 아니면 hint 는 그대로)."""
    info = classify_nav_failure(message, egress_code)
    data["nav_failure"] = info
    hint = data.get("hint")
    new_hint = nav_failure_hint(info)
    if hint is None or hint == "" or is_nav_failure_hint(hint):
        if new_hint is None:
            data.pop("hint", None)
        else:
            data["hint"] = new_hint
    return data
