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

> **2026-09-23 정정** — 이전의 액션 성공률·태스크 완수율 1.0은 **효과가 없는 클릭도 성공으로 센 값**이었습니다. 사후조건 검증이 '포커스가 버튼으로 옮겨감'을 효과로 인정했고, 테스트용 Mock 버튼 대부분이 눌러도 아무 반응이 없었습니다(클릭 성공 판정의 55~60%). 검증을 고치면 1.0 → 0.77 / 0.40으로 떨어졌고, Mock 버튼이 실제 사이트처럼 반응하도록 고친 뒤 위 수치를 다시 측정했습니다. 이제 무반응 버튼 하나를 섞으면 그 케이스가 실패로 잡힙니다(사보타주로 확인).

**실환경 태스크 완수율** — 공개 사이트 12곳, 난이도 6단계, 31개 태스크를 실제 LLM으로 수행합니다. 최근 통합 측정은 31/31입니다. 다만 이 중 3개는 에이전트가 성공을 인지하지 못한 채 결과만 맞은 경우로, 자기 보고 기준으로는 28/31(90.3%)입니다. 성공 판정은 에이전트의 자기 보고가 아니라 최종 페이지 상태를 JavaScript로 독립 검증합니다. **이 수치는 위 정정 이전 검증기로 측정한 것이며 아직 재측정하지 않았습니다.** 최종 판정은 페이지 상태로 하므로 결과 자체는 영향이 작을 것으로 보지만, 에이전트가 각 스텝에서 받은 성공/실패 신호는 달라집니다.

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

브라우저 런타임만 검증합니다. 내장 Mock 사이트 25종(Tier-2 검증용 3종 포함)을 사용하므로 외부 네트워크가 필요 없습니다.

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

918개가 통과해야 합니다(3개 건너뜀). Chromium이 필요한 테스트가 포함되어 있습니다.

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

**빠른 판단 모드 (선택).** `.env`에 `AGENT_DECIDER=jev`를 넣으면 Jev(보기 고르기 전용 모델)가 "다음 행동 → 대상 → 입력값"을 좁은 질문으로 하나씩 빠르게(한 번 약 0.2초) 판단합니다. 확신이 낮거나, 직전 행동이 실패했거나, 입력값을 만들어야 할 때만 `AGENT_FALLBACK_MODEL`(기본 `qwen/qwen3.8-27b`)이 넘겨받습니다. OpenRouter에서만 켜지고, 로컬 모델로 도는 로그인 작업에서는 쓰지 않습니다(페이지 내용이 클라우드로 가지 않게). 실측(agent_eval 31개 × 3회)은 평균 28.7/31 성공, 31개에 약 5~6분, $0.036입니다. 약점은 "끝났다"를 잘못 판단하는 경우(3회 중 거짓 완료 0~2건)입니다.

**목표 1개 실행 (`run`).** 주소와 목표 문장 하나로 에이전트를 돌리고 결과를 JSON으로 받습니다.

```bash
uv run agent-browser run --url https://example.com --goal "첫 문단을 요약해 줘" --out result.json
uv run agent-browser run --url URL --goal GOAL --human               # 실제 창·ko-KR (위장 없음)
uv run agent-browser run --url URL --goal GOAL --user-chrome         # 설치된 Chrome, 전용 프로필(~/.agent-browser/chrome-profile)
uv run agent-browser run --url URL --goal GOAL --handoff --handoff-wait 300
uv run agent-browser run --url URL --goal GOAL --no-answer           # 답 생성 끔(final_answer "")
```

- `final_answer`: 실행이 **완료(completed)** 로 끝난 뒤 목표·읽은 글(read_text)·끝난 화면 글로 한 번 만든 사람이 읽는 답입니다. 포기·차단·시간 초과·실행 오류면 만들지 않고 `""`입니다. 실패하면 실행 결과는 그대로 두고 `answer_error`에 이유를 남깁니다(`answer_model`, `answer_elapsed_s`, `answer_input_chars`도 함께 기록, 입력 글은 합계 12000자에서 자름). 페이지 글은 신뢰되지 않는 데이터로 감싸 넘기며 글 속 지시는 따르지 않게 합니다.
- `finish_reason`: 에이전트가 끝낸 이유 한 줄입니다(예: `jev finish 0.75`). 예전에 `final_answer`에 들어가던 값입니다.
- `answer_input`: 답 생성 모델에 실제로 보낸 페이지 글(경계 안 본문, 무력화·잘림 적용 후) 그대로입니다 — 답의 근거 감사용이며 `--out` 파일에만 남고 콘솔 요약에는 나오지 않습니다(답을 만들지 않았으면 `""`). 대조할 때는 NFKC 정규화를 권장합니다(네이버는 `李`를 호환 한자 U+F9E1로 씁니다).
- 답 생성은 루프와 같은 모델·엔드포인트(`OPENROUTER_BASE_URL`, `OPENROUTER_MODEL`)로 갑니다 — 로컬이면 로컬, Jev·폴백 모델은 쓰지 않습니다. 비용은 `usd`/`tokens`에 포함되지 않고 `answer_usd`/`answer_tokens`로 따로 보입니다(같은 예산 상한 안).
- `http_status`는 첫 이동 응답, `last_http_status`는 실행 중 마지막 메인 프레임 문서 응답의 상태입니다(브라우저 context 전체 — 새 탭 포함, 여러 탭이 동시에 움직이면 마지막으로 온 응답). 첫 화면은 200인데 검색 페이지에서 403으로 막힌 경우를 구분합니다. `steps`의 실패 줄에는 오류 메시지 앞 80자를 붙입니다(`type_text`는 입력값이 섞일 수 있어 붙이지 않음).

`--out` 파일은 페이지 본문이 들어가므로 권한 0600으로 씁니다. `--handoff`를 켜면 차단·캡차 화면에서 멈추고, 사람이 창에서 해결한 뒤 터미널에서 Enter를 누르거나 `touch ~/.agent-browser/handoff.done`(`--handoff-file`로 변경)하면 같은 목표로 이어 갑니다(이때도 화면을 다시 확인해, 여전히 막혀 있으면 멈춥니다). 정상 화면을 차단으로 잘못 본 경우에는 터미널에 `f`(또는 `force`)를 입력하고 Enter를 누르거나 `echo force > ~/.agent-browser/handoff.done` 하면 강제로 계속하며, 그 실행 동안 같은 판정은 다시 넘기지 않습니다(결과의 `handoffs[].forced`에 기록). `--user-chrome`은 우리가 띄운 Chrome만 닫습니다(`--keep-open`이면 둡니다). `--human`과 `--user-chrome`은 함께 쓸 수 없고, `--chrome-profile`에 평소 Chrome 프로필 경로를 주면 브라우저를 띄우기 전에 한 줄 오류로 거부합니다(exit 2).

> 막히면 사람에게 넘긴다 — 캡차를 풀거나 차단을 우회하지 않는다.

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
├── agent/         자율 루프 · 목표 키워드 추출 · Tier-2 에스컬레이션
├── vision/        Tier-2 SoM — 태그 후보 · 오버레이 · VLM 그라운더 · @sN 브리지
└── harness/       판정형 하네스 11종 + 실환경 평가 2종
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

**Tier-2 시각 폴백은 옵트인입니다** — 텍스트 셀렉터가 2회 연속 실패하면(아이콘 버튼, 난독화 라벨, Canvas UI) 스크린샷에 태그를 얹어 비전 모델에게 묻는 폴백이 있습니다. `serve --som-vision`으로 켭니다. 끄면 `take_screenshot(annotate_som=True)`는 이전처럼 `E_FEATURE_NOT_IMPLEMENTED`를 반환합니다(레거시 클라이언트 보호).

```bash
agent-browser serve --som-vision          # Tier-2 활성화
OPENROUTER_VISION_MODEL=...               # 선택. 없으면 OPENROUTER_MODEL 사용
```

- 발동은 무인 모드 태스크당 3회로 제한되며, 캡처 1장당 1,600토큰이 예산에 합산됩니다.
- 순수 Canvas는 DOM 대상이 없으므로 좌표 클릭(`click(x, y, epoch)`)으로 처리합니다. 좌표 모드는 클릭만 지원합니다.
- 이미지 안의 텍스트는 신뢰하지 않는다는 비전 프롬프트 래퍼가 강제됩니다.
- 비전 모델 지연은 기준(p95 ≤ 3.5초)에 맞는 모델을 골라야 합니다. `python -m harness.tier2_som --vlm live`로 실측합니다. **다만 실측한 비전 모델 8종 중 이 기준을 만족한 모델은 없었습니다**(p50은 2.7~3.1초대이나 max가 12~25초로 튐). 현재는 `OPENROUTER_MODEL`(glm-5.3-flash)을 그대로 사용합니다.
- **아이콘만으로 판별해야 하는 화면은 기권할 수 있습니다** — 라벨이 난수이고 SVG 아이콘 그림이 유일한 단서인 경우, 실측상 8회 중 2회 "해당 요소 없음"을 반환했습니다. 오답 클릭이 아니라 클릭하지 않는 쪽(fail-closed)이며, 이때 태스크는 실패로 끝납니다.
- **비전 모델은 좌표계를 일관되게 지키지 않습니다** — 절대 픽셀 대신 0~1000 정규화 좌표를 섞어 반환하는 사례를 gemini·glm 양쪽에서 관측했습니다. 응답에 `coord_space`(`absolute` / `normalized_1000`) 선언을 요구해 변환하며, 선언이 없으면 절대 픽셀로, 모르는 값이면 좌표를 버립니다(추측하지 않음). 단 모델이 정규화 좌표를 내면서 `absolute`로 잘못 선언하는 경우가 있어 이 처리로 전부 걸러지지는 않습니다.

**소셜 로그인 자동화는 지원하지 않습니다** — 구글·페이스북 등의 로그인 페이지를 에이전트가 직접 조작하는 것은 **의도적으로 지원 대상이 아닙니다.** 제공자들이 헤드리스 브라우저 지문, WebDriver 플래그, 비정상 로그인 타이밍을 능동적으로 탐지해 차단하기 때문입니다. 실측에서도 구글 검색이 `/sorry/index` CAPTCHA로, 쿠팡이 403으로 막혔습니다.

우회를 시도하면 계정 잠금이나 정지로 이어질 수 있습니다. 대신 아래 방식을 쓰십시오.

```bash
# 브라우저 창이 열립니다. 직접 로그인하십시오 (2FA·CAPTCHA도 여기서).
agent-browser session login naver --url https://nid.naver.com/nidlogin.login

agent-browser session list                        # 저장된 세션 목록
agent-browser session check naver --url https://mail.naver.com   # 유효성 확인
agent-browser session remove naver                # 삭제
```

비밀번호는 **어디에도 저장되지 않습니다.** 브라우저 안에서만 쓰이고, 저장되는 것은 로그인 결과물인 쿠키/localStorage입니다. 그마저 AES-256-GCM + Argon2id로 암호화해 `0600` 권한으로 보관합니다.

패스프레이즈는 OS 키체인 → 프롬프트 → `AGENT_AUTH_KEY_CI` 순으로 해석합니다. 무인 실행하려면 키체인에 저장해두면 됩니다(`login` 마지막에 물어봅니다).

이는 Playwright의 `storageState`, Browserbase의 Contexts, Steel의 세션 영속화와 같은 접근입니다. 2FA나 매직 링크가 걸린 경우 사람의 개입이 필요하며, 이는 회피 대상이 아니라 정상적인 설계입니다.

### 자격증명 다루기

`--secrets`로 플레이스홀더 치환을 쓰면 **자격증명이 LLM 컨텍스트에 들어가지 않습니다.**

```bash
cat > secrets.env <<'EOF'
X-LOGIN=myaccount
X-PASSWORD=실제비밀번호
EOF
chmod 600 secrets.env        # 0600이 아니면 기동을 거부합니다

agent-browser serve --secrets=./secrets.env
```

에이전트는 키 이름만 사용합니다.

```
LLM이 보내는 것    type_text(text="X-PASSWORD")
실제 입력되는 것    실제비밀번호
트레이스에 남는 것  "X-PASSWORD"
```

등록되지 않은 키는 **치환하지 않고 그대로 입력**합니다(조용한 실패 방지). 치환 여부는 `ActionResult.data.secret_resolved`로 확인할 수 있습니다.

`secrets.env`는 반드시 `.gitignore`에 넣으십시오.

**한계** — 치환된 값은 페이지 DOM에 존재하므로 이후 관찰이나 스크린샷에 노출될 수 있습니다. 이 기능이 보장하는 것은 "LLM 컨텍스트 유입 차단"뿐이며 종단 간 기밀성이 아닙니다. 볼트 연동(1Password, HashiCorp Vault)은 v1.1 대상입니다.

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
