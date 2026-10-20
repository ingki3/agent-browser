"""WS-51 R6 — 도구 식별자 생산자 census 일반화(R5-F3).

R5 의 `id_producers` 는 f-string 첫 조각이 literal 이고 안쪽 식이 특정 카운터 이름(`_seq`·`idx` …)일 때만 잡아서
상수 이름 접두사(`f'{SOM_ID_PREFIX}{used + 1}'`)·동적 접두사(`f'{prefix}{counters[prefix]}'`)·임의 산술을 놓쳤다.

`id_candidates(src)` — src/ 전체에서 **문자열을 조립하는 모든 식**(f-string, `+` 연결 사슬, `'…' % x`, `'…'.format(…)`)을
보고, 조각을 literal(문자열 상수 + **대문자 이름 상수로 풀리는 이름·속성**)과 동적 조각으로 나눈 뒤

* 동적 조각이 하나 이상이고(안쪽 식은 무엇이든 — 이름·첨자·산술·호출),
* literal 조각을 이은 글자가 식별자 글자(`[A-Za-z0-9@_#-]`)만으로 12자 이하

인 것을 '식별자 모양 조립' 후보로 전부 낸다. 주소·경로·파일 이름·선택자·문장(공백·`/`·`.`·`:`·`?`·`=` 등이 든 literal)은
식별자가 아니라 후보에서 빠진다(그 수는 `assembly_counts` 로 따로 보고). f-string 의 형식 지정(format_spec)은 조립이
아니라 건너뛴다. 난수 호출 생산자는 R5 `id_producers` 그대로(이 모듈은 조립 쪽만 일반화).
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from ws51_r5_census import SRC, _trees

#: 식별자 모양 literal(이은 글자) — 이 밖의 글자가 들면 주소·경로·문장 조립.
ID_LITERAL = re.compile(r"[A-Za-z0-9@_#-]*")
ID_LITERAL_MAX = 12
_PCT = re.compile(r"%(?:\([^)]*\))?[-#0 +]*(?:\*|\d+)?(?:\.(?:\*|\d+))?[hlL]?[diouxXeEfFgGcrsa]")
_BRACE = re.compile(r"\{[^{}]*\}")


def _string_constants(trees: Dict[str, ast.AST]) -> Dict[str, Set[str]]:
    """대문자 이름(앞 `_` 허용) → 그 이름에 대입된 문자열 상수들(모듈·클래스 어디서든)."""
    out: Dict[str, Set[str]] = defaultdict(set)
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    name = t.id if isinstance(t, ast.Name) else (t.attr if isinstance(t, ast.Attribute) else None)
                    if name and name.lstrip("_").isupper():
                        out[name].add(node.value.value)
    return out


def _const_of(expr: ast.AST, consts: Dict[str, Set[str]]) -> Optional[str]:
    name = expr.id if isinstance(expr, ast.Name) else (expr.attr if isinstance(expr, ast.Attribute) else None)
    if name and len(consts.get(name, ())) == 1:
        return next(iter(consts[name]))
    return None


Piece = Tuple[str, object]  # ("lit", str) | ("dyn", ast.AST)


def _joined(node: ast.JoinedStr, consts) -> List[Piece]:
    out: List[Piece] = []
    for v in node.values:
        if isinstance(v, ast.Constant):
            out.append(("lit", v.value))
            continue
        c = _const_of(v.value, consts) if v.format_spec is None and v.conversion == -1 else None
        out.append(("lit", c) if c is not None else ("dyn", v.value))
    return out


def _add_chain(node: ast.AST, consts) -> List[Piece]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _add_chain(node.left, consts) + _add_chain(node.right, consts)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [("lit", node.value)]
    if isinstance(node, ast.JoinedStr):
        return _joined(node, consts)
    c = _const_of(node, consts)
    return [("lit", c)] if c is not None else [("dyn", node)]


def _template(text: str, pattern: "re.Pattern[str]", call: ast.AST) -> List[Piece]:
    parts = pattern.split(text)
    out: List[Piece] = []
    for i, p in enumerate(parts):
        out.append(("lit", p))
        if i < len(parts) - 1:
            out.append(("dyn", call))
    return out


def _assemblies(src: Path = SRC):
    """(파일, 줄, 식, 조각들) — 문자열을 조립하는 모든 식(바깥 식만, 형식 지정 제외)."""
    trees = _trees(src)
    consts = _string_constants(trees)
    for rel, tree in trees.items():
        nested: Set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                nested.update(id(s) for s in (node.left, node.right) if isinstance(s, (ast.BinOp, ast.JoinedStr)))
            if isinstance(node, ast.FormattedValue) and node.format_spec is not None:
                nested.add(id(node.format_spec))
        for node in ast.walk(tree):
            if id(node) in nested:
                continue
            pieces: Optional[List[Piece]] = None
            if isinstance(node, ast.JoinedStr):
                pieces = _joined(node, consts)
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                pieces = _add_chain(node, consts)
                if not any(k == "lit" for k, _ in pieces):
                    pieces = None  # 숫자 덧셈·리스트 연결 등 — 문자열 literal 이 없다
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) \
                    and isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
                pieces = _template(node.left.value, _PCT, node.right)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format" \
                    and isinstance(node.func.value, ast.Constant) and isinstance(node.func.value.value, str):
                pieces = _template(node.func.value.value, _BRACE, node)
            if pieces is not None:
                yield rel, node.lineno, ast.unparse(node), pieces


def id_candidates(src: Path = SRC) -> List[Tuple[str, int, str, str]]:
    """(파일, 줄, 식, 이은 literal) — 식별자 모양 조립 후보 전수(동적 조각 ≥1, literal 이 식별자 글자 ≤12자)."""
    found = []
    for rel, line, expr, pieces in _assemblies(src):
        lits = "".join(v for k, v in pieces if k == "lit")  # type: ignore[misc]
        if any(k == "dyn" for k, _ in pieces) and ID_LITERAL.fullmatch(lits) and len(lits) <= ID_LITERAL_MAX:
            found.append((rel, line, expr, lits))
    return found


def assembly_counts(src: Path = SRC) -> Dict[str, int]:
    """조사 규모(보고용): 전체 조립 식 수, 식별자 모양 후보 수."""
    total = sum(1 for _ in _assemblies(src))
    return {"assemblies": total, "id_candidates": len(id_candidates(src))}
