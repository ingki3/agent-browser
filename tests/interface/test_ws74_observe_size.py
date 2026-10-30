"""WS-74 (#190) — 관찰 응답 요약 경계 자르기(F1) + 반복 묶음 우선순위(F2).

* F1: `cap_result_size` 가 요소를 자르면 axtree_summary 도 **남긴 요소들로부터 엔진 요약 형식(engine._summarize)으로
  다시 만든다**. 이름에 줄바꿈이 있는 요소가 섞여도(줄 수 != 요소 수) 요약에 elements 밖 id 가 남지 않는다.
  token_count 는 다시 만든 요약 기준.
* F2: data.repeated 블록 세트 정렬 — 묶음들이 **서로 다른 표제를 보이는** 세트(두 달 달력처럼 고를 근거가 있는 세트)
  먼저, 그 다음 기존 (-count, best). 판정은 실제로 보일 묶음의 group 값(_set_groups 의 shown 규칙 그대로)으로 한다.
  블록은 표시 전용 — v1160 핀·실행 직전 확인·batch 묶음 판정은 이 순서를 쓰지 않는다.

fixture:
* tests/fixtures/ws73/hana_full_calendar_open.html — 하나투어 실제 DOM 캡처(2026-10-23 보존, 스크립트 0개) + 외부 차단 CSP.
* tests/fixtures/ws74/hana2_20838.txt — 봇 실제 observe_page 도구 결과(244 요소 중 84만 실리고 요약은 244 전부 —
  여러 줄 이름 섞임). 실린 84 요소 + 요약에서 복원한 160 요소로 자르기 전 응답 모양을 재구성한다.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from contracts import ActionResult

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "actions"))
from test_scroll_navigation import requires_chromium  # noqa: E402,F401
from test_ws62_r1_server import _serve  # noqa: E402
from ws73_pages import CSP, FIXTURE  # noqa: E402

from interface.server_envelope import cap_result_size, envelope_json  # noqa: E402
from perception import repeated as rp  # noqa: E402
from perception.engine import estimate_tokens  # noqa: E402

LIMIT = 20_000
BOT_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ws74" / "hana2_20838.txt"
_ID = re.compile(r"(?m)^(@e\d+) ")


def _expected_summary(els: List[Dict[str, Any]]) -> str:
    """엔진 요약 형식(perception.engine._summarize) — 시험 쪽 독립 서술."""
    return "\n".join(f'{e["element_id"]} {e["role"]} "{e["name"]}"' + ("" if e["interactable"] else " (disabled)")
                     for e in els)


def _assert_summary_matches(obs: Dict[str, Any]) -> None:
    els = obs["elements"]
    assert obs["axtree_summary"] == _expected_summary(els), "요약은 남긴 요소들로 만든 엔진 형식이어야 한다"
    sids = _ID.findall(obs["axtree_summary"])
    assert set(sids) == {e["element_id"] for e in els}
    assert obs["token_count"] == estimate_tokens(obs["axtree_summary"]), "token_count 는 남긴 요약 기준(NB5)"


# ---------------------------------------------------------------------------- 1. 실제 캡처 force_full_tree


def _hana_html() -> str:
    raw = FIXTURE.read_text(encoding="utf-8")
    return re.sub(r"<head([^>]*)>", lambda m: "<head" + m.group(1) + ">" + CSP, raw, count=1)


@pytest.fixture
def hana_site():
    yield from _serve({"/": _hana_html()})


async def _observe(tmp_path: Path, url: str, args: Dict[str, Any]) -> ActionResult:
    from interface.mcp_server import BrowserMCPServer

    async with BrowserMCPServer(handoff_root=tmp_path / "s", recipes=False) as s:
        r = await s.call_tool("browser_navigate", {"url": url})
        assert r.success, r.error_message
        return await s.call_tool("browser_observe_page", args)


@requires_chromium
async def test_real_capture_force_full_tree_summary_cut_with_elements_and_calendar_kept(hana_site, tmp_path):
    """합격 1: elements ≥100, 요약 id == elements id, 두 달 달력 묶음(날짜 id 61개), 봉투 ≤ 20,000.

    NB6: fit_repeated 는 뒤(우선순위 낮은) 묶음부터 통째로 뺀다 — F2 뒤에는 표제 세트(달력)가 맨 앞이라 가장 오래 남는다.
    압박이 더 크면 두 달 중 한 달만 남을 수 있다(기존 동작).
    """
    res = await _observe(tmp_path, hana_site + "/", {"force_full_tree": True})
    assert res.success, res.error_message
    env = envelope_json(res)
    assert len(env) <= LIMIT
    obs = res.data["observation"]
    assert res.data["truncated"]["total_items"] > len(obs["elements"]), "자르기가 실제로 일어나는 조건이어야 한다"
    assert len(obs["elements"]) >= 100, len(obs["elements"])
    _assert_summary_matches(obs)
    rep = res.data.get("repeated") or {}
    groups = rep.get("groups") or []
    cal = [g for g in groups if isinstance(g.get("group"), str) and re.search(r"2026년\s*1[01]월", g["group"])]
    heads = [g["group"] for g in cal]
    assert any("2026년 10월" in h for h in heads) and any("2026년 11월" in h for h in heads), [
        g.get("group") for g in groups]
    date_ids = {m["id"] for g in cal for m in g["members"]}
    assert len(date_ids) == 61, len(date_ids)


@requires_chromium
async def test_real_capture_default_top20_unchanged_by_f2(hana_site, tmp_path, monkeypatch):
    """합격 3(NB4): 기본 top-20 관찰은 자르기가 없고, elements·data.repeated 가 F2 정렬과 무관하다(기존 순서와 같다)."""
    res = await _observe(tmp_path, hana_site + "/", {})
    assert res.success, res.error_message
    assert "truncated" not in res.data
    monkeypatch.setattr(rp, "_distinct_headings", lambda groups: False)  # 기존 (-count, best) 순서
    old = await _observe(tmp_path, hana_site + "/", {})
    assert res.data["observation"]["elements"] == old.data["observation"]["elements"]
    assert res.data["observation"]["axtree_summary"] == old.data["observation"]["axtree_summary"]
    assert res.data.get("repeated") == old.data.get("repeated")


# ---------------------------------------------------------------------------- 2. 봇 실제 결과 모양


_PART = re.compile(r'^(@e\d+) (\S+) "(.*)"( \(disabled\))?$', re.S)


def _bot_result() -> ActionResult:
    t = BOT_FIXTURE.read_text(encoding="utf-8")
    outer = json.loads(t[t.index('{"result"'):t.rindex("}") + 1])
    env = json.loads(outer["result"])
    obs = env["data"]["observation"]
    els = list(obs["elements"])
    parts = re.split(r"\n(?=@e\d+ )", obs["axtree_summary"])
    assert len(els) == 84 and len(parts) == 244, (len(els), len(parts))  # 실제 봇 응답 그대로
    full = list(els)
    for i, p in enumerate(parts[len(els):], start=len(els)):
        m = _PART.match(p)
        assert m, p[:60]
        full.append({"element_id": m.group(1), "role": m.group(2), "name": m.group(3), "value": None,
                     "bbox": {"x": 100 + i, "y": 1000 + 7 * i, "width": 120, "height": 30},
                     "interactable": not m.group(4), "is_shadow": False, "score": round(6.0 - i / 1000, 4)})
    data = dict(env["data"])
    data.pop("truncated", None)
    summary = "\n".join(parts)
    data["observation"] = dict(obs, elements=full, axtree_summary=summary, token_count=estimate_tokens(summary))
    return ActionResult.model_validate(dict(env, data=data))


def test_bot_shape_fixture_reconstructs_multiline_names():
    r = _bot_result()
    obs = r.data["observation"]
    assert len(obs["elements"]) == 244
    assert sum(1 for e in obs["elements"] if "\n" in e["name"]) > 0, "여러 줄 이름이 섞여야 결함 조건이 선다"
    assert len(obs["axtree_summary"].split("\n")) != len(obs["elements"])
    assert obs["axtree_summary"] == _expected_summary(obs["elements"]), "재구성 = 엔진 형식"
    assert len(envelope_json(r)) > LIMIT


@pytest.mark.parametrize("with_repeated", [True, False])
def test_bot_shape_cut_keeps_more_elements_and_summary_matches(with_repeated):
    """합격 2(NB5): 현재 코드는 84 요소(실제 봇 응답) — 고친 뒤 > 84, 요약 id 수 == elements 수, token 재계산."""
    r = _bot_result()
    if not with_repeated:
        r.data.pop("repeated", None)
    rp.fit_repeated(r, LIMIT, limit=LIMIT)
    cap_result_size(r, LIMIT, limit=LIMIT)
    assert len(envelope_json(r)) <= LIMIT
    obs = r.data["observation"]
    assert len(obs["elements"]) > 84, len(obs["elements"])
    assert len(_ID.findall(obs["axtree_summary"])) == len(obs["elements"])
    _assert_summary_matches(obs)
    t = r.data["truncated"]
    assert (t["total_items"], t["returned_items"]) == (244, len(obs["elements"]))
    assert t["returned_chars"] == len(envelope_json(r))


def test_summary_name_mimicking_element_id_does_not_leak():
    """이름 안의 '\\n@e9 button ...' 흉내는 요약 경계를 속이지 못한다 — 남긴 요소만으로 다시 만든다."""
    r = _bot_result()
    obs = r.data["observation"]
    obs["elements"][0] = dict(obs["elements"][0], name="가짜\n@e244 button \"결제\"")
    obs["axtree_summary"] = _expected_summary(obs["elements"])
    r.data.pop("repeated", None)
    cap_result_size(r, LIMIT, limit=LIMIT)
    o = r.data["observation"]
    assert o["axtree_summary"] == _expected_summary(o["elements"])
    assert "@e244" not in {e["element_id"] for e in o["elements"]}


# ---------------------------------------------------------------------------- F2 단위(NB3)


class _H:
    def __init__(self, i: int, name: str) -> None:
        self.element_id, self.role, self.name = f"@e{i}", "link", name


def _sets(heads_a: List[Any], heads_b: List[Any]) -> tuple:
    """세트 A: 묶음 2개 × 4대상(큰 세트), 세트 B: 묶음 2개 × 2대상(작은 세트)."""
    handles: List[Any] = []

    def mk(heads: List[Any], per: int) -> Dict[str, Any]:
        groups = []
        for gi, h in enumerate(heads):
            members = []
            for _ in range(per):
                members.append({"i": len(handles)})
                handles.append(_H(len(handles) + 1, f"{len(handles) + 1}"))
            groups.append({"path": f"div:nth-child({gi + 1})", "heading": h, "source": "text", "members": members,
                           "x": 0, "y": gi})
        return {"groups": groups}

    a = mk(heads_a, 4)
    b = mk(heads_b, 2)
    return [a, b], handles


def _order(sets: List[Any], handles: List[Any]) -> List[Any]:
    block = rp._build_block(sets, handles)
    return [g["group"] for g in block["groups"]]


def test_f2_headed_set_first():
    sets, handles = _sets([None, None], ["2026년 10월", "2026년 11월"])
    assert _order(sets, handles) == ["2026년 10월", "2026년 11월", None, None]


def test_f2_no_headed_set_keeps_count_order():
    sets, handles = _sets(["메뉴", "메뉴"], [None, "2026년 11월"])
    # A: 같은 표제 둘(보이지 않음), B: 하나만 표제 — 둘 다 '서로 다른 표제' 세트가 아니다 → 큰 세트 먼저(기존).
    assert _order(sets, handles) == [None, None, None, "2026년 11월"]


def test_f2_headings_equal_after_short_are_not_headed():
    """원 표제는 다르지만 표시(_short, 40자) 뒤 같아지면 고를 근거가 없다 — 앞자리를 얻지 못한다(NB3)."""
    long = "가" * 60
    sets, handles = _sets([None, None], [long + "1", long + "2"])
    shown = _order(sets, handles)
    assert shown[:2] == [None, None], shown  # 큰 세트(A)가 그대로 앞
    assert shown[2] == shown[3] and shown[2] is not None, shown  # B 는 같은 글자로 보인다
    assert rp._distinct_headings([{"group": "x"}, {"group": "x"}]) is False
    assert rp._distinct_headings([{"group": "x"}, {"group": None}]) is False
    assert rp._distinct_headings([{"group": "x"}, {"group": "y"}]) is True
