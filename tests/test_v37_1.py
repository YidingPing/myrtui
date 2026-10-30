"""myrtui v0.37.1 测试：KaTeX 位图后端（质量优先）。

覆盖：

- ``katex`` 模块：Chrome 探测、资产、HTML（离线 / 一页一公式 / 转义 / 颜色）；
- 后端选择：``auto → katex → latex → 无``、显式指定、``off``；
- KaTeX 渲染管线（fake printer / runner）：批量、缓存、失败、页数不符、
  页面装不下时的放大重试；
- 尺寸口径：1em = cell 高 / 1.2、2× 超采样；
- 真机冒烟（有 Chrome 才跑）。

CI 不装 Chrome / Pillow / TeX：除冒烟用例外全部 fake。
"""
from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import myrtui.client.katex as katex
import myrtui.client.mathimg as mathimg
import myrtui.client.mathrender as mathrender
from myrtui.client import graphics
from myrtui.client.mdpreview import MathBlock


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    mathrender.reset_state()
    mathimg.reset_state()
    monkeypatch.setattr(mathrender, "probe_termtex", lambda: None)
    mathimg.set_cache_root(tmp_path / "cache")
    yield
    mathrender.reset_state()
    mathimg.reset_state()
    mathimg.set_cache_root(None)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _fake_png(path: Path, size=(40, 20), color=(200, 200, 200, 255)) -> Path:
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
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return path


def _backend(name="katex", chrome=None, tools=None):
    if name == "katex":
        return mathimg.Backend(
            "katex",
            chrome=chrome or katex.Chrome("/fake/chrome"),
            converter="/fake/pdftocairo",
            converter_kind="pdftocairo",
        )
    return mathimg.Backend(
        "latex",
        tools=tools,
        converter="/fake/pdftocairo",
        converter_kind="pdftocairo",
    )


def _fake_printer(calls, pdf_pages=1):
    def printer(chrome, html, pdf, *, profile_dir, timeout):
        calls.append(("print", Path(html).read_text(encoding="utf-8")))
        Path(pdf).write_bytes(b"%PDF-1.4 fake")

    return printer


def _fake_runner(pages=1, clipped_first=False):
    state = {"calls": 0}

    def runner(command, **kwargs):
        state["calls"] += 1
        if "pdftocairo" in Path(command[0]).name or "pdftoppm" in Path(command[0]).name:
            prefix = Path(command[-1])
            clipped = clipped_first and state["calls"] == 1
            for index in range(1, pages + 1):
                png = Path(f"{prefix}-{index}.png")
                if clipped:
                    # 顶到页面右缘（2000 CSS px × 2）→ 触发「装不下」重试
                    _fake_png(png, size=(4000, 40), color=(255, 255, 255, 255))
                else:
                    _fake_png(png, size=(200, 60))
        return SimpleNamespace(returncode=0, stderr=b"", stdout=b"")

    runner.state = state  # type: ignore[attr-defined]
    return runner


# ---------------------------------------------------------------------------
# katex 模块
# ---------------------------------------------------------------------------


def test_katex_assets_and_version():
    assert katex.assets_available()
    assert katex.katex_version().startswith("katex")


def test_katex_find_chrome_env_override(tmp_path):
    fake = tmp_path / "chrome"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    assert katex.find_chrome({"MYRTUI_CHROME": str(fake)}) == katex.Chrome(str(fake))
    # 不存在的强制路径 → 忽略并回落（不抛）
    katex.find_chrome({"MYRTUI_CHROME": str(tmp_path / "nope")})


def test_katex_find_chrome_prefers_candidate(monkeypatch):
    monkeypatch.setattr(katex.os.path, "isfile", lambda path: True)
    monkeypatch.setattr(katex.os, "access", lambda path, mode: True)
    chrome = katex.find_chrome({})
    assert chrome is not None and chrome.path in katex.CHROME_CANDIDATES


def test_katex_build_html_is_offline_and_one_page_per_formula():
    html = katex.build_html([r"a^2", r"\frac{1}{2}"], color="#E0E0E0")
    assert "http://" not in html and "https://" not in html
    assert html.count('class="fx"') == 2
    assert "file://" in html
    assert "@page" in html and "page-break-after: always" in html
    assert "color: #E0E0E0" in html


def test_katex_build_html_escapes_script_breakouts():
    evil = r"x</script><script>alert(1)</script>"
    html = katex.build_html([evil], color="#E0E0E0")
    assert "</script><script>alert" not in html
    assert "\\u003c/script" in html


def test_katex_build_html_normalizes_font_to_body_size():
    html = katex.build_html(["x"], color="#E0E0E0")
    # KaTeX 默认 1.21em（网页放大），终端里要归一到 1em
    assert ".katex { font-size: 1em; }" in html


def test_katex_cache_key_includes_style_version():
    key = mathimg.cache_key(
        "x", color="#E0E0E0", background="#1E1E1E",
        dpi=200, kind="katex", cell=(10, 20),
    )
    # 样式版本直接参与哈希输入
    import hashlib
    payload = "\x1f".join([
        "x", "E0E0E0", "1E1E1E", "200", "katex", "10x20",
        f"{mathimg.terminal_em_px((10, 20)):.3f}",
        f"{katex.katex_version()}/{katex.STYLE_VERSION}",
    ])
    assert key == hashlib.sha1(payload.encode()).hexdigest()


def test_katex_build_html_fails_bad_formula_instead_of_red_text():
    html = katex.build_html(["x"], color="#E0E0E0")
    assert "throwOnError: true" in html
    assert "data-katex-error" in html
    assert "throwOnError: false" not in html


def test_katex_build_html_rejects_bad_color():
    html = katex.build_html(["x"], color="red;}</style><script>")
    assert "#E0E0E0" in html
    assert "<script>" not in html.split("<body>")[0]


def test_katex_raster_dpi_tracks_em():
    assert katex.raster_dpi(16.7) < katex.raster_dpi(33.3)
    assert katex.raster_dpi(0) >= 96


# ---------------------------------------------------------------------------
# 后端选择
# ---------------------------------------------------------------------------


def _enable_katex(monkeypatch):
    monkeypatch.setattr(katex, "find_chrome", lambda env=None: katex.Chrome("/fake/chrome"))
    monkeypatch.setattr(katex, "assets_available", lambda: True)
    monkeypatch.setattr(
        mathimg, "find_converter", lambda env=None: ("/fake/pdftocairo", "pdftocairo")
    )


def test_select_backend_prefers_latex_when_both_available(monkeypatch):
    _enable_katex(monkeypatch)
    monkeypatch.setattr(
        mathimg,
        "find_tools",
        lambda env=None: (
            mathimg.Tools("/fake/pdflatex", "/fake/pdftocairo", "pdftocairo"),
            None,
        ),
    )
    backend, warning = mathimg.select_backend({})
    assert warning is None
    assert backend is not None and backend.name == "latex"


def test_select_backend_uses_katex_without_tex(monkeypatch):
    _enable_katex(monkeypatch)
    monkeypatch.setattr(mathimg, "find_tools", lambda env=None: (None, "no tex"))
    backend, _ = mathimg.select_backend({})
    assert backend is not None and backend.name == "katex"


def test_select_backend_falls_back_to_latex(monkeypatch):
    monkeypatch.setattr(katex, "find_chrome", lambda env=None: None)
    monkeypatch.setattr(
        mathimg,
        "find_tools",
        lambda env=None: (
            mathimg.Tools("/fake/pdflatex", "/fake/pdftocairo", "pdftocairo"),
            None,
        ),
    )
    backend, _ = mathimg.select_backend({})
    assert backend is not None and backend.name == "latex"


def test_select_backend_none_when_nothing_available(monkeypatch):
    monkeypatch.setattr(katex, "find_chrome", lambda env=None: None)
    monkeypatch.setattr(mathimg, "find_tools", lambda env=None: (None, "no tex"))
    backend, warning = mathimg.select_backend({})
    assert backend is None and warning is None


def test_select_backend_explicit_katex_missing_chrome(monkeypatch):
    monkeypatch.setattr(katex, "find_chrome", lambda env=None: None)
    backend, warning = mathimg.select_backend(
        {"MYRTUI_MATH_IMG_BACKEND": "katex"}
    )
    assert backend is None
    assert warning and "Chrome" in warning


def test_select_backend_off(monkeypatch):
    _enable_katex(monkeypatch)
    backend, _ = mathimg.select_backend({"MYRTUI_MATH_IMG": "off"})
    assert backend is None


def test_select_backend_invalid_falls_back_to_auto(monkeypatch):
    _enable_katex(monkeypatch)
    monkeypatch.setattr(mathimg, "find_tools", lambda env=None: (None, "no tex"))
    backend, warning = mathimg.select_backend({"MYRTUI_MATH_IMG_BACKEND": "bogus"})
    assert backend is not None and backend.name == "katex"
    assert warning and "非法" in warning


# ---------------------------------------------------------------------------
# 渲染管线（fake printer / runner）
# ---------------------------------------------------------------------------


def test_render_katex_batch_scales_to_body_size():
    pytest.importorskip("PIL.Image")
    from PIL import Image

    calls: list = []
    result = mathimg.render_formulas(
        [r"a^2", r"\frac{1}{2}"],
        color="#E0E0E0",
        background="#1E1E1E",
        cell=(10, 20),
        backend=_backend(),
        printer=_fake_printer(calls),
        runner=_fake_runner(pages=2),
    )
    assert set(result) == {0, 1}
    assert len(calls) == 1  # 一次 Chrome 出两页
    em = mathimg.terminal_em_px((10, 20))
    # 2× 超采样（200x60）→ 缩回 1x（100x30）
    with Image.open(result[0]) as image:
        assert image.mode == "RGB"
        assert abs(image.width - 100) <= 1
        assert abs(image.height - 30) <= 1
    assert em > 0


def test_render_katex_cache_hit_skips_chrome():
    pytest.importorskip("PIL.Image")
    calls: list = []
    first = mathimg.render_formulas(
        ["a^2"],
        color="#E0E0E0",
        background="#1E1E1E",
        backend=_backend(),
        printer=_fake_printer(calls),
        runner=_fake_runner(),
    )
    assert first and len(calls) == 1
    second = mathimg.render_formulas(
        ["a^2"],
        color="#E0E0E0",
        background="#1E1E1E",
        backend=_backend(),
        printer=_fake_printer(calls),
        runner=_fake_runner(),
    )
    assert second and len(calls) == 1  # 没再启动 Chrome


def test_render_katex_failure_marks_and_does_not_retry():
    pytest.importorskip("PIL.Image")

    def boom(*args, **kwargs):
        raise RuntimeError("chrome boom")

    result = mathimg.render_formulas(
        ["a^2"],
        color="#E0E0E0",
        background="#1E1E1E",
        backend=_backend(),
        printer=boom,
        runner=_fake_runner(),
    )
    assert result == {}
    calls: list = []
    again = mathimg.render_formulas(
        ["a^2"],
        color="#E0E0E0",
        background="#1E1E1E",
        backend=_backend(),
        printer=_fake_printer(calls),
        runner=_fake_runner(),
    )
    assert again == {} and calls == []


def test_render_katex_page_mismatch_retries_individually():
    pytest.importorskip("PIL.Image")
    calls: list = []
    # 批量只出 1 页（不符）→ 逐条重试时应改为各出 1 页
    result = mathimg.render_formulas(
        ["a^2", "b^2"],
        color="#E0E0E0",
        background="#1E1E1E",
        backend=_backend(),
        printer=_fake_printer(calls),
        runner=_fake_runner(pages=1),
    )
    assert set(result) == {0, 1}
    assert len(calls) == 3  # 1 次批量 + 2 次逐条


def test_render_katex_clipped_page_widens_and_retries():
    pytest.importorskip("PIL.Image")
    calls: list = []
    runner = _fake_runner(pages=1, clipped_first=True)
    result = mathimg.render_formulas(
        ["a^2"],
        color="#E0E0E0",
        background="#1E1E1E",
        backend=_backend(),
        printer=_fake_printer(calls),
        runner=runner,
    )
    assert set(result) == {0}
    assert len(calls) == 2  # 第一次被判裁切 → 放大页重试


def test_render_katex_uses_black_on_pdftoppm():
    pytest.importorskip("PIL.Image")
    calls: list = []
    backend = mathimg.Backend(
        "katex",
        chrome=katex.Chrome("/fake/chrome"),
        converter="/fake/pdftoppm",
        converter_kind="pdftoppm",
    )
    mathimg.render_formulas(
        ["a^2"],
        color="#E0E0E0",
        background="#1E1E1E",
        backend=backend,
        printer=_fake_printer(calls),
        runner=_fake_runner(),
    )
    assert calls and "color: #000000" in calls[0][1]


def test_image_cached_katex_key_includes_cell_and_backend():
    path = mathimg._target_path(
        mathimg.cache_key(
            "a^2",
            color="#E0E0E0",
            background="#1E1E1E",
            dpi=katex.raster_dpi(mathimg.terminal_em_px((10, 20))),
            kind="katex",
            cell=(10, 20),
        ),
        None,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    _fake_png(path)
    assert (
        mathimg.image_cached(
            "a^2", color="#E0E0E0", background="#1E1E1E", kind="katex", cell=(10, 20)
        )
        == path
    )
    # 不同 cell 尺寸 → 不同缓存键 → 未命中
    assert (
        mathimg.image_cached(
            "a^2", color="#E0E0E0", background="#1E1E1E", kind="katex", cell=(8, 16)
        )
        is None
    )


# ---------------------------------------------------------------------------
# LaTeX 后端：CJK（中文标签 / 全角标点）
# ---------------------------------------------------------------------------


def test_has_cjk_detects_chinese_and_fullwidth():
    assert mathimg.has_cjk(r"\text{几何光行时}")
    assert mathimg.has_cjk(r"\text{Shapiro（§3.4）}")  # 全角括号
    assert mathimg.has_cjk(r"\text{单位：ns}")
    assert not mathimg.has_cjk(r"a^2+b^2=c^2")
    assert not mathimg.has_cjk(r"\text{see §3.4}")


def test_build_tex_wraps_only_cjk_pages():
    tex = mathimg.build_tex([r"a^2", r"\text{几何光行时}"], "#E0E0E0")
    assert tex.count(r"\usepackage{CJKutf8}") == 1
    assert tex.count(r"\begin{CJK*}{UTF8}{gbsn}") == 1
    assert tex.count(r"\begin{mathpage}") == 2
    # 纯数学页不包 CJK（否则会白白多一层分组）
    assert r"\begin{mathpage}$\displaystyle a^2$\end{mathpage}" in tex
    assert (
        r"\begin{mathpage}\begin{CJK*}{UTF8}{gbsn}$\displaystyle \text{几何光行时}$"
        r"\end{CJK*}\end{mathpage}"
    ) in tex


def test_build_tex_skips_cjk_package_for_pure_math():
    tex = mathimg.build_tex([r"a^2", r"\frac{1}{2}"], "#E0E0E0")
    assert "CJK" not in tex


def test_latex_unicode_error_maps_to_cjk_reason():
    """缺 CJK 字体时，诊断原因要能一眼看出是中文问题（不是宽度策略）。"""

    def failing_runner(command, **kwargs):
        return SimpleNamespace(
            returncode=1,
            stderr=(
                "! LaTeX Error: Unicode character 几 (U+51E0)\n"
                "               not set up for use with LaTeX."
            ).encode(),
            stdout=b"",
        )

    tools = mathimg.Tools("/fake/pdflatex", "/fake/pdftocairo", "pdftocairo")
    result = mathimg.render_formulas(
        [r"\text{几何}"],
        color="#E0E0E0",
        background="#1E1E1E",
        backend=_backend("latex", tools=tools),
        runner=failing_runner,
    )
    assert result == {}
    failures = mathimg.last_failures()
    assert failures and "CJK" in failures[0][1]


def test_latex_cache_key_includes_style_version():
    import hashlib

    key = mathimg.cache_key(
        "x", color="#E0E0E0", background="#1E1E1E", dpi=300, kind="latex"
    )
    payload = "\x1f".join(
        [
            "x",
            "E0E0E0",
            "1E1E1E",
            "300",
            "latex",
            "10x20",
            f"{mathimg.terminal_em_px((10, 20)):.3f}",
            f"latex/{mathimg.LATEX_STYLE_VERSION}",
        ]
    )
    assert key == hashlib.sha1(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# 真机冒烟
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    mathimg.find_tools()[0] is None, reason="本机没有 pdflatex / 转换器"
)
@pytest.mark.real_tools
def test_latex_real_cjk_formula_renders():
    """真机：中文标签的公式走 LaTeX 后端必须能出图（不再回落 termtex）。"""
    pytest.importorskip("PIL.Image")
    tools, _ = mathimg.find_tools()
    assert tools is not None
    backend = mathimg.Backend(
        "latex", tools=tools, converter=tools.converter, converter_kind=tools.kind
    )
    result = mathimg.render_formulas(
        [r"f=\underbrace{\frac{a}{c}}_{\text{几何光行时}}-\Delta t_S \tag{7}"],
        color="#E0E0E0",
        background="#1E1E1E",
        cell=(10, 20),
        backend=backend,
    )
    assert mathimg.last_failures() == []
    assert set(result) == {0}
    size = mathimg.png_size(result[0])
    assert size is not None and size[0] > 20 and size[1] > 10


@pytest.mark.skipif(katex.find_chrome() is None, reason="本机没有 Chrome")
@pytest.mark.skipif(not katex.assets_available(), reason="KaTeX 资产缺失")
@pytest.mark.real_tools
def test_katex_real_pipeline(tmp_path):
    pytest.importorskip("PIL.Image")
    from PIL import Image

    converter = mathimg.find_converter()
    if converter is None:
        pytest.skip("没有 pdftocairo / pdftoppm")
    backend, warning = mathimg.find_katex({})
    assert backend is not None, warning
    result = mathimg.render_formulas(
        [r"\frac{a+b}{c}"],
        color="#E0E0E0",
        background="#1E1E1E",
        cell=(10, 20),
        backend=backend,
    )
    assert set(result) == {0}
    with Image.open(result[0]) as image:
        assert image.mode == "RGB"
        assert image.width > 8 and image.height > 8

def test_render_katex_empty_page_records_support_reason():
    """KaTeX 不支持的公式 → 空页 → 记录「不支持」并回落（不是尺寸策略）。"""
    pytest.importorskip("PIL.Image")

    def transparent_runner(command, **kwargs):
        prefix = Path(command[-1])
        _fake_png(Path(f"{prefix}-1.png"), size=(20, 20), color=(0, 0, 0, 0))
        return SimpleNamespace(returncode=0, stderr=b"", stdout=b"")

    result = mathimg.render_formulas(
        [r"\notacommand{x}"],
        color="#E0E0E0",
        background="#1E1E1E",
        backend=_backend(),
        printer=_fake_printer([]),
        runner=transparent_runner,
    )
    assert result == {}
    failures = mathimg.last_failures()
    assert failures and "KaTeX 不支持" in failures[0][1]


def test_prewarm_writes_debug_failure_log(monkeypatch):
    def fake_render(*args, **kwargs):
        mathimg._record_failure("bad-formula", "KaTeX 不支持该公式（渲染为空页）")
        return {}

    monkeypatch.setattr(mathimg, "render_formulas", fake_render)
    mathimg.prewarm_document(
        "$$\nbad-formula\n$$",
        color="#E0E0E0",
        background="#1E1E1E",
        backend=_backend(),
        env={"MYRTUI_MATH_IMG_DEBUG": "1"},
    )
    log_path = mathimg.cache_root() / "failures.log"
    assert log_path.is_file()
    text = log_path.read_text(encoding="utf-8")
    assert "bad-formula" in text and "KaTeX 不支持" in text


def test_debug_log_not_written_without_env(monkeypatch):
    def fake_render(*args, **kwargs):
        mathimg._record_failure("bad-formula", "x")
        return {}

    monkeypatch.setattr(mathimg, "render_formulas", fake_render)
    mathimg.prewarm_document(
        "$$\nbad-formula\n$$",
        color="#E0E0E0",
        background="#1E1E1E",
        backend=_backend(),
        env={},
    )
    assert not (mathimg.cache_root() / "failures.log").exists()
