"""快捷键表（顶栏 ``?`` 悬停 / ``Ctrl-a ?`` 浮层的数据源，v0.22 / v0.29）。

单一数据源：浮层内容全部从这里生成，``app.py`` 只负责显隐/定位。
README 的键位表是手工同步的；``tests/test_v22.py`` 有一条防漂移用例，
保证 ``on_key`` 里新增前缀分支时不会忘了补进这里。

分组顺序按使用频率：全局前缀键 → 侧栏。v0.28 移除了 v0.21 的全屏预览层，
打开改走文件树右键菜单（直接注入当前 pane）；v0.36 以精简形态恢复
Markdown 只读预览（同样从右键菜单进入，不新增前缀键）。

**v0.5 i18n**：表里存的是**词表 key**而不是文案——模块级 ``t()`` 会在
import 时把语言固化，而测试的 autouse 夹具晚于 import 执行（见
``myrtui/i18n.py`` 头注释）。宽度也必须按当前语言现算：英文比中文宽，
模块级常量算出来的是中文口径。
"""
from __future__ import annotations

from dataclasses import dataclass

from rich.cells import cell_len
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from myrtui.i18n import t


@dataclass(frozen=True)
class Shortcut:
    """一行：键位 + 说明（说明是词表 key，渲染时才翻译）。"""

    keys: str
    """键列字面量（``d`` / ``Enter`` / ``↑↓``）；本身就是文案时见 ``keys_key``。"""

    desc_key: str
    """说明的 i18n key。"""

    keys_key: str | None = None
    """键列也要翻译时（如「右键」）用这个 key，替代 ``keys``。"""

    def label(self) -> str:
        return t(self.keys_key) if self.keys_key else self.keys


@dataclass(frozen=True)
class Section:
    """一组按键（标题 key + 行）。"""

    id: str
    """稳定标识（``prefix`` / ``sidebar``）：测试与逻辑按它定位，**不要拿标题文案当地址**。"""

    title_key: str
    rows: tuple[Shortcut, ...]

    def title(self) -> str:
        return t(self.title_key)


SECTIONS: tuple[Section, ...] = (
    Section(
        id="prefix",
        title_key="help.section.prefix",
        rows=(
            Shortcut("d", "help.detach"),
            Shortcut("k", "help.kill"),
            Shortcut("t", "help.new_pane"),
            Shortcut("w", "help.close_pane"),
            Shortcut("1..4", "help.focus_pane"),
            Shortcut("r", "help.respawn"),
            Shortcut("b", "help.sidebar_toggle"),
            Shortcut("s", "help.focus_tree"),
            Shortcut("g", "help.focus_git"),
            Shortcut("c", "help.copy"),
            Shortcut("v", "help.paste"),
            Shortcut("?", "help.toggle_help"),
        ),
    ),
    Section(
        id="sidebar",
        title_key="help.section.sidebar",
        rows=(
            Shortcut("↑↓", "help.move"),
            Shortcut("Enter", "help.open_menu_file"),
            Shortcut("Space", "help.open_menu_any"),
            Shortcut("Esc", "help.back_to_terminal"),
            Shortcut("", "help.right_click", keys_key="help.key.right_click"),
        ),
    ),
)


def key_width() -> int:
    """键列宽度（cell）：按当前语言现算，CJK 占 2 cell。"""
    return max(cell_len(row.label()) for section in SECTIONS for row in section.rows)


def content_width() -> int:
    """内容最大宽度（cell）：行首 1 + 键列 + 2 空格 + 说明。"""
    width = key_width()
    return max(
        1 + width + 2 + cell_len(t(row.desc_key))
        for section in SECTIONS
        for row in section.rows
    )


def help_width() -> int:
    """浮层总宽：border 2 + padding 左右各 1 + 预留 1 列竖向滚动条。"""
    return content_width() + 5


def render_shortcut_help() -> Text:
    """把 ``SECTIONS`` 渲染成浮层正文（单列、按 cell 对齐键列）。"""
    width = key_width()
    text = Text()
    for section_index, section in enumerate(SECTIONS):
        if section_index:
            text.append("\n")
        text.append(section.title(), style="bold")
        text.append("\n")
        for row in section.rows:
            label = row.label()
            text.append(" ")
            text.append(label, style="bold")
            text.append(" " * (width - cell_len(label) + 2))
            text.append(t(row.desc_key))
            text.append("\n")
    text.append("\n")
    text.append(t("help.footer"), style="dim")
    return text


class ShortcutHelp(VerticalScroll, can_focus=False):
    """屏幕级快捷键浮层（非模态，不抢焦点）。

    ``can_focus=False``：浮层显示期间打字 / ``Ctrl-a`` 前缀全部照旧，
    「一切键透传内层 shell」的承诺不被破坏。

    ``VerticalScroll`` 而不是 ``Static``：内容约 28 行，24 行终端上要能
    滚轮阅读——与 ``#commit-card`` 同一套做法（正文放内部 Static，外层
    负责限高与滚动）。
    """

    def compose(self) -> ComposeResult:
        yield Static(render_shortcut_help(), id="shortcut-help-body")

    def on_mount(self) -> None:
        self.border_title = t("help.title")

    def _on_enter(self, event) -> None:
        super()._on_enter(event)
        cancel = getattr(self.app, "_cancel_shortcut_help_hide", None)
        if cancel is not None:
            cancel()

    def _on_leave(self, event) -> None:
        super()._on_leave(event)
        # 浮层内部（border ↔ 正文）之间移动也会冒泡 Leave；坐标仍在本控件
        # 内就当作没离开（同 CommitCard 的判断口径）。
        if self.region.contains(*self.app.mouse_position):
            return
        hide = getattr(self.app, "_hide_shortcut_help", None)
        if hide is not None:
            hide()
