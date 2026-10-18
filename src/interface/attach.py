"""같은 프로필의 두 번째 serve 가 첫 serve 에 붙기 (WS-57 `serve --profile NAME --attach`).

왜: Chromium 영속 프로필(user-data-dir)은 한 프로세스만 쓴다(프로필 잠금 `.agent-browser-serve.lock`). Hermes 가
같은 프로필로 MCP 서버를 둘(대화용·감시용) 띄우면 두 번째는 잠금 오류로 죽었다. `--attach` 이면:

* 잠금을 잡은 쪽(첫 serve)은 브라우저를 띄우고, 프로필 폴더 안 0700 디렉터리에 유닉스 소켓(0600)을 연다.
  붙은 연결마다 별도 MCP 세션을 돌리고 그 연결의 기본 작업 공간(WS-57)을 따로 준다 — 로그인(쿠키)은 공유,
  탭·활성 탭·관찰 epoch 는 따로.
* 잠금을 못 잡은 쪽은 브라우저 없이 stdin/stdout ↔ 소켓 바이트 중계만 한다(MCP 를 해석하지 않는다).
  첫 serve 가 끝나면 중계는 stderr 한 줄 + 종료 코드 3 으로 끝난다(조용히 멈추지 않음).

보안: 같은 OS 사용자만 — 소켓 디렉터리 0700·소켓 0600·심볼릭 링크 거부·소유자 확인, 연결마다 상대 uid 확인
(SO_PEERCRED / LOCAL_PEERCRED, 확인할 수 없으면 거부). 신뢰 경계는 프로필 폴더(0700)·사람 인계 통로와 같다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import stat
import struct
import sys
import threading
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Set

from browser.workspace import WorkspaceNameError, validate_workspace

logger = logging.getLogger(__name__)

#: 프로필 폴더 안 소켓 디렉터리(0700)와 소켓 이름 — 유닉스 소켓 경로 길이 상한(macOS 104바이트) 때문에 짧게.
SOCKET_DIR = ".ab-attach"
SOCKET_NAME = "s"
#: sun_path 상한(NUL 포함 104/108) 안쪽.
MAX_SOCKET_PATH = 103 if sys.platform == "darwin" else 107
HANDSHAKE_MAX = 1024
HANDSHAKE_TIMEOUT_S = 5.0
HANDSHAKE_KEY = "agent_browser_attach"
#: 첫 serve 가 끝나 중계가 끊겼을 때의 종료 코드.
EXIT_PRIMARY_GONE = 3


class AttachError(RuntimeError):
    """붙기 통로를 열거나 붙을 수 없음(경로·권한·심볼릭 링크·상대 확인 실패)."""


def socket_path(profile_dir: Path) -> Path:
    return Path(profile_dir) / SOCKET_DIR / SOCKET_NAME


def _check_private_dir(path: Path) -> None:
    """심볼릭 링크가 아닌, 내 소유의, 남이 못 들어오는(0o077 비트 없음) 디렉터리인지."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise AttachError("붙기 소켓 디렉터리가 디렉터리가 아닙니다(심볼릭 링크 거부).")
    if st.st_uid != os.getuid():
        raise AttachError("붙기 소켓 디렉터리의 소유자가 이 사용자가 아닙니다.")
    if st.st_mode & 0o077:
        raise AttachError("붙기 소켓 디렉터리 권한이 너무 넓습니다(0700 이어야 함).")


def prepare_socket_dir(profile_dir: Path) -> Path:
    """첫 serve 쪽: 소켓 디렉터리를 0700 으로 만들고(있으면 확인) 경로를 돌려준다."""
    profile_dir = Path(profile_dir)
    _check_private_dir(profile_dir)
    d = profile_dir / SOCKET_DIR
    try:
        os.mkdir(d, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(d)
    if not stat.S_ISLNK(st.st_mode) and stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid():
        os.chmod(d, 0o700)  # 예전 권한이 넓었다면 좁힌다(내 디렉터리만)
    _check_private_dir(d)
    return d


def _check_socket_file(path: Path) -> None:
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISSOCK(st.st_mode):
        raise AttachError("붙기 소켓이 소켓 파일이 아닙니다(심볼릭 링크 거부).")
    if st.st_uid != os.getuid():
        raise AttachError("붙기 소켓의 소유자가 이 사용자가 아닙니다.")


def peer_uid(sock: socket.socket) -> Optional[int]:
    """연결 상대의 uid(커널이 알려 준 값). 확인할 수 없으면 None(부르는 쪽이 거부한다)."""
    try:
        if hasattr(socket, "SO_PEERCRED"):  # Linux: struct ucred {pid, uid, gid}
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            return struct.unpack("3i", raw)[1]
        if sys.platform == "darwin":  # struct xucred {u_int cr_version; uid_t cr_uid; short; gid_t[16]}
            sol_local, local_peercred = 0, 0x001
            raw = sock.getsockopt(sol_local, local_peercred, 76)
            return struct.unpack_from("II", raw)[1]
    except OSError:
        return None
    return None


def _same_user(sock: Any) -> bool:
    uid = peer_uid(sock) if sock is not None else None
    return uid is not None and uid == os.getuid()


def _encode(payload: Dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------------------------- 첫 serve


ServeConnection = Callable[[asyncio.StreamReader, asyncio.StreamWriter, str], Awaitable[None]]


class AttachListener:
    """첫 serve 의 붙기 통로. start() 뒤 연결마다 serve_connection(reader, writer, 작업 공간)을 돌린다."""

    def __init__(self, profile_dir: Path, serve_connection: ServeConnection, *, server_id: str = "") -> None:
        self.profile_dir = Path(profile_dir)
        self.path = socket_path(self.profile_dir)
        self._serve_connection = serve_connection
        self.server_id = server_id
        self._server: Any = None
        self._writers: Set[asyncio.StreamWriter] = set()
        self._tasks: Set[asyncio.Task] = set()
        self._seq = 0

    async def start(self) -> Path:
        if len(os.fsencode(str(self.path))) > MAX_SOCKET_PATH:
            raise AttachError("프로필 폴더 경로가 길어 붙기 소켓을 만들 수 없습니다.")
        prepare_socket_dir(self.profile_dir)
        try:
            # 프로필 잠금을 쥔 쪽만 여기 온다 — 남은 소켓은 죽은 serve 의 것이다(소켓 파일만 지운다).
            _check_socket_file(self.path)
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        old_umask = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(self._on_connection, path=str(self.path))
        finally:
            os.umask(old_umask)
        os.chmod(self.path, 0o600)
        _check_socket_file(self.path)
        return self.path

    async def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self._writers.add(writer)
        try:
            if not _same_user(writer.get_extra_info("socket")):
                logger.warning("붙기 연결 거부: 다른 사용자(또는 상대 확인 실패)")
                return
            try:
                line = await asyncio.wait_for(reader.readline(), HANDSHAKE_TIMEOUT_S)
            except (asyncio.TimeoutError, ValueError):
                return
            workspace = self._parse_handshake(line)
            if workspace is None:
                writer.write(_encode({"ok": False, "error": "잘못된 붙기 요청(작업 공간 이름 규칙 위반 등)"}))
                await writer.drain()
                return
            writer.write(_encode({"ok": True, "workspace": workspace, "server_id": self.server_id}))
            await writer.drain()
            await self._serve_connection(reader, writer, workspace)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:  # noqa: BLE001 - 한 연결의 실패로 첫 serve 를 죽이지 않는다
            logger.warning("붙은 연결 처리 중 오류", exc_info=True)
        finally:
            self._writers.discard(writer)
            if task is not None:
                self._tasks.discard(task)
            writer.close()

    def _parse_handshake(self, line: bytes) -> Optional[str]:
        if not line or len(line) > HANDSHAKE_MAX:
            return None
        try:
            data = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(data, dict) or data.get(HANDSHAKE_KEY) != 1:
            return None
        name = data.get("workspace")
        if name is None:
            self._seq += 1
            return f"attach-{self._seq}"
        try:
            return validate_workspace(name)
        except WorkspaceNameError:
            return None

    async def close(self) -> None:
        """통로를 닫고 붙은 연결을 모두 끊는다(중계 쪽은 EOF 를 보고 명확한 오류로 끝난다)."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for writer in list(self._writers):
            writer.close()
        pending = [t for t in self._tasks if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.wait(pending, timeout=2.0)
        if server is not None:
            try:
                await asyncio.wait_for(server.wait_closed(), 2.0)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
        try:
            _check_socket_file(self.path)
            os.unlink(self.path)
        except (FileNotFoundError, AttachError):
            pass


#: 붙은 연결의 MCP 스트림을 읽는 덩어리 크기(줄 길이 상한이 아니다).
READ_CHUNK = 65536


class StreamLines:
    """asyncio StreamReader → mcp stdio_server 의 stdin(줄 단위 비동기 반복).

    WS-57 R1(R8): StreamReader.readline() 은 연결의 기본 한도(64KiB)를 넘는 줄에서 ValueError 를 내 붙은 세션을
    끝냈다(큰 정상 요청 → 붙은 serve 종료·'첫 serve 가 끝나' 오진). stdio(mcp stdio_server 의 텍스트 줄 반복)처럼
    줄 길이 상한 없이 덩어리로 읽어 줄을 나눈다. 악수 줄의 1KiB 상한(HANDSHAKE_MAX)은 그대로다.
    """

    def __init__(self, reader: asyncio.StreamReader) -> None:
        self._reader = reader
        self._buf = bytearray()
        self._scanned = 0

    def __aiter__(self) -> "StreamLines":
        return self

    async def __anext__(self) -> str:
        while True:
            i = self._buf.find(b"\n", self._scanned)
            if i >= 0:
                line = bytes(self._buf[: i + 1])
                del self._buf[: i + 1]
                self._scanned = 0
                return line.decode("utf-8", errors="replace")
            self._scanned = len(self._buf)
            chunk = await self._reader.read(READ_CHUNK)
            if not chunk:
                if self._buf:  # 마지막 줄(줄바꿈 없음) — readline 과 같은 끝 처리
                    line = bytes(self._buf)
                    self._buf.clear()
                    self._scanned = 0
                    return line.decode("utf-8", errors="replace")
                raise StopAsyncIteration
            self._buf.extend(chunk)


class StreamText:
    """asyncio StreamWriter → mcp stdio_server 의 stdout(write/flush)."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self._writer = writer

    async def write(self, text: str) -> None:
        self._writer.write(text.encode("utf-8"))
        await self._writer.drain()

    async def flush(self) -> None:
        await self._writer.drain()


# ---------------------------------------------------------------------------------------------- 붙는 serve


def connect(profile_dir: Path, workspace: Optional[str], *, timeout: float = HANDSHAKE_TIMEOUT_S) -> tuple:
    """첫 serve 의 소켓에 붙고 악수한다. 반환: (소켓, 응답 dict). 붙을 수 없으면 AttachError."""
    path = socket_path(profile_dir)
    try:
        _check_private_dir(path.parent)
        _check_socket_file(path)
    except FileNotFoundError:
        raise AttachError("붙을 serve 가 없습니다(붙기 소켓 없음 — 첫 serve 가 --attach 없이 떴을 수 있음).") from None
    except OSError as exc:
        raise AttachError(f"붙기 소켓을 확인할 수 없습니다: {type(exc).__name__}") from None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        try:
            sock.connect(str(path))
        except OSError as exc:
            raise AttachError(f"첫 serve 에 붙지 못했습니다: {type(exc).__name__}") from None
        if not _same_user(sock):
            raise AttachError("붙기 상대가 같은 사용자인지 확인하지 못했습니다(거부).")
        sock.sendall(_encode({HANDSHAKE_KEY: 1, "workspace": workspace}))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(HANDSHAKE_MAX)
            if not chunk:
                raise AttachError("첫 serve 가 붙기 요청에 답하지 않고 끊었습니다.")
            buf += chunk
            if len(buf) > HANDSHAKE_MAX:
                raise AttachError("첫 serve 의 응답이 너무 깁니다.")
        try:
            reply = json.loads(buf.decode("utf-8"))
        except ValueError:
            raise AttachError("첫 serve 의 응답을 읽지 못했습니다.") from None
        if not isinstance(reply, dict) or not reply.get("ok"):
            raise AttachError(str((reply or {}).get("error") or "첫 serve 가 붙기를 거절했습니다."))
        sock.settimeout(None)
        return sock, reply
    except BaseException:
        sock.close()
        raise


def relay(sock: socket.socket, *, stdin_fd: int = 0, stdout_fd: int = 1, stderr: Any = None) -> int:
    """stdin → 소켓, 소켓 → stdout 바이트 중계. 클라이언트(stdin)가 먼저 끝나면 0, 첫 serve 가 먼저 끝나면
    stderr 한 줄 + EXIT_PRIMARY_GONE."""
    stderr = stderr if stderr is not None else sys.stderr
    stdin_done = threading.Event()

    def pump_in() -> None:
        try:
            while True:
                data = os.read(stdin_fd, 65536)
                if not data:
                    break
                sock.sendall(data)
        except OSError:
            pass
        finally:
            stdin_done.set()
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    threading.Thread(target=pump_in, name="attach-stdin", daemon=True).start()
    try:
        while True:
            try:
                data = sock.recv(65536)
            except OSError:
                data = b""
            if not data:
                break
            view = memoryview(data)
            while view:
                n = os.write(stdout_fd, view)
                view = view[n:]
    except OSError:
        return 0  # stdout 이 닫혔다(클라이언트 종료)
    finally:
        sock.close()
    if stdin_done.is_set():
        return 0
    print("agent-browser serve --attach: 오류: 붙어 있던 첫 serve 가 끝나 연결이 끊겼습니다 — "
          "MCP 클라이언트가 이 서버를 다시 시작하면 새로 붙거나 브라우저를 직접 띄웁니다.", file=stderr, flush=True)
    return EXIT_PRIMARY_GONE


def run_relay(profile_dir: Path, workspace: Optional[str], *, stderr: Any = None) -> int:
    """붙는 serve 의 본체: 붙기 → 중계. 붙을 수 없으면 AttachError(부르는 쪽이 원래 잠금 오류를 알린다)."""
    stderr = stderr if stderr is not None else sys.stderr
    sock, reply = connect(profile_dir, workspace)
    print(f"agent-browser serve: 첫 serve({reply.get('server_id') or '?'})에 붙었습니다 — 작업 공간 "
          f"{reply.get('workspace')!r}(탭·관찰 따로, 로그인 공유). 이 serve 의 브라우저 옵션은 쓰지 않습니다.",
          file=stderr, flush=True)
    return relay(sock, stderr=stderr)
