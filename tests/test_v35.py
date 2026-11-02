"""myrtui v0.35 测试：左键拖拽永远选字 + 松手自动复制（现代鼠标模型）。

分层：

- **纯逻辑**：``resolve_copy_on_select`` / ``resolve_drag_select`` /
  ``clipboard.is_remote_session``；
- **选区提取**：逐行裁尾随补白、中文 / 宽字符、跨 widget（向下 / 向上）、
  空行、只选空格不复制；
- **手势**：拖拽不注入 + 自动复制、点击合成 press+release、1/2 格阈值、
  右键/中键旧路径、``MYRTUI_DRAG_SELECT=app``、``MYRTUI_COPY_ON_SELECT=0``
  的 hold 路径、冻结 / 解冻、resize / dead / 超时收摊、超时后释放不补注入、
  点击时序（Screen 先清选区）；
- **剪贴板语义**：SSH 只发 OSC 52、``Ctrl-a v`` 只提示终端粘贴键。

**测试红线**：拖拽默认会自动复制 → 所有会触发复制的用例都 monkeypatch
``clipboard.copy_to_system_clipboard``，**绝不碰真实系统剪贴板**。
"""
from __future__ import annotations

import asyncio

import pytest

from myrtui.client import clipboard
from myrtui.client.app import (
    TerminalApp,
    resolve_copy_on_select,
    resolve_drag_select,
)
from myrtui.client.pane_widget import PaneWidget
from myrtui.mouse import LEVEL_ALL, press, release
from myrtui.tmux_backend import CaptureResult, PaneInfo

PANE_TEXT = "abcdefghij\nsecond line\nthird line\n"
TWO_PANE_TEXT = "ab\ncd\n"

MOUSE_1003 = (False, False, True, True)


@pytest.fixture(autouse=True)
def _pin_drag_select_always(monkeypatch):
    """本文件锚定 v0.35 的 ``always`` 路径。

    0.7.0 把默认改成 ``smart``（程序要鼠标时拖动归程序）；这里的用例讲的是
    「拖拽永远选字」那一档，必须显式钉住，否则语义会随默认值漂移。
    ``smart`` 自己的用例在 ``tests/test_v38.py``。
    """
    monkeypatch.setenv("MYRTUI_DRAG_SELECT", "always")


# ---------- 假后端 ----------


def _pane(
    index: int,
    *,
    mouse: tuple[bool, bool, bool, bool] = (False, False, False, False),
    dead: bool = False,
) -> PaneInfo:
    std, btn, any_, all_ = mouse
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
        mouse_standard=std,
        mouse_button=btn,
        mouse_any=any_,
        mouse_sgr=True,
        mouse_all=all_,
    )


class _FakeBackend:
    def __init__(
        self,
        count: int = 1,
        text: str = PANE_TEXT,
        mouse: dict[int, tuple[bool, bool, bool, bool]] | None = None,
        dead: bool = False,
    ):
        self.count = count
        self.text = text
        self.mouse = mouse or {}
        self.dead = dead
        self.sent: list[tuple] = []
        self.pasted: list[tuple[str, str]] = []

    def list_panes(self):
        return [
            _pane(
                i,
                mouse=self.mouse.get(i, (False, False, False, False)),
                dead=self.dead,
            )
            for i in range(self.count)
        ]

    def poll(self, offsets):
        panes = self.list_panes()
        caps = {
            p.pane_id: CaptureResult(text=self.text, offset=0) for p in panes
        }
        return panes, caps

    def resize_window(self, window_id, cols, rows):
        pass

    def send_keys(self, pane_id, *keys):
        self.sent.append(("keys", pane_id, keys))

    def send_text(self, pane_id, text):
        self.sent.append(("text", pane_id, text))

    def send_ctrl_a(self, pane_id):
        self.sent.append(("ctrl_a", pane_id))

    def send_mouse(self, pane_id, sequence):
        self.sent.append(("mouse", pane_id, sequence))

    def paste(self, pane_id, text):
        self.pasted.append((pane_id, text))


@pytest.fixture
def copied(monkeypatch):
    """假系统剪贴板写通道（记录文本，绝不碰真实剪贴板）。"""
    out: list[str] = []
    monkeypatch.setattr(
        clipboard,
        "copy_to_system_clipboard",
        lambda text: out.append(text) or True,
    )
    return out


def _mouse_sent(backend: _FakeBackend) -> list[tuple[str, str]]:
    return [(pid, seq) for kind, pid, seq in backend.sent if kind == "mouse"]


async def _drag(pilot, pane: PaneWidget, start, end) -> None:
    """pane 内容坐标（0-based，不含边框）拖拽：按下 → 移动 → 松开。"""
    await pilot.mouse_down(pane, offset=(start[0] + 1, start[1] + 1))
    await pilot.mouse_up(pane, offset=(end[0] + 1, end[1] + 1))
    await pilot.pause(0.1)


# ---------- 纯逻辑 ----------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, True),
        ("", True),
        ("1", True),
        ("true", True),
        ("garbage", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        ("OFF", False),
    ],
)
def test_resolve_copy_on_select(raw, expected):
    assert resolve_copy_on_select(raw) is expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        # 0.7.0 起默认是 smart（真终端约定）；always 仍可显式选。
        (None, "smart"),
        ("", "smart"),
        ("always", "always"),
        ("app", "app"),
        ("APP", "app"),
        ("smart", "smart"),
        ("garbage", "smart"),
    ],
)
def test_resolve_drag_select(raw, expected):
    assert resolve_drag_select(raw) == expected


def test_is_remote_session(monkeypatch):
    for key in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
        monkeypatch.delenv(key, raising=False)
    assert clipboard.is_remote_session() is False
    for key in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
        monkeypatch.setenv(key, "x")
        assert clipboard.is_remote_session() is True
        monkeypatch.delenv(key)


# ---------- 选区提取 ----------


def test_selection_strips_line_padding(copied, app_root):
    """整行选择 → 行尾补白（parse_sgr_line 的 pad_right）被裁掉。"""

    async def scenario():
        backend = _FakeBackend(1, text="abc\n")
        app = TerminalApp(backend, app_root)
        app._copy_on_select = False
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            await _drag(pilot, pane, (0, 0), (10, 0))
            assert app.screen.get_selected_text() == "abc"

    asyncio.run(scenario())


def test_selection_chinese_wide_char(copied, app_root):
    """中文 / 宽字符：cell 0 → cell 4 = ``中文a``（cell-aware 偏移）。"""

    async def scenario():
        backend = _FakeBackend(1, text="中文abc\n")
        app = TerminalApp(backend, app_root)
        app._copy_on_select = False
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            await _drag(pilot, pane, (0, 0), (4, 0))
            assert app.screen.get_selected_text() == "中文a"

    asyncio.run(scenario())


def test_selection_cross_into_sidebar_only_pressed_pane(copied, app_root):
    """向下拖进侧栏：只复制按下 pane 的内容（不含侧栏文字）。"""

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        app._copy_on_select = False
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            await pilot.mouse_down(pane, offset=(1, 1))
            await pilot.mouse_up("#file-tree", offset=(1, 2))
            await pilot.pause(0.1)
            text = app.screen.get_selected_text()
            assert text == "abcdefghij\nsecond line\nthird line", text
            assert "README" not in text and "notes.txt" not in text

    asyncio.run(scenario())


def test_selection_keeps_blank_lines(copied, app_root):
    """向下拖过空白行：空行保留为空行（逐行 rstrip 不会吞掉换行）。"""

    async def scenario():
        backend = _FakeBackend(1, text="aa\n\nbb\n")
        app = TerminalApp(backend, app_root)
        app._copy_on_select = False
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            await _drag(pilot, pane, (0, 0), (1, 2))
            assert app.screen.get_selected_text() == "aa\n\nbb"

    asyncio.run(scenario())


def test_selecting_only_spaces_copies_nothing(copied, app_root):
    """只选到空格 → ``strip()`` 为空 → 不复制、不弹提示、直接解冻。"""

    async def scenario():
        backend = _FakeBackend(1, text="   \n")
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            await _drag(pilot, pane, (0, 0), (20, 0))
            assert copied == []
            assert app._notice_text == ""
            assert app._frozen_pane is None
            assert not app.screen.selections

    asyncio.run(scenario())


# ---------- 手势 ----------


def test_drag_in_mouse_pane_selects_and_autocopies(copied, app_root):
    """1003 pane 左键拖拽 → 本地选区 + 自动复制；**不**注入任何鼠标序列。"""

    async def scenario():
        backend = _FakeBackend(1, mouse={0: MOUSE_1003})
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            assert app._mouse_levels.get("p0") == LEVEL_ALL
            await _drag(pilot, pane, (4, 0), (8, 0))
            await pilot.pause(0.2)
            assert app._clipboard == "efghi"
            assert copied == ["efghi"]
            assert "已复制" in app._notice_text
            assert _mouse_sent(backend) == []
            # 高亮保留 600ms 后解冻 + 清选区
            assert app._frozen_pane == "p0"
            await pilot.pause(0.8)
            assert app._frozen_pane is None
            assert not app.screen.selections
            assert app._selection_pane is None

    asyncio.run(scenario())


def test_click_in_mouse_pane_synthesizes_press_release(copied, app_root):
    """1003 pane 左键点击 → 清选区 + 一次合成的 press+release（按下点坐标）。"""

    async def scenario():
        backend = _FakeBackend(1, mouse={0: MOUSE_1003})
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            await pilot.click("#pane-0", offset=(6, 3))
            await pilot.pause(0.3)
            assert _mouse_sent(backend) == [
                ("p0", press(5, 2, 1, True) + release(5, 2, 1, True))
            ]
            assert copied == []
            assert not app.screen.selections

    asyncio.run(scenario())


def test_click_without_mouse_does_not_inject(copied, app_root):
    """无鼠标 pane 左键点击 → 只聚焦，不注入。"""

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            await pilot.click(pane, offset=(6, 3))
            await pilot.pause(0.2)
            assert _mouse_sent(backend) == []
            assert copied == []
            assert app.focused is pane

    asyncio.run(scenario())


def test_one_cell_is_click_two_cells_is_drag(copied, app_root):
    """阈值：移动 1 格 = 点击（注入）；移动 2 格 = 拖拽（不注入、复制）。"""

    async def one_cell():
        backend = _FakeBackend(1, mouse={0: MOUSE_1003})
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            await _drag(pilot, pane, (5, 0), (6, 0))
            await pilot.pause(0.2)
            assert _mouse_sent(backend) == [
                ("p0", press(5, 0, 1, True) + release(5, 0, 1, True))
            ]
            assert copied == []

    async def two_cells():
        backend = _FakeBackend(1, mouse={0: MOUSE_1003})
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            await _drag(pilot, pane, (5, 0), (7, 0))
            await pilot.pause(0.2)
            assert _mouse_sent(backend) == []
            assert copied == ["fgh"]

    asyncio.run(one_cell())
    asyncio.run(two_cells())


@pytest.mark.parametrize("button", [2, 3])
def test_right_middle_click_uses_old_path(copied, app_root, button):
    """右键 / 中键在 1003 pane：照旧注入 press+release，不复制、不留选区。"""

    async def scenario():
        backend = _FakeBackend(1, mouse={0: MOUSE_1003})
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            await pilot.click("#pane-0", offset=(6, 3), button=button)
            await pilot.pause(0.3)
            assert _mouse_sent(backend) == [
                ("p0", press(5, 2, button, True)),
                ("p0", release(5, 2, button, True)),
            ]
            assert copied == []
            assert not app.screen.selections

    asyncio.run(scenario())


def test_app_mode_restores_drag_forwarding(monkeypatch, copied, app_root):
    """``MYRTUI_DRAG_SELECT=app`` + 1003 pane → v0.34 旧行为（拖拽转发）。"""

    monkeypatch.setenv("MYRTUI_DRAG_SELECT", "app")

    async def scenario():
        backend = _FakeBackend(1, mouse={0: MOUSE_1003})
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            assert app._drag_select_mode == "app"
            assert pane.allow_select is False
            await _drag(pilot, pane, (5, 2), (8, 3))
            await pilot.pause(0.3)
            sent = _mouse_sent(backend)
            assert sent and sent[0] == ("p0", press(5, 2, 1, True))
            assert any(seq.endswith("m") for _, seq in sent), sent
            assert copied == []
            assert not app.screen.selections

    asyncio.run(scenario())


def test_copy_on_select_off_waits_for_ctrl_a_c(
    monkeypatch, copied, app_root
):
    """``MYRTUI_COPY_ON_SELECT=0``：松手不复制，进入 hold；``Ctrl-a c`` 复制并解冻。"""

    monkeypatch.setenv("MYRTUI_COPY_ON_SELECT", "0")

    async def scenario():
        backend = _FakeBackend(1, mouse={0: MOUSE_1003})
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            await _drag(pilot, pane, (4, 0), (8, 0))
            await pilot.pause(0.2)
            assert copied == []
            assert app._hold_selection is True
            assert app._frozen_pane == "p0"
            assert "已选中 5 字符" in app._notice_text
            assert "Ctrl-a c" in app._notice_text

            await pilot.press("ctrl+a")
            await pilot.press("c")
            await pilot.pause(0.2)
            assert copied == ["efghi"]
            assert app._hold_selection is False
            assert app._frozen_pane is None
            assert not app.screen.selections

    asyncio.run(scenario())


def test_frozen_pane_ignores_capture_updates(copied, app_root):
    """拖拽中 capture 更新 → 冻结 pane 的 ``_lines`` 不变；解冻后恢复更新。"""

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            await pilot.mouse_down(pane, offset=(1, 1))
            await pilot.pause(0.1)
            assert app._frozen_pane == "p0"
            old_first = pane._lines[0].plain

            backend.text = "ZZZZZZZZZZ\nsecond line\nthird line\n"
            app._request_poll()
            await pilot.pause(0.4)
            assert pane._lines[0].plain == old_first, "冻结期间画面不得更新"

            await pilot.mouse_up(pane, offset=(3, 1))
            await pilot.pause(0.8)  # 高亮期结束 → 解冻
            app._request_poll()
            await pilot.pause(0.4)
            assert pane._lines[0].plain.startswith("ZZZZZZZZZZ")

    asyncio.run(scenario())


def test_resize_cancels_gesture(copied, app_root):
    """拖拽中 resize → 取消手势 + 解冻 + 清选区（几何已过期）。"""

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            await pilot.mouse_down(pane, offset=(1, 1))
            await pilot.pause(0.1)
            assert app._gesture is not None
            app.on_resize(None)
            await pilot.pause(0.1)
            assert app._gesture is None
            assert app._frozen_pane is None
            assert not app.screen.selections
            assert copied == []

    asyncio.run(scenario())


def test_dead_pane_cancels_gesture(copied, app_root):
    """拖拽中 pane 变 dead → 取消手势、解冻、不复制。"""

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            await pilot.mouse_down(pane, offset=(1, 1))
            await pilot.pause(0.1)
            assert app._gesture is not None
            backend.dead = True
            app._request_poll()
            await pilot.pause(0.4)
            assert app._gesture is None
            assert app._frozen_pane is None
            assert not app.screen.selections
            assert copied == []

    asyncio.run(scenario())


def test_gesture_timeout_cancels_without_copy(copied, app_root):
    """30s 手势安全超时 → 解冻、不复制、不卡死。"""

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            await pilot.mouse_down(pane, offset=(1, 1))
            await pilot.pause(0.1)
            assert app._gesture is not None
            app._thaw_after()
            await pilot.pause(0.1)
            assert app._gesture is None
            assert app._frozen_pane is None
            assert not app.screen.selections
            assert copied == []

    asyncio.run(scenario())


def test_hold_timeout_cancels_without_copy(monkeypatch, copied, app_root):
    """10s hold 超时 → 清选区、解冻、不复制。"""

    monkeypatch.setenv("MYRTUI_COPY_ON_SELECT", "0")

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            await _drag(pilot, pane, (4, 0), (8, 0))
            await pilot.pause(0.1)
            assert app._hold_selection is True
            app._thaw_after()
            await pilot.pause(0.1)
            assert app._hold_selection is False
            assert app._frozen_pane is None
            assert not app.screen.selections
            assert copied == []

    asyncio.run(scenario())


def test_release_after_timeout_does_not_inject(copied, app_root):
    """超时收摊后才到达的 release → 不得补一次误注入（修订 1 回归）。"""

    async def scenario():
        backend = _FakeBackend(1, mouse={0: MOUSE_1003})
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            await pilot.mouse_down(pane, offset=(6, 3))
            await pilot.pause(0.1)
            assert app._gesture is not None
            app._thaw_after()  # 模拟 30s 安全超时
            await pilot.pause(0.1)
            await pilot.mouse_up(pane, offset=(6, 3))
            await pilot.pause(0.3)
            assert _mouse_sent(backend) == []
            assert copied == []

    asyncio.run(scenario())


def test_cross_widget_into_sidebar_only_pressed_pane(copied, app_root):
    """拖进侧栏（终点在按下点下方）→ pressed pane 是 start widget，
    只取「按下点 → pane 末尾」，不含侧栏文字。"""

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        app._copy_on_select = False
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            await pilot.mouse_down(pane, offset=(5, 1))
            # 侧栏文件树第 4 行 → screen y 比按下点更低 → 不触发 start/end 交换
            await pilot.mouse_up("#file-tree", offset=(1, 4))
            await pilot.pause(0.1)
            text = app.screen.get_selected_text()
            assert text == "efghij\nsecond line\nthird line", text
            assert "README" not in text and "notes.txt" not in text

    asyncio.run(scenario())


def test_cross_widget_upward_copies_pressed_pane_top(copied, app_root):
    """向上拖进另一个 widget（交换）→ 复制「pane 顶部 → 按下点」，不返回 None。

    ``_apply_content_selections`` 交换 start/end widget 时**一并交换 content
    offset**：pressed pane 变成 end widget，拿到 ``Selection(None, S)``，S 是
    它**自己的** start offset（修订 2，按 A）。
    """

    async def scenario():
        backend = _FakeBackend(2, text=TWO_PANE_TEXT)
        app = TerminalApp(backend, app_root)
        app._copy_on_select = False
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause(0.3)
            pane0 = app.query_one("#pane-0", PaneWidget)
            pane1 = app.query_one("#pane-1", PaneWidget)
            # 按下在下面的 pane-1（内容 (2,0)），向上拖到 pane-0
            await pilot.mouse_down(pane1, offset=(3, 1))
            await pilot.mouse_up(pane0, offset=(3, 2))
            await pilot.pause(0.1)
            assert app._selection_pane == "p1"
            text = app.screen.get_selected_text()
            assert text == "ab", text  # pane 顶部 → 按下点，不含 pane-0 的文字

    asyncio.run(scenario())


def test_click_timing_sends_single_combined_sequence(copied, app_root):
    """点击时序回归：Screen 层 MouseUp 先清选区；最终只注入一次（合并序列）。"""

    async def scenario():
        backend = _FakeBackend(1, mouse={0: MOUSE_1003})
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            await pilot.click("#pane-0", offset=(6, 3))
            await pilot.pause(0.3)
            sent = _mouse_sent(backend)
            assert len(sent) == 1, sent
            assert sent[0][1] == press(5, 2, 1, True) + release(5, 2, 1, True)
            assert not app.screen.selections

    asyncio.run(scenario())


# ---------- 剪贴板语义 ----------


def test_ssh_copy_still_writes_system_clipboard(monkeypatch, app_root):
    """0.7.0：SSH 不再是「只用 OSC 52」的理由。

    v0.35 的默认在 0.7.0 被实测推翻：SSH 里 ``pbcopy`` 也能写登录用户的
    pasteboard（远程桌面客户端通常还会同步），而 OSC 52 是否被外层终端接受
    无从判断。默认两条都发，由 ``MYRTUI_CLIPBOARD`` 显式收窄。
    """

    monkeypatch.setenv("SSH_CONNECTION", "192.0.2.10 1234 22")  # TEST-NET-1，避免私网地址
    called: list[str] = []
    monkeypatch.setattr(
        clipboard,
        "copy_to_system_clipboard",
        lambda text: called.append(text) or True,
    )

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            app.copy_text("oscy")
            await pilot.pause(0.1)
            assert app._clipboard == "oscy"
            assert called == ["oscy"]  # 系统通道照样写
            assert "OSC 52" not in app._notice_text

    asyncio.run(scenario())


def test_clipboard_osc52_only_copy(monkeypatch, app_root):
    """``MYRTUI_CLIPBOARD=osc52``：只发 OSC 52，不调系统命令。"""

    monkeypatch.setenv("MYRTUI_CLIPBOARD", "osc52")
    called: list[str] = []
    monkeypatch.setattr(
        clipboard,
        "copy_to_system_clipboard",
        lambda text: called.append(text) or True,
    )

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            app.copy_text("oscy")
            await pilot.pause(0.1)
            assert app._clipboard == "oscy"
            assert called == []
            assert "OSC 52" in app._notice_text

    asyncio.run(scenario())


def test_clipboard_osc52_only_paste_points_to_terminal(monkeypatch, app_root):
    """``MYRTUI_CLIPBOARD=osc52`` 的 ``Ctrl-a v``：提示三种常见外层终端的
    粘贴键，不读剪贴板、不调用 backend.paste。

    0.7.0 起这条提示由「系统通道被显式关掉」触发，不再由「是否 SSH」触发。
    """

    monkeypatch.setenv("MYRTUI_CLIPBOARD", "osc52")
    monkeypatch.setattr(
        clipboard,
        "read_system_clipboard",
        lambda: pytest.fail("osc52-only 模式不得读系统剪贴板"),
    )

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        app._copy_buffer = "internal-old"
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("ctrl+a")
            await pilot.press("v")
            await pilot.pause(0.2)
            assert backend.pasted == []
            assert "Cmd+V" in app._notice_text
            assert "Ctrl+V" in app._notice_text
            assert "Ctrl+Shift+V" in app._notice_text

    asyncio.run(scenario())


def test_ssh_paste_reads_system_clipboard(monkeypatch, app_root):
    """0.7.0：SSH 会话的 ``Ctrl-a v`` 照样读系统剪贴板（远程桌面会同步）。"""

    monkeypatch.setenv("SSH_TTY", "/dev/ttys042")
    monkeypatch.setattr(clipboard, "read_system_clipboard", lambda: "remote-text")

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("ctrl+a")
            await pilot.press("v")
            await pilot.pause(0.2)
            assert backend.pasted == [("p0", "remote-text")]

    asyncio.run(scenario())
