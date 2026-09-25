"""페이지의 보이는 글자를 읽는다 — read_text 의사 액션의 실제 동작.

관찰(`observe_page`)은 누를 수 있는 요소만 담는다. 메일 목록·표·검색 결과
본문처럼 **글자로만 된 내용**은 거기 없어서, 모델은 extract에 CSS 선택자를
지어내다 실패했다(2026-09-23 네이버 메일: 3회 모두 E_ELEMENT_NOT_FOUND).

`innerText`를 쓴다 — 화면에 보이는 순서·줄바꿈을 따르고, display:none 등
숨은 글자는 빠진다. 입력칸 값(비밀번호 포함)은 innerText에 들어가지 않는다.
"""

from __future__ import annotations

import re
from typing import Any

#: 한 번에 넣는 글자 상한. 로컬 모델은 입력 900토큰 읽는 데 약 8초가 걸린다
#: (2026-09-23 실측) — 너무 길면 스텝이 느려진다.
DEFAULT_MAX_CHARS = 3000

_BLANKS = re.compile(r"\n\s*\n+")


async def read_visible_text(page: Any, *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """본문(<main> 우선)의 보이는 글자. 넘치면 잘라 '(이하 N자 생략)'을 붙인다."""
    raw = await page.evaluate(
        """() => {
          const root = document.querySelector('main, [role=main]') || document.body;
          return root ? root.innerText : '';
        }"""
    )
    text = _BLANKS.sub("\n", (raw or "").replace("\r", "")).strip()
    lines = [ln.strip() for ln in text.split("\n")]
    text = "\n".join(ln for ln in lines if ln)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n(이하 {len(text) - max_chars}자 생략)"
