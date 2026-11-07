"""v2.20 / P3：断带双层播报 + 机制话术分档（零协议变更）。

只消费协议已有字段：``you_hit.crits_devices`` / ``crits_count`` / ``crits`` /
``blocked``、``me.mech``、``mode_switched.mech``。每条功能都成对验：

**该说话**（断带/断轮双层、9 类机制开场、5 类机制切换），
**不该说话**（``crits_devices=[]`` / ``crits_count>1`` / 非行走机构 / ``blocked`` /
``mech=unknown`` / 开关关掉 / 15 秒 2 句上限 / 机制 6 句上限 → 断言不出对应话术）。

| 反向对照 | 用例 |
|---|---|
| `crits_devices=[]` | ``test_track_empty_devices_never_says_track`` |
| `crits_count>1` | ``test_track_count_above_one_is_not_repeated`` |
| 非行走机构（gun/turretRotator） | ``test_non_walking_device_is_not_track`` |
| `blocked=true` | ``test_blocked_never_says_track`` |
| 断带开关关掉 | ``test_track_switch_off_falls_back_to_direction`` |
| 15 秒第 3 句 | ``test_track_caps_at_two_lines_in_fifteen_seconds`` |
| `me.mech=[]` / 认不出 | ``test_mech_opening_empty_or_unknown_is_generic`` |
| `mode_switched.mech=unknown` | ``test_mech_switch_unknown_falls_back_to_old_line`` |
| 机制开场/切换开关关掉 | ``test_mech_opening_switch_off_is_generic`` / ``test_mech_switch_switch_off_is_old_line`` |
| 机制单局 6 句上限 | ``test_mech_caps_at_six_lines_per_battle`` |
"""

from __future__ import annotations

import asyncio
import re

import pytest
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
def _frame(
    seq: int,
    *,
    events=(),
    me: dict | None = None,
    team: dict | None = None,
    arena: str = "arena-v220",
    mode: str = "battle",
):
    base_me = {"vehicle_name": "WZ-219", "health_ratio": 0.9, "alive": True, "reload_ratio": 0.5, "fires": 0}
    base_me.update(me or {})
    return state_reader.Frame(
        status=state_reader.STATUS_OK,
        schema=1,
        seq=seq,
        ts=1000.0,
        mode=mode,
        arena_id=arena,
        me=base_me,
        team=team
        if team is not None
        else {"enemy_alive": 15, "enemy_total": 15, "ally_alive": 15, "ally_total": 15, "enemy_total_known": True},
        arena={"period": 2, "time_left": 300, "map": "荒蛮之地"},
        mode_kind="standard",
        events=tuple(events),
    )


def _hit(
    seq: int,
    *,
    devices=(),
    crits: int | None = None,
    count: int | None = 1,
    blocked: bool = False,
    yaw: float = 90.0,
) -> dict:
    payload: dict = {"yaw_deg": yaw, "damage": 560}
    if devices is not None:
        payload["crits_devices"] = list(devices)
    if crits is not None:
        payload["crits"] = crits
    if count is not None:
        payload["crits_count"] = count
    if blocked:
        payload["blocked"] = True
    return {"seq": seq, "id": "you_hit", "ts": 1000.0, "payload": payload}


def _mode(seq: int, *, activated=True, mech: str | None = "siege") -> dict:
    payload: dict = {"state": 2 if activated else 0, "prev": 0 if activated else 2, "activated": activated}
    if mech is not None:
        payload["mech"] = mech
    return {"seq": seq, "id": "mode_switched", "ts": 1000.0, "payload": payload}


def _start(seq: int = 700) -> dict:
    return {"seq": seq, "id": "battle_start", "ts": 1000.0, "payload": {}}


def _arb(**kwargs):
    logger = kwargs.pop("logger", None) or sdk_stub.RecordingLogger("neko_wot.tests.companion220")
    return arbiter_mod.Arbiter(logger=logger, **kwargs), logger


def _battle(*, vehicle: str = "WZ-219", dmg: int = 1000, ts: float = 900.0) -> Battle:
    return Battle(
        ts=ts,
        map="荒蛮之地",
        vehicle=vehicle,
        mode_kind="standard",
        result="win",
        kills=1,
        dmg=dmg,
        deaths=0,
        seconds=300,
    )


# ============================================================ 断带 / 断轮双层播报
def test_track_critical_speaks_one_short_line():
    arb, _ = _arb(min_gap_s=0.0, track_break_enabled=True)

    said = arb.observe(_frame(1, events=[_hit(1, devices=("track",), crits=16)]), 0.0)

    assert said == "履带伤了。", said
    assert arb.last_cue.event_id == "you_hit"


def test_track_destroyed_speaks_a_stronger_line():
    arb, _ = _arb(min_gap_s=0.0, track_break_enabled=True)

    said = arb.observe(_frame(1, events=[_hit(1, devices=("track",), crits=65536)]), 0.0)

    assert said == "履带断了。", said


def test_wheel_critical_and_destroyed_use_the_wheel_words():
    critical, _ = _arb(min_gap_s=0.0, track_break_enabled=True)
    assert critical.observe(_frame(1, events=[_hit(1, devices=("wheel",), crits=512)]), 0.0) == "轮子伤了。"

    destroyed, _ = _arb(min_gap_s=0.0, track_break_enabled=True)
    assert destroyed.observe(_frame(1, events=[_hit(1, devices=("wheel",), crits=2097152)]), 0.0) == "轮子断了。"


def test_track_device_matching_is_case_and_space_tolerant():
    arb, _ = _arb(min_gap_s=0.0, track_break_enabled=True)

    said = arb.observe(_frame(1, events=[_hit(1, devices=(" Track ",), crits=16)]), 0.0)

    assert said == "履带伤了。", said


def test_track_empty_devices_never_says_track():
    """⛔ 反向对照：``crits_devices == []`` → 断带层闭嘴（绝不说“履带”）。"""
    arb, _ = _arb(min_gap_s=0.0, track_break_enabled=True)

    said = arb.observe(_frame(1, events=[_hit(1, devices=(), crits=16)]), 0.0)

    assert said is not None and "履带" not in said and "轮子" not in said, said


def test_track_count_above_one_is_not_repeated():
    """⛔ 反向对照：同一设备集合的连击（``crits_count>1``）不逐发喊断带。"""
    arb, _ = _arb(min_gap_s=0.0, track_break_enabled=True)

    for seq in (1, 2):
        said = arb.observe(_frame(seq, events=[_hit(seq, devices=("track",), crits=16, count=2)]), float(seq))
        assert said is None or "履带" not in said, said


def test_non_walking_device_is_not_track():
    """⛔ 反向对照：gun / turretRotator 这类非行走机构，一个断带字都不许有。"""
    for device, mask in (("gun", 32), ("turretRotator", 64)):
        arb, _ = _arb(min_gap_s=0.0, track_break_enabled=True)
        said = arb.observe(_frame(1, events=[_hit(1, devices=(device,), crits=mask)]), 0.0)
        assert said is None or "履带" not in said, (device, said)
        assert said is None or "轮子" not in said, (device, said)


def test_blocked_never_says_track():
    """⛔ 反向对照：跳弹不可能断带 —— 只说“没打穿”。"""
    arb, _ = _arb(min_gap_s=0.0, track_break_enabled=True, hit_reaction_enabled=True)

    said = arb.observe(_frame(1, events=[_hit(1, devices=("track",), crits=65536, blocked=True)]), 0.0)

    assert said is not None and "没打穿" in said, said
    assert "履带" not in said and "轮子" not in said, said
    assert re.search(r"\d", said) is None, said


def test_track_switch_off_falls_back_to_direction():
    """⛔ 反向对照：开关关掉，断带句整层消失（普通方向句照旧）。"""
    arb, _ = _arb(min_gap_s=0.0, track_break_enabled=False)

    said = arb.observe(_frame(1, events=[_hit(1, devices=("track",), crits=65536)]), 0.0)

    assert said == "右边挨了一下。", said


def test_track_caps_at_two_lines_in_fifteen_seconds():
    """⛔ 反向对照：15 秒内第 3 句断带不再说（方向句可以继续）。"""
    arb, _ = _arb(min_gap_s=0.0, track_break_enabled=True, type_cooldown_s={"you_hit": 0.0})

    first = arb.observe(_frame(1, events=[_hit(1, devices=("track",), crits=16)]), 0.0)
    second = arb.observe(_frame(2, events=[_hit(2, devices=("wheel",), crits=512)]), 5.0)
    third = arb.observe(_frame(3, events=[_hit(3, devices=("track",), crits=65536)]), 10.0)

    assert first == "履带伤了。"
    assert second == "轮子伤了。"
    assert third is not None and "履带" not in third and "轮子" not in third, third


# ============================================================ 机制开场
MECH_OPENING_EXPECTED = {
    "twin_gun": "双管车，记得能齐射。",
    "twin_gun_switch": "双管车，两种打法都能切。",
    "rocket": "火箭车，助推在。",
    "hydro": "液气悬挂，车身能压。",
    "susp_auto": "自动液压，车身自己压。",
    "wheeled": "轮式车，跑得快。",
    "drum": "弹鼓车，一梭子很猛。",
    "autoreload": "弹会自己回来。",
    "auto_cannon": "机关炮车，突突突。",
}


@pytest.mark.parametrize("mech", sorted(MECH_OPENING_EXPECTED))
def test_mech_opening_speaks_for_each_known_key(mech):
    arb, _ = _arb(min_gap_s=0.0, mech_opening_enabled=True)

    said = arb.observe(_frame(1, events=[_start()], me={"mech": [mech]}), 0.0)

    assert said == MECH_OPENING_EXPECTED[mech], said
    assert arb.last_cue.event_id == "battle_start"


def test_mech_opening_empty_or_unknown_is_generic():
    """⛔ 反向对照：``me.mech=[]`` / 认不出的 key → 通用开场句。"""
    for mech in ([], ["not_a_real_mech"], ["STUN_PLACEHOLDER"]):
        arb, _ = _arb(min_gap_s=0.0, mech_opening_enabled=True)
        said = arb.observe(_frame(1, events=[_start()], me={"mech": mech}), 0.0)
        assert said is not None and "开打了" in said, (mech, said)
        for fragment in ("双管车", "火箭车", "轮式车", "机关炮车"):
            assert fragment not in said, (mech, fragment, said)


def test_mech_opening_switch_off_is_generic():
    arb, _ = _arb(min_gap_s=0.0, mech_opening_enabled=False)

    said = arb.observe(_frame(1, events=[_start()], me={"mech": ["twin_gun"]}), 0.0)

    assert said is not None and "开打了" in said
    assert "双管车" not in said


def test_c7_persona_beats_mech_opening(tmp_path):
    """优先级硬判据：C7 人格化可用时，机制自述一个字都不出。"""
    path = tmp_path / "ledger" / "neko_battles.jsonl"
    history = History(path)
    for index in range(2):
        assert history.append(_battle(ts=800.0 + index)) is True
    arb, _ = _arb(
        min_gap_s=0.0,
        history=history,
        battle_start_persona_enabled=True,
        mech_opening_enabled=True,
    )

    said = arb.observe(_frame(1, events=[_start()], me={"mech": ["twin_gun"]}), 1000.0)

    assert said is not None and "今天第 3 局" in said, said
    assert "双管车" not in said


# ============================================================ 机制切换
def test_mech_switch_siege_on_and_off():
    on, _ = _arb(min_gap_s=0.0, mech_switch_enabled=True)
    assert on.observe(_frame(1, events=[_mode(1, activated=True, mech="siege")]), 0.0) == "攻城模式展开。"

    off, _ = _arb(min_gap_s=0.0, mech_switch_enabled=True)
    assert off.observe(_frame(1, events=[_mode(1, activated=False, mech="siege")]), 0.0) == "攻城模式收起。"


@pytest.mark.parametrize("mech", ("twinGun", "dualGun"))
def test_mech_switch_dual_gun_never_says_salvo(mech):
    """⛔ 双管切换只说“模式切过来了”，协议没有 canMakeDualShot，不许说齐射。"""
    arb, _ = _arb(min_gap_s=0.0, mech_switch_enabled=True)

    said = arb.observe(_frame(1, events=[_mode(1, activated=True, mech=mech)]), 0.0)

    assert said == "双管模式切过来了。", said
    assert "齐射" not in said


def test_mech_switch_autocannon_and_rocket():
    autocannon, _ = _arb(min_gap_s=0.0, mech_switch_enabled=True)
    assert autocannon.observe(_frame(1, events=[_mode(1, mech="autocannon")]), 0.0) == "机关炮模式开了。"

    rocket, _ = _arb(min_gap_s=0.0, mech_switch_enabled=True)
    assert rocket.observe(_frame(1, events=[_mode(1, mech="rocket")]), 0.0) == "火箭助推开了。"


def test_mech_switch_unknown_falls_back_to_old_line():
    """⛔ 反向对照：``mech=unknown`` / 缺键 → 回到现状那三句。"""
    unknown, _ = _arb(min_gap_s=0.0, mech_switch_enabled=True)
    assert unknown.observe(_frame(1, events=[_mode(1, mech="unknown")]), 0.0) == "战斗模式开了。"

    missing, _ = _arb(min_gap_s=0.0, mech_switch_enabled=True)
    assert missing.observe(_frame(1, events=[_mode(1, mech=None)]), 0.0) == "战斗模式开了。"


def test_mech_switch_switch_off_is_old_line():
    arb, _ = _arb(min_gap_s=0.0, mech_switch_enabled=False)

    said = arb.observe(_frame(1, events=[_mode(1, mech="siege")]), 0.0)

    assert said == "战斗模式开了。", said


def test_mech_switch_keeps_the_twenty_second_cooldown():
    """既有闸门不动：10 秒后的第二次切换照旧被 20 秒同类冷却吞掉。"""
    arb, _ = _arb(min_gap_s=0.0, mech_switch_enabled=True)

    assert arb.observe(_frame(1, events=[_mode(1, mech="siege")]), 0.0) is not None
    assert arb.observe(_frame(2, events=[_mode(2, mech="siege")]), 10.0) is None


def test_mech_caps_at_six_lines_per_battle():
    """⛔ 反向对照：机制话术（开场/切换共用）单局 6 句，第 7 句退回通用句。"""
    arb, _ = _arb(min_gap_s=0.0, mech_switch_enabled=True, type_cooldown_s={"mode_switched": 0.0})
    spoken = []
    for index in range(7):
        text = arb.observe(_frame(index + 1, events=[_mode(index + 1, mech="siege")]), float(index))
        if text:
            spoken.append(text)

    assert spoken[:6] == ["攻城模式展开。"] * 6, spoken
    assert spoken[6] == "战斗模式开了。", spoken
    assert arb._mech_lines_used == 6


def test_mech_opening_counts_against_the_same_six_line_cap():
    arb, _ = _arb(
        min_gap_s=0.0,
        mech_opening_enabled=True,
        mech_switch_enabled=True,
        type_cooldown_s={"mode_switched": 0.0},
    )
    assert arb.observe(_frame(1, events=[_start()], me={"mech": ["twin_gun"]}), 0.0) is not None

    spoken = []
    for index in range(6):
        event_seq = 800 + index
        text = arb.observe(
            _frame(2 + index, events=[_mode(event_seq, mech="siege")]),
            1.0 + index,
        )
        if text:
            spoken.append(text)

    assert spoken[:5] == ["攻城模式展开。"] * 5, spoken
    assert spoken[5] == "战斗模式开了。", spoken
    assert arb._mech_lines_used == 6


# ============================================================ 配置 / 接线 / 话术闸
def test_v220_settings_defaults_and_dirty_values():
    assert settings_mod.DEFAULTS["track_break_enabled"] is True
    assert settings_mod.DEFAULTS["mech_opening_enabled"] is True
    assert settings_mod.DEFAULTS["mech_switch_enabled"] is True

    parsed = settings_mod.parse_settings({})
    assert parsed.track_break_enabled is True
    assert parsed.mech_opening_enabled is True
    assert parsed.mech_switch_enabled is True

    assert settings_mod.parse_settings({"track_break_enabled": "no"}).track_break_enabled is False
    assert settings_mod.parse_settings({"mech_opening_enabled": 0}).mech_opening_enabled is False
    assert settings_mod.parse_settings({"mech_switch_enabled": None}).mech_switch_enabled is True


def test_config_example_and_readme_document_the_three_new_keys():
    plugin_dir = plugin_mod.PHRASE_BOOK.path.parents[1]
    example = (plugin_dir / "config.example.toml").read_text(encoding="utf-8")
    readme = (plugin_dir / "README.md").read_text(encoding="utf-8")
    for key in ("track_break_enabled", "mech_opening_enabled", "mech_switch_enabled"):
        assert key in example, key
        assert key in readme, key


def test_plugin_wires_the_v220_switches_into_the_arbiter(make_plugin):
    plugin, host = make_plugin(
        {
            "dry_run": True,
            "track_break_enabled": False,
            "mech_opening_enabled": False,
            "mech_switch_enabled": False,
        }
    )
    asyncio.run(plugin.startup())
    arbiter = plugin._arbiter

    assert arbiter.track_break_enabled is False
    assert arbiter.mech_opening_enabled is False
    assert arbiter.mech_switch_enabled is False
    records = [record for record in host.logger.records if "neko_wot startup:" in record]
    assert records, host.logger.records
    for fragment in ("track_break=False", "mech_open=False", "mech_switch=False"):
        assert fragment in records[0], records[0]


def test_new_v220_lines_pass_the_forbidden_gate_and_stay_short():
    tiers = {
        "you_hit": ("track_critical", "track_destroyed", "wheel_critical", "wheel_destroyed"),
        "battle_start": tuple(arbiter_mod.MECH_OPENING_TIERS.values()),
        "mode_switched": (
            "twin_gun_on",
            "twin_gun_off",
            "dual_gun_on",
            "dual_gun_off",
            "siege_on",
            "siege_off",
            "autocannon_on",
            "autocannon_off",
            "rocket_on",
            "rocket_off",
        ),
    }
    for event_id, names in tiers.items():
        for tier in names:
            lines = phrases_mod.DEFAULT_PHRASES[event_id][tier]
            assert lines, (event_id, tier)
            for line in lines:
                assert arbiter_mod.preach_is_forbidden(line) is False, (event_id, tier, line)
                assert len(line) <= 24, (event_id, tier, line)
                for word in ("建议", "应该", "下次你", "换线", "绕后", "换车", "装填"):
                    assert word not in line, (event_id, tier, word, line)
                if event_id == "you_hit":
                    assert re.search(r"\d", line) is None, (tier, line)
    for tier in ("twin_gun_on", "twin_gun_off", "dual_gun_on", "dual_gun_off"):
        for line in phrases_mod.DEFAULT_PHRASES["mode_switched"][tier]:
            assert "齐射" not in line, (tier, line)


def test_shipped_phrase_file_has_the_new_tiers():
    for event_id, tier in (
        ("you_hit", "track_destroyed"),
        ("battle_start", "mech_wheeled"),
        ("mode_switched", "siege_on"),
        ("mode_switched", "rocket_off"),
    ):
        assert tier in phrases_mod.PHRASE_BOOK.tiers(event_id), (event_id, tier)


def test_mech_payload_fields_are_whitelisted():
    """接线闸：``crits`` / ``crits_count`` / ``crits_devices`` / ``mech`` 必须放行。"""
    allowed = arbiter_mod._allowed_payload(
        {
            "crits": 65536,
            "crits_count": 1,
            "crits_devices": ["track", ""],
            "mech": "siege",
            "attacker_vehicle": "SENTINEL",
        }
    )
    assert allowed["crits"] == 65536
    assert allowed["crits_count"] == 1
    assert allowed["crits_devices"] == ["track"]
    assert allowed["mech"] == "siege"
    assert "attacker_vehicle" not in allowed
