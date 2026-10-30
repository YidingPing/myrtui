"""单文件图片预览（v0.4；v0.4.3 起支持同目录切换）。

只做“打开一张图片看看”：不联网、不缩放原文件、不做目录画廊。解码 /
缩放 / alpha 预合成全部在 worker 里完成（B1 同款约束），事件循环只挂
``textual-image`` 控件。

导航范围是**同一个目录**：按文件名（`name.lower()`）排序，与文件树
（``DirectoryTree`` 的 ``(not is_dir, name.lower())``）同序——在预览里按
方向键的先后，就是文件树里从上到下的先后。子目录不进循环。

没有 Sixel / TGP 时不显示图片，但仍给出图片元信息与可执行提示；图片超过
``imageprep`` 的 10 MB / 4000² 限制、缺 Pillow、解码失败都只影响这一张，
仍可继续翻。
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from myrtui import pane_commands
from myrtui.client import graphics as graphics_mod, imageprep
from myrtui.client.mdpreview import (
    DEFAULT_BACKGROUND,
    DEFAULT_FOREGROUND,
    _display_cells,
    _image_widget_class,
    _make_image_widget,
)
from myrtui.i18n import t

log = logging.getLogger("myrtui.imagepreview")

HEADER_ID = "imagepreview-header"
SCROLL_ID = "imagepreview-scroll"
PLACEHOLDER_ID = "imagepreview-placeholder"
FOOTER_ID = "imagepreview-footer"

RESIZE_DEBOUNCE = 0.4


@dataclass(frozen=True)
class LoadedImage:
    """一次图片预览读盘 / 解码的结果。"""

    prepared: imageprep.Prepared | None
    size: int = 0
    error: str | None = None


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size / (1024 * 1024):.1f} MiB"


def sibling_images(path: str | Path) -> list[Path]:
    """同目录的图片列表（worker）：按 ``name.lower()`` 排，与文件树同序。

    当前文件不在结果里（刚被删 / 未被扫描到）时按序插回，保证 ``index`` 始终
    合法——预览不该因为文件消失就卡死或跳号。
    """
    target = Path(path)
    entries: list[Path] = []
    try:
        with os.scandir(target.parent) as scan:
            for entry in scan:
                try:
                    if not entry.is_file():
                        continue
                except OSError:  # 权限 / 竞态：单条跳过
                    continue
                if pane_commands.is_image(entry.name):
                    entries.append(Path(entry.path))
    except OSError:
        return [target]
    entries.sort(key=lambda item: item.name.lower())
    if not any(item.name == target.name for item in entries):
        entries.append(target)
        entries.sort(key=lambda item: item.name.lower())
    return entries


def load_image(
    path: str | Path,
    *,
    background: str,
    max_width_px: int,
    max_height_px: int,
    cell: tuple[int, int],
) -> LoadedImage:
    """worker 专用：读盘 + 解码 + 预合成；任何失败都转成可读原因。"""
    target = Path(path)
    try:
        size = target.stat().st_size
    except OSError as exc:
        return LoadedImage(None, error=t("img.read_failed", reason=exc.strerror or exc))
    prepared = imageprep.prepare_one(
        str(target),
        target.name,
        base_dir=target.parent,
        background=background,
        max_width_px=max_width_px,
        max_height_px=max_height_px,
        cell=cell,
    )
    if not prepared.ok:
        return LoadedImage(prepared, size=size, error=prepared.note or t("img.decode_failed"))
    return LoadedImage(prepared, size=size)


class ImagePreviewScreen(ModalScreen[str | None], inherit_bindings=False):
    """图片预览（同目录 ←/→ 切换）。关闭时把最后看的那张路径传回去。"""

    BINDINGS = [
        Binding("escape", "close", show=False),
        Binding("q", "close", show=False),
        Binding("r", "reload", show=False),
        Binding("right", "next", show=False),
        Binding("n", "next", show=False),
        Binding("left", "previous", show=False),
        Binding("p", "previous", show=False),
    ]

    CSS = """
    ImagePreviewScreen {
        background: $surface;
        layout: vertical;
    }
    #imagepreview-header {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text;
        text-style: bold;
    }
    #imagepreview-scroll {
        height: 1fr;
        padding: 1 2;
        align-horizontal: center;
        scrollbar-size: 0 1;
    }
    #imagepreview-placeholder {
        padding: 1 2;
        color: $text-muted;
    }
    #imagepreview-footer {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        absolute: str,
        relative: str | None = None,
        *,
        graphics: graphics_mod.Capabilities | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._absolute = str(Path(absolute))
        self._relative = relative or self._absolute
        # 同目录切换后只剩文件名，目录前缀留在 header 里（子目录里的图不至于
        # 看起来像在根目录）。
        parent = Path(self._relative).parent
        if Path(self._relative).is_absolute() or str(parent) in ("", "."):
            self._prefix = ""
        else:
            self._prefix = parent.as_posix()
        self._graphics = graphics or graphics_mod.DISABLED
        self._resize_timer = None
        self._layout_retries = 0
        self._header_text = self._relative
        self._paths: list[Path] = []
        self._index = 0
        self._scanned = False

    @property
    def current(self) -> str:
        """当前图片的绝对路径（未扫描时退回归一化后的入参）。"""
        if self._paths:
            return str(self._paths[self._index])
        return self._absolute

    # ---------- 组装 ----------

    def compose(self) -> ComposeResult:
        yield Static(self._relative, id=HEADER_ID, markup=False)
        with VerticalScroll(id=SCROLL_ID):
            yield Static(t("img.loading"), id=PLACEHOLDER_ID, markup=False)
        yield Static(t("img.footer"), id=FOOTER_ID, markup=False)

    def on_mount(self) -> None:
        self.query_one(f"#{SCROLL_ID}", VerticalScroll).focus()
        # 等一次布局：viewport 尺寸决定解码时的最大像素。
        self.call_after_refresh(self._load)

    def on_unmount(self) -> None:
        if self._resize_timer is not None:
            self._resize_timer.stop()
            self._resize_timer = None

    # ---------- 加载 ----------

    @work(exclusive=True, group="imagepreview", exit_on_error=False)
    async def _load(self, path: Path | None = None, *, rescan: bool = False) -> None:
        target = Path(path) if path is not None else Path(self.current)
        if rescan or not self._scanned:
            try:
                paths = await asyncio.to_thread(sibling_images, target)
            except Exception:  # noqa: BLE001 —— 扫目录失败只影响切换
                log.debug("图片目录扫描失败", exc_info=True)
                paths = [target]
            self._paths = paths
            self._scanned = True
        if self._paths:
            self._index = next(
                (
                    index
                    for index, item in enumerate(self._paths)
                    if item.name == target.name
                ),
                0,
            )
            target = self._paths[self._index]
        self._show_loading()
        viewport = self._viewport()
        if viewport is None:
            if self._layout_retries < 1:
                self._layout_retries += 1
                self.call_after_refresh(self._load, target)
            return
        self._layout_retries = 0
        width_cells, height_cells = viewport
        cell = self._cell()
        colors = self._theme_colors()
        try:
            loaded = await asyncio.to_thread(
                load_image,
                str(target),
                background=colors[1],
                max_width_px=max(1, width_cells) * cell[0],
                max_height_px=max(1, height_cells) * cell[1],
                cell=cell,
            )
        except Exception:  # noqa: BLE001 —— 解码是增强，失败只显示提示
            log.debug("图片预览加载失败", exc_info=True)
            loaded = LoadedImage(None, error=t("img.load_failed"))
        if not self._alive():
            return
        self._absolute = str(target)
        self._update_header(loaded)
        if loaded.error is not None or loaded.prepared is None or not loaded.prepared.ok:
            await self._show_status_async(loaded.error or t("img.decode_failed"))
            return
        await self._mount_image(loaded.prepared, width_cells, height_cells, colors[1])
        self._set_footer(t("img.footer"))

    async def _mount_image(
        self,
        prepared: imageprep.Prepared,
        width_cells: int,
        height_cells: int,
        background: str,
    ) -> None:
        container = self.query_one(f"#{SCROLL_ID}", VerticalScroll)
        widget_cls = _image_widget_class(self._graphics)
        if widget_cls is None:
            await self._show_status_async(
                graphics_mod.blocked_reason(self._graphics)
            )
            return
        try:
            cells = _display_cells(
                prepared.image.width,
                prepared.image.height,
                self._graphics,
                max_width_cells=width_cells,
                max_rows=max(1, height_cells),
            )
            widget = _make_image_widget(
                widget_cls,
                prepared.image,
                background=background,
                width_cells=cells[0],
                height_cells=cells[1],
            )
            await container.remove_children()
            await container.mount(widget)
            # 换图后回到顶部：上一张的滚动位置对新图没有意义。
            container.scroll_home(animate=False)
        except Exception:  # noqa: BLE001 —— 控件构造失败只显示元信息
            log.debug("图片控件构造失败", exc_info=True)
            await self._show_status_async(t("img.widget_failed"))

    def _viewport(self) -> tuple[int, int] | None:
        try:
            container = self.query_one(f"#{SCROLL_ID}", VerticalScroll)
            width = int(container.size.width)
            height = int(container.size.height)
        except Exception:  # noqa: BLE001 —— 首帧布局未完成
            return None
        if width <= 0 or height <= 0:
            return None
        return max(1, width), max(1, height)

    def _cell(self) -> tuple[int, int]:
        return (
            self._graphics.cell_width or graphics_mod.DEFAULT_CELL_WIDTH,
            self._graphics.cell_height or graphics_mod.DEFAULT_CELL_HEIGHT,
        )

    def _theme_colors(self) -> tuple[str, str]:
        foreground, background = DEFAULT_FOREGROUND, DEFAULT_BACKGROUND
        try:
            variables = self.app.get_css_variables()
        except Exception:  # noqa: BLE001 —— 测试 / 卸载竞态
            return foreground, background
        return (
            str(variables.get("foreground") or foreground),
            str(variables.get("surface") or background),
        )

    # ---------- 状态 ----------

    def _update_header(self, loaded: LoadedImage) -> None:
        name = Path(self._absolute).name
        parts = [f"{self._prefix}/{name}" if self._prefix else name]
        if len(self._paths) > 1:
            parts.append(f"{self._index + 1}/{len(self._paths)}")
        if loaded.size:
            parts.append(_human_size(loaded.size))
        prepared = loaded.prepared
        if prepared is not None and prepared.original_size != (0, 0):
            width, height = prepared.original_size
            parts.append(f"{width}×{height}")
            if prepared.format:
                parts.append(prepared.format)
        self._header_text = " · ".join(parts)
        try:
            self.query_one(f"#{HEADER_ID}", Static).update(self._header_text)
        except Exception:  # noqa: BLE001 —— 卸载竞态
            pass

    def _show_loading(self) -> None:
        """只在占位还在时写“加载中…”，已挂上图片就保持原图（不闪）。"""
        try:
            self.query_one(f"#{PLACEHOLDER_ID}", Static).update(t("img.loading"))
        except Exception:  # noqa: BLE001 —— 图片已挂载 / 卸载竞态
            pass

    async def _show_status_async(self, message: str) -> None:
        container = self.query_one(f"#{SCROLL_ID}", VerticalScroll)
        await container.remove_children()
        await container.mount(Static(message, markup=False))
        self._set_footer(t("img.footer"))

    def _set_footer(self, text: str) -> None:
        try:
            self.query_one(f"#{FOOTER_ID}", Static).update(text)
        except Exception:  # noqa: BLE001 —— 卸载竞态
            pass

    def _alive(self) -> bool:
        try:
            return self.is_mounted and self.app.screen is self
        except Exception:  # noqa: BLE001 —— teardown 竞态
            return False

    # ---------- 交互 / resize ----------

    def action_close(self) -> None:
        self.dismiss(self.current)

    def action_reload(self) -> None:
        """``r``：重扫目录（新拖进来的图进入循环）+ 重载当前。"""
        self._scanned = False
        self._load(Path(self.current), rescan=True)

    def action_next(self) -> None:
        self._step(1)

    def action_previous(self) -> None:
        self._step(-1)

    def _step(self, delta: int) -> None:
        if len(self._paths) < 2:
            return  # 一张图：没有可切的目标，也别闪屏
        index = (self._index + delta) % len(self._paths)
        self._load(self._paths[index])

    def on_resize(self, event) -> None:  # noqa: ARG002 —— 只看尺寸
        if self._resize_timer is not None:
            self._resize_timer.stop()
        self._resize_timer = self.set_timer(RESIZE_DEBOUNCE, self._reload_for_resize)

    def _reload_for_resize(self) -> None:
        self._resize_timer = None
        if self._alive():
            self._load()
