# Agent-Browser 코드 리뷰 스타일 가이드

이 저장소는 **AI 에이전트 전용 헤드리스 브라우징 런타임**이며, 구현은 자율 AI 코딩 에이전트가 수행하고 사람은 게이트 승인자로 참여합니다.
리뷰어는 아래 규칙 위반을 **최우선으로 지적**해 주십시오. 기준 사양서는 저장소 루트의 `PRD.md`, 개발 지침은 `src/AGENTS.md`입니다.

## 리뷰 언어

- **모든 리뷰 코멘트는 한국어로 작성**합니다.
- 칭찬이나 요약보다 **결함·위험·수정 필요 지점**을 우선 지적합니다.
- 지적에는 근거(파일/행 번호, 스펙 섹션 번호)를 함께 제시합니다.

## 1. 배타적 디렉터리 소유권 (최우선)

각 워크스트림은 지정된 디렉터리만 수정할 수 있습니다. **소유권을 넘는 변경은 즉시 지적**하십시오.

| 워크스트림 | 소유 디렉터리 |
| :--- | :--- |
| WS-0 | `src/contracts/`, `tests/contracts/` |
| WS-1 | `src/browser/`, `tests/browser/` |
| WS-2 | `src/perception/`, `tests/perception/` |
| WS-3 | `src/actions/`, `tests/actions/` |
| WS-4 | `src/security/`, `tests/security/` |
| WS-5 | `src/interface/`, `tests/interface/` |
| WS-6 | `src/harness/`, `tests/harness/` |

- `pyproject.toml`, `uv.lock`, `src/cli.py`, `tests/conftest.py`, `.github/`, `check_docs.py`는 **사람/오케스트레이터 소유**입니다. 에이전트 PR이 이들을 수정하면 반드시 지적하십시오.
- `src/contracts/`는 Stage 0 동결 이후 **읽기 전용**입니다. 변경 시 "계약 동결 위반"으로 지적하십시오.

## 2. 타입 안전성

- 모든 공개 함수·메서드에 타입 힌트가 있어야 합니다.
- **`Any` 사용 금지.** 단, `contracts/models.py`의 `ActionResult.data: Dict[str, Any]`처럼 계약에 명시된 경우만 예외입니다.
- 모듈 간 호출은 `contracts/protocols.py`의 `Protocol` 시그니처를 따라야 합니다. 시그니처가 어긋나면 지적하십시오.
- Pydantic V2 문법을 사용합니다 (`model_validator`, `field_validator`). V1 문법(`@validator`, `.dict()`, `.parse_obj()`)은 지적 대상입니다.

## 3. 에러 처리

- 임의 문자열 예외를 던지지 말고 `contracts`의 `ErrorCode` Enum을 `ActionResult.error_code`에 바인딩해야 합니다.
- `except Exception: pass` 같은 무음 예외 처리는 반드시 지적하십시오.
- 에러 메시지에 셀렉터·URL·토큰 값이 그대로 노출되지 않는지 확인하십시오.

## 4. 비동기 안전성

- CPU 바운드 연산(Levenshtein 거리, 대량 DOM 정렬)은 `asyncio.to_thread`로 오프로드해야 합니다. 이벤트 루프를 블로킹하면 지적하십시오.
- 비동기 함수 내 `time.sleep()`, 동기 I/O(`requests`, `open()` 대용량 읽기)는 지적 대상입니다.
- 단일 `BrowserContext` 내 페이지 조작 시 `asyncio.Lock` 준수 여부를 확인하십시오.
- Playwright API는 스레드 안전하지 않습니다. 멀티스레드에서 공유하면 지적하십시오.

## 5. 보안 (엄격)

- **비밀값 하드코딩 절대 금지**: API 키, 패스워드, 토큰, `.enc` 경로. 테스트 코드와 로그 문자열도 포함합니다.
- 세션 파일은 `0600` 권한이어야 합니다.
- `page.route()` Egress 제어를 우회하는 네트워크 호출이 있는지 확인하십시오.
- 로그·트레이스에 PII, `Authorization` 헤더, `Set-Cookie`, URL 쿼리스트링 토큰이 마스킹 없이 남으면 지적하십시오.
- 웹 페이지에서 읽은 텍스트를 신뢰해 분기하는 코드(프롬프트 인젝션 경로)를 발견하면 지적하십시오.

## 6. 테스트

- 새 기능에는 대응 테스트가 있어야 합니다. 없으면 지적하십시오.
- 테스트가 실제 외부 네트워크에 접근하면 지적하십시오. Mock 사이트(`harness/`)를 사용해야 합니다.
- `sleep()` 기반 대기 대신 조건 기반 대기(`wait_for`)를 사용해야 합니다. 플레이키율 KPI(≤2%)에 직결됩니다.

## 7. 하네스 출력 규약 (`src/harness/`)

`harness/` 모듈은 다음을 반드시 지켜야 합니다.

- 임계값을 하드코딩하지 않고 `contracts/thresholds.py`에서 읽습니다.
- stdout에 단일 JSON 라인을 출력합니다: `{"metric": str, "value": float, "threshold": float, "passed": bool, "samples": int}`
- **임계값 미달 시 exit code 1을 반환**합니다. 항상 0을 반환하는 하네스는 게이트를 무력화하므로 반드시 지적하십시오.

## 8. 문서 무결성

- 제어문자 `0x0D`(CR), `0x07`(BEL) 삽입 금지.
- 마크다운에 미처리 LaTeX 수식(`$\rightarrow$`, `$\approx$` 등) 금지. 유니코드 기호(→, ≈, ×)를 사용합니다.
- 게이트 검증 명령어는 `python -c` 인라인으로 2줄 이상 작성하지 않습니다. `scripts/` 아래 파일로 분리해야 합니다.

## 리뷰 시 우선순위

1. 보안 결함, 비밀값 노출
2. 소유권 위반, 계약 동결 위반
3. 게이트를 무력화하는 변경(항상 통과하는 검증, 빈 시퀀스에 대한 `all()` 등 공허한 참)
4. 비동기 블로킹, 타입 안전성 위반
5. 테스트 누락, 스타일

## 하네스 미탐 (False Pass) 검토 — 최우선 지적 대상

`src/harness/**` 변경 시 다음을 반드시 확인하고, 위반이면 **P0으로 지적**하십시오.
게이트가 만점을 보고했으나 실제로는 기능이 파손돼 있어도 통과하던 사고가 4건 발생했습니다.

1. **커버리지 보고 누락**: 성공률만 출력하고 측정 범위(`*_covered`, `stages_covered`,
   `pruning_effective_pages` 등)를 보고하지 않는 하네스.
2. **미달 시 무효화 부재**: 필수 커버리지가 미달인데 `emit_error`(exit 2)로 측정을
   무효화하지 않고 그대로 성공률을 보고하는 코드.
3. **표본 편향**: 정상 케이스와 명백한 실패 케이스만 있고 **경계에 인접한 우회 케이스**가
   없는 보안 하네스 (예: allowlist 검증에 `evil-example.com` 류가 없음).
4. **측정 조건 미성립**: 측정 대상 기능이 발동하지 않는 입력만 모은 경우
   (예: Top-N 프루닝을 재는데 후보가 N보다 적은 페이지만 사용).
5. **경고로 처리**: 시나리오가 의도한 경로가 아닌 다른 경로로 해결됐는데 경고만 출력하고
   통과시키는 코드. 해당 경로는 미검증 상태이므로 실패시켜야 합니다.
6. **임계값 하향**: 게이트 실패에 대응해 `contracts/thresholds.py` 값이나 하네스 임계값을
   낮추는 변경. 구현 수정이 원칙이며, 임계값 변경은 사람 감독자 승인 사항입니다.

또한 하네스를 수정한 PR인데 **사보타주 검증 결과가 본문에 없으면** 그 사실을 지적하십시오.
