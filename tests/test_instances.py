"""实例管理测试：``myrtui ls`` / ``attach`` / ``stop``（计划 §3）。

覆盖评审 R1–R6 与 M1–M6 的对应测试面：
排序确定性 / flock probe 不写 record / stop 标记前缀隔离 / stop 竞态重验 /
``write_record=False`` / 子命令预分发边界（含 ``myrtui --root ls``）。

后端测试跑在隔离 tmux 实例上（临时 socket），结束 kill-server；
所有涉及锁/记录的用例都把 ``cli._data_dir`` 指到 tmp_path（hermetic）。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import time

import pytest

from myrtui import cli, instances
from myrtui.tmux_backend import TmuxBackend

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux 未安装"
)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """把状态目录指到测试专属 tmp（锁 / 标记 / 布局全部隔离）。"""
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(cli, "_data_dir", lambda: str(d))
    return d


def _make_lock(digest: str, pid: int | None, root: str | None) -> str:
    """手工落一个锁文件记录（不持锁 = 前台空闲）。"""
    path = os.path.join(cli._data_dir(), f"{digest}.lock")
    lines = ([] if pid is None else [str(pid)]) + ([] if root is None else [root])
    with open(path, "w") as f:
        f.write(("\n".join(lines) + "\n") if lines else "")
    return path


def _tmux_or_skip() -> None:
    if shutil.which("tmux") is None:
        pytest.skip("tmux 未安装")


def _session_backend(digest: str) -> TmuxBackend:
    name = f"myrtui-{digest}"
    return TmuxBackend(name, name)


# ---------- holder record（§3.1） ----------

def test_holder_record_two_line_roundtrip(data_dir):
    p = str(data_dir / "h.lock")
    fd = cli.acquire_lock(p, root="/some/proj")
    try:
        pid, root = cli.read_holder_record(p)
        assert pid == os.getpid()
        assert root == "/some/proj"
    finally:
        os.close(fd)


def test_holder_record_legacy_single_line(data_dir):
    """旧格式（单行 pid）读取兼容 → (pid, None)（评审 §3.1）。"""
    p = str(data_dir / "legacy.lock")
    with open(p, "w") as f:
        f.write("4242\n")
    assert cli.read_holder_record(p) == (4242, None)


def test_holder_record_garbage_tolerated(data_dir):
    p = str(data_dir / "garbage.lock")
    with open(p, "w") as f:
        f.write("not-a-pid\n")
    assert cli.read_holder_record(p) == (None, None)
    assert cli.read_holder_record(str(data_dir / "missing.lock")) == (None, None)


def test_acquire_lock_without_root_keeps_legacy_format(data_dir):
    """不传 root → 单行格式（既有测试零改动的兼容前提）。"""
    p = str(data_dir / "n.lock")
    fd = cli.acquire_lock(p)
    try:
        with open(p) as f:
            assert f.read().strip() == str(os.getpid())
    finally:
        os.close(fd)


# ---------- flock probe（评审 M6/R3） ----------

def test_probe_free_and_held(data_dir):
    p = str(data_dir / "probe.lock")
    fd = cli.acquire_lock(p, root="/proj")
    try:
        assert cli.probe_lock_held(p) is True
    finally:
        os.close(fd)
    assert cli.probe_lock_held(p) is False


def test_probe_does_not_dirty_record(data_dir):
    """probe 只判不写：试探后 holder record 保持原样（评审 M6）。"""
    p = str(data_dir / "keep.lock")
    fd = cli.acquire_lock(p, root="/proj")
    os.close(fd)  # 放锁，记录留 pid+root
    before = open(p).read()
    assert cli.probe_lock_held(p) is False
    assert open(p).read() == before


def test_acquire_lock_write_record_false(data_dir):
    """stop 路径的锁获取变体：拿到锁但不覆写记录（评审 R3）。"""
    p = str(data_dir / "norec.lock")
    with open(p, "w") as f:
        f.write("777\n/old/root\n")
    fd = cli.acquire_lock(p, write_record=False)
    try:
        with open(p) as f:
            assert f.read() == "777\n/old/root\n"
    finally:
        os.close(fd)


# ---------- 标记前缀（评审 R6） ----------

def test_stop_marker_prefix_isolation(tmp_path):
    """takeover 与 stop 各走各的前缀：读 stop 不得命中 takeover 行。"""
    p = str(tmp_path / "m.stop")
    cli.write_takeover_marker(p, pid=111, ts=1000.0, prefix="stop")
    cli.write_takeover_marker(f"{tmp_path}/m.takeover", pid=222, ts=2000.0)
    assert cli.read_takeover_markers(p, prefix="stop") == [(111, 1000.0)]
    assert cli.read_takeover_markers(p) == []  # 默认前缀读不到 stop 行
    tk = str(tmp_path / "m.takeover")
    assert cli.read_takeover_markers(tk) == [(222, 2000.0)]
    assert cli.read_takeover_markers(tk, prefix="stop") == []


def test_stop_marker_garbage_tolerated(tmp_path):
    p = str(tmp_path / "g.stop")
    with open(p, "w") as f:
        f.write("stop:not-a-pid:xyz\nstop:7:1000\nnoise\n")
    assert cli.read_takeover_markers(p, prefix="stop") == [(7, 1000)]


# ---------- 排序与扫描（评审 R1 / §3.2） ----------

def test_scan_sorting_root_then_hash(data_dir):
    """排序键 (root, hash)：root 已知按字典序在前，缺失按 hash 排后。"""
    _make_lock("bbbb000000000000", 1, "/z/root")
    _make_lock("aaaa000000000000", 2, "/a/root")
    _make_lock("cccc000000000000", 3, None)  # 旧格式 → 按 hash 排在后面
    _make_lock("aaaa111111111111", 4, "/a/root")
    insts = instances.scan_instances()
    assert [i.digest for i in insts] == [
        "aaaa000000000000",  # /a/root 字典序最小
        "aaaa111111111111",  # 同 root → hash tiebreak
        "bbbb000000000000",  # /z/root
        "cccc000000000000",  # root 缺失 → 最后
    ]


def test_scan_states_running_detached_dead(data_dir, tmp_path):
    """三态：持锁 = running；空闲+会话 = detached；空闲无会话 = dead。"""
    # running：本进程持锁（同进程不同 fd 也互斥，见 test_p1 同款用法）
    run_root = str(tmp_path / "run-proj")
    os.makedirs(run_root)
    _, run_digest = cli.derive_identity(run_root)
    run_lock = str(data_dir / f"{run_digest}.lock")
    holder = cli.acquire_lock(run_lock, root=run_root)

    # detached：锁空闲但有真实 tmux 会话
    det_root = str(tmp_path / "det-proj")
    os.makedirs(det_root)
    _, det_digest = cli.derive_identity(det_root)
    _make_lock(det_digest, 4242, det_root)
    backend = _session_backend(det_digest)
    backend.create_session(det_root, 80, 24)

    # dead：锁空闲、无会话
    _make_lock("dead000000000000", 111, "/gone/proj")

    try:
        by_digest = {i.digest: i for i in instances.scan_instances()}
        assert by_digest[run_digest].state == instances.STATE_RUNNING
        assert by_digest[run_digest].pid == os.getpid()
        assert by_digest[det_digest].state == instances.STATE_DETACHED
        assert by_digest["dead000000000000"].state == instances.STATE_DEAD
    finally:
        os.close(holder)
        backend.kill_server()
        time.sleep(0.2)


# ---------- ls（§3.2 / 评审 R4 拍板） ----------

def test_ls_default_hides_dead_and_all_shows(data_dir, capsys):
    _make_lock("dead000000000000", 111, "/gone")
    _make_lock("ffff000000000000", 222, "/also-gone")
    assert instances.cmd_ls([]) == 0
    out = capsys.readouterr().out
    assert "dead000000000000" not in out and "ffff000000000000" not in out, \
        "默认不应显示 dead 行"
    assert instances.cmd_ls(["--all"]) == 0
    out = capsys.readouterr().out
    assert "dead000000000000" in out and "ffff000000000000" in out


def test_ls_json_fields_and_index(data_dir, capsys):
    """JSON 字段契约（评审 M2）+ dead 行 index 为 null 不占序号（R1/R4）。"""
    _make_lock("aaa0000000000000", 111, "/p/b")
    _make_lock("aaa0000000000001", 222, "/p/c")
    _make_lock("dead000000000000", 333, "/p/a-dead")
    assert instances.cmd_ls(["--json", "--all"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 3
    for r in rows:
        assert set(r) == {"index", "state", "root", "short_hash", "pid"}
    # 三个锁都空闲且无会话 → 全 dead：不占序号、pid 为 null
    assert all(r["state"] == "dead" for r in rows)
    assert all(r["index"] is None for r in rows)
    assert all(r["pid"] is None for r in rows)


def test_ls_empty_output(data_dir, capsys):
    assert instances.cmd_ls([]) == 0
    assert "无 myrtui 实例" in capsys.readouterr().out
    _make_lock("dead000000000000", 111, "/gone")
    assert instances.cmd_ls([]) == 0
    out = capsys.readouterr().out
    assert "无存活实例" in out and "--all" in out


def test_ls_numbering_only_alive(data_dir, capsys):
    """序号只在存活集内分配（评审 R1 + 拍板）：running 占 1，dead 无号。"""
    _make_lock("dead000000000000", 111, "/a-dead")
    root = "/p/live"
    _make_lock("fff0000000000000", 222, root)
    # 给 fff0000000000000 一个会话 → detached 存活
    backend = _session_backend("fff0000000000000")
    backend.create_session(str(data_dir), 80, 24)
    try:
        assert instances.cmd_ls(["--all"]) == 0
        out = capsys.readouterr().out
        for line in out.splitlines():
            if "dead000000000000" in line:
                assert line.split()[0] == "-", "dead 行不占序号"
            if "fff0000000000000" in line:
                assert line.split()[0] == "1"
    finally:
        backend.kill_server()
        time.sleep(0.2)


# ---------- 目标解析（评审 R1/M1） ----------

def _two_alive(data_dir, tmp_path):
    """造两个存活实例，返回 (insts, alive, digests)。"""
    digests = []
    for n in (1, 2):
        root = str(tmp_path / f"proj{n}")
        os.makedirs(root, exist_ok=True)
        _, d = cli.derive_identity(root)
        lock = str(data_dir / f"{d}.lock")
        fd = cli.acquire_lock(lock, root=root)
        os.close(fd)  # 放锁，记录留 pid+root；再挂个会话 → detached 存活
        b = _session_backend(d)
        b.create_session(root, 80, 24)
        digests.append((d, root, b))
    insts = instances.scan_instances()
    alive = [i for i in insts if i.alive]
    return insts, alive, digests


def test_resolve_index_and_range(data_dir, tmp_path):
    insts, alive, digests = _two_alive(data_dir, tmp_path)
    got, err, code = instances._resolve("1", insts, alive)
    assert err is None and code == 0
    assert got.digest == alive[0].digest
    # 排序按 root 字典序 → proj1 在 proj2 前
    assert got.root.endswith("proj1")
    got2, err2, code2 = instances._resolve("2", insts, alive)
    assert err2 is None and got2.digest == alive[1].digest
    _, err3, code3 = instances._resolve("3", insts, alive)
    assert code3 == 2 and "超出范围" in err3
    for _, _, b in digests:
        b.kill_server()
    time.sleep(0.2)


def test_resolve_hash_prefix_ambiguous_and_missing(data_dir, tmp_path):
    # 两个 fake 实例都挂上会话 → 存活，才谈得上歧义（评审 R1：歧义只看存活集）
    roots = []
    for d in ("abcdef0123456789", "abcdef9999999999"):
        r = str(tmp_path / d)
        os.makedirs(r, exist_ok=True)
        _make_lock(d, 111, r)
        b = _session_backend(d)
        b.create_session(r, 80, 24)
        roots.append(b)
    try:
        insts = instances.scan_instances()
        _, err, code = instances._resolve("abcdef", insts, [])
        assert code == 2 and "多个实例" in err
        # 唯一前缀 → 命中存活实例
        got, err, code = instances._resolve("abcdef0123", insts, [])
        assert err is None and code == 0 and got.digest == "abcdef0123456789"
    finally:
        for b in roots:
            b.kill_server()
        time.sleep(0.2)

    # dead 命中 → 无存活实例（评审 M1）
    _make_lock("abcdef0123456789", 111, str(tmp_path / "abcdef0123456789"))
    insts = instances.scan_instances()
    got, err, code = instances._resolve("abcdef0123", insts, [])
    assert got is None and code == 2 and "无存活实例" in err
    # 完全不命中
    _, err, code = instances._resolve("zzzz", insts, [])
    assert code == 2 and "未找到" in err


def test_resolve_path_target(data_dir, tmp_path):
    root = str(tmp_path / "proj")
    os.makedirs(root)
    _, d = cli.derive_identity(root)
    _make_lock(d, 111, root)  # dead（无会话）
    insts = instances.scan_instances()
    # dead 路径目标 → 无存活实例（评审 M1）
    got, err, code = instances._resolve(root, insts, [])
    assert got is None and code == 2 and "无存活实例" in err
    # 从未运行过的目录 → 无记录
    fresh = str(tmp_path / "fresh")
    os.makedirs(fresh)
    _, err, code = instances._resolve(fresh, insts, [])
    assert code == 2 and "没有 myrtui 实例记录" in err


# ---------- attach（§3.3） ----------

def test_attach_no_arg_zero_alive(data_dir, capsys):
    assert instances.cmd_attach([]) == 2
    assert "无存活实例" in capsys.readouterr().err


def test_attach_no_arg_multiple_alive_prints_table(data_dir, tmp_path, capsys):
    _, alive, digests = _two_alive(data_dir, tmp_path)
    assert len(alive) == 2
    assert instances.cmd_attach([]) == 2
    cap = capsys.readouterr()
    assert "多个存活实例" in cap.err
    assert any(d in cap.out for d, _, _ in digests), "应打印候选列表"
    for _, _, b in digests:
        b.kill_server()
    time.sleep(0.2)


def test_attach_delegates_to_main_with_root(data_dir, tmp_path, monkeypatch):
    """单存活实例（或解析出的目标）→ 等价 cli.main(["--root", root])。"""
    _, alive, digests = _two_alive(data_dir, tmp_path)
    try:
        calls = []
        monkeypatch.setattr(cli, "main", lambda argv: calls.append(argv) or 7)
        assert instances.cmd_attach([]) == 2  # 两个存活 → 需指定目标
        assert instances.cmd_attach([alive[1].root]) == 7
        assert calls == [["--root", alive[1].root]]
        assert instances.cmd_attach([alive[1].digest]) == 7
        assert calls[-1] == ["--root", alive[1].root]
    finally:
        for _, _, b in digests:
            b.kill_server()
        time.sleep(0.2)


def test_attach_legacy_record_without_root(data_dir, capsys):
    """旧格式锁（无 root 行）但会话存活 → 无法 attach，提示 stop（§3.3）。"""
    _make_lock("legac000000000000", 111, None)
    b = _session_backend("legac000000000000")
    b.create_session(str(data_dir), 80, 24)
    try:
        assert instances.cmd_attach(["legac000000000000"]) == 2
        err = capsys.readouterr().err
        assert "缺少项目路径" in err and "stop" in err
    finally:
        b.kill_server()
        time.sleep(0.2)


# ---------- stop（§3.4 / 评审 R2/R3） ----------

def test_stop_dead_target_exits_2(data_dir, capsys):
    _make_lock("dead000000000000", 111, "/gone")
    assert instances.cmd_stop(["dead000000000000"]) == 2
    assert "无存活实例" in capsys.readouterr().err


def test_stop_requires_target(data_dir):
    with pytest.raises(SystemExit) as e:
        instances.cmd_stop([])
    assert e.value.code == 2  # argparse 用法错误口径


def test_stop_running_frontend_protocol(data_dir, tmp_path, monkeypatch):
    """完整协议：写 stop 标记 → 前端哨戒让位 → 重验 → kill 会话。

    且不覆写 holder record（R3）、清理自己的标记。
    """
    root = str(tmp_path / "proj")
    os.makedirs(root)
    _, digest = cli.derive_identity(root)
    lock = str(data_dir / f"{digest}.lock")
    stop_path = cli.stop_path_for(digest)

    backend = _session_backend(digest)
    backend.create_session(root, 80, 24)
    holder = cli.acquire_lock(lock, root=root)  # 模拟运行中的前端（record = 本进程）

    # 模拟前端哨戒（与 TerminalApp._check_takeover 同一逻辑）
    yielded = threading.Event()

    def sentry():
        while not yielded.is_set():
            for pid, ts in cli.read_takeover_markers(stop_path, prefix="stop"):
                if cli.is_marker_valid(pid, ts):
                    os.close(holder)  # 干净退场 = 放锁
                    yielded.set()
                    return
            time.sleep(0.05)

    th = threading.Thread(target=sentry, daemon=True)
    th.start()
    try:
        rc = instances.cmd_stop([digest])
        assert rc == 0, "前台让位 + 重验通过 → stop 成功"
        assert yielded.is_set()
        assert not backend.has_session(), "stop 应结束 tmux 会话"
        assert cli.read_takeover_markers(stop_path, prefix="stop") == [], \
            "成功路径必须清理自己的 stop 标记"
        # R3：record 未被 stop 进程覆写
        pid, r = cli.read_holder_record(lock)
        assert pid == os.getpid() and r == root
    finally:
        try:
            os.close(holder)
        except OSError:
            pass
        backend.kill_server()
        time.sleep(0.2)


def test_stop_detached_kills_session(data_dir, tmp_path):
    """detached（无前台）：直接拿锁 → kill 会话，无需等退场。"""
    root = str(tmp_path / "proj")
    os.makedirs(root)
    _, digest = cli.derive_identity(root)
    _make_lock(digest, 4242, root)
    backend = _session_backend(digest)
    backend.create_session(root, 80, 24)
    try:
        rc = instances.cmd_stop([digest])
        assert rc == 0
        assert not backend.has_session()
        # record 仍是原来的 4242（未被 stop 覆写）
        pid, r = cli.read_holder_record(str(data_dir / f"{digest}.lock"))
        assert pid == 4242 and r == root
    finally:
        backend.kill_server()
        time.sleep(0.2)


def test_stop_reverify_aborts_on_record_change(
    data_dir, tmp_path, capsys
):
    """评审 R2：重验发现记录 pid 与 scan 基线不符（新前台接手）→ 放弃 kill。"""
    root = str(tmp_path / "proj")
    os.makedirs(root)
    _, digest = cli.derive_identity(root)
    _make_lock(digest, os.getpid(), root)  # 当前记录 = 本进程
    # doctored 基线：scan 时看到的是另一个 pid（模拟接手竞态）
    inst = instances.Instance(
        digest=digest,
        root=root,
        pid=os.getpid() + 999_999,
        running=False,
        session=False,
    )
    rc = instances._stop_instance(inst)
    assert rc == 1
    assert "放弃 stop" in capsys.readouterr().err
    stop_path = cli.stop_path_for(digest)
    assert cli.read_takeover_markers(stop_path, prefix="stop") == [], \
        "放弃路径必须清理自己的标记"
    # 记录原样
    pid, r = cli.read_holder_record(str(data_dir / f"{digest}.lock"))
    assert pid == os.getpid() and r == root


def test_stop_timeout_cleans_marker(data_dir, tmp_path, monkeypatch):
    """前台不让位（哨戒不跑）→ 等锁超时 → 退出 1 且清掉自己的标记。"""
    monkeypatch.setattr(instances, "STOP_WAIT_RUNNING", 0.3)
    monkeypatch.setattr(instances, "STOP_POLL", 0.05)
    root = str(tmp_path / "proj")
    os.makedirs(root)
    _, digest = cli.derive_identity(root)
    lock = str(data_dir / f"{digest}.lock")
    holder = cli.acquire_lock(lock, root=root)
    try:
        rc = instances.cmd_stop([digest])
        assert rc == 1
        stop_path = cli.stop_path_for(digest)
        assert cli.read_takeover_markers(stop_path, prefix="stop") == []
    finally:
        os.close(holder)


# ---------- 前端哨戒读 stop 标记（§3.4 步骤 2） ----------

def test_app_sentry_exits_on_stop_marker(tmp_path):
    """持锁 App 收到有效 stop 标记 → 干净退出（会话无损，kill 由 stop 侧做）。"""
    name = f"myrtui-sentry-stop-{os.getpid()}"
    backend = TmuxBackend(name, name)
    proj = str(tmp_path / "p")
    os.makedirs(proj)
    backend.create_session(proj, 80, 24)
    sp = str(tmp_path / "s.stop")

    from myrtui.client.app import TerminalApp

    async def scenario():
        app = TerminalApp(backend, proj, stop_path=sp)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.6)
            assert not app._exit
            cli.write_takeover_marker(sp, pid=os.getpid(), ts=time.time(),
                                      prefix="stop")
            for _ in range(40):
                await pilot.pause(0.05)
                if app._exit:
                    break
            assert app._exit, "哨戒应在 2s 内看到有效 stop 标记并退场"
    asyncio.run(scenario())
    assert backend.has_session(), "stop 标记本身不得杀会话（由 stop 侧 kill）"
    backend.kill_server()
    time.sleep(0.2)


def test_app_sentry_ignores_stale_stop_marker(tmp_path):
    """过期 stop 标记 → 垃圾处理，不退场（M15 同一校验）。"""
    name = f"myrtui-sentry-stale-{os.getpid()}"
    backend = TmuxBackend(name, name)
    proj = str(tmp_path / "p")
    os.makedirs(proj)
    backend.create_session(proj, 80, 24)
    sp = str(tmp_path / "s.stop")

    from myrtui.client.app import TerminalApp

    async def scenario():
        app = TerminalApp(backend, proj, stop_path=sp)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.6)
            cli.write_takeover_marker(
                sp, pid=os.getpid(),
                ts=time.time() - cli.TAKEOVER_MAX_AGE - 5,
                prefix="stop",
            )
            await pilot.pause(1.5)
            assert not app._exit, "过期 stop 标记不应让位"
    asyncio.run(scenario())
    backend.kill_server()
    time.sleep(0.2)


# ---------- 子命令预分发（§3.5 / 评审 §4 红线） ----------

def test_main_dispatches_ls_and_list(monkeypatch):
    calls = []
    monkeypatch.setattr(
        instances, "run", lambda cmd, argv: calls.append((cmd, argv)) or 5
    )
    assert cli.main(["ls"]) == 5
    assert cli.main(["list", "--all"]) == 5
    assert cli.main(["stop", "abc"]) == 5
    assert calls == [("ls", []), ("ls", ["--all"]), ("stop", ["abc"])]


def test_main_does_not_dispatch_option_values(monkeypatch, tmp_path):
    """`myrtui --root ls`：argv[1] 是 `--root` → 走启动解析器，不误分发。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        instances, "run",
        lambda cmd, argv: pytest.fail("不得进入实例管理分发"),
    )
    rc = cli.main(["--root", "ls"])  # "ls" 不是目录 → 启动路径报错退出
    assert rc == 1


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as e:
        cli.build_parser().parse_args(["--version"])
    assert e.value.code == 0
    assert "myrtui" in capsys.readouterr().out
