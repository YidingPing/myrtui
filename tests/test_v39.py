"""myrtui 0.8.0 测试：外层复用器探测与启动提示。

背景（0.7.0 真机）：UU 远程的终端本身就是 tmux 3.5a，它的 root 表把
``MouseDrag1Pane`` 改成了无条件 ``copy-mode -M``。结果是按下能到 myrtui、
**按住期间的 motion 全被 copy-mode 吃掉**（实测按住 7.5 秒、0 条 motion，
松手时 ``copy-pipe pbcopy`` 把选区写进系统剪贴板），拖拽选字与「转发给程序」
整体失效。

myrtui 无法从内部改外层绑定，能做的是**认出来 + 给一次可执行的提示**。本文件
只测这个识别与提示，不测任何终端行为。
"""
from __future__ import annotations

import asyncio

import pytest

from myrtui.client import clipboard
from myrtui.client.app import TerminalApp, OUTER_MUX_HINT_SECONDS
from myrtui.client.topbar import TopBar
from myrtui.i18n import t
from myrtui.termenv import hint_enabled, outer_multiplexer
from myrtui.tmux_backend import CaptureResult, PaneInfo


# ---------------------------------------------------------------- 探测


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, None),
        ({"TERM": "xterm-256color"}, None),
        ({"TERM": "xterm-256color", "TERM_PROGRAM": "iTerm.app"}, None),
        # 1. $TMUX —— 最硬的信号
        ({"TMUX": "/tmp/tmux-501/default,123,0"}, "tmux"),
        # 2. $STY（screen 的会话名）
        ({"STY": "1234.pts-0.host"}, "screen"),
        # 3. tmux 3.2+ 的 TERM_PROGRAM（UU 会 unset $TMUX，但留这个）
        ({"TERM_PROGRAM": "tmux", "TERM": "xterm-256color"}, "tmux"),
        ({"TERM_PROGRAM": "TMUX"}, "tmux"),
        # 4. TERM 前缀（tmux / screen 的默认终端类型）
        ({"TERM": "tmux-256color"}, "tmux"),
        ({"TERM": "screen-256color"}, "screen"),
        # 空串 / 空白不算
        ({"TMUX": "", "TERM_PROGRAM": " ", "TERM": " "}, None),
    ],
)
def test_outer_multiplexer(env, expected):
    assert outer_multiplexer(env) == expected


# ---------------------------------------------------------------- 启动提示


class _FakeBackend:
    def __init__(self, count: int = 1, mouse: tuple[bool, bool, bool, bool] = (False, False, False, False)):
        self.count = count
        self.mouse = mouse
        self.pasted: list[tuple[str, str]] = []
        self.sent: list[tuple] = []

    def list_panes(self):
        return [
            PaneInfo(
                pane_id=f"p{i}",
                dead=False,
                command="sh",
                alternate_on=False,
                cursor_x=0,
                cursor_y=0,
                cursor_flag=False,
                history_size=100,
                height=24,
                width=80,
                window_id=f"w{i}",
                window_index=i,
                mouse_standard=self.mouse[0],
                mouse_button=self.mouse[1],
                mouse_any=self.mouse[2],
                mouse_sgr=True,
                mouse_all=self.mouse[3],
            )
            for i in range(self.count)
        ]

    def poll(self, offsets):
        panes = self.list_panes()
        return panes, {
            p.pane_id: CaptureResult(text="hi\n", offset=0) for p in panes
        }

    def resize_window(self, window_id, cols, rows):
        pass

    def send_keys(self, pane_id, *keys):
        pass

    def send_text(self, pane_id, text):
        pass

    def send_ctrl_a(self, pane_id):
        pass

    def paste(self, pane_id, text):
        self.pasted.append((pane_id, text))

    def send_mouse(self, pane_id, sequence):
        self.sent.append(("mouse", pane_id, sequence))

    def take_clipboard_events(self):
        return []

    def poll_clipboard_buffers(self):
        pass


def test_startup_hint_when_inside_tmux(monkeypatch, app_root):
    """``TERM_PROGRAM=tmux``（UU 的形态：$TMUX 被 unset）→ 启动提示一次。"""

    monkeypatch.setenv("TERM_PROGRAM", "tmux")

    async def scenario():
        app = TerminalApp(_FakeBackend(), app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            assert app._notice_text == t("app.outer_mux_hint")
            # 顶栏真的显示出来了（不只是记了个时间戳）
            assert app.query_one(TopBar)._status == t("app.outer_mux_hint")
            assert app._notice_until - __import__("time").monotonic() > 5.0
            assert OUTER_MUX_HINT_SECONDS >= 5.0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "raw,expected",
    [(None, True), ("", True), ("1", True), ("garbage", True),
     ("0", False), ("false", False), ("NO", False), ("off", False)],
)
def test_hint_enabled(raw, expected):
    assert hint_enabled(raw) is expected


def test_hint_can_be_disabled(monkeypatch, app_root):
    """``MYRTUI_OUTER_MUX_HINT=0``：外层 tmux 在场也不弹（UU 用户天天在里面）。"""

    monkeypatch.setenv("TERM_PROGRAM", "tmux")
    monkeypatch.setenv("MYRTUI_OUTER_MUX_HINT", "0")

    async def scenario():
        app = TerminalApp(_FakeBackend(), app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            assert t("app.outer_mux_hint") not in app._notice_text

    asyncio.run(scenario())


def test_no_hint_outside_multiplexer(monkeypatch, app_root):
    """普通终端：不弹这条提示（别把排障文案变成噪音）。"""

    async def scenario():
        app = TerminalApp(_FakeBackend(), app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            assert t("app.outer_mux_hint") not in app._notice_text

    asyncio.run(scenario())


def test_hint_expires_and_shares_the_status_entry(monkeypatch, app_root):
    """提示走顶栏统一入口：到期后自动让位，不会长期占着状态栏。"""

    monkeypatch.setenv("TERM", "tmux-256color")

    async def scenario():
        app = TerminalApp(_FakeBackend(), app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            assert app.query_one(TopBar)._status == t("app.outer_mux_hint")
            app._notice_until = 0.0  # 让提示过期（不睡 10 秒）
            app._refresh_status()
            await pilot.pause(0.1)
            assert app.query_one(TopBar)._status != t("app.outer_mux_hint")

    asyncio.run(scenario())


# ---------------------------------------------------------------- 右键粘贴


MOUSE_1003 = (False, False, True, True)


def test_right_click_pastes_when_program_has_no_mouse(monkeypatch, app_root):
    """0.8.0：程序没请求鼠标时，右键 = 粘贴（终端约定，填「右键没反应」的洞）。"""

    monkeypatch.setattr(clipboard, "read_system_clipboard", lambda: "client-side-text")

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            await pilot.mouse_down("#pane-0", offset=(3, 2), button=3)
            await pilot.pause(0.4)
            await pilot.mouse_up("#pane-0", offset=(3, 2))
            await pilot.pause(0.2)
            assert backend.pasted == [("p0", "client-side-text")]
            assert not [item for item in backend.sent if item[0] == "mouse"]

    asyncio.run(scenario())


def test_right_click_forwards_when_program_has_mouse(monkeypatch, app_root):
    """程序请求了鼠标（vim / opencode）→ 右键归它，不能抢成粘贴。"""

    monkeypatch.setattr(
        clipboard,
        "read_system_clipboard",
        lambda: pytest.fail("程序要鼠标时不得读剪贴板"),
    )

    async def scenario():
        backend = _FakeBackend(1, mouse=MOUSE_1003)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            await pilot.mouse_down("#pane-0", offset=(3, 2), button=3)
            await pilot.pause(0.2)
            await pilot.mouse_up("#pane-0", offset=(3, 2))
            await pilot.pause(0.2)
            assert backend.pasted == []
            assert [item for item in backend.sent if item[0] == "mouse"]

    asyncio.run(scenario())


def test_right_click_paste_falls_back_to_internal_buffer(monkeypatch, app_root):
    """系统剪贴板读不到 → 用内部缓冲（与 Ctrl-a v 同一条链）。"""

    monkeypatch.setattr(clipboard, "read_system_clipboard", lambda: None)

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        app._copy_buffer = "internal-old"
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            await pilot.mouse_down("#pane-0", offset=(3, 2), button=3)
            await pilot.pause(0.4)
            assert backend.pasted == [("p0", "internal-old")]

    asyncio.run(scenario())


def test_right_click_paste_respects_osc52_only_mode(monkeypatch, app_root):
    """``MYRTUI_CLIPBOARD=osc52``：没有可读的剪贴板 → 只提示终端粘贴键。"""

    monkeypatch.setenv("MYRTUI_CLIPBOARD", "osc52")

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            await pilot.mouse_down("#pane-0", offset=(3, 2), button=3)
            await pilot.pause(0.3)
            assert backend.pasted == []
            assert "Cmd+V" in app._notice_text

    asyncio.run(scenario())
