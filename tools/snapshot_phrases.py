"""生成 / 校验 的"改动前话术快照"。

快照是**话术外置化之前**（相等于 v2.7 的 ``arbiter.py``）跑 ``tests/phrase_matrix.py``
得到的渲染结果，逐字记录每一句。v2.8 把话术搬进 ``data/phrases.json`` 之后，渲染结果
必须与它**完全相等** —— 任何一句被悄悄改写都算失败。

用法（在本插件目录下）::

    .venv\\Scripts\\python.exe tools\\snapshot_phrases.py           # 重新写快照
    .venv\\Scripts\\python.exe tools\\snapshot_phrases.py --check   # 只比对，不改文件

⚠️ ``--write`` 只在"确认话术本来就要改"时用（真正的产品改动），平时的用途是
``--check``：它跟 ``tests/test_phrases_table.py`` 里那条闸等价，但会打印差异。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = PLUGIN_DIR / "tests"
SNAPSHOT = TESTS_DIR / "fixtures" / "phrases_render_v27.json"


def _load_modules():
    """函数内导入：先挂 ``tests/`` 到 sys.path（与 conftest 同一条路）。"""
    if str(TESTS_DIR) not in sys.path:
        sys.path.insert(0, str(TESTS_DIR))
    import phrase_matrix
    import sdk_stub

    package = sdk_stub.load_plugin_module()
    return package.arbiter, package.state_reader, phrase_matrix


def _read_snapshot() -> dict:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def _write_snapshot(rendered: dict) -> None:
    text = json.dumps(rendered, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    # 先编码再写字节：无 BOM、LF 结尾（JSON 文件带 BOM 会让 json.loads 在别的工具里炸）。
    SNAPSHOT.write_bytes(text.encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="话术快照")
    parser.add_argument("--check", action="store_true", help="只比对，不写文件")
    args = parser.parse_args(argv)

    arbiter_mod, state_reader, phrase_matrix = _load_modules()
    rendered = phrase_matrix.render_matrix(arbiter_mod, state_reader)

    if not args.check:
        _write_snapshot(rendered)
        print(f"wrote {SNAPSHOT} ({len(rendered)} lines)")
        return 0

    expected = _read_snapshot()
    diffs = []
    for key in sorted(set(expected) | set(rendered)):
        if expected.get(key) != rendered.get(key):
            diffs.append((key, expected.get(key), rendered.get(key)))
    if not diffs:
        print(f"OK: {len(rendered)} lines identical to {SNAPSHOT.name}")
        return 0
    for key, want, got in diffs:
        print(f"DIFF {key}\n  snapshot: {want!r}\n  current : {got!r}")
    print(f"{len(diffs)} difference(s)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
