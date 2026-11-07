"""v1.30 / P1：``you_hit`` 的 ``dup`` 读端闸。

**为什么有这条闸**：收口之前，同一发炮弹会有两条投递，实机一轮 5 条里 seq1/seq2、
seq3/seq4 各是同一发（时间戳相差
1ms、payload 逐项相同）。协议 v1.30 已经收口成唯一出口 + 250ms 同键去重，并把重复
的那条标成 ``dup=1``（协议 §5.1）；读端必须**真的丢掉它**，否则"受击统计"会被算两遍。

真实数据来自一次实机采样的 seq1~seq5，payload 逐字抄在这里（用例可以独立跑，
不依赖任何本机路径）。
"""

from __future__ import annotations

import sdk_stub

_pkg = sdk_stub.load_plugin_module()
arbiter_mod = _pkg.arbiter
state_reader = _pkg.state_reader

# ---------------------------------------------------------------- 实测 payload
# 写端 v1.30 的投递形状 = 实测 payload + ``src``（来源）+ ``dup``（是否重复投递）。
# 这里刻意保留实测里的所有键（attacker_id / crits / he / attack_reason_id…），
# 好顺便验证"白名单会把它们挡在话术之外"。
_REAL_HIT_1 = {
    "attack_reason_id": 0,
    "attacker": None,
    "attacker_ally": None,
    "attacker_id": 3966136,
    "attacker_vehicle": None,
    "blocked": False,
    "crits": 65536,
    "damage": 0,
    "he": False,
    "yaw": 0.033,
    "yaw_deg": 1.9,
}
_REAL_HIT_2 = {
    "attack_reason_id": 0,
    "attacker": None,
    "attacker_ally": None,
    "attacker_id": 3966133,
    "attacker_vehicle": None,
    "blocked": False,
    "crits": 65536,
    "damage": 721,
    "he": True,
    "yaw": 0.0584,
    "yaw_deg": 3.3,
}
_REAL_DAMAGED = {"delta": 721, "health": 2109, "hits": 1, "max_health": 2830, "ratio_after": 0.745}

T0 = 1793522285.706


def _hit(seq: int, payload: dict, *, src: str, dup: int, ts: float) -> dict:
    body = dict(payload)
    body["src"] = src
    body["dup"] = dup
    return {"seq": seq, "id": "you_hit", "ts": ts, "payload": body}


def _damaged(seq: int, ts: float) -> dict:
    return {"seq": seq, "id": "you_damaged", "ts": ts, "payload": dict(_REAL_DAMAGED)}


def _frame(events, *, seq: int = 1) -> state_reader.Frame:
    return state_reader.Frame(
        status=state_reader.STATUS_OK,
        schema=1,
        seq=seq,
        ts=T0,
        mode="battle",
        arena_id="arena-live",
        me={"vehicle_name": "WZ-219", "health_ratio": 0.75},
        events=tuple(events),
    )


def _replay(arbiter: arbiter_mod.Arbiter) -> list[str | None]:
    """把实测那 5 条按**真实时间线**喂进去（两发各两条 + 一条掉血）。"""
    first = [
        _hit(1, _REAL_HIT_1, src="entry", dup=0, ts=T0),
        _hit(2, _REAL_HIT_1, src="instance", dup=1, ts=T0 + 0.001),
    ]
    second = [
        _hit(3, _REAL_HIT_2, src="entry", dup=0, ts=T0 + 2.195),
        _hit(4, _REAL_HIT_2, src="instance", dup=1, ts=T0 + 2.196),
    ]
    hurt = [_damaged(5, T0 + 2.265)]
    return [
        arbiter.observe(_frame(first, seq=1), 0.0),
        arbiter.observe(_frame(second, seq=2), 2.196),
        arbiter.observe(_frame(hurt, seq=3), 2.265),
    ]


def test_real_sequence_speaks_the_hit_once_and_counts_one_sample_per_shot():
    """实测 5 条回放：``you_hit`` 只播 1 条；受击样本只记 1 条（一个物理事件）。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    spoken = _replay(arbiter)

    # 第一句是受击（yaw 1.9° → 正前方），第二发落在 8 秒同类冷却里，第三句是掉血。
    assert spoken[0] is not None and "正前方" in spoken[0], spoken
    assert spoken[1] is None, "两发之间只有 2.2 秒，第二发必须被 you_hit 的同类冷却压住：%r" % (spoken,)
    assert spoken[2] is not None and "挨了一下狠的" in spoken[2], spoken

    # dup 的功劳：4 条 you_hit 只落成 1 个"物理受击"，you_damaged 也只有 1 条进环。
    assert len(arbiter._hit_samples) == 1, arbiter._hit_samples


def test_dup_events_never_enter_the_fresh_list():
    """``dup=1`` 在 ``_consume`` 就被丢掉：既不渲染、也不进任何累计。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    fresh = arbiter._consume(
        (
            _hit(1, _REAL_HIT_1, src="entry", dup=0, ts=T0),
            _hit(2, _REAL_HIT_1, src="instance", dup=1, ts=T0 + 0.001),
        ),
        count_kills=False,
    )
    assert [event["seq"] for event in fresh] == [1]
    # 但它确实被记进"已见"了：下一帧原样再来一遍也不会复活
    again = arbiter._consume((_hit(2, _REAL_HIT_1, src="instance", dup=1, ts=T0 + 0.001),), count_kills=False)
    assert again == []


def test_same_frame_dup_and_original_produce_one_line_only():
    """同一帧同时来 dup=0 与 dup=1 → 只产生 1 条播报。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    event = [_hit(3, _REAL_HIT_2, src="entry", dup=0, ts=T0), _hit(4, _REAL_HIT_2, src="instance", dup=1, ts=T0)]

    said = arbiter.observe(_frame(event, seq=1), 0.0)

    assert said is not None and "挨了一下" in said, said
    # 掉血事件才是受击样本的来源；这一帧没有 you_damaged → 0 条样本
    assert len(arbiter._hit_samples) == 0
    assert set(arbiter._seen) == {3, 4}, "两条都要登记为已见（dup 那条也不许复活）"


def test_dup_is_dropped_even_after_cooldown_has_elapsed():
    """冷却过了也不许"补说"：dup 的丢弃在**入口**，不是靠冷却压掉的。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    assert arbiter.observe(_frame([_hit(1, _REAL_HIT_1, src="entry", dup=0, ts=T0)], seq=1), 0.0) is not None

    late = _hit(9, _REAL_HIT_1, src="class", dup=1, ts=T0 + 60.0)
    assert arbiter.observe(_frame([late], seq=2), 60.0) is None


def test_missing_or_zero_dup_still_speaks():
    """只有 ``dup == 1`` 才算重复：缺键 / 0 / 别的脏值一律照常处理（宁多说，不漏说）。"""
    for dup in (0, None, "yes", 2):
        arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
        payload = dict(_REAL_HIT_2)
        payload["src"] = "entry"
        if dup is not None:
            payload["dup"] = dup
        said = arbiter.observe(_frame([{"seq": 1, "id": "you_hit", "ts": T0, "payload": payload}], seq=1), 0.0)
        assert said is not None, dup


def test_dup_true_is_treated_as_one():
    """``dup=True`` 也算重复（JSON 之外的手工投递/别的语言里 True == 1）。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    event = _hit(1, _REAL_HIT_1, src="entry", dup=0, ts=T0)
    event_true = _hit(2, _REAL_HIT_1, src="instance", dup=0, ts=T0)
    event_true["payload"]["dup"] = True

    fresh = arbiter._consume((event, event_true), count_kills=False)
    assert [item["seq"] for item in fresh] == [1]

    # 顺带锁一条：两条都登记为"已见"，但只有 dup=0 的那条进了 fresh
    arbiter2 = arbiter_mod.Arbiter(min_gap_s=0.0)
    assert arbiter2.observe(_frame([event, event_true], seq=1), 0.0) is not None
    assert set(arbiter2._seen) == {1, 2}
