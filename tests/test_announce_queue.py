"""播报间隔闸 + 排队（v2.4）：同一秒连出两条太吵。

实机 dry-run：
``还有八百四十血。`` 和 ``血不多了，收着点打。`` 挤在同一秒里。开车时听不清。

规矩（v2.4）：
* 任意 ``min_gap_s``（默认 2 秒）内最多一条播报；
* 被拦下的那句**不丢**，进有界队列等下一次机会，过了时效（默认 4 秒）才扔；
* ``you_died`` / ``battle_end`` 仍然插队，但插队同样记时（后面还得等满间隔）；
* "每局一次"的额度（``hp_critical``）没说出来就不算用掉。
"""

from __future__ import annotations

from collections import deque

import sdk_stub

_pkg = sdk_stub.load_plugin_module()
arbiter_mod = _pkg.arbiter
state_reader = _pkg.state_reader

HP_LINE = "血不多了，{MASTER_NAME}，收着点打。"


# --------------------------------------------------------------------- 造帧
def _me(**overrides) -> dict:
    me = {"vehicle_name": "WZ-219", "health_ratio": 0.9, "alive": True}
    me.update(overrides)
    return me


def _frame(events=(), *, me=None, seq=1):
    return state_reader.Frame(
        status=state_reader.STATUS_OK,
        schema=1,
        seq=seq,
        ts=1000.0,
        mode="battle",
        arena_id="arena-1",
        me=_me() if me is None else me,
        result=None,
        events=tuple(events),
    )


def _event(seq: int, event_id: str, payload: dict | None = None) -> dict:
    return {"seq": seq, "id": event_id, "ts": 1000.0, "payload": payload or {}}


def _assisted(seq: int, total: int = 340) -> dict:
    return _event(seq, "you_assisted", {"total": total, "delta": 60})


# ------------------------------------------------- ① 间隔 / ② 排队 / ③ 时效
def test_three_in_one_second_speaks_one_and_queues_two():
    """同一秒连来三条：只说第一条，另外两条进队列（不是丢弃）。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=2.0)

    assert arbiter.observe(_frame([_event(1, "you_killed")], seq=1), 0.0) is not None
    assert arbiter.observe(_frame([_assisted(2)], seq=2), 0.0) is None
    assert arbiter.observe(
        _frame([_event(3, "you_damaged", {"health": 1500, "max_health": 1900, "delta": 400})], seq=3),
        0.0,
    ) is None

    assert [item[0] for item in arbiter._queue] == ["you_assisted", "you_damaged"]
    assert len(arbiter._queue) <= arbiter_mod.PENDING_MAX


def test_queued_line_is_spoken_when_the_gap_opens():
    """间隔一过，排队里那条（时效内、优先级高的）先说，哪怕这一帧什么新事件都没有。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=2.0)

    assert arbiter.observe(_frame([_event(1, "you_killed")], seq=1), 0.0) is not None
    assert arbiter.observe(_frame([_assisted(2)], seq=2), 0.0) is None
    assert arbiter.observe(_frame([], seq=3), 1.9) is None, "间隔没到就不许说"

    text = arbiter.observe(_frame([], seq=4), 2.0)
    assert text is not None
    assert "三百四十" in text
    assert arbiter._queue == deque()


def test_stale_queued_line_is_dropped():
    """过了时效（4 秒）还没机会说 → 扔掉。旧战况晚十几秒再说只会让人莫名其妙。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=2.0)

    assert arbiter.observe(_frame([_event(1, "you_killed")], seq=1), 0.0) is not None
    assert arbiter.observe(_frame([_assisted(2)], seq=2), 0.0) is None
    assert arbiter._queue, "前提：这句确实排上了"

    # 一直没人给它机会，直到时效过去（4 秒）
    assert arbiter.observe(_frame([], seq=3), 4.5) is None
    assert arbiter._queue == deque()


# ------------------------------------------------------------- ④ 插队与延时
def test_you_died_cuts_in_and_the_next_line_still_waits():
    """救命的那句插队，但插队也要记时：后面那句照样得等满 2 秒。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=2.0)

    assert arbiter.observe(_frame([_event(1, "you_killed")], seq=1), 0.0) is not None
    died = arbiter.observe(_frame([_event(2, "you_died")], seq=2, me=_me(alive=False)), 1.0)
    assert died is not None
    assert "被击毁" in died, "you_died 必须在间隔期内插队说出来"

    # 插队那一刻重新计时：1.0 + 2.0 之前一个字都不许说
    assert arbiter.observe(_frame([_assisted(3)], seq=3), 2.0) is None
    assert arbiter.observe(_frame([], seq=4), 2.9) is None
    assert len(arbiter._queue) == 1, "被拦下的那句应该还在队列里"

    text = arbiter.observe(_frame([], seq=5), 3.0)
    assert text is not None
    assert "三百四十" in text


# ------------------------------------------------------- ⑤ "每局一次"的额度
def test_hp_critical_dropped_from_queue_keeps_its_once_per_battle_quota():
    """排队里被扔掉的 hp_critical 不算说过话 —— 额度必须还回去。

    v2.12 改了两个前提（结论没变）：
    * 触发条件换成 ``interrupt_enabled=False``：``hp_critical`` 归 critical 档，打断
      **开启**时它无视间隔当场说、根本不进队列，"排队里被扔掉"这条路径就走不到；
    * 扔掉的方式换成"过时效"：关掉打断后 ``you_died`` 本身也只是排队、不再清队，
      所以原来那条"被 you_died 清队"的路径不再可达。
    """
    arbiter = arbiter_mod.Arbiter(min_gap_s=2.0, interrupt_enabled=False)

    assert arbiter.observe(_frame(seq=1), 0.0) is None                      # 基线 0.9
    assert arbiter.observe(_frame([_event(1, "you_killed")], seq=2), 0.5) is not None
    # 间隔里跌破 0.25 → 关掉打断，只能排队
    assert arbiter.observe(_frame(seq=3, me=_me(health_ratio=0.2)), 1.0) is None
    assert arbiter._hp_armed is False, "前提：触发那一刻额度被划掉了"
    assert [item[0] for item in arbiter._queue] == ["hp_critical"]

    # 一直没机会说，过了时效（1.0 + 4 秒）→ 扔掉，额度还回去
    assert arbiter.observe(_frame(seq=4, me=_me(health_ratio=0.2)), 5.5) is None
    assert arbiter._queue == deque()
    assert arbiter._hp_armed is True, "没说出来 = 没发生，额度必须还回去"

    # 血还在红线上：额度回来了 → 这句重新触发（排队），间隔一到就补说
    assert arbiter.observe(_frame(seq=5, me=_me(health_ratio=0.2)), 6.0) is None
    text = arbiter.observe(_frame(seq=6, me=_me(health_ratio=0.2)), 6.5)
    assert text == HP_LINE

