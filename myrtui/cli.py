"""myrtui 入口（DESIGN.md §4 cli.py / §6 / §9 / §12 P2 启动参数）。

流程：
1. 解析启动参数（P2：``--root`` / ``--shell`` / ``--scrollback``）并校验；
2. hash 派生：``os.path.realpath(...)`` 的 sha256 前 16 位 hex（M19：
   必须物理路径，禁用 $PWD / 未归一化路径，否则符号链接路径会分裂出会话；
   ``--root`` 同样先 realpath）；
3. 启动期 ``tmux -V`` 校验（M4/M16：正则解析，失败放行，< 3.1 拒绝）；
4. flock 抢占（§6）：P1 为「旁挂 ``<hash>.takeover`` 接管标记 + 保鲜校验交接」，
   抢不到锁时写标记并轮询等锁（带超时），全程无 pid kill；锁文件永不 unlink，
   inode 恒定（M14）；
5. 无会话则按 M13 序列创建（先 set -g 后 new-session，单次调用，含 P2 的
   shell / history-limit）；会话已存在则把显式参数下发给**之后新建**的 pane；
6. 进入 Textual UI（持锁期间运行 §6 哨戒：读接管标记、两级校验通过才让位）。

实例管理子命令（DISTRIBUTION-INSTANCES-PLAN §3）：``argv[1] ∈ {ls, list,
attach, stop}`` 时预分发到 ``myrtui.instances``，本模块只负责裸启动路径。
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import logging
import os
import shutil
import sys
import time

import platformdirs

from myrtui.tmux_backend import (
    DEFAULT_HISTORY_LIMIT,
    TmuxError,
    TmuxNotFoundError,
    TmuxBackend,
    check_tmux,
)

log = logging.getLogger("myrtui")

APP_NAME = "myrtui"

# §11 / §12 P2：--scrollback 取值范围（0 无意义，过大值等于让用户手滑吃内存）
MAX_SCROLLBACK = 1_000_000

# ── §6 P1 接管交接参数 ──────────────────────────────────────────────
TAKEOVER_POLL = 0.1  # LOCK_NB 轮询等锁间隔
TAKEOVER_REFRESH = 1.0  # 标记刷新周期（保持新鲜，远小于保鲜期）
TAKEOVER_TIMEOUT = 5.0  # 等锁总超时（发起方正常 1~2s 内完成交接）
TAKEOVER_MAX_AGE = 10.0  # 标记保鲜期（M15：过期按垃圾处理）
SENTRY_INTERVAL = 0.5  # 持锁者哨戒读周期（§6：每 500ms 读接管标记）


def derive_identity(cwd: str | None = None) -> tuple[str, str]:
    """返回 (project_root, hash)。root 取 realpath（M19），禁用 $PWD。

    P2 的 ``--root`` 走同一条路径：同样先 realpath 再派生——否则
    「符号链接路径 → 两套锁/两套 socket/两个会话」的分裂会在 --root 上重演。
    """
    root = os.path.realpath(cwd or os.getcwd())
    digest = hashlib.sha256(root.encode("utf-8")).hexdigest()[:16]
    return root, digest


def _data_dir() -> str:
    data_dir = platformdirs.user_data_dir(APP_NAME, appauthor=False)
    os.makedirs(data_dir, exist_ok=True)  # M20：首次必须建
    return data_dir


def lock_path_for(digest: str) -> str:
    """锁文件位置：<data>/myrtui/<hash>.lock（§6 M17：user_data_dir）。

    锁文件本身永不 unlink（inode 恒定是 flock 互斥的前提，M14/M17）。
    """
    return os.path.join(_data_dir(), f"{digest}.lock")


def takeover_path_for(digest: str) -> str:
    """旁挂接管标记：<data>/myrtui/<hash>.takeover（§6 M21 默认通道）。

    与锁文件不同，它不承载 flock 语义 → 允许 temp+rename 原子写、
    允许 unlink（锁到手后清理）。
    """
    return os.path.join(_data_dir(), f"{digest}.takeover")


def stop_path_for(digest: str) -> str:
    """旁挂停止标记：<data>/myrtui/<hash>.stop（实例管理命令 ``myrtui stop``）。

    与 takeover 分开两个文件（DISTRIBUTION-INSTANCES-PLAN §3.4 / 评审 R6）：
    takeover 标记被等锁的 attach 者每 1s 刷新、语义是「我要前台」；
    stop 的语义是「请整个退场」。混用同一文件会让让位与停止互相干扰，
    也会让等待中的 attach 者误触发停止。写法同 takeover（原子写、可 unlink）。
    """
    return os.path.join(_data_dir(), f"{digest}.stop")


def layout_path_for(digest: str) -> str:
    """布局比例文件：<data>/myrtui/<hash>.layout.json（v0.11）。

    与锁 / 接管标记同目录同命名约定（按项目 hash 分文件 → 同一项目的
    下次 attach 能恢复上次的分割位置，不同项目互不干扰）。
    """
    return os.path.join(_data_dir(), f"{digest}.layout.json")


# ── 接管标记：写 / 读 / 校验（§6 P1，M15 两级校验 + M21 异常语义） ──


def write_takeover_marker(
    path: str,
    pid: int | None = None,
    ts: float | None = None,
    *,
    prefix: str = "takeover",
) -> None:
    """原子写 ``<prefix>:<pid>:<时间戳>``（temp+rename；旁挂文件专属写法）。

    ``prefix`` 区分两种旁挂标记（评审 R6）：``takeover`` = 请让位给我，
    ``stop`` = 请整个退场（``myrtui stop``）。默认值保持接管路径零改动。
    """
    pid = os.getpid() if pid is None else pid
    ts = time.time() if ts is None else ts
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        f.write(f"{prefix}:{pid}:{ts}\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)  # 仅旁挂文件允许 replace（不承载 flock 语义）


def clear_takeover(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def read_takeover_markers(path: str, *, prefix: str = "takeover") -> list[tuple[int, float]]:
    """读取旁挂标记，返回 [(pid, ts), ...]；``prefix`` 过滤标记类型（评审 R6）。

    M14 读取容忍：原地并发/半截内容/格式异常一律视为「无该类请求」，
    不得据此触发任何动作。
    """
    try:
        with open(path) as f:
            content = f.read()
    except OSError:
        return []
    markers: list[tuple[int, float]] = []
    for line in content.splitlines():
        parts = line.split(":")
        if len(parts) != 3 or parts[0] != prefix:
            continue
        try:
            markers.append((int(parts[1]), float(parts[2])))
        except ValueError:
            continue
    return markers


def is_marker_valid(pid: int, ts: float, now: float | None = None) -> bool:
    """M15 两级校验：标记新鲜 + 发起方存活。全过才允许让位。

    M21 异常语义（写死）：``os.kill(pid, 0)`` 只有 ``ProcessLookupError``
    表示「不存在」；``PermissionError`` 等其余异常一律视为「不是我们的前端」
    → 不让位（实测 pid 1 即 PermissionError）。误判方向安全（至多不让位）。
    """
    now = time.time() if now is None else now
    if now - ts >= TAKEOVER_MAX_AGE:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False  # 发起方已死 → 标记是垃圾
    except Exception:
        return False  # PermissionError 等 → 不是我们控制的前端 → 不让位
    return True


# ── 锁获取 ──────────────────────────────────────────────────────────


def acquire_lock(path: str, *, root: str | None = None, write_record: bool = True) -> int:
    """P0 语义：抢不到锁即拒绝（保留给测试与无接管路径）。

    fd 用 O_RDWR 一次打开（M22：flock 与记录读写共用）。
    ``root`` 非空时把项目根写进持有者记录第二行（实例管理 ls/attach 反查用）；
    ``write_record=False`` 供 ``myrtui stop`` 使用——它拿锁只为 kill 会话，
    不该把锁文件记录覆写成 stop 进程自己的 pid/root（评审 R3）。
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise
    if write_record:
        write_holder_record(fd, root=root)
    return fd


def write_holder_record(fd: int, root: str | None = None) -> None:
    """原地覆写持有者记录（M14：seek+truncate+write，严禁 rename/replace）。

    格式（实例管理，计划 §3.1）：第一行 ``pid``；``root`` 非空时第二行
    项目根路径——``myrtui ls/attach/stop`` 靠它把 hash 反查回项目目录。
    ``root=None`` 保持旧单行格式（无项目上下文的调用方，兼容既有测试）。
    """
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    data = f"{os.getpid()}\n" if root is None else f"{os.getpid()}\n{root}\n"
    os.write(fd, data.encode())


def read_holder_record(path: str) -> tuple[int | None, str | None]:
    """读持有者记录，返回 ``(pid, root)``；缺失/半截/格式异常容忍为 ``(None, None)``。

    旧格式（单行 pid）→ ``(pid, None)``；根目录行缺失同样 ``root=None``。
    """
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return None, None
    if not lines:
        return None, None
    try:
        pid: int | None = int(lines[0])
    except ValueError:
        pid = None
    root = lines[1] if len(lines) > 1 and lines[1] else None
    return pid, root


def probe_lock_held(path: str) -> bool:
    """flock 试探（``myrtui ls`` 专用，评审 M6/R3）：只判不写。

    ``open + LOCK_EX|LOCK_NB + 立即 unlock + close``——不走 ``acquire_lock*``
    （那会覆写 holder record 成试探者自己的 pid，脏记录）。持锁仅微秒级，
    期间真前端抢锁失败会走接管轮询，放锁后下一轮即成功（计划 §5 R3）。
    """
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False  # 锁文件不存在 = 无实例（glob 扫描时理论上不会到这）
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True  # 有人持锁 = 前台运行中
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _drop_own_marker(path: str) -> None:
    """超时/异常离场时清掉自己的标记（别人的标记不碰）。

    不清的话：标记里的 pid 仍是本进程且新鲜，持锁者哨戒会对一个已经
    放弃等待的前端让位，白丢一次前台。
    """
    if any(pid == os.getpid() for pid, _ in read_takeover_markers(path)):
        clear_takeover(path)


def acquire_lock_with_takeover(
    lock_path: str,
    takeover_path: str,
    *,
    timeout: float | None = None,
    poll: float | None = None,
    refresh: float | None = None,
    root: str | None = None,
) -> int:
    """P1 抢占：抢不到锁 → 写接管标记 + 轮询等锁（带超时），无任何 pid kill。

    等待期间周期刷新标记保持新鲜（§6 步骤 1）；锁到手后原地覆写持有者
    记录并清掉旁挂标记（§6 步骤 3）。超时抛 ``TimeoutError``（§9）。
    timeout/poll/refresh 默认取模块常量，显式参数仅供测试缩短时长。
    ``root`` 写入持有者记录第二行（同 ``acquire_lock``）。
    """
    timeout = TAKEOVER_TIMEOUT if timeout is None else timeout
    poll = TAKEOVER_POLL if poll is None else poll
    refresh = TAKEOVER_REFRESH if refresh is None else refresh
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    next_refresh = 0.0
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                pass
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError(f"等锁超过 {timeout:.0f}s 未获让位")
            if now >= next_refresh:
                write_takeover_marker(takeover_path)
                next_refresh = now + refresh
            time.sleep(poll)
    except BaseException:
        # 含 TimeoutError / KeyboardInterrupt：不把已放弃的标记留给哨戒者
        os.close(fd)
        _drop_own_marker(takeover_path)
        raise
    write_holder_record(fd, root=root)
    clear_takeover(takeover_path)
    return fd


# ── 启动参数（§12 P2） ──────────────────────────────────────────────

# 实例管理子命令（计划 §3.5）：main() 按 argv[1] 字面量预分发到
# myrtui.instances；取值空间原本就无位置参数（`myrtui ls` 现为用法错误），
# 预分发不缩小既有合法输入集。
SUBCOMMANDS = frozenset({"ls", "list", "attach", "stop"})


def _version_string() -> str:
    """``--version`` 用：包未以分发形式安装（裸跑源码）→ unknown。"""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("myrtui")
    except PackageNotFoundError:
        return "unknown"


def build_parser() -> argparse.ArgumentParser:
    """``myrtui [--root PATH] [--shell PATH] [--scrollback N] [--version]``。

    取值畸形（非整数的 --scrollback 等）交给 argparse → 退出码 2（用法错误）；
    取值合法但环境不满足（root 不是目录、shell 不可执行、越界）由 ``main``
    打印原因后返回 1（运行错误，与既有错误路径口径一致）。

    子命令 ``ls`` / ``list`` / ``attach`` / ``stop`` 不进本解析器——由
    ``main`` 按 ``argv[1]`` 字面量预分发（实例管理）。
    """
    p = argparse.ArgumentParser(
        prog="myrtui",
        description="面向单个项目的轻量终端复用器（Textual 前端 + tmux 后端）。",
        epilog="子命令：ls（列出实例）、attach（接入实例）、stop（结束实例）；"
               "也可写 myrtui list（ls 别名）。",
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"myrtui {_version_string()}",
        help="显示版本号后退出",
    )
    p.add_argument(
        "--root",
        metavar="PATH",
        help="项目根目录覆盖（默认当前目录；realpath 归一化后派生会话身份，"
             "侧栏计数与 pane 起始 cwd 均以此为准）",
    )
    p.add_argument(
        "--shell",
        metavar="PATH",
        help="pane 的 shell（默认 $SHELL，退化到 /bin/zsh）；"
             "对已存在会话只影响之后新建的 pane",
    )
    p.add_argument(
        "--scrollback",
        type=int,
        metavar="N",
        help=f"历史深度 history-limit（默认 {DEFAULT_HISTORY_LIMIT}，"
              f"范围 1..{MAX_SCROLLBACK}）；对已存在会话只影响之后新建的 window",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    argv = sys.argv[1:] if argv is None else list(argv)

    # 0. 子命令预分发（计划 §3.5）：只认 argv[1] 恰为这些字面量。
    #    `myrtui --root ls` 的 argv[1] 是 `--root` → 不会误分发。
    if argv and argv[0] in SUBCOMMANDS:
        from myrtui import instances

        return instances.run("ls" if argv[0] == "list" else argv[0], argv[1:])

    args = build_parser().parse_args(argv)

    # 1. 启动参数校验（全部在抢锁/连 tmux 之前，fail fast）
    if args.root is not None and not os.path.isdir(args.root):
        print(f"myrtui: --root 不是目录：{args.root}", file=sys.stderr)
        return 1
    shell: str | None = None
    if args.shell is not None:
        shell = os.path.realpath(args.shell)
        if not os.path.isfile(shell) or not os.access(shell, os.X_OK):
            print(
                f"myrtui: --shell 不是可执行文件：{args.shell}", file=sys.stderr
            )
            return 1
    if args.scrollback is not None and not (
        1 <= args.scrollback <= MAX_SCROLLBACK
    ):
        print(
            f"myrtui: --scrollback 必须在 1..{MAX_SCROLLBACK}（收到 "
            f"{args.scrollback}）",
            file=sys.stderr,
        )
        return 1

    # 2. 身份派生：--root 与 cwd 同一路径（M19：一律 realpath）
    root, digest = derive_identity(args.root)
    socket_name = f"{APP_NAME}-{digest}"
    session_name = socket_name

    try:
        check_tmux()
    except (TmuxNotFoundError, TmuxError) as exc:
        print(f"myrtui: {exc}", file=sys.stderr)
        return 1

    lock_path = lock_path_for(digest)
    takeover_path = takeover_path_for(digest)
    try:
        lock_fd = acquire_lock_with_takeover(lock_path, takeover_path, root=root)
    except TimeoutError:
        # §9：P1 接管轮询超时后提示手动处理并退出
        print(
            f"myrtui: 该目录已有 myrtui 实例在运行（{root}），"
            f"且 {TAKEOVER_TIMEOUT:.0f}s 内未让位；请手动结束旧实例后重试。",
            file=sys.stderr,
        )
        return 1

    backend = TmuxBackend(socket_name, session_name)
    try:
        if not backend.has_session():
            cols, rows = shutil.get_terminal_size()
            backend.create_session(
                root,
                cols,
                rows,
                shell=shell,
                history_limit=args.scrollback,
            )
        else:
            # v0.31：老会话没有 set-clipboard on，启动时补一次（分页器复制兜底）
            backend.enable_clipboard_capture()
            if shell is not None or args.scrollback is not None:
                # 会话已存在：tmux 在创建时快照这两个选项 → 只对之后新建的
                # pane/window 生效（respawn 重跑的也是建窗时记录的命令）
                backend.apply_session_options(
                    shell=shell, history_limit=args.scrollback
                )
                print(
                    "myrtui: --shell / --scrollback 对已存在的会话只影响"
                    "之后新建的 pane（tmux 在创建时快照选项）。",
                    file=sys.stderr,
                )
    except TmuxError as exc:
        print(f"myrtui: {exc}", file=sys.stderr)
        os.close(lock_fd)
        return 1

    # v0.37：位图能力必须在 Textual 接管 stdin **之前**探测（textual-image
    # 要读 DA1 回复）；缺依赖 / 非 tty / 探测异常都只是「无位图」。
    from myrtui.client.graphics import describe, detect_silent

    graphics = detect_silent()
    log.debug("terminal graphics: %s", describe(graphics))

    from myrtui.client.app import TerminalApp

    app = TerminalApp(
        backend,
        root,
        takeover_path=takeover_path,
        stop_path=stop_path_for(digest),
        layout_path=layout_path_for(digest),
        graphics=graphics,
    )
    try:
        app.run()
    finally:
        # 前端退出（含接管让位）= detach；tmux 会话与 shell 继续存活（§3.1）。
        # 锁文件不删除（永不 unlink），fd 随进程退出由内核释放。
        os.close(lock_fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
