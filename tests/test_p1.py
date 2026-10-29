"""myrtui P1 测试：接管交接（§6）、多 pane（§12）、git 状态（§5）。

后端测试跑在隔离 tmux 实例上（临时 socket），结束 kill-server。
接管的进程级端到端（两进程真实 flock 交接）单独在本文件末尾。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
import uuid

import pytest
from textual.widgets import Label

from myrtui import cli
from myrtui.gitstat import GitInfo, git_status, parse_git_status
from myrtui.tmux_backend import TmuxBackend
from myrtui.client.app import MAX_PANES, TerminalApp
from myrtui.client.pane_widget import PaneWidget
from myrtui.client.topbar import BRAND_TEXT, TopBar, TopBarButton, PaneTab


# ---------- git 状态解析（§5，纯函数） ----------

def test_parse_git_status_branch_and_dirty():
    out = "## main...origin/main [ahead 1]\n M a.py\n M b.py\n"
    info = parse_git_status(out)
    assert info == GitInfo(branch="main", dirty=2)


def test_parse_git_status_no_upstream():
    out = "## feature/x\n?? new.txt\n"
    info = parse_git_status(out)
    assert info == GitInfo(branch="feature/x", dirty=1)


def test_parse_git_status_detached_head():
    info = parse_git_status("## HEAD (no branch)\n")
    assert info == GitInfo(branch="HEAD", dirty=0)


def test_parse_git_status_garbage():
    assert parse_git_status("") is None
    assert parse_git_status("not a git output") is None


def test_parse_git_status_empty_repo_no_commits():
    """F1：空仓库两种形态（新/旧 git），分支名不得取成 "No"/"Initial"。"""
    new_git = "## No commits yet on main\n"
    assert parse_git_status(new_git) == GitInfo(branch="main", dirty=0)
    old_git = "## Initial commit on master\n M a.txt\n"
    assert parse_git_status(old_git) == GitInfo(branch="master", dirty=1)
    # 带斜杠分支的空仓库
    assert parse_git_status(
        "## No commits yet on feature/x\n"
    ) == GitInfo(branch="feature/x", dirty=0)


def test_git_status_non_repo(tmp_path):
    # 临时目录不在任何仓库内 → not-repo（侧栏显示 GIT -）
    status, info = git_status(str(tmp_path))
    assert status == "not-repo"
    assert info is None


def test_git_status_missing_git_binary(tmp_path):
    status, info = git_status(str(tmp_path), git_bin="/nonexistent/git")
    assert status == "unavailable"
    assert info is None


def test_git_status_real_repo(tmp_path):
    git = _require_git()
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _git(git, repo, "init")
    _git(git, repo, "config", "user.email", "t@t")
    _git(git, repo, "config", "user.name", "t")
    (tmp_path / "repo" / "a.txt").write_text("x")
    _git(git, repo, "add", "a.txt")
    _git(git, repo, "commit", "-m", "init")
    # 干净仓库
    status, info = git_status(repo)
    assert status == "ok" and info is not None
    assert info.dirty == 0
    # 改一个已跟踪文件 → dirty=1（-uno 不计未跟踪）
    (tmp_path / "repo" / "a.txt").write_text("changed")
    status, info = git_status(repo)
    assert status == "ok" and info.dirty == 1


def test_sidebar_shows_branch_in_git_title(tmp_path):
    """v0.10：顶部摘要已删，Git 区块标题显示分支（非仓库 → ``GIT -``）。"""
    from myrtui.client.sidebar import Sidebar

    proj = str(tmp_path / "proj")
    os.makedirs(os.path.join(proj, "sub"))
    with open(os.path.join(proj, "f.txt"), "w") as f:
        f.write("x")
    sidebar = Sidebar(proj)
    sidebar.refresh_data()
    # 非 git 仓库 → 分支标签为 "-"
    assert sidebar._branch_label() == "-"
    assert sidebar._title == "GIT -"


# ---------- F2：侧栏宽度上限 + 分支名 cell 截断 ----------

def test_truncate_cells_ascii():
    from myrtui.client.sidebar import _truncate_cells

    assert _truncate_cells("short", 10) == "short"  # 不超长不截（幂等）
    assert _truncate_cells("abcdefghij", 5) == "abcd…"
    assert _truncate_cells("abcdefghij", 1) == "…"  # 只剩省略号一格
    assert _truncate_cells("abcdefghij", 0) == ""
    assert _truncate_cells("", 5) == ""


def test_truncate_cells_wide_chars():
    """N9 口径：CJK 宽字符按 2 cell 累加，不得用 len() 截出超宽行。"""
    from rich.cells import cell_len

    from myrtui.client.sidebar import _truncate_cells

    s = "中中中中"  # 8 cell
    assert cell_len(s) == 8
    out = _truncate_cells(s, 6)  # 预算 5 cell → 只放得下 2 个「中」+ …
    assert out == "中中…"
    assert cell_len(out) == 5 <= 6
    # 宽字符边界：预算刚好放下一个宽字符
    assert _truncate_cells("中abc", 3) == "中…"  # 2+1=3 已超（要留省略号格）
    assert _truncate_cells("中abc", 4) == "中a…"


def test_sidebar_truncates_long_branch(monkeypatch, tmp_path):
    """F2：超长分支名 → 标题按 cell 截断带省略号，且不溢出内容宽。"""
    from rich.cells import cell_len

    from myrtui.client.sidebar import FALLBACK_CONTENT_CELLS, Sidebar
    from myrtui.gitstat import GitInfo

    long_branch = "feature/very-long-branch-name-for-width-test"  # 45 字符
    monkeypatch.setattr(
        "myrtui.client.sidebar.git_status",
        lambda *a, **k: ("ok", GitInfo(branch=long_branch, dirty=1000)),
    )
    sidebar = Sidebar(str(tmp_path))
    sidebar.refresh_data()
    title = sidebar._title
    assert title.startswith("GIT ")
    assert "…" in title
    assert cell_len(title) <= FALLBACK_CONTENT_CELLS, title
    # 分支段确实被截短（原始 45 字符不完整保留）
    inner = title[len("GIT "):]
    assert cell_len(inner) <= FALLBACK_CONTENT_CELLS - cell_len("GIT ")
    assert len(inner) < len(long_branch)


def test_sidebar_width_contract_css():
    """P3 D5 + v0.11/v0.11.2：CSS 宽度上下限 + ``FALLBACK_CONTENT_CELLS`` 兜底预算。

    新契约**不再有**「``MAX_CONTENT_CELLS == max-width − 2``」的恒等式——
    侧栏宽度变成响应式（fr 比例 + 上下限），摘要截断预算按实际内容宽动态算，
    常量退化为「控件未挂载 / 宽度未知时的兜底值」。

    v0.11：宽度从 ``32%`` 改成 fr 比例（分隔线拖拽要精确控制比例，
    百分比扣掉分隔线后对不上），``max-width`` 从固定 48 放宽成 ``50%``
    （让侧栏能拖得比 48 更宽）；v0.11.2：默认 25% → 15%、min-width
    26 → 20（「基本显示文件树即可」）。
    """
    from myrtui.client.app import TerminalApp
    from myrtui.client.sidebar import FALLBACK_CONTENT_CELLS

    css = TerminalApp.CSS
    assert "#sidebar" in css
    sidebar_rule = css[css.index("#sidebar"):css.index("#pane-container")]
    assert "width: 15fr" in sidebar_rule
    assert "min-width: 20" in sidebar_rule
    assert "max-width: 50%" in sidebar_rule
    # 兜底预算 = 侧栏最小盒宽 20 − padding(0 1 → 2)
    assert FALLBACK_CONTENT_CELLS == 20 - 2


def test_sidebar_git_line_in_repo(tmp_path):
    """§5 第二层：仓库内标题显示 ``GIT <branch>``（脏计数不再显示）。"""
    from myrtui.client.sidebar import Sidebar

    git = _require_git()
    repo = str(tmp_path / "r2")
    os.makedirs(repo)
    _git(git, repo, "init")
    _git(git, repo, "config", "user.email", "t@t")
    _git(git, repo, "config", "user.name", "t")
    with open(os.path.join(repo, "a.txt"), "w") as f:
        f.write("x")
    _git(git, repo, "add", "a.txt")
    _git(git, repo, "commit", "-m", "init")
    with open(os.path.join(repo, "a.txt"), "w") as f:
        f.write("changed")
    sidebar = Sidebar(repo)
    sidebar.refresh_data()
    assert sidebar._git is not None
    assert sidebar._title == f"GIT {sidebar._git.branch}"
    assert sidebar._git.dirty == 1


def _require_git() -> str:
    git = shutil_which("git")
    if not git:
        pytest.skip("git 未安装")
    return git


def shutil_which(cmd: str) -> str | None:
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, cmd)
        if os.access(p, os.X_OK):
            return p
    return None


def _git(git: str, repo: str, *args: str) -> None:
    subprocess.run(
        [git, "-C", repo, *args],
        check=True, capture_output=True, timeout=30,
    )


# ---------- 接管标记：写/读/校验（§6 P1，M14/M15/M21） ----------

def test_marker_write_read_roundtrip(tmp_path):
    p = str(tmp_path / "h.takeover")
    cli.write_takeover_marker(p, pid=4242, ts=1000.5)
    assert cli.read_takeover_markers(p) == [(4242, 1000.5)]
    cli.clear_takeover(p)
    assert not os.path.exists(p)
    # 清掉后再读 = 无接管请求
    assert cli.read_takeover_markers(p) == []


def test_marker_read_tolerates_garbage(tmp_path):
    """M14 读取容忍：半截/垃圾内容一律视为「无接管请求」。"""
    p = str(tmp_path / "g.takeover")
    with open(p, "w") as f:
        f.write("takeover:not-a-pid:xyz\nrandom noise\n")
    assert cli.read_takeover_markers(p) == []
    # 合法行与垃圾行混排 → 只解析合法行
    with open(p, "w") as f:
        f.write("garbage\ntakeover:7:1000\n")
    assert cli.read_takeover_markers(p) == [(7, 1000)]


def test_marker_freshness():
    """M15 标记新鲜：年龄 ≥ 10s 按垃圾处理（存活判定用本进程 pid 恒过）。"""
    now = 1_000_000.0
    pid = os.getpid()
    assert cli.is_marker_valid(pid, now - 1, now=now)
    assert cli.is_marker_valid(pid, now - cli.TAKEOVER_MAX_AGE + 1, now=now)
    assert not cli.is_marker_valid(pid, now - cli.TAKEOVER_MAX_AGE, now=now)
    assert not cli.is_marker_valid(pid, now - 9999, now=now)


def test_marker_dead_pid_invalid():
    """发起方已死（ProcessLookupError）→ 标记是垃圾，不让位。"""
    # 找一个确实不存在的 pid：起一个子进程等它退出
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    dead_pid = proc.pid
    with pytest.raises(ProcessLookupError):
        os.kill(dead_pid, 0)
    assert not cli.is_marker_valid(dead_pid, time.time())


def test_marker_permission_error_means_not_ours(monkeypatch):
    """M21 异常语义：PermissionError ≠ 不存在 → 视为「不是我们的前端」，
    不让位（误判方向安全）。"""
    def raise_perm(pid, sig):
        raise PermissionError(pid)

    monkeypatch.setattr(os, "kill", raise_perm)
    assert not cli.is_marker_valid(os.getpid(), time.time())


def test_marker_valid_for_live_pid():
    """存活 pid + 新鲜标记 → 两级校验全过（可让位）。"""
    assert cli.is_marker_valid(os.getpid(), time.time())


# ---------- 锁 + 接管：单进程语义 ----------

def test_acquire_lock_writes_holder_record(tmp_path):
    p = str(tmp_path / "a.lock")
    fd = cli.acquire_lock(p)
    try:
        with open(p) as f:
            assert f.read().strip() == str(os.getpid())
    finally:
        os.close(fd)


def test_acquire_lock_rejects_when_held(tmp_path):
    p = str(tmp_path / "b.lock")
    fd = cli.acquire_lock(p)
    try:
        with pytest.raises(BlockingIOError):
            cli.acquire_lock(p)
    finally:
        os.close(fd)


def test_takeover_acquires_when_free_no_marker_left(tmp_path):
    """锁空闲时直接拿到，且不留接管标记（首次路径不误写）。"""
    lock = str(tmp_path / "c.lock")
    tp = str(tmp_path / "c.takeover")
    fd = cli.acquire_lock_with_takeover(lock, tp, timeout=1.0, poll=0.05)
    try:
        assert not os.path.exists(tp)
        with open(lock) as f:
            assert f.read().strip() == str(os.getpid())
    finally:
        os.close(fd)


def test_takeover_writes_marker_while_waiting(tmp_path):
    """持锁期间：等待方写接管标记；超时离场必须清掉自己的标记。"""
    lock = str(tmp_path / "d.lock")
    tp = str(tmp_path / "d.takeover")
    holder = cli.acquire_lock(lock)
    t0 = time.time()
    observed: list[tuple[int, float]] = []
    import threading

    def waiter():
        try:
            cli.acquire_lock_with_takeover(
                lock, tp, timeout=0.8, poll=0.05, refresh=0.3
            )
        except TimeoutError:
            pass

    th = threading.Thread(target=waiter)
    th.start()
    # 等待期间观察标记
    deadline = time.time() + 0.8
    while time.time() < deadline:
        ms = cli.read_takeover_markers(tp)
        if ms:
            observed = ms
            break
        time.sleep(0.03)
    th.join(timeout=5)
    assert observed, "等待期间应写入接管标记"
    pid, ts = observed[0]
    assert pid == os.getpid()
    assert ts >= t0 - 1
    # 超时离场后：自己的标记必须被清理（否则哨戒者会对放弃者让位）
    assert cli.read_takeover_markers(tp) == []
    os.close(holder)


def test_takeover_refreshes_marker_periodically(tmp_path):
    """等待时间超过 refresh 周期时，标记时间戳被周期刷新（保鲜，M15）。"""
    lock = str(tmp_path / "e.lock")
    tp = str(tmp_path / "e.takeover")
    holder = cli.acquire_lock(lock)
    import threading

    def waiter():
        try:
            cli.acquire_lock_with_takeover(
                lock, tp, timeout=1.0, poll=0.05, refresh=0.15
            )
        except TimeoutError:
            pass

    th = threading.Thread(target=waiter)
    th.start()
    # 采样标记时间戳：刷新会使其严格增大
    stamps: list[float] = []
    deadline = time.time() + 1.0
    while time.time() < deadline:
        ms = cli.read_takeover_markers(tp)
        if ms and (not stamps or ms[0][1] != stamps[-1]):
            stamps.append(ms[0][1])
        time.sleep(0.05)
    th.join(timeout=5)
    os.close(holder)
    assert len(stamps) >= 1, "等待期间应写入接管标记"
    # refresh=0.15s / 等待 1.0s → 至少刷新一次；离场清理可能吃掉最后采样，
    # 所以只要观察到 ≥2 个不同时间戳即证明周期刷新（保鲜）在跑
    assert len(stamps) >= 2, f"标记应被周期刷新，采样={stamps}"


# ---------- 多 pane 后端（§12 P1） ----------

@pytest.fixture
def backend():
    name = f"myrtui-p1-{uuid.uuid4().hex[:8]}"
    b = TmuxBackend(name, name)
    root = tempfile.mkdtemp(prefix="myrtui-proj-")
    b.create_session(root, 80, 24)
    yield b
    b.kill_server()


def test_new_window_index_and_order(backend):
    """P1：new_window 后按 window_index 排序，两个 pane 各自独立。"""
    panes = backend.list_panes()
    assert len(panes) == 1
    assert panes[0].window_index == 0
    wid = backend.new_window(tempfile.gettempdir())
    panes = sorted(backend.list_panes(), key=lambda p: p.window_index)
    assert len(panes) == 2
    assert [p.window_index for p in panes] == [0, 1]
    assert panes[1].window_id == wid


def test_poll_multi_pane_batched(backend):
    """N14：两个 pane 一次 poll 抓完，行数断言各自成立。"""
    backend.new_window(tempfile.gettempdir())
    time.sleep(0.3)
    panes = sorted(backend.list_panes(), key=lambda p: p.window_index)
    offsets = {p.pane_id: (0, p.height) for p in panes}
    infos, caps = backend.poll(offsets)
    assert len(infos) == 2
    for p in infos:
        assert len(caps[p.pane_id].text.splitlines()) == p.height


def test_kill_window_removes_pane(backend):
    wid = backend.new_window(tempfile.gettempdir())
    time.sleep(0.3)
    backend.kill_window(wid)
    time.sleep(0.3)
    panes = backend.list_panes()
    assert len(panes) == 1


# ---------- 多 pane 前端（headless，§12 P1） ----------

def _make_app(backend: TmuxBackend, root: str) -> TerminalApp:
    return TerminalApp(backend, root)


def test_app_single_pane_default_layout(backend, app_root):
    async def scenario():
        app = _make_app(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            assert len(app._pane_ids) == 1
            # 1 pane：全宽档 —— col-b 隐藏、pane-0 占满
            assert app.query_one("#col-b").display is False
            assert app.query_one("#pane-0", PaneWidget).display is True
            assert app.query_one("#pane-1", PaneWidget).display is False
            # 顶栏 1 个标签可见、+ 可用
            topbar = app.query_one(TopBar)
            assert topbar.query_one("#tab-0", PaneTab).display is True
            assert topbar.query_one("#tab-1", PaneTab).display is False
            assert topbar.query_one("#btn-new", TopBarButton).is_disabled is False
            # 品牌标签在最右侧
            brand = topbar.query_one("#topbar-brand", Label)
            assert brand.content == BRAND_TEXT
            assert brand.region.right == app.size.width
    asyncio.run(scenario())


def test_app_two_panes_stacked_vertically(backend, app_root):
    """v0.12：2 pane 档 = **上下分**（pane-1 在 pane-0 下面、同宽）。

    用户拍板：「默认第二个 pane 是与第一个左右分布的 → 改为上下分布」。
    """
    async def scenario():
        app = _make_app(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            await pilot.press("ctrl+a"); await pilot.press("t")
            await pilot.pause(0.8)
            assert len(app._pane_ids) == 2
            p0 = app.query_one("#pane-0", PaneWidget).region
            p1 = app.query_one("#pane-1", PaneWidget).region
            # 上下排列：pane-1 在 pane-0 下方；左右边界一致（等宽）
            assert p1.y > p0.y, (p0, p1)
            assert p0.x == p1.x and p0.width == p1.width, (p0, p1)
            # 两块高度相当（rows 比例默认 0.5，差 1 是分隔线本身）
            assert abs(p0.height - p1.height) <= 1, (p0, p1)
    asyncio.run(scenario())


def test_app_new_pane_layouts(backend, app_root):
    """Ctrl-a t 逐档：1→2 上下分、→3 左2右1（col-b 显示）、→4 2×2。"""
    async def scenario():
        app = _make_app(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            # 1 → 2：上下分（col-b 仍隐藏）
            await pilot.press("ctrl+a"); await pilot.press("t")
            await pilot.pause(0.6)
            assert len(app._pane_ids) == 2
            assert app.query_one("#col-b").display is False
            assert app.query_one("#pane-1", PaneWidget).display is True
            # 新 pane 获得焦点（Ctrl-a 1 回老 pane，3 无效因只有 2 个）
            assert app._active_index == 1
            await pilot.press("ctrl+a"); await pilot.press("3")
            assert app._active_index == 1, "越界槽位应被忽略"
            await pilot.press("ctrl+a"); await pilot.press("1")
            assert app._active_index == 0
            # 2 → 3：左2右1（col-b 显示）
            await pilot.press("ctrl+a"); await pilot.press("t")
            await pilot.pause(0.6)
            assert len(app._pane_ids) == 3
            assert app.query_one("#col-b").display is True
            assert app.query_one("#pane-2", PaneWidget).display is True
            assert app.query_one("#pane-3", PaneWidget).display is False
            # 3 → 4：2×2
            await pilot.press("ctrl+a"); await pilot.press("t")
            await pilot.pause(0.6)
            assert len(app._pane_ids) == 4
            assert app.query_one("#pane-3", PaneWidget).display is True
            # 满 4 → + 禁用；第 5 次 Ctrl-a t 被忽略
            topbar = app.query_one(TopBar)
            assert topbar.query_one("#btn-new", TopBarButton).is_disabled is True
            await pilot.press("ctrl+a"); await pilot.press("t")
            await pilot.pause(0.5)
            assert len(app._pane_ids) == MAX_PANES
    asyncio.run(scenario())


def test_app_close_pane_and_last_is_noop(backend, app_root):
    """Ctrl-a w 关闭当前 pane；最后一个 pane 时 no-op（设计拍板）。"""
    async def scenario():
        app = _make_app(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            await pilot.press("ctrl+a"); await pilot.press("t")
            await pilot.press("ctrl+a"); await pilot.press("t")
            await pilot.pause(0.6)
            assert len(app._pane_ids) == 3
            # 焦点在新 pane（槽位 2），Ctrl-a w 关掉它
            assert app._active_index == 2
            await pilot.press("ctrl+a"); await pilot.press("w")
            await pilot.pause(0.6)
            assert len(app._pane_ids) == 2
            assert backend.has_session(), "关闭一个 pane 不得杀 session"
            # 关到只剩 1 个后，Ctrl-a w 必须 no-op
            await pilot.press("ctrl+a"); await pilot.press("w")
            await pilot.pause(0.6)
            assert len(app._pane_ids) == 1
            await pilot.press("ctrl+a"); await pilot.press("w")
            await pilot.pause(0.6)
            assert len(app._pane_ids) == 1, "最后一个 pane 禁止关闭"
            assert backend.has_session(), "最后一个 pane 的 Ctrl-a w 必须 no-op"
    asyncio.run(scenario())


def test_app_key_routes_to_active_pane(backend, app_root):
    """键透传/焦点路由：输入落到 Ctrl-a N 选中的 pane，不是永远的 pane0。"""
    async def scenario():
        app = _make_app(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            await pilot.press("ctrl+a"); await pilot.press("t")
            await pilot.pause(0.6)
            ids = list(app._pane_ids)
            assert len(ids) == 2
            # 焦点切到 pane 1，输入 marker → 只应出现在 ids[1]
            await pilot.press("ctrl+a"); await pilot.press("1")
            assert app._active_index == 0
            await pilot.press("ctrl+a"); await pilot.press("2")
            assert app._active_index == 1
            for ch in "echo ROUTE-MARKER":
                await pilot.press(ch if ch != " " else "space")
            await pilot.press("enter")
            await pilot.pause(0.8)
            info = {p.pane_id: p for p in backend.list_panes()}
            _, caps = backend.poll(
                {pid: (0, info[pid].height) for pid in ids}
            )
            assert "ROUTE-MARKER" in caps[ids[1]].text, "焦点 pane 应收到输入"
            assert "ROUTE-MARKER" not in caps[ids[0]].text, "非焦点 pane 不应收到"
    asyncio.run(scenario())


def test_app_tab_click_switches_focus(backend, app_root):
    """点击顶栏 tab = Ctrl-a N（§12 顶栏标签可点击）。"""
    async def scenario():
        app = _make_app(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            await pilot.press("ctrl+a"); await pilot.press("t")
            await pilot.pause(0.6)
            assert app._active_index == 1
            await pilot.click("#tab-0")
            await pilot.pause(0.3)
            assert app._active_index == 0
            await pilot.click("#tab-1")
            await pilot.pause(0.3)
            assert app._active_index == 1
    asyncio.run(scenario())


def test_app_plus_button_creates_pane(backend, app_root):
    """顶栏 `+` 等价 Ctrl-a t。"""
    async def scenario():
        app = _make_app(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            await pilot.click("#btn-new")
            await pilot.pause(0.6)
            assert len(app._pane_ids) == 2
    asyncio.run(scenario())


def test_app_resize_follows_layout_change(backend, app_root):
    """布局档切换 → widget resize → resize-window 联动（N7，P0 验收口径）。"""
    async def scenario():
        app = _make_app(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            # 1 pane：tmux 尺寸 == widget 尺寸
            ids = list(app._pane_ids)
            info = {p.pane_id: p for p in backend.list_panes()}
            w0 = app.query_one("#pane-0", PaneWidget).size
            assert (info[ids[0]].width, info[ids[0]].height) == (
                w0.width, w0.height
            )
            # 切 2 pane 档 → 上下分：两边 widget 都收到 resize 并联动 tmux
            await pilot.press("ctrl+a"); await pilot.press("t")
            await pilot.pause(1.0)
            ids = list(app._pane_ids)
            assert len(ids) == 2
            info = {p.pane_id: p for p in backend.list_panes()}
            for slot in (0, 1):
                w = app.query_one(f"#pane-{slot}", PaneWidget).size
                assert (info[ids[slot]].width, info[ids[slot]].height) == (
                    w.width, w.height
                ), f"slot {slot} tmux 尺寸未联动"
            # 上下分后单 pane **高度**减半、宽度不变（v0.12 列优先）
            assert info[ids[0]].height < w0.height
            assert info[ids[0]].width == w0.width
    asyncio.run(scenario())


def test_app_external_topo_drift_resync(backend, app_root):
    """外部 kill-window → 下一轮 poll 同步拓扑，不误报失去连接。"""
    async def scenario():
        app = _make_app(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            await pilot.press("ctrl+a"); await pilot.press("t")
            await pilot.pause(0.6)
            assert len(app._pane_ids) == 2
            # 外部杀掉第二个 window（绕过 App）
            panes = sorted(backend.list_panes(), key=lambda p: p.window_index)
            backend.kill_window(panes[1].window_id)
            await pilot.pause(1.5)
            assert len(app._pane_ids) == 1, "应同步到外部拓扑"
            assert app._fail_count == 0, "不应累计失败"
            assert "失去连接" not in app.query_one(TopBar)._status
    asyncio.run(scenario())


# ---------- 接管哨戒：持锁者让位（§6 P1） ----------

def test_sentry_yields_on_valid_marker(backend, tmp_path, app_root):
    """持锁 App 收到有效接管标记 → 干净退出（让位，shell 无损）。"""
    tp = str(tmp_path / "sent.takeover")

    async def scenario():
        app = TerminalApp(backend, app_root, takeover_path=tp)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.6)
            assert app.return_value is None and app._exit is False
            # 有效标记：本进程 pid + 新鲜时间戳（等待方就是另一个 myrtui）
            cli.write_takeover_marker(tp, pid=os.getpid(), ts=time.time())
            # 哨戒周期 500ms → 留 2s 观察
            for _ in range(40):
                await pilot.pause(0.05)
                if app._exit:
                    break
            assert app._exit, "哨戒应在 2s 内看到有效标记并让位"
    asyncio.run(scenario())
    # 让位 = 前端退出；会话必须还活着（shell 无损）
    assert backend.has_session(), "让位不得影响 tmux 会话"


def test_sentry_ignores_stale_marker(backend, tmp_path, app_root):
    """过期标记（≥10s）→ 垃圾处理，不让位。"""
    tp = str(tmp_path / "stale.takeover")

    async def scenario():
        app = TerminalApp(backend, app_root, takeover_path=tp)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.6)
            cli.write_takeover_marker(
                tp, pid=os.getpid(), ts=time.time() - cli.TAKEOVER_MAX_AGE - 5
            )
            await pilot.pause(1.5)
            assert not app._exit, "过期标记不应让位"
            # 清掉垃圾标记后换成有效标记 → 让位（同一实例仍能正常交接）
            cli.write_takeover_marker(tp, pid=os.getpid(), ts=time.time())
            for _ in range(40):
                await pilot.pause(0.05)
                if app._exit:
                    break
            assert app._exit
    asyncio.run(scenario())
    assert backend.has_session()


def test_sentry_no_takeover_path_no_sentry(backend, app_root):
    """takeover_path=None（测试/直连场景）→ 不装哨戒，不误退。"""
    async def scenario():
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(1.2)
            assert not app._exit
    asyncio.run(scenario())


# ---------- 进程级接管端到端（两进程真实 flock 交接） ----------

def test_takeover_end_to_end_two_processes(tmp_path):
    """父进程持锁 + 哨戒线程；子进程请求接管 → 父让位、子获锁。

    模拟 cli.main 的完整交接路径（无 pid kill）。
    """
    lock = str(tmp_path / "e2e.lock")
    tp = str(tmp_path / "e2e.takeover")
    proj = str(tmp_path / "proj")
    os.makedirs(proj)

    # 父进程：持锁（模拟运行中的前端）
    holder = cli.acquire_lock(lock)

    # 父进程侧哨戒线程（与 TerminalApp._check_takeover 同一逻辑）
    import threading

    yielded = threading.Event()

    def sentry():
        while not yielded.is_set():
            for pid, ts in cli.read_takeover_markers(tp):
                if cli.is_marker_valid(pid, ts):
                    os.close(holder)  # 让位：释放 flock
                    yielded.set()
                    return
            time.sleep(0.1)

    th = threading.Thread(target=sentry, daemon=True)
    th.start()

    # 子进程：真实启动第二个 myrtui 的锁路径
    # （os._exit 不刷缓冲 → 每次 print 后必须 flush=True；
    #  sys.path 用包所在目录，不依赖 pytest 的 cwd）
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))
    code = (
        "import sys, os, time\n"
        f"sys.path.insert(0, {repo_root!r})\n"
        "from myrtui import cli\n"
        "fd = cli.acquire_lock_with_takeover("
        f"{lock!r}, {tp!r}, timeout=8.0, poll=0.1, refresh=0.3)\n"
        "print('ACQUIRED', os.getpid(), flush=True)\n"
        "cli.write_holder_record(fd)\n"
        "os.close(fd)\n"
        "print('CLEARED' if not os.path.exists(%r) else 'NOT-CLEARED', flush=True)\n"
        "os._exit(0)\n" % tp
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=30,
        cwd=os.getcwd(),
    )
    assert proc.returncode == 0, proc.stderr
    assert "ACQUIRED" in proc.stdout, f"子进程未获锁: {proc.stderr}"
    # 子进程锁到手后清掉了旁挂标记
    assert "CLEARED" in proc.stdout
    # 锁文件记录已换成子进程 pid（原地覆写，M14）
    with open(lock) as f:
        assert f.read().strip() == proc.stdout.split()[1]
    # 父进程哨戒确实因子进程标记而让位
    assert yielded.is_set(), "父进程哨戒应观察到接管标记并让位"
    # 收尾：holder 已在哨戒里 close；若子进程先超时则父进程还持锁
    try:
        os.close(holder)
    except OSError:
        pass


def test_takeover_timeout_when_holder_never_yields(tmp_path):
    """持锁者不让位（哨戒不跑）→ 等待方超时退出，提示手动处理（§9）。"""
    lock = str(tmp_path / "to.lock")
    tp = str(tmp_path / "to.takeover")
    holder = cli.acquire_lock(lock)
    try:
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            cli.acquire_lock_with_takeover(
                lock, tp, timeout=0.5, poll=0.05, refresh=0.2
            )
        assert time.monotonic() - t0 >= 0.5
        # 超时路径必须清理自己的标记
        assert cli.read_takeover_markers(tp) == []
    finally:
        os.close(holder)


def test_main_rejects_after_takeover_timeout(tmp_path, monkeypatch, capsys):
    """完整 main() 路径：抢不到锁且对方不让位 → 超时后提示退出（§9）。

    hermetic（审阅 F4）：`chdir(tmp_path)` 使 digest 从 tmp 路径派生——
    锁文件（全局共享、永不 unlink）与 tmux socket 名都落在本测试专属
    digest 上。否则本用例用「仓库自身」锁路径：开发者在本仓库跑着 myrtui
    （本工具正常用法）、或上轮残留子进程持锁时，该用例必挂。
    """
    monkeypatch.chdir(tmp_path)  # 必须在 derive_identity 之前
    root, digest = cli.derive_identity()
    assert tmp_path.name in root  # 确认 digest 派生源已被隔离
    lock = cli.lock_path_for(digest)
    tp = cli.takeover_path_for(digest)
    holder = cli.acquire_lock(lock)
    monkeypatch.setattr(cli, "TAKEOVER_TIMEOUT", 0.4)
    monkeypatch.setattr(cli, "TAKEOVER_POLL", 0.05)
    monkeypatch.setattr(cli, "TAKEOVER_REFRESH", 0.1)
    try:
        # 空 argv：P2 起 main() 会解析启动参数（默认读 sys.argv[1:]，
        # 那在 pytest 下是测试框架自己的参数）
        rc = cli.main([])
    finally:
        os.close(holder)
        cli.clear_takeover(tp)
    assert rc == 1
    err = capsys.readouterr().err
    assert "已有 myrtui 实例在运行" in err and "未让位" in err
