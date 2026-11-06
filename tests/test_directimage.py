"""kitty 直接贴图（``a=T``）：载荷、tmux 包装、控件渲染。

为什么单独立文件：``textual-image`` 的 TGP 只走 Unicode 占位符，而有些终端
（手机 SSH app 实测）能画 ``a=T``、也回 ``i=..;OK``，却不答 ``a=q``、不实现
``U=1``。这条路径只用于「一屏一张静态图」的界面，**不**参与正文滚动。
"""
from __future__ import annotations

import base64

import pytest
from textual.geometry import Size

from myrtui.client import directimage


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


def _render_text(widget) -> str:
    renderable = widget.render()
    return "".join(segment.text for segment in renderable.__rich_console__(None, None))


def test_render_reserves_cells_then_paints():
    widget = directimage.DirectImage(b"png-bytes")
    widget._size = Size(20, 4)
    text = _render_text(widget)
    assert text.startswith(" " * 20)          # 先占住 cell
    assert text.count("\n") == 4
    assert "\x1b[4A" in text                  # 回到首行
    assert "a=T" in text and "c=20" in text and "r=4" in text
    assert "i=%d" % widget.image_id in text


def test_render_reuses_the_sequence_until_resized():
    widget = directimage.DirectImage(b"png")
    widget._size = Size(10, 2)
    _render_text(widget)
    first = widget._sequence
    _render_text(widget)
    assert widget._sequence == first

    widget._size = Size(11, 2)
    _render_text(widget)
    assert widget._sequence != first and "c=11" in widget._sequence


def test_set_image_with_same_id_replaces_previous_page():
    widget = directimage.DirectImage(b"page-1")
    widget._size = Size(8, 2)
    _render_text(widget)
    first = widget._sequence
    widget.set_image(b"page-2")
    text = _render_text(widget)
    assert widget._sequence != first
    assert "cGFnZS0y" in text                 # "page-2"
    assert "i=%d" % widget.image_id in text   # 同一个 id：终端里旧图被覆盖


def test_png_bytes_accepts_path_bytes_and_pil(tmp_path):
    path = tmp_path / "a.png"
    path.write_bytes(b"raw-png")
    assert directimage.png_bytes(path) == b"raw-png"
    assert directimage.png_bytes(b"bytes-png") == b"bytes-png"

    pytest.importorskip("PIL")
    from PIL import Image

    png = directimage.png_bytes(Image.new("RGB", (2, 2), (1, 2, 3)))
    assert png.startswith(b"\x89PNG")
