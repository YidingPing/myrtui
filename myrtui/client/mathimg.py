"""块级公式 → PNG（LaTeX 优先，KaTeX 兜底；纯逻辑，不 import Textual）。

两条后端共用同一套「缓存 / 逐条回落 / 预合成 / 缩放」骨架：

- ``latex``（默认）：``standalone[multi=mathpage]`` 批量编译 → ``pdftocairo`` /
  ``pdftoppm``；含中文 / 全角标点的页自动包 ``CJK*``（``gbsn``），只有真用到
  才加载 ``CJKutf8``；
- ``katex``（兜底）：KaTeX → Chrome headless 打印 PDF（一公式一页）→
  ``pdftocairo -png -transp``（PDF 无白底，直接得透明 alpha）；没有 TeX（或
  ``MYRTUI_MATH_IMG_BACKEND=katex``）时启用。

两条路径的产物都会：

1. 按 alpha 去透明边；
2. **预合成到主题背景色**（Tabby 的 Sixel P2=1 语义不可靠，见
   ``docs-internal/docs/V37-SIXEL-GRAPHICS-PLAN.md`` §3.4 F4）；
3. 缩放到「与正文同字号」（1em = ``cell_height / 1.2`` 终端像素）；
4. 原子写入 ``<cache>/myrtui/mathimg/<sha1>.png``。

**B1**：只有 :func:`prewarm_document` / :func:`render_formulas` 会 spawn，
调用方必须放进 worker；事件循环上的 :func:`image_cached` 只做 ``stat``。
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Sequence

import platformdirs

from myrtui.client import katex, mathrender
from myrtui.i18n import t

log = logging.getLogger("myrtui.mathimg")

ENV_PDFLATEX = "MYRTUI_PDFLATEX"
ENV_CONVERTER = "MYRTUI_PDFTOCAIRO"
ENV_MATH_IMG = "MYRTUI_MATH_IMG"
ENV_BACKEND = "MYRTUI_MATH_IMG_BACKEND"
ENV_DEBUG = "MYRTUI_MATH_IMG_DEBUG"
ENV_SCALE = "MYRTUI_MATH_IMG_SCALE"

DPI = 300
TIMEOUT_SECONDS = 10.0
MAX_FORMULAS = 200
"""单文档最多尝试多少条公式位图；超出的走 termtex（避免一次编译过久）。"""

BASE_POINT_SIZE = 10.0
"""LaTeX ``standalone`` 默认字号（10pt）。"""

TERMINAL_EM_RATIO = 1.2
"""终端单元格高 ≈ 1.2em（行高惯例）。"""

DEFAULT_SCALE = 1.2
"""公式 em 相对终端正文 em 的放大系数。

Computer Modern 的 x-height ≈ 0.43em，而终端等宽字体 ≈ 0.52em：同样 1em 时
数学里的小写字母会**明显偏小**（真机反馈「字太小」）。按 x-height 补偿
0.52/0.43 ≈ 1.21，取 1.2（可用 ``MYRTUI_MATH_IMG_SCALE`` 覆盖）。
"""

MIN_SCALE = 0.5
MAX_SCALE = 2.5

MAX_FILE_BYTES = 4 * 1024 * 1024

LATEX_STYLE_VERSION = 2
"""影响 LaTeX 产物像素的模板版本（进缓存键；改 preamble / 字体时要 +1）。"""

CJK_PATTERN = re.compile(
    "["
    "\u2e80-\u2eff"  # CJK 部首补充
    "\u3000-\u303f"  # CJK 标点（、。「」…）
    "\u3040-\u30ff"  # 假名
    "\u3400-\u4dbf"  # 扩展 A
    "\u4e00-\u9fff"  # 基本区
    "\uf900-\ufaff"  # 兼容表意
    "\ufe30-\ufe4f"  # 兼容标点（：，！）
    "\uff00-\uffef"  # 全角形式（（）：，）
    "]"
)
"""公式里出现这些字符 → 需要 CJK 字体（pdflatex 默认无法排版）。"""

CJK_FONT = "gbsn"
"""CJKutf8 字体族：``gbsn``（Arphic 宋体，TeX Live 自带，无需系统字体）。"""
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_LABEL = re.compile(r"\\label\s*\{[^{}]*\}")

_RUNNER = subprocess.run
"""测试可替换（fake pdflatex / pdftocairo）。"""

_FAILED: set[str] = set()
_FAILED_LOCK = threading.Lock()

_LAST_FAILURES: list[tuple[str, str]] = []
"""最近一次渲染里回落的公式与原因（诊断用；``reset_state`` 清空）。"""

_CACHE_ROOT_OVERRIDE: Path | None = None
"""测试钩子：固定缓存目录（生产为 ``None``，走 platformdirs）。"""


@dataclass(frozen=True)
class Tools:
    """一组可用的 LaTeX / poppler 可执行文件。"""

    pdflatex: str
    converter: str
    kind: str
    """``pdftocairo``（保留透明）或 ``pdftoppm``（白底，需要转 alpha）。"""


@dataclass(frozen=True)
class Backend:
    """选定的公式后端。"""

    name: str
    """``katex`` 或 ``latex``。"""
    chrome: katex.Chrome | None = None
    tools: Tools | None = None
    converter: str = ""
    converter_kind: str = "pdftocairo"

    @property
    def available(self) -> bool:
        return self.tools is not None or self.chrome is not None


def cache_root() -> Path:
    if _CACHE_ROOT_OVERRIDE is not None:
        return _CACHE_ROOT_OVERRIDE
    return Path(platformdirs.user_cache_dir("myrtui", appauthor=False)) / "mathimg"


def set_cache_root(path: str | os.PathLike[str] | None) -> None:
    """测试钩子：把缓存目录固定到临时路径（``None`` 恢复默认）。"""
    global _CACHE_ROOT_OVERRIDE
    _CACHE_ROOT_OVERRIDE = Path(path) if path is not None else None


def reset_state() -> None:
    """测试钩子：清失败标记与诊断记录。"""
    with _FAILED_LOCK:
        _FAILED.clear()
        _LAST_FAILURES.clear()


def last_failures() -> list[tuple[str, str]]:
    """最近一次渲染里「回落 termtex」的公式 → ``[(source, reason), …]``。"""
    with _FAILED_LOCK:
        return list(_LAST_FAILURES)


def _record_failure(source: str, reason: str) -> None:
    with _FAILED_LOCK:
        if (source, reason) in _LAST_FAILURES:  # 批量失败 + 逐条重试会重复
            return
        _LAST_FAILURES.append((source, reason))
        if len(_LAST_FAILURES) > 200:
            del _LAST_FAILURES[:-200]


class EmptyProduct(RuntimeError):
    """产物为空。

    单独成类是因为「KaTeX 渲染出了空页」这条判定必须在**任何语言**下成立：
    原来靠 ``"为空" in str(exc)`` 匹配中文文案，i18n 之后 en 下永远匹配不上。
    """


def _reason_for(backend_name: str, exc: Exception) -> str:
    text = str(exc)
    if backend_name == "katex" and isinstance(exc, EmptyProduct):
        return t("math.katex_empty")
    if "Unicode character" in text:
        return t("math.cjk_missing")
    return text[:200] or exc.__class__.__name__


def math_img_scale(env: dict[str, str] | None = None) -> float:
    """``MYRTUI_MATH_IMG_SCALE``（公式字号倍率，默认 :data:`DEFAULT_SCALE`）。"""
    environ = os.environ if env is None else env
    raw = (environ.get(ENV_SCALE) or "").strip()
    if not raw:
        return DEFAULT_SCALE
    try:
        value = float(raw)
    except ValueError:
        log.warning("%s=%r 不是数字，按默认 %s 处理", ENV_SCALE, raw, DEFAULT_SCALE)
        return DEFAULT_SCALE
    if not (MIN_SCALE <= value <= MAX_SCALE):
        log.warning(
            "%s=%r 超出范围 [%s, %s]，按默认 %s 处理",
            ENV_SCALE,
            raw,
            MIN_SCALE,
            MAX_SCALE,
            DEFAULT_SCALE,
        )
        return DEFAULT_SCALE
    return value


def terminal_em_px(
    cell: tuple[int, int] = (10, 20),
    scale: float | None = None,
) -> float:
    """终端正文字号的像素 em（再乘 ``scale`` 得到公式 em）。"""
    height = max(1, int(cell[1] if len(cell) > 1 else 20))
    factor = math_img_scale() if scale is None else float(scale)
    return height / TERMINAL_EM_RATIO * factor


# ---------------------------------------------------------------------------
# 工具链探测
# ---------------------------------------------------------------------------


def find_converter(env: dict[str, str] | None = None) -> tuple[str, str] | None:
    """PDF→PNG 转换器 → ``(path, kind)``；``kind`` ∈ pdftocairo/pdftoppm。"""
    environ = os.environ if env is None else env
    forced = (environ.get(ENV_CONVERTER) or "").strip()
    if forced:
        if not os.path.exists(forced):
            return None
        kind = "pdftocairo" if "pdftocairo" in os.path.basename(forced) else "pdftoppm"
        return forced, kind
    for name, kind in (("pdftocairo", "pdftocairo"), ("pdftoppm", "pdftoppm")):
        found = shutil.which(name)
        if found:
            return found, kind
    return None


def find_tools(env: dict[str, str] | None = None) -> tuple[Tools | None, str | None]:
    """定位 ``pdflatex`` 与 PDF→PNG 转换器 → ``(tools, warning)``。"""
    environ = os.environ if env is None else env
    forced_latex = (environ.get(ENV_PDFLATEX) or "").strip()
    if forced_latex:
        if not os.path.exists(forced_latex):
            return None, t("math.pdflatex_env_missing", env=ENV_PDFLATEX, path=forced_latex)
        pdflatex = forced_latex
    else:
        pdflatex = shutil.which("pdflatex")
    if not pdflatex:
        return None, t("math.no_pdflatex", env=ENV_PDFLATEX)

    converter = find_converter(environ)
    if converter is None:
        return None, t("math.no_converter", env=ENV_CONVERTER)
    path, kind = converter
    return Tools(pdflatex, path, kind), None


def find_katex(
    env: dict[str, str] | None = None,
) -> tuple[Backend | None, str | None]:
    """KaTeX 后端：Chrome + 资产 + 转换器 都齐才可用。"""
    chrome = katex.find_chrome(env)
    if chrome is None:
        return None, t("math.no_chrome", env=katex.ENV_CHROME)
    if not katex.assets_available():
        return None, t("math.no_katex_assets")
    converter = find_converter(env)
    if converter is None:
        return None, t("math.no_converter", env=ENV_CONVERTER)
    return (
        Backend(
            "katex",
            chrome=chrome,
            converter=converter[0],
            converter_kind=converter[1],
        ),
        None,
    )


def math_img_enabled(env: dict[str, str] | None = None) -> bool:
    """``MYRTUI_MATH_IMG``：``auto``（默认）/ ``off``。"""
    environ = os.environ if env is None else env
    return (environ.get(ENV_MATH_IMG) or "").strip().lower() != "off"


def select_backend(
    env: dict[str, str] | None = None,
) -> tuple[Backend | None, str | None]:
    """解析 ``MYRTUI_MATH_IMG_BACKEND``（``auto`` / ``katex`` / ``latex``）。

    ``auto``（默认）优先 LaTeX（pdflatex），没有 TeX 时回落 KaTeX。
    """
    environ = os.environ if env is None else env
    if not math_img_enabled(environ):
        return None, None
    raw = (environ.get(ENV_BACKEND) or "").strip().lower()
    warning: str | None = None
    if raw not in ("", "auto", "katex", "latex"):
        warning = t("math.backend_invalid", env=ENV_BACKEND, raw=raw)
        log.warning(warning)
        raw = "auto"

    def _latex() -> tuple[Backend | None, str | None]:
        tools, warning = find_tools(environ)
        if tools is None:
            return None, warning
        return (
            Backend(
                "latex",
                tools=tools,
                converter=tools.converter,
                converter_kind=tools.kind,
            ),
            None,
        )

    if raw == "katex":
        return find_katex(environ)
    if raw == "latex":
        return _latex()

    # auto：**LaTeX 优先**（装了 TeX 的机器：更快 0.5s vs 2.3s、命令覆盖更全，
    # 2026-10-28 真机对比观感一致）；没有 TeX 时才用 KaTeX。
    backend, _why = _latex()
    if backend is not None:
        return backend, warning
    backend, _why = find_katex(environ)
    if backend is not None:
        return backend, warning
    return None, warning


# ---------------------------------------------------------------------------
# 公式归一化 / 缓存键
# ---------------------------------------------------------------------------


def normalize_formula(source: str) -> tuple[str, str | None]:
    """公式源码归一化 → ``(tex, tag)``。

    ``\\tag{...}`` 改成末尾 ``\\qquad (tag)``（KaTeX 的 display ``\\tag`` 会把
    标签拉到页宽右缘，图会变成一大片空白）；``\\label{...}`` 删除。
    """
    body, tag = mathrender.split_tag(source)
    body = _LABEL.sub(" ", body)
    body = " ".join(body.split())
    if tag:
        body = f"{body}\\qquad ({tag})"
    return body.strip(), tag


def _html_color(color: str) -> str:
    value = (color or "").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(char * 2 for char in value)
    if len(value) != 6:
        return "E0E0E0"
    try:
        int(value, 16)
    except ValueError:
        return "E0E0E0"
    return value.upper()


def _rgb(color: str) -> tuple[int, int, int]:
    value = _html_color(color)
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def cache_key(
    source: str,
    *,
    color: str,
    background: str,
    dpi: int,
    kind: str,
    cell: tuple[int, int] = (10, 20),
    em: float | None = None,
) -> str:
    em_px = terminal_em_px(cell) if em is None else float(em)
    payload = "\x1f".join(
        [
            source,
            _html_color(color),
            _html_color(background),
            str(int(dpi)),
            kind,
            f"{int(cell[0])}x{int(cell[1])}",
            f"{em_px:.3f}",
            (
                f"{katex.katex_version()}/{katex.STYLE_VERSION}"
                if kind == "katex"
                else f"latex/{LATEX_STYLE_VERSION}"
            ),
        ]
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()  # noqa: S324 —— 缓存键


def _cache_path(key: str) -> Path:
    return cache_root() / f"{key}.png"


def _target_path(key: str, override: Path | None) -> Path:
    return Path(override) / f"{key}.png" if override is not None else _cache_path(key)


def image_cached(
    source: str,
    *,
    color: str,
    background: str,
    kind: str = "katex",
    dpi: int | None = None,
    cell: tuple[int, int] = (10, 20),
    cache_root_override: Path | None = None,
    scale: float | None = None,
    env: dict[str, str] | None = None,
) -> Path | None:
    """**只查磁盘**（事件循环安全）：命中返回 PNG 路径，否则 ``None``。"""
    body, _tag = normalize_formula(source)
    if not body:
        return None
    em_px = terminal_em_px(cell, scale if scale is not None else math_img_scale(env))
    if dpi is None:
        dpi = katex.raster_dpi(em_px) if kind == "katex" else DPI
    key = cache_key(
        body,
        color=color,
        background=background,
        dpi=dpi,
        kind=kind,
        cell=cell,
        em=em_px,
    )
    path = _target_path(key, cache_root_override)
    try:
        if path.is_file() and path.stat().st_size > len(PNG_MAGIC):
            return path
    except OSError:
        return None
    return None


def png_size(path: str | os.PathLike[str]) -> tuple[int, int] | None:
    """读 PNG IHDR 拿像素尺寸（只读 24 字节，事件循环上安全）。"""
    try:
        with open(path, "rb") as handle:
            head = handle.read(24)
    except OSError:
        return None
    if len(head) < 24 or not head.startswith(PNG_MAGIC) or head[12:16] != b"IHDR":
        return None
    width = int.from_bytes(head[16:20], "big")
    height = int.from_bytes(head[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


# ---------------------------------------------------------------------------
# 子进程执行
# ---------------------------------------------------------------------------


def _run(
    command: list[str],
    *,
    cwd: str,
    timeout: float,
    env: dict[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> None:
    completed = (runner or _RUNNER)(  # noqa: S603 —— 固定可执行
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        env=env,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(
            t(
                "math.exit_code",
                tool=Path(command[0]).name,
                code=completed.returncode,
                detail=detail[:300],
            )
        )


def _tex_env() -> dict[str, str]:
    env = os.environ.copy()
    env["openin_any"] = "p"
    env["openout_any"] = "p"
    return env


def _compile(tools: Tools, workdir: Path, tex: str, *, timeout: float, runner) -> Path:
    tex_path = workdir / "math.tex"
    tex_path.write_text(tex, encoding="utf-8")
    # ``openin_any=p`` / ``openout_any=p`` 只允许相对路径 → 一律用 cwd 内相对
    # 名字（绝对路径会被 TeX 直接拒绝：openin_any = p）。
    _run(
        [
            tools.pdflatex,
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-no-shell-escape",
            "-file-line-error",
            "-output-directory",
            ".",
            "math.tex",
        ],
        cwd=str(workdir),
        timeout=timeout,
        env=_tex_env(),
        runner=runner,
    )
    pdf = workdir / "math.pdf"
    if not pdf.is_file() or pdf.stat().st_size == 0:
        raise RuntimeError(t("math.no_pdf"))
    if pdf.stat().st_size > MAX_FILE_BYTES:
        raise RuntimeError(t("math.pdf_too_large"))
    return pdf


def _convert(
    converter: str,
    kind: str,
    workdir: Path,
    pdf: Path,
    *,
    dpi: int,
    timeout: float,
    runner,
) -> list[Path]:
    prefix = workdir / "page"
    command = [converter]
    if kind == "pdftocairo":
        command += ["-png", "-transp"]
    else:
        command += ["-png"]
    command += ["-r", str(int(dpi)), str(pdf), str(prefix)]
    _run(command, cwd=str(workdir), timeout=timeout, runner=runner)
    pages = sorted(workdir.glob("page-*.png"))
    return [path for path in pages if path.stat().st_size > len(PNG_MAGIC)]


def has_cjk(source: str) -> bool:
    """公式里是否有 CJK 字符（中文标签、全角括号等）。"""
    return CJK_PATTERN.search(source) is not None


def build_tex(sources: Sequence[str], color: str) -> str:
    """多页公式文档（``multi=mathpage``，LaTeX 后端用）。

    含 CJK 的页单独包 ``CJK*``（pdflatex 默认没有中文排版能力，会直接
    ``Unicode character … not set up for use with LaTeX`` 整篇失败）。
    只有真用到 CJK 时才加载 ``CJKutf8``，避免给纯数学文档增加依赖。
    """
    html = _html_color(color)
    pages: list[str] = []
    needs_cjk = False
    for source in sources:
        page = "$\\displaystyle " + source + "$"
        if has_cjk(source):
            needs_cjk = True
            page = f"\\begin{{CJK*}}{{UTF8}}{{{CJK_FONT}}}" + page + "\\end{CJK*}"
        pages.append("\\begin{mathpage}" + page + "\\end{mathpage}")
    preamble = [
        "\\documentclass[border=1pt,multi=mathpage]{standalone}",
        "\\usepackage{amsmath,amssymb,bm}",
        "\\usepackage{xcolor}",
    ]
    if needs_cjk:
        preamble.append("\\usepackage{CJKutf8}")
    preamble.append("\\newenvironment{mathpage}{}{}")
    return (
        "\n".join(preamble)
        + "\n\\begin{document}\n"
        + f"\\color[HTML]{{{html}}}\n"
        + "\n".join(pages)
        + "\n\\end{document}\n"
    )


# ---------------------------------------------------------------------------
# 裁剪 / 预合成 / 缩放 / 落盘
# ---------------------------------------------------------------------------


class _Clipped(RuntimeError):
    """页面装不下公式（内容触到页面右/下边缘）。"""


def _flatten_to_rgb(
    png: Path,
    background: str,
    *,
    color: str,
    white_to_alpha: bool,
    scale: float = 1.0,
) -> Path:
    """去透明边 + 预合成到背景色 + 缩放 → 临时 RGB PNG。"""
    from PIL import Image  # noqa: PLC0415 —— Pillow 是可选依赖

    image = Image.open(png)
    image.load()
    rgba = image.convert("RGBA")
    if white_to_alpha:
        # pdftoppm 没有 -transp：按亮度反推 alpha，并用主题前景色重上色
        from PIL import ImageChops  # noqa: PLC0415

        red, green, blue, _alpha = rgba.split()
        darkest = ImageChops.darker(ImageChops.darker(red, green), blue)
        alpha = darkest.point(lambda value: 255 - value)
        tint = Image.new("RGBA", rgba.size, _rgb(color) + (255,))
        tint.putalpha(alpha)
        rgba = tint
    bbox = rgba.getchannel("A").getbbox()
    if bbox is None:
        raise EmptyProduct(t("math.empty_product"))
    left, top, right, bottom = bbox
    pad = 2
    rgba = rgba.crop(
        (
            max(0, left - pad),
            max(0, top - pad),
            min(rgba.width, right + pad),
            min(rgba.height, bottom + pad),
        )
    )
    if 0 < scale < 1.0:
        width = max(1, round(rgba.width * scale))
        height = max(1, round(rgba.height * scale))
        rgba = rgba.resize((width, height), Image.LANCZOS)
    base = Image.new("RGBA", rgba.size, _rgb(background) + (255,))
    flattened = Image.alpha_composite(base, rgba).convert("RGB")
    out = png.with_suffix(".flat.png")
    flattened.save(out, format="PNG", optimize=True)
    return out


def _store(flat: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(flat, "rb") as handle:
        if handle.read(len(PNG_MAGIC)) != PNG_MAGIC:
            raise RuntimeError(t("math.not_png"))
    handle, temp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".png")
    os.close(handle)
    temp = Path(temp_name)
    try:
        shutil.copyfile(flat, temp)
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)
    return target


def _is_failed(key: str) -> bool:
    with _FAILED_LOCK:
        return key in _FAILED


def _mark_failed(key: str) -> None:
    with _FAILED_LOCK:
        _FAILED.add(key)


@contextlib.contextmanager
def _chrome_profile() -> Iterator[Path | None]:
    """共享 Chrome profile（热启动更快）；被占用时退回临时目录。"""
    base = cache_root().parent / "chrome-profile"
    try:
        base.mkdir(parents=True, exist_ok=True)
        handle = open(base.parent / "chrome-profile.lock", "w")  # noqa: SIM115
    except OSError:
        yield None
        return
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield None
            return
        yield base
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def render_formulas(
    sources: Sequence[str],
    *,
    color: str,
    background: str,
    cell: tuple[int, int] = (10, 20),
    dpi: int = DPI,
    timeout: float = TIMEOUT_SECONDS,
    tools: Tools | None = None,
    backend: Backend | None = None,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
    printer: Callable[..., Path] | None = None,
    max_formulas: int = MAX_FORMULAS,
    cache_root_override: Path | None = None,
    env: dict[str, str] | None = None,
) -> dict[int, Path]:
    """把 ``sources`` 渲染成 PNG → ``{index: path}``（**worker 专用**）。"""
    if not sources:
        return {}
    if backend is None:
        backend = (
            Backend(
                "latex",
                tools=tools,
                converter=tools.converter,
                converter_kind=tools.kind,
            )
            if tools is not None
            else select_backend()[0]
        )
    if backend is None:
        return {}

    em_px = terminal_em_px(cell, math_img_scale(env))
    if backend.name == "katex":
        render_dpi = katex.raster_dpi(em_px)
        scale = 1.0 / katex.SUPERSAMPLE
    else:
        render_dpi = dpi
        scale = min(1.0, em_px / (dpi * BASE_POINT_SIZE / 72.0))

    limit = min(len(sources), max(0, int(max_formulas)))
    wanted: list[tuple[int, str, str, Path]] = []
    out: dict[int, Path] = {}
    for index, raw in enumerate(sources):
        if index >= limit:
            break
        tex, _tag = normalize_formula(raw)
        if not tex:
            continue
        key = cache_key(
            tex,
            color=color,
            background=background,
            dpi=render_dpi,
            kind=backend.name,
            cell=cell,
            em=em_px,
        )
        target = _target_path(key, cache_root_override)
        if target.is_file():
            out[index] = target
            continue
        if _is_failed(key):
            continue
        wanted.append((index, tex, key, target))

    if not wanted:
        return out

    converter_path, converter_kind = backend.converter, backend.converter_kind
    if not converter_path:
        found = find_converter()
        if found is None:
            return out
        converter_path, converter_kind = found

    def finalize(pages: list[Path], entries: list[tuple[int, str, str, Path]]) -> None:
        for png, (index, tex, key, target) in zip(pages, entries):
            try:
                flat = _flatten_to_rgb(
                    png,
                    background,
                    color=color,
                    white_to_alpha=converter_kind != "pdftocairo",
                    scale=scale,
                )
                out[index] = _store(flat, target)
            except _Clipped:
                raise
            except Exception as exc:  # noqa: BLE001 —— 单条失败不影响其余
                _mark_failed(key)
                _record_failure(tex, _reason_for(backend.name, exc))
                log.debug("公式位图落盘失败：%r", tex[:80], exc_info=True)

    # 1) 批量
    try:
        with tempfile.TemporaryDirectory(prefix="myrtui-mathimg-") as tmp:
            workdir = Path(tmp)
            if backend.name == "katex":
                pages, entries = _katex_pages(
                    backend,
                    wanted,
                    color=color,
                    em_px=em_px,
                    workdir=workdir,
                    converter_path=converter_path,
                    converter_kind=converter_kind,
                    timeout=timeout,
                    runner=runner,
                    printer=printer,
                )
            else:
                assert backend.tools is not None
                pdf = _compile(
                    backend.tools,
                    workdir,
                    build_tex([entry[1] for entry in wanted], color),
                    timeout=timeout,
                    runner=runner,
                )
                pages = _convert(
                    converter_path,
                    converter_kind,
                    workdir,
                    pdf,
                    dpi=render_dpi,
                    timeout=timeout,
                    runner=runner,
                )
                entries = wanted
            if len(pages) != len(entries):
                raise RuntimeError(
                    t("math.batch_pages", got=len(pages), want=len(entries))
                )
            finalize(pages, entries)
            wanted = [entry for entry in wanted if entry[0] not in out]
    except Exception:  # noqa: BLE001 —— 批量失败 → 逐条
        log.debug("公式批量编译失败，改为逐条重试", exc_info=True)

    # 2) 逐条重试（批量里失败的 / 页数不符的）
    for index, tex, key, target in wanted:
        if index in out:
            continue
        try:
            with tempfile.TemporaryDirectory(prefix="myrtui-mathimg-") as tmp:
                workdir = Path(tmp)
                if backend.name == "katex":
                    pages, entries = _katex_pages(
                        backend,
                        [(index, tex, key, target)],
                        color=color,
                        em_px=em_px,
                        workdir=workdir,
                        converter_path=converter_path,
                        converter_kind=converter_kind,
                        timeout=timeout,
                        runner=runner,
                        printer=printer,
                    )
                else:
                    assert backend.tools is not None
                    pdf = _compile(
                        backend.tools,
                        workdir,
                        build_tex([tex], color),
                        timeout=timeout,
                        runner=runner,
                    )
                    pages = _convert(
                        converter_path,
                        converter_kind,
                        workdir,
                        pdf,
                        dpi=render_dpi,
                        timeout=timeout,
                        runner=runner,
                    )
                    entries = [(index, tex, key, target)]
                if not pages:
                    raise RuntimeError(t("math.no_png_after_convert"))
                finalize(pages[:1], entries[:1])
        except Exception as exc:  # noqa: BLE001 —— D5：坏一条不毁整篇
            _mark_failed(key)
            _record_failure(tex, _reason_for(backend.name, exc))
            log.debug("公式位图失败，回落 termtex：%r", tex[:80], exc_info=True)

    return out


def _katex_pages(
    backend: Backend,
    entries: list[tuple[int, str, str, Path]],
    *,
    color: str,
    em_px: float,
    workdir: Path,
    converter_path: str,
    converter_kind: str,
    timeout: float,
    runner,
    printer,
) -> tuple[list[Path], list[tuple[int, str, str, Path]]]:
    """KaTeX 批量：一次 Chrome 打印 + 一次 poppler；装不下就换更大页重试。"""
    assert backend.chrome is not None
    chrome = backend.chrome
    sources = [entry[1] for entry in entries]
    page = (katex.PAGE_WIDTH, katex.PAGE_HEIGHT)
    last_error: Exception | None = None
    for _attempt in range(3):
        print_work = workdir / f"katex{_attempt}"
        print_work.mkdir(parents=True, exist_ok=True)
        html = print_work / "page.html"
        # pdftoppm 没有透明通道：用纯黑渲染色，回填时按亮度反推 alpha 再上色
        html_color = color if converter_kind == "pdftocairo" else "#000000"
        html.write_text(
            katex.build_html(sources, color=html_color, page=page), encoding="utf-8"
        )
        pdf = print_work / "page.pdf"
        profile: Path | None
        with _chrome_profile() as shared:
            profile = shared
            if printer is None:
                katex.print_pdf(
                    chrome,
                    html,
                    pdf,
                    profile_dir=profile or (print_work / "profile"),
                    timeout=max(timeout, 30.0),
                )
            else:
                printer(
                    chrome,
                    html,
                    pdf,
                    profile_dir=profile or (print_work / "profile"),
                    timeout=max(timeout, 30.0),
                )
        pages = _convert(
            converter_path,
            converter_kind,
            print_work,
            pdf,
            dpi=katex.raster_dpi(em_px),
            timeout=timeout,
            runner=runner,
        )
        if len(pages) != len(entries):
            raise RuntimeError(
                t("math.katex_pages", got=len(pages), want=len(entries))
            )
        if _pages_clipped(pages, page, katex.SUPERSAMPLE):
            last_error = _Clipped(t("math.page_overflow", page=page))
            page = (
                min(page[0] * 2, katex.MAX_PAGE_WIDTH),
                min(page[1] * 2, katex.MAX_PAGE_HEIGHT),
            )
            continue
        return pages, entries
    raise last_error or RuntimeError(t("math.katex_failed"))


def _pages_clipped(
    pages: list[Path], page: tuple[int, int], supersample: int
) -> bool:
    """内容触到页面右/下边缘 → 视为被裁切（用更大页重试）。"""
    try:
        from PIL import Image  # noqa: PLC0415
    except Exception:  # noqa: BLE001 —— 没有 Pillow 时跳过裁剪检测
        return False
    limit_x = page[0] * supersample - 2
    limit_y = page[1] * supersample - 2
    for png in pages:
        try:
            with Image.open(png) as image:
                bbox = image.convert("RGBA").getchannel("A").getbbox()
        except Exception:  # noqa: BLE001
            continue
        if bbox is None:
            continue
        if bbox[2] >= limit_x or bbox[3] >= limit_y:
            return True
    return False


def _write_debug_failures(
    failures: list[tuple[str, str]],
    env: dict[str, str] | None = None,
) -> None:
    """``MYRTUI_MATH_IMG_DEBUG=1``：把回落原因写到缓存目录（不污染 TUI）。"""
    environ = os.environ if env is None else env
    if (environ.get(ENV_DEBUG) or "").strip() not in ("1", "true", "yes"):
        return
    try:
        target = cache_root() / "failures.log"
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            for source, reason in failures:
                handle.write(f"--- {reason}\n{source}\n")
    except OSError:
        log.debug("写公式失败诊断失败", exc_info=True)


def prewarm_document(
    text: str,
    *,
    color: str,
    background: str,
    cell: tuple[int, int] = (10, 20),
    dpi: int = DPI,
    backend: Backend | None = None,
    env: dict[str, str] | None = None,
    max_formulas: int = MAX_FORMULAS,
) -> str | None:
    """worker 专用：把整篇文档的块级公式渲染成 PNG（进磁盘缓存）。"""
    if not math_img_enabled(env):
        return None
    if "$" not in text:
        return None
    if backend is None:
        backend, warning = select_backend(env)
        if backend is None:
            return warning
    blocks = mathrender.block_sources(text)
    if not blocks:
        return None
    rendered = render_formulas(
        blocks,
        color=color,
        background=background,
        cell=cell,
        dpi=dpi,
        backend=backend,
        max_formulas=max_formulas,
        env=env,
    )
    failures = last_failures()
    if failures:
        _write_debug_failures(failures, env)
    if len(blocks) > max_formulas:
        return t("math.too_many", count=len(blocks), max=max_formulas)
    log.debug("公式位图（%s）：%d/%d 条", backend.name, len(rendered), len(blocks))
    return None
