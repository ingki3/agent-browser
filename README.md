# Agent Browser

> **AI 에이전트 네이티브 헤드리스 브라우징 런타임 & MCP 서버**
>
> Python 3.11+ / Playwright CDP / Model Context Protocol

에이전트가 웹을 다루려면 두 가지가 필요합니다. 페이지를 **토큰 예산 안에서 이해하는 것**, 그리고 **눌렀다고 착각하지 않는 것**입니다. 이 프로젝트는 그 두 가지를 목표로 만들었습니다.

원시 HTML 대신 접근성 트리를 정제해 상위 20개 요소만 넘기고(관찰 토큰 중앙값 13개), 모든 액션은 실행 후 DOM 상태로 성공을 검증합니다.

---

## 무엇을 검증했는가

수치는 전부 실제 실행 결과입니다. CI의 릴리스 게이트에서 독립 재현됩니다.

| 항목 | 실측 | 임계 |
| :--- | ---: | ---: |
| Element Recall@20 | 1.0 | ≥ 0.95 |
| 관찰 토큰 p50 / p95 | 13 / 202 | ≤ 2,500 / 6,500 |
| 스텝 지연 p50 / p95 | 72ms / 92ms | ≤ 800 / 2,200ms |
| 액션 성공률 | 1.0 | ≥ 0.92 |
| 자가 치유율 | 1.0 | ≥ 0.80 |
| 프롬프트 주입 차단율 | 1.0 (오탐 0.0) | ≥ 0.90 |
| 테스트 플레이키율 | 0.0 | ≤ 0.02 |

**실환경 태스크 완수율** — 공개 사이트 12곳, 난이도 6단계, 31개 태스크를 실제 LLM으로 수행합니다. 최근 통합 측정은 31/31입니다. 다만 이 중 3개는 에이전트가 성공을 인지하지 못한 채 결과만 맞은 경우로, 자기 보고 기준으로는 28/31(90.3%)입니다. 성공 판정은 에이전트의 자기 보고가 아니라 최종 페이지 상태를 JavaScript로 독립 검증합니다.

---

## 설치

### 요구 사항

- Python 3.11 이상
- [uv](https://docs.astral.sh/uv/) (권장) 또는 pip
- 디스크 약 500MB (Chromium 포함)

### 방법 1 — 개발/테스트용 (권장)

```bash
git clone https://github.com/ingki3/agent-browser.git
cd agent-browser

uv sync --extra dev              # 의존성 + 개발 도구
uv run playwright install chromium
```

브라우저 바이너리는 별도 다운로드가 필요합니다. Linux에서 시스템 라이브러리까지 함께 설치하려면 `--with-deps`를 붙이십시오(sudo 권한 필요).

### 방법 2 — 패키지로 설치

```bash
pip install git+https://github.com/ingki3/agent-browser.git
playwright install chromium
```

### 설치 확인

```bash
uv run agent-browser tools      # 방법 1
agent-browser tools             # 방법 2
```

19종 툴 목록이 출력되면 정상입니다.

> **주의**: npm에도 `agent-browser`라는 이름의 다른 패키지가 있습니다. `which agent-browser`가 `node_modules` 경로를 가리킨다면 그건 이 프로젝트가 아닙니다. 방법 1의 `uv run` 접두사를 쓰면 혼동이 없습니다.

---

## 빠른 시작

### 1. 동작 확인 (LLM 불필요, 비용 0)

브라우저 런타임만 검증합니다. 내장 Mock 사이트 22종을 사용하므로 외부 네트워크가 필요 없습니다.

```bash
# 인지 엔진 — 요소 추출 정확도
uv run python -m harness.recall --pages 100 --top-n 20

# 액션 — 19종 전수 실행
uv run python -m harness.actions_test --tasks 40

# 자가 치유 — 셀렉터가 깨졌을 때 복구
uv run python -m harness.self_healing --tasks 60
```

각 명령은 JSON 한 줄을 출력하고 `passed: true`면 종료 코드 0입니다.

```json
{"metric": "element_recall_at_20", "value": 1.0, "threshold": 0.95, "passed": true, ...}
```

### 2. 전체 테스트

```bash
uv run pytest tests -q
```

505개가 통과해야 합니다. Chromium이 필요한 테스트가 포함되어 있습니다.

### 3. LLM 연동 (선택)

에이전트 루프를 돌리려면 LLM 키가 필요합니다. 현재 OpenRouter를 지원합니다.

```bash
cp .env.example .env
```

`.env`를 열어 두 줄을 채웁니다.

```
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MODEL=openai/gpt-4o-mini
```

키는 https://openrouter.ai/keys 에서 발급합니다.

```bash
uv run agent-browser llm-check --no-call   # 설정만 확인 (비용 0)
uv run agent-browser llm-check             # 실제 호출 (약 $0.000001)
```

키가 플레이스홀더 상태면 그렇다고 알려줍니다. 태스크를 다 돌린 뒤 401을 받는 일이 없도록 만들었습니다.

### 4. 실환경 태스크 실행

```bash
# 난이도별로 골라 실행
uv run python -m harness.agent_eval --task easy
uv run python -m harness.agent_eval --task dynamic

# 전체 25태스크 (약 30분, 약 $0.02)
uv run python -m harness.agent_eval --report artifacts/agent_eval.json
```

공개 사이트(위키백과, 해커뉴스, MDN 등)에 실제로 접속합니다.

---

## MCP 클라이언트 연동

19종 툴을 stdio로 노출합니다. Claude Desktop 설정 예시입니다.

```json
{
  "mcpServers": {
    "agent-browser": {
      "command": "uv",
      "args": ["run", "--directory", "/절대/경로/agent-browser",
               "agent-browser", "serve"]
    }
  }
}
```

`--directory`에는 클론한 디렉토리의 **절대 경로**를 넣으십시오. 설정 파일 위치는 다음과 같습니다.

| OS | 경로 |
| :--- | :--- |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |

MCP SDK는 1.x와 2.x를 모두 지원합니다. 두 메이저는 서버 등록 방식과 스키마 필드명이 달라, 런타임에 실제 API를 조회해 맞춥니다.

연동이 되는지 미리 확인하려면 다음을 실행하십시오. 실제 MCP 클라이언트 세션으로 `initialize → tools/list → tools/call` 왕복을 검증합니다.

```bash
uv run python -m harness.mcp_binding
```

노출되는 툴은 `browser_` 접두사를 가집니다.

```
browser_observe_page      페이지 관찰 (Top-20 요소 + 에포크)
browser_click             요소 클릭
browser_type_text         텍스트 입력
browser_navigate          URL 이동
...                       (전체 목록은 agent-browser tools)
```

### 관찰 → 액션 흐름

`observe_page`가 반환하는 `element_id`와 `epoch`을 액션에 그대로 넘깁니다.

```
observe_page  ->  @e3 (button "로그인"), epoch=0
click         ->  element_id="@e3", epoch=0
```

페이지가 바뀌면 `epoch`이 올라가고 이전 `element_id`는 무효가 됩니다. 오래된 ID로 액션을 보내면 `E_TOCTOU_MISMATCH`로 거부됩니다 — 다른 요소를 잘못 누르는 것보다 낫다는 판단입니다.

---

## 구조

```
src/
├── contracts/     [동결] 19종 액션 · ErrorCode 27종 · KPI 임계값
├── browser/       세션 암호화 · 만료 감지 · 컨텍스트 격리
├── perception/    AxTree 정제 · Shadow DOM 관통 · Top-20 스코어러
├── actions/       19종 디스패처 · 4단계 자가 치유 · 사후조건 검증
├── security/      Egress 차단 · PII 마스킹 · HITL · 프롬프트 격리
├── interface/     MCP 서버 · TUI · 관측성 트레이스
├── llm/           OpenRouter 어댑터 · 예산 가드
├── agent/         자율 루프 · 목표 키워드 추출
└── harness/       판정형 하네스 10종 + 실환경 평가 2종
```

`contracts/`는 Gate 0 승인 이후 동결되어 CI가 매 PR마다 변경 여부를 검증합니다.

---

## 안전장치

**예산 강제 차단** — 태스크당 $0.75 / 100,000토큰 / 30스텝 중 하나라도 넘으면 호출 자체를 막습니다. 경고가 아니라 차단이며, 응답을 받고 확인하면 이미 과금된 뒤라 호출 **전에** 검사합니다.

**Egress 차단** — allowlist 밖 도메인으로의 요청을 막습니다. 클라우드 메타데이터 엔드포인트(`169.254.169.254`)는 상시 차단합니다.

**프롬프트 주입 격리** — 웹에서 온 텍스트는 신뢰 경계 밖에 둡니다. 차단율 1.0, 오탐률 0.0으로 측정됩니다.

**세션 암호화** — 쿠키와 로컬스토리지를 AES-256-GCM + Argon2id로 암호화해 `0600` 권한으로 저장합니다.

---

## 문제 해결

**`playwright install`이 실패합니다**

Linux에서는 시스템 라이브러리가 필요합니다.

```bash
uv run playwright install --with-deps chromium   # sudo 권한 필요
```

**테스트가 Chromium을 못 찾습니다**

브라우저 바이너리는 의존성과 별도입니다. `uv sync` 후 `playwright install chromium`을 실행했는지 확인하십시오.

**`agent-browser` 명령이 다른 프로그램을 실행합니다**

npm에 동명 패키지가 있습니다. `which agent-browser`로 확인하고, `uv run agent-browser ...` 형태를 사용하십시오.

**LLM 응답이 비어 있습니다**

reasoning 계열 모델은 본문보다 사고 토큰을 먼저 소비합니다. `max_tokens`가 소진되면 오류로 처리되며 모델명과 현재 상한이 메시지에 표시됩니다. 기본값은 32,768입니다.

**실환경 태스크가 실패합니다**

공개 사이트는 구조가 바뀔 수 있습니다. `harness.agent_eval`은 판정 게이트가 아니라 관측용입니다. CI 필수 체크에는 포함되지 않습니다.

---

## 알려진 한계

**모델 판단의 편차** — 실패 사례는 런타임 결함이 아니라 LLM이 실행마다 다른 선택을 하는 경우입니다. `max_tokens` 조정으로 해결되지 않으며, 모델 비교가 다음 과제입니다.

**Tier-2 SoM 미구현** — Canvas 렌더링이나 안티스크래핑 난독화로 텍스트 셀렉터가 통하지 않는 경우의 시각 폴백은 v1.1 대상입니다. `take_screenshot(annotate_som=True)`는 현재 `E_FEATURE_NOT_IMPLEMENTED`를 반환합니다.

**소셜 로그인 자동화는 지원하지 않습니다** — 구글·페이스북 등의 로그인 페이지를 에이전트가 직접 조작하는 것은 **의도적으로 지원 대상이 아닙니다.** 제공자들이 헤드리스 브라우저 지문, WebDriver 플래그, 비정상 로그인 타이밍을 능동적으로 탐지해 차단하기 때문입니다. 실측에서도 구글 검색이 `/sorry/index` CAPTCHA로, 쿠팡이 403으로 막혔습니다.

우회를 시도하면 계정 잠금이나 정지로 이어질 수 있습니다. 대신 아래 방식을 쓰십시오.

```
1. 사람이 브라우저에서 직접 로그인
2. storage_state를 SessionStore로 암호화 저장 (AES-256-GCM)
3. 이후 세션을 복원해 재사용
```

이는 Playwright의 `storageState`, Browserbase의 Contexts, Steel의 세션 영속화와 같은 접근입니다. 2FA나 매직 링크가 걸린 경우 사람의 개입이 필요하며, 이는 회피 대상이 아니라 정상적인 설계입니다.

**자격증명 주입 경로가 없습니다** — 아이디/비밀번호를 안전하게 주입하는 볼트 연동(1Password, HashiCorp Vault)은 v1.1 대상입니다. 현재 `type_text`에 평문을 넣으면 값이 LLM 프롬프트로 전송됩니다.

트레이스 기록에는 비밀번호 필드(`input[type=password]`) 입력값 마스킹이 적용되지만, **이것을 보안 경계로 여기지 마십시오.** 마스킹은 방어의 한 겹일 뿐입니다. 스크린샷, 네트워크 페이로드, DOM 스냅샷 등 다른 경로는 덮지 못합니다. 로그인이 필요한 작업에는 위의 세션 저장 방식을 권장합니다.

---

## 개발 참여

`src/AGENTS.md`에 게이트 절차와 하네스 설계 규칙이 정리되어 있습니다. 특히 다음 두 가지를 지켜주십시오.

**게이트가 실패하면 임계값을 조정하지 마십시오.** 원인을 고치는 것이 원칙입니다.

**하네스를 만들면 사보타주로 검증하십시오.** 의도적으로 결함을 주입했을 때 실제로 잡히는지 확인해야 합니다. 이 프로젝트에서 게이트 자체의 미탐 4건이 이 방법으로 발견됐습니다.

```bash
# 커밋 전 표준 검증
python3 check_docs.py
python3 scripts/check_gate_commands.py
python3 scripts/check_contracts_freeze.py
python3 scripts/check_harness_coverage.py
uv run pytest tests -q
```

브랜치는 `feature → dev → main` 순서로 PR을 통해서만 진행합니다. 양쪽 브랜치 모두 직접 push가 차단되어 있습니다.

---

## 라이선스

Apache-2.0
