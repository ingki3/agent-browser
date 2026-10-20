# 기능 — PDF 읽기, YouTube 영상 검색, 동작 캐시

[← 문서 목차](README.md)

## PDF 읽기

`browser_navigate({"url": "https://example.com/report.pdf"})`는 PDF를 읽어 성공 결과의
`data.document`에 `{kind: "pdf", url, pages_total, pages: [{page, text}], chars, truncated,
text_layer}`를 돌려줍니다. 확장자가 없어도 PDF 응답이나 PDF 다운로드를 인식합니다.
PDF navigate 1회는 서버 GET 2회(브라우저 시작 + 재요청)와 robots.txt 요청을 발생시킵니다 — 일회용·서명·과금 링크는 두 번째 요청이 실패할 수 있습니다.
쪽 번호는 1부터 시작하며, 나머지 쪽은 `browser_extract({"selector": "pdf:pages=3-5"})`로 읽습니다.
쪽 범위는 현재 탭의 마지막 PDF에 적용되며 새 문서로 이동하면 해제됩니다.

한 번에 파일 25MiB, 처리 50쪽, 텍스트 20,000자까지 앞쪽부터 읽습니다. 상한·파싱 시간 초과·
손상·암호화는 성공 결과의 `document.reason`과 `data.hint`에 설명하며, 완료한 쪽은 보존합니다.
쪽 범위를 잘못 지정하거나 egress가 차단하면 기존 오류 코드로 실패합니다. 제어·bidi·zero-width
문자는 제거하고 주입 문구에는 기존 `data.injection_suspected` 신호를 붙입니다.

끼워 넣은 PDF와 PDF 링크는 `browser_observe_page`의 `data.documents`에 절대 HTTP(S) URL로
최대 5개 표시합니다. PDF 링크 클릭에는 `browser_navigate`로 읽을 URL을 안내합니다.
글자 층이 없는 스캔 PDF는 `text_layer: false`와 스크린샷 확인 안내를 돌려주며 OCR은 수행하지 않습니다.

브라우저 다운로드는 즉시 취소·삭제하고 같은 egress 프록시·가드·UA와 해당 주소에 적용되는
컨텍스트 쿠키로 메모리에 스트리밍합니다. 추가 가져오기는 같은 출처의 리다이렉트만 5회까지
허용합니다. 파일을 저장하거나 PDF 본문을 캐시하지 않으므로 쪽 범위마다 다시 가져옵니다.
파서는 별도 프로세스에서 5초 안에 중단하고, 압축 해제 8MiB·페이지 트리·RSS 감시로 자원을 제한합니다.
PDF 읽기는 텍스트 문서 결과이며, headless 탭에는 PDF 뷰어가 표시되지 않습니다. 스캔 PDF의
스크린샷은 PDF를 표시하는 사람의 브라우저에서 확인해야 합니다.

## YouTube 영상 검색 (`browser_youtube_search`)

`youtube.com/results`는 robots `User-agent: *` 금지라 부르는 에이전트가 열지 않습니다. 영상 정보는 영상 페이지를 열면 되고
자막은 이 도구의 **범위 밖**(부르는 에이전트의 별도 스킬)입니다. 비어 있던 **검색**만 공식 YouTube Data API v3로 채웁니다.

- 입력: `query`(필수, 200자 이하), `max_results`(1~25, 기본 10), 선택 `order`(`relevance`|`date`|`viewCount`),
  `published_after`(`YYYY-MM-DD` 또는 ISO 시각), `region_code`(`KR`), `relevance_language`(`ko`).
- 동작: `search.list`(part=snippet, type=video) 1회 → `videos.list`(part=contentDetails,statistics, id 묶음) 1회로 길이·조회수 보강.
  응답이 요청보다 많은 항목을 돌려줘도 `max_results`개까지만 씁니다. 보강이 실패해도(보강 단계의 할당량 초과·5xx·해석 불가,
  거대하거나 ASCII가 아닌 숫자 값 포함) 검색 결과는 `success=true`로 돌려주고 해당 필드만 `null` + `data.youtube.enrichment.reason`을 붙입니다.
- 결과: `data.videos[] = {video_id, url, title, channel_title, channel_id, published_at, duration_s, view_count, description}`
  (`description`은 300자까지). 제목·설명·채널 이름은 업로더가 쓴 글이라 페이지 텍스트와 같게 다룹니다 — 제어·bidi 문자를
  보이는 표기로 바꾸고, 지시처럼 보이는 문구에는 `data.injection_suspected` 신호를 붙입니다(차단하지 않음).
  `data.youtube = {source: "youtube_data_api_v3", returned, total_results_estimate, quota, note}` — API라 robots 신호는 없습니다.
  표시 값은 한 방향으로 만듭니다: 표시 정규화 → 키 가림(길이가 늘지 않음: `key=x`→`key=*`, 키→`***`) → 필드 상한 절단 1회
  (넘치면 `…`; 결합 문자·변이 선택자·피부색·ZWJ·국기 쌍은 쪼개지 않는 **근사 규칙**(stdlib `unicodedata`), 가림 표식은 통째로 뺌)
  → 전체 크기 상한은 뒤 영상부터 영상 단위로 빼고 `returned`·`truncated`를 맞춥니다(한 영상의 필드 때문에 다른 영상이 사라지지 않음).
- 네트워크: 다른 서버 요청과 같은 Egress 가드·검증 프록시를 지납니다(브라우저를 띄우지 않은 서버는 같은 운영자 정책으로
  이 호출만의 프록시를 씀, `--allow-domain`을 쓰면 `www.googleapis.com`도 허용해야 함). 대상 호스트는 `www.googleapis.com`
  고정, 리다이렉트 따르지 않음, 응답 1MB·요청 10초 상한, **재시도하지 않음**(429·5xx는 `data.youtube.reason` 신호만).
  압축은 요청하지 않고(`Accept-Encoding: identity`) 압축 해제도 하지 않습니다 — 압축된 응답은 `invalid_response`입니다.
  깊게 중첩된 JSON·형식이 다른 `items`도 `invalid_response`, 범위 밖 날짜·보낼 수 없는 문자(단독 surrogate)는
  `invalid_arguments`, 그 밖의 예상하지 못한 오류는 원문 없이 `reason: "internal_error"`입니다.
  승인·레시피 대상이 아닌 읽기 전용 조회입니다.
- 할당량: 무료 10,000단위/일(search.list 100, videos.list 1 → 검색 약 99회/일). 서버 프로세스 단위(`--profile`이면
  프로필 폴더 `youtube_quota.json`, 0600)로 오늘(태평양 시간 자정 리셋) 쓴 추정치를
  `data.youtube.quota = {used_estimate, limit, remaining_estimate, reset_at}`로 알립니다. 같은 키를 다른 곳에서도 쓰면
  실제보다 적게 잡힙니다. 장부 파일은 링크를 따르지 않고 무작위 임시 파일로 바꿔 쓰며, 읽을 수 없으면(손상·링크·거대)
  0부터 다시 세고 `quota.file_unreadable=true`를 붙입니다. 차감은 보수적입니다 — 요청이 API에 닿았을 수 있으면(시간 초과·
  상류 응답 형식 불량 포함) 차감하고, Egress 차단과 검증 프록시의 업스트림 **접속** 실패(HTTP 경로)만 0입니다
  (HTTPS에서 프록시가 접속을 거부하면 차단과 구별할 수 없어 `egress_blocked`·0으로 셉니다).
  API가 `quotaExceeded`/`dailyLimitExceeded`를 주면 남은 추정치를 0으로 두고 `data.youtube.quota_exceeded=true`를 붙입니다 —
  **검색 단계**에서면 `success=false`(`reason: "quota_exceeded"`), **보강 단계**에서면 검색 결과를 유지한 `success=true` +
  `data.youtube.enrichment.reason: "quota_exceeded"`입니다.
  키 문제(`keyInvalid` 등)는 `reason: "key_invalid"` + `api_reason`(값 없음)입니다.
- **키가 없으면** 도구는 목록에 보이고, 호출하면 `success=false` + `data.youtube.disabled=true` + 등록 방법 한 줄(`data.youtube.hint`)을 돌려줍니다.

키 발급(무료, 약 5분):

1. [Google Cloud 콘솔](https://console.cloud.google.com/)에서 프로젝트를 만들거나 고릅니다.
2. "API 및 서비스 → 라이브러리"에서 **YouTube Data API v3**를 사용 설정합니다.
3. "사용자 인증 정보 → 사용자 인증 정보 만들기 → API 키"로 키를 만듭니다.
4. 키 제한: "API 제한사항 → 키 제한 → YouTube Data API v3"만 고릅니다.
5. serve 를 띄우는 환경에 등록합니다 — 환경변수 `AGENT_BROWSER_YOUTUBE_API_KEY`(우선) 또는 OS 키체인
   `uv run keyring set agent-browser-youtube api_key`(service=`agent-browser-youtube`, account=`api_key`).

키는 요청 헤더(`X-goog-api-key`)로만 보내고 URL에 싣지 않으며, 응답·오류 문구·로그에서 가립니다. 대화나 설정 파일에 키를 붙여 넣지 마십시오.
등록된 값의 앞뒤 공백·탭은 지우지만 개행·CR이 들어 있으면(끝이라도) 지우지 않고 `reason: "key_malformed"`로 거부합니다(요청 0회).

## 동작 캐시 — 레시피 (`browser_recipe`)

같은 사이트에서 같은 일을 다시 할 때, 부르는 에이전트가 단계마다 관찰→판단(LLM 약 4.5초/단계)을 하지 않고 **검증된 동작 묶음을 도구 호출 한 번으로 재생**합니다. 페이지 구조가 바뀔 수 있다는 전제로, 어긋나면 **누르지 않고 멈춥니다**. 기본으로 켜져 있고 `serve --no-recipes`로 끕니다(끄면 기록·도구·관찰 후보·안내가 모두 없음).

- **통과한 흐름은 자동 저장됩니다**(WS-38b). 서버는 세션 중 *성공하고 사후 확인을 통과한* 동작(navigate·click·type_text·select_option·check_box·press_key)만 기록하고, 이것을 **구간**(흐름)으로 나눕니다. 새 구간은 `navigate`(그 단계가 첫 단계)·출처가 바뀜·끊김(실패·자가 치유된 단계·승인 증표로 실행한 단계·사람 조작·프레임 안·shadow 대상·selector/좌표 클릭·탭 전환·오류 페이지·재생)·20단계 초과에서 시작합니다. 관찰·추출·스크롤 등은 넣지도 끊지도 않습니다.
  - 구간에 통과 단계가 **2개 이상** 쌓일 때마다 그 구간 전체를 레시피 하나로 저장(upsert)합니다 — 구간이 자라면 같은 레시피를 갱신(구간당 1개, 가장 긴 판), 같은 구조(출처·첫 페이지·단계별 동작·인자 틀·페이지 패턴·대상 자리)의 레시피가 있으면 거기 합칩니다. 같은 과제를 검색어만 바꿔 반복해도 레시피는 1개입니다. 기존 레시피의 앞부분과 같은 구조인 동안은 새로 만들지 않고 기다렸다가, 구간이 거기서 끝나면 그때 저장합니다.
  - 입력 글자는 전부 자동 `params` 가 됩니다. 이름은 입력 칸의 이름/라벨(예: `검색어`), 없거나 겹치면 `text1`, `text2`… 이동 URL 의 경로·쿼리 값에 같은 글자가 있으면 그 param 으로 바뀝니다. 입력 글자가 저장될 문자열(대상 이름·URL 패턴)에 남으면 그 구간은 저장하지 않습니다.
  - 비밀번호 칸 입력·자격증명 치환 단계에서 구간을 끊고, 그 단계와 이후(다음 구간 전까지)는 저장하지 않습니다. 민감 쿼리 키(token·session·email 등)가 있는 이동으로 시작한 구간도 저장하지 않습니다 — 둘 다 에이전트에게 오류를 띄우지 않고 조용히 넘어갑니다.
  - 이름은 `자동: <첫 페이지 경로> <단계 요약>`(40자, 예: `자동: /list 검색어 입력→클릭×2`), 레시피에 `auto: true`. 구간이 **처음** 저장된 단계의 결과에만 `data.recipe_saved = {id, steps, params}` 가 한 번 붙습니다.
- **save 는 이름·params 를 바꾸고 싶을 때만** — 최근 통과 단계(최근 30개)를 골라 저장합니다. 같은 구조의 자동 레시피가 있으면 그 레시피를 이 이름·params 로 덮어쓰고 `auto: false` 가 됩니다(이후 자동 저장은 그 이름·params 를 바꾸지 않음).
  `browser_recipe {op:"save", name:"노트북 검색 첫 결과", last_n:3, params:{"query":"노트북"}, pins:{"2":"identity"}}`
- **대상은 '그 요소'가 아니라 '자리'로** 저장합니다. 목록(같은 틀 형제 ≥3) 안이면 자동 **slot**(목록 서명 + 항목 틀 + 순번 — "기사 목록의 첫 번째 제목 링크"), 밖이면 **ui**(역할군 + 이름 정확히 같음 + 랜드마크 + 위치 450px 가드). `pins`로 단계별 **identity**(쿼리 id·요소 id 정확히 같음 — "바로 그 상품")로 바꿀 수 있고, 광고·가격·순위 대상은 identity 를 거부합니다.
- **재생(run)은 에이전트가 고릅니다.** `observe_page` 결과에 `data.recipes = {how, candidates:[{id,name,steps,params,auto,ok}]}`가 있으면(현재 출처 + URL 패턴 + 골격이 맞는 활성 레시피, 성공 재생 수 `ok` 많은 순 → 최근 사용 순, 최대 5개) 단계별로 하기 전에 `browser_recipe {op:"run", id, params:{"검색어":"이어폰"}}` 로 실행합니다. MCP `initialize`의 서버 instructions, 도구 설명, `data.recipes.how`로 쓰는 법을 알립니다(예전 저장 권유 `data.recipe_hint` 는 없어졌습니다).
- **재생도 모든 관문을 그대로 지납니다** — 단계마다 찾은 요소로 새 핸들을 만들어 일반 액션과 같은 경로(HITL·egress·TOCTOU·신원 가드·주입 신호·사후 확인)로 보내며 자가 치유는 끕니다. 결제·제출처럼 승인이 필요한 단계는 매번 새로 승인을 받아야 합니다(승인 증표는 저장하지 않음).
- **멈추는 조건**(응답 `data.recipe.reason`, 멈춘 단계 `stopped_at`, 현재 관찰 `data.observation` 동봉 — 그 지점부터 평소대로 진행):

| reason | 뜻 |
| :--- | :--- |
| `page_changed` | 출처·URL 패턴·골격(깊이 4, 반복 목록 접음)이 기록과 다름 — A/B 화면이 기록되지 않음 등 |
| `not_ready` | 상호작용 요소가 기록의 50% 미만(최대 2초 기다림) — 덜 로드 |
| `target_not_found` | 같은 틀의 목록·자리·이름이 없음, 또는 틀(모양·역할·href 패턴)이 다름 — 같은 자리 광고 바꿔치기 포함 |
| `target_ambiguous` | 후보가 2개 이상(같은 틀 목록 2개, 같은 이름 버튼 여럿) |
| `expect_mismatch` | 실행 뒤 이동·사후 확인 신호가 기록과 다름 |
| `approval_required` | HITL 이 막음 — 그 단계의 응답(`data.step_result`)을 그대로 돌려줌 |
| `action_failed` | 그 밖의 실패(egress 차단 등, `data.step_result`) |

  연속 3번 멈추면(승인 필요 제외) `disabled` — 다시 기록해 같은 이름으로 save 하면 갱신됩니다(자동 저장은 꺼진 레시피를 다시 켜지 않습니다). 같은 이름·출처·동작 순서로 다른 화면(A/B)에서 다시 save 하면 그 화면 버전이 추가됩니다.
- **저장 위치** — `--profile NAME` 이 있으면 `~/.agent-browser/profiles/serve-NAME/recipes.json`(0600, 임시 파일+rename, 레시피 변경(자동 저장 포함)은 즉시 쓰고 실행 통계는 최대 5초 모아 쓰며 종료 때 마지막 쓰기). 없으면 메모리만(서버 종료 시 소멸). 파일이 깨졌으면 빈 저장소로 시작하고 원본은 `recipes.json.corrupt-…`로 보존합니다. 상한: 200개, 레시피당 20단계, 1MB(넘으면 자동 레시피부터, 그 안에서 오래 안 쓴 것부터 정리 — save 한 레시피는 자동 레시피가 남아 있는 동안 밀리지 않음).
- **저장하지 않는 것** — 쿠키·승인 증표·자격 증명·입력 원문·페이지 본문. 입력 글자는 `params` 자리표시자로만 저장하며, 치환되지 않은 글자가 남거나 비밀번호 칸 입력이면 save 를 거부합니다(자동 저장은 그 구간을 저장하지 않음). 이동 URL 은 params 를 경로 조각·쿼리 값에만 넣고(출처는 못 바꿈), params 로 치환되지 않은 쿼리 값은 버립니다(키만 — save 응답 `data.dropped_query_values` 가 알려 줌). 토큰·세션·이메일 같은 민감 키의 값은 params 로도 저장하지 않습니다. **토큰·이메일·전화번호가 보이는 흐름은 저장하지 않습니다**(WS-38b R1) — 경로에 JWT·base64url 같은 토큰이나 이메일이 실린 이동(인증·재설정 링크), 대상 이름·식별값에 이메일·전화번호가 있는 단계, 입력값이 이동 주소·대상 이름에 원문으로 남는 경우(NFKC·대소문자·퍼센트 인코딩 변형 포함)는 자동 저장하지 않고 save 는 이유와 함께 거부합니다. `select_option` 값도 입력 글자처럼 params 로 저장합니다(자동 저장). 레시피 이름은 80자·제어문자 제거로 살균되고 응답은 주입 신호 경로를 거칩니다.
  - 경로의 퍼센트 디코드 뒤 16자 이상 영문·숫자 혼합 조각과 `;` 경로 매개변수도 거부합니다(영문만·숫자만은 허용). 링크 대상의 `href_pat`·identity `path` 등 모든 저장 URL·패턴과 출처 호스트를 검사하며, 호스트의 16자 이상 영숫자 혼합 또는 `-`/`_` 포함 토큰형 라벨도 거부합니다. 사후 신호는 `:` 앞 종류만 저장합니다(WS-38b R2).
  - 대상 이름·identity 값·CSS 선택자·자동 params 라벨 등 페이지 유래 문자열도 검사합니다. 문자열 속 URL은 경로·호스트·쿼리 키를 보고, 나머지는 낱말마다 같은 토큰 규칙을 적용합니다. URL 조각의 sub-delim·`:`·`@`를 지운 형태도 검사하며 잘못된 포트는 거부합니다(WS-38b R3).
  - 하이픈 슬러그·호스트 라벨은 소문자·숫자·`-`만 있고 각 부분이 12자 이하이며 영문·숫자가 섞인 6자 이상 부분이 없으면 허용합니다(`electronics-accessories`, `samsung-galaxy-s24-ultra`, `my-online-shop-store.test`). 한계(NB-A): 이 예외 밖의 16자 이상 토큰형 파일명·슬러그는 거부될 수 있어 평범한 흐름의 저장 기회를 잃을 수 있습니다.
  - 한계(NB-C): 16자 미만 토큰이나 여러 짧은 경로 조각으로 나뉜 토큰은 이 길이 규칙으로 탐지하지 못합니다.
  - 한계(NB-D): 전화번호 검사는 국내 01x 휴대전화·`+국가번호` 형식에 한정하며 유선(02-…) 등은 탐지하지 못합니다.
- `browser_recipe {op:"list"}`·`{op:"delete", id}`. 측정: `uv run python -m harness.recipe_replay`(Mock 10종 — 콘텐츠 교체·순서 변경·params 치환·save 없이 자동 저장 뒤 새 세션 재생은 성공, A/B 미기록·덜 로드·모호 목록·광고 틀 위조·결제 승인·같은 틀 다른 URL 패턴은 정해진 사유로 멈춤. 하나라도 잘못 누르면 exit 2).
