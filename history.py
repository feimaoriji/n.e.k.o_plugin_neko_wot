"""本地战绩账本（v2.18）。

**只落本机、只追加、不上传**（合规 F8）：一行一局，UTF-8，JSONL。
位置：``%LOCALAPPDATA%\\WoT_Copilot\\neko_battles.jsonl``（与 mod 的 ``state.json`` 同目录）。

三条硬规矩（本模块的全部意义就是把它们关在一个文件里）：

1. ⛔ **绝不写 ``state.json``** —— 那是写端 mod 的盘，读端只读。这里只碰自己的账本。
2. **读只在 ``battle_start`` / ``battle_end`` 两处**（由 ``arbiter`` 负责调用）：
   账本每 tick 读一次就等于把 4Hz 的采样变成 4 次磁盘 IO，得不偿失。
3. **容错到底**：坏行跳过、整份坏 / 没有文件 / 没权限 → 一律当成"没有历史"，
   相关话术整句不出现；**永不抛异常**、绝不降级成猜（猜比不说坏）。

写入上限 :data:`MAX_ROWS` 行：超限时读最近 N 行 → 写 ``.tmp`` → :func:`os.replace`
原子替换（**绝不"先删再写"**：中途崩掉会连整份账本一起丢）。
写失败只 WARN，绝不影响播报。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

LOGGER = logging.getLogger("neko_wot.history")

SCHEMA_VERSION = 1
FILENAME = "neko_battles.jsonl"
# 保留最近多少局。500 局 ≈ 一个普通人两三个月的量，文件也就几十 KB。
MAX_ROWS = 500
# 协议 §5.3 的四个值；别的值一律当坏行（不猜、不折算）。
RESULT_VALUES = frozenset({"win", "lose", "draw", "unknown"})
TMP_SUFFIX = ".tmp"


def default_history_path() -> Path:
    """协议默认路径：``%LOCALAPPDATA%\\WoT_Copilot\\neko_battles.jsonl``。

    环境变量都拿不到时才退回**相对路径**（不猜绝对路径：写错地方比不写更糟）。
    """
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or ""
    if not base:
        return Path(FILENAME)
    return Path(base) / "WoT_Copilot" / FILENAME


@dataclass(frozen=True)
class Battle:
    """一局账目（字段顺序 = 落盘顺序，便于人眼比对）。"""

    ts: float
    map: str | None
    vehicle: str | None
    mode_kind: str | None
    result: str
    kills: int | None
    dmg: int | None
    deaths: int
    seconds: int | None

    def as_row(self) -> dict[str, Any]:
        """落盘形状（``v`` 是账本 schema，和 mod 的协议 schema 无关）。"""
        return {
            "ts": int(self.ts),
            "map": self.map,
            "vehicle": self.vehicle,
            "mode_kind": self.mode_kind,
            "result": self.result,
            "kills": self.kills,
            "dmg": self.dmg,
            "deaths": self.deaths,
            "seconds": self.seconds,
            "v": SCHEMA_VERSION,
        }


class History:
    """账本门面：``recent()`` 读、``append()`` 写，两个方法都**永不抛异常**。"""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        max_rows: int = MAX_ROWS,
        logger: Any | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else default_history_path()
        self.max_rows = max(1, int(max_rows))
        self.logger: Any = LOGGER if logger is None else logger
        # 同一句 WARNING 只打一次（4Hz 下别刷屏）。
        self._warned: set[str] = set()

    # ------------------------------------------------------------------ 读
    def recent(self, limit: int | None = None) -> list[Battle]:
        """最近若干局（旧 → 新）。文件不存在 / 坏了 / 没权限 → 空列表。"""
        rows = self._read_rows()
        if limit is not None and limit >= 0:
            return rows[-int(limit):] if limit else []
        return rows

    def _read_rows(self) -> list[Battle]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return []
        except OSError as error:
            self._warn("read", "读不到战绩账本 %s：%s", self.path, error)
            return []
        battles: list[Battle] = []
        # 坏字节不牵连整份文件：替换成占位符，坏行在 json 解析那一步被丢掉。
        for line in raw.decode("utf-8", errors="replace").splitlines():
            text = line.strip()
            if not text:
                continue
            battle = parse_row(text)
            if battle is not None:
                battles.append(battle)
        return battles[-self.max_rows:]

    # ------------------------------------------------------------------ 写
    def append(self, battle: Battle) -> bool:
        """追加一局并做 500 行截断。成功 ``True``，任何失败 ``False``（只 WARN）。"""
        rows = self._read_rows()
        rows.append(battle)
        keep = rows[-self.max_rows:]
        text = "".join(json.dumps(row.as_row(), ensure_ascii=False) + "\n" for row in keep)
        tmp = self.path.with_name(self.path.name + TMP_SUFFIX)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "wb") as handle:
                handle.write(text.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            # 原子替换：中途崩掉时旧账本仍然完整（绝不先删再写）。
            os.replace(tmp, self.path)
        except OSError as error:
            self._warn("write", "写战绩账本失败（不影响播报）：%s", error)
            return False
        return True

    # ------------------------------------------------------------------ 内部
    def _warn(self, key: str, template: str, *args: Any) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        try:
            self.logger.warning(template, *args)
        except Exception:  # pragma: no cover - 日志不许把账本弄挂
            pass


def parse_row(line: str) -> Battle | None:
    """一行 JSON → :class:`Battle`；坏行（非 dict / 缺 ts / result 不认识）→ ``None``。"""
    try:
        row = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(row, dict):
        return None
    result = row.get("result")
    if not isinstance(result, str) or result not in RESULT_VALUES:
        return None
    ts = _as_number(row.get("ts"))
    if ts is None:
        return None
    deaths = _as_count(row.get("deaths"))
    return Battle(
        ts=ts,
        map=_as_text(row.get("map")),
        vehicle=_as_text(row.get("vehicle")),
        mode_kind=_as_text(row.get("mode_kind")),
        result=result,
        kills=_as_count(row.get("kills")),
        dmg=_as_count(row.get("dmg")),
        deaths=deaths if deaths is not None else 0,
        seconds=_as_count(row.get("seconds")),
    )


def last_results(rows: Sequence[Battle], count: int) -> list[str]:
    """最近 ``count`` 局的结果（旧 → 新）；不足就用实际条数（调用方自己判长度）。"""
    if count <= 0:
        return []
    return [row.result for row in list(rows)[-count:]]


def vehicle_average_damage(rows: Iterable[Battle], vehicle: str | None) -> tuple[float | None, int]:
    """同一台车的**历史均伤** → ``(均伤, 样本局数)``。

    ``vehicle`` 为空、或者样本里没有 ``dmg`` 的局一律不计入（``None`` 不是 0）；
    样本为 0 时返回 ``(None, 0)`` —— 调用方据此闭嘴（宁可不说，不许拿 0 顶）。
    """
    if not vehicle:
        return None, 0
    samples = [row.dmg for row in rows if row.vehicle == vehicle and row.dmg is not None]
    if not samples:
        return None, 0
    return sum(samples) / len(samples), len(samples)


def _as_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _as_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


__all__ = [
    "Battle",
    "FILENAME",
    "History",
    "MAX_ROWS",
    "RESULT_VALUES",
    "SCHEMA_VERSION",
    "default_history_path",
    "last_results",
    "parse_row",
    "vehicle_average_damage",
]
