"""可拖拽分隔线（v0.11；v0.11.2 起不再承担焦点指示）。

分隔线本体只有 1 cell 厚，**恒为细线**：

- 按下拖动 → 调整相邻两块的比例（几何换算在 App 侧，本类只负责收事件）；
- 双击 → 该维度的比例复位默认；
- 指针靠近（热区 ±1）→ ``-near`` 提亮，告诉用户「这儿能拖」。

焦点视觉（v0.11.2 起）由 **PaneWidget 自己的边框**（``:focus`` → heavy 绿）
与侧栏聚焦的底色（v0.11.3 起**按半区**：``#file-tree:focus-within`` /
``#git-area:focus-within``）承担——分隔线是**整条** widget，跨出焦点
pane 的边界（sep-rows 全宽 / sep-main 全高），拿它画焦点框必然溢出到相邻
pane 旁边；侧栏聚焦则明确不需要绿框（底色变化即够）。

**为什么自己写而不用 Textual 的 ``Rule``**：

1. ``Rule`` 的 ``DEFAULT_CSS`` 带 ``margin: 0 2`` / ``1 0``——在比例换算里
   会凭空吃掉 4 个 cell，分隔线就不在「两块区域的边界」上了；
2. ``Rule`` 靠覆写 ``render()`` 画线，而 Widget 把 visual 缓存在
   ``_layout_cache`` 里，改样式后必须显式 ``refresh()`` 才会重画；本类用
   reactive（``repaint=True`` → 内部 ``refresh`` → 缓存失效）自动兜住。

**事件不 stop**：按下 / 移动 / 释放都继续冒泡到 App 层——App 层的
``_mouse_down_pane`` 记账必须保持一致（跨 pane 拖动时鼠标经过分隔线，
若被 stop 掉，pane 内的拖动就会断）。
"""
from __future__ import annotations

from textual.reactive import Reactive, reactive
from textual.widget import Widget

from myrtui.i18n import t

# 细线字形（box drawing，1 cell 宽）
THIN_GLYPHS = {"vertical": "│", "horizontal": "─"}

# 拖拽可用的鼠标按钮（v0.11.5）：左键 + 右键。
# v0.11.3 曾收口为只认左键（右键拖拽「不符合直觉」），但用户真机定位出特殊
# 环境——UU 远程等嵌入 tmux 的终端里左键事件不可达而右键可达——故恢复双键。
# 中键仍不参与（避免与「中美键拖动」类软件行为冲突）。
DRAG_BUTTONS = (1, 3)  # Textual：1 = 左键，3 = 右键


class Separator(Widget):
    """1 cell 厚的分隔线（可拖动 / 双击复位）。"""

    # 拖分隔线不是「拖选文字」——否则按下就把选区锚在了线上
    ALLOW_SELECT = False

    DEFAULT_CSS = """
    Separator {
        width: 1;
        height: 1fr;
        color: $text-disabled;
    }
    Separator.-horizontal {
        width: 1fr;
        height: 1;
    }
    Separator.-near {
        color: $text-muted;
    }
    """

    near: Reactive[bool] = reactive(False, toggle_class="-near")
    """指针在命中区附近 → 提亮（告诉用户「这儿能拖」）。"""

    def __init__(self, orientation: str = "vertical", **kwargs):
        kwargs.setdefault("markup", False)  # 线是纯字符，别让 markup 解析器插手
        super().__init__(**kwargs)
        if orientation not in THIN_GLYPHS:
            raise ValueError(t("separator.unknown_orientation", orientation=orientation))
        self.orientation = orientation
        self.add_class("-horizontal" if orientation == "horizontal" else "-vertical")

    # ---------- 绘制 ----------

    def render(self) -> str:
        """画线：竖线按高度逐行 ``│``，横线按宽度铺 ``─``。

        内容随尺寸变 → Textual 在 ``_size_updated`` 里清 ``_layout_cache``，
        缓存失效后自动重画（不需要手动 refresh）。
        """
        width, height = self.content_size
        return self.glyph_text(width, height)

    def glyph_text(self, width: int, height: int) -> str:
        """给定尺寸 → 线字符（拆出来是为了能直接单测，不依赖挂载）。"""
        glyph = THIN_GLYPHS[self.orientation]
        if self.orientation == "vertical":
            return "\n".join([glyph] * max(1, height))
        return glyph * max(1, width)

    # ---------- 鼠标 ----------

    def on_mouse_down(self, event) -> None:
        app = self.app
        if hasattr(app, "on_separator_mouse_down"):
            app.on_separator_mouse_down(self, event)

    def on_mouse_move(self, event) -> None:
        app = self.app
        if hasattr(app, "on_separator_mouse_move"):
            app.on_separator_mouse_move(self, event)

    def on_mouse_up(self, event) -> None:
        app = self.app
        if hasattr(app, "on_separator_mouse_up"):
            app.on_separator_mouse_up(self, event)

    def on_click(self, event) -> None:
        """双击 → 该维度比例复位默认（单击不动比例）。左/右键均可（v0.11.5）。"""
        if getattr(event, "button", 1) not in DRAG_BUTTONS:
            return
        if getattr(event, "chain", 1) < 2:
            return
        app = self.app
        if hasattr(app, "reset_layout_dimension"):
            app.reset_layout_dimension(self)
