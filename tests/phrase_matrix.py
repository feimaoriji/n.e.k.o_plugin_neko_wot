"""话术矩阵：每个事件 × 代表性入参 → "这句话应该长什么样"。

为什么要单独一个模块（而不是塞进某个 test_*.py）：

* ``tests/test_phrases_table.py`` 拿它跟**改动前快照**
  （``tests/fixtures/phrases_render_v27.json``）逐字比对 —— 话术外置化（v2.8）
  最容易悄悄改字，这条闸就是防这个。快照是用**改动前的 arbiter.py** 跑这份矩阵
  生成的，所以"渲染结果 == 快照" = "一句话都没被改写"。
* ``tools/snapshot_phrases.py`` 用同一份矩阵重新生成快照 / 做 ``--check``。

矩阵本身是**纯数据 + 纯函数**：每格一个全新 ``Arbiter``、显式传 ``now``，
不依赖文件、时钟、随机数、测试顺序（``pytest-randomly`` 在跑）。
"""

from __future__ import annotations

from typing import Any

# 每格：``name`` 唯一（用作快照的键）、``event`` 是被喂进去的事件 id，
# 其余是可选覆盖：``payload`` / ``me`` / ``team`` / ``arena`` / ``result`` /
# ``kills``（预置"这一局我已经数到几个"）/ ``seq``（决定低血俏皮话轮换、去重身份）。
CASES: tuple[dict[str, Any], ...] = (
    # ------------------------------------------------------------ 开局/战果
    {
        "name": "battle_start|payload_vehicle",
        "event": "battle_start",
        "payload": {"vehicle": "china:Ch71_WZ_219"},
        "me": {},
    },
    {
        "name": "battle_start|me_vehicle_name",
        "event": "battle_start",
        "payload": {},
        "me": {"vehicle_name": "WZ219"},
    },
    {"name": "battle_start|no_vehicle", "event": "battle_start", "payload": {}, "me": {}},
    {"name": "you_killed|first", "event": "you_killed", "kills": 0},
    {"name": "you_killed|fifth", "event": "you_killed", "kills": 4},
    # v1.30：连杀与"特殊死因"两档（字段真给 true 才走特殊档）
    {"name": "you_killed|streak", "event": "you_killed", "kills": 2},
    {"name": "you_killed|ammo", "event": "you_killed", "kills": 1, "payload": {"was_ammo": True}},
    {"name": "you_killed|fire", "event": "you_killed", "kills": 1, "payload": {"was_fire": True}},
    {"name": "you_died|default", "event": "you_died"},
    {"name": "you_respawned|default", "event": "you_respawned"},
    {"name": "on_fire|default", "event": "on_fire"},
    {"name": "fire_out|default", "event": "fire_out"},
    # ------------------------------------------------------------ 掉血四档
    {
        "name": "you_damaged|absolute",
        "event": "you_damaged",
        "payload": {"health": 1834, "max_health": 1900, "delta": 66},
    },
    {
        "name": "you_damaged|absolute_no_big_hit",
        "event": "you_damaged",
        "payload": {"health": 841, "max_health": 1900, "delta": 400},
    },
    {
        "name": "you_damaged|big_hit",
        "event": "you_damaged",
        "payload": {"health": 841, "max_health": 1900, "delta": 520},
    },
    {
        "name": "you_damaged|hundred",
        "event": "you_damaged",
        "payload": {"health": 105, "max_health": 1900, "delta": 10},
    },
    {
        "name": "you_damaged|thousand",
        "event": "you_damaged",
        "payload": {"health": 1000, "max_health": 1900, "delta": 10},
    },
    {
        "name": "you_damaged|low_hp_1",
        "event": "you_damaged",
        "seq": 1,
        "payload": {"health": 99, "max_health": 1900, "delta": 20},
    },
    {
        "name": "you_damaged|low_hp_2",
        "event": "you_damaged",
        "seq": 2,
        "payload": {"health": 99, "max_health": 1900, "delta": 20},
    },
    {
        "name": "you_damaged|low_hp_3",
        "event": "you_damaged",
        "seq": 3,
        "payload": {"health": 99, "max_health": 1900, "delta": 20},
    },
    {
        "name": "you_damaged|low_hp_4",
        "event": "you_damaged",
        "seq": 4,
        "payload": {"health": 99, "max_health": 1900, "delta": 20},
    },
    {
        "name": "you_damaged|low_hp_5_wraps",
        "event": "you_damaged",
        "seq": 5,
        "payload": {"health": 99, "max_health": 1900, "delta": 20},
    },
    {"name": "you_damaged|percent_965", "event": "you_damaged", "payload": {"ratio_after": 0.965}},
    {"name": "you_damaged|percent_957", "event": "you_damaged", "payload": {"ratio_after": 0.957}},
    {"name": "you_damaged|no_number", "event": "you_damaged", "payload": {}},
    {
        "name": "you_damaged|terminal",
        "event": "you_damaged",
        "payload": {"health": 0, "max_health": 1900, "delta": 388},
    },
    # ------------------------------------------------------------ 协助/弹药
    {"name": "you_assisted|plain", "event": "you_assisted", "payload": {"total": 340, "delta": 60}},
    {"name": "you_assisted|big", "event": "you_assisted", "payload": {"total": 1240, "delta": 260}},
    {"name": "you_assisted|small", "event": "you_assisted", "payload": {"total": 50, "delta": 10}},
    {"name": "you_assisted|single_digit", "event": "you_assisted", "payload": {"total": 5, "delta": 1}},
    {"name": "you_assisted|no_total", "event": "you_assisted", "payload": {}},
    {"name": "you_assisted|negative", "event": "you_assisted", "payload": {"total": -5}},
    {"name": "ammo_low|with_total", "event": "ammo_low", "payload": {"total": 3}},
    {"name": "ammo_low|without_total", "event": "ammo_low", "payload": {}},
    {"name": "ammo_low|bool_total", "event": "ammo_low", "payload": {"total": True}},
    {"name": "module_damaged|default", "event": "module_damaged"},
    {"name": "crew_dead|default", "event": "crew_dead"},
    # ------------------------------------------------------------ 结算四档
    {
        "name": "battle_end|win_with_numbers",
        "event": "battle_end",
        "result": {"won": True, "kills": 3, "damage": 2870},
    },
    {
        "name": "battle_end|lose_with_numbers",
        "event": "battle_end",
        "result": {"won": False, "kills": 1, "damage": 300},
    },
    {"name": "battle_end|draw", "event": "battle_end", "result": {"draw": True}},
    {"name": "battle_end|zero_kills_from_result", "event": "battle_end", "result": {"won": True, "kills": 0}},
    {"name": "battle_end|counted_kills", "event": "battle_end", "result": {}, "kills": 2},
    {"name": "battle_end|null_result", "event": "battle_end", "result": None},
    # v1.30：payload 兜底 + 三档胜负 + 协助伤害
    {
        "name": "battle_end|unknown_with_payload_numbers",
        "event": "battle_end",
        "result": None,
        "payload": {"result": "unknown", "kills": 2, "dmg_dealt_total": 800},
    },
    {
        "name": "battle_end|payload_wins_when_state_is_silent",
        "event": "battle_end",
        "result": {},
        "payload": {"result": "lose", "kills": 1},
    },
    {
        "name": "battle_end|state_beats_contradicting_payload",
        "event": "battle_end",
        "result": {"won": True, "kills": 1, "damage": 300},
        "payload": {"result": "lose", "kills": 9, "dmg_dealt_total": 9999},
    },
    {
        "name": "battle_end|with_assist",
        "event": "battle_end",
        "result": {"won": True, "kills": 1, "damage": 300, "assist": 415},
    },
    # ------------------------------------------------------------ 受击/模式
    {"name": "you_hit|blocked", "event": "you_hit", "payload": {"blocked": True}},
    {"name": "you_hit|no_yaw", "event": "you_hit", "payload": {}},
    {"name": "you_hit|front", "event": "you_hit", "payload": {"yaw_deg": 0.0}},
    {"name": "you_hit|right", "event": "you_hit", "payload": {"yaw_deg": 90.0}},
    {"name": "you_hit|back_left", "event": "you_hit", "payload": {"yaw_deg": 225.0}},
    {"name": "you_hit|wrap_359", "event": "you_hit", "payload": {"yaw_deg": 359.0}},
    {"name": "you_hit|edge_225", "event": "you_hit", "payload": {"yaw_deg": 22.5}},
    {"name": "mode_switched|on", "event": "mode_switched", "payload": {"activated": True}},
    {"name": "mode_switched|off", "event": "mode_switched", "payload": {"activated": False}},
    {"name": "mode_switched|unknown", "event": "mode_switched", "payload": {}},
    # ------------------------------------------------------------ 派生层
    {"name": "reload_done|default", "event": "reload_done"},
    {"name": "hp_critical|default", "event": "hp_critical"},
    {"name": "fire_cleared|default", "event": "fire_cleared"},
    {"name": "suppressed|default", "event": "suppressed"},
    {"name": "enemy_killed|one", "event": "enemy_killed", "payload": {"n": 1, "left": 3}},
    {"name": "enemy_killed|three", "event": "enemy_killed", "payload": {"n": 3, "left": 1}},
    {"name": "enemy_killed|no_payload", "event": "enemy_killed", "payload": {}},
    {"name": "ally_killed|one", "event": "ally_killed", "payload": {"n": 1, "left": 3}},
    {"name": "ally_killed|two", "event": "ally_killed", "payload": {"n": 2, "left": 0}},
    {"name": "enemy_low|default", "event": "enemy_low", "payload": {"left": 2}},
    {"name": "time_low|default", "event": "time_low"},
    {"name": "team_behind|default", "event": "team_behind"},
    # ------------------------------------------------------------ 人数差 6 档（v1.30）
    # 6 档判定本身在 ``test_numbers_matrix_v130``；这里只留"每档长什么样"的逐字快照。
    {"name": "team_numbers|big_advantage", "event": "team_numbers",
     "payload": {"tier": "big_advantage", "ally": 13, "enemy": 3}},
    {"name": "team_numbers|cleared", "event": "team_numbers",
     "payload": {"tier": "big_advantage", "ally": 13, "enemy": 0}},
    {"name": "team_numbers|small_advantage", "event": "team_numbers",
     "payload": {"tier": "small_advantage", "ally": 13, "enemy": 11}},
    {"name": "team_numbers|even", "event": "team_numbers",
     "payload": {"tier": "even", "ally": 8, "enemy": 8}},
    {"name": "team_numbers|small_disadvantage", "event": "team_numbers",
     "payload": {"tier": "small_disadvantage", "ally": 6, "enemy": 8}},
    {"name": "team_numbers|big_disadvantage", "event": "team_numbers",
     "payload": {"tier": "big_disadvantage", "ally": 3, "enemy": 9}},
    {"name": "team_numbers|wave", "event": "team_numbers",
     "payload": {"tier": "wave", "ally": 5}},
    {"name": "team_numbers|unknown", "event": "team_numbers",
     "payload": {"tier": "unknown", "ally": 5}},
    {"name": "team_numbers|missing_numbers", "event": "team_numbers",
     "payload": {"tier": "even", "ally": 8}},
    {"name": "team_numbers|bogus_tier", "event": "team_numbers",
     "payload": {"tier": "sure_win", "ally": 8, "enemy": 8}},
    # ------------------------------------------------------------ 二阶静默（v2.18 / C4）
    {"name": "idle_company|default", "event": "idle_company"},
)


def render_matrix(arbiter_mod: Any, state_reader: Any) -> dict[str, str | None]:
    """跑完整份矩阵，返回 ``{case_name: 渲染出来的句子或 None}``。"""
    return {case["name"]: _render_one(arbiter_mod, state_reader, case) for case in CASES}


def _render_one(arbiter_mod: Any, state_reader: Any, case: dict[str, Any]) -> str | None:
    """单格：全新仲裁器 + 显式 ``now``，保证"同输入同输出"。"""
    # v1.30：队伍事件默认静音；矩阵要逐字留证它们的措辞，所以显式打开
    # （产品默认值由 ``test_team_events_v130`` 单独锁）。
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0, team_events_enabled=True)
    # 先用一个空帧把"局"定下来：``observe`` 第一帧会走 ``_reset``（arena 变化），
    # 把 ``_kills`` 清零 —— 预置的击杀数必须写在它之后，否则"第 N 个"永远是 1。
    arbiter.observe(
        state_reader.Frame(
            status=state_reader.STATUS_OK,
            schema=1,
            seq=0,
            ts=999.0,
            mode="battle",
            arena_id="arena-golden",
            me={},
            events=(),
        ),
        -1.0,
    )
    arbiter._kills = int(case.get("kills", 0))
    seq = int(case.get("seq", 1))
    event = {
        "seq": seq,
        "id": case["event"],
        "ts": 1000.0,
        "payload": dict(case.get("payload") or {}),
    }
    frame = state_reader.Frame(
        status=state_reader.STATUS_OK,
        schema=1,
        seq=seq,
        ts=1000.0,
        mode=case.get("mode", "battle"),
        arena_id="arena-golden",
        me=case.get("me", {"vehicle_name": "WZ-219", "health_ratio": 0.9}),
        team=case.get("team"),
        arena=case.get("arena"),
        result=case.get("result"),
        events=(event,),
    )
    return arbiter.observe(frame, 0.0)
