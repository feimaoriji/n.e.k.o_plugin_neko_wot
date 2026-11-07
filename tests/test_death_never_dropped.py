"""问题 B：一连串事件时播报旧的 → 最后的阵亡没播（实机）。

玩家原话：「有时一连串事件可能会播报旧的，导致最后的死亡没播报」。
两条**互相独立**的根因，各有一组用例（拆掉任意一条，本文件都会红）：

§一 仲裁层 —— ``you_died`` 的同类冷却是**时间**闸（10 秒），而阵亡是**边沿**事件：
    重生模式（巴比伦）一局会死 2~3 次，第二次落在 10 秒内就被 ``_pick`` 静默跳过；
    被跳过的那一帧顺势去补说队列里那句旧的（``you_assisted``）—— 玩家听到的是
    "协助伤害攒到九百了"，阵亡一句都没有。

§二 通道层 —— 整局共用一个 ``coalesce_key``，而宿主的合并规则是
    "同 key 新的一句**直接把还没出口的旧句子丢掉**，priority 不参与"
    （``main_logic/proactive_delivery.py::submit``）：
    实机 21:33:10 阵亡 cue（priority=90）提交后 11 秒没轮到出口，
    21:33:21 结算 cue（priority=90，同一个 key）一到就把阵亡**整条删掉**，
    21:33:25 只出口了那条 109 字的结算 —— 这就是"最后的死亡没播报"。

两条的修法都只许"往前提/不许丢"：锚点（``GAP_BYPASS``）不吃同类冷却、用**身份**
（写端 payload 的 ``deaths``）去重，并且各用一把自己的 coalesce key。
"""

from __future__ import annotations

import itertools
import json
import time
from collections import deque

import sdk_stub

_pkg = sdk_stub.load_plugin_module()
arbiter_mod = _pkg.arbiter
settings_mod = _pkg.settings
plugin_mod = _pkg
state_reader = _pkg.state_reader

# 实机生产参数（<app_data>\plugins\neko_wot\config\plugin.toml）：
# min_gap_s = 2.0 / idle_fill_s = 30.0 / interrupt_enabled = true。
PROD = {"min_gap_s": 2.0, "idle_fill_s": 30.0}


# --------------------------------------------------------------------- 造帧
def _team(enemy_alive: int = 15, ally_alive: int = 15) -> dict:
    return {
        "enemy_alive": enemy_alive,
        "enemy_total": 15,
        "ally_alive": ally_alive,
        "ally_total": 15,
        "enemy_total_known": True,
    }


def _me(*, health: float = 0.9, alive: bool = True) -> dict:
    return {"vehicle_name": "WZ-219", "health_ratio": health, "alive": alive, "reload_ratio": 0.5, "fires": 0}


def _frame(
    seq: int,
    *,
    events=(),
    health: float = 0.9,
    alive: bool = True,
    mode: str = "battle",
    enemy_alive: int = 15,
    ally_alive: int = 15,
):
    return state_reader.Frame(
        status=state_reader.STATUS_OK,
        schema=1,
        seq=seq,
        ts=1000.0,
        mode=mode,
        arena_id="arena-babylon",
        me=_me(health=health, alive=alive),
        team=_team(enemy_alive, ally_alive),
        arena={"period": 2, "time_left": 300},
        mode_kind="standard",
        events=tuple(events),
    )


_EVENT_SEQ = itertools.count(1)


def _event(event_id: str, payload: dict | None = None) -> dict:
    """造一条 mod 事件。

    ``seq`` 必须**单调递增**：``Arbiter._restarted`` 把"最大 seq 变小"判成数据端重启，
    那一帧会被静默 prime（连 ``_seen`` 一起清）。用例里如果乱编 seq，测到的就不是
    要测的那条路径，而是重启兜底那条 —— 第一版用例就是这么假绿过一次。
    """
    return {"seq": next(_EVENT_SEQ), "id": event_id, "ts": 1000.0, "payload": payload or {}}


def _death(seq: int, deaths: int | None = 1, **kwargs) -> dict:
    payload = {} if deaths is None else {"deaths": deaths}
    return _frame(seq, events=[_event("you_died", payload)], health=0.0, alive=False, mode="dead", **kwargs)


# =================================================== §一 仲裁层：阵亡不许无声丢弃
def test_a_second_death_inside_the_ten_second_window_is_still_spoken():
    """复现（红）：一连串事件 + 6 秒内的第二次阵亡 → 播报了旧话、阵亡没播。

    时间线照一局重生模式（一局死两次）的实际节奏：
    第一次阵亡在 3.0s 说出口；复活后 6.0s 掉血、7.0s 一条协助（被间隔拦下进队列），
    8.5s 第二次阵亡 —— 它落在 ``you_died`` 的 10 秒同类冷却里。
    """
    arbiter = arbiter_mod.Arbiter(**PROD)
    said: list[tuple[float, str, str]] = []

    def feed(now: float, frame) -> str | None:
        text = arbiter.observe(frame, now)
        if text:
            said.append((now, arbiter.last_cue.event_id, text))
        return text

    assert feed(0.0, _frame(1)) is None, "首帧只建基线"
    assert feed(0.5, _frame(2, events=[_event("you_hit", {"yaw_deg": 30.0})])) is not None
    assert feed(1.0, _frame(3, events=[_event("you_damaged", {"health": 800, "max_health": 2000})])) is None
    first = feed(3.0, _death(4))
    assert first is not None and "被击毁" in first, "前提：第一次阵亡必须说得出来"
    assert feed(3.5, _frame(5, events=[_event("you_respawned", {"deaths": 1})], health=1.0)) is None
    assert feed(6.0, _frame(6, events=[_event("you_damaged", {"health": 1200, "max_health": 2000})])) is not None
    # 间隔没到：这句进队列（旧话就躺在那里等机会）
    assert feed(7.0, _frame(7, events=[_event("you_assisted", {"total": 900, "delta": 120})])) is None
    assert [item[0] for item in arbiter._queue] == ["you_assisted"], "前提：队列里确实有一句旧的"

    # 8.5s：第二次阵亡（间隔已过，但落在 you_died 的 10 秒同类冷却里）
    text = feed(8.5, _death(8, deaths=2))

    assert text is not None, f"阵亡被**静默丢弃**（返回 None），这一帧一句都没说：{said!r}"
    assert "被击毁" in text, f"阵亡没播，播的是旧话：{text!r}（这一局说过：{said!r}）"
    assert arbiter.last_cue.event_id == "you_died"
    assert arbiter.last_cue.level == arbiter_mod.LEVEL_CRITICAL
    assert arbiter._queue == deque(), "阵亡这一帧必须把排队里的旧话作废"


def test_the_same_death_reported_twice_is_spoken_only_once():
    """反向：拆掉时间闸**不等于**可以复读。

    写端负载里 ``deaths`` 是"第几次阵亡"，同一个数再来一遍就是同一次阵亡
    （抖动 / 多路投递），说一遍就够 —— 身份去重替代了那张 10 秒时间表。
    """
    arbiter = arbiter_mod.Arbiter(**PROD)

    first = arbiter.observe(_death(1, deaths=1), 1.0)
    second = arbiter.observe(_death(2, deaths=1), 2.0)

    assert first is not None and "被击毁" in first
    assert second is None, f"同一次阵亡（deaths=1）说了两遍：{second!r}"
    assert arbiter.last_cue.event_id == "you_died"


def test_a_new_death_counter_is_spoken_even_two_seconds_after_the_previous_one():
    """拆掉时间闸后，**真**的第二次阵亡（deaths 涨了）必须说 —— 哪怕只隔 2 秒。"""
    arbiter = arbiter_mod.Arbiter(**PROD)

    first = arbiter.observe(_death(1, deaths=1), 1.0)
    second = arbiter.observe(_death(2, deaths=2), 3.0)

    assert first is not None and "被击毁" in first
    assert second is not None and "被击毁" in second, "deaths 涨了就是新的一次阵亡，不许被冷却吃掉"


def test_death_without_a_counter_is_never_silently_dropped():
    """老写端/脏帧没有 ``deaths``：没有身份可比 → 按"事件本身"算一次，宁可多说也不丢。

    ``event.seq`` 已经由 ``_consume`` 做精确去重（同一条事件不会重放），所以这里的
    "两条"确实是两条独立事件 —— 丢掉任何一条都违反"阵亡不许无声丢弃"。
    """
    arbiter = arbiter_mod.Arbiter(**PROD)

    first = arbiter.observe(_death(1, deaths=None), 1.0)
    second = arbiter.observe(_death(2, deaths=None), 3.0)

    assert first is not None and "被击毁" in first
    assert second is not None and "被击毁" in second


def test_death_is_spoken_at_the_next_speaking_frame_when_interrupt_is_off():
    """关掉打断时阵亡只能排队 —— 但它必须在下一次能说话的帧补说，不许过期作废。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=2.0, idle_fill_s=30.0, interrupt_enabled=False)

    assert arbiter.observe(_frame(1), 0.0) is None
    assert arbiter.observe(_frame(2, events=[_event("you_killed")]), 0.5) is not None
    assert arbiter.observe(_death(3, deaths=1), 1.0) is None, "关掉打断：这一帧不许抢话"
    assert [item[0] for item in arbiter._queue] == ["you_died"], "但阵亡必须进队列"

    text = arbiter.observe(_frame(4), 2.6)

    assert text is not None and "被击毁" in text, f"排队里的阵亡没补说：{text!r}"
    assert arbiter.last_cue.event_id == "you_died"


def test_death_beats_a_pending_warning_in_the_same_frame():
    """同帧有 warning 候选时，阵亡照样赢（跨档永远高档压低档）。"""
    arbiter = arbiter_mod.Arbiter(**PROD)

    frame = _frame(
        1,
        events=[_event("you_damaged", {"health": 300, "max_health": 2000}), _event("you_died", {"deaths": 1})],
        health=0.0,
        alive=False,
        mode="dead",
    )
    text = arbiter.observe(frame, 1.0)

    assert text is not None and "被击毁" in text
    assert arbiter.last_cue.event_id == "you_died"


# ========================================= §二 通道层：阵亡不能被宿主的合并规则删掉
def _host_coalesce(pushes: list[dict]) -> dict[str, dict]:
    """复算宿主的合并规则：**同 key 新的一句顶掉还没出口的旧句子，priority 不参与**。

    宿主的合并规则（等价伪代码）::

        if self._queue:
            dropped = [c for c in self._queue if c.coalesce_key == key]
            if dropped:
                self._queue = [c for c in self._queue if c.coalesce_key != key]

    这里只保留"宿主队列最后剩下谁"，不模拟出口时机（那不影响"谁被删掉"）。
    """
    queue: dict[str, dict] = {}
    for push in pushes:
        queue[str(push.get("coalesce_key") or "")] = push
    return queue


def _section(**overrides) -> dict:
    section = {"dry_run": False, "state_path": "unused.json", "poll_interval_s": 0.05, "stale_seconds": 5.0}
    section.update(overrides)
    return section


def _speak(make_plugin, text: str, level: str, event_id: str) -> dict:
    section = _section()
    plugin, host = make_plugin(section)
    plugin._settings = settings_mod.parse_settings(section)
    plugin._speak(plugin._settings, text, level=level, event_id=event_id)
    assert len(host.pushed) == 1
    return host.pushed[0]


def test_anchors_get_their_own_coalesce_key(make_plugin):
    """阵亡 / 结算各一把 key：既不会被别人的话顶掉，也不会把别人的话顶掉。"""
    died = _speak(make_plugin, "我被击毁了…", "critical", "you_died")
    end = _speak(make_plugin, "这一局结束了。", "critical", "battle_end")
    chatter = _speak(make_plugin, "干掉一个。", "info", "enemy_killed")

    assert chatter["coalesce_key"] == plugin_mod.BATTLE_EVENT_COALESCE_KEY
    assert died["coalesce_key"] != plugin_mod.BATTLE_EVENT_COALESCE_KEY
    assert end["coalesce_key"] != plugin_mod.BATTLE_EVENT_COALESCE_KEY
    assert died["coalesce_key"] != end["coalesce_key"], "阵亡与结算共用一把 key = 后到的把前一条删掉"
    # metadata 与 kwargs 必须同源（宿主两条通道都会读）
    assert died["metadata"]["coalesce_key"] == died["coalesce_key"]
    assert end["metadata"]["coalesce_key"] == end["coalesce_key"]


def test_host_coalescing_cannot_delete_a_pending_death(make_plugin):
    """复现（红）：阵亡排着还没出口，后面的警告/结算一来就把它从宿主队列里删掉。

    实机 21:33:10（阵亡，90）→ 21:33:21（结算，90），中间一次出口都没有。
    """
    section = _section()
    plugin, host = make_plugin(section)
    plugin._settings = settings_mod.parse_settings(section)

    plugin._speak(plugin._settings, "我被击毁了…", level="critical", event_id="you_died")
    plugin._speak(plugin._settings, "还有八百血。", level="warning", event_id="you_damaged")
    plugin._speak(plugin._settings, "这一局结束了。", level="critical", event_id="battle_end")

    survivors = [push["parts"][0]["text"] for push in _host_coalesce(host.pushed).values()]

    assert "我被击毁了…" in survivors, f"阵亡被后面的 cue 顶掉了，宿主队列里只剩：{survivors!r}"


def test_the_death_still_wins_when_a_warning_lands_right_after_it(make_plugin):
    """反向：阵亡抢到出口之后，后来的警告照样走"整局共用一把 key"的老规矩。

    只有 ``GAP_BYPASS`` 锚点有专属 key —— 别的档必须继续共用一个 key，
    否则"新的一句永远替换旧的战况"这条设计（``__init__.py`` 顶部注释）就没了。
    """
    section = _section()
    plugin, host = make_plugin(section)
    plugin._settings = settings_mod.parse_settings(section)

    plugin._speak(plugin._settings, "我被击毁了…", level="critical", event_id="you_died")
    plugin._speak(plugin._settings, "还有八百血。", level="warning", event_id="you_damaged")
    plugin._speak(plugin._settings, "炮弹上膛了。", level="info", event_id="reload_done")

    chatter = [p for p in host.pushed if p["coalesce_key"] == plugin_mod.BATTLE_EVENT_COALESCE_KEY]
    assert [p["parts"][0]["text"] for p in chatter] == ["还有八百血。", "炮弹上膛了。"]
    assert [p["parts"][0]["text"] for p in _host_coalesce(chatter).values()] == ["炮弹上膛了。"]


def test_poll_once_pushes_the_death_on_its_anchor_key(tmp_path, make_plugin):
    """端到端接线：真读一帧 ``state.json`` → 仲裁 → ``push_message`` 必须带锚点 key。

    上面几条直接调 ``_speak``；这条证明 ``last_cue`` → 通道层那条链没断
    （``event_id`` 抄错一个字符，锚点 key 就悄悄退回共用那把，实机又会被顶掉）。
    """
    path = tmp_path / "state.json"

    def _write(seq: int, events: list[dict]) -> None:
        path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "seq": seq,
                    "ts": time.time(),
                    "mode": "battle",
                    "arena_id": "arena-babylon",
                    "me": {"vehicle_name": "WZ-219", "health_ratio": 0.0, "alive": False},
                    "team": {"ally_alive": 9, "ally_total": 15, "enemy_alive": 12, "enemy_total": 15},
                    "arena": {"period": 2, "time_left": 300},
                    "result": None,
                    "events": events,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    _write(1, [])
    section = _section(state_path=str(path))
    plugin, host = make_plugin(section)
    plugin._settings = settings_mod.parse_settings(section)
    plugin._arbiter = plugin_mod.Arbiter(min_gap_s=0.0)
    plugin._primed = False
    plugin._prev_seq = None
    assert plugin._poll_once(plugin._settings, plugin._arbiter, time.time()) is None, "首帧只对齐基线"

    _write(2, [{"seq": 21, "id": "you_died", "ts": time.time(), "payload": {"deaths": 1}}])
    text = plugin._poll_once(plugin._settings, plugin._arbiter, time.time())

    assert text is not None and "被击毁" in text
    pushed = host.pushed[-1]
    # ⚠️ 断言**字面量**，不拿 coalesce_key_for() 跟自己比（"用例验自己" = 假绿）
    assert pushed["coalesce_key"] == "neko_wot:battle_anchor:you_died"
    assert pushed["coalesce_key"] != plugin_mod.BATTLE_EVENT_COALESCE_KEY
    assert pushed["metadata"]["coalesce_key"] == pushed["coalesce_key"]
    assert pushed["metadata"]["event_id"] == "you_died"
    assert pushed["priority"] == arbiter_mod.LEVEL_PRIORITY[arbiter_mod.LEVEL_CRITICAL]
