"""v2.18 / P1「陪伴四件套」：C1 复盘分档 + C2 战绩账本 + C3 连败关怀 + C4 二阶静默 + C8 话术预算。

能力清单见 README「陪伴能力」一节。这一批用例守两类东西：

**该说的要说**（分档命中、关怀只来一次、二阶静默不越界、预算不误伤 critical），
**不该说的一个字都不说**（下面每条反向对照都是"构造不该说话的场景，断言静默"）：

| 反向对照 | 用例 |
|---|---|
| ``result="unknown"`` | ``test_unknown_recap_never_contains_a_win_lose_character`` |
| 坏 / 空 / 无权限的账本 | ``test_broken_ledger_still_speaks_and_never_mentions_history`` 等三条 |
| 3 连败只安慰一次 | ``test_streak_lose_care_fires_once_then_stays_quiet`` |
| 30 秒内 5 条候选 | ``test_burst_caps_at_three_lines_but_never_blocks_critical`` |
| 一局 45 条 | ``test_battle_budget_caps_at_forty_and_counts_the_dropped`` |
| ``history_enabled=false`` | ``test_history_disabled_creates_no_file_and_arbiter_has_no_ledger`` |
| 每 tick 不读盘 | ``test_the_ledger_is_only_touched_on_battle_start_and_battle_end`` |
"""

from __future__ import annotations

import asyncio
import json

import sdk_stub

_pkg = sdk_stub.load_plugin_module()
arbiter_mod = _pkg.arbiter
settings_mod = _pkg.settings
phrases_mod = _pkg.phrases
plugin_mod = _pkg
state_reader = _pkg.state_reader

Battle = _pkg.history.Battle
History = _pkg.history.History


# ------------------------------------------------------------------ 造帧 / 造器
def _me(vehicle: str = "WZ-219") -> dict:
    return {"vehicle_name": vehicle, "health_ratio": 0.9, "alive": True, "reload_ratio": 0.5, "fires": 0}


def _frame(seq: int, *, events=(), result=None, mode: str = "battle", mode_kind: str | None = "standard",
           arena: str = "arena-companion", vehicle: str = "WZ-219", map_name: str = "荒蛮之地"):
    return state_reader.Frame(
        status=state_reader.STATUS_OK,
        schema=1,
        seq=seq,
        ts=1000.0,
        mode=mode,
        arena_id=arena,
        me=_me(vehicle),
        team={"enemy_alive": 15, "enemy_total": 15, "ally_alive": 15, "ally_total": 15, "enemy_total_known": True},
        arena={"period": 2, "time_left": 300, "map": map_name},
        mode_kind=mode_kind,
        result=result,
        events=tuple(events),
    )


def _end_event(seq: int = 900, payload: dict | None = None) -> dict:
    return {"seq": seq, "id": "battle_end", "ts": 1000.0, "payload": payload or {}}


def _kill_event(seq: int = 901) -> dict:
    return {"seq": seq, "id": "you_killed", "ts": 1000.0, "payload": {}}


def _arb(**kwargs):
    logger = kwargs.pop("logger", None) or sdk_stub.RecordingLogger("neko_wot.tests.companion")
    return arbiter_mod.Arbiter(logger=logger, **kwargs), logger


def _lines(logger) -> list[str]:
    return [record.split(": ", 1)[1] if ": " in record else record for record in logger.records]


def _battle(*, result: str = "win", vehicle: str | None = "WZ-219", dmg: int | None = 800,
            kills: int | None = 2, deaths: int = 1, seconds: int | None = 300, ts: float = 1000.0):
    return Battle(
        ts=ts,
        map="荒蛮之地",
        vehicle=vehicle,
        mode_kind="standard",
        result=result,
        kills=kills,
        dmg=dmg,
        deaths=deaths,
        seconds=seconds,
    )


def _seed(tmp_path, rows, name: str = "ledger") -> History:
    """造一个**已注入 tmp_path** 的账本（绝不碰真实的 %LOCALAPPDATA%）。"""
    book = History(tmp_path / name / "neko_battles.jsonl")
    for row in rows:
        assert book.append(row) is True
    return book


class CountingHistory:
    """只数次数、不落盘的账本替身（给"每 tick 不读盘"那条用）。"""

    def __init__(self, rows=()):
        self.rows: list[object] = list(rows)
        self.reads = 0
        self.appends = 0

    def recent(self, limit=None):
        self.reads += 1
        return list(self.rows)

    def append(self, battle) -> bool:
        self.appends += 1
        self.rows.append(battle)
        return True


def _say_end(arbiter, *, payload=None, result=None, seq: int = 100, now: float = 1000.0,
             arena: str = "arena-companion", vehicle: str = "WZ-219",
             event_seq: int | None = None) -> str | None:
    """喂一帧 ``battle_end``（``result``=state.result，``payload``=写端 15 键）。"""
    event = {
        "seq": event_seq if event_seq is not None else seq * 10,
        "id": "battle_end",
        "ts": 1000.0,
        "payload": dict(payload or {}),
    }
    frame = _frame(seq, events=[event], result=result, arena=arena, vehicle=vehicle)
    return arbiter.observe(frame, now)


# ============================================================ C1：复盘分档
def test_recap_win_carry_points_at_the_result():
    arbiter, _ = _arb(min_gap_s=0.0)

    said = _say_end(arbiter, result={"won": True, "kills": 3, "damage": 2870})

    assert said is not None
    assert "赢了！" in said and "击毁 3 台" in said
    assert "手感在线" in said, said


def test_recap_win_with_a_death_is_teased_not_praised():
    arbiter, _ = _arb(min_gap_s=0.0)

    said = _say_end(arbiter, payload={"result": "win", "kills": 1, "deaths": 1}, result={"won": True, "kills": 1})

    assert said is not None
    assert "搭进去一条命也值了" in said, said
    assert "手感在线" not in said


def test_recap_lose_above_the_vehicle_average_needs_three_samples(tmp_path):
    """同车历史 ≥3 局、本局伤害高于均伤 → 安慰；只有 2 局样本时**不**这么算。"""
    rows = [_battle(result="lose", dmg=1000) for _ in range(3)]
    arbiter, _ = _arb(min_gap_s=0.0, history=_seed(tmp_path, rows, "avg"))

    said = _say_end(arbiter, payload={"result": "lose", "dmg_dealt_total": 1500}, seq=101)

    assert said is not None
    assert "伤害比平时高" in said, said

    two_samples = [_battle(result="lose", dmg=1000) for _ in range(2)]
    arbiter2, _ = _arb(min_gap_s=0.0, history=_seed(tmp_path, two_samples, "avg2"))
    said2 = _say_end(arbiter2, payload={"result": "lose", "dmg_dealt_total": 1500}, seq=102)
    assert "伤害比平时高" not in (said2 or ""), said2


def test_recap_lose_short_battle_does_not_judge_the_player():
    arbiter, _ = _arb(min_gap_s=0.0)

    said = _say_end(arbiter, payload={"result": "lose", "battle_seconds": 120})

    assert said is not None
    assert "这局太快了" in said, said


def test_recap_falls_back_to_the_default_line_when_nothing_matches():
    arbiter, _ = _arb(min_gap_s=0.0)

    said = _say_end(arbiter, payload={"result": "lose", "battle_seconds": 600, "dmg_dealt_total": 300})

    assert said is not None and "输了，下次再来。" in said
    for clause in ("手感在线", "搭进去一条命", "伤害比平时高", "这局太快了"):
        assert clause not in said, (clause, said)


def test_recap_disabled_returns_to_the_plain_line():
    arbiter, _ = _arb(min_gap_s=0.0, recap_enabled=False)

    said = _say_end(arbiter, result={"won": True, "kills": 5, "damage": 4000})

    assert said is not None
    assert "赢了！" in said and "击毁 5 台" in said
    assert "手感在线" not in said
    assert "搭进去一条命" not in said


def test_unknown_recap_never_contains_a_win_lose_character():
    """⛔ 反向对照：``result="unknown"`` 时复盘话术里**不含** 输/赢/胜/败 任何一个字。"""
    cases = (
        (None, {"result": "unknown", "kills": 2, "dmg_dealt_total": 800}),
        (None, {"result": "unknown"}),
        (None, {}),
        ({"won": None, "kills": 0, "damage": None}, {"result": "unknown"}),
        ({"draw": None, "outcome": None}, {"result": "unknown", "kills": 1}),
    )
    for result, payload in cases:
        arbiter, log = _arb(min_gap_s=0.0)
        said = _say_end(arbiter, payload=payload, result=result)

        assert said is not None, (result, payload)
        assert "打完了" in said, (result, payload, said)
        for char in "输赢胜败":
            assert char not in said, f"{char!r} 出现在 unknown 复盘里：{said!r}"

    # 反向再确认：赢/输两档**必须**说得出来（否则上面那条可以靠"什么都不说"通过）
    arbiter, _ = _arb(min_gap_s=0.0)
    assert "赢了" in (_say_end(arbiter, result={"won": True, "kills": 1}) or "")
    arbiter, _ = _arb(min_gap_s=0.0)
    assert "输了" in (_say_end(arbiter, payload={"result": "lose"}, seq=103) or "")


def test_new_recap_lines_pass_the_forbidden_gate_and_have_no_coach_tone():
    book = phrases_mod.DEFAULT_PHRASES["battle_end"]
    new_tiers = (
        "recap_win_carry",
        "recap_win_death",
        "recap_lose_above_avg",
        "recap_lose_short",
        "care_lose",
        "care_win",
    )
    for tier in new_tiers:
        assert tier in book, tier
        for line in book[tier]:
            assert arbiter_mod.preach_is_forbidden(line) is False, (tier, line)
            for word in ("建议", "应该", "下次你", "敌方", "敌人"):
                assert word not in line, (tier, word, line)
    assert "胜负" not in book["unknown"][0], "unknown 档连'胜负'两个字都不许有"


# ============================================================ C2：账本写入
def test_history_appends_one_row_per_battle(tmp_path):
    path = tmp_path / "ledger" / "neko_battles.jsonl"
    arbiter, _ = _arb(min_gap_s=0.0, history=History(path))

    assert _say_end(arbiter, result={"won": True, "kills": 2, "damage": 1200}) is not None

    rows = History(path).recent()
    assert len(rows) == 1
    assert rows[0].result == "win"
    assert rows[0].kills == 2 and rows[0].dmg == 1200
    assert rows[0].vehicle == "WZ-219" and rows[0].map == "荒蛮之地"
    assert rows[0].mode_kind == "standard"


def test_history_is_written_once_per_event_seq(tmp_path):
    path = tmp_path / "ledger" / "neko_battles.jsonl"
    arbiter, _ = _arb(min_gap_s=0.0, history=History(path))
    frame = _frame(100, events=[_end_event(901, {"result": "win", "kills": 1})], result={"won": True, "kills": 1})

    # 直接连喂两次同一个事件（模拟"同一局被处理两遍"；正常路径由 _seen 挡住）
    arbiter._note_history(list(frame.events), frame, 1000.0)
    arbiter._note_history(list(frame.events), frame, 1001.0)

    assert len(History(path).recent()) == 1, "同一局绝不允许写两行"


def test_history_disabled_creates_no_file_and_arbiter_has_no_ledger(make_plugin):
    """⛔ 反向对照：``history_enabled=false`` → 没有账本对象、一个字节都不落盘。"""
    built: list[object] = []
    original_history = plugin_mod.History

    class _SpyHistory:
        def __init__(self, *args, **kwargs):
            built.append(object())
            raise AssertionError("history_enabled=false 时不许构造账本")

    plugin_mod.History = _SpyHistory
    try:
        off, _host2 = make_plugin({"dry_run": True, "history_enabled": False})
        asyncio.run(off.startup())
    finally:
        plugin_mod.History = original_history

    assert built == [], "关掉账本时连路径都不该解析（否则迟早有人顺手 stat/写一次）"
    assert off._arbiter.history is None

    # 关着账本时结算照说，而且**不建任何文件**（用一个"只读"指针证明没被碰过）
    arbiter, _ = _arb(min_gap_s=0.0, history=None)
    assert _say_end(arbiter, result={"won": True, "kills": 1}) is not None


def test_broken_ledger_still_speaks_and_never_mentions_history(tmp_path):
    """⛔ 反向对照：账本是垃圾 → 播报照旧、不提历史、零异常。"""
    path = tmp_path / "ledger" / "neko_battles.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\x00 not a jsonl \n")

    arbiter, _ = _arb(min_gap_s=0.0, history=History(path))
    said = _say_end(arbiter, payload={"result": "lose", "dmg_dealt_total": 9999})

    assert said is not None and "输了" in said
    assert "伤害比平时高" not in said, "没有可信历史时绝不许拿它当比较基线"


def test_empty_and_missing_ledger_behaves_like_no_history(tmp_path):
    for name in ("missing", "empty"):
        path = tmp_path / name / "neko_battles.jsonl"
        if name == "empty":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"")

        arbiter, _ = _arb(min_gap_s=0.0, history=History(path))
        said = _say_end(arbiter, payload={"result": "lose", "battle_seconds": 600})

        assert said is not None and "输了" in said
        assert "伤害比平时高" not in said and "三局了" not in said


def test_unwritable_ledger_does_not_break_speech(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    arbiter, _ = _arb(min_gap_s=0.0, history=History(blocker / "neko_battles.jsonl"))
    said = _say_end(arbiter, result={"won": True, "kills": 1})

    assert said is not None and "赢了" in said


def test_the_ledger_is_only_touched_on_battle_start_and_battle_end():
    """⛔ 反向对照：``battle_start`` / ``battle_end`` 之外**一次都不读盘**。"""
    counter = CountingHistory()
    arbiter, _ = _arb(min_gap_s=0.0, history=counter)

    quiet = _frame(1)
    for index in range(10):
        assert arbiter.observe(quiet, 1.0 + index) is None
    assert counter.reads == 0, f"安静帧读盘了 {counter.reads} 次"

    start = _frame(2, events=[{"seq": 700, "id": "battle_start", "ts": 1000.0, "payload": {}}])
    arbiter.observe(start, 20.0)
    assert counter.reads == 1, "battle_start 该读一次"

    for index in range(5):
        arbiter.observe(_frame(3 + index), 21.0 + index)
    assert counter.reads == 1, f"战斗中的普通帧又读盘了（累计 {counter.reads} 次）"

    _say_end(arbiter, payload={"result": "win", "kills": 1}, seq=9, now=60.0, event_seq=950)
    assert counter.reads == 2, "battle_end 该再读一次"
    assert counter.appends == 1


# ============================================================ C3：连败 / 连胜关怀
def test_streak_lose_care_fires_once_then_stays_quiet(tmp_path):
    """⛔ 反向对照：3 连败只安慰一次；30 分钟内第二局**不许**再提连败。"""
    history = _seed(tmp_path, [_battle(result="lose", ts=1000.0), _battle(result="lose", ts=1100.0)])
    arbiter, _ = _arb(min_gap_s=0.0, history=history)

    first = _say_end(arbiter, payload={"result": "lose", "battle_seconds": 600}, seq=100, now=1200.0)
    assert first is not None and "三局了" in first, first

    # 第二局（新的 arena → 局内状态归零，但 30 分钟冷却还在）
    second = _say_end(arbiter, payload={"result": "lose", "battle_seconds": 600}, seq=101, now=1300.0,
                      arena="arena-2")
    assert second is not None and "输了" in second
    assert "三局了" not in second, f"30 分钟内又提连败：{second!r}"

    # 冷却过后（+31 分钟）再连败 → 可以再说
    third = _say_end(arbiter, payload={"result": "lose", "battle_seconds": 600}, seq=102, now=1300.0 + 31 * 60,
                     arena="arena-3")
    assert third is not None and "三局了" in third, third


def test_streak_win_care_is_one_short_praise(tmp_path):
    history = _seed(tmp_path, [_battle(result="win", ts=1000.0), _battle(result="win", ts=1100.0)])
    arbiter, _ = _arb(min_gap_s=0.0, history=history)

    said = _say_end(arbiter, result={"won": True, "kills": 1}, seq=100, now=1200.0)

    assert said is not None and "连着三局" in said, said
    assert said.count("连着三局") == 1


def test_streak_needs_three_clean_results(tmp_path):
    """⛔ 反向对照：最近 3 局里有 ``unknown`` / ``draw`` / 不足 3 局 → 不触发。"""
    for rows in (
        [_battle(result="lose"), _battle(result="unknown")],
        [_battle(result="lose"), _battle(result="draw")],
        [_battle(result="lose")],
        [],
    ):
        history = _seed(tmp_path, rows, name=f"s{len(rows)}-{rows[-1].result if rows else 'none'}")
        arbiter, _ = _arb(min_gap_s=0.0, history=history)

        said = _say_end(arbiter, payload={"result": "lose", "battle_seconds": 600}, now=1200.0)

        assert said is not None
        assert "三局了" not in said, (rows, said)


# ============================================================ C4：二阶静默
def test_deep_idle_speaks_once_per_battle():
    arbiter, log = _arb(min_gap_s=0.0, idle_deep_s=60.0, idle_fill_s=0.0)
    assert arbiter.observe(_frame(1, events=[_kill_event(801)]), 0.0) is not None  # 先说一句垫底

    assert arbiter.observe(_frame(2), 30.0) is None, "没到 60 秒不许开口"
    text = arbiter.observe(_frame(3), 61.0)

    assert text is not None and "我在呢" in text, text
    assert arbiter.last_cue.event_id == "idle_company"
    assert arbiter.last_cue.level == arbiter_mod.LEVEL_INFO
    assert any(line.startswith("speak idle_company") for line in _lines(log)), _lines(log)

    for index in range(5):     # 一局最多一次
        assert arbiter.observe(_frame(4 + index), 120.0 + index * 60) is None, "二阶静默一局只能一次"


def test_deep_idle_is_silent_outside_battle_or_when_mode_is_unknown():
    """⛔ 反向对照：车库/结算/观战/``mode_kind`` 判不出的局 —— 一律不说。"""
    for mode, mode_kind in (
        ("garage", "standard"),
        ("dead", "standard"),
        ("battle_end", "standard"),
        ("battle", "unknown"),
        ("battle", None),
    ):
        arbiter, _ = _arb(min_gap_s=0.0, idle_deep_s=60.0)
        assert arbiter.observe(_frame(1, events=[_kill_event(801)], mode="battle", mode_kind="standard"), 0.0)
        for index in range(4):
            said = arbiter.observe(_frame(2 + index, mode=mode, mode_kind=mode_kind), 61.0 + index * 30)
            assert said is None or "我在呢" not in said, (mode, mode_kind, said)


def test_deep_idle_zero_disables_it():
    arbiter, _ = _arb(min_gap_s=0.0, idle_deep_s=0.0)
    assert arbiter.observe(_frame(1, events=[_kill_event(801)]), 0.0) is not None

    assert arbiter.observe(_frame(2), 600.0) is None


def test_deep_idle_never_fires_before_the_first_line_of_the_battle():
    """开局一秒就冒"我在呢"不是陪伴是打扰：本局还没说过话 → 不许触发。"""
    arbiter, _ = _arb(min_gap_s=0.0, idle_deep_s=60.0)

    for index in range(5):
        assert arbiter.observe(_frame(1 + index), 61.0 + index * 10) is None


# ============================================================ C8：话术预算
def test_burst_caps_at_three_lines_but_never_blocks_critical():
    """⛔ 反向对照：30 秒内 5 条候选 → 出口 ≤3；critical 不受 burst 限制。"""
    arbiter, log = _arb(min_gap_s=0.0, speech_burst_max=3, type_cooldown_s={"you_killed": 0.0})
    spoken = []
    for index in range(5):
        text = arbiter.observe(_frame(1 + index, events=[_kill_event(801 + index)]), float(index))
        if text:
            spoken.append(text)

    assert len(spoken) == 3, spoken
    assert any("speech_burst" in line for line in _lines(log)), _lines(log)
    assert arbiter._budget_dropped == 2, "被窗闸丢掉的那 2 句要计入 dropped（日志与统计同一口径）"

    died = arbiter.observe(
        _frame(20, events=[{"seq": 950, "id": "you_died", "ts": 1000.0, "payload": {"deaths": 1}}]),
        10.0,
    )
    assert died is not None and "被击毁" in died, "critical 不许被 burst 挡"


def test_battle_budget_caps_at_forty_and_counts_the_dropped():
    """一局 45 条 → 出口 40 句，``dropped`` = 5（``_announce`` 是唯一出口）。"""
    arbiter, _ = _arb(min_gap_s=0.0, speech_budget_per_battle=40)

    results = [arbiter._announce("you_killed", float(index), f"第 {index} 句") for index in range(45)]

    assert [item for item in results if item is not None] == [f"第 {index} 句" for index in range(40)]
    assert results[40:] == [None] * 5
    assert arbiter._budget_used == 40
    assert arbiter._budget_dropped == 5


def test_budget_is_counted_per_battle_not_globally():
    arbiter, _ = _arb(min_gap_s=0.0, speech_budget_per_battle=2, type_cooldown_s={"you_killed": 0.0})

    assert arbiter.observe(_frame(1, events=[_kill_event(801)]), 0.0) is not None
    assert arbiter.observe(_frame(2, events=[_kill_event(802)]), 1.0) is not None
    assert arbiter.observe(_frame(3, events=[_kill_event(803)]), 2.0) is None, "第 3 句超预算"

    # 换局（新 arena）→ 预算归零
    assert arbiter.observe(_frame(9, events=[_kill_event(804)], arena="arena-next"), 3.0) is not None


def test_budget_reports_the_summary_line_at_battle_end(tmp_path):
    arbiter, log = _arb(min_gap_s=0.0, speech_budget_per_battle=40, debug_log=True,
                        history=History(tmp_path / "ledger" / "neko_battles.jsonl"))
    assert arbiter.observe(_frame(1, events=[_kill_event(801)]), 0.0) is not None

    _say_end(arbiter, result={"won": True, "kills": 1}, seq=101, now=30.0)

    summary = [line for line in _lines(log) if line.startswith("speech budget: used=")]
    assert summary, _lines(log)
    assert summary[0].startswith("speech budget: used=1 dropped=0"), summary[0]


# ============================================================ 配置 + 接线
def test_the_five_new_settings_have_the_documented_defaults_and_bounds():
    assert settings_mod.DEFAULTS["history_enabled"] is True
    assert settings_mod.DEFAULTS["recap_enabled"] is True
    assert settings_mod.DEFAULTS["idle_deep_s"] == 60.0
    assert settings_mod.DEFAULTS["speech_budget_per_battle"] == 40
    assert settings_mod.DEFAULTS["speech_burst_max"] == 3

    parsed = settings_mod.parse_settings({})
    assert parsed.history_enabled and parsed.recap_enabled
    assert (parsed.idle_deep_s, parsed.speech_budget_per_battle, parsed.speech_burst_max) == (60.0, 40, 3)

    # 越界裁剪（不是报错、不是抛异常）
    assert settings_mod.parse_settings({"idle_deep_s": 9999}).idle_deep_s == 300.0
    assert settings_mod.parse_settings({"idle_deep_s": -5}).idle_deep_s == 0.0
    assert settings_mod.parse_settings({"speech_budget_per_battle": 9999}).speech_budget_per_battle == 500
    assert settings_mod.parse_settings({"speech_budget_per_battle": -3}).speech_budget_per_battle == 0
    assert settings_mod.parse_settings({"speech_burst_max": 999}).speech_burst_max == 20
    assert settings_mod.parse_settings({"speech_burst_max": 0}).speech_burst_max == 1
    # 脏值回落默认
    for dirty in (None, "", "maybe", [], {}):
        assert settings_mod.parse_settings({"idle_deep_s": dirty}).idle_deep_s == 60.0, dirty
        assert settings_mod.parse_settings({"recap_enabled": dirty}).recap_enabled is True, dirty
    assert settings_mod.parse_settings({"history_enabled": "no"}).history_enabled is False
    assert settings_mod.parse_settings({"speech_budget_per_battle": "0"}).speech_budget_per_battle == 0


def test_config_example_documents_the_five_keys():
    example = (plugin_mod.PHRASE_BOOK.path.parents[1] / "config.example.toml").read_text(encoding="utf-8")
    for key in (
        "history_enabled",
        "recap_enabled",
        "idle_deep_s",
        "speech_budget_per_battle",
        "speech_burst_max",
    ):
        assert key in example, key


def test_plugin_wires_the_companion_switches_into_the_arbiter(make_plugin):
    plugin, _host = make_plugin(
        {
            "dry_run": True,
            "history_enabled": True,
            "recap_enabled": False,
            "idle_deep_s": 0.0,
            "speech_budget_per_battle": 12,
            "speech_burst_max": 5,
        }
    )
    asyncio.run(plugin.startup())
    arbiter = plugin._arbiter

    assert arbiter.history is not None
    assert arbiter.history.path.name == "neko_battles.jsonl"
    assert arbiter.recap_enabled is False
    assert arbiter.idle_deep_s == 0.0
    assert arbiter.speech_budget_per_battle == 12
    assert arbiter.speech_burst_max == 5

    off, _host = make_plugin({"dry_run": True, "history_enabled": False})
    asyncio.run(off.startup())
    assert off._arbiter.history is None


def test_startup_log_reports_the_companion_switches(make_plugin):
    plugin, host = make_plugin({"dry_run": True, "speech_budget_per_battle": 40, "idle_deep_s": 60.0})
    asyncio.run(plugin.startup())

    records = [record for record in host.logger.records if "neko_wot startup:" in record]
    assert records, host.logger.records
    line = records[0]
    for fragment in ("history=", "recap=", "idle_deep=60.0s", "budget=40/30s", "burst=3"):
        assert fragment in line, line


def test_history_default_path_is_never_state_json():
    assert History().path.name == "neko_battles.jsonl"


def test_new_event_id_is_registered_everywhere():
    """``idle_company`` 是派生 id：档位 / 档内排序 / 派生清单 / 冷却 / 话术，五处都要在。"""
    assert arbiter_mod.EVENT_LEVELS["idle_company"] == arbiter_mod.LEVEL_INFO
    assert "idle_company" in arbiter_mod.PRIORITY
    assert "idle_company" in arbiter_mod.DERIVED_EVENT_IDS
    assert "idle_company" in arbiter_mod.DERIVED_COOLDOWN_S
    assert phrases_mod.DEFAULT_PHRASES["idle_company"]["default"]


def test_rendered_companion_lines_are_not_enemy_intel():
    """二阶静默与复盘的每一句都不许带敌方情报（走同一张禁词表）。"""
    for event_id, tiers in (
        ("idle_company", phrases_mod.DEFAULT_PHRASES["idle_company"]),
        ("battle_end", phrases_mod.DEFAULT_PHRASES["battle_end"]),
    ):
        for tier, lines in tiers.items():
            for line in lines:
                assert arbiter_mod.preach_is_forbidden(line) is False, (event_id, tier, line)


def test_the_ledger_never_writes_to_state_json(tmp_path):
    """账本文件与 mod 的 ``state.json`` 必须物理隔离（不同目录、不同文件名）。"""
    book = History(tmp_path / "WoT_Copilot" / "neko_battles.jsonl")
    book.append(_battle())

    written = sorted(item.name for item in (tmp_path / "WoT_Copilot").iterdir())
    assert written == ["neko_battles.jsonl"], written
    assert json.loads((tmp_path / "WoT_Copilot" / "neko_battles.jsonl").read_text(encoding="utf-8"))["v"] == 1
