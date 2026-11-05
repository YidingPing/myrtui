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
from rich.cells import cell_len

from myrtui.client import clipboard
from myrtui.client.app import TerminalApp, OUTER_MUX_HINT_SECONDS, OUTER_MUX_FIX_SECONDS
from myrtui.client.topbar import TopBar
from myrtui.i18n import t
from myrtui.termenv import (
    HINT_ENV,
    DragProbe,
    OuterServer,
    binding_needs_fix,
    fix_argv,
    hint_enabled,
    outer_multiplexer,
    probe_drag_forwarding,
    socket_from_argv,
)
from myrtui.tmux_backend import CaptureResult, PaneInfo


@pytest.fixture(autouse=True)
def _no_real_outer_probe(monkeypatch):
    """本文件的 App 用例默认**不探真实绑定**。

    探测会跟着开发机 / CI 上的 tmux 漂移（本机就跑着好几个 server），跑真
    子进程还会给每个用例加 10–30 ms。要测探测结果的用例自己再 patch 一次。
    """
    from myrtui.client import app as app_module

    monkeypatch.setattr(app_module, "probe_drag_forwarding", lambda env, pid: None)



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


def test_right_click_pastes_even_when_program_has_mouse(monkeypatch, app_root):
    """右键 = 粘贴，**不管**程序有没有请求鼠标（0.8.0 的最终口径）。

    Textual 一直开着鼠标上报，终端按 xterm 约定不再自己处理右键——myrtui 是
    唯一能决定的人。真机核对过 opencode 的右键本来什么都不做，所以抢它零损失。
    """

    monkeypatch.setattr(clipboard, "read_system_clipboard", lambda: "clip")

    async def scenario():
        backend = _FakeBackend(1, mouse=MOUSE_1003)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            await pilot.mouse_down("#pane-0", offset=(3, 2), button=3)
            await pilot.pause(0.4)
            await pilot.mouse_up("#pane-0", offset=(3, 2))
            await pilot.pause(0.2)
            assert backend.pasted == [("p0", "clip")]
            assert not [item for item in backend.sent if item[0] == "mouse"]

    asyncio.run(scenario())


def test_shift_right_click_goes_to_the_program(monkeypatch, app_root):
    """Shift + 右键：把右键还给程序（vim / opencode 自己的行为）。"""

    monkeypatch.setattr(
        clipboard,
        "read_system_clipboard",
        lambda: pytest.fail("Shift+右键不得读剪贴板"),
    )

    async def scenario():
        backend = _FakeBackend(1, mouse=MOUSE_1003)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            await pilot.mouse_down("#pane-0", offset=(3, 2), button=3, shift=True)
            await pilot.pause(0.2)
            await pilot.mouse_up("#pane-0", offset=(3, 2))
            await pilot.pause(0.2)
            assert backend.pasted == []
            assert [item for item in backend.sent if item[0] == "mouse"]

    asyncio.run(scenario())


def test_shift_right_click_without_mouse_does_nothing(monkeypatch, app_root):
    """Shift + 右键在「没请求鼠标」的 pane 上：既不粘也不转发（别制造误粘贴）。"""

    monkeypatch.setattr(
        clipboard,
        "read_system_clipboard",
        lambda: pytest.fail("Shift+右键不得读剪贴板"),
    )

    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            await pilot.mouse_down("#pane-0", offset=(3, 2), button=3, shift=True)
            await pilot.pause(0.3)
            assert backend.pasted == []
            assert not [item for item in backend.sent if item[0] == "mouse"]

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


# ------------------------------------------------ 0.11.1 读真实绑定再提示


#: UU 远程 app bundle 里 `uuremote.conf` 的原样绑定（实测 2026-11-05）：
#: 只判 `pane_in_mode`，完全不看 `mouse_any_flag` → 程序要了鼠标也进 copy-mode。
UU_BUNDLE_BINDING = (
    'bind-key -T root MouseDrag1Pane if-shell -F "#{pane_in_mode}" '
    '"send -X begin-selection" "copy-mode -M"'
)
#: tmux 3.x 内建绑定：程序请求了鼠标就 `send-keys -M`（转发），否则 copy-mode。
TMUX_DEFAULT_BINDING = (
    'bind-key -T root MouseDrag1Pane if-shell -F -t = '
    '"#{||:#{pane_in_mode},#{mouse_any_flag}}" "send-keys -M" "copy-mode -M"'
)

UU_SOCKET = "/Users/u/Library/Application Support/UURemote/tmux.sock"


@pytest.mark.parametrize(
    "binding,expected",
    [
        (UU_BUNDLE_BINDING, True),
        (TMUX_DEFAULT_BINDING, False),
        # 裸转发 / 认不出的形态：一律当作「没事」，绝不基于猜测催用户改配置
        ("bind-key -T root MouseDrag1Pane send-keys -M", False),
        ('bind-key -T root MouseDrag1Pane if-shell -F "#{mouse_any_flag}" '
         '"send -M" "copy-mode -M"', False),
        ("", False),
        (None, False),
    ],
)
def test_binding_needs_fix(binding, expected):
    assert binding_needs_fix(binding) is expected


def test_fix_command_counts_as_healthy():
    """我们自己给的修复命令必须被判成「正常」，否则下次启动还会催。"""

    argv = fix_argv("tmux", UU_SOCKET)
    line = f"bind-key -T root MouseDrag1Pane {' '.join(argv[4:])}"
    assert "mouse_any_flag" in line
    assert binding_needs_fix(line) is False


def test_fix_command_quotes_a_socket_path_with_spaces():
    probe = DragProbe(OuterServer("tmux", UU_SOCKET), UU_BUNDLE_BINDING)
    command = probe.command
    # UU 的 socket 路径带空格：不转义的话用户粘贴出来是两条参数
    assert f"'{UU_SOCKET}'" in command
    assert "bind -Troot MouseDrag1Pane" in command
    assert "mouse_any_flag" in command


def test_socket_from_argv_rejoins_a_path_split_by_ps():
    args = (
        "/Applications/UURemote.app/Contents/Helpers/tmux/uuyc-mux "
        "-S /Users/u/Library/Application Support/UURemote/tmux.sock "
        "-f /Applications/UURemote.app/Contents/Helpers/tmux/uuremote.conf "
        "new-session -A -s uuremote-1"
    )
    assert socket_from_argv(args, exists={UU_SOCKET}.__contains__) == UU_SOCKET


def test_socket_from_argv_understands_relative_socket_names():
    args = "tmux -L myrtui-abc123 new-session -d"
    needle = "/tmp/tmux-501/myrtui-abc123"
    assert socket_from_argv(args, exists={needle}.__contains__, uid=501) == needle
    assert socket_from_argv(args, exists=lambda _p: False, uid=501) is None


def test_socket_from_argv_ignores_unknown_flags():
    assert socket_from_argv("uuyc-mux -f /x.conf", exists=lambda _p: True) is None


def _fake_ps(pairs):
    """{pid: (ppid, args)} → 注入给 ancestor_chain 的读函数。"""
    return lambda pid: pairs.get(pid, (None, None))


def test_probe_reads_the_binding_from_the_ancestor_server():
    """myrtui → zsh → uuyc-mux：从祖先 argv 找到 socket 并回读绑定。"""

    pairs = {
        100: (50, "/bin/zsh -l"),
        50: (1, f"/Applications/UURemote.app/Contents/Helpers/tmux/uuyc-mux "
               f"-S {UU_SOCKET} -f /x/uuremote.conf new-session -A -s uuremote-1"),
    }
    calls = []

    def run(argv):
        calls.append(argv)
        if "display-message" in argv:
            return "50"  # socket 属于 pid 50（祖先）→ 采信
        if "list-keys" in argv:
            return UU_BUNDLE_BINDING
        return None

    probe = probe_drag_forwarding(
        {}, 100, ps=_fake_ps(pairs), run=run,
        exists={UU_SOCKET}.__contains__,
    )
    assert probe is not None
    assert probe.needs_fix is True
    assert probe.server.socket == UU_SOCKET
    # 系统 tmux 能连上就用它（命令更短、用户更眼熟）
    assert probe.server.exe.endswith("tmux")
    assert any("list-keys" in call for call in calls)


def test_probe_falls_back_to_the_outer_binary_when_system_tmux_cannot_talk():
    """外层的 tmux 与系统 tmux 协议不兼容时，退回祖先进程自己的可执行文件。"""

    outer = "/Applications/UURemote.app/Contents/Helpers/tmux/uuyc-mux"
    pairs = {
        100: (50, "/bin/zsh -l"),
        50: (1, f"{outer} -S {UU_SOCKET} -f /x/uuremote.conf new-session -A -s u1"),
    }

    def run(argv):
        # 只有祖先那个二进制能读；系统 tmux 一律失败（模拟协议版本不匹配）
        if argv[0] != outer:
            return None
        if "display-message" in argv:
            return "50"
        if "list-keys" in argv:
            return UU_BUNDLE_BINDING
        return None

    probe = probe_drag_forwarding(
        {}, 100, ps=_fake_ps(pairs), run=run,
        exists={UU_SOCKET}.__contains__,
    )
    assert probe is not None
    assert probe.server.exe == outer
    assert probe.command.startswith("/Applications/UURemote.app")


def test_probe_refuses_a_socket_owned_by_another_server():
    """机器上并存的别的 tmux server 不能被当成外层（pid 对不上就放弃）。"""

    pairs = {
        100: (50, f"/usr/bin/tmux -S {UU_SOCKET} new-session -A -s other"),
    }

    def run(argv):
        if "display-message" in argv:
            return "999"  # 不是祖先 → 拒绝
        return UU_BUNDLE_BINDING

    probe = probe_drag_forwarding(
        {}, 100, ps=_fake_ps(pairs), run=run,
        exists={UU_SOCKET}.__contains__,
    )
    assert probe is None


def test_probe_returns_none_when_nothing_can_be_reached():
    probe = probe_drag_forwarding(
        {}, 100, ps=_fake_ps({100: (1, "/sbin/launchd")}),
        run=lambda _argv: None, exists=lambda _p: False,
    )
    assert probe is None


def _install_probe(monkeypatch, result):
    from myrtui.client import app as app_module

    monkeypatch.setenv("TERM_PROGRAM", "tmux")
    monkeypatch.setattr(app_module, "probe_drag_forwarding", lambda env, pid: result)


def test_bad_binding_puts_the_ready_to_paste_command_on_a_wide_topbar(
    monkeypatch, app_root
):
    """280 列实测状态区有 236 cell，够放整条命令（~190 字符）。"""

    probe = DragProbe(OuterServer("tmux", UU_SOCKET), UU_BUNDLE_BINDING)
    _install_probe(monkeypatch, probe)
    expected = t("app.outer_mux_hint_bad", cmd=probe.command)

    async def scenario():
        app = TerminalApp(_FakeBackend(), app_root)
        async with app.run_test(size=(280, 40)) as pilot:
            await pilot.pause(0.3)
            assert app._notice_text == expected
            assert app.query_one(TopBar)._status == expected
            assert app._notice_until - __import__("time").monotonic() > 10.0
            assert OUTER_MUX_FIX_SECONDS >= 15.0

    asyncio.run(scenario())


def test_thin_topbar_keeps_the_command_with_a_short_prefix(monkeypatch, app_root):
    """整句放不下、但命令还放得下 → 只留「修复：+ 命令」，别把用户推去翻日志。

    真机 280 列 + 4 个长 tab 时状态区实测 205–218 cell（整句 200 仍放得下），
    175–199 这一段才是这一档的用武之地。
    """

    probe = DragProbe(OuterServer("tmux", UU_SOCKET), UU_BUNDLE_BINDING)
    _install_probe(monkeypatch, probe)
    full = t("app.outer_mux_hint_bad", cmd=probe.command)
    with_prefix = t("app.outer_mux_hint_cmd", cmd=probe.command)
    width = cell_len(full) - 1  # 整句差一格 → 必须走第二档
    assert cell_len(with_prefix) <= width
    monkeypatch.setattr(
        TerminalApp, "_topbar_status_width", lambda _self: width
    )

    async def scenario():
        app = TerminalApp(_FakeBackend(), app_root)
        async with app.run_test(size=(280, 40)) as pilot:
            await pilot.pause(0.3)
            assert app._notice_text == t(
                "app.outer_mux_hint_cmd", cmd=probe.command
            )
            assert probe.command in app.query_one(TopBar)._status

    asyncio.run(scenario())


def test_narrow_topbar_falls_back_to_the_log_reference(monkeypatch, app_root):
    """120 列放不下 → 只给短句，绝不显示被截断的命令。"""

    probe = DragProbe(OuterServer("tmux", UU_SOCKET), UU_BUNDLE_BINDING)
    _install_probe(monkeypatch, probe)

    async def scenario():
        app = TerminalApp(_FakeBackend(), app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            assert app._notice_text == t("app.outer_mux_hint_bad_short")
            assert "myrtui.log" in app._notice_text
            # 短句里不该出现半条命令
            assert "MouseDrag1Pane" not in app._notice_text

    asyncio.run(scenario())


def test_healthy_binding_does_not_nag(monkeypatch, app_root):
    """绑定已经把拖动转给程序 → 不再提示（修好了还唠叨就是噪音）。"""

    _install_probe(
        monkeypatch, DragProbe(OuterServer("tmux", UU_SOCKET), TMUX_DEFAULT_BINDING)
    )

    async def scenario():
        app = TerminalApp(_FakeBackend(), app_root)
        async with app.run_test(size=(280, 40)) as pilot:
            await pilot.pause(0.3)
            assert app._notice_text == ""
            assert app.query_one(TopBar)._status != t(
                "app.outer_mux_hint_bad_short"
            )

    asyncio.run(scenario())


def test_hint_disabled_never_probes(monkeypatch, app_root):
    """``MYRTUI_OUTER_MUX_HINT=0``：连子进程都不该起。"""

    from myrtui.client import app as app_module

    calls = []
    monkeypatch.setenv("TERM_PROGRAM", "tmux")
    monkeypatch.setenv(HINT_ENV, "0")
    monkeypatch.setattr(
        app_module, "probe_drag_forwarding",
        lambda env, pid: calls.append((env, pid)),
    )

    async def scenario():
        app = TerminalApp(_FakeBackend(), app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            assert calls == []
            assert t("app.outer_mux_hint") not in app._notice_text

    asyncio.run(scenario())
