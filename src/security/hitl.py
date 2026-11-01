"""고위험 액션 HITL 게이트 (PRD §5.3-1, §3.3).

허용 도메인 내부라도 고위험 액션(폼 제출, 결제, 데이터 삭제)은
실행 모드에 따라 다르게 처리한다:

* 대화형(`interactive`) — `ConfirmDialog` 모달로 사용자 승인 대기
* 무인(`unattended`)   — `pre_approved_actions` 외 **전면 차단**하고
  `E_HITL_UNATTENDED_BLOCKED` 반환

무인 모드에서 승인 없이 통과시키면 결제·삭제가 무단 실행되므로,
기본값은 항상 "차단"이며 사전 승인은 명시적으로만 부여된다.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import unquote_plus, urlsplit

from contracts import ActionType, ConfirmDialog, DangerLevel, ErrorCode, ExecutionMode


class RiskLevel(str, Enum):
    """액션 위험 등급."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


#: 본질적으로 부작용이 큰 액션 (PRD §4.1 retry_safe=No 계열과 정합)
_INHERENTLY_RISKY: Set[ActionType] = {
    ActionType.UPLOAD_FILE,
    ActionType.DOWNLOAD_FILE,
}

#: 고위험 의도를 드러내는 텍스트 신호 (요소 이름/셀렉터에서 탐지)
HIGH_RISK_KEYWORDS = (
    # 결제
    "결제", "구매", "주문", "송금", "이체", "출금", "pay", "purchase", "checkout",
    "order", "transfer", "withdraw", "subscribe",
    # 삭제
    "삭제", "제거", "탈퇴", "해지", "delete", "remove", "destroy", "terminate",
    "deactivate", "cancel account",
    # 제출/확정
    "제출", "확정", "승인", "동의", "submit", "confirm", "approve", "agree",
    # 권한 변경
    "권한", "관리자", "permission", "admin", "grant", "revoke",
)

MEDIUM_RISK_KEYWORDS = (
    "저장", "수정", "변경", "업로드", "save", "update", "edit", "upload", "apply",
)

# ---------------------------------------------------------------------------
# WS-31: 이름 밖 문맥 신호 (보이는 텍스트·aria·alt·목적지·클래스 토큰·의사요소 글자)
# ---------------------------------------------------------------------------

#: 정규화에서 지우는 보이지 않는 문자: 유니코드 범주 Cf(서식 문자) **전체** + U+034F(CGJ, 범주 Mn
#: 이지만 글자 모양이 없음). WS-31 은 zero-width 5종+soft hyphen 만 지워 `결\u200f제`(RLM)·U+2064·
#: U+061C·U+034F 를 끼우면 통과했다(WS-31 R1 NB-2). Cf 에는 ZWJ(U+200D)도 있어 이모지 ZWJ 시퀀스는
#: 구성 이모지로 풀린다 — 이모지는 키워드가 아니므로 매칭에 영향이 없다(테스트로 고정).
#: 한글 완성형(Lo)·자모(Lo)·변형 선택자(Mn)는 지우지 않는다.
_EXTRA_INVISIBLE = frozenset({"\u034f"})
_SPACE_RE = re.compile(r"\s+")


def strip_invisible(text: str) -> str:
    """서식 문자(범주 Cf)와 CGJ 를 지운다 (대소문자·정규화는 그대로)."""
    return "".join(
        ch for ch in text
        if ch not in _EXTRA_INVISIBLE and unicodedata.category(ch) != "Cf"
    )


def normalize_gate_text(text: str) -> str:
    """게이트 매칭용 정규화: NFKC → 보이지 않는 문자 제거 → 공백 정리 → 소문자.

    한글 자모 분리(NFD) 같은 과한 정규화는 하지 않는다 — NFKC 는 완성형 한글을 유지한다.
    """
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = strip_invisible(text)
    return _SPACE_RE.sub(" ", text).strip().lower()


#: 문자열 원천 — 키워드를 **부분 문자열**로 찾는다(이름과 같은 규칙).
TEXT_SOURCES = frozenset(
    {"name", "text", "aria", "title", "child_title", "alt", "svg_title", "child_aria", "value",
     "pseudo", "selector", "detail"}
)
#: 목적지 원천(URL) — 경로·쿼리를 **단어 경계 토큰**으로 매칭한다(`/payroll-info` ≠ pay).
PATH_SOURCES = frozenset({"href", "formaction", "form_action"})
#: 마크업 식별자 원천 — camelCase·kebab·snake 를 토큰으로 나눠 매칭한다.
TOKEN_SOURCES = frozenset({"class", "id", "name_attr", "testid"})
#: 판정 보조 사실(키워드 매칭 안 함): `nav_link` 같은 출처로 이동하는 a[href] 자신, `form_method`
#: 제출 버튼의 실제 제출 메서드(get/post…). WS-31 R1 NB-6.
#: WS-76 R1: `role`(대상의 명시·암묵 역할), `tab_context`(탭·탭 목록 안 — 탐색), `edit_context`(텍스트 입력칸
#: 옆 — 편집)도 판정 보조 사실이다(단독 `차단`·`비우기`·`Trash` 판정에만 쓴다).
META_SOURCES = frozenset({"nav_link", "form_method", "role", "tab_context", "edit_context"})
#: 같은 출처 이동 링크에서 적용하지 않는 원천 — 마크업 식별자(class·아이콘 사전·id·testid·name)와
#: title(보조 설명). 링크는 이동이지 부작용이 아니다(WS-31 과차단: `fa-trash` '휴지통 보기',
#: `data-testid=nav-order-history`). 이름·보이는 글자·aria·alt·svg title·pseudo·href 는 그대로 본다.
_NAV_LINK_SKIPPED = TOKEN_SOURCES | {"title", "child_title"}

#: 링크 목적지(href)에서 빼는 **권한 명사** — 링크로 그 페이지에 가는 것은 보기다(`/admin` 대시보드,
#: `/scholarships/grant-2025`). 동사(revoke·delete·pay·transfer …)는 그대로 막는다. 버튼 이름·폼
#: action·formaction 에는 적용하지 않는다(WS-31 R1 — 코디네이터 결정 A).
_HREF_NOUNS = frozenset({"admin", "permission", "grant", "관리자", "권한"})

#: 목적지·식별자 토큰에서 빼는 키워드 — 폼 **메커니즘**을 뜻하는 일반어라 의도를 말하지 않는다.
#: 실측 근거: 로그인 버튼 `class="btn-submit"`·`id="submit"`, 로그인 폼 `action="/login/submit"`
#: 이 흔하다(이 토큰으로 막으면 정상 로그인이 차단된다 — WS-31 과차단 측정). 이 단어들은
#: 이름·보이는 글자(문자열 원천)에서는 그대로 고위험이다.
_MARKUP_GENERIC = frozenset({"submit", "confirm", "approve", "agree"})

#: 목적지·식별자 토큰과 비교할 영문 키워드 (HIGH_RISK_KEYWORDS 와 공유, 한 단어만).
_TOKEN_KEYWORDS = tuple(
    kw for kw in HIGH_RISK_KEYWORDS
    if kw.isascii() and " " not in kw and kw not in _MARKUP_GENERIC
)
#: 경로에서 부분 문자열로 찾는 한글 키워드 (한글 경로는 토큰 경계가 없다).
_HANGUL_KEYWORDS = tuple(kw for kw in HIGH_RISK_KEYWORDS if not kw.isascii())

#: 아이콘 클래스 사전 — 아이콘만 있는 버튼의 의미(삭제·결제). 접두사(fa-, bi-, icon- …)를 뗀
#: 이름이 이 표의 키와 같거나 `키-` 로 시작하면(`trash-can`, `credit-card-fill`) 해당한다.
#: 근거: Font Awesome(fa-trash, fa-credit-card, fa-cart-shopping), Bootstrap Icons(bi-trash,
#: bi-credit-card, bi-cart), Material Symbols(delete, shopping_cart, payment) 의 실제 이름.
#: 검색·홈·설정 같은 일반 아이콘은 넣지 않는다(과차단). 값은 판정 근거로 보고할 키워드.
#: WS-31 R1: 장바구니(cart·shopping-cart·cart-shopping)는 뺐다 — 헤더의 '장바구니 보기'가 가장 흔한
#: 쓰임이고, '장바구니 담기'도 결제가 아니라 되돌릴 수 있는 담기다(이름 '장바구니 담기'도 고위험
#: 키워드가 아니다). 결제는 그다음 checkout 단계(이름·경로 신호)에서 막는다.
ICON_CLASS_RISK = {
    "trash": "delete",
    "trash-can": "delete",
    "trash-alt": "delete",
    "delete": "delete",
    "delete-forever": "delete",
    "remove": "remove",
    "credit-card": "pay",
    "creditcard": "pay",
    "payment": "pay",
    "payments": "pay",
    "wallet": "pay",
    "shopping-bag": "purchase",
    "bag-check": "purchase",
    "money-bill": "pay",
    "cash": "pay",
}
_ICON_PREFIXES = ("fa-", "fas-", "far-", "bi-", "icon-", "ico-", "glyphicon-", "mdi-", "ti-",
                  "ri-", "la-", "las-", "material-icons-", "ms-", "i-")

#: Bootstrap 정렬 유틸리티(`order-1`, `order-md-2`, `order-first`) — 'order' 토큰 과매칭 방지.
_BOOTSTRAP_ORDER_RE = re.compile(r"^order-(\d|first|last|sm|md|lg|xl|xxl)")

_TOKEN_SPLIT_RE = re.compile(r"[^0-9a-zA-Z]+")
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def _split_camel(word: str) -> List[str]:
    return [m.group(0).lower() for m in _CAMEL_RE.finditer(word)]


def class_tokens(text: str) -> List[str]:
    """식별자 문자열을 토큰으로: camelCase·kebab-case·snake_case 분해, 소문자."""
    out: List[str] = []
    # 대소문자는 camelCase 분해에 필요하므로 정규화는 NFKC·보이지 않는 문자 제거까지만.
    cleaned = strip_invisible(unicodedata.normalize("NFKC", str(text or "")))
    for part in _TOKEN_SPLIT_RE.split(cleaned):
        if part:
            out.extend(_split_camel(part))
    return out


def path_tokens(url: str) -> List[str]:
    """URL 경로·쿼리를 토큰으로 (퍼센트 디코딩 뒤 class_tokens 와 같은 분해)."""
    raw = str(url or "")
    try:
        parts = urlsplit(raw)
        raw = f"{parts.path} {parts.query}"
    except ValueError:
        pass
    return class_tokens(unquote_plus(raw))


def _match_text(text: str) -> Optional[str]:
    return _contains_keyword(normalize_gate_text(text), HIGH_RISK_KEYWORDS)


def _match_tokens(tokens: Sequence[str]) -> Optional[str]:
    present = set(tokens)
    for kw in _TOKEN_KEYWORDS:
        if kw in present:
            return kw
    return None


def _match_path(url: str, path_only: bool = False) -> Optional[str]:
    """폼 제출 목적지: 경로 전체(+쿼리) 토큰. `path_only` 면 쿼리를 보지 않는다 — GET 제출은
    브라우저가 action 의 쿼리를 폼 값으로 **바꿔 버려** 보내지도 않는다(`/search?type=order`)."""
    if path_only:
        try:
            parts = urlsplit(str(url or ""))
            url = parts.path
        except ValueError:
            url = str(url or "").split("?", 1)[0]
    hit = _match_tokens(path_tokens(url))
    if hit:
        return hit
    try:
        decoded = normalize_gate_text(unquote_plus(url))
    except Exception:  # noqa: BLE001
        return None
    return _contains_keyword(decoded, _HANGUL_KEYWORDS)


def _match_href(url: str) -> Optional[str]:
    """링크(GET 이동) 목적지: **마지막 경로 조각**과 **값 전체가 키워드인 쿼리 값**만 본다.

    링크는 대개 조회다 — `/order/123`(주문 상세), `?sort=order_date`(정렬) 를 경로 전체 토큰으로
    보면 막힌다(WS-31 과차단 측정: 정상 표본 2건). 동작을 나타내는 링크는 대개 마지막 조각이
    동사다(`/account/delete`, `/cart/checkout`, `/order/123/pay`) 또는 `?action=delete`.
    폼 제출 목적지(form_action·formaction)는 실제 제출이므로 `_match_path`(경로 전체)를 쓴다.
    """
    raw = str(url or "")
    try:
        parts = urlsplit(raw)
        path, query = parts.path, parts.query
    except ValueError:
        path, query = raw, ""
    segments = [seg for seg in unquote_plus(path).split("/") if seg]
    last = segments[-1] if segments else ""
    present = set(class_tokens(last))
    for kw in _TOKEN_KEYWORDS:
        if kw in present and kw not in _HREF_NOUNS:
            return kw
    normalized_last = normalize_gate_text(last)
    for kw in _HANGUL_KEYWORDS:
        if kw in normalized_last and kw not in _HREF_NOUNS:
            return kw
    for pair in query.split("&"):
        value = unquote_plus(pair.partition("=")[2])
        norm = normalize_gate_text(value)
        if norm in _HREF_NOUNS:
            continue
        if norm in _TOKEN_KEYWORDS or norm in _HANGUL_KEYWORDS:
            return norm
    return None


def _match_icon_class(cls: str) -> Optional[str]:
    lowered = cls.strip().lower()
    for prefix in _ICON_PREFIXES:
        if lowered.startswith(prefix):
            stem = lowered[len(prefix):].replace("_", "-")
            for key in ICON_CLASS_RISK:
                if stem == key or stem.startswith(key + "-"):
                    return cls.strip()
    # Material Symbols 는 클래스가 아니라 글자(ligature)로 쓴다 — 그건 text 원천이 맡는다.
    return None


def _match_markup(source: str, text: str) -> Optional[str]:
    if source == "class":
        for cls in str(text or "").split():
            icon = _match_icon_class(cls)
            if icon:
                return icon
            if _BOOTSTRAP_ORDER_RE.match(cls.lower()):
                continue
            hit = _match_tokens(class_tokens(cls))
            if hit:
                return hit
        return None
    return _match_tokens(class_tokens(text))


def match_signal(source: str, text: str, path_only: bool = False) -> Optional[str]:
    """원천 종류에 맞는 규칙으로 고위험 키워드를 찾는다. 없으면 None.

    `path_only`: 폼 목적지(form_action·formaction)의 쿼리를 보지 않는다(GET 제출).
    WS-56 R3 B4: 키워드가 없어도 계정 '자체' 종료(Close account·계정 비활성화 …)면 고위험(`ACCOUNT_TERMINATION_KEYWORD`).
    WS-76: 휴지통 이동·비우기·스팸 신고·차단 구(`irreversible_action`)도 고위험.
    """
    if not text or source in META_SOURCES:
        return None
    if source == "href":
        hit = _match_href(text)
    elif source in PATH_SOURCES:
        hit = _match_path(text, path_only=path_only)
    elif source in TOKEN_SOURCES:
        hit = _match_markup(source, text)
    else:
        hit = _match_text(text)
    if hit is None and account_termination(source, text):
        return ACCOUNT_TERMINATION_KEYWORD
    if hit is None:
        hit = irreversible_action(source, text)
    if hit is None and source in PATH_SOURCES:
        hit = item_operation_destination(text, path_only=path_only)
    return hit


# ---------------------------------------------------------------------------
# WS-56 R3 B4·B8: 계정 '자체' 종료 판정 — 게이트(위험 HIGH)와 정책 금지 범주(approve_policy)가 공유한다.
# ---------------------------------------------------------------------------
#
# 계정 명사와 종료 동사가 **붙어서**(사이에 소유격·관사만) 나올 때만 계정 자체 종료다:
#   'Close account' · 'Delete my account' · 'Account deletion' · '계정 비활성화' · '계정을 영구 삭제'.
# 명사 뒤에 다른 대상이 오거나(목적어가 계정 안 항목) 동사와 명사 사이에 다른 목적어가 끼면 일반 동작이다:
#   'Delete email from my account' · '계정에서 메일 삭제' · 'Remove profile photo' · 'Delete account email'.
# 정규화: NFKC(전각) · 보이지 않는 문자 제거 · camelCase/kebab/snake 분해 · 한 글자씩 띄운 영문('d e l e t e')은 이어 붙임.
# 한글은 공백·구두점을 지운 정규형에서 '명사(+정보)(+을/를)(+영구/완전)+동사' 연속만 본다.

#: 게이트가 근거로 보고하는 키워드(matched_keyword).
ACCOUNT_TERMINATION_KEYWORD = "account termination"
_ACCT_NOUNS = frozenset({"account", "accounts", "profile", "membership"})
#: 닫기·취소 동사가 붙어도 종료인 명사(프로필 '닫기'는 창 닫기라 뺀다).
_ACCT_HARD_NOUNS = frozenset({"account", "accounts", "membership"})
_ACCT_VERBS = frozenset({"delete", "remove", "close", "erase", "deactivate", "disable", "terminate", "cancel",
                         "destroy", "wipe"})
#: 닫기·취소 계열(동사·동사형 명사의 앞머리).
_ACCT_SOFT_PREFIXES = ("clos", "canc")
#: 명사 뒤에 오는 동사(형 명사) ('Account deletion' · 'account-close').
_ACCT_VERB_NOUNS = frozenset({"deletion", "delete", "removal", "remove", "closure", "close", "erasure", "erase",
                              "deactivation", "deactivate", "termination", "terminate", "cancellation", "cancel",
                              "disable"})
#: 동사와 명사 사이에 와도 되는 말(소유격·관사·강조).
_ACCT_FILLERS = frozenset({"my", "your", "the", "this", "our", "a", "an", "entire", "whole", "user"})
#: 종료 구 뒤에 와도 계정 자체 종료로 보는 말(버튼·링크 꾸밈, 부사). 그 밖의 말이 이어지면 계정 안 항목('… photo').
_ACCT_TRAILERS = frozenset({"btn", "button", "link", "cta", "now", "permanently", "forever", "confirm",
                            "confirmation", "request", "page", "form", "modal", "dialog", "action", "submit",
                            "here", "and"})
_ACCT_COMPOUND_RE = re.compile(
    r"^(?:(?P<v>delete|remove|close|erase|deactivate|disable|terminate|cancel)(?:my|your|the|this)?"
    r"(?P<n>account|profile|membership)s?"
    r"|(?P<n2>account|profile|membership)(?P<vn>deletion|delete|removal|remove|closure|close|erasure|erase"
    r"|deactivation|deactivate|termination|terminate|cancellation|cancel))$"
)
_ACCT_KO_RE = re.compile(r"(?:계정|회원|프로필|멤버십)(?:정보)?(?:을|를|은|는)?(?:영구|완전)?"
                         r"(?:삭제|비활성|해지|폐쇄|탈퇴|종료|제거)")


def _acct_tokens(text: str) -> List[str]:
    """식별자 토큰(class_tokens) — 한 글자씩 띄운 영문 연속(2개 이상)은 한 토큰으로 잇는다."""
    out: List[str] = []
    run: List[str] = []
    for tok in class_tokens(text):
        if len(tok) == 1 and tok.isalpha():
            run.append(tok)
            continue
        if run:
            out.extend(["".join(run)] if len(run) > 1 else run)
            run = []
        out.append(tok)
    if run:
        out.extend(["".join(run)] if len(run) > 1 else run)
    return out


def _acct_end_ok(tokens: List[str], idx: int) -> bool:
    """idx(종료 구 다음 자리)가 끝이거나 꾸밈말인가."""
    return idx >= len(tokens) or tokens[idx] in _ACCT_TRAILERS


def _acct_pair(verb: str, noun: str) -> bool:
    """닫기·취소(close/cancel)는 account·membership 에만 — 'Close profile'(프로필 창 닫기)은 종료가 아니다."""
    return noun in _ACCT_NOUNS and (not verb.startswith(_ACCT_SOFT_PREFIXES) or noun in _ACCT_HARD_NOUNS)


def _account_termination_tokens(tokens: List[str]) -> bool:
    for i, tok in enumerate(tokens):
        if _acct_end_ok(tokens, i + 1):
            m = _ACCT_COMPOUND_RE.match(tok)
            if m and _acct_pair(m.group("v") or m.group("vn") or "", m.group("n") or m.group("n2") or ""):
                return True
        if tok in _ACCT_VERBS:
            j = i + 1
            while j < len(tokens) and tokens[j] in _ACCT_FILLERS:
                j += 1
            if j < len(tokens) and _acct_pair(tok, tokens[j]) and _acct_end_ok(tokens, j + 1):
                return True
        if tok in _ACCT_NOUNS and i + 1 < len(tokens) and tokens[i + 1] in _ACCT_VERB_NOUNS \
                and _acct_pair(tokens[i + 1], tok) and _acct_end_ok(tokens, i + 2):
            return True
    return False


def account_termination(source: str, text: str) -> bool:
    """이 원천의 문자열이 계정 '자체' 종료(삭제·탈퇴·비활성화·닫기)를 말하는가 (계정 안 항목 삭제는 아님)."""
    if not text or source in META_SOURCES:
        return False
    raw = str(text)
    if source in PATH_SOURCES:
        # 목적지는 경로만(쿼리 값·호스트는 다른 말이 이어져 판정을 흐린다) — 퍼센트 디코딩 후.
        try:
            raw = unquote_plus(urlsplit(raw).path)
        except Exception:  # noqa: BLE001 - 해석 실패 = 원문
            pass
    if source == "class":
        tokens: List[str] = []
        for cls in raw.split():
            if not _BOOTSTRAP_ORDER_RE.match(cls.lower()):
                tokens.extend(_acct_tokens(cls))
            if _account_termination_tokens(tokens):
                return True
            tokens = []
        return False
    if _account_termination_tokens(_acct_tokens(raw)):
        return True
    compact = "".join(ch for ch in normalize_gate_text(raw) if ch.isalnum())
    return bool(_ACCT_KO_RE.search(compact))


# ---------------------------------------------------------------------------
# WS-76 (#204): 되돌리기 어려운 메일·계정 조작 — 휴지통 이동·비우기, 스팸 신고·표시, (사용자·번호) 차단.
# ---------------------------------------------------------------------------
#
# 낱말 하나(휴지통·trash·spam·차단·block·비우기·empty)는 폴더 열기·안내·레이아웃 이름에도 쓰이므로
# **동사+대상 구**일 때만 고위험이다(계정 종료 판정과 같은 방식):
#   'Move to trash' · 'Empty trash' · 'Report spam' · 'Mark as spam' · 'Block user' · '휴지통으로 이동' ·
#   '휴지통 비우기' · '스팸 신고' · '사용자 차단'.
# 반전·안내는 고위험이 아니다(기준 등급 유지): 앞에 un/undo/not, 뒤에 해제·취소·안내·help·list 등
#   ('Undo move to trash' · '차단 해제' · '스팸 신고 안내' · 'Mark as not spam').
# WS-76 R1 B2: 부정·안내 꼬리는 구 **바로 뒤가 아니라 뒤 어디든**(활용형 '신고하기 안내'·역추적 '비우+기취소' 우회 차단),
#   안내 머리('How to …'·'Learn …'·'도움말')는 구 **앞 어디든** 본다.
# 한국어 동작 명사형 단독 라벨('차단'·'차단하기'·'비우기')은 라벨 전체가 그 낱말일 때만 고위험이다.
# WS-76 R1 B1: 단독 '차단' 은 탭 문맥(`tab_context`), 단독 '비우기' 는 입력칸 옆 편집 문맥(`edit_context`)이
#   **확인될 때만** 기준 등급(assess_risk) — 문맥 신호가 없거나 못 읽었으면 고위험 그대로(fail-closed).
# WS-76 R1 B3: 단독 'Trash' 는 동작 역할(button)일 때만 고위험 — 링크·탭·메뉴·트리 항목(폴더 목록)은 탐색.
# 복원 계열('스팸 아님'·'Not spam'·'Restore'·'Unblock')은 되돌리는 동작이라 올리지 않는다(기준 LOW 그대로).

#: 게이트가 근거로 보고하는 키워드(matched_keyword).
TRASH_KEYWORD = "move to trash"
EMPTY_KEYWORD = "empty"
SPAM_KEYWORD = "report spam"
BLOCK_KEYWORD = "block"
_IRR_ITEMS = frozenset({"selected", "message", "messages", "mail", "mails", "email", "emails", "conversation",
                        "conversations", "thread", "threads", "item", "items", "it", "this", "these", "all", "file",
                        "files", "them"})
_IRR_FILLERS = frozenset({"the", "my", "your", "this", "a", "an", "all", "selected"})
_IRR_MOVE_VERBS = frozenset({"move", "send", "put", "throw"})
_IRR_TRASH_DEST = frozenset({"trash", "bin", "junk", "spam", "wastebasket", "recycle"})
_IRR_EMPTY_DEST = frozenset({"trash", "bin", "junk", "spam", "cart", "basket", "wastebasket", "recycle"})
_IRR_SPAM = frozenset({"spam", "junk", "phishing"})
_IRR_BLOCK_OBJ = frozenset({"user", "users", "contact", "contacts", "number", "numbers", "sender", "senders",
                            "account", "person", "caller", "member", "profile", "channel", "domain", "address",
                            "email", "phone", "this", "them", "him", "her"})
#: 구 바로 앞에 오면 반전(되돌리기·부정)이다.
_IRR_NEG_BEFORE = frozenset({"un", "undo", "not", "dont", "don", "never", "cancel", "stop"})
#: 구 뒤(어디든)에 오면 동작이 아니라 안내·목록·되돌리기다(WS-76 R1 B2 — 바로 뒤만 보면 'Move to trash help' 는
#: 막지만 'Report spam (help)'·'… help center' 류 안내 문구 끝 꼬리를 놓친다).
_IRR_NEG_AFTER = frozenset({"undo", "help", "guide", "info", "settings", "setting", "list", "policy", "faq",
                            "learn", "tips", "how", "instructions", "cancel", "tutorial"})
#: 구 앞(어디든)에 오면 안내 문구다('How to report spam' · 'Learn how to empty trash').
_IRR_INFO_BEFORE = frozenset({"how", "learn", "help", "guide", "faq", "tips", "about", "tutorial", "instructions"})
#: 목적지 경로에 있으면 안내 페이지다('/help/report-spam').
_IRR_INFO_PATH = frozenset({"help", "guide", "faq", "support", "docs", "doc", "policy"})
#: 한 글자씩 띄운 영문을 이은 한 토큰('r e p o r t s p a m' → reportspam).
_IRR_COMPOUND_RE = re.compile(
    r"^(?:(?:move|send)(?:to)?(?:the)?(?:trash|bin|junk|spam)|trash(?:this|message|messages|it|selected)"
    r"|empty(?:the)?(?:trash|bin|spam|junk|cart)|report(?:as)?(?:spam|junk|phishing)|markas(?:spam|junk)"
    r"|block(?:this)?(?:user|sender|number|contact|caller))$"
)
#: WS-76 R1 B2: 부정·안내 꼬리는 구 뒤 **어디든**(예전 lookahead 는 '비우기취소' 에서 '비우'+'기취소' 로 역추적해
#: 우회됐고 '신고하기안내' 의 활용 어미 뒤 꼬리를 못 봤다), 안내 머리는 구 앞 어디든 본다(정규형 — 공백 없음).
_IRR_KO_NEG_AFTER_RE = re.compile(r"해제|취소|안내|방법|설명|도움말|목록|설정|관리|내역|기준|정책|해지|가이드")
_IRR_KO_INFO_BEFORE_RE = re.compile(r"도움말|안내|방법|가이드")
#: B3: 이동·버림 동사의 활용형(옮기기·옮겨·옮김·보내기·보내·보냄·버리기·버려·버림·넣기·이동).
_IRR_KO_MOVE = r"(?:이동|보내|보냄|버리|버려|버림|넣|옮기|옮겨|옮김)"
_IRR_KO = (
    (TRASH_KEYWORD, re.compile(r"휴지통(?:으로|에|로)?" + _IRR_KO_MOVE)),
    (EMPTY_KEYWORD, re.compile(r"(?:휴지통|스팸함|스팸메일함|장바구니|메일함|편지함|받은편지함|폴더|전체|모두|목록)"
                               r"(?:을|를)?(?:전체|모두|바로|지금)?(?:비우기|비우|비움)")),
    # 스팸함·스팸메일함 '으로/에' + 이동 동사도 스팸 처리('스팸함으로 보내기'). 조사 없는 '스팸함 이동'은 폴더 이동일 수 있어 뺀다.
    (SPAM_KEYWORD, re.compile(r"스팸(?:메일)?(?:(?:함|메일함|편지함)(?:으로|로|에)|으로|로)?"
                              r"(?:신고|표시|처리|등록|지정|분류|이동|보내|보냄|옮기|옮겨|옮김|버리|버려|넣)")),
    (BLOCK_KEYWORD, re.compile(r"(?:사용자|유저|계정|번호|전화번호|발신자|발신번호|보낸사람|연락처|친구|회원|아이디|"
                               r"상대방|상대|채널|작성자|메일주소|주소|도메인|수신)(?:을|를|은|는)?(?:영구)?차단"
                               r"(?!된)")),
)
#: B3: 따옴표·@·메일 주소로 대상을 지목한 차단('Block "Alice"' · 'Block @alice' · '"Alice" 차단' · 'Alice님 차단').
#: 정규화(NFKC·소문자) 뒤 원문 전체에 맞춘다 — 'Unblock "Alice"'·'Block quote' 는 맞지 않는다.
_IRR_QUOTED = r"[\"'\u201c\u2018\u00ab\u300c\u300e][^\"'\u201d\u2019\u00bb\u300d\u300f]+[\"'\u201d\u2019\u00bb\u300d\u300f]"
_IRR_BLOCK_TARGET_RE = re.compile(
    r"^block\s+(?:" + _IRR_QUOTED + r"|@[\w.\-]+|[\w.+\-]+@[\w\-]+\.[\w.\-]+)$"
    r"|^(?:" + _IRR_QUOTED + r"|\S+님)\s*(?:을|를)?\s*차단(?:하기|해|함)?$"
)
#: B3: 고유명 대상('Block Alice' · 'Block Alice Kim') — **원문 대소문자**로 Block + 대문자로 시작하는 낱말 1~2개.
#: 기능 이름(Block Ads·Block Cookies·Block Editor·Block Layout …)은 대상이 사람이 아니므로 뺀다(첫 낱말 기준).
_IRR_BLOCK_PROPER_RE = re.compile(r"^Block\s+([A-Z][\w'.\-]*)(?:\s+[A-Z][\w'.\-]*)?$")
_IRR_BLOCK_FEATURES = frozenset({
    "ad", "ads", "adverts", "advertising", "cookie", "cookies", "popup", "popups", "pop", "pop-ups", "pop-up", "image",
    "images", "tracker", "trackers", "tracking", "notification", "notifications", "editor", "quote", "quotes", "layout",
    "element", "elements", "list", "lists", "content", "settings", "setting", "all", "everything", "script", "scripts",
    "javascript", "site", "sites", "website", "websites", "request", "requests", "third-party", "third", "sound",
    "sounds", "video", "videos", "autoplay", "ip", "country", "countries", "keyword", "keywords", "word", "words",
    "comment", "comments", "size", "style", "styles", "text", "type", "height", "width", "storage", "location",
    "camera", "microphone", "mic", "download", "downloads", "redirect", "redirects", "font", "fonts", "banner",
    "banners", "page", "pages", "mode", "view", "format", "code", "theme", "template", "plugin", "plugins", "level",
    "inserter", "patterns", "pattern", "directory", "spam", "junk", "explorer", "chain", "game", "games", "puzzle",
    "time", "schedule", "calendar", "mentions", "replies", "invites", "invitations", "requests", "messages", "calls",
})
#: 라벨 전체가 이 낱말이면 동작(한국어 동작 명사형). '광고 차단 안내'·'비우기 안내'는 전체가 아니다.
_IRR_KO_WHOLE = (("차단", BLOCK_KEYWORD), ("차단하기", BLOCK_KEYWORD), ("비우기", EMPTY_KEYWORD))


def _irr_skip(tokens: List[str], start: int, allowed: frozenset, limit: int = 3) -> int:
    j = start
    while j < len(tokens) and j - start < limit and tokens[j] in allowed:
        j += 1
    return j


def _irreversible_tokens(tokens: List[str]) -> Optional[str]:
    for i, tok in enumerate(tokens):
        if i > 0 and tokens[i - 1] in _IRR_NEG_BEFORE:
            continue
        end: Optional[int] = None
        keyword = ""
        m = _IRR_COMPOUND_RE.match(tok)
        if m:
            end, keyword = i + 1, (SPAM_KEYWORD if tok.startswith(("report", "mark")) else
                                   BLOCK_KEYWORD if tok.startswith("block") else
                                   EMPTY_KEYWORD if tok.startswith("empty") else TRASH_KEYWORD)
        elif tok in _IRR_MOVE_VERBS:
            j = _irr_skip(tokens, i + 1, _IRR_ITEMS)
            if j < len(tokens) and tokens[j] in ("to", "into", "in"):
                j = _irr_skip(tokens, j + 1, _IRR_FILLERS)
                if j < len(tokens) and tokens[j] in _IRR_TRASH_DEST:
                    if tokens[j] == "recycle" and j + 1 < len(tokens) and tokens[j + 1] == "bin":
                        j += 1
                    end, keyword = j + 1, (SPAM_KEYWORD if tokens[j] in _IRR_SPAM else TRASH_KEYWORD)
        elif tok == "trash":
            if i + 1 < len(tokens) and tokens[i + 1] in _IRR_ITEMS:
                end, keyword = _irr_skip(tokens, i + 1, _IRR_ITEMS), TRASH_KEYWORD
        elif tok == "empty":
            j = _irr_skip(tokens, i + 1, _IRR_FILLERS)
            if j < len(tokens) and tokens[j] in _IRR_EMPTY_DEST:
                end, keyword = j + 1, EMPTY_KEYWORD
        elif tok == "report":
            j = _irr_skip(tokens, i + 1, _IRR_ITEMS | {"as"})
            if j < len(tokens) and tokens[j] in _IRR_SPAM:
                end, keyword = j + 1, SPAM_KEYWORD
        elif tok == "mark":
            j = _irr_skip(tokens, i + 1, _IRR_ITEMS)
            if j + 1 < len(tokens) and tokens[j] == "as" and tokens[j + 1] in _IRR_SPAM:
                end, keyword = j + 2, SPAM_KEYWORD
        elif tok == "block":
            j = _irr_skip(tokens, i + 1, _IRR_FILLERS - {"this"})
            if j < len(tokens) and tokens[j] in _IRR_BLOCK_OBJ:
                end, keyword = j + 1, BLOCK_KEYWORD
        if end is None:
            continue
        # WS-76 R1 B2: 안내 꼬리는 뒤 어디든, 안내 머리는 앞 어디든.
        if any(t in _IRR_NEG_AFTER for t in tokens[end:]) or any(t in _IRR_INFO_BEFORE for t in tokens[:i]):
            continue
        return keyword
    return None


def irreversible_action(source: str, text: str) -> Optional[str]:
    """이 원천의 문자열이 되돌리기 어려운 메일·계정 조작(휴지통·비우기·스팸 신고·차단)을 말하면 근거 키워드."""
    if not text or source in META_SOURCES:
        return None
    raw = str(text)
    if source in PATH_SOURCES:
        try:
            raw = unquote_plus(urlsplit(raw).path)
        except Exception:  # noqa: BLE001 - 해석 실패 = 원문
            pass
        if _IRR_INFO_PATH & set(class_tokens(raw)):
            return None
    if source == "class":
        for cls in raw.split():
            hit = _irreversible_tokens(_acct_tokens(cls))
            if hit:
                return hit
        return None
    hit = _irreversible_tokens(_acct_tokens(raw))
    if hit or source in TOKEN_SOURCES:
        return hit
    norm = normalize_gate_text(raw)
    if source not in PATH_SOURCES and _IRR_BLOCK_TARGET_RE.match(norm):
        return BLOCK_KEYWORD
    if source not in PATH_SOURCES and norm.startswith("block "):
        proper = _IRR_BLOCK_PROPER_RE.match(_SPACE_RE.sub(" ", strip_invisible(unicodedata.normalize("NFKC", raw))).strip())
        if proper and proper.group(1).lower() not in _IRR_BLOCK_FEATURES:
            return BLOCK_KEYWORD
    compact = "".join(ch for ch in norm if ch.isalnum())
    if source not in PATH_SOURCES:
        for word, keyword in _IRR_KO_WHOLE:
            if compact == word:
                return keyword
    for keyword, pattern in _IRR_KO:
        for m in pattern.finditer(compact):
            if _IRR_KO_NEG_AFTER_RE.search(compact, m.end()) or _IRR_KO_INFO_BEFORE_RE.search(compact, 0, m.start()):
                continue
            return keyword
    return None


#: (단독 라벨의 정규형, 그 라벨을 기준 등급으로 돌리는 문맥 META 원천).
#: '차단' 은 탭 문맥이면 탐색(상태 필터), '비우기' 는 입력칸 옆이면 편집(검색어 지우기) — WS-76 R1 B1.
_LONE_EXEMPT = (("차단", "tab_context"), ("비우기", "edit_context"))
#: 단독 'Trash' 를 동작으로 보는 역할 — WS-76 R1 B3. 링크·탭·메뉴·트리 항목(폴더 목록)은 탐색이라 뺀다.
_TRASH_ACTION_ROLES = frozenset({"button"})
#: 단독 'Trash' 를 볼 이름 원천(문자열 원천 중 대상 자신의 이름류).
_LONE_NAME_SOURCES = frozenset({"name", "text", "aria", "value", "title"})


def lone_label(text: str) -> str:
    """라벨 전체의 비교용 정규형 — 글자·숫자만 남긴다(`차단`·`비우기`·`trash`)."""
    return "".join(ch for ch in normalize_gate_text(text) if ch.isalnum())


#: B4 규칙: 마지막 조각이 조작(trash·spam·block…)이고 바로 앞 조각이 항목 id(숫자 포함)이거나 쿼리 action·op·cmd 값이 조작이면 항목 조작(HIGH), 그 밖(/mail/trash·/spam·?folder=trash)은 폴더 탐색.
_ITEM_OPS = frozenset({"trash", "spam", "junk", "block", "report-spam", "reportspam", "move-to-trash", "movetotrash",
                       "mark-as-spam", "markasspam", "report-junk", "block-sender", "block-user"})
_ITEM_OP_QUERY_KEYS = frozenset({"action", "op", "operation", "cmd", "command", "do", "act"})
_ITEM_ID_RE = re.compile(r"^(?=[0-9a-z_\-]*\d)[0-9a-z_\-]{1,64}$")


def item_operation_destination(url: str, path_only: bool = False) -> Optional[str]:
    """목적지 URL 이 **개별 항목 조작**(휴지통·스팸·차단)이면 근거 키워드 (WS-76 R1 B4)."""
    try:
        parts = urlsplit(str(url or ""))
        path, query = unquote_plus(parts.path), parts.query
    except ValueError:
        return None
    segments = [seg.lower() for seg in path.split("/") if seg]
    if _IRR_INFO_PATH & set(class_tokens(path)):
        return None
    if len(segments) >= 2 and segments[-1] in _ITEM_OPS and _ITEM_ID_RE.match(segments[-2]):
        return _item_op_keyword(segments[-1])
    if path_only:
        return None
    for pair in query.split("&"):
        key, _, value = pair.partition("=")
        if unquote_plus(key).strip().lower() in _ITEM_OP_QUERY_KEYS:
            op = unquote_plus(value).strip().lower().replace("_", "-")
            if op in _ITEM_OPS:
                return _item_op_keyword(op)
    return None


def _item_op_keyword(op: str) -> str:
    if "block" in op:
        return BLOCK_KEYWORD
    if "spam" in op or "junk" in op:
        return SPAM_KEYWORD
    return TRASH_KEYWORD


@dataclass
class RiskAssessment:
    """위험 판정 + 근거(운영자가 왜 막혔는지 알 수 있게)."""

    risk: RiskLevel
    reason: str
    basis: Dict[str, Any]


@dataclass
class ActionContext:
    """HITL 판정에 필요한 액션 맥락."""

    action: ActionType
    #: 대상 요소의 접근성 이름 또는 버튼 라벨
    element_name: str = ""
    #: 셀렉터 또는 element_id
    selector: str = ""
    #: 대상 도메인
    domain: str = ""
    #: 폼 제출을 유발하는가 (press_enter, submit 버튼 등)
    submits_form: bool = False
    #: 금액 등 부가 정보 (ConfirmDialog 메시지에 사용)
    detail: str = ""
    #: 대상을 특정·판정할 수 없었던 사유 (WS-30). 비어 있지 않으면 고위험으로 본다
    #: (fail-closed) — 예: selector 가 0개/여러 개에 맞음, 포커스 요소를 읽을 수 없음.
    unresolved_target: str = ""
    #: 이름 밖 문맥 신호 (WS-31) — (원천, 텍스트) 쌍. 원천: text·aria·title·alt·svg_title·
    #: child_aria·value·pseudo(문자열), href·formaction·form_action(경로 토큰),
    #: class·id·name_attr·testid(식별자 토큰). 페이지에서 읽은 그대로 넘기면 여기서 정규화한다.
    signals: Tuple[Tuple[str, str], ...] = ()
    #: 판정 근거에 덧붙일 사실(좌표 해석 결과 등) — 판정에는 쓰지 않는다.
    basis_extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HITLDecision:
    """HITL 게이트 판정 결과."""

    allowed: bool
    risk: RiskLevel
    requires_confirmation: bool
    reason: str
    error_code: Optional[ErrorCode] = None
    dialog: Optional[ConfirmDialog] = None
    #: 판정 근거 {name, matched_keyword, source, …} (WS-31) — 결과 data.gate_basis 로 나간다.
    basis: Dict[str, Any] = field(default_factory=dict)


def _contains_keyword(text: str, keywords: Sequence[str]) -> Optional[str]:
    lowered = text.lower()
    for kw in keywords:
        if kw.lower() in lowered:
            return kw
    return None


def _basis(ctx: ActionContext, keyword: Optional[str], source: Optional[str]) -> Dict[str, Any]:
    basis: Dict[str, Any] = {
        "name": ctx.element_name,
        "matched_keyword": keyword,
        "source": source,
    }
    for key, value in ctx.basis_extra.items():
        basis.setdefault(key, value)
    return basis


def _first_hit(
    sources: Sequence[Tuple[str, str]], path_only: bool = False, exempt: Sequence[str] = ()
) -> Optional[Tuple[str, str]]:
    for source, text in sources:
        hit = match_signal(source, text, path_only=path_only)
        if hit:
            # WS-76 R1 B1: 탐색·편집 문맥이 확인된 단독 '차단'·'비우기' 는 이 원천에서 고위험이 아니다(다른 원천은 계속 본다).
            if exempt and lone_label(text) in exempt:
                continue
            return hit, source
    return None


def _lone_context(ctx: ActionContext) -> Tuple[Tuple[str, ...], str]:
    """(문맥 예외가 확인된 단독 라벨들, 대상 역할) — META 원천 `tab_context`·`edit_context`·`role` 에서 (WS-76 R1)."""
    present = set()
    role = ""
    for src, text in ctx.signals:
        src = str(src)
        if src == "role":
            role = str(text or "").strip().lower().split(" ")[0]
        elif src in ("tab_context", "edit_context") and str(text or "").strip():
            present.add(src)
    return tuple(word for word, src in _LONE_EXEMPT if src in present), role


def _lone_trash_action(ctx: ActionContext, role: str) -> Optional[str]:
    """단독 'Trash' 가 동작 역할(button)이면 근거 원천 (WS-76 R1 B3). 역할을 모르면 기준 그대로(None)."""
    if role not in _TRASH_ACTION_ROLES:
        return None
    for src, text in [("name", ctx.element_name)] + [(str(s), str(t or "")) for s, t in ctx.signals]:
        if src in _LONE_NAME_SOURCES and lone_label(text) == "trash":
            return src
    return None


def _has_letters(text: str) -> bool:
    """글자(문자·숫자)가 있는가 — 이모지·기호만 있는 이름은 '빈 이름'으로 본다(아이콘 버튼)."""
    return any(unicodedata.category(ch)[0] in "LN" for ch in normalize_gate_text(text))


def _effective_signals(ctx: ActionContext) -> List[Tuple[str, str]]:
    """원천별 적용 규칙(WS-31 R1 NB-6)을 거친 문맥 신호.

    * 같은 출처 이동 링크(`nav_link`)면 마크업 식별자·title 원천을 뺀다.
    * 대상 자신의 title 은 보조 설명이라 이름에 글자가 없을 때만 쓴다(아이콘 버튼 title='결제'
      는 차단, '저장' 버튼의 title 문장이 '결제'를 언급하는 것은 통과). 자손 title 은 그대로.
    """
    raw = [(str(src), str(text or "")) for src, text in ctx.signals]
    nav_link = any(src == "nav_link" for src, _ in raw)
    named = _has_letters(ctx.element_name)
    out: List[Tuple[str, str]] = []
    for src, text in raw:
        if src in META_SOURCES:
            continue
        if nav_link and src in _NAV_LINK_SKIPPED:
            continue
        if src == "title" and named:
            continue
        out.append((src, text))
    return out


def assess_risk(ctx: ActionContext) -> RiskAssessment:
    """위험 등급 + 판정 근거. 고위험 키워드는 이름·셀렉터·부가정보 다음 문맥 신호 순서로 찾는다."""
    if ctx.unresolved_target:
        return RiskAssessment(
            RiskLevel.HIGH,
            f"대상 판정 불가({ctx.unresolved_target}) — 안전하게 차단",
            _basis(ctx, None, "unresolved"),
        )

    # 순서: 이름·셀렉터·부가정보 → 폼 제출 → 문맥 신호. 폼 제출 판정(WS-30)이 문맥 신호보다
    # 먼저여야 기존 사유('폼 제출 액션')가 유지된다 — 어느 쪽이든 고위험이다.
    own: List[Tuple[str, str]] = [
        ("name", ctx.element_name), ("selector", ctx.selector), ("detail", ctx.detail),
    ]
    exempt, role = _lone_context(ctx) if ctx.signals else ((), "")
    found = _first_hit(own, exempt=exempt)
    if found is None and ctx.submits_form:
        return RiskAssessment(RiskLevel.HIGH, "폼 제출 액션", _basis(ctx, None, "form_submit"))
    if found is None:
        get_submit = any(
            str(src) == "form_method" and str(text or "").strip().lower() == "get"
            for src, text in ctx.signals
        )
        found = _first_hit(_effective_signals(ctx), path_only=get_submit, exempt=exempt)
    if found is None and role:
        trash_source = _lone_trash_action(ctx, role)
        if trash_source:
            found = TRASH_KEYWORD, trash_source
    if found is not None:
        hit, source = found
        return RiskAssessment(
            RiskLevel.HIGH,
            f"고위험 키워드 '{hit}' 탐지(출처 {source})",
            _basis(ctx, hit, source),
        )

    if ctx.action in _INHERENTLY_RISKY:
        return RiskAssessment(
            RiskLevel.HIGH,
            f"부작용이 큰 액션: {ctx.action.value}",
            _basis(ctx, None, "action"),
        )

    haystack = normalize_gate_text(f"{ctx.element_name} {ctx.selector} {ctx.detail}")
    hit = _contains_keyword(haystack, MEDIUM_RISK_KEYWORDS)
    if hit:
        return RiskAssessment(
            RiskLevel.MEDIUM, f"중위험 키워드 '{hit}' 탐지", _basis(ctx, hit, "name")
        )

    return RiskAssessment(RiskLevel.LOW, "고위험 신호 없음", _basis(ctx, None, None))


def classify_risk(ctx: ActionContext) -> tuple[RiskLevel, str]:
    """액션의 위험 등급을 판정한다."""
    assessed = assess_risk(ctx)
    return assessed.risk, assessed.reason


def _build_dialog(ctx: ActionContext, reason: str) -> ConfirmDialog:
    """승인 요청 모달을 생성한다 (PRD §6.1 정형 스키마)."""
    target = ctx.element_name or ctx.selector or ctx.action.value
    message = f"'{target}' 에 대한 {ctx.action.value} 액션을 실행합니다."
    if ctx.domain:
        message += f"\n대상 도메인: {ctx.domain}"
    if ctx.detail:
        message += f"\n{ctx.detail}"
    message += f"\n\n판정 근거: {reason}"

    return ConfirmDialog(
        title="고위험 액션 승인 요청",
        message=message,
        confirm_label="실행",
        cancel_label="취소",
        danger_level=DangerLevel.HIGH,
    )


@dataclass
class HITLGate:
    """실행 모드별 고위험 액션 게이트."""

    mode: ExecutionMode = ExecutionMode.UNATTENDED
    #: 무인 모드에서 사전 승인된 액션 식별자 집합.
    #: 형식: "click:결제 진행" 또는 "click:*" (액션 전체 승인)
    pre_approved_actions: Sequence[str] = field(default_factory=tuple)

    def _is_pre_approved(self, ctx: ActionContext) -> bool:
        specific = f"{ctx.action.value}:{ctx.element_name}"
        wildcard = f"{ctx.action.value}:*"
        return specific in self.pre_approved_actions or wildcard in self.pre_approved_actions

    def evaluate(self, ctx: ActionContext) -> HITLDecision:
        """액션 실행 가부를 판정한다."""
        assessed = assess_risk(ctx)
        risk, reason, basis = assessed.risk, assessed.reason, assessed.basis

        # 저·중위험은 그대로 통과 (관측만)
        if risk is not RiskLevel.HIGH:
            return HITLDecision(
                allowed=True,
                risk=risk,
                requires_confirmation=False,
                reason=reason,
                basis=basis,
            )

        # --- 고위험 ---
        if self.mode is ExecutionMode.UNATTENDED:
            if self._is_pre_approved(ctx):
                return HITLDecision(
                    allowed=True,
                    risk=risk,
                    requires_confirmation=False,
                    reason=f"사전 승인 목록에 존재 ({reason})",
                    basis=basis,
                )
            # 기본값은 차단이다.
            return HITLDecision(
                allowed=False,
                risk=risk,
                requires_confirmation=False,
                reason=f"무인 모드에서 사전 승인되지 않은 고위험 액션 ({reason})",
                error_code=ErrorCode.HITL_UNATTENDED_BLOCKED,
                basis=basis,
            )

        # 대화형: 승인 모달을 띄우고 사용자 응답을 기다린다.
        return HITLDecision(
            allowed=False,  # 승인 전까지는 실행 불가
            risk=risk,
            requires_confirmation=True,
            reason=f"사용자 승인 필요 ({reason})",
            dialog=_build_dialog(ctx, reason),
            basis=basis,
        )
