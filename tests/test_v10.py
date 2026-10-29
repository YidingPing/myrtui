"""myrtui v0.10 测试：侧栏改版。

覆盖本轮四项改动：

1. 顶部摘要（``FILES n`` / ``GIT branch ±N``）删除；
2. Git 区块标题改成 ``GIT <branch>``（选中文件时 ``GIT <branch> · <相对路径>``）；
3. 提交行去掉日期 / 作者两列（只留 sha + 标题；后续版本已改为
   日期 + 标题 + 悬停卡片，见下方控件用例）；
4. 文件树按 git 状态着色（VS Code 习惯：修改=金、新增/未跟踪=绿、删除=红、
   重命名=青、冲突=红；**目录按子项继承**）。

分层：纯函数（``parse_file_status``）→ I/O（``git_file_status``）→ 控件
（标题渲染 / 树着色）。
"""
from __future__ import annotations

import asyncio
import os
import subprocess

import pytest
from rich.style import Style
from textual.app import App, ComposeResult

from myrtui.client.file_tree import LABEL_OFFSET, STATUS_COLORS, FileTree
from myrtui.client.git_panel import GitLogPanel
from myrtui.client.sidebar import Sidebar
from myrtui.gitlog import Commit
from myrtui.gitstat import git_file_status, parse_file_status


# ---------- 夹具 / 辅助 ----------

def _git(repo: str, *args: str) -> None:
    subprocess.run(
        ["git", "-C", repo, *args],
        check=True,
        capture_output=True,
        timeout=30,
    )


def _require_git() -> str:
    from shutil import which

    git = which("git")
    if not git:
        pytest.skip("git 未安装")
    return git


def _make_repo(root: str, *, commits: int = 1) -> str:
    """在 root 建一个真实仓库（默认 1 次提交）。"""
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


def _find_node(tree: FileTree, label: str):
    """按标签找树节点（深度优先）。"""

    def walk(node):
        for child in node.children:
            if str(child.label.plain) == label:
                return child
            found = walk(child)
            if found is not None:
                return found
        return None

    return walk(tree.root)


def _label_color(tree: FileTree, node) -> str | None:
    """节点标签（文件名部分）的实际前景色。

    用 ``Text.get_style_at_offset``——它按 span 顺序叠加，**后叠的覆盖先叠的**
    （与渲染器口径一致），避免「基类的扩展名高亮色」与「git 状态色」混淆。
    """
    text = tree.render_label(node, Style(), Style())
    style = text.get_style_at_offset(tree.app.console, LABEL_OFFSET)
    return style.color.name if style.color is not None else None


class _SidebarApp(App):
    """只挂一个侧栏的最小 App（避免为侧栏测试起 tmux）。"""

    def __init__(self, root: str) -> None:
        super().__init__()
        self.root_path = root

    def compose(self) -> ComposeResult:
        yield Sidebar(self.root_path, id="sidebar")


# ---------- 纯函数：parse_file_status ----------

def test_parse_file_status_basic_codes():
    """porcelain 的 XY 两列 → 归一化状态码。"""
    records = [" M a.txt", "A  b.txt", "?? c.txt", " D d.txt"]
    assert parse_file_status(records) == {
        "a.txt": "M",
        "b.txt": "A",
        "c.txt": "A",  # 未跟踪按新增着色
        "d.txt": "D",
    }


def test_parse_file_status_rename_skips_old_path():
    """重命名记录是 ``XY new\\0old\\0``——``old`` 那条无状态前缀，须跳过。"""
    records = ["RM new.txt", "old.txt", " M other.txt"]
    assert parse_file_status(records) == {"new.txt": "R", "other.txt": "M"}


def test_parse_file_status_strips_repo_prefix():
    """``prefix`` = 仓库根相对项目根的前缀；不在前缀下的条目丢弃。"""
    records = [" M sub/a.txt", " M other.txt"]
    assert parse_file_status(records, "sub/") == {"a.txt": "M"}


def test_parse_file_status_conflict_typechange_and_staged():
    """冲突 / 类型变更 / 已暂存新增的归一化。"""
    out = parse_file_status(["UU a.txt", " T b.txt", "AM c.txt", "MD d.txt"])
    assert out["a.txt"] == "U"  # 冲突
    assert out["b.txt"] == "M"  # 类型变更按修改
    assert out["c.txt"] == "A"  # 已暂存新增
    assert out["d.txt"] == "D"  # 改了又删了 → 删除


def test_parse_file_status_ignores_empty_and_junk():
    assert parse_file_status([]) == {}
    assert parse_file_status([""]) == {}
    assert parse_file_status(["!! ignored.txt"]) == {}


# ---------- I/O：git_file_status ----------

def test_git_file_status_real_repo(tmp_path):
    """真实仓库：修改 / 新增 / 删除各归其位（路径相对项目根）。"""
    _require_git()
    root = str(tmp_path / "repo")
    _make_repo(root)
    with open(os.path.join(root, "f0.txt"), "w") as f:
        f.write("changed")  # 已提交过 → 修改
    with open(os.path.join(root, "new.txt"), "w") as f:
        f.write("x")  # 未跟踪 → 新增
    os.remove(os.path.join(root, "f0.txt"))  # 删掉 → 只剩删除
    status, files = git_file_status(root)
    assert status == "ok"
    assert files == {"f0.txt": "D", "new.txt": "A"}


def test_git_file_status_non_repo(tmp_path):
    """非仓库 → ("not-repo", {})。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    status, files = git_file_status(str(plain))
    assert status == "not-repo"
    assert files == {}


def test_git_file_status_missing_git_binary(tmp_path):
    """git 缺失 → ("unavailable", {})，不抛错。"""
    plain = tmp_path / "plain2"
    plain.mkdir()
    status, files = git_file_status(str(plain), git_bin="/nonexistent/git")
    assert status == "unavailable"
    assert files == {}


def test_git_file_status_root_is_subdirectory(tmp_path):
    """项目根是仓库的**子目录** → 路径相对项目根（剥掉仓库前缀）。"""
    _require_git()
    repo = str(tmp_path / "repo2")
    _make_repo(repo)
    sub = os.path.join(repo, "sub")
    os.makedirs(sub, exist_ok=True)
    with open(os.path.join(sub, "a.txt"), "w") as f:
        f.write("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add sub")
    with open(os.path.join(sub, "a.txt"), "w") as f:
        f.write("changed")
    with open(os.path.join(repo, "outside.txt"), "w") as f:
        f.write("x")
    status, files = git_file_status(sub)
    assert status == "ok"
    assert files == {"a.txt": "M"}  # 仓库根下的 outside.txt 不在项目子树内


# ---------- 控件：标题 ----------

def test_sidebar_title_shows_branch(tmp_path):
    """仓库内 → 标题 ``GIT <branch>``。"""
    _require_git()
    root = str(tmp_path / "repo3")
    _make_repo(root)
    sidebar = Sidebar(root)
    sidebar.refresh_data()
    branch = subprocess.run(
        ["git", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert sidebar._title == f"GIT {branch}"


def test_sidebar_title_non_repo(tmp_path):
    """非仓库 → ``GIT -``。"""
    plain = tmp_path / "plain3"
    plain.mkdir()
    sidebar = Sidebar(str(plain))
    sidebar.refresh_data()
    assert sidebar._title == "GIT -"


def test_sidebar_title_git_unavailable(monkeypatch, tmp_path):
    """git 不可用 → ``GIT ?``。"""
    from myrtui.gitstat import GitInfo

    monkeypatch.setattr(
        "myrtui.client.sidebar.git_status",
        lambda *a, **k: ("unavailable", None),
    )
    sidebar = Sidebar(str(tmp_path))
    sidebar.refresh_data()
    assert sidebar._title == "GIT ?"


def test_sidebar_title_with_file_scope(tmp_path):
    """选中文件 → 标题追加 `` · <相对路径>``（分支段保持完整）。"""
    _require_git()
    root = str(tmp_path / "repo4")
    _make_repo(root)
    sidebar = Sidebar(root)
    sidebar.refresh_data()
    branch = sidebar._git.branch
    sidebar.set_scope("a.txt")
    assert sidebar._title == f"GIT {branch} · a.txt"
    sidebar.set_scope(None)
    assert "·" not in sidebar._title


def test_sidebar_title_truncates_scope_by_cell(tmp_path):
    """超长路径按 cell 截断（不溢出侧栏宽度）。"""
    from rich.cells import cell_len

    _require_git()
    root = str(tmp_path / "repo5")
    _make_repo(root)
    sidebar = Sidebar(root)
    sidebar.refresh_data()
    sidebar.set_scope("a/" * 40 + "file.txt")
    assert cell_len(sidebar._title) <= 24, sidebar._title
    assert "…" in sidebar._title


# ---------- 控件：提交行 ----------

def test_git_panel_row_is_date_and_subject_only():
    """提交行已改为 日期 + 标题（sha / 作者在悬停卡片里）。"""
    commits = [
        Commit(sha="a1b2c3d", author="zoe", date="2026-10-19", subject="标题"),
    ]
    panel = GitLogPanel()
    panel.show_commits(commits, width=40)
    text = panel.options[0].prompt.plain
    assert text == "2026-10-19  标题"
    assert "zoe" not in text
    assert "a1b2c3d" not in text


# ---------- 控件：文件树着色 ----------

def test_file_tree_set_status_map_colors_nodes(tmp_path):
    """``set_status_map`` → 文件名按状态着色（金 / 绿 / 红 / 青）。"""
    root = str(tmp_path / "tree")
    os.makedirs(root, exist_ok=True)
    for name in ("mod.txt", "add.txt", "del.txt", "ren.txt"):
        with open(os.path.join(root, name), "w") as f:
            f.write("x")

    async def scenario():
        app = _SidebarApp(root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            tree = app.query_one(FileTree)
            tree.set_status_map(
                {
                    "mod.txt": "M",
                    "add.txt": "A",
                    "del.txt": "D",
                    "ren.txt": "R",
                }
            )
            await pilot.pause(0.2)
            for label, code in (
                ("mod.txt", "M"),
                ("add.txt", "A"),
                ("del.txt", "D"),
                ("ren.txt", "R"),
            ):
                node = _find_node(tree, label)
                assert node is not None, label
                assert _label_color(tree, node) == STATUS_COLORS[code], label

    asyncio.run(scenario())


def test_file_tree_directory_inherits_child_color(tmp_path):
    """目录按子项继承颜色（VS Code 习惯），且优先级：新增 > 修改。"""
    root = str(tmp_path / "tree2")
    os.makedirs(os.path.join(root, "sub"), exist_ok=True)
    for name in ("sub/mod.txt", "sub/new.txt", "plain.txt"):
        with open(os.path.join(root, name), "w") as f:
            f.write("x")

    async def scenario():
        app = _SidebarApp(root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            tree = app.query_one(FileTree)
            tree.set_status_map({"sub/mod.txt": "M", "sub/new.txt": "A"})
            await pilot.pause(0.2)
            sub = _find_node(tree, "sub")
            assert sub is not None
            # sub 下既有修改又有新增 → 取优先级更高的「新增」绿
            assert _label_color(tree, sub) == STATUS_COLORS["A"]
            # 根目录继承全部子项 → 也是「新增」
            root_node = tree.root
            assert _label_color(tree, root_node) == STATUS_COLORS["A"]

    asyncio.run(scenario())


def test_file_tree_status_map_refresh_recolors(tmp_path):
    """状态变化后重新灌表 → 颜色随之更新（行缓存被清掉）。"""
    root = str(tmp_path / "tree3")
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "a.txt"), "w") as f:
        f.write("x")

    async def scenario():
        app = _SidebarApp(root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            tree = app.query_one(FileTree)
            node = _find_node(tree, "a.txt")
            assert node is not None
            assert _label_color(tree, node) is None  # 无状态 → 不着色
            tree.set_status_map({"a.txt": "M"})
            await pilot.pause(0.2)
            assert _label_color(tree, node) == STATUS_COLORS["M"]
            tree.set_status_map({"a.txt": "A"})
            await pilot.pause(0.2)
            assert _label_color(tree, node) == STATUS_COLORS["A"]

    asyncio.run(scenario())


def test_sidebar_feeds_tree_status_from_real_repo(tmp_path):
    """端到端：侧栏的 per-file 轮询把真实仓库的状态灌进树。"""
    _require_git()
    root = str(tmp_path / "repo6")
    _make_repo(root)
    with open(os.path.join(root, "f0.txt"), "w") as f:
        f.write("changed")
    with open(os.path.join(root, "new.txt"), "w") as f:
        f.write("x")

    async def scenario():
        app = _SidebarApp(root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(1.0)  # 等首轮 per-file 状态
            tree = app.query_one(FileTree)
            assert tree._status_map.get("f0.txt") == "M"
            assert tree._status_map.get("new.txt") == "A"

    asyncio.run(scenario())
