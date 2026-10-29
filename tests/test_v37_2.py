"""myrtui v0.37.2 测试：整篇 Markdown → PDF 分页位图（``v``）。

覆盖：

- ``docpage``：页面几何、缓存键、HTML（离线 / 行内与块级定界符 / 货币闸门 /
  ``\\label→\\tag`` / HTML 转义 / 本地图片 data URI / 远程图片丢弃）、
  ``render_pages``（fake Chrome + fake poppler：页数、尺寸归一、缓存命中、
  缺工具 / 失败 / 页数上限）；
- ``pageview``：页图控件的 cell 尺寸、失败提示、``r`` 清缓存、页号、
  预览层 ``v`` 的入口与降级；
- 真机冒烟（有 Chrome/Pillow 才跑）：两页 PDF 的像素尺寸必须等于页面尺寸。
"""
from __future__ import annotations

import asyncio
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import myrtui.client.docpage as docpage
import myrtui.client.mathimg as mathimg
from myrtui.client import graphics, katex
from myrtui.client.pageview import PagedDocScreen


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    docpage.set_cache_root(tmp_path / "pages")
    monkeypatch.setattr(docpage, "PROFILE_MARGIN", 0.0)
    # 开发机可能设了 MYRTUI_PAGE_THEME：用例默认按 sepia 断言
    monkeypatch.delenv(docpage.PAGE_THEME_ENV, raising=False)
    yield
    docpage.set_cache_root(None)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _fake_png(path: Path, size=(40, 20), color=(30, 30, 30)) -> Path:
    width, height = size
    raw = b"".join(b"\x00" + bytes(color) * width for _ in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return path


def _spec(cols=110, rows=39, cell=(10, 20)) -> docpage.PageSpec:
    return docpage.page_spec(cell, cols, rows)


def _fake_printer(calls: list, *, pages=2, size=None, fail=False):
    def printer(chrome, html, pdf, *, profile_dir, timeout):
        calls.append(("print", Path(html).read_text(encoding="utf-8")))
        if fail:
            raise RuntimeError("chrome boom")
        Path(pdf).write_bytes(b"%PDF-1.4 fake")
        printer.size = size  # type: ignore[attr-defined]
        printer.pages = pages  # type: ignore[attr-defined]

    return printer


def _fake_runner(calls: list, *, pages=2, size=None, fail=False):
    def runner(command, **kwargs):
        calls.append(("run", [str(part) for part in command]))
        if fail:
            return SimpleNamespace(returncode=1, stderr=b"poppler boom", stdout=b"")
        prefix = Path(command[-1])
        page_size = size or (1100, 780)
        for index in range(1, pages + 1):
            _fake_png(Path(f"{prefix}-{index}.png"), size=page_size)
        return SimpleNamespace(returncode=0, stderr=b"", stdout=b"")

    return runner


def _enable_tools(monkeypatch):
    monkeypatch.setattr(
        katex, "find_chrome", lambda env=None: katex.Chrome("/fake/chrome")
    )
    monkeypatch.setattr(
        mathimg, "find_converter", lambda env=None: ("/fake/pdftocairo", "pdftocairo")
    )


TEXT = "# 标题\n\n行内 $x^2$ 与 $5 and $10。\n\n$$\n\\frac{a}{b} \\label{eq:1}\n$$\n"


# ---------------------------------------------------------------------------
# docpage：几何 / 缓存键
# ---------------------------------------------------------------------------


def test_page_spec_is_viewport_in_cell_units():
    spec = _spec(cols=110, rows=39)
    assert spec.size == (1100, 780)
    assert spec.line_px == 20
    assert spec.font_px == 16
    assert spec.pad_x > 0


def test_page_spec_has_floor_for_tiny_terminals():
    spec = _spec(cols=1, rows=1)
    assert spec.width >= 200 and spec.height >= 200
    assert spec.font_px >= docpage.MIN_FONT_PX


def test_page_key_depends_on_geometry_colors_and_text():
    base = dict(source="/tmp/a.md", colors=("#E0E0E0", "#1E1E1E"), spec=_spec())
    key = docpage.page_key("x", **base)
    assert key == docpage.page_key("x", **base)  # 稳定
    assert key != docpage.page_key("y", **base)  # 正文
    assert key != docpage.page_key(
        "x",
        source="/tmp/b.md",
        colors=base["colors"],
        spec=base["spec"],
    )
    assert key != docpage.page_key(
        "x",
        source="/tmp/a.md",
        colors=("#FFFFFF", "#1E1E1E"),
        spec=base["spec"],
    )
    assert key != docpage.page_key(
        "x", source="/tmp/a.md", colors=base["colors"], spec=_spec(rows=40)
    )


# ---------------------------------------------------------------------------
# docpage：HTML
# ---------------------------------------------------------------------------


def test_build_html_uses_sepia_palette_by_default():
    html = docpage.build_document_html(
        TEXT, colors=docpage.PAGED_COLORS, spec=_spec()
    )
    assert "background: #E6DABE" in html
    assert "color: #3D3427" in html
    assert "rgba(61, 52, 39, 0.16)" in html
    assert "rgba(255, 255, 255" not in html
    assert "#14508F" in html


def test_build_html_paper_preset_still_available():
    html = docpage.build_document_html(
        "x", colors=docpage.PAPER_PAGE.colors, spec=_spec()
    )
    assert "background: #F7F4EC" in html
    assert "color: #26241F" in html


def test_build_html_white_preset_keeps_v040_look():
    html = docpage.build_document_html(
        "x", colors=docpage.WHITE_PAGE.colors, spec=_spec()
    )
    assert "background: #FFFFFF" in html
    assert "color: #111111" in html
    assert "#0969DA" in html


def test_palette_from_env_defaults_to_sepia(monkeypatch):
    monkeypatch.delenv(docpage.PAGE_THEME_ENV, raising=False)
    assert docpage.DEFAULT_PAGE_THEME == "sepia"
    assert docpage.palette_from_env().name == "sepia"
    assert docpage.palette_from_env("paper").name == "paper"
    assert docpage.palette_from_env("dim").name == "dim"


def test_palette_from_env_falls_back_on_bad_value(caplog):
    with caplog.at_level("WARNING", logger="myrtui.docpage"):
        palette = docpage.palette_from_env("sepia-magic")
    assert palette.name == docpage.DEFAULT_PAGE_THEME
    assert "MYRTUI_PAGE_THEME" in caplog.text


def test_palette_for_unknown_colors_is_derived():
    palette = docpage.palette_for_colors(("#DDE3EA", "#101418"))
    assert palette.name == "custom"
    assert palette.background == "#101418"
    assert palette.foreground == "#DDE3EA"
    assert "rgba(255, 255, 255" in palette.border


def test_default_palette_is_visibly_tinted():
    """光压亮度没用（paper / gray / dim 只差 2–6/255）：默认档必须有明显色度。"""
    palette = docpage.PAGE_THEMES[docpage.DEFAULT_PAGE_THEME]
    channels = docpage._rgb_tuple(palette.background)
    assert palette.background != "#FFFFFF"
    assert max(channels) - min(channels) >= 20  # 色度差够大，肉眼可辨
    assert sum(channels) / 3 <= 235  # 同时确实比纯白暗一档


def test_paged_screen_uses_env_palette(monkeypatch):
    monkeypatch.setenv(docpage.PAGE_THEME_ENV, "dim")
    screen = PagedDocScreen("/tmp/doc.md", text="x")
    assert screen._palette.name == "dim"
    assert screen._colors == docpage.DIM_PAGE.colors


def test_paged_screen_accepts_explicit_palette(monkeypatch):
    monkeypatch.setenv(docpage.PAGE_THEME_ENV, "dark")
    screen = PagedDocScreen(
        "/tmp/doc.md", text="x", palette=docpage.WHITE_PAGE
    )
    assert screen._palette.name == "white"
    assert screen._colors == docpage.WHITE_PAGE.colors


def test_normalize_page_flattens_transparency_onto_palette(tmp_path):
    pytest.importorskip("PIL.Image")
    from PIL import Image

    spec = _spec(cols=40, rows=10)
    path = tmp_path / "page.png"
    Image.new("RGBA", (spec.width + 3, spec.height + 2), (0, 0, 0, 0)).save(path)
    docpage._normalize_page(path, spec, docpage.PAPER_PAGE.background)
    with Image.open(path) as raw:
        assert raw.mode == "RGB"
        assert raw.size == spec.size
        assert raw.getpixel((0, 0)) == docpage._rgb_tuple("#F7F4EC")


def test_normalize_page_keeps_content_over_palette(tmp_path):
    pytest.importorskip("PIL.Image")
    from PIL import Image

    spec = _spec(cols=40, rows=10)
    path = tmp_path / "page.png"
    image = Image.new("RGBA", spec.size, (0, 0, 0, 0))
    image.paste((20, 20, 20, 255), (5, 5, 15, 15))
    image.save(path)
    docpage._normalize_page(path, spec, docpage.PAPER_PAGE.background)
    with Image.open(path) as raw:
        assert raw.convert("RGB").getpixel((10, 10)) == (20, 20, 20)
        assert raw.convert("RGB").getpixel((0, 0)) == docpage._rgb_tuple("#F7F4EC")


def test_build_html_is_offline_and_uses_local_assets():
    html = docpage.build_document_html(
        TEXT, colors=("#E0E0E0", "#1E1E1E"), spec=_spec()
    )
    assert "http://" not in html and "https://" not in html
    assert "file://" in html and "katex.min.js" in html
    assert "auto-render.min.js" in html
    assert "renderMathInElement" in html


def test_build_html_sets_exact_page_size():
    html = docpage.build_document_html(
        TEXT, colors=("#E0E0E0", "#1E1E1E"), spec=_spec(cols=110, rows=39)
    )
    assert "@page { size: 1100px 780px; margin: 0.0px; }" in html
    assert "print-color-adjust: exact" in html


def test_build_html_wraps_math_in_katex_delimiters():
    html = docpage.build_document_html(
        TEXT, colors=("#E0E0E0", "#1E1E1E"), spec=_spec()
    )
    assert "\\(x^2\\)" in html
    assert 'class="math-display"' in html
    assert "\\[\\frac{a}{b} \\tag{eq:1}\\]" in html


def test_build_html_keeps_currency_literal():
    html = docpage.build_document_html(
        "花费 $5 and $10 元", colors=("#E0E0E0", "#1E1E1E"), spec=_spec()
    )
    body = html.split("<body>")[1].split("<script src=")[0]
    assert "$5 and $10" in body
    assert "\\(" not in body


def test_build_html_rewrites_label_only_without_tag():
    assert docpage.rewrite_labels("a \\label{x}") == "a \\tag{x}"
    assert docpage.rewrite_labels("a \\tag{x} \\label{y}") == "a \\tag{x} "


def test_build_html_escapes_math_html():
    html = docpage.build_document_html(
        "文本 $a<b$ 与 $</script>$", colors=("#E0E0E0", "#1E1E1E"), spec=_spec()
    )
    body = html.split("<body>")[1].split("<script src=")[0]
    assert "a&lt;b" in body
    assert "</script>" not in body


def test_build_html_rejects_bad_color():
    html = docpage.build_document_html(
        "x", colors=("not-a-color", "also-bad"), spec=_spec()
    )
    assert "#E0E0E0" in html and "#1E1E1E" in html


def test_build_html_inlines_local_image(tmp_path):
    image = _fake_png(tmp_path / "pic.png", size=(8, 8))
    html = docpage.build_document_html(
        "![x](pic.png)",
        colors=("#E0E0E0", "#1E1E1E"),
        spec=_spec(),
        base_dir=tmp_path,
    )
    assert "data:image/png;base64," in html
    assert image.name not in html


def test_build_html_drops_remote_image():
    html = docpage.build_document_html(
        "![x](https://example.com/a.png)",
        colors=("#E0E0E0", "#1E1E1E"),
        spec=_spec(),
    )
    assert "https://example.com" not in html


# ---------------------------------------------------------------------------
# docpage：渲染（fake Chrome / poppler）
# ---------------------------------------------------------------------------


def test_render_pages_writes_cache_and_normalizes_size(monkeypatch):
    pytest.importorskip("PIL.Image")  # 尺寸归一用 Pillow
    _enable_tools(monkeypatch)
    calls: list = []
    pages = docpage.render_pages(
        TEXT,
        source="/tmp/a.md",
        colors=("#E0E0E0", "#1E1E1E"),
        spec=_spec(),
        printer=_fake_printer(calls, pages=2, size=(1100, 781)),
        runner=_fake_runner(calls, pages=2, size=(1100, 781)),
    )
    assert [p.name for p in pages] == ["p-001.png", "p-002.png"]
    assert all(p.is_file() for p in pages)
    # Chrome 少一两个 0.01pt → 781px；渲染后必须裁回 780
    from PIL import Image

    assert Image.open(pages[0]).size == (1100, 780)
    assert (pages[0].parent / "count").read_text() == "2"


def test_render_pages_cache_hit_skips_subprocess(monkeypatch):
    _enable_tools(monkeypatch)
    calls: list = []
    args = dict(source="/tmp/a.md", colors=("#E0E0E0", "#1E1E1E"), spec=_spec())
    docpage.render_pages(
        TEXT, **args, printer=_fake_printer(calls), runner=_fake_runner(calls)
    )
    before = len(calls)
    again = docpage.render_pages(
        TEXT, **args, printer=_fake_printer(calls), runner=_fake_runner(calls)
    )
    assert len(again) == 2
    assert len(calls) == before  # 0 子进程


def test_render_pages_requires_chrome(monkeypatch):
    _enable_tools(monkeypatch)
    monkeypatch.setattr(katex, "find_chrome", lambda env=None: None)
    with pytest.raises(docpage.PageRenderError, match="Chrome"):
        docpage.render_pages(
            TEXT,
            source="/tmp/a.md",
            colors=("#E0E0E0", "#1E1E1E"),
            spec=_spec(),
        )


def test_render_pages_requires_converter(monkeypatch):
    _enable_tools(monkeypatch)
    monkeypatch.setattr(mathimg, "find_converter", lambda env=None: None)
    with pytest.raises(docpage.PageRenderError, match="pdftocairo"):
        docpage.render_pages(
            TEXT,
            source="/tmp/a.md",
            colors=("#E0E0E0", "#1E1E1E"),
            spec=_spec(),
        )


def test_render_pages_rejects_too_many_pages(monkeypatch):
    _enable_tools(monkeypatch)
    calls: list = []
    with pytest.raises(docpage.PageRenderError, match="页数过多"):
        docpage.render_pages(
            TEXT,
            source="/tmp/a.md",
            colors=("#E0E0E0", "#1E1E1E"),
            spec=_spec(),
            printer=_fake_printer(calls, pages=docpage.MAX_PAGES + 1),
            runner=_fake_runner(calls, pages=docpage.MAX_PAGES + 1),
        )


def test_render_pages_propagates_failure(monkeypatch):
    _enable_tools(monkeypatch)
    calls: list = []
    with pytest.raises(RuntimeError, match="chrome boom"):
        docpage.render_pages(
            TEXT,
            source="/tmp/a.md",
            colors=("#E0E0E0", "#1E1E1E"),
            spec=_spec(),
            printer=_fake_printer(calls, fail=True),
            runner=_fake_runner(calls),
        )


def test_cached_pages_none_before_render():
    assert (
        docpage.cached_pages(
            TEXT,
            source="/tmp/a.md",
            colors=("#E0E0E0", "#1E1E1E"),
            spec=_spec(),
        )
        is None
    )


def test_invalidate_clears_cache(monkeypatch):
    _enable_tools(monkeypatch)
    calls: list = []
    args = dict(source="/tmp/a.md", colors=("#E0E0E0", "#1E1E1E"), spec=_spec())
    pages = docpage.render_pages(
        TEXT, **args, printer=_fake_printer(calls), runner=_fake_runner(calls)
    )
    assert pages and pages[0].is_file()
    docpage.invalidate(TEXT, **args)
    assert not pages[0].exists()
    assert docpage.cached_pages(TEXT, **args) is None


# ---------------------------------------------------------------------------
# pageview：LaTeX 排版视图
# ---------------------------------------------------------------------------


def _caps(**kwargs) -> graphics.Capabilities:
    base = dict(sixel=True, cell_width=10, cell_height=20, available=True, mode="sixel")
    base.update(kwargs)
    return graphics.Capabilities(**base)


def _paged_app(pages, *, caps=None, tmp_path=None):
    from textual.app import App

    class Demo(App):
        def on_mount(self) -> None:
            self.push_screen(
                PagedDocScreen(
                    "/tmp/doc.md",
                    text="# x\n",
                    graphics=caps or _caps(),
                    relative="doc.md",
                )
            )

    return Demo()


def test_paged_screen_mounts_pages_with_exact_cell_size(monkeypatch, tmp_path):
    pytest.importorskip("textual_image")  # 没装 [graphics] 时不会挂图
    spec = _spec(cols=110, rows=44)
    page = _fake_png(tmp_path / "p1.png", size=spec.size)
    monkeypatch.setattr(docpage, "cached_pages", lambda *a, **kw: [page])
    monkeypatch.setattr(
        docpage,
        "render_pages",
        lambda *a, **kw: pytest.fail("缓存命中不该渲染"),
    )

    async def scenario():
        app = _paged_app([page])
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.pause(0.4)
            screen = app.screen
            assert isinstance(screen, PagedDocScreen)
            container = screen.query_one("#paged-scroll")
            children = list(container.children)
            assert len(children) == 1
            widget = children[0]
            assert int(widget.styles.width.value) == 110
            assert int(widget.styles.height.value) == 44
            assert "LaTeX 排版 · 1 页" in screen._header_text

    asyncio.run(scenario())


def test_paged_screen_paints_chrome_with_palette(monkeypatch):
    """纸色必须同时铺到屏幕 / header / 滚动区，否则会露出白边和白色标题栏。"""

    async def scenario():
        app = _paged_app([])
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.pause(0.3)
            screen = app.screen
            assert screen.styles.background.hex.lower() == "#e6dabe"
            header = screen.query_one("#paged-header")
            assert header.styles.background.hex.lower() == "#dcd0b5"
            assert header.styles.color.hex.lower() == "#3d3427"
            scroll = screen.query_one("#paged-scroll")
            assert scroll.styles.background.hex.lower() == "#e6dabe"

    asyncio.run(scenario())


def test_paged_screen_shows_message_on_failure(monkeypatch):
    monkeypatch.setattr(docpage, "cached_pages", lambda *a, **kw: None)

    def boom(*args, **kwargs):
        raise docpage.PageRenderError("未找到 Chrome/Chromium（整篇渲染需要它）")

    monkeypatch.setattr(docpage, "render_pages", boom)

    async def scenario():
        app = _paged_app([])
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.pause(0.4)
            screen = app.screen
            texts = [
                str(child.render())
                for child in screen.query_one("#paged-scroll").children
                if hasattr(child, "render")
            ]
            assert any("LaTeX 排版失败" in text for text in texts)
            assert "Chrome" in screen._header_text + "".join(texts)

    asyncio.run(scenario())


def test_paged_screen_reload_invalidates_cache(monkeypatch, tmp_path):
    spec = _spec(cols=110, rows=44)
    page = _fake_png(tmp_path / "p1.png", size=spec.size)
    invalidated: list = []
    monkeypatch.setattr(docpage, "cached_pages", lambda *a, **kw: [page])
    monkeypatch.setattr(
        docpage, "invalidate", lambda *a, **kw: invalidated.append(True)
    )

    async def scenario():
        app = _paged_app([page])
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.pause(0.4)
            await pilot.press("r")
            await pilot.pause(0.4)
            assert invalidated

    asyncio.run(scenario())


def test_paged_screen_hides_scrollbar_to_keep_one_to_one(monkeypatch, tmp_path):
    spec = _spec(cols=110, rows=44)
    page = _fake_png(tmp_path / "p1.png", size=spec.size)
    monkeypatch.setattr(docpage, "cached_pages", lambda *a, **kw: [page])

    async def scenario():
        app = _paged_app([page])
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.pause(0.4)
            container = app.screen.query_one("#paged-scroll")
            # 滚动条不占列 → 页面宽度 == 终端宽度（1:1，不重采样）
            assert container.size.width == 110

    asyncio.run(scenario())


def test_preview_action_paged_requires_bitmaps(monkeypatch):
    """缺依赖时提示要指向安装命令，而不是甩锅给终端（v0.3.1 修复）。"""
    from myrtui.client.mdpreview import MarkdownPreviewScreen

    monkeypatch.delenv(graphics.ENV_NAME, raising=False)
    screen = MarkdownPreviewScreen(
        "/tmp/doc.md", graphics=graphics.Capabilities()
    )
    notices: list[str] = []
    screen._notice = lambda message, seconds=4.0: notices.append(message)  # type: ignore[assignment]
    screen._text = "x"
    screen.action_paged()
    assert notices and "位图" in notices[0]
    assert "textual-image" in notices[0] and graphics.INSTALL_HINT in notices[0]


def test_preview_action_paged_reports_no_terminal_protocol(monkeypatch):
    from myrtui.client.mdpreview import MarkdownPreviewScreen

    monkeypatch.delenv(graphics.ENV_NAME, raising=False)
    screen = MarkdownPreviewScreen(
        "/tmp/doc.md",
        graphics=graphics.Capabilities(available=True, mode="unicode"),
    )
    notices: list[str] = []
    screen._notice = lambda message, seconds=4.0: notices.append(message)  # type: ignore[assignment]
    screen._text = "x"
    screen.action_paged()
    assert notices and "终端" in notices[0] and "textual-image" not in notices[0]


def test_preview_action_paged_pushes_screen(monkeypatch, tmp_path):
    """预览里按 ``v`` → 真的推出 LaTeX 排版屏（端到端，不 mock 组件树）。"""
    from textual.app import App

    from myrtui.client.mdpreview import MarkdownPreviewScreen

    doc = tmp_path / "doc.md"
    doc.write_text("# 标题\n\n正文，没有公式。\n", encoding="utf-8")
    spec = _spec(cols=110, rows=44)
    page = _fake_png(tmp_path / "p1.png", size=spec.size)
    monkeypatch.setattr(docpage, "cached_pages", lambda *a, **kw: [page])

    class Demo(App):
        def on_mount(self) -> None:
            self.push_screen(
                MarkdownPreviewScreen(str(doc), graphics=_caps())
            )

    async def scenario():
        app = Demo()
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.pause(0.8)
            await pilot.press("v")
            await pilot.pause(0.8)
            assert isinstance(app.screen, PagedDocScreen)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 真机冒烟
# ---------------------------------------------------------------------------


@pytest.mark.skipif(katex.find_chrome() is None, reason="本机没有 Chrome")
@pytest.mark.skipif(
    mathimg.find_converter() is None, reason="本机没有 pdftocairo"
)
def test_real_pages_have_exact_pixel_size():
    pytest.importorskip("PIL.Image")
    from PIL import Image

    spec = _spec(cols=60, rows=20)
    pages = docpage.render_pages(
        "# 真机\n\n行内 $x^2$\n\n$$\n\\frac{a}{b}\n$$\n",
        source="/tmp/real.md",
        colors=("#E0E0E0", "#1E1E1E"),
        spec=spec,
    )
    assert pages
    for page in pages:
        assert Image.open(page).size == spec.size
