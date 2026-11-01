"""0.6.0 传输层测试（0.6.0-INPUT-LATENCY-PLAN §3）。

两部分，互相独立：

- **纯逻辑**（不 spawn tmux）：引号 / 排除表 / 分帧 / 超时 / ``%error``；
- **真 tmux**（隔离 socket）：开关、等价性、顺序、kill-server、重连、
  窗口尺寸、paste 回落。

``TmuxBackend`` 的默认传输仍是 ``exec``（只有 cli 按 ``MYRTUI_TRANSPORT``
解析后传入其它值），所以这些用例显式构造 ``transport="control"``——不改变
既有用例的语义。
"""
from __future__ import annotations

import asyncio
import queue
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass

import pytest

from myrtui.client.app import TerminalApp

from myrtui.tmux_backend import (
    DEFAULT_TRANSPORT,
    TRANSPORT_AUTO,
    TRANSPORT_CONTROL,
    TRANSPORT_EXEC,
    ControlClient,
    ControlFramer,
    ControlResult,
    TmuxBackend,
    TmuxError,
    _Arg,
    _command_names,
    _quote_arg,
    resolve_transport,
)


# ---------------------------------------------------------------- 纯逻辑：引号


def test_quote_arg_quotes_arguments():
    """空格 / 引号 / 反斜杠 / 格式串 / 空串：一律双引号 + 反斜杠转义。"""
    assert _quote_arg("a b") == '"a b"'
    assert _quote_arg("") == '""'
    assert _quote_arg("--") == '"--"'
    assert _quote_arg("#{pane_id}") == '"#{pane_id}"'
    assert _quote_arg('a"b') == '"a\\"b"'
    assert _quote_arg("a\\b") == '"a\\\\b"'
    # 行协议里 tmux 会展开 $NAME（exec 的 argv 不会）→ 必须转义
    assert _quote_arg("$HOME") == '"\\$HOME"'
    assert _quote_arg("a$b") == '"a\\$b"'


def test_quote_arg_separator_vs_literal_semicolon():
    """``;`` 只有作为分隔符时才不加引号；字面分号必须引号化。"""
    assert _quote_arg(";") == ";"
    assert _quote_arg(_Arg(";")) == '";"'


def test_command_names_splits_on_separators_only():
    args = [
        "send-keys", "-l", "-t", "%0", "--", _Arg(";"), ";",
        "display-message", "-p", "x",
    ]
    assert _command_names(args) == ["send-keys", "display-message"]


# ---------------------------------------------------------- 纯逻辑：排除表 / 解析


def test_control_eligible_excludes_protocol_edges():
    back = TmuxBackend("s", "s", transport=TRANSPORT_CONTROL)
    assert back._control_eligible(["capture-pane", "-p", "-t", "%0"], None)
    # stdin（load-buffer / paste）走不了行协议
    assert not back._control_eligible(["load-buffer", "-b", "x", "-"], "text")
    # 带换行的参数是行协议边界
    assert not back._control_eligible(["send-keys", "-l", "a\nb"], None)
    assert not back._control_eligible(["send-keys", "-l", "a\rb"], None)
    # 显式排除的命令（含 ``;`` 序列里的位置）
    assert not back._control_eligible(["kill-server"], None)
    assert not back._control_eligible(["has-session", "-t", "s"], None)
    assert not back._control_eligible(
        ["send-keys", "Enter", ";", "kill-server"], None
    )


def test_resolve_transport_defaults_and_invalid():
    assert resolve_transport(None) == (DEFAULT_TRANSPORT, None)
    assert DEFAULT_TRANSPORT == TRANSPORT_AUTO
    assert resolve_transport("  ") == (DEFAULT_TRANSPORT, None)
    assert resolve_transport(" EXEC ") == (TRANSPORT_EXEC, None)
    assert resolve_transport("control") == (TRANSPORT_CONTROL, None)
    mode, warning = resolve_transport("tcp")
    assert mode == TRANSPORT_AUTO and warning


# ---------------------------------------------------------------- 纯逻辑：分帧


def _feed(framer: ControlFramer, lines: list[str]) -> ControlFramer:
    for line in lines:
        framer.feed(line)
    return framer


def test_framer_keeps_percent_prefixed_content():
    """通知与块外行丢弃；块内以 ``%`` 开头的内容原样保留（计划 §2.1）。"""
    framer = _feed(
        ControlFramer("__S__", "__E__"),
        [
            "%output %0 \\033[0m hello",       # 通知：块外 → 丢
            "%session-changed $0 s",           # 通知：块外 → 丢
            "%begin 100 1 1",                  # START 块
            "__S__",
            "%end 100 1 1",
            "%begin 100 2 1",                  # 内容块
            "%begin fake",
            "%end fake",
            "%error fake",
            "%end 100 2 1",
            "%begin 100 3 1",                  # END 块
            "__E__",
            "%end 100 3 1",
        ],
    )
    assert framer.done
    assert framer.error is None
    assert framer.lines == ["%begin fake", "%end fake", "%error fake"]


def test_framer_error_block():
    framer = _feed(
        ControlFramer("__S__", "__E__"),
        [
            "%begin 100 1 1",
            "__S__",
            "%end 100 1 1",
            "%begin 100 2 1",
            "can't find pane: %99",
            "%error 100 2 1",  # 真错误：原样重复本块三元组，之后没有 %end
        ],
    )
    assert framer.done
    assert framer.error == "can't find pane: %99"


def test_framer_multiblock_output_joined():
    """``;`` 序列的每个子命令各占一块，块内容按顺序拼起来。"""
    framer = _feed(
        ControlFramer("__S__", "__E__"),
        [
            "%begin 1 1 1", "__S__", "%end 1 1 1",
            "%begin 1 2 1", "a", "", "b", "%end 1 2 1",
            "%begin 1 3 1", "", "%end 1 3 1",          # 空块（display-message 空串）
            "%begin 1 4 1", "__E__", "%end 1 4 1",
        ],
    )
    assert framer.done
    assert framer.lines == ["a", "", "b", ""]


# ---------------------------------------------------------- 纯逻辑：客户端失败模式


class _FakeStdin:
    def __init__(self) -> None:
        self.written: list[str] = []

    def write(self, text: str) -> None:
        self.written.append(text)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeProc:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()

    def poll(self):
        return None

    def terminate(self) -> None:
        pass

    def wait(self, timeout=None) -> int:
        return 0

    def kill(self) -> None:
        pass


def _bare_client() -> ControlClient:
    """不 spawn 的客户端：只测 run/_collect 的失败模式。"""
    client = object.__new__(ControlClient)
    client.start = "__S__"
    client.end = "__E__"
    client._queue = queue.Queue()
    client._lock = threading.Lock()
    client._closed = threading.Event()
    client._busy = threading.Event()
    client.proc = _FakeProc()
    return client


def test_client_timeout_is_transport_failure():
    client = _bare_client()
    result = client.run(["display-message", "-p", "x"], timeout=0.05)
    assert not result.ok and not result.command_failed
    assert result.error == "timeout"
    # 超时后客户端会被调用方丢弃；写入的那一行带哨兵与引号
    written = client.proc.stdin.written[0]
    assert "__S__" in written and "__E__" in written
    assert written.endswith("\n")
    assert '"display-message" "-p" "__S__" ; "display-message" "-p" "x" ; ' in written


def test_client_closed_is_transport_failure():
    from myrtui.tmux_backend import _EOF

    client = _bare_client()
    client._queue.put(_EOF)
    result = client.run(["display-message", "-p", "x"], timeout=1.0)
    assert not result.ok and not result.command_failed
    assert result.error == "control client closed"


# ------------------------------------------------------- 纯逻辑：_run 路由与口径


@dataclass
class _RecordingClient:
    result: ControlResult
    calls: list

    def run(self, args, timeout=0.0, *, raw=False):
        self.calls.append((list(args), timeout, raw))
        return self.result

    def close(self) -> None:
        pass


def _routing_backend(monkeypatch):
    back = TmuxBackend("s", "s", transport=TRANSPORT_CONTROL)
    exec_calls: list[list[str]] = []
    reconnects: list[bool] = []

    def fake_exec(args, *, stdin=None, check=True, timeout=10.0):
        exec_calls.append(list(args))
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="exec\n", stderr=""
        )

    monkeypatch.setattr(back, "_run_exec", fake_exec)
    monkeypatch.setattr(back, "_start_reconnect", lambda: reconnects.append(True))
    return back, exec_calls, reconnects


def test_run_routes_command_error_like_exec(monkeypatch):
    """``%error`` → check=True 抛 TmuxError；check=False 返回 returncode=1。"""
    back, exec_calls, _ = _routing_backend(monkeypatch)
    back._control = _RecordingClient(
        ControlResult(ok=False, error="can't find pane: %9", command_failed=True), []
    )
    with pytest.raises(TmuxError):
        back._run(["capture-pane", "-p", "-t", "%9"])
    proc = back._run(["capture-pane", "-p", "-t", "%9"], check=False)
    assert proc.returncode == 1 and "can't find pane" in proc.stderr
    assert exec_calls == []  # 命令错不是传输故障，不降级


def test_run_falls_back_to_exec_on_transport_failure(monkeypatch):
    back, exec_calls, reconnects = _routing_backend(monkeypatch)
    back._control = _RecordingClient(ControlResult(ok=False, error="timeout"), [])
    proc = back._run(["list-panes", "-s", "-t", "s"])
    assert proc.stdout == "exec\n"
    assert exec_calls == [["list-panes", "-s", "-t", "s"]]
    assert back._control is None  # 客户端被丢弃
    assert reconnects == [True]


def test_run_skips_control_for_excluded_commands(monkeypatch):
    back, exec_calls, _ = _routing_backend(monkeypatch)
    client = _RecordingClient(ControlResult(ok=True, stdout=""), [])
    back._control = client
    back._run(["kill-server"], check=False)
    back._run(["load-buffer", "-b", "x", "-"], stdin="data")
    back._run(["send-keys", "-l", "a\nb"])
    assert client.calls == []
    assert len(exec_calls) == 3


def test_run_marks_raw_output_commands(monkeypatch):
    """``show-buffer`` 的换行语义不同：必须告诉客户端按原始内容重建。"""
    back, _, _ = _routing_backend(monkeypatch)
    client = _RecordingClient(ControlResult(ok=True, stdout="hello"), [])
    back._control = client
    back._run(["show-buffer", "-b", "t"], check=False)
    back._run(["list-buffers", "-F", "#{buffer_name}"], check=False)
    assert [raw for _, _, raw in client.calls] == [True, False]


# ---------------------------------------------------------------- 真 tmux 夹具


def _wait_until(predicate, timeout: float = 10.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _type_until(backend, pane_id: str, command: str, marker: str, timeout: float = 15.0):
    """重发命令行直到 capture 里出现 ``marker``。

    ``create_session`` 之后 shell 可能还没开始读输入、第一发被丢掉
    （tests/README 的已知易碎点）；重发比加大 sleep 稳。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        backend.send_text(pane_id, command)
        backend.send_keys(pane_id, "Enter")
        if _wait_until(
            lambda: marker in backend.poll({pane_id: (0, 30)})[1][pane_id].text,
            timeout=1.5,
        ):
            return
    raise AssertionError(f"pane 没有出现 {marker!r}")


@pytest.fixture
def control_backend(tmp_path):
    name = f"myrtui-ctl-{uuid.uuid4().hex[:8]}"
    back = TmuxBackend(name, name, transport=TRANSPORT_CONTROL)
    back.create_session(str(tmp_path), 100, 30)
    try:
        assert back.open_control() is True
        yield back
    finally:
        back.kill_server()


# ---------------------------------------------------------------- 集成：开关


def test_open_and_close_leaves_no_process(control_backend):
    name = f"myrtui-ctl-{uuid.uuid4().hex[:8]}"
    back = TmuxBackend(name, name, transport=TRANSPORT_CONTROL)
    back.create_session("/tmp", 80, 24)
    try:
        assert back.open_control() is True
        client = back._control
        assert client is not None
        panes = back.list_panes()
        assert panes and panes[0].pane_id.startswith("%")
        back.close_control()
        assert back.control_active is False
        assert _wait_until(lambda: client.proc.poll() is not None, timeout=5.0)
    finally:
        back.kill_server()


def test_exec_transport_never_opens_control(tmp_path):
    name = f"myrtui-ctl-{uuid.uuid4().hex[:8]}"
    back = TmuxBackend(name, name, transport=TRANSPORT_EXEC)
    back.create_session(str(tmp_path), 80, 24)
    try:
        assert back.open_control() is False
        assert back.control_active is False
    finally:
        back.kill_server()


def test_auto_falls_back_when_attach_impossible():
    """``auto``：会话不存在 → attach 失败只告警，保持 exec（不抛）。"""
    name = f"myrtui-ctl-{uuid.uuid4().hex[:8]}"
    back = TmuxBackend(name, name, transport=TRANSPORT_AUTO)
    try:
        assert back.open_control() is False
        assert back.control_active is False
    finally:
        back.kill_server()


def test_control_mode_raises_when_attach_impossible():
    name = f"myrtui-ctl-{uuid.uuid4().hex[:8]}"
    back = TmuxBackend(name, name, transport=TRANSPORT_CONTROL)
    try:
        with pytest.raises(TmuxError):
            back.open_control()
        assert back.control_active is False
    finally:
        back.kill_server()


# ---------------------------------------------------------------- 集成：等价性


def test_empty_output_matches_exec(control_backend):
    """无输出（""）与「一个空行」的输出必须和 exec 逐字节一致。"""
    exec_backend = TmuxBackend(control_backend.socket, control_backend.session)
    ctl = control_backend._run(["display-message", "-p", ""]).stdout
    exe = exec_backend._run(["display-message", "-p", ""]).stdout
    assert ctl == exe == "\n"
    pid = control_backend.list_panes()[0].pane_id
    assert control_backend._run(["send-keys", "-t", pid, "Enter"]).stdout == ""


def test_poll_matches_exec_after_resize(control_backend, tmp_path):
    pid = control_backend.list_panes()[0].pane_id
    control_backend.send_text(pid, "clear; printf 'one\\ntwo\\nthree\\n'")
    control_backend.send_keys(pid, "Enter")
    assert _wait_until(
        lambda: "three" in control_backend.poll({pid: (0, 30)})[1][pid].text
    )
    control_backend.new_window(str(tmp_path))
    panes = control_backend.list_panes()
    control_backend.resize_window(panes[0].window_id, 120, 35)

    offsets = {p.pane_id: (0, p.height) for p in control_backend.list_panes()}
    panes_c, caps_c = control_backend.poll(offsets)

    exec_backend = TmuxBackend(control_backend.socket, control_backend.session)
    panes_e, caps_e = exec_backend.poll(offsets)

    assert [p.pane_id for p in panes_c] == [p.pane_id for p in panes_e]
    assert [(p.width, p.height) for p in panes_c] == [
        (p.width, p.height) for p in panes_e
    ]
    for pane_id in offsets:
        assert caps_c[pane_id].text == caps_e[pane_id].text
        assert caps_c[pane_id].offset == caps_e[pane_id].offset


def test_poll_scrollback_matches_exec(control_backend):
    """翻页（k > 0）的行数/内容/offset 与 exec 完全一致（M1/M12/M18）。"""
    pid = control_backend.list_panes()[0].pane_id
    command = "clear; for i in $(seq 1 60); do printf 'line-%02d\\n' $i; done"
    _type_until(control_backend, pid, command, "line-60")
    pane = control_backend.list_panes()[0]
    exec_backend = TmuxBackend(control_backend.socket, control_backend.session)
    for raw_k in (0, 5, 20):
        k = min(raw_k, pane.history_size - 1)
        ctl = control_backend.poll({pid: (k, pane.height)})[1][pid]
        exe = exec_backend.poll({pid: (k, pane.height)})[1][pid]
        assert ctl.offset == exe.offset == k
        assert ctl.text == exe.text


# ---------------------------------------------------------------- 集成：顺序与引号


def test_send_then_poll_sees_the_text(control_backend):
    """B1 的 FIFO 语义：先 send 后 poll，capture 必须包含刚发的字符。"""
    pid = control_backend.list_panes()[0].pane_id
    _type_until(control_backend, pid, "echo MYRTUI_FIFO_OK", "MYRTUI_FIFO_OK")


def test_literal_semicolon_survives_control_quoting(control_backend):
    """单个 ``;`` 是键入内容，不能被当成命令分隔符（引号往返）。"""
    pid = control_backend.list_panes()[0].pane_id
    control_backend.send_text(pid, ";")
    assert _wait_until(
        lambda: ";" in control_backend.poll({pid: (0, 30)})[1][pid].text
    )


def test_nasty_arguments_roundtrip(control_backend):
    """含空格 / 引号 / 反斜杠 / ``#{}`` 的参数经引号往返后原样进入 pane。"""
    pid = control_backend.list_panes()[0].pane_id
    payload = 'A B;C"D\\E#{x}--'
    _type_until(control_backend, pid, 'echo "' + payload + '"', payload)


def test_dollar_is_not_expanded_by_tmux(control_backend):
    """``$NAME`` 必须原样进 pane：行协议下 tmux 会展开，exec 的 argv 不会。"""
    pid = control_backend.list_panes()[0].pane_id
    _type_until(control_backend, pid, "echo 'VAL=$HOME'", "VAL=$HOME")


# ---------------------------------------------------------------- 集成：失败与重连


def test_kill_server_is_not_silent(control_backend):
    """kill-server 走 exec 且先关客户端；之后命令抛 TmuxError（不静默）。"""
    control_backend.kill_server()
    assert control_backend.control_active is False
    with pytest.raises(TmuxError):
        control_backend.list_panes()


def test_reconnect_after_external_restart(control_backend, tmp_path):
    """外部 kill 服务器后同名重建：本条命令降级 exec，退避后 control 恢复。"""
    # 外部杀掉服务器（不经过被测 backend，模拟 tmux server 挂掉）
    subprocess.run(
        ["tmux", "-L", control_backend.socket, "kill-server"],
        capture_output=True,
        timeout=10,
    )
    assert _wait_until(
        lambda: control_backend._control is None
        or control_backend._control.proc.poll() is not None
    )

    fresh = TmuxBackend(control_backend.socket, control_backend.session)
    fresh.create_session(str(tmp_path), 90, 28)

    # 第一次命令：控制通道已死 → 降级 exec 并触发重连
    panes = control_backend.list_panes()
    assert panes and panes[0].window_id
    assert _wait_until(lambda: control_backend.control_active, timeout=8.0)
    assert control_backend.reconnects == 1


# ---------------------------------------------------------------- 集成：尺寸与 paste


def test_attach_does_not_resize_window(tmp_path):
    """常驻 client 不得改写窗口尺寸（计划 §2.3 / 风险表）。"""
    name = f"myrtui-ctl-{uuid.uuid4().hex[:8]}"
    back = TmuxBackend(name, name, transport=TRANSPORT_CONTROL)
    back.create_session(str(tmp_path), 100, 33)
    try:
        before = [(p.width, p.height) for p in back.list_panes()]
        assert back.open_control() is True
        after = [(p.width, p.height) for p in back.list_panes()]
        assert after == before
    finally:
        back.kill_server()


def test_show_buffer_matches_exec(control_backend):
    """``show-buffer`` 不是逐行打印：无尾换行的内容不得被补一个 \n。"""
    exec_backend = TmuxBackend(control_backend.socket, control_backend.session)
    for content in ("hello", "a\nb", "x\ny\n"):
        control_backend._run(["set-buffer", "-b", "t", content])
        ctl = control_backend.show_buffer()
        exe = exec_backend.show_buffer()
        assert ctl == exe, f"content={content!r}"


def test_paste_uses_exec_while_control_is_active(control_backend):
    pid = control_backend.list_panes()[0].pane_id
    control_backend.paste(pid, "MYRTUI_PASTE_OK")
    assert _wait_until(
        lambda: "MYRTUI_PASTE_OK"
        in control_backend.poll({pid: (0, 30)})[1][pid].text
    )


# ---------------------------------------------------------------- 真机反馈（dev1）


def test_non_utf8_output_keeps_the_channel_alive(control_backend):
    """pane 输出非 UTF-8 字节不能让读线程死掉（0.6.0.dev1 真机 bug）。

    真机现象：图像/二进制输出时控制通道反复失联，每条命令降级 exec 并重连。
    根因是读线程严格解码抛 ``UnicodeDecodeError``。修法：utf-8 + replace，
    非法字节显示成 ``\\ufffd``，通道不断。
    """
    pid = control_backend.list_panes()[0].pane_id
    _type_until(control_backend, pid, "echo READY", "READY")
    for round_ in range(20):
        # 命令本身带 marker：marker 出现说明这条命令确实执行了，那么它输出的
        # 非 UTF-8 字节也一定经过了控制流（旧实现在这里会立刻掉线）。
        control_backend.send_text(pid, f"printf '\\377\\376'; echo MARK{round_}")
        control_backend.send_keys(pid, "Enter")
        assert _wait_until(
            lambda: f"MARK{round_}"
            in control_backend.poll({pid: (0, 30)})[1][pid].text
        )
        panes, caps = control_backend.poll({pid: (0, 30)})
        assert len(caps[pid].text.splitlines()) == panes[0].height
    assert control_backend.control_active
    assert control_backend.reconnects == 0
    # 两条路径的解码一致性由静止画面上的等价用例覆盖
    # （test_poll_matches_exec_after_resize / test_show_buffer_matches_exec）：
    # 这里 pane 还在滚动，连续两次 capture 本来就可能不同，不能逐字节比。


def test_idle_output_is_not_queued(control_backend):
    """闲置时的 %output 不进队列：否则图像/洪流会把命令埋进积压里等超时。"""
    pid = control_backend.list_panes()[0].pane_id
    _type_until(control_backend, pid, "echo READY", "READY")
    control_backend.send_text(pid, "yes myrtui-flood")
    control_backend.send_keys(pid, "Enter")
    time.sleep(0.6)
    client = control_backend._control
    assert client is not None
    # 先排掉「上一条命令收尾时在途」的行，再观察：闲置期不应有新行进队列
    while not client._queue.empty():
        client._queue.get_nowait()
    time.sleep(0.4)
    assert client._queue.qsize() == 0, "闲置期不应堆积 %output"
    control_backend.send_keys(pid, "C-c")
    time.sleep(0.2)
    _type_until(control_backend, pid, "echo AFTER_FLOOD", "AFTER_FLOOD")


def test_tui_logging_goes_to_file(tmp_path, monkeypatch, capsys):
    """TUI 接管终端后 warning 落盘，不许写到 stderr（会画在光标处）。"""
    import logging

    from myrtui import cli

    monkeypatch.delenv(cli.LOG_ENV, raising=False)
    monkeypatch.setattr(
        cli.platformdirs, "user_log_dir", lambda *a, **k: str(tmp_path)
    )
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level

    def flush() -> None:
        for handler in root.handlers:
            handler.flush()

    try:
        path = cli.redirect_logging_to_file()
        assert path == str(tmp_path / "myrtui.log")
        root.setLevel(logging.WARNING)
        logging.getLogger("myrtui.tmux").warning("boom-transport-lost")
        flush()
        assert "boom-transport-lost" in open(path, encoding="utf-8").read()
        assert "boom-transport-lost" not in capsys.readouterr().err

        # 显式路径优先（测试 / CI 用它把日志赶出 HOME）
        custom = tmp_path / "nested" / "custom.log"
        monkeypatch.setenv(cli.LOG_ENV, str(custom))
        assert cli.redirect_logging_to_file() == str(custom)
        logging.getLogger("myrtui.tmux").warning("boom-custom")
        flush()
        assert "boom-custom" in custom.read_text(encoding="utf-8")

        # 空串 = 关闭落盘，且绝不回落到 stderr
        monkeypatch.setenv(cli.LOG_ENV, "")
        assert cli.redirect_logging_to_file() is None
        logging.getLogger("myrtui.tmux").warning("boom-off")
        flush()
        assert "boom-off" not in capsys.readouterr().err
    finally:
        root.setLevel(level)
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
                handler.close()
        for handler in before:
            root.addHandler(handler)


# ---------------------------------------------------------------- 集成：整条前端


def test_app_keys_flow_through_control(control_backend, app_root):
    """真 app + control：拓扑、按键转发、poll 刷新都不崩（计划 §3.3 回归面）。"""

    async def scenario():
        app = TerminalApp(control_backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            assert app._pane_ids, "control 下首轮 poll 必须拿到 pane"
            pid = app._pane_ids[0]
            # 用不容易出现在提示符里的字符，避免误判
            await pilot.press("Q", "Q")
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                await pilot.pause(0.05)
                text = control_backend.poll({pid: (0, 30)})[1][pid].text
                if "QQ" in text:
                    return
            raise AssertionError("按键没有到达 pane")

    asyncio.run(scenario())
