"""图片预览：后缀识别 / 解码 / 无位图降级 / 同目录切换 / 菜单入口。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from myrtui import pane_commands
from myrtui.client import graphics


def _make_png(path, size=(8, 4), color=(200, 120, 40, 255)):
    pytest.importorskip("PIL.Image")
    from PIL import Image

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    mode = "RGBA" if len(color) == 4 else "RGB"
    Image.new(mode, size, color).save(path)
    return path


def test_is_image_suffix_is_case_insensitive():
    assert pane_commands.is_image("a.PNG")
    assert pane_commands.is_image("/tmp/photo.jpeg")
    assert not pane_commands.is_image("notes.md")
    assert not pane_commands.is_image("plain.txt")


def test_load_image_reports_metadata_and_flattens(tmp_path):
    path = _make_png(tmp_path / "pic.png")
    from myrtui.client.imagepreview import load_image

    loaded = load_image(
        path,
        background="#123456",
        max_width_px=80,
        max_height_px=80,
        cell=(10, 20),
    )
    assert loaded.error is None
    assert loaded.prepared is not None and loaded.prepared.ok
    assert loaded.prepared.format == "PNG"
    assert loaded.prepared.original_size == (8, 4)
    # 预合成到指定背景（这里图片不透明，像素保持原色）
    assert loaded.prepared.image.getpixel((0, 0)) == (200, 120, 40)


def _screen_app(path, *, caps, relative=None):
    from textual.app import App

    from myrtui.client.imagepreview import ImagePreviewScreen

    class Demo(App):
        def on_mount(self) -> None:
            self.push_screen(
                ImagePreviewScreen(
                    str(path),
                    relative=relative or Path(path).name,
                    graphics=caps,
                )
            )

    return Demo()


class _FakeBackend:
    """最小 tmux 后端：只要能让 TerminalApp 起来并轮询即可。"""

    def __init__(self) -> None:
        from myrtui.tmux_backend import PaneInfo

        self.panes = [
            PaneInfo(
                pane_id="p0",
                dead=False,
                command="zsh",
                alternate_on=False,
                cursor_x=0,
                cursor_y=0,
                cursor_flag=False,
                history_size=0,
                height=24,
                width=80,
                window_id="w0",
                window_index=0,
                mouse_standard=False,
                mouse_button=False,
                mouse_any=False,
                mouse_sgr=True,
                mouse_all=False,
            )
        ]

    def list_panes(self):
        return list(self.panes)

    def poll(self, offsets):
        from myrtui.tmux_backend import CaptureResult

        return list(self.panes), {
            pane.pane_id: CaptureResult(text="", offset=0) for pane in self.panes
        }

    def resize_window(self, window_id, cols, rows):
        pass

    def run_command(self, pane_id, command):
        pass

    def send_text(self, pane_id, text):
        pass

    def send_keys(self, pane_id, *keys):
        pass

    def send_ctrl_a(self, pane_id):
        pass


def test_sibling_images_sorted_like_file_tree(tmp_path):
    from myrtui.client.imagepreview import sibling_images

    _make_png(tmp_path / "b.PNG")
    _make_png(tmp_path / "a.jpg", color=(200, 120, 40))
    (tmp_path / "notes.md").write_text("x", encoding="utf-8")
    _make_png(tmp_path / "sub.png" / "inner.png")  # 目录名像图片也必须跳过

    names = [item.name for item in sibling_images(tmp_path / "b.PNG")]
    assert names == ["a.jpg", "b.PNG"]  # 大小写不敏感，与文件树同序


def test_sibling_images_keeps_missing_target(tmp_path):
    from myrtui.client.imagepreview import sibling_images

    _make_png(tmp_path / "a.png")
    missing = tmp_path / "zz.png"
    names = [item.name for item in sibling_images(missing)]
    assert names == ["a.png", "zz.png"]  # 目标不在目录里也按序插回


def test_sibling_images_missing_directory(tmp_path):
    from myrtui.client.imagepreview import sibling_images

    ghost = tmp_path / "nope" / "x.png"
    assert sibling_images(ghost) == [ghost]


def test_image_screen_cycles_through_directory(tmp_path):
    """←/→ 在同目录图片间循环；header 显示 序号/总数。"""
    for name in ("a.png", "b.png", "c.png"):
        _make_png(tmp_path / name)
    caps = graphics.Capabilities(available=True, mode="unicode")

    async def scenario():
        app = _screen_app(tmp_path / "a.png", caps=caps)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.5)
            screen = app.screen
            assert "1/3" in screen._header_text
            await pilot.press("right")
            await pilot.pause(0.3)
            assert "2/3" in screen._header_text
            assert screen.current.endswith("b.png")
            await pilot.press("right")
            await pilot.pause(0.3)
            assert "3/3" in screen._header_text
            await pilot.press("right")  # 末尾 → 回到第一张
            await pilot.pause(0.3)
            assert "1/3" in screen._header_text
            await pilot.press("left")  # 开头 → 绕到最后一张
            await pilot.pause(0.3)
            assert "3/3" in screen._header_text

    asyncio.run(scenario())


def test_image_header_keeps_directory_prefix(tmp_path):
    """子目录里的图：切换后 header 仍带目录前缀，不假装在根目录。"""
    sub = tmp_path / "shots"
    _make_png(sub / "a.png")
    _make_png(sub / "b.png")
    caps = graphics.Capabilities(available=True, mode="unicode")

    async def scenario():
        app = _screen_app(sub / "a.png", caps=caps, relative="shots/a.png")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.5)
            screen = app.screen
            assert screen._header_text.startswith("shots/a.png")
            await pilot.press("right")
            await pilot.pause(0.3)
            assert screen._header_text.startswith("shots/b.png")

    asyncio.run(scenario())


def test_image_screen_single_image_has_no_counter(tmp_path):
    _make_png(tmp_path / "only.png")
    caps = graphics.Capabilities(available=True, mode="unicode")

    async def scenario():
        app = _screen_app(tmp_path / "only.png", caps=caps)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.5)
            screen = app.screen
            assert "1/1" not in screen._header_text
            await pilot.press("right")  # 单张：静默无操作
            await pilot.pause(0.2)
            assert "1/1" not in screen._header_text
            assert screen.current.endswith("only.png")

    asyncio.run(scenario())


def test_image_screen_reload_rescans_directory(tmp_path):
    """`r` 会重扫目录：中途放进来的新图能进入循环。"""
    _make_png(tmp_path / "a.png")
    caps = graphics.Capabilities(available=True, mode="unicode")

    async def scenario():
        app = _screen_app(tmp_path / "a.png", caps=caps)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.5)
            screen = app.screen
            assert "1/1" not in screen._header_text
            _make_png(tmp_path / "b.png")
            await pilot.press("r")
            await pilot.pause(0.4)
            assert "1/2" in screen._header_text
            await pilot.press("right")
            await pilot.pause(0.3)
            assert "2/2" in screen._header_text

    asyncio.run(scenario())


async def _open_menu(app, pilot, path, absolute):
    """请求右键菜单并等它挂上（菜单是 push_screen，需要一帧）。"""
    from textual.widgets import OptionList

    app.on_file_tree_path_context_requested(
        SimpleNamespace(
            path=path,
            absolute=absolute,
            is_dir=False,
            screen_x=10,
            screen_y=10,
        )
    )
    await pilot.pause(0.15)
    return app.screen.query_one(OptionList)


def test_image_menu_has_no_editor_entry(tmp_path):
    from myrtui.client.app import TerminalApp
    from myrtui.client.file_tree import FileTree

    (tmp_path / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    app = TerminalApp(_FakeBackend(), str(tmp_path))
    absolute = str((tmp_path / "pic.png").resolve())

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            menu = await _open_menu(app, pilot, "pic.png", absolute)
            assert [option.id for option in menu.options] == [
                "image",
                "copy_rel",
                "copy_abs",
            ]
            prompts = [str(option.prompt) for option in menu.options]
            assert prompts[0] == "预览图片"
            assert not any("打开" in text or "查看" in text for text in prompts)
            assert app.query_one(FileTree)

            from myrtui.client.imagepreview import ImagePreviewScreen

            app._context_menu_closed("image", "pic.png", absolute, False)
            await pilot.pause(0.3)
            assert isinstance(app.screen, ImagePreviewScreen)

    asyncio.run(scenario())


def test_markdown_menu_still_offers_editor(tmp_path):
    from myrtui.client.app import TerminalApp

    (tmp_path / "notes.md").write_text("# x\n", encoding="utf-8")
    app = TerminalApp(_FakeBackend(), str(tmp_path))
    absolute = str((tmp_path / "notes.md").resolve())

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            menu = await _open_menu(app, pilot, "notes.md", absolute)
            ids = [option.id for option in menu.options]
            assert ids[0] == "view"
            assert "preview" in ids
            first = str(menu.options[0].prompt)
            assert "打开" in first or "查看" in first

    asyncio.run(scenario())


def test_close_returns_last_image_and_moves_tree_cursor(tmp_path):
    """关闭预览后，文件树光标落到最后看的那张图。"""
    _make_png(tmp_path / "a.png")
    _make_png(tmp_path / "b.png")
    from myrtui.client.app import TerminalApp
    from myrtui.client.file_tree import FileTree

    app = TerminalApp(_FakeBackend(), str(tmp_path))
    absolute = str((tmp_path / "a.png").resolve())

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            app._context_menu_closed("image", "a.png", absolute, False)
            await pilot.pause(0.4)
            assert app.screen.__class__.__name__ == "ImagePreviewScreen"
            await pilot.press("n")
            await pilot.pause(0.4)
            await pilot.press("escape")
            await pilot.pause(0.4)
            tree = app.query_one(FileTree)
            assert tree.cursor_node is not None
            assert Path(tree.cursor_node.data.path).name == "b.png"

    asyncio.run(scenario())


def test_image_screen_without_bitmaps_shows_reason(tmp_path):
    path = _make_png(tmp_path / "pic.png")
    caps = graphics.Capabilities(available=True, mode="unicode")

    async def scenario():
        app = _screen_app(path, caps=caps)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.5)
            screen = app.screen
            texts = [
                str(child.render())
                for child in screen.query_one("#imagepreview-scroll").children
                if hasattr(child, "render")
            ]
            assert any("位图" in text or "Sixel" in text for text in texts)
            assert "8×4" in screen._header_text

    asyncio.run(scenario())


def test_image_screen_mounts_bitmap(tmp_path, monkeypatch):
    path = _make_png(tmp_path / "pic.png")
    from textual.widget import Widget

    import myrtui.client.imagepreview as imagepreview

    class Dummy(Widget):
        def __init__(self, image, **kwargs):
            super().__init__(**kwargs)
            self.image = image

    monkeypatch.setattr(imagepreview, "_image_widget_class", lambda caps: Dummy)
    caps = graphics.Capabilities(
        sixel=True, cell_width=10, cell_height=20, available=True, mode="sixel"
    )

    async def scenario():
        app = _screen_app(path, caps=caps)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.5)
            screen = app.screen
            children = list(screen.query_one("#imagepreview-scroll").children)
            assert children and isinstance(children[0], Dummy)

    asyncio.run(scenario())
