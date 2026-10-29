"""myrtui v0.22 测试：顶栏 `?` 悬停快捷键浮层 + ``Ctrl-a g`` 聚焦 Git 历史。

覆盖：

- `?` 紧跟在 `Kill` 右侧；浮层默认隐藏，悬停延迟后显示、离开宽限后隐藏；
- 指针可移进浮层继续读（宽限内取消隐藏）；浮层不抢焦点、不吞键盘；
- 浮层是屏幕级 overlay：点内部不拖分隔线、不注入 pane，点外部收起；
- 80×24 下位置/宽度不越界；
- ``Ctrl-a g`` 聚焦 ``#git-log``（侧栏折叠时先展开）、``Esc`` 回 pane、
  侧栏聚焦时键不透传 shell、顶栏区分「Git 历史已聚焦」；
- 快捷键表与 ``on_key`` 前缀分支防漂移。
"""
from __future__ import annotations

import asyncio
import tempfile

from textual.events import MouseDown
from textual.widgets import Label

from myrtui.client import app as app_module
from myrtui.client.app import TerminalApp
from myrtui.client.pane_widget import PaneWidget
from myrtui.client.shortcuts import SECTIONS, ShortcutHelp
from myrtui.client.topbar import TopBar
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
    return tempfile.mkdtemp(prefix="myrtui-v22-")


def _fast(monkeypatch) -> None:
    """压缩悬停延迟/宽限，避免测试等产品默认的 0.15/0.25 秒。"""
    monkeypatch.setattr(app_module, "HELP_SHOW_DELAY", 0.02)
    monkeypatch.setattr(app_module, "HELP_HIDE_GRACE", 0.05)


def _texts(panel: ShortcutHelp) -> str:
    rendered = panel.query_one("#shortcut-help-body").render()
    return rendered.plain if hasattr(rendered, "plain") else str(rendered)


# ---------- 顶栏布局 ----------

def test_help_button_sits_right_of_kill():
    async def scenario():
        app = TerminalApp(_FakeBackend(1), _root())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            ids = [child.id for child in app.query_one(TopBar).children]
            assert "btn-help" in ids
            assert ids.index("btn-help") == ids.index("btn-kill") + 1

    asyncio.run(scenario())


# ---------- 悬停显隐 ----------

def test_hover_help_shows_panel_with_full_shortcut_list(monkeypatch):
    _fast(monkeypatch)

    async def scenario():
        app = TerminalApp(_FakeBackend(1), _root())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            panel = app.query_one("#shortcut-help", ShortcutHelp)
            assert not panel.display, "默认必须隐藏"

            await pilot.hover("#btn-help")
            await pilot.pause(0.1)
            assert panel.display, "悬停 `?` 应弹出浮层"
            text = _texts(panel)
            for needle in ("Ctrl-a 前缀", "detach", "聚焦 Git 历史", "侧栏", "打开 / 预览 / 复制路径", "透传 shell"):
                assert needle in text
            assert panel.region.y == 1, "浮层应紧贴顶栏下沿"
            assert panel.region.right <= 120
            assert panel.region.bottom <= 40

            # 指针移进浮层 → 宽限期过后仍在（可以继续读）
            await pilot.hover(panel, offset=(2, 2))
            await pilot.pause(app_module.HELP_HIDE_GRACE + 0.1)
            assert panel.display, "移进浮层不应被隐藏"

            # 移到浮层之外（顶栏品牌区，不与浮层重叠）→ 隐藏
            await pilot.hover("#topbar-brand")
            await pilot.pause(app_module.HELP_HIDE_GRACE + 0.15)
            assert not panel.display

    asyncio.run(scenario())


def test_click_help_button_opens_panel_without_waiting_hover_delay(monkeypatch):
    """悬停是主入口；点击路径立即展开（不依赖 0.15s 定时器）。"""
    monkeypatch.setattr(app_module, "HELP_SHOW_DELAY", 1.0)

    async def scenario():
        app = TerminalApp(_FakeBackend(1), _root())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            panel = app.query_one("#shortcut-help", ShortcutHelp)
            await pilot.click("#btn-help")
            await pilot.pause(0.05)
            assert panel.display

    asyncio.run(scenario())


def test_help_panel_stays_on_screen_in_narrow_terminal(monkeypatch):
    _fast(monkeypatch)

    async def scenario():
        app = TerminalApp(_FakeBackend(1), _root())
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.3)
            await pilot.hover("#btn-help")
            await pilot.pause(0.1)
            panel = app.query_one("#shortcut-help", ShortcutHelp)
            assert panel.display
            assert panel.region.x >= 0
            assert panel.region.right <= 80
            assert panel.region.y == 1
            assert panel.region.bottom <= 24

    asyncio.run(scenario())


# ---------- 非模态：不吃键、不误触分隔线 ----------

def test_help_panel_does_not_steal_keys(monkeypatch):
    _fast(monkeypatch)

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, _root())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            panel = app.query_one("#shortcut-help", ShortcutHelp)
            await pilot.hover("#btn-help")
            await pilot.pause(0.1)
            assert panel.display

            await pilot.press("x")
            await pilot.pause(0.2)
            assert ("text", "p0", "x") in backend.sent, "浮层显示时键必须照旧透传"
            assert app.focused.id == "pane-0"

            # 显式前缀动作（Ctrl-a s）→ 浮层收起，避免挡住目标区域
            await pilot.press("ctrl+a")
            await pilot.press("s")
            await pilot.pause(0.2)
            assert not panel.display

    asyncio.run(scenario())


def test_click_inside_help_panel_keeps_it_and_does_not_start_drag(monkeypatch):
    _fast(monkeypatch)

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, _root())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            panel = app.query_one("#shortcut-help", ShortcutHelp)
            await pilot.hover("#btn-help")
            await pilot.pause(0.1)
            assert panel.display
            x = panel.region.x + 2
            y = panel.region.y + 2

            app.on_mouse_down(
                MouseDown(
                    widget=panel, x=2.0, y=2.0, delta_x=0, delta_y=0,
                    button=1, shift=False, meta=False, ctrl=False,
                    screen_x=float(x), screen_y=float(y),
                )
            )
            assert panel.display, "点浮层内部不应收起"
            assert app._drag_sep is None, "浮层内按下不得起拖分隔线"
            assert backend.sent == [], "浮层内按下不得注入 pane"

            # 点浮层外 → 收起（同时不注入）
            app.on_mouse_down(
                MouseDown(
                    widget=None, x=1.0, y=1.0, delta_x=0, delta_y=0,
                    button=1, shift=False, meta=False, ctrl=False,
                    screen_x=1.0, screen_y=1.0,
                )
            )
            assert not panel.display
            assert backend.sent == []

    asyncio.run(scenario())


# ---------- Ctrl-a g：聚焦 Git 历史 ----------

def test_ctrl_a_g_focuses_git_log():
    async def scenario():
        app = TerminalApp(_FakeBackend(1), _root())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            await pilot.press("ctrl+a")
            await pilot.press("g")
            await pilot.pause(0.2)
            assert app.focused is not None
            assert app.focused.id == "git-log"
            assert app._git_focused() is True
            assert app._sidebar_focused() is True

    asyncio.run(scenario())


def test_ctrl_a_g_expands_collapsed_sidebar_first():
    async def scenario():
        app = TerminalApp(_FakeBackend(1), _root())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            await pilot.press("ctrl+a")
            await pilot.press("b")
            await pilot.pause(0.2)
            assert app.query_one("#sidebar").display is False

            await pilot.press("ctrl+a")
            await pilot.press("g")
            await pilot.pause(0.3)
            assert app.query_one("#sidebar").display is True
            assert app.focused.id == "git-log"

    asyncio.run(scenario())


def test_escape_from_git_log_returns_to_active_pane():
    async def scenario():
        app = TerminalApp(_FakeBackend(1), _root())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            await pilot.press("ctrl+a")
            await pilot.press("g")
            await pilot.pause(0.2)
            assert app.focused.id == "git-log"

            await pilot.press("escape")
            await pilot.pause(0.2)
            assert app.focused is app.query_one("#pane-0", PaneWidget)
            assert app.focused.id == "pane-0"

    asyncio.run(scenario())


def test_git_log_focus_blocks_keys_from_shell():
    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, _root())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            await pilot.press("ctrl+a")
            await pilot.press("g")
            await pilot.pause(0.2)
            assert backend.sent == []

            await pilot.press("x")
            await pilot.press("down")
            await pilot.pause(0.2)
            assert backend.sent == [], "焦点在 Git 历史时键不能进 shell（D8）"

            status = app.query_one("#topbar-status", Label).render()
            assert "Git 历史已聚焦" in status.plain

    asyncio.run(scenario())


# ---------- 数据表防漂移 ----------

def test_shortcut_table_covers_every_prefix_branch():
    """``on_key`` 的每个前缀分支都必须出现在浮层里（防新增键位忘补表）。"""
    import inspect
    import re

    from myrtui.client.app import TerminalApp

    source = inspect.getsource(TerminalApp.on_key)
    branch_letters = set(re.findall(r'key == "([a-z])"', source))
    prefix_rows = next(
        section for section in SECTIONS if section.title.startswith("Ctrl-a")
    ).rows
    table = {row.keys for row in prefix_rows}
    table_letters = {k for k in table if len(k) == 1 and k.isalpha()}
    assert branch_letters, "没有解析到前缀分支，防止正则失效后假绿"
    assert branch_letters == table_letters
    assert "1..4" in table, "数字切 pane 的分支是范围写法，单独锚定"
    # v0.29：`?` 不是字母，上面正则扫不到 → 单独锚定「分支存在且在表里」
    assert 'key in ("question_mark", "?")' in source, "Ctrl-a ? 分支丢了"
    assert "?" in table, "Ctrl-a ? 分支必须出现在快捷键浮层里"
