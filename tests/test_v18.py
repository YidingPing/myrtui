"""myrtui v0.18 测试：IME 锚点（中文选字框跟随活动 pane 光标）。

根因（用户反馈「选字框固定出现在整个 myrtui 左上角、压住顶栏 1:zsh」）：
Textual **每帧帧尾**都会把外层终端的硬件光标
``move_to(App.cursor_position)``（``textual/app.py::_display``），而该属性
默认 ``Offset(0,0)``、只有内置 ``Input``/``TextArea`` 会更新——本仓界面里
一个输入控件都没有 → 锚点每帧被按回 ``(0,0)``，OS 输入法据此摆放选字框。

本文件锚点（用户拍板：回退「保持该 pane 上次光标」、侧栏「跟 pane 走」）：

- 有效内层光标 → 锚点 = ``content_region`` 原点 + 光标（poll 后断言）；
- 切焦点（``Ctrl-a 2`` 同路径）→ 锚点**同步**跟到新 pane，不等下轮 poll；
- 光标不可用（vim 隐藏 / dead / 翻 scrollback）→ 回退该 pane **上次**光标；
- 从未有过光标 → 内容区左上角（且 ≠ 屏幕 ``(0,0)``——顶栏至少占 1 行，
  这正是修复前后最直观的差异）；
- 光标越界 → clamp 进内容区；
- 侧栏聚焦 → 锚点**不动**（侧栏不接收中文输入）；
- 侧栏折叠 → ``content_region`` 移位 → 锚点跟上；
- 锚点变化时向驱动补发 CUP：headless 下 Textual 的 ``_display`` 完全不写
  （``not self.is_headless`` 早退）→ spy 到的序列只可能是我们自己发的。
"""
from __future__ import annotations

import asyncio

from textual.geometry import Offset

from myrtui.client.app import MainScreen, TerminalApp
from myrtui.client.pane_widget import PaneWidget
from myrtui.tmux_backend import CaptureResult, PaneInfo

PANE_TEXT = "abcdefghij\nsecond line\n"


def _pane(
    index: int,
    cursor: tuple[int, int] = (0, 0),
    cursor_flag: bool = True,
    dead: bool = False,
) -> PaneInfo:
    return PaneInfo(
        pane_id=f"p{index}",
        dead=dead,
        command="sh",
        alternate_on=False,
        cursor_x=cursor[0],
        cursor_y=cursor[1],
        cursor_flag=cursor_flag,
        history_size=100,
        height=24,
        width=80,
        window_id=f"w{index}",
        window_index=index,
        mouse_standard=False,
        mouse_button=False,
        mouse_any=False,
        mouse_sgr=True,
        mouse_all=False,
    )


class _FakeBackend:
    """假后端：每槽位可配光标坐标 / cursor_flag（不碰真 tmux）。

    ``cursors`` / ``flags`` 按槽位下标覆盖，默认 (0,0) / True。
    测试中途改这两个 dict 即可模拟「vim 藏光标」之类的态变。
    """

    def __init__(
        self,
        count: int = 1,
        cursors: dict[int, tuple[int, int]] | None = None,
        flags: dict[int, bool] | None = None,
    ):
        self.count = count
        self.cursors = cursors or {}
        self.flags = flags or {}
        self.sent: list[tuple] = []

    def _pane(self, index: int) -> PaneInfo:
        return _pane(
            index,
            cursor=self.cursors.get(index, (0, 0)),
            cursor_flag=self.flags.get(index, True),
        )

    def list_panes(self):
        return [self._pane(i) for i in range(self.count)]

    def poll(self, offsets):
        panes = self.list_panes()
        caps = {p.pane_id: CaptureResult(text=PANE_TEXT, offset=0) for p in panes}
        return panes, caps

    def resize_window(self, window_id, cols, rows):
        pass

    def paste(self, pane_id, text):
        pass

    def send_keys(self, pane_id, *keys):
        self.sent.append(("keys", pane_id, keys))

    def send_text(self, pane_id, text):
        self.sent.append(("text", pane_id, text))

    def send_mouse(self, pane_id, sequence):
        pass

    def send_ctrl_a(self, pane_id):
        self.sent.append(("ctrl-a", pane_id))


def _region(app, slot: int = 0):
    """活动布局下的 ``#pane-{slot}`` 内容区（1 cell 边框已扣除）。"""
    return app.query_one(f"#pane-{slot}", PaneWidget).content_region


def _anchor_for(region, cursor: tuple[int, int]) -> Offset:
    """锚点公式（与 ``_sync_ime_anchor`` 同口径：原点 + 光标 + clamp）。"""
    return Offset(
        region.x + max(0, min(cursor[0], region.width - 1)),
        region.y + max(0, min(cursor[1], region.height - 1)),
    )


# ---------- 1. 有效光标 → 锚点跟随 ----------

def test_anchor_follows_active_pane_cursor(app_root):
    async def scenario():
        backend = _FakeBackend(1, cursors={0: (5, 2)})
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.4)
            region = _region(app)
            # 内容区起点绝不可能是屏幕 (0,0)：顶栏占第 0 行、pane 有 1 cell 边框
            assert (region.x, region.y) != (0, 0), "内容区起点应避开顶栏"
            assert app.cursor_position == _anchor_for(region, (5, 2))
            assert app.cursor_position != Offset(0, 0), (
                "修复前锚点恒为 (0,0)——选字框压住左上角「1:zsh」的根因"
            )
    asyncio.run(scenario())


# ---------- 2. 切焦点 → 锚点同步跟到新 pane ----------

def test_anchor_moves_on_focus_switch(app_root):
    async def scenario():
        backend = _FakeBackend(2, cursors={0: (5, 2), 1: (10, 6)})
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.4)
            assert app.cursor_position == _anchor_for(_region(app, 0), (5, 2))

            app.action_switch_pane(1)  # = Ctrl-a 2 → _set_active(1)
            # 同步断言：_set_active 内直接 sync，不等下一轮 poll / 重绘
            assert app.cursor_position == _anchor_for(_region(app, 1), (10, 6))

            app.action_switch_pane(0)
            assert app.cursor_position == _anchor_for(_region(app, 0), (5, 2))
    asyncio.run(scenario())


# ---------- 3. 光标不可用 → 回退该 pane 上次光标 ----------

def test_anchor_keeps_last_cursor_when_cursor_hidden(app_root):
    async def scenario():
        backend = _FakeBackend(1, cursors={0: (5, 2)}, flags={0: True})
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.4)
            before = app.cursor_position
            assert before == _anchor_for(_region(app), (5, 2))

            # 模拟 vim 藏光标 / pane dead / 翻 scrollback：cursor 变 None
            backend.flags[0] = False
            await pilot.pause(0.4)
            assert app.cursor_position == before, (
                "cursor 不可用时应回退该 pane 上次光标，而非跳走"
            )
    asyncio.run(scenario())


# ---------- 4. 从未有过光标 → 内容区左上角（≠ 屏幕 (0,0)） ----------

def test_anchor_falls_back_to_content_origin_when_never_had_cursor(app_root):
    async def scenario():
        backend = _FakeBackend(1, flags={0: False})
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.4)
            region = _region(app)
            assert app.cursor_position == Offset(region.x, region.y)
            assert app.cursor_position != Offset(0, 0), (
                "无光标回退也要落在 pane 内容区，绝不能回到屏幕 (0,0)"
            )
    asyncio.run(scenario())


# ---------- 5. 光标越界 → clamp 进内容区 ----------

def test_anchor_clamped_into_content_region(app_root):
    async def scenario():
        backend = _FakeBackend(1, cursors={0: (999, 999)})
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.4)
            region = _region(app)
            assert app.cursor_position == Offset(
                region.x + region.width - 1, region.y + region.height - 1
            )
    asyncio.run(scenario())


# ---------- 6. 侧栏聚焦 → 锚点不动（仍指活动 pane） ----------

def test_anchor_stays_on_pane_when_sidebar_focused(app_root):
    async def scenario():
        backend = _FakeBackend(1, cursors={0: (5, 2)})
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.4)
            before = app.cursor_position
            assert before == _anchor_for(_region(app), (5, 2))

            app.action_focus_sidebar()  # Ctrl-a s
            await pilot.pause(0.1)  # focus() 是异步落焦点（下一事件轮才生效）
            assert app._sidebar_focused()
            await pilot.pause(0.4)
            assert app.cursor_position == before, (
                "侧栏不接收中文输入，锚点应保持在 pane 光标处（拍板项）"
            )
    asyncio.run(scenario())


# ---------- 7. 侧栏折叠 → content_region 移位 → 锚点跟上 ----------

def test_anchor_updates_after_sidebar_toggle(app_root):
    async def scenario():
        backend = _FakeBackend(1, cursors={0: (5, 2)})
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.4)
            old_region = _region(app)
            assert app.cursor_position == _anchor_for(old_region, (5, 2))

            app.action_toggle_sidebar()  # Ctrl-a b → 侧栏折叠，pane 左移变宽
            await pilot.pause(0.4)
            new_region = _region(app)
            assert new_region != old_region, "折叠侧栏后 pane 内容区必须移位"
            assert app.cursor_position == _anchor_for(new_region, (5, 2))
    asyncio.run(scenario())


# ---------- 8. 锚点变化 → 向驱动补发 CUP（帧间直写） ----------

def test_anchor_change_writes_cup_to_driver(app_root):
    async def scenario():
        backend = _FakeBackend(1, cursors={0: (5, 2)})
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.4)
            assert app.cursor_position == _anchor_for(_region(app), (5, 2))

            # headless 下 Textual 的 _display 不写任何输出（is_headless 早退），
            # 所以 spy 到的每一条都只可能来自 _write_ime_anchor
            writes: list[str] = []
            app._driver.write = lambda data: writes.append(data)

            app.action_toggle_sidebar()  # 布局移位 → 锚点变化
            await pilot.pause(0.4)
            pos = app.cursor_position
            expected = f"\x1b[{pos.y + 1};{pos.x + 1}H"  # Control.move_to 的 CUP
            assert expected in writes, (
                f"锚点变化应补发 CUP {expected!r}，实发 {writes!r}"
            )
    asyncio.run(scenario())


# ---------- 9. 拓扑变化 → 消失 pane 的 _last_cursors 被清理 ----------

def test_last_cursors_cleaned_for_removed_pane(app_root):
    async def scenario():
        backend = _FakeBackend(2, cursors={0: (5, 2), 1: (10, 6)})
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.4)
            assert set(app._last_cursors) == {"p0", "p1"}

            # 外部关掉 p1 → 拓扑收缩到 1 槽
            app._topology_apply([_pane(0, cursor=(5, 2))])
            assert set(app._last_cursors) == {"p0"}, (
                "消失 pane 的 per-pane 锚点状态必须随拓扑清理（防无界增长）"
            )
            assert isinstance(app.screen, MainScreen)
    asyncio.run(scenario())
