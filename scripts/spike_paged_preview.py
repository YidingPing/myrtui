#!/usr/bin/env python3
"""v0.37.2 真机点验：整篇渲染视图（PDF 分页位图）。

**不是运行时功能**，只是验收辅助脚本：自建一篇多页样例 md，拉起
``MarkdownPreviewScreen``，按 ``V`` 进入整篇渲染视图。

    .venv/bin/python scripts/spike_paged_preview.py

期望（Tabby / Sixel）：

- 文本预览里按 `V` → 标题变成「… · 整篇渲染 · N 页 · Esc 返回 · r 重渲染」；
- 1–2 秒后出现整页位图（正文 + 行内公式 + 块级公式同一套排版）；
- 滚动无缝：页与页之间没有空白、没有半截行；
- `r` 重渲染（约 1 秒）；`Esc` 回文本预览，`q` 退出。

前提：Chrome/Chromium + pdftocairo（与公式位图相同）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from textual.app import App

from myrtui.client.graphics import describe, detect
from myrtui.client.mdpreview import MarkdownPreviewScreen

DOC = """# 整篇渲染点验（v0.37.2）

这一段用来检查**正文排版**：行内公式 $x^2+y^2=z^2$ 与正文同基线，还有一个
$\\sigma_{\\mathrm{floor}}$ 与 `inline code`，以及一个[链接](https://example.com)。

## 块级公式

$$
f(\\mathrm{lt}) = \\mathrm{lt} - \\underbrace{\\frac{|\\boldsymbol{x}_{\\mathrm{rx}}|}{c}}_{\\text{几何光行时}} - \\underbrace{\\Delta t_S(\\mathrm{lt})}_{\\text{Shapiro（§3.4）}} = 0 \\tag{7}
$$

$$
\\begin{bmatrix} a & b \\\\ c & d \\end{bmatrix} \\begin{pmatrix} x \\\\ y \\end{pmatrix} = \\begin{pmatrix} ax+by \\\\ cx+dy \\end{pmatrix}
$$

## 列表与代码

1. 第一条：$\\alpha + \\beta$
2. 第二条：$\\int_0^1 x\\,dx = \\tfrac12$
3. 第三条

```python
def hello(name: str) -> str:
    return f"hi {name}"
```

## 表格

| 符号 | 含义 | 值 |
|---|---|---|
| $c$ | 光速 | 299792458 m/s |
| $\\sigma$ | 标准差 | 0.5 |

## 长段落（用于翻页）

""" + "\n\n".join(
    f"第 {i} 段：这是一段用于填满页面、验证分页边界的文字。" * 2 for i in range(1, 41)
) + """

---

末尾：如果页边界切断了某一行，说明 `break-inside: avoid` 没兜住，需要回到
计划 §3 的「对齐行框」方案。
"""


class Demo(App):
    def __init__(self, doc: Path, capabilities) -> None:
        super().__init__()
        self._doc = doc
        self._capabilities = capabilities

    def on_mount(self) -> None:
        self.push_screen(
            MarkdownPreviewScreen(str(self._doc), graphics=self._capabilities)
        )


def main() -> int:
    capabilities, warning = detect()
    print(f"{describe(capabilities)}  available={capabilities.available}", file=sys.stderr)
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
    if not capabilities.bitmaps:
        print(
            "位图未启用，整篇渲染不可用——检查终端 Sixel/TGP、textual-image、"
            "外层 tmux 的 terminal-features。",
            file=sys.stderr,
        )
    root = Path(tempfile.mkdtemp(prefix="myrtui-v372-paged-"))
    doc = root / "paged.md"
    doc.write_text(DOC, encoding="utf-8")
    print(f"样例：{doc}", file=sys.stderr)
    print("提示：进入后按 V 打开整篇渲染；r 重渲染；Esc 返回。", file=sys.stderr)
    Demo(doc, capabilities).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
