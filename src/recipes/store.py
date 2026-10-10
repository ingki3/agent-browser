"""레시피 저장소 (WS-38 단계 2, 설계 §1·§2·§4-1·§5).

형식(recipes.json, 프로필당 1개)::

    {"version": 1,
     "index":   {"<origin>": {"<첫 단계 URL 패턴>": ["r_ab12", ...]}},   ← 찾기용 색인
     "recipes": {"r_ab12": {id, name, origin, params, index_pat, steps[], stats}}}

Step::

    {"action": "click", "args": {...인자 틀, 입력 글자는 "{query}"...},
     "origin": "...", "url_pat": "...",                       ← PageKey(출처·URL 패턴)
     "variants": [{"skel": 골격 12자, "ready": 요소 수, "target": Target|None}],   ← A/B 화면별
     "expect": {"nav": "none|same|cross", "url_pat": 이동 뒤 패턴|None, "signals": [종류]}}

* 저장은 에이전트가 명시할 때만(compile_recipe → RecipeStore.save). 자동 저장 없음.
* 입력 글자는 params 자리표시자로만 — 치환되지 않은 글자가 남으면 저장 거부(개인정보), 비밀번호 칸
  입력은 params 여도 거부. 쿠키·승인 증표·자격 증명·입력 원문·페이지 본문은 저장하지 않는다.
* 쓰기: 레시피 변경(save/delete/비활성)은 즉시(write-through, 임시 파일+rename, 0600). 실행 통계는
  최대 STATS_FLUSH_S 초 모아서(종료 때 마지막 쓰기) — 강제 종료 시 잃는 것은 최근 통계뿐.
* 읽기 실패(손상·모양 다름·심볼릭 링크)면 빈 저장소로 시작하고 원본은 옆에 보존·경고(fail-closed).
* 상한: 레시피 MAX_RECIPES 개, 단계 MAX_STEPS 개, 파일 MAX_FILE_BYTES — 넘으면 오래 안 쓴 것부터 정리.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import secrets as _secrets
import stat
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote, quote_plus, unquote_plus, urlsplit, urlunsplit

from recipes import keys

logger = logging.getLogger(__name__)

VERSION = 1
MAX_RECIPES = 200
MAX_STEPS = 20
MAX_FILE_BYTES = 1_000_000
#: 실행 통계를 모아 쓰는 최대 간격(초).
STATS_FLUSH_S = 5.0
#: 연속 실패가 이만큼이면 비활성(에이전트가 다시 save 하면 갱신).
DISABLE_AFTER_FAILS = 3
#: 레시피 이름·detail 길이 상한(에이전트에게 다시 나가는 문자열).
TEXT_LIMIT = 80

#: 기록·저장하는 액션(이 밖의 액션은 궤적에 넣지 않는다).
RECORDABLE = ("navigate", "click", "type_text", "select_option", "check_box", "press_key")
#: 대상 요소가 있는 액션.
ELEMENT_ACTIONS = ("click", "type_text", "select_option", "check_box")

_PARAM_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]{0,31})(:url)?\}")
_BIDI = set("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f")


class RecipeError(ValueError):
    """저장·재생을 거부하는 이유(에이전트에게 그대로 알린다)."""


def clean_text(value: Any, limit: int = TEXT_LIMIT) -> str:
    """에이전트에게 다시 나가는 문자열 살균: 제어·서식·양방향 문자 제거, 공백 정리, limit 자."""
    text = value if isinstance(value, str) else str(value or "")
    out = []
    for ch in text:
        if ch in _BIDI or unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp", "Co", "Cs"):
            out.append(" ")
            continue
        out.append(ch)
    cleaned = re.sub(r"\s+", " ", "".join(out)).strip()
    return cleaned[:limit]


# ---------------------------------------------------------------------------
# 궤적 → 레시피 (params 치환·거부)
# ---------------------------------------------------------------------------


def _sub_text(text: str, params: Dict[str, str], used: set) -> str:
    out = text
    for name, value in sorted(params.items(), key=lambda kv: -len(kv[1])):
        if value and value in out:
            out = out.replace(value, "{" + name + "}")
            used.add(name)
    leftover = _PLACEHOLDER.sub("", out)
    if leftover.strip():
        raise RecipeError(
            "params 로 치환되지 않은 입력 글자가 남아 저장하지 않았습니다(입력 원문은 저장하지 않음 — "
            "입력한 글자 전체를 params 값으로 주십시오)."
        )
    return out


#: 값이 params 로 지정되면 save 를 거부하는 쿼리 키(R1 NB-2). 긴 낱말은 키 어디에 있어도, 짧은 낱말은
#: 키 조각(영숫자 경계·camelCase)으로 같거나 끝에 붙을 때만 — keyword·author·side 같은 흔한 키는 통과.
_SENSITIVE_SUB = re.compile(r"token|session|passw|secret|e-?mail|csrf|xsrf", re.I)
_SENSITIVE_PART = frozenset({"sid", "key", "code", "auth", "authorization", "pw", "pwd", "otp"})
_SENSITIVE_SUFFIX = ("sid", "key")


def _sensitive_key(key: str) -> bool:
    if _SENSITIVE_SUB.search(key):
        return True
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", key)
    parts = [p.lower() for p in re.split(r"[^A-Za-z0-9]+|\s+", spaced) if p]
    if any(p in _SENSITIVE_PART for p in parts):
        return True
    low = key.lower()
    return any(low.endswith(s) for s in _SENSITIVE_SUFFIX)


def _sub_piece(piece: str, params: Dict[str, str], used: set) -> str:
    """URL 조각(경로 조각 하나·쿼리 값 하나) 안에서만 params 치환. 자리표시자는 늘 `:url`(재생 때
    퍼센트 인코딩 — 값의 `/ ? # @` 가 조각 밖으로 새지 못한다). 이미 만든 자리표시자 안은 다시 안 본다."""
    out = piece
    for name, value in sorted(params.items(), key=lambda kv: -len(kv[1])):
        if not value:
            continue
        for form in sorted({quote(value, safe=""), quote_plus(value), value}, key=len, reverse=True):
            if not form:
                continue
            chunks = re.split(r"(\{[A-Za-z_][A-Za-z0-9_]{0,31}(?::url)?\})", out)
            for k in range(0, len(chunks), 2):  # 짝수 칸 = 자리표시자 밖
                if form in chunks[k]:
                    chunks[k] = chunks[k].replace(form, "{" + name + ":url}")
                    used.add(name)
            out = "".join(chunks)
    return out


def _sub_url(url: str, params: Dict[str, str], used: set,
             dropped: Optional[List[str]] = None) -> str:
    """navigate URL 의 params 치환(R1 NB-1·NB-2).

    * scheme·host·port 는 그대로(치환 금지 — 재생이 다른 출처로 새지 않게). 사용자 정보(user:pass@)·
      `#` 이하는 버린다.
    * 경로: 조각(`/` 사이)마다 치환.
    * 쿼리: 값마다 치환. 자리표시자만 남지 않은 값(치환 안 된 원문)은 버리고 키만 남긴다 — 토큰·이메일
      같은 원문이 파일에 남지 않게. 버린 키는 dropped 에.
    * 민감 키(token·session·sid·auth·key·code·email·password·secret 류)의 값을 params 로 지정하면 거부.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        raise RecipeError("이동 URL 을 해석할 수 없어 저장하지 않습니다.") from None
    netloc = parts.netloc.rsplit("@", 1)[-1]
    path = "/".join(_sub_piece(seg, params, used) for seg in parts.path.split("/"))
    pairs = []
    for raw in parts.query.split("&") if parts.query else []:
        key, sep, value = raw.partition("=")
        if value:
            mine: set = set()
            sub = _sub_piece(value, params, mine)
            if _PLACEHOLDER.sub("", sub):  # 원문이 남음 → 값 버림(이 값에서 쓴 params 도 안 씀)
                value = ""
                if dropped is not None:
                    dropped.append(clean_text(unquote_plus(key), 40))
            else:
                if _sensitive_key(unquote_plus(key)):
                    raise RecipeError(
                        f"쿼리 키 {clean_text(unquote_plus(key), 40)!r} 는 민감 값(토큰·세션·인증·이메일 등)이라 "
                        "params 로도 저장하지 않습니다.")
                used.update(mine)
                value = sub
        pairs.append(key + sep + value if sep or value else key)
    return urlunsplit((parts.scheme, netloc, path, "&".join(pairs), ""))


def compile_recipe(
    name: Any,
    entries: List[Dict[str, Any]],
    *,
    params: Optional[Dict[str, Any]] = None,
    pins: Optional[Dict[Any, Any]] = None,
    dropped: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """기록된 궤적 항목들(기록 당시 스냅숏) → 저장할 레시피(dict, id 없음).

    dropped 를 주면 navigate URL 에서 값을 버린 쿼리 키 이름을 담는다(save 응답 안내용).
    """
    clean_name = clean_text(name)
    if not clean_name:
        raise RecipeError("레시피 이름이 비었습니다.")
    if not entries:
        raise RecipeError("저장할 단계가 없습니다(성공하고 사후 확인을 통과한 최근 동작이 없음).")
    if len(entries) > MAX_STEPS:
        raise RecipeError(f"단계가 {len(entries)}개 — 레시피당 최대 {MAX_STEPS}단계입니다.")
    params = dict(params or {})
    for pname, pvalue in params.items():
        if not isinstance(pname, str) or not _PARAM_NAME.match(pname):
            raise RecipeError(f"params 이름이 잘못됐습니다: {clean_text(pname)!r} (영문·숫자·_ 32자 이내)")
        if not isinstance(pvalue, str) or not pvalue:
            raise RecipeError(f"params.{pname} 값은 비어 있지 않은 문자열이어야 합니다.")
    pin_map: Dict[int, str] = {}
    for k, v in (pins or {}).items():
        try:
            idx = int(k)
        except (TypeError, ValueError):
            raise RecipeError(f"pins 의 키는 단계 번호여야 합니다: {clean_text(k)!r}") from None
        if not 0 <= idx < len(entries):
            raise RecipeError(f"pins 단계 번호 {idx} 가 범위(0~{len(entries) - 1}) 밖입니다.")
        pin_map[idx] = str(v)

    used: set = set()
    steps: List[Dict[str, Any]] = []
    origin = ""
    for i, e in enumerate(entries):
        action = str(e.get("action") or "")
        if action not in RECORDABLE:
            raise RecipeError(f"{i}단계 {action!r} 는 레시피에 넣을 수 없는 동작입니다.")
        desc = e.get("desc")
        args = dict(e.get("args") or {})
        if action == "type_text":
            if e.get("secret") or (desc or {}).get("secret"):
                raise RecipeError(f"{i}단계는 비밀번호 칸 입력이라 저장하지 않습니다(params 여도 거부).")
            args["text"] = _sub_text(str(args.get("text") or ""), params, used)
        if action == "navigate":
            args["url"] = _sub_url(str(args.get("url") or ""), params, used, dropped)
        target: Optional[Dict[str, Any]] = None
        if action in ELEMENT_ACTIONS:
            try:
                target = keys.make_target(desc, pin_map.get(i))
            except keys.TargetError as exc:
                raise RecipeError(f"{i}단계: {exc}") from None
        elif i in pin_map:
            raise RecipeError(f"{i}단계({action})는 대상이 없는 동작이라 pins 를 줄 수 없습니다.")
        expect = dict(e.get("expect") or {})
        step: Dict[str, Any] = {
            "action": action,
            "args": args,
            "origin": "" if action == "navigate" else str(e.get("origin") or ""),
            "url_pat": None if action == "navigate" else str(e.get("url_pat") or ""),
            "variants": ([] if action == "navigate" else
                         [{"skel": str(e.get("skel") or ""), "ready": int(e.get("ready") or 0),
                           "target": target}]),
            "expect": {"nav": str(expect.get("nav") or "none"),
                       "url_pat": expect.get("url_pat"),
                       "signals": sorted({str(s) for s in expect.get("signals") or []})},
        }
        if not origin:
            if action == "navigate":
                origin = keys.origin_of(str((e.get("args") or {}).get("url") or ""))
            else:
                origin = step["origin"]
        steps.append(step)
    unused = sorted(set(params) - used)
    if unused:
        raise RecipeError(f"params.{unused[0]} 값이 어느 입력에도 없습니다.")
    if not origin:
        raise RecipeError("출처(http/https)를 알 수 없어 저장하지 않습니다.")
    first = steps[0]
    index_pat = (first["expect"].get("url_pat") or "") if first["action"] == "navigate" else first["url_pat"]
    return {
        "name": clean_name,
        "origin": origin,
        "params": sorted(params),
        "index_pat": index_pat or "",
        "steps": steps,
    }


def render_args(step: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """저장된 인자 틀 + params → 실행 인자. 없는 params 는 거부."""
    params = {str(k): str(v) for k, v in (params or {}).items()}
    out = copy.deepcopy(step.get("args") or {})

    def _fill(text: str) -> str:
        def _one(m: "re.Match[str]") -> str:
            name, enc = m.group(1), m.group(2)
            if name not in params:
                raise RecipeError(f"params.{name} 가 필요합니다.")
            return quote(params[name], safe="") if enc else params[name]

        return _PLACEHOLDER.sub(_one, text)

    for key in ("text", "url"):
        if isinstance(out.get(key), str):
            out[key] = _fill(out[key])
    return out


def _same_shape(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return (
        a.get("name") == b.get("name")
        and a.get("origin") == b.get("origin")
        and [s["action"] for s in a.get("steps", [])] == [s["action"] for s in b.get("steps", [])]
    )


# ---------------------------------------------------------------------------
# 저장소
# ---------------------------------------------------------------------------


class RecipeStore:
    """레시피 저장소. path 가 None 이면 메모리만(서버 종료 시 소멸)."""

    def __init__(self, path: Optional[Path] = None, *,
                 clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path is not None else None
        self.clock = clock
        self.warnings: List[str] = []
        self._recipes: Dict[str, Dict[str, Any]] = {}
        self._index: Dict[str, Dict[str, List[str]]] = {}
        self._dirty_since: Optional[float] = None
        self._timer: Any = None
        self._load()

    @property
    def persistent(self) -> bool:
        return self.path is not None

    # -- 읽기 ---------------------------------------------------------------

    def _warn(self, text: str) -> None:
        self.warnings.append(text)
        logger.warning("레시피 저장소: %s", text)

    def _preserve(self, why: str) -> None:
        """읽을 수 없는 원본을 옆으로 옮겨 보존한다(새 쓰기가 덮지 않게)."""
        assert self.path is not None
        aside = self.path.with_name(f"{self.path.name}.corrupt-{int(self.clock())}-{os.getpid()}")
        try:
            os.rename(self.path, aside)
            self._warn(f"{why} — 빈 저장소로 시작, 원본은 {aside.name} 로 보존")
        except OSError as exc:
            self._warn(f"{why} — 원본을 옮기지 못해({type(exc).__name__}) 이번 실행은 메모리에만 저장")
            self.path = None

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            st_ = os.lstat(self.path)
        except FileNotFoundError:
            return
        except OSError as exc:
            self._warn(f"레시피 파일을 확인하지 못함({type(exc).__name__}) — 이번 실행은 메모리에만 저장")
            self.path = None
            return
        if stat.S_ISLNK(st_.st_mode) or not stat.S_ISREG(st_.st_mode):
            self._warn("레시피 파일이 일반 파일이 아님(심볼릭 링크 등) — 읽지 않음")
            self._preserve("레시피 파일이 일반 파일이 아님")
            return
        if stat.S_IMODE(st_.st_mode) & 0o077:
            try:
                os.chmod(self.path, 0o600)
                self._warn("레시피 파일 권한이 느슨해 0600 으로 좁힘")
            except OSError:
                self._warn("레시피 파일 권한이 느슨함(0600 으로 좁히지 못함)")
        try:
            raw = self.path.read_bytes()
            data = json.loads(raw.decode("utf-8"))
            recipes, index = self._validate(data)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            self._preserve(f"레시피 파일 손상({type(exc).__name__})")
            return
        self._recipes = recipes
        self._index = index

    @staticmethod
    def _validate(data: Any) -> tuple:
        if not isinstance(data, dict) or data.get("version") != VERSION:
            raise ValueError("version")
        recipes = data.get("recipes")
        if not isinstance(recipes, dict):
            raise ValueError("recipes")
        for rid, rec in recipes.items():
            if (not isinstance(rec, dict) or not isinstance(rec.get("steps"), list)
                    or not isinstance(rec.get("stats"), dict) or rec.get("id") != rid):
                raise ValueError("recipe")
        index: Dict[str, Dict[str, List[str]]] = {}
        for rid, rec in recipes.items():  # 색인은 레시피에서 다시 만든다(어긋남 방지)
            index.setdefault(str(rec.get("origin") or ""), {}).setdefault(
                str(rec.get("index_pat") or ""), []).append(rid)
        return recipes, index

    # -- 조회 ---------------------------------------------------------------

    def get(self, rid: str) -> Optional[Dict[str, Any]]:
        return self._recipes.get(rid)

    def list(self, origin: Optional[str] = None) -> List[Dict[str, Any]]:
        out = [r for r in self._recipes.values() if origin is None or r.get("origin") == origin]
        return sorted(out, key=lambda r: -float(r["stats"].get("last_used_at") or 0))

    def has_origin(self, origin: str) -> bool:
        return bool(self._index.get(origin))

    def candidates(self, origin: str, url_pat: str) -> List[Dict[str, Any]]:
        """출처 → URL 패턴 색인 조회(dict 두 번). disabled 제외, 최근 성공·사용 순."""
        ids = (self._index.get(origin) or {}).get(url_pat) or []
        out = [self._recipes[i] for i in ids if i in self._recipes
               and not self._recipes[i]["stats"].get("disabled")]
        return sorted(out, key=lambda r: (-float(r["stats"].get("last_ok_at") or 0),
                                          -float(r["stats"].get("last_used_at") or 0)))

    # -- 변경(write-through) --------------------------------------------------

    def _rebuild_index(self) -> None:
        index: Dict[str, Dict[str, List[str]]] = {}
        for rid, rec in self._recipes.items():
            index.setdefault(rec["origin"], {}).setdefault(rec["index_pat"], []).append(rid)
        self._index = index

    def save(self, recipe: Dict[str, Any]) -> Dict[str, Any]:
        """새 레시피 저장(같은 이름·출처·동작 순서면 A/B 변형으로 합치고 다시 켬). 즉시 파일에."""
        now = self.clock()
        snapshot = copy.deepcopy(self._recipes)
        existing = next((r for r in self._recipes.values() if _same_shape(r, recipe)), None)
        merged = existing is not None
        if existing is not None:
            for old, new in zip(existing["steps"], recipe["steps"]):
                old["args"] = new["args"]
                old["expect"] = new["expect"]
                for var in new["variants"]:
                    same = next((v for v in old["variants"] if v["skel"] == var["skel"]), None)
                    if same is None:
                        old["variants"].append(var)
                    else:
                        same.update(var)
            existing["params"] = recipe["params"]
            existing["stats"].update(disabled=False, fail_streak=0, last_used_at=now)
            rec = existing
        else:
            rid = "r_" + _secrets.token_hex(4)
            while rid in self._recipes:
                rid = "r_" + _secrets.token_hex(4)
            rec = dict(copy.deepcopy(recipe), id=rid, stats={
                "runs": 0, "ok": 0, "fail_streak": 0, "disabled": False,
                "created_at": now, "last_used_at": now, "last_ok_at": None,
            })
            self._recipes[rid] = rec
        self._evict(keep=rec["id"])
        if self._encoded_size() > MAX_FILE_BYTES:
            self._recipes = snapshot
            self._rebuild_index()
            raise RecipeError("레시피 1개가 파일 상한(1MB)을 넘어 저장하지 않았습니다.")
        self._rebuild_index()
        persisted = self._write()
        return {"id": rec["id"], "merged": merged, "persisted": persisted,
                "steps": len(rec["steps"]), "name": rec["name"]}

    def delete(self, rid: str) -> bool:
        if rid not in self._recipes:
            return False
        del self._recipes[rid]
        self._rebuild_index()
        self._write()
        return True

    def touch(self, rid: str) -> None:
        rec = self._recipes.get(rid)
        if rec is not None:
            rec["stats"]["last_used_at"] = self.clock()
            self._stats_dirty()

    def note_run(self, rid: str, *, ok: bool, ui_moves: Optional[Dict[tuple, List[int]]] = None,
                 count_failure: bool = True) -> Optional[Dict[str, Any]]:
        """실행 결과 통계. 성공이면 ui 대상 위치만 갱신(slot 틀·identity 값은 갱신 안 함)."""
        rec = self._recipes.get(rid)
        if rec is None:
            return None
        stats = rec["stats"]
        now = self.clock()
        stats["runs"] = int(stats.get("runs") or 0) + 1
        stats["last_used_at"] = now
        became_disabled = False
        if ok:
            stats["ok"] = int(stats.get("ok") or 0) + 1
            stats["fail_streak"] = 0
            stats["last_ok_at"] = now
            for (idx, skel), pos in (ui_moves or {}).items():
                try:
                    step = rec["steps"][idx]
                except IndexError:
                    continue
                for var in step["variants"]:
                    tgt = var.get("target")
                    if var["skel"] == skel and tgt and tgt.get("kind") == "ui":
                        tgt["pos"] = [int(pos[0]), int(pos[1])]
        elif count_failure:
            stats["fail_streak"] = int(stats.get("fail_streak") or 0) + 1
            if stats["fail_streak"] >= DISABLE_AFTER_FAILS and not stats.get("disabled"):
                stats["disabled"] = True
                became_disabled = True
        if became_disabled:
            self._write()  # 레시피 상태 변경 — 즉시
        else:
            self._stats_dirty()
        return dict(stats)

    # -- 쓰기 ---------------------------------------------------------------

    def _evict(self, keep: str) -> None:
        def lru() -> List[str]:
            return sorted((r for r in self._recipes if r != keep),
                          key=lambda r: float(self._recipes[r]["stats"].get("last_used_at") or 0))

        while len(self._recipes) > MAX_RECIPES:
            victims = lru()
            if not victims:
                break
            del self._recipes[victims[0]]
        while self._encoded_size() > MAX_FILE_BYTES:
            victims = lru()
            if not victims:
                break
            del self._recipes[victims[0]]
        self._rebuild_index()

    def _payload(self) -> bytes:
        index: Dict[str, Dict[str, List[str]]] = {}
        for rid, rec in self._recipes.items():
            index.setdefault(rec["origin"], {}).setdefault(rec["index_pat"], []).append(rid)
        data = {"version": VERSION, "index": index, "recipes": self._recipes}
        return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _encoded_size(self) -> int:
        return len(self._payload())

    def _write(self) -> bool:
        """임시 파일(0600) + rename. 실패하면 이전 파일은 그대로, 경고만."""
        self._dirty_since = None
        self._cancel_timer()
        if self.path is None:
            return False
        payload = self._payload()
        tmp = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}-{_secrets.token_hex(3)}")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
            return True
        except OSError as exc:
            self._warn(f"레시피 파일 쓰기 실패({type(exc).__name__}) — 메모리에는 남음")
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False

    def _stats_dirty(self) -> None:
        if self._dirty_since is None:
            self._dirty_since = self.clock()
        if self.path is None:
            return
        self._schedule()
        self.maybe_flush()

    def _schedule(self) -> None:
        if self._timer is not None:
            return
        try:
            import asyncio

            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._timer = loop.call_later(STATS_FLUSH_S, self._timer_fire)

    def _timer_fire(self) -> None:
        self._timer = None
        self.flush()

    def _cancel_timer(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def maybe_flush(self) -> None:
        if self._dirty_since is not None and self.clock() - self._dirty_since >= STATS_FLUSH_S:
            self.flush()

    def flush(self) -> None:
        if self._dirty_since is not None:
            self._write()

    def close(self) -> None:
        self.flush()
        self._cancel_timer()
