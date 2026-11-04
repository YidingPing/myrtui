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

import pytest
from textual.screen import Screen

from myrtui import layout_state
from myrtui.client import app as app_mod
from myrtui.client.app import (
    COMPACT_ENTER_HEIGHT,
    COMPACT_ENTER_WIDTH,
    COMPACT_EXIT_HEIGHT,
    COMPACT_EXIT_WIDTH,
    TerminalApp,
    compact_for,
    resolve_compact_mode,
)
from myrtui.client.context_menu import ContextMenuScreen
from myrtui.client.pane_widget import PaneWidget
from myrtui.client.topbar import BRAND_TEXT, TopBarButton
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

    # ---- 输入 / 生命周期（本套用例不关心，保持可调用） ----
    def send_text(self, pane_id, text):
        pass

    def send_keys(self, pane_id, *keys):
        pass

    def send_ctrl_a(self, pane_id):
        pass

    def send_mouse(self, pane_id, sequence):
        pass

    def paste(self, pane_id, text):
        pass

    def new_window(self, root):
        return "$new"

    def kill_window(self, window_id):
        pass

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
            # 压缩后的 `?` 仍在屏内（这是 0.9.x 的真机问题）
            help_btn = app.query_one("#btn-help", TopBarButton)
            assert help_btn.region.right <= 46
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

            # 新可见的槽位收到一次内容区尺寸（紧凑档无边框 → 46×21）
            assert await _wait_until(
                pilot, lambda: any(w == "$w2" for w, _c, _r in backend.resize_calls)
            ), backend.resize_calls
            sizes = [(c, r) for w, c, r in backend.resize_calls if w == "$w2"]
            assert sizes == [(46, 21)], sizes
    asyncio.run(scenario())


def test_compact_sidebar_takes_the_whole_page(app_root):
    """Ctrl-a b → 整页侧栏（pane 区隐藏、宽 == 屏宽、焦点在树）；Esc / 点 tab 返回。"""
    async def scenario():
        app = _app(FakeBackend(2), app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)

            await pilot.press("ctrl+a")
            await pilot.press("b")
            await pilot.pause(0.2)
            assert app._compact_page is True
            assert app.query_one("#pane-container").display is False
            assert app.query_one("#sidebar").display is True
            sidebar = app.query_one("#sidebar")
            assert await _wait_until(
                pilot, lambda: sidebar.outer_size.width == 46
            ), sidebar.outer_size
            assert app.focused is not None and app.focused.id == "file-tree"

            # Esc：回活动 pane，页面随之关闭
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert app._compact_page is False
            assert app.query_one("#pane-container").display is True
            assert app.query_one("#pane-0", PaneWidget).display is True
            assert app.focused is app.query_one("#pane-0", PaneWidget)

            # 再开页面 → 点顶栏 tab：关页面并切到该槽
            await pilot.press("ctrl+a")
            await pilot.press("b")
            await pilot.pause(0.2)
            assert app._compact_page is True
            await pilot.click("#tab-1")
            assert await _wait_until(
                pilot, lambda: app._compact_page is False and app._active_index == 1
            ), (app._compact_page, app._active_index)
            assert app.query_one("#pane-container").display is True
            assert _displayed_panes(app) == ["#pane-1"]
            assert app.focused is app.query_one("#pane-1", PaneWidget)
    asyncio.run(scenario())


def test_compact_drops_pane_border_and_restores_it(app_root):
    """紧凑时 content_size == outer_size（无边框）；退出后回到 outer - 2。"""
    async def scenario():
        app = _app(FakeBackend(1), app_root)
        async with app.run_test(size=(46, 22)) as pilot:
            await pilot.pause(0.4)
            pane = app.query_one("#pane-0", PaneWidget)
            assert (pane.outer_size.width, pane.outer_size.height) == (46, 21)
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


# ---------- 3. 回归确认（非紧凑尺寸 / 旧断言） ----------

def test_regression_context_menu_clamped_in_compact_size(app_root):
    """test_v20 的同款断言，在 40×12（会进紧凑档）下仍成立。"""
    async def scenario():
        app = _app(FakeBackend(1), app_root)
        async with app.run_test(size=(40, 12)) as pilot:
            await pilot.pause(0.4)
            assert app._compact is True
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
