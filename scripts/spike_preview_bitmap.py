#!/usr/bin/env python3
"""v0.37 真机点验：Markdown 预览的位图（块级公式 + 本地图片）。

**不是运行时功能**，只是验收辅助脚本：自建样例目录（含透明 PNG + 公式 +
缺失/远程图片），用真实能力探测拉起 `MarkdownPreviewScreen`。

    .venv/bin/python scripts/spike_preview_bitmap.py

期望（Tabby / Sixel）：

- 顶部横幅 `graphics=sixel cell=…`；
- 图片与块级公式是位图；图片透明区与背景同色（无黑框 / 异色框）；
- 缺失图片显示 `🖼 缺失（图片不存在）`，远程图片显示 `🖼 远程（不加载远程图片）`；
- `Esc` / `q` 退出，终端无残留。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception as exc:  # pragma: no cover - 缺可选依赖
    print(
        "缺少 Pillow：请先 `pip install graphical extra`\n"
        "  .venv/bin/pip install textual-image==0.14.2 pillow\n"
        f"原始错误：{exc}",
        file=sys.stderr,
    )
    raise SystemExit(2)

from textual.app import App

from myrtui.client.graphics import describe, detect
from myrtui.client.mdpreview import MarkdownPreviewScreen

DOC = """# v0.37 位图点验

段落一：下面应显示**位图**（不是 `🖼` 文本）。图里三块内容都是故意画的：

- 五条彩色方块 = 颜色是否正常；
- 中间**透明圆** = 透明像素 → 露出页面背景色（**应与周围背景同色、看不见**；
  如果看到黑圆/异色圆，说明背景合成错了）；
- 底部**半透明白椭圆** = 50% alpha 合成到背景后的灰（应能和周围背景区分）。

![示意图](pic.png)

段间公式（应为 LaTeX 位图，失败才回落 Unicode）：

$$
\\frac{a+b}{c} + \\sum_{i=1}^{n} i^2 = \\int_0^1 x\\,dx
$$

带**中文标签**的公式（LaTeX 后端会自动加载 CJK 字体，不应回落）：

$$
f(\\mathrm{lt}) = \\mathrm{lt} - \\underbrace{\\frac{|\\boldsymbol{x}_{\\mathrm{rx}}|}{c}}_{\\text{几何光行时}} - \\underbrace{\\Delta t_S(\\mathrm{lt})}_{\\text{Shapiro（§3.4）}} = 0 \\tag{7}
$$

段落二：句子里的行内公式 $x^2+y^2$ 仍是 Unicode 文本（不倾斜、不出位图）。

下面这段**只有公式**（`$...$` 独占一段）→ 应升级成块级位图：

$\\sigma_{\\mathrm{floor}} = \\frac{c}{2}\\sqrt{\\sigma_{\\mathrm{sta}}^2+\\sigma_{\\mathrm{meas}}^2}$

再往下两行是**回落示例**：它们不会显示图片，而是显示成
`🖼 名字（原因）` 的文本——这就是「alt 回落」。

![缺失](nope.png)

![远程](https://example.com/x.png)

段落三：滚动 / resize 后不应残影、吞字或错位。
"""


def _build_sample(root: Path) -> Path:
    """自建样例：背景 = 页面背景色，透明洞落在纯背景上（应看不见）。"""
    width, height = 640, 320
    background = (30, 30, 30, 255)  # = Textual dark 的 $surface，便于判断是否无缝
    image = Image.new("RGBA", (width, height), background)
    draw = ImageDraw.Draw(image)
    bars = [
        (235, 70, 70, 255),
        (70, 210, 120, 255),
        (70, 120, 235, 255),
        (245, 210, 70, 255),
        (210, 90, 230, 255),
    ]
    # 色条放在上半区，避免和透明洞 / 半透明形状重叠（避免把「洞」误读成图的一部分）
    for index, color in enumerate(bars):
        x = 20 + index * 122
        draw.rectangle((x, 56, x + 100, 130), fill=color)
    # 透明洞：完全落在纯背景上 → 预合成后应与背景**完全同色**（看不见）
    draw.ellipse((60, 150, 300, 300), fill=(0, 0, 0, 0))
    # 半透明白椭圆：合成后是中灰，和背景可区分
    draw.ellipse((360, 160, 620, 290), fill=(255, 255, 255, 90))
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 30)
    except OSError:
        font = ImageFont.load_default()
    draw.text((20, 16), "bars / transparent hole / 50% white", fill=(235, 235, 235, 255), font=font)
    image.save(root / "pic.png")

    doc = root / "preview.md"
    doc.write_text(DOC, encoding="utf-8")
    return doc


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
            "位图未启用（会看到 termtex / 原文 + alt）——检查：\n"
            "  1) 是否在支持 Sixel/TGP 的终端（Tabby 需开启 Sixel 开关）；\n"
            "  2) 是否装了 textual-image（.venv/bin/pip install textual-image pillow）；\n"
            "  3) 外层 tmux 是否配了 `set -ga terminal-features ',*:sixel'`；\n"
            "  4) MYRTUI_GRAPHICS 是否被设成 unicode / off。",
            file=sys.stderr,
        )

    root = Path(tempfile.mkdtemp(prefix="myrtui-v37-preview-"))
    doc = _build_sample(root)
    print(f"样例：{doc}", file=sys.stderr)
    Demo(doc, capabilities).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
