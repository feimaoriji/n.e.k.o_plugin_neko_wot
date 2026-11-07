"""``arbiter``：去重 / 优先级 / 冷却 / 全局间隔 / 结算 null / 合规。

合规用例是硬门槛：把所有模板跑一遍，断言产出里不含任何"敌方信息"关键词，
并且事件 payload 里塞进来的敌方字段绝不会出现在话术里。
"""

from __future__ import annotations

from collections import deque

import sdk_stub

_pkg = sdk_stub.load_plugin_module()
arbiter_mod = _pkg.arbiter
state_reader = _pkg.state_reader

# 禁词表**只有一份**，就在实现里：``arbiter.FORBIDDEN_WORD_GROUPS``。
# 用例从实现取，别在测试里再抄一份词表（两份词表就是两个真相，改一处必漂）。
# 这里取的是 ENEMY 组（一律禁止）；SELF 组是我方规范说法，**允许**说出口。
FORBIDDEN = arbiter_mod.FORBIDDEN_WORD_GROUPS["ENEMY"]


def _event(seq: int, event_id: str, payload=None, ts: float = 1000.0) -> dict:
    return {"seq": seq, "id": event_id, "ts": ts, "payload": payload or {}}


def _frame(events, *, arena: str | None = "arena-1", seq: int = 1, me=None, result=None):
    return state_reader.Frame(
        status=state_reader.STATUS_OK,
        schema=1,
        seq=seq,
        ts=1000.0,
        mode="battle",
        arena_id=arena,
        me={"vehicle_name": "WZ-219", "health_ratio": 0.8} if me is None else me,
        result=result,
        events=tuple(events),
    )


def test_same_event_is_spoken_once():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert arbiter.observe(_frame([_event(7, "you_killed")], seq=1), 0.0)
    # 同一条 event.seq 又来一遍（数据端保留滑动窗口后的常态）→ 不再说
    assert arbiter.observe(_frame([_event(7, "you_killed")], seq=2), 10.0) is None


def test_highest_priority_wins_within_one_frame():
    """同帧同档内仍按 ``PRIORITY`` 取最大（v2.12 起这个数字的作用域只剩档内）。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    frame = _frame([_event(1, "you_assisted", {"total": 340, "delta": 60}), _event(2, "you_killed")])

    text = arbiter.observe(frame, 0.0)

    assert text is not None
    assert "击毁一台" in text
    assert "三百四十" not in text


def test_critical_tier_beats_warning_even_with_lower_priority_number():
    """v2.12：跨档时**档位说了算** —— battle_end（档内 20）压 you_killed（档内 30）。

    旧实现只比一个裸数字，结算会被自己刚打的那个击杀盖掉；而事件只活一帧，
    这一局的结算播报就永远没了。档位拆成两层之后这种跨档错比不可能再发生。
    （原来这条断言写在 ``test_highest_priority_wins_within_one_frame`` 里，是旧口径。）
    """
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    frame = _frame([_event(1, "battle_end"), _event(2, "you_killed")])

    text = arbiter.observe(frame, 0.0)

    assert text is not None
    assert "结束" in text
    assert "击毁一台" not in text


def test_you_died_beats_on_fire():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    frame = _frame([_event(1, "on_fire"), _event(2, "you_died")])

    text = arbiter.observe(frame, 0.0)

    assert text is not None
    assert "被击毁" in text
    assert "着火" not in text


def test_same_type_cooldown():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert arbiter.observe(_frame([_event(1, "you_killed")]), 0.0)
    assert arbiter.observe(_frame([_event(2, "you_killed")]), 1.0) is None
    assert arbiter.observe(_frame([_event(3, "you_killed")]), 5.0) is not None


def test_global_min_gap_blocks_without_queueing():
    arbiter = arbiter_mod.Arbiter(min_gap_s=8.0)

    assert arbiter.observe(_frame([_event(1, "you_killed")]), 0.0)
    # 全局间隔内的 **info 档**直接丢掉，不排队（"别吵"的设计意图）。
    # v2.12 改了触发物：这里原来是 ``on_fire``，而着火现在归 critical 档，
    # 打断开启时会**无视 min_gap 当场说**（见 test_interrupt_enabled_* 那一批）。
    # "被间隔拦下的低价值句子直接丢"这条语义本身没变，换成 info 档来守。
    assert arbiter.observe(_frame([_event(2, "battle_start")]), 1.0) is None
    assert arbiter.observe(_frame([_event(3, "reload_done")]), 2.0) is None
    assert arbiter._queue == deque(), "info 被拦下时不许进队列"

    text = arbiter.observe(_frame([_event(4, "you_damaged")]), 9.0)

    assert text is not None
    assert "掉血" in text
    assert "开打了" not in text
    assert "上膛" not in text


def test_battle_end_bypasses_global_gap():
    """真实节奏：打完最后一炮（说了击杀）→ 2 秒后结算。复盘绝不能被间隔吃掉。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=8.0)

    assert arbiter.observe(_frame([_event(1, "you_killed")], seq=1), 0.0)
    text = arbiter.observe(
        _frame([_event(2, "battle_end")], seq=2, result={"won": True, "kills": 3, "damage": 2870}),
        2.0,
    )

    assert text is not None
    assert "结束" in text
    assert "2870" in text


def test_you_died_bypasses_global_gap():
    arbiter = arbiter_mod.Arbiter(min_gap_s=8.0)

    assert arbiter.observe(_frame([_event(1, "battle_start")], seq=1), 0.0)
    text = arbiter.observe(_frame([_event(2, "you_died")], seq=2), 2.0)

    assert text is not None
    assert "被击毁" in text


def test_kill_is_deferred_not_lost():
    """被间隔压住的击杀只是推迟，不会消失（事件只活一帧，丢了就没补救）。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=8.0)

    assert arbiter.observe(_frame([_event(1, "battle_start")], seq=1), 0.0)
    assert arbiter.observe(_frame([_event(2, "you_killed")], seq=2), 2.0) is None
    # 间隔一过，哪怕这帧只有掉血，也先把排队的高光补上
    text = arbiter.observe(_frame([_event(3, "you_damaged")], seq=3), 8.5)

    assert text is not None
    assert "击毁一台" in text


def test_pending_is_cleared_on_new_battle():
    arbiter = arbiter_mod.Arbiter(min_gap_s=8.0)

    assert arbiter.observe(_frame([_event(1, "battle_start")], seq=1), 0.0)
    assert arbiter.observe(_frame([_event(2, "you_killed")], seq=2), 2.0) is None
    # 换局（arena 变）→ 上一局排队的那句作废，不能说成新局的第一杀
    text = arbiter.observe(_frame([_event(3, "battle_start")], arena="arena-2", seq=3), 3.0)

    assert text is not None
    assert "开打了" in text


def test_battle_end_survives_null_result():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    frame = _frame([_event(1, "battle_end")], result={"won": None, "draw": None, "kills": None, "damage": None})

    text = arbiter.observe(frame, 0.0)

    assert text is not None
    assert "结束" in text


def test_battle_end_reads_result_numbers():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    frame = _frame([_event(1, "battle_end")], result={"won": True, "kills": 3, "damage": 2870})

    text = arbiter.observe(frame, 0.0)

    assert "赢了" in text
    assert "3" in text
    assert "2870" in text


def test_arena_change_keeps_dedup_and_speaks_new_start():
    """换局**不许**清已见集合（v2.2）：上一局残留的事件一句都不许重放。

    旧实现按 ``(arena_id, seq)`` 去重 + 换局清空 → 实测：新一局刚开打，
    插件对着窗口里残留的 ``you_died`` 喊了"被击毁了…"，真正的 ``battle_start``
    （同帧优先级被 50 压过）一句都没播。
    """
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert arbiter.observe(_frame([_event(7, "you_killed")], arena="arena-a"), 0.0)
    # 换局后同一 seq 仍是"见过的" → 不许再念（旧行为会再念一遍）
    assert arbiter.observe(_frame([_event(7, "you_killed")], arena="arena-b"), 30.0) is None
    # 但新局自己的事件（seq 继续涨）必须照常念，且同类冷却已随换局清零
    assert arbiter.observe(_frame([_event(8, "you_killed")], arena="arena-b"), 31.0) is not None


def test_seen_table_is_bounded():
    """去重表有界（内存不随战斗时长膨胀）。

    v2.2 语义变化：seq **回退**不再等于"可以重放"，而是"数据端重启"的信号 ——
    这一帧一句都不说（里面全是重启前的历史事件），但去重表已清空、新序列继续工作。
    """
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0, max_seen=3, type_cooldown_s={"you_killed": 0.0})

    for seq in range(1, 5):
        assert arbiter.observe(_frame([_event(seq, "you_killed")], seq=seq), 0.0) is not None
    assert len(arbiter._seen) <= 3, "去重表必须被 max_seen 夹住"

    # seq 回退到 1 → 判为数据端重启：静默 prime，一句都不说
    assert arbiter.observe(_frame([_event(1, "you_killed")], seq=99), 0.0) is None
    # 重启后的新序列照常工作（绝不能"永远判重启"把自己说哑）
    assert arbiter.observe(_frame([_event(2, "you_killed")], seq=100), 0.0) is not None


def test_forbidden_rules_live_in_one_constant_with_two_groups():
    """v2.7 B1：禁词规则是**一处常量**，分成 SELF（允许）/ ENEMY（禁止）两组。

    这里锁住"词表骨架"——矩阵用例是从实现取词的（单一真相），所以必须有这条
    用例防止有人把整张表删空/删掉核心词后"全绿"。
    """
    groups = arbiter_mod.FORBIDDEN_WORD_GROUPS

    assert set(groups) == {"SELF", "ENEMY"}
    for word in ("我被击毁了", "我这台没了"):
        assert word in groups["SELF"], word
    for word in ("敌人", "敌方", "对面", "位置", "装填", "reload", "瞄准"):
        assert word in groups["ENEMY"], word
    # 两组不许交叉：同一句话不能既算"允许的己方说法"又算"敌方情报"
    assert not set(groups["SELF"]) & set(groups["ENEMY"])


def test_hitting_both_groups_is_red_by_enemy_first():
    """v2.7 B1 优先级锁：同时命中两组 → 按 ENEMY 组从严判红。

    允许的己方说法（"我被击毁了"）**不能**给后面夹带的敌方情报洗白。
    """
    both = "我被击毁了，对面在左边。"
    assert arbiter_mod.preach_word_hits(both) == frozenset({"SELF", "ENEMY"})
    assert arbiter_mod.preach_is_forbidden(both) is True

    # 只命中 SELF：允许
    own = "我被击毁了，缓一下。"
    assert arbiter_mod.preach_word_hits(own) == frozenset({"SELF"})
    assert arbiter_mod.preach_is_forbidden(own) is False

    # 一句里既有我方损失、又带敌方情报（不是人数）→ 红。
    # ⚠️ v1.30 起「敌方 + 数量」是**唯一**豁免句式（「敌方剩两台」那样），
    # 所以这里用"敌方在左边"来锁"两组同时命中仍然从严"这条语义。
    mixed = "我们这边少了一个，敌方在左边。"
    assert "SELF" in arbiter_mod.preach_word_hits(mixed)
    assert arbiter_mod.preach_is_forbidden(mixed) is True

    # 干净的己方话术：既不命中任何组也不算红
    assert arbiter_mod.preach_word_hits("缓一下，{MASTER_NAME}。") == frozenset()
    assert arbiter_mod.preach_is_forbidden("缓一下，{MASTER_NAME}。") is False


def test_the_render_gate_really_drops_a_forbidden_line(monkeypatch):
    """闸是**真的接在出口上**的：话术一旦命中 ENEMY 组，这一帧就一句都不说。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    original = arbiter._render
    monkeypatch.setattr(arbiter, "_render", lambda event, frame: "我方击毁一台，敌方在左边。")

    assert arbiter.observe(_frame([_event(1, "you_killed")]), 0.0) is None
    # 还原（monkeypatch 自动还原）：同一句变干净后照常说
    monkeypatch.setattr(arbiter, "_render", original)
    assert arbiter.observe(_frame([_event(2, "you_killed")], seq=2), 30.0) is not None


def test_every_template_is_free_of_forbidden_words():
    rendered: list[str] = []
    for event_id in arbiter_mod.TYPE_COOLDOWN_S:
        arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
        frame = _frame([_event(1, event_id, payload={"ratio_after": 0.42, "total": 3, "vehicle": "WZ-219"})])
        text = arbiter.observe(frame, 0.0)
        assert text, event_id
        rendered.append(text)

    for text in rendered:
        lowered = text.lower()
        for word in FORBIDDEN:
            assert word.lower() not in lowered, f"{word!r} in {text!r}"

    assert any("{MASTER_NAME}" in text for text in rendered)


def test_event_payload_cannot_smuggle_enemy_fields():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    payload = {
        "victim_name": "SENTINEL_VICTIM",
        "enemy_position": [1.0, 2.0, 3.0],
        "tracer": "SENTINEL_TRACER",
        "reload_time": 9.9,
        "vehicle": "WZ-219",
    }

    text = arbiter.observe(_frame([_event(1, "battle_start", payload=payload)]), 0.0)

    assert text is not None
    assert "WZ-219" in text
    for leaked in ("SENTINEL_VICTIM", "SENTINEL_TRACER", "9.9"):
        assert leaked not in text


def test_non_ok_frame_is_ignored():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    stale = state_reader.Frame(status=state_reader.STATUS_STALE, events=(_event(1, "you_killed"),))

    assert arbiter.observe(stale, 0.0) is None
    assert arbiter.observe(None, 0.0) is None


def test_respawn_mode_death_does_not_promise_next_battle():
    """重生模式（巴比伦）：死亡话术不能说"下一局"，因为几秒后就回来了。

    数据端在重生模式下会重复发 you_died（每次阵亡一条），然后发 you_respawned。
    """
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    died = arbiter.observe(_frame([_event(1, "you_died", {"deaths": 1})], seq=1), 0.0)
    assert died is not None
    assert "被击毁" in died
    assert "下一局" not in died

    back = arbiter.observe(_frame([_event(2, "you_respawned", {"deaths": 1, "lives": 2})], seq=2), 3.0)
    assert back is not None
    assert "又活了" in back


def test_respawn_mode_two_lives_two_deaths():
    """一局死两次 = 两句话，不该被同类冷却吃掉（巴比伦模式实测会死 2~3 次）。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert arbiter.observe(_frame([_event(1, "you_died", {"deaths": 1})], seq=1), 0.0)
    assert arbiter.observe(_frame([_event(2, "you_respawned", {"deaths": 1})], seq=2), 3.0)
    assert arbiter.observe(_frame([_event(3, "you_died", {"deaths": 2})], seq=3), 30.0)
    assert arbiter.observe(_frame([_event(4, "you_respawned", {"deaths": 2})], seq=4), 33.0)


def test_battle_end_only_once_after_respawns():
    """重生模式真正结束时只报一次结算 —— 死亡阶段绝不能报"结束"。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert arbiter.observe(_frame([_event(1, "you_died", {"deaths": 1})], seq=1), 0.0)
    died_text = arbiter.observe(_frame([_event(2, "you_died", {"deaths": 2})], seq=2), 30.0)
    assert died_text is None or "结束" not in died_text

    end = arbiter.observe(
        _frame([_event(3, "battle_end", {"deaths": 2, "respawns": 2})],
               seq=3, result={"won": True, "kills": 2, "damage": 1500}),
        60.0,
    )
    assert end is not None
    assert "结束" in end


# ----------------------------------------------------------------- 话术精度（v2.0）
# 标准模式实机：`china:Ch71_WZ_219`、连说两次"96%"、"掉血 93% → 44%"
# 跳变、"结果未知却说击毁 0 台" —— 全都在这几条里钉住。


def test_damage_speaks_absolute_health():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    payload = {"delta": 66, "ratio_after": 0.965, "health": 1834, "max_health": 1900}

    text = arbiter.observe(_frame([_event(1, "you_damaged", payload)]), 0.0)

    assert text is not None
    assert "一千八百" in text  # 千位以上只到百位
    assert "%" not in text  # 有具体数字就不说百分比


def test_damage_speaks_colloquial_chinese():
    """维护者的口语习惯：X千X百 / X百X十（已定）。"""
    cases = {
        841: "八百四十",
        388: "三百八十",
        105: "一百",
        1900: "一千九百",
        1000: "一千",
    }
    for seq, (health, spoken) in enumerate(cases.items(), start=1):
        arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
        payload = {"health": health, "max_health": 1900, "delta": 10}
        text = arbiter.observe(_frame([_event(seq, "you_damaged", payload)]), 0.0)
        assert spoken in text, (health, text)


def test_big_hit_is_called_out():
    """阈值 500（已定）：397 只说血线，520 才配"挨了一下狠的"。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    payload = {"health": 841, "max_health": 1900, "delta": 397}
    text = arbiter.observe(_frame([_event(1, "you_damaged", payload)]), 0.0)
    assert "狠" not in text
    assert "八百四十" in text

    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    payload = {"health": 388, "max_health": 1900, "delta": 520}
    text = arbiter.observe(_frame([_event(1, "you_damaged", payload)]), 0.0)
    assert "狠" in text
    assert "三百八十" in text


def test_low_health_gets_a_joke_line():
    """低于一百血开始说俏皮话，且按事件 seq 轮换（不总说同一句）。"""
    pool = arbiter_mod.LOW_HP_LINES
    assert any("方向盘" in line for line in pool)
    assert any("螺丝钉" in line for line in pool)

    seen = set()
    for seq in range(1, len(pool) + 1):
        arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
        payload = {"health": 99, "max_health": 1900, "delta": 20}
        text = arbiter.observe(_frame([_event(seq, "you_damaged", payload)]), 0.0)
        assert "血量见底了" in text
        assert any(line in text for line in pool)
        seen.add(text)
    assert len(seen) == len(pool)  # 一局里轮着来，不是复读机


def test_damage_percent_floor_not_round():
    """96.5% 和 95.7% 都要说成 96/95 —— round 会让两句一模一样。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    first = arbiter.observe(_frame([_event(1, "you_damaged", {"ratio_after": 0.965})]), 0.0)
    second = arbiter.observe(_frame([_event(2, "you_damaged", {"ratio_after": 0.957})]), 30.0)

    assert "96%" in first
    assert "95%" in second


def test_damage_without_any_number_still_speaks():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    text = arbiter.observe(_frame([_event(1, "you_damaged", {})]), 0.0)

    assert text is not None
    assert "掉血" in text


def test_battle_start_prefers_localized_vehicle_name():
    """写端 me.vehicle_name 是游戏本地化串（WZ219），事件 payload 里是内部 ID。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    frame = _frame(
        [_event(1, "battle_start", {"vehicle": "china:Ch71_WZ_219"})],
        me={"vehicle_name": "WZ219", "health_ratio": 1.0},
    )

    text = arbiter.observe(frame, 0.0)

    assert "WZ219" in text
    assert "china:" not in text


def test_battle_start_cleans_internal_vehicle_id_as_fallback():
    """拿不到本地化串时，至少把 china:Ch71_WZ_219 洗成 WZ-219。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    frame = _frame([_event(1, "battle_start", {"vehicle": "china:Ch71_WZ_219"})], me={})

    text = arbiter.observe(frame, 0.0)

    assert "WZ-219" in text


def test_battle_end_without_result_does_not_claim_zero_kills():
    """result 恒为 None（写端还没读结算）时，绝不能编"击毁 0 台"。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    text = arbiter.observe(_frame([_event(1, "battle_end")], result=None), 0.0)

    assert text is not None
    assert "结束" in text
    assert "0 台" not in text
    assert "击毁" not in text


def test_battle_end_counts_only_observed_kills():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert arbiter.observe(_frame([_event(1, "you_killed")], seq=1), 0.0)
    arbiter.observe(_frame([_event(2, "you_killed")], seq=2), 30.0)
    end = arbiter.observe(_frame([_event(3, "battle_end")], seq=3), 60.0)

    assert "我数到击毁 2 台" in end


def test_pending_damage_keeps_only_latest():
    """挨打密集时排队只留最新血线，间隔一过报当前的，不报过期的。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=8.0)

    assert arbiter.observe(_frame([_event(1, "battle_start")], seq=1), 0.0)
    # 间隔内两连击：都被压住，但排队里只该留下最新的 1500
    assert arbiter.observe(
        _frame([_event(2, "you_damaged", {"health": 1800, "max_health": 1900})], seq=2), 2.0
    ) is None
    assert arbiter.observe(
        _frame([_event(3, "you_damaged", {"health": 1500, "max_health": 1900})], seq=3), 4.0
    ) is None
    # 间隔一过（同类冷却也要过：you_damaged 12s），报的是最新那条 1500
    text = arbiter.observe(_frame([], seq=4), 10.0)

    assert text is not None
    assert "一千五百" in text
    assert "一千八百" not in text


def test_damage_to_zero_health_is_silent():
    """血归零那一下别说"还剩 0/1900"，让 you_died 说。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    payload = {"delta": 388, "health": 0, "max_health": 1900, "ratio_after": 0.0}

    assert arbiter.observe(_frame([_event(1, "you_damaged", payload)]), 0.0) is None
    # 同一帧里有死亡事件时，说出来的必须是死亡那句
    text = arbiter.observe(_frame([_event(2, "you_damaged", payload), _event(3, "you_died")]), 1.0)
    assert text is not None
    assert "被击毁" in text


def test_terminal_damage_supersedes_pending_blood_line():
    """排队中的血线被"血归零"顶掉：最后听到的是死亡那句，不是"还剩 388"。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=8.0)

    assert arbiter.observe(_frame([_event(1, "battle_start")], seq=1), 0.0)
    pending = {"delta": 453, "health": 388, "max_health": 1900}
    assert arbiter.observe(_frame([_event(2, "you_damaged", pending)], seq=2), 2.0) is None

    terminal = {"delta": 388, "health": 0, "max_health": 1900}
    died = arbiter.observe(
        _frame([_event(3, "you_damaged", terminal), _event(4, "you_died", {"deaths": 1})], seq=3),
        4.0,
    )
    assert died is not None
    assert "被击毁" in died

    # 之后任何一帧都不许再补说那句过期的血线
    assert arbiter.observe(_frame([], seq=4), 30.0) is None


def test_same_health_wording_is_not_repeated():
    """1834/1819/1804 取整后都是"一千八百"，连说三遍等于卡带。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    first = arbiter.observe(_frame([_event(1, "you_damaged", {"health": 1834, "delta": 66})]), 0.0)
    assert "一千八百" in first

    assert arbiter.observe(
        _frame([_event(2, "you_damaged", {"health": 1819, "delta": 15})], seq=2), 30.0
    ) is None
    assert arbiter.observe(
        _frame([_event(3, "you_damaged", {"health": 1804, "delta": 15})], seq=3), 60.0
    ) is None
    # 血线掉到新的一档就又该说话了
    again = arbiter.observe(
        _frame([_event(4, "you_damaged", {"health": 1610, "delta": 194})], seq=4), 90.0
    )
    assert "一千六百" in again


# ------------------------------------------------- 去重身份与跨局（v2.2 / Story 3）
# 实测：新一局刚开打，插件对着上一局残留的 you_died 喊了
# "被击毁了…缓一下"，而真正的 battle_start（"开打了！"）一句都没播。
# 根因：去重键是 (arena_id, seq) 且换局清空已见集合 → 换局那一刻整窗残留事件全变"新的"。


def test_battle_two_start_is_spoken_when_seq_keeps_rising():
    """局1（seq 11..16）→ 局2 的 battle_start(seq 17)：跨局 seq 继续涨，开场白必须念。

    同时守"换局不许清已见集合"：局2 那一帧的窗口里还带着局1 的全部 4 条残留事件。
    """
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    battle1 = [
        _event(11, "battle_start", {"vehicle": "WZ-219"}),
        _event(12, "you_damaged", {"delta": 120, "health": 1780, "max_health": 1900}),
        _event(13, "you_died", {"deaths": 1}),
        _event(16, "battle_end", {"deaths": 1, "respawns": 1}),
    ]

    arbiter.prime(_frame(battle1, arena="arena-a"))          # 插件启动：静默 prime
    # 局1 窗口原样再出现一遍 → 一句都不许说
    assert arbiter.observe(_frame(battle1, arena="arena-a", seq=2), 1.0) is None

    # 局2：残留的 4 条 + 局2 的 battle_start(17)
    text = arbiter.observe(
        _frame([*battle1, _event(17, "battle_start", {"vehicle": "WZ-219"})],
               arena="arena-b", seq=3),
        40.0,
    )
    assert text is not None, "新一局的开场白被吞了"
    assert "开打了" in text, f"局2 第一句不是开场白：{text!r}"
    assert arbiter._max_seq_seen == 17, "见过的最大 seq 应随本帧推进（说明没被判重启）"


def test_arena_change_does_not_replay_leftover_events():
    """局2 的窗口里仍带着局1 的残留事件 → 只说 1 句，且必须是"开打了"。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    leftover = [_event(11, "you_died", {"deaths": 1}), _event(12, "battle_end")]

    assert arbiter.observe(_frame(leftover, arena="arena-1", seq=1), 0.0) is not None

    text = arbiter.observe(
        _frame([*leftover, _event(17, "battle_start", {"vehicle": "WZ-219"})],
               arena="arena-2", seq=2),
        40.0,
    )
    assert text is not None
    assert "开打了" in text
    assert "被击毁" not in text, "上一局的残留 you_died 被重放了"


def test_data_end_restart_primes_silently():
    """数据端重启（seq 回退）→ 静默 prime：这一帧一句都不说，之后照常工作。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert arbiter.observe(_frame([_event(11, "you_killed")], seq=1), 0.0)

    # mod 进程重开：seq 从 1 重新开始，窗口里全是"重启前就发生"的历史事件
    assert arbiter.observe(
        _frame([_event(1, "you_died", {"deaths": 1}), _event(2, "battle_end")], seq=2), 60.0
    ) is None, "重启后的第一帧不许复述历史（那是 prime 的活）"

    # 重启后真正的新事件必须照常念（去重表已清空、新基线已建立）
    text = arbiter.observe(
        _frame([_event(2, "battle_end"), _event(3, "you_killed")], seq=3), 61.0
    )
    assert text is not None
    assert "击毁一台" in text


# ------------------------------------------------------ 协助伤害话术（v2.2 / Story 4）


def test_assisted_speaks_total():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    text = arbiter.observe(
        _frame([_event(1, "you_assisted", {"total": 340, "delta": 60})]), 0.0
    )

    assert text is not None
    assert "三百四十" in text              # 与血量同一套取整口径（到十位）
    assert "帮得狠" not in text            # 60 不算"这一波帮得狠"
    assert "340" not in text               # 不许念阿拉伯数字


def test_assisted_big_delta_gets_prefix():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    text = arbiter.observe(
        _frame([_event(1, "you_assisted", {"total": 1240, "delta": 260})]), 0.0
    )

    assert text is not None
    assert "帮得狠" in text
    assert "一千二百" in text              # 千位以上只到百位


def test_assisted_requires_valid_total():
    """total 不是非负整数 → 一个字都不说（宁可没有，不许编数字）。"""
    for payload in ({}, {"total": None}, {"total": -5}, {"total": "340"}, {"total": 0},
                    {"total": True}):
        arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
        assert arbiter.observe(
            _frame([_event(1, "you_assisted", payload)]), 0.0
        ) is None, f"payload={payload!r} 不该说话"


def test_assisted_is_deferrable_latest_wins():
    """连续协助只念最新那句"攒到多少"（排队里被新值顶掉）。"""
    assert "you_assisted" in arbiter_mod.DEFERRABLE
    assert "you_assisted" in arbiter_mod.LATEST_WINS

    arbiter = arbiter_mod.Arbiter(min_gap_s=8.0)
    assert arbiter.observe(_frame([_event(1, "battle_start")], seq=1), 0.0)

    # 全局间隔内 → 排队（不是丢掉）
    assert arbiter.observe(
        _frame([_event(2, "you_assisted", {"total": 340, "delta": 60})], seq=2), 1.0
    ) is None
    # 新的协助值顶掉旧的（旧那句过时了）
    assert arbiter.observe(
        _frame([_event(3, "you_assisted", {"total": 880, "delta": 540})], seq=3), 2.0
    ) is None

    text = arbiter.observe(_frame([], seq=4), 9.0)
    assert text is not None
    assert "八百八十" in text
    assert "三百四十" not in text


def test_assisted_priority_between_kill_and_end():
    priority = arbiter_mod.PRIORITY
    assert priority["you_assisted"] == 25
    assert priority["you_killed"] > priority["you_assisted"] > priority["battle_end"]
