"""WS-51 R5 — 소스 전수 조사(AST) 공용: 제어 키 열거 어휘 생산자와 도구 식별자 생산자.

* `control_census(src)` — src/ 전체에서 제어 키(public_url.CONTROL_VOCABULARY 의 키)에 값이 실리는 자리(dict 리터럴 키,
  키워드 인자, 속성·첨자 대입, 비교, `_Stop(사유)`·`write_command(…, op)` 위치 인자)를 모아 값 식을 문자열 상수로 푼다.
  상수(대문자 이름)·str Enum 멤버(`X.Y`·`X.Y.value`)·조건식·or·튜플은 풀고, 그 밖의 식은 '동적 생산자'로 남긴다.
* `id_producers(src)` — 식별자 생산자(token_hex·token_urlsafe·token_bytes·uuid·randbelow·카운터 f-string id) 전수.

검증자 조사(`control-source-census.json`)와 같은 축을 저장소 테스트로 고정한다(새 생산자가 생기면 테스트가 바뀐다).
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

SRC = Path(__file__).resolve().parents[2] / "src"
WORD = re.compile(r"[a-z][a-z0-9_-]{0,63}")

#: 위치 인자로 제어 값이 실리는 호출: 함수 이름 → (인자 위치, 제어 키).
POSITIONAL = {"_Stop": (0, "reason"), "write_command": (2, "op"), "_send": (2, "op")}


def _files(src: Path) -> List[Path]:
    return sorted(p for p in src.rglob("*.py") if "__pycache__" not in p.parts)


def _trees(src: Path) -> Dict[str, ast.AST]:
    return {str(p.relative_to(src)): ast.parse(p.read_text(encoding="utf-8")) for p in _files(src)}


def _constants(trees: Dict[str, ast.AST]) -> Tuple[Dict[str, Set[str]], Dict[str, Dict[str, str]]]:
    """대문자 이름 상수(이름 → 값들)와 str Enum 클래스(클래스 → 멤버 → 값)."""
    consts: Dict[str, Set[str]] = defaultdict(set)
    enums: Dict[str, Dict[str, str]] = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                strs = _str_literals(node.value)
                for target in node.targets:
                    name = target.id if isinstance(target, ast.Name) else (
                        target.attr if isinstance(target, ast.Attribute) else None)
                    if name and name.lstrip("_").isupper() and strs:
                        consts[name].update(strs)
            if isinstance(node, ast.ClassDef) and any(
                    (isinstance(b, ast.Name) and b.id in ("Enum", "StrEnum")) or
                    (isinstance(b, ast.Attribute) and b.attr in ("Enum", "StrEnum")) for b in node.bases):
                members = {}
                for stmt in node.body:
                    if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant) \
                            and isinstance(stmt.value.value, str):
                        for target in stmt.targets:
                            if isinstance(target, ast.Name):
                                members[target.id] = stmt.value.value
                if members:
                    enums[node.name] = members
    return consts, enums


def _str_literals(value: ast.AST) -> Set[str]:
    """`"x"` 또는 `frozenset({"a", "b"})`·`{"a", "b"}`·`("a", "b")` 꼴 상수의 문자열들(아니면 빈 집합)."""
    if isinstance(value, ast.Constant):
        return {value.value} if isinstance(value.value, str) else set()
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in ("frozenset", "set") \
            and len(value.args) == 1:
        value = value.args[0]
    if isinstance(value, (ast.Set, ast.Tuple, ast.List)) and value.elts and all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts):
        return {e.value for e in value.elts}  # type: ignore[attr-defined]
    return set()


def _values(expr: ast.AST, consts, enums) -> Iterable[Tuple[str, str]]:
    """("lit", 값) 또는 ("dyn", 식) 들."""
    if isinstance(expr, ast.Constant):
        if isinstance(expr.value, str):
            yield "lit", expr.value
        return  # None·숫자(파일 권한 등) — 문자열 제어 값이 아니다
    if isinstance(expr, ast.IfExp):
        yield from _values(expr.body, consts, enums)
        yield from _values(expr.orelse, consts, enums)
        return
    if isinstance(expr, ast.BoolOp):
        for v in expr.values:
            yield from _values(v, consts, enums)
        return
    if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
        for v in expr.elts:
            yield from _values(v, consts, enums)
        return
    node = expr.value if isinstance(expr, ast.Attribute) and expr.attr == "value" else expr
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in enums \
            and node.attr in enums[node.value.id]:
        yield "lit", enums[node.value.id][node.attr]
        return
    name = node.id if isinstance(node, ast.Name) else (node.attr if isinstance(node, ast.Attribute) else None)
    if name and name.lstrip("_").isupper() and name in consts:
        for v in sorted(consts[name]):
            yield "lit", v
        return
    yield "dyn", ast.unparse(expr)


def _slot_name(node: ast.AST):
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        return node.slice.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" \
            and node.args and isinstance(node.args[0], ast.Constant):
        return node.args[0].value
    return None


def control_census(keys: Iterable[str], src: Path = SRC):
    """키 → {"words": {값: [위치…]}, "text": {값: [위치…]}, "dyn": {(파일, 식): [줄…]}}."""
    keys = set(keys)
    trees = _trees(src)
    consts, enums = _constants(trees)
    out = {k: {"words": defaultdict(list), "text": defaultdict(list), "dyn": defaultdict(list)} for k in keys}
    for rel, tree in trees.items():
        for node in ast.walk(tree):
            pairs = []
            if isinstance(node, ast.Dict):
                pairs = [(k.value, v) for k, v in zip(node.keys, node.values)
                         if isinstance(k, ast.Constant) and k.value in keys]
            elif isinstance(node, ast.Call):
                pairs = [(kw.arg, kw.value) for kw in node.keywords if kw.arg in keys]
                fname = node.func.id if isinstance(node.func, ast.Name) else (
                    node.func.attr if isinstance(node.func, ast.Attribute) else None)
                if fname in POSITIONAL:
                    pos, key = POSITIONAL[fname]
                    if key in keys and len(node.args) > pos:
                        pairs.append((key, node.args[pos]))
            elif isinstance(node, ast.Assign):
                pairs = [(_slot_name(t), node.value) for t in node.targets if _slot_name(t) in keys]
            elif isinstance(node, ast.Compare):
                name = _slot_name(node.left)
                if name in keys:
                    pairs = [(name, c) for c in node.comparators]
            for key, expr in pairs:
                for kind, val in _values(expr, consts, enums):
                    loc = f"{rel}:{node.lineno}"
                    if kind == "dyn":
                        out[key]["dyn"][(rel, val)].append(node.lineno)
                    elif WORD.fullmatch(val):
                        out[key]["words"][val].append(loc)
                    elif val:
                        out[key]["text"][val].append(loc)
    return out


#: 식별자 생산 호출(src/ 전체). 이름 → 설명.
ID_CALLS = {"token_hex", "token_urlsafe", "token_bytes", "randbelow", "uuid4", "uuid1", "getrandbits", "choice"}


def id_producers(src: Path = SRC) -> List[Tuple[str, int, str]]:
    """(파일, 줄, 식) — 난수 식별자 호출과 카운터 기반 f-string id(`f"tab-{self._n}"` 류) 전수."""
    found: List[Tuple[str, int, str]] = []
    for rel, tree in _trees(src).items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fname = node.func.attr if isinstance(node.func, ast.Attribute) else (
                    node.func.id if isinstance(node.func, ast.Name) else None)
                base = node.func.value if isinstance(node.func, ast.Attribute) else None
                base_name = base.id if isinstance(base, ast.Name) else ""
                if fname in ID_CALLS and (base_name.lstrip("_") in ("secrets", "uuid", "random") or fname in (
                        "token_hex", "token_urlsafe", "token_bytes", "uuid4")):
                    found.append((rel, node.lineno, ast.unparse(node)))
            elif isinstance(node, ast.JoinedStr):
                parts = node.values
                if parts and isinstance(parts[0], ast.Constant) and isinstance(parts[0].value, str) \
                        and re.fullmatch(r"[@a-z]{1,8}[-_]?", parts[0].value) and len(parts) == 2 \
                        and isinstance(parts[1], ast.FormattedValue):
                    inner = ast.unparse(parts[1].value)
                    if re.search(r"(_seq|_counter|_n|idx|count)\b", inner):
                        found.append((rel, node.lineno, ast.unparse(node)))
    return found
