"""0.10.0 窄屏紧凑档（compact）验收测试。

三层：

1. **纯函数**（无 Textual）：``resolve_compact_mode`` / ``compact_for``——
   阈值与滞回是这一版唯一的策略，必须能脱离 App 单测；
2. **App**（FakeBackend，无 tmux / 子进程）：46×22 进紧凑、100×30 保持桌面、
   进出不改 ``layout.json``、单 pane 跟随 active tab、侧栏整页、去边框；
3. **回归确认**：0.9.x 的关键断言在非紧凑尺寸下不变（槽位、`1:dead` 文案、
   品牌、分隔线显隐），以及 40×12 的上下文菜单在紧凑档下仍被钳在屏内。

阈值来源：手机竖屏真机 40–50 列 × 20 行（内部 0.10.0 计划 §0/§1）。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest
from textual.color import Color
from textual.events import MouseDown, MouseMove, MouseUp
from textual.geometry import Region
from textual.screen import Screen

from myrtui import layout_state
from myrtui.client import app as app_mod
from myrtui.client.app import (
    COMPACT_ENTER_HEIGHT,
    COMPACT_ENTER_WIDTH,
    COMPACT_EXIT_HEIGHT,
    COMPACT_EXIT_WIDTH,
    ConfirmDialog,
    MainScreen,
    TerminalApp,
    compact_for,
    resolve_compact_mode,
)
from myrtui.client.context_menu import ContextMenuScreen
from myrtui.client.git_panel import CommitCard, GitLogPanel
from myrtui.client.keybar import (
    CHIP_HEIGHT,
    CHIP_WIDTH,
    CONTEXTS,
    KeyBar,
    KeyChip,
    fit_chip_ids,
    resolve_keybar_mode,
)
from myrtui.client.pane_widget import PaneWidget
from myrtui.client.topbar import BRAND_TEXT, TopBarButton
from myrtui.gitlog import Commit
from myrtui.tmux_backend import CaptureResult, PaneInfo


# ---------- FakeBackend（纯内存，记录 resize-window） ----------

def make_pane(index: int) -> PaneInfo:
    return PaneInfo(
        pane_id=f"%{index}",
        dead=False,
        command="sh",
        alternate_on=False,
        cursor_x=0,
        cursor_y=0,
        cursor_flag=True,
        history_size=100,
        height=20,
        width=40,
        window_id=f"$w{index}",
        window_index=index,
        mouse_standard=False,
        mouse_button=False,
        mouse_any=False,
        mouse_sgr=True,
        mouse_all=False,
    )


class FakeBackend:
    """``list_panes`` / ``poll`` / ``resize_window`` 的内存实现。

    ``resize_calls`` 记 ``(window_id, cols, rows)``；window_id → 槽位用于把
    「新可见 pane 被 resize 一次」断言到具体 pane 上。
    """

    def __init__(self, count: int = 1):
        self.panes = [make_pane(i) for i in range(count)]
        self.resize_calls: list[tuple[str, int, int]] = []
        # 0.11.0 底栏断言：记录每一次 send-keys 的 (pane_id, keys...)
        self.sent_keys: list[tuple[str, tuple[str, ...]]] = []
        self.sent_text: list[tuple[str, str]] = []

    def list_panes(self) -> list[PaneInfo]:
        return list(self.panes)

    def poll(self, offsets):
        captures = {
            pid: CaptureResult(text=f"{pid}\n", offset=k)
            for pid, (k, _h) in offsets.items()
        }
        return list(self.panes), captures

    def poll_clipboard_buffers(self) -> None:
        return None

    def resize_window(self, window_id: str, cols: int, rows: int) -> None:
        self.resize_calls.append((window_id, cols, rows))

    # ---- 输入 / 生命周期（本套用例不关心注入内容，但底栏用例要看） ----
    def send_text(self, pane_id, text):
        self.sent_text.append((pane_id, text))

    def send_keys(self, pane_id, *keys):
        self.sent_keys.append((pane_id, tuple(keys)))

    def send_ctrl_a(self, pane_id):
        pass

    def send_mouse(self, pane_id, sequence):
        pass

    def paste(self, pane_id, text):
        pass

    def new_window(self, root):
        return "$new"

    def kill_window(self, window_id):
        # 真后端会关掉窗口；内存实现同样移除，_close_pane_now 的拓扑同步才有意义
        self.panes = [p for p in self.panes if p.window_id != window_id]

    def respawn_pane(self, pane_id):
        pass

    def kill_server(self):
        pass


async def _wait_until(pilot, cond, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """轮询等待条件成立（Textual 布局/resize 时机不确定，不写固定 sleep）。"""
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            return False
        await pilot.pause(interval)
    return True


def _app(backend, root, layout_path: str | None = None) -> TerminalApp:
    return TerminalApp(backend, root, layout_path=layout_path)


def _displayed_panes(app) -> list[str]:
    return [
        f"#pane-{slot}"
        for slot in range(app_mod.MAX_PANES)
        if app.query_one(f"#pane-{slot}", PaneWidget).display
    ]


def _tree_lines(tree) -> list[str]:
    return [str(line.path[-1].label.plain) for line in tree._tree_lines]


def _mouse(cls, x: int, y: int, button: int = 1):
    """屏幕坐标鼠标事件（Pilot.click 会把 MouseUp 固定回主屏，测不出真实时序）。"""
    return cls(
        widget=None, x=x, y=y, delta_x=0, delta_y=0, button=button,
        shift=False, meta=False, ctrl=False,
        screen_x=float(x), screen_y=float(y),
    )


# ---------- 1. 纯函数 ----------

def test_resolve_compact_mode_values():
    assert resolve_compact_mode("auto") == "auto"
    assert resolve_compact_mode("on") == "on"
    assert resolve_compact_mode("off") == "off"
    assert resolve_compact_mode("OFF") == "off"  # 大小写不敏感


@pytest.mark.parametrize("raw", [None, "", "  ", "yes", "1", "compact"])
def test_resolve_compact_mode_falls_back_to_auto(raw):
    """未设 / 空串 / 非法值 → auto（自适应，不是需要校验的配置点）。"""
    assert resolve_compact_mode(raw) == "auto"


def test_compact_for_phone_and_desktop():
    # 手机竖屏（真机 40–50 列）与桌面宽屏
    assert compact_for(46, 22, False, "auto") is True
    assert compact_for(100, 30, False, "auto") is False
    # 很矮但很宽 → 进；横屏手机 80×20 → 保持桌面档
    assert compact_for(80, 15, False, "auto") is True
    assert compact_for(80, 20, False, "auto") is False


def test_compact_for_hysteresis():
    """进入 < 60，退出 >= 64：60–63 停在原状态，不来回切。"""
    assert compact_for(COMPACT_ENTER_WIDTH - 1, 22, False, "auto") is True
    # 60 列不再「进入」，但已进入的也不退出（60 < 64）
    assert compact_for(COMPACT_ENTER_WIDTH, 22, False, "auto") is False
    assert compact_for(COMPACT_ENTER_WIDTH, 22, True, "auto") is True
    assert compact_for(63, 22, True, "auto") is True
    # 退出要宽高同时达标
    assert compact_for(COMPACT_EXIT_WIDTH, COMPACT_EXIT_HEIGHT - 1, True, "auto") is True
    assert compact_for(COMPACT_EXIT_WIDTH - 1, COMPACT_EXIT_HEIGHT, True, "auto") is True
    assert compact_for(COMPACT_EXIT_WIDTH, COMPACT_EXIT_HEIGHT, True, "auto") is False
    # 高度单独也能进（很矮但很宽）
    assert compact_for(100, COMPACT_ENTER_HEIGHT - 1, False, "auto") is True


def test_compact_for_explicit_modes():
    """``on`` / ``off`` 直接压过尺寸（off 是即刻回滚开关）。"""
    assert compact_for(46, 22, False, "on") is True
    assert compact_for(100, 30, True, "on") is True
    assert compact_for(46, 22, True, "off") is False
    assert compact_for(100, 30, False, "off") is False


def test_resolve_keybar_mode_values():
    """``MYRTUI_KEYBAR`` 只有 auto / off；非法值回落 auto。"""
    assert resolve_keybar_mode(None) == "auto"
    assert resolve_keybar_mode("auto") == "auto"
    assert resolve_keybar_mode("OFF") == "off"
    assert resolve_keybar_mode("  off  ") == "off"
    for raw in ("", "on", "yes", "full"):
        assert resolve_keybar_mode(raw) == "auto"


def test_keybar_fit_keeps_priority_and_display_order():
    """宽度够 → 全显示；每键 3+1 空隙，30 列丢方向键/DEL 的一部分。"""
    pane_all = tuple(spec.id for spec in CONTEXTS["pane"])
    assert fit_chip_ids(200) == pane_all
    assert fit_chip_ids(35) == pane_all
    assert fit_chip_ids(40) == pane_all

    mid = fit_chip_ids(34)
    assert mid == (
        "esc", "enter", "shift_tab", "slash", "at", "up", "down", "newline"
    )

    narrow = fit_chip_ids(30)
    assert "esc" in narrow
    assert "up" not in narrow and "down" not in narrow
    assert "newline" in narrow
    assert "backspace" in narrow
    # 仍按显示顺序（不是按 priority 排出来的顺序）
    assert narrow == (
        "esc", "enter", "shift_tab", "slash", "at", "newline", "backspace"
    )

    # 上下键成对：只放得下一只时两只都不出现
    no_pair = fit_chip_ids(27)
    assert "up" not in no_pair and "down" not in no_pair

    assert fit_chip_ids(5) == ("esc",)
    assert fit_chip_ids(0) == ()


def test_keybar_context_sets_fit_independently():
    """文件树 / Git 页只显示页面自己的键，不混入 AI coding 键。"""
    tree = CONTEXTS["tree"]
    assert fit_chip_ids(200, "tree") == tuple(spec.id for spec in tree)
    assert fit_chip_ids(20, "tree") == tuple(spec.id for spec in tree)
    tree_mid = fit_chip_ids(16, "tree")
    assert tree_mid == ("page_esc", "page_up", "page_down", "page_enter")
    tree_narrow = fit_chip_ids(12, "tree")
    assert tree_narrow == ("page_esc", "page_enter", "menu")
    assert "page_up" not in tree_narrow

    git = CONTEXTS["git"]
    assert fit_chip_ids(200, "git") == tuple(spec.id for spec in git)
    assert fit_chip_ids(16, "git") == tuple(spec.id for spec in git)
    assert fit_chip_ids(12, "git") == ("page_esc", "page_enter")


# ---------- 2. App ----------

def test_compact_layout_hides_sidebar_and_extra_panes(app_root):
    """46×22：侧栏 / 分隔线 / 品牌全让位，只留一个无边框 pane。"""
    async def scenario():
        app = _app(FakeBackend(4), app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)
            assert app._compact is True
            assert app.query_one("#sidebar").display is False
            assert _displayed_panes(app) == ["#pane-0"]
            for sep_id in ("#sep-main", "#sep-tree", "#sep-cols",
                           "#sep-rows-a", "#sep-rows-b"):
                assert app.query_one(sep_id).display is False, sep_id
            assert app.query_one("#topbar-brand").display is False
            # 压缩后的 `?` 仍在屏内（这是 0.9.x 的真机问题）；`×` 也在
            help_btn = app.query_one("#btn-help", TopBarButton)
            assert help_btn.region.right <= 46
            close_btn = app.query_one("#btn-close", TopBarButton)
            assert close_btn.is_disabled is False
            assert close_btn.region.right <= 46
            # tab 只留序号
            assert app.query_one("#tab-0")._body.strip() == "1"
    asyncio.run(scenario())


def test_desktop_layout_unchanged_at_100x30(app_root):
    """100×30：不紧凑，侧栏 + 2×2 与 0.9.x 完全一致。"""
    async def scenario():
        app = _app(FakeBackend(4), app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.4)
            assert app._compact is False
            assert app.query_one("#sidebar").display is True
            assert app.query_one("#col-a").display is True
            assert app.query_one("#col-b").display is True
            assert _displayed_panes(app) == [
                "#pane-0", "#pane-1", "#pane-2", "#pane-3"
            ]
            assert app.query_one("#tab-0")._body.strip() == "1:sh"
    asyncio.run(scenario())


def test_resize_out_of_compact_restores_layout_without_touching_state(app_root, tmp_path):
    """46 → 100：侧栏按 _sidebar_want 恢复、4 pane 全显示、layout.json 逐字节不变。"""
    layout_path = str(tmp_path / "layout.json")
    ratios = {"sidebar": 0.3, "tree": 0.6, "rows": 0.4, "cols": 0.55}
    layout_state.save_ratios(layout_path, ratios)
    before = open(layout_path, "rb").read()

    async def scenario():
        app = _app(FakeBackend(4), app_root, layout_path)
        assert app._ratios == ratios, "启动应恢复 layout.json 的比例"
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)
            assert app._compact is True
            await pilot.resize_terminal(100, 30)
            assert await _wait_until(pilot, lambda: app._compact is False), (
                "放大后应退出紧凑档"
            )
            assert await _wait_until(
                pilot, lambda: app.query_one("#sidebar").display is True
            )
            assert _displayed_panes(app) == [
                "#pane-0", "#pane-1", "#pane-2", "#pane-3"
            ]
            assert app.query_one("#col-b").display is True
            assert app.query_one("#tab-0")._body.strip() == "1:sh"
    asyncio.run(scenario())

    assert open(layout_path, "rb").read() == before, "紧凑档进出不得改写比例文件"


def test_compact_tab_switches_the_visible_pane(app_root):
    """4 pane + 紧凑：只有 active 可见；Ctrl-a 3 切到 pane-2 并 resize 一次。"""
    async def scenario():
        backend = FakeBackend(4)
        app = _app(backend, app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)
            assert _displayed_panes(app) == ["#pane-0"]

            await pilot.press("ctrl+a")
            await pilot.press("3")
            assert await _wait_until(
                pilot, lambda: _displayed_panes(app) == ["#pane-2"]
            ), f"切 tab 后只应显示 pane-2：{_displayed_panes(app)}"
            assert app.query_one("#pane-0", PaneWidget).display is False
            assert app._active_index == 2

            # 新可见的槽位收到一次内容区尺寸（紧凑档无边框 → 46×18：
            # 顶栏 1 行 + 防误触空行 1 行 + 底栏 3 行）
            assert await _wait_until(
                pilot, lambda: any(w == "$w2" for w, _c, _r in backend.resize_calls)
            ), backend.resize_calls
            sizes = [(c, r) for w, c, r in backend.resize_calls if w == "$w2"]
            assert sizes == [(46, 17)], sizes
    asyncio.run(scenario())


def test_topbar_close_button_closes_the_active_pane(app_root):
    """顶栏 `×` = Ctrl-a w：关掉当前 pane，按钮随拓扑保持可用（还有 2 个）。"""
    async def scenario():
        backend = FakeBackend(3)
        app = _app(backend, app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)
            assert app._active_index == 0
            await pilot.click("#btn-close")
            assert await _wait_until(
                pilot, lambda: len(app._pane_ids) == 2
            ), app._pane_ids
            assert app._active_index == 0
            assert _displayed_panes(app) == ["#pane-0"]
            assert app.query_one("#btn-close", TopBarButton).is_disabled is False
    asyncio.run(scenario())


def test_topbar_close_button_disabled_for_single_pane(app_root):
    """只剩一个 pane 时 `×` 置灰（与 Ctrl-a w 的 no-op 一致），点击不动。"""
    async def scenario():
        app = _app(FakeBackend(1), app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)
            btn = app.query_one("#btn-close", TopBarButton)
            assert btn.is_disabled is True
            await pilot.click("#btn-close")
            await pilot.pause(0.2)
            assert len(app._pane_ids) == 1
    asyncio.run(scenario())


def test_topbar_close_button_confirms_busy_pane(app_root):
    """`×` 复用 Ctrl-a w：前台有程序时先确认，Esc 取消不杀 pane。"""
    async def scenario():
        backend = FakeBackend(2)
        backend.panes[0] = replace(backend.panes[0], command="vim")
        app = _app(backend, app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)
            assert app._commands.get("%0") == "vim", app._commands
            await pilot.click("#btn-close")
            await pilot.pause(0.2)
            assert isinstance(app.screen, ConfirmDialog), app.screen
            assert len(app._pane_ids) == 2, "确认前不得关闭"
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert len(app._pane_ids) == 2
    asyncio.run(scenario())


def test_topbar_close_cancel_leaves_no_selection(app_root):
    """点 `×` 弹确认框 → 取消：主屏不得停在选字状态（真实 MouseUp 在模态屏）。"""
    async def scenario():
        backend = FakeBackend(2)
        backend.panes[0] = replace(backend.panes[0], command="vim")
        app = _app(backend, app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)
            main = app.screen
            btn = app.query_one("#btn-close", TopBarButton)
            x, y = btn.region.x + 1, btn.region.y

            main._forward_event(_mouse(MouseDown, x, y))
            assert await _wait_until(
                pilot, lambda: isinstance(app.screen, ConfirmDialog)
            ), app.screen
            app.screen._forward_event(_mouse(MouseUp, 1, 1))
            await pilot.press("escape")
            assert await _wait_until(
                pilot, lambda: isinstance(app.screen, MainScreen)
            )

            assert main._selecting is False, "取消后主屏不得仍处于选字状态"
            assert main._select_state is None
            main._forward_event(_mouse(MouseMove, 5, 10))
            await pilot.pause(0.1)
            assert main.selections == {}, "移动鼠标不得画出选区"
    asyncio.run(scenario())


def test_compact_sidebar_takes_the_whole_page(app_root):
    """Ctrl-a b → 文件树整页（pane 区 / Git 块隐藏、宽 == 屏宽）；Esc / 点 tab 返回。"""
    async def scenario():
        app = _app(FakeBackend(2), app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)

            await pilot.press("ctrl+a")
            await pilot.press("b")
            await pilot.pause(0.2)
            assert app._compact_page == "tree"
            assert app.query_one("#pane-container").display is False
            assert app.query_one("#sidebar").display is True
            sidebar = app.query_one("#sidebar")
            assert await _wait_until(
                pilot, lambda: sidebar.outer_size.width == 46
            ), sidebar.outer_size
            assert app.focused is not None and app.focused.id == "file-tree"
            # 0.10.0 真机：两块各占整页——树要足够高才弹得开右键菜单；
            # 0.11.0 起顶栏 1 行 + 空行 1 行、底栏 3 行 → 22 - 1 - 1 - 3 = 17
            assert app.query_one("#git-area").display is False
            tree = app.query_one("#file-tree")
            assert await _wait_until(
                pilot, lambda: tree.outer_size.height == 17
            ), tree.outer_size

            # Esc：回活动 pane，页面随之关闭
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert app._compact_page is None
            assert app.query_one("#pane-container").display is True
            assert app.query_one("#pane-0", PaneWidget).display is True
            assert app.focused is app.query_one("#pane-0", PaneWidget)

            # 再开页面 → 点顶栏 tab：关页面并切到该槽
            await pilot.press("ctrl+a")
            await pilot.press("b")
            await pilot.pause(0.2)
            assert app._compact_page == "tree"
            await pilot.click("#tab-1")
            assert await _wait_until(
                pilot, lambda: app._compact_page is None and app._active_index == 1
            ), (app._compact_page, app._active_index)
            assert app.query_one("#pane-container").display is True
            assert _displayed_panes(app) == ["#pane-1"]
            assert app.focused is app.query_one("#pane-1", PaneWidget)
    asyncio.run(scenario())


def test_compact_git_page_shows_only_history(app_root):
    """Ctrl-a g → Git 历史整页（树隐藏、历史拿满高）；Ctrl-a s 切回树页。"""
    async def scenario():
        app = _app(FakeBackend(2), app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)

            await pilot.press("ctrl+a")
            await pilot.press("g")
            await pilot.pause(0.2)
            assert app._compact_page == "git"
            assert app.query_one("#pane-container").display is False
            assert app.query_one("#file-tree").display is False
            git_area = app.query_one("#git-area")
            assert git_area.display is True
            assert await _wait_until(
                pilot, lambda: git_area.outer_size.height == 17
            ), git_area.outer_size
            assert app.focused is not None and app.focused.id == "git-log"

            # 两页互斥：Ctrl-a s 直接换成树页，焦点跟着走
            await pilot.press("ctrl+a")
            await pilot.press("s")
            await pilot.pause(0.2)
            assert app._compact_page == "tree"
            assert app.query_one("#git-area").display is False
            assert app.query_one("#file-tree").display is True
            assert app.focused is not None and app.focused.id == "file-tree"

            # Esc 回 pane，两块恢复（下次打开仍是完整侧栏）
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert app._compact_page is None
            assert app.query_one("#file-tree").display is True
            assert app.query_one("#git-area").display is True
    asyncio.run(scenario())


def test_compact_drops_pane_border_and_restores_it(app_root):
    """紧凑时 content_size == outer_size（无边框）；退出后回到 outer - 2。"""
    async def scenario():
        app = _app(FakeBackend(1), app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)
            pane = app.query_one("#pane-0", PaneWidget)
            assert (pane.outer_size.width, pane.outer_size.height) == (46, 17)
            assert pane.content_size == pane.outer_size

            await pilot.resize_terminal(100, 30)
            assert await _wait_until(pilot, lambda: app._compact is False)
            assert await _wait_until(
                pilot,
                lambda: pane.content_size.width == pane.outer_size.width - 2,
            ), (pane.content_size, pane.outer_size)
            assert pane.content_size.height == pane.outer_size.height - 2
    asyncio.run(scenario())


def test_compact_applies_after_modal_closes(app_root):
    """预览层打开时改尺寸 → pop 后立刻按新尺寸落 DOM，不等下一轮 poll。"""
    async def scenario():
        app = _app(FakeBackend(1), app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.4)
            assert app._compact is False

            app.push_screen(Screen())
            await pilot.pause(0.1)
            await pilot.resize_terminal(46, 22)
            await pilot.pause(0.1)
            assert app._compact is False, "盖住期间不得改主屏 DOM"

            app.pop_screen()
            assert await _wait_until(pilot, lambda: app._compact is True)
            assert app.query_one("#sidebar").display is False
            assert _displayed_panes(app) == ["#pane-0"]
    asyncio.run(scenario())


# ---------- 3. 窄屏底栏（0.11.0） ----------

def test_keybar_visible_only_in_compact(app_root):
    """桌面档隐藏；46×22 显示；退出紧凑又隐藏。"""
    async def scenario():
        app = _app(FakeBackend(1), app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.4)
            bar = app.query_one("#keybar", KeyBar)
            assert bar.display is False
            assert app.query_one("#topbar").outer_size.height == 1
            assert app.query_one("#btn-keybar", TopBarButton).display is False

            await pilot.resize_terminal(46, 22)
            assert await _wait_until(pilot, lambda: bar.display is True)
            assert app.query_one("#key-chip-esc", KeyChip).display is True
            assert app.query_one("#topbar").outer_size.height == 1
            assert app.query_one("#btn-keybar", TopBarButton).display is True
            assert app.query_one("#btn-keybar", TopBarButton).has_class("active")

            await pilot.resize_terminal(100, 30)
            assert await _wait_until(pilot, lambda: bar.display is False)
    asyncio.run(scenario())


def test_compact_bars_are_tall_enough_to_tap(app_root):
    """顶栏 1 行；底栏 3 行，键是 3×3，靠左排列。"""
    async def scenario():
        app = _app(FakeBackend(1), app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)
            bar = app.query_one("#keybar", KeyBar)
            assert app.query_one("#topbar").outer_size.height == 1
            assert bar.outer_size.height == 3
            chip = app.query_one("#key-chip-esc", KeyChip)
            assert chip.region.size == (CHIP_WIDTH, CHIP_HEIGHT)
            assert chip.region.x == 0, "键行靠左，右侧留白"
            assert app.query_one("#btn-help", TopBarButton).region.height == 1
            assert app.query_one("#pane-0", PaneWidget).outer_size.height == 17
    asyncio.run(scenario())


def test_compact_topbar_gap_absorbs_mis_taps(app_root):
    """顶栏与 pane 之间留 1 行空：点偏到那一行不会落进 opencode 的标签栏。"""
    async def scenario():
        backend = FakeBackend(1)
        app = _app(backend, app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)
            pane = app.query_one("#pane-0", PaneWidget)
            assert pane.region.y == 2, "顶栏 1 行 + 1 行防误触空行"
            # 空行属于 Screen 背景：按下 / 抬起都不转发给内层终端。
            screen = app.screen
            screen._forward_event(_mouse(MouseDown, 10, 1))
            screen._forward_event(_mouse(MouseUp, 10, 1))
            await pilot.pause(0.2)
            assert backend.sent_keys == []
            assert backend.sent_text == []
    asyncio.run(scenario())


def test_compact_bars_use_grey_keycaps(app_root):
    """灰底 + 粗体；3×3 键，标签在中间那行。"""
    async def scenario():
        app = _app(FakeBackend(1), app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)
            top_grey = Color.parse("#333333")
            bar_grey = Color.parse("#2b2b2b")
            key_grey = Color.parse("#3a3a3a")
            assert app.query_one("#topbar").styles.background == top_grey
            assert app.query_one("#keybar", KeyBar).styles.background == bar_grey

            tab = app.query_one("#tab-0")
            chip = app.query_one("#key-chip-esc", KeyChip)
            button = app.query_one("#btn-help", TopBarButton)
            assert button.styles.background == top_grey
            assert chip.styles.background == key_grey
            assert tab.styles.text_style.bold
            assert chip.styles.text_style.bold
            assert button.styles.text_style.bold

            tab_lines = tab.render_lines(Region(0, 0, tab.size.width, tab.size.height))
            assert tab_lines[0].text.strip() == "1"

            chip_lines = chip.render_lines(
                Region(0, 0, chip.outer_size.width, chip.outer_size.height)
            )
            assert chip_lines[1].text.strip() == "ESC"
            assert chip_lines[0].text.strip() == ""
            assert chip_lines[2].text.strip() == ""
            assert chip.outer_size == (CHIP_WIDTH, CHIP_HEIGHT)
    asyncio.run(scenario())


def test_press_flashes_keycap_and_topbar_button(app_root):
    """按下反馈：键帽/顶栏按钮闪 150ms accent，然后自动消失。"""
    async def scenario():
        app = _app(FakeBackend(1), app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)
            main = app.screen
            chip = app.query_one("#key-chip-esc", KeyChip)
            # 点最底那一行：3 行高的键整块都是热区，不只是第一行。
            x, y = chip.region.x + 1, chip.region.y + 2
            main._forward_event(_mouse(MouseDown, x, y))
            await pilot.pause(0.05)
            assert chip.has_class("flash")
            await pilot.pause(0.2)
            assert not chip.has_class("flash")

            button = app.query_one("#btn-keybar", TopBarButton)
            x, y = button.region.x + 1, button.region.y
            main._forward_event(_mouse(MouseDown, x, y))
            await pilot.pause(0.05)
            assert button.has_class("flash")
            await pilot.pause(0.2)
            assert not button.has_class("flash")
    asyncio.run(scenario())


def test_keybar_toggle_hides_and_restores_rows(app_root):
    """顶栏 ``KEY``：隐藏底栏 → pane 多 3 行；再点恢复。"""
    async def scenario():
        app = _app(FakeBackend(1), app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)
            bar = app.query_one("#keybar", KeyBar)
            pane = app.query_one("#pane-0", PaneWidget)
            button = app.query_one("#btn-keybar", TopBarButton)
            assert bar.display is True
            assert pane.outer_size.height == 17
            assert button.has_class("active")

            await pilot.click("#btn-keybar")
            assert await _wait_until(pilot, lambda: bar.display is False)
            assert await _wait_until(
                pilot, lambda: pane.outer_size.height == 20
            ), pane.outer_size
            assert not button.has_class("active")

            await pilot.click("#btn-keybar")
            assert await _wait_until(pilot, lambda: bar.display is True)
            assert await _wait_until(
                pilot, lambda: pane.outer_size.height == 17
            ), pane.outer_size
            assert button.has_class("active")
    asyncio.run(scenario())


def test_keybar_off_gives_the_row_back(app_root, monkeypatch):
    """``MYRTUI_KEYBAR=off``：底栏不占行，pane 高度与 0.10.0 一致。"""
    monkeypatch.setenv("MYRTUI_KEYBAR", "off")

    async def scenario():
        app = _app(FakeBackend(1), app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)
            assert app._compact is True
            assert app.query_one("#keybar", KeyBar).display is False
            assert app.query_one("#btn-keybar", TopBarButton).display is False
            assert app.query_one("#topbar").outer_size.height == 1
            pane = app.query_one("#pane-0", PaneWidget)
            assert (pane.outer_size.width, pane.outer_size.height) == (46, 20)
            assert pane.content_size == pane.outer_size
    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("chip", "expected"),
    [
        ("esc", "Escape"),
        ("enter", "Enter"),
        ("shift_tab", "BTab"),
        ("up", "Up"),
        ("down", "Down"),
        ("newline", "C-j"),
        ("backspace", "BSpace"),
    ],
)
def test_keybar_chip_injects_the_key(app_root, chip, expected):
    """点击按钮 = 按同一个键：注入到活动 pane，且不经过前缀。"""
    async def scenario():
        backend = FakeBackend(1)
        app = _app(backend, app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)
            before = len(backend.sent_keys)
            await pilot.click(f"#key-chip-{chip}")
            assert await _wait_until(
                pilot, lambda: len(backend.sent_keys) > before
            ), backend.sent_keys
            assert backend.sent_keys[-1] == ("%0", (expected,))
    asyncio.run(scenario())


@pytest.mark.parametrize(("chip", "text"), [("slash", "/"), ("at", "@")])
def test_keybar_chip_injects_printable_text(app_root, chip, text):
    """``/``、``@`` 是文本前缀，走 send-text 而不是 send-keys。"""
    async def scenario():
        backend = FakeBackend(1)
        app = _app(backend, app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)
            await pilot.click(f"#key-chip-{chip}")
            assert await _wait_until(
                pilot, lambda: backend.sent_text
            ), backend.sent_text
            assert backend.sent_text[-1] == ("%0", text)
    asyncio.run(scenario())


def test_keybar_esc_returns_from_sidebar_page_without_injecting(app_root):
    """侧栏整页时点 Esc：回 pane、关页面，**不**把 Escape 发给 shell。"""
    async def scenario():
        backend = FakeBackend(1)
        app = _app(backend, app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)
            await pilot.press("ctrl+a")
            await pilot.press("b")
            await pilot.pause(0.2)
            assert app._compact_page == "tree"
            assert app.focused is not None and app.focused.id == "file-tree"

            before = len(backend.sent_keys)
            await pilot.click("#key-chip-page_esc")
            assert await _wait_until(pilot, lambda: app._compact_page is None)
            assert app.focused is app.query_one("#pane-0", PaneWidget)
            assert len(backend.sent_keys) == before
    asyncio.run(scenario())


def test_keybar_click_keeps_pane_focus_and_no_selection(app_root):
    """chip 不可聚焦、不可选字：点击后焦点仍在 pane，主屏没有选区。"""
    async def scenario():
        app = _app(FakeBackend(1), app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)
            pane = app.query_one("#pane-0", PaneWidget)
            main = app.screen
            assert app.focused is pane

            await pilot.click("#key-chip-esc")
            await pilot.pause(0.2)
            assert app.focused is pane
            assert main._selecting is False
            assert main.selections == {}
    asyncio.run(scenario())


def test_keybar_chip_closes_the_shortcut_help(app_root):
    """浮层开着时点底栏 → 先收起浮层，再执行按键。"""
    async def scenario():
        backend = FakeBackend(1)
        app = _app(backend, app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)
            await pilot.click("#btn-help")
            panel = app.query_one("#shortcut-help")
            assert panel.display is True
            assert panel.region.y == 2, "紧凑档浮层应贴在顶栏 + 防误触空行之下"
            assert panel.region.bottom <= app.query_one("#keybar").region.y

            before = len(backend.sent_keys)
            await pilot.click("#key-chip-esc")
            assert await _wait_until(pilot, lambda: panel.display is False)
            assert await _wait_until(
                pilot, lambda: len(backend.sent_keys) > before
            ), backend.sent_keys
            assert backend.sent_keys[-1] == ("%0", ("Escape",))
    asyncio.run(scenario())


def test_keybar_obeys_an_armed_ctrl_a_prefix(app_root):
    """前缀已 arm 时点 Esc = ``Ctrl-a Esc``（第二个键），不是普通 Esc。"""
    async def scenario():
        backend = FakeBackend(1)
        app = _app(backend, app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)
            await pilot.press("ctrl+a")
            assert app._prefix_armed is True

            before = len(backend.sent_keys)
            await pilot.click("#key-chip-esc")
            assert await _wait_until(
                pilot, lambda: len(backend.sent_keys) > before
            ), backend.sent_keys
            assert app._prefix_armed is False
            assert backend.sent_keys[-1] == ("%0", ("Escape",))
    asyncio.run(scenario())


def test_tree_page_bar_navigates_and_opens_menu(app_root):
    """文件树整页：底栏切到 ESC / ↑ / ↓ / ENT / ≡，且真正能操作树。"""
    async def scenario():
        app = _app(FakeBackend(1), app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)
            await pilot.press("ctrl+a")
            await pilot.press("b")
            await pilot.pause(0.4)
            assert app._compact_page == "tree"

            visible = [chip.id for chip in app.query(KeyChip) if chip.display]
            assert visible == [
                "key-chip-page_esc",
                "key-chip-page_up",
                "key-chip-page_down",
                "key-chip-page_enter",
                "key-chip-menu",
            ]

            tree = app.query_one("#file-tree")
            before = tree.cursor_line
            await pilot.click("#key-chip-page_down")
            await pilot.pause(0.2)
            assert tree.cursor_line > before
            await pilot.click("#key-chip-page_up")
            await pilot.pause(0.2)
            assert tree.cursor_line == before

            tree.cursor_line = _tree_lines(tree).index("notes.txt")
            await pilot.pause(0.1)
            await pilot.click("#key-chip-page_enter")
            assert await _wait_until(
                pilot, lambda: isinstance(app.screen, ContextMenuScreen)
            ), app.screen
            await pilot.press("escape")
            assert await _wait_until(
                pilot, lambda: isinstance(app.screen, MainScreen)
            )

            app.query_one("#file-tree").focus()
            tree.cursor_line = _tree_lines(tree).index("notes.txt")
            await pilot.pause(0.1)
            await pilot.click("#key-chip-menu")
            assert await _wait_until(
                pilot, lambda: isinstance(app.screen, ContextMenuScreen)
            ), app.screen
    asyncio.run(scenario())


def test_git_page_details_button_shows_the_commit_card(app_root):
    """Git 整页：Details 显示高亮提交的完整内容，再点收起。"""
    async def scenario():
        app = _app(FakeBackend(1), app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)
            await pilot.press("ctrl+a")
            await pilot.press("g")
            await pilot.pause(0.4)
            assert app._compact_page == "git"

            panel = app.query_one("#git-log", GitLogPanel)
            panel.show_commits(
                [
                    Commit(
                        sha="abc1234",
                        author="alice",
                        date="2026-11-05 10:00",
                        subject="fix: 测试提交",
                        body="正文第一行\n正文第二行",
                    ),
                    Commit(
                        sha="def5678",
                        author="bob",
                        date="2026-11-04 09:00",
                        subject="feat: 第二条",
                        body="第二条正文",
                    ),
                ],
                width=40,
            )
            await pilot.pause(0.2)
            panel.highlighted = 0
            visible = [chip.id for chip in app.query(KeyChip) if chip.display]
            assert visible == [
                "key-chip-page_esc",
                "key-chip-page_up",
                "key-chip-page_down",
                "key-chip-page_enter",
            ]

            await pilot.click("#key-chip-page_down")
            await pilot.pause(0.2)
            assert panel.highlighted == 1

            await pilot.click("#key-chip-page_enter")
            card = app.query_one("#commit-card", CommitCard)
            assert card.display is True
            body = card.query_one("#commit-card-body").render().plain
            assert "def5678" in body
            assert "第二条正文" in body
            assert card.region.right <= app.size.width
            assert card.region.bottom <= app.query_one("#keybar").region.y

            await pilot.click("#key-chip-page_enter")
            assert card.display is False
    asyncio.run(scenario())


def test_topbar_toggle_in_page_keeps_tree_focus(app_root):
    """整页打开时点顶栏 KEY：只改底栏显隐，不把焦点抢回 pane。"""
    async def scenario():
        app = _app(FakeBackend(1), app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)
            await pilot.press("ctrl+a")
            await pilot.press("b")
            await pilot.pause(0.4)
            tree = app.query_one("#file-tree")
            bar = app.query_one("#keybar", KeyBar)
            assert app.focused is tree
            assert bar.display is True

            await pilot.click("#btn-keybar")
            assert await _wait_until(pilot, lambda: bar.display is False)
            assert app._compact_page == "tree"
            assert app.focused is tree
            assert await _wait_until(
                pilot, lambda: tree.outer_size.height == 20
            ), tree.outer_size

            await pilot.click("#btn-keybar")
            assert await _wait_until(pilot, lambda: bar.display is True)
            assert app._compact_page == "tree"
            assert app.focused is tree
            assert await _wait_until(
                pilot, lambda: tree.outer_size.height == 17
            ), tree.outer_size
    asyncio.run(scenario())


def test_keybar_drops_low_priority_chips_at_30_cols(app_root):
    """30 列：方向键先被丢，Esc / ENT / C-J / DEL 仍在。"""
    async def scenario():
        app = _app(FakeBackend(4), app_root)
        async with app.run_test(size=(30, 22)) as pilot:
            await pilot.pause(0.4)
            assert app._compact is True
            assert app.query_one("#key-chip-esc", KeyChip).display is True
            assert app.query_one("#key-chip-enter", KeyChip).display is True
            assert app.query_one("#key-chip-newline", KeyChip).display is True
            assert app.query_one("#key-chip-backspace", KeyChip).display is True
            assert app.query_one("#key-chip-up", KeyChip).display is False
            assert app.query_one("#key-chip-down", KeyChip).display is False
            # 4 tab + 6 个 3-cell 按钮 = 30 cell，30 列刚好全在屏内；
            # B / G 放不下就整对隐藏，不能挤掉现有的 ?。
            assert app.query_one("#btn-tree", TopBarButton).display is False
            assert app.query_one("#btn-git", TopBarButton).display is False
            for btn_id in ("#btn-keybar", "#btn-help"):
                btn = app.query_one(btn_id, TopBarButton)
                assert btn.region.right <= 30, (btn_id, btn.region)
    asyncio.run(scenario())


def test_compact_topbar_fits_40_cols(app_root):
    """顶栏 36 cell；40 列仍留出状态位，8 个按钮全在屏内。"""
    async def scenario():
        app = _app(FakeBackend(1), app_root)
        async with app.run_test(size=(40, 22)) as pilot:
            await pilot.pause(0.4)
            assert app._compact is True
            for btn_id in ("#btn-tree", "#btn-git", "#btn-keybar", "#btn-help"):
                btn = app.query_one(btn_id, TopBarButton)
                assert btn.display is True, btn_id
                assert btn.region.right <= 40, (btn_id, btn.region)
    asyncio.run(scenario())


def test_topbar_tree_and_git_buttons_open_and_close_pages(app_root):
    """顶栏 B / G：直接开整页，再点关闭；对应按钮加 active 底色。"""
    async def scenario():
        app = _app(FakeBackend(2), app_root)
        async with app.run_test(size=(49, 23)) as pilot:
            await pilot.pause(0.4)
            tree_btn = app.query_one("#btn-tree", TopBarButton)
            git_btn = app.query_one("#btn-git", TopBarButton)
            assert tree_btn.display is True and git_btn.display is True

            await pilot.click("#btn-tree")
            assert await _wait_until(pilot, lambda: app._compact_page == "tree")
            assert app.focused is app.query_one("#file-tree")
            assert tree_btn.has_class("active")

            await pilot.click("#btn-git")
            assert await _wait_until(pilot, lambda: app._compact_page == "git")
            assert app.focused is not None and app.focused.id == "git-log"
            assert git_btn.has_class("active") and not tree_btn.has_class("active")

            await pilot.click("#btn-git")
            assert await _wait_until(pilot, lambda: app._compact_page is None)
            assert not git_btn.has_class("active")
            assert app.focused is app.query_one("#pane-0", PaneWidget)
    asyncio.run(scenario())


def test_page_buttons_hidden_outside_compact(app_root):
    """桌面档侧栏常显，B / G 不占顶栏（也不改变原有按钮顺序）。"""
    async def scenario():
        app = _app(FakeBackend(1), app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.4)
            assert app._compact is False
            assert app.query_one("#btn-tree", TopBarButton).display is False
            assert app.query_one("#btn-git", TopBarButton).display is False
    asyncio.run(scenario())


# ---------- 4. 回归确认（非紧凑尺寸 / 旧断言） ----------

def test_regression_context_menu_clamped_in_compact_size(app_root):
    """test_v20 的同款断言，在 40×12（会进紧凑档）下仍成立。"""
    async def scenario():
        app = _app(FakeBackend(1), app_root)
        async with app.run_test(size=(40, 12)) as pilot:
            await pilot.pause(0.4)
            assert app._compact is True
            # 5 个按钮 + 4 个 tab = 27 列，40 列下 `?` 仍在屏内
            assert app.query_one("#btn-help").region.right <= 40
            assert app.query_one("#btn-close").region.right <= 40
            app.push_screen(ContextMenuScreen([("copy_abs", "abs")], 999, 999))
            await pilot.pause(0.2)
            menu = app.screen.query_one("OptionList")
            assert menu.region.right <= app.size.width
            assert menu.region.bottom <= app.size.height
    asyncio.run(scenario())


def test_regression_slots_and_columns_at_120x40(app_root):
    """test_p1 的槽位 / #col-b 断言（120×40 非紧凑）。"""
    async def scenario():
        app = _app(FakeBackend(4), app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            assert app._compact is False
            assert app.query_one("#col-a").display is True
            assert app.query_one("#col-b").display is True
            assert _displayed_panes(app) == [
                "#pane-0", "#pane-1", "#pane-2", "#pane-3"
            ]
    asyncio.run(scenario())


def test_regression_dead_tab_label_at_120x40(app_root):
    """test_p2 的 `1:dead` 文案：只有紧凑档改渲染。"""
    async def scenario():
        app = _app(FakeBackend(1), app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            app._tab_labels = ["1:dead"]
            app._tab_deads = [True]
            app._update_tabs()
            assert app.query_one("#tab-0")._body.strip() == "1:dead"
    asyncio.run(scenario())


def test_regression_brand_visible_at_120x40(app_root):
    """test_i18n / test_p1 的品牌断言：桌面档品牌仍在最右侧。"""
    async def scenario():
        app = _app(FakeBackend(1), app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            brand = app.query_one("#topbar-brand")
            assert brand.display is True
            assert brand.content == BRAND_TEXT
            assert brand.region.right == app.size.width
    asyncio.run(scenario())


def test_regression_separator_visibility_at_120x40(app_root):
    """test_v11 的分隔线显隐（桌面路径）。"""
    async def scenario():
        app = _app(FakeBackend(4), app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            for sep_id in ("#sep-main", "#sep-tree", "#sep-cols",
                           "#sep-rows-a", "#sep-rows-b"):
                assert app.query_one(sep_id).display is True, sep_id
            await pilot.press("ctrl+a")
            await pilot.press("b")
            await pilot.pause(0.2)
            assert app.query_one("#sidebar").display is False
            assert app.query_one("#sep-main").display is False
            assert app.query_one("#sep-tree").display is False
    asyncio.run(scenario())
