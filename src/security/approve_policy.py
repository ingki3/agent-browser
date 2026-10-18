"""사이트별 상시 허용 정책 — `agent-browser approve-policy add|list|remove` (WS-56).

고위험 액션이 HITL 게이트에 막혔을 때, 사람이 미리 정해 둔 (origin, 액션, [대상 이름]) 정책과 맞으면
승인 없이 실행한다(결과 `data.approval.via="policy"`). 정책은 **사람 CLI 만** 추가한다 — MCP 도구·서버 도구
어디에도 추가 수단이 없다. 저장 위치는 serve 영속 프로필 폴더 안 0600 파일(`POLICY_FILE`)이고, 서버는 게이트가
정책을 볼 때마다 파일 상태(stat)를 보고 바뀌었으면 다시 읽는다(서버 메모리 기준).

최고위험 범주(조정자 승인 2026-10-18)는 정책으로 열 수 없다 — 게이트가 맞춘 키워드 하나(matched_keyword)가
아니라 이름·selector·부가정보·문맥 신호 **전 원천**을 다시 훑어 범주 키워드가 하나라도 있으면 거부한다
('제출' 버튼인데 문맥에 '결제'가 있으면 거부). 범주:

* payment — 돈 이동(되돌릴 수 없는 금전 손실)
* account_termination — 계정 해지·탈퇴(계정 단위 비가역)
* permission — 권한 변경(보안 상승)
* unresolved_target — 판정 불가 대상(무엇을 누를지 모르면 정책 일치를 판단할 수 없다)
* upload_file — 로컬 파일 유출(정책은 사이트 단위라 파일 범위를 묶지 못한다)

fail-closed: 파일 손상·형식 오류·권한 느슨함(0600 아님)·심볼릭 링크·다른 소유자 → 정책 없음.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import secrets
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote_plus, urlsplit

from contracts import ActionType
from security.hitl import (
    _BOOTSTRAP_ORDER_RE,
    _ICON_PREFIXES,
    ICON_CLASS_RISK,
    META_SOURCES,
    PATH_SOURCES,
    TOKEN_SOURCES,
    ActionContext,
    class_tokens,
    normalize_gate_text,
    path_tokens,
)

logger = logging.getLogger(__name__)

#: 프로필 폴더 안 정책 파일 이름.
POLICY_FILE = "approve_policy.json"
FILE_VERSION = 1

#: 정책으로 열 수 없는 키워드 범주(기존 HIGH_RISK_KEYWORDS 의 부분집합 — 테스트로 고정).
FORBIDDEN_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "payment": ("결제", "구매", "주문", "송금", "이체", "출금", "pay", "purchase", "checkout",
                "order", "transfer", "withdraw", "subscribe"),
    "account_termination": ("탈퇴", "해지", "deactivate", "cancel account", "terminate"),
    "permission": ("권한", "관리자", "permission", "admin", "grant", "revoke"),
}
CATEGORY_UNRESOLVED = "unresolved_target"
#: 정책으로 열 수 없는 액션 → 범주 이름.
FORBIDDEN_ACTIONS: Dict[ActionType, str] = {ActionType.UPLOAD_FILE: "upload_file"}

_KEYWORD_CATEGORY: Dict[str, str] = {kw: cat for cat, kws in FORBIDDEN_KEYWORDS.items() for kw in kws}


class PolicyError(ValueError):
    """정책 값·파일이 잘못됨(fail-closed 사유)."""


# ---------------------------------------------------------------------------- 최고위험 판정


def _text_category(text: str) -> Optional[str]:
    """문자열 원천: 부분 문자열(게이트 _match_text 와 같은 정규화). 범주 순서대로 모든 키워드를 본다."""
    norm = normalize_gate_text(text)
    if not norm:
        return None
    for cat, kws in FORBIDDEN_KEYWORDS.items():
        for kw in kws:
            if kw.lower() in norm:
                return cat
    return None


def _token_category(tokens: List[str]) -> Optional[str]:
    present = set(tokens)
    for cat, kws in FORBIDDEN_KEYWORDS.items():
        for kw in kws:
            if kw.isascii() and " " not in kw and kw in present:
                return cat
    return None


def _icon_keyword(cls: str) -> Optional[str]:
    """아이콘 클래스 → 게이트가 보고하는 키워드(hitl._match_icon_class 와 같은 접두사·사전)."""
    lowered = cls.strip().lower()
    for prefix in _ICON_PREFIXES:
        if lowered.startswith(prefix):
            stem = lowered[len(prefix):].replace("_", "-")
            for key, keyword in ICON_CLASS_RISK.items():
                if stem == key or stem.startswith(key + "-"):
                    return keyword
    return None


def _source_category(source: str, text: str) -> Optional[str]:
    if not text or source in META_SOURCES:
        return None
    if source in PATH_SOURCES:
        try:
            decoded = unquote_plus(text)
        except Exception:  # noqa: BLE001 - 디코딩 실패 = 원문으로 본다
            decoded = text
        return _token_category(path_tokens(text)) or _text_category(decoded)
    if source in TOKEN_SOURCES:
        parts = text.split() if source == "class" else [text]
        if source == "class":
            for cls in parts:  # 아이콘 사전(fa-credit-card → pay)
                icon = _icon_keyword(cls)
                if icon and _KEYWORD_CATEGORY.get(icon):
                    return _KEYWORD_CATEGORY[icon]
        tokens = [t for p in parts if not (source == "class" and _BOOTSTRAP_ORDER_RE.match(p.lower()))
                  for t in class_tokens(p)]
        return _token_category(tokens)
    return _text_category(text)


def forbidden_category(ctx: ActionContext) -> Optional[str]:
    """정책으로 열 수 없는 범주 이름(없으면 None). 전 원천을 다시 훑는다."""
    if ctx.action in FORBIDDEN_ACTIONS:
        return FORBIDDEN_ACTIONS[ctx.action]
    if ctx.unresolved_target:
        return CATEGORY_UNRESOLVED
    sources: List[Tuple[str, str]] = [
        ("name", ctx.element_name), ("selector", ctx.selector), ("detail", ctx.detail),
    ]
    sources += [(str(src), str(text or "")) for src, text in ctx.signals]
    for src, text in sources:
        cat = _source_category(src, text)
        if cat is not None:
            return cat
    return None


# ---------------------------------------------------------------------------- 값 정규화


def normalize_origin(raw: Any) -> str:
    """`https://host[:port]` 만 받는다(경로·쿼리·사용자정보 금지). 소문자, 기본 포트는 생략."""
    text = str(raw or "").strip()
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise PolicyError(f"origin 형식이 아닙니다: {exc}") from None
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https") or not parts.hostname:
        raise PolicyError("origin 은 http(s)://호스트[:포트] 형식이어야 합니다")
    if parts.username is not None or parts.password is not None:
        raise PolicyError("origin 에 사용자 정보(user@)를 넣을 수 없습니다")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise PolicyError("origin 에는 경로·쿼리·조각을 넣지 않습니다(사이트 단위 정책)")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default = {"http": 80, "https": 443}[scheme]
    return f"{scheme}://{host}" + (f":{port}" if port is not None and port != default else "")


def _origin_key(origin: str) -> str:
    """서버의 현재 origin(`scheme://netloc` 소문자)을 정책과 같은 형태로. 실패하면 빈 값(불일치)."""
    try:
        return normalize_origin(origin)
    except PolicyError:
        return ""


def _action(raw: Any) -> ActionType:
    try:
        return ActionType(str(raw or "").strip())
    except ValueError:
        names = ", ".join(a.value for a in ActionType)
        raise PolicyError(f"알 수 없는 액션: {raw!r} (가능: {names})") from None


def _target(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    text = normalize_gate_text(str(raw))
    if not text:
        raise PolicyError("대상 이름이 비었습니다(이름 없이 쓰려면 --target-name 을 빼세요)")
    return text


def _validate_entry(item: Any) -> Dict[str, Any]:
    if not isinstance(item, dict):
        raise PolicyError("정책 항목이 객체가 아닙니다")
    origin = normalize_origin(item.get("origin"))
    if origin != item.get("origin"):
        raise PolicyError("정책 origin 이 정규형이 아닙니다")
    action = _action(item.get("action"))
    if action in FORBIDDEN_ACTIONS:
        raise PolicyError(f"정책으로 열 수 없는 액션({FORBIDDEN_ACTIONS[action]})")
    target = item.get("target_name")
    if target is not None and (not isinstance(target, str) or _target(target) != target):
        raise PolicyError("정책 대상 이름이 정규형이 아닙니다")
    out: Dict[str, Any] = {"origin": origin, "action": action.value, "target_name": target}
    if isinstance(item.get("added_at"), str):
        out["added_at"] = item["added_at"]
    return out


# ---------------------------------------------------------------------------- 파일


def _check_file(path: Path) -> os.stat_result:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise PolicyError("정책 파일이 일반 파일이 아닙니다(심볼릭 링크 등)")
    if st.st_uid != os.getuid():
        raise PolicyError("정책 파일 소유자가 이 사용자가 아닙니다")
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise PolicyError("정책 파일 권한이 0600 이 아닙니다")
    return st


def read_policies(path: Path) -> List[Dict[str, Any]]:
    """정책 목록. 파일이 없으면 []. 손상·권한·링크 → PolicyError(fail-closed)."""
    path = Path(path)
    try:
        _check_file(path)
    except FileNotFoundError:
        return []
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise PolicyError(f"정책 파일을 읽을 수 없습니다({type(exc).__name__})") from None
    if not isinstance(data, dict) or data.get("version") != FILE_VERSION or not isinstance(data.get("policies"), list):
        raise PolicyError("정책 파일 형식이 아닙니다")
    return [_validate_entry(item) for item in data["policies"]]


def _write(path: Path, rows: List[Dict[str, Any]]) -> None:
    path = Path(path)
    parent = path.parent
    pst = os.lstat(parent)
    if stat.S_ISLNK(pst.st_mode) or not stat.S_ISDIR(pst.st_mode):
        raise PolicyError("프로필 자리가 일반 폴더가 아닙니다")
    if path.exists() or path.is_symlink():
        _check_file(path)
    tmp = parent / f".tmp-policy-{secrets.token_hex(6)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": FILE_VERSION, "policies": rows}, fh, ensure_ascii=True, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _same(row: Dict[str, Any], origin: str, action: str, target: Optional[str]) -> bool:
    return row["origin"] == origin and row["action"] == action and row.get("target_name") == target


def add_policy(path: Path, origin: Any, action: Any, target_name: Any = None) -> Dict[str, Any]:
    """정책 하나 추가(같은 정책이 있으면 그것을 돌려준다). 최고위험 범주는 PolicyError."""
    norm_origin = normalize_origin(origin)
    act = _action(action)
    if act in FORBIDDEN_ACTIONS:
        raise PolicyError(f"이 액션은 정책으로 열 수 없습니다(범주 {FORBIDDEN_ACTIONS[act]}) — 매번 승인하세요")
    target = _target(target_name)
    if target is not None:
        cat = forbidden_category(ActionContext(action=act, element_name=target))
        if cat is not None:
            raise PolicyError(f"이 대상은 정책으로 열 수 없습니다(범주 {cat}) — 매번 승인하세요")
    rows = read_policies(path)
    for row in rows:
        if _same(row, norm_origin, act.value, target):
            return row
    entry = {"origin": norm_origin, "action": act.value, "target_name": target,
             "added_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")}
    rows.append(entry)
    _write(path, rows)
    return entry


def remove_policy(path: Path, origin: Any, action: Any, target_name: Any = None) -> bool:
    norm_origin = normalize_origin(origin)
    act = _action(action)
    target = _target(target_name)
    rows = read_policies(path)
    kept = [r for r in rows if not _same(r, norm_origin, act.value, target)]
    if len(kept) == len(rows):
        return False
    _write(path, kept)
    return True


# ---------------------------------------------------------------------------- 서버 메모리


class PolicyStore:
    """서버가 쓰는 정책 사본. `match` 마다 파일 상태를 보고 바뀌었으면 다시 읽는다.

    읽기 실패(손상·권한·링크)면 정책 없음 + `error` 에 사유(fail-closed — 이전 메모리 값을 쓰지 않는다).
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.rows: List[Dict[str, Any]] = []
        self.error: str = ""
        self._sig: Any = object()

    def _signature(self) -> Any:
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            return ("error", type(exc).__name__)
        return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_mode, st.st_uid)

    def refresh(self) -> None:
        sig = self._signature()
        if sig == self._sig:
            return
        self._sig = sig
        try:
            self.rows = read_policies(self.path)
            self.error = ""
        except PolicyError as exc:
            self.rows = []
            self.error = str(exc)
            logger.warning("approve-policy 파일을 쓰지 않습니다(fail-closed): %s", exc)

    def match(self, origin: str, action: ActionType, element_name: str) -> Optional[Dict[str, Any]]:
        self.refresh()
        key = _origin_key(origin)
        if not key:
            return None
        name = normalize_gate_text(element_name or "")
        for row in self.rows:
            if row["origin"] != key or row["action"] != action.value:
                continue
            target = row.get("target_name")
            if target is None or target == name:
                return dict(row)
        return None
