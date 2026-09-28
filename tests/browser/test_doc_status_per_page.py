"""탭별 메인 문서 상태 추적 (WS-26b, 검증 NB-6). 가짜 응답으로 규칙만 고정."""

from __future__ import annotations

from browser.doc_status import PageDocumentStatus, track_main_document_status


class _Req:
    def __init__(self, nav=True, rtype="document"):
        self._nav = nav
        self.resource_type = rtype

    def is_navigation_request(self):
        return self._nav


class _Frame:
    def __init__(self, page, parent=None):
        self.page = page
        self.parent_frame = parent


class _Page:
    def __init__(self, url):
        self.url = url

    def is_closed(self):
        return False


class _Resp:
    def __init__(self, url, status, frame=None, nav=True, frame_error=False):
        self.url = url
        self.status = status
        self.request = _Req(nav)
        self._frame = frame
        self._frame_error = frame_error

    @property
    def frame(self):
        if self._frame_error:
            raise RuntimeError("Frame for this navigation request is not available")
        return self._frame


def test_status_is_recorded_per_page():
    a, b = _Page("http://x/a"), _Page("http://x/b")
    t = PageDocumentStatus()
    t.on_response(_Resp("http://x/a", 200, _Frame(a)))
    t.on_response(_Resp("http://x/b", 403, _Frame(b)))
    assert t.status_for(a) == 200
    assert t.status_for(b) == 403
    assert t.status_for(_Page("http://x/c")) is None


def test_iframe_and_subresource_responses_are_ignored():
    a = _Page("http://x/a")
    t = PageDocumentStatus()
    t.on_response(_Resp("http://x/a", 200, _Frame(a)))
    t.on_response(_Resp("http://x/f", 403, _Frame(a, parent=_Frame(a))))
    t.on_response(_Resp("http://x/img", 404, _Frame(a), nav=False))
    assert t.status_for(a) == 200


def test_popup_first_response_resolved_later():
    """새 탭 첫 문서 응답은 frame 이 아직 없다 — 나중에 조회할 때 페이지를 찾는다."""
    pop = _Page("http://x/blocked")
    t = PageDocumentStatus()
    resp = _Resp("http://x/blocked", 403, frame_error=True)
    t.on_response(resp)
    resp._frame_error = False
    resp._frame = _Frame(pop)
    assert t.status_for(pop) == 403


def test_pending_does_not_override_newer_status():
    pop = _Page("http://x/ok")
    t = PageDocumentStatus()
    first = _Resp("http://x/blocked", 403, frame_error=True)
    t.on_response(first)
    t.on_response(_Resp("http://x/ok", 200, _Frame(pop)))
    first._frame_error = False
    first._frame = _Frame(pop)
    assert t.status_for(pop) == 200


def test_judge_status_requires_same_url():
    a = _Page("http://x/blocked")
    t = PageDocumentStatus()
    t.on_response(_Resp("http://x/blocked", 403, _Frame(a)))
    assert t.judge_status_for(a) == 403
    a.url = "http://x/blocked#top"  # 조각만 다르면 같은 문서
    assert t.judge_status_for(a) == 403
    a.url = "http://x/app/home"  # pushState 로 URL 이 바뀜
    assert t.judge_status_for(a) is None
    assert t.status_for(a) == 403


def test_attach_listens_on_context():
    handlers = {}

    class _Ctx:
        def on(self, ev, fn):
            handlers[ev] = fn

    t = PageDocumentStatus()
    t.attach(_Ctx())
    a = _Page("http://x/a")
    handlers["response"](_Resp("http://x/a", 418, _Frame(a)))
    assert t.status_for(a) == 418


def test_session_wide_tracker_unchanged():
    """run 경로의 세션 전역 기록은 그대로 '마지막 메인 문서 응답'."""
    handlers = {}

    class _Ctx:
        def on(self, ev, fn):
            handlers[ev] = fn

    record = {"last_http_status": None}
    track_main_document_status(_Ctx(), record)
    a, b = _Page("http://x/a"), _Page("http://x/b")
    handlers["response"](_Resp("http://x/a", 200, _Frame(a)))
    handlers["response"](_Resp("http://x/b", 403, frame_error=True))
    assert record["last_http_status"] == 403
