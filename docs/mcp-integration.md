# MCP 클라이언트 연동 — 상세

[← 문서 목차](README.md)

기본 설정(Claude Desktop 예시·설정 파일 위치)은 [README의 MCP 클라이언트 연동](../README.md#mcp-클라이언트-연동)을 보십시오. 차단·주입·robots 신호는 [signals.md](signals.md), 사람 인계·로그인 유지·필요할 때만 창은 [human-handoff.md](human-handoff.md)에 있습니다. 대화와 정기 작업이 한 브라우저를 나눠 쓰는 작업 공간·`serve --attach` 는 [human-handoff.md의 함께 쓰기 절](human-handoff.md#대화와-정기-작업이-같은-프로필을-함께-쓰기-serve---attach-작업-공간)에 있습니다.

## 브라우저 방식 (`serve --browser`)

MCP 서버가 여는 브라우저를 시작 옵션으로 고릅니다. Egress 가드·HITL·차단 신호(`data.challenge`)는 네 방식 모두 같습니다.

| 방식 | 무엇 | 언제 |
| :--- | :--- | :--- |
| `headless` (기본) | 화면 없는 Playwright Chromium, 고정 뷰포트 1280×720 | 일반 사이트, CI, 가장 가볍고 빠름 |
| `on-demand` | 평소 headless, **사람 인계·승인 코드 때만 창**(아래 '필요할 때만 창' 절) | 에이전트가 혼자 돌다가 캡차·로그인·고위험 승인 때만 사람을 부를 때 |
| `human` | 창이 보이는 Chromium, 창 크기 그대로(`no_viewport`), `locale=ko-KR` — 서버 수명 내내 창(디버그용 상시 창) | 사람이 계속 지켜보거나 디버그할 때 |
| `user-chrome` | 설치된 Google Chrome 을 자동화 플래그 없이 **전용 프로필**로 띄워 CDP(127.0.0.1)로 붙음 | headless·human 이 막히는 사이트(실측: G마켓은 이 방식만 검색까지 통과) |

```bash
agent-browser serve --browser human
agent-browser serve --browser user-chrome                        # 전용 프로필 ~/.agent-browser/chrome-profile
agent-browser serve --browser user-chrome --chrome-profile ~/ab-shop --keep-open
```

- **위장이 아닙니다.** UA 변경·`navigator.webdriver` 숨기기·stealth 스크립트·캡차 풀기는 하지 않습니다. `user-chrome` 에서 `navigator.webdriver` 가 `false` 인 것은 `--enable-automation` 없이 띄운 평범한 Chrome 이기 때문입니다.
- **전용 프로필만 씁니다.** 평소 Chrome 프로필(`~/Library/Application Support/Google/Chrome` 등)을 `--chrome-profile` 로 주면 서버가 시작을 거부합니다(쿠키·비밀번호 보호, Chrome 136+ 원격 디버깅 제약). 전용 폴더는 권한 700 으로 만듭니다. 그 창에서 한 번 로그인하면 전용 프로필에 남아 다음 실행에도 유지됩니다 — 그래서 `user-chrome` 은 저장 세션 주입(`session login` 세션)을 쓰지 않습니다.
- `user-chrome` 은 새 시크릿 창을 만들지 않고 Chrome 의 기본 창(프로필)을 그대로 씁니다. 처음 열린 빈 탭이 첫 탭이 됩니다. 컨텍스트는 하나만 씁니다.
- 서버가 끝나면 띄운 Chrome 도 닫습니다. `--keep-open` 이면 남겨 둡니다(시작 실패 때는 옵션과 무관하게 닫습니다). `--chrome-profile`·`--keep-open` 은 `--browser user-chrome` 과만 함께 쓸 수 있습니다.
- 서버를 신호로 끝내도(`SIGTERM`·`SIGHUP`·Ctrl+C) 같은 정리를 거쳐 닫고 종료 코드 128+신호 번호로 끝납니다. 정리는 최대 10초이고, 신호를 한 번 더 보내면 기다리지 않고 바로 끝납니다. 강제 종료(`kill -9`)하면 Chrome 창이 남을 수 있음, 그때는 창을 직접 닫으세요.
- 브라우저는 첫 툴 호출 때 뜹니다. 시작 로그는 stderr 한 줄이고 stdout 은 MCP 프로토콜 전용입니다.

Claude Desktop 설정에서 방식을 고르려면 `args` 에 붙입니다(Claude Code 는 `claude mcp add agent-browser -- uv run --directory /절대/경로/agent-browser agent-browser serve --browser user-chrome`).

```json
{
  "mcpServers": {
    "agent-browser": {
      "command": "uv",
      "args": ["run", "--directory", "/절대/경로/agent-browser",
               "agent-browser", "serve", "--browser", "user-chrome"]
    }
  }
}
```

`user-chrome` 은 Google Chrome 이 설치돼 있어야 하고 창이 뜹니다(화면 없는 서버에서는 `headless` 를 쓰십시오).

## 이동 대기 (`serve --nav-settle {on,off}`)

기본 `on` — `click`·`press_key` 등 페이지를 옮길 수 있는 액션 뒤 새 문서가 뜰 때까지 기다립니다(아래 "관찰 → 액션 흐름" 절의 이동 대기 설명). 대가로 이동이 없는 `click`/`press_key` 도 감지 창만큼(약 0.2초) 느려집니다.
`off` 는 이 대기를 끕니다. 액션을 많이 보내고 이동 여부를 스스로 판단하는 에이전트가 속도를 원할 때 씁니다(로컬 실측: 이동 없는 click p50 약 219ms → 26ms).
`off` 면 결과가 떠나는 중인 옛 문서 기준일 수 있고 `data` 에 `nav_wait_ms` 등 대기 키가 붙지 않습니다 — 이동 뒤 `wait_for`(`selector`·`network_idle`·`spa_route`)나 `observe_page` 로 새 문서를 확인할 책임이 호출자에게 있습니다(`stabilize` 는 옛 문서에서 곧바로 만족되므로 이 용도에 맞지 않습니다). 이동이 실제로 성공해도 그 액션 결과가 `E_TIMEOUT`·`E_PAGE_CRASHED` 로 올 수 있고, 그 결과의 `data.challenge`·`last_http_status` 도 옛 문서 기준입니다 — `reobserve_required` 면 다시 관찰해 판단하십시오. `run` 명령은 항상 `on` 입니다.

## 고위험 액션 허용 (`serve --pre-approve`, `--mode`)

무인 모드(기본)에서는 결제·주문·삭제·동의 같은 이름의 클릭, 폼 제출(`type_text(press_enter)`, 폼 안 입력칸에서 `press_key("Enter")`, 한 줄 입력칸에 줄바꿈 입력), 업로드·다운로드를 `E_HITL_UNATTENDED_BLOCKED`로 막습니다. 운영자가 서버를 띄울 때 `--pre-approve <액션>:<요소 이름>`(반복 가능, 예: `--pre-approve "click:결제 진행"`) 또는 `<액션>:*`로 미리 허용합니다. 차단 결과의 `data.pre_approve_hint`는 그 액션을 여는 값이고, 메시지 끝에 운영자용 안내가 붙습니다. `--mode interactive`는 차단 대신 승인 요청(`data.dialog`)을 돌려줍니다.
되돌리기 어려운 메일·계정 조작도 고위험입니다(WS-76): 휴지통 이동·비우기(`휴지통으로 이동`·`휴지통 비우기`·`장바구니 비우기`·`Move to trash`·`Empty trash`), 스팸 신고·표시(`스팸신고`·`Report spam`·`Mark as spam`), 사용자·번호 차단(`사용자 차단`·`차단하기`·`Block user`) — 동사+대상 구로만 맞추므로 폴더 열기(`휴지통`·`Trash`·`Spam`)·안내(`비우기 안내`)·복원(`스팸 아님`·`Not spam`·`차단 해제`·`Unblock`·`Restore`)은 그대로입니다. 활용형(`스팸으로 보내기`·`휴지통으로 옮겨`)·대상 지목(`Block "Alice"`)·동작 버튼 `Trash`(링크·탭의 `Trash` 는 폴더)·항목 조작 목적지(`/messages/42/trash`·`?action=spam`; `/mail/trash`·`?folder=trash` 는 폴더)도 고위험이고, `… 취소`·`… 안내`·`How to …` 는 활용형 뒤에서도 그대로입니다. 단독 `차단`·`비우기` 는 고위험이되 상태 필터 탭의 `차단`·입력칸 옆 `비우기`(검색어 지우기)는 그대로입니다(WS-76 R1).
`click(selector=…)`은 selector 문자열이 아니라 페이지에서 읽은 대상 이름으로 판정합니다. selector가 요소 0개에 맞거나 읽을 수 없으면 판정할 수 없으므로 막습니다. 여러 개(2~50개)에 맞으면 누르지 않고, 후보를 하나씩 같은 규칙으로 판정해 모두 저위험이면 사람 승인 대신 `E_ELEMENT_NOT_FOUND`("selector 가 N개 요소에 맞습니다 — browser_observe_page 로 관찰한 뒤 element_id 로 다시 고르세요", `data.ambiguous_target={count, candidates:[{role, name, visible}] 최대 5개}`, `reobserve_required: true`)로 돌려줍니다 — 에이전트가 관찰 후 `element_id`로 다시 고르면 됩니다. 후보 중 하나라도 고위험(결제·제출·삭제 등 이름·selector·문맥 신호)이거나 50개를 넘으면 지금처럼 판정 불가로 막습니다(승인 경로). 이 이름은 `observe_page`와 같은 규칙(aria-label > aria-labelledby > label > 버튼형 input 의 value > 텍스트 > placeholder > title > alt …)으로 읽어, 같은 요소를 `element_id`·`selector`·포커스 후 `press_key("Enter"/"Space")`로 눌러도 판정이 같습니다.

**가운데가 덮인 대상 (WS-79, #205)** — `click`·`hover`·`check_box`·다운로드 클릭은 누르기 전에 대상 중앙이 대상 자신인지 읽기만 해서 봅니다. 중앙이 고정 띠 등에 덮였고 대상의 다른 자리가 비어 있으면 그 자리를 실제 마우스 입력으로 누르고 `data.click_point`(요소 기준 위치)를 붙입니다(덮지 않은 클릭은 응답 그대로). 비어 있는 자리가 없거나 열린 모달·배경막 밖의 대상이면 예전처럼 기다린 뒤(5초) 덮개를 누르지 않고 `E_ELEMENT_NOT_INTERACTABLE`(`retry_safe: true`) + `data.covered_by={tag, role, layer, modal, transparent, points_checked, points_covered, name}` 로 실패합니다 — 덮은 요소(배너 닫기 등)를 먼저 처리하거나 스크롤한 뒤 다시 시도하세요. 스크립트 클릭으로 대신하지 않습니다. 실패 문구에는 브라우저 엔진의 원문 호출 기록·덮개 HTML 을 싣지 않습니다.

**select 선택지와 라벨로 고르기 (WS-80, #206)** — 관찰에 네이티브 `<select>`(combobox)가 있으면 `data.select_options` 에 요소 순서대로 선택지를 싣습니다: `[{element_id, total, options:[{index, label, selected?, disabled?, group?}], more, multiple?, hint?}]`. select 당 앞 10개 + 선택된 것(원래 `index` 유지, `multiple` 이면 선택된 것 전부), 라벨·optgroup 표제(`group`) 40자, select 12개, 블록 3,000자가 상한이고 넘친 select 수는 `data.select_options_omitted` 입니다. value(코드)는 싣지 않습니다. select 가 없으면 두 키 모두 없습니다(응답 그대로). 라벨은 페이지 글자라 `elements[].name` 과 같은 가림 규칙(도구가 아는 비밀만)·주입 신호 검사를 받고, 크기 상한에서는 요소보다 먼저 줄며 잘린 요소를 가리키는 항목은 지웁니다. `browser_batch` 응답의 현재 관찰에도 같은 두 키가 실립니다.
`select_option(value=…)` 은 ① option value 가 정확히 같은 것(여럿이면 첫 번째), ② 없으면 보이는 라벨(앞뒤 공백·연속 공백·NFKC 정규화 뒤 정확 일치)이 **하나뿐**인 것을 고릅니다 — 값과 라벨이 엇갈리면(`value=A`의 라벨이 `B`) value 가 이깁니다. 맞는 것이 없거나, 같은 라벨이 여럿이거나, 고를 선택지가 비활성(비활성 optgroup 포함)이면 기다리지 않고 `E_OPTION_NOT_FOUND`(`retry_safe: true`) + `data.select_candidates={reason: no_match|ambiguous|disabled, indices}` 로 실패합니다(라벨 글자는 싣지 않음 — 관찰 블록의 `index` 로 다시 고르세요). `index=` 는 예전 그대로이되 범위 밖이면 바로 `reason: out_of_range, total`. 고른 뒤 그 option 이 해석한 option 인지 다시 확인해, 그 사이 선택지가 바뀌었으면 `E_TOCTOU_MISMATCH`(`reobserve_required: true`)입니다. 이미 골라져 있는 값을 다시 고르면(페이지 변화가 없어도) 성공입니다. `multiple` select 는 고른 하나만 남깁니다. 해석·확인·표시는 격리 world 에서 읽어(최상위 문서·iframe·shadow 모두 — iframe 은 그 프레임의 격리 world) 페이지가 `options`·내장 객체를 위조해도 속지 않습니다. 격리 world 를 얻지 못하거나 대상 노드를 하나로 정할 수 없으면 페이지 world 로 내려가지 않고 라벨 해석·사후 확인·그 프레임의 `data.select_options` 를 생략합니다(이때 `value` 는 브라우저 엔진의 기본 고르기 그대로). 사람 승인 판정은 대상 select 기준 그대로입니다.
select2·choices.js 같은 **대리 위젯**(보이는 combobox 가 실제 `<select>` 가 아님)에 `select_option` 을 보내면 숨긴 select 에 값을 넣지 않고 `E_ELEMENT_NOT_INTERACTABLE` + 고정 안내(`data.hint`)로 실패합니다 — `browser_click` 으로 위젯을 열고 `browser_observe_page` 로 다시 관찰한 뒤 나타난 항목(`option`)을 `browser_click` 하세요.
`press_key`의 Enter·Space는 키가 실제로 가는 요소(최상위 문서부터 포커스를 따라 iframe·shadow 안까지)로 판정합니다. Chromium 실측으로 Enter가 폼을 제출하는 포커스 대상 — 폼 안 한 줄 입력칸(text·search·email·number·password·tel·url·date·time·datetime-local·month·week), checkbox·radio·range, `<select>`, 제출 버튼(`<button>`·`input[type=submit|image]`, Space 포함) — 은 모두 폼 제출로 막습니다. 다른 출처 iframe 안 포커스는 포커스를 가진 프레임 사슬이 하나로 확정될 때 그 프레임 안에서 읽고, 포커스를 읽을 수 없거나 사슬을 확정할 수 없거나 폼 안의 알 수 없는 요소(사용자 정의 요소)면 판정 불가로 막습니다.

**판정 근거 — 이름 + 문맥 신호.** 이름에 위험 단어가 없어도 아래 원천 중 하나에 있으면 고위험입니다. `element_id`·`selector`·좌표 클릭과 포커스 후 Enter/Space(요소를 누르는 경우)가 같은 판정 함수를 씁니다.

| 원천(`source`) | 무엇을 보나 | 매칭 |
| :--- | :--- | :--- |
| `name` | 접근성 이름(관찰과 같은 규칙) | 부분 문자열 |
| `text`·`aria` | 보이는 글자와 aria-label **둘 다**(aria '확인' + 보이는 '결제' 불일치 차단) | 부분 문자열 |
| `title`·`child_title`·`alt`·`svg_title`·`child_aria`·`value` | title(대상 자신의 title 은 이름에 글자가 없을 때만 — 아이콘 버튼 `title=결제`는 차단, '저장' 버튼의 설명 문장은 무시), 자손 title·img alt·svg `<title>`·aria-label(신호를 가진 자손만 골라 400개까지 — 빈 요소를 앞에 끼워 피할 수 없음), 버튼 value | 부분 문자열 |
| `pseudo` | CSS `::before`/`::after`의 `content` 글자(대상·앞 자손 20개 + 스타일시트에 글자 든 `content` 규칙이 걸린 자손) | 부분 문자열 |
| `href` | 링크 목적지의 **마지막 경로 조각**과 값 전체가 키워드인 쿼리 값(`/account/delete`, `?action=delete`) — 조회 링크(`/order/123`, `?sort=order_date`)와 권한 명사(`/admin`, `grant`)는 막지 않음 | 단어 토큰 |
| `form_action`·`formaction` | 제출 버튼의 `formaction`, 소속 폼(`form=` 포함)의 `action` 경로 전체 — GET 제출은 쿼리를 보지 않음(`/search?type=order` 통과, `/checkout` 차단) | 단어 토큰(`/payroll-info`는 pay 아님) |
| `id`·`class`·`name_attr`·`testid` | id·class·name·data-testid 를 camelCase·kebab·snake 로 나눈 토큰(`btn-pay`, `deleteAccount`), 아이콘 클래스 사전(`fa-trash`, `bi-credit-card` … — 장바구니 아이콘은 '보기'가 흔해 제외) | 단어 토큰. `submit`·`confirm` 같은 폼 일반어는 제외(로그인 `btn-submit` 과차단 방지) |

같은 출처로 이동하는 링크(`a[href]`, download·`#`·`javascript:` 아님)는 이동일 뿐 부작용이 아니므로 `id`·`class`·`name_attr`·`testid`·`title` 원천을 적용하지 않습니다(`fa-trash` '휴지통 보기' 링크 통과). 이름·보이는 글자·aria·alt·href 는 그대로 봅니다(`/account/delete` 링크는 차단).

모든 텍스트는 NFKC 정규화, 보이지 않는 서식 문자(유니코드 범주 Cf 전체 — zero-width·방향 표시·soft hyphen 등)와 U+034F 제거, 공백 정리 뒤 매칭합니다(`결\u200b제`·`결\u200f제` → 결제). 차단 결과의 `data.gate_basis`(`{name, matched_keyword, source}`)와 메시지의 `출처 <source>`로 왜 막혔는지 알 수 있습니다. 판정할 수 없으면 `source: "unresolved"`입니다.

**좌표 클릭(`click(x, y)`).** 게이트 전에 최상위 화면에서 그 좌표의 요소를 찾고(같은 출처 iframe·open shadow 안까지 따라 내려감), 클릭을 실제로 받는 상호작용 조상(button, 링크, 입력칸, `role=button|link|…`, label, summary, `[onclick]`, `[tabindex]`)의 이름·문맥으로 판정합니다. 사전 승인 값은 해석된 이름(`click:<이름>`, `data.pre_approve_hint`)입니다. 좌표에 요소가 없거나, 다른 출처 iframe 위이거나, closed shadow 위(안을 읽을 수 없음)이거나, 해석 중 오류가 나거나, 폼 안의 비상호작용 요소 위면 판정 불가로 막습니다. 캔버스처럼 상호작용 조상이 없고 폼 밖이며 위험 신호도 없는 곳은 통과시키고 결과 `data.gate_basis.coordinate_target: "non_interactive"`로 알립니다(Tier-2 SoM의 본래 용도).

**페이지가 기본 기능을 지워도(격리 world 판정, WS-67).** 페이지 스크립트가 `HTMLInputElement.prototype.form`·`Element.prototype.closest`·`matches` 를 지우거나 `null` 을 돌려주게 바꾸면 페이지 안에서 읽는 판정은 '폼 밖'으로 속습니다. 그래서 `press_key`(포커스 요소)와 좌표 클릭은 같은 대상을 CDP 격리 world(`Page.createIsolatedWorld` — DOM 은 같고 JS 기본 기능은 원본)에서 한 번 더 판정합니다. 포커스는 격리 world 의 `document.activeElement` 에서 같은 출처 iframe(`contentDocument.activeElement`)·open shadow(`shadowRoot.activeElement`)를 따라 내려가고, 좌표는 `DOM.getNodeForLocation`(UA shadow 제외)이 맞힌 노드에서 상호작용 조상으로 올라갑니다. 두 판정을 각각 위험 분류해 더 높은 쪽(판정 불가 > 폼 제출 > 위험 신호 > 없음)을 씁니다. 격리 판정을 못 하면(다른 출처 iframe 안 포커스, 맞힌 노드가 iframe 자신, 오류) 기존 판정만 써서 지금보다 더 막지 않습니다. `element_id`·`selector` 클릭, `type_text` 의 줄바꿈 제출 판정, 승인 결속(`_MEANING_JS`)은 아직 페이지 안 판정만 씁니다(후속 이슈 #163).

**남은 한계.** ① `select_option`·`check_box`가 `onchange`로 폼을 자동 제출하는 페이지는 미리 알 수 없어 이름 판정만 합니다. ② 캔버스에 그린 결제 버튼은 DOM 신호가 없어 좌표 클릭이 통과합니다. ③ 서버 쪽에서만 아는 위험(무해한 이름·경로의 API 가 실제로 결제)은 알 수 없습니다. ④ 키워드 사전 기반이라 '결제 내역'·'주문 목록'·'Remove filter' 같은 조회·UI 조작도 막힙니다 — 필요한 것은 `--pre-approve`로 엽니다. ⑤ 게이트 판정과 실제 클릭 사이의 DOM·JS 변경(호버 시 글자 교체, 클릭을 아래로 전달하는 덮개)은 막지 못합니다. 캔버스 화면에서도 엄격하게 하려면 무인 모드에서 좌표 클릭을 쓰지 않거나 `--mode interactive`로 사람이 확인하게 하세요.

`download_file`의 `save_dir`는 절대 경로여야 합니다. 상대 경로는 서버 작업 폴더 기준이 되어 저장 위치를 알 수 없으므로 `E_DOWNLOAD_FAILED`로 거부하고, 절대 경로의 `..`·심볼릭 링크는 정규화한 경로에 저장합니다(`downloaded_path`).

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

## 관찰 → 액션 흐름

`observe_page`가 반환하는 `element_id`와 `epoch`을 액션에 그대로 넘깁니다.

페이지를 열거나 이동·새로고침·탭 전환한 뒤에는 먼저 `browser_observe_page`를 부르십시오. 관찰 없이 쓴 selector가 요소를 못 찾거나 모호해서 실패하면 `data.hint`로 관찰을 안내합니다. 성공한 `extract`도 텍스트 항목의 절반 이상이 공백이거나 전체 공백 제외 글자가 8자 미만이면 안내합니다. 정상 결과에는 붙이지 않고 같은 페이지에서는 한 번만 보냅니다. 이동하면 다시 한 번 보낼 수 있습니다.

상품·가격이 화면의 상호작용 요소에 없더라도 관찰 결과의 `data.page_data`에 **페이지 JSON-LD 요약**이 있을 수 있습니다. Product·Offer/AggregateOffer·Flight·Event·Article/NewsArticle·BreadcrumbList를 배열·`@graph`·중첩에서 찾고, 이름·sku·brand·offers(가격·통화·재고 상태, AggregateOffer의 가격 범위)·평점 요약을 싣습니다. BreadcrumbList는 이름만 제공합니다. 항목은 최대 5개, 압축 JSON은 1,200자 이하이고 문자열은 제어문자를 제거해 이름·brand 128자, sku·가격 등 64자, URL 256자로 제한합니다. offer의 `url`은 같은 출처(scheme·host·port)의 HTTP(S) 주소만 남깁니다. 없거나 깨진 JSON-LD는 해당 필드를 생략합니다. 이는 사이트가 제공한 값이므로 현재 결제 가격을 보증하지 않으며, 다른 웹 텍스트처럼 `data.injection_suspected` 검사 대상입니다. `extract` 인자는 그대로입니다.

응답 형식: 응답(`ActionResult` JSON)에서 **빠진 필드는 계약 기본값**입니다(`healed=false`, `downloaded_path`·`popup_tab_id`·`error_code`·`error_message`=`null`, `data`=`{}`, 관찰 요소의 `value`=`null`·`is_shadow`=`false`). `success`·`action`·`current_url`·`snapshot_epoch`·`tab_id`·`retry_safe`·`reobserve_required`는 항상 있고, 실패면 `error_code`·`error_message`도 있습니다. `data` 안의 `null`은 그대로 싣습니다(`data.challenge: null` = 차단 없음). 계약 모델(`ActionResult.model_validate`)로 다시 읽으면 빠짐없는 결과와 같은 객체입니다.

결과 크기 상한: `observe_page`·`extract` 응답이 `serve --max-result-chars`(기본 20,000자)를 넘으면 **항목 경계**(관찰 요소는 점수 순, 추출 행은 문서 순 앞쪽)에서 잘라 싣고 `data.truncated`에 `total_items`·`returned_items`·`total_chars`·`returned_chars`·`hint`를 붙입니다. `axtree_summary`도 남긴 요소들로 다시 만들어 요소와 같은 지점에서 끊기고 `token_count`도 그 기준입니다(이름에 줄바꿈이 있어도 요약에 `elements` 밖 id 가 남지 않음, WS-74). 기본값 근거: Claude Code 는 MCP 도구 결과가 25,000 토큰을 넘으면 결과를 통째로 버리고, 한글 본문은 1자 ≈ 1토큰(cl100k 실측 0.97)이라 2만 자면 여유가 있습니다(비교 시험에서 `observe_page(force_full_tree)` 143,456자·`extract` 58,620자가 버려졌습니다). 추출 항목 **하나**가 이미 상한을 넘으면 그 항목의 `text`만 글자 묶음(이모지·결합 문자) 경계, 가능하면 공백에서 자르고 `text_truncated: true`·`text_chars`(원래 글자 수), `data.truncated.item_text_truncated: true`를 붙입니다. 더 보려면 `extract`의 `selector`를 좁히거나 `observe_page`의 `prune_top_n`을 쓰십시오. `returned_chars` 는 그 응답 최종 wire(가림·창 보고·주입 신호까지 붙인 JSON) 길이입니다. `browser_batch` 응답도 같은 상한을 지킵니다(WS-78, #200): 묶음 메타(`batch`·`step_result`·`next`·성공/오류 문구)를 먼저 재서 안쪽 관찰의 실효 예산을 그만큼 줄이므로 성공·멈춤(`step_result` 포함)·`observation_error`·주입 신호 어느 출구든 최종 wire ≤ 상한이고, 묶음 응답의 `data.truncated.returned_chars` 는 **묶음 응답 전체**의 최종 wire 길이입니다(`hint` 에는 설정 상한과 실효 예산이 함께 적힙니다). 그래도 넘으면 관찰을 **다시 실행하지 않고** 처음 얻은 관찰(원본)에서 더 작은 예산으로 다시 맞추고(관찰 1회 — 이미 검출한 `injection_suspected`·`challenge`·`handoff`·`robots`·`page_not_found`·`dialogs` 신호는 그대로 남습니다), 바깥 메타만으로 넘치면(예: 긴 미지원 키의 `E_KEY_PRESS_FAILED` 문구) `step_result`·`batch.steps` 의 긴 글자를 가린 뒤 200자로 자르고(`data.truncated.meta_truncated: true`), 그래도 넘으면 현재 관찰을 생략합니다(`data.observation_omitted` 고정 문구 + `data.truncated.observation_omitted: true`, 신호는 유지). 마지막으로 `step_result` 를 성공 여부·오류 코드만 남긴 고정 모양(`omitted`)으로 줄입니다. 미지원 키 오류 문구 자체도 키를 가린 뒤 고정 길이로 자릅니다(WS-78 R1).
없는 주소 신호(`data.page_not_found`, WS-78 #139): 지금 화면의 문서가 서버에서 404·410 으로 왔고(판정용 상태 — 현재 URL 이 그 응답 URL 과 같을 때만, pushState 로 옮긴 SPA 화면은 제외) 차단·캡차(`data.challenge`)가 아니면 `{"status": 404|410, "hint": "없는 주소 — 차단 아님, 다른 경로를 찾으세요"}` 를 붙입니다. `data.challenge` 를 싣는 액션 전부(이동·뒤로·새로고침·클릭·키·입력·선택·체크·관찰·탭·대기)와 `browser_batch` 응답 관찰에 붙고, 403·429·451 같은 차단 신호와 겹치지 않습니다. 접근이 막힌 것이 아니니 다른 경로(검색·상위 경로·사이트 안 링크)를 찾으십시오. 200 으로 오는 SPA 의 '없는 페이지' 화면은 판정하지 않습니다(신호 없음).

```
observe_page  ->  @e3 (button "로그인"), epoch=0
click         ->  element_id="@e3", epoch=0
```

페이지가 바뀌면 `epoch`이 올라가고 이전 `element_id`는 무효가 됩니다. 오래된 ID로 액션을 보내면 `E_TOCTOU_MISMATCH`로 거부됩니다 — 다른 요소를 잘못 누르는 것보다 낫다는 판단입니다.

같은 `epoch` 안에서도 관찰한 자리의 요소가 바뀌었으면(이름이나 role이 달라짐 — 광고 로테이션으로 '결제하기' 자리에 '회원 탈퇴'가 들어온 경우) 부작용 액션(`click`·`type_text`·`select_option`·`check_box` 등, 읽기 액션 `observe_page`·`take_screenshot`·`scroll`·`hover`·`wait_for`·`extract` 밖 전부)은 자가 치유를 시도하지 않고 `E_TOCTOU_MISMATCH`, `reobserve_required=true`, `data.element_changed={before, after}`·`data.hint`로 거부합니다. testid가 남았거나 이름이 비슷해도('결제하기 취소') 마찬가지입니다. 요소가 사라진 경우는 자가 치유를 시도하지만, 부작용 액션은 찾은 대체 요소의 role과 이름(공백·영문 대소문자만 무시)이 원래와 같을 때만 누릅니다 — 같은 `data-testid`를 단 '회원 탈퇴'나 이름이 비슷한 '결제하기 취소'가 다른 자리에 들어왔다면 같은 방식으로 거부합니다. 읽기 액션은 이름이 달라도 치유합니다. 대가로 같은 버튼의 이름만 바뀐 경우도 다시 관찰해야 합니다: '장바구니(1)'→'장바구니(2)' 카운터, '장바구니'→'장바구니3' 배지, 누르기 직전 '구매하기'→'처리 중…' 로딩 문구, '한국어'→'English' 다국어 토글, 그리고 이름이 바뀌면서 위치도 옮겨진 버튼(사라진 경우로 판정되지만 이름이 달라 누르지 않음). 안내에 실리는 이름·role은 제어문자를 보이는 표기로 바꾸고 80자로 자릅니다.

`click`(좌표 포함)·`press_key`·`select_option`·`check_box`·`type_text(press_enter)` 뒤 200ms 안에 메인 프레임 문서 요청이 시작되면, 새 문서가 커밋되고 `domcontentloaded`가 될 때까지(상한 8초) 기다린 뒤 결과를 돌려줍니다. 결과 `data`에 `nav_wait_ms`·`nav_committed`(상한 초과면 `nav_timed_out`, 204·다운로드·요청 실패면 `nav_aborted`)가 남고, 새 문서가 떴으면 `reobserve_required=true`입니다. 떠나는 중인 페이지를 관찰해 판단하지 않게 하려는 것입니다(G마켓 실측: Enter 뒤 결과 문서가 0.7~0.9초 늦게 와 홈 화면에서 scroll을 골랐다). 대신 이동이 없는 이 액션들은 감지 창만큼(약 200ms) 느려집니다. 링크·리다이렉트 클릭은 Playwright `click()`이 커밋까지 기다린 뒤 반환하므로 `nav_wait_ms`가 0에 가깝게 찍힙니다 — 기다리지 않았다는 뜻이 아니라 `click()` 안에서 기다린 것입니다.

요소 액션의 성공 판정(사후조건)은 대상 문서의 변화 외에 다음도 효과로 봅니다: 새 메인 문서 커밋(`signals`에 `navigated: …`, 예: `type_text(press_enter)`로 폼이 결과 페이지로 감), `switch_frame`으로 들어간 프레임 안 액션이 바꾼 최상위 문서(`top:…`)나 연 새 탭, 네이티브 다이얼로그(`dialog_opened:<type>`, `data.dialogs`에 종류·문구·`accepted`/`dismissed`). 아무 변화도 없는 클릭은 여전히 `E_TIMEOUT`입니다. `handle_dialog`를 먼저 부르지 않은 다이얼로그는 거절됩니다(beforeunload 는 수락). 다이얼로그는 원인을 가리지 않습니다 — 액션 중(대기 창 포함) 뜬 다이얼로그는 무관한 타이머가 띄운 것이어도 효과로 기록됩니다(`data.dialogs`의 문구로 확인하십시오).
프레임 안 액션에서 최상위 문서의 본문·노드 변화(`top:text_changed`·`top:dom_delta`)는 최상위가 스스로 바뀌지 않은 부분에서 일어났을 때만 효과로 인정합니다. 최상위의 변화 시각을 노드별로 기록해, 직전 액션 뒤 대기 동안(관찰·추출·스크린샷·대기 액션은 기록을 끊지 않음)이나 액션 뒤 관찰 창에서도 바뀐 노드(시계·광고 로테이션)는 자발 변화로 뺍니다(`data.top_change_attribution`). 관찰 창(0.3초)은 직전 액션과의 간격이 0.3초보다 짧을 때만 기다립니다 — 그런 연속 액션에서만 약 0.3초 느려집니다. 자발 변화와 같은 노드를 바꾼 효과는 인정하지 않아 `E_TIMEOUT`이 될 수 있습니다(거짓 성공보다 거짓 실패 쪽). `top:url_changed`·새 탭은 그대로 인정합니다. 프레임 안에서 `type_text(press_enter)`·`press_key`로 **그 프레임 문서**가 이동해도 `navigated: …`로 인정하고 `data.nav_frame="current_frame"`을 붙입니다. `--nav-settle off`면 이동 대기가 없으므로 `navigated:` 신호도 붙지 않아, 폼 이동 뒤 입력값 비교가 새 문서에서 이루어져 `E_TIMEOUT`이 올 수 있습니다 — 이때는 `observe_page`로 확인하십시오.

`scroll` 결과 `data`에는 요청량 `scrolled` 외에 실제 세로 이동량 `scrolled_px`(위로는 음수)가 붙고, 움직이지 않았으면(이미 끝이거나 창 스크롤이 없는 페이지) `no_effect: true`와 `hint`가 붙습니다. 성공 판정과 `reobserve_required`(문서 높이 변화) 규칙은 그대로입니다.

`switch_frame`의 `frame_selector`는 지금 들어가 있는 프레임 기준으로 먼저 찾고, 없으면 최상위 문서 기준으로 찾습니다. 그래서 깊은 프레임에서 현재 프레임에 없는 셀렉터를 주면 "없음"이 아니라 최상위 기준으로 찾은 더 얕은 프레임으로 되돌아갈 수 있습니다 — 결과의 `resolved_from`(`current_frame`/`root`)과 `frame_depth`로 확인하십시오. 결과 `data`에 `frame_url`·`frame_depth`·`frame_path`·`child_frames`(`selector_hint`, `url`)가 붙고, 못 찾으면 현재 프레임 URL과 그 안의 iframe 목록을 돌려줍니다. 최상위로 돌아가려면 `{"to_main": true}`만 보냅니다. 프레임 안의 `take_screenshot`은 최상위 페이지를 찍고 `data.frame_bbox`에 프레임 영역을 싣습니다.

## 사람 인계 응답 (`data.human_handoff`, WS-77)

`browser_control_request`·`browser_control_status`·`browser_control_wait`, 그리고 사람이 조작 중이라 거부된 액션 응답에 `data.human_handoff` 가 붙습니다(요청·사람 조작권·막 끝난 인계가 없으면 없음 — 인계를 쓰지 않는 응답은 그대로). 흐름은 [사람 인계의 창 안 [다 했어요]](human-handoff.md#사람-인계--조작권과-승인-mcp)에 있습니다.

| 필드 | 뜻 |
| :--- | :--- |
| `request_id` | 이 인계의 요청 id |
| `state` | `waiting_human`(요청 중) · `human_holding`(사람이 take) · `released` · `request_expired`, 창 없는 서버의 거부는 `no_window` |
| `expires_at` | 요청 만료 시각(ISO, on-demand 가 요청으로 연 창만 — 다른 방식은 만료가 없어 `null`). 끝난 인계(`released`·`request_expired`)도 `null`(WS-78) |
| `control_window` | (끝난 인계에도 지금 상태로 실림, WS-78) 작은 'agent-browser' 창 상태: `open` · `opening`(다시 띄우는 중) · `unavailable`(닫힘·이동 뒤 한 번 다시 띄운 뒤에도 없어짐 → CLI 안내) · `none` |
| `tell_user` | 사람에게 그대로 전할 **고정 문구**(페이지·사유 글자 없음). 창이 있으면 "브라우저 옆에 뜬 작은 'agent-browser' 창에서 … [다 했어요]를 누르세요", 창이 없어지면 `agent-browser control release` 안내, headless 서버면 창 있는 방식 안내. 끝난 인계는 종료용 고정 문구(`released`: 조작이 끝났다·더 할 일 없음, `request_expired`: 요청 만료 — 진행 중 안내를 다시 쓰지 않음, WS-78) |
| `human_activity_seen` | 요청 뒤 브라우저에서 활동이 보였는지. **활동 = 네트워크 요청(본문을 싣는 POST/PUT)·비밀번호 칸 제출·쿠키 변화** 뿐입니다 — 칸에 입력만 하고 제출하지 않았거나 클릭만 한 것은 활동이 아닙니다. `seen`(위 사건 중 하나라도) · `not_seen`(브라우저 수준 감시가 모든 탭에 온전히 걸렸는데 위 사건이 없음) · `unknown`(감시 설치 실패·일부 탭만 설치·쿠키 기준점 없음·아직 판정 불가). **반납 근거가 아닌 관측 신호**이며 페이지 스크립트가 만든 요청·쿠키도 `seen` 이 됩니다. `not_seen` 이어도 다시 관찰해 확인하십시오 |
| `tab_origins` | `{changed, before, now}` — 요청 때와 지금의 탭 origin(각 최대 5개). **호출한 작업 공간의 탭만** 셉니다(다른 작업 공간 탭은 `tab_control list` 의 `other_workspace_tabs` 개수로만) |
| `release_source` | 반납 출처(끝난 인계만): `window_done`(창의 [다 했어요]) · `cli` · `login_finish` · `window_closed` · `server` |
| `next_step` | 기다리는 중이면 `browser_control_wait`, 끝났으면 "다시 관찰해 결과 확인" |

`browser_control_wait` 은 요청 만료(on-demand)도 바로 `changed: "request_expired"` 로 돌려줍니다(전에는 `timeout`). 만료·반납 응답의 `human_activity_seen` 이 `seen` 이어도 로그인됐다고 단정하지 말고 다시 관찰해 확인하십시오.
