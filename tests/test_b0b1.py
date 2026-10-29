"""PERF-B0B1-PLAN 验收测试：B0 空闲节拍可调 + B1 backend 调用移出事件循环。

- B0：``MYRTUI_IDLE_INTERVAL`` 解析（合法/非法/负数/上下界夹取）+ ``_tick``
  调度确实使用该值（Textual 真定时器下不引 fake clock，直接驱动 ``_tick``
  看 ``_next_poll`` 前后值）；
- B1：T1 顺序 / T2 单飞合并 / T2b 高频段回显可达（合并 offsets 重建）/
  T3 事件循环不阻塞（核心断言）/ T4 错误路径 / T5 teardown。

除 B0 解析外均用 FakeBackend（无 tmux、无子进程）跑 Textual headless。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from myrtui.client import app as app_mod
from myrtui.client.app import (
    IDLE_INTERVAL,
    IDLE_INTERVAL_MAX,
    IDLE_INTERVAL_MIN,
    TerminalApp,
)
from myrtui.client.topbar import TopBar
from myrtui.tmux_backend import CaptureResult, PaneInfo, TmuxBackend, TmuxError


# ---------- FakeBackend（纯内存，记录调用序列） ----------

def make_pane(pane_id: str = "%1", **overrides) -> PaneInfo:
    fields = dict(
        pane_id=pane_id,
        dead=False,
        command="sh",
        alternate_on=False,
        cursor_x=0,
        cursor_y=0,
        cursor_flag=True,
        history_size=100,
        height=24,
        width=80,
        window_id="$1",
        window_index=0,
        mouse_standard=False,
        mouse_button=False,
        mouse_any=False,
        mouse_sgr=True,
        mouse_all=False,
    )
    fields.update(overrides)
    return PaneInfo(**fields)


class FakeBackend:
    """记录调用序列的假后端：poll 可注入延迟/异常（T2/T3/T4/T5 用）。"""

    def __init__(self, *, poll_delay: float = 0.0, poll_error: Exception | None = None):
        self.calls: list[str] = []
        self.offsets_log: list[dict[str, tuple[int, int]]] = []
        self.poll_delay = poll_delay
        self.poll_error = poll_error
        self.pane = make_pane()

    def list_panes(self) -> list[PaneInfo]:
        self.calls.append("list_panes")
        return [self.pane]

    def poll(self, offsets):
        self.calls.append("poll")
        self.offsets_log.append(dict(offsets))
        if self.poll_delay:
            time.sleep(self.poll_delay)
        if self.poll_error is not None:
            raise self.poll_error
        caps = {
            pid: CaptureResult(text=f"cap-{pid}\n", offset=k)
            for pid, (k, _h) in offsets.items()
        }
        return [self.pane], caps

    # ---- 输入 / 鼠标 / paste ----
    def send_text(self, pane_id, text):
        self.calls.append(f"send_text:{text}")

    def send_keys(self, pane_id, *keys):
        self.calls.append(f"send_keys:{','.join(keys)}")

    def send_ctrl_a(self, pane_id):
        self.calls.append("send_ctrl_a")

    def send_mouse(self, pane_id, sequence):
        self.calls.append("send_mouse")

    def paste(self, pane_id, text):
        self.calls.append(f"paste:{text}")

    # ---- 动作类 ----
    def resize_window(self, window_id, cols, rows):
        self.calls.append(f"resize_window:{cols}x{rows}")

    def new_window(self, root):
        self.calls.append("new_window")
        return "$2"

    def kill_window(self, window_id):
        self.calls.append("kill_window")

    def respawn_pane(self, pane_id):
        self.calls.append("respawn_pane")

    def kill_server(self):
        self.calls.append("kill_server")


# ---------- B0：解析（§2.3 的 4 项 + 下界/未设） ----------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0.5", 0.5),                  # 合法值
        ("abc", IDLE_INTERVAL),        # 非法串 → 警告 + 回落
        ("-1", IDLE_INTERVAL),         # 负数 → 警告 + 回落
        ("99", IDLE_INTERVAL_MAX),     # 超上界 → 夹取
        ("0.01", IDLE_INTERVAL_MIN),   # 低于下界 → 夹取
        (None, IDLE_INTERVAL),         # 未设 → 默认
    ],
)
def test_b0_idle_interval_parse(monkeypatch, raw, expected, app_root):
    """解析在 __init__：合法 / 非法串 / 负数 / 上下界夹取 / 未设。"""
    if raw is None:
        monkeypatch.delenv("MYRTUI_IDLE_INTERVAL", raising=False)
    else:
        monkeypatch.setenv("MYRTUI_IDLE_INTERVAL", raw)
    app = TerminalApp(TmuxBackend("s", "s"), app_root)
    assert app._idle_interval == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("0.5", 0.5), (None, IDLE_INTERVAL), ("99", IDLE_INTERVAL_MAX)],
)
def test_b0_tick_schedules_with_env_interval(monkeypatch, raw, expected, app_root):
    """生效：_tick 的空闲调度间隔用的是 _idle_interval（篡改 _next_poll 前后值断言）。"""
    if raw is None:
        monkeypatch.delenv("MYRTUI_IDLE_INTERVAL", raising=False)
    else:
        monkeypatch.setenv("MYRTUI_IDLE_INTERVAL", raw)

    async def scenario():
        app = TerminalApp(FakeBackend(), app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.4)  # 首帧拓扑 + 首轮 poll 落定
            assert app._idle_interval == expected
            app._tick_timer.pause()  # 冻结真定时器，直接驱动 _tick
            app._high_ticks = 0
            app._next_poll = 0.0  # 强制到点，否则 _tick 直接跳过调度段
            before = time.monotonic()
            app._tick()
            assert before + expected - 0.05 <= app._next_poll <= before + expected + 0.05

    asyncio.run(scenario())


# ---------- B1 T1：顺序等价 ----------

def test_b1_t1_send_ordered_before_poll(app_root):
    """T1：按键 send 严格先于随后的 poll；连打 5 键序列 = 发送顺序。"""
    async def scenario():
        backend = FakeBackend()
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.5)  # 初始拓扑 + 首轮 poll 落定
            app._tick_timer.pause()  # 冻结 tick，避免干扰调用序列
            # 首轮 poll 可能仍在 in-flight（macOS runner 上 0.5s 不够）：
            # 此时 clear 会把**迟到的 poll** 记到 send 前面 → 假失败
            # （v0.1.5 发布 CI run 76 实录：calls == ['poll', 'send_text:a', ...]）。
            # 等单飞/尾部合并都排空后再清账。
            deadline = time.monotonic() + 5.0
            while (
                app._poll_in_flight or app._poll_pending
            ) and time.monotonic() < deadline:
                await pilot.pause(0.02)
            backend.calls.clear()

            await pilot.press("a", "b", "c", "d", "e")
            await pilot.pause(0.4)

            sends = [c for c in backend.calls if c.startswith("send_text")]
            assert sends == [
                "send_text:a", "send_text:b", "send_text:c",
                "send_text:d", "send_text:e",
            ], f"发送乱序：{backend.calls}"
            polls = [i for i, c in enumerate(backend.calls) if c == "poll"]
            assert polls, f"burst 应触发 poll：{backend.calls}"
            # 每个 send 都先于它触发的那次 poll（回显语义不变）
            assert backend.calls.index("send_text:a") < polls[0]
            assert polls[-1] > backend.calls.index("send_text:e")

    asyncio.run(scenario())


# ---------- B1 T2：poll 单飞 + 尾部合并 ----------

def test_b1_t2_poll_single_flight_merge(app_root):
    """T2：fake poll 0.2s，连发 5 次 burst 实跑 ≤ 2 次（1 in-flight + 1 pending），
    无队列积压；第二轮 5 次同样合并——计数不随提交次数线性增长。"""
    async def scenario():
        backend = FakeBackend(poll_delay=0.2)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            app._tick_timer.pause()
            backend.calls.clear()

            for _ in range(5):
                app._burst()
            await pilot.pause(0.7)  # 足够完成 in-flight(0.2s) + 合并重提交(0.2s)
            first_round = backend.calls.count("poll")
            assert first_round <= 3, f"5 次 burst 应合并：{backend.calls}"

            for _ in range(5):
                app._burst()
            await pilot.pause(0.7)
            second_round = backend.calls.count("poll") - first_round
            assert second_round <= 3, f"第二轮同样应合并：{backend.calls}"
            # 两轮共 ≤ 6；若无合并会是 10（线性放大即回归）
            assert backend.calls.count("poll") <= 6

    asyncio.run(scenario())


# ---------- B1 T2b：高频段 tick + 合并 offsets 重建 ----------

async def _wait_until(pilot, cond, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """轮询等待条件成立（代替固定 sleep——CI runner 上线程调度/负载余量不一）。"""
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            return False
        await pilot.pause(interval)
    return True


def test_b1_t2b_high_freq_ticks_and_merged_offsets(app_root):
    """T2b：in-flight + 单飞 skip 期间 30ms tick 照发（_high_ticks 递减）；
    完成后 pending 合并的那次 poll 携带最新 offsets（§3.1-3 有意语义）。"""
    async def scenario():
        # poll_delay 取 1.0：给「fetch 已开始 → tick 合并 → 重提交」整段留出
        # 足够窗口（CI macos runner 上 0.05/0.5 固定 sleep 曾实测不够）。
        backend = FakeBackend(poll_delay=1.0)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            app._tick_timer.pause()
            pid = app._pane_ids[0]
            backend.calls.clear()
            backend.offsets_log.clear()

            app._burst()  # in-flight（慢 poll）
            # 等 worker 真正开始执行 fetch（_poll_in_flight 在提交时刻即置位，
            # 固定 sleep 会在线程尚未开跑时误判「单飞失败」）
            assert await _wait_until(
                pilot, lambda: backend.calls.count("poll") == 1
            ), f"fetch 未在 5s 内开始：{backend.calls}"
            assert app._poll_in_flight
            assert backend.calls.count("poll") == 1, f"单飞：队列里只应有 in-flight 那一次：{backend.calls}"
            app._high_ticks = 3
            app._offsets[pid] = 7  # in-flight 期间滚动（history=100 → clamp 后仍 7）
            app._tick()  # 高频 tick 照发：请求合并进 pending，不入队
            assert app._high_ticks == 2, "高频段 tick 应照旧递减"
            assert app._poll_pending
            assert backend.calls.count("poll") == 1, f"pending 合并不得新增执行：{backend.calls}"

            # in-flight 完成 → pending 合并重提交（第二次 fetch 开始）
            assert await _wait_until(
                pilot, lambda: backend.calls.count("poll") == 2, timeout=8.0
            ), f"合并重提交未在 8s 内发生：{backend.calls}"
            # 合并重提交在回调时刻重建 offsets → 携带滚动后的最新值
            assert backend.offsets_log[-1][pid][0] == 7, backend.offsets_log
            # apply 回投完成后再断言不覆盖（等第二次 fetch 落定 + 回调执行）
            assert await _wait_until(
                pilot, lambda: not app._poll_in_flight and not app._poll_pending,
                timeout=8.0,
            ), "重提交未落定"
            assert app._offsets[pid] == 7, "滚动值不得被 apply 回写覆盖"

    asyncio.run(scenario())


# ---------- B1 T3：事件循环不阻塞（本计划核心断言） ----------

def test_b1_t3_event_loop_not_blocked(app_root):
    """T3：fake poll sleep 0.2s 执行期间，独立 25ms 探针最大迟到 < 100ms。

    该用例在旧同步代码上必然红（探针迟到 ≈ 200ms）——B1 的回归锚点。
    """
    async def scenario():
        backend = FakeBackend(poll_delay=0.2)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.6)
            app._tick_timer.pause()

            lateness: list[float] = []
            state = {"last": time.monotonic()}

            def probe() -> None:
                now = time.monotonic()
                lateness.append(now - state["last"] - 0.025)
                state["last"] = now

            timer = app.set_interval(0.025, probe)
            app._burst()  # 0.2s 的慢 poll 应在工作线程执行
            await pilot.pause(0.5)
            timer.stop()

            assert lateness, "探针应至少触发一次"
            worst = max(lateness)
            assert worst < 0.1, f"事件循环被 poll 阻塞：探针最大迟到 {worst * 1000:.1f}ms"

    asyncio.run(scenario())


# ---------- B1 T4：错误路径（断线计数照旧 + 异常被消费） ----------

def test_b1_t4_poll_error_keeps_disconnect_semantics(app_root):
    """T4：fake poll 抛 TmuxError → _poll_failed 计数与断线提示照旧（§9）；
    异常被转成 outcome 消费（future 无 never-retrieved）。"""
    async def scenario():
        backend = FakeBackend(poll_error=TmuxError("probe: server down"))
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline and not app._disconnected:
                await pilot.pause(0.1)
            assert app._disconnected, "poll 连续失败应进入断线态"
            assert app._fail_count >= app_mod.FAIL_LIMIT
            status = app.query_one(TopBar)._status
            assert "失去连接" in status and "重连" in status

    asyncio.run(scenario())


# ---------- B1 §4.2 可选遥测：MYRTUI_LOOP_DEBUG ----------

def test_b1_loop_debug_probe_installs(monkeypatch, app_root):
    """MYRTUI_LOOP_DEBUG=1 → 探针定时器安装；默认关闭不装（零开销）。"""
    monkeypatch.setenv("MYRTUI_LOOP_DEBUG", "1")

    async def scenario():
        app = TerminalApp(FakeBackend(), app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.3)
            assert app._loop_probe_timer is not None

    asyncio.run(scenario())

    monkeypatch.delenv("MYRTUI_LOOP_DEBUG", raising=False)

    async def scenario_off():
        app = TerminalApp(FakeBackend(), app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.3)
            assert app._loop_probe_timer is None

    asyncio.run(scenario_off())


# ---------- B1 T5：teardown 竞态（v0.16 防线复测） ----------

def test_b1_t5_teardown_discards_inflight_result(app_root):
    """T5：in-flight job 于 app 退出后完成 → 无异常、无 NoMatches。

    异常逃逸会被 run_test 的 finally re-raise → 本用例红。
    """
    async def scenario():
        backend = FakeBackend(poll_delay=0.4)
        app = TerminalApp(backend, app_root)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.5)
            app._request_poll()
            assert app._poll_in_flight
            app.exit()
            await asyncio.sleep(0.7)  # job 在 teardown 之后完成 → 结果被丢弃
        assert app._executor_closed

    asyncio.run(scenario())
