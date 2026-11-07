"""v1.30 / P5：人数差话术矩阵（6 档 + 波次降级）+ 3 秒防抖 + 禁词放宽。

**唯一一次禁词松动**（已确认）：「对敌方队伍剩余数量的播报禁词可以适当放宽，
因为双方人数是可以直接看到的」——只放行「敌方/敌人/对面 + 数量」这一种句式。
位置/方位/血量百分比/装填… 一个字都没松（见 ``test_enemy_count_phrase_is_the_only_relaxation``）。

降级（协议 §4.2）：``mode_kind != "standard"`` 或 ``enemy_total_known is not True`` 时
敌方数字**根本不可用**（波次局会一直补人、未知局数不出来），那时只说我方存活，
一个敌方数字都不出现。
"""

from __future__ import annotations

import sdk_stub

_pkg = sdk_stub.load_plugin_module()
arbiter_mod = _pkg.arbiter
state_reader = _pkg.state_reader
phrases_mod = _pkg.phrases


def _frame(seq: int, ally: int, enemy, *, known: bool = True, mode_kind: str | None = "standard",
           arena: str = "arena-1") -> state_reader.Frame:
    return state_reader.Frame(
        status=state_reader.STATUS_OK,
        schema=1,
        seq=seq,
        ts=1000.0,
        mode="battle",
        arena_id=arena,
        mode_kind=mode_kind,
        me={"vehicle_name": "WZ-219", "health_ratio": 0.8, "alive": True},
        team={
            "ally_alive": ally,
            "ally_total": 15,
            "enemy_alive": enemy,
            "enemy_total": 15,
            "enemy_total_known": known,
        },
        events=(),
    )


def _arbiter(**kwargs) -> arbiter_mod.Arbiter:
    return arbiter_mod.Arbiter(min_gap_s=0.0, **kwargs)


def _wording(ally: int, enemy, *, baseline_enemy=15, **kwargs) -> str | None:
    """→ 这一帧会说的那句话（只走"检测 + 渲染"，不经过档位仲裁）。

    ⚠️ 为什么不用 ``observe``：清场（敌方 0）和大劣势这两档，同帧本来就有
    ``enemy_low`` / ``team_behind`` 这类 **warning** 句子，人数报点是 ``info`` 档，
    按设计就要让位（/档位表）。话术矩阵锁的是"这一档怎么说"，
    让位逻辑由 ``test_numbers_report_yields_to_warning_lines`` 单独锁。
    """
    arbiter = _arbiter()
    assert arbiter.observe(_frame(1, 15, baseline_enemy), 0.0) is None
    events = arbiter._derive(_frame(2, ally, enemy, **kwargs), 100.0)
    numbers = [event for event in events if event["id"] == "team_numbers"]
    assert len(numbers) == 1, events
    return arbiter._render(numbers[0], _frame(2, ally, enemy, **kwargs))


# ------------------------------------------------------------------ 6 档话术
def test_big_advantage_names_the_remaining_enemy_count():
    """大优势（差 ≥3）允许说「敌方还剩 N 个」—— 这是唯一放行的句式。"""
    assert _wording(15, 12) == "收尾了，敌方还剩 12 个。"


def test_zero_left_says_cleared_instead_of_zero():
    """清场了不说"还剩 0 个"（听着像没打完）。"""
    assert _wording(15, 0) == "都清完了，收尾吧。"


def test_small_advantage_reports_the_gap():
    assert _wording(15, 14) == "多 1 个，压上去。"


def test_even_reports_both_sides():
    assert _wording(15, 15, baseline_enemy=14) == "势均力敌，15 对 15。"


def test_small_disadvantage_reports_the_gap():
    assert _wording(15, 17) == "少 2 个，稳一稳，等支援。"


def test_big_disadvantage_has_no_numbers():
    assert _wording(10, 15) == "人差太多了，收缩防守。"


# ------------------------------------------------------- 波次 / 未知降级
def test_wave_mode_reports_my_side_only():
    """波次局：敌人会一直补，只说我自己这边 + 波次语境，一个敌方数字都不出现。"""
    said = _wording(8, None, known=False, mode_kind="wave")

    assert said == "这局一直补人，我方还剩 8 个，别恋战。"
    for word in ("敌方", "敌人", "对面", "还剩 0"):
        assert word not in said, (word, said)


def test_unknown_mode_reports_my_side_only():
    said = _wording(7, None, known=False, mode_kind="unknown")

    assert said == "人数看不到，我方还剩 7 个，稳着打。"
    assert "敌方" not in said and "敌人" not in said and "对面" not in said, said


def test_standard_but_unknown_total_degrades_and_drops_enemy_numbers():
    """``mode_kind == "standard"`` 但 ``enemy_total_known`` 不是 true → 敌方数字不可用。"""
    said = _wording(9, None, known=False)

    assert said == "人数看不到，我方还剩 9 个，稳着打。"


def test_wave_mode_wins_even_when_someone_fills_enemy_alive():
    """波次局就算 ``enemy_alive`` 偶尔有值，也只说我方（``enemy_total_known`` 为假）。"""
    said = _wording(6, 12, known=False, mode_kind="wave")

    assert said is not None
    assert "12" not in said and "敌方" not in said, said


def test_missing_mode_kind_is_silence_not_a_guess():
    """老 mod（没有 ``mode_kind``）：连"这局数不出来"都不许断言 → 闭嘴。"""
    arbiter = _arbiter()
    assert arbiter.observe(_frame(1, 15, 15, mode_kind=None), 0.0) is None
    assert arbiter.observe(_frame(2, 15, 12, mode_kind=None), 100.0) is None
    assert arbiter.observe(_frame(3, 15, 3, mode_kind=None), 200.0) is None


# ------------------------------------------------ 端到端：真的从嘴里说出来
def test_small_advantage_reaches_the_speaker():
    arbiter = _arbiter()
    assert arbiter.observe(_frame(1, 15, 15), 0.0) is None
    assert arbiter.observe(_frame(2, 15, 14), 100.0) == "多 1 个，压上去。"
    assert arbiter.last_cue.level == arbiter_mod.LEVEL_INFO


def test_numbers_report_yields_to_warning_lines():
    """清场那一帧 ``enemy_low``（warning）已经要说话 → 人数报点（info）让位。"""
    arbiter = _arbiter()
    assert arbiter.observe(_frame(1, 15, 15), 0.0) is None
    said = arbiter.observe(_frame(2, 15, 0), 100.0)

    assert said == "我们这边快收尾了，别放跑，{MASTER_NAME}。", said
    assert "清完" not in said


# ------------------------------------------------------------------ 防抖
def test_same_tier_never_repeats():
    arbiter = _arbiter()
    assert arbiter.observe(_frame(1, 15, 15), 0.0) is None
    assert arbiter.observe(_frame(2, 15, 14), 100.0) is not None
    for seq, now in ((3, 110.0), (4, 300.0), (5, 900.0)):
        assert arbiter.observe(_frame(seq, 15, 14), now) is None


def test_same_side_change_within_three_seconds_is_suppressed_then_retried():
    """同侧换档（+2 → +3）不到 3 秒 → 压掉；时间到了再补说（不吞档位）。"""
    arbiter = _arbiter()
    assert arbiter.observe(_frame(1, 15, 15), 0.0) is None            # even 基线
    assert arbiter.observe(_frame(2, 15, 14), 10.0) == "多 1 个，压上去。"
    assert arbiter.observe(_frame(3, 15, 13), 10.5) is None            # 同一档：重复
    assert arbiter.observe(_frame(4, 15, 12), 11.0) is None            # 同侧换档 + 不到 3 秒
    assert arbiter.observe(_frame(5, 15, 12), 13.0) == "收尾了，敌方还剩 12 个。"


def test_flip_from_advantage_to_disadvantage_speaks_immediately():
    """「从优势翻到劣势」是局面反转 → 立刻说，3 秒防抖压不住它。"""
    arbiter = _arbiter()
    assert arbiter.observe(_frame(1, 15, 15), 0.0) is None
    assert arbiter.observe(_frame(2, 15, 14), 10.0) == "多 1 个，压上去。"
    # 2 秒不到就翻成劣势：必须立刻说
    assert arbiter.observe(_frame(3, 15, 17), 12.0) == "少 2 个，稳一稳，等支援。"


def test_flip_from_disadvantage_to_advantage_also_speaks_immediately():
    arbiter = _arbiter()
    assert arbiter.observe(_frame(1, 15, 15), 0.0) is None
    assert arbiter.observe(_frame(2, 15, 17), 10.0) == "少 2 个，稳一稳，等支援。"
    assert arbiter.observe(_frame(3, 15, 12), 11.0) == "收尾了，敌方还剩 12 个。"


def test_first_frame_of_a_battle_only_builds_the_baseline():
    """换局首帧只建基线：接手的局面不是"刚发生的事"（和 enemy_low 同一条规矩）。"""
    arbiter = _arbiter()
    assert arbiter.observe(_frame(1, 3, 9), 0.0) is None
    # 第二帧同档（3v8 仍然是大劣势）→ 也不说
    assert arbiter.observe(_frame(2, 3, 8), 1.0) is None
    # 真的换档才说
    assert arbiter.observe(_frame(3, 8, 9), 20.0) == "少 1 个，稳一稳，等支援。"


# ----------------------------------------------------- 禁词放宽的**边界**
def test_enemy_count_phrase_is_the_only_relaxation():
    """放行的只有「敌方/敌人/对面 + 数量」；其余一律照旧判红。"""
    allowed = ("敌方还剩 3 个。", "敌人只剩两台。", "对面剩 1 个了。")
    for text in allowed:
        assert not arbiter_mod.preach_is_forbidden(text), text

    still_red = (
        "敌方在左边。",              # 没有数量
        "敌人来了。",                # 没有数量
        "对面装填好了。",            # 夹带了别的敌方词
        "敌方还剩 3 个，装填好了。",   # 豁免只挖掉人数那一段
        "敌方还剩 3 个，位置在左边。",
        "对面还剩 2 个，瞄准它。",
    )
    for text in still_red:
        assert arbiter_mod.preach_is_forbidden(text), text


def test_forbidden_word_table_lost_nothing():
    """词表一个词都没删（"放宽"是读端先挖人称段，不是改表）。"""
    enemy = arbiter_mod.FORBIDDEN_WORD_GROUPS["ENEMY"]
    for word in ("位置", "坐标", "方位", "来袭", "预测", "建议", "透视", "瞄准",
                 "弱点", "可见性", "透明度", "标记", "箭头", "装填", "reload"):
        assert word in enemy, word


def test_no_tier_line_mentions_blood_or_percent():
    """6 档（+ 降级两档 + 清场）全部话术：不含「血」「%」（血量百分比照旧禁）。"""
    tiers = dict(phrases_mod.DEFAULT_PHRASES["team_numbers"])
    assert set(tiers) >= set(arbiter_mod.NUMBERS_TIERS) | {"cleared"}
    for tier, lines in tiers.items():
        for line in lines:
            assert "血" not in line, (tier, line)
            assert "%" not in line, (tier, line)
            assert "％" not in line, (tier, line)


def test_no_tier_line_leaks_other_enemy_words():
    """挖掉人数豁免段之后，6 档话术里一个别的敌方词都不许有。"""
    for tier, lines in phrases_mod.DEFAULT_PHRASES["team_numbers"].items():
        for line in lines:
            rendered = line.replace("{enemy}", "3").replace("{ally}", "8").replace("{diff}", "3")
            masked = arbiter_mod._mask_enemy_count_phrases(rendered)
            for word in ("敌方", "敌人", "对面", "位置", "方位", "装填", "瞄准", "标记"):
                assert word not in masked, (tier, rendered)


def test_derived_numbers_payload_carries_only_counts():
    """派生 payload 只带 tier / 两边人数：位置、血量、车型一概不进。"""
    arbiter = _arbiter()
    arbiter.observe(_frame(1, 15, 15), 0.0)
    events = arbiter._derive(_frame(2, 15, 12), 100.0)

    numbers = [event for event in events if event["id"] == "team_numbers"]
    assert len(numbers) == 1
    assert set(numbers[0]["payload"]) == {"tier", "ally", "enemy"}
    assert arbiter_mod.PAYLOAD_ALLOWED_KEYS >= {"tier", "ally", "enemy"}


def test_state_reader_reads_top_level_mode_kind(tmp_path):
    """``mode_kind`` 是**顶层**键（协议 §4.2）：拿不到队号时 team 是 null，它是唯一的线索。"""
    path = tmp_path / "state.json"
    path.write_text(
        '{"schema": 1, "seq": 3, "ts": 1000.0, "status": "ok", "mode_kind": "wave", "team": null,'
        ' "me": {"alive": true}, "events": []}',
        encoding="utf-8",
    )
    frame = state_reader.read_state_file(path)
    assert frame.mode_kind == "wave"
    assert frame.team is None
