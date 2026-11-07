"""mod v1.38 前线模式（``mode_kind == "frontline"``）降噪。

前线 = 30v30 + 每人可复活多次（预备队最多 2 台、每 5 分钟 +1），一局 20+ 分钟，
击杀/损失几乎连续发生。读端在这类局里：

* ``enemy_killed`` / ``ally_killed`` 同类冷却 4 秒 → ``FRONTLINE_TEAM_COOLDOWN_S``（25 秒）；
* ``team_behind``（"我们这边落后了"）**整局不发** —— 复活局里人数差会被预备队补平，
  不代表落后；
* 人数播报沿用现有闸门：``frontline`` 不在 ``_numbers_snapshot`` 的白名单里 →
  **一条都不说**（敌方数字是显式 null，连"我方还剩 N 个"也不改成新话术）。

标准局（``standard``）与老 mod（没有 ``mode_kind``）的行为一个字节都不许变 ——
冷却、落后提醒、人数闸三处都按原路径走。
"""

from __future__ import annotations

import sdk_stub

_pkg = sdk_stub.load_plugin_module()
arbiter_mod = _pkg.arbiter
state_reader = _pkg.state_reader


def _frame(
    seq: int,
    *,
    mode_kind: str | None = "frontline",
    enemy_alive: int | None = 30,
    ally_alive: int | None = 30,
    enemy_total: int | None = 30,
    known: bool = False,
    events: tuple = (),
) -> state_reader.Frame:
    return state_reader.Frame(
        status=state_reader.STATUS_OK,
        schema=1,
        seq=seq,
        ts=1000.0,
        mode="battle",
        arena_id="arena-frontline",
        mode_kind=mode_kind,
        me={"vehicle_name": "WZ-219", "health_ratio": 0.8, "alive": True},
        team={
            "ally_alive": ally_alive,
            "ally_total": 30,
            "enemy_alive": enemy_alive,
            "enemy_total": enemy_total,
            "enemy_total_known": known,
        },
        events=tuple(events),
    )


def _drive_team_losses(mode_kind: str | None, *, count: int = 20) -> list[str | None]:
    """交替掉一台敌方/我方（各 10 条），间隔 1 秒 —— 前线局里就是"几乎连续发生"。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0, team_events_enabled=True)
    enemy, ally = 30, 30
    said: list[str | None] = [
        arbiter.observe(_frame(1, mode_kind=mode_kind, enemy_alive=enemy, ally_alive=ally), 0.0)
    ]
    for index in range(count):
        if index % 2 == 0:
            enemy -= 1
        else:
            ally -= 1
        said.append(
            arbiter.observe(
                _frame(2 + index, mode_kind=mode_kind, enemy_alive=enemy, ally_alive=ally),
                1.0 + index,
            )
        )
    return said


# ---------------------------------------------------------------- 集中判定
def test_is_frontline_only_accepts_the_exact_protocol_value():
    """``mode_kind`` 是自由字符串：只认精确的 ``frontline``，别的值一律按现状处理。"""
    assert arbiter_mod._is_frontline("frontline") is True
    for value in ("standard", "wave", "unknown", "Frontline", " frontline", "", None, 1, ["frontline"]):
        assert arbiter_mod._is_frontline(value) is False, value


def test_frontline_uses_25s_while_everything_else_uses_the_table():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    assert arbiter_mod.FRONTLINE_TEAM_COOLDOWN_S == 25.0

    arbiter.observe(_frame(1, mode_kind="frontline"), 0.0)
    assert arbiter._effective_cooldown("enemy_killed") == 25.0
    assert arbiter._effective_cooldown("ally_killed") == 25.0
    # 非队伍事件不受影响（标准值照旧从表里取）。
    assert arbiter._effective_cooldown("you_killed") == arbiter_mod.TYPE_COOLDOWN_S["you_killed"]

    arbiter.observe(_frame(2, mode_kind="standard"), 100.0)
    assert arbiter._effective_cooldown("enemy_killed") == 4.0


# ------------------------------------------------ ① 队伍事件：25 秒 vs 4 秒
def test_frontline_team_losses_are_capped_by_the_25_second_cooldown():
    """20 条连续击杀/损失（1 秒一条）→ 最多两条（每种事件各一条）。"""
    said = _drive_team_losses("frontline")
    spoken = [text for text in said if text]
    assert len(spoken) <= 2, said
    assert len(spoken) >= 1, said


def test_standard_team_losses_keep_the_old_four_second_cadence():
    """标准局：同样 20 条 → 冷却仍是 4 秒（改动前就是这样，至少 5 条）。"""
    said = _drive_team_losses("standard")
    spoken = [text for text in said if text]
    assert len(spoken) >= 5, said
    assert any("干掉一个" in text for text in spoken), spoken
    assert any("我们这边少了一个" in text for text in spoken), spoken


# ------------------------------------------------------ ② team_behind 静音
def test_frontline_never_says_team_behind():
    """我方 30 fall 到 26（差 -4）：标准局会说落后，前线局一条都不说。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    assert arbiter.observe(_frame(1, mode_kind="frontline"), 0.0) is None
    said = [
        arbiter.observe(_frame(2 + index, mode_kind="frontline", ally_alive=26 - index), 1.0 + index)
        for index in range(5)
    ]
    assert said == [None] * 5, said


def test_frontline_drops_a_foreign_team_behind_event_too():
    """老 mod 若直接塞同 id 事件：出口闸（第二道）也要挡住。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    assert arbiter.observe(_frame(1, mode_kind="frontline"), 0.0) is None
    injected = {"seq": 2, "id": "team_behind", "ts": 1000.0, "payload": {}}
    assert arbiter.observe(_frame(2, mode_kind="frontline", events=(injected,)), 1.0) is None


def test_standard_still_says_team_behind():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    assert arbiter.observe(_frame(1, mode_kind="standard", known=True), 0.0) is None
    said = arbiter.observe(_frame(2, mode_kind="standard", ally_alive=26, known=True), 1.0)
    assert said == "我们这边落后了，稳住。"


# --------------------------------------- ③ 前线 + 敌方 null：数字播报静音
def test_frontline_with_null_enemy_counts_stays_completely_silent():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    team = {
        "ally_alive": 30,
        "ally_total": 30,
        "enemy_alive": None,
        "enemy_total": None,
        "enemy_total_known": False,
    }
    assert arbiter_mod._numbers_snapshot("frontline", team) is None

    assert arbiter.observe(
        _frame(1, mode_kind="frontline", enemy_alive=None, enemy_total=None), 0.0
    ) is None
    said: list[str | None] = []
    for index, ally in enumerate((29, 28, 27, 26)):
        said.append(
            arbiter.observe(
                _frame(2 + index, mode_kind="frontline", enemy_alive=None, enemy_total=None, ally_alive=ally),
                10.0 + index,
            )
        )
    assert said == [None] * 4, said

    events = arbiter._derive(
        _frame(99, mode_kind="frontline", enemy_alive=None, enemy_total=None, ally_alive=20), 100.0
    )
    assert "team_numbers" not in {event["id"] for event in events}, events


# ------------------------------------------------- ④ 未来的新 mode_kind
def test_unknown_future_mode_is_safe_and_falls_back_to_default_gates():
    """认不出来的模式：不崩、数字静音、队伍类冷却回落默认（4 秒），不按前线处理。"""
    said = _drive_team_losses("some_future_mode")
    spoken = [text for text in said if text]
    assert len(spoken) >= 5, said

    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    assert arbiter.observe(
        _frame(1, mode_kind="some_future_mode", enemy_alive=None, enemy_total=None), 0.0
    ) is None
    events = arbiter._derive(
        _frame(2, mode_kind="some_future_mode", enemy_alive=None, enemy_total=None, ally_alive=29), 1.0
    )
    assert "team_numbers" not in {event["id"] for event in events}, events
    assert arbiter._effective_cooldown("enemy_killed") == 4.0


# ----------------------------------------------- ⑤ 老 mod：没有 mode_kind
def test_missing_mode_kind_behaves_byte_for_byte_like_standard():
    """老 mod（v1.29 及以前）没有这个键：队伍冷却、落后、人数三处都走标准路径。"""
    old = _drive_team_losses(None)
    standard = _drive_team_losses("standard")
    assert old == standard

    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    assert arbiter.observe(_frame(1, mode_kind=None, known=True), 0.0) is None
    assert arbiter._effective_cooldown("enemy_killed") == 4.0
    said = arbiter.observe(_frame(2, mode_kind=None, ally_alive=26, known=True), 1.0)
    assert said == "我们这边落后了，稳住。"
