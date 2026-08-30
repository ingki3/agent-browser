"""WS-7 LLM 어댑터 패키지 (OpenRouter).

* `LLMConfig` / `load_config` — `.env` 및 환경변수 기반 설정
* `OpenRouterClient`          — OpenAI 호환 Chat Completions
* `BudgetGuard`               — 태스크당 비용·토큰·스텝 상한 강제
* `probe_connection`          — 자격증명 사전 확인

사용:

    from llm import load_config, OpenRouterClient

    async with OpenRouterClient(load_config()) as client:
        res = await client.complete([{"role": "user", "content": "안녕"}])
        print(res.content, res.cost_usd)
"""

from llm.budget import (
    MODEL_PRICING,
    BudgetExceeded,
    BudgetGuard,
    estimate_cost,
)
from llm.client import (
    LLMError,
    LLMNotConfigured,
    LLMResponse,
    OpenRouterClient,
    probe_connection,
)
from llm.config import (
    DEFAULT_MODEL,
    OPENROUTER_BASE_URL,
    LLMConfig,
    load_config,
    is_placeholder,
    load_env_file,
    parse_env_text,
)

__all__ = [
    # 설정
    "LLMConfig",
    "load_config",
    "load_env_file",
    "parse_env_text",
    "is_placeholder",
    "DEFAULT_MODEL",
    "OPENROUTER_BASE_URL",
    # 클라이언트
    "OpenRouterClient",
    "LLMResponse",
    "LLMError",
    "LLMNotConfigured",
    "probe_connection",
    # 예산
    "BudgetGuard",
    "BudgetExceeded",
    "estimate_cost",
    "MODEL_PRICING",
]
