"""v2.19 / P2：C5 受击反应分档 + C6 劣势/残局细分 + C7 开场人格化。

能力清单见 README「陪伴能力」一节。每条功能都成对验：

**该说话**（阈值命中、合并成一句、三档分得开、账本可用），
**不该说话**（缺 ``max_health`` / 超 15 秒冷却 / 超单局 8 句 / 同一档第二次 /
计数缺失 / 账本空或坏 / 开关关掉 → 断言一句都不说）。

| 反向对照 | 用例 |
|---|---|
| C5 缺 ``max_health`` / 为 0 | ``test_c5_missing_max_health_is_silence_not_zero_percent`` |
| C5 15 秒同类冷却 | ``test_c5_same_type_cooldown_blocks_for_fifteen_seconds`` |
| C5 单局 8 句上限 | ``test_c5_battle_cap_is_eight_lines`` |
| C5 合并后不逐发喊 | ``test_c5_three_hits_in_fifteen_seconds_merge_into_one_line`` |
| C6 同一档一局只说一次 | ``test_c6_each_tier_only_once_per_battle`` |
| C6 任一计数缺失 | ``test_c6_missing_count_is_silence`` |
| C7 账本缺失 / 损坏 / 关闭 | ``test_c7_broken_or_empty_ledger_falls_back_to_generic_start`` |
"""

from __future__ import annotations

import asyncio
import re

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
    team: dict | None = None,
    me: dict | None = None,
    result: dict | None = None,
    mode: str = "battle",
    mode_kind: str | None = "standard",
    arena: str = "arena-v219",
    vehicle: str = "WZ-219",
    map_name: str = "荒蛮之地",
):
    base_me = {"vehicle_name": vehicle, "health_ratio": 0.9, "alive": True, "reload_ratio": 0.5, "fires": 0}
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
        arena={"period": 2, "time_left": 300, "map": map_name},
        mode_kind=mode_kind,
        result=result,
        events=tuple(events),
    )


def _damaged(seq: int, *, delta: int, max_health: int = 1900, health: int = 1500) -> dict:
    return {
        "seq": seq,
        "id": "you_damaged",
        "ts": 1000.0,
        "payload": {"health": health, "max_health": max_health, "delta": delta},
    }


def _hit(seq: int, *, yaw: float = 90.0, blocked: bool = False, damage: int = 560) -> dict:
    payload: dict = {"yaw_deg": yaw, "damage": damage}
    if blocked:
        payload["blocked"] = True
    return {"seq": seq, "id": "you_hit", "ts": 1000.0, "payload": payload}


def _start(seq: int = 700) -> dict:
    return {"seq": seq, "id": "battle_start", "ts": 1000.0, "payload": {}}


def _arb(**kwargs):
    logger = kwargs.pop("logger", None) or sdk_stub.RecordingLogger("neko_wot.tests.companion219")
    return arbiter_mod.Arbiter(logger=logger, **kwargs), logger


def _lines(logger) -> list[str]:
    return [record.split(": ", 1)[1] if ": " in record else record for record in logger.records]


def _battle(*, vehicle: str = "WZ-219", dmg: int = 1000, ts: float = 900.0, result: str = "win") -> Battle:
    return Battle(
        ts=ts,
        map="荒蛮之地",
        vehicle=vehicle,
        mode_kind="standard",
        result=result,
        kills=1,
        dmg=dmg,
        deaths=0,
        seconds=300,
    )


def _seed(tmp_path, rows, name: str = "ledger") -> History:
    book = History(tmp_path / name / "neko_battles.jsonl")
    for row in rows:
        assert book.append(row) is True
    return book


# ============================================================ C5：受击反应分档
def test_c5_heavy_hit_gets_the_heavy_line_in_full_mode():
    arb, _ = _arb(min_gap_s=0.0, hit_reaction_enabled=True, hit_reaction_level="full")

    said = arb.observe(_frame(1, events=[_damaged(1, delta=570)]), 0.0)

    assert said == "这一下不轻。", said
    assert arb.last_cue.event_id == "you_damaged"


def test_c5_critical_hit_gets_the_critical_line_not_the_health_number():
    arb, _ = _arb(min_gap_s=0.0, hit_reaction_enabled=True, hit_reaction_level="full")

    said = arb.observe(_frame(1, events=[_damaged(1, delta=1000, health=841)]), 0.0)

    assert said == "这一下够呛。", said
    assert "八百四十" not in said


def test_c5_quiet_default_uses_the_roadmap_thresholds():
    """quiet（默认）：≥25% 重、≥50% critical；<25% 保持 v2.18 的血量句。"""
    arb, _ = _arb(min_gap_s=0.0, hit_reaction_enabled=True, hit_reaction_level="quiet")
    heavy = arb.observe(_frame(1, events=[_damaged(1, delta=600, health=841)]), 0.0)
    assert heavy == "这一下不轻。", heavy

    arb2, _ = _arb(min_gap_s=0.0, hit_reaction_enabled=True, hit_reaction_level="quiet")
    light = arb2.observe(_frame(1, events=[_damaged(1, delta=380, health=841)]), 0.0)
    assert light is not None and "八百四十" in light, light
    assert "这一下不轻" not in light

    arb3, _ = _arb(min_gap_s=0.0, hit_reaction_enabled=True, hit_reaction_level="quiet")
    critical = arb3.observe(_frame(1, events=[_damaged(1, delta=1000)]), 0.0)
    assert critical == "这一下够呛。", critical


def test_c5_full_is_more_sensitive_than_quiet():
    """full 只把阈值下移：同样是 20%，full 升级、quiet 保持血量句。"""
    payload = {"health": 841, "max_health": 1900, "delta": 380}
    event = {"seq": 1, "id": "you_damaged", "ts": 1000.0, "payload": payload}

    quiet, _ = _arb(min_gap_s=0.0, hit_reaction_enabled=True, hit_reaction_level="quiet")
    assert "这一下不轻" not in (quiet.observe(_frame(1, events=[event]), 0.0) or "")

    full, _ = _arb(min_gap_s=0.0, hit_reaction_enabled=True, hit_reaction_level="full")
    assert full.observe(_frame(1, events=[event]), 0.0) == "这一下不轻。"


def test_c5_light_hit_keeps_the_v218_wording():
    arb, _ = _arb(min_gap_s=0.0, hit_reaction_enabled=True, hit_reaction_level="full")

    said = arb.observe(_frame(1, events=[_damaged(1, delta=100, health=1834)]), 0.0)

    assert said is not None and "一千八百" in said, said
    for upgraded in ("这一下不轻", "这一下够呛", "连着挨了几下"):
        assert upgraded not in said


def test_c5_blocked_is_ricochet_without_any_digit():
    """⛔ 跳弹口径：只说没打穿，一个数字都不许出现（damage 是面值不是掉血）。"""
    arb, _ = _arb(min_gap_s=0.0, hit_reaction_enabled=True, hit_reaction_level="full")

    said = arb.observe(_frame(1, events=[_hit(1, blocked=True, damage=560)]), 0.0)

    assert said is not None and "没打穿" in said, said
    assert re.search(r"\d", said) is None, said
    assert "560" not in said


def test_c5_three_hits_in_fifteen_seconds_merge_into_one_line():
    """⛔ 反向对照：15 秒内 ≥3 下 → 合并成一句；合并后窗口内不再逐发喊。"""
    arb, log = _arb(min_gap_s=0.0, hit_reaction_enabled=True, hit_reaction_level="full")

    first = arb.observe(_frame(1, events=[_hit(1)]), 0.0)
    second = arb.observe(_frame(2, events=[_hit(2)]), 5.0)
    merged = arb.observe(_frame(3, events=[_hit(3)]), 10.0)
    after = arb.observe(_frame(4, events=[_hit(4)]), 12.0)

    assert first == "右边挨了一下。", first
    assert second is None, "15 秒冷却内的第二下不该逐发喊"
    assert merged == "连着挨了几下，稳住。", merged
    assert after is None, "合并句出口后，窗口内不许再逐发喊"
    assert any("hit_merged" in line for line in _lines(log)), _lines(log)


def test_c5_same_type_cooldown_blocks_for_fifteen_seconds():
    arb, _ = _arb(min_gap_s=0.0, hit_reaction_enabled=True, hit_reaction_level="full")

    assert arb.observe(_frame(1, events=[_damaged(1, delta=60, health=1800)]), 0.0) is not None
    assert arb.observe(_frame(2, events=[_damaged(2, delta=60, health=1700)]), 10.0) is None
    assert arb.observe(_frame(3, events=[_damaged(3, delta=60, health=1600)]), 16.0) is not None


def test_c5_battle_cap_is_eight_lines():
    """⛔ 反向对照：C5 自己再算一层单局上限 —— 第 9 句直接闭嘴。"""
    arb, log = _arb(min_gap_s=0.0, hit_reaction_enabled=True, hit_reaction_level="full")
    spoken = []
    for index in range(9):
        health = 1500 - index * 100
        text = arb.observe(_frame(index + 1, events=[_damaged(index + 1, delta=60, health=health)]), index * 16.0)
        if text:
            spoken.append(text)

    assert len(spoken) == 8, spoken
    assert arb._hit_lines_used == 8
    assert any("hit_reaction_cap" in line for line in _lines(log)), _lines(log)


def test_c5_missing_max_health_is_silence_not_zero_percent():
    """⛔ 反向对照：``max_health`` 缺失 / 为 0 → 分档算不出来 → 闭嘴（不当 0%）。"""
    for payload in (
        {"health": 1500, "delta": 400},
        {"health": 1500, "max_health": 0, "delta": 400},
        {"health": 1500, "max_health": None, "delta": 400},
    ):
        arb, _ = _arb(min_gap_s=0.0, hit_reaction_enabled=True, hit_reaction_level="full")
        event = {"seq": 1, "id": "you_damaged", "ts": 1000.0, "payload": payload}
        assert arb.observe(_frame(1, events=[event], me={}), 0.0) is None, payload


def test_c5_reads_max_health_from_me_when_payload_lacks_it():
    """规格口径是 ``me.max_health``：payload 没带时从本帧 ``me`` 兜底。"""
    arb, _ = _arb(min_gap_s=0.0, hit_reaction_enabled=True, hit_reaction_level="full")
    event = {"seq": 1, "id": "you_damaged", "ts": 1000.0, "payload": {"health": 841, "delta": 400}}

    said = arb.observe(_frame(1, events=[event], me={"max_health": 1900}), 0.0)

    assert said == "这一下不轻。", said


def test_c5_disabled_keeps_the_v218_damage_line():
    """开关关掉 → 一记 50%+ 的狠招也只按 v2.18 的血量句说，不升级。"""
    arb, _ = _arb(min_gap_s=0.0, hit_reaction_enabled=False)

    said = arb.observe(_frame(1, events=[_damaged(1, delta=1000, health=841)]), 0.0)

    assert said is not None and "八百四十" in said, said
    assert "这一下够呛" not in said


# ============================================================ C6：劣势 / 残局细分
def test_c6_mild_disadvantage_gets_the_light_line():
    arb, _ = _arb(min_gap_s=0.0, team_behind_tiers_enabled=True)

    assert arb.observe(_frame(1, team={"ally_alive": 15, "enemy_alive": 15}), 0.0) is None
    said = arb.observe(_frame(2, team={"ally_alive": 13, "enemy_alive": 15}), 1.0)

    assert said == "我们这边少一两个，还早。", said
    assert arb.last_cue.event_id == "team_behind"


def test_c6_big_and_endgame_tiers_are_distinct():
    big, _ = _arb(min_gap_s=0.0, team_behind_tiers_enabled=True)
    big.observe(_frame(1, team={"ally_alive": 15, "enemy_alive": 15}), 0.0)
    assert big.observe(_frame(2, team={"ally_alive": 11, "enemy_alive": 15}), 1.0) == "人差得有点多，慢慢打。"

    endgame, _ = _arb(min_gap_s=0.0, team_behind_tiers_enabled=True)
    endgame.observe(_frame(1, team={"ally_alive": 15, "enemy_alive": 15}), 0.0)
    assert endgame.observe(_frame(2, team={"ally_alive": 2, "enemy_alive": 8}), 1.0) == "就剩咱们几个了，我陪着。"


def test_c6_all_three_tiers_can_speak_once_as_it_worsens():
    """一路恶化：mild → big → endgame，各说一次（只在“继续变差”时开口）。"""
    arb, _ = _arb(min_gap_s=0.0, team_behind_tiers_enabled=True)
    assert arb.observe(_frame(1, team={"ally_alive": 15, "enemy_alive": 15}), 0.0) is None
    assert "少一两个" in (arb.observe(_frame(2, team={"ally_alive": 13, "enemy_alive": 15}), 1.0) or "")
    assert "人差得" in (arb.observe(_frame(3, team={"ally_alive": 11, "enemy_alive": 15}), 7.0) or "")
    assert "就剩咱们" in (arb.observe(_frame(4, team={"ally_alive": 2, "enemy_alive": 8}), 13.0) or "")


def test_c6_each_tier_only_once_per_battle():
    """⛔ 反向对照：同一档第二次出现 → 一句都不说（每档一局一次）。"""
    cases = (
        ({"ally_alive": 13, "enemy_alive": 15}, "我们这边少一两个"),
        ({"ally_alive": 11, "enemy_alive": 15}, "人差得有点多"),
        ({"ally_alive": 2, "enemy_alive": 8}, "就剩咱们几个"),
    )
    for team, phrase in cases:
        arb, _ = _arb(min_gap_s=0.0, team_behind_tiers_enabled=True)
        arb.observe(_frame(1, team={"ally_alive": 15, "enemy_alive": 15}), 0.0)
        assert phrase in (arb.observe(_frame(2, team=team), 1.0) or ""), (team, phrase)
        assert arb.observe(_frame(3, team=team), 2.0) is None, (team, "同一档说了第二次")


def test_c6_missing_count_is_silence():
    """⛔ 反向对照：任一计数缺失 → 闭嘴，绝不拿别的字段顶。"""
    arb, _ = _arb(min_gap_s=0.0, team_behind_tiers_enabled=True)
    arb.observe(_frame(1, team={"ally_alive": 15, "enemy_alive": 15}), 0.0)

    for team in ({"ally_alive": 3}, {"enemy_alive": 12}, {}):
        said = arb.observe(_frame(2, team=team), 1.0)
        assert said is None or "少一两个" not in said, (team, said)
        assert said is None or "人差得" not in said, (team, said)
        assert said is None or "就剩咱们" not in said, (team, said)


def test_c6_disabled_returns_to_the_old_single_line():
    arb, _ = _arb(min_gap_s=0.0, team_behind_tiers_enabled=False)
    arb.observe(_frame(1, team={"ally_alive": 15, "enemy_alive": 15}), 0.0)
    assert arb.observe(_frame(2, team={"ally_alive": 11, "enemy_alive": 15}), 1.0) == "我们这边落后了，稳住。"
    # 旧的 -2 不触发（本单没有放宽频率）
    arb2, _ = _arb(min_gap_s=0.0, team_behind_tiers_enabled=False)
    arb2.observe(_frame(1, team={"ally_alive": 15, "enemy_alive": 15}), 0.0)
    assert arb2.observe(_frame(2, team={"ally_alive": 13, "enemy_alive": 15}), 1.0) is None


# ============================================================ C7：开场人格化
def test_c7_battle_start_says_today_count(tmp_path):
    history = _seed(tmp_path, [_battle(ts=800.0), _battle(ts=900.0)], "today")
    arb, _ = _arb(min_gap_s=0.0, history=history, battle_start_persona_enabled=True)

    said = arb.observe(_frame(1, events=[_start()]), 1000.0)

    assert said is not None and "今天第 3 局" in said, said
    assert "WZ-219" in said
    assert "？" not in said and "吧" not in said


def test_c7_vehicle_average_uses_three_samples(tmp_path):
    history = _seed(tmp_path, [_battle(dmg=1000, ts=100.0 + index) for index in range(4)], "average")
    arb, _ = _arb(min_gap_s=0.0, history=history, battle_start_persona_enabled=True)

    said = arb.observe(_frame(1, events=[_start()]), 1000.0)

    assert said is not None and "均伤 1000" in said, said
    assert "今天第 5 局" in said


def test_c7_broken_or_empty_ledger_falls_back_to_generic_start(tmp_path):
    """⛔ 反向对照：账本缺失 / 全部坏行 / 关掉账本 → 退回通用开场句，绝不猜数字。"""
    broken = tmp_path / "broken" / "neko_battles.jsonl"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"\xff\x00 not a jsonl \n")

    for history in (None, History(broken), History(tmp_path / "missing" / "neko_battles.jsonl")):
        arb, _ = _arb(min_gap_s=0.0, history=history, battle_start_persona_enabled=True)
        said = arb.observe(_frame(1, events=[_start()]), 1000.0)
        assert said is not None and "开打了" in said, (history, said)
        for fragment in ("今天第", "均伤", "打过"):
            assert fragment not in said, (fragment, said)


def test_c7_disabled_returns_to_generic_start(tmp_path):
    history = _seed(tmp_path, [_battle(ts=900.0)], "off")
    arb, _ = _arb(min_gap_s=0.0, history=history, battle_start_persona_enabled=False)

    said = arb.observe(_frame(1, events=[_start()]), 1000.0)

    assert said is not None and "开打了" in said
    assert "今天第" not in said


def test_c7_new_lines_are_short_and_never_ask_a_question():
    for tier in ("day", "day_vehicle", "vehicle_average", "vehicle_battles"):
        for line in phrases_mod.DEFAULT_PHRASES["battle_start"][tier]:
            assert len(line) <= 30, (tier, line)
            assert "？" not in line and "?" not in line, (tier, line)


# ============================================================ 配置 / 接线 / 话术闸
def test_v219_settings_defaults_and_dirty_values():
    assert settings_mod.DEFAULTS["hit_reaction_enabled"] is True
    assert settings_mod.DEFAULTS["hit_reaction_level"] == "quiet"
    assert settings_mod.DEFAULTS["team_behind_tiers_enabled"] is True
    assert settings_mod.DEFAULTS["battle_start_persona_enabled"] is True

    parsed = settings_mod.parse_settings({})
    assert parsed.hit_reaction_enabled is True
    assert parsed.hit_reaction_level == "quiet"
    assert parsed.team_behind_tiers_enabled is True
    assert parsed.battle_start_persona_enabled is True

    assert settings_mod.parse_settings({"hit_reaction_enabled": "no"}).hit_reaction_enabled is False
    assert settings_mod.parse_settings({"hit_reaction_level": "FULL"}).hit_reaction_level == "full"
    for dirty in (None, "", "loud", 3, []):
        assert settings_mod.parse_settings({"hit_reaction_level": dirty}).hit_reaction_level == "quiet", dirty


def test_config_example_and_readme_document_the_four_new_keys():
    plugin_dir = plugin_mod.PHRASE_BOOK.path.parents[1]
    example = (plugin_dir / "config.example.toml").read_text(encoding="utf-8")
    readme = (plugin_dir / "README.md").read_text(encoding="utf-8")
    for key in (
        "hit_reaction_enabled",
        "hit_reaction_level",
        "team_behind_tiers_enabled",
        "battle_start_persona_enabled",
    ):
        assert key in example, key
        assert key in readme, key


def test_plugin_wires_the_v219_switches_into_the_arbiter(make_plugin):
    plugin, host = make_plugin(
        {
            "dry_run": True,
            "hit_reaction_enabled": False,
            "hit_reaction_level": "full",
            "team_behind_tiers_enabled": False,
            "battle_start_persona_enabled": False,
        }
    )
    asyncio.run(plugin.startup())
    arbiter = plugin._arbiter

    assert arbiter.hit_reaction_enabled is False
    assert arbiter.hit_reaction_level == "full"
    assert arbiter.team_behind_tiers_enabled is False
    assert arbiter.battle_start_persona_enabled is False
    records = [record for record in host.logger.records if "neko_wot startup:" in record]
    assert records, host.logger.records
    for fragment in ("hit_reaction=False/full", "behind_tiers=False", "start_persona=False"):
        assert fragment in records[0], records[0]


def test_new_v219_lines_pass_the_forbidden_gate_and_stay_short():
    tiers = {
        "battle_start": ("day", "day_vehicle", "vehicle_average", "vehicle_battles"),
        "you_damaged": ("heavy", "critical", "swarm"),
        "you_hit": ("blocked_plain", "swarm"),
        "team_behind": ("mild", "big", "endgame"),
    }
    for event_id, names in tiers.items():
        for tier in names:
            lines = phrases_mod.DEFAULT_PHRASES[event_id][tier]
            assert lines, (event_id, tier)
            for line in lines:
                assert arbiter_mod.preach_is_forbidden(line) is False, (event_id, tier, line)
                for word in ("建议", "应该", "下次你", "换线", "绕后", "换车"):
                    assert word not in line, (event_id, tier, word, line)
    # 跳弹与重/临界句**零数字**（面值不是掉血）。
    for event_id, tier in (("you_hit", "blocked_plain"), ("you_damaged", "critical")):
        for line in phrases_mod.DEFAULT_PHRASES[event_id][tier]:
            assert re.search(r"\d", line) is None, (event_id, tier, line)


def test_shipped_phrase_file_has_the_new_tiers():
    """外置表与内置表逐字一致由 test_phrases_table 锁；这里点名新档必须真的在。"""
    for event_id, tier in (
        ("battle_start", "vehicle_average"),
        ("you_damaged", "heavy"),
        ("you_damaged", "critical"),
        ("you_damaged", "swarm"),
        ("you_hit", "blocked_plain"),
        ("team_behind", "endgame"),
    ):
        assert tier in phrases_mod.PHRASE_BOOK.tiers(event_id), (event_id, tier)
