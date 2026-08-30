"""관측성: 스텝 JSONL 트레이스 및 리플레이 (PRD §6.2).

기록 항목:
* Correlation ID, 스텝 번호, `snapshot_epoch`
* 토큰 수(tiktoken), 소요 시간(ms)
* 관찰 요약, 액션 입출력

**민감정보 마스킹은 기록 시점에 적용한다.** 나중에 지우는 방식은
이미 디스크에 평문이 남은 뒤이므로 의미가 없다. WS-4의 `mask_mapping`을
통과시킨 뒤에만 파일에 쓴다.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from contracts import ActionResult, ActionType
from security import mask_mapping


@dataclass
class StepRecord:
    """단일 스텝의 구조화 로그 (PRD §6.2)."""

    correlation_id: str
    step: int
    action: str
    snapshot_epoch: int
    success: bool
    latency_ms: float
    observation_tokens: int = 0
    observation_summary: str = ""
    action_input: Dict[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    healed: bool = False
    timestamp: float = field(default_factory=time.time)

    def to_masked_dict(self) -> Dict[str, Any]:
        """민감정보를 마스킹한 딕셔너리를 반환한다."""
        payload: Dict[str, Any] = {
            "correlation_id": self.correlation_id,
            "step": self.step,
            "action": self.action,
            "snapshot_epoch": self.snapshot_epoch,
            "success": self.success,
            "latency_ms": round(self.latency_ms, 2),
            "observation_tokens": self.observation_tokens,
            "observation_summary": self.observation_summary,
            "action_input": self.action_input,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "healed": self.healed,
            "timestamp": self.timestamp,
        }
        return mask_mapping(payload)


class StepTracer:
    """스텝 단위 JSONL 트레이스 기록기."""

    def __init__(
        self,
        output_path: Optional[Path] = None,
        *,
        correlation_id: Optional[str] = None,
    ) -> None:
        self.correlation_id = correlation_id or uuid.uuid4().hex[:12]
        self.output_path = Path(output_path) if output_path else None
        self._records: List[StepRecord] = []
        self._step = 0

        if self.output_path:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        result: ActionResult,
        *,
        action_input: Optional[Dict[str, Any]] = None,
        observation_summary: str = "",
        observation_tokens: int = 0,
    ) -> StepRecord:
        """액션 결과를 기록한다. 반환값은 마스킹 전 원본이다."""
        self._step += 1
        record = StepRecord(
            correlation_id=self.correlation_id,
            step=self._step,
            action=result.action.value,
            snapshot_epoch=result.snapshot_epoch,
            success=result.success,
            latency_ms=float(result.data.get("latency_ms", 0.0)),
            observation_tokens=observation_tokens,
            observation_summary=observation_summary,
            action_input=dict(action_input or {}),
            error_code=result.error_code.value if result.error_code else None,
            error_message=result.error_message,
            healed=result.healed,
        )
        self._records.append(record)

        if self.output_path:
            # 마스킹을 통과한 내용만 디스크에 쓴다.
            line = json.dumps(record.to_masked_dict(), ensure_ascii=False)
            with self.output_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

        return record

    # -- 집계 ---------------------------------------------------------------

    @property
    def records(self) -> List[StepRecord]:
        return list(self._records)

    @property
    def step_count(self) -> int:
        return self._step

    @property
    def success_rate(self) -> float:
        if not self._records:
            return 0.0
        return sum(1 for r in self._records if r.success) / len(self._records)

    def total_tokens(self) -> int:
        return sum(r.observation_tokens for r in self._records)

    def latencies(self) -> List[float]:
        return [r.latency_ms for r in self._records]


class TraceSession:
    """Playwright Trace 연계 (PRD §6.2).

    실패 시 `trace.zip`으로 저장해 Trace Viewer에서 리플레이할 수 있게 한다.
    """

    def __init__(self, context: Any, output_dir: Optional[Path] = None) -> None:
        self.context = context
        self.output_dir = Path(output_dir) if output_dir else Path("artifacts/traces")
        self._active = False

    async def start(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        await self.context.tracing.start(screenshots=True, snapshots=True)
        self._active = True

    async def save(self, name: str = "trace") -> Optional[Path]:
        """트레이스를 저장하고 경로를 반환한다."""
        if not self._active:
            return None
        path = self.output_dir / f"{name}.zip"
        await self.context.tracing.stop(path=str(path))
        self._active = False
        return path

    async def discard(self) -> None:
        """성공한 세션의 트레이스는 저장하지 않고 버린다 (디스크 절약)."""
        if self._active:
            await self.context.tracing.stop()
            self._active = False

    @property
    def active(self) -> bool:
        return self._active
