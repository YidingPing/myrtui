"""Textual 前端 App（DESIGN.md §4/§6/§8/§10/§11/§12）。

职责：布局（P1：1..4 布局档）、Ctrl-a 前缀状态机、键鼠路由、自适应轮询、
防闪烁 diff、resize 双向联动、接管哨戒（§6 P1）、侧栏 v2（P3：文件树 +
Git 历史）。唯一有状态的是 tmux，前端无共享可变状态（§4.1）。
"""
from __future__ import annotations

import asyncio
import functools
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from rich.control import Control
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.events import Key, MouseDown, MouseMove, MouseUp, Paste
from textual.geometry import Offset
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Label

from myrtui import gitlog, layout_state, pane_commands
from myrtui.cli import SENTRY_INTERVAL, is_marker_valid, read_takeover_markers
from myrtui.client import clipboard
from myrtui.client import graphics as graphics_mod
from myrtui.client.context_menu import ContextMenuScreen
from myrtui.client.file_tree import FileTree
from myrtui.client.git_panel import CommitCard, GitLogPanel
from myrtui.client.pane_widget import PaneWidget, parse_sgr_line
from myrtui.client.separators import DRAG_BUTTONS, Separator
from myrtui.client.shortcuts import ShortcutHelp, help_width
from myrtui.client.sidebar import Sidebar
from myrtui.client.topbar import TopBar, TopBarButton, PaneTab
from myrtui.mouse import (
    LEVEL_NONE,
    can_forward,
    decode_level,
    motion,
    press,
    release,
    wheel,
)
from myrtui.i18n import t
from myrtui.tmux_backend import CaptureResult, PaneInfo, TmuxBackend, TmuxError

import os as _os
import tempfile as _tempfile

log = logging.getLogger("myrtui.app")

# ── 鼠标诊断日志（v0.11.4，改动 B） ──────────────────────────────────
# ``MYRTUI_MOUSE_DEBUG=1`` 启动时，把 App 收到的每个鼠标事件与拖拽结论
# 追加写到临时日志，定位「某终端/SSH 链路下拖拽失效」用。默认关闭，
# 未开启时每次调用近零开销（一个 bool 判断）。
_MOUSE_DEBUG = _os.environ.get("MYRTUI_MOUSE_DEBUG") == "1"
_MOUSE_DEBUG_PATH = (
    _os.path.join(
        _tempfile.gettempdir(), f"myrtui-mouse-debug-{_os.getpid()}.log"
    )
    if _MOUSE_DEBUG
    else None
)


def _mouse_debug(msg: str) -> None:
    """追加一条鼠标诊断日志（仅 ``MYRTUI_MOUSE_DEBUG=1`` 时生效）。"""
    if not _MOUSE_DEBUG:
        return
    try:
        with open(_MOUSE_DEBUG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.time():.3f} {msg}\n")
    except OSError:
        pass  # 日志写不进去绝不影响主流程


# 轮询参数（§8）
IDLE_INTERVAL = 0.15  # 空闲 100–250ms 区间内取值（默认；B0 起可被
# MYRTUI_IDLE_INTERVAL 覆盖——多实例时给后台/次要实例降频，见 resolve_idle_interval）
IDLE_INTERVAL_MIN = 0.05  # B0 下界：0.03 级手滑值 = 33 fork/s 高频，与空闲降耗
# 初衷相悖，抬高一档（PERF-B0B1-PLAN §2.1）
IDLE_INTERVAL_MAX = 5.0  # B0 上界：防手滑打错量级
HIGH_FREQ_TICKS = 3  # 输入后保持 30ms 高频的 tick 数
TICK = 0.03
SCROLL_STEP = 3  # 滚轮步进 3 行（§11）
PREFIX_TIMEOUT = 1.0  # Ctrl-a 前缀超时（§10.1）
HELP_SHOW_DELAY = 0.15  # v0.22：悬停 `?` 多久弹出快捷键浮层（太短扫过即闪）
HELP_HIDE_GRACE = 0.25  # v0.22：离开 `?` 的宽限，留给「移进浮层继续读」
FAIL_LIMIT = 5  # 连续 N 次 tick capture 失败 → 提示失去连接（§9）
RECONNECT_NOTICE = 2.0  # P2 断线重连：恢复后「已重新连接」驻留时长（秒）
NOTICE_SECONDS = 2.0  # v0.13 顶栏瞬时提示（复制结果等）驻留时长（秒）
# ── v0.35 拖拽选字 / 松手自动复制 ─────────────────────────────────────
DRAG_THRESHOLD_CELLS = 2  # 指针移动 ≥ 2 格才算拖拽（1 格内容忍手抖，仍按点击）
THAW_DELAY = 0.6  # 自动复制后高亮保留时长（秒），让用户看清复制了什么
HOLD_TIMEOUT = 10.0  # MYRTUI_COPY_ON_SELECT=0 时等待 Ctrl-a c 的上限（秒）
GESTURE_TIMEOUT = 30.0  # 手势安全超时：mouse-up 丢失时不永久冻结（秒）
# v0.35：SSH 会话的粘贴提示。SSH 只转发 TERM 等少量环境变量，`TERM_PROGRAM`
# / `WT_SESSION` 通常拿不到 → 无法判断外层客户端，并列给出三种常见按键，
# 避免像 v0.35 初版那样只写 `Ctrl+Shift+V` 误导 macOS / Windows Terminal 用户。
def ssh_paste_hint() -> str:
    """SSH 会话的粘贴按键提示（渲染时求值：模块级 t() 会固化语言）。"""
    return t("app.ssh_paste_hint")
MOTION_INTERVAL = 0.03  # v0.7 鼠标 motion 节流：≥30ms 一条（1003 hover 下
# 每次移动一次 exec 不可承受 → 尾部合并，只保留最新位置）
GITLOG_INTERVAL = 3.0  # P3：当前目标的 git log 刷新节奏（D7：3s 足够及时，
# 且与 1s 的状态摘要解耦——一次 git log -n 50 在大仓库也要几十 ms）
LOOP_PROBE_INTERVAL = 0.025  # MYRTUI_LOOP_DEBUG 遥测探针节拍（§4.2）
LOOP_DEBUG_THRESHOLD = 0.03  # 迟到超过该值才记录（§1-1 目标线 30ms）

# §12 P1：1..4 布局档（v0.12 列优先：全宽 / 上下分 / 左2右1 / 2×2）
MAX_PANES = 4

# v0.28 查看 / 打开（v0.31 打开）：当前 pane 必须停在 shell 提示符才可注入命令。
# tmux ``pane_current_command`` 只给进程名（如 ``zsh`` / ``opencode`` / ``node``），
# 这里用常见 shell 白名单 + ``$SHELL`` 的 basename（见 ``__init__`` 的 ``_shell_names``）。
SHELL_COMMANDS = frozenset(
    {
        "sh", "ash", "bash", "zsh", "fish", "dash", "ksh", "ksh93", "mksh",
        "pdksh", "csh", "tcsh", "yash", "nu", "nushell", "xonsh", "elvish",
        "oil", "osh", "ys", "ion", "pwsh", "powershell",
    }
)

# ── v0.11 分隔线与比例 ──────────────────────────────────────────────
# 四个比例维度（见 layout_state.DIMENSIONS）：sidebar / tree / rows / cols。
# 分隔线 → (维度, 容器, 前块, 后块, 轴, 固定占用)
#   - 「轴」= 分割方向：x = 竖线（左右分），y = 横线（上下分）；
#   - 「固定占用」= 该容器里**不是 fr** 的兄弟节点占掉的 cell 数
#     （分隔线自己 1 个；侧栏那一条还要算上 1 行的 gitlog 标题）。
#
# **v0.12：pane 区改为列优先**（用户拍板「第二个 pane 默认在第一个**下面**」）：
#   - pane 区 = 左右两列（#col-a / #col-b），列内再上下分（pane-0/pane-1…）；
#   - ``cols`` 维度（左右）管**外层**那条竖线 sep-cols（只有 1 条）；
#   - ``rows`` 维度（上下）管**列内**那两条横线 sep-rows-a / sep-rows-b，
#     两条**共用同一份比例** → 2×2 时上下永远对齐（十字对齐不歪）。
# 维度名字与语义没变（rows=上下、cols=左右），只是「哪条线归哪个维度」换了。
SEPARATOR_SPECS: dict[str, tuple[str, str, str, str, str, int]] = {
    "sep-main": ("sidebar", "#main", "#sidebar", "#pane-container", "x", 1),
    "sep-tree": ("tree", "#sidebar", "#file-tree", "#git-area", "y", 1),
    "sep-cols": ("cols", "#pane-container", "#col-a", "#col-b", "x", 1),
    "sep-rows-a": ("rows", "#col-a", "#pane-0", "#pane-1", "y", 1),
    "sep-rows-b": ("rows", "#col-b", "#pane-2", "#pane-3", "y", 1),
}

# 分隔线的**命中区**半径（cell）：分隔线本体只有 1 cell 宽，终端按 cell 报
# 坐标，偏几格就落到旁边的 pane / 树上 → 手柄难点中（实测：偏 1 格拖不动）。
# 热区 = 线本身 ±1 格 → 实际可抓宽度 3 cell（v0.20 从 ±2 收回：滚动条归零后
# ±1 已够用，同时把树最右列的右键让给上下文菜单）。
SEPARATOR_GRAB_TOLERANCE = 1

# Textual key → tmux 键名（无前缀的一切键透传，DECCKM 由 tmux 自决，N2）
_SPECIAL_KEYS = {
    "enter": "Enter",
    "tab": "Tab",
    "shift+tab": "BTab",
    "backspace": "BSpace",
    "escape": "Escape",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "home": "Home",
    "end": "End",
    "pageup": "PageUp",
    "pagedown": "PageDown",
    "delete": "DC",
    "insert": "IC",
    "space": "Space",  # 仅供组合基键用（ctrl+space → C-Space）；裸 space 见 map_key
    "f1": "F1", "f2": "F2", "f3": "F3", "f4": "F4",
    "f5": "F5", "f6": "F6", "f7": "F7", "f8": "F8",
    "f9": "F9", "f10": "F10", "f11": "F11", "f12": "F12",
}

# Textual 修饰键前缀 → tmux 修饰符（前缀解析用，见 map_key）
_MOD_PREFIX = {"ctrl": "C", "alt": "M", "shift": "S"}


def resolve_idle_interval(raw: str | None) -> float:
    """解析 ``MYRTUI_IDLE_INTERVAL``（秒）→ 空闲轮询间隔（B0，PERF-B0B1-PLAN §2.1）。

    **在 ``TerminalApp.__init__`` 调用**（不是 import 时）：可单测、可
    monkeypatch env、随 app 实例走。口径：

    - 未设 / 空串 = 默认 ``IDLE_INTERVAL``；
    - 解析失败（``abc``）或 ≤ 0 → 警告 + 回落默认；
    - 合法值夹取 ``[IDLE_INTERVAL_MIN, IDLE_INTERVAL_MAX]``（下界防 33 fork/s
      高频手滑值，上界防打错量级），夹取时提示。
    """
    if raw is None or not raw.strip():
        return IDLE_INTERVAL
    try:
        value = float(raw)
    except ValueError:
        log.warning(
            "MYRTUI_IDLE_INTERVAL 非法（%r），回落默认 %.2fs", raw, IDLE_INTERVAL
        )
        return IDLE_INTERVAL
    if value <= 0:
        log.warning(
            "MYRTUI_IDLE_INTERVAL 必须 > 0（收到 %r），回落默认 %.2fs",
            raw, IDLE_INTERVAL,
        )
        return IDLE_INTERVAL
    clamped = min(IDLE_INTERVAL_MAX, max(IDLE_INTERVAL_MIN, value))
    if clamped != value:
        log.warning(
            "MYRTUI_IDLE_INTERVAL=%g 超出范围，夹取为 %.2fs（允许 %.2f..%.2f）",
            value, clamped, IDLE_INTERVAL_MIN, IDLE_INTERVAL_MAX,
        )
    return clamped


_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def resolve_copy_on_select(raw: str | None) -> bool:
    """解析 ``MYRTUI_COPY_ON_SELECT``（v0.35）→ 松手是否自动复制。

    口径与 ``resolve_idle_interval`` 一致：未设 / 空串 → 默认 **True**；
    ``0/false/no/off``（大小写不敏感）→ False；其他值（含非法值）→ 默认 True，
    **不报错**（这是体验开关，不是配置校验点）。
    """
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() not in _FALSE_VALUES


def resolve_drag_select(raw: str | None) -> str:
    """解析 ``MYRTUI_DRAG_SELECT``（v0.35）→ ``"always"`` / ``"app"``。

    - 未设 / 空串 / 非法值 → ``"always"``（默认：拖拽永远选字）；
    - ``app`` → 恢复 v0.34 行为（程序抢鼠标时拖拽转发给程序）。
    """
    if raw is None:
        return "always"
    value = raw.strip().lower()
    return value if value in ("always", "app") else "always"


def map_key(key: str, character: str | None = None) -> tuple[str, str] | None:
    """Textual key 名 → ("text", s) 或 ("keys", tmux 键名)；无法映射返回 None。

    - 可打印字符以 ``event.character`` 为准（Textual 对 ``-`` 报 key='minus'，
      只看 key 名会丢键）；
    - 但**修饰键组合必须先于 character 兜底**——否则 Alt+x 的 character 仍是
      可打印 'x'，会被误当普通字符发出；
    - 修饰键组合一律**前缀解析**：``alt+backspace`` → ``M-BSpace``、
      ``ctrl+left`` → ``C-Left``、``ctrl+shift+a`` → ``C-S-a``。
      旧实现只认长度 5/6，把 ``alt+up``（6 字符）等静默丢弃（P0 验收报告 §3-4）；
    - 裸 ``space`` 走文本路径（与历史行为一致，测试锚定）。
    """
    if key == "space":
        return "text", " "
    if key in _SPECIAL_KEYS:
        return "keys", _SPECIAL_KEYS[key]
    if len(key) == 1:
        return "text", key

    # 修饰键组合前缀解析（须在 character 兜底之前）
    parts = key.split("+")
    if len(parts) >= 2:
        mods = [_MOD_PREFIX[p] for p in parts[:-1] if p in _MOD_PREFIX]
        if mods and len(mods) == len(parts) - 1:
            base = parts[-1]
            base_name = _SPECIAL_KEYS.get(base) or (
                base if len(base) == 1 else None
            )
            if base_name is not None:
                return "keys", "-".join(mods + [base_name])

    if character and len(character) == 1 and character.isprintable():
        return "text", character
    return None


def _live_ids_of(panes: list[PaneInfo]) -> list[str]:
    """poll 返回的 panes → 槽位顺序 pane_id 列表（window_index 排序，截 4）。"""
    return [
        p.pane_id
        for p in sorted(panes, key=lambda x: x.window_index)
    ][:MAX_PANES]


@dataclass
class _PollOutcome:
    """``_poll_fetch``（线程段）→ ``_poll_apply``（循环段）的纯数据结果。

    对应旧 ``_poll`` 的两段式（PERF-B0B1-PLAN §3.2-1）：fetch 段在工作线程里
    跑 capture + TmuxError/漂移重试链（纯数据，不碰 DOM/前端状态），apply 段
    回到事件循环改状态/改 DOM。
    """

    panes: list[PaneInfo] | None = None
    captures: dict[str, CaptureResult] | None = None
    offsets: dict[str, tuple[int, int]] | None = None  # 本次 fetch 的请求快照（apply 段回写 offset 前比对用）
    probe: list[PaneInfo] | None = None  # 拓扑漂移探测到的新拓扑（None = 无漂移或探测失败）
    need_retry: bool = False  # 漂移重试（旧 _poll 的第二段；预算一轮，depth 封顶）
    error: str | None = None  # 最终失败 → 循环侧 warn + _poll_failed（§9 断线口径）


@dataclass
class _PointerGesture:
    """左键手势（v0.35）：按下 → （拖拽 → 自动复制）/（点击 → 合成 press+release）。

    坐标全部在**按下瞬间**快照：拖拽中拓扑 / 编码 / 修饰键变化不影响判定与
    点击合成（点击目标 = 按下点，与真实程序收到的语义一致）。
    """

    pane_id: str
    button: int
    down_x: int  # pane 内容坐标（不含边框）
    down_y: int
    down_screen_x: int
    down_screen_y: int
    level: int  # 按下时的 mouse level
    sgr: bool  # 按下时的编码位
    shift: bool
    meta: bool
    ctrl: bool
    dragging: bool = False


class MainScreen(Screen, inherit_bindings=False):
    """默认屏：**零框架键绑定**，tab/shift+tab/ctrl+c 一律透传给内层 shell（C9.1）。

    v0.15 修复锚点（用户反馈「按 Tab 焦点在 pane 之间跳，shell 补全没法用」）：
    本类曾经只写 ``BINDINGS = []``，**那删不掉继承来的绑定**——textual 8.2.8
    ``dom.py::_merge_bindings`` 是沿 MRO 逐基类合并，子类写空列表只表示
    「本类不贡献键」，基类的键原样保留。实测当时
    ``MainScreen._merged_bindings.key_to_bindings == ['ctrl+c', 'shift+tab', 'super+c', 'tab']``
    → 按键链路是 ``TerminalApp.on_key``（透传，先）之后 ``App._on_key →
    _check_bindings`` 命中 ``Screen`` 的 ``tab → app.focus_next`` → 焦点跳到
    文件树 / Git 历史（D8：侧栏聚焦后按键全被侧栏吃掉 → 补全失效）。
    ``inherit_bindings=False`` 是 Textual 的类关键字：``__init_subclass__``
    遇到本类时 ``clear()`` 之前累积的全部基类绑定 → screen 层真正零绑定。
    """

    BINDINGS = []  # 显式声明「本类无绑定」；真正生效的是上面的类关键字

    def get_selected_text(self) -> str | None:
        """取选区文本；v0.35 起把范围限制在**按下发生的 pane**。

        手势期间 Textual 的 ``Screen.selections`` 会包含拖过的所有 widget
        （跨到侧栏 / 另一个 pane）。我们只取按下 pane 自己覆盖的那一段：

        - ``Selection(start, end)``：单 widget 选择；
        - ``Selection(start, None)``：终点在按下点下方 / 右方 → 「按下点 →
          pane 末尾」；
        - ``Selection(None, end)``：终点在按下点上方 / 左方（交换后 pressed
          pane 变成 end widget，``end`` 是它**自己的** start offset）→
          「pane 顶部 → 按下点」；
        - ``Selection(None, None)``：整个 pane 被选中。

        三种形态都直接交给 ``PaneWidget.get_selection``（``Widget.get_selection``
        能正确处理）；不做 ``selection.start is None`` 守卫（那会丢掉向上拖拽
        的合法片段）。

        非手势（例如侧栏自己的选区）没有 ``_selection_pane`` → 走 Textual 默认
        的跨 widget 拼接逻辑。
        """
        app = self.app
        pane_id = getattr(app, "_selection_pane", None)
        if pane_id is not None:
            finder = getattr(app, "_widget_for_pid", None)
            pane = finder(pane_id) if finder is not None else None
            if pane is None:
                return None
            selection = self.selections.get(pane)
            if selection is None:
                return None
            got = pane.get_selection(selection)
            if got is None:
                return None
            return got[0].rstrip("\n") or None
        return super().get_selected_text()


class ConfirmDialog(ModalScreen[bool]):
    """破坏性动作的确认框（v0.5.4 起 `KillConfirm` 与「关闭忙 pane」共用）。

    安全默认（P0 验收报告 §2）：默认焦点在 Cancel 而非确认按钮，且 Esc / n 可取消、
    `y` 一键确认——否则键盘用户最自然的 Esc 无效、而 Enter 会直接执行破坏性动作。

    文案由调用方传入（kill-server 与 close-pane 各一套），类本身不认识任何词表键。
    CSS 写在 `ConfirmDialog` 上：Textual 的类型选择器匹配**全部基类名**
    （`DOMNode._css_types` 取自 `_css_bases`），所以子类实例照样命中。
    """

    # 必须写在 class body：Textual 在 __init_subclass__ 阶段收集 BINDINGS，
    # 事后赋值 ConfirmDialog.BINDINGS = [...] 不生效。
    BINDINGS = [
        Binding("escape", "dismiss(False)", "Cancel", show=False),
        Binding("n", "dismiss(False)", "Cancel", show=False),
        # v0.5.4：以前确认只能 Tab/Enter 或鼠标。Enter 落在 Cancel 上（安全默认），
        # 于是「安全」和「一键」只能二选一；`y` 把两边都给了。
        Binding("y", "dismiss(True)", "Confirm", show=False),
    ]

    CSS = """
    ConfirmDialog { align: center middle; }
    ConfirmDialog > Vertical {
        width: auto; height: auto;
        padding: 1 2; border: heavy $accent; background: $panel;
    }
    ConfirmDialog Horizontal { height: 3; }
    """

    def __init__(self, message: str, yes_label: str, no_label: str, **kwargs):
        super().__init__(**kwargs)
        self._message = message
        self._yes_label = yes_label
        self._no_label = no_label

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._message)
            with Horizontal():
                yield Button(self._yes_label, id="yes", variant="error")
                yield Button(self._no_label, id="no", variant="primary")

    def on_mount(self) -> None:
        # 默认焦点给 Cancel：Enter 落在破坏性按钮上是误触陷阱（P0 验收报告 §2）
        self.query_one("#no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class KillConfirm(ConfirmDialog):
    """``Ctrl-a k`` 的文案封装（行为全在 ``ConfirmDialog``）。

    类名被 ``tests/test_p0.py`` / ``test_v15.py`` / ``test_i18n.py`` 直接引用，
    v0.5.4 泛化时刻意**没有**改名——那三处断的是行为，不是类名语义。
    """

    def __init__(self, message: str, **kwargs):
        super().__init__(
            message,
            t("app.kill_confirm_yes"),
            t("app.kill_confirm_no"),
            **kwargs,
        )


class TerminalApp(App, inherit_bindings=False):
    ENABLE_COMMAND_PALETTE = False
    # 同 MainScreen：只写 BINDINGS=[] **删不掉** App 基类的默认键（textual 8.2.8
    # 实测 ``TerminalApp._merged_bindings.key_to_bindings == ['ctrl+c', 'ctrl+q']``）——
    # 其中 ctrl+q 还是 ``priority=True``，在 App.on_event 最前面就被查到 → 按下
    # **直接 quit 前端**；ctrl+c → help_quit。两者都违背 C9.1「一切键透传给
    # 内层 shell」→ 用 inherit_bindings=False 一并清掉（v0.15）。
    BINDINGS = []  # 本类无绑定；清掉继承绑定靠上面的类关键字（C9.1）
    # 焦点由 App 自己管（_sync_topology → 活动 pane；Ctrl-a s → 侧栏），
    # **不用框架的自动聚焦**：默认 AUTO_FOCUS="*" 会聚焦 DOM 里第一个可聚焦
    # 控件——侧栏加入文件树后那棵树排在 pane 之前，会把启动焦点抢走，
    # 导致「一切键透传给内层 shell」失效（P3）。
    AUTO_FOCUS = None

    CSS = """
    /* commit-card：悬停提交详情浮层。单独一层，盖在 pane 之上；absolute_offset
       由 GitLogPanel 按悬停行/侧栏右缘设置，constrain 保证不出屏。
       shortcut-help（v0.22）：顶栏 `?` 的快捷键浮层，层序在 commit-card 之上
       （两者同时出现的概率极低，但浮层必须压住悬停卡）。 */
    Screen { layout: vertical; layers: default commit-card shortcut-help; }
    #topbar { height: 1; layout: horizontal; background: $surface; }
    #topbar TopBarButton { width: auto; background: $surface; color: $text; }
    #topbar TopBarButton:hover { background: $accent; color: $text; }
    #topbar TopBarButton.disabled { color: $text-disabled; }
    #topbar PaneTab { width: auto; background: $surface; color: $text; }
    #topbar PaneTab.active { background: $accent; color: $text; }
    #topbar PaneTab.dead { color: $text-disabled; }
    /* 状态提示吃满中间空白，把品牌标签顶到最右侧 */
    #topbar-status { width: 1fr; color: $error; }
    #topbar-brand { width: auto; padding: 0 1; color: $text-muted; }
    /* 快捷键浮层：非模态、不参与布局；位置由 App 在显示时按 `?` 左缘计算
       （紧贴顶栏下沿 y=1），高度超屏时在浮层内滚轮阅读。 */
    #shortcut-help {
        layer: shortcut-help;
        overlay: screen;
        display: none;
        width: 45;
        height: auto;
        max-height: 90%;
        padding: 0 1;
        border: round $accent;
        background: $panel;
        color: $text;
        constrain: inside inflect;
        scrollbar-size: 0 1;
    }
    #shortcut-help-body { width: 1fr; height: auto; }
    #main { layout: horizontal; height: 1fr; }
    /* v0.11.3：聚焦底色**按半区染色**——焦点在文件树 → 只染上半区；焦点在
       Git 面板 → 只染下半区（#git-area = 标题行 + 历史面板的容器，CSS 无法
       跨兄弟节点染色，故包一层容器做 focus-within 锚点）。整侧栏染色的旧
       方案已删。 */
    #sidebar { width: 15fr; min-width: 20; max-width: 50%; padding: 0 1; background: $panel; }
    #file-tree { height: 50fr; background: $panel; overflow-x: hidden; scrollbar-size: 0 0; }
    #file-tree:focus-within { background: $accent 20%; }
    #git-area { height: 50fr; background: $panel; }
    #git-area:focus-within { background: $accent 20%; }
    #gitlog-title { height: 1; }
    /* git-log 背景透明：染色发生在 #git-area 容器上，自己不设底色才透得出来；
       未聚焦时透出容器的 $panel，视觉与之前一致。 */
    #git-log { height: 1fr; background: transparent; scrollbar-size: 0 0; }
    /* 悬停行：默认 $block-hover-background 太淡，换成更明显的 accent 底色 */
    #git-log > .option-list--option-hover {
        background: $accent 35%;
        color: $text;
    }
    /* 提交详情卡片：overlay 不参与布局；GitLogPanel 只负责 update /
       absolute_offset / display，样式集中在这里（与其它界面样式同处一处）。
       正文可能很长 → 限高 80% 屏高并在卡片内滚动；正文放在内部 Static
       （#commit-card-body）里，Textual 的鼠标选区取的是它。 */
    #commit-card {
        overlay: screen;
        layer: commit-card;
        display: none;
        width: auto;
        max-width: 46;
        max-height: 80%;
        height: auto;
        padding: 0 1;
        border: round $accent;
        background: $panel;
        color: $text;
        constrain: inside inflect;
        pointer: text;
        scrollbar-size: 0 1;
    }
    #commit-card-body { width: 1fr; height: auto; }
    /* v0.12：pane 区列优先——左右两列（#col-a / #col-b），列内上下分。
       2 pane 档 = 全宽上下分（pane-0 上 / pane-1 下）。 */
    #pane-container { width: 85fr; height: 1fr; layout: horizontal; }
    #col-a { width: 50fr; height: 1fr; }
    #col-b { width: 50fr; height: 1fr; }
    PaneWidget { width: 1fr; height: 1fr; }
    """

    def __init__(
        self,
        backend: TmuxBackend,
        root: str,
        takeover_path: str | None = None,
        stop_path: str | None = None,
        layout_path: str | None = None,
        graphics: graphics_mod.Capabilities | None = None,
    ):
        super().__init__()
        self.backend = backend
        self.root = root
        # v0.37：终端位图能力（cli 在 App.run() 前探测）；None = 无位图。
        self._graphics = graphics or graphics_mod.DISABLED
        # §6 P1：持锁者哨戒路径；None = 不装哨戒（测试/直连场景）
        self._takeover_path = takeover_path
        # 实例管理（计划 §3.4）：stop 标记路径——哨戒同一 tick 读取，
        # 有效即整个退场（与让位同为干净退出，shell 无损）
        self._stop_path = stop_path
        # v0.11：布局比例的落盘路径（None = 不持久化，测试/直连场景）
        # + 当前比例（启动即读入 → attach 同一项目时恢复上次的分割位置）
        self._layout_path = layout_path
        self._ratios: dict[str, float] = layout_state.load_ratios(layout_path)
        # 正在拖拽的分隔线（None = 没在拖）
        self._drag_sep: Separator | None = None
        # 分隔线控件缓存（id → 控件）：热区判定每帧都要用，别每次 query
        self._separators: dict[str, Separator] = {}
        # per-pane 前端状态（§11）：偏移由滚轮维护，内容由 capture 驱动
        self._offsets: dict[str, int] = {}
        self._history_sizes: dict[str, int] = {}
        self._heights: dict[str, int] = {}  # 上次 list-panes 的 pane_height（翻页公式用）
        self._commands: dict[str, str] = {}  # pane_current_command（v0.28 占用判定）
        self._alt_screens: dict[str, bool] = {}
        self._cache: dict[str, tuple] = {}  # 防闪烁 diff（N16）
        # IME 锚点（v0.18）：per-pane **最后一次**内层光标坐标（tmux
        # ``cursor_x/y``，仅实时视图且 cursor_flag 有效时记录）——光标被程序
        # 隐藏/dead/翻 scrollback 时锚点回退用（「保持该 pane 上次光标」拍板）
        self._last_cursors: dict[str, tuple[int, int]] = {}
        # resize 缓存：window_id 与上次下发的尺寸，避免每次 resize 事件
        # 都额外跑一次 list_panes（P0 验收报告 §3-2）
        self._window_ids: dict[str, str] = {}
        self._last_size: dict[str, tuple[int, int]] = {}
        self._fail_count = 0
        # §9 / §12 P2 断线重连：_disconnected = 已确认失去连接（持续重连中）；
        # _reconnect_until = 恢复后「已重新连接」提示的截止时刻
        self._disconnected = False
        self._reconnect_until = 0.0
        # v0.13 顶栏瞬时提示（复制结果等）：与 _reconnect_until 同款「到点过期」
        self._notice_text = ""
        self._notice_until = 0.0
        # v0.14 内部剪贴板：``Ctrl-a c`` 复制的选区存一份，``Ctrl-a v`` 在系统
        # 剪贴板读不到时回落到它。**不落盘**（沿用「无磁盘状态」原则，
        # detach/attach 后丢失）。
        self._copy_buffer: str = ""
        # ── v0.35 拖拽选字 / 松手自动复制 ──
        # _gesture = 进行中的左键手势（None = 无）；_selection_pane = 本次选区
        # 的起始 pane（MainScreen.get_selected_text 用它把范围限制在按下 pane）；
        # _frozen_pane = 画面被冻结的 pane（拖拽期间 capture 不再刷新它）；
        # _thaw_timer = 高亮 / hold / 手势安全超时的统一定时器；
        # _hold_selection = MYRTUI_COPY_ON_SELECT=0 时等待 Ctrl-a c 的状态。
        self._gesture: _PointerGesture | None = None
        self._selection_pane: str | None = None
        self._frozen_pane: str | None = None
        self._thaw_timer = None
        self._hold_selection = False
        self._copy_on_select: bool = resolve_copy_on_select(
            _os.environ.get("MYRTUI_COPY_ON_SELECT")
        )
        self._drag_select_mode: str = resolve_drag_select(
            _os.environ.get("MYRTUI_DRAG_SELECT")
        )
        # dead pane（§12 P2 respawn UX）：_dead 按 pane_id 记状态，
        # _dead_order 记死亡先后（尾部 = 最近死亡 → Ctrl-a r 的默认目标）
        self._dead: dict[str, bool] = {}
        self._dead_order: list[str] = []
        self._prefix_armed = False
        self._prefix_timer = None
        self._tick_timer = None
        self._high_ticks = 0
        self._next_poll = 0.0
        # B0（PERF-B0B1-PLAN §2）：空闲轮询间隔实例级解析——MYRTUI_IDLE_INTERVAL
        # 覆盖默认 IDLE_INTERVAL；后台/次要实例设 0.5 之类降频（2 次/s）
        self._idle_interval: float = resolve_idle_interval(
            _os.environ.get("MYRTUI_IDLE_INTERVAL")
        )
        # B1 §4.2 可选遥测：MYRTUI_LOOP_DEBUG=1 时用探针把事件循环迟到
        # > 30ms 的分布写到 stderr（真机验收期给 §7.2 决策门供数据；
        # 默认关闭，无行为影响）
        self._loop_debug: bool = _os.environ.get("MYRTUI_LOOP_DEBUG") == "1"
        self._loop_probe_timer = None
        # ── B1（PERF-B0B1-PLAN §3）：backend 阻塞调用移出事件循环 ──
        # 单线程串行执行器：ThreadPoolExecutor 的 _work_queue 是 FIFO →
        # 「提交顺序 = 执行顺序」（§3.1-1 顺序等价）。顺序敏感的 backend 调用
        # 必须走它，**不能**走 asyncio.to_thread 默认池（多线程并发会把快速
        # 连打字乱序）；无顺序依赖的阻塞写（剪贴板 pbcopy）才用默认池。
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="myrtui-io"
        )
        self._executor_closed = False  # on_unmount 置位：后续提交丢弃、回投不 apply
        # poll 单飞 + 尾部合并状态机（§3.1-3）：in-flight 期间的请求不入队，
        # 合并为「完成后的一次重提交」→ 实际并发 poll 恒 ≤ 1、队列深度恒 ≤ 1
        self._poll_in_flight = False
        self._poll_pending = False
        self._poll_depth = 0  # in-flight poll 的重试深度（0 = 常规请求）
        # 多 pane 拓扑（§12 P1）：槽位顺序 = window_index 排序，槽位 i ↔ #pane-i
        self._pane_ids: list[str] = []
        self._active_index = 0
        self._tab_labels: list[str] = []
        self._tab_deads: list[bool] = []
        self._shell_names = set(SHELL_COMMANDS)
        env_shell = _os.environ.get("SHELL")
        if env_shell:
            self._shell_names.add(_os.path.basename(env_shell).lstrip("-").lower())
        # 鼠标转发（v0.7 §10.2）：门控级别按 pane 内程序请求的模式逐 tick 刷新；
        # _mouse_down_pane = 按下发生地（拖动/释放恒路由回它，防跨 pane 卡状态）；
        # _mouse_down_forwarded = 该按下是否已注入（程序无鼠标时释放也不得注入）；
        # _mouse_sgr = 编码位（v0.7.1 审阅 F3：=0 须发 xterm 旧 3 字节编码）
        self._mouse_levels: dict[str, int] = {}
        self._mouse_sgr: dict[str, bool] = {}
        self._mouse_down_pane: str | None = None
        self._mouse_down_forwarded = False
        self._pending_motion: tuple[str, str] | None = None  # (pane_id, seq) 尾部合并
        self._last_motion_sent = 0.0
        # Git 历史（P3 D7）：**单一当前目标**——None = 仓库级，否则相对 root
        # 的文件路径。定时器刷的也是「当前目标」，不会把面板弹回仓库级。
        self._log_target: str | None = None
        self._gitlog_timer = None
        # v0.22：快捷键浮层的显示/隐藏定时器（悬停延迟 + 离开宽限）
        self._help_show_timer = None
        self._help_hide_timer = None
        # v0.29：键盘（Ctrl-a ?）打开的浮层不被鼠标离开的宽限定时器误关
        self._help_pinned = False

    def get_default_screen(self) -> Screen:
        return MainScreen()

    def compose(self) -> ComposeResult:
        yield TopBar(id="topbar")
        with Horizontal(id="main"):
            yield Sidebar(self.root, id="sidebar")
            # 侧栏 | pane 区的竖分隔线（可拖）
            yield Separator("vertical", id="sep-main")
            # v0.12：pane 区列优先——左右两列，列内上下分。
            # 4 个预挂槽位：布局档由显示/隐藏切换（display 切换会触发
            # on_resize → resize-window，尺寸联动免手工计算）
            with Horizontal(id="pane-container"):
                with Vertical(id="col-a"):
                    yield PaneWidget("", id="pane-0")
                    # 列内上下分隔线（可拖，pane-0 ↔ pane-1）
                    yield Separator("horizontal", id="sep-rows-a")
                    yield PaneWidget("", id="pane-1")
                # 左右两列之间的竖分隔线（可拖）
                yield Separator("vertical", id="sep-cols")
                with Vertical(id="col-b"):
                    yield PaneWidget("", id="pane-2")
                    # 列内上下分隔线（可拖，pane-2 ↔ pane-3；与 sep-rows-a 共用比例）
                    yield Separator("horizontal", id="sep-rows-b")
                    yield PaneWidget("", id="pane-3")
        # 悬停提交详情：屏幕级 overlay 最后挂载，保证盖在 pane 之上。
        yield CommitCard(id="commit-card")
        # v0.22：顶栏 `?` 的快捷键浮层（层序在 commit-card 之上）
        yield ShortcutHelp(id="shortcut-help")

    # ---------- 多 pane 拓扑（§12 P1） ----------

    def _topology_fetch(self) -> list[PaneInfo]:
        """拓扑同步的**线程段**（B1 §3.2-2）：list-panes + 槽位排序截断，纯数据。

        槽位顺序 = window_index 排序（新建追加、关闭后紧凑移位），
        槽位 i 始终对应控件 ``#pane-i``；Ctrl-a 1..4 / 顶栏 tab 按槽位寻址。
        """
        panes = sorted(self.backend.list_panes(), key=lambda p: p.window_index)
        return panes[:MAX_PANES]  # 外部建出第 5 个 window 时只取前 4（槽位只有 4 个）

    def _topology_apply(self, panes: list[PaneInfo]) -> None:
        """拓扑同步的**循环段**（B1 §3.2-2）：槽位分配、行显隐、焦点与顶栏标签。

        消费 ``_topology_fetch`` 的纯数据结果；亦被 poll 的漂移同步直接调用
        （旧 ``_poll`` 里 ``_sync_topology`` 的回投位置）。
        """
        ids = [p.pane_id for p in panes]
        self._pane_ids = ids
        # v0.35：拓扑变化（槽位显隐 / 移位 / 漂移）→ 几何与 pane 归属已过期，
        # 立即收摊手势与冻结（否则拖拽中关 pane / 切布局会留下冻结的旧画面）。
        # 放在更新 _pane_ids 之后：_thaw_pane 里的 _request_poll / _sync_ime_anchor
        # 要用新拓扑。
        if self._gesture is not None or self._frozen_pane is not None:
            self._cancel_gesture()
        for p in panes:
            self._offsets.setdefault(p.pane_id, 0)
            self._history_sizes.setdefault(p.pane_id, p.history_size)
            self._alt_screens.setdefault(p.pane_id, p.alternate_on)
            self._window_ids[p.pane_id] = p.window_id
            self._heights.setdefault(p.pane_id, p.height)
            self._commands[p.pane_id] = p.command
        # 清掉已消失 pane 的 per-pane 状态，防无界增长
        stale = (
            set(self._offsets) | set(self._history_sizes)
            | set(self._alt_screens) | set(self._dead)
        ) - set(ids)
        for d in (
            self._offsets,
            self._history_sizes,
            self._alt_screens,
            self._window_ids,
            self._heights,
            self._commands,
            self._last_size,
            self._cache,
            self._mouse_levels,
            self._mouse_sgr,
            self._dead,
            self._last_cursors,
        ):
            for pid in stale:
                d.pop(pid, None)
        self._dead_order = [p for p in self._dead_order if p in ids]
        # 按下状态涉及的 pane 消失 → 复位（防释放/拖动路由到已死槽位）
        if self._mouse_down_pane is not None and self._mouse_down_pane not in ids:
            self._mouse_down_pane = None
            self._mouse_down_forwarded = False
        count = len(ids)
        self._active_index = min(self._active_index, max(0, count - 1))
        # 列显隐决定布局档（v0.12 列优先）：1槽→全宽（col-b 隐藏）；2槽→上下分；
        # 3槽→左2右1（右列只有 pane-2）；4槽→2×2（左列 pane-0/1、右列 pane-2/3）
        col_b_visible = count >= 3
        col_b = self.query_one("#col-b")
        if col_b.display != col_b_visible:
            col_b.display = col_b_visible
        for slot in range(MAX_PANES):
            widget = self.query_one(f"#pane-{slot}", PaneWidget)
            visible = slot < count
            if visible:
                widget.pane_id = ids[slot]
            if widget.display != visible:
                widget.display = visible
        # 顶栏标签按新拓扑补长/截断（真实 command/dead 由下次 poll 刷新）：
        # 只截断不补长的话，新建 pane 后 set_tabs 按旧长度会把新 tab 藏到
        # 下一轮 poll；只补长不截断的话，关 pane 后会残留多余标签。
        self._tab_labels = self._tab_labels[:count]
        self._tab_deads = self._tab_deads[:count]
        while len(self._tab_labels) < count:
            i = len(self._tab_labels)
            self._tab_labels.append(f"{i + 1}:sh")
            self._tab_deads.append(False)
        self._update_tabs()
        # 分隔线显隐跟着拓扑走：藏起来的块旁边的线也要藏（否则留一条 1 cell
        # 的死线，还白白吃掉 fr 空间）
        self._sync_separators()
        # 仅默认屏时同步焦点（modal 打开期间不得把焦点拉回 pane）
        if count and isinstance(self.screen, MainScreen):
            self.query_one(f"#pane-{self._active_index}", PaneWidget).focus()
        # IME 锚点（v0.18）：拓扑变化（槽位显隐/移位）→ content_region 移位
        self._sync_ime_anchor()

    def _sync_topology(self) -> None:
        """提交一次拓扑同步 job（B1 §3.2-2）：线程段 list-panes → 循环段 apply。

        旧 ``_sync_topology`` 拆为 fetch/apply 两段后的提交入口（``on_mount``
        等处调用）；poll 的漂移同步走 ``_probe_panes`` + ``_topology_apply``，
        动作类（新建/关闭 pane）走「tmux 动作 + list-panes」合并 job。
        """
        self._submit_job(
            self._topology_fetch,
            apply=self._topology_apply,
            label=t("app.job.list_panes"),
        )

    # ---------- B1：单线程串行执行器（PERF-B0B1-PLAN §3.1） ----------

    def _submit_job(
        self,
        fetch,
        *,
        apply=None,
        label: str | None = None,
        on_error=None,
    ):
        """提交「线程段 fetch → 循环段 apply」的串行 job，入队即返。

        - ``fetch`` 在唯一工作线程里执行（子进程 + 纯数据解析，**绝不碰**
          DOM/前端状态）；执行器 FIFO → 提交顺序 = 执行顺序 = 事件顺序
          （§3.1-1：按键 send 恒先于其 burst poll 执行，回显语义不变）；
        - ``apply`` 经 future 回调回投事件循环（run_in_executor 的回调由
          call_soon 调度并保持完成顺序 → apply 顺序 = 提交顺序，§3.1-4）。
          默认池任务一律不带 apply 段（防第二种线程池打破顺序保证）；
        - ``TmuxError`` 按既有口径记 ``"<label>: %s"`` warning；其他异常同样
          消费并记录（§3.3-4：future 异常必须被 retrieve，否则 asyncio 静默
          丢错 + never-retrieved 告警）；``on_error(exc)`` 供调用方回滚乐观
          置位之类的善后；
        - apply 段统一 ``except NoMatches: return``（v0.16 同款兜底：回投时
          DOM 可能已在拆卸）；
        - 执行器已关闭（teardown，§3.3-1）→ 丢弃提交返回 None。
        """
        if self._executor_closed:
            return None
        # 标签只进日志；默认值在这里求值（写成默认参数会在 import 时固化语言）
        label = label or t("app.job.backend")
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(self._executor, fetch)
        fut.add_done_callback(
            functools.partial(
                self._job_done, label=label, apply=apply, on_error=on_error
            )
        )
        return fut

    def _job_done(self, fut, *, label, apply, on_error) -> None:
        """job 回投（事件循环侧）：异常 retrieve + closed 检查 + apply。"""
        if self._executor_closed:
            fut.exception()  # retrieve：结果丢弃但异常不静默（§3.3-4）
            if on_error is not None:
                on_error(None)
            return
        try:
            result = fut.result()
        except TmuxError as exc:
            log.warning("%s: %s", label, exc)
            if on_error is not None:
                on_error(exc)
            return
        except Exception as exc:  # 非 TmuxError 也不许静默炸回调
            log.warning("%s: 未预期异常 %s", label, exc, exc_info=True)
            if on_error is not None:
                on_error(exc)
            return
        if apply is not None:
            try:
                apply(result)
            except NoMatches:
                # teardown 竞态（v0.16 口径）：回投时 DOM 已拆卸 → 丢弃结果
                return

    @property
    def _active_pane(self) -> str | None:
        """当前焦点 pane 的 pane_id（键透传 / paste / 前缀超时的目标）。"""
        if not self._pane_ids:
            return None
        return self._pane_ids[min(self._active_index, len(self._pane_ids) - 1)]

    def _set_active(self, index: int) -> None:
        if not (0 <= index < len(self._pane_ids)):
            return  # 越界槽位静默忽略（Ctrl-a 4 但只有 2 个 pane）
        self._active_index = index
        self.query_one(f"#pane-{index}", PaneWidget).focus()
        self._update_tabs()
        # IME 锚点（v0.18）：切焦点瞬间选字框就得跟到新 pane，不等下轮 poll
        self._sync_ime_anchor()

    def focus_pane(self, pane_id: str) -> None:
        """点击 pane → 切焦点（§10.2）。"""
        if pane_id in self._pane_ids:
            self._set_active(self._pane_ids.index(pane_id))

    # ---------- IME 锚点（v0.18：中文选字框跟随活动 pane 光标） ----------

    def _sync_ime_anchor(self) -> None:
        """把**外层终端的硬件光标**（= OS 输入法/emoji 弹层的锚点）同步到
        活动 pane 的内层光标处。

        背景：选字框锚在硬件光标上，而 Textual 每帧帧尾都会把它
        ``move_to(App.cursor_position)``（``textual/app.py::_display``）；
        该属性默认 ``(0, 0)``、只有内置 ``Input``/``TextArea`` 会更新——
        本仓界面里一个都没有，不主动维护的话锚点每帧都被按回窗口左上角
        （选字框压住顶栏「1:zsh」，用户反馈的根因）。

        锚点 = 活动 pane ``content_region`` + 该 pane **最后一次**内层光标
        （``_last_cursors``，tmux ``cursor_x/y``）：

        - 内层光标不可用（vim 隐藏光标、pane dead、翻 scrollback）→ 回退
          该 pane 上次光标位置；该 pane 从未有过光标 → 内容区左上角；
        - 坐标恒 clamp 进内容区（resize 竞态下 tmux 光标可能越界）；
        - 焦点在侧栏时锚点**不动**（侧栏不接收中文输入，跟 pane 走即可）；
        - ``content_region`` 为 0 尺寸（首帧前/槽位未布局）→ 跳过，
          保持现状不写垃圾坐标。
        """
        if not isinstance(self.screen, MainScreen):
            # 模态（右键菜单等）打开时不要抢硬件光标：菜单里的输入控件
            # 自己维护光标，pane 锚点此时不可见。
            return
        if self._frozen_pane is not None:
            # v0.35：拖拽期间画面冻结，内层光标已不可见 → 不要移动输入法锚点
            return
        if not self._pane_ids:
            return
        index = min(self._active_index, len(self._pane_ids) - 1)
        pid = self._pane_ids[index]
        try:
            widget = self.query_one(f"#pane-{index}", PaneWidget)
        except NoMatches:
            return  # teardown 竞态（v0.16 口径）
        region = widget.content_region
        if region.width <= 0 or region.height <= 0:
            return  # 尚未布局（首帧前）→ 保持上一个有效锚点
        last_x, last_y = self._last_cursors.get(pid, (0, 0))
        pos = Offset(
            region.x + max(0, min(last_x, region.width - 1)),
            region.y + max(0, min(last_y, region.height - 1)),
        )
        if pos == self.cursor_position:
            return  # 无变化不发序列（每 tick 都会走到这里）
        self.cursor_position = pos
        self._write_ime_anchor(pos)

    def _write_ime_anchor(self, pos: Offset) -> None:
        """把硬件光标**立即**移到锚点（不等下一帧）。

        Textual 只在**有帧渲染**时才在帧尾补 ``move_to``；而锚点变化有一类
        路径天然**没有**后续帧——布局变化（侧栏折叠/拖分隔线）时帧已在
        ``Resize`` 消息处理**之前**画完，内容没 diff 时 poll 也不触发重绘。
        这里直接补一发 CUP（与 ``_display`` 帧尾发的同一条序列）：写在
        帧间空隙，事件循环单线程不会与整帧写交错（非 Windows 下每帧是
        单次 ``driver.write``），后续帧帧尾的 ``move_to`` 用的是同一个
        ``cursor_position``，两者恒收敛、幂等。

        纯体验行为：驱动未启动/已停时**静默跳过**，绝不影响主流程。
        """
        driver = self._driver
        if driver is None:
            return
        try:
            driver.write(Control.move_to(pos.x, pos.y).segment.text)
            driver.flush()
        except Exception:  # noqa: BLE001 —— 锚点是装饰性行为，失败不打扰主流程
            log.debug("IME 锚点写入失败", exc_info=True)

    # ---------- 侧栏与焦点模型（P3，§5 D8） ----------

    def _sidebar(self) -> Sidebar:
        return self.query_one("#sidebar", Sidebar)

    def _sidebar_focused(self) -> bool:
        """焦点是否落在侧栏子树内（D8 焦点模型）。

        **焦点控件的 BINDINGS 不在焦点层消费**（textual 8.2.8 实测）：键先到
        ``TerminalApp.on_key``（本方法所在处），之后才由框架 ``App._on_key`` →
        ``_check_bindings`` 派发给树。故焦点在侧栏时必须在此**拦截**透传，
        否则 ↑↓/enter/space 会同时进 shell（双重处理）。
        """
        focused = self.focused
        if focused is None:
            return False
        if focused.id == "sidebar":
            return True
        return any(ancestor.id == "sidebar" for ancestor in focused.ancestors)

    def _focus_active_pane(self) -> None:
        """焦点回当前活动 pane（D8：Esc 回终端 / 侧栏折叠后收回焦点）。"""
        if not self._pane_ids:
            return
        self._set_active(min(self._active_index, len(self._pane_ids) - 1))

    def action_toggle_sidebar(self) -> None:
        """Ctrl-a b：折叠 / 展开侧栏。

        折叠后 pane 变宽 → Textual 的 resize 事件 → ``resize-window`` 联动
        自动生效（§9 验收 8）。
        """
        sidebar = self._sidebar()
        sidebar.display = not sidebar.display
        self._sync_separators()  # 侧栏旁的竖线跟着藏 / 显
        if sidebar.display:
            self._refresh_status()
            self._refresh_git_log()  # 展开回来立刻补一次历史
            sidebar.refresh_file_status()  # 补一次文件树状态配色
        elif self._sidebar_focused():
            self._focus_active_pane()  # 侧栏被隐藏 → 焦点不能悬空

    def action_focus_sidebar(self) -> None:
        """Ctrl-a s：焦点移入侧栏（文件树）。

        键盘用户进入侧栏的唯一入口（无鼠标环境下树的键盘导航否则不可达）。
        与 ``Esc`` 互逆，构成「进侧栏 ↔ 回终端」的一对键。
        """
        sidebar = self._sidebar()
        if not sidebar.display:
            sidebar.display = True
            self._sync_separators()
        sidebar.query_one("#file-tree", FileTree).focus()
        self._refresh_status()

    def action_focus_git_log(self) -> None:
        """Ctrl-a g：焦点移入侧栏的 Git 历史面板（v0.22）。

        与 ``Ctrl-a s``（文件树）对称。侧栏折叠时先展开并补一次取数；
        面板只读——↑↓ 移动高亮，``Esc`` 回当前活动 pane。
        """
        sidebar = self._sidebar()
        if not sidebar.display:
            sidebar.display = True
            self._sync_separators()
            self._refresh_git_log()
            sidebar.refresh_file_status()
        try:
            self.query_one("#git-log", GitLogPanel).focus()
        except NoMatches:
            return  # teardown 竞态（同 _focus_active_pane 的兜底口径）
        self._refresh_status()

    # ---------- 分隔线与布局比例（v0.11） ----------

    @staticmethod
    def _fr(value: float) -> str:
        """比例 → fr 字符串（0.25 → ``25fr``）。"""
        return f"{value * 100:g}fr"

    def _apply_ratios(self) -> None:
        """把比例落到各块的 fr 宽 / 高上。

        **为什么用 fr 而不是 %**：``25%`` 是相对整个容器，剩下的 75% 里还得
        扣掉分隔线和各自的 padding，拖起来对不上；两块各写一份 fr 才是
        精确比例，还顺带白送「窗口缩放时比例不变」。
        """
        r = self._ratios
        self.query_one("#sidebar").styles.width = self._fr(r["sidebar"])
        self.query_one("#pane-container").styles.width = self._fr(1 - r["sidebar"])
        self.query_one("#file-tree").styles.height = self._fr(r["tree"])
        # v0.11.3：git-log 包进 #git-area 后，对半 fr 落在容器上；
        # 标题(1) + 历史(1fr) 在容器内部分配（#git-log 的高度由 CSS 管）
        self.query_one("#git-area").styles.height = self._fr(1 - r["tree"])
        # v0.12 列优先：**cols** = 左右两列的宽度占比（外层竖线）；
        # **rows** = 列内上下的高度占比（内层横线，两列共用 → 十字对齐）。
        # 隐藏的那一列不参与 fr 分配 → 剩下的列自动占满（Textual 的 fr
        # 按**可见**子节点归一化），不需要特判。
        cols = r["cols"]
        self.query_one("#col-a").styles.width = self._fr(cols)
        self.query_one("#col-b").styles.width = self._fr(1 - cols)
        rows = r["rows"]
        for slot in range(MAX_PANES):
            pane = self.query_one(f"#pane-{slot}", PaneWidget)
            # 偶数槽位（0/2）是列内上块，奇数槽位（1/3）是下块
            pane.styles.height = self._fr(rows if slot % 2 == 0 else 1 - rows)

    def _set_ratio(self, dimension: str, value: float) -> None:
        """设定某维度的比例并立刻生效（夹到合法范围）。"""
        value = min(layout_state.MAX_RATIO, max(layout_state.MIN_RATIO, value))
        if self._ratios.get(dimension) == value:
            return
        self._ratios[dimension] = value
        self._apply_ratios()

    def _sync_separators(self) -> None:
        """分隔线显隐跟着拓扑 / 侧栏折叠走。

        藏起来的块旁边的线必须一起藏：否则会留一条 1 cell 的死线，还把
        fr 空间白吃掉一块（比例立刻失真）。
        """
        count = len(self._pane_ids)
        sidebar_visible = self._sidebar().display
        visibilities = {
            "sep-main": sidebar_visible,
            "sep-tree": sidebar_visible,  # 在侧栏内部
            "sep-cols": count >= 3,       # 左右两列之间（右列存在才有）
            "sep-rows-a": count >= 2,     # 左列内上下分
            "sep-rows-b": count >= 4,     # 右列内上下分
        }
        for sep_id, visible in visibilities.items():
            sep = self.query_one(f"#{sep_id}", Separator)
            if sep.display != visible:
                sep.display = visible

    # （v0.11.2）焦点视觉从「分隔线变粗变绿」改为 **PaneWidget 自己的边框**
    # （`:focus` → heavy $success，见 pane_widget.py）——整条分隔线跨出焦点
    # pane 边界（sep-cols 全高、sep-rows-a 全宽、sep-main 全高），绿线溢出到
    # 相邻 pane 的旁边，肉眼上完全乱掉；侧栏聚焦改走**按半区**的底色变化
    # （v0.11.3：焦点在文件树 → 只染上半区；焦点在 Git 面板 → 只染下半区）。

    # ---------- 分隔线拖拽 ----------

    def _separator_near(
        self, x: int, y: int, tolerance: int | None = None
    ) -> Separator | None:
        """离 (x, y) 最近的可见分隔线（轴向距离 ≤ tolerance）。

        距离按**分隔线自己的轴**算：竖线比 x、横线比 y；且必须落在分隔线
        覆盖的那一段里——否则竖线的热区会漏到隔壁那一行去（同一列上可能
        上下各有一条竖线）。
        """
        if not self._separators:
            return None
        tol = SEPARATOR_GRAB_TOLERANCE if tolerance is None else tolerance
        best: Separator | None = None
        best_dist = tol + 1  # 严格小于 → 命中范围恰好是 [0, tol]
        for sep_id, spec in SEPARATOR_SPECS.items():
            separator = self._separators.get(sep_id)
            if separator is None or not separator.display:
                continue
            # **只看当前屏上的分隔线**：弹窗（modal）打开时主屏还铺在底下，
            # 坐标是重叠的——不排除的话，弹窗里的按钮会正好落在某条分隔线的
            # 热区上（点「Kill」变成拖分隔线，弹窗关不掉）。
            if separator.screen is not self.screen:
                continue
            region = separator.region
            if region.width == 0 or region.height == 0:
                continue
            if spec[4] == "x":  # 竖线：比 x
                if not (region.y <= y < region.y + region.height):
                    continue
                dist = abs(x - region.x)
            else:  # 横线：比 y
                if not (region.x <= x < region.x + region.width):
                    continue
                dist = abs(y - region.y)
            if dist < best_dist:
                best, best_dist = separator, dist
        return best

    def press_near_separator(self, event) -> bool:
        """按下落在分隔线热区 → 开始拖那条分隔线（返回 True = 已处理）。

        **为什么要有热区**：分隔线本体只有 1 cell 宽，终端按 cell 报坐标，
        手抖偏几格就落到旁边的 pane 上 → 看着有线却拖不动（v0.11 实测）。

        **拖拽认左键与右键**（v0.11.5）：v0.11.3 曾收口为只认左键，但用户
        真机定位出特殊环境——UU 远程等嵌入 tmux 的终端里**左键事件不可达**
        而右键可达 → 恢复双键拖拽，中键仍不参与；代价是热区内右键不再透传
        给内层程序（用户拍板接受）。
        """
        if getattr(event, "button", 1) not in DRAG_BUTTONS:
            return False
        separator = self._separator_near(event.screen_x, event.screen_y)
        if separator is None:
            return False
        event.stop()
        # 这次按下是「拖分隔线」，不是「点 pane」→ pane 内的鼠标记账要复位，
        # 否则释放时会给某个 pane 发一个孤儿 release。
        self._mouse_down_pane = None
        self._mouse_down_forwarded = False
        self._start_separator_drag(separator)
        return True

    def _start_separator_drag(self, separator: Separator) -> None:
        """开始拖一条分隔线（捕获鼠标 → 指针滑出分隔线仍收得到 move）。"""
        self._drag_sep = separator
        separator.capture_mouse()
        _mouse_debug(f"  drag started: {separator.id}")

    def _begin_app_drag(self, separator: Separator, event) -> None:
        """**前置截获**路径的起拖（v0.11.4）：不经过 widget 层。

        焦点语义与 widget 层路径一致（v0.11.1 口径）：按在 **pane** 内热区
        → 焦点切到该 pane（键盘透传不中断）；按在线本体/侧栏 → 不动焦点
        （``test_separator_drag_does_not_steal_focus`` 的既有语义）。
        鼠标记账必须复位：本次按下是拖分隔线，不是点 pane。
        """
        self._mouse_down_pane = None
        self._mouse_down_forwarded = False
        try:
            widget_under = self.screen.get_widget_at(
                int(event.screen_x), int(event.screen_y)
            )[0]
        except Exception:  # NoWidget：热区恰好落在无控件处
            widget_under = None
        if isinstance(widget_under, PaneWidget):
            self.focus_pane(widget_under.pane_id)
        self._start_separator_drag(separator)

    def _finish_separator_drag(self) -> None:
        """拖拽收摊（widget 层 / App 层两条路径共用）：释放捕获 + 落盘。"""
        separator = self._drag_sep
        if separator is None:
            return
        self._drag_sep = None
        separator.release_mouse()
        self._save_layout()
        _mouse_debug(f"  drag finished: {separator.id}")

    def _update_separator_hover(self, x: int, y: int) -> None:
        """指针在命中区附近 → 那条线提亮（告诉用户「这儿能拖」）。"""
        near = self._separator_near(x, y)
        for separator in self._separators.values():
            separator.near = separator is near

    def on_separator_mouse_down(self, separator: Separator, event) -> None:
        """按下分隔线 → 开始拖拽。

        **捕获鼠标**：拖的时候指针会滑到分隔线外面（甚至滑到别的 pane 上），
        不捕获就收不到后续 move 事件，比例会「粘住」不动。
        """
        if event.button not in DRAG_BUTTONS:
            return  # 拖拽认左/右键（v0.11.5），中键不参与
        if separator.id not in SEPARATOR_SPECS:
            return
        self._start_separator_drag(separator)

    def on_separator_mouse_move(self, separator: Separator, event) -> None:
        if self._drag_sep is not separator:
            return  # 不是本次拖拽（可能是 pane 内拖动途经分隔线）→ 忽略
        if not event.button:
            # 按钮在窗口外松开 → 不会再有 mouse_up → 收摊（否则鼠标被永久捕获）
            self._drag_sep = None
            separator.release_mouse()
            return
        self._drag_to(separator, event)

    def on_separator_mouse_up(self, separator: Separator, event) -> None:
        if self._drag_sep is not separator:
            return
        self._finish_separator_drag()  # widget 层路径：释放 + 落盘

    def _drag_to(self, separator: Separator, event) -> None:
        """分隔线跟到鼠标位置 → 换算成比例。

        换算：``比例 = (鼠标位置 - 容器内容起点) / 可分配空间``，其中可分配
        空间 = 容器尺寸 - 该维度里固定占用的 cell（分隔线自己、git 标题等）。
        """
        spec = SEPARATOR_SPECS.get(separator.id or "")
        if spec is None:
            return
        dimension, container_sel, _first, _second, axis, fixed = spec
        container = self.query_one(container_sel)
        if axis == "x":
            pointer = event.screen_x
            origin = container.content_region.x
            pool = container.content_size.width - fixed
        else:
            pointer = event.screen_y
            origin = container.content_region.y
            pool = container.content_size.height - fixed
        if pool <= 0:
            return
        value = min(layout_state.MAX_RATIO, max(layout_state.MIN_RATIO, (pointer - origin) / pool))
        _mouse_debug(
            f"  _drag_to {dimension}: pointer={pointer} origin={origin} "
            f"pool={pool} -> ratio={value:.4f}"
        )
        self._set_ratio(dimension, value)

    def reset_layout_dimension(self, separator: Separator) -> None:
        """双击分隔线 → 该维度比例复位默认。"""
        spec = SEPARATOR_SPECS.get(separator.id or "")
        if spec is None:
            return
        dimension = spec[0]
        default = layout_state.DEFAULT_RATIOS[dimension]
        if self._ratios.get(dimension) == default:
            return
        self._ratios[dimension] = default
        self._apply_ratios()
        self._save_layout()

    def _save_layout(self) -> None:
        """比例落盘（下次 attach 同一项目时恢复这次的分割位置）。"""
        layout_state.save_ratios(self._layout_path, self._ratios)

    # ---------- Git 历史（P3，§5 D6/D7） ----------

    def on_file_tree_path_selected(self, event: FileTree.PathSelected) -> None:
        """选中文件 → 切该文件的历史；选中目录 → 回仓库级（D6）。

        「单一当前目标」模型（D7 #7）：目标只在「选中变化」时切换，定时器与
        事件走同一条通道、同一份目标状态——定时器不会把面板弹回仓库级。
        """
        self._log_target = None if event.is_dir else event.path
        self._refresh_git_log()

    def on_file_tree_path_context_requested(
        self, event: FileTree.PathContextRequested
    ) -> None:
        """文件树请求上下文菜单（v0.20 右键；v0.28.1 起 Enter / Space 也可）。

        - 右键只高亮（树侧已设 ``cursor_line``），**不**改 ``_log_target``；
          ``Space`` 与右键同语义；
        - ``Enter`` 文件行在发本消息之前已按原语义发 ``PathSelected``
          （选中 + 切 Git 历史），因此菜单打开时历史目标已经跟到该文件；
        - 打开菜单前取消挂起的 ``Ctrl-a`` 前缀：``_prefix_timeout`` 定时器与
          screen 无关，不取消的话到点会把 Ctrl-a 注入 pane（D6 边角）；
        - 位置取右键 / 光标行的屏幕坐标，菜单自己负责 constrain；
        - 文件行：打开（编辑器）/ 预览 Markdown（仅 md）/ 预览图片（仅图片）
          / 复制相对路径 / 复制绝对路径；目录行：刷新此目录 / 复制两项。
          打开直接注入**当前 pane**；预览打开应用内全屏只读层，不占用 pane
          （v0.28 / v0.31 / v0.36）。
        """
        self._cancel_prefix()
        items: list[tuple[str, str]] = []
        if not event.is_dir:
            if pane_commands.is_image(event.absolute):
                # 图片是二进制：拿编辑器打开只会显示乱码，查看器（ov/less/cat）
                # 同理，所以这里只留应用内预览 + 两种复制（v0.4.3）。
                items.append(("image", t("menu.image_preview")))
            else:
                # v0.31：菜单直接显示当前解析到的工具，用户一眼知道会调起什么；
                # 没有编辑器时退回只读查看器（ov / less / cat）。
                editor = pane_commands.resolve_editor()
                if editor is None:
                    items.append(
                        ("view", t("menu.view", tool=pane_commands.viewer_name()))
                    )
                else:
                    items.append(("view", t("menu.open", editor=editor.name)))
                if pane_commands.is_markdown(event.absolute):
                    items.append(("preview", t("menu.preview_markdown")))
        else:
            items.append(("refresh_dir", t("menu.refresh_dir")))
        items.append(("copy_rel", t("menu.copy_rel")))
        items.append(("copy_abs", t("menu.copy_abs")))
        self.push_screen(
            ContextMenuScreen(items, event.screen_x, event.screen_y),
            callback=lambda action: self._context_menu_closed(
                action, event.path, event.absolute, event.is_dir
            ),
        )

    def _context_menu_closed(
        self,
        action: str | None,
        path: str,
        absolute: str,
        is_dir: bool,
    ) -> None:
        """菜单关闭回调：执行动作 + 归还焦点（D7）。

        ``pop_screen`` 不恢复打开前的焦点，本仓 ``AUTO_FOCUS = None``，不显式
        归还就会焦点悬空（后续 ↑↓ 既不动树、也不符合用户预期）。
        """
        if action in ("view", "preview", "image"):
            # 显式动作 = 切 Git 历史目标（Q5），与右键本身「不改历史」区分
            self._log_target = None if is_dir else path
            self._refresh_git_log()
            if is_dir:
                self._focus_file_tree()
                return
            if action == "preview":
                self._open_markdown_preview(path, absolute)
                return
            if action == "image":
                self._open_image_preview(path, absolute)
                return
            self._open_in_pane(absolute)
            return
        if action == "refresh_dir":
            self._refresh_tree_dir(path, absolute)
            self._focus_file_tree()
            return
        if action == "copy_abs":
            self.copy_text(absolute, note=t("app.copied_path"))
        elif action == "copy_rel":
            # 根节点的相对路径是空串 → 复制 "."（git add . 等命令可直接用）
            self.copy_text(path or ".", note=t("app.copied_rel"))
        self._focus_file_tree()

    def _refresh_tree_dir(self, relative: str, absolute: str) -> None:
        """刷新右键的目录；目录已被删则刷新父目录（让节点从树上消失）。

        ``FileTree.reload_path`` 对已删除目录**仍会命中内存节点**并返回
        ``True``（``reload_node`` 只把它刷成空子节点），所以删除语义必须由
        这里的 ``is_dir()`` 检查承接：刷新父目录，父目录重扫后自然不再列出
        被删项。返回 ``False`` 仅表示节点真的不在树里（未加载 / 竞态）。
        """
        try:
            tree = self.query_one("#file-tree", FileTree)
        except NoMatches:
            return  # teardown / 侧栏已拆
        if not _os.path.isdir(absolute):
            if relative:
                parent = (
                    relative.rsplit("/", 1)[0] if "/" in relative else ""
                )
                tree.reload_path(parent)
                self._notify(t("app.dir_gone_parent"))
            else:
                tree.reload()
                self._notify(t("app.dir_gone_reload"))
            return
        if not tree.reload_path(relative):
            self._notify(t("app.node_missing"))

    def _focus_file_tree(self) -> None:
        """把焦点还给文件树（teardown / 侧栏已拆时静默；Esc / 菜单动作后共用）。"""
        try:
            self.query_one("#file-tree", FileTree).focus()
        except NoMatches:
            pass  # teardown / 侧栏已拆（v0.16 同款兜底口径）

    def _cancel_prefix(self) -> None:
        """取消挂起/已触发的 ``Ctrl-a`` 前缀（v0.20 右键菜单逻辑抽成 helper）。"""
        if not self._prefix_armed:
            return
        self._prefix_armed = False
        if self._prefix_timer is not None:
            self._prefix_timer.stop()
            self._prefix_timer = None

    def _git_focused(self) -> bool:
        """焦点是否在 ``#git-log`` 子树内（v0.22：顶栏提示用）。"""
        focused = self.focused
        if focused is None:
            return False
        if focused.id == "git-log":
            return True
        return any(a.id == "git-log" for a in focused.ancestors)

    # ---------- Markdown 预览（v0.36） ----------

    def _open_markdown_preview(self, relative: str, absolute: str) -> None:
        """打开只读 Markdown 预览；>512 KiB 在推屏前拦截（V36 P1）。

        - 入口闸门是**推屏前**拦截：不出现「预览屏闪一下再报错」；
        - ``getsize`` 失败（文件已删 / 无权限）不拦，交给预览屏的
          ``read_for_preview`` 显示可读原因；
        - 预览不经过 ``_pane_problem``：pane 忙 / 全屏 / 已退出都能看。
        """
        from myrtui.client.mdpreview import (
            MARKDOWN_PREVIEW_MAX_BYTES,
            MarkdownPreviewScreen,
        )

        try:
            size = _os.path.getsize(absolute)
        except OSError:
            size = None
        if size is not None and size > MARKDOWN_PREVIEW_MAX_BYTES:
            self._notify(t("md.too_large"))
            self._focus_file_tree()
            return
        self.push_screen(
            MarkdownPreviewScreen(
                absolute, relative=relative, graphics=self._graphics
            ),
            callback=lambda _result: self._focus_file_tree(),
        )

    def _open_image_preview(self, relative: str, absolute: str) -> None:
        """打开单文件图片预览（不依赖 pane，不联网）。

        v0.4.3 起支持同目录 ←/→ 切换：关闭时把最后看的那张路径传回来，
        文件树光标跟着落上去，接着右键 / 继续看都顺手。
        """
        from myrtui.client.imagepreview import ImagePreviewScreen

        self.push_screen(
            ImagePreviewScreen(
                absolute, relative=relative, graphics=self._graphics
            ),
            callback=self._image_preview_closed,
        )

    def _image_preview_closed(self, result: str | None) -> None:
        """预览关闭：焦点回文件树；翻过图就把光标移到最后看的那张。"""
        self._focus_file_tree()
        if not result:
            return
        try:
            tree = self.query_one("#file-tree", FileTree)
        except NoMatches:
            return
        if not tree.select_path(result):
            log.debug("图片预览关闭后未能定位树节点：%s", result)

    # ---------- 打开到当前 pane（v0.28 → v0.31） ----------

    def _pane_problem(self, pane_id: str) -> str | None:
        """当前 pane 是否可接收打开命令；返回拒绝原因。"""
        if self._dead.get(pane_id):
            return t("app.pane_dead")
        if self._alt_screens.get(pane_id):
            return t("app.pane_alt")
        command = self._commands.get(pane_id)
        if not command:
            return t("app.pane_unknown")
        base = _os.path.basename(command).lstrip("-").lower()
        if base not in self._shell_names:
            return t("app.pane_running", command=command)
        return None

    def _open_in_pane(self, absolute: str) -> None:
        """把打开命令注入当前 pane；失败时提示并回文件树。"""
        self._hide_shortcut_help()
        pane = self._active_pane
        if pane is None:
            self._notify(t("app.no_pane"))
            self._focus_file_tree()
            return
        problem = self._pane_problem(pane)
        if problem is not None:
            self._notify(problem)
            self._focus_file_tree()
            return
        plan = pane_commands.build_open(absolute)
        if plan.notice:
            self._notify(plan.notice)
        if plan.command is None:
            self._focus_file_tree()
            return
        self._submit_job(
            lambda: self.backend.run_command(pane, plan.command),
            label=t("app.job.open_send"),
        )
        self._offsets[pane] = 0
        self._burst()
        self._set_active(self._pane_ids.index(pane))

    def _git_log_visible(self) -> bool:
        """历史面板是否可见（侧栏折叠 → 不可见 → 不取数，D7「仅面板可见时」）。"""
        sidebar = self._sidebar()
        return sidebar.display and sidebar.query_one("#git-log").display

    def _refresh_git_log(self) -> None:
        """取数入口（3s 定时器 / 目标切换事件）→ 启动一次 worker。"""
        try:
            visible = self._git_log_visible()
        except NoMatches:
            # teardown 竞态（同 _tick 的兜底口径）：Textual ``App._shutdown``
            # 先 ``_close_all()``（剪掉 MainScreen 全部子节点，含 #sidebar），
            # 之后才 ``_close_messages()``（这一步才停 App 级定时器）——
            # 这段窗口里本定时器仍可能触发，此时 ``_sidebar()`` 必抛
            # NoMatches。不吞掉的话异常记入 ``app._exception``，
            # ``run_test`` 的 finally re-raise → 测试偶发失败（v0.16 实录：
            # 放大窗口后 100% 复现，真实窗口毫秒级、tick 3s 一次 → 低概率）。
            return
        if not visible:
            return
        self._load_git_log()

    @work(exclusive=True, group="gitlog", exit_on_error=False)
    async def _load_git_log(self) -> None:
        """取数 worker：单一当前目标 + last-write-wins（D7）。

        **形态**：async worker + ``asyncio.to_thread``——``@work(thread=True)``
        配 async 方法是错的组合（线程里只拿到未被 await 的 coroutine，取数
        **静默不执行**、面板永远空白，R9）。

        - 所有查询合并进同一个 exclusive group：新请求取消同组未完成的旧请求；
        - 结果携带**发起时的目标**，回投前校验当前目标一致才写入面板，
          不一致一律丢弃（last-write-wins）。
        """
        target = self._log_target  # 发起时的查询目标
        status, commits = await asyncio.to_thread(gitlog.git_log, self.root, target)
        self._apply_git_log(target, status, commits)

    def _apply_git_log(
        self, target: str | None, status: str, commits: list[gitlog.Commit]
    ) -> None:
        """结果回投：目标已变 → 丢弃（last-write-wins）；否则写进面板。

        单独成方法是为了让这条规则**可单测**（exclusive 取消之外的那道防线：
        「已收尾但尚未回投」的时序）。
        """
        if target != self._log_target:
            return  # 过期结果一律丢弃
        self._show_git_log(status, commits, target)

    def _show_git_log(
        self, status: str, commits: list[gitlog.Commit], target: str | None
    ) -> None:
        """把取数结果写进标题与面板（三态文案见 §9 验收 7）。"""
        # 标题（``GIT <branch>`` / ``GIT <branch> · <相对路径>``）由 Sidebar
        # 渲染——分支数据在它手里；App 只告知「当前看的是哪个文件」。
        try:
            self._sidebar().set_scope(target)
            panel = self.query_one("#git-log", GitLogPanel)
        except NoMatches:
            # teardown 竞态（同 _refresh_git_log 的兜底口径）：worker 回投时
            # DOM 可能已在拆卸。worker 虽有 exit_on_error=False 不会炸 app，
            # 但吞掉可免 teardown 期间的报错噪音。
            return
        if status == "not-repo":
            panel.show_message(t("git.not_repo"))
        elif status == "unavailable":
            panel.show_message(t("git.unavailable"))
        elif not commits:
            panel.show_message(
                t("git.no_commits_file") if target else t("git.no_commits_branch")
            )
        else:
            panel.show_commits(commits)

    def _update_tabs(self) -> None:
        self.query_one(TopBar).set_tabs(
            self._tab_labels, self._tab_deads, self._active_index
        )

    # ---------- 生命周期 ----------

    async def on_event(self, event) -> None:
        """分隔线拖拽的 **App 级前置截获**（v0.11.4）+ 热区按下不算点击。

        **为什么前置截获**：用户真机复盘——同一份代码 macOS Terminal 直跑
        正常，**Windows Terminal + SSH 远程下左键拖不动**（v0.11.2 时代右键
        反而能拖）。Textual 的解析层对按钮对称，所见差异只能出在 Screen/
        widget 层的分发细节上（左键才有的文本选择管道、``capture_mouse``
        在 SSH 链路下的时序等）。因此把拖拽的 down/move/up 三步全部收到
        **App 层分发之前**处理，拖拽不再依赖 widget 层转发链与捕获：

        - ``MouseDown``：左键 + 命中热区 → 记账 + 起拖 + 复刻焦点切换
          （按在 pane 上 → 切到该 pane；按在线本体/侧栏 → 不动焦点），然后
          **跳过基类分发直接 return**——MouseDown 不接触任何 widget，
          click 天然不生成（拖分隔线本就不该产生 click，v0.11.1 语义）；
        - ``MouseMove``：拖拽活跃且按钮按住 → 直接 ``_drag_to``；
          按钮为 0（指针已在窗口外松开）→ 收摊（防鼠标被永久挂住）；
        - ``MouseUp``：拖拽活跃 → 收摊 + 落盘。
        未命中热区/非左键/拖拽未活跃的事件照旧走基类分发。widget 层的
        ``press_near_separator`` / ``on_separator_mouse_*`` 路径**保留**：
        pilot 与直接 ``screen._forward_event`` 的流量（测试）绕过本文件、
        走 widget 层与 capture 路由；两条路径幂等（重复起拖是 no-op）。

        **热区按下不算点击**（v0.11.1 口径）：截获路径根本不进基类，
        ``_mouse_down_widget`` 不会被记录 → click 不会生成；未截获而命中
        热区的照旧把 ``_mouse_down_widget`` 抹掉兜底。

        **诊断**：``MYRTUI_MOUSE_DEBUG=1`` 启动时，把每个鼠标事件与拖拽
        结论写进 ``<tmp>/myrtui-mouse-debug-<pid>.log``。
        """
        if isinstance(event, (MouseDown, MouseMove, MouseUp)) and not event.is_forwarded:
            sx, sy = getattr(event, "screen_x", None), getattr(event, "screen_y", None)
            _mouse_debug(
                f"{type(event).__name__} button={event.button} "
                f"screen=({sx},{sy}) drag_sep={self._drag_sep}"
            )
            if sx is not None and sy is not None:
                if isinstance(event, MouseDown):
                    near = self._separator_near(sx, sy)
                    if near is not None and event.button in DRAG_BUTTONS:
                        _mouse_debug(f"  -> drag start: {near.id}")
                        self._begin_app_drag(near, event)
                        return
                elif isinstance(event, MouseMove) and self._drag_sep is not None:
                    if event.button:
                        self._drag_to(self._drag_sep, event)
                        _mouse_debug(f"  -> drag move, ratios={self._ratios}")
                    else:
                        _mouse_debug("  -> drag end: button released outside")
                        self._finish_separator_drag()
                    return
                elif isinstance(event, MouseUp) and self._drag_sep is not None:
                    _mouse_debug("  -> drag end: mouse up")
                    self._finish_separator_drag()
                    return
                # 未截获的 MouseDown 仍要走「热区不算点击」兜底
                near = (
                    self._separator_near(sx, sy) if isinstance(event, MouseDown)
                    else None
                )
            else:
                near = None
            await super().on_event(event)
            if near is not None:
                self._mouse_down_widget = None
            return
        await super().on_event(event)

    def on_mount(self) -> None:
        # 分隔线控件缓存（热区判定每帧都要用）
        self._separators = {
            sep_id: self.query_one(f"#{sep_id}", Separator)
            for sep_id in SEPARATOR_SPECS
        }
        # 先落比例（attach 恢复上次的位置），再同步拓扑
        self._apply_ratios()
        self._sync_topology()
        # 单一定时器 + 尾部延迟控制（§8）：高频段 30ms，空闲段 IDLE_INTERVAL
        self._tick_timer = self.set_interval(TICK, self._tick)
        # P3：当前目标的 git log 3s 一刷（仅面板可见时取数）
        self._gitlog_timer = self.set_interval(GITLOG_INTERVAL, self._refresh_git_log)
        self.call_after_refresh(self._refresh_git_log)  # 首帧就有历史
        if self._takeover_path or self._stop_path:
            # §6 P1：持锁者每 500ms 读旁挂标记，两级校验全过才让位；
            # stop 标记同周期读取（实例管理计划 §3.4），有效则整个退场
            self.set_interval(SENTRY_INTERVAL, self._check_takeover)
        if self._loop_debug:
            self._install_loop_probe()

    def _install_loop_probe(self) -> None:
        """``MYRTUI_LOOP_DEBUG=1``：事件循环迟到遥测（§4.2 可选，§7.2 数据源）。

        25ms 探针记录迟到 > 30ms（§1-1 目标线）的事件到 stderr——把真机
        验收的主观手感变成量化分布。默认关闭时零开销（一个 bool 判断）。
        """
        state = {"last": time.monotonic()}

        def probe() -> None:
            now = time.monotonic()
            late = now - state["last"] - LOOP_PROBE_INTERVAL
            if late > LOOP_DEBUG_THRESHOLD:
                print(
                    f"[myrtui] loop late {late * 1000:.1f}ms",
                    file=sys.stderr, flush=True,
                )
            state["last"] = now

        self._loop_probe_timer = self.set_interval(LOOP_PROBE_INTERVAL, probe)

    def on_unmount(self) -> None:
        # App teardown 期间 _tick 可能再跑一轮，此时 DOM 拆卸中 query_one
        # 会抛 NoMatches → 先停定时器，_tick 内再兜底
        if self._tick_timer is not None:
            self._tick_timer.stop()
            self._tick_timer = None
        if self._prefix_timer is not None:
            self._prefix_timer.stop()
        if self._gitlog_timer is not None:
            self._gitlog_timer.stop()
            self._gitlog_timer = None
        if self._loop_probe_timer is not None:
            self._loop_probe_timer.stop()
            self._loop_probe_timer = None
        self._cancel_shortcut_help_timers()
        # v0.35：teardown 时停掉解冻 / hold 定时器，避免回调打到已拆卸的 DOM
        self._cancel_thaw_timer()
        self._gesture = None
        self._hold_selection = False
        self._frozen_pane = None
        self._selection_pane = None
        # B1（§3.3-1）：标记执行器关闭——后续提交直接丢弃；in-flight 回调
        # 检查该标记丢弃结果不 apply（DOM 已拆）。已入队任务照常执行完
        # （shutdown(wait=False) 不取消已排队项，进程退出时 atexit join
        # 兜底完成，action_kill 的 kill-server 靠这条落地），只是结果被丢弃。
        self._executor_closed = True
        self._executor.shutdown(wait=False)

    # ---------- 接管哨戒（§6 P1，M15/M21） ----------

    def _check_takeover(self) -> None:
        for pid, ts in read_takeover_markers(self._takeover_path or ""):
            if is_marker_valid(pid, ts):
                log.info("收到接管请求（pid=%s），让位退出", pid)
                self.exit()  # 让位 = 干净退出；shell 无损（§6）
                return
        # 实例管理：stop 标记单独一个文件、单独前缀（评审 R6——不复用
        # takeover 文件，避免让位与停止互相干扰）；有效 = 整个退场
        for pid, ts in read_takeover_markers(
            self._stop_path or "", prefix="stop"
        ):
            if is_marker_valid(pid, ts):
                log.info("收到 stop 请求（pid=%s），退场", pid)
                self.exit()  # 停止 = 干净退出；之后由 stop 侧 kill 会话
                return

    # ---------- 自适应轮询 ----------

    def _tick(self) -> None:
        now = time.monotonic()
        # motion 合并（v0.7）：与 capture 同节拍驱动，30ms 一条
        if self._pending_motion is not None:
            self._flush_motion(now)
        if self._high_ticks > 0 or now >= self._next_poll:
            try:
                self._request_poll()
            except NoMatches:
                # teardown 竞态：定时器触发到执行之间 DOM 已在拆卸。
                # B1 后提交本身不碰 DOM（_build_offsets 只读纯数据 dict），
                # 但提交路径既有防线按 §3.3-3 原样保留
                return
            if self._high_ticks > 0:
                self._high_ticks -= 1
                self._next_poll = now + TICK
            else:
                self._next_poll = now + self._idle_interval  # B0：可调空闲节拍

    def _burst(self) -> None:
        """输入/paste 后：立即 capture 一次，随后保持几个 30ms tick（§8）。"""
        self._high_ticks = HIGH_FREQ_TICKS
        self._request_poll()

    def _request_poll(self) -> None:
        """poll 请求入口（tick / burst / scroll）：单飞 + 尾部合并（§3.1-3）。

        重载下 poll 单次 30ms+，若每次 tick 都入队队列会无限增长——
        in-flight 期间的请求只置 ``pending=1``（不入队），完成时有 pending
        则再提交一次并清位 → 实际并发 poll 恒 ≤ 1、队列深度恒 ≤ 1。
        实际提交的 offsets 在**提交时刻**重建（滚动目标永远取最新，§3.1-3）。
        """
        if self._executor_closed:
            return  # teardown 后不再提交（§3.3-1）
        if self._poll_in_flight:
            self._poll_pending = True
            return
        self._poll_submit(depth=0)

    def _poll_submit(self, depth: int) -> None:
        if self._executor_closed:
            return
        self._poll_in_flight = True
        self._poll_pending = False
        self._poll_depth = depth
        # offsets / 槽位快照必须在事件循环侧构建（DOM/状态读，§3.1-3）
        fut = asyncio.get_running_loop().run_in_executor(
            self._executor,
            self._poll_fetch,
            self._build_offsets(),
            list(self._pane_ids),
            depth,
        )
        fut.add_done_callback(self._poll_done)

    def _poll_done(self, fut) -> None:
        """poll 回投（事件循环侧）：单飞状态机推进 + apply + 合并重提交。

        apply 回调只经 executor 的 future 产生 → 完成顺序 = 单线程 FIFO
        执行顺序 = 提交顺序（§3.1-4 回投顺序保证）。
        """
        self._poll_in_flight = False
        depth = self._poll_depth
        if self._executor_closed:
            fut.exception()  # retrieve（§3.3-4）；结果丢弃（DOM 已拆）
            return
        try:
            outcome = fut.result()  # _poll_fetch 已把 TmuxError 转成 outcome
        except Exception as exc:  # 防御：fetch 段未预期异常不得静默
            log.warning("poll 异常: %s", exc, exc_info=True)
            outcome = _PollOutcome(error=str(exc))
        retry = self._poll_apply(outcome, depth)
        if retry:
            self._poll_pending = False  # 拓扑重试占用合并槽位（等价旧内联重试）
            self._poll_submit(depth + 1)
        elif self._poll_pending:
            self._poll_submit(0)

    def _poll_fetch(
        self,
        offsets: dict[str, tuple[int, int]],
        known_ids: list[str],
        depth: int,
    ) -> _PollOutcome:
        """poll 的**线程段**（§3.2-1）：capture + TmuxError/漂移重试链，纯数据。

        对应旧 ``_poll`` 的两段式：整条 capture 失败可能是槽位指向已消失的
        pane（外部 kill-window）→ 先探 ``list-panes`` 拿真实拓扑，漂移则把
        探测结果带回循环侧做拓扑同步后重试；重试预算一轮（旧代码每轮
        ``_poll`` 至多两次 ``backend.poll``，depth=1 时不再探、不再重试）。
        """
        try:
            panes, captures = self.backend.poll(offsets)
        except TmuxError as exc:
            probe = self._probe_panes()
            if (
                depth == 0
                and probe is not None
                and [p.pane_id for p in probe] != known_ids
            ):
                return _PollOutcome(probe=probe, need_retry=True)
            return _PollOutcome(error=str(exc))
        if depth == 0 and _live_ids_of(panes) != known_ids:
            # 会话侧拓扑漂移（外部新建/关闭 window）：漂移判定用本次 poll
            # 已抓到的 panes，不再额外发 list-panes（N14）；重试前的拓扑
            # 同步（旧 _sync_topology）回投循环侧执行
            return _PollOutcome(
                panes=panes, captures=captures,
                probe=self._probe_panes(), need_retry=True,
            )
        return _PollOutcome(panes=panes, captures=captures, offsets=offsets)

    def _probe_panes(self) -> list[PaneInfo] | None:
        """拓扑探测（线程段）：失败告警并返回 None（对应旧 _sync_topology
        内部 ``log.warning("list-panes 失败")`` + return 的口径）。"""
        try:
            return self._topology_fetch()
        except TmuxError as exc:
            log.warning("list-panes 失败: %s", exc)
            return None

    def _poll_apply(self, outcome: _PollOutcome, depth: int) -> bool:
        """poll 的**循环段**（§3.2-1）：dead/offset/cache/标签/状态栏 + 兜底。

        返回 True = 需要以 depth+1 重提交（拓扑漂移，旧 ``_poll`` 的内联
        重试等价物）；重试预算只有一轮（depth 封顶）。
        """
        if outcome.error is not None:
            log.warning("poll 失败: %s", outcome.error)
            self._poll_failed()
            return False
        if outcome.panes is None:
            # 漂移重试路径：先把探测到的新拓扑 apply（旧 _sync_topology
            # 在重试 poll 之前执行），再重提交
            if outcome.probe is not None:
                self._topology_apply(outcome.probe)
            return bool(outcome.need_retry and depth == 0)
        self._poll_ok()
        if outcome.need_retry:
            if outcome.probe is not None:
                self._topology_apply(outcome.probe)
            return depth == 0
        try:
            self._poll_render(outcome)
        except NoMatches:
            # teardown 竞态（v0.16 口径，沿用旧 _tick 的兜底）：回投时 DOM
            # 已拆卸——worker 回调不会有 app 级兜底，必须在 apply 段吞掉
            return False
        return False

    def _build_offsets(self) -> dict[str, tuple[int, int]]:
        """当前槽位 → (k, height) 映射；K 先 clamp 到 history_size-1（M12）。"""
        offsets = {}
        for pid in self._pane_ids:
            k = min(self._offsets.get(pid, 0),
                    max(0, self._history_sizes.get(pid, 0) - 1))
            # height 用上次 list-panes 缓存（首次 poll offset 必为 0，不需要 H）
            offsets[pid] = (k, self._heights.get(pid, 0))
        return offsets

    def _poll_failed(self) -> None:
        """一次 capture 失败（§9）：累计计数，到限置「失去连接」并进入重连提示。"""
        self._fail_count += 1
        if self._fail_count >= FAIL_LIMIT:
            self._disconnected = True
        self._refresh_status()

    def _poll_ok(self) -> None:
        """capture 成功：清零失败计数；从断线态恢复则给出「已重新连接」提示。"""
        self._fail_count = 0
        if self._disconnected:
            self._disconnected = False
            self._reconnect_until = time.monotonic() + RECONNECT_NOTICE

    def _notify(self, text: str, seconds: float = NOTICE_SECONDS) -> None:
        """顶栏瞬时提示（v0.13）：到点自动过期。

        与「已重新连接」同款机制（``_notice_until`` 时间戳 + ``_refresh_status``
        统一写入口）——顶栏每 tick 都会被状态刷新覆盖，所以瞬时提示必须靠
        时间戳自己撑住，否则下一轮 poll 就没了。
        """
        self._notice_text = text
        self._notice_until = time.monotonic() + seconds
        self._refresh_status()

    def _refresh_status(self) -> None:
        """顶栏状态的唯一写入口（P2：断线/重连/dead 提示的优先级在此定死）。

        优先级：失去连接 > 已重新连接（限时）> **瞬时提示（限时，v0.13）** >
        dead pane 提示 > 侧栏聚焦提示 > 无。
        ``TopBar.set_status`` 自身全等比较，重复调用不产生渲染开销。
        """
        topbar = self.query_one(TopBar)
        if self._disconnected:
            topbar.set_status(t("app.disconnected"))
            return
        if time.monotonic() < self._reconnect_until:
            topbar.set_status(t("app.reconnected"))
            return
        if time.monotonic() < self._notice_until:
            topbar.set_status(self._notice_text)
            return
        dead = sum(1 for pid in self._pane_ids if self._dead.get(pid))
        if dead > 0:
            if dead >= len(self._pane_ids):
                topbar.set_status(t("app.all_dead"))
            else:
                topbar.set_status(t("app.some_dead", count=dead))
            return
        if self._sidebar_focused():
            # 侧栏聚焦提示（D8：让「键归侧栏」的规则可见）；v0.22 起区分
            # 文件树 / Git 历史两个焦点目标，``Ctrl-a s`` / ``Ctrl-a g`` 对称
            if self._git_focused():
                topbar.set_status(t("app.git_focused"))
            else:
                topbar.set_status(t("app.sidebar_focused"))
            return
        topbar.set_status("")

    def _poll_render(self, outcome: _PollOutcome) -> None:
        """poll apply 段主体（旧 ``_poll`` 的后半段，逐行沿用 N16 防闪烁 diff）。"""
        panes = outcome.panes or []
        captures = outcome.captures or {}
        requested = outcome.offsets or {}
        info_by_id = {p.pane_id: p for p in panes}
        labels: list[str] = []
        deads: list[bool] = []
        for i, pid in enumerate(self._pane_ids):
            widget = self.query_one(f"#pane-{i}", PaneWidget)
            info = info_by_id.get(pid)
            cap = captures.get(pid)
            if info is None or cap is None:
                # 槽位占位（拓扑刚变、下轮 poll 才有数据）
                labels.append(f"{i + 1}:—")
                deads.append(False)
                continue
            # 鼠门控逐 tick 刷新（程序可能中途启停鼠标，如 opencode 启动后
            # 才请求 1003）——all 直读 1003；编码位（sgr）决定发 SGR 还是
            # xterm 旧 3 字节（审阅 F3）
            self._mouse_levels[pid] = decode_level(
                info.mouse_standard, info.mouse_button, info.mouse_any,
                info.mouse_all,
            )
            self._mouse_sgr[pid] = info.mouse_sgr
            # dead 状态与死亡先后（P2 respawn UX：alive→dead 追加到尾部）
            was_dead = self._dead.get(pid, False)
            self._dead[pid] = info.dead
            if info.dead and not was_dead:
                if pid in self._dead_order:
                    self._dead_order.remove(pid)
                self._dead_order.append(pid)
            elif not info.dead and pid in self._dead_order:
                self._dead_order.remove(pid)
            # 备用屏切换时清零 offset（§11）
            if self._alt_screens.get(pid) != info.alternate_on:
                self._alt_screens[pid] = info.alternate_on
                self._offsets[pid] = 0
            self._history_sizes[pid] = info.history_size
            self._heights[pid] = info.height
            self._commands[pid] = info.command
            self._window_ids[pid] = info.window_id
            # backend 断言失败可能收紧了 offset，同步回来；但 B1 后 apply 与
            # 提交之间可能有滚动/回底写入（in-flight 期间），只有提交时刻的
            # 值（clamp 后）未被改动时才回写——否则会把用户滚动的新 offset
            # 覆盖成旧值（§3.1-3 合并 offset 在回调时刻重建的配套防线）
            req_k = requested.get(pid, (0, 0))[0]
            cur_k = self._offsets.get(pid, 0)
            hsize = self._history_sizes.get(pid, 0)
            if min(cur_k, max(0, hsize - 1)) == req_k:
                self._offsets[pid] = cap.offset

            live = cap.offset == 0
            cursor = (
                (info.cursor_x, info.cursor_y)
                if live and info.cursor_flag and not info.dead
                else None
            )
            # IME 锚点（v0.18）：记下该 pane 最后一次有效内层光标，供光标
            # 不可用（vim 隐藏/dead/翻 scrollback）时回退
            if cursor is not None:
                self._last_cursors[pid] = cursor
            # 防闪烁（N16）：capture 字串 + 光标 + dead 全等比较，无 diff 不重建
            diff_key = (cap.text, cursor, info.dead)
            # v0.35：拖拽中冻结 pane 的画面（不更新 _cache，解冻后靠 pop 强制
            # 重建）；元数据（鼠标级别 / offset / dead / 宽高等）照常更新
            if pid != self._frozen_pane and self._cache.get(pid) != diff_key:
                self._cache[pid] = diff_key
                lines = [
                    parse_sgr_line(line, info.width)
                    for line in cap.text.splitlines()
                ]
                widget.set_content(lines, cursor, info.dead)
            # v0.35：手势 / 冻结 pane 变 dead → 立即收摊（画面已失效，不再复制）
            if info.dead and (
                self._frozen_pane == pid
                or (self._gesture is not None and self._gesture.pane_id == pid)
            ):
                self._cancel_gesture()
            labels.append(
                f"{i + 1}:dead" if info.dead else f"{i + 1}:{info.command}"
            )
            deads.append(info.dead)
        if labels != self._tab_labels or deads != self._tab_deads:
            self._tab_labels, self._tab_deads = labels, deads
            self._update_tabs()
        self._refresh_status()
        # IME 锚点（v0.18）：每轮抓屏后对齐活动 pane 的光标（无 diff 时
        # 也是幂等 no-op，只发一次 CUP 或什么都不发）
        self._sync_ime_anchor()

    # ---------- 滚动（§11） ----------

    def scroll_pane(self, pane_id: str, direction: int) -> None:
        """滚轮：direction +1 上翻 / -1 下翻。备用屏内忽略。"""
        if self._alt_screens.get(pane_id):
            return  # 备用屏历史不可访问，直接忽略 wheel（§11）
        hsize = self._history_sizes.get(pane_id, 0)
        max_off = max(0, hsize - 1)  # 安全上限 history_size-1（M12）
        if max_off == 0:
            return
        new = self._offsets.get(pane_id, 0) + direction * SCROLL_STEP
        self._offsets[pane_id] = max(0, min(new, max_off))
        self._high_ticks = HIGH_FREQ_TICKS
        self._request_poll()  # B1：单飞提交（合并语义见 _request_poll）

    # ---------- 鼠标转发（v0.7 §10.2） ----------

    def _widget_for_pid(self, pane_id: str) -> PaneWidget | None:
        for i in range(MAX_PANES):
            w = self.query_one(f"#pane-{i}", PaneWidget)
            if w.pane_id == pane_id and w.display:
                return w
        return None

    def _coords_in(self, target: PaneWidget, event) -> tuple[int, int]:
        """事件坐标换算到目标 pane **内容区**内（screen 坐标 − 内容区原点，越界 clamp）。

        同 widget 事件两种算法等价（探针实测）；跨 widget（释放/拖动落在
        别处）必须走 screen 换算，否则拿到的是别家的相对坐标。
        v0.11.2 起 pane 有 1 cell 永久边框：tmux 窗口尺寸对齐的是**内容区**
        （见 ``PaneWidget.on_resize``），鼠标坐标也必须相对内容区，否则转发
        给内层程序的坐标整体偏移 1。
        """
        region = target.content_region
        w, h = region.width, region.height
        if event.screen_x is not None and event.screen_y is not None:
            x = int(event.screen_x) - region.x
            y = int(event.screen_y) - region.y
        else:
            x, y = int(event.x), int(event.y)
        return max(0, min(x, max(0, w - 1))), max(0, min(y, max(0, h - 1)))

    def _send_mouse(self, pane_id: str, seq: str, *, burst: bool = False) -> None:
        # B1 §3.2-5：注入提交即返（FIFO 保序）；错误经 future errback 记录
        #（口径同旧 log.warning("鼠标注入失败")）。burst 语义 = 合并请求一次 poll
        self._submit_job(
            lambda: self.backend.send_mouse(pane_id, seq),
            label=t("app.job.mouse"),
        )
        if burst:
            self._burst()  # 点击/滚轮/释放：立即 capture 看程序回显（§8）
        else:
            self._high_ticks = HIGH_FREQ_TICKS  # motion：随后几 tick 提频捕回显

    def _queue_motion(self, pane_id: str, seq: str) -> None:
        """motion 节流：≥30ms 一条，间隔内尾部合并（只留最新位置）。"""
        now = time.monotonic()
        if now - self._last_motion_sent >= MOTION_INTERVAL:
            self._last_motion_sent = now
            self._send_mouse(pane_id, seq)
        else:
            self._pending_motion = (pane_id, seq)

    def _flush_motion(self, now: float) -> None:
        """由 _tick 驱动（每 30ms）：把合并的最后一条 motion 发出去。"""
        if self._pending_motion is None:
            return
        if now - self._last_motion_sent < MOTION_INTERVAL:
            return
        pane_id, seq = self._pending_motion
        self._pending_motion = None
        if pane_id not in self._pane_ids:
            return  # 拖动期间 pane 被关：丢弃
        self._last_motion_sent = now
        self._send_mouse(pane_id, seq)

    # ---------- 左键手势：拖拽选字 + 松手自动复制（v0.35） ----------

    def _event_screen_coords(self, event) -> tuple[int, int]:
        """事件 → screen 坐标（坐标缺失时退回 widget 相对坐标，防御性分支）。

        用 screen 坐标判定拖拽距离：指针拖出 pane 边界后 ``event.x`` 会按
        **别的 widget** 重新起算，只有 screen 坐标能连续比较。
        """
        sx = getattr(event, "screen_x", None)
        sy = getattr(event, "screen_y", None)
        if sx is None or sy is None:
            return int(event.x), int(event.y)
        return int(sx), int(sy)

    def _clear_screen_selection(self) -> None:
        """清掉 Textual 的选区（teardown / 模态下静默跳过）。"""
        clear = getattr(self.screen, "clear_selection", None)
        if clear is not None:
            clear()

    def _start_gesture(self, widget: PaneWidget, event) -> None:
        """左键按下：冻结画面、开始本地选区（不向程序注入 press）。"""
        self._cancel_thaw_timer()
        self._hold_selection = False
        # 丢掉队列里的 hover / 旧拖动 motion，别在拖拽中补发到程序
        self._pending_motion = None
        self._thaw_pane()  # 先清掉旧 pane 的冻结 / 选区归属
        pid = widget.pane_id
        x, y = self._coords_in(widget, event)
        sx, sy = self._event_screen_coords(event)
        self._gesture = _PointerGesture(
            pane_id=pid,
            button=int(event.button),
            down_x=x,
            down_y=y,
            down_screen_x=sx,
            down_screen_y=sy,
            level=self._mouse_levels.get(pid, LEVEL_NONE),
            sgr=self._mouse_sgr.get(pid, True),
            shift=bool(event.shift),
            meta=bool(event.meta),
            ctrl=bool(event.ctrl),
        )
        self._selection_pane = pid
        self._freeze_pane(pid)
        self._schedule_thaw(GESTURE_TIMEOUT)  # 安全超时：mouse-up 丢失不永久冻结
        # 左键按下不注入：程序只在「点击」路径的松手时收到合成事件
        self._mouse_down_pane = None
        self._mouse_down_forwarded = False

    def _update_gesture_drag(self, event) -> None:
        """拖动中：只更新 dragging 标志，不注入任何事件。"""
        g = self._gesture
        if g is None:
            return
        sx, sy = self._event_screen_coords(event)
        if max(
            abs(sx - g.down_screen_x), abs(sy - g.down_screen_y)
        ) >= DRAG_THRESHOLD_CELLS:
            g.dragging = True

    def _finish_gesture(self, event) -> None:
        """左键松手：拖拽 → 自动复制；点击 → 合成 press+release。"""
        g = self._gesture
        if g is None or g.button != 1:
            return
        self._gesture = None
        if g.dragging:
            # 先取文本再解冻：MainScreen.get_selected_text 依赖 _selection_pane，
            # 而 _thaw_pane 会把它清掉。
            screen = self.screen
            text = (
                screen.get_selected_text()
                if isinstance(screen, MainScreen)
                else None
            )
            if text and text.strip():
                if self._copy_on_select:
                    self.copy_text(text)
                    self._schedule_thaw(THAW_DELAY)  # 高亮保留 600ms
                else:
                    self._hold_selection = True
                    self._schedule_thaw(HOLD_TIMEOUT)
                    self._notify(t("app.selection", count=len(text)))
            else:
                self._clear_screen_selection()
                self._thaw_pane()
            return
        # 点击：清选区 / 解冻，然后（若按下时程序请求了鼠标）合成一次点击
        self._clear_screen_selection()
        self._thaw_pane()
        if (
            can_forward(g.level, "press")
            and g.pane_id in self._pane_ids
            and not self._dead.get(g.pane_id, False)
        ):
            coords = dict(
                shift=g.shift, meta=g.meta, ctrl=g.ctrl
            )
            seq = (
                press(g.down_x, g.down_y, g.button, g.sgr, **coords)
                + release(g.down_x, g.down_y, g.button, g.sgr, **coords)
            )
            self._send_mouse(g.pane_id, seq, burst=True)

    def on_pointer_up(self, event) -> None:
        """统一释放入口：左键手势 → ``_finish_gesture``；否则旧转发路径。"""
        if self._gesture is not None and self._gesture.button == 1:
            self._finish_gesture(event)
            return
        self._forward_release(event)

    def _freeze_pane(self, pane_id: str) -> None:
        """冻结 pane 画面：拖拽期间 capture 不再覆盖它（选区才不会漂）。"""
        if self._frozen_pane == pane_id:
            return
        if self._frozen_pane is not None:
            self._thaw_pane()  # 旧 pane 先解冻（会清旧 _selection_pane）
        self._sync_ime_anchor()  # 冻结前对齐一次；之后 _sync_ime_anchor 跳过
        self._frozen_pane = pane_id

    def _thaw_pane(self) -> None:
        """解冻：丢弃冻结画面缓存 + 立即取最新内容 + 清选区归属。"""
        pid = self._frozen_pane
        self._frozen_pane = None
        self._selection_pane = None  # 选区归属只在本次手势 / 高亮期内有效
        self._cancel_thaw_timer()
        if pid:
            self._cache.pop(pid, None)  # 强制下一轮重绘最新内容
            self._request_poll()
        self._sync_ime_anchor()

    def _schedule_thaw(self, seconds: float) -> None:
        self._cancel_thaw_timer()
        self._thaw_timer = self.set_timer(seconds, self._thaw_after)

    def _thaw_after(self) -> None:
        self._cancel_thaw_timer()
        self._hold_selection = False
        self._gesture = None  # 安全超时：手势也一并取消
        self._clear_screen_selection()
        self._thaw_pane()

    def _cancel_thaw_timer(self) -> None:
        if self._thaw_timer is not None:
            self._thaw_timer.stop()
            self._thaw_timer = None

    def _cancel_gesture(self) -> None:
        """收摊：resize / dead / 按键 / 滚轮 / 失焦 / mouse-up 丢失的统一出口。"""
        self._gesture = None
        self._hold_selection = False
        self._clear_screen_selection()
        self._thaw_pane()

    def _has_pending_selection(self) -> bool:
        """高亮 / hold / 手势任一在进行中。"""
        return (
            self._gesture is not None
            or self._frozen_pane is not None
            or self._hold_selection
        )

    def on_pane_mouse_down(self, widget: PaneWidget, event) -> None:
        pid = widget.pane_id
        level = self._mouse_levels.get(pid, LEVEL_NONE)
        use_gesture = int(event.button) == 1 and (
            self._drag_select_mode == "always"
            or (self._drag_select_mode == "app" and level == LEVEL_NONE)
        )
        if use_gesture:
            self._start_gesture(widget, event)
            return
        # 旧路径：右键 / 中键 / MYRTUI_DRAG_SELECT=app 下程序抢鼠标
        self._mouse_down_pane = pid
        self._mouse_down_forwarded = can_forward(
            self._mouse_levels.get(pid, LEVEL_NONE), "press"
        )
        if not self._mouse_down_forwarded:
            return  # 程序未请求鼠标 → 按下只切焦点，不注入
        x, y = self._coords_in(widget, event)
        seq = press(x, y, int(event.button), self._mouse_sgr.get(pid, True),
                    shift=event.shift, meta=event.meta, ctrl=event.ctrl)
        self._send_mouse(pid, seq, burst=True)

    def _forward_release(self, event) -> None:
        """释放：恒路由回按下发生的 pane（配对完整性），坐标 screen 换算 + clamp。

        PaneWidget 落点本 pane 时已 stop；落在其他 pane / 顶栏 / 侧栏的释放
        分别由 PaneWidget.on_mouse_up（stop）与 App.on_mouse_up（冒泡）调进来。
        """
        pid = self._mouse_down_pane
        forwarded = self._mouse_down_forwarded
        self._mouse_down_pane = None
        self._mouse_down_forwarded = False
        if pid is None or not forwarded:
            return  # 按下未转发（程序无鼠标 / 按下在 pane 外）→ 不发孤儿释放
        target = self._widget_for_pid(pid)
        if target is None:
            return
        x, y = self._coords_in(target, event)
        seq = release(x, y, int(event.button), self._mouse_sgr.get(pid, True),
                      shift=event.shift, meta=event.meta, ctrl=event.ctrl)
        self._send_mouse(pid, seq, burst=True)

    def on_pane_mouse_move(self, widget: PaneWidget, event) -> None:
        # 指针靠近分隔线 → 那条线提亮（pane 内的 move 被 PaneWidget stop 掉了，
        # 走不到 App.on_mouse_move）
        self._update_separator_hover(event.screen_x, event.screen_y)
        if self._gesture is not None:
            if not event.button:
                # 手势中收到 button=0 的 move = mouse-up 丢失 → 收摊、不复制
                self._cancel_gesture()
                return
            self._update_gesture_drag(event)
            return
        if event.button:
            # 拖动：目标恒为按下发生的 pane（配对；跨 pane 拖动时坐标
            # screen 换算并 clamp 进该 pane），门控也按该 pane 的级别
            down = self._mouse_down_pane
            if down is None:
                return  # 按下发生在 pane 外 → 不注入
            target = self._widget_for_pid(down)
            if target is None:
                return
            if not can_forward(self._mouse_levels.get(down, LEVEL_NONE), "drag"):
                return
            x, y = self._coords_in(target, event)
            self._queue_motion(down, motion(
                x, y, int(event.button), self._mouse_sgr.get(down, True),
                shift=event.shift, meta=event.meta, ctrl=event.ctrl
            ))
            return
        # hover：指针下 pane 请求了 1003（ALL）才转发
        pid = widget.pane_id
        if not can_forward(self._mouse_levels.get(pid, LEVEL_NONE), "motion"):
            return
        x, y = self._coords_in(widget, event)
        self._queue_motion(pid, motion(
            x, y, 0, self._mouse_sgr.get(pid, True),
            shift=event.shift, meta=event.meta, ctrl=event.ctrl
        ))

    def on_pane_wheel(self, widget: PaneWidget, event, direction: int) -> None:
        """滚轮：程序请求鼠标（1000+）→ 转发；否则走 scrollback（§11）。

        **shift+滚轮 = 逃生舱**：无视程序鼠标请求、强制翻 scrollback——
        否则 mouse-on 的程序会永久吃掉该 pane 的历史翻页入口。

        v0.35：高亮 / hold / 手势期间滚轮 → 先收摊解冻，再按正常逻辑处理。
        """
        if self._has_pending_selection():
            self._cancel_gesture()
        pid = widget.pane_id
        if event.shift or not can_forward(
            self._mouse_levels.get(pid, LEVEL_NONE), "wheel"
        ):
            self.scroll_pane(pid, direction)
            return
        x, y = self._coords_in(widget, event)
        seq = wheel(direction > 0, x, y, self._mouse_sgr.get(pid, True),
                    shift=event.shift, meta=event.meta, ctrl=event.ctrl)
        self._send_mouse(pid, seq, burst=True)

    def on_mouse_down(self, event) -> None:
        # v0.35：高亮 / hold / 手势期间的按下（走到这里 = 按在 pane 外：overlay /
        # 分隔线 / 侧栏 / 顶栏）→ 先收摊解冻。PaneWidget 会 stop pane 内的按下，
        # 所以真正落在 pane 上的手势不会走到这里。
        if self._has_pending_selection():
            self._cancel_gesture()
        # v0.22：快捷键浮层也是屏幕级 overlay。点浮层内部 = 阅读/滚轮，不拖
        # 分隔线、不注入 pane；点浮层外则顺手收起。
        help_panel = self._shortcut_help()
        if help_panel is not None and help_panel.display:
            if help_panel.region.contains(event.screen_x, event.screen_y):
                self._mouse_down_pane = None
                self._mouse_down_forwarded = False
                return
            self._hide_shortcut_help()
        # 提交详情卡片是屏幕级浮层：点卡片内部是「开始选字」，不能被分隔线
        # ±1 热区截成拖拽；点卡片外则顺手关掉卡片（有选区时也不留浮层）。
        card = self._commit_card()
        if card is not None and card.display:
            if card.region.contains(event.screen_x, event.screen_y):
                self._mouse_down_pane = None
                self._mouse_down_forwarded = False
                return
            try:
                self.query_one(GitLogPanel)._hide_card()
            except NoMatches:
                pass
        # 分隔线热区：离分隔线 1 格以内的按下 → 拖分隔线（1 cell 的线难点中）
        if self.press_near_separator(event):
            return
        # PaneWidget stop 掉了 pane 内的按下；走到这里的说明按下在 pane 外
        # （顶栏/侧栏）→ 其后的拖动与释放不路由给任何 pane
        self._mouse_down_pane = None
        self._mouse_down_forwarded = False

    def _commit_card(self) -> CommitCard | None:
        try:
            return self.query_one("#commit-card", CommitCard)
        except NoMatches:
            return None

    def on_mouse_move(self, event) -> None:
        # 指针靠近分隔线 → 那条线提亮（提示「这儿能拖」）
        self._update_separator_hover(event.screen_x, event.screen_y)
        # v0.35：手势拖到 pane 外（顶栏 / 侧栏上空）时继续更新拖拽判定
        if self._gesture is not None:
            if not event.button:
                self._cancel_gesture()
                return
            self._update_gesture_drag(event)
            return
        # PaneWidget stop 掉了 pane 内的移动；走到这里的说明拖动离开了 pane
        # 区域（顶栏/侧栏上空）→ 继续按按下 pane 转发（坐标 clamp）
        if self._mouse_down_pane is None or not event.button:
            return
        target = self._widget_for_pid(self._mouse_down_pane)
        if target is None:
            return
        if not can_forward(
            self._mouse_levels.get(self._mouse_down_pane, LEVEL_NONE), "drag"
        ):
            return
        x, y = self._coords_in(target, event)
        self._queue_motion(self._mouse_down_pane, motion(
            x, y, int(event.button),
            self._mouse_sgr.get(self._mouse_down_pane, True),
            shift=event.shift, meta=event.meta, ctrl=event.ctrl
        ))

    def on_mouse_up(self, event) -> None:
        # 落在 pane 外的释放（pane 内的已被 PaneWidget stop）
        # v0.35：左键手势走 _finish_gesture，右键/中键/app 模式走 _forward_release
        self.on_pointer_up(event)

    # ---------- resize 联动（Textual → resize-window，N7） ----------

    def resize_pane(self, pane_id: str, cols: int, rows: int) -> None:
        """Textual widget 尺寸 → resize-window（N7）。失败记 warning 不中断（§9）。

        用 ``_window_ids`` / ``_last_size`` 缓存：resize 事件可能连续触发，
        每次都跑 list_panes 会白白多出 tmux 调用（P0 验收报告 §3-2）。

        B1（§3.2-6）job 化：首查 list_panes + resize-window 都进线程段；
        ``_last_size`` **提交前乐观置位**（保持连续事件的去重语义——提交即返
        后若不置位，in-flight 期间的同尺寸事件会重复入队），失败/关闭时回滚
        （否则乐观置位 + 任务丢弃会让同尺寸重试被去重永久跳过）。
        """
        # IME 锚点（v0.18）：布局一变 content_region 就移位，选字框锚点得
        # 立刻跟上（Resize 事件在布局帧**之后**才派发，帧尾 move_to 用的是
        # 旧锚点 → 此处补一次即时 CUP；置于去重早退之前，尺寸未变但位置
        # 变了的场景也要同步）
        self._sync_ime_anchor()
        if not pane_id or cols <= 0 or rows <= 0:
            return  # 槽位未分配 / 隐藏控件的 0 尺寸事件
        if self._last_size.get(pane_id) == (cols, rows):
            return  # 尺寸未变，跳过
        prev_size = self._last_size.get(pane_id)
        self._last_size[pane_id] = (cols, rows)  # 乐观置位（去重）；失败回滚

        def fetch():
            window_id = self._window_ids.get(pane_id)
            if window_id is None:
                # 首次（poll 未跑过）才回查一次，之后全走缓存
                info = next(
                    (p for p in self.backend.list_panes() if p.pane_id == pane_id),
                    None,
                )
                if info is None:
                    return None  # pane 已消失：无 window 可 resize
                window_id = info.window_id
            self.backend.resize_window(window_id, cols, rows)
            return window_id

        def apply(window_id: str | None) -> None:
            if window_id is None:
                rollback(None)  # pane 已消失：回滚乐观置位（保持下次重试）
                return
            self._window_ids[pane_id] = window_id

        def rollback(exc) -> None:
            # 失败不保留 _last_size（旧语义：下次还会重试）；但若期间已有
            # 更新的尺寸提交（乐观置位被覆盖），不要把新值回滚成旧值
            if self._last_size.get(pane_id) == (cols, rows):
                if prev_size is None:
                    self._last_size.pop(pane_id, None)
                else:
                    self._last_size[pane_id] = prev_size

        submitted = self._submit_job(
            fetch, apply=apply, label=t("app.job.resize"), on_error=rollback
        )
        if submitted is None:
            rollback(None)  # 执行器已关闭 → 立即回滚，防去重永久跳过

    # ---------- 多 pane 操作（§10.1 / §12 P1） ----------

    def action_new_pane(self) -> None:
        """Ctrl-a t / 顶栏 `+`：新建 pane（自动进入下一布局档）。"""
        if len(self._pane_ids) >= MAX_PANES:
            return
        before = set(self._pane_ids)

        def fetch():
            self.backend.new_window(self.root)
            try:
                return self._topology_fetch()
            except TmuxError as exc:
                # 建窗已成功但 list-panes 失败：照旧走焦点/合并逻辑（旧语义：
                # _sync_topology 内部告警后不动拓扑，之后 _burst 照常执行）
                log.warning("list-panes 失败: %s", exc)
                return None

        def apply(panes: list[PaneInfo] | None) -> None:
            if panes is not None:
                self._topology_apply(panes)
            # 新 pane 不一定落在末槽（tmux 分配 window index 可能补空洞），
            # 按集合差集定位后把焦点跟过去
            fresh = [i for i, pid in enumerate(self._pane_ids) if pid not in before]
            if fresh:
                self._set_active(fresh[0])
            self._burst()

        # B1 §3.2-7：tmux 动作 + list-panes 在线程段，拓扑/焦点在循环段
        self._submit_job(fetch, apply=apply, label=t("app.job.new_window"))

    def _close_busy_message(self, pane_id: str) -> str | None:
        """确认框正文：``None`` = 空闲可直关，非空 = 忙，把它原样交给确认框。

        **判定与文案放在同一个函数里**，调用方只剩一次 ``is None``。v0.5.4 初版
        让这里返回「命令名 / ``""`` / ``None``」三态、由调用方用 ``if busy`` 拼文案，
        那是一个测试覆盖不到的中间态（把 ``if busy`` 写成 ``if busy is not None``，
        所有用例照绿）。三态在这里就塌成两种：知道在跑什么 → 带命令名；
        不知道 → unknown 文案。

        「命令为空按忙处理」是刻意的：tmux 极少数情况下会报空
        （进程刚被 reap、负载极重），把它当空闲的代价是**静默强杀**，
        当忙的代价只是多问一次。**这条不是为「刚建好的 pane」准备的**——
        2026-10-31 实测 4/4：``new-window`` 返回时 shell 已经 spawn 完，
        同一个 job 里的 ``list-panes`` 直接报 ``zsh``，第一个
        ``_topology_apply`` 写进缓存的就是 ``zsh``，没有空窗口。

        读的都是每 tick 的 poll 缓存（``_commands`` / ``_alt_screens`` / ``_dead``），
        **不需要任何 tmux 调用**，UI 线程仍是 O(1)，且与「打开到当前 pane」的占用
        口径一致（``_pane_problem``）。区别只在文案：那里是「退出再试」，
        这里是「仍在运行，确定关闭吗」。
        """
        if self._dead.get(pane_id):
            return None  # 已退出：没有可丢的东西，一键关
        command = self._commands.get(pane_id)
        # 白名单只认 basename（tmux 偶尔给完整路径），展示也用 basename——
        # 确认框一行放不下 `/opt/homebrew/bin/…`，用户认的也是最后那一段。
        base = _os.path.basename(command) if command else ""
        if not base:
            return t("app.close_confirm_busy_unknown")
        if not self._alt_screens.get(pane_id) and (
            base.lstrip("-").lower() in self._shell_names
        ):
            return None  # 停在提示符（全屏程序即使命令名像 shell 也照样问）
        return t("app.close_confirm_busy", command=base)

    def action_close_pane(self) -> None:
        """Ctrl-a w：关闭当前 pane（kill-window）。

        最后一个 pane 关闭会让 kill-window 连带杀掉整个 session → 禁止，no-op
        （设计拍板；结束会话走 Ctrl-a k 的确认路径）。

        v0.5.4：pane 里有程序在前台时先确认——``kill-window`` 的信号打给整个
        前台进程组，vim 未保存的缓冲区 / 跑了一半的 build / 等输入的 ``ssh``
        会无声消失，而破坏面更大的 ``Ctrl-a k`` 反而一直有确认框。
        """
        if len(self._pane_ids) <= 1:
            return
        pid = self._pane_ids[self._active_index]
        window_id = self._window_ids.get(pid)
        if window_id is None:
            return
        message = self._close_busy_message(pid)
        if message is None:
            self._close_pane_now(window_id)
            return

        def confirmed(ok: bool | None) -> None:
            if not ok:
                return
            # 模态框期间 poll 仍在跑（确认框不是 MainScreen，拓扑照常更新）：
            # pane 可能已经消失、或已被 join 到别的 window。按**捕获的 pid** 重查，
            # 不读 _active_index——槽位在模态期间可能被 clamp 过。
            if pid not in self._pane_ids:
                return
            now = self._window_ids.get(pid)
            if now is None:
                return
            self._close_pane_now(now)

        self.push_screen(
            ConfirmDialog(
                message,
                t("app.close_confirm_yes"),
                t("app.close_confirm_no"),
            ),
            confirmed,
        )

    def _close_pane_now(self, window_id: str) -> None:
        """提交 kill-window + 拓扑同步：直关与确认后走**同一段代码**。

        拆出来是为了两条路径不会漂移（例如某天只在其中一条里补 ``_burst()``）。
        """

        def fetch():
            self.backend.kill_window(window_id)  # check=False：失败不抛（旧语义）
            try:
                return self._topology_fetch()
            except TmuxError as exc:
                log.warning("list-panes 失败: %s", exc)
                return None

        def apply(panes: list[PaneInfo] | None) -> None:
            if panes is not None:
                # 关闭后焦点留在同槽位（下一个 pane 上移补位），内部已 clamp
                self._topology_apply(panes)
            self._burst()

        self._submit_job(fetch, apply=apply, label=t("app.job.kill_window"))

    def action_switch_pane(self, index: int) -> None:
        """Ctrl-a 1..4：按槽位切焦点；槽位不存在则忽略。"""
        self._set_active(index)

    # ---------- respawn（§10.1 / §12 P2） ----------

    def _respawn_target(self) -> str | None:
        """``Ctrl-a r`` 的目标：焦点 pane 已死则优先它，否则最近死亡的 pane。

        「最近死亡」用前端自己观测的 alive→dead 转移顺序（``_dead_order``）
        ——tmux 不提供死亡时间戳，而前端每 tick 都在看 ``pane_dead``。
        无 dead pane → None（调用方静默 no-op，同 Ctrl-a 1..4 越界口径）。
        """
        active = self._active_pane
        if active is not None and self._dead.get(active):
            return active
        for pid in reversed(self._dead_order):
            if pid in self._pane_ids and self._dead.get(pid):
                return pid
        return None

    def action_respawn(self) -> None:
        """Ctrl-a r：respawn 最近死亡的 pane（§9 可 respawn 的 P2 键位）。"""
        pid = self._respawn_target()
        if pid is None:
            return  # 没有已退出的 pane：静默 no-op

        def apply(_result) -> None:
            # pane_id 不变（respawn 原地重开）→ per-pane 状态全部保留，
            # 只复位 dead 标记与翻页偏移（新 shell 应回实时尾部）
            self._dead[pid] = False
            if pid in self._dead_order:
                self._dead_order.remove(pid)
            self._offsets[pid] = 0
            self._refresh_status()
            self._burst()

        # B1 §3.2-7：tmux 动作进线程段，状态复位在循环段回投
        self._submit_job(
            lambda: self.backend.respawn_pane(pid),
            apply=apply,
            label=t("app.job.respawn"),
        )

    # ---------- 键盘（§10.1） ----------

    def _input_target(self) -> str | None:
        """可接收输入的焦点 pane；dead pane 返回 None（§9 / §12 P2）。

        dead pane 的 pty 里已无进程（tmux 对 dead pane 的 send-keys 静默
        丢弃，实测 exit=0）→ 拦在前端：用户对灰屏敲键不再毫无反馈。
        全局前缀键（Ctrl-a d/k/t/w/r/1..4）不经过这里，dead pane 焦点下照常可用。
        """
        pane = self._active_pane
        if pane is None or self._dead.get(pane):
            return None
        return pane

    def _forward_key(self, key: str, character: str | None = None) -> None:
        pane = self._input_target()
        if pane is None:
            return
        mapped = map_key(key, character)
        if mapped is None:
            return
        kind, value = mapped
        # B1 §3.2-3：提交即返（FIFO 保序——提交点在事件循环同步段，事件顺序
        # = 提交顺序 = 执行顺序，快速连打不乱序）；错误经 future errback 记录
        #（口径同旧 log.warning("send 失败")）
        if kind == "text":
            self._submit_job(
                lambda: self.backend.send_text(pane, value),
                label=t("app.job.send"),
            )
        else:
            self._submit_job(
                lambda: self.backend.send_keys(pane, value),
                label=t("app.job.send"),
            )
        # 上次输入任意键（除滚轮）→ 自动回实时尾部（§11）
        if self._offsets.get(pane, 0) != 0:
            self._offsets[pane] = 0
        self._burst()

    def _prefix_timeout(self) -> None:
        self._prefix_timer = None
        if self._prefix_armed:
            self._prefix_armed = False
            if self._sidebar_focused():
                return  # 侧栏聚焦 → Ctrl-a 本身也不发给 shell（D8）
            # 超时把 Ctrl-a 本身按 \x01 发给内层 shell（§10.1）
            pane = self._input_target()
            if pane is not None:
                self._submit_job(
                    lambda: self.backend.send_ctrl_a(pane),
                    label=t("app.job.send"),
                )  # B1 §3.2-4：同 _forward_key（提交即返，FIFO 保序）
                self._burst()

    def on_key(self, event: Key) -> None:
        if not isinstance(self.screen, MainScreen):
            return
        key = event.key
        # v0.35：高亮 / hold / 手势期间按键 → 先取消并解冻，然后正常处理。
        # 例外：Ctrl-a 前缀本身（等第二个键分流）与「前缀 + c」（action_copy
        # 会复制并立刻解冻）；手势中按任意键都直接收摊（画面不能与输入脱节）。
        if self._gesture is not None:
            self._cancel_gesture()
        elif self._frozen_pane is not None or self._hold_selection:
            if not (key == "ctrl+a" or (self._prefix_armed and key == "c")):
                self._cancel_gesture()
        if self._prefix_armed:
            self._prefix_armed = False
            if self._prefix_timer is not None:
                self._prefix_timer.stop()
                self._prefix_timer = None
            # v0.22：前缀键是一次显式导航/动作 → 收起悬停浮层，别挡住目标区域
            # v0.29：`?` 是开关，必须在「前缀动作先收起」之前分流——否则
            # 这里先隐藏、toggle 再看到隐藏状态又打开，按第二次关不掉。
            if key in ("question_mark", "?") or event.character == "?":
                self._toggle_shortcut_help()
                return
            self._hide_shortcut_help()
            if key == "d":
                self.action_detach()
            elif key == "k":
                self.action_kill()
            elif key == "t":
                self.action_new_pane()  # P1：新建 pane
            elif key == "w":
                self.action_close_pane()  # P1：关闭当前 pane（最后一个 no-op）
            elif key == "r":
                self.action_respawn()  # P2：respawn 最近死亡的 pane
            elif key == "b":
                self.action_toggle_sidebar()  # P3：折叠 / 展开侧栏
            elif key == "s":
                self.action_focus_sidebar()  # P3：焦点移入侧栏
            elif key == "g":
                self.action_focus_git_log()  # v0.22：聚焦 Git 历史
            elif key == "c":
                # v0.13：复制当前选区（**无选区则不复制**，只提示）
                # 放在「透传分支」之前 → 侧栏聚焦时同样可用
                self.action_copy()
            elif key == "v":
                # v0.14：粘贴（内部缓冲优先 → 系统剪贴板回落）
                self.action_paste()
            elif len(key) == 1 and key in "1234":
                self.action_switch_pane(int(key) - 1)  # P1：切焦点
            elif not self._sidebar_focused():
                # Ctrl-a [其他] → 透传给内层 shell（§10.1）
                # 侧栏聚焦时不透传（D8：焦点在侧栏 → 键归侧栏）
                self._forward_key(key, event.character)
            return
        if key == "ctrl+a":
            self._prefix_armed = True
            self._prefix_timer = self.set_timer(PREFIX_TIMEOUT, self._prefix_timeout)
            return
        if self._sidebar_focused():
            # 焦点在侧栏 → 键归侧栏，**不透传**给内层 shell（D8）。
            # 注意：焦点控件的 BINDINGS 不在焦点层消费——↑↓/enter/space 会先
            # 到这里（被透传），之后才由树消费 → 不拦截就会双重处理。
            if key == "escape":
                self._focus_active_pane()  # Esc 回终端
            return
        self._forward_key(key, event.character)

    def on_paste(self, event: Paste) -> None:
        if not event.text:
            return
        if not isinstance(self.screen, MainScreen):
            return
        # v0.35：粘贴前先收摊（高亮 / hold / 手势）——画面冻结与输入不能脱节
        if self._has_pending_selection():
            self._cancel_gesture()
        if self._sidebar_focused():
            return  # 焦点在侧栏 → 粘贴不进 shell（D8 决策 4）
        pane = self._input_target()
        if pane is None:
            return
        # B1 §3.2-8：paste（load-buffer + paste-buffer 两次子进程）提交即返，
        # 合并一次 poll；错误口径不变（记 warning）
        self._submit_job(
            lambda: self.backend.paste(pane, event.text),
            label=t("app.job.paste"),
        )  # load/paste-buffer 通道（B7）
        if self._offsets.get(pane, 0) != 0:
            self._offsets[pane] = 0
        self._burst()

    # ---------- 顶栏按钮 / tab ----------

    def on_top_bar_button_pressed(self, event: TopBarButton.Pressed) -> None:
        if event.button_id == "btn-new":
            self.action_new_pane()
        elif event.button_id == "btn-detach":
            self.action_detach()
        elif event.button_id == "btn-kill":
            self.action_kill()
        elif event.button_id == "btn-help":
            # 悬停是主入口；点击也立刻展开（触控/手快时不用等 0.15s）
            self._show_shortcut_help()
        if self._active_pane is not None:
            self.query_one(
                f"#pane-{min(self._active_index, len(self._pane_ids) - 1)}",
                PaneWidget,
            ).focus()

    def on_pane_tab_pressed(self, event: PaneTab.Pressed) -> None:
        """点击顶栏 tab = Ctrl-a N（§12：顶栏标签可点击）。"""
        self._set_active(event.index)

    # ---------- 快捷键帮助浮层（v0.22） ----------

    def _shortcut_help(self) -> ShortcutHelp | None:
        try:
            return self.query_one("#shortcut-help", ShortcutHelp)
        except NoMatches:
            return None

    def _cancel_shortcut_help_show(self) -> None:
        if self._help_show_timer is not None:
            self._help_show_timer.stop()
            self._help_show_timer = None

    def _cancel_shortcut_help_hide(self) -> None:
        if self._help_hide_timer is not None:
            self._help_hide_timer.stop()
            self._help_hide_timer = None

    def _cancel_shortcut_help_timers(self) -> None:
        self._cancel_shortcut_help_show()
        self._cancel_shortcut_help_hide()

    def _schedule_shortcut_help(self) -> None:
        """指针进入 `?`：延迟片刻再弹（扫过顶栏时不闪）。"""
        if self._help_pinned:
            return  # 已由键盘打开，指针经过不重置滚动位置
        self._cancel_shortcut_help_timers()
        self._help_show_timer = self.set_timer(
            HELP_SHOW_DELAY, self._show_shortcut_help
        )

    def _show_shortcut_help(self) -> None:
        self._help_show_timer = None
        panel = self._shortcut_help()
        if panel is None:
            return  # teardown 竞态
        width = min(help_width(), max(10, self.screen.size.width - 2))
        panel.styles.width = width
        try:
            btn = self.query_one("#btn-help", TopBarButton)
            x = btn.region.x
        except NoMatches:
            x = 0
        x = max(0, min(x, self.screen.size.width - width))
        panel.absolute_offset = Offset(x, 1)  # 紧贴顶栏下沿，指针可直接下移进来
        panel.scroll_home(animate=False)
        panel.display = True

    def _hide_shortcut_help(self) -> None:
        self._cancel_shortcut_help_timers()
        self._help_pinned = False
        panel = self._shortcut_help()
        if panel is not None:
            panel.display = False

    def _toggle_shortcut_help(self) -> None:
        """v0.29：``Ctrl-a ?`` 全键盘开关（tmux ``prefix ?`` 同语义）。

        键盘打开时置 ``_help_pinned``：鼠标随后扫过 ``?`` 再离开，宽限定时器
        不关掉用户正在读的浮层。任何显式收起入口（前缀动作 / 点浮层外 /
        resize）都经 ``_hide_shortcut_help`` 统一清 pin。
        """
        panel = self._shortcut_help()
        if panel is not None and panel.display:
            self._hide_shortcut_help()
            return
        self._cancel_shortcut_help_timers()
        self._show_shortcut_help()
        self._help_pinned = True

    def _schedule_hide_shortcut_help(self) -> None:
        """指针离开 `?`：给一个宽限，期间移进浮层则取消隐藏。"""
        if self._help_pinned:
            return  # 键盘打开的浮层归键盘管
        self._cancel_shortcut_help_show()
        self._cancel_shortcut_help_hide()
        panel = self._shortcut_help()
        if panel is None or not panel.display:
            return
        self._help_hide_timer = self.set_timer(
            HELP_HIDE_GRACE, self._hide_shortcut_help_if_unhovered
        )

    def _hide_shortcut_help_if_unhovered(self) -> None:
        self._help_hide_timer = None
        if self._help_pinned:
            return
        panel = self._shortcut_help()
        if panel is None:
            return
        pos = self.mouse_position
        if panel.region.contains(*pos):
            return  # 已移进浮层
        try:
            btn = self.query_one("#btn-help", TopBarButton)
        except NoMatches:
            btn = None
        if btn is not None and btn.region.contains(*pos):
            return
        panel.display = False

    def on_top_bar_button_hover_start(
        self, event: TopBarButton.HoverStart
    ) -> None:
        if event.button_id == "btn-help":
            self._schedule_shortcut_help()

    def on_top_bar_button_hover_end(self, event: TopBarButton.HoverEnd) -> None:
        if event.button_id == "btn-help":
            self._schedule_hide_shortcut_help()

    def on_resize(self, event) -> None:
        """窗口尺寸变化 → 收起浮层（绝对定位已过期，避免停在错误位置）。

        v0.35：拖拽中的几何已过期 → 取消手势 + 解冻。
        """
        if self._has_pending_selection():
            self._cancel_gesture()
        self._hide_shortcut_help()

    def on_app_blur(self, event) -> None:
        """外层终端失焦（v0.35）：收摊手势 / 高亮，避免回来后画面永久冻结。"""
        if self._has_pending_selection():
            self._cancel_gesture()

    # ---------- 选区复制 / 粘贴（v0.13 / v0.14） ----------

    def action_copy(self) -> None:
        """Ctrl-a c：复制当前选区；无选区只提示（v0.35 不再回落到 pane buffer）。

        v0.31 曾在本键无选区时读 tmux 最新 buffer（程序自己 OSC 52 复制的内容）
        兜底；v0.35 删除：拖拽松手已自动复制，读「全局最新 buffer」容易把很久
        以前的一次 yank 当成新内容，且 SSH 下并没有可用的出口。

        高亮 / hold 期间按 ``Ctrl-a c`` = 手动再复制一次，然后立即解冻。
        """
        screen = self.screen
        text = screen.get_selected_text() if isinstance(screen, MainScreen) else None
        if text:
            self.copy_text(text)
            if self._hold_selection or self._frozen_pane is not None:
                self._hold_selection = False
                self._clear_screen_selection()
                self._thaw_pane()
            return
        self._notify(t("app.nothing_to_copy"))

    def copy_text(
        self,
        text: str,
        note: str | None = None,
    ) -> None:
        """公共复制入口：OSC 52 + 系统命令 + 内部缓冲。"""
        self.copy_to_clipboard(text)  # OSC 52
        self._copy_buffer = text  # 内部缓冲（系统通道失败也存）
        # B1 §3.2-10：pbcopy（~6–10ms 阻塞）走默认线程池（asyncio.to_thread，
        # 无顺序依赖——默认池任务一律不携带 apply 段回投 DOM，见 _copy_to_system）；
        # 提示在回投时出（提示语义不变，只是从同步改为完成时）
        self._copy_to_system(text, note)

    @work(exclusive=True, group="clipcopy", exit_on_error=False)
    async def _copy_to_system(
        self,
        text: str,
        note: str | None = None,
    ) -> None:
        """写系统剪贴板（阻塞写放线程，R9 同款形态）→ 回投提示。

        ``note`` 供右键菜单等调用方覆盖提示前缀（默认仍是「已复制
        N 字符」）。系统通道失败时统一追加「仅 OSC 52」保留余地。

        v0.35：**SSH 会话**不调用系统命令（``pbcopy`` 写的是远端机器的剪贴板，
        对本地终端用户无意义，还可能拖慢提示），只发 OSC 52 并立即提示。
        """
        if clipboard.is_remote_session():
            self._notify(note or t("app.copied_osc52", count=len(text)))
            return
        ok = await asyncio.to_thread(clipboard.copy_to_system_clipboard, text)
        prefix = note or t("app.copied", count=len(text))
        message = prefix if ok else t("app.copied_osc52_only", prefix=prefix)
        self._notify(message)

    def action_paste(self) -> None:
        """Ctrl-a v：把文本粘贴进当前活动 pane。

        - **SSH 会话**（v0.35）：远端剪贴板对本地无意义 → 只提示用终端自己的
          粘贴快捷键（`SSH_PASTE_HINT`：macOS `Cmd+V` / Windows Terminal
          `Ctrl+V` / Tabby `Ctrl+Shift+V`），不读 ``_copy_buffer`` / 系统剪贴板；
          终端原生粘贴（``on_paste``）不受影响；
        - **本地**：系统剪贴板**优先**（``read_system_clipboard``），读不到
          （无命令 / 失败）才回落到内部缓冲 ``_copy_buffer``。

        前缀键**全局有效**——侧栏聚焦时同样粘贴到当前活动 pane（与
        ``Ctrl-a c`` 对称）。这与终端原生粘贴（``on_paste``，侧栏聚焦时
        **不**贴）口径不同：原生粘贴是「终端替我发的一坨文本」，而
        ``Ctrl-a v`` 是用户显式按下的动作。
        """
        if clipboard.is_remote_session():
            self._notify(ssh_paste_hint())
            return
        self._paste_from_system(fallback=self._copy_buffer)

    @work(exclusive=True, group="clipread", exit_on_error=False)
    async def _paste_from_system(self, fallback: str = "") -> None:
        """读系统剪贴板 → 粘贴（**阻塞读放线程**，R9 同款形态）。

        读剪贴板可能阻塞（``xclip -o`` 遇到挂死的剪贴板属主会一直等）→
        不能堵 UI 线程，故用 async worker + ``asyncio.to_thread``
        （``@work(thread=True)`` 配 async 方法是错的组合：线程里拿到的只是
        未 await 的 coroutine，取数**静默不执行**，见 R9）。

        v0.35：``fallback`` 是内部缓冲。``read_system_clipboard()`` 返回
        ``None``（无命令 / 失败）才回落；返回 ``""`` 表示「读到了但剪贴板是空
        的」→ 按「无可粘贴内容」处理，不再拿旧内部缓冲顶替。
        """
        text = await asyncio.to_thread(clipboard.read_system_clipboard)
        if text is None:
            text = fallback
        if not text:
            self._notify(t("app.nothing_to_paste"))
            return
        self._paste_text(text)

    def _paste_text(self, text: str) -> None:
        """把 text 送进焦点 pane（load-buffer + paste-buffer -p 通道，B7）。

        与终端原生粘贴（``on_paste``）同一条 tmux 通道 → bracket 口径一致
        （程序请求了 2004 才包 bracket）。
        """
        pane = self._input_target()
        if pane is None:
            return  # dead pane / 无 pane → 静默（与键盘透传同口径，§9 / P2）
        # B1 §3.2-8：提交即返 + 合并一次 poll；_notify 时机改为提交后（提示语义不变）
        self._submit_job(
            lambda: self.backend.paste(pane, text),
            label=t("app.job.paste"),
        )
        if self._offsets.get(pane, 0) != 0:
            self._offsets[pane] = 0
        self._burst()
        self._notify(t("app.pasted", count=len(text)))

    def action_detach(self) -> None:
        """Ctrl-a d：前端退出，tmux 会话与全部 shell 继续（§3.1）。"""
        self.exit()

    def action_kill(self) -> None:
        def callback(kill: bool | None) -> None:
            if kill:
                # B1 §3.2-9：kill-server 进执行器；**完成后再 exit**——保证
                # 「会话已死」先于 app 退出 / 锁释放（比 §3.2-9 的 atexit join
                # 兜底更严，且无阻塞等待点：用户已按确认键，等一个回调即可）。
                # 提交被拒（执行器已关）→ 直接退出（会话由 atexit join 兜底杀）
                submitted = self._submit_job(
                    self.backend.kill_server,
                    label=t("app.job.kill_server"),
                    apply=lambda _: self.exit(),
                )
                if submitted is None:
                    self.exit()

        self.push_screen(
            KillConfirm(t("app.kill_confirm")), callback
        )
