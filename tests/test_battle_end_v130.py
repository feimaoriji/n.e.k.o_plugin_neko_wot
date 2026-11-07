"""v1.30 / P2：``battle_end`` 的 15 键战果 + 三档胜负 + 数据来源单一化。

协议 §5.3（已冻结）：payload 15 键 —— ``result`` / ``kills`` / ``dmg_dealt_total`` /
``ally_alive`` / ``ally_total`` / ``enemy_alive`` / ``enemy_total`` / ``battle_seconds`` /
``hits_taken`` / ``blocked_count`` / ``shots_fired`` / ``deaths`` / ``respawns`` /
``src`` / ``mode_kind``。``result`` 只有四个值：``win`` / ``lose`` / ``draw`` / ``unknown``。

读端两条铁律：
1. **来源单一化**：``state.result`` 优先（面板在结算那一刻的实时读数），
   ``battle_end.payload.result`` 只做兜底；两者矛盾以 state 为准并留 DEBUG。
2. **unknown 绝不猜**：只说事实（打了几台/多少伤害），输赢一个字不提；
   数字缺哪个就少说哪个 ——「击毁 0 台」和「不知道几台」是两件事。
"""

from __future__ import annotations

import logging

import sdk_stub

_pkg = sdk_stub.load_plugin_module()
arbiter_mod = _pkg.arbiter
state_reader = _pkg.state_reader


def _payload(**overrides) -> dict:
    """一份**完整**的 15 键 payload（协议 §5.3 的形状）。"""
    base = {
        "result": "unknown",
        "kills": None,
        "dmg_dealt_total": None,
        "ally_alive": None,
        "ally_total": None,
        "enemy_alive": None,
        "enemy_total": None,
        "battle_seconds": 431,
        "hits_taken": None,
        "blocked_count": None,
        "shots_fired": None,
        "deaths": 1,
        "respawns": 0,
        "src": {"result": "panel", "kills": "panel", "dmg_dealt_total": "stats"},
        "mode_kind": "standard",
    }
    base.update(overrides)
    return base


def _frame(result=None, payload=None, *, seq: int = 7, arena: str = "arena-1", kills_hint: int = 0):
    frame = state_reader.Frame(
        status=state_reader.STATUS_OK,
        schema=1,
        seq=seq,
        ts=1000.0,
        mode="battle",
        arena_id=arena,
        me={"vehicle_name": "WZ-219", "health_ratio": 0.0},
        result=result,
        mode_kind="standard",
        events=({"seq": seq, "id": "battle_end", "ts": 1000.0, "payload": payload or {}},),
    )
    return frame


def _say(result=None, payload=None, *, kills_hint: int = 0):
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    # 首帧会重置局内计数（``_clear_derived``），所以先喂一帧空窗口把基线建好，
    # 再把手数到的击杀灌进去 —— 之后那一帧才是"结算"。
    arbiter.observe(
        state_reader.Frame(
            status=state_reader.STATUS_OK,
            schema=1,
            seq=6,
            ts=999.0,
            mode="battle",
            arena_id="arena-1",
            me={"vehicle_name": "WZ-219", "health_ratio": 0.1},
            mode_kind="standard",
            events=(),
        ),
        999.0,
    )
    arbiter._kills = kills_hint
    return arbiter.observe(_frame(result, payload), 1000.0)


def test_win_reads_kills_and_damage_from_payload_when_state_is_silent():
    """state.result 判不出胜负 → 用 payload.result；数字从 payload 的 kills/dmg_dealt_total 取。"""
    said = _say(None, _payload(result="win", kills=3, dmg_dealt_total=2870))

    assert said is not None
    assert "这一局结束了" in said
    assert "赢了！" in said
    assert "击毁 3 台" in said
    assert "2870" in said


def test_lose_tier_is_spoken():
    said = _say(None, _payload(result="lose", kills=0, dmg_dealt_total=180))
    assert said is not None
    assert "输了" in said
    assert "击毁 0 台" in said, "0 台是**真数**（panel 给的就是 0），照样要念：%r" % (said,)


def test_unknown_says_nothing_about_win_or_lose():
    """四值里的 ``unknown``：一个输赢字都不许出现，但战报数字照说。"""
    said = _say(None, _payload(result="unknown", kills=2, dmg_dealt_total=800))

    assert said is not None
    assert "打完了" in said
    assert "赢了" not in said and "输了" not in said and "打平" not in said, said
    assert "击毁 2 台" in said and "800" in said, said


def test_state_result_wins_over_a_contradicting_payload(caplog):
    """矛盾时以 ``state.result`` 为准，并留一条 DEBUG（不吵、但也不吞矛盾）。"""
    caplog.set_level(logging.DEBUG, logger="neko_wot.arbiter")

    said = _say({"won": True, "kills": 1, "damage": 300}, _payload(result="lose", kills=9, dmg_dealt_total=9999))

    assert said is not None
    assert "赢了" in said, said
    assert "输了" not in said
    # 数字也跟着 state（来源单一化：不允许一半来自 state、一半来自 payload）
    assert "击毁 1 台" in said and "300" in said, said
    assert "9999" not in said and "9 台" not in said, said
    assert any("矛盾" in record.message for record in caplog.records), [r.message for r in caplog.records]


def test_payload_result_is_ignored_when_state_has_an_outcome():
    """state 有胜负时 payload 的 result 不参与判定（它只是兜底）。"""
    said = _say({"won": False, "kills": 2, "damage": 640}, _payload(result="win"))
    assert said is not None
    assert "输了" in said


def test_empty_payload_and_no_result_says_unknown_without_numbers():
    """两边都没有数字：只说"打完了"，绝不吐"击毁 0 台"。"""
    said = _say(None, None, kills_hint=0)

    assert said is not None
    assert "打完了" in said
    assert "0 台" not in said and "赢" not in said and "输" not in said, said


def test_missing_kills_falls_back_to_my_own_count_said_as_a_count():
    """payload 没给击杀数、但我自己数到过 → 说"我数到"，不冒充实数。"""
    said = _say(None, _payload(result="win", kills=None, dmg_dealt_total=None), kills_hint=2)

    assert said is not None
    assert "我数到击毁 2 台" in said, said
    assert "击毁 2 台，" not in said, "别把自数说成面板实数：%r" % (said,)


def test_assist_is_spoken_only_when_present():
    with_assist = _say({"won": True, "kills": 1, "damage": 300, "assist": 415}, _payload(result="win"))
    assert with_assist is not None
    assert "协助伤害 415" in with_assist

    without = _say({"won": True, "kills": 1, "damage": 300, "assist": 0}, _payload(result="win"))
    assert without is not None
    assert "协助" not in without, without


def test_repeated_battle_end_in_the_sliding_window_is_spoken_once():
    """同一局的 ``battle_end`` 在多个快照里重复出现（mod 的滑动窗口）→ 只播 1 条。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    assert arbiter.observe(_frame(None, _payload(result="win", kills=1), seq=7), 1000.0) is not None
    assert arbiter.observe(_frame(None, _payload(result="win", kills=1), seq=7), 1001.0) is None


def test_no_enemy_numbers_leak_into_the_result_line():
    """payload 里的敌方存活/总数一个字都不许进话术（白名单里没有它们的位置）。"""
    said = _say(None, _payload(result="win", kills=2, dmg_dealt_total=900, ally_alive=9, ally_total=15,
                               enemy_alive=1, enemy_total=15))

    assert said is not None
    for word in ("敌方", "敌人", "对面", "15", "1 个"):
        assert word not in said, (word, said)


def test_battle_seconds_is_never_rendered():
    """``battle_seconds`` 在白名单里（写端给、面板也有），但**话术不用它** —— 不许因为
    "payload 里有这个数字"就自己编一句。"""
    said = _say(None, _payload(result="draw", battle_seconds=431))
    assert said is not None
    assert "431" not in said, said
