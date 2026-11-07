# 协议契约 v1（`state.json`）

> 这份文件是**契约**，不是实现说明：只写字段名、类型、语义与版本。
> 它**不描述**采集端如何取到这些值，也不包含任何游戏客户端代码或调用约定。

本插件（`neko_wot`）**只消费协议，不定义协议**。写端是一个独立的、闭源的配套组件；
两侧各自演进，唯一的人工同步点就是这份文档。

- 协议版本：`v1`（`schema = 1`）
- 传输方式：本机 JSON 文件，默认路径 `%LOCALAPPDATA%\WoT_Copilot\state.json`
- 写入方式：整帧覆盖（建议先写临时文件再原子替换），约 4 Hz
- 编码：UTF-8，**不带 BOM**（读端也容忍 BOM）
- 消费者：本插件按 `poll_interval_s`（默认 0.25 s）采样；读不到、读到一半、太旧都不许抛异常

## 1. 顶层字段

| 字段 | 类型 | 必填 | 语义 |
| --- | --- | --- | --- |
| `schema` | int | 是 | 协议版本。`< 1` 视为不兼容，整帧丢弃 |
| `seq` | int | 是 | 单调递增的帧序号。相同 `seq` 视为重复帧，不重复消费 |
| `ts` | number | 是 | 本帧生成的 Unix 时间戳（秒，可带小数） |
| `mode` | string | 否 | `garage` / `battle` / `battle_end` / `spectate` 等；未知值按"说不清"处理 |
| `arena_id` | string | 否 | 本局标识。变化即"换局"，插件据此清零局内状态 |
| `game_version` | string | 否 | 仅供排障，不参与判定 |
| `player_id` | int | 否 | 本人账号标识，仅供排障 |
| `me` | object | 否 | 本人载具状态，见 §2 |
| `team` | object \| null | 否 | 双方存活计数，见 §3。拿不到我方队号时整块为 `null` |
| `arena` | object \| null | 否 | 局信息，见 §4 |
| `mode_kind` | string | 否 | 本局计数语义，见 §5 |
| `result` | object \| null | 否 | 结算数据，见 §6。战斗中为 `null` |
| `events` | array | 否 | 最近发生的事件，见 §7 |

- 读端**忽略**未列出的字段：多给的键不会报错，也不会被使用。
- 任何字段缺失都只降级、不抛异常；缺键的语义是"不知道"，**不是**"零"。

## 2. `me`：本人载具

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `health` | int | 当前血量（绝对值） |
| `max_health` | int | 满血血量。缺省或为 0 时，依赖百分比的播报一律闭嘴 |
| `health_ratio` | number (0–1) | 血量比例 |
| `alive` | bool | 本人是否存活 |
| `reload_ratio` | number (0–1) | 装填进度（1 = 装好） |
| `fires` | int | 当前着火点数（0 = 没着火） |
| `vehicle` | string | 载具内部标识 |
| `vehicle_name` | string | 载具显示名（本地化串，如 `WZ-219`） |
| `tier` | int | 等级 |
| `mech` | string[] | 本人载具具备的机制，见 §8 |

## 3. `team`：存活计数

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `ally_alive` | int | 我方存活 |
| `ally_total` | int | 我方编制 |
| `enemy_alive` | int | 敌方存活 |
| `enemy_total` | int | 敌方编制（不可数时为 `null`） |

这几个数在记分板上对玩家可见，因此可以出现在播报里；**但仍受 §9 的出口闸约束**。

## 4. `arena`：局信息

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `period` | int | 阶段：`0` 等待 / `1` 准备 / `2` 战斗 / `3` 结算 |
| `time_left` | int | **显式剩余秒**。缺键 = 看不到计时；插件**不做任何推算** |
| `overtime` | bool | 是否处于加时态 |
| `map` | string | 地图显示名 |
| 其它 | any | 读端忽略 |

## 5. `mode_kind`：本局计数语义

顶层字段（不在 `team` 里），因为拿不到我方队号时 `team` 会整块为 `null`，
而"这局的敌人数能不能数"恰恰在最不确定的局最需要。

| 取值 | 语义 |
| --- | --- |
| `standard` | 常规对局：双方编制固定、人数可直接读 |
| `wave` | 波次/无限复活类：人数不按常规口径统计 |
| `frontline` | 前线模式：人数与事件节奏都不按常规口径 |
| `unknown` | 明知数不出来 |
| 其它/缺键 | 一律按 `unknown` 处理 —— **不许猜** |

## 6. `result`：结算

战斗中为 `null`。字段均为结算面板上本人可见的数。

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `won` | bool | 胜 |
| `draw` | bool | 平 |
| `survived` | bool | 是否存活到结束 |
| `kills` | int | 击毁数 |
| `damage` | int | 造成伤害 |
| `assist` | int | 协助伤害 |
| `blocked` | int | 抵挡伤害 |
| `spotting` | int | 点亮数 |
| `shots` / `hits` / `penetrations` | int | 射击 / 命中 / 击穿 |
| `xp` / `credits` | int | 收益 |
| `duration_s` | int | 本局时长（秒） |
| `map` / `vehicle` | string | 地图 / 载具显示名 |

胜负只有四个取值：`win` / `lose` / `draw` / `unknown`。**`unknown` 绝不猜**，
只报事实数字。

## 7. `events`：事件流

```json
{
  "seq": 12,
  "id": "you_hit",
  "ts": 1700000001.5,
  "payload": {"yaw_deg": 92.0, "crits": 16, "crits_count": 1, "crits_devices": ["track"]}
}
```

| 字段 | 类型 | 必填 | 语义 |
| --- | --- | --- | --- |
| `seq` | int | 是 | 事件序号，**全局单调**；同 `seq` 只消费一次 |
| `id` | string | 是 | 事件 id，见 §9 |
| `ts` | number | 否 | 事件发生时间 |
| `payload` | object | 否 | 事件参数，只允许 §7.1 的白名单键 |

`events` 是**最近的滑动窗口**，同一批事件会在若干连续帧里重复出现；读端按 `seq` 去重。
不是数组、元素不是对象、或缺少 `id` 的元素一律忽略。

### 7.1 `payload` 白名单

除下列键之外的 payload 键**一律丢弃**，不会进入任何播报文本。

| 键 | 类型 | 语义 |
| --- | --- | --- |
| `map` | string | 地图显示名 |
| `vehicle` | string | 载具显示名 |
| `ratio_after` | number (0–1) | 事件发生后的血量比例 |
| `total` | number | 累计值（协助等） |
| `health` / `max_health` / `delta` | number | 事件后的血量 / 满血 / 本次变化 |
| `yaw_deg` | number | 受击来向的航向角（度） |
| `blocked` | bool | 跳弹/未击穿（有受击、无掉血） |
| `crits` | int | 模块伤害的位掩码（本人载具受到的） |
| `crits_count` | int | 同一 burst 的序号（从 1 起） |
| `crits_devices` | string[] | 受损设备名：`track` / `wheel` / `gun` / `turret` 等 |
| `mech` | string | 机制名，见 §8 |
| `activated` | bool | 机制是否切到"开" |
| `dmg_acc` | number | 累计伤害 |
| `was_fire` / `was_ammo` | bool \| null | 击毁原因；`null` = 不知道，**不许当成 false** |
| `tier` / `ally` / `enemy` / `diff` | string / int / int / int | 人数差档位与两侧人数（派生事件用） |
| `n` / `left` | int | 计数与剩余（派生事件用） |
| `result` | string | 结算结果（`win` / `lose` / `draw` / `unknown`） |
| `kills` / `dmg_dealt_total` / `battle_seconds` | number | 结算数 |
| `ally_alive` / `ally_total` / `hits_taken` / `blocked_count` / `shots_fired` | int | 结算数 |

布尔键是单独一张表：其余布尔值会被有意丢弃（防止 `true` 被当数字 `1` 使用）。

## 8. 机制名枚举

`me.mech`（数组，本人载具具备的机制）：

`twin_gun` / `twin_gun_switch` / `rocket` / `hydro` / `susp_auto` / `wheeled` /
`drum` / `autoreload` / `auto_cannon`

`mode_switched.payload.mech`（单个字符串，**人工切换**后的机制）：

`twinGun` / `dualGun` / `siege` / `autocannon` / `rocket`

未知值一律按"不认识"处理：回到通用话术，不猜。

## 9. 事件 id 与紧急度

写端真正会发的 id（14 个）：

`battle_start` · `you_died` · `battle_end` · `on_fire` · `fire_out` · `you_respawned` ·
`you_killed` · `you_assisted` · `you_damaged` · `you_hit` · `mode_switched` ·
`module_damaged` · `crew_dead` · `ammo_low`

本插件自己从状态字段**派生**的 id（11 个，写端不发送）：

`reload_done` · `fire_cleared` · `hp_critical` · `enemy_killed` · `ally_killed` ·
`suppressed` · `enemy_low` · `time_low` · `team_behind` · `team_numbers` · `idle_company`

共 **25** 个 id，分三档（实现里唯一真相是 `arbiter.EVENT_LEVELS`）：

| 档 | 通道优先级 | 判据（玩家下一秒要不要改动作） | id |
| --- | --- | --- | --- |
| `critical`（4） | 90 | 局面已经变了：不改动作就吃亏，或这局已经结束 | `you_died` `battle_end` `on_fire` `hp_critical` |
| `warning`（15） | 50 | 战况与战术信息：该说，晚一帧说依然成立 | `you_respawned` `you_killed` `enemy_killed` `you_assisted` `ally_killed` `you_damaged` `you_hit` `suppressed` `enemy_low` `time_low` `team_behind` `mode_switched` `module_damaged` `crew_dead` `ammo_low` |
| `info`（6） | 10 | 机制回执与开场白：有别的可念就先念别的 | `reload_done` `fire_cleared` `battle_start` `fire_out` `team_numbers` `idle_company` |

- 未登记的 id 默认 `warning`：宁可当普通战况说出来，也不抛异常吞掉整帧。
- 每帧最多出口一条。档内再用 `arbiter.PRIORITY` 排序。

## 10. 缺失与脏值的统一约定

| 情况 | 约定 |
| --- | --- |
| 键缺失 | 语义是"不知道"，不是 `0`、不是 `false`；依赖它的播报**闭嘴** |
| 类型不对 | 等同缺失，绝不 `str()` 强转 |
| `crits_devices` 为空 | 不说是"断带/断轮" |
| `blocked == true` | 不说是"挨了一下狠的" |
| `arena.time_left` 缺失 | 不报时间告急（绝不推算剩余时间） |
| `mode_kind != "standard"` | 不报敌方人数 |
| `schema > 1` | 向前兼容：照常读，只记一条警告 |

## 11. 变更流程

1. 写端改协议并升 `schema`；
2. 同步更新本仓库的 `docs/PROTOCOL.md`、`tests/fixtures/` 与 `tests/test_protocol_snapshot.py`；
3. 两侧分别提交，插件版本号至少升 minor。

漏掉第 2 步的后果由 `tests/test_protocol_snapshot.py` 兜住：**它先红**。

## 12. 这份契约不包含什么

- 不包含采集端的实现方式、文件结构、构建方式；
- 不包含任何游戏客户端代码、反编译产物、函数名或调用约定；
- 不包含敌方单位的位置、朝向、血量、装填等任何字段；
- 不包含任何网络协议或远程接口 —— 两侧只通过本机文件通信。
