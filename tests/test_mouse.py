"""myrtui v0.7 鼠标转发测试：SGR/xterm 旧编码 合成 / 门控 / 注入 / App 路由（§10.2）。

分层：
- 纯函数：decode_level 实测矩阵、can_forward 门控、SGR 与旧编码字节级断言；
- 后端：send_mouse 经真实 tmux 注入 pty（cat -v 回显）、list-panes 鼠标 flag；
- App 级（headless + 真实 tmux）：点击/滚轮/hover 的门控与路由，含跨 pane
  按下-拖动-释放的配对完整性（恒路由回按下发生地）；
- 编码维度（v0.7.1 审阅 F3）：sgr=0 → xterm 旧 3 字节（vim 默认形态），
  vim 端到端落点验证。

门控不手工赋值——一律用 ``printf '\\033[?<mode>[;1006h]'`` 让 tmux 真实
置位（与实测矩阵同源），防止 poll 下一轮刷新覆盖测试预期。
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import time
import uuid

import pytest
from textual.events import MouseMove, MouseScrollDown, MouseScrollUp

from myrtui.client.app import TerminalApp
from myrtui.client.pane_widget import PaneWidget
from myrtui.mouse import (
    LEVEL_ALL,
    LEVEL_CLICK,
    LEVEL_DRAG,
    LEVEL_NONE,
    can_forward,
    decode_level,
    sgr_motion,
    sgr_press,
    sgr_release,
    sgr_wheel,
    x10_motion,
    x10_press,
    x10_release,
    x10_wheel,
)
from myrtui.tmux_backend import TmuxBackend


# ---------- 纯函数：门控矩阵（与实测 tmux 3.8 行为同源） ----------

@pytest.mark.parametrize(
    "std,btn,any_,all_,expected",
    [
        (False, False, False, False, LEVEL_NONE),  # 无请求
        (True, False, True, False, LEVEL_CLICK),   # 1000（any 聚合位亮）
        (False, True, True, False, LEVEL_DRAG),    # 1002
        # 1003 实测形态：all 直读（v0.7.1 小项，替代 any 排除法）
        (False, False, True, True, LEVEL_ALL),
        # 1000+1006+1003 组合实测：1003 覆盖 std/btn，sgr 正交（sgr 不进门控）
        (False, False, True, True, LEVEL_ALL),
        # 异常/未观测态
        (True, True, False, False, LEVEL_DRAG),    # 模式异常组合 → 取次宽
        (True, False, False, False, LEVEL_CLICK),  # any 不亮但 std 亮 → CLICK
        (False, False, True, False, LEVEL_CLICK),  # 仅聚合位亮（如 1005 扩展）
        # → 保守 CLICK（少转发优于错转发）
    ],
)
def test_decode_level_matrix(std, btn, any_, all_, expected):
    assert decode_level(std, btn, any_, all_) == expected


def test_decode_level_backcompat_default_all():
    """三参调用（all_ 缺省 False）——1003 须靠第 4 参显式给出才判 ALL。"""
    assert decode_level(False, False, True) == LEVEL_CLICK
    assert decode_level(False, False, True, True) == LEVEL_ALL


@pytest.mark.parametrize(
    "level,kind,allowed",
    [
        (LEVEL_NONE, "press", False),
        (LEVEL_NONE, "wheel", False),
        (LEVEL_CLICK, "press", True),
        (LEVEL_CLICK, "wheel", True),
        (LEVEL_CLICK, "drag", False),   # 1000 不含拖动
        (LEVEL_CLICK, "motion", False),  # 1000 不含 hover
        (LEVEL_DRAG, "drag", True),
        (LEVEL_DRAG, "motion", False),   # 1002 不含纯 hover
        (LEVEL_ALL, "motion", True),
        (LEVEL_ALL, "press", True),
    ],
)
def test_can_forward_matrix(level, kind, allowed):
    assert can_forward(level, kind) is allowed


# ---------- 纯函数：SGR(1006) 序列（字节级） ----------

def test_sgr_press_release_buttons_and_coords():
    # 坐标 1-based：(5,2) → ;6;3；Textual button 1/2/3 → SGR 0/1/2
    assert sgr_press(5, 2, 1) == "\x1b[<0;6;3M"
    assert sgr_press(0, 0, 2) == "\x1b[<1;1;1M"
    assert sgr_press(0, 0, 3) == "\x1b[<2;1;1M"
    # release 用小写 m 区分
    assert sgr_release(5, 2, 1) == "\x1b[<0;6;3m"


def test_sgr_modifiers():
    # shift=4 meta=8 ctrl=16（SGR 修饰位）
    assert sgr_press(0, 0, 1, shift=True) == "\x1b[<4;1;1M"
    assert sgr_press(0, 0, 1, meta=True) == "\x1b[<8;1;1M"
    assert sgr_press(0, 0, 1, ctrl=True) == "\x1b[<16;1;1M"
    assert sgr_release(0, 0, 1, shift=True, ctrl=True) == "\x1b[<20;1;1m"


def test_sgr_wheel_and_motion():
    assert sgr_wheel(True, 9, 9) == "\x1b[<64;10;10M"   # 上滚 64
    assert sgr_wheel(False, 0, 0) == "\x1b[<65;1;1M"    # 下滚 65
    assert sgr_motion(0, 0, 0) == "\x1b[<35;1;1M"       # hover（32|3）
    assert sgr_motion(1, 1, 1) == "\x1b[<32;2;2M"       # 左键拖动（32|0）
    assert sgr_motion(0, 0, 0, shift=True) == "\x1b[<39;1;1M"  # hover+shift


# ---------- 旧编码（v0.7.1 审阅 F3；字节 = tmux 翻译 oracle 实证值） ----------

def test_x10_press_release_bytes():
    # tmux oracle：外层发 SGR ;5;5M（1-based）→ tmux 翻给旧编码内层 = byte2/3 37('%')
    # 即 33 + 0based(4)。下面全部以 oracle 实测值断言。
    assert x10_press(4, 4, 1) == "\x1b[M" + chr(32) + chr(37) + chr(37)  # 左键 byte1=32
    assert x10_press(0, 0, 2) == "\x1b[M" + chr(33) + chr(33) + chr(33)  # 中键
    assert x10_press(0, 0, 3) == "\x1b[M" + chr(34) + chr(33) + chr(33)  # 右键
    # release：b=3 → byte1=35('#')（oracle 实证），SGR 的「button + 小写 m」不存在于旧编码
    assert x10_release(4, 4, 1) == "\x1b[M" + chr(35) + chr(37) + chr(37)
    # 修饰：shift=+4 → byte1=36('$')（oracle 实证）
    assert x10_press(4, 4, 1, shift=True) == "\x1b[M" + chr(36) + chr(37) + chr(37)
    assert x10_release(0, 0, 1, ctrl=True) == "\x1b[M" + chr(35 + 16) + chr(33) * 2


def test_x10_wheel_and_motion_bytes():
    # wheel：byte1=96/97（oracle 实证 chr(96)='`'、chr(97)='a'）
    assert x10_wheel(True, 4, 4) == "\x1b[M" + chr(96) + chr(37) + chr(37)
    assert x10_wheel(False, 4, 4) == "\x1b[M" + chr(97) + chr(37) + chr(37)
    # motion：32+32+button（oracle 实证 drag byte1=64('@')、hover=67）
    assert x10_motion(9, 7, 1) == "\x1b[M" + chr(64) + chr(42) + chr(40)
    assert x10_motion(0, 0, 0) == "\x1b[M" + chr(67) + chr(33) + chr(33)


def test_x10_coord_clamp():
    """旧编码坐标单字节上限 223（0-based ≤222 → byte ≤255），超宽不回退 SGR。"""
    from myrtui.mouse import X10_MAX_COORD
    seq = x10_press(999, 999, 1)
    assert seq == "\x1b[M" + chr(32) + chr(33 + X10_MAX_COORD) * 2
    assert all(ord(ch) <= 255 for ch in seq), "字符必须落在 latin-1 单字节内"
    # 负坐标同样 clamp 到 0（防御）
    assert x10_press(-5, -5, 1) == "\x1b[M" + chr(32) + chr(33) * 2


def test_dispatch_picks_encoding():
    """dispatch 按 sgr 位选编码（F3 核心：同一事件两种字节形态）。"""
    from myrtui.mouse import motion as d_motion, press as d_press, release as d_release
    from myrtui.mouse import wheel as d_wheel

    assert d_press(4, 4, 1, True) == sgr_press(4, 4, 1)
    assert d_press(4, 4, 1, False) == x10_press(4, 4, 1)
    assert d_release(4, 4, 1, False) == x10_release(4, 4, 1)
    assert d_wheel(True, 4, 4, False) == x10_wheel(True, 4, 4)
    assert d_motion(0, 0, 0, False) == x10_motion(0, 0, 0)
    assert d_press(4, 4, 1, True) != d_press(4, 4, 1, False)


# ---------- 后端：真实 tmux 注入 ----------

def _capture(backend: TmuxBackend, pid: str) -> str:
    info = next(p for p in backend.list_panes() if p.pane_id == pid)
    _, caps = backend.poll({pid: (0, info.height)})
    return caps[pid].text


def _wait_ready(backend: TmuxBackend, pid: str, timeout: float = 10.0) -> None:
    """等 pane 内 shell 真正可执行命令（打字就绪握手，不靠固定 sleep）。

    ``create_session`` 后立刻 ``send_text`` 时，zsh 冷启动可能吞掉输入
    （tests/README「并行下已知的易碎点」）。这里反复发一条幂等的
    ``printf`` 标记并轮询 capture，看到标记即认为 shell 已就绪。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        backend.send_text(pid, "printf __MYRTUI_READY__")
        backend.send_keys(pid, "Enter")
        for _ in range(10):
            time.sleep(0.1)
            if "__MYRTUI_READY__" in _capture(backend, pid):
                return
    raise AssertionError("pane 内 shell 未在 10s 内就绪")


@pytest.fixture
def backend():
    name = f"myrtui-mouse-{uuid.uuid4().hex[:8]}"
    b = TmuxBackend(name, name)
    root = tempfile.mkdtemp(prefix="myrtui-proj-")
    b.create_session(root, 80, 24)
    _wait_ready(b, b.list_panes()[0].pane_id)
    yield b
    b.kill_server()


def test_send_mouse_reaches_pty(backend):
    """send_keys -H 逐字节注入 → pane 内程序能收到原始 SGR 字节（cat -v 回显）。"""
    pid = backend.list_panes()[0].pane_id
    backend.send_text(pid, "cat -v")
    backend.send_keys(pid, "Enter")
    time.sleep(0.5)
    backend.send_mouse(pid, sgr_press(5, 2, 1))
    time.sleep(0.5)
    assert "^[[<0;6;3M" in _capture(backend, pid)


def test_list_panes_mouse_flags_and_decode(backend):
    """list-panes 的 mouse_*_flag → decode_level 与实测矩阵一致。"""
    info = backend.list_panes()[0]
    assert (info.mouse_standard, info.mouse_button, info.mouse_any) == (
        False, False, False
    )
    assert decode_level(
        info.mouse_standard, info.mouse_button, info.mouse_any
    ) == LEVEL_NONE
    # 程序请求 1000 → std=1（实测矩阵）
    # 轮询等 flag 置位，并在没置位时**重发**：zsh 冷启动偶尔会吞掉首条输入
    # （全量套件并行负载下尤其明显），固定 sleep / 单次发送都会假阴性。
    deadline = time.time() + 15.0
    info = backend.list_panes()[0]
    while time.time() < deadline:
        backend.send_text(info.pane_id, r"printf '\033[?1000;1006h'")
        backend.send_keys(info.pane_id, "Enter")
        for _ in range(20):
            time.sleep(0.1)
            info = backend.list_panes()[0]
            if info.mouse_standard:
                break
        if info.mouse_standard:
            break
    assert info.mouse_standard is True, _capture(backend, info.pane_id)
    assert decode_level(
        info.mouse_standard, info.mouse_button, info.mouse_any
    ) == LEVEL_CLICK


# ---------- App 级（headless + 真实 tmux） ----------

_MODE_FLAG = {
    "1000": "mouse_standard",
    "1002": "mouse_button",
    "1003": "mouse_any",
}


async def _request_mouse(backend: TmuxBackend, pid: str, mode: str,
                         sgr: bool = True) -> None:
    """让 tmux 真实置位鼠标 flag（mode: '1000'/'1002'/'1003'）。

    ``sgr=False`` → 不发 1006，编码位 sgr=0（审阅 F3 的 vim 场景）。
    发送后轮询 ``list-panes`` 到目标 flag 真的置位才返回——固定 sleep 在
    全量套件负载下会假阴性（shell 冷启动慢于 sleep 时 printf 还没执行）。
    """
    req = rf"\033[?{mode}" + (";1006h" if sgr else "h")
    flag = _MODE_FLAG[mode]
    deadline = time.time() + 15.0
    info = None
    while time.time() < deadline:
        backend.send_text(pid, f"printf '{req}'; cat -v")
        backend.send_keys(pid, "Enter")
        for _ in range(20):
            await asyncio.sleep(0.1)
            info = next(
                (p for p in backend.list_panes() if p.pane_id == pid), None
            )
            if info is not None and getattr(info, flag):
                return
    raise AssertionError(
        f"pane {pid} 未在 15s 内请求鼠标模式 {mode}："
        f"{_capture(backend, pid)[-200:] if info is not None else '无 pane'}"
    )


async def _wait_level(app: TerminalApp, pid: str, level: int) -> None:
    """等待 poll 把门控刷成期望级别（程序 printf 后 flag 生效需要一轮 poll）。"""
    for _ in range(30):
        if app._mouse_levels.get(pid) == level:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"level 未刷新为 {level}: {app._mouse_levels.get(pid)}")


async def _wait_min_level(app: TerminalApp, pid: str, min_level: int) -> None:
    """等待门控达到至少 min_level（vim 可能请求 1000/1002/1003 任一）。"""
    for _ in range(30):
        if app._mouse_levels.get(pid, LEVEL_NONE) >= min_level:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"level 未达到 {min_level}: {app._mouse_levels.get(pid)}"
    )


async def _wait_sgr(app: TerminalApp, pid: str, expected: bool) -> None:
    """等待编码位刷新为期望值（sgr=False = xterm 旧编码路径）。"""
    for _ in range(30):
        if pid in app._mouse_sgr and app._mouse_sgr[pid] == expected:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"sgr 未刷新为 {expected}: {app._mouse_sgr.get(pid)}"
    )


def _spy_mouse(backend: TmuxBackend) -> list:
    """passthrough spy：记录 (pane_id, seq) 后**继续真实注入**。

    纯拦截版会把注入吞掉——断言 sent 能过，但 cat -v 回显/vim 落点全假
    （F3 测试首次失败即此因）。backend 为每测试独占，无需还原。
    """
    sent: list = []
    orig = backend.send_mouse

    def spy(pid: str, seq: str) -> None:
        sent.append((pid, seq))
        orig(pid, seq)

    backend.send_mouse = spy  # type: ignore[method-assign]
    return sent


def _od_bytes(text: str) -> list[bytes]:
    """从 ``od -An -tx1`` 的输出里抽出字节序列。

    od 的字节分隔宽度随实现而异：BSD od（macOS）用双空格、GNU od
    （Linux）用单空格——按原始文本断言字面量会绑死平台。故只取「整行
    都是两位十六进制」的候选行，逐行解成字节再比。
    """
    rows = []
    for line in text.splitlines():
        toks = line.split()
        if toks and all(re.fullmatch(r"[0-9a-f]{2}", t) for t in toks):
            rows.append(bytes(int(t, 16) for t in toks))
    return rows


def _md(widget: PaneWidget, x: int, y: int, button: int = 1) -> object:
    """同 widget 按下事件：(x, y) = **内容区坐标**（v0.11.2 起 pane 有 1 cell
    永久边框，转发坐标以内容区为原点，tmux 窗口也按内容区尺寸联动）。"""
    from textual.events import MouseDown

    return MouseDown(
        widget=widget, x=float(x), y=float(y), delta_x=0, delta_y=0,
        button=button, shift=False, meta=False, ctrl=False,
        screen_x=float(widget.content_region.x + x),
        screen_y=float(widget.content_region.y + y),
    )


def _mu(widget: PaneWidget, x: int, y: int, button: int = 1) -> object:
    from textual.events import MouseUp

    return MouseUp(
        widget=widget, x=float(x), y=float(y), delta_x=0, delta_y=0,
        button=button, shift=False, meta=False, ctrl=False,
        screen_x=float(widget.content_region.x + x),
        screen_y=float(widget.content_region.y + y),
    )


def _mm(widget: PaneWidget, x: int, y: int, button: int = 0) -> MouseMove:
    """同 widget 移动事件：(x, y) = **内容区坐标**（见 _md 注释）。"""
    return MouseMove(
        widget=widget, x=float(x), y=float(y), delta_x=1, delta_y=0,
        button=button, shift=False, meta=False, ctrl=False,
        screen_x=float(widget.content_region.x + x),
        screen_y=float(widget.content_region.y + y),
    )


def test_click_forwarded_full_chain(backend, app_root):
    """端到端：printf 1000 → poll 刷新门控 → 点击 → cat -v 回显 press+release。"""
    async def scenario():
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            pid = app._pane_ids[0]
            await _request_mouse(backend, pid, "1000")
            await _wait_level(app, pid, LEVEL_CLICK)
            # offset 是**外框**坐标；内容 = 外框 −1（边框），(6,3) → 内容 (5,2)
            await pilot.click("#pane-0", offset=(6, 3))
            await pilot.pause(0.5)
            text = _capture(backend, pid)
            assert "^[[<0;6;3M" in text, f"press 未转发: {text!r}"
            assert "^[[<0;6;3m" in text, f"release 未转发: {text!r}"
    asyncio.run(scenario())


def test_click_not_forwarded_without_mouse(backend, app_root):
    """程序未请求鼠标 → 点击不注入任何 SGR 字节（只切焦点）。"""
    async def scenario():
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            sent = _spy_mouse(backend)
            await pilot.click("#pane-0", offset=(5, 2))
            await pilot.pause(0.5)
            assert sent == []
            # 按下状态未卡死：释放后复位
            assert app._mouse_down_pane is None
    asyncio.run(scenario())


def test_wheel_forwarded_and_shift_escape(backend, app_root):
    """滚轮：1000 程序 → 转发 64；shift+滚轮 = 逃生舱 → 永远翻 scrollback。"""
    async def scenario():
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            pid = app._pane_ids[0]
            await _request_mouse(backend, pid, "1000")
            await _wait_level(app, pid, LEVEL_CLICK)
            pw = app.query_one("#pane-0", PaneWidget)

            # 1) shift+滚轮 → 不转发（先做，避免与转发结果混淆）
            shift_up = MouseScrollUp(
                widget=pw, x=5.0, y=2.0, delta_x=0, delta_y=-1, button=0,
                shift=True, meta=False, ctrl=False,
                screen_x=float(pw.content_region.x + 5),
                screen_y=float(pw.content_region.y + 2),
            )
            pw.on_mouse_scroll_up(shift_up)
            await pilot.pause(0.4)
            assert "^[[<64;" not in _capture(backend, pid), "shift+滚轮不得转发"

            # 2) 普通滚轮 → 转发 64（绝对 screen 坐标，与真实分发一致）
            up = MouseScrollUp(
                widget=pw, x=5.0, y=2.0, delta_x=0, delta_y=-1, button=0,
                shift=False, meta=False, ctrl=False,
                screen_x=float(pw.content_region.x + 5),
                screen_y=float(pw.content_region.y + 2),
            )
            pw.on_mouse_scroll_up(up)
            await pilot.pause(0.4)
            assert "^[[<64;6;3M" in _capture(backend, pid), "滚轮未转发"
    asyncio.run(scenario())


def test_wheel_scrollback_without_mouse(backend, app_root):
    """程序未请求鼠标 → 滚轮保持 scrollback 行为（P0 §11 口径）。"""
    async def scenario():
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            pid = app._pane_ids[0]
            sent = _spy_mouse(backend)
            # 造历史：必须让**真实 tmux 缓冲区**滚出历史行，不能只改
            # app._history_sizes（那只是「应用以为有多少历史」）。
            # 原因：滚轮步进 SCROLL_STEP=3 → capture 要 -S -3；真实历史不足
            # 3 行时，行数断言（M3/M18）会按设计逐级降级（k → k//2 → 0），
            # 偏移最终归 0。而初始历史有几行取决于 pane 里那个 shell 的提示符
            # 占几行（本机 zsh 两行提示符 → 1 行；CI runner 无 $SHELL →
            # /bin/sh 单行提示符 → 0 行）——环境依赖，故此处自己造。
            backend.send_text(pid, "seq 1 60")
            backend.send_keys(pid, "Enter")
            await pilot.pause(1.0)
            app._history_sizes[pid] = 10
            pw = app.query_one("#pane-0", PaneWidget)
            up = MouseScrollUp(
                widget=pw, x=5.0, y=2.0, delta_x=0, delta_y=-1, button=0,
                shift=False, meta=False, ctrl=False,
            )
            pw.on_mouse_scroll_up(up)
            await pilot.pause(0.3)
            assert sent == [], "无鼠标程序不得收到滚轮序列"
            assert app._offsets[pid] > 0, "应走 scrollback（偏移增长）"
    asyncio.run(scenario())


def test_hover_gated_by_level(backend, app_root):
    """hover 门控：1000（CLICK）不转发 motion；1003（ALL）转发 35。"""
    async def scenario():
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            pid0 = app._pane_ids[0]
            app.action_new_pane()
            await pilot.pause(0.6)
            pid0, pid1 = app._pane_ids[0], app._pane_ids[1]
            await _request_mouse(backend, pid0, "1000")
            await _request_mouse(backend, pid1, "1003")
            await _wait_level(app, pid0, LEVEL_CLICK)
            await _wait_level(app, pid1, LEVEL_ALL)
            sent = _spy_mouse(backend)
            pw0 = app.query_one("#pane-0", PaneWidget)
            pw1 = app.query_one("#pane-1", PaneWidget)

            pw0.on_mouse_move(_mm(pw0, 3, 3, button=0))
            assert sent == [], "CLICK 级别不得转发 hover"

            pw1.on_mouse_move(_mm(pw1, 3, 3, button=0))
            # B1：send_mouse 提交进串行执行器（fire-and-forget），等一个回投
            await pilot.pause(0.1)
            assert len(sent) == 1 and sent[0][0] == pid1
            assert sent[0][1] == sgr_motion(3, 3, 0)
    asyncio.run(scenario())


def test_motion_tail_coalescing(backend, app_root):
    """motion 节流：30ms 内多次移动只发第一条 + pending 合并为最后一条。"""
    async def scenario():
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            pid = app._pane_ids[0]
            await _request_mouse(backend, pid, "1003")
            await _wait_level(app, pid, LEVEL_ALL)
            sent = _spy_mouse(backend)
            pw = app.query_one("#pane-0", PaneWidget)
            # 冻结 tick：pause 等回投期间 _flush_motion 不得提前清 pending
            app._tick_timer.pause()

            # 同一事件循环切片内连发 5 条（tick 不会插针）
            for i in range(5):
                pw.on_mouse_move(_mm(pw, i, 0, button=0))
            # B1：首条 send_mouse 提交进串行执行器，等一个回投再查 spy
            await pilot.pause(0.1)
            assert len(sent) == 1, "首条立即发，其余应进 pending"
            assert app._pending_motion is not None
            # pending 必须是最后一条（尾部合并）
            _, pending_seq = app._pending_motion
            assert pending_seq == sgr_motion(4, 0, 0)

            app._flush_motion(time.monotonic() + 1.0)
            await pilot.pause(0.1)  # B1：flush 的发送同样等一个回投
            assert len(sent) == 2
            assert app._pending_motion is None
            assert sent[1][1] == sgr_motion(4, 0, 0)
    asyncio.run(scenario())


def test_press_drag_release_stay_on_press_pane(backend, app_root, monkeypatch):
    """配对完整性：按下在 pane A → 跨 pane 拖动/释放全部路由回 A（坐标 clamp）。

    v0.35 默认 ``always`` 会把左键拖拽收归本地选字，这里测的是**旧转发路径**
    （``MYRTUI_DRAG_SELECT=app`` 的应急模式 / 程序抢鼠标时的 v0.34 行为）。
    """
    monkeypatch.setenv("MYRTUI_DRAG_SELECT", "app")

    async def scenario():
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            app.action_new_pane()
            await pilot.pause(0.6)
            pid_a, pid_b = app._pane_ids[0], app._pane_ids[1]
            await _request_mouse(backend, pid_a, "1002")  # DRAG 级别
            await _wait_level(app, pid_a, LEVEL_DRAG)
            sent = _spy_mouse(backend)
            pw_a = app.query_one("#pane-0", PaneWidget)
            pw_b = app.query_one("#pane-1", PaneWidget)

            # press → A（真实 pilot 事件路径；offset 外框 (6,3) → 内容 (5,2)）
            ok = await pilot.mouse_down("#pane-0", offset=(6, 3))
            assert ok
            await pilot.pause(0.3)
            assert len(sent) == 1 and sent[0][0] == pid_a
            assert sent[0][1] == sgr_press(5, 2, 1)

            # 拖动事件落在 B 上 → 仍路由回 A，坐标 clamp 进 A 的内容区
            # （v0.12 起 B 在 A **下面**，越界的是 y；两个轴都按实际区域算，
            #  断言不写死方向）
            pw_b.on_mouse_move(_mm(pw_b, 3, 3, button=1))
            await pilot.pause(0.1)  # B1：send_mouse 提交进执行器，等一个回投
            assert len(sent) == 2 and sent[0][0] == pid_a
            assert sent[1][0] == pid_a, "拖动必须路由回按下 pane"
            clamped_x = min(
                max(pw_b.content_region.x + 3 - pw_a.content_region.x, 0),
                pw_a.content_region.width - 1,
            )
            clamped_y = min(
                max(pw_b.content_region.y + 3 - pw_a.content_region.y, 0),
                pw_a.content_region.height - 1,
            )
            assert sent[1][1] == sgr_motion(clamped_x, clamped_y, 1)

            # release 落在 B 上 → 仍路由回 A
            # （pilot.mouse_up 会先发一条 MouseMove 再发 MouseUp → 中间可能
            # 多一条同位置拖动 motion，属正常；断言放宽为「全部回 A + 末条 release」）
            ok = await pilot.mouse_up("#pane-1", offset=(3, 3))
            assert ok
            await pilot.pause(0.3)
            assert len(sent) >= 3
            assert all(pid == pid_a for pid, _ in sent), "任何事件都不得路由到 B"
            last_seq = sent[-1][1]
            assert last_seq.endswith("m") and not last_seq.endswith("M"), last_seq
            assert app._mouse_down_pane is None, "释放后按下状态必须复位"
    asyncio.run(scenario())


def test_old_encoding_drag_app_level(backend, app_root, monkeypatch):
    """复审 §5-D：旧编码（sgr=0）的 motion App 级覆盖——1002 拖动走
    `x10_motion`（byte1=32+32+btn=64），补齐 §15.2-3 门控矩阵旧编码分支
    （此前 motion 仅纯函数覆盖）。v0.35：走 ``MYRTUI_DRAG_SELECT=app`` 的
    旧转发路径。"""
    monkeypatch.setenv("MYRTUI_DRAG_SELECT", "app")

    async def scenario():
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            pid = app._pane_ids[0]
            await _request_mouse(backend, pid, "1002", sgr=False)
            await _wait_level(app, pid, LEVEL_DRAG)
            await _wait_sgr(app, pid, False)
            sent = _spy_mouse(backend)
            pw = app.query_one("#pane-0", PaneWidget)

            # press → 旧编码 press（byte1=32+button；外框 (6,3) → 内容 (5,2)）
            ok = await pilot.mouse_down("#pane-0", offset=(6, 3))
            assert ok
            await pilot.pause(0.3)
            assert sent[0][1] == x10_press(5, 2, 1), sent[0][1]

            # 拖动 → 旧编码 motion（byte1 = 32|32|0 = 64），门控 DRAG 放行
            n_before = len(sent)
            pw.on_mouse_move(_mm(pw, 6, 4, button=1))
            await pilot.pause(0.1)  # B1：send_mouse 提交进执行器，等一个回投
            assert len(sent) == n_before + 1, "1002+sgr=0 的拖动 motion 应被转发"
            motion_seq = sent[-1][1]
            assert motion_seq == x10_motion(6, 4, 1), motion_seq
            assert motion_seq.startswith("\x1b[M"), "应为旧编码"
            assert "\x1b[<" not in motion_seq, "sgr=0 不得混入 SGR"

            # hover（无按钮）→ 1002 门控不放行（hover 需 1003）
            n_before = len(sent)
            pw.on_mouse_move(_mm(pw, 7, 5, button=0))
            assert len(sent) == n_before, "1002 不得转发 hover motion"

            # release → 旧编码 release（byte1=35；外框 (7,5) → 内容 (6,4)）
            ok = await pilot.mouse_up("#pane-0", offset=(7, 5))
            assert ok
            await pilot.pause(0.3)
            assert sent[-1][1] == x10_release(6, 4, 1), sent[-1][1]
            assert app._mouse_down_pane is None
    asyncio.run(scenario())


def test_release_outside_panes_routes_to_press_pane(backend, app_root, monkeypatch):
    """释放落在侧栏（App.on_mouse_up 兜底）→ 仍路由回按下 pane。

    v0.35：这是旧转发路径（``MYRTUI_DRAG_SELECT=app``）；默认 ``always`` 下
    左键释放走手势（点击合成 / 拖拽复制），不再转发。
    """
    monkeypatch.setenv("MYRTUI_DRAG_SELECT", "app")

    async def scenario():
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            pid = app._pane_ids[0]
            await _request_mouse(backend, pid, "1000")
            await _wait_level(app, pid, LEVEL_CLICK)
            sent = _spy_mouse(backend)
            assert await pilot.mouse_down("#pane-0", offset=(5, 2))
            await pilot.pause(0.3)
            assert len(sent) == 1 and sent[0][0] == pid

            # 释放 → 侧栏（无 handler → 冒泡到 App.on_mouse_up）
            # P3：侧栏是**容器**（文件树 + 历史），事件落在它的子控件上
            # （#file-tree），不再落在 #sidebar 本身
            # offset y=2：外框 y=3 → pane 内容区 y = 3 - content_region.y = 1
            assert await pilot.mouse_up("#file-tree", offset=(1, 2))
            await pilot.pause(0.3)
            assert len(sent) == 2 and sent[1][0] == pid, "侧栏释放应路由回按下 pane"
            # 侧栏 offset(1,2) → screen y=3；内容区 y = 3 - content_region.y = 1
            # （v0.11.2：原点是 pane 内容区）；x 在 pane 内容区左侧 → clamp 0
            assert sent[1][1] == sgr_release(0, 1, 1), sent[1][1]
            assert app._mouse_down_pane is None
    asyncio.run(scenario())


def test_press_outside_pane_no_orphan_release(backend, app_root):
    """按下发生在 pane 外（侧栏）→ 后续在 pane 上的释放不得注入孤儿序列。"""
    async def scenario():
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            pid = app._pane_ids[0]
            await _request_mouse(backend, pid, "1000")
            await _wait_level(app, pid, LEVEL_CLICK)
            sent = _spy_mouse(backend)
            # P3：侧栏是容器 → 按下落在子控件 #file-tree 上
            assert await pilot.mouse_down("#file-tree", offset=(1, 1))
            await pilot.pause(0.3)
            assert await pilot.mouse_up("#pane-0", offset=(5, 2))
            await pilot.pause(0.3)
            assert sent == [], "pane 外按下 → 不得产生孤儿释放"
    asyncio.run(scenario())


# ---------- 编码维度（v0.7.1 审阅 F3：sgr=0 必须发旧编码） ----------

def test_click_uses_old_encoding_without_1006(backend, app_root):
    """F3 回归：请求 1000 但无 1006（sgr=0）→ 注入 xterm 旧 3 字节，绝无 SGR。

    这正是审阅实证的 vim 默认形态——恒发 SGR 会让 vim 把 x 当行号、落点错行。
    期望字节（tmux oracle 实证）：press = ESC[M + chr(32+0) + chr(33+5) +
    chr(33+2)；release = ESC[M + chr(35) + 同坐标。cat -v 渲染 ESC 为 ^[。
    """
    async def scenario():
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            pid = app._pane_ids[0]
            await _request_mouse(backend, pid, "1000", sgr=False)
            await _wait_level(app, pid, LEVEL_CLICK)
            await _wait_sgr(app, pid, False)  # 编码位必须被 poll 捕获
            sent = _spy_mouse(backend)
            # 外框 (6,3) → 内容 (5,2)（pane 边框 1 cell，v0.11.2）
            await pilot.click("#pane-0", offset=(6, 3))
            await pilot.pause(0.5)

            assert sent, "点击未被转发"
            for _, seq in sent:
                assert seq.startswith("\x1b[M"), f"应为旧编码: {seq!r}"
                assert "\x1b[<" not in seq, f"混入 SGR: {seq!r}"
            # 坐标字节级：press byte1=32、release byte1=35，(5,2) → 38('&') 35('#')
            press_seq = "\x1b[M" + chr(32) + chr(38) + chr(35)
            release_seq = "\x1b[M" + chr(35) + chr(38) + chr(35)
            # v0.35：点击在松手时合成，press+release 拼成一串一次注入
            assert sent[0][1] == press_seq + release_seq, sent[0][1]
            assert len(sent) == 1, sent
            # cat -v 回显同样确认（注入真实到达 pty）
            text = _capture(backend, pid)
            assert "^[[M &#" in text, f"旧编码 press 未回显: {text!r}"
            assert "^[[<" not in text, "sgr=0 的 pane 不得出现 SGR 字节"
    asyncio.run(scenario())


def test_high_byte_old_encoding_latin1(backend, app_root):
    """旧编码坐标 >0x7f（x ≥ 95）→ latin-1 单字节注入；UTF-8 会打碎三字节帧。

    （审阅未覆盖的必现 bug：`encode('utf-8')` 把 chr(133) 打成 0xC2 0x85 两
    字节，帧头偏移全错、宽 pane 必现。）
    验证法：pane 内 `stty raw` + `od -N 6` 按字节读——单字节修复后输出
    `1b 5b 4d 20 85 23`；UTF-8 bug 版前 6 字节是 `... c2 85`。
    （不走 cat -v：canonical 模式等换行、tty ECHO 又会先渲染一遍，
    两者都会污染断言——od + raw 才是字节级定性。）
    """
    async def scenario():
        app = TerminalApp(backend, app_root)
        # P3：侧栏变宽（32%，上限 48）→ pane 变窄 → 终端放宽到 180 列，
        # 保证 x=100 仍落在 pane 内（侧栏封顶 48 → pane 132 列）
        async with app.run_test(size=(180, 40)) as pilot:
            await pilot.pause(0.8)
            pid = app._pane_ids[0]
            # 不用 _request_mouse（它带 cat -v）；这里要 od 读原始字节
            backend.send_text(pid, r"printf '\033[?1000h'; "
                                   "stty raw -echo; od -An -tx1 -N 6; stty sane")
            backend.send_keys(pid, "Enter")
            await pilot.pause(1.0)
            await _wait_level(app, pid, LEVEL_CLICK)
            await _wait_sgr(app, pid, False)
            # 点击 x=100 → byte2 = 33+100 = 133 = 0x85；press 6 字节恰好喂满 od
            # （offset 是外框坐标；内容 = 外框 −1，故外框 (101,3) → 内容 (100,2)）
            await pilot.click("#pane-0", offset=(101, 3))
            await pilot.pause(0.8)
            text = _capture(backend, pid)
            # od 格式：`1b  5b  4d  20  85  23`（单字节 ✓）；
            # UTF-8 bug 版前 6 字节为 `1b  5b  4d  20  c2  85`（双字节 ✗）
            # 注意：od 的字节分隔宽度随实现而异（BSD od 双空格、GNU od 单空格），
            # 故不按原始文本断言字面量，改按字节序列断言。
            want = bytes([0x1B, 0x5B, 0x4D, 0x20, 0x85, 0x23])  # ESC [ M SP 0x85 #
            assert want in _od_bytes(text), f"期望单字节 0x85 注入: {text!r}"
            assert not any(
                b"\xc2\x85" in row for row in _od_bytes(text)
            ), "UTF-8 把单字节打成了 c2 85 双字节"
    asyncio.run(scenario())


@pytest.mark.skipif(shutil.which("vim") is None, reason="vim 未安装")
def test_vim_mouse_click_and_wheel(backend, tmp_path):
    """F3 靶点（审阅 §2）+ 滚轮（用户真机报的症状）：vim `:set mouse=a`（本机
    实测 sgr=0 旧编码）下，点击第 N 行 → 光标落第 N 行；滚轮 → 视图真滚动。

    重要前提（§15.2-7 口径）：**vim 默认配置 `mouse=` 空、不请求鼠标**——
    此时门控 NONE 不注入，真实 tmux 同样不转发（对照实验实证），须先
    `:set mouse=a` 才进入本测试场景。

    断言两层：① 注入编码与 vim 请求的编码位一致；② 行为正确（cursor_y /
    首行变化）。②是跨环境稳的——即使某环境 vim 默认 sgr=1，正确性依然成立。
    """
    proj = str(tmp_path / "vproj")
    os.makedirs(proj)
    line_file = str(tmp_path / "lines.txt")
    with open(line_file, "w") as f:
        f.write("".join(f"L{i}\n" for i in range(1, 61)))

    async def scenario():
        app = TerminalApp(backend, proj)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            pid = app._pane_ids[0]
            # 起 vim（-u NONE 隔离用户 vimrc，保证行为可复现）
            backend.send_text(pid, f"vim -u NONE -N {line_file}")
            backend.send_keys(pid, "Enter")
            for _ in range(50):
                await pilot.pause(0.1)
                info = next(
                    p for p in backend.list_panes() if p.pane_id == pid
                )
                if info.command == "vim":
                    break
            assert info.command == "vim", "vim 未启动"
            # 等 vim 真正画出文件首帧再发 `:set`——`command == vim` 只代表 exec
            # 已发生，慢机上初始化/首帧可能未完成，过早发命令会丢（CI macos
            # task#179 首次实录：固定 0.8s 后 flags 仍全 False、cursor_y=8）。
            drawn = False
            for _ in range(50):  # 最多 ~5s
                await pilot.pause(0.1)
                if "L1" in _capture(backend, pid):
                    drawn = True
                    break
            assert drawn, f"vim 未画出首帧: {_capture(backend, pid)!r}"
            # 打开鼠标（审阅口径：mouse=a + 默认 ttymouse=xterm → 旧编码）
            backend.send_text(pid, ":set mouse=a")
            backend.send_keys(pid, "Enter")
            for _ in range(50):  # 最多 ~5s（固定 0.8s 在慢机上不够）
                await pilot.pause(0.1)
                info = next(p for p in backend.list_panes() if p.pane_id == pid)
                if info.mouse_standard or info.mouse_any:
                    break
            assert info.mouse_standard or info.mouse_any, (
                f"vim 未请求鼠标（5s）: cursor_y={info.cursor_y} "
                f"capture={_capture(backend, pid)!r}"
            )
            await _wait_min_level(app, pid, LEVEL_CLICK)
            await _wait_sgr(app, pid, info.mouse_sgr)

            # ① 点击 (5,10) → 第 11 行（行首 0-based row 10；外框 (6,11) → 内容 (5,10)）
            sent = _spy_mouse(backend)
            await pilot.click("#pane-0", offset=(6, 11))
            # 轮询而非固定 sleep（B1 后注入走串行执行器 + 慢机 vim 响应慢）
            for _ in range(50):  # 最多 ~5s
                await pilot.pause(0.1)
                if sent:
                    break
            assert sent, "vim 场景点击未转发"
            expect_prefix = "\x1b[<" if info.mouse_sgr else "\x1b[M"
            assert sent[0][1].startswith(expect_prefix), (
                f"编码与 vim 请求不符: sgr={info.mouse_sgr} seq={sent[0][1]!r}"
            )
            info2 = next(p for p in backend.list_panes() if p.pane_id == pid)
            for _ in range(50):  # 最多 ~5s（vim 落点回写）
                if info2.cursor_y == 10:
                    break
                await pilot.pause(0.1)
                info2 = next(p for p in backend.list_panes() if p.pane_id == pid)
            assert info2.cursor_y == 10, (
                f"点击落点错行: cursor_y={info2.cursor_y} 期望 10 "
                f"（sgr={info.mouse_sgr}，F3 回归特征）"
            )

            # ② 滚轮 → 视图真滚动（用户真机报的第二症状）
            first_before = _capture(backend, pid).splitlines()[0]
            sent.clear()
            pw = app.query_one("#pane-0", PaneWidget)
            for _ in range(3):
                ev = MouseScrollDown(
                    pw, 10.0, 5.0, 0, 1, 0, False, False, False,
                    screen_x=float(pw.content_region.x + 10),
                    screen_y=float(pw.content_region.y + 5),
                )
                pw.on_mouse_scroll_down(ev)
                await pilot.pause(0.3)
            for _ in range(50):  # 注入到达（固定 0.8s 余量不足 → 轮询）
                await pilot.pause(0.1)
                if sent:
                    break
            assert sent and all(
                seq.startswith(expect_prefix) for _, seq in sent
            ), f"滚轮未按 vim 编码注入: {sent}"
            first_after = _capture(backend, pid).splitlines()[0]
            for _ in range(50):  # vim 视图真滚动（首行变化）
                if first_after != first_before:
                    break
                await pilot.pause(0.1)
                first_after = _capture(backend, pid).splitlines()[0]
            assert first_before != first_after, (
                f"mouse=a 的 vim 滚轮未生效: {first_before!r} -> {first_after!r}"
            )
            # 收尾：退出 vim 不留死 pane
            backend.send_keys(pid, "C-c")
            backend.send_text(pid, ":q!")
            backend.send_keys(pid, "Enter")
    asyncio.run(scenario())
