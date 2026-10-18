"""WS-51 R8 — 속성 전달값 census(R7-B1, 소비자 표 독립).

R7 `literal_records` 는 **리터럴로 풀리는 값**만 모았다. `dialog.type`(Playwright Dialog)처럼 외부 객체의 속성·열거형을
응답 칸에 옮겨 담는 생산자는 리터럴이 아니라 빠졌다(R7-B1 — `data.dialogs[].type='beforeunload'` 가 등록 뒤 가려짐).
이 모듈은 src/ 전체에서 **문자열 키 칸(R7 `_slots` 와 같은 칸)에 다른 객체의 속성 접근 값이 실리는 자리**를 모아
(키, 속성 식, 생산 위치)로 낸다. 소비자 표(`public_url`)는 읽지 않는다.

값 식에서 속성 접근 잎(leaf)을 찾는 규칙:

* `x.attr`(x 가 `self` 가 아닌 것 — 이름·호출·첨자 어느 것이든 뿌리) → 잎 `x.attr`. `x.attr.value`(열거형 값)도 잎.
* 인자 없는 메서드 호출 `x.meth()`(예: `page.title()`) → 잎 `x.meth()`. `getattr(x, "a", …)` → 잎 `x.a`.
* 감싸기는 벗긴다: `str(…)`·`repr(…)`·`…[:n]`·`….lower()`·`….strip()` 같은 문자열 메서드, `await …`, 조건식·`or`/`and`
  갈래, f-string 의 `{…}` 칸.
* 지역 이름은 그 함수 안의 모든 대입을 따라간다(`kind = str(dialog.type)` … `{"type": kind}`). `self.속성`·매개변수는 R7 과
  같은 규칙(그 클래스의 `self.속성 = …`·생성 호출 인자, 이름이 하나뿐인 함수의 호출 인자)으로 따라간다.
* 대문자 이름 상수·str Enum 멤버(`X.Y`)는 리터럴이라 R7 몫 — 잎으로 세지 않는다. 모듈 이름 뿌리(`os.sep` 등)도
  잎이다(분류에서 판정).

조사 밖(한계): 첨자 조회 `info["type"]`(dict 값 — 생산 dict 의 리터럴·속성 칸이 이미 따로 잡힌다), 함수 반환값
`f(x)`(인자 있는 호출), 튜플 풀기·반복 변수, JS 문자열 안 값. 같은 이름 함수가 여럿이면 매개변수를 잇지 않는다(R7 과 같음).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, NamedTuple, Optional, Set

from ws51_r5_census import SRC
from ws51_r7_census import Scope, _Census, _slots

#: 값을 그대로(문자열로) 넘기는 감싸기 호출·메서드.
_WRAP_FUNCS = {"str", "repr"}
_WRAP_METHODS = {"lower", "upper", "strip", "lstrip", "rstrip", "casefold", "title", "decode"}


class AttrRecord(NamedTuple):
    key: str
    attr: str  # 마지막 속성 이름(호출이면 `meth()`)
    expr: str  # 잎 식(ast.unparse)
    file: str
    line: int
    table: Optional[str]



class _AttrCensus(_Census):
    def leaves(self, expr: ast.AST, scope: Scope, depth: int = 0) -> List[ast.AST]:
        """값 식의 속성 접근 잎들."""
        if depth > 8 or expr is None:
            return []
        if isinstance(expr, ast.IfExp):
            return self.leaves(expr.body, scope, depth + 1) + self.leaves(expr.orelse, scope, depth + 1)
        if isinstance(expr, ast.BoolOp):
            return [x for e in expr.values for x in self.leaves(e, scope, depth + 1)]
        if isinstance(expr, ast.JoinedStr):
            return [x for v in expr.values if isinstance(v, ast.FormattedValue)
                    for x in self.leaves(v.value, scope, depth + 1)]
        if isinstance(expr, ast.Await):
            return self.leaves(expr.value, scope, depth + 1)
        if isinstance(expr, ast.Subscript) and isinstance(expr.slice, ast.Slice):
            return self.leaves(expr.value, scope, depth + 1)
        if isinstance(expr, ast.Call):
            f = expr.func
            if isinstance(f, ast.Name) and f.id in _WRAP_FUNCS and len(expr.args) == 1:
                return self.leaves(expr.args[0], scope, depth + 1)
            if isinstance(f, ast.Name) and f.id == "getattr" and len(expr.args) >= 2 \
                    and isinstance(expr.args[1], ast.Constant) and isinstance(expr.args[1].value, str):
                obj = expr.args[0]
                if isinstance(obj, ast.Name) and obj.id == "self":
                    return []
                return [ast.Attribute(value=obj, attr=expr.args[1].value, ctx=ast.Load())]
            if isinstance(f, ast.Attribute) and f.attr in _WRAP_METHODS:
                return self.leaves(f.value, scope, depth + 1)
            if isinstance(f, ast.Attribute) and not expr.args and not expr.keywords \
                    and not (isinstance(f.value, ast.Name) and f.value.id == "self") and not self._literal(f.value):
                return [expr]
            return []
        if isinstance(expr, ast.Attribute):
            if self._literal(expr):
                return []
            if isinstance(expr.value, ast.Name) and expr.value.id == "self":
                return self._self_attr(expr.attr, scope, depth)
            return [expr]  # self.x.attr 도 잎 — 다른 객체(self.x)의 속성
        if isinstance(expr, ast.Name):
            pool = [(e, scope) for e in scope.local.get(expr.id, ())]
            if expr.id in scope.params and scope.in_init and scope.cls is not None:
                pool += scope.cls.ctor.get(expr.id, [])
            elif expr.id in scope.params and scope.func:
                pool += self.func_args.get((scope.func, expr.id), [])
            return [x for e, s in pool for x in self.leaves(e, s, depth + 1)]
        return []

    def _self_attr(self, attr: str, scope: Scope, depth: int) -> List[ast.AST]:
        cls = scope.cls
        if cls is None:
            return []
        init_scope = Scope({}, set(cls.init_params), cls, True)
        pool = [(e, init_scope) for e in cls.self_assigns.get(attr, ())]
        pool += [(e, Scope({}, set(), cls, False)) for e in cls.defaults.get(attr, ())]
        if not cls.init_params:
            pool += cls.ctor.get(attr, [])
        return [x for e, s in pool for x in self.leaves(e, s, depth + 1)]

    def _literal(self, node: ast.AST) -> bool:
        """대문자 상수·str Enum 멤버(`X.Y`·`X.Y.value`) — 리터럴(R7 몫)."""
        n = node.value if isinstance(node, ast.Attribute) and node.attr == "value" else node
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in self.enums:
            return True
        name = n.id if isinstance(n, ast.Name) else (n.attr if isinstance(n, ast.Attribute) else None)
        return bool(name) and name.lstrip("_").isupper() and name in self.consts


def _attr_name(leaf: ast.AST) -> str:
    if isinstance(leaf, ast.Call):
        return leaf.func.attr + "()"  # type: ignore[attr-defined]
    return leaf.attr  # type: ignore[attr-defined]


def attr_records(src: Path = SRC) -> List[AttrRecord]:
    """속성 전달 생산 자리 전부(중복 없음, 파일·줄 순)."""
    census = _AttrCensus(src)
    census.collect_ctor_args()
    out: Set[AttrRecord] = set()

    def on_node(node: ast.AST, rel: str, scope: Scope, table: Optional[str]) -> None:
        for key, expr in _slots(node):
            for leaf in census.leaves(expr, scope):
                out.add(AttrRecord(key, _attr_name(leaf), ast.unparse(leaf), rel, node.lineno,  # type: ignore[attr-defined]
                                   table))

    census.walk(on_node)
    return sorted(out, key=lambda r: (r.file, r.line, r.key, r.expr))
