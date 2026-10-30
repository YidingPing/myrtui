"""顶栏：pane 标签、+、Detach、Kill、?（DESIGN.md §4 / §12 P1 多 pane）。

P1 起标签是 1..4 个可点击 tab（点击等价 Ctrl-a N 切焦点），`+` 在满 4 时禁用。
按钮/tab 用可点击 Static 实现：Button 控件在 height=1 的顶栏内无法渲染文字。
点击事件经 ButtonPressed 风格的自定义消息冒泡到 App。

v0.22：`Kill` 右侧新增 `?`，悬停时由 App 弹出快捷键浮层。按钮把鼠标
Enter/Leave 转成 ``HoverStart`` / ``HoverEnd`` 消息（所有按钮通用，App 只
关心 ``btn-help``），避免 topbar 直接依赖 App 的私有方法。
"""
from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import Label, Static

from myrtui import __version__
from myrtui.i18n import t

# §12：1..4 布局档 → 最多 4 个 tab（与 app.MAX_PANES 对应，刻意独立成字面量
# 避免 topbar ↔ app 循环导入；两者同源于设计文档的 4 pane 上限）
MAX_TABS = 4

# 顶栏右上角品牌：程序名 + 包版本（源码真值；发版时与 pyproject 同步 bump）
BRAND_TEXT = f"myrtui {__version__}"


class TopBarButton(Static):
    """一行高、可点击的顶栏按钮。"""

    CAN_FOCUS = True

    class Pressed(Message):
        def __init__(self, button_id: str):
            super().__init__()
            self.button_id = button_id

    class HoverStart(Message):
        """指针进入按钮（v0.22：`?` 悬停浮层的触发信号）。"""

        def __init__(self, button_id: str):
            super().__init__()
            self.button_id = button_id

    class HoverEnd(Message):
        """指针离开按钮（v0.22：浮层延迟隐藏的触发信号）。"""

        def __init__(self, button_id: str):
            super().__init__()
            self.button_id = button_id

    def __init__(self, label: str, button_id: str, disabled: bool = False):
        super().__init__(label, id=button_id)
        self.disabled_btn = disabled
        if disabled:
            self.add_class("disabled")

    @property
    def is_disabled(self) -> bool:
        return self.disabled_btn

    def set_disabled(self, disabled: bool) -> None:
        self.disabled_btn = disabled
        self.set_class(disabled, "disabled")

    def on_mouse_down(self, event) -> None:
        event.stop()
        if not self.disabled_btn:
            self.post_message(self.Pressed(self.id or ""))

    def _on_enter(self, event: events.Enter) -> None:
        super()._on_enter(event)
        if event.node is self:
            self.post_message(self.HoverStart(self.id or ""))

    def _on_leave(self, event: events.Leave) -> None:
        super()._on_leave(event)
        if event.node is self:
            self.post_message(self.HoverEnd(self.id or ""))


class PaneTab(Static):
    """顶栏 pane 标签：显示 ``N:cmd``/``N:dead``，点击切焦点（等价 Ctrl-a N）。"""

    CAN_FOCUS = True

    class Pressed(Message):
        def __init__(self, index: int):
            super().__init__()
            self.index = index

    def __init__(self, index: int):
        super().__init__("", id=f"tab-{index}", classes="pane-tab")
        self.index = index
        self._body = ""
        self._dead = False
        self._active = False

    def set_state(self, label: str, dead: bool, active: bool) -> None:
        body = f" {label} "
        if body != self._body:
            self._body = body
            self.update(body)
        if dead != self._dead:
            self._dead = dead
            self.set_class(dead, "dead")
        if active != self._active:
            self._active = active
            self.set_class(active, "active")

    def on_mouse_down(self, event) -> None:
        event.stop()
        self.post_message(self.Pressed(self.index))


class TopBar(Static):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._status = ""

    def compose(self) -> ComposeResult:
        for i in range(MAX_TABS):
            yield PaneTab(i)
        yield TopBarButton(" + ", "btn-new")
        yield TopBarButton(f" {t('topbar.detach')} ", "btn-detach")
        yield TopBarButton(f" {t('topbar.kill')} ", "btn-kill")
        yield TopBarButton(" ? ", "btn-help")
        yield Label("", id="topbar-status")
        yield Label(BRAND_TEXT, id="topbar-brand")

    def set_tabs(self, labels: list[str], deads: list[bool], active: int) -> None:
        """按当前拓扑刷新 1..4 个标签：多余的隐藏，缺的显示。

        标签文本调用方已做防闪烁（仅变更时才进到这里不现实，本方法内部
        对 body/dead/active 各自全等比较，无 diff 不触发 update）。
        """
        for i in range(MAX_TABS):
            tab = self.query_one(f"#tab-{i}", PaneTab)
            visible = i < len(labels)
            if visible != tab.display:
                tab.display = visible
            if visible:
                tab.set_state(
                    labels[i],
                    dead=i < len(deads) and deads[i],
                    active=i == active,
                )
        new_btn = self.query_one("#btn-new", TopBarButton)
        want_disabled = len(labels) >= MAX_TABS
        if new_btn.is_disabled != want_disabled:
            new_btn.set_disabled(want_disabled)

    def set_status(self, text: str) -> None:
        """顶部状态提示（如「与会话失去连接」）。"""
        if text != self._status:
            self._status = text
            self.query_one("#topbar-status", Label).update(text)
