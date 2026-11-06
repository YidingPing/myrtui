"""「帧后直写」的静态图：Kitty 直接贴图（``a=T``）与 Sixel。

为什么需要：``textual-image`` 的 TGP **只走占位符**（虚拟放置 + ``U+10EEEE``）。
实测某手机 SSH app 能画 ``a=T``、也回 ``i=..;OK``，却不答 ``a=q`` 查询、也不实现
``U=1`` —— 占位符那条路只能画出一屏字符。这里改用「发图即显示」，像素就有了。

**为什么不走 Textual 的 render()**（0.12 实测）：把 ``\\x1b_G...`` 塞进 renderable
的 segment 里，Textual 8 会按**单元格宽度**裁剪整行——一段 70 字符的贴图序列在
60 列终端上会被砍掉尾巴（``\\x1b\\\\`` 和 ``\\x1b8`` 没了），终端永远等不到 APC
结束符，表现就是「白屏」。``textual-image`` 的 sixel 用的是同样的写法，同样会踩
（DCS 的 ``ST`` 被切 → 终端等不到结束）。所以两种协议都改成：``render()`` 只铺
空格占位，等这一帧画完（``call_after_refresh``）再**直接写终端**：``ESC7`` 存光标
→ ``CSI r;cH`` 定位到控件左上角 → 贴图 → ``ESC8`` 还原。Kitty 在 tmux 里整段包
进 ``ESC Ptmux;``（含光标移动），这样 tmux 自己的屏幕模型完全不受影响；sixel 在
复用器里本来就被检测层关掉（tmux 只会画一整屏 ``+``），不需要包装。

为什么只给「一屏一张静态图」的界面（``v`` 整页视图、图片预览）：直贴/直写出来的
图**不跟随单元格滚动**。Kitty 还能用 ``a=p`` 重新放置，sixel 连这个动作都没有，
Markdown 正文里滚动的图片因此继续走 termtex / alt。
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import random
import sys
from collections import OrderedDict
from itertools import count
from pathlib import Path

from textual import work
from textual.widget import Widget

log = logging.getLogger("myrtui.directimage")

_TMUX_PREFIX = "\x1bPtmux;"
_TMUX_SUFFIX = "\x1b\\"

REASSERT_DELAY = 0.25
"""贴图之后再补一次 ``a=p`` 放置的延迟：给可能跟上来的重画留一帧时间。"""

SIXEL_CACHE_SIZE = 4
"""sixel 编码结果的 LRU 条数：整屏 2520×1260 一条 ≈250 KiB，4 条够翻页来回。"""

_ids = count(random.randint(1, 1 << 31))
_tokens = count(1)
"""SixelPaint 的图片身份号：``PIL.Image`` 不可哈希（也没有值语义），用它当键。"""


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


def place_sequence(image_id: int, *, cols: int, rows: int) -> str:
    """``a=p``：把**已经传过**的图重新放置一次（几十字节，不重传数据）。

    用来兜「贴完又被下一帧擦掉」的偶发情况：重新放置一次就够了。
    """
    return (
        f"\x1b_Ga=p,i={image_id},c={max(1, int(cols))},r={max(1, int(rows))},"
        f"C=1,q=2\x1b\\"
    )


def _write(text: str) -> None:
    try:
        stdout = sys.__stdout__
        if stdout is None or not stdout.isatty():
            return  # 测试 / 重定向：别把转义序列写进捕获的 stdout
        stdout.write(text)
        stdout.flush()
    except Exception:  # noqa: BLE001 —— 退出竞态 / 已关的 fd：删不掉图不影响功能
        log.debug("直写终端失败", exc_info=True)


# ---------- sixel：编码（worker）+ 小 LRU ----------


def background_rgba(color: str | None) -> tuple[int, int, int, float]:
    """``#rrggbb`` → ``textual_image._sixel`` 要的背景元组（alpha 0–1 浮点）。

    颜色只用来把半透明像素预合成到底色上；解析失败按黑底（与 textual-image
    的默认值一致），不抛异常。
    """
    value = (color or "").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(char * 2 for char in value)
    try:
        return (
            int(value[0:2], 16),
            int(value[2:4], 16),
            int(value[4:6], 16),
            1.0,
        )
    except (ValueError, IndexError):
        return (0, 0, 0, 1.0)


class _ImageRef:
    """PIL 图的可哈希引用：按**对象身份**比较，并让缓存键持有强引用。

    ``PIL.Image.Image`` 定义了 ``__eq__`` 却没有 ``__hash__``（不可作字典键），
    而按内容哈希整屏像素又要 10 ms 级的时间。这里按 ``id`` 哈希、按 ``is``
    比较：键活着的时候图不会被回收，id 也就不会被复用。
    """

    __slots__ = ("image",)

    def __init__(self, image) -> None:
        self.image = image

    def __hash__(self) -> int:
        return id(self.image)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _ImageRef) and other.image is self.image


def _sixel_key(
    image, *, token: int, cols: int, rows: int, cell, background: str
) -> tuple:
    """缓存键：图片内容 + 画布格数 + cell 像素 + 背景色。

    路径 / 字节按内容（换 widget 也能命中，翻页来回不重编码）；``PIL.Image``
    按控件给的 ``token``（每换一张图递增），**不把大图钉在缓存里**。
    """
    if isinstance(image, (bytes, bytearray)):
        core: tuple = ("bytes", hashlib.sha1(bytes(image)).hexdigest())
    elif isinstance(image, (str, Path)):
        core = ("path", os.fspath(image))
    else:
        core = ("pil", int(token))
    return (
        core,
        max(1, int(cols)),
        max(1, int(rows)),
        max(1, int(cell[0])),
        max(1, int(cell[1])),
        str(background or ""),
    )


_SIXEL_CACHE: "OrderedDict[tuple, str | None]" = OrderedDict()
_SIXEL_INFLIGHT: set[tuple] = set()


def cache_lookup(key: tuple) -> tuple[bool, str | None]:
    """``(命中?, DCS)``；命中但 DCS 为 ``None`` 表示这张编码失败，别再试。"""
    try:
        sequence = _SIXEL_CACHE[key]
    except KeyError:
        return False, None
    _SIXEL_CACHE.move_to_end(key)
    return True, sequence


def store_sixel(key: tuple, sequence: str | None) -> None:
    _SIXEL_CACHE[key] = sequence
    _SIXEL_CACHE.move_to_end(key)
    while len(_SIXEL_CACHE) > SIXEL_CACHE_SIZE:
        _SIXEL_CACHE.popitem(last=False)


def clear_sixel_cache() -> None:
    """测试钩子：清掉编码缓存与在途标记。"""
    _SIXEL_CACHE.clear()
    _SIXEL_INFLIGHT.clear()


def encode_sixel(
    image, *, cols: int, rows: int, cell, background: str
) -> str | None:
    """**worker 专用**：图片 → 完整 sixel DCS；依赖缺失 / 编码失败 → ``None``。

    用 ``textual-image`` 的私有 API（``_pixeldata`` + ``_sixel``，pyproject 已 pin
    ``>=0.14,<0.16``）。整屏 2520×1260 实测 ≈380 ms，必须在线程里跑。
    """
    try:
        from textual_image._pixeldata import PixelData  # noqa: PLC0415 —— 私有 API，版本已 pin
        from textual_image._sixel import image_to_sixels  # noqa: PLC0415
    except Exception:  # noqa: BLE001 —— 依赖被卸载 / 私有 API 漂移
        log.debug("textual-image 不可用，sixel 停止编码", exc_info=True)
        return None
    try:
        data = PixelData(image)
        width = max(1, int(cols)) * max(1, int(cell[0]))
        height = max(1, int(rows)) * max(1, int(cell[1]))
        if data.width != width or data.height != height:
            data = data.scaled(width, height)
        return image_to_sixels(
            data.pil_image, background=background_rgba(background)
        )
    except Exception:  # noqa: BLE001 —— 坏图 / 量化失败只影响这一张
        log.debug("sixel 编码失败", exc_info=True)
        return None


# ---------- 控件骨架 ----------


class StaticImage(Widget):
    """一屏一张静态图的共同骨架：``render()`` 只铺空格，图像帧后直写。

    子类负责 ``paint()``（怎么写终端）与换图（``set_image``）。
    """

    DEFAULT_CSS = """
    StaticImage {
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
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._image = image
        self._cells: tuple[int, int] | None = (cols, rows) if cols and rows else None
        self._cols = 0
        self._rows = 0

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
        """只铺空格：图像在 ``paint`` 里直接写终端（见模块头）。"""
        cols, rows = self._box()
        return "\n".join(" " * cols for _ in range(rows))

    # ---------- 直写排程 ----------

    def paint(self) -> None:  # pragma: no cover —— 子类实现
        raise NotImplementedError

    def _paint_soon(self) -> None:
        try:
            self.call_after_refresh(self.paint)
        except Exception:  # noqa: BLE001 —— 未挂载 / 退出竞态
            log.debug("直写排程失败", exc_info=True)

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


class DirectImage(StaticImage):
    """一屏一张静态图，用 ``a=T`` 直接贴（不依赖 ``U=1`` 占位符）。

    尺寸由 ``cols`` / ``rows`` 指定（cell；也用于 ``styles``）。控件本身只铺
    空格占位，图像在帧画完后直接写终端。
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
        super().__init__(image, cols=cols, rows=rows, **kwargs)
        self._payload = png_bytes(image)
        self.image_id = image_id if image_id is not None else next_image_id()
        self._retired: list[int] = []
        self._sequence = ""
        self._reassert_timer = None

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
        body = ""
        if self._retired:
            # **先清空旧图，再贴新图**。真机三次试验的结论：
            #   · 同 id 重传 → 不重绘（画面停在第一页）；
            #   · 贴完再删 → 整屏空白（这个实现把 `a=d` 当「清空所有图」）；
            #   · 完全不删 → 第二张图不出现（它一次只留一张）。
            # 所以顺序只能是：删除 → 传输。
            body += "".join(delete_sequence(i) for i in self._retired)
            self._retired.clear()
        body += f"\x1b7\x1b[{row};{column}H{sequence}\x1b8"
        _write(tmux_wrap(body))
        self._reassert_soon()

    def assert_visible(self) -> None:
        """重新放置一次（``a=p``）：偶发「贴完被下一帧擦掉」靠它兜底。"""
        region = self.region
        if region.width <= 0 or region.height <= 0:
            return
        cols, rows = self._box()
        body = (
            f"\x1b7\x1b[{region.y + 1};{region.x + 1}H"
            f"{place_sequence(self.image_id, cols=cols, rows=rows)}\x1b8"
        )
        _write(tmux_wrap(body))

    def _reassert_soon(self) -> None:
        try:
            if not self.is_mounted:
                return
            if self._reassert_timer is not None:
                self._reassert_timer.stop()
            self._reassert_timer = self.set_timer(REASSERT_DELAY, self.assert_visible)
        except Exception:  # noqa: BLE001 —— 未挂载 / 退出竞态
            log.debug("直贴兜底排程失败", exc_info=True)

    def on_unmount(self) -> None:
        """一次性还掉所有用过的图 id（翻页不删，卸载时清账）。"""
        if self._reassert_timer is not None:
            self._reassert_timer.stop()
            self._reassert_timer = None
        ids = [self.image_id, *self._retired]
        self._retired.clear()
        _write(tmux_wrap("".join(delete_sequence(i) for i in ids)))

    def set_image(self, image) -> None:
        """换一张图：**换新 id** 并重贴，但**不** ``refresh()``。

        真机反证：把控件标脏会触发一次「重画空格」，这个 app 的空白单元格会把
        已经贴上的图擦掉（按 `v` 首帧能看见、翻页就没了、开关软键盘触发 resize
        后又能看见——全对得上）。控件内容本来就是空格，换图不需要重画。
        """
        self._retired.append(self.image_id)
        self.image_id = next_image_id()
        self._image = image
        self._payload = png_bytes(image)
        self._cols = self._rows = 0
        self._sequence = ""
        self._paint_soon()


class SixelPaint(StaticImage):
    """一屏一张静态图，用完整 sixel DCS 帧后直写（不经过 Rich segment）。

    与 :class:`DirectImage` 同一套骨架；差别都在 sixel 本身的限制里：

    - 编码走 ``textual-image._sixel``（私有 API，已 pin）。整屏编码 ≈380 ms，
      所以**进 worker 线程**，未完成时这一帧保持空格，完成后补画；
    - 编码结果进小 LRU（键含图片内容、格数、cell 像素、背景色），翻回同一页
      0 编码、0 事件循环阻塞；
    - sixel 没有「复用已编码图像」的动作（Kitty 有 ``a=p``），滚动后无法把图挪
      到新位置 → 只给整页视图与图片预览，正文里滚动的图片继续回落 ``alt``。

    DCS 直接写终端：``ESC7`` + ``CSI r;cH`` + DCS + ``ESC8``。tmux 里 sixel 本来
    就被检测层关掉，不做 passthrough 包装。
    """

    def __init__(
        self,
        image,
        *,
        cols: int = 0,
        rows: int = 0,
        **kwargs,
    ) -> None:
        super().__init__(image, cols=cols, rows=rows, **kwargs)
        self._cell: tuple[int, int] | None = None
        self._image_token = next(_tokens)
        """每换一张图 +1：PIL 图不可哈希，用它进缓存键。"""

    # ---------- 尺寸 ----------

    def set_cell(self, width: int, height: int) -> None:
        """画布用的 cell 像素尺寸（来自终端探测；不传则向 textual-image 要）。"""
        cell = (max(1, int(width)), max(1, int(height)))
        if self._cell == cell:
            return
        self._cell = cell
        self._paint_soon()

    def _cell_size(self) -> tuple[int, int]:
        if self._cell is not None:
            return self._cell
        try:
            from textual_image._terminal import get_cell_size  # noqa: PLC0415 —— 私有 API

            size = get_cell_size()
            return (max(1, int(size.width)), max(1, int(size.height)))
        except Exception:  # noqa: BLE001 —— 没依赖 / 未探测：按 VT340 兜底
            return (10, 20)

    def _background(self) -> str:
        try:
            return str(self.styles.background.hex)
        except Exception:  # noqa: BLE001 —— 未挂载 / 样式未解析
            return "#000000"

    def _key(self, cols: int, rows: int) -> tuple:
        return _sixel_key(
            self._image,
            token=self._image_token,
            cols=cols,
            rows=rows,
            cell=self._cell_size(),
            background=self._background(),
        )

    # ---------- 直写 ----------

    def paint(self) -> None:
        region = self.region
        if region.width <= 0 or region.height <= 0:
            return
        cols, rows = self._box()
        cell = self._cell_size()
        background = self._background()
        key = _sixel_key(
            self._image,
            token=self._image_token,
            cols=cols,
            rows=rows,
            cell=cell,
            background=background,
        )
        found, sequence = cache_lookup(key)
        if not found:
            self._encode_soon(key, cols, rows, cell, background)
            return
        if not sequence:
            return  # 这张图编码失败过：保持空格，别每帧重试
        _write(f"\x1b7\x1b[{region.y + 1};{region.x + 1}H{sequence}\x1b8")

    def _encode_soon(
        self, key: tuple, cols: int, rows: int, cell, background: str
    ) -> None:
        if key in _SIXEL_INFLIGHT:
            return
        _SIXEL_INFLIGHT.add(key)
        try:
            self._encode(key, cols, rows, cell, background)
        except Exception:  # noqa: BLE001 —— 未挂载（单测直接 paint）
            _SIXEL_INFLIGHT.discard(key)
            log.debug("sixel 编码排程失败", exc_info=True)

    @work(thread=True, group="sixel-paint", exit_on_error=False)
    def _encode(
        self, key: tuple, cols: int, rows: int, cell, background: str
    ) -> None:
        """worker：编码 + 回投（``call_from_thread`` 保证写终端发生在事件循环）。"""
        try:
            sequence = self._encode_image(cols, rows, cell, background)
            self.app.call_from_thread(self._encoded, key, sequence)
        except Exception:  # noqa: BLE001 —— 卸载竞态 / 退出：丢掉这次结果
            _SIXEL_INFLIGHT.discard(key)
            log.debug("sixel 编码回投失败", exc_info=True)

    def _encode_image(
        self, cols: int, rows: int, cell, background: str
    ) -> str | None:
        """编码入口（单测替换这里注入假 DCS）。"""
        return encode_sixel(
            self._image,
            cols=cols,
            rows=rows,
            cell=cell,
            background=background,
        )

    def _encoded(self, key: tuple, sequence: str | None) -> None:
        _SIXEL_INFLIGHT.discard(key)
        store_sixel(key, sequence)
        if not self.is_mounted:
            return
        try:
            # 编码期间可能换了图 / resize 过：只有键还对得上才补画。
            cols, rows = self._box()
            if self._key(cols, rows) == key:
                self.paint()
        except Exception:  # noqa: BLE001 —— 卸载竞态
            log.debug("sixel 补画失败", exc_info=True)

    def set_image(self, image) -> None:
        """换一张图并重画，但**不** ``refresh()``（同 :meth:`DirectImage.set_image`）。"""
        self._image = image
        self._image_token = next(_tokens)
        self._paint_soon()
