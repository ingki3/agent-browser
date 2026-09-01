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

# 7. 하네스 커버리지 자기검증 (§5 규칙 1 준수 여부)
python scripts/check_harness_coverage.py

# 8. 버전을 올렸다면 uv.lock도 함께 커밋되었는지 확인
#    `uv run`이 lock을 자동 재동기화하므로 검사 스크립트로는 잡히지 않는다.
#    (실측 — v1.0.3에서 lock이 1.0.2로 남은 채 릴리스됐다)
git status --porcelain uv.lock
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
│   ├── check_contracts_freeze.py # [CI] Stage 0 계약 동결 상태 검증기
│   └── check_harness_coverage.py # [CI] 하네스 커버리지 자기검증 강제
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

### ✅ Gate 제출 전 필수 체크리스트

게이트 통과를 보고하기 전에 아래를 모두 확인하십시오. 하나라도 미충족이면 **수치를 제출하지 마십시오.**

- [ ] 게이트 명령어를 **레포지토리 루트에서 실제로 실행**했고, 출력 JSON과 exit code를 확인했다.
- [ ] 하네스를 신규 작성·수정했다면 **사보타주 검증**(§5 규칙 5)을 수행했고, 파손 상태에서 게이트가 실패하는 것을 확인했다.
- [ ] 지표와 함께 **커버리지 필드**(`*_covered`, `stages_covered`, `pruning_effective_pages` 등)를 확인했고, 측정 범위가 요건을 충족한다.
- [ ] 분포가 한쪽에 쏠려 있지 않다 (예: `strategy_breakdown`이 특정 단계에만 몰려 있지 않다).
- [ ] 게이트가 실패했다면 **임계값이 아니라 구현을 수정**했다.
- [ ] 회귀 방지 테스트를 추가해 동일 결함이 재발하면 CI가 잡도록 했다.

---

## 5. 결정론적 하네스 출력 규약 및 Gate 검증 명령어

> **🚨 중요 원칙**:
> 1. 모든 게이트 명령어는 **레포지토리 루트(`agent-browser/`)에서 실행**합니다.
> 2. **인라인 `python -c` 2줄 이상 작성 금지**: 복잡한 검증 로직은 `scripts/` 아래 독립 파이썬 파일로 모듈화하여 호출합니다.
> 3. **하네스 공통 출력 규약**: 모든 `harness.*` 모듈은 임계값을 `contracts/thresholds.py`에서 읽어오며, stdout에 단일 JSON 라인(`{"metric": str, "value": float, "threshold": float, "passed": bool, "samples": int}`)을 출력하고 **임계값 미달 시 `Exit Code 1`을 강제 반환**합니다.

---

### 🧪 하네스 설계 필수 규칙 (Harness Design Rules)

> **이 절은 실제 사고 기록에서 도출되었습니다.** 아래 세 건은 모두 "게이트가 만점을 보고했으나 실제로는 해당 기능이 파손돼 있어도 통과하던" 사례입니다. 하네스를 작성·수정하는 모든 에이전트는 본 규칙을 준수해야 하며, 위반 시 게이트 수치는 **무효**로 간주합니다.

#### 사고 기록 (재발 방지 대상)

| # | 하네스 | 파손시킨 대상 | 게이트 반응 | 근본 원인 |
| :-- | :--- | :--- | :--- | :--- |
| 1 | `self_healing` | 치유 사다리 2·3·4단계 전체 | **1.0 통과** | 모든 시나리오가 1단계에서 해결돼 하위 단계가 미실행 |
| 2 | `actions_test` | 11종 액션 | **1.0 통과** | 19종 중 8종만 측정 |
| 3 | `egress_test` | allowlist 접미사 비교 취약점 | **유출 0건 통과** | 표본이 '명백한 외부 도메인'만 포함, 우회 기법 부재 |
| 4 | `recall` | 스코어러 전체 | **1.0 통과** | 모든 골든 페이지의 후보가 Top-N 미만이라 프루닝 미동작 |

> **핵심 교훈**: **측정하지 않은 경로는 파손돼도 게이트를 통과합니다.** 성공률이 높다는 것은 "구현이 옳다"가 아니라 "측정한 범위에서 옳다"만을 의미합니다.

#### 규칙 1 — 커버리지를 지표와 함께 보고할 것

성공률만 출력하는 하네스는 불완전합니다. **무엇을 측정했는지**를 `extra`에 함께 담고, 필수 범위가 미달이면 `Exit Code 2`로 실패시킵니다.

```python
# 필수: 측정 범위를 지표와 함께 보고
extra={
    "actions_covered": len(covered),      # 실제 실행한 액션 수
    "actions_required": len(ActionType),  # 계약상 필요한 수
}

# 필수: 미달 시 측정 자체를 무효 처리
missing = [a for a in ActionType if a not in covered]
if missing:
    sys.exit(int(emit_error(metric, f"{len(missing)}종 미측정: {missing}")))
```

적용 대상과 현재 구현:

| 하네스 | 커버리지 요건 | 미달 시 |
| :--- | :--- | :--- |
| `actions_test` | `ActionType` 19종 전수 실행 | exit 2 |
| `self_healing` | 치유 사다리 4단계 전수 발동 | exit 2 |
| `recall` | 프루닝이 실제 동작한 페이지 ≥ 1 | exit 2 |
| `selfcheck` | 13대 시나리오 전수 커버 | exit 1 |

#### 규칙 2 — 시나리오는 목표 경로를 **고유하게** 유발할 것

시나리오가 의도한 것보다 상위(또는 다른) 경로에서 해결되면, 목표 경로는 "측정된 것처럼 보이지만 실제로는 미검증" 상태입니다.

- 각 시나리오에 **의도한 경로를 명시**하십시오 (`expected_stage` 등).
- 실행 결과가 의도와 다르면 **경고가 아니라 실패**로 처리하십시오.
- 예: 3단계(텍스트 유사도)를 노렸다면 CSS 경로도 함께 변형해 4단계로 새지 않게 합니다.

#### 규칙 3 — 표본에 **우회·공격 케이스**를 포함할 것

정상 케이스와 명백한 실패 케이스만으로는 우회 취약점을 잡지 못합니다. 보안·검증 계열 하네스는 다음을 반드시 포함합니다.

- **경계에 인접한 케이스**: `evil-example.com`, `example.com.evil.test` (허용 도메인을 닮은 문자열)
- **의미가 반전된 케이스**: `'삭제'` → `'삭제 취소'` (문자열은 유사하나 동작이 반대)
- **반대 방향 오류**: 과차단·미탐. "전부 차단"으로 유출 0건을 만드는 위장을 별도 지표로 차단합니다.

#### 규칙 4 — 측정 대상이 **실제로 동작하는 조건**을 만들 것

기능이 발동하지 않는 입력만 모으면 그 기능을 측정할 수 없습니다.

- Top-N 프루닝을 측정하려면 후보가 **N보다 많은** 페이지가 필요합니다.
- 자가 치유를 측정하려면 요소가 **실제로 stale이 되는** 변형이 필요합니다.
- 측정 전 조건 성립 여부를 확인하고, 성립하지 않으면 실패시키십시오.

#### 규칙 5 — 하네스 신규 작성·수정 시 **사보타주 검증** 필수

새 하네스를 만들거나 기존 하네스를 수정했다면, 커밋 전에 다음을 수행하고 결과를 PR 본문에 기록합니다.

```bash
# 1. 측정 대상 모듈의 핵심 로직을 의도적으로 파손
#    (예: allowlist 검사를 통과시키기, 정렬 키를 무작위로 바꾸기)
# 2. 하네스 실행
python -m harness.<모듈>
# 3. 반드시 exit 1 또는 exit 2로 실패해야 함
#    exit 0이 나오면 그 하네스는 해당 결함을 탐지하지 못한다
# 4. 파손을 원복하고 정상 통과 확인
```

> **판정 기준**: 사보타주 상태에서 게이트가 통과하면, 그 하네스는 **결함이 있는 것으로 간주**하고 표본·시나리오를 보강해야 합니다. 임계값을 낮추는 방식의 대응은 금지합니다.

#### 규칙 6 — 게이트 실패 시 임계값을 조정하지 말 것

게이트가 실패하면 **구현을 고치는 것이 원칙**입니다. 실제 사례:

- 액션 성공률 66.7% 측정 → 임계값 하향이 아니라 접근성 이름 폴백·사후조건 신호 추가로 **100% 회복**
- Recall 0.909 측정 → 임계값 하향이 아니라 반복 패턴 감점 도입으로 **정답 순위 35위 → 4위**

임계값 변경이 불가피하다고 판단되면 **사람 감독자 승인**을 받아야 하며, PRD §1.5 수정과 `contracts/thresholds.py` 재동결 절차를 함께 거칩니다.

---


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
python -m harness.selfcheck --mock-sites 22

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

# 3. 19종 툴 MCP 클라이언트 E2E 왕복 스모크 (19종 미호출 시 exit 2)
python -m harness.mcp_smoke

# 4. 테스트 플레이키율 (<= 2.0%, 동적 시나리오 필수 포함)
python -m harness.flaky_test --runs 5

# 5·6. 스텝 지연 (p50 <= 800ms / p95 <= 2,200ms, 관찰+액션 구간 모두 측정)
python -m harness.latency_test --steps 100

# 7. 참조 에이전트 태스크 완수율 (>= 60.0%, 멀티스텝 필수 포함)
python -m harness.webarena --tasks 20

# 8. 결정론적 IPI 차단율 (>= 90.0%) 및 오탐율 (FPR <= 2.0%) 동시 측정
python -m harness.ipi_test

# 9. 세션 만료 프로브 오탐율 (FPR <= 1.0%)
python -m harness.session_probe --runs 50
```

> **명령어 주석**:
> * 5·6번은 하나의 실행에서 p50과 p95를 함께 산출합니다. p95 초과 시에도 exit 1입니다.
> * 8번은 `harness.ipi_test`입니다. `harness.wasp`는 Gate 4(v1.1) 전용입니다.
> * 7번의 참조 에이전트는 **LLM을 호출하지 않는 결정론적 정책**입니다. 런타임 능력만
>   측정하며, 모델 교체로 수치가 흔들리지 않아 회귀 탐지가 가능합니다.

---

### ⚠️ Mock 환경 수치의 한계 (실환경 측정 결과)

Gate 2 / Gate 3-B의 모든 수치는 **Mock 사이트 기준**입니다. 실제 웹과의 격차를
`python -m harness.realworld`로 측정한 결과, 다음 항목은 Mock 수치를 실제 성능으로
해석하면 안 됩니다.

| 항목 | Mock | 실제 웹 (측정값) |
| :--- | :--- | :--- |
| 최대 후보 요소 수 | 69개 | **1,051개** (위키백과 문서) |
| 관찰 토큰 (Top-20) | 13 | 103 (중앙값) |
| 관찰 지연 | 6ms | 36ms (중앙값), 119ms (최대) |
| 상위 0.3점 동점 밴드 | — | **46~67개**가 Top-20 자리 20개를 경쟁 |
| 대표 UI 요소 Top-20 진입 | — | **6개 중 2개** |

**핵심 격차 — 점수 밀집(score collapse)**:
실제 페이지에서는 본문 링크와 UI 컨트롤이 같은 점수대(7.1~7.3)에 뭉칩니다.
위키백과에서 상위 0.3점 안에 46개가 몰려 Top-20 자리를 다투므로, 순위가 사실상
DOM 순서로 결정되고 `Log in`(41위) 같은 실제 액션 요소가 밀려납니다.

**해석 지침**:
* Gate 2의 `Recall@20 = 1.0`은 "Mock 골든셋 기준"입니다. 실제 웹 태스크 성공률을
  보장하지 않습니다.
* 목표 키워드(`goal_keywords`)가 주어지면 `W_KEYWORD`(4.0)가 밀집을 뚫습니다.
  **실환경에서는 키워드 없는 관찰을 신뢰하지 마십시오.**
* v1.1의 Tier-2 SoM(비전 폴백)이 필요한 근거가 바로 이 구간입니다.

`harness.realworld`는 **판정 게이트가 아닙니다.** 외부 네트워크에 의존하므로
CI 필수 체크로 등록하지 않으며, 격차 관측 및 회귀 감시 용도로만 실행합니다.

---

### 📊 실환경 에이전트 완수율 (LLM 연동 실측)

`python -m harness.agent_eval --report artifacts/agent_eval.json`

공개 사이트 6곳에 대해 난이도 3단계 12개 태스크를 LLM(OpenRouter) 연동으로 실행한
결과입니다. **에이전트의 `finish` 선언을 신뢰하지 않고 최종 페이지 상태를 JS로
독립 검증**합니다.

| 지표 | 값 |
| :--- | :--- |
| 태스크 완수율 (독립 검증) | **31/31 = 100%** |
| 태스크 완수율 (자기 보고) | 28/31 = 90.3% |
| 난이도별 | easy 4/4 · medium 5/5 · hard 5/5 · multistep 6/6 · dynamic 5/5 · commercial 6/6 |
| 태스크당 비용 (중앙값) | $0.0004 |
| 태스크당 스텝 (중앙값) | 3 |
| 31개 전체 비용 / 시간 | $0.0390 / 57.3분 |

**100%를 액면 그대로 읽지 마십시오.** `silent_wins`가 3건입니다 — 에이전트가
성공을 인지하지 못한 채 결과만 맞은 경우입니다.

```
todo-add-complete   시간 상한 초과로 강제 종료 (631초)
dyn-enable-input    LLM이 JSON 아닌 응답 반환 -> 포기
melon-chart         finish 가드가 완료 선언을 거부
```

실질 성능은 자기 보고 기준 **90.3%**에 가깝습니다. 독립 검증만 보면
운으로 맞은 것까지 성공으로 집계됩니다.

`false_claims`는 0건입니다. WS-15의 finish 가드 이후 잘못된 성공 선언이
사라졌습니다.

난이도는 5단계입니다.

| 티어 | 성격 |
| :--- | :--- |
| `easy` | 단일 클릭으로 완료 |
| `medium` | 입력 + 제출 |
| `hard` | 탐색이 필요하거나 SPA |
| `multistep` | 5스텝 이상, 이전 스텝 결과 위에 액션을 쌓음 |
| `dynamic` | 액션 후에야 요소가 생기거나 활성화됨 |

`dynamic` 티어는 **'이미 존재하는 요소'를 다루지 않습니다.** 비동기 로딩,
클릭으로 생성되는 요소, 비활성 해제, 스크롤 지연 로딩을 검증합니다.
이 구간이 없으면 에포크 갱신과 재관찰 경로가 측정되지 않습니다.

**액션 성공률(94.4%)과 완수율(91.7%)의 격차가 LLM 판단 오류입니다.**
런타임이 액션을 정확히 수행해도 LLM이 잘못된 요소를 고르면 태스크는 실패합니다.

#### 자기 보고와 실제의 불일치 (`false_claims`)

유일한 실패 사례는 **에이전트가 성공을 선언했으나 실제로는 실패**한 경우입니다.

```
hn-comments: "첫 번째 기사의 댓글 링크를 클릭해 댓글 페이지로 이동"
  에이전트 주장: 달성   |   독립 검증: 미달성
  최종 URL: /newcomments   (기대: /item?id=...)
```

`comments`라는 이름의 다른 링크를 클릭하고 목표 달성으로 판단했습니다.
런타임은 정상 동작했고 요소 선택 판단이 틀린 것입니다.

> **측정 원칙**: 자기 보고만 집계하면 이 태스크도 성공으로 잡혀 완수율이
> 100%로 보고됩니다. 리포트의 `false_claims`와 `silent_wins`(달성했으나
> 스스로 모르는 경우)를 항상 함께 확인하십시오.

#### 검증식 유효성 사전 점검

각 태스크는 **액션을 수행하기 전에** 검증식을 한 번 평가합니다. 초기 상태에서
이미 참이면 그 검증식은 무의미하므로(아무것도 하지 않아도 통과) 측정 전체를
`exit 2`로 무효화합니다.

이 검사는 실제로 결함을 잡았습니다. `iana-reserved` 태스크는 시작 URL이
`/domains/reserved`인데 검증식이 `pathname.includes('/domains')`여서 처음부터
참이었습니다. 즉 **에이전트가 아무 일도 하지 않아도 성공으로 집계**되고
있었습니다.

> 완수율을 보고하기 전에 `baseline_already_true`가 있는지 반드시 확인하십시오.
> 통과하는 검증식보다 **실패할 수 있는 검증식**이 필요합니다.

#### 점수 밀집 해소 (WS-13)

실제 페이지에서 대다수 요소가 `role=link` + `in_viewport`로 동일해 점수가
뭉쳤습니다. Top-40의 reasons 조합이 **위키백과 2종, 해커뉴스 1종**이었고,
해커뉴스는 40개가 전부 7.15 동점이었습니다. 순위가 사실상 DOM 순서로
결정되어 네비게이션이 기사 링크에 밀렸습니다.

실측으로 변별 축을 찾았습니다.

| | 네비게이션 | 콘텐츠 링크 |
| :--- | :--- | :--- |
| 수직 위치 | top = 12px | top = 44~1056px |
| 라벨 길이 | 1단어 (3~8자) | 8단어 (30~79자) |
| 형제 수 | 7 (나란히 배치) | 1 |

`W_TOP_PROXIMITY`(상단 근접)와 `W_SHORT_LABEL`(짧은 라벨)을 추가한 결과입니다.

| 대상 | 이전 | 이후 |
| :--- | ---: | ---: |
| 위키백과 `Log in` | 41위 | 3위 |
| 해커뉴스 `past` | 50위 | 4위 |
| 해커뉴스 `ask` | 54위 | 3위 |
| 해커뉴스 `login` | 48위 | 6위 |

**목표 키워드 없이** 달성한 수치입니다. 이전에는 키워드 주입에 의존해야
Top-20에 들었습니다.

> 시맨틱 영역(nav/header/main)도 후보로 검토했으나 기각했습니다. 해커뉴스는
> 모든 요소가 `body` 직속이라(시맨틱 태그 미사용) 신호가 되지 않습니다.
> 수직 위치는 시맨틱 마크업 여부와 무관하게 동작합니다.

**주의**: 상단 근접은 보조 축입니다. 배너·광고도 상단에 있으므로 이 신호만으로
중요도를 판단하면 안 됩니다.

#### max_tokens 기본값과 예산의 관계 (WS-13)

reasoning 계열 모델은 특정 스텝에서 사고 토큰이 튑니다. 소진되면 그 스텝의
작업이 통째로 버려지므로 **기본값을 32,768로 둡니다**(8,192에서 상향).

상한을 늘려도 평상시 사용량은 늘지 않습니다. 모델이 실제 생성한 만큼만
과금되며 이 값은 절단 지점일 뿐입니다. 다만 폭주 스텝에서는 그만큼 더
소비하므로 계약 예산이 최종 방어선입니다.

```
32,768토큰 폭주가 반복될 때
  1스텝  누적  34,768토큰
  2스텝  누적  69,536토큰
  3스텝  누적 104,304토큰
  4스텝  BudgetExceeded 차단 (상한 100,000)
```

`BudgetGuard`가 계약 상한($0.75 / 100,000토큰 / 30스텝)을 강제하므로,
스텝 상한을 키워도 태스크 비용은 계약 범위를 넘지 않습니다.

**32,768 적용 후 실측**

| 태스크 | 8,192 | 32,768 |
| :--- | :--- | :--- |
| `todo-add-complete` | 실패 (소진) | 성공 5스텝 \$0.0130 |
| `dyn-enable-input` | 실패 (소진) | 1회 실패 / 1회 성공 |

`todo-add-complete`는 상향으로 해결됐습니다. `dyn-enable-input`은 8,192에서도
재실행 시 2/2 성공했고 32,768에서도 결과가 갈립니다 — **상한과 무관한 모델
판단의 플레이키성**입니다. 상한 상향으로 모든 실패가 사라지지는 않습니다.

> 비용은 올라갑니다. `todo-add-complete`가 \$0.0025 → \$0.0130이 됐습니다.
> 소진으로 스텝 작업이 통째로 버려지는 것보다는 낫지만, 폭주 스텝에서는
> 그만큼 더 지불합니다.

#### 기각된 대응: 소진 후 재시도 (WS-13)

**소진된 뒤 더 큰 값으로 재호출하는 방식**은 기각했습니다. 처음부터 넉넉히
주는 것(위 32,768)과는 다른 이야기입니다.

| | 재시도 없음 | 3배 재시도 |
| :--- | ---: | ---: |
| 소요 | 약 60초 | **1,067초** |
| 비용 | \$0.0025 | **\$0.0089** |
| 결과 | 실패 | 실패 |

같은 프롬프트를 두 번 태우면 모델이 더 긴 사고를 이어갈 뿐 결론에
도달하지 못했습니다. 재호출 대신 **처음부터 넉넉한 상한**을 주고, 그래도
소진되면 모델 교체로 대응합니다.

> 실패한 대응도 기록합니다. 같은 증상을 다시 만났을 때 이미 검증된
> 막다른 길을 반복하지 않기 위해서입니다.

#### silent_win 3건 조사 (WS-18)

31태스크 통합 측정에서 나온 `silent_wins` 3건을 조사했습니다. 셋이
서로 다른 원인이었고, **한 건은 고칠 수 없다는 결론**에 도달했습니다.

| 태스크 | 원인 | 조치 |
| :--- | :--- | :--- |
| `todo-add-complete` | 시간 상한 초과로 강제 종료 | 정상 동작 (WS-17 의도) |
| `dyn-enable-input` | LLM이 빈 응답 반환 | 진단 메시지 개선 |
| `melon-chart` | finish 가드가 과잉 차단 | **수정 불가 — 아래 참조** |

##### melon-chart: 실패의 종류를 보면 구분된다 (재분석으로 뒤집힘)

첫 클릭으로 이미 목표를 달성했는데, LLM이 확인차 같은 요소를 한 번
더 클릭해 실패했고, 그 실패 때문에 완료 선언이 막혔습니다.

1차 분석은 "구분 불가"였습니다 — 트레이스 모양(OK -> FAIL -> finish)이
원조 사례(hn-comments)와 동일했기 때문입니다. 그러나 재분석에서
**실패의 종류**가 다르다는 것이 드러났습니다.

```
막아야 했던 것     click -> FAIL E_TIMEOUT            (효과 실패)
막지 말아야 한 것  click -> FAIL E_ELEMENT_NOT_FOUND  (구식 참조, 2건 전부)
```

기록된 전 아티팩트에서 "실패 직후 finish"는 2건(melon-chart,
naver-search)뿐이고, 둘 다 **같은 요소에 성공 -> 같은 요소에
NOT_FOUND** 패턴이었습니다. 직전 성공이 페이지를 이동시켜 옛 참조가
낡은 것 — 실패가 성공의 부산물입니다.

**가드 정밀화 (WS-18)** 두 가지:

1. **무해한 실패는 가드를 발동하지 않는다.** 구식 참조 실패
   (`ELEMENT_NOT_FOUND`/`TOCTOU_MISMATCH`)가 직전에 성공한 바로 그
   요소에 대해 났다면, 액션이 시작조차 못 한 것이므로 목표 미달성의
   증거가 아닙니다. `E_TIMEOUT` 등 효과 실패는 여전히 발동합니다.
2. **재확인 후의 재선언은 수용한다.** 1차 거부는 새 관찰을 강제하는
   장치입니다. 새 관찰을 받고도 완료를 주장하면 그 판단을 존중하되
   `terminal_reason`에 흔적을 남깁니다. 최종 판정은 독립 검증의
   몫입니다.

검증(실측): commercial 6/6, hard 5/5에서 **silent_wins 0,
false_claims 0**. 사보타주 4종(무해 판정 제거 / 같은 요소 조건 제거 /
가드 전체 제거 / 상태 리셋 제거) 전부 탐지.

> **교훈**: "구분 불가" 결론은 트레이스의 **모양**만 본 것이었습니다.
> 실패의 **의미론**(어떤 오류 코드로, 어떤 요소에 대해)을 보면
> 갈렸습니다. 동일해 보이는 패턴도 한 계층 아래를 봐야 합니다.

##### 빈 LLM 응답을 파싱 오류로 오인

`dyn-enable-input`의 로그는 이랬습니다.

```
JSON 파싱 실패: Expecting value: line 1 column 1 (char 0).
본문 앞부분: ''
```

모델이 잘못된 JSON을 냈다고 오해하게 됩니다. 실제로는 reasoning
계열 모델이 `max_tokens`를 사고 과정에만 쓰고 **본문을 내지
못한** 것이었습니다. `finish_reason`으로 구분해 원인을 알립니다.

#### 실행 시간 상한 미강제 (WS-17)

31태스크 통합 측정 중 `internet-checkbox-both`에서 **13분간 정지**해
측정 전체가 막혔습니다. CPU 0.1%, 로그 갱신 없음 — 네트워크 대기로
멈춘 상태였습니다.

계약에 `MAX_WALL_CLOCK_SECONDS = 600`이 정의돼 있고 PRD에도
"태스크당 최대 Wall-Clock Time 10분"이 명시돼 있는데, **루프가 이를
읽지 않았습니다.** `elapsed_s`를 기록만 하고 상한으로 쓰지 않았습니다.

**스텝 수와 예산만으로는 못 막습니다.** 한 스텝 안에서 멈추면 스텝
카운터가 올라가지 않아 `max_steps`에 영원히 도달하지 못합니다.

두 계층에 넣었습니다.

| 계층 | 상한 | 역할 |
| :--- | :--- | :--- |
| `AgentLoop.run` | 600초 | 정상 종료 경로 (terminal_reason 기록) |
| `agent_eval` | 660초 | 최후 방어선 (`asyncio.wait_for`) |

루프 상한이 먼저 걸리도록 여유를 뒀습니다. 루프가 한 스텝 안에서
멈추면 루프 상한은 무력하므로 하네스 외곽이 필요합니다.

#### record 키 누락으로 측정 무효화 (WS-17)

위 타임아웃을 넣자마자 다음 측정이 `exit 2`로 끝났습니다.

```
{"metric": "agent_completion_rate", "value": 0.0,
 "error": "측정 실패: 'steps'"}
```

타임아웃 경로에서 `record`에 `steps`, `usd` 등을 채우지 않고 반환해
출력부가 KeyError를 냈습니다. **20태스크를 돌린 결과가 통째로
버려졌습니다.**

같은 결함이 예외 경로에도 있었습니다(원래부터).

> **원칙**: 조기 반환 경로는 정상 경로와 **같은 키 집합**을 채워야
> 합니다. 부분적으로만 채우면 소비자가 터집니다. AST로 세 경로의
> 키를 대조하는 테스트를 추가했습니다.

#### CJK 라벨 품질 판정 (WS-16)

상용 사이트를 태스크셋에 넣으면서 발견한 결함입니다. 네이버 뉴스의
섹션 메뉴는 같은 줄에 나란히 있는데 순위가 극단적으로 갈렸습니다.

```
'정치'      (2자, 한글)  name_quality 0.30  ->  417위
'생활/문화'  (5자)        name_quality 1.00  ->   15위
```

`_name_quality`가 2자 이하를 '식별력 부족'으로 감점하는데, 이 기준은
**라틴 문자를 전제한 것**입니다. `ok`, `go`는 애매하지만 한글 2자는
완전한 단어입니다.

CJK 문자가 포함되면 기준을 1자로 낮췄습니다.

```
수정 후   '정치' 17위, '경제' 18위, '사회' 19위, '세계' 20위
```

한국어·중국어·일본어 사이트 전반에 영향을 주는 결함이었고,
**영문 샌드박스만 돌렸다면 발견할 수 없었습니다.**

> **교훈**: 문자 체계를 전제한 규칙(길이, 단어 분리, 대소문자)은
> 다른 언어권에서 조용히 실패합니다. 태스크셋의 언어 다양성이
> 곧 검증 범위입니다.

#### commercial 티어와 봇 차단 (WS-16)

실제 상용 서비스는 한글 UI, 광고·배너, 수백 개의 링크, 잦은 DOM
개편이라는 조건을 갖습니다. 6개 태스크를 추가했습니다.

**사전 조사에서 제외한 사이트**가 있습니다.

| 사이트 | 상태 |
| :--- | :--- |
| 쿠팡 | 403 Access Denied (Akamai 엣지, UA 변경 무효) |
| 구글 검색 | `/sorry/index` CAPTCHA 리다이렉트 |

넣으면 100% 실패하는데 원인이 우리 런타임이 아니므로 완수율 지표를
오염시킵니다. 태스크 설계 시 반드시 실제로 열어보고 확인하십시오.

`commercial` 티어는 **관측용**입니다. DOM 개편으로 언제든 깨질 수
있으므로 판정 게이트로 쓰지 않습니다.

실측(6/6 통과):

```
naver-search         5스텝  $0.0019   48초
daum-search          5스텝  $0.0012   46초
naver-news-section   2스텝  $0.0003   18초
melon-chart          4스텝  $0.0008   60초
11st-search          3스텝  $0.0024  230초
gmarket-search       6스텝  $0.0014  114초
```

#### 이름이 겹치는 링크 구분 (WS-15)

실환경 25태스크 평가에서 `hn-comments` 하나가 실패했습니다.

```
목표    첫 번째 기사의 댓글 링크 클릭 (pathname에 /item 포함)
결과    /newcomments (사이트 전체 최신 댓글)
```

해커뉴스에는 이름이 `comments`인 링크가 두 종류입니다.

| 위치 | 이름 | href |
| :--- | :--- | :--- |
| 상단 네비 | `comments` | `newcomments` |
| 기사별 | `78 comments` | `item?id=...` |

**스코어러로는 풀 수 없습니다.** 목표를 모르는 상태에서 둘의 우열을
정할 근거가 없습니다. 실제로 WS-13 이전에는 21위/33위로 둘 다 Top-20
밖이었고, WS-13이 8위/13위로 끌어올려 비로소 선택 가능해졌습니다.

문제는 LLM이 구분할 정보를 못 받는다는 점이었습니다. 관찰 결과에
href가 없었습니다.

**계약 우회 주의** — `ObservedElement`는 동결이라 href 필드를 추가할
수 없습니다. 처음에 name에 힌트를 붙였다가 **Gate 2 Recall이
1.0 → 0.818로 떨어졌습니다.** 골든셋이 이름 정확 일치로 판정하기
때문입니다.

해법은 계층을 나누는 것이었습니다.

```
ElementHandle (내부)  href 보관        <- 계약 아님
ObservedElement (계약) name 원본 유지   <- 골든셋 무영향
render_observation    프롬프트에만 힌트 <- LLM이 구분
```

결과:

```
프롬프트   [@e8] link "comments" -> newcomments
           [@e13] link "79 comments" -> item
계약 모델  name='comments'  (원본)
TOCTOU     정상 통과
```

#### 실패 직후 완료 선언 금지 (WS-15)

같은 태스크에서 두 번째 결함이 드러났습니다.

```
click @e2 -> FAIL E_TIMEOUT
finish    -> OK              <- 실패 직후 완료 선언
```

`false_claims: 1`로 사후에 잡히긴 했지만, 애초에 선언하지 못하게
막는 것이 맞습니다. 실패한 액션은 페이지에 아무 효과도 남기지
않으므로 목표가 달성됐을 리 없습니다.

두 계층에 방어를 넣었습니다.

1. **프롬프트** — "직전 액션이 FAIL이면 목표는 달성되지 않았다"
2. **루프 가드** — 실패 직후 `finish`면 한 번 되돌리고, 계속 우기면
   종료하되 `completed=False`로 집계

> **원칙**: 자기 보고가 틀린 것을 사후에 잡는 것보다, 애초에 잘못된
> 보고를 못 하게 막는 편이 낫습니다. 독립 검증은 최후 방어선이지
> 유일한 방어선이 아닙니다.

#### 게이트 미탐 사례: SDK 바인딩 우회 (WS-14)

외부 사용자 테스트에서 `agent-browser serve`가 실행되지 않는다는 보고를
받았습니다. Claude Desktop 연동 경로 전체가 막힌 상태였습니다.

```
AttributeError: 'Server' object has no attribute 'list_tools'
```

**게이트는 19/19로 통과하고 있었습니다.** `harness.mcp_smoke`가
`BrowserMCPServer.call_tool`을 직접 호출해 SDK 바인딩 계층을 통째로
우회했기 때문입니다.

| | mcp_smoke | 실사용 |
| :--- | :--- | :--- |
| 호출 경로 | `backend.call_tool()` 직접 | stdio → SDK → backend |
| 검증 범위 | 툴 로직 | 툴 로직 + SDK 바인딩 |
| `create_server()` | 미호출 | 필수 |

이것은 규칙 4(측정 대상이 실제 동작하는 조건을 만들 것) 위반입니다.
`create_server`/`run_stdio`는 tests·harness 어디에서도 호출되지
않았습니다.

**대응**: `harness.mcp_binding` 신설. 실제 `ClientSession`으로
`initialize → tools/list → tools/call` 왕복을 검증합니다. SDK를
우회하지 않습니다.

> **교훈**: 사용자가 실제로 통과하는 경로와 하네스가 통과하는 경로가
> 다르면, 지표가 만점이어도 제품은 동작하지 않습니다. 어댑터·바인딩
> 계층은 반드시 바깥에서 안으로 호출해 검증하십시오.

#### SDK 메이저 호환 (WS-14)

MCP SDK는 1.x와 2.x의 API가 다릅니다.

| | mcp 1.x | mcp 2.x |
| :--- | :--- | :--- |
| 등록 방식 | `@server.list_tools()` 데코레이터 | 생성자 `on_list_tools=` |
| 스키마 필드 | `inputSchema` | `input_schema` |

버전 문자열로 분기하면 프리릴리스나 포크에서 어긋납니다. 실제 속성과
필드를 조회해 맞추십시오.

```python
field = "input_schema" if "input_schema" in Tool.model_fields else "inputSchema"
if hasattr(Server("__probe__"), "list_tools"):  # 1.x
```

#### 프레임 전환 (WS-12)

`switch_frame`은 프레임을 찾아 `snapshot_epoch`만 올리고 **활성 컨텍스트를
바꾸지 않는 결함**이 있었습니다. 전환은 성공으로 보고되는데 이후 관찰이
계속 메인 문서를 봅니다.

```
switch_frame: success=True, epoch 0 -> 1
전환 후 관찰: 19개 (전환 전과 동일)   <- 메인 문서
```

`DispatchContext.page`를 실제로 교체하고 `root_page`에 원본을 보존하도록
고쳤습니다. 루프도 매 스텝 디스패처의 활성 컨텍스트를 따라갑니다.
`{"to_main": True}`로 메인 복귀가 가능합니다.

> 관찰은 상호작용 요소만 수집하므로, 텍스트만 있는 프레임은 전환해도
> 0개가 정상입니다. 프레임 태스크는 조작 가능한 요소가 있는 페이지로
> 설계하십시오.

#### shadow DOM 요소 처리 (WS-11)

Playwright의 CSS 엔진은 shadow 경계를 자동 관통합니다. 따라서 shadow 내부에서
고유한 CSS 경로도 문서 전체에서는 여러 요소에 매칭됩니다. 실측에서 MDN의
shadow 내부 `button` 경로가 **18개 요소에 매칭**되어 항상 첫 번째
(`display:none`) 요소가 잡혔습니다.

세 가지를 함께 고쳐야 동작합니다.

| 계층 | 문제 | 해결 |
| :--- | :--- | :--- |
| 관찰 | `depth<6`에서 잘린 `button` 조각 생성 | 고유성을 확인하며 경로 확장 |
| 실행 | CSS로 shadow 요소 지목 불가 | `is_shadow`면 role+name 로케이터 |
| 검증 | `document.querySelector`가 shadow 미탐색 | `deepQuery`로 shadow 재귀 탐색 |

> 검증 계층을 빠뜨리면 입력은 성공하는데 값을 `None`으로 읽어 정상 액션을
> Silent Failure로 오판합니다. 관찰이 shadow를 수집한다면 검증도 같은 범위를
> 보아야 합니다.

#### Mock 대비

| | Mock (Gate 3-B) | 실환경 |
| :--- | :--- | :--- |
| 태스크 완수율 | 100% | **94.4%** |
| 참조 에이전트 | 결정론적 정책 (LLM 미사용) | 실제 LLM |
| 후보 요소 수 | 최대 69개 | 최대 1,051개 |
| 최장 태스크 | 4스텝 | 7스텝 |

`harness.agent_eval`도 **판정 게이트가 아닙니다.** 외부 사이트·네트워크·LLM
과금에 의존하므로 CI에 등록하지 않습니다. 사이트 개편으로 태스크가 깨질 수
있으므로, 실패 시 먼저 태스크 정의의 유효성을 확인하십시오.

---

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

### 3) 게이트 실패 4종 분류 및 대응 프로토콜 (Failure Protocols)
1. **[유형 A: 구현 결함]** 단위 테스트 또는 하네스 임계값 미달:
   - 해당 워크스트림 에이전트가 코드를 수정하고 로컬 테스트를 재수행합니다.
   - **CI 오케스트레이터가 서브태스크당 빌드 실패 횟수를 자동 카운트하며, 10회 연속 실패 시 프로세스를 강제 종료하고 사람 감독자에게 에스컬레이션**합니다.
2. **[유형 B: 하네스/테스트 자체 결함]** Mock 사이트 결함 또는 잘못된 골든셋:
   - 에이전트가 이슈를 제안하되, **사람 감독자가 확정**한 후 WS-6 브랜치에서 하네스를 수정합니다.
3. **[유형 C: 비현실적 KPI 목표 충돌]**:
   - 10회 이상 수정 후에도 아깝게 미달할 경우 임의로 임계값을 낮추지 않고, **사람 감독자가 확정**하여 목표 조정 또는 아키텍처 재검토를 결정합니다.
4. **[유형 D: 게이트 미탐 (False Pass)]** 게이트는 통과했으나 실제로는 기능이 파손된 경우:
   - **가장 위험한 유형입니다.** 실패가 아니라 통과로 나타나므로 아무도 알아채지 못한 채 다음 Stage로 진행됩니다.
   - 탐지 방법: §5 "하네스 설계 필수 규칙" 규칙 5의 **사보타주 검증**.
   - 발견 시 대응 순서:
     1. 해당 하네스의 **표본·시나리오를 보강**하여 사보타주가 탐지되도록 만듭니다.
     2. 보강 후 재측정에서 실제 결함이 드러나면 **유형 A로 전환**하여 구현을 수정합니다.
     3. 회귀 방지 테스트를 추가해 커버리지가 다시 축소되지 않도록 고정합니다.
   - **금지 사항**: 미탐이 확인된 하네스의 수치를 근거로 게이트를 승인하는 행위. 해당 Stage 승인은 하네스 보강 완료 후 재측정 결과로만 판단합니다.

> **유형 D 판별 신호**: 다음 중 하나라도 해당하면 미탐을 의심하고 사보타주 검증을 수행하십시오.
> - 지표가 처음부터 **만점(1.0)**이며 실패 사례가 한 건도 없다
> - `strategy_breakdown`, `covered_*` 같은 분포가 **한쪽에 완전히 쏠려** 있다
> - 측정 대상 기능이 **동작할 조건이 성립하지 않는다** (예: 후보 수 < Top-N)

### 4) 자동화된 회귀 방지 CI (Regression Guard)
* PR 머지 시 단위 테스트 및 Recall@20 벤치마크를 자동 측정하며, **절대 KPI 임계값 미달 시 또는 직전 커밋 대비 정상 편차 2.0%p 초과 하락 시 머지를 자동 차단**합니다.
