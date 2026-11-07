"""N.E.K.O 插件：World of Tanks 陪伴（MVP-0）。

链路很短：mod 写 ``state.json`` → 本插件按 4Hz 读 → 仲裁器决定说不说 →
``dry_run`` 时只打日志，关掉才真 ``push_message`` 进对话通道。
台词本身不在代码里：v2.8 起全在 ``data/phrases.json``（每帧 ``reload_if_changed``，
改文件不用重启插件），代码只决定"挑哪一句、占位符填什么"。

合规上只做"翻译屏幕上已有的东西"：读自己车的状态、队伍计数、已发生事件、
结算数据；不碰任何敌方位置/装弹计时/自动操作，也不画任何标记。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping

from plugin.sdk.plugin import NekoPluginBase, Ok, lifecycle, neko_plugin, timer_interval

from .arbiter import (
    DEFAULT_LEVEL,
    GAP_BYPASS,
    LEVEL_CRITICAL,
    LEVEL_INFO,
    LEVEL_PRIORITY,
    LEVEL_WARNING,
    Arbiter,
)
from .history import History
from .phrases import PHRASE_BOOK
from .settings import (
    INFO_CHANNEL_BUBBLE,
    OUTPUT_MODE_BUBBLE,
    OUTPUT_MODE_VERBATIM,
    OUTPUT_MODES,
    SECTION,
    SPEECH_BURST_WINDOW_S,
    Settings,
    load_settings,
)
from .state_reader import STATUS_OK, read_state_file, resolve_state_path

# 定时器最小周期是 1 秒（SDK 只收 int>0），所以 4Hz 采样只能在 tick 内部做：
# 每个 tick 用 poll_interval_s 的节奏连读若干次，读完返回、状态归零。
TICK_SECONDS = 1.0
# ⚠️ 装饰器里那个 id 必须写**字面量**：``neko-plugin check --release`` 的校验器只认
# 字符串常量节点，传模块常量会报 ``@timer_interval ... must declare a non-empty id``
# （实测，这条会直接把打包闸卡成 FAIL）。改名时两处一起改。
POLL_TIMER_ID = "wot_poll"
PUSH_SOURCE = "neko_wot"

# ---- 通道层契约（v2.12，形状照抄战雷 adapters/neko_dispatcher.py）----
# 宿主用 ``coalesce_key`` 顶替"还没说出口的旧战况"，所以整局只用一个 key：
# 新的一句永远替换旧的，而不是排队念一串过时的战报。
BATTLE_EVENT_COALESCE_KEY = "neko_wot:battle_event"
#
# ⚠️ 但**锚点除外**（v2.14 / 实机）：宿主的"顶替"是**无条件**的 ——
# ``main_logic/proactive_delivery.py::submit`` 的合并规则是
# 「同 key 新的一句直接把还没出口的旧句子丢掉」（``dropped = [c for c in self._queue
# if c.coalesce_key == key]``），**priority 根本不参与比较**。
# 整局共用一个 key 时，阵亡那句只要还没轮到出口，一条后来的战况/结算就能把它整条删掉：
# 实机 21:33:10 阵亡（priority=90）提交后 11 秒没出口，21:33:21 结算（priority=90，
# 同一个 key）一到就把它顶掉了 —— 玩家听到的只有结算，"最后的死亡没播报"。
# 所以 ``GAP_BYPASS`` 这两个"一局说完就再也不会出现"的锚点各用一把自己的 key：
# 既不会被别人的话顶掉，也不会把别人的话顶掉；其余战况继续共用一把 key（顶替照旧）。
ANCHOR_COALESCE_PREFIX = "neko_wot:battle_anchor"


def coalesce_key_for(event_id: str | None) -> str:
    """这句话该挂在哪个 coalesce key 上（**唯一实现**，通道层四个出口都从这里取）。

    只有 ``GAP_BYPASS``（``you_died`` / ``battle_end``）拿专属 key；其余（含
    ``event_id`` 为空的手工调用）一律用整局那把 —— 保证"新的一句替换旧战况"这条
    设计不被顺手改掉（见上一条注释）。
    """
    if isinstance(event_id, str) and event_id in GAP_BYPASS:
        return f"{ANCHOR_COALESCE_PREFIX}:{event_id}"
    return BATTLE_EVENT_COALESCE_KEY


# verbatim 的"不改写"全靠把 LLM 的活退化成"复述"：推荐句 + 字数 + 约束一起进 metadata。
# 真实台词基本都在 20 字以内（``data/phrases.json`` 实测），24 是留了一点余量的上限。
REPLY_MAX_CHARS = 24
REPLY_CONTRACT = f"照这句说，别扩写：只复述推荐回复，不追加、不改语气、不超过 {REPLY_MAX_CHARS} 字。"


@neko_plugin
class NekoWotPlugin(NekoPluginBase):
    def __init__(self, ctx):
        super().__init__(ctx)
        self._settings: Settings | None = None
        self._arbiter: Arbiter | None = None
        self._prev_seq: int | None = None
        # 首帧只对齐基线（见 _poll_once）：state.json 里的 events 是"最近 N 条的
        # 滑动窗口"，不是增量。启动那一刻盘上躺着的多半是上一局的历史事件，
        # 一上来就播报等于复述昨天的事—— 实机踩到过。
        self._primed = False

    @lifecycle(id="startup")
    async def startup(self, **_):
        config = await self.config.dump()
        settings = load_settings(config)
        self._settings = settings
        # 回落要吵（v2.12）：``output_mode_invalid`` 只在"键写了但写错"时为真
        # （键缺失不算，那是"没配"），所以这条 WARNING 一出现就代表真有人配错了 ——
        # "我明明写了 llm 它却不出声"只能靠它定位。这里**不抛异常**：配置写错不该让
        # 插件起不来。原始值直接从 dump 出来的 section 里取（Settings 只留解析结果）。
        if settings.output_mode_invalid:
            raw_section = config.get(SECTION) if isinstance(config, Mapping) else None
            raw_mode = raw_section.get("output_mode") if isinstance(raw_section, Mapping) else None
            self.logger.warning(
                "neko_wot 配置 output_mode=%r 不认识，已回落 %s（合法值：%s）",
                raw_mode,
                settings.output_mode,
                "/".join(OUTPUT_MODES),
            )
        # 三个仲裁参数全部来自配置（v2.12）：间隔、打断开关、info 空闲填空闸。
        # 仲裁器本身的 idle_fill_s 默认是 0.0（"不传就不管闲"），生产默认的 10 秒
        # 由配置层 ``settings.DEFAULTS["idle_fill_s"]`` 提供，所以这里必须显式传。
        self._arbiter = Arbiter(
            min_gap_s=settings.min_gap_s,
            interrupt_enabled=settings.interrupt_enabled,
            idle_fill_s=settings.idle_fill_s,
            # v1.30：队伍事件（队友击杀/阵亡）默认静音，配置打开后只按 info 档说。
            team_events_enabled=settings.team_events_enabled,
            # v2.15 / P0：决策日志 + **插件自己的 logger**（只有它落进
            # N.E.K.O_Plugin_neko_wot_*.log；arbiter 的模块 logger 是另一棵树）。
            debug_log=settings.debug_log,
            logger=self.logger,
            # v2.18 / P1 陪伴四件套：账本 / 复盘分档 / 二阶静默 / 话术预算。
            # ``history_enabled=False`` 时**连 History 都不构造** —— 不读不写，一个字节不碰盘
            # （"关掉就是要它安静"，不是"关掉但仍然去 stat 一下"）。
            history=History(logger=self.logger) if settings.history_enabled else None,
            recap_enabled=settings.recap_enabled,
            idle_deep_s=settings.idle_deep_s,
            speech_budget_per_battle=settings.speech_budget_per_battle,
            speech_burst_max=settings.speech_burst_max,
            # v2.19 / P2：C5 受击分档 / C6 劣势细分 / C7 开场人格化。
            hit_reaction_enabled=settings.hit_reaction_enabled,
            hit_reaction_level=settings.hit_reaction_level,
            team_behind_tiers_enabled=settings.team_behind_tiers_enabled,
            battle_start_persona_enabled=settings.battle_start_persona_enabled,
            # v2.20 / P3：断带双层 + 机制分档。
            track_break_enabled=settings.track_break_enabled,
            mech_opening_enabled=settings.mech_opening_enabled,
            mech_switch_enabled=settings.mech_switch_enabled,
        )
        self._prev_seq = None
        self._primed = False
        history_status = "off" if self._arbiter.history is None else str(self._arbiter.history.path)
        self.logger.info(
            "neko_wot startup: dry_run=%s output_mode=%s team_events=%s debug_log=%s "
            "history=%s recap=%s idle_deep=%ss budget=%s/%ss burst=%s "
            "hit_reaction=%s/%s behind_tiers=%s start_persona=%s "
            "track_break=%s mech_open=%s mech_switch=%s poll=%ss gap=%ss stale=%ss state=%s",
            settings.dry_run,
            settings.output_mode,
            # 队伍事件开关必须打出来（v2.14）：它会改变"这一局说不说别人的击杀"，
            # 而 实机 那局 15 条"干掉一个。"只能靠话术反推当时是开还是关 ——
            # 启动日志里却没有这一项。日志里能看见，才不用再事后猜。
            settings.team_events_enabled,
            settings.debug_log,
            # v2.18：账本路径 / 复盘开关 / 静默二阶 / 话术预算，一行全看见（排障口径同 v2.14）。
            history_status,
            settings.recap_enabled,
            settings.idle_deep_s,
            settings.speech_budget_per_battle,
            int(SPEECH_BURST_WINDOW_S),
            settings.speech_burst_max,
            # v2.19：三组新开关同样必须打出来（同一个排障口径）。
            settings.hit_reaction_enabled,
            settings.hit_reaction_level,
            settings.team_behind_tiers_enabled,
            settings.battle_start_persona_enabled,
            # v2.20：断带 / 机制开场 / 机制切换三个开关。
            settings.track_break_enabled,
            settings.mech_opening_enabled,
            settings.mech_switch_enabled,
            settings.poll_interval_s,
            settings.min_gap_s,
            settings.stale_seconds,
            resolve_state_path(settings.state_path),
        )
        # 话术表来源必须打出来：读不到/JSON 坏了时插件会静默退化成内置默认表，
        # 不报一句就等于"玩家觉得台词改不动、又不知道为什么"。
        phrase_status = PHRASE_BOOK.status()
        self.logger.info(
            "neko_wot 话术表：source=%s events=%s path=%s%s",
            phrase_status["source"],
            phrase_status["events"],
            phrase_status["path"],
            "" if phrase_status["error"] is None else f" error={phrase_status['error']}",
        )
        return Ok({"status": "running", "dry_run": settings.dry_run})

    @timer_interval(id="wot_poll", seconds=1, auto_start=True)
    async def wot_poll(self, **_):
        settings = self._settings
        arbiter = self._arbiter
        if settings is None or arbiter is None:
            # 没走完 startup（配置读失败等）就什么都不做，不猜。
            return Ok({"status": "idle"})

        loop = asyncio.get_running_loop()
        deadline = loop.time() + TICK_SECONDS
        reads = 0
        while True:
            self._poll_once(settings, arbiter, time.time())
            reads += 1
            remaining = deadline - loop.time()
            if remaining <= 0.0:
                break
            await asyncio.sleep(min(settings.poll_interval_s, remaining))
        return Ok({"status": "ok", "reads": reads})

    @lifecycle(id="shutdown")
    async def shutdown(self, **_):
        self._settings = None
        self._arbiter = None
        self._prev_seq = None
        self._primed = False
        self.logger.info("neko_wot shutdown")
        return Ok({"status": "stopped"})

    # ------------------------------------------------------------------ 内部
    def _poll_once(self, settings: Settings, arbiter: Arbiter, now: float) -> str | None:
        """采样一帧；有新话要说就发出去。返回说出的文本（测试用）。"""
        # 话术热更新（v2.8）：只 stat 一下 phrases.json 的 mtime+size，变了才重读。
        # 放在每帧的入口处，所以"改文件 → 下一帧生效"（≤ poll_interval_s），不用重启插件。
        PHRASE_BOOK.reload_if_changed()
        frame = read_state_file(
            resolve_state_path(settings.state_path),
            stale_seconds=settings.stale_seconds,
            prev_seq=self._prev_seq,
            now=now,
        )
        if frame.status == STATUS_OK and frame.seq is not None:
            if not self._primed:
                # 首帧只对齐基线：盘上那些事件是"我启动之前就发生的"，一句都不说。
                # （events 是滑动窗口，重启后 _seen 是空的 → 不设基线就把上一局
                # 的阵亡/结算重播一遍。 dry-run 实测确有此现象。）
                self._primed = True
                self._prev_seq = frame.seq
                arbiter.prime(frame)
                self.logger.info(
                    "neko_wot 首帧对齐基线：seq=%s arena=%s 忽略历史事件 %d 条",
                    frame.seq,
                    frame.arena_id,
                    len(frame.events),
                )
                return None
            self._prev_seq = frame.seq
        text = arbiter.observe(frame, now)
        if text:
            # ``observe()`` 的返回值恒等于 ``last_cue.text``（直发 / 插队 / 队列补说
            # 三条出口都走 ``_announce``），所以 level / event_id 照抄它就行，通道层
            # 不自己猜事件。万一没有 cue（理论上到不了），``_speak`` 会退回
            # ``DEFAULT_LEVEL``。
            cue = arbiter.last_cue
            self._speak(
                settings,
                text,
                level=cue.level if cue is not None else None,
                event_id=cue.event_id if cue is not None else None,
            )
        return text

    def _speak(
        self,
        settings: Settings,
        text: str,
        *,
        level: str | None = None,
        event_id: str | None = None,
    ) -> None:
        """把仲裁器选出来的那一句送进宿主通道（v2.12 三模式）。

        ``level`` / ``event_id`` 由 :meth:`_poll_once` 从 ``arbiter.last_cue`` 抄来；
        直接调用（既有用例、手工调试）不传时按 ``DEFAULT_LEVEL`` 处理、``event_id``
        留空串 —— 通道层只照抄，不猜"这句属于哪条事件"。

        * ``verbatim``（默认）→ :meth:`_push_voice` + 完整复述契约 metadata：**要出声**，
          让 LLM 只复述审过的台词；
        * ``bubble`` → :meth:`_push_bubble`：``blind`` + ``["chat"]``，0 token、不发声；
        * ``llm`` → :meth:`_push_voice` 但**不带**复述契约：v2.8 现状，放开让模型说；
        * ``info_channel="bubble"`` 且这一句是 info 档 → 无论 ``output_mode`` 是什么，
          这一句单独降成气泡（机制回执最容易被嫌吵）。
        """
        if settings.dry_run:
            self.logger.info("[dry-run] 本应说出：%s", text)
            return
        cue_level = _cue_level(level)
        priority = _resolve_priority(settings, cue_level)
        coalesce_key = coalesce_key_for(event_id)
        if cue_level == LEVEL_INFO and settings.info_channel == INFO_CHANNEL_BUBBLE:
            self._push_bubble(
                settings, text, level=cue_level, event_id=event_id, priority=priority, coalesce_key=coalesce_key
            )
            return
        if settings.output_mode == OUTPUT_MODE_BUBBLE:
            self._push_bubble(
                settings, text, level=cue_level, event_id=event_id, priority=priority, coalesce_key=coalesce_key
            )
            return
        self._push_voice(
            settings,
            text,
            level=cue_level,
            event_id=event_id,
            priority=priority,
            coalesce_key=coalesce_key,
            # 只有 verbatim 发"只复述别扩写"的契约；llm 模式的语义就是自由发挥
            # （能走到这里的只剩 verbatim / llm 两支，bubble 上面已经返回了；
            #  脏值 settings 层已经回落到 verbatim，不会落到别的字符串上）。
            recommend=settings.output_mode == OUTPUT_MODE_VERBATIM,
        )

    def _push_voice(
        self,
        settings: Settings,
        text: str,
        *,
        level: str,
        event_id: str | None,
        priority: int,
        coalesce_key: str,
        recommend: bool,
    ) -> None:
        """出声通道：``ai_behavior="respond"`` + ``visibility=[]``（verbatim / llm 共用）。

        为什么 verbatim 也走 ``respond``（v2.12 把 v2.9 的结论**反过来**了，理由在这）：
        v2.9 用 ``blind`` + ``["chat"]`` 只求"原文显示"，代价是**永远不出声**；G3 要求
        台词要出声，而插件侧**没有**"直接朗读固定文本"的入口（``direct_reply`` 只有宿主
        ``agent_server`` 能发，bridge 不转发），能出声的就只有 ``respond``。于是 verbatim
        的"不改写"不再靠绕过 LLM，而是靠 **把 LLM 的活退化成复述**：审过的原句
        （``plugin_recommended_reply``）+ 字数上限（``max_reply_chars``）+ 约束文本
        （``reply_contract``）一起进 metadata，宿主照做就逐字。

        ``visibility=[]``：``respond`` 已经会让宿主把这句话说出来，再叠 ``["chat"]``
        会把同一句再渲染成一条 system 气泡 = 玩家听一遍、屏幕再看一遍多余的块。
        ``["chat"]`` 只属于 :meth:`_push_bubble`。
        """
        metadata = self._build_metadata(
            settings,
            text,
            level=level,
            event_id=event_id,
            priority=priority,
            coalesce_key=coalesce_key,
            recommend=recommend,
        )
        # 口径成文版（v2.12 复核，与上面这段理由一致）：README「输出模式」一节
        # 与 README「输出模式」。改通道语义时三处一起改。
        result = self.push_message(
            visibility=[],
            ai_behavior="respond",
            parts=[{"type": "text", "text": text}],
            source=PUSH_SOURCE,
            priority=priority,
            coalesce_key=coalesce_key,
            metadata=metadata,
        )
        self._log_push_result(result)

    def _push_bubble(
        self,
        settings: Settings,
        text: str,
        *,
        level: str,
        event_id: str | None,
        priority: int,
        coalesce_key: str,
    ) -> None:
        """气泡通道：``blind`` + ``visibility=["chat"]``（v2.9 verbatim 的原路）。

        bridge 把 ``blind`` 映射成 ``delivery_mode=silent``（宿主还有一道
        ``ai_behavior=="blind"`` 硬闸兜底）：不建 callback、不起 turn = 0 token；
        ``"chat"`` 让 parts 原文变成一条 ``role='system'`` 气泡。**不发声**。

        两个入口共用这一个落点：``output_mode="bubble"``（整档气泡）与
        ``info_channel="bubble"`` 且这一句是 info 档（只降 info）。
        ``priority`` / ``coalesce_key`` / ``metadata`` 一样不少 —— metadata 不花钱，
        少一条分支就少一个"为什么气泡不顶替旧气泡"的坑。
        """
        metadata = self._build_metadata(
            settings,
            text,
            level=level,
            event_id=event_id,
            priority=priority,
            coalesce_key=coalesce_key,
            recommend=True,
        )
        result = self.push_message(
            visibility=["chat"],
            ai_behavior="blind",
            parts=[{"type": "text", "text": text}],
            source=PUSH_SOURCE,
            priority=priority,
            coalesce_key=coalesce_key,
            metadata=metadata,
        )
        self._log_push_result(result)

    def _build_metadata(
        self,
        settings: Settings,
        text: str,
        *,
        level: str,
        event_id: str | None,
        priority: int,
        coalesce_key: str,
        recommend: bool,
    ) -> dict[str, object]:
        """metadata 通道（形状照抄战雷 ``neko_dispatcher.py`` 的 1100~1120 行）。

        公共键三档全带：宿主靠 ``coalesce_key`` 顶替没出口的旧战况、靠 ``priority``
        数值决定谁先出口 —— 少了它们，气泡和出声两条路就会各排各的队。
        ``coalesce_key`` **必须和 kwargs 那条通道同值**（锚点用专属 key，见
        :func:`coalesce_key_for`）：两条通道各带一把 key 时，宿主按哪个排队都说不清。
        ``event_id`` 逐句带真值：有 ``last_cue`` 就照抄，没有就**留空串**（"不知道是
        哪条事件"比瞎填一个 id 安全，宿主/日志按空串处理）。
        """
        metadata: dict[str, object] = {
            "plugin": PUSH_SOURCE,
            "event_id": event_id or "",
            "level": level,
            "priority": priority,
            "coalesce_key": coalesce_key,
            "replace_pending": True,
            # 兼容位：宿主当前**没有**消费点。真正生效的是 coalesce_key + priority；
            # 照抄战雷的形状只是为了让宿主将来接的时候两边字段对得上。
            "interrupt_pending": level == LEVEL_CRITICAL and settings.interrupt_enabled,
        }
        if recommend:
            metadata.update(
                {
                    "plugin_recommended_reply": text,
                    "max_reply_chars": REPLY_MAX_CHARS,
                    "reply_max_chars": REPLY_MAX_CHARS,
                    "reply_contract": REPLY_CONTRACT,
                    "battle_reply_contract": REPLY_CONTRACT,
                    "live_reply_contract": REPLY_CONTRACT,
                }
            )
        return metadata

    def _log_push_result(self, result: object) -> None:
        if isinstance(result, dict) and result.get("submitted") is False:
            self.logger.info("push_message rejected: %s", result.get("reason"))


def _cue_level(level: str | None) -> str:
    """把外部传进来的档位收口成三档之一（未登记 / ``None`` → ``DEFAULT_LEVEL``）。"""
    return level if isinstance(level, str) and level in LEVEL_PRIORITY else DEFAULT_LEVEL


def _resolve_priority(settings: Settings, level: str) -> int:
    """档位 → 通道层 priority（**只从** ``LEVEL_PRIORITY`` 取，不在这里写死 90/50/10）。

    ``interrupt_enabled=False`` 时 critical 降到 warning 的 50 —— 这是"关掉打断"的
    **唯一**实现方式：宿主只看这个数字，别指望 ``interrupt_pending``。
    """
    if level == LEVEL_CRITICAL and not settings.interrupt_enabled:
        return LEVEL_PRIORITY[LEVEL_WARNING]
    return LEVEL_PRIORITY[level]
