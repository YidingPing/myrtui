"""KaTeX 位图前端（纯逻辑，不 import Textual）。

职责：把一批块级公式渲染成**每公式一页**的 PDF，交给 poppler 转 PNG。

为什么是 PDF 而不是截图：

- Chrome 的 PDF **不画白底**（2026-10-28 实测），`pdftocairo -png -transp`
  直接得到「透明背景 + 着色文字」，天然适合叠在终端背景上；
- 一页一公式 → 不需要注入 JS 量 boundingRect、不需要二次截图；
- 矢量 → 任意 DPI 重栅格化，最终由调用方缩到精确像素。

KaTeX 资产（css/js/woff2）vendor 在 ``htmlexport_data/``，**无 CDN、无网络**；
``file://`` 下相对字体的加载在 Chrome 156 实测无需额外 flag。

字体大小不在这里定：调用方用「CSS 16px + 栅格化 DPI」把 1em 映射到
终端 cell 高度（``dpi = 12 * em_px`` 给出 2× 超采样）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

log = logging.getLogger("myrtui.katex")

ASSET_DIR = Path(__file__).parent / "htmlexport_data"
KATEX_CSS = ASSET_DIR / "katex.min.css"
KATEX_JS = ASSET_DIR / "katex.min.js"
KATEX_VERSION_FILE = ASSET_DIR / "KATEX_VERSION"

ENV_CHROME = "MYRTUI_CHROME"

CSS_FONT_PX = 16
"""KaTeX 的基准字号（CSS px）；em→终端像素的换算靠栅格化 DPI。"""

SUPERSAMPLE = 2
"""先按 2× 栅格化，再由调用方 LANCZOS 缩到最终像素。"""

STYLE_VERSION = 2
"""影响产物像素的样式版本（进缓存键；改 CSS 时要 +1）。"""

PAGE_WIDTH = 2000
PAGE_HEIGHT = 480
"""每个公式页的 CSS 尺寸（够宽以容纳长公式；超出会触发重试用更大页）。"""

MAX_PAGE_WIDTH = 8000
MAX_PAGE_HEIGHT = 1920

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",
)
CHROME_NAMES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
    "microsoft-edge",
    "brave-browser",
)

PDF_MAGIC = b"%PDF-"


@dataclass(frozen=True)
class Chrome:
    """一个可用的 Chrome/Chromium 可执行文件。"""

    path: str


def find_chrome(env: dict[str, str] | None = None) -> Chrome | None:
    """``MYRTUI_CHROME`` → 常见安装路径 → ``PATH``。"""
    environ = os.environ if env is None else env
    forced = (environ.get(ENV_CHROME) or "").strip()
    if forced:
        if os.path.isfile(forced) and os.access(forced, os.X_OK):
            return Chrome(forced)
        log.warning("%s=%r 不可执行，忽略", ENV_CHROME, forced)
    for candidate in CHROME_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return Chrome(candidate)
    for name in CHROME_NAMES:
        found = shutil.which(name)
        if found:
            return Chrome(found)
    return None


def katex_version() -> str:
    try:
        return KATEX_VERSION_FILE.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def assets_available() -> bool:
    return KATEX_CSS.is_file() and KATEX_JS.is_file()


def raster_dpi(em_px: float) -> int:
    """让 1em（CSS 16px）栅格化成 ``SUPERSAMPLE × em_px`` 设备像素。

    ``dpi = 96 * SUPERSAMPLE * em_px / 16``；96/16 = 6，2× 即 12。
    """
    em_px = max(4.0, float(em_px))
    return max(96, int(round(6 * SUPERSAMPLE * em_px)))


def _escape_script(text: str) -> str:
    """JSON 塞进 ``<script>``：转义 ``<``，避免 ``</script>`` 提前闭合。"""
    return json.dumps(text, ensure_ascii=False).replace("<", "\\u003c")


def build_html(
    sources: Sequence[str],
    *,
    color: str,
    page: tuple[int, int] = (PAGE_WIDTH, PAGE_HEIGHT),
) -> str:
    """一页一公式的自包含 HTML（只引用本地 vendor 资产）。"""
    width, height = page
    payload = "[" + ", ".join(_escape_script(source) for source in sources) + "]"
    css_url = KATEX_CSS.resolve().as_uri()
    js_url = KATEX_JS.resolve().as_uri()
    safe_color = color if re.fullmatch(r"#[0-9A-Fa-f]{3,8}", color or "") else "#E0E0E0"
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<link rel="stylesheet" href="{css_url}">
<style>
@page {{ size: {width}px {height}px; margin: 0; }}
html, body {{ margin: 0; padding: 0; background: transparent; }}
body {{ font: {CSS_FONT_PX}px/1.2 "KaTeX_Main", serif; color: {safe_color};
        -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
.fx {{ padding: 6px; text-align: center; page-break-after: always;
       break-after: page; }}
.fx:last-child {{ page-break-after: auto; break-after: auto; }}
/* KaTeX 默认 .katex {{ font-size: 1.21em }}（为网页正文设计的放大），
   终端里公式应与正文同高 → 归一到 1em。 */
.katex {{ font-size: 1em; }}
.katex-display {{ margin: 0; }}
</style></head>
<body>
{''.join(f'<div class="fx" id="fx{i}"></div>' for i in range(len(sources)))}
<script src="{js_url}"></script>
<script>
const sources = {payload};
sources.forEach((source, index) => {{
  const host = document.getElementById('fx' + index);
  try {{
    katex.render(source, host, {{
      displayMode: true, throwOnError: true, output: 'html', strict: 'ignore'
    }});
  }} catch (error) {{
    // 不认识 / 畸形公式：这一页留空 → 后端按「空产物」标记失败 → 回落 termtex
    host.setAttribute('data-katex-error', '1');
  }}
}});
</script>
</body></html>
"""


def print_pdf(
    chrome: Chrome,
    html_path: Path,
    pdf_path: Path,
    *,
    profile_dir: Path,
    timeout: float = 30.0,
) -> Path:
    """Chrome headless 打印 PDF（无网络、无头、无页眉页脚）。"""
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    command = [
        chrome.path,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-pdf-header-footer",
        "--disable-extensions",
        "--disable-sync",
        "--disable-background-networking",
        "--disable-component-update",
        "--metrics-recording-only",
        f"--user-data-dir={profile_dir}",
        f"--print-to-pdf={pdf_path}",
        html_path.resolve().as_uri(),
    ]
    completed = subprocess.run(  # noqa: S603 —— 固定可执行 + 本地文件
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(
            f"Chrome 退出码 {completed.returncode}: {detail[-300:]}"
        )
    try:
        head = pdf_path.read_bytes()[: len(PDF_MAGIC)]
    except OSError as exc:
        raise RuntimeError(f"Chrome 未产出 PDF: {exc}") from exc
    if head != PDF_MAGIC:
        raise RuntimeError("Chrome 产物不是 PDF")
    return pdf_path
