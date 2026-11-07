# Changelog

本文件记录本项目的所有重要变更。
格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-11-08

### 新增

- **首个公开版本。** 读取本机 `state.json`（协议 v1），把"自己这一侧、屏幕上本来就
  看得到的事实"翻译成一句中文陪伴文本，经宿主对话通道送出。
- **三种输出模式**（`output_mode`）：`verbatim`（默认，让宿主 LLM 只复述审过的原句）、
  `bubble`（只弹气泡、不出声、0 token）、`llm`（交给宿主 LLM 自由发挥）。
- **三级紧急度 + 档内排序**：24 个事件 id 分 `critical` / `warning` / `info`，
  每帧最多出口一条；`critical` 可配置为无视最小间隔。
- **话术外置**：全部台词在 `data/phrases.json`，热重载，逐字快照闸 + 整份文件禁词闸。
- **本地战绩账本**：每局一行写到本机 `neko_battles.jsonl`（不上传、可整项关闭），
  用于结算复盘分档、连败/连胜关怀与开场人格化。
- **陪伴细节**：受击反应分档、劣势细分、二阶静默、全局话术预算与 30 秒滑动窗、
  断带/断轮双层播报、载具机制开场与切换分档。
- **合规闸**：所有出口文本都要过 `preach_is_forbidden`；事件 payload 只放行白名单键，
  敌方信息字段一律丢弃。

### 说明

- 运行时零第三方依赖（仅标准库）；开发与测试依赖见 `pyproject.toml`。
- 协议契约定在 [`docs/PROTOCOL.md`](docs/PROTOCOL.md)，本插件只消费、不定义协议。
