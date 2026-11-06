"""窄屏底栏：按上下文切换的 3 行高快捷按钮（0.11.0）。

为什么做这条底栏：手机软键盘上按 ``Esc`` / ``Enter`` / ``Shift-Tab`` 要先呼出
键盘，慢且经常来不及；各手机终端自带的 extra keys 行又「少这少那」。底栏只做
**跨终端一致、按当前页面取键** 的一层：

- 终端 pane（AI coding）：``ESC`` / ``ENT`` / ``⇧ ⇥`` / ``/`` / ``@`` /
  ``↑`` / ``↓`` / ``C-J`` / ``DEL``；
- 文件树整页：``ESC`` / ``↑`` / ``↓`` / ``ENT`` / ``≡``（Space，打开菜单）；
- Git 历史整页：``ESC`` / ``↑`` / ``↓`` / ``ENT``（显示 / 收起提交详情）；
- 上下文菜单：``ESC`` / ``↑`` / ``↓`` / ``ENT``（菜单是模态屏，主屏那条
  被盖住，必须自己有一条才点得到）。

每个键固定 3 cell 宽 × 3 行高，标签最多 3 个字符；键之间留 1 cell 空隙，
避免 ``ESCENT`` 粘在一起。单字符标签在 3×3 里水平 / 垂直居中，3 字符标签
正好填满宽度。真机反馈 3 cell 宽的单行键仍然太小（49 列屏上约 24pt），
所以高度加到 3 行，触控目标变成 3×3；整条靠左排列，右侧留白给后续加键。
应用动作（文件树、复制、respawn 等）不塞进 pane 行：顶栏已有
``+ X D K ?``，其余走 ``?`` 浮层，避免和外部键行、顶栏三处重复。

键语义不另写一套——普通键交给 ``TerminalApp._dispatch_key``，页面动作交给
``TerminalApp._dispatch_page_action``，与真实键盘/文件树行为一致。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Static

from myrtui.client.flash import flash_press
from myrtui.i18n import t

log = logging.getLogger("myrtui.keybar")

#: 每个键的内容宽（cell）；``KeyChip`` 的 CSS 宽度同此值。
CHIP_WIDTH = 3
#: 每个键的高度（行）；3 行才够手指点，CSS 高度同此值。
CHIP_HEIGHT = 3
#: 键与键之间的空隙（cell）；在 CSS 里用 ``margin-right`` 实现。
CHIP_GAP = 1

#: 环境变量：``auto``（默认，紧凑档显示）/ ``off``（永久关闭，连顶栏开关一起隐藏）。
#: 不做 ``TERM_PROGRAM`` 探测——只能猜「是什么 app」，猜不到它自带的 extra keys
#: 行是否开启、配了哪些键；显式开关更可靠。
KEYBAR_ENV = "MYRTUI_KEYBAR"


def resolve_keybar_mode(raw: str | None) -> str:
    """解析 ``MYRTUI_KEYBAR`` → ``"auto"`` / ``"off"``。

    未设 / 空串 / 非法值一律按 ``auto``（与 ``MYRTUI_COMPACT`` 同一口径：
    这是视口自适应，不是需要校验的配置点）。
    """
    if raw is None:
        return "auto"
    value = raw.strip().lower()
    return value if value in ("auto", "off") else "auto"


@dataclass(frozen=True)
class KeyChipSpec:
    """一个底栏按钮：显示词表 key + 要执行的键名或页面动作。"""

    id: str
    label_key: str
    priority: int
    key: str | None = None
    """交给 ``_dispatch_key`` 的 Textual 键名；页面动作按钮为 None。"""

    character: str | None = None
    """可打印键（``/`` / ``@``）的字符；不传则按普通键名解析。"""

    page_action: str | None = None
    """不是终端键、而是当前页面动作时的动作名（见 ``_dispatch_page_action``）。"""


# 同一份 spec 会在多个上下文复用（Esc 等）；页面方向键用独立 spec。
ESC = KeyChipSpec("esc", "keybar.esc", 100, key="escape")
ENTER = KeyChipSpec("enter", "keybar.enter", 98, key="enter")
SHIFT_TAB = KeyChipSpec("shift_tab", "keybar.shift_tab", 95, key="shift+tab")
SLASH = KeyChipSpec("slash", "keybar.slash", 90, key="slash", character="/")
AT = KeyChipSpec("at", "keybar.at", 88, key="at", character="@")
UP = KeyChipSpec("up", "keybar.up", 80, key="up")
DOWN = KeyChipSpec("down", "keybar.down", 80, key="down")
NEWLINE = KeyChipSpec("newline", "keybar.newline", 92, key="ctrl+j")
BACKSPACE = KeyChipSpec("backspace", "keybar.backspace", 50, key="backspace")
MENU = KeyChipSpec("menu", "keybar.menu", 75, page_action="menu")
# 整页里的方向键 / Enter 不能走 _dispatch_key（侧栏聚焦时它只拦不透传），
# 所以用独立 id 的页面动作 chip；pane 上仍用普通键。
PAGE_UP = KeyChipSpec("page_up", "keybar.up", 80, page_action="cursor_up")
PAGE_DOWN = KeyChipSpec("page_down", "keybar.down", 80, page_action="cursor_down")
PAGE_ENTER = KeyChipSpec("page_enter", "keybar.enter", 98, page_action="activate")
PAGE_ESC = KeyChipSpec("page_esc", "keybar.esc", 100, page_action="back")

#: 四种页面上下文的按钮顺序（也是显示顺序）。
CONTEXTS: dict[str, tuple[KeyChipSpec, ...]] = {
    "pane": (ESC, ENTER, SHIFT_TAB, SLASH, AT, UP, DOWN, NEWLINE, BACKSPACE),
    "tree": (PAGE_ESC, PAGE_UP, PAGE_DOWN, PAGE_ENTER, MENU),
    "git": (PAGE_ESC, PAGE_UP, PAGE_DOWN, PAGE_ENTER),
    # 上下文菜单自己的一条（0.11.2）：菜单是 ModalScreen，主屏底栏被盖住，
    # 点原来的位置会命中「点菜单外 → 关闭」。这条底栏由菜单屏自己处理。
    "menu": (PAGE_ESC, PAGE_UP, PAGE_DOWN, PAGE_ENTER),
}

#: 去重后的全部 chip（KeyBar 一次挂载，按上下文显隐）。
ALL_CHIPS: tuple[KeyChipSpec, ...] = tuple(
    {spec.id: spec for group in CONTEXTS.values() for spec in group}.values()
)

#: 必须成对保留的键：只放得下一个方向键时两个都丢，避免出现「只有 ↑ 没有 ↓」。
FIT_GROUPS: dict[str, tuple[str, ...]] = {
    "up": ("up", "down"),
    "down": ("up", "down"),
    "page_up": ("page_up", "page_down"),
    "page_down": ("page_up", "page_down"),
}


def _add_cost(used: int, count: int = 1) -> int:
    """新增 count 个键要占的总宽（含键间空隙，不含行尾空隙）。"""
    return count * CHIP_WIDTH + (count - 1) * CHIP_GAP + (
        CHIP_GAP if used else 0
    )


def fit_chip_ids(available: int, context: str = "pane") -> tuple[str, ...]:
    """按可用宽度挑出当前上下文要显示的 chip id（纯函数，便于脱 UI 单测）。

    每个键 3 cell、键间 1 cell 空隙（行尾不留空隙）；按 priority 从高到低贪心装入，
    最后**按显示顺序**返回。方向键作为一组一起进出；``ESC`` 优先级最高。
    """
    if available <= 0:
        return ()
    specs = CONTEXTS.get(context, CONTEXTS["pane"])
    kept: set[str] = set()
    used = 0
    handled_groups: set[tuple[str, ...]] = set()
    for spec in sorted(specs, key=lambda item: item.priority, reverse=True):
        group = FIT_GROUPS.get(spec.id)
        if group is not None:
            if group in handled_groups:
                continue
            handled_groups.add(group)
            width = _add_cost(used, len(group))
            if used + width <= available:
                kept.update(group)
                used += width
            continue
        width = _add_cost(used)
        if used + width <= available:
            kept.add(spec.id)
            used += width
    return tuple(spec.id for spec in specs if spec.id in kept)


class KeyChip(Static):
    """3 行高、按下即发的底栏按钮（整块 3×3 都是热区）。

    与 ``TopBarButton`` 同样的触屏口径：``ALLOW_SELECT = False``（否则
    Screen 会先建选区，后续 MouseUp 又可能落到别的屏）＋ ``MouseDown``
    直接 stop + 发消息。不启用焦点（``CAN_FOCUS = False``）：它只是触摸快捷
    入口，键盘用户直接按真实键更快，也不该让点击把焦点从 pane 抢走。
    """

    CAN_FOCUS = False
    ALLOW_SELECT = False

    class Pressed(Message):
        """按钮按下（冒泡到 App；普通键带 key/character，页面键带 page_action）。"""

        def __init__(
            self,
            chip_id: str,
            key: str | None,
            character: str | None,
            page_action: str | None,
        ):
            super().__init__()
            self.chip_id = chip_id
            self.key = key
            self.character = character
            self.page_action = page_action

    def __init__(self, spec: KeyChipSpec, label: str, prefix: str = "key-chip"):
        super().__init__(label, id=f"{prefix}-{spec.id}", classes="key-chip")
        self.spec = spec

    def on_mouse_down(self, event: events.MouseDown) -> None:
        event.stop()
        flash_press(self)
        self.post_message(
            self.Pressed(
                self.spec.id,
                self.spec.key,
                self.spec.character,
                self.spec.page_action,
            )
        )


class KeyBar(Horizontal):
    """窄屏底栏容器：按上下文与宽度拟合按钮，隐藏时整体不占行。

    ``prefix`` 用来区分同屏存在的多条底栏（0.11.2：上下文菜单自己一条，
    主屏一条）：chip 的 id 变成 ``<prefix>-<spec.id>``。菜单那条用
    ``menu-chip-*``，App 级 ``query(KeyChip)`` 数主屏键时不会被它干扰。
    """

    def __init__(
        self,
        *,
        prefix: str = "key-chip",
        context: str = "pane",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._chip_prefix = prefix
        self._key_context = context if context in CONTEXTS else "pane"
        self._visible: tuple[str, ...] = ()
        # 初始隐藏：App.on_mount 的 _apply_compact 会按尺寸/模式决定是否显示；
        # 菜单底栏在 on_mount 里自己 set_visible(True)。
        self.display = False

    def _chip_id(self, spec: KeyChipSpec) -> str:
        return f"#{self._chip_prefix}-{spec.id}"

    def compose(self) -> ComposeResult:
        for spec in ALL_CHIPS:
            yield KeyChip(spec, t(spec.label_key), self._chip_prefix)

    def on_mount(self) -> None:
        """按初始上下文排一次 DOM（``ALL_CHIPS`` 的顺序只对常用上下文成立）。"""
        try:
            self._reorder(self._key_context)
        except Exception:  # noqa: BLE001 —— teardown 竞态
            log.debug("底栏按键排序失败", exc_info=True)

    def set_context(self, context: str) -> None:
        """切换 pane / tree / git / menu 的按钮集合（幂等）。"""
        if context not in CONTEXTS:
            context = "pane"
        if context == self._key_context:
            return
        self._key_context = context
        self._reorder(context)
        self._visible = ()  # 强制重算（原上下文可见的键可能已不在集合里）
        if self.display:
            self._fit(self.size.width or self.screen.size.width)

    def _reorder(self, context: str) -> None:
        """按上下文顺序重排 DOM；否则文件树页会显示成 ESC / ENT / ↑ / ↓。"""
        for index, spec in enumerate(CONTEXTS[context]):
            chip = self.query_one(self._chip_id(spec), KeyChip)
            self.move_child(chip, before=index)

    def set_visible(self, visible: bool) -> None:
        """显示/隐藏整行（隐藏时不占 pane 高度）。"""
        if self.display != visible:
            self.display = visible
        if visible:
            # 首次显示时自身尺寸可能还是 0（尚未布局）→ 用屏宽先拟合一次，
            # on_resize 随后会按真实宽度再校准。
            self._fit(self.size.width or self.screen.size.width)

    def on_resize(self, event: events.Resize) -> None:
        if self.display:
            self._fit(event.size.width)

    def _fit(self, width: int) -> None:
        visible = fit_chip_ids(width, self._key_context)
        if visible == self._visible:
            return
        self._visible = visible
        last = visible[-1] if visible else None
        for spec in ALL_CHIPS:
            chip = self.query_one(self._chip_id(spec), KeyChip)
            chip.display = spec.id in visible
            # 最后一个可见键不留右侧空隙：左对齐时它只在精确宽度下才有意义，
            # 留着会让 35 列的 9 键行在 35 列屏上多占 1 格而被裁掉。
            chip.styles.margin = (0, 0 if spec.id == last else CHIP_GAP, 0, 0)
