"""/ v2.12：通道层三模式（verbatim / bubble / llm）+ metadata 通道。

守这几件事：

1. **verbatim（默认）= 出声**：``ai_behavior="respond"`` + ``visibility=[]``
   （respond 已经会让宿主把这句话说出来，再叠 ``["chat"]`` 就是同一句被听一遍、
   屏幕再显示一遍），metadata 里带审过的原句 ``plugin_recommended_reply`` +
   ``max_reply_chars`` + ``reply_contract``，把 LLM 的活退化成"复述"；
2. **bubble = 纯气泡**：``blind`` + ``visibility=["chat"]``，0 token、不发声；
3. **llm = v2.8 现状**：``respond`` + ``visibility=[]``，公共 metadata 照带，
   但**不带**复述契约（那个模式的语义就是自由发挥）；
4. **priority 只来自 LEVEL_PRIORITY**（critical 90 / warning 50 / info 10），
   ``interrupt_enabled=False`` 时 critical 降到 50 —— 这是打断开关的唯一实现；
5. **info_channel="bubble"** 把 info 档单独降成气泡，不看 ``output_mode``；
6. **脏值回落 verbatim**，并在 startup 打一条带原始值的 WARNING，不抛异常。
"""

from __future__ import annotations

import asyncio
import time

import pytest
import sdk_stub

plugin_mod = sdk_stub.load_plugin_module()
settings_mod = plugin_mod.settings

VERBATIM, BUBBLE, LLM = "verbatim", "bubble", "llm"

# 公共 metadata：三档模式全带（宿主靠它们做顶替 / 打断 / 排序）。
BASE_METADATA_KEYS = {
    "plugin",
    "event_id",
    "level",
    "priority",
    "coalesce_key",
    "replace_pending",
    "interrupt_pending",
}
# 复述契约块：verbatim / bubble 带，llm 故意不带。
CONTRACT_METADATA_KEYS = {
    "plugin_recommended_reply",
    "max_reply_chars",
    "reply_max_chars",
    "reply_contract",
    "battle_reply_contract",
    "live_reply_contract",
}


def _write_state(path, *, seq: int, events: list[dict] | None = None, mode: str = "battle") -> str:
    import json

    payload = {
        "schema": 1,
        "seq": seq,
        "ts": time.time(),
        "mode": mode,
        "arena_id": "arena-1",
        "me": {"vehicle_name": "WZ-219", "health_ratio": 0.8},
        "team": {"ally_alive": 9, "ally_total": 15, "enemy_alive": 12, "enemy_total": 15},
        "result": None,
        "events": events or [],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _kill_event(seq: int = 7) -> dict:
    return {"seq": seq, "id": "you_killed", "ts": time.time(), "payload": {}}


def _section(state_path: str, **overrides) -> dict:
    section = {
        "dry_run": True,
        "state_path": state_path,
        "poll_interval_s": 0.05,
        "stale_seconds": 5.0,
        "min_gap_s": 0.0,
    }
    section.update(overrides)
    return section


def _speak_line(make_plugin, line: str = "测试台词", *, level=None, event_id=None, **overrides) -> dict:
    """不跑状态文件，直接调一次 ``_speak``，返回那次 push 的全部 kwargs。"""
    section = _section("unused.json", dry_run=False, **overrides)
    plugin, host = make_plugin(section)
    plugin._settings = settings_mod.parse_settings(section)
    plugin._speak(plugin._settings, line, level=level, event_id=event_id)
    assert len(host.pushed) == 1, f"应该恰好推一次，实际推了 {len(host.pushed)} 次"
    return host.pushed[0]


def _speak_once(tmp_path, make_plugin, plugin_module, **overrides):
    """真跑一帧：首帧对齐基线 → 写一条击杀事件 → 再跑一帧，返回 (host, 说出的文本)。

    返回的文本就是 ``_poll_once`` 的返回值，也就是 arbiter 给我的那一句 —— 与推出去的
    ``parts[0]["text"]`` 逐字比对，才能证明"没被改过"。
    """
    path = tmp_path / "state.json"
    state = _write_state(path, seq=1)
    section = _section(state, dry_run=False, **overrides)
    plugin, host = make_plugin(section)

    plugin._settings = settings_mod.parse_settings(section)
    plugin._arbiter = plugin_module.Arbiter(min_gap_s=0.0)
    plugin._primed = False
    plugin._prev_seq = None

    plugin._poll_once(plugin._settings, plugin._arbiter, time.time())  # 首帧只对齐基线
    _write_state(path, seq=2, events=[_kill_event()])
    text = plugin._poll_once(plugin._settings, plugin._arbiter, time.time())
    return host, text


# ------------------------------------------------------------------ ① 默认值
def test_default_output_mode_is_verbatim():
    assert settings_mod.DEFAULT_OUTPUT_MODE == VERBATIM
    assert settings_mod.DEFAULTS["output_mode"] == VERBATIM
    # 空配置 / 整个 section 不存在 → 都是 verbatim
    assert settings_mod.parse_settings(None).output_mode == VERBATIM
    assert settings_mod.parse_settings({}).output_mode == VERBATIM
    assert settings_mod.load_settings({}).output_mode == VERBATIM


def test_default_mode_reaches_the_plugin_and_is_logged(tmp_path, make_plugin):
    """接线闸：默认值真的进到实例上，启动日志能看出来现在是什么模式。"""
    plugin, host = make_plugin({"dry_run": True})
    asyncio.run(plugin.startup())

    assert plugin._settings.output_mode == VERBATIM
    assert any("output_mode=verbatim" in record for record in host.logger.records)


# -------------------------------------------------- ② verbatim：出声、逐字、不改写
def test_verbatim_pushes_the_arbiter_text_character_for_character(tmp_path, make_plugin, plugin_module):
    """verbatim 既要**出声**（respond），又要把原句一个字符都不差地推上去。"""
    host, text = _speak_once(tmp_path, make_plugin, plugin_module)  # 不给 output_mode → 走默认 verbatim

    assert text, "这一帧本来就有话要说，返回空说明用例本身没造出事件"
    assert len(host.pushed) == 1
    pushed = host.pushed[0]
    assert pushed["parts"] == [{"type": "text", "text": text}], "verbatim 不许包裹/加前后缀/改字"
    # verbatim 要**出声**：respond + visibility=[]（respond 已经会让宿主说出来）
    assert pushed["ai_behavior"] == "respond"
    assert pushed["visibility"] == []
    assert pushed["source"] == "neko_wot"
    # kwargs 通道与 metadata 通道同时带 priority / coalesce_key（照抄战雷）
    assert pushed["coalesce_key"] == "neko_wot:battle_event"
    assert pushed["priority"] == 50  # you_killed 属 warning
    assert pushed["metadata"]["priority"] == 50
    assert pushed["metadata"]["coalesce_key"] == "neko_wot:battle_event"
    # 占位符原样送出去（{MASTER_NAME} 是宿主展开的，插件绝不替换）
    assert "{MASTER_NAME}" in text


def test_verbatim_metadata_carries_the_approved_line_and_contract(make_plugin):
    """metadata 是 verbatim 的"不许改写"唯一凭据：推荐句 + 字数上限 + 约束文本。"""
    line = "打得好，再来一个！"
    pushed = _speak_line(make_plugin, line, level="warning", event_id="you_killed")
    metadata = pushed["metadata"]

    assert BASE_METADATA_KEYS <= set(metadata)
    assert CONTRACT_METADATA_KEYS <= set(metadata)
    assert metadata["plugin"] == "neko_wot"
    assert metadata["plugin_recommended_reply"] == line
    assert metadata["event_id"] == "you_killed"
    assert metadata["level"] == "warning"
    assert metadata["priority"] == 50
    assert metadata["coalesce_key"] == "neko_wot:battle_event"
    assert metadata["replace_pending"] is True
    # 字数三处一致，且契约文本里写的就是这个上限（别写死两个 24）
    assert metadata["max_reply_chars"] == plugin_mod.REPLY_MAX_CHARS == 24
    assert metadata["reply_max_chars"] == plugin_mod.REPLY_MAX_CHARS
    assert metadata["reply_contract"] == plugin_mod.REPLY_CONTRACT
    assert metadata["battle_reply_contract"] == plugin_mod.REPLY_CONTRACT
    assert metadata["live_reply_contract"] == plugin_mod.REPLY_CONTRACT
    assert "24" in metadata["reply_contract"]
    assert "复述" in metadata["reply_contract"]


def test_verbatim_does_not_strip_or_reshape_the_line(make_plugin):
    """直接喂一句带花括号/引号/首尾空格的台词：出去的一个字节都不许变。"""
    line = '  跳弹了。“引号”…{MASTER_NAME} '
    pushed = _speak_line(make_plugin, line)

    assert pushed["parts"][0]["text"] == line
    assert pushed["metadata"]["plugin_recommended_reply"] == line


# ------------------------------------------------------------ ③ llm：保持现状
def test_llm_mode_still_pushes_respond_exactly_as_before(tmp_path, make_plugin, plugin_module):
    host, text = _speak_once(tmp_path, make_plugin, plugin_module, output_mode=LLM)

    assert text
    pushed = host.pushed[0]
    assert pushed["ai_behavior"] == "respond"
    assert pushed["visibility"] == []
    assert pushed["parts"] == [{"type": "text", "text": text}]


def test_llm_mode_carries_priority_but_not_the_repeat_contract(make_plugin):
    """llm 要的是"顶替旧战况"，不是"复述这句"——带上契约就等于偷偷变回 verbatim。"""
    pushed = _speak_line(make_plugin, "这场打得漂亮。", level="warning", output_mode=LLM)
    metadata = pushed["metadata"]

    assert BASE_METADATA_KEYS <= set(metadata)
    assert not (CONTRACT_METADATA_KEYS & set(metadata)), sorted(CONTRACT_METADATA_KEYS & set(metadata))
    assert pushed["priority"] == 50
    assert metadata["priority"] == 50
    assert pushed["coalesce_key"] == "neko_wot:battle_event"


# ----------------------------------------------------------- ④ bubble：纯气泡
def test_bubble_mode_is_a_blind_chat_bubble(make_plugin):
    pushed = _speak_line(make_plugin, "装填好了。", level="info", output_mode=BUBBLE)

    assert pushed["ai_behavior"] == "blind"
    assert pushed["visibility"] == ["chat"]
    assert pushed["parts"] == [{"type": "text", "text": "装填好了。"}]
    assert pushed["source"] == "neko_wot"


def test_bubble_mode_still_carries_priority_and_metadata(make_plugin):
    """三种模式都带 metadata：metadata 不花钱，少一条分支就少一个坑。"""
    pushed = _speak_line(make_plugin, "装填好了。", level="info", event_id="reload_done", output_mode=BUBBLE)
    metadata = pushed["metadata"]

    assert BASE_METADATA_KEYS <= set(metadata)
    assert CONTRACT_METADATA_KEYS <= set(metadata)
    assert pushed["priority"] == 10
    assert metadata["priority"] == 10
    assert metadata["event_id"] == "reload_done"
    assert metadata["plugin_recommended_reply"] == "装填好了。"


# ------------------------------------------------- ⑤ priority 与打断开关
@pytest.mark.parametrize(("level", "expected"), [("critical", 90), ("warning", 50), ("info", 10)])
def test_priority_comes_from_level_priority_table(make_plugin, level, expected):
    pushed = _speak_line(make_plugin, "一句话。", level=level, event_id="any_event")

    assert pushed["priority"] == expected
    assert pushed["metadata"]["priority"] == expected
    assert pushed["metadata"]["level"] == level


def test_level_priority_table_is_the_only_source_of_numbers():
    """90 / 50 / 10 只许有一个真相：arbiter.LEVEL_PRIORITY。"""
    table = plugin_mod.arbiter.LEVEL_PRIORITY

    assert table == {"critical": 90, "warning": 50, "info": 10}
    # 契约文本里的上限必须跟着 REPLY_MAX_CHARS 走，不是写死的第二个 24
    assert plugin_mod.REPLY_MAX_CHARS == 24
    assert str(plugin_mod.REPLY_MAX_CHARS) in plugin_mod.REPLY_CONTRACT


def test_interrupt_disabled_downgrades_critical_priority_to_fifty(make_plugin):
    """打断开关的唯一实现方式：critical 的 priority 降到 warning 的 50。"""
    critical = _speak_line(make_plugin, "被击毁了！", level="critical", event_id="you_died", interrupt_enabled=False)

    assert critical["priority"] == 50
    assert critical["metadata"]["priority"] == 50
    assert critical["metadata"]["interrupt_pending"] is False
    # 其它档不受开关影响
    warning = _speak_line(make_plugin, "干掉一个。", level="warning", interrupt_enabled=False)
    assert warning["priority"] == 50


def test_interrupt_enabled_marks_critical_as_interrupt_pending(make_plugin):
    critical = _speak_line(make_plugin, "被击毁了！", level="critical", event_id="you_died")
    assert critical["priority"] == 90
    assert critical["metadata"]["interrupt_pending"] is True

    warning = _speak_line(make_plugin, "干掉一个。", level="warning")
    assert warning["metadata"]["interrupt_pending"] is False


# ------------------------------------------- ⑥ info_channel="bubble"
def test_info_channel_bubble_degrades_only_the_info_tier(make_plugin):
    info = _speak_line(make_plugin, "装填好了。", level="info", info_channel="bubble")
    assert info["ai_behavior"] == "blind"
    assert info["visibility"] == ["chat"]

    warning = _speak_line(make_plugin, "干掉一个。", level="warning", info_channel="bubble")
    assert warning["ai_behavior"] == "respond"
    assert warning["visibility"] == []


@pytest.mark.parametrize("mode", [VERBATIM, LLM])
def test_info_channel_bubble_beats_output_mode(make_plugin, mode):
    """info 档的降级不受 output_mode 影响（哪怕是 llm 模式也一样）。"""
    pushed = _speak_line(make_plugin, "装填好了。", level="info", info_channel="bubble", output_mode=mode)

    assert pushed["ai_behavior"] == "blind"
    assert pushed["visibility"] == ["chat"]


# --------------------------------------------- ⑦ 接线：level / event_id 逐句带真值
def test_poll_once_forwards_the_cue_level_and_event_id(tmp_path, make_plugin, plugin_module):
    path = tmp_path / "state.json"
    state = _write_state(path, seq=1)
    section = _section(state, dry_run=False)
    plugin, host = make_plugin(section)
    plugin._settings = settings_mod.parse_settings(section)
    plugin._arbiter = plugin_module.Arbiter(min_gap_s=0.0)
    plugin._primed = False
    plugin._prev_seq = None

    plugin._poll_once(plugin._settings, plugin._arbiter, time.time())  # 首帧只对齐基线
    _write_state(path, seq=2, events=[{"seq": 7, "id": "you_died", "ts": time.time(), "payload": {}}])
    text = plugin._poll_once(plugin._settings, plugin._arbiter, time.time())

    assert text
    pushed = host.pushed[0]
    assert pushed["parts"][0]["text"] == text
    assert pushed["metadata"]["event_id"] == "you_died"
    assert pushed["metadata"]["level"] == "critical"
    assert pushed["priority"] == 90
    assert pushed["metadata"]["interrupt_pending"] is True


def test_unknown_level_falls_back_to_warning_with_empty_event_id(make_plugin):
    """没 cue / 档位不认识时：level=warning、event_id 留空串（不瞎填 id）。"""
    pushed = _speak_line(make_plugin, "一句话。", level="catastrophic")

    assert pushed["metadata"]["level"] == "warning"
    assert pushed["metadata"]["priority"] == 50
    assert pushed["metadata"]["event_id"] == ""
    assert pushed["priority"] == 50


# ------------------------------------------------------------ ⑧ 非法值回落
@pytest.mark.parametrize("dirty", ["", "   ", "bogus", None, 123, 0, ["verbatim"], {"a": 1}, True])
def test_invalid_output_mode_falls_back_to_verbatim_without_raising(dirty):
    parsed = settings_mod.parse_settings({"output_mode": dirty})

    assert parsed.output_mode == VERBATIM


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("llm", LLM), (" LLM ", LLM), ("Verbatim", VERBATIM), ("  bubble  ", BUBBLE)],
)
def test_output_mode_accepts_legal_values_case_and_space_insensitively(raw, expected):
    assert settings_mod.parse_settings({"output_mode": raw}).output_mode == expected


def test_invalid_output_mode_logs_a_warning_with_the_raw_value(make_plugin):
    plugin, host = make_plugin({"dry_run": True, "output_mode": "verbatum"})
    asyncio.run(plugin.startup())

    warnings = [record for record in host.logger.records if record.startswith("warning:")]
    assert any("output_mode" in record and "verbatum" in record for record in warnings), warnings
    # 回落本身不崩、也不影响其它配置
    assert plugin._settings.output_mode == VERBATIM
    assert plugin._settings.output_mode_invalid is True


def test_default_config_logs_no_output_mode_warning(make_plugin):
    """键整个缺失是"没配"不是"配错"：默认配置不许每次启动都吵。"""
    plugin, host = make_plugin({"dry_run": True})
    asyncio.run(plugin.startup())

    assert not [record for record in host.logger.records if record.startswith("warning:")]


# ------------------------------------------- ⑨ 反向闸：verbatim 不许偷偷变回静音
def test_verbatim_never_produces_a_blind_or_read_push(tmp_path, make_plugin, plugin_module):
    """一次 tick 只推一次、behavior 只有 respond —— 掉回 blind 就等于"永远不出声"。"""
    host, text = _speak_once(tmp_path, make_plugin, plugin_module)

    assert text
    behaviors = [pushed["ai_behavior"] for pushed in host.pushed]
    assert behaviors == ["respond"], f"verbatim 下只许 respond，实际 {behaviors}"
    assert all(pushed.get("visibility") == [] for pushed in host.pushed)
    # 不碰任何 v1 legacy 参数（delivery/reply 会把通道语义改到别处去）
    for legacy in ("delivery", "reply", "description", "content", "message_type"):
        assert not host.pushed[0].get(legacy), legacy


def test_every_mode_still_respects_dry_run(make_plugin):
    """dry_run 优先于 output_mode / info_channel：测试模式下一条都不许真推。"""
    for mode in (VERBATIM, BUBBLE, LLM):
        for info_channel in ("voice", "bubble"):
            section = _section("unused.json", dry_run=True, output_mode=mode, info_channel=info_channel)
            plugin, host = make_plugin(section)
            plugin._settings = settings_mod.parse_settings(section)
            plugin._speak(plugin._settings, "这句话不该出去。", level="critical", event_id="you_died")
            assert host.pushed == [], (mode, info_channel)
