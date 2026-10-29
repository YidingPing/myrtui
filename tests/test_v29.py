"""myrtui v0.29 测试：``Ctrl-a ?`` 全键盘开关快捷键浮层。

覆盖：

- ``Ctrl-a ?`` 显示 / 再按收起，显示位置与现有悬停路径一致；
- 键盘打开的浮层（pinned）不被「鼠标离开 `?`」的宽限定时器误关；
- 浮层显示期间普通键仍透传 shell、焦点仍在 pane（非模态承诺不变）；
- 任一前缀动作 / 点浮层外收起并清 pin；
- 80×24 下不越界且正文含 `?` 的自述行。
"""
from __future__ import annotations

import asyncio
import tempfile

from textual.events import MouseDown

from myrtui.client import app as app_module
from myrtui.client.app import TerminalApp
from myrtui.client.pane_widget import PaneWidget
from myrtui.client.shortcuts import ShortcutHelp
from myrtui.tmux_backend import CaptureResult, PaneInfo

PANE_TEXT = "abcdefghij\nsecond line\n"


def _pane(index: int, dead: bool = False) -> PaneInfo:
    return PaneInfo(
        pane_id=f"p{index}",
        dead=dead,
        command="sh",
        alternate_on=False,
        cursor_x=0,
        cursor_y=0,
        cursor_flag=False,
        history_size=100,
        height=24,
        width=80,
        window_id=f"w{index}",
        window_index=index,
        mouse_standard=False,
        mouse_button=False,
        mouse_any=False,
        mouse_sgr=True,
        mouse_all=False,
    )


class _FakeBackend:
    """假后端：固定拓扑 + 键鼠记账（不碰真 tmux）。"""

    def __init__(self, count: int = 1):
        self.count = count
        self.sent: list[tuple] = []

    def list_panes(self):
        return [_pane(i) for i in range(self.count)]

    def poll(self, offsets):
        panes = self.list_panes()
        caps = {p.pane_id: CaptureResult(text=PANE_TEXT, offset=0) for p in panes}
        return panes, caps

    def resize_window(self, window_id, cols, rows):
        pass

    def paste(self, pane_id, text):
        pass

    def send_keys(self, pane_id, *keys):
        self.sent.append(("keys", pane_id, keys))

    def send_text(self, pane_id, text):
        self.sent.append(("text", pane_id, text))

    def send_mouse(self, pane_id, sequence):
        pass

    def send_ctrl_a(self, pane_id):
        self.sent.append(("ctrl-a", pane_id))


def _root() -> str:
    return tempfile.mkdtemp(prefix="myrtui-v29-")


def _fast(monkeypatch) -> None:
    """压缩悬停延迟/宽限，避免测试等产品默认的 0.15/0.25 秒。"""
    monkeypatch.setattr(app_module, "HELP_SHOW_DELAY", 0.02)
    monkeypatch.setattr(app_module, "HELP_HIDE_GRACE", 0.05)


def _texts(panel: ShortcutHelp) -> str:
    rendered = panel.query_one("#shortcut-help-body").render()
    return rendered.plain if hasattr(rendered, "plain") else str(rendered)


async def _open_by_keyboard(pilot) -> None:
    await pilot.press("ctrl+a")
    await pilot.press("question_mark")
    await pilot.pause(0.1)


# ---------- 开关 ----------

def test_ctrl_a_question_mark_toggles_panel():
    async def scenario():
        app = TerminalApp(_FakeBackend(1), _root())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            panel = app.query_one("#shortcut-help", ShortcutHelp)
            assert not panel.display

            await _open_by_keyboard(pilot)
            assert panel.display, "Ctrl-a ? 应弹出浮层"
            assert app._help_pinned is True
            assert panel.region.y == 1, "键盘路径沿用顶栏下沿定位"
            assert panel.region.right <= 120 and panel.region.bottom <= 40
            assert "显示 / 收起本浮层" in _texts(panel)

            await _open_by_keyboard(pilot)
            assert not panel.display, "再按一次应收起"
            assert app._help_pinned is False

    asyncio.run(scenario())


def test_keyboard_help_fits_80x24():
    async def scenario():
        app = TerminalApp(_FakeBackend(1), _root())
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.3)
            await _open_by_keyboard(pilot)
            panel = app.query_one("#shortcut-help", ShortcutHelp)
            assert panel.display
            assert panel.region.x >= 0
            assert panel.region.right <= 80
            assert panel.region.y == 1
            assert panel.region.bottom <= 24, "v0.28 后的表格应能在 80×24 完整放下"
            assert "透传 shell" in _texts(panel)

    asyncio.run(scenario())


# ---------- 非模态与 pin ----------

def test_keyboard_help_does_not_steal_keys():
    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, _root())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            await _open_by_keyboard(pilot)
            panel = app.query_one("#shortcut-help", ShortcutHelp)
            assert panel.display
            assert app.focused.id == "pane-0"

            await pilot.press("x")
            await pilot.pause(0.2)
            assert ("text", "p0", "x") in backend.sent, "浮层显示时键必须照旧透传"
            assert app.focused is app.query_one("#pane-0", PaneWidget)
            assert panel.display, "普通键不应收起浮层"

    asyncio.run(scenario())


def test_pinned_help_survives_hover_leave(monkeypatch):
    """键盘打开的浮层归键盘管：指针扫过 `?` 再离开也不关。"""
    _fast(monkeypatch)

    async def scenario():
        app = TerminalApp(_FakeBackend(1), _root())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            await _open_by_keyboard(pilot)
            panel = app.query_one("#shortcut-help", ShortcutHelp)
            assert panel.display

            await pilot.hover("#btn-help")
            await pilot.hover("#topbar-brand")
            await pilot.pause(app_module.HELP_HIDE_GRACE + 0.15)
            assert panel.display, "pinned 浮层不得被鼠标离开定时器关掉"
            assert app._help_pinned is True

            # 再按一次才收起
            await _open_by_keyboard(pilot)
            assert not panel.display

    asyncio.run(scenario())


def test_prefix_action_hides_keyboard_help_and_clears_pin():
    async def scenario():
        app = TerminalApp(_FakeBackend(1), _root())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            await _open_by_keyboard(pilot)
            panel = app.query_one("#shortcut-help", ShortcutHelp)
            assert panel.display

            await pilot.press("ctrl+a")
            await pilot.press("b")
            await pilot.pause(0.2)
            assert not panel.display, "任一前缀动作都应收起浮层"
            assert app._help_pinned is False

    asyncio.run(scenario())


def test_click_outside_hides_keyboard_help_and_clears_pin():
    async def scenario():
        app = TerminalApp(_FakeBackend(1), _root())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            await _open_by_keyboard(pilot)
            panel = app.query_one("#shortcut-help", ShortcutHelp)
            assert panel.display

            app.on_mouse_down(
                MouseDown(
                    widget=None, x=1.0, y=1.0, delta_x=0, delta_y=0,
                    button=1, shift=False, meta=False, ctrl=False,
                    screen_x=1.0, screen_y=1.0,
                )
            )
            assert not panel.display
            assert app._help_pinned is False

    asyncio.run(scenario())
