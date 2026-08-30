"""OpenRouter 클라이언트 (WS-7).

OpenAI 호환 `/chat/completions` 엔드포인트를 사용한다. OpenRouter는
여러 벤더를 단일 API로 노출하므로 모델 교체가 설정 한 줄로 끝난다.

설계 원칙:
* **키를 로그·예외에 남기지 않는다.** httpx 예외는 요청 헤더를 포함할 수
  있어 그대로 전파하면 키가 트레이스백에 실린다. 자체 예외로 감싼다.
* **예산 가드를 통과하지 않으면 호출하지 않는다.** 응답을 받고 나서
  초과를 확인하면 이미 과금된 뒤다.
* **usage는 응답값을 신뢰한다.** 자체 추정은 폴백일 뿐이다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from llm.budget import BudgetGuard
from llm.config import LLMConfig, load_config

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """LLM 호출 실패. 원본 예외의 헤더/키는 포함하지 않는다."""


class LLMNotConfigured(LLMError):
    """API 키가 없어 호출할 수 없다."""


@dataclass
class LLMResponse:
    """단일 호출 결과."""

    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    finish_reason: str = ""
    raw: Optional[Dict[str, Any]] = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def parse_json(self) -> Any:
        """응답 본문을 JSON으로 파싱한다.

        모델이 ```json 펜스를 붙이는 경우가 흔해 이를 벗겨낸다.
        """
        text = (self.content or "").strip()
        if text.startswith("```"):
            lines = text.split("\n")
            # 첫 줄(```json)과 마지막 펜스를 제거
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"JSON 파싱 실패: {exc}. 본문 앞부분: {text[:200]!r}") from None


class OpenRouterClient:
    """OpenRouter Chat Completions 클라이언트."""

    def __init__(
        self,
        config: Optional[LLMConfig] = None,
        budget: Optional[BudgetGuard] = None,
    ) -> None:
        self.config = config or load_config()
        self.budget = budget or BudgetGuard()
        self._client: Any = None

    # -- 수명주기 -----------------------------------------------------------

    async def __aenter__(self) -> "OpenRouterClient":
        import httpx

        if not self.config.configured:
            raise LLMNotConfigured(
                "OPENROUTER_API_KEY가 설정되지 않았습니다. "
                ".env 파일에 추가하거나 환경변수로 지정하십시오."
            )
        self._client = httpx.AsyncClient(
            base_url=self.config.base_url,
            timeout=self.config.timeout_s,
            headers=self._headers(),
        )
        return self

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
        # OpenRouter 순위 페이지 표기용 (선택 사항)
        if self.config.app_url:
            headers["HTTP-Referer"] = self.config.app_url
        if self.config.app_title:
            headers["X-Title"] = self.config.app_title
        return headers

    # -- 호출 ---------------------------------------------------------------

    async def complete(
        self,
        messages: List[Dict[str, str]],
        *,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> LLMResponse:
        """Chat Completion을 호출한다.

        호출 **전에** 예산을 확인한다. 응답 후 확인하면 이미 과금된 뒤다.
        """
        if self._client is None:
            raise LLMError("클라이언트가 시작되지 않았습니다. async with를 사용하십시오.")

        self.budget.check()  # 상한 초과 시 BudgetExceeded

        target_model = model or self.config.model
        payload: Dict[str, Any] = {
            "model": target_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            # OpenRouter가 실제 과금액을 함께 반환하도록 요청한다.
            "usage": {"include": True},
        }
        if response_format is not None:
            payload["response_format"] = response_format

        data = await self._post_with_retry("/chat/completions", payload)

        try:
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
            finish = choice.get("finish_reason", "")
        except (KeyError, IndexError) as exc:
            raise LLMError(f"응답 형식이 예상과 다릅니다: {exc}") from None

        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
        # OpenRouter는 usage.cost로 실제 과금액(USD)을 제공한다.
        actual = usage.get("cost")
        actual_usd = float(actual) if actual is not None else None

        cost = self.budget.record(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=target_model,
            actual_usd=actual_usd,
        )

        return LLMResponse(
            content=content,
            model=data.get("model", target_model),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            finish_reason=finish,
            raw=data,
        )

    async def _post_with_retry(
        self, path: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """POST + 제한적 재시도.

        httpx 예외를 그대로 올리면 요청 헤더(Authorization)가 트레이스백에
        실릴 수 있으므로, 상태 코드와 본문 일부만 담아 자체 예외로 바꾼다.
        """
        import asyncio

        import httpx

        last_error = ""
        for attempt in range(self.config.max_retries + 1):
            try:
                response = await self._client.post(path, json=payload)
            except httpx.TimeoutException:
                last_error = f"타임아웃 ({self.config.timeout_s}s)"
            except httpx.HTTPError as exc:
                # 예외 객체를 전파하지 않고 타입명만 남긴다.
                last_error = f"네트워크 오류: {type(exc).__name__}"
            else:
                if response.status_code == 200:
                    return response.json()

                body = response.text[:300]
                if response.status_code in (401, 403):
                    raise LLMError(
                        f"인증 실패 ({response.status_code}). "
                        f"OPENROUTER_API_KEY를 확인하십시오. 응답: {body}"
                    )
                if response.status_code == 402:
                    raise LLMError(f"크레딧 부족 (402). 응답: {body}")
                if response.status_code not in (429, 500, 502, 503, 504):
                    raise LLMError(f"HTTP {response.status_code}: {body}")
                last_error = f"HTTP {response.status_code}: {body}"

            if attempt < self.config.max_retries:
                delay = 2**attempt
                logger.warning(
                    "LLM 호출 재시도 %d/%d (%s)",
                    attempt + 1,
                    self.config.max_retries,
                    last_error,
                )
                await asyncio.sleep(delay)

        raise LLMError(f"재시도 {self.config.max_retries}회 후 실패: {last_error}")


async def probe_connection(config: Optional[LLMConfig] = None) -> Dict[str, Any]:
    """자격증명과 모델 가용성을 최소 비용으로 확인한다.

    실환경 검증 전에 키가 유효한지 먼저 알아야 한다. 태스크를 다 돌린 뒤
    401을 받으면 시간과 비용을 낭비한다.
    """
    cfg = config or load_config()
    if not cfg.configured:
        return {"ok": False, "reason": "OPENROUTER_API_KEY 미설정", "model": cfg.model}

    try:
        async with OpenRouterClient(cfg) as client:
            response = await client.complete(
                [{"role": "user", "content": "Reply with exactly: OK"}],
                max_tokens=8,
            )
        return {
            "ok": True,
            "model": response.model,
            "reply": response.content.strip()[:40],
            "prompt_tokens": response.prompt_tokens,
            "completion_tokens": response.completion_tokens,
            "cost_usd": round(response.cost_usd, 6),
        }
    except LLMError as exc:
        return {"ok": False, "reason": str(exc)[:200], "model": cfg.model}
