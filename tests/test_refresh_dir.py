"""「刷新此目录」回归（v0.30 能力，v0.34 上传移除时从 test_v30 迁出）。

v0.30 的目录右键菜单有两半：上传（v0.34 整体移除，见
``docs-internal/docs/legacy/V34-REMOVE-UPLOAD-PLAN.md``）与「刷新此目录」。本文件只守后者：

- 菜单结构：文件行不含目录动作，目录行含 ``refresh_dir``；
- 刷新语义：新增文件可见、其它展开与光标保持、根节点、目录被删刷父目录、
  节点不在树里走全树兜底；
- 刷新不碰 Git 历史目标、不调用 tmux 后端；
- 外部改动（v0.53 的 git 快照 + 0.5.5 的扫盘信号）按「行集合变没变」落到树上。
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from textual.widgets import OptionList

from myrtui.client.app import MainScreen, TerminalApp
from myrtui.client.file_tree import MAX_SCAN_ENTRIES, FileTree, scan_dirs
from myrtui.client.sidebar import Sidebar
from myrtui.tmux_backend import CaptureResult, PaneInfo


def _pane(index: int = 0) -> PaneInfo:
    return PaneInfo(
        pane_id=f"p{index}",
        dead=False,
        command="sh",
        alternate_on=False,
        cursor_x=0,
        cursor_y=0,
        cursor_flag=False,
        history_size=10,
        height=24,
        width=80,
        window_id=f"w{index}",
        window_index=index,
        mouse_standard=False,
        mouse_button=False,
        mouse_any=False,
        mouse_sgr=True,
        mouse_all=False,
    )


class _FakeBackend:
    """假后端：拓扑 + 记录 pane 注入（不碰 tmux）。"""

    def __init__(self, panes: list[PaneInfo]):
        self.panes = list(panes)
        self.commands: list[tuple[str, str]] = []
        self.sent: list[tuple] = []

    def list_panes(self):
        return list(self.panes)

    def poll(self, offsets):
        return list(self.panes), {
            p.pane_id: CaptureResult(text="", offset=0) for p in self.panes
        }

    def resize_window(self, window_id, cols, rows):
        pass

    def run_command(self, pane_id, command):
        self.commands.append((pane_id, command))

    def send_text(self, pane_id, text):
        self.sent.append(("text", text))

    def send_keys(self, pane_id, *keys):
        self.sent.append(("keys", keys))

    def send_ctrl_a(self, pane_id):
        self.sent.append(("ctrl_a", pane_id))


def _project(tmp_path: Path) -> str:
    """项目根放在 ``tmp_path/proj``。"""
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    (root / "a.txt").write_text("a\n")
    (root / "notes.md").write_text("# notes\n")
    (root / "sub").mkdir(exist_ok=True)
    (root / "sub" / "b.txt").write_text("b\n")
    (root / "sib").mkdir(exist_ok=True)
    (root / "sib" / "c.txt").write_text("c\n")
    return str(root)


def _make_app(tmp_path: Path) -> TerminalApp:
    return TerminalApp(_FakeBackend([_pane()]), _project(tmp_path))


def _tree_lines(tree: FileTree) -> list[str]:
    return [str(line.path[-1].label.plain) for line in tree._tree_lines]


def _node(tree: FileTree, label: str):
    return next(child for child in tree.root.children if child.label.plain == label)


async def _open_menu(pilot, tree: FileTree, label: str, x: int = 4) -> int:
    line = _tree_lines(tree).index(label)
    await pilot.click(FileTree, offset=(x, line), button=3)
    await pilot.pause(0.25)
    return line


def _menu_ids(app) -> list[str | None]:
    menu = app.screen.query_one(OptionList)
    return [option.id for option in menu.options]


# ---------- 菜单结构 ----------

def test_file_menu_has_no_directory_actions(tmp_path):
    """文件行菜单只有文件动作：不含「刷新此目录」等目录项。"""
    app = _make_app(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            await _open_menu(pilot, app.query_one(FileTree), "a.txt")
            ids = _menu_ids(app)
            assert "refresh_dir" not in ids
            assert {"copy_rel", "copy_abs"} <= set(ids)
            assert "view" in ids
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert isinstance(app.screen, MainScreen)

    asyncio.run(scenario())


def test_refresh_does_not_touch_git_or_pane(tmp_path):
    """刷新目录是纯前端动作：不改 Git 历史目标、不调用 tmux 后端。"""
    app = _make_app(tmp_path)
    backend = app.backend

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            app._refresh_tree_dir(
                "sub", str((tmp_path / "proj" / "sub").resolve())
            )
            await pilot.pause(0.3)

            assert app._log_target is None
            assert backend.commands == []
            assert backend.sent == []

    asyncio.run(scenario())


# ---------- 刷新语义 ----------

def test_refresh_directory_shows_new_file(tmp_path):
    app = _make_app(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            (tmp_path / "proj" / "sub" / "new.txt").write_text("new\n")
            app._refresh_tree_dir(
                "sub", str((tmp_path / "proj" / "sub").resolve())
            )
            await pilot.pause(0.5)

            assert "new.txt" in _tree_lines(tree)
            assert _node(tree, "sub").is_expanded

    asyncio.run(scenario())


def test_refresh_keeps_other_expansion_and_cursor(tmp_path):
    app = _make_app(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            sub = _node(tree, "sub")
            sib = _node(tree, "sib")
            sub.expand()
            sib.expand()
            await pilot.pause(0.4)
            tree.cursor_line = _tree_lines(tree).index("b.txt")
            await pilot.pause(0.1)

            (tmp_path / "proj" / "sub" / "new.txt").write_text("new\n")
            app._refresh_tree_dir(
                "sub", str((tmp_path / "proj" / "sub").resolve())
            )
            await pilot.pause(0.5)

            assert _node(tree, "sib").is_expanded
            assert "c.txt" in _tree_lines(tree)
            assert tree.cursor_node is not None
            assert tree.cursor_node.data.path.name == "b.txt"

    asyncio.run(scenario())


def test_refresh_root_shows_new_file(tmp_path):
    app = _make_app(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            (tmp_path / "proj" / "new-root.txt").write_text("new\n")
            app._refresh_tree_dir("", str(tmp_path / "proj"))
            await pilot.pause(0.5)

            assert "new-root.txt" in _tree_lines(tree)

    asyncio.run(scenario())


def test_refresh_deleted_dir_refreshes_parent(tmp_path):
    app = _make_app(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            target_dir = tmp_path / "proj" / "sub"
            absolute = str(target_dir.resolve())
            shutil.rmtree(target_dir)
            app._refresh_tree_dir("sub", absolute)
            await pilot.pause(0.5)

            assert "sub" not in _tree_lines(tree)
            assert "上级目录" in app._notice_text

    asyncio.run(scenario())


def test_refresh_missing_node_falls_back(tmp_path):
    app = _make_app(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            assert tree.reload_path("no-such") is False
            await pilot.pause(0.4)
            assert isinstance(app.screen, MainScreen)

    asyncio.run(scenario())


# ---------- 外部改动（v0.53：树跟着磁盘走） ----------
#
# 这里直接喂 ``sync_status`` 的快照，不跑真 git：驱动这份快照的是
# ``Sidebar._refresh_file_status_worker``（3 s 一轮），本文件只守「快照怎么变，
# 树就怎么变」。所有断言落在**结构**上（行在不在、展开没展开、节点对象有没有被
# 换掉），不写固定 sleep，也不用耗时判断。


def _forbid_full_reload(tree: FileTree) -> None:
    """把全树 ``reload()`` 换成炸弹：自动刷新一旦走兜底就立刻炸掉。"""

    def _boom():
        raise AssertionError("外部刷新不能触发全树 reload()")

    tree.reload = _boom


def test_sync_status_row_exists_does_not_touch_structure(tmp_path):
    """行还在（只有状态码变）→ 只配色，节点对象与光标都不动。"""
    app = _make_app(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            _forbid_full_reload(tree)
            sub_before = _node(tree, "sub")
            tree.cursor_line = _tree_lines(tree).index("a.txt")
            await pilot.pause(0.1)

            assert tree.sync_status({"a.txt": "M"}) is True
            await pilot.pause(0.3)

            assert tree.sync_status({"a.txt": "M"}) is False  # 同一份快照：不动
            await pilot.pause(0.2)

            assert _node(tree, "sub") is sub_before  # 没有重扫重建
            assert tree.cursor_node is not None
            assert tree.cursor_node.data.path.name == "a.txt"
            assert tree._status_map == {"a.txt": "M"}

    asyncio.run(scenario())


def test_sync_status_shows_new_file_in_expanded_dir(tmp_path):
    """展开的目录里出现新文件 → 行出现，其它展开与光标保持。"""
    app = _make_app(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            _forbid_full_reload(tree)
            _node(tree, "sub").expand()
            _node(tree, "sib").expand()
            await pilot.pause(0.4)
            tree.cursor_line = _tree_lines(tree).index("b.txt")
            await pilot.pause(0.1)

            (tmp_path / "proj" / "sub" / "new.txt").write_text("new\n")
            tree.sync_status({"sub/new.txt": "A"})
            await pilot.pause(0.5)

            assert "new.txt" in _tree_lines(tree)
            assert _node(tree, "sub").is_expanded
            assert _node(tree, "sib").is_expanded
            assert "c.txt" in _tree_lines(tree)
            assert tree.cursor_node is not None
            assert tree.cursor_node.data.path.name == "b.txt"

    asyncio.run(scenario())


def test_sync_status_drops_deleted_row(tmp_path):
    """上一份快照里有、这一份没有、磁盘上也没了 → 死行消失。"""
    app = _make_app(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            _forbid_full_reload(tree)
            _node(tree, "sub").expand()
            await pilot.pause(0.4)
            assert "b.txt" in _tree_lines(tree)

            tree.sync_status({"sub/b.txt": "M"})  # 行还在：只上色，结构不动
            await pilot.pause(0.2)
            assert "b.txt" in _tree_lines(tree)

            (tmp_path / "proj" / "sub" / "b.txt").unlink()
            tree.sync_status({"sub/b.txt": "D"})  # git 报删除
            await pilot.pause(0.5)
            assert "b.txt" not in _tree_lines(tree)
            assert _node(tree, "sub").is_expanded

            # 未跟踪文件被删：快照里整个键消失，靠「上一份 ∪ 这一份」才找得到
            (tmp_path / "proj" / "sub" / "gone.txt").write_text("x\n")
            tree.sync_status({"sub/gone.txt": "A"})
            await pilot.pause(0.4)
            assert "gone.txt" in _tree_lines(tree)
            (tmp_path / "proj" / "sub" / "gone.txt").unlink()
            tree.sync_status({})
            await pilot.pause(0.5)
            assert "gone.txt" not in _tree_lines(tree)

    asyncio.run(scenario())


def test_sync_status_never_expands_a_collapsed_dir(tmp_path):
    """收起但已加载的目录只标脏，不弹开；用户展开时再读到新文件。"""
    app = _make_app(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            _forbid_full_reload(tree)
            _node(tree, "sib").expand()
            await pilot.pause(0.3)
            _node(tree, "sib").collapse()
            await pilot.pause(0.2)
            sib = _node(tree, "sib")
            assert not sib.is_expanded

            (tmp_path / "proj" / "sib" / "later.txt").write_text("x\n")
            tree.sync_status({"sib/later.txt": "A"})
            await pilot.pause(0.4)

            assert not _node(tree, "sib").is_expanded  # 没被弹开
            assert _node(tree, "sib").data.loaded is False  # 标了脏
            assert "later.txt" not in _tree_lines(tree)

            # 用户自己展开 → 基类按 loaded=False 重读盘，新文件出现
            _node(tree, "sib").expand()
            await pilot.pause(0.6)
            assert "later.txt" in _tree_lines(tree)

    asyncio.run(scenario())


def test_sync_status_root_level_new_file(tmp_path):
    """顶层新文件：最近的已加载祖先就是根，根重扫即可（不走全树兜底）。"""
    app = _make_app(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            _forbid_full_reload(tree)
            (tmp_path / "proj" / "top.txt").write_text("x\n")
            tree.sync_status({"top.txt": "A"})
            await pilot.pause(0.5)

            assert "top.txt" in _tree_lines(tree)

    asyncio.run(scenario())


def test_sync_status_works_behind_a_symlinked_root(tmp_path):
    """根路径与子项写法不同（软链根 / macOS ``/var``）时，删除也必须生效。

    ``DirectoryTree._load_directory`` 会把子项 **resolve** 之后建节点，而根节点
    保留调用方给的写法；索引若按文件系统路径建，整棵子树都对不上——表现是外部
    新建的文件能出现（新行走的是「找不到祖先」以外的分支），但外部删除的文件
    永远刷不掉。2026-10-31 实测复现，这里锁死。
    """
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.txt").write_text("a\n")
    (real / "b.txt").write_text("b\n")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    app = TerminalApp(_FakeBackend([_pane()]), str(link))

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.6)
            tree = app.query_one(FileTree)
            _forbid_full_reload(tree)
            assert {"a.txt", "b.txt"} <= set(tree._loaded_nodes())

            tree.sync_status({"a.txt": "A", "b.txt": "A"})
            await pilot.pause(0.3)
            (real / "a.txt").unlink()
            (real / "b.txt").unlink()
            tree.sync_status({})
            await pilot.pause(0.8)

            assert "a.txt" not in _tree_lines(tree)
            assert "b.txt" not in _tree_lines(tree)

    asyncio.run(scenario())


# ---------- 扫盘信号（0.5.5：忽略目录 / 嵌套仓 / 非仓库目录） ----------
#
# 0.5.3 的结构刷新只有 git 快照一个信号源，而 ``git status`` 对 ignore 目录只给
# 一行 ``!! dir/``（``parse_file_status`` 还直接跳过），对嵌套仓只给 ``?? sub/``
# ——里面新加了什么永远不知道。这里守第二条信号：每轮 ``scan_dirs``（线程，纯数据）
# → ``sync_dirs``（事件循环，只增删行，不配色）。


def _sync_now(tree: FileTree) -> bool:
    """跑一轮「扫盘 → 合树」，等价于 worker 里那两拍（测试同步驱动）。"""
    return tree.sync_dirs(scan_dirs(tree.scan_targets()))


async def _wait_until(pilot, predicate, timeout: float = 3.0) -> bool:
    """轮询到条件成立（Textual 时序不确定，固定 sleep 会抖，见 tests/README.md）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await pilot.pause(0.05)
    return predicate()


# ----- 纯函数级 -----


def test_scan_dirs_reports_name_and_kind(tmp_path):
    """一行一个 ``(名字, 是否目录)``：普通文件、目录、空目录、指向目录的软链。"""
    root = tmp_path / "d"
    root.mkdir()
    (root / "file.txt").write_text("x\n")
    (root / "sub").mkdir()
    (root / "empty").mkdir()
    (root / "link").symlink_to(root / "sub", target_is_directory=True)

    assert scan_dirs([("", root)]) == {
        "": {
            ("file.txt", False),
            ("sub", True),
            ("empty", True),
            ("link", True),  # follow_symlinks=True：与树的 _safe_is_dir 同口径
        }
    }


def test_scan_dirs_skips_dot_git(tmp_path):
    """``.git`` 不进结果——与 ``filter_paths`` 共用 ``_visible`` 那一条规则。"""
    root = tmp_path / "d"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "index").write_text("x")
    (root / "a.txt").write_text("a\n")

    assert scan_dirs([("", root)]) == {"": {("a.txt", False)}}


def test_scan_dirs_respects_entry_cap(tmp_path):
    """项数超过 ``MAX_SCAN_ENTRIES`` → 整条目录不返回（跳过，交给手动刷新）。"""
    root = tmp_path / "big"
    root.mkdir()
    for index in range(MAX_SCAN_ENTRIES + 1):
        (root / f"f{index}").write_text("")

    assert scan_dirs([("big", root)]) == {}


def test_scan_dirs_survives_missing_dir(tmp_path):
    """目录在扫描前被删 / 已不是目录 → 跳过该目录，不抛。"""
    (tmp_path / "a-file").write_text("x\n")
    assert scan_dirs([("gone", tmp_path / "gone")]) == {}
    assert scan_dirs([("a-file", tmp_path / "a-file")]) == {}


# ----- 树级 -----


def test_sync_dirs_adds_file_in_ignored_dir(tmp_path):
    """git 看不见的目录里新建文件 → 行出现，其它展开与光标保持。"""
    app = _make_app(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            _forbid_full_reload(tree)
            root = tmp_path / "proj"

            # 目录第一眼是新的：根那一轮先把它加进来
            ignored = root / "docs-internal"
            ignored.mkdir()
            (ignored / "a.md").write_text("a\n")
            _sync_now(tree)
            assert await _wait_until(pilot, lambda: "docs-internal" in _tree_lines(tree))

            _node(tree, "docs-internal").expand()
            _node(tree, "sib").expand()
            await pilot.pause(0.4)
            tree.cursor_line = _tree_lines(tree).index("c.txt")

            # 忽略目录内部的变化：git 快照里什么都没有，只有扫盘看得见
            (ignored / "b.md").write_text("b\n")
            _sync_now(tree)

            assert await _wait_until(pilot, lambda: "b.md" in _tree_lines(tree))
            assert _node(tree, "docs-internal").is_expanded
            assert _node(tree, "sib").is_expanded
            assert tree.cursor_node is not None
            assert tree.cursor_node.data.path.name == "c.txt"

    asyncio.run(scenario())


def test_sync_dirs_drops_deleted_file(tmp_path):
    """删掉的行消失；目录本身不被弹开或收起。"""
    app = _make_app(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            _forbid_full_reload(tree)
            _node(tree, "sub").expand()
            await pilot.pause(0.4)
            assert "b.txt" in _tree_lines(tree)

            (tmp_path / "proj" / "sub" / "b.txt").unlink()
            _sync_now(tree)

            assert await _wait_until(pilot, lambda: "b.txt" not in _tree_lines(tree))
            assert _node(tree, "sub").is_expanded

    asyncio.run(scenario())


def test_sync_dirs_renames_row(tmp_path):
    """改名 = 旧行消失 + 新行出现（集合判等的自然结果）。"""
    app = _make_app(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            _forbid_full_reload(tree)
            assert "a.txt" in _tree_lines(tree)

            (tmp_path / "proj" / "a.txt").rename(tmp_path / "proj" / "renamed.txt")
            _sync_now(tree)

            assert await _wait_until(
                pilot,
                lambda: "renamed.txt" in _tree_lines(tree)
                and "a.txt" not in _tree_lines(tree),
            )

    asyncio.run(scenario())


def test_sync_dirs_file_replaced_by_dir(tmp_path):
    """同名换型必须跟变：``(name, is_dir)`` 而不是纯名字集。

    纯名字集会判等（名字没变）→ 那一行永远停在错的状态，菜单还会按文件处理。
    行是否目录由 ``allow_expand`` 决定（菜单的 ``is_dir`` 就是它）。
    """
    app = _make_app(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            _forbid_full_reload(tree)
            root = tmp_path / "proj"

            (root / "foo").write_text("x\n")
            _sync_now(tree)
            assert await _wait_until(pilot, lambda: "foo" in _tree_lines(tree))
            assert _node(tree, "foo").allow_expand is False

            (root / "foo").unlink()
            (root / "foo").mkdir()
            _sync_now(tree)
            assert await _wait_until(
                pilot, lambda: _node(tree, "foo").allow_expand is True
            )

            # 反向：目录 → 同名文件
            (root / "foo").rmdir()
            (root / "foo").write_text("y\n")
            _sync_now(tree)
            assert await _wait_until(
                pilot, lambda: _node(tree, "foo").allow_expand is False
            )

    asyncio.run(scenario())


def test_sync_dirs_collapsed_dir_is_marked_stale_not_expanded(tmp_path):
    """收起但已加载的目录：不弹开、只标脏；用户展开时再读到新文件。"""
    app = _make_app(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            _forbid_full_reload(tree)
            _node(tree, "sib").expand()
            await pilot.pause(0.3)
            _node(tree, "sib").collapse()
            await pilot.pause(0.2)
            assert not _node(tree, "sib").is_expanded

            (tmp_path / "proj" / "sib" / "later.txt").write_text("x\n")
            _sync_now(tree)

            assert await _wait_until(
                pilot, lambda: _node(tree, "sib").data.loaded is False
            )
            assert not _node(tree, "sib").is_expanded  # 没被弹开
            assert "later.txt" not in _tree_lines(tree)

            _node(tree, "sib").expand()
            assert await _wait_until(pilot, lambda: "later.txt" in _tree_lines(tree))

    asyncio.run(scenario())


def test_sync_dirs_no_change_is_a_noop(tmp_path):
    """同一份快照连灌两次：第二次不动结构、节点对象同一（与防闪烁同口径）。"""
    app = _make_app(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            snapshot = scan_dirs(tree.scan_targets())
            assert tree.sync_dirs(snapshot) is False

            before = {relative: node for relative, node in tree._loaded_nodes().items()}

            def _boom(_node):
                raise AssertionError("没变化不该碰结构（reload_node）")

            tree.reload_node = _boom
            assert tree.sync_dirs(snapshot) is False
            await pilot.pause(0.2)

            after = {relative: node for relative, node in tree._loaded_nodes().items()}
            assert before.keys() == after.keys()
            assert all(before[key] is after[key] for key in before)

    asyncio.run(scenario())


def test_sync_dirs_never_reloads_the_whole_tree(tmp_path):
    """外部改动只重扫目标目录，绝不走全树 ``reload()`` 兜底。"""
    app = _make_app(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            _forbid_full_reload(tree)

            (tmp_path / "proj" / "top.txt").write_text("x\n")
            _sync_now(tree)

            assert await _wait_until(pilot, lambda: "top.txt" in _tree_lines(tree))

    asyncio.run(scenario())


def test_sync_dirs_skips_git_internals(tmp_path):
    """.git 内部写入不算变化：既不出现行，也不触发任何重扫。"""
    app = _make_app(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            before = {relative: node for relative, node in tree._loaded_nodes().items()}

            (tmp_path / "proj" / ".git").mkdir()
            (tmp_path / "proj" / ".git" / "index").write_text("x")
            assert _sync_now(tree) is False
            await pilot.pause(0.2)

            assert ".git" not in _tree_lines(tree)
            after = {relative: node for relative, node in tree._loaded_nodes().items()}
            assert all(before[key] is after[key] for key in before)

    asyncio.run(scenario())


# ----- App 级（走 Sidebar 的调用点） -----


def test_sidebar_refresh_follows_disk_without_git(tmp_path, monkeypatch):
    """git 不在 PATH 时照样跟随磁盘——钉住「拆掉 ``status != ok`` 早退」。"""
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    app = _make_app(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            sidebar = app.query_one(Sidebar)
            tree = app.query_one(FileTree)
            # 先确认 git 真的取不到（否则这条用例就没验到东西）
            assert await _wait_until(
                pilot, lambda: sidebar._git_status == "unavailable"
            )

            (tmp_path / "proj" / "nogit.txt").write_text("x\n")
            sidebar._refresh_file_status_worker()  # worker 路径

            assert await _wait_until(pilot, lambda: "nogit.txt" in _tree_lines(tree))

    asyncio.run(scenario())


def test_sidebar_refresh_ignored_dir_end_to_end(tmp_path):
    """非仓库目录 + 被忽略目录：跑一轮同步刷新，忽略目录里的新文件出现。"""
    root = tmp_path / "proj"
    (root / "docs-internal").mkdir(parents=True)
    (root / "docs-internal" / "a.md").write_text("a\n")
    (root / "a.txt").write_text("a\n")
    app = TerminalApp(_FakeBackend([_pane()]), str(root))

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            sidebar = app.query_one(Sidebar)
            tree = app.query_one(FileTree)
            _forbid_full_reload(tree)

            _node(tree, "docs-internal").expand()
            await pilot.pause(0.4)
            assert "a.md" in _tree_lines(tree)

            (root / "docs-internal" / "b.md").write_text("b\n")
            sidebar.refresh_data()  # 同步入口：分支 + 状态 + 扫盘

            assert await _wait_until(pilot, lambda: "b.md" in _tree_lines(tree))
            assert _node(tree, "docs-internal").is_expanded

    asyncio.run(scenario())
