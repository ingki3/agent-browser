"""OpenRouter Decisions API 클라이언트 — Jev(typesafe) 같은 '보기 고르기' 모델용.

Jev는 글을 생성하지 않는다. `state`(맥락)와 좁은 `questions`를 받아 정해 둔
보기 중 하나(choice) 또는 예/아니오 확률(noul)을 돌려준다. 한 번에 ~0.2초.

    POST {base}/api/alpha/decisions
    {"model": "~typesafe/jev-latest", "state": {...},
     "questions": {"action": {"type": "choice", "instructions": "...", "criteria": {...}}}}

실측(2026-09-24, agent_eval 31개): 한 번에 여러 질문을 섞어 물으면 답이 서로
어긋났다(비밀번호를 아이디 칸에). 질문은 **하나씩 차례로** 묻는다 — 호출자가
`ask()`를 여러 번 부른다.

보안: `OpenRouterClient`와 같은 원칙 — 예외에 요청 헤더(키)를 싣지 않는다.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from llm.budget import BudgetGuard
from llm.client import LLMError
from llm.config import LLMConfig

DEFAULT_DECISIONS_MODEL = "~typesafe/jev-latest"
#: 실측 p50 0.23초, 최대 0.8초. 넉넉히 두되 막히면 곧바로 폴백으로 넘어가게 짧게.
DEFAULT_DECISIONS_TIMEOUT_S = 15.0


def decisions_url(base_url: str) -> str:
    """Chat API base(`.../api/v1`)에서 Decisions 엔드포인트를 만든다."""
    parts = urlsplit(base_url.rstrip("/"))
    path = parts.path
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    return f"{parts.scheme}://{parts.netloc}{path}/alpha/decisions"


@dataclass
class DecisionAnswer:
    """질문 하나의 답."""

    choice: Optional[str] = None
    confidence: float = 0.0
    probabilities: Dict[str, float] = field(default_factory=dict)
    #: noul(예/아니오) 질문의 '예' 확률
    noul: Optional[float] = None
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    input_tokens: int = 0


class DecisionsClient:
    """Decisions API 호출기. `async with`로 쓰거나 `start()`/`close()`."""

    def __init__(
        self,
        config: LLMConfig,
        budget: Optional[BudgetGuard] = None,
        *,
        model: str = DEFAULT_DECISIONS_MODEL,
        timeout_s: float = DEFAULT_DECISIONS_TIMEOUT_S,
    ) -> None:
        self.config = config
        self.budget = budget or BudgetGuard()
        self.model = model
        self.timeout_s = timeout_s
        self.url = decisions_url(config.base_url)
        self._client: Any = None

    async def start(self) -> "DecisionsClient":
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_s)
        return self

    async def __aenter__(self) -> "DecisionsClient":
        return await self.start()

    async def __aexit__(self, *exc) -> None:  # noqa: ANN002
        await self.close()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        if self.config.app_url:
            headers["HTTP-Referer"] = self.config.app_url
        if self.config.app_title:
            headers["X-Title"] = self.config.app_title
        return headers

    async def ask(self, state: Any, name: str, question: Dict[str, Any]) -> DecisionAnswer:
        """질문 하나를 묻는다. 호출 **전에** 예산을 확인한다."""
        import httpx

        if self._client is None:
            raise LLMError("Decisions 클라이언트가 시작되지 않았습니다.")
        self.budget.check()
        payload = {"model": self.model, "state": state, "questions": {name: question}}
        started = time.perf_counter()
        try:
            # httpx timeout은 바이트 간격 상한이라 전체 상한을 따로 건다(client.py와 같은 이유).
            response = await asyncio.wait_for(
                self._client.post(self.url, json=payload, headers=self._headers()),
                timeout=self.timeout_s,
            )
        except (httpx.TimeoutException, asyncio.TimeoutError):
            raise LLMError(f"Decisions 타임아웃 ({self.timeout_s}s)") from None
        except httpx.HTTPError as exc:
            raise LLMError(f"Decisions 네트워크 오류: {type(exc).__name__}") from None
        latency = (time.perf_counter() - started) * 1000
        if response.status_code != 200:
            raise LLMError(f"Decisions HTTP {response.status_code}: {response.text[:300]}")
        try:
            data = response.json()
            raw = data["answers"][name]
        except (ValueError, KeyError, TypeError) as exc:
            raise LLMError(f"Decisions 응답 형식이 예상과 다릅니다: {exc}") from None

        usage = data.get("usage") or {}
        in_tok = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        actual = usage.get("cost")
        cost = self.budget.record(
            prompt_tokens=in_tok,
            completion_tokens=0,  # 출력 토큰은 무과금
            model=self.model,
            actual_usd=float(actual) if actual is not None else 0.0,
        )
        probs = {str(k): float(v) for k, v in (raw.get("probabilities") or {}).items()}
        conf = raw.get("confidence")
        if conf is None and probs:
            conf = max(probs.values())
        noul = raw.get("noul")
        return DecisionAnswer(
            choice=raw.get("choice"),
            confidence=float(conf or 0.0),
            probabilities=probs,
            noul=float(noul) if noul is not None else None,
            latency_ms=latency,
            cost_usd=cost,
            input_tokens=in_tok,
        )
