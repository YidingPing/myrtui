"""快捷键表（顶栏 ``?`` 悬停 / ``Ctrl-a ?`` 浮层的数据源，v0.22 / v0.29）。

单一数据源：浮层内容全部从这里生成，``app.py`` 只负责显隐/定位。
README 的键位表是手工同步的；``tests/test_v22.py`` 有一条防漂移用例，
保证 ``on_key`` 里新增前缀分支时不会忘了补进这里。

分组顺序按使用频率：全局前缀键 → 侧栏。v0.28 移除了 v0.21 的全屏预览层，
打开改走文件树右键菜单（直接注入当前 pane）；v0.36 以精简形态恢复
Markdown 只读预览（同样从右键菜单进入，不新增前缀键）。
"""
from __future__ import annotations

from dataclasses import dataclass

from rich.cells import cell_len
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static


@dataclass(frozen=True)
class Shortcut:
    """一行：键位 + 说明。"""

    keys: str
    desc: str


@dataclass(frozen=True)
class Section:
    """一组按键（标题 + 行）。"""

    title: str
    rows: tuple[Shortcut, ...]


SECTIONS: tuple[Section, ...] = (
    Section(
        "Ctrl-a 前缀",
        (
            Shortcut("d", "detach（前端退出，shell 保留）"),
            Shortcut("k", "结束会话（需确认）"),
            Shortcut("t", "新建 pane"),
            Shortcut("w", "关闭当前 pane"),
            Shortcut("1..4", "切换焦点 pane"),
            Shortcut("r", "重开已退出的 pane"),
            Shortcut("b", "折叠 / 展开侧栏"),
            Shortcut("s", "聚焦文件树"),
            Shortcut("g", "聚焦 Git 历史"),
            Shortcut("c", "复制选区（拖拽松手已自动复制）"),
            Shortcut("v", "粘贴（系统剪贴板优先）"),
            Shortcut("?", "显示 / 收起本浮层"),
        ),
    ),
    Section(
        "侧栏",
        (
            Shortcut("↑↓", "移动光标"),
            Shortcut("Enter", "打开菜单（文件）/ 展开目录"),
            Shortcut("Space", "打开菜单（任意行）"),
            Shortcut("Esc", "回到终端"),
            Shortcut("右键", "打开 / 预览 / 复制路径"),
        ),
    ),
)

FOOTER = "其余按键直接透传 shell"

# 键列宽度按 cell 计算（CJK 占 2 cell；与全仓 N9 口径一致）
KEY_WIDTH = max(cell_len(row.keys) for section in SECTIONS for row in section.rows)

# 内容最大宽度（cell）：行首 1 + 键列 + 2 空格 + 说明
CONTENT_WIDTH = max(
    1 + KEY_WIDTH + 2 + cell_len(row.desc)
    for section in SECTIONS
    for row in section.rows
)
# border 2 + padding 左右各 1 + 预留 1 列竖向滚动条（内容超高时出现）
HELP_WIDTH = CONTENT_WIDTH + 5


def render_shortcut_help() -> Text:
    """把 ``SECTIONS`` 渲染成浮层正文（单列、按 cell 对齐键列）。"""
    text = Text()
    for section_index, section in enumerate(SECTIONS):
        if section_index:
            text.append("\n")
        text.append(section.title, style="bold")
        text.append("\n")
        for row in section.rows:
            text.append(" ")
            text.append(row.keys, style="bold")
            text.append(" " * (KEY_WIDTH - cell_len(row.keys) + 2))
            text.append(row.desc)
            text.append("\n")
    text.append("\n")
    text.append(FOOTER, style="dim")
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
        self.border_title = "快捷键"

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
