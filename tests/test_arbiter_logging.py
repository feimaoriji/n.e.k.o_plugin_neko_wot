"""P0：仲裁结果日志 —— 让"为什么没说 / 为什么说了"可以从日志回答。

今晚的两个实机缺陷都卡在同一件事上：**插件成功推送时完全不打日志**（只有 ``dry_run``
打 ``[dry-run] 本应说出``），出事只能靠耳朵。这一组用例把日志本身当成契约来钉：

1. **每个决策点都要有行**：本帧候选 / 每档选中谁 / 每个被丢的候选 + 真实闸门名 /
   真说出口 / 队列动作；
2. **原因必须是真闸门名**（不许"未知"）：``team_events_off`` / ``type_cooldown(剩3.2s)`` /
   ``gap(剩0.8s)`` / ``forbidden_text`` / ``no_text`` / ``quota_used`` / ``seen(seq=123)`` /
   ``level_lost_to_critical`` / ``queue_full`` / ``latest_wins`` / ``interrupt_off`` …
3. **安静帧一个字都不许打**（poll 每秒一帧；没有新候选、队列也没动 = 一行都没有），
   正常一局的量级是"几十到一两百行"，不是每秒几十行；
4. ``debug_log``（默认 **true**）关掉时一行都不打。

日志的"回答能力"由两条端到端用例守着：
``test_the_log_answers_why_team_kills_would_be_spoken``（grep ``team_events_off``）与
``test_the_log_answers_why_the_death_was_not_spoken``（死亡那一帧的 ``drop you_died``）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import sdk_stub

_pkg = sdk_stub.load_plugin_module()
arbiter_mod = _pkg.arbiter
plugin_mod = _pkg
settings_mod = _pkg.settings
state_reader = _pkg.state_reader

PLUGIN_DIR = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ 造帧/造器
def _team(enemy_alive: int = 15, ally_alive: int = 15) -> dict:
    return {
        "enemy_alive": enemy_alive,
        "enemy_total": 15,
        "ally_alive": ally_alive,
        "ally_total": 15,
        "enemy_total_known": True,
    }


def _frame(seq: int, *, events=(), health: float = 0.9, alive: bool = True, mode: str = "battle", arena="arena-log"):
    return state_reader.Frame(
        status=state_reader.STATUS_OK,
        schema=1,
        seq=seq,
        ts=1000.0,
        mode=mode,
        arena_id=arena,
        me={"vehicle_name": "WZ-219", "health_ratio": health, "alive": alive, "reload_ratio": 0.5, "fires": 0},
        team=_team(),
        arena={"period": 2, "time_left": 300},
        mode_kind="standard",
        events=tuple(events),
    )


def _event(event_id: str, payload: dict | None = None, seq: int | None = None) -> dict:
    return {"seq": seq if seq is not None else _next_seq(), "id": event_id, "ts": 1000.0, "payload": payload or {}}


_seen_counter = 0


def _next_seq() -> int:
    global _seen_counter
    _seen_counter += 1
    return _seen_counter


def _arb(**kwargs):
    """→ (仲裁器, 记录器)。记录器就是 SDK 桩里那个 ``RecordingLogger``。"""
    logger = kwargs.pop("logger", None) or sdk_stub.RecordingLogger("neko_wot.tests.arbiter_log")
    return arbiter_mod.Arbiter(logger=logger, **kwargs), logger


def _lines(logger) -> list[str]:
    """把 ``info: xxx`` 拆成 ``xxx``（只看内容，不看级别前缀）。"""
    return [record.split(": ", 1)[1] if ": " in record else record for record in logger.records]


# ============================================================ ① 候选 / 选中 / 说出
def test_candidate_and_pick_lines_describe_the_frame():
    arb, log = _arb(min_gap_s=0.0)
    frame = _frame(
        1,
        events=[
            _event("you_hit", {"yaw_deg": 30.0}),
            _event("you_damaged", {"health": 1800, "max_health": 2000}),
        ],
    )

    text = arb.observe(frame, 0.0)

    lines = _lines(log)
    assert any(
        line.startswith("arbiter: fresh=[you_hit,you_damaged]") and "derived=[" in line for line in lines
    ), lines
    assert any(
        line.startswith("arbiter: pick critical=") and "warning=you_hit" in line and "info=" in line for line in lines
    ), lines
    assert text is not None
    assert any(line.startswith('speak you_hit level=warning text="') for line in lines), lines


def test_speak_line_reports_the_level_and_the_verbatim_text():
    arb, log = _arb(min_gap_s=0.0)
    frame = _frame(1, events=[_event("you_died", {"deaths": 1})], health=0.0, alive=False, mode="dead")

    text = arb.observe(frame, 0.0)

    assert text is not None
    speak_lines = [line for line in _lines(log) if line.startswith("speak you_died ")]
    assert speak_lines, _lines(log)
    assert "level=critical" in speak_lines[0]
    assert f'text="{text}"' in speak_lines[0]


# ============================================== ② 每个被丢的候选 + 真实闸门名（逐条）
def test_reason_team_events_off():
    arb, log = _arb(min_gap_s=0.0)  # 默认 team_events_enabled=False

    assert arb.observe(_frame(1), 0.0) is None
    injected = _event("enemy_killed", {"n": 1, "left": 14})
    assert arb.observe(_frame(2, events=[injected]), 1.0) is None

    assert "drop enemy_killed reason=team_events_off" in _lines(log), _lines(log)


def test_reason_type_cooldown_shows_the_remaining_seconds():
    arb, log = _arb(min_gap_s=0.0)

    assert arb.observe(_frame(1, events=[_event("you_killed")]), 0.0) is not None
    assert arb.observe(_frame(2, events=[_event("you_killed")]), 3.2) is None  # 冷却 4.0 秒

    drops = [line for line in _lines(log) if line.startswith("drop you_killed reason=type_cooldown")]
    assert len(drops) == 1, _lines(log)
    assert "剩0.8s" in drops[0], drops[0]


def test_reason_gap_shows_remaining_and_marks_enqueued():
    arb, log = _arb(min_gap_s=2.0)

    assert arb.observe(_frame(1, events=[_event("you_killed")]), 0.0) is not None
    assert arb.observe(_frame(2, events=[_event("you_assisted", {"total": 400})]), 1.2) is None

    drops = [line for line in _lines(log) if line.startswith("drop you_assisted reason=gap(")]
    assert len(drops) == 1, _lines(log)
    assert "剩0.8s" in drops[0], drops[0]
    assert drops[0].endswith("-> enqueued"), drops[0]
    assert any(line.startswith("enqueue you_assisted ttl=") for line in _lines(log)), _lines(log)


def test_reason_gap_without_enqueue_for_a_non_deferrable_id():
    arb, log = _arb(min_gap_s=2.0)

    assert arb.observe(_frame(1, events=[_event("you_killed")]), 0.0) is not None
    assert arb.observe(_frame(2, events=[_event("module_damaged")]), 0.5) is None  # 不在 DEFERRABLE

    drops = [line for line in _lines(log) if line.startswith("drop module_damaged reason=gap(")]
    assert len(drops) == 1, _lines(log)
    assert "-> enqueued" not in drops[0], "没进队列就不许写 -> enqueued"


def test_reason_queue_not_empty():
    arb, log = _arb(min_gap_s=2.0)

    assert arb.observe(_frame(1, events=[_event("you_killed")]), 0.0) is not None
    assert arb.observe(_frame(2, events=[_event("you_assisted", {"total": 400})]), 0.2) is None  # 进队列
    # 间隔到了、队列里还有话：crew_dead 抢不到这一帧，这一帧补说的是队里那句旧的
    # （这正是"播报旧的"的现场，日志必须把 crew_dead 为什么没说写清楚）
    text = arb.observe(_frame(3, events=[_event("crew_dead")]), 2.5)

    assert text is not None and "协助伤害" in text, text
    drops = [line for line in _lines(log) if line.startswith("drop crew_dead reason=queue_not_empty(")]
    assert len(drops) == 1, _lines(log)
    assert "n=1" in drops[0], drops[0]


def test_reason_idle_fill_for_info_tier():
    arb, log = _arb(min_gap_s=0.0, idle_fill_s=10.0)

    assert arb.observe(_frame(1, events=[_event("you_killed")]), 0.0) is not None
    assert arb.observe(_frame(2, events=[_event("reload_done")]), 4.0) is None

    drops = [line for line in _lines(log) if line.startswith("drop reload_done reason=idle_fill(")]
    assert len(drops) == 1, _lines(log)
    assert "剩6.0s" in drops[0], drops[0]


def test_reason_forbidden_text(monkeypatch):
    arb, log = _arb(min_gap_s=0.0)
    monkeypatch.setattr(arb, "_render", lambda event, frame: "我方击毁一台，敌方在左边。")

    assert arb.observe(_frame(1, events=[_event("you_killed")]), 0.0) is None

    assert "drop you_killed reason=forbidden_text" in _lines(log), _lines(log)


def test_reason_no_text():
    arb, log = _arb(min_gap_s=0.0)
    # health=0 的掉血句按规矩不说（那句交给 you_died）→ 渲染为空
    assert arb.observe(_frame(1, events=[_event("you_damaged", {"health": 0, "max_health": 2000})]), 0.0) is None

    assert "drop you_damaged reason=no_text" in _lines(log), _lines(log)


def test_reason_repeated_text():
    arb, log = _arb(min_gap_s=0.0, type_cooldown_s={"you_damaged": 0.0})
    payload = {"health": 1834, "max_health": 1900, "delta": 66}

    assert arb.observe(_frame(1, events=[_event("you_damaged", payload)]), 0.0) is not None
    assert arb.observe(_frame(2, events=[_event("you_damaged", payload)]), 1.0) is None

    assert "drop you_damaged reason=repeated_text" in _lines(log), _lines(log)


def test_reason_death_already_spoken():
    arb, log = _arb(min_gap_s=0.0)

    assert arb.observe(_frame(1, events=[_event("you_died", {"deaths": 1})], health=0.0, alive=False), 0.0) is not None
    assert arb.observe(_frame(2, events=[_event("you_died", {"deaths": 1})], health=0.0, alive=False), 2.0) is None

    drops = [line for line in _lines(log) if line.startswith("drop you_died reason=")]
    assert any("deaths=1" in line for line in drops), drops


def test_reason_level_lost_to_critical():
    arb, log = _arb(min_gap_s=0.0)
    frame = _frame(
        1,
        events=[_event("battle_start"), _event("you_died", {"deaths": 1})],
        health=0.0,
        alive=False,
        mode="dead",
    )

    assert arb.observe(frame, 0.0) is not None

    drops = [line for line in _lines(log) if line.startswith("drop battle_start reason=")]
    assert len(drops) == 1, _lines(log)
    assert drops[0] == "drop battle_start reason=level_lost_to_critical", drops[0]


def test_reason_same_tier_lost_names_the_winner():
    arb, log = _arb(min_gap_s=0.0)
    frame = _frame(
        1,
        events=[
            _event("you_hit", {"yaw_deg": 30.0}),
            _event("you_damaged", {"health": 1800, "max_health": 2000}),
        ],
    )

    assert arb.observe(frame, 0.0) is not None

    drops = [line for line in _lines(log) if line.startswith("drop you_damaged reason=")]
    assert len(drops) == 1, _lines(log)
    assert drops[0] == "drop you_damaged reason=same_tier_lost_to_you_hit", drops[0]


def test_reason_seen_for_an_anchor_event():
    """窗口重放：``you_died`` 第二次出现在 events 里 → 必须留一行 ``seen(seq=...)``。"""
    arb, log = _arb(min_gap_s=0.0)
    died = _event("you_died", {"deaths": 1})
    frame1 = _frame(1, events=[died], health=0.0, alive=False)
    assert arb.observe(frame1, 0.0) is not None

    # 下一帧：写端滑动窗口原样带着同一条事件（真实行为：窗口 16 条，不随写盘清空）
    assert arb.observe(_frame(2, events=[died], health=0.0, alive=False), 1.0) is None

    seen = [line for line in _lines(log) if line.startswith("drop you_died reason=seen(")]
    assert len(seen) == 1, _lines(log)
    assert f"seq={died['seq']}" in seen[0], seen[0]


def test_reason_interrupt_off_marks_the_critical_as_enqueued():
    arb, log = _arb(min_gap_s=10.0, interrupt_enabled=False)

    assert arb.observe(_frame(1, events=[_event("you_killed")]), 0.0) is not None
    assert arb.observe(_frame(2, events=[_event("on_fire")]), 1.0) is None

    drops = [line for line in _lines(log) if line.startswith("drop on_fire reason=")]
    assert len(drops) == 1, _lines(log)
    assert "interrupt_off" in drops[0], drops[0]
    assert drops[0].endswith("-> enqueued"), drops[0]


def test_reason_quota_used_for_hp_critical():
    arb, log = _arb(min_gap_s=0.0)

    assert arb.observe(_frame(1, health=0.30), 0.0) is None       # 基线
    assert arb.observe(_frame(2, health=0.20), 1.0) is not None  # 第一次跌破 → 报（额度用掉）
    assert arb.observe(_frame(3, health=0.30), 2.0) is None      # 回到 0.30（< 0.35，不重新上膛）
    assert arb.observe(_frame(4, health=0.20), 3.0) is None      # 再次跌破 → 额度没了

    drops = [line for line in _lines(log) if line.startswith("drop hp_critical reason=")]
    assert len(drops) == 1, _lines(log)
    assert "quota_used" in drops[0], drops[0]


# ============================================================ ③ 队列动作
def test_queue_actions_are_logged():
    arb, log = _arb(min_gap_s=2.0, pending_max=1)

    assert arb.observe(_frame(1, events=[_event("you_killed")]), 0.0) is not None
    assert arb.observe(_frame(2, events=[_event("suppressed")]), 0.1) is None      # enqueue
    assert arb.observe(_frame(3, events=[_event("you_assisted", {"total": 400})]), 0.2) is None  # 挤掉上一条
    lines = _lines(log)

    assert any(line.startswith("enqueue suppressed ttl=") for line in lines), lines
    assert any(line.startswith("drop suppressed reason=queue_full") for line in lines), lines

    # 过期
    assert arb.observe(_frame(4), 30.0) is None
    assert any(line.startswith("expire you_assisted") for line in _lines(log)), _lines(log)


def test_queue_discard_and_clear_queue_are_logged():
    arb, log = _arb(min_gap_s=2.0)

    assert arb.observe(_frame(1, events=[_event("you_killed")]), 0.0) is not None
    assert arb.observe(_frame(2, events=[_event("you_damaged", {"health": 1500, "max_health": 2000})]), 0.5) is None
    # 同类只留最新
    assert arb.observe(_frame(3, events=[_event("you_damaged", {"health": 900, "max_health": 2000})]), 1.0) is None
    line_discard = [line for line in _lines(log) if line.startswith("discard you_damaged")]
    assert line_discard and "latest_wins" in line_discard[0], _lines(log)

    # 阵亡清队（gap_bypass）
    assert arb.observe(_frame(4, events=[_event("you_died", {"deaths": 1})], health=0.0, alive=False), 1.2) is not None
    cleared = [line for line in _lines(log) if line.startswith("clear_queue ")]
    assert cleared, _lines(log)
    assert "reason=gap_bypass" in cleared[-1], cleared


def test_queue_clear_on_arena_reset_is_logged_with_its_own_reason():
    arb, log = _arb(min_gap_s=2.0)

    assert arb.observe(_frame(1, arena="arena-1"), 0.0) is None
    assert arb.observe(_frame(2, events=[_event("you_killed")], arena="arena-1"), 0.5) is not None
    assert arb.observe(_frame(3, events=[_event("you_assisted", {"total": 400})], arena="arena-1"), 1.0) is None
    assert arb._queue, "前提：队列里有话"

    assert arb.observe(_frame(4, arena="arena-2"), 1.5) is None

    cleared = [line for line in _lines(log) if line.startswith("clear_queue ")]
    assert cleared and "reason=arena_reset" in cleared[0], _lines(log)


# ============================================================ ④ 安静帧 / 音量
def test_a_quiet_frame_writes_nothing():
    arb, log = _arb(min_gap_s=2.0)
    assert arb.observe(_frame(1), 0.0) is None
    baseline = len(log.records)

    for index in range(6):
        assert arb.observe(_frame(10 + index), 10.0 + index) is None

    assert log.records[baseline:] == [], f"安静帧打日志了：{log.records[baseline:]!r}"


def test_chatter_window_replays_do_not_spam():
    """写端窗口每 250ms 把同一批事件重新发一遍 —— 非锚点的 ``seen`` 不许逐条打。"""
    arb, log = _arb(min_gap_s=0.0)
    window = [
        _event("you_hit", {"yaw_deg": 30.0}),
        _event("you_damaged", {"health": 1800, "max_health": 2000}),
        _event("you_hit", {"yaw_deg": 90.0}),
    ]
    assert arb.observe(_frame(1, events=window), 0.0) is not None
    baseline = len(log.records)

    for index in range(20):  # 同一批事件被重放 20 次
        arb.observe(_frame(10 + index, events=window), 1.0 + index * 0.25)

    spam = [line for line in _lines(log)[baseline:] if "reason=seen" in line]
    assert spam == [], f"窗口重放进日志了（会淹掉真信号）：{spam[:3]!r}"


def test_a_simulated_battle_stays_within_the_line_budget():
    """一局的日志量级：几十到一两百行（不是每秒几十行）。"""
    arb, log = _arb(min_gap_s=2.0)
    now = 0.0
    arb.observe(_frame(1), now)

    # 90 个事件 ≈ 实机一局（35 掉血 + 52 受击 + 收尾），每 2 秒一个
    for index in range(90):
        now += 2.0
        if index % 3 == 0:
            events = [_event("you_hit", {"yaw_deg": float(index % 360)})]
        elif index % 3 == 1:
            events = [_event("you_damaged", {"health": max(50, 1800 - index * 15), "max_health": 2000})]
        else:
            events = [_event("module_damaged")]
        arb.observe(_frame(10 + index, events=events), now)

    total = len(log.records)
    assert 0 < total <= 500, f"一局 {total} 行，超出「几十到一两百」的量级"
    assert any("speak " in line for line in _lines(log))


# ==================================================== ⑤ debug_log 开关与接线
def test_debug_log_false_silences_every_line():
    arb, log = _arb(min_gap_s=0.0, debug_log=False)

    assert arb.observe(_frame(1, events=[_event("you_killed")]), 0.0) is not None
    assert arb.observe(_frame(2, events=[_event("you_killed")]), 3.2) is None
    assert arb.observe(_frame(3, events=[_event("you_died", {"deaths": 1})], health=0.0, alive=False), 4.0) is not None

    assert log.records == [], f"debug_log=False 还在打：{log.records!r}"


def test_settings_debug_log_default_is_true_and_dirty_values_are_tolerated():
    assert settings_mod.DEFAULTS["debug_log"] is True
    assert settings_mod.parse_settings({}).debug_log is True
    assert settings_mod.parse_settings({"debug_log": False}).debug_log is False
    assert settings_mod.parse_settings({"debug_log": "no"}).debug_log is False
    for dirty in (None, "", "maybe", [], {}):
        assert settings_mod.parse_settings({"debug_log": dirty}).debug_log is True, dirty


def test_config_example_toml_ships_the_debug_log_key():
    example = (PLUGIN_DIR / "config.example.toml").read_text(encoding="utf-8")
    assert "debug_log" in example, "config.example.toml 必须带上 debug_log（首次初始化就拷它）"
    assert "debug_log = true" in example


def test_plugin_wires_its_own_logger_and_the_switch_into_the_arbiter(make_plugin):
    plugin, host = make_plugin({"dry_run": True, "debug_log": False})
    asyncio.run(plugin.startup())
    assert plugin._arbiter.debug_log is False
    assert plugin._arbiter.logger is host.logger, "必须用插件自己的 logger（才落进 N.E.K.O_Plugin_neko_wot_*.log）"

    plugin, host = make_plugin({"dry_run": True})  # 不写 → 默认 true
    asyncio.run(plugin.startup())
    assert plugin._arbiter.debug_log is True


def test_plugin_log_records_the_decision_when_it_pushes(tmp_path, make_plugin):
    """端到端：真跑一帧，决策行必须出现在插件 logger 里（不是落在某个私有 logger 上）。"""
    import json
    import time

    path = tmp_path / "state.json"

    def _write(seq: int, events: list[dict] | None = None) -> None:
        path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "seq": seq,
                    "ts": time.time(),
                    "mode": "battle",
                    "arena_id": "arena-log",
                    "me": {"vehicle_name": "WZ-219", "health_ratio": 0.8, "alive": True},
                    "team": _team(),
                    "arena": {"period": 2, "time_left": 300},
                    "result": None,
                    "events": events or [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    _write(1)
    section = {
        "dry_run": True,
        "state_path": str(path),
        "poll_interval_s": 0.05,
        "stale_seconds": 5.0,
        "min_gap_s": 0.0,
    }
    plugin, host = make_plugin(section)
    plugin._settings = settings_mod.parse_settings(section)
    plugin._arbiter = plugin_mod.Arbiter(
        min_gap_s=0.0, logger=host.logger, debug_log=plugin._settings.debug_log
    )
    plugin._primed = False
    plugin._prev_seq = None
    plugin._poll_once(plugin._settings, plugin._arbiter, time.time())
    _write(2, [{"seq": 41, "id": "you_killed", "ts": time.time(), "payload": {}}])
    assert plugin._poll_once(plugin._settings, plugin._arbiter, time.time()) is not None

    records = [record for record in host.logger.records if record.startswith("info: ")]
    assert any("speak you_killed level=warning" in record for record in records), records
    assert any("arbiter: fresh=[you_killed]" in record for record in records), records


# ============================================ ⑥ 这份日志要能回答两个问题
def test_the_log_answers_why_team_kills_would_be_spoken():
    """问题 A：「为什么这 15 条 enemy_killed 说了」→ grep ``team_events_off`` 有没有出现。

    开着开关时同一条事件会说出来（``speak enemy_killed``）；关着时只有一行
    ``drop enemy_killed reason=team_events_off`` —— 一眼就能分辨"没生成 / 生成了没让说"。
    """
    killed = _event("enemy_killed", {"n": 1, "left": 14})

    off, off_log = _arb(min_gap_s=0.0)
    off.observe(_frame(1), 0.0)
    assert off.observe(_frame(2, events=[killed]), 1.0) is None
    assert "drop enemy_killed reason=team_events_off" in _lines(off_log)

    on, on_log = _arb(min_gap_s=0.0, team_events_enabled=True)
    on.observe(_frame(1), 0.0)
    assert on.observe(_frame(2, events=[_event("enemy_killed", {"n": 1, "left": 14})]), 1.0) is not None
    assert any(line.startswith("speak enemy_killed level=info") for line in _lines(on_log)), _lines(on_log)


def test_the_log_answers_why_the_death_was_not_spoken():
    """问题 B：「为什么死亡没说」→ 死亡那一帧必须有一行 ``drop you_died reason=...``。"""
    arb, log = _arb(min_gap_s=2.0)

    assert arb.observe(_frame(1, events=[_event("you_killed")]), 0.0) is not None
    assert arb.observe(_frame(2, events=[_event("you_assisted", {"total": 400})]), 0.5) is None
    assert arb.observe(_frame(3), 1.0) is None  # 让队列躺着
    # 用"数据端重启"这条真实存在但极难从外部看见的路径造一个"阵亡没说出来"
    # （seq 回退 → 静默 prime；这里直接断言那一帧留下的痕迹）
    lines_before = len(_lines(log))
    arb.observe(
        _frame(4, events=[_event("you_died", {"deaths": 1}, seq=1)], health=0.0, alive=False),
        1.5,
    )
    lines = _lines(log)[lines_before:]
    assert lines, "那一帧什么也没留下 —— 这就是今晚的观测盲区"
    assert any("silent_prime" in line or "drop you_died" in line or "speak you_died" in line for line in lines), lines
