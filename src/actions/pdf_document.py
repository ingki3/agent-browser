"""WS-43 bounded PDF reading; network always uses the browser's guarded proxy.

Downloads are cancelled/deleted immediately. A bounded memory stream is fetched
with the actual context UA and URL-scoped cookies. Fetched bytes stay in memory;
the browser's temporary artifacts are explicitly removed before returning.
Only a tab's source URL is retained; page-range reads revalidate/refetch it.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit
from weakref import WeakKeyDictionary

from contracts import ActionResult, ActionType, ErrorCode

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Download, Page
    from actions.dispatcher import ActionDispatcher
    from security.egress import EgressGuard
    from security.egress_proxy import EgressProxy
    from security.robots_signal import RobotsSignals

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_PAGES = 50
MAX_CHARS = 20_000
MAX_STREAM_BYTES = 8 * 1024 * 1024
MAX_RSS_BYTES = 256 * 1024 * 1024
PARSE_TIMEOUT_S = 5.0
FETCH_TIMEOUT_S = 15.0
PDF_INSTRUCTIONS = (
    "PDF 는 browser_navigate 로 읽습니다(data.document). 나머지 쪽은 browser_extract의 "
    "selector='pdf:pages=3-5'로 읽고, 끼워 넣은 PDF 주소는 observe_page의 data.documents에서 찾습니다."
)
_RANGE = re.compile(r"pdf:pages=([1-9][0-9]{0,6})(?:-([1-9][0-9]{0,6}))?\Z")


def clean_text(text: str) -> str:
    """Remove control, bidi and invisible formatting; preserve text line breaks."""
    return "".join(c for c in text if c in "\n\t" or unicodedata.category(c) not in {"Cc", "Cf", "Cs"})


def origin(url: str) -> tuple[str, str, int | None]:
    p = urlsplit(url)
    return p.scheme.lower(), (p.hostname or "").lower(), p.port or {"http": 80, "https": 443}.get(p.scheme)


def safe_url(url: str) -> bool:
    try:
        p = urlsplit(url)
        return bool(p.scheme in {"http", "https"} and p.hostname and not p.username and
                    not p.password and clean_text(url) == url and len(url) <= 4096 and p.port != 0)
    except ValueError:
        return False


@dataclass
class FetchResult:
    body: bytes = b""
    url: str = ""
    reason: str = ""


async def fetch_pdf(context: BrowserContext, url: str, guard: EgressGuard | None,
                    proxy: EgressProxy | None, robots: RobotsSignals, *, sniff: bool = False) -> FetchResult:
    """Same pinned egress/UA as Chromium, bounded bytes, same-origin redirects.

    Cookies are queried only for the validated destination on each hop. They
    never appear in results/logs and cross-origin redirects fail closed.
    """
    import httpx
    from security.egress_proxy import PROXY_USER

    if not safe_url(url) or guard is None or proxy is None or not proxy.running:
        return FetchResult(url=url, reason="egress_unavailable")
    initial = origin(url)
    try:
        async with asyncio.timeout(FETCH_TIMEOUT_S):
            ua = await robots._context_user_agent(context)
            if ua is None:
                return FetchResult(url=url, reason="user_agent_unavailable")
            transport = httpx.Proxy(proxy.server_url, auth=(PROXY_USER, proxy.token) if proxy.token else None)
            async with httpx.AsyncClient(proxy=transport, trust_env=False, follow_redirects=False,
                                         headers={"User-Agent": ua, "Accept-Encoding": "identity"},
                                         timeout=FETCH_TIMEOUT_S) as client:
                for hop in range(6):
                    if not safe_url(url) or not (await guard.evaluate_async(url)).allowed:
                        return FetchResult(url=url, reason="egress_blocked")
                    if origin(url) != initial:
                        return FetchResult(url=url, reason="redirect_origin")
                    cookies = await context.cookies([url])
                    cookie = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                    client.cookies.clear()
                    async with client.stream("GET", url, headers={"Cookie": cookie} if cookie else {}) as response:
                        if 300 <= response.status_code < 400:
                            location = response.headers.get("location")
                            if not location or hop == 5:
                                return FetchResult(url=url, reason="redirect_limit")
                            url = urljoin(url, location)
                            continue
                        if not 200 <= response.status_code < 300:
                            return FetchResult(url=url, reason=f"http_{response.status_code}")
                        if response.headers.get("content-encoding", "identity").lower() != "identity":
                            return FetchResult(url=url, reason="content_encoding")
                        length = response.headers.get("content-length")
                        if length is not None:
                            try:
                                if int(length) < 0 or int(length) > MAX_FILE_BYTES:
                                    return FetchResult(url=url, reason="file_size_limit")
                            except ValueError:
                                return FetchResult(url=url, reason="invalid_length")
                        body = bytearray()
                        async for chunk in response.aiter_raw(chunk_size=16 * 1024):
                            if sniff and not body and b"%PDF-" not in chunk[:1024]:
                                return FetchResult(url=url, reason="not_pdf")
                            if len(body) + len(chunk) > MAX_FILE_BYTES:
                                return FetchResult(url=url, reason="file_size_limit")
                            body.extend(chunk)
                        if sniff and not body:
                            return FetchResult(url=url, reason="not_pdf")
                        return FetchResult(bytes(body), url)
    except TimeoutError:
        return FetchResult(url=url, reason="fetch_timeout")
    except Exception:  # noqa: BLE001 - never expose request headers/credentials
        return FetchResult(url=url, reason="fetch_failed")
    return FetchResult(url=url, reason="redirect_limit")


def empty_document(reason: str = "") -> dict:
    return {"kind": "pdf", "pages_total": 0, "pages": [], "chars": 0,
            "truncated": bool(reason), "text_layer": False, **({"reason": reason} if reason else {})}


def _parse_worker(body: bytes, start: int, end: int, chars: int) -> None:
    """Separate process: bounded decompression, page tree and process memory."""
    import resource
    from pypdf import PdfReader, apply_configuration
    from pypdf.errors import LimitReachedError

    # Darwin rejects RLIMIT_DATA changes on some hosts. Library limits and the
    # hard subprocess deadline still apply there.
    try:
        _, hard = resource.getrlimit(resource.RLIMIT_DATA)
        cap = 256 * 1024 * 1024 if hard == resource.RLIM_INFINITY else min(hard, 256 * 1024 * 1024)
        resource.setrlimit(resource.RLIMIT_DATA, (cap, hard))
    except (ValueError, OSError):
        pass
    doc = empty_document()

    def emit() -> None:
        print(json.dumps(doc, ensure_ascii=True), flush=True)

    try:
        with apply_configuration(
            maximum_declared_stream_length=MAX_STREAM_BYTES,
            array_based_stream_maximum_output_length=MAX_STREAM_BYTES,
            zlib_maximum_output_length=MAX_STREAM_BYTES, lzw_maximum_output_length=MAX_STREAM_BYTES,
            run_length_maximum_output_length=MAX_STREAM_BYTES, jbig2_maximum_output_length=MAX_STREAM_BYTES,
            image_maximum_buffer_size=MAX_STREAM_BYTES, page_tree_maximum_entries=10000,
            page_tree_maximum_depth=50, xform_maximum_invocations_per_extraction=100,
            disable_legacy_handling=True,
        ):
            reader = PdfReader(io.BytesIO(body), root_object_recovery_limit=1000)
            if reader.is_encrypted:
                doc.update(reason="encrypted", truncated=True)
                emit()
                return
            total = len(reader.pages)
            doc["pages_total"] = total
            if start > total:
                doc.update(reason="page_range", truncated=True)
                emit()
                return
            stop = min(end, total, start + MAX_PAGES - 1)
            emit()  # page count survives a timeout on the first page
            for number in range(start, stop + 1):
                text = clean_text(reader.pages[number - 1].extract_text() or "")
                room = chars - doc["chars"]
                doc["pages"].append({"page": number, "text": text[:room]})
                doc["chars"] += len(text[:room])
                doc["text_layer"] = doc["text_layer"] or bool(text.strip())
                if len(text) > room:
                    doc.update(reason="chars_limit", truncated=True)
                    emit()
                    return
                emit()  # bounded progress, parent keeps completed pages
            if stop < min(end, total):
                doc.update(reason="pages_limit", truncated=True)
            elif not doc["text_layer"]:
                doc["reason"] = "no_text_layer"
    except (MemoryError, LimitReachedError):
        doc.update(reason="resource_limit", truncated=True)
    except Exception:  # noqa: BLE001 - malformed/encrypted PDF is a partial document
        doc.update(reason="parse_error", truncated=True)
    emit()


async def parse_pdf(body: bytes, start: int = 1, end: int = 10_000_000,
                    timeout: float = PARSE_TIMEOUT_S, chars: int = MAX_CHARS) -> dict:
    """Kill and join the parser at the deadline; preserve completed-page output."""
    doc = empty_document()
    if len(body) > MAX_FILE_BYTES:
        return empty_document("file_size_limit")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "actions.pdf_document", "--worker", str(start), str(end), str(chars),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        limit=256 * 1024,
    )

    async def write() -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(body)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.stdin.close()

    writer = asyncio.create_task(write())

    async def monitor() -> None:
        while proc.returncode is None:
            if await asyncio.to_thread(_rss_bytes, proc.pid) > MAX_RSS_BYTES:
                if proc.returncode is None:
                    proc.kill()
                return
            await asyncio.sleep(0.05)

    memory = asyncio.create_task(monitor())
    try:
        async with asyncio.timeout(timeout):
            assert proc.stdout is not None
            while line := await proc.stdout.readline():
                doc = json.loads(line)
            await proc.wait()
            if proc.returncode != 0:
                doc.update(reason="resource_limit", truncated=True)
    except TimeoutError:
        doc.update(reason="parse_timeout", truncated=True)
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        writer.cancel()
        memory.cancel()
        await asyncio.gather(writer, memory, return_exceptions=True)
    return doc


def _rss_bytes(pid: int) -> int:
    """Parent watchdog covers Darwin where RLIMIT_DATA is not enforceable."""
    try:
        result = subprocess.run(["/bin/ps", "-o", "rss=", "-p", str(pid)],
                                capture_output=True, text=True, timeout=0.5, check=False)
        return int(result.stdout.strip() or "0") * 1024
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0  # process may already have exited; deadline remains enforced


_DOCUMENTS_JS = """() => {
  const urls = new Set();
  for (const el of document.querySelectorAll('embed[src],iframe[src],object[data],a[href]')) {
    const raw = el.getAttribute(el.tagName === 'OBJECT' ? 'data' : el.tagName === 'A' ? 'href' : 'src');
    try {
      const u = new URL(raw, document.baseURI);
      if (!['http:', 'https:'].includes(u.protocol) || u.username || u.password) continue;
      if (el.type !== 'application/pdf' && !u.pathname.toLowerCase().endsWith('.pdf')) continue;
      urls.add(u.href);
      if (urls.size === 5) break;
    } catch (e) { /* invalid URL */ }
  }
  return [...urls];
}"""


class PdfDocuments:
    """MCP-only document extension, keeping frozen action contracts intact."""

    def __init__(self) -> None:
        self.current: WeakKeyDictionary = WeakKeyDictionary()
        self.watched: WeakKeyDictionary = WeakKeyDictionary()

    def _watch(self, page: Page) -> None:
        if page in self.watched:
            return
        self.watched[page] = True

        def moved(frame) -> None:
            if frame == page.main_frame:
                self.current.pop(page, None)

        page.on("framenavigated", moved)

    async def dispatch(self, dispatcher: ActionDispatcher, action: ActionType, params: dict,
                       guard: EgressGuard | None, proxy: EgressProxy | None,
                       robots: RobotsSignals, max_chars: int = MAX_CHARS) -> ActionResult:
        page = getattr(getattr(dispatcher, "ctx", None), "page", None)
        if page is None:
            return await dispatcher.dispatch(action, params)
        if action is ActionType.EXTRACT and str(params.get("selector", "")).startswith("pdf:"):
            match = _RANGE.fullmatch(params["selector"])
            url = self.current.get(page)
            if not match or not url or int(match[1]) > int(match[2] or match[1]):
                return dispatcher._result(success=False, action=action, retry_safe=True,
                    error_code=ErrorCode.ELEMENT_NOT_FOUND, error_message="현재 PDF와 올바른 pdf:pages=3-5 범위가 필요합니다.")
            return await self._read(dispatcher, action, url, guard, proxy, robots,
                                    int(match[1]), int(match[2] or match[1]), max_chars)
        downloads: list[Download] = []
        cleanup: list[asyncio.Task] = []
        primary_pdf = False
        primary_url = ""
        listening = action in {ActionType.NAVIGATE, ActionType.CLICK} and hasattr(page, "on")

        async def discard(download: Download) -> None:
            try:
                await download.cancel()
            finally:
                try:
                    await download.delete()
                except Exception as exc:  # noqa: BLE001 - cancelled artifacts cannot use API deletion
                    if "canceled" not in str(exc):
                        raise
                finally:
                    # Playwright delete cannot reach unlink after cancellation.
                    # Use only the driver's host-created opaque artifact path,
                    # never suggested_filename (a page-controlled string).
                    artifact = getattr(getattr(download, "_impl_obj", None), "_artifact", None)
                    raw_path = getattr(artifact, "absolute_path", None)
                    if isinstance(raw_path, str) and Path(raw_path).is_absolute():
                        Path(raw_path).unlink(missing_ok=True)
                        Path(raw_path + ".crdownload").unlink(missing_ok=True)

        def on_download(download: Download) -> None:
            downloads.append(download)
            cleanup.append(asyncio.create_task(discard(download)))

        def on_response(response) -> None:
            nonlocal primary_pdf, primary_url
            if response.request.is_navigation_request() and response.request.frame == page.main_frame:
                primary_pdf = response.headers.get("content-type", "").split(";", 1)[0].strip().lower() == "application/pdf"
                primary_url = response.url

        if listening:
            page.on("download", on_download)
            page.on("response", on_response)
        try:
            result = await dispatcher.dispatch(action, params)
            if action is ActionType.NAVIGATE and result.error_code is ErrorCode.DOWNLOAD_FAILED and not downloads:
                # goto rejects just before the download event; only this path waits.
                await asyncio.sleep(0.05)
        finally:
            if listening:
                page.remove_listener("download", on_download)
                page.remove_listener("response", on_response)
            if cleanup:
                await asyncio.gather(*cleanup)
        pdf = next((d for d in downloads if d.suggested_filename.lower().endswith(".pdf")), None)
        if action is ActionType.NAVIGATE and (primary_pdf or (
                downloads and result.error_code is ErrorCode.DOWNLOAD_FAILED)):
            url = primary_url or (downloads[-1].url if downloads else params["url"])
            self._watch(page)
            self.current.pop(page, None)
            return await self._read(dispatcher, action, url, guard, proxy, robots, max_chars=max_chars,
                                    sniff=not primary_pdf and pdf is None)
        if action is ActionType.NAVIGATE and result.success:
            self.current.pop(page, None)
        if action is ActionType.CLICK and (primary_pdf or pdf is not None):
            url = primary_url or pdf.url
            if safe_url(url) and origin(url) == origin(page.url):
                result.data["hint"] = f"이 PDF 는 browser_navigate('{url}') 로 읽으세요."
        if action is ActionType.OBSERVE_PAGE and result.success and hasattr(page, "evaluate"):
            urls = await page.evaluate(_DOCUMENTS_JS)
            documents = [{"kind": "pdf", "url": url} for url in urls if isinstance(url, str) and safe_url(url)]
            if documents:
                result.data["documents"] = documents
        return result

    async def _read(self, dispatcher: ActionDispatcher, action: ActionType, url: str,
                    guard: EgressGuard | None, proxy: EgressProxy | None, robots: RobotsSignals,
                    start: int = 1, end: int = 10_000_000, max_chars: int = MAX_CHARS,
                    sniff: bool = False) -> ActionResult:
        fetched = await fetch_pdf(dispatcher.ctx.page.context, url, guard, proxy, robots, sniff=sniff)
        if fetched.reason == "not_pdf" or (sniff and fetched.reason and fetched.reason not in {
                "egress_blocked", "egress_unavailable", "redirect_origin"}):
            return dispatcher._result(success=False, action=action, retry_safe=True,
                error_code=ErrorCode.DOWNLOAD_FAILED,
                error_message=("PDF가 아닌 다운로드가 시작되어 페이지 이동을 완료하지 못했습니다."
                               if fetched.reason == "not_pdf" else
                               f"다운로드의 PDF 여부를 확인하지 못했습니다: {fetched.reason}"))
        doc = empty_document(fetched.reason) if fetched.reason else await parse_pdf(
            fetched.body, start, end, chars=min(MAX_CHARS, max_chars))
        # Public URL has no hidden/control characters or userinfo.
        doc["url"] = fetched.url if safe_url(fetched.url) else ""
        if fetched.reason in {"egress_blocked", "egress_unavailable", "redirect_origin"}:
            return dispatcher._result(success=False, action=action, retry_safe=True,
                error_code=ErrorCode.INVALID_URL, error_message=f"PDF 읽기 차단: {fetched.reason}")
        if doc.get("reason") == "page_range":
            return dispatcher._result(success=False, action=action, retry_safe=True,
                error_code=ErrorCode.ELEMENT_NOT_FOUND, error_message="PDF 쪽 범위를 벗어났습니다.")
        if action is ActionType.NAVIGATE:
            self.current[dispatcher.ctx.page] = url
            dispatcher.ctx.engine.bump_epoch("navigate_pdf")
        data = {"document": doc}
        reason = doc.get("reason")
        if reason:
            data["hint"] = ("글자 없는 PDF(스캔 이미지) — 스크린샷으로 확인하세요." if reason == "no_text_layer"
                            else "암호 걸린 PDF — 텍스트를 읽을 수 없습니다." if reason == "encrypted"
                            else f"PDF 부분 결과: {reason}. 나머지 쪽은 pdf:pages=3-5 로 읽으세요.")
        result = dispatcher._result(success=True, action=action, retry_safe=True, data=data)
        result.current_url = doc["url"]
        return result


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--worker":
        _parse_worker(sys.stdin.buffer.read(MAX_FILE_BYTES + 1), int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]))
