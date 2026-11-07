"""/ v2.12：三级紧急度表（``EVENT_LEVELS`` + ``LEVEL_PRIORITY``）。

这一批用例守的是"**档位只有一个真相**"，四件事：

1. **成员精确**：三档各自的 id 集合逐条比对—— 多一个少一个都红；
2. **数值固定**：``LEVEL_PRIORITY`` 就是 90 / 50 / 10，键必须是那三个档位常量
   （不许出现字面量 ``"critical"`` 之外的第四种写法）；
3. **覆盖完整**：``PRIORITY``（档内排序）与 ``DERIVED_EVENT_IDS``（派生层合成）里的
   每个 id 都必须在 ``EVENT_LEVELS`` 里登记过 —— 漏一个就是"这条事件没有档位"；
4. **默认档**：未登记的 id 取 ``warning``、不抛异常（新事件 id 不能把插件打哑）。

⚠️ 本文件**不测仲裁行为**（按档选谁说是 的事），只锁常量表本身。
"""

from __future__ import annotations

import sdk_stub

_pkg = sdk_stub.load_plugin_module()
arbiter_mod = _pkg.arbiter

# 三档成员，**在测试里写死一份**（不跟实现互抄）：实现改表，这里先红。
# v2.12 补登 4 个 id：fire_out → info；module_damaged / crew_dead /
# ammo_low → warning（它们本来就在 phrases.json 里，只是 建表时漏了档位）。
CRITICAL_IDS = frozenset({"you_died", "battle_end", "on_fire", "hp_critical"})
WARNING_IDS = frozenset(
    {
        "you_killed",
        "you_respawned",
        "you_damaged",
        "you_hit",
        "you_assisted",
        "enemy_killed",
        "ally_killed",
        "suppressed",
        "time_low",
        "team_behind",
        "enemy_low",
        "mode_switched",
        "module_damaged",
        "crew_dead",
        "ammo_low",
    }
)
INFO_IDS = frozenset(
    {
        "reload_done",
        "fire_cleared",
        "battle_start",
        "fire_out",
        "team_numbers",
        # v2.18 / C4：二阶静默的陪伴句 —— 不依赖任何战况，只能垫底，所以进 info。
        "idle_company",
    }
)


def _ids_in(level: str) -> frozenset[str]:
    """当前实现里归在这一档下的全部 id。"""
    return frozenset(event_id for event_id, value in arbiter_mod.EVENT_LEVELS.items() if value == level)


def test_critical_tier_members_are_exact():
    """critical 只有四个：阵亡 / 结算 / 着火 / 血线告急。多一个就是档位漂了。"""
    assert _ids_in(arbiter_mod.LEVEL_CRITICAL) == CRITICAL_IDS


def test_warning_tier_members_are_exact():
    """warning 是战况与战术提醒，共 15 个（v2.12 补登了三个战损/弹药提醒）。"""
    assert _ids_in(arbiter_mod.LEVEL_WARNING) == WARNING_IDS


def test_info_tier_members_are_exact():
    """info 只有机制回执 + 开场白 + 人数报点（v2.12 补登 fire_out，v1.30 加 team_numbers）。"""
    assert _ids_in(arbiter_mod.LEVEL_INFO) == INFO_IDS


def test_tiers_partition_the_table_without_leftovers():
    """三档合起来正好覆盖整张表：没有第四个档位，也没有落单的 id。"""
    assert CRITICAL_IDS | WARNING_IDS | INFO_IDS == set(arbiter_mod.EVENT_LEVELS)
    # v2.18 / C4：24 → 25（新增派生 id ``idle_company``，info 档）。
    assert len(arbiter_mod.EVENT_LEVELS) == 25
    assert CRITICAL_IDS.isdisjoint(WARNING_IDS)
    assert CRITICAL_IDS.isdisjoint(INFO_IDS)
    assert WARNING_IDS.isdisjoint(INFO_IDS)


def test_level_constants_and_levels_order():
    """档位字符串是小写字面量；``LEVELS`` 由急到缓排列（判高低档时按它比）。"""
    assert arbiter_mod.LEVEL_CRITICAL == "critical"
    assert arbiter_mod.LEVEL_WARNING == "warning"
    assert arbiter_mod.LEVEL_INFO == "info"
    assert tuple(arbiter_mod.LEVELS) == (
        arbiter_mod.LEVEL_CRITICAL,
        arbiter_mod.LEVEL_WARNING,
        arbiter_mod.LEVEL_INFO,
    )
    # 表里出现的值只能是这三个常量之一，不许有拼写变体（"Critical" / "warn"）。
    assert set(arbiter_mod.EVENT_LEVELS.values()) == set(arbiter_mod.LEVELS)


def test_level_priority_is_90_50_10():
    """通道层数值定死 90 / 50 / 10，键用三个档位常量，且严格递减。"""
    level_priority = arbiter_mod.LEVEL_PRIORITY

    assert level_priority == {
        arbiter_mod.LEVEL_CRITICAL: 90,
        arbiter_mod.LEVEL_WARNING: 50,
        arbiter_mod.LEVEL_INFO: 10,
    }
    assert set(level_priority) == set(arbiter_mod.LEVELS)
    assert (
        level_priority[arbiter_mod.LEVEL_CRITICAL]
        > level_priority[arbiter_mod.LEVEL_WARNING]
        > level_priority[arbiter_mod.LEVEL_INFO]
    )


def test_every_priority_id_has_a_level():
    """``PRIORITY`` 里的每个 id 都必须登记档位 —— 否则它没有"跨档"这一维。"""
    missing = sorted(set(arbiter_mod.PRIORITY) - set(arbiter_mod.EVENT_LEVELS))
    assert missing == []
    # 反过来不要求：battle_start 有档位但不参与档内排序（它是开场白特例）。
    assert "battle_start" in arbiter_mod.EVENT_LEVELS
    assert "battle_start" not in arbiter_mod.PRIORITY


def test_every_derived_event_id_has_a_level():
    """派生层合成的事件 id 也一个不少地登记了档位。"""
    missing = sorted(set(arbiter_mod.DERIVED_EVENT_IDS) - set(arbiter_mod.EVENT_LEVELS))
    assert missing == []


def test_unknown_event_id_falls_back_to_warning():
    """未登记的 id → ``warning``，**不抛异常**（老 mod 冒出新事件别把插件打哑）。"""
    assert arbiter_mod.DEFAULT_LEVEL == arbiter_mod.LEVEL_WARNING
    assert arbiter_mod.EVENT_LEVELS.get("no_such_event", arbiter_mod.DEFAULT_LEVEL) == arbiter_mod.LEVEL_WARNING
    assert arbiter_mod.LEVEL_PRIORITY[arbiter_mod.DEFAULT_LEVEL] == 50


def test_critical_tier_covers_the_gap_bypass_anchors():
    """critical 必须包含全部"无视间隔的锚点"，外加两条即时危险（着火 / 血线告急）。"""
    assert set(arbiter_mod.GAP_BYPASS) <= CRITICAL_IDS
    assert _ids_in(arbiter_mod.LEVEL_CRITICAL) == set(arbiter_mod.GAP_BYPASS) | {"on_fire", "hp_critical"}


def test_protected_arbiter_tables_are_still_intact():
    """只加表，不许动既有闸表：这些 id 少一个就是行为变更，必须挡下。"""
    assert {"battle_end", "you_died"} <= set(arbiter_mod.GAP_BYPASS)
    assert "hp_critical" in arbiter_mod.QUOTA_EVENT_IDS
    assert {"you_damaged", "you_assisted"} <= set(arbiter_mod.LATEST_WINS)
    assert {"you_killed", "hp_critical"} <= set(arbiter_mod.DEFERRABLE)
    assert {"battle_start", "you_died", "you_killed"} <= set(arbiter_mod.TYPE_COOLDOWN_S)
    assert {"reload_done", "hp_critical", "suppressed"} <= set(arbiter_mod.DERIVED_COOLDOWN_S)
