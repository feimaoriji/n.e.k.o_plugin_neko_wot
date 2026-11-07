"""v1.30 / P4：队伍事件（队友击杀 / 队友阵亡）默认静音。

设计口径：「没必要，击杀播报只聚焦用户本人」—— 那一局 15 次队友击杀全播了。
所以这一轮只加一个开关 ``team_events_enabled``：

* 默认 ``False``：`enemy_killed` / `ally_killed` **一条都不合成**（派生层就不产生），
  出口再挡一道（老 mod 直接塞同 id 事件也出不了声）；
* ``True``：按 ``info`` 档说 —— 不占 ``warning`` / ``critical`` 的位置，也不抢它们的话。

判据（``team`` 计数边沿）一个字没动，变的只是"说不说 + 哪一档"。
"""

from __future__ import annotations

import sdk_stub

_pkg = sdk_stub.load_plugin_module()
arbiter_mod = _pkg.arbiter
settings_mod = _pkg.settings
state_reader = _pkg.state_reader


def _team(enemy_alive: int, ally_alive: int) -> dict:
    return {
        "enemy_alive": enemy_alive,
        "enemy_total": 15,
        "ally_alive": ally_alive,
        "ally_total": 15,
        "enemy_total_known": True,
    }


def _frame(seq: int, *, enemy_alive: int = 15, ally_alive: int = 15, events=(), me=None):
    return state_reader.Frame(
        status=state_reader.STATUS_OK,
        schema=1,
        seq=seq,
        ts=1000.0,
        mode="battle",
        arena_id="arena-1",
        me=me or {"vehicle_name": "WZ-219", "health_ratio": 0.8, "alive": True},
        team=_team(enemy_alive, ally_alive),
        mode_kind="standard",
        events=tuple(events),
    )


def _drive_teammate_kills(arbiter, count: int = 15) -> list[str | None]:
    """15 次队友击杀：敌人数一格格往下掉（这就是派生 enemy_killed 的边沿）。"""
    said: list[str | None] = [arbiter.observe(_frame(1), 0.0)]
    for index in range(count):
        said.append(arbiter.observe(_frame(2 + index, enemy_alive=14 - index), 10.0 + index * 10.0))
    return said


def test_default_is_silent_for_fifteen_teammate_kills_and_a_teammate_death():
    """默认配置：15 次队友击杀 + 一次队友阵亡 → **0 条**播报。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    assert arbiter.team_events_enabled is False

    said = _drive_teammate_kills(arbiter)
    assert [text for text in said if text], said

    # 队友阵亡（我方 -1）
    assert arbiter.observe(_frame(40, enemy_alive=0, ally_alive=14), 400.0) is None


def test_enabled_speaks_teammate_kills_on_the_info_tier():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0, team_events_enabled=True)

    text = arbiter.observe(_frame(1), 0.0)  # 建基线
    assert text is None
    text = arbiter.observe(_frame(2, enemy_alive=14), 10.0)

    assert text is not None and "干掉一个" in text, text
    assert arbiter.last_cue is not None
    assert arbiter.last_cue.level == arbiter_mod.LEVEL_INFO, "队伍事件只能占 info 档"
    assert arbiter.last_cue.priority == arbiter_mod.LEVEL_PRIORITY[arbiter_mod.LEVEL_INFO]


def test_enabled_ally_killed_is_also_info():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0, team_events_enabled=True)
    arbiter.observe(_frame(1), 0.0)
    text = arbiter.observe(_frame(2, ally_alive=14), 10.0)

    assert text is not None and "少了一个" in text, text
    assert arbiter.last_cue.level == arbiter_mod.LEVEL_INFO


def test_static_table_is_untouched_it_is_only_a_runtime_downgrade():
    """``EVENT_LEVELS`` 里这两条仍然是 warning —— 降档是**运行时**的，表不动。

    这样"分级表 = 静态事实"这条闸（``test_event_levels``）继续有效；队伍事件的
    info 语义由 ``_level_of`` 一处负责，改坏一个地方就红。
    """
    assert arbiter_mod.EVENT_LEVELS["enemy_killed"] == arbiter_mod.LEVEL_WARNING
    assert arbiter_mod.EVENT_LEVELS["ally_killed"] == arbiter_mod.LEVEL_WARNING


def test_critical_line_still_wins_over_a_teammate_kill_in_the_same_frame():
    """同帧有 critical（自己被打死）：队伍事件不许抢。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0, team_events_enabled=True)
    arbiter.observe(_frame(1), 0.0)

    died = {"seq": 2, "id": "you_died", "ts": 1000.0, "payload": {}}
    text = arbiter.observe(_frame(2, enemy_alive=14, events=[died]), 10.0)

    assert text is not None
    assert "击毁" in text or "缓一下" in text, text
    assert "干掉" not in text, text


def test_pick_gate_is_a_second_lock_for_foreign_events():
    """老 mod 直接把同 id 的事件塞进快照：开关关着时出口闸也要挡住。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    injected = {"seq": 2, "id": "enemy_killed", "ts": 1000.0, "payload": {"n": 1, "left": 14}}

    assert arbiter.observe(_frame(1), 0.0) is None
    # 敌人数不变（避免人数差播报混进来），只喂一条"外来"的 enemy_killed 事件
    assert arbiter.observe(_frame(2, events=[injected]), 10.0) is None


def test_settings_default_and_dirty_values_fall_back_to_silent():
    """配置层：默认 False；认不出来的值一律回落到 False（这条开关的本意是少说话）。"""
    assert settings_mod.DEFAULTS["team_events_enabled"] is False
    assert settings_mod.parse_settings({}).team_events_enabled is False
    for dirty in (None, "", "maybe", [], {}):
        assert settings_mod.parse_settings({"team_events_enabled": dirty}).team_events_enabled is False, dirty
    assert settings_mod.parse_settings({"team_events_enabled": True}).team_events_enabled is True
    assert settings_mod.parse_settings({"team_events_enabled": "yes"}).team_events_enabled is True


async def test_plugin_wiring_passes_the_switch_to_the_arbiter(make_plugin):
    """插件装配层：配置真的传到了 ``Arbiter``（不然开关是个摆设）。"""
    plugin, _host = make_plugin({"dry_run": False})
    await plugin.startup()
    assert plugin._arbiter.team_events_enabled is False

    plugin, _host = make_plugin({"dry_run": False, "team_events_enabled": True})
    await plugin.startup()
    assert plugin._arbiter.team_events_enabled is True
