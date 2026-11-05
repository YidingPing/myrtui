"""顶栏：pane 标签、+、Detach、Kill、?（DESIGN.md §4 / §12 P1 多 pane）。

P1 起标签是 1..4 个可点击 tab（点击等价 Ctrl-a N 切焦点），`+` 在满 4 时禁用。
按钮/tab 用可点击 Static 实现：Button 控件在 height=1 的顶栏内无法渲染文字。
点击事件经 ButtonPressed 风格的自定义消息冒泡到 App。

v0.22：`Kill` 右侧新增 `?`，悬停时由 App 弹出快捷键浮层。按钮把鼠标
Enter/Leave 转成 ``HoverStart`` / ``HoverEnd`` 消息（所有按钮通用，App 只
关心 ``btn-help``），避免 topbar 直接依赖 App 的私有方法。

0.10.0 紧凑档（``set_compact``）：tab 只留序号（``1 2 3``），按钮文案压成
``+ X D K KEY ?``，品牌由 CSS 隐藏（``Screen.compact #topbar-brand``）。0.11.0 增加
``KEY`` 开关（底栏显隐），紧凑顶栏仍为 1 行：宽度 = 4×3 + 6×3 = 30 列，
40 列屏还剩 10 格给状态。46 列上 `?` 被挤出屏外是 0.9.x 的真机问题（见内部
0.10.0 计划 §0）。
"""
from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import Label, Static

from myrtui import __version__
from myrtui.client.flash import flash_press
from myrtui.i18n import t

# §12：1..4 布局档 → 最多 4 个 tab（与 app.MAX_PANES 对应，刻意独立成字面量
# 避免 topbar ↔ app 循环导入；两者同源于设计文档的 4 pane 上限）
MAX_TABS = 4

# 顶栏右上角品牌：程序名 + 包版本（源码真值；发版时与 pyproject 同步 bump）
BRAND_TEXT = f"myrtui {__version__}"

# 0.10.0 紧凑档按钮文案：`D`/`K` 与 `Ctrl-a d/k` 同字母（`K` 仍有二次确认）
COMPACT_BUTTON_TEXTS = {
    "btn-new": " + ",
    "btn-close": " X ",
    "btn-detach": " D ",
    "btn-kill": " K ",
    "btn-keybar": "KEY",
    "btn-help": " ? ",
}


class TopBarButton(Static):
    """一行高、可点击的顶栏按钮。"""

    CAN_FOCUS = True

    # 顶栏按钮不是可选文本。Static 默认 ALLOW_SELECT=True，Screen 会在
    # MouseDown 时先建选区状态、再把事件转给按钮；而按钮的 Pressed 会立刻
    # push 确认框，真实的 MouseUp 落到模态屏 → 主屏的 selecting 永远清不掉，
    # 取消后鼠标一动就开始选字（0.10.0 真机：点 `×` / `Kill` 再取消）。
    ALLOW_SELECT = False

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
            flash_press(self)
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

    # 同理：tab 上按下不该起顶栏的文本选区（否则拖动时指针变文本光标）
    ALLOW_SELECT = False

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
        # 0.10.0：缓存最近一次拓扑与按钮完整文案——切紧凑档时要原地重渲染，
        # 不能等下一次 poll 才把 `1:zsh` 换成 `1`。
        self._labels: list[str] = []
        self._deads: list[bool] = []
        self._active = 0
        self._compact = False

    def compose(self) -> ComposeResult:
        for i in range(MAX_TABS):
            yield PaneTab(i)
        yield TopBarButton(" + ", "btn-new")
        yield TopBarButton(" × ", "btn-close")
        yield TopBarButton(f" {t('topbar.detach')} ", "btn-detach")
        yield TopBarButton(f" {t('topbar.kill')} ", "btn-kill")
        keybar_btn = TopBarButton(t("keybar.toggle"), "btn-keybar")
        keybar_btn.tooltip = t("keybar.toggle_tip")
        yield keybar_btn
        yield TopBarButton(" ? ", "btn-help")
        yield Label("", id="topbar-status")
        yield Label(BRAND_TEXT, id="topbar-brand")

    def set_tabs(self, labels: list[str], deads: list[bool], active: int) -> None:
        """按当前拓扑刷新 1..4 个标签：多余的隐藏，缺的显示。

        标签文本调用方已做防闪烁（仅变更时才进到这里不现实，本方法内部
        对 body/dead/active 各自全等比较，无 diff 不触发 update）。
        """
        self._labels = list(labels)
        self._deads = list(deads)
        self._active = active
        self._render_tabs()

    def _render_tabs(self) -> None:
        """按缓存拓扑渲染 tab（``set_tabs`` 与 ``set_compact`` 共用的出口）。"""
        for i in range(MAX_TABS):
            tab = self.query_one(f"#tab-{i}", PaneTab)
            visible = i < len(self._labels)
            if visible != tab.display:
                tab.display = visible
            if visible:
                label = self._labels[i]
                if self._compact:
                    # 紧凑档只留序号：`1:zsh` → `1`（cmd 在每个 tab 上重复
                    # 占地，40 列屏上正是它挤掉了 `?`）
                    label = label.split(":", 1)[0]
                tab.set_state(
                    label,
                    dead=i < len(self._deads) and self._deads[i],
                    active=i == self._active,
                )
        new_btn = self.query_one("#btn-new", TopBarButton)
        want_disabled = len(self._labels) >= MAX_TABS
        if new_btn.is_disabled != want_disabled:
            new_btn.set_disabled(want_disabled)
        # 关闭当前 pane：只剩一个时 no-op（与 Ctrl-a w 一致），按钮同步置灰
        close_btn = self.query_one("#btn-close", TopBarButton)
        close_disabled = len(self._labels) <= 1
        if close_btn.is_disabled != close_disabled:
            close_btn.set_disabled(close_disabled)

    def set_compact(self, compact: bool) -> None:
        """切紧凑档的顶栏形态（幂等；App._apply_compact 调用）。

        按钮文案与 tab 都按当前缓存重渲染——只改 ``display`` 的话，退出紧凑
        后标签会停在 ``1`` 直到下一次拓扑/命令变化。
        """
        if compact == self._compact:
            return
        self._compact = compact
        texts = (
            COMPACT_BUTTON_TEXTS
            if compact
            else {
                "btn-new": " + ",
                "btn-close": " X ",
                "btn-detach": f" {t('topbar.detach')} ",
                "btn-kill": f" {t('topbar.kill')} ",
                "btn-keybar": t("keybar.toggle"),
                "btn-help": " ? ",
            }
        )
        for button_id, text in texts.items():
            self.query_one(f"#{button_id}", TopBarButton).update(text)
        self._render_tabs()

    def set_keybar_control(self, visible: bool, on: bool) -> None:
        """显示 / 隐藏顶栏 ``KEY`` 开关，并同步「底栏开着」的底色。"""
        btn = self.query_one("#btn-keybar", TopBarButton)
        if btn.display != visible:
            btn.display = visible
        btn.set_class(visible and on, "active")

    def set_status(self, text: str) -> None:
        """顶部状态提示（如「与会话失去连接」）。"""
        if text != self._status:
            self._status = text
            self.query_one("#topbar-status", Label).update(text)
