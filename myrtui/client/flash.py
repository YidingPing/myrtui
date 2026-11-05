"""按下闪烁：顶栏按钮和底栏键共用的一次性高亮（0.11.0）。

用户真机反馈：1 行高的按钮点下去没有反馈，不知道是否命中。这里在
``MouseDown`` 时给控件加 ``flash`` class，``PRESS_FLASH_SECONDS`` 后移除；
不依赖 Textual 的 ``:active``（终端里不可靠），也不改变按钮的常驻状态。
"""
from __future__ import annotations

from textual.widget import Widget

#: 高亮停留时长（秒）：太短看不见，太长会像“选中”而不是“按下”。
PRESS_FLASH_SECONDS = 0.15


def flash_press(widget: Widget) -> None:
    """给控件闪一下（重复调用会重置计时器）。"""
    widget.add_class("flash")
    timer = getattr(widget, "_flash_timer", None)
    if timer is not None:
        timer.stop()

    def clear() -> None:
        widget._flash_timer = None
        widget.remove_class("flash")

    widget._flash_timer = widget.set_timer(PRESS_FLASH_SECONDS, clear)
