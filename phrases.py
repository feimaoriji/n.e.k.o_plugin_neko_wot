"""话术表（v2.8）：所有台词从 ``phrases.json`` 读，代码只留"挑哪一句"。

三层结构：

1. :data:`DEFAULT_PHRASES` —— **内置默认表**。它是"插件单独拷走/文件丢了/JSON 被
   改成半截"时的兜底，内容与仓库里的 ``data/phrases.json`` **逐字相同**（
   ``tests/test_phrases_table.py`` 用一条用例锁死这个相等关系）。
2. :class:`PhraseBook` —— 读盘、校验、热更新、按 ``event_id / tier`` 取句。
   读不到或结构不对时**退化为内置默认表并告警，绝不抛异常**（规矩同 ``settings.py``：
   配置是用户手写的，什么脏值都可能有）。
3. :func:`render_line` —— 只填**认识的**占位符，其余原样保留。

## 占位符为什么"未知就原样保留"

* ``{MASTER_NAME}`` 是**宿主**在注入边界展开的（见 ``arbiter`` 模块 docstring 与
  README「输出模式」），本插件一个字都不能替换它 —— 在渲染这一层，
  "宿主的占位符"和"用户打错的名字"是**分不开的**；
* 所以两种选择里只能选"原样保留"：``str.format`` 式的 ``KeyError`` 会让这一帧
  整句消失（甚至抛进 tick），而"原样保留"最坏只是把那串花括号显示出来，可读、
  可排障、不影响别的话术；
* 副作用是**不支持 format spec**（``{kills:>3}`` 不会被替换）—— 话术是给人听/
  看的短句，用不上对齐/补零；真要用就自己算好再塞进来。

## 分档（tier）

``events[event_id][tier] = [句子, 句子, ...]``。``tier`` 是**同一事件的不同情形**
（例如 ``you_hit`` 的 ``blocked`` / ``no_yaw`` / ``sector``）；同一个 tier 里放多句
就是**变体**，由 ``Arbiter`` 用"局内已说次数"或"event.seq"当种子取
``lines[seed % len(lines)]`` —— 同输入同输出，可测、不随机。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Mapping

LOGGER = logging.getLogger("neko_wot.phrases")

PHRASES_FILENAME = "phrases.json"
# 话术表放在插件包的 ``data/`` 子目录里（v2.8.1 起，发布包/市场包都带着它）：
# ``plugin/plugins/neko_wot/data/phrases.json``。
# 路径**一律由 ``__file__`` 推出来**，不读环境变量、不依赖进程 CWD —— 插件被宿主
# 从任意工作目录加载时，`os.getcwd()` 与插件目录通常毫无关系。
DATA_DIRNAME = "data"
PLUGIN_DIR = Path(__file__).resolve().parent
PHRASES_PATH = PLUGIN_DIR / DATA_DIRNAME / PHRASES_FILENAME
TABLE_VERSION = 1

# ----------------------------------------------------------------- 内置默认表
# ⚠️ 改这里的任何一句都等于**改话术**：必须同时改 ``phrases.json``，并重新生成
# ``tests/fixtures/phrases_render_v27.json`` 之外的"新快照"（话术变更是有意的产品
# 动作，要走版本号 + README 版本记录，不能顺手改）。
DEFAULT_PHRASES: dict[str, dict[str, tuple[str, ...]]] = {
    "battle_start": {
        "with_vehicle": ("开打了！{MASTER_NAME}，{vehicle} 上场，稳住。",),
        "default": ("开打了！{MASTER_NAME}，稳住。",),
        # ---- v2.19 / C7：开场人格化（有可信账本时**替换**上面那句，不额外加句）----
        # 都不许是问句（会打断操作），也不许出现“今天状态不错吧？”这类要回答的话。
        "day": ("今天第 {today} 局了。",),
        "day_vehicle": ("今天第 {today} 局，{vehicle} 又上了。",),
        "vehicle_average": ("这台车均伤 {avg}，今天第 {today} 局。",),
        "vehicle_battles": ("这台车 {battles} 局，今天第 {today} 局。",),
        # ---- v2.20 / P3：机制自述（me.mech 的 9 个固定 key；C7 优先级更高）----
        "mech_twin_gun": ("双管车，记得能齐射。",),
        "mech_twin_gun_switch": ("双管车，两种打法都能切。",),
        "mech_rocket": ("火箭车，助推在。",),
        "mech_hydro": ("液气悬挂，车身能压。",),
        "mech_susp_auto": ("自动液压，车身自己压。",),
        "mech_wheeled": ("轮式车，跑得快。",),
        "mech_drum": ("弹鼓车，一梭子很猛。",),
        "mech_autoreload": ("弹会自己回来。",),
        "mech_auto_cannon": ("机关炮车，突突突。",),
    },
    "you_killed": {
        # v1.30：分级档（单杀 / 连杀 / 殉爆 / 烧穿）。
        # ``default`` 是"这一局第一个"，逐字未改（v2.7 起就是这句）。
        "default": ("击毁一台。{MASTER_NAME}，这一局第 {kills} 个。",),
        "streak": ("又干掉一台，第 {kills} 个！",),
        # 死因专属：只在 ``was_ammo`` / ``was_fire`` **确为 true** 时才说。
        # ⚠️ 本轮写端恒给 null（协议 §5.2），所以这两句暂时不会出现 —— 留着是为了
        # 等真机拿到来源时不用再动代码。
        "ammo": ("弹药架殉爆了，第 {kills} 个！",),
        "fire": ("烧穿了，第 {kills} 个！",),
    },
    "you_died": {
        "default": ("我被击毁了…{MASTER_NAME}，缓一下。",),
    },
    "you_respawned": {
        "default": ("又活了！{MASTER_NAME}，接着干。",),
    },
    "on_fire": {
        "default": ("着火了！{MASTER_NAME}，快处理。",),
    },
    "fire_out": {
        "default": ("火灭了，缓一口气。",),
    },
    "you_damaged": {
        # 低血俏皮话：**唯一**按 event.seq 轮换的话术（v2.0 起的行为），
        # 顺序就是轮换顺序 —— 调换顺序等于改台词。
        # ⚠️ 这四句是维护者特意保留的（已当面确认）：**故意保留**，
        # 别当成 bug 去"修"成正经话、也别顺手删/换；要换台词先问维护者。
        "low_hp": (
            "就剩个方向盘了",
            "剩颗螺丝钉了",
            "只剩一副履带了",
            "剩个喇叭了",
        ),
        "low_hp_wrapped": ("血量见底了，{low_hp}。",),
        "big_hit": ("挨了一下狠的，还有{spoken}血。",),
        "plain_health": ("还有{spoken}血。",),
        "percent": ("掉血了，还剩 {percent}% 的血。",),
        "unknown": ("掉血了，注意点。",),
        # ---- v2.19 / C5：按 `delta / max_health` 分档（≥25% 重 / ≥50% critical）----
        # 两句都**不带任何数字**（短、不端着），也不评价战术；quiet 档只用 critical。
        "heavy": ("这一下不轻。",),
        "critical": ("这一下够呛。",),
        # 15 秒内 ≥3 下合并成这一句（不逐发喊）。它不计“重/轻”，只是一句陪。
        "swarm": ("连着挨了几下，稳住。",),
    },
    "you_assisted": {
        "big": ("这一波帮得狠，协助伤害攒到{spoken}了。",),
        "plain": ("协助伤害攒到{spoken}了。",),
    },
    "module_damaged": {
        "default": ("有模块被打坏了。",),
    },
    "crew_dead": {
        "default": ("车组有人阵亡了。",),
    },
    "ammo_low": {
        "with_total": ("弹药只剩 {total} 发了。",),
        "default": ("弹药不多了。",),
    },
    "battle_end": {
        "head": ("这一局结束了，{MASTER_NAME}。",),
        "draw": ("打平了。",),
        "win": ("赢了！",),
        "lose": ("输了，下次再来。",),
        # v1.30：结算永远给不出胜负时（``result == "unknown"``）只说事实，
        # 一个字都不猜输赢（协议 §5.3"读端不许猜"）。
        # ⚠️ v2.18 / C1：这一档改写过一次 —— 原文是"打完了，胜负没看清。"，
        # 现在**连"胜/负"两个字都不许出现**（unknown 复盘的硬判据：输/赢/胜/败一个都不许有）。
        "unknown": ("打完了，结果没看清。",),
        "kills_with_damage": ("击毁 {kills} 台，打了 {damage} 伤害。",),
        "kills_only": ("击毁 {kills} 台。",),
        "counted": ("我数到击毁 {kills} 台。",),
        # 有协助伤害才带（面板上本人可见）。
        "assist": ("协助伤害 {assist}。",),
        # ---- v2.18 / P1（C1 复盘分档 + C3 连败/连胜关怀）----
        # 这一档的句子是**结算句的收尾**（head + 胜负 + 战报数字之后），一局最多两句：
        # 一句复盘分档、一句关怀。⛔ 不许出现"建议/应该/下次你"（那是教练不是陪伴），
        # ⛔ 不许出现敌方情报，⛔ 不许评价具体打法。
        "recap_win_carry": ("手感在线。",),
        "recap_win_death": ("搭进去一条命也值了。",),
        "recap_lose_above_avg": ("伤害比平时高，别往心里去。",),
        "recap_lose_short": ("这局太快了，不怪你。",),
        "care_lose": ("三局了，手是不是有点僵。",),
        "care_win": ("连着三局，这状态可以啊。",),
    },
    "you_hit": {
        "blocked": ("跳弹了。",),
        "no_yaw": ("挨了一下。",),
        "sector": ("{sector}挨了一下。",),
        # ---- v2.19 / C5 ----
        # 跳弹口径：只说“没打穿”，⛔ 一个数字都不带（you_hit.damage 是面值不是掉血）。
        "blocked_plain": ("跳弹了，没打穿。",),
        # 连续挨打合并（与 you_damaged.swarm 同一件事，按事件 id 各取一句）。
        "swarm": ("连着挨了几下，稳住。",),
        # ---- v2.20 / P3：断带 / 断轮双层（设备名 track/wheel，见 §5.1b）----
        "track_critical": ("履带伤了。",),
        "track_destroyed": ("履带断了。",),
        "wheel_critical": ("轮子伤了。",),
        "wheel_destroyed": ("轮子断了。",),
    },
    "mode_switched": {
        "on": ("战斗模式开了。",),
        "off": ("切回普通了。",),
        "unknown": ("模式切换了一下。",),
        # ---- v2.20 / P3：按 mode_switched.mech 分档（五个已知值 × 切入/切回）----
        # ⛔ twinGun / dualGun 只描述“切换模式”这件事，**不许说齐射**
        # （协议没有 canMakeDualShot，那是下一单 mod 的事）。
        "twin_gun_on": ("双管模式切过来了。",),
        "twin_gun_off": ("切回单发了。",),
        "dual_gun_on": ("双管模式切过来了。",),
        "dual_gun_off": ("切回单发了。",),
        "siege_on": ("攻城模式展开。",),
        "siege_off": ("攻城模式收起。",),
        "autocannon_on": ("机关炮模式开了。",),
        "autocannon_off": ("机关炮模式收了。",),
        "rocket_on": ("火箭助推开了。",),
        "rocket_off": ("助推收了。",),
    },
    "reload_done": {
        "default": ("炮弹上膛了，{MASTER_NAME}。",),
    },
    "hp_critical": {
        "default": ("血不多了，{MASTER_NAME}，收着点打。",),
    },
    "enemy_killed": {
        "one": ("干掉一个。",),
        "many": ("一口气干掉 {n} 个。",),
    },
    "ally_killed": {
        "one": ("我们这边少了一个。",),
        "many": ("我们少了 {n} 个。",),
    },
    "fire_cleared": {
        "default": ("火灭了，缓口气。",),
    },
    "suppressed": {
        "default": ("被盯上了，{MASTER_NAME}，先躲一下。",),
    },
    "enemy_low": {
        "default": ("我们这边快收尾了，别放跑，{MASTER_NAME}。",),
    },
    "time_low": {
        "default": ("时间不多了。",),
    },
    "team_behind": {
        "default": ("我们这边落后了，稳住。",),
        # ---- v2.19 / C6：劣势/残局细分（每档一局只一次）----
        # ⛔ 都不许劝“换线/绕后/该怎么办”；endgame 只陪、不评价战术。
        "mild": ("我们这边少一两个，还早。",),
        "big": ("人差得有点多，慢慢打。",),
        "endgame": ("就剩咱们几个了，我陪着。",),
    },
    # ---- v1.30：人数差话术矩阵（6 档 + 波次/未知降级）----
    #
    # ⚠️ "敌我人数"是**唯一**允许直接说出敌方数字的地方（既有口径：双方人数
    # 在记分板上直接可见）。仍然禁止：血量百分比、位置/方位/距离/车型。
    # 档位名与 ``arbiter.NUMBERS_*`` 常量一一对应，改名字等于改代码。
    "team_numbers": {
        # 大优势：敌方全清 / 差 ≥3。清场了就不说"还剩 0 个"（听着像没打完）。
        "big_advantage": ("收尾了，敌方还剩 {enemy} 个。",),
        "cleared": ("都清完了，收尾吧。",),
        "small_advantage": ("多 {diff} 个，压上去。",),
        "even": ("势均力敌，{ally} 对 {enemy}。",),
        "small_disadvantage": ("少 {diff} 个，稳一稳，等支援。",),
        "big_disadvantage": ("人差太多了，收缩防守。",),
        # 波次模式（巴比伦这类）：敌人无限补，只说我方存活 + 波次语境。
        "wave": ("这局一直补人，我方还剩 {ally} 个，别恋战。",),
        # 判不出这局敌人能不能数：连"波次"都不说，只说我自己这边。
        "unknown": ("人数看不到，我方还剩 {ally} 个，稳着打。",),
    },
    # ---- v2.18 / C4：二阶静默（战斗里长时间没说话时的一句"我在"）----
    # ⛔ 不依赖任何战况、不评价战局、不提敌方；一局最多一次（靠 `_deep_used` 锁）。
    "idle_company": {
        "default": ("我在呢，慢慢来。",),
    },
}

# 只认 ``{名字}`` 这种最朴素的占位符：``{0}`` / ``{kills:>3}`` 一律不动（见模块
# docstring"不支持 format spec"）。``MASTER_NAME`` 因此天然"认不出来"→ 原样保留。
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def render_line(line: str, fields: Mapping[str, Any] | None = None) -> str:
    """把 ``{key}`` 换成 ``fields[key]``；不认识的键、``None`` 值一律原样保留。"""
    values = fields or {}

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            return match.group(0)
        value = values[key]
        if value is None:
            return match.group(0)
        return str(value)

    return _PLACEHOLDER.sub(_replace, line)


class PhraseBook:
    """一层薄薄的"话术表"：读盘 + 校验 + 热更新 + 取句。永不抛异常。"""

    def __init__(self, path: str | Path | None = None, *, autoload: bool = True) -> None:
        self.path = Path(path) if path is not None else PHRASES_PATH
        self._table: dict[str, dict[str, tuple[str, ...]]] = DEFAULT_PHRASES
        self._source = "builtin"
        self.error: str | None = None
        # (mtime_ns, size)：热更新只看这两个数，比内容哈希便宜得多（4Hz 也不心疼）。
        self._stamp: tuple[int, int] | None = None
        if autoload:
            self.reload(force=True)

    # ------------------------------------------------------------ 加载/热更新
    def reload(self, *, force: bool = False) -> bool:
        """读盘 → 校验 → 换表。文件没变（同 mtime/size）且 ``force=False`` 时不动。

        返回 ``True`` 表示**这次真的换了表**（用例靠它判断热更新确实发生了）。
        """
        try:
            stat = self.path.stat()
        except OSError as error:
            self._stamp = None
            self._fall_back(f"读不到话术表 {self.path.name}：{error}")
            return False

        stamp = (stat.st_mtime_ns, stat.st_size)
        if not force and stamp == self._stamp:
            return False
        self._stamp = stamp

        try:
            raw = self.path.read_bytes()
        except OSError as error:
            self._fall_back(f"话术表读不进来 {self.path.name}：{error}")
            return False

        if raw.startswith(b"\xef\xbb\xbf"):
            # 带 BOM 也能读（``utf-8-sig``），但编辑器一多就容易长出 BOM —— 记一句。
            LOGGER.warning("neko_wot 话术表带 BOM（%s），已容忍；建议存成无 BOM 的 UTF-8", self.path.name)
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            self._fall_back(f"{self.path.name} 不是合法 UTF-8 JSON：{error}")
            return False

        table = _normalize_table(data)
        if table is None:
            self._fall_back(f'{self.path.name} 结构不对（需要 {{"events": {{事件: {{档: [句子]}}}}}}）')
            return False

        self._table = table
        self._source = "file"
        self.error = None
        return True

    def reload_if_changed(self) -> bool:
        """热更新入口：每帧/每 tick 调一次，只 stat 一下，变没变由 mtime+size 判。"""
        return self.reload(force=False)

    # ------------------------------------------------------------------ 取句
    def lines(self, event_id: str, tier: str = "default") -> tuple[str, ...]:
        """取某个事件某一档的全部句子；文件里没有就退回内置默认，再没有就空表。"""
        found = self._lookup(self._table, event_id, tier)
        if found:
            return found
        return self._lookup(DEFAULT_PHRASES, event_id, tier)

    def tiers(self, event_id: str) -> tuple[str, ...]:
        """某个事件有哪些档位（外部表优先，缺了补内置默认）。排障/用例用。"""
        found: list[str] = list(self._table.get(event_id, {}))
        for tier in DEFAULT_PHRASES.get(event_id, {}):
            if tier not in found:
                found.append(tier)
        return tuple(found)

    def text(
        self,
        event_id: str,
        tier: str = "default",
        *,
        seed: int = 0,
        **fields: Any,
    ) -> str | None:
        """取一句并填占位符；这一档没有句子 → ``None``（宁可不说，不许编）。"""
        lines = self.lines(event_id, tier)
        if not lines:
            return None
        return render_line(lines[int(seed) % len(lines)], fields)

    def status(self) -> dict[str, Any]:
        """给启动日志/排障用：现在用的是文件还是内置默认、有多少个事件。"""
        return {
            "source": self._source,
            "path": str(self.path),
            "events": len(self._table),
            "error": self.error,
        }

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _lookup(
        table: Mapping[str, Mapping[str, tuple[str, ...]]],
        event_id: str,
        tier: str,
    ) -> tuple[str, ...]:
        tiers = table.get(event_id)
        if not isinstance(tiers, Mapping):
            return ()
        lines = tiers.get(tier)
        return lines if isinstance(lines, tuple) else ()

    def _fall_back(self, message: str) -> None:
        """退化到内置默认表。**同一句错误只告警一次**（4Hz 下别刷屏）。"""
        if message != self.error:
            LOGGER.warning("neko_wot 话术表回退到内置默认：%s", message)
        self.error = message
        self._source = "builtin"
        self._table = DEFAULT_PHRASES


def _normalize_table(raw: object) -> dict[str, dict[str, tuple[str, ...]]] | None:
    """校验并规整外部表：``{"version": 1, "events": {事件: {档: [句子]}}}``。

    容错口径：结构骨架不对（不是 dict / 没有 ``events`` / 一句可用的话都没有）→
    ``None``（调用方回退内置默认）；单条坏句子（空串、非字符串）只丢那一句，
    不牵连整张表 —— 用户手改时"多打一个逗号"不该让插件变哑巴。
    """
    if not isinstance(raw, dict):
        return None
    events = raw.get("events")
    if not isinstance(events, dict):
        return None

    table: dict[str, dict[str, tuple[str, ...]]] = {}
    for event_id, tiers in events.items():
        if not isinstance(event_id, str) or not isinstance(tiers, dict):
            continue
        clean: dict[str, tuple[str, ...]] = {}
        for tier, lines in tiers.items():
            if not isinstance(tier, str) or not isinstance(lines, list):
                continue
            texts = tuple(line for line in lines if isinstance(line, str) and line.strip())
            if texts:
                clean[tier] = texts
        if clean:
            table[event_id] = clean
    return table or None


# 模块级单例：生产路径用它（``arbiter`` 默认取这张表，tick 每帧调
# ``reload_if_changed`` 就能热更新）；用例可以自己 ``PhraseBook(path=...)`` 造临时表。
PHRASE_BOOK = PhraseBook()
