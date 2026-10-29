"""文件树右键上下文菜单（v0.20）。

用 ``ModalScreen`` 承载：菜单期间独占键盘（``TerminalApp.on_key`` 首行的
``not isinstance(self.screen, MainScreen)`` 会早退，键不会透传进 pane）、
点菜单外直接关闭，关闭后由 App 回调把焦点还给文件树。

背景是**透明**的（``background: transparent``）——textual 8.2.8 的
``Screen.render`` 在 alpha < 1 时用 ``BackgroundScreen`` 把主屏合成回来，
因此不会出现默认 ``ModalScreen`` 的「底屏压暗 60%」。

定位复用 ``#commit-card`` 的同一套机制：``OptionList`` 设
``overlay: screen`` 脱离文档流 + 运行时 ``absolute_offset`` 指向右键位置，
CSS ``constrain: inside inflect`` 保证不越出屏幕边界。

菜单项是 ``(action_id, label)`` 数据表：选中后 ``dismiss(action_id)``，
App 只认 id、不解析文案；以后加动作只需在数据表里加一行。

v0.28 起文件行的动作是「查看 / 渲染 Markdown / 复制相对路径 / 复制绝对路径」；
v0.31 起「查看」升级为「打开」（按 ``$MYRTUI_EDITOR`` / ``$EDITOR`` /
``$VISUAL`` / micro / vim / vi 的顺序调起编辑器），打开直接注入当前 pane，
不再打开全屏预览层；「渲染 Markdown」已移除，Markdown 统一走应用内预览。
v0.28.1 起键盘也能调出：
``Enter``（文件行）/ ``Space``（任意行）由 ``FileTree`` 换算光标行坐标后
发同一条 ``PathContextRequested``，本屏不区分来源。
"""
from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.geometry import Offset
from textual.screen import ModalScreen
from textual.widgets import OptionList
from textual.widgets.option_list import Option


class ContextMenuScreen(ModalScreen[str | None]):
    """指针处的上下文菜单；返回选中的 option id，``None`` = 取消。

    关闭途径：Esc、点菜单外、选中某一项。菜单外点击在 **MouseDown** 上处理
    ——点空白处时 App 不记 ``_mouse_down_widget``，不会生成 Click；只有
    MouseDown 能可靠收到。
    """

    CSS = """
    ContextMenuScreen {
        background: transparent;
    }
    ContextMenuScreen > OptionList {
        overlay: screen;
        width: auto;
        min-width: 18;
        max-width: 40;
        max-height: 12;
        border: round $accent;
        background: $panel;
        color: $text;
        constrain: inside inflect;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "取消", show=False)]

    def __init__(self, items: list[tuple[str, str]], x: int, y: int, **kwargs):
        super().__init__(**kwargs)
        self._items = list(items)
        self._x = int(x)
        self._y = int(y)

    def compose(self) -> ComposeResult:
        yield OptionList(
            *(Option(label, id=action_id) for action_id, label in self._items)
        )

    def on_mount(self) -> None:
        menu = self.query_one(OptionList)
        menu.absolute_offset = Offset(self._x, self._y)
        if menu.option_count:
            menu.highlighted = 0
        menu.focus()

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        event.stop()
        self.dismiss(event.option.id)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        """点菜单外 → 关闭（点击不穿透到底下的树/panel）。"""
        menu = self.query_one(OptionList)
        if not menu.region.contains(event.screen_x, event.screen_y):
            event.stop()
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
