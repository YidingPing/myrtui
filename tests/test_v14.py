"""myrtui v0.14 测试：``Ctrl-a v`` 粘贴（系统剪贴板优先 + 内部缓冲回落）。

> v0.35 反转了粘贴优先级：本地**系统剪贴板优先**，读不到（``None``）才回落
> 内部缓冲；SSH 会话只提示终端粘贴键（见 ``tests/test_v35.py``）。

分层：
- **读通道**：``clipboard.read_system_clipboard`` 的探测、成功、失败路径
  （含 PowerShell 那条多补的尾换行）；
- **粘贴**：
  - 系统剪贴板有内容 → 优先用它（即使内部缓冲有旧值）；
  - 系统剪贴板读不到（``None``）→ 回落内部缓冲（``Ctrl-a c`` 的选区）；
  - 两边都空 / 剪贴板为空（``""``）→ 提示「无可粘贴内容」，**不**调 ``backend.paste``；
  - dead pane → 静默不粘；侧栏聚焦 → 仍粘（前缀键全局有效）；
- **端到端**：真 tmux 里 ``Ctrl-a c`` 复制选区 → ``Ctrl-a v`` 粘回 pane。

假后端只提供拓扑与 ``paste`` 记账（不碰真 tmux、不碰用户真实剪贴板）。
"""
from __future__ import annotations

import asyncio
import tempfile
import time
import uuid

import pytest

from myrtui.client import clipboard
from myrtui.client.app import TerminalApp
from myrtui.client.pane_widget import PaneWidget
from myrtui.client.topbar import TopBar
from myrtui.tmux_backend import CaptureResult, PaneInfo, TmuxBackend

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
    """假后端：固定拓扑 + 固定内容 + ``paste`` 记账。"""

    def __init__(self, count: int = 1, text: str = PANE_TEXT, dead: bool = False):
        self.count = count
        self.text = text
        self.dead = dead
        self.pasted: list[tuple[str, str]] = []
        self.sent: list[tuple] = []

    def list_panes(self):
        return [_pane(i, dead=self.dead) for i in range(self.count)]

    def poll(self, offsets):
        panes = self.list_panes()
        caps = {p.pane_id: CaptureResult(text=self.text, offset=0) for p in panes}
        return panes, caps

    def resize_window(self, window_id, cols, rows):
        pass

    def paste(self, pane_id, text):
        self.pasted.append((pane_id, text))

    def send_keys(self, pane_id, *keys):
        self.sent.append(("keys", pane_id, keys))

    def send_text(self, pane_id, text):
        self.sent.append(("text", pane_id, text))

    def send_mouse(self, pane_id, sequence):
        self.sent.append(("mouse", pane_id, sequence))


async def _drag_select(pilot, pane: PaneWidget, start, end) -> None:
    """在 pane 内容区拖出一段选区（内容坐标，0-based，不含边框）。"""
    await pilot.mouse_down(pane, offset=(start[0] + 1, start[1] + 1))
    await pilot.mouse_up(pane, offset=(end[0] + 1, end[1] + 1))
    await pilot.pause(0.1)


# ---------- 读通道 ----------

def test_read_probe_prefers_platform_command(monkeypatch):
    """按平台探测**读**命令：darwin→pbpaste、win32→powershell、linux→xclip。"""

    def probe(platform, which_map):
        monkeypatch.setattr(clipboard.sys, "platform", platform)
        monkeypatch.setattr(clipboard.shutil, "which", lambda exe: which_map.get(exe))
        monkeypatch.setattr(clipboard, "_read_probed", False)
        monkeypatch.setattr(clipboard, "_read_command", None)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        return clipboard.system_clipboard_read_command()

    assert probe("darwin", {"pbpaste": "/usr/bin/pbpaste"}) == (
        ["/usr/bin/pbpaste"],
        False,
    )
    argv, strip = probe(
        "win32", {"powershell": "C:/powershell.exe"}
    )
    assert argv[0] == "C:/powershell.exe"
    assert "Get-Clipboard" in argv[-1] and strip is True
    assert probe(
        "linux", {"xclip": "/usr/bin/xclip", "pbpaste": "/usr/bin/pbpaste"}
    ) == (["/usr/bin/xclip", "-selection", "clipboard", "-o"], False)


def test_read_probe_prefers_wl_paste_under_wayland(monkeypatch):
    """Wayland 会话里优先 wl-paste。"""

    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.setattr(clipboard.shutil, "which", lambda exe: f"/usr/bin/{exe}")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(clipboard, "_read_probed", False)
    monkeypatch.setattr(clipboard, "_read_command", None)
    assert clipboard.system_clipboard_read_command() == (
        ["/usr/bin/wl-paste", "--no-newline"],
        False,
    )


def test_read_clipboard_success(monkeypatch):
    """读成功 → 返回解码后的文本。"""

    monkeypatch.setattr(clipboard, "system_clipboard_read_command", lambda: (["/bin/cat"], False))

    class _Proc:
        returncode = 0
        stdout = "héllo".encode("utf-8")

    monkeypatch.setattr(clipboard.subprocess, "run", lambda cmd, **kw: _Proc())
    assert clipboard.read_system_clipboard() == "héllo"


def test_read_clipboard_strips_powershell_extra_newline(monkeypatch):
    """PowerShell 那条会**多补一个**换行 → 剥掉一个（否则粘贴凭空多一次回车）。"""

    monkeypatch.setattr(
        clipboard, "system_clipboard_read_command", lambda: (["powershell"], True)
    )

    class _Proc:
        returncode = 0
        stdout = b"line\r\n"

    monkeypatch.setattr(clipboard.subprocess, "run", lambda cmd, **kw: _Proc())
    assert clipboard.read_system_clipboard() == "line"


def test_read_clipboard_failure(monkeypatch):
    """命令不存在 / 非 0 / 抛异常 / 超时 → None。"""

    monkeypatch.setattr(clipboard, "system_clipboard_read_command", lambda: None)
    assert clipboard.read_system_clipboard() is None

    monkeypatch.setattr(
        clipboard, "system_clipboard_read_command", lambda: (["/bin/false"], False)
    )

    class _Proc:
        returncode = 1
        stdout = b""

    monkeypatch.setattr(clipboard.subprocess, "run", lambda cmd, **kw: _Proc())
    assert clipboard.read_system_clipboard() is None

    def boom(cmd, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(clipboard.subprocess, "run", boom)
    assert clipboard.read_system_clipboard() is None

    def timeout(cmd, **kwargs):
        raise clipboard.subprocess.TimeoutExpired(cmd, 2.0)

    monkeypatch.setattr(clipboard.subprocess, "run", timeout)
    assert clipboard.read_system_clipboard() is None


# ---------- 粘贴 ----------

def test_paste_falls_back_to_internal_when_system_unavailable(
    monkeypatch, app_root
):
    """系统剪贴板**读不到**（None）→ 回落到内部缓冲（``Ctrl-a c`` 的选区）。"""

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        app._copy_on_select = False  # v0.35：拖拽不自动复制，只测粘贴回落
        monkeypatch.setattr(clipboard, "read_system_clipboard", lambda: None)
        monkeypatch.setattr(
            clipboard, "copy_to_system_clipboard", lambda text: True
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            await _drag_select(pilot, pane, (4, 0), (8, 0))
            assert app.screen.get_selected_text() == "efghi"

            await pilot.press("ctrl+a")
            await pilot.press("c")
            await pilot.pause(0.1)
            assert app._copy_buffer == "efghi"

            await pilot.press("ctrl+a")
            await pilot.press("v")
            await pilot.pause(0.2)
            assert backend.pasted == [("p0", "efghi")]
            assert "已粘贴" in app.query_one(TopBar)._status

    asyncio.run(scenario())


def test_paste_prefers_system_clipboard(monkeypatch, app_root):
    """v0.35：系统剪贴板**优先**——即使内部缓冲有内容（可能是旧值）。"""

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        app._copy_buffer = "internal-stale"
        monkeypatch.setattr(clipboard, "read_system_clipboard", lambda: "from-system")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("ctrl+a")
            await pilot.press("v")
            await pilot.pause(0.4)  # worker 线程读 + 回投
            assert backend.pasted == [("p0", "from-system")]

    asyncio.run(scenario())


def test_paste_empty_system_clipboard_does_not_fall_back(monkeypatch, app_root):
    """读到空剪贴板（``""``）≠ 读不到（``None``）：不拿旧内部缓冲顶替。"""

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        app._copy_buffer = "internal-stale"
        monkeypatch.setattr(clipboard, "read_system_clipboard", lambda: "")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("ctrl+a")
            await pilot.press("v")
            await pilot.pause(0.4)
            assert backend.pasted == []
            assert app.query_one(TopBar)._status == "无可粘贴内容"

    asyncio.run(scenario())


def test_paste_without_any_content_copies_nothing(monkeypatch, app_root):
    """两边都空 → 提示「无可粘贴内容」，**不**调 paste。"""

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        monkeypatch.setattr(clipboard, "read_system_clipboard", lambda: "")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("ctrl+a")
            await pilot.press("v")
            await pilot.pause(0.4)
            assert backend.pasted == []
            assert app.query_one(TopBar)._status == "无可粘贴内容"

    asyncio.run(scenario())


def test_paste_into_dead_pane_is_silent(monkeypatch, app_root):
    """dead pane → 静默不粘（与键盘透传同口径）。"""

    async def scenario():
        backend = _FakeBackend(1, dead=True)
        app = TerminalApp(backend, app_root)
        monkeypatch.setattr(clipboard, "read_system_clipboard", lambda: "text")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            assert app._dead.get("p0") is True, "poll 应已观测到 dead"
            await pilot.press("ctrl+a")
            await pilot.press("v")
            await pilot.pause(0.4)
            assert backend.pasted == []

    asyncio.run(scenario())


def test_paste_works_with_sidebar_focused(monkeypatch, app_root):
    """侧栏聚焦时 ``Ctrl-a v`` 仍粘贴到活动 pane（前缀键全局有效）。"""

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        monkeypatch.setattr(clipboard, "read_system_clipboard", lambda: "text")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            app.action_focus_sidebar()
            await pilot.pause(0.1)
            assert app._sidebar_focused()
            await pilot.press("ctrl+a")
            await pilot.press("v")
            await pilot.pause(0.4)
            assert backend.pasted == [("p0", "text")]

    asyncio.run(scenario())


# ---------- 端到端（真 tmux） ----------

@pytest.fixture
def tmux_backend():
    """隔离的 tmux 实例（测试结束自动清理）。"""
    name = f"myrtui-v14-{uuid.uuid4().hex[:8]}"
    backend = TmuxBackend(name, name)
    root = tempfile.mkdtemp(prefix="myrtui-v14-")
    backend.create_session(root, 80, 24)
    yield backend, root
    backend.kill_server()
    time.sleep(0.2)


def test_end_to_end_copy_then_paste_with_real_tmux(tmux_backend, monkeypatch):
    """真 tmux 闭环：拖出选区 → ``Ctrl-a c`` 复制 → ``Ctrl-a v`` 粘回 pane。"""

    backend, root = tmux_backend
    # 系统剪贴板通道注入假实现——测试不该动用户真实剪贴板。
    # v0.35：粘贴改为「系统剪贴板优先」，假读通道必须返回刚复制的内容。
    copied: list[str] = []
    monkeypatch.setattr(
        clipboard,
        "copy_to_system_clipboard",
        lambda text: copied.append(text) or True,
    )
    monkeypatch.setattr(
        clipboard,
        "read_system_clipboard",
        lambda: copied[-1] if copied else None,
    )

    async def scenario():
        app = TerminalApp(backend, root)
        app._copy_on_select = False  # v0.35：本用例只测 Ctrl-a c 手动复制
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            pid = app._pane_ids[0]
            backend.send_text(pid, "echo marker-from-tmux")
            backend.send_keys(pid, "Enter")
            await pilot.pause(1.2)
            pane = app.query_one("#pane-0", PaneWidget)
            row = next(
                i for i, line in enumerate(pane._lines) if "marker-from-tmux" in line.plain
            )
            col = pane._lines[row].plain.index("marker-from-tmux")

            # 选中 "marker"（终点格含在内——Textual 的选区口径）
            await pilot.mouse_down(pane, offset=(col + 1, row + 1))
            await pilot.mouse_up(pane, offset=(col + 7, row + 1))
            await pilot.pause(0.2)
            assert app.screen.get_selected_text() == "marker-"

            await pilot.press("ctrl+a")
            await pilot.press("c")
            await pilot.pause(0.2)
            assert app._copy_buffer == "marker-"

            # 粘回同一个 pane → shell 里出现这行字
            await pilot.press("ctrl+a")
            await pilot.press("v")
            await pilot.pause(1.0)
            lines = [line.plain.rstrip() for line in pane._lines]
            # 断言某行**以**被粘贴的文本结尾（粘贴落在 shell 提示符后面，
            # 不是独立一行）；不能用 in —— 原内容里就有 "marker-from-tmux"，
            # 它含 "marker-"，in 会假阳性
            assert any(line.endswith("marker-") for line in lines), lines

    asyncio.run(scenario())
