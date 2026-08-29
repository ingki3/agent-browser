# Agent Browser (`agent-browser`)

> **Agent-Native Headless Browsing Engine & Model Context Protocol (MCP) Server**
> 
> Python 3.11+ 비동기(`asyncio`) 런타임과 Playwright CDP를 기반으로 구축된 AI 에이전트 전용 헤드리스 브라우징 인프라입니다.

---

## 🚀 Key Features

* **95% Token-Compressed Perception (`observe_page`)**: 원시 HTML 대신 접근성 트리(AxTree)와 `Prune4Web` Top-20 핵심 인터랙티브 노드를 선별하여 관찰 토큰을 획기적으로 절감(p50 ≤ 2,500 토큰).
* **19 Action Primitives & Self-Healing**: 폼 입력, 탭 제어, Closed Shadow DOM 순회, 그리고 셀렉터 변경 시 자동 복구되는 자가 치유(Self-healing) 액션 사다리 내장.
* **AES-256-GCM Session Vault**: 1회 대화형 로그인 후 Cookies/LocalStorage를 Argon2id + AES-256-GCM 암호화하여 무인 모드(`--mode=unattended`)에서 영구 재사용.
* **RFC-Compliant MCP Server**: Claude Desktop, Cursor 등 외부 에이전트 도구로 즉시 노출.
* **3-Layer Security Guardrail**: 도메인 Allowlist, Egress 데이터 유출 방지, 아웃바운드 PII 정규식 자동 마스킹, 고위험 액션 `ConfirmDialog` 승인 강제.

---

## 🛠️ Quick Start & Bootstrap

```bash
# 1. uv 가상환경 동기화 및 dev 의존성 설치
uv sync --extra dev

# 2. editable 모드로 로컬 패키지 설치 (src-layout 활성화)
uv pip install -e .

# 3. Chromium 브라우저 바이너리 설치
playwright install --with-deps chromium

# 4. 문서 및 스펙 무결성 검증
python check_docs.py
```

---

## 📖 Specifications & Guidelines

* **제품 요구사항 및 기술 사양서**: [PRD.md](PRD.md)
* **AI 에이전트 개발 운영 지침서**: [src/AGENTS.md](src/AGENTS.md)
