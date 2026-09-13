"""SoM 오버레이 렌더러 (PRD §3.1 Tier-2, Stage 4 Task 3).

후보 bbox 좌상단에 태그 라벨을 DOM으로 주입해 스크린샷을 찍고,
`finally`에서 주입 노드를 전부 제거해 페이지를 원상복구한다.

설계 결정:
* 뷰포트를 계약값 1280×720으로 **강제**한다 — `SOM_IMAGE_TOKENS_PER_CAPTURE`
  (1,600토큰/장) 산정 기준이므로 캡처 크기가 달라지면 예산이 틀어진다.
* 에포크는 올리지 않는다. 오버레이는 상호작용 요소가 아니며, 캡처 후
  제거되므로 기존 `@eN` 핸들은 그대로 유효하다(관찰 무효화 방지).
* DOM 주입 방식의 한계(MutationObserver 사이트 부작용)는 계획서 리스크 3
  참조 — 문제 발견 시 CDP captureScreenshot + 합성으로 전환한다.
"""

from __future__ import annotations

from typing import Any, Sequence

from contracts import thresholds
from vision.candidates import SomCandidate

#: 주입 노드를 식별하는 속성. 제거·검증 모두 이 속성으로 한다.
SOM_ATTR = "data-som-tag"

#: 라벨 주입 스크립트. 인자: [{tag, x, y}, ...]
#: 라벨은 bbox 좌상단에 붙이고, 뷰포트 밖으로 밀리지 않게 0 이상으로 클램프한다.
_INJECT_SCRIPT = """
(items) => {
  const MAX_Z = 2147483647;
  for (const it of items) {
    const d = document.createElement('div');
    d.setAttribute('%(attr)s', it.tag);
    d.textContent = it.tag;
    d.style.cssText = [
      'position:fixed',
      'left:' + Math.max(0, it.x) + 'px',
      'top:' + Math.max(0, it.y) + 'px',
      'background:#ffeb3b',
      'color:#000',
      'font:bold 12px/14px monospace',
      'padding:1px 3px',
      'border:1px solid #000',
      'border-radius:2px',
      'z-index:' + MAX_Z,
      'pointer-events:none',
      'user-select:none',
      'white-space:nowrap',
    ].join(';');
    document.documentElement.appendChild(d);
  }
  return items.length;
}
""" % {"attr": SOM_ATTR}

_REMOVE_SCRIPT = """
() => {
  const nodes = document.querySelectorAll('[%(attr)s]');
  nodes.forEach((n) => n.remove());
  return nodes.length;
}
""" % {"attr": SOM_ATTR}


async def _ensure_viewport(page: Any) -> None:
    target = {
        "width": thresholds.VIEWPORT_WIDTH,
        "height": thresholds.VIEWPORT_HEIGHT,
    }
    if getattr(page, "viewport_size", None) != target:
        await page.set_viewport_size(target)


async def render_som(page: Any, candidates: Sequence[SomCandidate]) -> bytes:
    """태그 라벨을 얹은 뷰포트 PNG를 반환한다. 페이지 DOM은 호출 전과 같다.

    후보가 비어 있으면 라벨 없는 일반 스크린샷을 반환한다 — 순수 Canvas
    페이지에서 좌표 모드로 넘어갈 때 VLM에 보낼 원본이 필요하다.
    """
    await _ensure_viewport(page)

    items = [
        {"tag": c.tag, "x": c.bbox.x, "y": c.bbox.y}
        for c in candidates
    ]
    injected = False
    try:
        if items:
            await page.evaluate(_INJECT_SCRIPT, items)
            injected = True
        # full_page=False — 뷰포트 크기가 곧 토큰 산정 기준이다.
        return await page.screenshot(type="png", full_page=False)
    finally:
        if injected:
            try:
                await page.evaluate(_REMOVE_SCRIPT)
            except Exception:  # noqa: BLE001
                # 페이지가 이미 닫혔거나 내비게이션 중이면 제거할 DOM도 없다.
                pass
