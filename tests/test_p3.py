"""myrtui P3 测试：侧栏 v2 —— 文件树 + Git 历史（P3 方案 §9.1 覆盖矩阵）。

分层：
- 纯函数：``parse_git_log`` / ``FileTree.filter_paths``；
- I/O：``git_log``（真实仓库 / 非仓库 / 空仓库 / git 缺失 / 文件级）；
- 控件：树（点目录展开、点文件发消息、guide_depth 重绘）、历史面板（日期
  三档、截断、悬停卡片）；
- 集成：文件树 ↔ 历史面板联动、焦点模型（D8）、取数竞态（D7）、
  ``Ctrl-a b`` 折叠、``Ctrl-a s`` 进侧栏。

后端测试跑在隔离 tmux 实例上（临时 socket），结束 kill-server。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import threading
import uuid

import pytest
from textual.app import App, ComposeResult
from textual.events import Paste
from textual.widgets import Static

from myrtui import gitlog
from myrtui.client.app import TerminalApp
from myrtui.client.file_tree import FileTree
from myrtui.client.git_panel import CommitCard, GitLogPanel, commit_card_text
from myrtui.client.pane_widget import PaneWidget
from myrtui.gitlog import Commit
from myrtui.tmux_backend import TmuxBackend


# ---------- 夹具 ----------

@pytest.fixture
def backend():
    name = f"myrtui-p3-{uuid.uuid4().hex[:8]}"
    b = TmuxBackend(name, name)
    root = tempfile.mkdtemp(prefix="myrtui-proj-")
    b.create_session(root, 80, 24)
    yield b
    b.kill_server()


def _git(repo: str, *args: str) -> None:
    subprocess.run(
        ["git", "-C", repo, *args],
        check=True,
        capture_output=True,
        timeout=30,
    )


def _make_repo(root: str, *, commits: int = 2) -> str:
    """在 root 建一个真实仓库（默认 2 次提交），返回仓库路径。"""
    os.makedirs(root, exist_ok=True)
    _git(root, "init", "-q", ".")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    for i in range(commits):
        with open(os.path.join(root, f"f{i}.txt"), "w") as f:
            f.write("x")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", f"feat: 第 {i} 次提交")
    return root


def _branch(repo: str) -> str:
    """仓库当前分支名（git init 的默认名随版本/配置而变，断言不能写死）。"""
    proc = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.stdout.strip()


def _tree_lines(tree: FileTree) -> list[str]:
    """树当前各行的标签（按显示顺序）。"""
    return [str(line.path[-1].label.plain) for line in tree._tree_lines]


def _panel_text(panel: GitLogPanel) -> list[str]:
    return [option.prompt.plain for option in panel.options]


class _TreeApp(App):
    """只挂一棵树的最小 App（记录 PathSelected 消息）。

    **树必须挂在运行中的 App 里**：未挂载地构造 ``FileTree(...)`` 时，基类
    ``DirectoryTree.__init__`` 里的 ``self.path = path`` 会触发 async watcher，
    而消息泵没在跑 → 该协程永远等不到 ``await`` → GC 时报
    ``RuntimeWarning: coroutine 'DirectoryTree.watch_path' was never awaited``
    （验收复核 D1/O4 的真正成因）。产品代码不受影响（树永远挂载在 App 里）。
    """

    def __init__(self, root: str) -> None:
        super().__init__()
        self.root_path = root
        self.selected: list[tuple[str, bool]] = []

    def compose(self) -> ComposeResult:
        yield FileTree(self.root_path)

    def on_file_tree_path_selected(self, event: FileTree.PathSelected) -> None:
        self.selected.append((event.path, event.is_dir))


# ---------- 纯函数：parse_git_log（§9.1） ----------

def test_parse_git_log_normal():
    out = "a1b2c3d\x1falice\x1f2026-10-19\x1ffeat: 侧栏改进\n"
    out += "9f8e7d6\x1fbob\x1f2026-10-18\x1ffix: 宽度契约\n"
    commits = gitlog.parse_git_log(out)
    assert len(commits) == 2
    assert commits[0] == Commit(
        sha="a1b2c3d", author="alice", date="2026-10-19", subject="feat: 侧栏改进"
    )
    assert commits[1].sha == "9f8e7d6"


def test_parse_git_log_subject_with_pipe_and_spaces():
    """字段分隔用 \\x1f：subject 含 | 与空格不得串字段（§4.2）。"""
    out = "a1b2c3d\x1falice\x1f2026-10-19\x1ffix: a | b : c 空格\n"
    commits = gitlog.parse_git_log(out)
    assert len(commits) == 1
    assert commits[0].subject == "fix: a | b : c 空格"
    assert commits[0].author == "alice"


def test_parse_git_log_multiline_body():
    """NUL 记录分隔：正文可多行，不会把一条提交拆成多行。"""
    out = (
        "a1b2c3d\x1falice\x1f2026-10-19\x1ffeat: 标题\x1f"
        "正文第一行\n正文第二行\n\nfooter\n\x00"
        "\nb2c3d4e\x1fbob\x1f2026-10-18\x1ffix: 无正文\x1f\x00"
    )
    commits = gitlog.parse_git_log(out)
    assert len(commits) == 2
    assert commits[0].subject == "feat: 标题"
    assert commits[0].body == "正文第一行\n正文第二行\n\nfooter"
    assert commits[1].subject == "fix: 无正文"
    assert commits[1].body == ""


def test_parse_git_log_empty_and_garbage():
    assert gitlog.parse_git_log("") == []
    assert gitlog.parse_git_log("not a git output\n") == []


def test_parse_git_log_skips_short_lines():
    """段数不足的行跳过、不抛错（R7）。"""
    out = "a1b2c3d\x1falice\x1f2026-10-19\n"  # 少一段
    out += "a1b2c3d\x1falice\x1f2026-10-19\x1f正常标题\n"
    commits = gitlog.parse_git_log(out)
    assert len(commits) == 1
    assert commits[0].subject == "正常标题"


# ---------- 纯函数：FileTree.filter_paths（D3/D4） ----------

def test_filter_paths_excludes_git_only(tmp_path):
    """唯一硬排除 .git；.gitignore / node_modules / 普通隐藏目录一律保留。

    用**已挂载**的树（pilot 形态）——未挂载构造会留下永不 await 的 watcher
    协程（RuntimeWarning，见验收报告 O4/D1）。
    """

    async def scenario():
        app = _TreeApp(str(tmp_path))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.4)
            tree = app.query_one(FileTree)
            paths = [
                tmp_path / ".git",
                tmp_path / ".gitignore",
                tmp_path / ".venv",
                tmp_path / "node_modules",
                tmp_path / "a.py",
            ]
            kept = {p.name for p in tree.filter_paths(paths)}
            assert ".git" not in kept
            assert {".gitignore", ".venv", "node_modules", "a.py"} <= kept

    asyncio.run(scenario())


# ---------- I/O：git_log（§9.1） ----------

def test_git_log_real_repo(tmp_path):
    repo = _make_repo(str(tmp_path / "repo"), commits=2)
    status, commits = gitlog.git_log(repo)
    assert status == "ok"
    assert len(commits) == 2
    # 最近提交在最上
    assert commits[0].subject == "feat: 第 1 次提交"
    assert commits[1].subject == "feat: 第 0 次提交"
    assert all(len(c.sha) >= 7 for c in commits)
    # --date=format:%Y-%m-%d %H:%M → 2026-10-20 14:30
    assert len(commits[0].date) == 16
    assert commits[0].date[10] == " "
    assert commits[0].date[13] == ":"


def test_git_log_non_repo(tmp_path):
    status, commits = gitlog.git_log(str(tmp_path))
    assert status == "not-repo"
    assert commits == []


def test_git_log_empty_repo_is_ok_and_empty(tmp_path):
    """空仓库特判：git 可用，只是当前分支没有提交 → ("ok", [])。

    不能落到 unavailable（那会显示「git 不可用」，误导用户）。
    """
    repo = str(tmp_path / "empty")
    os.makedirs(repo)
    _git(repo, "init", "-q", ".")
    status, commits = gitlog.git_log(repo)
    assert status == "ok"
    assert commits == []


def test_git_log_missing_git_binary(tmp_path):
    status, commits = gitlog.git_log(str(tmp_path), git_bin="/nonexistent/git")
    assert status == "unavailable"
    assert commits == []


def test_git_log_path_filters_commits(tmp_path):
    """文件级：只返回该路径的提交。"""
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    with open(os.path.join(repo, "a.txt"), "w") as f:
        f.write("x")
    with open(os.path.join(repo, "b.txt"), "w") as f:
        f.write("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    with open(os.path.join(repo, "b.txt"), "w") as f:
        f.write("y")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "touch b")
    status, commits = gitlog.git_log(repo)
    assert status == "ok" and len(commits) == 2
    status, commits = gitlog.git_log(repo, path="b.txt")
    assert status == "ok"
    assert [c.subject for c in commits] == ["touch b", "init"]
    status, commits = gitlog.git_log(repo, path="a.txt")
    assert status == "ok"
    assert [c.subject for c in commits] == ["init"]


def test_git_log_untracked_file_is_ok_empty(tmp_path):
    """未跟踪文件：无提交记录但 git 可用 → ("ok", [])（D6）。"""
    repo = _make_repo(str(tmp_path / "repo"), commits=1)
    with open(os.path.join(repo, "new.txt"), "w") as f:
        f.write("x")
    status, commits = gitlog.git_log(repo, path="new.txt")
    assert status == "ok"
    assert commits == []


# ---------- 控件：文件树 ----------

def test_tree_shows_root_and_first_level(tmp_path):
    """首屏：项目根 + 第一层子项，无需任何点击（验收 1）。"""
    root = str(tmp_path / "proj")
    os.makedirs(os.path.join(root, "sub"))
    with open(os.path.join(root, "a.txt"), "w") as f:
        f.write("x")

    async def scenario():
        app = _TreeApp(root)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.6)
            tree = app.query_one(FileTree)
            assert tree.root.label.plain == os.path.basename(root)
            assert {c.label.plain for c in tree.root.children} == {"sub", "a.txt"}

    asyncio.run(scenario())


def test_tree_click_directory_expands(tmp_path):
    """点目录行 → 该目录展开并显示其子项；再点 → 收起（验收 2）。"""
    root = str(tmp_path / "proj")
    os.makedirs(os.path.join(root, "sub"))
    with open(os.path.join(root, "sub", "deep.txt"), "w") as f:
        f.write("x")
    with open(os.path.join(root, "a.txt"), "w") as f:
        f.write("x")

    async def scenario():
        app = _TreeApp(root)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.6)
            tree = app.query_one(FileTree)
            line = _tree_lines(tree).index("sub")
            await pilot.click(FileTree, offset=(4, line))
            await pilot.pause(0.8)
            sub = next(c for c in tree.root.children if c.label.plain == "sub")
            assert sub.is_expanded
            assert [c.label.plain for c in sub.children] == ["deep.txt"]
            # 再点一次 → 收起
            await pilot.click(FileTree, offset=(4, line))
            await pilot.pause(0.5)
            assert not sub.is_expanded

    asyncio.run(scenario())


def test_tree_click_file_posts_path_selected(tmp_path):
    """点文件行 → 发 PathSelected（is_dir=False，仅选中不做别的）。"""
    root = str(tmp_path / "proj")
    os.makedirs(root)
    with open(os.path.join(root, "a.txt"), "w") as f:
        f.write("x")

    async def scenario():
        app = _TreeApp(root)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.6)
            line = _tree_lines(app.query_one(FileTree)).index("a.txt")
            await pilot.click(FileTree, offset=(4, line))
            await pilot.pause(0.5)
            assert app.selected == [("a.txt", False)]

    asyncio.run(scenario())


def test_tree_click_directory_posts_path_selected_is_dir(tmp_path):
    """点目录行也发 PathSelected（is_dir=True）→ App 据此回到仓库级。"""
    root = str(tmp_path / "proj")
    os.makedirs(os.path.join(root, "sub"))

    async def scenario():
        app = _TreeApp(root)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.6)
            line = _tree_lines(app.query_one(FileTree)).index("sub")
            await pilot.click(FileTree, offset=(4, line))
            await pilot.pause(0.5)
            assert app.selected == [("sub", True)]

    asyncio.run(scenario())


def test_tree_guide_depth_change_repaints(tmp_path):
    """R8 回归防线：guide_depth 是 reactive，赋值后必须**确实重绘**。

    若子类用普通类属性遮蔽了描述符 → watcher 不触发 → 缩进不变。
    """
    root = str(tmp_path / "proj")
    os.makedirs(os.path.join(root, "sub"))
    with open(os.path.join(root, "sub", "deep.txt"), "w") as f:
        f.write("x")

    async def scenario():
        app = _TreeApp(root)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.6)
            tree = app.query_one(FileTree)
            assert tree.guide_depth == 2  # 本类在 __init__ 里改成 2
            line = _tree_lines(tree).index("sub")
            await pilot.click(FileTree, offset=(4, line))  # 展开 → 出现孙节点
            await pilot.pause(0.8)
            deep_line = _tree_lines(tree).index("deep.txt")

            def label_col(render_line: int) -> int:
                return tree.render_line(render_line).text.index("deep.txt")

            before = label_col(deep_line)
            tree.guide_depth = 4
            await pilot.pause(0.5)
            after = label_col(deep_line)
            assert after > before, f"guide_depth 改了但没重绘（{before} → {after}）"

    asyncio.run(scenario())


def test_large_dir_expansion_runs_in_worker_thread(tmp_path, monkeypatch):
    """G2：展开大目录**不冻结 UI** —— 目录扫描在**线程** worker 里跑。

    确定性断言（不靠 sleep 赌时序）：monkeypatch 基类的目录枚举钩子
    ``_directory_content``，记录执行线程；扫描发生在**非主线程** → 事件循环
    不被目录扫描阻塞（方案 §2.2 G2 / §9 验收 2）。
    """
    base = str(tmp_path / "proj")
    os.makedirs(base)
    big = os.path.join(base, "big")
    os.makedirs(big)
    for i in range(300):
        with open(os.path.join(big, f"f{i}.txt"), "w") as f:
            f.write("x")

    threads: list[str] = []

    async def scenario():
        app = _TreeApp(base)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            original = FileTree._directory_content

            def spy(self, location, worker):
                threads.append(threading.current_thread().name)
                return original(self, location, worker)

            monkeypatch.setattr(FileTree, "_directory_content", spy)
            line = _tree_lines(tree).index("big")
            await pilot.click(FileTree, offset=(4, line))  # 展开大目录
            await pilot.pause(1.5)
            node = next(c for c in tree.root.children if c.label.plain == "big")
            assert len(node.children) == 300  # 子项确实加载出来了

    asyncio.run(scenario())
    assert threads, "未观察到目录扫描"
    assert threads[0] != threading.current_thread().name, (
        f"目录扫描跑在主线程上（会冻结事件循环）：{threads[0]}"
    )


# ---------- 控件：历史面板 ----------

def test_git_panel_show_commits_renders_date_and_truncates():
    """行内含日期；标题过长按 cell 截断加 …（N9 口径，验收 6）。"""
    commits = [
        Commit(
            sha="a1b2c3d",
            author="alice",
            date="2026-10-19 12:34",
            subject="feat: 一个非常非常长的提交标题需要被截断",
        ),
    ]
    panel = GitLogPanel()
    panel.show_commits(commits, width=40)
    assert panel.option_count == 1
    text = panel.options[0].prompt.plain
    assert "2026-10-19 12:34" in text
    assert "a1b2c3d" not in text
    assert text.startswith("2026-10-19 12:34  ")
    assert "…" in text


def test_git_panel_show_message():
    panel = GitLogPanel()
    panel.show_message("非 git 仓库")
    assert panel.option_count == 1
    assert panel.options[0].prompt.plain == "非 git 仓库"


def test_git_panel_row_is_date_and_subject_only():
    """行内只有日期（时间）+ 标题；sha / 作者已移到悬停卡片。"""
    commits = [
        Commit(
            sha="a1b2c3d", author="zoe", date="2026-10-19 12:34", subject="标题"
        ),
    ]
    panel = GitLogPanel()
    panel.show_commits(commits, width=40)
    text = panel.options[0].prompt.plain
    assert text == "2026-10-19 12:34  标题"
    assert "zoe" not in text
    assert "a1b2c3d" not in text


def test_git_panel_date_tiers_by_width():
    """日期列三档：MM-DD / YYYY-MM-DD / YYYY-MM-DD HH:MM。"""
    commit = Commit(
        sha="a1b2c3d", author="zoe", date="2026-10-19 12:34", subject="标题"
    )
    expectations = [
        (18, "10-19  标题"),
        (24, "2026-10-19  标题"),
        (32, "2026-10-19 12:34  标题"),
    ]
    for width, expected in expectations:
        panel = GitLogPanel()
        panel.show_commits([commit], width=width)
        assert panel.options[0].prompt.plain == expected
    # 只有日期、没有时间的输入（老测试数据 / 手工构造）在宽档退化为日期
    date_only = Commit(
        sha="a1b2c3d", author="zoe", date="2026-10-19", subject="标题"
    )
    panel = GitLogPanel()
    panel.show_commits([date_only], width=32)
    assert panel.options[0].prompt.plain == "2026-10-19  标题"


def test_git_panel_same_data_keeps_highlight():
    """3s 定时刷新内容没变时不得重建选项（否则高亮/滚动被重置）。"""
    commit = Commit(
        sha="a1b2c3d", author="zoe", date="2026-10-19 12:34", subject="标题"
    )
    panel = GitLogPanel()
    panel.show_commits([commit], width=40)
    panel.highlighted = 0
    panel.show_commits([commit], width=40)
    assert panel.highlighted == 0
    assert panel.option_count == 1


def test_commit_card_text_contains_full_subject_and_meta():
    """悬停卡片：完整标题 + 正文 + 短 sha + 完整日期时间 + 作者。"""
    commit = Commit(
        sha="a1b2c3d",
        author="alice",
        date="2026-10-19 12:34",
        subject="feat: 一个非常非常长的提交标题必须完整显示",
        body="正文第一行\n正文第二行",
    )
    text = commit_card_text(commit).plain
    assert "feat: 一个非常非常长的提交标题必须完整显示" in text
    assert "正文第一行\n正文第二行" in text
    assert "a1b2c3d" in text
    assert "2026-10-19 12:34" in text
    assert "alice" in text


def test_hover_card_default_delay_is_one_second():
    """产品默认悬停 1s 才显示（测试里可 monkeypatch 压缩）。"""
    from myrtui.client import git_panel

    assert git_panel.HOVER_CARD_DELAY == 1.0


def test_git_panel_hover_card_after_delay(backend, tmp_path, monkeypatch):
    """悬停满设定时间才弹卡片；指针可移进卡片，离开后隐藏。"""
    from myrtui.client import git_panel

    monkeypatch.setattr(git_panel, "HOVER_CARD_DELAY", 0.05)
    root = str(tmp_path / "proj")
    os.makedirs(root)
    _make_repo(root, commits=1)

    async def scenario():
        app = TerminalApp(backend, root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            panel = app.query_one(GitLogPanel)
            card = app.query_one("#commit-card", CommitCard)
            assert panel.option_count == 1
            assert not card.display

            # 直接走悬停意图入口：计时到点前不得显示
            panel._hover_row(0, panel.region.y)
            assert not card.display

            await pilot.pause(0.1)
            assert card.display
            assert card.region.x > panel.region.right  # 不盖住侧栏/悬停行
            assert card.region.right <= 120
            assert git_panel.CARD_MIN_WIDTH <= card.region.width <= git_panel.CARD_MAX_WIDTH
            rendered = card.query_one("#commit-card-body", Static).render()
            plain = rendered.plain if hasattr(rendered, "plain") else str(rendered)
            assert "feat: 第 0 次提交" in plain
            assert panel._commits[0].sha in plain
            assert panel._commits[0].date in plain

            # 真实 MouseMove：进入列表 → 弹出卡片 → 移进卡片（跨 1 格空隙）
            panel._cancel_hover()
            await pilot.hover(panel, offset=(1, 0))
            await pilot.pause(0.1)
            assert card.display
            await pilot.hover(card, offset=(2, 1))
            await pilot.pause(git_panel.HOVER_CARD_GRACE + 0.1)
            assert card.display, "指针移进卡片后不应被隐藏"

            # 离开卡片到 pane → 无选区 → 隐藏
            await pilot.hover(app.query_one("#pane-0"), offset=(1, 1))
            await pilot.pause(0.1)
            assert not card.display

    asyncio.run(scenario())


def test_git_panel_hover_card_text_is_selectable_and_copyable(
    backend, tmp_path, monkeypatch
):
    """卡片内容可用鼠标拖动选中，``Ctrl-a c`` 复制到内部缓冲/系统剪贴板。"""
    from myrtui.client import clipboard, git_panel

    copied: list[str] = []
    monkeypatch.setattr(
        clipboard,
        "copy_to_system_clipboard",
        lambda text: copied.append(text) or True,
    )
    monkeypatch.setattr(git_panel, "HOVER_CARD_DELAY", 0.05)
    root = str(tmp_path / "proj")
    os.makedirs(root)
    _make_repo(root, commits=1)
    _git(
        root,
        "commit",
        "--allow-empty",
        "-m",
        "feat: body commit\n\nbody line one\nbody line two",
    )

    async def scenario():
        app = TerminalApp(backend, root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            panel = app.query_one(GitLogPanel)
            card = app.query_one("#commit-card", CommitCard)
            await pilot.hover(panel, offset=(1, 0))
            await pilot.pause(0.1)
            assert card.display
            body = card.query_one("#commit-card-body", Static).render().plain
            assert "body line one" in body
            assert "body line two" in body
            assert card.region.height >= 5

            await pilot.mouse_down(card, offset=(2, 1))
            await pilot.hover(card, offset=(20, 1))
            await pilot.mouse_up(card, offset=(20, 1))
            await pilot.pause(0.05)
            selected = app.screen.get_selected_text()
            assert selected and "feat: body commit" in selected, selected
            assert card.display, "选中后卡片不应被鼠标离开逻辑误关"

            await pilot.press("ctrl+a", "c")
            await pilot.pause(0.2)
            assert app._copy_buffer == selected
            assert copied and copied[-1] == selected

    asyncio.run(scenario())


def test_git_panel_long_body_card_scrolls(backend, tmp_path, monkeypatch):
    """超长正文不让卡片撑出屏幕：限高 80% 并可在卡片内滚动。"""
    from myrtui.client import git_panel

    monkeypatch.setattr(git_panel, "HOVER_CARD_DELAY", 0.05)
    root = str(tmp_path / "proj")
    os.makedirs(root)
    _make_repo(root, commits=1)
    body = "\n".join(f"body line {i}" for i in range(80))
    _git(root, "commit", "--allow-empty", "-m", f"feat: long body\n\n{body}")

    async def scenario():
        app = TerminalApp(backend, root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            panel = app.query_one(GitLogPanel)
            card = app.query_one("#commit-card", CommitCard)
            await pilot.hover(panel, offset=(1, 0))
            await pilot.pause(0.1)
            assert card.display
            assert card.region.height <= int(40 * 0.8) + 1
            assert card.max_scroll_y > 0

    asyncio.run(scenario())


def test_git_panel_truncates_by_cell_width_cjk():
    """CJK 标题按 cell 宽截断（不得用 len()，N9）。"""
    from rich.cells import cell_len

    commits = [
        Commit(
            sha="a1b2c3d", author="t", date="2026-10-19 12:34", subject="中中中中中中"
        ),
    ]
    panel = GitLogPanel()
    panel.show_commits(commits, width=24)
    text = panel.options[0].prompt.plain
    assert cell_len(text) <= 24, text


# ---------- 集成：文件树 ↔ 历史面板 ----------

def test_select_file_switches_git_log_to_file_level(backend, tmp_path):
    """选中文件 → 标题变 GIT <branch> · <相对路径>，列表切为该文件的历史（验收 5）。"""
    root = str(tmp_path / "proj")
    os.makedirs(root)
    _make_repo(root, commits=1)
    with open(os.path.join(root, "a.txt"), "w") as f:
        f.write("x")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "add a.txt")
    branch = _branch(root)

    async def scenario():
        app = TerminalApp(backend, root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.2)
            panel = app.query_one(GitLogPanel)
            assert app.query_one("#gitlog-title").render() == f"GIT {branch}"
            repo_texts = _panel_text(panel)
            assert len(repo_texts) == 2  # 仓库级：两次提交
            tree = app.query_one(FileTree)
            line = _tree_lines(tree).index("a.txt")
            await pilot.click(FileTree, offset=(4, line))
            await pilot.pause(1.2)
            assert app.query_one("#gitlog-title").render() == f"GIT {branch} · a.txt"
            assert app._log_target == "a.txt"
            texts = _panel_text(panel)
            # 文件级：a.txt 自己的提交必须在（最近提交在最上），且列表是仓库级
            # 列表的子集。
            # 注：git 的 --follow 会把**仓库的根提交**也带进文件级历史——那是
            # git 自身的行为（根提交相对空树「新增」了所有文件），不是本项目
            # 的 bug，见 P3 方案 §14.3 修订记录 #11。
            assert any("add a.txt" in t for t in texts), texts
            assert "add a.txt" in texts[0]
            assert all(t in repo_texts for t in texts), texts

    asyncio.run(scenario())


def test_select_directory_returns_to_repo_level(backend, tmp_path):
    """选中目录 → 回到仓库级（标题 GIT <branch>）。"""
    root = str(tmp_path / "proj")
    os.makedirs(os.path.join(root, "sub"))
    _make_repo(root, commits=1)
    with open(os.path.join(root, "a.txt"), "w") as f:
        f.write("x")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "add a.txt")
    branch = _branch(root)

    async def scenario():
        app = TerminalApp(backend, root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.2)
            tree = app.query_one(FileTree)
            lines = _tree_lines(tree)
            file_line = lines.index("a.txt")
            dir_line = lines.index("sub")
            await pilot.click(FileTree, offset=(4, file_line))
            await pilot.pause(1.0)
            assert app._log_target == "a.txt"
            await pilot.click(FileTree, offset=(4, dir_line))
            await pilot.pause(1.0)
            assert app._log_target is None
            assert app.query_one("#gitlog-title").render() == f"GIT {branch}"

    asyncio.run(scenario())


def test_git_log_non_repo_shows_message(backend, tmp_path):
    """非 git 目录 → 面板显示「非 git 仓库」（验收 7）。"""
    root = str(tmp_path / "plain")
    os.makedirs(root)

    async def scenario():
        app = TerminalApp(backend, root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.5)
            panel = app.query_one(GitLogPanel)
            assert _panel_text(panel) == ["非 git 仓库"]

    asyncio.run(scenario())


def test_git_log_empty_repo_shows_message(backend, tmp_path):
    """空仓库 → 面板显示「当前分支暂无提交」（**不得**显示「git 不可用」）。"""
    root = str(tmp_path / "empty")
    os.makedirs(root)
    _git(root, "init", "-q", ".")

    async def scenario():
        app = TerminalApp(backend, root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.5)
            panel = app.query_one(GitLogPanel)
            assert _panel_text(panel) == ["当前分支暂无提交"]

    asyncio.run(scenario())


# ---------- 集成：取数竞态（D7） ----------

def test_git_log_last_write_wins_discards_stale(backend, tmp_path):
    """目标已变 → 过期结果丢弃（last-write-wins，D7 #2）。"""
    root = str(tmp_path / "proj")
    os.makedirs(root)
    _make_repo(root, commits=1)

    async def scenario():
        app = TerminalApp(backend, root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            panel = app.query_one(GitLogPanel)
            before = _panel_text(panel)
            # 模拟「取数期间目标被改掉」：直接走回投关口
            app._log_target = "b.txt"
            app._apply_git_log(
                "a.txt",
                "ok",
                [Commit(sha="deadbee", author="x", date="2026-01-01", subject="过期")],
            )
            await pilot.pause(0.3)
            assert _panel_text(panel) == before
            assert "过期" not in " ".join(_panel_text(panel))

    asyncio.run(scenario())


def test_git_log_timer_keeps_current_file_target(backend, tmp_path, monkeypatch):
    """3s 定时器不得把面板弹回仓库级（单一当前目标，D7 #7）。

    把刷新周期缩到 0.2s 加速验证：定时器刷的也是**当前目标**。
    """
    monkeypatch.setattr("myrtui.client.app.GITLOG_INTERVAL", 0.2)
    root = str(tmp_path / "proj")
    os.makedirs(root)
    _make_repo(root, commits=1)
    with open(os.path.join(root, "a.txt"), "w") as f:
        f.write("x")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "add a.txt")
    branch = _branch(root)

    async def scenario():
        app = TerminalApp(backend, root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            tree = app.query_one(FileTree)
            line = _tree_lines(tree).index("a.txt")
            await pilot.click(FileTree, offset=(4, line))  # 选中 a.txt
            await pilot.pause(0.8)
            assert app._log_target == "a.txt"
            # 让定时器跑好几轮
            for _ in range(12):
                await pilot.pause(0.1)
            assert app._log_target == "a.txt", "定时器不得改目标"
            assert app.query_one("#gitlog-title").render() == f"GIT {branch} · a.txt"

    asyncio.run(scenario())


# ---------- 集成：焦点模型（D8） ----------

def _spy_keys(backend):
    """记录注入内层 shell 的键/文本（焦点模型断言用）。"""
    sent: list[tuple[str, tuple]] = []
    orig_keys = backend.send_keys
    orig_text = backend.send_text

    def send_keys(pane_id, *keys):
        sent.append(("keys", keys))
        return orig_keys(pane_id, *keys)

    def send_text(pane_id, text):
        sent.append(("text", (text,)))
        return orig_text(pane_id, text)

    backend.send_keys = send_keys
    backend.send_text = send_text
    return sent


def test_sidebar_focus_blocks_keys(backend, tmp_path):
    """焦点在侧栏 → 键不进 shell（可打印键 + ↑↓ 两键裁决用例，D8）。"""
    root = str(tmp_path / "proj")
    os.makedirs(root)
    with open(os.path.join(root, "a.txt"), "w") as f:
        f.write("x")

    async def scenario():
        app = TerminalApp(backend, root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.8)
            sent = _spy_keys(backend)
            # Ctrl-a s → 焦点进侧栏
            await pilot.press("ctrl+a")
            await pilot.press("s")
            await pilot.pause(0.4)
            assert app._sidebar_focused()
            sent.clear()
            tree = app.query_one(FileTree)
            # 可打印键：不得进 shell
            await pilot.press("a")
            await pilot.pause(0.3)
            assert sent == [], f"侧栏聚焦时键不得透传给 shell: {sent}"
            # ↑↓：不得进 shell，且树的光标照常移动
            await pilot.press("down")
            await pilot.pause(0.3)
            assert tree.cursor_line == 1, "树光标应照常移动"
            await pilot.press("up")
            await pilot.pause(0.3)
            assert tree.cursor_line == 0
            assert sent == [], f"侧栏聚焦时方向键不得透传给 shell: {sent}"

    asyncio.run(scenario())


def test_sidebar_focus_blocks_paste(backend, tmp_path):
    """焦点在侧栏 → 粘贴不进 shell（D8 决策 4）。"""
    root = str(tmp_path / "proj")
    os.makedirs(root)

    async def scenario():
        app = TerminalApp(backend, root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.8)
            pasted: list[str] = []
            orig_paste = backend.paste

            def paste(pane_id, text):
                pasted.append(text)
                return orig_paste(pane_id, text)

            backend.paste = paste
            await pilot.press("ctrl+a")
            await pilot.press("s")
            await pilot.pause(0.4)
            assert app._sidebar_focused()
            app.post_message(Paste("PASTE-MARKER"))
            await pilot.pause(0.5)
            assert pasted == [], "侧栏聚焦时粘贴不得进 shell"

    asyncio.run(scenario())


def test_escape_returns_focus_to_pane(backend, tmp_path):
    """Esc → 焦点回活动 pane（D8 决策 2 / 验收 9）。"""
    root = str(tmp_path / "proj")
    os.makedirs(root)

    async def scenario():
        app = TerminalApp(backend, root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.8)
            await pilot.press("ctrl+a")
            await pilot.press("s")
            await pilot.pause(0.4)
            assert app._sidebar_focused()
            await pilot.press("escape")
            await pilot.pause(0.4)
            assert not app._sidebar_focused()
            assert isinstance(app.focused, PaneWidget)

    asyncio.run(scenario())


def test_ctrl_a_s_focuses_sidebar_without_mouse(backend, tmp_path):
    """Ctrl-a s → 焦点移入侧栏（无鼠标路径，验收 10）。"""
    root = str(tmp_path / "proj")
    os.makedirs(root)

    async def scenario():
        app = TerminalApp(backend, root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.8)
            assert not app._sidebar_focused()
            await pilot.press("ctrl+a")
            await pilot.press("s")
            await pilot.pause(0.4)
            assert isinstance(app.focused, FileTree)
            status = app.query_one("#topbar-status").render()
            assert "侧栏已聚焦" in status.plain

    asyncio.run(scenario())


def test_ctrl_a_b_collapses_sidebar_and_resizes_pane(backend, tmp_path):
    """Ctrl-a b → 侧栏消失、pane 变宽，resize-window 联动生效（验收 8）。"""
    root = str(tmp_path / "proj")
    os.makedirs(root)

    async def scenario():
        app = TerminalApp(backend, root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.8)
            resized: list[tuple[int, int]] = []
            orig_resize = backend.resize_window

            def resize_window(window_id, cols, rows):
                resized.append((cols, rows))
                return orig_resize(window_id, cols, rows)

            backend.resize_window = resize_window
            before = app.query_one("#pane-0", PaneWidget).size.width
            await pilot.press("ctrl+a")
            await pilot.press("b")
            await pilot.pause(0.8)
            assert app.query_one("#sidebar").display is False
            after = app.query_one("#pane-0", PaneWidget).size.width
            assert after > before
            assert resized, "resize-window 应被调用"
            assert resized[-1][0] == after
            # 再按一次复原
            await pilot.press("ctrl+a")
            await pilot.press("b")
            await pilot.pause(0.8)
            assert app.query_one("#sidebar").display is True
            assert app.query_one("#pane-0", PaneWidget).size.width == before

    asyncio.run(scenario())
