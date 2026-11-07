"""neko_wot 测试的公共设施。

这里先做一件必须先做的事：在**收集期之前**决定用真 SDK 还是极薄桩
（见 ``sdk_stub``），否则插件包一 import 就会缺宿主依赖。

另外：仓库根 ``tests/clock_guard.py`` 会拦住任何"替换 stdlib 时钟"的测试，
所以本目录的用例一律**显式传 now**，从不 monkeypatch ``time``。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent


def _bootstrap():
    """把本目录挂进 sys.path 并装载 sdk_stub。

    用函数而不是模块级语句，是因为 ``ruff check --ignore-noqa`` 会把任何位于
    import 之后的模块级语句算成 E402——而``# noqa`` 在这个命令里是失效的。
    """
    if str(_TESTS_DIR) not in sys.path:
        sys.path.insert(0, str(_TESTS_DIR))
    import sdk_stub

    return sdk_stub


sdk_stub = _bootstrap()
# 收集期就把桩装好（或者确认真 SDK 可用）。
BACKEND = sdk_stub.sdk_backend()


@pytest.fixture(scope="session")
def sdk_backend_name() -> str:
    return BACKEND


@pytest.fixture(scope="session")
def plugin_module():
    return sdk_stub.load_plugin_module()


@pytest.fixture(scope="session")
def plugin_class():
    return sdk_stub.load_plugin_class()


@pytest.fixture
def make_plugin(plugin_module):
    """构造一个插件实例：``make_plugin({"dry_run": False})`` → (plugin, host)。"""

    def _make(section: dict | None = None):
        host = sdk_stub.FakeHostCtx(section)
        plugin = plugin_module.NekoWotPlugin(host)
        return plugin, host

    return _make
