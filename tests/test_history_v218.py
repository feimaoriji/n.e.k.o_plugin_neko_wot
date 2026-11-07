"""v2.18：本地战绩账本（``history.py``）—— 「本地战绩账本」。

这一批用例只盯账本本身（落盘形状 / 500 行截断 / 原子替换 / 容错），
"什么时候读、写几次"由 ``test_companion_v218.py`` 从仲裁器那一侧钉。

三条不许破的底线：

1. **只碰自己的账本**：路径默认在 ``%LOCALAPPDATA%\\WoT_Copilot\\neko_battles.jsonl``，
   用例一律注入 ``tmp_path``，绝不去碰真实目录（也绝不碰 mod 的 ``state.json``）。
2. **坏数据一律当"没有历史"**：坏行跳过、整份坏/没文件/没权限 → 空表；永不抛异常。
3. **绝不先删再写**：超限时写 ``.tmp`` 再 :func:`os.replace`；替换失败时旧账本必须还在。
"""

from __future__ import annotations

import json
import os

import sdk_stub

_pkg = sdk_stub.load_plugin_module()
history_mod = _pkg.history

Battle = history_mod.Battle
History = history_mod.History


def _battle(*, ts: float = 1000.0, vehicle: str | None = "WZ-219", dmg: int | None = 800,
            result: str = "win", kills: int | None = 2, deaths: int = 1, seconds: int | None = 300,
            map_name: str | None = "荒蛮之地", mode_kind: str | None = "standard") -> object:
    return Battle(
        ts=ts,
        map=map_name,
        vehicle=vehicle,
        mode_kind=mode_kind,
        result=result,
        kills=kills,
        dmg=dmg,
        deaths=deaths,
        seconds=seconds,
    )


def _path(tmp_path):
    return tmp_path / "WoT_Copilot" / "neko_battles.jsonl"


# ------------------------------------------------------------------ ① 落盘形状
def test_append_writes_one_json_line_per_battle(tmp_path):
    path = _path(tmp_path)
    book = History(path)

    assert book.append(_battle(ts=111.0)) is True
    assert book.append(_battle(ts=222.0, result="lose", kills=None, dmg=None, deaths=0, seconds=None)) is True

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first == {
        "ts": 111,
        "map": "荒蛮之地",
        "vehicle": "WZ-219",
        "mode_kind": "standard",
        "result": "win",
        "kills": 2,
        "dmg": 800,
        "deaths": 1,
        "seconds": 300,
        "v": 1,
    }
    assert json.loads(lines[1])["result"] == "lose"
    assert json.loads(lines[1])["kills"] is None, "读不到的数字必须是 null，不许拿 0 顶"


def test_recent_roundtrips_and_keeps_order(tmp_path):
    book = History(_path(tmp_path))
    for index in range(3):
        book.append(_battle(ts=float(100 + index)))

    rows = book.recent()

    assert [row.ts for row in rows] == [100.0, 101.0, 102.0], "旧 → 新"
    assert rows[-1].dmg == 800
    assert book.recent(1) == [rows[-1]]


def test_rows_are_capped_to_the_most_recent_rows(tmp_path):
    """超限 → 只留最近 N 行，且**最旧的那条被裁掉**（不是最新被裁）。"""
    book = History(_path(tmp_path), max_rows=5)
    for index in range(8):
        book.append(_battle(ts=float(index)))

    rows = book.recent()

    assert len(rows) == 5
    assert [row.ts for row in rows] == [3.0, 4.0, 5.0, 6.0, 7.0]
    raw_lines = _path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == 5, "文件本身也要被截断，不能只截内存里的"
    assert not _path(tmp_path).with_name("neko_battles.jsonl.tmp").exists()


# ------------------------------------------------------------------ ② 容错
def test_missing_file_is_an_empty_history(tmp_path):
    assert History(_path(tmp_path)).recent() == []


def test_broken_lines_are_skipped_not_fatal(tmp_path):
    path = _path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps(_battle(ts=1.0).as_row(), ensure_ascii=False)
    path.write_bytes(
        "\n".join(
            [
                "not json at all",
                good,
                '{"ts": 2, "result": "sure_win"}',   # result 不认识 → 坏行
                '{"result": "win"}',                 # 缺 ts → 坏行
                "[1, 2, 3]",                         # 不是 dict → 坏行
                "",
            ]
        ).encode("utf-8")
    )

    rows = History(path).recent()

    assert [row.ts for row in rows] == [1.0]


def test_a_whole_broken_file_reads_as_empty(tmp_path):
    path = _path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe\x00garbage\n\x00\x00")

    assert History(path).recent() == []


def test_unreadable_location_is_treated_as_no_history(tmp_path):
    """路径在"一个普通文件下面"（= 不存在的目录/没权限的同义构造）→ 空表、不抛。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    book = History(blocker / "neko_battles.jsonl")

    assert book.recent() == []


def test_write_failure_returns_false_and_never_raises(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    book = History(blocker / "neko_battles.jsonl")

    assert book.append(_battle()) is False


def test_failed_replace_keeps_the_old_ledger_intact(tmp_path, monkeypatch):
    """**绝不先删再写**：替换失败时旧账本必须原样还在（不是只剩一个空文件）。"""
    path = _path(tmp_path)
    book = History(path)
    assert book.append(_battle(ts=1.0)) is True
    original = path.read_bytes()

    def _boom(src, dst, *args, **kwargs):
        raise OSError(13, "permission denied")

    monkeypatch.setattr(history_mod.os, "replace", _boom)
    assert book.append(_battle(ts=2.0)) is False

    assert path.exists(), "替换失败也不许把原文件删掉"
    assert path.read_bytes() == original
    assert History(path).recent() == [_battle(ts=1.0)]


# ------------------------------------------------------------------ ③ 派生统计
def test_vehicle_average_damage_ignores_nulls_and_other_vehicles(tmp_path):
    rows = [
        _battle(vehicle="WZ-219", dmg=1000),
        _battle(vehicle="WZ-219", dmg=2000),
        _battle(vehicle="WZ-219", dmg=None),
        _battle(vehicle="T-34", dmg=9000),
    ]

    average, samples = history_mod.vehicle_average_damage(rows, "WZ-219")

    assert samples == 2, "null 不是 0，必须整条跳过"
    assert average == 1500.0
    assert history_mod.vehicle_average_damage(rows, None) == (None, 0)
    assert history_mod.vehicle_average_damage(rows, "unknown-tank") == (None, 0)


def test_last_results_returns_the_tail_in_order():
    rows = [_battle(result="lose"), _battle(result="win"), _battle(result="unknown")]

    assert history_mod.last_results(rows, 2) == ["win", "unknown"]
    assert history_mod.last_results(rows, 0) == []
    assert history_mod.last_results([], 3) == []


def test_default_path_is_under_localappdata(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\tester\AppData\Local")

    path = history_mod.default_history_path()

    assert path.name == "neko_battles.jsonl"
    assert path.parent.name == "WoT_Copilot"
    assert str(path).startswith(r"C:\Users\tester\AppData\Local")
    assert path.name != "state.json", "⛔ 绝不许写到 mod 的 state.json"


def test_parse_row_rejects_non_protocol_results():
    good = json.dumps({"ts": 1, "result": "draw", "deaths": 2})
    assert history_mod.parse_row(good) is not None

    for bogus in ('{"ts": 1, "result": "victory"}', '{"ts": 1}', '{"result": "win"}', "[]", "null", "{"):
        assert history_mod.parse_row(bogus) is None, bogus


def test_history_never_raises_when_the_path_is_a_directory(tmp_path):
    """路径本身是个目录（另一种"写不进去"）→ 读写都只是空表/False，绝不抛。"""
    book = History(tmp_path)

    assert book.recent() == []
    assert book.append(_battle()) is False
    assert os.path.isdir(tmp_path)
