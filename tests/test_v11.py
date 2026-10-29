"""myrtui v0.11 测试：可拖拽分隔线 + 布局比例持久化。

覆盖本轮三项改动：

1. **分隔线**：侧栏↔pane 区、文件树↔Git 面板、上下行、行内左右各一条；
   细线默认，**焦点所在区域用粗线**；
2. **鼠标拖动**改比例（拖动 → 该维度的比例变 → 两块的实际尺寸跟着变）；
3. **下次 attach 恢复**：拖完落盘 → 同项目重新 attach 时按上次的位置起。

分层：纯数据（``layout_state``）→ 控件（``Separator`` 渲染）→ 集成（App 里的
拖拽 / 焦点粗线 / 落盘恢复）。
"""
from __future__ import annotations

import asyncio
import json
import os

from rich.color import ColorTriplet
from textual.events import Click, MouseDown, MouseMove, MouseUp
from textual.screen import ModalScreen
from textual.widgets import Button

from myrtui import layout_state
from myrtui.client.app import TerminalApp
from myrtui.client.file_tree import FileTree
from myrtui.client.pane_widget import PaneWidget
from myrtui.client.separators import Separator
from myrtui.tmux_backend import PaneInfo


# ---------- 夹具 / 辅助 ----------

def _pane(index: int) -> PaneInfo:
    """造一个假的 pane 信息（布局测试不碰 tmux，只要拓扑）。"""
    return PaneInfo(
        pane_id=f"p{index}", dead=False, command="sh", alternate_on=False,
        cursor_x=0, cursor_y=0, cursor_flag=False, history_size=10,
        height=24, width=80, window_id=f"w{index}", window_index=index,
        mouse_standard=False, mouse_button=False, mouse_any=False,
        mouse_sgr=True, mouse_all=False,
    )


class _FakeBackend:
    """假后端：只提供拓扑（列表 / poll），布局测试用不到 tmux。"""

    def __init__(self, count: int = 2):
        self.count = count
        self.resized: list[tuple[str, int, int]] = []

    def list_panes(self):
        return [_pane(i) for i in range(self.count)]

    def poll(self, offsets):
        return self.list_panes(), {}

    def resize_window(self, window_id, cols, rows):
        self.resized.append((window_id, cols, rows))


async def _drag(app, pilot, sep_id: str, x: int, y: int) -> None:
    """模拟「按下分隔线 → 拖到 (x, y) → 松开」。

    move 事件必须带 ``button=1``（按住拖），Pilot.hover 发的是 button=0 的
    悬停事件，模拟不了拖拽。
    """
    sep = app.query_one(f"#{sep_id}", Separator)
    await pilot.mouse_down(f"#{sep_id}", offset=(0, 0))
    app.screen._forward_event(
        MouseMove(
            widget=sep, x=0.0, y=0.0, delta_x=1, delta_y=0, button=1,
            shift=False, meta=False, ctrl=False,
            screen_x=float(x), screen_y=float(y),
        )
    )
    await pilot.pause(0.2)
    await pilot.mouse_up(f"#{sep_id}", offset=(0, 0))
    await pilot.pause(0.2)


async def _drag_at(app, pilot, x: int, y: int, to_x: int, to_y: int) -> None:
    """在屏幕坐标 (x, y) 按下 → 拖到 (to_x, to_y) → **在终点松开**。

    用来测**热区**：按下的位置不一定正好落在线上。
    必须在终点释放：``pilot.mouse_up`` 会先补一条带按钮的 MouseMove 到释放点，
    若在原按压点释放，这条 move 会把比例拽回起点（拖了等于没拖）。
    """
    target = app.screen.get_widget_at(x, y)[0]
    await pilot.mouse_down(target, offset=(x - target.region.x, y - target.region.y))
    app.screen._forward_event(
        MouseMove(
            widget=target, x=0.0, y=0.0, delta_x=1, delta_y=0, button=1,
            shift=False, meta=False, ctrl=False,
            screen_x=float(to_x), screen_y=float(to_y),
        )
    )
    await pilot.pause(0.2)
    await pilot.mouse_up(target, offset=(to_x - target.region.x, to_y - target.region.y))
    await pilot.pause(0.2)


class _ProbeModal(ModalScreen):
    """只用来占住屏幕的探针弹窗（验证热区不穿透到主屏）。"""

    def compose(self):
        yield Button("probe", id="probe")


# ---------- 纯数据：layout_state ----------

def test_layout_state_roundtrip(tmp_path):
    """比例存盘 → 读回：四个维度都在，缺项补默认。"""
    path = str(tmp_path / "l.json")
    ratios = {"sidebar": 0.4, "tree": 0.6, "rows": 0.3, "cols": 0.7}
    layout_state.save_ratios(path, ratios)
    assert layout_state.load_ratios(path) == ratios
    # 落盘格式：带版本号，便于以后改结构
    payload = json.loads(open(path, encoding="utf-8").read())
    assert payload["version"] == layout_state.FILE_VERSION
    assert payload["ratios"]["sidebar"] == 0.4


def test_layout_state_missing_and_corrupt_fall_back(tmp_path):
    """文件缺失 / 坏 JSON / 结构不对 → 一律回落默认值（attach 必须能起来）。"""
    assert layout_state.load_ratios(None) == layout_state.DEFAULT_RATIOS
    assert layout_state.load_ratios(str(tmp_path / "nope.json")) == (
        layout_state.DEFAULT_RATIOS
    )
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert layout_state.load_ratios(str(broken)) == layout_state.DEFAULT_RATIOS
    wrong = tmp_path / "wrong.json"
    wrong.write_text('{"ratios": [1, 2, 3]}', encoding="utf-8")
    assert layout_state.load_ratios(str(wrong)) == layout_state.DEFAULT_RATIOS


def test_layout_state_normalize_clamps():
    """越界 / 非数字 / NaN → 夹紧或回落默认值。"""
    out = layout_state.normalize(
        {"sidebar": 5.0, "tree": -1.0, "rows": "x", "cols": float("nan")}
    )
    assert out["sidebar"] == layout_state.MAX_RATIO
    assert out["tree"] == layout_state.MIN_RATIO
    assert out["rows"] == layout_state.DEFAULT_RATIOS["rows"]
    assert out["cols"] == layout_state.DEFAULT_RATIOS["cols"]


def test_layout_state_save_is_atomic(tmp_path):
    """落盘不留临时文件（temp + rename 的原子写）。"""
    path = str(tmp_path / "l.json")
    layout_state.save_ratios(path, {"sidebar": 0.4})
    leftovers = [p for p in os.listdir(tmp_path) if p.startswith(".layout-")]
    assert leftovers == []


def test_layout_state_old_version_falls_back(tmp_path):
    """v0.12：version 1 的旧文件 → **作废**回落默认值。

    旧版是行优先（``rows`` = 上下两行），新版是列优先（``rows`` = 列内上下），
    同一份数字的**几何含义已经变了** → 版本不匹配一律回落默认（用户拍板：
    升版本重置，而不是默默套用旧比例）。
    """
    path = tmp_path / "old.json"
    path.write_text(
        json.dumps({"version": 1, "ratios": {"sidebar": 0.4, "rows": 0.3}}),
        encoding="utf-8",
    )
    assert layout_state.load_ratios(str(path)) == layout_state.DEFAULT_RATIOS
    # 当前版本的文件照常读回
    good = tmp_path / "new.json"
    layout_state.save_ratios(str(good), {"sidebar": 0.4})
    assert layout_state.load_ratios(str(good))["sidebar"] == 0.4
    assert layout_state.FILE_VERSION == 2


# ---------- 控件：Separator 渲染 ----------

def test_separator_glyphs_always_thin():
    """分隔线恒为细线（v0.11.2：粗线/焦点指示已从 Separator 移除）。

    焦点视觉改由 PaneWidget 的边框（``:focus`` → heavy $success）与
    侧栏聚焦由**按半区**的底色承担（v0.11.3：``#file-tree:focus-within`` /
    ``#git-area:focus-within``，明确不需要绿框）。
    """
    vertical = Separator("vertical")
    horizontal = Separator("horizontal")
    assert vertical.render() == "│"
    assert horizontal.render() == "─"
    # heavy 反应式已删除（不再有 -heavy 类 / 粗线字形）
    assert not hasattr(vertical, "heavy")
    assert not hasattr(horizontal, "heavy")
    # 运行中也不会冒出粗线字形
    assert "┃" not in vertical.glyph_text(1, 4)
    assert "━" not in horizontal.glyph_text(6, 1)


def test_separator_vertical_fills_height():
    """竖线按高度逐行铺满（1 cell 宽 × N 行高）。"""
    vertical = Separator("vertical")
    assert vertical.glyph_text(1, 4) == "│\n│\n│\n│"
    horizontal = Separator("horizontal")
    assert horizontal.glyph_text(6, 1) == "──────"


# ---------- 集成：App 里的分隔线 ----------

def test_sidebar_default_width_is_15_percent(app_root):
    """侧栏默认宽度 = 15%（v0.11.2：25% 仍太宽，只够文件树即可）。

    默认比例同时存在于两处，必须一致：``layout_state``（运行时真值）与
    app.py 的 CSS（第一帧的值）——不一致会首帧跳一下。
    """
    from myrtui.client.app import TerminalApp

    assert layout_state.DEFAULT_RATIOS["sidebar"] == 0.15
    css = TerminalApp.CSS
    sidebar_rule = css[css.index("#sidebar"):css.index("#pane-container")]
    assert "width: 15fr" in sidebar_rule
    assert "min-width: 20" in sidebar_rule

    async def scenario():
        backend = _FakeBackend(2)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            # 120 列：15% ≈ 17 列 < min-width 20 → 取 20（min-width 兜底）
            width = app.query_one("#sidebar").region.width
            assert width == 20, width
            assert app._ratios["sidebar"] == 0.15
    asyncio.run(scenario())


def test_separators_exist_and_hide_with_topology(app_root):
    """分隔线随拓扑显隐：1 pane → 列内上下 / 左右列的线都藏起来。"""
    async def scenario():
        backend = _FakeBackend(1)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            assert app.query_one("#sep-main", Separator).display is True
            assert app.query_one("#sep-tree", Separator).display is True
            assert app.query_one("#sep-rows-a", Separator).display is False
            assert app.query_one("#sep-cols", Separator).display is False
            assert app.query_one("#sep-rows-b", Separator).display is False
            # 侧栏折叠 → 侧栏旁的线一起藏
            app.action_toggle_sidebar()
            await pilot.pause(0.2)
            assert app.query_one("#sep-main", Separator).display is False
            assert app.query_one("#sep-tree", Separator).display is False
    asyncio.run(scenario())


def test_drag_sidebar_changes_width(app_root):
    """拖竖线 → 侧栏变宽、pane 区变窄，比例同步更新。"""
    async def scenario():
        backend = _FakeBackend(2)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            before = app.query_one("#sidebar").region.width
            await _drag(app, pilot, "sep-main", 60, 10)
            after = app.query_one("#sidebar").region.width
            assert after > before, (before, after)
            assert app._ratios["sidebar"] > layout_state.DEFAULT_RATIOS["sidebar"]
            # 侧栏变宽 → pane 变窄（两者之和 = 容器宽）
            assert app.query_one("#sidebar").region.width + (
                app.query_one("#pane-container").region.width
            ) + 1 == 120
    asyncio.run(scenario())


def test_grab_zone_allows_drag_near_the_line(app_root):
    """**热区**：按在分隔线偏 1 格处能拖；2 格不再命中（v0.20 从 ±2 收回）。

    分隔线只有 1 cell 宽，终端按 cell 报坐标 → 手抖偏 1 格就落到旁边的
    pane 上，看着有线却拖不动；±1 是「够用且不过度侵占邻区」的半径。
    """
    async def scenario():
        backend = _FakeBackend(2)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            sep = app.query_one("#sep-main", Separator)
            before = app._ratios["sidebar"]
            # 按在分隔线右边 1 格（落在 pane-0 上）→ 仍然要拖得动
            await _drag_at(app, pilot, sep.region.x + 1, 10, 60, 10)
            assert app._ratios["sidebar"] > before, app._ratios
            # 复位后按在右边 2 格（v0.20 起已在热区之外）→ 不再起拖
            app._ratios["sidebar"] = before
            app._apply_ratios()
            await pilot.pause(0.2)
            sep = app.query_one("#sep-main", Separator)
            await _drag_at(app, pilot, sep.region.x + 2, 10, 70, 10)
            assert app._ratios["sidebar"] == before, app._ratios
            # 按在远处（pane 中间）→ 不是分隔线热区 → 不启动拖拽
            app._ratios["sidebar"] = before
            await _drag_at(app, pilot, 90, 10, 100, 10)
            assert app._ratios["sidebar"] == before
    asyncio.run(scenario())


def test_grab_zone_keeps_focus_on_pressed_pane(app_root):
    """热区命中时焦点仍切到被按的 pane（键盘透传不中断）。"""
    async def scenario():
        backend = _FakeBackend(2)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            app._set_active(1)
            await pilot.pause(0.2)
            sep = app.query_one("#sep-main", Separator)
            pane0 = app.query_one("#pane-0", PaneWidget)
            # 按在分隔线右侧 1 格 → 落在 pane-0 上 → 拖分隔线，且焦点切到 pane-0
            offset = (sep.region.x + 1 - pane0.region.x, 10)
            await pilot.mouse_down("#pane-0", offset=offset)
            await pilot.pause(0.2)
            assert app._drag_sep is not None
            assert app._active_index == 0
            await pilot.mouse_up("#pane-0", offset=offset)
            await pilot.pause(0.2)
    asyncio.run(scenario())


def test_drag_button_left_and_right(app_root):
    """v0.11.5：拖拽认**左键与右键**，中键不参与；双击复位两键皆可。

    历史口径：v0.11.3 曾收口为只认左键（右键拖拽「不符合直觉」），但用户
    真机定位出特殊环境——UU 远程等嵌入 tmux 的终端里**左键事件不可达**而
    右键可达 → 恢复双键拖拽（用户拍板：热区内右键不透传内层程序可接受）。
    """
    async def scenario():
        backend = _FakeBackend(2)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            # ① 中键按在线本体 → 不启动拖拽
            await pilot.mouse_down("#sep-main", offset=(0, 8), button=2)
            await pilot.pause(0.2)
            assert app._drag_sep is None, "中键不得启动拖拽"
            await pilot.mouse_up("#sep-main", offset=(0, 8))
            await pilot.pause(0.2)
            # ② 右键按在线本体 → 启动拖拽（UU 远程场景的救命键）
            await pilot.mouse_down("#sep-main", offset=(0, 8), button=3)
            await pilot.pause(0.2)
            assert app._drag_sep is not None, "右键应启动拖拽"
            await pilot.mouse_up("#sep-main", offset=(0, 8))
            await pilot.pause(0.2)
            # ③ 右键按 pane 内热区（±1）→ 同样启动 + 切焦点
            app._set_active(1)
            await pilot.pause(0.2)
            sep = app.query_one("#sep-main", Separator)
            pane0 = app.query_one("#pane-0")
            offset = (sep.region.x + 1 - pane0.region.x, 8)
            await pilot.mouse_down("#pane-0", offset=offset, button=3)
            await pilot.pause(0.2)
            assert app._drag_sep is not None, "pane 内热区的右键也应启动拖拽"
            assert app._active_index == 0, "热区按下的焦点切换与按钮无关"
            await pilot.mouse_up("#pane-0", offset=offset)
            await pilot.pause(0.2)
            # ④ 左键在线本体 → 语义不变
            await pilot.mouse_down("#sep-main", offset=(0, 8), button=1)
            await pilot.pause(0.2)
            assert app._drag_sep is not None
            await pilot.mouse_up("#sep-main", offset=(0, 8))
            await pilot.pause(0.2)
            # ⑤ 左/右双击复位（两键皆可）
            app._ratios["sidebar"] = 0.4
            app._apply_ratios()
            await pilot.pause(0.2)
            await pilot.click("#sep-main", offset=(0, 8), button=3, times=2)
            await pilot.pause(0.3)
            assert app._ratios["sidebar"] == layout_state.DEFAULT_RATIOS["sidebar"]
            app._ratios["sidebar"] = 0.4
            app._apply_ratios()
            await pilot.pause(0.2)
            await pilot.click("#sep-main", offset=(0, 8), button=1, times=2)
            await pilot.pause(0.3)
            assert app._ratios["sidebar"] == layout_state.DEFAULT_RATIOS["sidebar"]
    asyncio.run(scenario())


def test_drag_via_real_app_pipeline(app_root):
    """v0.11.4 真机管道（终端驱动 → ``app.post_message`` → App 前置截获）全链。

    **这是与 pilot 测试不同的路径**：pilot 直接 ``screen._forward_event``（绕
    过 App.on_event），真机驱动把事件投到 App → ``TerminalApp.on_event``
    的前置截获。Windows Terminal + SSH / UU 远程等环境下拖拽曾在此路径失效，
    v0.11.4 起整链收到 App 层：左键整链（down → 多次 move → up）与右键整链
    （v0.11.5 双键口径）都要在 **App 层**完成拖拽；pane 内热区按下切焦点。

    ``test_dragging_separator_does_not_click_tree_node`` 已覆盖「前置截获
    不产生 click」，本用例覆盖比例换算与收摊。
    """
    async def scenario():
        backend = _FakeBackend(2)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)

            def post(cls, x, yy, button):
                app.post_message(cls(
                    widget=None, x=float(x), y=float(yy), delta_x=1, delta_y=0,
                    button=button, shift=False, meta=False, ctrl=False,
                    screen_x=float(x), screen_y=float(yy),
                ))

            for button in (1, 3):  # v0.11.5：左/右键整链等价
                before = app._ratios["sidebar"]
                x0 = app.query_one("#sep-main", Separator).region.x
                # ① pane 内热区按下 → 切焦点 + 起拖
                app._set_active(1)
                await pilot.pause(0.2)
                post(MouseDown, x0 + 1, 10, button)
                await pilot.pause(0.25)
                assert app._drag_sep is not None, f"按钮 {button} 起拖（App 级截获）"
                assert app._active_index == 0, "pane 内热区按下应切焦点（v0.11.1 语义）"
                # ② move ×2：比例跟随（不依赖 widget 层分发与 capture）
                post(MouseMove, x0 + 6, 10, button)
                await pilot.pause(0.2)
                post(MouseMove, x0 + 16, 10, button)
                await pilot.pause(0.3)
                assert app._ratios["sidebar"] > before, (before, app._ratios["sidebar"])
                # ③ up → 收摊（capture 释放）
                post(MouseUp, x0 + 16, 10, button)
                await pilot.pause(0.25)
                assert app._drag_sep is None and app.mouse_captured is None
                # 中键仍不参与：
                app._ratios["sidebar"] = before
                app._apply_ratios()
                await pilot.pause(0.2)
                x0 = app.query_one("#sep-main", Separator).region.x
                post(MouseDown, x0, 10, 2)
                await pilot.pause(0.25)
                assert app._drag_sep is None, "中键不得起拖"
                post(MouseMove, x0 + 12, 10, 2)
                await pilot.pause(0.2)
                post(MouseUp, x0 + 12, 10, 2)
                await pilot.pause(0.25)
                assert app._ratios["sidebar"] == before
                assert app._drag_sep is None and app.mouse_captured is None
    asyncio.run(scenario())


def test_grab_zone_works_from_tree_and_git_sides(app_root):
    """v0.11.2 的补丁 + v0.20 收窄：侧栏内两条横线入口仍可用。

    v0.11.2 把树 / Git 面板滚动条归零、热区放宽到 ±2，打通了三条按下路径；
    v0.20 热区收到 ±1 后：树底行 / Git 标题行（各距 `sep-tree` 1 格）仍可拖，
    而**树内最右格距 `sep-main` 2 格**，已不在热区内——侧栏宽度请从 pane 一侧
    拖。本用例同时把这条新边界钉死（防止有人把热区又悄悄放宽）。
    """
    async def scenario():
        backend = _FakeBackend(2)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)

            def reset():
                app._ratios.update(layout_state.DEFAULT_RATIOS)
                app._apply_ratios()

            # 1) 树内最右格（距 sep-main 恰好 2 格）→ v0.20 起不再命中热区
            sep_main = app.query_one("#sep-main", Separator).region
            tree = app.query_one("#file-tree").region
            tc = tree.x + tree.width - 1
            assert sep_main.x - tc == 2, (sep_main, tree)  # 前提自检
            await _drag_at(app, pilot, tc, 10, tc - 6, 10)
            assert app._ratios["sidebar"] == layout_state.DEFAULT_RATIOS["sidebar"]

            # 2) 树底行（sep-tree 上方 1 格）→ 拖得动
            reset()
            await pilot.pause(0.2)
            sep_tree = app.query_one("#sep-tree", Separator).region
            await _drag_at(app, pilot, sep_tree.x + 5, sep_tree.y - 1,
                           sep_tree.x + 5, sep_tree.y - 5)
            assert app._ratios["tree"] < layout_state.DEFAULT_RATIOS["tree"]

            # 3) Git 标题行（sep-tree 下方 1 格）→ 拖得动
            reset()
            await pilot.pause(0.2)
            sep_tree = app.query_one("#sep-tree", Separator).region
            await _drag_at(app, pilot, sep_tree.x + 5, sep_tree.y + 1,
                           sep_tree.x + 5, sep_tree.y + 6)
            assert app._ratios["tree"] > layout_state.DEFAULT_RATIOS["tree"]
    asyncio.run(scenario())


def test_dragging_separator_does_not_click_tree_node(tmp_path):
    """拖分隔线**不生成 click**（否则文件树会被顺带选中 / 目录被展开）。

    Textual 靠「按下与松开落在同一控件」生成 click（发生在 ``App.on_event``）；
    拖分隔线时手起手落都在文件树上 → 会多出一个 click → 树的那一行被选中、
    目录被 toggle 展开。

    **注意**：``Pilot`` 的鼠标事件直接走 ``Screen._forward_event``，**绕过**
    ``App.on_event`` → 不会生成 click → 这条用例必须自己往 App 投事件，
    否则测不到真实终端里的那条路径。
    """
    root = tmp_path / "proj"
    root.mkdir()
    for i in range(40):  # 文件够多 → 树铺满可视区，最后一行紧贴分隔线
        (root / f"f{i:02d}.txt").write_text("x")

    async def scenario():
        backend = _FakeBackend(2)
        app = TerminalApp(backend, str(root))
        clicks: list[object] = []
        original = TerminalApp.on_event

        async def spy(self, event):
            if isinstance(event, Click):
                clicks.append(event)
            await original(self, event)

        TerminalApp.on_event = spy
        try:
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause(0.8)
                sep = app.query_one("#sep-tree", Separator)
                x, y = sep.region.x + 4, sep.region.y - 1
                app.post_message(
                    MouseDown(
                        widget=None, x=float(x), y=float(y), delta_x=0, delta_y=0,
                        button=1, shift=False, meta=False, ctrl=False,
                        screen_x=float(x), screen_y=float(y),
                    )
                )
                await pilot.pause(0.3)
                assert app._drag_sep is not None, "热区没命中"
                app.post_message(
                    MouseUp(
                        widget=None, x=float(x), y=float(y), delta_x=0, delta_y=0,
                        button=1, shift=False, meta=False, ctrl=False,
                        screen_x=float(x), screen_y=float(y),
                    )
                )
                await pilot.pause(0.4)
                assert clicks == [], "拖分隔线不该产生 click"
        finally:
            TerminalApp.on_event = original

    asyncio.run(scenario())


def test_drag_rows_and_cols_dimensions(app_root):
    """左右列线、列内上下线各管各的比例维度（v0.12 列优先）。"""
    async def scenario():
        backend = _FakeBackend(4)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            # 左右列（外层竖线 sep-cols）→ cols 维度
            await _drag(app, pilot, "sep-cols", 85, 5)
            assert app._ratios["cols"] != layout_state.DEFAULT_RATIOS["cols"]
            # 列内上下（横线 sep-rows-a）→ rows 维度
            # 目标 y=12：pane 区从 y=1 起，(12-1)/(39-1) ≈ 0.29 ≠ 默认 0.5
            await _drag(app, pilot, "sep-rows-a", 10, 12)
            assert app._ratios["rows"] != layout_state.DEFAULT_RATIOS["rows"]
    asyncio.run(scenario())


def test_rows_drag_keeps_grid_aligned(app_root):
    """拖一条列内上下线 → 另一列的同一条线同步（2×2 十字对齐不能歪）。"""
    async def scenario():
        backend = _FakeBackend(4)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            await _drag(app, pilot, "sep-rows-a", 10, 12)
            assert app._ratios["rows"] != layout_state.DEFAULT_RATIOS["rows"]
            p0 = app.query_one("#pane-0").region
            p2 = app.query_one("#pane-2").region
            p1 = app.query_one("#pane-1").region
            p3 = app.query_one("#pane-3").region
            # 两列的上下分割线共用一份 rows 比例 → 上块等高、下块等高、y 相同
            assert p0.height == p2.height
            assert p1.height == p3.height
            assert p0.y == p2.y
    asyncio.run(scenario())


def test_double_click_resets_dimension(app_root):
    """双击分隔线 → 该维度比例复位默认（其它维度不动）。"""
    async def scenario():
        backend = _FakeBackend(2)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            await _drag(app, pilot, "sep-main", 60, 10)
            await _drag(app, pilot, "sep-tree", 10, 12)
            assert app._ratios["sidebar"] != layout_state.DEFAULT_RATIOS["sidebar"]
            await pilot.click("#sep-main", offset=(0, 0), times=2)
            await pilot.pause(0.3)
            assert app._ratios["sidebar"] == layout_state.DEFAULT_RATIOS["sidebar"]
            assert app._ratios["tree"] != layout_state.DEFAULT_RATIOS["tree"]
    asyncio.run(scenario())


def _column_text(app, x: int, y0: int = 0, y1: int | None = None) -> str:
    """取屏幕上第 x 列、y0..y1 行的字符（验证分隔线真的画成了粗/细线）。"""
    strips = app.screen._compositor.render_strips()
    lines = []
    for y, strip in enumerate(strips):
        if y < y0 or (y1 is not None and y >= y1):
            continue
        text = "".join(seg.text for seg in strip)
        lines.append(text[x] if x < len(text) else " ")
    return "\n".join(lines)


def _row_text(app, y: int, x0: int = 0, x1: int | None = None) -> str:
    """取屏幕上第 y 行、x0..x1 列的字符（验证**横**分隔线画成了粗/细线）。"""
    strips = app.screen._compositor.render_strips()
    if y >= len(strips):
        return ""
    text = "".join(seg.text for seg in strips[y])
    if x1 is None:
        x1 = len(text)
    return text[x0:x1]


def _cell_color(app, x: int, y: int):
    """取屏幕上 (x, y) 那一格的实际前景色（验证「粗线是绿色的」）。

    返回的是 **rich** 的 Color（渲染出来的那一格真正用的颜色）。
    """
    strips = app.screen._compositor.render_strips()
    strip = strips[y]
    cell = 0
    for seg in strip:
        width = len(seg.text)
        if cell <= x < cell + width:
            return seg.style.color
        cell += width
    return None


def test_focus_styles_on_pane_border_and_sidebar(app_root):
    """v0.11.2/v0.11.3 焦点视觉：焦点 pane **边框**加粗变绿；侧栏聚焦**按半区**变底色。

    旧实现（相邻分隔线整条变粗变绿）的绿线会溢出焦点 pane 的边界——
    ``sep-cols`` 是全高、``sep-rows-a`` 是全宽，绿线一直画到相邻 pane 旁边
    （用户实测「逻辑完全是乱的」），已整体移除；分隔线恒为细线。
    侧栏聚焦的底色 v0.11.3 起**按半区**：焦点在文件树 → 只染上半区；
    焦点在 Git 面板 → 只染下半区（标题行 + 历史面板，靠 ``#git-area``
    容器做 :focus-within 锚点——CSS 无法跨兄弟节点染色）。
    """
    async def scenario():
        backend = _FakeBackend(4)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            pane0 = app.query_one("#pane-0", PaneWidget)
            pane1 = app.query_one("#pane-1", PaneWidget)
            col_a = app.query_one("#sep-rows-a", Separator)

            def thin_at(sep) -> bool:
                """分隔线恒细：竖线看整列、横线看整行（v0.12 列内那条是横线）。"""
                if sep.orientation == "horizontal":
                    return "━" not in _row_text(
                        app, sep.region.y, sep.region.x,
                        sep.region.x + sep.region.width,
                    )
                return "┃" not in _column_text(
                    app, sep.region.x, sep.region.y,
                    sep.region.y + sep.region.height,
                )

            # 启动焦点在 pane-0 → 边框 heavy + 绿（$success = #4EBF71）
            top = pane0.styles.border.top
            assert top[0] == "heavy", top
            assert top[1].rgb == (78, 191, 113), top[1]
            # 相邻 pane 边框保持细灰
            assert pane1.styles.border.top[0] == "solid", pane1.styles.border.top
            # 屏幕上画出来的确实是粗绿边（取实际字符 + 实际前景色）
            assert "┃" in _column_text(
                app, pane0.region.x, pane0.region.y,
                pane0.region.y + pane0.region.height,
            )
            assert _cell_color(
                app, pane0.region.x, pane0.region.y + 3
            ).get_truecolor() == ColorTriplet(78, 191, 113)
            # 分隔线不参与焦点指示（恒细）
            assert thin_at(col_a)

            # 焦点切到 pane-2 → 边框跟随；**几何零位移**（切焦点不得让
            # 内容区缩放 → 不触发 tmux resize 风暴）
            cs_before = pane0.content_size
            app._set_active(2)
            await pilot.pause(0.2)
            assert pane0.styles.border.top[0] == "solid"
            assert app.query_one("#pane-2", PaneWidget).styles.border.top[0] == "heavy"
            assert pane0.content_size == cs_before

            # 侧栏聚焦 → 底色**按半区**染色（v0.11.3）：焦点在文件树 → 只染
            # 上半区；焦点在 Git 面板 → 只染下半区（含标题行）；无任何绿框/绿线
            tree = app.query_one("#file-tree")
            git_area = app.query_one("#git-area")
            bg_tree_default = tree.styles.background
            bg_git_default = git_area.styles.background

            app.action_focus_sidebar()  # 焦点 → 文件树
            await pilot.pause(0.3)
            assert tree.styles.background != bg_tree_default, "上半区应染色"
            assert git_area.styles.background == bg_git_default, "下半区不该跟着变"
            assert app.query_one("#pane-2", PaneWidget).styles.border.top[0] == "solid"
            assert thin_at(col_a)

            app.query_one("#git-log").focus()  # 焦点 → Git 面板
            await pilot.pause(0.3)
            assert tree.styles.background == bg_tree_default, "上半区应恢复"
            assert git_area.styles.background != bg_git_default, "下半区应染色"

            # 焦点回 pane → 两半区底色都恢复
            app._focus_active_pane()
            await pilot.pause(0.3)
            assert tree.styles.background == bg_tree_default
            assert git_area.styles.background == bg_git_default
            assert app.query_one("#pane-2", PaneWidget).styles.border.top[0] == "heavy"

            # #git-area 包裹不改变几何：树 + git 区 + 分隔线 = 侧栏内容高
            sidebar_region = app.query_one("#sidebar").region
            assert (
                tree.region.height + git_area.region.height + 1
                == sidebar_region.height
            )
    asyncio.run(scenario())


def test_focus_region_lines_react_to_sidebar_collapse(app_root):
    """侧栏折叠 → 侧栏那条线藏起来，焦点 pane 的框少一条边（不悬空）。"""
    async def scenario():
        backend = _FakeBackend(2)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            app.action_toggle_sidebar()
            await pilot.pause(0.2)
            assert app.query_one("#sep-main", Separator).display is False
            app.action_toggle_sidebar()
            await pilot.pause(0.2)
            assert app.query_one("#sep-main", Separator).display is True
    asyncio.run(scenario())


def test_grab_zone_ignores_separators_behind_modal(app_root):
    """弹窗打开时，热区**不**命中「弹窗背后」的主屏分隔线。

    主屏还铺在弹窗底下，坐标是重叠的——不排除的话，弹窗里的按钮会正好
    落在某条分隔线的热区上（点「Kill」变成拖分隔线 → 弹窗关不掉）。
    """
    async def scenario():
        backend = _FakeBackend(2)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            sep = app.query_one("#sep-tree", Separator)
            x, y = sep.region.x + 2, sep.region.y
            # 没有弹窗时 → 命中那条分隔线
            assert app._separator_near(x, y) is sep
            # 弹窗打开 → 主屏的分隔线全部不算数
            app.push_screen(_ProbeModal())
            await pilot.pause(0.3)
            assert app._separator_near(x, y) is None
    asyncio.run(scenario())


def test_layout_persisted_and_restored(tmp_path, app_root):
    """拖完落盘 → 同项目重新 attach 时按上次的比例起（v0.11 的核心诉求）。"""
    path = str(tmp_path / "layout.json")

    async def scenario():
        backend = _FakeBackend(2)
        app = TerminalApp(backend, app_root, layout_path=path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            await _drag(app, pilot, "sep-main", 60, 10)
            saved = app._ratios["sidebar"]
            assert saved != layout_state.DEFAULT_RATIOS["sidebar"]
        # 落盘了
        assert os.path.exists(path)
        assert layout_state.load_ratios(path)["sidebar"] == saved

        # 重新 attach（同一项目 → 同一 layout 文件）→ 比例恢复
        backend2 = _FakeBackend(2)
        app2 = TerminalApp(backend2, app_root, layout_path=path)
        async with app2.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            assert app2._ratios["sidebar"] == saved
            width = app2.query_one("#sidebar").region.width
            assert width == int(saved * (120 - 1))
    asyncio.run(scenario())


def test_separator_drag_does_not_steal_focus(app_root):
    """点/拖分隔线不抢焦点（否则 pane 的键盘透传会断）。"""
    async def scenario():
        backend = _FakeBackend(2)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            app._set_active(1)
            await pilot.pause(0.2)
            focused_before = app.focused
            await _drag(app, pilot, "sep-main", 60, 10)
            assert app.focused is focused_before
    asyncio.run(scenario())
