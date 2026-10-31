"""tmux 后端封装（DESIGN.md §4.2 接口表）。

约定：
- 所有命令都带 ``-L <socket-name>``，走隔离实例；
- 前端从不 attach tmux client，每次调用都是秒级生灭的短命 client；
- 多 pane 时用 ``;`` 串联，单次进程调用抓完全部（N14）；
- capture 永不追加 ``-a``（M2），永不启用 ``-J``（M11）；
- 翻页公式 ``-S -K -E $((H-1-K))``（M1），K 先 clamp 到 ``history_size-1``（M12），
  每次断言返回行数 == H（M3/M18），失败收紧 K 重试一次，仍失败退回实时视图。
"""
from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
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


class TmuxBackend:
    def __init__(self, socket_name: str, session_name: str, tmux_bin: str = "tmux"):
        self.socket = socket_name
        self.session = session_name
        self.tmux = tmux_bin

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
        # stdin 必须显式隔离：前端跑在真实 PTY 里，若让 tmux client 继承
        # TTY stdin，它会创建 client 并与前端抢读终端输入 → 丢键。
        proc = subprocess.run(
            self._base() + args,
            input=stdin,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
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
        """字串注入（literal UTF-8），不溢出 ARG_MAX（N17 口径）。"""
        if text:
            self._run(["send-keys", "-l", "-t", pane_id, "--", text])

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
                "send-keys", "-l", "-t", pane_id, "--", command, ";",
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
