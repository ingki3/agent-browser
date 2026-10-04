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
* 127.0.0.1 무작위 포트에만 바인드. ``token`` 을 주면 ``Proxy-Authorization: Basic``
  이 일치하는 요청만 받는다(Playwright 가 launch(proxy=...) 자격증명으로 자동 응답).
* 판정 불가(머리 파싱 실패·해석 실패)는 막는다(fail-closed). 프록시가 죽으면 브라우저는
  직접 접속으로 빠지지 않고 실패한다(--proxy-server 고정, 실측).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import logging
import secrets
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from security.egress import EgressDecision, EgressGuard

logger = logging.getLogger(__name__)

#: 요청 머리 상한(바이트). 넘으면 판정 불가 → 차단.
MAX_HEAD_BYTES = 64 * 1024
#: 업스트림 접속 대기 상한(초)
CONNECT_TIMEOUT_S = 10.0
#: 클라이언트가 머리를 보내기까지 기다리는 상한(초)
HEAD_TIMEOUT_S = 30.0
#: 응답 머리 — 차단 이유를 브라우저 쪽 진단에 남긴다(에이전트용 이유는 가드 기록으로 간다).
BLOCK_HEADER = "X-Agent-Browser-Egress"
#: 업스트림에 넘기지 않는 hop-by-hop 머리
_HOP_HEADERS = frozenset({
    "proxy-authorization", "proxy-connection", "connection", "keep-alive",
    "te", "trailer", "upgrade", "proxy-authenticate",
})

#: 프록시 자격증명 사용자 이름(비밀 아님, 토큰이 비밀)
PROXY_USER = "agent-browser"


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
    token: Optional[str] = field(default_factory=lambda: secrets.token_urlsafe(24))

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
        """chromium.launch(proxy=...) 인자. Playwright 가 407 에 자격증명으로 자동 응답한다."""
        out = {"server": self.server_url}
        if self.token:
            out.update(username=PROXY_USER, password=self.token)
        return out

    def chrome_args(self) -> List[str]:
        """Playwright 를 거치지 않고 띄우는 Chrome(user-chrome)용 인자.

        Chrome 은 명령줄로 프록시 자격증명을 받을 수 없다 — 이 경로의 프록시는 토큰 없이
        (token=None) 띄워야 한다(한계: 같은 컴퓨터의 다른 프로세스도 이 포트를 쓸 수 있으나,
        프록시는 가드가 허용한 목적지로만 중계하므로 그 프로세스가 원래 못 가는 곳은 못 간다).
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

        if not self._authorized(headers):
            await self._reply(writer, 407, "Proxy Authentication Required", "",
                              extra={"Proxy-Authenticate": 'Basic realm="agent-browser"'})
            return

        if method.upper() == "CONNECT":
            await self._connect(target, reader, writer)
            return
        await self._forward_http(method, target, version, headers, reader, writer)

    def _authorized(self, headers: List[Tuple[str, str]]) -> bool:
        if not self.token:
            return True
        expected = "Basic " + base64.b64encode(f"{PROXY_USER}:{self.token}".encode()).decode()
        for name, value in headers:
            if name.lower() == "proxy-authorization" and hmac.compare_digest(value.strip(), expected):
                return True
        return False

    def _is_self(self, host: str, port: int) -> bool:
        return port == self.port and host.strip("[]") in ("127.0.0.1", "localhost", "::1", self.host)

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
        last: Optional[BaseException] = None
        for ip in decision.resolved:
            try:
                return await asyncio.wait_for(asyncio.open_connection(ip, port), CONNECT_TIMEOUT_S)
            except (OSError, asyncio.TimeoutError) as exc:
                last = exc
        raise ConnectionError(f"업스트림 접속 실패: {decision.host} ({type(last).__name__ if last else '해석 없음'})")

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
        try:
            up_reader, up_writer = await self._open_upstream(decision, port)
        except ConnectionError:
            await self._reply(writer, 502, "Bad Gateway", "")
            return
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
        try:
            up_reader, up_writer = await self._open_upstream(decision, port)
        except ConnectionError:
            await self._reply(writer, 502, "Bad Gateway", "")
            return
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
        await _pipe_both(reader, writer, up_reader, up_writer)

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
