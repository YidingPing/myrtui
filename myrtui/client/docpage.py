"""整篇 Markdown → 分页位图（Chrome PDF 分页 + pdftocairo）。

预览的「LaTeX 排版」只读视图用它：把**整篇** md 排成 HTML（正文 + KaTeX 行内 /
块级公式同一套排版）→ Chrome ``--print-to-pdf`` → ``pdftocairo -png``。
页面尺寸 = 终端可视区（``列 × cell_w`` × ``行 × cell_h``）px，``-r 96`` 出来
的 PNG 与页面 px 一一对应，显示端 1:1、不重采样。

为什么不用「一张长图 + 滚动」：单张几万像素高的图要么撞 Chrome 截图上限，要么
让 ``textual-image`` 每次滚动都重编码一大块缓冲（见 ``myrtui-internal/docs/V37.2-PAGED-PREVIEW-PLAN.md``
§2）。PDF 天然分页，一页 = 一屏，payload 有界。

本模块**纯逻辑**（不 import Textual），子进程只允许在 :func:`render_pages`
里发生 —— 调用方必须放进 worker（B1 同款约束）。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import platformdirs

from myrtui.client import katex, mathimg, mathrender
from myrtui.client.mathtext import is_probably_math, math_parser_factory

log = logging.getLogger("myrtui.docpage")

PAGE_STYLE_VERSION = 3
"""影响页面像素的样式版本（进缓存键；改 CSS / 字体时要 +1）。

v3：底色从纯白改为纸色，边框 / 代码块颜色改为按前景色叠加。
"""

MAX_PAGES = 400
"""单篇最多缓存多少页（防病态文档把缓存撑爆）。"""

MIN_FONT_PX = 9
"""正文最小字号（cell 很矮时也不至于糊成一团）。"""

CSS_DPI = 96.0
"""Chrome CSS px → PDF pt 的换算基准；``pdftocairo -r 96`` 正好还原 px。"""

PROFILE_MARGIN = 0.0
"""页边距留 0：页面像素 = 终端可视区像素（水平内边距写在 body 上）。"""

_BAD_COLOR = re.compile(r"[^#0-9A-Fa-f]")


class PageRenderError(RuntimeError):
    """LaTeX 排版不可用 / 失败（调用方据此回落到文本预览）。"""


@dataclass(frozen=True)
class PageSpec:
    """一页的像素几何（宽度、高度、字号、行高）。"""

    width: int
    height: int
    font_px: int
    line_px: int
    pad_x: int

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height


def page_spec(
    cell: tuple[int, int],
    cols: int,
    rows: int,
) -> PageSpec:
    """终端可视区 → 页面像素与正文字号。

    行高钉死成 ``cell_h``：文本密度与终端一致，且页面高度是它的整数倍，
    配合 ``break-inside: avoid`` 基本能把分页断点留在行与行之间。
    """
    cell_w = max(1, int(cell[0] or 10))
    cell_h = max(1, int(cell[1] or 20))
    width = max(200, int(cols) * cell_w)
    height = max(200, int(rows) * cell_h)
    line_px = cell_h
    font_px = max(MIN_FONT_PX, round(cell_h * 0.8))
    pad_x = max(8, min(48, cell_w * 2))
    return PageSpec(width, height, font_px, line_px, pad_x)


_CACHE_ROOT_OVERRIDE: Path | None = None


def cache_root() -> Path:
    if _CACHE_ROOT_OVERRIDE is not None:
        return Path(_CACHE_ROOT_OVERRIDE)
    return Path(platformdirs.user_cache_dir("myrtui", appauthor=False)) / "pages"


def set_cache_root(path: str | None) -> None:
    """测试钩子：把 LaTeX 排版缓存固定到临时目录（``None`` 恢复默认）。"""
    global _CACHE_ROOT_OVERRIDE
    _CACHE_ROOT_OVERRIDE = Path(path) if path else None


def page_key(
    text: str,
    *,
    source: str,
    colors: tuple[str, str],
    spec: PageSpec,
) -> str:
    """缓存键：正文 + 源路径 + 页面几何 + 主题色 + 资产/样式版本。"""
    payload = "\x1f".join(
        [
            str(Path(source)),
            hashlib.sha1(text.encode("utf-8")).hexdigest(),  # noqa: S324
            f"{spec.width}x{spec.height}",
            f"{spec.font_px}/{spec.line_px}",
            *colors,
            katex.katex_version(),
            str(PAGE_STYLE_VERSION),
        ]
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()  # noqa: S324


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def _safe_color(value: str, fallback: str) -> str:
    text = (value or "").strip()
    if re.fullmatch(r"#[0-9A-Fa-f]{3,8}", text):
        return text
    return fallback


def _is_light(color: str) -> bool:
    """``#RGB`` / ``#RRGGBB`` → 是否浅色（决定边框 / 代码块用黑还是白透明叠加）。"""
    value = (color or "").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(char * 2 for char in value)
    if len(value) < 6:
        return False
    try:
        red, green, blue = (int(value[index : index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return False
    return (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255 > 0.55


def _rgb_tuple(color: str, fallback: tuple[int, int, int] = (0, 0, 0)):
    value = (color or "").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(char * 2 for char in value)
    if len(value) < 6:
        return fallback
    try:
        return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return fallback


def _fade(color: str, alpha: float) -> str:
    """前景色 → 半透明叠加色（``rgba()``）：浅底用黑、深底用白，不引入冷灰。"""
    red, green, blue = _rgb_tuple(color)
    return f"rgba({red}, {green}, {blue}, {alpha})"


def _mix(color: str, other: str, ratio: float) -> str:
    """``color`` 向 ``other`` 混 ``ratio``（0–1）→ ``#RRGGBB``。

    header / 次要文字这类颜色要交给 Textual 的 ``styles``，只接受实色。
    """
    first = _rgb_tuple(color)
    second = _rgb_tuple(other)
    ratio = max(0.0, min(1.0, ratio))
    mixed = (
        round(first[index] + (second[index] - first[index]) * ratio)
        for index in range(3)
    )
    return "#" + "".join(f"{value:02X}" for value in mixed)


@dataclass(frozen=True)
class PagePalette:
    """LaTeX 排版页面的一套配色（同时驱动 HTML 与终端里的外围控件）。"""

    name: str
    background: str
    foreground: str
    header_background: str
    muted: str
    link: str
    border: str
    quote_border: str
    rule: str
    code_background: str
    pre_background: str
    table_head_background: str

    @property
    def colors(self) -> tuple[str, str]:
        """``(前景, 背景)``：缓存键与 ``render_pages`` 的口径。"""
        return self.foreground, self.background


def _light_palette(
    name: str,
    *,
    background: str,
    foreground: str,
    header_background: str,
    muted: str,
    link: str,
) -> PagePalette:
    """浅色预设：边框 / 代码块统一用前景色半透明叠加，避免白底上的冷灰块。"""
    return PagePalette(
        name=name,
        background=background,
        foreground=foreground,
        header_background=header_background,
        muted=muted,
        link=link,
        border=_fade(foreground, 0.16),
        quote_border=_fade(foreground, 0.22),
        rule=_fade(foreground, 0.14),
        code_background=_fade(foreground, 0.05),
        pre_background=_fade(foreground, 0.045),
        table_head_background=_fade(foreground, 0.045),
    )


PAPER_PAGE = _light_palette(
    "paper",
    background="#F7F4EC",
    foreground="#26241F",
    header_background="#EFEBE1",
    muted="#6B6459",
    link="#1A5FB4",
)
"""v0.4.1 的默认（`#F7F4EC`，14.1:1）。偏亮，但留着给喜欢干净白底的人。"""

SEPIA_PAGE = _light_palette(
    "sepia",
    background="#E6DABE",
    foreground="#3D3427",
    header_background="#DCD0B5",
    muted="#6C6251",
    link="#14508F",
)
"""默认：米黄纸（8.8:1）。

起因：v0.4.1 把 paper 设成默认后反馈"还是刺眼"，但 paper / gray / dim 之间的
亮度差只有 2–6/255，低于人眼可辨阈值——**只压亮度没用**。这一档靠色度（不是
亮度）拉开观感：平均通道 213，比纯白低 42，同时正文抬到 `#3D3427` 减少字缘晕光。
"""

GRAY_PAGE = _light_palette(
    "gray",
    background="#F4F4F2",
    foreground="#222222",
    header_background="#EBEBE8",
    muted="#5F625F",
    link="#0B6BCB",
)

WHITE_PAGE = _light_palette(
    "white",
    background="#FFFFFF",
    foreground="#111111",
    header_background="#F3F3F3",
    muted="#555555",
    link="#0969DA",
)
"""v0.4.0 的纯白（留作逃生口：有人就喜欢高对比）。"""

DIM_PAGE = _light_palette(
    "dim",
    background="#EDEDEA",
    foreground="#2E2E2E",
    header_background="#E2E2DE",
    muted="#5A5C58",
    link="#1F5FA0",
)
"""夜间低亮度：滤掉白光，接近电子墨水的观感。"""

DARK_PAGE = PagePalette(
    name="dark",
    background="#1E1E1E",
    foreground="#E0E0E0",
    header_background="#262626",
    muted="#8A8A8A",
    link="#6CB6FF",
    border=_fade("#FFFFFF", 0.22),
    quote_border=_fade("#FFFFFF", 0.25),
    rule=_fade("#FFFFFF", 0.2),
    code_background=_fade("#FFFFFF", 0.08),
    pre_background=_fade("#FFFFFF", 0.07),
    table_head_background=_fade("#FFFFFF", 0.06),
)
"""跟随深色终端主题（v0.4.0 之前的整篇观感）。"""

PAGE_THEMES: dict[str, PagePalette] = {
    palette.name: palette
    for palette in (SEPIA_PAGE, PAPER_PAGE, GRAY_PAGE, WHITE_PAGE, DIM_PAGE, DARK_PAGE)
}
DEFAULT_PAGE_THEME = SEPIA_PAGE.name
PAGE_THEME_ENV = "MYRTUI_PAGE_THEME"

PAGED_FOREGROUND = SEPIA_PAGE.foreground
PAGED_BACKGROUND = SEPIA_PAGE.background
PAGED_COLORS = SEPIA_PAGE.colors
"""默认米黄纸的 ``(前景, 背景)``；具体预设见 :data:`PAGE_THEMES`。"""


def palette_from_env(env: str | None = None) -> PagePalette:
    """``MYRTUI_PAGE_THEME=sepia|paper|gray|white|dim|dark`` → 调色板。

    非法值告警后回落默认（与 ``MYRTUI_GRAPHICS`` / ``MYRTUI_MATH_BACKEND``
    的口径一致：宁可用默认，不要因为拼错就白屏）。
    """
    raw = os.environ.get(PAGE_THEME_ENV, "") if env is None else env
    name = (raw or "").strip().lower()
    if not name:
        return PAGE_THEMES[DEFAULT_PAGE_THEME]
    palette = PAGE_THEMES.get(name)
    if palette is None:
        log.warning(
            "%s=%s 不是已知预设（%s），按 %s 处理",
            PAGE_THEME_ENV,
            raw,
            " / ".join(PAGE_THEMES),
            DEFAULT_PAGE_THEME,
        )
        return PAGE_THEMES[DEFAULT_PAGE_THEME]
    return palette


def palette_for_colors(colors: tuple[str, str]) -> PagePalette:
    """``(前景, 背景)`` → 调色板。

    先按颜色反查预设（保证同一组颜色在 HTML / 终端两侧观感一致），查不到
    再按亮度派生一套——老调用方与自定义主题都不受影响。
    """
    background = _safe_color(colors[1], "#1E1E1E")
    foreground = _safe_color(
        colors[0], "#111111" if _is_light(background) else "#E0E0E0"
    )
    for palette in PAGE_THEMES.values():
        if palette.colors == (foreground, background):
            return palette
    return _derive_palette(foreground, background)


def _derive_palette(foreground: str, background: str) -> PagePalette:
    """任意前景 / 背景 → 调色板（把 v0.4.0 的两套硬编码规则参数化）。"""
    if _is_light(background):
        tint = foreground
        alphas = (0.18, 0.22, 0.15, 0.05, 0.04, 0.04)
    else:
        tint = "#FFFFFF"
        alphas = (0.22, 0.25, 0.2, 0.08, 0.07, 0.06)
    return PagePalette(
        name="custom",
        background=background,
        foreground=foreground,
        header_background=_mix(background, tint, 0.05),
        muted=_mix(foreground, background, 0.45),
        link="#0969DA" if _is_light(background) else "#6CB6FF",
        border=_fade(tint, alphas[0]),
        quote_border=_fade(tint, alphas[1]),
        rule=_fade(tint, alphas[2]),
        code_background=_fade(tint, alphas[3]),
        pre_background=_fade(tint, alphas[4]),
        table_head_background=_fade(tint, alphas[5]),
    )


def _escape_script(text: str) -> str:
    import json

    return json.dumps(text, ensure_ascii=False).replace("<", "\\u003c")


_TAG_RE = re.compile(r"\\tag\{[^{}]*\}")
_LABEL_RE = re.compile(r"\\label\{([^{}]*)\}")


def rewrite_labels(source: str) -> str:
    """``\\label{X}`` → ``\\tag{X}``（KaTeX 没有 ``\\label``）。

    已经有 ``\\tag`` 的不再补（避免出现两个编号）。
    """
    if "\\label" not in source:
        return source
    if _TAG_RE.search(source):
        return _LABEL_RE.sub("", source)
    return _LABEL_RE.sub(lambda m: f"\\tag{{{m.group(1)}}}", source)


def escape_math(source: str) -> str:
    """公式源 → HTML 文本（``&`` / ``<`` / ``>`` 转义，浏览器会解码回去）。

    公式 token 走 ``html_inline`` / ``html_block``，markdown-it 不会再转义；
    不处理的话 ``a < b`` 会被当成标签、``</script>`` 能提前闭合脚本。
    """
    return (
        source.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _math_tokens(text: str) -> list:
    """md → token 流，数学 token 换成 KaTeX 定界符（``\\(…\\)`` / ``\\[…\\]``）。"""
    from markdown_it.token import Token as MarkdownToken  # noqa: PLC0415

    parser = math_parser_factory()
    tokens = parser.parse(text)
    for token in tokens:
        if token.type in ("math_block", "math_block_label"):
            body = escape_math(rewrite_labels((token.content or "").strip()))
            token.type = "html_block"
            token.tag = ""
            token.content = (
                f'<div class="math-display">\\[{body}\\]</div>\n'
                if body
                else ""
            )
        if token.type != "inline" or not token.children:
            continue
        children = []
        for child in token.children:
            if child.type in ("math_inline", "math_inline_double"):
                source = (child.content or "").strip()
                if source and is_probably_math(source):
                    wrapper = MarkdownToken("html_inline", "", 0)
                    wrapper.content = (
                        f"\\({escape_math(rewrite_labels(source))}\\)"
                    )
                    children.append(wrapper)
                else:
                    literal = MarkdownToken("text", "", 0)
                    markup = child.markup or "$"
                    literal.content = f"{markup}{child.content}{markup}"
                    children.append(literal)
            else:
                children.append(child)
        token.children = children
    return tokens


_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_IMG_SRC_ATTR = re.compile(r'\bsrc\s*=\s*("([^"]*)"|\'([^\']*)\')', re.IGNORECASE)
_DANGEROUS_TAG = re.compile(
    r"<\s*(script|iframe|object|embed|link|meta|base|form|style)\b[^>]*>"
    r"(?:.*?<\s*/\s*\1\s*>)?",
    re.IGNORECASE | re.DOTALL,
)
_ON_ATTR = re.compile(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
_JS_URL = re.compile(r"\b(href|src)\s*=\s*(\"|')\s*javascript:[^\"']*\2", re.IGNORECASE)

MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _data_uri(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_IMAGE_BYTES:
            return None
        payload = path.read_bytes()
    except OSError:
        return None
    import base64
    import mimetypes

    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def sanitize_body(html: str) -> str:
    """删掉「会联网 / 会执行」的标签与属性（预览只读、承诺不联网）。

    markdown-it 的 ``html: true`` 允许原始 HTML：一份 md 里写
    ``<script src="https://…">`` 会让 Chrome 真的发请求。这里只针对**主动
    危险**的东西（脚本 / iframe / 外链样式 / 事件属性 / ``javascript:``），
    正文与 KaTeX 需要的标签一律不动。
    """
    cleaned = _DANGEROUS_TAG.sub("", html)
    cleaned = _ON_ATTR.sub("", cleaned)
    return _JS_URL.sub(r'\1=""', cleaned)


def _inline_images(html: str, base_dir: Path | None) -> str:
    """本地图片内联成 data URI；远程 / 缺失的整段删掉（不联网）。"""

    def replace(match: re.Match[str]) -> str:
        tag = match.group(0)
        found = _IMG_SRC_ATTR.search(tag)
        if found is None:
            return tag
        src = found.group(2) if found.group(2) is not None else found.group(3) or ""
        if src.startswith("data:"):
            return tag
        if src.startswith(("http://", "https://", "//")):
            return ""
        raw = src[7:] if src.startswith("file://") else src
        candidate = Path(raw)
        if not candidate.is_absolute() and base_dir is not None:
            candidate = base_dir / raw
        uri = _data_uri(candidate)
        if uri is None:
            return ""
        return tag[: found.start()] + f'src="{uri}"' + tag[found.end() :]

    return _IMG_TAG.sub(replace, html)


def build_document_html(
    text: str,
    *,
    colors: tuple[str, str],
    spec: PageSpec,
    base_dir: Path | None = None,
    palette: PagePalette | None = None,
) -> str:
    """整篇 md → 自包含 HTML（零 http(s)，KaTeX 走本地 vendor 资产）。"""
    chosen = palette or palette_for_colors(colors)
    background = chosen.background
    foreground = chosen.foreground
    border = chosen.border
    quote_border = chosen.quote_border
    rule = chosen.rule
    code_bg = chosen.code_background
    pre_bg = chosen.pre_background
    th_bg = chosen.table_head_background
    link = chosen.link
    parser = math_parser_factory()
    tokens = _math_tokens(text)
    body = parser.renderer.render(tokens, parser.options, {})
    body = sanitize_body(body)
    body = _inline_images(body, base_dir)

    css_url = katex.KATEX_CSS.resolve().as_uri()
    js_url = katex.KATEX_JS.resolve().as_uri()
    auto_url = (katex.ASSET_DIR / "auto-render.min.js").resolve().as_uri()
    line = spec.line_px
    font = spec.font_px
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<link rel="stylesheet" href="{css_url}">
<style>
@page {{ size: {spec.width}px {spec.height}px; margin: {PROFILE_MARGIN}px; }}
html, body {{ margin: 0; padding: 0; }}
body {{
  background: {background}; color: {foreground};
  font: {font}px/{line}px -apple-system, BlinkMacSystemFont, "Helvetica Neue",
        "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", sans-serif;
  padding: 0 {spec.pad_x}px;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
  word-wrap: break-word; overflow-wrap: anywhere;
}}
h1, h2, h3, h4, h5, h6 {{
  margin: {line}px 0 {line // 2}px; line-height: 1.25; font-weight: 600;
  break-after: avoid; break-inside: avoid;
}}
h1 {{ font-size: 1.65em; }} h2 {{ font-size: 1.4em; }}
h3 {{ font-size: 1.2em; }} h4, h5, h6 {{ font-size: 1.05em; }}
p, ul, ol, dl, blockquote, pre, table, figure {{
  margin: 0 0 {line // 2}px; break-inside: avoid;
}}
p, li {{ orphans: 2; widows: 2; }}
ul, ol {{ padding-left: 2.2em; }}
li {{ margin: 0; }}
blockquote {{
  margin-left: 0; padding: 0 0 0 0.9em;
  border-left: 3px solid {quote_border}; opacity: 0.9;
}}
hr {{ border: 0; border-top: 1px solid {rule}; margin: {line // 2}px 0; }}
a {{ color: {link}; text-decoration: none; }}
code {{
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.92em; background: {code_bg};
  padding: 0 2px; border-radius: 3px;
}}
pre {{
  background: {pre_bg}; border-radius: 4px;
  padding: {line // 2}px 0.9em; overflow: hidden;
}}
pre code {{ background: none; padding: 0; font-size: 0.92em; }}
table {{ border-collapse: collapse; }}
th, td {{ border: 1px solid {border}; padding: 1px 0.6em; }}
th {{ background: {th_bg}; font-weight: 600; }}
img {{ max-width: 100%; height: auto; }}
.math-display {{ text-align: center; margin: {line // 2}px 0; break-inside: avoid; }}
.katex {{ font-size: 1em; }}
.katex-display {{ margin: 0; }}
</style></head>
<body>
{body}
<script src="{js_url}"></script>
<script src="{auto_url}"></script>
<script>
renderMathInElement(document.body, {{
  delimiters: [
    {{left: '\\\\[', right: '\\\\]', display: true}},
    {{left: '\\\\(', right: '\\\\)', display: false}}
  ],
  throwOnError: false,
  strict: 'ignore'
}});
</script>
</body></html>
"""


# ---------------------------------------------------------------------------
# 渲染（worker 专用）
# ---------------------------------------------------------------------------


def _run(
    command: Sequence[str],
    *,
    cwd: str,
    timeout: float,
    runner=None,
) -> None:
    completed = (runner or subprocess.run)(  # noqa: S603 —— 固定可执行
        list(command),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or b"").decode("utf-8", "replace").strip()
        raise PageRenderError(
            f"{Path(command[0]).name} 退出码 {completed.returncode}: {detail[-300:]}"
        )


def _normalize_page(path: Path, spec: PageSpec, background: str) -> None:
    """把页面裁/补到**精确** ``spec.size``（只裁边、不缩放），并铺满底色。

    Chrome 的 ``@page`` 像素→pt 换算会掉一两个 0.01pt（实测 1100×780 得到
    1100×781），直接显示会被 ``textual-image`` 缩一丁点。这里按目标尺寸
    裁掉多出的边、缺的用背景色补齐，保证页与页之间 1:1 对齐。

    同时把带 alpha 的页（``pdftocairo -transp`` 或未来换后端）合成到页面底色
    上——否则透明区在终端里是黑块。整页铺色只在这一处，纸色不依赖 Chrome 是否
    真的打印了 ``body`` 背景。
    """
    try:
        from PIL import Image  # noqa: PLC0415 —— 可选依赖（位图链路）
    except Exception:  # noqa: BLE001 —— 没有 Pillow 就按原尺寸用
        return
    target = spec.size
    with Image.open(path) as raw:
        raw.load()
        image = raw.convert("RGBA")
        cropped = image.crop(
            (0, 0, min(target[0], image.width), min(target[1], image.height))
        )
    if image.size == target and not _has_transparency(cropped):
        # 尺寸正好且完全不透明：不重新编码（省一次 PNG 写盘）
        return
    base = Image.new("RGBA", target, (*mathimg._rgb(background), 255))  # noqa: SLF001
    base.alpha_composite(cropped, (0, 0))
    base.convert("RGB").save(path)


def _has_transparency(image) -> bool:
    """RGBA 图是否真有透明像素（整张 alpha=255 时省掉一次重编码）。"""
    try:
        alpha = image.getchannel("A")
    except Exception:  # noqa: BLE001 —— 没有 alpha 通道
        return False
    return alpha.getextrema()[0] < 255


def _cached_pages(root: Path) -> list[Path] | None:
    """完整缓存（``count`` 落地 = LaTeX 排版成功）→ 页文件列表。"""
    try:
        count = int((root / "count").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if count <= 0:
        return None
    pages = [root / f"p-{index:03d}.png" for index in range(1, count + 1)]
    for page in pages:
        try:
            if page.stat().st_size <= len(mathimg.PNG_MAGIC):
                return None
        except OSError:
            return None
    return pages


def cached_pages(
    text: str,
    *,
    source: str,
    colors: tuple[str, str],
    spec: PageSpec,
) -> list[Path] | None:
    """**只查磁盘**（事件循环安全）：命中返回页文件列表。"""
    return _cached_pages(cache_root() / page_key(text, source=source, colors=colors, spec=spec))


def render_pages(
    text: str,
    *,
    source: str,
    colors: tuple[str, str],
    spec: PageSpec,
    base_dir: Path | None = None,
    cache_root_override: Path | None = None,
    timeout: float = 90.0,
    runner=None,
    printer=None,
) -> list[Path]:
    """整篇 → 分页 PNG（**worker 专用**；缓存命中时 0 子进程）。"""
    key = page_key(text, source=source, colors=colors, spec=spec)
    root = (Path(cache_root_override) if cache_root_override else cache_root()) / key
    cached = _cached_pages(root)
    if cached is not None:
        return cached

    chrome = katex.find_chrome()
    if chrome is None:
        raise PageRenderError("未找到 Chrome/Chromium（LaTeX 排版需要它）")
    converter = mathimg.find_converter()
    if converter is None:
        raise PageRenderError("未找到 pdftocairo / pdftoppm")

    html = build_document_html(text, colors=colors, spec=spec, base_dir=base_dir)
    with tempfile.TemporaryDirectory(prefix="myrtui-pages-") as tmp:
        workdir = Path(tmp)
        html_path = workdir / "doc.html"
        html_path.write_text(html, encoding="utf-8")
        pdf = workdir / "doc.pdf"
        profile = workdir / "profile"
        if printer is None:
            katex.print_pdf(
                chrome, html_path, pdf, profile_dir=profile, timeout=timeout
            )
        else:
            printer(chrome, html_path, pdf, profile_dir=profile, timeout=timeout)
        if not pdf.is_file() or pdf.stat().st_size == 0:
            raise PageRenderError("Chrome 没有产出 PDF")

        prefix = workdir / "p"
        command = [converter[0], "-png", "-r", str(int(CSS_DPI))]
        command += [str(pdf), str(prefix)]
        _run(command, cwd=str(workdir), timeout=timeout, runner=runner)
        pages = [
            path
            for path in sorted(workdir.glob("p-*.png"))
            if path.stat().st_size > len(mathimg.PNG_MAGIC)
        ]
        if not pages:
            raise PageRenderError("PDF 没有转出任何页面")
        if len(pages) > MAX_PAGES:
            raise PageRenderError(f"页数过多（{len(pages)} > {MAX_PAGES}）")

        root.mkdir(parents=True, exist_ok=True)
        for page in pages:
            _normalize_page(page, spec, colors[1])
        for index, page in enumerate(pages, 1):
            target = root / f"p-{index:03d}.png"
            temp = target.with_suffix(".tmp")
            shutil.copyfile(page, temp)
            os.replace(temp, target)
        (root / "count").write_text(str(len(pages)), encoding="utf-8")
        return [root / f"p-{index:03d}.png" for index in range(1, len(pages) + 1)]


def invalidate(
    text: str,
    *,
    source: str,
    colors: tuple[str, str],
    spec: PageSpec,
) -> None:
    """丢掉这篇文档的 LaTeX 排版缓存（``r`` 重排版）。"""
    shutil.rmtree(
        cache_root() / page_key(text, source=source, colors=colors, spec=spec),
        ignore_errors=True,
    )


def reset_cache() -> None:
    """测试钩子：清掉 LaTeX 排版缓存。"""
    shutil.rmtree(cache_root(), ignore_errors=True)
