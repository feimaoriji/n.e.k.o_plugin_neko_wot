"""``[neko_wot]`` 配置段的默认值、容错与裁剪。

配置是用户手写的 TOML，什么脏值都可能出现：字符串、负数、NaN、整个段不存在。
这里的规矩是"永不抛异常"——读不懂就用默认值，越界就裁剪到边界内。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

SECTION = "neko_wot"

# 输出模式（v2.9 起；v2.12 起三模式）：决定"这句审过的台词怎么送出去"。
#
# * ``verbatim``（默认）—— **原文被复述出来**：``ai_behavior="respond"`` + ``visibility=[]``，
#   靠 metadata 里的推荐句（``plugin_recommended_reply``）+ 字数上限（``max_reply_chars``）
#   + 约束文本（``reply_contract``）把 LLM 的活退化成"照这句说"。**会出声**。
#   v2.9 曾用 ``blind`` + ``["chat"]`` 直接显示原文，代价是永远不出声；插件侧没有
#   "直接朗读固定文本"的入口，要出声就只能走 ``respond`` —— 通道结论与改动理由见
#   README「输出模式」一节。
# * ``bubble``（v2.12）—— **只弹气泡、不发声**：``blind`` + ``visibility=["chat"]``，
#   0 token、不进模型上下文、不被 LLM 改写（不建 callback、不起 turn、不碰 TTS）。
#   给"看着就行、别在我耳边说话"的场景用。
# * ``llm`` —— 现状：``ai_behavior="respond"``，交给宿主 LLM 润色后再回复（不带复述契约）。
#
# 写错/留空/不是字符串 → 一律回落到 ``verbatim``（不抛异常），并在 startup 打一条 WARNING。
# 测试期默认 ``verbatim``：要验的是"台词表本身好不好"，任何 LLM 改写都会把
# 台词问题（措辞、长度、禁用词）跟润色效果混在一起，测不出结论。
DEFAULT_OUTPUT_MODE = "verbatim"
OUTPUT_MODE_VERBATIM = "verbatim"
OUTPUT_MODE_BUBBLE = "bubble"
OUTPUT_MODE_LLM = "llm"
# 顺序即文档顺序（README 配置表 / 输出模式一节按这个次序排）。
OUTPUT_MODES = (OUTPUT_MODE_VERBATIM, OUTPUT_MODE_BUBBLE, OUTPUT_MODE_LLM)

# 信息类播报（v2.12）走哪条通道：``voice`` = 出声（走 TTS），``bubble`` = 只弹气泡。
#
# 默认 ``voice`` 是"保持既有体验"的选择：v2.11 及以前只有出声一条路，玩家升级
# 不该突然变成哑巴。想清静的人显式写 ``info_channel = "bubble"``。
DEFAULT_INFO_CHANNEL = "voice"
INFO_CHANNEL_VOICE = "voice"
INFO_CHANNEL_BUBBLE = "bubble"
INFO_CHANNELS = (INFO_CHANNEL_VOICE, INFO_CHANNEL_BUBBLE)

# 队伍事件（队友击杀 / 队友阵亡）默认**不出声**（v1.30）。
#
# 设计口径：「没必要，击杀播报只聚焦用户本人」—— 那一局 15 次队友击杀
# 全播了。所以这两个派生 id 默认零播报，想听的人显式打开。
#
# 打开后只按 ``info`` 档说（不占 warning/critical，也不抢那两档的话），仍受同类
# 冷却与 ``min_gap_s`` 管。脏值一律回退 ``False``（"没说清"要按"静音"处理：这条
# 配置的存在意义就是少说话，读不懂时不该反而更吵）。
DEFAULT_TEAM_EVENTS_ENABLED = False

# 决策日志（v2.15 / P0）：仲裁器每帧的候选 / 选中 / 丢弃原因 / 说出口 / 队列动作
# 要不要打进插件日志。**默认 true**：实机缺陷排查全靠它，
# 因为"成功推送"原来一个字都不打 —— 观测盲区比日志噪音贵得多。
# 关掉只影响这些 INFO 行，不影响播报行为；脏值一律回退 true（这条开关的存在意义
# 是排障，读不懂时应当保留日志，而不是悄悄变哑）。
DEFAULT_DEBUG_LOG = True

# ---- v2.18 / P1「陪伴四件套」的五把旋钮 ----
#
# README「陪伴能力」里的 C1/C2/C3/C4/C8。五条都是"说得更像人"，
# 不新增任何"播什么"的敌方情报；默认值一律取**更安静/更克制**的那一档：
#
# * ``history_enabled``（C2 战绩账本）：False = **不写不读、一个字节都不落盘**。
#   关掉时 C1 的"历史均伤"档与 C3 的连败关怀一并失效（它们的数据源就是账本）。
# * ``recap_enabled``（C1 复盘分档）：False = 回到 v2.17 那句默认结算文案。
# * ``idle_deep_s``（C4 静默二阶）：0 = 关。它是"一局最多一次"的不依赖战况的陪伴，
#   所以给 0~300 秒的边界（和 ``idle_fill_s`` 同量级），越界裁剪而不是报错。
# * ``speech_budget_per_battle``（C8 单局总量）：0 = 不限。超了**直接丢**（不排队）。
# * ``speech_burst_max``（C8 滑动窗）：窗口固定 :data:`SPEECH_BURST_WINDOW_S` 秒，
#   ``critical`` 不受它限制（但计入单局总量）。1~20；配置层**不允许**关掉窗闸
#   （要"完全放开"就把单局总量设 0 并自己接受话痨，而不是偷偷让窗闸失效）。
DEFAULT_HISTORY_ENABLED = True
DEFAULT_RECAP_ENABLED = True
DEFAULT_IDLE_DEEP_S = 60.0
DEFAULT_SPEECH_BUDGET_PER_BATTLE = 40
DEFAULT_SPEECH_BURST_MAX = 3

# ---- v2.19 / P2「C5 受击分档 / C6 劣势细分 / C7 开场人格化」的四把旋钮 ----
#
# * ``hit_reaction_enabled``（C5 受击反应分档）：默认 **开**。关掉 = 回到 v2.18 的
#   纯血量播报（heavy/critical 档、跳弹“没打穿”、连续挨打合并都不再出现）。
# * ``hit_reaction_level``（C5 档位）：默认 ``quiet``（**更安静的那一档**）。
#   ``quiet`` = 按路线图口径（≥25% 重档 / ≥50% critical）；``full`` = 阈值下移到
#   15% / 35%（更敏感）。两个档位共用同一套密度上限（15 秒 / 单局 8 句），
#   差别只在“多早开始升级口径”。
# * ``team_behind_tiers_enabled``（C6 劣势细分）：默认 **开**（现状 team_behind
#   本来就会说话，本单只是把它分成 mild/big/endgame 三档，不新增事件 id）。
#   关掉 = 回到 v2.18 那句“我们这边落后了，稳住。”。
# * ``battle_start_persona_enabled``（C7 开场人格化）：默认 **开**。开 = 有可信账本时
#   把开场那句换成“今天第 N 局 / 本车均伤”的选句；账本缺失/损坏/为空/关掉账本 →
#   退回 v2.18 的通用开场句（**绝不猜数字**）。
DEFAULT_HIT_REACTION_ENABLED = True
DEFAULT_HIT_REACTION_LEVEL = "quiet"
HIT_REACTION_QUIET = "quiet"
HIT_REACTION_FULL = "full"
HIT_REACTION_LEVELS = (HIT_REACTION_QUIET, HIT_REACTION_FULL)
DEFAULT_TEAM_BEHIND_TIERS_ENABLED = True
DEFAULT_BATTLE_START_PERSONA_ENABLED = True

# ---- v2.20 / P3「断带双层播报 + 机制话术分档」的三把旋钮 ----
#
# * ``track_break_enabled``（断带/断轮双层播报）：默认 **开**。只消费协议已有的
#   ``you_hit.crits_devices`` / ``crits_count`` / ``crits``；``crits_devices == []``、
#   ``crits_count != 1``、``blocked == true``、非行走机构一律不说“履带/轮子”。
#   关掉 = 回到 v2.19 的普通受击方向句（不新增任何断带话术）。
# * ``mech_opening_enabled``（机制开场自述）：默认 **开**。只在 ``me.mech`` 非空且能
#   认出 9 类固定 key 时把开场句换成机制自述；C7 人格化开场优先于它（两者都可用
#   时只出 C7）。``me.mech == []`` / 认不出来 → 通用开场句。
# * ``mech_switch_enabled``（机制切换分档）：默认 **开**。按 ``mode_switched.mech``
#   的五个已知值选句；``unknown`` / 缺键 → 退回 v2.19 那三句。20 秒冷却与
#   “只报人工切换”的既有闸门一个字都不动。
DEFAULT_TRACK_BREAK_ENABLED = True
DEFAULT_MECH_OPENING_ENABLED = True
DEFAULT_MECH_SWITCH_ENABLED = True
# 滑动窗长度（秒）。**故意不做成配置键**：多一个键就多一种"窗和上限对不上"的错配，
# 而 30 秒是"两句话之间的最小呼吸"这个产品口径的一部分。
SPEECH_BURST_WINDOW_S = 30.0

DEFAULTS: dict[str, Any] = {
    "dry_run": True,
    "state_path": "",
    "poll_interval_s": 0.25,
    "stale_seconds": 5.0,
    "min_gap_s": 2.0,
    "output_mode": DEFAULT_OUTPUT_MODE,
    "interrupt_enabled": True,
    "idle_fill_s": 10.0,
    "info_channel": DEFAULT_INFO_CHANNEL,
    "team_events_enabled": DEFAULT_TEAM_EVENTS_ENABLED,
    "debug_log": DEFAULT_DEBUG_LOG,
    "history_enabled": DEFAULT_HISTORY_ENABLED,
    "recap_enabled": DEFAULT_RECAP_ENABLED,
    "idle_deep_s": DEFAULT_IDLE_DEEP_S,
    "speech_budget_per_battle": DEFAULT_SPEECH_BUDGET_PER_BATTLE,
    "speech_burst_max": DEFAULT_SPEECH_BURST_MAX,
    # ---- v2.19 / P2 ----
    "hit_reaction_enabled": DEFAULT_HIT_REACTION_ENABLED,
    "hit_reaction_level": DEFAULT_HIT_REACTION_LEVEL,
    "team_behind_tiers_enabled": DEFAULT_TEAM_BEHIND_TIERS_ENABLED,
    "battle_start_persona_enabled": DEFAULT_BATTLE_START_PERSONA_ENABLED,
    # ---- v2.20 / P3 ----
    "track_break_enabled": DEFAULT_TRACK_BREAK_ENABLED,
    "mech_opening_enabled": DEFAULT_MECH_OPENING_ENABLED,
    "mech_switch_enabled": DEFAULT_MECH_SWITCH_ENABLED,
}

# 一轮 tick 预算 1s，所以 tick 内的采样间隔不能超过它，也不能小到空转。
POLL_INTERVAL_BOUNDS = (0.05, 1.0)
STALE_BOUNDS = (0.5, 300.0)
MIN_GAP_BOUNDS = (0.0, 300.0)
# 空闲填空的间隔（v2.12）：0 = 不填（等于关掉这个功能），上限 5 分钟 —— 再长就不是
# "填空"而是"玩家已经忘了你在陪他"。和 ``MIN_GAP_BOUNDS`` 同量级，越界裁剪而不是报错。
IDLE_FILL_BOUNDS = (0.0, 300.0)
# 二阶静默（v2.18 / C4）：和 ``IDLE_FILL_BOUNDS`` 同一把尺子 —— 再长就不是"填空"
# 而是"玩家已经忘了你在陪他"。0 = 关。
IDLE_DEEP_BOUNDS = (0.0, 300.0)
# 单局话术总量（v2.18 / C8）：0 = 不限；上限 500 是"防呆"而不是"推荐值"
# （正常一局 15~30 句，40 已经是很宽的口径）。
SPEECH_BUDGET_BOUNDS = (0, 500)
# 滑动窗内上限（v2.18 / C8）：1~20。**最小值是 1，不是 0** —— 关掉窗闸的唯一办法
# 是把单局总量设 0（"我就要它一直说"是产品决策，不该藏在窗闸的 0 值里）。
SPEECH_BURST_BOUNDS = (1, 20)


@dataclass(frozen=True)
class Settings:
    dry_run: bool = DEFAULTS["dry_run"]
    state_path: str = DEFAULTS["state_path"]
    poll_interval_s: float = DEFAULTS["poll_interval_s"]
    stale_seconds: float = DEFAULTS["stale_seconds"]
    min_gap_s: float = DEFAULTS["min_gap_s"]
    output_mode: str = DEFAULTS["output_mode"]
    # 派生标志，**不是配置键**（所以不进 ``DEFAULTS``，也不进 config.example.toml）：
    # ``output_mode`` 写过但没写对、已经静默回落到默认值时为 True。回落本身不吵，
    # "我明明写了 llm 它却不说话"这类问题只能靠启动日志里的这一句定位。
    output_mode_invalid: bool = False
    interrupt_enabled: bool = DEFAULTS["interrupt_enabled"]
    idle_fill_s: float = DEFAULTS["idle_fill_s"]
    info_channel: str = DEFAULTS["info_channel"]
    # 队伍事件开关（v1.30）：默认 False = 队友击杀/队友阵亡不出声。
    team_events_enabled: bool = DEFAULTS["team_events_enabled"]
    # 决策日志（v2.15 / P0）：默认 True = 仲裁器的候选/丢弃/说出口都写进插件日志。
    debug_log: bool = DEFAULTS["debug_log"]
    # ---- v2.18 / P1：陪伴四件套 ----
    # 战绩账本（C2）：默认 True；False = 不写不读（历史均伤档与连败关怀一并失效）。
    history_enabled: bool = DEFAULTS["history_enabled"]
    # 复盘分档（C1）：默认 True；False = 回到 v2.17 那句默认结算文案。
    recap_enabled: bool = DEFAULTS["recap_enabled"]
    # 静默二阶（C4）：默认 60 秒；0 = 关。
    idle_deep_s: float = DEFAULTS["idle_deep_s"]
    # 单局话术总量（C8）：默认 40；0 = 不限。
    speech_budget_per_battle: int = DEFAULTS["speech_budget_per_battle"]
    # 滑动窗上限（C8）：默认 3 句 / 30 秒；1~20。
    speech_burst_max: int = DEFAULTS["speech_burst_max"]
    # ---- v2.19 / P2：C5 / C6 / C7 ----
    # 受击反应分档（C5）：默认 True；False = 回到 v2.18 的纯血量播报。
    hit_reaction_enabled: bool = DEFAULTS["hit_reaction_enabled"]
    # 受击档位（C5）：默认 quiet；只认 quiet/full，脏值回落 quiet。
    hit_reaction_level: str = DEFAULTS["hit_reaction_level"]
    # 劣势细分（C6）：默认 True；False = 回到 v2.18 的单句“落后了”。
    team_behind_tiers_enabled: bool = DEFAULTS["team_behind_tiers_enabled"]
    # 开场人格化（C7）：默认 True；False = 永远用通用开场句。
    battle_start_persona_enabled: bool = DEFAULTS["battle_start_persona_enabled"]
    # ---- v2.20 / P3：断带双层 + 机制分档 ----
    # 断带/断轮双层播报：默认 True；False = 只保留普通受击方向句。
    track_break_enabled: bool = DEFAULTS["track_break_enabled"]
    # 机制开场自述：默认 True；False = 永远用通用开场句（C7 仍可生效）。
    mech_opening_enabled: bool = DEFAULTS["mech_opening_enabled"]
    # 机制切换分档：默认 True；False = 回到“战斗模式开了/切回普通了”。
    mech_switch_enabled: bool = DEFAULTS["mech_switch_enabled"]


def load_settings(config: object) -> Settings:
    """从 ``config.dump()`` 的整份配置里取 ``[neko_wot]`` 段。"""
    section = config.get(SECTION) if isinstance(config, Mapping) else None
    return parse_settings(section)


def parse_settings(section: object) -> Settings:
    raw: Mapping[str, Any] = section if isinstance(section, Mapping) else {}
    output_mode, output_mode_fell_back = _resolve_output_mode(raw.get("output_mode"))
    return Settings(
        dry_run=_as_bool(raw.get("dry_run"), DEFAULTS["dry_run"]),
        state_path=_as_path(raw.get("state_path")),
        poll_interval_s=_as_float(raw.get("poll_interval_s"), DEFAULTS["poll_interval_s"], POLL_INTERVAL_BOUNDS),
        stale_seconds=_as_float(raw.get("stale_seconds"), DEFAULTS["stale_seconds"], STALE_BOUNDS),
        min_gap_s=_as_float(raw.get("min_gap_s"), DEFAULTS["min_gap_s"], MIN_GAP_BOUNDS),
        output_mode=output_mode,
        # 键整个缺失**不算**回落：那是"没配"，不是"配错"。混为一谈的话默认配置每次
        # 启动都会告警，真写错时那条告警反而被噪音淹掉。
        output_mode_invalid=output_mode_fell_back and "output_mode" in raw,
        interrupt_enabled=_as_bool(raw.get("interrupt_enabled"), DEFAULTS["interrupt_enabled"]),
        idle_fill_s=_as_float(raw.get("idle_fill_s"), DEFAULTS["idle_fill_s"], IDLE_FILL_BOUNDS),
        info_channel=_as_info_channel(raw.get("info_channel")),
        # 认不出来的值（"maybe" 这种字符串、``None``）→ False。理由见常量注释：
        # 这条开关的本意是少说话，读不懂时落到"更安静"的一侧。
        team_events_enabled=_as_bool(raw.get("team_events_enabled"), DEFAULTS["team_events_enabled"]),
        # 决策日志（v2.15）：认不出来的值 → True（这条开关的意义是排障，读不懂时留日志）。
        debug_log=_as_bool(raw.get("debug_log"), DEFAULTS["debug_log"]),
        # ---- v2.18 / P1：陪伴四件套 ----
        # 两个开关认不出来 → True（"没配"= 保持路线图的默认档；关掉是显式动作）。
        history_enabled=_as_bool(raw.get("history_enabled"), DEFAULTS["history_enabled"]),
        recap_enabled=_as_bool(raw.get("recap_enabled"), DEFAULTS["recap_enabled"]),
        idle_deep_s=_as_float(raw.get("idle_deep_s"), DEFAULTS["idle_deep_s"], IDLE_DEEP_BOUNDS),
        speech_budget_per_battle=_as_int(
            raw.get("speech_budget_per_battle"),
            DEFAULTS["speech_budget_per_battle"],
            SPEECH_BUDGET_BOUNDS,
        ),
        speech_burst_max=_as_int(raw.get("speech_burst_max"), DEFAULTS["speech_burst_max"], SPEECH_BURST_BOUNDS),
        # ---- v2.19 / P2：C5 / C6 / C7 ----
        # 三个开关认不出来 → 默认档（“没配”= 按路线图默认；关掉是显式动作）。
        hit_reaction_enabled=_as_bool(raw.get("hit_reaction_enabled"), DEFAULTS["hit_reaction_enabled"]),
        hit_reaction_level=_as_hit_reaction_level(raw.get("hit_reaction_level")),
        team_behind_tiers_enabled=_as_bool(
            raw.get("team_behind_tiers_enabled"), DEFAULTS["team_behind_tiers_enabled"]
        ),
        battle_start_persona_enabled=_as_bool(
            raw.get("battle_start_persona_enabled"), DEFAULTS["battle_start_persona_enabled"]
        ),
        # ---- v2.20 / P3：断带双层 + 机制分档 ----
        # 三个开关认不出来 → True（“没配”= 保持路线图默认；关掉是显式动作）。
        track_break_enabled=_as_bool(raw.get("track_break_enabled"), DEFAULTS["track_break_enabled"]),
        mech_opening_enabled=_as_bool(raw.get("mech_opening_enabled"), DEFAULTS["mech_opening_enabled"]),
        mech_switch_enabled=_as_bool(raw.get("mech_switch_enabled"), DEFAULTS["mech_switch_enabled"]),
    )


def _resolve_output_mode(value: object) -> tuple[str, bool]:
    """→ ``(模式, 是否回落过)``。第二个返回值只喂 ``Settings.output_mode_invalid``。"""
    if isinstance(value, str):
        text = value.strip().lower()
        if text in OUTPUT_MODES:
            return text, False
    return DEFAULT_OUTPUT_MODE, True


def _as_output_mode(value: object) -> str:
    """只认 ``OUTPUT_MODES``（大小写、首尾空白宽容）。

    其它一律回退默认 —— 空串、数字、``None``、``"bogus"`` 都**不抛异常**：
    配置是用户手写的，写错一个字母不该让插件起不来。
    """
    return _resolve_output_mode(value)[0]


def _as_info_channel(value: object) -> str:
    """只认 ``voice`` / ``bubble``（大小写、首尾空白宽容），其它回退 ``voice``。

    回退到 ``voice`` 而不是 ``bubble``：出声是玩家的既有体验，配置写错时宁可保持
    原样，也不要让插件毫无征兆地"哑掉"。
    """
    if isinstance(value, str):
        text = value.strip().lower()
        if text in INFO_CHANNELS:
            return text
    return DEFAULT_INFO_CHANNEL


def _as_hit_reaction_level(value: object) -> str:
    """只认 :data:`HIT_REACTION_LEVELS`（大小写、首尾空白宽容），其它回退 ``quiet``。

    回退到**更安静**的那一档：这个键存在的意义就是控制“挨打时喊多大声”，读不懂时
    不该反而喊得更响（和 ``team_events_enabled`` 的回退方向同一条理由）。
    """
    if isinstance(value, str):
        text = value.strip().lower()
        if text in HIT_REACTION_LEVELS:
            return text
    return DEFAULT_HIT_REACTION_LEVEL


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "on"}:
            return True
        if text in {"false", "0", "no", "off"}:
            return False
    return default


def _as_path(value: object) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _as_float(value: object, default: float, bounds: tuple[float, float]) -> float:
    """非数字 / NaN / 无穷 → 默认值；有限但越界 → 裁剪到边界。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return default
    else:
        return default
    if not math.isfinite(number):
        return default
    low, high = bounds
    return min(max(number, low), high)


def _as_int(value: object, default: int, bounds: tuple[int, int]) -> int:
    """整数配置项：非数字 / NaN / 无穷 → 默认值；有限但越界 → 裁剪到边界。

    浮点按**向零截断**（``40.9`` → 40）：这些键是"几句/几次"的计数而不是时长，
    截断比四舍五入更保守（宁可少说一句）。
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            return default
        number = int(value)
    elif isinstance(value, str):
        try:
            number = int(float(value.strip()))
        except (ValueError, OverflowError):
            return default
    else:
        return default
    low, high = bounds
    return min(max(number, low), high)
