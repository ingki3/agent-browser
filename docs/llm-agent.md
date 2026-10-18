# 내장 LLM 에이전트 — LLM 연동과 실환경 태스크

[← 문서 목차](README.md)

## LLM 연동 (선택)

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
- `http_status`는 첫 이동 응답, `last_http_status`는 실행 중 마지막 메인 프레임 문서 응답의 상태입니다(브라우저 context 전체 — `target=_blank`·`window.open`으로 연 새 탭의 첫 문서 포함, iframe 문서는 제외, 여러 탭이 동시에 움직이면 마지막으로 온 응답). 첫 화면은 200인데 검색 페이지에서 403으로 막힌 경우를 구분합니다. 단, 에이전트 루프의 차단 판정은 지금 보고 있는 탭의 화면 내용으로 하므로, 루프가 옮겨 가지 않은 새 탭의 차단 화면은 `challenge`에 잡히지 않고 `last_http_status`에만 남습니다. `steps`의 실패 줄에는 오류 메시지 앞 80자를 한 줄로 접어 붙이고, 그 안 `http(s)://` URL의 쿼리·fragment는 `?…`로 가립니다(`type_text`는 입력값이 섞일 수 있어 붙이지 않음).

`--out` 파일은 페이지 본문이 들어가므로 권한 0600으로 씁니다. `--handoff`를 켜면 차단·캡차 화면에서 멈추고, 사람이 창에서 해결한 뒤 터미널에서 Enter를 누르거나 `touch ~/.agent-browser/handoff.done`(`--handoff-file`로 변경)하면 같은 목표로 이어 갑니다(이때도 화면을 다시 확인해, 여전히 막혀 있으면 멈춥니다). 정상 화면을 차단으로 잘못 본 경우에는 터미널에 `f`(또는 `force`)를 입력하고 Enter를 누르거나 `echo force > ~/.agent-browser/handoff.done` 하면 강제로 계속하며, 그 실행 동안 같은 판정은 다시 넘기지 않습니다(결과의 `handoffs[].forced`에 기록). `--user-chrome`은 우리가 띄운 Chrome만 닫습니다(`--keep-open`이면 둡니다). `--human`과 `--user-chrome`은 함께 쓸 수 없고, `--chrome-profile`에 평소 Chrome 프로필 경로를 주면 브라우저를 띄우기 전에 한 줄 오류로 거부합니다(exit 2).

> 막히면 사람에게 넘긴다 — 캡차를 풀거나 차단을 우회하지 않는다.

## 실환경 태스크 실행

```bash
# 난이도별로 골라 실행
uv run python -m harness.agent_eval --task easy
uv run python -m harness.agent_eval --task dynamic

# 전체 25태스크 (약 30분, 약 $0.02)
uv run python -m harness.agent_eval --report artifacts/agent_eval.json
```

공개 사이트(위키백과, 해커뉴스, MDN 등)에 실제로 접속합니다.
