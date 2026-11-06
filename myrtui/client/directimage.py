"""Kitty 直接贴图（``a=T``）：给「能画图、但不支持 Unicode 占位符」的终端。

为什么需要：``textual-image`` 的 TGP **只走占位符**（虚拟放置 + ``U+10EEEE``）。
实测某手机 SSH app 能画 ``a=T``、也回 ``i=..;OK``，却不答 ``a=q`` 查询、也不实现
``U=1`` —— 占位符那条路只能画出一屏字符。这里改用「发图即显示」，像素就有了。

代价：**图不跟随单元格滚动**。所以只给「一屏一张静态图」的界面用（``v`` 整页
视图、图片预览），Markdown 正文里滚动的公式/图片继续走 termtex / alt。

**为什么不走 Textual 的 render()**（0.12 实测）：把 ``\\x1b_G...`` 塞进 renderable
的 segment 里，Textual 8 会按**单元格宽度**裁剪整行——一段 70 字符的贴图序列在
60 列终端上会被砍掉尾巴（``\\x1b\\\\`` 和 ``\\x1b8`` 没了），终端永远等不到 APC
结束符，表现就是「白屏」。``textual-image`` 的 sixel 用的是同样的写法，同样会踩。
所以改成：``render()`` 只铺空格占位，等这一帧画完（``call_after_refresh``）再
**直接写终端**：``ESC7`` 存光标 → ``CSI r;cH`` 定位到控件左上角 → 贴图 →
``ESC8`` 还原。在 tmux 里整段包进 ``ESC Ptmux;``（含光标移动），这样 tmux 自己的
屏幕模型完全不受影响。
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

from textual.widget import Widget

log = logging.getLogger("myrtui.directimage")

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


class DirectImage(Widget):
    """一屏一张静态图，用 ``a=T`` 直接贴（不依赖 ``U=1`` 占位符）。

    尺寸由 ``cols`` / ``rows`` 指定（cell；也用于 ``styles``）。控件本身只铺
    空格占位，图像在帧画完后直接写终端。
    """

    DEFAULT_CSS = """
    DirectImage {
        width: 1fr;
        height: 1fr;
    }
    """

    def __init__(
        self,
        image,
        *,
        cols: int = 0,
        rows: int = 0,
        image_id: int | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._payload = png_bytes(image)
        self.image_id = image_id if image_id is not None else next_image_id()
        self._retired: list[int] = []
        self._cells: tuple[int, int] | None = (cols, rows) if cols and rows else None
        self._cols = 0
        self._rows = 0
        self._sequence = ""

    # ---------- 布局 / 渲染 ----------

    def set_cells(self, cols: int, rows: int) -> None:
        """固定贴图盒子（cell）：调用方知道确切尺寸，别依赖 layout 时机。"""
        cols, rows = max(1, int(cols)), max(1, int(rows))
        if self._cells == (cols, rows):
            return
        self._cells = (cols, rows)
        self._cols = self._rows = 0
        self.refresh()

    def _box(self) -> tuple[int, int]:
        if self._cells is not None:
            return self._cells
        region = self.region
        return max(1, int(region.width)), max(1, int(region.height))

    def render(self):
        """只铺空格：图像在 ``_paint`` 里直接写终端（见模块头）。"""
        cols, rows = self._box()
        return "\n".join(" " * cols for _ in range(rows))

    # ---------- 贴图 ----------

    def _build(self, cols: int, rows: int) -> str:
        if cols == self._cols and rows == self._rows and self._sequence:
            return self._sequence
        self._cols, self._rows = cols, rows
        self._sequence = display_sequence(
            self._payload, cols=cols, rows=rows, image_id=self.image_id
        )
        return self._sequence

    def paint(self) -> None:
        """把图贴到控件左上角（在 tmux 里整段走 passthrough）。"""
        region = self.region
        if region.width <= 0 or region.height <= 0:
            return
        cols, rows = self._box()
        sequence = self._build(cols, rows)
        row, column = region.y + 1, region.x + 1
        # 翻页时**不删旧图**：有的实现把 `a=d` 当成「清空所有图」而不看 `i=`，
        # 删一次就会把刚贴上的新页一起抹掉（真机现象：翻页后整屏空白）。
        # 页面不透明且贴在同一格，新页自然盖住旧页；旧 id 统一在卸载时清理。
        _write(tmux_wrap(f"\x1b7\x1b[{row};{column}H{sequence}\x1b8"))

    def _paint_soon(self) -> None:
        try:
            self.call_after_refresh(self.paint)
        except Exception:  # noqa: BLE001 —— 未挂载 / 退出竞态
            log.debug("直贴排程失败", exc_info=True)

    def on_mount(self) -> None:
        self._paint_soon()

    def on_show(self) -> None:  # pragma: no cover —— 被弹窗盖住再回来时补一帧
        self._paint_soon()

    def on_resize(self) -> None:
        region = self.region
        if region.width and region.height:
            self._cells = (int(region.width), int(region.height))
        self._cols = self._rows = 0
        self._paint_soon()

    def on_unmount(self) -> None:
        """一次性还掉所有用过的图 id（翻页不删，卸载时清账）。"""
        ids = [self.image_id, *self._retired]
        self._retired.clear()
        _write(tmux_wrap("".join(delete_sequence(i) for i in ids)))

    def set_image(self, image) -> None:
        """换一张图：**换新 id**（有终端对同 id 重传不重绘，画面会停在第一页）。

        旧 id 只记下来、不当场删——见 :meth:`paint` 的注释。
        """
        self._retired.append(self.image_id)
        self.image_id = next_image_id()
        self._payload = png_bytes(image)
        self._cols = self._rows = 0
        self._sequence = ""
        self.refresh()
        self._paint_soon()
