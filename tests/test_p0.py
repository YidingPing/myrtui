"""myrtui P0 测试：后端封装 + 前端辅助逻辑。

后端测试跑在隔离 tmux 实例上（临时 socket），结束 kill-server。
"""
from __future__ import annotations

import os
import tempfile
import time
import uuid

import pytest

from myrtui.tmux_backend import (
    TmuxBackend,
    TmuxError,
    parse_tmux_version,
)
from myrtui.client.app import map_key
from myrtui.client.pane_widget import parse_sgr_line, _char_index_at_cell


# ---------- 版本解析（M16 容错） ----------

@pytest.mark.parametrize(
    "out,expected",
    [
        ("tmux 3.8", (3, 8)),
        ("tmux next-3.9", (3, 9)),   # 开发版
        ("tmux 3.4a", (3, 4)),       # 字母版本
        ("openbsd-999", None),       # 失配放行
        ("", None),
    ],
)
def test_parse_tmux_version(out, expected):
    assert parse_tmux_version(out) == expected


# ---------- 键映射（Textual → tmux） ----------

@pytest.mark.parametrize(
    "key,char,expected",
    [
        # 可打印字符（Textual 对符号报 key 名，character 才是字符）
        ("a", "a", ("text", "a")),
        ("minus", "-", ("text", "-")),
        ("underscore", "_", ("text", "_")),
        ("full_stop", ".", ("text", ".")),
        ("space", " ", ("text", " ")),
        # 特殊键 → tmux 键名
        ("enter", "\n", ("keys", "Enter")),
        ("tab", "\t", ("keys", "Tab")),
        ("shift+tab", None, ("keys", "BTab")),
        ("backspace", None, ("keys", "BSpace")),
        ("up", None, ("keys", "Up")),
        ("f5", None, ("keys", "F5")),
        # 单字符基数的修饰键组合（character 是可打印字符，须走键名路径）
        ("ctrl+c", "\x03", ("keys", "C-c")),
        ("alt+x", "x", ("keys", "M-x")),
        # 组合键前缀解析（P0 验收报告 §3-4：旧实现把这些静默丢弃）
        ("alt+up", None, ("keys", "M-Up")),
        ("alt+backspace", None, ("keys", "M-BSpace")),
        ("ctrl+left", None, ("keys", "C-Left")),
        ("ctrl+shift+a", None, ("keys", "C-S-a")),
        ("ctrl+space", None, ("keys", "C-Space")),
        ("alt+f1", None, ("keys", "M-F1")),
        ("ctrl+alt+x", None, ("keys", "C-M-x")),
        ("ctrl+shift+left", None, ("keys", "C-S-Left")),
        ("alt+enter", None, ("keys", "M-Enter")),
        # 无法映射：未知修饰键 / 无 character 的未知键
        ("super+x", None, None),
        ("super", None, None),
        ("hyper+unknown", None, None),
    ],
)
def test_map_key(key, char, expected):
    assert map_key(key, char) == expected


# ---------- SGR 解析 ----------

def test_sgr_basic_colors_and_width():
    line = "\x1b[1;31mred\x1b[0m plain"
    t = parse_sgr_line(line, 10)
    assert t.plain == "red plain "
    # 第一段红色
    assert t.spans[0].start == 0 and t.spans[0].end == 3
    assert "red" in str(t.spans[0].style) or "#cd0000" in str(t.spans[0].style)


def test_sgr_truecolor_and_256():
    t = parse_sgr_line("\x1b[38;2;10;20;30mX\x1b[0m\x1b[48;5;196mY", 4)
    assert "0a141e" in str(t.spans[0].style)  # truecolor hex
    assert "ff0000" in str(t.spans[1].style)  # 256 色 196 → #ff0000


def test_sgr_pads_and_truncates_by_cells():
    # 不足补齐
    t = parse_sgr_line("abc", 6)
    assert t.plain == "abc   "
    # 超出裁切（按 cell 宽度）
    t2 = parse_sgr_line("abcdefghij", 4)
    assert t2.plain == "abcd"


def test_char_index_at_cell_wide_char():
    # CJK 宽字符占 2 cell
    plain = "中a"
    assert _char_index_at_cell(plain, 0) == 0
    assert _char_index_at_cell(plain, 2) == 1  # 'a' 在 cell 2
    assert _char_index_at_cell(plain, 3) == 2  # 末尾


# ---------- 后端：隔离实例集成 ----------

@pytest.fixture
def backend():
    name = f"myrtui-test-{uuid.uuid4().hex[:8]}"
    b = TmuxBackend(name, name)
    root = tempfile.mkdtemp(prefix="myrtui-proj-")
    b.create_session(root, 80, 24)
    yield b
    b.kill_server()


def test_create_session_and_term(backend):
    """M13：初始化序列先 set 后 new-session，pane0 拿到显式 TERM。"""
    assert backend.has_session()
    panes = backend.list_panes()
    assert len(panes) == 1
    p = panes[0]
    assert (p.width, p.height) == (80, 24)
    backend.send_text(p.pane_id, 'echo "TERM=$TERM"')
    backend.send_keys(p.pane_id, "Enter")
    time.sleep(0.5)
    _, caps = backend.poll({p.pane_id: (0, p.height)})
    assert "TERM=tmux-256color" in caps[p.pane_id].text


def test_paging_formula_and_line_assertion(backend):
    """M1/M3/M18：翻页公式 + 行数断言，所有 offset 返回恰好 H 行。"""
    pid = backend.list_panes()[0].pane_id
    for i in range(30):
        backend.send_text(pid, f"echo line-{i}")
        backend.send_keys(pid, "Enter")
    time.sleep(0.8)
    info = backend.list_panes()[0]
    H = info.height
    for k in (0, 1, 5, 10, min(H, info.history_size - 1)):
        _, caps = backend.poll({pid: (k, H)})
        assert len(caps[pid].text.splitlines()) == H, f"k={k}"
        assert caps[pid].offset == k


def test_paging_content_is_history(backend):
    """翻页视图内容应是历史而非实时底部（M1 的核心断言）。"""
    pid = backend.list_panes()[0].pane_id
    for i in range(30):
        backend.send_text(pid, f"echo marker-{i}")
        backend.send_keys(pid, "Enter")
    time.sleep(0.8)
    info = backend.list_panes()[0]
    _, caps = backend.poll({pid: (5, info.height)})
    first = caps[pid].text.splitlines()[0]
    # 顶部应是较老的行（不是实时底部的 marker-29 附近）
    assert "marker-29" not in first


def test_oversized_offset_degrades_via_assertion(backend):
    """越界 offset 静默降级由行数断言兜底，不抛错、返回 H 行。"""
    pid = backend.list_panes()[0].pane_id
    backend.send_text(pid, "echo hi"); backend.send_keys(pid, "Enter")
    time.sleep(0.5)
    info = backend.list_panes()[0]
    _, caps = backend.poll({pid: (info.history_size + 50, info.height)})
    assert len(caps[pid].text.splitlines()) == info.height


def test_paste_bracketed(backend):
    pid = backend.list_panes()[0].pane_id
    backend.send_keys(pid, "cat")
    time.sleep(0.3)
    backend.paste(pid, "pasted-content")
    time.sleep(0.5)
    _, caps = backend.poll({pid: (0, 0)})
    assert "pasted-content" in caps[pid].text
    backend.send_keys(pid, "C-c")


def test_resize_window(backend):
    p = backend.list_panes()[0]
    backend.resize_window(p.window_id, 60, 20)
    time.sleep(0.3)
    p2 = backend.list_panes()[0]
    assert (p2.width, p2.height) == (60, 20)


def test_dead_pane_capture_and_respawn(backend):
    """remain-on-exit：shell 退出后 pane 保留、capture 可用、可 respawn。"""
    pid = backend.list_panes()[0].pane_id
    backend.send_text(pid, "exit")
    backend.send_keys(pid, "Enter")
    for _ in range(20):
        time.sleep(0.3)
        if backend.list_panes()[0].dead:
            break
    assert backend.list_panes()[0].dead
    _, caps = backend.poll({pid: (0, 0)})
    assert len(caps[pid].text.splitlines()) == backend.list_panes()[0].height
    backend.respawn_pane(pid)
    time.sleep(0.5)
    assert not backend.list_panes()[0].dead


def test_poll_raises_on_dead_server(backend):
    backend.kill_server()
    time.sleep(0.3)
    with pytest.raises(TmuxError):
        backend.poll({"%0": (0, 24)})


def test_alt_screen_default_capture(backend):
    """M2：渲染永不追加 -a；默认 capture 取 base.grid，进备用屏抓到备用屏内容。"""
    pid = backend.list_panes()[0].pane_id
    backend.send_text(pid, "printf '\\033[?1049h\\033[2JALT-HERE'")
    backend.send_keys(pid, "Enter")
    time.sleep(0.5)
    info = backend.list_panes()[0]
    assert info.alternate_on
    _, caps = backend.poll({pid: (0, info.height)})
    assert "ALT-HERE" in caps[pid].text
    backend.send_text(pid, "printf '\\033[?1049l'")
    backend.send_keys(pid, "Enter")
    time.sleep(0.5)
    assert not backend.list_panes()[0].alternate_on


# ---------- KillConfirm 安全性（P0 验收报告 §2 必须修） ----------

def test_kill_confirm_defaults_to_safe(backend, app_root):
    """确认框必须是安全默认：焦点在 Cancel、Esc/n 可取消、Enter 不杀会话。

    P0 验收报告 §2：修复前 Esc 无效而默认焦点是 Kill 按钮 → 按 Enter
    会直接杀掉全部 shell（本意防误触的确认框变成误触陷阱）。
    """
    import asyncio

    from myrtui.client.app import TerminalApp

    async def scenario():
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)

            # 1) 默认焦点必须是 Cancel
            app.action_kill()
            await pilot.pause(0.4)
            assert app.screen.__class__.__name__ == "KillConfirm"
            assert app.screen.focused is not None
            assert app.screen.focused.id == "no", "默认焦点应为 Cancel"

            # 2) Esc 关闭且不杀会话
            await pilot.press("escape")
            await pilot.pause(0.4)
            assert app.screen.__class__.__name__ != "KillConfirm", "Esc 应关闭弹窗"
            assert backend.has_session(), "Esc 取消不应杀会话"

            # 3) n 键同样取消
            app.action_kill()
            await pilot.pause(0.4)
            await pilot.press("n")
            await pilot.pause(0.4)
            assert app.screen.__class__.__name__ != "KillConfirm", "n 应关闭弹窗"
            assert backend.has_session()

            # 4) 焦点在 Cancel 时按 Enter → 仍不杀（双保险）
            app.action_kill()
            await pilot.pause(0.4)
            assert app.screen.focused.id == "no"
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert backend.has_session(), "Enter 落在 Cancel 上不应杀会话"

            # 5) 点击 Kill 按钮 → 确认后才杀（正常路径不受影响）
            app.action_kill()
            await pilot.pause(0.4)
            await pilot.click("#yes")
            await pilot.pause(0.8)
            assert not backend.has_session(), "点击 Kill 应执行 kill"

    asyncio.run(scenario())
