"""``state_reader``：7 种 status 各一条，外加"字段全是 null 也不炸"。

所有断言都不依赖真实时钟和真实文件系统副作用——文件写在 ``tmp_path``，
时间靠 ``now`` 显式传入。
"""

from __future__ import annotations

import json

import sdk_stub

_state_reader = sdk_stub.load_plugin_module().state_reader

BASE = {
    "schema": 1,
    "seq": 12,
    "ts": 1000.0,
    "mode": "battle",
    "game_version": "2.4.1.0",
    "player_id": 1000000001,
    "arena_id": "arena-1",
    "me": {"health": 1800, "health_ratio": 0.81, "fires": 0},
    "team": {"ally_alive": 9, "ally_total": 15, "enemy_alive": 12, "enemy_total": 15},
    "result": None,
    "events": [{"seq": 1, "id": "you_killed", "ts": 999.0, "payload": {"ammo": "AP"}}],
}


def _write(tmp_path, payload) -> str:
    path = tmp_path / "state.json"
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_ok_frame_parses_known_fields(tmp_path):
    frame = _state_reader.read_state_file(_write(tmp_path, BASE), stale_seconds=5.0, prev_seq=None, now=1001.0)

    assert frame.status == _state_reader.STATUS_OK
    assert frame.ok is True
    assert frame.schema_warning is False
    assert frame.seq == 12
    assert frame.mode == "battle"
    assert frame.arena_id == "arena-1"
    assert frame.player_id == 1000000001
    assert frame.team["enemy_alive"] == 12
    assert [event["id"] for event in frame.events] == ["you_killed"]


def test_missing_file(tmp_path):
    frame = _state_reader.read_state_file(tmp_path / "nope.json", now=1.0)

    assert frame.status == _state_reader.STATUS_MISSING
    assert frame.seq is None
    assert frame.detail


def test_truncated_json_is_tolerated(tmp_path):
    path = _write(tmp_path, '{"schema": 1, "seq": 12, "events": [{"id": "you_k')
    frame = _state_reader.read_state_file(path, now=1.0)

    assert frame.status == _state_reader.STATUS_TRUNCATED


def test_truncated_at_comma_is_tolerated(tmp_path):
    path = _write(tmp_path, '{"schema": 1, "seq": 12,')
    frame = _state_reader.read_state_file(path, now=1.0)

    assert frame.status == _state_reader.STATUS_TRUNCATED


def test_invalid_json_is_not_mistaken_for_truncation(tmp_path):
    path = _write(tmp_path, '{"schema": 1, "seq": }')
    frame = _state_reader.read_state_file(path, now=1.0)

    assert frame.status == _state_reader.STATUS_INVALID


def test_stale_frame_keeps_data_for_debugging(tmp_path):
    frame = _state_reader.read_state_file(_write(tmp_path, BASE), stale_seconds=5.0, now=1100.0)

    assert frame.status == _state_reader.STATUS_STALE
    assert frame.seq == 12
    assert frame.me["health"] == 1800


def test_duplicate_seq(tmp_path):
    frame = _state_reader.read_state_file(_write(tmp_path, BASE), prev_seq=12, now=1001.0)

    assert frame.status == _state_reader.STATUS_DUPLICATE


def test_unsupported_schema(tmp_path):
    for schema in (0, "1", None, 1.5):
        payload = dict(BASE, schema=schema)
        frame = _state_reader.read_state_file(_write(tmp_path, payload), now=1001.0)
        assert frame.status == _state_reader.STATUS_UNSUPPORTED_SCHEMA, schema


def test_null_everywhere_does_not_raise(tmp_path):
    payload = {
        "schema": 1,
        "seq": 1,
        "ts": 5.0,
        "mode": None,
        "arena_id": None,
        "me": None,
        "team": None,
        "result": None,
        "events": None,
    }
    frame = _state_reader.read_state_file(_write(tmp_path, payload), now=5.0)

    assert frame.status == _state_reader.STATUS_OK
    assert frame.mode is None
    assert frame.me is None
    assert frame.team is None
    assert frame.result is None
    assert frame.events == ()


def test_newer_schema_keeps_known_fields_and_warns(tmp_path):
    payload = dict(BASE, schema=2, future_field={"anything": [1, 2, 3]})
    frame = _state_reader.read_state_file(_write(tmp_path, payload), now=1001.0)

    assert frame.status == _state_reader.STATUS_OK
    assert frame.schema_warning is True
    assert frame.seq == 12


def test_non_dict_events_are_dropped(tmp_path):
    payload = dict(BASE, events=[{"seq": 1, "id": "on_fire"}, "junk", None, 7])
    frame = _state_reader.read_state_file(_write(tmp_path, payload), now=1001.0)

    assert [event["id"] for event in frame.events] == ["on_fire"]
