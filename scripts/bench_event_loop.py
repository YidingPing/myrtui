#!/usr/bin/env python3
"""内部性能计划（B0B1）§4.2 合成基准：事件循环探针 + 多实例负载一体。

用法（需 textual + 真 tmux，建议在 venv 里跑）::

    python scripts/bench_event_loop.py                      # 1 实例、30s
    python scripts/bench_event_loop.py -n 4 -t 60           # 4 实例并发（日常场景）
    python scripts/bench_event_loop.py -n 4 -t 60 --idle 0.5  # 同上 + B0 后台降频对照

每个「实例」= 一个**独立子进程**里的真 ``TerminalApp``（Textual headless
``run_test``）对接隔离 tmux 会话（临时 socket），pane 里跑流式刷屏循环
（模拟 AI coding UI 满屏输出）。负载驱动：按 ``--input-interval`` 打按键链
（真实 ``_forward_key`` 路径）。探针：每 25ms 一个定时器，记录迟到
（= 事件循环被阻塞的时长）。

输出：每实例探针迟到 p50 / p95 / max（ms）+ backend 子进程调用速率，
以及对照 §1-1 目标（单次阻塞 < 30ms）的判定。旧同步代码上 poll/按键链
执行期间探针迟到 ≈ poll 墙钟（重载 70–100ms）→ 本脚本必然超标。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROBE_INTERVAL = 0.025  # 探针节拍（§4.1 T3 同款）
TARGET_MS = 30.0  # §1-1：事件循环单次阻塞目标
# pane 内流式刷屏（≈ 每 50ms 一整屏 80×24 输出，模拟 AI UI 满屏流式）
STREAM_CMD = (
    "while :; do "
    "for i in {1..24}; do printf 'stream-line-%04d %.0s' $i {1..8}; echo; done; "
    "sleep 0.05; done"
)


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(p * (len(ordered) - 1)))))
    return ordered[idx]


async def run_single(args) -> dict:
    """单实例：真 app + 真 tmux + 探针 + 按键链负载，返回统计。"""
    from myrtui.client.app import TerminalApp
    from myrtui.tmux_backend import TmuxBackend

    name = f"myrtui-bench-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    backend = TmuxBackend(name, name)
    root = tempfile.mkdtemp(prefix="myrtui-bench-")
    backend.create_session(root, 80, 24)

    # backend 子进程调用计数（fork 速率估算；poll 一次 = 1 次 fork）
    calls = {"n": 0}

    def _counting(method: str):
        orig = getattr(backend, method)

        def counted(*a, **k):
            calls["n"] += 1
            return orig(*a, **k)

        return counted

    for method in ("poll", "list_panes", "send_text", "send_keys"):
        setattr(backend, method, _counting(method))

    pane_id = backend.list_panes()[0].pane_id
    backend.send_text(pane_id, STREAM_CMD)  # 起流式刷屏
    backend.send_keys(pane_id, "Enter")

    app = TerminalApp(backend, root)
    lateness: list[float] = []
    state = {"last": 0.0, "t0": 0.0, "calls0": 0}
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.5)  # 首帧拓扑落定后再统计
            state["t0"] = time.monotonic()
            state["last"] = state["t0"]
            state["calls0"] = calls["n"]

            def probe() -> None:
                now = time.monotonic()
                lateness.append(now - state["last"] - PROBE_INTERVAL)
                state["last"] = now

            timer = app.set_interval(PROBE_INTERVAL, probe)
            if args.idle is not None:
                app._idle_interval = args.idle  # B0 后台降频对照
            deadline = time.monotonic() + args.duration
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await pilot.pause(min(args.input_interval, remaining))
                if time.monotonic() >= deadline:
                    break
                for ch in "echo hi":  # 按键链（真实 _forward_key 路径）
                    app._forward_key(ch)
            elapsed = time.monotonic() - state["t0"]
            timer.stop()
    finally:
        backend.kill_server()

    return {
        "p50": _pct(lateness, 0.50) * 1000,
        "p95": _pct(lateness, 0.95) * 1000,
        "p99": _pct(lateness, 0.99) * 1000,
        "over30": sum(1 for x in lateness if x * 1000 > TARGET_MS),
        "max": max(lateness, default=0.0) * 1000,
        "fork_rate": (calls["n"] - state["calls0"]) / elapsed,
        "samples": len(lateness),
    }


def run_child(args) -> int:
    """--single：子进程模式，跑一个实例并把统计打成一行 JSON。"""
    result = asyncio.run(run_single(args))
    print(json.dumps(result))
    return 0


def run_parent(args) -> int:
    """父进程：并行起 n 个独立子进程实例（对应 n 个真实 myrtui 进程）。"""
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    cmd = [
        sys.executable, os.path.abspath(__file__), "--single",
        "-t", str(args.duration),
        "--input-interval", str(args.input_interval),
    ]
    if args.idle is not None:
        cmd += ["--idle", str(args.idle)]
    procs = [
        subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, env=env,
        )
        for _ in range(args.instances)
    ]
    results = []
    for i, proc in enumerate(procs):
        out, _ = proc.communicate()
        try:
            results.append({"idx": i, **json.loads(out.strip().splitlines()[-1])})
        except (ValueError, IndexError):
            print(f"实例 {i} 失败（退出码 {proc.returncode}）：{out!r}", file=sys.stderr)

    print(f"\n{'inst':>4} {'p50(ms)':>9} {'p95(ms)':>9} {'p99(ms)':>9} {'max(ms)':>9} "
          f"{'>30ms':>6} {'fork/s':>8} {'探针数':>7}")
    for r in results:
        print(f"{r['idx']:>4} {r['p50']:>9.1f} {r['p95']:>9.1f} {r['p99']:>9.1f} "
              f"{r['max']:>9.1f} {r['over30']:>6} {r['fork_rate']:>8.1f} "
              f"{r['samples']:>7}")
    worst = max((r["max"] for r in results), default=0.0)
    worst_p95 = max((r["p95"] for r in results), default=0.0)
    over = sum(r["over30"] for r in results)
    total = sum(r["samples"] for r in results)
    print(f"\np95 全实例最大：{worst_p95:.1f} ms；单次最大（max）：{worst:.1f} ms；"
          f">30ms 样本 {over}/{total}")
    verdict = "达标（p95 < 30ms）" if worst_p95 < TARGET_MS else "超标（目标 p95 < 30ms，残余归因见 §7 决策门）"
    print(f"事件循环阻塞 —— {verdict}")
    return 0 if worst_p95 < TARGET_MS else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("-n", "--instances", type=int, default=1,
                   help="并发实例数（独立进程；日常场景 2–4，默认 1）")
    p.add_argument("-t", "--duration", type=float, default=30.0,
                   help="每实例统计时长（秒，默认 30）")
    p.add_argument("--input-interval", type=float, default=0.15,
                   help="按键链驱动间隔（秒，默认 0.15 ≈ 输入后高频节拍）")
    p.add_argument("--idle", type=float, default=None,
                   help="把每实例空闲间隔改成该值（B0 对照，如 0.5）")
    p.add_argument("--single", action="store_true",
                   help=argparse.SUPPRESS)  # 内部：子进程单实例模式
    args = p.parse_args()
    if args.single:
        return run_child(args)
    return run_parent(args)


if __name__ == "__main__":
    sys.exit(main())
