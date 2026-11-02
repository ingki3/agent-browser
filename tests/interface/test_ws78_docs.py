"""WS-78 문서 — T1(#142)·T5(#208) security.md, T2·T3·T4 mcp-integration.md, T4 human-handoff.md."""

from __future__ import annotations

from pathlib import Path

DOCS = Path(__file__).resolve().parents[2] / "docs"


def _read(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def test_security_doc_press_key_and_whitespace_limits():
    text = _read("security.md")
    assert "키로 친 비밀번호 (WS-78, #142)" in text
    for phrase in ("키 **전** 포커스 노드", "키 **뒤** 그 노드", "data.sensitive_input", "OOPIF", "닫힌 shadow",
                   "4~7자 숫자 접두", "입력 초반 1~3자"):
        assert phrase in text, phrase
    assert "공백이 든 등록 값 (WS-78, #208)" in text
    for phrase in ("관찰 요소 이름", "`extract` 본문", "반복 묶음 표제"):
        assert phrase in text, phrase


def test_mcp_integration_doc():
    text = _read("mcp-integration.md")
    assert "data.page_not_found" in text and "없는 주소 — 차단 아님, 다른 경로를 찾으세요" in text
    assert "SPA" in text and "WS-78 #139" in text
    assert "묶음 응답 전체**의 최종 wire 길이" in text
    assert "끝난 인계(`released`·`request_expired`)도 `null`" in text


def test_human_handoff_doc():
    text = _read("human-handoff.md")
    assert "사람용 상태 파일·CLI 의 사유 가림 (WS-78, #215)" in text
    assert "approvals/*.json" in text
