"""myrtui P2 测试：启动参数（§12）、respawn UX（§9/§10.1）、断线重连提示（§9）。

后端测试跑在隔离 tmux 实例上（临时 socket），结束 kill-server；
App 级测试用 Textual headless driver，同样对接隔离实例。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import time
import uuid

import pytest

from myrtui import cli
from myrtui.client import app as app_mod
from myrtui.client.app import TerminalApp
from myrtui.client.topbar import PaneTab, TopBar
from myrtui.tmux_backend import DEFAULT_HISTORY_LIMIT, TmuxBackend, TmuxError


# ---------- 隔离实例 fixture ----------

@pytest.fixture
def backend():
    name = f"myrtui-p2-{uuid.uuid4().hex[:8]}"
    b = TmuxBackend(name, name)
    root = tempfile.mkdtemp(prefix="myrtui-proj-")
    b.create_session(root, 80, 24)
    yield b
    b.kill_server()


def _show(backend: TmuxBackend, option: str) -> str:
    """直读 tmux 全局选项值（绕过封装，验证下发结果）。"""
    proc = subprocess.run(
        ["tmux", "-L", backend.socket, "show", "-g", option],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    # 形如 "history-limit 777\n" → "777"
    return proc.stdout.split(None, 1)[1].strip()


def _exit_pane(backend: TmuxBackend, pane_id: str, timeout: float = 10.0) -> None:
    """把 ``exit`` 送进 pane 直到它真的死亡。

    ``create_session`` 之后 shell 可能还没开始读输入，第一发 ``send_text``
    会被 tmux 丢掉（tests/README「并行下已知的易碎点」）。所以**重发**而不是
    加大 sleep：每 0.5s 一发，直到 ``pane.dead``。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        backend.send_text(pane_id, "exit")
        backend.send_keys(pane_id, "Enter")
        for _ in range(5):
            time.sleep(0.1)
            if any(p.pane_id == pane_id and p.dead for p in backend.list_panes()):
                return
    raise AssertionError(f"pane {pane_id} 在 {timeout:.0f}s 内没有退出（shell 没收到 exit）")


def _wait_dead(backend: TmuxBackend, pane_id: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.2)
        if any(p.pane_id == pane_id and p.dead for p in backend.list_panes()):
            return
    raise AssertionError(f"pane {pane_id} 未进入 dead 状态")


# ---------- §12 P2：启动参数（后端） ----------

def test_create_session_default_scrollback(backend):
    """缺省 history-limit 仍是 §11 的 5000（--scrollback 只是覆盖它）。"""
    assert int(_show(backend, "history-limit")) == DEFAULT_HISTORY_LIMIT == 5000


def test_create_session_shell_and_scrollback(backend):
    """P2：shell / history_limit 必须在 new-session 之前下发（M13 同机制）。"""
    root = tempfile.mkdtemp(prefix="myrtui-proj-")
    name = f"myrtui-p2-{uuid.uuid4().hex[:8]}"
    b = TmuxBackend(name, name)
    try:
        b.create_session(root, 80, 24, shell="/bin/sh", history_limit=777)
        assert _show(b, "history-limit") == "777"
        assert _show(b, "default-shell") == "/bin/sh"
        # pane 0 拿到的就是它（登录 shell → $0 带 '-' 前缀；SHELL 由 tmux 转抄）
        pid = b.list_panes()[0].pane_id
        b.send_text(pid, 'echo "ZERO=$0 SHELL=$SHELL"')
        b.send_keys(pid, "Enter")
        time.sleep(0.8)
        _, caps = b.poll({pid: (0, 24)})
        assert "ZERO=-sh" in caps[pid].text
        assert "SHELL=/bin/sh" in caps[pid].text
    finally:
        b.kill_server()


def test_apply_session_options_existing_session(backend):
    """会话已存在时下发 --shell/--scrollback：只影响之后新建的 pane/window。"""
    assert _show(backend, "history-limit") == "5000"
    backend.apply_session_options(history_limit=1234, shell="/bin/sh")
    assert _show(backend, "history-limit") == "1234"
    assert _show(backend, "default-shell") == "/bin/sh"
    backend.apply_session_options()  # 无参 = no-op，不得报错
    assert _show(backend, "history-limit") == "1234"


# ---------- §12 P2：启动参数（CLI 校验） ----------

def test_build_parser_defaults():
    args = cli.build_parser().parse_args([])
    assert (args.root, args.shell, args.scrollback) == (None, None, None)


def test_build_parser_non_int_scrollback_is_usage_error():
    """取值畸形 = argparse 用法错误（退出码 2），不是 main() 的运行错误。"""
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["--scrollback", "abc"])
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "argv,needle",
    [
        (["--root", "/nonexistent/myrtui-root"], "--root"),
        (["--shell", "/nonexistent/myrtui-shell"], "--shell"),
        (["--scrollback", "0"], "--scrollback"),
        (["--scrollback", "-5"], "--scrollback"),
        (["--scrollback", str(cli.MAX_SCROLLBACK + 1)], "--scrollback"),
    ],
)
def test_main_rejects_bad_options(argv, needle, capsys):
    """环境/取值不满足 → 打印原因返回 1，且在抢锁之前 fail fast。"""
    assert cli.main(argv) == 1
    assert needle in capsys.readouterr().err


def test_main_rejects_non_executable_shell(tmp_path, capsys):
    plain = tmp_path / "notexec.sh"
    plain.write_text("#!/bin/sh\n")
    plain.chmod(0o644)  # 存在但不可执行
    assert cli.main(["--shell", str(plain)]) == 1
    assert "--shell" in capsys.readouterr().err


def test_derive_identity_root_flag_realpath(tmp_path, monkeypatch):
    """M19/P2：--root 与 cwd 走同一条 realpath 通道（符号链接不得分裂身份）。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    link = tmp_path / "link"
    link.symlink_to(proj, target_is_directory=True)

    root_a, digest_a = cli.derive_identity(str(link))
    root_b, digest_b = cli.derive_identity(str(proj))
    assert root_a == root_b == os.path.realpath(proj)
    assert digest_a == digest_b

    monkeypatch.chdir(link)  # cwd 走链接路径 → 同一身份
    root_c, digest_c = cli.derive_identity()
    assert (root_c, digest_c) == (root_a, digest_a)


# ---------- §12 P2：main() 参数编排端到端（伪前端，不进 Textual） ----------

_FAKE_CALLS: list[tuple[str, str | None]] = []


class _FakeApp:
    """只记录 cli.main 传入的 (root, takeover_path)，立即返回。"""

    def __init__(
        self, backend, root, takeover_path=None, stop_path=None,
        layout_path=None, graphics=None,
    ):
        self.backend = backend
        self.root = root
        self.takeover_path = takeover_path
        self.stop_path = stop_path
        self.layout_path = layout_path
        self.graphics = graphics
        _FAKE_CALLS.append((root, takeover_path))

    def run(self):
        return None


def test_main_root_scrollback_shell_end_to_end(tmp_path, monkeypatch, capsys):
    """--root 决定身份/会话/锁；--scrollback/--shell 在建会话时生效；
    会话已存在时下发选项并提示「只影响之后新建的 pane」。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    link = tmp_path / "link"
    link.symlink_to(proj, target_is_directory=True)
    monkeypatch.chdir(tmp_path)  # cwd ≠ root：身份必须来自 --root
    monkeypatch.setattr(app_mod, "TerminalApp", _FakeApp)
    _FAKE_CALLS.clear()

    b = None
    try:
        rc = cli.main(["--root", str(link), "--scrollback", "777",
                       "--shell", "/bin/sh"])
        assert rc == 0
        root, digest = cli.derive_identity(str(link))
        assert root == os.path.realpath(proj)
        assert _FAKE_CALLS[-1][0] == root
        b = TmuxBackend(f"myrtui-{digest}", f"myrtui-{digest}")
        assert b.has_session()
        assert _show(b, "history-limit") == "777"
        # cli 对 --shell 先做 realpath（cli.py 校验段），故比对也要用
        # realpath：Ubuntu 的 /bin/sh 经 usrmerge 解析到 /usr/bin/dash。
        assert _show(b, "default-shell") == os.path.realpath("/bin/sh")
        # cwd 派生的身份从未被触碰（没有为它建锁）
        _, cwd_digest = cli.derive_identity(str(tmp_path))
        assert not os.path.exists(cli.lock_path_for(cwd_digest))

        # 第二次：同 root 的另一符号链接形态 → 同一会话（realpath 归一），
        # 会话已存在 → 只下发选项 + 提示
        rc = cli.main(["--root", str(proj), "--scrollback", "999"])
        assert rc == 0
        assert _show(b, "history-limit") == "999"
        assert "只影响之后新建" in capsys.readouterr().err
    finally:
        if b is not None:
            b.kill_server()


# ---------- §12 P2：respawn（Ctrl-a r） ----------

def test_respawn_target_priority(app_root):
    """目标选择纯逻辑：焦点 dead 优先 > 最近死亡 > 无 dead = None。"""
    app = TerminalApp(TmuxBackend("s", "s"), app_root)
    app._pane_ids = ["%1", "%2", "%3"]
    app._dead = {"%1": True, "%2": True, "%3": False}
    app._dead_order = ["%1", "%2"]  # %2 更近死亡

    app._active_index = 2  # 焦点活着 → 最近死亡的 %2
    assert app._respawn_target() == "%2"
    app._active_index = 0  # 焦点已死 → 优先焦点（%1），即使 %2 更近
    assert app._respawn_target() == "%1"
    app._active_index = 1
    assert app._respawn_target() == "%2"

    app._dead = {"%3": False}  # 没有 dead pane
    app._dead_order = []
    assert app._respawn_target() is None


def test_app_dead_hint_and_ctrl_a_r_respawn(backend, app_root):
    """shell 退出 → 标灰 + 顶栏提示；Ctrl-a r 重开后提示消失（§9/§12 P2）。"""
    async def scenario():
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            pid = app._pane_ids[0]
            _exit_pane(backend, pid)
            _wait_dead(backend, pid)
            # App 的抓屏是轮询的：等它把 dead 状态落到 _dead 上（最多 3s），
            # 不赌单次 pause 的长度（慢机器上 0.6s 不够）。
            for _ in range(30):
                if app._dead.get(pid) is True:
                    break
                await pilot.pause(0.1)
            assert app._dead.get(pid) is True
            topbar = app.query_one(TopBar)
            assert topbar.query_one("#tab-0", PaneTab)._body.strip() == "1:dead"
            assert "Ctrl-a r" in topbar._status  # 单 pane 全灰 → 全部退出提示

            await pilot.press("ctrl+a")
            await pilot.press("r")
            await pilot.pause(0.9)
            assert app._dead.get(pid) is False
            assert not backend.list_panes()[0].dead
            assert topbar._status == "", "重开后 dead 提示应消失"
            assert topbar.query_one("#tab-0", PaneTab)._body.strip() != "1:dead"
    asyncio.run(scenario())


def test_app_respawn_noop_without_dead_pane(backend, app_root):
    """没有已退出 pane 时 Ctrl-a r 静默 no-op（同 Ctrl-a 1..4 越界口径）。"""
    async def scenario():
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            pid = app._pane_ids[0]
            await pilot.press("ctrl+a")
            await pilot.press("r")
            await pilot.pause(0.5)
            assert app._pane_ids == [pid]
            assert not backend.list_panes()[0].dead
            assert backend.has_session()
            assert app.query_one(TopBar)._status == ""
    asyncio.run(scenario())


def test_app_respawn_most_recent_by_default(backend, app_root):
    """焦点 pane 存活时，Ctrl-a r 重开**最近死亡**的那个（非最早死亡的）。"""
    async def scenario():
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            await pilot.press("ctrl+a"); await pilot.press("t")
            await pilot.press("ctrl+a"); await pilot.press("t")
            await pilot.pause(0.8)
            assert len(app._pane_ids) == 3
            ids = list(app._pane_ids)
            backend.send_text(ids[0], "exit"); backend.send_keys(ids[0], "Enter")
            _wait_dead(backend, ids[0])
            await pilot.pause(0.6)  # 让前端先观测到第 1 次死亡
            backend.send_text(ids[1], "exit"); backend.send_keys(ids[1], "Enter")
            _wait_dead(backend, ids[1])
            await pilot.pause(0.6)
            assert app._dead_order == [ids[0], ids[1]]
            assert app._active_index == 2  # 焦点在存活的第 3 个 pane

            await pilot.press("ctrl+a"); await pilot.press("r")
            await pilot.pause(0.9)
            assert app._dead.get(ids[1]) is False, "应重开最近死亡的 pane"
            assert app._dead.get(ids[0]) is True, "更早死亡的 pane 不动"
            status = app.query_one(TopBar)._status
            assert "1 个 pane 已退出" in status, status
    asyncio.run(scenario())


def test_app_respawn_prefers_focused_dead_pane(backend, app_root):
    """焦点 pane 已死时优先重开焦点 pane（即使另一个更近死亡）。"""
    async def scenario():
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            await pilot.press("ctrl+a"); await pilot.press("t")
            await pilot.pause(0.8)
            ids = list(app._pane_ids)
            backend.send_text(ids[0], "exit"); backend.send_keys(ids[0], "Enter")
            _wait_dead(backend, ids[0])
            await pilot.pause(0.6)
            backend.send_text(ids[1], "exit"); backend.send_keys(ids[1], "Enter")
            _wait_dead(backend, ids[1])
            await pilot.pause(0.6)
            assert app._dead_order == [ids[0], ids[1]]
            await pilot.press("ctrl+a"); await pilot.press("1")  # 焦点回到已死的 pane 0
            await pilot.pause(0.3)
            assert app._active_index == 0

            await pilot.press("ctrl+a"); await pilot.press("r")
            await pilot.pause(0.9)
            assert app._dead.get(ids[0]) is False, "应优先重开焦点 pane"
            assert app._dead.get(ids[1]) is True, "更近死亡的另一个不动"
    asyncio.run(scenario())


def test_app_input_blocked_on_dead_pane(backend, app_root):
    """dead pane 不接收输入（tmux 对它静默丢弃，前端直接拦下），
    但全局前缀键照常可用。"""
    async def scenario():
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            pid = app._pane_ids[0]
            backend.send_text(pid, "exit")
            backend.send_keys(pid, "Enter")
            _wait_dead(backend, pid)
            await pilot.pause(0.6)
            assert app._dead.get(pid) is True

            sent: list[tuple] = []
            backend.send_text = lambda p, t: sent.append(("text", p, t))
            backend.send_keys = lambda p, *k: sent.append(("keys", p, k))
            await pilot.press(*"hello")
            await pilot.pause(0.5)
            assert sent == [], f"dead pane 不得接收输入：{sent}"

            # 全局键不走输入通道：Ctrl-a t 仍能新建 pane
            await pilot.press("ctrl+a")
            await pilot.press("t")
            await pilot.pause(0.9)
            assert len(app._pane_ids) == 2
    asyncio.run(scenario())


# ---------- §12 P2：断线 / 重连提示 ----------

def test_app_disconnect_then_reconnect_status(backend, monkeypatch, app_root):
    """连续 5 次失败 → 「失去连接…正在重连」；恢复 → 「已重新连接」→ 清空。"""
    monkeypatch.setattr(app_mod, "RECONNECT_NOTICE", 0.3)

    async def scenario():
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            status = app.query_one(TopBar)._status
            assert "失去连接" not in status

            real_poll = backend.poll

            def boom(*a, **k):
                raise TmuxError("probe: server down")

            backend.poll = boom
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline and not app._disconnected:
                await pilot.pause(0.1)
            assert app._disconnected, "poll 连续失败应进入断线态"
            status = app.query_one(TopBar)._status
            assert "失去连接" in status and "重连" in status

            backend.poll = real_poll
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline and app._disconnected:
                await pilot.pause(0.1)
            assert not app._disconnected, "恢复后应退出断线态"
            assert app.query_one(TopBar)._status == "已重新连接"

            await pilot.pause(0.8)  # > RECONNECT_NOTICE
            assert app.query_one(TopBar)._status == "", "重连提示到期应清空"
    asyncio.run(scenario())


def test_app_real_server_death_and_reconnect(backend, app_root):
    """真·会话级断线：外部 kill-server → 断线提示；同名会话重建 → 已重新连接。"""
    async def scenario():
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            assert not app._disconnected

            backend.kill_server()  # 外部杀掉整个 tmux server（不是 mock）
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not app._disconnected:
                await pilot.pause(0.1)
            assert app._disconnected, "会话消失应进入断线态"
            status = app.query_one(TopBar)._status
            assert "失去连接" in status and "重连" in status

            # 同 socket/同会话名重建（等价于后端重启）→ 恢复
            time.sleep(0.3)
            backend.create_session(tempfile.mkdtemp(prefix="myrtui-proj-"), 80, 24)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and app._disconnected:
                await pilot.pause(0.1)
            assert not app._disconnected, "后端恢复后应退出断线态"
            assert app.query_one(TopBar)._status == "已重新连接"
    asyncio.run(scenario())


def test_app_status_priority_disconnect_over_dead(backend, monkeypatch, app_root):
    """状态优先级：失去连接 > dead 提示（断线时不得把 dead 提示顶掉/覆盖）。"""
    monkeypatch.setattr(app_mod, "RECONNECT_NOTICE", 0.3)

    async def scenario():
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            pid = app._pane_ids[0]
            backend.send_text(pid, "exit")
            backend.send_keys(pid, "Enter")
            _wait_dead(backend, pid)
            await pilot.pause(0.6)
            assert "Ctrl-a r" in app.query_one(TopBar)._status  # dead 提示在

            real_poll = backend.poll

            def boom(*a, **k):
                raise TmuxError("probe: server down")

            backend.poll = boom
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline and not app._disconnected:
                await pilot.pause(0.1)
            status = app.query_one(TopBar)._status
            assert "失去连接" in status
            assert "退出" not in status, "断线提示必须压过 dead 提示"

            backend.poll = real_poll
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline and app._disconnected:
                await pilot.pause(0.1)
            # 恢复后（重连提示窗口内）依旧压过 dead 提示
            assert app.query_one(TopBar)._status == "已重新连接"
    asyncio.run(scenario())
