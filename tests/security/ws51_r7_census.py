"""WS-51 R7 — 리터럴 제어값 census(R6-B1, 소비자 표 독립).

R5 `control_census` 는 소비자 `public_url.CONTROL_VOCABULARY` 의 **키 목록을 입력**으로 받아, 표에 없는 키
(`nav_frame`·`resolved_from`)는 애초에 조사하지 않았다. 이 모듈은 소비자 표를 읽지 않는다 — src/ 전체 AST 에서
**문자열 키 칸에 리터럴 문자열 값이 실리는 모든 자리**를 모아 키 → 값 집합 → 생산 위치로 낸다.

칸(키가 문자열 상수인 것만):

* dict 리터럴 `{"k": v}` (`x.update({...})`·`return {...}` 포함)
* 첨자 대입 `x["k"] = v`
* `dict(k=v)`·`x.update(k=v)` 키워드, `x.setdefault("k", v)`

값 식은 리터럴로 풀리는 갈래를 모두 낸다(동적 갈래는 값이 아니라 뺀다 — 동적 생산자 분류는 R5 REVIEWED):

* 문자열 상수, 조건식(`a if c else b`)·`or`/`and` 의 각 갈래
* 대문자 이름 상수(모듈·클래스 어디서든 문자열 상수가 대입된 이름), str Enum 멤버(`X.Y`·`X.Y.value`)
* 지역 이름 — 그 함수 안의 모든 대입을 따라간다(`resolved_from = "current_frame"` … `data["resolved_from"] = resolved_from`)
* `self.속성` — 그 클래스 안의 `self.속성 = …` 대입, 클래스 본문 필드 기본값(`x: str = "…"`), 그리고(``__init__`` 이
  없는 데이터클래스·모델이면) 그 클래스 생성 호출 `C(속성=…)`·위치 인자(필드 순서)의 인자 식(호출한 쪽 범위로 해석)
* `__init__` 의 매개변수 — 그 클래스 생성 호출의 같은 이름·위치 인자
* 그 밖의 함수 매개변수 — src 안에 그 이름의 함수가 하나뿐이면 그 이름 호출의 같은 이름·위치 인자(동명 함수가
  여럿이면 잇지 않는다 — 다른 함수의 인자를 섞지 않기 위해; 그 매개변수는 동적 생산자로 남는다)

조사 밖(한계 — 보고서에 기록): 다른 객체의 속성(`found.reason`), 튜플·목록 원소를 거쳐 풀어 쓰는 값(`for src, text in
signals`), JS 문자열 안 값, 함수 반환값. 이런 동적 생산자는 R5 `control_census` 의 REVIEWED(제어 키별 동적 생산자 분류)
가 맡는다.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Set, Tuple

from ws51_r5_census import SRC, _trees

Exprs = List[Tuple[ast.AST, "Scope"]]


def _str_consts(trees: Dict[str, ast.AST]) -> Tuple[Dict[str, Set[str]], Dict[str, Dict[str, str]]]:
    """대문자 이름 상수(이름 → 문자열 값들)와 str Enum 클래스(클래스 → 멤버 → 값)."""
    consts: Dict[str, Set[str]] = defaultdict(set)
    enums: Dict[str, Dict[str, str]] = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    name = t.id if isinstance(t, ast.Name) else (t.attr if isinstance(t, ast.Attribute) else None)
                    if name and name.lstrip("_").isupper():
                        consts[name].add(node.value.value)
            if isinstance(node, ast.ClassDef) and any(
                    (isinstance(b, ast.Name) and b.id in ("Enum", "StrEnum", "str")) or
                    (isinstance(b, ast.Attribute) and b.attr in ("Enum", "StrEnum")) for b in node.bases):
                members = {}
                for stmt in node.body:
                    if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant) \
                            and isinstance(stmt.value.value, str):
                        for t in stmt.targets:
                            if isinstance(t, ast.Name):
                                members[t.id] = stmt.value.value
                if members:
                    enums[node.name] = members
    return consts, enums


def _assigns(root: ast.AST, attr: bool) -> Dict[str, List[ast.AST]]:
    """root 안의 이름 대입(attr=False) 또는 `self.속성` 대입(attr=True): 이름 → 값 식들."""
    out: Dict[str, List[ast.AST]] = defaultdict(list)
    for node in ast.walk(root):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if attr and isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                    out[t.attr].append(node.value)
                elif not attr and isinstance(t, ast.Name):
                    out[t.id].append(node.value)
    return out


class _Class:
    def __init__(self, node: ast.ClassDef):
        self.fields: List[str] = []  # 데이터클래스·NamedTuple·모델 필드 순서
        self.defaults: Dict[str, List[ast.AST]] = defaultdict(list)
        self.init_params: List[str] = []
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                self.fields.append(stmt.target.id)
                if stmt.value is not None:
                    self.defaults[stmt.target.id].append(stmt.value)
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == "__init__":
                a = stmt.args
                self.init_params = [p.arg for p in a.posonlyargs + a.args][1:] + [p.arg for p in a.kwonlyargs]
        self.self_assigns = _assigns(node, attr=True)
        self.ctor: Dict[str, Exprs] = defaultdict(list)  # 생성 호출 인자: 이름 → [(식, 호출한 쪽 범위)]

    def positional(self) -> List[str]:
        return self.init_params or self.fields


class Scope:
    def __init__(self, local: Dict[str, List[ast.AST]], params: Set[str], cls: Optional[_Class], in_init: bool,
                 func: str = ""):
        self.local, self.params, self.cls, self.in_init, self.func = local, params, cls, in_init, func


class _Census:
    def __init__(self, src: Path):
        self.trees = _trees(src)
        self.consts, self.enums = _str_consts(self.trees)
        self.classes: Dict[str, _Class] = {}
        #: 함수 이름 → 매개변수 순서들(같은 이름 여럿이면 전부 — 과잉 근사), 함수 호출 인자: (이름, 매개변수) → 식들
        self.funcs: Dict[str, List[List[str]]] = defaultdict(list)
        self.func_args: Dict[Tuple[str, str], Exprs] = defaultdict(list)
        for tree in self.trees.values():
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    self.classes.setdefault(node.name, _Class(node))
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name != "__init__":
                    a = node.args
                    names = [p.arg for p in a.posonlyargs + a.args]
                    if names and names[0] in ("self", "cls"):
                        names = names[1:]
                    self.funcs[node.name].append(names + [p.arg for p in a.kwonlyargs])

    def walk(self, on_node: Callable[[ast.AST, str, Scope, Optional[str]], None]) -> None:
        """범위를 따라 걷는다. table = 모듈·클래스 수준 이름 대입의 값 안이면 그 이름(조회표 — 응답 칸이 아닐 수 있음)."""
        def visit(node: ast.AST, rel: str, scope: Scope, in_func: bool, table: Optional[str]) -> None:
            if isinstance(node, ast.ClassDef):
                scope = Scope({}, set(), self.classes.get(node.name), False)
                in_func, table = False, None
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                a = node.args
                params = {p.arg for p in a.posonlyargs + a.args + a.kwonlyargs}
                params |= {p.arg for p in (a.vararg, a.kwarg) if p is not None}
                fname = getattr(node, "name", "")
                scope = Scope(_assigns(node, attr=False), params, scope.cls, fname == "__init__", fname)
                in_func, table = True, None
            elif not in_func and isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [t.id for t in targets if isinstance(t, ast.Name)]
                table = names[0] if names else table
            on_node(node, rel, scope, table)
            for child in ast.iter_child_nodes(node):
                visit(child, rel, scope, in_func, table)

        for rel, tree in self.trees.items():
            visit(tree, rel, Scope(_assigns(tree, attr=False), set(), None, False), False, None)

    def collect_ctor_args(self) -> None:
        def on_node(node: ast.AST, rel: str, scope: Scope, table: Optional[str]) -> None:
            if not isinstance(node, ast.Call):
                return
            fname = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else None)
            defs = self.funcs.get(fname or "", ())
            for names in (defs if len(defs) == 1 else ()):  # 이름이 하나뿐인 함수만(동명 함수 혼동 방지)
                for i, arg in enumerate(node.args):
                    if i < len(names) and not isinstance(arg, ast.Starred):
                        self.func_args[(fname, names[i])].append((arg, scope))
                for kw in node.keywords:
                    if kw.arg:
                        self.func_args[(fname, kw.arg)].append((kw.value, scope))
            cls = self.classes.get(fname or "")
            if cls is None:
                return
            names = cls.positional()
            for i, arg in enumerate(node.args):
                if i < len(names) and not isinstance(arg, ast.Starred):
                    cls.ctor[names[i]].append((arg, scope))
            for kw in node.keywords:
                if kw.arg:
                    cls.ctor[kw.arg].append((kw.value, scope))

        self.walk(on_node)

    def resolve(self, expr: ast.AST, scope: Scope, depth: int = 0) -> List[str]:
        """식의 리터럴 갈래 값들(동적 갈래는 빠진다)."""
        if depth > 8:
            return []
        if isinstance(expr, ast.Constant):
            return [expr.value] if isinstance(expr.value, str) else []
        if isinstance(expr, ast.IfExp):
            return self.resolve(expr.body, scope, depth + 1) + self.resolve(expr.orelse, scope, depth + 1)
        if isinstance(expr, ast.BoolOp):
            return [v for e in expr.values for v in self.resolve(e, scope, depth + 1)]
        node = expr.value if isinstance(expr, ast.Attribute) and expr.attr == "value" else expr
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in self.enums \
                and node.attr in self.enums[node.value.id]:
            return [self.enums[node.value.id][node.attr]]
        name = node.id if isinstance(node, ast.Name) else (node.attr if isinstance(node, ast.Attribute) else None)
        if name and name.lstrip("_").isupper() and name in self.consts:
            return sorted(self.consts[name])
        pool: Exprs = []
        if isinstance(node, ast.Name):
            pool += [(e, scope) for e in scope.local.get(node.id, ())]
            if node.id in scope.params and scope.in_init and scope.cls is not None:
                pool += scope.cls.ctor.get(node.id, [])
            elif node.id in scope.params and scope.func:
                pool += self.func_args.get((scope.func, node.id), [])
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self" \
                and scope.cls is not None:
            cls = scope.cls
            init_scope = Scope({}, set(cls.init_params), cls, True)
            pool += [(e, init_scope) for e in cls.self_assigns.get(node.attr, ())]
            pool += [(e, Scope({}, set(), cls, False)) for e in cls.defaults.get(node.attr, ())]
            if not cls.init_params:
                pool += cls.ctor.get(node.attr, [])
        return [v for e, s in pool for v in self.resolve(e, s, depth + 1)]


def _slots(node: ast.AST) -> List[Tuple[str, ast.AST]]:
    if isinstance(node, ast.Dict):
        return [(k.value, v) for k, v in zip(node.keys, node.values)
                if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    if isinstance(node, ast.Assign):
        return [(t.slice.value, node.value) for t in node.targets
                if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                and isinstance(t.slice.value, str)]
    if isinstance(node, ast.Call):
        fname = node.func.id if isinstance(node.func, ast.Name) else (
            node.func.attr if isinstance(node.func, ast.Attribute) else None)
        out = []
        if fname in ("dict", "update"):
            out += [(kw.arg, kw.value) for kw in node.keywords if kw.arg]
        if fname == "setdefault" and len(node.args) == 2 and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            out.append((node.args[0].value, node.args[1]))
        return out
    return []


class Record(NamedTuple):
    key: str
    value: str
    file: str  # src 상대 경로
    line: int
    table: Optional[str]  # 모듈·클래스 수준 이름 대입(조회표) 안이면 그 이름


def literal_records(src: Path = SRC) -> List[Record]:
    """리터럴 생산 자리 전부(중복 없음, 파일·줄 순)."""
    census = _Census(src)
    census.collect_ctor_args()
    out: Set[Record] = set()

    def on_node(node: ast.AST, rel: str, scope: Scope, table: Optional[str]) -> None:
        for key, expr in _slots(node):
            for val in census.resolve(expr, scope):
                out.add(Record(key, val, rel, node.lineno, table))  # type: ignore[attr-defined]

    census.walk(on_node)
    return sorted(out, key=lambda r: (r.file, r.line, r.key, r.value))


def literal_census(src: Path = SRC) -> Dict[str, Dict[str, List[str]]]:
    """키 → {값: [파일:줄 …]} (보고서 표용 집계)."""
    out: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
    for r in literal_records(src):
        out[r.key][r.value].append(f"{r.file}:{r.line}")
    return {k: dict(v) for k, v in out.items()}
