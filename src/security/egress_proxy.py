"""검증한 IP 로만 접속하는 로컬 Egress 프록시 (WS-29b).

왜 필요한가 (실측, .hermes/state/ws29b/report.md 단계 1):
* ``context.route`` 는 리다이렉트 홉을 보지 못한다 — 공개 주소가 302 로 사설 주소를
  가리키면 그대로 도달했다(allowlist 설정에서도 비허용 도메인 도달).
* WebSocket·WebRTC 는 route 를 거치지 않는다.
* route 에서 도메인을 해석해 검사해도 Chrome 은 따로 해석한다(DNS 재바인딩).

그래서 브라우저를 ``--proxy-server`` 로 이 프록시에 묶는다. 프록시는 모든 요청(매
리다이렉트 홉·서비스워커·WebSocket 터널 포함)에서 호스트를 EgressGuard 로 판정하고,
도메인은 해석한 IP 전부를 검사한 뒤 **검사한 그 IP 로만** 접속한다(재해석 없음).

* HTTPS·WSS·ws 는 ``CONNECT`` 터널만 — TLS 를 열어 보지 않는다(MITM 없음).
* 평문 HTTP 는 absolute-form 요청을 받아 origin-form 으로 바꿔 전달하고 연결은 요청
  하나로 닫는다(같은 연결로 다른 호스트 요청이 섞이지 않게).
* 127.0.0.1 무작위 포트에만 바인드하고 **자격(Basic 인증)을 요구하지 않는다**(WS-47 R3). 브라우저에
  프록시 자격을 주면 Playwright 가 그 자격을 origin 없는 httpCredentials 로 넣어 사이트의 401 에도
  보냈다(실측) — 그래서 어느 브라우저 경로에도 자격을 주지 않는다. 한계: 같은 컴퓨터의 다른 프로세스가
  포트를 찾으면 쓸 수 있다(가드가 허용한 목적지로만 중계). 접속 측 확인은 후속 과제.
* 프록시가 브라우저에 407 을 보이는 일은 없다: 자기 챌린지가 없고, 상류(사이트)의 407 은 502 로 바꾸며
  모든 상류 응답에서 Proxy-Authenticate 류 머리를 뺀다(WS-47 R3).
* 판정 불가(머리 파싱 실패·해석 실패)는 막는다(fail-closed). 프록시가 죽으면 브라우저는
  직접 접속으로 빠지지 않고 실패한다(--proxy-server 고정, 실측).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from security.egress import (
    CAT_LOOPBACK,
    UPSTREAM_CONNECT_FAILED,
    UPSTREAM_RESOLVE_FAILED,
    EgressDecision,
    EgressGuard,
    IPAddress,
    UpstreamFailure,
    _unwrap,
)

logger = logging.getLogger(__name__)

#: 요청 머리 상한(바이트). 넘으면 판정 불가 → 차단.
MAX_HEAD_BYTES = 64 * 1024
#: 상류 응답 머리 상한(바이트, Chromium 과 같은 256KiB). 넘으면 502(판정 불가).
MAX_UPSTREAM_HEAD_BYTES = 256 * 1024
#: 업스트림 접속 대기 상한(초)
CONNECT_TIMEOUT_S = 10.0
#: 클라이언트가 머리를 보내기까지 기다리는 상한(초)
HEAD_TIMEOUT_S = 30.0
#: 응답 머리 — 차단 이유를 브라우저 쪽 진단에 남긴다(에이전트용 이유는 가드 기록으로 간다).
BLOCK_HEADER = "X-Agent-Browser-Egress"
#: 프록시가 스스로 만든 업스트림 실패(502) 표시 머리 값의 접두. 사이트가 실제로 준 502 와
#: 구별한다(가드의 업스트림 실패 기록과 함께, WS-29b R1).
UPSTREAM_FAIL_MARK = "upstream-failed"
#: 상류(사이트)가 407 을 줘서 프록시가 502 로 바꿨다는 표시(WS-47 R3). 가드 기록은 남기지 않는다.
UPSTREAM_407_MARK = "upstream-407-replaced"
#: 상류 응답 머리를 판정할 수 없어 502 로 막았다는 표시(WS-47 R3).
UPSTREAM_BAD_RESPONSE_MARK = "upstream-bad-response"


class UpstreamError(ConnectionError):
    """검증은 통과했지만 업스트림에 닿지 못함. code 는 resolve_failed | connect_failed."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class SelfConnectRefused(Exception):
    """연결하려는 해석된 IP·포트가 이 프록시 자신의 리스너 (WS-47 R2) — egress 차단으로 응답한다."""


#: 업스트림에 넘기지 않는 hop-by-hop 머리
_HOP_HEADERS = frozenset({
    "proxy-authorization", "proxy-connection", "connection", "keep-alive",
    "te", "trailer", "upgrade", "proxy-authenticate",
})

#: 상류 응답에서 브라우저로 넘기지 않는 프록시 인증 계열 머리(WS-47 R3)
_UPSTREAM_DROP_HEADERS = frozenset({
    "proxy-authenticate", "proxy-authentication-info", "proxy-authorization", "proxy-connection",
})

def chromium_proxy_args() -> List[str]:
    """프록시를 쓸 때 Chromium 에 함께 주는 플래그.

    * ``--disable-quic``: HTTP/3(UDP) 직접 접속 경로를 끈다.
    * ``--force-webrtc-ip-handling-policy=disable_non_proxied_udp``: WebRTC 가 프록시를
      거치지 않는 UDP(STUN/TURN·P2P)를 쓰지 못하게 한다(실측: 이 플래그 없으면 STUN 도달).
    """
    return ["--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]


@dataclass
class EgressProxy:
    """EgressGuard 판정을 강제하는 로컬 HTTP 프록시."""

    guard: EgressGuard
    host: str = "127.0.0.1"
    port: int = 0
    _server: Optional[asyncio.base_events.Server] = field(default=None, init=False, repr=False)
    _tasks: "set[asyncio.Task]" = field(default_factory=set, init=False, repr=False)
    #: 프록시가 처리한 연결 수(관측용)
    handled: int = field(default=0, init=False)

    # -- 수명주기 ------------------------------------------------------------

    async def start(self) -> "EgressProxy":
        if self.host not in ("127.0.0.1", "::1"):
            raise ValueError("Egress 프록시는 루프백에만 바인드합니다")
        self._server = await asyncio.start_server(
            self._on_client, self.host, self.port, limit=MAX_HEAD_BYTES
        )
        self.port = self._server.sockets[0].getsockname()[1]
        logger.info("Egress 프록시 시작: %s:%d", self.host, self.port)
        return self

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(BaseException):
                await task
        self._tasks.clear()

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def server_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def playwright_proxy(self) -> Dict[str, str]:
        """chromium.launch(proxy=...) 인자 — 서버 주소만(자격 없음, WS-47 R3)."""
        return {"server": self.server_url}

    def chrome_args(self) -> List[str]:
        """Playwright 를 거치지 않고 띄우는 Chrome(user-chrome)용 인자(Playwright 경로와 같은 무자격 프록시).

        ``<-loopback>`` 은 루프백도 프록시를 거치게 한다(Chrome 기본은 루프백 직접 접속).
        """
        return [f"--proxy-server={self.server_url}", "--proxy-bypass-list=<-loopback>",
                *chromium_proxy_args()]

    # -- 연결 처리 -----------------------------------------------------------

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self.handled += 1
        try:
            await self._serve(reader, writer)
        except (asyncio.CancelledError, ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:  # noqa: BLE001 - 한 연결의 오류가 프록시를 죽이지 않게
            logger.debug("Egress 프록시 연결 처리 오류", exc_info=True)
        finally:
            if task is not None:
                self._tasks.discard(task)
            with contextlib.suppress(Exception):
                writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_S)
        except (asyncio.LimitOverrunError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            await self._reply(writer, 400, "Bad Request", "요청 머리를 판정할 수 없음")
            return
        try:
            request_line, headers = _parse_head(head)
            method, target, version = request_line.split(" ", 2)
        except ValueError:
            await self._reply(writer, 400, "Bad Request", "요청 머리를 판정할 수 없음")
            return

        method_upper = method.upper()
        if method_upper != "CONNECT" and request_target_form(method, target) != "absolute":
            # 프록시 클라이언트는 absolute-form(또는 CONNECT authority-form)만 보낸다 — origin-form 등은
            # 다른 경로로 중계돼 들어온 요청이라 400(RFC 9112 형식 파싱 — 쿼리 속 "://" 에 속지 않음).
            # 자격·realm 이 없어진 뒤로는 비밀을 지키는 경계가 아니라 이른 거절(심층 방어)이다.
            await self._reply(writer, 400, "Bad Request", "absolute-form http 요청만 받습니다")
            return

        if method_upper == "CONNECT":
            await self._connect(target, reader, writer)
            return
        await self._forward_http(method, target, version, headers, reader, writer)

    def _is_self(self, host: str, port: int) -> bool:
        return port == self.port and host.strip("[]") in ("127.0.0.1", "localhost", "::1", self.host)

    def _listeners(self) -> List[Tuple[str, int]]:
        """이 프록시가 실제로 듣는 주소·포트 전부(바인드한 소켓 기준)."""
        out: List[Tuple[str, int]] = []
        server = self._server
        for sock in (server.sockets if server is not None else ()):
            with contextlib.suppress(OSError):
                name = sock.getsockname()
                out.append((str(name[0]), int(name[1])))
        if not out:
            out.append((self.host, self.port))
        return out

    def _self_blocked(self, decision: EgressDecision) -> EgressDecision:
        return EgressDecision(False, decision.url, None, "프록시 자기 자신(해석된 IP·포트)",
                              host=decision.host, category=CAT_LOOPBACK)

    async def _decide(self, url: str, host: str, port: int) -> EgressDecision:
        if self._is_self(host, port):
            d = EgressDecision(False, url, None, "프록시 자기 자신", host=host, category="loopback")
            return d
        decision = await self.guard.evaluate_async(url)
        if not decision.allowed:
            self.guard.record_block(decision)
            logger.info("Egress 프록시 차단: %s (%s)", host, decision.category)
        return decision

    async def _open_upstream(self, decision: EgressDecision, port: int):
        """검증한 IP 로만 접속한다. 검증한 IP 가 없으면(해석 실패) 접속하지 않는다."""
        if not decision.resolved:
            raise UpstreamError(f"업스트림 해석 실패: {decision.host}", UPSTREAM_RESOLVE_FAILED)
        listeners = self._listeners()
        # 해석된 IP 중 하나라도 이 프록시 리스너면 요청 전체를 막는다(다음 IP 로 넘어가지 않음).
        if any(targets_listener(ip, port, listeners) for ip in decision.resolved):
            raise SelfConnectRefused(decision.host)
        last: Optional[BaseException] = None
        for ip in decision.resolved:
            try:
                return await asyncio.wait_for(asyncio.open_connection(ip, port, limit=MAX_UPSTREAM_HEAD_BYTES),
                                              CONNECT_TIMEOUT_S)
            except (OSError, asyncio.TimeoutError) as exc:
                last = exc
        raise UpstreamError(f"업스트림 접속 실패: {decision.host} ({type(last).__name__})",
                            UPSTREAM_CONNECT_FAILED)

    async def _reply_upstream_failure(self, writer, decision: EgressDecision, port: int,
                                      exc: UpstreamError) -> None:
        """업스트림 실패를 가드에 기록하고 프록시 표시 머리를 단 502 를 돌려준다.

        에이전트에게는 MCP 경계가 이 기록으로 이동 실패(data.egress.code)를 알린다 —
        사이트가 실제로 준 502 는 기록이 없으므로 구별된다.
        """
        self.guard.record_upstream_failure(
            UpstreamFailure(host=decision.host, code=exc.code, url=decision.url, port=port)
        )
        logger.info("Egress 프록시 업스트림 실패: %s (%s)", decision.host, exc.code)
        await self._reply(writer, 502, "Bad Gateway", exc.code,
                          extra={BLOCK_HEADER: f"{UPSTREAM_FAIL_MARK}; code={exc.code}"})

    async def _upstream_or_reply(self, writer, decision: EgressDecision, port: int):
        """업스트림을 연다 — 못 열면 응답(자기 자신: egress 차단 403, 그 밖: 프록시 표시 502)하고 None."""
        try:
            return await self._open_upstream(decision, port)
        except SelfConnectRefused:
            blocked = self._self_blocked(decision)
            logger.info("Egress 프록시 자기접속 차단: %s:%d", decision.host, port)
            await self._reply(writer, 403, "Forbidden", "egress_blocked",
                              extra={BLOCK_HEADER: f"blocked; category={blocked.category}"})
        except UpstreamError as exc:
            await self._reply_upstream_failure(writer, decision, port, exc)
        return None

    async def _connect(self, target: str, reader, writer) -> None:
        host, port = _split_authority(target, default_port=443)
        if host is None:
            await self._reply(writer, 400, "Bad Request", "CONNECT 대상 판정 불가")
            return
        h = f"[{host}]" if ":" in host else host
        decision = await self._decide(f"https://{h}:{port}/", host, port)
        if not decision.allowed:
            await self._reply(writer, 403, "Forbidden", "egress_blocked",
                              extra={BLOCK_HEADER: f"blocked; category={decision.category}"})
            return
        upstream = await self._upstream_or_reply(writer, decision, port)
        if upstream is None:
            return
        up_reader, up_writer = upstream
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await _pipe_both(reader, writer, up_reader, up_writer)

    async def _forward_http(self, method, target, version, headers, reader, writer) -> None:
        try:
            parts = urlsplit(target)
        except ValueError:
            parts = None
        if parts is None or parts.scheme.lower() != "http" or not parts.hostname:
            await self._reply(writer, 400, "Bad Request", "absolute-form http 요청만 받습니다")
            return
        host = parts.hostname
        try:
            port = parts.port or 80
        except ValueError:
            await self._reply(writer, 400, "Bad Request", "포트 판정 불가")
            return
        decision = await self._decide(target, host, port)
        if not decision.allowed:
            await self._reply(writer, 403, "Forbidden", "egress_blocked",
                              extra={BLOCK_HEADER: f"blocked; category={decision.category}"})
            return
        upstream = await self._upstream_or_reply(writer, decision, port)
        if upstream is None:
            return
        up_reader, up_writer = upstream
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        lines = [f"{method} {path} {version}"]
        for name, value in headers:
            if name.lower() in _HOP_HEADERS:
                continue
            lines.append(f"{name}: {value}")
        lines.append("Connection: close")
        up_writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        await up_writer.drain()
        # 요청 본문은 계속 흘려보내되, 응답은 머리를 먼저 검사한다(상류 407 재작성·프록시 인증 머리 제거).
        upload = asyncio.ensure_future(_pipe(reader, up_writer))
        try:
            if await self._relay_response_head(up_reader, writer):
                await _pipe(up_reader, writer)
                await upload
        finally:
            if not upload.done():
                upload.cancel()
                with contextlib.suppress(BaseException):
                    await upload
            with contextlib.suppress(Exception):
                up_writer.close()

    async def _relay_response_head(self, up_reader, writer) -> bool:
        """상류 응답 머리를 검사해 넘긴다. False 면 이미 대신 답했다(연결을 닫을 것).

        * 상류(사이트)의 407 은 브라우저에 중계하지 않는다 — 브라우저는 프록시 연결에서 받은 407 을
          프록시의 챌린지로 처리한다(CDP 도 source=Proxy·우리 프록시 출처로 보고, Playwright 가 자격으로
          답하려 든다). 프록시 표시 머리를 단 502 로 바꾸고 본문도 넘기지 않는다.
        * 모든 상류 응답에서 Proxy-Authenticate·Proxy-Authentication-Info·Proxy-Authorization 류
          머리를 뺀다(hop-by-hop, 사이트가 줄 이유가 없다).
        * 1xx(101 제외)는 머리를 넘기고 다음 머리를 다시 검사한다. 판정 불가 머리는 502(fail-closed).
        """
        while True:
            try:
                head = await up_reader.readuntil(b"\r\n\r\n")
                lines = head.decode("latin-1").split("\r\n")
                status_line = lines[0]
                version, status, _reason = (status_line.split(" ", 2) + [""])[:3]
                if not version.startswith("HTTP/1.") or not status.isdigit() or len(status) != 3:
                    raise ValueError("응답 상태 줄 형식 오류")
                code = int(status)
            except (asyncio.LimitOverrunError, asyncio.IncompleteReadError, ValueError):
                await self._reply(writer, 502, "Bad Gateway", "upstream_bad_response",
                                  extra={BLOCK_HEADER: UPSTREAM_BAD_RESPONSE_MARK})
                return False
            if code == 407:
                logger.info("Egress 프록시: 상류 407 을 502 로 바꿈(브라우저에 프록시 챌린지로 보이지 않게)")
                await self._reply(writer, 502, "Bad Gateway", "upstream_407",
                                  extra={BLOCK_HEADER: UPSTREAM_407_MARK})
                return False
            writer.write("\r\n".join([status_line, *_without_proxy_auth(lines[1:])]).encode("latin-1"))
            await writer.drain()
            if not (100 <= code < 200) or code == 101:
                return True

    @staticmethod
    async def _reply(writer, code: int, reason: str, body: str, extra: Optional[Dict[str, str]] = None) -> None:
        data = body.encode()
        head = [f"HTTP/1.1 {code} {reason}", f"Content-Length: {len(data)}",
                "Content-Type: text/plain; charset=utf-8", "Connection: close"]
        for k, v in (extra or {}).items():
            head.append(f"{k}: {v}")
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + data)
        with contextlib.suppress(Exception):
            await writer.drain()


def _plain_ip(text: str) -> Optional[IPAddress]:
    """접속에 쓰일 IP 문자열 → 주소(IPv4-mapped 등 안의 IPv4 는 꺼냄). 판정 불가면 None."""
    try:
        addr = ipaddress.ip_address(str(text).strip("[]").split("%", 1)[0])
    except ValueError:
        return None
    return _unwrap(addr)


def _is_local_address(addr: IPAddress) -> bool:
    """이 컴퓨터의 주소인가 — 루프백·미지정이거나 실제로 bind 할 수 있으면(로컬 인터페이스)."""
    if addr.is_loopback or addr.is_unspecified:
        return True
    family = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((str(addr), 0))
        return True
    except OSError:
        return False


def targets_listener(ip: str, port: int, listeners: Sequence[Tuple[str, int]]) -> bool:
    """``ip:port`` 로 접속하면 ``listeners`` 중 하나(이 프록시 자신)에 닿는가 (WS-47 R2).

    호스트 문자열이 아니라 실제로 연결할 IP 로 판정한다 — DNS 별칭·재해석·``localhost.``·
    10진/8진 표기·IPv4-mapped IPv6 와 무관하다. 판정 불가 IP 는 자기 자신으로 본다(fail-closed).
    * 정확히 같은 주소·포트 → 자기 자신.
    * 목적지가 미지정 주소(0.0.0.0·::) → 같은 포트의 로컬 리스너에 닿는다(흔한 스택 동작).
    * 리스너가 0.0.0.0/:: 바인드 → 같은 포트의 모든 로컬 주소가 자기 자신.
    다른 포트, 또는 같은 포트라도 리스너가 받지 않는 다른 IP(예: 127.0.0.1 리스너와 ::1)는 아니다.
    """
    target = _plain_ip(ip)
    for host, lport in listeners:
        if int(lport) != int(port):
            continue
        if target is None:
            return True
        bound = _plain_ip(host)
        if bound is None:
            return True
        if bound.is_unspecified:
            if _is_local_address(target):
                return True
            continue
        if target.is_unspecified or target == bound:
            return True
    return False


_ABSOLUTE_FORM = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")


def request_target_form(method: str, target: str) -> str:
    """요청 대상의 형식(RFC 9112 §3.2): origin | absolute | authority | asterisk | invalid.

    부분문자열 검색이 아니라 시작 형식으로 판정한다 — ``/probe?next=http://x`` 는 origin-form.
    * origin-form: ``/`` 로 시작(absolute-path [ "?" query ]).
    * absolute-form: ``scheme://`` 로 시작(scheme = ALPHA *( ALPHA / DIGIT / "+" / "-" / "." )).
    * authority-form: CONNECT 의 ``host:port``.
    * asterisk-form: OPTIONS 의 ``*``.
    """
    if method.upper() == "CONNECT":
        return "authority" if target and not target.startswith("/") and "/" not in target else "invalid"
    if target.startswith("/"):
        return "origin"
    if _ABSOLUTE_FORM.match(target):
        return "absolute"
    if target == "*":
        return "asterisk"
    return "invalid"


def _without_proxy_auth(lines: List[str]) -> List[str]:
    """응답 머리 줄(빈 끝 줄 포함)에서 프록시 인증 계열 머리를 뺀다 — 나머지는 원문 그대로.

    obs-fold 이어짐 줄(공백·탭 시작)은 앞 머리를 따라 함께 빠지거나 남는다.
    """
    out: List[str] = []
    dropping = False
    for line in lines:
        if line[:1] in (" ", "\t") and line.strip():
            if not dropping:
                out.append(line)
            continue
        dropping = line.split(":", 1)[0].strip().lower() in _UPSTREAM_DROP_HEADERS if line else False
        if not dropping:
            out.append(line)
    return out


def _parse_head(head: bytes) -> Tuple[str, List[Tuple[str, str]]]:
    text = head.decode("latin-1")
    lines = text.split("\r\n")
    request_line = lines[0]
    headers: List[Tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip():
            raise ValueError("머리 줄 형식 오류")
        headers.append((name, value.strip()))
    return request_line, headers


def _split_authority(target: str, *, default_port: int) -> Tuple[Optional[str], int]:
    try:
        parts = urlsplit("//" + target)
        host = parts.hostname
        port = parts.port or default_port
    except ValueError:
        return None, 0
    return host, port


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await src.read(65536)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        with contextlib.suppress(Exception):
            if dst.can_write_eof():
                dst.write_eof()


async def _pipe_both(c_reader, c_writer, u_reader, u_writer) -> None:
    try:
        await asyncio.gather(_pipe(c_reader, u_writer), _pipe(u_reader, c_writer))
    finally:
        with contextlib.suppress(Exception):
            u_writer.close()
