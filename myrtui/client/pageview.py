"""LaTeX 排版视图：Markdown → 分页位图（只读，按整页翻）。

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
import time
from pathlib import Path

from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from myrtui.client import directimage, docpage, graphics as graphics_mod
from myrtui.client.overlay import QuietWhenCovered
from myrtui.i18n import t

log = logging.getLogger("myrtui.pageview")

HEADER_ID = "paged-header"
SCROLL_ID = "paged-scroll"
PLACEHOLDER_ID = "paged-placeholder"

RESIZE_DEBOUNCE = 0.5
"""resize 之后等这么久再重渲染（拖窗口时不要每帧都印 PDF）。"""

WHEEL_COOLDOWN = 0.12
"""滚轮翻页冷却（秒）：触控板一次滑动会连发十几个事件，不该连翻十几页。"""


class _PageScroll(VerticalScroll):
    """整页滚动区：静止时永远只有**一张完整页**可见。

    为什么不能自由滚动（0.6.1）：页面是整屏 Sixel 位图，``textual-image``
    的重编码缓存键包含 ``crop``——滚动一格就换一次 crop，于是每一步都要把
    上下两张整屏图重新编码（2520×1260 实测 ≈380 ms/张，而且发生在**事件循环**
    上）；键位默认还带动画，一次 ``PgDn`` 会渲染 10 余帧、每帧都重编码 →
    画面闪、响应迟钝（一次翻页实测 12 次编码 / 1.5 s）。

    一页本来就等于一屏，吸附到页边界后静止状态只有一张完整页可见，crop 恒定
    → 缓存命中 → 滚动 0 次编码。代价是没有「半页」过渡，换来的是可用的响应。
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._wheel_at = 0.0

    # 翻页状态只留在 Screen 一份（header 也归它刷），这里只负责把键 / 滚轮
    # 翻译成「第几页」。用鸭子类型的探测而不是 import：滚动区不反向依赖
    # PagedDocScreen，单独拿去做控件测试时也不会炸。
    def _owner(self):
        try:
            screen = self.screen
        except Exception:  # noqa: BLE001 —— 卸载竞态：滚轮事件可能还在队列里
            return None
        return screen if hasattr(screen, "goto_page") else None

    def _step(self, delta: int) -> None:
        owner = self._owner()
        if owner is not None:
            owner.goto_page(owner.page_index + delta)

    # 「一行」和「一页」在这里是同一个动作：任何中间偏移都会造出半页。
    def action_scroll_up(self) -> None:
        self._step(-1)

    def action_scroll_down(self) -> None:
        self._step(+1)

    def action_page_up(self) -> None:
        self._step(-1)

    def action_page_down(self) -> None:
        self._step(+1)

    def action_scroll_home(self) -> None:
        owner = self._owner()
        if owner is not None:
            owner.goto_page(0)

    def action_scroll_end(self) -> None:
        owner = self._owner()
        if owner is not None:
            owner.goto_page(owner.page_count - 1)

    def _wheel(self, delta: int, event) -> None:
        # 一律吞掉：不吞的话父类会按 scroll_sensitivity 滚几行，造出半页。
        event.stop()
        now = time.monotonic()
        if now - self._wheel_at < WHEEL_COOLDOWN:
            return
        self._wheel_at = now
        self._step(delta)

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self._wheel(+1, event)

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._wheel(-1, event)


class PagedDocScreen(QuietWhenCovered, ModalScreen[None], inherit_bindings=False):
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
        self._page_index = 0
        self._rows = 0
        """一页占多少**终端行**（= ``_mount_pages`` 挂控件时用的高度）。

        翻页偏移必须用它算：用容器当下的高度会在 resize 中间态漂移，产生
        「半页」→ 整屏位图重新编码（见 :class:`_PageScroll`）。
        """
        self._direct = None
        """直贴模式（``a=T``）下唯一的页图控件；非直贴时为 ``None``。"""

    @property
    def page_index(self) -> int:
        """当前页下标（0 起）；还没挂页时为 0。"""
        return self._page_index

    @property
    def page_count(self) -> int:
        return len(self._pages)

    # ---------- 组装 ----------

    def compose(self) -> ComposeResult:
        yield Static(self._relative, id=HEADER_ID, markup=False)
        with _PageScroll(id=SCROLL_ID):
            yield Static(t("page.placeholder"), id=PLACEHOLDER_ID, markup=False)

    def on_mount(self) -> None:
        # 颜色放在 CSS 变量之外：Textual 的 CSS 不能插 Python 值，只能挂载后设。
        self._apply_palette()
        # 键盘翻页要求焦点在滚动区：本仓 ``App.AUTO_FOCUS = None``，从 Markdown
        # 预览按 ``v`` 进来时焦点还留在下层屏，``PgUp/PgDn`` 会整个失效。
        try:
            self.query_one(f"#{SCROLL_ID}", _PageScroll).focus()
        except Exception:  # noqa: BLE001 —— 未挂载 / 测试替身
            log.debug("滚动区聚焦失败", exc_info=True)
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
            self._set_status(t("page.placeholder"))
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
                    self._set_status(t("page.failed", exc=exc))
                except Exception:  # noqa: BLE001 —— 连提示都放不下就只留日志
                    log.debug("LaTeX 排版失败提示也失败", exc_info=True)

    async def _mount_pages(self, pages: list[Path], spec: docpage.PageSpec) -> None:
        widget_cls = self._widget_class()
        if widget_cls is None:
            self._set_status(t("page.no_bitmaps"))
            return
        if not pages:
            self._set_status(t("page.no_pages"))
            return
        self._pages = list(pages)
        container = self.query_one(f"#{SCROLL_ID}", VerticalScroll)
        # 盒子 = 滚动区本身（1 页 1 屏），不用 spec 反推，避免被最小尺寸夹过
        cols = max(1, int(container.size.width))
        rows = max(1, int(container.size.height))
        background = self._colors[1]
        if widget_cls is directimage.DirectImage:
            # 直贴（a=T）的图不跟单元格滚动：只挂**一个**控件，翻页时换图。
            widget = widget_cls(pages[0], cols=cols, rows=rows)
            widget.styles.width = cols
            widget.styles.height = rows
            widget.styles.background = background
            await container.remove_children()
            await container.mount(widget)
            self._direct = widget
            self._rows = rows
            container.scroll_home(animate=False)
            self._show_page(0)
            return
        widgets = []
        for path in pages:
            widget = widget_cls(path)
            widget.styles.width = cols
            widget.styles.height = rows
            widget.styles.background = background
            widgets.append(widget)
        await container.remove_children()
        await container.mount_all(widgets)
        self._rows = rows
        container.scroll_home(animate=False)
        self._page_index = 0
        self._set_header(len(pages))

    def _widget_class(self):
        from myrtui.client.mdpreview import _static_widget_class  # noqa: PLC0415 —— 避免循环 import

        return _static_widget_class(self._graphics)

    # ---------- 状态 ----------

    def _set_header(self, pages: int, page: int | None = None) -> None:
        position = (
            t("page.header_position", page=page, pages=pages)
            if page
            else t("page.header_total", pages=pages)
        )
        self._header_text = t(
            "page.header", name=self._relative, position=position
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
        self._page_index = 0
        self._rows = 0
        self._direct = None
        self.query_one(f"#{HEADER_ID}", Static).update(self._relative)

    # ---------- 交互 ----------

    def action_close(self) -> None:
        self.app.pop_screen()

    def action_reload(self) -> None:
        """``r``：丢掉缓存重渲染（换主题 / 怀疑渲染坏了时用）。"""
        self._load(refresh=True)

    def goto_page(self, index: int) -> None:
        """跳到第 ``index`` 页（0 起，自动夹取）：**整页**吸附、不动画。

        偏移用 ``_rows`` 而不是容器当前高度——resize 中间态里两者会短暂不一致，
        用错的那个就会落到半页位置，触发整屏 Sixel 重新编码（见 ``_PageScroll``）。
        """
        if not self._pages:
            return
        index = max(0, min(len(self._pages) - 1, int(index)))
        self._page_index = index
        if self._direct is not None:
            # 直贴：没有可滚动的第二屏，换图即可（同一 id 复用，旧页自动被替换）。
            self._direct.set_image(self._pages[index])
            self._show_page(index)
            return
        try:
            container = self.query_one(f"#{SCROLL_ID}", _PageScroll)
        except Exception:  # noqa: BLE001 —— 卸载竞态
            return
        rows = max(1, int(self._rows or container.size.height or 1))
        container.scroll_to(y=index * rows, animate=False, immediate=True)
        self._show_page(index)

    def _show_page(self, index: int) -> None:
        """刷新 header 的页号；``index`` 是 0 起下标。"""
        self._page_no = index + 1
        self._set_header(len(self._pages), index + 1)

    def on_resize(self, event) -> None:  # noqa: ARG002 —— 只看尺寸
        self.call_after_refresh(self._after_resize)

    def _after_resize(self) -> None:
        spec = self._spec_for()
        if spec is None or spec == self._spec:
            return
        self._spec = spec
        if self._timer is not None:
            self._timer.stop()
        self._set_status(t("page.resize"))
        self._timer = self.set_timer(RESIZE_DEBOUNCE, self._load)

    def on_update_scroll(self, message) -> None:  # noqa: ARG002 —— 只跟位置
        """Textual 的滚动通知（``UpdateScroll``）直接投给 Screen。

        通知可能早于 ``scroll_y`` 落值 → 等一帧再读。
        """
        self.call_after_refresh(self._update_page)

    def _update_page(self) -> None:
        """位置变了就同步页号（键盘 / 滚轮以外的来源同样走这里）。"""
        if self._direct is not None:
            return  # 直贴模式没有滚动偏移，页号只由 goto_page 改
        if not self._pages or self._rows <= 0:
            return
        try:
            container = self.query_one(f"#{SCROLL_ID}", _PageScroll)
        except Exception:  # noqa: BLE001 —— 卸载竞态
            return
        # 用目标位置（``scroll_target_y``）：动画期间 ``scroll_y`` 还在动；
        # 位置按页对齐后这里取整不会漂移。
        index = int(round(container.scroll_target_y / self._rows))
        index = max(0, min(len(self._pages) - 1, index))
        if index == self._page_index:
            return
        self._page_index = index
        self._show_page(index)
