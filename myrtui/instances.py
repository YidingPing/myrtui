"""实例管理子命令：``myrtui ls`` / ``attach`` / ``stop``（及 ``list`` 别名）。

计划：``docs-internal/docs/DISTRIBUTION-INSTANCES-PLAN.md`` §3（已合入评审 R1–R6 / M1–M6）。
由 ``cli.main`` 按 ``argv[1]`` 字面量预分发进入本模块（cli.SUBCOMMANDS）。

关键约定（逐条对应评审）：

- 扫描 ``user_data_dir/*.lock``，三态判定：``running``（flock 被持 = 前台）、
  ``detached``（锁空闲但 tmux 会话存活）、``dead``（仅残留锁文件）。
  flock 判定走 ``cli.probe_lock_held`` 专用路径，**不写 holder record**
  （评审 M6/R3）；
- 排序键 ``(root, hash)``、**序号只在存活集内分配**（评审 R1 + R4 拍板）；
  ``attach``/``stop`` 不缓存 ``ls`` 结果，重新扫描后按同一规则解析；
  dead 行默认不显示（``--all`` 才显示，且不占序号）；
- ``stop`` 用独立的 ``<hash>.stop`` 标记（前缀 ``stop``，评审 R6）；
  等前台干净退场后**拿锁前先记录、拿锁后 kill 前重验**（标记仍为自己、
  holder record 的 pid/root 未变），防与等锁 attach 的竞态（评审 R2）；
  拿锁用 ``write_record=False`` 变体，不覆写记录（评审 R3）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass

from myrtui import cli
from myrtui.i18n import t
from myrtui.tmux_backend import (
    TmuxBackend,
    TmuxError,
    TmuxNotFoundError,
    check_tmux,
)

# ── stop 参数 ───────────────────────────────────────────────────────
STOP_WAIT_RUNNING = 3.0  # 前台在场时等它干净退场的总时长
STOP_WAIT_FREE = 1.0  # 扫描时锁已空闲（detached）的兜底等锁时长
STOP_POLL = 0.05  # 等锁轮询间隔

# ── 状态 ────────────────────────────────────────────────────────────

STATE_RUNNING = "running"
STATE_DETACHED = "detached"
STATE_DEAD = "dead"


@dataclass
class Instance:
    digest: str
    root: str | None  # holder record 第二行；旧格式锁为 None
    pid: int | None  # holder record 第一行（running = 当前前台，其余为上次持有者）
    running: bool  # flock 被持
    session: bool  # tmux 会话存活

    @property
    def state(self) -> str:
        if self.running:
            return STATE_RUNNING
        if self.session:
            return STATE_DETACHED
        return STATE_DEAD

    @property
    def alive(self) -> bool:
        return self.state != STATE_DEAD

    @property
    def socket_name(self) -> str:
        return f"{cli.APP_NAME}-{self.digest}"


def _has_session(digest: str) -> bool:
    name = f"{cli.APP_NAME}-{digest}"
    return TmuxBackend(name, name).has_session()


def scan_instances() -> list[Instance]:
    """扫描 ``<data>/*.lock``，按 ``(root, hash)`` 固定排序返回全部实例。

    排序规则（评审 R1）：root 已知按 root 字典序在前，root 缺失（旧格式锁）
    按 hash 排在后；tiebreaker 一律 hash。序号分配在此之后只取存活集。
    """
    data_dir = cli._data_dir()
    try:
        names = os.listdir(data_dir)
    except FileNotFoundError:
        return []
    out: list[Instance] = []
    for name in names:
        if not name.endswith(".lock"):
            continue
        digest = name[: -len(".lock")]
        if not digest:
            continue
        path = os.path.join(data_dir, name)
        pid, root = cli.read_holder_record(path)
        out.append(
            Instance(
                digest=digest,
                root=root,
                pid=pid,
                running=cli.probe_lock_held(path),
                session=_has_session(digest),
            )
        )
    out.sort(key=lambda i: (i.root is None, i.root or "", i.digest))
    return out


# ── 目标解析（attach / stop 共用，评审 R1：总是重新扫描） ──────────


def _resolve(
    spec: str, insts: list[Instance], alive: list[Instance]
) -> tuple[Instance | None, str | None, int]:
    """解析目标 → ``(实例, 错误文案, 退出码)``；成功时错误为 ``(inst, None, 0)``。

    - 纯数字 → 存活集序号（1 起；纯数字一律按序号解释，路径请带 `/`）；
    - 是目录 → realpath 派生 hash 精确匹配；
    - 其余 → hash 前缀匹配（不区分大小写）；
    - 命中但已 dead → 「无存活实例」（评审 M1）；多命中 → 歧义（退出 2）。
    """
    not_found = t("inst.not_found", spec=spec)
    dead_msg = t("inst.dead", spec=spec)

    if spec.isdigit():
        idx = int(spec)
        if idx < 1 or idx > len(alive):
            span = f"1..{len(alive)}" if alive else t("inst.no_alive_short")
            return None, t("inst.out_of_range", idx=idx, span=span), 2
        return alive[idx - 1], None, 0

    if os.path.isdir(spec):
        root, digest = cli.derive_identity(spec)
        m = [i for i in insts if i.digest == digest]
        if not m:
            return None, t("inst.no_record", root=root), 2
        if not m[0].alive:
            return None, t("inst.dead", spec=root), 2
        return m[0], None, 0

    pre = spec.lower()
    m = [i for i in insts if i.digest.startswith(pre)]
    if not m:
        return None, not_found, 2
    a = [i for i in m if i.alive]
    if not a:
        return None, dead_msg, 2
    if len(a) > 1:
        digests = ", ".join(i.digest for i in a)
        return (
            None,
            t("inst.ambiguous", digests=digests),
            2,
        )
    return a[0], None, 0


# ── 输出 ────────────────────────────────────────────────────────────


def _row(index: int | None, inst: Instance) -> dict:
    """表格 / JSON 共用的一行（字段契约见计划 §3.2 / 评审 M2）。

    ``index``：存活集序号（dead 行为 null，不占序号）；
    ``pid``：仅 running 显示当前前台 pid，其余状态为 null（避免 stale pid 误导）。
    """
    return {
        "index": index,
        "state": inst.state,
        "root": inst.root,
        "short_hash": inst.digest,
        "pid": inst.pid if inst.state == STATE_RUNNING else None,
    }


def _print_table(rows: list[dict]) -> None:
    print(f"{'#':>3}  {'STATE':<8} {'PID':>7}  PROJECT-ROOT  HASH")
    for r in rows:
        idx = str(r["index"]) if r["index"] is not None else "-"
        pid = str(r["pid"]) if r["pid"] is not None else "-"
        root = r["root"] or t("inst.old_record")
        print(f"{idx:>3}  {r['state']:<8} {pid:>7}  {root}  {r['short_hash']}")


def _alive_rows(alive: list[Instance]) -> list[dict]:
    return [_row(n, inst) for n, inst in enumerate(alive, start=1)]


def _check_tmux_or_exit() -> int:
    try:
        check_tmux()
    except (TmuxNotFoundError, TmuxError) as exc:
        print(f"myrtui: {exc}", file=sys.stderr)
        return 1
    return 0


# ── ls ──────────────────────────────────────────────────────────────


def cmd_ls(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="myrtui ls",
        description=t("inst.ls_desc"),
    )
    p.add_argument(
        "--all",
        action="store_true",
        help=t("inst.ls_all_help"),
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="json_out",
        help=t("inst.ls_json_help"),
    )
    a = p.parse_args(argv)

    rc = _check_tmux_or_exit()
    if rc:
        return rc

    insts = scan_instances()
    alive = [i for i in insts if i.alive]
    alive_index = {i.digest: n for n, i in enumerate(alive, start=1)}

    shown = insts if a.all else alive
    rows = [_row(alive_index.get(i.digest) if i.alive else None, i)
            for i in shown]

    if a.json_out:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        if insts and not alive:
            print(t("inst.ls_empty_dead", n=len(insts)))
        elif insts:
            print(t("inst.ls_empty"))
        else:
            print(t("inst.ls_none"))
        return 0
    _print_table(rows)
    return 0


# ── attach ──────────────────────────────────────────────────────────


def cmd_attach(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="myrtui attach",
        description=t("inst.attach_desc"),
        epilog=t("inst.target_epilog"),
    )
    p.add_argument("target", nargs="?", help=t("inst.target_metavar"))
    a = p.parse_args(argv)

    rc = _check_tmux_or_exit()
    if rc:
        return rc

    insts = scan_instances()
    alive = [i for i in insts if i.alive]

    if a.target is None:
        if not alive:
            print(t("inst.no_alive"), file=sys.stderr)
            return 2
        if len(alive) > 1:
            _print_table(_alive_rows(alive))
            print(
                t("inst.multiple"),
                file=sys.stderr,
            )
            return 2
        chosen = alive[0]
    else:
        chosen, err, code = _resolve(a.target, insts, alive)
        if err is not None:
            print(err, file=sys.stderr)
            return code

    assert chosen is not None
    if chosen.root is None:
        # 旧格式锁（无 root 行）：attach 需要项目路径，stop 不需要
        print(
            t("inst.old_lock", digest=chosen.digest),
            file=sys.stderr,
        )
        return 2
    # 等价 `myrtui --root <root>`：锁竞争交给现有接管协议（计划 §3.3）
    return cli.main(["--root", chosen.root])


# ── stop ────────────────────────────────────────────────────────────


def _drop_own_stop(path: str) -> None:
    """清掉自己的 stop 标记（别人的不碰，口径同 cli._drop_own_marker）。"""
    if any(
        pid == os.getpid()
        for pid, _ in cli.read_takeover_markers(path, prefix="stop")
    ):
        cli.clear_takeover(path)


def _stop_marker_is_ours(path: str) -> bool:
    now = time.time()
    return any(
        pid == os.getpid() and cli.is_marker_valid(pid, ts, now=now)
        for pid, ts in cli.read_takeover_markers(path, prefix="stop")
    )


def _record_unchanged(lock_path: str, pid: int | None, root: str | None) -> bool:
    """评审 R2 重验：holder record 与 scan 时观察到的 pid/root 一致。

    任一侧观察值为 None（旧格式 / 读取失败）则该项跳过比对；
    pid 变化 = 有新前台接手并覆写了记录 → 必须放弃 kill。
    """
    cur_pid, cur_root = cli.read_holder_record(lock_path)
    if pid is not None and cur_pid != pid:
        return False
    if root is not None and cur_root != root:
        return False
    return True


def _stop_instance(inst: Instance) -> int:
    lock_path = os.path.join(cli._data_dir(), f"{inst.digest}.lock")
    stop_path = cli.stop_path_for(inst.digest)
    # 重验基线：scan 时观察到的记录（pid 变了 = 新前台接手）
    base_pid, base_root = inst.pid, inst.root

    # 1) 写 stop 标记（fresh；两级校验由 is_marker_valid 把关，M15/M21）
    cli.write_takeover_marker(stop_path, prefix="stop")
    wait = STOP_WAIT_RUNNING if inst.running else STOP_WAIT_FREE
    fd: int | None = None
    try:
        # 2) 等前台干净退场（哨戒读标记 → self.exit() → 放锁）；
        #    detached 时首轮即拿到，超时窗口相应缩短
        deadline = time.monotonic() + wait
        while True:
            try:
                # write_record=False：stop 拿锁只为 kill，不落脏记录（评审 R3）
                fd = cli.acquire_lock(lock_path, write_record=False)
                break
            except BlockingIOError:
                pass
            if time.monotonic() >= deadline:
                print(
                    t("inst.stop_timeout", wait=wait, digest=inst.digest),
                    file=sys.stderr,
                )
                _drop_own_stop(stop_path)
                return 1
            time.sleep(STOP_POLL)

        # 3) kill 前重验（评审 R2）：标记仍属于自己 + 记录未被新前台改写
        if not _stop_marker_is_ours(stop_path) or not _record_unchanged(
            lock_path, base_pid, base_root
        ):
            print(
                t("inst.stop_taken_over"),
                file=sys.stderr,
            )
            _drop_own_stop(stop_path)
            return 1

        # 4) 结束会话（kill-server 对不存在的 socket 静默失败）
        backend = TmuxBackend(inst.socket_name, inst.socket_name)
        backend.kill_server()
        _drop_own_stop(stop_path)
        return 0
    finally:
        if fd is not None:
            os.close(fd)  # 永不 unlink 锁文件（M14/M17）；fd 随释放退场


def cmd_stop(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="myrtui stop",
        description=t("inst.stop_desc"),
        epilog=t("inst.target_epilog_stop"),
    )
    p.add_argument("target", help=t("inst.target_required"))
    a = p.parse_args(argv)

    rc = _check_tmux_or_exit()
    if rc:
        return rc

    insts = scan_instances()
    alive = [i for i in insts if i.alive]
    chosen, err, code = _resolve(a.target, insts, alive)
    if err is not None:
        print(err, file=sys.stderr)
        return code

    assert chosen is not None
    return _stop_instance(chosen)


# ── 分发入口（cli.main 调用） ───────────────────────────────────────


def run(cmd: str, argv: list[str]) -> int:
    if cmd == "ls":
        return cmd_ls(argv)
    if cmd == "attach":
        return cmd_attach(argv)
    if cmd == "stop":
        return cmd_stop(argv)
    raise ValueError(t("inst.unknown_cmd", cmd=cmd))
