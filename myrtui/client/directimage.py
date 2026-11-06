"""Kitty 直接贴图（``a=T``）：给「能画图、但不支持 Unicode 占位符」的终端。

为什么需要：``textual-image`` 的 TGP **只走占位符**（虚拟放置 + ``U+10EEEE``）。
实测某手机 SSH app 能画 ``a=T``、也回 ``i=..;OK``，却不答 ``a=q`` 查询、也不实现
``U=1`` —— 占位符那条路只能画出一屏字符。这里改用「发图即显示」，像素就有了。

代价：**图不跟随单元格滚动**。所以只给「一屏一张静态图」的界面用（``v`` 整页
视图、图片预览），Markdown 正文里滚动的公式/图片继续走 termtex / alt。

画法沿用 ``textual-image`` Sixel renderable 的技巧：先在控件区域铺满空格把
cell 占住，再 ``ESC7`` → 光标上移 ``rows`` 行 → ``a=T,C=1`` 贴图 → ``ESC8``
还原光标。Textual 只要把这段文本写到控件左上角，定位由终端自己完成；页面吸附
滚动时 Textual 会重写这些 cell，于是图片跟着重新发一遍。
"""
from __future__ import annotations

import base64
import io
import logging
import os
import random
import sys
from itertools import count
from pathlib import Path

from rich.control import ControlType
from rich.measure import Measurement
from rich.segment import Segment
from textual.widget import Widget

log = logging.getLogger("myrtui.directimage")

# Rich 会按自己的规则处理 segment 文本（宽度、换行），给个空控制码让它原样写出。
_NULL_CONTROL = [(ControlType.CURSOR_FORWARD, 0)]

# textual-image 的 IS_TMUX 在 import 时求值；这里每次现读环境，测试也好注入。
_TMUX_PREFIX = "\x1bPtmux;"
_TMUX_SUFFIX = "\x1b\\"

_ids = count(random.randint(1, 1 << 31))


def next_image_id() -> int:
    return next(_ids)


def tmux_wrap(sequence: str, env: dict[str, str] | None = None) -> str:
    """在外层 tmux 里包一层 ``ESC Ptmux;``（需要 ``allow-passthrough on``）。"""
    values = os.environ if env is None else env
    if not values.get("TMUX"):
        return sequence
    return _TMUX_PREFIX + sequence.replace("\x1b", "\x1b\x1b") + _TMUX_SUFFIX


def png_bytes(image) -> bytes:
    """``Path`` / ``bytes`` / PIL.Image → PNG 字节。"""
    if isinstance(image, (bytes, bytearray)):
        return bytes(image)
    if isinstance(image, (str, Path)):
        return Path(image).read_bytes()
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def display_sequence(payload: bytes | str, *, cols: int, rows: int, image_id: int) -> str:
    """``a=T``：传输并**直接显示**在光标处，``C=1`` 不动光标，``q=2`` 不要回执。"""
    b64 = payload if isinstance(payload, str) else base64.b64encode(payload).decode()
    return (
        f"\x1b_Ga=T,f=100,C=1,c={max(1, int(cols))},r={max(1, int(rows))},"
        f"i={image_id},q=2;{b64}\x1b\\"
    )


def delete_sequence(image_id: int) -> str:
    return f"\x1b_Ga=d,d=I,i={image_id},q=2\x1b\\"


def _write(text: str) -> None:
    try:
        stdout = sys.__stdout__
        if stdout is None or not stdout.isatty():
            return  # 测试 / 重定向：别把转义序列写进捕获的 stdout
        stdout.write(text)
        stdout.flush()
    except Exception:  # noqa: BLE001 —— 退出竞态 / 已关的 fd：删不掉图不影响功能
        log.debug("kitty 直贴写终端失败", exc_info=True)


class _DirectRenderable:
    """Rich renderable：先占 cell，再把贴图序列交给终端。

    结构与 ``textual_image.renderable.sixel`` 一致（它已在 Tabby 上真机验证过
    Textual 会把这段文本原样写进终端）。
    """

    def __init__(self, cols: int, rows: int, sequence: str) -> None:
        self._cols = cols
        self._rows = rows
        self._sequence = sequence

    def __rich_console__(self, console, options):  # noqa: ARG002 —— 只按自己的尺寸
        for _ in range(self._rows):
            yield Segment(" " * self._cols + "\n")
        yield Segment(
            "\x1b7" + f"\x1b[{self._rows}A" + self._sequence + "\x1b8",
            control=_NULL_CONTROL,
        )

    def __rich_measure__(self, console, options) -> Measurement:  # noqa: ARG002
        return Measurement(self._cols, self._cols)


class DirectImage(Widget):
    """一屏一张静态图，用 ``a=T`` 直接贴（不依赖 ``U=1`` 占位符）。

    尺寸由调用方通过 ``styles.width/height`` 指定（cell）；控件只负责在
    ``render`` 里把「空格 + 贴图序列」交给 Textual，滚出去 / 卸载时删图。
    """

    DEFAULT_CSS = """
    DirectImage {
        width: 1fr;
        height: 1fr;
    }
    """

    def __init__(self, image, *, image_id: int | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._payload = png_bytes(image)
        self.image_id = image_id if image_id is not None else next_image_id()
        self._cols = 0
        self._rows = 0
        self._sequence = ""

    # ---------- 渲染 ----------

    def _build(self, cols: int, rows: int) -> str:
        if cols == self._cols and rows == self._rows and self._sequence:
            return self._sequence
        self._cols, self._rows = cols, rows
        self._sequence = tmux_wrap(
            display_sequence(self._payload, cols=cols, rows=rows, image_id=self.image_id)
        )
        return self._sequence

    def render(self):
        # ``outer_size`` 直接读 ``_size``：无边框/padding 时就是内容区，且测试
        # 里不用跑一遍布局就能确定尺寸。
        size = self.outer_size
        cols = max(1, int(size.width))
        rows = max(1, int(size.height))
        sequence = self._build(cols, rows)
        if not sequence:
            return ""
        return _DirectRenderable(cols, rows, sequence)

    def on_unmount(self) -> None:
        """占用的图 id 必须还回去，否则终端里会一直攒着旧页。"""
        _write(tmux_wrap(delete_sequence(self.image_id)))

    def set_image(self, image) -> None:
        """换一张图：同一个 id 会被终端覆盖，旧页不会留残影。"""
        self._payload = png_bytes(image)
        self._cols = self._rows = 0
        self._sequence = ""
        self.refresh()

    def on_resize(self) -> None:
        self._cols = self._rows = 0
        self.refresh()
