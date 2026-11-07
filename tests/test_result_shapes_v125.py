"""mod v1.25 的 result 形状 → 插件话术（协议漂移的回归闸）。

背景（回放 + 端到端实测）：

* mod v1.24 起产 ``result = {"outcome": "win|lose", ...}``，而插件读 ``result["won"]``
  （bool）—— 键名对不上，**结算胜负那句永远不会说**。两边各自有测试、各自全绿，
  协议快照也测不出来，因为快照只校验"顶层键稳定"，不校验 result 内部语义。
* v1.25 把 mod 侧改成协议 §6 形状（``won``/``kills``/``damage``/...）+ v1.24 兼容
  字段并存，这个文件就是"形状对齐"的闸门。

三条用例覆盖：回放实测形状 / 真打假设形状 / 旧 v1.24 形状（向后兼容现状）。
"""

from __future__ import annotations

import sdk_stub

_pkg = sdk_stub.load_plugin_module()
arbiter_mod = _pkg.arbiter
state_reader = _pkg.state_reader


def _frame(result: dict) -> object:
    """一帧 battle_end：只带结算事件 + result，其它字段留默认。"""
    event = {"id": "battle_end", "seq": 1, "ts": 0.0, "payload": {}}
    return state_reader.Frame(
        status="ok",
        schema=1,
        seq=100,
        ts=1793478253.796,
        mode="battle_end",
        game_version="2.4.1.1",
        player_id=123456,
        events=(event,),
        result=result,
    )


def test_replay_shaped_result_still_speaks() -> None:
    """回放实测形状（won=null / kills=0 / survived=true）：照常出结算句，不哑火。"""
    result = {
        "won": None, "draw": None, "survived": True, "kills": 0,
        "damage": None, "assist": None, "xp": None, "deaths": 0, "respawns": 0,
        "winner": None, "outcome": None, "reason": None, "my_team": 2,
    }
    said = arbiter_mod.Arbiter().observe(_frame(result), now=1000.0)
    assert said is not None, "result 有值却一句话都没说（回放结算会变成哑巴）"
    assert "这一局结束了" in said
    assert "赢" not in said and "输" not in said, "判不出胜负时不许猜胜负：%r" % (said,)


def test_real_battle_shaped_result_reports_outcome_and_stats() -> None:
    """真打形状（won=True / kills=3 / damage=2870）：胜负 + 战果都要说对。"""
    result = {
        "won": True, "draw": None, "survived": False, "kills": 3,
        "damage": 2870, "assist": 415, "xp": 1240, "deaths": 1, "respawns": 0,
        "winner": 1, "outcome": "win", "reason": "winners", "my_team": 1,
    }
    said = arbiter_mod.Arbiter().observe(_frame(result), now=1000.0)
    assert said is not None
    assert "赢了" in said, "won=True 必须说赢：%r" % (said,)
    assert "3 台" in said, "击杀数必须念出来：%r" % (said,)
    assert "2870" in said, "伤害必须念出来：%r" % (said,)


def test_legacy_v124_shape_is_now_read_by_outcome() -> None:
    """旧 v1.24 形状（只有 ``outcome``，没有 ``won``）：v1.30 起**按 outcome 判定**。

    这条用例原来是"已知缺口"锁：读端只认 ``won``（bool），``outcome`` 一律不翻译。
    已冻结的协议（协议 §5.3）把派生顺序写死了：
    ``result.won === true → win``、``=== false → lose``、``result.outcome == "win"/"lose"``、
    ``result.draw === true → draw``、其余 ``unknown``。所以 v1.30 起 outcome 是**协议
    指定**的第三顺位来源，这里改成锁"它确实被认了、而且只认 win/lose 两个字面量"。

    真正的"不许猜"由 ``unknown`` 那一档守（见上面 ``test_unknown_result_*``）。
    """
    result = {"winner": 1, "outcome": "win", "reason": "winners", "my_team": 1}
    said = arbiter_mod.Arbiter().observe(_frame(result), now=1000.0)
    assert said is not None, "旧形状也不许崩"
    assert "这一局结束了" in said
    assert "赢了" in said, "协议 §5.3 第三顺位：outcome == \"win\" 必须认：%r" % (said,)

    # 只认两个字面量：别的 outcome 值一律不下结论（不猜）。
    for bogus in ("Victory", "WINNER", "", "unknown", None):
        result = {"winner": 1, "outcome": bogus, "my_team": 1}
        said = arbiter_mod.Arbiter().observe(_frame(result), now=1000.0)
        assert said is not None
        assert "赢" not in said and "输" not in said, (bogus, said)
