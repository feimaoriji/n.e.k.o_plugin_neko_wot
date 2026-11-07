"""/ v2.12：每帧判定顺序（G2 五步）+ ``last_cue`` 快照。

这一批用例只守**仲裁行为**（档位表本身由 ``test_event_levels.py`` 锁）：

1. ``observe()`` 一帧最多一条，路径是 critical → warning → info 顺序；
2. critical 由 ``interrupt_enabled`` 决定"无视 min_gap 当场说"还是"排队不抢话"，
   但**同类冷却照旧生效**（它只保留"无视全局间隔 + 可打断"两个特权）；
3. warning 受 ``min_gap_s``，被拦按 ``DEFERRABLE`` + ``pending_ttl_s`` 排队；
4. info 受 ``idle_fill_s``（默认 0.0 = 不管闲），被拦**直接丢**、不进队列；
5. 每说出一句都更新只读快照 ``last_cue``（event_id / level / text / priority），
   而 ``observe()`` 的返回类型仍然是 ``str | None``。
"""

from __future__ import annotations

from collections import deque

import sdk_stub

_pkg = sdk_stub.load_plugin_module()
arbiter_mod = _pkg.arbiter
state_reader = _pkg.state_reader


# --------------------------------------------------------------------- 造帧
def _me(**overrides) -> dict:
    me = {"vehicle_name": "WZ-219", "health_ratio": 0.9, "alive": True}
    me.update(overrides)
    return me


def _frame(events=(), *, me=None, seq=1, arena="arena-1"):
    return state_reader.Frame(
        status=state_reader.STATUS_OK,
        schema=1,
        seq=seq,
        ts=1000.0,
        mode="battle",
        arena_id=arena,
        me=_me() if me is None else me,
        result=None,
        events=tuple(events),
    )


def _event(seq: int, event_id: str, payload: dict | None = None) -> dict:
    return {"seq": seq, "id": event_id, "ts": 1000.0, "payload": payload or {}}


# ------------------------------------------------------- ⓪ 构造参数与只读快照
def test_constructor_defaults_keep_the_arbiter_bare():
    """仲裁器默认"管闲"和"打断"都开，但空闲填空闸是 0.0（生产默认值在配置层）。

    ``idle_fill_s`` 默认必须是 0.0：既有几十条用例构造完立刻断言 info 档（装填好了 /
    开场白）说得出来，默认 10.0 会把它们集体打红。生产默认的 10 秒由
    ``settings.DEFAULTS["idle_fill_s"]`` + ``startup()`` 接线提供。
    """
    arbiter = arbiter_mod.Arbiter()

    assert arbiter.interrupt_enabled is True
    assert arbiter.idle_fill_s == 0.0
    assert arbiter.last_cue is None, "还没说过话 → 快照是 None"


def test_last_cue_records_all_four_fields_on_a_critical_line():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    text = arbiter.observe(_frame([_event(1, "you_died")], me=_me(alive=False)), 0.0)

    assert isinstance(text, str), "observe() 的返回类型仍是 str | None"
    cue = arbiter.last_cue
    assert cue is not None
    assert cue.event_id == "you_died"
    assert cue.level == arbiter_mod.LEVEL_CRITICAL
    assert cue.text == text
    assert cue.priority == arbiter_mod.LEVEL_PRIORITY[arbiter_mod.LEVEL_CRITICAL] == 90


def test_last_cue_follows_every_spoken_line():
    """快照跟着"最近一次说出口"走，且只读（NamedTuple，改不动）。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert arbiter.observe(_frame([_event(1, "module_damaged")], seq=1), 0.0) is not None
    assert arbiter.last_cue.event_id == "module_damaged"
    assert arbiter.last_cue.level == arbiter_mod.LEVEL_WARNING
    assert arbiter.last_cue.priority == 50

    assert arbiter.observe(_frame([_event(2, "fire_out")], seq=2), 20.0) is not None
    assert arbiter.last_cue.event_id == "fire_out"
    assert arbiter.last_cue.level == arbiter_mod.LEVEL_INFO
    assert arbiter.last_cue.priority == 10

    try:
        arbiter.last_cue.priority = 0
    except AttributeError:
        pass
    else:  # pragma: no cover - 只有把 NamedTuple 换成可变对象才会走到
        raise AssertionError("last_cue 必须是只读结构（frozen dataclass / NamedTuple）")


# ------------------------------------------------------------ ① 三档各自的出口
def test_critical_line_is_spoken():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    text = arbiter.observe(_frame([_event(1, "on_fire")]), 0.0)

    assert text is not None and "着火" in text
    assert arbiter.last_cue.level == arbiter_mod.LEVEL_CRITICAL


def test_warning_line_is_spoken():
    """v2.12 补登的三个 warning id 真的能走到仲裁器、并作为 warning 说出来。

    （``module_damaged`` / ``crew_dead`` / ``ammo_low`` 在 ``data/phrases.json`` 里
    一直有话术，只是 建表时漏了档位。）
    """
    for event_id, marker in (("module_damaged", "模块"), ("crew_dead", "车组"), ("ammo_low", "弹药")):
        arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

        text = arbiter.observe(_frame([_event(1, event_id)]), 0.0)

        assert text is not None and marker in text, event_id
        assert arbiter.last_cue.event_id == event_id
        assert arbiter.last_cue.level == arbiter_mod.LEVEL_WARNING


def test_info_line_is_spoken():
    """``fire_out`` 是 v2.12 补登的 id，归 info（"松口气"的机制回执）。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    text = arbiter.observe(_frame([_event(1, "fire_out")]), 0.0)

    assert text is not None and "火灭" in text
    assert arbiter.last_cue.level == arbiter_mod.LEVEL_INFO


# --------------------------------------------------------- ② 跨档：高档压低档
def test_critical_beats_warning_and_info_in_the_same_frame():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    frame = _frame(
        [
            _event(1, "reload_done"),
            _event(2, "you_killed"),
            _event(3, "you_died"),
        ],
        me=_me(alive=False),
    )

    text = arbiter.observe(frame, 0.0)

    assert text is not None and "被击毁" in text
    assert "击毁一台" not in text
    assert "上膛" not in text
    assert arbiter.last_cue.event_id == "you_died"


def test_warning_beats_info_in_the_same_frame():
    """warning 的档内数字（``crew_dead`` 走默认 10）比 info 的 ``reload_done``(12) 低，
    但档位高就是先说 —— 跨档不比数字。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    frame = _frame([_event(1, "reload_done"), _event(2, "crew_dead")])

    text = arbiter.observe(frame, 0.0)

    assert text is not None and "车组" in text
    assert "上膛" not in text
    assert arbiter.last_cue.level == arbiter_mod.LEVEL_WARNING


# ----------------------------------------------------- ③ critical 的打断开关
def test_interrupt_on_lets_critical_ignore_min_gap():
    arbiter = arbiter_mod.Arbiter(min_gap_s=10.0, interrupt_enabled=True)

    assert arbiter.observe(_frame([_event(1, "you_killed")], seq=1), 0.0) is not None
    died = arbiter.observe(_frame([_event(2, "you_died")], seq=2, me=_me(alive=False)), 1.0)

    assert died is not None and "被击毁" in died, "打断开启：critical 不等间隔"
    assert arbiter._last_spoken_at == 1.0, "插队也要记时（后面照样得等满间隔）"


def test_interrupt_off_queues_critical_instead_of_cutting_in():
    arbiter = arbiter_mod.Arbiter(min_gap_s=10.0, interrupt_enabled=False)

    assert arbiter.observe(_frame([_event(1, "you_killed")], seq=1), 0.0) is not None
    assert arbiter.observe(_frame([_event(2, "on_fire")], seq=2), 1.0) is None, "关掉打断就不许抢话"
    assert [item[0] for item in arbiter._queue] == ["on_fire"], "critical 也必须进队列（不许丢）"

    # 间隔一到就补说，而且它一定排在排队里的 warning 之前（按档位补说）
    assert arbiter.observe(_frame([_event(3, "you_assisted", {"total": 340, "delta": 60})], seq=3), 2.0) is None
    text = arbiter.observe(_frame([], seq=4), 10.5)
    assert text is not None and "着火" in text
    assert arbiter.last_cue.event_id == "on_fire"


def test_critical_still_respects_its_own_type_cooldown():
    """critical 只有两个特权（无视全局间隔 + 可打断），**不保留无限次**。

    ``on_fire`` 的同类冷却是 15 秒：间隔为 0 也一样不许连着说。
    """
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0, interrupt_enabled=True)

    assert arbiter.observe(_frame([_event(1, "on_fire")], seq=1), 0.0) is not None
    assert arbiter.observe(_frame([_event(2, "on_fire")], seq=2), 5.0) is None, "同类冷却 15 秒"
    assert arbiter.observe(_frame([_event(3, "on_fire")], seq=3), 15.0) is not None


# ------------------------------------------------------- ④ warning 受全局间隔
def test_warning_is_blocked_by_min_gap_and_queued():
    arbiter = arbiter_mod.Arbiter(min_gap_s=10.0)

    assert arbiter.observe(_frame([_event(1, "you_killed")], seq=1), 0.0) is not None
    assert arbiter.observe(_frame([_event(2, "you_assisted", {"total": 340, "delta": 60})], seq=2), 1.0) is None
    assert [item[0] for item in arbiter._queue] == ["you_assisted"]

    text = arbiter.observe(_frame([], seq=3), 10.0)
    assert text is not None and "三百四十" in text
    assert arbiter.last_cue.event_id == "you_assisted"


# ------------------------------------------------------ ⑤ info 的空闲填空闸
def test_info_is_held_back_while_the_plugin_was_just_talking():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0, idle_fill_s=10.0)

    assert arbiter.observe(_frame([_event(1, "you_killed")], seq=1), 0.0) is not None
    text = arbiter.observe(_frame([_event(2, "reload_done")], seq=2), 5.0)

    assert text is None, "刚说过话（5 秒 < 空闲闸 10 秒），info 不许补位"
    assert arbiter._queue == deque(), "info 被拦下直接丢，不进队列"


def test_info_fills_the_gap_once_idle_long_enough():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0, idle_fill_s=10.0)

    assert arbiter.observe(_frame([_event(1, "you_killed")], seq=1), 0.0) is not None
    assert arbiter.observe(_frame([_event(2, "reload_done")], seq=2), 9.9) is None
    text = arbiter.observe(_frame([_event(3, "reload_done")], seq=3), 10.0)

    assert text is not None and "上膛" in text
    assert arbiter.last_cue.level == arbiter_mod.LEVEL_INFO


# ------------------------------------------------------------ ⑥ 一帧最多一条
def test_one_frame_never_produces_two_lines():
    """同帧三档齐全：只有一条出口，且下一帧没有新事件时不会再补一句。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    frame = _frame(
        [_event(1, "reload_done"), _event(2, "you_killed"), _event(3, "you_died")],
        me=_me(alive=False),
    )

    text = arbiter.observe(frame, 0.0)

    assert isinstance(text, str)
    hits = sum(marker in text for marker in ("被击毁", "击毁一台", "上膛"))
    assert hits == 1, f"一帧只许一条（实际命中 {hits} 条候选）：{text!r}"
    assert arbiter.observe(_frame([], seq=2), 0.0) is None, "同一帧不许接着补第二条"
