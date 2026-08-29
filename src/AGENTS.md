# AI Coding Agent Operational Handbook & Guidelines (`AGENTS.md`)

> **[IMPORTANT: 최상단 무결성 및 보안 수칙 (Strict Integrity Rules)]**
> 1. **바이트 무결성**: 모든 코드 및 마크다운 생성 시 **Carriage Return (`0x0D`), ASCII Bell (`0x07`), 미처리된 LaTeX 수식(`$...$`)의 삽입을 절대 금지**합니다.
> 2. **비밀정보 하드코딩 금지**: `*.enc`, `auth/`, `.env`, API 키, 패스워드는 절대 코드/테스트/로그에 하드코딩하지 않으며 `.gitignore` 규칙을 엄격히 준수합니다.
> 3. **배타적 소유권 준수**: 할당된 워크스트림 디렉터리 외 타 모듈 코드나 루트 공용 파일을 임의로 수정하지 않습니다.
> 4. **기준 사양서**: [PRD.md](../PRD.md) (v13.0 - Ultimate Final Implementation Baseline). 레포지토리 루트에 위치합니다.

---

## 1. 프로젝트 개요, 기술 스택 및 환경 부트스트랩

* **언어 및 런타임**: Python 3.11+ 비동기 런타임 (`asyncio`)
* **패키지 및 빌드 관리**: `uv >= 0.5.0` & `hatchling` (src-layout 기반)
* **브라우저 제어 엔진**: `playwright >= 1.48.0` (Async API & Direct CDP Session)
* **데이터 검증 & 모델**: `pydantic >= 2.10.0` (Pydantic V2)
* **보안 & 암호화**: `cryptography >= 44.0.0` (AES-256-GCM, Argon2id KDF), `keyring >= 25.5.0`
* **인터페이스**: `textual >= 0.80.0`, `rich >= 13.9.0`, `mcp >= 1.1.0` (FastMCP)

### 🛠️ 필수 환경 부트스트랩 (작업 시작 전 1회 실행)
모든 에이전트는 작업 착수 전 레포지토리 루트에서 아래 명령을 실행하여 환경을 초기화하고 자가 진단합니다:
```bash
# 1. uv 가상환경 동기화 및 dev 의존성(pytest 등) 설치
uv sync --extra dev

# 2. editable 패키지 설치 (src-layout 활성화)
uv pip install -e .

# 3. Chromium 브라우저 바이너리 및 시스템 의존성 설치
playwright install --with-deps chromium

# 4. 환경 자가 진단 (Python 3.11+ 및 필수 라이브러리 스모크)
python -c "import sys; assert sys.version_info >= (3, 11)"
python -c "import playwright, pydantic, textual, mcp, cryptography; print('Dependencies OK')"

# 5. 문서 및 스펙 무결성 선행 검증 (Exit Code 0 확인)
python check_docs.py

# 6. 게이트 명령어 구문 검증 (본 문서 §5의 명령어가 실제 실행 가능한지 확인)
python scripts/check_gate_commands.py
```

---

## 2. 레포지토리 전체 구조 및 배타적 소유권 (Module Ownership)

컨텍스트 오염 및 머지 충돌을 방지하기 위해 **워크스트림별 배타 소유 디렉터리**를 엄격히 격리합니다:

```
agent-browser/
├── pyproject.toml              # [사람/오케스트레이터 소유] 의존성 및 빌드 설정 (수정 시 사람 승인 필수)
├── uv.lock                     # [사람/오케스트레이터 소유] 의존성 락파일 (커밋 대상, 갱신 시 사람 승인 필수)
├── check_docs.py               # [사람/오케스트레이터 선제공] 문서/코드 무결성 자동 검사기
├── README.md                   # [사람/오케스트레이터 소유] 프로젝트 개요 문서
├── .gitignore                  # [사람/오케스트레이터 소유] 비밀값/임시파일 격리
├── scripts/                    # [사람/오케스트레이터 소유] 게이트 검증 스크립트 모음
│   ├── check_auth_perms.py     # [Gate 1] 세션 스토리지 파일 권한 검증기
│   ├── check_gate_commands.py  # [CI] 본 문서 §5 게이트 명령어 구문 검증기
│   └── check_contracts_freeze.py # [CI] Stage 0 계약 동결 상태 검증기
├── src/
│   ├── contracts/              # [WS-0] 인터페이스 모델, Pydantic 스키마, Protocol 클래스 (Stage 0 이후 동결)
│   ├── browser/                # [WS-1] Playwright CDP 코어, BrowserContext 풀, 세션 관리자 (AES-256-GCM)
│   ├── perception/             # [WS-2] Computed Layout DOM 살균기, AxTree 파서, Prune4Web 스코어러
│   ├── actions/                # [WS-3] 19종 액션 툴 전수 구현, Staleness 상태 검증, 단계인식 자가치유
│   ├── security/               # [WS-4] 도메인 Allowlist, page.route 인터셉션, PII 마스킹, HITL 게이트
│   ├── interface/              # [WS-5] Textual TUI 대시보드, ConfirmDialog 렌더러, FastMCP 서버 (mcp_server.py)
│   ├── harness/                # [WS-6] Mock 사이트 20종, Recall@20 벤치마크 러너, E2E 평가 스위트
│   ├── vision/                 # [WS-v1.1] Tier-2 Set-of-Marks (SoM) 시각 그라운딩 엔진 (Stage 4)
│   ├── cli.py                  # [통합/오케스트레이터 소유] 루트 CLI 명령어 진입점 (interface를 래핑)
│   └── AGENTS.md               # [가이드라인] 본 운영 지침서
└── tests/
    ├── conftest.py             # [사람/오케스트레이터 선제공] 루트 공용 테스트 픽스처
    ├── contracts/              # [WS-0 소유] 계약 스모크 테스트 (각 WS가 자신의 tests/<모듈>/ 직접 생성)
    ├── browser/                # [WS-1 소유] 브라우저 및 세션 관리자 테스트
    ├── perception/             # [WS-2 소유] AxTree 추출 및 Prune4Web 스코어링 단위 테스트
    ├── actions/                # [WS-3 소유] 19종 액션 툴 및 자가치유 테스트
    ├── security/               # [WS-4 소유] Egress 인터셉션 및 가드레일 테스트
    ├── interface/              # [WS-5 소유] TUI 및 MCP 툴 스키마/서버 테스트
    └── harness/                # [WS-6 소유] Mock 서버 및 평가 하네스 자체 검증 테스트
```

### 📖 워크스트림별 PRD 스펙 앵커 (Spec Reference Anchors)
에이전트는 코드 작성 시 [PRD.md](../PRD.md)의 지정된 섹션을 반드시 정독하고 구현에 반영해야 합니다:

| 워크스트림 | 담당 모듈 | PRD.md 필독 스펙 섹션 |
| :--- | :--- | :--- |
| **WS-0** | `contracts/` | §4 (모델 전체), §4.1 (19종 액션 표), §4.2 (`ObserveResult` 스키마), §6.1 (`ConfirmDialog` 스키마), §7.2 (Protocol 클래스) |
| **WS-1** | `browser/` | §5.1 (스토리지 암호화, 세션 만료 프로브 3종), §5.2 (오리진 격리), §3.4 (리소스/메모리 1.5GB 상한), §1.5 (지연 KPI — 엔진 스파이크 측정 기준) |
| **WS-2** | `perception/` | §3.1 (Tier-1 텍스트 파이프라인), §3.2 (Top-N 복구 사다리), §4.2 (에포크/Staleness), §4.3 (Closed Shadow DOM CDP pierce 순회) |
| **WS-3** | `actions/` | §4.1 (19종 액션 툴 전수 명세), §4.3 (단계 인식 자가 치유 및 사후조건 검증), §4.2 (에포크 무효화 트리거) |
| **WS-4** | `security/` | §5.3 (3중 가드레일, `page.route` Egress 제어 및 기술적 한계), §3.3 (무인/대화형 실행 모드 정책) |
| **WS-5** | `interface/` | §6.1 (Textual TUI 대시보드, Worker API, `ConfirmDialog` 렌더러), §8-2 (MCP 툴 스키마 버저닝 정책) |
| **WS-6** | `harness/` | §1.5 (통일된 KPI 전체 = 임계값 출처), §7.3 (게이트별 평가 커맨드 및 골든셋 10종) |

---

## 3. Git 워크플로 및 공유 자원 거버넌스 (Git Workflow)

1. **브랜치 명명 규칙**:
   - `ws/<번호>-<모듈명>` (예: `ws/0-contracts`, `ws/1-browser`, `ws/2-perception`)
2. **커밋 메시지 컨벤션**:
   - `[WS-<번호>] <간결한 작업 요약>` (예: `[WS-0] Define Pydantic V2 models and Protocol classes`)
3. **PR 및 머지 규칙**:
   - 워크스트림 브랜치의 PR 대상은 항상 `dev`입니다. `main`은 릴리스 시점에 `dev → main` PR로만 갱신됩니다.
   - `dev` / `main` 양쪽 모두 **직접 push가 차단**되어 있으며, 관리자에게도 동일하게 적용됩니다.
   - **머지 권한**: 해당 스테이지의 Gate 기계 검증이 100% 통과한 것을 확인한 후 **통합 오케스트레이터(또는 사람 감독자)만 머지를 승인**합니다.
4. **공용 픽스처 격리**:
   - 루트 `tests/conftest.py`는 오케스트레이터 소유입니다. 각 워크스트림은 `tests/<모듈>/conftest.py` 내에 독립 픽스처를 정의합니다.

### 🔒 계약 동결 상태 (Contract Freeze Status)

| 항목 | 내용 |
| :--- | :--- |
| **동결 태그** | `contracts-v1.0-frozen` |
| **승인 커밋** | `db5d49b` ([WS-0] Stage 0 계약 패키지 구현 및 동결) |
| **승인 일자** | 2026-08-29 (사람 감독자 Gate 0 승인 완료) |
| **동결 범위** | `src/contracts/**` (읽기 전용) |
| **자동 검증** | `python scripts/check_contracts_freeze.py` — CI 필수 통과 |

**Gate 0 통과 기록**: Input 모델 19종 / `ErrorCode` 27종 / KPI 상수 정합성 / `pytest tests/contracts` 31 passed

**계약 변경이 필요한 경우 (Stage 0 재동결 절차)**:
1. 에이전트는 계약 파일을 직접 수정하지 않고, **변경 필요 사유와 영향 범위를 사람 감독자에게 보고**합니다.
2. 사람 감독자가 재동결을 승인하면 변경을 반영하고 새 동결 태그(예: `contracts-v1.1-frozen`)를 생성합니다.
3. 오케스트레이터가 `scripts/check_contracts_freeze.py`의 `FREEZE_TAG` 상수를 갱신합니다.
4. 계약 변경은 **모든 워크스트림에 파급**되므로, 이미 완료된 스테이지의 회귀 테스트를 재실행해야 합니다.

---

## 4. 4단계 실행 파이프라인 (Execution Pipeline)

```mermaid
flowchart TD
    subgraph Stage0 ["Stage 0: 계약 선행 동결 (병렬 불가, 사람 승인 필수)"]
        WS0["`contracts/` 패키지 선행 구현 & 동결<br/>• ActionType(19종), ActionResult, ObserveResult<br/>• ClickInput 등 19종 입력 모델 & ACTION_INPUT_MAP<br/>• ErrorCode Enum 20종 & thresholds.py 동결<br/>• 모듈 간 Protocol 클래스 3종 선행 정의<br/>• `contracts/__init__.py` 최상위 Re-export 구축"]
        Gate0{"[Gate 0: 사람 승인 + 기계 검증]<br/>인터페이스 계약 동결"}
        WS0 --> Gate0
    end

    subgraph Stage1 ["Stage 1: 하네스 최우선 구축 & 인프라 코어 (병렬)"]
        WS6["`WS-6 harness/` (최우선 병렬)<br/>• Mock 사이트 20종 구축 (13대 필수 시나리오)<br/>• Recall@20 평가 파이프라인 (골든셋 10종)<br/>• WebArena Lite 100 하네스"]
        WS1["`WS-1 browser/`<br/>• Playwright CDP 코어<br/>• Session Manager (AES-256-GCM)<br/>• 엔진 지연 실측 스파이크"]
        WS4["`WS-4 security/`<br/>• Allowlist & Route 인터셉션<br/>• PII 마스킹 & HITL 게이트"]
        Gate1{"[Gate 1: Phase 1 Exit]<br/>기계 검증 + 하네스 골든셋 통과"}
        WS6 --> Gate1
        WS1 --> Gate1
        WS4 --> Gate1
    end

    subgraph Stage2 ["Stage 2: 인지 엔진 구현 & 즉시 벤치마크"]
        WS2["`WS-2 perception/`<br/>• Layout 살균기 & AxTree 파서<br/>• CDP pierce & Prune4Web 스코어러"]
        Gate2{"[Gate 2: Phase 2 Exit]<br/>Stage 1 하네스로 Recall@20 즉시 측정"}
        WS2 --> Gate2
    end

    subgraph Stage3 ["Stage 3: 액션 스페이스 & 인터페이스 병렬 (MVP Release)"]
        WS3["`WS-3 actions/`<br/>• WS-2 AxTree 주입받아 19종 툴 구현<br/>• Staleness 검증 & 자가치유 사다리"]
        Gate3A{"[Checkpoint 3-A]<br/>WS-3 단위 & 품질 검증 통과"}
        WS3 --> Gate3A
        
        WS5["`WS-5 interface/`<br/>• Textual TUI & ConfirmDialog<br/>• FastMCP 서버 (mcp_server.py)"]
        Gate3A --> WS5
        
        Gate3B{"[Checkpoint 3-B / Gate 3: 사람 승인]<br/>MVP v1.0 Release 최종 통과"}
        WS5 --> Gate3B
    end

    subgraph Stage4 ["Stage 4: Post-MVP v1.1 확장"]
        WS_v11["Tier-2 SoM 비전 엔진 (`src/vision/`)<br/>& 3차 Guardrail LLM"]
        Gate4{"[Gate 4: 사람 승인]<br/>v1.1 Release (기계 검증 3종)"}
        WS_v11 --> Gate4
    end

    Gate0 --> Stage1
    Gate1 --> Stage2
    Gate2 --> Stage3
    Gate3B --> Stage4
```

---

## 5. 결정론적 하네스 출력 규약 및 Gate 검증 명령어

> **🚨 중요 원칙**:
> 1. 모든 게이트 명령어는 **레포지토리 루트(`agent-browser/`)에서 실행**합니다.
> 2. **인라인 `python -c` 2줄 이상 작성 금지**: 복잡한 검증 로직은 `scripts/` 아래 독립 파이썬 파일로 모듈화하여 호출합니다.
> 3. **하네스 공통 출력 규약**: 모든 `harness.*` 모듈은 임계값을 `contracts/thresholds.py`에서 읽어오며, stdout에 단일 JSON 라인(`{"metric": str, "value": float, "threshold": float, "passed": bool, "samples": int}`)을 출력하고 **임계값 미달 시 `Exit Code 1`을 강제 반환**합니다.

### [Gate 0] Stage 0 계약 동결 검증
```bash
# 1. 19종 Input 모델 존재 검증
python -c "import contracts; assert len([m for m in dir(contracts) if m.endswith('Input')]) == 19"

# 2. 20종 이상 ErrorCode Enum 정의 검증
python -c "from contracts import ErrorCode; assert len(ErrorCode) >= 20"

# 3. KPI 상수 정의 정합성 검증
python -c "from contracts import thresholds; assert thresholds.RECALL_AT_20 == 0.95 and thresholds.ACTION_SUCCESS_RATE == 0.92"

# 4. 계약 모델 단위 스모크 테스트
pytest tests/contracts -q
```

### [Gate 1] Stage 1 인프라 & 하네스 검증
```bash
# 1. 코어 및 보안 단위 테스트
pytest tests/browser tests/security -q

# 2. 세션 스토리지 파일 권한 (0600) 및 실물 존재 검증 (POSIX / Windows 안전)
python scripts/check_auth_perms.py

# 3. 하네스 출력 계약 준수 자체 검증 (의도적 미달 케이스에서 exit 1 및 JSON 스키마 출력 확인)
python -m harness.contract_selftest

# 4. 비인가 Egress 차단 검증
python -m harness.egress_test

# 5. 하네스 자체 단위 테스트
pytest tests/harness -q

# 6. Mock 사이트 20종 기동 검증 (13대 필수 시나리오 분산 배치)
python -m harness.selfcheck --mock-sites 20

# 7. 하네스 골든셋 정합성 검증 (정답 10종 완벽 일치: recall == 1.0)
python -m harness.recall --golden

# 8. [성능 스파이크] 엔진 지연 실측 리포트 제출 (판정 아님 — 수치 확보가 목적)
python -m harness.engine_spike --sites 20 --report artifacts/engine_spike.json
```

#### ⏱️ Stage 1 성능 스파이크 (Engine Latency Spike) — WS-1 & WS-6 공동 산출물

**배경**: Playwright Python은 Node 드라이버를 서브프로세스로 구동하므로 IPC 오버헤드가 존재합니다. 그러나 실제 스텝 지연의 지배 요인은 LLM 추론(1,500~3,000ms)이며, IPC는 호출당 0.1~1ms 수준으로 추정됩니다. **추정을 근거로 아키텍처를 바꾸지 않기 위해, Stage 1에서 실측 수치를 확보합니다.**

`harness.engine_spike`는 Mock 사이트 20종에 대해 아래 4개 지표를 측정하고 JSON 리포트를 남깁니다. **본 항목은 임계값 판정 게이트가 아니며, 리포트 산출 및 제출 여부만 확인합니다.**

| 측정 항목 | 측정 방법 | 참고 기준 (판정 아님) |
| :--- | :--- | :--- |
| **AxTree 추출 단독 지연** | `Accessibility.getFullAXTree` 호출 전후 시각 차 (p50 / p95) | 관찰 예산 300ms 대비 비중 확인 |
| **CDP 왕복 오버헤드** | 무연산 CDP 호출(`Runtime.evaluate("1")`) 1,000회 평균 | 호출당 1ms 초과 시 배치 설계 재검토 |
| **Actionability 대기 지연** | 200ms 광고 로테이션 페이지에서 `click` 대기 시간 (p50 / p95) | `stable` 판정이 동적 노드에서 지연되는지 확인 |
| **관찰 파이프라인 총 지연** | AxTree 추출 + 살균 + 프루닝 전 구간 (p50 / p95) | §1.5 관찰 예산 300ms 대비 여유 판단 |

**리포트 활용 규칙**:
* AxTree 추출만으로 관찰 예산(300ms)의 50%를 초과하면 **Stage 2 착수 전 사람 감독자에게 보고**하고, 예산 재조정 또는 프루닝 전략 변경을 논의합니다.
* CDP 왕복이 호출당 1ms를 초과하면 Prune4Web 스코어러를 **요소별 호출이 아닌 단일 `Runtime.evaluate` 일괄 처리**로 설계해야 합니다 (WS-2 필수 준수 사항).
* Actionability 대기가 광고 로테이션 페이지에서 p95 1,000ms를 초과하면, §4.2 자체 staleness 검증을 통과한 요소에 한해 `force=True` 경로를 허용할지 사람 감독자가 판단합니다.
* 본 리포트는 Stage 3의 `harness.latency_test`(실제 판정 게이트) 임계값 타당성을 검토하는 근거 자료로 사용됩니다.

#### 🎪 Mock 사이트 20종 필수 커버리지 기준 (WS-6 수용 기준)
* 13대 필수 시나리오(로그인 폼/2FA, 다단계 폼+파일 업로드, CSV 다운로드, 중첩 iframe, Open Shadow DOM, Closed Shadow DOM, 무한 스크롤, 200ms 광고 로테이션 동적 노드, 네이티브 Dialog, 팝업 새 탭, 세션 만료 리다이렉트 HTTP 401, SPA 클라이언트 라우팅, 지연 로딩 노드)가 20종 사이트에 분산 배치되어 전수 커버되어야 합니다.

### [Gate 2] Stage 2 인지 엔진 검증
```bash
# 1. 인지 엔진 단위 테스트
pytest tests/perception -q

# 2. WebArena 100개 샘플 Recall@20 (>= 95.0%) 및 토큰(p50<=2500, p95<=6500), 지연(p50<=300ms) 검증
python -m harness.recall --pages 100 --top-n 20
```

### [Gate 3-A] Stage 3 액션 툴 단위 검증 (WS-3 완료 즉시)
```bash
# 1. 액션 단위 테스트 통과 및 19종 툴 전수 정의 검증
pytest tests/actions -q
python -c "from contracts import ActionType; assert len(ActionType) == 19"

# 2. 액션 실행 성공률 (>= 92.0%)
python -m harness.actions_test --tasks 100

# 3. 자가 치유 성공률 (>= 80.0%)
python -m harness.self_healing --tasks 100

# 4. 동적 페이지 Staleness 불일치율 (<= 5.0%)
python -m harness.staleness --runs 100
```

### [Gate 3-B] Stage 3 인터페이스 통합 및 MVP v1.0 Release Gate (전사 완료)
```bash
# 1. 인터페이스 단위 테스트
pytest tests/interface -q

# 2. 전체 통합 회귀 단위 테스트 스위트 (선행 모듈 파손 방지)
pytest tests -q

# 3. 19종 툴 MCP 클라이언트 E2E 왕복 스모크
python -m harness.mcp_smoke --tools 19

# 4. 테스트 플레이키율 (<= 2.0%)
python -m harness.flaky_test --runs 100

# 5. 스텝당 로컬 순수 지연 (p50 <= 800ms)
python -m harness.latency_test --mode step

# 6. 복합 스텝 지연 (p95 <= 2,200ms)
python -m harness.latency_test --mode complex

# 7. 참조 에이전트 태스크 완수율 (>= 60.0%)
python -m harness.webarena --tasks 100

# 8. 결정론적 IPI 차단율 (>= 90.0%, FPR <= 2.0%)
python -m harness.wasp --mode deterministic

# 9. 세션 만료 프로브 오탐율 (FPR <= 1.0%)
python -m harness.session_probe --runs 50
```

### [Gate 4] Stage 4 Post-MVP v1.1 Release Gate
```bash
# 1. 종합 IPI 3중 방어선 검증
python -m harness.wasp --mode full          # block_rate >= 0.98, fpr <= 0.02
python -m harness.stakebench                # block_rate >= 0.96

# 2. Tier-2 SoM 무인 발동 빈도 및 VLM 지연 검증
python -m harness.tier2_som --runs 50 --mode unattended   # trigger_rate <= 0.10, p95_latency_ms <= 3500
```

---

## 6. 에이전트 개발 거버넌스 및 안전 수칙 (Strict Operating Rules)

### 1) 타입 안전성 및 `Any` 예외 규정
* 모든 공개 함수와 메서드는 엄격한 타입 힌트와 Pydantic V2 모델을 강제합니다.
* **`Any` 예외**: `contracts/models.py`에 선언된 `ActionResult.data: Dict[str, Any]`와 같이 계약 자체에 명시된 불가피한 경우를 제외하고는 **임의의 `Any` 사용을 전면 금지**합니다.

### 2) 개발 토큰 예산 배분 및 초과 처리 정책
* **총 개발 예산 상한**: **8,000,000 토큰 (또는 USD $80)**
* **워크스트림별 배분**:
  - `WS-1 browser/`: 1,000,000 토큰
  - `WS-2 perception/`: 1,000,000 토큰
  - `WS-3 actions/`: 1,500,000 토큰
  - `WS-4 security/`: 500,000 토큰
  - `WS-5 interface/`: 1,000,000 토큰
  - `WS-6 harness/`: 1,000,000 토큰
  - 전사 통합 및 예비 버퍼: 2,000,000 토큰
* **예산 소진 시 동작**: 단일 워크스트림이 할당 예산을 소진하면 CI 오케스트레이터가 즉시 작업을 일시정지하고 사람 감독자에게 증액 승인을 요청합니다.

### 3) 게이트 실패 3종 분류 및 대응 프로토콜 (Failure Protocols)
1. **[유형 A: 구현 결함]** 단위 테스트 또는 하네스 임계값 미달:
   - 해당 워크스트림 에이전트가 코드를 수정하고 로컬 테스트를 재수행합니다.
   - **CI 오케스트레이터가 서브태스크당 빌드 실패 횟수를 자동 카운트하며, 10회 연속 실패 시 프로세스를 강제 종료하고 사람 감독자에게 에스컬레이션**합니다.
2. **[유형 B: 하네스/테스트 자체 결함]** Mock 사이트 결함 또는 잘못된 골든셋:
   - 에이전트가 이슈를 제안하되, **사람 감독자가 확정**한 후 WS-6 브랜치에서 하네스를 수정합니다.
3. **[유형 C: 비현실적 KPI 목표 충돌]**:
   - 10회 이상 수정 후에도 아깝게 미달할 경우 임의로 임계값을 낮추지 않고, **사람 감독자가 확정**하여 목표 조정 또는 아키텍처 재검토를 결정합니다.

### 4) 자동화된 회귀 방지 CI (Regression Guard)
* PR 머지 시 단위 테스트 및 Recall@20 벤치마크를 자동 측정하며, **절대 KPI 임계값 미달 시 또는 직전 커밋 대비 정상 편차 2.0%p 초과 하락 시 머지를 자동 차단**합니다.
