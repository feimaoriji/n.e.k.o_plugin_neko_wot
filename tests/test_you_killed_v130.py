"""v1.30 / P3：``you_killed`` 按"这一局第几个"分级 + 特殊死因档。

写端 shape（协议 §5.2，6 键）：``victim_id`` / ``victim_vehicle`` / ``dmg_dealt`` /
``dmg_acc`` / ``was_fire`` / ``was_ammo`` —— **本轮 ``was_fire`` / ``was_ammo`` 恒为
null**（拿不到确凿来源），所以：

* 读到 null → 走普通档（单杀 / 连杀），绝不把"不知道"说成"不是被烧死的"；
* 只有**真给 true** 才走「殉爆 / 烧穿」专属措辞；
* ``victim_vehicle`` 之类不在白名单里的话术来源一个都不许念。
"""

from __future__ import annotations

import sdk_stub

_pkg = sdk_stub.load_plugin_module()
arbiter_mod = _pkg.arbiter
state_reader = _pkg.state_reader


def _payload(**overrides) -> dict:
    """协议 §5.2 的 6 键（本轮的取值：两个死因键恒 null）。"""
    base = {
        "victim_id": 3966133,
        "victim_vehicle": None,
        "dmg_dealt": 421,
        "dmg_acc": 2870,
        "was_fire": None,
        "was_ammo": None,
    }
    base.update(overrides)
    return base


def _frame(seq: int, event_seq: int, payload: dict | None = None, *, arena: str = "arena-1"):
    return state_reader.Frame(
        status=state_reader.STATUS_OK,
        schema=1,
        seq=seq,
        ts=1000.0,
        mode="battle",
        arena_id=arena,
        me={"vehicle_name": "WZ-219", "health_ratio": 0.7},
        events=({"seq": event_seq, "id": "you_killed", "ts": 1000.0, "payload": payload or {}},),
    )


def _quiet_frame(seq: int, *, arena: str = "arena-1"):
    """同一局里"这一帧没有击杀"的空窗口（只用来建基线）。"""
    return state_reader.Frame(
        status=state_reader.STATUS_OK,
        schema=1,
        seq=seq,
        ts=1000.0,
        mode="battle",
        arena_id=arena,
        me={"vehicle_name": "WZ-219", "health_ratio": 0.7},
        events=(),
    )


def _kills(n: int, payload: dict | None = None) -> list[str]:
    """连打 n 个（每帧一个，event seq 递增），返回每次说的话。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    said: list[str] = []
    arbiter.observe(_quiet_frame(1), 0.0)  # 建基线（这一帧还没有击杀）
    for index in range(n):
        text = arbiter.observe(_frame(2 + index, 101 + index, payload), 10.0 + index * 20.0)
        assert text is not None, index
        said.append(text)
    return said


def test_first_kill_uses_the_unchanged_default_line():
    assert _kills(1) == ["击毁一台。{MASTER_NAME}，这一局第 1 个。"]


def test_second_kill_switches_to_the_streak_tier():
    """本局第 ≥2 个走连杀档（v1.30 新增）。"""
    said = _kills(2, _payload())

    assert said[0] == "击毁一台。{MASTER_NAME}，这一局第 1 个。"
    assert said[1] == "又干掉一台，第 2 个！"


def test_ammo_rack_detonation_tier():
    """``was_ammo`` **确为 true** 才说殉爆；本轮写端给 null，所以平时见不到这句。"""
    said = _kills(2, _payload(was_ammo=True, was_fire=None))
    assert said[1] == "弹药架殉爆了，第 2 个！"


def test_burning_tier():
    said = _kills(2, _payload(was_fire=True, was_ammo=None))
    assert said[1] == "烧穿了，第 2 个！"


def test_ammo_beats_fire_when_both_are_true():
    """两个都为 true 时取"殉爆"（更准的那个死因）。"""
    said = _kills(2, _payload(was_ammo=True, was_fire=True))
    assert said[1] == "弹药架殉爆了，第 2 个！"


def test_null_deaths_do_not_claim_a_cause():
    """两个死因都是 null（本轮常态）→ 普通档，一个字都不许提殉爆/烧。"""
    said = _kills(2, _payload(was_fire=None, was_ammo=None))
    for text in said:
        assert "殉爆" not in text and "烧" not in text and "火" not in text, text


def test_dirty_death_flags_are_not_treated_as_true():
    """``"true"`` / 1 / ``"yes"`` 这些脏值一律不算数（只有真 true 才算）。"""
    for payload in (
        _payload(was_ammo="true"),
        _payload(was_ammo=1),
        _payload(was_fire="yes"),
        _payload(was_fire=1.0),
    ):
        said = _kills(2, payload)
        assert "殉爆" not in said[1] and "烧穿" not in said[1], (payload, said)


def test_victim_fields_never_reach_the_phrase_table():
    """``victim_vehicle`` / ``victim_id`` 不在白名单 → 一个字符都不许出现在话术里。"""
    said = _kills(1, _payload(victim_vehicle="IS-7", victim_id=123456789))
    assert "IS-7" not in said[0]
    assert "123456789" not in said[0]


def test_kills_counter_resets_on_a_new_battle():
    """换局后重新从"第 1 个"数起（局内计数属于局内状态）。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    arbiter.observe(_quiet_frame(1), 0.0)
    assert "第 1 个" in arbiter.observe(_frame(2, 101, arena="arena-1"), 10.0)
    assert "第 2 个" in arbiter.observe(_frame(3, 102, arena="arena-1"), 30.0)

    # 新一局：首帧只建基线，随后第一个击杀又说"第 1 个"
    assert arbiter.observe(_quiet_frame(4, arena="arena-2"), 100.0) is None
    assert "第 1 个" in arbiter.observe(_frame(5, 201, arena="arena-2"), 130.0)
