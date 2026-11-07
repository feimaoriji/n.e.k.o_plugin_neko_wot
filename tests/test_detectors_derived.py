"""派生层六个 detector（v2.3）：触发 / 不触发 / 边界 + 话术。

派生事件由 :meth:`Arbiter._derive` 从 ``Frame`` 的**状态字段**推导，不来自 mod
事件，所以用例分两层写：

* 判定层（直接调 ``_derive``）：只看"这一帧该出哪几个 id"，不受全局间隔 / 同类
  冷却干扰；
* 话术层（``observe``）：确认最终说出口的那句，以及让位关系（自己阵亡时不许说
  "我们这边少了一个"）。

六个 detector：reload_done / hp_critical / enemy_killed / ally_killed /
fire_cleared / suppressed。
"""

from __future__ import annotations

import re
from dataclasses import replace

import sdk_stub

_pkg = sdk_stub.load_plugin_module()
arbiter_mod = _pkg.arbiter
state_reader = _pkg.state_reader

SUPPRESSED_LINE = "被盯上了，{MASTER_NAME}，先躲一下。"


# --------------------------------------------------------------------- 造帧
def _me(**overrides) -> dict:
    me = {
        "vehicle_name": "WZ-219",
        "health_ratio": 0.9,
        "alive": True,
        "reload_ratio": 0.2,
        "fires": 0,
    }
    me.update(overrides)
    return me


def _team(enemy_alive: int = 15, ally_alive: int = 15) -> dict:
    return {
        "enemy_alive": enemy_alive,
        "ally_alive": ally_alive,
        "enemy_total": 15,
        "ally_total": 15,
    }


def _frame(*, me=None, team=None, events=(), mode="battle", arena="arena-1", seq=1):
    return state_reader.Frame(
        status=state_reader.STATUS_OK,
        schema=1,
        seq=seq,
        ts=1000.0,
        mode=mode,
        arena_id=arena,
        me=_me() if me is None else me,
        team=_team() if team is None else team,
        result=None,
        events=tuple(events),
    )


def _hit(seq: int, yaw: float | None = None, **payload) -> dict:
    """一条 ``you_damaged``；``yaw=None`` 模拟写端没给 ``hitDirYaw``（降级路径）。"""
    body = dict(payload)
    if yaw is not None:
        body["hitDirYaw"] = yaw
    return {"seq": seq, "id": "you_damaged", "ts": 1000.0, "payload": body}


def _event(seq: int, event_id: str, payload: dict | None = None) -> dict:
    return {"seq": seq, "id": event_id, "ts": 1000.0, "payload": payload or {}}


def _ids(steps, *, team_events_enabled: bool = False) -> list[str]:
    """把若干 ``(frame, now)`` 依次喂给 ``_derive``，收集产出的 id。

    ``team_events_enabled``（v1.30）默认 False —— 队伍事件默认静音，
    要测它们必须显式打开，跟产品默认保持一致。
    """
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0, team_events_enabled=team_events_enabled)
    produced: list[str] = []
    for frame, now in steps:
        produced.extend(str(event.get("id")) for event in arbiter._derive(frame, now))
    return produced


# ------------------------------------------------------- 表接入 / 常量一致性
def test_derived_ids_are_wired_into_the_tables():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    for event_id in arbiter_mod.DERIVED_EVENT_IDS:
        assert event_id in arbiter_mod.PRIORITY, event_id
        assert arbiter._cooldown[event_id] == arbiter_mod.DERIVED_COOLDOWN_S[event_id], event_id
    # 派生 id 不许混进"mod 真事件"那张表（合规用例会逐条渲染它）
    for event_id in arbiter_mod.DERIVED_EVENT_IDS:
        assert event_id not in arbiter_mod.TYPE_COOLDOWN_S, event_id

    assert {"battle_end", "you_died"} <= arbiter_mod.GAP_BYPASS
    assert "hp_critical" not in arbiter_mod.GAP_BYPASS      # v2.4：改走排队（能被覆盖 → 额度可还）
    assert {"enemy_killed", "ally_killed", "suppressed"} <= arbiter_mod.DEFERRABLE
    assert "hp_critical" in arbiter_mod.DEFERRABLE
    assert {"enemy_killed", "ally_killed"} <= arbiter_mod.LATEST_WINS


# ------------------------------------------------------------ ① reload_done
def test_reload_done_fires_twice_across_two_fill_cycles():
    """1.0 → 0.4 → 1.0 → 0.4 → 1.0：两级边沿各说一次（同类冷却 10 秒）。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert arbiter.observe(_frame(me=_me(reload_ratio=1.0), seq=1), 0.0) is None  # 基线
    assert arbiter.observe(_frame(me=_me(reload_ratio=0.4), seq=2), 1.0) is None
    first = arbiter.observe(_frame(me=_me(reload_ratio=1.0), seq=3), 2.0)
    assert first == "炮弹上膛了，{MASTER_NAME}。"

    assert arbiter.observe(_frame(me=_me(reload_ratio=0.4), seq=4), 3.0) is None
    second = arbiter.observe(_frame(me=_me(reload_ratio=1.0), seq=5), 13.0)
    assert second == "炮弹上膛了，{MASTER_NAME}。"


def test_reload_done_needs_a_crossing_not_just_a_full_bar():
    """一直是满的、或者只涨到 0.995 之前，都不算"装填完成"。"""
    assert _ids(
        [
            (_frame(me=_me(reload_ratio=1.0), seq=1), 0.0),
            (_frame(me=_me(reload_ratio=1.0), seq=2), 1.0),
            (_frame(me=_me(reload_ratio=1.0), seq=3), 2.0),
        ]
    ) == []
    assert _ids(
        [
            (_frame(me=_me(reload_ratio=0.2), seq=1), 0.0),
            (_frame(me=_me(reload_ratio=0.55), seq=2), 1.0),
            (_frame(me=_me(reload_ratio=0.98), seq=3), 2.0),
        ]
    ) == []
    # 0.98 → 0.99 正好踩在阈值上，算满
    assert _ids(
        [
            (_frame(me=_me(reload_ratio=0.98), seq=1), 0.0),
            (_frame(me=_me(reload_ratio=0.99), seq=2), 1.0),
        ]
    ) == ["reload_done"]


def test_reload_done_is_gated_on_battle_and_alive():
    # 车库里（不在局内）装填条也会满，但不能念
    assert _ids(
        [
            (_frame(me=_me(reload_ratio=0.2), seq=1), 0.0),
            (_frame(me=_me(reload_ratio=1.0), seq=2, mode="garage"), 1.0),
        ]
    ) == []
    # 自己已经死了：装填条归满没有意义
    assert _ids(
        [
            (_frame(me=_me(reload_ratio=0.2), seq=1), 0.0),
            (_frame(me=_me(reload_ratio=1.0, alive=False), seq=2), 1.0),
        ]
    ) == []


def test_reload_done_never_guesses_when_the_field_is_missing():
    for broken in (None, "1.0", True, {"ratio": 1.0}):
        assert _ids(
            [
                (_frame(me=_me(reload_ratio=0.2), seq=1), 0.0),
                (_frame(me=_me(reload_ratio=broken), seq=2), 1.0),
            ]
        ) == [], broken


# ------------------------------------------------------------ ② hp_critical
def test_hp_critical_once_per_dip_then_rearms_above_035():
    produced = _ids(
        [
            (_frame(me=_me(health_ratio=0.9), seq=1), 0.0),
            (_frame(me=_me(health_ratio=0.2), seq=2), 1.0),    # 首次跌破 → 报
            (_frame(me=_me(health_ratio=0.22), seq=3), 2.0),   # 还在线下 → 不重复
            (_frame(me=_me(health_ratio=0.3), seq=4), 3.0),    # 0.3 不构成"重新上膛"
            (_frame(me=_me(health_ratio=0.2), seq=5), 4.0),    # 于是这里也不报
            (_frame(me=_me(health_ratio=0.4), seq=6), 5.0),    # 回到 0.35 以上 → 上膛
            (_frame(me=_me(health_ratio=0.2), seq=7), 6.0),    # 再报一次
        ]
    )
    assert produced == ["hp_critical", "hp_critical"]


def test_hp_critical_boundaries():
    # 0.25 本身不算"跌破"
    assert _ids(
        [(_frame(me=_me(health_ratio=0.9), seq=1), 0.0), (_frame(me=_me(health_ratio=0.25), seq=2), 1.0)]
    ) == []
    # 0.2499 算
    assert _ids(
        [(_frame(me=_me(health_ratio=0.9), seq=1), 0.0), (_frame(me=_me(health_ratio=0.2499), seq=2), 1.0)]
    ) == ["hp_critical"]
    # 血归零那一下不说（那句交给 you_died）
    assert _ids(
        [(_frame(me=_me(health_ratio=0.9), seq=1), 0.0), (_frame(me=_me(health_ratio=0.0), seq=2), 1.0)]
    ) == []
    # 已经死了：不说
    assert _ids(
        [
            (_frame(me=_me(health_ratio=0.9), seq=1), 0.0),
            (_frame(me=_me(health_ratio=0.1, alive=False), seq=2), 1.0),
        ]
    ) == []
    # 字段缺失 / 类型不对：不猜
    for broken in (None, "0.2", False):
        assert _ids(
            [(_frame(me=_me(health_ratio=0.9), seq=1), 0.0), (_frame(me=_me(health_ratio=broken), seq=2), 1.0)]
        ) == [], broken


def test_hp_critical_is_queued_then_spoken():
    """血线告急被全局间隔压住时**排队**（v2.4），不插队也不丢。

    v2.12：``hp_critical`` 归 critical 档，打断**开启**时它会无视间隔当场
    说出来（那是"打断"的定义）。要验"排队 + 补说"这条路径，必须把打断关掉 ——
    关掉之后 critical 也排队（``_enqueue(force=True)``，无视 DEFERRABLE）。
    """
    arbiter = arbiter_mod.Arbiter(min_gap_s=8.0, interrupt_enabled=False)

    assert arbiter.observe(_frame(me=_me(health_ratio=0.9), seq=1), 0.0) is None
    spoke = arbiter.observe(
        _frame(me=_me(health_ratio=0.9), events=[_event(1, "you_killed")], seq=2), 1.0
    )
    assert spoke is not None  # 这一句占住了 8 秒全局间隔
    # 间隔里跌破 0.25：这一帧说不出来，但也不许丢
    assert arbiter.observe(_frame(me=_me(health_ratio=0.2), seq=3), 2.0) is None
    assert [item[0] for item in arbiter._queue] == ["hp_critical"], "关掉打断 = 排队，不抢话"
    # 间隔一过（1.0 + 8）：补说那句"血不多了"
    text = arbiter.observe(_frame(me=_me(health_ratio=0.2), seq=4), 9.5)
    assert text == "血不多了，{MASTER_NAME}，收着点打。"


# ----------------------------------------------------------- ③ enemy_killed
def test_enemy_killed_reports_n_and_left():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0, team_events_enabled=True)
    arbiter._derive(_frame(team=_team(enemy_alive=15), seq=1), 0.0)

    one = arbiter._derive(_frame(team=_team(enemy_alive=14), seq=2), 1.0)
    assert [event["id"] for event in one] == ["enemy_killed"]
    assert one[0]["payload"] == {"n": 1, "left": 14}

    two = arbiter._derive(_frame(team=_team(enemy_alive=12), seq=3), 2.0)
    assert two[0]["payload"] == {"n": 2, "left": 12}


def test_enemy_killed_ignores_rises_and_missing_counts():
    # 计数上涨（新一局 / 读错了）：不报
    assert _ids(
        [(_frame(team=_team(enemy_alive=14), seq=1), 0.0), (_frame(team=_team(enemy_alive=15), seq=2), 1.0)]
    ) == []
    # team 缺失 / 字段为 None：不报也不抛
    assert _ids([(_frame(team=None, seq=1), 0.0), (_frame(team={}, seq=2), 1.0)]) == []
    assert _ids(
        [
            (_frame(team=_team(enemy_alive=15), seq=1), 0.0),
            (_frame(team={"enemy_alive": None}, seq=2), 1.0),
            (_frame(team={"enemy_alive": "14"}, seq=3), 2.0),
        ]
    ) == []
    # 结算后 team 被清零：不在局内就不比较，免得说成"一口气干掉 15 个"
    assert _ids(
        [
            (_frame(team=_team(enemy_alive=15), seq=1), 0.0),
            (_frame(team=_team(enemy_alive=0), seq=2, mode="battle_end"), 1.0),
        ]
    ) == []


def test_team_loss_wording():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0, team_events_enabled=True)
    assert arbiter.observe(_frame(team=_team(enemy_alive=15), seq=1), 0.0) is None

    one = arbiter.observe(_frame(team=_team(enemy_alive=14), seq=2), 1.0)
    assert one == "干掉一个。"

    # 同类冷却 4 秒；一次性掉两个换个说法
    many = arbiter.observe(_frame(team=_team(enemy_alive=12), seq=3), 6.0)
    assert many == "一口气干掉 2 个。"


def test_team_loss_never_speaks_an_absolute_remaining_count():
    """实测：15v15 的局报出"还剩 1 个"。

    ``team.enemy_alive`` 只覆盖 AoI 内可见车，总数不可知（雾战下我方/敌方
    编队数同样不可靠），所以哪怕这一帧的 ``enemy_total`` 明显不完整、``left``
    只有 1，播报文本里也不许出现"还剩"。

    ⚠️ v1.30 起队伍事件默认静音、打开后也只占 info 档（同帧有 warning
    档句子时会让位），所以这里直接测**渲染口径**（``_render``），不走仲裁 ——
    本用例只锁"话术里不许出现还剩"。
    """
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0, team_events_enabled=True)
    thin = {"enemy_alive": 2, "enemy_total": 2, "ally_alive": 2, "ally_total": 2}

    enemy_said = arbiter._render(
        {"id": "enemy_killed", "payload": {"n": 1, "left": 1}},
        _frame(team=dict(thin, enemy_alive=1), seq=1),
    )
    assert enemy_said == "干掉一个。"
    assert re.search("还剩|剩 \\d", enemy_said) is None, enemy_said

    ally_said = arbiter._render(
        {"id": "ally_killed", "payload": {"n": 1, "left": 1}},
        _frame(team=dict(thin, ally_alive=1), seq=2),
    )
    assert ally_said == "我们这边少了一个。"
    assert re.search("还剩|剩 \\d", ally_said) is None, ally_said

    # 一次掉两个的复数分支同样只报 n
    many = arbiter._render(
        {"id": "ally_killed", "payload": {"n": 3, "left": 1}},
        _frame(team=dict(thin, ally_alive=1), seq=4),
    )
    assert many == "我们少了 3 个。"
    assert re.search("还剩|剩 \\d", many) is None, many


# ------------------------------------------------------------ ④ ally_killed
def test_ally_killed_speaks_when_i_am_alive():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0, team_events_enabled=True)
    assert arbiter.observe(_frame(team=_team(ally_alive=15), seq=1), 0.0) is None
    text = arbiter.observe(_frame(team=_team(ally_alive=14), seq=2), 1.0)
    assert text == "我们这边少了一个。"


def test_ally_killed_is_silent_on_my_own_death_frame():
    """自己死那一帧 ally_alive 也会掉 —— 那句必须让给 you_died。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0, team_events_enabled=True)
    arbiter._derive(_frame(team=_team(ally_alive=15), seq=1), 0.0)

    produced = arbiter._derive(_frame(team=_team(ally_alive=14), me=_me(alive=False), seq=2), 1.0)
    assert produced == [], "自己阵亡时不必再报 ally_killed"


def test_own_death_says_you_died_only():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    assert arbiter.observe(_frame(team=_team(ally_alive=15), seq=1), 0.0) is None

    text = arbiter.observe(
        _frame(
            team=_team(ally_alive=14),
            me=_me(alive=False),
            events=[_event(1, "you_died")],
            seq=2,
        ),
        1.0,
    )
    assert text is not None
    assert "被击毁" in text
    assert "少了一个" not in text


def test_ally_killed_does_not_guess_when_alive_is_missing():
    assert _ids(
        [
            (_frame(team=_team(ally_alive=15), seq=1), 0.0),
            (_frame(team=_team(ally_alive=14), me={"alive": None}, seq=2), 1.0),
            (_frame(team=_team(ally_alive=13), me={"alive": "yes"}, seq=3), 2.0),
        ]
    ) == []


# ----------------------------------------------------------- ⑤ fire_cleared
def test_fire_cleared_on_edge_and_wording():
    assert _ids(
        [(_frame(me=_me(fires=1), seq=1), 0.0), (_frame(me=_me(fires=0), seq=2), 1.0)]
    ) == ["fire_cleared"]
    # 一直是 0：没有边沿
    assert _ids(
        [(_frame(me=_me(fires=0), seq=1), 0.0), (_frame(me=_me(fires=0), seq=2), 1.0)]
    ) == []
    # 两处火一起灭也算灭了
    assert _ids(
        [(_frame(me=_me(fires=2), seq=1), 0.0), (_frame(me=_me(fires=0), seq=2), 1.0)]
    ) == ["fire_cleared"]
    # 字段缺失：不猜
    assert _ids(
        [(_frame(me=_me(fires=1), seq=1), 0.0), (_frame(me=_me(fires=None), seq=2), 1.0)]
    ) == []

    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    assert arbiter.observe(_frame(me=_me(fires=1), seq=1), 0.0) is None
    assert arbiter.observe(_frame(me=_me(fires=0), seq=2), 1.0) == "火灭了，缓口气。"


# ------------------------------------------------------------- ⑥ suppressed
def test_suppressed_crossfire_with_health_drop():
    """6 秒内两下、方向夹角 90°、掉了 17% 血 → 被压制。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    assert arbiter.observe(_frame(me=_me(health_ratio=0.95), seq=1), 0.0) is None
    arbiter.observe(
        _frame(me=_me(health_ratio=0.9), events=[_hit(1, yaw=0.0)], seq=2), 1.0
    )
    text = arbiter.observe(
        _frame(me=_me(health_ratio=0.78), events=[_hit(2, yaw=90.0)], seq=3), 4.0
    )
    assert text == SUPPRESSED_LINE


def test_suppressed_requires_two_hits():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    assert arbiter.observe(_frame(me=_me(health_ratio=0.95), seq=1), 0.0) is None
    text = arbiter.observe(
        _frame(me=_me(health_ratio=0.7), events=[_hit(1, yaw=0.0)], seq=2), 1.0
    )
    assert "被盯上" not in (text or ""), "单次受击不是被夹"


def test_suppressed_requires_directions_to_differ():
    """两下都来自同一个方向（夹角 20°）是磨血，不叫被压制。"""
    assert _ids(
        [
            (_frame(me=_me(health_ratio=0.95), seq=1), 0.0),
            (_frame(me=_me(health_ratio=0.9), events=[_hit(1, yaw=0.0)], seq=2), 1.0),
            (_frame(me=_me(health_ratio=0.75), events=[_hit(2, yaw=20.0)], seq=3), 2.0),
        ]
    ) == []


def test_suppressed_requires_health_drop():
    """方向分开了但血量没掉够 10%（比如都被跳弹）：不报。"""
    assert _ids(
        [
            (_frame(me=_me(health_ratio=0.95), seq=1), 0.0),
            (_frame(me=_me(health_ratio=0.94), events=[_hit(1, yaw=0.0)], seq=2), 1.0),
            (_frame(me=_me(health_ratio=0.90), events=[_hit(2, yaw=90.0)], seq=3), 2.0),
        ]
    ) == []


def test_suppressed_degrades_when_hit_direction_is_missing():
    """写端没给 hitDirYaw → 只按"次数 + 掉血"判（降级路径），依然会说。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    assert arbiter.observe(_frame(me=_me(health_ratio=0.95), seq=1), 0.0) is None
    arbiter.observe(_frame(me=_me(health_ratio=0.88), events=[_hit(1)], seq=2), 1.0)
    text = arbiter.observe(_frame(me=_me(health_ratio=0.75), events=[_hit(2)], seq=3), 3.0)
    assert text == SUPPRESSED_LINE


def test_suppressed_respects_its_30_second_cooldown():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    assert arbiter.observe(_frame(me=_me(health_ratio=0.98), seq=1), 0.0) is None
    arbiter.observe(_frame(me=_me(health_ratio=0.88), events=[_hit(1, yaw=0.0)], seq=2), 1.0)
    assert (
        arbiter.observe(_frame(me=_me(health_ratio=0.70), events=[_hit(2, yaw=90.0)], seq=3), 3.0)
        == SUPPRESSED_LINE
    )
    # 冷却内的第三下：条件都满足，但不许再念
    later = arbiter.observe(
        _frame(me=_me(health_ratio=0.55), events=[_hit(3, yaw=180.0)], seq=4), 5.0
    )
    assert "被盯上" not in (later or "")


# ------------------------------------------------- 兜底：没数据 / 不改输入
def test_all_state_fields_none_is_silent_and_safe():
    blank = state_reader.Frame(
        status=state_reader.STATUS_OK,
        schema=1,
        seq=1,
        ts=1000.0,
        mode="battle",
        arena_id="arena-1",
        me={"health_ratio": None, "reload_ratio": None, "fires": None, "alive": None},
        team={"enemy_alive": None, "ally_alive": None},
        result=None,
        events=(),
    )
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    assert arbiter.observe(blank, 0.0) is None
    assert arbiter.observe(replace(blank, seq=2), 1.0) is None

    bare = replace(blank, seq=3, mode=None, me=None, team=None)
    assert arbiter.observe(bare, 2.0) is None

    junk = replace(
        blank,
        seq=4,
        me={"health_ratio": "0.2", "reload_ratio": "1.0", "fires": True, "alive": "yes"},
        team={"enemy_alive": "14", "ally_alive": 14.5},
    )
    assert arbiter.observe(junk, 3.0) is None


def test_derived_layer_never_mutates_the_frame():
    me = _me(health_ratio=0.2, reload_ratio=1.0, fires=0)
    team = _team(enemy_alive=14)
    before_me, before_team = dict(me), dict(team)
    frame = _frame(me=me, team=team, events=[_event(1, "you_damaged")], seq=2)

    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    arbiter._derive(_frame(me=_me(reload_ratio=0.2), team=_team(enemy_alive=15), seq=1), 0.0)
    arbiter.observe(frame, 1.0)

    assert me == before_me
    assert team == before_team
    assert frame.events == (_event(1, "you_damaged"),)


def test_derived_seq_does_not_look_like_a_data_restart():
    """合成事件的 seq 只当渲染种子，绝不能进 ``_max_event_seq`` 的重启判定。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0, team_events_enabled=True)
    arbiter.observe(_frame(me=_me(reload_ratio=0.2, fires=1), team=_team(), seq=1), 0.0)

    loaded = _frame(
        me=_me(health_ratio=0.2, reload_ratio=1.0, fires=0),
        team=_team(enemy_alive=14, ally_alive=14),
        events=[_event(3, "you_killed")],
        seq=2,
    )
    assert arbiter.observe(loaded, 1.0) is not None
    assert arbiter._derived_seq >= 5, "这一帧该同时派生出装填/血线/战况等多条"
    assert arbiter._max_seq_seen == 3, "见过的最大 seq 只能来自真实事件"

    # 真实 seq 正常推进 → 不许被判成"数据端重启"（那样会静默吞掉新事件）
    fresh = _frame(events=[_event(3, "you_killed"), _event(4, "you_respawned")], seq=3)
    assert arbiter.observe(fresh, 2.0) is not None
    assert arbiter._max_seq_seen == 4


def test_battle_change_clears_derived_baselines():
    """换局后第一帧只建基线：绝不能拿上一局的敌我计数当参照。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    assert arbiter.observe(_frame(team=_team(enemy_alive=3), seq=1, arena="arena-a"), 0.0) is None

    text = arbiter.observe(
        _frame(team=_team(enemy_alive=15), seq=2, arena="arena-b", events=[_event(1, "battle_start")]),
        30.0,
    )
    assert text is not None
    assert "开打了" in text
    assert "干掉" not in text
