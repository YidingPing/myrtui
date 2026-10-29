"""布局比例的持久化：拖拽分隔线 → 下次 attach 同一项目时恢复。

**纯数据模块（不 import Textual）**——比例只是一组数字，不该和 UI 框架耦合；
这样才能在没有 UI 的环境里单测，也能被 cli / app 两侧共用。

- 四个比例维度（:data:`DIMENSIONS`）：

  ==========  ====================================================
  ``sidebar``  侧栏 | pane 区（``#main`` 里的竖线）
  ``tree``     文件树 | Git 面板（侧栏内的横线）
  ``rows``     列内上下（``#col-a`` / ``#col-b`` 里的横线）
  ``cols``     左右两列（``#pane-container`` 里的竖线）
  ==========  ====================================================

- 取值 0..1 = 「前一块」在该维度**可分配空间**里的占比（不含分隔线本身）；
- 落盘位置 ``<user_data_dir>/myrtui/<hash>.layout.json``（见
  ``cli.layout_path_for``，与锁 / 接管标记同一目录约定）；
- 写：temp + ``os.replace`` 原子替换——半截 JSON 会让下次 attach 直接读炸；
- 读：文件缺失 / 损坏 / 值越界 → **一律回落默认值**：attach 是「尽力恢复」，
  绝不能因为一个坏文件就起不来（这条比「精确恢复」重要）。

**v0.12：落盘格式升到 version 2**——pane 区由行优先改为列优先（第二个 pane
默认在第一个**下面**），旧的 version 1 文件里的比例几何含义已变（v1 的
``rows`` 是「上下两行」、v2 是「列内上下」），故**版本不匹配 → 回落默认值**
（老用户调好的比例作废一次，换来行为口径干净）。
"""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from typing import Mapping

log = logging.getLogger("myrtui.layout")

# 比例维度（顺序固定，落盘 / 展示都按它）
DIMENSIONS: tuple[str, ...] = ("sidebar", "tree", "rows", "cols")

# 默认比例：与 app.py 的 CSS 初值一致（CSS 是「第一帧」，这里才是真值）
DEFAULT_RATIOS: dict[str, float] = {
    "sidebar": 0.15,  # 侧栏占 #main 宽度的 15%（v0.11.2：25% 仍太宽，只够文件树就好）
    "tree": 0.5,      # 文件树 / Git 面板各半
    "rows": 0.5,      # 上下行各半
    "cols": 0.5,      # 左右列各半
}

# 拖拽允许的范围：留一点底，否则会把某一块压成 0 而「找不回来」
MIN_RATIO = 0.05
MAX_RATIO = 0.95

# 落盘格式版本（以后改结构时靠它区分旧文件）
# v2：pane 区改列优先（第二个 pane 默认在第一个下面）→ v1 的比例几何含义
#      已变，版本不匹配的文件直接作废（回落默认值）。
FILE_VERSION = 2


def default_ratios() -> dict[str, float]:
    """默认比例的一份副本（调用方改它不会污染模块常量）。"""
    return dict(DEFAULT_RATIOS)


def normalize(ratios: Mapping[str, object]) -> dict[str, float]:
    """把任意来源的比例表收敛成合法值。

    - 缺项 → 默认值；
    - 非数字 / NaN / inf → 该项回落默认值（``json`` 认 ``NaN``，必须防）；
    - 越界 → 夹到 ``[MIN_RATIO, MAX_RATIO]``。
    """
    out = default_ratios()
    for key in DIMENSIONS:
        if key not in ratios:
            continue
        raw = ratios[key]
        try:
            value = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        out[key] = min(MAX_RATIO, max(MIN_RATIO, value))
    return out


def load_ratios(path: str | None) -> dict[str, float]:
    """读比例；文件缺失 / 损坏 → 默认值（绝不抛异常）。

    attach 的第一诉求是「能起来」，恢复比例只是锦上添花。
    """
    if not path:
        return default_ratios()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return default_ratios()
    except (OSError, ValueError) as exc:
        log.warning("布局文件读取失败（用默认值）: %s: %s", path, exc)
        return default_ratios()
    if not isinstance(payload, dict):
        log.warning("布局文件格式不对（用默认值）: %s", path)
        return default_ratios()
    version = payload.get("version")
    if version != FILE_VERSION:
        # 版本不匹配 = 几何含义变了（v1 是行优先、v2 是列优先）→ 旧比例作废
        log.warning("布局文件版本不匹配（用默认值）: %s（%s != %s）", path, version, FILE_VERSION)
        return default_ratios()
    raw = payload.get("ratios")
    if not isinstance(raw, dict):
        log.warning("布局文件缺少 ratios 字段（用默认值）: %s", path)
        return default_ratios()
    return normalize(raw)


def save_ratios(path: str | None, ratios: Mapping[str, object]) -> None:
    """原子写比例；失败只记 warning（拖完分隔线不该把 UI 拖崩）。"""
    if not path:
        return
    payload = {"version": FILE_VERSION, "ratios": normalize(ratios)}
    directory = os.path.dirname(path) or "."
    tmp_path = None
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".layout-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_path, path)  # 原子替换：读者要么看到旧的、要么看到新的
        tmp_path = None
    except OSError as exc:
        log.warning("布局文件写入失败: %s: %s", path, exc)
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
