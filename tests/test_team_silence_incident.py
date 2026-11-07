"""问题 A：队友击杀 / 队友阵亡被播报（回归用例）。

**结论先写在这里，别再造一次谣言**：

1. 出问题的那一局，插件进程启动得比两道闸上线还早；而两道闸
   （``_derive`` 的 ``and self.team_events_enabled`` 与 ``_pick`` 的 ``TEAM_EVENT_IDS``）
   是之后才写进磁盘的 —— **运行中的进程不可能读到后写进去的代码**。所以那 15 条是
   "闸之前"的行为，不是现在这条链路绕过了闸。
2. 现在真生效的值是 ``False``：``config.dump()`` 的 ``[neko_wot]`` 段来自**宿主运行配置**
   ``<app_data>/plugins/neko_wot/config/plugin.toml``（里面没有这个键 → 走
   ``settings.DEFAULTS`` 的 False）。仓库里的 ``config.example.toml`` 只是**首次初始化**
   时拷过去当种子（``ensure_plugin_layout_runtime_config``），不是运行时读取源。
3. 本文件按**一局的真实节奏**回放：同一个回放，开关关着 → 队伍事件 0 条；
   开关打开 → 真的会出一串"干掉一个。"。两条一起看，才排除"用例本身没驱动检测器"的假绿。

反向对照（把闸拆掉任意一道就红）见 ``tests/test_detectors_derived.py`` 里的同窗口用例。
"""

from __future__ import annotations

import asyncio
from collections import deque

import sdk_stub

_pkg = sdk_stub.load_plugin_module()
arbiter_mod = _pkg.arbiter
phrases_mod = _pkg.phrases
plugin_mod = _pkg
settings_mod = _pkg.settings
state_reader = _pkg.state_reader

# (相对开打秒数, 敌方存活, 我方存活)；每个点只掉一格，第一次出现就是那一格对应的播报。
INCIDENT_TIMELINE: tuple[tuple[float, int, int], ...] = (
    (0.0, 15, 15),      # battle_start
    (191.0, 14, 15),    # 干掉一个。
    (248.0, 13, 15),    # 干掉一个。
    (259.0, 12, 15),    # 干掉一个。
    (265.0, 12, 14),    # 我们这边少了一个。
    (267.0, 11, 14),    # 干掉一个。
    (281.0, 11, 13),    # 我们这边少了一个。
    (283.0, 10, 13),    # 干掉一个。
    (292.0, 9, 13),     # 干掉一个。
    (302.0, 8, 13),     # 干掉一个。
    (309.0, 7, 13),     # 干掉一个。
    (318.0, 6, 13),     # 干掉一个。
    (349.0, 5, 13),     # 干掉一个。
    (358.0, 4, 13),     # 干掉一个。
    (377.0, 3, 13),     # 干掉一个。
    (381.0, 2, 13),     # 干掉一个。
    (388.0, 1, 13),     # 干掉一个。
    (396.0, 0, 13),     # 干掉一个。
)

# 生产参数（宿主运行配置 <app_data>/plugins/neko_wot/config/plugin.toml）
PROD = {"min_gap_s": 2.0, "idle_fill_s": 30.0}


def _frame(seq: int, *, enemy_alive: int, ally_alive: int, events=(), mode: str = "battle", result=None):
    return state_reader.Frame(
        status=state_reader.STATUS_OK,
        schema=1,
        seq=seq,
        ts=1000.0,
        mode=mode,
        arena_id="arena-1901",
        me={"vehicle_name": "705工程A", "health_ratio": 0.8, "alive": True, "reload_ratio": 0.5, "fires": 0},
        team={
            "enemy_alive": enemy_alive,
            "enemy_total": 15,
            "ally_alive": ally_alive,
            "ally_total": 15,
            "enemy_total_known": True,
        },
        arena={"period": 2, "time_left": 300},
        # 那一局写端还没有 mode_kind（v1.30 才有）：顺手让人数差播报闭嘴，
        # 这条回放只盯"队伍事件到底说不说"。
        mode_kind=None,
        result=result,
        events=tuple(events),
    )


def _replay(arbiter) -> list[tuple[float, str | None, str]]:
    said: list[tuple[float, str | None, str]] = []
    for index, (now, enemy_alive, ally_alive) in enumerate(INCIDENT_TIMELINE, start=1):
        text = arbiter.observe(_frame(index, enemy_alive=enemy_alive, ally_alive=ally_alive), now)
        if text:
            said.append((now, arbiter.last_cue.event_id if arbiter.last_cue else None, text))
    # 16:39:36 结算：这一句**应该**说得出来（闸只静音队伍事件，不静音结算）
    end = arbiter.observe(
        _frame(
            len(INCIDENT_TIMELINE) + 1,
            enemy_alive=0,
            ally_alive=13,
            mode="dead",
            result={"won": False, "kills": 0, "damage": 900},
            events=({"seq": 9901, "id": "battle_end", "ts": 1000.0, "payload": {"result": "lose"}},),
        ),
        410.0,
    )
    if end:
        said.append((410.0, arbiter.last_cue.event_id if arbiter.last_cue else None, end))
    return said


def test_the_incident_replays_silent_for_team_events_with_the_switch_off():
    """默认配置回放那一局：``干掉一个。`` / ``我们这边少了一个。`` 一条都不许出现。"""
    arbiter = arbiter_mod.Arbiter(**PROD)
    assert arbiter.team_events_enabled is False, "默认必须是静音（settings.py:47-55 的契约）"

    said = _replay(arbiter)

    team_lines = [row for row in said if row[1] in ("enemy_killed", "ally_killed")]
    assert team_lines == [], f"队伍事件被播报了：{team_lines!r}"
    leaked = [row for row in said if "干掉一个" in row[2] or "我们这边少了一个" in row[2]]
    assert leaked == [], f"话术表里那两句泄漏出来了：{leaked!r}"
    assert said and said[-1][1] == "battle_end", "闸只管队伍事件：结算这一句该说还得说"


def test_the_same_replay_does_drive_the_team_detectors_when_the_switch_is_on():
    """**反假绿**：同一个回放打开开关必须真的出一串队伍事件。

    否则上面那条"0 条"可能只是因为用例根本没喂动检测器 —— 那不是闸，是空转。
    打开后按 ``info`` 档说（不抢 warning/critical 的位置）。
    """
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0, team_events_enabled=True)

    said = _replay(arbiter)

    team_lines = [row for row in said if row[1] in ("enemy_killed", "ally_killed")]
    assert len(team_lines) >= 15, f"回放没驱动出队伍事件（用例空转）：{said!r}"
    assert any("干掉一个" in row[2] for row in team_lines)
    assert any("我们这边少了一个" in row[2] for row in team_lines)


def test_that_line_only_comes_from_enemy_killed_and_no_you_killed_variant():
    """``干掉一个。`` 的出处只有一个：``enemy_killed.one``。

    顺便把"万一是 you_killed 的另一条文案"这条怀疑一次结清：``you_killed`` 四档
    （default / streak / ammo / fire）逐条渲染，没有一条等于它。
    """
    book = phrases_mod.PHRASE_BOOK
    assert book.text("enemy_killed", "one") == "干掉一个。"

    you_killed = set()
    for tier in ("default", "streak", "ammo", "fire"):
        line = book.text("you_killed", tier, kills=1)
        assert line is not None
        you_killed.add(line)
        assert line != "干掉一个。"
    assert len(you_killed) == 4, "you_killed 的四档必须是四句不同的话"

    # 整张表扫一遍：``干掉一个。`` 只出现在 enemy_killed.one
    owners = [
        (event_id, tier)
        for event_id, tiers in phrases_mod.DEFAULT_PHRASES.items()
        for tier, lines in tiers.items()
        if "干掉一个。" in lines
    ]
    assert owners == [("enemy_killed", "one")], f"这句还有别的出处：{owners!r}"


def test_the_derived_layer_does_not_even_synthesise_team_events_when_off():
    """第一道闸在**派生层**：关着的时候这两条事件根本不该被合成出来。

    和下面 ``_pick`` 那道出口闸各守一处 —— 单独拆掉任意一道，本文件都还有用例能红。
    """
    arbiter = arbiter_mod.Arbiter(**PROD)
    assert arbiter.observe(_frame(1, enemy_alive=15, ally_alive=15), 0.0) is None, "先建基线"

    derived = arbiter._derive(_frame(2, enemy_alive=14, ally_alive=14), 1.0)

    ids = [str(event.get("id")) for event in derived]
    assert "enemy_killed" not in ids, ids
    assert "ally_killed" not in ids, ids


def test_the_activation_gate_is_a_second_lock_for_foreign_events():
    """第二道闸在出口（``_pick``）：老 mod 直接塞同 id 事件也出不了声。"""
    arbiter = arbiter_mod.Arbiter(**PROD)
    assert arbiter.observe(_frame(1, enemy_alive=15, ally_alive=15), 0.0) is None

    injected = (
        {"seq": 7001, "id": "enemy_killed", "ts": 1000.0, "payload": {"n": 1, "left": 14}},
        {"seq": 7002, "id": "ally_killed", "ts": 1000.0, "payload": {"n": 1, "left": 14}},
    )
    text = arbiter.observe(_frame(2, enemy_alive=15, ally_alive=15, events=injected), 1.0)

    assert text is None, f"外来的队伍事件从出口漏出去了：{text!r}"
    assert arbiter._queue == deque(), "挡在外面的东西不许悄悄进队列"


def test_the_effective_switch_is_off_because_the_runtime_config_has_no_key():
    """配置来源：``config.dump()`` 的 ``[neko_wot]`` = 宿主运行配置那一份。

    运行配置里**没有** ``team_events_enabled``（种子 ``config.example.toml`` 里有，
    但那只在首次初始化时拷一次），所以真生效的是 ``DEFAULTS`` 的 False。
    """
    # 与 <NEKO_APP_DATA>/plugins/neko_wot/config/plugin.toml 现值的键集合一致
    runtime_section = {
        "dry_run": False,
        "state_path": "",
        "poll_interval_s": 0.25,
        "stale_seconds": 5.0,
        "min_gap_s": 2.0,
        "idle_fill_s": 30.0,
        "output_mode": "verbatim",
        "info_channel": "voice",
        "interrupt_enabled": True,
    }
    assert "team_events_enabled" not in runtime_section

    effective = {"plugin": {"id": "neko_wot"}, "plugin_runtime": {"enabled": True}, "neko_wot": runtime_section}
    settings = settings_mod.load_settings(effective)

    assert settings.team_events_enabled is False
    assert settings.min_gap_s == 2.0, "确认读的就是这一段（不是空 section 的默认值）"
    assert settings.idle_fill_s == 30.0


def test_startup_log_reports_the_effective_team_switch(make_plugin):
    """可观测性：启动日志必须能看出这个开关到底是多少。

    16:35 那一局之所以只能靠"话术反推"，就是因为启动日志里没有这个字段 ——
    ``dry_run`` / ``output_mode`` / ``gap`` 都打了，唯独漏了这个会改变播报内容的开关。
    """
    for expected in (False, True):
        plugin, host = make_plugin({"dry_run": True, "team_events_enabled": expected})
        asyncio.run(plugin.startup())

        assert plugin._arbiter.team_events_enabled is expected
        records = [record for record in host.logger.records if "neko_wot startup:" in record]
        assert records, f"没有启动日志：{host.logger.records!r}"
        assert f"team_events={expected}" in records[0], records[0]
