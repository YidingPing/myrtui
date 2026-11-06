"""kitty 直接贴图（``a=T``）：载荷、tmux 包装、定位与贴图。

为什么单独立文件：``textual-image`` 的 TGP 只走 Unicode 占位符，而有些终端
（手机 SSH app 实测）能画 ``a=T``、也回 ``i=..;OK``，却不答 ``a=q``、不实现
``U=1``。这条路径只用于「一屏一张静态图」的界面，**不**参与正文滚动。

控件不走 Textual 的 segment 通道（它按单元格裁剪，会把 APC 尾巴切掉 → 白屏），
而是 ``render()`` 铺空格、帧画完后直接写终端。
"""
from __future__ import annotations

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


def test_set_image_reuses_the_same_id(monkeypatch):
    written: list[str] = []
    monkeypatch.setattr(directimage, "_write", written.append)

    widget = _Placed(b"page-1", cols=8, rows=2)
    widget.paint()
    first = widget._sequence
    widget.set_image(b"page-2")
    widget.paint()
    assert widget._sequence != first
    assert "cGFnZS0y" in widget._sequence       # "page-2"
    assert "i=%d" % widget.image_id in widget._sequence


def test_png_bytes_accepts_path_bytes_and_pil(tmp_path):
    path = tmp_path / "a.png"
    path.write_bytes(b"raw-png")
    assert directimage.png_bytes(path) == b"raw-png"
    assert directimage.png_bytes(b"bytes-png") == b"bytes-png"

    pytest.importorskip("PIL")
    from PIL import Image

    png = directimage.png_bytes(Image.new("RGB", (2, 2), (1, 2, 3)))
    assert png.startswith(b"\x89PNG")
