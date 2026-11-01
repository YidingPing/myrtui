"""tmux 后端封装（DESIGN.md §4.2 接口表）。

约定：
- 所有命令都带 ``-L <socket-name>``，走隔离实例；
- 两种传输（0.6.0：``MYRTUI_TRANSPORT``）：``exec`` 每次调用 fork 一个
  短命 client（老路径），``control`` 常驻一个 ``tmux -C attach`` 连接；
  ``auto`` 能用 control 就用，失败回落 exec——命令构造与解析两条路径共用；
- 前端从不 attach **交互式** tmux client：control 客户端只喂单行命令，
  不读终端、不参与键位，窗口尺寸靠 ``resize-window``（实测 attach 不改尺寸）；
- 多 pane 时用 ``;`` 串联，单次进程调用抓完全部（N14）；
- capture 永不追加 ``-a``（M2），永不启用 ``-J``（M11）；
- 翻页公式 ``-S -K -E $((H-1-K))``（M1），K 先 clamp 到 ``history_size-1``（M12），
  每次断言返回行数 == H（M3/M18），失败收紧 K 重试一次，仍失败退回实时视图。
"""
from __future__ import annotations

import logging
import os
import queue
import re
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from myrtui.i18n import t

log = logging.getLogger("myrtui.tmux")

# 版本下限：capture-pane -N 为 3.1 新增（M4）
MIN_TMUX_VERSION = (3, 1)
_VERSION_RE = re.compile(r"(\d+)\.(\d+)")

# list-panes 输出字段：设计文档 §4.2 的 8 项 + height/width/window_id/window_index
# （翻页公式需要 H，resize-window 需要 window 目标；多 pane 槽位排序需要 index）
# + mouse_*_flag（v0.7 鼠标转发门控，记录 pane 内程序请求的追踪模式）
# + mouse_sgr_flag（v0.7.1 审阅 F3：编码位，1006 → SGR / 否则 xterm 旧 3 字节）
# + mouse_all_flag（v0.7.1 小项：直读 1003，替代 any 排除法）
_PANE_FORMAT = (
    "#{pane_id} #{pane_dead} #{pane_current_command} #{alternate_on} "
    "#{cursor_x} #{cursor_y} #{cursor_flag} #{history_size} "
    "#{pane_height} #{pane_width} #{window_id} #{window_index} "
    "#{mouse_standard_flag} #{mouse_button_flag} #{mouse_any_flag} "
    "#{mouse_sgr_flag} #{mouse_all_flag}"
)

PASTE_BUFFER = "myrtui-paste"

# §11 默认历史深度；P2 起可由启动参数 --scrollback 覆盖
DEFAULT_HISTORY_LIMIT = 5000


class TmuxError(RuntimeError):
    """tmux 调用失败。"""


class TmuxNotFoundError(TmuxError):
    """tmux 未安装。"""


@dataclass(frozen=True)
class PaneInfo:
    pane_id: str
    dead: bool
    command: str
    alternate_on: bool
    cursor_x: int
    cursor_y: int
    cursor_flag: bool
    history_size: int
    height: int
    width: int
    window_id: str
    window_index: int  # 窗口序号：多 pane 槽位排序依据（P1）
    mouse_standard: bool  # pane 内程序请求了 1000（点击）——v0.7 鼠标转发门控
    mouse_button: bool  # 请求了 1002（+拖动）
    mouse_any: bool  # 请求了 1003（+任意移动）；亦为「任一模式启用」聚合位
    mouse_sgr: bool  # 请求了 1006（SGR 编码）——v0.7.1 审阅 F3：=0 时须发
    # xterm 旧 3 字节编码，恒发 SGR 会让默认 vim 点击落点错行
    mouse_all: bool  # 直读 1003（v0.7.1 小项：替代 any 排除法判 ALL）


@dataclass(frozen=True)
class CaptureResult:
    text: str
    offset: int  # 实际使用的翻页偏移（断言失败收紧后可能变小，0 = 实时视图）


def parse_tmux_version(output: str) -> tuple[int, int] | None:
    """解析 ``tmux -V`` 输出（M16 容错：next-3.9 / 3.4a 均可，失配返回 None 放行）。"""
    m = _VERSION_RE.search(output)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def check_tmux(tmux_bin: str = "tmux") -> tuple[int, int] | None:
    """启动期版本校验（M4/M16）。解析失败告警放行，版本过低抛错。"""
    try:
        proc = subprocess.run(
            [tmux_bin, "-V"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        raise TmuxNotFoundError(t("tmux.not_installed")) from None
    version = parse_tmux_version(proc.stdout)
    if version is None:
        log.warning("无法解析 tmux 版本（%r），放行", proc.stdout.strip())
        return None
    if version < MIN_TMUX_VERSION:
        raise TmuxError(t("tmux.too_old", version=proc.stdout.strip()))
    return version


# ── 传输选择与常驻客户端（0.6.0-INPUT-LATENCY-PLAN §2） ──────────────
#
# 每个按键/每次抓屏都要付一次 ``tmux`` client 启动（实测 4.5–5 ms，真 app
# 里被 GIL 竞争放大到 8–16 ms）；控制通道的往返是 0.06–0.3 ms。所以：
# control 只替换 ``_run`` 的传输层，命令、解析、``check=False`` 语义两条路径
# 完全一致；``exec`` 永远保留为逃生口与对照。

TRANSPORT_ENV = "MYRTUI_TRANSPORT"
TRANSPORT_AUTO = "auto"
TRANSPORT_EXEC = "exec"
TRANSPORT_CONTROL = "control"
TRANSPORTS = (TRANSPORT_AUTO, TRANSPORT_EXEC, TRANSPORT_CONTROL)

# 0.6.0 起默认 auto（计划 §2.4）：能用 control 就用，建不起来回落 exec。
DEFAULT_TRANSPORT = TRANSPORT_AUTO

CONTROL_TIMEOUT = 10.0  # 单条命令超时；超时按「客户端失联」处理（计划 §2.1）
CONTROL_READY_TIMEOUT = 3.0  # 建连接 + 握手
RECONNECT_BACKOFF = (1.0, 2.0, 5.0)  # 运行时失联后的重连退避（计划 §2.3）

# 控制模式的块定界行：``%begin <ts> <num> <flags>`` / ``%end ...``。
# **内容行也可能以 ``%`` 开头**（实测 ``display-message -p '%end fake'``
# 原样输出 ``%end fake``），所以只认「原样重复本块 %begin 三元组」的那一行：
# 内容要撞车必须先知道当前 epoch + 计数器，实际不可能。
_BEGIN_RE = re.compile(r"^%begin (\d+) (\d+) (\d+)$")

# 控制模式不能承载的命令（计划 §2.2）：
# - kill-server：服务器与客户端一起退出，等不到 %end；
# - start-server / new-session / has-session：发生在客户端建立之前，
#   或会重建服务器；
# - load-buffer：协议是行式的，带 stdin 的请求由 ``stdin`` 判据再拦一次。
_CONTROL_DENY = frozenset(
    {"kill-server", "has-session", "start-server", "new-session", "load-buffer"}
)

# 输出是「原始内容」而非「逐行打印」的命令（换行处理不同，见 _collect）。
# 逐行打印的命令（capture-pane / list-panes / display-message …）每行一次
# print，块行数 = 打印行数；show-buffer 是整段内容一次 print，内容里的 \n
# 直接变成块行，**包括末尾那个空行**。两条路径要逐字节一致，必须分开处理。
_RAW_OUTPUT = frozenset({"show-buffer"})


class _Arg(str):
    """「这是实参，不是命令分隔符」的标记（只对值恰为 ``;`` 的实参有意义）。

    tmux 的命令分隔符是 ``;``（不加引号），而 pane 里打一个 ``;`` 太常见：
    控制模式下必须序列化成引号化的 ``";"``（实测 tmux 按字面处理、不拆分）。
    """


def _is_separator(arg: str) -> bool:
    return arg == ";" and not isinstance(arg, _Arg)


def _quote_arg(arg: str) -> str:
    """单行协议的参数引号：双引号 + 反斜杠转义（计划 §2.1）。

    ``;`` 是命令分隔符，**不加引号**；值就是 ``;`` 的实参用 :class:`_Arg`
    标记后按普通参数引号化。

    ``$`` 必须转义：控制模式是**行协议**，tmux 解析命令行时会把
    ``"$HOME"`` 展开成环境变量（实测 ``display-message -p "$HOME"`` →
    ``/Users/…``），而 exec 模式的 argv 不做这种展开。不转义的话，往 pane
    里打一句 ``echo $HOME`` 会被提前展开成别的命令（真机必现）。
    """
    if _is_separator(arg):
        return ";"
    if arg == "":
        return '""'
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return '"' + escaped + '"'


def _command_names(args: list[str]) -> list[str]:
    """按 ``;`` 拆出序列里每条命令的名字（走不走 control 的判据）。"""
    names: list[str] = []
    expect = True
    for arg in args:
        if _is_separator(arg):
            expect = True
            continue
        if expect:
            names.append(arg)
            expect = False
    return names


def resolve_transport(raw: str | None) -> tuple[str, str | None]:
    """解析 ``MYRTUI_TRANSPORT`` → ``(mode, warning)``。

    未设 / 空串 = ``auto``；非法值告警后回落 ``auto``（与
    ``MYRTUI_IDLE_INTERVAL`` / ``MYRTUI_MATH_BACKEND`` 同口径）。
    """
    if raw is None or not raw.strip():
        return DEFAULT_TRANSPORT, None
    mode = raw.strip().lower()
    if mode in TRANSPORTS:
        return mode, None
    warning = (
        f"{TRANSPORT_ENV}={raw!r} is not one of {'/'.join(TRANSPORTS)}; "
        f"falling back to {DEFAULT_TRANSPORT}"
    )
    log.warning(warning)
    return DEFAULT_TRANSPORT, warning


class ControlFramer:
    """把控制模式的行流切成「两条哨兵之间的命令输出」（计划 §2.1）。

    tmux 保证通知（``%output`` 等）不会落进输出块内，但**块内的内容行**
    可以以 ``%`` 开头。所以：

    - 块外：只认 ``%begin <ts> <num> <flags>``，其余行（通知、本次之前的
      残留）直接丢弃；
    - 块内：只把「原样重复本块三元组」的 ``%end`` / ``%error`` 当定界行，
      其余一律是内容——内容要撞车得先知道 epoch 与计数器，实际不会发生；
    - 序列自己插入的 ``START`` / ``END`` 哨兵各占一个块，只收集两条哨兵
      之间的输出，避免把 ``%`` 前缀内容误判成通知。
    """

    def __init__(self, start: str, end: str) -> None:
        self.start = start
        self.end = end
        self.lines: list[str] = []
        self.error: str | None = None
        self.done = False
        self._triple: str | None = None
        self._block: list[str] = []
        self._started = False

    def feed(self, line: str) -> bool:
        """喂一行，返回是否已收齐（``done``）。"""
        if self.done:
            return True
        if self._triple is None:
            m = _BEGIN_RE.match(line)
            if m is not None:
                self._triple = f"{m.group(1)} {m.group(2)} {m.group(3)}"
                self._block = []
            return False
        if line == f"%end {self._triple}":
            self._triple = None
            block, self._block = self._block, []
            self._consume(block)
        elif line == f"%error {self._triple}":
            self._triple = None
            self.error = "\n".join(self._block) or "tmux error"
            self._block = []
            self.done = True
        else:
            self._block.append(line)
        return self.done

    def _consume(self, block: list[str]) -> None:
        if not self._started:
            if block == [self.start]:
                self._started = True
            return
        if block == [self.end]:
            self.done = True
            return
        self.lines.extend(block)


@dataclass(frozen=True)
class ControlResult:
    """一次 control 往返的结果。

    ``ok``：命令执行完毕（``stdout`` 是块内容，可能为空串）；
    ``command_failed``：tmux 回了 ``%error``——命令本身的错，传输还活着，
    按 exec 的失败口径返回（returncode=1 + stderr）；
    两者都不成立：传输故障（超时 / 管道关闭），调用方关客户端并降级 exec。
    """

    ok: bool
    stdout: str = ""
    error: str = ""
    command_failed: bool = False


_EOF = object()  # 读线程结束（客户端退出 / 管道关闭）


class ControlClient:
    """常驻 ``tmux -C attach`` 连接（计划 §2.1）。

    - 命令写成一行（``;`` 分隔）送进 stdin；
    - 独立线程逐行读 stdout，块外行直接丢弃，不逐行解析，**闲置时连队列都不进**；
    - 每条命令各产出一个 ``%begin/%end`` 块（实测，不是整串一个块），
      靠 :class:`ControlFramer` 收齐到 END 哨兵才返回；
    - 命令之间用锁串行化：调用方（单 worker + 少量事件循环侧的显式调用）
      本来就要求「提交顺序 = 执行顺序」。
    """

    def __init__(
        self,
        tmux_bin: str,
        socket_name: str,
        session_name: str,
        *,
        timeout: float = CONTROL_READY_TIMEOUT,
    ) -> None:
        token = uuid.uuid4().hex
        self.start = f"__MYRTUI_CTL_{token}_S__"
        self.end = f"__MYRTUI_CTL_{token}_E__"
        self._queue: queue.Queue[str | object] = queue.Queue()
        self._lock = threading.Lock()
        self._closed = threading.Event()
        # 「有命令在飞」：读线程只在置位时入队，其余时间的 %output 直接丢。
        # 否则画面一直在动（图像、tail -f）时队列会无限涨，下一条命令得先把
        # 积压排空才能看到自己的块——这是超时与掉线的放大器。
        self._busy = threading.Event()
        self.proc = subprocess.Popen(
            [tmux_bin, "-L", socket_name, "-C", "attach", "-t", session_name],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,  # attach 失败的原因走握手错误，不写 stderr
            text=True,
            # **不能严格解码**：pane 里的二进制 / 图像 / Latin-1 输出会原样
            # 经过 tmux（它只转义控制字符），一个非法字节就让读线程抛
            # UnicodeDecodeError 死掉 → 之后每条命令都报 closed、无限重连
            # （0.6.0.dev1 真机实录：画面一动就满屏闪 warning）。
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._reader = threading.Thread(
            target=self._read_loop, name="myrtui-tmux-control", daemon=True
        )
        self._reader.start()
        # 握手：attach 失败（无会话 / 非 tty / tmux 太老）时进程直接退出，
        # 靠一条命令确认连接可用；失败即「这个环境用不了 control」。
        result = self.run(
            ["display-message", "-p", "myrtui-control-ready"], timeout=timeout
        )
        if not result.ok:
            self.close()
            raise TmuxError(t("tmux.control_unavailable", reason=result.error))

    # ---------- 读线程 ----------

    def _read_loop(self) -> None:
        try:
            assert self.proc.stdout is not None
            for line in self.proc.stdout:
                if not self._busy.is_set():
                    continue  # 闲置：%output / 通知直接丢，不占队列
                self._queue.put(line.rstrip("\r\n"))
        except Exception:  # 管道异常：按失联处理
            pass
        finally:
            self._queue.put(_EOF)

    def _next(self, timeout: float) -> str | object | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    # ---------- 命令 ----------

    def run(
        self,
        args: list[str],
        timeout: float = CONTROL_TIMEOUT,
        *,
        raw: bool = False,
    ) -> ControlResult:
        with self._lock:
            if self._closed.is_set() or self.proc.poll() is not None:
                return ControlResult(ok=False, error="control client closed")
            sequence = [
                "display-message", "-p", self.start, ";",
                *args,
                ";", "display-message", "-p", self.end,
            ]
            line = " ".join(_quote_arg(arg) for arg in sequence)
            self._busy.set()
            try:
                try:
                    assert self.proc.stdin is not None
                    self.proc.stdin.write(line + "\n")
                    self.proc.stdin.flush()
                except (OSError, ValueError):
                    return ControlResult(ok=False, error="control client closed")
                return self._collect(timeout, raw=raw)
            finally:
                self._busy.clear()

    def _collect(self, timeout: float, *, raw: bool = False) -> ControlResult:
        framer = ControlFramer(self.start, self.end)
        deadline = time.monotonic() + timeout
        while not framer.done:
            left = deadline - time.monotonic()
            if left <= 0:
                return ControlResult(ok=False, error="timeout")
            line = self._next(left)
            if line is _EOF:
                return ControlResult(ok=False, error="control client closed")
            if line is None:
                return ControlResult(ok=False, error="timeout")
            framer.feed(line)
        if framer.error is not None:
            return ControlResult(ok=False, error=framer.error, command_failed=True)
        text = "\n".join(framer.lines)
        if raw:
            # 原始内容：块行已经是内容的精确切分（含末尾空行 → 末尾换行）
            return ControlResult(ok=True, stdout=text)
        # 逐行打印：exec 路径的每一行都以 \n 结尾。空块（命令无输出）→ ""；
        # 只含一个空行的块 → "\n"，两者不能混（M18 口径）。
        return ControlResult(ok=True, stdout=text + "\n" if framer.lines else "")

    def close(self) -> None:
        """幂等关闭：先置位（让在途 run 尽快返回），再杀进程。

        **不拿命令锁**：run() 可能正卡在 10 s 的读等待里，退出路径不能被它拖住。
        """
        self._closed.set()
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.proc.kill()
            except OSError:
                pass


class TmuxBackend:
    def __init__(
        self,
        socket_name: str,
        session_name: str,
        tmux_bin: str = "tmux",
        *,
        transport: str = TRANSPORT_EXEC,
    ):
        self.socket = socket_name
        self.session = session_name
        self.tmux = tmux_bin
        # 传输模式只由调用方（cli）按 MYRTUI_TRANSPORT 解析后传入；默认 exec
        # 让「不关心传输」的路径（instances / 单测）永远走老路，零行为变化。
        self.transport = transport
        self._control: ControlClient | None = None
        self._control_lock = threading.Lock()
        self._control_closed = threading.Event()
        self._reconnect_thread: threading.Thread | None = None
        self.reconnects = 0  # 诊断计数（MYRTUI_LOOP_DEBUG=1 时由 cli 记录）

    # ---------- 基础 ----------

    def _base(self) -> list[str]:
        return [self.tmux, "-L", self.socket]

    def _run(
        self,
        args: list[str],
        *,
        stdin: str | None = None,
        check: bool = True,
        timeout: float = 10.0,
    ) -> subprocess.CompletedProcess:
        """命令执行入口：能走 control 就走，否则走 exec（计划 §2.2）。

        两条路径的返回值语义必须一致：stdout 逐字节相同、``check=False``
        时 returncode=1 + stderr。control 的**命令错**（``%error``）按同样
        口径返回；control 的**传输故障**（超时 / 管道关闭）关掉客户端、
        本条命令改走 exec，并后台重连——不新增用户可见状态。
        """
        client = self._control
        if client is not None and self._control_eligible(args, stdin):
            raw = any(name in _RAW_OUTPUT for name in _command_names(args))
            result = client.run(args, timeout, raw=raw)
            if result.ok:
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=result.stdout, stderr=""
                )
            if result.command_failed:
                if check:
                    raise TmuxError(
                        t(
                            "tmux.command_failed",
                            args=" ".join(args[:3]),
                            code=1,
                            stderr=result.error,
                        )
                    )
                return subprocess.CompletedProcess(
                    args=args, returncode=1, stdout="", stderr=result.error + "\n"
                )
            self._drop_control(result.error, client)
        return self._run_exec(args, stdin=stdin, check=check, timeout=timeout)

    @staticmethod
    def _control_eligible(args: list[str], stdin: str | None) -> bool:
        """这条命令能不能走 control（计划 §2.2 的排除表）。"""
        if stdin is not None:
            return False  # load-buffer / paste：行协议载不了 stdin
        if any("\n" in arg or "\r" in arg for arg in args):
            return False  # 行协议边界：换行参数直接降级 exec
        return not any(name in _CONTROL_DENY for name in _command_names(args))

    def _run_exec(
        self,
        args: list[str],
        *,
        stdin: str | None = None,
        check: bool = True,
        timeout: float = 10.0,
    ) -> subprocess.CompletedProcess:
        # stdin 必须显式隔离：前端跑在真实 PTY 里，若让 tmux client 继承
        # TTY stdin，它会创建 client 并与前端抢读终端输入 → 丢键。
        # 解码与 control 同口径（utf-8 + replace）：pane 里可能有非 UTF-8 字节，
        # 严格解码会抛 UnicodeDecodeError，两条路径的行为不该在这里分叉。
        proc = subprocess.run(
            self._base() + args,
            input=stdin,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if check and proc.returncode != 0:
            raise TmuxError(
                t(
                    "tmux.command_failed",
                    args=" ".join(args[:3]),
                    code=proc.returncode,
                    stderr=proc.stderr.strip(),
                )
            )
        return proc

    # ---------- 传输：常驻客户端（计划 §2.3） ----------

    @property
    def control_active(self) -> bool:
        return self._control is not None

    def open_control(self) -> bool:
        """挂常驻客户端；返回是否真的用上了 control。

        - ``exec`` 模式：no-op（逃生口 / 本地 A/B 的开关）；
        - ``auto``：attach 失败（无会话 / 非 tty / tmux 太老）记一条 warning，
          保持 exec，不新增用户可见状态；
        - ``control``：建不起来就抛 :class:`TmuxError`（验收模式，不静默降级）。

        调用时机：``create_session`` / ``apply_session_options`` 之后——那几步
        必须在客户端建立之前、走 exec 执行（计划 §2.2）。
        """
        if self.transport == TRANSPORT_EXEC or self._control is not None:
            return self._control is not None
        self._control_closed.clear()
        try:
            client = ControlClient(self.tmux, self.socket, self.session)
        except TmuxError as exc:
            if self.transport == TRANSPORT_CONTROL:
                raise
            log.warning("control transport unavailable (%s); staying on exec", exc)
            return False
        except OSError as exc:  # tmux 不在 PATH / fd 耗尽之类
            if self.transport == TRANSPORT_CONTROL:
                raise TmuxError(
                    t("tmux.control_unavailable", reason=exc)
                ) from None
            log.warning("control transport unavailable (%s); staying on exec", exc)
            return False
        with self._control_lock:
            if self._control is not None:
                client.close()  # 重连线程抢先装上了：保留它
                return True
            self._control = client
        log.info("tmux transport=control")
        return True

    def close_control(self) -> None:
        """关常驻客户端并停掉重连（退出路径第一步，计划 §2.3）。"""
        self._control_closed.set()
        with self._control_lock:
            client, self._control = self._control, None
        if client is not None:
            client.close()
            log.debug("control 客户端已关闭")
        thread = self._reconnect_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _drop_control(
        self, reason: str, client: ControlClient | None = None
    ) -> None:
        """传输故障：关客户端、本条命令降级 exec、后台按退避重连。

        ``client`` 给定时只关「确实失败的那个」——并发的重连线程可能已经装上了
        新客户端，这里不能把它一起关掉。
        """
        with self._control_lock:
            stale = self._control
            if client is not None and stale is not client:
                return
            self._control = None
        if stale is not None:
            stale.close()
        log.warning("control transport lost (%s); this command used exec", reason)
        self._start_reconnect()

    def _start_reconnect(self) -> None:
        if self._control_closed.is_set():
            return
        thread = self._reconnect_thread
        if thread is not None and thread.is_alive():
            return
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, name="myrtui-tmux-reconnect", daemon=True
        )
        self._reconnect_thread.start()

    def _reconnect_loop(self) -> None:
        """按 1 s / 2 s / 5 s 退避重连，最多 3 次（计划 §2.3）。

        会话被外部 kill 后同名重建（P2 已覆盖的路径）也走这里：重新
        ``attach -t <session>`` 即可，会话名不变。
        """
        for attempt, delay in enumerate(RECONNECT_BACKOFF, start=1):
            if self._control_closed.wait(delay):
                return
            if self._control is not None:
                return
            try:
                client = ControlClient(self.tmux, self.socket, self.session)
            except (TmuxError, OSError) as exc:
                log.debug("control 重连第 %d 次失败：%s", attempt, exc)
                continue
            with self._control_lock:
                if self._control_closed.is_set() or self._control is not None:
                    client.close()  # 被别的线程抢先恢复 / 已经在退出
                    return
                self._control = client
                self.reconnects += 1
            log.info("tmux transport=control (reconnected, attempt %d)", attempt)
            return
        log.warning(
            "control transport could not reconnect after %d attempts; staying on exec",
            len(RECONNECT_BACKOFF),
        )

    # ---------- 生命周期 ----------

    def socket_path(self) -> Path:
        """本实例 tmux server socket 的路径（``-L`` 的落点）。

        规则（tmux 3.8 实测）：``${TMUX_TMPDIR:-/tmp}/tmux-<uid>/<socket>``。
        tmux **不读** ``$TMPDIR``——本机 ``$TMPDIR`` 指向 /var/folders/…，
        而 ``#{socket_path}`` 给出 ``/private/tmp/tmux-501/…``；只认
        ``TMUX_TMPDIR``。测试里改 ``TMUX_TMPDIR`` 也验证过同一条规则。

        只给「server 是否还可能活着」的探测用（``server_listening``）；
        ``_base()`` 继续走 ``-L``，不在两处各维护一份路径规则。
        """
        base = os.environ.get("TMUX_TMPDIR") or "/tmp"
        return Path(base) / f"tmux-{os.getuid()}" / self.socket

    def server_listening(self) -> bool | None:
        """server socket 上是否有人监听：``True`` / ``False`` / ``None``（判不出）。

        比 ``has_session()`` 便宜三个数量级：残留 socket 逐个 ``connect``
        全探一遍 0.01 s，逐个 fork ``tmux has-session`` 是 4.98 s（2026-10-31
        本机 1094 个 socket / 1005 个锁实测）。``myrtui ls`` 因此不必为
        历史上打开过的每个项目付一次进程启动。

        只有「文件不存在」与「连接被拒」算 ``False``（内核说没人 listen）；
        超时 / ``EAGAIN`` / 非 socket 文件等一律 ``None``，调用方退回
        ``has_session()``——宁可多 fork 一次，也不能把活着的 detached
        会话报成 dead（``Instance.alive`` 直接决定 attach/stop 的判据）。
        """
        try:
            path = self.socket_path()
            if not path.exists():
                return False
        except OSError:
            return None  # 路径过长 / 权限异常：交给 tmux 自己判
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(0.2)
        try:
            sock.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError):
            return False  # 残留 socket 文件：server 已退出
        except OSError:
            return None
        else:
            return True
        finally:
            sock.close()

    def has_session(self) -> bool:
        proc = self._run(["has-session", "-t", self.session], check=False)
        return proc.returncode == 0

    def create_session(
        self,
        root: str,
        cols: int,
        rows: int,
        *,
        shell: str | None = None,
        history_limit: int | None = None,
    ) -> None:
        """M13 初始化序列：单次调用、先 ``set -g`` 后 ``new-session``。

        tmux 选项在 pane spawn 时快照，后设的实例参数对 pane 0 无效，
        四条 ``set -g`` 必须先于 ``new-session``。
        不传 shell 位置参数——那会得到非登录 shell（M5）。

        P2 启动参数（§12）：``shell``（``--shell``，缺省 ``$SHELL`` / ``/bin/zsh``）、
        ``history_limit``（``--scrollback``，缺省 5000）——同样必须在建会话前设好，
        否则 pane 0 拿到的是旧值（与 M13 同一机制）。
        """
        import os

        shell = shell or os.environ.get("SHELL") or "/bin/zsh"
        if history_limit is None:
            history_limit = DEFAULT_HISTORY_LIMIT
        self._run(
            [
                "start-server", ";",
                "set", "-g", "default-terminal", "tmux-256color", ";",
                "set", "-g", "remain-on-exit", "on", ";",
                "set", "-g", "history-limit", str(history_limit), ";",
                "set", "-g", "default-shell", shell, ";",
                "set", "-g", "set-clipboard", "on", ";",
                "new-session", "-d", "-s", self.session,
                "-c", root, "-x", str(cols), "-y", str(rows),
            ]
        )

    def apply_session_options(
        self,
        *,
        shell: str | None = None,
        history_limit: int | None = None,
    ) -> None:
        """会话已存在时下发 ``--shell`` / ``--scrollback``（P2 §12）。

        tmux 语义（实测）：``default-shell`` 与 ``history-limit`` 都在
        **window/pane 创建时取值/快照**——本调用只对**之后新建**的 window 生效，
        对 pane 0、已有 window 与 ``respawn-pane``（重跑建窗时记录的命令）
        均无效。调用方（cli）据此对用户显式提示。
        """
        parts: list[list[str]] = []
        if history_limit is not None:
            parts.append(["set", "-g", "history-limit", str(history_limit)])
        if shell is not None:
            parts.append(["set", "-g", "default-shell", shell])
        if not parts:
            return
        args: list[str] = []
        for i, part in enumerate(parts):
            if i:
                args.append(";")
            args.extend(part)
        self._run(args)

    def enable_clipboard_capture(self) -> None:
        """让 pane 内的 OSC 52 收进 tmux buffer（v0.31 分页器复制兜底）。

        myrtui 的前端**从不 attach tmux client**；tmux 3.8 默认
        ``set-clipboard external`` 只把 OSC 52 转发给 attached client——没有
        client 就丢掉。显式设 ``on`` 后 tmux 自己存进 paste buffer，
        ``Ctrl-a c`` 再读出来走 myrtui 的 OSC 52 + 系统命令双通道。
        对已存在会话在启动时补一次（``create_session`` 里已带）。
        """
        self._run(["set", "-g", "set-clipboard", "on"], check=False)

    def show_buffer(self) -> str | None:
        """最新 tmux paste buffer 的文本；无 buffer / 非文本 → ``None``。

        不用裸 ``show-buffer``：tmux 的「当前 buffer」只对无名缓冲
        （``set-buffer`` / OSC 52 默认创建的 ``bufferNNNN``）生效，显式
        ``-b name`` 建的缓冲不会成为当前 buffer。``list-buffers`` 按创建
        时间倒序 → 取第一行名字再用 ``-b`` 精确读取。
        """
        try:
            listing = self._run(
                ["list-buffers", "-F", "#{buffer_name}"], check=False
            )
        except UnicodeDecodeError:
            return None
        names = listing.stdout.splitlines()
        if listing.returncode != 0 or not names:
            return None
        try:
            proc = self._run(["show-buffer", "-b", names[0]], check=False)
        except UnicodeDecodeError:
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout

    def kill_server(self) -> None:
        # 控制客户端会随服务器一起退出，等不到 %end（计划 §2.2）→ 先关客户端
        # 再走 exec；否则 kill-server 命令本身会挂在读等待上。
        self.close_control()
        self._run(["kill-server"], check=False)

    def new_window(self, root: str) -> str:
        proc = self._run(
            ["new-window", "-d", "-t", self.session, "-c", root,
             "-P", "-F", "#{window_id}"]
        )
        return proc.stdout.strip()

    def kill_window(self, window_id: str) -> None:
        self._run(["kill-window", "-t", window_id], check=False)

    def respawn_pane(self, pane_id: str) -> None:
        """重开 dead pane（与 ``remain-on-exit`` 配套；P2 键位 ``Ctrl-a r``）。

        tmux 对活 pane 调 ``respawn-pane`` 会报错（不带 ``-k`` 不强杀），
        重跑的是**建窗时记录的命令**，不是当前 ``default-shell``（实测）。
        """
        self._run(["respawn-pane", "-t", pane_id])

    def resize_window(self, window_id: str, cols: int, rows: int) -> None:
        """resize-window 自动置 window-size=manual（N5）。

        禁用 refresh-client：那是 client 域命令，而我们从不 attach client。
        """
        self._run(
            ["resize-window", "-t", window_id, "-x", str(cols), "-y", str(rows)],
            check=False,
        )

    # ---------- 状态查询 ----------

    def list_panes(self) -> list[PaneInfo]:
        # -s 才是 session 目标（N11）
        proc = self._run(["list-panes", "-s", "-t", self.session, "-F", _PANE_FORMAT])
        return self._parse_panes(proc.stdout)

    @staticmethod
    def _parse_panes(out: str) -> list[PaneInfo]:
        panes = []
        for line in out.splitlines():
            parts = line.split(" ", 16)
            if len(parts) < 17:
                continue
            (pid, dead, cmd, alt, cx, cy, cflag, hsize, h, w, wid, widx,
             mstd, mbtn, many, msgr, mall) = parts
            panes.append(
                PaneInfo(
                    pane_id=pid,
                    dead=dead == "1",
                    command=cmd,
                    alternate_on=alt == "1",
                    cursor_x=int(cx),
                    cursor_y=int(cy),
                    cursor_flag=cflag == "1",
                    history_size=int(hsize),
                    height=int(h),
                    width=int(w),
                    window_id=wid,
                    window_index=int(widx),
                    mouse_standard=mstd == "1",
                    mouse_button=mbtn == "1",
                    mouse_any=many == "1",
                    mouse_sgr=msgr == "1",
                    mouse_all=mall == "1",
                )
            )
        return panes

    # ---------- capture ----------

    def _capture_args(self, pane_id: str, k: int, height: int) -> list[str]:
        args = ["capture-pane", "-p", "-e", "-N"]
        if k > 0:
            # M1：-S -K 单独用返回 H+K 行且末行仍是实时底部，必须配 -E
            args += ["-S", f"-{k}", "-E", str(height - 1 - k)]
        args += ["-t", pane_id]
        return args

    @staticmethod
    def _count_lines(text: str) -> int:
        # M18：按 splitlines() 语义计数，不用 \n 计数（末行空白/缺尾换行时差 1）
        return len(text.splitlines())

    def _capture_checked(
        self, pane_id: str, k: int, height: int
    ) -> CaptureResult:
        """单 pane capture + 行数断言（回归防线，M3/M18）。

        断言失败（静默降级的信号）：收紧 K 重试一次，仍失败退回实时视图（K=0）。
        """
        proc = self._run(self._capture_args(pane_id, k, height))
        text = proc.stdout
        if self._count_lines(text) == height or k == 0:
            return CaptureResult(text=text, offset=k)
        k2 = max(0, k // 2)  # 收紧 K 重试一次
        proc = self._run(self._capture_args(pane_id, k2, height))
        text = proc.stdout
        if self._count_lines(text) == height or k2 == 0:
            return CaptureResult(text=text, offset=k2)
        log.warning("capture 行数断言失败（pane=%s），退回实时视图", pane_id)
        proc = self._run(self._capture_args(pane_id, 0, height))
        return CaptureResult(text=proc.stdout, offset=0)

    def poll(
        self, offsets: dict[str, tuple[int, int]]
    ) -> tuple[list[PaneInfo], dict[str, CaptureResult]]:
        """单次进程调用抓完全部 pane + 状态（N14）。

        Args:
            offsets: {pane_id: (k, height)}；k=0 即实时视图。
        """
        sep = f"__MYRTUI_SEP_{uuid.uuid4().hex}__"
        args: list[str] = []
        order: list[str] = []
        for pane_id, (k, height) in offsets.items():
            # K 的 clamp 由调用方按 history_size-1 完成（M12）；这里只防负值。
            # 越界/非法导致的静默降级由下行数断言兜底（M3/M18）。
            args += self._capture_args(pane_id, max(0, k), height)
            args += [";", "display-message", "-p", sep, ";"]
            order.append(pane_id)
        args += ["list-panes", "-s", "-t", self.session, "-F", _PANE_FORMAT]
        proc = self._run(args)

        blocks: list[list[str]] = [[]]
        for line in proc.stdout.splitlines():
            if line == sep:
                blocks.append([])
            else:
                blocks[-1].append(line)
        capture_blocks = blocks[: len(order)]
        panes = self._parse_panes("\n".join(blocks[len(order)]))

        results: dict[str, CaptureResult] = {}
        heights = {p.pane_id: p.height for p in panes}
        for pane_id, block in zip(order, capture_blocks):
            k, _ = offsets[pane_id]
            height = heights.get(pane_id, offsets[pane_id][1])
            # 补回 stdout 的尾换行：stdout.splitlines() 不保留「末行是空行」
            # 这一信息，直接 join 会让 splitlines() 计数少 1（M18 口径）——
            # 新 pane 底行恒空 → 恒渲染 H-1 行、末行光标不画。补 \n 后
            # splitlines(text) == len(block) == 原始 stdout 计数，恒等成立。
            text = ("\n".join(block) + "\n") if block else ""
            if self._count_lines(text) != height and k > 0:
                # 静默降级信号（resize 竞态 / 越界），走带断言的单 pane 路径
                results[pane_id] = self._capture_checked(pane_id, k, height)
            else:
                results[pane_id] = CaptureResult(text=text, offset=k)
        return panes, results

    # ---------- 输入 ----------

    def send_keys(self, pane_id: str, *keys: str) -> None:
        """键名注入。DECCKM 由 tmux 按 pane 模式自决，客户端零模式跟踪（N2）。"""
        if keys:
            self._run(["send-keys", "-t", pane_id, *keys])

    def send_text(self, pane_id: str, text: str) -> None:
        """字串注入（literal UTF-8），不溢出 ARG_MAX（N17 口径）。

        文字用 :class:`_Arg` 标记：单个 ``;`` 是合法的键入内容，不能被控制
        模式当成命令分隔符拆开（否则打一个分号就丢半条命令）。
        """
        if text:
            self._run(["send-keys", "-l", "-t", pane_id, "--", _Arg(text)])

    def run_command(self, pane_id: str, command: str) -> None:
        """把一条 shell 命令送进 pane 并回车（v0.28 查看 / 渲染，v0.31 打开）。

        单次 tmux 调用完成「C-u 清行 + 字面命令 + Enter」：

        - ``C-u`` 先清掉用户可能已输入但未执行的内容，避免命令拼接；
        - ``send-keys -l --`` 保证命令按字面注入，路径里的 ``;`` / 管道符
          不会被 tmux 当成键名或命令分隔符；
        - 命令由 :mod:`myrtui.pane_commands` 负责引号、降级与安全检查。
        """
        if not command:
            return
        self._run(
            [
                "send-keys", "-t", pane_id, "C-u", ";",
                "send-keys", "-l", "-t", pane_id, "--", _Arg(command), ";",
                "send-keys", "-t", pane_id, "Enter",
            ]
        )

    def send_ctrl_a(self, pane_id: str) -> None:
        """前缀超时时把 Ctrl-a 本身发给内层 shell。"""
        self.send_keys(pane_id, "C-a")

    def send_mouse(self, pane_id: str, sequence: str) -> None:
        """注入鼠标序列（v0.7 §10.2，按 pane 编码为 SGR 或 xterm 旧 3 字节）。

        - ``send-keys -H`` 必须**逐字节传参**——整串 hex 作单参数实测无效
          （tmux 把一个参数当一个 key 解析）；
        - **latin-1 而非 UTF-8**（审阅 F3 关联必现 bug）：旧编码坐标字节
          = ``33 + x``，宽 pane（x > 94）会超 0x7f，UTF-8 会把单字节打成
          双字节、三字节帧全错；latin-1 保证 codepoint 0–255 ↔ 单字节。
        """
        hexes = [f"{b:02x}" for b in sequence.encode("latin-1")]
        if hexes:
            self._run(["send-keys", "-H", "-t", pane_id, *hexes])

    def paste(self, pane_id: str, text: str) -> None:
        """load-buffer + paste-buffer -p：仅当 callee 已请求 2004 才包 bracket（N17）。"""
        if not text:
            return
        self._run(["load-buffer", "-b", PASTE_BUFFER, "-"], stdin=text)
        self._run(
            ["paste-buffer", "-p", "-d", "-b", PASTE_BUFFER, "-t", pane_id],
            check=False,
        )
