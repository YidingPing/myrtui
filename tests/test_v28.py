"""myrtui v0.28 测试：文件树菜单注入到当前 pane（v0.31 改为「打开」）。

覆盖：

- ``pane_commands`` 纯逻辑：Markdown 判断、二进制/缺失/控制字符拒绝、
  ``less`` 命令拼装；
- 后端 ``run_command``：单次 tmux 调用完成 C-u + 字面命令 + Enter；
- App：右键打开注入当前 pane、偏移回底、焦点跟到 pane；
- 占用保护：opencode / 全屏程序 / dead pane 拒绝；
- 入口：右键 / ``Enter`` / ``Space`` 调出菜单；``Enter`` 文件行保留选中语义，
  ``Space`` 不改历史；双击 / ``Ctrl-a o`` 仍不触发动作。
"""
from __future__ import annotations

import asyncio
import shlex
from pathlib import Path

import pytest
from textual.widgets import OptionList

import myrtui.pane_commands as pane_commands
from myrtui.client.app import TerminalApp
from myrtui.client.context_menu import ContextMenuScreen
from myrtui.client.file_tree import FileTree
from myrtui.client.pane_widget import PaneWidget
from myrtui.tmux_backend import CaptureResult, PaneInfo, TmuxBackend


# ---------- 纯逻辑：pane_commands ----------

def _which(mapping: dict[str, str]):
    return lambda name: mapping.get(name)


def test_is_markdown_extensions(tmp_path):
    assert pane_commands.is_markdown(tmp_path / "a.md")
    assert pane_commands.is_markdown(tmp_path / "a.MARKDOWN")
    assert pane_commands.is_markdown(tmp_path / "a.mdown")
    assert not pane_commands.is_markdown(tmp_path / "a.txt")
    assert not pane_commands.is_markdown(tmp_path / "README")


def test_build_view_prefers_ov(tmp_path):
    target = tmp_path / "a b'c.txt"
    target.write_text("hello\n")
    plan = pane_commands.build_view(
        str(target),
        which=_which({"ov": "/bin/ov", "less": "/bin/less", "cat": "/bin/cat"}),
    )
    assert plan.notice is None
    assert plan.command == f"/bin/ov {shlex.quote(str(target))}"


def test_build_view_prefers_less_without_ov(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello\n")
    plan = pane_commands.build_view(
        str(target), which=_which({"less": "/bin/less", "cat": "/bin/cat"})
    )
    assert plan.notice is None
    assert plan.command == f"/bin/less -R {shlex.quote(str(target))}"


def test_build_view_falls_back_to_cat(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello\n")
    plan = pane_commands.build_view(
        str(target), which=_which({"cat": "/bin/cat"})
    )
    assert plan.command == f"/bin/cat -- {shlex.quote(str(target))}"
    assert plan.notice is not None and "分页器" in plan.notice


def test_build_view_rejects_binary(tmp_path):
    target = tmp_path / "bin.dat"
    target.write_bytes(b"abc\0def")
    plan = pane_commands.build_view(
        str(target), which=_which({"less": "/bin/less"})
    )
    assert plan.command is None
    assert "二进制" in plan.notice


def test_build_view_rejects_missing_and_control_paths(tmp_path):
    missing = tmp_path / "nope.txt"
    plan = pane_commands.build_view(
        str(missing), which=_which({"less": "/bin/less"})
    )
    assert plan.command is None
    assert "不存在" in plan.notice

    target = tmp_path / "a.txt"
    target.write_text("x")
    plan = pane_commands.build_view(
        str(target) + "\n", which=_which({"less": "/bin/less"})
    )
    assert plan.command is None
    assert "控制字符" in plan.notice


# ---------- 后端：单次 tmux 调用 ----------

def test_run_command_is_one_tmux_call(monkeypatch):
    backend = TmuxBackend("sock", "sess")
    calls: list[list[str]] = []
    monkeypatch.setattr(backend, "_run", lambda args, **kwargs: calls.append(args))

    backend.run_command("p1", "cat -- '/a b'")

    assert calls == [
        [
            "send-keys", "-t", "p1", "C-u", ";",
            "send-keys", "-l", "-t", "p1", "--", "cat -- '/a b'", ";",
            "send-keys", "-t", "p1", "Enter",
        ]
    ]


def test_run_command_ignores_empty(monkeypatch):
    backend = TmuxBackend("sock", "sess")
    calls: list[list[str]] = []
    monkeypatch.setattr(backend, "_run", lambda args, **kwargs: calls.append(args))
    backend.run_command("p1", "")
    assert calls == []


# ---------- App 夹具 ----------

def _pane(
    index: int = 0,
    *,
    command: str = "zsh",
    dead: bool = False,
    alternate_on: bool = False,
    width: int = 80,
) -> PaneInfo:
    return PaneInfo(
        pane_id=f"p{index}",
        dead=dead,
        command=command,
        alternate_on=alternate_on,
        cursor_x=0,
        cursor_y=0,
        cursor_flag=False,
        history_size=100,
        height=24,
        width=width,
        window_id=f"w{index}",
        window_index=index,
        mouse_standard=False,
        mouse_button=False,
        mouse_any=False,
        mouse_sgr=True,
        mouse_all=False,
    )


class _FakeBackend:
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
    (tmp_path / "a.txt").write_text("a\n")
    (tmp_path / "notes.md").write_text("# notes\n")
    (tmp_path / "bin.dat").write_bytes(b"x\0y")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b\n")
    return str(tmp_path)


def _tools(monkeypatch):
    # 与开发机环境解耦：清掉显式编辑器变量，假 PATH 里放 micro + less。
    for var in ("MYRTUI_EDITOR", "EDITOR", "VISUAL", "MYRTUI_PAGER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        pane_commands.shutil,
        "which",
        lambda name: {
            "micro": "/bin/micro",
            "ov": "/bin/ov",
            "less": "/bin/less",
        }.get(name),
    )


def _tree_lines(tree: FileTree) -> list[str]:
    return [str(line.path[-1].label.plain) for line in tree._tree_lines]


# ---------- App：打开 ----------

def test_context_menu_open_sends_editor_and_focuses_pane(tmp_path, monkeypatch):
    _tools(monkeypatch)
    root = _project(tmp_path)
    backend = _FakeBackend([_pane()])
    app = TerminalApp(backend, root)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            app._offsets["p0"] = 4
            absolute = str((tmp_path / "a.txt").resolve())
            app._context_menu_closed("view", "a.txt", absolute, False)
            await pilot.pause(0.3)

            assert backend.commands == [
                ("p0", f"/bin/micro {shlex.quote(absolute)}")
            ]
            assert app._offsets["p0"] == 0
            assert app.focused is app.query_one("#pane-0", PaneWidget)

    asyncio.run(scenario())


def test_menu_press_enter_selects_view(tmp_path, monkeypatch):
    """右键菜单第一项（打开）+ Enter 的端到端路径。"""
    _tools(monkeypatch)
    root = _project(tmp_path)
    backend = _FakeBackend([_pane()])
    app = TerminalApp(backend, root)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            line = _tree_lines(tree).index("a.txt")
            await pilot.click(FileTree, offset=(4, line), button=3)
            await pilot.pause(0.2)
            await pilot.press("enter")
            await pilot.pause(0.3)

            absolute = str((tmp_path / "a.txt").resolve())
            assert backend.commands == [
                ("p0", f"/bin/micro {shlex.quote(absolute)}")
            ]

    asyncio.run(scenario())


# ---------- App：占用保护 ----------

@pytest.mark.parametrize(
    ("pane", "needle"),
    [
        (_pane(command="opencode"), "opencode"),
        (_pane(alternate_on=True), "全屏程序"),
        (_pane(dead=True), "已退出"),
    ],
)
def test_busy_pane_is_refused(tmp_path, monkeypatch, pane, needle):
    _tools(monkeypatch)
    root = _project(tmp_path)
    backend = _FakeBackend([pane])
    app = TerminalApp(backend, root)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            absolute = str((tmp_path / "a.txt").resolve())
            app._context_menu_closed("view", "a.txt", absolute, False)
            await pilot.pause(0.2)

            assert backend.commands == []
            assert needle in app._notice_text

    asyncio.run(scenario())


def test_binary_file_is_refused(tmp_path, monkeypatch):
    _tools(monkeypatch)
    root = _project(tmp_path)
    backend = _FakeBackend([_pane()])
    app = TerminalApp(backend, root)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            absolute = str((tmp_path / "bin.dat").resolve())
            app._context_menu_closed("view", "bin.dat", absolute, False)
            await pilot.pause(0.2)

            assert backend.commands == []
            assert "二进制" in app._notice_text

    asyncio.run(scenario())


# ---------- App：入口（右键 / 键盘） ----------

def test_enter_on_file_opens_menu_without_view(tmp_path, monkeypatch):
    """``Enter`` 文件行：弹菜单但不直接注入命令（v0.28.1）。"""
    _tools(monkeypatch)
    root = _project(tmp_path)
    backend = _FakeBackend([_pane()])
    app = TerminalApp(backend, root)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            tree.focus()
            tree.cursor_line = _tree_lines(tree).index("a.txt")
            await pilot.pause(0.1)
            await pilot.press("enter")
            await pilot.pause(0.2)

            assert isinstance(app.screen, ContextMenuScreen)
            menu = app.screen.query_one(OptionList)
            assert [option.id for option in menu.options] == [
                "view",
                "copy_rel",
                "copy_abs",
            ]
            assert backend.commands == []
            # Enter 保留「选中 + 切 Git 历史」语义
            assert app._log_target == "a.txt"

    asyncio.run(scenario())


def test_space_on_file_opens_menu_without_changing_history(tmp_path, monkeypatch):
    """``Space`` 文件行：弹菜单但不切 Git 历史（等价键盘右键）。"""
    _tools(monkeypatch)
    root = _project(tmp_path)
    backend = _FakeBackend([_pane()])
    app = TerminalApp(backend, root)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            tree.focus()
            tree.cursor_line = _tree_lines(tree).index("notes.md")
            await pilot.pause(0.1)
            await pilot.press("space")
            await pilot.pause(0.2)

            menu = app.screen.query_one(OptionList)
            assert [option.id for option in menu.options] == [
                "view",
                "preview",
                "copy_rel",
                "copy_abs",
            ]
            assert app._log_target is None
            assert backend.commands == []

    asyncio.run(scenario())


def test_space_on_directory_opens_menu_without_expanding(tmp_path, monkeypatch):
    """``Space`` 目录行：弹菜单，不展开、不切历史。"""
    _tools(monkeypatch)
    root = _project(tmp_path)
    backend = _FakeBackend([_pane()])
    app = TerminalApp(backend, root)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            tree.focus()
            line = _tree_lines(tree).index("sub")
            node = tree.get_node_at_line(line)
            assert node is not None and node.allow_expand
            assert not node.is_expanded
            tree.cursor_line = line
            await pilot.pause(0.1)
            await pilot.press("space")
            await pilot.pause(0.2)

            menu = app.screen.query_one(OptionList)
            assert [option.id for option in menu.options] == [
                "refresh_dir",
                "copy_rel",
                "copy_abs",
            ]
            assert not node.is_expanded
            assert app._log_target is None

    asyncio.run(scenario())


def test_space_on_root_opens_menu_with_refresh_and_copy(tmp_path, monkeypatch):
    """``Space`` 根节点：弹菜单（刷新 + 复制），不改历史。"""
    _tools(monkeypatch)
    root = _project(tmp_path)
    backend = _FakeBackend([_pane()])
    app = TerminalApp(backend, root)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            tree.focus()
            tree.cursor_line = 0
            await pilot.pause(0.1)
            await pilot.press("space")
            await pilot.pause(0.2)

            menu = app.screen.query_one(OptionList)
            assert [option.id for option in menu.options] == [
                "refresh_dir",
                "copy_rel",
                "copy_abs",
            ]
            assert app._log_target is None

    asyncio.run(scenario())


def test_enter_on_directory_still_expands_without_menu(tmp_path, monkeypatch):
    """``Enter`` 目录行仍是展开/收起，不弹菜单（回归）。"""
    _tools(monkeypatch)
    root = _project(tmp_path)
    backend = _FakeBackend([_pane()])
    app = TerminalApp(backend, root)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            tree.focus()
            tree.cursor_line = _tree_lines(tree).index("sub")
            node = tree.get_node_at_line(tree.cursor_line)
            assert node is not None and not node.is_expanded
            await pilot.press("enter")
            await pilot.pause(0.5)

            assert node.is_expanded
            assert not isinstance(app.screen, ContextMenuScreen)
            assert backend.commands == []

    asyncio.run(scenario())


def test_keyboard_menu_anchored_at_cursor_row(tmp_path, monkeypatch):
    """键盘菜单在光标行右侧弹出，不越出屏幕。"""
    _tools(monkeypatch)
    root = _project(tmp_path)
    backend = _FakeBackend([_pane()])
    app = TerminalApp(backend, root)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            tree.focus()
            tree.cursor_line = _tree_lines(tree).index("a.txt")
            await pilot.pause(0.1)
            anchor_x, anchor_y = tree._menu_anchor()
            await pilot.press("enter")
            await pilot.pause(0.2)

            menu = app.screen.query_one(OptionList)
            assert menu.region.x >= tree.region.right
            assert menu.region.y == anchor_y
            assert menu.region.right <= 120
            assert anchor_x == tree.content_region.right

    asyncio.run(scenario())


def test_escape_from_keyboard_menu_returns_focus(tmp_path, monkeypatch):
    """``Esc`` 关闭键盘菜单 → 焦点回文件树、不注入任何命令。"""
    _tools(monkeypatch)
    root = _project(tmp_path)
    backend = _FakeBackend([_pane()])
    app = TerminalApp(backend, root)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            tree.focus()
            tree.cursor_line = _tree_lines(tree).index("a.txt")
            await pilot.pause(0.1)
            await pilot.press("enter")
            await pilot.pause(0.2)
            await pilot.press("escape")
            await pilot.pause(0.2)

            assert not isinstance(app.screen, ContextMenuScreen)
            assert app.focused is tree
            assert backend.commands == []

    asyncio.run(scenario())


def test_keyboard_menu_enter_selects_view(tmp_path, monkeypatch):
    """键盘：``Enter`` 开菜单 → ``Enter`` 选第一项「打开」，注入编辑器。"""
    _tools(monkeypatch)
    root = _project(tmp_path)
    backend = _FakeBackend([_pane()])
    app = TerminalApp(backend, root)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            tree.focus()
            tree.cursor_line = _tree_lines(tree).index("a.txt")
            await pilot.pause(0.1)
            await pilot.press("enter")
            await pilot.pause(0.2)
            await pilot.press("enter")
            await pilot.pause(0.3)

            absolute = str((tmp_path / "a.txt").resolve())
            assert backend.commands == [
                ("p0", f"/bin/micro {shlex.quote(absolute)}")
            ]

    asyncio.run(scenario())


def test_double_click_on_file_does_not_view(tmp_path, monkeypatch):
    _tools(monkeypatch)
    root = _project(tmp_path)
    backend = _FakeBackend([_pane()])
    app = TerminalApp(backend, root)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            line = _tree_lines(tree).index("a.txt")
            await pilot.click(FileTree, offset=(4, line), times=2)
            await pilot.pause(0.3)

            assert backend.commands == []

    asyncio.run(scenario())


def test_ctrl_a_o_is_forwarded_to_shell(tmp_path, monkeypatch):
    _tools(monkeypatch)
    root = _project(tmp_path)
    backend = _FakeBackend([_pane()])
    app = TerminalApp(backend, root)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            await pilot.press("ctrl+a")
            await pilot.press("o")
            await pilot.pause(0.2)

            assert backend.commands == []
            assert ("text", "o") in backend.sent

    asyncio.run(scenario())


# ---------- 菜单项 ----------

def test_markdown_menu_has_preview_item(tmp_path, monkeypatch):
    _tools(monkeypatch)
    root = _project(tmp_path)
    backend = _FakeBackend([_pane()])
    app = TerminalApp(backend, root)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            line = _tree_lines(tree).index("notes.md")
            await pilot.click(FileTree, offset=(4, line), button=3)
            await pilot.pause(0.2)

            menu = app.screen.query_one(OptionList)
            assert [option.id for option in menu.options] == [
                "view",
                "preview",
                "copy_rel",
                "copy_abs",
            ]
            assert isinstance(app.screen, ContextMenuScreen)

    asyncio.run(scenario())
