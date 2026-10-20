# 사람 인계와 로그인 — 미리 로그인, 조작권·승인, 로그인 유지, 필요할 때만 창

[← 문서 목차](README.md)

## 미리 로그인해 두기(봇·원격 사용)

텔레그램 봇처럼 밖에서 쓰려면 맥 앞에서 먼저 로그인해 두세요. 봇의 상주 서버는
`agent-browser serve --profile private --browser on-demand` 로 같은 이름의 프로필을 씁니다.

```text
agent-browser login https://example.com --profile private
agent-browser profile sites private
agent-browser profile sites private --json
```

`login URL [--profile NAME] [--server ID] [--timeout SEC]` 는 사람 명령입니다(기본 대기 600초, 최대 3600초).
실행 중인 서버가 하나면 그 서버의 프로필을 고르므로 `--profile` 을 생략할 수 있습니다.
그 프로필을 쓰는 서버가 있으면 진행 중인 호출을 마친 뒤 사람에게 조작권을 넘기고 새 탭에
URL 을 엽니다(on-demand 는 창으로 전환). 창에서 로그인하고 **'로그인 상태 유지'** 를 체크한 뒤
Enter 를 누르거나 그 탭을 닫으면 조작권을 반납합니다. Ctrl-C·SIGTERM·SIGHUP·시간 만료에도 반납합니다.
강제 종료(kill -9) 뒤에는 서버가 대기 만료 시 반납합니다.
서버가 없으면 같은 프로필을 잠근 뒤 headed Chromium 으로 열고, Enter·탭/창 닫기 뒤 브라우저를
닫아 잠금을 풉니다. URL 은 HTTP(S)만 허용하며 서버 경유 시 기존 egress 정책을 그대로 적용합니다.
headless 전용 서버나 화면 없는 환경에서는 실패합니다. 맥 앞에서 headless 서버를 끝내고 같은
`login` 명령을 실행한 뒤 `serve --profile private --browser on-demand` 로 다시 띄우세요.
프로필 없는 서버는 로그인을 유지하지 못하므로 `--profile` 로 서버를 띄워야 합니다.

`profile sites NAME [--json]` 은 쿠키가 있는 사이트, 가장 늦은 만료 시각(UTC), 세션 쿠키만 있는지
표시합니다. **쿠키 있음은 로그인됨을 보증하지 않습니다.** 쿠키 이름·값은 출력하지 않습니다.
서버 사용 중에는 Hub 로 서버가 집계한 메타데이터만 받고, 서버가 없으면 읽기 전용 SQLite
사본으로 읽습니다. DB 잠금·복사는 최대 2초 또는 잠금 재시도 20회까지 기다린 뒤 이유와 함께 실패합니다.
두 경로 모두 사이트 목록 12KB 상한을 넘으면 `truncated: true` 또는 잘림 안내가 나옵니다.
등록 도메인 집계는 흔한 공용 접미사만 포함한 근사입니다. 그 밖은 호스트가 덜 묶이거나 더
묶일 수 있으며 표시용일 뿐 보안 판정에 쓰지 않습니다.

**평소 Chrome 의 로그인 쿠키 가져오기(WS-59, 사람 전용).** 평소 Chrome 에 이미 로그인한 사이트를 골라
그 쿠키만 봇 프로필로 한 번 복사합니다. 맥 앞에서 사람이 직접 실행하는 명령이며 에이전트(MCP)에는 없습니다.

```text
agent-browser profile import-cookies --profile private --site example.com --dry-run
agent-browser profile import-cookies --profile private --site example.com --site example.co.kr
```

- 실행하면 macOS 키체인 **'Chrome Safe Storage' 허용 창**이 뜹니다(쿠키 값을 풀기 위해). '허용'을 누르세요 —
  거부하면 아무것도 가져오지 않습니다. `--chrome-profile 'Profile 1'`·`--chrome-dir PATH` 로 다른 Chrome 프로필을 고릅니다.
- `--site` 는 등록 도메인(`example.com`)이며 그 하위 도메인 쿠키까지 가져옵니다. `evil-example.com` 같은 닮은
  이름은 제외하고, 공용 접미사(`github.io`·`co.uk`)·하위 도메인(`www.example.com`)·IP 는 거부합니다.
- 평소 Chrome 의 `Cookies` DB 파일(과 곁 파일 `-journal`·`-wal`)만 임시 폴더(0700)로 복사해 읽고 바로 지웁니다
  (Chrome 이 켜져 있어도 됨). 복사 중 Chrome 이 DB 를 쓰면 다시 복사하고, 쓰던 중(커밋 전)인 값은 가져오지 않습니다.
  비밀번호·기록 등 다른 파일은 읽지 않고, 평소 프로필에 연결하지도 않습니다. `--chrome-dir` 부터 DB 까지 경로에
  심볼릭 링크가 끼어 있으면 거부합니다.
- 그 프로필을 `serve` 가 쓰는 중이면 거부합니다(서버를 끝내고 실행). 만료·secure·HttpOnly·SameSite 는 그대로
  넣고, 만료된 쿠키·세션 쿠키(브라우저를 닫으면 사라짐)·알 수 없는 암호 형식은 건너뛰고 이유별 개수만 알려 줍니다.
- 출력은 사이트별 쿠키 **개수·이름**만입니다. 값·키체인 비밀은 화면·로그에 나오지 않습니다(값이 든 쿠키 이름은
  `[REDACTED]`). `--dry-run` 은 쓰지 않습니다.
- 넣은 뒤 봇 프로필의 쿠키를 다시 읽어 값·속성까지 같은 것만 '가져온 쿠키'로 셉니다. 브라우저가 거부해 같은 이름의
  기존 쿠키가 남은 경우는 따로 알려 주고 실패로 셉니다. 실제로 들어간 사이트만 프로필의 로그인 기록
  (`login_ledger.json`, 0600)에 `chrome_import` 로 남습니다.
- 주의: 가져온 뒤에는 봇이 그 계정으로 행동할 수 있습니다. 꼭 필요한 사이트만 고르세요.

로그인이 풀리면 맥 앞에서 다시 실행해야 합니다. 세션 쿠키만 발급하거나 매번 휴대폰 인증을
요구하는 사이트에는 미리 로그인 방식이 맞지 않을 수 있습니다. 밖에서 급하면 Chrome 원격
데스크톱·맥 화면 공유 같은 원격 데스크톱 앱으로 맥 화면을 보며 같은 명령을 실행하세요.
`browser_control_request` 의 `data.login_hint`·`how_to_respond` 와 무인 거부 결과의
`data.login_hint` 는 에이전트가 사용자에게 전달할 한 줄 명령을 제공합니다.
주소의 호스트가 안전한 문자 범위가 아니면 명령 대신 `<페이지 주소>` 를 확인하도록 안내합니다.

## 사람 인계 — 조작권과 승인 (MCP)

액션 툴 19종 외에 계약 밖 **사람 인계 서버 도구 4개**(`browser_control_request`·`browser_control_status`·`browser_control_wait`·`browser_approval_wait`)가 tools/list 에 함께 실립니다(레시피 도구 `browser_recipe` 는 [동작 캐시](features.md#동작-캐시--레시피-browser_recipe), 영상 검색 도구 `browser_youtube_search` 는 [YouTube 영상 검색](features.md#youtube-영상-검색-browser_youtube_search) 참조). 사람은 같은 컴퓨터의 터미널에서 `agent-browser control …`·`agent-browser approve …`로 답합니다. `serve`는 시작할 때 stderr 에 `server_id=<id>`를 한 줄 씁니다. 서버별 상태는 `~/.agent-browser/servers/<server_id>/`(0700, 파일 0600)에 있고 서버가 끝나면 지웁니다(프로세스가 없는 옛 디렉터리는 다음 서버가 시작할 때 청소). 서버가 하나만 떠 있으면 `--server`를 생략할 수 있고, 둘 이상이면 목록을 보여 주고 거부합니다.

**캡차 예시(조작권).** 창이 보이는 서버(`serve --browser human` 또는 `--browser user-chrome`)와 필요할 때 창을 여는 `--browser on-demand` 서버에서 됩니다 — headless 서버의 `browser_control_request`는 사람이 볼 창이 없어 거부하고 이 옵션들을 안내합니다.

1. 에이전트: `browser_navigate` 결과에 `data.challenge`가 있음 → `browser_control_request(reason="캡차 확인")`. 창이 앞으로 오고 창 위에 안내 띠(DevTools 오버레이 — 페이지 DOM 을 바꾸지 않고 페이지 스크립트가 읽거나 누를 수 없음)가 뜨며, 서버 stderr 에도 안내가 나갑니다.
2. 에이전트: `browser_control_wait(timeout_s=120)` — 사람이 가져가거나 돌려줄 때까지 기다립니다(상한 120초, 다시 부르면 이어서 기다림).
3. 사람: 터미널에서 아래를 치고, 브라우저 창에서 직접 캡차를 풉니다.

```text
agent-browser control take
agent-browser control release
```

4. 에이전트: `control_wait`가 `changed: "released"`로 돌아오면 `browser_observe_page`로 다시 관찰하고 이어서 답합니다. 반납 때 `snapshot_epoch`가 올라가므로(사람이 화면을 바꿨을 수 있음) 이전 `element_id`는 무효입니다. 사람이 에이전트가 쓰던 탭을 닫았으면 반납 때 남은 탭으로 바꾸고 `data.tab_closed_by_human={closed_tab_id, active_tab_id, hint}`로 알립니다(`browser_tab_control(command="list")`로 확인). 탭 복구가 10초 안에 끝나지 않으면 반납 응답에는 이 알림이 빠지고 다음 `browser_control_status` 응답에 실립니다(서버 로그에 경고 — 의도된 저하).

`holder=human`인 동안 조작 액션(click·type_text·navigate·press_key·select_option·check_box·scroll·hover·upload_file·download_file·handle_dialog·switch_frame·reload·go_back, `tab_control`의 create/switch/close)은 `E_HITL_UNATTENDED_BLOCKED`와 `data.control={holder, reason, since, how_to_wait: "browser_control_wait"}`로 거부됩니다. 관찰(`observe_page`·`take_screenshot`·`extract`·`wait_for`·`tab_control list`)은 허용합니다 — 사람이 하는 일을 보고 이어받을 수 있게. 단 `secret_wanted=true`로 요청한 동안(비밀번호 입력 등)은 요청 순간부터 반납까지 관찰도 막습니다. 비밀값은 에이전트에게 가지 않습니다.

**조작권 구간의 로그인 기록(WS-58).** 사람이 조작권을 가진 동안(take~release) 그 브라우저(모든 작업 공간의 탭·새 탭 — 사람 조작권은 브라우저 전체, WS-57)의 POST/PUT 요청(Network 계층, 메서드·origin 만 — 본문 미열람)·쿠키 추가·값 변경·비밀번호 칸 제출·`secret_wanted` 인계는 그 origin 을 로그인 기록(`human_control`)에 남겨, 이후 그 사이트에서는 헤드리스 이름 재시도를 하지 않습니다. 요청 감시는 조작권이 넘어간 그 동기 구간에서 먼저 붙고(설치 중 요청도 기록), 설치가 실패하면 그 구간에 방문한 origin 전부를 기록합니다(fail-closed). 규칙과 한계는 [헤드리스 이름 재시도](headless-retry.md)에 있습니다.

**결제 버튼 예시(승인 증표).** 창이 보이는 서버(`--browser human`·`user-chrome`)와 `--browser on-demand` 서버(코드는 별도 작은 창 — 아래 '필요할 때만 창' 절)에서 고위험 액션이 막히면(무인 차단·대화형 확인 필요 모두) `data.approval = {approval_id, expires_at, how_to_approve}`가 붙습니다. 서버는 이 증표를 행동 내용의 해시(액션 종류 + 정규화한 파라미터 + 대상 요소 판정 근거 `gate_basis` + 탭 id + 현재 문서 origin + `snapshot_epoch`의 SHA-256)에 묶어 두고, 해시는 사람 CLI 만 상태 파일에서 읽습니다(에이전트 응답에는 싣지 않음). **headless 서버는 승인 증표를 발급하지 않습니다** — 사람이 볼 창이 없어 확인 코드를 띄울 곳이 없기 때문입니다. 그때는 기존처럼 `--pre-approve`·`--mode interactive` 안내만 나가며, 대화형 모드의 확인도 같은 이유로 창(확인 코드)이 있어야 완료할 수 있습니다.

1. 에이전트: `browser_click(element_id="@e5", epoch=3)` → 차단, `approval_id=ap_…`. 메시지는 "사람에게 `agent-browser approve ap_…` 실행을 요청"하라고 안내합니다.
2. 사람: 터미널에서 `agent-browser approve ap_…`를 치면 내용(액션·대상·근거·origin·탭·epoch·파라미터·만료)이 **실행 중인 서버의 메모리 기준**으로 나오고(상태 파일과 다르면 경고하고 승인 절차를 시작하지 않음), **브라우저 창 위(오버레이)에 6자리 확인 코드**가 액션 종류·대상 이름과 함께 뜹니다(120초 유효 — 터미널 내용과 대조). 그 코드를 프롬프트에 입력하거나 `--code`로 다시 실행합니다. `--deny`는 거절, 빈 입력은 취소입니다. 페이지·에이전트가 정한 문자열(요소 이름·사유·URL)은 제어문자·개행·양방향 제어를 `\x1b`·`\u202e` 같은 보이는 표기로 바꿔 보여 줍니다(화면 위조 방지).

```text
agent-browser approve ap_XXXX            # 창에 코드가 뜸 → 터미널이면 그 자리에서 입력
agent-browser approve ap_XXXX --code 123456
```

확인 코드는 서버가 `secrets`로 만들어 창 오버레이에만 띄우고, 서버 메모리에는 HMAC 만 둡니다 — 상태 파일·MCP 응답·serve stderr·로그·ack 어디에도 쓰지 않습니다. 틀리면 거부, 연속 3회 틀리거나 코드 표시를 5회 넘게 요청하면 그 증표를 폐기합니다(새로 막히면 새 id). headed 창에서는 오버레이가 화면 캡처에 찍히므로 **코드가 떠 있는 동안 `take_screenshot`(일반·전체·SoM)은 `E_SCREENSHOT_FAILED`, `data.blocked_by="approval_code_displayed"`로 거부**합니다(관찰·추출은 그대로). `--yes`는 더 이상 코드를 대신하지 못합니다. 터미널(TTY) 여부는 프롬프트를 띄울지 정하는 편의일 뿐 보안 근거가 아닙니다(pty 로 흉내 낼 수 있음).

3. 에이전트: `browser_approval_wait(approval_id)`로 기다린 뒤 **같은 인자에 `approval_id`를 더해** 다시 호출합니다: `browser_click(element_id="@e5", epoch=3, approval_id="ap_…")`.

서버는 (사람이 승인함 ∧ 만료 전(기본 30분, `serve --approval-ttl SEC`) ∧ 다시 계산한 digest 가 같음 ∧ 처음 씀)일 때만 통과시킵니다. 증표로 실행하는 호출은 **자가 치유(비슷한 이름의 요소로 대체)를 끄고**, `element_id` 대상이 관찰 때 그 요소 그대로(연결·role·이름)일 때만 누릅니다 — 승인 뒤 같은 이름의 다른 버튼으로 바뀌었으면 `E_TOCTOU_MISMATCH`, `data.approval.reason="target_changed"`로 거부합니다(증표는 승인 상태로 남음). 끝난 증표(사용·거절·만료·폐기)는 10분 뒤 메모리·파일에서 지우고, 증표 수는 200개로 제한합니다. 승인 전·다른 서버의 id·만료·재사용·파라미터/대상/탭/origin/epoch 가 달라진 호출은 거부하고 이유를 `data.approval.rejected`에 싣습니다. **에이전트가 가진 `approval_id`만으로는 실행되지 않습니다** — 승인은 사람이 대역 밖에서만 할 수 있고, 서버는 디스크의 승인 파일이 아니라 자기 메모리의 상태만 믿습니다(명령 파일은 같은 사용자 소유·0600·server_id 일치·digest 일치일 때만 처리). 증표로 실행했는데 결과가 불확실하면(`E_TIMEOUT`·`E_PAGE_CRASHED`·`E_NAVIGATE_TIMEOUT`) `data.approval.outcome="outcome_unknown"`, `retry_safe=false`로 알리고 같은 증표는 다시 쓸 수 없습니다 — 자동으로 다시 시도하지 말고 관찰해 확인하십시오. 운영자 정적 허용 `--pre-approve`는 그대로 동작합니다.

**한계.** ① 확인 코드는 "MCP 만 가진 에이전트는 승인할 수 없다"를 보장합니다. **같은 OS 사용자로 셸을 쓰는 에이전트**는 화면 녹화 권한(macOS 화면 기록 등)까지 얻으면 창의 코드를 볼 수 있고, 같은 사용자 권한으로 상태 디렉터리도 다룰 수 있습니다 — 근본 해법은 **에이전트를 다른 OS 사용자로 돌리는 것**입니다. 코드가 떠 있는 동안 화면 캡처 도구는 거부하지만, OS 화면 녹화 권한을 가진 같은 uid 프로세스는 볼 수 있습니다. ② digest 의 origin 은 최상위 문서 기준입니다 — 프레임 안(결제 iframe 등)의 origin 변화는 잡지 않습니다(후속). ③ 서버 생존 판정은 server.json 의 uid·pid·시작 시각으로 합니다(다른 사용자 프로세스가 pid 를 재사용하면 죽은 서버로 봄). ④ 코드 표시와 화면 캡처는 직렬화합니다: 표시 요청이 오면 새 캡처를 먼저 거부하고, 이미 진행 중인 캡처가 끝난 뒤(5초 넘으면 표시 취소)에만 코드를 띄우며, 캡처 도중 코드 오버레이가 한 번이라도 켜졌으면(세대 번호) 그 캡처 결과를 버립니다. ⑤ approve 화면의 서버 응답은 요청마다 새 challenge 로 HMAC 해 확인하지만, 같은 OS 사용자는 그 응답도 흉내 낼 수 있습니다(①과 같은 경계) — 창 오버레이의 액션·대상과 대조하십시오.

## 로그인 유지 (`serve --profile NAME`)

`serve`는 기본으로 매번 빈 브라우저로 시작합니다 — 서버를 다시 켜면 로그인이 사라집니다. `--profile NAME`을 주면 **이름 붙인 영속 프로필 폴더**로 시작해 쿠키·로컬 저장소·IndexedDB 가 다음 실행에도 남습니다(쿠키만 옮겨 담는 방식은 네이버가 다음 실행에서 거부한 실측이 있어 폴더를 통째로 씁니다). `--profile`이 없으면 동작은 예전과 같습니다.

```text
# 1) 창 있는 브라우저로 한 번 로그인 — 사람이 창에서 직접(에이전트는 비밀번호를 보지 않음)
agent-browser serve --browser human --profile work
#    에이전트: browser_control_request(reason="로그인", secret_wanted=true)
#    사람:     agent-browser control take → 창에서 로그인('로그인 상태 유지' 체크) → agent-browser control release
# 2) 이후에는 headless 로 — 같은 이름이면 로그인이 유지됨
agent-browser serve --profile work
```

- **이름**: 영문 소문자·숫자·하이픈 1~32자(`[a-z0-9-]`). 경로 문자(`/`·`..`)는 거부합니다.
- **보관 위치**: `~/.agent-browser/profiles/serve-NAME/`(폴더 권한 700). 루트 폴더는 우리가 새로 만들 때만 700 으로 만들고, 이미 있는 루트(예: `AGENT_BROWSER_PROFILE_ROOT=$HOME`)의 권한은 바꾸지 않습니다. 사이트별 프로필 폴더와 같은 루트이며 `serve-` 앞머리로 구분합니다. 루트는 환경변수 `AGENT_BROWSER_PROFILE_ROOT`로 바꿀 수 있습니다(테스트는 임시 폴더를 씁니다). 평소 Chrome 프로필 아래와 user-chrome 전용 폴더(`~/.agent-browser/chrome-profile`) 아래는 거부합니다.
- **보안 정책은 그대로**: Egress 검증 프록시(사설망 기본 차단·CONNECT 만·fail-closed), QUIC 끔, WebRTC 비프록시 UDP 차단, route 가드, 문서 상태·차단 신호, HITL·승인 증표·조작권·안내 띠가 영속 모드에서도 같은 경로로 설치됩니다.
- **동시 사용 거부**: 한 프로필은 한 서버만 씁니다. 다른 serve 가 쓰는 중이면 시작하자마자 `프로필 'work' 를 서버 <server_id> 가 쓰는 중` 한 줄과 종료 코드 2 로 끝납니다(잠금은 프로세스가 죽으면 OS 가 풉니다). 우리 잠금 밖의 Chromium 이 그 폴더를 쓰는 중이어도(SingletonLock) 거부합니다. 두 serve 가 `--attach` 이면 거부 대신 붙습니다(아래 '함께 쓰기' 절).
- **잠금 파일을 직접 지우지 마십시오**: 프로필 폴더 안 `.agent-browser-serve.lock` 을 지우면 같은 프로필의 동시 사용을 막지 못할 수 있습니다(잠금은 파일 단위라, 지운 뒤 새로 만든 파일은 다른 서버의 잠금과 별개가 됩니다). 서버가 죽으면 잠금은 OS 가 풀어 주므로 지울 필요가 없습니다. 폴더를 없애려면 `agent-browser profile remove` 를 쓰십시오.
- **`--browser user-chrome`과 함께 쓸 수 없습니다** — user-chrome 은 이미 전용 영속 프로필(`--chrome-profile`)을 씁니다.
- **에이전트에게 보이는 것**: `browser_control_status`의 `data.profile = {name, persistent: true}`(경로는 싣지 않음). headless + `--profile` 서버에서 로그인이 필요해 `browser_control_request`를 부르면 "창 없음" 안내에 `--browser human --profile NAME`으로 한 번 로그인하라는 문구가 붙습니다.
- **목록·지우기**:

```text
agent-browser profile list            # 이름·크기·마지막 사용·사용 중(서버 id)
agent-browser profile remove work     # 확인 프롬프트(사용 중이면 거부). 비대화형은 --yes
```

**한계.** ① **쿠키는 평문**입니다 — Playwright Chromium 프로필은 OS 키체인 암호화를 쓰지 않아 폴더 권한 700 으로만 보호됩니다. 같은 OS 사용자로 도는 프로그램은 읽을 수 있습니다. ② **사이트가 세션 쿠키(만료 없음)만 주면 유지되지 않습니다** — Chromium 은 재시작 때 세션 쿠키를 버립니다(실측: Mock 사이트의 영속 쿠키는 남고 세션 쿠키는 사라짐). 로그인할 때 '로그인 상태 유지'를 체크하십시오. 쿠키 수명을 늘리거나 바꾸는 조작은 하지 않습니다(사이트가 준 그대로). ③ 서버를 SIGTERM·SIGINT·stdin 종료로 끝내면 브라우저를 닫아 쿠키를 디스크에 씁니다. SIGKILL 같은 강제 종료는 직전 변경이 남지 않을 수 있습니다. ④ 영속 프로필에는 캐시·서비스워커 등록도 남습니다(서비스워커 요청도 검증 프록시를 거칩니다).

## 대화와 정기 작업이 같은 프로필을 함께 쓰기 (`serve --attach`, 작업 공간)

같은 serve 를 대화와 정기 작업(cron 감시 등)이 동시에 쓰면 예전에는 활성 탭·`snapshot_epoch`·탭 닫기가 서로에게 샜습니다(실사용: 감시 작업이 대화가 사람에게 열어 준 창의 마지막 탭을 닫음). 이제 호출자마다 **작업 공간**(자기 탭 묶음·자기 활성 탭·자기 관찰 epoch·자기 레시피 궤적)을 갖고, 로그인(쿠키)은 같은 영속 컨텍스트라 공유합니다.

- **연결 단위(권장)**: 같은 `--profile` 의 serve 를 둘 띄우되 둘 다 `--attach` 를 붙입니다. 먼저 뜬 쪽이 브라우저를 띄우고, 나중 쪽은 잠금 오류 대신 그 serve 에 붙습니다(프로필 폴더 안 0700 디렉터리의 0600 소켓, 같은 OS 사용자만 — 연결마다 상대 uid 확인, 심볼릭 링크 거부). `--workspace NAME` 이 그 연결의 기본 작업 공간입니다(없으면 첫 serve 는 `default`, 붙은 쪽은 `attach-N`). 첫 serve 가 끝나면 붙은 serve 는 stderr 한 줄 + 종료 코드 3 으로 끝납니다(클라이언트가 다시 띄우면 새로 붙거나 직접 브라우저를 띄움). 붙은 serve 의 브라우저 옵션은 쓰이지 않습니다(첫 serve 것).

```text
agent-browser serve --profile private --browser on-demand --attach                      # 대화
agent-browser serve --profile private --browser on-demand --attach --workspace watch    # 감시(cron)
```

- **호출 단위**: 한 연결을 여럿이 나눠 쓰면 도구 인자 `workspace`(영문 소문자·숫자·하이픈 1~32자, 계약 밖 서버 메타)를 매 호출에 붙입니다. 연결 기본값보다 우선합니다. 규칙 위반 이름은 `E_TAB_NOT_FOUND` 로 거부(브라우저 무접촉).
- **따로인 것**: `browser_tab_control` list/switch/close(남의 탭은 `E_TAB_NOT_FOUND`, list 는 남의 탭을 `data.other_workspace_tabs` 개수로만), navigate·관찰 대상 탭, epoch, 레시피 궤적, 호출별 승인·판정 근거 상태. 마지막 자기 탭을 닫으면 빈 탭으로 바뀝니다(WS-50 규칙, 남의 탭에 영향 없음).
- **함께인 것**: 쿠키·로그인, Egress 가드, 탭 상한(전체 10개), 창(on-demand 창 전환은 브라우저 재기동 — 모든 작업 공간 탭을 주인째 복원하고 각 epoch 를 올림), 사람 조작권(브라우저 전체), 승인 정책, 레시피 저장소.
- 한 작업 공간만 쓰면(기존 설정) 동작·응답은 예전 그대로입니다(`other_workspace_tabs` 없음). 도구 스키마는 그대로입니다 — `workspace` 인자는 서버 instructions 한 줄로만 알립니다(스키마에 넣으면 tools/list 토큰 예산 2500 을 넘음, 실측 2654).
- **브라우저 전체 사건의 보고·무효화(WS-57 R1)**: 창 전환(재기동) 보고 `data.window.tabs`(도구 결과·`control_wait`·`control_request`)는 부른 작업 공간의 탭만 싣고 다른 작업 공간은 `other_workspace_tabs` 개수로만 — 작업 공간마다 한 번씩 듣습니다. 사람이 닫은 탭 알림 `tab_closed_by_human` 은 그 탭 주인에게만. 사람 조작권 반납·사람이 창을 닫음·창 재기동은 **모든** 작업 공간의 epoch 를 올려 반납 전 `element_id`·승인 증표를 어디서도 다시 쓰지 못합니다. 클릭의 '새 탭' 효과는 자기 작업 공간 탭만 셉니다(남의 탭 생성은 효과가 아님). 자기 탭이 사람·사이트에 닫혔으면 남의 탭이 살아 있어도 다음 호출이 자기 새 탭으로 복구합니다(기본 작업 공간 포함). 승인 증표 호출의 치유 끄기는 호출 문맥 단위입니다(같은 작업 공간의 동시 호출이 서로 풀지 않음).
- **같은 작업 공간은 한 번에 하나(WS-57 R2)**: 같은 작업 공간의 액션 도구 19종(관찰·추출·스크린샷·`wait_for` 포함)과 `browser_recipe` 는 작업 공간 락으로 차례로 돕니다 — 한 호출의 사후조건 대기 중에 다른 호출이 연 alert·이동·새 탭이 그 호출의 효과(성공·레시피 궤적)로 읽히지 않게. 다른 작업 공간끼리는 지금처럼 동시입니다. 긴 `wait_for` 는 같은 작업 공간의 다음 호출을 그 시간만큼 기다리게 합니다. 사람을 기다리는 서버 도구(`control_wait`·`approval_wait`·`control_request`)는 락을 쥐지 않습니다. Egress 차단·업스트림 실패 이유(`data.egress`)는 그 요청을 낸 탭의 작업 공간 호출에만, 이동 대상(또는 그 리다이렉트 앞 홉) 호스트가 맞을 때만 붙습니다 — 다른 작업 공간이 막힌 호스트가 내 이동 오류로 복사되지 않고, 탭이 없는 요청(서비스 워커 등)의 차단은 어느 오류에도 붙지 않습니다. 그 탭 **메인 프레임 문서 이동(과 그 리다이렉트 체인)** 의 차단만 이동 오류가 됩니다(WS-57 R3) — 정상 문서 안 이미지·스크립트·fetch·iframe 하위 문서가 비허용 주소로 리다이렉트돼 막혀도 navigate 는 성공 그대로이고, 막힌 기록이 있어도 탭이 지금 주소로 200 문서를 받았으면 성공입니다.
- **시작·붙기 수명(WS-57 R1)**: 첫 동시 호출들은 브라우저·검증 프록시 시작 하나를 함께 기다립니다(단일 비행, 종료 때 진행 중 시작도 정리). 붙은 연결의 요청 취소·연결 종료는 그 시작을 멈추지 않고 primary 의 프로필 잠금을 풀지 않습니다(잠금은 primary 수명 소유 — 시작 실패 되감기도 시작이 잡은 잠금만 풂). 붙기 통로의 MCP 줄에는 길이 상한이 없습니다(stdio 와 같음, 악수 줄만 1KiB).

## 필요할 때만 창 (`serve --browser on-demand`)

에이전트는 평소 화면 없이(headless) 혼자 브라우징하고, **사람이 필요할 때만** 창을 띄웁니다 — 캡차·로그인 인계(`browser_control_request`)와 고위험 행동 승인 코드. `--browser human`은 서버 수명 내내 창이 떠 있는 **디버그용 상시 창**으로 남습니다.

```text
agent-browser serve --browser on-demand                 # 서버 전용 임시 프로필(종료 때 삭제)
agent-browser serve --browser on-demand --profile work  # 이름 붙인 영속 프로필(로그인 유지)
```

Chromium 은 같은 브라우저를 headless ↔ 창 있음으로 바꿀 수 없습니다. 그래서 **같은 프로필 폴더를 닫고 창 있음/없음으로 다시 엽니다**(`--profile`이 없으면 서버 전용 임시 프로필 `~/.agent-browser/profiles/ondemand-<server_id>/`, 0700, 서버 종료 때 삭제 — 비정상 종료로 남은 것은 다음 on-demand 서버가 시작할 때 잠금이 풀린 것만 지움, `profile list`에는 안 보임). 다시 연 브라우저에는 처음 시작과 **같은 경로**로 검증 프록시(실행 인자)·route 가드·문서 상태 추적·탭·CDP·디스패처를 다시 달고, 열려 있던 탭 URL 을 복원합니다(활성 탭 먼저, `about:blank`·`chrome://` 등 http(s) 가 아닌 탭은 건너뜀). 전환 동안 프로필 잠금은 그대로 쥡니다.

1. **창 열기** — `browser_control_request` 가 오면(또는 사람이 요청 없이 `control take`) 진행 중인 도구 호출이 끝나길 기다렸다가 창을 엽니다. 전환 중 들어온 도구 호출은 끝날 때까지(최대 30초) 기다리고, 넘으면 `E_TIMEOUT` + `data.window`로 알립니다. 결과 `data.window = {mode: "on-demand", state: "headed", reopened: true, tabs: [{tab_id, was, url, active, restored, http_status?, error?}], skipped, switch_ms, snapshot_epoch, hint}`.
2. **창 닫기** — 사람이 `control release` 하면 마지막으로 보던 탭 URL 로 headless 를 다시 열고 `browser_control_wait`(또는 그다음 도구 결과)의 `data.window = {state: "headless", reopened: true, …, hint: "… 다시 관찰하세요"}`로 알립니다. 사람이 take 하지 않은 채 요청이 10분 지나면 요청을 거두고 창도 닫습니다. 사람이 창을 직접 닫으면(X) 조작권을 에이전트에게 돌리고 headless 로 다시 열어 `data.window.notice = {reason: "window_closed"}`로 알립니다. 에이전트가 `tab_control close` 로 탭을 닫은 것은 사람의 창 닫기로 보지 않습니다.
3. **sticky** — headless 로 돌아온 뒤, 창에서 해결했던 사이트(등록 가능 도메인 — `shop.example.com` 과 `www.example.com` 은 같은 사이트)에서 차단/캡차(`data.challenge`)가 다시 보이면 그 결과에 `data.window.sticky_pending = {domain, kind, detected_at}`와 "사람을 다시 부르라"는 안내가 붙습니다. 다음 `browser_control_request` 때 창을 열고 **그 서버 수명 동안 창을 유지**합니다(`window.sticky: true, sticky_reason`). 서버를 다시 시작하면 초기화됩니다. 사람은 `agent-browser control status`에서 `window=… sticky=…`로 봅니다.
4. **승인 코드 창** — headless 상태에서 `agent-browser approve`로 코드가 필요하면, 페이지는 headless 그대로 두고 **별도의 작은 창**(별도 Chromium 프로세스 — 에이전트 탭 목록·영속 프로필에 없음, 스크립트 끔, 네트워크 전부 차단, 로컬 내용만)에 승인 내용(액션·대상·문서 출처·승인 만료)과 6자리 코드를 띄웁니다. 페이지를 다시 열지 않으므로 승인 뒤 같은 인자 + `approval_id` 재호출이 **문서 해시 일치로 그대로 실행**됩니다. 코드를 입력·거절·만료하면 창을 닫습니다. 창을 띄우지 못하면 코드를 무효로 하고 승인할 수 없다고 알립니다(fail-closed). 이미 창이 열린 상태(인계 중·sticky)면 기존처럼 그 창 오버레이에 띄웁니다. 코드가 떠 있는 동안 화면 캡처 거부는 그대로입니다(방어 겹침).
5. **실패하면 닫는다** — 검증 프록시가 실행 중이 아니거나, 다시 열기·route 재설치가 실패하거나 전환 중 서버가 신호로 끝나면, 보호가 덜 걸렸을 수 있는 브라우저를 남기지 않고 닫습니다(`data.window.state: "failed"`). 다음 도구 호출이 headless 로 다시 열고 결과에 `data.window.recovered: true`를 붙입니다. 탭 하나의 복원 실패는 그 탭만 `restored: false`(이유)로 알립니다.
6. **꺼진 브라우저 복구** — 브라우저(영속 컨텍스트)가 꺼졌거나 관리 탭이 0개이면 다음 도구 호출 입구에서 같은 전환 경로로 headless 를 다시 열고 마지막 탭 URL 을 복원한 뒤 그 호출을 실행합니다(`data.window.notice = {reason: "browser_recovered", hint}`, 1회, `reobserve_required=true`). 사람이 조작권을 가진 동안에는 하지 않고, 다시 열지 못하면 `E_PAGE_CRASHED`(serve 재시작 안내)이며 호출당 1회만 시도합니다.

**무엇이 유지되고 사라지나**(로컬 Mock 실측, `.hermes/state/ws34/probe_perf.py`):

| 항목 | 전환 뒤 |
| :--- | :--- |
| 영속 쿠키·localStorage·IndexedDB | 유지(같은 프로필 폴더) |
| 세션 쿠키(만료 없음) | 유지 — 이 서버가 가진 값 그대로 다시 넣음(수명·값 변경 없음) |
| 탭 URL·활성 탭 | 복원(http(s) 탭만, 활성 탭 먼저) |
| 입력 중이던 폼 내용·sessionStorage·스크롤 위치 | **사라짐**(페이지를 다시 엶) |
| `element_id`·`tab_id` | 무효(`snapshot_epoch` 증가, 새 tab_id 는 `window.tabs[].tab_id`) |
| 전환 시간 p50 | headless→창 약 0.6초, 창→headless 약 0.3초(탭 3개 복원) |

**한계.** ① headless 는 UA 에 `HeadlessChrome`을 싣고 우리는 UA 를 바꾸지 않습니다(위장 금지) — 사이트가 창에서 통과한 결과를 headless 에서 인정하지 않고 캡차를 다시 요구할 수 있습니다(그래서 sticky). ② 전환은 **페이지를 다시 여는 것**입니다 — POST 결과 화면·입력 중인 폼·SPA 메모리 상태는 사라지고 GET 으로 다시 불러옵니다. 창을 열기 전에 입력을 끝내거나 사람에게 맡기십시오. ③ 등록 가능 도메인은 흔한 2단계 접미사(`co.kr`·`co.uk` 등)만 아는 근사입니다. ④ 같은 OS 사용자로 셸을 쓰는 에이전트에 대한 한계(승인 절 ①)는 별도 창에서도 같습니다. ⑤ 창 있는 Chromium 은 시작 직후 브라우저 자체의 Google 배경 요청(계정 조정기 ListAccounts·GCM checkin·네트워크 시각·검색 사전 연결·AI 모드 자격 조회)을 보냅니다(영속 프로필 창: on-demand 전환·`human --profile`). 비영속 `human`(프로필 없음)도 같은 요청을 보내며(실측) 이 경로는 아직 끄지 않습니다 — 요청은 검증 프록시 정책 안에서 판정됩니다. 영속 프로필로 띄우는 모든 경로(on-demand headless·창, `--profile`)의 실행 인자에서 끕니다(기능 끔, 계정·GCM 엔드포인트는 닿지 않는 루프백) — 차단 프록시 실측 외부 호스트 0건. 위장 인자는 없습니다. ⑥ 사람이 승인 코드 창을 닫으면 그 코드는 무효입니다 — `agent-browser approve` 를 다시 실행하면 새 창·새 코드가 뜹니다. 코드 창·오버레이의 대상 칸은 "(사이트가 붙인 이름)" 표기 뒤에 보이며 그 안의 6자리 이상 숫자열은 `••••••` 로 가립니다.
