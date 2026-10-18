# 보안 — 안전장치와 자격증명

[← 문서 목차](README.md)

## 안전장치

**예산 강제 차단** — 태스크당 $0.75 / 100,000토큰 / 30스텝 중 하나라도 넘으면 호출 자체를 막습니다. 경고가 아니라 차단이며, 응답을 받고 확인하면 이미 과금된 뒤라 호출 **전에** 검사합니다.

**Egress 차단** — allowlist 밖 도메인으로의 요청을 막습니다(`--allow-domain` 미지정이면 공개 주소는 모두 허용). `serve`·`run` 모두 같은 정책이며, 브라우저는 127.0.0.1 무작위 포트의 **검증 프록시**로만 나갑니다. 프록시가 요청마다(리다이렉트 매 홉, 서비스워커, WebSocket 포함) 호스트를 판정하고, 도메인은 해석된 IP **전부**를 검사한 뒤 검사한 그 IP 로만 접속합니다(DNS 재바인딩 방지). HTTPS 는 `CONNECT` 터널만 하고 TLS 를 열어 보지 않습니다.

| 대역 | 기본 | 여는 옵션 |
| :--- | :--- | :--- |
| 루프백 `127/8`·`::1`·`localhost`·`*.localhost` | 허용(로컬 Mock·개발 서버) | `--block-loopback` 으로 차단 |
| 사설 `10/8`·`172.16/12`·`192.168/16`, 링크로컬 `169.254/16`·`fe80::/10`, CGNAT `100.64/10`, IPv6 ULA `fc00::/7`, 그 밖의 비공개 대역 | 차단 | `--allow-private-network`(로컬 NAS·사내망) |
| 클라우드 메타데이터(`169.254.169.254`, `fd00:ec2::254`, `metadata.google.internal`), `0.0.0.0`·`::`, 멀티캐스트·예약 대역 | 항상 차단 | 없음 |

IPv4-mapped IPv6(`[::ffff:192.168.1.1]`)·정수(`3232235777`)·16진(`0xc0.0xa8.1.1`)·8진·축약(`192.168.257`) 표기는 브라우저와 같은 규칙으로 정규화해 판정하고, 숫자로 끝나는데 해석할 수 없는 호스트는 막습니다. 도메인 해석 결과는 최대 30초 캐시하며, 해석이 실패하면 막지 않고 기록만 합니다(브라우저도 같은 이름을 찾지 못합니다). Chromium 은 `--disable-quic`(HTTP/3 끔)과 `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`(프록시 밖 WebRTC UDP 끔)로 띄웁니다 — 영상 통화 같은 P2P WebRTC 는 동작하지 않습니다. 프록시가 죽으면 브라우저는 직접 접속으로 빠지지 않고 실패합니다.

차단된 문서 이동은 MCP 결과 `data.egress` 에 `{"code": "egress_blocked", "host", "category", "reason", "open_with"}` 로 알립니다(`navigate` 는 `success: false`, `E_INVALID_URL`). `open_with` 는 운영자 옵션 이름이며, 메타데이터처럼 열 수 없는 대역은 `null` 입니다. 허용된 목적지라도 프록시가 닿지 못하면(이름 해석 실패·접속 실패) 프록시 없이와 같이 이동 실패로 알립니다(`navigate` 는 `success: false`, `E_NAVIGATE_TIMEOUT`, `data.egress={"code": "resolve_failed"|"connect_failed", "host"}`). 사이트가 실제로 돌려준 502 는 그대로 이동 성공입니다(`data.last_http_status`). 차단 기록·로그의 URL 은 스킴·호스트·포트·경로까지만 남깁니다(쿼리·조각·사용자정보 제외).

**남은 한계.** ① 검증 프록시는 자격을 요구하지 않는 127.0.0.1 무작위 포트입니다(모든 브라우저 모드 공통, WS-47 R3 — 브라우저에 프록시 자격을 주면 Playwright 가 그 자격을 사이트의 401 에도 보냈습니다, 실측). 같은 컴퓨터의 다른 프로세스가 포트를 찾으면 쓸 수 있으나 가드가 허용한 목적지로만 중계됩니다(접속한 프로세스 확인은 후속 과제). 설치된 Chrome 은 WebRTC 플래그를 무시해(Chrome 154 실측) 전용 프로필의 `webrtc.ip_handling_policy` 설정으로도 겁니다. 우리가 띄우지 않은(이미 떠 있는) Chrome 에 붙는 경로는 Egress 정책 밖입니다(`serve`·`run` 에는 그런 경로가 없습니다). ② 프록시는 해석한 IP 로 접속하므로 같은 IP 안의 가상 호스트는 구분하지 않습니다(HTTPS SNI·Host 는 브라우저가 보낸 그대로). ③ 하위 요청(이미지·비콘 등) 차단은 `data.egress` 에 싣지 않습니다(문서 이동만). ④ `session login`(사람이 직접 로그인하는 창)에는 Egress 정책을 걸지 않습니다. ⑤ 루프백은 포트와 무관하게 허용됩니다 — SSH 등 같은 컴퓨터의 로컬 서비스로도 터널이 열릴 수 있습니다. 막으려면 `--block-loopback` 을 쓰십시오.

**프롬프트 주입 신호** — `run`(내장 LLM 루프)은 웹 텍스트를 신뢰 경계 블록에 넣어 지시로 읽지 않게 합니다. `serve`(MCP)는 결과의 웹 유래 텍스트에 지시형 문구가 있으면 `data.injection_suspected` 신호를 붙입니다 — 차단이 아니라 신호이며 정규식 기반이라 우회될 수 있습니다([프롬프트 주입 신호](signals.md#프롬프트-주입-신호-datainjection_suspected)). 제품 경로(MCP 결과) 측정 탐지율 1.0, 오탐률 0.0(Mock 표본 기준).

**응답 주소의 비밀 값 가리기** — `serve`(MCP)가 돌려주는 주소 칸(`current_url`·`url`·`*_url`·`data.window.tabs[].url` — 스킴과 무관, `about:blank#…`·상대 주소 포함)과 자유 문장(오류 문구·robots 규칙·서버 stderr·로그 줄) 안의 주소는 쿼리·조각의 비밀 값을 `[REDACTED]` 로 가립니다 — 키 이름이 비밀 계열(token·session·code·sig·key 등, 퍼센트 디코딩 → Unicode NFKC → casefold 로 판정해 대소문자·퍼센트 인코딩·전각 `ｔｏｋｅｎ`·long-s `ſeſſion` 무시, 짧은 낱말은 낱말 단위 판정. 정렬·국가·우편·상태·언어·통화·상품 분류 같은 비밀 아닌 복합어 키 — `sortKey`·`country_code`·`productCode`·`statusCode` 등, 대소문자·`_`·`-` 를 무시한 정확 일치 허용 목록 — 는 키로는 가리지 않고 값 판정·레지스트리만 받으며, `promoCode`·`couponCode`·`token_type`·`code` 와 커서·페이지 토큰 키 `pagekey`·`cursorKey`·`next_key`·`pageToken` 등은 어떤 철자든 계속 가립니다 — WS-55)이거나 값이 JWT·24자 이상 무작위 문자열일 때만, 파라미터 구분자는 `&`·`;`이고 경로 matrix 매개변수(`/p;jsessionid=…;v=2` — 세그먼트마다)도 같은 판정·등록을 받으며(WS-55 R1), 사용자정보(`user:pass@`)는 뺍니다. 자유 문장(페이지 제목 포함)은 세 겹입니다 — ① **도구가 본 주소의 비밀 값은 어디서든 가립니다**: 이동 대상·리다이렉트 체인·main-frame 이동(같은 문서 안 이동 포함)·팝업·새 탭·다운로드 대상·응답의 주소 칸·복원한 탭·레시피 파일의 단계 주소에서 위 규칙으로 가린 바로 그 값과 주소 밖에서 도구가 비밀로 아는 값(`type_text` 의 SecretStore·자격증명 해석 값, 그 입력 액션 동안(clear·포커스 전·clear 후·입력 후) 한 번이라도 비밀번호 칸이던 입력칸에 친 값 — 첫 판정은 clear 전이라 clear 의 input 이벤트로 페이지가 type 을 바꾸거나 칸을 교체해도 유지(WS-55 R2), 로그인 칸에 채운 아이디·비밀번호 — WS-55 R1. 일반 칸에 에이전트가 친 검색어는 등록하지 않습니다)을 프로세스 메모리 안 레지스트리(디스크·로그·응답에 쓰지 않음, 값 256개·글자 16Mi 자 LRU — 값 단위로 통째로 잊고 살아 있는 값은 모든 표현을 가집니다. 값 하나의 표현 합이 16Mi 자를 넘으면 그 값만 남깁니다. 긴 값도 그대로 보관)에 기록해, 문장·로그·주소 칸 어디에 나와도 정해진 **표현 집합**(원문·퍼센트 — quote/quote_plus/JS encodeURIComponent·encodeURI 상당, 16진 대문자·소문자, 이중 퍼센트 · JSON 문자열 · repr · HTML 명명/숫자 참조 · 그 조합 일부, `security.secret_registry.representations`)으로 정확 일치해 가립니다(8자 이상은 부분 일치, 4~7자는 앞뒤가 영숫자가 아닐 때만, 256자 이상 값은 잘리거나 일부가 바뀐 꼴도 원문 연속 256자 이상이 남지 않게). 글자만으로 된 4~7자 값·4자 미만·도구 어휘(`navigate`·`E_INVALID_URL` 같은 액션·오류 코드)·도구가 만든 식별자(`tab-1`·`@e3`·`@r2`·시각 폴백 `@s10`·승인/요청 id·서버 id·레시피 id `r_`+16진 8자리·자동 레시피 매개변수 이름 `text1`·`choice1`)는 등록하지 않습니다(생산자 목록은 소스의 식별자 모양 문자열 조립 전수 조사로 고정 — `tests/security/test_ws51_r6_census.py`). 주소 칸에서는 8자 이상 값의 원문·퍼센트 꼴만 쓰고(호스트는 라벨 단위, 대소문자 무시) 4~7자 값은 쓰지 않습니다(`/archive/2026/` 같은 정상 경로 보존). 응답의 제어 필드(`action`·`error_code`·`*tab_id`·`element_id`·`approval_id`·레시피 `id`·`params`·`status`·`holder`·`nav_aborted`·`nav_frame`·`resolved_from`·`captured`·`dialogs[].handled`·`dialogs[].type`·`egress.code`·`egress.category`·`robots.scope`·`gate_basis.source`·`challenge.vendor`·`healing_attempts`(전략명과 복합 코드 `text_similarity(identity_changed)`·`…(ambiguous)`)·`signals[]` 의 순수 신호 코드·시각 등)는 값이 **그 키의 실제 열거 어휘**(코드 상수·라이브러리 문서에서 도출한 `CONTROL_VOCABULARY` — 예: `holder` 는 `agent`·`human`. 목록 안 dict 의 키 경로로 정한 칸은 `CONTROL_ITEM_VOCABULARY` — `dialogs[].type` 은 Playwright Dialog.type `alert`·`confirm`·`prompt`·`beforeunload` 이고 다른 위치의 `type` 은 면제하지 않습니다. 목록 바로 아래 문자열 원소로 정한 칸은 `CONTROL_LIST_VOCABULARY` — `healing_attempts[]` 의 전략명·복합 코드, `signals[]` 의 `text_changed`·`element_attr_changed`·`navigation_started`·`top:…`·`dialog_opened:…` 같은 도구 고정 코드만이고, 같은 목록의 `url_changed: A -> B`·`focus_moved: …`·`value_applied: …`·`dom_delta: 3 -> 5` 같은 `종류: 상세` 문장은 정제합니다)나 식별자 형식일 때만 그대로 둡니다(키 목록은 소비자 표와 무관한 소스 전수 조사 — 문자열 키 칸에 리터럴 값이 실리는 모든 자리(`tests/security/test_ws51_r7_census.py`)와 다른 객체의 속성·열거형 값을 옮겨 담는 모든 자리(`tests/security/test_ws51_r8_census.py`)·목록에 문자열(복합 f-string 코드 포함)을 쌓는 모든 자리(`tests/security/test_ws51_r9_census.py` — 치유·신호 코드는 생산 코드의 원소 틀에서 도출해 소비자 어휘와 같음을 고정)를 제어·식별자·주소·자유 문장·페이지 값·응답 출구 아님 등으로 분류해 고정). 자유 문장 칸(사람이 쓴 `reason`·`message`·`detail`·`note`·`hint`·`sticky_reason` 등)은 낱말 모양 값도 가립니다(그 키의 어휘와 똑같은 값만 예외). ② **출처 모를 문장은 URL 문법 경계만**: 공백·따옴표로 나뉜 주소 덩어리에 같은 규칙을 적용하고 공백은 주소의 끝으로 봅니다 — `?token=X 다음 말` 의 '다음 말' 은 남습니다. 비밀 키 값이 비었거나 `\` 뿐인데 바로 따옴표가 오면(`token="X"`·`token=\"X\"`·JSON·repr 을 몇 겹 거친 문자열 안 주소) 같은 깊이로 이스케이프된 짝 따옴표나 줄 끝까지 길이 상한 없이 가립니다. ③ JWT 모양. **위협 모델·허용 한계**(계획 `.hermes/plans/ws51-r4-plan.md` §1): 출구는 액션 19종 응답·서버 도구 응답·서버 stderr(줄 기록·logging 의 메시지/args/extra(중첩·키 포함)/traceback/stack·`sys`/`threading` excepthook)·MCP SDK 예외 문구입니다. 범위 밖 — 자식 프로세스(Chromium·드라이버) stderr, 화면 캡처 픽셀, 훅 설치 전 로그, 디스크용 파일(레시피·control.json), switch_frame 전 하위 프레임 이동·XHR/fetch·POST 본문·Set-Cookie·서버 재시작 전에 본 값, 대소문자 섞인 16진·base64·`\u00XX` 전수·NFC/NFD 차이·말줄임(`abcd…wxyz`). 허용 한계 — 도구가 어떤 주소에서도 본 적 없는 비밀이 출처 모를 문장에만 공백 뒤로 이어지는 꼴(`/a?token=prefix X` 의 X, 페이지가 쓴 내용), 4~7자 순수 알파벳 값, 4~7자 값의 영숫자 인접, 상한을 넘어 잊힌 옛 값, 서로 다른 등록 값이 겹쳐 남는 일부 조각, 레지스트리가 프로세스 전역이라 탭 A 의 값이 탭 B 에서도 가려지는 것(의도), **호스트 라벨 부분 일치**(`xx<V>yy.example.com` — 라벨 전체가 V 일 때만 가림, 정상 도메인 보호), 제어 키의 값이 그 키의 열거 어휘와 똑같은 등록 값, **제어 어휘 누락**(등록 값이 도구 내부 코드 낱말과 똑같을 때만 그 코드가 `[REDACTED]` 로 바뀌는 종류 — 누출이 아니며 예: `종류: 상세` 신호의 종류 접두 `dom_delta` 는 문장째 정제. 새로 찾는 것은 후속 과제 NB 로 처리). 표현 합이 수십만 자를 넘는 거대 값은 그 값 하나가 메모리를 차지합니다(77만 자 값 → 표현 약 6,800만 자, 등록 약 1.3초) — 공격 페이지의 자가 부하입니다. **페이지 내용 subtree**(`extract` 의 `data.items`·관찰 `data.observation.elements`·`axtree_summary`)는 원문 보존 계약이라 범위 밖입니다. `?`·`#` 없는 `token=X` 대입식, 등호 없는 짧은 값, 상대 주소를 담은 `next=`, `?`·`#`·`://` 없는 상대 주소 문장의 경로 matrix(`/p;token=X` 단독), 터키어 `ı`·`İ` 처럼 casefold 동치가 아닌 키도 범위 밖입니다. 검색어·날짜·쪽 번호 같은 값은 그대로이고, 페이지 **내용**인 `extract` 항목(`data.items`)·관찰 요소(`data.observation.elements`·`axtree_summary`)는 원문입니다. 규칙 기반이라 이 규칙에 맞지 않는 비밀(평범한 키 이름의 짧은 값 등)은 가려지지 않습니다. 처리는 입력 길이에 선형입니다(실 observe+응답 1MiB 제목, 5회 중앙값: 조밀한 `a…` 등록 값 8개 0.04초, 4자 값 256개 조밀 일치 0.12초. 함수 단위 1MiB 최악 — 특수 문자 값 256개 0.35초, 4~7자 경계 실패 조밀 0.32초, 64KiB 값 16개 블록 조회 0.24초). 로그는 포맷 **전에** 인자·`extra` 를 같은 모양으로 정제하고(문자열·dict 키·중첩·`extra` 속성 이름. 숫자(int·float·Decimal·IntEnum 등)는 가려질 때만 표지 문자열이 되고 가려지지 않은 숫자는 그대로라 `%d`·`%5.2f`·`%x` 출력이 같으며, 가려진 숫자 자리의 숫자 지정자는 `%s` 로 바꿔 형식 오류 없이 표지. bytes·datetime·UUID·Enum·객체는 str/repr 을 가리는 래퍼 — 가려지면 숫자로 바뀌지 않음. 표준·JSON 형 Formatter 가 extra 를 찍어도 원문 0. 문자열 `extra` 속성 이름은 바꾸지 않아 기존 Formatter 의 `%(name)s`·`{name}`·`${name}` 조회가 그대로 동작하고, 숫자·bytes 같은 비문자열 이름은 중첩 dict 키와 같은 규칙으로 가린 이름이 됩니다) 완성된 메시지를 한 번 더 가리고, handler 가 내보내는 최종 줄(속성 이름을 찍는 JSON 형 Formatter 포함)과 `Handler.handleError` 진단(Formatter 오류·emit 예외 traceback)에서도 등록 값을 가리며, 겹쳐 설치돼도 마지막 설치가 끝날 때까지 유지됩니다. 도구 처리 중 예외는 고정 문구 + 가린 예외 문구로 바꿔 MCP 오류로 보냅니다. 서버 안(탭 복원·창 전환·레시피 재생)은 원문 주소를 쓰고, 가린 주소를 `navigate`·`tab_control create` 에 넘기면 이동하지 않고 `E_INVALID_URL` 입니다(reload·go_back·탭 전환이나 링크 재추출 안내).

**세션 암호화** — 쿠키와 로컬스토리지를 AES-256-GCM + Argon2id로 암호화해 `0600` 권한으로 저장합니다.

헤드리스 이름 재시도의 쿠키 장부(값 지문·영속 파일·보안 경계, WS-47·WS-52)는 [headless-retry.md](headless-retry.md)에 있습니다.

---

## 로그인 세션과 자격증명

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
