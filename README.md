# Agent Browser

> **AI 에이전트 네이티브 헤드리스 브라우징 런타임 & MCP 서버**
>
> Python 3.11+ / Playwright CDP / Model Context Protocol

에이전트가 웹을 다루려면 두 가지가 필요합니다. 페이지를 **토큰 예산 안에서 이해하는 것**, 그리고 **눌렀다고 착각하지 않는 것**입니다. 이 프로젝트는 그 두 가지를 목표로 만들었습니다.

원시 HTML 대신 접근성 트리를 정제해 상위 20개 요소만 넘기고(관찰 토큰 중앙값 13개), 모든 액션은 실행 후 DOM 상태로 성공을 검증합니다.

다른 에이전트(Claude Desktop·Claude Code 등 MCP 클라이언트)가 MCP 로 부르는 브라우저 도구입니다. 무엇을 할지는 부르는 에이전트가 판단하고, 이 도구는 페이지 조작·관찰, 차단·주입·robots 신호, 사람 인계를 맡습니다.

---

## 주요 기능

- **19종 액션 툴 + 서버 도구** — 관찰·클릭·입력·이동·탭·프레임·다운로드 등 액션과, 사람 인계·레시피·YouTube 검색 서버 도구를 stdio MCP 로 노출합니다. → [MCP 클라이언트 연동](docs/mcp-integration.md)
- **관찰 → 액션 검증** — 접근성 트리를 정제한 상위 요소와 `epoch` 을 돌려주고, 오래된 `element_id` 는 `E_TOCTOU_MISMATCH` 로 거부하며, 성공은 실행 뒤 DOM 상태(사후조건)로 판정합니다. → [관찰 → 액션 흐름](docs/mcp-integration.md#관찰--액션-흐름)
- **고위험 액션 게이트** — 결제·주문·삭제·제출·업로드를 무인 모드에서 막고, `--pre-approve`·`--mode interactive`·사람 승인 코드로 엽니다. → [고위험 액션 허용](docs/mcp-integration.md#고위험-액션-허용-serve---pre-approve---mode)
- **차단 대신 신호** — 차단·캡차(`data.challenge`), 프롬프트 주입(`data.injection_suspected`), robots.txt(`data.robots`)를 결과에 붙이고 판단은 부르는 에이전트에 맡깁니다. → [응답 신호](docs/signals.md)
- **헤드리스 이름 재시도** — 헤드리스 이름만 막는 사이트에서 이름·Client Hints 만 바꿔 한 번 다시 열고, 그 사실을 `data.identity` 로 공개합니다. → [헤드리스 이름 재시도](docs/headless-retry.md)
- **사람 인계·로그인 유지** — 조작권(`control take/release`)·승인 코드, 필요할 때만 창(`--browser on-demand`), 영속 프로필(`--profile`), 미리 로그인(`agent-browser login`). → [사람 인계와 로그인](docs/human-handoff.md)
- **동작 캐시(레시피)** — 통과한 흐름을 자동 저장해 도구 호출 한 번으로 재생하고, 화면이 어긋나면 누르지 않고 멈춥니다. → [기능](docs/features.md#동작-캐시--레시피-browser_recipe)
- **도착 페이지 기억** — 사용자가 "이 페이지 기억해"라고 한 주소를 저장해 같은 출처를 관찰할 때 후보(`data.saved_pages`)로 붙입니다(자동 저장 없음). → [기능](docs/features.md#도착-페이지-기억-browser_recipe-remember_pagepagesforget_page-ws-61)
- **반복 묶음 대상 구분** — 두 달 달력의 같은 날짜·상품 행의 같은 '담기'처럼 같은 틀이 반복되면 관찰에 묶음 표제·위치(`data.repeated`)를 붙이고, 관찰 뒤 묶음(달)이 바뀐 대상은 누르지 않습니다. → [기능](docs/features.md#반복-묶음-대상-구분-datarepeated-ws-62)
- **PDF 읽기·YouTube 영상 검색** — PDF 본문을 `data.document` 로, YouTube 검색을 공식 Data API v3 로. → [기능](docs/features.md)
- **Egress 가드·출력 비밀 가림** — 검증 프록시로 사설망·메타데이터를 막고, 응답 주소·문장 속 비밀 값을 `[REDACTED]` 로 가립니다. → [보안](docs/security.md)
- **우회하지 않습니다** — 캡차 자동 풀이·확인 화면 통과·IP 프록시 순환·자동화 지문 숨기기를 하지 않습니다. 막히면 사람에게 넘깁니다.

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

6936개가 통과해야 합니다(7개 건너뜀, 4개 예상 실패). Chromium이 필요한 테스트가 포함되어 있습니다.

LLM 연동(`run` 명령)과 실환경 태스크 실행은 [docs/llm-agent.md](docs/llm-agent.md)를 보십시오.

---

## MCP 클라이언트 연동

19종 툴(과 사람 인계·레시피·YouTube 검색용 서버 도구)을 stdio로 노출합니다. Claude Desktop 설정 예시입니다.

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

브라우저 방식(`--browser`)·고위험 액션 허용·관찰 → 액션 흐름 등 상세는 [docs/mcp-integration.md](docs/mcp-integration.md)에 있습니다.

---

## 검증 요약

수치는 전부 실제 실행 결과이며 CI의 릴리스 게이트에서 독립 재현됩니다 — Element Recall@20 1.0, 액션 성공률 1.0, 자가 치유율 1.0, 스텝 지연 p50 72ms(Mock 기준). 표 전체·2026-09-23 정정·실환경 태스크 완수율은 [무엇을 검증했는가](docs/development.md#무엇을-검증했는가)에 있습니다.

---

## 문서

| 문서 | 내용 |
| :--- | :--- |
| [docs/README.md](docs/README.md) | 문서 목차 |
| [docs/mcp-integration.md](docs/mcp-integration.md) | MCP 연동 상세 — `serve --browser`·`--nav-settle`·`--pre-approve`/`--mode`, 관찰 → 액션 흐름, 응답 형식 |
| [docs/signals.md](docs/signals.md) | 차단·캡차, 프롬프트 주입, robots.txt 신호 |
| [docs/headless-retry.md](docs/headless-retry.md) | 헤드리스 이름 재시도, `data.identity`·`retry_skipped`·`cookie_change`, 쿠키 장부(WS-47·WS-52) |
| [docs/features.md](docs/features.md) | PDF 읽기, YouTube 영상 검색, 동작 캐시(레시피), 도착 페이지 기억 |
| [docs/human-handoff.md](docs/human-handoff.md) | 미리 로그인, 사람 인계(조작권·승인), 로그인 유지(`--profile`), 필요할 때만 창(`on-demand`) |
| [docs/llm-agent.md](docs/llm-agent.md) | 내장 LLM 에이전트(`run`)·실환경 태스크 실행 |
| [docs/security.md](docs/security.md) | 안전장치(예산·Egress·주입·출력 비밀 가림·세션 암호화), 로그인 세션과 자격증명 |
| [docs/architecture.md](docs/architecture.md) | 소스 구조 |
| [docs/troubleshooting.md](docs/troubleshooting.md) | 문제 해결 |
| [docs/limitations.md](docs/limitations.md) | 알려진 한계 |
| [docs/development.md](docs/development.md) | 개발 참여, 무엇을 검증했는가 |

---

## 라이선스

Apache-2.0
