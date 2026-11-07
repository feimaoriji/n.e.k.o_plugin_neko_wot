"""插件接线：dry_run 只打日志 / 真发一次 / 三连 tick 去重 / 定时器与收尾。

时钟不 patch（仓库的 clock_guard 会拦）；状态文件的 ``ts`` 直接取 ``time.time()``，
tick 预算用 monkeypatch 模块常量压短，避免每个用例真等 1 秒。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time

import pytest
import sdk_stub

TICK_BUDGET = 0.15


def _write_state(path, *, seq: int, events: list[dict] | None = None, mode: str = "battle") -> str:
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
    text = json.dumps(payload, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
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


async def test_dry_run_logs_and_never_pushes(tmp_path, make_plugin, plugin_module, monkeypatch):
    monkeypatch.setattr(plugin_module, "TICK_SECONDS", TICK_BUDGET)
    path = tmp_path / "state.json"
    state = _write_state(path, seq=1)
    plugin, host = make_plugin(_section(state))

    await plugin.startup()
    await plugin.wot_poll()  # 首帧只对齐基线
    _write_state(path, seq=2, events=[_kill_event()])
    await plugin.wot_poll()

    assert host.pushed == []
    assert any("本应说出" in record for record in host.logger.records)


async def test_real_push_uses_v2_fields(tmp_path, make_plugin, plugin_module, monkeypatch):
    """llm 模式（v2.8 及以前的默认）下仍然只用 v2 字段推 respond。

    v2.12 起默认是 verbatim（respond + 复述契约 metadata），所以这条要**显式**要求
    llm 模式 —— 它守的是"润色那条老路一个字都没变"，不是默认值。
    """
    monkeypatch.setattr(plugin_module, "TICK_SECONDS", TICK_BUDGET)
    path = tmp_path / "state.json"
    state = _write_state(path, seq=1)
    plugin, host = make_plugin(_section(state, dry_run=False, output_mode="llm"))

    await plugin.startup()
    await plugin.wot_poll()  # 首帧只对齐基线
    _write_state(path, seq=2, events=[_kill_event()])
    await plugin.wot_poll()

    assert len(host.pushed) == 1
    pushed = host.pushed[0]
    assert pushed["visibility"] == []
    assert pushed["ai_behavior"] == "respond"
    assert pushed["source"] == "neko_wot"
    assert pushed["parts"][0]["type"] == "text"
    assert "{MASTER_NAME}" in pushed["parts"][0]["text"]
    # 只用 v2 字段，不碰任何 v1 legacy 参数（它们会发 DeprecationWarning）
    for legacy in (
        "message_type",
        "description",
        "content",
        "binary_data",
        "binary_url",
        "mime",
        "unsafe",
        "fast_mode",
        "delivery",
        "reply",
    ):
        assert not pushed.get(legacy), legacy


async def test_three_ticks_speak_once(tmp_path, make_plugin, plugin_module, monkeypatch):
    monkeypatch.setattr(plugin_module, "TICK_SECONDS", TICK_BUDGET)
    path = tmp_path / "state.json"
    state = _write_state(path, seq=1)
    plugin, host = make_plugin(_section(state, dry_run=False))
    await plugin.startup()
    # 把同类冷却清零：这个用例只验证 (arena_id, seq) 去重
    plugin._arbiter._cooldown["you_killed"] = 0.0

    await plugin.wot_poll()  # 首帧只对齐基线
    _write_state(path, seq=2, events=[_kill_event()])
    await plugin.wot_poll()
    # 数据端改成滑动窗口后会重发同一条事件，只换顶层 seq
    _write_state(path, seq=3, events=[_kill_event()])
    await plugin.wot_poll()
    _write_state(path, seq=4, events=[_kill_event()])
    await plugin.wot_poll()

    assert len(host.pushed) == 1


async def test_first_frame_never_replays_history(tmp_path, make_plugin, plugin_module, monkeypatch):
    """插件重启时盘上躺着上一局的历史事件，一句都不许复述（实测）。"""
    monkeypatch.setattr(plugin_module, "TICK_SECONDS", TICK_BUDGET)
    history = [
        {"seq": 11, "id": "battle_start", "ts": time.time(), "payload": {}},
        {"seq": 12, "id": "you_killed", "ts": time.time(), "payload": {}},
        {"seq": 13, "id": "you_died", "ts": time.time(), "payload": {}},
        {"seq": 14, "id": "battle_end", "ts": time.time(), "payload": {}},
    ]
    path = tmp_path / "state.json"
    state = _write_state(path, seq=40, events=history, mode="garage")
    plugin, host = make_plugin(_section(state, dry_run=False))

    await plugin.startup()
    await plugin.wot_poll()
    assert host.pushed == []

    # 基线对齐之后，窗口里新追加的事件照常播报（并且只播新的一条）
    _write_state(
        path,
        seq=41,
        events=[*history[1:], {"seq": 15, "id": "you_respawned", "ts": time.time(), "payload": {}}],
    )
    await plugin.wot_poll()
    assert len(host.pushed) == 1
    assert "又活了" in host.pushed[0]["parts"][0]["text"]


async def test_prime_does_not_count_history_kills(tmp_path, make_plugin, plugin_module, monkeypatch):
    """基线里的击杀不算"这一局我打掉的"，否则第一句就是"第 9 个"。"""
    monkeypatch.setattr(plugin_module, "TICK_SECONDS", TICK_BUDGET)
    history = [
        {"seq": n, "id": "you_killed", "ts": time.time(), "payload": {}} for n in range(1, 9)
    ]
    path = tmp_path / "state.json"
    state = _write_state(path, seq=1, events=history)
    plugin, host = make_plugin(_section(state, dry_run=False))
    await plugin.startup()

    await plugin.wot_poll()
    assert host.pushed == []
    assert plugin._arbiter._kills == 0

    _write_state(path, seq=2, events=[*history, _kill_event(seq=9)])
    await plugin.wot_poll()
    assert len(host.pushed) == 1
    assert "第 1 个" in host.pushed[0]["parts"][0]["text"]


async def test_only_wot_poll_timer_and_shutdown_clears_state(tmp_path, make_plugin, plugin_module, monkeypatch):
    monkeypatch.setattr(plugin_module, "TICK_SECONDS", TICK_BUDGET)

    timers = {}
    for name, member in vars(plugin_module.NekoWotPlugin).items():
        meta = getattr(member, sdk_stub.EVENT_META_ATTR, None)
        if meta is not None and getattr(meta, "kind", "") == "timer":
            timers[name] = meta
    assert set(timers) == {"wot_poll"}
    assert timers["wot_poll"].extra.get("seconds") == 1

    state = _write_state(tmp_path / "state.json", seq=1, events=[_kill_event()])
    plugin, _host = make_plugin(_section(state))
    await plugin.startup()

    before = asyncio.all_tasks()
    await plugin.wot_poll()
    assert asyncio.all_tasks() - before == set()

    await plugin.shutdown()
    assert plugin._settings is None
    assert plugin._arbiter is None
    assert plugin._prev_seq is None


def test_push_message_signature_is_not_a_catch_all(make_plugin, sdk_backend_name):
    """桩必须和真 SDK 同形；写错关键字时只有桩会当场 TypeError。

    真 SDK 的 ``NekoPluginBase.push_message(**kwargs)`` 会把参数原样透传给宿主
    ctx，所以"未知关键字报错"这件事在生产里由宿主决定——这条用例只在桩后端跑，
    用来证明桩本身没有 ``**kwargs`` 兜底。签名漂移由下一条用例负责。
    """
    plugin, _host = make_plugin({})

    with pytest.raises(TypeError):
        plugin.push_message(["chat"])

    if sdk_backend_name != "stub":
        pytest.skip("真 SDK 的 ctx 层按 **kwargs 透传，未知关键字不在这一层拦截")
    with pytest.raises(TypeError):
        plugin.push_message(text="x")


def test_push_message_signature_matches_the_real_sdk(sdk_backend_name):
    expected = {
        "visibility",
        "ai_behavior",
        "parts",
        "source",
        "target_lanlan",
        "metadata",
        "priority",
        "coalesce_key",
        "message_type",
        "description",
        "content",
        "binary_data",
        "binary_url",
        "mime",
        "unsafe",
        "fast_mode",
        "delivery",
        "reply",
    }
    stub_params = inspect.signature(sdk_stub.StubPluginBase.push_message).parameters
    assert set(stub_params) - {"self"} == expected

    if sdk_backend_name != "real":
        pytest.skip("桩后端下没有真 SDK 可对照")
    from plugin.sdk.shared.core.context import SdkContext

    real_params = inspect.signature(SdkContext.push_message).parameters
    assert set(real_params) - {"self"} == expected


def test_poll_before_startup_is_idle(make_plugin):
    plugin, host = make_plugin({})

    result = asyncio.run(plugin.wot_poll())

    assert result.value["status"] == "idle"
    assert host.pushed == []
