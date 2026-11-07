"""读 mod 写下的 ``state.json``（协议 v1）。

纯标准库、不 import 任何 N.E.K.O 模块；时间一律由调用方通过 ``now`` 传入，
所以整个模块可以脱离文件系统时钟做单元测试。

读端只做协议里那四件事：判新帧（``seq``）、判陈旧（``ts``）、容忍写一半
（JSON 截断）、容忍不认识的 ``schema``（向前兼容）。判断结果统一落在
:class:`Frame` 的 ``status`` 上，一行一个原因，方便排障时直接看日志。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, replace
from typing import Any

SCHEMA_VERSION = 1

STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_TRUNCATED = "malformed_truncated"
STATUS_INVALID = "malformed_invalid"
STATUS_STALE = "stale"
STATUS_DUPLICATE = "duplicate"
STATUS_UNSUPPORTED_SCHEMA = "unsupported_schema"

STATUSES = frozenset(
    {
        STATUS_OK,
        STATUS_MISSING,
        STATUS_TRUNCATED,
        STATUS_INVALID,
        STATUS_STALE,
        STATUS_DUPLICATE,
        STATUS_UNSUPPORTED_SCHEMA,
    }
)


def default_state_path() -> str:
    """协议默认路径：``%LOCALAPPDATA%\\WoT_Copilot\\state.json``。"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or ""
    if not base:
        return os.path.join("WoT_Copilot", "state.json")
    return os.path.join(base, "WoT_Copilot", "state.json")


def resolve_state_path(configured: object) -> str:
    """配置里给了就用配置，给了空白就用默认路径。"""
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return default_state_path()


@dataclass(frozen=True)
class Frame:
    """一帧的读取结果。``status != ok`` 时其它字段只用于排障，不用于说话。"""

    status: str
    detail: str = ""
    schema: int | None = None
    schema_warning: bool = False
    seq: int | None = None
    ts: float | None = None
    mode: str | None = None
    arena_id: str | None = None
    game_version: str | None = None
    player_id: int | None = None
    me: dict[str, Any] | None = None
    team: dict[str, Any] | None = None
    # 局信息（协议 §2.2）：period / bonus_type / type_id / vehicles / map。
    # v2.4 起插件真的用它了（``arena.period == 2`` 才算"战斗阶段"），所以协议里
    # 这一块不再是"只给排障看"。字段一律原样保留、不做类型清洗——读端按需判。
    arena: dict[str, Any] | None = None
    # 本局的计数语义（协议 §4.2 / v1.30）：``standard`` / ``wave`` / ``unknown``。
    # 它是**顶层**键、不在 ``team`` 里：拿不到我方队号时 ``team`` 整块是 ``null``，
    # 而"这局的敌人数能不能数"恰恰在最不确定的局最需要它。
    mode_kind: str | None = None
    result: dict[str, Any] | None = None
    events: tuple[dict[str, Any], ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


def read_state_file(
    path: str | os.PathLike[str],
    stale_seconds: float = 5.0,
    prev_seq: int | None = None,
    now: float | None = None,
) -> Frame:
    """读一帧并给出精确的状态。任何异常都必须变成 Frame，不许往外抛。"""
    moment = time.time() if now is None else now

    try:
        with open(os.fspath(path), "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return Frame(STATUS_MISSING, detail="state.json not found")
    except OSError as error:  # 权限/被占用：都当"这帧读不到"
        return Frame(STATUS_MISSING, detail=f"unreadable: {error}")

    if not raw.strip():
        return Frame(STATUS_TRUNCATED, detail="empty file")

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        # 写端按 UTF-8 落盘；只有末尾被切断时才会崩在多字节序列上
        status = STATUS_TRUNCATED if error.start >= len(raw) - 4 else STATUS_INVALID
        return Frame(status, detail=f"not utf-8 at byte {error.start}")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        status = STATUS_TRUNCATED if _looks_truncated(text, error) else STATUS_INVALID
        return Frame(status, detail=f"{error.msg} (char {error.pos})")

    if not isinstance(data, dict):
        return Frame(STATUS_INVALID, detail=f"top-level {type(data).__name__}")

    schema = _as_int(data.get("schema"))
    if schema is None or schema < SCHEMA_VERSION:
        return Frame(STATUS_UNSUPPORTED_SCHEMA, detail=f"schema={data.get('schema')!r}")

    seq = _as_int(data.get("seq"))
    ts = _as_float(data.get("ts"))
    if seq is None or ts is None:
        return Frame(STATUS_INVALID, detail="seq/ts missing or not a number")

    frame = Frame(
        STATUS_OK,
        schema=schema,
        schema_warning=schema > SCHEMA_VERSION,
        seq=seq,
        ts=ts,
        mode=_as_str(data.get("mode")),
        arena_id=_as_str(data.get("arena_id")),
        game_version=_as_str(data.get("game_version")),
        player_id=_as_int(data.get("player_id")),
        me=_as_dict(data.get("me")),
        team=_as_dict(data.get("team")),
        arena=_as_dict(data.get("arena")),
        mode_kind=_as_str(data.get("mode_kind")),
        result=_as_dict(data.get("result")),
        events=_as_events(data.get("events")),
    )

    if prev_seq is not None and seq == prev_seq:
        return replace(frame, status=STATUS_DUPLICATE, detail=f"seq {seq} unchanged")
    if ts < moment - float(stale_seconds):
        return replace(frame, status=STATUS_STALE, detail=f"{moment - ts:.1f}s old")
    return frame


def _looks_truncated(text: str, error: json.JSONDecodeError) -> bool:
    """区分"写到一半"和"内容本身是坏的"。

    截断的特征：解析在文本末尾附近失败，且文档没有正常收尾（不以 ``}`` 结束）。
    """
    if error.msg.startswith("Unterminated string"):
        return True
    stripped = text.rstrip()
    if stripped.endswith("}"):
        return False
    return error.pos >= len(text) - 2


def _as_dict(value: object) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_events(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, dict))
