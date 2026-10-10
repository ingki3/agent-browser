"""레시피 서비스 — MCP 서버와 레시피 모듈을 잇는 얇은 층 (WS-38 단계 3~5).

* 기록: 서버 `_call_tool` 이 액션 직전 `pre()`(기록 시점 스냅숏, 한 번의 evaluate) · 직후 `post()` 를
  부른다. 성공 + 사후 확인 통과만 궤적에 넣고, 실패·치유·승인 재생·사람 조작·미지원 동작에서 끊는다.
* 관찰: `observe()` 가 observe_page 결과에 `data.recipes` 후보를 단다 — 레시피 없는 출처는 dict 조회
  하나로 끝(골격 계산 없음), 후보가 있을 때만 골격 1회.
* 서버 도구 `browser_recipe {op: save|run|list|delete}` 처리.
* 저장 권유 `data.recipe_hint` — 같은 출처 연속 통과 ≥3 + 그 페이지 레시피 없음, 세션·출처당 1회.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from recipes import keys
from recipes.recorder import HINT_STREAK, NEUTRAL, Trajectory, make_entry
from recipes.replay import run_recipe
from recipes.store import ELEMENT_ACTIONS, MAX_STEPS, RECORDABLE, RecipeError, RecipeStore, \
    clean_text, compile_recipe

logger = logging.getLogger(__name__)

#: observe data.recipes 의 안내 한 줄.
HOW = "browser_recipe run 으로 한 번에 실행 — 멈추면 그 지점부터 직접 진행"
#: 관찰 후보 상한.
MAX_CANDIDATES = 5
#: 레시피 파일 이름(serve --profile 폴더 안).
FILE_NAME = "recipes.json"


def hint_text(last_n: int) -> str:
    return (f"이 흐름을 다시 할 일이면 browser_recipe save last_n={last_n} 로 저장하세요"
            "(입력 글자는 params 로)")


class ServerHost:
    """재생(replay.ReplayHost)이 쓰는 서버 기능 — 기존 call_tool 경로를 그대로 쓴다."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self._seq = 0

    def page(self) -> Any:
        return self.server._recipe_page()

    def epoch(self) -> int:
        return int(self.server._engine.epoch) if self.server._engine is not None else 0

    def register(self, found: Dict[str, Any]) -> str:
        from perception.engine import ElementHandle

        self._seq += 1
        element_id = f"@r{self._seq}"
        self.server._engine.register_external_handle(element_id, ElementHandle(
            element_id=element_id, epoch=self.epoch(), role=str(found.get("role") or ""),
            name=str(found.get("name") or ""), css_path=str(found["css"]), is_shadow=False,
            href=found.get("href")))
        return element_id

    async def dispatch(self, action: str, args: Dict[str, Any]) -> Any:
        from contracts import ActionType
        from interface.mcp_server import tool_name

        disp = self.server._dispatcher
        if disp is not None:
            disp.heal_disabled = True  # 재생은 찾은 그 요소만 — 비슷한 요소로 바꿔 누르지 않는다
        try:
            return await self.server.call_tool(tool_name(ActionType(action)), args)
        finally:
            if disp is not None:
                disp.heal_disabled = False

    async def observe(self) -> Dict[str, Any]:
        from contracts import ActionType
        from interface.mcp_server import envelope_dict, tool_name

        try:
            res = await self.server.call_tool(tool_name(ActionType.OBSERVE_PAGE), {})
        except Exception as exc:  # noqa: BLE001 - 관찰 실패로 중단 응답을 망치지 않는다
            return {"observation_error": clean_text(f"{type(exc).__name__}: {exc}")}
        env = envelope_dict(res)
        data = env.get("data") or {}
        out: Dict[str, Any] = {}
        if "observation" in data:
            out["observation"] = data["observation"]
        for k in ("injection_suspected", "truncated", "challenge"):
            if data.get(k) is not None:
                out[k] = data[k]
        if not res.success:
            out["observation_error"] = clean_text(res.error_message or "")
        return out


class RecipeService:
    def __init__(self, server: Any) -> None:
        self.server = server
        self.trajectory = Trajectory()
        self.running = False
        self._store: Optional[RecipeStore] = None
        self.host = ServerHost(server)

    # -- 저장소 -------------------------------------------------------------

    @property
    def store(self) -> RecipeStore:
        if self._store is None:
            path: Optional[Path] = None
            if self.server.profile is not None:
                folder = self.server.acquire_profile()
                if folder is not None:
                    path = Path(folder) / FILE_NAME
            self._store = RecipeStore(path)
            for w in self._store.warnings:
                logger.warning("레시피: %s", w)
        return self._store

    def close(self) -> None:
        if self._store is not None:
            self._store.close()

    # -- 기록 ---------------------------------------------------------------

    def reset(self, why: str) -> None:
        self.trajectory.reset(why)

    async def pre(self, action: Any, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """액션 직전 기록 스냅숏. 기록하지 않는 동작이면 None(필요하면 궤적을 끊는다)."""
        if self.running:
            return None
        a = action.value
        if a in NEUTRAL:
            return None
        if a not in RECORDABLE:
            self.reset(f"unsupported:{a}")
            return None
        page = self.server._recipe_page()
        if page is None:
            self.reset("no_page_or_frame")
            return None
        css: Optional[str] = None
        if a in ELEMENT_ACTIONS:
            eid = params.get("element_id")
            engine = self.server._engine
            handle = engine.get_handle(eid) if (eid and engine is not None) else None
            css = str(getattr(handle, "css_path", "") or "") if handle is not None else ""
            if handle is None or getattr(handle, "is_shadow", False) or not css:
                self.reset("unsupported_target")
                return None
        try:
            snap = await keys.snapshot(page, css)
        except Exception:  # noqa: BLE001 - 기록 실패가 액션을 막지 않는다
            logger.debug("레시피 기록 스냅숏 실패", exc_info=True)
            self.reset("snapshot_failed")
            return None
        if css and not snap.get("target"):
            self.reset("target_unreadable")
            return None
        return snap

    def post(self, action: Any, params: Dict[str, Any], pre: Optional[Dict[str, Any]], result: Any,
             used_approval: bool) -> None:
        if self.running or action.value in NEUTRAL or pre is None:
            return
        if used_approval:
            self.reset("approval_replay")
            return
        if not result.success or result.healed:
            self.reset("failed" if not result.success else "healed")
            return
        data = result.data or {}
        a = action.value
        if a in ELEMENT_ACTIONS and not data.get("signals"):
            self.reset("no_postcondition")
            return
        secret = data.get("secret_resolved") is not None
        entry = make_entry(a, params, pre, str(result.current_url or ""), data, secret)
        if entry["expect"]["nav"] == "error":
            self.reset("nav_error")  # R1 NB-3: 오류 페이지(차단·실패)로 간 단계는 기록하지 않는다
            return
        self.trajectory.add(entry)
        self._maybe_hint(entry, result)

    def _maybe_hint(self, entry: Dict[str, Any], result: Any) -> None:
        streak = min(self.trajectory.origin_streak(), MAX_STEPS)
        if streak < HINT_STREAK:
            return
        if entry["action"] == "navigate":
            origin = keys.origin_of(str(entry["args"].get("url") or ""))
        else:
            origin = entry["origin"]
        if not origin or origin in self.trajectory.hinted:
            return
        first = self.trajectory.last(streak)[0]
        pat = (first["expect"].get("url_pat") if first["action"] == "navigate" else first["url_pat"]) or ""
        if self.store.candidates(origin, pat):
            return
        self.trajectory.hinted.add(origin)
        result.data["recipe_hint"] = {"text": hint_text(streak), "last_n": streak}

    # -- 관찰 후보 -----------------------------------------------------------

    async def observe(self, result: Any) -> None:
        if self.running or not result.success:
            return
        page = self.server._recipe_page()
        if page is None:
            return
        url = str(getattr(page, "url", "") or "")
        origin = keys.origin_of(url)
        store = self.store
        if not origin or not store.has_origin(origin):
            return  # 레시피 없는 출처: 추가 비용 0
        cands = store.candidates(origin, keys.url_pattern(url))
        if not cands:
            return
        try:
            skel = (await keys.snapshot(page, None))["skel"]
        except Exception:  # noqa: BLE001
            return
        out: List[Dict[str, Any]] = []
        for rec in cands:
            first = rec["steps"][0]
            if first["action"] != "navigate" and not any(v.get("skel") == skel for v in first["variants"]):
                continue
            out.append({"id": rec["id"], "name": rec["name"], "steps": len(rec["steps"]),
                        "params": list(rec.get("params") or [])})
            if len(out) >= MAX_CANDIDATES:
                break
        if out:
            result.data["recipes"] = {"how": HOW, "candidates": out}

    # -- 서버 도구 -----------------------------------------------------------

    @staticmethod
    def _error(message: str) -> Dict[str, Any]:
        from contracts import ErrorCode

        return {"success": False, "error_code": ErrorCode.FEATURE_NOT_IMPLEMENTED.value,
                "error_message": clean_text(message, 300)}

    async def tool(self, args: Dict[str, Any]) -> Dict[str, Any]:
        op = str(args.get("op") or "")
        try:
            if op == "save":
                return self._save(args)
            if op == "list":
                return self._list()
            if op == "delete":
                rid = str(args.get("id") or "")
                if not self.store.delete(rid):
                    return self._error(f"레시피 {rid!r} 가 없습니다.")
                return {"success": True, "data": {"deleted": rid}}
            if op == "run":
                return await self._run(args)
        except RecipeError as exc:
            return self._error(str(exc))
        return self._error("op 는 save|run|list|delete 중 하나입니다.")

    def _save(self, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            last_n = int(args.get("last_n") or 0)
        except (TypeError, ValueError):
            last_n = 0
        if not 1 <= last_n <= MAX_STEPS:
            raise RecipeError(f"last_n 은 1~{MAX_STEPS} 입니다.")
        entries = self.trajectory.last(last_n)
        if not entries:
            have = len(self.trajectory.entries)
            raise RecipeError(
                f"성공하고 사후 확인을 통과한 최근 단계가 {have}개뿐입니다(last_n={last_n}). "
                "실패·치유·승인 재생·사람 조작·미지원 동작에서 기록이 끊깁니다.")
        params = args.get("params") or {}
        pins = args.get("pins") or {}
        if not isinstance(params, dict) or not isinstance(pins, dict):
            raise RecipeError("params·pins 는 객체여야 합니다.")
        dropped: List[str] = []
        rec = compile_recipe(args.get("name"), entries, params=params, pins=pins, dropped=dropped)
        saved = self.store.save(rec)
        data: Dict[str, Any] = {
            "recipe": {"id": saved["id"], "name": saved["name"], "steps": saved["steps"],
                       "params": rec["params"], "merged": saved["merged"],
                       "targets": [((s["variants"] or [{}])[-1].get("target") or {}).get("kind")
                                   for s in rec["steps"]]},
            "persisted": saved["persisted"],
        }
        if dropped:
            # R1 NB-2: 치환 안 된 쿼리 값은 저장하지 않는다(키만) — 재생 때 값이 필요하면 params 로.
            keys_ = sorted(set(dropped))[:10]
            data["dropped_query_values"] = {
                "keys": keys_,
                "hint": "이동 URL 의 이 쿼리 값은 저장하지 않았습니다(빈 값으로 재생). 값이 필요하면 "
                        "params 로 지정해 다시 save 하세요(토큰·세션·이메일 등 민감 값은 params 도 거부).",
            }
        if self.store.warnings:
            data["warnings"] = [clean_text(w, 200) for w in self.store.warnings[-3:]]
        return {"success": True, "data": data}

    def _list(self) -> Dict[str, Any]:
        out = []
        for rec in self.store.list():
            st = rec["stats"]
            out.append({"id": rec["id"], "name": rec["name"], "origin": rec["origin"],
                        "steps": len(rec["steps"]), "params": list(rec.get("params") or []),
                        "runs": st.get("runs", 0), "ok": st.get("ok", 0),
                        "disabled": bool(st.get("disabled"))})
        return {"success": True, "data": {
            "recipes": out, "storage": "profile" if self.store.persistent else "memory"}}

    async def _run(self, args: Dict[str, Any]) -> Dict[str, Any]:
        from interface.mcp_server import envelope_dict

        if not self.server.started:
            await self.server.start()
        params = args.get("params") or {}
        if not isinstance(params, dict):
            raise RecipeError("params 는 객체여야 합니다.")
        self.running = True
        try:
            return await run_recipe(self.host, self.store, str(args.get("id") or ""), params,
                                    encode=envelope_dict)
        finally:
            self.running = False
            self.reset("replay")  # 재생한 단계는 궤적에 넣지 않는다
