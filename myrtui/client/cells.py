"""按**单元格宽度**裁剪文本（N9 口径：cell_len，勿用 len）。

CJK 宽字符占 2 cell，用 len() 裁剪会截出超宽行（撑破布局、盖住相邻列）。
侧栏摘要（``sidebar.py``）与 Git 历史面板（``git_panel.py``）共用同一套口径，
故抽到本模块——避免两个模块各写一份、久而久之分叉。
"""
from __future__ import annotations

from rich.cells import cell_len


def truncate_cells(s: str, max_cells: int) -> str:
    """按单元格宽度截断并补 ``…``（N9 口径：cell_len，勿用 len——CJK 宽字符
    占 2 cell，len 会截出超宽行）。

    ``max_cells`` 含省略号本身；不超长原样返回（幂等）；max_cells <= 0 返回空。
    """
    if max_cells <= 0:
        return ""
    if cell_len(s) <= max_cells:
        return s
    budget = max_cells - 1  # 留 1 cell 给省略号
    out: list[str] = []
    used = 0
    for ch in s:
        w = cell_len(ch)
        if used + w > budget:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"
