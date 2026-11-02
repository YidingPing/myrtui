"""myrtui 0.7.0 测试：pane 内 OSC 52 复制直通。

分层：

- **解析**（``myrtui.osc52``）：``%output`` 的八进制转义、裸形态 /
  DCS 包裹形态 / BEL 与 ST 终止符、多条、查询请求、非法 base64、体积上限；
- **后端**（``TmuxBackend``）：``%output`` 分流、去重、队列上限、关闭开关、
  ``exec`` 传输的 buffer 差分兜底；真 tmux 集成（control 两种形态 + exec 兜底）；
- **App**：``_drain_clipboard`` 转成一次 ``copy_text``（带 pane 槽位）、
  ``MYRTUI_CLIPBOARD`` 通道策略矩阵、``MYRTUI_DRAG_SELECT=smart`` 的归属。

**红线**：所有会走到复制的用例都 monkeypatch
``clipboard.copy_to_system_clipboard``——绝不碰真实系统剪贴板。
"""
from __future__ import annotations

import asyncio
import base64
import shutil
import time
import uuid

import pytest

from myrtui import osc52
from myrtui.client import clipboard
from myrtui.client.app import TerminalApp, resolve_drag_select
from myrtui.client.pane_widget import PaneWidget
from myrtui.tmux_backend import (
    TRANSPORT_CONTROL,
    TRANSPORT_EXEC,
    CaptureResult,
    ClipboardEvent,
    PaneInfo,
    TmuxBackend,
    resolve_passthrough,
)

PANE_TEXT = "abcdefghij\nsecond line\nthird line\n"
MOUSE_1003 = (False, False, True, True)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


# ---------------------------------------------------------------- 解析


def test_unescape_octal_and_backslash():
    assert osc52.unescape(r"\033]52;c;x\007") == "\x1b]52;c;x\x07"
    assert osc52.unescape(r"a\134b") == "a\\b"
    assert osc52.unescape("plain") == "plain"
    # 非八进制转义原样保留（tmux 不产生，但别让解析器吃掉字符）
    assert osc52.unescape(r"\t") == "t"


def test_extract_bare_bel():
    payload = r"\033]52;c;%s\007" % _b64("hello")
    assert osc52.extract(payload) == ["hello"]


def test_extract_bare_st_terminator():
    payload = r"\033]52;c;%s\033\134" % _b64("hello")
    assert osc52.extract(payload) == ["hello"]


def test_extract_dcs_passthrough():
    """opencode / codex 在 ``$TMUX`` 下发的就是这个形态（实测 tmux 3.8 连
    paste buffer 都不建，只有 ``%output`` 里能看到）。"""

    payload = r"\033Ptmux;\033]52;c;%s\033\134" % _b64("你好 dcs")
    assert osc52.extract(payload) == ["你好 dcs"]


def test_extract_doubled_escape():
    """内层 ESC 被程序重复成 ``ESC ESC`` 时同样成立。"""

    payload = r"\033Ptmux;\033\033]52;c;%s\033\033\134" % _b64("dup")
    assert osc52.extract(payload) == ["dup"]


def test_extract_multiple_in_one_frame():
    payload = (
        r"x\033]52;c;%s\007y\033]52;c;%s\007"
        % (_b64("one"), _b64("two"))
    )
    assert osc52.extract(payload) == ["one", "two"]


def test_extract_ignores_query_and_other_selection():
    assert osc52.extract(r"\033]52;c;?\007") == []
    assert osc52.extract(r"\033]52;p;%s\007" % _b64("primary")) == []


def test_extract_ignores_invalid_base64():
    assert osc52.extract(r"\033]52;c;not*base64\007") == []


def test_extract_rejects_oversized():
    body = _b64("x" * (osc52.MAX_BYTES + 1))
    assert osc52.extract(r"\033]52;c;%s\007" % body) == []


def test_extract_plain_output_is_free():
    """普通输出（刷屏）不产生任何事件——只做一次子串判断。"""

    assert osc52.extract(r"line\r\n" * 100) == []


# ---------------------------------------------------------------- 后端：开关与队列


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, True),
        ("", True),
        ("1", True),
        ("on", True),
        ("garbage", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("OFF", False),
    ],
)
def test_resolve_passthrough(raw, expected):
    assert resolve_passthrough(raw) is expected


def test_on_pane_output_pushes_event():
    backend = TmuxBackend("s", "s")
    backend._on_pane_output(
        "%output %0 " + r"\033]52;c;%s\007" % _b64("hi") + "\n"
    )
    events = backend.take_clipboard_events()
    assert events == [ClipboardEvent(text="hi", pane_id="%0")]
    assert backend.take_clipboard_events() == []  # 取走即清


def test_on_pane_output_dedupes_within_window():
    backend = TmuxBackend("s", "s")
    line = "%output %0 " + r"\033]52;c;%s\007" % _b64("same")
    backend._on_pane_output(line)
    backend._on_pane_output(line)
    assert len(backend.take_clipboard_events()) == 1
    # 窗口外重发同一内容 → 收（用户可能确实又粘了一次）
    backend._clip_last = ("same", time.monotonic() - 5.0)
    backend._on_pane_output(line)
    assert len(backend.take_clipboard_events()) == 1


def test_on_pane_output_ignores_non_osc_lines():
    backend = TmuxBackend("s", "s")
    backend._on_pane_output("%output %0 hello\n")
    backend._on_pane_output("%begin 1 2 0\n")
    assert backend.take_clipboard_events() == []


def test_on_pane_output_respects_kill_switch():
    backend = TmuxBackend("s", "s", clipboard_passthrough=False)
    backend._on_pane_output(
        "%output %0 " + r"\033]52;c;%s\007" % _b64("hi")
    )
    assert backend.take_clipboard_events() == []


def test_event_queue_is_bounded():
    from myrtui.tmux_backend import CLIPBOARD_MAX_EVENTS

    backend = TmuxBackend("s", "s")
    for i in range(CLIPBOARD_MAX_EVENTS + 4):
        backend._push_clipboard(f"text-{i}")
    events = backend.take_clipboard_events()
    assert len(events) == CLIPBOARD_MAX_EVENTS
    assert backend._clip_dropped >= 1


# ---------------------------------------------------------------- 后端：真 tmux


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def tmux_name():
    return f"myrtui-clip-{uuid.uuid4().hex[:8]}"


def test_control_transport_forwards_both_forms(tmp_path, tmux_name):
    """真 tmux：裸形态与 DCS 形态都要转发（DCS 现在 100% 失败于旧实现）。"""

    backend = TmuxBackend(tmux_name, tmux_name, transport=TRANSPORT_CONTROL)
    backend.create_session(str(tmp_path), 80, 24)
    try:
        assert backend.open_control() is True
        pane = backend.list_panes()[0].pane_id

        bare = _b64("bare-form")
        backend.run_command(
            pane, "printf '\\033]52;c;%s\\007'" % bare
        )
        assert _wait_until(lambda: backend.take_clipboard_events() != [])
        assert backend.take_clipboard_events() == []

        dcs = _b64("dcs-form")
        time.sleep(0.4)  # 跨过去重窗口
        backend.run_command(
            pane, "printf '\\033Ptmux;\\033]52;c;%s\\033\\\\'" % dcs
        )
        assert _wait_until(lambda: backend.take_clipboard_events() != [])
    finally:
        backend.kill_server()


def test_exec_transport_uses_buffer_diff(tmp_path, tmux_name):
    """``exec`` 兜底：裸形态经 tmux paste buffer 差分转发；自己的粘贴缓冲不转发。"""

    backend = TmuxBackend(tmux_name, tmux_name, transport=TRANSPORT_EXEC)
    backend.create_session(str(tmp_path), 80, 24)
    try:
        pane = backend.list_panes()[0].pane_id
        backend.poll_clipboard_buffers()  # 基线：启动前残留的 buffer 不转发
        assert backend.take_clipboard_events() == []

        backend.run_command(pane, "printf '\\033]52;c;%s\\007'" % _b64("exec-form"))
        time.sleep(0.3)
        backend._buffer_next = 0.0  # 跳过 1 s 节流（测试不睡两秒）
        backend.poll_clipboard_buffers()
        events = backend.take_clipboard_events()
        assert [event.text for event in events] == ["exec-form"]

        backend.paste(pane, "self-paste")
        time.sleep(0.2)
        backend._buffer_next = 0.0
        backend.poll_clipboard_buffers()
        assert backend.take_clipboard_events() == []
    finally:
        backend.kill_server()


# ---------------------------------------------------------------- App：转发


def _pane(index: int, *, mouse=(False, False, False, False)) -> PaneInfo:
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
    def __init__(self, count: int = 1, mouse=None):
        self.count = count
        self.mouse = mouse or {}
        self.events: list[ClipboardEvent] = []
        self.sent: list[tuple] = []
        self.pasted: list[tuple[str, str]] = []

    def list_panes(self):
        return [_pane(i, mouse=self.mouse.get(i, (False, False, False, False)))
                for i in range(self.count)]

    def poll(self, offsets):
        panes = self.list_panes()
        caps = {p.pane_id: CaptureResult(text=PANE_TEXT, offset=0) for p in panes}
        return panes, caps

    def take_clipboard_events(self):
        events, self.events = self.events, []
        return events

    def poll_clipboard_buffers(self):
        pass

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
    out: list[str] = []
    monkeypatch.setattr(
        clipboard,
        "copy_to_system_clipboard",
        lambda text: out.append(text) or True,
    )
    return out


def test_drain_clipboard_copies_with_pane_slot(copied, app_root):
    """pane 内复制 → 走公共出口，提示带槽位号。"""

    async def scenario():
        backend = _FakeBackend(2)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            backend.events.append(ClipboardEvent(text="from-agent", pane_id="p1"))
            app._tick()  # 走真实节拍入口，证明接线
            await pilot.pause(0.2)
            assert app.clipboard == "from-agent"
            assert copied == ["from-agent"]
            assert "2" in app._notice_text
            assert "from-agent" not in app._notice_text
            assert backend.events == []

    asyncio.run(scenario())


def test_drain_clipboard_ignores_blank(copied, app_root):
    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            backend.events.append(ClipboardEvent(text="   \n", pane_id="p0"))
            app._drain_clipboard()
            await pilot.pause(0.1)
            assert app.clipboard == ""
            assert copied == []

    asyncio.run(scenario())


def test_drain_clipboard_without_backend_hook(app_root):
    """后端替身没有剪贴板钩子时静默跳过（instances / 老替身）。"""

    class _Bare:
        pass

    backend = _FakeBackend(1)
    app = TerminalApp(backend, app_root)
    app.backend = _Bare()
    app._drain_clipboard()  # 不抛即可


def test_clipboard_passthrough_off_keeps_app_working(copied, app_root, monkeypatch):
    """``MYRTUI_CLIPBOARD_PASSTHROUGH=0``：App 侧照旧工作（后端不推事件）。"""

    monkeypatch.setenv("MYRTUI_CLIPBOARD_PASSTHROUGH", "0")
    assert resolve_passthrough("0") is False

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            app._drain_clipboard()
            assert copied == []

    asyncio.run(scenario())


# ---------------------------------------------------------------- 通道策略


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "auto"),
        ("", "auto"),
        ("AUTO", "auto"),
        ("osc52", "osc52"),
        ("system", "system"),
        ("garbage", "auto"),
    ],
)
def test_resolve_clipboard_channel(raw, expected):
    mode, _warning = clipboard.resolve_clipboard_channel(raw)
    assert mode == expected


def test_channel_helpers():
    assert clipboard.use_osc52("auto") and clipboard.use_system("auto")
    assert clipboard.use_osc52("osc52") and not clipboard.use_system("osc52")
    assert clipboard.use_system("system") and not clipboard.use_osc52("system")


# ---------------------------------------------------------------- smart 拖拽


def test_resolve_drag_select_smart():
    assert resolve_drag_select("smart") == "smart"
    assert resolve_drag_select("SMART") == "smart"
    assert resolve_drag_select(None) == "smart"  # 0.7.0 的默认
    assert resolve_drag_select("always") == "always"
    assert resolve_drag_select("nonsense") == "smart"


def test_default_mode_is_smart_for_mouse_panes(copied, app_root):
    """不设任何环境变量：程序要了鼠标 → 拖动直接归程序（0.7.0 的默认）。"""

    async def scenario():
        backend = _FakeBackend(1, mouse={0: MOUSE_1003})
        app = TerminalApp(backend, app_root)
        assert app._drag_select_mode == "smart"
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            await pilot.mouse_down(pane, offset=(1, 1))
            await pilot.mouse_up(pane, offset=(6, 1))
            await pilot.pause(0.1)
            assert [item for item in backend.sent if item[0] == "mouse"]
            assert copied == []

    asyncio.run(scenario())


def test_smart_drag_forwards_to_app(copied, monkeypatch, app_root):
    """程序请求了鼠标 + 普通拖动 → 给程序（不动本地选区）。"""

    monkeypatch.setenv("MYRTUI_DRAG_SELECT", "smart")

    async def scenario():
        backend = _FakeBackend(1, mouse={0: MOUSE_1003})
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            await pilot.mouse_down(pane, offset=(1, 1))
            await pilot.mouse_up(pane, offset=(6, 1))
            await pilot.pause(0.1)
            sent = [item for item in backend.sent if item[0] == "mouse"]
            assert sent, "smart 模式下拖动必须转发给程序"
            assert app._gesture is None
            assert copied == []
            # 0.7.0：拖动归程序时不再叠加 myrtui 的 Textual 选区
            assert not app.screen.selections

    asyncio.run(scenario())


def test_smart_shift_drag_selects_locally(copied, monkeypatch, app_root):
    """``smart`` + Shift：绕过程序，回到本地选字 + 自动复制。"""

    monkeypatch.setenv("MYRTUI_DRAG_SELECT", "smart")

    async def scenario():
        backend = _FakeBackend(1, mouse={0: MOUSE_1003})
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            await pilot.mouse_down(pane, offset=(1, 1), shift=True)
            await pilot.mouse_up(pane, offset=(10, 1))
            await pilot.pause(0.2)
            assert copied == ["abcdefghij"], copied
            assert not [item for item in backend.sent if item[0] == "mouse"]

    asyncio.run(scenario())


def test_smart_without_mouse_request_selects_locally(copied, monkeypatch, app_root):
    """程序没要鼠标 → ``smart`` 与 ``always`` 一致（本地选字）。"""

    monkeypatch.setenv("MYRTUI_DRAG_SELECT", "smart")

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            pane = app.query_one("#pane-0", PaneWidget)
            await pilot.mouse_down(pane, offset=(1, 1))
            await pilot.mouse_up(pane, offset=(10, 1))
            await pilot.pause(0.2)
            assert copied == ["abcdefghij"], copied

    asyncio.run(scenario())
