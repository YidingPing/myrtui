"""LaTeX 排版视图：Markdown → 分页位图（只读，可滚动）。

文本预览的公式由 termtex 排成字符网格；这一屏把**整篇**交给浏览器
（Chrome + KaTeX）排版，正文与行内/块级公式同一套字体与基线。页面默认是
**米黄纸**（``#E6DABE`` / ``#3D3427``），可用 ``MYRTUI_PAGE_THEME`` 换成
``paper`` / ``gray`` / ``white`` / ``dim`` / ``dark``。

代价是这一屏里没有可选中文本、不能搜索、窗口尺寸变了要重渲染——所以它是
*逃生口*，不是默认阅读器：``v`` 打开，``Esc`` 回文本预览。

分页几何 = 终端可视区：``列 × cell_w`` × ``(行 - 1) × cell_h`` px，PDF 一页
= 一屏，``pdftocairo -r 96`` 出来 1:1 贴进 cell 网格。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from myrtui.client import docpage, graphics as graphics_mod

log = logging.getLogger("myrtui.pageview")

HEADER_ID = "paged-header"
SCROLL_ID = "paged-scroll"
PLACEHOLDER_ID = "paged-placeholder"

RESIZE_DEBOUNCE = 0.5
"""resize 之后等这么久再重渲染（拖窗口时不要每帧都印 PDF）。"""

EMPTY = "LaTeX 排版中…（Chrome 打印 PDF → 分页）"


class _PageScroll(VerticalScroll):
    """滚动区：只用来看页号（滚动位置变化时由 Screen 刷新 header）。"""


class PagedDocScreen(ModalScreen[None], inherit_bindings=False):
    """整篇 Markdown 的分页位图视图。"""

    BINDINGS = [
        Binding("escape", "close", show=False),
        Binding("q", "close", show=False),
        Binding("v", "close", show=False),
        Binding("r", "reload", show=False),
    ]

    CSS = """
    PagedDocScreen {
        layout: vertical;
    }
    #paged-header {
        height: 1;
        padding: 0 1;
        text-style: bold;
    }
    #paged-scroll {
        height: 1fr;
        /* 滚动条会吃掉 2 列，页面宽度就会和 PDF 页面差 2 列 → 被缩小重采样。
           这里索性不显示滚动条（页数在 header 里），保证 1:1。 */
        scrollbar-size-vertical: 0;
    }
    #paged-placeholder {
        padding: 1 2;
    }
    """

    def __init__(
        self,
        absolute: str,
        *,
        text: str,
        graphics: graphics_mod.Capabilities | None = None,
        relative: str | None = None,
        palette: docpage.PagePalette | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._absolute = str(Path(absolute))
        self._relative = relative or self._absolute
        self._text = text
        self._graphics = graphics or graphics_mod.DISABLED
        self._palette = palette or docpage.palette_from_env()
        self._colors = self._palette.colors
        self._spec: docpage.PageSpec | None = None
        self._pages: list[Path] = []
        self._timer = None
        self._header_text = self._relative
        self._page_no = 0

    # ---------- 组装 ----------

    def compose(self) -> ComposeResult:
        yield Static(self._relative, id=HEADER_ID, markup=False)
        with _PageScroll(id=SCROLL_ID):
            yield Static(EMPTY, id=PLACEHOLDER_ID, markup=False)

    def on_mount(self) -> None:
        # 颜色放在 CSS 变量之外：Textual 的 CSS 不能插 Python 值，只能挂载后设。
        self._apply_palette()
        # 等一次布局：页面宽度要用滚动区的**内容**宽（不是 app 宽）
        self.call_after_refresh(self._start)

    def _apply_palette(self) -> None:
        """把纸色铺到屏幕 / header / 滚动区 / 占位提示（否则四周仍是白）。"""
        palette = self._palette
        self.styles.background = palette.background
        self.styles.color = palette.foreground
        try:
            header = self.query_one(f"#{HEADER_ID}", Static)
            header.styles.background = palette.header_background
            header.styles.color = palette.foreground
            container = self.query_one(f"#{SCROLL_ID}", VerticalScroll)
            container.styles.background = palette.background
            placeholder = self.query_one(f"#{PLACEHOLDER_ID}", Static)
            placeholder.styles.background = palette.background
            placeholder.styles.color = palette.muted
        except Exception:  # noqa: BLE001 —— 挂载竞态：下一次状态刷新会补上
            log.debug("调色板应用失败", exc_info=True)

    def _start(self) -> None:
        spec = self._spec_for()
        if spec is None:
            return
        self._spec = spec
        self._load()

    def _spec_for(self) -> docpage.PageSpec | None:
        """滚动区尺寸 → 页面像素（1 页 = 1 屏，1:1 不缩放）。"""
        try:
            container = self.query_one(f"#{SCROLL_ID}", VerticalScroll)
        except Exception:  # noqa: BLE001 —— 还没挂上
            return None
        width = int(container.size.width)
        height = int(container.size.height)
        if width <= 0 or height <= 0:
            return None
        cell = (
            self._graphics.cell_width or graphics_mod.DEFAULT_CELL_WIDTH,
            self._graphics.cell_height or graphics_mod.DEFAULT_CELL_HEIGHT,
        )
        return docpage.page_spec(cell, max(20, width), max(10, height))

    # ---------- 加载 ----------

    @work(exclusive=True, group="paged", exit_on_error=False)
    async def _load(self, refresh: bool = False) -> None:
        spec = self._spec
        if spec is None:
            return
        try:
            self._set_status(EMPTY)
            if refresh:
                await asyncio.to_thread(
                    docpage.invalidate,
                    self._text,
                    source=self._absolute,
                    colors=self._colors,
                    spec=spec,
                )
            pages = await asyncio.to_thread(
                docpage.cached_pages,
                self._text,
                source=self._absolute,
                colors=self._colors,
                spec=spec,
            )
            if pages is None:
                pages = await asyncio.to_thread(
                    docpage.render_pages,
                    self._text,
                    source=self._absolute,
                    colors=self._colors,
                    spec=spec,
                    base_dir=str(Path(self._absolute).parent),
                )
            if not self.is_mounted:
                return
            await self._mount_pages(pages, spec)
        except Exception as exc:  # noqa: BLE001 —— 渲染失败只提示，不白屏
            log.debug("LaTeX 排版失败", exc_info=True)
            if self.is_mounted:
                try:
                    self._set_status(f"LaTeX 排版失败：{exc}（Esc 返回文本预览）")
                except Exception:  # noqa: BLE001 —— 连提示都放不下就只留日志
                    log.debug("LaTeX 排版失败提示也失败", exc_info=True)

    async def _mount_pages(self, pages: list[Path], spec: docpage.PageSpec) -> None:
        widget_cls = self._widget_class()
        if widget_cls is None:
            self._set_status("当前终端没有位图协议，LaTeX 排版不可用（Esc 返回）")
            return
        if not pages:
            self._set_status("没有可显示的页面（Esc 返回）")
            return
        self._pages = list(pages)
        container = self.query_one(f"#{SCROLL_ID}", VerticalScroll)
        # 盒子 = 滚动区本身（1 页 1 屏），不用 spec 反推，避免被最小尺寸夹过
        cols = max(1, int(container.size.width))
        rows = max(1, int(container.size.height))
        widgets = []
        background = self._colors[1]
        for path in pages:
            widget = widget_cls(path)
            widget.styles.width = cols
            widget.styles.height = rows
            widget.styles.background = background
            widgets.append(widget)
        await container.remove_children()
        await container.mount_all(widgets)
        container.scroll_home(animate=False)
        self._set_header(len(pages))

    def _widget_class(self):
        from myrtui.client.mdpreview import _image_widget_class  # noqa: PLC0415 —— 避免循环 import

        return _image_widget_class(self._graphics)

    # ---------- 状态 ----------

    def _set_header(self, pages: int, page: int | None = None) -> None:
        position = f"{page}/{pages} 页" if page else f"{pages} 页"
        self._header_text = (
            f"{self._relative} · LaTeX 排版 · {position} · Esc 返回 · r 重渲染"
        )
        self.query_one(f"#{HEADER_ID}", Static).update(self._header_text)

    def _set_status(self, message: str) -> None:
        """把滚动区换成一条提示（**不复用 ID**：``remove`` 是异步的）。"""
        container = self.query_one(f"#{SCROLL_ID}", VerticalScroll)
        for child in list(container.children):
            child.remove()
        status = Static(message, markup=False)
        status.styles.background = self._palette.background
        status.styles.color = self._palette.muted
        container.mount(status)
        self._header_text = self._relative
        self._page_no = 0
        self.query_one(f"#{HEADER_ID}", Static).update(self._relative)

    # ---------- 交互 ----------

    def action_close(self) -> None:
        self.app.pop_screen()

    def action_reload(self) -> None:
        """``r``：丢掉缓存重渲染（换主题 / 怀疑渲染坏了时用）。"""
        self._load(refresh=True)

    def on_resize(self, event) -> None:  # noqa: ARG002 —— 只看尺寸
        self.call_after_refresh(self._after_resize)

    def _after_resize(self) -> None:
        spec = self._spec_for()
        if spec is None or spec == self._spec:
            return
        self._spec = spec
        if self._timer is not None:
            self._timer.stop()
        self._set_status("尺寸变了，重新排版…")
        self._timer = self.set_timer(RESIZE_DEBOUNCE, self._load)

    def on_update_scroll(self, message) -> None:  # noqa: ARG002 —— 只跟位置
        """Textual 的滚动通知（``UpdateScroll``）直接投给 Screen。

        通知可能早于 ``scroll_y`` 落值 → 等一帧再读。
        """
        self.call_after_refresh(self._update_page)

    def _update_page(self) -> None:
        """滚动后刷新 header 里的页号（``2/3 页``）。"""
        if not self._pages or self._spec is None:
            return
        rows = max(1, self._spec.height // max(1, self._graphics.cell_height or 20))
        scroll = self.query_one(f"#{SCROLL_ID}", _PageScroll)
        # 用目标位置（``scroll_target_y``）：平滑滚动期间 ``scroll_y`` 还在动
        page = min(len(self._pages), int(scroll.scroll_target_y) // rows + 1)
        if page == self._page_no:
            return
        self._page_no = page
        self._set_header(len(self._pages), page)
