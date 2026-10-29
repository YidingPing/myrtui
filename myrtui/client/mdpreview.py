"""Markdown 全屏只读预览（v0.36；v0.37 增加 Sixel/TGP 位图）。

设计边界（``myrtui-internal/docs/V36-MARKDOWN-PREVIEW-PLAN.md`` / ``myrtui-internal/docs/V37-SIXEL-GRAPHICS-PLAN.md``）：

- **全屏 ``ModalScreen``**：不占 pane、不改 tmux 后端；打开期间复用
  ``TerminalApp.on_key`` 首行的 ``MainScreen`` 闸门，键不会泄进内层 shell；
- **只读渲染**：Textual ``Markdown`` + GFM；链接点击拦截（不导航）；
  不提供源码视图 / 搜索 / 编辑 / 复制 / PDF 导出；
- **数学**：``\\$...\\$`` / ``\\$\\$...\\$\\$`` 由 dollarmath token 接管；行内走
  ``termtex -inline`` 的 Unicode 文本，块级优先 LaTeX 位图（有 Sixel/TGP 且
  渲染成功），否则 ``termtex -width`` 的二维字符排版，再退 LaTeX 原文；
- **文档图片**：独立成段的 ``![alt](path)`` 走位图（仅本地路径、不联网），
  行内图片仍是文本；失败显示 ``🖼 alt``；
- **B1**：读盘、定界符归一化、termtex / pdflatex 子进程、图片解码全部在
  worker 线程；事件循环上的 ``MathBlock`` / ``ImageBlock`` 只查缓存，**绝不
  spawn**；
- **透明**：Tabby 的 Sixel P2=1 语义是「不画、露出控件背景色」，所以图片与
  公式 PNG 都在 worker 里**预合成到块背景色**，控件背景也钉成同一个颜色
  （计划 §3.4 F4）；
- **上限**：入口与屏内各有一道 512 KiB 闸门（屏内那道防「菜单打开到 worker
  跑到读盘之间文件被替换」的竞态）；
- 预览层自身不调用 ``backend.*``。
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from markdown_it.token import Token as MarkdownToken
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Markdown, Static

from myrtui import fileview
from myrtui.client import graphics as graphics_mod
from myrtui.client import imageprep, mathrender
from myrtui.client.mathtext import (
    inline_math_tokens,
    math_parser_factory,
    normalize_math_delimiters,
)

# v0.37 起数学 / 图片块是普通 Widget，**不再依赖** Textual 的私有
# ``MarkdownBlock``（v0.36 需要继承它）。Markdown.update 接受
# ``unhandled_token`` 返回的任意 Widget，只要求它是 ``Widget``。

log = logging.getLogger("myrtui.mdpreview")

MARKDOWN_PREVIEW_MAX_BYTES = 512_000
"""Markdown 预览的入口上限（512 KiB）；超过提示用「打开」。"""

FOOTER = "Esc 返回 · r 重载 · v LaTeX 排版 · ↑↓/PgUp/PgDn 滚动"
LINK_NOTICE = "预览不打开链接"
IMAGE_ALT_PREFIX = "🖼 "

DEFAULT_FOREGROUND = "#E0E0E0"
DEFAULT_BACKGROUND = "#1E1E1E"
"""无 App 上下文（测试 / 探测失败）时的主题色兜底。"""


def math_content(text: str) -> Content:
    """块级公式文本 → ``Content``（块级输出的唯一入口）。

    - ``Content(text)`` 本身是**纯文本**（textual 8.2.8 不解析 markup），矩阵
      输出的 ``[a b]`` 不会被当成样式标签；
    - 多行输出按**显示宽度**补齐到等宽：MathBlock 居中，逐行各自居中都
      会打散二维对齐（分式横线与分子/分母错位）。
    """
    lines = text.split("\n")
    if len(lines) <= 1:
        return Content(text)
    cells = [Content(line).cell_length for line in lines]
    target = max(cells)
    padded = [line + " " * (target - cell) for line, cell in zip(lines, cells)]
    return Content("\n".join(padded))


def _math_width(markdown) -> int:
    """``MathBlock`` 查缓存用的宽度——必须与预渲染同一个夹取函数。

    两处各写一份的话，diff 一格就会永远 miss、termtex 静默失效。
    """
    try:
        return mathrender.clamp_width(markdown.app.size.width)
    except Exception:  # noqa: BLE001 —— 未挂载 / 私有 API 漂移时给安全值
        return mathrender.MIN_WIDTH


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size / (1024 * 1024):.1f} MiB"


def _image_widget_class(capabilities: graphics_mod.Capabilities):
    """按能力挑控件类；``textual-image`` 缺失 / 导入失败 → ``None``。

    只在这里 import ``textual_image``：CI 不装该依赖，模块级 import 会让
    整个预览层不可用。
    """
    if not capabilities.bitmaps:
        return None
    try:
        from textual_image.widget import SixelImage, TGPImage  # noqa: PLC0415
    except Exception:  # noqa: BLE001 —— 依赖被卸载 / 私有 API 漂移
        log.debug("textual-image 不可用，图片回落 alt", exc_info=True)
        return None
    return SixelImage if capabilities.sixel else TGPImage


def _display_cells(
    width_px: int,
    height_px: int,
    capabilities: graphics_mod.Capabilities,
    max_width_cells: int,
    max_rows: int = imageprep.MAX_ROWS,
    scale: float = 1.0,
) -> tuple[int, int]:
    """像素尺寸 → 单元格盒子：只缩小不放大、高度 ≤``max_rows``。

    宽高都**向上取整**：盒子永远不小于图片，显示时只需要「等比放进去 +
    补背景边」，绝不会被 `textual-image` 拉满盒子造成非等比拉伸。
    """
    cell_w = max(1, int(capabilities.cell_width or 10))
    cell_h = max(1, int(capabilities.cell_height or 20))
    width_px = max(1, int(width_px) * max(0.01, scale))
    height_px = max(1, int(height_px) * max(0.01, scale))
    limit = max(1, int(max_width_cells))

    shrink = min(1.0, (limit * cell_w) / width_px)
    target_w = width_px * shrink
    target_h = height_px * shrink
    if target_h > max_rows * cell_h:
        tight = (max_rows * cell_h) / target_h
        target_w *= tight
        target_h *= tight
    width_cells = max(1, min(limit, -(-round(target_w) // cell_w)))
    height_cells = max(1, -(-round(target_h) // cell_h))
    return width_cells, height_cells


def _available_width_cells(markdown) -> int:
    """正文字宽（cells）：与 termtex 同口径，再扣掉块 padding。"""
    try:
        width = markdown.app.size.width
    except Exception:  # noqa: BLE001 —— 未挂载 / 测试替身
        return 80
    return max(10, mathrender.clamp_width(width) - 4)


def _make_image_widget(
    widget_cls,
    image,
    *,
    background: str,
    width_cells: int | None = None,
    height_cells: int | None = None,
    max_rows: int = imageprep.MAX_ROWS,
) -> Widget:
    """构造位图控件：按像素尺寸换算成明确的 cell 宽高（不放大）。

    背景色必须与「预合成用的背景色」一致：Tabby 的 Sixel P2=1 语义是
    「不画、露出控件背景」，不一致就会出现异色方块（计划 §3.4 F4）。
    """
    widget = widget_cls(image)
    if width_cells and height_cells:
        widget.styles.width = int(width_cells)
        widget.styles.height = int(height_cells)
    else:
        # 拿不到像素尺寸时的兜底：自然尺寸 + 高度上限
        widget.styles.width = "auto"
        widget.styles.height = "auto"
        widget.styles.max_height = max_rows
    widget.styles.background = background
    return widget


def _image_token(src: str, alt: str):
    """独立图片段落 → 自定义 token（交给 ``unhandled_token`` 变 Widget）。"""
    token = MarkdownToken("myrtui_image", "", 0)
    token.content = src
    token.info = alt
    return token


def _rewrite_image_paragraphs(tokens):
    """把「整段只有一个图片」替换成 :func:`_image_token`。

    行内图片（段落里还有正文）不动，仍由文本流显示 alt。
    """
    out = []
    index = 0
    total = len(tokens)
    while index < total:
        token = tokens[index]
        if (
            token.type == "paragraph_open"
            and index + 2 < total
            and tokens[index + 1].type == "inline"
            and tokens[index + 2].type == "paragraph_close"
        ):
            inline = tokens[index + 1]
            children = [
                child
                for child in (inline.children or [])
                if not (child.type == "text" and not child.content.strip())
            ]
            if len(children) == 1 and children[0].type == "image":
                image = children[0]
                src = image.attrGet("src") or ""
                if src:
                    out.append(_image_token(src, image.content or ""))
                    index += 3
                    continue
        out.append(token)
        index += 1
    return out


def _background_of(markdown) -> str:
    _color, background = getattr(
        markdown, "graphics_colors", (DEFAULT_FOREGROUND, DEFAULT_BACKGROUND)
    )
    return background


class MathBlock(Vertical):
    """块级公式：termtex 二维文本，失败时回落 LaTeX 原文。"""

    can_focus = False

    DEFAULT_CSS = """
    MathBlock {
        width: 1fr;
        height: auto;
        margin: 1 0;
        padding: 1 2;
        align-horizontal: center;
        background: $surface;
        color: $text;
    }
    """

    def __init__(self, markdown, token, source_range=None, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._markdown_ref = markdown
        self._token = token
        self.source_range = source_range

    def compose(self) -> ComposeResult:
        # 只查表 / 回落原文：事件循环上绝不 spawn（B1）。
        rendered = mathrender.render_block_cached(
            self._token.content, width=_math_width(self._markdown_ref)
        )
        text = rendered.text
        if rendered.tag:
            text = f"{text}    ({rendered.tag})"
        yield Static(math_content(text))


class ImageBlock(Vertical):
    """文档图片：位图优先，否则 ``🖼 alt``；绝不吞掉前后段落。"""

    can_focus = False

    DEFAULT_CSS = """
    ImageBlock {
        width: 1fr;
        height: auto;
        margin: 1 0;
        padding: 0 2;
        align-horizontal: center;
        background: $surface;
        color: $text-muted;
    }
    """

    def __init__(self, markdown, src: str, alt: str = "") -> None:
        super().__init__()
        self._markdown_ref = markdown
        self._src = src
        self._alt = alt or src

    def compose(self) -> ComposeResult:
        markdown = self._markdown_ref
        capabilities = getattr(markdown, "graphics", graphics_mod.DISABLED)
        prepared = getattr(markdown, "prepared_images", {}).get(self._src)
        widget_cls = _image_widget_class(capabilities)
        note = getattr(prepared, "note", None)
        if widget_cls is not None and prepared is not None and prepared.ok:
            try:
                width_cells, height_cells = _display_cells(
                    prepared.image.width,
                    prepared.image.height,
                    capabilities,
                    _available_width_cells(markdown),
                )
                yield _make_image_widget(
                    widget_cls,
                    prepared.image,
                    background=_background_of(markdown),
                    width_cells=width_cells,
                    height_cells=height_cells,
                )
                return
            except Exception:  # noqa: BLE001 —— 单图失败只回落 alt
                log.debug("图片位图控件构造失败：%s", self._src, exc_info=True)
        label = IMAGE_ALT_PREFIX + self._alt
        if note:
            label = f"{label}（{note}）"
        yield Static(label, markup=False)


class MathMarkdown(Markdown):
    """在 Textual Markdown 的 token 流里接管数学公式与独立图片。"""

    def _parse_markdown(self, tokens):
        tokens = _rewrite_image_paragraphs(list(tokens))
        for token in tokens:
            if token.type == "inline" and token.children:
                children = []
                for child in token.children:
                    if child.type in ("math_inline", "math_inline_double"):
                        children.extend(
                            inline_math_tokens(
                                child, mathrender.render_inline_cached
                            )
                        )
                    else:
                        children.append(child)
                token.children = children
        yield from super()._parse_markdown(tokens)

    def unhandled_token(self, token):
        if token.type in ("math_block", "math_block_label"):
            return MathBlock(self, token)
        if token.type == "myrtui_image":
            return ImageBlock(self, token.content, token.info or "")
        return super().unhandled_token(token)


class MarkdownPreviewScreen(ModalScreen[None], inherit_bindings=False):
    """Markdown 全屏只读预览（不依赖 pane）。"""

    RESIZE_DEBOUNCE = 0.4
    """改宽度后等这么久再按新宽度重排 termtex 公式。"""

    BINDINGS = [
        Binding("escape", "close", show=False),
        Binding("q", "close", show=False),
        Binding("r", "reload", show=False),
        Binding("v", "paged", show=False),
    ]

    CSS = """
    MarkdownPreviewScreen {
        background: $surface;
        layout: vertical;
    }
    #mdpreview-header {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text;
        text-style: bold;
    }
    #mdpreview-scroll {
        height: 1fr;
        padding: 0 1;
    }
    #mdpreview-placeholder {
        padding: 1 2;
        color: $text-muted;
    }
    #mdpreview-footer {
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
        self._graphics = graphics or graphics_mod.DISABLED
        self._text = ""
        self._restore_scroll: int | None = None
        self._notice_timer = None
        self._resize_timer = None
        self._render_width = 0
        self._markdown = self._make_markdown()

    # ---------- 组装 ----------

    def _make_markdown(self) -> Markdown:
        markdown = MathMarkdown(
            parser_factory=math_parser_factory,
            open_links=False,
            id="mdpreview-md",
        )
        markdown.graphics = self._graphics
        markdown.graphics_colors = (DEFAULT_FOREGROUND, DEFAULT_BACKGROUND)
        markdown.prepared_images = {}
        return markdown

    def compose(self) -> ComposeResult:
        yield Static(
            self._relative, id="mdpreview-header", markup=False
        )
        with VerticalScroll(id="mdpreview-scroll"):
            yield Static("渲染中…", id="mdpreview-placeholder", markup=False)
            yield self._markdown
        yield Static(FOOTER, id="mdpreview-footer", markup=False)

    def on_mount(self) -> None:
        self._markdown.display = False
        self.query_one("#mdpreview-scroll", VerticalScroll).focus()
        self._load()

    def on_unmount(self) -> None:
        if self._notice_timer is not None:
            self._notice_timer.stop()
            self._notice_timer = None
        if self._resize_timer is not None:
            self._resize_timer.stop()
            self._resize_timer = None

    def on_resize(self, event) -> None:  # noqa: ARG002 —— 只看尺寸
        """宽度变了 → termtex 块级缓存键失效，防抖后按新宽度重排。"""
        if not self._text.strip():
            return
        if mathrender.clamp_width(self.app.size.width) == self._render_width:
            return
        if self._resize_timer is not None:
            self._resize_timer.stop()
        self._resize_timer = self.set_timer(
            self.RESIZE_DEBOUNCE, self._reload_for_resize
        )

    def _reload_for_resize(self) -> None:
        self._resize_timer = None
        if not self._alive():
            return
        scroll = self.query_one("#mdpreview-scroll", VerticalScroll)
        self._restore_scroll = int(scroll.scroll_y)
        self._load()

    # ---------- 加载 ----------

    @work(exclusive=True, group="mdpreview", exit_on_error=False)
    async def _load(self) -> None:
        await self._read_and_render()

    async def _read_and_render(self) -> None:
        self._set_loading()
        try:
            size = os.path.getsize(self._absolute)
        except OSError:
            # 文件被删 / 无权限：不在这里判死，交给 read_for_preview 出可读原因。
            size = None
        if size is not None and size > MARKDOWN_PREVIEW_MAX_BYTES:
            self._show_message("Markdown 过大（>512 KiB），请用「打开」")
            self._restore_scroll = None
            return

        preview = await asyncio.to_thread(fileview.read_for_preview, self._absolute)
        if not self._alive():
            return
        self._update_header(preview)
        if preview.error is not None:
            self._show_message(preview.error)
            self._restore_scroll = None
            return
        if preview.binary:
            self._show_message("二进制文件，暂不支持预览")
            self._restore_scroll = None
            return
        if not preview.text.strip():
            self._show_message("空文件")
            self._restore_scroll = None
            return

        normalized = await asyncio.to_thread(
            normalize_math_delimiters, preview.text
        )
        if not self._alive():
            return
        width = mathrender.clamp_width(self.app.size.width)
        self._render_width = width
        try:
            warning = await asyncio.to_thread(
                mathrender.prewarm_document, normalized, width
            )
        except Exception:  # noqa: BLE001 —— 数学是增强，失败不影响正文
            log.debug("mdpreview prewarm failed", exc_info=True)
            warning = None
        if not self._alive():
            return

        colors = self._theme_colors()
        self._markdown.graphics_colors = colors
        images: dict = {}
        if self._graphics.bitmaps:
            try:
                images = await asyncio.to_thread(
                    self._prewarm_images,
                    normalized,
                    colors,
                    width,
                )
            except Exception:  # noqa: BLE001 —— 位图是增强，失败只回落 alt
                log.debug("mdpreview image prewarm failed", exc_info=True)
        if not self._alive():
            return
        self._markdown.prepared_images = images

        self._text = normalized
        try:
            await self._markdown.update(normalized)
        except Exception:  # noqa: BLE001 —— 渲染失败只提示，可 r 重试
            log.warning("mdpreview render failed", exc_info=True)
            self._show_message("Markdown 渲染失败（r 可重试）")
            self._restore_scroll = None
            return
        if not self._alive():
            return

        self._show_content()
        if warning:
            self._notice(warning, seconds=6.0)
        else:
            self._set_footer(FOOTER)
        self._restore_after_reload()

    def _theme_colors(self) -> tuple[str, str]:
        """主题前景 / 背景色（``#RRGGBB``）：图片预合成底色。"""
        foreground, background = DEFAULT_FOREGROUND, DEFAULT_BACKGROUND
        try:
            variables = self.app.get_css_variables()
        except Exception:  # noqa: BLE001 —— 测试 / 卸载竞态
            return foreground, background
        foreground = str(variables.get("foreground") or foreground)
        background = str(variables.get("surface") or background)
        return foreground, background

    def _prewarm_images(
        self,
        text: str,
        colors: tuple[str, str],
        width_cells: int,
    ) -> dict:
        """worker：本地图片位图（只返回内存结果，不碰 UI）。"""
        _foreground, background = colors
        caps = self._graphics
        cell = (caps.cell_width, caps.cell_height)
        # 预热可选依赖的 import（`textual_image.widget` 顶层会调 get_cell_size）；
        # 放 worker 里避免第一次 compose 时在事件循环上卡一下。
        try:
            import textual_image.widget  # noqa: F401, PLC0415
        except Exception:  # noqa: BLE001 —— 没装就回落文本
            pass
        return imageprep.prepare_document(
            text,
            base_dir=str(Path(self._absolute).parent),
            background=background,
            max_width_px=max(1, width_cells) * cell[0],
            max_height_px=imageprep.MAX_ROWS * cell[1],
            cell=cell,
        )

    def _alive(self) -> bool:
        """worker 回投后预览层是否仍在最上层（N3：Esc 可能已关掉它）。"""
        try:
            return self.is_mounted and self.app.screen is self
        except Exception:  # noqa: BLE001 —— teardown 竞态
            return False

    # ---------- 视图状态 ----------

    def _placeholder(self) -> Static:
        return self.query_one("#mdpreview-placeholder", Static)

    def _footer(self) -> Static:
        return self.query_one("#mdpreview-footer", Static)

    def _set_loading(self) -> None:
        self._markdown.display = False
        placeholder = self._placeholder()
        placeholder.update("渲染中…")
        placeholder.display = True

    def _show_message(self, message: str) -> None:
        self._markdown.display = False
        placeholder = self._placeholder()
        placeholder.update(message)
        placeholder.display = True
        self._notice(message, seconds=6.0)

    def _show_content(self) -> None:
        self._markdown.display = True
        self._placeholder().display = False

    def _update_header(self, preview: fileview.Preview) -> None:
        header = self.query_one("#mdpreview-header", Static)
        suffix = ""
        if preview.size:
            suffix = f" · {_human_size(preview.size)}"
        header.update(f"{self._relative} · Markdown{suffix}")

    def _set_footer(self, text: str) -> None:
        try:
            self._footer().update(text)
        except Exception:  # noqa: BLE001 —— 卸载竞态
            pass

    def _notice(self, text: str, seconds: float = 3.0) -> None:
        if self._notice_timer is not None:
            self._notice_timer.stop()
        self._set_footer(text)
        self._notice_timer = self.set_timer(seconds, self._restore_footer)

    def _restore_footer(self) -> None:
        self._notice_timer = None
        self._set_footer(FOOTER)

    def _restore_after_reload(self) -> None:
        restore = self._restore_scroll
        self._restore_scroll = None
        if not restore:
            return
        scroll = self.query_one("#mdpreview-scroll", VerticalScroll)
        self.call_after_refresh(
            lambda: scroll.scroll_to(y=restore, animate=False)
        )

    # ---------- 动作 ----------

    def action_close(self) -> None:
        self.dismiss(None)

    def action_reload(self) -> None:
        scroll = self.query_one("#mdpreview-scroll", VerticalScroll)
        self._restore_scroll = int(scroll.scroll_y)
        self._load()

    def action_paged(self) -> None:
        """``v``：整篇交给浏览器排版 → PDF 分页 → 位图滚动（只读逃生口）。"""
        if not self._graphics.bitmaps:
            self._notice(graphics_mod.blocked_reason(self._graphics), seconds=6.0)
            return
        if not self._text.strip():
            self._notice("文档还没渲染好")
            return
        from myrtui.client.pageview import PagedDocScreen  # noqa: PLC0415 —— 避免循环 import

        self.app.push_screen(
            PagedDocScreen(
                self._absolute,
                text=self._text,
                graphics=self._graphics,
                relative=self._relative,
            )
        )

    def on_markdown_link_clicked(self, event: Markdown.LinkClicked) -> None:
        """链接不导航（F1：textual 8.2.8 沿 MRO 分发，必须 prevent_default）。"""
        event.prevent_default()
        event.stop()
        self._notice(LINK_NOTICE)
