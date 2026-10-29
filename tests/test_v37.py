"""myrtui v0.37 测试：Sixel / TGP 位图（探测 / 公式 PNG / 本地图片 / 预览）。

CI **不装** ``textual-image`` / Pillow / TeX：所有依赖都 fake 注入；用到
Pillow 的用例内部 ``pytest.importorskip``，用到 pdflatex 的用例按可执行文件
存在性跳过。断言只锁行为口径，不锁像素细节。
"""
from __future__ import annotations

import asyncio
import shutil
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

import myrtui.client.imageprep as imageprep
import myrtui.client.mathimg as mathimg
import myrtui.client.mathrender as mathrender
from myrtui.client import graphics
from myrtui.client.app import TerminalApp
from myrtui.client import mdpreview
from myrtui.client.mdpreview import (
    ImageBlock,
    MarkdownPreviewScreen,
    MathBlock,
    MathMarkdown,
    _rewrite_image_paragraphs,
)
from myrtui.client.mathtext import math_parser_factory
from myrtui.tmux_backend import CaptureResult, PaneInfo


# ---------------------------------------------------------------------------
# 公共 fixture / 工具
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """不探测开发机的真 termtex / 真缓存；每个用例从干净状态开始。"""
    mathrender.reset_state()
    mathimg.reset_state()
    monkeypatch.setattr(mathrender, "probe_termtex", lambda: None)
    # 不探测真 Chrome / TeX：位图后端默认关（需要时用例里再显式打开）
    monkeypatch.setattr(mathimg, "select_backend", lambda env=None: (None, None))
    mathimg.set_cache_root(tmp_path / "cache")
    yield
    mathrender.reset_state()
    mathimg.reset_state()
    mathimg.set_cache_root(None)


def _fake_png(path: Path, size=(40, 20), color=(200, 200, 200, 255)) -> Path:
    """写一个真 PNG（Pillow 不可用时也要能造，供 magic / 尺寸测试）。"""
    width, height = size
    raw = b"".join(b"\x00" + bytes(color) * width for _ in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    data = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(data)
    return path


class _DummyImageWidget(Widget):
    """替身：不依赖 textual-image，只记录被塞进来的 image 参数。"""

    def __init__(self, image, **kwargs) -> None:
        super().__init__(**kwargs)
        self.image = image

    def compose(self) -> ComposeResult:
        yield Static("IMG", markup=False)


# ---------------------------------------------------------------------------
# graphics：能力探测
# ---------------------------------------------------------------------------


def _caps(sixel=False, tgp=False, cell=(10, 20)):
    return SimpleNamespace(
        sixel=sixel, tgp=tgp, cell_size=SimpleNamespace(width=cell[0], height=cell[1])
    )


def test_graphics_off_disables_everything():
    caps, warning = graphics.detect("off", probe=lambda: _caps(sixel=True))
    assert warning is None
    assert caps.bitmaps is False
    assert caps.mode == "off"


def test_graphics_unicode_disables_bitmaps():
    caps, _ = graphics.detect("unicode", probe=lambda: _caps(sixel=True))
    assert caps.bitmaps is False
    assert caps.mode == "unicode"


def test_graphics_sixel_probe_reports_capability_and_cell():
    caps, warning = graphics.detect("auto", probe=lambda: _caps(sixel=True, cell=(12, 24)), tty=True)
    assert warning is None
    assert caps.sixel is True
    assert caps.bitmaps is True
    assert caps.protocol == "sixel"
    assert (caps.cell_width, caps.cell_height) == (12, 24)


def test_graphics_tgp_probe():
    caps, _ = graphics.detect("auto", probe=lambda: _caps(tgp=True), tty=True)
    assert caps.tgp is True
    assert caps.protocol == "tgp"


def test_graphics_forced_sixel_without_support_warns():
    caps, warning = graphics.detect("sixel", probe=lambda: _caps(), tty=True)
    assert caps.bitmaps is False
    assert warning and "sixel" in warning


def test_graphics_forced_tgp_disables_sixel():
    caps, _ = graphics.detect("tgp", probe=lambda: _caps(sixel=True, tgp=True), tty=True)
    assert caps.sixel is False and caps.tgp is True


def test_graphics_non_tty_skips_probe_without_warning():
    called = []

    def probe():
        called.append(True)
        return _caps(sixel=True)

    caps, warning = graphics.detect("auto", probe=probe, tty=False)
    assert called == []
    assert caps.bitmaps is False
    assert warning is None


def test_graphics_probe_exception_falls_back():
    def probe():
        raise RuntimeError("boom")

    caps, warning = graphics.detect("auto", probe=probe, tty=True)
    assert caps.bitmaps is False
    assert warning is None


def test_graphics_missing_library_warns_in_auto_too():
    """v0.3.1：auto 下缺 textual-image 也必须给出可诊断的 warning。

    起因：正式版（pipx 装 wheel 不带 extra）静默回落，`V` 却提示「终端没有
    图像协议」——真正缺的是 Python 依赖。
    """
    caps, warning = graphics.detect(
        "auto", loader=lambda: None, tty=True
    )
    assert caps.bitmaps is False
    assert caps.available is False
    assert warning and "textual-image" in warning

    caps, warning = graphics.detect("sixel", loader=lambda: None, tty=True)
    assert caps.bitmaps is False and warning and "textual-image" in warning


def test_graphics_explicit_off_unicode_dont_blame_the_terminal():
    for mode in ("off", "unicode"):
        caps, warning = graphics.detect(mode, loader=lambda: None, tty=True)
        assert warning is None and caps.mode == mode


def test_graphics_blocked_reason_separates_causes(monkeypatch):
    missing = graphics.Capabilities(mode="unicode")
    monkeypatch.delenv(graphics.ENV_NAME, raising=False)
    reason = graphics.blocked_reason(missing)
    assert "textual-image" in reason and graphics.INSTALL_HINT in reason

    # 终端不支持：依赖在、只是没协议
    no_protocol = graphics.Capabilities(available=True, mode="unicode")
    assert "终端" in graphics.blocked_reason(no_protocol, env="auto")

    # 用户显式关掉：不要再指示去装依赖
    assert "MYRTUI_GRAPHICS=off" in graphics.blocked_reason(no_protocol, env="off")
    assert "MYRTUI_GRAPHICS=unicode" in graphics.blocked_reason(no_protocol, env="unicode")


def test_graphics_describe_flags_missing_library():
    missing = graphics.Capabilities(mode="unicode")
    assert "textual-image 缺失" in graphics.describe(missing)
    # 显式关闭不算「缺依赖」
    disabled = graphics.Capabilities(mode="unicode", available=True)
    assert "缺失" not in graphics.describe(disabled)


def test_graphics_invalid_mode_falls_back_to_auto():
    caps, warning = graphics.detect(
        "bogus", probe=lambda: _caps(sixel=True), tty=True
    )
    assert caps.sixel is True
    assert warning and "非法" in warning


def test_graphics_cells_for_rounds_up():
    caps = graphics.Capabilities(sixel=True, cell_width=10, cell_height=20)
    assert caps.cells_for(10, 20) == (1, 1)
    assert caps.cells_for(11, 21) == (2, 2)
    assert caps.cells_for(100, 200) == (10, 10)


def test_graphics_describe():
    off = graphics.describe(graphics.DISABLED)
    assert off == "graphics=off"
    on = graphics.describe(
        graphics.Capabilities(sixel=True, cell_width=10, cell_height=20)
    )
    assert "sixel" in on and "10x20" in on


# ---------------------------------------------------------------------------
# mathimg：LaTeX 模板 / 归一化 / 缓存 / 失败回落
# ---------------------------------------------------------------------------


def test_mathimg_build_tex_has_one_page_per_formula():
    tex = mathimg.build_tex([r"a^2", r"\frac{1}{2}"], "#E0E0E0")
    assert "multi=mathpage" in tex
    assert tex.count(r"\begin{mathpage}") == 2
    assert r"\color[HTML]{E0E0E0}" in tex
    assert r"\displaystyle a^2" in tex


def test_mathimg_build_tex_sanitizes_color():
    tex = mathimg.build_tex(["x"], "not-a-color")
    assert r"\color[HTML]{E0E0E0}" in tex


def test_mathimg_normalize_formula_strips_label_and_rewrites_tag():
    tex, tag = mathimg.normalize_formula(r"E=mc^2 \label{eq:1} \tag{3}")
    assert tag == "3"
    assert r"\label" not in tex
    assert tex.endswith(r"\qquad (3)")


def test_mathimg_cache_key_depends_on_color_background_dpi():
    base = dict(color="#E0E0E0", background="#1E1E1E", dpi=300, kind="pdftocairo")
    first = mathimg.cache_key("x^2", **base)
    assert first == mathimg.cache_key("x^2", **base)
    assert first != mathimg.cache_key("x^2", **{**base, "color": "#FFFFFF"})
    assert first != mathimg.cache_key("x^2", **{**base, "background": "#000000"})
    assert first != mathimg.cache_key("x^2", **{**base, "dpi": 150})


def test_mathimg_find_tools_missing_pdflatex(monkeypatch):
    monkeypatch.setattr(mathimg.shutil, "which", lambda name: None)
    tools, warning = mathimg.find_tools({})
    assert tools is None
    assert warning and "pdflatex" in warning


def test_mathimg_find_tools_prefers_pdftocairo(monkeypatch):
    def which(name):
        return f"/usr/bin/{name}" if name in ("pdflatex", "pdftocairo") else None

    monkeypatch.setattr(mathimg.shutil, "which", which)
    tools, warning = mathimg.find_tools({})
    assert warning is None
    assert tools is not None
    assert tools.kind == "pdftocairo"


def test_mathimg_find_tools_falls_back_to_pdftoppm(monkeypatch):
    def which(name):
        return f"/usr/bin/{name}" if name in ("pdflatex", "pdftoppm") else None

    monkeypatch.setattr(mathimg.shutil, "which", which)
    tools, _ = mathimg.find_tools({})
    assert tools is not None and tools.kind == "pdftoppm"


def test_mathimg_env_converter_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(mathimg.shutil, "which", lambda name: "/usr/bin/pdflatex" if name == "pdflatex" else None)
    tools, warning = mathimg.find_tools({"MYRTUI_PDFTOCAIRO": str(tmp_path / "nope")})
    assert tools is None
    assert warning and "MYRTUI_PDFTOCAIRO" in warning


def test_mathimg_image_cached_requires_existing_file(tmp_path):
    key = mathimg.cache_key(
        mathimg.normalize_formula("x")[0],
        color="#E0E0E0",
        background="#1E1E1E",
        dpi=300,
        kind="latex",
    )
    assert (
        mathimg.image_cached("x", color="#E0E0E0", background="#1E1E1E", kind="latex")
        is None
    )
    target = mathimg.cache_root() / f"{key}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    _fake_png(target)
    hit = mathimg.image_cached(
        "x", color="#E0E0E0", background="#1E1E1E", kind="latex"
    )
    assert hit == target


def test_mathimg_image_cached_normalizes_tag(tmp_path):
    """带 ``\\tag`` 的源码与预渲染同键（否则永远 miss）。"""
    tex, _ = mathimg.normalize_formula(r"x \tag{1}")
    key = mathimg.cache_key(
        tex, color="#E0E0E0", background="#1E1E1E", dpi=300, kind="latex"
    )
    target = mathimg.cache_root() / f"{key}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    _fake_png(target)
    assert (
        mathimg.image_cached(
            r"x \tag{1}", color="#E0E0E0", background="#1E1E1E", kind="latex"
        )
        == target
    )


def _fake_runner(pages: int = 2, fail: bool = False, garbage: bool = False):
    """伪造 pdflatex / 转换器：产出 math.pdf + page-N.png。"""
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        exe = Path(command[0]).name
        if fail:
            return SimpleNamespace(returncode=1, stderr=b"fake failure", stdout=b"")
        cwd = Path(kwargs.get("cwd") or ".")
        if "pdflatex" in exe:
            (cwd / "math.pdf").write_bytes(b"%PDF-1.4 fake")
        else:
            prefix = Path(command[-1])
            if garbage:
                (Path(str(prefix) + "-1.png")).write_bytes(b"not a png")
            else:
                for index in range(1, pages + 1):
                    _fake_png(Path(f"{prefix}-{index}.png"))
        return SimpleNamespace(returncode=0, stderr=b"", stdout=b"")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def _tools():
    return mathimg.Tools("/usr/bin/pdflatex", "/usr/bin/pdftocairo", "pdftocairo")


@pytest.mark.parametrize("count", [1, 2, 3])
def test_mathimg_render_formulas_batch(count):
    pytest.importorskip("PIL.Image")
    runner = _fake_runner(pages=count)
    result = mathimg.render_formulas(
        [f"x^{i}" for i in range(count)],
        color="#E0E0E0",
        background="#1E1E1E",
        tools=_tools(),
        runner=runner,
    )
    assert set(result) == set(range(count))
    for path in result.values():
        assert path.is_file() and path.read_bytes().startswith(mathimg.PNG_MAGIC)
    # 一次批量编译 + 一次转换
    assert sum("pdflatex" in Path(c[0]).name for c in runner.calls) == 1


def test_mathimg_render_formulas_uses_cache_second_time():
    pytest.importorskip("PIL.Image")
    runner = _fake_runner(pages=1)
    first = mathimg.render_formulas(
        ["x"], color="#E0E0E0", background="#1E1E1E", tools=_tools(), runner=runner
    )
    assert first
    calls = len(runner.calls)
    second = mathimg.render_formulas(
        ["x"], color="#E0E0E0", background="#1E1E1E", tools=_tools(), runner=runner
    )
    assert second and len(runner.calls) == calls


def test_mathimg_render_formulas_failure_returns_empty_and_marks_failed():
    pytest.importorskip("PIL.Image")
    runner = _fake_runner(fail=True)
    result = mathimg.render_formulas(
        ["x"], color="#E0E0E0", background="#1E1E1E", tools=_tools(), runner=runner
    )
    assert result == {}
    # 失败后不再重试（once 语义）
    calls = len(runner.calls)
    again = mathimg.render_formulas(
        ["x"], color="#E0E0E0", background="#1E1E1E", tools=_tools(), runner=runner
    )
    assert again == {}
    assert len(runner.calls) == calls


def test_mathimg_render_formulas_garbage_output_rejected():
    pytest.importorskip("PIL.Image")
    runner = _fake_runner(pages=1, garbage=True)
    result = mathimg.render_formulas(
        ["x"], color="#E0E0E0", background="#1E1E1E", tools=_tools(), runner=runner
    )
    assert result == {}


def test_mathimg_render_formulas_respects_limit():
    pytest.importorskip("PIL.Image")
    runner = _fake_runner(pages=1)
    result = mathimg.render_formulas(
        ["a", "b", "c"],
        color="#E0E0E0",
        background="#1E1E1E",
        tools=_tools(),
        runner=runner,
        max_formulas=1,
    )
    assert set(result) == {0}


def test_mathimg_prewarm_document_skips_without_math():
    assert mathimg.prewarm_document("plain text", color="#E0E0E0", background="#1E1E1E") is None


def test_mathimg_prewarm_document_respects_off(monkeypatch):
    called = []
    monkeypatch.setattr(mathimg, "find_tools", lambda env=None: called.append(1) or (None, "x"))
    text = "$$\nx\n$$"
    assert (
        mathimg.prewarm_document(
            text, color="#E0E0E0", background="#1E1E1E", env={"MYRTUI_MATH_IMG": "off"}
        )
        is None
    )
    assert called == []


def test_mathimg_prewarm_document_without_tools_returns_warning(monkeypatch):
    monkeypatch.setattr(
        mathimg, "select_backend", lambda env=None: (None, "缺 pdflatex")
    )
    warning = mathimg.prewarm_document(
        "$$\nx\n$$", color="#E0E0E0", background="#1E1E1E"
    )
    assert warning == "缺 pdflatex"


@pytest.mark.skipif(
    shutil.which("pdflatex") is None or shutil.which("pdftocairo") is None,
    reason="本机没有 pdflatex / pdftocairo",
)
def test_mathimg_real_latex_pipeline(tmp_path):
    pytest.importorskip("PIL.Image")
    tools, _warning = mathimg.find_tools()
    assert tools is not None
    result = mathimg.render_formulas(
        [r"\frac{a}{b}", r"\sum_{i=1}^{n} i^2"],
        color="#E0E0E0",
        background="#1E1E1E",
        tools=tools,
    )
    assert set(result) == {0, 1}
    from PIL import Image

    for path in result.values():
        with Image.open(path) as image:
            assert image.mode == "RGB"
            assert image.width > 4 and image.height > 4


# ---------------------------------------------------------------------------
# imageprep：路径 / 限制 / 预合成
# ---------------------------------------------------------------------------


def test_imageprep_extracts_standalone_only():
    text = (
        "before\n\n![alt](a.png)\n\nafter\n\n行内 ![x](b.png) 不算\n"
    )
    assert imageprep.extract_standalone_images(text) == [("a.png", "alt")]


def test_imageprep_ignores_plain_links():
    assert imageprep.extract_standalone_images("[x](a.png)") == []


@pytest.mark.parametrize("src", ["https://x/y.png", "http://x/y.png", "data:image/png;base64,AA"])
def test_imageprep_rejects_remote(src, tmp_path):
    path, reason = imageprep.resolve_image(src, tmp_path)
    assert path is None
    assert reason


def test_imageprep_resolves_relative_and_absolute(tmp_path):
    target = tmp_path / "img.png"
    _fake_png(target)
    relative, reason = imageprep.resolve_image("img.png", tmp_path)
    assert reason is None and relative == target.resolve()
    absolute, reason = imageprep.resolve_image(str(target), tmp_path)
    assert reason is None and absolute == target.resolve()


def test_imageprep_resolves_file_url(tmp_path):
    target = tmp_path / "img.png"
    _fake_png(target)
    path, reason = imageprep.resolve_image(f"file://{target}", tmp_path)
    assert reason is None and path == target.resolve()


def test_imageprep_missing_file(tmp_path):
    path, reason = imageprep.resolve_image("nope.png", tmp_path)
    assert path is None and reason == "图片不存在"


def test_imageprep_directory_rejected(tmp_path):
    path, reason = imageprep.resolve_image(".", tmp_path)
    assert path is None and reason


def test_imageprep_oversize_rejected(tmp_path, monkeypatch):
    target = tmp_path / "big.png"
    _fake_png(target)
    monkeypatch.setattr(imageprep, "MAX_IMAGE_BYTES", 1)
    path, reason = imageprep.resolve_image("big.png", tmp_path)
    assert path is None and "过大" in reason


def test_imageprep_prepare_document_uses_injected_prepare(tmp_path):
    def fake_prepare(src, alt, **kwargs):
        return imageprep.Prepared(source=src, alt=alt, image=object(), width_cells=3, height_cells=2)

    text = "![a](a.png)\n\n![b](b.png)\n"
    result = imageprep.prepare_document(
        text,
        base_dir=tmp_path,
        background="#1E1E1E",
        max_width_px=800,
        max_height_px=400,
        prepare=fake_prepare,
    )
    assert set(result) == {"a.png", "b.png"}
    assert result["a.png"].ok


def test_imageprep_prepare_document_limit(tmp_path):
    def fake_prepare(src, alt, **kwargs):
        return imageprep.Prepared(source=src, alt=alt, image=object())

    text = "\n\n".join(f"![{i}]({i}.png)" for i in range(5))
    result = imageprep.prepare_document(
        text,
        base_dir=tmp_path,
        background="#1E1E1E",
        max_width_px=800,
        max_height_px=400,
        limit=2,
        prepare=fake_prepare,
    )
    assert len(result) == 2


def test_imageprep_prepare_document_survives_failure(tmp_path):
    def fake_prepare(src, alt, **kwargs):
        raise RuntimeError("boom")

    result = imageprep.prepare_document(
        "![a](a.png)",
        base_dir=tmp_path,
        background="#1E1E1E",
        max_width_px=800,
        max_height_px=400,
        prepare=fake_prepare,
    )
    assert result["a.png"].ok is False
    assert result["a.png"].note


def test_imageprep_prepare_one_flattens_and_scales(tmp_path):
    pytest.importorskip("PIL.Image")
    from PIL import Image

    target = tmp_path / "img.png"
    image = Image.new("RGBA", (400, 200), (255, 0, 0, 0))
    image.paste((0, 255, 0, 255), (0, 0, 100, 100))
    image.save(target)

    prepared = imageprep.prepare_one(
        "img.png",
        "alt",
        base_dir=tmp_path,
        background="#1E1E1E",
        max_width_px=200,
        max_height_px=400,
        cell=(10, 20),
    )
    assert prepared.ok
    assert prepared.image.mode == "RGB"
    assert prepared.image.width <= 200
    # 透明区域被预合成成背景色
    assert prepared.image.getpixel((prepared.image.width - 1, prepared.image.height - 1)) == (30, 30, 30)


def test_imageprep_prepare_one_broken_file(tmp_path):
    pytest.importorskip("PIL.Image")
    target = tmp_path / "bad.png"
    target.write_bytes(b"not really a png")
    prepared = imageprep.prepare_one(
        "bad.png",
        "alt",
        base_dir=tmp_path,
        background="#1E1E1E",
        max_width_px=200,
        max_height_px=400,
        cell=(10, 20),
    )
    assert prepared.ok is False
    assert prepared.note


# ---------------------------------------------------------------------------
# mdpreview：token 改写 / 位图选择 / 回落
# ---------------------------------------------------------------------------


def _tokens(text):
    return math_parser_factory().parse(text)


def test_rewrite_image_paragraphs_keeps_surrounding_text():
    tokens = _tokens("before\n\n![alt](a.png)\n\nafter\n")
    rewritten = _rewrite_image_paragraphs(tokens)
    types = [token.type for token in rewritten]
    assert "myrtui_image" in types
    assert types.count("paragraph_open") == 2
    image_token = next(t for t in rewritten if t.type == "myrtui_image")
    assert image_token.content == "a.png"
    assert image_token.info == "alt"


def test_rewrite_image_paragraphs_leaves_inline_image_alone():
    tokens = _tokens("text ![alt](a.png) more\n")
    rewritten = _rewrite_image_paragraphs(tokens)
    assert all(token.type != "myrtui_image" for token in rewritten)


def test_image_block_falls_back_to_alt_without_bitmaps(tmp_path):
    block = ImageBlock(SimpleNamespace(graphics=graphics.DISABLED, prepared_images={}), "a.png", "图示")
    widgets = list(block.compose())
    assert len(widgets) == 1
    assert "🖼 图示" in str(widgets[0].content)


def test_image_block_notes_reason(tmp_path):
    prepared = imageprep.Prepared(source="a.png", alt="图示", note="图片不存在")
    block = ImageBlock(
        SimpleNamespace(graphics=graphics.DISABLED, prepared_images={"a.png": prepared}),
        "a.png",
        "图示",
    )
    widgets = list(block.compose())
    assert "图片不存在" in str(widgets[0].content)


def test_image_block_uses_bitmap_widget_when_available(monkeypatch):
    monkeypatch.setattr(
        "myrtui.client.mdpreview._image_widget_class", lambda caps: _DummyImageWidget
    )
    prepared = imageprep.Prepared(
        source="a.png",
        alt="图示",
        image=SimpleNamespace(width=100, height=50),
        width_cells=3,
        height_cells=2,
    )
    markdown = SimpleNamespace(
        graphics=graphics.Capabilities(sixel=True),
        prepared_images={"a.png": prepared},
        graphics_colors=("#E0E0E0", "#1E1E1E"),
    )
    block = ImageBlock(markdown, "a.png", "图示")
    widgets = list(block.compose())
    assert len(widgets) == 1
    assert isinstance(widgets[0], _DummyImageWidget)


def test_display_cells_keeps_aspect_and_caps_rows():
    caps = graphics.Capabilities(sixel=True, cell_width=10, cell_height=20)
    # 600x300 天然 60x15，不放大
    assert mdpreview._display_cells(600, 300, caps, 76) == (60, 15)
    # 超宽：压到 76 列，等比
    width, height = mdpreview._display_cells(3000, 450, caps, 76)
    assert width == 76 and 1 <= height <= 20
    # 超高：压到 20 行，等比
    width, height = mdpreview._display_cells(400, 2000, caps, 76)
    assert height == 20 and width <= 76


def test_mathimg_terminal_em_matches_body_text():
    # 20px cell / 1.2em ≈ 16.7px 是终端 em；CM 的 x-height 更小（0.43 vs 0.52），
    # 默认 ×1.2 补偿后 ≈ cell 高（真机反馈「字太小」的修法）
    assert 18.0 <= mathimg.terminal_em_px((10, 20)) <= 21.0
    assert mathimg.terminal_em_px((10, 20), 1.0) < mathimg.terminal_em_px((10, 20))
    assert mathimg.terminal_em_px((20, 40)) > mathimg.terminal_em_px((10, 20))
    assert mathimg.terminal_em_px((10, 0)) > 0


def test_mathimg_scale_env_and_clamp(monkeypatch):
    assert mathimg.math_img_scale({}) == mathimg.DEFAULT_SCALE
    assert mathimg.math_img_scale({"MYRTUI_MATH_IMG_SCALE": "1.35"}) == 1.35
    assert mathimg.math_img_scale({"MYRTUI_MATH_IMG_SCALE": "big"}) == mathimg.DEFAULT_SCALE
    assert mathimg.math_img_scale({"MYRTUI_MATH_IMG_SCALE": "9"}) == mathimg.DEFAULT_SCALE
    # 倍率进缓存键：改倍率必须重新出图
    base = dict(
        color="#E0E0E0", background="#1E1E1E", dpi=300, kind="latex", cell=(10, 20)
    )
    assert mathimg.cache_key("x", em=20.0, **base) != mathimg.cache_key("x", em=24.0, **base)


def test_katex_raster_dpi_is_two_times_em():
    em = mathimg.terminal_em_px((10, 20))
    dpi = __import__("myrtui.client.katex", fromlist=["raster_dpi"]).raster_dpi(em)
    # dpi/96 * 16px = 2 * em
    assert abs((dpi / 96) * 16 - 2 * em) <= 2


def test_display_cells_never_upscales():
    caps = graphics.Capabilities(sixel=True, cell_width=10, cell_height=20)
    assert mdpreview._display_cells(450, 120, caps, 200) == (45, 6)


def test_display_cells_box_never_smaller_than_image():
    """盒子必须 ≥ 图片：否则 textual-image 会拉满盒子，造成非等比拉伸。"""
    caps = graphics.Capabilities(sixel=True, cell_width=10, cell_height=20)
    for width, height in ((216, 59), (575, 42), (263, 55), (12, 12), (700, 60)):
        cells = mdpreview._display_cells(width, height, caps, 76)
        assert cells[0] * 10 >= width
        assert cells[1] * 20 >= height
    # 超宽：盒子必然小于图片（等比缩小是刻意的），交给 _padded_formula 等比缩
    cells = mdpreview._display_cells(1666, 104, caps, 76)
    assert cells[0] == 76


def test_mathimg_png_size(tmp_path):
    png = _fake_png(tmp_path / "a.png", size=(40, 20))
    assert mathimg.png_size(png) == (40, 20)
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"nope")
    assert mathimg.png_size(bad) is None


def test_math_block_falls_back_to_text_without_bitmap(monkeypatch):
    monkeypatch.setattr(mathimg, "image_cached", lambda *a, **k: None)
    token = SimpleNamespace(content="x^2")
    markdown = SimpleNamespace(
        graphics=graphics.DISABLED,
        graphics_colors=("#E0E0E0", "#1E1E1E"),
    )
    block = MathBlock(markdown, token)
    widgets = list(block.compose())
    assert len(widgets) == 1
    assert "x^2" in str(widgets[0].content)


def test_math_block_cached_termtex_text(monkeypatch):
    monkeypatch.setattr(mathimg, "image_cached", lambda *a, **k: None)
    token = SimpleNamespace(content="\\frac{a}{b}")
    markdown = SimpleNamespace(
        graphics=graphics.DISABLED,
        graphics_colors=("#E0E0E0", "#1E1E1E"),
        app=SimpleNamespace(size=SimpleNamespace(width=100)),
    )
    mathrender._CACHE.put(("\\frac{a}{b}", mathrender.clamp_width(100)), "a\n─\nb")
    block = MathBlock(markdown, token)
    widgets = list(block.compose())
    assert "─" in str(widgets[0].content)


# ---------------------------------------------------------------------------
# 接线：TerminalApp / 预览屏透传能力
# ---------------------------------------------------------------------------


class _FakeBackend:
    def __init__(self, *, command: str = "zsh"):
        self.panes = [
            PaneInfo(
                pane_id="p0",
                dead=False,
                command=command,
                alternate_on=False,
                cursor_x=0,
                cursor_y=0,
                cursor_flag=False,
                history_size=100,
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
        self.commands: list[tuple[str, str]] = []
        self.sent: list[tuple] = []

    def list_panes(self):
        return list(self.panes)

    def poll(self, offsets):
        return list(self.panes), {
            pane.pane_id: CaptureResult(text="", offset=0) for pane in self.panes
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
    (tmp_path / "notes.md").write_text(
        "# 标题\n\n段落一。\n\n![图](pic.png)\n\n段落二。\n\n$$\n\\frac{a}{b}\n$$\n",
        encoding="utf-8",
    )
    (tmp_path / "pic.txt").write_text("not an image\n", encoding="utf-8")
    return str(tmp_path)


def test_app_default_graphics_disabled(tmp_path):
    app = TerminalApp(_FakeBackend(), _project(tmp_path))
    assert app._graphics.bitmaps is False


def test_preview_passes_graphics_and_keeps_image_alt(tmp_path):
    caps = graphics.Capabilities(sixel=True, cell_width=10, cell_height=20)
    app = TerminalApp(_FakeBackend(), _project(tmp_path), graphics=caps)
    absolute = str((tmp_path / "notes.md").resolve())

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            app._context_menu_closed("preview", "notes.md", absolute, False)
            await pilot.pause(0.8)
            screen = app.screen
            assert isinstance(screen, MarkdownPreviewScreen)
            markdown = screen.query_one("#mdpreview-md")
            assert markdown.graphics is caps
            # 图片路径存在但不是图片 → alt 回落，且前后段落都在
            text = " ".join(
                str(widget.content)
                for widget in markdown.query("MarkdownBlock")
            )
            assert "段落一" in text or "段落二" in text
            blocks = markdown.query(ImageBlock)
            assert blocks
            assert "🖼 图" in str(blocks.first().query_one(Static).content)

    asyncio.run(scenario())


def test_preview_math_without_bitmaps_uses_text(tmp_path):
    app = TerminalApp(_FakeBackend(), _project(tmp_path))
    absolute = str((tmp_path / "notes.md").resolve())

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            app._context_menu_closed("preview", "notes.md", absolute, False)
            await pilot.pause(0.6)
            blocks = app.screen.query(MathBlock)
            assert blocks
            assert "\\frac{a}{b}" in str(blocks.first().query_one(Static).content)

    asyncio.run(scenario())


def test_preview_math_stays_termtex_even_with_bitmaps(tmp_path, monkeypatch):
    """文本预览口径：块级公式也只走 termtex，不再调用公式位图。"""
    called: list = []
    monkeypatch.setattr(
        mathimg, "image_cached", lambda *a, **k: called.append(1)
    )
    caps = graphics.Capabilities(
        sixel=True, cell_width=10, cell_height=20, available=True, mode="sixel"
    )
    app = TerminalApp(_FakeBackend(), _project(tmp_path), graphics=caps)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            app._context_menu_closed(
                "preview", "notes.md", str((tmp_path / "notes.md").resolve()), False
            )
            await pilot.pause(0.6)
            block = app.screen.query_one(MathBlock)
            assert block.query_one(Static)
            assert called == []

    asyncio.run(scenario())


def test_mathmarkdown_rewrites_images_in_update(tmp_path):
    """Markdown.update 接受自定义 Widget（不依赖 MarkdownBlock.source_range）。"""
    app = TerminalApp(_FakeBackend(), _project(tmp_path))

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            app._context_menu_closed("preview", "notes.md", str((tmp_path / "notes.md").resolve()), False)
            await pilot.pause(0.6)
            markdown = app.screen.query_one("#mdpreview-md")
            assert isinstance(markdown, MathMarkdown)
            # 图片块与公式块都被挂载（不吞段落）
            assert markdown.query(ImageBlock)
            assert markdown.query(MathBlock)

    asyncio.run(scenario())
