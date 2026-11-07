"""要不要说话、说什么 —— 纯函数式仲裁器。

输入一帧（:class:`~.state_reader.Frame`）+ 当前时刻，输出一句要说的话或 ``None``。
不做 IO、不取时钟、不替换 ``{MASTER_NAME}``（占位符留给宿主在注入边界展开）。
**唯一的 IO 例外**是 v2.15 的决策日志：只往**注入进来的 logger** 写一行 INFO
（见下面「决策日志」一节），不读文件、不写文件、不取时钟，``debug_log=False`` 时连这行都没有。
**文本不在这个文件里**（v2.8）：所有台词都在 ``data/phrases.json``，由
:class:`~.phrases.PhraseBook` 读进来并按 ``event_id / tier`` 取句。本模块只决定
"这一帧说不说、说哪个事件的哪一档、占位符填什么"，以及占位符的**取值**（中文
数字、方位词这类"算出来的东西"）。话术表读不到 → 自动退化为内置默认表，不崩。

合规是硬约束：文本只允许来自白名单信息源（``me.*``、``team`` 四个计数、已发生
``events``、``result.*``），并且只读事件 payload 里白名单内的键。事件 payload
再花哨也进不了输出——这是"敌方字段绝不入话术"的实现层保证。

紧急度分三级（v2.12）：:data:`EVENT_LEVELS` 把每个事件 id 归进
``critical`` / ``warning`` / ``info`` 之一，:data:`LEVEL_PRIORITY` 给出三档在通道层的
数值（90 / 50 / 10，越大越急），:data:`PRIORITY` 只剩"同一档内谁先念"的作用域。
未登记的 id 一律按 ``warning``（:data:`DEFAULT_LEVEL`）处理，**不抛异常** —— 老版本
mod 发来个新事件 id 只该少说话，不该把整个插件打哑。档位判定要跨模块复用，请
``from .arbiter import EVENT_LEVELS, LEVEL_PRIORITY``，别在别处再抄一张表。

每帧判定顺序（v2.12）：**一帧最多一条**。有 ``critical`` 时按
``interrupt_enabled`` 决定"无视 ``min_gap_s`` 当场说"还是"排队不抢话"；没有才看
``warning``（受 ``min_gap_s``，被拦排队）；最后才轮到 ``info``（还要过
``idle_fill_s`` 空闲闸，被拦直接丢）。同帧跨档永远高档压低档。最后真的说出口的
那句会记进 :attr:`Arbiter.last_cue`（event_id / level / text / priority），
``observe()`` 的返回类型仍是 ``str | None``。

## 决策日志（v2.15 / P0）

**成功推送原来一个字都不打**，实机出事只能靠耳朵 —— 这是当时最大的观测盲区。
现在 :meth:`Arbiter.observe` 的每个决策点都留一行 INFO（``logger`` 由插件在
``startup()`` 里传进来 = 插件自己的 logger，落进 ``N.E.K.O_Plugin_neko_wot_*.log``）：

* ``arbiter: fresh=[you_hit,you_damaged] derived=[team_numbers]`` —— 这一帧到了什么
* ``arbiter: pick critical=None warning=you_hit info=None`` —— 每档选中谁
* ``drop <id> reason=<真闸门名>`` —— **每个被丢的候选一行**，原因只许写真实闸门
  （``team_events_off`` / ``type_cooldown(剩3.2s)`` / ``gap(剩0.8s)`` / ``queue_not_empty(n=2)`` /
  ``idle_fill(剩6.0s)`` / ``no_text`` / ``forbidden_text`` / ``repeated_text`` /
  ``death_already_spoken(deaths=1)`` / ``same_tier_lost_to_<胜者>`` /
  ``level_lost_to_<胜出档>`` / ``interrupt_off`` / ``quota_used`` / ``seen(seq=123)`` /
  ``frontline_team_behind`` / ``queue_full``）；真的进了队列的会在行尾加 `` -> enqueued``
* ``speak <id> level=<档> text="<原文>"`` / ``enqueue|expire|discard|clear_queue …``

**安静帧一个字都不打**（poll 每秒一帧；没有新候选且队列为空就直接返回，见
``observe()`` 的早退），所以正常一局是几十到一两百行，不是每秒几十行。
``debug_log=False`` 时整组关掉（配置层默认 true，见 ``settings.DEFAULT_DEBUG_LOG``）。
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict, deque
from datetime import datetime
from typing import Any, NamedTuple

from .history import Battle, History, last_results, vehicle_average_damage
from .phrases import DEFAULT_PHRASES, PHRASE_BOOK, PhraseBook
from .state_reader import STATUS_OK, Frame

LOGGER = logging.getLogger("neko_wot.arbiter")

# ------------------------------------------------------- 口播禁词规则（v2.7 B1/B4）
# **产品规则只写在这一处**：话术重写、用例矩阵、CI 闸全部从这里取。别在别处再抄
# 一份词表 —— 两份词表就是两个真相，改一处必漂。
#
# 为什么分两组（v2.7 拆表）：
# * ``SELF``  —— "我们自己这侧"的**规范说法**（我方单位 / 我方损失 / 我方剩余 /
#   我被击毁），**允许**说出口。拆表就是为了放行"我被击毁了""我们这边少了一个"
#   这类己方话术；旧版把"我方战损"和"敌方情报"混成一张表，正常己方话术也一起被禁。
# * ``ENEMY`` —— **敌方单位 / 敌方剩余数量 / 敌方处境 / 敌方毁灭**，外加历史遗留
#   的机械黑话（"装填"/"reload"，字面禁、与情报无关）。一律禁止：敌方的位置、
#   数量、状态、预测一个字都不许出现在口播里。
#
# 判定入口只有一个 :func:`preach_is_forbidden`：命中 ENEMY 组即判红，**同时命中
# 两组时也按 ENEMY 组从严判红**（前面有一句允许的己方说法，不能给整句洗白）。
FORBIDDEN_WORD_GROUPS: dict[str, tuple[str, ...]] = {
    "SELF": (
        # 我方单位 / 我方损失 / 我方剩余 / 我被击毁 的规范说法（允许）
        "我方",
        "我们这边",
        "我们少了",
        "我被击毁了",
        "我这台没了",
        "我数到击毁",
    ),
    "ENEMY": (
        # 敌方单位
        "敌人",
        "敌方",
        "敌军",
        "敌车",
        "对面",
        # 敌方位置 / 情报 / 预测
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
        # 机械黑话（历史遗留：字面禁，跟敌方情报无关）
        "装填",
        "reload",
    ),
}


def preach_word_hits(text: str) -> frozenset[str]:
    """这句话命中的禁词分组（可能同时命中 SELF 和 ENEMY，也可能是空集）。

    v1.30 的**唯一一次放宽**：先按 :func:`_mask_enemy_count_phrases` 把
    「敌方/敌人/对面 + 数量」整段挖掉再比词表 —— 双方人数在记分板/小地图上是
    直接可见的（产品口径），说"敌方还剩 3 个"不算泄密。
    ⛔ 吐出来的只是那一小段：`敌方` 后面**没有数字**、或者夹带了别的敌方词
    （位置/方位/装填…）照旧判红。
    """
    lowered = _mask_enemy_count_phrases(str(text or "")).lower()
    return frozenset(
        group
        for group, words in FORBIDDEN_WORD_GROUPS.items()
        if any(word.lower() in lowered for word in words)
    )


def preach_is_forbidden(text: str) -> bool:
    """口播合规判定（唯一入口）。命中 ENEMY 组 → 红；SELF 组单独命中 → 不算问题。

    两组同时命中时**按 ENEMY 组从严判红**：一句话里既有"我被击毁了"这种允许的
    己方说法、又夹带了敌方情报，整句作废。
    """
    return "ENEMY" in preach_word_hits(text)


# ------------------------------------------- 人数句豁免（v1.30，唯一一次放宽）
# 维护者原话：「对敌方队伍剩余数量的播报禁词可以适当放宽，因为双方人数是可以直接看到的」。
#
# 放开的**只有一种句式**：「敌方/敌人/对面 + 数量」，例如
#   「敌方还剩 3 个」「敌人只剩两台」「对面 1 个」
# ⛔ 边界（一个字都没松，词表里那些词一个都没删）：
#   * 位置 / 坐标 / 方位 / 来袭 / 预测 / 建议 / 透视 / 瞄准 / 弱点 / 可见性 / 透明度 /
#     标记 / 箭头 / 装填 / reload —— 照旧一律判红；
#   * 「敌方」后面**没有数量**（「敌方在左边」）→ 不豁免 → 红；
#   * 血量百分比照旧禁（名单见人数矩阵用例：6 档文案不许出现「血」「%」）；
#   * 豁免靠"先把整段挖掉再比词表"实现，所以 `敌方还剩 3 个装填` 只会挖掉前半段，
#     剩下的「装填」照旧命中 —— 不会给"夹带"洗白。
ENEMY_COUNT_WORDS = ("敌方", "敌人", "对面")
# 数字：阿拉伯 / 全角 / 中文（含「两」与「几」，口语里很正常）。
_COUNT_NUMERAL = r"[0-9０-９零〇一二三四五六七八九十百两几]"
_ENEMY_COUNT_PHRASE = re.compile(
    r"(?:%s)\s*(?:还剩|只剩|就剩|剩下|剩|只有|还有|共|是)?\s*%s+(?:\s*(?:个|台|辆|人))?"
    % ("|".join(ENEMY_COUNT_WORDS), _COUNT_NUMERAL)
)
# 挖掉的那一段用这个占位符顶替（不参与词表比对，也不改原句长度语义）。
COUNT_MASK = "⟨人数⟩"


def _mask_enemy_count_phrases(text: str) -> str:
    """把「敌方/敌人/对面 + 数量」整段替换成 :data:`COUNT_MASK`。

    只挖**连词带数字的整段** —— 单独一个「敌方」不会被挖掉，所以
    「敌方在左边」照样命中 ENEMY 组。这是"放开人数"和"放开敌方情报"的分界线。
    """
    return _ENEMY_COUNT_PHRASE.sub(COUNT_MASK, text)


def enemy_count_phrase_allowed(text: str) -> bool:
    """这句话里**存在**可豁免的人数句式吗（用例/排障用，不参与判定）。"""
    return bool(_ENEMY_COUNT_PHRASE.search(str(text or "")))


# --------------------------------------------------- 三级紧急度表（v2.12）
# 一帧里可能同时挤着"我被击毁了"和"装填完成"，只比一个裸数字说不出**为什么**谁赢，
# 也没法回答"这条事件到底算不算急"。所以紧急度拆成两层，两层各有唯一真相：
#   * **档位**（本节的 ``EVENT_LEVELS``）—— 跨档的硬边界，critical > warning > info；
#   * **档内排序**（下面的 ``PRIORITY``）—— 同档里谁先念，数字作用域**只剩档内**。
# 于是 ``PRIORITY`` 里的 50 和 10 不再代表"一个比另一个急"，只代表"同档内的次序"；
# ⚠️ 别拿两条不同档的 ``PRIORITY`` 比大小，那样的比较没有定义。
#
# **为什么要单开一张表**：仲裁层（``_pick`` / ``_queued_ready``）和通道层
# （``push_message(priority=...)``）说的是同一件事 —— 这句话多急。判定散在两处
# 就会漂：迟早一句"结算"被一句"装填完成"顶掉，而且现场复现不出来（事件只活一帧）。
#
# 分档依据是"玩家下一秒要不要改动作"，不是"事件多不多"：
#   * ``critical`` —— 局面已经变了：不改动作就吃亏，或这局已经结束
#     （阵亡 / 结算 / 着火 / 血线告急）。一帧只该说一条，别的不许抢。
#   * ``warning`` —— 战况与战术信息：战果、受伤、被压制、时间/人数/落后这类提醒。
#     该说，但晚一帧说依然成立。
#   * ``info`` —— 机制回执与开场白（装填好了 / 火灭了 / 开打了）。
#     有别的可念就先念别的，垫底。
#
# **未登记的 id 默认 ``warning``**（见 ``DEFAULT_LEVEL``）：老版本 mod 发来一个
# 新事件 id 时，宁可当"普通战况"说出来，也不要抛异常 —— 少一句话是产品问题，
# 抛异常把整帧吞掉是整个插件哑火。加新事件 id 请顺手补进 ``EVENT_LEVELS``。
LEVEL_CRITICAL = "critical"
LEVEL_WARNING = "warning"
LEVEL_INFO = "info"
# 由急到缓（顺序有意义：判"更高档"时按索引比，别自己再排一份）。
LEVELS: tuple[str, ...] = (LEVEL_CRITICAL, LEVEL_WARNING, LEVEL_INFO)
DEFAULT_LEVEL = LEVEL_WARNING

# event_id → 档位。**全部 24 个 id 一个不落**（mod 真发的 + 派生层合成的 + 开场白）。
# 按 critical / warning / info 分组排列；组内顺序无意义，只为好读，别据此推断急缓
# ——组内急缓看 ``PRIORITY``。
#
# v2.12 补登最后 4 个 id（``fire_out`` / ``module_damaged`` / ``crew_dead`` /
# ``ammo_low``）：它们在 ``data/phrases.json`` 里本来就有话术（实测 23 个事件 id），
# ``TYPE_COOLDOWN_S`` 里也有冷却，只是 建表时漏了档位 —— 漏档等于它们只能吃
# ``DEFAULT_LEVEL``（warning），"火灭了"这种回执就永远轮不到 info 的空闲档。
EVENT_LEVELS: dict[str, str] = {
    # ---------------------------------------------------------------- critical
    "you_died": LEVEL_CRITICAL,      # 我这台没了（**每次阵亡一条**：重生模式一局可能多条）
    "battle_end": LEVEL_CRITICAL,    # 结算：事件只活一帧，被压过就永远没有第二次
    "on_fire": LEVEL_CRITICAL,       # 着火：不处理就烧完
    "hp_critical": LEVEL_CRITICAL,   # 派生层：血线告急（每局一次额度，走 _refund）
    # ----------------------------------------------------------------- warning
    "you_respawned": LEVEL_WARNING,  # 重生回来了，可以再打
    "you_killed": LEVEL_WARNING,     # 自己的战果
    "enemy_killed": LEVEL_WARNING,   # 敌方少人（含队友战果）
    "you_assisted": LEVEL_WARNING,   # 协助计数
    "ally_killed": LEVEL_WARNING,    # 我方少人
    "you_damaged": LEVEL_WARNING,    # 掉血（同一炮常和 you_hit 同帧）
    "you_hit": LEVEL_WARNING,        # 受击方位："往哪边躲"是战术，不是局面
    "suppressed": LEVEL_WARNING,     # 被夹击（30 秒冷却 + 每局 3 次）
    "enemy_low": LEVEL_WARNING,      # 收尾提醒
    "time_low": LEVEL_WARNING,       # 时间告急
    "team_behind": LEVEL_WARNING,    # 落后提醒
    "mode_switched": LEVEL_WARNING,  # 玩家自己按的机制切换（不是战况）
    # v2.12 补登：mod 真发、phrases.json 里有话术的三个战损/弹药提醒。
    # 都不是"下一秒不改动作就吃亏"，但都比机制回执急 —— 所以是 warning，不抢 critical。
    "module_damaged": LEVEL_WARNING,  # 部件被打坏（15 秒冷却，同一部位不会连着报）
    "crew_dead": LEVEL_WARNING,       # 车组减员（15 秒冷却）
    "ammo_low": LEVEL_WARNING,        # 弹药见底（20 秒冷却，够低频）
    # -------------------------------------------------------------------- info
    "reload_done": LEVEL_INFO,       # 最高频，垫底：有别的可念就先念别的
    "fire_cleared": LEVEL_INFO,      # 松口气
    "battle_start": LEVEL_INFO,      # 开场白（同帧有阵亡时让位给 critical）
    # v2.12 补登：火灭了是"松口气"的机制回执，和 fire_cleared 同档。
    "fire_out": LEVEL_INFO,          # 火全灭（mod 事件，与派生 fire_cleared 同一件事）
    # v1.30 新增：人数差播报（派生层合成）。它是**报点**，不是战果也不是危险，
    # 所以按 info 档排 —— 真有击杀/阵亡/结算时一个字都不抢。
    "team_numbers": LEVEL_INFO,
    # v2.18 / C4 新增：二阶静默的陪伴句（派生层合成）。它**不依赖任何战况**，也不评价
    # 战局 —— 所以只能垫底：有装填/火灭/人数这类真回执时一律让位。
    "idle_company": LEVEL_INFO,
}

# 档位 → 通道层数值。**只为"跨档可比"而存在**：宿主侧 ``push_message(priority=...)``
# 只认一个数字，所以三档必须定死成一个数（90 / 50 / 10，越大越急）。
# 三档之间故意留大间隔：将来要插"介于两档之间"的东西不用挪动已有数值。
# ⚠️ 通道层**只许**从这里取（``LEVEL_PRIORITY[EVENT_LEVELS[event_id]]``，未登记 id
# 用 ``DEFAULT_LEVEL``），别在别处硬编码 90/50/10 —— 两处各写一份就是两个真相。
LEVEL_PRIORITY: dict[str, int] = {
    LEVEL_CRITICAL: 90,
    LEVEL_WARNING: 50,
    LEVEL_INFO: 10,
}


class Cue(NamedTuple):
    """最近一次真的说出口的那句的只读快照（``Arbiter.last_cue``）。

    ``observe()`` 的返回类型**不许动**（既有用例几十条直接 ``== "某句话"`` 断言），
    所以"这句属于哪个事件、哪一档、通道层该给多少 priority"只能挂在实例属性上往外传。
    ``priority`` 取的是**档位数值**（``LEVEL_PRIORITY[level]``，90/50/10），不是档内
    排序数字 —— 通道层只认跨档可比的三个数。
    """

    event_id: str      # 事件 id（含派生层合成的 id）
    level: str         # critical / warning / info（未登记 id 落到 DEFAULT_LEVEL）
    text: str          # 说出口的原文（占位符还没展开）
    priority: int      # LEVEL_PRIORITY[level]

# 档内排序：数字越大越优先。**作用域只有同一档**（v2.12 起跨档由 EVENT_LEVELS 决定，
# 不同档的两条不再比这个数）。同帧多事件时先看档、档内再看这个数，其余视为已消费。
PRIORITY: dict[str, int] = {
    "you_died": 50,
    "on_fire": 40,
    "you_respawned": 35,
    # ↓ 以下 6 个 id 由**派生层**从状态字段合成（见 DERIVED_EVENT_IDS），不是 mod 事件。
    # 血线告急：得马上改打法，比任何战果都急；但不能压过阵亡/着火。
    "hp_critical": 33,
    "you_killed": 30,
    # 敌方少人（含队友战果）：低于 you_killed，真有自己击杀时先念自己的。
    "enemy_killed": 28,
    "you_assisted": 25,
    # 受击方向（v2.4，mod 事件 you_hit）：同一炮会**同时**送来 ``you_hit`` 和
    # ``you_damaged``，两条挤在同一帧里，所以"念哪个"是真问题。方向比"还剩多少血"
    # 可操作（往哪边躲立刻有用），排在掉血之上；但仍然远低于战果/阵亡/着火。
    "you_hit": 13,
    # 战斗模式切换（v2.4，mod 事件 mode_switched）：玩家自己按的，不是战况，
    # 只是机制提示；压在"被盯上了"（16）之下、"火灭/装填"之上。
    "mode_switched": 15,
    # 我方少人：坏消息排在自己战果之后，别盖掉高光。
    "ally_killed": 18,
    # 被压制是提醒不是战果；同帧有结算（20）时让结算先说。
    "suppressed": 16,
    # 火灭属于"松口气"，排在结算/战果之后。
    "fire_cleared": 14,
    # 装填完成最高频（每十几秒就可能一次），垫底：有别的可念就先念别的。
    "reload_done": 12,
    # 人数差播报（v1.30）：info 档里也排最后 —— 它是"报点"，不是战果。
    "team_numbers": 9,
    # 二阶静默（v2.18 / C4）：info 档里也排最后（比人数报点还低），它是最不值钱的一句，
    # 任何真回执（装填/火灭/开场白）在同帧都能把它顶掉。
    "idle_company": 8,
    # 收尾提醒（v2.4 派生层）：**压在战况计数之下**——对面掉到 2 台的那一帧通常
    # 就是"干掉一个"的那一帧，击杀确认必须先说（那是玩家刚干的事）。收尾这句
    # 不排队、也不在检测时扣额度，所以下一帧条件还在，它会自己补上。
    "enemy_low": 17,
    # 时间告急（v2.4 派生层）：一局一次的时钟闸，比"我方少人"重要（打不完就白打），
    # 但**必须低于 battle_end(20)** —— 结算走 GAP_BYPASS，一旦被更高的 id 压过，
    # 那一局就再也没有结算播报了（事件只活一帧）。同理也不抢"自己击毁"。
    "time_low": 19,
    # 落后提醒（v2.4 派生层）：坏消息，但比"我方少了一个"这种即时边沿更宏观、
    # 更新更慢；同样压在 battle_end 之下。
    "team_behind": 17,
    "battle_end": 20,
}
DEFAULT_PRIORITY = 10

# 能突破全局最小间隔的事件：一局最多一次的"锚点"。
# 事件只存活一帧（写端下一帧就把 events 清空），被间隔闸拦下就**永远没有
# 第二次机会**；而结算/阵亡又恰恰最可能紧跟在上一句话之后（打完最后一炮就结束）。
# 每局各只一次，绕开间隔不会造成刷屏。
#
# v2.12：**"绕开 min_gap"这件事本身已经交给 critical 档 + ``interrupt_enabled``
# 开关**（不再按 id 逐个开特权），这个常量保留下来干两件更窄的事：
#   ① 它标记"这一帧的叙事翻篇" —— critical 直发时**只有这两个 id 会清空延迟队列**
#      （排队的战况话术作废、额度还回去）；别的 critical（着火 / 血线告急）只插队，
#      不砸掉玩家已经该听到的那句；
#   ② 它是"事件只活一帧、绝不能丢"的锚点清单 —— 关掉打断时它俩照样**进队列**
#      （``_enqueue(force=True)`` 无视 ``DEFERRABLE``），只是不再插队。
# 既有用例断言这个常量非空且恰好是 critical 档的子集，别删、别改成员。
GAP_BYPASS = frozenset({"battle_end", "you_died"})

# 被全局间隔拦下时"排队等下一次机会"的事件（稀有高光）。
# 直接丢的话玩家会觉得"我明明击毁了，它没反应"；排队只会把这句话推迟几秒。
# ``you_damaged`` 也排队，但语义不同：同类排队只保留**最新**一条（血量话术要说
# "现在剩多少"，报一条过期的血线毫无意义 —— 实机：93% 之后直接 44%）。
# ``you_assisted``（v2.2）同理：连续协助只念最新那句"攒到多少"，中间过程没人关心。
# v2.3 的派生战况（``enemy_killed`` / ``ally_killed``）也排队：它们是**边沿**事件，
# 状态一变就再也不会出现第二次，直接丢就成了"队友打掉两个它没反应"。
# ``suppressed`` 也是边沿（30 秒冷却 + 每局 3 次），丢了就真没了；它的文本不带
# 数字，晚几秒说依然成立。
# v2.4 把派生的 ``hp_critical`` 从 GAP_BYPASS 挪到这里（间隔闸从 8 秒降到 2 秒，
# 排队+时效已经能保证它在两秒内说出来）。走队列的好处是它**可以被排队覆盖**：
# 真要没说出来，额度会还回去（见 ``_refund``），不会白白烧掉"每局一次"。
DEFERRABLE = frozenset(
    {
        "you_killed",
        "you_damaged",
        "you_assisted",
        "enemy_killed",
        "ally_killed",
        "suppressed",
        "hp_critical",
    }
)

# 排队时"只保留最新"的同类：新的一条直接顶掉旧的。
# 战况计数只有最新那一条还有意义，排队的旧值一过期就是错的（和血线同理）。
LATEST_WINS = frozenset({"you_damaged", "you_assisted", "enemy_killed", "ally_killed"})

# "每局一次"的额度（v2.4）：这些 id 一旦说出来就算用掉，**没说出来要还回去**。
# 实测的教训是反过来的：额度在"触发"那一刻就烧掉，被排队覆盖/过期就
# 白白没有了 —— "血不多了"整局再也不会响。
QUOTA_EVENT_IDS = frozenset({"hp_critical"})

# 排队时效（秒）：被间隔拦下的句子最多等这么久，过了就扔（旧消息没价值）。
# 默认间隔 2 秒时正好是 4 秒；间隔调大时按 ``min_gap + TTL/2`` 放宽，
# 否则"间隔 8 秒 + 时效 4 秒"等于排了也白排。
PENDING_TTL_S = 4.0
# 队列上限：同帧/连击的瞬时堆积最多三条，再多就是噪声。
PENDING_MAX = 3

# 同类事件冷却（秒）。全局最小间隔另由构造参数 ``min_gap_s`` 控制。
#
# ⚠️ **0.0 = 不做时间防抖**（``_type_ready`` 恒真），只给"身份唯一、绝不许丢"的边沿事件用。
# ``you_died`` 就是这一类（v2.14 / 实测）：
#   * 阵亡是**边沿**，一局死几次就得说几次（巴比伦那类重生模式一局死 2~3 次）；
#   * 旧的 10 秒时间闸是 ``_pick`` 里**唯一**还能把 critical 静默丢掉的出口，而它按
#     **时间**判重、不按**身份**：第二次真阵亡只要落在 10 秒内就被吞掉，那一帧顺势
#     去补说队列里那句旧的（"协助伤害攒到…"）—— 玩家听到旧话、阵亡没播。
#     实测复现见 ``tests/test_death_never_dropped.py``（8.5 秒处的第二死）；
#   * 防复读改由**身份**负责：``_pick`` 用写端 payload 的 ``deaths``（"第几次阵亡"）
#     去重，同一次阵亡重复投递仍然只说一遍。时间闸做不到这件事（它既会吞真阵亡，
#     也拦不住换了 seq 的重复投递）。
TYPE_COOLDOWN_S: dict[str, float] = {
    "battle_start": 20.0,
    "you_killed": 4.0,
    "you_died": 0.0,
    "you_respawned": 5.0,
    "on_fire": 15.0,
    "fire_out": 10.0,
    "you_damaged": 12.0,
    "you_assisted": 15.0,
    # 受击方向（v2.4，mod 事件 you_hit）：一炮一条，挨打密集时会连着来。8 秒是
    # "同一片方向连着挨两下只说一次"的量；再长会漏掉"换了方向"这种真信息。
    "you_hit": 8.0,
    # 战斗模式切换（v2.4，mod 事件 mode_switched）：写端只报**人工**切换（自动液压
    # 不发，见 协议 §5 v1.20），本来就低频；5 秒只是防抖。
    "mode_switched": 20.0,
    "module_damaged": 15.0,
    "crew_dead": 15.0,
    "ammo_low": 20.0,
    "battle_end": 10.0,
}

# 前线模式（``mode_kind == "frontline"``，mod v1.38 的新取值）的队伍事件冷却。
#
# 前线是 30v30 + 每人可复活多次（预备队最多 2 台、每 5 分钟 +1），一局轻松 20+ 分钟；
# 击杀/损失几乎连续发生。沿用标准局的 4 秒冷却 = 每 4 秒念一句"干掉一个 / 少了一个"，
# 实机就是刷屏。25 秒约等于"每半分钟最多一句"：战况的明显转折仍然听得见，但不再
# 追着每一台车念。
#
# ⚠️ 只覆盖 ``TEAM_EVENT_IDS``（``enemy_killed`` / ``ally_killed``）；标准局、未知
# ``mode_kind``、老 mod（没有这个键）一律走 ``DERIVED_COOLDOWN_S`` 的原值 ——
# standard 的行为一个字节都不许变（见 ``Arbiter._effective_cooldown``）。
FRONTLINE_TEAM_COOLDOWN_S = 25.0

DEFAULT_COOLDOWN_S = 10.0

# 事件 payload 里允许被读的键。加键必须先想清楚"屏幕上看得见吗"。
# ``health``/``max_health``/``delta`` 都是自己车的血量（屏幕左下角血条上的数字），
# v1.9 起写端会带上；读到就直接说具体数字，比"还剩 96% 的血"有用得多。
# ``n``/``left`` 只由派生层自己合成（自家战况计数，屏幕右上角就看得见），mod 从不发。
PAYLOAD_ALLOWED_KEYS = frozenset(
    {
        "map",
        "vehicle",
        "ratio_after",
        "total",
        "health",
        "max_health",
        "delta",
        "n",
        "left",
        # 受击粗方位（v2.4，``you_hit``）：屏幕上本来就有那个受击指示箭头，
        # 八格方位是它显示的同一件事。**绝不**放行 attacker/attacker_vehicle/he。
        "yaw_deg",
        # ---- v1.30：``battle_end`` 的 15 键战果（协议 §5.3）----
        # 全是**结算面板上本人看得见**的数：胜负、击杀、本人伤害、战斗秒数、双方存活。
        # ``src``（来源表）是给真机标定用的对象，不是标量 → 被下面"只收标量"的
        # 规则挡掉，读端一个字都不念它。
        "result",
        "kills",
        "dmg_dealt_total",
        "battle_seconds",
        "ally_alive",
        "ally_total",
        "hits_taken",
        "blocked_count",
        "shots_fired",
        # ---- v1.30：``you_killed`` 的累计伤害（协议 §5.2）----
        "dmg_acc",
        # ---- v1.30：人数差档位（派生事件，双方人数在记分板上可见）----
        # 只放行"档位名 + 两边的整数人数 + 差值"；敌方位置/血量/车型一个都不在此列。
        "tier",
        "ally",
        "enemy",
        "diff",
        # ---- v2.20 / P3：断带/断轮 + 机制分档（协议 §5.1b / §5.4）----
        # ``crits`` 是官方回调的原始位掩码（自己车受到的模块伤害），
        # ``crits_count`` 是同一 (attacker, 设备集合) 的 3s burst 序号。
        # ``crits_devices`` 是 list[str]（设备名），单独走下面的"列表键"白名单。
        "crits",
        "crits_count",
        # ``mode_switched.mech``：六值，由上游按固定枚举声明（协议 §5.4），不是从车名猜的。
        "mech",
    }
)

# 白名单里的**列表**键：只收"非空字符串列表"（例如 ``crits_devices``）。
# 其它对象/嵌套 dict 一律丢弃，防止把整个 payload 塞进话术。
PAYLOAD_ALLOWED_LIST_KEYS = frozenset({"crits_devices"})

# 白名单里的**布尔**键单独一张表：默认 ``_allowed_payload`` 会把 bool 一律丢掉
# （防止 True 被当成数字 1），但这两个键本身语义就是布尔，读端要的就是真假。
PAYLOAD_ALLOWED_BOOL_KEYS = frozenset(
    {
        # 跳弹/未击穿（有受击、无掉血）：屏幕上会闪"未击穿"，念一句不算泄密。
        "blocked",
        # 战斗模式切换后的档位（``mode_switched`` 的派生布尔，见 协议 §5）。
        "activated",
        # v1.30：``you_killed`` 的两个"死因"布尔（协议 §5.2）。
        # ⚠️ 本轮写端**恒给 null**（没有确凿来源），所以这里放行的是"键"，不是"结论"：
        # 读到 null 就走普通档，绝不把"不知道"当成 false 来说。
        "was_fire",
        "was_ammo",
    }
)
_MAX_TEXT_FIELD = 40

# 去重表有界，防止一局打太久内存慢慢涨。
MAX_SEEN = 512

# ----------------------------------------------------------------- 派生层（v2.3）
# mod 只发**原始事实**（开打/掉血/击毁/阵亡…）；下面这九个语义事件由插件从
# ``Frame`` 的**状态字段**推导出来，喂进同一套 _pick/_gap/_type/_commit 流程。
#
# 合成事件**不参与 event.seq 去重**：它们带自己的自增 seq（只当渲染种子），并且
# 从不进入 ``frame.events``，所以永远不会污染 ``_max_event_seq`` 的重启判定
# —— 否则一条合成 seq 就能把"数据端重启"判成永真，插件立刻哑巴。
# 边沿去重靠各 detector 自己的基线（见 ``_derive``），另有一张有界
# ``_derived_seen`` 兜"同一帧重复调用"。
DERIVED_EVENT_IDS = frozenset(
    {
        "reload_done",
        "hp_critical",
        "enemy_killed",
        "ally_killed",
        "fire_cleared",
        "suppressed",
        # ---- v2.4：整局层面的三个提醒（对面快清完了 / 时间不多了 / 我们落后了）----
        "enemy_low",
        "time_low",
        "team_behind",
        # ---- v1.30：人数差档位播报（6 档 + 波次降级）----
        "team_numbers",
        # ---- v2.18 / C4：二阶静默的陪伴句（不依赖任何战况，只报"我在"）----
        "idle_company",
    }
)

# 派生事件自己的同类冷却。**故意不并进 TYPE_COOLDOWN_S**：那张表是"mod 真发的
# 事件 id"清单，``test_every_template_is_free_of_forbidden_words`` 会逐条渲染它，
# 而派生事件只靠状态变化产生，拿一个没有状态的帧去渲染必然为空。
DERIVED_COOLDOWN_S: dict[str, float] = {
    "reload_done": 10.0,
    "hp_critical": 10.0,
    # 队伍事件（enemy_killed / ally_killed）：标准局 4.0 秒；前线模式（30v30 复活局）
    # 由 ``FRONTLINE_TEAM_COOLDOWN_S`` 在运行时覆盖成 25 秒 —— 见 ``_effective_cooldown``。
    "enemy_killed": 4.0,
    "ally_killed": 4.0,
    "fire_cleared": 10.0,
    "suppressed": 30.0,
    # ---- v2.4：整局层面三个提醒的冷却 ----
    # 这三个**真正的闸是每局上限**（1/1/2 次），冷却只做防抖，所以给得很小：
    # 给成 30 秒的话，"让位给战况计数后重试"会被自己的冷却挡住，一局就少说一次。
    "enemy_low": 5.0,
    "time_low": 5.0,
    "team_behind": 5.0,
    # 人数差播报（v1.30）：**不用**同类冷却 —— 防抖只有一处
    # （``_numbers_should_speak``：3 秒 + 关键翻转放行）。两层闸叠在一起会出现
    # "翻转被 3 秒同类冷却吃掉"的死角：防抖说该立刻说，同类冷却说还差 1 秒。
    "team_numbers": 0.0,
    # 二阶静默（v2.18 / C4）：一局只一次（靠 ``_deep_used`` 锁），**不用**时间冷却 ——
    # 时间冷却会和"等 idle_deep_s 才允许开口"叠成两层闸，把唯一一次机会再削掉。
    "idle_company": 0.0,
}

# 判定阈值。全是"屏幕上看得见的事实"，不含任何敌方信息。
RELOAD_READY = 0.99          # 装填条满（写端 0..1，末位抖动很常见，别卡 1.0）
HP_CRITICAL = 0.25           # 血线告急
HP_REARM = 0.35              # 回到这以上才允许再报一次"血不多了"
SUPPRESS_WINDOW_S = 6.0      # 被压制的观察窗
SUPPRESS_MIN_HITS = 2        # 窗内至少挨两下
SUPPRESS_SPREAD_DEG = 60.0   # 两个来弹方向夹角超过这个才叫"被夹"
SUPPRESS_MIN_DROP = 0.1      # 且窗内至少掉 10% 血
SUPPRESS_MAX_PER_BATTLE = 3  # 每局最多念三次"被盯上了"
HIT_SAMPLES = 8              # 受击样本环大小（固定，4Hz 里不做任何增长式分配）
MAX_DERIVED_SEEN = 64        # 合成事件去重表（只兜重复帧）
_EPS = 1e-9                  # 0.9 - 0.8 在 float 里是 0.09999999999999998

# 决策日志默认开关（v2.15 / P0）。**默认开**：出事时才有得复盘；配置层
# ``settings.DEFAULT_DEBUG_LOG`` 与它同值，``[neko_wot] debug_log = false`` 可关掉。
DEFAULT_DEBUG_LOG = True
# ``seen`` 只对这几个 id 逐条打：写端 ``events`` 是 16 条**滑动窗口**，每 250ms 把同一批
# 事件重发一遍 —— 非锚点逐条打会在一秒内刷几十行，把真信号淹掉。锚点（阵亡/结算）被
# 当成"已见过"才是真事故（一句必须说的话被去重吃掉），所以只有它们值得一行。
SEEN_LOG_IDS = frozenset({"you_died", "battle_end"})

# ============================================================ v2.18 / P1 陪伴四件套
#
# README「陪伴能力」里的 C1/C2/C3/C4/C8。**不新增"播什么"的敌方情报**，
# 只在既有事件上补"说得更像人"：结算复盘分档 / 本地战绩记忆 / 连败关怀 / 二阶静默 /
# 全局话术预算。所有开关的默认值都在 `settings.py`（更安静的那一档），这里只放常量与阈值。

# ---- C1 复盘分档（判据只许用结算数据 + 本机历史）----
RECAP_CARRY_KILLS = 3          # win 且 kills ≥ 3 → 夸战果
RECAP_SHORT_BATTLE_S = 180     # lose 且 battle_seconds < 180 → 短局安慰
RECAP_AVG_MIN_BATTLES = 3      # "高于本车历史均伤"至少要有 3 局同车样本

# ---- C3 连败 / 连胜关怀 ----
STREAK_LEN = 3                 # 最近 3 局结果相同才触发
STREAK_COOLDOWN_S = 30 * 60.0  # 同类（连败/连胜各自）30 分钟内只说一次

# ---- C8 全局话术预算 ----
# 滑动窗长度。配置层不再单开一个键（`settings.SPEECH_BURST_WINDOW_S` 是同一把尺子）：
# 窗和上限拆成两个键就会出现"窗 5 秒 + 上限 3 句"这种自相矛盾的配置。
SPEECH_BURST_WINDOW_S = 30.0

# ============================================================ v2.19 / P2 C5~C7
#
# README「陪伴能力」里的 C5/C6/C7。仍然**零协议变更**：
# 只消费既有的 `you_damaged.delta / me.max_health`、`you_hit.blocked`、
# `team.ally_alive / enemy_alive`、`battle_start` + 本机 C2 账本。

# ---- C5 受击反应分档 ----
# 分档依据**只有一个**：这一下的面值掉血 / 自己车的最大血量。
# 阈值是产品口径（路线图 §3 C5），别在别处再写一份。
HIT_REACTION_HEAVY_RATIO = 0.25     # quiet（默认）：≥25% → 重话术
HIT_REACTION_CRITICAL_RATIO = 0.50  # quiet（默认）：≥50% → critical 档
# full 档只是“更敏感”（阈值下移），密度上限与 quiet 完全一样。
HIT_REACTION_FULL_HEAVY_RATIO = 0.15
HIT_REACTION_FULL_CRITICAL_RATIO = 0.35
# 受这两条事件驱动（`you_hit.blocked` 也算一次受击，但不算掉血面值）。
HIT_REACTION_EVENT_IDS = frozenset({"you_damaged", "you_hit"})
# 同类冷却 15 秒 / 单局 8 句：C5 **自己再算一层**，不指望 C8 的 40 句兜底。
HIT_REACTION_COOLDOWN_S = 15.0
HIT_REACTION_MAX_PER_BATTLE = 8
# 连续挨打合并：15 秒内 ≥3 次（任意档）→ 合并成一句，不逐发喊。
HIT_REACTION_MERGE_WINDOW_S = 15.0
HIT_REACTION_MERGE_HITS = 3
# 受击时间环的大小：最多留 16 个时间戳（4Hz 下 15 秒窗最多 60 帧，但真正的
# “挨打”远少于帧数；16 足够判断“≥3 次”且不随战斗时长增长）。
HIT_REACTION_BURST_SAMPLES = 16

# ---- C6 劣势/残局安抚细分 ----
# 差值 = 敌方存活 - 我方存活。三档每档每局最多一次；旧单句模式仍用
# ``TEAM_BEHIND_MAX_PER_BATTLE``（2 次），分档模式只在“档位继续变差”时开口，
# 所以最多 3 句且不会重复同一档。
TEAM_BEHIND_TIER_MILD = "mild"        # 差 1~2：轻描淡写
TEAM_BEHIND_TIER_BIG = "big"          # 差 ≥3：沉一点，但不劝战术
TEAM_BEHIND_TIER_ENDGAME = "endgame"  # 我方极少且对方明显多：只陪、不评价
TEAM_BEHIND_TIERS: tuple[str, ...] = (
    TEAM_BEHIND_TIER_MILD,
    TEAM_BEHIND_TIER_BIG,
    TEAM_BEHIND_TIER_ENDGAME,
)
TEAM_BEHIND_TIER_MAX_PER_BATTLE = len(TEAM_BEHIND_TIERS)
TEAM_BEHIND_ENDGAME_ALLY = 2          # 我方存活 ≤2 才算“残局”
TEAM_BEHIND_ENDGAME_DIFF = 3          # 且敌方比我方多 ≥3

# ---- C7 开场人格化 ----
# 同一台车至少要有这么多局历史，才配说“本车均伤”（样本太少等于编）。
BATTLE_START_VEHICLE_MIN_BATTLES = 3

# ============================================================ v2.20 / P3 断带 + 机制
#
# 只消费协议 §5.1b / §5.4 已经存在的字段：``you_hit.crits_devices`` /
# ``crits_count`` / ``crits`` / ``blocked``、``me.mech``、``mode_switched.mech``。
# ⛔ 不造 ``canMakeDualShot`` / 过热温度 / ``hull_yaw`` 这类清单外字段。

# ---- 断带 / 断轮双层播报 ----
# 设备名 = ``VEHICLE_DEVICE_TYPE_NAMES`` 里的 track / wheel（§5.1b 原文，小写）。
# 位掩码：criticalDevices = mask & 4095；destroyedDevices = (mask >> 12) & 4095。
TRACK_DEVICE_BITS: dict[str, int] = {"track": 1 << 4, "wheel": 1 << 9}
TRACK_DESTROYED_SHIFT = 12
TRACK_BREAK_WINDOW_S = 15.0
TRACK_BREAK_MAX_PER_WINDOW = 2
TRACK_BREAK_TIERS: tuple[str, ...] = (
    "track_critical",
    "track_destroyed",
    "wheel_critical",
    "wheel_destroyed",
)
TRACK_BREAK_TIER_SET = frozenset(TRACK_BREAK_TIERS)

# ---- 机制话术分档 ----
# 机制话术（开场 + 切换）单独计数：单局 ≤6 句。
MECH_MAX_LINES_PER_BATTLE = 6
# ``me.mech`` 的 9 个固定 key → 开场档位。unknown / [] 不在表里 = 不说。
MECH_OPENING_TIERS: dict[str, str] = {
    "twin_gun": "mech_twin_gun",
    "twin_gun_switch": "mech_twin_gun_switch",
    "rocket": "mech_rocket",
    "hydro": "mech_hydro",
    "susp_auto": "mech_susp_auto",
    "wheeled": "mech_wheeled",
    "drum": "mech_drum",
    "autoreload": "mech_autoreload",
    "auto_cannon": "mech_auto_cannon",
}
# 多机制车只出一句：按这个固定顺序挑第一个认识的 key（不猜、不拼接）。
MECH_OPENING_ORDER: tuple[str, ...] = (
    "twin_gun",
    "twin_gun_switch",
    "auto_cannon",
    "drum",
    "autoreload",
    "wheeled",
    "rocket",
    "hydro",
    "susp_auto",
)
# ``mode_switched.mech`` 的五个已知值 → (切入档, 切回档)。
# ⛔ twinGun / dualGun 说的是“切换模式”这件事，话术里不许出现“齐射”
# （协议没有 canMakeDualShot）。
MECH_SWITCH_TIERS: dict[str, tuple[str, str]] = {
    "twinGun": ("twin_gun_on", "twin_gun_off"),
    "dualGun": ("dual_gun_on", "dual_gun_off"),
    "siege": ("siege_on", "siege_off"),
    "autocannon": ("autocannon_on", "autocannon_off"),
    "rocket": ("rocket_on", "rocket_off"),
}

# ---- v2.4：整局层面三个提醒的阈值（全部只读公开计数/公开时间）----
ENEMY_LOW_AT = 2             # team.enemy_alive ≤ 2 → "对面快清完了"
TEAM_BEHIND_AT = -3          # 我方存活 - 敌方存活 ≤ -3 → "我们这边落后了"
TEAM_BEHIND_MAX_PER_BATTLE = 2   # 落后提醒每局最多两次（回落再成立算一次）
TIME_LOW_S = 60.0            # 剩余时间 ≤ 60 秒 → "时间不多了"
ARENA_PERIOD_BATTLE = 2      # 协议 §2.2：0=等待 1=准备 2=战斗 3=结算

# ---- v1.30：人数差话术矩阵（6 档 + 波次降级）----
# 档位名字**就是 phrases.json 里的 tier 名**，改一个等于改话术，两处一起改。
NUMBERS_BIG_ADV = "big_advantage"          # 敌方 0，或 我方-敌方 ≥ 3
NUMBERS_SMALL_ADV = "small_advantage"      # 差 +1 ~ +2
NUMBERS_EVEN = "even"                      # 差 0
NUMBERS_SMALL_DIS = "small_disadvantage"   # 差 -1 ~ -2
NUMBERS_BIG_DIS = "big_disadvantage"       # 差 ≤ -3
NUMBERS_WAVE = "wave"                      # 波次局：敌人会一直补，只说我方
NUMBERS_UNKNOWN = "unknown"                # 判不出来：只说我方，不报敌方数字
NUMBERS_TIERS: tuple[str, ...] = (
    NUMBERS_BIG_ADV,
    NUMBERS_SMALL_ADV,
    NUMBERS_EVEN,
    NUMBERS_SMALL_DIS,
    NUMBERS_BIG_DIS,
    NUMBERS_WAVE,
    NUMBERS_UNKNOWN,
)
NUMBERS_BIG_DIFF = 3         # 差 ≥3 / ≤-3 算"大优/大劣"
# 档位切换防抖（秒）：同一侧的档位来回跳（+3 ↔ +2）时至少隔这么久才再说一遍。
# **只压重复/同侧抖动**：从优势翻到劣势（或翻成"人数看不到"）是局面反转，
# 必须立刻说 —— 见 ``_numbers_should_speak``。
NUMBERS_MIN_GAP_S = 3.0

# 队伍事件（队友击杀 / 队友阵亡）：默认不播（settings.team_events_enabled）。
# 这两个 id 由派生层从 ``team`` 计数合成 —— 是"别人打掉的车"，不是游戏里的本人战果。
TEAM_EVENT_IDS = frozenset({"enemy_killed", "ally_killed"})

# 剩余时间：**只认一个键** —— ``arena.time_left``（协议 §2.2，mod v1.27 起
# 写端显式给整数剩余秒）。**故意不做别名表**（``timeLeft`` / ``remaining`` /
# ``seconds_left`` 之类）：写端只写这一个名字，多认一个键就多一份"把别的字段误当
# 剩余秒"的风险，而读错的倒计时比没有更糟。
#
# ⛔ 红线（产品红线）：**读不到 ``time_left`` 就一条都不报**（缺键 ≠ 剩余 0，≠ 时间到）；
# 绝不用 ``period`` / ``periodLength`` / ``phase`` 反推剩余时间（推算要知道开局时刻，
# 一局从加载就开表、BR/重生模式还会中途跳相位 → 必然偏）。
#
# 同帧的 ``overtime``（加时标记，mod v1.27）只当**闸**用、不参与取值：
# ``overtime is True`` → 这一帧不报，并且把本局的锁合上（见 ``_derive`` ⑧）。
# ``time_probe`` 是写端排障用的探针，读端**不读**（判定不许依赖探针）。
ARENA_TIME_LEFT_KEY = "time_left"
ARENA_OVERTIME_KEY = "overtime"

# 受击粗方位（八格，每格 45°，索引 0 = 正前方）。**只有八个词**：
# 距离、角度、攻击者名字、弹种一律不说（合规红线）。
HIT_SECTORS = ("正前方", "右前方", "右边", "右后方", "正后方", "左后方", "左边", "左前方")
# 参考系修正（度）。写端 v1.17 起给的是它自己采到的航向角 ``hitDirYaw``，但它的
# **零点/正负号还没标定**（DATA_MAP §9.7「标定完成前，读端不许自己解释正负号」）。
# 实测标定出来后**只改这一个数**：整体反了填 180，左右镜像填 90/-90。
HIT_SECTOR_OFFSET_DEG = 0.0


class Arbiter:
    """有状态的仲裁器：去重、冷却、选优先级都在这里。"""

    def __init__(
        self,
        *,
        min_gap_s: float = 2.0,
        max_seen: int = MAX_SEEN,
        type_cooldown_s: dict[str, float] | None = None,
        pending_ttl_s: float | None = None,
        pending_max: int = PENDING_MAX,
        phrases: PhraseBook | None = None,
        interrupt_enabled: bool = True,
        idle_fill_s: float = 0.0,
        team_events_enabled: bool = False,
        debug_log: bool = DEFAULT_DEBUG_LOG,
        logger: Any | None = None,
        history: History | None = None,
        recap_enabled: bool = True,
        idle_deep_s: float = 0.0,
        speech_budget_per_battle: int = 0,
        speech_burst_max: int = 0,
        hit_reaction_enabled: bool = False,
        hit_reaction_level: str = "quiet",
        team_behind_tiers_enabled: bool = False,
        battle_start_persona_enabled: bool = False,
        track_break_enabled: bool = False,
        mech_opening_enabled: bool = False,
        mech_switch_enabled: bool = False,
    ) -> None:
        self.min_gap_s = max(0.0, float(min_gap_s))
        # ---- v2.18 / P1 陪伴四件套 ----
        # ``history`` 注入才启用账本（None = 均伤档与连胜连败关怀全关，一个字节不碰盘）。
        # 构造默认全部取"关/不限"：这一层不替配置层做决定（同 ``idle_fill_s`` 的理由），
        # 生产默认值由 ``settings.DEFAULTS`` + ``startup()`` 接线。
        self.history = history
        self.recap_enabled = bool(recap_enabled)
        self.idle_deep_s = max(0.0, float(idle_deep_s))
        self.speech_budget_per_battle = max(0, int(speech_budget_per_battle))
        self.speech_burst_max = max(0, int(speech_burst_max))
        # ---- v2.19 / P2：C5 / C6 / C7 ----
        # 构造层默认**关**（和 ``idle_fill_s`` / ``recap_enabled`` 同一条规矩）：
        # 既有用例构造 ``Arbiter(min_gap_s=...)`` 时期望 v2.18 的观感；生产默认值由
        # ``settings.DEFAULTS`` + ``startup()`` 接线（三条默认都是“开”）。
        self.hit_reaction_enabled = bool(hit_reaction_enabled)
        self.hit_reaction_level = (
            hit_reaction_level if hit_reaction_level in ("quiet", "full") else "quiet"
        )
        self.team_behind_tiers_enabled = bool(team_behind_tiers_enabled)
        self.battle_start_persona_enabled = bool(battle_start_persona_enabled)
        # ---- v2.20 / P3：断带双层 + 机制分档（构造层同样默认关，生产默认开）----
        self.track_break_enabled = bool(track_break_enabled)
        self.mech_opening_enabled = bool(mech_opening_enabled)
        self.mech_switch_enabled = bool(mech_switch_enabled)
        # C5 局内状态：受击时间环 / 说出口几句 / 上次开口时刻 / 合并锁。
        self._hit_burst_times: deque[float] = deque(maxlen=HIT_REACTION_BURST_SAMPLES)
        self._hit_lines_used = 0
        self._last_hit_line_at: float | None = None
        self._hit_merged_at: float | None = None
        # C6 局内状态：当前档位 + 已经说过的档位（每档一局一次）。
        self._behind_tier: str | None = None
        self._behind_tiers_used: set[str] = set()
        # C7：``battle_start`` 那一帧按账本算好的开场上下文（空 dict = 没历史）。
        self._start_ctx: dict[str, Any] = {}
        # v2.20：断带话术的 15 秒窗口（最多 2 句）+ 机制话术单局计数（开场/切换共用）。
        self._track_line_times: deque[float] = deque(maxlen=TRACK_BREAK_MAX_PER_WINDOW)
        self._mech_lines_used = 0
        # 渲染时记下"这句属于哪个档"，``_commit`` 用它在**说出口**时记额度。
        self._rendered_tier: dict[str, str] = {}
        # 本帧时刻（``_pick`` 写、``_render`` 读）。**故意不给 ``_render`` 加参数**：
        # 既有用例 monkeypatch 了 ``_render(event, frame)`` 的形状，改签名会误伤闸门测试。
        self._now = 0.0
        # 局内计数（换局归零，见 ``_clear_derived``）；``_streak_last_at`` 跨局保留。
        self._budget_used = 0
        self._budget_dropped = 0
        self._speak_times: deque[float] = deque()
        self._deep_used = False
        self._deep_try_at = 0.0
        self._recap_ctx: dict[str, Any] = {}
        self._care_used = False
        self._streak_last_at: dict[str, float] = {}
        self._history_rows: list[Battle] = []
        self._history_written_key: tuple[str, int | None] | None = None
        # ---- 决策日志（v2.15 / P0）----
        # ``logger`` 默认是模块自己的 logger（用例/独立调用够用）；生产路径由
        # ``__init__.py::startup()`` 传入**插件自己的** logger —— 只有它落进
        # ``N.E.K.O_Plugin_neko_wot_*.log``（模块 logger 是另一棵树，不一定有 handler）。
        self.debug_log = bool(debug_log)
        self.logger: Any = LOGGER if logger is None else logger
        # 打断开关（v2.12）：critical 档是否**无视 min_gap_s 立刻说**。
        # 开（默认）= "我被击毁了"这种局面已经变了的话，不等间隔、当场说；
        # 关 = 连 critical 也排队（不抢话），玩家只想听"慢慢说"的时候用。
        # 注意它**不解除同类冷却**：on_fire 的 15 秒照旧生效，否则着火会变成复读机。
        self.interrupt_enabled = bool(interrupt_enabled)
        # info 档的空闲填空闸（秒）：距上一句话不足这么久，info 一律不补位（直接丢）。
        #
        # ⚠️ 默认值是 ``0.0``（= 不传就是"不管闲"），**不是生产默认的 10 秒**：
        #    · 产品默认值（10 秒）属于**配置层**：``settings.DEFAULTS["idle_fill_s"]``，
        # 由 ``__init__.py::startup()`` 读出来传进来（v2.12 已做）；
        #    · 仲裁器这一层几十条既有用例构造 ``Arbiter(min_gap_s=...)`` 后立刻断言
        #      ``reload_done`` / ``battle_start`` 这类 info 档的话说出来 —— 默认 10 秒
        #      会把它们集体打红，那不是"用例写错了"，是这一层不该替配置层做决定。
        self.idle_fill_s = max(0.0, float(idle_fill_s))
        # 队伍事件开关（v1.30）：False = 队友击杀/队友阵亡**一律不出声**
        # （连候选都不进）。True = 按 info 档说 —— 不抢 critical/warning 的话。
        # 语义差别只在"说不说 + 哪一档"，判据（``team`` 计数边沿）一个字没动。
        self.team_events_enabled = bool(team_events_enabled)
        # 排队时效：默认间隔 2 秒 → 4 秒；间隔调大时跟着放宽半个间隔。
        self.pending_ttl_s = max(
            PENDING_TTL_S,
            self.min_gap_s + PENDING_TTL_S / 2,
        ) if pending_ttl_s is None else max(0.0, float(pending_ttl_s))
        self._pending_max = max(1, int(pending_max))
        self._max_seen = max(1, int(max_seen))
        # 话术表（v2.8）：默认是模块级单例（``phrases.PHRASE_BOOK``，支持热更新）；
        # 用例可以注入临时表来验证"换表/分档/热更新"。仲裁器自己**不读盘**。
        self._phrases = PHRASE_BOOK if phrases is None else phrases
        # 局内"每个 id 说出口几次"：多句变体按它轮换（见 ``_say``），换局归零。
        self._said_counts: dict[str, int] = {}
        base_cooldown = dict(TYPE_COOLDOWN_S if type_cooldown_s is None else type_cooldown_s)
        for derived_id, seconds in DERIVED_COOLDOWN_S.items():
            base_cooldown.setdefault(derived_id, seconds)
        self._cooldown = base_cooldown
        self._arena: str | None = None
        # 本帧的计数语义（顶层 ``mode_kind``，协议 §4.2）：同类冷却要看局模式
        # （frontline 的 30v30 复活局把队伍事件放宽到 25 秒）。每帧由 ``observe`` /
        # ``prime`` 更新 —— ``_type_ready`` 在 ``_pick`` 和队列补说两处都要用，拿不到 frame。
        # ``None`` = 老 mod 没有这个键 / 还没读到第一帧 → 一律按非 frontline 处理。
        self._mode_kind: str | None = None
        # 去重身份 = ``event.seq``（v2.2）。**不再带 arena_id**：
        # ① 实测 seq 在一次 mod 加载期间一路递增、跨局不清零（22:21 局 11 起、22:51 局续到 34）；
        # ② 带 arena 的键在**换局那一刻**会把上一局的残留事件当成"新事件"重放 ——
        # 实测：新一局刚开打，插件对着残留的 `you_died` 喊了
        #    "被击毁了…缓一下"，而真正的 `battle_start`（"开打了！"）一句都没播
        #    （同帧取优先级最高的一条，you_died 50 把 battle_start 10 挤掉了）。
        self._seen: OrderedDict[int, set[str]] = OrderedDict()
        # 见过的最大 event.seq：用来区分"换局"（seq 继续涨）和"数据端重启"（seq 回退）。
        self._max_seq_seen: int | None = None
        self._last_spoken_at: float | None = None
        self._last_type_at: dict[str, float] = {}
        # 最近一次**真的说出口**的那句的只读快照（v2.12）。通道层要
        # event_id / level / priority，而 ``observe()`` 的返回类型不许动（既有用例
        # 几十条直接比字符串），所以挂在这里。只有 ``_announce`` 会更新它 ——
        # 直发、插队、队列补说三条出口都走那一个落点。
        # 初始 ``None``（还没说过话）；**跨局不清**：它描述的是"最近一次说出口的"。
        self.last_cue: Cue | None = None
        self._kills = 0
        # 被全局间隔压住、等下一次机会再说的那几句：(event_id, 已渲染文本, 过期时刻)。
        # v2.4 从"一个格子"换成有界队列：同一秒连出三条时只丢一条太亏，而一帧只能
        # 说一句；排队的句子按优先级补说，过了时效就扔（旧消息没价值）。
        self._queue: deque[tuple[str, str, float]] = deque()
        # 上一句说出去的血量话术，用来避免"一千八百"连念三遍
        self._last_damage_text: str | None = None
        # ---- 派生层（v2.3）：边沿比较用的基线 + 固定大小的受击样本环 ----
        self._prev_reload: float | None = None
        self._prev_health: float | None = None
        self._prev_fires: int | None = None
        self._prev_enemy_alive: int | None = None
        self._prev_ally_alive: int | None = None
        self._hp_armed = True
        # (取样的 now, hitDirYaw | None, 挨这一下**之前**的血量)
        self._hit_samples: deque[tuple[float, float | None, float | None]] = deque(maxlen=HIT_SAMPLES)
        self._suppressed_count = 0
        # ---- 整局层面提醒的额度（v2.4）：按"说出口"计数，换局归零 ----
        self._enemy_low_count = 0
        self._time_low_count = 0
        # 时间告急的"本局已经用掉"锁（v2.10 / B5）：**只会开、不会关**（除换局）。
        # 为什么不能只靠上面那个计数：加时那一帧我们故意不说话，那时 count 还是 0，
        # 可这一局的时钟已经走到头了 —— 必须有一把独立的锁把它合上，否则
        # "说了一次 → 归零 → 加时 → 又回到 60"会复活第二次（验收）。
        self._time_low_locked = False
        self._behind_count = 0
        # 阵亡的**身份**（v2.14）：写端 payload 的 ``deaths`` = "这一局第几次阵亡"。
        # ``TYPE_COOLDOWN_S["you_died"]`` 现在是 0.0（不吃时间闸），防复读全靠这个数：
        # 同一次阵亡重复投递（抖动/多路投递）还是同一个 ``deaths`` → 只说一遍；
        # 真·下一次阵亡 ``deaths`` 会涨 → 一秒之后也照样说。换局归零（见 ``_clear_derived``）。
        # ``None`` = 本局还没说过 / 写端没给这个字段（那就按事件本身算一次，宁可多说也不丢）。
        # 只用来做身份：不进话术，所以不必进 ``PAYLOAD_ALLOWED_KEYS``。
        self._last_death_count: int | None = None
        # 落后提醒是**边沿**：None = 还没有基线（换局后第一帧、启动 prime 之前）。
        self._behind_active: bool | None = None
        # 人数差档位（v1.30）：上一档 + 上次**报出去**的时刻。
        # ``None`` = 本局还没报过（换局/启动首帧只建基线，不出声）。
        self._numbers_tier: str | None = None
        self._numbers_at: float | None = None
        # 合成事件的自增 seq（只当渲染种子）与"同一帧只出一次"的有界兜底表
        self._derived_seq = 0
        self._derived_seen: OrderedDict[str, None] = OrderedDict()

    def observe(self, frame: Frame | None, now: float) -> str | None:
        """返回这一帧该说的话；不说是常态，返回 ``None``。

        判定顺序（v2.12，五步）。**一帧最多产出一条**，
        任何路径都不会出现"说完一句接着补一句"：

        ① 收本帧候选（mod 事件 + 派生层）→ 过同类冷却 → 过每局额度；
        ② 有 ``critical`` 候选：``interrupt_enabled`` 开 → 无视 ``min_gap_s`` 直接说
           （同类冷却照旧生效）；关 → 进延迟队列等下一次机会，**这一帧不抢话**；
        ③ 否则取 ``warning`` 里 ``PRIORITY`` 最高的一条，受 ``min_gap_s`` 管；
           被拦则按 ``DEFERRABLE`` + ``pending_ttl_s`` 排队；
        ④ 否则 ``info`` 只在 ``now - last_spoken_at >= idle_fill_s`` 时补位
           （``min_gap_s`` 也要过；被拦就**直接丢**，不排队）；
        ⑤ 上面都没说话时，才轮到补说队列里那句。

        同帧跨档时**永远高档压低档**：critical 压 warning 压 info，哪怕低档的
        ``PRIORITY`` 数字更高（``PRIORITY`` 的作用域只剩档内）。
        """
        if frame is None or frame.status != STATUS_OK:
            return None
        # 每一帧都刷新局模式：同类冷却（``_effective_cooldown``）要在 ``_pick`` 和
        # 队列补说两处按它判，那两处都拿不到 frame。判定本身只走 ``_is_frontline``。
        self._mode_kind = frame.mode_kind

        # ⓪ 数据端重启兜底：最大 seq 变回小值 = 计数器归零/换了 state 文件。
        #    这时去重表里的旧 seq 全部作废（不清就会把新一局的 1..N 当成"见过"），
        #    但这一帧本身是历史残留 → **静默 prime**：全部标记已见、一句都不说。
        if self._restarted(frame.events):
            self._log("silent_prime reason=seq_regress n=%d", len(frame.events))
            self._restart_quietly(frame.arena_id or "", frame.events)
            return None

        arena = frame.arena_id or ""
        if arena != self._arena:
            self._reset(arena)

        fresh = self._consume(frame.events)
        # 受击样本必须在 _derive 之前采：此刻 ``_prev_health`` 还是"这一下之前"的血量。
        self._absorb_hits(fresh, now)
        derived = self._derive(frame, now)
        fresh_ids = {str(event.get("id")) for event in fresh}
        fresh_ids.update(str(event.get("id")) for event in derived)
        all_fresh = [*fresh, *derived]

        # ⓪' 战绩账本（v2.18~）：**只在 battle_start / battle_end 这两帧碰盘**。
        #    放在 _pick 之前：结算分档要用"本局之前"的历史均伤，而渲染发生在 _pick 里。
        self._note_history(all_fresh, frame, now)

        # 安静帧早退（v2.15 / P0）：没有新候选、队列也空 —— 这一帧**一个字都不打**。
        # poll 每秒一帧，不这么拦的话日志会被空转帧淹掉，"一局几十到一两百行"就不可能。
        if not all_fresh and not self._queue:
            return None

        self._log("arbiter: fresh=%s derived=%s", _brief_ids(fresh), _brief_ids(derived))

        # ① 收候选：``_pick`` 已经过完同类冷却 + 禁词闸 + you_damaged 复读抑制，
        #    并按档位分好（每档只留自己 PRIORITY 最高的一条）。
        picks = self._pick(all_fresh, frame, now)
        # 话术预算（v2.18 / C8）：**在挑赢家之前**把超预算的档位摘掉，否则日志会出现
        # "pick critical=you_died" 却一句都没说（`_announce` 还会再拦一次，那是队列那条路）。
        for level in LEVELS:
            if level in picks and not self._budget_allows(level, now):
                self._budget_dropped += 1
                self._log_drop(picks[level][2], self._budget_reason(level, now))
                picks.pop(level)
        self._log(
            "arbiter: pick critical=%s warning=%s info=%s",
            _pick_id(picks, LEVEL_CRITICAL),
            _pick_id(picks, LEVEL_WARNING),
            _pick_id(picks, LEVEL_INFO),
        )
        # 一帧只出口一条：没抢到这一帧的档位，逐个说明是被谁压下的（真实档位名）。
        winner = next((level for level in LEVELS if level in picks), None)
        for level in LEVELS:
            if level == winner or level not in picks:
                continue
            self._log_drop(picks[level][2], f"level_lost_to_{winner}")

        # 队列维护先做，和这一帧选出谁无关：过期的扔、同类"只留最新"的作废。
        self._purge_expired(now)
        # 同类排队只留最新：这一帧带了更新的血线/战况（哪怕它自己不值得说，
        # 比如血已归零要让位给 you_died），排队里那条一律作废。
        for fresh_id in tuple(LATEST_WINS.intersection(fresh_ids)):
            self._discard(fresh_id)

        # ② critical：打断开关决定"当场说"还是"排队等"。
        critical = picks.get(LEVEL_CRITICAL)
        if critical is not None:
            _, text, event_id = critical
            if not self.interrupt_enabled:
                # 关掉打断：连 critical 也不抢话。它**必须进队列**（不能按
                # DEFERRABLE 过滤）—— 这几条事件只活一帧，丢了就永远没有第二次。
                self._log_drop(event_id, "interrupt_off", tail=" -> enqueued")
                self._enqueue(event_id, text, now, force=True)
                return None
            if event_id in GAP_BYPASS:
                # 生死/结算把叙事翻篇：还在排队的那几句一起作废（没说出来 → 额度还回去）。
                self._clear_queue(reason="gap_bypass")
            return self._announce(event_id, now, text)

        # ③ warning：受全局最小间隔管，被拦就排队。
        warning = picks.get(LEVEL_WARNING)
        if warning is not None:
            _, text, event_id = warning
            if self._gap_ready(now) and not self._queue:
                return self._announce(event_id, now, text)
            # 两个原因都走排队：间隔没到，或者队列里还有话在前头（按优先级补说，
            # 不看新旧 —— "后来的更重要就把前面那句挤掉"正是丢播报的老毛病）。
            # 非 DEFERRABLE 的照旧直接丢：它们本来就会反复出现。
            self._log_drop(event_id, self._block_reason(now), tail=" -> enqueued" if event_id in DEFERRABLE else "")
            self._enqueue(event_id, text, now)
        elif picks.get(LEVEL_INFO) is not None:
            # ④ info：只在"很久没说过话"时补位，两个闸都要过。
            _, text, event_id = picks[LEVEL_INFO]
            if self._idle_ready(now) and self._gap_ready(now) and not self._queue:
                return self._announce(event_id, now, text)
            # 被拦下的 info **直接丢**（v2.12）：晚几秒说"装填好了"没有价值，
            # 而且 info 一进队列就会把 warning 的位置顶掉（队列上限只有 3 条）。
            self._log_drop(event_id, self._info_block_reason(now))

        # ⑤ 队列补说：间隔一过就先补说排队里的那句（这一帧有新句子排在后面时也一样）。
        if self._queue and self._gap_ready(now):
            index = self._queued_ready(now)
            if index is not None:
                return self._take(index, now)
        return None

    def prime(self, frame: Frame | None) -> None:
        """启动基线：把盘上已有的事件登记成"已消费"，一句话都不说。

        ``events`` 是最近 N 条的滑动窗口而不是增量，插件重启后去重表是空的，
        所以第一次读到的 16 条全是历史。这里只登记不渲染、也不计击杀数，
        免得开局第一句就变成"这一局第 7 个"。
        """
        if frame is None or frame.status != STATUS_OK:
            return
        self._mode_kind = frame.mode_kind
        arena = frame.arena_id or ""
        if arena != self._arena:
            self._reset(arena)
        # 启动基线：以这一帧的 seq 为起点（prime 之前 _max_seq_seen 是 None，不会误判重启）
        self._max_seq_seen = None
        self._track_seq(frame.events)
        self._consume(frame.events, count_kills=False)
        self._kills = 0
        self._clear_queue(reason="prime")
        # 派生层也要对齐基线：盘上这一帧的血量/装填/队伍计数是"我启动之前的状态"，
        # 拿它做边沿比较等于复述历史（和上面事件同一套道理）。
        self._prime_derived(frame)

    def _pick(
        self, fresh: list[dict[str, Any]], frame: Frame, now: float
    ) -> dict[str, tuple[int, str, str]]:
        """同帧多事件按档取最高：每档只留自己 ``PRIORITY`` 最高的那条。

        返回 ``{档位: (档内 PRIORITY, 文本, event_id)}``；这一帧没有候选的档位
        **不出现在字典里**（调用方一律 ``picks.get(档位)``）。

        **冷却中的 id 直接跳过**（v2.4）：同一炮会同时送来 ``you_hit``（受击方向）
        和 ``you_damaged``（血线），两条必然同帧。如果只比优先级，那么方向句的
        8 秒冷却里，被它压住的掉血句就再也说不出来 —— 优先级高但**说不了**的那条
        把说得了的挤掉，是这个架构最容易踩的饿死坑。

        v2.12 把"全局取一个最大数"改成"按档取"：档位由 ``EVENT_LEVELS``
        决定，``PRIORITY`` 只在**同档内**比 —— 跨档比数字这种错配从此不可能发生。
        出口闸一条都没丢：同类冷却、:func:`preach_is_forbidden`、
        ``you_damaged`` 复读抑制，以及 v2.14 给阵亡补的**身份闸**（``deaths``）。
        """
        best: dict[str, tuple[int, str, str]] = {}
        self._now = now
        for event in fresh:
            event_id = str(event.get("id"))
            # 队伍事件出口闸（v1.30）：关着的时候连候选都不算 ——
            # 派生层那边已经不发它们，这里是第二道（老 mod 直接塞同 id 事件也挡）。
            if event_id in TEAM_EVENT_IDS and not self.team_events_enabled:
                self._log_drop(event_id, "team_events_off")
                continue
            # 前线模式的"落后"语义不成立（30v30 复活局里人数差会被预备队补平）：
            # 派生层已经不生成这条；这里是第二道 —— 老 mod 若直接塞同 id 事件也挡。
            if event_id == "team_behind" and _is_frontline(frame.mode_kind):
                self._log_drop(event_id, "frontline_team_behind")
                continue
            # v2.19 / C5：受击话术自己再算一层闸（15 秒同类冷却 + 单局 8 句）。
            # 合并句只**绕过冷却**（合并本身就是“别再逐发喊”），8 句硬上限照样管它。
            # v2.20：断带/断轮是独立事件链（自己的 15s≤2 与 burst 闸），不吃 C5 的
            # 冷却/8 句额度 —— 否则一次断带可能被 15 秒前的普通掉血吞掉。
            track_tier = (
                self._track_tier_for(_allowed_payload(event.get("payload")), now)
                if event_id == "you_hit" and self.track_break_enabled
                else None
            )
            if self.hit_reaction_enabled and event_id in HIT_REACTION_EVENT_IDS and track_tier is None:
                blocked = self._hit_reaction_block_reason(now)
                if blocked is not None:
                    self._log_drop(event_id, blocked)
                    continue
            if track_tier is None and not self._type_ready(event_id, now):
                self._log_drop(event_id, f"type_cooldown(剩{self._cooldown_left(event_id, now):.1f}s)")
                continue
            text = self._render(event, frame)
            if text is None:
                # 渲染不出来（血已归零 / 字数字段缺失 / 表里没这一档）：宁可不说，不许编。
                self._rendered_tier.pop(event_id, None)
                self._log_drop(event_id, "no_text")
                continue
            # 合规硬闸（v2.7）：渲染出来的句子必须过 :func:`preach_is_forbidden`。
            # 这是**所有**话术的唯一出口（排队补说的文本也是先在这里过闸的），
            # 所以档位上"命中 ENEMY 组 → 一个字都不说"，而不是说了再遮。
            if preach_is_forbidden(text):
                self._rendered_tier.pop(event_id, None)
                self._log_drop(event_id, "forbidden_text")
                continue
            # 阵亡的**身份**闸（v2.14）：``you_died`` 不吃同类冷却（``TYPE_COOLDOWN_S``
            # 里是 0.0），防复读改看写端 payload 的 ``deaths`` = "这一局第几次阵亡"。
            # 同一次阵亡被重复投递（抖动 / 多路投递）还是同一个数 → 只说一遍；
            # 真·下一次阵亡 ``deaths`` 会涨 → 哪怕只隔一秒也照样说出来。
            # 这是"时间闸"换"身份闸"：时间既会吞掉真阵亡，也拦不住换了 seq 的重复投递。
            # 写端没给这个字段（老 mod / 脏帧）→ 没有身份可比，按事件本身算一次
            # （``event.seq`` 已由 ``_consume`` 精确去重）—— 宁可多说，不许无声丢弃。
            if event_id == "you_died":
                payload_obj = event.get("payload")
                deaths = _as_count(payload_obj.get("deaths")) if isinstance(payload_obj, dict) else None
                if deaths is not None:
                    if deaths == self._last_death_count:
                        self._log_drop(event_id, f"death_already_spoken(deaths={deaths})")
                        continue
                    self._last_death_count = deaths
            # 同一句话刚说过就别复读：血量取整后 1834/1819/1804 都会念成"还有
            # 一千八百血"，连念三遍等于卡带（回放抓到）。v2.12 挪进
            # 候选过滤：被抑制的那条不该占着名额，让同帧的次优候选顶上来。
            if event_id == "you_damaged" and text == self._last_damage_text:
                self._rendered_tier.pop(event_id, None)
                self._log_drop(event_id, "repeated_text")
                continue
            level = self._level_of(event_id)
            rank = PRIORITY.get(event_id, DEFAULT_PRIORITY)
            current = best.get(level)
            if current is None:
                best[level] = (rank, text, event_id)
            elif rank > current[0]:
                self._rendered_tier.pop(current[2], None)
                self._log_drop(current[2], f"same_tier_lost_to_{event_id}")
                best[level] = (rank, text, event_id)
            else:
                self._rendered_tier.pop(event_id, None)
                self._log_drop(event_id, f"same_tier_lost_to_{current[2]}")
        return best

    def _announce(self, event_id: str, now: float, text: str) -> str | None:
        """**所有出口的唯一落点**：直发 / 插队 / 队列补说都从这里出去。

        做三件事：过话术预算（v2.18 / C8）→ 记账（``_commit``：间隔、同类冷却、
        额度、变体轮换）→ 更新只读快照 :attr:`last_cue`。
        ⚠️ 别在别的分支里直接 ``_commit`` 再 ``return`` —— 那条路径会漏更新
        ``last_cue``，通道层就拿不到 level/priority。
        超预算时返回 ``None``（**丢弃、不排队**：排队必在关键时刻爆发）。
        """
        level = self._level_of(event_id)
        if not self._budget_allows(level, now):
            self._budget_dropped += 1
            self._log_drop(event_id, self._budget_reason(level, now))
            return None
        self._commit(event_id, now, text)
        self._budget_record(now)
        self._log('speak %s level=%s text="%s"', event_id, level, text)
        self.last_cue = Cue(
            event_id=event_id,
            level=level,
            text=text,
            priority=LEVEL_PRIORITY[level],
        )
        return text

    def _level_of(self, event_id: str) -> str:
        """事件 id → 档位（**唯一的档位查询口**，含队伍事件的运行时降档）。

        * 未登记的 id → ``DEFAULT_LEVEL``（warning），不抛异常；
        * 队伍事件（``enemy_killed`` / ``ally_killed``）在开关打开时**强制 info 档**
          （v1.30）：它们是"别人打掉的车"，只配垫底报一句，绝不占
          warning/critical 的位置。开关关着时它们根本到不了这里（``_pick`` 已挡）。
        """
        if event_id in TEAM_EVENT_IDS and self.team_events_enabled:
            return LEVEL_INFO
        return EVENT_LEVELS.get(event_id, DEFAULT_LEVEL)

    # ------------------------------------------------------------------ 内部
    # ------------------------------------------------- 决策日志（v2.15 / P0）
    def _log(self, template: str, *args: Any) -> None:
        """一行决策日志（INFO）。``debug_log=False`` 时整条不打。

        日志**永远不许**把播报弄挂：logger 是宿主注入的对象，签名可能变、
        也可能已经关掉了底层 handler —— 这里吞掉异常，宁可少一行日志。
        """
        if not self.debug_log:
            return
        try:
            self.logger.info(template, *args)
        except Exception:  # pragma: no cover - 只有坏掉的 logger 才会走到
            pass

    def _log_drop(self, event_id: str, reason: str, *, tail: str = "") -> None:
        """``drop <id> reason=<真闸门名>``。``reason`` 只许写真实闸门名，不许写"未知"。"""
        self._log("drop %s reason=%s%s", event_id, reason, tail)

    def _reset(self, arena: str) -> None:
        """换局：只重置**局内话术状态**。

        ⚠️ **故意不清 `_seen`**（v2.2）：换局那一刻 `events` 窗口里还留着上一局的
        11 条（含残留的 `you_died`），清空去重表 = 把它们当"新事件"重放，于是
        新一局第一句就是"被击毁了"。`event.seq` 跨局单调，残留事件本来就已经在表里，
        不动它就是最正确的做法。
        """
        self._arena = arena
        self._last_type_at.clear()
        self._last_spoken_at = None
        self._kills = 0
        self._clear_queue(reason="arena_reset")
        self._last_damage_text = None
        # 派生层基线必须清：上一局的"敌方还剩 3 个"拿来比这一局，会当场喊
        # "一口气干掉 12 个"。清空后第一帧只建基线、不出声。
        self._clear_derived()

    def _restarted(self, events: tuple[dict[str, Any], ...]) -> bool:
        """这一帧像不像"数据端重启"（最大 seq 比见过的最大值还小）。

        只会发生在这两种情形：mod 进程重开（`_event_seq` 从 0 起）、或插件被指到
        另一个 state 文件。换局**不会**触发 —— 实测 seq 跨局一路递增（11→34）。
        """
        max_seq = _max_event_seq(events)
        if max_seq is None:
            return False
        if self._max_seq_seen is not None and max_seq < self._max_seq_seen:
            self._max_seq_seen = max_seq      # 新序列的新起点，别一直判重启
            return True
        self._max_seq_seen = max_seq if self._max_seq_seen is None else max(
            self._max_seq_seen, max_seq
        )
        return False

    def _track_seq(self, events: tuple[dict[str, Any], ...]) -> None:
        """只更新"见过最大 seq"，不判重启（prime 专用）。"""
        max_seq = _max_event_seq(events)
        if max_seq is None:
            return
        self._max_seq_seen = max_seq if self._max_seq_seen is None else max(
            self._max_seq_seen, max_seq
        )

    def _restart_quietly(self, arena: str, events: tuple[dict[str, Any], ...]) -> None:
        """数据端重启后的第一帧：清空已见 + 静默 prime（一句都不说）。

        为什么不直接说点什么：这一帧的事件全是"重启前就发生的"，播报等于复述历史
        （和插件启动时 `prime()` 是同一个道理）。重启后真正的新事件下一帧才出现。
        """
        self._seen.clear()
        self._reset(arena)
        self._consume(events, count_kills=False)
        self._kills = 0
        self._clear_queue(reason="restart")

    def _consume(
        self,
        events: tuple[dict[str, Any], ...],
        *,
        count_kills: bool = True,
    ) -> list[dict[str, Any]]:
        fresh: list[dict[str, Any]] = []
        for event in events:
            event_id = event.get("id")
            if not isinstance(event_id, str) or not event_id:
                continue
            seq = event.get("seq")
            if isinstance(seq, bool) or not isinstance(seq, int):
                key = None            # 没有可用身份：不参与去重（照旧放行）
            else:
                key = seq
                if key in self._seen:
                    # 写端 ``events`` 是 16 条**滑动窗口**：同一批事件每 250ms 重新出现一次。
                    # 非锚点逐条打会一秒刷几十行（把真信号淹掉），只有锚点被当成"见过"
                    # 才是真事故 —— 那意味着一条必须说的话被去重吃掉了。
                    # v2.16（实测）：锚点**也只打一行** —— 结算/观战帧里 seq
                    # 不再增长，同一批事件每 5 秒重发一次，逐帧打就是每 5 秒一对
                    # you_died+battle_end，一局结束能刷几百行（实测 22:54→23:06 共 205 行）。
                    # 同一 (event_id, seq) 只记一次；不同 seq 照旧各记一行 —— 信号不丢，噪声归零。
                    if event_id in SEEN_LOG_IDS:
                        logged = self._seen[key]
                        if event_id not in logged:
                            logged.add(event_id)
                            self._log("drop %s reason=seen(seq=%s)", event_id, key)
                    continue
                self._seen[key] = set()
                while len(self._seen) > self._max_seen:
                    self._seen.popitem(last=False)
            # 协议 §5.1 读端义务（v1.30）：``dup == 1`` 的 ``you_hit`` 是
            # **同一发炮弹**的多路投递 → 直接丢弃：不播报、不进受击样本环、
            # 不参与任何累计。seq 已经在上面登记过，所以它下一帧也不会复活。
            if _is_duplicate_hit(event):
                continue
            fresh.append(event)
            if count_kills and event_id == "you_killed":
                self._kills += 1
        return fresh

    def _gap_ready(self, now: float) -> bool:
        """全局最小间隔是否已过。"""
        if self._last_spoken_at is None:
            return True
        return now - self._last_spoken_at >= self.min_gap_s

    def _effective_cooldown(self, event_id: str) -> float:
        """这个 id 此刻的同类冷却秒数（**唯一**按局模式分支的地方）。

        标准 / 未知 / 老 mod（``mode_kind`` 缺失或为 ``None``）一律走原表
        （``self._cooldown``，可被构造参数 ``type_cooldown_s`` 注入，行为不变）；
        只有前线模式（30v30 复活局）把 ``TEAM_EVENT_IDS`` 放宽到
        :data:`FRONTLINE_TEAM_COOLDOWN_S`。判定只走 :func:`_is_frontline` 一处。
        """
        if event_id in TEAM_EVENT_IDS and _is_frontline(self._mode_kind):
            return FRONTLINE_TEAM_COOLDOWN_S
        return self._cooldown.get(event_id, DEFAULT_COOLDOWN_S)

    def _type_ready(self, event_id: str, now: float) -> bool:
        """同类事件冷却是否已过。

        实际秒数由 :meth:`_effective_cooldown` 决定（前线模式放宽队伍事件）。
        冷却为 ``0.0`` 的 id（``TYPE_COOLDOWN_S["you_died"]``）这里恒真 —— 那是
        "绝不许静默丢弃的边沿事件"，防复读由 ``_pick`` 的身份闸负责。
        """
        last = self._last_type_at.get(event_id)
        if last is None:
            return True
        return now - last >= self._effective_cooldown(event_id)

    def _cooldown_left(self, event_id: str, now: float) -> float:
        """同类冷却还剩几秒（只给日志用；``_type_ready`` 才是判定）。"""
        last = self._last_type_at.get(event_id)
        if last is None:
            return 0.0
        return max(0.0, self._effective_cooldown(event_id) - (now - last))

    def _gap_wait(self, now: float) -> float:
        """全局最小间隔还剩几秒（只给日志用）。"""
        if self._last_spoken_at is None:
            return 0.0
        return max(0.0, self.min_gap_s - (now - self._last_spoken_at))

    # ------------------------------------------------- v2.19 / C5 受击分档闸
    def _hit_burst_count(self, now: float) -> int:
        """最近 :data:`HIT_REACTION_MERGE_WINDOW_S` 秒内挨了几下（一帧最多算一下）。"""
        window_start = now - HIT_REACTION_MERGE_WINDOW_S
        while self._hit_burst_times and self._hit_burst_times[0] <= window_start:
            self._hit_burst_times.popleft()
        return len(self._hit_burst_times)

    def _hit_should_merge(self, now: float) -> bool:
        """15 秒内已经 ≥3 下 → 这一下换成合并句（不逐发喊）。"""
        return self._hit_burst_count(now) >= HIT_REACTION_MERGE_HITS

    def _hit_reaction_tier(self, delta: float | int | None, max_health: float) -> str | None:
        """掉血面值 / 最大血量 → ``heavy`` / ``critical`` / ``None``（保持轻量句）。

        ``delta`` 缺失 / 非正数 / ``max_health`` 非正 → ``None``（不猜）。
        ``quiet``（默认）按路线图口径：≥25% heavy、≥50% critical；
        ``full`` 只是把阈值下移到 15% / 35%（更敏感），密度上限不变。
        """
        if delta is None or max_health <= 0:
            return None
        ratio = float(delta) / float(max_health)
        if ratio < 0.0:
            return None
        if self.hit_reaction_level == "full":
            heavy_at = HIT_REACTION_FULL_HEAVY_RATIO
            critical_at = HIT_REACTION_FULL_CRITICAL_RATIO
        else:
            heavy_at = HIT_REACTION_HEAVY_RATIO
            critical_at = HIT_REACTION_CRITICAL_RATIO
        if ratio >= critical_at:
            return "critical"
        if ratio >= heavy_at:
            return "heavy"
        return None

    def _hit_reaction_block_reason(self, now: float) -> str | None:
        """C5 自己的出口闸：``None`` = 放行，否则返回**真实**的丢弃原因。

        顺序有意义：
        1. 刚合并过 → 窗口内不再逐发喊（``hit_merged``）；
        2. 单局 8 句用光 → ``hit_reaction_cap``（合并句也不能顶破）；
        3. 窗口内已经 ≥3 下 → **放行合并句**（它本身就是“别再逐发喊”，不吃冷却）；
        4. 距上次受击话术不足 15 秒 → ``hit_reaction_cooldown``。
        """
        if self._hit_merged_at is not None:
            left = HIT_REACTION_MERGE_WINDOW_S - (now - self._hit_merged_at)
            if left > 0.0:
                return f"hit_merged(剩{left:.1f}s)"
        # 单局 8 句是**硬上限**：合并句也不能把它顶破（它同样计入 8 句）。
        if self._hit_lines_used >= HIT_REACTION_MAX_PER_BATTLE:
            return f"hit_reaction_cap(used={self._hit_lines_used}/{HIT_REACTION_MAX_PER_BATTLE})"
        if self._hit_burst_count(now) >= HIT_REACTION_MERGE_HITS:
            return None
        if self._last_hit_line_at is not None:
            left = HIT_REACTION_COOLDOWN_S - (now - self._last_hit_line_at)
            if left > 0.0:
                return f"hit_reaction_cooldown(剩{left:.1f}s)"
        return None

    # ------------------------------------------- v2.20 / P3 断带 / 断轮
    def _track_tier_for(self, payload: dict[str, Any], now: float) -> str | None:
        """这一下该不该报断带/断轮，以及报哪一档（``None`` = 不出断带句）。

        闸门顺序（缺一条就退回普通受击句）：
        1. 开关关掉；
        2. ``blocked == true`` —— 跳弹不可能断带；
        3. ``crits_count != 1`` —— 只认新 burst / 设备集合变化，同一串连击不逐发喊；
        4. ``crits_devices`` 里没有 ``track`` / ``wheel``（gun/turret 等非行走机构不算）；
        5. 15 秒内已经报过 2 句（断带话术自己的密度上限）。

        严重度只从协议已有的原始 ``crits`` 掩码解：``(mask >> 12) & 4095`` 是
        destroyedDevices，``mask & 4095`` 是 criticalDevices。只是“设备名列表”本身
        没有严重度，所以不能只靠 ``crits_devices`` 判层。
        """
        if not self.track_break_enabled:
            return None
        if payload.get("blocked") is True:
            return None
        if _as_count(payload.get("crits_count")) != 1:
            return None
        parts = _walking_device_parts(payload)
        if not parts:
            return None
        window_start = now - TRACK_BREAK_WINDOW_S
        while self._track_line_times and self._track_line_times[0] <= window_start:
            self._track_line_times.popleft()
        if len(self._track_line_times) >= TRACK_BREAK_MAX_PER_WINDOW:
            return None
        crits = _as_count(payload.get("crits"))
        if crits is not None:
            for part in parts:
                if crits & (TRACK_DEVICE_BITS[part] << TRACK_DESTROYED_SHIFT):
                    return f"{part}_destroyed"
            for part in parts:
                if crits & TRACK_DEVICE_BITS[part]:
                    return f"{part}_critical"
        # 有设备名但没有可用的原始掩码：只说“伤了”，不猜“断了”（宁可轻，不许重）。
        return f"{parts[0]}_critical"

    def _render_mode_switched(self, payload: dict[str, Any]) -> str | None:
        """v2.20 / P3：``mode_switched`` 按 ``mech`` 分档；认不出 → 现状三句。"""
        mech = payload.get("mech")
        activated = payload.get("activated")
        if (
            self.mech_switch_enabled
            and self._mech_lines_used < MECH_MAX_LINES_PER_BATTLE
            and isinstance(mech, str)
            and mech.strip() in MECH_SWITCH_TIERS
            and (activated is True or activated is False)
        ):
            on_tier, off_tier = MECH_SWITCH_TIERS[mech.strip()]
            self._rendered_tier["mode_switched"] = "mech"
            return self._say("mode_switched", on_tier if activated is True else off_tier)
        self._rendered_tier["mode_switched"] = ""
        return _render_mode_switched(payload, book=self._phrases)

    def _block_reason(self, now: float) -> str:
        """warning 被拦下的**真实**原因（间隔 / 队列还有话）。"""
        if not self._gap_ready(now):
            return f"gap(剩{self._gap_wait(now):.1f}s)"
        return f"queue_not_empty(n={len(self._queue)})"

    def _info_block_reason(self, now: float) -> str:
        """info 被拦下的**真实**原因（空闲闸 / 间隔 / 队列）。"""
        if not self._idle_ready(now):
            # ⚠️ 不能写 ``self._last_spoken_at or now``：0.0 是合法时刻，会被当成 None。
            spoken_at = self._last_spoken_at if self._last_spoken_at is not None else now
            wait = max(0.0, self.idle_fill_s - (now - spoken_at))
            return f"idle_fill(剩{wait:.1f}s)"
        return self._block_reason(now)

    def _idle_ready(self, now: float) -> bool:
        """info 档的"空闲填空"闸：距上一句话至少 ``idle_fill_s`` 秒。

        从没说过话（``_last_spoken_at is None``）= 空闲无限长 → 放行。
        ``idle_fill_s=0.0``（构造默认）时这个闸恒真，等于"不传就不管闲"。
        """
        if self._last_spoken_at is None:
            return True
        return now - self._last_spoken_at >= self.idle_fill_s

    # ------------------------------------------------- v2.18 / C8 话术预算
    def _budget_allows(self, level: str, now: float) -> bool:
        """这一档还能不能说（单局总量 + 滑动窗）。

        * 单局总量用完 → 一律不许（**含 critical**：产品口径是"一局最多 N 句"，
          被丢的那句会在日志里留 ``reason=speech_budget(...)``，不是无声消失）。
        * 滑动窗：``critical`` **不受限**（阵亡/结算不许被"话太密"压掉），但照样计入总量。
        """
        if self.speech_budget_per_battle > 0 and self._budget_used >= self.speech_budget_per_battle:
            return False
        if self.speech_burst_max > 0 and level != LEVEL_CRITICAL:
            return self._burst_used(now) < self.speech_burst_max
        return True

    def _burst_used(self, now: float) -> int:
        """最近一个滑动窗内说出口几句（队列很短，逐个比较就够）。"""
        window_start = now - SPEECH_BURST_WINDOW_S
        return sum(1 for moment in self._speak_times if moment > window_start)

    def _budget_reason(self, level: str, now: float) -> str:
        """→ 真实的丢弃原因（日志用，不许写"未知"）。"""
        if self.speech_budget_per_battle > 0 and self._budget_used >= self.speech_budget_per_battle:
            return f"speech_budget(used={self._budget_used}/{self.speech_budget_per_battle})"
        return f"speech_burst(n={self._burst_used(now)}/{self.speech_burst_max},窗{SPEECH_BURST_WINDOW_S:.0f}s)"

    def _budget_record(self, now: float) -> None:
        """记一句"说出去了"（单局总量 +1、滑动窗加一个时间点、顺手清窗外的）。"""
        self._budget_used += 1
        self._speak_times.append(now)
        window_start = now - SPEECH_BURST_WINDOW_S
        while self._speak_times and self._speak_times[0] <= window_start:
            self._speak_times.popleft()

    # ------------------------------------------- v2.18 / C2~C3 战绩账本与关怀
    def _note_history(self, fresh: list[dict[str, Any]], frame: Frame, now: float) -> None:
        """账本只在 ``battle_start`` / ``battle_end`` 两帧碰盘（**绝不每 tick 读**）。

        * ``battle_start``：读一次，缓存到 ``_history_rows``（供本局参考；这一版只打日志，
          为 C7「开场人格化」留好数据，不新增播报）。
        * ``battle_end``：先读一次（分档要用**本局之前**的均伤）→ 追加本局（按 seq 去重，
          一局绝不允许写两行）→ 算连败/连胜。

        ⚠️ **复盘分档不依赖账本**（C1 的五档里只有"高于历史均伤"那一档需要它）：
        所以 ``_recap_ctx`` 在 ``history is None`` 时也照算 —— 否则关掉账本会顺手把
        "赢得很猛/搭进去一条命/输得太快"三档一起关掉，那越权了（那是 ``recap_enabled`` 的事）。
        """
        ids = {str(event.get("id")) for event in fresh}
        if "battle_start" in ids:
            # v2.19 / C7：开场人格化只用 C2 账本，不新增任何数据源。
            # 账本关着 / 读不到 / 坏了 → ``_start_ctx`` 留空，``_render`` 退回通用开场句。
            self._history_rows = self._history_read() if self.history is not None else []
            self._start_ctx = self._battle_start_context(frame, now)
            self._log(
                "history load n=%d reason=battle_start ctx=%s",
                len(self._history_rows),
                "none" if not self._start_ctx else "ok",
            )
        end = next((event for event in fresh if str(event.get("id")) == "battle_end"), None)
        if end is None:
            return

        rows = self._history_read()      # history=None → 空表（= 没有历史）
        payload = end.get("payload") if isinstance(end.get("payload"), dict) else {}
        battle = self._battle_from_frame(frame, payload, now)
        self._recap_ctx = {
            "tier": _recap_tier(battle, vehicle_average_damage(rows, battle.vehicle)),
            "care": self._streak_care(rows, battle, now),
        }
        if self.history is not None:
            self._append_history(battle, end)
        if self.speech_budget_per_battle > 0:
            # 每局结束一行统计（只在 debug_log 开时打，`_log` 自己会挡）。
            self._log("speech budget: used=%d dropped=%d", self._budget_used, self._budget_dropped)

    def _history_read(self) -> list[Battle]:
        """读账本；任何异常都当成"没有历史"（相关话术整句不出现，绝不猜）。"""
        try:
            return list(self.history.recent()) if self.history is not None else []
        except Exception:  # pragma: no cover - History 自己已经吞了，这里再兜一层
            return []

    def _battle_start_context(self, frame: Frame, now: float) -> dict[str, Any]:
        """C7 开场人格化的判据（**只用账本 + 本帧自己的车**）。

        * ``today``：今天（本地日期）+ 本局 = 第几局；
        * ``battles`` / ``avg``：同一台车的历史局数与均伤（只统计有 ``dmg`` 的局）。

        账本为空 / 行全坏 / 时间戳读不出来 → ``{}``（调用方退回通用开场句）。
        ⛔ 绝不拿 0 或别的字段顶：没有可信历史就不提历史。
        """
        if not self.battle_start_persona_enabled or not self._history_rows:
            return {}
        try:
            today = datetime.fromtimestamp(now).date()
        except (OverflowError, OSError, ValueError):
            return {}
        today_count = 1
        for row in self._history_rows:
            if _same_local_day(row.ts, today):
                today_count += 1
        vehicle = _own_vehicle(frame)
        context: dict[str, Any] = {"today": today_count, "vehicle": vehicle}
        if vehicle:
            samples = [row for row in self._history_rows if row.vehicle == vehicle]
            damages = [row.dmg for row in samples if row.dmg is not None]
            context["battles"] = len(samples)
            context["avg"] = int(round(sum(damages) / len(damages))) if damages else None
        return context

    def _append_history(self, battle: Battle, event: dict[str, Any]) -> None:
        """追加本局账目（**按事件 seq 去重**：同一局绝不允许写两行）。"""
        if self.history is None:
            return
        seq = event.get("seq")
        key = (self._arena or "", seq if isinstance(seq, int) and not isinstance(seq, bool) else None)
        if self._history_written_key == key:
            return
        self._history_written_key = key
        try:
            written = self.history.append(battle)
        except Exception:  # pragma: no cover - 账本失败绝不影响播报
            written = False
        self._log("history append result=%s ok=%s", battle.result, written)

    def _battle_from_frame(self, frame: Frame, payload: dict[str, Any], now: float) -> Battle:
        """这一帧的结算数据 → 一行账目 / 复盘分档的输入。

        **来源单一化**（v1.30 的老规矩，别在这里破）：``state.result`` 优先、payload 兜底，
        且**每一格都按同一顺位取** —— 不允许"胜负来自 state、击杀来自 payload"这种半掺，
        否则分档用的数字和念出来的数字会对不上（``test_state_result_wins_over_a_contradicting_payload``）。
        ``battle_seconds`` 只有 payload 有（面板的 ``result`` 里没有这个键）。
        """
        result = frame.result if isinstance(frame.result, dict) else {}
        arena = frame.arena if isinstance(frame.arena, dict) else {}
        outcome, _ = _resolve_outcome(result, payload)
        kills = _as_count(result.get("kills"))
        if kills is None:
            kills = _as_count(payload.get("kills"))
        dmg = _as_count(result.get("damage"))
        if dmg is None:
            dmg = _as_count(payload.get("dmg_dealt_total"))
        deaths = _as_count(result.get("deaths"))
        if deaths is None:
            deaths = _as_count(payload.get("deaths"))
        map_name = arena.get("map") if isinstance(arena.get("map"), str) else None
        return Battle(
            ts=float(now),
            map=map_name,
            vehicle=_own_vehicle(frame),
            mode_kind=frame.mode_kind,
            result=outcome,
            kills=kills,
            dmg=dmg,
            deaths=deaths if deaths is not None else 0,
            seconds=_as_count(payload.get("battle_seconds")),
        )

    def _streak_care(self, rows: list[Battle], battle: Battle, now: float) -> str | None:
        """连败 / 连胜关怀（v2.18 / C3）→ 话术档位名，或 ``None``。

        三个条件缺一不可：最近 3 局（含本局）结果**完全相同且只能是 win/lose**、
        本局还没用过（``_care_used``）、同类 30 分钟内没说过（``_streak_last_at``）。
        含 ``draw``/``unknown`` 的历史一律不触发（判不清就别提）。
        """
        if battle.result not in ("win", "lose") or self._care_used:
            return None
        history_tail = last_results(rows, STREAK_LEN - 1)
        chain = [*history_tail, battle.result]
        if len(chain) < STREAK_LEN or any(item != battle.result for item in chain):
            return None
        tier = "care_win" if battle.result == "win" else "care_lose"
        last = self._streak_last_at.get(tier)
        if last is not None and now - last < STREAK_COOLDOWN_S:
            return None
        self._care_used = True
        self._streak_last_at[tier] = now
        return tier

    def _numbers_should_speak(self, tier: str, now: float) -> bool:
        """人数差档位该不该报（v1.30 的防抖）。

        规则只有三条，其余一律"不说"：

        1. 本局第一次拿到档位（``_numbers_tier is None``）→ **只建基线**，不出声
           （和 ``enemy_low`` / ``team_behind`` 的"换局首帧只建基线"同一条规矩：
           接手的局面不是"刚发生的事"）；
        2. 档位没变 → 重复，不说（这是最主要的"少说"来源：一直 13v3 不吭声）；
        3. 档位变了：距上次**报出去**不足 ``NUMBERS_MIN_GAP_S``（3 秒）→ 压掉，
           **除非**是优势↔劣势的关键翻转（局面反转必须立刻说）。

        被压掉的变化**不吞档位**：``_numbers_tier`` 只在真的报出去时推进，
        所以抖动过去之后，下一次比较仍然对着"最后说出口的那一档"。
        """
        if tier == self._numbers_tier:
            return False
        if self._numbers_tier is None:
            self._numbers_tier = tier
            return False
        if self._numbers_at is not None and now - self._numbers_at < NUMBERS_MIN_GAP_S:
            return _numbers_flip(self._numbers_tier, tier)
        return True

    def _commit(self, event_id: str, now: float, text: str | None = None) -> None:
        """记下"这句话说出去了"，供间隔/冷却判断使用。

        ⚠️ 正常路径请走 :meth:`_announce`（它在这里之外还要更新 ``last_cue``）。
        直接调本方法只用于"没文本、只记账"的场合（例如用例手工铺时间线）。
        """
        self._last_spoken_at = now
        self._last_type_at[event_id] = now
        # "局内说了几次"：多句变体的轮换种子（换局归零，见 ``_clear_derived``）。
        self._said_counts[event_id] = self._said_counts.get(event_id, 0) + 1
        if event_id == "idle_company":
            # 二阶静默（v2.18 / C4）：**说出口**才算用掉那一局一次的机会（没说出来还会重试）。
            self._deep_used = True
        if event_id == "you_damaged" and text is not None:
            self._last_damage_text = text
        # v2.20：渲染时记的档位在这里一次性取走（断带 / 机制 / C5 / C6 共用）。
        tier = self._rendered_tier.pop(event_id, None)
        # ---- v2.20 / P3：断带话术单独计数（15 秒 ≤2）；不占 C5 的 8 句额度 ----
        if event_id == "you_hit" and tier in TRACK_BREAK_TIER_SET:
            self._track_line_times.append(now)
        # ---- v2.19 / C5：受击话术自己记账（单局 8 句 / 15 秒冷却 / 合并锁）----
        elif self.hit_reaction_enabled and event_id in HIT_REACTION_EVENT_IDS:
            self._hit_lines_used += 1
            self._last_hit_line_at = now
            if tier == "swarm":
                self._hit_merged_at = now
        # ---- v2.20 / P3：机制话术（开场 + 切换）单局 ≤6 句 ----
        if tier == "mech":
            self._mech_lines_used += 1
        # "每局上限"在这里记账（v2.4）：额度要绑在**说出口**上，而不是"检测到"那一刻。
        # 检测到就扣额度的话，一句被排队覆盖/过期就整局哑了（hp_critical 踩过）。
        if event_id == "enemy_low":
            self._enemy_low_count += 1
        elif event_id == "time_low":
            self._time_low_count += 1
            # 说出口才算"这一局用掉了"。锁一旦合上，剩余时间再怎么变长（加时/换阶段）
            # 都不许把它掰回来 —— 只有换局（``_clear_derived``）能重置。
            self._time_low_locked = True
        elif event_id == "team_behind":
            self._behind_count += 1
            self._behind_active = True       # 边沿到此才算被消费（让位过的不算）
            # v2.19 / C6：每档一局一次 —— 同样绑在“说出口”上（让位过的不算用过）。
            if tier in TEAM_BEHIND_TIERS:
                self._behind_tiers_used.add(tier)
                self._behind_tier = tier

    # ----------------------------------------------------------- 排队（v2.4）
    def _refund(self, event_id: str) -> None:
        """没说出来就别记账：把自己的"每局一次"额度还回去。

        ``hp_critical`` 的额度在 ``_derive`` 触发那一刻就划掉了；如果这句话
        被排队覆盖/过期/换局清掉，等于整局再也不会响 —— 所以扔掉时补一次。
        """
        if event_id in QUOTA_EVENT_IDS:
            self._hp_armed = True

    def _enqueue(self, event_id: str, text: str, now: float, *, force: bool = False) -> None:
        """间隔没到就把这句塞进队列；不排队的 id 直接丢（它们会反复出现）。

        ``force=True``（v2.12）无视 ``DEFERRABLE``：关掉打断时 critical 档
        必须照样进队列 —— 那几条事件只活一帧，丢了就永远没有第二次机会。
        """
        if not force and event_id not in DEFERRABLE:
            return
        # 预算检查**不在这里**：``observe`` 已经在挑赢家之前把超预算的档位摘掉了
        # （``_budget_allows``），队列补说的那句由 ``_announce`` 再拦一次 —— 同一个
        # 判定不做第三遍，否则日志会把一句话丢两次。
        if event_id in LATEST_WINS:
            self._discard(event_id)  # 同类只留最新，别让队列里躺着两句旧数字
        self._queue.append((event_id, text, now + self.pending_ttl_s))
        while len(self._queue) > self._pending_max:
            dropped_id, _dropped_text, _expires = self._queue.popleft()
            self._refund(dropped_id)
            self._log_drop(dropped_id, "queue_full")
        self._log("enqueue %s ttl=%.1f len=%d", event_id, self.pending_ttl_s, len(self._queue))

    def _discard(self, event_id: str) -> None:
        """扔掉排队里所有同 id 的句子（没说出来 → 额度还回去）。"""
        kept = [item for item in self._queue if item[0] != event_id]
        if len(kept) == len(self._queue):
            return
        self._queue.clear()
        self._queue.extend(kept)
        self._refund(event_id)
        self._log("discard %s reason=latest_wins", event_id)

    def _clear_queue(self, *, reason: str) -> None:
        """清空队列（换局/重启/生死翻篇）；没说出来的一律还额度。

        ``reason`` 是**必填**的真实来由（``gap_bypass`` / ``arena_reset`` / ``restart`` /
        ``prime``）：日志里"队列为什么被清了"必须能直接看出来。
        """
        count = len(self._queue)
        while self._queue:
            dropped_id, _dropped_text, _expires = self._queue.popleft()
            self._refund(dropped_id)
        if count:
            self._log("clear_queue n=%d reason=%s", count, reason)

    def _purge_expired(self, now: float) -> None:
        """过了时效的排队直接扔 —— 旧消息没价值（数字类的更没价值）。"""
        for item in tuple(self._queue):
            if item[2] <= now:
                self._queue.remove(item)
                self._refund(item[0])
                self._log("expire %s", item[0])

    def _queued_ready(self, now: float) -> int | None:
        """挑一条现在能说的排队句子：同类冷却已过 + 档位高者先（同档看 PRIORITY、再取最早）。

        排序键是 ``(档位数值, 档内 PRIORITY)`` 的元组，不是单个数字（v2.12）：
        队列里同时躺着 critical 和 warning 时，先补说急的那档 —— 单看 ``PRIORITY``
        会出现"warning 的 30 压过 critical 的 40"这种跨档错比（其实数字不可比）。
        """
        best_index: int | None = None
        best_rank: tuple[int, int] | None = None
        for index, (event_id, _text, _expires) in enumerate(self._queue):
            if not self._type_ready(event_id, now):
                continue
            level = self._level_of(event_id)
            rank = (LEVEL_PRIORITY[level], PRIORITY.get(event_id, DEFAULT_PRIORITY))
            if best_rank is None or rank > best_rank:
                best_rank = rank
                best_index = index
        return best_index

    def _take(self, index: int, now: float) -> str:
        """把队列里第 ``index`` 条说出来（并记账 + 更新 ``last_cue``）。"""
        event_id, text, _expires = self._queue[index]
        del self._queue[index]
        return self._announce(event_id, now, text)

    # --------------------------------------------------------------- 派生层
    def _clear_derived(self) -> None:
        """换局/启动：派生状态一律回到"没有基线"。

        基线留着比清掉更危险 —— 上一局的队伍计数/血量拿来跟这一局比，会当场喊
        "一口气干掉 12 个"。清空之后第一帧只建基线、不出声。
        """
        self._prev_reload = None
        self._prev_health = None
        self._prev_fires = None
        self._prev_enemy_alive = None
        self._prev_ally_alive = None
        self._hp_armed = True
        self._hit_samples.clear()
        self._suppressed_count = 0
        # ---- 整局层面提醒的额度（v2.4）：按"说出口"计数，换局归零 ----
        self._enemy_low_count = 0
        self._time_low_count = 0
        # 换局才把"时间告急"重新上膛（v2.10 / B5）：上一局说过、或者加时过，
        # 都不许把锁带进新一局（这里是**唯一**解锁点）。
        self._time_low_locked = False
        self._behind_count = 0
        # 阵亡身份（``deaths``）属于局内状态：换局/重启后第一个 ``deaths`` 必须重新算一次。
        self._last_death_count = None
        # 落后提醒是**边沿**：None = 还没有基线（换局后第一帧、启动 prime 之前）。
        self._behind_active: bool | None = None
        # 人数差档位（v1.30）：换局一律回到"没有基线"，新局首帧只建基线。
        self._numbers_tier = None
        self._numbers_at = None
        self._derived_seen.clear()
        # 话术变体的轮换种子是"局内次数"，同样属于局内状态。
        self._said_counts.clear()
        # ---- v2.18：话术预算 / 二阶静默 / 复盘上下文都是**局内**状态 ----
        self._budget_used = 0
        self._budget_dropped = 0
        self._speak_times.clear()
        self._deep_used = False
        self._deep_try_at = 0.0
        self._recap_ctx = {}
        self._care_used = False
        # ⚠️ ``_streak_last_at``（连败/连胜 30 分钟冷却）**故意不在这里清**：它跨局有效，
        # 一局一清的话"30 分钟内第二局不许再提"就成了空话。
        self._history_written_key = None
        # ---- v2.19 / P2：C5 / C6 / C7 的局内状态 ----
        self._hit_burst_times.clear()
        self._hit_lines_used = 0
        self._last_hit_line_at = None
        self._hit_merged_at = None
        self._behind_tier = None
        self._behind_tiers_used.clear()
        self._start_ctx = {}
        self._rendered_tier.clear()
        # v2.20 / P3：断带窗口与机制计数都是局内状态。
        self._track_line_times.clear()
        self._mech_lines_used = 0

    def _prime_derived(self, frame: Frame) -> None:
        """启动基线：把这一帧的状态字段当成"已经发生过的"，一句话都不说。"""
        self._clear_derived()
        me = frame.me if isinstance(frame.me, dict) else {}
        team = frame.team if isinstance(frame.team, dict) else {}
        self._prev_reload = _as_ratio(me.get("reload_ratio"))
        self._prev_health = _as_ratio(me.get("health_ratio"))
        self._prev_fires = _as_count(me.get("fires"))
        self._prev_enemy_alive = _as_count(team.get("enemy_alive"))
        self._prev_ally_alive = _as_count(team.get("ally_alive"))
        # 启动时就已经"血线告急"：那不是我们看见的"跌破"，先别上膛。
        health = self._prev_health
        self._hp_armed = not (health is not None and 0.0 < health < HP_CRITICAL)
        # 启动/换局那一刻就已经落后 → 记成"已经在落后里"，等**再**恶化才出声。
        # 否则重启/换局后的第一句就是坏消息（MVP-2 B 组的"prime 帧不许说话"）。
        self._behind_active = _team_behind(team)
        # v2.19 / C6：当前档位也算基线（第一帧只接住局面，不评价）。
        self._behind_tier = _team_behind_tier(team) if self.team_behind_tiers_enabled else None
        # 人数差（v1.30）：启动那一刻的局面是"接手的"，不是"刚发生的" →
        # 只把当前档位记成基线，一句话都不说（``_numbers_at`` 留 None 表示"没报过"）。
        snapshot = _numbers_snapshot(frame.mode_kind, team)
        self._numbers_tier = None if snapshot is None else snapshot[0]
        self._numbers_at = None

    def _absorb_hits(self, fresh: list[dict[str, Any]], now: float) -> None:
        """把这一帧**新出现**的受击事件记进固定大小的环（suppressed 用）。

        复用 ``_consume`` 的去重结果：同一条事件在滑动窗口里反复出现也只记一次。
        时间基准统一用 observe 的 ``now``（和冷却同一个钟），不掺事件自带的 ts。

        v2.19 / C5：同一个环还用来判"15 秒内挨了 ≥3 次"。一炮通常同时送来
        ``you_hit`` + ``you_damaged``，所以**一帧最多记一次**（按帧计"挨打次数"，
        不按事件条数），否则同一炮会被数成两下。
        """
        frame_hit = False
        for event in fresh:
            event_id = event.get("id")
            if event_id in ("you_damaged", "you_hit"):
                frame_hit = True
            if event_id != "you_damaged":
                continue
            payload = event.get("payload")
            yaw: float | None = None
            if isinstance(payload, dict):
                raw = payload.get("hitDirYaw")
                # 缺失/None/字符串 → None（降级路径），不猜方向。
                if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                    yaw = float(raw) % 360.0
            self._hit_samples.append((now, yaw, self._prev_health))
        if frame_hit:
            self._hit_burst_times.append(now)

    def _emit(self, out: list[dict[str, Any]], event_id: str, frame: Frame, payload: dict[str, Any]) -> None:
        """造一条合成事件。seq 走自己的自增序列，绝不进 ``_max_event_seq``。"""
        if frame.seq is not None:
            key = f"{event_id}@{frame.seq}"
            if key in self._derived_seen:
                return  # 同一帧重复调用（重复帧兜底）只出一次
            self._derived_seen[key] = None
            while len(self._derived_seen) > MAX_DERIVED_SEEN:
                self._derived_seen.popitem(last=False)
        self._derived_seq += 1
        out.append(
            {
                "seq": self._derived_seq,
                "id": event_id,
                "ts": frame.ts,
                "payload": payload,
                "derived": True,
            }
        )

    def _derive(self, frame: Frame, now: float) -> list[dict[str, Any]]:
        """从 ``Frame`` 的**状态字段**推导语义事件。只读 frame，绝不改 frame/events。

        九个 detector 一律走"边沿"：值跨过阈值才产出一条事件，所以 4Hz 连读到同一
        个不动的帧也只出一次。字段缺失 / None / 类型不对 → 该 detector 本帧不出声
        （**绝不拿 0 或默认值顶替**，那是编事实）。
        """
        me = frame.me if isinstance(frame.me, dict) else {}
        team = frame.team if isinstance(frame.team, dict) else {}
        arena = frame.arena if isinstance(frame.arena, dict) else {}
        battle = frame.mode == "battle"

        alive = me.get("alive")
        health = _as_ratio(me.get("health_ratio"))
        reload_ratio = _as_ratio(me.get("reload_ratio"))
        fires = _as_count(me.get("fires"))
        enemy_alive = _as_count(team.get("enemy_alive"))
        ally_alive = _as_count(team.get("ally_alive"))
        # v2.6：真实编制。mod v1.26 起 ``team.enemy_total`` 是面板条数（开局就 15），
        # 不再是从 0 慢慢爬的"认识过几台" —— 收尾提醒必须靠它当闸（见 ⑦）。
        enemy_total = _as_count(team.get("enemy_total"))

        derived: list[dict[str, Any]] = []

        # ① 装填完成：reload_ratio 由 <0.99 跨到 ≥0.99。只在自己活着、局内时算。
        if battle and alive is True and reload_ratio is not None:
            prev_reload = self._prev_reload
            if prev_reload is not None and prev_reload < RELOAD_READY <= reload_ratio:
                self._emit(derived, "reload_done", frame, {})

        # ② 血线告急：跌破 0.25 报一次；回到 0.35 以上才重新上膛 —— 这样"擦着
        #    0.25 来回抖"不会变成复读机，而真回血再来一次也不是哑巴。
        if health is not None:
            if health >= HP_REARM:
                self._hp_armed = True
            elif alive is True and 0.0 < health < HP_CRITICAL:
                if self._hp_armed:
                    self._hp_armed = False
                    self._emit(derived, "hp_critical", frame, {})
                elif self._prev_health is not None and self._prev_health >= HP_CRITICAL:
                    # 又一次跌破红线，可这一局的额度已经用掉（或启动时就在红线下）——
                    # 这是"候选被额度挡下"的真实时刻，只在**这一次跌破**记一行，不是每帧都记。
                    self._log_drop("hp_critical", "quota_used")

        # ③ 敌方少人：只在局内比较。结算后 team 可能被清零，那时候说
        #    "一口气干掉 15 个"就是纯纯的编事实。
        #
        # v1.30：``team_events_enabled`` 关着（默认）时**不合成**这条事件 ——
        #    队友的击杀不是本人的战果，产品定位要求静音。基线照旧推进，
        #    打开开关时不会因为"攒了很久"而一次报一串。
        if battle and self.team_events_enabled and enemy_alive is not None and self._prev_enemy_alive is not None:
            if enemy_alive < self._prev_enemy_alive:
                self._emit(
                    derived,
                    "enemy_killed",
                    frame,
                    {"n": self._prev_enemy_alive - enemy_alive, "left": enemy_alive},
                )

        # ④ 我方少人：自己死的那一帧 ``me.alive`` 是 False，那句交给真实的
        #    ``you_died``（同一件事说两遍最招人烦）。alive 缺失同样不猜。
        # v1.30：同样受 ``team_events_enabled`` 管（默认静音）。
        if (
            battle
            and self.team_events_enabled
            and alive is True
            and ally_alive is not None
            and self._prev_ally_alive is not None
        ):
            if ally_alive < self._prev_ally_alive:
                self._emit(
                    derived,
                    "ally_killed",
                    frame,
                    {"n": self._prev_ally_alive - ally_alive, "left": ally_alive},
                )

        # ⑤ 火灭了：**从状态边沿自己推**（``me.fires`` 由 ≥1 跨到 0），不只等 mod 的
        #    ``fire_out`` 事件。理由是事件只活一帧、且写端漏发/整帧丢弃都会让它永远
        #    没有第二次；而 fires 是这一帧的确定状态，"1→0 和 2→0 都是全灭了"。
        #    ``fire_out``（mod 事件）与 ``fire_cleared``（派生）说的是同一件事，两条
        #    同时到达时由 ``_pick`` 按档内 ``PRIORITY`` 只留一条（同属 info 档）。
        if battle and fires == 0 and self._prev_fires is not None and self._prev_fires >= 1:
            self._emit(derived, "fire_cleared", frame, {})

        # ⑥ 被压制（替代"集火"，真集火不好做）：窗内挨够两下 + 方向分散 + 掉了 10% 血。
        if health is not None and self._suppressed_count < SUPPRESS_MAX_PER_BATTLE:
            hits = [sample for sample in self._hit_samples if now - sample[0] <= SUPPRESS_WINDOW_S]
            if len(hits) >= SUPPRESS_MIN_HITS and _directions_spread(hits):
                start_health = hits[0][2]
                if start_health is not None and start_health - health >= SUPPRESS_MIN_DROP - _EPS:
                    self._suppressed_count += 1
                    self._emit(derived, "suppressed", frame, {})

        # ⑦ 收尾提醒（v2.4，v2.6 补闸）：已知的对面存活 ≤2 → 一局只说一次。
        #
        #    原话术口径是"已知"：``team.enemy_alive`` 只覆盖看得到的车（mod v1.24 的
        #    累积已知表），雾战里"就剩两台"是可能说错的 —— 所以话术**不报数字**
        # （那次"还剩 N 个"翻车就是同一个坑）。
        #    要求上一帧也有计数（``_prev_enemy_alive``）：换局/启动后的第一帧只建
        #    基线，不拿"接手的局面"当"刚发生的事"。
        #
        # ⛔ v2.6 修的是"开局空枪"（实测）：旧判据只有
        #    ``enemy_alive <= 2``，而 mod v1.24 口径下**开局 `enemy_alive` 就是 0**
        #    （双方出生点隔老远，一台敌车都还没进 AoI）→ 0 ≤ 2 成立 → **每局开场
        #    第二帧就说"对面没几台了，收尾吧"**。这不是猜错数字，是把"编制还没
        #    拿到"当成了"对面快没了"。
        #    → 加 ``enemy_total > 0`` 这道闸：编制没拿到（缺失/0/加载期）本帧不出声。
        #    mod v1.26 起 ``enemy_total`` 是**真实编制**（面板条目数，开局即 15），
        #    所以真正的"对面全灭"照样说得出来（``enemy_alive=0, enemy_total=15`` 成立）。
        if (
            battle
            and alive is True
            and enemy_alive is not None
            and enemy_total is not None
            and enemy_total > 0
            and self._prev_enemy_alive is not None
            and enemy_alive <= ENEMY_LOW_AT
            and self._enemy_low_count < 1
        ):
            self._emit(derived, "enemy_low", frame, {"left": enemy_alive})

        # ⑧ 时间告急（v2.4，v2.10 收口）：period==2（战斗阶段）+ 剩余 ≤60 秒 + 一局一次。
        #
        #    数据源**只认** ``arena.time_left``（mod v1.27 显式剩余秒，协议 §2.2）：
        #      · 键不存在（写端读不到计时，或 >3600 的脏帧被写端整帧丢弃）→ 本帧闭嘴；
        #        **缺键 ≠ 0**，不能说成"时间到了"；
        #      · ``overtime is True``（加时）→ 不喊时间告急，**并且把本局的锁合上**：
        #        加时之后哪怕转回正常态、又跌到 60 秒，也不许复活第二次；
        #      · 剩余时间**变长**（加时/换阶段/换模式）→ 什么都不重置，锁照样合着；
        #      · ``time_probe`` 是写端的排障探针，这里**一个字节都不读**（判定不许依赖探针）。
        #    ⛔ 红线：绝不拿 ``period`` / ``periodLength`` / ``phase`` 反推剩余秒。
        period = _as_count(arena.get("period"))
        remaining = _arena_time_left(frame)
        if battle and period == ARENA_PERIOD_BATTLE and remaining is not None:
            if _arena_overtime(frame):
                self._time_low_locked = True
            elif not self._time_low_locked and self._time_low_count < 1 and remaining <= TIME_LOW_S:
                self._emit(derived, "time_low", frame, {})

        # ⑨ 落后提醒（v2.4）：我方存活 - 敌方存活 ≤ -3 的**边沿**。
        #    "边沿"= 回落再成立才算新的一次（一直落后不重复念），每局最多两次。
        #    ``_behind_active`` 只在**说出口**时（``_commit``）才置 True：这条提醒
        #    优先级压在战况计数之下，掉落那一帧通常让位给"我们这边少了一个" ——
        #    边沿当场就消费掉的话，这句话就永远没了。
        #
        #    ⛔ 前线模式（mod v1.38 的 ``frontline``）：30v30 复活局里"场上活着的少"
        #    不代表落后（预备队每 5 分钟 +1、人人可复活）→ 这条**整局不发**。
        #    判定只走 ``_is_frontline`` 一处；标准局/未知/老 mod 的路径一个字没动。
        behind = _team_behind(team)
        if battle and behind is not None and not _is_frontline(frame.mode_kind):
            if self.team_behind_tiers_enabled:
                # v2.19 / C6：按差值分 mild / big / endgame，每档一局一次。
                # 边沿判据从“落后/不落后”细化成“档位变差”，但**基线规则不变**：
                # 换局/启动第一帧只接住当前档位，不出声。
                tier = _team_behind_tier(team)
                if self._behind_active is None:
                    self._behind_active = behind
                    self._behind_tier = tier
                elif tier is None:
                    self._behind_active = False       # 追上来了 → 允许下一次边沿
                    self._behind_tier = None
                elif (
                    tier != self._behind_tier
                    and _team_behind_severity(tier) > _team_behind_severity(self._behind_tier)
                    and self._behind_count < TEAM_BEHIND_TIER_MAX_PER_BATTLE
                    and tier not in self._behind_tiers_used
                ):
                    self._emit(derived, "team_behind", frame, {"tier": tier})
                elif tier != self._behind_tier:
                    # 好转（big→mild）或已被额度/档位闸挡住：不开口，只更新当前档。
                    self._behind_tier = tier
            elif self._behind_active is None:
                self._behind_active = behind      # 接管局面（换局首帧）：只建基线
            elif not behind:
                self._behind_active = False       # 追上来了 → 允许下一次边沿
            elif not self._behind_active and self._behind_count < TEAM_BEHIND_MAX_PER_BATTLE:
                self._emit(derived, "team_behind", frame, {})

        # ⑩ 人数差档位（v1.30）：6 档 + 波次/未知降级。
        #
        #    ⛔ 判据（协议 §4 的读端义务）：说"敌方还剩 N 个"必须**同时**满足
        #    ``mode_kind == "standard"``、``enemy_total_known is True``、
        #    ``enemy_alive`` 是整数。波次局敌人会一直补、未知局数不出来 —— 那时候
        #    只说我方人数（``wave`` / ``unknown`` 两档），一个敌方数字都不出现。
        #    缺失/None/类型不对一律走 ``unknown``（不猜，不拿 0 顶替）。
        #
        #    防抖：只压"同一侧的档位抖动 + 重复"；从优势翻到劣势（或反过来）是
        #    局面反转，必须立刻说。首帧只建基线（和别的边沿 detector 一样）。
        if battle:
            snapshot = _numbers_snapshot(frame.mode_kind, team)
            if snapshot is not None:
                tier, ally_n, enemy_n = snapshot
                if self._numbers_should_speak(tier, now):
                    self._numbers_tier = tier
                    self._numbers_at = now
                    self._emit(
                        derived,
                        "team_numbers",
                        frame,
                        {"tier": tier, "ally": ally_n, "enemy": enemy_n},
                    )

        # ⑪ 二阶静默（v2.18 / C4）：距上次说话 ≥ ``idle_deep_s`` 时，说一句**不依赖任何
        #    战况**的陪伴（"我在呢"），一局最多一次。判据全部是"闸"，缺一条就不说：
        #    · 只在**战斗中**（``mode == battle``）且 ``mode_kind`` 是能判定的那几档
        #      —— 车库/结算/观战/判不出的局一律不说；
        #    · 本局必须**已经说过话**（``_last_spoken_at`` 非空）：否则开局那一秒就会
        #      冒出一句"我在呢"，那不是陪伴是打扰；
        #    · 空闲闸/间隔/队列都要过，且用 ``_deep_try_at`` 把重试压到 60 秒一次
        #      （被别的句子顶掉时**不丢**，下个窗口再试；但绝不逐帧重试刷日志）。
        if (
            battle
            and self.idle_deep_s > 0.0
            and not self._deep_used
            and now >= self._deep_try_at
            and frame.mode_kind in ("standard", "wave", "frontline")
            and self._last_spoken_at is not None
            and now - self._last_spoken_at >= self.idle_deep_s
            and self._idle_ready(now)
            and self._gap_ready(now)
            and not self._queue
        ):
            self._deep_try_at = now + self.idle_deep_s
            self._emit(derived, "idle_company", frame, {})

        # 基线最后推进：上面所有比较用的都是"上一帧的值"。
        if reload_ratio is not None:
            self._prev_reload = reload_ratio
        if health is not None:
            self._prev_health = health
        if fires is not None:
            self._prev_fires = fires
        if enemy_alive is not None:
            self._prev_enemy_alive = enemy_alive
        if ally_alive is not None:
            self._prev_ally_alive = ally_alive
        return derived

    # ------------------------------------------------------------------ 话术
    def _say(
        self,
        event_id: str,
        tier: str = "default",
        *,
        seed: int | None = None,
        **fields: Any,
    ) -> str | None:
        """取一句台词并填占位符；这一档没有句子 → ``None``（宁可不说，不许编）。

        ``seed`` 决定"同一档里放了几句变体"时挑第几句（``lines[seed % len]``）：
        默认用**局内已说出口次数**（``_commit`` 记账、换局归零），所以同输入同输出、
        可测；个别话术要保住历史行为（低血俏皮话按 ``event.seq`` 轮换），由调用点
        显式把 ``seq`` 传进来。单句档位下 ``seed`` 不影响结果。
        """
        if seed is None:
            seed = self._said_counts.get(event_id, 0)
        return self._phrases.text(event_id, tier, seed=seed, **fields)

    def _render(self, event: dict[str, Any], frame: Frame) -> str | None:
        event_id = str(event.get("id"))
        payload = _allowed_payload(event.get("payload"))
        now = self._now

        if event_id == "battle_start":
            # 优先用 me.vehicle_name（写端取的是游戏本地化串，如 "WZ219"），
            # 事件 payload 里的 vehicle 是内部 ID（"china:Ch71_WZ_219"），只当兜底。
            vehicle = _own_vehicle(frame) or _display_vehicle(payload.get("vehicle"))
            self._rendered_tier["battle_start"] = ""
            # v2.19 / C7：有可信账本时**替换**这一句的选句（仍然只有一句）。
            # 账本空/坏/关 → 一个“今天/均伤”都不提，退回 v2.18 的通用开场句。
            if self.battle_start_persona_enabled and self._start_ctx:
                ctx = self._start_ctx
                today = ctx.get("today")
                battles = ctx.get("battles")
                average = ctx.get("avg")
                if vehicle and isinstance(battles, int) and battles >= BATTLE_START_VEHICLE_MIN_BATTLES:
                    if isinstance(average, int):
                        return self._say(
                            "battle_start",
                            "vehicle_average",
                            vehicle=vehicle,
                            avg=average,
                            today=today,
                        )
                    return self._say(
                        "battle_start",
                        "vehicle_battles",
                        vehicle=vehicle,
                        battles=battles,
                        today=today,
                    )
                if isinstance(today, int) and today >= 2:
                    if vehicle:
                        return self._say("battle_start", "day_vehicle", vehicle=vehicle, today=today)
                    return self._say("battle_start", "day", today=today)
            # v2.20 / P3：C7 不可用（没历史/关掉）时才轮到机制自述；
            # 两者都可用时**只出 C7**（本单硬判据）。
            mech_tier = _mech_opening_tier(frame)
            if (
                self.mech_opening_enabled
                and mech_tier is not None
                and self._mech_lines_used < MECH_MAX_LINES_PER_BATTLE
            ):
                self._rendered_tier["battle_start"] = "mech"
                return self._say("battle_start", mech_tier)
            if vehicle:
                return self._say("battle_start", "with_vehicle", vehicle=vehicle)
            return self._say("battle_start")
        if event_id == "you_killed":
            # v1.30：先看特殊死因（殉爆 / 烧穿），再按"这一局第几个"分级。
            # ⛔ 死因字段本轮写端**恒给 null**（协议 §5.2，没有确凿来源）→ 读到 null
            # 就走普通档；绝不把"不知道"说成"不是被烧死的"（那是断言）。
            kills = self._kills
            if payload.get("was_ammo") is True:
                return self._say("you_killed", "ammo", kills=kills)
            if payload.get("was_fire") is True:
                return self._say("you_killed", "fire", kills=kills)
            if kills >= 2:
                return self._say("you_killed", "streak", kills=kills)
            return self._say("you_killed", "default", kills=kills)
        if event_id == "you_died":
            # 不能顺口说"下一局再打回来"：重生模式（巴比伦这类）死了几秒就回来，
            # 而普通模式死了就是死了。写端这两者都是 mode=dead，所以话术必须中性，
            # 真回来了会有 you_respawned 单独说一句。
            # v2.7 拆表后己方战损属于 SELF 组（允许）：说"我被击毁了"是说自己，
            # 不是敌方情报 —— 旧版因为把两组混成一张表，这句只能退成"被击毁了"。
            return self._say("you_died")
        if event_id == "you_respawned":
            return self._say("you_respawned")
        if event_id == "on_fire":
            return self._say("on_fire")
        if event_id == "fire_out":
            return self._say("fire_out")
        if event_id == "you_damaged":
            seq = event.get("seq")
            health = _as_number(payload.get("health"))
            # 血归零那一下不说：紧接着的 you_died 才是该说的话（v2.0 起的规矩，
            # C5 分档也不许把它抢走）。
            if health is not None and health <= 0:
                return None
            if self.hit_reaction_enabled:
                max_health = _as_number(payload.get("max_health"))
                if max_health is None or max_health <= 0:
                    me = frame.me if isinstance(frame.me, dict) else {}
                    max_health = _as_number(me.get("max_health"))
                # ⛔ 拿不到最大血量 = 算不出“这一下占了几成”，**闭嘴**；
                # 绝不按 0% 处理（那会把一记狠招说成“挨了一下”）。
                if max_health is None or max_health <= 0:
                    return None
                self._rendered_tier[event_id] = ""
                if self._hit_should_merge(now):
                    self._rendered_tier[event_id] = "swarm"
                    return self._say("you_damaged", "swarm")
                tier = self._hit_reaction_tier(_as_number(payload.get("delta")), float(max_health))
                if tier is not None:
                    self._rendered_tier[event_id] = tier
                    return self._say("you_damaged", tier)
            return _render_damage(
                payload,
                seq if isinstance(seq, int) and not isinstance(seq, bool) else 0,
                book=self._phrases,
            )
        if event_id == "you_assisted":
            return _render_assisted(payload, book=self._phrases)
        if event_id == "module_damaged":
            return self._say("module_damaged")
        if event_id == "crew_dead":
            return self._say("crew_dead")
        if event_id == "ammo_low":
            total = payload.get("total")
            if isinstance(total, int) and not isinstance(total, bool):
                return self._say("ammo_low", "with_total", total=total)
            return self._say("ammo_low")
        if event_id == "battle_end":
            return self._render_result(frame, payload)
        # ---- v2.4：mod 早就在发、插件一直没消费的两个事件 ----
        if event_id == "you_hit":
            self._rendered_tier[event_id] = ""
            # v2.20 / P3：断带/断轮优先于方向/跳弹句；`crits_count != 1`、
            # `blocked`、非行走机构、15 秒内已 2 句时 helper 返回 None，退回下面的句。
            track_tier = self._track_tier_for(payload, now)
            if track_tier is not None:
                self._rendered_tier[event_id] = track_tier
                return self._say("you_hit", track_tier)
            if self.hit_reaction_enabled:
                self._rendered_tier[event_id] = ""
                # 连续挨打合并优先于“跳弹/方向”：15 秒内已经 ≥3 下时，
                # 这一下不再单独喊，换一句“连着挨了几下”。
                if self._hit_should_merge(now):
                    self._rendered_tier[event_id] = "swarm"
                    return self._say("you_hit", "swarm")
                if payload.get("blocked") is True:
                    # 跳弹口径：**只说没打穿，一个数字都不带**（you_hit.damage 是面值）。
                    return self._say("you_hit", "blocked_plain")
            return _render_you_hit(payload, book=self._phrases)
        if event_id == "mode_switched":
            return self._render_mode_switched(payload)
        # ---- 派生层（v2.3）：话术要短、像人话，不说"检测到"这种机器腔 ----
        if event_id == "reload_done":
            # v2.7 B4：语义不变（自己的炮准备好了），去掉禁词表上的机械黑话"装填"。
            return self._say("reload_done")
        if event_id == "hp_critical":
            return self._say("hp_critical")
        if event_id == "enemy_killed":
            return _render_team_loss(payload, enemy=True, book=self._phrases)
        if event_id == "ally_killed":
            return _render_team_loss(payload, enemy=False, book=self._phrases)
        if event_id == "fire_cleared":
            return self._say("fire_cleared")
        if event_id == "suppressed":
            return self._say("suppressed")
        # ---- v2.4：整局层面的三个提醒（都不报数字：口径是"已知"，不是"全部"）----
        if event_id == "enemy_low":
            # v2.7 B3：这句话只说**我方处境**，一个敌方数字/敌方状态都不带
            # （v2.4 的"对面没几台了"就把敌方剩余说了出去，虽然只是"已知"）。
            # 判定条件没动：battle + alive + enemy_total > 0 的闸见 ``_derive`` ⑦。
            return self._say("enemy_low")
        if event_id == "team_behind":
            # v2.19 / C6：payload 带 tier 时按档说；老路径/开关关着 → 原默认句。
            tier = payload.get("tier")
            if self.team_behind_tiers_enabled and isinstance(tier, str) and tier in TEAM_BEHIND_TIERS:
                self._rendered_tier["team_behind"] = tier
                return self._say("team_behind", tier)
            self._rendered_tier["team_behind"] = ""
            return self._say("team_behind")
        if event_id == "time_low":
            return self._say("time_low")
        # ---- v1.30：人数差档位（6 档 + 波次/未知降级）----
        if event_id == "team_numbers":
            return _render_team_numbers(payload, book=self._phrases)
        # ---- v2.18 / C4：二阶静默的陪伴句（不依赖任何战况）----
        if event_id == "idle_company":
            return self._say("idle_company")
        return None

    def _render_result(self, frame: Frame, payload: dict[str, Any]) -> str | None:
        """结算话术：数据来源单一化 + 三档胜负 + 战报数字（v1.30）。

        来源顺序：``state.result``（面板快照，本局最近一份）优先，``battle_end.payload``
        （写端在结算那一刻的证词，协议 §5.3 的 15 键）兜底。**两者矛盾以 state 为准**，
        只留一条 DEBUG 日志 —— 不吵，但也不悄悄吞掉矛盾。

        胜负只有四个值（协议 §5.3）：``win`` / ``lose`` / ``draw`` / ``unknown``。
        ``unknown`` **绝不猜**：只说事实（击毁几台、打了多少伤害），输赢一个字不提。
        数字缺哪个就少说哪个 ——「击毁 0 台」和「不知道几台」是两件事。
        """
        result = frame.result if isinstance(frame.result, dict) else {}
        outcome, source = _resolve_outcome(result, payload)
        parts = [self._say("battle_end", "head")]
        if outcome == "draw":
            parts.append(self._say("battle_end", "draw"))
        elif outcome == "win":
            parts.append(self._say("battle_end", "win"))
        elif outcome == "lose":
            parts.append(self._say("battle_end", "lose"))
        else:
            # unknown 档（v1.30 新增）：只交代"打完了"，输赢不下结论。
            parts.append(self._say("battle_end", "unknown"))

        kills = _as_count(result.get("kills"))
        if kills is None:
            kills = _as_count(payload.get("kills"))
        damage = _as_count(result.get("damage"))
        if damage is None:
            damage = _as_count(payload.get("dmg_dealt_total"))
        if kills is not None:
            if damage is not None:
                parts.append(self._say("battle_end", "kills_with_damage", kills=kills, damage=damage))
            else:
                parts.append(self._say("battle_end", "kills_only", kills=kills))
        elif self._kills > 0:
            # 面板和写端都没给击杀数，但我自己数到过 —— 明说"我数到"，不冒充实数。
            parts.append(self._say("battle_end", "counted", kills=self._kills))
        assist = _as_count(result.get("assist"))
        if assist is None:
            assist = _as_count(payload.get("assist"))
        if assist is not None and assist > 0:
            parts.append(self._say("battle_end", "assist", assist=assist))
        # ---- v2.18 / C1 复盘分档 + C3 连败/连胜关怀（同一句结算的收尾）----
        # 分档在 ``_note_history`` 里一次算好（它才拿得到 now 与账本）；这里只取句子。
        # ``recap_enabled=False`` → 回到 v2.17 那句默认文案（这一段一个字都不加）。
        if self.recap_enabled:
            recap = self._recap_ctx.get("tier")
            if recap:
                parts.append(self._say("battle_end", str(recap)))
        care = self._recap_ctx.get("care")
        if care:
            parts.append(self._say("battle_end", str(care)))
        if source == "payload":
            LOGGER.debug("battle_end 胜负取自 payload.result（state.result 判不出）")
        # 表里一片都取不到（用户把 battle_end 整段删了）→ 宁可整句不说，别吐空串。
        return "".join(part for part in parts if part) or None


def _render_team_loss(
    payload: dict[str, Any],
    *,
    enemy: bool,
    book: PhraseBook = PHRASE_BOOK,
) -> str | None:
    """战况计数话术（派生层）：只说 ``n`` 掉几个，**一句都不提还剩几个**。

    为什么砍掉"还剩 N 个"（实机 dry-run）：
    那一局报出"干掉一个，还剩 1 个"，可这局实际是 15v15 —— ``team.enemy_alive``
    只有 1~2，因为 mod 侧的车队计数只覆盖 AoI 内可见车，敌人总数根本不可知；
    雾战下连"我方/敌方总编队数"都不可靠。数字不确定就一个字都别说，这是
     既定的规矩。

    ``left`` 仍然从 payload 里解析（合成事件必须带全，缺了说明这帧数据不对），
    但**故意不进文案**。将来 mod 侧真能给出可靠的编队总数，再把 left 接回话术，
    例如 ``干掉一个，还剩 {left} 个。``。

    ``n`` 必须是正整数才说得出口 —— 不合法就返回 None（宁可不说，不许编数字）。
    """
    left = _as_count(payload.get("left"))
    n = _as_count(payload.get("n"))
    if left is None or n is None or n <= 0:
        return None
    event_id = "enemy_killed" if enemy else "ally_killed"
    if n == 1:
        return book.text(event_id, "one")
    return book.text(event_id, "many", n=n)


def _angle_gap(first: float, second: float) -> float:
    """两个偏航角之间的**最小**夹角（0..180），照顾好 350° 和 10° 只差 20°。"""
    diff = abs(first - second) % 360.0
    return min(diff, 360.0 - diff)


def _directions_spread(hits: list[tuple[float, float | None, float | None]]) -> bool:
    """最近的受击方向是否分散（来弹不在同一条线上）。

    判据是"存在一对夹角 > 60°"：两下都从同一个方向来是磨血，从两侧来才叫被夹。

    **降级路径**：样本里 ``hitDirYaw`` 缺失/不是数字（写端没给这个字段）时，
    就不拿方向当判据，只靠"6 秒内挨够两下 + 掉血 ≥ 10%"两条（即 a + c）。
    宁可判得松一点，也不许自己编一个方向出来。

    样本环最多 8 条，这里是常数级比较，4Hz 里没有 O(n²) 风险。
    """
    yaws = [yaw for _, yaw, _ in hits if yaw is not None]
    if len(yaws) < 2:
        return True  # 降级：没有（成对的）方向数据
    for index, first in enumerate(yaws):
        for second in yaws[index + 1:]:
            if _angle_gap(first, second) > SUPPRESS_SPREAD_DEG:
                return True
    return False


def _max_event_seq(events: tuple[dict[str, Any], ...]) -> int | None:
    """这一帧里最大的合法 ``event.seq``（没有合法 seq 就返回 None，不猜）。"""
    best: int | None = None
    for event in events:
        seq = event.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int):
            continue
        if best is None or seq > best:
            best = seq
    return best


# --------------------------------------------------------- 决策日志的小格式化（v2.15）
def _recap_tier(battle: Battle, average: tuple[float | None, int]) -> str | None:
    """复盘分档（v2.18 / C1）→ ``battle_end`` 的子档名，或 ``None``（= 用默认文案）。

    **判据只许用结算数据**（``battle_end.payload`` 的 ``result/kills/dmg_dealt_total/``
    ``deaths/battle_seconds``，缺字段时允许同名字段从 ``state.result`` 兜底）+ 本机历史，
    优先级严格按五档自上而下（第一条命中就返回）：

    1. ``unknown`` → 不加任何分档句：事实由 ``battle_end.unknown`` + 战报数字负责
       （那一档的文案里**一个输/赢/胜/败字都没有**）；
    2. ``win`` 且 ``kills ≥ 3`` → 夸战果；
    3. ``win`` 且 ``deaths > 0`` → "搭进去一条命也值了"式调侃；
    4. ``lose`` 且本局伤害 > 本车历史均伤（同车样本 ≥ 3 局、本局 ``dmg`` 非空）→ 安慰；
    5. ``lose`` 且时长 < 180 秒 → 短局安慰（**不评价打法**）；
    6. 其余 → ``None``（默认文案）。

    ⛔ 五个分档句都在 ``preach_is_forbidden`` 的闸下面（``_pick`` 统一过闸），
    而且都不许出现"建议/应该/下次你"这类指导口吻。
    """
    if battle.result == "unknown":
        return None
    if battle.result == "win":
        if battle.kills is not None and battle.kills >= RECAP_CARRY_KILLS:
            return "recap_win_carry"
        if battle.deaths > 0:
            return "recap_win_death"
        return None
    if battle.result == "lose":
        avg, samples = average
        if battle.dmg is not None and avg is not None and samples >= RECAP_AVG_MIN_BATTLES and battle.dmg > avg:
            return "recap_lose_above_avg"
        if battle.seconds is not None and battle.seconds < RECAP_SHORT_BATTLE_S:
            return "recap_lose_short"
        return None
    return None


def _brief_ids(events: list[dict[str, Any]]) -> str:
    """``fresh`` / ``derived`` 的候选清单：``[you_hit,you_damaged]``（到达顺序）。"""
    return "[" + ",".join(str(event.get("id")) for event in events) + "]"


def _pick_id(picks: dict[str, tuple[int, str, str]], level: str) -> str:
    """这一档选中了谁（没选中就是 ``None`` —— 日志里要能直接看出来是"没选"）。"""
    found = picks.get(level)
    return "None" if found is None else found[2]


def _allowed_payload(payload: object) -> dict[str, Any]:
    """只放行白名单键的标量/字符串列表值；其余（尤其是敌方字段）直接丢掉。"""
    if not isinstance(payload, dict):
        return {}
    allowed: dict[str, Any] = {}
    for key, value in payload.items():
        if (
            key not in PAYLOAD_ALLOWED_KEYS
            and key not in PAYLOAD_ALLOWED_BOOL_KEYS
            and key not in PAYLOAD_ALLOWED_LIST_KEYS
        ):
            continue
        # 布尔默认一律丢掉（True 当数字 1 用是最经典的静默 bug），只有白名单里
        # 明确标了"这个键就是布尔"的才放行。
        if isinstance(value, bool):
            if key in PAYLOAD_ALLOWED_BOOL_KEYS:
                allowed[key] = value
            continue
        # v2.20：``crits_devices`` 是 list[str]，只收非空字符串；空列表/脏元素丢。
        if key in PAYLOAD_ALLOWED_LIST_KEYS:
            if isinstance(value, list):
                cleaned = [item[:_MAX_TEXT_FIELD] for item in value if isinstance(item, str) and item.strip()]
                if cleaned:
                    allowed[key] = cleaned
            continue
        if not isinstance(value, (str, int, float)):
            continue
        if isinstance(value, str):
            value = value[:_MAX_TEXT_FIELD]
        allowed[key] = value
    return allowed


# 单次伤害到这个量级才配一句"挨了一下狠的"（既有阈值口径：
# 300 太吵，被两门车轮流磨也够 300；500 才真是"一炮闷在脸上"）。
BIG_HIT_DELTA = 500


# ---------------------------------------------------------------- v2.4 新话术
def _hit_sector(yaw_deg: float) -> str:
    """角度 → 八格粗方位（每格 45°，0° 落在"正前方"格的正中）。

    分格用"格心 ±22.5°"：``(angle + 22.5) // 45`` 正好把 337.5°~22.5° 都归到 0 号格，
    所以 359°/1° 都是"正前方"，不会因为绕圈在边界上来回跳。
    """
    angle = (float(yaw_deg) + HIT_SECTOR_OFFSET_DEG) % 360.0
    return HIT_SECTORS[int((angle + 22.5) % 360.0 // 45.0)]


def _walking_device_parts(payload: dict[str, Any]) -> list[str]:
    """``crits_devices`` → 只保留 ``track`` / ``wheel``（§5.1b 的设备名原文）。

    大小写/空格容错（``" Track "`` 也算），但**精确匹配**设备名：gun / turretRotator
    这类非行走机构一个都不收；空列表/缺键 → ``[]``（调用方闭嘴）。
    """
    raw = payload.get("crits_devices")
    if not isinstance(raw, list):
        return []
    parts: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        token = re.sub(r"\s+", "", item).lower()
        if token in TRACK_DEVICE_BITS and token not in parts:
            parts.append(token)
    return parts


def _mech_opening_tier(frame: Frame) -> str | None:
    """``me.mech`` → 开场机制档位；空/认不出来 → ``None``（说通用开场句）。

    ``me.mech`` 是 9 类固定 key 的 list（§4.2 / mod v1.40），多机制车只挑
    :data:`MECH_OPENING_ORDER` 里第一个认识的 key，不拼接、不按车名猜。
    """
    me = frame.me if isinstance(frame.me, dict) else {}
    raw = me.get("mech")
    if not isinstance(raw, list):
        return None
    labels = {item.strip().lower() for item in raw if isinstance(item, str) and item.strip()}
    for key in MECH_OPENING_ORDER:
        if key in labels:
            return MECH_OPENING_TIERS[key]
    return None


def _render_you_hit(payload: dict[str, Any], *, book: PhraseBook = PHRASE_BOOK) -> str | None:
    """受击话术：**只说粗方位**，一格一句。

    合规上这是最敏感的一句 —— 距离、来袭角度、攻击者、弹种一个都不能出现，
    所以这里只读 ``yaw_deg`` 和 ``blocked`` 两个白名单键，其它一概不碰。
    """
    # 跳弹/未击穿（有受击、没掉血）单独一句：这时候说方向没意义，说"跳弹"才有用。
    if payload.get("blocked") is True:
        return book.text("you_hit", "blocked")
    yaw = _as_number(payload.get("yaw_deg"))
    if yaw is None:
        # 写端拿不到方位角时照样发事件（v1.17 协议）：只说"挨了一下"，方向不猜。
        return book.text("you_hit", "no_yaw")
    return book.text("you_hit", "sector", sector=_hit_sector(float(yaw)))


def _render_mode_switched(payload: dict[str, Any], *, book: PhraseBook = PHRASE_BOOK) -> str | None:
    """战斗模式切换（液压悬挂）：开/关各一句。

    ``activated`` 不是真布尔（老版本 mod 没这个键）时不猜方向，只说"切了一下"。
    """
    activated = payload.get("activated")
    if activated is True:
        return book.text("mode_switched", "on")
    if activated is False:
        return book.text("mode_switched", "off")
    return book.text("mode_switched", "unknown")


def _is_duplicate_hit(event: dict[str, Any]) -> bool:
    """``you_hit`` 的 ``dup == 1`` = 同一发炮弹的第二条投递（协议 §5.1）。

    读端义务：**直接丢弃**——不播报、不进受击统计、不参与任何累计。写端已经做了
    250ms 同键去重，这里是双保险：写端哪天真漏了一条，这边也不出声。
    只认 ``dup == 1``（``True`` 也算）：缺键 / 0 / 别的脏值一律当"不是重复"。
    """
    if event.get("id") != "you_hit":
        return False
    payload = event.get("payload")
    return isinstance(payload, dict) and payload.get("dup") == 1


def _outcome_from_state(result: dict[str, Any]) -> str:
    """``state.result`` → ``win`` / ``lose`` / ``draw`` / ``unknown``（不猜）。"""
    won = result.get("won")
    if won is True:
        return "win"
    if won is False:
        return "lose"
    outcome = result.get("outcome")
    if isinstance(outcome, str):
        text = outcome.strip().lower()
        if text in ("win", "lose"):
            return text
    if result.get("draw") is True:
        return "draw"
    return "unknown"


def _outcome_from_payload(value: object) -> str:
    """``battle_end.payload.result``（协议 §5.3 的四值）→ 同一套值。"""
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("win", "lose", "draw", "unknown"):
            return text
    return "unknown"


def _resolve_outcome(result: dict[str, Any], payload: dict[str, Any]) -> tuple[str, str]:
    """→ ``(胜负四值, "state"|"payload")``；矛盾时以 ``state`` 为准并留 DEBUG。

    为什么 state 优先：``state.result`` 是面板钩子在**结算那一刻**的实时读数，
    ``battle_end.payload`` 是写端状态机打包时的拷贝，中间可能已经翻页/换局。
    """
    from_state = _outcome_from_state(result)
    from_payload = _outcome_from_payload(payload.get("result"))
    if from_state == "unknown" and from_payload != "unknown":
        return from_payload, "payload"
    if from_state != "unknown" and from_payload not in ("unknown", from_state):
        LOGGER.debug("battle_end 胜负矛盾：state.result=%s payload.result=%s → 以 state 为准", from_state, from_payload)
    return from_state, "state"


def _is_frontline(mode_kind: object) -> bool:
    """这一帧是不是前线模式（``mode_kind == "frontline"``，mod v1.38）。

    ``mode_kind`` 是**自由字符串**、将来还会加值（协议 §4.2），所以判定只留这一个
    入口，别在别处写 ``== "frontline"`` 或自己再抄一份取值表。认不出来（``None`` /
    老 mod 缺键 / ``standard`` / ``wave`` / 未来的新值）一律 **False** —— 按现状
    处理，绝不把"没见过"当成前线。
    """
    return mode_kind == "frontline"


def _team_behind(team: dict[str, Any]) -> bool | None:
    """我方存活 - 敌方存活 ≤ -3。任一计数读不到 → None（不猜）。"""
    ally = _as_count(team.get("ally_alive"))
    enemy = _as_count(team.get("enemy_alive"))
    if ally is None or enemy is None:
        return None
    return ally - enemy <= TEAM_BEHIND_AT


def _team_behind_tier(team: dict[str, Any]) -> str | None:
    """v2.19 / C6：劣势 → ``mild`` / ``big`` / ``endgame``；不落后或字段缺失 → ``None``。

    ⛔ 任一计数缺失（``ally_alive`` / ``enemy_alive``）→ ``None``：**闭嘴，绝不拿
    别的字段顶**。``endgame`` 先判（我方 ≤2 且对方多 ≥3），否则按差值：
    ≥3 → ``big``，1~2 → ``mild``。
    """
    ally = _as_count(team.get("ally_alive"))
    enemy = _as_count(team.get("enemy_alive"))
    if ally is None or enemy is None:
        return None
    diff = enemy - ally
    if diff <= 0:
        return None
    if 1 <= ally <= TEAM_BEHIND_ENDGAME_ALLY and diff >= TEAM_BEHIND_ENDGAME_DIFF:
        return TEAM_BEHIND_TIER_ENDGAME
    if diff >= NUMBERS_BIG_DIFF:
        return TEAM_BEHIND_TIER_BIG
    return TEAM_BEHIND_TIER_MILD


def _team_behind_severity(tier: str | None) -> int:
    """档位严重度（``None`` = 没落后）。只在“变差”时开口，避免追上来还报坏消息。"""
    return {
        None: 0,
        TEAM_BEHIND_TIER_MILD: 1,
        TEAM_BEHIND_TIER_BIG: 2,
        TEAM_BEHIND_TIER_ENDGAME: 3,
    }.get(tier, 0)


def _same_local_day(ts: float, day: Any) -> bool:
    """时间戳是否落在本地 ``day`` 这一天（C7“今天第几局”用）。坏时间戳 → False。"""
    try:
        return datetime.fromtimestamp(float(ts)).date() == day
    except (OverflowError, OSError, ValueError, TypeError):
        return False


# --------------------------------------------------- 人数差档位（v1.30）
# 「优势/劣势」这一维只用来判"要不要立刻说"（关键翻转），档位本身是 7 个值。
_NUMBERS_SIDES: dict[str, str] = {
    NUMBERS_BIG_ADV: "adv",
    NUMBERS_SMALL_ADV: "adv",
    NUMBERS_EVEN: "even",
    NUMBERS_SMALL_DIS: "dis",
    NUMBERS_BIG_DIS: "dis",
    NUMBERS_WAVE: "blind",
    NUMBERS_UNKNOWN: "blind",
}


def _numbers_side(tier: str) -> str:
    """档位 → "哪一边"（adv / even / dis / blind）。未知档位按 blind。"""
    return _NUMBERS_SIDES.get(tier, "blind")


def _numbers_flip(previous: str, current: str) -> bool:
    """是不是"关键翻转"：优势 ↔ 劣势（局面反转，必须立刻说）。

    同侧换档（+3 → +2）不算翻转 —— 那正是防抖要压的抖动。
    """
    return {_numbers_side(previous), _numbers_side(current)} == {"adv", "dis"}


def _numbers_snapshot(mode_kind: object, team: dict[str, Any]) -> tuple[str, int, int | None] | None:
    """→ ``(档位, 我方存活, 敌方存活或 None)``；连我方人数都读不到 → ``None``。

    ⛔ 敌方数字只在 ``mode_kind == "standard"`` **且** ``enemy_total_known is True``
    时才给（协议 §4 的读端义务）。波次局 / 判不出来的局一律 ``None``，话术走
    ``wave`` / ``unknown`` 两档，只说我自己这边 —— 绝不拿 0 或"最后一帧"顶替。
    """
    ally = _as_count(team.get("ally_alive"))
    if ally is None:
        return None  # 连我方人数都没有：这句话一个字都编不出来
    kind = mode_kind if isinstance(mode_kind, str) else ""
    # 老版本 mod（v1.29 及以前）没有 ``mode_kind``：连"这局敌人能不能数"都不知道，
    # 那就一句话都不说 —— 「人数看不到」本身也是关于这局模式的一句断言（协议 §4.2：
    # 只有 ``unknown`` 才许说"看不到"，缺键不等于 ``unknown``）。
    # ⛔ 前线模式（v1.38 的 ``frontline``）**故意不在**下面这个白名单里：30v30 复活局
    # 的 ``enemy_alive`` / ``enemy_total`` 是显式 null，连"人数看不到"都不该断言 →
    # 整条人数播报静音，绝不改成"报我方 30 人"之类的新话术。别把 frontline 加进来。
    if kind not in ("standard", "wave", "unknown"):
        return None
    if kind == "wave":
        return NUMBERS_WAVE, ally, None
    if kind == "unknown":
        return NUMBERS_UNKNOWN, ally, None
    if team.get("enemy_total_known") is not True:
        return NUMBERS_UNKNOWN, ally, None
    enemy = _as_count(team.get("enemy_alive"))
    if enemy is None:
        return NUMBERS_UNKNOWN, ally, None
    diff = ally - enemy
    if enemy == 0 or diff >= NUMBERS_BIG_DIFF:
        return NUMBERS_BIG_ADV, ally, enemy
    if diff >= 1:
        return NUMBERS_SMALL_ADV, ally, enemy
    if diff == 0:
        return NUMBERS_EVEN, ally, enemy
    if diff > -NUMBERS_BIG_DIFF:
        return NUMBERS_SMALL_DIS, ally, enemy
    return NUMBERS_BIG_DIS, ally, enemy


def _render_team_numbers(payload: dict[str, Any], *, book: PhraseBook = PHRASE_BOOK) -> str | None:
    """人数差话术：按档位取句，**缺数字就一个字都不说**（宁可没有，不许编）。

    这里只读 ``tier`` / ``ally`` / ``enemy`` 三个白名单键；敌方位置、血量、车型
    一个都不在 payload 里（合成事件时就没带）。
    数字档必须两个人数都在，否则退成"看不见"那一档也不对（那是换了个说法编事实）→
    直接返回 ``None``。
    """
    tier = payload.get("tier")
    if not isinstance(tier, str) or tier not in NUMBERS_TIERS:
        return None
    ally = _as_count(payload.get("ally"))
    enemy = _as_count(payload.get("enemy"))
    if tier in (NUMBERS_WAVE, NUMBERS_UNKNOWN):
        if ally is None:
            return None
        return book.text("team_numbers", tier, ally=ally)
    if ally is None or enemy is None:
        return None
    if tier == NUMBERS_BIG_ADV:
        # 敌方清零时不说"还剩 0 个"（听着像没打完）—— 换一句收尾的。
        if enemy == 0:
            return book.text("team_numbers", "cleared")
        return book.text("team_numbers", tier, enemy=enemy)
    if tier == NUMBERS_SMALL_ADV:
        return book.text("team_numbers", tier, diff=ally - enemy)
    if tier == NUMBERS_EVEN:
        return book.text("team_numbers", tier, ally=ally, enemy=enemy)
    if tier == NUMBERS_SMALL_DIS:
        return book.text("team_numbers", tier, diff=enemy - ally)
    return book.text("team_numbers", tier)


def _arena_time_left(frame: Frame) -> float | None:
    """``arena.time_left`` = 显式剩余秒（协议 §2.2 / mod v1.27）。

    返回 ``None`` 的语义是**"看不到计时"**，不是"时间到了"：键不存在（写端没读到
    字段，或 ``>3600`` 的脏帧被写端整帧丢弃）、类型不对（bool / 字符串 / None）
    一律 ``None``，调用方必须因此闭嘴。**绝不回落到 period/periodLength 推算**。
    """
    arena = frame.arena if isinstance(frame.arena, dict) else {}
    value = _as_number(arena.get(ARENA_TIME_LEFT_KEY))
    return None if value is None else float(value)


def _arena_overtime(frame: Frame) -> bool:
    """``arena.overtime`` 为真 = 这一帧的原始剩余秒是负数（加时）。

    只认 ``is True``：老版本 mod 没有这个键、或者写了 ``1`` / ``"true"`` 这类脏值，
    一律当"没有加时标记"（不猜，不把脏值当真）。
    """
    arena = frame.arena if isinstance(frame.arena, dict) else {}
    return arena.get(ARENA_OVERTIME_KEY) is True


def _render_damage(payload: dict[str, Any], event_seq: int = 0, *, book: PhraseBook = PHRASE_BOOK) -> str | None:
    """掉血话术：血量说中文口语（一千八百 / 三百八十），见底了说句俏皮话。

    数值来源优先用写端给的绝对血量（v1.9 起 payload.health），拿不到才退回百分比。
    精度：千位以上只到百位、百位段只到十位 —— 血条一直在抖，念"一千八百三十四"
    既拗口也没用（既有口语习惯：多于一百说 X千X百，低于一百说剩个
    方向盘）。
    """
    health = _as_number(payload.get("health"))
    delta = _as_number(payload.get("delta"))

    if health is not None and health <= 0:
        # 归零那一下不说：紧接着的 you_died 才是该说的话。
        return None
    if health is not None:
        if health < 100:
            # 低血俏皮话是**唯一**按 ``event.seq`` 轮换的话术（v2.0 起的行为）：
            # 换成"局内次数"会改变轮换顺序，等于改了台词。
            return _low_hp_line(book, event_seq)
        spoken = _spoken_health(int(health))
        if spoken:
            if delta is not None and delta >= BIG_HIT_DELTA:
                return book.text("you_damaged", "big_hit", spoken=spoken)
            return book.text("you_damaged", "plain_health", spoken=spoken)
    ratio = payload.get("ratio_after")
    if isinstance(ratio, (int, float)) and not isinstance(ratio, bool):
        # 用 floor 不用 round：96.5% 和 95.7% 四舍五入都会说成 "96%"，
        # 听着像卡了重复（实机就是这么连说两次 96%）。
        percent = max(0, min(100, int(float(ratio) * 100)))
        return book.text("you_damaged", "percent", percent=percent)
    return book.text("you_damaged", "unknown")


def _low_hp_line(book: PhraseBook, event_seq: int) -> str | None:
    """挑一句"血量见底"的俏皮话并按 ``low_hp_wrapped`` 包一层。

    俏皮话那一档被删空时退成 ``unknown``（"掉血了，注意点。"），
    绝不吐出"血量见底了，。"这种半截句子。
    """
    lines = book.lines("you_damaged", "low_hp")
    if not lines:
        return book.text("you_damaged", "unknown")
    return book.text("you_damaged", "low_hp_wrapped", low_hp=lines[int(event_seq) % len(lines)])


# 血量见底时的俏皮话（口语习惯：剩个方向盘 / 螺丝钉）。轮着用，一局不重复。
# v2.8 起正文在 ``phrases.json`` 的 ``you_damaged.low_hp``；这里保留常量是为了
# **内置默认表与用例的对照**（``test_arbiter`` 拿它验轮换），运行期不读它。
LOW_HP_LINES = DEFAULT_PHRASES["you_damaged"]["low_hp"]

_CN_DIGITS = "零一二三四五六七八九"


def _cn_number(value: int) -> str:
    """0..9999 的中文念法（血条够用，不处理"万"）。"""
    if value <= 0:
        return "零"
    if value < 10:
        return _CN_DIGITS[value]
    if value < 20:
        return "十" + (_CN_DIGITS[value % 10] if value % 10 else "")
    if value < 100:
        tens, ones = divmod(value, 10)
        return _CN_DIGITS[tens] + "十" + (_CN_DIGITS[ones] if ones else "")
    if value < 1000:
        hundreds, rest = divmod(value, 100)
        head = _CN_DIGITS[hundreds] + "百"
        if rest == 0:
            return head
        if rest < 10:
            return head + "零" + _CN_DIGITS[rest]
        return head + _cn_number(rest)
    thousands, rest = divmod(value, 1000)
    head = _CN_DIGITS[thousands] + "千"
    if rest == 0:
        return head
    if rest < 100:
        return head + "零" + _cn_number(rest)
    return head + _cn_number(rest)


def _spoken_health(health: int) -> str | None:
    """血量 → 口语（1834 → 一千八百；841 → 八百四十；388 → 三百八十）。"""
    if health < 100:
        return None
    rounded = health // 100 * 100 if health >= 1000 else health // 10 * 10
    if rounded <= 0:
        return None
    return _cn_number(rounded)


# 协助伤害（v2.2）：轻坦主要靠它，所以也该念。写端只在"累计涨了 ≥50"或
# "安静 ≥10s 且确有增长"时才发一条，payload = {total, delta}。
# 这一波帮得狠的阈值：200（= 约一辆八级车的一半血，够格说句"帮得狠"）。
BIG_ASSIST_DELTA = 200


def _render_assisted(payload: dict[str, Any], *, book: PhraseBook = PHRASE_BOOK) -> str | None:
    """协助伤害话术：``协助伤害攒到{spoken}了。``（大额加个前缀）。

    ``total`` 不是非负整数就**一个字都不说**（写端读不到字段本来就不发事件，
    这里再兜一层：宁可没有，不许编数字）。
    取整口径与 ``_spoken_health`` 完全一致（≥1000 到百位、否则到十位），
    不足一百的极小值退回个位数说法（正常不会出现：写端最小上报步进是 50）。
    """
    total = payload.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        return None
    spoken = _spoken_health(int(total))
    if spoken is None:
        if total <= 0:
            return None
        spoken = _cn_number(int(total))
    delta = _as_number(payload.get("delta"))
    if delta is not None and delta >= BIG_ASSIST_DELTA:
        return book.text("you_assisted", "big", spoken=spoken)
    return book.text("you_assisted", "plain", spoken=spoken)


def _fmt(value: float | int) -> str:
    """整数就按整数说，真有小数才带一位（结算伤害数用得上）。"""
    number = float(value)
    if number == int(number):
        return str(int(number))
    return "%.1f" % number


# 内部 ID 形如 ``china:Ch71_WZ_219``：国家前缀 + 编号前缀 + 型号。
# 上游能给出本地化串（``WZ219``）时走 me.vehicle_name，这里只当兜底清洗。
def _display_vehicle(raw: object) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None
    name = raw.strip()
    if ":" in name:  # 去国家命名空间
        name = name.split(":", 1)[1]
    if "_" in name:
        head, tail = name.split("_", 1)
        # ``Ch71`` / ``R45`` / ``GB92`` 这种"字母+数字"是编号前缀，不是型号
        if tail and head[:1].isalpha() and any(ch.isdigit() for ch in head[:3]):
            name = tail
    name = name.replace("_", "-")
    return name[:_MAX_TEXT_FIELD] or None


def _own_vehicle(frame: Frame) -> str | None:
    me = frame.me if isinstance(frame.me, dict) else {}
    for key in ("vehicle_name", "vehicle"):
        value = me.get(key)
        if isinstance(value, str) and value:
            return _display_vehicle(value)
    return None


def _as_number(value: object) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _as_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_ratio(value: object) -> float | None:
    """0..1 的比值字段：不是数字就当没有（字符串 "0.42" 也当没有，不转型不猜）。"""
    number = _as_number(value)
    if number is None:
        return None
    return float(number)
