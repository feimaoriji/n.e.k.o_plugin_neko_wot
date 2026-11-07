"""/ v2.8：话术表外置（``phrases.json`` + 内置默认 + 分档 + 热更新）。

这一批用例守的是四件事，任何一条失守都等于"玩家的台词被悄悄改了"：

1. **一字不变**：``tests/fixtures/phrases_render_v27.json`` 是**改动前**（v2.7 的
   ``arbiter.py``）跑同一份 ``phrase_matrix`` 得到的快照，现在必须逐字相等；
2. **外置表 == 内置默认表**：``phrases.json`` 与 ``phrases.DEFAULT_PHRASES`` 完全
   相等（含每一档、每一句、顺序）。谁只改了一边，这条先红；
3. **禁词闸**：把 ``phrases.json`` 整份（所有档位、所有变体）逐句过
   ``FORBIDDEN_WORD_GROUPS["ENEMY"]`` —— 外置化之后这是唯一的"文本源头"，
   闸必须扫文件，不能只扫渲染出来的默认结果；
4. **容错与热更新**：文件丢了 / JSON 坏了 / 结构不对 → 退化成内置默认表并告警，
   不崩不提空串；改文件后 ``reload_if_changed`` 能看到新句子，占位符不认识也不
   KeyError。
"""

from __future__ import annotations

import json
import os

import phrase_matrix
import pytest
import sdk_stub

_pkg = sdk_stub.load_plugin_module()
arbiter_mod = _pkg.arbiter
phrases_mod = _pkg.phrases
state_reader = _pkg.state_reader

PLUGIN_DIR = sdk_stub.PLUGIN_DIR
PHRASES_FILE = PLUGIN_DIR / "data" / "phrases.json"
GOLDEN = PLUGIN_DIR / "tests" / "fixtures" / "phrases_render_v27.json"


def _shipped_table() -> dict:
    return json.loads(PHRASES_FILE.read_text(encoding="utf-8"))


def _write_book(tmp_path, events: dict, name: str = "phrases.json"):
    """把一张表落成文件并造一个 ``PhraseBook``（不带 BOM、UTF-8、LF）。"""
    path = tmp_path / name
    text = json.dumps({"version": 1, "events": events}, ensure_ascii=False, indent=2) + "\n"
    path.write_bytes(text.encode("utf-8"))
    return path, phrases_mod.PhraseBook(path)


# ------------------------------------------------------------------ ① 一字不变
def test_rendered_text_is_byte_identical_to_the_pre_refactor_snapshot():
    """核心闸：62 个话术格逐字等于改动前快照。

    快照由 ``tools/snapshot_phrases.py`` 生成（先用**改动前的代码**生成、提交，
    再用改动后的代码比对）。任何一句被改写、被合句、被调换轮换顺序都会在这里红。
    """
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    actual = phrase_matrix.render_matrix(arbiter_mod, state_reader)

    assert set(actual) == set(expected), "矩阵条目变了（增删都要同步快照）"
    mismatched = {key: (expected[key], actual[key]) for key in expected if expected[key] != actual[key]}
    assert mismatched == {}, f"话术被改写了：{mismatched}"


def test_snapshot_covers_every_priority_event():
    """快照必须覆盖 PRIORITY ∪ 派生 id 的每一个事件（漏一个就是漏一句）。"""
    rendered_ids = {case["event"] for case in phrase_matrix.CASES}
    expected_ids = set(arbiter_mod.PRIORITY) | set(arbiter_mod.DERIVED_EVENT_IDS)
    missing = expected_ids - rendered_ids
    assert missing == set(), f"这些事件没有任何快照样本：{sorted(missing)}"


# ------------------------------------------------------- ② 外置表 == 内置默认表
def test_shipped_phrases_file_matches_the_builtin_default_table():
    """只改了一边（json 或代码里的默认表）就必须红。"""
    table = _shipped_table()["events"]
    builtin = {
        event: {tier: list(lines) for tier, lines in tiers.items()}
        for event, tiers in phrases_mod.DEFAULT_PHRASES.items()
    }
    assert table == builtin


def test_shipped_phrases_file_has_no_bom_and_is_valid_utf8():
    raw = PHRASES_FILE.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "phrases.json 不许带 BOM"
    raw.decode("utf-8")  # 不是合法 UTF-8 就抛
    assert json.loads(raw.decode("utf-8"))["version"] == phrases_mod.TABLE_VERSION


def test_default_table_cannot_be_silently_edited_into_a_missing_tier():
    """每一个事件都必须有 ``default`` 或自带的多档；关键档位一个都不许少。"""
    required = {
        "battle_start": {"with_vehicle", "default"},
        "you_damaged": {"low_hp", "low_hp_wrapped", "big_hit", "plain_health", "percent", "unknown"},
        "battle_end": {"head", "draw", "win", "lose", "kills_with_damage", "kills_only", "counted"},
        "you_hit": {"blocked", "no_yaw", "sector"},
        "mode_switched": {"on", "off", "unknown"},
        "you_assisted": {"big", "plain"},
        "ammo_low": {"with_total", "default"},
        "enemy_killed": {"one", "many"},
        "ally_killed": {"one", "many"},
    }
    for event_id, tiers in required.items():
        assert tiers <= set(phrases_mod.DEFAULT_PHRASES[event_id]), event_id

    for event_id in arbiter_mod.PRIORITY:
        assert phrases_mod.DEFAULT_PHRASES.get(event_id), f"{event_id} 没有任何话术"
    for event_id in arbiter_mod.DERIVED_EVENT_IDS:
        assert phrases_mod.DEFAULT_PHRASES.get(event_id), f"{event_id} 没有任何话术"


# --------------------------------------------------------------------- ③ 禁词闸
# v1.30 的**唯一一次放宽**：允许「敌方/敌人/对面 + 数量」（双方人数在记分板
# 上直接可见）。所以这三条闸分两步判：
#   1. ``preach_is_forbidden(渲染后的句子)`` —— 出口闸真实口径（含豁免）；
#   2. 把豁免那一段挖掉之后，ENEMY 组**其它词**一个都不许出现 —— 这条等价于旧的
#      "逐词扫全文"，只是不再冤枉「敌方还剩 3 个」。
# 模板里带 ``{占位符}``，扫之前先按样本值渲染，否则「敌方还剩 {enemy} 个」在词表
# 眼里是"敌方后面没有数字"。
SAMPLE_FIELDS = {
    "MASTER_NAME": "指挥官",
    "vehicle": "WZ-219",
    "kills": 3,
    "damage": 2870,
    "assist": 415,
    "spoken": "八百四十",
    "percent": 96,
    "total": 3,
    "n": 2,
    "left": 3,
    "sector": "左前方",
    "low_hp": "剩个方向盘了",
    "ally": 8,
    "enemy": 3,
    "diff": 3,
}


def _render_sample(line: str) -> str:
    """把模板里的 ``{占位符}`` 换成样本值（人数一律换成数字）。"""
    text = line
    for key, value in SAMPLE_FIELDS.items():
        text = text.replace("{%s}" % key, str(value))
    return text


def _forbidden_hits_after_relaxation(line: str) -> list[str]:
    """按"挖掉人数豁免段"的口径找 ENEMY 组其它词（旧逐词扫的等价物）。"""
    masked = arbiter_mod._mask_enemy_count_phrases(_render_sample(line))
    return [word for word in arbiter_mod.FORBIDDEN_WORD_GROUPS["ENEMY"] if word.lower() in masked.lower()]


def test_every_line_in_the_phrases_file_is_free_of_enemy_words():
    """整份 ``phrases.json``（含所有档位、所有变体）逐条过 ENEMY 禁词组。

    外置化之后文本源头只有这一个文件，所以"扫文件"比"扫默认渲染结果"更严：
    连玩家永远听不到的那几句（例如多句变体里的备用句）也一起扫。
    """
    hits = []
    for event_id, tiers in _shipped_table()["events"].items():
        for tier, lines in tiers.items():
            for line in lines:
                assert not arbiter_mod.preach_is_forbidden(_render_sample(line)), (event_id, tier, line)
                for word in _forbidden_hits_after_relaxation(line):
                    hits.append((event_id, tier, word, line))
    assert hits == [], f"话术表命中敌方禁词：{hits}"


def test_every_line_in_the_builtin_table_is_free_of_enemy_words():
    """内置默认表（退化路径）同样要干净 —— 两条源头不能只扫一条。"""
    hits = []
    for event_id, tiers in phrases_mod.DEFAULT_PHRASES.items():
        for tier, lines in tiers.items():
            for line in lines:
                assert not arbiter_mod.preach_is_forbidden(_render_sample(line)), (event_id, tier, line)
                hits.extend(
                    (event_id, tier, word, line)
                    for word in _forbidden_hits_after_relaxation(line)
                )
    assert hits == [], f"内置表命中敌方禁词：{hits}"


# ---- ③-b 独立硬断言（v2.13 / P6）：**不经过豁免实现**的整表裸词扫描
# 为什么必须另立一条：上面两条扫描都先调 ``_mask_enemy_count_phrases`` 把人数段挖掉再比
# 词表 —— 那是"用被测函数验证自己"。豁免正则一旦越放越宽（把「敌方还剩 3 个装填」整段
# 挖掉），那两条会跟着一起变绿，闸门就成了摆设。
# 这一组刻意**与实现解耦**：
#   1. 期望词表在本文件里**手写一份**（不从 ``arbiter`` 取），放宽人数不会让它自动跟着变；
#   2. 豁免名额也只有「敌方/敌人/对面」三个字面词（同样手写，不 import）；
#   3. 判定只走出口闸 ``preach_is_forbidden``（玩家真正被拦的那一道），不碰任何内部辅助。
# 换句话说：想让它变绿，只能真的从词表里删词 —— 那就该红。
ENEMY_WORDS_EXPECTED = (
    "敌人",
    "敌方",
    "敌军",
    "敌车",
    "对面",
    "位置",
    "坐标",
    "方位",
    "来袭",
    "预测",
    "建议",
    "透视",
    "瞄准",
    "弱点",
    "可见性",
    "透明度",
    "标记",
    "箭头",
    "装填",
    "reload",
)
ENEMY_COUNT_WORDS_COPY = ("敌方", "敌人", "对面")
ENEMY_WORDS_NEVER_EXEMPT = tuple(w for w in ENEMY_WORDS_EXPECTED if w not in ENEMY_COUNT_WORDS_COPY)


def test_enemy_word_table_lost_no_word_outside_the_count_allowance():
    """放宽人数**不许动词表**：除三个计数词外，一个词都不许从 ENEMY 组消失。"""
    enemy = set(arbiter_mod.FORBIDDEN_WORD_GROUPS["ENEMY"])
    missing = [word for word in ENEMY_WORDS_EXPECTED if word not in enemy]
    assert missing == [], f"放宽人数时把词删了：{missing}"
    assert len(ENEMY_WORDS_NEVER_EXEMPT) == 17  # 20 词表 - 3 个计数词（敌方/敌人/对面）


def test_any_sentence_carrying_a_never_exempt_enemy_word_is_still_red():
    """句子里只要还带着这 16 个词中的任何一个 → 判红，**挂在人语句前后也一样**。"""
    for word in ENEMY_WORDS_NEVER_EXEMPT:
        for text in (word, f"敌方还剩 3 个，{word}", f"{word}了，对面剩 2 个"):
            assert arbiter_mod.preach_is_forbidden(text), (word, text)
    # 反向钉住：豁免本身还在（过紧也是回归，另一种假绿）
    for text in ("敌方还剩 3 个。", "敌人只剩两台。", "对面 1 个。"):
        assert not arbiter_mod.preach_is_forbidden(text), text


def test_no_shipped_line_literally_contains_a_never_exempt_enemy_word():
    """整份话术表（外置 + 内置）**字面**不许出现这 16 个词 —— 全程不调用任何豁免函数。"""
    offenders = []
    for source, events in (("shipped", _shipped_table()["events"]), ("builtin", phrases_mod.DEFAULT_PHRASES)):
        for event_id, tiers in events.items():
            for tier, lines in tiers.items():
                for line in lines:
                    rendered = _render_sample(line).lower()
                    offenders.extend(
                        (source, event_id, tier, word, rendered)
                        for word in ENEMY_WORDS_NEVER_EXEMPT
                        if word.lower() in rendered
                    )
    assert offenders == [], f"话术表字面命中敌方词：{offenders}"


def test_every_rendered_template_passes_the_export_gate():
    """渲染出来的每一句仍然要过 ``preach_is_forbidden``（外置化不能绕开出口闸）。"""
    rendered = phrase_matrix.render_matrix(arbiter_mod, state_reader)
    for key, text in rendered.items():
        if text is None:
            continue
        assert not arbiter_mod.preach_is_forbidden(text), f"{key}: {text!r}"
        hits = _forbidden_hits_after_relaxation(text)
        assert hits == [], f"{key}: {hits} in {text!r}"


# ------------------------------------------------------------------ ④ 容错
def test_missing_file_falls_back_to_the_builtin_table_without_crashing(tmp_path):
    book = phrases_mod.PhraseBook(tmp_path / "nope.json")
    assert book.status()["source"] == "builtin"
    assert book.error and "nope.json" in book.error
    assert book.text("you_died") == "我被击毁了…{MASTER_NAME}，缓一下。"


def test_broken_json_falls_back_to_the_builtin_table(tmp_path):
    path = tmp_path / "phrases.json"
    path.write_bytes('{"events": {"you_died": {"default": ["半截'.encode())
    book = phrases_mod.PhraseBook(path)
    assert book.status()["source"] == "builtin"
    assert "JSON" in (book.error or "")
    assert book.text("you_died") == "我被击毁了…{MASTER_NAME}，缓一下。"


def test_wrong_shape_falls_back_to_the_builtin_table(tmp_path):
    for payload in ("[]", '{"events": []}', '{"phrases": {}}', '{"events": {"you_died": {}}}'):
        path = tmp_path / "phrases.json"
        path.write_bytes(payload.encode("utf-8"))
        book = phrases_mod.PhraseBook(path)
        assert book.status()["source"] == "builtin", payload
        assert book.text("you_died") == "我被击毁了…{MASTER_NAME}，缓一下。", payload


def test_a_single_bad_line_does_not_take_down_the_whole_file(tmp_path):
    """坏句子只丢它自己：用户手改漏一个引号不该让插件变哑巴。"""
    path, book = _write_book(
        tmp_path,
        {
            "you_died": {"default": ["改过的台词，{MASTER_NAME}。", "", 42]},
            "time_low": {"default": "不是列表"},
        },
    )
    assert book.status()["source"] == "file"
    assert book.text("you_died") == "改过的台词，{MASTER_NAME}。"
    # 整档坏掉 → 退回内置默认（而不是"没有这句"）
    assert book.text("time_low") == "时间不多了。"


def test_non_string_and_blank_lines_are_dropped_at_load_time(tmp_path):
    """非法值丢弃：非字符串 / 空白句子在**加载时**就丢掉，不进表也不进渲染。

    这条单独立一例，而不是塞进上面那条：上面用 ``text()``（``seed=0`` 只取第一句），
    坏句子排在后面时根本取不到，测不出"到底丢没丢"；这里直接看 ``lines()``。
    """
    _, book = _write_book(
        tmp_path,
        {"you_died": {"default": ["改过的台词，{MASTER_NAME}。", "", "   ", 42, None]}},
    )
    assert book.status()["source"] == "file"
    assert book.lines("you_died") == ("改过的台词，{MASTER_NAME}。",)


def test_partial_file_still_falls_back_per_event(tmp_path):
    """文件只写了三个事件：其余的按内置默认走，不许因此闭嘴。"""
    _, book = _write_book(tmp_path, {"you_died": {"default": ["只剩这句了。"]}})
    assert book.text("you_died") == "只剩这句了。"
    assert book.text("on_fire") == "着火了！{MASTER_NAME}，快处理。"
    assert book.text("no_such_event") is None


# ------------------------------------------------------------- 占位符 / 分档
def test_unknown_placeholders_are_kept_verbatim_instead_of_raising():
    """``{MASTER_NAME}`` 归宿主展开，本层认不出来就原样保留（绝不 KeyError）。"""
    assert phrases_mod.render_line("你好 {MASTER_NAME}。", {}) == "你好 {MASTER_NAME}。"
    assert phrases_mod.render_line("你好 {MASTER_NAME}。", {"kills": 2}) == "你好 {MASTER_NAME}。"
    assert phrases_mod.render_line("{kils} 打错了", {"kills": 2}) == "{kils} 打错了"
    assert phrases_mod.render_line("{kills:>3}", {"kills": 2}) == "{kills:>3}"
    assert phrases_mod.render_line("{0}", {"0": "零"}) == "{0}"
    # None 值同样原样保留（宁可显示花括号，也别显示 "None"）
    assert phrases_mod.render_line("{vehicle}", {"vehicle": None}) == "{vehicle}"


def test_known_placeholders_are_filled():
    line = "开打了！{MASTER_NAME}，{vehicle} 上场，稳住。"
    assert phrases_mod.render_line(line, {"vehicle": "WZ-219"}) == "开打了！{MASTER_NAME}，WZ-219 上场，稳住。"
    assert phrases_mod.render_line("还剩 {percent}% 的血。", {"percent": 96}) == "还剩 96% 的血。"


def test_variants_rotate_by_battle_count_and_are_deterministic(tmp_path):
    """同一档多句变体：按"局内已说次数"轮换，同输入同输出（可测、不随机）。

    轮换种子取 ``_said_counts``，它只在 ``_commit``（真说出口）时 +1 —— 所以这里
    说完一句就补一次 ``_commit``，模拟真实播报节奏。
    """
    _, book = _write_book(
        tmp_path,
        {"time_low": {"default": ["甲", "乙", "丙"]}, "you_died": {"default": ["唯一一句。"]}},
    )

    def speak_four() -> list[str]:
        arbiter = arbiter_mod.Arbiter(min_gap_s=0.0, phrases=book)
        said = []
        for index in range(4):
            said.append(arbiter._say("time_low"))
            arbiter._commit("time_low", float(index))
        return said

    assert speak_four() == ["甲", "乙", "丙", "甲"]
    assert speak_four() == speak_four()  # 确定性：一模一样再来一遍
    # 单句档位不受影响
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0, phrases=book)
    assert {arbiter._say("you_died") for _ in range(5)} == {"唯一一句。"}


def test_variant_rotation_resets_when_a_new_battle_starts():
    """换局归零：上一局说到第几句不许带到下一局。"""
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)
    arbiter._said_counts["time_low"] = 2
    frame = state_reader.Frame(
        status=state_reader.STATUS_OK,
        schema=1,
        seq=1,
        ts=1000.0,
        mode="battle",
        arena_id="arena-2",
        me={},
        events=(),
    )
    arbiter.observe(frame, 0.0)
    assert arbiter._said_counts == {}


def test_phrase_book_can_be_injected_into_the_arbiter(tmp_path):
    """注入一张改过的表 → 说出来就是改过的那句（外置化真的生效了）。"""
    _, book = _write_book(tmp_path, {"you_died": {"default": ["换过的阵亡台词。"]}})
    arbiter = arbiter_mod.Arbiter(min_gap_s=0.0, phrases=book)
    said = arbiter.observe(
        state_reader.Frame(
            status=state_reader.STATUS_OK,
            schema=1,
            seq=1,
            ts=1000.0,
            mode="battle",
            arena_id="arena-1",
            me={},
            events=({"seq": 1, "id": "you_died", "ts": 1000.0, "payload": {}},),
        ),
        0.0,
    )
    assert said == "换过的阵亡台词。"


# ------------------------------------------------------------------ 热更新
def test_reload_if_changed_picks_up_edits_without_a_restart(tmp_path):
    path, book = _write_book(tmp_path, {"you_died": {"default": ["第一版。"]}})
    assert book.text("you_died") == "第一版。"

    text = json.dumps({"version": 1, "events": {"you_died": {"default": ["第二版。"]}}}, ensure_ascii=False)
    path.write_bytes(text.encode("utf-8"))
    # mtime 分辨率不足以稳定区分"同一秒内两次写" → 显式把时间戳推远
    os.utime(path, ns=(2_000_000_000, 2_000_000_000))

    assert book.reload_if_changed() is True
    assert book.text("you_died") == "第二版。"
    # 没再改 → 不重复加载
    assert book.reload_if_changed() is False


def test_broken_hot_reload_keeps_the_previous_table(tmp_path):
    """热更新读到半截 JSON（正在被编辑器写）→ 退回内置默认并告警，不崩。"""
    path, book = _write_book(tmp_path, {"you_died": {"default": ["第一版。"]}})
    path.write_bytes(b'{"events": {"you_died": {')
    os.utime(path, ns=(3_000_000_000, 3_000_000_000))

    assert book.reload_if_changed() is False
    assert book.status()["source"] == "builtin"
    assert book.error
    assert book.text("you_died") == "我被击毁了…{MASTER_NAME}，缓一下。"


def test_poll_once_reloads_the_phrase_table_every_frame(make_plugin, monkeypatch):
    """接线闸：``_poll_once`` 每帧都问一次热更新（否则"改文件下一帧生效"是假的）。"""
    calls = {"reload": 0}

    def fake_reload_if_changed() -> bool:
        calls["reload"] += 1
        return False

    monkeypatch.setattr(phrases_mod.PHRASE_BOOK, "reload_if_changed", fake_reload_if_changed)
    plugin, _host = make_plugin({"state_path": "does-not-matter.json", "poll_interval_s": 0.05})
    plugin._settings = _pkg.settings.parse_settings(
        {"state_path": "does-not-matter.json", "poll_interval_s": 0.05, "stale_seconds": 5.0}
    )
    plugin._arbiter = arbiter_mod.Arbiter(min_gap_s=0.0)

    plugin._poll_once(plugin._settings, plugin._arbiter, 1000.0)
    plugin._poll_once(plugin._settings, plugin._arbiter, 1000.25)

    assert calls["reload"] == 2


def test_startup_logs_where_the_phrases_came_from(make_plugin):
    """退化发生时启动日志必须能看出来源（否则玩家只会觉得"台词改不动"）。"""
    import asyncio

    plugin, host = make_plugin({"dry_run": True})
    asyncio.run(plugin.startup())

    assert any("话术表：source=" in record for record in host.logger.records)


@pytest.mark.parametrize("event_id", sorted(set(arbiter_mod.PRIORITY) | set(arbiter_mod.DERIVED_EVENT_IDS)))
def test_priority_events_all_have_at_least_one_tier(event_id):
    """兜底闸：任何事件都不许"代码里有 id、表里没话"。"""
    assert phrases_mod.PHRASE_BOOK.tiers(event_id), f"{event_id} 在话术表里没有档位"
