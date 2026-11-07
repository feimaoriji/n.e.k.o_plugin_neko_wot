"""只在真 SDK 导不进来时启用的 ``plugin.sdk.plugin`` 极薄替身。

为什么需要它：插件包在导入时就会 ``from plugin.sdk.plugin import ...``，而真 SDK
会连锁拉进 msgpack / pydantic / pyzmq 这些宿主依赖。没有它们，pytest 连用例都收集
不起来。替身只解决"能在没有宿主的环境里跑测试"，**不替插件兜错**：

* 每个函数的参数表都照抄真 SDK（仅关键字、同名默认值），写错关键字直接 ``TypeError``；
* 不提供 ``**kwargs`` 兜底，也不做任何"看起来能跑"的自动纠正；
* ``dump()`` 的返回形状与真 SDK 的 ``unwrap_config_payload`` 一致（``{"config": ...}`` → 内层）。

真 SDK 能导入时这里一行都不会执行，测试就跑在真 SDK 上。
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EVENT_META_ATTR = "__neko_event_meta__"

PLUGIN_DIR = Path(__file__).resolve().parents[1]
SDK_MODULE = "plugin.sdk.plugin"
_PLUGIN_MODULE_NAME = "neko_wot_under_test"

_BACKEND: str | None = None


# --------------------------------------------------------------------- 后端选择
def sdk_backend() -> str:
    """返回 ``"real"`` 或 ``"stub"``，只探测一次。"""
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = "real" if _real_sdk_available() else _install_stub()
    return _BACKEND


def _real_sdk_available() -> bool:
    try:
        importlib.import_module(SDK_MODULE)
    except Exception:
        return False
    return True


def _install_stub() -> str:
    """只顶掉 ``plugin.sdk.plugin`` 这一个模块，父包尽量保留真的。

    保留真 ``plugin`` 很重要：pytest 会把本目录按包路径
    ``plugin.plugins.neko_wot.tests`` 收集，父包一旦被换成无 ``__path__`` 的
    空壳，收集阶段就 ``No module named 'plugin.plugins'`` 了。
    """
    api = types.ModuleType(SDK_MODULE)
    api.__all__ = ["NekoPluginBase", "Ok", "neko_plugin", "plugin_entry", "timer_interval", "lifecycle"]
    api.NekoPluginBase = StubPluginBase
    api.Ok = Ok
    api.neko_plugin = neko_plugin
    api.plugin_entry = plugin_entry
    api.timer_interval = timer_interval
    api.lifecycle = lifecycle

    try:
        plugin_pkg = importlib.import_module("plugin")
    except Exception:
        plugin_pkg = types.ModuleType("plugin")
        plugin_pkg.__path__ = []
        sys.modules["plugin"] = plugin_pkg

    try:
        sdk_pkg = importlib.import_module("plugin.sdk")
    except Exception:
        sdk_pkg = types.ModuleType("plugin.sdk")
        sdk_pkg.__path__ = []
        sys.modules["plugin.sdk"] = sdk_pkg
        plugin_pkg.sdk = sdk_pkg

    sdk_pkg.plugin = api
    sys.modules[SDK_MODULE] = api
    return "stub"


# --------------------------------------------------------------------- 桩 API
@dataclass(frozen=True)
class Ok:
    """真 SDK 的 ``Ok`` 是个 frozen dataclass，这里保持一致的最小形状。"""

    value: Any = None

    def is_ok(self) -> bool:
        return True

    def is_err(self) -> bool:
        return False


@dataclass
class StubEventMeta:
    """真 ``EventMeta`` 的字段子集，够测试断言 kind/id/seconds 即可。"""

    event_type: str
    id: str
    kind: str = "action"
    name: str = ""
    description: str = ""
    auto_start: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def neko_plugin(cls: type | None = None):
    def _mark(target: type) -> type:
        target.__neko_plugin__ = True
        return target

    return _mark(cls) if cls is not None else _mark


def plugin_entry(*, id: str, name: str | None = None, description: str = "", **_unused: Any):
    def _decorator(fn):
        setattr(fn, EVENT_META_ATTR, StubEventMeta("plugin_entry", id, "action", name or id, description))
        return fn

    return _decorator


def lifecycle(*, id: str, name: str | None = None, description: str = "", metadata: Any = None):
    def _decorator(fn):
        setattr(fn, EVENT_META_ATTR, StubEventMeta("lifecycle", id, "lifecycle", name or id, description))
        return fn

    return _decorator


def timer_interval(
    *,
    id: str,
    seconds: int,
    name: str | None = None,
    description: str = "",
    auto_start: bool = True,
    metadata: Any = None,
):
    if seconds <= 0:
        raise ValueError("seconds must be > 0")

    def _decorator(fn):
        meta = StubEventMeta(
            "timer",
            id,
            "timer",
            name or id,
            description,
            auto_start=auto_start,
            extra={"mode": "interval", "seconds": seconds},
        )
        setattr(fn, EVENT_META_ATTR, meta)
        return fn

    return _decorator


class _StubConfig:
    """镜像 ``PluginConfig.dump()``：``ctx.get_own_config()`` → 解 ``config`` 壳。"""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    async def dump(self, *, timeout: float = 5.0) -> dict[str, Any]:
        payload = await self._ctx.get_own_config(timeout=timeout)
        if isinstance(payload, dict):
            inner = payload.get("config")
            if isinstance(inner, dict):
                return dict(inner)
            return dict(payload)
        return {}


class StubPluginBase:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.config = _StubConfig(ctx)
        self.logger = ctx.logger

    def push_message(
        self,
        *,
        visibility: list[str] | None = None,
        ai_behavior: str | None = None,
        parts: list[dict[str, Any]] | None = None,
        source: str = "",
        target_lanlan: str | None = None,
        metadata: dict[str, Any] | None = None,
        priority: int = 0,
        coalesce_key: str | None = None,
        message_type: str | None = None,
        description: str | None = None,
        content: str | None = None,
        binary_data: bytes | None = None,
        binary_url: str | None = None,
        mime: str | None = None,
        unsafe: bool = False,
        fast_mode: bool = False,
        delivery: str | bool | None = None,
        reply: bool | None = None,
    ) -> dict[str, Any]:
        # 参数表照抄真 SDK：写错关键字这里就会 TypeError，绝不兜底。
        return self.ctx.push_message(
            visibility=visibility,
            ai_behavior=ai_behavior,
            parts=parts,
            source=source,
            target_lanlan=target_lanlan,
            metadata=metadata,
            priority=priority,
            coalesce_key=coalesce_key,
            message_type=message_type,
            description=description,
            content=content,
            binary_data=binary_data,
            binary_url=binary_url,
            mime=mime,
            unsafe=unsafe,
            fast_mode=fast_mode,
            delivery=delivery,
            reply=reply,
        )


# --------------------------------------------------------------------- 测试宿主
class RecordingLogger:
    """记录每次调用，同时转发给 stdlib，方便 caplog / -s 观察。"""

    def __init__(self, name: str = "neko_wot.tests") -> None:
        self.records: list[str] = []
        self._logger = logging.getLogger(name)

    def _record(self, level: str, msg: object, args: tuple[Any, ...]) -> None:
        text = str(msg)
        if args:
            try:
                text = text % args
            except (TypeError, ValueError):
                text = " ".join([text, *(str(item) for item in args)])
        self.records.append(f"{level}: {text}")
        self._logger.log(logging.INFO if level in {"info", "debug"} else logging.WARNING, text)

    def debug(self, msg: object, *args: Any, **_kwargs: Any) -> None:
        self._record("debug", msg, args)

    def info(self, msg: object, *args: Any, **_kwargs: Any) -> None:
        self._record("info", msg, args)

    def warning(self, msg: object, *args: Any, **_kwargs: Any) -> None:
        self._record("warning", msg, args)

    def error(self, msg: object, *args: Any, **_kwargs: Any) -> None:
        self._record("error", msg, args)

    def exception(self, msg: object, *args: Any, **_kwargs: Any) -> None:
        self._record("exception", msg, args)

    def critical(self, msg: object, *args: Any, **_kwargs: Any) -> None:
        self._record("critical", msg, args)

    def bind(self, **_kwargs: Any) -> RecordingLogger:
        return self

    def opt(self, **_kwargs: Any) -> RecordingLogger:
        return self


class FakeHostCtx:
    """宿主上下文替身：真 SDK 和桩都能吃。

    ``ensure_sdk_context()`` 会检查 7 个属性 + 22 个方法，凑齐了它就是"已经是
    SDK 上下文"，真 SDK 不会再包一层；桩则只用到 ``get_own_config`` / ``logger``
    / ``push_message`` 三个。
    """

    def __init__(self, section: dict[str, Any] | None = None, plugin_id: str = "neko_wot") -> None:
        self.plugin_id = plugin_id
        self.metadata: dict[str, Any] = {}
        self.logger = RecordingLogger()
        self.config_path = str(PLUGIN_DIR / "plugin.toml")
        self.bus: dict[str, Any] = {}
        self.images = None
        self.models = None
        self.config_payload: dict[str, Any] = {"neko_wot": dict(section or {})}
        self.pushed: list[dict[str, Any]] = []
        self.status: dict[str, Any] = {}
        self._effective_config: dict[str, Any] = {
            "plugin": {"store": {"enabled": False}, "database": {"enabled": False}},
            "plugin_state": {"backend": "off"},
        }

    # --- config ---
    async def get_own_config(self, timeout: float = 5.0) -> dict[str, Any]:
        return {"config": dict(self.config_payload)}

    async def get_own_base_config(self, timeout: float = 5.0) -> dict[str, Any]:
        return {"config": dict(self.config_payload)}

    async def get_own_profiles_state(self, timeout: float = 5.0) -> dict[str, Any]:
        return {"profiles": [], "active": "default"}

    async def get_own_profile_config(self, profile_name: str, timeout: float = 5.0) -> dict[str, Any]:
        return {"config": dict(self.config_payload)}

    async def get_own_effective_config(self, profile_name: str | None = None, timeout: float = 5.0) -> dict[str, Any]:
        return {"config": dict(self.config_payload)}

    async def update_own_config(self, updates: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        return {"config": dict(updates)}

    async def replace_own_config(self, config: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        return {"config": dict(config)}

    async def upsert_own_profile_config(
        self,
        profile_name: str,
        config: dict[str, Any],
        *,
        make_active: bool = False,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        return {"config": dict(config)}

    async def delete_own_profile_config(self, profile_name: str, timeout: float = 10.0) -> dict[str, Any]:
        return {}

    async def set_own_active_profile(self, profile_name: str, timeout: float = 10.0) -> dict[str, Any]:
        return {}

    # --- 其它宿主能力：插件不用，返回空壳即可 ---
    async def query_plugins(self, filters: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
        return {"plugins": []}

    async def trigger_plugin_event(self, **_kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    async def get_system_config(self, timeout: float = 5.0) -> dict[str, Any]:
        return {"config": {}}

    async def query_memory(self, bucket_id: str, query: str, timeout: float = 5.0) -> dict[str, Any]:
        return {"items": []}

    async def run_update(self, **_kwargs: Any) -> dict[str, Any]:
        return {}

    async def run_update_async(self, **_kwargs: Any) -> dict[str, Any]:
        return {}

    async def export_push(self, **_kwargs: Any) -> dict[str, Any]:
        return {}

    async def export_push_async(self, **_kwargs: Any) -> dict[str, Any]:
        return {}

    async def finish(self, **_kwargs: Any) -> dict[str, Any]:
        return {}

    def push_message(self, **kwargs: Any) -> dict[str, Any]:
        self.pushed.append(dict(kwargs))
        return {"submitted": True}

    async def create_card(self, **_kwargs: Any) -> Any:
        return None

    def get_card(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    async def create_view(self, **_kwargs: Any) -> Any:
        return None

    def get_view(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    def update_status(self, status: dict[str, Any]) -> None:
        self.status = dict(status)


# --------------------------------------------------------------------- 加载插件
def load_plugin_module() -> types.ModuleType:
    """按路径加载真实插件包（合成包名，避免和 sys.path 上的 ``plugin`` 打架）。"""
    sdk_backend()
    module = sys.modules.get(_PLUGIN_MODULE_NAME)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(
        _PLUGIN_MODULE_NAME,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:  # pragma: no cover - 环境坏了才会走到
        raise RuntimeError(f"cannot load plugin package from {PLUGIN_DIR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PLUGIN_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def load_plugin_class() -> type:
    return load_plugin_module().NekoWotPlugin
