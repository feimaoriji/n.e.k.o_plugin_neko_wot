# World of Tanks Copilot（`neko_wot`）

> 一个把"自己这一侧、屏幕上本来就看得见的事实"说成一句中文陪伴文本的插件。
> 不做位置情报，不做战术建议，不碰游戏进程。

版本 **v0.1.0** · 许可证 [Apache-2.0](LICENSE) · 协议契约 [`docs/PROTOCOL.md`](docs/PROTOCOL.md)

---

## 中文

### 这是什么

`neko_wot` 是一个运行在插件宿主里的读端插件。它按 0.25 秒的节奏读取本机一个
JSON 文件（`state.json`），由仲裁器决定"这一帧要不要说话、说什么"，然后把一句中文
陪伴文本交给宿主通道。

一句话：**只读文件、只说话。**

### 数据边界（合规自述）

| 做 | 不做 |
| --- | --- |
| 读本机 `state.json`（只读打开） | 读写游戏进程内存、注入 DLL、hook 渲染 |
| 读自己车况、双方存活计数、已发生事件、结算数据 | 读任何敌方单位的位置/朝向/装弹/血量 |
| 输出中文自然语言（含 `{MASTER_NAME}` 占位符） | 输出坐标、距离、方位、预判、"建议打谁" |
| 事件去重 + 冷却 + 全局最小间隔 | 自动开火/自动瞄准/自动走位/任何输入注入 |
| 默认 `dry_run = true` | 联网、上传、遥测 |

- 话术模板里**不允许**出现敌方信息（敌方单位、位置、坐标、方位、预测、瞄准、
  弱点、装填等）。这张表在 `arbiter.FORBIDDEN_WORD_GROUPS["ENEMY"]`，**只有一处**。
- 己方说法**允许**（"我方"、"我们这边少了一个"、"我被击毁了"）——说自己战损不算泄密。
- 事件 payload 只放行白名单键（见 [`docs/PROTOCOL.md`](docs/PROTOCOL.md) §7.1），
  其余字段一律丢弃，不会进入播报文本。
- 不做任何画面标记或叠加层：这个插件没有 UI，也不碰渲染。

### 它怎么工作

1. 写端（一个独立的、闭源的配套组件）按约 4 Hz 把本机状态写进
   `%LOCALAPPDATA%\WoT_Copilot\state.json`；
2. 本插件按 `poll_interval_s` 采样，按 `seq` 判新帧、按 `ts` 判陈旧、容忍写一半；
3. 仲裁器把"这一帧发生的事"整理成候选，按档位与档内优先级挑**一条**；
4. 渲染出来的句子必须过禁词闸，然后经宿主通道送出；
5. `dry_run = true` 时只写日志，什么都不推。

插件**不定义协议，只消费协议** —— 见 [`docs/PROTOCOL.md`](docs/PROTOCOL.md)。

### 安装

- Python 3.11+
- 运行时**零第三方依赖**（只用标准库）
- 把本目录放进宿主的插件目录，`plugin.toml` 里声明的入口是
  `plugin.plugins.neko_wot:NekoWotPlugin`

### 配置

配置段是 `[neko_wot]`，完整示例见 [`config.example.toml`](config.example.toml)。常用键：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `dry_run` | `true` | 只打 `[dry-run]` 日志，不推送 |
| `state_path` | `""` | 空 = 走协议默认路径 |
| `poll_interval_s` | `0.25` | tick（1 s）内的采样间隔 |
| `stale_seconds` | `5.0` | `ts` 超过这个时间就当"游戏没在跑" |
| `min_gap_s` | `2.0` | 两句播报之间的全局最小间隔（被拦下的进排队，最多 3 条、时效 4 秒） |
| `output_mode` | `verbatim` | 见下节 |
| `interrupt_enabled` | `true` | `critical` 档是否无视 `min_gap_s` 直发 |
| `idle_fill_s` | `10.0` | 空闲多少秒后允许 `info` 档补一句；`0` = 不做这道闸 |
| `info_channel` | `voice` | `info` 档走 `voice`（出声）还是 `bubble`（只弹气泡） |
| `team_events_enabled` | `false` | 队友击杀/队友阵亡要不要出声（打开后也只占 `info` 档） |
| `debug_log` | `true` | 决策日志：每帧的候选/选中/丢弃原因/说出口 |
| `history_enabled` | `true` | 本地战绩账本；`false` = 不写不读，一个字节都不落盘 |
| `recap_enabled` | `true` | 结算复盘分档；`false` = 回到通用结算文案 |
| `idle_deep_s` | `60.0` | 二阶静默阈值（秒）；`0` = 关 |
| `speech_budget_per_battle` | `40` | 单局话术上限；`0` = 不限 |
| `speech_burst_max` | `3` | 任意 30 秒内最多几句 |
| `hit_reaction_enabled` | `true` | 受击反应分档 |
| `hit_reaction_level` | `quiet` | `quiet` / `full`（阈值 25%/50% → 15%/35%） |
| `team_behind_tiers_enabled` | `true` | 劣势细分（`mild` / `big` / `endgame`） |
| `battle_start_persona_enabled` | `true` | 开场人格化（用本机账本说"今天第 N 局"） |
| `track_break_enabled` | `true` | 断带/断轮双层播报 |
| `mech_opening_enabled` | `true` | 载具机制开场自述 |
| `mech_switch_enabled` | `true` | 载具机制切换分档 |

写错、留空、类型不对一律回落到默认值，**不会让插件起不来**；回落时启动日志会打一条
`WARNING`（键整个缺失算"没配"，不告警）。

### 输出模式

`output_mode` 只决定"一句已经审过的台词怎么送出去"，不影响说什么。

| 取值 | 玩家看到/听到 | LLM 干的活 | token |
| --- | --- | --- | --- |
| `verbatim`（默认） | AI 的回复（就是那句台词被复述出来），屏幕 + 语音 | **只复述**：照推荐句说，≤ 24 字，不扩写 | 1 次生成 |
| `bubble` | 原文变成一条 `system` 气泡 | 没有 LLM | **0**（不建 callback、不起 turn、不碰 TTS） |
| `llm` | AI 自己的回复 | 自由发挥 | 1 次生成 |

- **验证待办**（静态读码只能确定"`respond` 会进 TTS 队列"，不能确定念出来的是原句）：
  1. `verbatim` 下宿主复述的句子是否与 `data/phrases.json` 逐字一致、有没有超 24 字；
  2. `bubble` 的气泡在语音模式下是否完全无声；
  3. 从推送到出声的端到端延迟；
  4. 连续多条推送时同一个 `coalesce_key` 是否真的顶掉了还没说出口的旧句。

  确认之前不要在文档里承诺"念出来的一定逐字不动"。

### 紧急度分级

14 个写端事件 + 11 个派生事件 = **25 个 id**，分三档（唯一真相是 `arbiter.EVENT_LEVELS`）：

| 档 | 通道优先级 | 判据 | 例子 |
| --- | --- | --- | --- |
| `critical` | 90 | 局面已经变了 | 阵亡 / 结算 / 着火 / 血线告急 |
| `warning` | 50 | 该说，晚一帧说依然成立 | 击毁 / 受击 / 时间告急 / 人数差 |
| `info` | 10 | 机制回执与开场白，垫底 | 装填完成 / 火灭了 / 开打了 |

每帧最多出口一条；未登记的新事件 id 默认按 `warning` 处理，不抛异常。

### 话术表

**所有台词都在 [`data/phrases.json`](data/phrases.json)，代码里一句台词都没有。**
按 `events.<事件 id>.<档位>` 组织，同一档可以有多句变体；文件改了会被自动重载
（每帧 `os.stat` 一下 `mtime`/`size`）。改句子的流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。

内置默认表在 `phrases.py` 的 `DEFAULT_PHRASES`，与 JSON 逐字一致，只在文件丢失或改坏时兜底。

### 陪伴能力

以下能力都只改"同一件事怎么说"，**不新增任何关于敌方的播报**，且都能单独关掉。

| 能力 | 触发 | 说什么 |
| --- | --- | --- |
| 结算复盘分档 | 结算那一帧 | 赢得很猛 / 赢了但搭进去一条命 / 输得可惜 / 输得太快 / 结果没看清只报事实 |
| 本地战绩账本 | 每局结算追加一行 | 不直接说话（是下面两项的数据源），只写本机、可整项关闭 |
| 连败 / 连胜关怀 | 最近 3 局结果相同 | 连败："三局了，手是不是有点僵。" / 连胜："连着三局，这状态可以啊。" |
| 二阶静默 | 战斗中长时间没说话 | "我在呢，慢慢来。"（一局最多一次） |
| 话术预算 | 每次说出口 | 单局 ≤ 40 句、任意 30 秒 ≤ 3 句；`critical` 不受窗闸限制但计入总量 |
| 受击反应分档 | 掉血比例 | 重击 / 极重 / 跳弹（跳弹**一个数字都不出现**） |
| 劣势细分 | 双方人数差 | 少一两个 / 差得有点多 / 就剩咱们几个 |
| 开场人格化 | 开局 | 今天第 N 局 / 这台车均伤 / 这台车打过几局（账本缺失就退回通用句，绝不猜数字） |
| 断带 / 断轮双层 | 模块伤害 | 履带伤了 / 履带断了 / 轮子伤了 / 轮子断了 |
| 机制开场与切换 | 载具机制 | 双管 / 火箭 / 液气 / 自动液压 / 轮式 / 弹鼓 / 自动补弹 / 机关炮 |

账本只在开局与结算两帧读写，**绝不每 tick 读盘**；关掉 `history_enabled` 就一个字节都不落盘。

### 开发与测试

```bash
python -m pip install pytest pytest-asyncio pytest-randomly ruff==0.12.4

python -m pytest tests -q --tb=short -p no:cacheprovider
ruff check --ignore-noqa --isolated --no-respect-gitignore --target-version py311 \
           --line-length 120 --select E4,E7,E9,F,I --exclude .venv --exclude __pycache__ --exclude .ruff_cache .
```

CI 跑的就是这两条（见 `.github/workflows/ci.yml`）。测试用一份极薄 SDK 替身，
所以在没有宿主的环境里也能跑。

目录结构：

```text
arbiter.py        仲裁器：决定这一帧说不说、说哪一条、怎么渲染
phrases.py        话术表加载 + 内置默认表 + 占位符语义
settings.py       配置默认值、容错与裁剪
state_reader.py   state.json 读取与帧状态判定（纯标准库，可脱离时钟测试）
history.py        本地战绩账本（JSONL，原子替换）
__init__.py       插件入口：tick、通道推送、生命周期
data/phrases.json 全部台词
docs/PROTOCOL.md  协议契约（只写字段与语义）
tests/            568 条用例
tools/            话术快照工具
```

### 许可与免责

Apache-2.0，全文见 [LICENSE](LICENSE)。本项目是非官方、玩家自制的陪伴插件，
与 Wargaming.net / Wargaming Group Limited 无关；**不含任何游戏客户端代码**。
详见 [NOTICE](NOTICE)。

---

## English

> Draft — wording will be polished by the maintainer.

### What it is

`neko_wot` is a reader-side plugin that runs inside a plugin host. Roughly four times a
second it samples a local JSON file (`state.json`), an arbiter decides whether this frame
should speak and what to say, and one short line of Chinese companion text is handed to
the host's chat/voice channel.

In one sentence: **it only reads a file, and it only talks.**

### Data boundary

| It does | It does not |
| --- | --- |
| Read the local `state.json` (read-only) | Read or write game process memory, inject DLLs, hook rendering |
| Read your own vehicle state, both teams' alive counts, past events, results | Read any enemy unit's position, heading, reload or health |
| Emit natural-language Chinese text (with a `{MASTER_NAME}` placeholder) | Emit coordinates, distances, bearings, predictions, "shoot this one" advice |
| Deduplicate events, apply cooldowns and a global minimum gap | Auto-fire, auto-aim, auto-drive, or inject any input |
| Default to `dry_run = true` | Network access, uploads, telemetry |

Templates are not allowed to contain enemy-side wording; the single source of truth for
that list is `arbiter.FORBIDDEN_WORD_GROUPS["ENEMY"]`. Event payloads are filtered
through a key whitelist, so anything else is dropped before rendering.

### How it works

1. A separate, closed-source companion component writes local state to
   `%LOCALAPPDATA%\WoT_Copilot\state.json` at about 4 Hz.
2. This plugin samples it, detects new frames by `seq`, stale frames by `ts`, and
   tolerates a half-written file.
3. The arbiter turns the frame into candidates and picks **exactly one** per frame,
   first by severity tier, then by in-tier priority.
4. The rendered line must pass the forbidden-word gate before it is handed to the host.
5. With `dry_run = true` nothing is sent; decisions are only logged.

The plugin **consumes** the protocol, it never defines it — see
[`docs/PROTOCOL.md`](docs/PROTOCOL.md).

### Install

Python 3.11+, no runtime third-party dependencies. Drop this directory into the host's
plugin folder; the entry point declared in `plugin.toml` is
`plugin.plugins.neko_wot:NekoWotPlugin`.

### Configuration

All keys live in `[neko_wot]`; the annotated example is
[`config.example.toml`](config.example.toml). Invalid values fall back to defaults with a
startup warning — they never keep the plugin from starting.

Highlights: `dry_run` (default `true`), `state_path`, `poll_interval_s` (`0.25`),
`stale_seconds` (`5.0`), `min_gap_s` (`2.0`), `output_mode` (`verbatim` / `bubble` / `llm`),
`interrupt_enabled`, `idle_fill_s`, `info_channel`, `team_events_enabled`, `debug_log`.

### Severity tiers

25 event ids (14 sent by the writer, 11 derived by this plugin) fall into three tiers:
`critical` (channel priority 90), `warning` (50) and `info` (10). At most one line leaves
per frame. Unknown ids are treated as `warning` instead of raising.

### Phrase table

Every line lives in [`data/phrases.json`](data/phrases.json); there is no line of dialogue
in the code. The file is hot-reloaded when its `mtime`/`size` changes, and the built-in
default table in `phrases.py` is a byte-for-byte fallback.

### Companion features

Result recap tiers, a local battle ledger (written only on this machine, can be disabled
entirely), losing/winning-streak care, a second-tier idle line, a global speech budget
with a 30-second sliding window, hit-reaction tiers, disadvantage tiers, personalized
battle-start lines, track/wheel damage tiers, and vehicle-mechanic lines. None of them
adds enemy-side information.

### Development

```bash
python -m pip install pytest pytest-asyncio pytest-randomly ruff==0.12.4

python -m pytest tests -q --tb=short -p no:cacheprovider
ruff check --ignore-noqa --isolated --no-respect-gitignore --target-version py311 \
           --line-length 120 --select E4,E7,E9,F,I --exclude .venv --exclude __pycache__ --exclude .ruff_cache .
```

These are exactly what CI runs (`.github/workflows/ci.yml`).

### License and disclaimer

Apache-2.0 — see [LICENSE](LICENSE). This is an unofficial, fan-made companion plugin,
not affiliated with Wargaming.net or Wargaming Group Limited, and it contains no game
client code. See [NOTICE](NOTICE).
