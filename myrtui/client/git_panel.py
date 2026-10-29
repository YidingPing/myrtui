"""Git 提交历史面板（侧栏 v2 下半区，P3 方案 §4.4）。

行格式按**面板内容宽**自适应（日期列逐步加长，标题始终优先保住可读宽度）：

- ``< 24`` cell：``MM-DD  标题``（例 ``10-23  feat: …``）；
- ``24–31`` cell：``YYYY-MM-DD  标题``；
- ``>= 32`` cell：``YYYY-MM-DD HH:MM  标题``。

字段间 2 空格；截断一律按 **cell 宽度**（``cell_len``，N9 口径——CJK 宽字符
占 2 cell，用 ``len()`` 会截出超宽行）。

悬停：行底色立即加深；**同一行停留满 ``HOVER_CARD_DELAY`` 秒**后，在侧栏
右侧弹出 ``CommitCard``（完整标题 + 正文 + 短 sha + 日期时间 + 作者）。卡片只在
内存里取数，不额外跑 git；正文过长时限高并可滚轮查看，卡片内的文字可用鼠标
拖动选中（``Ctrl-a c`` 复制）。鼠标从列表移进卡片有 ``HOVER_CARD_GRACE`` 宽限；
滚轮 / 列表刷新时立即隐藏。
面板只读：点击仅高亮，不做详情。
"""
from __future__ import annotations

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.geometry import Offset
from textual.widgets import OptionList, Static

from myrtui.client.cells import truncate_cells
from myrtui.gitlog import Commit

# 面板未布局时（宽度未知）的兜底宽度：与侧栏 min-width 20 − padding 2 对齐
FALLBACK_WIDTH = 18

_SEP = "  "  # 字段分隔（2 空格）

# 日期列三档阈值（面板内容宽，cell）
FULL_DATE_MIN_WIDTH = 24
FULL_TIME_MIN_WIDTH = 32

# 悬停多久才弹卡片：太短会在扫视列表时频繁闪烁
HOVER_CARD_DELAY = 1.0
# 鼠标离开列表后留给「移进卡片」的宽限：卡片在侧栏右侧，中间隔 1 格
HOVER_CARD_GRACE = 0.3
CARD_MAX_WIDTH = 46
CARD_MIN_WIDTH = 16


class CommitCard(VerticalScroll, can_focus=False):
    """悬停提交详情卡片（屏幕级浮层，由 GitLogPanel 负责定位/显隐）。

    正文可能很长 → 卡片限高 80% 屏高、可滚轮查看；内容放在内部 Static 里
    以便 Textual 的文本选区直接取到纯文本（拖动选中后可 ``Ctrl-a c`` 复制）。
    ``can_focus=False``：点卡片选字不得抢走 pane 的键盘焦点。
    """

    def compose(self):
        yield Static("", id="commit-card-body")

    def set_content(self, text: Text) -> None:
        self.query_one("#commit-card-body", Static).update(text)
        self.scroll_home(animate=False)

    def _on_enter(self, event: events.Enter) -> None:
        super()._on_enter(event)
        panel = self._panel()
        if panel is not None:
            panel._card_entered()

    def _on_leave(self, event: events.Leave) -> None:
        super()._on_leave(event)
        # 在卡片内部移动（border ↔ 正文 Static）也会收到冒泡的 Leave；
        # 用指针坐标判断是否真的离开了卡片。
        if self.region.contains(*self.app.mouse_position):
            return
        panel = self._panel()
        if panel is not None:
            panel._card_left()

    def _panel(self) -> "GitLogPanel | None":
        try:
            return self.screen.query_one(GitLogPanel)
        except NoMatches:
            return None


class GitLogPanel(OptionList):
    """Git 提交历史列表。每行：日期（自适应长度）+ 标题（cell 截断）。"""

    def __init__(self, **kwargs) -> None:
        # compact=True：去掉 OptionList 默认的边框与内边距（框架自带的
        # -textual-compact 样式），侧栏里只留内容区
        kwargs.setdefault("compact", True)
        super().__init__(**kwargs)
        self._commits: list[Commit] = []
        self._render_width: int | None = None
        self._hover_index: int | None = None
        self._hover_y = 0
        self._hover_timer = None
        self._hide_timer = None

    def show_commits(self, commits: list[Commit], width: int | None = None) -> None:
        """整表替换；逐行渲染 Text，标题按宽度截断加 ``…``（N9 口径）。

        ``width`` 省略时取控件自身的可用宽度（面板知道自己的宽度，不必由外部
        传入；参数仅作测试/覆盖用）。

        3s 定时刷新会反复调用本方法：内容与宽度都没变时**直接返回**，否则
        ``set_options`` 会重置高亮/滚动位置，也会让悬停卡片闪一下。
        """
        if width is None:
            width = self.scrollable_content_region.width
        if width <= 0:
            width = FALLBACK_WIDTH
        if commits == self._commits and width == self._render_width:
            return
        self._cancel_hover()
        self._commits = list(commits)
        self._render_width = width
        self.set_options([self._format_commit(commit, width) for commit in commits])

    def show_message(self, text: str) -> None:
        """非仓库 / git 不可用 / 该文件暂无提交 等占位文案。"""
        self._cancel_hover()
        self._commits = []
        self._render_width = None
        self.set_options([Text(text)])

    def on_resize(self, event) -> None:
        """宽度变化 → 按新宽度重算日期档位与标题预算。"""
        if self._commits:
            self.show_commits(self._commits)

    # ---------- 行渲染 ----------

    def _format_commit(self, commit: Commit, width: int) -> Text:
        """一行 = 日期（按宽度取档）+ 标题；总宽不超过 ``width``。"""
        label = _date_label(commit.date, width)
        row = Text()
        row.append(label, style="dim")
        used = cell_len(label)
        budget = width - used - len(_SEP)
        if budget > 0:
            row.append(_SEP)
            row.append(truncate_cells(commit.subject, budget))
        return row

    # ---------- 悬停卡片 ----------

    def _on_mouse_move(self, event: events.MouseMove) -> None:
        """跟踪指针所在行：换行才重置 1s 计时，同一条目内移动不重置。"""
        super()._on_mouse_move(event)
        self._hover_row(event.style.meta.get("option"), event.screen_y)

    def _hover_row(self, index: int | None, y: int) -> None:
        """悬停意图入口（与鼠标事件解耦，便于单测计时语义）。"""
        if index is None or not (0 <= index < len(self._commits)):
            self._cancel_hover()
            return
        self._hover_y = y
        if index == self._hover_index:
            # 同一行内移动：计时不重来；卡片已显示就跟着指针纵向走
            if not self._position_card():
                self._hide_card()
            return
        self._hover_index = index
        self._cancel_hover_timer()
        self._hide_card()
        self._hover_timer = self.set_timer(HOVER_CARD_DELAY, self._show_hover_card)

    def _on_leave(self, event: events.Leave) -> None:
        super()._on_leave(event)
        card = self._card()
        if card is not None and card.display:
            # 卡片已显示：不要立刻隐藏——指针可能正要移进右侧卡片（中间
            # 隔 1 格）。给一个短宽限，期间进入卡片/开始选字都会取消隐藏。
            self._cancel_hover_timer()
            self._schedule_hide_card()
        else:
            self._cancel_hover()

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        super()._on_mouse_scroll_down(event)
        self._cancel_hover()

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        super()._on_mouse_scroll_up(event)
        self._cancel_hover()

    def on_hide(self) -> None:
        self._cancel_hover()

    def on_unmount(self) -> None:
        self._cancel_hover()

    def _show_hover_card(self) -> None:
        """1s 计时到点：确认仍悬停在同一行后再显示。"""
        self._hover_timer = None
        index = self._hover_index
        if index is None or not (0 <= index < len(self._commits)):
            return
        card = self._card()
        if card is None:
            return
        card.set_content(commit_card_text(self._commits[index]))
        if not self._position_card(card):
            return
        card.display = True

    def _position_card(self, card: CommitCard | None = None) -> bool:
        """把卡片放到侧栏右侧、与悬停行对齐；放不下则返回 False。"""
        if card is None:
            card = self._card()
        if card is None:
            return False
        x = self.region.right + 1
        available = self.screen.size.width - x - 1
        if available < CARD_MIN_WIDTH:
            return False
        card.styles.max_width = min(CARD_MAX_WIDTH, available)
        card.absolute_offset = Offset(x, self._hover_y)
        return True

    def _hide_card(self) -> None:
        self._cancel_hide_timer()
        card = self._card()
        if card is not None:
            # 卡片的正文 Static 是复用的：不清理旧选区的话，下一条提交的
            # 文字会继承上一轮的高亮（Screen.selections 仍指向该 Static）。
            self._clear_card_selection(card)
            card.display = False

    def _clear_card_selection(self, card: CommitCard) -> None:
        """只清掉落在卡片上的选区，不动 Screen 当前的拖拽状态。

        ``Screen.clear_selection()`` 会连 ``_select_state`` 一起清空——点卡片
        外要开始一段新选区时会把刚建立的拖拽起点也抹掉；这里只删旧高亮。
        """
        selections = getattr(self.screen, "selections", {})
        stale = [
            widget
            for widget in selections
            if card in widget.ancestors_with_self
        ]
        if not stale:
            return
        self.screen.selections = {
            widget: selection
            for widget, selection in selections.items()
            if widget not in stale
        }

    def _schedule_hide_card(self) -> None:
        """离开列表/卡片后延迟隐藏：给指针跨过 1 格空隙的时间。"""
        self._cancel_hide_timer()
        self._hide_timer = self.set_timer(
            HOVER_CARD_GRACE, self._hide_card_if_unhovered
        )

    def _hide_card_if_unhovered(self) -> None:
        self._hide_timer = None
        card = self._card()
        if card is None:
            return
        if card.display and (
            self._pointer_in_card(card) or self._card_has_selection(card)
        ):
            return  # 指针在卡片上 / 正在选字：保持显示
        self._hide_card()

    def _card_entered(self) -> None:
        """指针进入卡片 → 取消离开列表时安排的那次隐藏。"""
        self._cancel_hide_timer()

    def _card_left(self) -> None:
        """指针离开卡片：没有选区就隐藏；有选区保留，等点卡片外再关。"""
        card = self._card()
        if card is None:
            return
        if self._card_has_selection(card):
            return
        self._hide_card()

    def _pointer_in_card(self, card: CommitCard) -> bool:
        x, y = self.app.mouse_position
        return card.region.contains(x, y)

    def _card_has_selection(self, card: CommitCard) -> bool:
        """选区是否落在卡片（或卡片内部的正文 Static）上。"""
        selections = getattr(self.screen, "selections", {})
        return any(
            card in widget.ancestors_with_self for widget in selections
        )

    def _card(self) -> CommitCard | None:
        """取屏幕级卡片；未挂载 / teardown 竞态时返回 None。"""
        if not self.is_mounted:
            return None
        try:
            return self.screen.query_one(CommitCard)
        except NoMatches:
            return None

    def _cancel_hover(self) -> None:
        self._hover_index = None
        self._cancel_hover_timer()
        self._hide_card()

    def _cancel_hover_timer(self) -> None:
        if self._hover_timer is not None:
            self._hover_timer.stop()
            self._hover_timer = None

    def _cancel_hide_timer(self) -> None:
        if self._hide_timer is not None:
            self._hide_timer.stop()
            self._hide_timer = None


def _date_label(value: str, width: int) -> str:
    """按面板内容宽选日期档位。

    ``value`` 是 git 的 ``--date=format:%Y-%m-%d %H:%M`` 输出；测试里也可能
    只给 ``YYYY-MM-DD``（没有时间），此时宽档退化为日期本身。
    """
    if len(value) < 10:
        return value
    if width >= FULL_TIME_MIN_WIDTH and len(value) >= 16 and value[10] == " ":
        return value
    if width >= FULL_DATE_MIN_WIDTH:
        return value[:10]
    return value[5:10]


def commit_card_text(commit: Commit) -> Text:
    """悬停卡片内容：完整标题 + 正文 + 短 sha / 完整日期时间 / 作者。"""
    card = Text(commit.subject, style="bold")
    body = commit.body.strip("\n")
    if body.strip():
        card.append("\n\n")
        card.append(body)
    card.append("\n\n")
    card.append(commit.sha, style="bold")
    card.append(f" · {commit.date} · {commit.author}", style="dim")
    return card
