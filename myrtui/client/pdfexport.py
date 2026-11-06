"""Markdown → PDF（Markdown 预览层的 ``p`` / ``m``）。

**定位**：终端里读完一篇 md，要一份**能发出去 / 能打印 / 能在手机上看**的文件。
终端逐页位图（``v`` 屏）解决的是「在终端里看清排版」，这里解决的是「拿走」——
矢量、可选中、白底黑字。两个版式共用同一条正文管线：

- ``p`` → A4 打印版：10.5 pt、上下 16 mm / 左右 14 mm；
- ``m`` → 手机阅读版：仍是 A4（页宽不变，公式和表格更容易放下），12 pt、
  6 mm 页边距；手机横屏或放大看。超宽公式 / 表格 / 代码块会按页宽缩字号，
  避免右侧被裁掉。

为什么复用 ``v`` 那条链而不是另起一套排版：``docpage.render_body()`` 已经把
「md → 自包含 HTML」做到了公式 / 表格 / 代码块 / 本地图片逐字一致，Chrome
``--print-to-pdf`` 又已经由 ``katex.print_pdf()`` 封装好（原子写、魔数校验、
错误文案）。这里换掉的只有两件事：**打印样式**（A4 纸 + 页边距）和**目的地**
（源文件同目录，而不是缓存目录）。

历史（别重走）：v0.27 做过同样的事（``htmlexport.write_pdf`` + ``@page A4``），
v0.28 随整条预览层一起删除。这次从 ``992a56f`` 取回排版思路，但正文管线换成
v0.36+ 的 ``docpage.render_body``（不再有第二套 token 流），产物也改成用户文件。

三条约束：

1. **正文与预览同源**：公式判定、``\\label`` → ``\\tag``、图片内联全部走
   ``docpage.render_body``，不允许在这里重写；
2. **不联网**：KaTeX 走本地 vendor 资产，``http(s)`` 图片由 ``render_body``
   直接删掉；
3. **失败不破坏旧文件**：先写临时文件，成功后才 ``os.replace``。

纯逻辑，不 import Textual；子进程只发生在 :func:`export_pdf` 里 —— 调用方
**必须**放进 worker。
"""
from __future__ import annotations

import html
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from myrtui.client import docpage, katex
from myrtui.i18n import t

log = logging.getLogger("myrtui.pdfexport")

A4_MARGIN = "16mm 14mm"
"""A4 页边距：上下 16 mm、左右 14 mm（正文约 95 mm 宽的 1.2 倍行距，常见报告口径）。"""

FONT_PT = 10.5
"""正文字号（pt）。10.5pt ≈ 五号字，中文正文的常规选择。"""

LINE_HEIGHT = 1.6
"""行高倍数：中文正文 1.6 比 1.4 更松，长段落读起来不挤。"""

PHONE_MARGIN = "6mm"
"""手机阅读版的页边距。只给屏幕看，不是打印口径：打印机通常要求 ≥5 mm。"""

PHONE_FONT_PT = 12.0
"""手机阅读版的正文字号：比打印版大 14%，横屏/放大后约等于手机 18–20 px。"""

PHONE_CSS = """
table { font-size: 0.9em; }
th, td { padding: 0.2em 0.5em; }
pre { white-space: pre; overflow: hidden; }
pre code { font-size: 1em; }
.katex-display { break-inside: avoid; }
"""

PHONE_JS = """
<script>
(function () {
  function shrink(el, inner, minScale) {
    var avail = el.getBoundingClientRect().width;
    if (!avail) return;
    var width = inner.getBoundingClientRect().width;
    if (!width || width <= avail) return;
    var scale = Math.max(minScale, (avail / width) * 0.98);
    el.style.fontSize = (parseFloat(getComputedStyle(el).fontSize) * scale) + 'px';
  }
  document.querySelectorAll('.katex-display').forEach(function (el) {
    var inner = el.querySelector('.katex');
    if (inner) shrink(el, inner, 0.3);
  });
  document.querySelectorAll('pre').forEach(function (el) {
    shrink(el, el.querySelector('code') || el, 0.4);
  });
  document.querySelectorAll('table').forEach(function (el) {
    var avail = el.parentElement.getBoundingClientRect().width;
    var width = el.scrollWidth;
    if (!avail || width <= avail) return;
    var scale = Math.max(0.5, (avail / width) * 0.98);
    el.style.fontSize = (parseFloat(getComputedStyle(el).fontSize) * scale) + 'px';
  });
})();
</script>
"""
"""手机阅读版的兜底排版：只缩**超宽**的公式 / 代码块 / 表格。

A4 的页宽比手机窄页大，正常情况下这段脚本一次都不会触发；触发时也是缩字号
而不是裁掉右侧。KaTeX auto-render 是同步的，所以本脚本紧跟在它后面即可
（``export_pdf`` 还会传 ``--virtual-time-budget`` 兜底）。
"""

CHROME_TIMEOUT_SECONDS = 60.0
"""整篇导出给 Chrome 的上限：比 ``v`` 屏的 30 s 宽，因为文档可能几十页。"""

VIRTUAL_TIME_BUDGET_SECONDS = 3.0
"""给页内 KaTeX auto-render 留的时间；``v`` 屏不传这个 flag，只有导出传。"""

# 打印专用的**固定浅色**：不跟随终端主题。sepia / dark 主题打出来是整片棕或
# 整片黑，费墨且不可读；导出是「交给别人 / 打印」，配色必须和屏幕解耦。
BACKGROUND = "#ffffff"
FOREGROUND = "#1a1a1a"
MUTED = "#555555"
BORDER = "#cccccc"
RULE = "#dddddd"
CODE_BACKGROUND = "#f4f4f4"
PRE_BACKGROUND = "#f7f7f7"
TABLE_HEAD_BACKGROUND = "#f0f0f0"
LINK = "#0b4f9e"


@dataclass(frozen=True)
class ExportResult:
    """一次导出的结果（``error`` 为空即成功；UI 据此决定提示什么）。"""

    path: Path
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class PrintPreset:
    """一种导出附录（纸张 / 边距 / 字号 / 文件名后缀 / 额外排版）。

    ``suffix`` 进文件名：手机版不能覆盖同目录的 A4 打印版
    （``README.md`` → ``README.pdf`` vs ``README.phone.pdf``）。
    """

    key: str
    page: str
    margin: str
    font_pt: float
    line_height: float
    suffix: str = ""
    extra_css: str = ""
    fit_script: str = ""


A4 = PrintPreset(
    key="a4",
    page="A4",
    margin=A4_MARGIN,
    font_pt=FONT_PT,
    line_height=LINE_HEIGHT,
)

PHONE = PrintPreset(
    key="phone",
    page="A4",
    margin=PHONE_MARGIN,
    font_pt=PHONE_FONT_PT,
    line_height=LINE_HEIGHT,
    suffix=".phone",
    extra_css=PHONE_CSS,
    fit_script=PHONE_JS,
)


def target_for(source: str | Path, preset: PrintPreset = A4) -> Path:
    """目的地：**源文件同目录**的 ``<stem><suffix>.pdf``。

    A4 仍是 ``README.md`` → ``README.pdf``；手机版是 ``README.phone.pdf``。
    """
    src = Path(source)
    return src.with_name(f"{src.stem}{preset.suffix}.pdf")


def build_print_html(
    text: str,
    *,
    base_dir: Path | None = None,
    title: str = "",
    preset: PrintPreset = A4,
) -> str:
    """整篇 md → 打印用 HTML（自包含；正文来自 ``docpage.render_body``）。

    与 ``v`` 屏的模板有三处**故意不同**，都是打印语义逼出来的：

    - ``break-inside: avoid`` **不给 ``p``**：一页一屏时代的写法，A4 下会让长
      段落整段被推到下一页，然后仍然溢出；
    - ``pre`` 用 ``white-space: pre-wrap``：``overflow: auto`` 在纸上等于
      **静默裁掉**右半边；
    - 加 ``print-color-adjust: exact``：否则代码块 / 表头底色打印时全白。

    ``preset`` 只换纸张 / 边距 / 字号和附件；正文一个字都不变。手机版额外带一段
    兜底脚本（见 ``PHONE_JS``），A4 打印版不注入它。
    """
    body = docpage.render_body(text, base_dir=base_dir)
    heading = html.escape(title or "")
    extra_css = f"\n{preset.extra_css}" if preset.extra_css else ""
    fit_script = f"\n{preset.fit_script}" if preset.fit_script else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>{heading}</title>
{docpage.katex_stylesheet()}
<style>
@page {{ size: {preset.page}; margin: {preset.margin}; }}
html, body {{ margin: 0; padding: 0; }}
body {{
  background: {BACKGROUND}; color: {FOREGROUND};
  font: {preset.font_pt:g}pt/{preset.line_height} -apple-system, BlinkMacSystemFont,
        "Helvetica Neue", "PingFang SC", "Hiragino Sans GB",
        "Noto Sans CJK SC", sans-serif;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
  word-wrap: break-word; overflow-wrap: anywhere;
}}
h1, h2, h3, h4, h5, h6 {{
  margin: 1.1em 0 0.5em; line-height: 1.25; font-weight: 600;
  break-after: avoid; break-inside: avoid;
}}
h1 {{ font-size: 1.7em; }} h2 {{ font-size: 1.4em; }}
h3 {{ font-size: 1.2em; }} h4, h5, h6 {{ font-size: 1.05em; }}
p, ul, ol, dl, blockquote {{ margin: 0 0 0.6em; }}
p, li {{ orphans: 2; widows: 2; }}
ul, ol {{ padding-left: 2.2em; }}
li {{ margin: 0 0 0.15em; }}
blockquote {{
  padding: 0 0 0 0.9em; border-left: 2px solid {BORDER}; color: {MUTED};
}}
hr {{ border: 0; border-top: 1px solid {RULE}; margin: 1em 0; }}
a {{ color: {LINK}; text-decoration: none; }}
code {{
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.9em; background: {CODE_BACKGROUND};
  padding: 0 2px; border-radius: 3px;
}}
pre {{
  background: {PRE_BACKGROUND}; border: 1px solid {RULE}; border-radius: 4px;
  padding: 0.5em 0.8em; margin: 0 0 0.6em;
  white-space: pre-wrap; overflow-wrap: anywhere;
  break-inside: avoid;
}}
pre code {{ background: none; padding: 0; }}
table {{ border-collapse: collapse; margin: 0 0 0.7em; break-inside: avoid; }}
thead {{ display: table-header-group; }}
th, td {{ border: 1px solid {BORDER}; padding: 0.25em 0.6em; text-align: left; }}
th {{ background: {TABLE_HEAD_BACKGROUND}; font-weight: 600; }}
img {{ max-width: 100%; height: auto; break-inside: avoid; }}
figure {{ margin: 0 0 0.6em; break-inside: avoid; }}
.math-display, .katex-display {{
  text-align: center; margin: 0.6em 0; break-inside: avoid;
}}
.katex {{ font-size: 1.05em; }}{extra_css}
</style></head>
<body>
{body}
{docpage.katex_scripts()}{fit_script}
</body></html>
"""


def export_pdf(
    text: str,
    source: str | Path,
    *,
    target: str | Path | None = None,
    chrome: katex.Chrome | None = None,
    preset: PrintPreset = A4,
    runner=None,
    timeout: float = CHROME_TIMEOUT_SECONDS,
) -> ExportResult:
    """整篇 Markdown → PDF（**worker 专用**：会 fork Chrome）。

    ``source`` 决定两件事：目的地（同目录 ``<stem><suffix>.pdf``，``target``
    可覆盖）和图片内联的相对路径基准。``preset`` 决定版式（``A4`` / ``PHONE``）。
    失败返回 ``ExportResult.error``，**不抛异常**——调用方是 UI，一次导出失败
    不该让预览层崩掉。
    """
    src = Path(source)
    out = Path(target) if target is not None else target_for(src, preset)
    executable = chrome or katex.find_chrome()
    if executable is None:
        return ExportResult(out, error=t("md.export_no_chrome"))

    # 临时 HTML 与 Chrome profile 都在这里，导出结束即删。
    with tempfile.TemporaryDirectory(prefix="myrtui-pdf-") as tmp:
        workdir = Path(tmp)
        html_path = workdir / "doc.html"
        html_path.write_text(
            build_print_html(
                text, base_dir=src.parent, title=src.stem, preset=preset
            ),
            encoding="utf-8",
        )
        # 临时 PDF 必须和目标**同一个目录**：os.replace 才是原子的（跨文件系统
        # 会退化成 copy+unlink，断电就留半截）。
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=out.parent, prefix=".myrtui-pdf-", suffix=".pdf"
            )
        except OSError as exc:
            return ExportResult(out, error=t("md.export_failed", exc=exc))
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            katex.print_pdf(
                executable,
                html_path,
                tmp_path,
                profile_dir=workdir / "profile",
                timeout=timeout,
                virtual_time_budget=VIRTUAL_TIME_BUDGET_SECONDS,
                runner=runner,
            )
            os.replace(tmp_path, out)
        except subprocess.TimeoutExpired:
            log.warning("PDF 导出超时：%s（%.0fs）", out, timeout)
            return ExportResult(
                out, error=t("md.export_timeout", seconds=int(timeout))
            )
        except (RuntimeError, OSError) as exc:
            log.warning("PDF 导出失败：%s", out, exc_info=True)
            return ExportResult(out, error=t("md.export_failed", exc=exc))
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return ExportResult(out)
