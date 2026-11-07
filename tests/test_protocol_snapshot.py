"""协议快照：拿一份真实形状的 ``state.json`` 走完整条链路。

为什么需要这个文件
------------------
协议 v1 的**权威源在写端（采集端）仓库**，本插件仓库只消费它。两侧各自演进，
中间靠 ``docs/PROTOCOL.md`` 这份契约解耦 —— 这就是唯一的人工同步点。

于是把"插件到底依赖协议里的哪些字段"写成断言，放在这里：写端一旦改字段名、
改事件 id、少写一个键，**这些用例先红**，而不是等到真打一局时才发现
"插件怎么不说话了"。

快照内容取自协议样本帧的形状，``ts`` 固定为常数
以便复现 —— ``read_state_file`` 要显式传 ``now`` 才能绕开陈旧判定。

改协议的流程（约定）
--------------------
1. 先在写端仓库改协议并升 ``schema`` 版本；
2. 再同步更新 ``docs/PROTOCOL.md``、这里的快照与本文件的契约断言；
3. 两侧一起提交，插件版本号至少升 minor。
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import replace

import sdk_stub

_pkg = sdk_stub.load_plugin_module()
arbiter_mod = _pkg.arbiter
state_reader = _pkg.state_reader

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
BATTLE_FRAME = FIXTURES / "frame_v1_battle.json"
END_FRAME = FIXTURES / "frame_v1_battle_end.json"

# 插件真的读过/渲染过的顶层键（少一个 = 协议漂移）
TOP_LEVEL_KEYS = ("schema", "seq", "ts", "mode", "arena_id", "me", "team", "result", "events")

# ``me`` 里插件当前**用到**的字段
ME_USED = ("vehicle_name", "health_ratio")

# ``me`` 里插件**暂时没用、但协议承诺存在**的字段 —— 留作漂移哨兵
ME_RESERVED = ("pos", "ammo", "crew", "modules", "speed", "alive", "health", "max_health")

TEAM_KEYS = ("ally_alive", "ally_total", "enemy_alive", "enemy_total")


def _raw(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read(path: pathlib.Path):
    """按快照自带的 ts 构造一个"刚刚写入"的读取时刻。"""
    return state_reader.read_state_file(path, stale_seconds=5.0, now=_raw(path)["ts"] + 0.5)


def _one_event(frame, event):
    """把快照帧收窄成"只含这一个事件"的一帧，方便逐事件验证。"""
    return replace(frame, events=(event,))


# ---------------------------------------------------------------- 帧结构契约
def test_battle_frame_parses_ok():
    frame = _read(BATTLE_FRAME)
    assert frame.status == state_reader.STATUS_OK, frame.detail
    assert frame.schema == 1
    assert frame.mode == "battle"
    assert frame.arena_id
    assert frame.me is not None


def test_top_level_keys_are_stable():
    for path in (BATTLE_FRAME, END_FRAME):
        raw = _raw(path)
        missing = [key for key in TOP_LEVEL_KEYS if key not in raw]
        assert not missing, f"{path.name} 缺顶层键: {missing}"
        # 协议升级必须同步升快照，否则这里失败
        assert raw["schema"] == state_reader.SCHEMA_VERSION


def test_me_fields_contract():
    me = _raw(BATTLE_FRAME)["me"]
    for key in ME_USED:
        assert key in me, f"插件在用的字段没了: {key}"
    for key in ME_RESERVED:
        assert key in me, f"协议承诺的字段没了: {key}"
    assert isinstance(me["health_ratio"], float)
    assert isinstance(me["alive"], bool)


def test_team_counts_contract():
    team = _raw(BATTLE_FRAME)["team"]
    for key in TEAM_KEYS:
        assert key in team, f"队伍计数缺字段: {key}"
        assert isinstance(team[key], int) and not isinstance(team[key], bool)


# ---------------------------------------------------------------- 事件契约
def test_every_snapshot_event_is_renderable():
    """协议里出现的每种事件，插件都必须说得出人话（否则就是漏实现）。"""
    frame = _read(BATTLE_FRAME)
    assert frame.events, "快照必须带事件样本"

    for event in frame.events:
        arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
        text = arbiter.observe(_one_event(frame, event), frame.ts)
        assert text, f"事件 {event['id']!r} 渲染不出来"

    known = {event["id"] for event in frame.events}
    assert known >= {"battle_start", "you_damaged", "you_killed", "you_assisted"}


def test_assisted_event_contract():
    """`you_assisted`（协议 v1.10）：payload 只有 `total` / `delta`，且都念得出来。

    写端只在"累计 ≥50 步进"或"安静 ≥10s 且确有增长"时发一条，`total` 单调不减。
    插件这边只消费、不推算：`total` 不是非负整数就一个字都不说。
    """
    raw = _raw(BATTLE_FRAME)
    samples = [event for event in raw["events"] if event["id"] == "you_assisted"]
    assert samples, "快照里必须有 you_assisted 样例"

    payload = samples[0]["payload"]
    assert set(payload) == {"total", "delta"}, f"payload 键变了: {sorted(payload)}"
    for key in ("total", "delta"):
        assert isinstance(payload[key], int) and not isinstance(payload[key], bool)

    frame = _read(BATTLE_FRAME)
    event = next(e for e in frame.events if e["id"] == "you_assisted")
    text = arbiter_mod.Arbiter(min_gap_s=0.0).observe(_one_event(frame, event), frame.ts)
    assert text
    assert "三百四十" in text, f"话术没用口语血量口径: {text!r}"
    assert "340" not in text, f"不许念阿拉伯数字: {text!r}"


def test_battle_end_frame_result_numbers():
    frame = _read(END_FRAME)
    assert frame.status == state_reader.STATUS_OK, frame.detail
    assert frame.mode == "battle_end"

    result = frame.result
    assert result is not None
    assert result["won"] is True
    assert result["kills"] == 3
    assert result["damage"] == 2870

    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    text = arbiter.observe(_one_event(frame, frame.events[0]), frame.ts)
    assert "赢了" in text
    assert "3" in text
    assert "2870" in text


# ---------------------------------------------------------------- 合规哨兵
def test_own_position_never_reaches_speech():
    """玩家自己的 pos 在协议里（合规允许读），但绝不允许被念出来。"""
    for path in (BATTLE_FRAME, END_FRAME):
        frame = _read(path)
        coords = _raw(path)["me"]["pos"]
        tokens = [str(value) for value in coords]

        for event in frame.events:
            arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
            text = arbiter.observe(_one_event(frame, event), frame.ts) or ""
            for token in tokens:
                assert token not in text, f"坐标 {token} 漏进了话术: {text!r}"


def test_snapshot_events_carry_no_enemy_identity():
    """快照本身也不许出现敌方单位标识（协议层合规）。"""
    for path in (BATTLE_FRAME, END_FRAME):
        frame = _read(path)
        for event in frame.events:
            payload = event.get("payload") or {}
            for forbidden in ("enemy_position", "victim_name", "tracer", "reload_time"):
                assert forbidden not in payload, f"{path.name} 的 payload 含 {forbidden}"
