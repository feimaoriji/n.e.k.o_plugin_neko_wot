"""MVP-2 判定层（B1~B6 + C1~C4）：受击方向 / 战斗模式 / 收尾 / 时间 / 落后。

这一批和 v2.3 的六个派生 detector 分成两个文件，因为它们的**数据来源不一样**：

* ``you_hit`` / ``mode_switched`` 是 **mod 真发的事件**（协议 v1.17 / v1.18），
  插件此前一条都没消费 —— 用例要盯 payload 白名单（合规最敏感）；
* ``enemy_low`` / ``time_low`` / ``team_behind`` 是**派生**的整局层面提醒，
  用例要盯"每局上限 + 换局归零 + 取不到字段就不发"。

每个 detector 至少三条：正常触发 / 边界不触发 / 换局或重启不误报。
"""

from __future__ import annotations

import json
import re

import sdk_stub
from test_arbiter import FORBIDDEN

_pkg = sdk_stub.load_plugin_module()
arbiter_mod = _pkg.arbiter
state_reader = _pkg.state_reader

# 禁词欠账（v2.7 清零）：v2.3 的 ``reload_done`` 话术撞过禁词表上的"装填"，当时留了
# 一个 id 豁免。v2.7 把这句重写成"炮弹上膛了"，欠账清空 —— 现在**任何 id 都不许豁免**，
# 矩阵用例里也不再有 ``continue`` 分支，并且直接断言这张表是空集。
KNOWN_FORBIDDEN_WORD_DEBT: frozenset[str] = frozenset()


# --------------------------------------------------------------------- 造帧
def _me(**overrides) -> dict:
    me = {
        "vehicle_name": "WZ-219",
        "health_ratio": 0.9,
        "alive": True,
        "reload_ratio": 0.2,
        "fires": 0,
    }
    me.update(overrides)
    return me


def _team(enemy_alive: int = 15, ally_alive: int = 15, *, enemy_total: int | None = 15) -> dict:
    team = {
        "enemy_alive": enemy_alive,
        "ally_alive": ally_alive,
        "ally_total": 15,
    }
    # ``enemy_total=None`` = 老版本 mod 根本没这个键（收尾闸必须因此闭嘴）
    if enemy_total is not None:
        team["enemy_total"] = enemy_total
    return team


def _frame(events=(), *, me=None, team=None, arena=None, mode="battle", arena_id="arena-1", seq=1):
    return state_reader.Frame(
        status=state_reader.STATUS_OK,
        schema=1,
        seq=seq,
        ts=1000.0,
        mode=mode,
        arena_id=arena_id,
        me=_me() if me is None else me,
        team=_team() if team is None else team,
        arena=arena,
        result=None,
        events=tuple(events),
    )


def _event(seq: int, event_id: str, payload: dict | None = None) -> dict:
    return {"seq": seq, "id": event_id, "ts": 1000.0, "payload": payload or {}}


def _said(arbiter, frame, now):
    return arbiter.observe(frame, now)


# ===================================================================== B1
# mod 事件 ``you_hit``（协议 v1.17）：只说八格粗方位，跳弹单独一句。
def test_you_hit_eight_sectors():
    """0/45/…/315 八个角度各说一格；分格边界 ±22.5° 不跳格。"""
    cases = {
        0.0: "正前方",
        45.0: "右前方",
        90.0: "右边",
        135.0: "右后方",
        180.0: "正后方",
        225.0: "左后方",
        270.0: "左边",
        315.0: "左前方",
        # 绕圈边界：337.5° 以上和 22.5° 以下都归"正前方"，359°/1° 不许说成别的格
        359.0: "正前方",
        1.0: "正前方",
        22.5: "右前方",
        22.4: "正前方",
    }
    for yaw, sector in cases.items():
        arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
        said = _said(arbiter, _frame([_event(1, "you_hit", {"yaw_deg": yaw})]), 0.0)
        assert said == f"{sector}挨了一下。", (yaw, said)


def test_you_hit_without_yaw_says_only_that_it_happened():
    """写端拿不到方位角照样发事件（v1.17）：只说"挨了一下"，方向绝不猜。"""
    for payload in ({}, {"yaw_deg": None}):
        arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
        said = _said(arbiter, _frame([_event(1, "you_hit", payload)]), 0.0)
        assert said == "挨了一下。", payload


def test_you_hit_blocked_is_a_separate_ricochet_line():
    """跳弹（blocked=true）单独一句：有受击、没掉血，说方向没意义。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    said = _said(arbiter, _frame([_event(1, "you_hit", {"yaw_deg": 90.0, "blocked": True})]), 0.0)
    assert said == "跳弹了。"


def test_you_hit_cooldown_does_not_starve_the_damage_line():
    """受击方向 8 秒冷却里，同一炮的"还剩多少血"必须说得出来（v2.4 饿死坑）。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    first = _said(arbiter, _frame([_event(1, "you_hit", {"yaw_deg": 90.0})], seq=1), 0.0)
    assert first == "右边挨了一下。"

    # 5 秒后又是一炮：方向句还在冷却（8s），但血线句的冷却还没用过 → 轮到它说
    second_frame = _frame(
        [
            _event(2, "you_hit", {"yaw_deg": 90.0}),
            _event(3, "you_damaged", {"health": 1200, "max_health": 1900}),
        ],
        seq=2,
    )
    second = _said(arbiter, second_frame, 5.0)
    assert second is not None, "方向句冷却中把血线句一起饿死了"
    assert "血" in second

    # 冷却一过，同一个方向再来一发照常报
    third = _said(arbiter, _frame([_event(4, "you_hit", {"yaw_deg": 225.0})], seq=3), 20.0)
    assert third == "左后方挨了一下。"


def test_you_hit_payload_cannot_smuggle_enemy_fields():
    """payload 白名单不破：攻击者/弹种/伤害一个都不许进话术（C4）。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    payload = {
        "yaw_deg": 90.0,
        "attacker_id": 424242,
        "attacker": "SENTINEL_ATTACKER",
        "attacker_vehicle": "SENTINEL_TANK",
        "damage": 481,
        "crits": 3,
        "he": True,
        "attack_reason_id": 7,
    }

    said = _said(arbiter, _frame([_event(1, "you_hit", payload)]), 0.0)

    assert said == "右边挨了一下。"
    for leaked in ("SENTINEL_ATTACKER", "SENTINEL_TANK", "424242", "481", "7"):
        assert leaked not in said, leaked


# ===================================================================== B2
# mod 事件 ``mode_switched``（协议 v1.18 / v1.20）：液压/战斗模式开与关。
def test_mode_switched_says_on_and_off():
    on = _said(
        arbiter_mod.Arbiter(min_gap_s=0.0),
        _frame([_event(1, "mode_switched", {"state": 2, "prev": 0, "activated": True, "switch_s": 1.8})]),
        0.0,
    )
    off = _said(
        arbiter_mod.Arbiter(min_gap_s=0.0),
        _frame([_event(2, "mode_switched", {"state": 0, "prev": 2, "activated": False, "switch_s": 1.6})]),
        0.0,
    )

    assert on == "战斗模式开了。"
    assert off == "切回普通了。"


def test_mode_switched_without_activated_does_not_guess():
    """老版本 mod 没有 ``activated``：只说"切了一下"，不替它判断开关。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    said = _said(arbiter, _frame([_event(1, "mode_switched", {"state": 2, "prev": 0})]), 0.0)

    assert said == "模式切换了一下。"
    assert "开" not in said and "普通" not in said


def test_mode_switched_cooldown_swallows_the_second_flip():
    """切入/切回**共用**一个 20 秒冷却（v2.16：实测 5 秒太密）。

    on 和 off 是同一个 ``event_id`` → 本来就共用同一个类型冷却计时器；这里锁的是
    "来回切一顿，20 秒内只念第一句"，以及"过了冷却才允许再念"。
    """
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert _said(arbiter, _frame([_event(1, "mode_switched", {"activated": True})], seq=1), 0.0)
    assert _said(arbiter, _frame([_event(2, "mode_switched", {"activated": False})], seq=2), 1.0) is None
    assert _said(arbiter, _frame([_event(3, "mode_switched", {"activated": False})], seq=3), 6.0) is None
    assert _said(arbiter, _frame([_event(4, "mode_switched", {"activated": False})], seq=4), 19.0) is None
    assert (
        _said(arbiter, _frame([_event(5, "mode_switched", {"activated": False})], seq=5), 20.5)
        == "切回普通了。"
    )


# ===================================================================== B3
# 派生 ``enemy_low``：已知对面存活 ≤2，一局一次。
def test_enemy_low_fires_once_per_battle():
    """一局一次（B3）。

    v1.30 起「战况计数」默认静音（击杀播报只聚焦本人战果），所以这一帧
    说话的就是收尾句自己 —— 不再需要"让位给'一口气干掉 N 个'"那套叙事。
    """
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert _said(arbiter, _frame(team=_team(enemy_alive=15), seq=1), 0.0) is None  # 建基线
    assert _said(arbiter, _frame(team=_team(enemy_alive=5), seq=2), 1.0) is None  # 5 台：还不够
    said = _said(arbiter, _frame(team=_team(enemy_alive=2), seq=3), 6.0)
    assert said == "我们这边快收尾了，别放跑，{MASTER_NAME}。"
    # 额度用完：后面哪怕一直 ≤2 也不许再念
    for seq, now in ((4, 7.0), (5, 60.0), (6, 120.0)):
        assert _said(arbiter, _frame(team=_team(enemy_alive=2), seq=seq), now) is None


def test_enemy_low_line_says_nothing_about_the_enemy():
    """v2.7 B3 话术重写：只说**我方处境**，敌方数字/敌方状态一个都不带。

    旧句"对面没几台了"把敌方剩余说了出去（哪怕口径只是"已知"）。新句
    "我们这边快撑不住了"只描述自己这侧，判定条件（battle + enemy_total > 0 的闸）
    一行都没动 —— 本用例只锁话术，不锁闸。
    """
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert _said(arbiter, _frame(team=_team(enemy_alive=15), seq=1), 0.0) is None  # 建基线
    said = _said(arbiter, _frame(team=_team(enemy_alive=2), seq=2), 1.0)

    # ⚠️ 属性闸**先于**黄金文本断言：反过来说，一旦有人改话术，黄金文本会先红，那三条
    # 属性闸就永远跑不到 —— 反向对照会把"闸没接上"误判成"闸接上了"。所以先撞内容规则。
    assert re.search(r"\d", said) is None, f"不许出现数字（=敌方数量）：{said}"
    assert arbiter_mod.preach_word_hits(said) == frozenset({"SELF"}), said
    assert not arbiter_mod.preach_is_forbidden(said), said
    assert said == "我们这边快收尾了，别放跑，{MASTER_NAME}。"


def test_enemy_low_ignores_three_left():
    """边界：≤2 才算"快清完"，还剩 3 台一律不说（阈值不许放宽）。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert _said(arbiter, _frame(team=_team(enemy_alive=15), seq=1), 0.0) is None
    assert _said(arbiter, _frame(team=_team(enemy_alive=3), seq=2), 1.0) is None
    assert _said(arbiter, _frame(team=_team(enemy_alive=3), seq=3), 10.0) is None
    # 掉到 2 才说（而且一局一次）
    assert _said(arbiter, _frame(team=_team(enemy_alive=2), seq=4), 11.0) == "我们这边快收尾了，别放跑，{MASTER_NAME}。"
    assert _said(arbiter, _frame(team=_team(enemy_alive=2), seq=5), 12.0) is None


def test_enemy_low_needs_a_baseline_frame():
    """第一帧只建基线：接手一个"对面就剩两台"的局面不等于"刚发生"。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert _said(arbiter, _frame(team=_team(enemy_alive=2), seq=1), 0.0) is None
    assert _said(arbiter, _frame(team=_team(enemy_alive=2), seq=2), 1.0) == "我们这边快收尾了，别放跑，{MASTER_NAME}。"


def test_enemy_low_is_silent_when_i_am_dead():
    """自己已经阵亡：收尾提醒没有行动意义（重生模式除外，那时 alive=True）。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert _said(arbiter, _frame(me=_me(alive=True), team=_team(enemy_alive=15), seq=1), 0.0) is None
    # 阵亡那一帧：收尾句不许顶上来
    assert _said(arbiter, _frame(me=_me(alive=False), team=_team(enemy_alive=2), seq=2), 1.0) is None
    # 之后一直 ≤2，但自己已经阵亡 → 永远不出
    assert _said(arbiter, _frame(me=_me(alive=False), team=_team(enemy_alive=2), seq=3), 30.0) is None


def test_enemy_low_quota_resets_on_new_battle():
    """换局归零（C2）：上一局用掉的额度不能把新一局带哑。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert _said(arbiter, _frame(team=_team(enemy_alive=15), seq=1), 0.0) is None
    assert _said(arbiter, _frame(team=_team(enemy_alive=2), seq=2), 1.0) == "我们这边快收尾了，别放跑，{MASTER_NAME}。"
    assert _said(arbiter, _frame(team=_team(enemy_alive=2), seq=3), 2.0) is None

    # 新一局：第一帧建基线（不许误报），第二帧才准说
    assert _said(arbiter, _frame(team=_team(enemy_alive=2), arena_id="arena-2", seq=4), 100.0) is None
    assert _said(arbiter, _frame(team=_team(enemy_alive=2), arena_id="arena-2", seq=5), 101.0) == (
        "我们这边快收尾了，别放跑，{MASTER_NAME}。"
    )


def test_enemy_low_is_silent_while_the_lineup_is_unknown():
    """回归锁（开局空枪）：enemy_alive=0 且编制未知 → 一句收尾都不许出。

    mod v1.24 口径下的**真实开局**：敌车一台都没进 AoI，``enemy_alive`` 恒 0、
    ``enemy_total`` 也恒 0（"一台都没认识过"）。旧判据只有 ``enemy_alive <= 2``，
    0 ≤ 2 成立 → 每局开场第二帧就播"对面没几台了，收尾吧"。
    v2.6 加 ``enemy_total > 0`` 这道闸，连喂 5 帧必须一句都没有。
    """
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    for seq in range(1, 6):
        said = _said(arbiter, _frame(team=_team(enemy_alive=0, enemy_total=0), seq=seq), float(seq))
        assert said is None, (seq, said)


def test_enemy_low_still_fires_when_the_whole_team_is_wiped():
    """闸别修成哑巴：enemy_alive=0 但 enemy_total=15（对面全灭）→ 必须说。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert _said(arbiter, _frame(team=_team(enemy_alive=15), seq=1), 0.0) is None  # 建基线
    assert _said(arbiter, _frame(team=_team(enemy_alive=0), seq=2), 1.0) == "我们这边快收尾了，别放跑，{MASTER_NAME}。"
    assert _said(arbiter, _frame(team=_team(enemy_alive=0), seq=3), 2.0) is None


def test_enemy_low_is_silent_without_the_enemy_total_field():
    """老版本 mod 没有 ``enemy_total`` 这个键 → 编制未知，本帧不出声（不猜）。"""
    for enemy_total in (None, 0):
        arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
        team = _team(enemy_alive=2, enemy_total=enemy_total)
        assert _said(arbiter, _frame(team=team, seq=1), 0.0) is None
        assert _said(arbiter, _frame(team=team, seq=2), 1.0) is None, enemy_total


# ===================================================================== B4
# 派生 ``team_behind``：我方 - 敌方 ≤ -3 的边沿，一局最多两次。
def test_team_behind_is_an_edge_with_a_cap():
    """边沿 + 一局两次上限。

    v1.30 起「我们少了 N 个」默认静音，落后句不再需要"让位一帧"，
    边沿与额度语义一行没改。
    """
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert _said(arbiter, _frame(team=_team(ally_alive=15, enemy_alive=15), seq=1), 0.0) is None
    # -2：还不够
    assert _said(arbiter, _frame(team=_team(ally_alive=13, enemy_alive=15), seq=2), 1.0) is None
    # 掉到 -3：第一次说
    assert _said(arbiter, _frame(team=_team(ally_alive=12, enemy_alive=15), seq=3), 6.0) == "我们这边落后了，稳住。"
    # 一直落后不重复
    assert _said(arbiter, _frame(team=_team(ally_alive=11, enemy_alive=15), seq=4), 10.0) is None
    # 回落（-1）清掉边沿
    assert _said(arbiter, _frame(team=_team(ally_alive=14, enemy_alive=15), seq=5), 20.0) is None
    # 再成立 → 第二次
    assert _said(arbiter, _frame(team=_team(ally_alive=12, enemy_alive=15), seq=6), 21.0) == "我们这边落后了，稳住。"
    # 第三次（回落再成立）→ 额度用光
    assert _said(arbiter, _frame(team=_team(ally_alive=15, enemy_alive=15), seq=7), 40.0) is None
    assert _said(arbiter, _frame(team=_team(ally_alive=12, enemy_alive=15), seq=8), 41.0) is None
    assert _said(arbiter, _frame(team=_team(ally_alive=12, enemy_alive=15), seq=9), 50.0) is None


def test_team_behind_ignores_minus_two():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert _said(arbiter, _frame(team=_team(ally_alive=15, enemy_alive=15), seq=1), 0.0) is None
    # -2 只报战况计数，不许说"落后"
    assert "落后" not in (_said(arbiter, _frame(team=_team(ally_alive=13, enemy_alive=15), seq=2), 1.0) or "")
    assert _said(arbiter, _frame(team=_team(ally_alive=13, enemy_alive=15), seq=3), 20.0) is None


def test_team_behind_new_battle_baseline_is_silent():
    """换局第一帧就落后 → 只建基线（不许新一局第一句就是坏消息）。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert _said(arbiter, _frame(team=_team(ally_alive=15, enemy_alive=15), seq=1), 0.0) is None
    # 换局，且新局一开局就落后 -5
    assert _said(arbiter, _frame(team=_team(ally_alive=10, enemy_alive=15), arena_id="arena-2", seq=2), 100.0) is None
    # 还在落后，不是新边沿
    assert _said(arbiter, _frame(team=_team(ally_alive=10, enemy_alive=15), arena_id="arena-2", seq=3), 101.0) is None
    # 追平之后再掉下去才是新边沿
    assert _said(arbiter, _frame(team=_team(ally_alive=15, enemy_alive=15), arena_id="arena-2", seq=4), 110.0) is None
    said = _said(arbiter, _frame(team=_team(ally_alive=12, enemy_alive=15), arena_id="arena-2", seq=5), 111.0)
    assert said is not None
    assert "落后" in said
    # 边沿已消费：同一局面下一帧不再说
    assert _said(arbiter, _frame(team=_team(ally_alive=12, enemy_alive=15), arena_id="arena-2", seq=6), 112.0) is None


# ===================================================================== B5
# 派生 ``time_low``：period==2 + 剩余 ≤60 秒，一局一次；取不到就不发。
def test_time_low_fires_in_battle_phase():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert _said(arbiter, _frame(arena={"period": 2, "time_left": 300.0}, seq=1), 0.0) is None
    assert _said(arbiter, _frame(arena={"period": 2, "time_left": 45.0}, seq=2), 1.0) == "时间不多了。"
    # 一局一次
    assert _said(arbiter, _frame(arena={"period": 2, "time_left": 20.0}, seq=3), 30.0) is None


def test_time_low_ignores_non_battle_periods():
    """period 不是 2（等待/准备/结算）→ 不是"还在打但时间不多"，一句话都不说。"""
    for period in (0, 1, 3, None):
        arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
        arena = {"time_left": 10.0}
        if period is not None:
            arena["period"] = period
        assert _said(arbiter, _frame(arena=arena, seq=1), 0.0) is None, period


def test_time_low_is_silent_without_a_remaining_field():
    """协议 §2.2 的 arena 现在**没有**剩余时间字段：取不到就永远不发（不许猜）。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    # 真实的 arena 形状（实机 state.json 原文）
    real = {"map": "巴比伦异象", "bonus_type": 103, "vehicles": 72, "period": 3, "type_id": None}
    assert _said(arbiter, _frame(arena=dict(real, period=2), seq=1), 0.0) is None
    # arena 整个缺失/不是 dict 也不许崩
    assert _said(arbiter, _frame(arena=None, seq=2), 1.0) is None


def test_time_low_quota_resets_on_new_battle():
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert _said(arbiter, _frame(arena={"period": 2, "time_left": 50.0}, seq=1), 0.0) == "时间不多了。"
    assert _said(arbiter, _frame(arena={"period": 2, "time_left": 40.0}, seq=2), 30.0) is None
    # 换局：额度归零，新局同样条件照样能再说一次
    assert _said(arbiter, _frame(arena={"period": 2, "time_left": 50.0}, arena_id="arena-2", seq=3), 100.0) == (
        "时间不多了。"
    )


# ---- B5 收口（v2.10）：只认 ``arena.time_left``（mod v1.27 显式剩余秒）+ 加时闭嘴 ----
def test_time_low_fires_once_at_the_sixty_second_boundary():
    """① 边界 60 秒整要报（≤60），61 秒不报；说出口之后这一局不再有第二遍。

    顺带锁话术口径：这句只谈"时间"，不许带任何敌方信息（与 ``test_arbiter`` 的
    禁词矩阵同一套规则，这里不另开豁免）。
    """
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert _said(arbiter, _frame(arena={"period": 2, "time_left": 61}, seq=1), 0.0) is None
    said = _said(arbiter, _frame(arena={"period": 2, "time_left": 60}, seq=2), 1.0)
    assert said == "时间不多了。"
    assert not arbiter_mod.preach_is_forbidden(said), said
    assert re.search(r"\d", said) is None, f"时间告急不该报任何数字：{said}"

    for seq, now, left in ((3, 20.0, 59), (4, 40.0, 12), (5, 60.0, 0)):
        assert _said(arbiter, _frame(arena={"period": 2, "time_left": left}, seq=seq), now) is None, left


def test_time_low_is_silent_when_the_time_left_key_is_missing():
    """② 缺键 = 看不到计时（**不是**剩余 0、不是"时间到了"）：一帧都不许猜。

    真实会出现的两种缺键帧：① 写端读不到显式剩余秒字段；② 回绕脏数据（>3600）
    被写端整帧丢弃（那一帧没有 ``time_left`` 键）。探针在场也不许拿它做判定。
    """
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    for seq in range(1, 5):
        assert _said(arbiter, _frame(arena={"period": 2, "vehicles": 72}, seq=seq), float(seq)) is None

    probe = {
        "period": 2,
        "time_probe": {"raw": -3, "norm": 0, "over": True, "inc": 0, "src": "remaining"},
    }
    assert _said(arbiter, _frame(arena=probe, seq=9), 20.0) is None


def test_time_low_is_silent_in_overtime():
    """③ 加时（``overtime=true``）不喊时间告急：0 秒是加时态的正常值。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    for seq, now, left in ((1, 0.0, 0), (2, 1.0, 0), (3, 2.0, 3)):
        arena = {"period": 2, "time_left": left, "overtime": True}
        assert _said(arbiter, _frame(arena=arena, seq=seq), now) is None, left


def test_time_low_reads_only_the_time_left_key():
    """白名单只放 ``time_left``：别名键 / ``periodLength`` / ``phase`` 一个都不认。

    读端没有"反推"这条路 —— 拿整局时长、相位去算剩余秒要知道开局时刻，
    一局从加载就开表、中途还会跳相位，算出来必然是错的（红线，见 ``arbiter`` 常量区）。
    """
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    arena = {
        "period": 2,
        "timeLeft": 30.0,
        "remaining": 30.0,
        "remaining_s": 30.0,
        "seconds_left": 30.0,
        "periodLength": 900,
        "phase": 2,
    }

    for seq in range(1, 4):
        assert _said(arbiter, _frame(arena=dict(arena), seq=seq), float(seq) * 10.0) is None


def test_time_low_is_not_rearmed_when_the_clock_grows():
    """④ 剩余时间变长（换阶段/加时前那一跳）**不重置**额度：一局仍然只 1 次。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert _said(arbiter, _frame(arena={"period": 2, "time_left": 45}, seq=1), 0.0) == "时间不多了。"
    # 写端"变大照常采纳"：时间一下跳到 300 秒（换阶段/加时）
    assert _said(arbiter, _frame(arena={"period": 2, "time_left": 300}, seq=2), 10.0) is None
    # 再跌回 60 秒以内：已经说过了，不许重来
    for seq, now, left in ((3, 30.0, 55), (4, 60.0, 5)):
        assert _said(arbiter, _frame(arena={"period": 2, "time_left": left}, seq=seq), now) is None, left


def test_time_low_does_not_come_back_after_overtime():
    """⑤ 归零 → 加时 → 又跌到 60：不许重复（加时那一帧合上的锁转回正常态也不复活）。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert _said(arbiter, _frame(arena={"period": 2, "time_left": 60}, seq=1), 0.0) == "时间不多了。"
    assert _said(arbiter, _frame(arena={"period": 2, "time_left": 0, "overtime": True}, seq=2), 2.0) is None
    for seq, now, left in ((3, 30.0, 60), (4, 40.0, 30)):
        assert _said(arbiter, _frame(arena={"period": 2, "time_left": left}, seq=seq), now) is None, left


def test_time_low_overtime_latches_even_when_the_line_was_never_spoken():
    """⑤' 加载进一局就已经在加时（一句都没说过）→ 之后时间"回到" 60 秒也不许开口。

    这条盯的是"加时那一帧要合锁"这件事本身：只跳过当前帧、不置锁，后面就会补一句
    迟到的"时间不多了"。
    """
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert _said(arbiter, _frame(arena={"period": 2, "time_left": 0, "overtime": True}, seq=1), 0.0) is None
    assert _said(arbiter, _frame(arena={"period": 2, "time_left": 240}, seq=2), 10.0) is None
    assert _said(arbiter, _frame(arena={"period": 2, "time_left": 30}, seq=3), 200.0) is None


def test_time_low_lock_is_reset_only_by_a_new_battle():
    """⑥ 换局（``arena_id`` 变化）才解锁：新一局同条件可以再说一次。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    assert _said(arbiter, _frame(arena={"period": 2, "time_left": 0, "overtime": True}, seq=1), 0.0) is None
    assert _said(arbiter, _frame(arena={"period": 2, "time_left": 30}, seq=2), 10.0) is None
    # 新一局：锁清掉。剩余秒是即时事实（没有"建基线"这一步），第一帧就该说
    said = _said(arbiter, _frame(arena={"period": 2, "time_left": 30}, arena_id="arena-2", seq=3), 100.0)
    assert said == "时间不多了。"


# ============================================================ C1 / C3 / C4
def test_new_ids_are_wired_into_the_tables():
    """C1：新 id 全部进表，且都没压过 battle_end（否则结算那帧会被挤掉）。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    for event_id in ("you_hit", "mode_switched"):
        assert event_id in arbiter_mod.TYPE_COOLDOWN_S, event_id
        assert arbiter._cooldown[event_id] == arbiter_mod.TYPE_COOLDOWN_S[event_id], event_id

    for event_id in ("enemy_low", "time_low", "team_behind"):
        assert event_id in arbiter_mod.DERIVED_EVENT_IDS, event_id
        assert arbiter._cooldown[event_id] == arbiter_mod.DERIVED_COOLDOWN_S[event_id], event_id
        assert event_id not in arbiter_mod.TYPE_COOLDOWN_S, event_id

    for event_id in ("you_hit", "mode_switched", "enemy_low", "time_low", "team_behind"):
        assert event_id in arbiter_mod.PRIORITY, event_id
        assert arbiter_mod.PRIORITY[event_id] < arbiter_mod.PRIORITY["battle_end"], event_id


def test_every_priority_template_is_free_of_forbidden_words():
    """C3：**整张 PRIORITY 表**（含派生 id）逐条渲染过禁词表。

    ``test_arbiter`` 里那张只覆盖 ``TYPE_COOLDOWN_S``（mod 真事件）；这里把派生 id
    也拉进来 —— 新加的三句全是派生层，正是原来没被禁词闸扫到的那一块。
    v2.7（B1/B4）起：不再有 id 豁免，禁词只有实现里那一份 ``FORBIDDEN_WORD_GROUPS``。
    """
    assert KNOWN_FORBIDDEN_WORD_DEBT == frozenset(), "禁词欠账必须清零，任何 id 都不许豁免"

    rendered: dict[str, str] = {}
    # 战况计数（enemy_killed / ally_killed）要 payload 的 n/left 才渲染，
    # 人数差（team_numbers）要 tier/ally/enemy —— 这里用"均势"档，不含任何敌方词；
    # 其余 id 用同一份通用 payload 就够（所有键都在白名单内）。
    # v1.30：队伍事件默认静音，这条"每句都能渲染"的表闸必须显式打开开关。
    payload = {"ratio_after": 0.42, "total": 3, "vehicle": "WZ-219", "n": 2, "left": 3,
               "tier": "even", "ally": 8, "enemy": 8}
    for event_id in sorted(set(arbiter_mod.PRIORITY) | set(arbiter_mod.DERIVED_EVENT_IDS)):
        arbiter = arbiter_mod.Arbiter(min_gap_s=0.0, team_events_enabled=True)
        frame = _frame([_event(1, event_id, dict(payload))])
        text = arbiter.observe(frame, 0.0)
        assert text, f"{event_id} 渲染不出来"
        rendered[event_id] = text

    for event_id, text in rendered.items():
        lowered = text.lower()
        for word in FORBIDDEN:
            assert word.lower() not in lowered, f"{event_id}: {word!r} in {text!r}"
        assert not arbiter_mod.preach_is_forbidden(text), f"{event_id}: {text!r}"


def test_self_group_lines_are_allowed_and_actually_spoken():
    """v2.7 B1：SELF 组（我方说法）**允许**出现，拆表的意义就在这。

    旧版只有一张表，己方战损话术也被一起禁掉；现在必须能听见"我被击毁了"。
    """
    payload = {"ratio_after": 0.42, "total": 3, "vehicle": "WZ-219", "n": 2, "left": 3,
               "tier": "even", "ally": 8, "enemy": 8}
    rendered: dict[str, str] = {}
    for event_id in sorted(set(arbiter_mod.PRIORITY) | set(arbiter_mod.DERIVED_EVENT_IDS)):
        # v1.30：队伍事件默认静音 —— 本用例要逐句渲染，必须显式打开。
        arbiter = arbiter_mod.Arbiter(min_gap_s=0.0, team_events_enabled=True)
        text = arbiter.observe(_frame([_event(1, event_id, dict(payload))]), 0.0)
        assert text, f"{event_id} 渲染不出来"
        rendered[event_id] = text

    assert "我被击毁了" in rendered["you_died"]

    self_words = arbiter_mod.FORBIDDEN_WORD_GROUPS["SELF"]
    spoken = "\n".join(rendered.values())
    hits = [word for word in self_words if word in spoken]
    assert "我被击毁了" in hits, hits
    assert len(hits) >= 2, f"SELF 组出现的说法太少：{hits}"


def test_state_reader_reads_the_arena_block(tmp_path):
    """``frame.arena`` 是真读出来的（B5 的数据源），不是测试自己塞的。"""
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "seq": 7,
                "ts": 1000.0,
                "mode": "battle",
                "arena_id": "arena-1",
                "arena": {"period": 2, "time_left": 42.0, "vehicles": 72},
                "me": {"vehicle_name": "WZ-219"},
                "team": None,
                "result": None,
                "events": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    frame = state_reader.read_state_file(path, stale_seconds=5.0, now=1000.5)

    assert frame.status == state_reader.STATUS_OK, frame.detail
    assert frame.arena == {"period": 2, "time_left": 42.0, "vehicles": 72}
