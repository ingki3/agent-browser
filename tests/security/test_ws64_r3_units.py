"""WS-64 R3 B2 — 팝업 lineage 는 생성 시점에 opener 탭에서 살아 있던 문서만(전체 방문 이력 상속 제거, 브라우저 없음).

생성 순간은 opener 세션의 Page.windowOpen 으로 안다(같은 세션의 이동 이벤트와 순서가 보장됨 — 그때의 살아 있는 문서를
적는다, `TabLineage.note_open`). 그 기록이 없으면(판별 불가 경합) 지금 살아 있는 문서 + 각 프레임의 **직전 1개 문서**까지만.
실제 Chromium 경합·과잉 거부 재현은 tests/interface/test_ws64_r3_server.py.
"""

from __future__ import annotations

from security import manual_origin as mo

from test_ws64_r2_units import _Context, _Frame, _known_root

A, B, C = "https://srail.kr", "https://plain.example", "https://third.example"


def _popup_doc(ctx: _Context, opener: object, url: str = "blob:null/p") -> set:
    popup = ctx.open(url, opener=opener)
    rec = mo.lineage_record(popup)
    assert rec is not None and rec.known
    return rec.of_document(popup.main_frame)


async def test_completed_navigation_then_new_popup_has_no_old_visit():
    """A → B 이동을 마친 뒤 B 가 연 팝업(생성 기록 있음): A 는 살아 있지 않았다 — 물려받지 않는다."""
    ctx = _Context()
    mo.watch_lineage(ctx)
    root = _known_root(ctx, A + "/main")
    root.navigate(root.main_frame, B + "/")
    ad = _Frame("about:blank", parent=root.main_frame)
    root.frames.append(ad)
    root.navigate(ad, "about:srcdoc")  # B 의 sandbox iframe(불투명) — 이 문서가 팝업을 연다
    rec = mo.lineage_record(root)
    assert rec is not None
    rec.note_open("blob:null/p")  # opener 세션 Page.windowOpen
    doc = _popup_doc(ctx, root)
    assert A not in doc and doc == {B}


async def test_open_recorded_before_opener_left_keeps_creator():
    """경합: A 가 팝업을 연(생성 기록) 직후 B 로 떠나고 그 뒤에 팝업 이벤트가 와도 생성 순간의 A 를 물려받는다."""
    ctx = _Context()
    mo.watch_lineage(ctx)
    root = _known_root(ctx, A + "/main")
    rec = mo.lineage_record(root)
    assert rec is not None
    rec.note_open("blob:null/p")
    root.navigate(root.main_frame, B + "/")
    assert A in _popup_doc(ctx, root)


async def test_undecidable_race_inherits_only_previous_document():
    """생성 기록이 없으면(판별 불가) 지금 문서 + 직전 1개 문서까지 — 그 이전 방문(A)은 물려받지 않는다."""
    ctx = _Context()
    mo.watch_lineage(ctx)
    root = _known_root(ctx, A + "/main")
    root.navigate(root.main_frame, B + "/")
    root.navigate(root.main_frame, C + "/")
    doc = _popup_doc(ctx, root)
    assert B in doc and C in doc and A not in doc


async def test_unmatched_open_record_is_conservative():
    """생성 기록의 주소가 팝업과 다르면(판별 불가) 보수적으로: 직전 문서 + 남은 생성 기록 전부."""
    ctx = _Context()
    mo.watch_lineage(ctx)
    root = _known_root(ctx, A + "/main")
    rec = mo.lineage_record(root)
    assert rec is not None
    rec.note_open("https://elsewhere.example/")
    root.navigate(root.main_frame, B + "/")
    root.navigate(root.main_frame, C + "/")
    doc = _popup_doc(ctx, root)
    assert {A, B, C} <= doc


async def test_detached_creator_frame_is_previous_document():
    """판별 불가 경합에서 연 프레임이 곧바로 떨어졌어도(직전 1개 문서) 그 문서의 lineage 를 물려받는다."""
    ctx = _Context()
    mo.watch_lineage(ctx)
    root = _known_root(ctx, B + "/")
    frame = _Frame("about:blank", parent=root.main_frame)
    root.frames.append(frame)
    root.navigate(frame, A + "/embed")
    root.emit("framedetached", frame)
    root.frames.remove(frame)
    assert A in _popup_doc(ctx, root)


async def test_popup_that_navigated_to_unlisted_drops_inherited():
    """A 가 만든 팝업이 일반 B 로 이동한 뒤 B 가 만든 불투명 프레임 문서는 A 를 물려받지 않는다(살아 있지 않음).
    이동 전 팝업 문서 자체·그 팝업의 팝업은 A 를 유지(opener 이동·닫힘 보존)."""
    ctx = _Context()
    mo.watch_lineage(ctx)
    root = _known_root(ctx, A + "/main")
    rec = mo.lineage_record(root)
    assert rec is not None
    rec.note_open("blob:null/p")
    popup = ctx.open("blob:null/p", opener=root)
    prec = mo.lineage_record(popup)
    assert prec is not None and A in prec.of_document(popup.main_frame)
    popup.navigate(popup.main_frame, "blob:null/q")  # 불투명 → 불투명: 직전 문서(A lineage) 유지
    assert A in prec.of_document(popup.main_frame)
    popup.navigate(popup.main_frame, B + "/")
    ad = _Frame("about:blank", parent=popup.main_frame)
    popup.frames.append(ad)
    popup.navigate(ad, "about:srcdoc")
    assert prec.of_document(ad) == {B}
