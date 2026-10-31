"""「刷新此目录」回归（v0.30 能力，v0.34 上传移除时从 test_v30 迁出）。

v0.30 的目录右键菜单有两半：上传（v0.34 整体移除，见
``docs-internal/docs/V34-REMOVE-UPLOAD-PLAN.md``）与「刷新此目录」。本文件只守后者：

- 菜单结构：文件行不含目录动作，目录行含 ``refresh_dir``；
- 刷新语义：新增文件可见、其它展开与光标保持、根节点、目录被删刷父目录、
  节点不在树里走全树兜底；
- 刷新不碰 Git 历史目标、不调用 tmux 后端。
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from textual.widgets import OptionList

from myrtui.client.app import MainScreen, TerminalApp
from myrtui.client.file_tree import FileTree
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
