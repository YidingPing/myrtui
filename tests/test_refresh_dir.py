"""「刷新此目录」回归（v0.30 能力，v0.34 上传移除时从 test_v30 迁出）。

v0.30 的目录右键菜单有两半：上传（v0.34 整体移除，见
``myrtui-internal/docs/V34-REMOVE-UPLOAD-PLAN.md``）与「刷新此目录」。本文件只守后者：

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
