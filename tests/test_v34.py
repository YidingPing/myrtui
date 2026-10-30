"""v0.34：上传能力整体移除后的防回归。

方案见 ``docs-internal/docs/V34-REMOVE-UPLOAD-PLAN.md``。这里锁住的都是「删干净」的证据：

1. 目录菜单只剩「刷新此目录 / 复制相对 / 复制绝对」；
2. ``Ctrl-a u`` 回到「未定义前缀键透传」语义（``u`` 进 shell）；
3. 文件树聚焦时 bracketed paste 的路径文本不进 pane、不弹屏；
4. ``myrtui --help`` 不再出现 ``uplink``；
5. ``myrtui uplink`` 变成 argparse 的用法错误（退出码 2），
   这是老版本本机 agent 会看到的信号；
6. ``myrtui`` 包内不再有 upload / uplink 的实现或引用。
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest
from textual.events import Paste
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

    def paste(self, pane_id, text):
        self.sent.append(("paste", text))


def _project(tmp_path: Path) -> str:
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    (root / "a.txt").write_text("a\n")
    (root / "sub").mkdir(exist_ok=True)
    (root / "sub" / "b.txt").write_text("b\n")
    return str(root)


def _make_app(tmp_path: Path) -> TerminalApp:
    return TerminalApp(_FakeBackend([_pane()]), _project(tmp_path))


def _tree_lines(tree: FileTree) -> list[str]:
    return [str(line.path[-1].label.plain) for line in tree._tree_lines]


# ---------- 1. 菜单 ----------

def test_directory_menu_has_only_refresh_and_copy(tmp_path):
    app = _make_app(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            line = _tree_lines(tree).index("sub")
            await pilot.click(FileTree, offset=(4, line), button=3)
            await pilot.pause(0.25)

            menu = app.screen.query_one(OptionList)
            assert [option.id for option in menu.options] == [
                "refresh_dir",
                "copy_rel",
                "copy_abs",
            ]

    asyncio.run(scenario())


# ---------- 2. Ctrl-a u 透传 ----------

def test_ctrl_a_u_is_forwarded_to_shell(tmp_path):
    app = _make_app(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            await pilot.press("ctrl+a", "u")
            await pilot.pause(0.3)

            assert ("text", "u") in app.backend.sent

    asyncio.run(scenario())


# ---------- 3. 文件树粘贴不注入 ----------

def test_paste_on_focused_tree_does_not_reach_pane(tmp_path):
    app = _make_app(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            app.query_one(FileTree).focus()
            await pilot.pause(0.1)
            app.post_message(Paste("/tmp/a.txt"))
            await pilot.pause(0.3)

            assert app.backend.sent == []
            assert isinstance(app.screen, MainScreen)

    asyncio.run(scenario())


def test_paste_on_focused_pane_is_forwarded(tmp_path):
    """焦点在 pane 时，粘贴就是普通粘贴（含终端拖拽送的路径文本）。

    终端无法区分「拖文件」与「粘贴文本」，所以这不是上传入口、也不该被吞：
    README「已知限制」写明「先点侧栏再拖」。
    """
    app = _make_app(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            app.query_one("#pane-0").focus()
            await pilot.pause(0.1)
            app.post_message(Paste("/tmp/a.txt"))
            await pilot.pause(0.3)

            assert ("paste", "/tmp/a.txt") in app.backend.sent

    asyncio.run(scenario())


# ---------- 4 / 5. CLI ----------

def test_help_has_no_uplink_subcommand():
    from myrtui.cli import build_parser

    text = build_parser().format_help()
    assert "uplink" not in text


def test_uplink_is_unknown_argument(capsys):
    from myrtui.cli import main

    with pytest.raises(SystemExit) as excinfo:
        main(["uplink"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "unrecognized arguments: uplink" in err


# ---------- 6. 包内无残留 ----------

def test_no_upload_modules_or_references():
    assert importlib.util.find_spec("myrtui.upload") is None
    assert importlib.util.find_spec("myrtui.uplink") is None
    assert importlib.util.find_spec("myrtui.uplink_agent") is None
    assert importlib.util.find_spec("myrtui.uplink_stream") is None
    assert importlib.util.find_spec("myrtui.uplink_cli") is None

    package = Path(__file__).resolve().parents[1] / "myrtui"
    offenders = sorted(
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if "upload" in path.read_text(encoding="utf-8")
        or "uplink" in path.read_text(encoding="utf-8")
    )
    assert offenders == [], f"仍有上传相关引用：{offenders}"
