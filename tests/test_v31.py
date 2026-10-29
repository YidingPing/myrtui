"""myrtui v0.31 测试：文件树「打开」用编辑器与分页器。

覆盖：

- ``resolve_editor``：``$MYRTUI_EDITOR`` → ``$EDITOR`` → ``$VISUAL`` →
  ``micro`` → ``vim`` → ``vi``（helix / nvim 只认显式环境变量），含带参数、
  裸名字解析、非法引号跳过、无编辑器；
- ``build_open``：显式/环境编辑器、路径引号、无编辑器时退回 ``less``；
- App 菜单标签随解析结果变化（「打开（micro）」/「查看（less）」）。
"""
from __future__ import annotations

import asyncio
import shlex
import tempfile
import uuid

from textual.widgets import OptionList

import myrtui.pane_commands as pane_commands
from myrtui.client.app import TerminalApp
from myrtui.client.file_tree import FileTree
from myrtui.tmux_backend import CaptureResult, PaneInfo, TmuxBackend


def _which(mapping: dict[str, str]):
    return lambda name: mapping.get(name)


# ---------- 纯逻辑：编辑器解析 ----------

def test_resolve_editor_myrtui_editor_wins_with_args():
    spec = pane_commands.resolve_editor(
        which=_which({"vim": "/bin/vim"}),
        env={"MYRTUI_EDITOR": "myedit -u NONE", "EDITOR": "vim"},
    )
    assert spec is not None
    assert spec.argv == ("myedit", "-u", "NONE")
    assert spec.name == "myedit"


def test_resolve_editor_resolves_bare_name_to_absolute_path():
    spec = pane_commands.resolve_editor(
        which=_which({"vim": "/bin/vim"}),
        env={"EDITOR": "vim"},
    )
    assert spec is not None
    assert spec.argv == ("/bin/vim",)
    assert spec.name == "vim"


def test_resolve_editor_env_order_editor_then_visual():
    spec = pane_commands.resolve_editor(
        which=_which({"vis": "/bin/vis"}),
        env={"VISUAL": "vis"},
    )
    assert spec is not None and spec.argv == ("/bin/vis",)


def test_resolve_editor_invalid_quotes_skip_to_next_candidate():
    spec = pane_commands.resolve_editor(
        which=_which({"vim": "/bin/vim"}),
        env={"MYRTUI_EDITOR": "'unclosed", "EDITOR": "vim"},
    )
    assert spec is not None and spec.argv == ("/bin/vim",)


def test_resolve_editor_probe_order():
    spec = pane_commands.resolve_editor(
        which=_which({"vim": "/bin/vim", "vi": "/bin/vi"}),
        env={},
    )
    assert spec is not None and spec.argv == ("/bin/vim",)

    spec = pane_commands.resolve_editor(
        which=_which({"micro": "/bin/micro", "vim": "/bin/vim"}),
        env={},
    )
    assert spec is not None and spec.argv == ("/bin/micro",)


def test_resolve_editor_ignores_hx_and_nvim_unless_explicit():
    """自动探测不再选 helix / nvim；显式环境变量仍然尊重。"""
    spec = pane_commands.resolve_editor(
        which=_which({"hx": "/bin/hx", "nvim": "/bin/nvim", "vim": "/bin/vim"}),
        env={},
    )
    assert spec is not None and spec.argv == ("/bin/vim",)

    spec = pane_commands.resolve_editor(
        which=_which({"hx": "/bin/hx", "nvim": "/bin/nvim"}),
        env={"EDITOR": "nvim"},
    )
    assert spec is not None and spec.argv == ("/bin/nvim",)


def test_resolve_editor_none_when_nothing_installed():
    assert pane_commands.resolve_editor(which=_which({}), env={}) is None


def test_viewer_name_reflects_available_tools():
    assert (
        pane_commands.viewer_name(
            which=_which({"ov": "/bin/ov", "less": "/bin/less"})
        )
        == "ov"
    )
    assert pane_commands.viewer_name(which=_which({"less": "/bin/less"})) == "less"
    assert pane_commands.viewer_name(which=_which({"cat": "/bin/cat"})) == "cat"


# ---------- 纯逻辑：分页器 ----------

def test_resolve_pager_prefers_ov_then_less():
    assert pane_commands.resolve_pager(
        which=_which({"ov": "/bin/ov", "less": "/bin/less"}), env={}
    ) == ("/bin/ov",)
    assert pane_commands.resolve_pager(
        which=_which({"less": "/bin/less"}), env={}
    ) == ("/bin/less", "-R")
    assert pane_commands.resolve_pager(which=_which({}), env={}) is None


def test_resolve_pager_env_override_with_args():
    assert pane_commands.resolve_pager(
        which=_which({"less": "/bin/less"}),
        env={"MYRTUI_PAGER": "less -R --mouse"},
    ) == ("/bin/less", "-R", "--mouse")


def test_pager_name():
    assert (
        pane_commands.pager_name(which=_which({"less": "/bin/less"}), env={})
        == "less"
    )
    assert pane_commands.pager_name(which=_which({}), env={}) is None


# ---------- 纯逻辑：build_open ----------

def test_build_open_explicit_editor_with_args_and_quoting(tmp_path):
    target = tmp_path / "a b'c.txt"
    target.write_text("hello\n")
    spec = pane_commands.EditorSpec(("code", "--wait"))
    plan = pane_commands.build_open(str(target), spec, which=_which({}), env={})
    assert plan.notice is None
    assert plan.command == f"code --wait {shlex.quote(str(target))}"


def test_build_open_uses_environment_editor(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello\n")
    plan = pane_commands.build_open(
        str(target),
        which=_which({"micro": "/bin/micro"}),
        env={"EDITOR": "micro"},
    )
    assert plan.command == f"/bin/micro {shlex.quote(str(target))}"


def test_build_open_without_editor_falls_back_to_less(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello\n")
    plan = pane_commands.build_open(
        str(target), which=_which({"less": "/bin/less"}), env={}
    )
    assert plan.command == f"/bin/less -R {shlex.quote(str(target))}"
    assert plan.notice is not None and "编辑器" in plan.notice


def test_build_open_rejects_binary_and_missing(tmp_path):
    binary = tmp_path / "bin.dat"
    binary.write_bytes(b"x\0y")
    plan = pane_commands.build_open(
        str(binary), which=_which({"vim": "/bin/vim"}), env={}
    )
    assert plan.command is None and "二进制" in plan.notice

    plan = pane_commands.build_open(
        str(tmp_path / "nope.txt"), which=_which({"vim": "/bin/vim"}), env={}
    )
    assert plan.command is None and "不存在" in plan.notice


# ---------- App：菜单标签 ----------

def _pane(index: int = 0, *, mouse: bool = False) -> PaneInfo:
    return PaneInfo(
        pane_id=f"p{index}",
        dead=False,
        command="zsh",
        alternate_on=False,
        cursor_x=0,
        cursor_y=0,
        cursor_flag=False,
        history_size=100,
        height=24,
        width=80,
        window_id=f"w{index}",
        window_index=index,
        mouse_standard=mouse,
        mouse_button=mouse,
        mouse_any=mouse,
        mouse_sgr=True,
        mouse_all=False,
    )


class _FakeBackend:
    def __init__(self, panes: list[PaneInfo], buffer: str | None = None):
        self.panes = list(panes)
        self.commands: list[tuple[str, str]] = []
        self.buffer = buffer

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

    def show_buffer(self):
        return self.buffer

    def send_text(self, pane_id, text):
        pass

    def send_keys(self, pane_id, *keys):
        pass

    def send_ctrl_a(self, pane_id):
        pass


def _project(tmp_path):
    (tmp_path / "a.txt").write_text("a\n")
    (tmp_path / "notes.md").write_text("# notes\n")
    return str(tmp_path)


def _tree_lines(tree: FileTree) -> list[str]:
    return [str(line.path[-1].label.plain) for line in tree._tree_lines]


def _fake_tools(monkeypatch, mapping):
    for var in ("MYRTUI_EDITOR", "EDITOR", "VISUAL", "MYRTUI_PAGER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(pane_commands.shutil, "which", _which(mapping))


def _menu_labels(
    tmp_path, monkeypatch, mapping, filename: str = "a.txt"
) -> list[str]:
    _fake_tools(monkeypatch, mapping)
    root = _project(tmp_path)
    backend = _FakeBackend([_pane()])
    app = TerminalApp(backend, root)

    async def scenario() -> list[str]:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one(FileTree)
            line = _tree_lines(tree).index(filename)
            await pilot.click(FileTree, offset=(4, line), button=3)
            await pilot.pause(0.2)
            menu = app.screen.query_one(OptionList)
            labels = [str(option.prompt) for option in menu.options]
            await pilot.press("escape")
            await pilot.pause(0.1)
            return labels

    return asyncio.run(scenario())


def test_menu_label_shows_editor_name(tmp_path, monkeypatch):
    labels = _menu_labels(
        tmp_path,
        monkeypatch,
        {"micro": "/bin/micro", "vim": "/bin/vim", "less": "/bin/less"},
    )
    assert labels[0] == "打开（micro）"


def test_menu_label_falls_back_to_viewer(tmp_path, monkeypatch):
    labels = _menu_labels(
        tmp_path, monkeypatch, {"less": "/bin/less"}
    )
    assert labels[0] == "查看（less）"


def test_menu_label_falls_back_to_ov(tmp_path, monkeypatch):
    labels = _menu_labels(
        tmp_path, monkeypatch, {"ov": "/bin/ov", "less": "/bin/less"}
    )
    assert labels[0] == "查看（ov）"


# ---------- 后端：剪贴板捕获 ----------

def test_backend_captures_clipboard_buffers(tmp_path):
    name = f"myrtui-v31-{uuid.uuid4().hex[:8]}"
    backend = TmuxBackend(name, name)
    try:
        backend.create_session(str(tmp_path), 80, 24)
        option = backend._run(
            ["show-options", "-gv", "set-clipboard"]
        ).stdout.strip()
        assert option == "on"

        assert backend.show_buffer() is None
        backend._run(["set-buffer", "-b", "t", "hello"])
        assert backend.show_buffer() == "hello"

        # 老会话（external）启动时补一次 on
        backend._run(["set", "-g", "set-clipboard", "external"])
        backend.enable_clipboard_capture()
        option = backend._run(
            ["show-options", "-gv", "set-clipboard"]
        ).stdout.strip()
        assert option == "on"
    finally:
        backend.kill_server()
