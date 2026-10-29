"""myrtui v0.15 测试：**框架继承键绑定清零**（tab / shift+tab / ctrl+q / ctrl+c）。

根因（用户反馈「按 Tab 焦点在 pane 之间跳，shell 补全没法用」）：
``MainScreen.BINDINGS = []`` / ``TerminalApp.BINDINGS = []`` **删不掉继承来的绑定**
——textual 8.2.8 ``dom.py::_merge_bindings`` 沿 MRO 逐基类合并，子类空列表只表示
「本类不贡献键」。修复是类关键字 ``inherit_bindings=False``。本文件锚点：

- 静态：两者的 ``_merged_bindings`` 必须为空（防止「注释改了、绑定还在」回归）；
- 行为：``tab`` / ``shift+tab`` 不再移动焦点且照常透传给内层 shell；
  ``ctrl+q`` 不再 quit 前端、``ctrl+c`` 不再触发 ``help_quit``，一律透传（C9.1）；
- 端到端：真 tmux 里连按两次 Tab 后仍能正常打字（用户可见症状的直接口径）。
"""
from __future__ import annotations

import asyncio
import tempfile
import time
import uuid

import pytest

from myrtui.client.app import KillConfirm, MainScreen, TerminalApp
from myrtui.client.pane_widget import PaneWidget
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


def _keys_sent(backend) -> list[tuple[str, str, tuple]]:
    return [row for row in backend.sent if row[0] == "keys"]


# ---------- 静态锚点：绑定必须真的没了 ----------

def test_main_screen_and_app_have_no_inherited_bindings():
    """``inherit_bindings=False`` 生效判据：screen/app 层零绑定。

    修复前实测：``MainScreen`` = ``['ctrl+c', 'shift+tab', 'super+c', 'tab']``、
    ``TerminalApp`` = ``['ctrl+c', 'ctrl+q']``。
    """
    assert MainScreen._merged_bindings.key_to_bindings == {}
    assert TerminalApp._merged_bindings.key_to_bindings == {}


def test_kill_confirm_keeps_its_own_bindings():
    """``KillConfirm`` **不**受影响：模态框里 Tab/Escape 归它自己管。"""
    keys = KillConfirm._merged_bindings.key_to_bindings
    assert "escape" in keys and "n" in keys and "tab" in keys


# ---------- tab / shift+tab：焦点不动、键照发 ----------

def test_tab_keeps_focus_on_active_pane_and_is_forwarded(app_root):
    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.4)
            pane = app.query_one("#pane-0", PaneWidget)
            assert app.focused is pane, "启动焦点应在活动 pane"

            await pilot.press("tab")
            await pilot.pause(0.2)
            assert app.focused is pane, "Tab 不得移动焦点（修复前会跳到文件树）"
            assert app._sidebar_focused() is False
            assert _keys_sent(backend) == [("keys", "p0", ("Tab",))]
    asyncio.run(scenario())


def test_shift_tab_keeps_focus_on_active_pane_and_is_forwarded(app_root):
    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.4)
            pane = app.query_one("#pane-0", PaneWidget)

            await pilot.press("shift+tab")
            await pilot.pause(0.2)
            assert app.focused is pane
            assert _keys_sent(backend) == [("keys", "p0", ("BTab",))]
    asyncio.run(scenario())


def test_tab_does_not_steal_focus_with_multiple_panes(app_root):
    """多 pane：连按 Tab 也不得离开活动 pane（修复前会跳到侧栏 / Git 历史）。"""

    async def scenario():
        backend = _FakeBackend(2)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.4)
            pane = app.query_one("#pane-0", PaneWidget)
            for _ in range(4):
                await pilot.press("tab")
                await pilot.pause(0.1)
                assert app.focused is pane, "Tab 不得离开活动 pane"
            await pilot.pause(0.2)
            assert app._sidebar_focused() is False
            assert app._active_index == 0
    asyncio.run(scenario())


def test_tab_while_sidebar_focused_stays_in_sidebar(app_root):
    """侧栏聚焦时 Tab：既不透传（D8）、也不把焦点甩到别处。"""

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.4)
            app.action_focus_sidebar()
            await pilot.pause(0.2)
            focused = app.focused
            assert app._sidebar_focused()

            await pilot.press("tab")
            await pilot.pause(0.2)
            assert app.focused is focused, "Tab 不得把焦点甩出侧栏"
            assert _keys_sent(backend) == [], "侧栏聚焦时 Tab 不透传（D8）"
    asyncio.run(scenario())


# ---------- ctrl+q / ctrl+c：不再吃键 ----------

def test_ctrl_q_is_forwarded_and_does_not_quit(app_root):
    """``ctrl+q`` 是 App 的 ``priority=True`` 绑定（修复前按下直接退出前端）。"""

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.4)
            await pilot.press("ctrl+q")
            await pilot.pause(0.3)
            assert app.is_running, "ctrl+q 不得 quit 前端"
            assert ("keys", "p0", ("C-q",)) in _keys_sent(backend)
    asyncio.run(scenario())


def test_ctrl_c_is_forwarded_and_does_not_help_quit(app_root):
    """``ctrl+c`` 原是 Screen/App 的绑定（copy_text / help_quit）→ 现在归 shell。"""

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.4)
            await pilot.press("ctrl+c")
            await pilot.pause(0.3)
            assert app.is_running
            assert ("keys", "p0", ("C-c",)) in _keys_sent(backend)
    asyncio.run(scenario())


# ---------- 端到端（真 tmux）：用户可见症状的直接口径 ----------

@pytest.fixture
def tmux_backend():
    """隔离的 tmux 实例（测试结束自动清理）。"""
    name = f"myrtui-v15-{uuid.uuid4().hex[:8]}"
    backend = TmuxBackend(name, name)
    root = tempfile.mkdtemp(prefix="myrtui-v15-")
    backend.create_session(root, 80, 24)
    yield backend, root
    backend.kill_server()
    time.sleep(0.2)


def test_shell_still_usable_after_tab_with_real_tmux(tmux_backend):
    """连按两次 Tab（shell 进补全态）→ 仍能正常打字回车（焦点没被偷走）。"""

    backend, root = tmux_backend

    async def scenario():
        app = TerminalApp(backend, root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(1.0)
            pid = app._pane_ids[0]

            await pilot.press("tab")
            await pilot.press("tab")
            await pilot.pause(0.3)
            assert app.focused is app.query_one("#pane-0", PaneWidget)
            assert app._sidebar_focused() is False

            # ① 逃逸补全查询态：runner 的 bash（≥4.4，分页文案 `--More--`；本机
            #    bash3.2 是 `(y or n)`）在**空行**双 Tab 后进入 readline 的
            #    `get_y_or_n()` 查询——该函数**忽略非 y/n 键并响铃继续等** →
            #    后续打字全被吃（CI macos task#170/174/176 三轮同症状：pane 里停在
            #    `--More--`，15 键一个都没回显）。ctrl+c 走 SIGINT 中止查询、
            #    ctrl+u 清行 → 两个 shell 都回到干净提示符（本机 zsh + bash 实测）。
            # ② 回显等待改轮询（B1 后 send_text 走串行执行器：每次一个 fork，
            #    慢机 15 键 >1s；固定 pause(1.0) 实测不够）。
            # 断言语义不变：焦点仍在 pane + 必须看到 TAB-USABLE（shell 收到输入）。
            async def _reset_line() -> None:
                await pilot.press("ctrl+c")
                await pilot.pause(0.3)
                await pilot.press("ctrl+u")
                await pilot.pause(0.2)

            await _reset_line()
            ok = False
            last = ""
            for attempt in range(2):  # 首轮 + 一次「重置后重打」的重试
                if attempt:
                    await _reset_line()
                for ch in "echo TAB-USABLE":
                    await pilot.press(ch if ch != " " else "space")
                await pilot.press("enter")
                for _ in range(30):  # 每轮最多 ~6s，两轮共 ~12s
                    await pilot.pause(0.2)
                    _, caps = backend.poll({pid: (0, 24)})
                    last = caps[pid].text
                    if "TAB-USABLE" in last:
                        ok = True
                        break
                if ok:
                    break
            assert ok, f"Tab 之后 shell 应仍能接收输入（两轮 ~12s 未见回显）：{last[-200:]!r}"

    asyncio.run(scenario())
