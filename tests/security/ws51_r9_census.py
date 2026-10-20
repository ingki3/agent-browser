"""WS-51 R9 — 컨테이너 안 코드 census(R8-B1·B2, 소비자 표 독립).

R7 `literal_records` 는 **문자열 키 칸에 바로 실리는 리터럴**, R8 `attr_records` 는 **키 칸에 실리는 속성 접근 값**을 모았다.
둘 다 **목록(컨테이너) 안에 쌓는 문자열**은 보지 못했다:

* R8-B1 — `attempts[-1] = f"{strategy.value}(ambiguous)"` 처럼 복합 코드(f-string·괄호 결합)를 목록 원소로 덮어쓰는 생산자
  (`data.healing_attempts` 로 나간다). R8 은 `HealingStrategy` 기본값만 보았다.
* R8-B2 — `signals.append("text_changed")` 처럼 순수 신호 코드를 목록에 쌓는 생산자(`data.signals` 로 나간다). R8 은
  `post.signals` 칸 전체를 자유 문장으로 분류했다.

이 모듈은 src/ 전체에서 **목록에 문자열(상수·f-string)을 넣는 모든 자리**를 (컨테이너 이름, 값 틀, 파일, 줄)로 낸다:

* `X.append(E)`·`X.insert(i, E)`·`X.extend([E, …])`·`X[i] = E`(정수·음수 첨자 — 문자열 키 첨자는 R7 칸) — 컨테이너는 X 의
  이름(`attempts`·`signals`)·속성 이름(`post.signals` → `signals`).
* 목록 리터럴·목록 내포 `[E, …]`·`[E for …]` — 컨테이너는 그 목록이 놓인 자리: dict 문자열 키(`{"signals": [...]}`)·
  키워드 인자(`signals=[...]`)·이름 대입(`signals = [...]`)·`return` (`<return:함수>`). 그 밖의 자리는 `<expr>`.

E 는 문자열 상수 또는 f-string. 값 틀(template)은 상수면 그 값, f-string 이면 `{식}` 칸을 그대로 둔 틀
(`{strategy.value}(ambiguous)` · `top:{s}` · `dialog_opened:{d['type']}`) — 틀의 고정 부분이 코드인지 문장인지는 분류가 판정한다.
소비자 표(`public_url`)는 읽지 않는다.

조사 밖(한계): 문자열 결합 연산(`a + b`)·`"…".format(…)`·`%` 서식으로 만든 원소, 튜플·집합 리터럴, 함수 반환값을 거친 원소.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, NamedTuple, Optional, Set

from ws51_r5_census import SRC, _trees


class ItemRecord(NamedTuple):
    container: str
    template: str
    file: str
    line: int
    func: str  # 둘러싼 함수 이름('' = 모듈·클래스 수준)


def template_of(node: ast.AST) -> Optional[str]:
    """문자열 상수·f-string 의 값 틀(그 밖은 None)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                parts.append("{" + ast.unparse(v.value) + "}")
        return "".join(parts)
    return None


def _name(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _elements(node: ast.AST) -> List[ast.AST]:
    if isinstance(node, ast.List):
        return [e for e in node.elts if not isinstance(e, ast.Starred)]
    if isinstance(node, ast.ListComp):
        return [node.elt]
    return []


def item_records(src: Path = SRC) -> List[ItemRecord]:
    """목록에 문자열을 넣는 생산 자리 전부(중복 없음, 파일·줄 순)."""
    out: Set[ItemRecord] = set()
    str_groups: Set[tuple] = set()  # 문자열 원소가 하나라도 있는 (파일, 함수, 컨테이너)

    for rel, tree in _trees(src).items():
        parents = {}
        funcs = {}

        def walk(node: ast.AST, func: str) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func = node.name
            funcs[id(node)] = func
            for child in ast.iter_child_nodes(node):
                parents[id(child)] = node
                walk(child, func)

        walk(tree, "")

        def add(container: str, elem: ast.AST, at: ast.AST) -> None:
            t = template_of(elem)
            out.add(ItemRecord(container, t if t is not None else "{" + ast.unparse(elem) + "}", rel,
                               getattr(elem, "lineno", getattr(at, "lineno", 0)), funcs.get(id(at), "")))
            if t is not None:
                str_groups.add((rel, funcs.get(id(at), ""), container))

        def place(lst: ast.AST) -> str:
            """목록 리터럴·내포가 놓인 자리의 컨테이너 이름."""
            p = parents.get(id(lst))
            if isinstance(p, ast.BinOp):  # list(post.signals) + [..] 같은 이어붙이기 — 바깥 자리
                return place(p)
            if isinstance(p, ast.Dict):
                for k, v in zip(p.keys, p.values):
                    if v is lst and isinstance(k, ast.Constant) and isinstance(k.value, str):
                        return k.value
            if isinstance(p, ast.keyword) and p.arg:
                return p.arg
            if isinstance(p, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = p.targets if isinstance(p, ast.Assign) else [p.target]
                for t in targets:
                    if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) \
                            and isinstance(t.slice.value, str):
                        return t.slice.value
                    n = _name(t)
                    if n:
                        return n
            if isinstance(p, ast.Return):
                return "<return:" + funcs.get(id(p), "") + ">"
            if isinstance(p, ast.Call) and isinstance(p.func, ast.Attribute) and p.func.attr == "extend":
                return _name(p.func.value) or "<expr>"
            return "<expr>"

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in ("append", "insert") and node.args:
                container = _name(node.func.value)
                if container:
                    add(container, node.args[-1], node)
            elif isinstance(node, (ast.List, ast.ListComp)):
                elems = _elements(node)
                if elems:
                    container = place(node)
                    for e in elems:
                        add(container, e, node)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Subscript) and not (
                            isinstance(t.slice, ast.Constant) and isinstance(t.slice.value, str)):
                        container = _name(t.value)
                        if container:
                            add(container, node.value, node)
    kept = [r for r in out if (r.file, r.func, r.container) in str_groups]
    return sorted(kept, key=lambda r: (r.file, r.line, r.container, r.template))
