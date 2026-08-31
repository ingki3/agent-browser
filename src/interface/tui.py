"""Textual TUI 대시보드 및 A2UI 렌더러 (PRD §6.1).

대시보드 구성:
* Header — 활성 탭, URL, 에포크, 실행 모드
* CoT & Action Trace Pane — 사고 흐름 및 액션 로그
* AxTree / Top-20 View — 프루닝된 후보 요소 목록
* Interactive A2UI Modal Pane — ConfirmDialog 네이티브 모달

**A2UI 보안 원칙 (PRD §6.1)**:
임의의 스크립트 실행을 원천 차단한다. `ConfirmDialog` 정형 JSON 스키마만
파싱해 Textual 네이티브 위젯으로 렌더링하며, 스키마 밖의 필드는
계약(`extra="forbid"`)에서 이미 거부된다. 여기서는 **렌더링 시점에도**
문자열을 그대로 마크업으로 해석하지 않도록 이스케이프한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from contracts import ConfirmDialog, DangerLevel, ExecutionMode, ObserveResult

#: danger_level -> 표시 색상 (Rich 마크업)
DANGER_STYLES: Dict[DangerLevel, str] = {
    DangerLevel.LOW: "green",
    DangerLevel.MEDIUM: "yellow",
    DangerLevel.HIGH: "bold red",
}


def escape_markup(text: str) -> str:
    """Rich 마크업 해석을 차단한다.

    웹 페이지에서 읽은 문자열이 `[bold]` 같은 마크업을 포함하면 TUI
    렌더링이 왜곡되거나, 사용자에게 조작된 내용을 보여줄 수 있다.
    대괄호를 이스케이프해 리터럴로만 표시한다.
    """
    return (text or "").replace("[", r"\[")


@dataclass
class DashboardState:
    """헤더에 표시할 세션 상태."""

    tab_id: str = ""
    url: str = ""
    epoch: int = 0
    mode: ExecutionMode = ExecutionMode.INTERACTIVE
    step: int = 0

    def header_line(self) -> str:
        mode_label = "대화형" if self.mode is ExecutionMode.INTERACTIVE else "무인"
        return (
            f"[{mode_label}] tab={self.tab_id or '-'} "
            f"epoch={self.epoch} step={self.step} "
            f"url={escape_markup(self.url[:60]) or '-'}"
        )


def render_confirm_dialog(dialog: ConfirmDialog) -> str:
    """ConfirmDialog를 텍스트 모달로 렌더링한다.

    스크립트를 실행하지 않으며, 스키마에 정의된 5개 필드만 사용한다.
    """
    style = DANGER_STYLES.get(dialog.danger_level, "yellow")
    lines = [
        f"[{style}]{'=' * 56}[/{style}]",
        f"[{style}]{escape_markup(dialog.title)}[/{style}]",
        "",
        escape_markup(dialog.message),
        "",
        f"  [reverse] {escape_markup(dialog.confirm_label)} [/reverse]"
        f"   {escape_markup(dialog.cancel_label)}",
        f"[{style}]{'=' * 56}[/{style}]",
    ]
    return "\n".join(lines)


def render_element_list(observation: ObserveResult, limit: int = 20) -> List[str]:
    """AxTree Top-N 뷰를 렌더링한다."""
    rows: List[str] = []
    for element in observation.elements[:limit]:
        marker = "" if element.interactable else " [dim](비활성)[/dim]"
        shadow = " [magenta]⧉[/magenta]" if element.is_shadow else ""
        rows.append(
            f"[cyan]{element.element_id}[/cyan] "
            f"[yellow]{element.role}[/yellow] "
            f"{escape_markup(element.name[:48])}{shadow}{marker}"
        )
    return rows


def render_trace_line(
    step: int, action: str, success: bool, detail: str = ""
) -> str:
    """액션 트레이스 한 줄을 렌더링한다."""
    icon = "[green]OK[/green]" if success else "[red]FAIL[/red]"
    body = f" {escape_markup(detail)}" if detail else ""
    return f"[dim]{step:3}[/dim] {icon} [bold]{action}[/bold]{body}"


# ---------------------------------------------------------------------------
# Textual 앱 (선택적 — textual 미설치 환경에서도 위 함수들은 동작한다)
# ---------------------------------------------------------------------------


def build_app(state: Optional[DashboardState] = None) -> Any:
    """Textual 대시보드 앱을 생성한다.

    `textual`을 지연 import해 헤드리스/CI 환경에서 본 모듈의 순수 함수만
    사용할 수 있게 한다.
    """
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical
    from textual.widgets import Footer, Header, RichLog, Static

    dashboard_state = state or DashboardState()

    class AgentBrowserApp(App):
        """에이전트 브라우저 TUI 대시보드."""

        CSS = """
        #status { height: 1; background: $panel; }
        #elements { width: 45%; border: solid $primary; }
        #trace { width: 55%; border: solid $secondary; }
        #modal { height: auto; border: heavy $error; display: none; }
        #modal.visible { display: block; }
        """
        BINDINGS = [("q", "quit", "종료"), ("r", "refresh", "새로고침")]

        def compose(self) -> ComposeResult:  # noqa: D102
            yield Header()
            yield Static(dashboard_state.header_line(), id="status")
            with Horizontal():
                yield RichLog(id="elements", markup=True, highlight=False)
                yield RichLog(id="trace", markup=True, highlight=False)
            yield Static("", id="modal")
            yield Footer()

        # -- 갱신 API (Worker에서 호출) ---------------------------------

        def update_status(self, new_state: DashboardState) -> None:
            self.query_one("#status", Static).update(new_state.header_line())

        def show_observation(self, observation: ObserveResult) -> None:
            log = self.query_one("#elements", RichLog)
            log.clear()
            for row in render_element_list(observation):
                log.write(row)

        def append_trace(
            self, step: int, action: str, success: bool, detail: str = ""
        ) -> None:
            self.query_one("#trace", RichLog).write(
                render_trace_line(step, action, success, detail)
            )

        def show_dialog(self, dialog: ConfirmDialog) -> None:
            modal = self.query_one("#modal", Static)
            modal.update(render_confirm_dialog(dialog))
            modal.add_class("visible")

        def hide_dialog(self) -> None:
            modal = self.query_one("#modal", Static)
            modal.update("")
            modal.remove_class("visible")

    return AgentBrowserApp()
