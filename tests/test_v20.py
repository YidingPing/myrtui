"""myrtui v0.20 测试：文件树右键菜单（最小版）+ 热区 ±1 的右键边界。

覆盖：

- 右键 = 只高亮（不 toggle 目录、不发 ``PathSelected``、不改 Git 历史目标）；
- 菜单弹出位置、选项、Esc / 点外关闭、键盘不泄进 pane；
- 「复制绝对路径」走双剪贴板通道，关闭后焦点回文件树；
- 分隔线热区收到 ±1 后，树最右列（距线 2 格）的右键不再被 App 层拖拽截获。

判定口径见 ``docs-internal/docs/V20-CONTEXT-MENU-PLAN.md`` §7/§8。
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from textual.events import MouseDown, MouseUp
from textual.widgets import OptionList

from myrtui.client import clipboard
from myrtui.client.app import MainScreen, TerminalApp
from myrtui.client.context_menu import ContextMenuScreen
from myrtui.client.file_tree import FileTree
from myrtui.client.topbar import TopBar
from myrtui.tmux_backend import PaneInfo


# ---------- 夹具 ----------

def _pane(index: int) -> PaneInfo:
    """假 pane 拓扑（不碰 tmux）。"""
    return PaneInfo(
        pane_id=f"p{index}", dead=False, command="sh", alternate_on=False,
        cursor_x=0, cursor_y=0, cursor_flag=False, history_size=10,
        height=24, width=80, window_id=f"w{index}", window_index=index,
        mouse_standard=False, mouse_button=False, mouse_any=False,
        mouse_sgr=True, mouse_all=False,
    )


class _FakeBackend:
    """假后端：拓扑 + 记录向前端 pane 注入的键（用于「键不泄进 shell」断言）。"""

    def __init__(self, count: int = 2):
        self.count = count
        self.sent: list[tuple[str, object]] = []

    def list_panes(self):
        return [_pane(i) for i in range(self.count)]

    def poll(self, offsets):
        return self.list_panes(), {}

    def resize_window(self, window_id, cols, rows):
        pass

    def send_text(self, pane_id, text):
        self.sent.append(("text", text))

    def send_keys(self, pane_id, *keys):
        self.sent.append(("keys", keys))

    def send_ctrl_a(self, pane_id):
        self.sent.append(("ctrl_a", pane_id))


class _RecordingApp(TerminalApp):
    """TerminalApp + 记录两条树消息，供断言「右键不发 PathSelected」。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.selected: list[tuple[str, bool]] = []
        self.contexts: list[FileTree.PathContextRequested] = []

    def on_file_tree_path_selected(self, event: FileTree.PathSelected) -> None:
        # 不要调 super：textual 的处理器沿 MRO 逐个执行，基类那次仍会跑
        self.selected.append((event.path, event.is_dir))

    def on_file_tree_path_context_requested(
        self, event: FileTree.PathContextRequested
    ) -> None:
        # 同上：基类 TerminalApp 的处理器由 MRO 分发自然执行（只推一次菜单）
        self.contexts.append(event)


def _project() -> str:
    """临时项目：a.txt + sub/b.txt（小树，避免文件树铺满影响行号）。"""
    root = Path(tempfile.mkdtemp(prefix="myrtui-v20-"))
    (root / "a.txt").write_text("a")
    (root / "notes.md").write_text("# notes\n")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("b")
    return str(root)


def _tree_lines(tree: FileTree) -> list[str]:
    """树当前各行的标签（按显示顺序，与 test_p3 同口径）。"""
    return [str(line.path[-1].label.plain) for line in tree._tree_lines]


def _node(tree: FileTree, label: str):
    return next(child for child in tree.root.children if child.label.plain == label)


async def _open_on(pilot, tree: FileTree, label: str, x: int = 4) -> int:
    """右键点某一行 → 返回该行号（调用方自行 pause）。"""
    line = _tree_lines(tree).index(label)
    await pilot.click(FileTree, offset=(x, line), button=3)
    await pilot.pause(0.25)
    return line


def _menu(app) -> OptionList:
    return app.screen.query_one(OptionList)


# ---------- 右键：选中语义 ----------

def test_right_click_directory_does_not_toggle():
    """右键目录：不展开 / 不收起；消息 is_dir=True；菜单弹出。"""
    root = _project()

    async def scenario():
        app = _RecordingApp(_FakeBackend(), root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.6)
            tree = app.query_one(FileTree)
            node = _node(tree, "sub")
            assert not node.is_expanded

            await _open_on(pilot, tree, "sub")

            assert not node.is_expanded, "右键不得 toggle 目录"
            assert app.contexts and app.contexts[-1].is_dir is True
            assert isinstance(app.screen, ContextMenuScreen)

    asyncio.run(scenario())


def test_right_click_on_toggle_arrow_does_not_toggle():
    """右键点在展开箭头（meta["toggle"]）上：仍不切换展开状态。"""
    root = _project()

    async def scenario():
        app = _RecordingApp(_FakeBackend(), root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.6)
            tree = app.query_one(FileTree)
            node = _node(tree, "sub")
            line = _tree_lines(tree).index("sub")
            # 一级子项的箭头在 guide 前缀之后（guide_depth=2 → 图标从 x=2 起）
            await pilot.click(FileTree, offset=(2, line), button=3)
            await pilot.pause(0.25)

            assert not node.is_expanded, "右键点箭头也不得 toggle"
            assert isinstance(app.screen, ContextMenuScreen)

    asyncio.run(scenario())


def test_right_click_does_not_post_path_selected():
    """右键文件：不发 PathSelected（Git 历史目标不动）。"""
    root = _project()

    async def scenario():
        app = _RecordingApp(_FakeBackend(), root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.6)
            tree = app.query_one(FileTree)
            await _open_on(pilot, tree, "a.txt")

            assert app.selected == [], "右键不得发 PathSelected"
            assert app._log_target is None, "右键不得切换 Git 历史目标"

    asyncio.run(scenario())


def test_right_click_only_moves_cursor_line():
    """右键只移动树的 cursor_line，不改 Git 标题作用域。"""
    root = _project()

    async def scenario():
        app = _RecordingApp(_FakeBackend(), root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.6)
            tree = app.query_one(FileTree)
            line = await _open_on(pilot, tree, "a.txt")

            assert tree.cursor_line == line
            assert app._sidebar()._scope is None

    asyncio.run(scenario())


def test_right_click_root_path_normalized():
    """右键根节点：`path` 归一为 `""`（与 PathSelected/_node_relative 同口径）。"""
    root = _project()

    async def scenario():
        app = _RecordingApp(_FakeBackend(), root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.6)
            tree = app.query_one(FileTree)
            await _open_on(pilot, tree, Path(root).name)

            assert app.contexts and app.contexts[-1].path == ""
            assert app.contexts[-1].absolute == str(Path(root).resolve())

    asyncio.run(scenario())


# ---------- 菜单：位置 / 关闭 / 键盘 ----------

def test_right_click_opens_menu_at_pointer():
    """菜单在右键位置弹出；文件行三项（打开 + 复制相对/绝对路径）。"""
    root = _project()

    async def scenario():
        app = _RecordingApp(_FakeBackend(), root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.6)
            tree = app.query_one(FileTree)
            line = await _open_on(pilot, tree, "a.txt")

            assert isinstance(app.screen, ContextMenuScreen)
            menu = _menu(app)
            x = tree.region.x + 4
            y = tree.region.y + line
            assert menu.region.contains(x, y), (menu.region, x, y)
            assert [option.id for option in menu.options] == [
                "view",
                "copy_rel",
                "copy_abs",
            ]

    asyncio.run(scenario())


def test_right_click_markdown_adds_preview_item():
    """Markdown 文件行比普通文件多「预览 Markdown」一项。"""
    root = _project()

    async def scenario():
        app = _RecordingApp(_FakeBackend(), root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.6)
            tree = app.query_one(FileTree)
            await _open_on(pilot, tree, "notes.md")

            menu = _menu(app)
            assert [option.id for option in menu.options] == [
                "view",
                "preview",
                "copy_rel",
                "copy_abs",
            ]

    asyncio.run(scenario())


def test_context_menu_escape_closes_and_restores_focus():
    """Esc 关闭菜单，焦点回文件树（pop_screen 不自动恢复焦点）。"""
    root = _project()

    async def scenario():
        app = _RecordingApp(_FakeBackend(), root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.6)
            tree = app.query_one(FileTree)
            await _open_on(pilot, tree, "a.txt")

            await pilot.press("escape")
            await pilot.pause(0.2)

            assert isinstance(app.screen, MainScreen)
            assert app.focused is tree, app.focused

    asyncio.run(scenario())


def test_context_menu_swallows_keys():
    """菜单期间 ↑↓/Esc 不泄进 pane（on_key 的 MainScreen 闸门覆盖模态屏）。"""
    root = _project()

    async def scenario():
        backend = _FakeBackend()
        app = _RecordingApp(backend, root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.6)
            tree = app.query_one(FileTree)
            await _open_on(pilot, tree, "a.txt")

            await pilot.press("down")
            await pilot.press("up")
            await pilot.pause(0.2)
            assert backend.sent == [], backend.sent

            await pilot.press("escape")
            await pilot.pause(0.2)
            assert isinstance(app.screen, MainScreen)

    asyncio.run(scenario())


def test_context_menu_cancels_pending_prefix():
    """Ctrl-a 后 1s 内右键开菜单 → 前缀被取消，超时不再把 Ctrl-a 注入 pane。"""
    root = _project()

    async def scenario():
        backend = _FakeBackend()
        app = _RecordingApp(backend, root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.6)
            tree = app.query_one(FileTree)

            await pilot.press("ctrl+a")
            await pilot.pause(0.1)
            assert app._prefix_armed is True

            await _open_on(pilot, tree, "a.txt")
            assert app._prefix_armed is False
            assert app._prefix_timer is None

            await pilot.pause(1.2)  # 越过 PREFIX_TIMEOUT
            assert backend.sent == [], backend.sent

    asyncio.run(scenario())


# ---------- 菜单：动作 ----------

def test_context_menu_copy_absolute_path(monkeypatch):
    """选中「复制绝对路径」：双通道 + 提示 + 焦点归还。"""
    root = _project()
    copied: list[str] = []
    monkeypatch.setattr(
        clipboard,
        "copy_to_system_clipboard",
        lambda text: copied.append(text) or True,
    )

    async def scenario():
        app = _RecordingApp(_FakeBackend(), root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.6)
            tree = app.query_one(FileTree)
            await _open_on(pilot, tree, "a.txt")

            await pilot.click(OptionList, offset=(2, 3))  # 菜单第三项（复制绝对路径）
            await pilot.pause(0.3)

            expected = str((Path(root) / "a.txt").resolve())
            assert copied == [expected]
            assert app._copy_buffer == expected
            assert app._notice_text == "已复制路径"
            assert isinstance(app.screen, MainScreen)
            assert app.focused is tree

    asyncio.run(scenario())


def test_context_menu_copy_relative_path(monkeypatch):
    """选中「复制相对路径」：文件复制相对项目根的路径。"""
    root = _project()
    copied: list[str] = []
    monkeypatch.setattr(
        clipboard,
        "copy_to_system_clipboard",
        lambda text: copied.append(text) or True,
    )

    async def scenario():
        app = _RecordingApp(_FakeBackend(), root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.6)
            tree = app.query_one(FileTree)
            await _open_on(pilot, tree, "a.txt")

            await pilot.click(OptionList, offset=(2, 2))  # 菜单第二项（复制相对路径）
            await pilot.pause(0.3)

            assert copied == ["a.txt"]
            assert app._copy_buffer == "a.txt"
            assert app._notice_text == "已复制相对路径"
            assert isinstance(app.screen, MainScreen)
            assert app.focused is tree

    asyncio.run(scenario())


def test_context_menu_copy_relative_path_root_is_dot(monkeypatch):
    """根节点的相对路径是空串 → 复制 "."。"""
    root = _project()
    copied: list[str] = []
    monkeypatch.setattr(
        clipboard,
        "copy_to_system_clipboard",
        lambda text: copied.append(text) or True,
    )

    async def scenario():
        app = _RecordingApp(_FakeBackend(), root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.6)
            tree = app.query_one(FileTree)
            await _open_on(pilot, tree, Path(root).name)

            menu = _menu(app)
            assert [option.id for option in menu.options] == [
                "refresh_dir",
                "copy_rel",
                "copy_abs",
            ]
            await pilot.click(OptionList, offset=(2, 2))  # 第 2 项（复制相对路径）
            await pilot.pause(0.3)

            assert copied == ["."]
            assert app._copy_buffer == "."
            assert app._notice_text == "已复制相对路径"

    asyncio.run(scenario())


def test_context_menu_click_outside_dismisses_without_selecting():
    """点菜单外：菜单关闭，底下的树行不被选中（不穿透）。"""
    root = _project()

    async def scenario():
        app = _RecordingApp(_FakeBackend(), root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.6)
            tree = app.query_one(FileTree)
            line = await _open_on(pilot, tree, "a.txt")
            cursor_before = tree.cursor_line

            await pilot.click(offset=(100, 5))  # 菜单外（pane 区）
            await pilot.pause(0.3)

            assert isinstance(app.screen, MainScreen)
            assert tree.cursor_line == cursor_before
            assert app.selected == []

    asyncio.run(scenario())


def test_context_menu_position_clamped():
    """右下角定位 → constrain 把菜单夹回屏幕内。"""
    root = _project()

    async def scenario():
        app = _RecordingApp(_FakeBackend(), root)
        async with app.run_test(size=(40, 12)) as pilot:
            await pilot.pause(0.4)
            app.push_screen(ContextMenuScreen([("copy_abs", "复制绝对路径")], 999, 999))
            await pilot.pause(0.2)

            menu = _menu(app)
            assert menu.region.right <= app.size.width
            assert menu.region.bottom <= app.size.height

            app.pop_screen()
            await pilot.pause(0.1)

    asyncio.run(scenario())


def test_menu_open_click_near_separator_does_not_drag():
    """菜单开着时，热区上的右键也不会起拖（模态屏让主屏分隔线不参与命中）。

    审阅修订 R9 假设「菜单开着时热区右键会被 App 层截获并起拖」；实际
    ``_separator_near`` 只认**当前屏**的分隔线，模态打开时主屏的线被排除
    （既有 ``test_grab_zone_ignores_separators_behind_modal`` 同理），故该
    事件落到菜单屏 → 按「点菜单外」关闭。
    """
    root = _project()

    async def scenario():
        app = _RecordingApp(_FakeBackend(), root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.6)
            tree = app.query_one(FileTree)
            await _open_on(pilot, tree, "a.txt")
            sep = app.query_one("#sep-main")
            x, y = sep.region.x, 10

            for event_type in (MouseDown, MouseUp):
                app.post_message(
                    event_type(
                        widget=None, x=float(x), y=float(y),
                        delta_x=0, delta_y=0, button=3,
                        shift=False, meta=False, ctrl=False,
                        screen_x=float(x), screen_y=float(y),
                    )
                )
                await pilot.pause(0.2)

            assert app._drag_sep is None, "菜单开着时热区右键不应起拖"
            assert isinstance(app.screen, MainScreen), "应被当作点菜单外关闭"

    asyncio.run(scenario())


def test_right_click_near_separator_not_seized_when_outside_hot_zone():
    """树最右列（距 sep-main 2 格）的右键走 App 层不再被拖拽截获。

    **必须自己往 App 投 MouseDown/Up**：``pilot.click`` 走
    ``Screen._forward_event``，绕过 ``App.on_event`` 的前置截获，测不到这条断言
    （同 ``test_dragging_separator_does_not_click_tree_node`` 的做法）。
    """
    root = _project()

    async def scenario():
        app = _RecordingApp(_FakeBackend(), root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.6)
            tree = app.query_one(FileTree)
            sep = app.query_one("#sep-main")
            x = tree.region.right - 1
            y = tree.region.y
            assert sep.region.x - x == 2, (sep.region, tree.region)  # 前提自检

            for event_type in (MouseDown, MouseUp):
                app.post_message(
                    event_type(
                        widget=None, x=float(x), y=float(y),
                        delta_x=0, delta_y=0, button=3,
                        shift=False, meta=False, ctrl=False,
                        screen_x=float(x), screen_y=float(y),
                    )
                )
                await pilot.pause(0.2)

            assert app._drag_sep is None, "距线 2 格不应起拖"
            assert isinstance(app.screen, ContextMenuScreen), "右键应弹菜单"

    asyncio.run(scenario())
