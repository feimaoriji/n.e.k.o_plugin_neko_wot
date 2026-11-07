"""``settings``：空配置全默认 / 脏值兜底与裁剪 / 示例配置不漂移。"""

from __future__ import annotations

import tomllib

import pytest
import sdk_stub

settings_mod = sdk_stub.load_plugin_module().settings
PLUGIN_DIR = sdk_stub.PLUGIN_DIR


def test_empty_config_yields_defaults():
    parsed = settings_mod.load_settings({})

    assert parsed.dry_run is True
    assert parsed.state_path == ""
    assert parsed.poll_interval_s == 0.25
    assert parsed.stale_seconds == 5.0
    assert parsed.min_gap_s == 2.0
    assert parsed.output_mode == "verbatim"  # v2.9：默认原文直出


def test_dirty_values_fall_back_or_clamp():
    parsed = settings_mod.parse_settings(
        {
            "dry_run": "nope",
            "state_path": None,
            "poll_interval_s": "abc",
            "stale_seconds": float("nan"),
            "min_gap_s": -5,
        }
    )

    assert parsed.dry_run is True
    assert parsed.state_path == ""
    assert parsed.poll_interval_s == 0.25
    assert parsed.stale_seconds == 5.0
    assert parsed.min_gap_s == 0.0  # 有限负数 → 裁剪到下界


def test_out_of_range_values_are_clamped():
    parsed = settings_mod.parse_settings({"poll_interval_s": 99, "min_gap_s": 10000, "dry_run": "false"})

    assert parsed.poll_interval_s == 1.0
    assert parsed.min_gap_s == 300.0
    assert parsed.dry_run is False


def test_example_config_covers_every_default_key():
    with (PLUGIN_DIR / "config.example.toml").open("rb") as handle:
        document = tomllib.load(handle)

    assert set(document["neko_wot"]) == set(settings_mod.DEFAULTS)


# ============================ v2.12：output_mode=bubble / 打断 / 空闲填空 / info 通道
#
# 四个新键的验收口径只有两条：**默认值别漂**、**脏值别抛异常也别悄悄变成别的意思**。
# 后者尤其要紧——``interrupt_enabled`` 写错一个字若回落到 False，玩家会觉得插件"变迟钝了"；
# ``info_channel`` 写错若回落到 bubble，插件会突然哑掉，而这两件事都不会报错。


# ------------------------------------------------- ① 三处默认值必须一致
def test_new_keys_defaults_agree_across_defaults_settings_and_parse():
    parsed = settings_mod.parse_settings({})
    bare = settings_mod.Settings()

    assert settings_mod.DEFAULTS["interrupt_enabled"] is True
    assert settings_mod.DEFAULTS["idle_fill_s"] == 10.0
    assert settings_mod.DEFAULTS["info_channel"] == "voice"

    for settings in (bare, parsed):
        assert settings.interrupt_enabled is True
        assert settings.idle_fill_s == 10.0
        assert settings.info_channel == "voice"
        # 派生标志不是配置键：默认配置下必须是干净的 False，否则启动日志会天天空喊。
        assert settings.output_mode_invalid is False


def test_new_keys_reach_settings_through_nested_load_settings():
    parsed = settings_mod.load_settings(
        {"neko_wot": {"interrupt_enabled": "no", "idle_fill_s": 30, "info_channel": " bubble "}}
    )

    assert parsed.interrupt_enabled is False
    assert parsed.idle_fill_s == 30.0
    assert parsed.info_channel == "bubble"


def test_output_modes_tuple_gained_bubble_without_moving_the_default():
    assert settings_mod.OUTPUT_MODES == ("verbatim", "bubble", "llm")
    assert settings_mod.DEFAULT_OUTPUT_MODE == "verbatim"
    assert settings_mod.DEFAULTS["output_mode"] == "verbatim"


@pytest.mark.parametrize(("raw", "expected"), [("bubble", "bubble"), (" Bubble ", "bubble"), ("BUBBLE", "bubble")])
def test_output_mode_accepts_bubble_case_and_space_insensitively(raw, expected):
    parsed = settings_mod.parse_settings({"output_mode": raw})

    assert parsed.output_mode == expected
    assert parsed.output_mode_invalid is False  # 合法值不算回落


# ------------------------------------------------- ② output_mode 回落标志
@pytest.mark.parametrize(
    "dirty", ["", "   ", "bogus", "verbatimm", "bubbles", None, 123, 0, ["verbatim"], {"a": 1}, True]
)
def test_output_mode_fallback_raises_the_flag(dirty):
    """写过但没写对 → 回退 verbatim + 举旗（启动日志据此告警），仍然不许抛异常。"""
    parsed = settings_mod.parse_settings({"output_mode": dirty})

    assert parsed.output_mode == "verbatim"
    assert parsed.output_mode_invalid is True


@pytest.mark.parametrize("legal", ["verbatim", " VerbAtim ", "llm", " LLM ", "bubble"])
def test_output_mode_flag_stays_off_for_legal_values(legal):
    assert settings_mod.parse_settings({"output_mode": legal}).output_mode_invalid is False


@pytest.mark.parametrize("section", [{}, {"dry_run": True}, {"output_mode": "verbatim"}])
def test_output_mode_flag_stays_off_when_key_is_absent(section):
    """没配 ≠ 配错：键缺失是正常默认路径，若也举旗，真写错时反被噪音淹没。"""
    parsed = settings_mod.parse_settings(section)

    assert parsed.output_mode == "verbatim"
    assert parsed.output_mode_invalid is False


# ------------------------------------------------- ③ interrupt_enabled
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("YES", True),
        (" on ", True),
        ("1", True),
        (1, True),
        (2.5, True),
        ("false", False),
        ("OFF", False),
        ("no", False),
        ("0", False),
        (0, False),
    ],
)
def test_interrupt_enabled_accepts_legal_values(raw, expected):
    assert settings_mod.parse_settings({"interrupt_enabled": raw}).interrupt_enabled is expected


@pytest.mark.parametrize("dirty", ["maybe", "", "   ", "yess", "2", "truthy", None, [], {}, object()])
def test_interrupt_enabled_dirty_values_fall_back_to_true(dirty):
    """回退方向是 True（= 保持 v2.11 的观感），不是 False。"""
    assert settings_mod.parse_settings({"interrupt_enabled": dirty}).interrupt_enabled is True


# ------------------------------------------------- ④ idle_fill_s
@pytest.mark.parametrize(("raw", "expected"), [(5, 5.0), ("12.5", 12.5), (0, 0.0), (" 30 ", 30.0), (300, 300.0)])
def test_idle_fill_s_accepts_numbers_and_numeric_strings(raw, expected):
    assert settings_mod.parse_settings({"idle_fill_s": raw}).idle_fill_s == expected


@pytest.mark.parametrize(
    "dirty", ["abc", "", "   ", None, float("nan"), float("inf"), float("-inf"), [], {}, True, "1,5"]
)
def test_idle_fill_s_dirty_values_fall_back_to_default(dirty):
    assert settings_mod.parse_settings({"idle_fill_s": dirty}).idle_fill_s == 10.0


@pytest.mark.parametrize(("raw", "expected"), [(-5, 0.0), (-0.1, 0.0), (9999, 300.0), (1e9, 300.0)])
def test_idle_fill_s_out_of_range_values_are_clamped(raw, expected):
    """有限越界 → 裁剪到边界（不是回退默认值，那会把用户意图整个抹掉）。"""
    assert settings_mod.parse_settings({"idle_fill_s": raw}).idle_fill_s == expected


def test_idle_fill_bounds_are_declared_alongside_the_other_bounds():
    assert settings_mod.IDLE_FILL_BOUNDS == (0.0, 300.0)


# ------------------------------------------------- ⑤ info_channel
@pytest.mark.parametrize(
    ("raw", "expected"), [("voice", "voice"), ("VOICE", "voice"), (" Bubble ", "bubble"), ("bubble", "bubble")]
)
def test_info_channel_accepts_legal_values(raw, expected):
    assert settings_mod.parse_settings({"info_channel": raw}).info_channel == expected


@pytest.mark.parametrize(
    "dirty", ["", "   ", "tts", "bubbles", "bubble2", None, 1, ["voice"], {"mode": "bubble"}, True]
)
def test_info_channel_dirty_values_fall_back_to_voice(dirty):
    """回退 voice：配置写错时宁可保持出声，也不要让插件毫无征兆地哑掉。"""
    assert settings_mod.parse_settings({"info_channel": dirty}).info_channel == "voice"


def test_info_channel_constants_match_the_parser():
    assert settings_mod.INFO_CHANNELS == ("voice", "bubble")
    assert settings_mod.DEFAULT_INFO_CHANNEL == "voice"


def test_unknown_keys_are_ignored_without_touching_the_new_ones():
    """整段配置里混进无关键 / 别的段，都不许影响这四个新键。"""
    parsed = settings_mod.parse_settings({"output_mode": "bubble", "nope": 1, "interrupt_enabled": False})

    assert parsed.output_mode == "bubble"
    assert parsed.interrupt_enabled is False
    assert parsed.idle_fill_s == 10.0
    assert parsed.info_channel == "voice"
