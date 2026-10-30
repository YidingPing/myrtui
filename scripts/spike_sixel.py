#!/usr/bin/env python3
"""临时 Sixel Spike（v0.37 前置验证，验证后可删）。

用途：在 **Tabby** 里确认 Textual + `textual-image` 的 Sixel 是否稳定：
颜色 / 透明 / 滚动 / resize / 重绘是否有残影或错位。

运行前（只需开发环境）：

    .venv/bin/pip install textual-image==0.14.2 pillow
    .venv/bin/python scripts/spike_sixel.py

请在 **Tabby 的普通 shell**里直接运行（不要经过本仓库的 pane 抓屏链路）；
如果在外层 tmux 里，先看 `tmux show -gv allow-passthrough` 与
`terminal-features` 是否认识 Sixel。

观察项见内部图形方案（V37）§3。第 1/3 张是原始 RGBA，
第 2/4 张已预合成到背景色 `#808080`：预合成版的透明洞应与画布完全同色
（看不出圆），原始版会露出终端自己的填充色。若对比成立，说明正式实现必须
自己 flatten alpha（Tabby 的 Sixel 插件不做 P2=1 透明合成）。
"""
from __future__ import annotations

import os
import sys

try:
    from PIL import Image, ImageDraw, ImageFont
    from textual_image._terminal import probe_terminal
    from textual_image.widget import SixelImage
except Exception as exc:  # pragma: no cover - 只在缺依赖时触发
    print(
        "缺少依赖：请先运行\n"
        "  .venv/bin/pip install textual-image==0.14.2 pillow\n"
        f"原始错误：{exc}",
        file=sys.stderr,
    )
    raise SystemExit(2)

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Static


def _font(size: int):
    for path in (
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


BG = (128, 128, 128, 255)
"""对照组用的「主题背景色」（= CSS 里 SixelImage 的 background）。

用中灰是为了让「终端自己的透明填充色」（浅灰/黑/白）一眼可辨。
Tabby 的 xterm Sixel 插件不实现 P2=1（「0 位保留原像素」）的透明语义；
所以正式实现要在编码前把 alpha 预合成到背景色。
"""


def _make_image(index: int, *, flatten: bool) -> Image.Image:
    """一张带色条、文字、透明洞和棋盘格的小图。

    ``flatten=True``：模拟正式实现的修法——先把 RGBA 预合成到 ``BG``，
    输出无 alpha 的 RGB（不触发 P2=1 透明模式）。
    """
    width, height = 520, 260
    # 画布 = 背景色：预合成版的透明洞应与画布**完全同色**（看不出圆），
    # 原始 RGBA 版则会露出终端自己的透明填充（浅灰/黑）。
    image = Image.new("RGBA", (width, height), BG)
    draw = ImageDraw.Draw(image)

    # 色条：检查 Sixel 调色板 / 颜色顺序
    colors = [
        (235, 70, 70, 255),
        (70, 210, 120, 255),
        (70, 120, 235, 255),
        (245, 210, 70, 255),
        (210, 90, 230, 255),
    ]
    for i, color in enumerate(colors):
        x = 20 + i * 96
        draw.rectangle((x, 70, x + 80, 120), fill=color)

    # 透明洞：**只落在纯画布上**（不跨色条），预合成后应与画布完全同色。
    # 注意：图里**只放这一个圆**，避免半透明描边等其他圆混淆判定。
    draw.ellipse((300, 140, 470, 250), fill=(0, 0, 0, 0))
    draw.text(
        (300, 112),
        "hole below",
        fill=(245, 245, 245, 255),
        font=_font(18),
    )

    # 棋盘格：检查缩放 / 每格颜色
    cell = 12
    for y in range(190, 240, cell):
        for x in range(150, 290, cell):
            if ((x // cell) + (y // cell)) % 2 == 0:
                draw.rectangle((x, y, x + cell - 1, y + cell - 1), fill=(220, 220, 220, 255))

    draw.text((20, 18), f"SIXEL {index + 1}", fill=(245, 245, 245, 255), font=_font(48))
    draw.text(
        (300, 20),
        "RAW" if not flatten else "FLATTENED",
        fill=(245, 245, 245, 255),
        font=_font(28),
    )
    if flatten:
        base = Image.new("RGBA", image.size, BG)
        return Image.alpha_composite(base, image).convert("RGB")
    return image


class SpikeApp(App):
    """四张图放进可滚动容器，专门看滚动时的 Sixel 重绘。"""

    CSS = """
    Screen {
        layout: vertical;
        background: $surface;
    }
    #spike-header {
        height: 2;
        padding: 0 1;
        background: $panel;
        color: $text;
    }
    #spike-scroll {
        height: 1fr;
        padding: 0 1;
    }
    Image {
        width: 64;
        height: auto;
        margin: 1 2;
        /* 与脚本里的 BG 一致：透明洞「应该」露出的颜色。
           注意：textual-image 的类名是 Image，SixelImage 只是导出别名，
           CSS 选择器必须写 Image，否则宽度/背景都不生效。 */
        background: #808080;
    }
    .spike-caption {
        padding: 0 2;
        color: $text-muted;
    }
    #spike-hint {
        padding: 1 2;
        color: $text-muted;
    }
    #spike-footer {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("q", "quit", show=False),
        Binding("escape", "quit", show=False),
    ]

    def __init__(self, capabilities) -> None:
        super().__init__()
        self._capabilities = capabilities
        # 两张原始 RGBA、两张预合成：对比透明洞的颜色
        self._images = [
            _make_image(0, flatten=False),
            _make_image(1, flatten=True),
            _make_image(2, flatten=False),
            _make_image(3, flatten=True),
        ]
        self._captions = [
            "原始 RGBA：透明洞会露出终端自己的填充（浅灰/黑）→ 圆可见",
            "已预合成到画布色 #808080：洞应与画布完全同色（看不出圆）",
            "原始 RGBA：透明洞会露出终端自己的填充（浅灰/黑）→ 圆可见",
            "已预合成到画布色 #808080：洞应与画布完全同色（看不出圆）",
        ]

    def compose(self) -> ComposeResult:
        cap = self._capabilities
        cell = cap.cell_size
        header = (
            f"sixel={cap.sixel}  tgp={cap.tgp}  cell={cell.width}x{cell.height}px  "
            f"TERM={os.environ.get('TERM', '?')}  "
            f"TMUX={'yes' if os.environ.get('TMUX') else 'no'}"
        )
        if not cap.sixel:
            header += "  [探测未命中：若当前就在 Tabby，请记录此现象]"
        yield Static(header, id="spike-header", markup=False)
        with VerticalScroll(id="spike-scroll"):
            for index, image in enumerate(self._images):
                widget = SixelImage(image, id=f"spike-image-{index}")
                # 内联设置，避免 CSS 类型选择器解析问题；也是正式实现的做法：
                # 「预合成用的颜色」必须与控件实际背景一致。
                widget.styles.background = "#808080"
                yield widget
                yield Static(
                    f"— 第 {index + 1} 张：{self._captions[index]} —",
                    classes="spike-caption",
                    markup=False,
                )
            yield Static(
                "检查：颜色是否正确、透明洞是否露出背景、滚动时是否残影/错位、"
                "resize 后是否错位；有任何异常请记录窗口大小与所在终端。",
                id="spike-hint",
                markup=False,
            )
        yield Static(
            "↑↓ / PgUp/PgDn / 滚轮滚动 · q / Esc 退出",
            id="spike-footer",
            markup=False,
        )

    def action_quit(self) -> None:
        self.exit()


def main() -> int:
    try:
        capabilities = probe_terminal()
    except Exception as exc:  # pragma: no cover - 只在非 tty / 探测失败时
        print(f"terminal probe failed: {exc}", file=sys.stderr)
        return 2
    SpikeApp(capabilities).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
