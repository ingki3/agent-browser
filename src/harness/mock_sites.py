"""Mock 사이트 25종 정의 및 정적 서버 (src/AGENTS.md §5 WS-6 수용 기준).

13대 필수 시나리오를 22종 사이트에 분산 배치하고, Stage 4 Tier-2(SoM)
실패 유발 페이지 3종(icon-buttons / obfuscated-labels / canvas-ui)을 더한다. 각 사이트는 단일 HTML
문자열로 생성되며, 외부 네트워크 의존이 전혀 없다(플레이키 방지).

시나리오 커버리지는 `Scenario` Enum과 각 사이트의 `scenarios` 선언으로
표현되며, `harness.selfcheck`가 13종 전수 커버를 기계 검증한다.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, List, Optional, Tuple


class Scenario(str, Enum):
    """13대 필수 시나리오 (src/AGENTS.md §5)."""

    LOGIN_FORM = "login_form"  # 로그인 폼 / 2FA
    MULTI_STEP_FORM = "multi_step_form"  # 다단계 폼 + 파일 업로드
    CSV_DOWNLOAD = "csv_download"
    NESTED_IFRAME = "nested_iframe"
    OPEN_SHADOW_DOM = "open_shadow_dom"
    CLOSED_SHADOW_DOM = "closed_shadow_dom"
    INFINITE_SCROLL = "infinite_scroll"
    AD_ROTATION = "ad_rotation"  # 200ms 광고 로테이션 동적 노드
    NATIVE_DIALOG = "native_dialog"  # Alert / Confirm / Prompt
    POPUP_TAB = "popup_tab"
    SESSION_EXPIRY = "session_expiry"  # HTTP 401 리다이렉트
    SPA_ROUTING = "spa_routing"
    LAZY_LOADING = "lazy_loading"


@dataclass(frozen=True)
class MockSite:
    """단일 Mock 사이트 정의."""

    site_id: str
    title: str
    scenarios: Tuple[Scenario, ...]
    html: str
    #: Recall 골든셋 정답 요소의 접근성 이름 (없으면 골든셋 비대상)
    golden_target: Optional[str] = None
    #: 골든셋 정답 요소의 role
    golden_role: Optional[str] = None


def _page(title: str, body: str, head: str = "") -> str:
    """공통 HTML 스캐폴드. 외부 리소스를 일절 참조하지 않는다."""
    return (
        "<!DOCTYPE html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
        f"<title>{title}</title>{head}</head><body>{body}</body></html>"
    )


# ---------------------------------------------------------------------------
# 사이트 정의 (20종)
# ---------------------------------------------------------------------------


def _build_sites() -> List[MockSite]:
    sites: List[MockSite] = []

    # 01. 기본 로그인 폼
    sites.append(
        MockSite(
            site_id="s01_login",
            title="로그인",
            scenarios=(Scenario.LOGIN_FORM,),
            golden_target="로그인",
            golden_role="button",
            html=_page(
                "로그인",
                """
                <h1>계정 로그인</h1>
                <form id="login-form">
                  <label for="user">아이디</label>
                  <input id="user" name="user" type="text">
                  <label for="pw">비밀번호</label>
                  <input id="pw" name="pw" type="password">
                  <button id="submit" type="submit">로그인</button>
                </form>
                <a href="/s01_login/help">비밀번호 찾기</a>
                """,
            ),
        )
    )

    # 02. 2FA 코드 입력
    sites.append(
        MockSite(
            site_id="s02_twofactor",
            title="2단계 인증",
            scenarios=(Scenario.LOGIN_FORM,),
            golden_target="인증 확인",
            golden_role="button",
            html=_page(
                "2단계 인증",
                """
                <h1>2단계 인증</h1>
                <p>등록된 기기로 전송된 6자리 코드를 입력하세요.</p>
                <input id="otp" name="otp" inputmode="numeric" maxlength="6">
                <button id="verify">인증 확인</button>
                <button id="resend">코드 재전송</button>
                """,
            ),
        )
    )

    # 03. 다단계 폼 + 파일 업로드
    sites.append(
        MockSite(
            site_id="s03_multistep",
            title="신청서 작성",
            scenarios=(Scenario.MULTI_STEP_FORM,),
            golden_target="다음 단계",
            golden_role="button",
            html=_page(
                "신청서 작성",
                """
                <h1>신청서 (1/3단계)</h1>
                <form id="step1">
                  <label for="name">성명</label><input id="name">
                  <label for="doc">증빙 서류</label><input id="doc" type="file">
                  <button id="next">다음 단계</button>
                </form>
                """,
            ),
        )
    )

    # 04. CSV 다운로드
    sites.append(
        MockSite(
            site_id="s04_download",
            title="보고서 다운로드",
            scenarios=(Scenario.CSV_DOWNLOAD,),
            golden_target="CSV 내려받기",
            golden_role="link",
            html=_page(
                "보고서 다운로드",
                """
                <h1>월간 보고서</h1>
                <a id="dl" href="/s04_download/report.csv" download>CSV 내려받기</a>
                """,
            ),
        )
    )

    # 05. 중첩 iframe
    sites.append(
        MockSite(
            site_id="s05_iframe",
            title="중첩 프레임",
            scenarios=(Scenario.NESTED_IFRAME,),
            html=_page(
                "중첩 프레임",
                """
                <h1>결제 위젯</h1>
                <iframe id="outer" src="/s05_iframe/outer" width="400" height="300"></iframe>
                """,
            ),
        )
    )

    # 06. Open Shadow DOM
    sites.append(
        MockSite(
            site_id="s06_open_shadow",
            title="Open Shadow",
            scenarios=(Scenario.OPEN_SHADOW_DOM,),
            html=_page(
                "Open Shadow",
                """
                <h1>Open Shadow 위젯</h1>
                <div id="host"></div>
                <script>
                  const host = document.getElementById('host');
                  const root = host.attachShadow({mode: 'open'});
                  root.innerHTML = '<button id="inner-open">주문 확정</button>';
                </script>
                """,
            ),
        )
    )

    # 07. Closed Shadow DOM (CDP pierce 없이는 접근 불가)
    sites.append(
        MockSite(
            site_id="s07_closed_shadow",
            title="Closed Shadow",
            scenarios=(Scenario.CLOSED_SHADOW_DOM,),
            html=_page(
                "Closed Shadow",
                """
                <h1>Closed Shadow 위젯</h1>
                <div id="host"></div>
                <script>
                  const host = document.getElementById('host');
                  const root = host.attachShadow({mode: 'closed'});
                  root.innerHTML = '<button id="inner-closed">비공개 결제</button>';
                </script>
                """,
            ),
        )
    )

    # 08. 무한 스크롤
    sites.append(
        MockSite(
            site_id="s08_infinite",
            title="무한 스크롤 목록",
            scenarios=(Scenario.INFINITE_SCROLL,),
            html=_page(
                "무한 스크롤 목록",
                """
                <h1>상품 목록</h1>
                <ul id="list"></ul>
                <script>
                  let page = 0;
                  function load() {
                    const ul = document.getElementById('list');
                    for (let i = 0; i < 20; i++) {
                      const li = document.createElement('li');
                      li.textContent = '상품 ' + (page * 20 + i);
                      ul.appendChild(li);
                    }
                    page++;
                  }
                  load();
                  window.addEventListener('scroll', () => {
                    if (window.innerHeight + window.scrollY >= document.body.offsetHeight - 50) load();
                  });
                </script>
                """,
            ),
        )
    )

    # 09. 200ms 광고 로테이션 (staleness / actionability 스트레스)
    sites.append(
        MockSite(
            site_id="s09_ad_rotation",
            title="광고 로테이션",
            scenarios=(Scenario.AD_ROTATION,),
            golden_target="장바구니 담기",
            golden_role="button",
            html=_page(
                "광고 로테이션",
                """
                <h1>특가 상품</h1>
                <div id="ad">광고 A</div>
                <button id="cart">장바구니 담기</button>
                <script>
                  let n = 0;
                  setInterval(() => {
                    const ad = document.getElementById('ad');
                    ad.remove();
                    const fresh = document.createElement('div');
                    fresh.id = 'ad';
                    fresh.textContent = '광고 ' + (++n);
                    document.body.insertBefore(fresh, document.getElementById('cart'));
                  }, 200);
                </script>
                """,
            ),
        )
    )

    # 10. 네이티브 다이얼로그
    sites.append(
        MockSite(
            site_id="s10_dialog",
            title="네이티브 다이얼로그",
            scenarios=(Scenario.NATIVE_DIALOG,),
            golden_target="계정 삭제",
            golden_role="button",
            html=_page(
                "네이티브 다이얼로그",
                """
                <h1>계정 설정</h1>
                <button id="del" onclick="confirm('정말 삭제하시겠습니까?')">계정 삭제</button>
                <button id="alert" onclick="alert('저장되었습니다')">저장</button>
                """,
            ),
        )
    )

    # 11. 팝업 새 탭
    sites.append(
        MockSite(
            site_id="s11_popup",
            title="팝업 탭",
            scenarios=(Scenario.POPUP_TAB,),
            golden_target="약관 새 창으로 보기",
            golden_role="link",
            html=_page(
                "팝업 탭",
                """
                <h1>이용 약관</h1>
                <a id="popup" href="/s11_popup/terms" target="_blank">약관 새 창으로 보기</a>
                """,
            ),
        )
    )

    # 12. 세션 만료 (401)
    sites.append(
        MockSite(
            site_id="s12_session_expiry",
            title="세션 만료",
            scenarios=(Scenario.SESSION_EXPIRY,),
            html=_page(
                "세션 만료",
                """
                <h1>대시보드</h1>
                <p>보호된 리소스에 접근하려면 재인증이 필요합니다.</p>
                <a id="protected" href="/s12_session_expiry/protected">보호 리소스 열기</a>
                """,
            ),
        )
    )

    # 13. SPA 클라이언트 라우팅
    sites.append(
        MockSite(
            site_id="s13_spa",
            title="SPA 라우팅",
            scenarios=(Scenario.SPA_ROUTING,),
            golden_target="설정으로 이동",
            golden_role="button",
            html=_page(
                "SPA 라우팅",
                """
                <h1 id="view">홈</h1>
                <button id="go-settings">설정으로 이동</button>
                <script>
                  document.getElementById('go-settings').addEventListener('click', () => {
                    history.pushState({}, '', '/s13_spa/settings');
                    document.getElementById('view').textContent = '설정';
                  });
                </script>
                """,
            ),
        )
    )

    # 14. 지연 로딩
    sites.append(
        MockSite(
            site_id="s14_lazy",
            title="지연 로딩",
            scenarios=(Scenario.LAZY_LOADING,),
            golden_target="지연 로딩 버튼",
            golden_role="button",
            html=_page(
                "지연 로딩",
                """
                <h1>지연 콘텐츠</h1>
                <div id="slot">불러오는 중...</div>
                <script>
                  setTimeout(() => {
                    document.getElementById('slot').innerHTML =
                      '<button id="lazy-btn">지연 로딩 버튼</button>';
                  }, 300);
                </script>
                """,
            ),
        )
    )

    # 15. 로그인 + 세션 만료 복합
    sites.append(
        MockSite(
            site_id="s15_login_expiry",
            title="재인증 흐름",
            scenarios=(Scenario.LOGIN_FORM, Scenario.SESSION_EXPIRY),
            html=_page(
                "재인증 흐름",
                """
                <h1>세션이 만료되었습니다</h1>
                <form id="relogin">
                  <input id="user2" name="user"><input id="pw2" type="password">
                  <button id="relogin-btn">다시 로그인</button>
                </form>
                <a id="protected2" href="/s15_login_expiry/protected">이전 페이지로</a>
                """,
            ),
        )
    )

    # 16. iframe 내부 Shadow DOM 복합
    sites.append(
        MockSite(
            site_id="s16_iframe_shadow",
            title="프레임 내 Shadow",
            scenarios=(Scenario.NESTED_IFRAME, Scenario.OPEN_SHADOW_DOM),
            html=_page(
                "프레임 내 Shadow",
                """
                <h1>임베드 위젯</h1>
                <iframe id="widget" src="/s16_iframe_shadow/widget" width="360" height="240"></iframe>
                """,
            ),
        )
    )

    # 17. 무한 스크롤 + 지연 로딩 복합
    sites.append(
        MockSite(
            site_id="s17_feed",
            title="피드",
            scenarios=(Scenario.INFINITE_SCROLL, Scenario.LAZY_LOADING),
            html=_page(
                "피드",
                """
                <h1>뉴스 피드</h1>
                <div id="feed"></div>
                <script>
                  let i = 0;
                  function add() {
                    const d = document.createElement('article');
                    d.textContent = '기사 ' + (i++);
                    document.getElementById('feed').appendChild(d);
                  }
                  for (let k = 0; k < 15; k++) add();
                  setTimeout(() => { for (let k = 0; k < 10; k++) add(); }, 250);
                  window.addEventListener('scroll', add);
                </script>
                """,
            ),
        )
    )

    # 18. 다단계 폼 + SPA 라우팅 복합
    sites.append(
        MockSite(
            site_id="s18_wizard",
            title="가입 마법사",
            scenarios=(Scenario.MULTI_STEP_FORM, Scenario.SPA_ROUTING),
            html=_page(
                "가입 마법사",
                """
                <h1 id="step-title">1단계: 기본 정보</h1>
                <input id="email" type="email">
                <input id="attach" type="file">
                <button id="wizard-next">계속</button>
                <script>
                  document.getElementById('wizard-next').addEventListener('click', () => {
                    history.pushState({}, '', '/s18_wizard/step2');
                    document.getElementById('step-title').textContent = '2단계: 상세 정보';
                  });
                </script>
                """,
            ),
        )
    )

    # 19. 다이얼로그 + 팝업 복합
    sites.append(
        MockSite(
            site_id="s19_checkout",
            title="결제 확인",
            scenarios=(Scenario.NATIVE_DIALOG, Scenario.POPUP_TAB),
            golden_target="결제 진행",
            golden_role="button",
            html=_page(
                "결제 확인",
                """
                <h1>주문 확인</h1>
                <button id="pay" onclick="confirm('342,000원을 결제합니다')">결제 진행</button>
                <a id="receipt" href="/s19_checkout/receipt" target="_blank">영수증 미리보기</a>
                """,
            ),
        )
    )

    # 20. 광고 로테이션 + Closed Shadow 복합 (최난도)
    sites.append(
        MockSite(
            site_id="s20_stress",
            title="복합 스트레스",
            scenarios=(Scenario.AD_ROTATION, Scenario.CLOSED_SHADOW_DOM, Scenario.CSV_DOWNLOAD),
            html=_page(
                "복합 스트레스",
                """
                <h1>대시보드</h1>
                <div id="banner">배너 0</div>
                <div id="host2"></div>
                <a id="export" href="/s20_stress/export.csv" download>내보내기</a>
                <script>
                  const root = document.getElementById('host2').attachShadow({mode: 'closed'});
                  root.innerHTML = '<button id="hidden-action">숨은 실행</button>';
                  let c = 0;
                  setInterval(() => {
                    document.getElementById('banner').textContent = '배너 ' + (++c);
                  }, 200);
                </script>
                """,
            ),
        )
    )

    # 21. 폼 위젯 모음 (액션 툴 전수 검증용)
    sites.append(
        MockSite(
            site_id="s21_widgets",
            title="폼 위젯",
            scenarios=(Scenario.MULTI_STEP_FORM,),
            html=_page(
                "폼 위젯",
                """
                <h1>배송 정보</h1>
                <button id="help" title="도움말">도움말</button>
                <div id="tip" style="display:none">툴팁 내용</div>

                <label for="agree">약관 동의</label>
                <input type="checkbox" id="agree" aria-label="약관 동의">

                <label for="ship">배송 방법</label>
                <select id="ship" aria-label="배송 방법">
                  <option value="standard">일반 배송</option>
                  <option value="express">특급 배송</option>
                </select>

                <label for="attach">첨부 파일</label>
                <input type="file" id="attach" aria-label="첨부 파일">

                <script>
                  document.getElementById('help').addEventListener('mouseenter', () => {
                    document.getElementById('tip').style.display = 'block';
                  });
                </script>
                """,
            ),
        )
    )

    # 22. 고밀도 노이즈 사이트 (Recall@20 프루닝 실효 검증용)
    #     후보 요소가 20개를 크게 넘어야 Top-N 프루닝이 실제로 동작한다.
    #     정답 버튼은 노이즈 한가운데(DOM 순서상 뒤쪽)에 배치한다.
    _noise_links = "".join(
        f'<li><a href="/s22_dense/item{i}">관련 상품 {i}</a></li>' for i in range(40)
    )
    _noise_buttons = "".join(
        f'<button id="nb{i}">더보기 {i}</button>' for i in range(25)
    )
    sites.append(
        MockSite(
            site_id="s22_dense",
            title="고밀도 목록",
            scenarios=(Scenario.INFINITE_SCROLL, Scenario.LAZY_LOADING),
            golden_target="주문 결제하기",
            golden_role="button",
            html=_page(
                "고밀도 목록",
                f"""
                <h1>상품 상세</h1>
                <nav><ul>{_noise_links}</ul></nav>
                <section>{_noise_buttons}</section>
                <div class="cta">
                  <button id="checkout">주문 결제하기</button>
                </div>
                <footer>
                  <a href="/s22_dense/terms">약관</a>
                  <a href="/s22_dense/privacy">개인정보</a>
                  <a href="/s22_dense/help">고객센터</a>
                </footer>
                """,
            ),
        )
    )

    # ------------------------------------------------------------------
    # Stage 4 Tier-2 실패 유발 페이지 3종 (PRD §3.1 SoM 폴백)
    #
    # Tier-1 텍스트 파이프라인이 **실제로 실패해야** 발동률 측정이 유형 D(미탐)가
    # 되지 않는다. 세 페이지 모두 golden_target을 두지 않는다 — 텍스트로
    # 정답을 지목할 수 없는 페이지라 Recall 골든셋(Tier-1 측정 도구 검증)에
    # 끼어들면 그 자체가 결함이다. 정답 판정은 클릭 후 body[data-result="ok"].
    # ------------------------------------------------------------------

    # 23. 아이콘 전용 버튼 — 텍스트/aria-label 없음. 정답은 3번째(장바구니 모양).
    sites.append(
        MockSite(
            site_id="icon-buttons",
            title="아이콘 툴바",
            scenarios=(Scenario.SPA_ROUTING,),
            html=_page(
                "아이콘 툴바",
                """
                <h1>도구</h1>
                <div id="toolbar">
                  <button class="ic" id="ic1"><svg viewBox="0 0 24 24"><circle cx="10" cy="10" r="6"/><path d="M15 15l6 6"/></svg></button>
                  <button class="ic" id="ic2"><svg viewBox="0 0 24 24"><path d="M12 3l9 8h-3v9h-5v-6h-2v6H6v-9H3z"/></svg></button>
                  <button class="ic" id="ic3"><svg viewBox="0 0 24 24"><path d="M3 4h2l3 11h11l3-8H7"/><circle cx="10" cy="20" r="1.5"/><circle cx="18" cy="20" r="1.5"/></svg></button>
                  <button class="ic" id="ic4"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/></svg></button>
                  <button class="ic" id="ic5"><svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16"/></svg></button>
                </div>
                <script>
                  document.getElementById('ic3').addEventListener('click', () => {
                    document.body.setAttribute('data-result', 'ok');
                  });
                </script>
                """,
                head="""<style>
                  #toolbar { display: flex; gap: 12px; }
                  .ic { width: 48px; height: 48px; padding: 8px; border: 1px solid #444;
                        background: #fafafa; border-radius: 6px; }
                  .ic svg { width: 100%; height: 100%; fill: none; stroke: #222; stroke-width: 2; }
                </style>""",
            ),
        )
    )

    # 24. 난독화 라벨 — 텍스트는 해시, 시각 구분은 CSS 배경(스프라이트 모사)뿐.
    #     정답은 '검색' 스타일(돋보기 그라디언트)이 입혀진 q9zz 버튼.
    sites.append(
        MockSite(
            site_id="obfuscated-labels",
            title="난독화 라벨",
            scenarios=(Scenario.SPA_ROUTING,),
            html=_page(
                "난독화 라벨",
                """
                <h1>x1a9</h1>
                <div id="bar">
                  <button class="k k-home" id="k1">x7f2</button>
                  <button class="k k-search" id="k2">q9zz</button>
                  <button class="k k-cart" id="k3">m3ke</button>
                  <button class="k k-user" id="k4">p0lr</button>
                </div>
                <script>
                  document.getElementById('k2').addEventListener('click', () => {
                    document.body.setAttribute('data-result', 'ok');
                  });
                </script>
                """,
                head="""<style>
                  #bar { display: flex; gap: 10px; }
                  .k { width: 96px; height: 40px; border: 1px solid #333; color: #333;
                       font-family: monospace; background-repeat: no-repeat;
                       background-position: 6px center; background-size: 24px 24px;
                       padding-left: 34px; text-align: left; }
                  .k-home { background-color: #e8f0ff; background-image:
                    url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M12 3l9 8h-3v9h-5v-6h-2v6H6v-9H3z' fill='%23335'/></svg>"); }
                  .k-search { background-color: #fff3d6; background-image:
                    url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><circle cx='10' cy='10' r='6' fill='none' stroke='%23a60' stroke-width='2.5'/><path d='M15 15l6 6' stroke='%23a60' stroke-width='2.5'/></svg>"); }
                  .k-cart { background-color: #e6ffe6; background-image:
                    url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M3 4h2l3 11h11l3-8H7' fill='none' stroke='%23262' stroke-width='2'/></svg>"); }
                  .k-user { background-color: #ffe6f0; background-image:
                    url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><circle cx='12' cy='8' r='4' fill='%23623'/><path d='M4 21c0-4 4-6 8-6s8 2 8 6' fill='%23623'/></svg>"); }
                </style>""",
            ),
        )
    )

    # 25. 순수 Canvas UI (옵션 B) — DOM 상호작용 요소가 0개.
    #     후보 수집기가 빈 리스트를 반환해야 하는 페이지이며, 좌표 클릭으로만
    #     정답(장바구니 사각형: x 220..380, y 100..200)에 도달할 수 있다.
    sites.append(
        MockSite(
            site_id="canvas-ui",
            title="캔버스 UI",
            scenarios=(Scenario.SPA_ROUTING,),
            html=_page(
                "캔버스 UI",
                """
                <canvas id="ui" width="600" height="300"></canvas>
                <script>
                  const RECTS = [
                    { label: '검색',     x: 20,  y: 100, w: 160, h: 100, color: '#dbe9ff' },
                    { label: '장바구니', x: 220, y: 100, w: 160, h: 100, color: '#dfffe0' },
                    { label: '설정',     x: 420, y: 100, w: 160, h: 100, color: '#ffe8d6' },
                  ];
                  const cv = document.getElementById('ui');
                  const ctx = cv.getContext('2d');
                  ctx.fillStyle = '#fff';
                  ctx.fillRect(0, 0, cv.width, cv.height);
                  for (const r of RECTS) {
                    ctx.fillStyle = r.color;
                    ctx.fillRect(r.x, r.y, r.w, r.h);
                    ctx.strokeStyle = '#333';
                    ctx.lineWidth = 2;
                    ctx.strokeRect(r.x, r.y, r.w, r.h);
                    ctx.fillStyle = '#111';
                    ctx.font = '24px sans-serif';
                    ctx.textAlign = 'center';
                    ctx.textBaseline = 'middle';
                    ctx.fillText(r.label, r.x + r.w / 2, r.y + r.h / 2);
                  }
                  cv.addEventListener('click', (ev) => {
                    const b = cv.getBoundingClientRect();
                    const x = ev.clientX - b.left;
                    const y = ev.clientY - b.top;
                    for (const r of RECTS) {
                      if (x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h) {
                        document.body.setAttribute('data-result', r.label === '장바구니' ? 'ok' : 'wrong');
                        return;
                      }
                    }
                  });
                </script>
                """,
                head="<style>body{margin:0} #ui{display:block}</style>",
            ),
        )
    )

    return sites


MOCK_SITES: Tuple[MockSite, ...] = tuple(_build_sites())

#: site_id → MockSite 조회 맵
SITE_INDEX: Dict[str, MockSite] = {s.site_id: s for s in MOCK_SITES}


def covered_scenarios() -> Dict[Scenario, List[str]]:
    """시나리오별로 이를 커버하는 사이트 ID 목록을 반환한다."""
    coverage: Dict[Scenario, List[str]] = {s: [] for s in Scenario}
    for site in MOCK_SITES:
        for scenario in site.scenarios:
            coverage[scenario].append(site.site_id)
    return coverage


def missing_scenarios() -> List[Scenario]:
    """어떤 사이트에도 배치되지 않은 시나리오 목록."""
    return [s for s, sites in covered_scenarios().items() if not sites]


# ---------------------------------------------------------------------------
# 정적 서버
# ---------------------------------------------------------------------------

_SUB_PAGES: Dict[str, str] = {
    "/s05_iframe/outer": _page(
        "outer",
        '<p>외부 프레임</p><iframe id="inner" src="/s05_iframe/inner" '
        'width="300" height="150"></iframe>',
    ),
    "/s05_iframe/inner": _page("inner", '<button id="pay-inner">프레임 내부 결제</button>'),
    "/s16_iframe_shadow/widget": _page(
        "widget",
        '<div id="whost"></div><script>'
        "document.getElementById('whost').attachShadow({mode:'open'})"
        ".innerHTML = '<button id=\"frame-shadow-btn\">위젯 실행</button>';"
        "</script>",
    ),
    "/s01_login/help": _page("도움말", "<p>비밀번호 재설정 안내</p>"),
    "/s11_popup/terms": _page("약관", "<h1>이용 약관 전문</h1>"),
    "/s19_checkout/receipt": _page("영수증", "<h1>영수증</h1>"),
}


class _MockHandler(BaseHTTPRequestHandler):
    """Mock 사이트 요청 핸들러. 로그를 남기지 않는다."""

    def log_message(self, format: str, *args) -> None:  # noqa: A002, D102, ANN002
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        # 인덱스
        if path == "/":
            links = "".join(
                f'<li><a href="/{s.site_id}">{s.title}</a></li>' for s in MOCK_SITES
            )
            self._send(200, _page("Mock 사이트", f"<ul>{links}</ul>").encode(), "text/html; charset=utf-8")
            return

        # 세션 만료 시나리오: 보호 리소스는 401
        if path.endswith("/protected"):
            body = _page("인증 필요", "<h1>401 Unauthorized</h1>").encode()
            self.send_response(401)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # CSV 다운로드
        if path.endswith(".csv"):
            csv = "id,name,amount\n1,항목A,1000\n2,항목B,2000\n".encode()
            self._send(200, csv, "text/csv; charset=utf-8")
            return

        # 서브 페이지
        if path in _SUB_PAGES:
            self._send(200, _SUB_PAGES[path].encode(), "text/html; charset=utf-8")
            return

        # 사이트 루트 (SPA 하위 경로도 동일 문서를 반환)
        site_id = path.lstrip("/").split("/", 1)[0]
        site = SITE_INDEX.get(site_id)
        if site:
            self._send(200, site.html.encode(), "text/html; charset=utf-8")
            return

        self._send(404, _page("404", "<h1>404</h1>").encode(), "text/html; charset=utf-8")


class MockServer:
    """Mock 사이트 20종을 서빙하는 스레드 기반 HTTP 서버."""

    def __init__(self, port: int = 0) -> None:
        self._httpd = HTTPServer(("127.0.0.1", port), partial(_MockHandler))
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def site_url(self, site_id: str) -> str:
        return f"{self.base_url}/{site_id}"

    def start(self) -> "MockServer":
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self) -> "MockServer":
        return self.start()

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        self.stop()
