"""SGR 解析：capture -e 输出 → rich.text.Text（DESIGN.md §4.3）。

要点：
- SGR 直接映射为 hex/RGB 的 Style，不用 ANSI 颜色名，
  避免 Textual ANIToTruecolor 过滤器二次改写（第一轮 C9.2）；
- 宽字符/组合字符按单元格宽度处理，裁切/补齐一律用 cell_len（N9）。

v0.13：pane 内**允许** Textual 鼠标选区（拖拽选字），并**禁用**
Textual 默认的双击/三击全选（见 ``PaneWidget._on_click``）。

v0.35：**左键拖拽永远归 myrtui 选字**（默认 ``MYRTUI_DRAG_SELECT=always``）
——不管 pane 内程序是否请求鼠标，拖拽都产生本地选区，松手后由 App 自动
复制（OSC 52 + 系统命令）。单击 / 滚轮 / 右键 / 中键仍按旧规则转给程序：
左键按下不注入，只有「松手且判定为点击」时才由 App 合成 press+release。
``MYRTUI_DRAG_SELECT=app`` 可恢复 v0.34 的旧门控（程序抢鼠标时拖拽转发）。
"""
from __future__ import annotations

import re

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual import events
from textual.selection import Selection
from textual.widgets import Static

from myrtui.mouse import LEVEL_NONE

_SGR_RE = re.compile(r"\x1b\[([0-9;:]*)m")

# 标准 16 色 → hex（xterm 默认调色板）
_ANSI16 = [
    "#000000", "#cd0000", "#00cd00", "#cdcd00",
    "#0000ee", "#cd00cd", "#00cdcd", "#e5e5e5",
    "#7f7f7f", "#ff0000", "#00ff00", "#ffff00",
    "#5c5cff", "#ff00ff", "#00ffff", "#ffffff",
]


def _xterm256(n: int) -> str:
    if n < 16:
        return _ANSI16[n]
    if n < 232:
        n -= 16
        r, g, b = n // 36, (n // 6) % 6, n % 6
        conv = lambda v: 0 if v == 0 else 55 + v * 40  # noqa: E731
        return f"#{conv(r):02x}{conv(g):02x}{conv(b):02x}"
    v = 8 + (n - 232) * 10
    return f"#{v:02x}{v:02x}{v:02x}"


class _SGRState:
    __slots__ = ("fg", "bg", "bold", "italic", "underline", "reverse", "dim")

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.fg: str | None = None
        self.bg: str | None = None
        self.bold = False
        self.italic = False
        self.underline = False
        self.reverse = False
        self.dim = False

    def style(self) -> Style:
        return Style(
            color=self.fg,
            bgcolor=self.bg,
            bold=self.bold,
            italic=self.italic,
            underline=self.underline,
            reverse=self.reverse,
            dim=self.dim,
        )

    def apply(self, params: str) -> None:
        codes = [int(p) if p else 0 for p in params.replace(":", ";").split(";")]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                self.reset()
            elif c == 1:
                self.bold = True
            elif c == 2:
                self.dim = True
            elif c == 3:
                self.italic = True
            elif c == 4:
                self.underline = True
            elif c == 7:
                self.reverse = True
            elif c == 22:
                self.bold = self.dim = False
            elif c == 23:
                self.italic = False
            elif c == 24:
                self.underline = False
            elif c == 27:
                self.reverse = False
            elif 30 <= c <= 37:
                self.fg = _ANSI16[c - 30]
            elif c == 39:
                self.fg = None
            elif 40 <= c <= 47:
                self.bg = _ANSI16[c - 40]
            elif c == 49:
                self.bg = None
            elif 90 <= c <= 97:
                self.fg = _ANSI16[c - 90 + 8]
            elif 100 <= c <= 107:
                self.bg = _ANSI16[c - 100 + 8]
            elif c in (38, 48):
                color, consumed = _ext_color(codes, i + 1)
                if color is not None:
                    if c == 38:
                        self.fg = color
                    else:
                        self.bg = color
                i += consumed
            i += 1


def _ext_color(codes: list[int], start: int) -> tuple[str | None, int]:
    """解析 38/48 的扩展形式，返回 (颜色, 消耗的参数个数)。"""
    if start >= len(codes):
        return None, 0
    if codes[start] == 5:  # 256 色
        if start + 1 >= len(codes):
            return None, 1
        return _xterm256(codes[start + 1]), 2
    if codes[start] == 2:  # 真彩
        if start + 3 >= len(codes):
            return None, 1
        r, g, b = codes[start + 1 : start + 4]
        return f"#{r:02x}{g:02x}{b:02x}", 4
    return None, 1


def parse_sgr_line(line: str, width: int) -> Text:
    """把一行带 SGR 的 capture 输出解析为定宽 rich Text。

    按 cell_len 裁切/补齐到 width（N9：不用 len()）。
    """
    text = Text(no_wrap=True, end="")
    state = _SGRState()
    pos = 0
    for m in _SGR_RE.finditer(line):
        chunk = line[pos : m.start()]
        if chunk:
            text.append(chunk, state.style())
        state.apply(m.group(1))
        pos = m.end()
    tail = line[pos:]
    if tail:
        text.append(tail, state.style())

    # 定宽：裁切/补齐一律按单元格宽度（Rich truncate 原地生效，pad=True 补齐）
    length = cell_len(text.plain)
    if length > width:
        text.truncate(width)
    elif length < width:
        text.pad_right(width - length)
    return text


def _char_index_at_cell(plain: str, cell_x: int) -> int:
    """给定单元格列，返回 plain 中对应的字符下标（宽字符感知，N9）。"""
    cell = 0
    for i, ch in enumerate(plain):
        if cell >= cell_x:
            return i
        cell += cell_len(ch)
    return len(plain)


class PaneWidget(Static, can_focus=True):
    """单个终端 pane：渲染 capture 输出、块状光标、滚轮翻 scrollback。

    - **选区**（v0.13；v0.35 改为「拖拽永远选字」）：``allow_select`` 在默认
      ``always`` 模式下**恒为 True**——不管 pane 内程序是否请求鼠标，左键拖拽
      都由 myrtui 做本地选区、松手自动复制；单击 / 滚轮 / 右键 / 中键仍按旧
      门控转发。``MYRTUI_DRAG_SELECT=app`` 时恢复 v0.34 的门控（程序未请求
      鼠标才允许选区）；
    - **双击不选中**（v0.13）：覆写 ``_on_click`` 抑制 Textual 默认的
      双击/三击全选，双击仍只是聚焦 pane；
    - 滚轮事件处理后 event.stop()，防止冒泡被祖先容器吃掉（N19）；
    - 块状光标仅在实时视图（offset == 0）绘制（M12）；
    - **永久 1 cell 边框**（v0.11.2）：默认细灰（solid），聚焦时
      ``:focus`` 切 **heavy + 绿（$success）**——即「焦点 pane 边框加粗变绿」。
      边框**恒在**（不随焦点增减）：border 类型切换宽度不变 → 切焦点零几何
      位移 → 不触发 tmux resize 风暴；若聚焦才画边框，内容区会随焦点来回
      缩放 2 cell，内层程序（vim 等）会被迫重排。
    """

    # Textual 8.x 的焦点开关是**类关键字** can_focus=True（见上面的类头）；
    # 写成类属性 ``CAN_FOCUS = True`` 是**空操作**（8.x 已无该属性，
    # __init_subclass__ 只认 can_focus 关键字）→ pane 永远不可聚焦，
    # 「点击 pane → 焦点回 pane」与 P3 的焦点模型（D8）都落空。
    # P3 顺带修掉（P3 方案 §14.3 修订记录 #10）。

    # 类级开关：**原则上**允许文本选区；真正的门控在下面的 ``allow_select``
    # property（按 pane 内程序是否请求鼠标动态决定）。v0.13 起不再是 False。
    ALLOW_SELECT = True

    DEFAULT_CSS = """
    PaneWidget {
        /* 不用 $text-disabled：它的值是 `auto 38%`（两个 token），border 简写
           只收单 token 颜色（hex/RGB/ANSI）→ 解析直接报错（实测）。#6c6c6c
           即 auto 38% 的近似暗灰。 */
        border: solid #6c6c6c;
    }
    PaneWidget:focus {
        border: heavy $success;
    }
    """

    def __init__(self, pane_id: str, **kwargs):
        super().__init__(**kwargs)
        self.pane_id = pane_id
        # 滚动偏移由 App._offsets（per-pane 内存状态，§11）统一管理
        self._lines: list[Text] = []
        self._cursor: tuple[int, int] | None = None  # (x, y) 仅实时视图
        self._dead = False

    # ---------- 选区（v0.13） ----------

    @property
    def allow_select(self) -> bool:
        """Textual 是否允许在本控件上起选区（v0.35：默认恒允许）。

        规则：

        - ``MYRTUI_DRAG_SELECT=always``（默认）→ **恒 True**。拖拽永远由
          myrtui 做本地选区；程序请求了鼠标也不影响（单击 / 滚轮 / 右键 /
          中键仍走 §10.2 的转发门控）；
        - ``MYRTUI_DRAG_SELECT=app`` → 恢复 v0.34 门控：pane 内程序**未**
          请求鼠标（``LEVEL_NONE``）才允许选区；
        - 未挂载 / 无 app 上下文 → ``False``（不猜）。

        数据源：``App._mouse_levels``（每 tick 从 ``list-panes`` 的
        ``mouse_*_flag`` 刷新，app.py §_poll）。
        """
        try:
            app = self.app
        except Exception:  # 未挂载 / 无活动 app → 不允许
            return False
        if getattr(app, "_drag_select_mode", "always") != "app":
            return True
        levels = getattr(app, "_mouse_levels", None)
        if not isinstance(levels, dict):
            return False
        return levels.get(self.pane_id, LEVEL_NONE) == LEVEL_NONE

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """选区提取：逐行裁掉行尾补白（v0.35）。

        ``parse_sgr_line`` 会把每行补齐到 pane 宽度（``pad_right``），直接复制
        会带上大量尾随空格。只在这里（复制路径）裁剪；屏幕渲染仍保留 pad，
        否则光标 / 背景会被破坏。

        中文 / 宽字符不需要额外映射：Selection 的偏移本来就是 **cell-aware
        的字符偏移**（Textual 的 ``get_widget_and_offset_at`` 按
        ``get_character_cell_size`` 累加），``Widget.get_selection`` 能正确
        extract。
        """
        got = super().get_selection(selection)
        if got is None:
            return None
        text, ending = got
        text = "\n".join(line.rstrip() for line in text.split("\n"))
        return text, ending

    async def _on_click(self, event: events.Click) -> None:
        """**刻意不执行基类的双击/三击全选**（v0.13 决策：不要）。

        基类 ``Widget._on_click``（textual/widget.py:4695-4702）在
        ``event.chain == 2/3`` 时调 ``text_select_all()`` → 整个 pane 文本
        被选中。pane 内我们只要**拖拽选字**，双击仍只是聚焦 pane。

        实现要点：``event.prevent_default()`` 的官方语义就是「阻止基类
        handler 被调用」（textual/message.py:131-139 → ``_no_default_action``
        → ``_get_dispatch_methods`` 的 break，textual/message_pump.py:758-760）
        → 基类 ``_on_click`` 根本不会执行；基类最后一步的 broker 我们自己
        补上，保持事件流与基类一致（DOM 冒泡不受影响——冒泡看的是
        ``_stop_propagation``，不是 ``_no_default_action``）。
        """
        event.prevent_default()
        await self.broker_event("click", event)

    def set_content(
        self,
        lines: list[Text],
        cursor: tuple[int, int] | None,
        dead: bool,
    ) -> None:
        """内容更新（调用方已做防闪烁 diff）。

        用 ``update()`` 而非覆写 ``render()``：Static 把 visual 缓存在
        ``_layout_cache``，覆写 render 不会失效该缓存 → 屏幕不刷新。
        """
        self._lines = lines
        self._cursor = cursor
        self._dead = dead
        self.update(self._compose_text())
        self.set_class(dead, "dead")

    def _compose_text(self) -> Text:
        if not self._lines:
            return Text("")
        out = Text(no_wrap=True, end="")
        cursor_y = self._cursor[1] if self._cursor is not None else -1
        cursor_x = self._cursor[0] if self._cursor is not None else -1
        for y, line in enumerate(self._lines):
            if y == cursor_y and 0 <= cursor_x:
                # 块状光标：反色该单元格（tmux 不提供光标形状，块状是唯一选择，M12）
                line = line.copy()
                idx = _char_index_at_cell(line.plain, cursor_x)
                if idx < len(line.plain):
                    line.stylize(Style(reverse=True), idx, idx + 1)
            out.append(line)
            out.append("\n")
        if self._dead:
            # dead pane 灰置（§3.1：前端标灰该 pane）
            out.stylize(Style(dim=True))
        return out

    # ---------- 鼠标 ----------

    def on_mouse_scroll_up(self, event) -> None:
        event.stop()
        app = self.app
        if hasattr(app, "on_pane_wheel"):
            # 程序请求了鼠标 → 转发；否则走 scrollback（§11，含备用屏忽略）
            app.on_pane_wheel(self, event, +1)
            return
        if hasattr(app, "scroll_pane"):
            app.scroll_pane(self.pane_id, +1)

    def on_mouse_scroll_down(self, event) -> None:
        event.stop()
        app = self.app
        if hasattr(app, "on_pane_wheel"):
            app.on_pane_wheel(self, event, -1)
            return
        if hasattr(app, "scroll_pane"):
            app.scroll_pane(self.pane_id, -1)

    def _clear_textual_selection(self) -> None:
        """清掉 Textual 在 MouseDown 时可能顺手建立的选区（v0.35）。

        分隔线热区 / 右键 / 中键按下时 Screen 层已经先建了选区（``allow_select``
        现在恒 True），这些路径不该留下高亮。
        """
        try:
            self.screen.clear_selection()
        except Exception:  # 未挂载 / teardown 竞态
            pass

    def on_mouse_down(self, event) -> None:
        app = self.app
        # 分隔线热区：按下落在分隔线附近（±1 cell）→ 拖分隔线而不是点 pane。
        # pane 内的按下会被本方法 stop 掉，App 层的全局 on_mouse_down 收不到，
        # 所以热区判定必须在这里也做一次。
        if hasattr(app, "press_near_separator") and app.press_near_separator(event):
            # Screen 层可能已按 allow_select 建立了选区 → 清掉（v0.35）
            self._clear_textual_selection()
            # 焦点仍切到本 pane（键盘透传不中断），但按下**不**注入内层 shell
            if hasattr(app, "focus_pane"):
                app.focus_pane(self.pane_id)
            return
        if int(event.button) != 1:
            # 右键 / 中键：转发按下时先清掉 Textual 顺手建立的选区（v0.35）
            self._clear_textual_selection()
        event.stop()
        if hasattr(app, "focus_pane"):
            app.focus_pane(self.pane_id)  # 点击 pane → 切焦点（§10.2）
        if hasattr(app, "on_pane_mouse_down"):
            app.on_pane_mouse_down(self, event)
        self.focus()

    def on_mouse_up(self, event) -> None:
        # 释放可能落在别的 widget 上（App.on_mouse_up 兜底）；落在本 pane 时
        # 必须 stop，避免 App 层二次转发。v0.35：统一走 on_pointer_up
        # （左键手势 → _finish_gesture；右键 / 中键 / app 模式 → _forward_release）
        event.stop()
        app = self.app
        handler = getattr(app, "on_pointer_up", None)
        if handler is not None:
            handler(event)
        elif hasattr(app, "_forward_release"):
            app._forward_release(event)

    def on_mouse_move(self, event) -> None:
        # 恒 stop：拖动/hover 都在本方法路由，App 层只处理离开 pane 区域的拖动
        event.stop()
        app = self.app
        if hasattr(app, "on_pane_mouse_move"):
            app.on_pane_mouse_move(self, event)

    def on_resize(self, event) -> None:
        """resize 双向联动：Textual widget 尺寸 → resize-window（N7）。

        v0.11.2：pane 有了 1 cell 永久边框后，tmux 窗口要跟的是**内容区**
        （``content_size`` = 外框 − border）；``event.size`` 是外框尺寸，直接
        发会让 tmux 窗口比内容区大 2×2 → capture 回来的内容被边框盖住两行
        两列（右边/下边溢出被裁）。
        """
        app = self.app
        if hasattr(app, "resize_pane"):
            size = self.content_size
            app.resize_pane(self.pane_id, size.width, size.height)
