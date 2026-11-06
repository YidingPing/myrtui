"""kitty 直接贴图（``a=T``）：载荷、tmux 包装、定位与贴图。

为什么单独立文件：``textual-image`` 的 TGP 只走 Unicode 占位符，而有些终端
（手机 SSH app 实测）能画 ``a=T``、也回 ``i=..;OK``，却不答 ``a=q``、不实现
``U=1``。这条路径只用于「一屏一张静态图」的界面，**不**参与正文滚动。

控件不走 Textual 的 segment 通道（它按单元格裁剪，会把 APC 尾巴切掉 → 白屏），
而是 ``render()`` 铺空格、帧画完后直接写终端。
"""
from __future__ import annotations

import asyncio
import base64

import pytest
from textual.geometry import Region

from myrtui.client import directimage


class _Placed(directimage.DirectImage):
    """绕过 layout：region 固定，测试直接看写出去的字节。"""

    region = Region(2, 0, 10, 3)


def test_display_sequence_has_placement_params():
    seq = directimage.display_sequence(b"\x89PNG-bytes", cols=12, rows=5, image_id=9)
    assert seq.startswith("\x1b_Ga=T,")
    assert "C=1" in seq          # 不动光标
    assert "c=12" in seq and "r=5" in seq
    assert "i=9" in seq
    assert "q=2" in seq          # 不要回执，避免污染 stdin
    assert base64.b64encode(b"\x89PNG-bytes").decode() in seq
    assert seq.endswith("\x1b\\")


def test_display_sequence_clamps_zero_size():
    seq = directimage.display_sequence(b"x", cols=0, rows=-3, image_id=1)
    assert "c=1" in seq and "r=1" in seq


def test_tmux_wrap_only_inside_tmux():
    seq = directimage.display_sequence(b"x", cols=1, rows=1, image_id=1)
    assert directimage.tmux_wrap(seq, env={}) == seq

    wrapped = directimage.tmux_wrap(seq, env={"TMUX": "/tmp/s,1,0"})
    assert wrapped.startswith("\x1bPtmux;")
    assert wrapped.endswith("\x1b\\")
    assert "\x1b\x1b_G" in wrapped   # 内层 ESC 必须翻倍，tmux 才认


def test_render_only_reserves_cells():
    """render 里不能夹带转义：Textual 会按 cell 宽裁剪（白屏的真因）。"""
    widget = _Placed(b"png")
    text = widget.render()
    assert "\x1b" not in text
    lines = text.splitlines()
    assert len(lines) == 3 and set(lines[0]) == {" "}


def test_paint_saves_cursor_moves_then_restores(monkeypatch):
    written: list[str] = []
    monkeypatch.setattr(directimage, "_write", written.append)

    widget = _Placed(b"png-bytes", cols=10, rows=3)
    widget.paint()
    assert len(written) == 1
    text = written[0]
    assert text.startswith("\x1b7\x1b[1;3H")   # 存光标 + 定位到 region 左上角
    assert "a=T" in text and "c=10" in text and "r=3" in text
    assert "i=%d" % widget.image_id in text
    assert text.endswith("\x1b8")              # 还原光标


def test_paint_wraps_the_whole_move_for_tmux(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/s,1,0")
    written: list[str] = []
    monkeypatch.setattr(directimage, "_write", written.append)

    _Placed(b"png", cols=4, rows=2).paint()
    text = written[0]
    # 光标移动也包在 passthrough 里：tmux 的屏幕模型不受影响
    assert text.startswith("\x1bPtmux;")
    assert "\x1b\x1b7\x1b\x1b[1;3H" in text
    assert text.endswith("\x1b\\")


def test_paint_resizes_to_region_when_cells_unknown(monkeypatch):
    written: list[str] = []
    monkeypatch.setattr(directimage, "_write", written.append)

    _Placed(b"png").paint()                    # 没传 cols/rows → 用 region
    assert "c=10" in written[0] and "r=3" in written[0]


def test_set_image_deletes_the_old_page_before_transmitting(monkeypatch):
    """真机三次试验定下的顺序：删除 → 传输。

    同 id 重传不重绘；贴完再删会整屏空白（`a=d` 被当成清空所有图）；完全不删
    则第二张图不出现。所以只能在贴新图之前把旧的删掉。
    """
    written: list[str] = []
    monkeypatch.setattr(directimage, "_write", written.append)

    widget = _Placed(b"page-1", cols=8, rows=2)
    widget.paint()
    first_id = widget.image_id
    first = widget._sequence
    assert "a=d" not in written[0]                 # 第一页没有可删的

    widget.set_image(b"page-2")
    assert widget.image_id != first_id
    widget.paint()

    text = written[-1]
    assert widget._sequence != first
    assert text.index(f"a=d,d=I,i={first_id}") < text.index("a=T")
    assert "cGFnZS0y" in text                      # "page-2"
    assert f"i={widget.image_id}" in text

    widget.on_unmount()
    assert f"a=d,d=I,i={widget.image_id}" in written[-1]


def test_assert_visible_replaces_without_retransmitting(monkeypatch):
    """兜底只用 `a=p`（几十字节）：重传整页在手机上太贵。"""
    written: list[str] = []
    monkeypatch.setattr(directimage, "_write", written.append)

    widget = _Placed(b"png", cols=10, rows=3)
    widget.paint()
    written.clear()
    widget.assert_visible()

    text = written[0]
    assert "a=p" in text and "a=T" not in text
    assert f"i={widget.image_id}" in text
    assert text.startswith("\x1b7\x1b[1;3H") and text.endswith("\x1b8")


def test_set_image_does_not_repaint_the_widget(monkeypatch):
    """换图不能把控件标脏：重画空格会把已贴的图擦掉（真机：翻页后空白）。"""
    written: list[str] = []
    monkeypatch.setattr(directimage, "_write", written.append)

    widget = _Placed(b"page-1", cols=8, rows=2)
    refreshed: list[bool] = []
    monkeypatch.setattr(widget, "refresh", lambda *a, **kw: refreshed.append(True))

    widget.set_image(b"page-2")
    assert refreshed == []


def test_png_bytes_accepts_path_bytes_and_pil(tmp_path):
    path = tmp_path / "a.png"
    path.write_bytes(b"raw-png")
    assert directimage.png_bytes(path) == b"raw-png"
    assert directimage.png_bytes(b"bytes-png") == b"bytes-png"

    pytest.importorskip("PIL")
    from PIL import Image

    png = directimage.png_bytes(Image.new("RGB", (2, 2), (1, 2, 3)))
    assert png.startswith(b"\x89PNG")


# ---------- sixel：帧后直写（0.11.2） ----------

_DCS = '\x1bP0;0;0q"1;1;100;60#0;2;0;0;0#0~~~~~~\x1b\\'
"""一段结构完整的假 DCS（头 + 光栅属性 + 数据 + ST）。"""


class _PlacedSixel(directimage.SixelPaint):
    """绕过 layout：region 固定，测试直接看写出去的字节。"""

    region = Region(2, 0, 10, 3)


@pytest.fixture(autouse=True)
def _clean_sixel_cache():
    directimage.clear_sixel_cache()
    yield
    directimage.clear_sixel_cache()


def test_sixel_render_only_reserves_cells():
    """render 里不能夹带转义：Textual 会按 cell 宽裁剪整段 DCS（白屏的真因）。"""
    widget = _PlacedSixel(b"png")
    text = widget.render()
    assert "\x1b" not in text
    lines = text.splitlines()
    assert len(lines) == 3 and set(lines[0]) == {" "}


def test_sixel_paint_writes_the_whole_dcs_between_cursor_saves(monkeypatch):
    written: list[str] = []
    monkeypatch.setattr(directimage, "_write", written.append)

    widget = _PlacedSixel(b"png", cols=10, rows=3)
    directimage.store_sixel(widget._key(10, 3), _DCS)
    widget.paint()

    assert len(written) == 1
    text = written[0]
    assert text.startswith("\x1b7\x1b[1;3H")  # 存光标 + 定位到 region 左上角
    assert text.endswith("\x1b8")             # 还原光标
    body = text[len("\x1b7\x1b[1;3H") : -len("\x1b8")]
    assert body == _DCS                       # DCS 完整，没有被裁剪
    assert body.startswith("\x1bP") and body.endswith("\x1b\\")


def test_sixel_cache_hit_does_not_encode_again(monkeypatch):
    written: list[str] = []
    monkeypatch.setattr(directimage, "_write", written.append)

    widget = _PlacedSixel(b"png", cols=10, rows=3)
    directimage.store_sixel(widget._key(10, 3), _DCS)
    encodes: list[tuple] = []
    monkeypatch.setattr(widget, "_encode", lambda *args: encodes.append(args))

    widget.paint()
    widget.paint()

    assert encodes == []          # 翻回同一页 / 同一尺寸：0 编码
    assert len(written) == 2


def test_sixel_paint_schedules_a_single_encode_for_a_miss(monkeypatch):
    widget = _PlacedSixel(b"png", cols=10, rows=3)
    calls: list[tuple] = []
    monkeypatch.setattr(widget, "_encode", lambda *args: calls.append(args))

    widget.paint()
    widget.paint()                # 编码未完成：不该重复排队

    assert len(calls) == 1
    assert directimage._SIXEL_INFLIGHT    # 在途去重


def test_sixel_failed_encode_is_negative_cached(monkeypatch):
    """编码失败只记一次 ``None``：之后保持空格，不每帧重试。"""
    written: list[str] = []
    monkeypatch.setattr(directimage, "_write", written.append)

    widget = _PlacedSixel(b"png", cols=10, rows=3)
    directimage.store_sixel(widget._key(10, 3), None)
    encodes: list[tuple] = []
    monkeypatch.setattr(widget, "_encode", lambda *args: encodes.append(args))

    widget.paint()

    assert encodes == [] and written == []


def test_sixel_encoding_runs_off_the_event_loop(monkeypatch):
    """编码必须在 worker 线程（整屏 ≈380 ms，事件循环上会卡住整个 UI）。"""
    import threading
    import time

    from textual.app import App

    written: list[str] = []
    monkeypatch.setattr(directimage, "_write", written.append)
    threads: list[threading.Thread] = []

    def fake_encode(self, cols, rows, cell, background):
        threads.append(threading.current_thread())
        return _DCS

    monkeypatch.setattr(directimage.SixelPaint, "_encode_image", fake_encode)

    class Demo(App):
        def compose(self):
            yield directimage.SixelPaint(b"png", cols=10, rows=3)

    async def scenario():
        app = Demo()
        async with app.run_test(size=(40, 12)) as pilot:
            deadline = time.monotonic() + 5
            while not written and time.monotonic() < deadline:
                await pilot.pause(0.02)
            assert written, "编码完成后没有补画"
            assert threads and threads[0] is not threading.main_thread()

    asyncio.run(scenario())


def test_background_rgba_accepts_short_and_long_hex():
    assert directimage.background_rgba("#102030") == (16, 32, 48, 1.0)
    assert directimage.background_rgba("#fff") == (255, 255, 255, 1.0)
    assert directimage.background_rgba("nonsense") == (0, 0, 0, 1.0)


def test_static_widget_class_chooses_sixel_paint_and_keeps_body_alt():
    """整页视图 / 图片预览 → SixelPaint；正文里滚动的图片仍回落 alt。"""
    from myrtui.client import graphics, mdpreview

    caps = graphics.Capabilities(sixel=True, available=True, mode="sixel")
    assert mdpreview._static_widget_class(caps) is directimage.SixelPaint
    assert mdpreview._image_widget_class(caps) is None


def test_static_widget_class_keeps_tgp_for_scrolling_body():
    pytest.importorskip("textual_image")
    from textual_image.widget import TGPImage

    from myrtui.client import graphics, mdpreview

    caps = graphics.Capabilities(tgp=True, available=True, mode="tgp")
    assert mdpreview._static_widget_class(caps) is TGPImage
    assert mdpreview._image_widget_class(caps) is TGPImage
