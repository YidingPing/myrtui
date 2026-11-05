"""外层终端复用器探测：判断 myrtui 是不是跑在别人的 tmux / screen 里面。

为什么需要：myrtui 自己虽然用 tmux 管 pane，但**它也可以被套在另一层复用器
里**（UU 远程的终端就是一个自带 tmux 3.5a 的会话）。外层那层如果 `mouse on`，
它的 root 表会先处理鼠标——UU 的配置把 `MouseDrag1Pane` 改成无条件进 copy-mode，
于是按下能到 myrtui、**按住期间的 motion 全被 copy-mode 吃掉**，拖拽选字/转发
整体失效（0.7.0 真机实测：按住 7.5 秒、0 条 motion，松手时 `copy-pipe pbcopy`
把选区写进了系统剪贴板）。

myrtui 无法从内部改变外层复用器的绑定（也不该去改用户的终端配置），能做的
只有两件事：

1. **认出来**（:func:`outer_multiplexer`）——纯环境变量判断，零成本；
2. **读出它真实的绑定**（:func:`probe_drag_forwarding`）——只在识别到外层
   复用器、且没被 ``MYRTUI_OUTER_MUX_HINT=0`` 关掉时跑一次，全部子进程都有
   超时，失败一律当作「不知道」，绝不阻塞 UI、绝不误伤别人的 server。

0.11.1 起提示不再是「猜」的：探到绑定确实吞掉拖动时，把**可以直接粘贴的修复
命令**交给 UI（宽终端放顶栏，窄终端只写日志）。绑定已经正确就闭嘴——UU 用户
每次启动都看到一条已经没用的排障文案就是噪音。

本模块只做判断与探测，不碰环境、不 import Textual。

**为什么不直接 `tmux bind` 修**：那是用户的终端配置，且 UU 每次重启 server 都会
从 app bundle 的 `uuremote.conf` 重新加载那条错误绑定——能自动改就等于偷偷
改配置，出问题也没人知道是谁改的。修复动作永远由用户显式执行。
"""
from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass

log = logging.getLogger("myrtui.termenv")


def outer_multiplexer(env: Mapping[str, str]) -> str | None:
    """返回外层复用器的名字（``"tmux"`` / ``"screen"``），认不出则 ``None``。

    判据按可靠度排序：

    1. ``$TMUX``（tmux 给 pane 里的进程设的 socket 路径）——最硬，但它也可能被
       配置主动 unset（UU 就这么干，好让用户在里面再开自己的 tmux）；
    2. ``$STY``（screen 的会话名）；
    3. ``$TERM_PROGRAM=tmux``——tmux 3.2+ 设它，不随 ``unset TMUX`` 消失；
    4. ``$TERM`` 以 ``tmux`` / ``screen`` 开头（tmux 的默认终端是
       ``tmux-256color`` / ``screen-256color``）。

    注意：**不是**在判断「我是不是 tmux 后端」——那是自己的 server，与本函数
    无关。
    """
    if env.get("TMUX"):
        return "tmux"
    if env.get("STY"):
        return "screen"
    program = (env.get("TERM_PROGRAM") or "").strip().lower()
    if program == "tmux":
        return "tmux"
    term = (env.get("TERM") or "").strip().lower()
    if term.startswith("tmux"):
        return "tmux"
    if term.startswith("screen"):
        return "screen"
    return None


# 提示的开关键：外层复用器在场是常态（UU 用户每次启动都在里面），一直弹一条
# 已经没用处的排障文案就是噪音 → 给一个显式关掉的入口。
HINT_ENV = "MYRTUI_OUTER_MUX_HINT"
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def hint_enabled(raw: str | None) -> bool:
    """``MYRTUI_OUTER_MUX_HINT`` → 是否显示启动提示（默认 **True**）。

    口径与 ``MYRTUI_CLIPBOARD_PASSTHROUGH`` 一致：只有明确的假值才关，
    手滑写错的值按默认（开）处理。
    """
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() not in _FALSE_VALUES


# ---------- 绑定探测（0.11.1） ----------
#
# ``$TMUX`` 被 UU 主动 unset，所以「外层 server 的 socket」不能只靠环境变量：
# 真正的线索是**祖先进程的 argv**（pane 的 shell 由外层 server 直接拉起，server
# 的 argv 里保留着 ``-S <socket>``）。找到 socket 之后还要验证它确实属于我们的
# 祖先链——机器上同时开着好几个 tmux server 是常态（本机实测 10+ 个），
# 对着别人的 server 报「你的绑定错了」比不报更糟。

#: 显式指定外层 socket 的逃生口（自动探测失效时用；文档 README 有登记）。
SOCKET_ENV = "MYRTUI_OUTER_TMUX_SOCKET"

#: 单条探测子进程的超时（``ps`` / ``tmux list-keys`` 都是毫秒级，2 s 是兜底）。
PROBE_TIMEOUT = 2.0

#: 祖先链最多向上找几层：myrtui → shell →（可能再套一层）→ 外层 server。
MAX_ANCESTOR_DEPTH = 8

DRAG_KEY = "MouseDrag1Pane"

#: tmux 内建 `MouseDrag1Pane` 的判定式：在 copy-mode 里，或 pane 内程序请求了
#: 鼠标，就把事件交给程序（= myrtui），否则才进 copy-mode 自己选。
#:
#: **``pane_in_mode`` 不能省**：``copy-mode -M`` 每次都会**重新起拖**而不是
#: 延长选区，所以普通 shell 上连续 motion 必须走 ``send-keys -M`` → 落进
#: copy-mode 的 key 表（那里才有 ``begin-selection``）。UU 自带的绑定只判
#: ``pane_in_mode``，正是「程序要了鼠标也被拖进 copy-mode」的根源。
FIX_CONDITION = "#{||:#{pane_in_mode},#{mouse_any_flag}}"

#: 已知的 socket 位置（排在祖先进程之后，只作兜底；每个都要过 pid 验证）。
#: UU 远程把 socket 放在自己的 Application Support 下，而不是 /tmp/tmux-*。
_KNOWN_SOCKETS = ("~/Library/Application Support/UURemote/tmux.sock",)


@dataclass(frozen=True)
class OuterServer:
    """外层 tmux server 的「怎么跟它说话」：客户端可执行文件 + socket 路径。"""

    exe: str
    socket: str


@dataclass(frozen=True)
class DragProbe:
    """一次绑定探测的结果。"""

    server: OuterServer
    binding: str

    @property
    def needs_fix(self) -> bool:
        return binding_needs_fix(self.binding)

    @property
    def command(self) -> str:
        """可直接粘贴到 shell 的修复命令（路径含空格时自动加引号）。"""
        return shlex.join(fix_argv(self.server.exe, self.server.socket))


def parse_tmux_env(raw: str | None) -> str | None:
    """``$TMUX``（``<socket>,<pid>,<session>``）→ socket 路径。"""
    if not raw:
        return None
    parts = raw.rsplit(",", 2)
    if len(parts) != 3:
        return None
    socket = parts[0].strip()
    return socket or None


def socket_from_argv(
    args: str,
    *,
    exists: Callable[[str], bool] = os.path.exists,
    uid: int | None = None,
) -> str | None:
    """从 ``ps`` 的 argv 文本里取 tmux 的 ``-S <socket>`` / ``-L <name>``。

    ``ps args`` 用单个空格拼接 argv（引号已经丢了），所以**带空格的 socket
    路径会被拆成多段**——UU 的路径就是 ``…/Application Support/UURemote/…``。
    这里按「拼回去之后路径真的存在」逐段贪心合并，拼不出来就放弃（宁可探不到，
    也不猜一个不存在的路径）。
    """
    tokens = args.split()
    for index, token in enumerate(tokens):
        if token not in ("-S", "-L") or index + 1 >= len(tokens):
            continue
        name = tokens[index + 1]
        if token == "-L":
            # ``-L`` 是相对 socket 名：tmux 放在 /tmp/tmux-<uid>/ 下。
            if exists(name):
                return name
            candidate = f"/tmp/tmux-{os.getuid() if uid is None else uid}/{name}"
            return candidate if exists(candidate) else None
        if exists(name):
            return name
        joined = name
        for extra in tokens[index + 2:]:
            if extra.startswith("-"):
                break
            joined = f"{joined} {extra}"
            if exists(joined):
                return joined
        return None
    return None


def binding_needs_fix(binding: str | None) -> bool:
    """这一行 ``MouseDrag1Pane`` 绑定是否会把拖动从程序手里抢走。

    只有**确定**是坏形态才返回 True：绑定里出现 ``copy-mode``（会自己起拖选）
    且**没有** ``mouse_any_flag``（完全不看 pane 内程序要不要鼠标）。裸的
    ``send-keys -M``、或者已经是 tmux 内建判定式，都算正常——不认识的形态一律
    当作「没事」，绝不基于猜测催用户改配置。
    """
    if not binding:
        return False
    line = binding.strip().lower()
    if DRAG_KEY.lower() not in line:
        return False
    if "copy-mode" not in line:
        return False
    return "mouse_any_flag" not in line


def fix_argv(exe: str, socket: str) -> list[str]:
    """恢复 tmux 内建 ``MouseDrag1Pane`` 语义的 argv（不含 shell 引号）。"""
    return [
        exe,
        "-S",
        socket,
        "bind",
        "-Troot",
        DRAG_KEY,
        "if-shell",
        "-F",
        FIX_CONDITION,
        "send -M",
        "copy-mode -M",
    ]


# ---------- 探测实现（全部子进程可注入，测试不碰真实环境） ----------

def _ps_read(pid: int) -> tuple[int | None, str | None]:
    """``ps -o ppid=,args= -p <pid>`` → ``(ppid, args)``；失败返回 ``(None, None)``。"""
    try:
        proc = subprocess.run(
            ["ps", "-o", "ppid=,args=", "-p", str(pid)],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("outer-mux: ps failed for %s (%s)", pid, exc)
        return None, None
    line = proc.stdout.strip()
    if not line:
        return None, None
    head, _, rest = line.partition(" ")
    try:
        return int(head), rest.strip()
    except ValueError:
        return None, None


def _system_tmux() -> str | None:
    """PATH 里的 ``tmux``（返回**命令名**而不是绝对路径：顶栏那条命令要短）。"""
    return "tmux" if shutil.which("tmux") else None


def _run(argv: list[str]) -> str | None:
    """跑一条探测命令，成功返回 stdout（去尾空白），失败返回 ``None``。"""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, errors="replace",
            timeout=PROBE_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("outer-mux: %s failed (%s)", argv, exc)
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def ancestor_chain(
    pid: int,
    *,
    ps: Callable[[int], tuple[int | None, str | None]] | None = None,
    limit: int = MAX_ANCESTOR_DEPTH,
) -> list[tuple[int, str]]:
    """**自己 + 祖先**的 ``(pid, args)`` 列表（由近到远），最多 ``MAX_ANCESTOR_DEPTH`` 项。

    防御：父 pid 为 0/1（launchd/init）或成环就停——探测路径绝不允许无限循环。
    """
    read = ps or _ps_read
    chain: list[tuple[int, str]] = []
    seen: set[int] = set()
    current = pid
    for _ in range(limit):
        if current in seen:
            break
        seen.add(current)
        ppid, args = read(current)
        if ppid is None:
            break
        chain.append((current, args or ""))
        if ppid <= 1:
            break
        current = ppid
    return chain


def _server_pid(run: Callable[[list[str]], str | None],
                exe: str, socket: str) -> int | None:
    out = run([exe, "-S", socket, "display-message", "-p", "#{pid}"])
    if not out:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def _socket_candidates(
    env: Mapping[str, str],
    chain: list[tuple[int, str]],
    *,
    exists: Callable[[str], bool],
    uid: int | None,
) -> list[OuterServer]:
    """按可靠度给出候选 ``(exe, socket)``，已去重。"""
    candidates: list[OuterServer] = []
    seen: set[str] = set()

    def add(exe: str | None, socket: str | None) -> None:
        if not socket or not exe or socket in seen:
            return
        seen.add(socket)
        candidates.append(OuterServer(exe, socket))

    system_tmux = _system_tmux()
    explicit = (env.get(SOCKET_ENV) or "").strip()
    if explicit:
        add(system_tmux or "tmux", explicit)
    # $TMUX 是 tmux 自己写的真值；它也可能是 ``-L`` 相对名，这里只认绝对路径。
    add(system_tmux or "tmux", parse_tmux_env(env.get("TMUX")))
    # 祖先进程（最近的一层在前）：server 的 argv 里带着 -S。
    for _pid, args in chain:
        socket = socket_from_argv(args, exists=exists, uid=uid)
        if socket:
            add(args.split()[0] if args.split() else None, socket)
    for known in _KNOWN_SOCKETS:
        path = os.path.expanduser(known)
        if exists(path):
            add(system_tmux or "tmux", path)
    return candidates


def probe_drag_forwarding(
    env: Mapping[str, str],
    pid: int,
    *,
    ps: Callable[[int], tuple[int | None, str | None]] | None = None,
    run: Callable[[list[str]], str | None] | None = None,
    exists: Callable[[str], bool] = os.path.exists,
    uid: int | None = None,
) -> DragProbe | None:
    """探外层 tmux 的 ``MouseDrag1Pane``；探不到一律返回 ``None``。

    返回 ``None`` 的语义是「不知道」，调用方退回通用提示。只有**确认** socket
    属于本进程祖先链（``display-message -p '#{pid}'`` 命中祖先）才会继续——
    机器上并存的其它 tmux server 不应该被误判成外层。
    """
    reader = run or _run
    chain = ancestor_chain(pid, ps=ps)
    own = {p for p, _a in chain}
    if not own:
        own = {pid}
    for candidate in _socket_candidates(env, chain, exists=exists, uid=uid):
        # 客户端优先用系统 tmux（命令更短、用户更眼熟）；它连不上（协议/版本
        # 不兼容）再退回祖先进程自己的可执行文件。
        exes = [_system_tmux(), candidate.exe]
        for exe in dict.fromkeys(e for e in exes if e):
            server_pid = _server_pid(reader, exe, candidate.socket)
            if server_pid is None or server_pid not in own:
                continue
            binding = reader([
                exe, "-S", candidate.socket, "list-keys", "-T", "root", DRAG_KEY,
            ])
            if not binding:
                continue
            return DragProbe(OuterServer(exe, candidate.socket), binding)
    return None
