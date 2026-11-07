# Contributing

感谢愿意帮忙。这个项目的定位很窄：**只把"玩家自己屏幕上已经看得到的事实"说成一句
中文陪伴文本**。任何越过这条线的改动（敌方位置、战术建议、画面叠加、自动操作）都不会
被合并，请先读 [NOTICE](NOTICE) 与 [docs/PROTOCOL.md](docs/PROTOCOL.md)。

## 开发环境

- Python 3.11+
- 运行时零第三方依赖；开发依赖：

  ```bash
  python -m pip install pytest pytest-asyncio pytest-randomly ruff==0.12.4
  ```

## 提交前自检（两条都必须绿）

```bash
python -m pytest tests -q --tb=short -p no:cacheprovider
ruff check --ignore-noqa --isolated --no-respect-gitignore --target-version py311 \
           --line-length 120 --select E4,E7,E9,F,I --exclude .venv --exclude __pycache__ --exclude .ruff_cache .
```

CI 跑的就是这两条（见 `.github/workflows/ci.yml`）。

## 改话术

这句话是产品行为，不是文案：

1. 改 `data/phrases.json`（UTF-8、无 BOM）；
2. `phrases.py` 里的 `DEFAULT_PHRASES` 必须与它逐字一致；
3. 用 `python tools/snapshot_phrases.py --check` 看逐字快照差异，确认要改就加 `--write`；
4. 跑测试 —— 禁词闸会逐条扫整份话术表。

## 改协议

协议由写端（采集端）拥有，本仓库只消费。要动协议请先在写端改并升 `schema`，
再同步本仓库的 `docs/PROTOCOL.md`、`tests/fixtures/` 与 `tests/test_protocol_snapshot.py`。

## 提交信息

`<type>: <一句话>`，`type` 用 `feat` / `fix` / `docs` / `test` / `chore` / `refactor` / `perf`。
一个提交只做一件事。
