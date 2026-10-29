"""myrtui v0.13 测试：pane 内文本选区 + ``Ctrl-a c`` 复制 + 剪贴板通道。

> v0.35 起语义变化：默认 ``always`` 模式下 pane 恒允许选区、拖拽松手自动复制；
> 本文件里拖拽用例统一 ``app._copy_on_select = False`` 只测选区 / 手动复制。
> 新语义的完整覆盖见 ``tests/test_v35.py``。

分层：
- **门控**：``PaneWidget.allow_select`` 在默认 ``always`` 下恒 True；
  ``MYRTUI_DRAG_SELECT=app`` 恢复 v0.34 门控（程序请求鼠标 → 关闭选区）；
- **选区**：pilot 真机「按下 → 拖到 → 松开」→ ``Screen.get_selected_text()``；
- **双击**：Textual 默认的「双击/三击全选」被禁用（v0.13 决策：不要）；
- **复制**：``Ctrl-a c`` → OSC 52（``App._clipboard``）+ 系统命令兜底（可注入）；
- **剪贴板模块**：按平台探测命令 + 失败路径。

除最后一条端到端用例外都不碰真 tmux：假后端给固定 pane 内容（选区要有东西可选）。
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import time
import uuid

import pytest

from myrtui.client import clipboard
from myrtui.client.app import TerminalApp
from myrtui.client.pane_widget import PaneWidget
from myrtui.client.topbar import TopBar
from myrtui.mouse import LEVEL_ALL, LEVEL_CLICK, LEVEL_DRAG, LEVEL_NONE
from myrtui.tmux_backend import CaptureResult, PaneInfo, TmuxBackend

# 假后端给的 pane 内容（每行会被 parse_sgr_line 补齐到 pane 宽度）
PANE_TEXT = "abcdefghij\nsecond line\nthird line\n"


def _pane(
    index: int,
    *,
    mouse: tuple[bool, bool, bool, bool] = (False, False, False, False),
) -> PaneInfo:
    """造一个假的 pane 信息；``mouse`` = (std, btn, any, all)。"""
    std, btn, any_, all_ = mouse
    return PaneInfo(
        pane_id=f"p{index}",
        dead=False,
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
        mouse_standard=std,
        mouse_button=btn,
        mouse_any=any_,
        mouse_sgr=True,
        mouse_all=all_,
    )


class _FakeBackend:
    """假后端：固定拓扑 + 固定 pane 内容（选区测试用，不碰真 tmux）。"""

    def __init__(
        self,
        count: int = 1,
        text: str = PANE_TEXT,
        mouse: dict[int, tuple[bool, bool, bool, bool]] | None = None,
    ):
        self.count = count
        self.text = text
        self.mouse = mouse or {}
        self.sent: list[tuple] = []

    def list_panes(self):
        return [
            _pane(i, mouse=self.mouse.get(i, (False, False, False, False)))
            for i in range(self.count)
        ]

    def poll(self, offsets):
        panes = self.list_panes()
        caps = {p.pane_id: CaptureResult(text=self.text, offset=0) for p in panes}
        return panes, caps

    def resize_window(self, window_id, cols, rows):
        pass

    def send_keys(self, pane_id, *keys):
        self.sent.append(("keys", pane_id, keys))

    def send_text(self, pane_id, text):
        self.sent.append(("text", pane_id, text))

    def send_ctrl_a(self, pane_id):
        # 前缀超时路径会走这里（``Ctrl-a`` 后 1s 内没按第二键 → 把前缀本身
        # 发给 pane）。假后端必须实现，否则高负载下定时器抢跑会变成
        # AttributeError 而不是断言失败。
        self.sent.append(("ctrl_a", pane_id))

    def send_mouse(self, pane_id, sequence):
        self.sent.append(("mouse", pane_id, sequence))

    def paste(self, pane_id, text):
        self.sent.append(("paste", pane_id, text))


async def _drag_select(app, pilot, pane: PaneWidget, start, end) -> None:
    """在 pane 内容区从 start 拖到 end（**内容坐标**，0-based，不含边框）。

    用 pilot 的 mouse_down / mouse_up：mouse_up 之前 pilot 会补一条**带按钮**
    的 MouseMove 到释放点 → 选区随之延伸到终点（与真机拖拽一致）。
    """
    await pilot.mouse_down(pane, offset=(start[0] + 1, start[1] + 1))
    await pilot.mouse_up(pane, offset=(end[0] + 1, end[1] + 1))
    await pilot.pause(0.1)


# ---------- 门控：程序是否请求鼠标 ----------

def test_allow_select_open_when_program_has_no_mouse(app_root):
    """pane 内程序**没**请求鼠标 → 允许拖拽选字。"""

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            assert app._mouse_levels.get("p0") == LEVEL_NONE
            assert pane.allow_select is True

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "flags,level",
    [
        ((True, False, True, False), LEVEL_CLICK),   # 1000（点击）
        ((False, True, True, False), LEVEL_DRAG),    # 1002（+拖动）
        ((False, False, True, True), LEVEL_ALL),     # 1003（+移动）
    ],
)
def test_allow_select_true_in_always_mode(flags, level, app_root):
    """v0.35 默认 ``always``：程序请求了鼠标，pane 仍允许本地拖拽选字。"""

    async def scenario():
        backend = _FakeBackend(1, mouse={0: flags})
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            assert app._drag_select_mode == "always"
            assert app._mouse_levels.get("p0") == level
            assert pane.allow_select is True

    asyncio.run(scenario())


def test_allow_select_old_gating_in_app_mode(monkeypatch, app_root):
    """``MYRTUI_DRAG_SELECT=app`` → 恢复 v0.34 门控（程序抢鼠标时不允许选区）。"""

    async def scenario():
        backend = _FakeBackend(1, mouse={0: (False, False, True, True)})
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            assert app._drag_select_mode == "app"
            assert app._mouse_levels.get("p0") == LEVEL_ALL
            assert pane.allow_select is False

        # 无鼠标 pane 仍然允许（app 模式只影响程序抢鼠标的场景）
        backend2 = _FakeBackend(1)
        app2 = TerminalApp(backend2, app_root)
        async with app2.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane2 = app2.query_one("#pane-0", PaneWidget)
            assert pane2.allow_select is True

    monkeypatch.setenv("MYRTUI_DRAG_SELECT", "app")
    asyncio.run(scenario())


def test_allow_select_closed_without_app_context():
    """没挂载（拿不到 app）时不允许起选区——不猜。"""
    pane = PaneWidget("p0")
    assert pane.allow_select is False


# ---------- 选区 ----------

def test_drag_in_pane_creates_selection(app_root):
    """pane 内拖拽 → 产生选区，内容就是拖过的那几个字符。"""

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        app._copy_on_select = False  # v0.35：本用例只测选区，关掉自动复制
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            assert pane._lines, "pane 应已有内容"
            await _drag_select(app, pilot, pane, (4, 0), (8, 0))
            selected = app.screen.get_selected_text()
            assert selected == "efghi", selected

    asyncio.run(scenario())


def test_double_click_does_not_select_all(app_root):
    """**双击不选中**（v0.13 决策）：Textual 默认的双击/三击全选被禁用。"""

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            await pilot.double_click(pane, offset=(5, 1))
            await pilot.pause(0.1)
            assert not app.screen.selections, "双击不应产生任何选区"
            assert app.screen.get_selected_text() is None

    asyncio.run(scenario())


def test_single_click_does_not_leave_selection(app_root):
    """单击（同点按下松开）→ 不留选区（Textual 自己清），点击仍能聚焦 pane。"""

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            await pilot.click(pane, offset=(5, 1))
            await pilot.pause(0.1)
            assert not app.screen.selections
            assert app.focused is pane

    asyncio.run(scenario())


# ---------- 复制（Ctrl-a c） ----------

def test_ctrl_a_c_copies_selection(monkeypatch, app_root):
    """有选区 → ``Ctrl-a c`` 把选区复制到两条通道（OSC 52 + 系统命令）。"""

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        app._copy_on_select = False  # v0.35：拖拽不再自动复制，本用例测手动复制
        copied: list[str] = []
        monkeypatch.setattr(
            clipboard, "copy_to_system_clipboard", lambda text: copied.append(text) or True
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            await _drag_select(app, pilot, pane, (4, 0), (8, 0))
            assert app.screen.get_selected_text() == "efghi"

            await pilot.press("ctrl+a")
            await pilot.press("c")
            await pilot.pause(0.1)

            # OSC 52 通道（Textual 内部缓冲）
            assert app._clipboard == "efghi"
            # 系统命令兜底通道
            assert copied == ["efghi"]
            # 顶栏提示
            assert "已复制" in app.query_one(TopBar)._status

    asyncio.run(scenario())


def test_ctrl_a_c_without_selection_copies_nothing(monkeypatch, app_root):
    """**无选区 → 不复制**（v0.13 决策）：两条通道都不写，只提示。"""

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        copied: list[str] = []
        monkeypatch.setattr(
            clipboard, "copy_to_system_clipboard", lambda text: copied.append(text) or True
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            # 一次 press 连发两个键：两次独立 press 之间要等一轮 screen，
            # 高负载下可能跨过 1s 前缀超时 → 变成把 Ctrl-a 本身发给 pane。
            await pilot.press("ctrl+a", "c")
            await pilot.pause(0.1)

            assert copied == [], "无选区时不得写系统剪贴板"
            assert app._clipboard == "", "无选区时不得发 OSC 52"
            assert app.query_one(TopBar)._status == "无可复制内容"

    asyncio.run(scenario())


def test_ctrl_a_c_works_with_sidebar_focused(monkeypatch, app_root):
    """侧栏聚焦时 ``Ctrl-a c`` 同样可用（前缀键是全局的）。"""

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        app._copy_on_select = False  # v0.35：手动复制路径
        copied: list[str] = []
        monkeypatch.setattr(
            clipboard, "copy_to_system_clipboard", lambda text: copied.append(text) or True
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            await _drag_select(app, pilot, pane, (4, 0), (8, 0))
            app.action_focus_sidebar()
            await pilot.pause(0.1)
            assert app._sidebar_focused()

            await pilot.press("ctrl+a")
            await pilot.press("c")
            await pilot.pause(0.1)
            assert copied == ["efghi"]

    asyncio.run(scenario())


# ---------- 剪贴板模块 ----------

def test_clipboard_probe_prefers_platform_command(monkeypatch):
    """按平台探测：macOS → pbcopy；Windows → clip.exe；Linux → xclip/wl-copy。"""

    def probe(platform, which_map):
        monkeypatch.setattr(clipboard.sys, "platform", platform)
        monkeypatch.setattr(clipboard.shutil, "which", lambda exe: which_map.get(exe))
        monkeypatch.setattr(clipboard, "_probed", False)
        monkeypatch.setattr(clipboard, "_command", None)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        return clipboard.system_clipboard_command()

    assert probe("darwin", {"pbcopy": "/usr/bin/pbcopy"}) == ["/usr/bin/pbcopy"]
    assert probe("win32", {"clip.exe": "C:/clip.exe"}) == ["C:/clip.exe"]
    assert probe(
        "linux", {"xclip": "/usr/bin/xclip", "wl-copy": "/usr/bin/wl-copy"}
    ) == ["/usr/bin/xclip", "-selection", "clipboard"]


def test_clipboard_probe_prefers_wl_copy_under_wayland(monkeypatch):
    """Wayland 会话里优先 wl-copy（X11 与 Wayland 是两套剪贴板）。"""

    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.setattr(
        clipboard.shutil, "which", lambda exe: f"/usr/bin/{exe}"
    )
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(clipboard, "_probed", False)
    monkeypatch.setattr(clipboard, "_command", None)
    assert clipboard.system_clipboard_command() == ["/usr/bin/wl-copy"]


def test_clipboard_probe_returns_none_when_missing(monkeypatch):
    """找不到任何命令 → None（调用方据此判定「复制不了」）。"""

    monkeypatch.setattr(clipboard.sys, "platform", "darwin")
    monkeypatch.setattr(clipboard.shutil, "which", lambda exe: None)
    monkeypatch.setattr(clipboard, "_probed", False)
    monkeypatch.setattr(clipboard, "_command", None)
    assert clipboard.system_clipboard_command() is None
    assert clipboard.copy_to_system_clipboard("x") is False


def test_copy_to_system_clipboard_success(monkeypatch):
    """命令返回 0 → True。"""

    monkeypatch.setattr(clipboard, "system_clipboard_command", lambda: ["/bin/true"])
    calls: list[bytes] = []

    class _Proc:
        returncode = 0

    def fake_run(cmd, **kwargs):
        calls.append(kwargs["input"])
        return _Proc()

    monkeypatch.setattr(clipboard.subprocess, "run", fake_run)
    assert clipboard.copy_to_system_clipboard("hello") is True
    assert calls == [b"hello"]


def test_copy_to_system_clipboard_failure(monkeypatch):
    """命令非 0 / 抛异常 / 空文本 → False。"""

    monkeypatch.setattr(clipboard, "system_clipboard_command", lambda: ["/bin/false"])

    class _Proc:
        returncode = 1

    monkeypatch.setattr(clipboard.subprocess, "run", lambda cmd, **kw: _Proc())
    assert clipboard.copy_to_system_clipboard("hello") is False

    def boom(cmd, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(clipboard.subprocess, "run", boom)
    assert clipboard.copy_to_system_clipboard("hello") is False

    assert clipboard.copy_to_system_clipboard("") is False


# ---------- 端到端（真 tmux） ----------

@pytest.fixture
def tmux_backend():
    """隔离的 tmux 实例（测试结束自动清理）。"""
    name = f"myrtui-v13-{uuid.uuid4().hex[:8]}"
    backend = TmuxBackend(name, name)
    root = tempfile.mkdtemp(prefix="myrtui-v13-")
    backend.create_session(root, 80, 24)
    yield backend, root
    backend.kill_server()
    time.sleep(0.2)


def test_end_to_end_select_and_copy_with_real_tmux(tmux_backend, monkeypatch):
    """真 tmux 端到端：真 capture 内容 → 拖拽选中 → ``Ctrl-a c`` 复制。

    系统剪贴板通道**注入假实现**——测试不该动用户真实的剪贴板。
    """
    backend, root = tmux_backend
    copied: list[str] = []
    monkeypatch.setattr(
        clipboard, "copy_to_system_clipboard", lambda text: copied.append(text) or True
    )

    async def scenario():
        app = TerminalApp(backend, root)
        app._copy_on_select = False  # v0.35：本用例只测 Ctrl-a c 手动复制
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            pid = app._pane_ids[0]
            # 往真实 shell 里打一行 → capture 回来是真内容
            backend.send_text(pid, "echo hello-from-tmux")
            backend.send_keys(pid, "Enter")
            await pilot.pause(1.2)
            pane = app.query_one("#pane-0", PaneWidget)
            row = next(
                i for i, line in enumerate(pane._lines) if "hello-from-tmux" in line.plain
            )
            col = pane._lines[row].plain.index("hello-from-tmux")

            # 拖过 "hello"（**终点格含在内**——Textual 的选区口径）
            await pilot.mouse_down(pane, offset=(col + 1, row + 1))
            await pilot.mouse_up(pane, offset=(col + 6, row + 1))
            await pilot.pause(0.2)
            assert app.screen.get_selected_text() == "hello-"

            await pilot.press("ctrl+a")
            await pilot.press("c")
            await pilot.pause(0.3)
            assert app._clipboard == "hello-"
            assert copied == ["hello-"]
            assert "已复制" in app.query_one(TopBar)._status

            # 无选区 → 不复制（只提示）
            app.screen.clear_selection()
            await pilot.pause(0.1)
            await pilot.press("ctrl+a")
            await pilot.press("c")
            await pilot.pause(0.2)
            assert copied == ["hello-"], "无选区时不得再写剪贴板"
            assert app.query_one(TopBar)._status == "无可复制内容"

    asyncio.run(scenario())
